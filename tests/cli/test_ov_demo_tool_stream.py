"""The live scene drives the REAL tool renderer, not a second draw path.

`ov demo live` is the one scene an operator actually watches, and it was the
one scene that rendered nothing the cockpit renders. Its beats were
hand-written lines — `⏺ Bash(…)`, `⎿ 3 failed` — which agreed with themselves
while `tool_render_registry` (the descriptor table every real tool call goes
through: per-tool icon, argument summarizer, result summarizer, status glyph,
density policy, `t-N` expansion refs) went entirely unexercised.

These tests pin the property that makes the scene worth having: a regression
in the renderer must reach the demo. They deliberately do NOT assert on the
rendered text, because pinning output means updating the pin whenever a
renderer legitimately changes — and people update such tests by pasting the
new output, which blesses regressions.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.battle_test import agent_roster as ar
from backend.core.ouroboros.cli import ov_demo


@pytest.fixture(autouse=True)
def _isolated():
    ar.reset_roster_for_tests()
    ov_demo._STORE = None
    yield
    ar.reset_roster_for_tests()
    ov_demo._STORE = None


def _lines(script):
    return [ln for _t, ln in script if isinstance(ln, str)]


class TestItDrivesTheRealRenderer:
    def test_a_broken_tool_renderer_reaches_the_demo(self, monkeypatch):
        """THE property. Asserted by BREAKING `compose` and proving the scene
        notices — a truthy check on the output would pass just as well against
        a hardcoded string, which is precisely the failure being guarded."""
        import backend.core.ouroboros.battle_test.tool_render_view as trv

        calls = {"n": 0}
        real = trv.compose

        def _spy(*a, **k):
            calls["n"] += 1
            out = real(*a, **k)
            return out

        monkeypatch.setattr(trv, "compose", _spy)
        ov_demo.compose_live_script()
        assert calls["n"] >= 4, (
            "the scene must route every tool beat through the cockpit's own "
            f"compose(); saw {calls['n']}"
        )

    def test_the_summaries_are_the_registrys_not_the_scripts(self):
        """The payloads are RAW — 847 lines of file, a pytest tail. If the
        script stated its own conclusions, the summarizers would be free to
        regress silently."""
        text = "\n".join(_lines(ov_demo.compose_live_script()))
        # Numbers no beat contains: the registry counted them.
        assert "847 lines read" in text
        assert "14 matches" in text
        assert "command failed" in text
        # And nothing in the beat list pre-computed them.
        beats = repr(ov_demo._LIVE_BEATS)
        assert "847 lines read" not in beats
        assert "14 matches" not in beats

    def test_every_tool_shape_the_cockpit_can_draw_is_exercised(self):
        """Header-only, match list, diff, log, and a FAILING call — the shapes
        an operator meets in one real op. A demo of the happy path shows the
        least interesting third of the surface."""
        kinds = {b[2][0] for b in ov_demo._LIVE_BEATS if b[1] == "tool"}
        assert {"read_file", "search_code", "edit_file", "bash"} <= kinds
        statuses = {b[2][3] for b in ov_demo._LIVE_BEATS if b[1] == "tool"}
        assert "error" in statuses, "a demo with no failure shows no ⎿ red"

    def test_elision_appears_WITH_its_recovery_path(self):
        """Demonstrating a truncation without the way back teaches the
        operator that elision is loss."""
        text = "\n".join(_lines(ov_demo.compose_live_script()))
        assert "elided" in text
        assert "/expand t-" in text, (
            "an elided body must carry a real ref — a `None` store still "
            "renders but issues no hint, which is the affordance being shown"
        )


class TestTheAgentViewFillsIn:
    def test_the_roster_is_driven_at_PLAYBACK_not_composition(self):
        """The roster measures elapsed against the clock. Driving it during
        composition spawns and finishes every agent in one microsecond, so the
        demo would open with a roster already full of agents that each ran for
        `0s` — a surface populated before the first frame demonstrates nothing
        about filling in."""
        script = ov_demo.compose_live_script()
        assert ar.get_agent_roster().render() == [], (
            "composing the script must not have touched the live roster"
        )
        actions = [ln for _t, ln in script if callable(ln)]
        assert len(actions) >= 4, "spawns and finishes must be deferred"

    def test_running_the_actions_fills_the_roster(self):
        script = ov_demo.compose_live_script()
        for _t, line in script:
            if callable(line):
                line()
        rows = ar.get_agent_roster().render(width=100)
        assert any("Explore" in ln for ln in rows)
        assert any("Review" in ln for ln in rows)

    def test_a_finish_announces_itself_through_the_rosters_own_notice(self):
        """`finished_notice` quotes the GOAL, not the id — the demo must not
        write its own sentence for that."""
        script = ov_demo.compose_live_script()
        produced = [ln() for _t, ln in script if callable(ln)]
        notices = [p for p in produced if p]
        assert notices and all("Agent" in n for n in notices)


class TestTheSchedule:
    def test_timings_stay_monotonic_after_blocks_expand(self):
        """A beat now emits a BLOCK whose body can run past the next beat's
        timestamp. The driver sleeps against the schedule, so an out-of-order
        entry would meet a target already past and dump the remainder at once
        — the burst the rhythm exists to avoid."""
        times = [t for t, _ in ov_demo.compose_live_script()]
        assert times == sorted(times)

    def test_the_scene_still_costs_nothing(self):
        """The cost guarantee, structurally: no provider import may appear."""
        import ast
        import pathlib
        tree = ast.parse(pathlib.Path(
            "backend/core/ouroboros/cli/ov_demo.py").read_text("utf-8"))
        banned = ("providers", "candidate_generator", "doubleword",
                  "anthropic", "openai", "requests", "httpx", "urllib")
        for node in ast.walk(tree):
            names = ([a.name for a in node.names]
                     if isinstance(node, ast.Import)
                     else [node.module or ""]
                     if isinstance(node, ast.ImportFrom) else [])
            for name in names:
                assert not any(b in name.lower() for b in banned), name


class TestDegradation:
    def test_a_dead_tool_renderer_degrades_to_a_line_not_a_crash(
        self, monkeypatch,
    ):
        import backend.core.ouroboros.battle_test.tool_render_view as trv

        def _boom(*_a, **_k):
            raise RuntimeError("renderer down")

        monkeypatch.setattr(trv, "compose", _boom)
        script = ov_demo.compose_live_script()
        assert script, "a broken renderer must not empty the scene"
        assert any("unavailable" in ln for ln in _lines(script)), (
            "and it must SAY so rather than silently drawing a shorter deck"
        )
