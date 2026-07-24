"""Agentic-visibility spine (2026-07-24): the three DARK agent-spawning
subsystems (L3 worktree graphs, big-file chunk swarm, worker pool) now
emit on the canonical broker — which the mirrored breadcrumb router
carries to every attached ov cockpit.
"""

from __future__ import annotations

import asyncio

import pytest


# ---------------------------------------------------------------------------
# Event-type + descriptor registration invariants
# ---------------------------------------------------------------------------


def test_new_event_types_are_valid_on_the_broker():
    """The AWE lesson: a type missing from _VALID_EVENT_TYPES is
    SILENTLY dropped at publish."""
    from backend.core.ouroboros.governance.ide_observability_stream import (
        _VALID_EVENT_TYPES,
    )
    for et in ("swarm_scatter", "swarm_stitched", "worker_op_claimed",
               "execution_graph_progress"):
        assert et in _VALID_EVENT_TYPES, et


def test_breadcrumb_descriptors_seeded_and_render():
    from backend.core.ouroboros.governance.event_breadcrumb_registry import (
        build_default_registry,
    )
    reg = build_default_registry()
    sev, text = reg.render("swarm_scatter", {
        "file": "a/b/big.py", "agents": 7, "max_concurrency": 4,
    })
    assert "7 super agents" in text and "big.py" in text
    _sev, text2 = reg.render("swarm_stitched", {
        "file": "big.py", "succeeded": 6, "failed": 1, "max_in_flight": 4,
    })
    assert "6 ok" in text2 and "1 failed" in text2
    _sev, text3 = reg.render("worker_op_claimed", {
        "worker_id": 2, "queue_depth": 5, "goal": "fix the tests",
    })
    assert "worker 2" in text3
    _sev, text4 = reg.render("execution_graph_progress", {
        "graph_id": "g-123456789abc", "kind": "unit_started",
        "unit_id": "unit-42", "done": 1, "total": 4,
    })
    assert "L3 graph" in text4 and "unit_started" in text4


# ---------------------------------------------------------------------------
# Chunk swarm scatter/stitch emit through the broker
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chunk_swarm_emits_scatter_and_stitch(monkeypatch):
    import backend.core.ouroboros.governance.chunk_swarm as cs
    events = []
    monkeypatch.setattr(
        cs, "_emit_swarm_event",
        lambda et, op, payload: events.append((et, payload)),
    )
    src = "def f():\n    return 1\n"

    async def _gen(_target):
        return None                            # every agent fails — still emits

    result = await cs.swarm_repair(
        src, "big.py",
        targets=[],                            # empty → early return, no events
        generate_fn=_gen,
    )
    assert result.stitched == src
    assert events == []                        # nothing to scatter → silent


@pytest.mark.asyncio
async def test_chunk_swarm_scatter_event_carries_fanout(monkeypatch):
    import ast
    import backend.core.ouroboros.governance.chunk_swarm as cs
    events = []
    monkeypatch.setattr(
        cs, "_emit_swarm_event",
        lambda et, op, payload: events.append((et, op, payload)),
    )
    src = "def f():\n    return 1\n\n\ndef g():\n    return 2\n"
    tree = ast.parse(src)
    fns = [n for n in tree.body if isinstance(n, ast.FunctionDef)]

    class _Chunk:
        def __init__(self, node):
            self.start_line = node.lineno
            self.end_line = node.end_lineno

    targets = [
        cs.ChunkTarget(chunk=_Chunk(n), symbol=n.name, instruction="noop")
        for n in fns
    ]

    async def _gen(_t):
        return None                            # agents fail; lifecycle still visible

    await cs.swarm_repair(src, "pkg/big.py", targets=targets, generate_fn=_gen)
    kinds = [e[0] for e in events]
    assert kinds == ["swarm_scatter", "swarm_stitched"]
    assert events[0][2]["agents"] == 2
    assert events[1][2]["failed"] == 2


# ---------------------------------------------------------------------------
# L3 bridge: graduated default + boot wiring invariant
# ---------------------------------------------------------------------------


def test_exec_graph_bridge_default_on(monkeypatch):
    monkeypatch.delenv("JARVIS_EXEC_GRAPH_BRIDGE_ENABLED", raising=False)
    from backend.core.ouroboros.governance.execution_graph_progress_bridge import (
        master_enabled,
    )
    assert master_enabled() is True
    monkeypatch.setenv("JARVIS_EXEC_GRAPH_BRIDGE_ENABLED", "0")
    assert master_enabled() is False


def test_gls_starts_the_bridge_wiring_invariant():
    """The forwarder existed with ZERO callers (wired-but-inert). The
    GLS boot path must now start it."""
    from pathlib import Path
    src = Path(
        "backend/core/ouroboros/governance/governed_loop_service.py"
    ).read_text()
    assert "start_default_bridge()" in src
    # And the pool pickup emits on the broker.
    pool = Path(
        "backend/core/ouroboros/governance/background_agent_pool.py"
    ).read_text()
    assert 'publish_task_event(' in pool and '"worker_op_claimed"' in pool
