"""Non-critical telemetry backs off when the loop is starved. Nothing else does.

Measured, bt-2026-09-09-024244::

    [ControlPlaneStarvation] lag_ms=1739.6 (requested=100.0 observed=1839.6)
        threshold=500.0 event_n=34 — main asyncio loop is starved

11x the warn threshold with an 8-agent exploration fleet running, and the
load-shed latch that exists for exactly this did nothing: it requires an LLM
stream to be active, and no stream was.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.governance import control_plane_load_shed as LS
from backend.core.ouroboros.governance.autonomy.autonomy_types import (
    EventEnvelope,
    EventType,
)
from backend.core.ouroboros.governance.autonomy.event_emitter import EventEmitter


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    LS._reset_for_test()
    monkeypatch.delenv("JARVIS_TELEMETRY_SHED_LAG_THRESHOLD_MS", raising=False)
    yield
    LS._reset_for_test()


def _event() -> EventEnvelope:
    return EventEnvelope(
        source_layer="L1", event_type=EventType.HEALTH_PROBE_RESULT,
        payload={"ok": True}, op_id="op-test",
    )


# --------------------------------------------------------------------------
# The gap the soak found
# --------------------------------------------------------------------------

def test_starvation_sheds_without_a_stream(monkeypatch):
    """THE defect: the latch's stream precondition made lag alone inert."""
    monkeypatch.setenv("JARVIS_CONTROL_PLANE_LOAD_SHED_ENABLED", "true")
    monkeypatch.setenv("JARVIS_TELEMETRY_SHED_LAG_THRESHOLD_MS", "500")
    assert LS.is_shedding() is False, "no stream — the old latch stays closed"
    assert LS.telemetry_shedding(lag_ms=1739.6) is True


def test_a_healthy_loop_does_not_shed(monkeypatch):
    monkeypatch.setenv("JARVIS_CONTROL_PLANE_LOAD_SHED_ENABLED", "true")
    monkeypatch.setenv("JARVIS_TELEMETRY_SHED_LAG_THRESHOLD_MS", "500")
    assert LS.telemetry_shedding(lag_ms=12.0) is False


def test_unknown_lag_never_sheds(monkeypatch):
    """0.0 means BOTH 'healthy' and 'unreadable'. Both must keep the data —
    going blind when the signal fails is the opposite of observability."""
    monkeypatch.setenv("JARVIS_CONTROL_PLANE_LOAD_SHED_ENABLED", "true")
    assert LS.telemetry_shedding(lag_ms=0.0) is False

    def _boom():
        raise RuntimeError("watchdog unavailable")

    import backend.core.ouroboros.governance.control_plane_watchdog as W

    monkeypatch.setattr(W, "recent_lag_ms", _boom)
    assert LS.telemetry_shedding() is False


def test_the_flag_still_gates_it(monkeypatch):
    monkeypatch.delenv("JARVIS_CONTROL_PLANE_LOAD_SHED_ENABLED", raising=False)
    assert LS.telemetry_shedding(lag_ms=99_999.0) is False


def test_threshold_is_derived_from_the_watchdogs_own(monkeypatch):
    """One definition of 'starved'. Two constants drift."""
    monkeypatch.delenv("JARVIS_TELEMETRY_SHED_LAG_THRESHOLD_MS", raising=False)
    monkeypatch.setenv("JARVIS_CONTROL_PLANE_WATCHDOG_THRESHOLD_MS", "250")
    assert LS.telemetry_threshold_ms() == 250.0


# --------------------------------------------------------------------------
# What may be shed, and what may never be
# --------------------------------------------------------------------------

def test_subscribers_are_delivered_to_even_under_backpressure(monkeypatch):
    """A subscriber is a control path. An event it misses is a decision that
    does not happen — that is data loss, not backpressure."""
    monkeypatch.setenv("JARVIS_CONTROL_PLANE_LOAD_SHED_ENABLED", "true")
    monkeypatch.setenv("JARVIS_TELEMETRY_SHED_LAG_THRESHOLD_MS", "1")
    monkeypatch.setattr(LS, "telemetry_shedding", lambda *a, **k: True)

    seen = []
    em = EventEmitter()
    em.subscribe(EventType.HEALTH_PROBE_RESULT, lambda e: seen.append(e))

    asyncio.run(em.emit(_event()))

    assert len(seen) == 1, "backpressure dropped a subscriber delivery"


def test_the_spine_copy_is_shed_and_counted(monkeypatch):
    published = []

    class _Bus:
        async def publish_raw(self, **kw):
            published.append(kw)

    import backend.core.trinity_event_bus as BUS

    monkeypatch.setattr(BUS, "get_event_bus_if_exists", lambda: _Bus())
    monkeypatch.setattr(LS, "telemetry_shedding", lambda *a, **k: True)

    em = EventEmitter()
    asyncio.run(em.emit(_event()))

    assert published == [], "the observability copy was published while starved"
    counts = LS.shed_counts()
    assert counts.get("autonomy.health_probe_result") == 1, (
        f"a dropped event left no record: {counts}"
    )


def test_the_spine_copy_flows_when_the_loop_is_healthy(monkeypatch):
    published = []

    class _Bus:
        async def publish_raw(self, **kw):
            published.append(kw)

    import backend.core.trinity_event_bus as BUS

    monkeypatch.setattr(BUS, "get_event_bus_if_exists", lambda: _Bus())
    monkeypatch.setattr(LS, "telemetry_shedding", lambda *a, **k: False)

    em = EventEmitter()
    asyncio.run(em.emit(_event()))

    assert len(published) == 1
    assert published[0]["topic"] == "autonomy.health_probe_result"
    assert LS.shed_counts() == {}


def test_backpressure_lifts_by_itself(monkeypatch):
    """Unlatched by design: a reading, not a state. The first consumer needs a
    stream boundary to clear its latch; telemetry has no such boundary."""
    monkeypatch.setenv("JARVIS_CONTROL_PLANE_LOAD_SHED_ENABLED", "true")
    monkeypatch.setenv("JARVIS_TELEMETRY_SHED_LAG_THRESHOLD_MS", "500")
    assert LS.telemetry_shedding(lag_ms=900.0) is True
    assert LS.telemetry_shedding(lag_ms=10.0) is False
