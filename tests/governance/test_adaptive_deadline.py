"""An op's deadline is measured, not multiplied.

`pipeline * 1.2` is wrong in both directions and no constant is right: too
small sheds ops that were about to land, too large lets a wedged one hold the
loop for the session. The multiplier cannot know what the machine is doing,
which is the only thing that decides how long an op takes.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.autonomy.adaptive_deadline import (
    compute_outcome_deadline,
    observe_generation_duration,
    observe_op_duration,
    reset_for_tests,
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for var in (
        "JARVIS_ADAPTIVE_DEADLINE_COLD_MULTIPLIER",
        "JARVIS_ADAPTIVE_DEADLINE_SAFETY",
        "JARVIS_ADAPTIVE_DEADLINE_QUEUE_COEFF",
        "JARVIS_ADAPTIVE_DEADLINE_MAX_QUEUE_FACTOR",
    ):
        monkeypatch.delenv(var, raising=False)
    reset_for_tests()
    yield
    reset_for_tests()


def _d(**kw):
    kw.setdefault("pipeline_budget_s", 100.0)
    kw.setdefault("wall_ceiling_s", 10_000.0)
    return compute_outcome_deadline(**kw)


# --------------------------------------------------------------------------
# Cold start must reproduce the behaviour it replaces
# --------------------------------------------------------------------------

def test_cold_start_is_the_legacy_multiplier():
    """Arming the controller changes nothing until it has evidence."""
    est = _d()
    assert est.basis == "cold_start"
    assert est.seconds == pytest.approx(120.0)      # 100 * 1.2


def test_the_cold_multiplier_is_configurable(monkeypatch):
    monkeypatch.setenv("JARVIS_ADAPTIVE_DEADLINE_COLD_MULTIPLIER", "1.5")
    assert _d().seconds == pytest.approx(150.0)


# --------------------------------------------------------------------------
# It learns
# --------------------------------------------------------------------------

def test_observed_op_duration_drives_the_deadline():
    observe_op_duration("sentinel", 400.0)
    est = _d(route="sentinel")
    assert est.basis == "observed_op_ewma"
    assert est.observed_op_s > 0
    # A mean is a CENTRE — half of ops exceed it, so waiting exactly the mean
    # sheds half of them. The safety factor is why this is above it.
    assert est.seconds > est.observed_op_s


def test_a_slower_lane_gets_a_longer_deadline():
    observe_op_duration("fast", 50.0)
    observe_op_duration("slow", 500.0)
    assert _d(route="slow").seconds > _d(route="fast").seconds


def test_generation_latency_alone_still_informs():
    """Before any op completes, the two dominant phases are still measured."""
    observe_generation_duration("sentinel", 300.0)
    est = _d(route="sentinel")
    assert est.basis == "phase_sum"
    assert est.seconds > 120.0


def test_a_failed_op_still_teaches():
    """Learning only from successes biases every future deadline short."""
    observe_op_duration("sentinel", 600.0)
    assert _d(route="sentinel").basis == "observed_op_ewma"


# --------------------------------------------------------------------------
# Queue depth
# --------------------------------------------------------------------------

def test_a_deeper_queue_extends_the_deadline():
    observe_op_duration("sentinel", 200.0)
    shallow = _d(route="sentinel", queue_depth=1).seconds
    deep = _d(route="sentinel", queue_depth=5).seconds
    assert deep > shallow


def test_queue_scaling_is_bounded_not_multiplied():
    """Concurrent ops overlap; they do not serialise. A queue of 50 must not
    ask for 50x the time."""
    observe_op_duration("sentinel", 200.0)
    est = _d(route="sentinel", queue_depth=50)
    assert est.queue_factor <= 3.0


# --------------------------------------------------------------------------
# The envelope — it can choose within, never beyond
# --------------------------------------------------------------------------

def test_the_deadline_never_falls_below_the_pipeline_budget():
    """Less than the pipeline would shed ops the pipeline considers live."""
    observe_op_duration("sentinel", 1.0)
    est = _d(route="sentinel", pipeline_budget_s=500.0)
    assert est.seconds >= 500.0


def test_the_deadline_never_exceeds_the_session_wall():
    observe_op_duration("sentinel", 100_000.0)
    est = _d(route="sentinel", pipeline_budget_s=100.0, wall_ceiling_s=300.0)
    assert est.seconds <= 300.0


def test_monotonic_in_cost():
    """More history never produces a SHORTER deadline."""
    observe_op_duration("sentinel", 100.0)
    first = _d(route="sentinel").seconds
    observe_op_duration("sentinel", 900.0)
    assert _d(route="sentinel").seconds >= first


# --------------------------------------------------------------------------
# Resilience + explainability
# --------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [0.0, -5.0, None])
def test_a_junk_observation_is_ignored_not_fatal(bad):
    observe_op_duration("sentinel", bad)
    assert _d(route="sentinel").basis == "cold_start"


def test_a_zero_pipeline_budget_still_yields_a_deadline():
    est = _d(pipeline_budget_s=0.0, wall_ceiling_s=0.0)
    assert est.seconds > 0


def test_the_estimate_explains_itself():
    """A shed op must be explainable without re-deriving the arithmetic."""
    observe_op_duration("sentinel", 250.0)
    text = _d(route="sentinel", queue_depth=3).render()
    for token in ("deadline=", "basis=", "op_ewma=", "queue=", "bounds="):
        assert token in text


def test_the_loop_delegates_rather_than_multiplying():
    import inspect

    from backend.core.ouroboros.governance.autonomy import sentinel_loop

    src = inspect.getsource(sentinel_loop.SentinelLoop._outcome_deadline_s)
    assert "compute_outcome_deadline" in src, "the loop still owns a multiplier"
