"""Regression spine for the A1 soak-child termination coordination.

Root cause (bt-iso-1783144982, 2026-07-04): the isomorphic A1 driver audited
the soak child's debug.log BEFORE the child had actually terminated, scoring a
spurious FAILED while 5/6 real autonomy criteria had passed (the child's own
watchdog heartbeat read remaining=53s at audit time).

Two defeated approaches both tried to GUESS the child's wall anchor:
  * v1: flat ``cap + 30`` anchored at exit-wait START.
  * v2: ``launch + boot_ceiling(90) + cap + max_margin`` -- but the boot->arm
    skew is unbounded (observed 168s: a ~72s ShippedCodeInvariants boot
    validation armed the watchdog long after boot-READY), so a hardcoded 90s
    ceiling is empirically false.

ROOT-CAUSE FIX: the harness PUBLISHES its absolute wall deadline to
``<session_dir>/wall_deadline.json`` at arm time; the parent CONSUMES it. The
parent's exit deadline = ``published_deadline_wall + max_kill_margin + slack``,
which is correct for ANY boot duration because it is anchored to the child's
OWN published deadline -- no boot-ceiling assumption. These tests pin that
invariant (incl. the boot-time-INDEPENDENCE the v2 ceiling lacked), the
env-knob single-source-of-truth, the file read, and the adaptive
termination-wait coroutine.
"""
from __future__ import annotations

import asyncio
import importlib
import json

iso = importlib.import_module("scripts.isomorphic_a1_local")


# Harness defaults (backend/core/ouroboros/battle_test/harness.py):
#  - in-process ladder (_start_wall_clock_hard_deadline_thread): grace 5..600
#    default 30; margin 5..300 default 30.
#  - external sentinel (_arm_external_watchdog): margin floor 30 / default 90,
#    WALL-authoritative -- the LATEST guaranteed kill (90 > 30+30 by default).
_HARNESS_GRACE_DEFAULT = 30.0
_HARNESS_MARGIN_DEFAULT = 30.0
_HARNESS_EXTERNAL_MARGIN_DEFAULT = 90.0


def _clear_all_knobs(monkeypatch):
    for k in (
        "JARVIS_WALL_CLOCK_HARD_DEADLINE_GRACE_S",
        "JARVIS_WALL_CLOCK_HARD_KILL_MARGIN_S",
        "JARVIS_EXTERNAL_WATCHDOG_MARGIN_S",
        "JARVIS_EXTERNAL_WATCHDOG_ENABLED",
        "JARVIS_A1_SOAK_EXIT_SLACK_S",
        "JARVIS_A1_SOAK_ARM_CEILING_S",
        "JARVIS_A1_SOAK_TERM_POLL_S",
    ):
        monkeypatch.delenv(k, raising=False)


# ── margin helpers: single source of truth with the harness ──────────────

def test_shared_env_knob_readers_match_harness_defaults_and_bounds(monkeypatch):
    _clear_all_knobs(monkeypatch)
    assert iso._child_hard_deadline_grace_s() == _HARNESS_GRACE_DEFAULT
    assert iso._child_hard_kill_margin_s() == _HARNESS_MARGIN_DEFAULT
    assert iso._child_external_watchdog_margin_s() == _HARNESS_EXTERNAL_MARGIN_DEFAULT
    # max_kill_margin picks the LATER layer (external 90 > in-process 60).
    assert iso._child_max_kill_margin_s() == 90.0
    # Floor/ceiling clamps mirror the harness.
    monkeypatch.setenv("JARVIS_WALL_CLOCK_HARD_DEADLINE_GRACE_S", "1")
    monkeypatch.setenv("JARVIS_WALL_CLOCK_HARD_KILL_MARGIN_S", "9999")
    monkeypatch.setenv("JARVIS_EXTERNAL_WATCHDOG_MARGIN_S", "1")
    assert iso._child_hard_deadline_grace_s() == 5.0
    assert iso._child_hard_kill_margin_s() == 300.0
    assert iso._child_external_watchdog_margin_s() == 30.0
    # in-process ladder now dominates (5+300=305 > external 30).
    assert iso._child_max_kill_margin_s() == 305.0
    # External sentinel disabled -> 0 contribution (mirrors _arm_external_watchdog).
    monkeypatch.setenv("JARVIS_EXTERNAL_WATCHDOG_ENABLED", "false")
    assert iso._child_external_watchdog_margin_s() == 0.0
    # Garbage -> default, never raises.
    monkeypatch.setenv("JARVIS_WALL_CLOCK_HARD_DEADLINE_GRACE_S", "not-a-number")
    assert iso._child_hard_deadline_grace_s() == _HARNESS_GRACE_DEFAULT


# ── the core invariant: parent deadline covers the child's guaranteed kill ─

def test_parent_deadline_covers_child_guaranteed_kill(monkeypatch):
    _clear_all_knobs(monkeypatch)
    # Child published its wall-cap deadline (armed_wall + cap). Its guaranteed
    # death is that + max_kill_margin (the external sentinel at +90 by default).
    child_deadline_wall = 1_000_000.0
    child_guaranteed_death = child_deadline_wall + _HARNESS_EXTERNAL_MARGIN_DEFAULT
    parent_deadline = iso._parent_exit_deadline_wall(child_deadline_wall)
    assert parent_deadline >= child_guaranteed_death + 5.0, (
        "parent must outlast the child's guaranteed death, with slack")


def test_parent_deadline_is_boot_time_INDEPENDENT(monkeypatch):
    # THE crux the v2 boot-ceiling missed: because the parent anchors to the
    # child's OWN published deadline (not a launch+ceiling guess), the invariant
    # holds for ANY boot->arm skew -- including the 168s that broke v2, and a
    # pathological 900s cold boot. armed_wall = launch + boot_skew; the child
    # publishes deadline_wall = armed_wall + cap; parent covers it regardless.
    _clear_all_knobs(monkeypatch)
    launch_wall = 1_000_000.0
    cap = 3600.0
    for boot_skew in (5.0, 90.0, 168.0, 500.0, 900.0):
        armed_wall = launch_wall + boot_skew
        published_deadline_wall = armed_wall + cap
        child_guaranteed_death = (
            published_deadline_wall + _HARNESS_EXTERNAL_MARGIN_DEFAULT)
        parent_deadline = iso._parent_exit_deadline_wall(published_deadline_wall)
        assert parent_deadline >= child_guaranteed_death + 5.0, (
            "invariant must hold at boot_skew=%.0fs (v2's 90s ceiling failed "
            "at 168s)" % boot_skew)


def test_parent_deadline_tracks_env_knobs(monkeypatch):
    # Widen EITHER ladder -> the parent's deadline grows with the max.
    _clear_all_knobs(monkeypatch)
    monkeypatch.setenv("JARVIS_WALL_CLOCK_HARD_DEADLINE_GRACE_S", "120")
    monkeypatch.setenv("JARVIS_WALL_CLOCK_HARD_KILL_MARGIN_S", "90")   # in-proc 210
    monkeypatch.setenv("JARVIS_EXTERNAL_WATCHDOG_MARGIN_S", "150")     # external 150
    monkeypatch.setenv("JARVIS_A1_SOAK_EXIT_SLACK_S", "20")
    child_deadline_wall = 1_000_000.0
    # max(210, 150) = 210; parent = deadline + 210 + 20.
    assert iso._parent_exit_deadline_wall(child_deadline_wall) == (
        child_deadline_wall + 210.0 + 20.0)


# ── the published-deadline file read ─────────────────────────────────────

def test_read_published_wall_deadline_roundtrip(tmp_path):
    (tmp_path / "wall_deadline.json").write_text(json.dumps({
        "armed_wall": 1_000_000.0, "cap_s": 3600.0,
        "deadline_wall": 1_003_600.0}))
    assert iso._read_published_wall_deadline(str(tmp_path)) == 1_003_600.0


def test_read_published_wall_deadline_missing_or_garbage(tmp_path):
    # Missing file -> None (child not armed yet).
    assert iso._read_published_wall_deadline(str(tmp_path)) is None
    # Garbage -> None, never raises.
    (tmp_path / "wall_deadline.json").write_text("{not json")
    assert iso._read_published_wall_deadline(str(tmp_path)) is None
    # Present but no deadline_wall key -> None.
    (tmp_path / "wall_deadline.json").write_text(json.dumps({"cap_s": 10}))
    assert iso._read_published_wall_deadline(str(tmp_path)) is None


# ── the adaptive termination-wait coroutine ──────────────────────────────

class _FakeProc:
    """Minimal Popen stand-in: alive for `alive_polls` poll() calls, then rc."""
    def __init__(self, alive_polls: int, rc: int = 0):
        self._alive_polls = alive_polls
        self._rc = rc
        self.returncode = None

    def poll(self):
        if self._alive_polls > 0:
            self._alive_polls -= 1
            return None
        self.returncode = self._rc
        return self._rc


def test_termination_wait_returns_exited_when_child_exits(tmp_path, monkeypatch):
    _clear_all_knobs(monkeypatch)
    monkeypatch.setenv("JARVIS_A1_SOAK_TERM_POLL_S", "1")  # floored to 1s
    debug_log = str(tmp_path / "debug.log")
    open(debug_log, "w").close()
    proc = _FakeProc(alive_polls=2, rc=0)  # exits on the 3rd poll
    status = asyncio.get_event_loop().run_until_complete(
        iso._await_soak_child_termination(proc, debug_log, launch_monotonic=None))
    assert status == "exited"


def test_termination_wait_force_reaps_past_published_deadline(tmp_path, monkeypatch):
    # Child NEVER exits, and its published deadline is already in the past ->
    # the coroutine must force-reap (not hang, not audit-truncated-silently).
    _clear_all_knobs(monkeypatch)
    monkeypatch.setenv("JARVIS_A1_SOAK_TERM_POLL_S", "1")
    debug_log = str(tmp_path / "debug.log")
    open(debug_log, "w").close()
    # deadline_wall far in the past -> _parent_exit_deadline_wall already elapsed.
    (tmp_path / "wall_deadline.json").write_text(json.dumps({
        "deadline_wall": 1.0}))  # epoch 1970 -> long past
    proc = _FakeProc(alive_polls=10_000, rc=0)  # never exits within the test
    status = asyncio.get_event_loop().run_until_complete(
        iso._await_soak_child_termination(proc, debug_log, launch_monotonic=None))
    assert status == "force_reaped"
