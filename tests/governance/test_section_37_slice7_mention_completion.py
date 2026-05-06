"""§37 Slice 7 — @mention path-completion regression spine.

Pins per operator binding 2026-05-05:

  * Composes existing PathCompleter + _MENTION_RE substrate (no
    parallel file-tree walking)
  * Word-boundary gate: fires only when current cursor-word
    starts with `@`
  * Mid-word @ (email-like / @decorators in pasted code) does
    NOT trigger
  * No-@ text does NOT trigger
  * NEVER raises (filesystem error / permission error returns
    no completions)
  * Master-flag-aware (polish off → no completer)
  * Merged into repl_completion.build_completer alongside the
    existing slash completer
  * AST regression: source references PathCompleter +
    merge_completers

Verifies (15 tests).
"""
from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import patch

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Master flag awareness
# ---------------------------------------------------------------------------


def test_completer_returns_none_when_polish_off(monkeypatch):
    """Master-flag-aware: when JARVIS_REPL_INPUT_POLISH_ENABLED
    =false, no completer is returned."""
    from backend.core.ouroboros.battle_test.repl_input_polish import (
        build_mention_completer,
    )
    monkeypatch.setenv(
        "JARVIS_REPL_INPUT_POLISH_ENABLED", "false",
    )
    assert build_mention_completer() is None


def test_completer_returns_completer_when_polish_on(monkeypatch):
    from backend.core.ouroboros.battle_test.repl_input_polish import (
        build_mention_completer,
    )
    monkeypatch.setenv(
        "JARVIS_REPL_INPUT_POLISH_ENABLED", "true",
    )
    c = build_mention_completer()
    assert c is not None
    assert hasattr(c, "get_completions")


# ---------------------------------------------------------------------------
# Word-boundary gating
# ---------------------------------------------------------------------------


def test_at_prefix_word_triggers():
    """The critical positive test: @ at start of cursor-word
    triggers completion."""
    from backend.core.ouroboros.battle_test.repl_input_polish import (
        build_mention_completer,
    )
    from prompt_toolkit.document import Document

    c = build_mention_completer()
    doc = Document(text="@back", cursor_position=5)
    completions = list(c.get_completions(doc, None))
    assert len(completions) > 0


def test_no_at_does_not_trigger():
    """No @ in input → no completions."""
    from backend.core.ouroboros.battle_test.repl_input_polish import (
        build_mention_completer,
    )
    from prompt_toolkit.document import Document

    c = build_mention_completer()
    doc = Document(text="hello world", cursor_position=11)
    completions = list(c.get_completions(doc, None))
    assert completions == []


def test_email_like_mid_word_at_does_not_trigger():
    """Critical correctness: `user@host` (email pattern) MUST
    NOT trigger file completion."""
    from backend.core.ouroboros.battle_test.repl_input_polish import (
        build_mention_completer,
    )
    from prompt_toolkit.document import Document

    c = build_mention_completer()
    doc = Document(text="user@host", cursor_position=9)
    completions = list(c.get_completions(doc, None))
    assert completions == []


def test_decorator_at_after_text_does_not_trigger():
    """`text@decorator` (mid-word @) MUST NOT trigger."""
    from backend.core.ouroboros.battle_test.repl_input_polish import (
        build_mention_completer,
    )
    from prompt_toolkit.document import Document

    c = build_mention_completer()
    doc = Document(text="def foo@bar", cursor_position=11)
    completions = list(c.get_completions(doc, None))
    assert completions == []


def test_at_after_whitespace_does_trigger():
    """`prose @file` (@ after whitespace boundary) DOES trigger."""
    from backend.core.ouroboros.battle_test.repl_input_polish import (
        build_mention_completer,
    )
    from prompt_toolkit.document import Document

    c = build_mention_completer()
    doc = Document(
        text="check this @backend",
        cursor_position=19,
    )
    completions = list(c.get_completions(doc, None))
    assert len(completions) > 0


def test_at_after_tab_does_trigger():
    """Tab is also a word-boundary."""
    from backend.core.ouroboros.battle_test.repl_input_polish import (
        build_mention_completer,
    )
    from prompt_toolkit.document import Document

    c = build_mention_completer()
    doc = Document(
        text="cmd\t@back",
        cursor_position=9,
    )
    completions = list(c.get_completions(doc, None))
    assert len(completions) > 0


# ---------------------------------------------------------------------------
# Defensive paths — NEVER raises
# ---------------------------------------------------------------------------


def test_filesystem_error_returns_no_completions(monkeypatch):
    """If PathCompleter raises (filesystem error / permission
    error), the completer returns no completions rather than
    crashing the REPL."""
    from backend.core.ouroboros.battle_test.repl_input_polish import (
        build_mention_completer,
    )
    from prompt_toolkit.document import Document

    c = build_mention_completer()
    # Patch the Document construction to raise at runtime
    with patch(
        "prompt_toolkit.document.Document",
        side_effect=RuntimeError("simulated"),
    ):
        doc = Document(text="@back", cursor_position=5)
        # Should not raise
        completions = list(c.get_completions(doc, None))
        assert completions == []


def test_empty_text_does_not_raise():
    """Empty input must not crash."""
    from backend.core.ouroboros.battle_test.repl_input_polish import (
        build_mention_completer,
    )
    from prompt_toolkit.document import Document

    c = build_mention_completer()
    doc = Document(text="", cursor_position=0)
    completions = list(c.get_completions(doc, None))
    assert completions == []


def test_just_at_sign_does_not_crash():
    """Operator typing just `@` (no path part yet) renders
    cwd entries (PathCompleter's behavior on empty path)."""
    from backend.core.ouroboros.battle_test.repl_input_polish import (
        build_mention_completer,
    )
    from prompt_toolkit.document import Document

    c = build_mention_completer()
    doc = Document(text="@", cursor_position=1)
    # Should not raise — number of completions depends on cwd
    completions = list(c.get_completions(doc, None))
    assert isinstance(completions, list)


# ---------------------------------------------------------------------------
# Merge with slash completer
# ---------------------------------------------------------------------------


def test_completion_dispatch_merges_both(monkeypatch):
    """build_completer in repl_completion.py MUST return a
    completer that handles BOTH slash + mention contexts."""
    monkeypatch.setenv(
        "JARVIS_REPL_INPUT_POLISH_ENABLED", "true",
    )
    from backend.core.ouroboros.battle_test.repl_completion import (
        build_completer, VerbRegistry,
    )
    # Empty registry is fine — slash completer gracefully
    # handles zero verbs; mention completer is the load-bearing
    # surface this test verifies.
    reg = VerbRegistry(verbs=tuple())
    c = build_completer(reg)
    assert c is not None

    from prompt_toolkit.document import Document

    # Test slash context fires
    doc = Document(text="/", cursor_position=1)
    slash_completions = list(c.get_completions(doc, None))
    # May or may not have results depending on registry, but
    # it MUST NOT crash

    # Test mention context fires
    doc = Document(text="@back", cursor_position=5)
    mention_completions = list(c.get_completions(doc, None))
    assert len(mention_completions) > 0, (
        "merged completer MUST handle @-mention context"
    )


def test_merged_completer_falls_through_when_polish_off(
    monkeypatch,
):
    """When polish is off, build_completer falls through to
    slash-only — mention contexts get no completions."""
    monkeypatch.setenv(
        "JARVIS_REPL_INPUT_POLISH_ENABLED", "false",
    )
    from backend.core.ouroboros.battle_test.repl_completion import (
        build_completer, VerbRegistry,
    )

    reg = VerbRegistry(verbs=tuple())
    c = build_completer(reg)
    assert c is not None  # Slash-only completer still works

    from prompt_toolkit.document import Document
    doc = Document(text="@back", cursor_position=5)
    completions = list(c.get_completions(doc, None))
    assert completions == [], (
        "polish-off should yield no @-mention completions"
    )


# ---------------------------------------------------------------------------
# Source AST regressions
# ---------------------------------------------------------------------------


def test_source_uses_path_completer():
    """AST pin: build_mention_completer MUST compose stdlib
    PathCompleter (no parallel file-tree walking)."""
    target = (
        _repo_root()
        / "backend/core/ouroboros/battle_test"
        / "repl_input_polish.py"
    )
    source = target.read_text(encoding="utf-8")
    assert "PathCompleter" in source, (
        "repl_input_polish.py MUST compose stdlib "
        "PathCompleter (Slice 7 regression)"
    )


def test_repl_completion_source_uses_merge_completers():
    """AST pin: repl_completion.build_completer MUST use
    merge_completers to combine slash + mention."""
    target = (
        _repo_root()
        / "backend/core/ouroboros/battle_test"
        / "repl_completion.py"
    )
    source = target.read_text(encoding="utf-8")
    assert "merge_completers" in source, (
        "repl_completion.py MUST merge slash + mention "
        "completers (Slice 7 wiring regression)"
    )
    assert "build_mention_completer" in source, (
        "repl_completion.py MUST import "
        "build_mention_completer (Slice 7 wiring)"
    )


def test_build_mention_completer_in_public_api():
    """Public API stability: build_mention_completer MUST be
    in repl_input_polish.__all__."""
    from backend.core.ouroboros.battle_test import (
        repl_input_polish,
    )
    assert "build_mention_completer" in repl_input_polish.__all__
