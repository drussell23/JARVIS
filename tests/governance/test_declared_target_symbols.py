"""Operator-declared repair targets: signed intent outranks inference.

`resolve_target_symbols` had two ways to find a repair target — a stack trace
(evidence) and goal-text keyword scoring (a guess) — and no way to be TOLD one.
So a roadmap goal could DECLARE its target and still be resolved by keyword
matching, which is what happened in soak bt-2026-08-28-115654: a goal naming
`_should_use_lean_prompt` resolved to `_read_with_truncation` at confidence
0.50, because the only signal reaching the resolver was prose and the prose
said "truncation" more often than it said the symbol's name.

The declaration was not missing. `_build_signing_payload` signs the whole
`goals` list, so `target_symbol` was covered by the HMAC — and then dropped by
`RoadmapGoal`, which had no such field. Signed intent was being discarded one
layer below the signature that protected it.

Consequence, on the goal that motivated this: the op was scoped to
providers.py entire (10,923 lines, ~126,600 tokens) against a 32,768-token
local model — a 3.9x overrun that makes truncation arithmetic, not luck. The
declared symbol is 75 lines, ~750 tokens: a 169x reduction.
"""
from __future__ import annotations

import textwrap

import pytest

from backend.core.ouroboros.governance.target_symbol_resolver import (
    METHOD_DECLARED,
    METHOD_GOAL_KEYWORD,
    METHOD_STACK_TRACE,
    resolve_target_symbols,
)

# Deliberately keyword-hostile: the prose is dense with words matching the
# WRONG symbol, so a passing result cannot be keyword scoring getting lucky.
_SOURCE = textwrap.dedent('''
    def _read_with_truncation(path):
        """Truncation truncation truncation."""
        return path

    def _should_use_lean_prompt(ctx):
        """The symbol the operator actually declared."""
        return bool(ctx)

    class Helper:
        def truncate_helper(self):
            return 1
''')

_HOSTILE_GOAL = "fix the truncation truncation truncation behaviour"


def test_declared_symbol_beats_keyword_inference():
    """The regression: a declaration must not lose to prose."""
    inferred = resolve_target_symbols(
        source=_SOURCE, file_path="providers.py", goal=_HOSTILE_GOAL,
    )
    declared = resolve_target_symbols(
        source=_SOURCE, file_path="providers.py", goal=_HOSTILE_GOAL,
        declared_symbols=("_should_use_lean_prompt",),
    )

    assert declared.method == METHOD_DECLARED
    assert "_should_use_lean_prompt" in declared.primary
    # And it genuinely changed the outcome — otherwise this test proves nothing.
    assert inferred.primary != declared.primary


def test_declared_symbol_is_confidence_one():
    """A declaration is a fact, like a stack trace — not a score.

    It must not be filterable by a confidence floor an operator raised to
    suppress weak keyword guesses.
    """
    res = resolve_target_symbols(
        source=_SOURCE, file_path="providers.py", goal=_HOSTILE_GOAL,
        declared_symbols=("_should_use_lean_prompt",),
        min_confidence=0.99,
    )

    assert res.confidence == 1.0
    assert res.method == METHOD_DECLARED


def test_a_declaration_naming_nothing_here_degrades_to_inference():
    """A typo must not fabricate a target.

    Matching is against the file's OWN symbol index, so a name absent from
    this file resolves nothing and the inference passes still run — the
    failure mode of a wrong declaration is "no worse than before", never
    "confidently wrong".
    """
    res = resolve_target_symbols(
        source=_SOURCE, file_path="providers.py", goal=_HOSTILE_GOAL,
        declared_symbols=("_no_such_symbol_anywhere",),
    )

    assert res.method != METHOD_DECLARED
    assert "_no_such_symbol_anywhere" not in res.primary


def test_declaration_does_not_absorb_unrelated_stack_frames():
    """A declared target must not be widened by frames it did not ask for.

    `if primaries:` after the stack-trace pass would otherwise relabel a
    DECLARED result as STACK_TRACE with zero frames parsed — a resolution
    reporting a provenance it does not have.
    """
    frames = ['File "providers.py", line 2, in _read_with_truncation']
    res = resolve_target_symbols(
        source=_SOURCE, file_path="providers.py", goal=_HOSTILE_GOAL,
        traceback_frames=frames,
        declared_symbols=("_should_use_lean_prompt",),
    )

    assert res.method == METHOD_DECLARED
    assert "_read_with_truncation" not in res.primary


def test_no_declaration_leaves_the_cascade_untouched():
    """Every op without a roadmap claim — nearly all of them — is unchanged."""
    res = resolve_target_symbols(
        source=_SOURCE, file_path="providers.py",
        traceback_frames=['File "providers.py", line 2, in _read_with_truncation'],
    )

    assert res.method == METHOD_STACK_TRACE
    assert res.confidence == 1.0


@pytest.mark.parametrize("declared", [(), None, ("",), ("   ",)])
def test_empty_declarations_are_inert(declared):
    """Blank/absent declarations must not short-circuit inference."""
    res = resolve_target_symbols(
        source=_SOURCE, file_path="providers.py", goal="lean prompt",
        declared_symbols=declared,
    )

    assert res.method in (METHOD_GOAL_KEYWORD, "unresolved")


# ---------------------------------------------------------------------------
# Reader: the field must survive parsing, in either spelling
# ---------------------------------------------------------------------------


def _goal_from(entry: dict):
    from backend.core.ouroboros.governance.roadmap_reader import (
        _parse_goal_entry,
    )
    return _parse_goal_entry(entry)


@pytest.mark.parametrize("key", ["target_symbol", "target_symbols"])
def test_reader_accepts_both_spellings(key):
    """An operator writing ONE symbol should not have to know the field is
    plural. A schema that punishes the singular is one people get wrong
    silently — and silence is what this whole defect was made of."""
    goal = _goal_from({
        "id": "g1", "title": "t", "description": "d",
        "target_files": ["backend/x.py"],
        key: "_should_use_lean_prompt",
    })

    assert goal is not None
    assert goal.target_symbols == ("_should_use_lean_prompt",)


def test_reader_defaults_to_empty_for_older_roadmaps():
    """A roadmap written before this field parses and behaves as before."""
    goal = _goal_from({
        "id": "g1", "title": "t", "description": "d",
        "target_files": ["backend/x.py"],
    })

    assert goal is not None
    assert goal.target_symbols == ()
