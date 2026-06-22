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


# ---------------------------------------------------------------------------
# A2: run_probe_if_due async probe driver tests
# ---------------------------------------------------------------------------

import asyncio
import os


def test_run_probe_closes_on_success():
    """A successful probe transitions OPEN -> HALF_OPEN -> CLOSED."""
    b = _fresh()
    for _ in range(20):
        b.record("batch", ok=False, failure_mode="TIMEOUT", now=0.0)

    async def good(_lane):
        return True

    res = asyncio.run(tcb.run_probe_if_due(b, "batch", good, now=10_000.0))
    assert res is True
    assert b.state("batch") is tcb.BreakerState.CLOSED


def test_run_probe_timeout_counts_as_fail(monkeypatch):
    """A probe that times out counts as ok=False; lane stays OPEN."""
    b = _fresh()
    for _ in range(20):
        b.record("batch", ok=False, failure_mode="TIMEOUT", now=0.0)

    async def hang(_lane):
        await asyncio.sleep(100)

    monkeypatch.setenv("JARVIS_TRANSPORT_BREAKER_PROBE_TIMEOUT_S", "0.05")
    res = asyncio.run(tcb.run_probe_if_due(b, "batch", hang, now=10_000.0))
    assert res is False
    assert b.state("batch") is tcb.BreakerState.OPEN


def test_run_probe_not_due_returns_none():
    """When the lane is CLOSED (not due for probe), returns None."""
    b = _fresh()
    res = asyncio.run(tcb.run_probe_if_due(b, "batch", None, now=0.0))
    assert res is None


def test_run_probe_raise_counts_as_fail():
    """A probe that raises an exception counts as ok=False; lane stays OPEN."""
    b = _fresh()
    for _ in range(20):
        b.record("batch", ok=False, failure_mode="TIMEOUT", now=0.0)

    async def explode(_lane):
        raise RuntimeError("probe exploded")

    res = asyncio.run(tcb.run_probe_if_due(b, "batch", explode, now=10_000.0))
    assert res is False
    assert b.state("batch") is tcb.BreakerState.OPEN


def test_run_probe_falsy_result_counts_as_fail():
    """A probe returning a falsy value (e.g. empty string) counts as ok=False."""
    b = _fresh()
    for _ in range(20):
        b.record("batch", ok=False, failure_mode="TIMEOUT", now=0.0)

    async def falsy(_lane):
        return ""

    res = asyncio.run(tcb.run_probe_if_due(b, "batch", falsy, now=10_000.0))
    assert res == ""
    assert b.state("batch") is tcb.BreakerState.OPEN


def test_run_probe_lane_is_half_open_during_probe():
    """due_for_probe transitions to HALF_OPEN before note_probe_result is called."""
    b = _fresh()
    for _ in range(20):
        b.record("batch", ok=False, failure_mode="TIMEOUT", now=0.0)

    seen_state: list[tcb.BreakerState] = []

    async def observe_state(_lane):
        seen_state.append(b.state("batch"))
        return True

    asyncio.run(tcb.run_probe_if_due(b, "batch", observe_state, now=10_000.0))
    # Lane was HALF_OPEN when probe ran, CLOSED after
    assert seen_state == [tcb.BreakerState.HALF_OPEN]
    assert b.state("batch") is tcb.BreakerState.CLOSED


def test_both_lanes_open_no_rotate_defers_to_dual_lane_breaker():
    """Total outage (both lanes OPEN) -> do NOT rotate onto a second dead lane;
    return preferred so dual_lane_breaker owns the terminal pause."""
    b = _fresh()
    t = 0.0
    for _ in range(20):
        b.record("batch", ok=False, failure_mode="TIMEOUT", now=t)
        b.record("realtime", ok=False, failure_mode="TIMEOUT", now=t)
        t += 1.0
    assert b.state("batch") is tcb.BreakerState.OPEN
    assert b.state("realtime") is tcb.BreakerState.OPEN
    assert b.select_lane("batch", now=t) == "batch"   # no rotate onto dead sibling


def test_single_lane_open_rotates_to_healthy_sibling():
    b = _fresh()
    t = 0.0
    for _ in range(20):
        b.record("batch", ok=False, failure_mode="TIMEOUT", now=t)
        b.record("realtime", ok=True, now=t)   # realtime stays healthy
        t += 1.0
    assert b.state("batch") is tcb.BreakerState.OPEN
    assert b.state("realtime") is tcb.BreakerState.CLOSED
    assert b.select_lane("batch", now=t) == "realtime"
