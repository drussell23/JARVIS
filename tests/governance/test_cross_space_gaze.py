"""Cross-Space Gaze spine — ghost filter, dedup, temporal focus decay.

Mandate 4 verbatim (2026-07-19): a payload with a valid IDE (Space 1),
a mirrored ghost of the SAME IDE (Space 2, same PID/title), and an
invisible 10x10 system window → the ghost filter drops the system
window, the deduplicator merges the mirrored IDE, and the FSM
orchestrates without redundant data. Plus temporal focus decay
suppressing stale-Space proactive alerts.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.comms.duplex.cross_space_gaze import (
    CrossSpaceGaze,
    filter_and_dedup,
    is_ghost_window,
)


def _win(pid, title, w=800, h=600, onscreen=True, owner="Code"):
    return {
        "kCGWindowOwnerPID": pid, "kCGWindowName": title,
        "kCGWindowOwnerName": owner, "kCGWindowIsOnscreen": onscreen,
        "kCGWindowBounds": {"Width": w, "Height": h},
    }


class _FakeAnalyzer:
    def __init__(self, insights):
        self._insights = insights
        self.calls = 0
    def analyze_cross_space_context(self, spaces_data, screenshots=None):
        self.calls += 1
        return {"insights": self._insights, "spaces_seen": list(spaces_data)}


class TestGhostFilter:
    def test_offscreen_tiny_and_daemon_windows_dropped(self):
        assert is_ghost_window(_win(1, "IDE", onscreen=False)) is True
        assert is_ghost_window(_win(1, "shadow", w=10, h=10)) is True
        assert is_ghost_window(_win(1, "", owner="WindowServer")) is True
        assert is_ghost_window(_win(1, "main.py — Code")) is False

    def test_unreadable_window_fails_closed(self):
        assert is_ghost_window({"garbage": True}) is True


class TestMandate4:
    def test_ghost_dropped_mirror_merged_no_redundancy(self):
        """MANDATE 4 VERBATIM."""
        spaces = {
            1: [_win(500, "orchestrator.py — Code")],       # valid IDE
            2: [
                _win(500, "orchestrator.py — Code"),         # MIRROR (dup)
                _win(99, "", w=10, h=10, owner="WindowServer"),  # ghost
            ],
        }
        clean, stats = filter_and_dedup(spaces)
        assert stats["ghosts_dropped"] == 1                 # system window
        assert stats["duplicates_merged"] == 1              # mirrored IDE
        assert stats["kept"] == 1                           # one real window
        # The IDE survives on its FIRST space only:
        assert 1 in clean and 2 not in clean
        assert len(clean[1]) == 1
        # The FSM orchestrates seamlessly (no redundant data reaches
        # the analyzer):
        analyzer = _FakeAnalyzer(insights=[])
        gaze = CrossSpaceGaze(thermal_ok=lambda: True, analyzer=analyzer)
        r = gaze.tick(spaces)
        assert r["synthesized"] is True
        assert r["context"]["spaces_seen"] == [1]           # deduped input


class TestTemporalFocusDecay:
    def test_stale_space_proactive_suppressed(self):
        clock = [10000.0]
        analyzer = _FakeAnalyzer(insights=[
            {"description": "test in Space 2 matches source in Space 1",
             "affected_spaces": [1, 2]},
        ])
        gaze = CrossSpaceGaze(
            thermal_ok=lambda: True, analyzer=analyzer,
            clock=lambda: clock[0],
        )
        gaze.note_focus(1)                                  # Space 1 fresh now
        clock[0] += 5.0
        gaze.note_focus(2)
        clock[0] += 4 * 3600.0                              # 4h later
        gaze.note_focus(1)                                  # only S1 refreshed
        spaces = {1: [_win(1, "src.py")], 2: [_win(2, "test.py")]}
        r = gaze.tick(spaces)
        assert r["synthesized"] is True
        # Space 2 is 4h stale → the cross-space insight is NOT proactive:
        assert r["proactive"] == []
        assert gaze.stats["proactive_suppressed"] == 1

    def test_all_fresh_spaces_allow_proactive(self):
        clock = [10000.0]
        analyzer = _FakeAnalyzer(insights=[
            {"description": "reconcile", "affected_spaces": [1, 2]},
        ])
        gaze = CrossSpaceGaze(
            thermal_ok=lambda: True, analyzer=analyzer, clock=lambda: clock[0],
        )
        gaze.note_focus(1); gaze.note_focus(2)              # both fresh
        r = gaze.tick({1: [_win(1, "a")], 2: [_win(2, "b")]})
        assert len(r["proactive"]) == 1                     # allowed

    def test_explicit_prompt_bypasses_decay(self):
        clock = [10000.0]
        analyzer = _FakeAnalyzer(insights=[
            {"description": "x", "affected_spaces": [7]},
        ])
        gaze = CrossSpaceGaze(
            thermal_ok=lambda: True, analyzer=analyzer, clock=lambda: clock[0],
        )
        # Space 7 never focused → stale — but the operator ASKED:
        r = gaze.tick({7: [_win(7, "q")]}, explicit=True)
        assert len(r["proactive"]) == 1


class TestDRYAndThermal:
    def test_unchanged_topology_skips_analyzer(self):
        analyzer = _FakeAnalyzer(insights=[])
        gaze = CrossSpaceGaze(thermal_ok=lambda: True, analyzer=analyzer)
        spaces = {1: [_win(1, "a")]}
        gaze.tick(spaces)
        gaze.tick(spaces)                                   # identical topology
        assert analyzer.calls == 1                          # dhash bypass
        assert gaze.stats["unchanged_skips"] == 1

    def test_changed_topology_reanalyzes(self):
        analyzer = _FakeAnalyzer(insights=[])
        gaze = CrossSpaceGaze(thermal_ok=lambda: True, analyzer=analyzer)
        gaze.tick({1: [_win(1, "a")]})
        gaze.tick({1: [_win(1, "a")], 2: [_win(2, "b")]})   # new window
        assert analyzer.calls == 2

    def test_explicit_bypasses_dhash_gate(self):
        analyzer = _FakeAnalyzer(insights=[])
        gaze = CrossSpaceGaze(thermal_ok=lambda: True, analyzer=analyzer)
        spaces = {1: [_win(1, "a")]}
        gaze.tick(spaces)
        gaze.tick(spaces, explicit=True)                    # forced
        assert analyzer.calls == 2

    def test_thermal_lock_skips_synthesis(self):
        analyzer = _FakeAnalyzer(insights=[])
        gaze = CrossSpaceGaze(thermal_ok=lambda: False, analyzer=analyzer)
        r = gaze.tick({1: [_win(1, "a")]})
        assert r["synthesized"] is False
        assert r["reason"] == "thermal_locked"
        assert analyzer.calls == 0                          # DRY governor
        assert gaze.stats["thermal_skips"] == 1

    def test_no_real_windows_after_filter(self):
        analyzer = _FakeAnalyzer(insights=[])
        gaze = CrossSpaceGaze(thermal_ok=lambda: True, analyzer=analyzer)
        r = gaze.tick({1: [_win(1, "", owner="Dock", w=5, h=5)]})
        assert r["synthesized"] is False
        assert r["reason"] == "no_real_windows"
