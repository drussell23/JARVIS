"""The deck's column discipline — the property that made it necessary.

`ov demo live` shipped with a result line whose body sat at column 4 and a
diff hunk beneath it whose body sat at column 5, because each was a separate
f-string at a separate call site and nothing compared them. These tests assert
the COLUMNS agree, on the rendered text rather than on the markup, because the
column is what the operator sees and the tags are not.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.ui import deck_grammar as deck
from backend.core.ouroboros.ui import theme


@pytest.fixture(autouse=True)
def _truecolor(monkeypatch):
    """Pin the tier so every assertion runs against a STYLED line.

    Under a captured stdout the tier is NONE and every style resolves to the
    empty string — the one condition in which a column bug caused by markup
    could not possibly show up.
    """
    monkeypatch.setattr(theme, "_active_tier_cache", theme.ColorTier.TRUECOLOR)
    yield


def _plain(markup: str) -> str:
    """What the terminal actually prints — tags resolved away."""
    from rich.text import Text
    return Text.from_markup(markup).plain


def _column_of(line: str, marker: str) -> int:
    """Cell index where ``marker`` starts, on the rendered line.

    Measured in CELLS, not codepoints: a wide glyph earlier in the line moves
    the column without moving the index, and the codepoint answer would be the
    one that looks right in a test and wrong on a terminal.
    """
    from rich.cells import cell_len
    plain = _plain(line)
    return cell_len(plain[: plain.index(marker)])


class TestColumns:
    def test_action_body_sits_in_claude_codes_column(self):
        assert _plain(deck.action("Read", "foo.py")).startswith("⏺ Read(")

    def test_detail_body_and_diff_body_share_one_column(self):
        """THE regression. Two literals, one column, no one to notice.

        A four-digit line number fills the gutter exactly, so its left edge IS
        the hunk's left edge — the column the result above it must also start
        in for the diff to read as belonging to that result.
        """
        result = deck.detail("Read 847 lines")
        hunk = deck.diff(1234, "+", "    except Foo:")
        assert _column_of(result, "Read") == deck.DETAIL_COLUMN
        assert _column_of(hunk, "1234") == deck.DETAIL_COLUMN

    def test_detail_keeps_two_spaces_after_the_glyph(self):
        # `  ⎿  text`, not `  ⎿ text`. One space is where the drift started.
        assert _plain(deck.detail("x")) == "  ⎿  x"

    def test_a_wide_glyph_keeps_its_space(self):
        """`💭` is one codepoint and TWO cells.

        Padded by `len()` it welds to the first word — `💭the vision floor` —
        which is exactly how it reached an operator's screen.
        """
        plain = _plain(deck.voice("the vision floor raises"))
        assert plain.startswith("💭 the vision floor")

    def test_the_line_number_gutter_is_a_column_not_a_prefix(self):
        wide = _plain(deck.diff(9, "+", "x"))
        narrow = _plain(deck.diff(1234, "+", "x"))
        assert wide.index("+") == narrow.index("+")


class TestHierarchy:
    """§08's first Don't: 'a flat wall of one green — no hierarchy'."""

    def test_chrome_and_content_are_not_the_same_colour(self):
        line = deck.detail("Read 847 lines")
        assert theme.semantic("faint") in line   # the ⎿
        assert theme.semantic("muted") in line   # the text

    def test_outcome_tone_reaches_the_bullet(self):
        assert theme.semantic("crit") in deck.action("Validate", tone="crit")
        assert theme.semantic("ok") in deck.action("Validate")

    def test_additions_and_removals_are_told_apart(self):
        """The band belongs to the SIGN -- and it is a BACKGROUND role.

        This asserted the FOREGROUND `ok`/`crit` tones, which `diff` stopped
        using in eaa1d1468e (2026-07-30): `on <foreground green>` is a
        saturated slab that renders syntax-highlighted code illegible over it,
        so the hunk body moved to the dedicated `code_add_bg` / `code_del_bg`
        background roles. Red ever since, unseen because CI runs tests/unit
        and tests/integration and never tests/ui.

        The roles are read from `role_palette`, their declared owner, rather
        than restated as two hexes: the exact tint is a palette decision, and
        "an addition does not look like a removal" is the invariant this test
        is named for.
        """
        from backend.core.ouroboros.ui.semantic_tokens import role_palette
        palette = role_palette()
        add_band, del_band = palette["code_add_bg"], palette["code_del_bg"]
        assert add_band != del_band, "one tint cannot tell two signs apart"

        added, removed = deck.diff(1, "+", "x"), deck.diff(1, "-", "x")
        assert f"on {add_band}" in added
        assert f"on {del_band}" in removed
        # ...and neither sign wears the other's band.
        assert f"on {del_band}" not in added
        assert f"on {add_band}" not in removed


class TestContentIsNeverMarkup:
    def test_a_bracket_in_a_diff_survives(self):
        """`rows[0]` is code. A deck that eats it has lost the line."""
        assert "rows[0]" in _plain(deck.diff(1, "+", "    return rows[0]"))

    def test_a_bracketed_tag_shaped_string_is_not_a_tag(self):
        assert _plain(deck.detail("[bold]not a tag[/bold]")).endswith(
            "[bold]not a tag[/bold]")


class TestNeverRaises:
    """Chrome must never be what takes the surface down."""

    @pytest.mark.parametrize("call", [
        lambda: deck.action(None),                      # type: ignore[arg-type]
        lambda: deck.detail(None),                      # type: ignore[arg-type]
        lambda: deck.diff("nonsense", "+", "x"),        # type: ignore[arg-type]
        lambda: deck.voice(None),                       # type: ignore[arg-type]
    ])
    def test_junk_degrades(self, call):
        assert isinstance(call(), str)

    def test_a_dead_theme_still_yields_a_readable_line(self, monkeypatch):
        def _boom(*_a, **_k):
            raise RuntimeError("theme down")

        monkeypatch.setattr(theme, "semantic", _boom)
        # Uncoloured, but still in the right column and still legible.
        assert _plain(deck.detail("847 lines")) == "  ⎿  847 lines"


class TestDegradation:
    def test_ascii_locale_keeps_the_geometry(self, monkeypatch):
        """A no-UTF-8 terminal loses the glyphs, never the columns."""
        monkeypatch.setattr(theme, "supports_unicode", lambda *a, **k: False)
        assert _column_of(deck.diff(1234, "+", "x"), "1234") == (
            deck.DETAIL_COLUMN)
        assert _column_of(deck.detail("x"), "x") == deck.DETAIL_COLUMN
        assert "⎿" not in _plain(deck.detail("x"))

    def test_no_colour_tier_still_composes(self, monkeypatch):
        monkeypatch.setattr(theme, "_active_tier_cache", theme.ColorTier.NONE)
        assert deck.detail("847 lines") == "  ⎿  847 lines"
