"""The loading percentage is a measurement of the boot, not a guess."""
from __future__ import annotations

import json

from backend.core.boot_timeline import BootTimeline


def _learn(store, milestones, total):
    store.write_text(json.dumps({"boots": 1, "total": total, "milestones": milestones}))


def test_first_boot_approaches_but_never_reaches_the_end(tmp_path):
    t = BootTimeline(origin=0.0, store=tmp_path / "t.json")
    assert t.learning and t.percent(now=5) == 0
    t.phase_started("preflight", 1)
    t.phase_ended("preflight", 2)
    assert 0 < t.percent(now=3) < 100
    for i in range(500):
        t.phase_started(f"p{i}", 3)
    assert t.percent(now=4) == 99


def test_position_on_the_learned_timeline(tmp_path):
    store = tmp_path / "t.json"
    _learn(store, {"a:start": 0, "a:end": 20, "b:start": 20, "b:end": 100}, total=100)
    t = BootTimeline(origin=0.0, store=store)
    t.phase_started("a", 0)
    assert t.percent(now=10) == 10            # halfway through the learned gap
    t.phase_ended("a", 20)
    t.phase_started("b", 20)
    assert t.percent(now=60) == 60


def test_a_slow_boot_holds_at_the_milestone_it_has_not_reached(tmp_path):
    store = tmp_path / "t.json"
    _learn(store, {"a:start": 0, "a:end": 20, "b:start": 20, "b:end": 50}, total=100)
    t = BootTimeline(origin=0.0, store=store)
    t.phase_started("a", 0)
    assert t.percent(now=500) == 20           # "a" has not ended: never past it


def test_one_hundred_belongs_to_completion_alone(tmp_path):
    store = tmp_path / "t.json"
    _learn(store, {"a:start": 0, "a:end": 10}, total=20)
    t = BootTimeline(origin=0.0, store=store)
    t.phase_started("a", 0)
    t.phase_ended("a", 10)
    assert t.percent(now=10_000) == 99
    t.completed(30)
    assert t.percent(now=30) == 100


def test_completion_teaches_the_next_boot(tmp_path):
    store = tmp_path / "t.json"
    t = BootTimeline(origin=100.0, store=store)
    t.phase_started("a", 100)
    t.phase_ended("a", 160)
    t.completed(200)
    learned = json.loads(store.read_text())
    assert learned["total"] == 100 and learned["milestones"]["a:end"] == 60
    nxt = BootTimeline(origin=0.0, store=store)
    assert not nxt.learning and nxt.remaining_seconds(now=40) == 60


def test_a_phase_finishing_early_cannot_carry_the_bar_past_one_still_running(tmp_path):
    # Live 08:51: parallel phases finished out of the learned order and the
    # page went 33% -> 57% in two seconds while earlier work still ran.
    store = tmp_path / "t.json"
    _learn(store, {"a:start": 0, "a:end": 50, "b:start": 0, "b:end": 90}, total=100)
    t = BootTimeline(origin=0.0, store=store)
    t.phase_started("a", 0)
    t.phase_started("b", 0)
    t.phase_ended("b", 10)                     # b done early; a still running
    assert t.percent(now=11) <= 50
    assert t.percent(now=500) == 50            # holds at a's learned end
    t.phase_ended("a", 501)
    assert t.percent(now=501) == 90            # caught up once a finished


def test_a_phase_never_started_does_not_freeze_the_bar(tmp_path):
    store = tmp_path / "t.json"
    _learn(store, {"a:start": 0, "a:end": 20, "skipped:start": 20, "skipped:end": 40,
                   "c:start": 40, "c:end": 80}, total=100)
    t = BootTimeline(origin=0.0, store=store)
    t.phase_started("a", 0)
    t.phase_ended("a", 20)
    t.phase_started("c", 21)
    assert t.percent(now=60) > 40              # "skipped" never ran: no hold there


def test_a_late_starting_phase_holds_the_bar_instead_of_rolling_it_back(tmp_path):
    store = tmp_path / "t.json"
    _learn(store, {"a:start": 0, "a:end": 60, "late:start": 10, "late:end": 30}, total=100)
    t = BootTimeline(origin=0.0, store=store)
    t.phase_started("a", 0)
    t.phase_ended("a", 60)
    shown = t.percent(now=61)
    t.phase_started("late", 62)                # caps at 30 now, but never goes back
    assert t.percent(now=63) == shown


def test_the_page_shows_the_timeline_not_a_literal_written_elsewhere(tmp_path):
    # Boot of 2026-10-14 09:35: _current_progress was set to 57 when
    # intelligence ended and the page showed max(that, timeline): 37% -> 57%
    # in an instant, then flat for 27 s.
    import types
    import unified_supervisor as us

    store = tmp_path / "t.json"
    _learn(store, {"a:start": 0, "a:end": 37, "b:start": 37, "b:end": 100}, total=100)
    import time
    origin = time.time() - 37                      # 37 s into a boot, right now
    timeline = BootTimeline(origin=origin, store=store)
    timeline.phase_started("a", origin)
    timeline.phase_ended("a", origin + 37)
    k = types.SimpleNamespace(_boot_timeline=timeline, _current_progress=57)
    measured = us.JarvisSystemKernel._measured_boot_percent(k)
    assert measured == timeline.percent() and measured < 57


# The loading page's phase row is drawn from this (2026-10-15: it was nine
# fixed dots for J-Prime's former Python server's phases and never lit).
def test_the_snapshot_is_the_boots_phases_done_running_and_to_come(tmp_path):
    store = tmp_path / "t.json"
    _learn(store, {"clean_slate:start": 0, "clean_slate:end": 8, "preflight:start": 9,
                   "preflight:end": 25, "trinity:start": 37, "trinity:end": 42}, total=60)
    t = BootTimeline(origin=0.0, store=store)
    t.phase_started("clean_slate", 0)
    t.phase_ended("clean_slate", 7.5)
    t.phase_started("preflight", 9)
    snap = t.snapshot(now=12)
    assert [(p["name"], p["title"], p["state"]) for p in snap["phases"]] == [
        ("clean_slate", "Clean slate", "done"),
        ("preflight", "Preflight", "running"),
        ("trinity", "Trinity", "upcoming"),        # learned from previous boots
    ]
    assert snap["phases"][0]["seconds"] == 7.5 and snap["phases"][1]["seconds"] == 3.0
    assert not snap["complete"] and not snap["learning"] and 0 < snap["percent"] < 100


def test_a_completed_boot_lists_nothing_still_to_come(tmp_path):
    t = BootTimeline(origin=0.0, store=tmp_path / "t.json")
    t.phase_started("preflight", 1)
    t.phase_ended("preflight", 2)
    t.completed(3)
    snap = t.snapshot(now=10)
    assert [p["state"] for p in snap["phases"]] == ["done"]
    assert snap["complete"] and snap["percent"] == 100 and snap["elapsed"] == 3.0


def test_a_first_boot_shows_only_what_it_has_seen(tmp_path):
    t = BootTimeline(origin=0.0, store=tmp_path / "t.json")
    t.phase_started("clean_slate", 1)
    snap = t.snapshot(now=2)
    assert snap["learning"] and [p["name"] for p in snap["phases"]] == ["clean_slate"]
