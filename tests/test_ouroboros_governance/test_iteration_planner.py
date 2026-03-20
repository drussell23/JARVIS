"""Tests for IterationTaskSource, IterationPlanner, and select_acceptance_tests.

Go/No-Go tests: T06, T10, T11, T12, T20, T33.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.core.ouroboros.governance.autonomy.iteration_types import (
    BlastRadiusPolicy,
    IterationTask,
    PlannerOutcome,
    PlannerRejectReason,
    PlanningContext,
    TaskRejectionTracker,
)
from backend.core.ouroboros.governance.autonomy.tiers import AutonomyTier


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_context(**overrides: Any) -> PlanningContext:
    defaults: Dict[str, Any] = dict(
        repo_commit="abc123",
        oracle_snapshot_id="snap-001",
        policy_hash="ph-000",
        schema_version="3.0",
        trust_tier=AutonomyTier.GOVERNED,
        budget_remaining_usd=4.0,
    )
    defaults.update(overrides)
    return PlanningContext(**defaults)


def _write_backlog(path: Path, items: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(items), encoding="utf-8")


@dataclass
class FakeCandidate:
    file_path: str
    cyclomatic_complexity: int
    static_evidence_score: float


def _fake_miner(candidates: List[FakeCandidate]) -> Any:
    """Return a mock miner whose scan_once() returns *candidates*."""
    m = AsyncMock()
    m.scan_once = AsyncMock(return_value=candidates)
    return m


# ---------------------------------------------------------------------------
# 1. get_backlog_tasks reads JSON, filters pending, sorts by priority
# ---------------------------------------------------------------------------

class TestGetBacklogTasks:
    @pytest.mark.asyncio
    async def test_reads_filters_sorts(self, tmp_path: Path) -> None:
        from backend.core.ouroboros.governance.autonomy.iteration_planner import (
            IterationTaskSource,
        )

        backlog = tmp_path / ".jarvis" / "backlog.json"
        _write_backlog(backlog, [
            {"task_id": "t1", "description": "low", "target_files": ["a.py"],
             "priority": 1, "repo": "jarvis", "status": "pending"},
            {"task_id": "t2", "description": "high", "target_files": ["b.py"],
             "priority": 9, "repo": "jarvis", "status": "pending"},
            {"task_id": "t3", "description": "done", "target_files": ["c.py"],
             "priority": 5, "repo": "jarvis", "status": "completed"},
        ])

        source = IterationTaskSource(
            backlog_path=backlog,
            miner=None,
            rejection_tracker=TaskRejectionTracker(),
        )
        tasks = await source.get_backlog_tasks()

        assert len(tasks) == 2
        # Sorted by priority descending
        assert tasks[0].priority == 9
        assert tasks[1].priority == 1

    # -----------------------------------------------------------------------
    # 2. T33 — get_backlog_tasks handles malformed JSON
    # -----------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_malformed_json_returns_empty(self, tmp_path: Path) -> None:
        """T33 — corrupted backlog must not crash, must return []."""
        from backend.core.ouroboros.governance.autonomy.iteration_planner import (
            IterationTaskSource,
        )

        backlog = tmp_path / ".jarvis" / "backlog.json"
        backlog.parent.mkdir(parents=True, exist_ok=True)
        backlog.write_text("{INVALID JSON!!", encoding="utf-8")

        source = IterationTaskSource(
            backlog_path=backlog,
            miner=None,
            rejection_tracker=TaskRejectionTracker(),
        )
        tasks = await source.get_backlog_tasks()
        assert tasks == []

    # -----------------------------------------------------------------------
    # 3. get_backlog_tasks skips poisoned tasks
    # -----------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_skips_poisoned_tasks(self, tmp_path: Path) -> None:
        from backend.core.ouroboros.governance.autonomy.iteration_planner import (
            IterationTaskSource,
        )

        backlog = tmp_path / ".jarvis" / "backlog.json"
        _write_backlog(backlog, [
            {"task_id": "t-poison", "description": "bad", "target_files": ["x.py"],
             "priority": 10, "repo": "jarvis", "status": "pending"},
            {"task_id": "t-ok", "description": "good", "target_files": ["y.py"],
             "priority": 5, "repo": "jarvis", "status": "pending"},
        ])

        tracker = TaskRejectionTracker(poison_threshold=2)
        tracker.record_rejection("t-poison", PlannerRejectReason.BLAST_RADIUS_EXCEEDED)
        tracker.record_rejection("t-poison", PlannerRejectReason.BLAST_RADIUS_EXCEEDED)
        assert tracker.is_poisoned("t-poison")

        source = IterationTaskSource(
            backlog_path=backlog, miner=None, rejection_tracker=tracker,
        )
        tasks = await source.get_backlog_tasks()
        assert len(tasks) == 1
        assert tasks[0].task_id == "t-ok"

    # -----------------------------------------------------------------------
    # 4. get_backlog_tasks returns empty for missing file
    # -----------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        from backend.core.ouroboros.governance.autonomy.iteration_planner import (
            IterationTaskSource,
        )

        backlog = tmp_path / ".jarvis" / "backlog.json"
        # File does not exist
        source = IterationTaskSource(
            backlog_path=backlog, miner=None,
            rejection_tracker=TaskRejectionTracker(),
        )
        tasks = await source.get_backlog_tasks()
        assert tasks == []


# ---------------------------------------------------------------------------
# 5. get_miner_tasks converts StaticCandidate
# ---------------------------------------------------------------------------

class TestGetMinerTasks:
    @pytest.mark.asyncio
    async def test_converts_static_candidate(self, tmp_path: Path) -> None:
        from backend.core.ouroboros.governance.autonomy.iteration_planner import (
            IterationTaskSource,
        )

        candidates = [
            FakeCandidate("backend/foo.py", 15, 0.75),
            FakeCandidate("backend/bar.py", 20, 0.90),
        ]
        miner = _fake_miner(candidates)

        source = IterationTaskSource(
            backlog_path=tmp_path / "missing.json",
            miner=miner,
            rejection_tracker=TaskRejectionTracker(),
        )
        tasks = await source.get_miner_tasks()

        assert len(tasks) == 2
        assert tasks[0].source == "opportunity_miner"
        assert "backend/foo.py" in tasks[0].target_files
        assert tasks[0].requires_human_ack is True  # miner tasks always need ack


# ---------------------------------------------------------------------------
# 6. select_task — backlog-first by default
# ---------------------------------------------------------------------------

class TestSelectTask:
    @pytest.mark.asyncio
    async def test_backlog_first_default(self, tmp_path: Path) -> None:
        from backend.core.ouroboros.governance.autonomy.iteration_planner import (
            IterationTaskSource,
        )

        backlog = tmp_path / ".jarvis" / "backlog.json"
        _write_backlog(backlog, [
            {"task_id": "t-backlog", "description": "from backlog",
             "target_files": ["a.py"], "priority": 5, "repo": "jarvis",
             "status": "pending"},
        ])

        candidates = [FakeCandidate("backend/x.py", 12, 0.6)]
        miner = _fake_miner(candidates)

        source = IterationTaskSource(
            backlog_path=backlog, miner=miner,
            rejection_tracker=TaskRejectionTracker(),
        )
        task = await source.select_task(cycle_count=1, fairness_interval=5)

        assert task is not None
        assert task.task_id == "t-backlog"

    # -----------------------------------------------------------------------
    # 7. T20 — select_task miner-first on Nth cycle
    # -----------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_miner_first_on_nth_cycle(self, tmp_path: Path) -> None:
        """T20 — On the fairness interval cycle, miner gets priority."""
        from backend.core.ouroboros.governance.autonomy.iteration_planner import (
            IterationTaskSource,
        )

        backlog = tmp_path / ".jarvis" / "backlog.json"
        _write_backlog(backlog, [
            {"task_id": "t-backlog", "description": "from backlog",
             "target_files": ["a.py"], "priority": 5, "repo": "jarvis",
             "status": "pending"},
        ])

        candidates = [FakeCandidate("backend/mined.py", 14, 0.7)]
        miner = _fake_miner(candidates)

        source = IterationTaskSource(
            backlog_path=backlog, miner=miner,
            rejection_tracker=TaskRejectionTracker(),
        )
        # cycle_count=5, fairness_interval=5 → 5 % 5 == 0 → miner first
        task = await source.select_task(cycle_count=5, fairness_interval=5)

        assert task is not None
        assert task.source == "opportunity_miner"

    # -----------------------------------------------------------------------
    # 8. select_task returns None when both empty
    # -----------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_returns_none_when_both_empty(self, tmp_path: Path) -> None:
        from backend.core.ouroboros.governance.autonomy.iteration_planner import (
            IterationTaskSource,
        )

        # Empty miner, missing backlog file
        miner = _fake_miner([])
        source = IterationTaskSource(
            backlog_path=tmp_path / "nope.json",
            miner=miner,
            rejection_tracker=TaskRejectionTracker(),
        )
        task = await source.select_task(cycle_count=1, fairness_interval=5)
        assert task is None


# ---------------------------------------------------------------------------
# 9. T06 — plan() returns PlannerOutcome(status="rejected") not None
# ---------------------------------------------------------------------------

class TestIterationPlanner:
    @pytest.mark.asyncio
    async def test_plan_returns_outcome_never_none(self, tmp_path: Path) -> None:
        """T06 — plan() MUST return PlannerOutcome, NEVER None or raise."""
        from backend.core.ouroboros.governance.autonomy.iteration_planner import (
            IterationPlanner,
        )

        planner = IterationPlanner(
            oracle=None,
            blast_radius=BlastRadiusPolicy(max_files_changed=0),  # will reject
            rejection_tracker=TaskRejectionTracker(),
            repo_root=tmp_path,
        )
        task = IterationTask(
            task_id="t1", source="test", description="fix it",
            target_files=("a.py",), repo="jarvis",
        )
        ctx = _make_context()
        result = await planner.plan(task, "iter-001", ctx)

        assert isinstance(result, PlannerOutcome)
        assert result.status in ("accepted", "rejected")
        # With max_files_changed=0, should reject
        assert result.status == "rejected"
        assert result.reject_reason is not None

    # -----------------------------------------------------------------------
    # 10. T12 — plan() with Oracle returns expansion_proof in metadata
    # -----------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_plan_with_oracle_has_expansion_proof(self, tmp_path: Path) -> None:
        """T12 — When Oracle is available, metadata must contain expansion_proof."""
        from backend.core.ouroboros.governance.autonomy.iteration_planner import (
            IterationPlanner,
        )

        # Create target files on disk so they pass canonicalization
        (tmp_path / "backend").mkdir(parents=True, exist_ok=True)
        (tmp_path / "backend" / "foo.py").write_text("# code")
        (tmp_path / "backend" / "bar.py").write_text("# code")

        oracle = MagicMock()
        oracle.semantic_search = AsyncMock(return_value=[
            ("jarvis:backend/bar.py", 0.85),
        ])
        oracle.get_file_neighborhood = MagicMock(return_value=MagicMock(
            imports=[], importers=[], callers=[], callees=[],
            inheritors=[], base_classes=[], test_counterparts=[],
            all_unique_files=MagicMock(return_value=[]),
        ))

        planner = IterationPlanner(
            oracle=oracle,
            blast_radius=BlastRadiusPolicy(max_files_changed=50),
            rejection_tracker=TaskRejectionTracker(),
            repo_root=tmp_path,
        )
        task = IterationTask(
            task_id="t2", source="test", description="fix auth bug",
            target_files=("backend/foo.py",), repo="jarvis",
        )
        ctx = _make_context()
        result = await planner.plan(task, "iter-002", ctx)

        assert result.status == "accepted"
        assert result.metadata is not None
        assert result.metadata.expansion_proof != ""
        # expansion_proof should mention oracle
        assert "oracle" in result.metadata.expansion_proof.lower()

    # -----------------------------------------------------------------------
    # 11. plan() without Oracle falls back to task.target_files
    # -----------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_plan_without_oracle_uses_target_files(self, tmp_path: Path) -> None:
        from backend.core.ouroboros.governance.autonomy.iteration_planner import (
            IterationPlanner,
        )

        (tmp_path / "backend").mkdir(parents=True, exist_ok=True)
        (tmp_path / "backend" / "solo.py").write_text("# code")

        planner = IterationPlanner(
            oracle=None,
            blast_radius=BlastRadiusPolicy(max_files_changed=50),
            rejection_tracker=TaskRejectionTracker(),
            repo_root=tmp_path,
        )
        task = IterationTask(
            task_id="t3", source="test", description="small fix",
            target_files=("backend/solo.py",), repo="jarvis",
        )
        ctx = _make_context()
        result = await planner.plan(task, "iter-003", ctx)

        assert result.status == "accepted"
        assert result.graph is not None
        # The single file should be in the graph's units
        all_files = set()
        for unit in result.graph.units:
            all_files.update(unit.target_files)
        assert "backend/solo.py" in all_files

    # -----------------------------------------------------------------------
    # 12. plan() rejects on blast radius exceeded
    # -----------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_plan_rejects_blast_radius_exceeded(self, tmp_path: Path) -> None:
        from backend.core.ouroboros.governance.autonomy.iteration_planner import (
            IterationPlanner,
        )

        # Create many files to exceed the blast radius
        (tmp_path / "src").mkdir(parents=True, exist_ok=True)
        files = []
        for i in range(10):
            f = f"src/file_{i}.py"
            (tmp_path / f).write_text(f"# file {i}")
            files.append(f)

        planner = IterationPlanner(
            oracle=None,
            blast_radius=BlastRadiusPolicy(max_files_changed=3),
            rejection_tracker=TaskRejectionTracker(),
            repo_root=tmp_path,
        )
        task = IterationTask(
            task_id="t-big", source="test", description="huge refactor",
            target_files=tuple(files), repo="jarvis",
        )
        ctx = _make_context()
        result = await planner.plan(task, "iter-004", ctx)

        assert result.status == "rejected"
        assert result.reject_reason == PlannerRejectReason.BLAST_RADIUS_EXCEEDED

    # -----------------------------------------------------------------------
    # 13. T11 — plan() catches DAG cycle
    # -----------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_plan_catches_dag_cycle(self, tmp_path: Path) -> None:
        """T11 — If dependency analysis produces a cycle, plan rejects gracefully."""
        from backend.core.ouroboros.governance.autonomy.iteration_planner import (
            IterationPlanner,
        )

        # Create files
        (tmp_path / "backend").mkdir(parents=True, exist_ok=True)
        (tmp_path / "backend" / "a.py").write_text("# a")
        (tmp_path / "backend" / "b.py").write_text("# b")

        # Oracle that returns circular dependencies
        oracle = MagicMock()
        oracle.semantic_search = AsyncMock(return_value=[])

        # Create a neighborhood where a imports b and b imports a
        neighborhood = MagicMock()
        neighborhood.imports = ["jarvis:backend/b.py"]
        neighborhood.importers = ["jarvis:backend/b.py"]
        neighborhood.callers = []
        neighborhood.callees = []
        neighborhood.inheritors = []
        neighborhood.base_classes = []
        neighborhood.test_counterparts = []
        neighborhood.all_unique_files = MagicMock(return_value=[])
        oracle.get_file_neighborhood = MagicMock(return_value=neighborhood)

        planner = IterationPlanner(
            oracle=oracle,
            blast_radius=BlastRadiusPolicy(max_files_changed=50),
            rejection_tracker=TaskRejectionTracker(),
            repo_root=tmp_path,
        )

        task = IterationTask(
            task_id="t-cycle", source="test", description="cycle test",
            target_files=("backend/a.py", "backend/b.py"), repo="jarvis",
        )
        ctx = _make_context()
        result = await planner.plan(task, "iter-005", ctx)

        # Must return a PlannerOutcome, never raise
        assert isinstance(result, PlannerOutcome)
        # Even if there's a cycle, the planner should handle it gracefully.
        # It can either succeed (by collapsing the cycle into a single unit)
        # or reject with DAG_CYCLE_DETECTED.
        assert result.status in ("accepted", "rejected")
        if result.status == "rejected":
            assert result.reject_reason == PlannerRejectReason.DAG_CYCLE_DETECTED


# ---------------------------------------------------------------------------
# 14. T10 — select_acceptance_tests is deterministic
# ---------------------------------------------------------------------------

class TestSelectAcceptanceTests:
    def test_deterministic_output(self, tmp_path: Path) -> None:
        """T10 — same inputs always produce same test file list."""
        from backend.core.ouroboros.governance.autonomy.iteration_planner import (
            select_acceptance_tests,
        )

        # Create test file on disk matching Rule 1
        tests_dir = tmp_path / "tests" / "test_core"
        tests_dir.mkdir(parents=True, exist_ok=True)
        (tests_dir / "test_auth.py").write_text("# tests")

        target_files = ("backend/core/auth.py",)
        r1 = select_acceptance_tests(target_files, tmp_path)
        r2 = select_acceptance_tests(target_files, tmp_path)

        assert r1 == r2
        assert len(r1) <= 5

    def test_rule1_mapping(self, tmp_path: Path) -> None:
        """Rule 1: backend/foo/bar.py -> tests/test_foo/test_bar.py."""
        from backend.core.ouroboros.governance.autonomy.iteration_planner import (
            select_acceptance_tests,
        )

        tests_dir = tmp_path / "tests" / "test_foo"
        tests_dir.mkdir(parents=True, exist_ok=True)
        (tests_dir / "test_bar.py").write_text("# test")

        result = select_acceptance_tests(("backend/foo/bar.py",), tmp_path)
        assert "tests/test_foo/test_bar.py" in result

    def test_rule2_mapping(self, tmp_path: Path) -> None:
        """Rule 2: backend/foo/bar.py -> tests/foo/test_bar.py."""
        from backend.core.ouroboros.governance.autonomy.iteration_planner import (
            select_acceptance_tests,
        )

        tests_dir = tmp_path / "tests" / "foo"
        tests_dir.mkdir(parents=True, exist_ok=True)
        (tests_dir / "test_bar.py").write_text("# test")

        result = select_acceptance_tests(("backend/foo/bar.py",), tmp_path)
        assert "tests/foo/test_bar.py" in result

    def test_rule3_parent_dir_fallback(self, tmp_path: Path) -> None:
        """Rule 3: no direct match -> search parent dir for test_*.py."""
        from backend.core.ouroboros.governance.autonomy.iteration_planner import (
            select_acceptance_tests,
        )

        # No Rule 1 or Rule 2 match, but parent dir has a test file
        tests_dir = tmp_path / "tests" / "test_core"
        tests_dir.mkdir(parents=True, exist_ok=True)
        (tests_dir / "test_something.py").write_text("# test")

        result = select_acceptance_tests(("backend/core/widget.py",), tmp_path)
        assert "tests/test_core/test_something.py" in result

    def test_cap_at_five(self, tmp_path: Path) -> None:
        """Acceptance tests are capped at 5 per unit."""
        from backend.core.ouroboros.governance.autonomy.iteration_planner import (
            select_acceptance_tests,
        )

        tests_dir = tmp_path / "tests" / "test_foo"
        tests_dir.mkdir(parents=True, exist_ok=True)
        for i in range(10):
            (tests_dir / f"test_{i}.py").write_text(f"# test {i}")

        result = select_acceptance_tests(("backend/foo/bar.py",), tmp_path)
        assert len(result) <= 5

    def test_only_existing_files(self, tmp_path: Path) -> None:
        """Only tests that actually exist on disk are returned."""
        from backend.core.ouroboros.governance.autonomy.iteration_planner import (
            select_acceptance_tests,
        )

        # No test files created on disk
        result = select_acceptance_tests(("backend/foo/bar.py",), tmp_path)
        assert len(result) == 0


# ---------------------------------------------------------------------------
# 15. plan() canonicalizes all paths
# ---------------------------------------------------------------------------

class TestPlannerCanonicalizesPaths:
    @pytest.mark.asyncio
    async def test_paths_are_canonicalized(self, tmp_path: Path) -> None:
        from backend.core.ouroboros.governance.autonomy.iteration_planner import (
            IterationPlanner,
        )

        (tmp_path / "backend").mkdir(parents=True, exist_ok=True)
        (tmp_path / "backend" / "foo.py").write_text("# code")

        planner = IterationPlanner(
            oracle=None,
            blast_radius=BlastRadiusPolicy(max_files_changed=50),
            rejection_tracker=TaskRejectionTracker(),
            repo_root=tmp_path,
        )
        # Pass path with ./ prefix — should be canonicalized
        task = IterationTask(
            task_id="t-canon", source="test", description="canonicalize test",
            target_files=("./backend/foo.py",), repo="jarvis",
        )
        ctx = _make_context()
        result = await planner.plan(task, "iter-006", ctx)

        assert result.status == "accepted"
        assert result.graph is not None
        for unit in result.graph.units:
            for f in unit.target_files:
                assert not f.startswith("./"), f"Path not canonicalized: {f}"
            for f in unit.owned_paths:
                assert not f.startswith("./"), f"owned_path not canonicalized: {f}"
