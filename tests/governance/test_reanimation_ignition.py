"""Sovereign Fusion — regression spine for igniting the resilience matrix.

These tests deliberately DO NOT import ``unified_supervisor`` (its import is
sandbox-blocked by the split-brain guard). They exercise the REAL
``backend.core.cybernetic_reanimation`` primitives + the extracted pure
producer predicate, with fakes standing in for the 7 reanimated organs.

Two concerns under test:
  1. The PRODUCER logic — ``_pressure_active`` threshold predicate fed through
     ``PressureSignalEmitter.observe`` proves edge-triggering: a single RISING
     edge while pressure is sustained, then exactly one FALLING edge on clear.
  2. The WIRING — a synthetic ``RESOURCE_PRESSURE`` rising edge dispatched
     through a real ``EventActivationDispatcher`` reaches a registered mock
     organ handler. Shadow-safety is inherited from source-level
     ``shadow_guard`` chokepoints (referenced, not re-tested here).
"""
from __future__ import annotations

import asyncio

import pytest

from backend.core.cybernetic_reanimation import (
    EventActivationDispatcher,
    PressureSignal,
    PressureSignalEmitter,
    PressureSignalType,
    SignalEdge,
    _pressure_active,
    resilience_shadow_mode_enabled,
)


# ---------------------------------------------------------------------------
# 1. Producer predicate — pure threshold logic
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "mem,cpu,mem_thr,cpu_thr,expected",
    [
        (0.5, 0.5, 0.9, 0.9, False),   # both below
        (0.95, 0.5, 0.9, 0.9, True),   # mem over
        (0.5, 0.95, 0.9, 0.9, True),   # cpu over
        (0.9, 0.0, 0.9, 0.9, True),    # at-threshold mem (>=)
        (0.0, 0.9, 0.9, 0.9, True),    # at-threshold cpu (>=)
        (0.89, 0.89, 0.9, 0.9, False), # just below both
    ],
)
def test_pressure_active_predicate(mem, cpu, mem_thr, cpu_thr, expected):
    assert _pressure_active(mem, cpu, mem_thr, cpu_thr) is expected


def test_pressure_active_never_raises_on_bad_input():
    # sampler must survive a degenerate probe reading
    assert _pressure_active(None, "x", 0.9, 0.9) is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 2. Edge-triggering through the real emitter (the producer's signal shaping)
# ---------------------------------------------------------------------------

def _make_emitter():
    emitted = []
    em = PressureSignalEmitter(emit_fn=emitted.append)
    return em, emitted


def test_sampler_rising_once_then_silent_while_sustained():
    em, emitted = _make_emitter()
    MEM_THR = CPU_THR = 0.9

    def sample(mem, cpu):
        return em.observe(
            PressureSignalType.RESOURCE_PRESSURE,
            "system",
            active=_pressure_active(mem, cpu, MEM_THR, CPU_THR),
            detail={"mem": mem, "cpu": cpu},
        )

    # below threshold — no edge
    assert sample(0.5, 0.5) is None
    # crosses up — exactly one RISING edge
    sig = sample(0.95, 0.5)
    assert sig is not None and sig.edge is SignalEdge.RISING
    assert sig.type is PressureSignalType.RESOURCE_PRESSURE
    # still over — sustained, NO re-emit (the anti-spam invariant)
    assert sample(0.96, 0.6) is None
    assert sample(0.99, 0.2) is None
    # only one signal so far
    assert len(emitted) == 1
    assert emitted[0].edge is SignalEdge.RISING


def test_sampler_falling_edge_on_clear():
    em, emitted = _make_emitter()
    MEM_THR = CPU_THR = 0.9

    def sample(mem, cpu):
        return em.observe(
            PressureSignalType.RESOURCE_PRESSURE,
            "system",
            active=_pressure_active(mem, cpu, MEM_THR, CPU_THR),
        )

    sample(0.95, 0.5)            # rising
    falling = sample(0.1, 0.1)   # clear -> falling
    assert falling is not None and falling.edge is SignalEdge.FALLING
    # back below stays silent
    assert sample(0.2, 0.2) is None
    assert [s.edge for s in emitted] == [SignalEdge.RISING, SignalEdge.FALLING]


# ---------------------------------------------------------------------------
# 3. Ignition wiring — emitter -> dispatcher -> mock organ handler
# ---------------------------------------------------------------------------

def test_rising_edge_reaches_registered_organ():
    """Prove the full producer->dispatcher->organ synapse: a RESOURCE_PRESSURE
    rising edge produced by the emitter is delivered to a registered mock organ.

    This mirrors what ``_ignite_resilience_reanimation`` wires in the kernel
    (build_resilience_dispatcher + PressureSignalEmitter(emit_fn=schedule)),
    but stays on the importable primitives since unified_supervisor cannot be
    imported under the split-brain guard.
    """
    received = []

    async def organ_handler(sig: PressureSignal):
        received.append(sig)

    dispatcher = EventActivationDispatcher()
    dispatcher.register_organ(
        "FakeLoadShedder",
        organ_handler,
        [PressureSignalType.RESOURCE_PRESSURE],
    )

    # the kernel's _schedule_dispatch does loop.create_task(dispatcher.dispatch)
    scheduled = []

    def schedule(sig):
        scheduled.append(sig)

    emitter = PressureSignalEmitter(emit_fn=schedule)

    # produce a rising edge
    emitter.observe(PressureSignalType.RESOURCE_PRESSURE, "system", active=True)
    assert len(scheduled) == 1

    # now actually run dispatch (what create_task would do on the loop)
    delivered = asyncio.run(dispatcher.dispatch(scheduled[0]))
    assert delivered == 1
    assert len(received) == 1
    assert received[0].type is PressureSignalType.RESOURCE_PRESSURE
    assert received[0].edge is SignalEdge.RISING


def test_dispatch_is_fail_soft_across_organs():
    """One broken organ never starves the others (the bus invariant the
    ignition relies on)."""
    good = []

    async def broken(sig):
        raise RuntimeError("organ exploded")

    async def healthy(sig):
        good.append(sig)

    d = EventActivationDispatcher()
    d.register_organ("Broken", broken, [PressureSignalType.RESOURCE_PRESSURE])
    d.register_organ("Healthy", healthy, [PressureSignalType.RESOURCE_PRESSURE])

    sig = PressureSignal(
        type=PressureSignalType.RESOURCE_PRESSURE,
        source="system",
        edge=SignalEdge.RISING,
    )
    delivered = asyncio.run(d.dispatch(sig))
    # the healthy organ still received the signal; broken one swallowed
    assert delivered == 1
    assert len(good) == 1


def test_shadow_mode_default_on_is_inherited():
    """Shadow-safety is inherited at the SOURCE chokepoints (shadow_guard wraps
    every dangerous action). Reanimation only wakes the organs; it does not
    bypass that guard. Default posture is shadow-ON (fail-safe)."""
    import os

    prev = os.environ.pop("JARVIS_RESILIENCE_SHADOW_MODE", None)
    try:
        assert resilience_shadow_mode_enabled() is True
    finally:
        if prev is not None:
            os.environ["JARVIS_RESILIENCE_SHADOW_MODE"] = prev
