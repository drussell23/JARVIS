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
    assert "_block(safe)" in inspect.getsource(ov._render_markup_frame)
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


def test_the_action_glyph_follows_the_displaying_terminal(monkeypatch):
    from backend.core.ouroboros.battle_test import terminal_capabilities as tc
    from backend.core.ouroboros.ui import theme
    monkeypatch.setattr(theme, "supports_unicode", lambda env=None: True)
    monkeypatch.setattr(tc, "current_capabilities",
                        lambda: tc.TerminalCapabilities(cols=120, rows=40, symbol_glyphs=False))
    assert theme.mark("action") == "●"
    monkeypatch.setattr(tc, "current_capabilities",
                        lambda: tc.TerminalCapabilities(cols=120, rows=40, symbol_glyphs=True))
    assert theme.mark("action") == "⏺"
    assert theme.mark("action", unicode=True) == "⏺"         # explicit wins


@pytest.mark.parametrize("env,expected", [
    ({"JARVIS_OV_SYMBOL_GLYPHS": "1"}, True),
    ({"JARVIS_OV_SYMBOL_GLYPHS": "0"}, False),
])
def test_local_symbol_rule_honours_the_operator(env, expected):
    from backend.core.ouroboros.ui.theme import local_symbol_glyphs
    assert local_symbol_glyphs(env) is expected


def test_capability_wire_carries_symbol_glyphs_and_defaults_safe():
    from backend.core.ouroboros.battle_test.terminal_capabilities import TerminalCapabilities
    assert TerminalCapabilities.from_wire({"cols": 80, "rows": 24}).symbol_glyphs is True
    assert TerminalCapabilities.from_wire(
        {"cols": 80, "rows": 24, "symbol_glyphs": False}).symbol_glyphs is False
    from backend.core.ouroboros.battle_test import cockpit_attach
    assert '"symbol_glyphs": _local_symbol_glyphs()' in inspect.getsource(cockpit_attach)


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
