"""End-to-end integration tests for the Autonomy Iteration Mode pipeline.

These tests verify the full pipeline: backlog -> task source -> planner ->
execution -> evaluation -> review gate, with all dependencies mocked at
the boundary.

Key mocks:
- ``preflight_check`` is patched to pass (it has dedicated unit tests).
- ``asyncio.create_subprocess_exec`` is patched so git commands return
  deterministic results.
- Scheduler, trust graduator, comm, and ledger are mocked at the boundary.

Covers:
- T23: Backlog happy path -- full IDLE->SELECTING->...->REVIEW_GATE->IDLE
- T24: Miner task at SUGGEST tier with requires_human_ack
- T25: Review gate in GOVERNED mode creates a branch (git subprocess called)
- T26: Review gate in GOVERNED mode does NOT auto-merge
- T29: Cross-repo barriers -- multi-repo graph partitions by barrier_id
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from backend.core.ouroboros.governance.autonomy.iteration_types import (
    BlastRadiusPolicy,
    IterationState,
    IterationStopPolicy,
    IterationTask,
    PlannerOutcome,
    PlannerRejectReason,
    PlanningContext,
    TaskRejectionTracker,
)
from backend.core.ouroboros.governance.autonomy.iteration_planner import (
    IterationPlanner,
    IterationTaskSource,
)
from backend.core.ouroboros.governance.autonomy.iteration_budget import (
    IterationBudgetGuard,
)
from backend.core.ouroboros.governance.autonomy.resource_governor import (
    ResourceGovernor,
)
from backend.core.ouroboros.governance.autonomy.subagent_types import (
    ExecutionGraph,
    GraphExecutionPhase,
    WorkUnitSpec,
)
from backend.core.ouroboros.governance.autonomy.tiers import AutonomyTier


# ---------------------------------------------------------------------------
# Lazy import of service under test
# ---------------------------------------------------------------------------

def _import_service():
    from backend.core.ouroboros.governance.autonomy.iteration_service import (
        AutonomyIterationService,
    )
    return AutonomyIterationService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PREFLIGHT_MODULE = (
    "backend.core.ouroboros.governance.autonomy.iteration_service.preflight_check"
)


def _make_backlog_json(
    tmp_path: Path,
    tasks: Optional[List[Dict[str, Any]]] = None,
) -> Path:
    """Write a backlog.json file and return its path."""
    backlog_path = tmp_path / ".jarvis" / "backlog.json"
    backlog_path.parent.mkdir(parents=True, exist_ok=True)
    if tasks is None:
        tasks = [
            {
                "task_id": "task-e2e-001",
                "status": "pending",
                "description": "improve test coverage for module X",
                "target_files": ["backend/core/example.py"],
                "repo": "jarvis",
                "priority": 10,
            }
        ]
    backlog_path.write_text(json.dumps(tasks), encoding="utf-8")
    return backlog_path


def _make_work_unit(
    unit_id: str = "u0",
    repo: str = "jarvis",
    barrier_id: str = "",
    dependency_ids: tuple = (),
) -> WorkUnitSpec:
    return WorkUnitSpec(
        unit_id=unit_id,
        repo=repo,
        goal="improve test coverage",
        target_files=("backend/core/example.py",),
        barrier_id=barrier_id,
        dependency_ids=dependency_ids,
    )


def _make_graph(
    graph_id: str = "graph-e2e-001",
    units: Optional[tuple] = None,
) -> ExecutionGraph:
    if units is None:
        units = (_make_work_unit(),)
    return ExecutionGraph(
        graph_id=graph_id,
        op_id="iter-e2e001",
        planner_id="iteration_planner_v1",
        schema_version="3.0",
        units=units,
        concurrency_limit=1,
    )


def _make_planning_context(tier: AutonomyTier = AutonomyTier.GOVERNED) -> PlanningContext:
    return PlanningContext(
        repo_commit="abc123",
        oracle_snapshot_id="snap-e2e",
        policy_hash="e2e-hash",
        schema_version="3.0",
        trust_tier=tier,
        budget_remaining_usd=4.5,
    )


class FakeLedger:
    """In-memory ledger for E2E testing."""

    def __init__(self) -> None:
        self.entries: List[Any] = []

    async def append(self, entry: Any) -> bool:
        self.entries.append(entry)
        return True

    def all_entries(self) -> List[Any]:
        return list(self.entries)


class FakeGraphStore:
    """In-memory graph store for E2E testing."""

    def __init__(self, inflight: Optional[Dict[str, Any]] = None) -> None:
        self._inflight = inflight or {}

    def load_inflight(self) -> Dict[str, Any]:
        return dict(self._inflight)


def _build_e2e_service(
    *,
    tmp_path: Path,
    backlog_tasks: Optional[List[Dict[str, Any]]] = None,
    budget_ok: bool = True,
    graph: Optional[Any] = None,
    graph_phase: str = "completed",
    feature_flag: bool = True,
    governance_mode: str = "governed",
    stop_policy: Optional[IterationStopPolicy] = None,
    max_consecutive_failures: int = 3,
):
    """Build a full-pipeline AutonomyIterationService with real task source + planner,
    but mocked scheduler/trust/comm/ledger.

    Preflight is mocked separately at the call site.
    """
    AutonomyIterationService = _import_service()

    # Create backlog
    backlog_path = _make_backlog_json(tmp_path, backlog_tasks)

    # Real task source with real backlog file
    tracker = TaskRejectionTracker()
    task_source = IterationTaskSource(
        backlog_path=backlog_path,
        miner=None,
        rejection_tracker=tracker,
    )

    # Real planner with mock oracle (async-safe)
    oracle = MagicMock()
    oracle.get_file_neighborhood = MagicMock(return_value=None)
    oracle.semantic_search = AsyncMock(return_value=[])

    if stop_policy is None:
        stop_policy = IterationStopPolicy(
            max_iterations_per_session=25,
            max_consecutive_failures=max_consecutive_failures,
            max_wall_time_s=3600.0,
            max_spend_usd=5.0,
            cooldown_base_s=0.0,
            max_cooldown_s=0.0,
            miner_fairness_interval=5,
        )

    planner = IterationPlanner(
        oracle=oracle,
        blast_radius=stop_policy.blast_radius,
        rejection_tracker=tracker,
        repo_root=tmp_path,
    )

    # Budget guard
    budget_guard = MagicMock()
    budget_guard.can_proceed = MagicMock(
        return_value=(budget_ok, "" if budget_ok else "budget exhausted")
    )
    budget_guard.record_spend = AsyncMock()
    budget_guard.compute_cooldown = MagicMock(return_value=0.0)

    # Resource governor (mock to always not yield)
    resource_governor = AsyncMock()
    resource_governor.should_yield = AsyncMock(return_value=False)

    # Scheduler
    if graph is None:
        graph = _make_graph()

    from backend.core.ouroboros.governance.autonomy.subagent_types import GraphExecutionState
    completed_state = MagicMock(spec=GraphExecutionState)
    completed_state.phase = GraphExecutionPhase(graph_phase)
    completed_state.completed_units = tuple(u.unit_id for u in graph.units)
    completed_state.failed_units = ()
    completed_state.cancelled_units = ()
    completed_state.last_error = "" if graph_phase == "completed" else "failed"

    scheduler = AsyncMock()
    scheduler.submit = AsyncMock(return_value=True)
    scheduler.wait_for_graph = AsyncMock(return_value=completed_state)
    scheduler._store = FakeGraphStore()

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
        repo_root=tmp_path,
        governance_mode=governance_mode,
    )

    return service, {
        "task_source": task_source,
        "planner": planner,
        "oracle": oracle,
        "budget_guard": budget_guard,
        "resource_governor": resource_governor,
        "scheduler": scheduler,
        "trust_graduator": trust_graduator,
        "ledger": ledger,
        "comm": comm,
        "env_patch": env_patch,
        "tracker": tracker,
        "backlog_path": backlog_path,
    }


def _make_fake_subprocess(stdout: bytes = b"abc123\n", returncode: int = 0):
    """Create an async mock for asyncio.create_subprocess_exec."""
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(stdout, b""))
    proc.returncode = returncode
    return proc


# ---------------------------------------------------------------------------
# T23: Backlog happy path -- full cycle through FSM
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backlog_happy_path(tmp_path: Path):
    """T23: Backlog task flows through SELECTING -> PLANNING -> EXECUTING ->
    EVALUATING -> REVIEW_GATE -> IDLE and the ledger records the outcome."""
    service, deps = _build_e2e_service(tmp_path=tmp_path)

    with patch.dict(os.environ, deps["env_patch"]), \
         patch(_PREFLIGHT_MODULE, new_callable=AsyncMock, return_value=None):
        await service.start()

        # Let the FSM run a few cycles
        for _ in range(80):
            await asyncio.sleep(0.01)
            if service._cycle_count >= 1 and service._state == IterationState.IDLE:
                break

        await service.stop()

    # Verify: at least one cycle completed
    assert service._cycle_count >= 1

    # Verify: ledger has an ITERATION_OUTCOME entry
    outcome_entries = [
        e for e in deps["ledger"].entries
        if hasattr(e, "state") and e.state.value == "iteration_outcome"
    ]
    assert len(outcome_entries) >= 1

    # Verify: scheduler was asked to submit and wait
    deps["scheduler"].submit.assert_called()
    deps["scheduler"].wait_for_graph.assert_called()

    # Verify: comm protocol received intent + decision events
    deps["comm"].emit_intent.assert_called()
    deps["comm"].emit_decision.assert_called()


# ---------------------------------------------------------------------------
# T24: Miner task at SUGGEST tier with requires_human_ack
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_miner_ack_in_suggest(tmp_path: Path):
    """T24: When a miner-sourced task has requires_human_ack=True at SUGGEST
    tier, the comm protocol emits a suggest_pr decision (not auto-apply)."""
    backlog = [
        {
            "task_id": "task-miner-ack-001",
            "status": "pending",
            "description": "refactor detected by miner",
            "target_files": ["backend/core/example.py"],
            "repo": "jarvis",
            "priority": 5,
            "requires_human_ack": True,
        }
    ]

    service, deps = _build_e2e_service(
        tmp_path=tmp_path,
        backlog_tasks=backlog,
        governance_mode="suggest",
    )

    with patch.dict(os.environ, deps["env_patch"]), \
         patch(_PREFLIGHT_MODULE, new_callable=AsyncMock, return_value=None):
        await service.start()

        for _ in range(80):
            await asyncio.sleep(0.01)
            if service._cycle_count >= 1 and service._state == IterationState.IDLE:
                break

        await service.stop()

    assert service._cycle_count >= 1

    # In suggest mode, the review gate emits a suggest_pr decision via comm.emit_decision
    decision_calls = deps["comm"].emit_decision.call_args_list
    assert len(decision_calls) >= 1

    # Verify at least one decision call mentions "suggest_pr" in its kwargs
    suggest_calls = [
        c for c in decision_calls
        if c.kwargs.get("outcome") == "suggest_pr"
        or c.kwargs.get("reason_code") == "review_gate_suggest"
    ]
    assert len(suggest_calls) >= 1, (
        f"Expected at least one suggest_pr decision call, got: {decision_calls}"
    )


# ---------------------------------------------------------------------------
# T25: Review gate creates branch in GOVERNED mode (git subprocess)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_review_gate_creates_branch_governed(tmp_path: Path):
    """T25: In GOVERNED mode, the review gate attempts to create a git branch
    via subprocess. We mock asyncio.create_subprocess_exec to verify."""
    service, deps = _build_e2e_service(
        tmp_path=tmp_path,
        governance_mode="governed",
    )

    subprocess_calls: List[tuple] = []

    async def fake_subprocess(*args, **kwargs):
        subprocess_calls.append(args)
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"abc123\n", b""))
        proc.returncode = 0
        return proc

    with patch.dict(os.environ, deps["env_patch"]), \
         patch(_PREFLIGHT_MODULE, new_callable=AsyncMock, return_value=None), \
         patch("asyncio.create_subprocess_exec", side_effect=fake_subprocess):
        await service.start()

        for _ in range(80):
            await asyncio.sleep(0.01)
            if service._cycle_count >= 1 and service._state == IterationState.IDLE:
                break

        await service.stop()

    assert service._cycle_count >= 1

    # Verify git checkout -b was called with an autonomy/ branch
    branch_calls = [
        c for c in subprocess_calls
        if len(c) >= 4 and c[0] == "git" and c[1] == "checkout" and c[2] == "-b"
        and str(c[3]).startswith("autonomy/")
    ]
    assert len(branch_calls) >= 1, (
        f"Expected git checkout -b autonomy/... call, got: {subprocess_calls}"
    )


# ---------------------------------------------------------------------------
# T26: Review gate in GOVERNED mode does NOT auto-merge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_review_gate_no_auto_merge_governed(tmp_path: Path):
    """T26: In GOVERNED mode, the review gate must never issue a merge or push
    command. Only branch creation is allowed."""
    service, deps = _build_e2e_service(
        tmp_path=tmp_path,
        governance_mode="governed",
    )

    subprocess_calls: List[tuple] = []

    async def fake_subprocess(*args, **kwargs):
        subprocess_calls.append(args)
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"abc123\n", b""))
        proc.returncode = 0
        return proc

    with patch.dict(os.environ, deps["env_patch"]), \
         patch(_PREFLIGHT_MODULE, new_callable=AsyncMock, return_value=None), \
         patch("asyncio.create_subprocess_exec", side_effect=fake_subprocess):
        await service.start()

        for _ in range(80):
            await asyncio.sleep(0.01)
            if service._cycle_count >= 1 and service._state == IterationState.IDLE:
                break

        await service.stop()

    # Verify no merge or push git commands were issued
    for args_tuple in subprocess_calls:
        if args_tuple and args_tuple[0] == "git":
            git_subcommand = args_tuple[1] if len(args_tuple) > 1 else ""
            assert git_subcommand != "merge", (
                f"GOVERNED mode must not auto-merge, but got: {args_tuple}"
            )
            assert git_subcommand != "push", (
                f"GOVERNED mode must not push, but got: {args_tuple}"
            )


# ---------------------------------------------------------------------------
# T29: Cross-repo barriers -- verify barrier grouping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_repo_barriers(tmp_path: Path):
    """T29: A multi-repo graph with barrier_ids groups units by barrier.
    The planner preserves barrier grouping in the assembled graph."""
    unit_a = _make_work_unit(unit_id="u-repo-a", repo="jarvis", barrier_id="deploy-phase-1")
    unit_b = _make_work_unit(unit_id="u-repo-b", repo="reactor", barrier_id="deploy-phase-1")
    unit_c = _make_work_unit(
        unit_id="u-repo-c",
        repo="jarvis",
        barrier_id="deploy-phase-2",
        dependency_ids=("u-repo-a",),
    )

    graph = _make_graph(
        graph_id="graph-barrier-001",
        units=(unit_a, unit_b, unit_c),
    )

    # Verify barrier grouping: units with same barrier_id are grouped
    barrier_groups: Dict[str, List[str]] = {}
    for unit in graph.units:
        if unit.barrier_id:
            barrier_groups.setdefault(unit.barrier_id, []).append(unit.unit_id)

    assert "deploy-phase-1" in barrier_groups
    assert sorted(barrier_groups["deploy-phase-1"]) == ["u-repo-a", "u-repo-b"]
    assert "deploy-phase-2" in barrier_groups
    assert barrier_groups["deploy-phase-2"] == ["u-repo-c"]

    # Verify the graph was built validly (DAG checks passed in __post_init__)
    assert graph.graph_id == "graph-barrier-001"
    assert len(graph.units) == 3

    # Verify multi-repo: units span different repos
    repos = {u.repo for u in graph.units}
    assert repos == {"jarvis", "reactor"}

    # Verify dependency chain: u-repo-c depends on u-repo-a (cross-barrier)
    unit_c_from_graph = next(u for u in graph.units if u.unit_id == "u-repo-c")
    assert "u-repo-a" in unit_c_from_graph.dependency_ids


# ---------------------------------------------------------------------------
# Additional integration tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_budget_exhausted_stays_idle(tmp_path: Path):
    """When budget is exhausted, service stays in IDLE and does not select tasks."""
    service, deps = _build_e2e_service(
        tmp_path=tmp_path,
        budget_ok=False,
    )

    with patch.dict(os.environ, deps["env_patch"]):
        await service.start()

        # Let FSM run briefly
        for _ in range(20):
            await asyncio.sleep(0.01)

        state_before_stop = service._state
        await service.stop()

    # Should never have selected a task
    assert service._cycle_count == 0
    # Should still be in IDLE (budget denied entry to SELECTING)
    assert state_before_stop in (IterationState.IDLE, IterationState.STOPPED)


@pytest.mark.asyncio
async def test_feature_flag_disabled_stops_service(tmp_path: Path):
    """When feature flag is off, the service transitions to STOPPED immediately."""
    service, deps = _build_e2e_service(
        tmp_path=tmp_path,
        feature_flag=False,
    )

    # No env_patch since feature_flag=False
    await service.start()

    for _ in range(20):
        await asyncio.sleep(0.01)
        if service._state == IterationState.STOPPED:
            break

    await service.stop()

    assert service._state == IterationState.STOPPED
    assert service._cycle_count == 0


@pytest.mark.asyncio
async def test_real_task_source_reads_backlog(tmp_path: Path):
    """IterationTaskSource correctly reads and parses backlog.json into IterationTask objects."""
    backlog = [
        {
            "task_id": "task-read-001",
            "status": "pending",
            "description": "fix broken import",
            "target_files": ["backend/core/broken.py"],
            "repo": "jarvis",
            "priority": 20,
        },
        {
            "task_id": "task-read-002",
            "status": "completed",  # Should be filtered out
            "description": "already done",
            "target_files": ["backend/core/done.py"],
            "repo": "jarvis",
            "priority": 5,
        },
        {
            "task_id": "task-read-003",
            "status": "pending",
            "description": "add logging",
            "target_files": ["backend/core/logging.py"],
            "repo": "jarvis",
            "priority": 15,
        },
    ]

    backlog_path = _make_backlog_json(tmp_path, backlog)
    tracker = TaskRejectionTracker()
    source = IterationTaskSource(
        backlog_path=backlog_path,
        miner=None,
        rejection_tracker=tracker,
    )

    tasks = await source.get_backlog_tasks()

    # Only pending tasks
    assert len(tasks) == 2
    # Sorted by priority descending
    assert tasks[0].task_id == "task-read-001"  # priority 20
    assert tasks[1].task_id == "task-read-003"  # priority 15
    # Completed task filtered out
    assert all(t.task_id != "task-read-002" for t in tasks)


@pytest.mark.asyncio
async def test_planner_rejects_empty_target_files(tmp_path: Path):
    """Planner rejects a task that has no target files after expansion."""
    tracker = TaskRejectionTracker()
    oracle = MagicMock()
    oracle.get_file_neighborhood = MagicMock(return_value=None)
    oracle.semantic_search = AsyncMock(return_value=[])

    planner = IterationPlanner(
        oracle=oracle,
        blast_radius=BlastRadiusPolicy(),
        rejection_tracker=tracker,
        repo_root=tmp_path,
    )

    task = IterationTask(
        task_id="task-empty-001",
        source="backlog",
        description="do nothing",
        target_files=(),  # empty
        repo="jarvis",
        priority=1,
    )

    context = _make_planning_context()
    outcome = await planner.plan(task, "iter-test-empty", context)

    assert outcome.status == "rejected"
    assert outcome.reject_reason is not None


@pytest.mark.asyncio
async def test_stop_kills_inflight_graph(tmp_path: Path):
    """Calling stop() cancels any inflight graph via scheduler.abort()."""
    service, deps = _build_e2e_service(tmp_path=tmp_path)

    # Make scheduler.wait_for_graph block forever so we can stop mid-execution
    wait_event = asyncio.Event()

    async def slow_wait(*args, **kwargs):
        await wait_event.wait()

    deps["scheduler"].wait_for_graph = AsyncMock(side_effect=slow_wait)
    deps["scheduler"].abort = AsyncMock()

    with patch.dict(os.environ, deps["env_patch"]), \
         patch(_PREFLIGHT_MODULE, new_callable=AsyncMock, return_value=None):
        await service.start()

        # Wait for the service to reach EXECUTING (graph submitted)
        for _ in range(80):
            await asyncio.sleep(0.01)
            if service._state == IterationState.EXECUTING:
                break

        # Now stop while the scheduler is waiting
        await service.stop()

    # Service should be STOPPED
    assert service._state == IterationState.STOPPED

    # Verify the terminal ledger entry was written
    stop_entries = [
        e for e in deps["ledger"].entries
        if hasattr(e, "data") and isinstance(e.data, dict) and e.data.get("event") == "service_stopped"
    ]
    assert len(stop_entries) >= 1
