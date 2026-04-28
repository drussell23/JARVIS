"""Phase 1 Slice 1.1 — DeterministicClock regression spine.

Pins:
  §1  clock_enabled flag — default false; case-tolerant
  §2  ClockMode resolution: explicit > env > master flag
  §3  RealClock PASSTHROUGH — pure passthrough, no recording
  §4  RealClock RECORD — captures monotonic / wall_clock / sleep
  §5  RealClock RECORD — trace bounded by JARVIS_DETERMINISM_CLOCK_TRACE_MAX
  §6  FrozenClock REPLAY — returns recorded values in order
  §7  FrozenClock REPLAY — sleep is instant (no actual blocking)
  §8  FrozenClock REPLAY — past-end-of-trace falls back gracefully
  §9  FrozenClock REPLAY — warn-once per kind (no log spam)
  §10 clock_for_session caching: same op → same instance
  §11 clock_for_session — different ops, different instances
  §12 clock_for_session — explicit mode override
  §13 clock_for_session — env mode override (passthrough/record/replay)
  §14 import_trace + export_trace round-trip
  §15 NEVER-raises contract on garbage input
  §16 Authority invariants — no orchestrator/phase_runner imports
  §17 Async sleep semantics
"""
from __future__ import annotations

import asyncio
import time as _time
from typing import Any, Optional

import pytest

from backend.core.ouroboros.governance.determinism import (
    FrozenClock,
    RealClock,
    clock_enabled,
    clock_for_session,
)
from backend.core.ouroboros.governance.determinism.clock import (
    ClockMode,
    _resolve_mode,
    reset_all_for_tests,
    reset_for_op,
)


@pytest.fixture
def clean_clock_state(monkeypatch):
    """Reset clock cache + clear any leftover env vars between tests."""
    monkeypatch.delenv("JARVIS_DETERMINISM_CLOCK_ENABLED", raising=False)
    monkeypatch.delenv("OUROBOROS_DETERMINISM_CLOCK_MODE", raising=False)
    monkeypatch.delenv("OUROBOROS_BATTLE_SESSION_ID", raising=False)
    monkeypatch.delenv("JARVIS_DETERMINISM_CLOCK_TRACE_MAX", raising=False)
    reset_all_for_tests()
    yield
    reset_all_for_tests()


# ---------------------------------------------------------------------------
# §1 — clock_enabled flag
# ---------------------------------------------------------------------------


def test_flag_default_false(clean_clock_state) -> None:
    assert clock_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "On"])
def test_flag_truthy(monkeypatch, clean_clock_state, val) -> None:
    monkeypatch.setenv("JARVIS_DETERMINISM_CLOCK_ENABLED", val)
    assert clock_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", "garbage", ""])
def test_flag_falsy(monkeypatch, clean_clock_state, val) -> None:
    monkeypatch.setenv("JARVIS_DETERMINISM_CLOCK_ENABLED", val)
    assert clock_enabled() is False


# ---------------------------------------------------------------------------
# §2 — Mode resolution priority
# ---------------------------------------------------------------------------


def test_explicit_mode_wins(clean_clock_state, monkeypatch) -> None:
    monkeypatch.setenv("OUROBOROS_DETERMINISM_CLOCK_MODE", "replay")
    monkeypatch.setenv("JARVIS_DETERMINISM_CLOCK_ENABLED", "true")
    # Explicit RECORD beats env=replay + flag=true
    assert _resolve_mode(ClockMode.RECORD) is ClockMode.RECORD


def test_env_mode_beats_master_flag(clean_clock_state, monkeypatch) -> None:
    """Env override beats master flag default behavior."""
    monkeypatch.setenv("OUROBOROS_DETERMINISM_CLOCK_MODE", "passthrough")
    monkeypatch.setenv("JARVIS_DETERMINISM_CLOCK_ENABLED", "true")
    # Master flag would say RECORD; env override says PASSTHROUGH
    assert _resolve_mode(None) is ClockMode.PASSTHROUGH


def test_master_flag_default_when_no_overrides(
    clean_clock_state, monkeypatch,
) -> None:
    monkeypatch.setenv("JARVIS_DETERMINISM_CLOCK_ENABLED", "true")
    # No explicit, no env → master flag → RECORD
    assert _resolve_mode(None) is ClockMode.RECORD


def test_passthrough_when_flag_off(clean_clock_state) -> None:
    """No env, flag off → PASSTHROUGH."""
    assert _resolve_mode(None) is ClockMode.PASSTHROUGH


@pytest.mark.parametrize("val", ["unknown_value", "RECORD-MODE", " "])
def test_unknown_env_mode_falls_to_flag(
    clean_clock_state, monkeypatch, val,
) -> None:
    """Unrecognized env mode → fall through to master flag."""
    monkeypatch.setenv("OUROBOROS_DETERMINISM_CLOCK_MODE", val)
    monkeypatch.setenv("JARVIS_DETERMINISM_CLOCK_ENABLED", "false")
    assert _resolve_mode(None) is ClockMode.PASSTHROUGH


# ---------------------------------------------------------------------------
# §3-§5 — RealClock
# ---------------------------------------------------------------------------


def test_passthrough_no_recording() -> None:
    c = RealClock(op_id="op-1", mode=ClockMode.PASSTHROUGH)
    c.monotonic()
    c.monotonic()
    c.wall_clock()
    trace = c.export_trace()
    assert trace["monotonic"] == []
    assert trace["wall"] == []


def test_record_captures_monotonic() -> None:
    c = RealClock(op_id="op-1", mode=ClockMode.RECORD)
    v1 = c.monotonic()
    v2 = c.monotonic()
    trace = c.export_trace()
    assert trace["monotonic"] == [v1, v2]


def test_record_captures_wall_clock() -> None:
    c = RealClock(op_id="op-1", mode=ClockMode.RECORD)
    v1 = c.wall_clock()
    v2 = c.wall_clock()
    trace = c.export_trace()
    assert trace["wall"] == [v1, v2]


@pytest.mark.asyncio
async def test_record_captures_sleep() -> None:
    c = RealClock(op_id="op-1", mode=ClockMode.RECORD)
    await c.sleep(0.01)
    await c.sleep(0.02)
    trace = c.export_trace()
    assert trace["sleep"] == [pytest.approx(0.01), pytest.approx(0.02)]


def test_real_clock_returns_real_time() -> None:
    c = RealClock(op_id="op-1", mode=ClockMode.RECORD)
    v1 = c.monotonic()
    real_now = _time.monotonic()
    # Time should be very close (within 1s for this fast test)
    assert abs(real_now - v1) < 1.0


def test_record_trace_bounded(monkeypatch) -> None:
    """Trace ring buffer caps at JARVIS_DETERMINISM_CLOCK_TRACE_MAX."""
    monkeypatch.setenv("JARVIS_DETERMINISM_CLOCK_TRACE_MAX", "1000")
    c = RealClock(op_id="op-1", mode=ClockMode.RECORD)
    for _ in range(1500):
        c.monotonic()
    trace = c.export_trace()
    assert len(trace["monotonic"]) == 1000  # capped


@pytest.mark.asyncio
async def test_negative_sleep_clamps_to_zero() -> None:
    c = RealClock(op_id="op-1", mode=ClockMode.RECORD)
    await c.sleep(-1.0)
    trace = c.export_trace()
    assert trace["sleep"] == [0.0]


# ---------------------------------------------------------------------------
# §6-§9 — FrozenClock REPLAY
# ---------------------------------------------------------------------------


def test_frozen_replays_monotonic_in_order() -> None:
    fc = FrozenClock(op_id="op-1")
    fc.import_trace(monotonic=[100.5, 200.1, 300.7])
    assert fc.monotonic() == 100.5
    assert fc.monotonic() == 200.1
    assert fc.monotonic() == 300.7


def test_frozen_replays_wall_clock_in_order() -> None:
    fc = FrozenClock(op_id="op-1")
    fc.import_trace(wall=[1.0, 2.0])
    assert fc.wall_clock() == 1.0
    assert fc.wall_clock() == 2.0


@pytest.mark.asyncio
async def test_frozen_sleep_is_instant() -> None:
    """REPLAY sleep returns immediately, regardless of recorded duration."""
    fc = FrozenClock(op_id="op-1")
    fc.import_trace(sleep=[5.0, 10.0, 30.0])  # would block 45s if real
    t0 = _time.monotonic()
    await fc.sleep(5.0)
    await fc.sleep(10.0)
    await fc.sleep(30.0)
    elapsed = _time.monotonic() - t0
    # Should complete in well under 1s (asyncio.sleep(0) only)
    assert elapsed < 1.0


@pytest.mark.asyncio
async def test_frozen_sleep_advances_cursor() -> None:
    """REPLAY sleep consumes one entry per call."""
    fc = FrozenClock(op_id="op-1")
    fc.import_trace(sleep=[1.0, 2.0])
    await fc.sleep(1.0)
    await fc.sleep(2.0)
    # Now past-end — should warn but not raise
    await fc.sleep(3.0)


def test_frozen_past_end_of_trace_falls_back() -> None:
    """When trace exhausts, return last value + warn (NEVER raise)."""
    fc = FrozenClock(op_id="op-1")
    fc.import_trace(monotonic=[100.0])
    assert fc.monotonic() == 100.0
    # Cursor now past end
    fallback = fc.monotonic()
    # Falls back to last recorded value
    assert fallback == 100.0


def test_frozen_empty_trace_returns_zero() -> None:
    fc = FrozenClock(op_id="op-1")
    fc.import_trace(monotonic=[], wall=[], sleep=[])
    # Empty trace + cursor at 0 = past-end immediately → falls back to 0.0
    assert fc.monotonic() == 0.0
    assert fc.wall_clock() == 0.0


def test_frozen_warn_once_per_kind(caplog) -> None:
    """Past-end warnings emit ONCE per call kind, not per call."""
    import logging
    caplog.set_level(logging.WARNING)
    fc = FrozenClock(op_id="op-1")
    fc.import_trace(monotonic=[])
    # 5 past-end calls — should warn only once for "monotonic"
    for _ in range(5):
        fc.monotonic()
    monotonic_warnings = [
        r for r in caplog.records
        if "kind=monotonic" in r.getMessage()
    ]
    assert len(monotonic_warnings) == 1


# ---------------------------------------------------------------------------
# §10-§13 — clock_for_session factory
# ---------------------------------------------------------------------------


def test_clock_for_session_caches_per_op(clean_clock_state) -> None:
    c1 = clock_for_session(op_id="op-A", mode=ClockMode.PASSTHROUGH)
    c2 = clock_for_session(op_id="op-A", mode=ClockMode.PASSTHROUGH)
    assert c1 is c2  # same instance


def test_clock_for_session_distinct_per_op(clean_clock_state) -> None:
    c1 = clock_for_session(op_id="op-A", mode=ClockMode.PASSTHROUGH)
    c2 = clock_for_session(op_id="op-B", mode=ClockMode.PASSTHROUGH)
    assert c1 is not c2


def test_clock_for_session_replay_returns_frozen(clean_clock_state) -> None:
    c = clock_for_session(op_id="op-A", mode=ClockMode.REPLAY)
    assert isinstance(c, FrozenClock)


def test_clock_for_session_record_returns_real(clean_clock_state) -> None:
    c = clock_for_session(op_id="op-A", mode=ClockMode.RECORD)
    assert isinstance(c, RealClock)
    assert c.mode is ClockMode.RECORD


def test_clock_for_session_env_override_replay(
    clean_clock_state, monkeypatch,
) -> None:
    monkeypatch.setenv("OUROBOROS_DETERMINISM_CLOCK_MODE", "replay")
    c = clock_for_session(op_id="op-A")
    assert isinstance(c, FrozenClock)


def test_clock_for_session_re_modes_existing_instance(
    clean_clock_state,
) -> None:
    """A second call with a different mode adapts the cached instance
    (RealClock can re-mode between PASSTHROUGH and RECORD)."""
    c1 = clock_for_session(op_id="op-A", mode=ClockMode.PASSTHROUGH)
    assert c1.mode is ClockMode.PASSTHROUGH
    c2 = clock_for_session(op_id="op-A", mode=ClockMode.RECORD)
    assert c2 is c1
    assert c2.mode is ClockMode.RECORD


# ---------------------------------------------------------------------------
# §14 — Trace round-trip
# ---------------------------------------------------------------------------


def test_record_then_replay_round_trip() -> None:
    """RECORD: capture trace. REPLAY: import + replay → same values."""
    rec = RealClock(op_id="op-1", mode=ClockMode.RECORD)
    rec.monotonic()
    rec.monotonic()
    rec.wall_clock()
    trace = rec.export_trace()

    rep = FrozenClock(op_id="op-1")
    rep.import_trace(
        monotonic=trace["monotonic"],
        wall=trace["wall"],
        sleep=trace["sleep"],
    )
    # Replay returns the recorded values in order
    assert rep.monotonic() == trace["monotonic"][0]
    assert rep.monotonic() == trace["monotonic"][1]
    assert rep.wall_clock() == trace["wall"][0]


def test_import_trace_handles_garbage() -> None:
    """Bad entries in import_trace are silently dropped, NEVER raises."""
    fc = FrozenClock(op_id="op-1")
    fc.import_trace(
        monotonic=[1.0, "garbage", None, 2.0, [1, 2]],  # type: ignore[list-item]
    )
    assert fc.monotonic() == 1.0
    assert fc.monotonic() == 2.0


def test_import_trace_handles_none() -> None:
    fc = FrozenClock(op_id="op-1")
    fc.import_trace()  # all None
    # Empty trace, doesn't raise
    assert fc.monotonic() == 0.0


def test_trace_lengths_diagnostic() -> None:
    rec = RealClock(op_id="op-1", mode=ClockMode.RECORD)
    rec.monotonic()
    rec.monotonic()
    rec.wall_clock()
    sizes = rec.trace_lengths()
    assert sizes["monotonic"] == 2
    assert sizes["wall"] == 1
    assert sizes["sleep"] == 0


# ---------------------------------------------------------------------------
# §15 — NEVER-raises contract
# ---------------------------------------------------------------------------


def test_clock_for_session_garbage_op_id(clean_clock_state) -> None:
    c1 = clock_for_session(op_id="", mode=ClockMode.PASSTHROUGH)
    c2 = clock_for_session(op_id="   ", mode=ClockMode.PASSTHROUGH)
    c3 = clock_for_session(op_id="unknown", mode=ClockMode.PASSTHROUGH)
    assert c1 is c2
    assert c1 is c3


def test_real_clock_continues_after_record_fault(monkeypatch) -> None:
    """If trace recording faults, the time call still returns a real
    value (defensive try/except inside RECORD branch)."""
    c = RealClock(op_id="op-1", mode=ClockMode.RECORD)
    # Patch the trace's append to raise — should NOT propagate
    original_append = c._append_capped

    def boom(lst, val):
        raise RuntimeError("simulated fault")

    c._append_capped = boom  # type: ignore[method-assign]
    try:
        v = c.monotonic()
        assert v > 0  # got a real value despite the fault
    finally:
        c._append_capped = original_append  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# §16 — Authority invariants
# ---------------------------------------------------------------------------


def test_no_orchestrator_imports() -> None:
    """determinism.clock MUST NOT import orchestrator / phase_runner /
    candidate_generator."""
    import inspect
    from backend.core.ouroboros.governance.determinism import clock as ck
    src = inspect.getsource(ck)
    forbidden = (
        "from backend.core.ouroboros.governance.orchestrator",
        "from backend.core.ouroboros.governance.phase_runner",
        "from backend.core.ouroboros.governance.candidate_generator",
    )
    for f in forbidden:
        assert f not in src, f"determinism.clock must NOT contain {f!r}"


# ---------------------------------------------------------------------------
# §17 — Async sleep
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_clock_sleep_actually_blocks() -> None:
    """RealClock.sleep does real asyncio.sleep, not instant."""
    c = RealClock(op_id="op-1", mode=ClockMode.PASSTHROUGH)
    t0 = _time.monotonic()
    await c.sleep(0.05)
    elapsed = _time.monotonic() - t0
    # Should take at least 0.04s (allow for scheduling jitter)
    assert elapsed >= 0.04
