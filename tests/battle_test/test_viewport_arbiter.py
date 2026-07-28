"""What fits, and what gives way when it does not.

`LayoutController` already answers "which mode is the operator in". What
nothing asked was whether the requested layout FITS the window — and a layout
engine handed three regions on an 80-column terminal either dies on the
container maths or squeezes every region to a width where the content is
present and unreadable. A diff wrapped at 24 columns is not a diff.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.battle_test.viewport_arbiter import (
    FLOAT, HIDDEN, REGION_PRIORITY, SPLIT, ViewportArbiter, min_region_cols,
)


def _all_three() -> ViewportArbiter:
    arbiter = ViewportArbiter()
    arbiter.request("lanes", True)
    arbiter.request("transcript", True)
    return arbiter


def _placed(arbiter: ViewportArbiter, cols: int) -> dict:
    return {p.region: p for p in arbiter.arbitrate(cols)}


# --------------------------------------------------------------------------
# the mandate's two assertions
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_wide_terminal_shows_all_three_side_by_side() -> None:
    placed = _placed(_all_three(), 200)
    assert [p.placement for p in placed.values()] == [SPLIT, SPLIT, SPLIT]
    assert all(p.cols >= min_region_cols(p.region) for p in placed.values())


@pytest.mark.asyncio
async def test_a_narrow_terminal_collapses_instead_of_panicking() -> None:
    """The whole point: a geometry that cannot fit must produce a DECISION,
    not an exception."""
    placed = _placed(_all_three(), 80)
    assert placed["deck"].placement == SPLIT
    assert placed["transcript"].placement in (FLOAT, HIDDEN)
    assert all(p.placement in (SPLIT, FLOAT, HIDDEN) for p in placed.values())


# --------------------------------------------------------------------------
# nothing may exceed the window
# --------------------------------------------------------------------------

@pytest.mark.parametrize("cols", [200, 120, 100, 80, 60, 40, 24, 12, 5, 1, 0])
def test_placements_never_claim_more_columns_than_exist(cols: int) -> None:
    """A region asking for more columns than the terminal has IS the geometry
    panic this class exists to prevent — handed to the layout engine as data
    instead of raised as an exception."""
    total = sum(p.cols for p in _all_three().arbitrate(cols))
    assert total <= max(cols, 1)


def test_the_deck_survives_a_terminal_narrower_than_its_minimum() -> None:
    """Reachable at the floor: the deck is never demoted below FLOAT, so a
    20-column terminal still has to render it somehow."""
    placed = _placed(_all_three(), 20)
    assert placed["deck"].visible is True
    assert placed["deck"].cols <= 20


# --------------------------------------------------------------------------
# demotion, not refusal
# --------------------------------------------------------------------------

def test_intent_survives_the_squeeze() -> None:
    """Resizing back must restore what the operator ASKED FOR, not what
    survived — otherwise an accidental drag silently deletes their layout."""
    arbiter = _all_three()
    arbiter.arbitrate(40)
    assert arbiter.requested("transcript") is True
    restored = _placed(arbiter, 200)
    assert restored["transcript"].placement == SPLIT


def test_a_hidden_region_is_pending_not_declined() -> None:
    arbiter = _all_three()
    hidden = [p for p in arbiter.arbitrate(20) if p.placement == HIDDEN]
    assert hidden
    assert all(p.requested for p in hidden)


def test_the_lowest_priority_region_gives_way_first() -> None:
    """Explicit, because "whichever the loop reached last" is not a
    decision — it changes when someone reorders a dict."""
    placed = _placed(_all_three(), 80)
    assert placed["transcript"].placement != SPLIT
    assert placed["lanes"].placement == SPLIT
    assert REGION_PRIORITY.index("deck") < REGION_PRIORITY.index("transcript")


def test_the_deck_can_never_be_dismissed() -> None:
    """A cockpit that hides the organism's own output has stopped being a
    cockpit."""
    arbiter = ViewportArbiter()
    arbiter.request("deck", False)
    assert arbiter.requested("deck") or _placed(arbiter, 100)["deck"].visible


# --------------------------------------------------------------------------
# resize behaviour
# --------------------------------------------------------------------------

def test_promotion_needs_a_margin_so_a_drag_does_not_flicker() -> None:
    """Resize events arrive continuously while a window is dragged; a region
    that re-promotes the instant it fits oscillates across one drag."""
    arbiter = _all_three()
    arbiter.arbitrate(60)                       # transcript demoted
    boundary = sum(min_region_cols(r) for r in REGION_PRIORITY)
    at_edge = _placed(arbiter, boundary)
    assert at_edge["transcript"].placement != SPLIT, "re-promoted with no margin"
    generous = _placed(arbiter, boundary + 32)
    assert generous["transcript"].placement == SPLIT


def test_demotion_has_no_margin() -> None:
    """Becoming unreadable is urgent; becoming readable can wait."""
    arbiter = _all_three()
    arbiter.arbitrate(400)
    assert _placed(arbiter, 60)["transcript"].placement != SPLIT


def test_focus_mode_is_not_second_guessed() -> None:
    """An explicit "only this one" leaves the arbiter nothing to negotiate;
    overriding it with a heuristic would override the operator."""
    class _Controller:
        focused_region = "lanes"

    arbiter = ViewportArbiter(controller=_Controller())
    arbiter.request("lanes", True)
    arbiter.request("transcript", True)
    placed = _placed(arbiter, 200)
    assert placed["lanes"].placement == SPLIT
    assert placed["transcript"].placement == HIDDEN
    assert placed["deck"].placement == HIDDEN


# --------------------------------------------------------------------------
# configuration and robustness
# --------------------------------------------------------------------------

def test_minimums_are_tunable(monkeypatch: pytest.MonkeyPatch) -> None:
    """"Readable" depends on font and content — an operator on a dense
    terminal reads a 34-column lane list fine, and one at 20pt does not."""
    monkeypatch.setenv("JARVIS_MIN_COLS_LANES", "64")
    assert min_region_cols("lanes") == 64


def test_the_kill_switch_shows_everything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_VIEWPORT_ARBITER_ENABLED", "0")
    assert all(p.placement == SPLIT for p in _all_three().arbitrate(40))


@pytest.mark.parametrize("cols", [None, "wide", -5, 3.7])
def test_a_junk_dimension_never_raises(cols) -> None:
    """A resize must never kill the application."""
    assert isinstance(_all_three().arbitrate(cols), list)


def test_it_holds_no_widgets() -> None:
    """The value is being provable at every width WITHOUT a terminal — a
    geometry rule testable only by resizing a window is one nobody tests."""
    import ast
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    src = (repo / "backend/core/ouroboros/battle_test/"
           "viewport_arbiter.py").read_text()
    # AST, not a substring: the module's own docstring EXPLAINS that it is
    # free of prompt_toolkit, and a text search cannot tell an explanation
    # from an import.
    imported = {
        (n.module or "") for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.ImportFrom)
    } | {
        a.name for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.Import) for a in n.names
    }
    assert not any("prompt_toolkit" in name for name in imported)
