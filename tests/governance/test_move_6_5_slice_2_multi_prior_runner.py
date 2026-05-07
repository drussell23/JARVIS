"""Move 6.5 Slice 2 — MultiPriorRunner.

Operator binding 2026-05-07 (verbatim — non-negotiable):

  "Reuse generative_quorum.compute_consensus and its frozen
   outcome types — do not fork consensus math. Hard
   Task.cancel() on over-budget / prune signal is allowed
   only with an explicit grace (~5s) composited with existing
   timeout primitives. Cancelled rolls may leave partial
   session artifacts — must be observable in ledger, not
   silent."

Pinned coverage (~32 tests):
  * Closed roll-outcome taxonomy (4-value) bytes-pinned
  * Master flag default-FALSE per §33.1 (separate from Slice 1)
  * Disabled verdict shape when master off
  * FAILED verdict on empty/malformed prior_set
  * Happy path: 4 priors → DISAGREEMENT (4 distinct ASTs)
  * Convergence: 4 identical diffs → CONSENSUS
  * Majority: 3 same + 1 distinct → MAJORITY_CONSENSUS
  * Timeout path: per-roll wait_for fires
  * Cost-cancel path: watchdog Task.cancel() + grace drain
  * Cost-cancel observability: cancelled rolls preserved in
    verdict (operator binding "must be observable in ledger,
    not silent")
  * Generator error path: returns GENERATOR_ERROR roll
  * Non-awaitable generator: defensive GENERATOR_ERROR
  * Watchdog defensive against flaky cost oracle (raises →
    treated as not exceeded)
  * Roll-to-prior-id orthogonal threading complete
  * MultiPriorRoll + MultiPriorVerdictResult to_dict shape
  * Public API surface complete + register_flags
  * 5 AST pins clean (parametrized) + each fires on synthetic
    regression
  * Slice 2 does NOT top-level-import compute_consensus
"""
from __future__ import annotations

import ast
import asyncio
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _module_path() -> Path:
    return (
        _repo_root()
        / "backend/core/ouroboros/governance/verification/"
        "multi_prior_runner.py"
    )


def _make_priorset(monkeypatch, k: int = 4):
    """Build a deterministic PriorSet via Slice 1 for use in
    Slice 2 tests."""
    monkeypatch.setenv(
        "JARVIS_MULTI_PRIOR_PLANNING_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.verification.multi_prior_planning import (  # noqa: E501
        materialize_priors,
    )
    return materialize_priors(
        op_id="op-1",
        route="complex",
        posture="EXPLORE",
        k=k,
    )


# ---------------------------------------------------------------------------
# Closed taxonomy
# ---------------------------------------------------------------------------


def test_outcome_taxonomy_4_values():
    from backend.core.ouroboros.governance.verification.multi_prior_runner import (  # noqa: E501
        MultiPriorRollOutcome,
    )
    assert {o.name for o in MultiPriorRollOutcome} == {
        "COMPLETED", "TIMEOUT",
        "CANCELLED_OVER_BUDGET", "GENERATOR_ERROR",
    }


def test_outcome_str_values_canonical():
    from backend.core.ouroboros.governance.verification.multi_prior_runner import (  # noqa: E501
        MultiPriorRollOutcome,
    )
    assert (
        MultiPriorRollOutcome.COMPLETED.value == "completed"
    )
    assert (
        MultiPriorRollOutcome.CANCELLED_OVER_BUDGET.value
        == "cancelled_over_budget"
    )


# ---------------------------------------------------------------------------
# Master flag
# ---------------------------------------------------------------------------


def test_master_default_false(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_MULTI_PRIOR_RUNNER_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.verification.multi_prior_runner import (  # noqa: E501
        master_enabled,
    )
    assert master_enabled() is False


def test_master_truthy(monkeypatch):
    from backend.core.ouroboros.governance.verification.multi_prior_runner import (  # noqa: E501
        master_enabled,
    )
    for v in ("1", "true", "yes", "on"):
        monkeypatch.setenv(
            "JARVIS_MULTI_PRIOR_RUNNER_ENABLED", v,
        )
        assert master_enabled() is True


# ---------------------------------------------------------------------------
# Disabled / failed verdict construction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_verdict_when_master_off(
    monkeypatch,
):
    monkeypatch.delenv(
        "JARVIS_MULTI_PRIOR_RUNNER_ENABLED", raising=False,
    )
    ps = _make_priorset(monkeypatch)
    from backend.core.ouroboros.governance.verification.multi_prior_runner import (  # noqa: E501
        run_multi_prior_quorum,
    )

    async def gen(*, prior, roll_id):  # noqa: ARG001
        return "x"

    verdict = await run_multi_prior_quorum(
        gen, op_id="op-1", prior_set=ps,
    )
    assert (
        verdict.consensus_verdict.outcome.value == "disabled"
    )
    assert verdict.k == 0


@pytest.mark.asyncio
async def test_disabled_verdict_when_priorset_empty(
    monkeypatch,
):
    monkeypatch.setenv(
        "JARVIS_MULTI_PRIOR_RUNNER_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.verification.multi_prior_runner import (  # noqa: E501
        run_multi_prior_quorum,
    )

    class EmptySet:
        priors = ()
        op_id = "op-1"

    async def gen(*, prior, roll_id):  # noqa: ARG001
        return "x"

    verdict = await run_multi_prior_quorum(
        gen, op_id="op-1", prior_set=EmptySet(),
    )
    assert (
        verdict.consensus_verdict.outcome.value == "disabled"
    )


@pytest.mark.asyncio
async def test_enabled_override_takes_precedence(
    monkeypatch,
):
    monkeypatch.delenv(
        "JARVIS_MULTI_PRIOR_RUNNER_ENABLED", raising=False,
    )
    ps = _make_priorset(monkeypatch)
    from backend.core.ouroboros.governance.verification.multi_prior_runner import (  # noqa: E501
        run_multi_prior_quorum,
    )

    async def gen(*, prior, roll_id):  # noqa: ARG001
        return "x"

    verdict = await run_multi_prior_quorum(
        gen, op_id="op-1", prior_set=ps,
        enabled_override=True,
    )
    assert verdict.completed_count == ps.k


# ---------------------------------------------------------------------------
# Happy path: distinct, convergent, majority
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_distinct_diffs_yield_disagreement(
    monkeypatch,
):
    monkeypatch.setenv(
        "JARVIS_MULTI_PRIOR_RUNNER_ENABLED", "true",
    )
    ps = _make_priorset(monkeypatch)
    from backend.core.ouroboros.governance.verification.multi_prior_runner import (  # noqa: E501
        run_multi_prior_quorum, MultiPriorRollOutcome,
    )

    async def gen(*, prior, roll_id):
        return f"unique-diff-for-{prior.prior_id}-{roll_id}"

    verdict = await run_multi_prior_quorum(
        gen, op_id="op-1", prior_set=ps,
    )
    assert verdict.completed_count == 4
    assert (
        verdict.consensus_verdict.outcome.value
        == "disagreement"
    )
    for r in verdict.rolls:
        assert (
            r.outcome is MultiPriorRollOutcome.COMPLETED
        )


@pytest.mark.asyncio
async def test_convergent_diffs_yield_consensus(
    monkeypatch,
):
    monkeypatch.setenv(
        "JARVIS_MULTI_PRIOR_RUNNER_ENABLED", "true",
    )
    ps = _make_priorset(monkeypatch)
    from backend.core.ouroboros.governance.verification.multi_prior_runner import (  # noqa: E501
        run_multi_prior_quorum,
    )

    async def gen(*, prior, roll_id):  # noqa: ARG001
        return "identical"

    verdict = await run_multi_prior_quorum(
        gen, op_id="op-1", prior_set=ps,
    )
    assert verdict.completed_count == 4
    assert (
        verdict.consensus_verdict.outcome.value
        == "consensus"
    )
    assert verdict.consensus_verdict.is_unanimous() is True


@pytest.mark.asyncio
async def test_majority_diffs_yield_majority_consensus(
    monkeypatch,
):
    monkeypatch.setenv(
        "JARVIS_MULTI_PRIOR_RUNNER_ENABLED", "true",
    )
    ps = _make_priorset(monkeypatch)
    from backend.core.ouroboros.governance.verification.multi_prior_runner import (  # noqa: E501
        run_multi_prior_quorum,
    )

    call_count = {"n": 0}

    async def gen(*, prior, roll_id):  # noqa: ARG001
        idx = call_count["n"]
        call_count["n"] += 1
        # 3 priors return same diff, 1 returns different
        return "outlier" if idx == 3 else "majority"

    verdict = await run_multi_prior_quorum(
        gen, op_id="op-1", prior_set=ps,
    )
    assert verdict.completed_count == 4
    assert verdict.consensus_verdict.outcome.value in (
        "majority_consensus",
    )


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeout_classifies_each_roll(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_MULTI_PRIOR_RUNNER_ENABLED", "true",
    )
    ps = _make_priorset(monkeypatch)
    from backend.core.ouroboros.governance.verification.multi_prior_runner import (  # noqa: E501
        run_multi_prior_quorum, MultiPriorRollOutcome,
    )

    async def slow(*, prior, roll_id):  # noqa: ARG001
        await asyncio.sleep(2.0)
        return "x"

    verdict = await run_multi_prior_quorum(
        slow, op_id="op-1", prior_set=ps,
        timeout_per_roll_s=0.2,
    )
    assert verdict.timeout_count == 4
    assert verdict.completed_count == 0
    for r in verdict.rolls:
        assert r.outcome is MultiPriorRollOutcome.TIMEOUT


# ---------------------------------------------------------------------------
# Cost-cap watchdog cancellation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cost_cancel_fires_task_cancel(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_MULTI_PRIOR_RUNNER_ENABLED", "true",
    )
    ps = _make_priorset(monkeypatch)
    from backend.core.ouroboros.governance.verification.multi_prior_runner import (  # noqa: E501
        run_multi_prior_quorum, MultiPriorRollOutcome,
    )

    class TripImmediately:
        def is_exceeded(self):
            return True

    async def slow(*, prior, roll_id):  # noqa: ARG001
        await asyncio.sleep(2.0)
        return "x"

    verdict = await run_multi_prior_quorum(
        slow, op_id="op-1", prior_set=ps,
        timeout_per_roll_s=10.0,
        cost_governor_snapshot=TripImmediately(),
        cost_check_interval_s=0.05,
        grace_period_s=0.5,
    )
    assert verdict.cancelled_count == 4
    for r in verdict.rolls:
        assert r.outcome is (
            MultiPriorRollOutcome.CANCELLED_OVER_BUDGET
        )


@pytest.mark.asyncio
async def test_cost_cancel_observable_in_ledger(
    monkeypatch,
):
    """Operator binding: cancelled rolls MUST be observable
    in the verdict (not silent). Roll-to-prior-id mapping
    MUST cover every cancelled roll."""
    monkeypatch.setenv(
        "JARVIS_MULTI_PRIOR_RUNNER_ENABLED", "true",
    )
    ps = _make_priorset(monkeypatch)
    from backend.core.ouroboros.governance.verification.multi_prior_runner import (  # noqa: E501
        run_multi_prior_quorum,
    )

    class TripImmediately:
        def is_exceeded(self):
            return True

    async def slow(*, prior, roll_id):  # noqa: ARG001
        await asyncio.sleep(2.0)
        return "x"

    verdict = await run_multi_prior_quorum(
        slow, op_id="op-1", prior_set=ps,
        timeout_per_roll_s=10.0,
        cost_governor_snapshot=TripImmediately(),
        cost_check_interval_s=0.05,
        grace_period_s=0.5,
    )
    assert len(verdict.rolls) == 4
    assert len(verdict.roll_to_prior_id) == 4
    for r in verdict.rolls:
        assert r.roll_id in verdict.roll_to_prior_id
        assert (
            verdict.roll_to_prior_id[r.roll_id]
            == r.prior_id
        )


@pytest.mark.asyncio
async def test_cost_oracle_flaky_does_not_kill_rolls(
    monkeypatch,
):
    """Defensive: cost oracle that raises is treated as
    'not exceeded' so flaky budget reads don't cancel
    spuriously."""
    monkeypatch.setenv(
        "JARVIS_MULTI_PRIOR_RUNNER_ENABLED", "true",
    )
    ps = _make_priorset(monkeypatch)
    from backend.core.ouroboros.governance.verification.multi_prior_runner import (  # noqa: E501
        run_multi_prior_quorum,
    )

    class FlakyCost:
        def is_exceeded(self):
            raise RuntimeError("oracle blew up")

    async def quick(*, prior, roll_id):  # noqa: ARG001
        return f"out-{roll_id}"

    verdict = await run_multi_prior_quorum(
        quick, op_id="op-1", prior_set=ps,
        cost_governor_snapshot=FlakyCost(),
        cost_check_interval_s=0.05,
    )
    # All 4 should complete (flaky oracle doesn't cancel)
    assert verdict.completed_count == 4
    assert verdict.cancelled_count == 0


@pytest.mark.asyncio
async def test_cost_oracle_polls_periodically(monkeypatch):
    """Watchdog polls injected oracle on cadence."""
    monkeypatch.setenv(
        "JARVIS_MULTI_PRIOR_RUNNER_ENABLED", "true",
    )
    ps = _make_priorset(monkeypatch, k=2)
    from backend.core.ouroboros.governance.verification.multi_prior_runner import (  # noqa: E501
        run_multi_prior_quorum,
    )

    class CountingCost:
        def __init__(self):
            self.calls = 0

        def is_exceeded(self):
            self.calls += 1
            return False

    async def medium(*, prior, roll_id):  # noqa: ARG001
        await asyncio.sleep(0.4)
        return "x"

    cost = CountingCost()
    await run_multi_prior_quorum(
        medium, op_id="op-1", prior_set=ps,
        cost_governor_snapshot=cost,
        cost_check_interval_s=0.1,
    )
    # 0.4s rolls / 0.1s poll → at least 2 polls
    assert cost.calls >= 2


# ---------------------------------------------------------------------------
# Generator error paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generator_error_classified(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_MULTI_PRIOR_RUNNER_ENABLED", "true",
    )
    ps = _make_priorset(monkeypatch)
    from backend.core.ouroboros.governance.verification.multi_prior_runner import (  # noqa: E501
        run_multi_prior_quorum, MultiPriorRollOutcome,
    )

    async def boom(*, prior, roll_id):  # noqa: ARG001
        raise RuntimeError("provider blew up")

    verdict = await run_multi_prior_quorum(
        boom, op_id="op-1", prior_set=ps,
    )
    assert verdict.error_count == 4
    for r in verdict.rolls:
        assert r.outcome is (
            MultiPriorRollOutcome.GENERATOR_ERROR
        )


@pytest.mark.asyncio
async def test_non_awaitable_generator_classified(
    monkeypatch,
):
    monkeypatch.setenv(
        "JARVIS_MULTI_PRIOR_RUNNER_ENABLED", "true",
    )
    ps = _make_priorset(monkeypatch)
    from backend.core.ouroboros.governance.verification.multi_prior_runner import (  # noqa: E501
        run_multi_prior_quorum, MultiPriorRollOutcome,
    )

    def sync_gen(*, prior, roll_id):  # noqa: ARG001
        return "not-awaitable"

    verdict = await run_multi_prior_quorum(
        sync_gen,  # type: ignore[arg-type]
        op_id="op-1", prior_set=ps,
    )
    assert verdict.error_count == 4
    for r in verdict.rolls:
        assert r.outcome is (
            MultiPriorRollOutcome.GENERATOR_ERROR
        )


# ---------------------------------------------------------------------------
# Orthogonal prior-identity threading
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_roll_to_prior_id_complete(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_MULTI_PRIOR_RUNNER_ENABLED", "true",
    )
    ps = _make_priorset(monkeypatch)
    from backend.core.ouroboros.governance.verification.multi_prior_runner import (  # noqa: E501
        run_multi_prior_quorum,
    )

    async def gen(*, prior, roll_id):  # noqa: ARG001
        return "x"

    verdict = await run_multi_prior_quorum(
        gen, op_id="op-1", prior_set=ps,
    )
    expected_prior_ids = {p.prior_id for p in ps.priors}
    actual_prior_ids = set(verdict.roll_to_prior_id.values())
    assert actual_prior_ids == expected_prior_ids


# ---------------------------------------------------------------------------
# Frozen artifact serialization
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verdict_to_dict_shape(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_MULTI_PRIOR_RUNNER_ENABLED", "true",
    )
    ps = _make_priorset(monkeypatch)
    from backend.core.ouroboros.governance.verification.multi_prior_runner import (  # noqa: E501
        run_multi_prior_quorum,
    )

    async def gen(*, prior, roll_id):  # noqa: ARG001
        return "x"

    verdict = await run_multi_prior_quorum(
        gen, op_id="op-1", prior_set=ps,
    )
    d = verdict.to_dict()
    assert d["op_id"] == "op-1"
    assert d["completed_count"] == 4
    assert d["consensus_verdict"] is not None
    assert isinstance(d["rolls"], list)
    assert len(d["rolls"]) == 4
    assert "schema_version" in d


@pytest.mark.asyncio
async def test_roll_to_dict_shape(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_MULTI_PRIOR_RUNNER_ENABLED", "true",
    )
    ps = _make_priorset(monkeypatch, k=2)
    from backend.core.ouroboros.governance.verification.multi_prior_runner import (  # noqa: E501
        run_multi_prior_quorum,
    )

    async def gen(*, prior, roll_id):  # noqa: ARG001
        return "diff"

    verdict = await run_multi_prior_quorum(
        gen, op_id="op-1", prior_set=ps,
    )
    d = verdict.rolls[0].to_dict()
    assert d["outcome"] == "completed"
    assert d["candidate_diff"] == "diff"
    assert "schema_version" in d


@pytest.mark.asyncio
async def test_is_actionable_composes_move6(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_MULTI_PRIOR_RUNNER_ENABLED", "true",
    )
    ps = _make_priorset(monkeypatch)
    from backend.core.ouroboros.governance.verification.multi_prior_runner import (  # noqa: E501
        run_multi_prior_quorum,
    )

    async def gen(*, prior, roll_id):  # noqa: ARG001
        return "identical"

    verdict = await run_multi_prior_quorum(
        gen, op_id="op-1", prior_set=ps,
    )
    assert verdict.is_actionable() is True


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pin_name", [
        "multi_prior_runner_taxonomy_4_values",
        "multi_prior_runner_master_default_false",
        "multi_prior_runner_authority_asymmetry",
        "multi_prior_runner_no_top_level_consensus_import",
        "multi_prior_runner_cancellation_discipline",
    ],
)
def test_ast_pin_validates_clean(pin_name):
    from backend.core.ouroboros.governance.verification.multi_prior_runner import (  # noqa: E501
        register_shipped_invariants,
    )
    src = _module_path().read_text(encoding="utf-8")
    tree = ast.parse(src)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == pin_name
    )
    violations = pin.validate(tree, src)
    assert violations == ()


def test_taxonomy_pin_fires_on_drift():
    from backend.core.ouroboros.governance.verification.multi_prior_runner import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = '''
class MultiPriorRollOutcome:
    COMPLETED = "completed"
    TIMEOUT = "timeout"
    EXTRA = "extra"
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "multi_prior_runner_taxonomy_4_values"
        )
    )
    assert pin.validate(tree, bad)


def test_authority_pin_fires_on_orchestrator_import():
    from backend.core.ouroboros.governance.verification.multi_prior_runner import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = (
        "from backend.core.ouroboros.governance.orchestrator "
        "import x"
    )
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "multi_prior_runner_authority_asymmetry"
        )
    )
    assert pin.validate(tree, bad)


def test_no_top_level_consensus_pin_fires_on_top_level():
    from backend.core.ouroboros.governance.verification.multi_prior_runner import (  # noqa: E501
        register_shipped_invariants,
    )
    forbidden = "compute" + "_consensus"
    bad = (
        "from backend.core.ouroboros.governance.verification."
        f"generative_quorum import {forbidden}\n"
        "x = 1\n"
    )
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "multi_prior_runner_no_top_level_consensus_import"
        )
    )
    assert pin.validate(tree, bad)


def test_cancellation_discipline_pin_fires_when_missing():
    from backend.core.ouroboros.governance.verification.multi_prior_runner import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = """
async def run_multi_prior_quorum():
    pass

async def _cost_watchdog():
    pass
"""
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "multi_prior_runner_cancellation_discipline"
        )
    )
    assert pin.validate(tree, bad)


# ---------------------------------------------------------------------------
# Public API + runtime no-top-level import discipline
# ---------------------------------------------------------------------------


def test_public_api_complete():
    from backend.core.ouroboros.governance.verification import (  # noqa: E501
        multi_prior_runner as mod,
    )
    expected = {
        "CostBudgetSnapshot",
        "MULTI_PRIOR_RUNNER_SCHEMA_VERSION",
        "MultiPriorGenerator",
        "MultiPriorRoll", "MultiPriorRollOutcome",
        "MultiPriorVerdictResult",
        "master_enabled",
        "register_flags",
        "register_shipped_invariants",
        "run_multi_prior_quorum",
    }
    assert set(mod.__all__) == expected


def test_register_flags_seeds_master():
    from backend.core.ouroboros.governance.verification.multi_prior_runner import (  # noqa: E501
        register_flags,
    )
    registry = MagicMock()
    register_flags(registry)
    assert registry.register.call_count == 1
    assert (
        registry.register.call_args.kwargs["name"]
        == "JARVIS_MULTI_PRIOR_RUNNER_ENABLED"
    )


def test_register_flags_swallows_errors():
    from backend.core.ouroboros.governance.verification.multi_prior_runner import (  # noqa: E501
        register_flags,
    )
    registry = MagicMock()
    registry.register.side_effect = RuntimeError("boom")
    register_flags(registry)


def test_runner_does_not_top_level_import_consensus():
    """Confirms the runner module's __dict__ does NOT carry
    Move 6's compute_consensus at import time. Slice 2 must
    lazy-import inside its run function — composition
    discipline runtime check."""
    from backend.core.ouroboros.governance.verification import (  # noqa: E501
        multi_prior_runner as mod,
    )
    forbidden = "compute" + "_consensus"
    assert forbidden not in mod.__dict__


# ---------------------------------------------------------------------------
# Wall-clock sanity (defensive — not a perf test)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wall_clock_recorded(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_MULTI_PRIOR_RUNNER_ENABLED", "true",
    )
    ps = _make_priorset(monkeypatch, k=2)
    from backend.core.ouroboros.governance.verification.multi_prior_runner import (  # noqa: E501
        run_multi_prior_quorum,
    )

    async def gen(*, prior, roll_id):  # noqa: ARG001
        await asyncio.sleep(0.05)
        return "x"

    start = time.monotonic()
    verdict = await run_multi_prior_quorum(
        gen, op_id="op-1", prior_set=ps,
    )
    elapsed = time.monotonic() - start
    assert verdict.wall_clock_s > 0
    assert verdict.wall_clock_s <= elapsed + 0.1
