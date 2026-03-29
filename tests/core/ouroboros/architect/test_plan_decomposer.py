"""
Tests for PlanDecomposer — deterministic plan -> IntentEnvelope conversion.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.architect.plan import (
    ArchitecturalPlan,
    PlanStep,
    StepIntentKind,
)
from backend.core.ouroboros.architect.plan_decomposer import PlanDecomposer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_plan(steps) -> ArchitecturalPlan:
    return ArchitecturalPlan.create(
        parent_hypothesis_id="hyp-test-001",
        parent_hypothesis_fingerprint="fp-test",
        title="Test Plan",
        description="Used in unit tests",
        repos_affected=("jarvis",),
        non_goals=(),
        steps=tuple(steps),
        acceptance_checks=(),
        model_used="test-model",
        snapshot_hash="snap-abc",
    )


def _step(index: int, paths=None, depends_on=()) -> PlanStep:
    return PlanStep(
        step_index=index,
        description=f"Step {index}",
        intent_kind=StepIntentKind.CREATE_FILE,
        target_paths=tuple(paths or [f"src/file_{index}.py"]),
        repo="jarvis",
        depends_on=tuple(depends_on),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestProducesOneEnvelopePerStep:
    def test_produces_one_envelope_per_step(self):
        plan = _make_plan([_step(0), _step(1), _step(2)])
        envelopes = PlanDecomposer.decompose(plan, saga_id="saga-001")
        assert len(envelopes) == 3

    def test_single_step_plan(self):
        plan = _make_plan([_step(0)])
        envelopes = PlanDecomposer.decompose(plan, saga_id="saga-single")
        assert len(envelopes) == 1


class TestEnvelopeSourceIsArchitecture:
    def test_envelope_source_is_architecture(self):
        plan = _make_plan([_step(0), _step(1)])
        envelopes = PlanDecomposer.decompose(plan, saga_id="saga-002")
        for env in envelopes:
            assert env.source == "architecture"


class TestEnvelopeCarriesSagaBinding:
    def test_envelope_carries_saga_binding(self):
        plan = _make_plan([_step(0), _step(1)])
        saga_id = "saga-xyz-999"
        envelopes = PlanDecomposer.decompose(plan, saga_id=saga_id)
        for env in envelopes:
            assert env.evidence["saga_id"] == saga_id
            assert env.evidence["plan_hash"] == plan.plan_hash

    def test_step_index_in_evidence(self):
        plan = _make_plan([_step(0), _step(1), _step(2)])
        envelopes = PlanDecomposer.decompose(plan, saga_id="saga-idx")
        indices = [env.evidence["step_index"] for env in envelopes]
        # All step indices must be present (order may vary by topology)
        assert sorted(indices) == [0, 1, 2]


class TestEnvelopeCarriesAnalysisComplete:
    def test_envelope_carries_analysis_complete(self):
        plan = _make_plan([_step(0), _step(1)])
        envelopes = PlanDecomposer.decompose(plan, saga_id="saga-003")
        for env in envelopes:
            assert env.evidence["analysis_complete"] is True


class TestEnvelopeTargetFilesMatchStep:
    def test_envelope_target_files_match_step(self):
        steps = [
            _step(0, paths=["src/alpha.py", "src/beta.py"]),
            _step(1, paths=["tests/test_alpha.py"]),
        ]
        plan = _make_plan(steps)
        envelopes = PlanDecomposer.decompose(plan, saga_id="saga-004")

        # Build a map from step_index -> target_files via envelope
        idx_to_files = {
            env.evidence["step_index"]: set(env.target_files)
            for env in envelopes
        }
        assert idx_to_files[0] == {"src/alpha.py", "src/beta.py"}
        assert idx_to_files[1] == {"tests/test_alpha.py"}

    def test_envelope_confidence_is_one(self):
        plan = _make_plan([_step(0)])
        envelopes = PlanDecomposer.decompose(plan, saga_id="saga-conf")
        assert envelopes[0].confidence == 1.0

    def test_envelope_urgency_is_normal(self):
        plan = _make_plan([_step(0)])
        envelopes = PlanDecomposer.decompose(plan, saga_id="saga-urg")
        assert envelopes[0].urgency == "normal"

    def test_envelope_requires_human_ack_is_false(self):
        plan = _make_plan([_step(0)])
        envelopes = PlanDecomposer.decompose(plan, saga_id="saga-ack")
        assert envelopes[0].requires_human_ack is False

    def test_envelope_intent_kind_in_evidence(self):
        step = _step(0)
        plan = _make_plan([step])
        envelopes = PlanDecomposer.decompose(plan, saga_id="saga-kind")
        assert envelopes[0].evidence["intent_kind"] == step.intent_kind.value


class TestTopologicalOrder:
    def test_topological_order_respects_dependencies(self):
        """
        Step 0 has no deps.
        Step 1 depends on step 0.
        Step 2 depends on step 1.
        Expected order: 0 -> 1 -> 2.
        """
        steps = [
            _step(0),
            _step(1, depends_on=(0,)),
            _step(2, depends_on=(1,)),
        ]
        plan = _make_plan(steps)
        envelopes = PlanDecomposer.decompose(plan, saga_id="saga-topo")
        indices = [env.evidence["step_index"] for env in envelopes]
        assert indices == [0, 1, 2]

    def test_topological_order_diamond(self):
        """
        Diamond: step 0 at root; steps 1 and 2 depend on 0; step 3 depends on 1 and 2.
        0 must come before 1 and 2; 1 and 2 before 3.
        """
        steps = [
            _step(0),
            _step(1, depends_on=(0,)),
            _step(2, depends_on=(0,)),
            _step(3, depends_on=(1, 2)),
        ]
        plan = _make_plan(steps)
        envelopes = PlanDecomposer.decompose(plan, saga_id="saga-diamond")
        indices = [env.evidence["step_index"] for env in envelopes]

        pos = {idx: i for i, idx in enumerate(indices)}
        assert pos[0] < pos[1]
        assert pos[0] < pos[2]
        assert pos[1] < pos[3]
        assert pos[2] < pos[3]

    def test_topological_order_deterministic_within_tier(self):
        """
        Steps 0, 1, 2 are all independent (no deps). Must be emitted in
        step_index order (0, 1, 2) for determinism.
        """
        steps = [_step(0), _step(1), _step(2)]
        plan = _make_plan(steps)
        envelopes = PlanDecomposer.decompose(plan, saga_id="saga-det")
        indices = [env.evidence["step_index"] for env in envelopes]
        assert indices == [0, 1, 2]
