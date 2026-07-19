"""Half-block pixel renderer — the fidelity-class upgrade spine.

Operator verdict on the quadrant renderer: lumpy, smeary, "amateur".
Root cause was structural — one color per cell. The half-block path
renders ``▀`` with independent fg (upper pixel) + bg (lower pixel):
1×2 true pixels per cell, per-pixel gradient, coverage-alpha
anti-aliasing, and coil scale banding so the mark reads as a snake.
Plus: the ceremony now leaves the PERSISTENT emblem in scrollback
instead of erasing it (the shot-1 vanishing-crest glitch).
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.ui import crest
from backend.core.ouroboros.ui.theme import ColorTier


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in (
        "JARVIS_OV_CREST_RENDERER", "JARVIS_OV_CREST_BAND_AMP",
        "JARVIS_OV_CREST_FEATHER",
    ):
        monkeypatch.delenv(k, raising=False)
    crest._generate_pixels_cached.cache_clear()
    crest._generate_cached.cache_clear()
    yield
    crest._generate_pixels_cached.cache_clear()
    crest._generate_cached.cache_clear()


def _pf():
    pf = crest.generate_crest_pixels(80, 40)
    assert pf is not None
    return pf


# ---------------------------------------------------------------------------
# (1) The pixel raster — 2× vertical resolution, AA, banding
# ---------------------------------------------------------------------------


def test_pixel_frame_doubles_vertical_resolution():
    pf = _pf()
    assert pf.px_rows == pf.rows * 2
    assert len(pf.pixels) > 400                   # a real raster, not a sketch


def test_coverage_alpha_antialiasing_edges_darker_than_core():
    """True AA: edge pixels (partial coverage) carry dimmer color than
    full-coverage interior pixels of the same region."""
    pf = _pf()
    lums = sorted(sum(rgb) for rgb, _d in pf.pixels.values())
    # A healthy AA distribution has a real spread: the dimmest edge
    # pixels are well below the brightest interior pixels.
    assert lums[0] < lums[-1] * 0.55


def test_no_channel_clipping():
    pf = _pf()
    for rgb, _d in pf.pixels.values():
        for ch in rgb:
            assert 0 <= ch <= 255


def test_scale_banding_modulates_coil(monkeypatch):
    monkeypatch.setenv("JARVIS_OV_CREST_BAND_AMP", "0.0")
    crest._generate_pixels_cached.cache_clear()
    flat = crest.generate_crest_pixels(80, 40)
    monkeypatch.setenv("JARVIS_OV_CREST_BAND_AMP", "0.2")
    crest._generate_pixels_cached.cache_clear()
    banded = crest.generate_crest_pixels(80, 40)
    assert flat is not None and banded is not None
    flat_lum = [sum(rgb) for rgb, _ in flat.pixels.values()]
    band_lum = [sum(rgb) for rgb, _ in banded.pixels.values()]
    import statistics
    # Banding widens luminance variance along the coil.
    assert statistics.pstdev(band_lum) > statistics.pstdev(flat_lum)


def test_reveal_clock_threads_through_pixels():
    pf = _pf()
    full = crest.pixels_to_text(pf).plain
    early = crest.pixels_to_text(pf, elapsed=0.05).plain
    assert len(early.strip()) < len(full.strip())


def test_halfblock_text_uses_dual_color_cells():
    pf = _pf()
    from rich.text import Text
    text = crest.pixels_to_text(pf)
    assert isinstance(text, Text)
    dual = [
        sp for sp in text.spans
        if " on rgb(" in str(sp.style)
    ]
    assert dual                                    # fg+bg pixels exist


# ---------------------------------------------------------------------------
# (2) Renderer dispatch — capability-aware, env-tunable
# ---------------------------------------------------------------------------


def _frame():
    f = crest.generate_crest(80, 40, tier=ColorTier.TRUECOLOR, unicode_ok=True)
    assert f.unavailable_reason is None
    return f


def test_auto_dispatch_prefers_halfblock_on_truecolor():
    text = crest.render_crest_auto(_frame(), ColorTier.TRUECOLOR)
    assert "▀" in text.plain                       # pixel path


def test_auto_dispatch_falls_back_on_16_color():
    text = crest.render_crest_auto(_frame(), ColorTier.STANDARD)
    assert "▀" not in text.plain or "█" in text.plain  # quadrant path


def test_renderer_env_forces_quadrant(monkeypatch):
    monkeypatch.setenv("JARVIS_OV_CREST_RENDERER", "quadrant")
    f = _frame()
    auto = crest.render_crest_auto(f, ColorTier.TRUECOLOR)
    legacy = crest.frame_to_text(f, ColorTier.TRUECOLOR)
    assert auto.plain == legacy.plain              # byte-identical fallback


# ---------------------------------------------------------------------------
# (3) Persistent emblem — the vanishing-crest glitch is dead
# ---------------------------------------------------------------------------


def test_ceremony_prints_persistent_emblem_pin():
    from pathlib import Path
    src = (
        Path(__file__).resolve().parents[2]
        / "backend/core/ouroboros/ui/awakening.py"
    ).read_text()
    body = src[src.index("async def _run_animated"):]
    body = body[:body.index("async def _cool_down")]
    # After the Live (transient) closes, the FULL crest prints into
    # scrollback BEFORE the cooled header.
    assert "PERSISTENT EMBLEM" in body
    assert body.index("self._console.print(self._render_crest_text(") < \
        body.index("self._print_cooled_header()")


def test_conductor_renders_via_auto_dispatch_pin():
    from pathlib import Path
    src = (
        Path(__file__).resolve().parents[2]
        / "backend/core/ouroboros/ui/awakening.py"
    ).read_text()
    assert "render_crest_auto" in src


def test_static_emblem_uses_auto_dispatch_pin():
    from pathlib import Path
    src = (
        Path(__file__).resolve().parents[2]
        / "backend/core/ouroboros/ui/crest.py"
    ).read_text()
    body = src[src.index("def print_static_crest"):][:1600]
    assert "render_crest_auto(frame, tier)" in body
