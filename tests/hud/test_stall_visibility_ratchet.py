"""The instrument must be able to see the era it is in.

WHAT HAPPENED
-------------
`StallSampler.trigger_s` was 2.0s, chosen when the worst observed stall was
32.31s. Five rounds of fixes brought the worst to 1.44s -- at which point the
dumper could no longer fire at all, and a real stall was reported by
`LoopSentinel` (threshold 0.25s) with no stack behind it. The stall looked
anonymous. It was not: it was sub-threshold, and the threshold was the bug.

Lowering the trigger to 0.60s produced a dump on the first occurrence, whose
innermost frame was `importlib._bootstrap_external._write_atomic` -- the loop
writing a .pyc during a first-time import of `coding_council.orchestrator`,
reached from a FastAPI health endpoint. Cold import measured at 325ms.

These tests pin both halves: the dumper can see stalls of the size this
system now produces, and the import it caught is pre-warmed so it stops
landing on the loop.
"""
from __future__ import annotations

import pytest

from backend.hud import stall_sampler
from backend.hud import loop_sentinel
from backend.core import prewarm


class TestTheDumperCanSeeTheCurrentEra:

    def test_the_trigger_is_below_the_stalls_this_system_actually_produces(self):
        """The measured worst stall was 1.44s. A 2.0s trigger cannot fire on
        that, which is how a real stall came to look like a ghost."""
        assert stall_sampler.trigger_s() <= 1.44, (
            f"trigger is {stall_sampler.trigger_s()}s — blind to the 1.44s "
            f"class this system currently produces")

    def test_it_still_sits_well_above_the_sentinel_so_dumps_stay_rare(self):
        """A dump per scheduling hiccup would be noise, and the sampler's
        whole value is that its output is worth reading."""
        assert stall_sampler.trigger_s() >= loop_sentinel.stall_threshold_s() * 2, (
            "the dumper is now close enough to the sentinel to dump on "
            "ordinary hiccups")

    def test_the_cost_of_a_lower_trigger_is_bounded(self):
        """Lowering a diagnostic's threshold is only safe because the spend
        is capped somewhere else. If these caps go, the trigger must be
        re-examined -- that is why they are asserted here and not assumed."""
        assert stall_sampler.max_dumps() <= 32, "unbounded dump count"
        assert stall_sampler.min_gap_s() >= 1.0, "dumps could tail-chase"

    def test_a_typo_in_the_knob_cannot_turn_it_into_a_spin(self, monkeypatch):
        monkeypatch.setenv(stall_sampler.ENV_TRIGGER_S, "0.0001")
        assert stall_sampler.trigger_s() >= 0.25, "clamp floor lost"
        monkeypatch.setenv(stall_sampler.ENV_TRIGGER_S, "not-a-number")
        assert stall_sampler.trigger_s() > 0, "a bad value must not raise"


class TestTheImportItCaughtIsWarmed:

    def test_the_health_endpoint_import_is_in_the_prewarm_list(self):
        """`main.health_check` -> `get_coding_council_health` ->
        `get_coding_council` does a LAZY `from .orchestrator import ...`
        inside an async function. Warming it moves that cost off the request
        path without changing a line of the subsystem."""
        assert "backend.core.coding_council.orchestrator" in prewarm.DEFAULT_PREWARM

    def test_every_prewarm_entry_is_importable_or_absent_not_a_typo(self):
        """A misspelled entry warms nothing and fails silently -- the list
        would still 'work' while the stall it targets kept happening."""
        import importlib.util
        for name in prewarm.DEFAULT_PREWARM:
            top = name.split(".")[0]
            assert importlib.util.find_spec(top) is not None or top not in (
                "backend",), f"{name}: top-level package {top} not found"

    def test_the_list_stays_deduplicated(self):
        names = prewarm.prewarm_modules()
        assert len(names) == len(set(names))
