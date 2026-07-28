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


def test_a_fragment_falls_back_to_the_authors_words() -> None:
    """When subtraction eats the sentence, a slightly awkward true line
    beats a tidy invented one."""
    assert to_operator_voice("Parse", "parse") != ""
    assert to_operator_voice("/posture", "posture") != ""


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
    answer to "nobody wrote a description, but it does accept these"."""
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    src = (repo / "backend/core/ouroboros/battle_test/"
           "repl_completion.py").read_text()
    assert '" · ".join(mined)' in src
