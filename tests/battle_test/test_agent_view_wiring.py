"""The agent view, mounted — and the two-process property that gates it.

`AgentRoster` shipped complete: fed from both seams, reaped, bounded, tested.
`render()` had ZERO production callers, so none of it reached a screen. The
naive fix — call `render()` from the cockpit — would have looked correct in
every unit test and drawn an empty roster forever in the surface operators
actually use, because under `ov attach` the cockpit is a DIFFERENT PROCESS
from the daemon that dispatches the agents.

So the tests that matter here are not "does it render". They are:

  * does the roster survive the process boundary,
  * does it retire when the daemon behind it stops answering,
  * and does the mount cost nothing when nothing is running.

Each of these has a failure mode that reads as normal operation, which is why
they are pinned rather than eyeballed.
"""
from __future__ import annotations

import json

import pytest

from backend.core.ouroboros.battle_test.agent_roster import (
    AgentRoster,
    render_roster,
    reset_roster_for_tests,
    reset_roster_visibility_for_tests,
    roster_visible,
    set_roster_visible,
    toggle_roster,
)


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def _isolated():
    reset_roster_for_tests()
    reset_roster_visibility_for_tests()
    yield
    reset_roster_for_tests()
    reset_roster_visibility_for_tests()


@pytest.fixture
def shown():
    """The roster VISIBLE, for the tests that are about what it draws.

    Visibility is off by default in production — the roster mounts below the
    caret and is asked for with `/tasks`. The tests below are about staleness,
    elapsed time, width and folding, none of which is a question about the
    mode, so they opt in explicitly rather than inherit it.

    Explicit rather than autouse, and that distinction is load-bearing: an
    autouse fixture would have quietly kept
    `test_a_frame_without_agents_does_not_clear_the_roster` passing on
    `[] == []` when the gate turned it off, which is the shape of a test that
    guards nothing while reporting green.
    """
    set_roster_visible(True)
    yield
    reset_roster_visibility_for_tests()


@pytest.fixture
def busy():
    """A daemon-side roster with two running agents and one finished."""
    clock = _Clock()
    roster = AgentRoster(clock=clock)
    roster.spawn("s1", "Explore", "Map ov completion architecture")
    clock.now += 40
    roster.spawn("s2", "Review", "Refute the risk_tier_floor fix")
    clock.now += 5
    roster.finish("s2", "finished")
    return roster, clock


# ---------------------------------------------------------------------------
# The property the whole design exists for
# ---------------------------------------------------------------------------


class TestCrossesTheProcessBoundary:
    def test_a_reader_with_no_roster_of_its_own_still_sees_the_agents(self, busy):
        """THE regression.

        The attach client dispatches nothing, so its module singleton is
        permanently empty. Rendering that would show an organism that never
        delegates — indistinguishable from one that delegates constantly.
        """
        roster, _clock = busy
        wire = json.loads(json.dumps(roster.snapshot()))   # the bridge

        from backend.core.ouroboros.battle_test.agent_roster import (
            get_agent_roster,
        )
        assert get_agent_roster().render() == [], (
            "a fresh process must have an empty local roster — if this fails "
            "the test is not proving anything about the boundary"
        )

        lines = render_roster(wire, width=100)
        assert any("Explore" in ln for ln in lines)
        assert any("Refute the risk_tier_floor fix" in ln for ln in lines)

    def test_the_snapshot_carries_no_clock_of_its_own(self, busy):
        """`time.monotonic()` is an arbitrary per-process origin.

        Shipping one and subtracting it from the reader's clock yields a
        duration wrong by however long the two processes have been alive —
        and wrong in a way that looks entirely plausible.
        """
        roster, _clock = busy
        for row in roster.snapshot()["rows"]:
            assert "started" not in row and "finished" not in row
            assert "elapsed_s" in row

    def test_the_snapshot_survives_json(self, busy):
        roster, _clock = busy
        snap = roster.snapshot()
        assert json.loads(json.dumps(snap)) == snap

    def test_local_and_remote_render_identically(self, busy):
        """One renderer, two sources. Two renderers would be edited months
        apart and the surfaces would quietly stop agreeing."""
        roster, _clock = busy
        assert roster.render(width=100) == render_roster(
            roster.snapshot(), width=100,
        )


# ---------------------------------------------------------------------------
# A dead daemon must not leave ghosts running
# ---------------------------------------------------------------------------


class TestStaleness:
    def test_a_stale_frame_retires_the_roster(self, busy, shown, monkeypatch):
        """If the daemon dies mid-dispatch, the last frame says three agents
        are running and will say so forever. The roster expires on the SAME
        window as the pulse — one definition of "lost contact", not two that
        drift."""
        import time
        from backend.core.ouroboros.cli.ov import AttachUI

        roster, _clock = busy
        ui = AttachUI()
        ui.on_telemetry({
            "kind": "heartbeat", "active": True,
            "agents": roster.snapshot(),
        })
        assert ui._agent_lines(), "a fresh frame should render"

        # Reach past the staleness window without waiting for it.
        ui._heartbeat_arrived = time.monotonic() - 10_000
        assert ui._agent_lines() == []

    def test_a_frame_without_agents_does_not_clear_the_roster(self, busy, shown):
        """An older daemon that has never heard of `agents` is not a daemon
        reporting an empty roster. Clearing on absence would blank the view
        against a peer that simply predates the field."""
        from backend.core.ouroboros.cli.ov import AttachUI

        roster, _clock = busy
        ui = AttachUI()
        ui.on_telemetry({"kind": "heartbeat", "active": True,
                         "agents": roster.snapshot()})
        before = ui._agent_lines()
        ui.on_telemetry({"kind": "heartbeat", "active": True})   # no key
        assert ui._agent_lines() == before

    def test_an_explicit_empty_snapshot_does_clear_it(self, busy, shown):
        """That daemon IS saying "nothing is running", which is news."""
        from backend.core.ouroboros.cli.ov import AttachUI

        roster, _clock = busy
        ui = AttachUI()
        ui.on_telemetry({"kind": "heartbeat", "active": True,
                         "agents": roster.snapshot()})
        assert ui._agent_lines()
        ui.on_telemetry({"kind": "heartbeat", "active": True,
                         "agents": {"rows": [], "total": 0, "hidden": 0}})
        assert ui._agent_lines() == []


# ---------------------------------------------------------------------------
# Elapsed between frames
# ---------------------------------------------------------------------------


class TestElapsed:
    def test_running_agents_advance_between_frames(self, busy):
        """Frames arrive at ~1 Hz. Without the age correction every duration
        freezes for a second then jumps, which reads as a stalled system."""
        roster, _clock = busy
        snap = roster.snapshot()
        fresh = render_roster(snap, age_s=0.0, width=100)
        later = render_roster(snap, age_s=5.0, width=100)
        assert "45s" in "\n".join(fresh)
        assert "50s" in "\n".join(later)

    def test_a_finished_agent_does_not_invent_motion(self, busy):
        """Its duration is settled. Advancing it would be a claim the reader
        has no way to check and the daemon never made."""
        roster, _clock = busy
        snap = roster.snapshot()
        review = [ln for ln in render_roster(snap, age_s=90.0, width=100)
                  if "Review" in ln]
        assert review and "5s" in review[0]


# ---------------------------------------------------------------------------
# Cost when nothing is happening
# ---------------------------------------------------------------------------


class TestCostsNothingWhenIdle:
    def test_an_empty_roster_renders_no_rows(self):
        assert render_roster({"rows": [], "total": 0, "hidden": 0}) == []
        assert render_roster(None) == []
        assert render_roster("not a snapshot") == []       # type: ignore[arg-type]

    def test_the_cockpit_row_collapses_to_nothing(self):
        """`ConditionalContainer`, not a zero-height Window: a Window that
        renders an empty string still occupies a row, and an idle cockpit
        must be EXACTLY as tall as it was before this existed."""
        from backend.core.ouroboros.battle_test.bipartite_layout import (
            build_agent_row,
        )
        row = build_agent_row(lambda: [])
        assert row is not None
        assert row.filter() is False

    def test_the_row_is_exactly_as_tall_as_the_roster(self):
        """`Dimension.exact`. A range like (min=0, max=8) reads as "grow if
        needed" and does the opposite — HSplit distributes leftover rows by
        weight, so a child whose max exceeds its preferred absorbs the slack
        and nails an eight-row slab open above the prompt."""
        from backend.core.ouroboros.battle_test.bipartite_layout import (
            build_agent_row,
        )
        lines = ["a", "b", "c", "d"]
        row = build_agent_row(lambda: lines)
        assert row.filter() is True
        height = row.content.height()
        assert height.min == height.max == height.preferred == len(lines)

    def test_a_raising_source_costs_the_rows_not_the_cockpit(self):
        from backend.core.ouroboros.battle_test.bipartite_layout import (
            build_agent_row,
        )

        def _boom():
            raise RuntimeError("roster down")

        row = build_agent_row(_boom)
        assert row is not None and row.filter() is False


# ---------------------------------------------------------------------------
# Bounds and adaptation
# ---------------------------------------------------------------------------


class TestBoundedAndAdaptive:
    def test_a_swarm_does_not_become_a_two_hundred_row_frame(self):
        """The cap is not only about screen space. This rides a 1 Hz
        heartbeat, and an unbounded roster would put 200 rows on the wire
        every second for an operator who cannot read 200 rows."""
        clock = _Clock()
        roster = AgentRoster(clock=clock, max_rows=8)
        for i in range(200):
            roster.spawn(f"w{i}", "Chunk", f"shard {i}")
        snap = roster.snapshot()
        assert len(snap["rows"]) == 8
        assert snap["hidden"] == snap["total"] - 8
        assert any("more" in ln for ln in render_roster(snap, width=100))

    def test_the_window_follows_the_environment(self, monkeypatch):
        """No hardcoded row budget: an operator with a tall terminal can ask
        for more without a code change or a restart."""
        clock = _Clock()
        roster = AgentRoster(clock=clock)          # None == follow the env
        for i in range(12):
            roster.spawn(f"w{i}", "Chunk", f"shard {i}")
        monkeypatch.setenv("JARVIS_AGENT_VIEW_ROWS", "3")
        assert len(roster.snapshot()["rows"]) == 3
        monkeypatch.setenv("JARVIS_AGENT_VIEW_ROWS", "10")
        assert len(roster.snapshot()["rows"]) == 10

    def test_a_narrow_terminal_drops_the_goal_rather_than_lying(self, busy):
        """Three characters and an ellipsis is not a goal. The kind and the
        duration still answer "is something running", which is the question
        the roster exists for."""
        roster, _clock = busy
        lines = render_roster(roster.snapshot(), width=30)
        body = [ln for ln in lines if "Explore" in ln][0]
        assert "Map" not in body
        assert "40s" in body or "45s" in body

    def test_width_is_the_readers_call(self, busy):
        """The daemon that composed the snapshot cannot know the terminal the
        snapshot will be drawn into — and two clients may differ."""
        roster, _clock = busy
        wide = render_roster(roster.snapshot(), width=200)
        narrow = render_roster(roster.snapshot(), width=50)
        assert wide != narrow


# ---------------------------------------------------------------------------
# The wiring itself
# ---------------------------------------------------------------------------


class TestTheMount:
    def test_the_heartbeat_carries_the_roster(self, monkeypatch):
        """Proven by BREAKING the composer and watching the payload lose the
        field — a truthy check would pass just as well against a hardcoded
        dict, which is the wiring failure being guarded."""
        import backend.core.ouroboros.battle_test.agent_roster as ar
        from backend.core.ouroboros.battle_test import attach_heartbeat as hb

        sentinel = {"schema_version": "roster.v1", "rows": [
            {"id": "x", "kind": "Explore", "goal": "SENTINEL",
             "state": "running", "elapsed_s": 1.0}],
            "total": 1, "running": 1, "hidden": 0}

        class _Fake:
            def snapshot(self, **_kw):
                return sentinel

        monkeypatch.setattr(ar, "get_agent_roster", lambda: _Fake())

        class _Snap:
            phase = "GENERATE"
            primary_op_id = ""
            phase_detail = ""
            provider = ""
            route = ""

        class _Builder:
            def snapshot(self):
                return _Snap()

        monkeypatch.setattr(
            "backend.core.ouroboros.battle_test.status_line"
            ".get_status_line_builder", lambda: _Builder(),
        )
        payload = hb.build_heartbeat_payload()
        assert payload is not None
        assert payload["agents"] == sentinel

    def test_the_cockpit_is_handed_the_clients_snapshot_not_its_own_roster(self):
        """The bug this whole change exists to avoid, pinned at the seam: the
        callable the cockpit renders must read the AttachUI, which holds the
        daemon's frame — never `get_agent_roster()`, which in this process is
        empty by construction."""
        import inspect
        from backend.core.ouroboros.cli.ov import _bipartite_attach_loop

        # Scoped to the ONE function that mounts the cockpit, not the module:
        # a module-wide search would be satisfied by any mention anywhere and
        # would keep passing after the seam moved.
        src = inspect.getsource(_bipartite_attach_loop)
        idx = src.find("agent_rows=")
        assert idx > 0, "the cockpit never receives an agent_rows source"
        wiring = src[idx:idx + 240]
        assert "_agent_lines" in wiring
        assert "get_agent_roster" not in wiring

    def test_the_keys_reach_the_canonical_registry(self):
        """A footer that advertises a key nothing binds is the defect the
        registry exists to prevent, so the hint is composed from the same
        declaration that registers them."""
        from backend.core.ouroboros.battle_test.agent_roster import roster_hint
        from backend.core.ouroboros.governance import keybinding_registry as kr

        AgentRoster()                       # registration happens on init
        actions = {e.action for e in kr.list_all()}
        assert {"select", "view"} <= actions
        assert roster_hint() == "↑/↓ to select · Enter to view"


class TestMasterFlag:
    def test_off_costs_nothing_anywhere(self, busy, monkeypatch):
        roster, _clock = busy
        monkeypatch.setenv("JARVIS_AGENT_VIEW_ENABLED", "0")
        assert roster.render() == []
        assert roster.snapshot()["rows"] == []
        assert render_roster({"rows": [{"id": "a", "kind": "k", "goal": "g",
                                        "state": "running",
                                        "elapsed_s": 1.0}]}) == []


class TestHeightBudget:
    """A 40-worker swarm on a 24-row cockpit must not become the cockpit."""

    def _swarm(self, n: int):
        clock = _Clock()
        roster = AgentRoster(clock=clock, max_rows=n)
        for i in range(n):
            roster.spawn(f"w{i}", "Chunk", f"shard {i}")
        return roster

    def test_the_roster_folds_to_its_row_budget(self):
        from backend.core.ouroboros.battle_test.agent_roster import (
            roster_line_budget,
        )
        snap = self._swarm(40).snapshot()
        budget = roster_line_budget(24)
        lines = render_roster(snap, width=100, max_lines=budget)
        assert lines and len(lines) <= budget

    def test_what_the_budget_folded_is_counted_not_dropped(self):
        """Two separate elisions — the producer's window and the height
        budget — and one honest number. Reporting either alone undercounts,
        and the operator reads this to decide if they are seeing all of it."""
        roster = self._swarm(40)
        snap = roster.snapshot(max_rows=10)      # producer already withheld 30
        assert snap["hidden"] == 30
        # 9 rows − 1 chrome − 1 count row = 7 agents drawn, so 3 more fold.
        lines = render_roster(snap, width=100, max_lines=9)
        more = [ln for ln in lines if "more" in ln]
        assert more and "33" in more[0]          # 30 withheld + 3 folded

    def test_an_unknown_height_does_not_fold_against_a_guess(self):
        from backend.core.ouroboros.battle_test.agent_roster import (
            roster_line_budget,
        )
        assert roster_line_budget(None) is None
        assert roster_line_budget(0) is None
        snap = self._swarm(12).snapshot(max_rows=12)
        # 12 agent rows + 1 chrome row (`main`) — the hint no longer costs
        # two of every render.
        assert len(render_roster(snap, width=100, max_lines=None)) == 12 + 1

    def test_the_share_is_proportional_not_a_row_count(self):
        """Eleven rows is a footer on a 60-row terminal and half the cockpit
        on a 24-row one."""
        from backend.core.ouroboros.battle_test.agent_roster import (
            roster_line_budget,
        )
        assert roster_line_budget(60) > roster_line_budget(24)

    def test_an_impossible_budget_renders_nothing_rather_than_chrome(self):
        """`main` and a count row with no agents between them is a header for
        a list that is not there.

        The threshold moved when the permanent hint was dropped: chrome is
        one row now, so a 3-row budget CAN say something honest (main, one
        agent, "… N more"). Two rows cannot, and that is where it goes
        silent.
        """
        snap = self._swarm(5).snapshot()
        assert render_roster(snap, width=100, max_lines=3) != []
        assert render_roster(snap, width=100, max_lines=2) == []

    def test_the_wire_window_is_not_the_display_window(self):
        """A 60-row client and a 24-row one attach to the same daemon.

        If the producer serialised only what the smallest reader could draw,
        the roomy one would be truncated by a peer's screen size — a coupling
        neither operator could see or explain.
        """
        from backend.core.ouroboros.battle_test.agent_roster import (
            roster_line_budget, roster_wire_rows,
        )
        snap = self._swarm(40).snapshot(max_rows=roster_wire_rows())
        tall = render_roster(snap, width=100,
                             max_lines=roster_line_budget(60))
        small = render_roster(snap, width=100,
                              max_lines=roster_line_budget(24))
        assert len(tall) > len(small), (
            "the tall terminal must draw more — if these match, the wire "
            "window is capping the display instead of the screen"
        )
        # And both still account for every agent they did not draw.
        for lines in (tall, small):
            drawn = sum(1 for ln in lines if "Chunk" in ln)
            more = [ln for ln in lines if "more" in ln]
            assert more and str(40 - drawn) in more[0]


# ---------------------------------------------------------------------------
# Rows below the caret are ASKED FOR
# ---------------------------------------------------------------------------


class TestVisibility:
    """The roster mounts BELOW the input, so every row it takes sits between
    the operator's cursor and the bottom of their screen.

    Claude Code puts nothing standing there — "the input box stays fixed at
    the bottom of the screen" — and keeps the running-subagent view behind a
    verb, stating the separation outright: the Ctrl+T checklist "is separate
    from the background-task view. To see running shells and subagents, use
    `/tasks` instead."

    This cockpit had it mounted permanently, so three workers and a sentinel
    cost five rows under the caret of an IDLE session.
    """

    def test_hidden_by_default(self):
        assert roster_visible() is False

    def test_the_env_seeds_the_session_and_the_verb_owns_it(self, monkeypatch):
        """Lazy resolution, not import-time: the flag is read from a render
        path, and a harness that imports this module before configuring its
        environment would otherwise be pinned to a value it never chose."""
        monkeypatch.setenv("JARVIS_AGENT_VIEW_ROSTER_VISIBLE", "1")
        reset_roster_visibility_for_tests()
        assert roster_visible() is True
        assert set_roster_visible(False) is False
        assert roster_visible() is False, "the verb must outrank the seed"

    def test_toggle_round_trips(self):
        assert toggle_roster() is True
        assert toggle_roster() is False

    def test_a_hidden_roster_costs_zero_rows_on_the_daemon_cockpit(self, busy):
        from backend.core.ouroboros.battle_test import serpent_flow

        assert serpent_flow._local_agent_rows() == []
        set_roster_visible(True)
        # Whether THIS process has agents is beside the point; what must be
        # true is that the provider stops short of the renderer when hidden
        # and reaches it when shown.
        assert isinstance(serpent_flow._local_agent_rows(), list)

    def test_a_hidden_roster_costs_zero_rows_on_the_attach_client(self, busy):
        from backend.core.ouroboros.cli.ov import AttachUI

        roster, _clock = busy
        ui = AttachUI()
        ui.on_telemetry({"kind": "heartbeat", "active": True,
                         "agents": roster.snapshot()})
        assert ui._agent_lines() == []
        set_roster_visible(True)
        assert ui._agent_lines(), "shown, the same frame must draw"

    def test_the_renderer_holds_no_opinion_about_visibility(self, busy):
        """The gate belongs to the PROVIDERS.

        Putting it in `render_roster` made every caller that wanted the
        picture — `/tasks` included — opt out of a mode question it had not
        asked, and broke seventeen tests that were only ever about folding,
        width and glyphs.
        """
        roster, _clock = busy
        assert roster_visible() is False
        assert render_roster(roster.snapshot(), width=100), (
            "the renderer draws what it is given; the surface decides "
            "whether to ask"
        )

    def test_every_provider_is_gated(self):
        """A PRODUCER-side audit, not a consumer-side one.

        Two of three providers gated is the shape of defect this repo keeps
        finding late: the surface an operator does not happen to open that
        week keeps drawing rows the others have stopped drawing, and nothing
        fails. So the assertion is over the SET of providers, and a fourth
        one added tomorrow lands in this list or fails here.
        """
        import ast
        import inspect

        from backend.core.ouroboros.battle_test import serpent_flow
        from backend.core.ouroboros.cli import ov

        providers = (
            serpent_flow._local_agent_rows,
            ov.AttachUI._agent_lines,
        )
        for fn in providers:
            tree = ast.parse(inspect.getsource(fn).lstrip())
            called = {
                node.func.id for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
            }
            assert "roster_visible" in called, (
                f"{fn.__qualname__} draws roster rows without consulting "
                "roster_visible — it will keep spending rows below the caret "
                "after /tasks hides them everywhere else"
            )

    def test_the_demo_opts_in_explicitly(self):
        """The demo exists to SHOW the surfaces, so it turns the roster on —
        and has to say so in code rather than inherit a default, or it would
        silently keep rendering a roster the cockpit had stopped drawing."""
        from backend.core.ouroboros.cli import ov_demo

        assert roster_visible() is False
        ov_demo._agent_view_rows()
        assert roster_visible() is True
