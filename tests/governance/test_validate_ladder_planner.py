"""The ladder may not start what it cannot finish.

Replays session ``bt-2026-09-08-202025``, op ``op-01a08280-f4f5``::

    [ValidateRetryFSM] budget_exhausted_pre ... n_cands=3 remaining_s=-138.3

``remaining_s`` is NEGATIVE: the guard asked whether any time was left, never
whether there was enough, so a three-candidate iteration was started with far
too little budget and the overrun was discovered 138 s after the fact.
"""
from __future__ import annotations

import inspect

import pytest

from backend.core.ouroboros.governance import test_timeout_derivation as TT
from backend.core.ouroboros.governance import validate_ladder_planner as VLP
from backend.core.ouroboros.governance.validate_ladder_planner import (
    FULL,
    PRUNED,
    STOP,
    admitted_candidates,
    plan_iteration,
    projected_iteration_cost_s,
)

#: Measured on this host during that run.
PYTEST_COST_S = 350.0
CANDS = ("c0", "c1", "c2")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in (VLP.ENV_ENABLED, VLP.ENV_CONCURRENCY_K, VLP.ENV_SAFETY):
        monkeypatch.delenv(name, raising=False)
    TT.reset_for_tests()
    yield
    TT.reset_for_tests()


def _warm(cost_s=PYTEST_COST_S, shard=1):
    for _ in range(12):
        TT.observe_shard_cost(shard, cost_s)
        TT.observe_per_file_rate(shard, cost_s)


# --------------------------------------------------------------------------
# The defect
# --------------------------------------------------------------------------

def test_the_live_overrun_is_refused():
    """THE regression: 3 candidates, ~350s each, with far too little left."""
    _warm()
    plan = plan_iteration(remaining_s=50.0, candidates=CANDS)
    assert plan.should_stop, plan.render()
    assert plan.admit == 0


def test_the_runner_consults_the_planner_before_spending():
    from backend.core.ouroboros.governance.phase_runners import validate_runner

    src = inspect.getsource(validate_runner.VALIDATERunner.run)
    assert "_plan_ladder_iteration(" in src
    assert "_iter_candidates" in src
    assert "for c in _iter_candidates" in src, (
        "the gather still fans out over the FULL candidate set"
    )


def test_a_stop_preserves_what_earlier_iterations_established():
    """The old path returned best_candidate=None for three candidates that
    were never given the chance to be judged."""
    from backend.core.ouroboros.governance.phase_runners import validate_runner

    src = inspect.getsource(validate_runner.VALIDATERunner.run)
    stop = src.index("ladder_stop_return")
    tail = src[stop:stop + 400]
    assert '"best_candidate": best_candidate' in tail
    assert '"best_validation": best_validation' in tail


def test_the_stop_has_its_own_terminal_reason():
    """`validation_budget_insufficient` is a different fact from
    `validation_budget_exhausted` — refused before spending, not discovered
    after overrunning — and a soak has to be able to tell them apart."""
    from backend.core.ouroboros.governance.phase_runners import validate_runner

    src = inspect.getsource(validate_runner.VALIDATERunner.run)
    assert "validation_budget_insufficient" in src


# --------------------------------------------------------------------------
# The three outcomes
# --------------------------------------------------------------------------

def test_full_when_it_fits():
    _warm()
    plan = plan_iteration(remaining_s=10_000.0, candidates=CANDS)
    assert plan.mode == FULL and plan.admit == 3


def test_pruned_when_only_a_subset_fits():
    _warm()
    # One candidate ~437s with safety; three ~875s. 600s fits two, not three.
    plan = plan_iteration(remaining_s=600.0, candidates=CANDS)
    assert plan.mode == PRUNED, plan.render()
    assert 0 < plan.admit < 3


def test_stop_when_not_even_one_fits():
    _warm()
    plan = plan_iteration(remaining_s=100.0, candidates=CANDS)
    assert plan.mode == STOP and plan.admit == 0


def test_an_already_exhausted_budget_stops():
    _warm()
    assert plan_iteration(remaining_s=0.0, candidates=CANDS).mode == STOP
    assert plan_iteration(remaining_s=-138.3, candidates=CANDS).mode == STOP


# --------------------------------------------------------------------------
# Invariants
# --------------------------------------------------------------------------

def test_never_admits_more_than_offered():
    _warm()
    for n in range(1, 6):
        cands = tuple(f"c{i}" for i in range(n))
        plan = plan_iteration(remaining_s=10_000.0, candidates=cands)
        assert plan.admit <= n


def test_never_admits_an_iteration_it_projects_cannot_finish():
    _warm()
    for rem in (10.0, 100.0, 300.0, 600.0, 900.0, 5000.0):
        plan = plan_iteration(remaining_s=rem, candidates=CANDS)
        if plan.admit > 0:
            assert plan.projected_s <= rem, plan.render()


def test_monotone_in_remaining_time():
    _warm()
    admits = [
        plan_iteration(remaining_s=rem, candidates=CANDS).admit
        for rem in (50, 200, 450, 700, 1000, 5000)
    ]
    assert admits == sorted(admits), admits


def test_cold_start_admits_everything():
    """Arming this changes nothing until the estimator has measured a run."""
    plan = plan_iteration(remaining_s=1.0, candidates=CANDS)
    assert plan.mode == FULL and plan.admit == 3
    assert plan.basis == "cold_start"


def test_concurrency_is_scaled_not_multiplied():
    """N concurrent validations do not cost N times one, and do not cost one
    either — the same shape compute_validation_reserve uses."""
    _warm()
    one = projected_iteration_cost_s(1)
    three = projected_iteration_cost_s(3)
    assert one < three < 3 * one, (one, three)


def test_the_concurrency_knob_is_shared_with_the_reserve():
    assert VLP.ENV_CONCURRENCY_K == "JARVIS_VALIDATION_RESERVE_CONCURRENCY_K"


@pytest.mark.parametrize("k", ["0", "0.25", "1", "banana", "-3", "9"])
def test_the_coefficient_is_bounded(monkeypatch, k):
    monkeypatch.setenv(VLP.ENV_CONCURRENCY_K, k)
    assert 0.0 <= VLP.concurrency_coefficient() <= 1.0


# --------------------------------------------------------------------------
# Pruning removes SIBLINGS, never tests
# --------------------------------------------------------------------------

def test_pruning_keeps_generation_order():
    plan = VLP.LadderPlan(
        admit=2, mode=PRUNED, reason="", offered=3, remaining_s=600.0,
        projected_s=0.0, per_candidate_s=0.0, basis="observed_bucket",
    )
    assert list(admitted_candidates(CANDS, plan)) == ["c0", "c1"]


def test_pruning_never_admits_zero_tests_for_an_admitted_candidate():
    """Each admitted candidate faces the identical resolved test set: the
    planner selects siblings, and has no say over which tests run."""
    src = inspect.getsource(VLP)
    assert "test_files" not in src and "resolve_affected_tests" not in src


def test_a_none_plan_leaves_the_set_untouched():
    assert admitted_candidates(CANDS, None) == CANDS


# --------------------------------------------------------------------------
# Total
# --------------------------------------------------------------------------

def test_the_kill_switch_restores_the_old_behaviour(monkeypatch):
    _warm()
    monkeypatch.setenv(VLP.ENV_ENABLED, "false")
    plan = plan_iteration(remaining_s=1.0, candidates=CANDS)
    assert plan.mode == FULL and plan.admit == 3


@pytest.mark.parametrize(
    "rem,cands",
    [(None, None), ("x", "y"), (float("nan"), 3), (object(), object())],
)
def test_never_raises(rem, cands):
    plan = plan_iteration(remaining_s=rem, candidates=cands)
    assert plan.admit >= 0


def test_the_runner_helpers_are_fail_soft():
    from backend.core.ouroboros.governance.phase_runners import validate_runner

    assert validate_runner._plan_ladder_iteration(
        remaining_s=object(), candidates=object(), iteration=0,
    ) is not None or True  # must not raise
    assert validate_runner._admitted_candidates(CANDS, None) == CANDS


def test_the_live_ladder_is_the_dispatcher_path_not_the_orchestrator_copy():
    """Two copies of this loop exist. The dispatcher short-circuits
    `_run_pipeline` before the orchestrator's inline copy, so validate_runner
    is the reachable one — pinned here because a silent flip would make this
    fix unreachable, which is this repo's most expensive recurring failure."""
    from backend.core.ouroboros.governance import orchestrator

    src = inspect.getsource(orchestrator.GovernedOrchestrator._run_pipeline)
    gate = src.index("if _dispatcher_enabled():")
    ret = src.index("return await _dispatch_pipeline", gate)
    inline = src.index("for _iter_idx in range(1 + self._config.max_validate_retries)")
    assert ret < inline, "the orchestrator's inline ladder now runs first"
