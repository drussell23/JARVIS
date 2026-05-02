"""GenerationSubagentExecutor worktree-isolation contract.

Covers the Manifesto §1 Boundary / §6 Iron Gate rule: if isolation was
promised (worktree_manager is present) and cannot be obtained, the unit
must fail hard rather than silently running against the shared tree.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from backend.core.ouroboros.governance.autonomy.subagent_scheduler import (
    GenerationSubagentExecutor,
)
from backend.core.ouroboros.governance.autonomy.subagent_types import (
    ExecutionGraph,
    WorkUnitSpec,
    WorkUnitState,
)


class _SpyGenerator:
    """Generator stub that records calls. generate() must never be reached
    when worktree create fails."""

    def __init__(self) -> None:
        self.generate_calls: list[tuple[Any, Any]] = []

    async def generate(self, ctx: Any, deadline: Any) -> Any:
        self.generate_calls.append((ctx, deadline))
        raise AssertionError(
            "generate() must not be called after worktree_create failure"
        )


class _FailingWorktreeManager:
    """Worktree manager whose create() always raises.

    Models the failure modes reap_orphans and shutil.rmtree exist to
    recover from: disk full, permission denied, git worktree add rejecting
    the path (e.g. 'already exists').
    """

    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.create_calls: int = 0
        self.cleanup_calls: int = 0

    async def create(self, branch_name: str) -> Path:
        self.create_calls += 1
        raise self._exc

    async def cleanup(self, worktree_path: Path) -> None:
        self.cleanup_calls += 1


def _make_unit_graph(repo_root: Path) -> tuple[ExecutionGraph, WorkUnitSpec]:
    unit = WorkUnitSpec(
        unit_id="u1",
        repo="jarvis",
        goal="update a",
        target_files=("jarvis/a.py",),
        owned_paths=("jarvis/a.py",),
    )
    graph = ExecutionGraph(
        graph_id="graph-wt-fail",
        op_id="op-wt-fail",
        planner_id="planner-test",
        schema_version="2d.1",
        concurrency_limit=1,
        units=(unit,),
    )
    return graph, unit


@pytest.mark.asyncio
async def test_execute_fails_hard_when_worktree_create_raises(tmp_path: Path) -> None:
    """§1 Boundary: promised isolation must be obtained or unit fails.

    Verifies:
      1. result.status == FAILED
      2. failure_class == "worktree_isolation" (cascading state vector fix:
         decoupled from generic "infra" to prevent retry flapping)
      3. error carries "worktree_create_failed:" marker with the original
         exception type
      4. generator.generate() was never called (the shared tree was never
         touched)
      5. worktree_manager.cleanup() was not called (no path was created)
    """
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    failing_mgr = _FailingWorktreeManager(
        RuntimeError("git worktree add failed (rc=128): path already exists")
    )
    spy_generator = _SpyGenerator()

    executor = GenerationSubagentExecutor(
        generator=spy_generator,
        validation_runner=None,
        repo_roots={"jarvis": repo_root},
        worktree_manager=failing_mgr,
    )

    graph, unit = _make_unit_graph(repo_root)
    result = await executor.execute(graph, unit)

    assert result.status is WorkUnitState.FAILED
    assert result.failure_class == "worktree_isolation"
    assert result.error is not None
    assert result.error.startswith("worktree_create_failed:"), (
        f"expected worktree_create_failed: marker, got: {result.error}"
    )
    assert "RuntimeError" in result.error, (
        f"expected original exception type preserved, got: {result.error}"
    )

    assert failing_mgr.create_calls == 1, "worktree create must be attempted once"
    assert failing_mgr.cleanup_calls == 0, (
        "cleanup must not be called when no path was ever created"
    )
    assert spy_generator.generate_calls == [], (
        "generate() must not be called after worktree create failure"
    )

    assert result.patch is None


@pytest.mark.asyncio
async def test_execute_reports_no_op_when_generator_returns_noop(tmp_path: Path) -> None:
    """Sanity: success path still works when worktree_manager is None.

    Regression cover — ensures the hard-fail change did not accidentally
    break the no-worktree (shared-tree) mode, which is the only path when
    l3_enable_worktree_isolation is disabled.
    """
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    class _NoopGeneration:
        is_noop = True
        candidates: tuple = ()

    class _NoopGenerator:
        async def generate(self, ctx: Any, deadline: Any) -> Any:
            return _NoopGeneration()

    executor = GenerationSubagentExecutor(
        generator=_NoopGenerator(),
        validation_runner=None,
        repo_roots={"jarvis": repo_root},
        worktree_manager=None,
    )

    graph, unit = _make_unit_graph(repo_root)
    result = await executor.execute(graph, unit)

    assert result.status is WorkUnitState.COMPLETED
    assert result.failure_class is None or result.failure_class == ""
