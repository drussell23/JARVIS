"""Slice 252 — Shadow-Telemetry: pipe shadow_guard traps into the StreamEventBroker.

Decoupled (no kernel import) so it runs in-sandbox. Proves: the
SHADOW_ACTION_TRAPPED event type is registered; emit_shadow_trap +
shadow_guard publish a structured event to the live broker non-blockingly; and
the dispatcher's ContextVar attributes the trap to its triggering signal end to
end (signal -> organ -> shadow_guard -> broker), without grepping text logs.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance import ide_observability_stream as ios
from backend.core import cybernetic_reanimation as cr
from backend.core.cybernetic_reanimation import (
    PressureSignal, PressureSignalType as PT, SignalEdge,
    EventActivationDispatcher, shadow_guard, shadow_guard_async,
    emit_shadow_trap, SHADOW_TRAPPED,
)


@pytest.fixture(autouse=True)
def _stream_on(monkeypatch):
    monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "true")
    monkeypatch.delenv("JARVIS_RESILIENCE_SHADOW_MODE", raising=False)  # default TRUE
    ios.reset_default_broker()
    yield
    ios.reset_default_broker()


def _trapped_events():
    broker = ios.get_default_broker()
    return [e for e in broker.recent_history(limit=100)
            if e.event_type == ios.EVENT_TYPE_SHADOW_ACTION_TRAPPED]


class TestEventTypeRegistered:
    def test_shadow_action_trapped_is_valid_event(self):
        assert ios.EVENT_TYPE_SHADOW_ACTION_TRAPPED == "shadow_action_trapped"
        assert ios.EVENT_TYPE_SHADOW_ACTION_TRAPPED in ios._VALID_EVENT_TYPES


class TestPublishWrapper:
    def test_publish_registers_structured_payload(self):
        eid = ios.publish_shadow_action_trapped(
            organ_name="SelfHealingOrchestrator",
            intended_action="kill pid 1234",
            triggering_signal="anomaly_detected:proc:rising",
        )
        assert eid is not None
        evs = _trapped_events()
        assert len(evs) == 1
        p = evs[0].payload
        assert p["organ_name"] == "SelfHealingOrchestrator"
        assert p["intended_action"] == "kill pid 1234"
        assert p["triggering_signal"] == "anomaly_detected:proc:rising"

    def test_disabled_stream_returns_none(self, monkeypatch):
        monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "0")
        assert ios.publish_shadow_action_trapped(organ_name="x", intended_action="y") is None

    def test_never_raises(self):
        ios.publish_shadow_action_trapped()  # all defaults — must not raise


class TestEmitShadowTrap:
    def test_emit_publishes_to_broker(self):
        emit_shadow_trap("LoadSheddingController", "shed (reject) request: reject:overload")
        evs = _trapped_events()
        assert len(evs) == 1
        assert evs[0].payload["organ_name"] == "LoadSheddingController"

    def test_emit_reads_signal_from_contextvar(self):
        tok = cr._current_signal_var.set(
            PressureSignal(PT.RESOURCE_PRESSURE, "cpu", SignalEdge.RISING)
        )
        try:
            emit_shadow_trap("AutoScalingController", "scale down")
        finally:
            cr._current_signal_var.reset(tok)
        assert _trapped_events()[0].payload["triggering_signal"] == "resource_pressure:cpu:rising"

    def test_emit_never_raises_without_broker(self, monkeypatch):
        monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "0")
        emit_shadow_trap("X", "y")  # no broker publish — must not raise


class TestShadowGuardEmits:
    def test_trap_publishes_telemetry_and_skips_execute(self):
        ran = []
        result = shadow_guard("terminate proc-9", lambda: ran.append("KILLED"),
                              organ="SelfHealingOrchestrator")
        assert result is SHADOW_TRAPPED and ran == []
        evs = _trapped_events()
        assert len(evs) == 1
        assert evs[0].payload["organ_name"] == "SelfHealingOrchestrator"
        assert evs[0].payload["intended_action"] == "terminate proc-9"

    def test_shadow_off_does_not_emit_and_executes(self, monkeypatch):
        monkeypatch.setenv("JARVIS_RESILIENCE_SHADOW_MODE", "0")
        ran = []
        result = shadow_guard("terminate proc-9", lambda: ran.append("KILLED") or "done",
                              organ="SelfHealingOrchestrator")
        assert result == "done" and ran == ["KILLED"]
        assert _trapped_events() == []

    async def test_async_guard_emits(self):
        async def act(): return "x"
        await shadow_guard_async("restart svc", act, organ="GracefulDegradationManager")
        assert _trapped_events()[0].payload["organ_name"] == "GracefulDegradationManager"


class TestPhase3DispatchChain:
    async def test_signal_to_organ_to_guard_to_broker_attributed(self):
        """Synthesize a signal -> dispatcher sets the ContextVar -> organ wakes ->
        its shadow_guard traps -> the broker registers a SHADOW_ACTION_TRAPPED
        whose triggering_signal is attributed to the dispatched signal. No kernel,
        non-blocking."""
        d = EventActivationDispatcher()

        async def reanimated_organ(sig):
            # the organ reasons, then routes its kill through the guard
            return shadow_guard("kill the leaking worker", lambda: ("EXECUTED",),
                                organ="SelfHealingOrchestrator")

        d.register_organ("SelfHealingOrchestrator", reanimated_organ, [PT.ANOMALY_DETECTED])
        n = await d.dispatch(PressureSignal(PT.ANOMALY_DETECTED, "worker-7", SignalEdge.RISING))
        assert n == 1
        evs = _trapped_events()
        assert len(evs) == 1
        p = evs[0].payload
        assert p["organ_name"] == "SelfHealingOrchestrator"
        assert p["intended_action"] == "kill the leaking worker"
        # the trap was attributed to the signal that woke the organ (ContextVar)
        assert p["triggering_signal"] == "anomaly_detected:worker-7:rising"
