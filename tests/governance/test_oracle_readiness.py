"""Tests for OracleReadiness — granular readiness primitive for the
Oracle's deferred (background) initialization.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from backend.core.ouroboros.oracle_readiness import (
    ORACLE_READINESS_SCHEMA_VERSION,
    OracleInitFailed,
    OracleReadiness,
    OracleReadinessScope,
    OracleReadinessState,
)


_REPO = Path(__file__).resolve().parents[2]


# ===========================================================================
# Schema + closed taxonomy
# ===========================================================================


def test_schema_version_pinned():
    assert ORACLE_READINESS_SCHEMA_VERSION == "oracle_readiness.v1"


def test_scope_taxonomy_closed():
    """Scope is a closed 3-value enum — adding members requires
    explicit code change so consumers stay synchronized."""
    members = {s.name for s in OracleReadinessScope}
    assert members == {"GRAPH", "SEMANTIC", "FULL"}


def test_state_is_frozen():
    s = OracleReadinessState(
        graph_ready=False, semantic_ready=False,
        failed=False, failure_class="",
    )
    with pytest.raises(Exception):
        s.graph_ready = True  # type: ignore[misc]


# ===========================================================================
# Initial state
# ===========================================================================


def test_initial_state_all_false():
    r = OracleReadiness()
    assert r.is_ready(OracleReadinessScope.GRAPH) is False
    assert r.is_ready(OracleReadinessScope.SEMANTIC) is False
    assert r.is_ready(OracleReadinessScope.FULL) is False
    assert r.is_failed() is False
    state = r.state()
    assert state.fully_ready is False
    assert state.failure_class == ""


# ===========================================================================
# Mark transitions
# ===========================================================================


def test_mark_graph_ready_only():
    r = OracleReadiness()
    r.mark_graph_ready()
    assert r.is_ready(OracleReadinessScope.GRAPH) is True
    assert r.is_ready(OracleReadinessScope.SEMANTIC) is False
    assert r.is_ready(OracleReadinessScope.FULL) is False


def test_mark_semantic_ready_only():
    r = OracleReadiness()
    r.mark_semantic_ready()
    assert r.is_ready(OracleReadinessScope.GRAPH) is False
    assert r.is_ready(OracleReadinessScope.SEMANTIC) is True
    assert r.is_ready(OracleReadinessScope.FULL) is False


def test_mark_both_implies_full_ready():
    r = OracleReadiness()
    r.mark_graph_ready()
    r.mark_semantic_ready()
    assert r.is_ready(OracleReadinessScope.FULL) is True
    assert r.state().fully_ready is True


def test_mark_idempotent():
    """Repeated marks don't reset or duplicate state."""
    r = OracleReadiness()
    r.mark_graph_ready()
    r.mark_graph_ready()
    r.mark_graph_ready()
    assert r.is_ready(OracleReadinessScope.GRAPH) is True


def test_failure_short_circuits_ready():
    """After mark_failed, is_ready returns False even if scope
    events were previously set — the FAILURE is authoritative."""
    r = OracleReadiness()
    r.mark_graph_ready()
    assert r.is_ready(OracleReadinessScope.GRAPH) is True
    r.mark_failed(RuntimeError("simulated"))
    assert r.is_ready(OracleReadinessScope.GRAPH) is False


def test_failure_first_one_wins():
    r = OracleReadiness()
    r.mark_failed(RuntimeError("first"))
    r.mark_failed(ValueError("second"))
    assert isinstance(r.failure(), RuntimeError)


# ===========================================================================
# Async wait scenarios
# ===========================================================================


def test_wait_unblocks_on_graph_ready():
    async def scenario():
        r = OracleReadiness()
        async def trigger():
            await asyncio.sleep(0.01)
            r.mark_graph_ready()
        asyncio.ensure_future(trigger())
        await r.wait_until_ready(OracleReadinessScope.GRAPH, timeout=2.0)
        assert r.is_ready(OracleReadinessScope.GRAPH) is True

    asyncio.get_event_loop().run_until_complete(scenario())


def test_wait_full_unblocks_on_both_ready():
    async def scenario():
        r = OracleReadiness()

        async def trigger():
            await asyncio.sleep(0.01)
            r.mark_graph_ready()
            await asyncio.sleep(0.01)
            r.mark_semantic_ready()

        asyncio.ensure_future(trigger())
        await r.wait_until_ready(OracleReadinessScope.FULL, timeout=2.0)
        assert r.is_ready(OracleReadinessScope.FULL) is True

    asyncio.get_event_loop().run_until_complete(scenario())


def test_wait_fast_path_already_ready():
    """If the scope was set BEFORE the wait, return immediately."""
    async def scenario():
        r = OracleReadiness()
        r.mark_graph_ready()
        # No trigger task — the wait should return without blocking
        await asyncio.wait_for(
            r.wait_until_ready(OracleReadinessScope.GRAPH),
            timeout=0.1,  # tight — proves no actual wait
        )

    asyncio.get_event_loop().run_until_complete(scenario())


def test_wait_raises_on_failure():
    async def scenario():
        r = OracleReadiness()

        async def fail_it():
            await asyncio.sleep(0.01)
            r.mark_failed(RuntimeError("simulated init failure"))

        asyncio.ensure_future(fail_it())
        with pytest.raises(OracleInitFailed) as ei:
            await r.wait_until_ready(OracleReadinessScope.SEMANTIC, timeout=2.0)
        assert ei.value.scope is OracleReadinessScope.SEMANTIC
        assert isinstance(ei.value.cause, RuntimeError)

    asyncio.get_event_loop().run_until_complete(scenario())


def test_wait_respects_timeout():
    async def scenario():
        r = OracleReadiness()
        # No trigger — wait should hit timeout
        with pytest.raises(asyncio.TimeoutError):
            await r.wait_until_ready(
                OracleReadinessScope.GRAPH, timeout=0.05,
            )

    asyncio.get_event_loop().run_until_complete(scenario())


# ===========================================================================
# State replay — events created lazily, prior marks must be visible
# ===========================================================================


def test_marks_before_event_creation_replayed():
    """Marks made BEFORE an asyncio.Event was lazily created must
    be reflected on the event when it's eventually created."""
    async def scenario():
        r = OracleReadiness()
        # Set ready BEFORE any wait/event-construction
        r.mark_graph_ready()
        # Now wait — events get created here, must reflect prior state
        await asyncio.wait_for(
            r.wait_until_ready(OracleReadinessScope.GRAPH),
            timeout=0.1,
        )

    asyncio.get_event_loop().run_until_complete(scenario())


def test_failure_before_wait_replayed():
    """Failure recorded BEFORE wait must surface as OracleInitFailed."""
    async def scenario():
        r = OracleReadiness()
        r.mark_failed(RuntimeError("pre-wait failure"))
        with pytest.raises(OracleInitFailed):
            await r.wait_until_ready(
                OracleReadinessScope.GRAPH, timeout=0.5,
            )

    asyncio.get_event_loop().run_until_complete(scenario())


# ===========================================================================
# Reset for tests
# ===========================================================================


def test_reset_for_tests_clears_state():
    r = OracleReadiness()
    r.mark_graph_ready()
    r.mark_semantic_ready()
    r.reset_for_tests()
    assert r.is_ready(OracleReadinessScope.GRAPH) is False
    assert r.is_ready(OracleReadinessScope.SEMANTIC) is False
    assert r.is_failed() is False


# ===========================================================================
# Source-level regression — Oracle wired correctly
# ===========================================================================


_ORACLE = _REPO / "backend/core/ouroboros/oracle.py"


def test_oracle_constructs_readiness_in_init():
    """TheOracle.__init__ MUST construct the readiness primitive so
    the object is queryable before initialize() runs."""
    src = _ORACLE.read_text()
    assert "OracleReadiness" in src
    assert "self._readiness = OracleReadiness()" in src


def test_oracle_initialize_signals_graph_ready():
    """Phase 1/2 (cache_load OR full_index) must signal graph_ready."""
    src = _ORACLE.read_text()
    assert "self._readiness.mark_graph_ready()" in src


def test_oracle_initialize_signals_semantic_ready():
    """Phase 3 (semantic_index_init) must signal semantic_ready."""
    src = _ORACLE.read_text()
    assert "self._readiness.mark_semantic_ready()" in src


def test_oracle_initialize_records_failure_on_exception():
    """Failure path MUST call mark_failed(exc) so waiters surface
    OracleInitFailed instead of hanging forever."""
    src = _ORACLE.read_text()
    assert "self._readiness.mark_failed(exc)" in src


def test_oracle_preserves_libmalloc_ordering_comments():
    """The macOS ARM64 libmalloc-safety constraint MUST stay
    documented inline so the deferral refactor doesn't accidentally
    parallelize graph load with ChromaDB init."""
    src = _ORACLE.read_text()
    # Existing safety constraint comments preserved (one of the
    # multiple in-line comments documenting the ordering)
    assert "libmalloc" in src
    assert "AFTER graph loading" in src
    assert "macOS ARM64" in src


def test_oracle_initialize_signals_graph_before_semantic():
    """Source-level ordering: mark_graph_ready MUST appear textually
    before mark_semantic_ready in initialize()."""
    src = _ORACLE.read_text()
    g_idx = src.find("self._readiness.mark_graph_ready()")
    s_idx = src.find("self._readiness.mark_semantic_ready()")
    assert g_idx > 0 and s_idx > 0
    assert g_idx < s_idx, (
        "graph readiness MUST be signaled before semantic readiness "
        "to preserve libmalloc-safe ordering"
    )
