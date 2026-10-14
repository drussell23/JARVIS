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
