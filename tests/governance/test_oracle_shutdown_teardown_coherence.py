"""Oracle shutdown teardown-coherence — bounded ``_save_cache``.

Closes bt-2026-05-25-020602: a 1.1GB ``codebase_graph.pkl`` synchronous
serialization in ``Oracle._save_cache`` held the Python GIL + the
process I/O slot in uninterruptible kernel state past the
``BoundedShutdownWatchdog`` 30s window, so ``os._exit(75)`` could not
be scheduled. Two-layer preventive defense:

* Layer 1 — ``_save_cache`` lifts the heavy work to ``asyncio.to_thread``
  so the asyncio event loop AND the watchdog daemon thread can keep
  running while a worker thread does the I/O.
* Layer 2 — ``shutdown()`` wraps ``self._save_cache()`` in
  ``asyncio.wait_for(timeout=JARVIS_ORACLE_SHUTDOWN_DEADLINE_S)`` so
  even if the worker thread stays in kernel I/O past the deadline, the
  asyncio side abandons and returns control to the harness teardown
  chain within the watchdog's budget.

Why preventive defense is the right shape (not just a bigger watchdog
deadline): on uninterruptible kernel I/O macOS does not deliver any
syscall to the waiting thread — including ``os._exit``. The ONLY ways
out are I/O completion or kernel timeout. So the structural fix MUST
prevent shutdown from entering long uninterruptible I/O in the first
place.

The graph cache is rebuildable on next boot from index. An abandoned
save means slower cold start, NOT correctness loss.

Test surface: 3 AST pins + 6 spine tests.
"""

from __future__ import annotations

import ast
import asyncio
import os
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ORACLE_FILE = REPO_ROOT / "backend" / "core" / "ouroboros" / "oracle.py"


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(), filename=str(path))


# ──────────────────────────────────────────────────────────────────────
# AST PINS — 3
# ──────────────────────────────────────────────────────────────────────


def test_ast_pin_shutdown_wraps_save_cache_in_wait_for() -> None:
    """``TheOracle.shutdown`` body must reference ``asyncio.wait_for``
    AND ``_save_cache`` — proves Layer 2 bound is composed."""
    tree = _parse(ORACLE_FILE)
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != "TheOracle":
            continue
        for sub in node.body:
            if not isinstance(sub, ast.AsyncFunctionDef):
                continue
            if sub.name != "shutdown":
                continue
            body_src = ast.unparse(sub)
            assert "asyncio.wait_for" in body_src, (
                "TheOracle.shutdown body does not call asyncio.wait_for "
                "— Layer 2 bound missing. The bt-2026-05-25-020602 wedge "
                "trap is open again."
            )
            assert "_save_cache" in body_src, (
                "TheOracle.shutdown body does not reference _save_cache "
                "— Layer 2 bound not wrapping the right call."
            )
            assert "_oracle_shutdown_deadline_s" in body_src, (
                "TheOracle.shutdown does not consume the env-tunable "
                "deadline helper — hardcoded timeouts forbidden."
            )
            return
    pytest.fail("TheOracle.shutdown not found")


def test_ast_pin_save_cache_uses_to_thread() -> None:
    """``TheOracle._save_cache`` body must reference ``asyncio.to_thread``
    AND ``_write_cache_blocking`` — proves Layer 1 (lift to worker
    thread) is composed."""
    tree = _parse(ORACLE_FILE)
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != "TheOracle":
            continue
        for sub in node.body:
            if not isinstance(sub, ast.AsyncFunctionDef):
                continue
            if sub.name != "_save_cache":
                continue
            body_src = ast.unparse(sub)
            assert "asyncio.to_thread" in body_src, (
                "TheOracle._save_cache body does not dispatch via "
                "asyncio.to_thread — Layer 1 lift missing. The GIL + "
                "I/O slot will starve the BoundedShutdownWatchdog."
            )
            assert "_write_cache_blocking" in body_src, (
                "TheOracle._save_cache does not call "
                "_write_cache_blocking — Layer 1 worker entry-point "
                "is the contract."
            )
            return
    pytest.fail("TheOracle._save_cache not found")


def test_ast_pin_write_cache_blocking_preserves_atomic_durability() -> None:
    """``TheOracle._write_cache_blocking`` body must still use a temp
    file + ``os.replace`` — the Arc B.1 atomic-durability invariant
    survives the to_thread lift. Mirrors original bt-2026-05-18-062703
    'invalid load key' regression protection."""
    tree = _parse(ORACLE_FILE)
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != "TheOracle":
            continue
        for sub in node.body:
            if not isinstance(sub, ast.FunctionDef):
                continue
            if sub.name != "_write_cache_blocking":
                continue
            body_src = ast.unparse(sub)
            assert "mkstemp" in body_src, (
                "_write_cache_blocking must serialize into a temp file "
                "(Arc B.1 atomic durability)."
            )
            assert "os.replace" in body_src, (
                "_write_cache_blocking must promote via os.replace "
                "(POSIX-atomic rename) — direct write_bytes to final "
                "path can leave a torn cache on crash."
            )
            return
    pytest.fail("TheOracle._write_cache_blocking not found")


# ──────────────────────────────────────────────────────────────────────
# Spine — 6
# ──────────────────────────────────────────────────────────────────────


def test_spine_default_deadline_is_five_seconds() -> None:
    """Default deadline is 5s — sized for graphs up to ~50K nodes on
    local SSD with room for jitter."""
    from backend.core.ouroboros.oracle import (
        _oracle_shutdown_deadline_s,
        _ORACLE_SHUTDOWN_DEADLINE_DEFAULT_S,
    )
    # Clear env to default
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("JARVIS_ORACLE_SHUTDOWN_DEADLINE_S", None)
        assert _oracle_shutdown_deadline_s() == 5.0
    assert _ORACLE_SHUTDOWN_DEADLINE_DEFAULT_S == 5.0


def test_spine_env_override_honored() -> None:
    """Operator can tune the deadline via env."""
    from backend.core.ouroboros.oracle import _oracle_shutdown_deadline_s
    with patch.dict(
        os.environ,
        {"JARVIS_ORACLE_SHUTDOWN_DEADLINE_S": "12.5"},
    ):
        assert _oracle_shutdown_deadline_s() == 12.5


def test_spine_env_zero_skips_save() -> None:
    """``=0`` is the explicit-skip configuration. Returns 0.0 from the
    helper; ``shutdown`` reads that and skips ``_save_cache`` entirely."""
    from backend.core.ouroboros.oracle import _oracle_shutdown_deadline_s
    with patch.dict(
        os.environ,
        {"JARVIS_ORACLE_SHUTDOWN_DEADLINE_S": "0"},
    ):
        assert _oracle_shutdown_deadline_s() == 0.0


def test_spine_env_bad_value_falls_back_to_default() -> None:
    """Malformed env value falls back to the 5s default (no crash)."""
    from backend.core.ouroboros.oracle import _oracle_shutdown_deadline_s
    with patch.dict(
        os.environ,
        {"JARVIS_ORACLE_SHUTDOWN_DEADLINE_S": "not-a-number"},
    ):
        assert _oracle_shutdown_deadline_s() == 5.0


def test_spine_shutdown_timeout_abandons_save_gracefully() -> None:
    """If ``_save_cache`` exceeds the deadline, shutdown logs + returns
    cleanly — the harness teardown chain MUST move on within the
    BoundedShutdownWatchdog budget. This is THE regression test for
    bt-2026-05-25-020602."""
    from backend.core.ouroboros import oracle as oracle_mod

    class _FakeOracle:
        """Minimal stand-in — calls the real ``shutdown`` method but
        with a ``_save_cache`` that never returns."""
        _running = True

        async def _save_cache(self) -> None:
            await asyncio.sleep(60.0)  # vastly exceeds 0.05s deadline

        shutdown = oracle_mod.TheOracle.shutdown

    fake = _FakeOracle()
    with patch.dict(
        os.environ,
        {"JARVIS_ORACLE_SHUTDOWN_DEADLINE_S": "0.05"},
    ):
        # Should NOT raise, should NOT hang
        asyncio.run(
            asyncio.wait_for(fake.shutdown(), timeout=2.0),
        )
    # _running must be cleared regardless of save outcome
    assert fake._running is False


def test_spine_shutdown_zero_deadline_skips_save_entirely() -> None:
    """``JARVIS_ORACLE_SHUTDOWN_DEADLINE_S=0`` is the explicit-skip
    configuration — ``_save_cache`` is NOT called. Use this in
    deployments where shutdown speed dominates cache-warm trade-off."""
    from backend.core.ouroboros import oracle as oracle_mod

    save_calls: list[int] = []

    class _FakeOracle:
        _running = True

        async def _save_cache(self) -> None:
            save_calls.append(1)

        shutdown = oracle_mod.TheOracle.shutdown

    fake = _FakeOracle()
    with patch.dict(
        os.environ,
        {"JARVIS_ORACLE_SHUTDOWN_DEADLINE_S": "0"},
    ):
        asyncio.run(fake.shutdown())
    assert save_calls == [], (
        "_save_cache was called despite deadline=0 — skip configuration "
        "broken."
    )
    assert fake._running is False
