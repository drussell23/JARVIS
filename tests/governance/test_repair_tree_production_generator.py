"""Treefinement Production Wiring Phase C — ProductionBranchGenerator spine.

Pins the production BranchGenerator Protocol implementation that
composes Phase A's _generate_repair_candidate primitive + Phase 3's
maybe_inject_sibling_outcomes substrate.

Invariants covered
------------------
* Implements the BranchGenerator Protocol (runtime isinstance pass)
* Composes _generate_repair_candidate (single-source primitive)
* Layer 0 / no parent / no siblings → minimal context, no enrichment
* Layer N+1 / with parent → hypothesis_seed threaded; cross-branch
  block built via Phase 3 (when master flag on + posture allows)
* Provider exception → quarantine to ('', 'generation_failed:...', 0.0)
* Empty candidates → quarantine to ('', 'generation_failed:empty_candidates', 0.0)
* unified_diff candidate → returned as-is
* full_content-only candidate → ('', 'candidate_full_content_only_unsupported_phase_c', cost)
  (documented Phase D follow-on)
* Cross-branch block injection respects Phase 3 master flag + posture
  skip-list + layer-0 short-circuit
* Hypothesis derivation from parent_branch.fix_hypothesis ('extends[parent]: child')
* CancelledError propagates (orchestrator POSTMORTEM contract)
* Context-builder failure → quarantine (NEVER raises into runner)
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Tuple

import pytest

from backend.core.ouroboros.governance.op_context import RepairContext
from backend.core.ouroboros.governance.repair_engine import (
    CandidateGenerationResult,
    RepairBudget,
    RepairEngine,
)
from backend.core.ouroboros.governance.repair_tree import (
    BranchGenerator,
    BranchOutcome,
    CrossBranchLearningConfig,
    Posture,
    PruningReason,
    RepairBranch,
)
from backend.core.ouroboros.governance.repair_tree_production import (
    PER_CALL_COST_USD_ENV_VAR,
    ProductionBranchGenerator,
    _AugmentedRepairContext,
)


# ===========================================================================
# Test fixtures
# ===========================================================================


class _RecordingProvider:
    """Captures _generate_repair_candidate's repair_context + seed."""

    def __init__(
        self,
        *,
        result: Optional[Any] = None,
        raises: Optional[BaseException] = None,
    ):
        self.result = result
        self.raises = raises
        self.calls: list = []

    async def generate(self, ctx, pipeline_deadline, *, repair_context):
        self.calls.append({
            "ctx": ctx,
            "pipeline_deadline": pipeline_deadline,
            "repair_context": repair_context,
        })
        if self.raises is not None:
            raise self.raises
        return self.result


class _StubGenResult:
    def __init__(self, *, candidates, model_id="m", provider_name="p"):
        self.candidates = candidates
        self.model_id = model_id
        self.provider_name = provider_name


def _make_branch(*, bid: str, hypothesis: str = "rename foo",
                 outcome: BranchOutcome = BranchOutcome.PROMOTED,
                 layer_index: int = 0,
                 score: float = 0.5) -> RepairBranch:
    return RepairBranch(
        branch_id=bid, parent_branch_id=None,
        layer_index=layer_index, failure_class="test",
        fix_hypothesis=hypothesis, diff="--- a\n+++ b\n",
        validator_score=score, outcome=outcome,
        prune_reason=None, worktree_id=None,
        cost_usd=0.0, validation_runs_consumed=1,
    )


def _make_engine(provider: _RecordingProvider) -> RepairEngine:
    return RepairEngine(
        budget=RepairBudget(),
        prime_provider=provider,
        repo_root=Path("."),
    )


def _make_generator(
    *,
    provider: Optional[_RecordingProvider] = None,
    cross_branch_config: Optional[CrossBranchLearningConfig] = None,
    posture: Optional[Posture] = None,
    cost: Optional[float] = None,
    repair_context_builder: Any = None,
) -> ProductionBranchGenerator:
    if provider is None:
        provider = _RecordingProvider(
            result=_StubGenResult(
                candidates=[{"unified_diff": "--- a\n+++ b\n@@ -1 +1 @@\n-x\n+y\n",
                             "fix_hypothesis": "stub-hypothesis"}],
            ),
        )
    engine = _make_engine(provider)
    return ProductionBranchGenerator(
        repair_engine=engine,
        ctx=object(),
        pipeline_deadline=datetime.now(timezone.utc),
        cross_branch_config=cross_branch_config,
        posture=posture,
        cost_per_call_usd=cost,
        repair_context_builder=repair_context_builder,
    )


def _invoke(generator, *, op_id="op-test", layer_index=0,
            parent_branch=None, sibling_outcomes=()) -> Tuple[str, str, float]:
    return asyncio.run(generator(
        op_id=op_id, layer_index=layer_index,
        parent_branch=parent_branch, sibling_outcomes=sibling_outcomes,
    ))


# ===========================================================================
# Protocol conformance (runtime isinstance check)
# ===========================================================================


def test_generator_implements_branch_generator_protocol():
    gen = _make_generator()
    assert isinstance(gen, BranchGenerator)


# ===========================================================================
# Layer 0 / no parent / no siblings — happy path
# ===========================================================================


def test_layer_0_returns_diff_with_default_hypothesis():
    """Layer 0 with default candidate (no fix_hypothesis field) →
    derives hypothesis from rationale/intent fallback to
    'l2_repair_layer_0'."""
    provider = _RecordingProvider(
        result=_StubGenResult(
            candidates=[{"unified_diff": "--- a\n+++ b\n@@ -1 +1 @@\n-a\n+b\n"}],
        ),
    )
    gen = _make_generator(provider=provider)
    diff, hypothesis, cost = _invoke(gen, layer_index=0)
    assert diff == "--- a\n+++ b\n@@ -1 +1 @@\n-a\n+b\n"
    assert hypothesis == "l2_repair_layer_0"
    assert cost > 0.0


def test_candidate_fix_hypothesis_field_used():
    """Provider candidate's fix_hypothesis takes precedence over default."""
    provider = _RecordingProvider(
        result=_StubGenResult(
            candidates=[{
                "unified_diff": "--- a\n+++ b\n@@ -1 +1 @@\n-x\n+y\n",
                "fix_hypothesis": "rename foo to bar in helper.py",
            }],
        ),
    )
    gen = _make_generator(provider=provider)
    _, hypothesis, _ = _invoke(gen, layer_index=0)
    assert hypothesis == "rename foo to bar in helper.py"


def test_candidate_rationale_field_used_as_fallback():
    """When fix_hypothesis missing, rationale takes over."""
    provider = _RecordingProvider(
        result=_StubGenResult(
            candidates=[{
                "unified_diff": "--- a\n+++ b\n@@ -1 +1 @@\n-x\n+y\n",
                "rationale": "stub rationale",
            }],
        ),
    )
    gen = _make_generator(provider=provider)
    _, hypothesis, _ = _invoke(gen, layer_index=0)
    assert hypothesis == "stub rationale"


def test_candidate_intent_field_used_as_third_fallback():
    """When fix_hypothesis + rationale both missing, intent used."""
    provider = _RecordingProvider(
        result=_StubGenResult(
            candidates=[{
                "unified_diff": "--- a\n+++ b\n@@ -1 +1 @@\n-x\n+y\n",
                "intent": "fix the test",
            }],
        ),
    )
    gen = _make_generator(provider=provider)
    _, hypothesis, _ = _invoke(gen, layer_index=0)
    assert hypothesis == "fix the test"


# ===========================================================================
# Cost reporting
# ===========================================================================


def test_cost_reported_from_constructor_param():
    gen = _make_generator(cost=0.123)
    _, _, cost = _invoke(gen)
    assert cost == 0.123


def test_cost_reported_from_env(monkeypatch):
    monkeypatch.setenv(PER_CALL_COST_USD_ENV_VAR, "0.05")
    gen = _make_generator()  # no explicit cost
    _, _, cost = _invoke(gen)
    assert cost == 0.05


def test_cost_zero_on_failure_path():
    """Provider exception → cost=0.0 (no provider call to charge)."""
    provider = _RecordingProvider(raises=RuntimeError("provider broke"))
    gen = _make_generator(provider=provider)
    _, _, cost = _invoke(gen)
    assert cost == 0.0


# ===========================================================================
# Failure quarantine — provider exception, empty candidates, etc.
# ===========================================================================


def test_provider_exception_quarantines():
    provider = _RecordingProvider(raises=RuntimeError("provider broke"))
    gen = _make_generator(provider=provider)
    diff, hypothesis, cost = _invoke(gen)
    assert diff == ""
    assert hypothesis == "generation_failed:generate_error:RuntimeError"
    assert cost == 0.0


def test_empty_candidates_quarantines():
    provider = _RecordingProvider(
        result=_StubGenResult(candidates=[]),
    )
    gen = _make_generator(provider=provider)
    diff, hypothesis, _ = _invoke(gen)
    assert diff == ""
    assert hypothesis == "generation_failed:empty_candidates"


def test_full_content_only_quarantines_with_documented_marker():
    """Phase C limitation — full_content without unified_diff returns
    structured quarantine. Phase D may add a synthesizer."""
    provider = _RecordingProvider(
        result=_StubGenResult(
            candidates=[{
                "full_content": "x = 2\n",
                "file_path": "foo.py",
            }],
        ),
    )
    gen = _make_generator(provider=provider)
    diff, hypothesis, cost = _invoke(gen)
    assert diff == ""
    assert hypothesis == "candidate_full_content_only_unsupported_phase_c"
    assert cost > 0.0  # provider call DID succeed; we just can't use the shape


def test_no_content_candidate_quarantines():
    """Neither unified_diff nor full_content present."""
    provider = _RecordingProvider(
        result=_StubGenResult(
            candidates=[{"file_path": "foo.py"}],  # neither field
        ),
    )
    gen = _make_generator(provider=provider)
    diff, hypothesis, cost = _invoke(gen)
    assert diff == ""
    assert hypothesis == "candidate_no_content"
    assert cost == 0.0


def test_context_builder_exception_quarantines():
    """Custom builder that raises → generator quarantines, NEVER
    reaches provider."""
    def _broken_builder(*_args, **_kwargs):
        raise ValueError("builder exploded")

    provider = _RecordingProvider(
        result=_StubGenResult(
            candidates=[{"unified_diff": "--- a\n+++ b\n"}],
        ),
    )
    gen = _make_generator(
        provider=provider, repair_context_builder=_broken_builder,
    )
    diff, hypothesis, _ = _invoke(gen)
    assert diff == ""
    assert hypothesis.startswith("generation_failed:context_builder:")
    assert "ValueError" in hypothesis
    # Provider never called
    assert provider.calls == []


# ===========================================================================
# Cancellation propagation
# ===========================================================================


def test_cancellation_from_provider_propagates():
    provider = _RecordingProvider(raises=asyncio.CancelledError())
    gen = _make_generator(provider=provider)
    with pytest.raises(asyncio.CancelledError):
        _invoke(gen)


def test_cancellation_from_context_builder_propagates():
    def _cancelling_builder(*_args, **_kwargs):
        raise asyncio.CancelledError()
    gen = _make_generator(repair_context_builder=_cancelling_builder)
    with pytest.raises(asyncio.CancelledError):
        _invoke(gen)


# ===========================================================================
# Cross-branch threading — layer N+1 / parent / siblings
# ===========================================================================


def test_layer_n_plus_1_threads_hypothesis_seed_from_parent(monkeypatch):
    """Parent branch's fix_hypothesis becomes hypothesis_seed; the
    Phase A primitive captures it (Phase A explicitly del's it but
    the call site MUST pass it)."""
    monkeypatch.setenv("JARVIS_L2_CROSS_BRANCH_LEARNING_ENABLED", "true")
    captures = []

    async def _capturing_primitive(
        self, ctx, pipeline_deadline, *,
        repair_context, hypothesis_seed=None,
    ):
        captures.append({
            "repair_context": repair_context,
            "hypothesis_seed": hypothesis_seed,
        })
        return CandidateGenerationResult(
            candidate={"unified_diff": "--- a\n+++ b\n"},
            model_id="m", provider_name="p", stop_reason=None,
        )

    monkeypatch.setattr(
        RepairEngine, "_generate_repair_candidate",
        _capturing_primitive,
    )

    parent = _make_branch(
        bid="parent-1",
        hypothesis="extract helper function from inline block",
    )
    gen = _make_generator()
    _invoke(gen, layer_index=1, parent_branch=parent)

    assert len(captures) == 1
    assert captures[0]["hypothesis_seed"] == (
        "extract helper function from inline block"
    )


def test_no_hypothesis_seed_for_layer_0_or_no_parent(monkeypatch):
    """Layer 0 (or any layer with no parent) → hypothesis_seed=None."""
    captures = []

    async def _capturing_primitive(
        self, ctx, pipeline_deadline, *,
        repair_context, hypothesis_seed=None,
    ):
        captures.append(hypothesis_seed)
        return CandidateGenerationResult(
            candidate={"unified_diff": "--- a\n+++ b\n"},
            model_id="m", provider_name="p", stop_reason=None,
        )

    monkeypatch.setattr(
        RepairEngine, "_generate_repair_candidate",
        _capturing_primitive,
    )

    gen = _make_generator()
    _invoke(gen, layer_index=0)
    _invoke(gen, layer_index=2, parent_branch=None)
    assert captures == [None, None]


def test_empty_parent_hypothesis_yields_none_seed(monkeypatch):
    """Parent with whitespace-only / empty hypothesis → seed=None."""
    captures = []

    async def _capture(
        self, ctx, pipeline_deadline, *,
        repair_context, hypothesis_seed=None,
    ):
        captures.append(hypothesis_seed)
        return CandidateGenerationResult(
            candidate={"unified_diff": "--- a\n+++ b\n"},
            model_id="m", provider_name="p", stop_reason=None,
        )

    monkeypatch.setattr(
        RepairEngine, "_generate_repair_candidate", _capture,
    )

    parent = _make_branch(bid="p", hypothesis="   \n\t  ")
    gen = _make_generator()
    _invoke(gen, layer_index=1, parent_branch=parent)
    assert captures[0] is None


def test_parent_hypothesis_extends_in_derived_hypothesis(monkeypatch):
    """When a parent exists with non-empty hypothesis, the candidate's
    hypothesis is wrapped with 'extends[parent]: child'."""
    monkeypatch.setenv("JARVIS_L2_CROSS_BRANCH_LEARNING_ENABLED", "true")
    provider = _RecordingProvider(
        result=_StubGenResult(
            candidates=[{
                "unified_diff": "--- a\n+++ b\n@@ -1 +1 @@\n-x\n+y\n",
                "fix_hypothesis": "child strategy",
            }],
        ),
    )
    parent = _make_branch(bid="p", hypothesis="parent strategy")
    gen = _make_generator(provider=provider)
    _, hypothesis, _ = _invoke(
        gen, layer_index=1, parent_branch=parent,
    )
    assert hypothesis == "extends[parent strategy]: child strategy"


def test_long_parent_hypothesis_truncated_in_extends_format(monkeypatch):
    monkeypatch.setenv("JARVIS_L2_CROSS_BRANCH_LEARNING_ENABLED", "true")
    long_parent_hyp = "x" * 200
    provider = _RecordingProvider(
        result=_StubGenResult(
            candidates=[{
                "unified_diff": "--- a\n+++ b\n@@ -1 +1 @@\n-x\n+y\n",
                "fix_hypothesis": "short child",
            }],
        ),
    )
    parent = _make_branch(bid="p", hypothesis=long_parent_hyp)
    gen = _make_generator(provider=provider)
    _, hypothesis, _ = _invoke(
        gen, layer_index=1, parent_branch=parent,
    )
    # Truncated to ~80 chars + ellipsis
    assert "..." in hypothesis
    assert hypothesis.startswith("extends[")


# ===========================================================================
# Cross-branch block enrichment — composes Phase 3 substrate
# ===========================================================================


def test_cross_branch_block_built_at_layer_n_plus_1(monkeypatch):
    """Layer 1 with siblings + master flag on → augmented context
    has non-empty cross_branch_outcomes field."""
    monkeypatch.setenv("JARVIS_L2_CROSS_BRANCH_LEARNING_ENABLED", "true")
    captures = []

    async def _capture(
        self, ctx, pipeline_deadline, *,
        repair_context, hypothesis_seed=None,
    ):
        captures.append(repair_context)
        return CandidateGenerationResult(
            candidate={"unified_diff": "--- a\n+++ b\n"},
            model_id="m", provider_name="p", stop_reason=None,
        )

    monkeypatch.setattr(
        RepairEngine, "_generate_repair_candidate", _capture,
    )

    siblings = (
        _make_branch(bid="sib-A", hypothesis="strategy alpha", score=0.7),
        _make_branch(
            bid="sib-B",
            outcome=BranchOutcome.PRUNED_VALIDATOR,
            hypothesis="strategy beta", score=0.3,
        ),
    )
    parent = _make_branch(bid="p", hypothesis="parent strategy")
    gen = _make_generator()
    _invoke(
        gen, layer_index=1,
        parent_branch=parent, sibling_outcomes=siblings,
    )

    assert len(captures) == 1
    augmented = captures[0]
    assert isinstance(augmented, _AugmentedRepairContext)
    assert "Sibling Branch Outcomes" in augmented.cross_branch_outcomes
    assert "strategy alpha" in augmented.cross_branch_outcomes


def test_cross_branch_block_empty_at_layer_0(monkeypatch):
    """Layer 0 → no siblings yet → cross_branch_outcomes empty."""
    monkeypatch.setenv("JARVIS_L2_CROSS_BRANCH_LEARNING_ENABLED", "true")
    captures = []

    async def _capture(
        self, ctx, pipeline_deadline, *,
        repair_context, hypothesis_seed=None,
    ):
        captures.append(repair_context)
        return CandidateGenerationResult(
            candidate={"unified_diff": "--- a\n+++ b\n"},
            model_id="m", provider_name="p", stop_reason=None,
        )

    monkeypatch.setattr(
        RepairEngine, "_generate_repair_candidate", _capture,
    )

    gen = _make_generator()
    _invoke(gen, layer_index=0)
    assert captures[0].cross_branch_outcomes == ""


def test_cross_branch_block_empty_when_master_off(monkeypatch):
    """Master flag OFF → enrichment skipped even with siblings."""
    monkeypatch.delenv(
        "JARVIS_L2_CROSS_BRANCH_LEARNING_ENABLED", raising=False,
    )
    cfg = CrossBranchLearningConfig(
        enabled=False, max_siblings=2, max_chars=800,
        skip_postures=("MAINTAIN",),
    )
    captures = []

    async def _capture(
        self, ctx, pipeline_deadline, *,
        repair_context, hypothesis_seed=None,
    ):
        captures.append(repair_context)
        return CandidateGenerationResult(
            candidate={"unified_diff": "--- a\n+++ b\n"},
            model_id="m", provider_name="p", stop_reason=None,
        )

    monkeypatch.setattr(
        RepairEngine, "_generate_repair_candidate", _capture,
    )

    siblings = (_make_branch(bid="s", hypothesis="x", score=0.5),)
    parent = _make_branch(bid="p")
    gen = _make_generator(cross_branch_config=cfg)
    _invoke(
        gen, layer_index=1,
        parent_branch=parent, sibling_outcomes=siblings,
    )
    assert captures[0].cross_branch_outcomes == ""


def test_cross_branch_block_skipped_for_maintain_posture(monkeypatch):
    """MAINTAIN posture is in default skip_postures → enrichment off."""
    monkeypatch.setenv("JARVIS_L2_CROSS_BRANCH_LEARNING_ENABLED", "true")
    captures = []

    async def _capture(
        self, ctx, pipeline_deadline, *,
        repair_context, hypothesis_seed=None,
    ):
        captures.append(repair_context)
        return CandidateGenerationResult(
            candidate={"unified_diff": "--- a\n+++ b\n"},
            model_id="m", provider_name="p", stop_reason=None,
        )

    monkeypatch.setattr(
        RepairEngine, "_generate_repair_candidate", _capture,
    )

    siblings = (_make_branch(bid="s", hypothesis="x", score=0.7),)
    parent = _make_branch(bid="p")
    gen = _make_generator(posture=Posture.MAINTAIN)
    _invoke(
        gen, layer_index=1,
        parent_branch=parent, sibling_outcomes=siblings,
    )
    assert captures[0].cross_branch_outcomes == ""


def test_cross_branch_block_active_for_explore_posture(monkeypatch):
    monkeypatch.setenv("JARVIS_L2_CROSS_BRANCH_LEARNING_ENABLED", "true")
    captures = []

    async def _capture(
        self, ctx, pipeline_deadline, *,
        repair_context, hypothesis_seed=None,
    ):
        captures.append(repair_context)
        return CandidateGenerationResult(
            candidate={"unified_diff": "--- a\n+++ b\n"},
            model_id="m", provider_name="p", stop_reason=None,
        )

    monkeypatch.setattr(
        RepairEngine, "_generate_repair_candidate", _capture,
    )

    siblings = (
        _make_branch(bid="s", hypothesis="explore strategy", score=0.7),
    )
    parent = _make_branch(bid="p")
    gen = _make_generator(posture=Posture.EXPLORE)
    _invoke(
        gen, layer_index=1,
        parent_branch=parent, sibling_outcomes=siblings,
    )
    assert "explore strategy" in captures[0].cross_branch_outcomes


# ===========================================================================
# Augmented context preserves canonical RepairContext fields
# ===========================================================================


def test_augmented_context_preserves_canonical_fields(monkeypatch):
    """Augmented wrapper exposes ALL RepairContext fields via attribute
    access — provider's existing prompt-construction code reads them
    transparently."""
    captures = []

    async def _capture(
        self, ctx, pipeline_deadline, *,
        repair_context, hypothesis_seed=None,
    ):
        captures.append(repair_context)
        return CandidateGenerationResult(
            candidate={"unified_diff": "--- a\n+++ b\n"},
            model_id="m", provider_name="p", stop_reason=None,
        )

    monkeypatch.setattr(
        RepairEngine, "_generate_repair_candidate", _capture,
    )

    custom_ctx = RepairContext(
        iteration=3, max_iterations=5, failure_class="syntax",
        failure_signature_hash="abc", failing_tests=("t1", "t2"),
        failure_summary="error here",
        current_candidate_content="x = 1",
        current_candidate_file_path="foo.py",
    )

    def _builder(*_args, **_kwargs):
        return custom_ctx

    gen = _make_generator(repair_context_builder=_builder)
    _invoke(gen)
    augmented = captures[0]
    assert augmented.iteration == 3
    assert augmented.max_iterations == 5
    assert augmented.failure_class == "syntax"
    assert augmented.failing_tests == ("t1", "t2")
    assert augmented.failure_summary == "error here"
    assert augmented.current_candidate_content == "x = 1"
    assert augmented.current_candidate_file_path == "foo.py"
