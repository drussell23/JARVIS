"""Slice 12Z — Invariant Drift Cooperative Refactor.

bt-2026-05-23-221029 (Slice 12Y validation soak) LoopDeadman
tombstone captured the exact wedge stack:

    File ".../pathlib.py:1059 in read_text"
    File ".../meta/cross_kingdom_boundary.py:163 in _scan_one_file"
    File ".../meta/cross_kingdom_boundary.py:243 in scan_governance_tree"
    File ".../meta/cross_kingdom_boundary.py:291 in _validate_cross_kingdom_boundary"
    File ".../meta/shipped_code_invariants.py:2526 in validate_invariants_grouped"
    File ".../meta/shipped_code_invariants.py:2654 in validate_all_async"
    File ".../invariant_drift_auditor.py:639 in _capture_shipped_invariants_async"
    File ".../invariant_drift_auditor.py:671 in capture_snapshot_async"

The async-pool path in :func:`validate_all_async` worked correctly, but
its **sync fallback** (line 2654: ``return validate_invariants_grouped()``)
ran on the main asyncio loop when the process pool was unavailable
(PermissionError observed in production). That fallback called
:func:`scan_governance_tree` — sync ``rglob`` + ``read_text`` over the
entire governance/ tree — holding the GIL for 301.8s until LoopDeadman
fired. SidecarProfiler caught 4 in-progress STUCK_FRAME emissions all
pointing at ``pathlib.read_text`` / ``pathlib.stat`` / ``pathlib.open``.

Slice 12Z closes the wedge via TWO composable disciplines:

# Discipline 1 — Async-cooperative ``scan_governance_tree_async``

NEW async sibling in ``cross_kingdom_boundary.py``. Composes the
Slice 12U :mod:`cooperative_fs_io` substrate:

  * :func:`iter_files_cooperative` — async iteration with
    ``asyncio.sleep(0)`` yields every N items (default 64 via
    ``JARVIS_EVENT_LOOP_YIELD_EVERY_N``) so the heartbeat coroutine
    + SDK stream consumer get scheduling slots throughout the scan
  * :func:`read_text_offloaded` — per-file read dispatches to the
    dedicated ``advisor-blast`` ThreadPoolExecutor (Slice 12T Part 3
    isolation contract)
  * :func:`offload_blocking` — per-file AST parse + walk via the
    Slice 12U canonical primitive (cumulative substring scan +
    AST work offloaded together so the GIL releases between
    files)

Result parity with sync sibling pinned by
:meth:`TestScanResultParity.test_async_matches_sync`. Same
violation-string format. NEW helper
:func:`_scan_one_file_from_source` lifted to module level so the
offload worker doesn't capture caller-local state.

# Discipline 2 — ``validate_all_async`` sync-fallback offload

Slice 12Z modifies ``validate_all_async`` so its
exception-fallback path (when the process pool fails) dispatches
``validate_invariants_grouped`` via
:func:`event_loop_governance.offload_blocking` instead of calling
sync on the loop. This is the architectural fix that closes the
proven wedge: even when the pool is unavailable, the sync
grouped-validation runs on a worker thread; the loop continues
ticking; LoopDeadman never trips on this path again.

A double-fallback guard remains: if even ``offload_blocking``
fails (e.g., ``event_loop_governance`` not importable in some
degraded build), the absolute-last-resort sync call still runs —
preserves the observability-non-authoritative contract that says
"missing pin > hard crash".
"""

from __future__ import annotations

import ast
import asyncio
import os
import sys
import threading
import time
from pathlib import Path
from typing import Tuple

import pytest

from backend.core.ouroboros.governance.meta import cross_kingdom_boundary
from backend.core.ouroboros.governance.meta.cross_kingdom_boundary import (
    _scan_one_file_from_source,
    scan_governance_tree,
    scan_governance_tree_async,
)


# ──────────────────────────────────────────────────────────────────────
# Shared fixtures
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def fake_governance_tree(tmp_path: Path) -> Path:
    """Build a small governance/-like tree with mixed clean +
    forbidden imports. Mirrors the real tree's structure
    (subdirs + .py files) without being so large that test
    runtime explodes."""
    # Clean files.
    (tmp_path / "clean_a.py").write_text(
        "import json\nimport os\n",
    )
    (tmp_path / "clean_b.py").write_text(
        "from backend.core.ouroboros import oracle\n",
    )
    # Subdir with one forbidden import.
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "violator_top.py").write_text(
        "from backend.core.coding_council import foo\n",
    )
    # Lazy nested forbidden import.
    (sub / "violator_nested.py").write_text(
        "def f():\n"
        "    import backend.core.coding_council.bar\n",
    )
    # __pycache__ — must be skipped.
    pyc = tmp_path / "__pycache__"
    pyc.mkdir()
    (pyc / "ignored.py").write_text(
        "from backend.core.coding_council import IGNORED\n",
    )
    # A few extra clean files to give the iterator some bulk
    # for the heartbeat-yield test.
    for i in range(15):
        (tmp_path / f"extra_{i:02d}.py").write_text(
            f"# extra clean file {i}\nprint('hi')\n",
        )
    return tmp_path


# ──────────────────────────────────────────────────────────────────────
# Discipline 1 — scan_governance_tree_async basic correctness
# ──────────────────────────────────────────────────────────────────────


class TestScanAsyncShape:
    def test_is_coroutine_function(self):
        assert asyncio.iscoroutinefunction(
            scan_governance_tree_async,
        ), (
            "scan_governance_tree_async no longer async — "
            "Slice 12Z core regression"
        )

    def test_helper_is_module_level(self):
        """The offload worker MUST be module-level (not a
        closure) so the executor doesn't capture caller state."""
        assert callable(_scan_one_file_from_source)
        import inspect
        sig = inspect.signature(_scan_one_file_from_source)
        params = list(sig.parameters.keys())
        assert params == ["source", "forbidden_prefix"], (
            f"_scan_one_file_from_source signature drifted: {params}"
        )

    def test_exports_present(self):
        for name in (
            "scan_governance_tree_async",
            "_scan_one_file_from_source",
        ):
            assert hasattr(cross_kingdom_boundary, name)
            assert name in cross_kingdom_boundary.__all__


class TestScanResultParity:
    """The async path MUST return the same violations as the
    sync path for the same input tree. This is the load-bearing
    correctness claim — if it drifts, the refactor regressed
    invariant accuracy."""

    @pytest.mark.asyncio
    async def test_async_matches_sync(self, fake_governance_tree):
        sync_result = scan_governance_tree(
            governance_root_override=fake_governance_tree,
        )
        async_result = await scan_governance_tree_async(
            governance_root_override=fake_governance_tree,
        )
        assert set(sync_result) == set(async_result), (
            f"Async scan returned different violations:\n"
            f"  sync : {sync_result}\n"
            f"  async: {async_result}"
        )

    @pytest.mark.asyncio
    async def test_pycache_skipped_in_async(
        self, fake_governance_tree,
    ):
        """__pycache__/ MUST be skipped in async path too —
        same exemption discipline as sync."""
        result = await scan_governance_tree_async(
            governance_root_override=fake_governance_tree,
        )
        for v in result:
            assert "__pycache__" not in v, (
                f"async scan reported __pycache__ violation: {v}"
            )

    @pytest.mark.asyncio
    async def test_detects_top_level_forbidden_import(
        self, fake_governance_tree,
    ):
        result = await scan_governance_tree_async(
            governance_root_override=fake_governance_tree,
        )
        # violator_top.py:1 should be flagged.
        assert any(
            "violator_top.py:1" in v and "coding_council" in v
            for v in result
        ), f"top-level forbidden import not detected: {result}"

    @pytest.mark.asyncio
    async def test_detects_lazy_nested_forbidden_import(
        self, fake_governance_tree,
    ):
        result = await scan_governance_tree_async(
            governance_root_override=fake_governance_tree,
        )
        # violator_nested.py:2 (nested inside def f()) flagged.
        assert any(
            "violator_nested.py:2" in v and "coding_council" in v
            for v in result
        ), f"nested forbidden import not detected: {result}"


# ──────────────────────────────────────────────────────────────────────
# Discipline 1 — Non-blocking proof (THE load-bearing claim)
# ──────────────────────────────────────────────────────────────────────


class TestScanAsyncNonBlocking:
    """The whole point of Slice 12Z: a concurrent heartbeat
    coroutine MUST accumulate ticks during the scan. Pre-Slice-12Z
    the sync fallback wedged for 301.8s on the same code path."""

    @pytest.mark.asyncio
    async def test_heartbeat_ticks_during_scan(
        self, fake_governance_tree, monkeypatch,
    ):
        # Aggressive yield cadence so the small fake tree
        # (~20 .py files) still triggers cooperative yields.
        monkeypatch.setenv(
            "JARVIS_EVENT_LOOP_YIELD_EVERY_N", "2",
        )
        ticks = 0
        scan_running = True

        async def heartbeat():
            nonlocal ticks
            while scan_running:
                ticks += 1
                await asyncio.sleep(0.001)

        hb_task = asyncio.create_task(heartbeat())
        await asyncio.sleep(0.01)
        ticks_before = ticks

        await scan_governance_tree_async(
            governance_root_override=fake_governance_tree,
        )

        scan_running = False
        await asyncio.sleep(0.01)
        hb_task.cancel()
        try:
            await hb_task
        except asyncio.CancelledError:
            pass
        ticks_during = ticks - ticks_before
        assert ticks_during >= 1, (
            f"Heartbeat starved during scan_governance_tree_async: "
            f"ticks={ticks_during} — Slice 12Z exorcism failed; "
            "scan still wedges the loop"
        )


# ──────────────────────────────────────────────────────────────────────
# Discipline 1 — AST drift-prevention
# ──────────────────────────────────────────────────────────────────────


class TestScanAsyncASTPins:
    def _read(self) -> str:
        return Path(
            "backend/core/ouroboros/governance/meta/"
            "cross_kingdom_boundary.py"
        ).read_text()

    def test_async_uses_cooperative_fs_io(self):
        src = self._read()
        tree = ast.parse(src)
        target = None
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.AsyncFunctionDef)
                and node.name == "scan_governance_tree_async"
            ):
                target = node
                break
        assert target is not None
        body = ast.unparse(target)
        assert "iter_files_cooperative" in body, (
            "scan_governance_tree_async no longer uses "
            "iter_files_cooperative — Slice 12Z exorcism broken"
        )
        assert "read_text_offloaded" in body, (
            "scan_governance_tree_async no longer uses "
            "read_text_offloaded — per-file reads back on loop"
        )

    def test_async_does_not_call_rglob_directly(self):
        """The async path MUST go through the substrate. A
        direct ``root.rglob(...)`` call would bypass the
        cooperative yields and re-introduce the wedge."""
        src = self._read()
        tree = ast.parse(src)
        target = None
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.AsyncFunctionDef)
                and node.name == "scan_governance_tree_async"
            ):
                target = node
                break
        assert target is not None
        for inner in ast.walk(target):
            if isinstance(inner, ast.Call):
                fn = inner.func
                if (
                    isinstance(fn, ast.Attribute)
                    and fn.attr == "rglob"
                ):
                    pytest.fail(
                        "scan_governance_tree_async calls "
                        ".rglob() directly — bypasses Slice 12Z "
                        "cooperative substrate; loop will wedge"
                    )

    def test_async_does_not_call_path_read_text(self):
        """Per-file reads MUST go via ``read_text_offloaded``
        (dedicated executor). Direct ``Path.read_text()`` is the
        wedge pattern."""
        src = self._read()
        tree = ast.parse(src)
        target = None
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.AsyncFunctionDef)
                and node.name == "scan_governance_tree_async"
            ):
                target = node
                break
        assert target is not None
        for inner in ast.walk(target):
            if isinstance(inner, ast.Call):
                fn = inner.func
                # Direct call like `path.read_text(...)`.
                if (
                    isinstance(fn, ast.Attribute)
                    and fn.attr == "read_text"
                ):
                    pytest.fail(
                        "scan_governance_tree_async calls "
                        ".read_text() directly — bypasses "
                        "Slice 12Z offload; per-file reads "
                        "wedge the loop"
                    )


# ──────────────────────────────────────────────────────────────────────
# Discipline 2 — validate_all_async sync-fallback offload
# ──────────────────────────────────────────────────────────────────────


class TestValidateAllAsyncFallbackOffload:
    """The bt-2026-05-23-221029 fix point: when the process
    pool fails, the sync fallback MUST run via offload_blocking
    (thread pool) instead of sync on the loop."""

    def _read(self) -> str:
        return Path(
            "backend/core/ouroboros/governance/meta/"
            "shipped_code_invariants.py"
        ).read_text()

    def test_fallback_uses_offload_blocking(self):
        """AST pin: the validate_all_async exception fallback
        MUST reference ``offload_blocking``. Without this, a
        pool failure re-introduces the wedge."""
        src = self._read()
        # Find validate_all_async function via AST.
        tree = ast.parse(src)
        target = None
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.AsyncFunctionDef)
                and node.name == "validate_all_async"
            ):
                target = node
                break
        assert target is not None, (
            "validate_all_async missing — Slice 12Z fix has "
            "no home"
        )
        body = ast.unparse(target)
        assert "offload_blocking" in body, (
            "validate_all_async fallback no longer references "
            "offload_blocking — Slice 12Z wedge fix regressed; "
            "pool-failure path will wedge the loop again"
        )

    def test_slice12z_marker_present(self):
        src = self._read()
        assert "Slice 12Z" in src, (
            "Slice 12Z marker comment removed from "
            "shipped_code_invariants.py — refactor lost"
        )

    def test_double_fallback_preserves_last_resort_sync(self):
        """When even offload_blocking fails (degraded build
        without event_loop_governance), the absolute last
        resort is still a direct sync call — observability
        non-authoritative contract."""
        src = self._read()
        # The two-stage fallback: offload_blocking inside an
        # outer try; sync inside its inner except.
        # AST inspection of the fallback structure.
        tree = ast.parse(src)
        target = None
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.AsyncFunctionDef)
                and node.name == "validate_all_async"
            ):
                target = node
                break
        assert target is not None
        body_text = ast.unparse(target)
        # Both paths must reference the sync helper —
        # `validate_invariants_grouped` appears in the
        # offload arg AND in the absolute-last-resort branch.
        assert body_text.count(
            "validate_invariants_grouped"
        ) >= 2, (
            "validate_all_async fallback chain incomplete — "
            "expected offload_blocking(validate_invariants_grouped) "
            "+ absolute-last-resort sync fallback (2 references)"
        )


# ──────────────────────────────────────────────────────────────────────
# Cross-discipline sanity
# ──────────────────────────────────────────────────────────────────────


class TestSyncSiblingUnchanged:
    """The sync ``scan_governance_tree`` is still called by tests
    + other sync callers. Slice 12Z MUST NOT break it."""

    def test_sync_still_callable(self, fake_governance_tree):
        result = scan_governance_tree(
            governance_root_override=fake_governance_tree,
        )
        assert isinstance(result, tuple)
        # 2 forbidden imports in the fake tree (top + nested).
        assert len(result) == 2


class TestRegisterShippedInvariantsUnchanged:
    """The validator registration is the prod-facing API."""

    def test_register_returns_one_invariant(self):
        invs = cross_kingdom_boundary.register_shipped_invariants()
        # Single invariant registered:
        # governance_no_coding_council_imports.
        assert len(invs) == 1
        assert (
            invs[0].invariant_name
            == "governance_no_coding_council_imports"
        )
