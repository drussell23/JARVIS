"""The pytest wall, derived from the op's envelope instead of a literal.

Every scenario here is a replay of session ``bt-2026-09-08-193049``, op
``op-01a08280-f4f2``: a 1530 s ceiling, one target file, three pytest runs
killed at 120 s / 120 s / 109 s, and a candidate that was never judged.
"""
from __future__ import annotations

import inspect

import pytest

from backend.core.ouroboros.governance import test_timeout_derivation as TT
from backend.core.ouroboros.governance.test_timeout_derivation import (
    derive_test_timeouts,
    ladder_depth,
    observe_per_file_rate,
    observe_shard_cost,
    shard_bucket,
)

#: The op's real numbers.
LIVE_BUDGET_S = 1400.0
LIVE_SHARD = 1
LEGACY_CAP_S = 120.0
LIVE_OP_DURATION_S = 1447.75


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in (
        TT.ENV_MAX_VALIDATE_RETRIES, TT.ENV_LEGACY_TIMEOUT,
        TT.ENV_PER_TEST_FRACTION, TT.ENV_SAFETY,
        TT.ENV_MAX_BUDGET_FRACTION, TT.ENV_BUCKET_CAP,
    ):
        monkeypatch.delenv(name, raising=False)
    TT.reset_for_tests()
    yield
    TT.reset_for_tests()


# --------------------------------------------------------------------------
# The defect
# --------------------------------------------------------------------------

def test_the_live_op_would_no_longer_be_capped_at_120s():
    """THE regression. A 1400s budget must not produce a 120s wall."""
    plan = derive_test_timeouts(budget_s=LIVE_BUDGET_S, shard_size=LIVE_SHARD)
    assert plan.invocation_s > LEGACY_CAP_S, (
        f"still pinned at the legacy cap: {plan.render()}"
    )


def test_the_adapter_default_no_longer_carries_a_static_ceiling():
    """The exact line that caused it: ``timeout: float = 120.0`` as a
    parameter default no production caller ever overrides."""
    from backend.core.ouroboros.governance.test_runner import PythonAdapter

    sig = inspect.signature(PythonAdapter.__init__)
    assert sig.parameters["timeout"].default is None, (
        "PythonAdapter still defaults to a static timeout"
    )


def test_the_adapter_derives_rather_than_clamping_the_budget():
    """Structural: ``min(timeout_budget_s, self._timeout)`` is the defect
    itself — it admits the live budget and then discards it."""
    from backend.core.ouroboros.governance.test_runner import PythonAdapter

    src = inspect.getsource(PythonAdapter.run)
    assert "min(timeout_budget_s" not in src, "the budget is still clamped away"
    assert "derive_test_timeouts" in src


# --------------------------------------------------------------------------
# The budget is authority
# --------------------------------------------------------------------------

def test_never_exceeds_the_budget():
    for budget in (10.0, 61.0, 119.0, 300.0, 5000.0):
        plan = derive_test_timeouts(budget_s=budget, shard_size=2)
        assert plan.invocation_s <= budget or budget < TT._ABSOLUTE_FLOOR_S, plan


def test_a_spent_budget_yields_the_floor_not_the_longest_timeout():
    """Found by this module's own smoke test, not by a soak: every ceiling is
    a FRACTION of the budget, so budget=0 disabled all of them at once and a
    warm estimator turned the exhausted case into the LONGEST wall of any
    input (1260s)."""
    for _ in range(8):
        observe_shard_cost(3, 210.0)
        observe_per_file_rate(3, 210.0)
    plan = derive_test_timeouts(budget_s=0.0, shard_size=3)
    assert plan.basis == "budget_exhausted"
    assert plan.invocation_s == TT._ABSOLUTE_FLOOR_S, plan.render()


def test_the_whole_retry_ladder_fits_the_budget(monkeypatch):
    """The ladder collapse this closes: three invocations at 120s inside a
    1530s ceiling still exhausted the retries, because each one was killed
    rather than answered. A share the ladder cannot afford is the same bug
    with a bigger number."""
    monkeypatch.setenv(TT.ENV_MAX_VALIDATE_RETRIES, "2")
    plan = derive_test_timeouts(budget_s=LIVE_BUDGET_S, shard_size=1)
    assert plan.ladder_depth == 3
    assert plan.invocation_s * plan.ladder_depth <= LIVE_BUDGET_S * 1.01


def test_the_share_tracks_the_FSM_s_own_knob(monkeypatch):
    """Read, never mirrored: raise the retries and every slice narrows with
    no second place to remember."""
    monkeypatch.setenv(TT.ENV_MAX_VALIDATE_RETRIES, "2")
    assert ladder_depth() == 3
    shallow = derive_test_timeouts(budget_s=LIVE_BUDGET_S, shard_size=1)
    monkeypatch.setenv(TT.ENV_MAX_VALIDATE_RETRIES, "9")
    assert ladder_depth() == 10
    deep = derive_test_timeouts(budget_s=LIVE_BUDGET_S, shard_size=1)
    assert deep.invocation_s < shallow.invocation_s


# --------------------------------------------------------------------------
# The floor is a floor, never a ceiling
# --------------------------------------------------------------------------

def test_never_below_the_legacy_value_when_the_budget_allows():
    """Strictly monotone-improving: this may only ever hand pytest MORE wall
    than the constant did."""
    for budget in (400.0, 900.0, 1400.0, 3000.0):
        plan = derive_test_timeouts(budget_s=budget, shard_size=1)
        assert plan.invocation_s >= LEGACY_CAP_S, plan.render()


def test_a_budget_smaller_than_the_floor_is_still_bounded_by_the_budget():
    plan = derive_test_timeouts(budget_s=40.0, shard_size=1)
    assert plan.invocation_s == 40.0


# --------------------------------------------------------------------------
# Learned, and shard-proportional
# --------------------------------------------------------------------------

def test_cold_start_moves_UP_not_back_to_the_constant():
    """Deliberately unlike every other derivator here. With no history there
    is no evidence for 120s — that value IS the defect — and the budget is the
    one real quantity in hand."""
    plan = derive_test_timeouts(budget_s=LIVE_BUDGET_S, shard_size=1)
    assert plan.basis == "cold_start"
    assert plan.invocation_s > LEGACY_CAP_S


def test_observation_moves_the_estimate():
    cold = derive_test_timeouts(budget_s=LIVE_BUDGET_S, shard_size=1)
    for _ in range(10):
        observe_shard_cost(1, 300.0)
    warm = derive_test_timeouts(budget_s=LIVE_BUDGET_S, shard_size=1)
    assert warm.basis == "observed_bucket"
    assert warm.projected_s > 0.0
    assert warm.invocation_s != cold.invocation_s or warm.projected_s > 0


def test_monotone_in_shard_size():
    """A bigger shard never yields a SHORTER wall."""
    for _ in range(10):
        observe_per_file_rate(1, 30.0)
    walls = [
        derive_test_timeouts(budget_s=100_000.0, shard_size=n).invocation_s
        for n in (1, 2, 4, 8, 16)
    ]
    assert walls == sorted(walls), walls


def test_monotone_in_observed_cost():
    """A slower tree never yields a SHORTER wall."""
    prev = 0.0
    for cost in (10.0, 50.0, 200.0, 600.0):
        TT.reset_for_tests()
        for _ in range(12):
            observe_shard_cost(1, cost)
        wall = derive_test_timeouts(budget_s=100_000.0, shard_size=1).invocation_s
        assert wall >= prev, (cost, wall, prev)
        prev = wall


def test_a_timed_out_run_is_never_observed():
    """A self-confirming ceiling: a timed-out run's duration IS the cap we
    chose, so feeding it back teaches the estimator that the cap was right."""
    from backend.core.ouroboros.governance.test_runner import PythonAdapter

    src = inspect.getsource(PythonAdapter.run)
    assert "if not test_result.timed_out:" in src, (
        "timed-out runs are being fed back into the estimator"
    )


def test_shard_buckets_are_memory_bounded(monkeypatch):
    """The EWMA is 'memory-bounded by the size of the route vocabulary', so
    the vocabulary has to stay small."""
    monkeypatch.setenv(TT.ENV_BUCKET_CAP, "32")
    keys = {shard_bucket(n) for n in range(0, 5000)}
    assert len(keys) <= 8, sorted(keys)


# --------------------------------------------------------------------------
# The two caps cannot drift
# --------------------------------------------------------------------------

def test_per_test_cap_is_always_strictly_below_the_wall():
    """A per-test cap at or above the wall means pytest-timeout can never fire
    first, so a hung test becomes an unattributable infra timeout instead of a
    named failing test — and only a named failure is trainable."""
    for budget in (10.0, 130.0, 1400.0, 9000.0):
        plan = derive_test_timeouts(budget_s=budget, shard_size=3)
        assert 1 <= plan.per_test_s < plan.invocation_s, plan.render()


def test_the_per_test_cap_follows_the_derived_wall_not_the_import_time_constant():
    """It was ``0.25 * 120 = 30`` computed at import. Raising the wall alone
    would have left it pinned there and merely moved the timeout."""
    from backend.core.ouroboros.governance.test_runner import TestRunner

    src = inspect.getsource(TestRunner._run_pytest)
    assert "_effective_per_test_timeout_s()" in src
    assert "--timeout=\" + str(_TEST_PER_TEST_TIMEOUT_S)" not in src


def test_the_runner_clamps_per_test_below_its_own_wall(tmp_path):
    from backend.core.ouroboros.governance.test_runner import TestRunner

    runner = TestRunner(repo_root=tmp_path, timeout=50.0, per_test_timeout_s=9999)
    assert runner._effective_per_test_timeout_s() < 50


def test_a_legacy_runner_is_byte_identical(tmp_path):
    from backend.core.ouroboros.governance import test_runner as TR
    from backend.core.ouroboros.governance.test_runner import TestRunner

    runner = TestRunner(repo_root=tmp_path)
    assert runner._effective_per_test_timeout_s() == TR._TEST_PER_TEST_TIMEOUT_S


def test_a_TINY_legacy_wall_must_still_be_killed_by_the_WALL(tmp_path):
    """The regression the existing suite caught, not this one.

    An earlier version clamped the per-test cap to ``wall - 1`` on every path,
    so ``TestRunner(repo_root, timeout=2.0)`` ran pytest with ``--timeout=1``:
    pytest-timeout killed the test at 1s and the run RETURNED a report instead
    of the subprocess being killed at 2s. A 2s wall is the harness handing out
    impossible time — INFRASTRUCTURE — and relabelling it as a named test
    failure is exactly what ``failure_class=infra`` exists to prevent.
    """
    from backend.core.ouroboros.governance import test_runner as TR
    from backend.core.ouroboros.governance.test_runner import TestRunner

    runner = TestRunner(repo_root=tmp_path, timeout=2.0)
    assert runner._effective_per_test_timeout_s() == TR._TEST_PER_TEST_TIMEOUT_S
    assert runner._effective_per_test_timeout_s() > 2.0, (
        "pytest-timeout would preempt the wall and relabel infra as a test failure"
    )


# --------------------------------------------------------------------------
# Total
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "budget,shard",
    [(None, None), ("x", "y"), (float("nan"), -3), (-1.0, 0), (object(), object())],
)
def test_never_raises_on_hostile_input(budget, shard):
    plan = derive_test_timeouts(budget_s=budget, shard_size=shard)
    assert plan.invocation_s >= TT._ABSOLUTE_FLOOR_S
    assert plan.per_test_s >= 1


def test_the_degraded_path_is_itself_total():
    """Caught by this module's own smoke test: the recovery path rebuilt the
    plan with bare ``int(shard_size)`` and raised OUT of the very handler whose
    contract is that it never does."""
    plan = TT._degraded_plan("garbage", "garbage")
    assert plan.basis == "degraded"


def test_an_operator_pin_is_still_honoured():
    """The parameter survives so a DELIBERATE cap still works — a deliberate
    pin is a different thing from a forgotten default."""
    plan = derive_test_timeouts(
        budget_s=LIVE_BUDGET_S, shard_size=1, operator_ceiling_s=90.0,
    )
    assert plan.invocation_s == 90.0
    assert "operator_cap" in plan.basis


def test_the_live_op_would_have_had_room_to_finish():
    """The whole point: op-01a08280-f4f2 ran 1447.75s and never got a verdict.
    Three invocations of the derived wall must cover the work the ladder
    actually attempted."""
    plan = derive_test_timeouts(budget_s=LIVE_BUDGET_S, shard_size=1)
    assert plan.invocation_s * plan.ladder_depth > LIVE_OP_DURATION_S * 0.9
