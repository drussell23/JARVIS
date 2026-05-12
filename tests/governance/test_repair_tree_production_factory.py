"""Treefinement Production Wiring Phase D — factory + adapter +
Phase 5 gate integration spine.

Pins:

* production_tree_runner_factory composition correctness — returns
  a zero-arg async closure capturing WorktreeManager +
  GitApplyDiffApplier + CanonicalBranchValidator (composed with
  TestRunner + SemanticGuardian) + ProductionBranchGenerator
* Factory raises ValueError (NOT silently degrades) when ctx.repo_root
  is missing AND test_runner/worktree_manager not injected — gate's
  stage-1 try/except catches cleanly
* tree_result_to_repair_result deterministic mapping over closed
  LayerVerdict × BranchOutcome taxonomies (WON / EXHAUSTED /
  BUDGET_TERMINAL / empty layers / unexpected verdict)
* Adapter NEVER raises — degraded inputs produce structured
  treefinement_adapter_failed:<reason>
* RepairIterationRecord synthesis preserves operator-visible
  telemetry shape (1-based iteration index from layer_index,
  outcome mapping, patch_signature_hash from branch.branch_id)
* Phase 5 gate integration — _invoke_tree_factory calls factory,
  awaits closure, adapts result, returns RepairResult. Failures
  at any of the 3 stages return None → gate falls through to
  LINEAR _run_inner byte-identically.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Tuple

import pytest

from backend.core.ouroboros.governance.repair_engine import (
    RepairBudget,
    RepairEngine,
    RepairResult,
)
from backend.core.ouroboros.governance.repair_tree import (
    BranchOutcome,
    LayerVerdict,
    PruningReason,
    RepairBranch,
    RepairTreeLayer,
    RepairTreeResult,
    TreefinementBudget,
    BranchingStrategy,
    MASTER_FLAG_ENV_VAR,
    register_production_tree_runner_factory,
)
from backend.core.ouroboros.governance.repair_tree_production import (
    GitApplyDiffApplier,
    ProductionBranchGenerator,
    _TREE_OUTCOME_TO_ITERATION_OUTCOME,
    _TREE_VERDICT_TO_STOP_REASON,
    production_tree_runner_factory,
    tree_result_to_repair_result,
)


# ===========================================================================
# Test fixtures
# ===========================================================================


def _branch(
    *,
    bid: str = "test-branch-1234567890",
    outcome: BranchOutcome = BranchOutcome.PROMOTED,
    prune: Optional[PruningReason] = None,
    layer_index: int = 0,
    hypothesis: str = "stub strategy",
    diff: str = "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-x\n+y\n",
    score: float = 0.5,
    cost_usd: float = 0.001,
    runs: int = 1,
) -> RepairBranch:
    return RepairBranch(
        branch_id=bid,
        parent_branch_id=None,
        layer_index=layer_index,
        failure_class="test",
        fix_hypothesis=hypothesis,
        diff=diff,
        validator_score=score,
        outcome=outcome,
        prune_reason=prune,
        worktree_id=f"wt-{bid}",
        cost_usd=cost_usd,
        validation_runs_consumed=runs,
    )


def _layer(
    *,
    layer_index: int = 0,
    branches: Tuple[RepairBranch, ...] = (),
    verdict: LayerVerdict = LayerVerdict.EXPANDED,
    wall_ms: float = 100.0,
) -> RepairTreeLayer:
    return RepairTreeLayer(
        layer_index=layer_index,
        branches=branches,
        verdict=verdict,
        wall_ms=wall_ms,
        parallel_units_actual=len(branches),
    )


def _tree_result(
    *,
    op_id: str = "op-test",
    layers: Tuple[RepairTreeLayer, ...] = (),
    winning_path: Tuple[str, ...] = (),
) -> RepairTreeResult:
    return RepairTreeResult(
        root_op_id=op_id,
        layers=layers,
        winning_branch_path=winning_path,
        final_status=None,
    )


class _StubCtx:
    def __init__(self, *, op_id: str = "op-stub", repo_root: Any = None):
        self.op_id = op_id
        self.repo_root = repo_root
        self.generation = None


@pytest.fixture(autouse=True)
def _isolate_factory():
    """Clear any registered production factory between tests."""
    register_production_tree_runner_factory(None)
    yield
    register_production_tree_runner_factory(None)


# ===========================================================================
# Adapter — tree_result_to_repair_result mapping table
# ===========================================================================


def test_adapter_empty_layers_returns_empty_result_stop_reason():
    result = _tree_result()
    out = tree_result_to_repair_result(result, op_id="op-A")
    assert isinstance(out, RepairResult)
    assert out.terminal == "L2_STOPPED"
    assert out.stop_reason == "treefinement_empty_result"
    assert out.candidate is None
    assert out.iterations == ()


def test_adapter_won_terminal_returns_converged():
    won = _branch(
        bid="winner",
        outcome=BranchOutcome.WON,
        hypothesis="winning strategy",
        diff="--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-old\n+new\n",
    )
    result = _tree_result(
        layers=(_layer(
            branches=(won,), verdict=LayerVerdict.WON_TERMINAL,
        ),),
        winning_path=("winner",),
    )
    out = tree_result_to_repair_result(result, op_id="op-A")
    assert out.terminal == "L2_CONVERGED"
    assert out.stop_reason is None
    assert out.candidate is not None
    assert out.candidate["unified_diff"] == won.diff
    assert out.candidate["file_path"] == "foo.py"
    assert out.candidate["fix_hypothesis"] == "winning strategy"


def test_adapter_exhausted_returns_stop_reason():
    pruned = _branch(
        outcome=BranchOutcome.PRUNED_VALIDATOR,
        prune=PruningReason.WORSE_THAN_SIBLING,
    )
    result = _tree_result(
        layers=(_layer(
            branches=(pruned,), verdict=LayerVerdict.EXHAUSTED,
        ),),
    )
    out = tree_result_to_repair_result(result, op_id="op-A")
    assert out.terminal == "L2_STOPPED"
    assert out.stop_reason == "treefinement_exhausted"


def test_adapter_budget_terminal_returns_stop_reason():
    result = _tree_result(
        layers=(_layer(
            branches=(_branch(),),
            verdict=LayerVerdict.BUDGET_TERMINAL,
        ),),
    )
    out = tree_result_to_repair_result(result, op_id="op-A")
    assert out.terminal == "L2_STOPPED"
    assert out.stop_reason == "treefinement_budget_terminal"


def test_adapter_won_without_branch_quarantines_defensively():
    """Defensive: verdict=WON_TERMINAL but no branch has
    outcome=WON (taxonomy mismatch). Adapter returns structured
    failure instead of crashing."""
    result = _tree_result(
        layers=(_layer(
            branches=(_branch(outcome=BranchOutcome.PROMOTED),),
            verdict=LayerVerdict.WON_TERMINAL,
        ),),
    )
    out = tree_result_to_repair_result(result, op_id="op-A")
    assert out.terminal == "L2_STOPPED"
    assert (
        out.stop_reason
        == "treefinement_adapter_failed:won_terminal_without_branch"
    )


def test_adapter_summary_field_counts_branches_and_outcomes():
    layer = _layer(branches=(
        _branch(bid="a", outcome=BranchOutcome.PROMOTED, cost_usd=0.01),
        _branch(
            bid="b", outcome=BranchOutcome.PRUNED_VALIDATOR,
            prune=PruningReason.WORSE_THAN_SIBLING, cost_usd=0.02,
        ),
        _branch(
            bid="c", outcome=BranchOutcome.PRUNED_DUPLICATE,
            prune=PruningReason.DUPLICATE_PATCH_SIG, cost_usd=0.0,
        ),
    ), verdict=LayerVerdict.EXPANDED)
    result = _tree_result(layers=(layer,))
    out = tree_result_to_repair_result(result, op_id="op-A")
    summary = out.summary
    assert summary["treefinement"] is True
    assert summary["layer_count"] == 1
    assert summary["branch_count"] == 3
    assert summary["won_count"] == 0
    assert summary["promoted_count"] == 1
    assert summary["pruned_count"] == 2
    assert abs(summary["total_cost_usd"] - 0.03) < 1e-9


def test_adapter_iterations_synthesized_per_branch():
    """One RepairIterationRecord synthesized per branch across all
    layers. iteration index is 1-based from layer_index."""
    layer0 = _layer(
        layer_index=0,
        branches=(_branch(bid="L0-1", layer_index=0),),
    )
    layer1 = _layer(
        layer_index=1,
        branches=(
            _branch(bid="L1-1", layer_index=1),
            _branch(bid="L1-2", layer_index=1),
        ),
    )
    result = _tree_result(layers=(layer0, layer1))
    out = tree_result_to_repair_result(result, op_id="op-A")
    assert len(out.iterations) == 3
    # 1-based iteration: layer 0 → iter 1; layer 1 → iter 2
    iters_by_iteration = [r.iteration for r in out.iterations]
    assert sorted(iters_by_iteration) == [1, 2, 2]
    # patch_signature_hash composes branch_id
    sigs = {r.patch_signature_hash for r in out.iterations}
    assert sigs == {"L0-1", "L1-1", "L1-2"}


def test_adapter_iteration_outcome_mapping():
    """Verify BranchOutcome → iteration outcome string mapping."""
    layer = _layer(branches=(
        _branch(bid="promoted", outcome=BranchOutcome.PROMOTED),
        _branch(bid="won", outcome=BranchOutcome.WON),
        _branch(
            bid="pruned-val", outcome=BranchOutcome.PRUNED_VALIDATOR,
            prune=PruningReason.WORSE_THAN_SIBLING,
        ),
        _branch(
            bid="pruned-dup", outcome=BranchOutcome.PRUNED_DUPLICATE,
            prune=PruningReason.DUPLICATE_PATCH_SIG,
        ),
    ), verdict=LayerVerdict.EXPANDED)
    result = _tree_result(layers=(layer,))
    out = tree_result_to_repair_result(result, op_id="op-A")
    outcomes_by_sig = {
        r.patch_signature_hash: r.outcome for r in out.iterations
    }
    assert outcomes_by_sig["promoted"] == "progress"
    assert outcomes_by_sig["won"] == "converged"
    assert outcomes_by_sig["pruned-val"] == "no_progress"
    assert outcomes_by_sig["pruned-dup"] == "no_progress"


def test_adapter_prune_reason_threaded_to_stop_reason():
    """Branch with prune_reason → iteration record's stop_reason
    surfaces the taxonomy value."""
    layer = _layer(branches=(
        _branch(
            outcome=BranchOutcome.PRUNED_VALIDATOR,
            prune=PruningReason.SEMANTIC_GUARDIAN_HARD_FINDING,
        ),
    ), verdict=LayerVerdict.EXHAUSTED)
    out = tree_result_to_repair_result(
        _tree_result(layers=(layer,)), op_id="op-A",
    )
    rec = out.iterations[0]
    assert rec.stop_reason == (
        "treefinement_pruned:semantic_guardian_hard_finding"
    )


def test_adapter_never_raises_on_garbage():
    """Pass a malformed tree_result; adapter returns structured
    failure instead of propagating."""
    class _BadResult:
        @property
        def layers(self):
            raise RuntimeError("malformed")

        root_op_id = "op-x"
        winning_branch_path = ()
        final_status = None

    out = tree_result_to_repair_result(
        _BadResult(),  # type: ignore[arg-type]
        op_id="op-x",
    )
    assert out.terminal == "L2_STOPPED"
    assert out.stop_reason.startswith("treefinement_adapter_failed:")


# ===========================================================================
# Closed taxonomy mapping pins
# ===========================================================================


def test_verdict_to_stop_reason_table_completeness():
    """Mapping table MUST cover EXHAUSTED + BUDGET_TERMINAL. Adding
    a new LayerVerdict requires explicit table extension."""
    assert _TREE_VERDICT_TO_STOP_REASON == {
        "exhausted": "treefinement_exhausted",
        "budget_terminal": "treefinement_budget_terminal",
    }


def test_outcome_to_iteration_outcome_table_completeness():
    """Mapping table MUST cover all 5 BranchOutcome members."""
    expected_keys = {
        m.value for m in BranchOutcome
    }
    assert set(_TREE_OUTCOME_TO_ITERATION_OUTCOME.keys()) == expected_keys


# ===========================================================================
# Factory — composition + ValueError surfacing
# ===========================================================================


def test_factory_raises_when_repo_root_missing(tmp_path):
    """Missing repo_root + no dependency injection → ValueError.
    Gate's stage-1 try/except catches and falls through to LINEAR."""
    ctx = _StubCtx(repo_root=None)
    engine = RepairEngine(
        budget=RepairBudget(),
        prime_provider=object(),
        repo_root=Path("."),
    )
    budget = TreefinementBudget(
        enabled=True,
        branching_strategy=BranchingStrategy.BFS,
        max_branches_per_layer=2,
        beam_width=2,
        branch_dedup_enabled=True,
        cross_branch_learning_enabled=False,
        emergency_demote_threshold=0.85,
    )
    with pytest.raises(ValueError, match="ctx.repo_root required"):
        production_tree_runner_factory(
            budget=budget,
            ctx=ctx,
            repair_engine=engine,
            pipeline_deadline=datetime.now(timezone.utc),
        )


def test_factory_accepts_injected_dependencies_without_repo_root(tmp_path):
    """When deps are injected, repo_root is no longer required —
    test path."""
    ctx = _StubCtx(repo_root=None)
    engine = RepairEngine(
        budget=RepairBudget(),
        prime_provider=object(),
        repo_root=Path("."),
    )
    budget = TreefinementBudget(
        enabled=True,
        branching_strategy=BranchingStrategy.BFS,
        max_branches_per_layer=2,
        beam_width=2,
        branch_dedup_enabled=True,
        cross_branch_learning_enabled=False,
        emergency_demote_threshold=0.85,
    )

    class _StubWorktreeManager:
        async def create(self, name):
            return tmp_path / name

        async def cleanup(self, path):
            pass

    class _StubTestRunner:
        async def run(self, test_files, sandbox_dir=None):
            from backend.core.ouroboros.governance.test_runner import (
                TestResult,
            )
            return TestResult(
                passed=True, total=0, failed=0,
                failed_tests=(), duration_seconds=0.0,
                stdout="", flake_suspected=False,
            )

        async def resolve_affected_tests(self, files):
            return ()

    closure = production_tree_runner_factory(
        budget=budget,
        ctx=ctx,
        repair_engine=engine,
        pipeline_deadline=datetime.now(timezone.utc),
        worktree_manager=_StubWorktreeManager(),
        test_runner=_StubTestRunner(),
    )
    assert callable(closure)


def test_factory_closure_returns_repair_tree_result(tmp_path, monkeypatch):
    """End-to-end: factory closure produces a RepairTreeResult.
    Use stub provider so we don't depend on real Claude API."""
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    ctx = _StubCtx(repo_root=tmp_path)

    class _StubProvider:
        async def generate(self, ctx, deadline, *, repair_context):
            class _R:
                candidates = [{
                    "unified_diff": (
                        "--- a/x.py\n+++ b/x.py\n"
                        "@@ -1 +1 @@\n-x\n+y\n"
                    ),
                    "fix_hypothesis": "stub",
                }]
                model_id = "stub"
                provider_name = "stub"
            return _R()

    engine = RepairEngine(
        budget=RepairBudget(),
        prime_provider=_StubProvider(),
        repo_root=tmp_path,
    )
    budget = TreefinementBudget(
        enabled=True,
        branching_strategy=BranchingStrategy.BFS,
        max_branches_per_layer=1,
        beam_width=1,
        branch_dedup_enabled=False,
        cross_branch_learning_enabled=False,
        emergency_demote_threshold=0.85,
    )

    # Inject stubs to avoid needing a real worktree
    class _StubWM:
        async def create(self, name):
            d = tmp_path / "wt"
            d.mkdir(exist_ok=True)
            return d

        async def cleanup(self, path):
            pass

    class _StubApplier:
        async def __call__(self, *, worktree_dir, diff):
            from backend.core.ouroboros.governance.repair_tree import (
                DiffApplyResult,
            )
            return DiffApplyResult(
                files=(("x.py", "x\n", "y\n"),), error="",
            )

    class _StubTR:
        async def run(self, test_files, sandbox_dir=None):
            from backend.core.ouroboros.governance.test_runner import (
                TestResult,
            )
            return TestResult(
                passed=True, total=3, failed=0,
                failed_tests=(), duration_seconds=0.01,
                stdout="", flake_suspected=False,
            )

        async def resolve_affected_tests(self, files):
            return ()

    closure = production_tree_runner_factory(
        budget=budget,
        ctx=ctx,
        repair_engine=engine,
        pipeline_deadline=datetime.now(timezone.utc),
        worktree_manager=_StubWM(),
        diff_applier=_StubApplier(),
        test_runner=_StubTR(),
        max_layers=1,
    )
    tree_result = asyncio.run(closure())
    assert isinstance(tree_result, RepairTreeResult)
    assert tree_result.root_op_id == ctx.op_id


# ===========================================================================
# Phase 5 gate integration — _invoke_tree_factory exercises real path
# ===========================================================================


def test_gate_with_factory_returns_repair_result(tmp_path, monkeypatch):
    """End-to-end: registered factory → gate invokes → returns
    RepairResult (WON path)."""
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    monkeypatch.setenv("JARVIS_L2_BRANCHING_STRATEGY", "bfs")

    # Build a factory that returns a precomputed WON tree result
    async def _winning_closure() -> RepairTreeResult:
        won = _branch(
            outcome=BranchOutcome.WON,
            hypothesis="canonical winner",
            diff="--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-x\n+y\n",
        )
        return _tree_result(
            layers=(_layer(
                branches=(won,), verdict=LayerVerdict.WON_TERMINAL,
            ),),
            winning_path=(won.branch_id,),
        )

    def _factory(*, budget, ctx, repair_engine, pipeline_deadline,
                 posture=None):
        return _winning_closure

    register_production_tree_runner_factory(_factory)

    engine = RepairEngine(
        budget=RepairBudget(),
        prime_provider=object(),
        repo_root=tmp_path,
    )

    class _Ctx:
        op_id = "op-end-to-end"
        repo_root = str(tmp_path)
        generation = None

    out = asyncio.run(engine._maybe_run_treefinement(
        _Ctx(), None, datetime.now(timezone.utc),
    ))
    assert out is not None
    assert out.terminal == "L2_CONVERGED"
    assert out.candidate["fix_hypothesis"] == "canonical winner"


def test_gate_falls_through_when_factory_construction_raises(
    tmp_path, monkeypatch,
):
    """Factory raises during construction → gate stage-1 catches →
    return None → caller falls through to LINEAR."""
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    monkeypatch.setenv("JARVIS_L2_BRANCHING_STRATEGY", "bfs")

    def _broken_factory(**_kwargs):
        raise RuntimeError("factory broke")

    register_production_tree_runner_factory(_broken_factory)

    engine = RepairEngine(
        budget=RepairBudget(),
        prime_provider=object(),
        repo_root=tmp_path,
    )

    class _Ctx:
        op_id = "op-broken-factory"
        repo_root = str(tmp_path)
        generation = None

    out = asyncio.run(engine._maybe_run_treefinement(
        _Ctx(), None, datetime.now(timezone.utc),
    ))
    # None = fall through to LINEAR _run_inner
    assert out is None


def test_gate_falls_through_when_closure_raises(tmp_path, monkeypatch):
    """Closure raises during await → gate stage-2 catches →
    return None → fall through."""
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    monkeypatch.setenv("JARVIS_L2_BRANCHING_STRATEGY", "bfs")

    async def _broken_closure():
        raise RuntimeError("invocation broke")

    def _factory(**_kwargs):
        return _broken_closure

    register_production_tree_runner_factory(_factory)

    engine = RepairEngine(
        budget=RepairBudget(),
        prime_provider=object(),
        repo_root=tmp_path,
    )

    class _Ctx:
        op_id = "op-broken-closure"
        repo_root = str(tmp_path)
        generation = None

    out = asyncio.run(engine._maybe_run_treefinement(
        _Ctx(), None, datetime.now(timezone.utc),
    ))
    assert out is None


def test_gate_cancellation_propagates(tmp_path, monkeypatch):
    """CancelledError MUST propagate through the gate (orchestrator
    POSTMORTEM contract). Only non-cancel errors fall through."""
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    monkeypatch.setenv("JARVIS_L2_BRANCHING_STRATEGY", "bfs")

    async def _cancel_closure():
        raise asyncio.CancelledError()

    def _factory(**_kwargs):
        return _cancel_closure

    register_production_tree_runner_factory(_factory)

    engine = RepairEngine(
        budget=RepairBudget(),
        prime_provider=object(),
        repo_root=tmp_path,
    )

    class _Ctx:
        op_id = "op-cancel"
        repo_root = str(tmp_path)
        generation = None

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(engine._maybe_run_treefinement(
            _Ctx(), None, datetime.now(timezone.utc),
        ))


def test_gate_with_adapter_returning_stopped_result(tmp_path, monkeypatch):
    """Factory returns EXHAUSTED tree → gate adapts to RepairResult
    L2_STOPPED with treefinement_exhausted stop_reason. The gate
    DOES return this result (not None) — only None means
    'fall through to LINEAR'. A treefinement-stopped result is the
    legitimate gate output."""
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    monkeypatch.setenv("JARVIS_L2_BRANCHING_STRATEGY", "bfs")

    async def _exhausted_closure():
        return _tree_result(
            layers=(_layer(
                branches=(_branch(
                    outcome=BranchOutcome.PRUNED_VALIDATOR,
                    prune=PruningReason.WORSE_THAN_SIBLING,
                ),),
                verdict=LayerVerdict.EXHAUSTED,
            ),),
        )

    register_production_tree_runner_factory(
        lambda **_kw: _exhausted_closure,
    )

    engine = RepairEngine(
        budget=RepairBudget(),
        prime_provider=object(),
        repo_root=tmp_path,
    )

    class _Ctx:
        op_id = "op-exhausted"
        repo_root = str(tmp_path)
        generation = None

    out = asyncio.run(engine._maybe_run_treefinement(
        _Ctx(), None, datetime.now(timezone.utc),
    ))
    assert out is not None
    assert out.terminal == "L2_STOPPED"
    assert out.stop_reason == "treefinement_exhausted"
