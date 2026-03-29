"""
End-to-end integration tests for the Architecture Reasoning Agent pipeline.

Covers the full plan → validate → store → decompose → saga → accept lifecycle,
plus the ArchitectureReasoningAgent's hypothesis-filtering logic.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.core.ouroboros.architect.acceptance_runner import (
    AcceptanceResult,
    AcceptanceRunner,
)
from backend.core.ouroboros.architect.plan import (
    AcceptanceCheck,
    ArchitecturalPlan,
    CheckKind,
    PlanStep,
    StepIntentKind,
)
from backend.core.ouroboros.architect.plan_decomposer import PlanDecomposer
from backend.core.ouroboros.architect.plan_store import PlanStore
from backend.core.ouroboros.architect.plan_validator import PlanValidator
from backend.core.ouroboros.architect.reasoning_agent import (
    AgentConfig,
    ArchitectureReasoningAgent,
)
from backend.core.ouroboros.architect.saga import SagaPhase
from backend.core.ouroboros.architect.saga_orchestrator import SagaOrchestrator


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_two_step_plan(
    *,
    with_check: bool = False,
) -> ArchitecturalPlan:
    """Return a valid 2-step ArchitecturalPlan for integration tests.

    Step 0: create a new module file.
    Step 1: wire it into the registry (depends on step 0).
    """
    steps = (
        PlanStep(
            step_index=0,
            description="Create the plugin file",
            intent_kind=StepIntentKind.CREATE_FILE,
            target_paths=("backend/core/ouroboros/architect/plugin.py",),
            repo="jarvis",
        ),
        PlanStep(
            step_index=1,
            description="Wire the plugin into the registry",
            intent_kind=StepIntentKind.MODIFY_FILE,
            target_paths=("backend/core/ouroboros/architect/registry.py",),
            repo="jarvis",
            depends_on=(0,),
        ),
    )

    checks: tuple[AcceptanceCheck, ...] = ()
    if with_check:
        checks = (
            AcceptanceCheck(
                check_id="chk-echo",
                check_kind=CheckKind.EXIT_CODE,
                command="true",
                expected="0",
                sandbox_required=False,  # must be False so checks actually run
            ),
        )

    return ArchitecturalPlan.create(
        parent_hypothesis_id="hyp-integ-0001",
        parent_hypothesis_fingerprint="fp-integ",
        title="Add Plugin and Wire Registry",
        description="Two-step integration test plan.",
        repos_affected=("jarvis",),
        non_goals=("Do not touch unrelated modules",),
        steps=steps,
        acceptance_checks=checks,
        model_used="test-model",
        created_at=1_700_000_000.0,
        snapshot_hash="snap-integ",
    )


# ---------------------------------------------------------------------------
# test_plan_validates
# ---------------------------------------------------------------------------


def test_plan_validates() -> None:
    """A correctly constructed 2-step plan passes all 10 validator rules."""
    plan = _make_two_step_plan()
    validator = PlanValidator()

    result = validator.validate(plan)

    assert result.passed, f"Validation failed unexpectedly: {result.reasons}"
    assert result.reasons == []


# ---------------------------------------------------------------------------
# test_plan_stores_and_loads
# ---------------------------------------------------------------------------


def test_plan_stores_and_loads(tmp_path: Path) -> None:
    """Storing a plan and loading it back by plan_hash yields the same title."""
    plan = _make_two_step_plan()
    store = PlanStore(store_dir=tmp_path / "plans")

    store.store(plan)
    assert store.exists(plan.plan_hash)

    loaded = store.load(plan.plan_hash)

    assert loaded is not None
    assert loaded.title == plan.title
    assert loaded.plan_hash == plan.plan_hash
    assert loaded.plan_id == plan.plan_id


# ---------------------------------------------------------------------------
# test_plan_decomposes_to_envelopes
# ---------------------------------------------------------------------------


def test_plan_decomposes_to_envelopes() -> None:
    """Decomposing a 2-step plan yields 2 envelopes with the correct step indices."""
    plan = _make_two_step_plan()
    saga_id = "testdecompose001"

    envelopes = PlanDecomposer.decompose(plan, saga_id)

    assert len(envelopes) == 2

    # Envelopes should be in topological order: step 0 first, step 1 second.
    step_indices = [env.evidence["step_index"] for env in envelopes]
    assert step_indices == [0, 1]

    # Every envelope must carry the saga correlation id.
    for env in envelopes:
        assert env.evidence["saga_id"] == saga_id
        assert env.evidence["plan_hash"] == plan.plan_hash


# ---------------------------------------------------------------------------
# test_full_saga_lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_saga_lifecycle(tmp_path: Path) -> None:
    """End-to-end: create plan → store → create saga → execute → COMPLETE.

    Uses a real AcceptanceRunner with a sandbox_required=False EXIT_CODE check
    so that the acceptance logic actually executes (``true`` always exits 0).
    Intake router is mocked to return 'enqueued' for each envelope.
    """
    plan = _make_two_step_plan(with_check=True)

    # Store the plan in a real PlanStore backed by tmp_path.
    store = PlanStore(store_dir=tmp_path / "plans")
    store.store(plan)

    # Mock intake router — must support async ingest().
    intake_router = MagicMock()
    intake_router.ingest = AsyncMock(return_value="enqueued")

    # Real AcceptanceRunner — the check uses ``true`` which exits 0 instantly.
    acceptance_runner = AcceptanceRunner()

    orchestrator = SagaOrchestrator(
        plan_store=store,
        intake_router=intake_router,
        acceptance_runner=acceptance_runner,
        saga_dir=tmp_path / "sagas",
    )

    saga = orchestrator.create_saga(plan)
    result = await orchestrator.execute(saga.saga_id)

    assert result.phase is SagaPhase.COMPLETE, (
        f"Expected COMPLETE but got {result.phase}; abort_reason={result.abort_reason}"
    )
    assert result.abort_reason is None

    # intake was called once per step (2 steps total).
    assert intake_router.ingest.await_count == 2


# ---------------------------------------------------------------------------
# test_reasoning_agent_filters_correctly
# ---------------------------------------------------------------------------


def test_reasoning_agent_filters_correctly() -> None:
    """ArchitectureReasoningAgent.should_design filters on gap_type + confidence.

    - missing_capability + high confidence  → should_design is True
    - incomplete_wiring (not an arch type)  → should_design is False regardless
    """
    oracle = MagicMock()
    doubleword = MagicMock()
    agent = ArchitectureReasoningAgent(
        oracle=oracle,
        doubleword=doubleword,
        config=AgentConfig(min_confidence=0.7),
    )

    # Qualifying hypothesis: architectural gap type + confidence above threshold.
    qualifying = MagicMock()
    qualifying.gap_type = "missing_capability"
    qualifying.confidence = 0.85

    assert agent.should_design(qualifying) is True

    # Non-qualifying hypothesis: gap type not in the architectural set.
    non_qualifying = MagicMock()
    non_qualifying.gap_type = "incomplete_wiring"
    non_qualifying.confidence = 0.99  # high confidence, but wrong gap_type

    assert agent.should_design(non_qualifying) is False
