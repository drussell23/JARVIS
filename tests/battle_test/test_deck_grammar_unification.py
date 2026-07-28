"""ONE grammar in the deck — the structural defect an operator spotted.

`deck_grammar` owns the deck's column discipline: `⏺` at column 0, `⎿` at 2,
body at 5. `tool_render_view` emitted its own — summary and body both flat at
column 0 — and its headers came in two shapes, because fifteen of eighteen
descriptors had `cc_verb=None` and fell to an icon path.

So one screen showed:

    ⏺ Repair(L2 · iteration 1/5)
      ⎿  fixture rebuilt from the live seam     ← deck_grammar
    ⏺ Update(risk_tier_floor.py)  90ms
    ⎿  +5 / -2 lines                            ← tool_render_view, flat
    --- a/backend/core/ouroboros/governance/…    ← body at column 0
    🔍 search_code "except Exception"           ← icon path, snake_case
    💻 bash $ python3 -m pytest -q

Three shapes, three naming conventions, two column disciplines. These tests
pin the single grammar that replaced them.
"""
from __future__ import annotations

import pytest
from rich.text import Text

from backend.core.ouroboros.battle_test.tool_render_registry import (
    _DESCRIPTORS, ToolStatus, get_descriptor,
)
from backend.core.ouroboros.battle_test.tool_render_view import compose
from backend.core.ouroboros.ui import deck_grammar as deck


def _plain(markup: str) -> str:
    return Text.from_markup(markup).plain


def _col(markup: str) -> int:
    p = _plain(markup)
    return len(p) - len(p.lstrip())


@pytest.fixture(autouse=True)
def _no_colour(monkeypatch):
    from backend.core.ouroboros.ui import theme
    monkeypatch.setattr(theme, "_active_tier_cache", theme.ColorTier.NONE)
    yield


class TestOneHeaderShape:
    @pytest.mark.parametrize("kind", sorted(_DESCRIPTORS))
    def test_every_builtin_renders_the_action_shape(self, kind):
        out = compose(kind, "arg", "result", duration_ms=10)
        assert _plain(out.header_markup).startswith("⏺ "), kind

    @pytest.mark.parametrize("kind", sorted(_DESCRIPTORS))
    def test_no_builtin_leaks_its_internal_id(self, kind):
        """`search_code` and `run_tests` are tool ids. `Search` and `Test`
        are what the operator is watching happen."""
        assert get_descriptor(kind).cc_verb
        assert kind not in _plain(compose(kind, "a", "r").header_markup)

    def test_an_unknown_tool_gets_the_same_shape_and_its_own_name(self):
        """An MCP-forwarded tool is still a tool call. A distinct layout for
        it would tell the operator about our descriptor table."""
        out = compose("mcp_slack_post_message", "#eng", "ok")
        plain = _plain(out.header_markup)
        assert plain.startswith("⏺ McpSlackPostMessage(")
        assert "_default" not in plain


class TestOneColumnDiscipline:
    def test_the_summary_sits_in_the_decks_continuation_column(self):
        out = compose("read_file", "foo.py", "a\nb\nc")
        assert _col(out.summary_markup) == 2

    def test_body_lines_sit_in_the_decks_body_column(self):
        out = compose("bash", "pytest -q", "FAILED a\nFAILED b\n2 failed")
        assert out.body_lines_markup
        for line in out.body_lines_markup:
            assert _col(line) == deck.DETAIL_COLUMN

    def test_a_tool_block_and_a_grammar_block_agree(self):
        """The two must be indistinguishable in structure — that is the
        whole point. A reader should not be able to tell which module drew
        which line."""
        tool = compose("read_file", "foo.py", "x\ny")
        assert _col(tool.header_markup) == _col(deck.action("Repair", "L2"))
        assert _col(tool.summary_markup) == _col(deck.detail("rebuilt"))

    @pytest.mark.parametrize("kind,result", [
        ("edit_file", "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new"),
        ("bash", "line one\nline two"),
        ("search_code", "a.py:1: hit\nb.py:2: hit"),
    ])
    def test_every_body_SHAPE_indents(self, kind, result):
        """Diff, log and match bodies each have their own colour wrapper.
        Position is owned once, at the seam where they are applied — not
        four times, once per wrapper."""
        out = compose(kind, "arg", result)
        assert out.body_lines_markup
        assert all(_col(ln) == deck.DETAIL_COLUMN
                   for ln in out.body_lines_markup)

    def test_the_elision_marker_indents_with_its_body(self):
        """It described lines at column 5 from column 3."""
        from backend.core.ouroboros.battle_test.tool_render_store import (
            BoundedBodyStore,
        )
        big = "\n".join(f"line {i}" for i in range(200))
        out = compose("bash", "cmd", big, store=BoundedBodyStore())
        markers = [ln for ln in out.body_lines_markup if "elided" in ln]
        assert markers and _col(markers[0]) == deck.DETAIL_COLUMN


class TestTheGlyphIsCanonical:
    def test_the_continuation_mark_comes_from_the_theme(self):
        """It was `⏎` (U+23CE) here and `⎿` (U+23BF) everywhere else, while
        four docstrings claimed otherwise."""
        from backend.core.ouroboros.ui.theme import mark
        out = compose("read_file", "foo.py", "x")
        assert mark("detail") in _plain(out.summary_markup)

    def test_status_failure_is_marked(self):
        out = compose("bash", "cmd", "boom", status=ToolStatus.ERROR)
        assert "✗" in _plain(out.header_markup)


class TestArgumentsAreStyledByWhatTheyARE:
    """A regression introduced by the unification itself.

    Routing every tool through the CC-verb branch also routed every argument
    through the FILE style, because the only descriptors that had ever
    reached that branch were file tools. `Bash(python3 -m pytest …)` and
    `Search("except Exception")` came out blue-underlined — this deck's
    convention for a path you can open — inviting a click that goes nowhere.
    """

    def test_a_path_still_looks_like_a_path(self):
        out = compose("read_file", "governance/risk_tier_floor.py", "x")
        assert "underline" in out.header_markup

    @pytest.mark.parametrize("kind,arg", [
        ("bash", "python3 -m pytest -q"),
        ("search_code", "except Exception"),
        ("type_check", "mypy backend/"),
    ])
    def test_a_command_or_pattern_does_NOT(self, kind, arg):
        out = compose(kind, arg, "x")
        assert "underline" not in out.header_markup, (
            f"{kind} argument styled as a clickable path"
        )

    def test_every_descriptor_declares_what_its_argument_is(self):
        """Data, not a code path — the same shape as the verbs."""
        for kind, desc in _DESCRIPTORS.items():
            assert desc.arg_kind in ("path", "command", "text", "url"), (
                f"{kind} has arg_kind={desc.arg_kind!r}"
            )

    def test_an_unknown_kind_degrades_to_the_old_behaviour(self):
        """Every descriptor written before `arg_kind` existed behaved as a
        file tool; an unrecognised value must keep doing that rather than
        rendering unstyled."""
        from backend.core.ouroboros.battle_test.tool_render_view import (
            _arg_colour,
        )
        assert _arg_colour("nonsense", None) == _arg_colour("path", None)
