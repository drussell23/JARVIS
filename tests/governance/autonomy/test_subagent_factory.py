"""Tests for subagent_factory — capability routing + ScopedToolBackend cage."""
from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.governance.autonomy.subagent_factory import (
    SubagentFactory,
    WorkerRoute,
    route_for_shape,
)
from backend.core.ouroboros.governance.autonomy.worker_synthesizer import (
    WorkerShape,
)


def _policy_ctx(call_id: str):
    from pathlib import Path

    from backend.core.ouroboros.governance.tool_executor import PolicyContext

    return PolicyContext(
        repo="JARVIS",
        repo_root=Path("/tmp"),
        op_id="op",
        call_id=call_id,
        round_index=0,
    )


def _read_only_shape() -> WorkerShape:
    return WorkerShape(
        role="python-source analyzer",
        allowed_tools=("read_file", "search_code", "get_callers"),
        mutation_budget=0,
        context_budget_tokens=8000,
        read_only=True,
        rationale="test read-only",
        confidence=0.9,
    )


def _mutating_shape() -> WorkerShape:
    return WorkerShape(
        role="python-source mutator",
        allowed_tools=("read_file", "search_code", "edit_file"),
        mutation_budget=2,
        context_budget_tokens=8000,
        read_only=False,
        rationale="test mutating",
        confidence=0.9,
    )


# ---------------------------------------------------------------------------
# Capability routing (NOT type-name dispatch)
# ---------------------------------------------------------------------------


def test_read_only_shape_routes_to_explore():
    assert route_for_shape(_read_only_shape()) is WorkerRoute.EXPLORE


def test_mutating_shape_routes_to_general():
    assert route_for_shape(_mutating_shape()) is WorkerRoute.GENERAL


def test_factory_build_sets_route_by_capability():
    factory = SubagentFactory()
    ro = factory.build(
        _read_only_shape(), worker_id="w-ro", goal="analyze", scope_paths=["a.py"],
    )
    mut = factory.build(
        _mutating_shape(), worker_id="w-mut", goal="fix", scope_paths=["a.py"],
    )
    assert ro.route is WorkerRoute.EXPLORE
    assert mut.route is WorkerRoute.GENERAL


# ---------------------------------------------------------------------------
# ScopedToolBackend cage built with the synthesized allowlist
# ---------------------------------------------------------------------------


def test_factory_builds_scoped_backend_with_allowlist():
    factory = SubagentFactory()
    built = factory.build(
        _read_only_shape(), worker_id="w1", goal="analyze", scope_paths=["a.py"],
    )
    backend = built.backend
    # The cage carries the mutation budget from the shape.
    assert backend.max_mutations == 0
    # The gate enforces the allowlist (read_file allowed, edit denied).
    allowed, _ = backend._gate.can_use("read_file")
    assert allowed is True
    denied, _ = backend._gate.can_use("edit_file")
    assert denied is False


def test_mutating_backend_has_budget():
    factory = SubagentFactory()
    built = factory.build(
        _mutating_shape(), worker_id="w2", goal="fix", scope_paths=["a.py"],
    )
    assert built.backend.max_mutations == 2


# ---------------------------------------------------------------------------
# Cage enforcement: exceeding the allowlist / budget -> POLICY_DENIED
# ---------------------------------------------------------------------------


def test_worker_exceeding_allowlist_is_policy_denied():
    from backend.core.ouroboros.governance.tool_executor import (
        ToolCall,
        ToolExecStatus,
    )

    factory = SubagentFactory()
    built = factory.build(
        _read_only_shape(), worker_id="w3", goal="analyze", scope_paths=["a.py"],
    )
    call = ToolCall(name="bash", arguments={"command": "ls"})
    ctx = _policy_ctx("c1")
    result = asyncio.run(built.backend.execute_async(call, ctx, deadline=0.0))
    assert result.status is ToolExecStatus.POLICY_DENIED


def test_mutating_worker_exhausts_budget_then_denied():
    from backend.core.ouroboros.governance.tool_executor import (
        ToolCall,
        ToolExecStatus,
    )

    # Budget of 1 -> first edit authorized (null inner backend returns a
    # POLICY_DENIED-shaped no-op, but the slot is consumed), second denied
    # by the COUNT gate.
    shape = WorkerShape(
        role="python-source mutator",
        allowed_tools=("read_file", "edit_file"),
        mutation_budget=1,
        context_budget_tokens=8000,
        read_only=False,
    )
    factory = SubagentFactory()
    built = factory.build(shape, worker_id="w4", goal="fix", scope_paths=["a.py"])

    call = ToolCall(name="edit_file", arguments={"path": "a.py"})
    ctx = _policy_ctx("c1")
    # First authorized at the gate (consumes the slot).
    asyncio.run(built.backend.execute_async(call, ctx, deadline=0.0))
    assert built.backend.mutations_count == 1
    # Second is count-denied at the cage.
    ctx2 = _policy_ctx("c2")
    result2 = asyncio.run(built.backend.execute_async(call, ctx2, deadline=0.0))
    assert result2.status is ToolExecStatus.POLICY_DENIED
    assert "budget" in result2.error.lower()


def test_built_worker_prompt_reflects_shape():
    factory = SubagentFactory()
    built = factory.build(
        _mutating_shape(), worker_id="w5", goal="fix the bug", scope_paths=["a.py"],
    )
    assert "python-source mutator" in built.system_prompt
    assert "fix the bug" in built.system_prompt
    assert "read_only_mode = FALSE" in built.system_prompt


# ---------------------------------------------------------------------------
# Sovereign Wiring (Phase 1d): BoundSender injection on/off (give them voice)
# ---------------------------------------------------------------------------


def _make_bus(graph_id: str = "g1"):
    from backend.core.ouroboros.governance.autonomy.agent_message_bus import (
        AgentMessageBus,
    )

    return AgentMessageBus(graph_id=graph_id)


def test_no_bus_means_no_voice_byte_identical_1c():
    """No bus supplied -> sender/inbox are None (silent worker, as Phase 1c)."""
    factory = SubagentFactory()
    built = factory.build(
        _read_only_shape(), worker_id="w1", goal="analyze", scope_paths=["a.py"],
    )
    assert built.sender is None
    assert built.inbox is None
    assert built.has_voice is False


def test_bus_supplied_but_gate_off_means_no_voice(monkeypatch):
    """Even with a bus, the master gate OFF -> no BoundSender (byte-identical)."""
    monkeypatch.setenv("JARVIS_SWARM_MESSAGE_BUS_ENABLED", "false")
    factory = SubagentFactory()
    bus = _make_bus()
    built = factory.build(
        _read_only_shape(), worker_id="w1", goal="analyze",
        scope_paths=["a.py"], bus=bus, graph_id="g1",
    )
    assert built.sender is None
    assert built.inbox is None
    assert built.has_voice is False


def test_bus_on_grants_identity_locked_boundsender(monkeypatch):
    """Gate ON + bus -> worker gets an identity-locked BoundSender + inbox.

    The worker NEVER gets the bus object or the graph secret -- only the
    BoundSender locked to its own id (no from_worker override possible)."""
    monkeypatch.setenv("JARVIS_SWARM_MESSAGE_BUS_ENABLED", "true")
    from backend.core.ouroboros.governance.autonomy.agent_message_bus import (
        BoundSender,
    )

    factory = SubagentFactory()
    bus = _make_bus()
    built = factory.build(
        _read_only_shape(), worker_id="w1", goal="analyze",
        scope_paths=["a.py"], bus=bus, graph_id="g1",
    )
    assert built.has_voice is True
    assert isinstance(built.sender, BoundSender)
    # The sender is locked to THIS worker's id -- the worker never holds the
    # bus object or the graph secret.
    assert built.sender._worker_id == "w1"
    assert built.sender is not bus
    # The BoundSender.send signature has NO from_worker parameter (structural
    # identity lock).
    import inspect

    params = inspect.signature(built.sender.send).parameters
    assert "from_worker" not in params
    # C1: the worker's inbox is a SentinelInbox (the mandatory filter), NEVER
    # the raw bus.subscribe() deque.
    from backend.core.ouroboros.governance.autonomy.agent_message_bus import (
        SentinelInbox,
    )

    assert isinstance(built.inbox, SentinelInbox)
    import collections

    assert not isinstance(built.inbox, collections.deque)
    # The never-obey framing clause was injected into the worker prompt.
    from backend.core.ouroboros.governance.autonomy.swarm_sentinel import (
        PEER_DATA_FRAMING,
    )

    assert PEER_DATA_FRAMING in built.system_prompt


def test_boundsender_actually_delivers_to_a_peer(monkeypatch):
    """The granted BoundSender can deliver an artifact-handoff to a peer."""
    monkeypatch.setenv("JARVIS_SWARM_MESSAGE_BUS_ENABLED", "true")
    from backend.core.ouroboros.governance.autonomy.agent_message_bus import (
        MessageKind,
    )

    factory = SubagentFactory()
    bus = _make_bus()
    bus.register_worker("w2")  # the peer recipient
    built = factory.build(
        _read_only_shape(), worker_id="w1", goal="analyze",
        scope_paths=["a.py"], bus=bus, graph_id="g1",
    )
    ok = built.sender.send("w2", MessageKind.ARTIFACT_HANDOFF, {"artifact": "x"})
    assert ok is True
    assert len(bus.subscribe("w2")) == 1
