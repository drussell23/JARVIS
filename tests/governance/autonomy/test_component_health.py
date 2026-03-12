"""tests/governance/autonomy/test_component_health.py

TDD tests for ComponentHealthTracker and SafetyNet integration (Task H2).

Covers:
- ComponentStatus is_healthy / needs_attention properties
- ComponentHealthTracker registration, update, query, aggregation, history
- SafetyNet integration: health probes update the tracker
"""
from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.governance.autonomy.component_health import (
    ComponentHealthTracker,
    ComponentState,
    ComponentStatus,
    StateTransition,
    TransitionReason,
)
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


# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------

def _probe_event(
    success: bool,
    component: str = "system",
    health_score: float | None = None,
) -> EventEnvelope:
    payload: dict = {
        "provider": "gcp-jprime",
        "success": success,
        "latency_ms": 50.0,
        "component": component,
    }
    if health_score is not None:
        payload["health_score"] = health_score
    return EventEnvelope(
        source_layer="L1",
        event_type=EventType.HEALTH_PROBE_RESULT,
        payload=payload,
    )


# -----------------------------------------------------------------------
# ComponentStatus tests
# -----------------------------------------------------------------------

class TestComponentStatus:
    def test_is_healthy_when_ready_and_high_score(self):
        status = ComponentStatus(name="test", state=ComponentState.READY, health_score=0.8)
        assert status.is_healthy is True

    def test_not_healthy_when_error_state(self):
        status = ComponentStatus(name="test", state=ComponentState.ERROR, health_score=1.0)
        assert status.is_healthy is False

    def test_not_healthy_when_low_score(self):
        status = ComponentStatus(name="test", state=ComponentState.READY, health_score=0.5)
        assert status.is_healthy is False

    def test_needs_attention_error_state(self):
        status = ComponentStatus(name="test", state=ComponentState.ERROR, health_score=1.0)
        assert status.needs_attention is True

    def test_needs_attention_low_score(self):
        status = ComponentStatus(name="test", state=ComponentState.READY, health_score=0.3)
        assert status.needs_attention is True

    def test_needs_attention_high_error_count(self):
        status = ComponentStatus(
            name="test", state=ComponentState.READY, health_score=0.9, error_count=6,
        )
        assert status.needs_attention is True

    def test_no_attention_healthy(self):
        status = ComponentStatus(
            name="test", state=ComponentState.READY, health_score=1.0, error_count=0,
        )
        assert status.needs_attention is False


# -----------------------------------------------------------------------
# ComponentHealthTracker tests
# -----------------------------------------------------------------------

class TestComponentHealthTracker:
    def test_register_and_get_status(self):
        tracker = ComponentHealthTracker()
        tracker.register("redis")
        status = tracker.get_status("redis")
        assert status is not None
        assert status.name == "redis"
        assert status.state == ComponentState.NOT_INITIALIZED

    def test_get_status_unregistered_returns_none(self):
        tracker = ComponentHealthTracker()
        assert tracker.get_status("unknown") is None

    def test_update_records_transition(self):
        tracker = ComponentHealthTracker()
        tracker.register("redis")
        tracker.update("redis", ComponentState.ACTIVE, reason=TransitionReason.AUTOMATIC)
        history = tracker.get_history("redis")
        assert len(history) == 1
        assert history[0].from_state == ComponentState.NOT_INITIALIZED
        assert history[0].to_state == ComponentState.ACTIVE

    def test_get_unhealthy(self):
        tracker = ComponentHealthTracker()
        tracker.register("a")
        tracker.register("b")
        tracker.update("a", ComponentState.ACTIVE, health_score=1.0)
        tracker.update("b", ComponentState.ERROR, health_score=0.2)
        unhealthy = tracker.get_unhealthy()
        assert len(unhealthy) == 1
        assert unhealthy[0].name == "b"

    def test_get_needs_attention(self):
        tracker = ComponentHealthTracker()
        tracker.register("c")
        tracker.update("c", ComponentState.READY, health_score=0.3)
        attention = tracker.get_needs_attention()
        assert len(attention) == 1
        assert attention[0].name == "c"

    def test_get_aggregate_health(self):
        tracker = ComponentHealthTracker()
        tracker.register("x")
        tracker.register("y")
        tracker.update("x", ComponentState.ACTIVE, health_score=1.0)
        tracker.update("y", ComponentState.ACTIVE, health_score=0.5)
        assert tracker.get_aggregate_health() == pytest.approx(0.75)

    def test_aggregate_health_empty(self):
        tracker = ComponentHealthTracker()
        assert tracker.get_aggregate_health() == 0.0

    def test_history_bounded(self):
        tracker = ComponentHealthTracker(max_history=200)
        tracker.register("z")
        for i in range(250):
            state = ComponentState.ACTIVE if i % 2 == 0 else ComponentState.BUSY
            tracker.update("z", state)
        all_history = tracker.get_history()
        assert len(all_history) <= 200

    def test_history_filtered_by_name(self):
        tracker = ComponentHealthTracker()
        tracker.register("alpha")
        tracker.register("beta")
        tracker.update("alpha", ComponentState.ACTIVE)
        tracker.update("beta", ComponentState.ACTIVE)
        tracker.update("alpha", ComponentState.BUSY)
        alpha_history = tracker.get_history("alpha")
        beta_history = tracker.get_history("beta")
        assert len(alpha_history) == 2
        assert len(beta_history) == 1

    def test_to_dict_structure(self):
        tracker = ComponentHealthTracker()
        tracker.register("svc")
        tracker.update("svc", ComponentState.ACTIVE, health_score=0.9)
        d = tracker.to_dict()
        assert "components" in d
        assert "svc" in d["components"]
        comp = d["components"]["svc"]
        assert "state" in comp
        assert "health_score" in comp
        assert "error_count" in comp
        assert "is_healthy" in comp
        assert "needs_attention" in comp


# -----------------------------------------------------------------------
# SafetyNet integration tests
# -----------------------------------------------------------------------

class TestSafetyNetHealthTrackerIntegration:
    @pytest.fixture
    def bus(self):
        return CommandBus(maxsize=100)

    @pytest.fixture
    def emitter(self):
        return EventEmitter()

    def test_health_probe_success_updates_tracker(self, bus, emitter):
        net = ProductionSafetyNet(command_bus=bus)
        net.register_event_handlers(emitter)

        net._on_health_probe(_probe_event(success=True, component="jprime"))

        status = net._health_tracker.get_status("jprime")
        assert status is not None
        assert status.state == ComponentState.ACTIVE
        assert status.health_score == 1.0

    def test_health_probe_failure_updates_tracker(self, bus, emitter):
        net = ProductionSafetyNet(command_bus=bus)
        net.register_event_handlers(emitter)

        # Start with a success to establish the component
        net._on_health_probe(_probe_event(success=True, component="jprime"))
        # Then fail
        net._on_health_probe(_probe_event(success=False, component="jprime"))

        status = net._health_tracker.get_status("jprime")
        assert status is not None
        assert status.state == ComponentState.ERROR
        assert status.health_score == pytest.approx(0.9)  # 1.0 - 0.1

    def test_health_probe_failure_decrements_health_floor(self, bus, emitter):
        """Repeated failures should not drop below 0.0."""
        net = ProductionSafetyNet(command_bus=bus)
        net.register_event_handlers(emitter)

        net._on_health_probe(_probe_event(success=True, component="svc"))
        for _ in range(15):
            net._on_health_probe(_probe_event(success=False, component="svc"))

        status = net._health_tracker.get_status("svc")
        assert status is not None
        assert status.health_score >= 0.0

    def test_health_probe_success_with_explicit_score(self, bus, emitter):
        net = ProductionSafetyNet(command_bus=bus)
        net.register_event_handlers(emitter)

        net._on_health_probe(_probe_event(success=True, component="db", health_score=0.85))

        status = net._health_tracker.get_status("db")
        assert status is not None
        assert status.health_score == pytest.approx(0.85)

    def test_get_health_summary_returns_dict(self, bus, emitter):
        net = ProductionSafetyNet(command_bus=bus)
        net.register_event_handlers(emitter)

        net._on_health_probe(_probe_event(success=True, component="svc"))
        summary = net.get_health_summary()
        assert "aggregate_health" in summary
        assert "unhealthy_components" in summary
        assert "components" in summary
