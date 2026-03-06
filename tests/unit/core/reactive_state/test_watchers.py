"""Tests for the WatcherManager with bounded-queue backpressure.

Covers subscribe/notify, exact and glob key matching, unsubscribe,
callback exception isolation, multiple watchers, drop counting,
and overflow semantics.
"""
from __future__ import annotations

import time
from typing import List, Optional, Tuple

import pytest

from backend.core.reactive_state.types import StateEntry
from backend.core.reactive_state.watchers import WatcherManager


# ── Helpers ────────────────────────────────────────────────────────────


def _make_entry(key: str, value: object = True, version: int = 1) -> StateEntry:
    return StateEntry(
        key=key,
        value=value,
        version=version,
        epoch=1,
        writer="test",
        origin="explicit",
        updated_at_mono=time.monotonic(),
        updated_at_unix_ms=int(time.time() * 1000),
    )


# ── Tests ──────────────────────────────────────────────────────────────


class TestSubscribeAndNotify:
    """subscribe() registers a watcher that receives (old, new) on notify()."""

    def test_callback_receives_old_and_new(self) -> None:
        mgr = WatcherManager()
        received: List[Tuple[Optional[StateEntry], StateEntry]] = []

        def cb(old: Optional[StateEntry], new: StateEntry) -> None:
            received.append((old, new))

        mgr.subscribe("gcp.vm_ready", cb)

        old = _make_entry("gcp.vm_ready", value=False, version=1)
        new = _make_entry("gcp.vm_ready", value=True, version=2)
        mgr.notify("gcp.vm_ready", old, new)

        assert len(received) == 1
        assert received[0][0] is old
        assert received[0][1] is new


class TestExactKeyMatch:
    """Only watchers whose pattern matches the key are triggered."""

    def test_exact_key_only_triggers_matching_watcher(self) -> None:
        mgr = WatcherManager()
        hits: List[str] = []

        mgr.subscribe("gcp.vm_ready", lambda o, n: hits.append("gcp"))
        mgr.subscribe("audio.active", lambda o, n: hits.append("audio"))

        new = _make_entry("gcp.vm_ready")
        mgr.notify("gcp.vm_ready", None, new)

        assert hits == ["gcp"]


class TestGlobPatternMatch:
    """Glob patterns like 'memory.*' match dotted keys correctly."""

    def test_memory_glob_matches_memory_keys_only(self) -> None:
        mgr = WatcherManager()
        matched_keys: List[str] = []

        mgr.subscribe("memory.*", lambda o, n: matched_keys.append(n.key))

        mgr.notify("memory.tier", None, _make_entry("memory.tier"))
        mgr.notify("memory.available_gb", None, _make_entry("memory.available_gb"))
        mgr.notify("gcp.offload_active", None, _make_entry("gcp.offload_active"))

        assert "memory.tier" in matched_keys
        assert "memory.available_gb" in matched_keys
        assert "gcp.offload_active" not in matched_keys
        assert len(matched_keys) == 2


class TestStarPatternMatchesAll:
    """The '*' pattern matches every key."""

    def test_star_matches_all_keys(self) -> None:
        mgr = WatcherManager()
        seen: List[str] = []

        mgr.subscribe("*", lambda o, n: seen.append(n.key))

        mgr.notify("gcp.vm_ready", None, _make_entry("gcp.vm_ready"))
        mgr.notify("audio.active", None, _make_entry("audio.active"))
        mgr.notify("memory.tier", None, _make_entry("memory.tier"))

        assert seen == ["gcp.vm_ready", "audio.active", "memory.tier"]


class TestUnsubscribe:
    """unsubscribe() removes the watcher so it no longer fires."""

    def test_unsubscribe_stops_notifications(self) -> None:
        mgr = WatcherManager()
        count = [0]

        watch_id = mgr.subscribe("gcp.vm_ready", lambda o, n: count.__setitem__(0, count[0] + 1))

        mgr.notify("gcp.vm_ready", None, _make_entry("gcp.vm_ready"))
        assert count[0] == 1

        result = mgr.unsubscribe(watch_id)
        assert result is True

        mgr.notify("gcp.vm_ready", None, _make_entry("gcp.vm_ready"))
        assert count[0] == 1  # No increment after unsubscribe

        # Unsubscribing again returns False
        assert mgr.unsubscribe(watch_id) is False


class TestCallbackExceptionIsolation:
    """A raising callback does not prevent other watchers from firing."""

    def test_bad_callback_does_not_poison_others(self) -> None:
        mgr = WatcherManager()
        good_hits: List[str] = []

        def bad_callback(old: Optional[StateEntry], new: StateEntry) -> None:
            raise RuntimeError("I am broken")

        def good_callback(old: Optional[StateEntry], new: StateEntry) -> None:
            good_hits.append(new.key)

        mgr.subscribe("gcp.*", bad_callback)
        mgr.subscribe("gcp.*", good_callback)

        # Should not raise, and the good callback should still fire
        mgr.notify("gcp.vm_ready", None, _make_entry("gcp.vm_ready"))

        assert good_hits == ["gcp.vm_ready"]


class TestMultipleWatchersSamePattern:
    """Multiple watchers on the same pattern all receive notifications."""

    def test_both_watchers_receive(self) -> None:
        mgr = WatcherManager()
        a_hits: List[str] = []
        b_hits: List[str] = []

        mgr.subscribe("gcp.vm_ready", lambda o, n: a_hits.append("a"))
        mgr.subscribe("gcp.vm_ready", lambda o, n: b_hits.append("b"))

        mgr.notify("gcp.vm_ready", None, _make_entry("gcp.vm_ready"))

        assert a_hits == ["a"]
        assert b_hits == ["b"]


class TestDropCountTracked:
    """drop_count starts at 0 and total_drops starts at 0."""

    def test_initial_drop_counts_are_zero(self) -> None:
        mgr = WatcherManager()
        assert mgr.total_drops() == 0

        # Subscribe and do normal notifications -- no drops
        mgr.subscribe("gcp.*", lambda o, n: None)
        mgr.notify("gcp.vm_ready", None, _make_entry("gcp.vm_ready"))

        assert mgr.total_drops() == 0


class TestDropOldestOverflow:
    """With synchronous callbacks all notifications are delivered (no async queue)."""

    def test_sync_dispatch_delivers_all(self) -> None:
        mgr = WatcherManager()
        delivered: List[int] = []

        mgr.subscribe(
            "counter",
            lambda o, n: delivered.append(n.value),
            max_queue_size=2,
            overflow_policy="drop_oldest",
        )

        for i in range(5):
            mgr.notify("counter", None, _make_entry("counter", value=i, version=i + 1))

        # All 5 delivered because callbacks are synchronous
        assert delivered == [0, 1, 2, 3, 4]
        assert mgr.total_drops() == 0
