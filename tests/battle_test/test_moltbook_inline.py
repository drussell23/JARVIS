"""The room's commentary, attached to the work it is about.

Moltbook posts already carry `op_id` — the schema was built for attribution —
so a post BELONGS to an operation and renders under it in the same `⎿`
grammar a tool result uses. Adjacency is the value: the same sentence in a
side panel is decoration; under the failure it is commentary.

The race this must survive: a reaction is formulated ASYNCHRONOUSLY, so by
the time it lands its parent may have scrolled out of the window or been
evicted from the ring. Mutating a committed region is how a TUI tears.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.battle_test.moltbook_inline import (
    GHOST, INLINE, MUTED, decide_placement, find_anchor, inline_enabled,
    is_conflict, posture_allows, render_ghost, render_post, render_thread,
)

_OP = "iron-gate:0198e4c1-7759-86"


def _deck(n: int = 40) -> list:
    lines = [f"line {i}" for i in range(n)]
    lines[10] = f"⏺ Validate({_OP})"
    lines[11] = "  ⎿ ✗ 3 failed · test_scoped_paths"
    return lines


def _post(body: str = "three tests. you broke the file.", **kw) -> dict:
    base = {"handle": "@the-pit", "body": body, "op_id": _OP,
            "ref": "m-418", "kind": "banter"}
    base.update(kw)
    return base


# --------------------------------------------------------------------------
# the mandate's two assertions
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_reaction_to_a_VISIBLE_op_injects_inline() -> None:
    deck = _deck()
    placement = decide_placement(_post(), deck, window=(0, 20))
    assert placement.kind == INLINE
    assert placement.anchor_index == 11, "should anchor after the op's LAST line"

    rows = render_post(_post())
    assert rows and "💬" in rows[0] and "@the-pit" in rows[0]


@pytest.mark.asyncio
async def test_an_OUT_OF_BOUNDS_op_routes_to_the_ghost_pointer() -> None:
    """No geometry exception, no inline mutation — the parent has scrolled
    away and writing into it is what tears a frame."""
    deck = _deck()
    placement = decide_placement(_post(), deck, window=(30, 40))
    assert placement.kind == GHOST
    assert placement.reason == "anchor_offscreen"

    ghost = render_ghost(_post())
    assert "@the-pit" in ghost and "↑" in ghost
    assert "7759-86" in ghost, "the operator must be able to find it"


# --------------------------------------------------------------------------
# anchoring
# --------------------------------------------------------------------------

def test_it_anchors_to_the_ops_LAST_line() -> None:
    """An op emits several lines; a comment belongs after everything it has
    said, not wedged under its opening line."""
    assert find_anchor(_deck(), _OP) == 11


def test_an_evicted_op_ghosts_rather_than_guessing() -> None:
    placement = decide_placement(_post(), ["unrelated"] * 20, window=(0, 20))
    assert placement.kind == GHOST
    assert placement.reason == "anchor_evicted"


def test_an_unattributed_post_never_lands_inline() -> None:
    """With no op_id it cannot be commentary on anything, so inline it would
    sit beside work it has nothing to do with."""
    placement = decide_placement(_post(op_id=""), _deck(), window=(0, 20))
    assert placement.kind == GHOST
    assert placement.reason == "unattributed"


def test_no_window_means_no_bounds_check() -> None:
    """A caller without a viewport (tests, headless) still gets inline."""
    assert decide_placement(_post(), _deck()).kind == INLINE


# --------------------------------------------------------------------------
# reading the room
# --------------------------------------------------------------------------

def test_HARDEN_mutes_banter() -> None:
    """Under an incident, banter competes for attention with the thing going
    wrong."""
    placement = decide_placement(_post(), _deck(), window=(0, 20),
                                 posture="HARDEN")
    assert placement.kind == MUTED
    assert placement.reason == "posture_quiet"


def test_HARDEN_never_mutes_a_CONFLICT() -> None:
    """REVIEW contesting GENERATE is not banter — it is the system reporting
    that its own components disagree, which is what an incident needs."""
    contest = _post("⚔ the containment check is wrong", kind="conflict")
    placement = decide_placement(contest, _deck(), window=(0, 20),
                                 posture="HARDEN")
    assert placement.kind == INLINE


def test_other_postures_let_the_room_speak() -> None:
    for posture in ("EXPLORE", "CONSOLIDATE", "MAINTAIN", ""):
        assert posture_allows(_post(), posture) is True


def test_an_unknown_posture_does_not_silence() -> None:
    """The gate exists to quiet noise during trouble, not to demand proof of
    calm."""
    assert posture_allows(_post(), "SOMETHING_NEW") is True


@pytest.mark.parametrize("body,kind,expected", [
    ("⚔ contested", "banter", True),
    ("plain talk", "conflict", True),
    ("plain talk", "banter", False),
])
def test_conflict_detection(body: str, kind: str, expected: bool) -> None:
    assert is_conflict({"body": body, "kind": kind}) is expected


# --------------------------------------------------------------------------
# threading
# --------------------------------------------------------------------------

def test_a_reply_is_indented_under_its_parent() -> None:
    """Threading falls out of the same column discipline tool results use —
    no second layout vocabulary to learn."""
    parent = render_post(_post(), depth=0)[0]
    reply = render_post(_post(handle="@the-builder"), depth=1)[0]
    assert reply.startswith(" " * (len(parent) - len(parent.lstrip()) + 1))
    assert "▸" in reply


def test_a_long_argument_folds() -> None:
    """An argument must never bury the diff it is about."""
    thread = [_post()] + [_post(handle=f"@r{i}") for i in range(6)]
    rows = render_thread(thread)
    assert any("more replies" in r for r in rows)
    assert len(rows) <= 5


def test_a_short_thread_does_not_fold() -> None:
    rows = render_thread([_post(), _post(handle="@b")])
    assert not any("more repl" in r for r in rows)


def test_a_long_body_is_clipped_not_wrapped() -> None:
    """A post must occupy one deck line — wrapping would make commentary
    outweigh the work it comments on."""
    rows = render_post(_post("x " * 300))
    assert len(rows) == 1
    assert rows[0].endswith("m-418") or "…" in rows[0]


# --------------------------------------------------------------------------
# it can never break the deck
# --------------------------------------------------------------------------

@pytest.mark.parametrize("junk", [None, {}, "string", 42, {"op_id": None}])
def test_junk_never_raises(junk) -> None:
    placement = decide_placement(junk, _deck(), window=(0, 20))
    assert placement.kind in (INLINE, GHOST, MUTED)
    assert isinstance(render_post(junk), list)
    assert isinstance(render_ghost(junk), str)


def test_the_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_MOLTBOOK_INLINE_ENABLED", "0")
    assert inline_enabled() is False
    assert decide_placement(_post(), _deck(), window=(0, 20)).kind == MUTED


def test_it_reuses_the_op_line_seam() -> None:
    """One renderer, not two — a comment travels the mirror, the op buffer
    and /expand exactly as tool chrome does."""
    import inspect

    from backend.core.ouroboros.battle_test.serpent_flow import SerpentFlow

    src = inspect.getsource(SerpentFlow.post_inline)
    assert "self._op_line(" in src


def test_the_reaction_engine_is_posture_gated() -> None:
    """Refused at the ENGINE, not just the renderer: a muted post would still
    have cost a model call and consumed the resident's cooldown."""
    import inspect

    from backend.core.ouroboros.governance import moltbook

    src = inspect.getsource(moltbook._reply_budget_ok)
    assert "_posture_permits_banter" in src
