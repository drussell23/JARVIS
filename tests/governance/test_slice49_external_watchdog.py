"""Slice 49 Phase 2 — external subprocess watchdog (GIL-immune kill path).

v44 wedged 73min past a 40min cap because the in-process resource-zero
watchdog (a Python thread) never fired. The fix: an OUT-OF-PROCESS sentinel
that SIGKILLs the parent from outside the interpreter entirely — it cannot
be GIL-starved by construction.

Design:
  * parent `beat()`s a wall timestamp into a heartbeat file each loop tick
  * the child polls the file with its OWN clocks
  * BUDGET kill is wall-authoritative (fires at --max-wall-seconds regardless
    of GIL — this is what should have killed v44)
  * STALENESS kill is suspend-aware: a host sleep (wall jumps but the child's
    monotonic barely moves) must NOT be mistaken for a wedge (Slice 46 lesson)

Pins:
  §1  budget exceeded → kill (wall-authoritative)
  §2  stale heartbeat (real wedge) → kill
  §3  suspend interval (wall>>monotonic) → NO staleness kill
  §4  fresh heartbeat within window → no kill
  §5  suspend detector: wall-monotonic divergence past threshold
  §6  beat() writes a parseable wall timestamp atomically
  §7  END-TO-END: an armed watchdog actually SIGKILLs a stalled victim process
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.external_watchdog import (
    ExternalProcessWatchdog,
    _suspend_detected,
    evaluate_kill,
)
from backend.core.ouroboros.governance import external_watchdog as ew


# ── §1 budget (wall-authoritative) ──────────────────────────────────────
def test_budget_exceeded_kills() -> None:
    kill, reason = evaluate_kill(
        now_wall=1000.0, armed_wall=900.0, last_beat_wall=999.0,
        budget_s=60.0, stale_window_s=30.0, suspended=False,
    )
    assert kill and reason == "wall_budget_exceeded"


# ── §2 stale heartbeat (real wedge) ─────────────────────────────────────
def test_stale_heartbeat_kills() -> None:
    kill, reason = evaluate_kill(
        now_wall=1000.0, armed_wall=995.0, last_beat_wall=900.0,
        budget_s=3600.0, stale_window_s=30.0, suspended=False,
    )
    assert kill and reason == "heartbeat_stale"


# ── §3 suspend must NOT trigger a staleness kill ────────────────────────
def test_suspend_does_not_kill_on_staleness() -> None:
    kill, _ = evaluate_kill(
        now_wall=1000.0, armed_wall=995.0, last_beat_wall=900.0,
        budget_s=3600.0, stale_window_s=30.0, suspended=True,
    )
    assert kill is False


# ── §4 fresh heartbeat ──────────────────────────────────────────────────
def test_fresh_heartbeat_no_kill() -> None:
    kill, _ = evaluate_kill(
        now_wall=1000.0, armed_wall=995.0, last_beat_wall=998.0,
        budget_s=3600.0, stale_window_s=30.0, suspended=False,
    )
    assert kill is False


# ── §5 suspend detector ─────────────────────────────────────────────────
def test_suspend_detector() -> None:
    # wall jumped 300s but monotonic only moved 2s → host slept
    assert _suspend_detected(mono_delta=2.0, wall_delta=300.0, threshold=5.0)
    # both advanced ~equally → genuine elapsed time (wedge candidate)
    assert not _suspend_detected(mono_delta=29.0, wall_delta=30.0, threshold=5.0)


# ── §6 beat() writes parseable timestamp ────────────────────────────────
def test_beat_writes_wall_timestamp(tmp_path: Path) -> None:
    hb = tmp_path / "heartbeat.tick"
    wd = ExternalProcessWatchdog(
        target_pid=99999, heartbeat_path=hb, budget_s=60.0, stale_window_s=30.0,
    )
    before = time.time()
    wd.beat()
    val = float(hb.read_text().strip())
    assert before - 1.0 <= val <= time.time() + 1.0


# ── §7 END-TO-END: real subprocess kill of a stalled victim ─────────────
@pytest.mark.timeout(30)
def test_watchdog_kills_stalled_victim(tmp_path: Path) -> None:
    victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        hb = tmp_path / "heartbeat.tick"
        wd = ExternalProcessWatchdog(
            target_pid=victim.pid, heartbeat_path=hb,
            budget_s=1.5, stale_window_s=1.5, poll_s=0.25,
        )
        wd.arm()  # spawns the external sentinel; never beats → budget fires
        # poll for the victim to die
        deadline = time.time() + 12.0
        while time.time() < deadline and victim.poll() is None:
            time.sleep(0.25)
        assert victim.poll() is not None, "watchdog did not kill the stalled victim"
        wd.disarm()
    finally:
        if victim.poll() is None:
            victim.kill()
            victim.wait(timeout=5)


# ── §8 harness wiring pin (arm at boot / beat in monitor / disarm on stop) ─
def test_harness_wires_external_watchdog() -> None:
    import backend.core.ouroboros.battle_test.harness as _h
    src = Path(_h.__file__).read_text()
    # arm at boot — right after the in-process hard-deadline thread
    assert "_arm_external_watchdog(_wall_cap)" in src
    # beat each wall-clock monitor tick
    assert "self._beat_external_watchdog()" in src
    # disarm at clean shutdown
    assert "self._disarm_external_watchdog()" in src
    # the three methods exist on the harness class
    for m in (
        "_arm_external_watchdog", "_beat_external_watchdog",
        "_disarm_external_watchdog",
    ):
        assert hasattr(_h.BattleTestHarness, m), f"missing {m}"


# ---------------------------------------------------------------------------
# A transient read failure must not kill a healthy organism
# ---------------------------------------------------------------------------


def test_one_unreadable_poll_does_not_kill_a_beating_parent(tmp_path) -> None:
    """THE regression: soak bt-2026-09-01-173444.

    A healthy 44-minute soak was SIGKILLed on its FIRST failed beat read,
    4ms after a successful write, with ZERO write failures. The fallback
    went straight to ``armed_wall``, so one unreadable poll computed an age
    equal to the whole session (2645s) against a 120s window. SIGKILL is
    uncatchable, so teardown never ran and the trajectory corpus was lost.

    The heartbeat lives on a 9p mount where ``os.replace`` is not reliably
    atomic however correct the writer is -- so the sentinel cannot assume
    every read succeeds, and must fail SAFE when one does not.
    """
    armed, stale, budget = 1000.0, 120.0, 3690.0
    now = armed + 2645.0
    fresh = now - 0.004                      # the beat really was 4ms old

    # what the old fallback did
    kill_old, reason_old = ew.evaluate_kill(
        now_wall=now, armed_wall=armed, last_beat_wall=armed,
        budget_s=budget, stale_window_s=stale, suspended=False,
    )
    assert kill_old and reason_old == "heartbeat_stale"

    # what a remembered good beat does
    kill_new, _ = ew.evaluate_kill(
        now_wall=now, armed_wall=armed, last_beat_wall=fresh,
        budget_s=budget, stale_window_s=stale, suspended=False,
    )
    assert kill_new is False


def test_a_genuinely_hung_parent_still_dies(tmp_path) -> None:
    """Fail-safe must not become fail-never.

    If reads keep failing AND the last good beat ages past the window, the
    kill fires on its own -- the remembered beat is a fact with a timestamp,
    not an amnesty.
    """
    armed, stale, budget = 1000.0, 120.0, 3690.0
    now = armed + 2645.0
    kill, reason = ew.evaluate_kill(
        now_wall=now, armed_wall=armed, last_beat_wall=now - 600.0,
        budget_s=budget, stale_window_s=stale, suspended=False,
    )
    assert kill and reason == "heartbeat_stale"


def test_a_parent_that_never_beat_is_still_governed_by_budget() -> None:
    """`armed_wall` remains the fallback for a parent with NO beat ever.

    That is what the original fallback was actually for, and it is kept.
    """
    armed, stale, budget = 1000.0, 120.0, 3690.0
    kill, reason = ew.evaluate_kill(
        now_wall=armed + budget + 1, armed_wall=armed, last_beat_wall=armed,
        budget_s=budget, stale_window_s=stale, suspended=False,
    )
    assert kill and reason == "wall_budget_exceeded"


def test_read_beat_ex_reports_why_it_failed(tmp_path) -> None:
    """The reason must survive: a read failure and a hang look identical
    from the value alone, and only one of them warrants a kill."""
    missing = tmp_path / "nope.tick"
    val, exc = ew._read_beat_ex(str(missing))
    assert val is None and isinstance(exc, OSError)

    torn = tmp_path / "torn.tick"
    torn.write_text("")                       # mid-write shape
    val, exc = ew._read_beat_ex(str(torn))
    assert val is None and isinstance(exc, ValueError)

    good = tmp_path / "good.tick"
    good.write_text(repr(1234.5))
    assert ew._read_beat_ex(str(good)) == (1234.5, None)


def test_read_beat_wrapper_keeps_its_optional_float_shape(tmp_path) -> None:
    """Existing callers and tests depend on the narrow shape."""
    good = tmp_path / "g.tick"
    good.write_text(repr(99.5))
    assert ew._read_beat(str(good)) == 99.5
    assert ew._read_beat(str(tmp_path / "absent.tick")) is None
