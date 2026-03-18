"""tests/unit/core/test_component_health_beacon.py — Disease 7 beacon tests."""
from __future__ import annotations

import time

import pytest

from backend.core.component_health_beacon import (
    BeaconRegistry,
    BeaconStatus,
    ComponentHealthBeacon,
    ProgressUpdate,
    get_beacon_registry,
)


class TestComponentHealthBeacon:
    def test_initial_status_is_idle(self):
        b = ComponentHealthBeacon("svc")
        assert b.status == BeaconStatus.IDLE

    def test_heartbeat_transitions_to_working(self):
        b = ComponentHealthBeacon("svc")
        b.heartbeat("loading", 10.0)
        assert b.status == BeaconStatus.WORKING

    def test_heartbeat_clamps_progress_pct(self):
        b = ComponentHealthBeacon("svc")
        b.heartbeat(progress_pct=150.0)
        assert b.progress_pct == 100.0
        b.heartbeat(progress_pct=-5.0)
        assert b.progress_pct == 0.0

    def test_complete_sets_100_pct(self):
        b = ComponentHealthBeacon("svc")
        b.complete("done")
        assert b.status == BeaconStatus.COMPLETE
        assert b.progress_pct == 100.0
        assert b.note == "done"

    def test_fail_sets_failed_status(self):
        b = ComponentHealthBeacon("svc")
        b.fail("exploded")
        assert b.status == BeaconStatus.FAILED
        assert b.note == "exploded"

    def test_stall_seconds_increases_over_time(self):
        b = ComponentHealthBeacon("svc")
        b.heartbeat()
        time.sleep(0.05)
        assert b.stall_seconds() >= 0.04

    def test_is_stalled_true_when_over_threshold(self):
        b = ComponentHealthBeacon("svc")
        b.heartbeat()
        time.sleep(0.05)
        assert b.is_stalled(threshold_s=0.03)

    def test_is_stalled_false_when_under_threshold(self):
        b = ComponentHealthBeacon("svc")
        b.heartbeat()
        assert not b.is_stalled(threshold_s=60.0)

    def test_complete_never_stalled(self):
        b = ComponentHealthBeacon("svc")
        b.complete()
        # Even with tiny threshold, complete beacons are never stalled
        assert not b.is_stalled(threshold_s=0.0)

    def test_failed_never_stalled(self):
        b = ComponentHealthBeacon("svc")
        b.fail("oops")
        assert not b.is_stalled(threshold_s=0.0)

    def test_history_accumulates(self):
        b = ComponentHealthBeacon("svc")
        b.heartbeat("a")
        b.heartbeat("b")
        h = b.history()
        assert len(h) == 2
        assert h[0].note == "a"
        assert h[1].note == "b"

    def test_snapshot_is_frozen(self):
        import dataclasses
        b = ComponentHealthBeacon("svc")
        b.heartbeat("x", 50.0)
        snap = b.snapshot()
        assert dataclasses.is_dataclass(snap)
        with pytest.raises((dataclasses.FrozenInstanceError, TypeError, AttributeError)):
            snap.note = "tampered"  # type: ignore[misc]

    def test_snapshot_reflects_current_state(self):
        b = ComponentHealthBeacon("svc")
        b.heartbeat("loading", 33.0)
        snap = b.snapshot()
        assert snap.status == BeaconStatus.WORKING
        assert snap.progress_pct == 33.0
        assert snap.note == "loading"


class TestBeaconRegistry:
    def test_get_or_create_new(self):
        r = BeaconRegistry()
        b = r.get_or_create("alpha")
        assert isinstance(b, ComponentHealthBeacon)
        assert b.component == "alpha"

    def test_get_or_create_returns_same_instance(self):
        r = BeaconRegistry()
        b1 = r.get_or_create("alpha")
        b2 = r.get_or_create("alpha")
        assert b1 is b2

    def test_get_returns_none_for_unknown(self):
        r = BeaconRegistry()
        assert r.get("unknown") is None

    def test_all_stalled_empty_when_no_stalls(self):
        r = BeaconRegistry()
        r.get_or_create("alpha").heartbeat()
        assert r.all_stalled(threshold_s=60.0) == []

    def test_all_stalled_returns_stalled_beacons(self):
        r = BeaconRegistry()
        b = r.get_or_create("alpha")
        b.heartbeat()
        time.sleep(0.05)
        stalled = r.all_stalled(threshold_s=0.03)
        assert b in stalled

    def test_all_completed(self):
        r = BeaconRegistry()
        r.get_or_create("a").complete()
        r.get_or_create("b").heartbeat()
        done = r.all_completed()
        assert len(done) == 1
        assert done[0].component == "a"

    def test_all_failed(self):
        r = BeaconRegistry()
        r.get_or_create("a").fail("boom")
        r.get_or_create("b").complete()
        failed = r.all_failed()
        assert len(failed) == 1
        assert failed[0].component == "a"

    def test_snapshot_keys_match_registered(self):
        r = BeaconRegistry()
        r.get_or_create("x").heartbeat()
        r.get_or_create("y").complete()
        snap = r.snapshot()
        assert set(snap.keys()) == {"x", "y"}

    def test_reset_clears_all(self):
        r = BeaconRegistry()
        r.get_or_create("a")
        r.reset()
        assert r.get("a") is None


class TestSingleton:
    def test_get_beacon_registry_is_reused(self):
        r1 = get_beacon_registry()
        r2 = get_beacon_registry()
        assert r1 is r2
