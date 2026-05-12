"""Pytest assertions for the content sanitizer pipeline.

Test categories
---------------

A. Leaf-primitive happy paths — type-correct inputs work normally.
B. Leaf-primitive None-safety — None must not crash + returns "".
C. ``sanitize_comment`` composition — composes the two primitives.
D. ``sanitize_thread`` exclusion contract — None entries MUST be
   EXCLUDED from the output (not surfaced as empty strings).

Multi-site trap
---------------

A naive fix that only None-checks ``strip_html_tags`` STILL crashes
inside ``truncate(None)`` because ``len(None)`` raises ``TypeError``
— test ``test_truncate_handles_none_input`` catches this.

A naive fix that None-checks both leaf primitives but leaves
``sanitize_thread`` unchanged would PRODUCE ``""`` entries for None
comments, violating the documented exclusion contract — test
``test_sanitize_thread_excludes_none_entries`` catches this.

The correct coherent fix requires THREE coordinated changes
(strip_html_tags + truncate + sanitize_thread).
"""
from __future__ import annotations

from before import (
    sanitize_comment,
    sanitize_thread,
    strip_html_tags,
    truncate,
)


# ---- A. Leaf-primitive happy paths ----


def test_strip_html_tags_removes_simple_tag():
    assert strip_html_tags("<p>hello</p>") == "hello"


def test_strip_html_tags_removes_nested_tags():
    assert strip_html_tags("<div><b>hi</b> there</div>") == "hi there"


def test_strip_html_tags_no_tags_unchanged():
    assert strip_html_tags("plain text") == "plain text"


def test_truncate_short_text_unchanged():
    assert truncate("hi", 10) == "hi"


def test_truncate_exact_length_unchanged():
    assert truncate("hello", 5) == "hello"


def test_truncate_long_text_appends_ellipsis():
    assert truncate("hello world", 5) == "hello..."


# ---- B. Leaf-primitive None-safety ----


def test_strip_html_tags_handles_none_input():
    """Type-error canary: ``re.sub(None)`` raises TypeError under
    the buggy code.  A None-safe primitive returns ``""``."""
    assert strip_html_tags(None) == ""


def test_truncate_handles_none_input():
    """Type-error canary #2: ``len(None)`` raises TypeError.

    A naive fix that ONLY None-checks ``strip_html_tags`` still
    leaves ``truncate(None)`` crashing — this is the multi-site
    trap.  Coherent fix requires None-safety at BOTH leaf primitives.
    """
    assert truncate(None, 10) == ""


# ---- C. sanitize_comment composition ----


def test_sanitize_comment_strips_then_truncates():
    """Composition path: long-with-HTML input → stripped first, then
    truncated if needed."""
    result = sanitize_comment(
        "<p>" + "a" * 200 + "</p>", max_length=10,
    )
    assert result == "aaaaaaaaaa..."


def test_sanitize_comment_short_html_unchanged_length():
    assert sanitize_comment("<b>hi</b>") == "hi"


def test_sanitize_comment_handles_none():
    """Both leaf primitives must be None-safe for this to pass."""
    assert sanitize_comment(None) == ""


# ---- D. sanitize_thread exclusion contract ----


def test_sanitize_thread_normal_list():
    thread = [
        "<p>first</p>",
        "<b>second</b>",
        "<i>third</i>",
    ]
    assert sanitize_thread(thread) == ["first", "second", "third"]


def test_sanitize_thread_empty_list():
    assert sanitize_thread([]) == []


def test_sanitize_thread_excludes_none_entries():
    """SEMANTIC CONTRACT CANARY — load-bearing for this fixture.

    None entries represent deleted/missing comments and MUST BE
    EXCLUDED from the output (NOT surfaced as empty strings).  A
    naive fix that None-checks the leaf primitives but leaves
    ``sanitize_thread`` unchanged would emit ``["a", "", "b"]``
    here — failing this assertion.

    The correct coherent fix requires THREE coordinated changes:
      1. strip_html_tags None-safe
      2. truncate None-safe
      3. sanitize_thread filters None entries

    This test discriminates "type-error fix" from "semantic-contract
    fix" — the whole point of the multi-site trap."""
    thread = [
        "<p>first</p>",
        None,                     # deleted comment — exclude
        "<b>second</b>",
        None,                     # also deleted
        "<i>third</i>",
    ]
    result = sanitize_thread(thread)
    assert result == ["first", "second", "third"]
    # Defense in depth: explicitly assert NO empty string in output
    # (empty-string would mean "we sanitized a None to '' instead of
    # excluding it" — the exact failure mode of the type-error-only
    # fix)
    assert "" not in result


def test_sanitize_thread_preserves_order():
    """When None entries are interleaved with real comments, the
    surviving comments MUST maintain their original relative order."""
    thread = [
        None,
        "<p>charlie</p>",
        None,
        "<p>alpha</p>",
        "<p>bravo</p>",
        None,
    ]
    assert sanitize_thread(thread) == ["charlie", "alpha", "bravo"]


def test_sanitize_thread_all_none_returns_empty():
    """Edge case: a thread of all-deleted comments returns ``[]``
    (no empty-string filler)."""
    assert sanitize_thread([None, None, None]) == []
