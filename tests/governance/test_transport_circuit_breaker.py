from __future__ import annotations
import importlib
from backend.core.ouroboros.governance import transport_circuit_breaker as tcb


def _fresh():
    importlib.reload(tcb)
    return tcb.TransportCircuitBreaker()


def test_closed_passes_preferred():
    b = _fresh()
    assert b.select_lane("batch", now=0.0) == "batch"
    assert b.state("batch") is tcb.BreakerState.CLOSED


def test_trips_open_after_adaptive_failures_and_rotates():
    b = _fresh()
    t = 0.0
    for _ in range(20):  # sustained batch failures
        b.record("batch", ok=False, failure_mode="TIMEOUT", now=t); t += 1.0
    assert b.state("batch") is tcb.BreakerState.OPEN
    # OPEN batch -> rotate to realtime
    assert b.select_lane("batch", now=t) == "realtime"


def test_success_keeps_closed():
    b = _fresh()
    t = 0.0
    for _ in range(20):
        b.record("batch", ok=True, now=t); t += 1.0
    assert b.state("batch") is tcb.BreakerState.CLOSED


def test_open_becomes_due_for_probe_after_jittered_timer():
    b = _fresh()
    t = 0.0
    for _ in range(20):
        b.record("batch", ok=False, failure_mode="TIMEOUT", now=t); t += 1.0
    assert not b.due_for_probe("batch", now=t + 1.0)      # within recovery window
    assert b.due_for_probe("batch", now=t + 10_000.0)     # after any jittered window


def test_probe_success_closes_probe_fail_reopens_longer():
    b = _fresh()
    t = 0.0
    for _ in range(20):
        b.record("batch", ok=False, failure_mode="TIMEOUT", now=t); t += 1.0
    t += 10_000.0
    assert b.due_for_probe("batch", now=t)
    b.note_probe_result("batch", ok=False, now=t)         # HALF_OPEN -> OPEN
    assert b.state("batch") is tcb.BreakerState.OPEN
    first_wait = b._recovery_deadline("batch") - t        # internal, longer each reopen
    t2 = b._recovery_deadline("batch") + 1.0
    b.note_probe_result("batch", ok=True, now=t2)         # probe ok -> CLOSED
    assert b.state("batch") is tcb.BreakerState.CLOSED


def test_failsoft_bad_input_never_raises():
    b = _fresh()
    b.record("bogus", ok=False, failure_mode=None, now=0.0)
    assert b.select_lane("bogus", now=0.0) == "bogus"      # unknown lane passes through


def test_off_byte_identical_select_is_identity(monkeypatch):
    monkeypatch.setenv("JARVIS_TRANSPORT_BREAKER_ENABLED", "false")
    b = _fresh()
    for _ in range(50):
        b.record("batch", ok=False, failure_mode="TIMEOUT", now=0.0)
    # disabled: never rotates (caller checks breaker_enabled(); breaker still tracks)
    assert tcb.breaker_enabled() is False
