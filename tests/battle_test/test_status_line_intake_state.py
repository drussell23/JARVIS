"""The status line must distinguish "nothing to do" from "not started yet".

A real boot, watched by the operator:

    22:18:20  session boots
    22:19:05  cockpit reads  IDLE · $0.00/$0.50      <- screenshot taken here
    22:20:32  TestFailureSensor enqueues 4 REAL failures
    22:20:44  Advisor runs, Orchestrator reaches caution

For two minutes the cockpit said IDLE while sensors were arming and then
while four genuine test failures sat queued. The operator reasonably
concluded the organism was broken. It was not — the status line simply had
no handle on the intake layer, so every moment before the first FSM context
existed rendered as the same word used for an organism with nothing to do.

These tests pin the distinction, and the lazy resolution that makes it work.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.core.ouroboros.battle_test.status_line import (  # noqa: E402
    StatusLineBuilder,
)


class _Router:
    def __init__(self, depth: int) -> None:
        self._depth = depth

    def intake_queue_depth(self) -> int:
        return self._depth


class _Intake:
    """Shaped like the real IntakeLayerService for the two attributes read."""

    def __init__(self, sensors: int, queued: int) -> None:
        self._sensors = list(range(sensors))
        self._router = _Router(queued)


def _builder(intake) -> StatusLineBuilder:
    # No governed loop service: the orchestrator has no live FSM context,
    # which is exactly the window this change is about.
    return StatusLineBuilder(governed_loop_service=None, intake_service=intake)


# ---------------------------------------------------------------------------
# the state that was invisible
# ---------------------------------------------------------------------------

def test_queued_signals_are_not_reported_as_idle() -> None:
    """THE regression. Four enqueued failures read as IDLE for two minutes."""
    phase, detail = _builder(_Intake(sensors=17, queued=4))._sample_phase_and_detail()
    assert phase == "QUEUED", f"work was waiting and the line said {phase!r}"
    assert "4" in detail


def test_a_single_queued_signal_is_not_pluralised() -> None:
    phase, detail = _builder(_Intake(sensors=17, queued=1))._sample_phase_and_detail()
    assert (phase, detail) == ("QUEUED", "1 signal")


def test_idle_still_says_idle_but_shows_its_evidence() -> None:
    """The word was never wrong. What was missing was the count beside it —
    the difference between "quiet" and "nothing is watching"."""
    phase, detail = _builder(_Intake(sensors=17, queued=0))._sample_phase_and_detail()
    assert phase == "IDLE"
    assert "17 sensors" == detail


def test_no_sensors_yet_is_arming_not_idle() -> None:
    """A sensor that has not registered cannot find anything. Reporting that
    as idle is the same failure, one step earlier in the boot."""
    phase, _ = _builder(_Intake(sensors=0, queued=0))._sample_phase_and_detail()
    assert phase == "ARMING"


# ---------------------------------------------------------------------------
# the wiring that makes it work at all
# ---------------------------------------------------------------------------

def test_intake_is_resolved_lazily_through_a_callable() -> None:
    """The builder is constructed hundreds of lines before intake exists.

    Passing the attribute eagerly captures None forever, and the status line
    then reports ARMING for the life of the process — a more confident lie
    than the IDLE it replaces.
    """
    box = {"intake": None}
    builder = _builder(lambda: box["intake"])

    assert builder._sample_phase_and_detail()[0] == "IDLE", (
        "with nothing resolvable yet it must fall back, not invent a state"
    )

    box["intake"] = _Intake(sensors=17, queued=3)
    phase, detail = builder._sample_phase_and_detail()
    assert (phase, detail) == ("QUEUED", "3 signals"), (
        "the callable was resolved once and cached — it must be sampled fresh"
    )


def test_the_harness_passes_a_callable_not_the_attribute() -> None:
    """Structural, because the eager version fails silently and forever.

    `_intake_service` is assigned well after `StatusLineBuilder(...)` is
    constructed. A test that only exercised the builder would pass while the
    shipped wiring captured None.
    """
    import inspect
    from backend.core.ouroboros.battle_test import harness

    src = inspect.getsource(harness)
    idx = src.index("_status_builder = StatusLineBuilder(")
    window = src[idx:idx + 700]
    assert "intake_service=" in window, "the builder is not given intake at all"
    assert "lambda" in window, (
        "intake_service was passed eagerly; it must be a callable resolved "
        "at sample time"
    )


# ---------------------------------------------------------------------------
# it must never be the reason a cockpit breaks
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "intake",
    [
        None,
        object(),                       # no _sensors, no _router
        _Intake(sensors=3, queued=0),
    ],
    ids=["absent", "wrong-shape", "healthy"],
)
def test_it_never_raises(intake) -> None:
    phase, detail = _builder(intake)._sample_phase_and_detail()
    assert isinstance(phase, str) and isinstance(detail, str)


def test_a_router_that_raises_degrades_to_sensor_count() -> None:
    """A queue that cannot be read is not a queue that is empty — but it is
    also not a reason to stop rendering. Report what IS known."""
    class _Angry:
        def intake_queue_depth(self):
            raise RuntimeError("queue is mid-swap")

    intake = _Intake(sensors=17, queued=0)
    intake._router = _Angry()
    phase, detail = _builder(intake)._sample_phase_and_detail()
    assert (phase, detail) == ("IDLE", "17 sensors")


def test_a_live_fsm_phase_still_wins() -> None:
    """Intake is the state BELOW the orchestrator. Once an op exists, the op
    is the answer — this must not shadow GENERATE/VALIDATE/APPLY."""
    from datetime import datetime, timezone

    class _Ctx:
        phase = "GENERATE"
        phase_entered_at = datetime.now(tz=timezone.utc)

    class _GLS:
        _fsm_contexts = {"op-1": _Ctx()}

    b = StatusLineBuilder(
        governed_loop_service=_GLS(),
        intake_service=_Intake(sensors=17, queued=9),
    )
    assert b._sample_phase_and_detail()[0] == "GENERATE"
