"""Tests for UNMASKING terminal DW exhaustion into the outage gradient.

Run-#13 blindspot (2026-06-28 soak bt-2026-06-29-032526)
-------------------------------------------------------
The BACKGROUND route's DW failures terminated as ``background_dw_blocked_by_topology``
(``dw_severed_queued``) -- a topology PRE-BLOCK that raises *before* the model-walk
loop, so the per-route ``ProviderHealthGradient.record_sweep(success=False)`` at the
end of that loop was NEVER reached. The orchestrator then swallowed the raise as
``background_accepted``. Net: DW was 100% down for 20 minutes, yet
``is_global_outage("background")`` never tripped (the window never filled), so the
Failover FSM never awakened J-Prime.

The fix routes EVERY terminal "zero usable DW candidate for this route" exit through
a single strictly-enforced chokepoint -- ``record_terminal_exhaustion`` -- so the
topology pre-block path feeds the gradient the same raw failed sweep the model-walk
exhaustion already does. The op still fails gracefully; the telemetry stops lying.

TDD with injected fakes -- ZERO real provider calls.
"""
from __future__ import annotations

from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

import pytest

from backend.core.ouroboros.governance import provider_quarantine as pq


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    pq._PROVIDER_HEALTH_GRADIENT_SINGLETON = None
    monkeypatch.setenv("JARVIS_QUARANTINE_WINDOW", "5")
    monkeypatch.setenv("JARVIS_PROVIDER_QUARANTINE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_QUARANTINE_UNMASK_EXHAUSTION_ENABLED", "true")
    yield
    pq._PROVIDER_HEALTH_GRADIENT_SINGLETON = None


# ---------------------------------------------------------------------------
# The chokepoint helper
# ---------------------------------------------------------------------------

def test_record_terminal_exhaustion_feeds_failed_sweep():
    """One call -> one False sweep on the route's gradient window."""
    pq.record_terminal_exhaustion("background", reason="dw_severed_queued")
    grad = pq.get_provider_health_gradient()
    assert "background" in grad.tracked_routes()
    assert grad.success_rate("background") == 0.0


def test_record_terminal_exhaustion_fills_window_to_outage():
    """Five terminal exhaustions on a route -> is_global_outage True (the exact
    signal the Failover FSM reads to awaken J-Prime)."""
    grad = pq.get_provider_health_gradient()
    assert grad.is_global_outage("background") is False
    for _ in range(5):
        pq.record_terminal_exhaustion("background", reason="dw_severed_queued")
    assert grad.is_global_outage("background") is True


def test_record_terminal_exhaustion_gate_off_is_inert(monkeypatch):
    """OFF -> byte-identical legacy: no sweep recorded (the masking persists)."""
    monkeypatch.setenv("JARVIS_QUARANTINE_UNMASK_EXHAUSTION_ENABLED", "false")
    pq.record_terminal_exhaustion("background", reason="dw_severed_queued")
    grad = pq.get_provider_health_gradient()
    # Route never recorded -> empty window -> success_rate defaults to 1.0.
    assert grad.success_rate("background") == 1.0


def test_record_terminal_exhaustion_failsoft_on_bad_route():
    """A junk route never raises (fail-soft)."""
    pq.record_terminal_exhaustion(None, reason="x")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The candidate_generator topology-block path now feeds the gradient
# ---------------------------------------------------------------------------

def _deadline():
    return datetime.now(timezone.utc) + timedelta(seconds=120)


class _BlockedTopology:
    enabled = True

    def is_dw_blocked_for_route(self, route):  # noqa: ANN001
        # The exact dw_severed_queued shape: DW skip_and_queue-blocked for the route.
        return (True, "catalog purged: empty static list", "skip_and_queue")


async def test_topology_block_records_failed_sweep(monkeypatch):
    """Driving the REAL _generate_dispatch into a topology skip_and_queue block
    on BACKGROUND now records a failed sweep -- the previously-masked path."""
    import backend.core.ouroboros.governance.candidate_generator as cg
    import backend.core.ouroboros.governance.provider_topology as ptopo

    # Slice 23 sentinel inactive so dispatch reaches the topology gate.
    monkeypatch.setattr(
        cg, "_slice23_should_activate_sentinel", lambda route: (False, "test")
    )
    # DW is topology-severed for the route (skip_and_queue).
    monkeypatch.setattr(ptopo, "get_topology", lambda: _BlockedTopology())

    gen = cg.CandidateGenerator(
        primary=SimpleNamespace(provider_name="claude"), jprime=None
    )
    ctx = SimpleNamespace(
        op_id="op-unmask-1",
        provider_override="",
        provider_route="background",
        is_read_only=False,
    )

    with pytest.raises(RuntimeError, match="background_dw_blocked_by_topology"):
        await gen._generate_dispatch(ctx, _deadline())

    grad = pq.get_provider_health_gradient()
    assert "background" in grad.tracked_routes()
    assert grad.success_rate("background") == 0.0  # the unmasked truth
