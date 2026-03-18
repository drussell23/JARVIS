"""P2-3: Deterministic replay harness tests.

Verifies that governance event streams can be recorded and replayed
to produce structurally identical outcomes (same op_id chains, same
causal ordering, same message type sequences).

The harness is intentionally lightweight — it records CommMessages
emitted by CommProtocol and verifies replay properties, not the
business logic of individual pipeline stages.
"""
from __future__ import annotations

import asyncio
import pytest
from typing import List

from backend.core.ouroboros.governance.comm_protocol import (
    CommMessage,
    CommProtocol,
    MessageType,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class RecordingTransport:
    """CommProtocol transport that captures all messages in order."""

    def __init__(self) -> None:
        self.messages: List[CommMessage] = []

    async def send(self, msg: CommMessage) -> None:
        self.messages.append(msg)


def _make_protocol() -> tuple[CommProtocol, RecordingTransport]:
    recorder = RecordingTransport()
    protocol = CommProtocol(transports=[recorder])
    return protocol, recorder


async def _emit_full_lifecycle(
    protocol: CommProtocol,
    op_id: str = "op-test-001",
) -> None:
    """Emit a complete 5-phase lifecycle through *protocol*."""
    await protocol.emit_intent(
        op_id=op_id,
        goal="Refactor authentication module",
        target_files=["backend/auth.py"],
        risk_tier="low",
        blast_radius=1,
    )
    await protocol.emit_plan(
        op_id=op_id,
        steps=["Step 1: analysis", "Step 2: patch"],
        rollback_strategy="git-revert",
    )
    await protocol.emit_heartbeat(
        op_id=op_id,
        phase="GENERATE",
        progress_pct=50.0,
    )
    await protocol.emit_decision(
        op_id=op_id,
        outcome="applied",
        reason_code="patch_validated",
    )
    await protocol.emit_postmortem(
        op_id=op_id,
        root_cause="none",
        failed_phase=None,
    )


# ---------------------------------------------------------------------------
# Recording tests
# ---------------------------------------------------------------------------


class TestRecordingTransport:
    @pytest.mark.asyncio
    async def test_captures_all_five_phases(self) -> None:
        protocol, recorder = _make_protocol()
        await _emit_full_lifecycle(protocol)
        types = [m.msg_type for m in recorder.messages]
        assert types == [
            MessageType.INTENT,
            MessageType.PLAN,
            MessageType.HEARTBEAT,
            MessageType.DECISION,
            MessageType.POSTMORTEM,
        ]

    @pytest.mark.asyncio
    async def test_seq_numbers_monotonically_increase(self) -> None:
        protocol, recorder = _make_protocol()
        await _emit_full_lifecycle(protocol)
        seqs = [m.seq for m in recorder.messages]
        assert seqs == list(range(1, len(seqs) + 1))

    @pytest.mark.asyncio
    async def test_causal_chain_correct(self) -> None:
        protocol, recorder = _make_protocol()
        await _emit_full_lifecycle(protocol)
        msgs = recorder.messages
        # INTENT has no causal parent
        assert msgs[0].causal_parent_seq is None
        # Each subsequent message links back to the previous seq
        for i in range(1, len(msgs)):
            assert msgs[i].causal_parent_seq == msgs[i - 1].seq

    @pytest.mark.asyncio
    async def test_all_messages_share_op_id(self) -> None:
        op_id = "op-replay-test-xyz"
        protocol, recorder = _make_protocol()
        await _emit_full_lifecycle(protocol, op_id=op_id)
        assert all(m.op_id == op_id for m in recorder.messages)

    @pytest.mark.asyncio
    async def test_global_seq_strictly_increasing(self) -> None:
        protocol, recorder = _make_protocol()
        await _emit_full_lifecycle(protocol)
        global_seqs = [m.global_seq for m in recorder.messages]
        assert global_seqs == sorted(set(global_seqs)), (
            "global_seq must be strictly increasing across messages"
        )

    @pytest.mark.asyncio
    async def test_correlation_id_defaults_to_op_id(self) -> None:
        op_id = "op-corr-check"
        protocol, recorder = _make_protocol()
        await _emit_full_lifecycle(protocol, op_id=op_id)
        for msg in recorder.messages:
            assert msg.correlation_id == op_id, (
                f"Expected correlation_id={op_id!r}, got {msg.correlation_id!r}"
            )

    @pytest.mark.asyncio
    async def test_set_correlation_id_overrides_op_id(self) -> None:
        op_id = "op-child-001"
        corr_id = "op-saga-root-999"
        protocol, recorder = _make_protocol()
        protocol.set_correlation_id(corr_id)
        await _emit_full_lifecycle(protocol, op_id=op_id)
        for msg in recorder.messages:
            assert msg.correlation_id == corr_id


# ---------------------------------------------------------------------------
# Replay harness tests
# ---------------------------------------------------------------------------


class TestReplayHarness:
    """Verify that a recorded stream replayed through a fresh protocol
    produces structurally identical message shape."""

    @pytest.mark.asyncio
    async def test_replay_produces_same_type_sequence(self) -> None:
        # --- Record ---
        p1, r1 = _make_protocol()
        await _emit_full_lifecycle(p1, op_id="op-orig")
        recorded = r1.messages

        # --- Replay: re-emit the same lifecycle through a fresh protocol ---
        p2, r2 = _make_protocol()
        await _emit_full_lifecycle(p2, op_id="op-replay")
        replayed = r2.messages

        # Structure must match: same types, same seq count, same causal shape
        assert len(replayed) == len(recorded)
        for orig, rep in zip(recorded, replayed):
            assert orig.msg_type == rep.msg_type
            assert orig.seq == rep.seq
            # Causal chain preserved: both are None or both are int
            assert (orig.causal_parent_seq is None) == (rep.causal_parent_seq is None)

    @pytest.mark.asyncio
    async def test_two_operations_have_independent_seq_counters(self) -> None:
        protocol, recorder = _make_protocol()
        await _emit_full_lifecycle(protocol, op_id="op-A")
        await _emit_full_lifecycle(protocol, op_id="op-B")
        # op-A messages
        op_a = [m for m in recorder.messages if m.op_id == "op-A"]
        op_b = [m for m in recorder.messages if m.op_id == "op-B"]
        # Each op has independent per-op seq starting at 1
        assert op_a[0].seq == 1
        assert op_b[0].seq == 1
        # global_seq is NOT independent — it must be strictly ordered across both
        all_global = [m.global_seq for m in recorder.messages]
        assert all_global == sorted(all_global)
        assert len(set(all_global)) == len(all_global), "global_seq must be unique"

    @pytest.mark.asyncio
    async def test_idempotency_key_unique_across_messages(self) -> None:
        protocol, recorder = _make_protocol()
        await _emit_full_lifecycle(protocol, op_id="op-idem-test")
        keys = [m.idempotency_key for m in recorder.messages]
        assert len(keys) == len(set(keys)), "idempotency_keys must be globally unique"
