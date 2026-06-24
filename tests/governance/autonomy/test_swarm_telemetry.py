"""Tests for the swarm.* telemetry mesh (Phase 1d).

Each publish_swarm_* helper:
  * rides the shared StreamEventBroker with a registered valid event type;
  * stamps schema_version;
  * is fail-soft -- a raising broker NEVER propagates into the caller;
  * (message_sent) carries NO payload CONTENT, only the EDGE.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance import ide_observability_stream as ios


@pytest.fixture(autouse=True)
def _stream_on(monkeypatch):
    """Ensure the stream master gate is ON and a fresh broker per test."""
    monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "true")
    ios.reset_default_broker()
    yield
    ios.reset_default_broker()


def _drain(broker):
    """Return the published event payloads from the broker history."""
    # The broker keeps a bounded history; read it via the documented snapshot.
    return list(getattr(broker, "_history", []))


# ---------------------------------------------------------------------------
# Event types are registered as valid (else broker.publish silently no-ops)
# ---------------------------------------------------------------------------


def test_swarm_event_types_registered():
    for ev in (
        ios.EVENT_TYPE_SWARM_NODE_SPAWNED,
        ios.EVENT_TYPE_SWARM_MESSAGE_SENT,
        ios.EVENT_TYPE_SWARM_NODE_VAPORIZED,
        ios.EVENT_TYPE_SWARM_DEADLOCK,
        ios.EVENT_TYPE_SWARM_SENTINEL_BLOCK,
    ):
        assert ev in ios._VALID_EVENT_TYPES


def test_swarm_event_type_values():
    assert ios.EVENT_TYPE_SWARM_NODE_SPAWNED == "swarm_node_spawned"
    assert ios.EVENT_TYPE_SWARM_MESSAGE_SENT == "swarm_message_sent"
    assert ios.EVENT_TYPE_SWARM_NODE_VAPORIZED == "swarm_node_vaporized"
    assert ios.EVENT_TYPE_SWARM_DEADLOCK == "swarm_deadlock"
    assert ios.EVENT_TYPE_SWARM_SENTINEL_BLOCK == "swarm_sentinel_block"


# ---------------------------------------------------------------------------
# Each helper publishes with schema_version + the documented payload
# ---------------------------------------------------------------------------


def test_publish_node_spawned_payload():
    ios.publish_swarm_node_spawned("g1", "w1", "python-analyzer", 3, True)
    events = _drain(ios.get_default_broker())
    spawn = [e for e in events if e.event_type == ios.EVENT_TYPE_SWARM_NODE_SPAWNED]
    assert len(spawn) == 1
    p = spawn[0].payload
    assert p["graph_id"] == "g1"
    assert p["worker_id"] == "w1"
    assert p["role"] == "python-analyzer"
    assert p["allowed_tools_count"] == 3
    assert p["read_only"] is True
    assert p["schema_version"] == ios.STREAM_SCHEMA_VERSION


def test_publish_message_sent_is_edge_only_no_content():
    ios.publish_swarm_message_sent("g1", "w1", "w2", "artifact_handoff", "mid-1")
    events = _drain(ios.get_default_broker())
    edge = [e for e in events if e.event_type == ios.EVENT_TYPE_SWARM_MESSAGE_SENT]
    assert len(edge) == 1
    p = edge[0].payload
    assert p == {
        "graph_id": "g1",
        "from_worker": "w1",
        "to_worker": "w2",
        "kind": "artifact_handoff",
        "msg_id": "mid-1",
        "schema_version": ios.STREAM_SCHEMA_VERSION,
    }
    # CRITICAL: no payload / content / text key ever rides the edge.
    for forbidden in ("payload", "content", "text", "untrusted_peer_data", "body"):
        assert forbidden not in p


def test_publish_node_vaporized_payload():
    ios.publish_swarm_node_vaporized("g1", "w1", 17)
    events = _drain(ios.get_default_broker())
    vap = [e for e in events if e.event_type == ios.EVENT_TYPE_SWARM_NODE_VAPORIZED]
    assert len(vap) == 1
    p = vap[0].payload
    assert p["graph_id"] == "g1"
    assert p["worker_id"] == "w1"
    assert p["turns_cleared"] == 17
    assert p["schema_version"] == ios.STREAM_SCHEMA_VERSION


def test_publish_deadlock_payload():
    ios.publish_swarm_deadlock("g1", ["w1", "w2"], "semantic_stagnation")
    events = _drain(ios.get_default_broker())
    dl = [e for e in events if e.event_type == ios.EVENT_TYPE_SWARM_DEADLOCK]
    assert len(dl) == 1
    p = dl[0].payload
    assert p["graph_id"] == "g1"
    assert p["pair"] == ["w1", "w2"]
    assert p["trigger"] == "semantic_stagnation"
    assert p["schema_version"] == ios.STREAM_SCHEMA_VERSION


def test_publish_sentinel_block_payload():
    ios.publish_swarm_sentinel_block("op-1", "worker_imperative_injection:drop")
    events = _drain(ios.get_default_broker())
    sb = [e for e in events if e.event_type == ios.EVENT_TYPE_SWARM_SENTINEL_BLOCK]
    assert len(sb) == 1
    p = sb[0].payload
    assert p["op_id"] == "op-1"
    assert p["reason"] == "worker_imperative_injection:drop"
    assert p["schema_version"] == ios.STREAM_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Fail-soft: a raising broker NEVER propagates into the caller
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        lambda: ios.publish_swarm_node_spawned("g", "w", "r", 1, True),
        lambda: ios.publish_swarm_message_sent("g", "a", "b", "k", "m"),
        lambda: ios.publish_swarm_node_vaporized("g", "w", 1),
        lambda: ios.publish_swarm_deadlock("g", ["a", "b"], "t"),
        lambda: ios.publish_swarm_sentinel_block("op", "r"),
    ],
)
def test_publish_helpers_fail_soft_on_raising_broker(monkeypatch, call):
    class _Boom:
        def publish(self, *a, **k):
            raise RuntimeError("broker exploded")

    monkeypatch.setattr(ios, "get_default_broker", lambda: _Boom())
    # Must NOT raise.
    call()


# ---------------------------------------------------------------------------
# Producer-side wiring: the emit points fire (and fail-soft never breaks them)
# ---------------------------------------------------------------------------


def test_bus_send_emits_message_edge_no_content(monkeypatch):
    """A successful AgentMessageBus delivery emits the message EDGE."""
    monkeypatch.setenv("JARVIS_SWARM_MESSAGE_BUS_ENABLED", "true")
    from backend.core.ouroboros.governance.autonomy.agent_message_bus import (
        AgentMessageBus,
        MessageKind,
    )

    bus = AgentMessageBus(graph_id="gX")
    bus.register_worker("w1")
    bus.register_worker("w2")
    sender = bus.issue_sender("w1")
    assert sender.send("w2", MessageKind.FINDING, {"secret_text": "do not leak"}) is True

    edge = [
        e for e in _drain(ios.get_default_broker())
        if e.event_type == ios.EVENT_TYPE_SWARM_MESSAGE_SENT
    ]
    assert len(edge) == 1
    p = edge[0].payload
    assert p["graph_id"] == "gX"
    assert p["from_worker"] == "w1"
    assert p["to_worker"] == "w2"
    assert p["kind"] == "finding"
    # The message free-text NEVER appears on the edge.
    assert "secret_text" not in str(p)
    assert "do not leak" not in str(p)


def test_vaporize_quietly_emits_vaporized_edge():
    """vaporize_quietly(graph_id=...) emits swarm_node_vaporized with turns."""
    from backend.core.ouroboros.governance.autonomy.ephemeral_memory_sandbox import (
        EphemeralMemorySandbox,
        vaporize_quietly,
    )

    sb = EphemeralMemorySandbox(worker_id="w1", sub_goal_prompt="do the thing")
    sb.append({"role": "tool", "content": "result"})
    vaporize_quietly(sb, graph_id="gV")

    vap = [
        e for e in _drain(ios.get_default_broker())
        if e.event_type == ios.EVENT_TYPE_SWARM_NODE_VAPORIZED
    ]
    assert len(vap) == 1
    assert vap[0].payload["graph_id"] == "gV"
    assert vap[0].payload["worker_id"] == "w1"
    assert vap[0].payload["turns_cleared"] >= 1


def test_vaporize_quietly_no_graph_id_no_edge():
    """No graph_id -> no telemetry edge (legacy/non-swarm path unchanged)."""
    from backend.core.ouroboros.governance.autonomy.ephemeral_memory_sandbox import (
        EphemeralMemorySandbox,
        vaporize_quietly,
    )

    sb = EphemeralMemorySandbox(worker_id="w1", sub_goal_prompt="x")
    vaporize_quietly(sb)
    vap = [
        e for e in _drain(ios.get_default_broker())
        if e.event_type == ios.EVENT_TYPE_SWARM_NODE_VAPORIZED
    ]
    assert vap == []


def test_deadlock_shatter_emits_deadlock_edge():
    """The EpistemicDeadlockBreaker emits swarm_deadlock on shatter."""
    from backend.core.ouroboros.governance.autonomy.deadlock_breaker import (
        DeadlockInterruptedException,
        EpistemicDeadlockBreaker,
    )

    breaker = EpistemicDeadlockBreaker(
        correlation_id="corr-1", worker_a="w1", worker_b="w2",
        op_id="op-1", graph_id="gD",
    )
    # Drive past the dumb max-turn backstop with distinct (non-stagnant) turns.
    with pytest.raises(DeadlockInterruptedException):
        for i in range(10):
            breaker.observe_turn(f"turn number {i} with unique content {i*i}")

    dl = [
        e for e in _drain(ios.get_default_broker())
        if e.event_type == ios.EVENT_TYPE_SWARM_DEADLOCK
    ]
    assert len(dl) == 1
    assert dl[0].payload["graph_id"] == "gD"
    assert dl[0].payload["pair"] == ["w1", "w2"]
    assert dl[0].payload["trigger"]
