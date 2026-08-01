"""What a verb is FOR, in one line, in the operator's voice.

The palette rendered rows like::

    /breadcrumbs   Parse /breadcrumbs and set/show the feed verbosity

A docstring is written for an implementer and it shows: it opens with the
verb the FUNCTION performs rather than the one the operator does, then
repeats the verb name that is already the left-hand column.

Normalising is SUBTRACTIVE only. No word appears that the author did not
write, because a palette that paraphrases can be confidently wrong and the
operator acts on it.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.battle_test.verb_description import (
    describe_width, to_operator_voice,
)


@pytest.mark.parametrize("text,verb,expected", [
    ("Parse /breadcrumbs and set/show the feed verbosity", "breadcrumbs",
     "Set/show the feed verbosity"),
    ("Dispatch /vision subcommands to the vision REPL.", "vision",
     "Subcommands to the vision REPL"),
    ("Show the current governor caps and recent throttles.", "governor",
     "Show the current governor caps and recent throttles"),
])
def test_implementation_openers_are_stripped(text, verb, expected) -> None:
    assert to_operator_voice(text, verb) == expected


def test_a_NAMED_verb_phrase_goes_entirely() -> None:
    """"the /posture verb — status, override" is naming the verb, which is
    already the left-hand column."""
    assert to_operator_voice(
        "Handle the /posture verb — status, override, history.", "posture",
    ) == "Status, override, history"


def test_an_ACTING_verb_name_SURVIVES() -> None:
    """THE case that destroyed sentences. In "/attach a file or image" the
    name IS the sentence's verb — deleting it produced "File or image to the
    next generation", a fragment about files rather than an instruction to
    attach one. The discriminator is what follows: an article or preposition
    means it was doing grammatical work."""
    assert to_operator_voice(
        "/attach a file or image to the next generation.", "attach",
    ) == "Attach a file or image to the next generation"
    assert to_operator_voice(
        "/cancel an in-flight operation by id.", "cancel",
    ) == "Cancel an in-flight operation by id"


def test_nothing_is_invented() -> None:
    """A palette that paraphrases can be confidently wrong."""
    source = "Show the current governor caps and recent throttles."
    result = to_operator_voice(source, "governor")
    for word in result.rstrip("…").split():
        assert word.lower().strip(".,;:") in source.lower(), (
            f"the word {word!r} was not written by the author"
        )


def test_only_the_first_sentence_is_used() -> None:
    """A palette row is one line; everything after the first full stop is
    detail nobody asked for while scanning."""
    result = to_operator_voice(
        "Show the posture. It also writes an audit entry and emits SSE.",
        "posture",
    )
    assert "audit" not in result


def test_capitals_that_carry_meaning_survive() -> None:
    """Title-casing would mangle identifiers; lowering would mangle DW, L2
    and APPROVAL_REQUIRED."""
    result = to_operator_voice(
        "Return the DW provider liquidity for each lane.", "provider",
    )
    assert "DW" in result


def test_a_fragment_of_a_REAL_sentence_falls_back_to_the_author() -> None:
    """When subtraction eats a genuine description, a slightly awkward true
    line beats a tidy invented one."""
    assert to_operator_voice(
        "Handle the posture verb and show what it decided.", "posture",
    ) != ""


def test_a_docstring_that_never_HAD_a_description_returns_nothing() -> None:
    """Distinct from the case above, and the distinction is the whole point.
    "Parse" is not a mangled description — it is the absence of one, and
    returning it would put a fragment in the palette where a subcommand list
    would at least have been informative."""
    assert to_operator_voice("Parse", "parse") == ""
    assert to_operator_voice("/posture", "posture") == ""


def test_it_is_clipped_but_never_mid_punctuation() -> None:
    long = "Show " + "the current governor caps " * 12
    result = to_operator_voice(long, "governor")
    assert len(result) <= describe_width()
    assert result.endswith("…")


def test_the_width_is_tunable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_PALETTE_DESC_WIDTH", "30")
    assert describe_width() == 30


@pytest.mark.parametrize("junk", [None, "", "   ", 42])
def test_junk_never_raises(junk) -> None:
    assert isinstance(to_operator_voice(junk, "x"), str)


def test_the_palette_uses_it() -> None:
    import ast
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    src = (repo / "backend/core/ouroboros/battle_test/"
           "repl_completion.py").read_text()
    names = {a.name for n in ast.walk(ast.parse(src))
             if isinstance(n, ast.ImportFrom) for a in n.names}
    assert "to_operator_voice" in names


def test_subcommand_rows_read_as_a_LIST_not_a_usage_string() -> None:
    """A pipe borrows grammar from a usage string. These rows are the honest
    answer to "nobody wrote a description, but it does accept these".

    SUPERSEDED 2026-07-31: was ``assert '" · ".join(mined)' in src`` — a grep
    of `repl_completion.py`'s own source text.

    That assertion tested a SPELLING, not a separator. It broke the moment the
    join moved inside a deferred supplier (same join, same middot, different
    local name) and it would equally have PASSED had the line been dead code,
    commented out, or in a branch nothing reaches. A source-text assertion
    cannot tell "this behaviour holds" from "this string is present".

    Rewritten against the rendered row: build a dispatcher whose only
    resolvable help is its mined vocabulary, and read what the palette shows.
    """
    def dispatch_widget_command(line: str):
        # No prose anywhere -- the ONLY thing resolvable is the vocabulary the
        # body compares against, which is the rung under test.
        sub = (line or "").split()[-1]
        if sub == "status":
            return "s"
        if sub == "history":
            return "h"
        if sub == "explain":
            return "e"
        return ""

    from backend.core.ouroboros.battle_test import repl_completion

    row = repl_completion._describe(dispatch_widget_command)
    assert "·" in row, row
    assert "|" not in row, row
    assert "status" in row and "history" in row


# --------------------------------------------------------------------------
# a docstring existing is not a description existing
# --------------------------------------------------------------------------

def test_rst_literal_markup_is_unwrapped() -> None:
    """Every rule here matches on WORDS, and ``/anticipate`` is not a word.
    The verb-name check silently failed on it and left "``/anticipate`` line"
    as the description. Docstrings in this codebase mark up verb names by
    convention, so this is the common case rather than an edge one."""
    assert to_operator_voice(
        "Parse ``/breadcrumbs`` and set/show the feed verbosity.",
        "breadcrumbs",
    ) == "Set/show the feed verbosity"


@pytest.mark.parametrize("doc,verb", [
    ("Parse ``/anticipate`` line. NEVER raises.", "anticipate"),
    ("§32.11 Slice 4 canonical entry point — auto-discovered.", "autobiography"),
    ("Parse a ``/backlog auto-proposed ...`` line and dispatch.",
     "backlog_auto_proposed"),
    ("Dispatch a ``/postmortems dag ...`` subcommand.", "dag"),
])
def test_an_implementation_contract_is_REFUSED(doc: str, verb: str) -> None:
    """THE conflation that produced "78/78 documented" while the palette
    still showed subcommand lists. "Parse ``/anticipate`` line" normalises to
    "Line" — not a short description, but the ABSENCE of one wearing a
    capital letter. Refusing lets the resolver fall through to a rung that at
    least says what the verb accepts."""
    assert to_operator_voice(doc, verb) == ""


def test_a_real_description_is_not_refused() -> None:
    """The rejection must not be so eager that it eats working prose."""
    for doc, verb in [
        ("Show the current governor caps and recent throttles.", "governor"),
        ("Browse, bookmark and replay past sessions.", "session"),
        ("Set/show the feed verbosity.", "breadcrumbs"),
    ]:
        assert to_operator_voice(doc, verb) != ""


def test_every_palette_verb_has_a_real_description() -> None:
    """THE invariant, asserted on what the resolver actually produces rather
    than on whether a docstring exists — because those are different facts
    and conflating them is what let this ship twice."""
    import ast
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    bare = []
    for path in (repo / "backend/core/ouroboros").rglob("*.py"):
        try:
            tree = ast.parse(path.read_text())
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not (node.name.startswith("dispatch_")
                    and node.name.endswith("_command")):
                continue
            verb = node.name[len("dispatch_"):-len("_command")]
            doc = ast.get_docstring(node) or ""
            first = " ".join(doc.strip().splitlines()[0].split()) if doc else ""
            if not to_operator_voice(first, verb):
                bare.append(verb)
    assert bare == [], (
        "these verbs render as a subcommand list because their docstring "
        "describes the FUNCTION, not the verb:\n  " + "\n  ".join(sorted(bare))
    )
