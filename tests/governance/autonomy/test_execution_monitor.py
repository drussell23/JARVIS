"""tests/governance/autonomy/test_execution_monitor.py

TDD tests for ExecutionStatus, ExecutionMonitor, and SafetyNet integration
(Task H3: Extract ExecutionStatus signals -> L3 SafetyNet execution monitoring).

Covers:
- ExecutionStatus terminal/resource-violation classification
- ExecutionMonitor: record, ring buffer, classify, constraints, rates, distribution
- SafetyNet integration: OP_COMPLETED handler, resource violation escalation
"""
from __future__ import annotations

import asyncio
import time

import pytest

from backend.core.ouroboros.governance.autonomy.autonomy_types import (
    CommandType,
    EventEnvelope,
    EventType,
)
from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus
from backend.core.ouroboros.governance.autonomy.event_emitter import EventEmitter
from backend.core.ouroboros.governance.autonomy.execution_monitor import (
    ExecutionConstraints,
    ExecutionMonitor,
    ExecutionOutcome,
    ExecutionStatus,
)
from backend.core.ouroboros.governance.autonomy.safety_net import (
    ProductionSafetyNet,
    SafetyNetConfig,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_outcome(
    op_id: str = "op-1",
    status: ExecutionStatus = ExecutionStatus.COMPLETED,
    duration_ms: float = 100.0,
    memory_peak_mb: float = 64.0,
    call_depth: int = 5,
    error_message: str = "",
) -> ExecutionOutcome:
    now = time.monotonic_ns()
    return ExecutionOutcome(
        op_id=op_id,
        status=status,
        start_ns=now - int(duration_ms * 1_000_000),
        end_ns=now,
        duration_ms=duration_ms,
        error_message=error_message,
        memory_peak_mb=memory_peak_mb,
        call_depth=call_depth,
    )


def _op_completed_event(
    success: bool = True,
    error: str = "",
    op_id: str = "op-1",
    duration_ms: float = 100.0,
    memory_peak_mb: float = 64.0,
    call_depth: int = 5,
) -> EventEnvelope:
    return EventEnvelope(
        source_layer="L1",
        event_type=EventType.OP_COMPLETED,
        payload={
            "success": success,
            "error": error,
            "op_id": op_id,
            "duration_ms": duration_ms,
            "memory_peak_mb": memory_peak_mb,
            "call_depth": call_depth,
        },
        op_id=op_id,
    )


# ===========================================================================
# ExecutionStatus tests
# ===========================================================================


class TestExecutionStatus:
    def test_pending_and_running_not_terminal(self):
        pending = _make_outcome(status=ExecutionStatus.PENDING)
        running = _make_outcome(status=ExecutionStatus.RUNNING)
        assert pending.is_terminal is False
        assert running.is_terminal is False

    def test_completed_is_terminal(self):
        outcome = _make_outcome(status=ExecutionStatus.COMPLETED)
        assert outcome.is_terminal is True

    def test_failed_is_terminal(self):
        outcome = _make_outcome(status=ExecutionStatus.FAILED)
        assert outcome.is_terminal is True

    def test_timeout_is_resource_violation(self):
        outcome = _make_outcome(status=ExecutionStatus.TIMEOUT)
        assert outcome.is_resource_violation is True

    def test_failed_not_resource_violation(self):
        outcome = _make_outcome(status=ExecutionStatus.FAILED)
        assert outcome.is_resource_violation is False

    def test_all_resource_violations(self):
        resource_statuses = [
            ExecutionStatus.TIMEOUT,
            ExecutionStatus.MEMORY_EXCEEDED,
            ExecutionStatus.DEPTH_EXCEEDED,
            ExecutionStatus.ITERATION_EXCEEDED,
        ]
        for status in resource_statuses:
            outcome = _make_outcome(status=status)
            assert outcome.is_resource_violation is True, f"{status} should be resource violation"

    def test_security_violation_not_resource_violation(self):
        outcome = _make_outcome(status=ExecutionStatus.SECURITY_VIOLATION)
        assert outcome.is_resource_violation is False

    def test_security_violation_is_terminal(self):
        outcome = _make_outcome(status=ExecutionStatus.SECURITY_VIOLATION)
        assert outcome.is_terminal is True


# ===========================================================================
# ExecutionMonitor tests
# ===========================================================================


class TestExecutionMonitor:
    def test_record_and_get_recent(self):
        monitor = ExecutionMonitor()
        monitor.record(_make_outcome(op_id="a"))
        monitor.record(_make_outcome(op_id="b"))
        monitor.record(_make_outcome(op_id="c"))

        recent = monitor.get_recent_outcomes(limit=2)
        assert len(recent) == 2
        assert recent[0].op_id == "b"
        assert recent[1].op_id == "c"

    def test_ring_buffer_bounded(self):
        monitor = ExecutionMonitor(max_outcomes=100)
        for i in range(150):
            monitor.record(_make_outcome(op_id=f"op-{i}"))

        recent = monitor.get_recent_outcomes(limit=200)
        assert len(recent) <= 100

    def test_classify_success(self):
        monitor = ExecutionMonitor()
        status = monitor.classify_from_payload({"success": True})
        assert status == ExecutionStatus.COMPLETED

    def test_classify_timeout(self):
        monitor = ExecutionMonitor()
        status = monitor.classify_from_payload(
            {"success": False, "error": "operation timeout"}
        )
        assert status == ExecutionStatus.TIMEOUT

    def test_classify_memory_exceeded(self):
        monitor = ExecutionMonitor()
        status = monitor.classify_from_payload(
            {"success": False, "error": "memory limit exceeded"}
        )
        assert status == ExecutionStatus.MEMORY_EXCEEDED

    def test_classify_depth_exceeded(self):
        monitor = ExecutionMonitor()
        status = monitor.classify_from_payload(
            {"success": False, "error": "max recursion depth exceeded"}
        )
        assert status == ExecutionStatus.DEPTH_EXCEEDED

    def test_classify_iteration_exceeded(self):
        monitor = ExecutionMonitor()
        status = monitor.classify_from_payload(
            {"success": False, "error": "iteration loop limit"}
        )
        assert status == ExecutionStatus.ITERATION_EXCEEDED

    def test_classify_security_violation(self):
        monitor = ExecutionMonitor()
        status = monitor.classify_from_payload(
            {"success": False, "error": "security violation detected"}
        )
        assert status == ExecutionStatus.SECURITY_VIOLATION

    def test_classify_permission_as_security(self):
        monitor = ExecutionMonitor()
        status = monitor.classify_from_payload(
            {"success": False, "error": "permission denied for file"}
        )
        assert status == ExecutionStatus.SECURITY_VIOLATION

    def test_classify_generic_failure(self):
        monitor = ExecutionMonitor()
        status = monitor.classify_from_payload(
            {"success": False, "error": "unknown error occurred"}
        )
        assert status == ExecutionStatus.FAILED

    def test_classify_no_error_field_failure(self):
        """Payload with success=False but no error field should still be FAILED."""
        monitor = ExecutionMonitor()
        status = monitor.classify_from_payload({"success": False})
        assert status == ExecutionStatus.FAILED

    def test_get_failure_rate(self):
        monitor = ExecutionMonitor()
        for i in range(5):
            monitor.record(_make_outcome(op_id=f"ok-{i}", status=ExecutionStatus.COMPLETED))
        for i in range(5):
            monitor.record(_make_outcome(op_id=f"fail-{i}", status=ExecutionStatus.FAILED))

        rate = monitor.get_failure_rate(window=20)
        assert abs(rate - 0.5) < 0.01

    def test_get_failure_rate_empty(self):
        monitor = ExecutionMonitor()
        rate = monitor.get_failure_rate(window=20)
        assert rate == 0.0

    def test_get_failure_rate_windowed(self):
        """Only the last N outcomes are considered."""
        monitor = ExecutionMonitor()
        # 10 failures, then 10 successes
        for i in range(10):
            monitor.record(_make_outcome(op_id=f"fail-{i}", status=ExecutionStatus.FAILED))
        for i in range(10):
            monitor.record(_make_outcome(op_id=f"ok-{i}", status=ExecutionStatus.COMPLETED))

        # Window of 10 should only see the 10 successes
        rate = monitor.get_failure_rate(window=10)
        assert rate == 0.0

    def test_get_resource_violation_rate(self):
        monitor = ExecutionMonitor()
        for i in range(8):
            monitor.record(_make_outcome(op_id=f"ok-{i}", status=ExecutionStatus.COMPLETED))
        for i in range(2):
            monitor.record(_make_outcome(op_id=f"to-{i}", status=ExecutionStatus.TIMEOUT))

        rate = monitor.get_resource_violation_rate(window=20)
        assert abs(rate - 0.2) < 0.01

    def test_get_resource_violation_rate_empty(self):
        monitor = ExecutionMonitor()
        rate = monitor.get_resource_violation_rate(window=20)
        assert rate == 0.0

    def test_check_constraints_timeout(self):
        constraints = ExecutionConstraints(max_execution_time_s=10.0)
        monitor = ExecutionMonitor(constraints=constraints)
        outcome = _make_outcome(duration_ms=15_000.0)  # 15 seconds > 10s limit
        violations = monitor.check_constraints(outcome)
        assert "execution_time" in violations

    def test_check_constraints_memory(self):
        constraints = ExecutionConstraints(max_memory_mb=256)
        monitor = ExecutionMonitor(constraints=constraints)
        outcome = _make_outcome(memory_peak_mb=512.0)  # 512 > 256 limit
        violations = monitor.check_constraints(outcome)
        assert "memory" in violations

    def test_check_constraints_depth(self):
        constraints = ExecutionConstraints(max_call_depth=50)
        monitor = ExecutionMonitor(constraints=constraints)
        outcome = _make_outcome(call_depth=100)  # 100 > 50 limit
        violations = monitor.check_constraints(outcome)
        assert "call_depth" in violations

    def test_check_constraints_no_violations(self):
        constraints = ExecutionConstraints()
        monitor = ExecutionMonitor(constraints=constraints)
        outcome = _make_outcome(duration_ms=100.0, memory_peak_mb=64.0, call_depth=5)
        violations = monitor.check_constraints(outcome)
        assert violations == []

    def test_check_constraints_multiple_violations(self):
        constraints = ExecutionConstraints(
            max_execution_time_s=1.0,
            max_memory_mb=32,
            max_call_depth=3,
        )
        monitor = ExecutionMonitor(constraints=constraints)
        outcome = _make_outcome(duration_ms=5000.0, memory_peak_mb=64.0, call_depth=10)
        violations = monitor.check_constraints(outcome)
        assert "execution_time" in violations
        assert "memory" in violations
        assert "call_depth" in violations

    def test_status_distribution(self):
        monitor = ExecutionMonitor()
        monitor.record(_make_outcome(op_id="a", status=ExecutionStatus.COMPLETED))
        monitor.record(_make_outcome(op_id="b", status=ExecutionStatus.COMPLETED))
        monitor.record(_make_outcome(op_id="c", status=ExecutionStatus.FAILED))
        monitor.record(_make_outcome(op_id="d", status=ExecutionStatus.TIMEOUT))

        dist = monitor.get_status_distribution()
        assert dist["COMPLETED"] == 2
        assert dist["FAILED"] == 1
        assert dist["TIMEOUT"] == 1

    def test_to_dict_structure(self):
        monitor = ExecutionMonitor()
        monitor.record(_make_outcome(op_id="a", status=ExecutionStatus.COMPLETED))
        monitor.record(_make_outcome(op_id="b", status=ExecutionStatus.FAILED))

        d = monitor.to_dict()
        assert "failure_rate" in d
        assert "resource_violation_rate" in d
        assert "total_recorded" in d
        assert "status_distribution" in d
        assert d["total_recorded"] == 2

    def test_to_dict_empty(self):
        monitor = ExecutionMonitor()
        d = monitor.to_dict()
        assert d["failure_rate"] == 0.0
        assert d["resource_violation_rate"] == 0.0
        assert d["total_recorded"] == 0

    def test_lifetime_counts_persist_after_ring_buffer_prune(self):
        """Lifetime status counts should reflect ALL recorded outcomes, not just the ring buffer."""
        monitor = ExecutionMonitor(max_outcomes=5)
        for i in range(10):
            monitor.record(_make_outcome(op_id=f"op-{i}", status=ExecutionStatus.COMPLETED))

        dist = monitor.get_status_distribution()
        assert dist["COMPLETED"] == 10  # lifetime, not buffer size


# ===========================================================================
# SafetyNet integration tests
# ===========================================================================


class TestSafetyNetExecutionIntegration:
    @pytest.mark.asyncio
    async def test_op_completed_recorded_in_monitor(self):
        bus = CommandBus(maxsize=100)
        emitter = EventEmitter()
        net = ProductionSafetyNet(command_bus=bus)
        net.register_event_handlers(emitter)

        event = _op_completed_event(success=True, op_id="op-42")
        await emitter.emit(event)

        summary = net.get_execution_summary()
        assert summary["total_recorded"] == 1

    @pytest.mark.asyncio
    async def test_op_completed_failure_recorded(self):
        bus = CommandBus(maxsize=100)
        emitter = EventEmitter()
        net = ProductionSafetyNet(command_bus=bus)
        net.register_event_handlers(emitter)

        event = _op_completed_event(success=False, error="unknown", op_id="op-fail")
        await emitter.emit(event)

        summary = net.get_execution_summary()
        assert summary["total_recorded"] == 1
        assert summary["failure_rate"] == 1.0

    @pytest.mark.asyncio
    async def test_resource_violation_rate_triggers_mode_switch(self):
        """When resource violation rate exceeds threshold, SafetyNet should emit REQUEST_MODE_SWITCH."""
        config = SafetyNetConfig(resource_violation_rate_threshold=0.5)
        bus = CommandBus(maxsize=100)
        emitter = EventEmitter()
        net = ProductionSafetyNet(command_bus=bus, config=config)
        net.register_event_handlers(emitter)

        # Emit many timeout events to push resource violation rate above 0.5
        for i in range(15):
            event = _op_completed_event(
                success=False,
                error="operation timeout",
                op_id=f"timeout-{i}",
            )
            await emitter.emit(event)

        # Should have at least one REQUEST_MODE_SWITCH command
        cmds = []
        while bus._heap:
            _, _, cmd = bus._heap.pop(0)
            cmds.append(cmd)
        mode_switch_cmds = [
            c for c in cmds
            if c.command_type == CommandType.REQUEST_MODE_SWITCH
            and c.payload.get("reason", "").startswith("Resource violation")
        ]
        assert len(mode_switch_cmds) >= 1
        assert mode_switch_cmds[0].payload["target_mode"] == "REDUCED_AUTONOMY"

    @pytest.mark.asyncio
    async def test_no_mode_switch_below_threshold(self):
        """No escalation when resource violation rate is below threshold."""
        config = SafetyNetConfig(resource_violation_rate_threshold=0.5)
        bus = CommandBus(maxsize=100)
        emitter = EventEmitter()
        net = ProductionSafetyNet(command_bus=bus, config=config)
        net.register_event_handlers(emitter)

        # 1 timeout out of 10 = 10% < 50% threshold
        for i in range(9):
            await emitter.emit(_op_completed_event(success=True, op_id=f"ok-{i}"))
        await emitter.emit(_op_completed_event(
            success=False, error="operation timeout", op_id="timeout-0"
        ))

        # Filter for resource-violation-triggered mode switches only
        cmds = []
        while bus._heap:
            _, _, cmd = bus._heap.pop(0)
            cmds.append(cmd)
        resource_mode_cmds = [
            c for c in cmds
            if c.command_type == CommandType.REQUEST_MODE_SWITCH
            and "Resource violation" in c.payload.get("reason", "")
        ]
        assert len(resource_mode_cmds) == 0

    @pytest.mark.asyncio
    async def test_get_execution_summary_returns_dict(self):
        bus = CommandBus(maxsize=100)
        emitter = EventEmitter()
        net = ProductionSafetyNet(command_bus=bus)
        net.register_event_handlers(emitter)

        summary = net.get_execution_summary()
        assert isinstance(summary, dict)
        assert "failure_rate" in summary
