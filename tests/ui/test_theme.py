"""Tests for the unified theme engine (backend/core/ouroboros/ui/theme.py).

TDD: written before the implementation. These pin the contract from the
ov-theme spec (docs/superpowers/specs/2026-07-06-ov-theme-and-boot-cockpit-design.md):

  * tier-indexed degradation (TRUECOLOR -> 256 -> 16 -> stripped)
  * a single Console factory (build_console)
  * glyph degradation via mark()
  * zero escape leakage at the NONE tier (bulletproof mandate #4)
"""
from __future__ import annotations

from rich.console import Console

from backend.core.ouroboros.ui import theme
from backend.core.ouroboros.ui.theme import ColorTier, Token


def _forced_console(color_system: str) -> Console:
    return Console(color_system=color_system, force_terminal=True)


class TestDetectTier:
    def test_truecolor_console_detects_truecolor(self) -> None:
        assert theme.detect_tier(_forced_console("truecolor")) is ColorTier.TRUECOLOR

    def test_256_console_detects_c256(self) -> None:
        assert theme.detect_tier(_forced_console("256")) is ColorTier.C256

    def test_standard_console_detects_standard(self) -> None:
        assert theme.detect_tier(_forced_console("standard")) is ColorTier.STANDARD

    def test_no_color_console_detects_none(self) -> None:
        assert theme.detect_tier(Console(color_system=None)) is ColorTier.NONE


class TestAccentDegradation:
    """The one accent color degrades across tiers per the spec table."""

    def test_truecolor_is_teal_hex(self) -> None:
        assert "#3aafa9" in theme.style_for(Token.ACCENT, ColorTier.TRUECOLOR).lower()

    def test_c256_uses_color_73(self) -> None:
        assert "73" in theme.style_for(Token.ACCENT, ColorTier.C256)

    def test_standard_is_cyan(self) -> None:
        assert "cyan" in theme.style_for(Token.ACCENT, ColorTier.STANDARD).lower()

    def test_none_tier_is_empty(self) -> None:
        assert theme.style_for(Token.ACCENT, ColorTier.NONE) == ""


class TestSupportsUnicode:
    def test_utf8_lang_is_true(self) -> None:
        assert theme.supports_unicode({"LANG": "en_US.UTF-8"}) is True

    def test_utf8_lc_all_is_true(self) -> None:
        assert theme.supports_unicode({"LC_ALL": "en_US.UTF-8"}) is True

    def test_non_utf8_lang_is_false(self) -> None:
        assert theme.supports_unicode({"LANG": "C"}) is False

    def test_empty_env_is_false(self) -> None:
        assert theme.supports_unicode({}) is False


class TestMark:
    def test_dot_unicode(self) -> None:
        assert theme.mark("dot", unicode=True) == "·"  # ·

    def test_dot_ascii(self) -> None:
        assert theme.mark("dot", unicode=False) == "-"

    def test_check_unicode(self) -> None:
        assert theme.mark("check", unicode=True) == "✓"  # ✓

    def test_check_ascii(self) -> None:
        assert theme.mark("check", unicode=False) == "OK"

    def test_unknown_mark_returns_empty(self) -> None:
        assert theme.mark("does-not-exist", unicode=True) == ""


class TestBuildConsole:
    def test_returns_a_console(self) -> None:
        assert isinstance(theme.build_console(), Console)

    def test_force_tier_none_emits_no_escapes(self) -> None:
        """NONE tier: styled text renders, but zero ANSI escapes leak."""
        console = theme.build_console(force_tier=ColorTier.NONE, force_terminal=True)
        with console.capture() as cap:
            console.print("[accent]hello[/accent]")
        out = cap.get()
        assert "hello" in out
        assert "\x1b[" not in out

    def test_force_tier_truecolor_colors_accent(self) -> None:
        """At truecolor the accent token actually emits color escapes."""
        console = theme.build_console(
            force_tier=ColorTier.TRUECOLOR, force_terminal=True
        )
        with console.capture() as cap:
            console.print("[accent]hello[/accent]")
        assert "\x1b[" in cap.get()

    def test_theme_defines_every_token(self) -> None:
        """Every semantic token must resolve at every tier (no KeyError)."""
        for tier in ColorTier:
            for token in Token:
                # Must not raise; NONE tier may be empty.
                theme.style_for(token, tier)


class TestRenderRulePrimitive:
    def test_render_rule_never_raises_across_tiers_and_widths(self) -> None:
        for tier in ColorTier:
            for width in (40, 80, 120):
                console = theme.build_console(force_tier=tier, width=width)
                theme.render_rule(console)  # must never raise
                theme.render_rule(console, label="boot")
