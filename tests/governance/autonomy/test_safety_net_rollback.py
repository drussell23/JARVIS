"""tests/governance/autonomy/test_safety_net_rollback.py

TDD tests for L3 rollback root cause analysis (Task 10: P1.6).

Covers:
- Single rollback emits REPORT_ROLLBACK_CAUSE command
- Pattern detection when same reason repeats >= threshold
- Root cause classification for various failure reasons
- Old rollback entries are pruned outside the window
"""
from __future__ import annotations

import time

import pytest

from backend.core.ouroboros.governance.autonomy.autonomy_types import (
    CommandType,
    EventEnvelope,
    EventType,
)
from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus
from backend.core.ouroboros.governance.autonomy.event_emitter import EventEmitter
from backend.core.ouroboros.governance.autonomy.safety_net import (
    ProductionSafetyNet,
    SafetyNetConfig,
)


def _rollback_event(
    op_id: str,
    brain_id: str = "qwen_coder",
    reason: str = "validation_failed",
) -> EventEnvelope:
    return EventEnvelope(
        source_layer="L1",
        event_type=EventType.OP_ROLLED_BACK,
        payload={
            "op_id": op_id,
            "brain_id": brain_id,
            "rollback_reason": reason,
            "affected_files": ["auth.py"],
            "phase_at_failure": "VALIDATE",
        },
    )


class TestRollbackRootCause:
    def test_single_rollback_analyzed(self):
        bus = CommandBus(maxsize=100)
        emitter = EventEmitter()
        config = SafetyNetConfig(rollback_pattern_threshold=2)
        net = ProductionSafetyNet(command_bus=bus, config=config)
        net.register_event_handlers(emitter)

        net._on_rollback(_rollback_event("op1"))

        assert bus.qsize() >= 1
        cmd = bus._heap[0][2]
        assert cmd.command_type == CommandType.REPORT_ROLLBACK_CAUSE
        assert cmd.payload["op_id"] == "op1"

    def test_pattern_detected_same_reason(self):
        bus = CommandBus(maxsize=100)
        emitter = EventEmitter()
        config = SafetyNetConfig(rollback_pattern_threshold=2)
        net = ProductionSafetyNet(command_bus=bus, config=config)
        net.register_event_handlers(emitter)

        net._on_rollback(_rollback_event("op1", reason="validation_failed"))
        net._on_rollback(_rollback_event("op2", reason="validation_failed"))

        # Second rollback should trigger pattern match + incident
        cmds = []
        while bus._heap:
            _, _, cmd = bus._heap.pop(0)
            cmds.append(cmd)
        cause_cmds = [c for c in cmds if c.command_type == CommandType.REPORT_ROLLBACK_CAUSE]
        assert len(cause_cmds) >= 2
        # At least one should have pattern_match=True
        assert any(c.payload.get("pattern_match") for c in cause_cmds)

    def test_root_cause_classification_validation(self):
        """Validate reason maps to validation_failure category."""
        assert ProductionSafetyNet._classify_root_cause("validation_failed") == "validation_failure"
        assert ProductionSafetyNet._classify_root_cause("VALIDATE error") == "validation_failure"

    def test_root_cause_classification_timeout(self):
        assert ProductionSafetyNet._classify_root_cause("generation_timeout") == "timeout"

    def test_root_cause_classification_syntax(self):
        assert ProductionSafetyNet._classify_root_cause("syntax_error in patch") == "syntax_error"
        assert ProductionSafetyNet._classify_root_cause("could not parse diff") == "syntax_error"

    def test_root_cause_classification_test_failure(self):
        assert ProductionSafetyNet._classify_root_cause("test_failure") == "test_failure"

    def test_root_cause_classification_permission(self):
        assert ProductionSafetyNet._classify_root_cause("permission_denied") == "permission_error"
        assert ProductionSafetyNet._classify_root_cause("access_error") == "permission_error"

    def test_root_cause_classification_unknown(self):
        assert ProductionSafetyNet._classify_root_cause("something_weird") == "unknown"

    def test_command_envelope_metadata(self):
        """Verify source_layer, target_layer, and ttl_s on emitted command."""
        bus = CommandBus(maxsize=100)
        emitter = EventEmitter()
        net = ProductionSafetyNet(command_bus=bus)
        net.register_event_handlers(emitter)

        net._on_rollback(_rollback_event("op1"))

        cmd = bus._heap[0][2]
        assert cmd.source_layer == "L3"
        assert cmd.target_layer == "L2"
        assert cmd.ttl_s == 300.0

    def test_payload_includes_affected_files_and_model(self):
        """Verify the command payload carries affected_files and model_used."""
        bus = CommandBus(maxsize=100)
        emitter = EventEmitter()
        net = ProductionSafetyNet(command_bus=bus)
        net.register_event_handlers(emitter)

        net._on_rollback(_rollback_event("op1", brain_id="deepseek_r1"))

        cmd = bus._heap[0][2]
        assert cmd.payload["affected_files"] == ["auth.py"]
        assert cmd.payload["model_used"] == "deepseek_r1"

    def test_similar_op_ids_populated_on_pattern(self):
        """When a pattern is detected, similar_op_ids should list prior ops."""
        bus = CommandBus(maxsize=100)
        emitter = EventEmitter()
        config = SafetyNetConfig(rollback_pattern_threshold=2)
        net = ProductionSafetyNet(command_bus=bus, config=config)
        net.register_event_handlers(emitter)

        net._on_rollback(_rollback_event("op1", reason="timeout"))
        net._on_rollback(_rollback_event("op2", reason="timeout"))

        # The second command should reference op1 as a similar op
        cmd = bus._heap[1][2]
        assert cmd.payload["pattern_match"] is True
        assert "op1" in cmd.payload["similar_op_ids"]

    def test_different_reasons_no_pattern(self):
        """Different reasons should not trigger pattern detection."""
        bus = CommandBus(maxsize=100)
        emitter = EventEmitter()
        config = SafetyNetConfig(rollback_pattern_threshold=2)
        net = ProductionSafetyNet(command_bus=bus, config=config)
        net.register_event_handlers(emitter)

        net._on_rollback(_rollback_event("op1", reason="timeout"))
        net._on_rollback(_rollback_event("op2", reason="syntax_error"))

        cmds = [bus._heap[i][2] for i in range(len(bus._heap))]
        for cmd in cmds:
            assert cmd.payload.get("pattern_match") is False

    def test_rollback_history_pruning(self):
        """Entries outside the window should be pruned."""
        bus = CommandBus(maxsize=100)
        emitter = EventEmitter()
        # Very small window so old entries get pruned
        config = SafetyNetConfig(
            rollback_pattern_threshold=2,
            rollback_pattern_window_s=0.001,  # 1 ms
        )
        net = ProductionSafetyNet(command_bus=bus, config=config)
        net.register_event_handlers(emitter)

        net._on_rollback(_rollback_event("op1", reason="timeout"))
        # Wait long enough for the entry to expire from the window
        time.sleep(0.01)
        net._on_rollback(_rollback_event("op2", reason="timeout"))

        # op1 should have been pruned, so no pattern match on op2
        cmd = bus._heap[-1][2]
        assert cmd.payload["pattern_match"] is False
        assert cmd.payload["similar_op_ids"] == []
