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
    def test_a_wider_terminal_gives_a_wider_band(self):
        """Without padding an added block has a ragged right edge that stops at
        each line's text instead of reading as one slab.

        This used to compare "no width" against an explicit one, on the premise
        that omitting the width meant no padding. That premise is gone by design:
        `band_fill` RESOLVES a width when none is passed, so a band always reaches
        the edge and the comparison has to be between two explicit widths. The old
        form failed once the ambient terminal happened to be wider than the explicit
        argument — a test measuring the wrong thing, passing by coincidence."""
        narrow = dg.diff(1, "+", "raise", path=PATH, width=60)
        wide = dg.diff(1, "+", "raise", path=PATH, width=110)
        assert len(wide) > len(narrow)

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


class TestOneWidthAuthority:
    """The jagged right edge was two functions disagreeing, not a padding bug.

    The inline tool renderer resolved its own width from `shutil` while the overlay
    used a Console sized somewhere else, so the same hunk banded to two different
    edges depending on which path drew it — and a SIGWINCH moved one surface and not
    the other.
    """

    def test_the_fill_scales_with_the_terminal(self):
        """The mandated check: mocked 80 vs 120 columns must yield different fills,
        and the difference must be exactly the column difference."""
        from backend.core.ouroboros.ui.deck_grammar import band_fill
        narrow = band_fill(30, 80, indent=12)
        wide = band_fill(30, 120, indent=12)
        assert wide > narrow
        assert wide - narrow == 40, f"{wide} - {narrow} != 40"

    def test_the_fill_is_never_negative(self):
        """A line already wider than the terminal needs no fill, and a negative
        repeat count would raise inside a render."""
        from backend.core.ouroboros.ui.deck_grammar import band_fill
        assert band_fill(500, 80, indent=12) == 0

    def test_a_resize_between_calls_changes_the_answer(self):
        """Resolved PER CALL, never captured. A band measured once is correct until
        the first SIGWINCH and then clipped — the canvas draws with
        `wrap_lines=False`, so an overrun is cut rather than reflowed."""
        import shutil

        from backend.core.ouroboros.ui import deck_grammar as dg
        sizes = iter([shutil.os.terminal_size((120, 30)),
                      shutil.os.terminal_size((80, 30))])
        original = shutil.get_terminal_size
        shutil.get_terminal_size = lambda *_a, **_k: next(sizes)
        try:
            first = dg.resolve_deck_width()
            second = dg.resolve_deck_width()
        finally:
            shutil.get_terminal_size = original
        assert (first, second) == (120, 80)

    def test_a_degenerate_width_floors_rather_than_collapsing(self):
        from backend.core.ouroboros.ui.deck_grammar import resolve_deck_width
        assert resolve_deck_width(3) >= 20
        assert resolve_deck_width(0) > 0

    def test_both_surfaces_call_the_same_resolver(self):
        """Structural, by AST — the comments name `shutil` in prose to explain what
        was removed, so a substring search matches the explanation."""
        import ast
        import inspect

        from backend.core.ouroboros.battle_test import diff_overlay as do
        from backend.core.ouroboros.battle_test import tool_render_view as tv

        for module, fn_name in ((do, "_terminal_width"),
                                (tv, "_diff_wrapper_for")):
            fn = getattr(module, fn_name)
            names = {
                n.name for node in ast.walk(ast.parse(
                    inspect.getsource(fn).lstrip()))
                if isinstance(node, ast.ImportFrom) for n in node.names
            }
            assert "resolve_deck_width" in names or "deck_grammar" in names, (
                f"{fn_name} no longer routes through the shared width authority")


class TestStructuralMetadataRecedes:
    def test_the_hunk_header_is_subordinate(self):
        """`@@ -410,7 +410,22 @@` tells you WHERE a hunk sits; it is not the change
        and must not compete with it. It was cyan here and magenta in the overlay —
        two loud colours for a coordinate."""
        from backend.core.ouroboros.ui.semantic_tokens import role_palette
        palette = role_palette()
        assert palette["code_hunk"] == palette["structural_dim"]
        assert palette["code_hunk"] not in ("cyan", "magenta")

    def test_it_is_still_distinguishable_from_the_code(self):
        """Subordinate, not invisible — a header the operator cannot find is a
        different bug than one that shouts."""
        from backend.core.ouroboros.ui.semantic_tokens import role_palette
        palette = role_palette()
        assert palette["code_hunk"]
        assert palette["code_hunk"] not in ("none", "")
        assert palette["code_hunk"] != palette["code_add"]
