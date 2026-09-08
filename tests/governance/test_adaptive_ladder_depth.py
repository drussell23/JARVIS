"""The retry ceiling is decided by the runtime, not by a constant.

``max_validate_retries`` was chosen before anyone had measured what a
validation iteration costs. At ~570 s per iteration against a ~890 s budget,
"3 iterations" was arithmetically impossible — and the ladder discovered that
at iteration 2, having already spent the budget getting there.
"""
from __future__ import annotations

import inspect

import pytest

from backend.core.ouroboros.governance import test_timeout_derivation as TT
from backend.core.ouroboros.governance import validate_ladder_planner as VLP
from backend.core.ouroboros.governance.validate_ladder_planner import negotiate_depth

BUDGET_S = 890.0        # the measured validation budget
CONFIGURED = 2          # JARVIS_MAX_VALIDATE_RETRIES default


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in (VLP.ENV_ENABLED, VLP.ENV_CONCURRENCY_K, VLP.ENV_SAFETY):
        monkeypatch.delenv(name, raising=False)
    TT.reset_for_tests()
    yield
    TT.reset_for_tests()


def _warm(cost_s):
    for _ in range(12):
        TT.observe_shard_cost(1, cost_s)
        TT.observe_per_file_rate(1, cost_s)


# --------------------------------------------------------------------------
# The defect
# --------------------------------------------------------------------------

def test_an_unaffordable_ladder_is_cut_down():
    """THE regression: 450s per candidate, 3 candidates, 890s budget."""
    _warm(450.0)
    n = negotiate_depth(
        remaining_s=BUDGET_S, configured_depth=CONFIGURED, n_candidates=3,
    )
    assert n.depth < n.configured, n.render()


def test_a_cheap_ladder_keeps_its_configured_depth():
    """Once targeted resolution drops the cost, the ladder gets its retries
    back with no constant edited anywhere."""
    _warm(2.0)
    n = negotiate_depth(
        remaining_s=BUDGET_S, configured_depth=CONFIGURED, n_candidates=3,
    )
    assert n.depth == CONFIGURED, n.render()


def test_negotiation_can_only_REDUCE_never_raise():
    """The configured value also encodes non-time limits — how often it is
    useful to re-ask a model that just failed — which this knows nothing
    about."""
    _warm(0.01)
    for budget in (100.0, 5_000.0, 1_000_000.0):
        n = negotiate_depth(
            remaining_s=budget, configured_depth=CONFIGURED, n_candidates=1,
        )
        assert n.depth <= CONFIGURED, n.render()


def test_monotone_in_budget():
    _warm(50.0)
    depths = [
        negotiate_depth(
            remaining_s=b, configured_depth=5, n_candidates=3,
        ).depth
        for b in (10, 100, 300, 600, 1200, 5000)
    ]
    assert depths == sorted(depths), depths


def test_monotone_in_cost():
    prev = 99
    for cost in (1.0, 20.0, 100.0, 400.0):
        TT.reset_for_tests()
        _warm(cost)
        d = negotiate_depth(
            remaining_s=BUDGET_S, configured_depth=5, n_candidates=3,
        ).depth
        assert d <= prev, (cost, d, prev)
        prev = d


def test_cold_start_keeps_the_configured_depth():
    n = negotiate_depth(
        remaining_s=BUDGET_S, configured_depth=CONFIGURED, n_candidates=3,
    )
    assert n.depth == CONFIGURED
    assert n.basis == "cold_start"


def test_no_budget_means_no_retries():
    _warm(50.0)
    assert negotiate_depth(
        remaining_s=0.0, configured_depth=CONFIGURED, n_candidates=3,
    ).depth == 0


def test_the_depth_is_a_RETRY_count_matching_the_loop_bound():
    """The loop runs `1 + depth` iterations, so N affordable iterations is
    N-1 retries. Off by one here silently restores the overrun."""
    _warm(100.0)
    n = negotiate_depth(remaining_s=350.0, configured_depth=9, n_candidates=1)
    # 350s / ~250s per iteration (100 * 1.25 safety * 2 flake) -> 1 iteration
    assert (1 + n.depth) * n.iteration_cost_s <= 350.0 * 1.05, n.render()


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------

def test_the_runner_uses_the_negotiated_depth_for_its_loop_bound():
    from backend.core.ouroboros.governance.phase_runners import validate_runner

    src = inspect.getsource(validate_runner.VALIDATERunner.run)
    assert "for _iter_idx in range(1 + _ladder_depth):" in src, (
        "the loop still ranges over the configured constant"
    )
    assert "_negotiate_ladder_depth(" in src


def test_the_negotiator_reads_the_same_deadline_the_loop_reads():
    """Negotiation and the per-iteration guard must not disagree about how
    much time exists."""
    from backend.core.ouroboros.governance.phase_runners import validate_runner

    src = inspect.getsource(validate_runner._negotiate_ladder_depth)
    assert "ctx.pipeline_deadline" in src


def test_retries_remaining_starts_at_the_negotiated_depth():
    from backend.core.ouroboros.governance.phase_runners import validate_runner

    src = inspect.getsource(validate_runner.VALIDATERunner.run)
    assert "validate_retries_remaining = _ladder_depth" in src


def test_the_kill_switch_restores_the_constant(monkeypatch):
    monkeypatch.setenv(VLP.ENV_ENABLED, "false")
    _warm(450.0)
    n = negotiate_depth(
        remaining_s=BUDGET_S, configured_depth=CONFIGURED, n_candidates=3,
    )
    assert n.depth == CONFIGURED


@pytest.mark.parametrize(
    "rem,cfg,n",
    [(None, None, None), ("x", "y", "z"), (float("nan"), -1, 0), (object(), object(), object())],
)
def test_never_raises(rem, cfg, n):
    out = negotiate_depth(remaining_s=rem, configured_depth=cfg, n_candidates=n)
    assert out.depth >= 0
