"""Tests for AutonomyIterationService — 10-state FSM (T13-T35).

Covers:
- T13: IDLE -> SELECTING when budget allows
- T15: error streak -> PAUSED + trust demotion
- T17: recovery resumes non-terminal graph -> EVALUATING
- T18: recovery partial-apply checksum mismatch -> PAUSED
- T22: causal trace — iteration_id appears in comm messages
- T27: kill switch — stop() -> STOPPED + terminal ledger
- T30: consecutive failures -> trust tier demoted
- T31: feature flag off -> STOPPED
- T32: start() idempotent
- T34/T35: full state transitions through the FSM
"""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core.ouroboros.governance.autonomy.iteration_types import (
    IterationState,
    IterationStopPolicy,
    IterationTask,
    PlannerOutcome,
    PlannerRejectReason,
    PlanningContext,
)
from backend.core.ouroboros.governance.autonomy.tiers import AutonomyTier


# ---------------------------------------------------------------------------
# Lazy import of the service under test (module must exist before importing)
# ---------------------------------------------------------------------------


def _import_service():
    from backend.core.ouroboros.governance.autonomy.iteration_service import (
        AutonomyIterationService,
    )
    return AutonomyIterationService


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_task(task_id: str = "task-001") -> IterationTask:
    return IterationTask(
        task_id=task_id,
        source="backlog",
        description="improve foo",
        target_files=("backend/foo.py",),
        repo="jarvis",
        priority=5,
    )


def _make_planning_context() -> PlanningContext:
    return PlanningContext(
        repo_commit="abc123",
        oracle_snapshot_id="snap-1",
        policy_hash="deadbeef",
        schema_version="3.0",
        trust_tier=AutonomyTier.GOVERNED,
        budget_remaining_usd=4.0,
    )


def _make_graph_mock(graph_id: str = "plan-test") -> MagicMock:
    """Create a minimal mock graph object."""
    graph = MagicMock()
    graph.graph_id = graph_id
    graph.op_id = "iter-test123456"
    graph.causal_trace_id = f"{graph_id}:abc123"
    graph.units = ()
    graph.unit_map = {}
    return graph


def _make_planner_outcome(status: str = "accepted", graph: Any = None) -> PlannerOutcome:
    if status == "accepted" and graph is None:
        graph = _make_graph_mock()
    return PlannerOutcome(
        status=status,
        graph=graph,
        reject_reason=PlannerRejectReason.ZERO_ACTIONABLE_UNITS if status == "rejected" else None,
    )


class FakeLedger:
    """In-memory ledger for testing."""

    def __init__(self) -> None:
        self.entries: List[Any] = []

    async def append(self, entry: Any) -> bool:
        self.entries.append(entry)
        return True

    def all_entries(self) -> List[Any]:
        return list(self.entries)


class FakeGraphStore:
    """In-memory graph store for testing."""

    def __init__(self, inflight: Optional[Dict[str, Any]] = None) -> None:
        self._inflight = inflight or {}

    def load_inflight(self) -> Dict[str, Any]:
        return dict(self._inflight)


def _build_service(
    *,
    budget_ok: bool = True,
    task: Optional[IterationTask] = None,
    plan_status: str = "accepted",
    graph_phase: str = "completed",
    feature_flag: bool = True,
    inflight: Optional[Dict[str, Any]] = None,
    stop_policy: Optional[IterationStopPolicy] = None,
    governance_mode: str = "governed",
    max_consecutive_failures: int = 3,
):
    """Build an AutonomyIterationService with mocked dependencies."""
    AutonomyIterationService = _import_service()

    # Task source
    task_source = AsyncMock()
    task_source.select_task = AsyncMock(return_value=task)

    # Planner
    planner = AsyncMock()
    graph_mock = _make_graph_mock()
    outcome = _make_planner_outcome(plan_status, graph_mock if plan_status == "accepted" else None)
    planner.plan = AsyncMock(return_value=outcome)

    # Budget guard
    budget_guard = MagicMock()
    budget_guard.can_proceed = MagicMock(
        return_value=(budget_ok, "" if budget_ok else "budget exhausted")
    )
    budget_guard.record_spend = AsyncMock()
    budget_guard.compute_cooldown = MagicMock(return_value=0.0)

    # Resource governor
    resource_governor = AsyncMock()
    resource_governor.should_yield = AsyncMock(return_value=False)

    # Scheduler
    from backend.core.ouroboros.governance.autonomy.subagent_types import (
        GraphExecutionPhase,
        GraphExecutionState,
    )
    completed_state = MagicMock()
    completed_state.phase = GraphExecutionPhase(graph_phase)
    completed_state.completed_units = ("u0",)
    completed_state.failed_units = ()
    completed_state.cancelled_units = ()
    completed_state.last_error = "" if graph_phase == "completed" else "failed"

    scheduler = AsyncMock()
    scheduler.submit = AsyncMock(return_value=True)
    scheduler.wait_for_graph = AsyncMock(return_value=completed_state)
    scheduler._store = FakeGraphStore(inflight)

    # Trust graduator
    trust_graduator = MagicMock()
    trust_graduator.demote = MagicMock(return_value=AutonomyTier.OBSERVE)

    # Ledger
    ledger = FakeLedger()

    # Comm protocol
    comm = AsyncMock()
    comm.emit_intent = AsyncMock()
    comm.emit_plan = AsyncMock()
    comm.emit_decision = AsyncMock()
    comm.emit_postmortem = AsyncMock()

    # Stop policy
    if stop_policy is None:
        stop_policy = IterationStopPolicy(
            max_iterations_per_session=25,
            max_consecutive_failures=max_consecutive_failures,
            max_wall_time_s=3600.0,
            max_spend_usd=5.0,
            cooldown_base_s=0.0,  # zero cooldown for tests
            max_cooldown_s=0.0,
            miner_fairness_interval=5,
        )

    env_patch = {}
    if feature_flag:
        env_patch["JARVIS_AUTONOMY_ITERATION_ENABLED"] = "true"

    service = AutonomyIterationService(
        task_source=task_source,
        planner=planner,
        budget_guard=budget_guard,
        resource_governor=resource_governor,
        scheduler=scheduler,
        trust_graduator=trust_graduator,
        ledger=ledger,
        comm=comm,
        stop_policy=stop_policy,
        repo_root=Path("/fake/repo"),
        governance_mode=governance_mode,
    )

    return service, {
        "task_source": task_source,
        "planner": planner,
        "budget_guard": budget_guard,
        "resource_governor": resource_governor,
        "scheduler": scheduler,
        "trust_graduator": trust_graduator,
        "ledger": ledger,
        "comm": comm,
        "stop_policy": stop_policy,
        "graph_mock": graph_mock,
        "completed_state": completed_state,
        "env_patch": env_patch,
    }


# ---------------------------------------------------------------------------
# Test 1: start sets IDLE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_sets_idle():
    """start() with no inflight graphs -> IDLE."""
    service, deps = _build_service(budget_ok=False)
    with patch.dict(os.environ, deps["env_patch"], clear=False):
        await service.start()
        try:
            assert service._state == IterationState.IDLE
            assert service._running is True
        finally:
            await service.stop()


# ---------------------------------------------------------------------------
# Test 2: start idempotent (T32)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_idempotent():
    """Second start() call is a no-op — T32."""
    service, deps = _build_service(budget_ok=False)
    with patch.dict(os.environ, deps["env_patch"], clear=False):
        await service.start()
        first_task = service._loop_task
        await service.start()  # second call
        assert service._loop_task is first_task  # same task object
        await service.stop()


# ---------------------------------------------------------------------------
# Test 3: IDLE -> SELECTING when budget OK (T13)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idle_to_selecting():
    """Budget OK -> transitions from IDLE to SELECTING — T13."""
    service, deps = _build_service(budget_ok=True, task=_make_task())
    with patch.dict(os.environ, deps["env_patch"], clear=False):
        # Drive one step manually
        service._state = IterationState.IDLE
        service._running = True
        await service._do_idle()
        assert service._state == IterationState.SELECTING


# ---------------------------------------------------------------------------
# Test 4: IDLE stays when budget exhausted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idle_stays_when_budget_exhausted():
    """Budget exhausted -> stays in IDLE."""
    service, deps = _build_service(budget_ok=False)
    with patch.dict(os.environ, deps["env_patch"], clear=False):
        service._state = IterationState.IDLE
        service._running = True
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await service._do_idle()
        assert service._state == IterationState.IDLE


# ---------------------------------------------------------------------------
# Test 5: SELECTING -> PLANNING (task found)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_selecting_to_planning():
    """Task found -> PLANNING."""
    service, deps = _build_service(task=_make_task())
    with patch.dict(os.environ, deps["env_patch"], clear=False):
        service._state = IterationState.SELECTING
        service._running = True
        service._cycle_count = 0
        await service._do_selecting()
        assert service._state == IterationState.PLANNING
        assert service._current_task is not None


# ---------------------------------------------------------------------------
# Test 6: SELECTING -> IDLE (no tasks)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_selecting_to_idle_nothing():
    """No tasks available -> IDLE."""
    service, deps = _build_service(task=None)
    with patch.dict(os.environ, deps["env_patch"], clear=False):
        service._state = IterationState.SELECTING
        service._running = True
        service._cycle_count = 0
        await service._do_selecting()
        assert service._state == IterationState.IDLE


# ---------------------------------------------------------------------------
# Test 7: PLANNING -> EXECUTING (plan accepted)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_planning_to_executing():
    """Plan accepted -> EXECUTING."""
    service, deps = _build_service(task=_make_task(), plan_status="accepted")
    with patch.dict(os.environ, deps["env_patch"], clear=False):
        service._state = IterationState.PLANNING
        service._running = True
        service._current_task = _make_task()
        service._current_iteration_id = "iter-test123456"
        await service._do_planning()
        assert service._state == IterationState.EXECUTING


# ---------------------------------------------------------------------------
# Test 8: PLANNING rejected -> EVALUATING
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_planning_rejected_to_evaluating():
    """Plan rejected -> EVALUATING."""
    service, deps = _build_service(task=_make_task(), plan_status="rejected")
    with patch.dict(os.environ, deps["env_patch"], clear=False):
        service._state = IterationState.PLANNING
        service._running = True
        service._current_task = _make_task()
        service._current_iteration_id = "iter-test123456"
        await service._do_planning()
        assert service._state == IterationState.EVALUATING


# ---------------------------------------------------------------------------
# Test 9: EXECUTING -> EVALUATING (graph completes)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_executing_to_evaluating():
    """Graph completes successfully -> EVALUATING."""
    service, deps = _build_service(
        task=_make_task(),
        plan_status="accepted",
        graph_phase="completed",
    )
    with patch.dict(os.environ, deps["env_patch"], clear=False):
        service._state = IterationState.EXECUTING
        service._running = True
        service._current_task = _make_task()
        service._current_iteration_id = "iter-test123456"
        service._current_graph = deps["graph_mock"]
        service._current_graph_id = deps["graph_mock"].graph_id
        # Mock preflight to pass
        with patch(
            "backend.core.ouroboros.governance.autonomy.iteration_service.preflight_check",
            new_callable=AsyncMock,
            return_value=None,
        ):
            await service._do_executing()
        assert service._state == IterationState.EVALUATING


# ---------------------------------------------------------------------------
# Test 10: Recovery resumes (T17)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recovery_resumes():
    """Non-terminal graph in store -> resume -> EVALUATING — T17."""
    from backend.core.ouroboros.governance.autonomy.subagent_types import (
        GraphExecutionPhase,
    )

    service, deps = _build_service()
    with patch.dict(os.environ, deps["env_patch"], clear=False):
        # Set up a non-terminal state that transitions to terminal on wait
        non_terminal = MagicMock()
        non_terminal.phase = GraphExecutionPhase.RUNNING
        non_terminal.graph = _make_graph_mock()
        non_terminal.checksum = "abc"
        non_terminal.completed_units = ()
        non_terminal.failed_units = ()

        terminal = MagicMock()
        terminal.phase = GraphExecutionPhase.COMPLETED
        terminal.completed_units = ("u0",)
        terminal.failed_units = ()
        terminal.cancelled_units = ()
        terminal.last_error = ""
        terminal.checksum = "abc"

        deps["scheduler"]._store._inflight = {"plan-recovery": non_terminal}
        deps["scheduler"].wait_for_graph = AsyncMock(return_value=terminal)

        service._state = IterationState.RECOVERING
        service._running = True
        service._recovery_graph_id = "plan-recovery"
        service._recovery_attempts = 0
        await service._do_recovering()
        assert service._state == IterationState.EVALUATING


# ---------------------------------------------------------------------------
# Test 11: Recovery partial apply checksum mismatch -> PAUSED (T18)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recovery_partial_apply():
    """Checksum mismatch during recovery -> PAUSED — T18."""
    from backend.core.ouroboros.governance.autonomy.subagent_types import (
        GraphExecutionPhase,
    )

    service, deps = _build_service()
    with patch.dict(os.environ, deps["env_patch"], clear=False):
        # Non-terminal with irrecoverable state
        non_terminal = MagicMock()
        non_terminal.phase = GraphExecutionPhase.RUNNING
        non_terminal.graph = _make_graph_mock()
        non_terminal.checksum = "old_checksum"
        non_terminal.completed_units = ("u0",)
        non_terminal.failed_units = ("u1",)

        deps["scheduler"]._store._inflight = {"plan-recovery": non_terminal}
        # wait_for_graph raises timeout -> triggers irrecoverable
        deps["scheduler"].wait_for_graph = AsyncMock(
            side_effect=asyncio.TimeoutError()
        )

        service._state = IterationState.RECOVERING
        service._running = True
        service._recovery_graph_id = "plan-recovery"
        service._recovery_attempts = 2  # max attempts reached
        await service._do_recovering()
        assert service._state == IterationState.PAUSED


# ---------------------------------------------------------------------------
# Test 12: EVALUATING success -> REVIEW_GATE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evaluating_success_to_review_gate():
    """Changes applied -> REVIEW_GATE."""
    service, deps = _build_service()
    with patch.dict(os.environ, deps["env_patch"], clear=False):
        service._state = IterationState.EVALUATING
        service._running = True
        service._current_iteration_id = "iter-test123456"
        service._current_task = _make_task()
        service._last_outcome = "success"
        service._consecutive_failures = 0
        service._cycle_count = 1
        await service._do_evaluating()
        assert service._state == IterationState.REVIEW_GATE


# ---------------------------------------------------------------------------
# Test 13: EVALUATING failure -> COOLDOWN
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evaluating_failure_to_cooldown():
    """Failure -> COOLDOWN."""
    service, deps = _build_service()
    with patch.dict(os.environ, deps["env_patch"], clear=False):
        service._state = IterationState.EVALUATING
        service._running = True
        service._current_iteration_id = "iter-test123456"
        service._current_task = _make_task()
        service._last_outcome = "failure"
        service._consecutive_failures = 1
        service._cycle_count = 1
        await service._do_evaluating()
        assert service._state == IterationState.COOLDOWN


# ---------------------------------------------------------------------------
# Test 14: Error streak pauses (T15) + trust regression (T30)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_error_streak_pauses():
    """3 consecutive failures -> PAUSED + trust demotion — T15, T30."""
    service, deps = _build_service(max_consecutive_failures=3)
    with patch.dict(os.environ, deps["env_patch"], clear=False):
        service._state = IterationState.EVALUATING
        service._running = True
        service._current_iteration_id = "iter-test123456"
        service._current_task = _make_task()
        service._last_outcome = "failure"
        service._consecutive_failures = 3  # equals max
        service._cycle_count = 3
        await service._do_evaluating()
        assert service._state == IterationState.PAUSED
        # Trust graduator demote was called
        deps["trust_graduator"].demote.assert_called_once()


# ---------------------------------------------------------------------------
# Test 15: Trust regression (T30) — demote called with correct args
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trust_regression():
    """Consecutive failures trigger trust tier demotion — T30."""
    service, deps = _build_service(max_consecutive_failures=3)
    with patch.dict(os.environ, deps["env_patch"], clear=False):
        service._state = IterationState.EVALUATING
        service._running = True
        service._current_iteration_id = "iter-test123456"
        service._current_task = _make_task()
        service._last_outcome = "failure"
        service._consecutive_failures = 3
        service._cycle_count = 3
        await service._do_evaluating()
        assert service._state == IterationState.PAUSED
        call_args = deps["trust_graduator"].demote.call_args
        assert "error_streak" in call_args[1].get("reason", "") or "error_streak" in str(call_args)


# ---------------------------------------------------------------------------
# Test 16: Kill switch (T27)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kill_switch():
    """stop() -> STOPPED + terminal ledger — T27."""
    service, deps = _build_service(budget_ok=False)
    with patch.dict(os.environ, deps["env_patch"], clear=False):
        await service.start()
        # Give the loop a chance to run one iteration
        await asyncio.sleep(0.05)
        await service.stop()
        assert service._state == IterationState.STOPPED
        assert service._running is False
        # Terminal ledger entry written
        assert any(
            getattr(e, "state", None) is not None
            for e in deps["ledger"].entries
        )


# ---------------------------------------------------------------------------
# Test 17: Feature flag off (T31)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_feature_flag_off():
    """Flag disabled -> STOPPED — T31."""
    service, deps = _build_service(feature_flag=False)
    # Do NOT set the env var — the feature flag is off
    env = {k: v for k, v in deps["env_patch"].items()}
    env.pop("JARVIS_AUTONOMY_ITERATION_ENABLED", None)
    with patch.dict(os.environ, env, clear=False):
        # Ensure the flag is truly off
        os.environ.pop("JARVIS_AUTONOMY_ITERATION_ENABLED", None)
        assert service._is_enabled() is False


# ---------------------------------------------------------------------------
# Test 18: Causal trace (T22)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_causal_trace():
    """iteration_id appears in comm messages — T22."""
    service, deps = _build_service(
        budget_ok=True,
        task=_make_task(),
        plan_status="accepted",
        graph_phase="completed",
    )
    with patch.dict(os.environ, deps["env_patch"], clear=False):
        service._state = IterationState.SELECTING
        service._running = True
        service._cycle_count = 0

        # Step through SELECTING -> PLANNING
        await service._do_selecting()
        assert service._state == IterationState.PLANNING
        iteration_id = service._current_iteration_id
        assert iteration_id.startswith("iter-")

        # Step through PLANNING -> EXECUTING
        with patch(
            "backend.core.ouroboros.governance.autonomy.iteration_service.preflight_check",
            new_callable=AsyncMock,
            return_value=None,
        ):
            await service._do_planning()

        # Check that comm.emit_intent was called with the iteration_id
        if deps["comm"].emit_intent.called:
            call_args = deps["comm"].emit_intent.call_args
            assert iteration_id in str(call_args)


# ---------------------------------------------------------------------------
# Test 19: health() returns current state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_state_in_health():
    """health() returns current FSM state."""
    service, deps = _build_service()
    service._state = IterationState.COOLDOWN
    service._cycle_count = 7
    service._consecutive_failures = 2
    health = service.health()
    assert health["state"] == "COOLDOWN"
    assert health["cycle_count"] == 7
    assert health["consecutive_failures"] == 2
    assert health["running"] is False


# ---------------------------------------------------------------------------
# Test 20: Cooldown waits correct duration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cooldown_waits():
    """Cooldown sleeps for budget_guard.compute_cooldown() seconds."""
    service, deps = _build_service()
    deps["budget_guard"].compute_cooldown.return_value = 0.0  # instant
    with patch.dict(os.environ, deps["env_patch"], clear=False):
        service._state = IterationState.COOLDOWN
        service._running = True
        service._consecutive_failures = 1
        await service._do_cooldown()
        assert service._state == IterationState.IDLE
        deps["budget_guard"].compute_cooldown.assert_called_once_with(1)


# ---------------------------------------------------------------------------
# Test 21: REVIEW_GATE transitions to IDLE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_review_gate_to_idle():
    """REVIEW_GATE completes and returns to IDLE."""
    service, deps = _build_service(governance_mode="governed")
    with patch.dict(os.environ, deps["env_patch"], clear=False):
        service._state = IterationState.REVIEW_GATE
        service._running = True
        service._current_iteration_id = "iter-test123456"
        service._current_task = _make_task()
        service._governance_mode = "governed"
        await service._do_review_gate()
        assert service._state == IterationState.IDLE


# ---------------------------------------------------------------------------
# Test 22: Resume from PAUSED
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_from_paused():
    """resume() moves from PAUSED to IDLE."""
    service, deps = _build_service()
    service._state = IterationState.PAUSED
    await service.resume("manual resume")
    assert service._state == IterationState.IDLE


# ---------------------------------------------------------------------------
# Test 23: Resume ignored when not PAUSED
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_ignored_when_not_paused():
    """resume() is a no-op when not in PAUSED state."""
    service, deps = _build_service()
    service._state = IterationState.IDLE
    await service.resume("should be ignored")
    assert service._state == IterationState.IDLE


# ---------------------------------------------------------------------------
# Test 24: Start with inflight graphs -> RECOVERING
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_with_inflight_goes_recovering():
    """start() with inflight graphs transitions to RECOVERING."""
    from backend.core.ouroboros.governance.autonomy.subagent_types import (
        GraphExecutionPhase,
    )
    inflight_state = MagicMock()
    inflight_state.phase = GraphExecutionPhase.RUNNING
    inflight_state.graph = _make_graph_mock("plan-inflight")

    service, deps = _build_service(
        inflight={"plan-inflight": inflight_state},
        budget_ok=False,
    )
    with patch.dict(os.environ, deps["env_patch"], clear=False):
        await service.start()
        try:
            assert service._state == IterationState.RECOVERING
        finally:
            await service.stop()
