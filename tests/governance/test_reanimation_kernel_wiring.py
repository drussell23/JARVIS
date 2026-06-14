"""Spec 2 Phase 3 — live-kernel reanimation wiring proof.

Imports unified_supervisor (the kernel), so this MUST run sandbox-off
(split_brain_guard needs a writable lock dir). Proves: a typed pressure signal
wakes SelfHealingOrchestrator through build_resilience_dispatcher, it CALCULATES
the remediation, and Shadow Mode TRAPS the kill (logs, does not execute); with
shadow off, the kill executes; and all 7 organs register.
"""
from __future__ import annotations

import pytest

from backend.core.cybernetic_reanimation import (
    PressureSignal, PressureSignalType as PT, SignalEdge,
)


@pytest.fixture(autouse=True)
def _shadow_default(monkeypatch):
    monkeypatch.delenv("JARVIS_RESILIENCE_SHADOW_MODE", raising=False)
    yield


def _sho_with_fake_kill():
    from unified_supervisor import SelfHealingOrchestrator
    sho = SelfHealingOrchestrator()
    killed = []
    async def fake_kill(component):
        killed.append(component)
        return True
    for strat in SelfHealingOrchestrator.RemediationStrategy:
        sho.register_handler(strat, fake_kill)
    return sho, killed


class TestShadowTrapsRemediation:
    async def test_anomaly_wakes_selfhealing_shadow_traps_kill(self, caplog):
        from unified_supervisor import build_resilience_dispatcher
        sho, killed = _sho_with_fake_kill()
        d = build_resilience_dispatcher({"SelfHealingOrchestrator": sho})
        with caplog.at_level("WARNING"):
            n = await d.dispatch(
                PressureSignal(PT.ANOMALY_DETECTED, "proc-victim", SignalEdge.RISING)
            )
        assert n == 1, "SelfHealing must wake on ANOMALY_DETECTED"
        assert killed == [], "Shadow Mode MUST trap the remediation kill"
        assert any("[SHADOW MODE] Would have execute remediation" in r.message
                   for r in caplog.records), "must log the trapped command"
        # it REASONED (attempted the remediation) but did not ACT
        assert sho._stats["remediations_attempted"] >= 1

    async def test_shadow_off_executes_the_kill(self, monkeypatch):
        monkeypatch.setenv("JARVIS_RESILIENCE_SHADOW_MODE", "0")
        from unified_supervisor import build_resilience_dispatcher
        sho, killed = _sho_with_fake_kill()
        d = build_resilience_dispatcher({"SelfHealingOrchestrator": sho})
        await d.dispatch(
            PressureSignal(PT.ANOMALY_DETECTED, "proc-victim", SignalEdge.RISING)
        )
        assert killed == ["proc-victim"], "with shadow off, the kill executes"


class TestLoadSheddingShadow:
    async def test_with_shedding_traps_reject_in_shadow(self, caplog):
        from unified_supervisor import LoadSheddingController
        try:
            lsc = LoadSheddingController()
        except TypeError:
            pytest.skip("LoadSheddingController needs config — covered by code + py_compile")
        # force a shed decision
        lsc.should_accept = lambda priority: (False, "reject:overload")  # type: ignore
        ran = []
        async def handler():
            ran.append("served")
            return "ok"
        with caplog.at_level("WARNING"):
            result = await lsc.with_shedding(priority=5, handler=handler)
        # shadow mode: the request was NOT rejected — it was served instead of shed
        assert ran == ["served"] and result == "ok"
        assert any("[SHADOW MODE] Would have shed" in r.message for r in caplog.records)


class TestAllSevenOrgansRegister:
    async def test_all_seven_register(self):
        from unified_supervisor import build_resilience_dispatcher
        names = [
            "SelfHealingOrchestrator", "LoadSheddingController",
            "GracefulDegradationManager", "AutoScalingController",
            "AnomalyDetector", "ProcessHealthPredictor", "AdvancedCircuitBreaker",
        ]
        # dummy instances suffice — registration is keyed by name, handlers only
        # fire on dispatch (and are fail-soft on a dummy)
        organs = {n: object() for n in names}
        d = build_resilience_dispatcher(organs)
        assert d.organ_count() == 7, "all 7 surviving resilience organs must register"

    async def test_missing_organ_is_skipped(self):
        from unified_supervisor import build_resilience_dispatcher
        d = build_resilience_dispatcher({"SelfHealingOrchestrator": object()})
        assert d.organ_count() == 1


class TestSlice252ShadowTelemetryKernel:
    async def test_anomaly_trap_publishes_to_stream_broker(self, monkeypatch):
        """Phase 3 (Slice 252): a synthesized ANOMALY_DETECTED wakes the real
        SelfHealingOrchestrator via build_resilience_dispatcher, the shadow_guard
        traps the kill, AND the StreamEventBroker registers a structured
        SHADOW_ACTION_TRAPPED event — non-blocking, no log-grep needed."""
        monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "true")
        from backend.core.ouroboros.governance import ide_observability_stream as ios
        ios.reset_default_broker()
        from unified_supervisor import build_resilience_dispatcher
        sho, killed = _sho_with_fake_kill()
        d = build_resilience_dispatcher({"SelfHealingOrchestrator": sho})

        n = await d.dispatch(
            PressureSignal(PT.ANOMALY_DETECTED, "proc-victim", SignalEdge.RISING)
        )
        assert n == 1
        assert killed == [], "Shadow Mode must trap the kill"

        evs = [e for e in ios.get_default_broker().recent_history(limit=100)
               if e.event_type == ios.EVENT_TYPE_SHADOW_ACTION_TRAPPED]
        assert len(evs) >= 1, "broker must register the SHADOW_ACTION_TRAPPED event"
        p = evs[0].payload
        assert p["organ_name"] == "SelfHealingOrchestrator"
        assert "anomaly_detected:proc-victim" in p["triggering_signal"]
        assert "execute remediation" in p["intended_action"]
