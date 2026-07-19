"""Crest bg-fill + Karen postlude — the 7:08 PM screenshot fixes.

Operator report: (a) the coil renders as separated "bricks" on terminal
profiles with line-spacing > 1.0 — foreground block glyphs cannot span
the leading gap; background color fills the whole line box, so interior
full-block cells now paint as bg-colored spaces. (b) Karen's briefing
line printed ABOVE the live crest (Rich routes prints above an active
Live) — briefing lines now queue on the conductor and render on the
clean post-ceremony screen.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.ui.awakening import AwakeningConductor
from backend.core.ouroboros.ui.crest import CrestCell, crest_fill_mode, render_cell
from backend.core.ouroboros.ui.theme import ColorTier


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("JARVIS_OV_CREST_FILL", raising=False)
    yield


def _cell(glyph: str) -> CrestCell:
    return CrestCell(x=0, y=0, glyph=glyph, kind="coil",
                     rgb=(10, 200, 30), delay_s=0.0)


# ---------------------------------------------------------------------------
# (a) bg-fill — solid strokes across line-spacing gaps
# ---------------------------------------------------------------------------


def test_full_block_paints_background():
    ch, style = render_cell(_cell("█"), ColorTier.TRUECOLOR)
    assert ch == " "
    assert style == "on rgb(10,200,30)"    # bg spans the line box (leading incl.)


def test_edge_quadrant_keeps_foreground_silhouette():
    for glyph in ("▙", "▛", "▚", "▀", "▗"):
        ch, style = render_cell(_cell(glyph), ColorTier.TRUECOLOR)
        assert ch == glyph
        assert style == "rgb(10,200,30)"


def test_glyph_mode_is_legacy(monkeypatch):
    monkeypatch.setenv("JARVIS_OV_CREST_FILL", "glyph")
    ch, style = render_cell(_cell("█"), ColorTier.TRUECOLOR)
    assert ch == "█" and style == "rgb(10,200,30)"


def test_sub_c256_tier_never_bg_paints():
    ch, _style = render_cell(_cell("█"), ColorTier.STANDARD)
    assert ch == "█"                        # 16-color: geometry unchanged


def test_fill_mode_default_and_validation(monkeypatch):
    assert crest_fill_mode() == "bg"
    monkeypatch.setenv("JARVIS_OV_CREST_FILL", "nonsense")
    assert crest_fill_mode() == "bg"


# ---------------------------------------------------------------------------
# (b) Karen postlude — never photobomb the ceremony
# ---------------------------------------------------------------------------


class _Console:
    def __init__(self) -> None:
        self.printed = []

    def print(self, *a, **_k) -> None:
        self.printed.append(str(a[0]) if a else "")


def _bare_conductor() -> AwakeningConductor:
    c = AwakeningConductor.__new__(AwakeningConductor)
    c._console = _Console()
    c.ceremony_active = False
    c._postlude = []
    return c


def test_briefing_queues_while_ceremony_live():
    c = _bare_conductor()
    c.ceremony_active = True
    assert c.queue_postlude("💭 Karen ▸ awake") is True
    assert c._console.printed == []          # NOTHING above the crest


def test_briefing_after_ceremony_falls_through():
    c = _bare_conductor()
    assert c.queue_postlude("late line") is False   # caller prints directly


def test_flush_renders_on_clean_screen():
    c = _bare_conductor()
    c.ceremony_active = True
    c.queue_postlude("💭 Karen ▸ awake")
    c.ceremony_active = False
    c._flush_postlude()
    assert any("Karen" in p for p in c._console.printed)
    assert c._postlude == []


def test_harness_sink_routes_via_postlude_pin():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    src = (root / "backend/core/ouroboros/battle_test/harness.py").read_text()
    body = src[src.index("def _speak_sink"):][:1600]
    assert "queue_postlude" in body
    # Fallback direct print survives for post-ceremony (slow-synth) lines.
    assert "console.print(rendered" in body


def test_run_flushes_postlude_pin():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    src = (root / "backend/core/ouroboros/ui/awakening.py").read_text()
    body = src[src.index("    async def run"):][:2000]
    assert "finally:" in body
    assert "_flush_postlude()" in body
