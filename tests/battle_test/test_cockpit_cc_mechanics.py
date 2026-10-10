"""Cockpit mechanics, Claude Code's flow with O+V's look (2026-10-09).

From the operator's screenshot: a wrapped tool result fell back to column 0
out from under its ``⎿``; a result's last line ended mid-word (``functio``);
one elision was announced twice ("elided" in the body, "parked" under it);
exploration tools dumped raw listings; ``⏺Search`` lost its space in Windows
Terminal; the slash palette painted over the newest transcript lines.
"""
from __future__ import annotations

import inspect
from io import StringIO

import pytest
from rich.console import Console

from backend.core.ouroboros.ui.block_wrap import block
from backend.core.ouroboros.ui.text_bounds import clip_lines


@pytest.fixture(autouse=True)
def _unicode_terminal(monkeypatch):
    """These tests are written in the glyphs a UTF-8 cockpit shows; the ASCII
    degradation has its own test, which overrides this."""
    from backend.core.ouroboros.ui import theme
    monkeypatch.setattr(theme, "supports_unicode", lambda env=None: True)


def _render(line: str, width: int = 60) -> list:
    c = Console(file=StringIO(), width=width, force_terminal=False, color_system=None)
    c.print(block(line))
    return [ln.rstrip() for ln in c.file.getvalue().splitlines()]


# ------------------------------------------------------------------ blocks


def test_a_wrapped_result_stays_under_its_continuation_glyph():
    rows = _render("  ⎿  (no matches for pattern='a|b|c|d|e|f|g|h|i|j|k|l|m|n|o|p|q|r|s|t|u|v|w|x|y|z' "
                   "glob=backend/api/monitoring_api.py). Try a broader file_glob.")
    assert rows[0].startswith("  ⎿  (no matches")
    assert len(rows) > 1
    assert all(r.startswith("     ") for r in rows[1:])        # never column 0


def test_an_action_line_wraps_under_its_verb():
    rows = _render("● Search(\"" + "x" * 90 + "\")  115ms")
    assert rows[0].startswith("● Search(")
    assert all(r.startswith("  ") for r in rows[1:])


def test_a_long_unbroken_token_folds_inside_the_column():
    rows = _render("  ⎿  " + "y" * 150, width=40)
    assert all(len(r) <= 40 for r in rows)
    assert all(r.startswith("     ") for r in rows[1:])


def test_a_plain_line_wraps_normally():
    rows = _render("plain words " * 12, width=40)
    assert len(rows) > 1


@pytest.mark.parametrize("line", ["", "   ", "⎿", "[bold unbalanced", "\x1b[31mred\x1b[0m"])
def test_block_never_raises(line):
    _render(line)


def test_every_surface_lays_lines_out_as_blocks():
    from backend.core.ouroboros.battle_test import bipartite_layout, presentation_restraint
    from backend.core.ouroboros.cli import ov
    from backend.core.ouroboros.governance import claude_style_transport
    assert "block(_line_to_text(ln))" in inspect.getsource(bipartite_layout)
    assert "block(markup)" in inspect.getsource(presentation_restraint.print_fit)
    # Each line the transcript flow emits is laid out as a block.
    assert "_block(_ln)" in inspect.getsource(ov._render_markup_frame)
    assert "block(text)" in inspect.getsource(claude_style_transport)


# ------------------------------------------------------------------ bounds


def test_clip_cuts_at_a_line_and_says_what_it_left_out():
    text = "\n".join(f"function: get_{i} (line {i})" for i in range(40))
    out, omitted = clip_lines(text, 120)
    assert omitted > 0
    kept, marker = out.rsplit("\n", 1)
    assert all(line.startswith("function: get_") and line.endswith(")") for line in kept.splitlines())
    assert marker.startswith("…") and "not kept" in marker


def test_clip_leaves_fitting_text_alone():
    assert clip_lines("short", 100) == ("short", 0)


def test_clip_cuts_a_single_long_line_at_a_word():
    out, _ = clip_lines("word " * 50, 60)
    assert out.split("\n")[0].endswith("word")


def test_previews_are_bounded_by_lines_everywhere():
    from backend.core.ouroboros.governance import tool_executor, tool_narration
    assert "_clip_preview(" in inspect.getsource(tool_executor)
    assert "clip_lines(" in inspect.getsource(tool_narration)


# ------------------------------------------------------------------ elision


def _compose(tool, result):
    from backend.core.ouroboros.battle_test.tool_render_store import BoundedBodyStore
    from backend.core.ouroboros.battle_test.tool_render_view import compose
    return compose(tool, "pkg/mod.py", result, store=BoundedBodyStore(), op_id="op-1")


def test_one_marker_carries_the_way_back():
    from backend.core.ouroboros.battle_test.tool_render_registry import is_elision_line
    r = _compose("bash", "\n".join(f"line {i}" for i in range(60)) + "\n3 failed, 44 passed")
    markers = [b for b in r.body_lines_markup if "/expand" in b]
    assert len(markers) == 1 and "elided" not in markers[0]
    assert r.expansion_hint == ""
    assert "/expand" not in r.summary_markup
    assert any(is_elision_line(ln) for ln in ("… +12 lines · /expand t-3", "… +2 more lines elided …"))


@pytest.mark.parametrize("tool", [
    "list_symbols", "list_dir", "glob_files", "search_code", "get_callers",
    "git_log", "git_blame", "web_fetch", "web_search", "read_file",
])
def test_exploration_tools_collapse_to_an_expandable_summary(tool):
    r = _compose(tool, "\n".join(f"function: f{i} (line {i})" for i in range(30)))
    assert r.body_lines_markup == ()
    assert "/expand t-" in r.summary_markup


def test_a_fully_shown_body_offers_no_expand():
    r = _compose("bash", "ok\n")
    assert "/expand" not in r.summary_markup


# ------------------------------------------------------------------ glyphs


def test_the_action_mark_is_the_green_dot(monkeypatch):
    """The operator's request: ● in O+V's green, not ⏺. Owned by the theme,
    tinted by the outcome, so one table answers every surface."""
    from backend.core.ouroboros.ui import deck_grammar, theme
    monkeypatch.setattr(theme, "supports_unicode", lambda env=None: True)
    monkeypatch.setattr(theme, "active_tier", lambda: theme.ColorTier.TRUECOLOR)
    assert theme.mark("action") == "●"
    green = theme.semantic("venom_green", theme.ColorTier.TRUECOLOR)
    assert green and deck_grammar.bullet() == f"[{green}]●[/{green}]"
    assert deck_grammar.bullet(markup=False) == "●"
    red = theme.semantic(deck_grammar.FAILED_TONE, theme.ColorTier.TRUECOLOR)
    assert deck_grammar.bullet(deck_grammar.FAILED_TONE) == f"[{red}]●[/{red}]"


def test_the_mark_degrades_to_ascii(monkeypatch):
    from backend.core.ouroboros.ui import deck_grammar, theme
    monkeypatch.setattr(theme, "supports_unicode", lambda env=None: False)
    assert deck_grammar.bullet(markup=False) == "*"


def test_a_completed_header_is_tinted_by_its_outcome(monkeypatch):
    from backend.core.ouroboros.ui import deck_grammar, theme
    monkeypatch.setattr(theme, "active_tier", lambda: theme.ColorTier.TRUECOLOR)
    ok = _compose("read_file", "x")
    green = theme.semantic(deck_grammar.DONE_TONE, theme.ColorTier.TRUECOLOR)
    assert ok.header_markup.startswith(f"[{green}]")
    from backend.core.ouroboros.battle_test.tool_render_store import BoundedBodyStore
    from backend.core.ouroboros.battle_test.tool_render_view import compose
    bad = compose("read_file", "pkg/mod.py", "boom", status="error",
                  store=BoundedBodyStore(), op_id="op-1")
    red = theme.semantic(deck_grammar.FAILED_TONE, theme.ColorTier.TRUECOLOR)
    assert bad.header_markup.startswith(f"[{red}]")


def test_the_daemon_colours_for_the_DISPLAYING_cockpit(monkeypatch):
    """A daemon writes to a log, so its own probe says NONE. The cockpit's
    declared depth decides; a cockpit that did not say is not 'no colour'."""
    from backend.core.ouroboros.battle_test import terminal_capabilities as tc
    from backend.core.ouroboros.ui import theme
    monkeypatch.delenv("JARVIS_UI_THEME_FORCE_TIER", raising=False)
    monkeypatch.setattr(theme, "_active_tier_cache", theme.ColorTier.NONE)
    try:
        tc.declare("cc-a", tc.TerminalCapabilities(cols=120, rows=40, color_depth=16_777_216))
        tc.declare("cc-b", tc.TerminalCapabilities(cols=120, rows=40, color_depth=0))
        assert theme.active_tier() == theme.ColorTier.TRUECOLOR
        tc.declare("cc-c", tc.TerminalCapabilities(cols=120, rows=40, color_depth=8))
        assert theme.active_tier() == theme.ColorTier.STANDARD
    finally:
        for sid in ("cc-a", "cc-b", "cc-c"):
            tc.forget(sid)
    assert theme.active_tier() == theme.ColorTier.NONE


# ------------------------------------------------------------------ flow


def _flow(lines):
    from backend.core.ouroboros.ui.deck_grammar import TranscriptFlow
    f, out = TranscriptFlow(), []
    for ln in lines:
        out.extend(f.feed(ln))
    return out


def test_one_blank_before_each_action_and_none_at_the_top():
    assert _flow(["", "● A(x)", "  ⎿  r", "● B(y)"]) == \
        ["● A(x)", "  ⎿  r", "", "● B(y)"]


def test_no_blank_inside_a_block_and_runs_collapse():
    assert _flow(["● A(x)", "", "  ⎿  r", "", "", "body", "", "", "● B"]) == \
        ["● A(x)", "  ⎿  r", "", "body", "", "● B"]


def test_one_continuation_glyph_per_block():
    out = _flow(["● A(x)", "  ⎿  one", "  ⎿  two", "● B", "  ⎿  three"])
    assert out == ["● A(x)", "  ⎿  one", "     two", "", "● B", "  ⎿  three"]


def test_a_block_indented_by_its_sink_is_still_one_block():
    """The daemon mirrors op lines two columns in, so `⎿` lands at column 4.
    Measured from column 0 it read as body: blanks inside the block survived
    and every `⎿` stayed. Measured from the opener, it is the same block."""
    out = _flow(["  ● Read(a.py)", "", "    ⎿  3 lines", "    ⎿  more", "  ● B"])
    assert out == ["  ● Read(a.py)", "    ⎿  3 lines", "       more", "", "  ● B"]


def test_the_flow_never_raises_and_passes_unknown_lines_through():
    assert _flow([None, 42, "[bad markup"]) == ["42", "[bad markup"]


def test_the_canvas_applies_the_flow_at_its_single_sink():
    from backend.core.ouroboros.battle_test import bipartite_layout as bl
    src = inspect.getsource(bl.BipartiteLayout)
    assert "self._flow.feed(" in src and "def _append(" in src


# ------------------------------------------------------------------ duplicates


def test_the_summary_suffix_carries_the_ref_not_a_count():
    """`+5 / -2 lines · +2 lines · /expand t-8` read as two change sizes; the
    second counted denoised diff headers. The ref is the information."""
    import re
    from rich.text import Text
    plain = Text.from_markup(
        _compose("list_dir", "\n".join(f"f{i}.py" for i in range(30))).summary_markup,
    ).plain
    assert re.search(r" · /expand t-\d+$", plain)
    assert not re.search(r"\+\d+ lines? · /expand", plain)


@pytest.mark.parametrize("tool,result", [
    ("run_tests", "a PASSED\nb PASSED\n47 passed in 3.98s"),
    ("bash", "collected 3 items\n3 passed in 0.10s"),
])
def test_no_body_line_restates_the_summary(tool, result):
    from rich.text import Text
    from backend.core.ouroboros.ui import theme
    r = _compose(tool, result)
    said = Text.from_markup(r.summary_markup).plain.replace(
        theme.mark("detail"), "", 1).strip()
    bodies = [Text.from_markup(b).plain.strip() for b in r.body_lines_markup]
    assert said and said not in bodies


def test_a_short_command_is_not_repeated_under_its_header():
    from backend.core.ouroboros.battle_test.tool_render_store import BoundedBodyStore
    from backend.core.ouroboros.battle_test.tool_render_view import compose
    from rich.text import Text
    r = compose("bash", "pytest -q", "ok\n1 passed", store=BoundedBodyStore(), op_id="op")
    assert not any(Text.from_markup(b).plain.strip().startswith("$ ")
                   for b in r.body_lines_markup)


def test_a_clipped_command_still_shows_in_full():
    from backend.core.ouroboros.battle_test.tool_render_store import BoundedBodyStore
    from backend.core.ouroboros.battle_test.tool_render_view import compose
    from rich.text import Text
    cmd = "pytest " + " ".join(f"tests/x/test_{i}.py" for i in range(20))
    r = compose("bash", cmd, "ok", store=BoundedBodyStore(), op_id="op")
    assert any(Text.from_markup(b).plain.strip().startswith("$ ")
               for b in r.body_lines_markup)


# ------------------------------------------------------------------ live tool


def test_inflight_tools_track_parallel_calls_and_expire():
    from backend.core.ouroboros.battle_test.tool_inflight import InflightTools
    t = InflightTools()
    t.started("op", "Read(a)", now=100.0)
    t.started("op", "Search(b)", now=100.0)
    assert t.current("op", now=101.0) == "Search(b) (+1)"
    t.finished("op", "Search(b)")
    assert t.current("op", now=101.0) == "Read(a)"
    assert t.current("op", now=100.0 + 10 ** 6) == ""      # a hung tool goes quiet
    t.clear("op")
    assert t.current("op") == ""


def test_inflight_tools_are_bounded(monkeypatch):
    from backend.core.ouroboros.battle_test.tool_inflight import InflightTools
    monkeypatch.setenv("JARVIS_TOOL_INFLIGHT_MAX_OPS", "2")
    t = InflightTools()
    for i in range(5):
        t.started(f"op{i}", "Read(x)")
    assert [t.current(f"op{i}") for i in range(5)] == ["", "", "", "Read(x)", "Read(x)"]


# ------------------------------------------------------------------ palette


def test_palette_opens_below_the_input_by_default(monkeypatch):
    from backend.core.ouroboros.battle_test import bipartite_layout as bl
    monkeypatch.delenv("JARVIS_OV_PALETTE_PLACEMENT", raising=False)
    assert bl.palette_placement() == "below"
    monkeypatch.setenv("JARVIS_OV_PALETTE_PLACEMENT", "overlay")
    assert bl.palette_placement() == "overlay"
    monkeypatch.setenv("JARVIS_OV_PALETTE_PLACEMENT", "sideways")
    assert bl.palette_placement() == "below"
    src = inspect.getsource(bl)
    assert "rows += [_rule(0), prompt, _rule(1)]\n        if _palette_below:\n            rows.append(_palette)" in src
    assert "if _palette is not None and not _palette_below:" in src
