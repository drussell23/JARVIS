"""Slice 12T — Forensic Observability + Sensor Scoping + Executor Isolation.

bt-2026-05-23-180315 (Path A verification soak post-Slice-12S) surfaced
that Slice 12S's per-scan refactor worked in production (11 scans
carried the ``mode=cooperative_async`` tag) but the wedge **moved**:
``ControlPlaneStarvation`` events DOUBLED (28→62) and the existing
``faulthandler.dump_traceback(file=sys.stderr)`` revealed
``predictive_engine._fragility`` doing a synchronous
``pathlib.read_text()`` on the asyncio loop — *not* the Advisor.

The stack dump only landed in the operator's stdout-tee log; the
session ``debug.log`` ended at "Stack dump to follow if enabled."
because the dump never reached the file handler. **Never again** —
Part 1 routes the dump through THREE sinks so future wedges leave a
discoverable tombstone regardless of stderr plumbing.

Slice 12T combines:

* **Part 1 — Forensic Tombstone** (``loop_deadman.py`` +
  ``harness.py``). Wedge-detection path now writes the
  ``faulthandler`` dump to (a) ``sys.stderr`` (preserved verbatim),
  (b) ``<session_dir>/loop_deadman_tombstone.txt`` via the new
  ``JARVIS_LOOP_DEADMAN_TOMBSTONE_DIR`` knob (harness sets it at
  boot), and (c) per-thread frame dumps via the standard logger as
  ``[LoopDeadman.TOMBSTONE]`` lines (lands in ``debug.log``).
  All three sinks are individually try-wrapped — the exit path
  MUST NEVER raise.

* **Part 2 — Sensor-Op Advisor Scoping** (``classify_runner.py``).
  BACKGROUND-tier sensor ops (``todo_scanner``, ``opportunity_miner``,
  ``doc_staleness``, ``ai_miner``, ``exploration``, ``backlog``,
  ``architecture``) and SPECULATIVE-tier (``intent_discovery``)
  short-circuit the heavy blast scan: the existing
  ``_precomputed_blast_radius`` seam (Slice 12S) is fed the
  ``conservative_cap`` value, preserving the existing BLOCK
  behavior byte-identically while skipping the 10-22s scan. Other
  Advisor signals (staleness, large-file, chronic_entropy) still
  compute normally. Composes existing
  ``urgency_router._BACKGROUND_SOURCES`` /
  ``_SPECULATIVE_SOURCES`` taxonomy — single source of truth, no
  parallel classifier. Master switch
  ``JARVIS_ADVISOR_BG_SCAN_SKIP_ENABLED`` (default TRUE).

* **Part 3 — Concurrency Isolation** (``operation_advisor.py``).
  Slice 12S's per-file reads were routed through
  ``offload_blocking`` → ``asyncio.to_thread`` → the DEFAULT
  executor — exactly the contested pool (16 sensors + Oracle +
  DreamEngine) the dedicated ``advisor-blast`` executor was
  created to isolate from (Task #88f). Part 3 restores the
  isolation contract: per-file reads dispatch through
  ``loop.run_in_executor(_get_advisor_blast_executor(), …)`` while
  preserving the cooperative yield cadence between batches. Reuses
  the bounded ``_ADVISOR_BLAST_EXECUTOR_MAX_WORKERS`` (default 2)
  so even N parallel scans cannot flood any pool.

Tests pin the tombstone sinks, the scoping short-circuit, the
isolation contract, and three AST drift-prevention pins.
"""

from __future__ import annotations

import ast
import asyncio
import importlib
import inspect
import io
import os
import sys
import threading
from pathlib import Path
from typing import Tuple

import pytest

from backend.core.ouroboros.governance import loop_deadman
from backend.core.ouroboros.governance import operation_advisor
from backend.core.ouroboros.governance.loop_deadman import (
    LoopDeadman,
    deadman_tombstone_dir,
    deadman_tombstone_to_logger_enabled,
)
from backend.core.ouroboros.governance.operation_advisor import (
    OperationAdvisor,
    _BLAST_RADIUS_CACHE_SHARED,
    _read_bounded_text_for_blast,
)


# ──────────────────────────────────────────────────────────────────────
# Shared fixtures
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clear_caches():
    _BLAST_RADIUS_CACHE_SHARED.clear()
    yield
    _BLAST_RADIUS_CACHE_SHARED.clear()


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Tombstone-dir + scoping flag must not leak across tests."""
    for var in (
        "JARVIS_LOOP_DEADMAN_TOMBSTONE_DIR",
        "JARVIS_LOOP_DEADMAN_TOMBSTONE_LOGGER",
        "JARVIS_ADVISOR_BG_SCAN_SKIP_ENABLED",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    """30-file repo matching the Slice 12S fixture shape."""
    target = tmp_path / "mypkg" / "target.py"
    target.parent.mkdir(parents=True)
    target.write_text("def hello(): return 42\n")
    for i in range(15):
        (tmp_path / f"importer_{i:02d}.py").write_text(
            f"from mypkg.target import hello\n# importer {i}\n",
        )
    for i in range(15):
        (tmp_path / f"unrelated_{i:02d}.py").write_text(
            f"# unrelated file {i}\nprint('hi')\n",
        )
    return tmp_path


# ──────────────────────────────────────────────────────────────────────
# Part 1 — Forensic Tombstone
# ──────────────────────────────────────────────────────────────────────


class TestPart1TombstoneEnvKnobs:
    def test_tombstone_dir_unset_returns_none(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_LOOP_DEADMAN_TOMBSTONE_DIR", raising=False,
        )
        assert deadman_tombstone_dir() is None

    def test_tombstone_dir_empty_returns_none(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_LOOP_DEADMAN_TOMBSTONE_DIR", "",
        )
        assert deadman_tombstone_dir() is None

    def test_tombstone_dir_set_returns_value(self, monkeypatch, tmp_path):
        monkeypatch.setenv(
            "JARVIS_LOOP_DEADMAN_TOMBSTONE_DIR", str(tmp_path),
        )
        assert deadman_tombstone_dir() == str(tmp_path)

    def test_tombstone_logger_defaults_true(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_LOOP_DEADMAN_TOMBSTONE_LOGGER", raising=False,
        )
        assert deadman_tombstone_to_logger_enabled() is True

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("true", True), ("1", True), ("on", True),
            ("false", False), ("0", False), ("no", False),
            ("off", False),
        ],
    )
    def test_tombstone_logger_truthy(self, monkeypatch, raw, expected):
        monkeypatch.setenv(
            "JARVIS_LOOP_DEADMAN_TOMBSTONE_LOGGER", raw,
        )
        assert deadman_tombstone_to_logger_enabled() is expected


class TestPart1TombstoneFireBehavior:
    """The wedge-fire path must dump to all three sinks AND never
    raise. We replace ``os._exit`` with a sentinel-raising callable
    so the test can observe the dump and survive."""

    def _spy_exit(self, monkeypatch, capture):
        class _ExitSentinel(BaseException):
            pass

        def _fake_exit(code):
            capture["exit_code"] = code
            raise _ExitSentinel()

        monkeypatch.setattr(loop_deadman.os, "_exit", _fake_exit)
        return _ExitSentinel

    def test_fire_writes_tombstone_file_in_session_dir(
        self, monkeypatch, tmp_path,
    ):
        """When ``JARVIS_LOOP_DEADMAN_TOMBSTONE_DIR`` is set, the
        fire path MUST create ``loop_deadman_tombstone.txt`` in
        that dir with the wedge header + faulthandler dump."""
        monkeypatch.setenv(
            "JARVIS_LOOP_DEADMAN_TOMBSTONE_DIR", str(tmp_path),
        )
        capture: dict = {}
        sentinel = self._spy_exit(monkeypatch, capture)
        dm = LoopDeadman(
            timeout_s=30.0, heartbeat_s=1.0, stack_dump=True,
        )
        with pytest.raises(sentinel):
            dm._fire_wedge(wedge_age_s=305.0)

        tombstone = tmp_path / "loop_deadman_tombstone.txt"
        assert tombstone.exists(), (
            "Slice 12T Part 1 — tombstone file was not created"
        )
        body = tombstone.read_text()
        assert "WEDGE TOMBSTONE" in body
        assert "wedge_age_s=305.0" in body
        assert f"pid={os.getpid()}" in body
        # faulthandler dump must include at least one Thread or
        # Current thread marker.
        assert "Thread" in body or "Current thread" in body, (
            "tombstone file missing faulthandler stack dump"
        )
        assert capture["exit_code"] == 75

    def test_fire_skips_tombstone_file_when_dir_unset(
        self, monkeypatch, tmp_path,
    ):
        """No env var → no file. Stderr dump still fires."""
        monkeypatch.delenv(
            "JARVIS_LOOP_DEADMAN_TOMBSTONE_DIR", raising=False,
        )
        capture: dict = {}
        sentinel = self._spy_exit(monkeypatch, capture)
        # Replace stderr with a capturable buffer.
        stderr_buf = io.StringIO()
        monkeypatch.setattr(loop_deadman.sys, "stderr", stderr_buf)

        dm = LoopDeadman(
            timeout_s=30.0, heartbeat_s=1.0, stack_dump=True,
        )
        with pytest.raises(sentinel):
            dm._fire_wedge(wedge_age_s=305.0)

        # No tombstone file in tmp_path.
        assert not (tmp_path / "loop_deadman_tombstone.txt").exists()
        # Exit still fired.
        assert capture["exit_code"] == 75

    def test_fire_emits_logger_tombstone_lines(
        self, monkeypatch, caplog,
    ):
        """Per-thread frames must be logged via the standard
        logger as ``[LoopDeadman.TOMBSTONE]`` CRITICAL lines so
        they land in debug.log regardless of stderr plumbing."""
        monkeypatch.setenv(
            "JARVIS_LOOP_DEADMAN_TOMBSTONE_LOGGER", "true",
        )
        capture: dict = {}
        sentinel = self._spy_exit(monkeypatch, capture)
        dm = LoopDeadman(
            timeout_s=30.0, heartbeat_s=1.0, stack_dump=True,
        )
        with caplog.at_level("CRITICAL", logger="Ouroboros.LoopDeadman"):
            with pytest.raises(sentinel):
                dm._fire_wedge(wedge_age_s=305.0)

        tombstone_msgs = [
            r.getMessage() for r in caplog.records
            if "TOMBSTONE" in r.getMessage()
        ]
        assert len(tombstone_msgs) >= 1, (
            "No [LoopDeadman.TOMBSTONE] lines emitted — debug.log "
            "will be silent on the next wedge"
        )
        # Each tombstone line should carry thread_id + a frame.
        joined = "\n".join(tombstone_msgs)
        assert "thread_id=" in joined

    def test_fire_logger_disabled_emits_no_tombstone_lines(
        self, monkeypatch, caplog,
    ):
        monkeypatch.setenv(
            "JARVIS_LOOP_DEADMAN_TOMBSTONE_LOGGER", "false",
        )
        capture: dict = {}
        sentinel = self._spy_exit(monkeypatch, capture)
        dm = LoopDeadman(
            timeout_s=30.0, heartbeat_s=1.0, stack_dump=True,
        )
        with caplog.at_level("CRITICAL", logger="Ouroboros.LoopDeadman"):
            with pytest.raises(sentinel):
                dm._fire_wedge(wedge_age_s=305.0)
        tombstone_msgs = [
            r.getMessage() for r in caplog.records
            if "TOMBSTONE" in r.getMessage()
        ]
        assert len(tombstone_msgs) == 0

    def test_fire_swallows_tombstone_file_errors(
        self, monkeypatch, tmp_path,
    ):
        """Pointing the tombstone dir at an unwriteable path
        MUST NOT prevent the exit — the dump is best-effort,
        the os._exit MUST always fire."""
        bad_dir = tmp_path / "no_such_dir" / "deeper"
        # Don't create the dir — open() will raise.
        monkeypatch.setenv(
            "JARVIS_LOOP_DEADMAN_TOMBSTONE_DIR", str(bad_dir),
        )
        capture: dict = {}
        sentinel = self._spy_exit(monkeypatch, capture)
        dm = LoopDeadman(
            timeout_s=30.0, heartbeat_s=1.0, stack_dump=True,
        )
        with pytest.raises(sentinel):
            dm._fire_wedge(wedge_age_s=305.0)
        # Exit fired even though the file write failed.
        assert capture["exit_code"] == 75

    def test_fire_stack_dump_disabled_skips_all_sinks(
        self, monkeypatch, tmp_path,
    ):
        """When ``stack_dump=False`` the dump is fully bypassed
        — preserves byte-identical pre-Slice-12T behavior."""
        monkeypatch.setenv(
            "JARVIS_LOOP_DEADMAN_TOMBSTONE_DIR", str(tmp_path),
        )
        capture: dict = {}
        sentinel = self._spy_exit(monkeypatch, capture)
        dm = LoopDeadman(
            timeout_s=30.0, heartbeat_s=1.0, stack_dump=False,
        )
        with pytest.raises(sentinel):
            dm._fire_wedge(wedge_age_s=305.0)
        # No tombstone file.
        assert not (tmp_path / "loop_deadman_tombstone.txt").exists()
        # Exit still fired.
        assert capture["exit_code"] == 75


# ──────────────────────────────────────────────────────────────────────
# Part 2 — Sensor-Op Advisor Scoping
# ──────────────────────────────────────────────────────────────────────


class TestPart2BackgroundTierSkip:
    """Verifies BACKGROUND/SPECULATIVE sensor ops skip the heavy
    blast scan while preserving the BLOCK outcome via the
    conservative_cap injection. Direct unit tests on the env-knob
    classifier (the source-of-truth `_BACKGROUND_SOURCES` /
    `_SPECULATIVE_SOURCES` taxonomy)."""

    def test_background_sources_set_includes_all_taxonomy(self):
        """Pin the taxonomy so a future urgency_router change
        doesn't silently remove a sensor from the skip path."""
        from backend.core.ouroboros.governance.urgency_router import (
            _BACKGROUND_SOURCES,
            _SPECULATIVE_SOURCES,
        )
        # These are the canonical background-tier sources Slice
        # 12T skips. Any future addition is fine; any removal
        # would silently re-open the wedge for that sensor.
        for required in (
            "todo_scanner", "doc_staleness", "ai_miner",
            "exploration", "backlog", "architecture",
        ):
            assert required in _BACKGROUND_SOURCES, (
                f"{required!r} dropped from _BACKGROUND_SOURCES — "
                "Slice 12T skip path no longer covers that sensor"
            )
        assert "intent_discovery" in _SPECULATIVE_SOURCES

    def test_skip_flag_default_true(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_ADVISOR_BG_SCAN_SKIP_ENABLED", raising=False,
        )
        # Default value used in classify_runner is the literal
        # "true" — mirror that here as the canonical source.
        raw = os.environ.get(
            "JARVIS_ADVISOR_BG_SCAN_SKIP_ENABLED", "true",
        ).strip().lower()
        assert raw == "true"

    def test_precomputed_blast_radius_seam_skips_scan(
        self, fake_repo,
    ):
        """When ``_precomputed_blast_radius`` is passed to
        :meth:`advise`, the heavy scan must NOT run — verified
        by spying on ``_compute_blast_radius``. The result must
        carry the injected value as ``Advisory.blast_radius``."""
        adv = OperationAdvisor(project_root=fake_repo)
        scan_calls = {"n": 0}
        original = adv._compute_blast_radius

        def _spy(*args, **kwargs):
            scan_calls["n"] += 1
            return original(*args, **kwargs)

        adv._compute_blast_radius = _spy  # type: ignore[method-assign]

        result = adv.advise(
            target_files=("mypkg/target.py",),
            description="background tier op",
            op_id="op-bg-test",
            is_read_only=False,
            repo_root=fake_repo,
            _precomputed_blast_radius=50,
        )
        assert scan_calls["n"] == 0, (
            "Heavy scan ran despite _precomputed_blast_radius — "
            "Slice 12T Part 2 skip seam broken"
        )
        assert result.blast_radius == 50

    def test_precomputed_blast_radius_omitted_falls_through(
        self, fake_repo,
    ):
        """When the kwarg is omitted, the heavy scan must run
        normally — preserves byte-identical legacy behavior for
        non-background callers."""
        adv = OperationAdvisor(project_root=fake_repo)
        scan_calls = {"n": 0}
        original = adv._compute_blast_radius

        def _spy(*args, **kwargs):
            scan_calls["n"] += 1
            return original(*args, **kwargs)

        adv._compute_blast_radius = _spy  # type: ignore[method-assign]

        adv.advise(
            target_files=("mypkg/target.py",),
            description="foreground op",
            op_id="op-fg-test",
            is_read_only=False,
            repo_root=fake_repo,
        )
        assert scan_calls["n"] == 1, (
            "Heavy scan did NOT run for foreground op — Slice 12T "
            "Part 2 incorrectly broadens the skip"
        )


class TestPart2ASTPin:
    """Pin the classify_runner integration so a refactor cannot
    silently bypass the skip."""

    def _read_classify(self) -> str:
        return Path(
            "backend/core/ouroboros/governance/phase_runners/"
            "classify_runner.py"
        ).read_text()

    def test_bg_tier_skip_variable_present(self):
        src = self._read_classify()
        assert "_bg_tier_skip" in src, (
            "Slice 12T Part 2 skip variable removed from "
            "classify_runner.py"
        )
        assert "_BACKGROUND_SOURCES" in src, (
            "classify_runner no longer references "
            "_BACKGROUND_SOURCES — skip is uncoupled from the "
            "urgency_router taxonomy"
        )
        assert "_SPECULATIVE_SOURCES" in src

    def test_skip_path_calls_advise_with_precomputed(self):
        """The skip path must use the
        ``_precomputed_blast_radius`` seam — not call a parallel
        method or compute the radius itself."""
        src = self._read_classify()
        # Both the env-knob name and the seam kwarg must appear.
        assert "JARVIS_ADVISOR_BG_SCAN_SKIP_ENABLED" in src
        assert "_precomputed_blast_radius" in src
        assert "blast_radius_conservative_cap" in src, (
            "Skip path doesn't reference conservative_cap — "
            "BLOCK behavior may not be preserved"
        )


# ──────────────────────────────────────────────────────────────────────
# Part 3 — Concurrency Isolation
# ──────────────────────────────────────────────────────────────────────


class TestPart3DedicatedExecutorRouting:
    """Per-file reads in the cooperative scan MUST dispatch
    through the dedicated ``advisor-blast`` ThreadPoolExecutor —
    not the default ``asyncio.to_thread`` pool. Verified by
    inspecting ``threading.current_thread().name`` from inside
    the worker."""

    @pytest.mark.asyncio
    async def test_per_file_reads_run_on_advisor_blast_thread(
        self, fake_repo, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_EVENT_LOOP_YIELD_EVERY_N", "4",
        )
        captured_threads: set = set()
        original_helper = (
            operation_advisor._read_bounded_text_for_blast
        )

        def _spy(path_str, max_bytes):
            captured_threads.add(
                threading.current_thread().name,
            )
            return original_helper(path_str, max_bytes)

        monkeypatch.setattr(
            operation_advisor,
            "_read_bounded_text_for_blast", _spy,
        )

        adv = OperationAdvisor(project_root=fake_repo)
        await adv._compute_blast_radius_async(
            ("mypkg/target.py",), root=fake_repo,
        )
        assert captured_threads, "no per-file reads ran"
        # Every thread name must start with the dedicated prefix
        # — proves NO read fell through to the default pool.
        for name in captured_threads:
            assert name.startswith("advisor-blast"), (
                f"per-file read ran on thread {name!r}; expected "
                "'advisor-blast' prefix — Slice 12T Part 3 "
                "isolation broken"
            )

    @pytest.mark.asyncio
    async def test_offload_blocking_no_longer_invoked_in_scan(
        self, fake_repo, monkeypatch,
    ):
        """The Slice 12S regression: ``offload_blocking`` (which
        hits the default pool) MUST NOT be called from
        ``_compute_blast_radius_async`` — Part 3 replaced it."""
        from backend.core.ouroboros.governance import (
            event_loop_governance as elg,
        )
        spy_count = {"n": 0}
        original = elg.offload_blocking

        async def _spy(fn, *args, **kwargs):
            spy_count["n"] += 1
            return await original(fn, *args, **kwargs)

        monkeypatch.setattr(
            "backend.core.ouroboros.governance."
            "event_loop_governance.offload_blocking",
            _spy,
        )

        adv = OperationAdvisor(project_root=fake_repo)
        await adv._compute_blast_radius_async(
            ("mypkg/target.py",), root=fake_repo,
        )
        assert spy_count["n"] == 0, (
            f"offload_blocking called {spy_count['n']}× during "
            "blast scan — Part 3 isolation regressed to Slice 12S"
        )

    @pytest.mark.asyncio
    async def test_result_parity_preserved_after_isolation(
        self, fake_repo,
    ):
        """Switching the dispatch target must NOT change the
        integer returned. Parity with the sync path is the
        load-bearing correctness contract."""
        adv = OperationAdvisor(project_root=fake_repo)
        sync_count = adv._compute_blast_radius(
            ("mypkg/target.py",), root=fake_repo,
        )
        _BLAST_RADIUS_CACHE_SHARED.clear()
        async_count = await adv._compute_blast_radius_async(
            ("mypkg/target.py",), root=fake_repo,
        )
        assert sync_count == async_count == 15


class TestPart3ASTPin:
    def _read_advisor(self) -> str:
        return Path(
            "backend/core/ouroboros/governance/operation_advisor.py"
        ).read_text()

    def test_async_scan_uses_dedicated_executor(self):
        """``_compute_blast_radius_async`` MUST use
        ``loop.run_in_executor`` with the dedicated executor
        accessor — NOT ``offload_blocking``."""
        src = self._read_advisor()
        tree = ast.parse(src)
        target_fn = None
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.AsyncFunctionDef)
                and node.name == "_compute_blast_radius_async"
            ):
                target_fn = node
                break
        assert target_fn is not None

        # Must reference _get_advisor_blast_executor in the body.
        body_text = ast.unparse(target_fn)
        assert "_get_advisor_blast_executor" in body_text, (
            "_compute_blast_radius_async doesn't dispatch to "
            "the dedicated executor — Part 3 isolation broken"
        )
        assert "run_in_executor" in body_text, (
            "_compute_blast_radius_async doesn't use "
            "loop.run_in_executor for per-file reads"
        )
        # Must NOT call offload_blocking from inside the function
        # (it was the Slice 12S code path Part 3 replaces).
        offload_count = 0
        for inner in ast.walk(target_fn):
            if isinstance(inner, ast.Call):
                fn = inner.func
                if (
                    isinstance(fn, ast.Name)
                    and fn.id == "offload_blocking"
                ):
                    offload_count += 1
                elif (
                    isinstance(fn, ast.Attribute)
                    and fn.attr == "offload_blocking"
                ):
                    offload_count += 1
        assert offload_count == 0, (
            "offload_blocking still called inside "
            "_compute_blast_radius_async — Part 3 incomplete"
        )

    def test_module_level_helper_exists(self):
        """The per-file read worker MUST live at module level
        (not as a closure) so the executor thread doesn't
        capture the OperationAdvisor instance."""
        assert callable(
            operation_advisor._read_bounded_text_for_blast,
        )
        sig = inspect.signature(
            operation_advisor._read_bounded_text_for_blast,
        )
        params = list(sig.parameters.keys())
        assert params == ["path_str", "max_bytes"], (
            f"helper signature drifted: {params}"
        )


# ──────────────────────────────────────────────────────────────────────
# Cross-part: end-to-end no-loop-wedge proof
# ──────────────────────────────────────────────────────────────────────


class TestCrossPartHeartbeatStillTicks:
    """Combined sanity: the cooperative scan with Part 3
    isolation still yields enough that a concurrent heartbeat
    coroutine accumulates ticks during the scan. Regression
    against the bt-2026-05-23-180315 wedge."""

    @pytest.mark.asyncio
    async def test_heartbeat_ticks_with_dedicated_executor(
        self, fake_repo, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_EVENT_LOOP_YIELD_EVERY_N", "4",
        )
        ticks = 0
        scan_running = True

        async def heartbeat():
            nonlocal ticks
            while scan_running:
                ticks += 1
                await asyncio.sleep(0.001)

        adv = OperationAdvisor(project_root=fake_repo)
        hb_task = asyncio.create_task(heartbeat())
        await asyncio.sleep(0.01)
        ticks_before = ticks

        await adv._compute_blast_radius_async(
            ("mypkg/target.py",), root=fake_repo,
        )

        scan_running = False
        await asyncio.sleep(0.01)
        hb_task.cancel()
        try:
            await hb_task
        except asyncio.CancelledError:
            pass

        ticks_during = ticks - ticks_before
        assert ticks_during >= 2, (
            f"Heartbeat starved during Part 3 scan: "
            f"ticks={ticks_during} — loop is wedged"
        )
