"""Slice 29 — heartbeat plumbing hardening + sentinel forensics.

bt-2026-07-15-233357 post-mortem: `heartbeat.tick` froze at 16:36:00 while
the organism was provably healthy (log flowing, 2 starvation events) until
the ExternalWatchdog SIGKILLed it at 16:51:17 — a FALSE-POSITIVE
heartbeat_stale kill. Two blind spots enabled it:
  1. the wall-clock monitor task (the sentinel's ONLY beat writer) can die
     silently — its ref is held (no GC warning), CancelledError is the only
     handled terminal, and beat() swallowed OSError without a trace;
  2. the kill fired ~915s after the freeze instead of the 120s stale window
     and the sentinel recorded NOTHING about why (suppression? poll drift?).

Fixes under test (root-cause only — stale window and loop cadence untouched):
  * done-callback supervisor: every terminal outcome of the monitor task is
    LOUD; unexpected deaths (any BaseException derivative) restart the task,
    bounded by JARVIS_WALL_CLOCK_MONITOR_MAX_RESTARTS;
  * beat(): rate-limited WARNING on write failure + recovery log + public
    counters — never raises, never silent;
  * sentinel: dynamic delta forensics (format_poll_forensics) emitted on
    stale-suppression, poll drift, beat-aging status, and the kill itself.
"""
from __future__ import annotations

import inspect
import logging
from pathlib import Path

from backend.core.ouroboros.battle_test.harness import (
    BattleTestHarness,
    _should_restart_wall_clock_monitor,
    _wall_clock_monitor_max_restarts,
)
from backend.core.ouroboros.governance.external_watchdog import (
    ExternalProcessWatchdog,
    format_poll_forensics,
    run_watchdog,
)


# ── supervisor decision (pure) ───────────────────────────────────────


def _decide(**kw):
    base = dict(cancelled=False, exc=None, restarts=0, max_restarts=5, closing=False)
    base.update(kw)
    return _should_restart_wall_clock_monitor(**base)


def test_restart_on_unexpected_exception():
    restart, verdict = _decide(exc=RuntimeError("boom"))
    assert restart is True and verdict == "unexpected_death:RuntimeError"


def test_restart_covers_baseexception_derivatives():
    for exc in (SystemExit(1), KeyboardInterrupt(), MemoryError()):
        restart, verdict = _decide(exc=exc)
        assert restart is True
        assert type(exc).__name__ in verdict


def test_no_restart_on_cancellation():
    assert _decide(cancelled=True) == (False, "cancelled")


def test_no_restart_on_normal_return():
    assert _decide() == (False, "normal_return")


def test_no_restart_during_teardown():
    restart, verdict = _decide(exc=RuntimeError("x"), closing=True)
    assert restart is False and verdict == "closing"


def test_restart_budget_bounds_thrash():
    restart, verdict = _decide(exc=RuntimeError("x"), restarts=5, max_restarts=5)
    assert restart is False and verdict.startswith("restart_budget_exhausted")


def test_max_restarts_env_driven(monkeypatch):
    monkeypatch.setenv("JARVIS_WALL_CLOCK_MONITOR_MAX_RESTARTS", "2")
    assert _wall_clock_monitor_max_restarts() == 2
    monkeypatch.setenv("JARVIS_WALL_CLOCK_MONITOR_MAX_RESTARTS", "junk")
    assert _wall_clock_monitor_max_restarts() == 5


def test_supervisor_wired_into_arming():
    src = inspect.getsource(
        BattleTestHarness._arm_wall_clock_monitor_supervised,
    )
    assert "add_done_callback" in src
    assert "_on_wall_clock_monitor_done" in src
    done_src = inspect.getsource(BattleTestHarness._on_wall_clock_monitor_done)
    assert "BaseException" in done_src
    assert "_should_restart_wall_clock_monitor" in done_src
    assert "critical" in done_src  # unexpected death is LOUD


# ── beat() visibility ────────────────────────────────────────────────


def _make_wd(tmp_path: Path) -> ExternalProcessWatchdog:
    return ExternalProcessWatchdog(
        target_pid=1, heartbeat_path=tmp_path / "hb.tick",
        budget_s=100.0, stale_window_s=10.0,
    )


def test_beat_failure_warns_and_counts(tmp_path, caplog):
    wd = _make_wd(tmp_path)
    # Force the atomic replace to fail: make the heartbeat path a DIRECTORY.
    (tmp_path / "hb.tick").mkdir()
    with caplog.at_level(logging.WARNING, logger="Ouroboros.ExternalWatchdog"):
        wd.beat()
    assert wd.beat_failures == 1
    assert any("heartbeat WRITE FAILED" in r.message for r in caplog.records)


def test_beat_failure_rate_limited(tmp_path, caplog):
    wd = _make_wd(tmp_path)
    (tmp_path / "hb.tick").mkdir()
    with caplog.at_level(logging.WARNING, logger="Ouroboros.ExternalWatchdog"):
        for _ in range(12):
            wd.beat()
    warns = [r for r in caplog.records if "WRITE FAILED" in r.message]
    # 1st + every 10th (10th failure) — NOT one per call.
    assert len(warns) == 2
    assert wd.beat_failures == 12


def test_beat_recovery_logged(tmp_path, caplog):
    wd = _make_wd(tmp_path)
    (tmp_path / "hb.tick").mkdir()
    with caplog.at_level(logging.WARNING, logger="Ouroboros.ExternalWatchdog"):
        wd.beat()
        (tmp_path / "hb.tick").rmdir()
        wd.beat()
    assert any("RECOVERED" in r.message for r in caplog.records)
    assert wd.beat_successes == 1
    assert wd._beat_consecutive_failures == 0


def test_beat_never_raises(tmp_path):
    wd = _make_wd(tmp_path)
    (tmp_path / "hb.tick").mkdir()
    wd.beat()  # must not raise despite the OSError inside


# ── sentinel forensics ───────────────────────────────────────────────


def test_forensics_line_is_fully_dynamic():
    line = format_poll_forensics(
        tag="stale-suppressed", now_wall=1000.0, armed_wall=100.0,
        last_beat_wall=850.0, stale_window_s=120.0, budget_s=2490.0,
        suspended=True, wall_delta=31.0, mono_delta=1.0, poll_s=1.0,
        suppressed_polls=7, max_poll_drift_s=30.0,
    )
    # Every number in the line is a computed delta — the exact math the
    # bt-233357 post-mortem lacked.
    assert "beat_age_s=150.0" in line
    assert "budget_elapsed_s=900.0/2490" in line
    assert "suspended=True" in line
    assert "poll_drift_s=30.00" in line
    assert "suppressed_polls=7" in line
    assert "stale-suppressed" in line


def test_forensics_handles_missing_beat():
    line = format_poll_forensics(
        tag="beat-aging", now_wall=1000.0, armed_wall=100.0,
        last_beat_wall=None, stale_window_s=120.0, budget_s=2490.0,
        suspended=False, wall_delta=1.0, mono_delta=1.0, poll_s=1.0,
        suppressed_polls=0, max_poll_drift_s=0.0,
    )
    assert "beat_age_s=-1.0" in line  # sentinel could not read the beat


def test_sentinel_emits_forensics_at_the_blind_spots():
    src = inspect.getsource(run_watchdog)
    assert "stale-suppressed" in src   # staleness true but suppressed
    assert "poll-drift" in src         # child scheduling drift (App Nap class)
    assert "beat-aging" in src         # periodic status while beat ages
    assert '"kill:" + reason' in src   # full math on the kill itself
    # Root-cause mandate: the decision function itself is untouched —
    # forensics observe, never alter, the kill math.
    assert "evaluate_kill(" in src


def test_evaluate_kill_untouched_regression():
    """The Slice 49 pure decision contract is byte-level intact — Slice 29
    adds observation only (no widened windows, no new suppression)."""
    from backend.core.ouroboros.governance.external_watchdog import (
        evaluate_kill,
    )
    kill, reason = evaluate_kill(
        now_wall=1000.0, armed_wall=900.0, last_beat_wall=800.0,
        budget_s=10_000.0, stale_window_s=120.0, suspended=False,
    )
    assert (kill, reason) == (True, "heartbeat_stale")
    kill, reason = evaluate_kill(
        now_wall=1000.0, armed_wall=900.0, last_beat_wall=800.0,
        budget_s=10_000.0, stale_window_s=120.0, suspended=True,
    )
    assert kill is False
