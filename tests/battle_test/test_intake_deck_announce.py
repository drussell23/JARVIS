"""Intake gets a voice on the deck: what was FOUND, before an op exists.

The deck renders ops, and an op is the last thing to happen — a sensor fires,
a signal is enqueued, and only later does an FSM context exist. On a real boot
four genuine test failures sat queued for two minutes behind a blank deck,
indistinguishable from an organism with nothing to do.

These pin the two halves that make the line trustworthy: intake announces only
what it actually queued, and the renderer says WHICH failure rather than that
one exists.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.core.ouroboros.governance.intake.unified_intake_router import (  # noqa: E402
    _announce_enqueued,
    set_intake_announce_sink,
)


class _Envelope:
    def __init__(self, **kw) -> None:
        self.source = kw.get("source", "test_failure")
        self.description = kw.get("description", "")
        self.target_files = kw.get("target_files", ())
        self.urgency = kw.get("urgency", "normal")
        self.signal_id = kw.get("signal_id", "sig-1")


@pytest.fixture(autouse=True)
def _detach_sink():
    yield
    set_intake_announce_sink(None)   # never leak a sink between tests


# ---------------------------------------------------------------------------
# the announcement
# ---------------------------------------------------------------------------

def test_the_sink_receives_what_was_queued() -> None:
    seen = []
    set_intake_announce_sink(seen.append)
    _announce_enqueued(_Envelope(
        source="test_failure",
        target_files=("tests/governance/test_topological_sort_tiebreak.py",),
    ))
    assert len(seen) == 1
    assert seen[0]["source"] == "test_failure"
    assert seen[0]["target_files"][0].endswith("test_topological_sort_tiebreak.py")


def test_no_sink_is_not_an_error() -> None:
    """The overwhelmingly common case — headless, CI, no cockpit attached."""
    set_intake_announce_sink(None)
    _announce_enqueued(_Envelope())          # must simply do nothing


def test_a_raising_sink_cannot_reach_intake() -> None:
    """Zero authority means a broken cockpit cannot disturb dispatch."""
    def _explode(_payload):
        raise RuntimeError("the deck is on fire")

    set_intake_announce_sink(_explode)
    _announce_enqueued(_Envelope())          # must swallow


def test_a_malformed_envelope_cannot_reach_intake() -> None:
    class _Hostile:
        @property
        def target_files(self):
            raise RuntimeError("evidence mid-write")

    seen = []
    set_intake_announce_sink(seen.append)
    _announce_enqueued(_Hostile())
    assert seen == [], "a broken envelope must not produce a half-built line"


def test_announcement_follows_the_put_it_does_not_predict_it() -> None:
    """Structural: the line claims a signal was QUEUED, so it must be emitted
    after the queue accepted it. Announcing first would render a signal that a
    raising put never actually enqueued."""
    import inspect
    from backend.core.ouroboros.governance.intake import unified_intake_router

    src = inspect.getsource(unified_intake_router)
    put_at = src.index("await self._queue.put(\n            (priority, envelope.submitted_at")
    announce_at = src.index("_announce_enqueued(envelope)", put_at)
    assert announce_at > put_at


# ---------------------------------------------------------------------------
# the line itself
# ---------------------------------------------------------------------------

class _Flow:
    """Only what note_intake_signal touches."""

    def __init__(self) -> None:
        self.mirrored = []

    def _mirror_markup(self, line: str) -> None:
        self.mirrored.append(line)

    def _borderless(self) -> bool:
        return False

    @staticmethod
    def _action_glyph() -> str:
        return "⏺"


def _render(**payload) -> str:
    from backend.core.ouroboros.battle_test.serpent_flow import SerpentFlow
    flow = _Flow()
    SerpentFlow.note_intake_signal(flow, payload)   # type: ignore[arg-type]
    assert flow.mirrored, "nothing was rendered"
    return flow.mirrored[-1]


def test_the_argument_is_the_point() -> None:
    """`⏺ TestFailure()` says a sensor fired. `⏺ TestFailure(<file>)` says
    which one — the distinction this file's own CC-style comment insists on."""
    line = _render(
        source="test_failure",
        target_files=("tests/governance/test_topological_sort_tiebreak.py",),
    )
    assert "TestFailure" in line
    assert "test_topological_sort_tiebreak.py" in line
    assert "tests/governance/" not in line, "the path should be trimmed to a name"
    assert "queued" in line


def test_extra_targets_are_counted_not_listed() -> None:
    line = _render(
        source="doc_staleness",
        target_files=("a/one.py", "b/two.py", "c/three.py"),
    )
    assert "one.py +2" in line
    assert "two.py" not in line


def test_the_verb_is_derived_from_the_source() -> None:
    """No lookup table: a sensor added tomorrow renders correctly, and a stale
    map cannot leak raw snake_case to an operator."""
    assert "OpportunityMiner" in _render(source="opportunity_miner",
                                         target_files=("x/y.py",))
    assert "CrossRepoDrift" in _render(source="cross_repo_drift",
                                       target_files=("x/y.py",))


def test_a_targetless_signal_falls_back_to_its_description() -> None:
    """A goal or a scheduled sweep has no file. That is a real state, not a
    reason to draw an empty pair of brackets."""
    line = _render(source="scheduled", target_files=(),
                   description="nightly dependency audit")
    assert "nightly dependency audit" in line
    assert "()" not in line


def test_a_long_description_is_truncated() -> None:
    line = _render(source="backlog", target_files=(),
                   description="x" * 200)
    assert "…" in line
    assert len(line) < 300


def test_an_unknown_source_still_renders() -> None:
    line = _render(source="", target_files=("x/y.py",))
    assert "Signal" in line


def test_rendering_never_raises() -> None:
    from backend.core.ouroboros.battle_test.serpent_flow import SerpentFlow

    class _Broken:
        def _mirror_markup(self, line):
            raise RuntimeError("mirror down")

        def _borderless(self):
            return False

        @staticmethod
        def _action_glyph():
            return "⏺"

    SerpentFlow.note_intake_signal(_Broken(), {"source": "x"})  # type: ignore[arg-type]


def test_the_harness_wires_the_sink_to_the_flow() -> None:
    """Structural. An unwired sink is the wired-but-inert trap: every unit test
    here passes while the deck stays blank in production."""
    import inspect
    from backend.core.ouroboros.battle_test import harness

    src = inspect.getsource(harness)
    assert "set_intake_announce_sink" in src
    idx = src.index("set_intake_announce_sink")
    assert "note_intake_signal" in src[idx:idx + 200], (
        "the sink is imported but not pointed at the renderer"
    )
