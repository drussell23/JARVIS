"""Agent view — who is working right now.

O+V dispatches subagents constantly, and each announced itself as a LINE in
the deck. Lines scroll. Thirty seconds later the operator cannot tell whether
three agents are still running or all of them finished without scrolling back
and mentally diffing spawns against results.

A line is an event; "who is working right now" is a STATE.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.battle_test.agent_roster import (
    AgentRoster, format_duration,
)


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def roster():
    clock = _Clock()
    return AgentRoster(clock=clock), clock


# --------------------------------------------------------------------------
# the roster
# --------------------------------------------------------------------------

def test_an_empty_roster_renders_NOTHING(roster) -> None:
    """Not "0 agents". A cockpit that always shows a section for work that is
    not happening spends a row saying nothing, every session, forever."""
    r, _c = roster
    assert r.render() == []


def test_dispatched_agents_appear_under_main(roster) -> None:
    r, _c = roster
    r.spawn("sub-3f2a", "Explore", "Map ov completion architecture")
    lines = r.render()
    assert "❯ ⏺ main" in lines
    assert any("Explore" in ln and "Map ov completion" in ln for ln in lines)
    assert "  ↑/↓ to select · Enter to view" in lines


def test_a_running_agent_is_hollow_and_a_finished_one_is_filled(roster) -> None:
    r, c = roster
    r.spawn("a", "Explore", "goal")
    assert any("◯" in ln for ln in r.render())
    c.now += 5
    r.finish("a")
    assert any("⏺ Explore" in ln for ln in r.render())


def test_failure_and_loss_are_different_marks(roster) -> None:
    """One reported back; the other never did. Collapsing them hides which
    agents the system lost track of."""
    r, c = roster
    r.spawn("a", "Review", "verify"); r.spawn("b", "Explore", "map")
    r.finish("a", "failed")
    c.now += 10_000
    r.reap()
    marks = "\n".join(r.render())
    assert "✗" in marks and "?" in marks


# --------------------------------------------------------------------------
# finishing is not guaranteed
# --------------------------------------------------------------------------

def test_a_lost_agent_is_reaped_as_unknown_not_deleted(roster) -> None:
    """A ghost is worse than an omission: it implies work is still
    happening."""
    r, c = roster
    r.spawn("a", "Explore", "goal")
    assert r.running_count == 1
    c.now += 10_000
    assert r.reap() == 1
    assert r.running_count == 0
    assert r.entries[0].state == "unknown"


def test_a_respawn_is_a_retry_not_a_second_agent(roster) -> None:
    r, c = roster
    r.spawn("a", "Explore", "goal")
    c.now += 100
    r.spawn("a", "Explore", "goal")
    assert len(r.entries) == 1
    assert r.entries[0].elapsed(c.now) == 0.0, (
        "elapsed spanned an attempt that already ended"
    )


def test_eviction_never_drops_a_RUNNING_agent(roster) -> None:
    """It is the one thing on this list that is still true."""
    r, c = roster
    for i in range(60):
        r.spawn(f"done-{i}", "Explore", "old")
        r.finish(f"done-{i}")
    r.spawn("live", "Review", "still working")
    for i in range(60, 120):
        r.spawn(f"done-{i}", "Explore", "old")
        r.finish(f"done-{i}")
    assert any(e.agent_id == "live" for e in r.entries)


# --------------------------------------------------------------------------
# selection survives the roster changing
# --------------------------------------------------------------------------

def test_selection_is_held_by_ID_not_index(roster) -> None:
    """The operator can be pointing at row 3 when an agent two rows above
    finishes. An index would move the cursor onto a different agent at the
    exact moment they press Enter."""
    r, c = roster
    r.spawn("a", "Explore", "first")
    r.spawn("b", "Review", "second")
    r.select(2)                                    # main → a → b
    assert r.selected.agent_id == "b"
    r.spawn("c", "Explore", "third")               # roster grows
    r.finish("a")                                  # one above changes state
    assert r.selected.agent_id == "b", "the cursor moved under the operator"


def test_selection_clamps_at_both_ends(roster) -> None:
    r, _c = roster
    r.spawn("a", "Explore", "goal")
    r.select(-5)
    assert r.selected is None, "row 0 is main"
    r.select(99)
    assert r.selected.agent_id == "a"


def test_the_cursor_is_drawn_where_it_points(roster) -> None:
    r, _c = roster
    r.spawn("a", "Explore", "goal")
    r.select(1)
    lines = r.render()
    assert any(ln.startswith("❯") and "Explore" in ln for ln in lines)
    assert any(ln.strip().endswith("main") and not ln.startswith("❯")
               for ln in lines)


# --------------------------------------------------------------------------
# the notice
# --------------------------------------------------------------------------

def test_the_finished_notice_quotes_the_GOAL(roster) -> None:
    """That is what the operator asked for and will recognise; the id means
    nothing to them."""
    r, c = roster
    r.spawn("sub-3f2a", "Explore", "Map ov completion architecture")
    c.now += 339
    notice = r.finished_notice(r.finish("sub-3f2a"))
    assert notice == (
        '⏺ Agent "Map ov completion architecture" finished · 5m 39s'
    )


def test_a_failed_agent_says_failed(roster) -> None:
    r, c = roster
    r.spawn("a", "Review", "verify the fix")
    c.now += 61
    notice = r.finished_notice(r.finish("a", "failed", detail="timeout"))
    assert "failed" in notice and "timeout" in notice


def test_finishing_twice_announces_once(roster) -> None:
    """The result renderer can fire again on a retry path; a second notice
    would tell the operator an agent finished that already had."""
    r, _c = roster
    r.spawn("a", "Explore", "goal")
    assert r.finish("a") is not None
    assert r.finish("a") is None


@pytest.mark.parametrize("seconds,expected", [
    (0, "0s"), (39, "39s"), (60, "1m 00s"), (339, "5m 39s"),
    (3600, "1h 00m"), (7325, "2h 02m"),
])
def test_duration_formatting(seconds: float, expected: str) -> None:
    assert format_duration(seconds) == expected


# --------------------------------------------------------------------------
# robustness — this renders under the prompt the operator reads constantly
# --------------------------------------------------------------------------

@pytest.mark.parametrize("junk", [None, "", "   "])
def test_an_unidentifiable_spawn_is_refused(junk) -> None:
    r = AgentRoster()
    r.spawn(junk, "Explore", "goal")
    assert r.entries == []


def test_a_long_goal_is_clipped(roster) -> None:
    r, _c = roster
    r.spawn("a", "Explore", "x" * 500)
    assert all(len(ln) < 200 for ln in r.render())


def test_the_kill_switch(monkeypatch: pytest.MonkeyPatch, roster) -> None:
    r, _c = roster
    r.spawn("a", "Explore", "goal")
    monkeypatch.setenv("JARVIS_AGENT_VIEW_ENABLED", "0")
    assert r.render() == []
