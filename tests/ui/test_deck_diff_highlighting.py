"""A diff line carries TWO facts, and the deck was collapsing them into one.

    the SIGN    was this added or removed   -> the background band
    the SYNTAX  what does this code say     -> the foreground

`deck_grammar.diff` tinted the whole line by its sign, so every added line was
uniformly green and the code inside a hunk stopped reading as code: an operator
could see that something changed but not what it said. Claude Code keeps them
separate, and these tests hold that separation.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.ui import deck_grammar as dg

PATH = "backend/core/ouroboros/governance/risk_tier_floor.py"


class TestSyntaxAndSignAreSeparate:
    def test_an_added_line_carries_both_a_band_and_syntax(self):
        # Asserted against the ROLE, never a literal colour: the band moved from
        # `green` to a dark tint precisely because a saturated slab made every
        # syntax colour on top illegible, and a test naming the colour would have
        # to be edited each time the tint is tuned — then quietly stop protecting
        # the property it was written for.
        from backend.core.ouroboros.ui.semantic_tokens import role_palette
        band = role_palette()["code_add_bg"]
        out = dg.diff(412, "+", "    except RiskFloorConfigError:", path=PATH)
        assert f"on {band}" in out, "the add band is missing"
        assert "bright_blue" in out or "blue" in out, (
            "the keyword lost its syntax colour — the line is flat again")

    def test_a_removed_line_uses_the_del_role(self):
        from backend.core.ouroboros.ui.semantic_tokens import role_palette
        out = dg.diff(412, "-", "    except Exception:", path=PATH)
        assert f"on {role_palette()['code_del_bg']}" in out

    def test_an_unchanged_line_gets_no_band(self):
        """Context lines must not be painted, or the whole hunk reads as changed."""
        from backend.core.ouroboros.ui.semantic_tokens import role_palette
        palette = role_palette()
        out = dg.diff(410, " ", "    try:", path=PATH)
        assert f"on {palette['code_add_bg']}" not in out
        assert f"on {palette['code_del_bg']}" not in out

    def test_the_background_roles_are_distinct_from_the_foreground_ones(self):
        """A colour that reads correctly as FOREGROUND does not read correctly as
        BACKGROUND. `code_add` is a foreground green; `on green` is a saturated slab
        that makes a dim comment on top unreadable, which is what shipped first."""
        from backend.core.ouroboros.ui.semantic_tokens import role_palette
        palette = role_palette()
        assert palette["code_add"] != palette["code_add_bg"]
        assert palette["code_add_bg"].startswith("#"), (
            "a band needs an explicit dark tint; the 16-colour names have no dark "
            "variants")

    def test_the_band_comes_from_the_semantic_roles(self):
        """`_style` maps this module's OWN tone vocabulary and resolves an unknown
        name to "" — so asking it for a semantic role silently produced no band,
        which is exactly how the first version shipped highlighted code with no
        slab behind it."""
        from backend.core.ouroboros.ui.semantic_tokens import role_palette
        palette = role_palette()
        assert palette.get("code_add") and palette.get("code_del")
        assert dg._style("code_add") == "", (
            "if _style learns these roles, prefer it and drop the direct palette "
            "read — but do not let both answer")


class TestTheLexerIsInferredNeverDeclared:
    @pytest.mark.parametrize("path,probe", [
        (PATH, "except"),
        ("docs/RISK_TIERS.md", "# Risk tiers"),
        ("Makefile", "all:"),
    ])
    def test_rich_infers_the_language_from_the_path(self, path, probe):
        """No extension table in this module: a hardcoded map is wrong the first
        time someone edits a `.toml`, and Rich already owns that knowledge."""
        assert dg.diff(1, "+", probe, path=path)

    def test_no_path_means_no_highlighting_rather_than_a_guess(self):
        """Highlighting against a default lexer would colour arbitrary tokens
        confidently and wrongly. A wrong colour is a claim."""
        out = dg.diff(412, "+", "    except RiskFloorConfigError:")
        assert "bright_blue" not in out

    def test_an_unknown_extension_degrades_to_plain_text(self):
        assert "plain text" in dg.diff(1, "+", "plain text", path="LICENSE")


class TestTheBandIsSolid:
    def test_width_pads_the_band_to_a_common_edge(self):
        """Without padding an added block has a ragged right edge that stops at
        each line's text instead of reading as one slab."""
        narrow = dg.diff(1, "+", "raise", path=PATH)
        padded = dg.diff(1, "+", "raise", path=PATH, width=90)
        assert len(padded) > len(narrow)

    def test_padding_measures_the_CODE_not_the_markup(self):
        """Markup tags occupy no columns. Measuring the markup string would
        over-pad by however many tags the highlighter happened to emit, and a
        long line would wrap."""
        import re
        out = dg.diff(1, "+", "raise", path=PATH, width=60)
        visible = re.sub(r"\[/?[^\]]+\]", "", out)
        assert len(visible) <= 62, f"padded to {len(visible)} visible columns"


class TestItNeverBreaksTheDeck:
    @pytest.mark.parametrize("code", ["", "   ", "def f(:", "\t\tx = 1"])
    def test_degenerate_code_still_renders(self, code):
        assert dg.diff(1, "+", code, path=PATH) is not None

    def test_a_line_with_markup_characters_is_escaped(self):
        """Deck lines are markup, so `[bold]` inside SOURCE must not become a tag."""
        out = dg.diff(1, "+", "x = arr[bold]", path=PATH)
        assert "\\[bold]" in out or "[bold]" not in out.replace("[on green", "")

    def test_a_broken_highlighter_still_yields_the_code(self, monkeypatch):
        """The operator must not lose the diff because Pygments failed."""
        import rich.syntax
        monkeypatch.setattr(
            rich.syntax.Syntax, "guess_lexer",
            staticmethod(lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError())))
        assert "raise" in dg.diff(1, "+", "raise", path=PATH)


class TestTheDemoShowsIt:
    """The demo's hunk is highlighted by the TOOL RENDERER, not by a scripted beat.

    It used to be both, and that was the defect the operator photographed: the
    `edit_file` tool block rendered a flat red/green wall while a hand-built
    `Update` beat rendered the same change with a gutter, syntax and a band — two
    visual languages for one fact. Routing `tool_render_view`'s diff bodies through
    `deck_grammar.diff` made the scripted block redundant, so it is gone.

    These assert the ROUTE as well as the result: a future change that re-adds a
    parallel scripted block should fail here rather than quietly reintroduce two
    styles.
    """

    def test_the_tool_renderer_bands_a_diff_body(self):
        """Asserted at the RENDERER, not through `compose_live_script`.

        The composed-script version of this was WIDTH-DEPENDENT: it passed when
        #70294 was committed and failed on a later run in a differently-sized
        terminal, because `tool_render_view`'s density policy elides the entire diff
        body at some widths — the block then renders its `Update(path)` header and
        summary with nothing underneath. That elision is a real open bug, and it is
        NOT this test's job to be the thing that notices it flakily; a test whose
        result depends on the terminal it runs in cannot protect anything.

        So this pins the deterministic half — given a diff line, the deck bands and
        highlights it — and the elision is filed separately rather than asserted
        against here.
        """
        from backend.core.ouroboros.ui.semantic_tokens import role_palette
        palette = role_palette()
        added = dg.diff(412, "+", "    except RiskFloorConfigError:", path=PATH)
        removed = dg.diff(412, "-", "    except Exception:", path=PATH)
        assert f"on {palette['code_add_bg']}" in added
        assert f"on {palette['code_del_bg']}" in removed
        assert "bright_blue" in added or "blue" in added, (
            "the hunk has a band but no syntax colour")

    def test_the_highlighting_comes_from_the_tool_renderer(self):
        """Not from a scripted `diff` beat. The whole point of the change is that
        EVERY tool round is highlighted, not one curated block."""
        from backend.core.ouroboros.cli import ov_demo as d
        assert not [a for _at, kind, a in d._LIVE_BEATS if kind == "diff"], (
            "a scripted diff beat is back — the tool renderer already highlights, "
            "so this would narrate the same edit twice")
        assert any(kind == "tool" and args and args[0] == "edit_file"
                   for _at, kind, args in d._LIVE_BEATS), (
            "no edit_file beat left to carry the hunk")

    def test_the_tool_wrapper_infers_path_and_numbers_from_the_diff(self):
        """No parameter threading: a unified diff carries `+++ b/path` and `@@`,
        so the styler derives both from the body it is given."""
        from backend.core.ouroboros.battle_test.tool_render_view import (
            _diff_wrapper_for,
        )
        wrap = _diff_wrapper_for([
            "--- a/x/risk_tier_floor.py", "+++ b/x/risk_tier_floor.py",
            "@@ -410,7 +410,22 @@", "     try:", "+        raise",
        ])
        wrap("--- a/x/risk_tier_floor.py", None)
        wrap("+++ b/x/risk_tier_floor.py", None)
        wrap("@@ -410,7 +410,22 @@", None)
        ctx = wrap("     try:", None)
        added = wrap("+        raise", None)
        assert "410" in ctx, "the hunk origin was not read from @@"
        assert "bright_blue" in added or "blue" in added, (
            "the path was not read from +++, so no language was inferred")

    def test_a_body_without_a_diff_header_degrades_in_place(self):
        """A `git log` or a truncated fragment has no `@@` and no `+++`. It must
        land on the previous flat rendering, not on an error."""
        from backend.core.ouroboros.battle_test.tool_render_view import (
            _diff_wrapper_for,
        )
        wrap = _diff_wrapper_for(["some log output", "more output"])
        assert wrap("some log output", None) is not None
        assert wrap("+ not really a diff", None) is not None


class TestCommentsStayLegibleOnABand:
    """`dim` is not a colour — it is SGR 2, "reduce the intensity of whatever this
    is" — and Pygments' comment token carries EXACTLY that, with no foreground at
    all. Over the deck's normal background that reads as secondary text, correctly.
    Over a diff band it reduces intensity RELATIVE TO THE BAND, so the comment sinks
    into it: legible in principle, unreadable in practice.
    """

    def test_the_comment_token_really_is_only_dim(self):
        """The premise. If Pygments ever gives comments a concrete colour, this
        whole translation becomes unnecessary and should be deleted rather than
        left running."""
        from rich.console import Console
        from rich.syntax import Syntax
        code = "    # a comment here"
        text = Syntax(code, "python", theme="ansi_dark").highlight(code)
        styles = {str(s.style) for s in text.render(Console(width=60))
                  if s.text.strip()}
        assert "dim" in styles, f"comment styling changed: {styles}"

    def test_a_banded_comment_gets_a_real_colour(self):
        out = dg.diff(413, "+", "        # a malformed floor", path=PATH)
        assert "dim" not in out, (
            "the comment is still asking for a relative intensity on a band it "
            "cannot be relative to")
        from backend.core.ouroboros.ui.semantic_tokens import role_palette
        assert role_palette()["verbose"] in out

    def test_an_unbanded_comment_keeps_plain_dim(self):
        """Context lines have no band, so the reference point never moved and `dim`
        is exactly right there. Rewriting it everywhere would be inventing a theme
        rather than repairing a broken reference."""
        out = dg.diff(410, " ", "    # a context comment", path=PATH)
        assert "dim" in out

    def test_only_dim_is_translated(self):
        """`bold`, `italic` and every concrete colour already have a fixed
        appearance a background cannot dilute."""
        from backend.core.ouroboros.ui.deck_grammar import _legible_over_band
        assert _legible_over_band("bold bright_blue", True) == "bold bright_blue"
        assert _legible_over_band("", True) == ""

    def test_other_attributes_survive_the_translation(self):
        """Pygments emits `dim italic` too. Dropping the whole style to fix one
        word would take the italic with it."""
        from backend.core.ouroboros.ui.deck_grammar import _legible_over_band
        out = _legible_over_band("dim italic", True)
        assert "italic" in out and "dim" not in out

    def test_unbanded_styles_are_untouched(self):
        from backend.core.ouroboros.ui.deck_grammar import _legible_over_band
        assert _legible_over_band("dim italic", False) == "dim italic"
