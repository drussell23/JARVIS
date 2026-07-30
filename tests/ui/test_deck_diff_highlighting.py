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
    def test_the_script_carries_a_banded_update_block(self):
        from backend.core.ouroboros.cli import ov_demo as d
        script = d.compose_live_script()
        from backend.core.ouroboros.ui.semantic_tokens import role_palette
        palette = role_palette()
        marks = (f"on {palette['code_add_bg']}", f"on {palette['code_del_bg']}")
        banded = [l for _at, l in script
                  if isinstance(l, str) and any(m in l for m in marks)]
        assert banded, "ov demo live shows no syntax-highlighted hunk"
        assert any("bright_blue" in l or "blue" in l for l in banded), (
            "the demo's hunk has a band but no syntax colour")

    def test_the_hunk_beats_name_a_path(self):
        """The 4th beat arg is what lets the language be inferred."""
        from backend.core.ouroboros.cli import ov_demo as d
        hunks = [args for _at, kind, args in d._LIVE_BEATS if kind == "diff"]
        assert hunks, "no diff beats in the script"
        assert all(len(a) > 3 and str(a[3]).endswith(".py") for a in hunks)
