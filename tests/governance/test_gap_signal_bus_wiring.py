"""Task #3 — the Shannon-entropy capability-gap loop, re-wired.

Root cause (verified): orchestrator.py and phase_runners/validate_runner.py both
emitted an IMMEDIATE_TRIGGER capability gap by calling
``GapSignalBus.get_instance()`` — a method that DOES NOT EXIST. The resulting
AttributeError was swallowed by a blanket ``except``, so for the life of the
feature the entropy gap signal reached the CapabilityGapSensor consumer NEVER:
producer live, consumer live, wire calling a phantom method.

These tests are the tripwire the original code lacked — they assert
*reachability* (a produced event actually arrives at the consumer's singleton),
not mere existence. The AST/source pins that guarded the old code proved the
function existed; nothing proved it was called or that it worked.
"""
from __future__ import annotations

import asyncio

from backend.neural_mesh.synthesis.gap_signal_bus import (
    CapabilityGapEvent,
    GapSignalBus,
    emit_capability_gap,
    get_gap_signal_bus,
)


def test_get_instance_never_reintroduced():
    """The phantom accessor must never come back. If a future refactor adds a
    ``get_instance`` that mints a fresh bus, this is the guard."""
    assert not hasattr(GapSignalBus, "get_instance"), (
        "GapSignalBus.get_instance() is the phantom accessor that silently "
        "severed the entropy gap loop — producers MUST use get_gap_signal_bus()"
    )


def test_producer_and_consumer_share_one_singleton():
    """emit_capability_gap lands on the exact instance get_gap_signal_bus()
    returns — the consumer's view. This is the property whose ABSENCE (a second,
    disconnected bus) is the subtler failure mode the fix forecloses."""
    async def _run():
        consumer_bus = get_gap_signal_bus()
        ev = CapabilityGapEvent(
            goal="reachability probe", task_type="code_gen::.py",
            target_app="ouroboros", source="entropy_calculator",
        )
        assert emit_capability_gap(ev) is True
        got = await asyncio.wait_for(consumer_bus.get(), timeout=2.0)
        assert got is ev  # identity — literally the same object, same bus
    asyncio.run(_run())


def test_emit_capability_gap_fail_soft(monkeypatch):
    """A side-channel emit must never raise into the caller."""
    def _boom():
        raise RuntimeError("bus construction exploded")
    monkeypatch.setattr(
        "backend.neural_mesh.synthesis.gap_signal_bus.get_gap_signal_bus",
        _boom,
    )
    ev = CapabilityGapEvent(goal="x", task_type="t", target_app="a", source="s")
    assert emit_capability_gap(ev) is False  # swallowed, reported, not raised


# ── The entropy folding helper ────────────────────────────────────────

def test_entropy_helper_reaches_the_consumer_and_preserves_signal():
    """emit_entropy_capability_gap (the DRY seam both call sites now use) must
    reach the consumer AND fold the richer entropy signal — systemic score into
    the goal, quadrant recommendation into resolution_mode (no hardcoding, no
    information loss vs the discarded CognitiveInefficiencyEvent)."""
    from backend.core.ouroboros.governance.entropy_calculator import (
        emit_entropy_capability_gap,
    )
    from backend.core.ouroboros.governance.entropy_calculator import (
        EntropyQuadrant,
        _QUADRANT_RECOMMENDATIONS,
    )

    class _Composite:
        systemic_score = 0.913
        quadrant = EntropyQuadrant.IMMEDIATE_TRIGGER

    async def _run():
        bus = get_gap_signal_bus()
        ok = emit_entropy_capability_gap(
            op_id="op-42", domain_key="code_gen::.py",
            composite=_Composite(), description="live entropy trigger",
        )
        assert ok is True
        got = await asyncio.wait_for(bus.get(), timeout=2.0)
        assert got.task_type == "code_gen::.py"
        assert got.source == "entropy_calculator"
        assert "0.913" in got.goal            # systemic score preserved
        assert "live entropy trigger" in got.goal
        # resolution_mode carries the REAL quadrant recommendation, not "synthesis"
        assert got.resolution_mode == _QUADRANT_RECOMMENDATIONS[
            EntropyQuadrant.IMMEDIATE_TRIGGER
        ]
    asyncio.run(_run())


def test_entropy_helper_fail_soft_on_garbage_composite():
    from backend.core.ouroboros.governance.entropy_calculator import (
        emit_entropy_capability_gap,
    )
    # A composite missing every attribute must not raise.
    ok = emit_entropy_capability_gap(
        op_id="op-x", domain_key="d", composite=object(), description="",
    )
    assert ok in (True, False)  # returns a bool, never raises


# ── The producers are actually wired (no silent inert regression) ─────────

def test_both_producers_call_the_seam():
    """Guard against re-severing: both call sites must invoke the seam and must
    NOT contain the phantom accessor."""
    import inspect
    from backend.core.ouroboros.governance import orchestrator
    from backend.core.ouroboros.governance.phase_runners import validate_runner

    for mod in (orchestrator, validate_runner):
        src = inspect.getsource(mod)
        assert "emit_entropy_capability_gap(" in src, (
            f"{mod.__name__} no longer calls the capability-gap seam — re-severed"
        )
        assert "GapSignalBus.get_instance()" not in src, (
            f"{mod.__name__} still calls the phantom GapSignalBus.get_instance()"
        )
