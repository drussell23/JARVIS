"""Tests for the Multi-Vector Awaken Trigger (Task CR2).

The J-Prime golden-image fallback node must awaken not only on a DW data-plane
outage but ALSO on cloud-budget exhaustion (the cloud primary refused on budget
with NO cloud fallback configured). The controller must additionally REMEMBER
*why* it awakened (``_awaken_reason``) so a later recovery strategy can branch.

DW stays primary; J-Prime is the fallback. All boundaries are injected fakes ->
ZERO real GCP / network. Default-OFF byte-identical: the budget vector fires only
when ``JARVIS_FAILOVER_BUDGET_AWAKEN_ENABLED=true``.
"""
from __future__ import annotations

import pytest

import backend.core.ouroboros.governance.failover_lifecycle as fl
from backend.core.ouroboros.governance import provider_quarantine as pq
from backend.core.ouroboros.governance.failover_lifecycle import (
    AWAKEN_REASON_BUDGET,
    AWAKEN_REASON_DATA_PLANE,
    FailoverLifecycleController,
    FailoverState,
)


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    pq._PROVIDER_HEALTH_GRADIENT_SINGLETON = None
    fl._reset_singleton_for_tests()
    monkeypatch.setenv("JARVIS_FAILOVER_LIFECYCLE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_FAILOVER_ROUTE", "dw")
    # Keep the other awaken vectors quiet so only the budget vector is exercised.
    monkeypatch.setenv("JARVIS_FAILOVER_ANY_ROUTE_OUTAGE_ENABLED", "false")
    monkeypatch.setenv("JARVIS_FAILOVER_EARLY_PREWARM_ENABLED", "false")
    monkeypatch.delenv("JARVIS_FAILOVER_BUDGET_AWAKEN_ENABLED", raising=False)
    yield
    pq._PROVIDER_HEALTH_GRADIENT_SINGLETON = None
    fl._reset_singleton_for_tests()


def _make_ctrl(clock, awakens=None, flares=None, **kw):
    awakens = awakens if awakens is not None else []
    flares = flares if flares is not None else []

    def _awaken(*, startup_script):
        awakens.append(startup_script)
        return True

    defaults = dict(
        vm_awaken_fn=_awaken,
        vm_delete_fn=lambda: True,
        dw_probe_fn=lambda: False,
        node_ready_fn=lambda endpoint: True,
        clock_fn=clock,
        is_degrading_fn=lambda: False,
        flare_fn=lambda payload: flares.append(payload),
    )
    defaults.update(kw)
    return FailoverLifecycleController(**defaults)


# ---------------------------------------------------------------------------
# note_budget_exhausted anchors the budget vector (idempotent)
# ---------------------------------------------------------------------------

def test_note_budget_exhausted_sets_anchor():
    clock = FakeClock()
    ctrl = _make_ctrl(clock)
    assert ctrl._budget_exhausted_at is None

    ctrl.note_budget_exhausted()
    first = ctrl._budget_exhausted_at
    assert first is not None

    # Idempotent: a second call does NOT re-anchor (only the first wins).
    clock.t += 50.0
    ctrl.note_budget_exhausted()
    assert ctrl._budget_exhausted_at == first


# ---------------------------------------------------------------------------
# Budget vector awakens J-Prime when the master flag is ON
# ---------------------------------------------------------------------------

async def test_budget_vector_awakens_when_enabled(monkeypatch):
    monkeypatch.setenv("JARVIS_FAILOVER_BUDGET_AWAKEN_ENABLED", "true")
    clock = FakeClock()
    awakens = []
    ctrl = _make_ctrl(clock, awakens=awakens)
    # No data-plane outage at all -- only the budget vector should fire.
    assert ctrl.state == FailoverState.DORMANT

    ctrl.note_budget_exhausted()
    await ctrl.tick()

    assert ctrl.state == FailoverState.AWAKENING
    assert ctrl._awaken_reason == AWAKEN_REASON_BUDGET
    assert len(awakens) == 1  # the GCE awaken boundary actually fired
    # Anchor is consumed (single-shot) so it doesn't re-fire every tick.
    assert ctrl._budget_exhausted_at is None


# ---------------------------------------------------------------------------
# Default-OFF byte-identical: budget vector inert when the flag is OFF
# ---------------------------------------------------------------------------

async def test_budget_vector_inert_when_flag_off(monkeypatch):
    # Flag intentionally NOT set (default OFF).
    clock = FakeClock()
    awakens = []
    ctrl = _make_ctrl(clock, awakens=awakens)

    ctrl.note_budget_exhausted()
    await ctrl.tick()

    assert ctrl.state == FailoverState.DORMANT
    assert awakens == []
    assert ctrl._awaken_reason == ""  # reason field stays inert


# ---------------------------------------------------------------------------
# awaken_reason taxonomy: a data-plane trigger maps to DATA_PLANE_OUTAGE
# ---------------------------------------------------------------------------

async def test_awaken_reason_data_plane_for_outage_trigger():
    clock = FakeClock()
    ctrl = _make_ctrl(clock)

    await ctrl._enter_awakening(
        now=clock(), trigger="reactive_outage", route="background",
    )

    assert ctrl._awaken_reason == AWAKEN_REASON_DATA_PLANE
