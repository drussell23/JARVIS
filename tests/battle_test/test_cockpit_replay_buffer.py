"""A cockpit that detaches gets its own answers back when it returns.

Before: cockpit ids were random per attachment and `_broadcast` dropped any
frame addressed to an absent cockpit, so a /session answer that finished
after the operator closed the terminal was composed and thrown away, and the
re-attached `ov` -- a new id -- was no longer in its /session at all.

Pinned here:
  * identity is the TERMINAL (stable across attaches, distinct across
    terminals, random when there is no terminal);
  * addressed output to an absent cockpit is held for THAT cockpit only;
  * re-binding flushes it, addressed, before live output, two-phase;
  * expired / evicted history is announced as truncated, never presented
    as whole;
  * the cockpit's live /session mode is restored on re-attach.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List

import pytest

from backend.core.ouroboros.battle_test import attach_session
from backend.core.ouroboros.battle_test import cockpit_attach as ca


class _Writer:
    def __init__(self, fail: bool = False) -> None:
        self.frames: List[Dict[str, Any]] = []
        self.closed = False
        self.fail = fail

    def is_closing(self) -> bool:
        return self.closed

    def write(self, data: bytes) -> None:
        if self.fail:
            raise BrokenPipeError
        for line in data.decode().splitlines():
            if line.strip():
                self.frames.append(json.loads(line))

    async def drain(self) -> None:
        if self.fail:
            raise BrokenPipeError

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def bridge(tmp_path):
    b = ca.CockpitAttachBridge(path=tmp_path / "c.sock")
    return b


def _texts(w: _Writer) -> List[str]:
    return [f.get("text", "") for f in w.frames]


class TestIdentity:
    def test_same_terminal_same_id(self, monkeypatch):
        monkeypatch.delenv("JARVIS_COCKPIT_ID", raising=False)
        monkeypatch.setenv("TMUX_PANE", "%7")
        assert attach_session.cockpit_identity() == attach_session.cockpit_identity()

    def test_different_terminal_different_id(self, monkeypatch):
        monkeypatch.delenv("JARVIS_COCKPIT_ID", raising=False)
        monkeypatch.setenv("TMUX_PANE", "%7")
        a = attach_session.cockpit_identity()
        monkeypatch.setenv("TMUX_PANE", "%8")
        assert attach_session.cockpit_identity() != a

    def test_explicit_id_wins_and_is_sanitised(self, monkeypatch):
        monkeypatch.setenv("JARVIS_COCKPIT_ID", "ops desk/1")
        assert attach_session.cockpit_identity() == "opsdesk1"

    def test_no_terminal_facts_is_random(self, monkeypatch):
        for v in ("JARVIS_COCKPIT_ID", "WT_SESSION", "TERM_SESSION_ID", "TMUX", "TMUX_PANE"):
            monkeypatch.delenv(v, raising=False)
        monkeypatch.setattr(attach_session.os, "ttyname",
                            lambda fd: (_ for _ in ()).throw(OSError()))
        assert attach_session.cockpit_identity() != attach_session.cockpit_identity()

    def test_stable_id_switch_off_is_random(self, monkeypatch):
        monkeypatch.delenv("JARVIS_COCKPIT_ID", raising=False)
        monkeypatch.setenv("TMUX_PANE", "%7")
        monkeypatch.setenv("JARVIS_COCKPIT_STABLE_ID", "false")
        assert attach_session.cockpit_identity() != attach_session.cockpit_identity()


class TestRetention:
    def test_addressed_markup_with_no_audience_waits_for_its_cockpit(self, bridge):
        bridge.publish_markup("answer for A", session="A")
        assert bridge.backlog_stats()["backlog_pending"] == 0
        assert bridge.backlog_stats()["cockpit_backlog_pending"] == 1

    def test_contextvar_addressed_line_waits_for_its_cockpit(self, bridge):
        with attach_session.session_scope("A"):
            bridge.publish_line("verb answer")
        assert bridge._cockpit_backlogs["A"].snapshot()["backlog_pending"] == 1

    async def test_absent_target_while_others_attached_is_held_not_broadcast(self, bridge):
        b = _Writer()
        bridge._loop = asyncio.get_running_loop()
        bridge._clients.add(b)
        bridge.bind_session("B", b)
        with attach_session.session_scope("A"):
            bridge.publish_markup("private answer")
        assert b.frames == []
        assert bridge._cockpit_backlogs["A"].snapshot()["backlog_pending"] == 1

    def test_control_plane_frames_are_not_held(self, bridge):
        bridge._retain_for_cockpit("A", {"type": "prompt", "text": "approve?"})
        assert "A" not in bridge._cockpit_backlogs

    def test_rings_are_lru_bounded_and_eviction_is_remembered(self, bridge, monkeypatch):
        monkeypatch.setenv("JARVIS_ATTACH_COCKPIT_BACKLOGS", "2")
        for c in ("A", "B", "C"):
            bridge.publish_markup(f"for {c}", session=c)
        assert list(bridge._cockpit_backlogs) == ["B", "C"]
        assert "A" in bridge._evicted_cockpits

    def test_master_flag_off_holds_nothing(self, bridge, monkeypatch):
        monkeypatch.setenv("JARVIS_ATTACH_OFFLINE_BACKLOG_ENABLED", "0")
        bridge.publish_markup("answer", session="A")
        assert not bridge._cockpit_backlogs


class TestReplay:
    async def test_rebind_flushes_addressed_in_order_then_binds(self, bridge):
        for i in range(3):
            bridge.publish_markup(f"a{i}", session="A")
        w = _Writer()
        assert await bridge._attach_cockpit("A", w) is True
        assert _texts(w) == ["a0", "a1", "a2"]
        assert all(f["addressed"] and f["backlogged"] for f in w.frames)
        assert bridge._sessions["A"] is w
        assert "A" not in bridge._cockpit_backlogs

    async def test_only_the_owner_gets_it(self, bridge):
        bridge.publish_markup("for A", session="A")
        b = _Writer()
        await bridge._attach_cockpit("B", b)
        assert b.frames == []
        assert bridge._cockpit_backlogs["A"].snapshot()["backlog_pending"] == 1

    async def test_socket_dying_mid_replay_loses_nothing(self, bridge):
        bridge.publish_markup("keep me", session="A")
        assert await bridge._attach_cockpit("A", _Writer(fail=True)) is False
        assert "A" not in bridge._sessions
        w = _Writer()
        await bridge._attach_cockpit("A", w)
        assert _texts(w) == ["keep me"]

    async def test_expired_history_is_announced_truncated(self, bridge, monkeypatch):
        monkeypatch.setenv("JARVIS_ATTACH_BACKLOG_SIZE", "2")
        for i in range(4):
            bridge.publish_markup(f"a{i}", session="A")
        w = _Writer()
        await bridge._attach_cockpit("A", w)
        assert "history truncated: 2 earlier line(s)" in _texts(w)[0]
        assert _texts(w)[1:] == ["a2", "a3"]

    async def test_evicted_cockpit_is_told_on_return(self, bridge, monkeypatch):
        monkeypatch.setenv("JARVIS_ATTACH_COCKPIT_BACKLOGS", "1")
        bridge.publish_markup("for A", session="A")
        bridge.publish_markup("for B", session="B")
        w = _Writer()
        await bridge._attach_cockpit("A", w)
        assert len(w.frames) == 1 and "history truncated" in w.frames[0]["text"]
        assert "A" not in bridge._evicted_cockpits

    async def test_fresh_cockpit_gets_no_truncation_notice(self, bridge):
        w = _Writer()
        await bridge._attach_cockpit("new", w)
        assert w.frames == []
        assert bridge._sessions["new"] is w

    async def test_live_session_mode_is_restored_on_reattach(self, bridge, monkeypatch):
        from backend.core.ouroboros.governance import foreground_session as fs
        seen = []
        mgr = fs.SessionManager(mode_notify=lambda c, s, o: seen.append((c, s)))
        fs.reset_for_tests(mgr)
        try:
            s = mgr.start("A")
            seen.clear()
            await bridge._attach_cockpit("A", _Writer())
            assert seen == [("A", s.id)]
        finally:
            fs.reset_for_tests(None)


class TestConcurrentClientsOnOneTerminal:
    def test_siblings_get_distinct_stable_slots(self, monkeypatch):
        """Two live clients on one terminal sharing its id let the bridge
        re-point the first client's address at the second (A's input then
        read as B's, A's pause hold counted as B's)."""
        from backend.core.ouroboros.battle_test.cockpit_attach import CockpitAttachClient
        monkeypatch.setenv("JARVIS_COCKPIT_ID", "termA")
        a = CockpitAttachClient(path="/nonexistent")
        b = CockpitAttachClient(path="/nonexistent")
        assert (a.session_id, b.session_id) == ("termA", "termA-1")
        a._release_identity()
        assert CockpitAttachClient(path="/nonexistent").session_id == "termA"
