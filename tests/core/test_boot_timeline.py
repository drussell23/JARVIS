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
