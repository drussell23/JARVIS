"""TDD suite for ``capture_failure_telemetry`` (Task 5 — Isomorphic Local Sandbox).

Four mandatory assertions (RED -> GREEN):

1. Happy path: given a fake op_ctx with known phase + a comm with a known
   causal chain -> artifact JSON contains FSM phase, causal-parent sequence,
   and memory snapshot.
2. Fail-soft: when comm / op_ctx / gate is None or a source raises (inject
   raising stubs) -> ``capture_failure_telemetry`` STILL returns a Path with
   partial telemetry and NEVER raises.
3. save_summary called: it calls ``session_recorder.save_summary`` with
   ``session_outcome="incomplete_kill"``.
4. Bounded: a huge causal chain is capped at <=50 entries.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, PropertyMock

import pytest

from backend.core.ouroboros.battle_test.failure_telemetry import (
    capture_failure_telemetry,
    _CAUSAL_CHAIN_CAP,
)
from backend.core.ouroboros.governance.comm_protocol import (
    CommMessage,
    CommProtocol,
    LogTransport,
    MessageType,
)
from backend.core.ouroboros.governance.op_context import OperationPhase


# ---------------------------------------------------------------------------
# Test 1 -- Happy path: FSM phase + causal-parent chain + memory snapshot
# ---------------------------------------------------------------------------


def test_happy_path_contains_required_fields(tmp_path: Path) -> None:
    """Artifact JSON must contain FSM phase, causal-parent chain, and memory snapshot."""
    # Fake op_ctx with a known phase
    op_ctx = MagicMock()
    op_ctx.phase = OperationPhase.GENERATE

    # CommProtocol with a 2-message chain
    transport = LogTransport()
    msg1 = CommMessage(
        msg_type=MessageType.INTENT,
        op_id="op-happy-1",
        seq=1,
        causal_parent_seq=None,
        payload={},
    )
    msg2 = CommMessage(
        msg_type=MessageType.PLAN,
        op_id="op-happy-1",
        seq=2,
        causal_parent_seq=1,
        payload={},
    )
    transport.messages.extend([msg1, msg2])
    comm = CommProtocol(transports=[transport])

    artifact_dir = capture_failure_telemetry(
        op_ctx=op_ctx,
        output_dir=tmp_path,
        reason="test_failure",
        comm=comm,
    )

    assert artifact_dir.exists(), "artifact directory must be created"
    telemetry_file = artifact_dir / "failure_telemetry.json"
    assert telemetry_file.exists(), "failure_telemetry.json must be written"

    data = json.loads(telemetry_file.read_text(encoding="utf-8"))

    # FSM phase
    assert data["fsm_phase"] == "GENERATE", (
        f"Expected fsm_phase='GENERATE', got {data.get('fsm_phase')!r}"
    )

    # Memory snapshot present with expected keys from MemoryPressureGate.snapshot()
    assert data.get("memory_snapshot") is not None, (
        "memory_snapshot must not be None on happy path"
    )
    mem = data["memory_snapshot"]
    assert "level" in mem or "enabled" in mem, (
        f"memory_snapshot missing expected keys; got: {list(mem.keys())}"
    )

    # Causal chain
    chain = data.get("causal_chain")
    assert isinstance(chain, list), f"causal_chain must be a list, got {type(chain)}"
    assert len(chain) == 2, f"Expected 2 messages, got {len(chain)}"
    # First message (INTENT) has no causal parent
    assert chain[0]["causal_parent_seq"] is None
    # Second message (PLAN) links back to seq=1
    assert chain[1]["causal_parent_seq"] == 1, (
        f"Expected causal_parent_seq=1, got {chain[1]['causal_parent_seq']}"
    )


# ---------------------------------------------------------------------------
# Test 2 -- Fail-soft: never raises with None / raising sources
# ---------------------------------------------------------------------------


def test_fail_soft_with_all_none(tmp_path: Path) -> None:
    """Returns a Path without raising when all optional sources are None."""
    result = capture_failure_telemetry(
        op_ctx=None,
        output_dir=tmp_path,
        reason="null_test",
        comm=None,
        session_recorder=None,
    )
    assert isinstance(result, Path)
    # Partial telemetry JSON should still be written
    telemetry_file = result / "failure_telemetry.json"
    assert telemetry_file.exists()
    data = json.loads(telemetry_file.read_text(encoding="utf-8"))
    assert data["reason"] == "null_test"
    assert data["fsm_phase"] is None
    assert data["causal_chain"] is None


def test_fail_soft_with_raising_op_ctx(tmp_path: Path) -> None:
    """Never raises when op_ctx.phase access raises a RuntimeError."""
    op_ctx = MagicMock()
    type(op_ctx).phase = PropertyMock(side_effect=RuntimeError("phase boom"))

    result = capture_failure_telemetry(
        op_ctx=op_ctx,
        output_dir=tmp_path,
        reason="raising_ctx",
    )
    assert isinstance(result, Path)
    telemetry_file = result / "failure_telemetry.json"
    assert telemetry_file.exists()
    data = json.loads(telemetry_file.read_text(encoding="utf-8"))
    # Phase capture failed gracefully -- fsm_phase must be None
    assert "fsm_phase" in data
    assert data["fsm_phase"] is None


def test_fail_soft_with_raising_comm_transport(tmp_path: Path) -> None:
    """Never raises when iterating comm._transports raises."""
    comm = MagicMock()
    # Make the _transports attribute raise on access
    type(comm)._transports = PropertyMock(side_effect=RuntimeError("transport boom"))

    result = capture_failure_telemetry(
        output_dir=tmp_path,
        reason="raising_transport",
        comm=comm,
    )
    assert isinstance(result, Path)
    telemetry_file = result / "failure_telemetry.json"
    data = json.loads(telemetry_file.read_text(encoding="utf-8"))
    # causal_chain capture failed gracefully
    assert data["causal_chain"] is None


def test_fail_soft_with_raising_gate(tmp_path: Path, monkeypatch: Any) -> None:
    """Never raises when get_default_gate() raises."""
    import backend.core.ouroboros.governance.memory_pressure_gate as _mpg

    def _raise_gate() -> None:
        raise RuntimeError("gate boom")

    monkeypatch.setattr(_mpg, "get_default_gate", _raise_gate)

    result = capture_failure_telemetry(
        output_dir=tmp_path,
        reason="raising_gate",
    )
    assert isinstance(result, Path)
    telemetry_file = result / "failure_telemetry.json"
    data = json.loads(telemetry_file.read_text(encoding="utf-8"))
    assert data["memory_snapshot"] is None


# ---------------------------------------------------------------------------
# Test 3 -- save_summary called with session_outcome="incomplete_kill"
# ---------------------------------------------------------------------------


def test_save_summary_called_with_incomplete_kill(tmp_path: Path) -> None:
    """save_summary is invoked exactly once with session_outcome='incomplete_kill'."""
    recorder = MagicMock()
    recorder.save_summary.return_value = tmp_path / "summary.json"

    capture_failure_telemetry(
        output_dir=tmp_path,
        reason="crash",
        session_recorder=recorder,
    )

    recorder.save_summary.assert_called_once()
    call_kwargs = recorder.save_summary.call_args.kwargs
    assert call_kwargs.get("session_outcome") == "incomplete_kill", (
        f"Expected session_outcome='incomplete_kill', got kwargs={call_kwargs}"
    )
    # Confirm stop_reason is also threaded through
    assert call_kwargs.get("stop_reason") == "crash"


def test_save_summary_not_called_when_recorder_is_none(tmp_path: Path) -> None:
    """save_summary must not be called when session_recorder=None."""
    result = capture_failure_telemetry(
        output_dir=tmp_path,
        reason="no_recorder",
        session_recorder=None,
    )
    assert isinstance(result, Path)


# ---------------------------------------------------------------------------
# Test 4 -- Bounded: huge causal chain is capped at <=_CAUSAL_CHAIN_CAP
# ---------------------------------------------------------------------------


def test_bounded_causal_chain(tmp_path: Path) -> None:
    """100-message chain is capped at _CAUSAL_CHAIN_CAP in the artifact JSON."""
    transport = LogTransport()
    for i in range(100):
        transport.messages.append(
            CommMessage(
                msg_type=MessageType.HEARTBEAT,
                op_id="op-bounded",
                seq=i + 1,
                causal_parent_seq=i if i > 0 else None,
                payload={},
            )
        )
    comm = CommProtocol(transports=[transport])

    artifact_dir = capture_failure_telemetry(
        output_dir=tmp_path,
        reason="bounded_test",
        comm=comm,
    )

    telemetry_file = artifact_dir / "failure_telemetry.json"
    assert telemetry_file.exists()
    data = json.loads(telemetry_file.read_text(encoding="utf-8"))
    chain = data["causal_chain"]
    assert isinstance(chain, list)
    assert len(chain) <= _CAUSAL_CHAIN_CAP, (
        f"Expected chain len <= {_CAUSAL_CHAIN_CAP}, got {len(chain)}"
    )
    # Cap takes the MOST-RECENT entries (tail), so last entry seq=100
    assert chain[-1]["seq"] == 100, (
        f"Expected last seq=100 (most-recent tail), got {chain[-1]['seq']}"
    )
