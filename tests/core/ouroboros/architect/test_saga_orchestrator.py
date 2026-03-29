"""
Tests for SagaOrchestrator — WAL-backed state machine for executing
ArchitecturalPlans through the Unified Intake Router.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core.ouroboros.architect.plan import (
    AcceptanceCheck,
    ArchitecturalPlan,
    CheckKind,
    PlanStep,
    StepIntentKind,
)
from backend.core.ouroboros.architect.saga import SagaPhase, StepPhase
from backend.core.ouroboros.architect.acceptance_runner import AcceptanceResult
from backend.core.ouroboros.architect.saga_orchestrator import SagaOrchestrator


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_plan(
    *,
    num_steps: int = 1,
    with_check: bool = False,
) -> ArchitecturalPlan:
    """Build a minimal ArchitecturalPlan with *num_steps* sequential steps."""
    steps = tuple(
        PlanStep(
            step_index=i,
            description=f"Step {i}",
            intent_kind=StepIntentKind.CREATE_FILE,
            target_paths=(f"backend/core/file_{i}.py",),
            repo="jarvis",
            depends_on=(i - 1,) if i > 0 else (),
        )
        for i in range(num_steps)
    )
    checks: tuple[AcceptanceCheck, ...] = ()
    if with_check:
        checks = (
            AcceptanceCheck(
                check_id="chk-001",
                check_kind=CheckKind.EXIT_CODE,
                command="true",
                expected="0",
                sandbox_required=False,
            ),
        )
    return ArchitecturalPlan.create(
        parent_hypothesis_id="hyp-test",
        parent_hypothesis_fingerprint="fp-test",
        title="Test Plan",
        description="A test plan.",
        repos_affected=("jarvis",),
        non_goals=(),
        steps=steps,
        acceptance_checks=checks,
        model_used="claude-test",
        created_at=1_700_000_000.0,
        snapshot_hash="snap-test",
    )


def _make_orchestrator(
    tmp_path: Path,
    plan: ArchitecturalPlan | None = None,
    intake_return: str = "enqueued",
    acceptance_results: list[AcceptanceResult] | None = None,
) -> tuple[SagaOrchestrator, MagicMock, AsyncMock, AsyncMock]:
    """Return (orchestrator, plan_store_mock, intake_router_mock, acceptance_runner_mock)."""
    plan_store = MagicMock()
    plan_store.load.return_value = plan

    intake_router = MagicMock()
    intake_router.ingest = AsyncMock(return_value=intake_return)

    acceptance_runner = MagicMock()
    if acceptance_results is None:
        acceptance_results = []
    acceptance_runner.run_checks = AsyncMock(return_value=acceptance_results)

    saga_dir = tmp_path / "sagas"
    orchestrator = SagaOrchestrator(
        plan_store=plan_store,
        intake_router=intake_router,
        acceptance_runner=acceptance_runner,
        saga_dir=saga_dir,
    )
    return orchestrator, plan_store, intake_router, acceptance_runner


# ---------------------------------------------------------------------------
# test_create_saga
# ---------------------------------------------------------------------------


def test_create_saga(tmp_path: Path) -> None:
    """create_saga returns a PENDING SagaRecord with the correct step count."""
    plan = _make_plan(num_steps=3)
    orchestrator, _, _, _ = _make_orchestrator(tmp_path, plan=plan)

    saga = orchestrator.create_saga(plan)

    assert saga.phase is SagaPhase.PENDING
    assert saga.plan_hash == plan.plan_hash
    assert saga.plan_id == plan.plan_id
    assert len(saga.step_states) == 3
    for step_state in saga.step_states.values():
        assert step_state.phase is StepPhase.PENDING

    # saga_id should be a 16-char hex string
    assert len(saga.saga_id) == 16
    assert all(c in "0123456789abcdef" for c in saga.saga_id)


def test_create_saga_single_step(tmp_path: Path) -> None:
    """create_saga works for a 1-step plan."""
    plan = _make_plan(num_steps=1)
    orchestrator, _, _, _ = _make_orchestrator(tmp_path, plan=plan)

    saga = orchestrator.create_saga(plan)

    assert saga.phase is SagaPhase.PENDING
    assert len(saga.step_states) == 1


# ---------------------------------------------------------------------------
# test_execute_saga_completes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_saga_completes(tmp_path: Path) -> None:
    """1-step plan where intake returns 'enqueued' and acceptance passes → COMPLETE."""
    plan = _make_plan(num_steps=1)
    good_result = AcceptanceResult(check_id="chk-001", passed=True, output="ok")
    orchestrator, plan_store, intake_router, acceptance_runner = _make_orchestrator(
        tmp_path,
        plan=plan,
        intake_return="enqueued",
        acceptance_results=[good_result],
    )
    plan_store.load.return_value = plan

    saga = orchestrator.create_saga(plan)
    result = await orchestrator.execute(saga.saga_id)

    assert result.phase is SagaPhase.COMPLETE
    assert result.abort_reason is None
    assert result.completed_at is not None

    # Step 0 must be COMPLETE
    assert result.step_states[0].phase is StepPhase.COMPLETE
    assert result.step_states[0].completed_at is not None

    # intake_router.ingest was called once
    intake_router.ingest.assert_awaited_once()


@pytest.mark.asyncio
async def test_execute_multi_step_saga_completes(tmp_path: Path) -> None:
    """3-step sequential plan completes in order."""
    plan = _make_plan(num_steps=3)
    orchestrator, plan_store, intake_router, _ = _make_orchestrator(
        tmp_path, plan=plan, intake_return="enqueued"
    )
    plan_store.load.return_value = plan

    saga = orchestrator.create_saga(plan)
    result = await orchestrator.execute(saga.saga_id)

    assert result.phase is SagaPhase.COMPLETE
    assert intake_router.ingest.await_count == 3

    for i in range(3):
        assert result.step_states[i].phase is StepPhase.COMPLETE


# ---------------------------------------------------------------------------
# test_execute_saga_aborts_on_failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_saga_aborts_on_failure(tmp_path: Path) -> None:
    """intake raises an Exception → first step FAILED, saga ABORTED, rest BLOCKED."""
    plan = _make_plan(num_steps=3)

    plan_store = MagicMock()
    plan_store.load.return_value = plan
    intake_router = MagicMock()
    intake_router.ingest = AsyncMock(side_effect=RuntimeError("broker unavailable"))
    acceptance_runner = MagicMock()
    acceptance_runner.run_checks = AsyncMock(return_value=[])

    saga_dir = tmp_path / "sagas"
    orchestrator = SagaOrchestrator(
        plan_store=plan_store,
        intake_router=intake_router,
        acceptance_runner=acceptance_runner,
        saga_dir=saga_dir,
    )

    saga = orchestrator.create_saga(plan)
    result = await orchestrator.execute(saga.saga_id)

    assert result.phase is SagaPhase.ABORTED
    assert result.abort_reason is not None
    assert "broker unavailable" in result.abort_reason

    # Step 0 failed (it was the one that raised)
    assert result.step_states[0].phase is StepPhase.FAILED
    assert result.step_states[0].error is not None

    # Steps 1 and 2 were never started → BLOCKED
    assert result.step_states[1].phase is StepPhase.BLOCKED
    assert result.step_states[2].phase is StepPhase.BLOCKED

    # intake_router.ingest was only called once (for step 0)
    intake_router.ingest.assert_awaited_once()


@pytest.mark.asyncio
async def test_execute_aborts_when_plan_not_found(tmp_path: Path) -> None:
    """If plan_store.load returns None the saga is immediately ABORTED."""
    plan = _make_plan(num_steps=1)

    plan_store = MagicMock()
    plan_store.load.return_value = None  # plan missing from store
    intake_router = MagicMock()
    intake_router.ingest = AsyncMock(return_value="enqueued")
    acceptance_runner = MagicMock()
    acceptance_runner.run_checks = AsyncMock(return_value=[])

    saga_dir = tmp_path / "sagas"
    orchestrator = SagaOrchestrator(
        plan_store=plan_store,
        intake_router=intake_router,
        acceptance_runner=acceptance_runner,
        saga_dir=saga_dir,
    )

    # We still need a saga record; create it then load a fake plan_hash
    saga = orchestrator.create_saga(plan)
    result = await orchestrator.execute(saga.saga_id)

    assert result.phase is SagaPhase.ABORTED
    assert result.abort_reason is not None
    # intake was never called
    intake_router.ingest.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_aborts_when_acceptance_fails(tmp_path: Path) -> None:
    """All steps complete but acceptance check fails → ABORTED."""
    plan = _make_plan(num_steps=1, with_check=True)
    failed_result = AcceptanceResult(
        check_id="chk-001", passed=False, error="test failed"
    )
    orchestrator, plan_store, intake_router, _ = _make_orchestrator(
        tmp_path,
        plan=plan,
        intake_return="enqueued",
        acceptance_results=[failed_result],
    )
    plan_store.load.return_value = plan

    saga = orchestrator.create_saga(plan)
    result = await orchestrator.execute(saga.saga_id)

    assert result.phase is SagaPhase.ABORTED
    assert result.abort_reason is not None
    assert "chk-001" in result.abort_reason


# ---------------------------------------------------------------------------
# test_saga_persists_to_wal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_saga_persists_to_wal(tmp_path: Path) -> None:
    """After execute, a new SagaOrchestrator instance can read the completed saga."""
    plan = _make_plan(num_steps=1)

    def make_fresh_orchestrator() -> SagaOrchestrator:
        ps = MagicMock()
        ps.load.return_value = plan
        ir = MagicMock()
        ir.ingest = AsyncMock(return_value="enqueued")
        ar = MagicMock()
        ar.run_checks = AsyncMock(return_value=[])
        return SagaOrchestrator(
            plan_store=ps,
            intake_router=ir,
            acceptance_runner=ar,
            saga_dir=tmp_path / "sagas",
        )

    # First instance: create and execute
    orchestrator_a = make_fresh_orchestrator()
    saga = orchestrator_a.create_saga(plan)
    await orchestrator_a.execute(saga.saga_id)

    # Second instance: loads from WAL
    orchestrator_b = make_fresh_orchestrator()
    loaded = orchestrator_b.get_saga(saga.saga_id)

    assert loaded is not None
    assert loaded.saga_id == saga.saga_id
    assert loaded.phase is SagaPhase.COMPLETE
    assert loaded.step_states[0].phase is StepPhase.COMPLETE


@pytest.mark.asyncio
async def test_wal_written_on_abort(tmp_path: Path) -> None:
    """An aborted saga is also persisted to WAL so it can be inspected later."""
    plan = _make_plan(num_steps=2)

    plan_store = MagicMock()
    plan_store.load.return_value = plan
    intake_router = MagicMock()
    intake_router.ingest = AsyncMock(side_effect=RuntimeError("network error"))
    acceptance_runner = MagicMock()
    acceptance_runner.run_checks = AsyncMock(return_value=[])

    saga_dir = tmp_path / "sagas"
    orch_a = SagaOrchestrator(
        plan_store=plan_store,
        intake_router=intake_router,
        acceptance_runner=acceptance_runner,
        saga_dir=saga_dir,
    )
    saga = orch_a.create_saga(plan)
    await orch_a.execute(saga.saga_id)

    # New orchestrator reads WAL
    orch_b = SagaOrchestrator(
        plan_store=MagicMock(),
        intake_router=MagicMock(),
        acceptance_runner=MagicMock(),
        saga_dir=saga_dir,
    )
    loaded = orch_b.get_saga(saga.saga_id)
    assert loaded is not None
    assert loaded.phase is SagaPhase.ABORTED


# ---------------------------------------------------------------------------
# test_list_sagas
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_sagas(tmp_path: Path) -> None:
    """list_sagas returns all sagas created and executed by this orchestrator."""
    plan_a = _make_plan(num_steps=1)
    plan_b = _make_plan(num_steps=2)

    def make_orch(plan: ArchitecturalPlan) -> SagaOrchestrator:
        ps = MagicMock()
        ps.load.return_value = plan
        ir = MagicMock()
        ir.ingest = AsyncMock(return_value="enqueued")
        ar = MagicMock()
        ar.run_checks = AsyncMock(return_value=[])
        return SagaOrchestrator(
            plan_store=ps,
            intake_router=ir,
            acceptance_runner=ar,
            saga_dir=tmp_path / "sagas",
        )

    orch_a = make_orch(plan_a)
    saga_a = orch_a.create_saga(plan_a)
    await orch_a.execute(saga_a.saga_id)

    orch_b = make_orch(plan_b)
    saga_b = orch_b.create_saga(plan_b)
    await orch_b.execute(saga_b.saga_id)

    # A third orchestrator on same saga_dir sees both
    orch_c = SagaOrchestrator(
        plan_store=MagicMock(),
        intake_router=MagicMock(),
        acceptance_runner=MagicMock(),
        saga_dir=tmp_path / "sagas",
    )
    sagas = orch_c.list_sagas()
    saga_ids = {s.saga_id for s in sagas}

    assert saga_a.saga_id in saga_ids
    assert saga_b.saga_id in saga_ids
    assert len(sagas) == 2


def test_list_sagas_empty(tmp_path: Path) -> None:
    """list_sagas returns an empty list when no sagas have been persisted."""
    orch = SagaOrchestrator(
        plan_store=MagicMock(),
        intake_router=MagicMock(),
        acceptance_runner=MagicMock(),
        saga_dir=tmp_path / "sagas",
    )
    assert orch.list_sagas() == []


def test_get_saga_missing(tmp_path: Path) -> None:
    """get_saga returns None for an unknown saga_id."""
    orch = SagaOrchestrator(
        plan_store=MagicMock(),
        intake_router=MagicMock(),
        acceptance_runner=MagicMock(),
        saga_dir=tmp_path / "sagas",
    )
    assert orch.get_saga("nonexistent000000") is None
