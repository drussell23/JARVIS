"""The organism does not stop working when the operator closes a terminal.

``publish_line`` / ``publish_markup`` both opened with ``if not self._clients:
return`` -- correct about the socket, wrong about the organism. Every
``Update(path)`` block, tool result and diff composed while detached was
formatted and discarded, so reattaching showed an idle-looking cockpit for a
session that had been busy. That is indistinguishable from the organism having
done nothing, which is the same defect class as a receipt reading "queued"
about work that was thrown away.

These tests pin the retention RULES, not just the happy path, because every
rule here exists to stop the fix from causing a worse bug than the one it
closes:

  * ambient only  -- replaying one cockpit's private verb output into a
                     stranger's scrollback is what `_broadcast`'s addressing
                     exists to prevent;
  * empty audience only -- a second cockpit is joining a live session, not
                     recovering a gap;
  * two-phase drain -- a client whose socket dies mid-flush must not destroy
                     the backlog on everyone else's behalf;
  * TTL + overflow counted -- history painted as present tense, and a
                     truncated backlog presenting itself as complete, are both
                     dishonesty rather than mere staleness.

No socket is bound here: the bridge's publish path and its ring are exercised
directly, so the suite runs anywhere. The end-to-end socket cycle (detach ->
work -> reattach -> replay) was verified live over a real UDS pair.
"""
from __future__ import annotations

import time

import pytest

from backend.core.ouroboros.battle_test import cockpit_attach as ca


@pytest.fixture
def bridge(tmp_path):
    return ca.CockpitAttachBridge(path=tmp_path / "c.sock")


class TestRetention:
    def test_frames_published_with_no_audience_are_kept(self, bridge):
        bridge.publish_markup("⏺ Update(a.py)")
        bridge.publish_line("a plain line")
        assert bridge.backlog_stats()["backlog_pending"] == 2

    def test_order_is_preserved_oldest_first(self, bridge):
        for i in range(5):
            bridge.publish_markup(f"frame-{i}")
        texts = [f["text"] for f in bridge._backlog.peek()]
        assert texts == [f"frame-{i}" for i in range(5)]

    def test_replayed_frames_carry_honest_provenance(self, bridge):
        bridge.publish_markup("⏺ old work")
        frame = bridge._backlog.peek()[0]
        assert frame["backlogged"] is True
        assert frame["addressed"] is False
        # The moment it HAPPENED, not the moment it was replayed. A client
        # stamping receipt-time would date an hour-old diff to now.
        assert frame["ts"] <= time.time()

    def test_addressed_output_is_never_retained(self, bridge):
        """Session-addressed output belongs to a cockpit that has gone.

        Session ids do not survive a reattach, so replaying this would paint
        one operator's `/posture status` into another's scrollback."""
        bridge.publish_markup("private verb answer", session="sess-A")
        assert bridge.backlog_stats()["backlog_pending"] == 0

    def test_channel_kill_switch_is_not_an_empty_audience(self, bridge, monkeypatch):
        """A disabled channel must not become a DELAYED channel."""
        monkeypatch.setenv("JARVIS_ATTACH_TOOL_ACTIVITY_ENABLED", "0")
        bridge.publish_markup("⏺ should never be kept")
        assert bridge.backlog_stats()["backlog_pending"] == 0

    def test_master_flag_off_is_byte_identical_drop(self, bridge, monkeypatch):
        monkeypatch.setenv("JARVIS_ATTACH_OFFLINE_BACKLOG_ENABLED", "0")
        bridge.publish_markup("⏺ dropped as before")
        bridge.publish_line("dropped as before")
        assert bridge.backlog_stats()["backlog_pending"] == 0


class TestBounds:
    def test_overflow_is_counted_never_silent(self, monkeypatch):
        monkeypatch.setenv("JARVIS_ATTACH_BACKLOG_SIZE", "3")
        ring = ca._OfflineBacklog()
        for i in range(7):
            ring.retain({"type": "line", "text": str(i)})
        snap = ring.snapshot()
        assert snap["backlog_pending"] == 3
        assert snap["backlog_overflowed"] == 4, (
            "a truncated backlog that reported itself complete would be a "
            "receipt for work it did not show"
        )

    def test_the_ring_keeps_the_NEWEST_frames(self, monkeypatch):
        monkeypatch.setenv("JARVIS_ATTACH_BACKLOG_SIZE", "3")
        ring = ca._OfflineBacklog()
        for i in range(6):
            ring.retain({"type": "line", "text": str(i)})
        assert [f["text"] for f in ring.peek()] == ["3", "4", "5"]

    def test_stale_frames_age_out(self, monkeypatch):
        monkeypatch.setenv("JARVIS_ATTACH_BACKLOG_TTL_S", "0.05")
        ring = ca._OfflineBacklog()
        ring.retain({"type": "line", "text": "ancient"})
        time.sleep(0.12)
        assert ring.peek() == []
        assert ring.snapshot()["backlog_dropped_stale"] == 1

    def test_ttl_zero_disables_ageing(self, monkeypatch):
        monkeypatch.setenv("JARVIS_ATTACH_BACKLOG_TTL_S", "0")
        ring = ca._OfflineBacklog()
        ring.retain({"type": "line", "text": "kept"})
        time.sleep(0.05)
        assert len(ring.peek()) == 1

    def test_size_and_ttl_are_read_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("JARVIS_ATTACH_BACKLOG_SIZE", "17")
        monkeypatch.setenv("JARVIS_ATTACH_BACKLOG_TTL_S", "42.5")
        assert ca._backlog_size() == 17
        assert ca._backlog_ttl_s() == 42.5

    def test_malformed_env_falls_back_rather_than_raising(self, monkeypatch):
        monkeypatch.setenv("JARVIS_ATTACH_BACKLOG_SIZE", "not-a-number")
        monkeypatch.setenv("JARVIS_ATTACH_BACKLOG_TTL_S", "banana")
        assert ca._backlog_size() == 200
        assert ca._backlog_ttl_s() == 1800.0


class TestTwoPhaseDrain:
    def test_peek_does_not_consume(self):
        ring = ca._OfflineBacklog()
        ring.retain({"type": "line", "text": "x"})
        assert len(ring.peek()) == 1
        assert len(ring.peek()) == 1, "a failed flush must leave the ring intact"

    def test_commit_discards_exactly_what_was_sent(self):
        """A frame retained DURING the flush must survive to the next attach."""
        ring = ca._OfflineBacklog()
        for i in range(3):
            ring.retain({"type": "line", "text": str(i)})
        sent = ring.peek()
        ring.retain({"type": "line", "text": "arrived-mid-flush"})
        ring.commit(len(sent))
        assert [f["text"] for f in ring.peek()] == ["arrived-mid-flush"]
        assert ring.snapshot()["backlog_replayed"] == 3

    def test_commit_is_bounded_by_what_is_present(self):
        ring = ca._OfflineBacklog()
        ring.retain({"type": "line", "text": "only"})
        ring.commit(99)          # must not raise or wrap
        assert ring.peek() == []


class TestResilience:
    def test_retention_never_raises_on_a_hostile_frame(self):
        ring = ca._OfflineBacklog()

        class _Hostile(dict):
            def __iter__(self):  # noqa: D105
                raise RuntimeError("boom")

        ring.retain(_Hostile())          # must be swallowed
        ring.peek()                      # must not propagate

    def test_publish_never_raises_when_the_ring_is_broken(self, bridge):
        bridge._backlog = None           # simulate catastrophic state
        bridge.publish_markup("⏺ still must not raise")
        bridge.publish_line("still must not raise")

    def test_stats_are_safe_when_the_ring_is_gone(self, bridge):
        bridge._backlog = None
        assert bridge.backlog_stats() == {}
