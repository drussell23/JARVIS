"""tests/governance/autonomy/test_safety_net_health.py

TDD tests for L3 health probe escalation (Task 9: P1.5 Health Escalation).

Covers:
- No escalation on single failure (below threshold)
- Escalation to REDUCED_AUTONOMY at configurable threshold
- Severe escalation to READ_ONLY_PLANNING at higher threshold
- Success probe resets consecutive failure count
"""
from __future__ import annotations

import asyncio

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


@pytest.fixture
def bus():
    return CommandBus(maxsize=100)


@pytest.fixture
def emitter():
    return EventEmitter()


def _probe_event(success: bool, consecutive_failures: int = 0) -> EventEnvelope:
    return EventEnvelope(
        source_layer="L1",
        event_type=EventType.HEALTH_PROBE_RESULT,
        payload={
            "provider": "gcp-jprime",
            "success": success,
            "latency_ms": 50.0,
            "consecutive_failures": consecutive_failures,
        },
    )


class TestHealthEscalation:
    def test_no_escalation_on_single_failure(self, bus, emitter):
        config = SafetyNetConfig(probe_failure_escalation_threshold=3)
        net = ProductionSafetyNet(command_bus=bus, config=config)
        net.register_event_handlers(emitter)

        net._on_health_probe(_probe_event(success=False, consecutive_failures=1))
        assert bus.qsize() == 0

    def test_escalation_at_threshold(self, bus, emitter):
        config = SafetyNetConfig(probe_failure_escalation_threshold=3)
        net = ProductionSafetyNet(command_bus=bus, config=config)
        net.register_event_handlers(emitter)

        for i in range(3):
            net._on_health_probe(_probe_event(success=False, consecutive_failures=i + 1))

        assert bus.qsize() >= 1
        cmd = bus._heap[0][2]
        assert cmd.command_type == CommandType.REQUEST_MODE_SWITCH
        assert cmd.payload["target_mode"] == "REDUCED_AUTONOMY"

    def test_severe_escalation_at_5_failures(self, bus, emitter):
        config = SafetyNetConfig(
            probe_failure_escalation_threshold=3,
            probe_failure_severe_threshold=5,
        )
        net = ProductionSafetyNet(command_bus=bus, config=config)
        net.register_event_handlers(emitter)

        for i in range(5):
            net._on_health_probe(_probe_event(success=False, consecutive_failures=i + 1))

        # Should have both REDUCED and READ_ONLY commands
        cmds = []
        while bus.qsize() > 0:
            cmds.append(asyncio.get_event_loop().run_until_complete(bus.get()))
        modes = [c.payload["target_mode"] for c in cmds]
        assert "READ_ONLY_PLANNING" in modes

    def test_success_resets_failure_count(self, bus, emitter):
        config = SafetyNetConfig(probe_failure_escalation_threshold=3)
        net = ProductionSafetyNet(command_bus=bus, config=config)
        net.register_event_handlers(emitter)

        net._on_health_probe(_probe_event(success=False, consecutive_failures=1))
        net._on_health_probe(_probe_event(success=False, consecutive_failures=2))
        net._on_health_probe(_probe_event(success=True, consecutive_failures=0))
        net._on_health_probe(_probe_event(success=False, consecutive_failures=1))
        # Reset after success, so only 1 consecutive failure — no escalation
        assert bus.qsize() == 0
