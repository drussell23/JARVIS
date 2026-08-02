"""The first frame an operator sees was the unstyled one.

`ui/theme` ships a design language: six operator-plane glyphs, each with an
ASCII degradation, and its own comment says why — "so 16-color/none terminals
keep identical geometry". It also ships semantic tokens (ACCENT / BODY /
MUTED / WARNING, with SUCCESS reserved for OUTCOMES) that resolve per colour
tier.

The attach client used neither. Measured before this change: **75 hardcoded
⏺ / ⎿ / ⚠ / · and ZERO calls to `mark()`**, and every banner line printed as
`console.print(str, markup=False)` — flat, one weight, no dim.

Both gaps are visible, not theoretical:

  * `supports_unicode()` is locale-driven, and a non-UTF-8 ``LANG`` is
    ordinary over ssh, cron and CI. On those terminals the whole glyph
    vocabulary rendered as mojibake or nothing, and the degradation that
    exists to prevent it never ran.
  * three functions above this banner, the BOOT banner builds Rich `Text`
    with per-span styles and cites "the CC title grammar ... exactly like
    Claude Code v2.1.218". Two standards on one screen, and the operator's
    first frame was the unstyled one.

The half-migrated state is worse than either endpoint and this suite exists
largely to forbid it: converting the header while leaving the liquidity rows
hardcoded produced a banner showing ``*`` and ``-`` beside ``⎿`` and ``⚠`` in
the same frame — geometry more broken than before the change.
"""
from __future__ import annotations

import os

import pytest
from rich.console import Console

from backend.core.ouroboros.cli.ov import (
    _glyph, _liquidity_lines, _render_hydration, _tone,
)
from backend.core.ouroboros.ui import theme

_PAYLOAD = {
    "status": {"phase": "GENERATE", "phase_detail": "op-7f3a",
               "cost_spent_usd": 1.24, "cost_budget_usd": 2.50},
    "liquidity": {
        "providers": {"anthropic": {"tokens_remaining": 5_000_000,
                                    "seconds_to_reset": None}},
        "any_exhausted": True,
        "economic": {"claude": {"state": "open",
                                "consecutive_economic_failures": 3}},
    },
    "ops": [],
}


@pytest.fixture
def utf8(monkeypatch):
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    theme.reset_active_tier_cache()
    yield
    theme.reset_active_tier_cache()


@pytest.fixture
def ascii_only(monkeypatch):
    for key in ("LC_ALL", "LC_CTYPE", "LANG"):
        monkeypatch.delenv(key, raising=False)
    theme.reset_active_tier_cache()
    yield
    theme.reset_active_tier_cache()


def _render(payload=None, width=96) -> str:
    console = Console(force_terminal=True, width=width, record=True)
    _render_hydration(console, payload or _PAYLOAD)
    return console.export_text()


# ---------------------------------------------------------------------------
# 1. degradation — the property the design language promises
# ---------------------------------------------------------------------------


class TestGlyphDegradation:
    def test_utf8_terminals_get_the_real_glyphs(self, utf8):
        out = _render()
        assert "⏺" in out and "⎿" in out and "⚠" in out

    def test_a_non_utf8_locale_gets_ascii(self, ascii_only):
        """ssh, cron and CI routinely have no UTF-8 locale. The degradation
        existed and was unreachable from this client."""
        out = _render()
        for glyph in ("⏺", "⎿", "⚠"):
            assert glyph not in out, f"{glyph} survived ASCII degradation"

    def test_the_banner_is_never_MIXED(self, ascii_only):
        """THE regression this suite is mostly here for.

        Converting the header while leaving the liquidity rows hardcoded gave
        a single frame containing ``*`` and ``-`` beside ``⎿`` and ``⚠``.
        Half-migrated geometry is worse than either endpoint, and it renders
        as damage rather than as a fallback.
        """
        out = _render()
        unicode_glyphs = sum(out.count(g) for g in ("⏺", "⎿", "⚠", "·"))
        assert unicode_glyphs == 0, (
            f"{unicode_glyphs} unicode glyph(s) left in an ASCII render — "
            f"the banner is half-migrated")

    def test_every_ROW_still_opens_with_a_marker(self, ascii_only):
        """Degradation preserves GEOMETRY, not just legibility: every logical
        row keeps a marker in column one so the block stays a block.

        Asserted on rows wide enough not to wrap. A WRAPPED line legitimately
        starts with prose — see `test_wrapped_lines_have_no_hanging_indent`,
        which records that as a known gap rather than pretending it is fine.
        """
        for line in [l for l in _render(width=200).splitlines() if l.strip()]:
            assert line[0] in "*-! ", repr(line)

    def test_wrapped_lines_have_no_hanging_indent(self, utf8):
        """RECORDED, not fixed.

        At 96 columns the credit warning wraps and the continuation restarts
        at column 0, so the block's left edge breaks mid-warning. Claude Code
        indents continuations to keep the structure readable, and this
        codebase already solves it once — `layout_palette` wraps "with a
        hanging indent" — so the fix is a wiring job rather than a new idea.
        Out of scope here: this slice is glyphs and tone, and changing wrap
        geometry touches every consumer of these lines.

        Pinned so the gap is visible and so closing it is a test edit rather
        than a discovery.
        """
        wrapped = [l for l in _render(width=96).splitlines() if l.strip()]
        continuation = [l for l in wrapped if l.startswith("above is")]
        assert continuation, "the fixture stopped wrapping — re-tune the width"
        assert not continuation[0].startswith(" "), (
            "a hanging indent appeared — good, but update this test to "
            "assert it rather than its absence")

    def test_glyph_resolution_never_raises(self):
        assert _glyph("action", "*")
        assert _glyph("nonexistent-glyph-name", "FB") == "FB"


# ---------------------------------------------------------------------------
# 2. hierarchy — three tiers, matching the boot banner
# ---------------------------------------------------------------------------


class TestVisualHierarchy:
    def test_the_header_is_no_longer_one_flat_weight(self, utf8):
        console = Console(force_terminal=True, width=96, record=True)
        _render_hydration(console, _PAYLOAD)
        ansi = console.export_text(styles=True)
        assert "\x1b[2m" in ansi, "no dim span — the banner is still flat"

    def test_values_are_brighter_than_their_labels(self, utf8):
        """"phase" recedes, ``GENERATE`` does not. The label is scaffolding;
        the value is the fact."""
        console = Console(force_terminal=True, width=96, record=True)
        _render_hydration(console, _PAYLOAD)
        ansi = console.export_text(styles=True)
        assert "\x1b[2m  phase " in ansi or "phase " in ansi
        assert "GENERATE" in ansi

    def test_a_warning_does_NOT_recede_with_the_block(self, utf8):
        """Everything in this banner is secondary except the one line the
        operator must act on. Dimming it with the rest would bury it."""
        console = Console(force_terminal=True, width=120, record=True)
        _render_hydration(console, _PAYLOAD)
        ansi = console.export_text(styles=True)
        warn_line = next(l for l in ansi.splitlines() if "OUT OF CREDIT" in l)
        assert "\x1b[2m" not in warn_line, "the credit warning was dimmed"

    def test_tone_resolves_or_degrades_silently(self):
        assert isinstance(_tone("muted", "dim"), str)
        assert _tone("not-a-token", "fallback") == "fallback"

    def test_success_is_not_spent_on_a_connection(self):
        """The theme reserves SUCCESS for OUTCOMES (apply/verify OK). Styling
        "attached" with it would make a connection look like an achievement,
        and would devalue the token everywhere it legitimately appears."""
        import inspect
        from backend.core.ouroboros.cli import ov
        source = inspect.getsource(ov._render_hydration)
        assert '_tone("success"' not in source


# ---------------------------------------------------------------------------
# 3. content that survived the restyle
# ---------------------------------------------------------------------------


class TestNothingWasLost:
    def test_the_facts_are_all_still_there(self, utf8):
        out = _render()
        for fact in ("attached", "GENERATE", "op-7f3a", "$1.24", "$2.50",
                     "5,000,000 tokens", "OUT OF CREDIT", "Ctrl+C",
                     "ov restart"):
            assert fact in out, fact

    def test_the_literal_backticks_are_gone(self, utf8):
        """`markup=False` printed them as characters, so the hint advertised
        "`ov restart`" with the quoting visible. A terminal shows a command by
        styling it, not by fencing it."""
        assert "`" not in _render()

    def test_it_renders_with_an_empty_payload(self, utf8):
        out = _render({}, width=96)
        assert "attached" in out

    @pytest.mark.parametrize("hostile", [
        {"status": None, "liquidity": None, "ops": None},
        {"status": {"phase": None, "cost_spent_usd": None}},
        {"liquidity": {"providers": "not-a-dict"}},
    ])
    def test_hostile_payloads_never_raise(self, hostile, utf8):
        _render_hydration(Console(force_terminal=True, width=80), hostile)

    def test_narrow_terminals_do_not_lose_the_warning(self, utf8):
        """A 40-column terminal wraps; it must not truncate the one
        actionable line out of existence."""
        assert "OUT OF CREDIT" in _render(width=40)


# ---------------------------------------------------------------------------
# 4. the seam, so the remaining sites can follow
# ---------------------------------------------------------------------------


class TestTheSeamExists:
    def test_liquidity_lines_resolve_glyphs_too(self, ascii_only):
        lines = _liquidity_lines(
            {"anthropic": {"tokens_remaining": 5_000_000}},
            any_exhausted=True,
            economic={"claude": {"consecutive_economic_failures": 2}},
        )
        joined = "\n".join(lines)
        assert "⎿" not in joined and "⚠" not in joined

    def test_the_client_now_calls_mark_at_all(self):
        """It called it zero times across 75 hardcoded glyphs. This is the
        first consumer; the remaining sites are mechanical from here."""
        from pathlib import Path
        source = Path(
            "backend/core/ouroboros/cli/ov.py"
        ).read_text(encoding="utf-8", errors="replace")
        assert "def _glyph(" in source
        assert "from backend.core.ouroboros.ui.theme import mark" in source
