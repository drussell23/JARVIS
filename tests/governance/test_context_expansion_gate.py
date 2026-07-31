"""CONTEXT_EXPANSION is gated on LATENCY, not on which provider is primary.

The guard disabled expansion whenever DoubleWord was primary, because DW was
batch-only and a plan() round-trip took 2-4 minutes. That judgement was right
for its time. Its PREMISE changed when DW's realtime plane (SSE + priority
service_tier) became the default, with TTFT in seconds — and the guard kept
answering the old way, because it was phrased as a provider question rather
than the latency question it actually asks.

The cost was not small. Skipping CONTEXT_EXPANSION skips the ENTIRE
architecture-memory arc — ModuleContextRouter, the admission ledger, operator
rules — plus Oracle dependency injection. A 28-minute armed soak
(bt-2026-07-31-171143) ran 79 ops through GENERATE and logged zero memory
routing for exactly this reason, while every flag involved read as "on".
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.doubleword_provider import (
    dw_realtime_plane_active,
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for var in ("JARVIS_DW_SERVICE_TIER_ENABLED", "JARVIS_DW_RT_SERVICE_TIER",
                "JARVIS_CONTEXT_EXPANSION_ENABLED"):
        monkeypatch.delenv(var, raising=False)
    yield


def test_realtime_plane_is_active_by_default():
    """RT is the default plane, so the batch rationale does not apply."""
    assert dw_realtime_plane_active() is True


def test_disabling_service_tier_means_batch_plane(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_SERVICE_TIER_ENABLED", "0")
    assert dw_realtime_plane_active() is False


def test_empty_tier_means_batch_plane(monkeypatch):
    """An empty tier is how `apply_rt_service_tier` is told to skip
    injection — so it is also how the plane reads as batch."""
    monkeypatch.setenv("JARVIS_DW_RT_SERVICE_TIER", "")
    assert dw_realtime_plane_active() is False


def test_the_predicate_shares_its_inputs_with_the_stamper():
    """One definition of "RT is in play".

    `dw_realtime_plane_active` and `apply_rt_service_tier` must consult the
    same two knobs, or the gate can believe RT is active on a request the
    stamper declined to mark — which is the drift this predicate exists to
    prevent.
    """
    import inspect

    from backend.core.ouroboros.governance import doubleword_provider as dwp

    src = inspect.getsource(dwp.dw_realtime_plane_active)
    assert "dw_service_tier_enabled" in src
    assert "_dw_rt_service_tier" in src


@pytest.mark.parametrize("junk", ["   ", "!!!", "priority ", "TRUE", "0"])
def test_predicate_never_raises_on_odd_values(monkeypatch, junk):
    """A null byte cannot be tested here — os.environ rejects it on SET, so
    the exception would be the test's, not the predicate's. These are the
    values that can actually reach it."""
    monkeypatch.setenv("JARVIS_DW_RT_SERVICE_TIER", junk)
    monkeypatch.setenv("JARVIS_DW_SERVICE_TIER_ENABLED", junk)
    assert isinstance(dw_realtime_plane_active(), bool)


# ---------------------------------------------------------------------------
# The gate itself
# ---------------------------------------------------------------------------


def _gate_source() -> str:
    from pathlib import Path
    return (Path(__file__).resolve().parents[2]
            / "backend/core/ouroboros/governance/governed_loop_service.py"
            ).read_text(encoding="utf-8")


def test_the_gate_no_longer_disables_on_dw_primary_alone():
    """The regression that would reintroduce the silence."""
    src = _gate_source()
    assert "dw_realtime_plane_active" in src, (
        "the gate no longer consults the plane — DW-primary would disable "
        "expansion again, taking the whole memory arc with it")


def test_the_batch_rationale_is_preserved():
    """The original judgement still applies on the batch plane.

    Flipping the gate to always-on would have been the workaround: it fixes
    the RT case by breaking the case the guard was written for.
    """
    src = _gate_source()
    assert "BATCH plane" in src


def test_an_operator_override_exists_and_wins():
    src = _gate_source()
    assert "JARVIS_CONTEXT_EXPANSION_ENABLED" in src
    # The override is checked BEFORE the provider inference, so it wins.
    assert src.index("JARVIS_CONTEXT_EXPANSION_ENABLED") < src.index(
        "dw_realtime_plane_active")
