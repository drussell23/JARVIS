"""Tests for agent_message_bus — legit send/subscribe/request, per-graph teardown.

Normal-path TDD suite. The adversarial red-team lives in
test_agent_message_bus_redteam.py.
"""
from __future__ import annotations

import os

import pytest

from backend.core.ouroboros.governance.autonomy.agent_message_bus import (
    AgentMessage,
    AgentMessageBus,
    MessageKind,
    bus_enabled,
)


def _make_bus(graph_id="g1", op_id="op1"):
    return AgentMessageBus(graph_id=graph_id, op_id=op_id)


def test_default_off():
    # Master gate default false -> no bus created by callers.
    assert bus_enabled() is False


def test_register_and_membership():
    bus = _make_bus()
    bus.register_worker("w1")
    bus.register_worker("w2")
    assert bus.is_member("w1")
    assert bus.is_member("w2")
    assert not bus.is_member("ghost")
    assert bus.members() == ("w1", "w2")


def test_legit_send_and_subscribe():
    bus = _make_bus()
    bus.register_worker("w1")
    bus.register_worker("w2")
    msg = bus.make_signed(
        from_worker="w1",
        to_worker="w2",
        kind=MessageKind.FINDING,
        payload={"note": "found a bug in module X"},
    )
    assert bus.send(msg) is True
    inbox = bus.subscribe("w2")
    assert len(inbox) == 1
    delivered = inbox[0]
    assert delivered.from_worker == "w1"
    # Delivered payload is structurally fenced as untrusted peer data.
    assert delivered.payload["untrusted_peer_data"]["note"] == "found a bug in module X"
    assert bus.delivered == 1


def test_artifact_handoff_roundtrip():
    bus = _make_bus()
    bus.register_worker("producer")
    bus.register_worker("consumer")
    msg = bus.make_signed(
        from_worker="producer",
        to_worker="consumer",
        kind=MessageKind.ARTIFACT_HANDOFF,
        payload={"diff_hash": "abc123", "status": "completed"},
    )
    assert bus.send(msg) is True
    inbox = bus.subscribe("consumer")
    assert inbox[0].kind is MessageKind.ARTIFACT_HANDOFF
    assert inbox[0].payload["untrusted_peer_data"]["diff_hash"] == "abc123"


def test_request_response_roundtrip():
    bus = _make_bus()
    bus.register_worker("asker")
    bus.register_worker("answerer")
    # Ask: request stores the correlation and sends a CLARIFICATION_REQUEST.
    resp = bus.request(
        from_worker="asker",
        to_worker="answerer",
        payload={"question": "what schema version?"},
        correlation_id="corr-1",
    )
    assert resp is None  # no answer yet
    # The answerer's inbox got the request.
    answerer_inbox = bus.subscribe("answerer")
    assert len(answerer_inbox) == 1
    assert answerer_inbox[0].kind is MessageKind.CLARIFICATION_REQUEST
    # Answer comes back bound to the same correlation id.
    reply = bus.make_signed(
        from_worker="answerer",
        to_worker="asker",
        kind=MessageKind.CLARIFICATION_RESPONSE,
        payload={"answer": "v1.1"},
        correlation_id="corr-1",
    )
    assert bus.send(reply) is True
    assert bus.response_for("corr-1") is not None
    assert bus.response_for("corr-1").payload["untrusted_peer_data"]["answer"] == "v1.1"


def test_message_to_unknown_recipient_dropped_sender_never_blocks():
    bus = _make_bus()
    bus.register_worker("w1")
    msg = bus.make_signed(
        from_worker="w1",
        to_worker="nonexistent",
        kind=MessageKind.STATUS,
        payload={"phase": "running"},
    )
    # Dropped (recipient not registered), sender does not block / raise.
    assert bus.send(msg) is False
    assert bus.dropped.get("unknown_recipient", 0) == 1


def test_per_graph_teardown_destroys_bus():
    bus = _make_bus()
    bus.register_worker("w1")
    bus.register_worker("w2")
    msg = bus.make_signed(
        from_worker="w1", to_worker="w2", kind=MessageKind.STATUS, payload={"x": 1}
    )
    assert bus.send(msg) is True
    bus.destroy()
    # After destroy: inert. A new send drops, members cleared, secret zeroed.
    assert bus.send(msg) is False
    assert bus.members() == ()
    snap = bus.metrics_snapshot()
    assert snap["destroyed"] is True


def test_per_graph_distinct_secrets():
    b1 = AgentMessageBus(graph_id="A")
    b2 = AgentMessageBus(graph_id="B")
    # Distinct graph-scoped secrets -> the SAME worker id derives a different
    # per-worker key in each graph, so the same canonical message signs to a
    # different digest. The graph secret never leaves the bus.
    k1 = b1.register_worker("w")
    k2 = b2.register_worker("w")
    assert k1 and k2 and k1 != k2
    m1 = b1.make_signed(from_worker="w", to_worker="w", kind=MessageKind.STATUS, payload={})
    # The signature minted in graph A does NOT verify in graph B.
    assert b2._verify_signature(m1) is False


def test_metrics_snapshot_no_content():
    bus = _make_bus()
    bus.register_worker("w1")
    bus.register_worker("w2")
    msg = bus.make_signed(
        from_worker="w1", to_worker="w2", kind=MessageKind.FINDING,
        payload={"secret_note": "do not leak"},
    )
    bus.send(msg)
    snap = bus.metrics_snapshot()
    # Snapshot is counters only — never message content.
    assert "do not leak" not in str(snap)
    assert snap["delivered"] == 1
    assert snap["members"] == 2


def test_dedup_replay_consumed_id():
    bus = _make_bus()
    bus.register_worker("w1")
    bus.register_worker("w2")
    msg = bus.make_signed(
        from_worker="w1", to_worker="w2", kind=MessageKind.STATUS, payload={"k": "v"},
        msg_id="fixed-id",
    )
    assert bus.send(msg) is True
    # Re-send the exact same msg (same id) -> deduped.
    msg2 = bus.make_signed(
        from_worker="w1", to_worker="w2", kind=MessageKind.STATUS, payload={"k": "v"},
        msg_id="fixed-id",
    )
    assert bus.send(msg2) is False
    assert bus.dropped.get("duplicate", 0) == 1
