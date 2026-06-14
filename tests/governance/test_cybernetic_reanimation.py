"""Spec 2 — Cybernetic Reanimation foundation: edge-triggered signals, async
dispatch to reanimated organs, and the Shadow Mode fail-safe.

Decoupled (duck-typed bus/organs), so it runs in-sandbox without importing the
102K-line kernel. Proves Phase 4: signals are edge-triggered, organs wake on
dispatch, and Shadow Mode traps the dangerous execution commands.
"""
from __future__ import annotations

import pytest

from backend.core import cybernetic_reanimation as cr
from backend.core.cybernetic_reanimation import (
    PressureSignalType as PT, SignalEdge, PressureSignal,
    PressureSignalEmitter, EventActivationDispatcher,
    shadow_guard, resilience_shadow_mode_enabled, SHADOW_TRAPPED,
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.delenv("JARVIS_RESILIENCE_SHADOW_MODE", raising=False)
    yield


class TestShadowMode:
    def test_default_true_failsafe(self):
        assert resilience_shadow_mode_enabled() is True

    def test_kill_switch_off(self, monkeypatch):
        monkeypatch.setenv("JARVIS_RESILIENCE_SHADOW_MODE", "0")
        assert resilience_shadow_mode_enabled() is False

    def test_shadow_guard_traps_dangerous_action(self, caplog):
        executed = []
        with caplog.at_level("WARNING"):
            result = shadow_guard("terminate process 1234",
                                  lambda: executed.append("KILLED"))
        assert result is SHADOW_TRAPPED
        assert executed == [], "the kill MUST NOT execute in shadow mode"
        assert any("[SHADOW MODE] Would have terminate process 1234" in r.message
                   for r in caplog.records)

    def test_shadow_off_executes(self, monkeypatch):
        monkeypatch.setenv("JARVIS_RESILIENCE_SHADOW_MODE", "0")
        executed = []
        result = shadow_guard("shed 10% load", lambda: executed.append("SHED") or "done")
        assert result == "done"
        assert executed == ["SHED"]


class TestEdgeTriggeredEmitter:
    def test_rising_then_sustained_then_falling(self):
        emitted = []
        em = PressureSignalEmitter(emitted.append)
        # rising edge -> one signal
        s1 = em.observe(PT.RESOURCE_PRESSURE, "cpu", active=True, severity="critical")
        # sustained (still active) -> NO emit
        s2 = em.observe(PT.RESOURCE_PRESSURE, "cpu", active=True)
        s3 = em.observe(PT.RESOURCE_PRESSURE, "cpu", active=True)
        # falling edge -> one signal
        s4 = em.observe(PT.RESOURCE_PRESSURE, "cpu", active=False)
        assert s1 is not None and s1.edge is SignalEdge.RISING and s1.severity == "critical"
        assert s2 is None and s3 is None, "sustained pressure must NOT re-emit (edge-triggered)"
        assert s4 is not None and s4.edge is SignalEdge.FALLING
        assert [s.edge for s in emitted] == [SignalEdge.RISING, SignalEdge.FALLING]

    def test_per_source_isolation(self):
        em = PressureSignalEmitter(lambda s: None)
        assert em.observe(PT.ANOMALY_DETECTED, "diskA", active=True) is not None
        # different source, independent edge state
        assert em.observe(PT.ANOMALY_DETECTED, "diskB", active=True) is not None

    def test_broken_emit_fn_never_raises(self):
        def boom(_s): raise RuntimeError("bus down")
        em = PressureSignalEmitter(boom)
        # still returns the signal + updates state, swallows the emit error
        assert em.observe(PT.COMPONENT_DEGRADED, "x", active=True) is not None


class TestDispatcher:
    async def test_routes_only_to_subscribed_organs(self):
        d = EventActivationDispatcher()
        seen = {"heal": [], "shed": []}
        async def heal(sig): seen["heal"].append(sig.type)
        async def shed(sig): seen["shed"].append(sig.type)
        d.register_organ("SelfHealingOrchestrator", heal, [PT.ANOMALY_DETECTED, PT.COMPONENT_DEGRADED])
        d.register_organ("LoadSheddingController", shed, [PT.RESOURCE_PRESSURE])
        assert d.organ_count() == 2
        n = await d.dispatch(PressureSignal(PT.RESOURCE_PRESSURE, "cpu", SignalEdge.RISING))
        assert n == 1
        assert seen["shed"] == [PT.RESOURCE_PRESSURE] and seen["heal"] == []

    async def test_failsoft_one_broken_organ(self):
        d = EventActivationDispatcher()
        ok = []
        async def broken(sig): raise RuntimeError("organ crashed")
        async def good(sig): ok.append(sig.type)
        d.register_organ("broken", broken, [PT.ANOMALY_DETECTED])
        d.register_organ("good", good, [PT.ANOMALY_DETECTED])
        n = await d.dispatch(PressureSignal(PT.ANOMALY_DETECTED, "z", SignalEdge.RISING))
        assert n == 1 and ok == [PT.ANOMALY_DETECTED], "broken organ must not break the good one"


class TestPhase4Integration:
    async def test_signal_wakes_organ_and_shadow_traps_the_command(self, caplog):
        """Full chain: edge-triggered RESOURCE_PRESSURE fires -> LoadShedding-style
        organ wakes -> routes its shed command through shadow_guard -> TRAPPED."""
        d = EventActivationDispatcher()
        shed_executed = []

        async def load_shedder(sig: PressureSignal):
            # the reanimated organ reasons + calls its DANGEROUS action via the guard
            return shadow_guard(
                f"shed load for {sig.source}",
                lambda: shed_executed.append(sig.source),
            )

        d.register_organ("LoadSheddingController", load_shedder, [PT.RESOURCE_PRESSURE])

        # emitter -> bus(=dispatch) bridge
        import asyncio
        async def bus_emit(sig): await d.dispatch(sig)
        em = PressureSignalEmitter(lambda s: asyncio.ensure_future(bus_emit(s)))

        with caplog.at_level("WARNING"):
            sig = em.observe(PT.RESOURCE_PRESSURE, "mem", active=True, severity="critical")
            assert sig is not None  # edge fired
            # let the scheduled dispatch run
            await asyncio.sleep(0.02)

        assert shed_executed == [], "shadow mode must TRAP the shed command"
        assert any("[SHADOW MODE] Would have shed load for mem" in r.message
                   for r in caplog.records)

    async def test_phase4_with_shadow_off_executes(self, monkeypatch):
        monkeypatch.setenv("JARVIS_RESILIENCE_SHADOW_MODE", "0")
        d = EventActivationDispatcher()
        killed = []
        async def healer(sig): return shadow_guard("kill pid 9", lambda: killed.append(9))
        d.register_organ("SelfHealingOrchestrator", healer, [PT.ANOMALY_DETECTED])
        await d.dispatch(PressureSignal(PT.ANOMALY_DETECTED, "proc", SignalEdge.RISING))
        assert killed == [9], "with shadow off, the organ actually acts"


class TestBusBridge:
    async def test_attach_to_bus_routes_extracted_signals(self):
        class FakeBus:
            def __init__(self): self.handlers = []
            def subscribe(self, h): self.handlers.append(h)
            def emit(self, ev):
                for h in self.handlers: h(ev)
        d = EventActivationDispatcher()
        got = []
        async def organ(sig): got.append(sig.source)
        d.register_organ("x", organ, [PT.COMPONENT_DEGRADED])
        bus = FakeBus()
        d.attach_to_bus(bus, extract=lambda ev: ev if isinstance(ev, PressureSignal) else None)
        bus.emit(PressureSignal(PT.COMPONENT_DEGRADED, "vision", SignalEdge.RISING))
        import asyncio; await asyncio.sleep(0.02)
        assert got == ["vision"]
