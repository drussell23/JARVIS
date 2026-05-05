"""Tests for repl_input_polish (Gap #7 Slice 4)."""
from __future__ import annotations

import ast
import sys
from pathlib import Path
from unittest import mock

import pytest

from backend.core.ouroboros.battle_test.repl_input_polish import (
    AttachmentExtraction,
    MASTER_FLAG_ENV_VAR,
    REPL_INPUT_POLISH_SCHEMA_VERSION,
    TITLE_ENABLED_ENV_VAR,
    clear_terminal_title,
    extract_attachments,
    format_title,
    is_attachment_mention,
    is_polish_enabled,
    is_terminal_title_enabled,
    make_esc_cancel_binding,
    set_terminal_title,
)


_REPO = Path("/Users/djrussell23/Documents/repos/JARVIS-AI-Agent")


@pytest.fixture(autouse=True)
def clean(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(MASTER_FLAG_ENV_VAR, raising=False)
    monkeypatch.delenv(TITLE_ENABLED_ENV_VAR, raising=False)
    yield


# ===========================================================================
# Schema + master flags
# ===========================================================================


def test_schema_version_pinned():
    assert REPL_INPUT_POLISH_SCHEMA_VERSION == "repl_input_polish.v1"


def test_polish_master_flag_default_on_post_graduation():
    """Slice 5 flipped this default-true (2026-05-04)."""
    assert is_polish_enabled() is True


def test_polish_master_flag_explicit_off(monkeypatch):
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "false")
    assert is_polish_enabled() is False


def test_terminal_title_inherits_polish_when_unset(monkeypatch):
    """When TITLE_ENABLED is unset, follows the polish master flag."""
    # Polish default-on → title default-on (inherited)
    assert is_terminal_title_enabled() is True
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "false")
    # Polish off → title off (inherited)
    assert is_terminal_title_enabled() is False


def test_terminal_title_explicit_off_overrides_polish(monkeypatch):
    monkeypatch.setenv(TITLE_ENABLED_ENV_VAR, "false")
    assert is_terminal_title_enabled() is False


def test_terminal_title_explicit_on_when_polish_off(monkeypatch):
    """Operator can opt INTO title-only without enabling full polish."""
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "false")
    monkeypatch.setenv(TITLE_ENABLED_ENV_VAR, "true")
    assert is_terminal_title_enabled() is True


# ===========================================================================
# is_attachment_mention — heuristic
# ===========================================================================


@pytest.mark.parametrize("token,expected", [
    # Path-shaped (slash present)
    ("backend/auth.py", True),
    ("./foo", True),
    ("../bar", True),
    ("a/b/c", True),
    # File-shaped (extension)
    ("foo.py", True),
    ("README.md", True),
    ("script.sh", True),
    ("file.tar", True),
    # Neither slash nor extension
    ("here", False),
    ("foo", False),
    ("", False),
    ("plain_word", False),
])
def test_is_attachment_mention(token, expected):
    assert is_attachment_mention(token) is expected


def test_is_attachment_mention_handles_non_string():
    assert is_attachment_mention(None) is False  # type: ignore[arg-type]
    assert is_attachment_mention(42) is False  # type: ignore[arg-type]


# ===========================================================================
# extract_attachments — input pre-processing
# ===========================================================================


def test_extract_no_mentions():
    out = extract_attachments("just plain prose")
    assert out.cleaned_line == "just plain prose"
    assert out.paths == ()


def test_extract_single_mention():
    out = extract_attachments("@backend/auth.py")
    assert out.paths == ("backend/auth.py",)
    assert out.cleaned_line == ""


def test_extract_mention_in_prose():
    out = extract_attachments("review @backend/auth.py for issues")
    assert out.paths == ("backend/auth.py",)
    assert out.cleaned_line == "review for issues"


def test_extract_multiple_mentions():
    out = extract_attachments(
        "compare @foo.py and @bar.py with @baz/qux.py please"
    )
    assert out.paths == ("foo.py", "bar.py", "baz/qux.py")
    assert "@foo.py" not in out.cleaned_line
    assert "@bar.py" not in out.cleaned_line
    assert "@baz/qux.py" not in out.cleaned_line
    assert "compare" in out.cleaned_line
    assert "please" in out.cleaned_line


def test_extract_dedupes_repeated_mentions():
    out = extract_attachments("@a.py and again @a.py twice")
    assert out.paths == ("a.py",)


def test_extract_preserves_email_addresses():
    """Email addresses should NOT be picked up — `user@host` lacks
    whitespace boundary before the @."""
    out = extract_attachments("contact dev@example.com")
    assert out.paths == ()
    assert "dev@example.com" in out.cleaned_line


def test_extract_preserves_decorator_in_pasted_code():
    """Python decorators (@decorator) without slash/extension should
    NOT be picked up via the heuristic."""
    out = extract_attachments("apply @lru_cache here")
    # @lru_cache lacks both / and extension → false-positive guard
    assert out.paths == ()
    # The token stays in the cleaned line (treated as prose)
    assert "@lru_cache" in out.cleaned_line


def test_extract_handles_mention_at_start():
    out = extract_attachments("@foo.py do work")
    assert out.paths == ("foo.py",)


def test_extract_handles_mention_at_end():
    out = extract_attachments("do work @foo.py")
    assert out.paths == ("foo.py",)


def test_extract_collapses_whitespace():
    out = extract_attachments("hello  @foo.py    world")
    assert out.cleaned_line == "hello world"


def test_extract_handles_non_string():
    out = extract_attachments(None)  # type: ignore[arg-type]
    assert out.cleaned_line == ""
    assert out.paths == ()


def test_extract_returns_frozen():
    out = extract_attachments("@foo.py")
    with pytest.raises(Exception):
        out.cleaned_line = "tampered"  # type: ignore[misc]


# ===========================================================================
# Terminal title — set / clear / format
# ===========================================================================


def test_set_terminal_title_when_disabled_returns_false(monkeypatch):
    """Explicit master-flag-off → no-op, regardless of TTY state."""
    monkeypatch.setenv(TITLE_ENABLED_ENV_VAR, "false")
    assert set_terminal_title("test") is False


def test_set_terminal_title_when_enabled_writes_osc(monkeypatch, capsys):
    """OSC 0 sequence emitted to stderr."""
    monkeypatch.setenv(TITLE_ENABLED_ENV_VAR, "true")
    monkeypatch.setenv("TERM", "xterm-256color")
    fake_real = mock.Mock()
    fake_real.isatty.return_value = True
    monkeypatch.setattr(sys, "__stdout__", fake_real)

    assert set_terminal_title("My Title") is True
    captured = capsys.readouterr()
    # OSC 0 sequence: ESC ] 0 ; <text> BEL
    assert "\x1b]0;My Title\x07" in captured.err


def test_set_terminal_title_strips_embedded_escape_chars(monkeypatch, capsys):
    """Pathological input must not break the OSC sequence."""
    monkeypatch.setenv(TITLE_ENABLED_ENV_VAR, "true")
    monkeypatch.setenv("TERM", "xterm-256color")
    fake_real = mock.Mock()
    fake_real.isatty.return_value = True
    monkeypatch.setattr(sys, "__stdout__", fake_real)

    set_terminal_title("evil\x07injection\x1b]")
    captured = capsys.readouterr()
    # Sanitized — no embedded BEL or ESC inside the payload (the
    # trailing ``]`` survives but is harmless; only \x07 and \x1b
    # can prematurely terminate the OSC sequence).
    payload_start = captured.err.index("\x1b]0;") + 4
    payload_end = captured.err.index("\x07", payload_start)
    payload = captured.err[payload_start:payload_end]
    assert "\x07" not in payload
    assert "\x1b" not in payload
    assert payload.startswith("evil")
    assert "injection" in payload


def test_set_terminal_title_truncates_long_input(monkeypatch, capsys):
    monkeypatch.setenv(TITLE_ENABLED_ENV_VAR, "true")
    monkeypatch.setenv("TERM", "xterm-256color")
    fake_real = mock.Mock()
    fake_real.isatty.return_value = True
    monkeypatch.setattr(sys, "__stdout__", fake_real)

    set_terminal_title("x" * 500)
    captured = capsys.readouterr()
    # Bounded; ellipsis appended
    assert "\x1b]0;" in captured.err
    payload_start = captured.err.index("\x1b]0;") + 4
    payload_end = captured.err.index("\x07", payload_start)
    payload = captured.err[payload_start:payload_end]
    assert len(payload) <= 200
    assert payload.endswith("…")


def test_set_terminal_title_skipped_for_dumb_terminal(monkeypatch):
    """TERM=dumb / TERM=linux → no OSC emission."""
    monkeypatch.setenv(TITLE_ENABLED_ENV_VAR, "true")
    monkeypatch.setenv("TERM", "linux")
    fake_real = mock.Mock()
    fake_real.isatty.return_value = True
    monkeypatch.setattr(sys, "__stdout__", fake_real)

    assert set_terminal_title("any") is False


def test_set_terminal_title_skipped_when_not_tty(monkeypatch):
    monkeypatch.setenv(TITLE_ENABLED_ENV_VAR, "true")
    monkeypatch.setenv("TERM", "xterm-256color")
    fake_real = mock.Mock()
    fake_real.isatty.return_value = False
    monkeypatch.setattr(sys, "__stdout__", fake_real)
    fake_proxy = mock.Mock()
    fake_proxy.isatty.return_value = False
    monkeypatch.setattr(sys, "stdout", fake_proxy)

    assert set_terminal_title("any") is False


def test_clear_terminal_title_emits_empty(monkeypatch, capsys):
    monkeypatch.setenv(TITLE_ENABLED_ENV_VAR, "true")
    monkeypatch.setenv("TERM", "xterm-256color")
    fake_real = mock.Mock()
    fake_real.isatty.return_value = True
    monkeypatch.setattr(sys, "__stdout__", fake_real)

    clear_terminal_title()
    captured = capsys.readouterr()
    assert "\x1b]0;\x07" in captured.err


# ===========================================================================
# format_title — title-string composition
# ===========================================================================


def test_format_title_idle():
    assert format_title() == "O+V"


def test_format_title_with_phase_op():
    out = format_title(op_id="op-019d83-foo", phase="GENERATE")
    assert "GENERATE" in out
    assert "op-foo" in out


def test_format_title_with_cost():
    out = format_title(cost_used=0.04, cost_budget=0.50)
    assert "$0.04/$0.50" in out


def test_format_title_idle_phase_omitted():
    """phase=IDLE shouldn't show — that's the default state."""
    out = format_title(phase="IDLE")
    assert out == "O+V"


def test_format_title_handles_none_inputs():
    out = format_title(op_id=None, phase=None, cost_used=0.0)
    assert out == "O+V"


def test_format_title_uses_dot_separator():
    """The middot · is the consistent separator."""
    out = format_title(op_id="op-x", phase="GENERATE", cost_used=0.04, cost_budget=0.5)
    assert " · " in out


# ===========================================================================
# Esc-to-cancel binding factory
# ===========================================================================


class _FakeFlow:
    def __init__(self):
        self._active_ops = set()
        self._swarm_snapshots = {}


class _FakeRepl:
    def __init__(self, flow):
        self._flow = flow

    async def _handle_cancel(self, op_id, immediate=False):
        self.cancelled = (op_id, immediate)


def test_make_esc_binding_returns_none_when_disabled(monkeypatch):
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "false")
    repl = _FakeRepl(_FakeFlow())
    binding = make_esc_cancel_binding(repl)
    assert binding is None


def test_make_esc_binding_returns_keybindings_when_enabled():
    """Post-graduation default-on; binding is constructed."""
    repl = _FakeRepl(_FakeFlow())
    binding = make_esc_cancel_binding(repl)
    # Returns a prompt_toolkit KeyBindings instance
    assert binding is not None
    # KeyBindings has a `bindings` attribute / `_bindings` list
    assert hasattr(binding, "add") or hasattr(binding, "bindings")


def test_pick_active_op_id_empty_returns_none(monkeypatch):
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    from backend.core.ouroboros.battle_test.repl_input_polish import (
        _pick_active_op_id,
    )
    assert _pick_active_op_id(None) is None
    flow = _FakeFlow()
    assert _pick_active_op_id(flow) is None


def test_pick_active_op_id_picks_most_recent(monkeypatch):
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    from backend.core.ouroboros.battle_test.repl_input_polish import (
        _pick_active_op_id,
    )
    flow = _FakeFlow()
    flow._active_ops = {"op-a", "op-b", "op-c"}

    snap_a = mock.Mock(spec=["started_monotonic"])
    snap_a.started_monotonic = 100.0
    snap_b = mock.Mock(spec=["started_monotonic"])
    snap_b.started_monotonic = 200.0  # most recent
    snap_c = mock.Mock(spec=["started_monotonic"])
    snap_c.started_monotonic = 50.0

    flow._swarm_snapshots = {"op-a": snap_a, "op-b": snap_b, "op-c": snap_c}
    assert _pick_active_op_id(flow) == "op-b"


# ===========================================================================
# Source-level regression — wiring into serpent_flow
# ===========================================================================


_SERPENT_FLOW = _REPO / "backend/core/ouroboros/battle_test/serpent_flow.py"


def test_loop_wires_attachment_extraction():
    src = _SERPENT_FLOW.read_text()
    assert "extract_attachments" in src
    assert "_extraction.paths" in src


def test_loop_wires_esc_binding():
    src = _SERPENT_FLOW.read_text()
    assert "make_esc_cancel_binding" in src
    assert "add_bindings" in src


def test_op_started_sets_terminal_title():
    src = _SERPENT_FLOW.read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "op_started":
                body = ast.unparse(node)
                assert "_maybe_set_terminal_title" in body
                return
    pytest.fail("op_started method not found")


def test_op_completed_clears_or_refreshes_title():
    src = _SERPENT_FLOW.read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "op_completed":
                body = ast.unparse(node)
                assert "_maybe_set_terminal_title" in body
                return
    pytest.fail("op_completed method not found")


def test_op_failed_clears_or_refreshes_title():
    src = _SERPENT_FLOW.read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "op_failed":
                body = ast.unparse(node)
                assert "_maybe_set_terminal_title" in body
                return
    pytest.fail("op_failed method not found")


def test_extraction_attachment_record_frozen():
    extraction = AttachmentExtraction(cleaned_line="x", paths=("a",))
    with pytest.raises(Exception):
        extraction.cleaned_line = "y"  # type: ignore[misc]
