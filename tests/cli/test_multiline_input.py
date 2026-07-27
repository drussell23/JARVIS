"""Enter submits, unless the text is visibly unfinished.

A goal worth giving an autonomous organism is often longer than one line — a
paragraph of context, a pasted traceback, a list of constraints. The prompt
accepted exactly one.

And it was not only a typing limit: with `multiline=False` a PASTED block
loses its newlines, so an operator pasting a stack trace got it silently
collapsed into one line. That is data loss, not inconvenience.

The obvious fix breaks submission — turn multiline on and Enter starts
inserting newlines with no key left meaning "go". So the buffer is
CONDITIONALLY multiline, using signals every shell and REPL already taught
operators.

Every ambiguous case resolves to SUBMIT. A prompt that refuses to submit has
no escape hatch; one that submits early can be retyped.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from backend.core.ouroboros.battle_test.input_continuation import (
    multiline_enabled,
    strip_continuations,
    wants_continuation,
)

_REPO = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------
# 1. the common case still submits
# --------------------------------------------------------------------------

def test_a_one_line_goal_submits() -> None:
    """Making every short goal cost two keystrokes to save the rare long one
    is the wrong trade."""
    assert wants_continuation("fix the flaky test") is False


def test_an_empty_buffer_submits() -> None:
    """Rather than silently growing blank lines the operator cannot see."""
    assert wants_continuation("") is False
    assert wants_continuation("   \n  ") is False


# --------------------------------------------------------------------------
# 2. the three "unfinished" signals
# --------------------------------------------------------------------------

def test_a_trailing_backslash_continues() -> None:
    """The shell's own continuation mark — the one signal operators reach for
    without being told."""
    assert wants_continuation("fix these things: \\") is True


def test_trailing_whitespace_does_not_defeat_the_backslash() -> None:
    assert wants_continuation("continue \\   ") is True


def test_an_open_code_fence_continues() -> None:
    """Pasting half a ``` block and having it submit is the most annoying
    possible outcome."""
    assert wants_continuation("here:\n```python\nx = 1") is True


def test_a_closed_fence_submits() -> None:
    assert wants_continuation("here:\n```python\nx = 1\n```") is False


def test_an_unbalanced_bracket_continues_ONCE_MULTILINE() -> None:
    assert wants_continuation("def f(\n    x=1") is True


def test_a_balanced_multiline_block_submits() -> None:
    assert wants_continuation("def f(\n    x=1\n)") is False


# --------------------------------------------------------------------------
# 3. it never holds the operator hostage
# --------------------------------------------------------------------------

def test_prose_with_a_bracket_still_submits() -> None:
    """THE trap this rule was written to avoid: "the smiley is (: nice" must
    not trap someone in a prompt that will not submit. On a first line a
    bracket is prose far more often than code."""
    assert wants_continuation("the smiley is (: nice") is False
    assert wants_continuation("call foo(bar") is False


def test_a_bracket_inside_quotes_is_not_structure() -> None:
    assert wants_continuation('say "a (thing" now\nand more') is False


def test_a_stray_closer_is_not_a_reason_to_demand_input() -> None:
    assert wants_continuation("stray ) closer\nsecond line") is False


@pytest.mark.parametrize("junk", [None, 42, object(), b"bytes"])
def test_it_never_raises(junk: Any) -> None:
    assert isinstance(wants_continuation(junk), bool)


def test_the_switch_returns_single_line_behaviour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_INPUT_MULTILINE_ENABLED", "0")
    assert multiline_enabled() is False
    assert wants_continuation("unfinished \\") is False


# --------------------------------------------------------------------------
# 4. the daemon receives the goal, not the mechanics
# --------------------------------------------------------------------------

def test_continuation_backslashes_are_stripped() -> None:
    """A trailing `\\` is punctuation for the prompt; it would reach the model
    as noise."""
    assert strip_continuations("line one \\\nline two") == "line one\nline two"


def test_content_backslashes_survive() -> None:
    """Only a LINE-ENDING backslash is mechanics. One mid-line is content."""
    assert "\\d" in strip_continuations("match \\d+ digits")


def test_fences_and_brackets_are_content_and_survive() -> None:
    text = "here:\n```python\nx = f(1)\n```"
    assert strip_continuations(text) == text


def test_the_relay_strips_before_sending() -> None:
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        from backend.core.ouroboros.cli.ov import (
            AttachUI, _route_operator_line,
        )

    class _Client:
        def __init__(self) -> None:
            self.sent: list = []

        def send_input(self, text: str) -> bool:
            self.sent.append(text)
            return True

        def send_audio(self, _c: str) -> bool:
            return True

    client = _Client()
    _route_operator_line(client, AttachUI(), "line one \\\nline two")
    assert client.sent == ["line one\nline two"]


# --------------------------------------------------------------------------
# 5. both surfaces, one rule — applied through the library's own seam
# --------------------------------------------------------------------------

def _multiline_kwarg(path: str) -> list:
    src = (_REPO / path).read_text()
    return [
        kw.value
        for node in ast.walk(ast.parse(src))
        if isinstance(node, ast.Call)
        for kw in node.keywords
        if kw.arg == "multiline"
    ]


@pytest.mark.parametrize("path", [
    "backend/core/ouroboros/cli/ov.py",
    "backend/core/ouroboros/battle_test/bipartite_layout.py",
])
def test_no_prompt_is_hard_wired_single_line(path: str) -> None:
    """AST, not a substring: both files' comments EXPLAIN the old
    `multiline=False`, and a text search cannot tell an explanation from a
    setting. Grepping source for a symbol that also appears in its own prose
    has produced a false pass five times in this codebase."""
    values = _multiline_kwarg(path)
    assert values, f"{path} sets no multiline at all"
    assert not [
        v for v in values
        if isinstance(v, ast.Constant) and v.value is False
    ], "a prompt is still hard-wired single-line"


@pytest.mark.parametrize("path", [
    "backend/core/ouroboros/cli/ov.py",
    "backend/core/ouroboros/battle_test/bipartite_layout.py",
])
def test_both_surfaces_apply_the_shared_rule(path: str) -> None:
    """Two surfaces with two rules is how the palette ended up rendering
    differently on each. One definition, or they drift."""
    src = (_REPO / path).read_text()
    imported = {
        alias.name
        for node in ast.walk(ast.parse(src))
        if isinstance(node, ast.ImportFrom) for alias in node.names
    }
    assert "continuation_filter" in imported, f"{path} has its own rule"
    assert "buffer.multiline = continuation_filter" in src.replace(
        "_buf.multiline", "buffer.multiline",
    ), f"{path} builds the filter but never applies it"


def test_the_condition_is_passed_to_the_BUFFER_not_the_widget() -> None:
    """`TextArea(multiline=<Condition>)` LOOKS right — the parameter is
    annotated `FilterOrBool` — and raises ValueError at construction: the
    widget branches on raw truthiness before any `to_filter`, and a Filter
    refuses `__bool__`. The literal True selects the growable branch; the
    falsy one hard-clamps `height = D.exact(1)` and would put the caret below
    the fold on line two."""
    from prompt_toolkit.filters import Condition
    from prompt_toolkit.widgets import TextArea

    with pytest.raises(ValueError):
        TextArea(multiline=Condition(lambda: False))

    for path in ("backend/core/ouroboros/cli/ov.py",
                 "backend/core/ouroboros/battle_test/bipartite_layout.py"):
        assert all(
            isinstance(v, ast.Constant) and v.value is True
            for v in _multiline_kwarg(path)
        ), f"{path} passes a non-literal multiline to a widget"


def test_the_filter_reads_ITS_buffer_not_whatever_has_focus() -> None:
    """The first version read `get_app().current_buffer`. It returned False
    for every case under test — no running Application — and looked correct.
    Bound to one buffer, it is both right when focus moves and testable."""
    from prompt_toolkit.buffer import Buffer

    from backend.core.ouroboros.battle_test.input_continuation import (
        continuation_filter,
    )

    mine, other = Buffer(), Buffer()
    mine.multiline = continuation_filter(lambda: mine.text)
    other.text = "unfinished \\"
    mine.text = "fix the flaky test"
    assert mine.multiline() is False, "it consulted the wrong buffer"
    mine.text = "unfinished \\"
    assert mine.multiline() is True


def test_the_filter_is_callable_even_when_it_degrades() -> None:
    """`is_multiline` CALLS this value on every keystroke — a bare bool would
    raise on the next one."""
    from backend.core.ouroboros.battle_test.input_continuation import (
        continuation_filter,
    )

    def _explode() -> str:
        raise RuntimeError("buffer gone")

    filt = continuation_filter(_explode)
    assert callable(filt)
    assert filt() is False


def test_a_pasted_block_keeps_its_newlines() -> None:
    """The reason this is more than a UX limit: with a single-line buffer a
    pasted traceback was silently collapsed to one line."""
    from prompt_toolkit.buffer import Buffer

    from backend.core.ouroboros.battle_test.input_continuation import (
        continuation_filter,
    )

    buf = Buffer(multiline=True)
    buf.multiline = continuation_filter(lambda: buf.text)
    pasted = 'Traceback:\n  File "x.py", line 3\n    raise ValueError'
    buf.insert_text(pasted)
    assert buf.text.count("\n") == 2
    assert buf.text == pasted


# --------------------------------------------------------------------------
# 6. the escape hatch is real
# --------------------------------------------------------------------------
#
# Every ambiguous case above resolves to SUBMIT, and that is ONLY safe because
# a deliberate newline is always one keystroke away. prompt_toolkit ships no
# `escape enter` binding of its own — checked against the installed library —
# so these prove the promise the rules depend on.

def test_alt_enter_is_bound() -> None:
    from prompt_toolkit.key_binding import KeyBindings

    from backend.core.ouroboros.battle_test.input_continuation import (
        install_newline_binding,
    )

    kb = KeyBindings()
    assert install_newline_binding(kb) is True
    combos = [tuple(str(k) for k in b.keys) for b in kb.bindings]
    assert ("Keys.Escape", "Keys.ControlM") in combos


def test_alt_enter_actually_inserts() -> None:
    """A registered binding that does nothing is the wired-but-inert defect
    this codebase keeps producing — so invoke the handler, don't just count
    it."""
    from prompt_toolkit.buffer import Buffer
    from prompt_toolkit.key_binding import KeyBindings

    from backend.core.ouroboros.battle_test.input_continuation import (
        install_newline_binding,
    )

    kb = KeyBindings()
    install_newline_binding(kb)
    buf = Buffer()
    buf.text = "first"
    buf.cursor_position = len("first")

    class _Event:
        current_buffer = buf

    kb.bindings[0].handler(_Event())
    assert buf.text == "first\n"


def test_the_switch_removes_the_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from prompt_toolkit.key_binding import KeyBindings

    from backend.core.ouroboros.battle_test.input_continuation import (
        install_newline_binding,
    )

    monkeypatch.setenv("JARVIS_INPUT_MULTILINE_ENABLED", "0")
    kb = KeyBindings()
    assert install_newline_binding(kb) is False
    assert not kb.bindings


def test_it_never_raises_on_a_bad_registry() -> None:
    from backend.core.ouroboros.battle_test.input_continuation import (
        install_newline_binding,
    )

    assert install_newline_binding(None) is False
    assert install_newline_binding(object()) is False


def test_both_surfaces_install_it() -> None:
    for path in ("backend/core/ouroboros/cli/ov.py",
                 "backend/core/ouroboros/battle_test/bipartite_layout.py"):
        text = (_REPO / path).read_text()
        called = {
            node.func.id
            for node in ast.walk(ast.parse(text))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "install_newline_binding" in called, f"{path} has no escape hatch"


def test_esc_yields_the_sequence_while_composing() -> None:
    """`eager=True` means "fire now, don't wait for a longer sequence" — it
    would swallow the escape half of Alt+Enter. Esc stays instant on an EMPTY
    buffer and yields while the operator is typing."""
    src = (_REPO / "backend/core/ouroboros/cli/ov.py").read_text()
    unconditional = [
        node.lineno
        for node in ast.walk(ast.parse(src))
        if isinstance(node, ast.Call)
        for kw in node.keywords
        if kw.arg == "eager"
        and isinstance(kw.value, ast.Constant)
        and kw.value.value is True
    ]
    assert not unconditional, f"eager Esc swallows Alt+Enter at {unconditional}"
