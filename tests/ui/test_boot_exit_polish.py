"""Unified Boot/Exit Polish Pass — labels, cinematics, feathering, version.

Operator mandates: reflection-based labels (no dict rot), a lifecycle
scope whose exit line degrades cleanly on missing data, feathering math
that can never clip negative, and TOML-sourced versioning.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.ui import boot_labels as bl
from backend.core.ouroboros.ui import crest


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("JARVIS_OV_CREST_FEATHER", raising=False)
    bl.reset_label_cache_for_tests()
    yield
    bl.reset_label_cache_for_tests()


# ---------------------------------------------------------------------------
# (1) Reflection labels — declared at the lifecycle site, no dict in the UI
# ---------------------------------------------------------------------------


def test_decorated_boot_method_reflects_its_label():
    assert bl.resolve_label("boot_governed_loop_service") == (
        "governed loop · the organism"
    )
    assert bl.resolve_label("boot_oracle") == "oracle · codebase index"


def test_undecorated_mark_humanizes():
    # Sub-marks with no harness method degrade algorithmically.
    assert bl.resolve_label("oracle_load_cache") == "oracle load cache"
    assert bl.humanize_mark("boot_git_index_guard") == "git index guard"
    assert bl.humanize_mark("harness_run_pre_boot_done") == "pre boot done"


def test_rename_follows_for_free():
    """The rot-proof property: an unknown mark NEVER renders raw
    plumbing prefixes; the floor always produces something human."""
    assert bl.resolve_label("boot_some_future_phase") == "some future phase"
    assert isinstance(bl.resolve_label(""), str)   # degenerate never raises


def test_wake_renderer_consumes_resolver_pin():
    from pathlib import Path
    src = (
        Path(__file__).resolve().parents[2]
        / "backend/core/ouroboros/ui/wake_sequence.py"
    ).read_text()
    assert "resolve_label" in src
    body = src[src.index("def render_frame"):][:2000]
    assert "resolve_label(name)" in body


def test_no_static_label_dict_in_ui_pin():
    from pathlib import Path
    src = (
        Path(__file__).resolve().parents[2]
        / "backend/core/ouroboros/ui/wake_sequence.py"
    ).read_text()
    assert "boot_governed_loop_service" not in src   # no dict rot in the UI


# ---------------------------------------------------------------------------
# (2) Exit cinematics — one conformed goodbye, null-safe
# ---------------------------------------------------------------------------


class _H:
    """Minimal harness duck for _emit_exit_cinematic."""

    def __init__(self) -> None:
        self.lines = []
        self._started_at = None
        self._governed_loop_service = None
        self._cost_tracker = None

    def _repl_print(self, msg: str) -> None:
        self.lines.append(msg)


def _emit(h, monkeypatch, cockpit=True):
    from backend.core.ouroboros.battle_test.harness import BattleTestHarness
    monkeypatch.setenv(
        "JARVIS_OV_PRESENTATION", "cockpit" if cockpit else "soak",
    )
    BattleTestHarness._emit_exit_cinematic(h)


def test_exit_line_with_full_data(monkeypatch):
    import time as _t

    class _Cost:
        total_cost = 0.42

    class _GLS:
        _completed_ops = {"a": 1, "b": 2}

    h = _H()
    h._started_at = _t.time() - 34 * 60
    h._cost_tracker = _Cost()
    h._governed_loop_service = _GLS()
    _emit(h, monkeypatch)
    assert len(h.lines) == 1
    line = h.lines[0]
    assert line.startswith("⏺ organism rests")
    assert "2 changes landed" in line
    assert "$0.42" in line
    assert "34m" in line


def test_exit_line_null_data_falls_back_clean(monkeypatch):
    h = _H()                                   # everything missing/None
    _emit(h, monkeypatch)
    assert h.lines == ["⏺ organism rests · session closed"]


def test_exit_line_soak_is_silent(monkeypatch):
    h = _H()
    _emit(h, monkeypatch, cockpit=False)
    assert h.lines == []                       # SOAK teardown byte-identical


def test_run_wrapped_in_lifecycle_scope_pin():
    from pathlib import Path
    src = (
        Path(__file__).resolve().parents[2]
        / "backend/core/ouroboros/battle_test/harness.py"
    ).read_text()
    body = src[src.index("    async def run(self)"):][:900]
    assert "async with self._exit_cinematic_scope():" in body
    scope = src[src.index("def _exit_cinematic_scope"):][:1600]
    assert "finally:" in scope
    assert "_emit_exit_cinematic()" in scope


# ---------------------------------------------------------------------------
# (3) Edge feathering — dimmed boundaries, clip-safe
# ---------------------------------------------------------------------------


def _frame(cols=80, rows=30):
    from backend.core.ouroboros.ui.theme import ColorTier
    crest._generate_cached.cache_clear()
    return crest.generate_crest(
        cols, rows, tier=ColorTier.TRUECOLOR, unicode_ok=True,
    )


def test_feathering_dims_boundary_not_interior():
    f = _frame()
    cells = {(c.x, c.y): c for c in f.cells}

    def neighbors(x, y):
        return sum(
            (x + dx, y + dy) in cells
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))
        )

    boundary = [c for c in f.cells if neighbors(c.x, c.y) < 4]
    interior = [c for c in f.cells if neighbors(c.x, c.y) == 4]
    assert boundary and interior
    b_lum = sum(sum(c.rgb) for c in boundary) / len(boundary)
    i_lum = sum(sum(c.rgb) for c in interior) / len(interior)
    assert b_lum < i_lum * 0.85                # edges measurably dimmer


def test_feathering_never_clips_negative_or_overflows():
    f = _frame()
    for c in f.cells:
        for ch in c.rgb:
            assert 0 <= ch <= 255


def test_feather_disable_and_clamp(monkeypatch):
    monkeypatch.setenv("JARVIS_OV_CREST_FEATHER", "1.0")
    f_off = _frame()
    lum_off = sum(sum(c.rgb) for c in f_off.cells)
    monkeypatch.setenv("JARVIS_OV_CREST_FEATHER", "0.65")
    f_on = _frame(cols=80, rows=30)
    lum_on = sum(sum(c.rgb) for c in f_on.cells)
    assert lum_on < lum_off                    # feathering removes light
    monkeypatch.setenv("JARVIS_OV_CREST_FEATHER", "-3")
    assert crest._edge_feather() == 0.2        # clamped, never negative


# ---------------------------------------------------------------------------
# (4) Versioning — TOML-sourced, milestone-paired
# ---------------------------------------------------------------------------


def test_version_resolves_dynamically():
    from backend.core.ouroboros.cli.ov import resolve_version
    v = resolve_version()
    assert v and v != "0.0.0+unknown"
    assert v.count(".") >= 1                   # semver-ish, not hardcoded


def test_version_line_carries_milestone():
    from backend.core.ouroboros.cli.ov import RELEASE_NAME, version_line
    line = version_line()
    assert line.startswith("ov ")
    assert RELEASE_NAME in line


def test_version_verb_routes_and_exits_zero(capsys):
    from backend.core.ouroboros.cli import ov
    for argv in (["version"], ["--version"], ["-V"]):
        assert ov.resolve(argv).action == "version"
    assert ov.main(["--version"]) == 0
    assert "ov " in capsys.readouterr().out
