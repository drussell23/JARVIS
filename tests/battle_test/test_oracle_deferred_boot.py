"""Tests for the harness's deferred Oracle initialization (Manifesto
§2 progressive awakening — REPL no longer blocks on Oracle warm-up).
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parents[2]
_HARNESS = _REPO / "backend/core/ouroboros/battle_test/harness.py"


# ===========================================================================
# Source-level structural pins — boot_oracle deferral wiring
# ===========================================================================


def _get_boot_oracle_node() -> ast.AsyncFunctionDef:
    src = _HARNESS.read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "boot_oracle":
            return node
    pytest.fail("boot_oracle method not found in harness.py")
    raise RuntimeError("unreachable")  # for static analysis


def test_boot_oracle_constructs_synchronously():
    """``self._oracle = TheOracle()`` MUST happen unconditionally
    (sync construction is cheap; no I/O beyond cache-dir mkdir)."""
    body = ast.unparse(_get_boot_oracle_node())
    assert "self._oracle = TheOracle()" in body


def test_boot_oracle_reads_master_env_flag():
    """``JARVIS_ORACLE_BLOCK_BOOT`` is the single env knob — no
    secret modes, no in-code overrides."""
    body = ast.unparse(_get_boot_oracle_node())
    assert "JARVIS_ORACLE_BLOCK_BOOT" in body


def test_boot_oracle_default_path_is_deferred():
    """Default behavior (env unset) MUST go through the deferred
    branch — spawn a background task instead of awaiting."""
    body = ast.unparse(_get_boot_oracle_node())
    # Spawn-and-track pattern
    assert "asyncio.ensure_future" in body
    assert "self._oracle_init_task" in body
    # The deferred coroutine
    assert "_deferred_init" in body


def test_boot_oracle_legacy_path_preserved():
    """Operators with ``JARVIS_ORACLE_BLOCK_BOOT=true`` get the
    legacy synchronous-await behavior verbatim — for deterministic
    CI / perf-baseline harness runs."""
    body = ast.unparse(_get_boot_oracle_node())
    assert "await self._oracle.initialize()" in body


def test_boot_oracle_attaches_done_callback_to_task():
    """The deferred task MUST have a done_callback that swallows
    CancelledError + consumes exceptions so the loop-level handler
    doesn't classify shutdown as a leak."""
    body = ast.unparse(_get_boot_oracle_node())
    assert "add_done_callback" in body


def test_harness_init_declares_oracle_init_task_attribute():
    """The harness ``__init__`` MUST declare ``_oracle_init_task``
    as a typed attribute so static analysis + tests can locate it."""
    src = _HARNESS.read_text()
    assert "self._oracle_init_task: Optional[asyncio.Task] = None" in src


# ===========================================================================
# Shutdown lifecycle — task is settled cleanly
# ===========================================================================


def test_shutdown_settles_init_task_before_oracle_shutdown():
    """``_shutdown_components`` MUST settle the init task BEFORE
    calling ``oracle.shutdown()`` — otherwise a still-running init
    races with shutdown and leaves a half-initialized Chroma client."""
    src = _HARNESS.read_text()
    # The init-task settle block + the oracle.shutdown call both
    # exist; the settle block appears textually before the shutdown
    # within the same Oracle-handling region.
    settle_marker = "self._oracle_init_task is not None"
    shutdown_marker = "await self._oracle.shutdown()"
    settle_idx = src.find(settle_marker)
    # Find the LAST shutdown marker (shutdown_components has it)
    shutdown_idx = src.rfind(shutdown_marker)
    assert settle_idx > 0
    assert shutdown_idx > 0
    assert settle_idx < shutdown_idx


def test_shutdown_settles_with_bounded_timeout():
    """The shutdown wait MUST be bounded so a wedged init can't
    block clean teardown indefinitely."""
    src = _HARNESS.read_text()
    # The wait_for(..., timeout=2.0) wraps the init task during
    # shutdown — verify the bounded pattern + cancel-fallback exist.
    assert "asyncio.wait_for(" in src
    assert "self._oracle_init_task.cancel()" in src


# ===========================================================================
# Boot-timing instrumentation present (preserves observability)
# ===========================================================================


def test_oracle_initialize_phases_recorded():
    """The 3 sub-phases (cache_load / full_index / semantic_index_init)
    MUST be wrapped with _OraclePhase context managers so operators
    can see boot-timing breakdown."""
    src = (_REPO / "backend/core/ouroboros/oracle.py").read_text()
    for phase_name in (
        "oracle_load_cache",
        "oracle_full_index",
        "oracle_semantic_index_init",
    ):
        assert phase_name in src, f"phase {phase_name!r} not instrumented"


# ===========================================================================
# Manifesto §2 (Progressive Awakening) compliance
# ===========================================================================


def test_oracle_object_available_synchronously_after_construction():
    """At Manifesto §2 the Oracle reference must exist immediately
    so downstream wiring (GovernanceStack, BlastRadiusAdapter) can
    attach even before initialize() finishes. ``__init__`` does the
    sync work; readiness primitive lets consumers wait."""
    from backend.core.ouroboros.oracle import TheOracle
    o = TheOracle()
    # Without calling initialize(), the readiness probes return
    # False — there's no silent half-graph claim.
    assert o.is_graph_ready() is False
    assert o.is_semantic_ready() is False
    assert o.is_fully_ready() is False
    # Legacy is_ready() agrees (returns _running, which is False).
    assert o.is_ready() is False
    # Readiness primitive is composed and queryable.
    assert o.readiness is not None


def test_oracle_wait_until_ready_accepts_string_scope():
    """``wait_until_ready`` accepts string scope ("graph"/"semantic"/
    "full") for ergonomics — invalid strings degrade to FULL."""
    import asyncio
    from backend.core.ouroboros.oracle import TheOracle

    async def scenario():
        o = TheOracle()
        # Manually mark ready so the wait returns immediately
        o.readiness.mark_graph_ready()
        await asyncio.wait_for(
            o.wait_until_ready(scope="graph"), timeout=0.1,
        )

    asyncio.get_event_loop().run_until_complete(scenario())
