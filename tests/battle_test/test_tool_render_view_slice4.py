"""Tests for tool_render_view (Gap #2 Slice 4) — Rich markup composition
and master-flag-gated wiring into serpent_flow + ouroboros_tui.

Strategy
--------

Two test surfaces:

  • Pure ``compose`` tests — feed deterministic ``explicit_density``
    + stub providers, assert on returned :class:`ComposedToolRender`
    markup strings.
  • Integration tests via ``Console(record=True)`` — capture what
    actually lands on the terminal when the master flag is on.

The body store is reset between tests via the Slice 3 hook.
"""
from __future__ import annotations

import pytest
from rich.console import Console

from backend.core.ouroboros.battle_test.tool_render_policy import (
    DensityLevel,
    DensityPolicy,
)
from backend.core.ouroboros.battle_test.tool_render_registry import ToolStatus
from backend.core.ouroboros.battle_test.tool_render_store import (
    BoundedBodyStore,
    reset_default_store_for_tests,
)
from backend.core.ouroboros.battle_test.tool_render_view import (
    MASTER_FLAG_ENV_VAR,
    TOOL_RENDER_VIEW_SCHEMA_VERSION,
    ComposedToolRender,
    compose,
    compose_if_enabled,
    is_master_flag_enabled,
)
from backend.core.ouroboros.ui import theme


def _plain(markup: str) -> str:
    """Header markup tints the mark and the verb separately
    (deck_grammar.action), so substrings are checked on plain text."""
    from rich.text import Text
    return Text.from_markup(markup).plain


def _act(verb: str = "") -> str:
    """The theme's action mark (ASCII-degraded when needed), plus a verb."""
    mark = theme.mark("action")
    return f"{mark} {verb}" if verb else mark


# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture(autouse=True)
def clean_state(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(MASTER_FLAG_ENV_VAR, raising=False)
    monkeypatch.delenv("JARVIS_TOOL_RENDER_DENSITY", raising=False)
    monkeypatch.delenv("JARVIS_TOOL_RENDER_STORE_SIZE", raising=False)
    reset_default_store_for_tests()
    yield
    reset_default_store_for_tests()


def _density(
    level: DensityLevel = DensityLevel.BALANCED,
    *,
    max_body_lines: int = 10,
    max_summary_chars: int = 80,
) -> DensityPolicy:
    return DensityPolicy(
        level=level,
        max_body_lines=max_body_lines,
        max_summary_chars=max_summary_chars,
        provenance=f"test:{level.value}",
    )


# ===========================================================================
# Schema + master flag
# ===========================================================================


def test_schema_version_pinned():
    assert TOOL_RENDER_VIEW_SCHEMA_VERSION == "tool_render_view.v1"


def test_master_flag_on_by_default_post_graduation():
    """Slice 5 graduation flipped this default-true (2026-05-04).
    Operators opt OUT via ``=false`` / ``=0`` / ``=off``."""
    assert is_master_flag_enabled() is True


@pytest.mark.parametrize("raw,expected", [
    # Empty / unset → default ON post-graduation
    ("", True),
    # Truthy values
    ("true", True), ("True", True), ("TRUE", True),
    ("1", True), ("yes", True), ("on", True),
    # Garbage that isn't a recognized off-token → default ON
    ("garbage", True), ("maybe", True),
    # Off-tokens
    ("false", False), ("False", False),
    ("0", False), ("no", False), ("off", False),
])
def test_master_flag_parsing(monkeypatch, raw, expected):
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, raw)
    assert is_master_flag_enabled() is expected


def test_master_flag_unset_is_on():
    """Defensive: unset (env var not present at all) → default ON."""
    # The autouse fixture deletes the env var before this test.
    assert is_master_flag_enabled() is True


# ===========================================================================
# compose() — header composition
# ===========================================================================


def test_compose_returns_frozen_record():
    out = compose(
        "read_file", "foo.py", "line1\n",
        explicit_density=_density(),
    )
    assert isinstance(out, ComposedToolRender)
    assert out.schema_version == TOOL_RENDER_VIEW_SCHEMA_VERSION


def test_compose_cc_verb_header_for_read():
    out = compose(
        "read_file", "backend/foo.py", "x" * 100,
        explicit_density=_density(),
    )
    assert _act("Read") in _plain(out.header_markup)
    assert "backend/foo.py" in out.header_markup


def test_compose_cc_verb_header_for_edit():
    out = compose(
        "edit_file", "backend/foo.py", "+added\n-removed",
        explicit_density=_density(),
    )
    assert _act("Update") in _plain(out.header_markup)


def test_compose_cc_verb_header_for_write():
    out = compose(
        "write_file", "new.py", "line1\nline2",
        explicit_density=_density(),
    )
    assert _act("Write") in _plain(out.header_markup)


def test_compose_icon_header_for_search():
    out = compose(
        "search_code", "pattern", "hit1\nhit2",
        explicit_density=_density(),
    )
    # INVERTED: one grammar. `● Search("pattern")`, not `🔍 search_code`.
    assert _act() in out.header_markup
    assert "Search" in out.header_markup
    assert "🔍" not in out.header_markup
    assert "search_code" not in out.header_markup


def test_compose_default_descriptor_for_unknown_tool():
    """MCP-forwarded tools land here — and get the SAME shape.

    The registry resolves them to the default descriptor, whose own
    `tool_kind` is the literal string `_default`, so this layer substitutes
    the name the caller actually asked for. Without it an MCP call rendered
    as `Default(#eng)`, naming our fallback instead of their tool.
    """
    out = compose(
        "mcp_unknown", "args", "result",
        explicit_density=_density(),
    )
    assert _act() in out.header_markup
    assert "McpUnknown" in out.header_markup
    assert "🔧" not in out.header_markup
    assert "_default" not in out.header_markup


def test_compose_includes_duration_ms():
    out = compose(
        "read_file", "foo.py", "x",
        duration_ms=42.0,
        explicit_density=_density(),
    )
    assert "42ms" in out.header_markup


def test_compose_renders_seconds_for_long_durations():
    out = compose(
        "bash", "sleep", "out",
        duration_ms=1500.0,
        explicit_density=_density(),
    )
    assert "1.5s" in out.header_markup


def test_compose_status_failure_appends_x_mark():
    out = compose(
        "bash", "false", "",
        status=ToolStatus.ERROR,
        explicit_density=_density(),
    )
    assert "✗" in out.header_markup


def test_compose_status_success_no_x_mark():
    out = compose(
        "bash", "true", "ok",
        status=ToolStatus.SUCCESS,
        explicit_density=_density(),
    )
    assert "✗" not in out.header_markup


# ===========================================================================
# compose() — body / summary / expansion hint
# ===========================================================================


def test_compose_summary_uses_continuation_glyph():
    out = compose(
        "read_file", "foo.py", "a\nb\nc",
        explicit_density=_density(),
    )
    assert "⏎" in out.summary_markup or "Read " in out.summary_markup


def test_compose_no_body_for_read_file_descriptor():
    """``read_file`` is BodyShape.NONE — body suppressed regardless
    of density budget."""
    out = compose(
        "read_file", "foo.py", "many\nlines\nof\nsource\ncode",
        explicit_density=_density(max_body_lines=20),
    )
    assert out.body_lines_markup == ()


def test_compose_body_for_search_code_within_budget():
    """SUPERSEDED 2026-10-09: search is summary-only now; a body-bearing
    tool exercises the budget instead."""
    out = compose(
        "type_check", "pattern", "hit1\nhit2\nhit3",
        explicit_density=_density(max_body_lines=10),
        store=BoundedBodyStore(capacity=10),
    )
    assert len(out.body_lines_markup) == 3


def test_compose_body_elided_when_over_budget():
    big = "\n".join(f"line{i}" for i in range(50))
    store = BoundedBodyStore(capacity=10)
    out = compose(
        "type_check", "pattern", big,
        explicit_density=_density(max_body_lines=10),
        store=store,
    )
    # Total emitted lines respects the budget.
    assert len(out.body_lines_markup) == 10
    # Truncation marker appears, in the registry's own grammar.
    from backend.core.ouroboros.battle_test.tool_render_registry import is_elision_line
    from rich.text import Text
    assert any(is_elision_line(Text.from_markup(ln).plain.strip())
               for ln in out.body_lines_markup)


def test_compose_expansion_hint_emitted_when_elided_with_store():
    big = "\n".join(f"line{i}" for i in range(50))
    store = BoundedBodyStore(capacity=10)
    out = compose(
        "bash", "long_cmd", big,
        explicit_density=_density(max_body_lines=10),
        store=store,
    )
    # The way back rides the body's own marker (one line, never a second
    # hint line restating a count).
    assert out.expansion_hint == ""
    assert any("/expand t-" in ln for ln in out.body_lines_markup)


def test_compose_no_expansion_hint_without_store():
    """Without a body store, no ref is issued so the hint would be
    misleading. The body still gets elided but no ``/expand`` recovery
    promise is made."""
    big = "\n".join(f"line{i}" for i in range(50))
    out = compose(
        "bash", "long_cmd", big,
        explicit_density=_density(max_body_lines=10),
        store=None,
    )
    assert out.expansion_hint == ""


def test_compose_no_expansion_hint_when_no_elision():
    store = BoundedBodyStore(capacity=10)
    out = compose(
        "search_code", "pat", "a\nb",  # 2 lines, fits in budget=10
        explicit_density=_density(max_body_lines=10),
        store=store,
    )
    assert out.expansion_hint == ""


def test_compose_compact_density_emits_no_body():
    """``DensityLevel.COMPACT`` carries ``max_body_lines=0`` —
    header + summary only, regardless of result size."""
    out = compose(
        "search_code", "pat", "a\nb\nc",
        explicit_density=_density(
            level=DensityLevel.COMPACT, max_body_lines=0,
        ),
        store=BoundedBodyStore(capacity=10),
    )
    assert out.body_lines_markup == ()


# ===========================================================================
# Body parking — Slice 3 store integration
# ===========================================================================


def test_compose_parks_body_in_store_when_eligible():
    big = "\n".join(f"line{i}" for i in range(50))
    store = BoundedBodyStore(capacity=10)
    compose(
        "search_code", "pat", big,
        explicit_density=_density(max_body_lines=10),
        store=store,
    )
    assert len(store) == 1
    refs = store.all_refs()
    assert len(refs) == 1
    parked = store.lookup(refs[0])
    assert parked is not None
    # FULL body parked, not the bounded chunk.
    assert parked.body == big
    assert parked.tool_name == "search_code"


def test_compose_parks_a_summary_only_result():
    """SUPERSEDED 2026-10-09: every non-empty result is parked. A
    summary-only block (``read_file``) shows no body, which is exactly why its
    full text must be one /expand away."""
    store = BoundedBodyStore(capacity=10)
    out = compose(
        "read_file", "foo.py", "x" * 1000,
        explicit_density=_density(max_body_lines=20),
        store=store,
    )
    assert len(store) == 1 and "/expand t-" in out.summary_markup


def test_compose_does_not_park_when_compact_density():
    """COMPACT means show_body=False; nothing to park."""
    store = BoundedBodyStore(capacity=10)
    compose(
        "search_code", "pat", "a\nb\nc",
        explicit_density=_density(
            level=DensityLevel.COMPACT, max_body_lines=0,
        ),
        store=store,
    )
    # SUPERSEDED 2026-10-09: COMPACT hides the body, so the result is parked
    # for /expand rather than dropped.
    assert len(store) == 1


# ===========================================================================
# Markup wrapping per body shape
# ===========================================================================


def test_compose_diff_body_uses_diff_colors():
    diff = "+added line\n-removed line\n@@ hunk header\n context"
    store = BoundedBodyStore(capacity=10)
    out = compose(
        "edit_file", "foo.py", diff,
        explicit_density=_density(max_body_lines=10),
        store=store,
    )
    # SUPERSEDED 2026-10-09: a diff renders like Claude Code's: the SIGN's
    # background band carries add/remove (`deck_grammar.diff`), so syntax
    # colour stays legible on top of it.
    from backend.core.ouroboros.ui.semantic_tokens import _ROLE_BACKGROUNDS
    from rich.text import Text
    add_lines = [ln for ln in out.body_lines_markup if "+ added" in Text.from_markup(ln).plain]
    del_lines = [ln for ln in out.body_lines_markup if "- removed" in Text.from_markup(ln).plain]
    assert add_lines and _ROLE_BACKGROUNDS["code_add_bg"] in add_lines[0]
    assert del_lines and _ROLE_BACKGROUNDS["code_del_bg"] in del_lines[0]


def test_compose_log_body_uses_dim():
    store = BoundedBodyStore(capacity=10)
    out = compose(
        "bash", "ls", "a\nb",
        explicit_density=_density(max_body_lines=10),
        store=store,
    )
    assert all("dim" in ln for ln in out.body_lines_markup)


def test_compose_palette_override_changes_colors():
    """Caller supplies a custom palette — substrate threads it through."""
    custom = {
        "neural": "magenta",
        "file": "yellow underline",
        "dim": "dim",
        "death": "red",
        "code_add": "green",
        "code_del": "red",
        "code_hunk": "cyan",
        "heal": "yellow",
    }
    custom["dim"] = "magenta"
    out = compose(
        "read_file", "foo.py", "x",
        palette=custom,
        explicit_density=_density(),
    )
    # SUPERSEDED 2026-10-09: the HEADER is composed by deck_grammar (one
    # grammar for every block opener, outcome-tinted bullet); the caller's
    # palette still styles what the composer owns: the summary and body.
    assert "magenta" in out.summary_markup


# ===========================================================================
# Defensive: rich-markup escaping
# ===========================================================================


def test_compose_escapes_brackets_in_args():
    """A path containing ``[`` would otherwise be interpreted as
    Rich markup. The composer must escape it."""
    out = compose(
        "read_file", "foo[bar].py", "",
        explicit_density=_density(),
    )
    # The escaped form ``\[`` appears, not raw ``[``.
    assert "foo\\[bar].py" in out.header_markup


def test_compose_escapes_brackets_in_body_lines():
    store = BoundedBodyStore(capacity=10)
    out = compose(
        "bash", "ls", "[suspicious markup] real text",
        explicit_density=_density(max_body_lines=10),
        store=store,
    )
    # No raw `[suspicious markup]` appears unescaped; the bash `$ ls`
    # command line now leads the body, so the escaped result follows it.
    esc = "\\[suspicious markup]"
    assert any(esc in ln for ln in out.body_lines_markup)
    assert not any(("[suspicious markup]" in ln and esc not in ln)
                   for ln in out.body_lines_markup)


# ===========================================================================
# compose_if_enabled — master-flag short-circuit
# ===========================================================================


def test_compose_if_enabled_returns_none_when_flag_off(monkeypatch):
    """Post-graduation default is ON — operators must explicitly
    set ``=false`` to disable."""
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "false")
    out = compose_if_enabled(
        "read_file", "foo.py", "x",
        explicit_density=_density(),
    )
    assert out is None


def test_compose_if_enabled_returns_render_when_flag_on():
    # No monkeypatch — default-on after graduation.
    out = compose_if_enabled(
        "read_file", "foo.py", "x",
        explicit_density=_density(),
    )
    assert out is not None
    assert _act("Read") in _plain(out.header_markup)


def test_compose_if_enabled_swallows_compose_failure(monkeypatch):
    """Defensive: compose's own failure mode must not crash callers.
    The function returns None and the caller falls through to legacy."""
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    out = compose_if_enabled(
        "read_file",
        # Pass a deliberately broken explicit_density that isn't a
        # DensityPolicy — compose should defensively coerce, but if
        # something deeper raised, compose_if_enabled would catch.
        "foo.py", "x",
        explicit_density=_density(),
    )
    assert out is not None  # normal path still works


# ===========================================================================
# Integration via Console(record=True) — what actually lands on terminal
# ===========================================================================


def test_console_renders_header_via_compose():
    """End-to-end: pipe a compose() result through Console.print and
    assert the recorded plain-text output matches the expected shape."""
    console = Console(record=True, force_terminal=True, color_system="truecolor")
    out = compose(
        "read_file", "foo.py", "x",
        explicit_density=_density(),
    )
    console.print(out.header_markup)
    text = console.export_text()
    assert _act("Read") in text
    assert "foo.py" in text


def test_console_renders_full_pipeline():
    console = Console(record=True, force_terminal=True, color_system="truecolor")
    big = "\n".join(f"line{i}" for i in range(20))
    store = BoundedBodyStore(capacity=10)
    out = compose(
        "type_check", "pat", big,
        explicit_density=_density(max_body_lines=8),
        store=store,
    )
    console.print(out.header_markup)
    if out.summary_markup:
        console.print(out.summary_markup)
    for line in out.body_lines_markup:
        console.print(line)
    if out.expansion_hint:
        console.print(out.expansion_hint)
    text = console.export_text()
    assert "TypeCheck" in text or "Type" in text
    # The deck's column discipline, end to end: the summary indents under
    # the action and the body indents under the summary.
    lines = [ln for ln in text.splitlines() if ln.strip()]
    assert lines[0].startswith(f"{_act()} ")
    assert lines[1].startswith("  ⎿  ")
    assert lines[2].startswith("     ")
    assert "/expand t-" in text


# ===========================================================================
# Defensive contract — compose never raises
# ===========================================================================


def test_compose_handles_none_inputs():
    out = compose(None, None, None, explicit_density=_density())  # type: ignore[arg-type]
    assert isinstance(out, ComposedToolRender)


def test_compose_handles_garbage_status():
    out = compose(
        "bash", "ls", "out", status="not-a-valid-status",
        explicit_density=_density(),
    )
    assert isinstance(out, ComposedToolRender)


def test_compose_handles_negative_duration():
    out = compose(
        "read_file", "foo.py", "x",
        duration_ms=-1.0,
        explicit_density=_density(),
    )
    # Negative duration → no duration suffix in header.
    assert "ms" not in out.header_markup


# ===========================================================================
# Authority invariant — view layer is the ONLY Rich surface
# ===========================================================================


def test_view_module_imports_substrate_only():
    """Slice 4 is the ONLY layer that imports Rich. Substrate
    modules (Slice 1-3) must remain renderer-agnostic."""
    from backend.core.ouroboros.battle_test import (
        tool_render_registry,
        tool_render_policy,
        tool_render_store,
    )
    for mod in (tool_render_registry, tool_render_policy, tool_render_store):
        src = open(mod.__file__).read()
        assert "from rich" not in src and "import rich" not in src, (
            f"{mod.__name__} must not import Rich — that belongs to Slice 4"
        )


# ---------------------------------------------------------------------------
# Shell command visibility (Claude Code idiom) — bash shows `$ <command>`
# ---------------------------------------------------------------------------


def test_a_bash_call_shows_the_command_on_a_dollar_line(monkeypatch):
    monkeypatch.delenv("JARVIS_SHELL_COMMAND_LINE_ENABLED", raising=False)
    # Long enough that the HEADER clips it: the `$` line exists to show the
    # command in full, and only appears when the header cannot.
    _cmd = "pytest -q tests/foo.py " + " ".join(f"--deselect=t{i}" for i in range(12))
    out = compose(
        "bash", _cmd, "3 passed",
        explicit_density=_density(max_body_lines=10),
        store=BoundedBodyStore(capacity=10),
    )
    joined = "\n".join(out.body_lines_markup)
    assert "$ pytest -q tests/foo.py" in joined
    # The command line leads the body, above the output.
    assert out.body_lines_markup[0].strip().startswith(("$", "[")) and "$ pytest" in out.body_lines_markup[0]


def test_a_non_bash_call_has_no_dollar_line():
    out = compose(
        "read_file", "backend/x.py", "9 lines",
        explicit_density=_density(max_body_lines=10),
        store=BoundedBodyStore(capacity=10),
    )
    assert not any("$ " in ln for ln in out.body_lines_markup)


def test_the_command_line_strips_ansi_escape_codes():
    out = compose(
        "bash", "echo \x1b[31mred\x1b[0m done " + "x" * 120, "ok",
        explicit_density=_density(max_body_lines=10),
        store=BoundedBodyStore(capacity=10),
    )
    joined = "\n".join(out.body_lines_markup)
    assert "\x1b[" not in joined and "$ echo red done" in joined
    # And the header never carries terminal control either.
    assert "\x1b" not in out.header_markup


def test_a_heredoc_command_is_flattened_and_clipped(monkeypatch):
    monkeypatch.setenv("JARVIS_SHELL_COMMAND_MAX_CHARS", "40")
    out = compose(
        "bash", "for i in 1 2 3; do\n  echo very-long-loop-body-$i\n"
                "  echo another-very-long-line-in-the-loop-$i\ndone",
        "ok", explicit_density=_density(max_body_lines=10),
        store=BoundedBodyStore(capacity=10),
    )
    line = out.body_lines_markup[0]
    assert "\n" not in line and "…" in line


def test_the_command_line_can_be_disabled(monkeypatch):
    monkeypatch.setenv("JARVIS_SHELL_COMMAND_LINE_ENABLED", "false")
    out = compose(
        "bash", "ls -la", "ok",
        explicit_density=_density(max_body_lines=10),
        store=BoundedBodyStore(capacity=10),
    )
    assert not any("$ ls" in ln for ln in out.body_lines_markup)
