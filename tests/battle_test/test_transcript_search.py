"""Finding a line you already saw, after the alternate screen took Cmd+F.

Full-screen rendering bought a fixed viewport and cost the terminal's own
search — the deck lives in the alternate buffer, so `Cmd+F` and tmux copy
mode cannot see it. `canvas_viewport` gave a way to SCROLL 20k lines, which
is enough to re-read something whose position you know and useless for
finding something you only remember the words of.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.battle_test.canvas_viewport import CanvasViewport
from backend.core.ouroboros.battle_test.transcript_search import (
    TranscriptSearch, find_matches, search_enabled, smart_case,
)


def _deck(n: int = 500) -> list:
    lines = [f"line {i}" for i in range(n)]
    lines[42] = "ERROR containment failed"
    lines[300] = "error in the gate"
    lines[420] = "another error here"
    return lines


def _wired():
    lines = _deck()
    viewport = CanvasViewport()
    viewport.window(lines, 20, appended=len(lines))
    return lines, viewport, TranscriptSearch(viewport)


# --------------------------------------------------------------------------
# finding
# --------------------------------------------------------------------------

def test_it_finds_every_occurrence() -> None:
    lines, _v, search = _wired()
    assert search.search(lines, "error") == 3
    assert search.matches == [42, 300, 420]


def test_a_lowercase_query_matches_loosely() -> None:
    assert smart_case("error") is False
    assert len(find_matches(_deck(), "error")) == 3


def test_a_CAPITAL_makes_it_literal() -> None:
    """Typing an uppercase letter is a deliberate act, and ignoring it makes
    `Error` unfindable among a thousand `error`s."""
    assert smart_case("ERROR") is True
    assert find_matches(_deck(), "ERROR") == [42]


def test_the_query_is_a_SUBSTRING_never_a_pattern() -> None:
    """An operator searching for `_contained(p, roots)` means exactly that.
    A regex engine would either raise on the parenthesis or match something
    they did not ask for — and both look like the search is broken rather
    than the query being clever."""
    lines = ["if not _contained(p, roots):", "unrelated"]
    assert find_matches(lines, "_contained(p, roots)") == [0]
    assert find_matches(lines, "(") == [0]     # no regex error
    assert find_matches(lines, "a.c") == []    # '.' is not "any char"


def test_an_empty_query_finds_nothing() -> None:
    assert find_matches(_deck(), "") == []
    assert find_matches(_deck(), "   ") == []


def test_a_long_query_is_bounded() -> None:
    """A paste into the search bar must not turn every keystroke into an
    unbounded scan."""
    _lines, _v, search = _wired()
    search.search(_deck(), "x" * 5000)
    assert len(search.query) <= 200


# --------------------------------------------------------------------------
# stepping
# --------------------------------------------------------------------------

def test_n_walks_the_matches_and_WRAPS() -> None:
    """A search that stops at the last match leaves the operator pressing a
    key that does nothing, unable to tell "no more" from "broken"."""
    lines, _v, search = _wired()
    search.search(lines, "error")
    assert [search.step() for _ in range(3)] == [300, 420, 42]


def test_N_walks_backwards() -> None:
    lines, _v, search = _wired()
    search.search(lines, "error")
    assert search.step(forward=False) == 420


def test_stepping_with_no_matches_is_a_no_op() -> None:
    lines, _v, search = _wired()
    search.search(lines, "nothing-matches-this")
    assert search.step() is None


def test_matches_are_INDICES_not_text() -> None:
    """The deck grows while you read it. Holding matched TEXT would let an
    identical line arriving later steal the cursor; holding a screen offset
    would shift what `n` means on every append."""
    lines, _v, search = _wired()
    search.search(lines, "error")
    before = search.matches
    lines.extend(["error appears again"] * 40)      # organism keeps emitting
    assert [search.step() for _ in range(3)] == [300, 420, 42]
    assert search.matches == before


# --------------------------------------------------------------------------
# moving the view
# --------------------------------------------------------------------------

def test_a_match_is_revealed_with_context_above_it() -> None:
    """Centred rather than top-aligned: the line BEFORE a match is very often
    what the operator was actually looking for."""
    lines, viewport, search = _wired()
    search.search(lines, "ERROR")
    assert search.reveal(search.current, len(lines), 20) is True
    visible, above, _below = viewport.window(lines, 20)
    assert "ERROR containment failed" in visible
    assert above > 0, "the match was pinned to the top with no context"


def test_revealing_a_match_already_on_screen_does_not_jump() -> None:
    lines, _v, search = _wired()
    search.search(lines, "error")
    search.reveal(search.current, len(lines), 20)
    assert search.reveal(search.current, len(lines), 20) is False


def test_a_short_deck_needs_no_scrolling() -> None:
    search = TranscriptSearch(CanvasViewport())
    assert search.offset_for(2, total=5, budget=20) == 0


# --------------------------------------------------------------------------
# cancelling
# --------------------------------------------------------------------------

def test_escape_puts_the_operator_back_where_they_were() -> None:
    """Someone who searched, found nothing useful, and pressed Escape has not
    asked to be moved. Losing your place is what makes people stop searching."""
    lines, viewport, search = _wired()
    viewport.page(1, total=len(lines), budget=20)
    parked = viewport.offset
    search.begin()
    search.search(lines, "ERROR")
    search.reveal(search.current, len(lines), 20)
    assert viewport.offset != parked
    search.cancel()
    assert viewport.offset == parked
    assert search.active is False


# --------------------------------------------------------------------------
# saying what happened
# --------------------------------------------------------------------------

def test_the_status_counts_matches() -> None:
    lines, _v, search = _wired()
    search.search(lines, "error")
    assert search.status() == "/error  1/3"
    search.step()
    assert search.status() == "/error  2/3"


def test_a_miss_is_said_OUT_LOUD() -> None:
    """Silence is indistinguishable from a key that did not register, and the
    operator retypes the query instead of trying a different one."""
    lines, _v, search = _wired()
    search.search(lines, "nothing-matches-this")
    assert "no matches" in search.status()


def test_it_reuses_the_EXISTING_viewport() -> None:
    """A search moving the view by its own arithmetic would immediately
    disagree with the scroll authority that holds a window still while the
    organism appends."""
    import ast
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    src = (repo / "backend/core/ouroboros/battle_test/"
           "transcript_search.py").read_text()
    classes = [n for n in ast.walk(ast.parse(src))
               if isinstance(n, ast.ClassDef)]
    assert [c.name for c in classes] == ["TranscriptSearch"], (
        "a second viewport implementation appeared"
    )


@pytest.mark.parametrize("junk", [None, 42, object()])
def test_junk_never_raises(junk) -> None:
    assert find_matches(_deck(), junk) == [] or isinstance(
        find_matches(_deck(), junk), list,
    )
    search = TranscriptSearch(None)
    assert search.search(junk, "x") == 0


def test_the_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_TRANSCRIPT_SEARCH_ENABLED", "0")
    assert search_enabled() is False
    lines, _v, search = _wired()
    assert search.search(lines, "error") == 0
