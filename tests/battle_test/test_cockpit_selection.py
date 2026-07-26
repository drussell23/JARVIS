"""D3 — selection, focus, and the tombstone that makes them safe.

The ghost pane
--------------
The deck is painted from a snapshot. The operator reads it, reaches for an
arrow key, and presses Enter — and in that human interval the worker can
finish. Destroying the lane on completion makes that a race the operator
loses at random.

Wrapping the lookup in ``except KeyError`` does not fix it. Catching the miss
turns a crash into an empty pane, which is the same lie told politely: the
operator asked to see a worker's output and was shown nothing, with no way to
tell "it produced nothing" from "it was deleted between your eye and your
finger". The fix is retention, so the selection that was valid when it was
displayed is still valid when it is acted on.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List

import pytest

from backend.core.ouroboros.battle_test.cockpit_fsm import (
    MODE_FLOW,
    MODE_SELECT,
    CockpitFSM,
    focus_lane,
    is_valid_mode,
)
from backend.core.ouroboros.battle_test.lane_rings import (
    LaneRegistry,
    get_lane_registry,
    reset_lane_registry,
)


@pytest.fixture(autouse=True)
def _clean():
    reset_lane_registry()
    yield
    reset_lane_registry()


def _rows(*lanes: str) -> List[Dict[str, Any]]:
    return [
        {"lane": ln, "lines": 3, "last_seen": 0.0, "label": "",
         "tombstoned": False, "age_s": 0.1}
        for ln in lanes
    ]


# --------------------------------------------------------------------------
# 1. the tombstone — requirement 1
# --------------------------------------------------------------------------

def test_focusing_a_just_finished_lane_hydrates_its_history() -> None:
    """The race the operator would otherwise lose."""
    reg = LaneRegistry(ring=10)
    reg.record("unit/7", "read saga.py")
    reg.record("unit/7", "patched _topological_sort")
    reg.mark_dead("unit/7")          # worker finishes mid-selection

    assert reg.is_tombstoned("unit/7")
    hist = [ln.text for ln in reg.history("unit/7")]
    assert hist == ["read saga.py", "patched _topological_sort"], (
        "a finished lane lost its history — the pane would render empty"
    )


def test_a_tombstoned_lane_is_still_listed_and_selectable() -> None:
    reg = LaneRegistry(ring=10)
    reg.record("unit/7", "x")
    reg.mark_dead("unit/7")
    rows = reg.summary()
    assert rows and rows[0]["lane"] == "unit/7"
    assert rows[0]["tombstoned"] is True

    fsm = CockpitFSM(lanes_provider=lambda: rows)
    assert fsm.enter_select() is True
    assert fsm.focus_selected() is True
    assert fsm.focused_lane == "unit/7"


def test_live_lanes_sort_above_tombstones() -> None:
    """A running worker is more interesting than a finished one."""
    clock = {"t": 0.0}
    reg = LaneRegistry(ring=10, clock=lambda: clock["t"])
    reg.record("unit/dead", "x")
    reg.mark_dead("unit/dead")
    clock["t"] = 1.0
    reg.record("unit/live", "y")
    assert [r["lane"] for r in reg.summary()] == ["unit/live", "unit/dead"]


def test_tombstones_expire_but_quiet_live_lanes_do_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Silence is not death — a quiet worker is still a worker."""
    monkeypatch.setenv("JARVIS_LANE_TOMBSTONE_TTL_S", "10")
    clock = {"t": 0.0}
    reg = LaneRegistry(ring=10, clock=lambda: clock["t"])
    reg.record("unit/dead", "x")
    reg.record("unit/quiet", "y")
    reg.mark_dead("unit/dead")
    clock["t"] = 30.0
    lanes = [r["lane"] for r in reg.summary()]
    assert "unit/dead" not in lanes
    assert "unit/quiet" in lanes


def test_marking_an_unknown_lane_does_not_invent_one() -> None:
    reg = LaneRegistry(ring=10)
    assert reg.mark_dead("never/existed") is False
    assert reg.summary() == []


def test_eviction_prefers_tombstones_over_live_lanes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_LANE_MAX", "2")
    clock = {"t": 0.0}
    reg = LaneRegistry(ring=5, clock=lambda: clock["t"])
    reg.record("live/old", "a")          # oldest, but ALIVE
    clock["t"] = 5.0
    reg.record("dead/new", "b")
    reg.mark_dead("dead/new")
    clock["t"] = 10.0
    reg.record("live/new", "c")          # forces an eviction
    lanes = [r["lane"] for r in reg.summary()]
    assert "live/old" in lanes, "a running worker was evicted for a corpse"
    assert "dead/new" not in lanes


# --------------------------------------------------------------------------
# 2. the FSM — requirement 2
# --------------------------------------------------------------------------

def test_escape_returns_to_flow_from_focus() -> None:
    fsm = CockpitFSM(lanes_provider=lambda: _rows("unit/1"))
    fsm.enter_select()
    fsm.focus_selected()
    assert fsm.mode.startswith("focus:")
    assert fsm.escape() is True
    assert fsm.mode == MODE_FLOW, "esc did not restore the ambient view"


def test_escape_from_select_also_returns_to_flow() -> None:
    fsm = CockpitFSM(lanes_provider=lambda: _rows("unit/1"))
    fsm.enter_select()
    fsm.escape()
    assert fsm.mode == MODE_FLOW


def test_escape_is_one_press_not_one_level() -> None:
    """An operator hitting Escape wants out, not one step back."""
    fsm = CockpitFSM(lanes_provider=lambda: _rows("a", "b"))
    fsm.enter_select()
    fsm.move(1)
    fsm.focus_selected()
    fsm.escape()
    assert fsm.mode == MODE_FLOW
    assert fsm.cursor == 0


def test_select_is_refused_on_an_empty_deck() -> None:
    """A cursor list with nothing in it is a mode you cannot leave by
    selecting."""
    fsm = CockpitFSM(lanes_provider=lambda: [])
    assert fsm.enter_select() is False
    assert fsm.mode == MODE_FLOW


def test_cursor_clamps_and_does_not_wrap() -> None:
    """Wrapping in a list whose length changes underneath means Down at the
    bottom can silently land on row 0 of a deck that just grew."""
    fsm = CockpitFSM(lanes_provider=lambda: _rows("a", "b"))
    fsm.enter_select()
    fsm.move(-5)
    assert fsm.cursor == 0
    fsm.move(99)
    assert fsm.cursor == 1


def test_cursor_resolves_against_the_current_deck_not_a_snapshot() -> None:
    """The structural answer to drift: nothing is cached, so a deck that
    shrank between keypresses cannot produce an out-of-range selection."""
    rows = _rows("a", "b", "c")
    fsm = CockpitFSM(lanes_provider=lambda: rows)
    fsm.enter_select()
    fsm.move(2)
    assert fsm.cursor == 2
    del rows[1:]                       # the deck shrinks under the cursor
    assert fsm.focus_selected() is True
    assert fsm.focused_lane == "a", "selection resolved against a stale list"


def test_deck_emptying_during_select_drops_back_to_flow() -> None:
    rows = _rows("a")
    fsm = CockpitFSM(lanes_provider=lambda: rows)
    fsm.enter_select()
    rows.clear()
    fsm.move(1)
    assert fsm.mode == MODE_FLOW


def test_a_broken_lanes_provider_is_an_empty_deck() -> None:
    def _boom() -> Any:
        raise RuntimeError("provider on fire")

    fsm = CockpitFSM(lanes_provider=_boom)
    assert fsm.rows() == []
    assert fsm.enter_select() is False


@pytest.mark.parametrize("bad", ["", "focus:", "focus:has space",
                                 "focus:" + "x" * 200, "nonsense"])
def test_invalid_modes_are_rejected(bad: str) -> None:
    assert is_valid_mode(bad) is False
    assert focus_lane(bad) is None


def test_selection_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_COCKPIT_SELECTION", "0")
    fsm = CockpitFSM(lanes_provider=lambda: _rows("a"))
    assert fsm.enter_select() is False
    assert fsm.focus("a") is False


# --------------------------------------------------------------------------
# 3. hydration over the bridge
# --------------------------------------------------------------------------

class _Writer:
    def __init__(self) -> None:
        self.frames: List[Dict[str, Any]] = []

    def is_closing(self) -> bool:
        return False

    def write(self, data: bytes) -> None:
        for line in data.decode().splitlines():
            if line.strip():
                self.frames.append(json.loads(line))

    def close(self) -> None:
        pass


async def test_history_request_is_answered_only_to_the_asker() -> None:
    from backend.core.ouroboros.battle_test.cockpit_attach import (
        CockpitAttachBridge,
    )
    reg = get_lane_registry()
    reg.record("unit/9", "did the thing")
    reg.mark_dead("unit/9")

    a, b = _Writer(), _Writer()
    bridge = CockpitAttachBridge()
    bridge._loop = asyncio.get_running_loop()   # type: ignore[attr-defined]
    for w, sid in ((a, "sess-A"), (b, "sess-B")):
        bridge._clients.add(w)                  # type: ignore[arg-type]
        bridge.bind_session(sid, w)             # type: ignore[arg-type]

    bridge._serve_lane_history("unit/9", "sess-A")
    await asyncio.sleep(0)

    assert a.frames and a.frames[0]["type"] == "lane_history"
    assert a.frames[0]["lines"] == ["did the thing"]
    assert a.frames[0]["tombstoned"] is True
    assert b.frames == [], "another cockpit saw terminal A's focused lane"


async def test_a_vanished_lane_answers_found_false_not_silence() -> None:
    """An empty pane and an aged-out lane are different facts."""
    from backend.core.ouroboros.battle_test.cockpit_attach import (
        CockpitAttachBridge,
    )
    w = _Writer()
    bridge = CockpitAttachBridge()
    bridge._loop = asyncio.get_running_loop()   # type: ignore[attr-defined]
    bridge._clients.add(w)                      # type: ignore[arg-type]
    bridge.bind_session("sess-A", w)            # type: ignore[arg-type]

    bridge._serve_lane_history("unit/gone", "sess-A")
    await asyncio.sleep(0)
    assert w.frames[0]["found"] is False
    assert w.frames[0]["lines"] == []


# --------------------------------------------------------------------------
# 4. the client renders each mode into the one toolbar region
# --------------------------------------------------------------------------

def test_toolbar_shows_the_lane_list_in_select() -> None:
    from backend.core.ouroboros.cli import ov

    ui = ov.AttachUI()
    ui._lanes = _rows("swarm/a", "unit/b")
    assert ui.fsm.enter_select() is True
    out = ui.toolbar()
    assert "swarm/a" in out and "unit/b" in out
    assert "↑↓" in out, "no affordance shown for the new mode"


def test_toolbar_shows_hydrated_output_in_focus() -> None:
    from backend.core.ouroboros.cli import ov

    ui = ov.AttachUI()
    ui._lanes = _rows("unit/b")
    ui.fsm.enter_select()
    ui.fsm.focus_selected()
    ui.on_lane_history({
        "lane": "unit/b", "found": True, "tombstoned": True,
        "dropped": 0, "lines": ["patched saga.py"],
    })
    out = ui.toolbar()
    assert "patched saga.py" in out
    assert "finished" in out


def test_stale_hydration_for_an_abandoned_lane_is_ignored() -> None:
    """The operator escaped before the answer arrived; it must not repaint
    a pane they already left."""
    from backend.core.ouroboros.cli import ov

    ui = ov.AttachUI()
    ui._lanes = _rows("unit/b")
    ui.fsm.enter_select()
    ui.fsm.focus_selected()
    ui.fsm.escape()
    ui.on_lane_history({
        "lane": "unit/b", "found": True, "lines": ["late answer"],
    })
    assert "late answer" not in ui.toolbar()


def test_escape_restores_the_ambient_deck_view() -> None:
    """Requirement 2, at the render layer."""
    from backend.core.ouroboros.cli import ov

    ui = ov.AttachUI()
    ui.on_ambient("DW provider failover")
    ui._lanes = _rows("unit/b")
    ui.fsm.enter_select()
    assert "failover" not in ui.toolbar(), "ambient deck leaked into SELECT"
    ui.fsm.escape()
    assert "failover" in ui.toolbar(), "ambient view was not restored"
