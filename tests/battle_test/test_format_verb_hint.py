"""Regression spine for §41.3 #19 — `format_verb_hint` substrate.

The hint composer renders a compact usage+example block from an
existing :class:`VerbDescriptor` — no new registry, no hardcoded
verb→hint map. Used by the unknown-verb typo-suggestion path to
surface the descriptor's data inline instead of a generic
"append --help" instruction."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from backend.core.ouroboros.battle_test.repl_completion import (
    VerbCategory,
    VerbDescriptor,
    format_verb_hint,
)


def _verb(
    *,
    slash: str = "/cancel",
    description: str = "Cancel an op",
    arg_spec: str = "<op_id> [--immediate]",
    examples: tuple = ("/cancel op-abc",),
    aliases: tuple = (),
) -> VerbDescriptor:
    return VerbDescriptor(
        slash_form=slash,
        handler_method="",
        description=description,
        arg_spec=arg_spec,
        examples=examples,
        aliases=aliases,
        category=VerbCategory.LIFECYCLE,
    )


# --- Happy path -----------------------------------------------------------


def test_hint_renders_usage_line():
    text = format_verb_hint(_verb())
    assert "usage: /cancel <op_id> [--immediate]" in text


def test_hint_includes_one_example_by_default():
    text = format_verb_hint(_verb())
    assert "example: /cancel op-abc" in text


def test_hint_compact_single_block():
    """Two lines for usage+1 example: no blank padding, no extra
    sections (aliases, description) — that's `format_verb_help`'s
    job. The hint is for inline injection into other messages."""
    text = format_verb_hint(_verb())
    lines = text.splitlines()
    assert len(lines) == 2


def test_hint_omits_aliases():
    """Aliases belong in --help output, not the inline hint."""
    text = format_verb_hint(_verb(aliases=("/stop",)))
    assert "/stop" not in text


def test_hint_omits_description():
    """Description belongs in --help output, not the inline hint."""
    text = format_verb_hint(_verb(description="VERY UNIQUE DESC"))
    assert "VERY UNIQUE DESC" not in text


def test_hint_indent_default_two_spaces():
    text = format_verb_hint(_verb())
    for line in text.splitlines():
        assert line.startswith("  "), line


def test_hint_indent_override():
    text = format_verb_hint(_verb(), indent="    ")
    for line in text.splitlines():
        assert line.startswith("    "), line


# --- max_examples knob ----------------------------------------------------


def test_hint_max_examples_zero_drops_examples():
    text = format_verb_hint(
        _verb(examples=("/cancel a", "/cancel b")),
        max_examples=0,
    )
    assert "example:" not in text


def test_hint_max_examples_two_shows_both():
    text = format_verb_hint(
        _verb(examples=("/cancel a", "/cancel b", "/cancel c")),
        max_examples=2,
    )
    assert "/cancel a" in text
    assert "/cancel b" in text
    assert "/cancel c" not in text


def test_hint_max_examples_capacity_exceeds_available():
    """max_examples=10 but only 1 example provided — shows the 1."""
    text = format_verb_hint(
        _verb(examples=("/cancel a",)),
        max_examples=10,
    )
    assert "/cancel a" in text


def test_hint_negative_max_examples_clamps_to_zero():
    text = format_verb_hint(_verb(), max_examples=-5)
    assert "example:" not in text


# --- Edge cases & resilience ---------------------------------------------


def test_hint_no_arg_spec_still_shows_slash():
    text = format_verb_hint(_verb(arg_spec=""))
    assert "/cancel" in text
    assert "usage:" in text


def test_hint_no_examples_shows_usage_only():
    text = format_verb_hint(_verb(examples=()))
    assert "usage:" in text
    assert "example:" not in text


def test_hint_minimal_descriptor():
    minimal = VerbDescriptor(
        slash_form="/x", handler_method="", description="",
    )
    text = format_verb_hint(minimal)
    assert "/x" in text


def test_hint_garbage_input_returns_empty_or_safe():
    """NEVER raises — non-VerbDescriptor inputs degrade."""
    assert format_verb_hint(None) == ""  # type: ignore[arg-type]
    assert format_verb_hint("not a verb") == ""  # type: ignore[arg-type]
    assert format_verb_hint(42) == ""  # type: ignore[arg-type]


def test_hint_handles_attribute_access_error():
    """If a buggy descriptor somehow gets past the type check,
    the catch-all returns empty rather than raising."""

    class _Bogus:
        @property
        def slash_form(self):
            raise RuntimeError("boom")

    # Not a VerbDescriptor → first guard catches it
    assert format_verb_hint(_Bogus()) == ""  # type: ignore[arg-type]


# --- AST pin: format_verb_hint exported -----------------------------------


def test_ast_pin_format_verb_hint_exported():
    src = Path(
        "backend/core/ouroboros/battle_test/repl_completion.py"
    ).read_text()
    assert '"format_verb_hint"' in src


def test_ast_pin_format_verb_hint_composes_descriptor():
    """The hint composer must read from VerbDescriptor's existing
    fields (arg_spec, examples, slash_form). No second registry
    introduced."""
    src = Path(
        "backend/core/ouroboros/battle_test/repl_completion.py"
    ).read_text()
    idx = src.find("def format_verb_hint")
    assert idx > 0
    body = src[idx:idx + 2000]
    assert "verb.arg_spec" in body
    assert "verb.examples" in body
    assert "verb.slash_form" in body
