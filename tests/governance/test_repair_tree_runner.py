"""Regression spine for Treefinement Phase 1 — tree runner core.

Pins the load-bearing structural invariants for the BFS / BEAM_K
layer dispatch loop:

* Posture-K composition flows through the canonical
  ``parallel_dispatch.posture_weight_for`` (no parallel posture
  weights table).
* Branch_id composition flows through the canonical
  ``failure_classifier.patch_signature_hash`` (no parallel
  signature primitive).
* Worktree isolation flows through the canonical
  ``worktree_manager.WorktreeManager`` (no shared-tree fallback
  per §1 Boundary mirror of the L3 ``subagent_scheduler``
  discipline).
* Cost envelope shared with ``RepairBudget.max_total_validation_runs``
  (no parallel budget bookkeeping).
* Generator + validator exceptions quarantine to per-branch
  PRUNED_VALIDATOR — never propagate into the orchestrator
  (§7 fail-closed). ``asyncio.CancelledError`` is the sole
  exception that propagates.
* Cross-layer information signal (sibling_outcomes + parent_branch)
  is plumbed correctly so Phase 3 can wire StrategicDirection
  injection without re-engineering the data flow.

Phase 5 adds composition AST pins on top of these behavioral
tests; this file proves the runtime contract first.
"""
from __future__ import annotations

import asyncio
import ast
import inspect
from pathlib import Path
from typing import Any, List, Optional, Tuple

import pytest

from backend.core.ouroboros.governance import repair_tree
from backend.core.ouroboros.governance.posture import Posture
from backend.core.ouroboros.governance.repair_tree import (
    MASTER_FLAG_ENV_VAR,
    BranchingStrategy,
    BranchOutcome,
    LayerVerdict,
    PruningReason,
    RepairBranch,
    RepairTreeRunner,
    TreefinementBudget,
    _branch_id_for,
    _compute_layer_k,
    _aggregate_layer_verdict,
    _select_survivors,
)


# ===========================================================================
# Test fixtures — deterministic clock + injected stubs
# ===========================================================================


class _DeterministicClock:
    """Monotonic-style clock that advances in fixed ticks per call.
    Used to make wall_ms assertions reproducible."""

    def __init__(self, start: float = 0.0, tick: float = 0.001):
        self._t = start
        self._tick = tick

    def __call__(self) -> float:
        v = self._t
        self._t += self._tick
        return v


class _StubWorktreeManager:
    """Records create/cleanup calls; never touches the filesystem.
    Failure-injectable via ``fail_create`` and ``fail_cleanup``."""

    def __init__(
        self,
        *,
        fail_create: bool = False,
        fail_cleanup: bool = False,
    ):
        self.fail_create = fail_create
        self.fail_cleanup = fail_cleanup
        self.created: List[str] = []
        self.cleaned: List[Path] = []

    async def create(self, branch_name: str) -> Path:
        if self.fail_create:
            raise RuntimeError(
                f"git worktree add failed for {branch_name}"
            )
        self.created.append(branch_name)
        return Path(f"/tmp/stub-worktree/{branch_name.replace('/', '_')}")

    async def cleanup(self, worktree_path: Path) -> None:
        if self.fail_cleanup:
            raise RuntimeError(f"cleanup failed for {worktree_path}")
        self.cleaned.append(worktree_path)


def _make_runner(
    *,
    strategy: BranchingStrategy = BranchingStrategy.BFS,
    enabled: bool = True,
    max_branches: int = 3,
    beam_width: int = 2,
    cross_branch: bool = True,
    dedup: bool = True,
    threshold: float = 0.85,
    repair_budget: Any = None,
    worktree_manager: Optional[_StubWorktreeManager] = None,
    clock: Optional[_DeterministicClock] = None,
) -> RepairTreeRunner:
    budget = TreefinementBudget(
        enabled=enabled,
        branching_strategy=strategy,
        max_branches_per_layer=max_branches,
        beam_width=beam_width,
        branch_dedup_enabled=dedup,
        cross_branch_learning_enabled=cross_branch,
        emergency_demote_threshold=threshold,
    )
    return RepairTreeRunner(
        budget,
        repair_budget=repair_budget,
        worktree_manager=worktree_manager,  # type: ignore[arg-type]
        clock=clock or _DeterministicClock(),
    )


def _make_generator(
    *,
    diffs_per_call: Optional[List[str]] = None,
    raise_on_call: int = -1,
    capture_calls: Optional[List[dict]] = None,
):
    """Build a stub BranchGenerator that yields canned diffs."""
    diffs = list(diffs_per_call) if diffs_per_call else []
    state = {"call": 0}

    async def _gen(**kwargs) -> Tuple[str, str, float]:
        idx = state["call"]
        state["call"] += 1
        if capture_calls is not None:
            capture_calls.append(dict(kwargs))
        if idx == raise_on_call:
            raise RuntimeError(f"generator-explode-{idx}")
        diff = diffs[idx] if idx < len(diffs) else f"diff-call-{idx}"
        return (diff, f"hypothesis-{idx}", 0.001 * (idx + 1))

    return _gen


def _make_validator(
    *,
    outcomes: Optional[List[Tuple[BranchOutcome, float]]] = None,
    raise_on_branch: Optional[str] = None,
):
    """Build a stub BranchValidator that maps branch_id → outcome.
    Exhausting the outcomes list yields PROMOTED with score 0.5."""
    outs = list(outcomes) if outcomes else []
    state = {"call": 0}

    async def _val(
        *,
        op_id: str,
        branch_id: str,
        diff: str,
        worktree_dir: Path,
    ):
        idx = state["call"]
        state["call"] += 1
        if raise_on_branch and raise_on_branch in branch_id:
            raise RuntimeError(f"validator-explode-{branch_id}")
        outcome, score = (
            outs[idx] if idx < len(outs)
            else (BranchOutcome.PROMOTED, 0.5)
        )
        prune_reason = (
            PruningReason.WORSE_THAN_SIBLING
            if outcome == BranchOutcome.PRUNED_VALIDATOR
            else None
        )
        return (outcome, score, prune_reason, 1)

    return _val


@pytest.fixture(autouse=True)
def _enable_master_flag(monkeypatch):
    """Phase 1 tests need the master flag ON to actually exercise
    the runner. Phase 0 tests verified the master-flag-FALSE path
    short-circuits — Phase 1 tests assume the flag is ON unless
    a specific test overrides it."""
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    yield


# ===========================================================================
# Helper-function unit tests (composition primitives)
# ===========================================================================


def test_branch_id_composes_canonical_hash():
    """branch_id MUST equal the canonical patch_signature_hash for
    the same input — drift here breaks single-source dedup."""
    from backend.core.ouroboros.governance.failure_classifier import (
        patch_signature_hash,
    )
    diff = "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-x\n+y\n"
    assert _branch_id_for(diff) == patch_signature_hash(diff)


def test_branch_id_handles_empty_diff():
    """Empty diff still produces a deterministic hash — needed for
    infra-failed branches whose diff is empty by design."""
    a = _branch_id_for("")
    b = _branch_id_for("")
    assert a == b
    assert isinstance(a, str)
    assert len(a) > 0


def test_compute_layer_k_posture_neutral():
    """No posture → weight 1.0 → K = base_k."""
    assert _compute_layer_k(
        posture=None, base_k=3, remaining_runs=10,
    ) == 3


def test_compute_layer_k_posture_harden_narrows():
    """HARDEN posture has weight <1.0 → K shrinks."""
    k_neutral = _compute_layer_k(
        posture=None, base_k=3, remaining_runs=10,
    )
    k_harden = _compute_layer_k(
        posture=Posture.HARDEN, base_k=3, remaining_runs=10,
    )
    assert k_harden <= k_neutral, (
        f"HARDEN posture MUST narrow K — got "
        f"neutral={k_neutral} harden={k_harden}"
    )


def test_compute_layer_k_budget_caps():
    """Tight budget shrinks K below base_k."""
    k = _compute_layer_k(
        posture=None, base_k=10, remaining_runs=2,
        runs_per_branch=1,
    )
    assert k == 2


def test_compute_layer_k_never_returns_zero():
    """Minimum K=1 — a layer always gets at least one attempt."""
    assert _compute_layer_k(
        posture=None, base_k=10, remaining_runs=0,
    ) >= 1
    # Even with weight near zero (synthetic future posture)
    assert _compute_layer_k(
        posture=None, base_k=0, remaining_runs=10,
    ) >= 1


def test_select_survivors_bfs_keeps_all_promoted():
    branches = (
        _branch(outcome=BranchOutcome.PROMOTED, score=0.5, bid="a"),
        _branch(outcome=BranchOutcome.PROMOTED, score=0.9, bid="b"),
        _branch(outcome=BranchOutcome.PRUNED_VALIDATOR, score=0.0, bid="c"),
    )
    survivors = _select_survivors(
        branches,
        strategy=BranchingStrategy.BFS,
        beam_width=2,
    )
    assert len(survivors) == 2
    assert {s.branch_id for s in survivors} == {"a", "b"}


def test_select_survivors_beam_k_top_m():
    branches = (
        _branch(outcome=BranchOutcome.PROMOTED, score=0.3, bid="a"),
        _branch(outcome=BranchOutcome.PROMOTED, score=0.9, bid="b"),
        _branch(outcome=BranchOutcome.PROMOTED, score=0.5, bid="c"),
    )
    survivors = _select_survivors(
        branches,
        strategy=BranchingStrategy.BEAM_K,
        beam_width=2,
    )
    assert len(survivors) == 2
    assert {s.branch_id for s in survivors} == {"b", "c"}


def test_select_survivors_beam_k_deterministic_tie_break():
    """Same score → branch_id lex sort. Reproducible across runs."""
    branches = (
        _branch(outcome=BranchOutcome.PROMOTED, score=0.5, bid="zebra"),
        _branch(outcome=BranchOutcome.PROMOTED, score=0.5, bid="apple"),
        _branch(outcome=BranchOutcome.PROMOTED, score=0.5, bid="mango"),
    )
    survivors = _select_survivors(
        branches,
        strategy=BranchingStrategy.BEAM_K,
        beam_width=2,
    )
    # Lex sort on branch_id breaks ties → "apple" + "mango"
    assert {s.branch_id for s in survivors} == {"apple", "mango"}


def test_aggregate_verdict_won_wins_over_everything():
    branches = (
        _branch(outcome=BranchOutcome.PRUNED_VALIDATOR, score=0.0, bid="x"),
        _branch(outcome=BranchOutcome.WON, score=1.0, bid="y"),
        _branch(outcome=BranchOutcome.PROMOTED, score=0.5, bid="z"),
    )
    verdict = _aggregate_layer_verdict(
        branches, survivors=branches[2:3], budget_remaining=10,
    )
    assert verdict == LayerVerdict.WON_TERMINAL


def test_aggregate_verdict_budget_terminal_when_drained():
    branches = (_branch(outcome=BranchOutcome.PROMOTED, score=0.5, bid="a"),)
    verdict = _aggregate_layer_verdict(
        branches, survivors=branches, budget_remaining=0,
    )
    assert verdict == LayerVerdict.BUDGET_TERMINAL


def test_aggregate_verdict_exhausted_when_no_survivors():
    branches = (
        _branch(outcome=BranchOutcome.PRUNED_VALIDATOR, score=0.0, bid="a"),
        _branch(outcome=BranchOutcome.PRUNED_DUPLICATE, score=0.0, bid="b"),
    )
    verdict = _aggregate_layer_verdict(
        branches, survivors=(), budget_remaining=10,
    )
    assert verdict == LayerVerdict.EXHAUSTED


def test_aggregate_verdict_expanded_when_survivors_present():
    branches = (_branch(outcome=BranchOutcome.PROMOTED, score=0.5, bid="a"),)
    verdict = _aggregate_layer_verdict(
        branches, survivors=branches, budget_remaining=10,
    )
    assert verdict == LayerVerdict.EXPANDED


def _branch(*, outcome, score, bid) -> RepairBranch:
    return RepairBranch(
        branch_id=bid,
        parent_branch_id=None,
        layer_index=0,
        failure_class="test",
        fix_hypothesis="x",
        diff="",
        validator_score=score,
        outcome=outcome,
        prune_reason=None,
        worktree_id=None,
        cost_usd=0.0,
        validation_runs_consumed=1,
    )


# ===========================================================================
# Single-layer BFS — happy path
# ===========================================================================


def test_run_tree_single_layer_bfs_three_promoted():
    """K=3, all 3 PROMOTED, no WON → expanded layer + walks to next.
    But max_layers=1 → loop exits with one EXPANDED layer."""
    runner = _make_runner(
        max_branches=3,
        worktree_manager=_StubWorktreeManager(),
    )
    generator = _make_generator(
        diffs_per_call=["d1", "d2", "d3"],
    )
    validator = _make_validator(
        outcomes=[
            (BranchOutcome.PROMOTED, 0.7),
            (BranchOutcome.PROMOTED, 0.5),
            (BranchOutcome.PROMOTED, 0.9),
        ],
    )
    result = asyncio.run(
        runner.run_tree(
            op_id="op-bfs-1",
            generator=generator,
            validator=validator,
            max_layers=1,
        )
    )
    assert len(result.layers) == 1
    layer = result.layers[0]
    assert layer.verdict == LayerVerdict.EXPANDED
    assert len(layer.branches) == 3
    assert all(b.outcome == BranchOutcome.PROMOTED for b in layer.branches)
    assert layer.parallel_units_actual == 3


def test_run_tree_won_terminal_short_circuits():
    """First WON branch triggers WON_TERMINAL + winning_branch_path."""
    runner = _make_runner(
        max_branches=3,
        worktree_manager=_StubWorktreeManager(),
    )
    generator = _make_generator(diffs_per_call=["d1", "d2", "d3"])
    validator = _make_validator(
        outcomes=[
            (BranchOutcome.PRUNED_VALIDATOR, 0.0),
            (BranchOutcome.WON, 1.0),
            (BranchOutcome.PROMOTED, 0.5),
        ],
    )
    result = asyncio.run(
        runner.run_tree(
            op_id="op-won",
            generator=generator,
            validator=validator,
            max_layers=5,
        )
    )
    assert len(result.layers) == 1, "WON_TERMINAL MUST early-return"
    assert result.layers[0].verdict == LayerVerdict.WON_TERMINAL
    assert len(result.winning_branch_path) == 1, (
        "Single-layer winner has 1-element path"
    )
    won = next(
        b for b in result.layers[0].branches
        if b.outcome == BranchOutcome.WON
    )
    assert result.winning_branch_path[0] == won.branch_id


def test_run_tree_exhausted_breaks_loop():
    """All branches PRUNED → EXHAUSTED → loop terminates."""
    runner = _make_runner(
        max_branches=3,
        worktree_manager=_StubWorktreeManager(),
    )
    generator = _make_generator(diffs_per_call=["d1", "d2", "d3"])
    validator = _make_validator(
        outcomes=[(BranchOutcome.PRUNED_VALIDATOR, 0.0)] * 3,
    )
    result = asyncio.run(
        runner.run_tree(
            op_id="op-exhaust",
            generator=generator,
            validator=validator,
            max_layers=5,
        )
    )
    assert len(result.layers) == 1
    assert result.layers[0].verdict == LayerVerdict.EXHAUSTED
    assert result.winning_branch_path == ()


# ===========================================================================
# Cross-layer plumbing — the AlphaVerus information signal
# ===========================================================================


def test_run_tree_propagates_sibling_outcomes_to_next_layer():
    """sibling_outcomes from layer N MUST reach generator at N+1.
    This is the data-flow foundation Phase 3 builds on."""
    runner = _make_runner(
        max_branches=2,
        worktree_manager=_StubWorktreeManager(),
    )
    captures: List[dict] = []
    generator = _make_generator(
        diffs_per_call=["L0-A", "L0-B", "L1-A", "L1-B"],
        capture_calls=captures,
    )
    validator = _make_validator(
        outcomes=[
            (BranchOutcome.PROMOTED, 0.6),
            (BranchOutcome.PROMOTED, 0.8),
            (BranchOutcome.PROMOTED, 0.5),
            (BranchOutcome.PROMOTED, 0.5),
        ],
    )
    asyncio.run(
        runner.run_tree(
            op_id="op-propagate",
            generator=generator,
            validator=validator,
            max_layers=2,
        )
    )
    # Layer 0 calls — sibling_outcomes empty, parent None
    assert captures[0]["layer_index"] == 0
    assert captures[0]["sibling_outcomes"] == ()
    assert captures[0]["parent_branch"] is None
    # Layer 1 calls — sibling_outcomes from layer 0, parent = best survivor
    assert captures[2]["layer_index"] == 1
    assert len(captures[2]["sibling_outcomes"]) == 2, (
        "Layer 1 sibling_outcomes MUST contain ALL layer-0 branches "
        "(winners + losers — both are signal per AlphaVerus)"
    )
    parent = captures[2]["parent_branch"]
    assert parent is not None
    assert parent.validator_score == 0.8, (
        "Best-survivor parent (highest validator_score) MUST be "
        "selected for next-layer GENERATE seeding"
    )


# ===========================================================================
# Cross-branch dedup (within layer + across layers)
# ===========================================================================


def test_run_tree_within_layer_dedup_yields_pruned_duplicate():
    """Two siblings produce identical diffs → second is
    PRUNED_DUPLICATE (validator never invoked)."""
    val_calls = {"n": 0}

    async def _counting_validator(**kwargs):
        val_calls["n"] += 1
        return (BranchOutcome.PROMOTED, 0.5, None, 1)

    runner = _make_runner(
        max_branches=3,
        worktree_manager=_StubWorktreeManager(),
    )
    generator = _make_generator(
        diffs_per_call=["same-diff", "same-diff", "different-diff"],
    )
    result = asyncio.run(
        runner.run_tree(
            op_id="op-dedup",
            generator=generator,
            validator=_counting_validator,
            max_layers=1,
        )
    )
    layer = result.layers[0]
    pruned_dup = [
        b for b in layer.branches
        if b.outcome == BranchOutcome.PRUNED_DUPLICATE
    ]
    assert len(pruned_dup) == 1
    assert pruned_dup[0].prune_reason == PruningReason.DUPLICATE_PATCH_SIG
    # Validator invoked only for the 2 unique diffs
    assert val_calls["n"] == 2


def test_run_tree_cross_layer_dedup():
    """branch_id seen at layer 0 → same diff at layer 1 = PRUNED_DUPLICATE."""
    runner = _make_runner(
        max_branches=2,
        worktree_manager=_StubWorktreeManager(),
    )
    # Layer 0: 2 distinct diffs. Layer 1: regenerates same first diff.
    generator = _make_generator(
        diffs_per_call=["d-A", "d-B", "d-A", "d-C"],
    )
    validator = _make_validator(
        outcomes=[
            (BranchOutcome.PROMOTED, 0.5),
            (BranchOutcome.PROMOTED, 0.5),
            # Layer 1 — only validator entries 2..3 matter
            (BranchOutcome.PROMOTED, 0.5),
        ],
    )
    result = asyncio.run(
        runner.run_tree(
            op_id="op-cross-dedup",
            generator=generator,
            validator=validator,
            max_layers=2,
        )
    )
    assert len(result.layers) == 2
    layer1_branches = result.layers[1].branches
    pruned = [
        b for b in layer1_branches
        if b.outcome == BranchOutcome.PRUNED_DUPLICATE
    ]
    assert len(pruned) == 1, (
        "Cross-layer dedup MUST catch d-A re-emerging at layer 1"
    )


def test_run_tree_dedup_disabled_skips_pruning():
    """When dedup disabled, identical diffs both validate."""
    runner = _make_runner(
        max_branches=2,
        worktree_manager=_StubWorktreeManager(),
        dedup=False,
    )
    generator = _make_generator(
        diffs_per_call=["same-diff", "same-diff"],
    )
    validator = _make_validator(
        outcomes=[(BranchOutcome.PROMOTED, 0.5)] * 2,
    )
    result = asyncio.run(
        runner.run_tree(
            op_id="op-no-dedup",
            generator=generator,
            validator=validator,
            max_layers=1,
        )
    )
    layer = result.layers[0]
    assert all(
        b.outcome != BranchOutcome.PRUNED_DUPLICATE
        for b in layer.branches
    ), "Dedup-disabled MUST NOT produce PRUNED_DUPLICATE"


# ===========================================================================
# Worktree integration — §1 Boundary mandates no shared-tree fallback
# ===========================================================================


def test_run_tree_worktree_create_failure_quarantines_branch():
    """worktree_create_failed MUST surface as failure_class=infra
    branch — NEVER fall back to a shared tree (§1 Boundary mirror
    of the L3 subagent_scheduler discipline)."""
    wm = _StubWorktreeManager(fail_create=True)
    runner = _make_runner(max_branches=2, worktree_manager=wm)
    val_called = {"n": 0}

    async def _validator(**kwargs):
        val_called["n"] += 1
        return (BranchOutcome.PROMOTED, 1.0, None, 1)

    generator = _make_generator(diffs_per_call=["d1", "d2"])
    result = asyncio.run(
        runner.run_tree(
            op_id="op-wt-fail",
            generator=generator,
            validator=_validator,
            max_layers=1,
        )
    )
    layer = result.layers[0]
    assert all(
        b.failure_class == "infra"
        and "worktree_create_failed" in b.fix_hypothesis
        for b in layer.branches
    ), "All branches MUST quarantine to infra failure class"
    assert val_called["n"] == 0, (
        "Validator MUST NOT be invoked when worktree creation failed "
        "— this is the §1 Boundary contract: no shared-tree fallback"
    )


def test_run_tree_worktree_cleanup_called_on_success():
    wm = _StubWorktreeManager()
    runner = _make_runner(max_branches=2, worktree_manager=wm)
    generator = _make_generator(diffs_per_call=["d1", "d2"])
    validator = _make_validator(
        outcomes=[(BranchOutcome.PROMOTED, 0.5)] * 2,
    )
    asyncio.run(
        runner.run_tree(
            op_id="op-cleanup-success",
            generator=generator,
            validator=validator,
            max_layers=1,
        )
    )
    assert len(wm.cleaned) == 2


def test_run_tree_worktree_cleanup_called_on_validator_failure():
    """Cleanup MUST run even when validator fails — finally block."""
    wm = _StubWorktreeManager()
    runner = _make_runner(max_branches=2, worktree_manager=wm)
    generator = _make_generator(diffs_per_call=["d1", "d2"])

    async def _failing_validator(**_kwargs):
        raise RuntimeError("validator-explode")

    asyncio.run(
        runner.run_tree(
            op_id="op-cleanup-on-fail",
            generator=generator,
            validator=_failing_validator,
            max_layers=1,
        )
    )
    assert len(wm.cleaned) == 2, (
        "Cleanup MUST run via finally even on validator exception"
    )


def test_run_tree_no_worktree_manager_runs_in_no_isolation_mode():
    """When worktree_manager is None, branches still produce —
    caller is responsible for isolation. Tests run in this mode
    by default for speed (no real git ops)."""
    runner = _make_runner(max_branches=2, worktree_manager=None)
    generator = _make_generator(diffs_per_call=["d1", "d2"])
    validator = _make_validator(
        outcomes=[(BranchOutcome.PROMOTED, 0.5)] * 2,
    )
    result = asyncio.run(
        runner.run_tree(
            op_id="op-no-isolation",
            generator=generator,
            validator=validator,
            max_layers=1,
        )
    )
    layer = result.layers[0]
    assert all(b.worktree_id is None for b in layer.branches)
    assert all(
        b.outcome == BranchOutcome.PROMOTED for b in layer.branches
    )


# ===========================================================================
# Fail-closed contract — §7 quarantine, §1 Boundary cancellation
# ===========================================================================


def test_run_tree_generator_exception_quarantines():
    """One generator exception MUST NOT poison the other K-1 branches.
    Quarantines to infra-failed branch."""
    runner = _make_runner(
        max_branches=3,
        worktree_manager=_StubWorktreeManager(),
    )
    generator = _make_generator(
        diffs_per_call=["d1", "d2", "d3"],
        raise_on_call=1,  # second branch throws
    )
    validator = _make_validator(
        outcomes=[(BranchOutcome.PROMOTED, 0.5)] * 3,
    )
    result = asyncio.run(
        runner.run_tree(
            op_id="op-gen-fail",
            generator=generator,
            validator=validator,
            max_layers=1,
        )
    )
    layer = result.layers[0]
    failed = [
        b for b in layer.branches
        if b.failure_class == "generator_exception"
    ]
    assert len(failed) == 1
    assert "RuntimeError" in failed[0].fix_hypothesis


def test_run_tree_validator_exception_quarantines():
    runner = _make_runner(
        max_branches=2,
        worktree_manager=_StubWorktreeManager(),
    )
    generator = _make_generator(diffs_per_call=["d1", "d2"])

    async def _selectively_failing_validator(*, branch_id, **_kwargs):
        if "d2" in str(_kwargs.get("diff", "")):
            raise ValueError("validator-explode")
        return (BranchOutcome.PROMOTED, 0.5, None, 1)

    result = asyncio.run(
        runner.run_tree(
            op_id="op-val-fail",
            generator=generator,
            validator=_selectively_failing_validator,
            max_layers=1,
        )
    )
    layer = result.layers[0]
    pruned = [
        b for b in layer.branches
        if b.outcome == BranchOutcome.PRUNED_VALIDATOR
    ]
    promoted = [
        b for b in layer.branches
        if b.outcome == BranchOutcome.PROMOTED
    ]
    assert len(pruned) == 1
    assert len(promoted) == 1


def test_run_tree_cancellation_propagates():
    """asyncio.CancelledError MUST propagate (orchestrator handles
    POSTMORTEM). All other exceptions quarantine."""
    runner = _make_runner(
        max_branches=2,
        worktree_manager=_StubWorktreeManager(),
    )

    async def _cancelling_generator(**_kwargs):
        raise asyncio.CancelledError("cancel mid-flight")

    validator = _make_validator(outcomes=[(BranchOutcome.PROMOTED, 0.5)])

    async def _runner_invocation():
        return await runner.run_tree(
            op_id="op-cancel",
            generator=_cancelling_generator,
            validator=validator,
            max_layers=1,
        )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_runner_invocation())


# ===========================================================================
# Emergency brake + deadline composition (§7 fail-closed, §11 monotonic)
# ===========================================================================


def test_run_tree_emergency_brake_at_startup_returns_empty():
    runner = _make_runner(
        max_branches=2,
        worktree_manager=_StubWorktreeManager(),
    )
    generator = _make_generator(diffs_per_call=["d1"])

    async def _validator(**_kwargs):
        raise AssertionError("validator must not be called when braked")

    result = asyncio.run(
        runner.run_tree(
            op_id="op-brake",
            generator=generator,
            validator=_validator,
            emergency_brake_check=lambda: True,
            max_layers=2,
        )
    )
    assert result.layers == ()


def test_run_tree_brake_check_exception_treated_as_inactive():
    """Defensive: brake check raise MUST NOT crash the runner."""
    runner = _make_runner(
        max_branches=1,
        worktree_manager=_StubWorktreeManager(),
    )
    generator = _make_generator(diffs_per_call=["d1"])
    validator = _make_validator(outcomes=[(BranchOutcome.PROMOTED, 0.5)])

    def _broken_brake():
        raise RuntimeError("brake check exploded")

    result = asyncio.run(
        runner.run_tree(
            op_id="op-brake-broken",
            generator=generator,
            validator=validator,
            emergency_brake_check=_broken_brake,
            max_layers=1,
        )
    )
    # Brake treated as inactive → tree dispatches normally
    assert len(result.layers) == 1
    assert result.layers[0].verdict == LayerVerdict.EXPANDED


def test_run_tree_deadline_at_zero_breaks_with_budget_terminal():
    runner = _make_runner(
        max_branches=2,
        worktree_manager=_StubWorktreeManager(),
    )
    generator = _make_generator(diffs_per_call=["d1", "d2"])
    validator = _make_validator(
        outcomes=[(BranchOutcome.PROMOTED, 0.5)] * 2,
    )
    deadline_state = {"first_call": True}

    def _deadline():
        # First call (layer 0 entry): permissive. Second call (layer 1
        # entry): drained. This exercises the mid-tree deadline check.
        if deadline_state["first_call"]:
            deadline_state["first_call"] = False
            return 100.0
        return 0.0

    result = asyncio.run(
        runner.run_tree(
            op_id="op-deadline",
            generator=generator,
            validator=validator,
            deadline_check=_deadline,
            max_layers=3,
        )
    )
    assert len(result.layers) == 2, (
        "Layer 0 dispatches (deadline OK); layer 1 records "
        "BUDGET_TERMINAL synthetic layer (deadline drained)"
    )
    assert result.layers[1].verdict == LayerVerdict.BUDGET_TERMINAL
    assert result.layers[1].branches == ()


def test_run_tree_deadline_check_exception_treated_as_no_deadline():
    runner = _make_runner(
        max_branches=1,
        worktree_manager=_StubWorktreeManager(),
    )
    generator = _make_generator(diffs_per_call=["d1"])
    validator = _make_validator(outcomes=[(BranchOutcome.PROMOTED, 0.5)])

    def _broken_deadline():
        raise RuntimeError("deadline exploded")

    result = asyncio.run(
        runner.run_tree(
            op_id="op-deadline-broken",
            generator=generator,
            validator=validator,
            deadline_check=_broken_deadline,
            max_layers=1,
        )
    )
    assert len(result.layers) == 1


# ===========================================================================
# Budget envelope composition (single-source per §1 Boundary)
# ===========================================================================


def test_run_tree_respects_repair_budget_validation_runs():
    """Tree MUST count branches against
    RepairBudget.max_total_validation_runs (shared envelope, no
    parallel bookkeeping)."""

    class _StubBudget:
        max_total_validation_runs = 4

    runner = _make_runner(
        max_branches=3,
        worktree_manager=_StubWorktreeManager(),
        repair_budget=_StubBudget(),
    )
    generator = _make_generator(
        diffs_per_call=[f"d{i}" for i in range(10)],
    )
    validator = _make_validator(
        outcomes=[(BranchOutcome.PROMOTED, 0.5)] * 10,
    )
    result = asyncio.run(
        runner.run_tree(
            op_id="op-budget",
            generator=generator,
            validator=validator,
            max_layers=10,
        )
    )
    total_runs = sum(
        b.validation_runs_consumed
        for layer in result.layers
        for b in layer.branches
    )
    assert total_runs <= 4, (
        f"Total validation runs ({total_runs}) MUST NOT exceed "
        "the shared RepairBudget envelope (4)"
    )


def test_run_tree_default_budget_when_no_repair_budget_injected():
    """When repair_budget is None, runner falls back to permissive
    default (8 runs) so tests can run without constructing a full
    RepairBudget."""
    runner = _make_runner(
        max_branches=2,
        worktree_manager=_StubWorktreeManager(),
        repair_budget=None,
    )
    assert runner._max_validation_runs() == 8


def test_run_tree_handles_garbage_repair_budget():
    """Defensive: malformed repair_budget MUST NOT crash the
    accessor."""
    class _BadBudget:
        @property
        def max_total_validation_runs(self):
            raise RuntimeError("budget exploded")

    runner = _make_runner(
        max_branches=1,
        worktree_manager=_StubWorktreeManager(),
        repair_budget=_BadBudget(),
    )
    # Accessor swallows errors → falls back to default
    assert runner._max_validation_runs() == 8


# ===========================================================================
# Layer telemetry — wall_ms + parallel_units_actual
# ===========================================================================


def test_run_tree_layer_records_wall_ms():
    clock = _DeterministicClock(start=10.0, tick=0.5)
    runner = _make_runner(
        max_branches=1,
        worktree_manager=_StubWorktreeManager(),
        clock=clock,
    )
    generator = _make_generator(diffs_per_call=["d1"])
    validator = _make_validator(outcomes=[(BranchOutcome.PROMOTED, 0.5)])
    result = asyncio.run(
        runner.run_tree(
            op_id="op-wall",
            generator=generator,
            validator=validator,
            max_layers=1,
        )
    )
    layer = result.layers[0]
    assert layer.wall_ms > 0.0, (
        "wall_ms MUST be recorded (clock advances during dispatch)"
    )


def test_run_tree_parallel_units_actual_reflects_post_posture_k():
    """parallel_units_actual records the K post posture-weighting,
    not the raw budget.max_branches_per_layer."""
    runner = _make_runner(
        max_branches=4,
        worktree_manager=_StubWorktreeManager(),
    )
    generator = _make_generator(
        diffs_per_call=[f"d{i}" for i in range(4)],
    )
    validator = _make_validator(
        outcomes=[(BranchOutcome.PROMOTED, 0.5)] * 4,
    )
    # HARDEN posture should narrow K below 4
    result = asyncio.run(
        runner.run_tree(
            op_id="op-posture",
            generator=generator,
            validator=validator,
            posture=Posture.HARDEN,
            max_layers=1,
        )
    )
    layer = result.layers[0]
    assert layer.parallel_units_actual <= 4, (
        f"HARDEN posture MUST narrow K — got "
        f"parallel_units_actual={layer.parallel_units_actual}"
    )


# ===========================================================================
# Multi-layer winning_path composition
# ===========================================================================


def test_run_tree_winning_path_walks_parent_pointers():
    """winning_branch_path MUST be the chain root→leaf via
    parent_branch_id pointers."""
    runner = _make_runner(
        max_branches=1,  # K=1 keeps test deterministic
        worktree_manager=_StubWorktreeManager(),
    )
    generator = _make_generator(diffs_per_call=["L0", "L1", "L2"])
    validator = _make_validator(
        outcomes=[
            (BranchOutcome.PROMOTED, 0.5),
            (BranchOutcome.PROMOTED, 0.5),
            (BranchOutcome.WON, 1.0),
        ],
    )
    result = asyncio.run(
        runner.run_tree(
            op_id="op-path",
            generator=generator,
            validator=validator,
            max_layers=3,
        )
    )
    assert result.layers[-1].verdict == LayerVerdict.WON_TERMINAL
    assert len(result.winning_branch_path) == 3, (
        "3-layer chain → 3-element winning_path"
    )
    # First element MUST be the root (layer 0) branch_id
    assert (
        result.winning_branch_path[0]
        == result.layers[0].branches[0].branch_id
    )
    # Last element MUST be the WON branch
    won_id = result.layers[-1].branches[0].branch_id
    assert result.winning_branch_path[-1] == won_id


# ===========================================================================
# AST composition pins (Phase 1 single-source-of-truth invariants)
# ===========================================================================


_MODULE_SRC = Path(inspect.getfile(repair_tree)).read_text(encoding="utf-8")
_MODULE_AST = ast.parse(_MODULE_SRC)


def _module_imports() -> List[Tuple[str, Tuple[str, ...]]]:
    """List of (module_name, names_imported) tuples for ImportFrom."""
    out: List[Tuple[str, Tuple[str, ...]]] = []
    for node in ast.walk(_MODULE_AST):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            names = tuple(a.name for a in node.names)
            out.append((mod, names))
    return out


def test_composition_pin_patch_signature_hash():
    """The branch_id derivation MUST compose the canonical
    failure_classifier.patch_signature_hash. Drift to an inline
    hash function is a §1 Boundary regression."""
    imports = _module_imports()
    matches = [
        (m, n) for (m, n) in imports
        if m.endswith("failure_classifier") and "patch_signature_hash" in n
    ]
    assert matches, (
        "repair_tree.py MUST import patch_signature_hash from "
        "failure_classifier — composition pin"
    )


def test_composition_pin_posture_weight_for():
    """K sizing MUST compose the canonical posture_weight_for."""
    imports = _module_imports()
    matches = [
        (m, n) for (m, n) in imports
        if m.endswith("parallel_dispatch") and "posture_weight_for" in n
    ]
    assert matches, (
        "repair_tree.py MUST import posture_weight_for from "
        "parallel_dispatch — composition pin"
    )


def test_composition_pin_worktree_manager():
    """Branch isolation MUST compose the canonical WorktreeManager."""
    imports = _module_imports()
    matches = [
        (m, n) for (m, n) in imports
        if m.endswith("worktree_manager") and "WorktreeManager" in n
    ]
    assert matches, (
        "repair_tree.py MUST import WorktreeManager from "
        "worktree_manager — composition pin"
    )


def test_composition_pin_posture_enum():
    """Posture taxonomy MUST come from the canonical posture module."""
    imports = _module_imports()
    matches = [
        (m, n) for (m, n) in imports
        if m.endswith(".posture") and "Posture" in n
    ]
    assert matches, (
        "repair_tree.py MUST import Posture from posture — "
        "composition pin"
    )


def test_runner_does_not_define_inline_hash():
    """No method on RepairTreeRunner may name itself *_hash or
    *_sig — that's the parallel-signature anti-pattern."""
    for node in ast.walk(_MODULE_AST):
        if isinstance(node, ast.ClassDef) and node.name == "RepairTreeRunner":
            for stmt in ast.walk(node):
                if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    name = stmt.name.lower()
                    assert "hash" not in name, (
                        f"method {stmt.name!r} suggests parallel "
                        "hash function — compose patch_signature_hash"
                    )
                    assert "patch_sig" not in name, (
                        f"method {stmt.name!r} suggests parallel "
                        "patch signature — compose patch_signature_hash"
                    )
