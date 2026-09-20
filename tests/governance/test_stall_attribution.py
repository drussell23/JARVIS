"""Sample the main thread while it is blocked -- the one moment it matters.

`ControlPlaneWatchdog` measures lag correctly (5,559 events, p50 2,050ms,
max 43.7s) and structurally cannot attribute it: it runs ON the loop, so it
only regains control once the blocker has returned. Across every snapshot in
a session the MainThread frames were the watchdog itself.

`LoopDeadman` already solves the catastrophic case from outside the loop, but
at a 300s ceiling with a 5s heartbeat, and it is lethal. Nothing covered the
band this system lives in: 2-40s stalls that resolve.

The load-bearing test is `test_captures_the_frame_that_is_blocking`: it
blocks the main thread in a *named function* and asserts the sampler names
it. Everything else guards the instrument against becoming a problem itself.
"""
from __future__ import annotations

import threading
import time

import pytest

from backend.core.ouroboros.governance.stall_attribution import (
    StallAttributor,
    StallRecord,
    attribution_enabled,
    get_default_attributor,
    note_tick,
    stall_threshold_ms,
)


def _block_main_thread_for(seconds: float) -> None:
    """Busy-block without yielding -- what a runaway regex or a big
    ast.parse does to the loop."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        pass


# ---------------------------------------------------------------------------
# The blindspot it closes
# ---------------------------------------------------------------------------


def test_captures_the_frame_that_is_blocking():
    """The whole point: the stack is read DURING the stall, so the frame
    that is stuck is the frame that is reported."""
    att = StallAttributor(threshold_ms=150, poll_s=0.01)
    att.start()
    try:
        att.note_tick()
        _block_main_thread_for(0.6)
    finally:
        att.stop()

    records = att.records()
    assert records, "no sample taken while the main thread was blocked"
    stack = "\n".join(records[0].frames)
    assert "_block_main_thread_for" in stack, (
        f"sampler did not name the blocking frame; got:\n{stack}"
    )


def test_a_ticking_loop_is_never_sampled():
    """A healthy loop must produce no records at all, or the signal is
    noise and gets ignored."""
    att = StallAttributor(threshold_ms=150, poll_s=0.01)
    att.start()
    try:
        for _ in range(40):
            att.note_tick()
            time.sleep(0.01)
    finally:
        att.stop()
    assert att.records() == []


def test_stall_duration_is_reported():
    att = StallAttributor(threshold_ms=100, poll_s=0.01)
    att.start()
    try:
        att.note_tick()
        _block_main_thread_for(0.5)
    finally:
        att.stop()
    assert att.records()[0].stalled_ms >= 100


def test_culprit_is_the_innermost_frame():
    record = StallRecord(at_wall=0.0, stalled_ms=1.0, frames=["outer", "inner"])
    assert record.culprit == "inner"


def test_culprit_is_safe_with_no_frames():
    assert StallRecord(at_wall=0.0, stalled_ms=1.0).culprit == "<no frames>"


# ---------------------------------------------------------------------------
# The instrument must not become the problem
# ---------------------------------------------------------------------------


def test_saturation_breaker_latches_and_disarms():
    """A continuously degraded loop would otherwise emit thousands of
    records a minute and take the host's disk with it."""
    att = StallAttributor(threshold_ms=10, poll_s=0.001)
    att._sat_max = 3
    att._sat_window_s = 60.0
    att._cooldown_s = 0.0
    att.start()
    try:
        att.note_tick()
        _block_main_thread_for(0.4)
    finally:
        att.stop()
    assert att.saturated is True
    assert not att.running


def test_saturated_sampler_stays_quiet():
    """The breaker latches: a sampler that recovered on its own would
    re-enter the cascade it just escaped."""
    att = StallAttributor(threshold_ms=10, poll_s=0.001)
    att._saturated = True
    before = len(att.records())
    att._sample(999.0)
    assert len(att.records()) == before


def test_cooldown_rate_limits_records():
    att = StallAttributor(threshold_ms=10, poll_s=0.001)
    att._cooldown_s = 10.0
    att.start()
    try:
        att.note_tick()
        _block_main_thread_for(0.3)
    finally:
        att.stop()
    assert len(att.records()) <= 1


def test_ring_is_bounded():
    att = StallAttributor(threshold_ms=10, poll_s=0.01, ring_cap=4)
    for i in range(20):
        att._records.append(StallRecord(at_wall=0.0, stalled_ms=float(i)))
    assert len(att.records()) == 4


def test_note_tick_takes_no_lock():
    """The hot path is one float store. A lock here would make the
    instrument part of what it measures."""
    att = StallAttributor()
    holder = threading.Thread(target=lambda: att._lock.acquire())
    holder.start()
    holder.join()
    try:
        att.note_tick()      # must not block on the held lock
    finally:
        try:
            att._lock.release()
        except RuntimeError:
            pass


# ---------------------------------------------------------------------------
# Lifecycle + configuration
# ---------------------------------------------------------------------------


def test_start_is_idempotent():
    att = StallAttributor(threshold_ms=5000, poll_s=0.05)
    try:
        assert att.start() is True
        assert att.start() is False
    finally:
        att.stop()


def test_stop_is_safe_when_never_started():
    StallAttributor().stop()


def test_thread_is_daemon_so_it_cannot_hold_shutdown():
    att = StallAttributor(threshold_ms=5000, poll_s=0.05)
    att.start()
    try:
        assert att._thread.daemon is True
    finally:
        att.stop()


def test_disabled_never_arms(monkeypatch):
    monkeypatch.setenv("JARVIS_STALL_ATTRIBUTION_ENABLED", "false")
    att = StallAttributor()
    assert att.start() is False
    assert attribution_enabled() is False


def test_enabled_by_default(monkeypatch):
    monkeypatch.delenv("JARVIS_STALL_ATTRIBUTION_ENABLED", raising=False)
    assert attribution_enabled() is True


def test_threshold_derives_from_the_watchdog(monkeypatch):
    """One definition of 'starved' in the system, not two to keep in
    agreement."""
    monkeypatch.delenv("JARVIS_STALL_ATTRIBUTION_THRESHOLD_MS", raising=False)
    from backend.core.ouroboros.governance.control_plane_watchdog import (
        _resolve_threshold_ms,
    )
    assert stall_threshold_ms() == pytest.approx(
        max(500.0, _resolve_threshold_ms()),
    )


def test_threshold_override_is_honoured(monkeypatch):
    monkeypatch.setenv("JARVIS_STALL_ATTRIBUTION_THRESHOLD_MS", "1234")
    assert stall_threshold_ms() == 1234.0


def test_threshold_override_has_a_floor(monkeypatch):
    """Below ~500ms every scheduler hiccup becomes a stack walk."""
    monkeypatch.setenv("JARVIS_STALL_ATTRIBUTION_THRESHOLD_MS", "1")
    assert stall_threshold_ms() >= 500.0


def test_nonsense_env_falls_back(monkeypatch):
    monkeypatch.setenv("JARVIS_STALL_ATTRIBUTION_THRESHOLD_MS", "banana")
    assert stall_threshold_ms() > 0


def test_default_attributor_is_stable():
    assert get_default_attributor() is get_default_attributor()


def test_module_level_note_tick_never_raises():
    note_tick()
