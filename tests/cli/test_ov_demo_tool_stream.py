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
        operator that elision is loss.

        Pins the AFFORDANCE, not the mechanism. It used to assert the
        mid-body `… elided …` marker, which stopped appearing once
        signal-selection denoised the pytest body under budget — the body
        got shorter and more useful, and the test read that as a regression.
        What has to be true is that the operator is TOLD rows were withheld
        and HOW to get them.
        """
        text = "\n".join(_lines(ov_demo.compose_live_script()))
        assert "more lines" in text, "no truncation was disclosed at all"
        assert "/expand t-" in text, (
            "rows were withheld with no way back — a `None` store still "
            "renders but issues no ref, which is the affordance being shown"
        )
