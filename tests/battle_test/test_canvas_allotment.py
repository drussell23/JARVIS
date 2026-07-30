"""The canvas must size itself against the region it is GIVEN.

The defect these tests hold shut: `BipartiteLayout` budgeted its deck against
the TERMINAL, while what bounds it is the leftover its parent `HSplit` hands it.
Measured at 200x60 with a 12-row crest mounted, the mux believed it had 39 rows,
produced 39 lines, and was given 8 — and because a `Window` draws a content block
from its TOP, the 8 it kept were the OLDEST. The operator watched a window parked
thirty-one lines behind the live tail, and the last beats of a script appeared
never to render at all.

`test_the_newest_line_is_visible_under_heavy_chrome` is the one that matters: it
asserts the operator-visible symptom rather than the arithmetic, so it fails for
any future reason the tail stops being reachable — a new strip, a taller header,
a changed dimension — not only for the cause fixed here.
"""
from __future__ import annotations

import re

import pytest


def _render_once(app, rows=60, cols=200):
    """Drive ONE real frame through prompt_toolkit's own render path.

    Not a simulation: `write_to_screen` is what the renderer calls, so the
    heights the controls receive here are the heights they receive in a cockpit.
    The input `BufferControl` wants a running loop and raises without one; the
    canvas is measured long before that, so the exception is caught and the
    partial frame is exactly what we need.
    """
    from prompt_toolkit.layout.containers import to_container
    from prompt_toolkit.layout.mouse_handlers import MouseHandlers
    from prompt_toolkit.layout.screen import Screen, WritePosition

    screen = Screen(default_char=None, initial_width=cols, initial_height=rows)
    try:
        to_container(app.layout.container).write_to_screen(
            screen, MouseHandlers(), WritePosition(0, 0, cols, rows),
            "", False, None,
        )
    except Exception:  # noqa: BLE001 — the input buffer needs an event loop
        pass
    return screen


def _heavy_app(mux, header_rows=12):
    """A cockpit wearing every strip it supports.

    The defect scaled with sibling cost, so a bare canvas-and-prompt layout
    would not have shown it — which is precisely why it survived.
    """
    from backend.core.ouroboros.battle_test.bipartite_layout import (
        build_bipartite_application,
    )
    return build_bipartite_application(
        mux,
        on_accept=lambda _t: None,
        toolbar=lambda: "toolbar",
        header=lambda: "\n".join(["crest"] * header_rows),
        header_height=header_rows,
        status_rows=lambda: ["status"],
        search_rows=lambda: ["search"],
        pending_rows=lambda: ["pending"],
        queue_rows=lambda: ["queued"],
        agent_rows=lambda: ["agent"],
    )


def _visible(mux):
    """The deck lines an operator would actually see, ANSI stripped."""
    plain = re.sub(r"\x1b\[[0-9;]*m", "", mux.render_canvas_ansi())
    return [ln for ln in plain.split("\n") if ln.strip()]


@pytest.fixture()
def fullscreen(monkeypatch):
    """The cockpit mode. Outside it the canvas is a bounded 8-row region, which
    is a different (and also previously broken) arithmetic."""
    monkeypatch.setenv("JARVIS_BIPARTITE_FULLSCREEN", "1")


class TestTheOperatorVisibleSymptom:
    def test_the_newest_line_is_visible_under_heavy_chrome(self, fullscreen):
        """THE regression. A deck whose newest line cannot be seen is not a
        transcript, and this is what "the last beats never rendered" was."""
        from backend.core.ouroboros.battle_test.bipartite_layout import (
            BipartiteLayout,
        )
        mux = BipartiteLayout()
        for i in range(200):
            mux.push_raw(f"deckline{i}")
        _render_once(_heavy_app(mux))
        lines = _visible(mux)
        assert any("deckline199" in ln for ln in lines), (
            "the NEWEST deck line is not visible — the canvas is rendering more "
            "lines than its window holds and the overflow is clipped from the "
            "bottom.\nvisible tail was: "
            + repr([ln.strip()[:40] for ln in lines[-3:]])
        )

    def test_the_deck_never_produces_more_lines_than_it_was_given(self, fullscreen):
        """The mechanism behind the symptom. Producing more rows than the window
        holds is silent — prompt_toolkit simply drops them — so nothing but an
        explicit comparison catches it."""
        from backend.core.ouroboros.battle_test.bipartite_layout import (
            BipartiteLayout,
        )
        mux = BipartiteLayout()
        for i in range(200):
            mux.push_raw(f"line{i}")
        _render_once(_heavy_app(mux))
        assert len(_visible(mux)) <= mux.height, (
            f"produced {len(_visible(mux))} lines into {mux.height} rows"
        )

    def test_a_taller_header_costs_the_deck_rows_not_its_tail(self, fullscreen):
        """A bigger header should make the deck SHORTER, never make it lose the
        end. Before the fix a 12-row crest cost the deck its last four beats
        while the deck went on believing it had the whole terminal."""
        from backend.core.ouroboros.battle_test.bipartite_layout import (
            BipartiteLayout,
        )
        heights = {}
        for header_rows in (3, 12, 20):
            mux = BipartiteLayout()
            for i in range(200):
                mux.push_raw(f"row{i}")
            _render_once(_heavy_app(mux, header_rows=header_rows))
            heights[header_rows] = mux.height
            assert any("row199" in ln for ln in _visible(mux)), (
                f"tail lost with a {header_rows}-row header")
        assert heights[3] > heights[12] > heights[20], (
            f"the deck did not shrink as the header grew: {heights}")


class TestObservationReplacesPrediction:
    def test_the_allotment_is_a_guess_until_a_frame_measures_it(self):
        """An estimate is a legitimate fallback before the first render. It must
        simply never claim to be a measurement — the rule that would have made
        the hardcoded 24 visible instead of load-bearing."""
        from backend.core.ouroboros.battle_test.bipartite_layout import (
            BipartiteLayout,
        )
        assert BipartiteLayout().allotment_measured is False

    def test_a_render_turns_the_guess_into_a_measurement(self, fullscreen):
        from backend.core.ouroboros.battle_test.bipartite_layout import (
            BipartiteLayout,
        )
        mux = BipartiteLayout()
        _render_once(_heavy_app(mux))
        assert mux.allotment_measured is True

    def test_the_measured_height_is_the_region_not_the_terminal(self, fullscreen):
        """The whole point. With 12 rows of crest plus six one-row strips, a
        60-row terminal cannot be leaving the canvas 60 — or 59."""
        from backend.core.ouroboros.battle_test.bipartite_layout import (
            BipartiteLayout,
        )
        mux = BipartiteLayout()
        _render_once(_heavy_app(mux), rows=60)
        assert 3 <= mux.height <= 60 - 12, (
            f"height {mux.height} does not account for the mounted chrome")

    def test_scroll_metrics_clamp_against_the_measured_budget(self, fullscreen):
        """#70279's lesson: the scroll keys move against this number, so a stale
        budget means a deck that can neither show its tail nor be scrolled to
        it. One source, both readers."""
        from backend.core.ouroboros.battle_test.bipartite_layout import (
            BipartiteLayout,
        )
        mux = BipartiteLayout()
        for i in range(200):
            mux.push_raw(f"x{i}")
        _render_once(_heavy_app(mux))
        total, budget = mux.scroll_metrics()
        assert budget == mux._line_budget()
        assert total == 200


class TestObserveAllotmentContract:
    """Every degenerate shape the framework can hand a control."""

    def _mux(self):
        from backend.core.ouroboros.battle_test.bipartite_layout import (
            BipartiteLayout,
        )
        return BipartiteLayout()

    @pytest.mark.parametrize("height", [None, 0, -5])
    def test_a_degenerate_height_is_refused_not_recorded(self, height):
        """`None` means "measure, do not draw"; 0 means a collapsed conditional
        container. Recording either as a budget renders an empty deck — the last
        good measurement is a better answer than a degenerate fresh one."""
        mux = self._mux()
        mux.observe_allotment(200, 40)
        before = mux.height
        mux.observe_allotment(200, height)
        assert mux.height == before

    def test_a_none_height_does_not_forge_a_measurement(self):
        """A control asked to measure has told us nothing about our size, so
        provenance must not flip. Otherwise the very first `create_content(w,
        None)` would brand the terminal estimate as measured."""
        mux = self._mux()
        mux.observe_allotment(200, None)
        assert mux.allotment_measured is False

    def test_width_is_taken_independently_of_height(self):
        """They arrive together and can fail separately; a valid width should
        not be discarded because the height was a measuring pass."""
        mux = self._mux()
        mux.observe_allotment(137, None)
        assert mux.width == 137

    def test_an_unchanged_allotment_reports_no_change(self):
        """The return value gates a repaint. Invalidating on every frame from
        inside a render is a repaint loop, so "same size" must be falsy."""
        mux = self._mux()
        mux.observe_allotment(200, 40)
        assert mux.observe_allotment(200, 40) is False

    def test_a_changed_allotment_reports_the_change(self):
        mux = self._mux()
        mux.observe_allotment(200, 40)
        assert mux.observe_allotment(200, 25) is True

    def test_an_unchanged_height_still_counts_as_measured(self):
        """An estimate that happens to equal the measurement is still an
        estimate until something measures it — the advisor's rule about a cap
        that coincides with a real blast radius."""
        mux = self._mux()
        seeded = mux.height
        mux.observe_allotment(None, seeded)
        assert mux.allotment_measured is True

    def test_garbage_never_raises_into_a_frame(self):
        """This runs inside `create_content`. An exception here is a dead
        cockpit, so the contract is total."""
        mux = self._mux()
        for bad in ("tall", object(), [], {}):
            assert mux.observe_allotment(bad, bad) is False


class TestNoPredictionRemains:
    def test_the_canvas_no_longer_guesses_from_the_terminal(self):
        """`size.rows - 1` was the prediction: one row reserved for chrome, true
        while the cockpit was a canvas and a prompt, wronger with every strip
        added since. A structural pin, because a well-meaning re-add would
        restore the defect and every behavioural test above would still pass —
        the prediction only loses to the measurement when both are present."""
        import ast
        import inspect

        from backend.core.ouroboros.battle_test import bipartite_layout as bl

        src = inspect.getsource(bl.build_bipartite_application)
        tree = ast.parse(src.lstrip())
        fragments = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "_canvas_fragments"
        ]
        assert fragments, "_canvas_fragments not found"
        body = ast.dump(fragments[0])
        assert "on_resize" not in body, (
            "the canvas is predicting its size from the terminal again; it must "
            "observe the allotment prompt_toolkit hands create_content")

    def test_observation_is_wired_into_the_control(self):
        """Behaviour, not spelling: build an app, render, and require that the
        mux was told a height it could not have guessed."""
        from backend.core.ouroboros.battle_test.bipartite_layout import (
            BipartiteLayout,
        )
        mux = BipartiteLayout(height=999)
        _render_once(_heavy_app(mux))
        assert mux.height != 999, (
            "a render did not reach observe_allotment — the measuring control "
            "is not mounted on the canvas window")
