"""Ignition: the Sentinel loop is actually started, stopped, and streamed.

A loop nobody starts is this repo's most expensive failure shape — the engine
was built, tested and unreachable. These tests pin the wiring itself: the boot
call, the shutdown call, the ordering of both, and the fact that the autonomous
stream reaches the cockpit through the one rendering chokepoint rather than a
second path of its own.
"""
from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest

from backend.core.ouroboros.battle_test.harness import BattleTestHarness

LAUNCHER = Path(__file__).resolve().parents[2] / "scripts" / "soaks" / "cockpit_interactive.sh"


# --------------------------------------------------------------------------
# Reachability — the engine must be ignited by the boot sequence
# --------------------------------------------------------------------------

def test_boot_starts_the_sentinel_loop():
    src = inspect.getsource(BattleTestHarness)
    assert "await self._start_sentinel_loop()" in src, (
        "the loop is never started — a built, tested, unreachable engine"
    )


def test_shutdown_stops_the_sentinel_loop():
    src = inspect.getsource(BattleTestHarness._shutdown_components)
    assert "_stop_sentinel_loop" in src, "the loop outlives the organism"


def test_the_loop_is_ignited_after_intake():
    """It dispatches THROUGH intake. Starting earlier would have it discover
    work it cannot submit, then cool the target for a failure that was never
    the target's."""
    src = inspect.getsource(BattleTestHarness)
    assert src.index("boot_intake()") < src.index("await self._start_sentinel_loop()")


def test_the_loop_is_stopped_before_the_rest_of_teardown():
    """A loop still discovering while intake closes files goals that cannot
    be submitted."""
    src = inspect.getsource(BattleTestHarness._shutdown_components)
    assert src.index("_stop_sentinel_loop") < src.index("_disarm_external_watchdog")


# --------------------------------------------------------------------------
# Both switches, never one
# --------------------------------------------------------------------------

def _harness_stub():
    """A bare object carrying only the methods under test."""

    class _Stub:
        _repl_print = staticmethod(lambda msg: None)
        _inject_sanctioned_goal = staticmethod(lambda **kw: "op-x")

        class _Cfg:
            repo_path = Path(".")

        _config = _Cfg()

    stub = _Stub()
    stub._start_sentinel_loop = BattleTestHarness._start_sentinel_loop.__get__(stub)
    stub._stop_sentinel_loop = BattleTestHarness._stop_sentinel_loop.__get__(stub)
    stub._resolve_test_watcher = BattleTestHarness._resolve_test_watcher.__get__(stub)
    stub._render_sentinel_outcome = BattleTestHarness._render_sentinel_outcome.__get__(stub)
    return stub


@pytest.mark.parametrize(
    "sentinel,discovery",
    [("false", "false"), ("true", "false"), ("false", "true")],
)
def test_one_switch_alone_never_arms(monkeypatch, sentinel, discovery):
    """They are separable capabilities; unattended application of
    self-authored code is the COMPOSITION. One variable must not buy it."""
    monkeypatch.setenv("JARVIS_SENTINEL_MODE_ENABLED", sentinel)
    monkeypatch.setenv("JARVIS_GOAL_DISCOVERY_ENABLED", discovery)
    stub = _harness_stub()
    asyncio.run(stub._start_sentinel_loop())
    assert getattr(stub, "_sentinel_loop", None) is None


def test_both_switches_arm_the_loop(monkeypatch):
    monkeypatch.setenv("JARVIS_SENTINEL_MODE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_GOAL_DISCOVERY_ENABLED", "true")
    monkeypatch.setenv("JARVIS_SENTINEL_INTERVAL_S", "3600")   # keep it idle
    stub = _harness_stub()
    try:
        asyncio.run(stub._start_sentinel_loop())
        assert getattr(stub, "_sentinel_loop", None) is not None
    finally:
        asyncio.run(stub._stop_sentinel_loop())
        assert getattr(stub, "_sentinel_loop", None) is None


def test_ignition_failure_never_stops_the_organism(monkeypatch):
    """A dead sentinel is a degraded organism, not a dead one."""
    monkeypatch.setenv("JARVIS_SENTINEL_MODE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_GOAL_DISCOVERY_ENABLED", "true")
    stub = _harness_stub()
    stub._config = None                     # break it on purpose
    asyncio.run(stub._start_sentinel_loop())   # must not raise
    assert getattr(stub, "_sentinel_loop", None) is None


def test_stopping_an_unstarted_loop_is_a_no_op():
    stub = _harness_stub()
    asyncio.run(stub._stop_sentinel_loop())    # must not raise


# --------------------------------------------------------------------------
# The stream reaches the cockpit through the ONE chokepoint
# --------------------------------------------------------------------------

def test_outcomes_render_through_the_design_language_chokepoint():
    src = inspect.getsource(BattleTestHarness._render_sentinel_outcome)
    assert "self._repl_print" in src, (
        "a second rendering path would make the organism's own work look "
        "foreign in its own cockpit"
    )


def test_every_outcome_state_renders():
    from backend.core.ouroboros.governance.autonomy.sentinel_loop import PassOutcome

    printed = []
    stub = _harness_stub()
    stub._repl_print = printed.append
    stub._render_sentinel_outcome = BattleTestHarness._render_sentinel_outcome.__get__(stub)
    for state in ("landed", "failed", "timed_out", "refused", "idle", "??"):
        stub._render_sentinel_outcome(
            PassOutcome(state, "backend/a.py", "g", "op", "detail", 4.0)
        )
    assert len(printed) == 6
    assert any("landed" in p for p in printed)


def test_a_broken_outcome_object_does_not_break_the_view():
    stub = _harness_stub()
    stub._render_sentinel_outcome(object())    # must not raise


def test_a_failing_console_does_not_break_the_loop():
    def boom(_msg):
        raise RuntimeError("console gone")

    stub = _harness_stub()
    stub._repl_print = boom
    stub._render_sentinel_outcome = BattleTestHarness._render_sentinel_outcome.__get__(stub)
    from backend.core.ouroboros.governance.autonomy.sentinel_loop import PassOutcome
    stub._render_sentinel_outcome(PassOutcome("landed"))   # must not raise


# --------------------------------------------------------------------------
# The launcher
# --------------------------------------------------------------------------

def test_the_injected_envelope_declares_an_urgency():
    """Urgency decides the LANE. Unset, a source="roadmap" envelope goes down
    the background lane to DoubleWord, which on this host is blocked by
    topology (no cloud credit) — so the op dies
    `background_dw_blocked_by_topology` AFTER passing provenance, PLAN and
    every gate. The PRD records the same trap costing "every roadmap op in
    five soaks", because UrgencyRouter keys on SOURCE, not urgency."""
    src = inspect.getsource(BattleTestHarness._inject_sanctioned_goal)
    assert "urgency=" in src, (
        "the envelope declares no urgency — it will be routed to the blocked "
        "background lane"
    )
    assert "JARVIS_WORK_ORDER_DEFAULT_URGENCY" in src, (
        "urgency must come from the same knob WorkOrderSensor reads, or the "
        "two lanes will disagree about the operator's policy"
    )


def test_the_launcher_requires_an_explicit_flag():
    if not LAUNCHER.exists():  # pragma: no cover
        pytest.skip("launcher not present")
    text = LAUNCHER.read_text(encoding="utf-8")
    assert "--sentinel" in text
    # Arming must be INSIDE the flag branch, never unconditional.
    idx = text.index('if [ "$SENTINEL" = "1" ]')
    tail = text[idx:idx + 400]
    assert "JARVIS_SENTINEL_MODE_ENABLED=true" in tail
    assert "JARVIS_GOAL_DISCOVERY_ENABLED=true" in tail


def test_help_answers_without_a_terminal():
    """The user asking how to invoke the launcher is exactly the user who has
    not yet arranged a TTY."""
    if not LAUNCHER.exists():  # pragma: no cover
        pytest.skip("launcher not present")
    text = LAUNCHER.read_text(encoding="utf-8")
    # Anchor on the GUARD, not the prose describing it — the header comment
    # also says "not a TTY", and a test that cannot tell code from its own
    # documentation fails on the documentation.
    assert text.index("--help|-h") < text.index("[ -t 0 ]")
