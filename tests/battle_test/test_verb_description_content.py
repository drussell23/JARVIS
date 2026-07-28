"""A docstring existing is not a description existing.

`Parse a ``/canvas`` line and dispatch.` normalises to `"Line and dispatch"` —
17 characters, so it cleared the length floor and shipped as the palette's
description for months. Every word in it is machinery.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.battle_test.verb_description import (
    implementation_vocabulary, is_contentless, to_operator_voice,
)


class TestStructuralContentTest:
    @pytest.mark.parametrize("residue", [
        "Line and dispatch",
        "Handle the command and return",
        "Parse the line and dispatch to the handler",
        "Dispatches the subcommand arguments",
        "Returns the result of the call",
    ])
    def test_pure_machinery_is_contentless(self, residue):
        # Enumerating bad PHRASES is whack-a-mole: the next template produces a
        # new one and the palette lies again. The test is whether ANY word
        # names something in the operator's world.
        assert is_contentless(residue)

    @pytest.mark.parametrize("real", [
        "L1 command bus throughput and counters",
        "Browse an operation's causal lineage",
        "Set or show the feed verbosity",
        "Retrospective audit of O+V-signed commits",
    ])
    def test_real_descriptions_survive(self, real):
        # The floor must not eat legitimate lines. "command" and "line" appear
        # in machinery vocabulary, so a description containing them still has
        # to pass on the strength of its OTHER words.
        assert not is_contentless(real)

    def test_the_exact_canvas_docstring(self):
        doc = "Parse a ``/canvas`` line and dispatch. NEVER raises."
        assert to_operator_voice(doc, "canvas", 60) == ""

    def test_vocabulary_is_configurable(self, monkeypatch):
        monkeypatch.setenv("JARVIS_PALETTE_IMPL_WORDS", "widget,frobnicate")
        assert is_contentless("widget frobnicate")

    def test_a_single_domain_word_is_enough(self):
        # Conservative on purpose: one real noun beats discarding a line that
        # might be the only help an operator gets.
        assert not is_contentless("Dispatch the posture override")


class TestCascadeIsPublic:
    def test_describe_dispatcher_is_exported(self):
        # The demo needed the FULL cascade, and the only alternative was
        # importing the private `_describe` — the reach-around the risk-tier
        # ladder has an authority invariant against.
        from backend.core.ouroboros.battle_test import repl_completion
        assert callable(repl_completion.describe_dispatcher)

    def test_cascade_falls_through_to_something_useful(self):
        """A contentless docstring must not become a BLANK.

        The demo originally called `to_operator_voice` directly — the first
        rung only — and rendered nothing for every verb whose description
        lives in its module docstring or subcommands. A demo disagreeing with
        the surface it previews is the one thing a demo must never do.
        """
        from backend.core.ouroboros.battle_test.repl_completion import (
            describe_dispatcher,
        )

        def fn(line: str) -> None:
            """Parse a ``/thing`` line and dispatch. NEVER raises."""

        assert describe_dispatcher(fn) != ""
