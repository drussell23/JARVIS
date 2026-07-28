"""The arbiter's decision, made into containers.

`viewport_arbiter` answers "which regions fit, and how" and holds no widgets
on purpose. This is the prompt_toolkit consumer that was missing — the reason
`/layout`, transcript mode and lanes all waited on the same thing.

Not a port. `split_layout` renders three regions in RICH, and a `rich.Layout`
cannot mount inside a `prompt_toolkit.Application`. Calling it dead code was
wrong; it belongs to SerpentFlow's own console and stays there.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.battle_test.region_layout import (
    RegionSources, build_region_tree, dynamic_region_container,
    region_layout_enabled,
)
from backend.core.ouroboros.battle_test.viewport_arbiter import (
    ViewportArbiter,
)


def _win(label: str = "x"):
    from prompt_toolkit.layout.containers import Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    return lambda: Window(FormattedTextControl(label))


def _sources() -> RegionSources:
    return RegionSources(deck=_win("DECK"), lanes=_win("LANES"),
                         transcript=_win("TRANSCRIPT"))


def _arbiter() -> ViewportArbiter:
    arbiter = ViewportArbiter()
    arbiter.request("lanes", True)
    arbiter.request("transcript", True)
    return arbiter


def _size(cols: int, rows: int = 40):
    return lambda: type("S", (), {"columns": cols, "rows": rows})


# --------------------------------------------------------------------------
# the tree follows the arbiter
# --------------------------------------------------------------------------

def test_a_wide_terminal_builds_three_columns() -> None:
    from prompt_toolkit.layout.containers import HSplit

    tree = build_region_tree(_arbiter().arbitrate(200, 40), _sources())
    assert isinstance(tree, HSplit)


def test_a_narrow_terminal_floats_instead_of_splitting() -> None:
    """A FLOAT draws OVER the deck rather than beside it — the same
    FloatContainer the `/` palette established."""
    from prompt_toolkit.layout.containers import FloatContainer

    tree = build_region_tree(_arbiter().arbitrate(80, 40), _sources())
    assert isinstance(tree, FloatContainer)
    assert tree.floats


def test_hidden_regions_are_never_built() -> None:
    """A region that does not fit must cost nothing — building its widget
    anyway defeats the arbiter's entire purpose."""
    built = []

    def _tracking(label: str):
        def _factory():
            built.append(label)
            return _win(label)()
        return _factory

    sources = RegionSources(deck=_tracking("deck"), lanes=_tracking("lanes"),
                            transcript=_tracking("transcript"))
    build_region_tree(_arbiter().arbitrate(20, 40), sources)
    assert "transcript" not in built


def test_the_prompt_is_appended_below_the_body() -> None:
    from prompt_toolkit.layout.containers import HSplit

    tree = build_region_tree(_arbiter().arbitrate(200, 40), _sources(),
                             prompt=_win("PROMPT")())
    assert isinstance(tree, HSplit)
    assert len(tree.children) == 2


# --------------------------------------------------------------------------
# the layout is a FUNCTION, not a structure
# --------------------------------------------------------------------------

def test_a_resize_re_derives_rather_than_mutates() -> None:
    """THE design point. Rebuilding and reassigning `app.layout.container` on
    SIGWINCH discards focus and scroll state living on the widgets, and races
    a renderer that may be midway through a frame referencing the tree being
    replaced. A factory has neither problem."""
    from prompt_toolkit.layout.containers import DynamicContainer, FloatContainer, HSplit

    dims = {"cols": 200}
    container = dynamic_region_container(
        _arbiter(), _sources(),
        size=lambda: type("S", (), {"columns": dims["cols"], "rows": 40}),
    )
    assert isinstance(container, DynamicContainer)

    wide = container.get_container()
    dims["cols"] = 40
    narrow = container.get_container()

    assert isinstance(wide, HSplit)
    assert isinstance(narrow, FloatContainer)
    assert wide is not narrow, "the tree was mutated instead of re-derived"


def test_the_factory_never_raises_into_the_renderer() -> None:
    """An exception in a render factory is a blank cockpit."""
    class _Exploding:
        def arbitrate(self, *_a, **_k):
            raise RuntimeError("arbiter died")

    container = dynamic_region_container(_Exploding(), _sources(),
                                         size=_size(120))
    assert container.get_container() is not None


def test_a_broken_region_is_dropped_not_propagated() -> None:
    """One broken panel must not take the cockpit down with it."""
    def _boom():
        raise RuntimeError("panel died")

    sources = RegionSources(deck=_win("DECK"), lanes=_boom,
                            transcript=_win("T"))
    assert build_region_tree(_arbiter().arbitrate(200, 40), sources) is not None


# --------------------------------------------------------------------------
# widths come from the arbiter, not from prompt_toolkit
# --------------------------------------------------------------------------

def test_column_widths_are_EXACT() -> None:
    """A ranged dimension advertises willingness to absorb slack, and VSplit
    hands leftover to whichever child will take it — the prompt learned this
    the hard way. The arbiter already decided these widths."""
    from prompt_toolkit.layout.dimension import Dimension

    from backend.core.ouroboros.battle_test.region_layout import _dimension

    dim = _dimension(28)
    assert dim.min == dim.max == dim.preferred == 28


def test_everything_floating_still_leaves_something_underneath() -> None:
    """The arbiter guarantees the deck is never demoted below FLOAT, but the
    builder must not assume it — a tree with no body has nothing to draw on."""
    from backend.core.ouroboros.battle_test.viewport_arbiter import (
        FLOAT, Placement,
    )

    tree = build_region_tree(
        [Placement("deck", FLOAT, 40), Placement("lanes", FLOAT, 28)],
        _sources(),
    )
    assert tree is not None


def test_no_regions_at_all_is_survivable() -> None:
    assert build_region_tree([], _sources()) is not None


def test_the_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_REGION_LAYOUT_ENABLED", "0")
    assert region_layout_enabled() is False
    container = dynamic_region_container(_arbiter(), _sources(),
                                         size=_size(200))
    assert container.get_container() is not None


def test_it_does_NOT_import_rich() -> None:
    """`split_layout` is Rich and stays SerpentFlow's. Mixing the two
    rendering models is what made this a build rather than a port."""
    import ast
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    src = (repo / "backend/core/ouroboros/battle_test/"
           "region_layout.py").read_text()
    imported = {
        (n.module or "") for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.ImportFrom)
    }
    assert not any("rich" in name for name in imported)
