"""The mount — #70213's seam, finally consumed.

`viewport_arbiter` decided placements from #70187 and
`region_layout.dynamic_region_container` could build them from #70213, with
NOTHING reading either. `JARVIS_REGION_LAYOUT_ENABLED` read DARK on the
progress board for two PRs, which is precisely what that state exists to say.

These pin the two properties that make the mount safe to leave on: the root
still renders at every width, and the observable surface has not moved.
"""
from __future__ import annotations

import os
import pty

import pytest

from prompt_toolkit.layout.containers import Window

from backend.core.ouroboros.battle_test.bipartite_layout import (
    BipartiteLayout, _mount_region_layout, build_bipartite_application,
)


def _render_at(cols: int, app) -> str:
    """Render on a REAL pty output.

    prompt_toolkit's geometry differs under a dummy output, and the 8-row
    prompt slab was invisible until a real terminal drew it.
    """
    from prompt_toolkit.data_structures import Size
    from prompt_toolkit.output.vt100 import Vt100_Output

    _master, slave = pty.openpty()
    try:
        out = Vt100_Output(open(slave, "w", closefd=False),
                           lambda: Size(rows=30, columns=cols))
        app.output = out
        app.renderer.output = out
        app._redraw()
        return type(app.layout.container).__name__
    finally:
        os.close(_master)


@pytest.mark.parametrize("cols", [200, 120, 80, 40])
def test_renders_without_geometry_panic(cols):
    os.environ.setdefault("TERM", "xterm-256color")
    app = build_bipartite_application(BipartiteLayout(), on_accept=lambda t: None)
    assert _render_at(cols, app)


class TestConservativeMount:
    def test_chrome_is_never_a_region(self):
        # The prompt and toolbar must survive every arbitration. As columns
        # they would be something the arbiter could decide to hide at 40 cols,
        # and a cockpit that drops its prompt is not a narrower cockpit.
        rows = [Window(), Window(), Window()]
        root = _mount_region_layout(rows)
        assert type(root).__name__ == "HSplit"
        assert len(root.children) == len(rows)

    def test_falls_back_when_the_flag_is_off(self, monkeypatch):
        monkeypatch.setenv("JARVIS_REGION_LAYOUT_ENABLED", "0")
        rows = [Window(), Window()]
        root = _mount_region_layout(rows)
        assert type(root).__name__ == "HSplit"

    def test_empty_rows_do_not_explode(self):
        assert _mount_region_layout([]) is not None

    def test_mount_never_raises(self, monkeypatch):
        # This is the ROOT container of the daily surface. A status-quo
        # fallback always beats a cockpit that will not boot.
        import backend.core.ouroboros.battle_test.region_layout as rl

        def _boom(*_a, **_k):
            raise RuntimeError("arbiter down")

        monkeypatch.setattr(rl, "dynamic_region_container", _boom)
        root = _mount_region_layout([Window(), Window()])
        assert type(root).__name__ == "HSplit"


class TestSeamIsLive:
    def test_bipartite_actually_imports_the_region_layout(self):
        # The whole point. A seam with no consumer is what the board calls
        # DARK, and this module spent two PRs in that state.
        import ast
        import pathlib

        src = pathlib.Path(
            "backend/core/ouroboros/battle_test/bipartite_layout.py",
        ).read_text(encoding="utf-8")
        modules = {
            n.module or "" for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.ImportFrom)
        }
        assert any("region_layout" in m for m in modules)
        assert any("viewport_arbiter" in m for m in modules)
