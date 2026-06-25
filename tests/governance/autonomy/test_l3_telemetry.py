"""Per-subagent L3 telemetry contract.

Covers the L3 fan-out resource/cost footprint visibility:
  1. WorkUnitResult carries ``worktree_lifespan_s`` measured create->reap
     (mocked monotonic) when a worktree manager is present.
  2. WorkUnitResult carries ``dw_cost_usd`` threaded from the generation
     result's ``cost_usd`` (honest-null when the result reports no cost).
  3. Per-unit ``[L3Telemetry] unit=... lifespan_s=... dw_cost_usd=...`` and
     graph-aggregate ``[L3Telemetry] graph=... total_lifespan_s=...
     total_dw_cost_usd=...`` lines emit the right values.
  4. A telemetry exception is swallowed (the unit result is unaffected) ->
     fail-soft.
  5. ``JARVIS_L3_TELEMETRY_ENABLED=false`` -> no L3Telemetry lines, the
     returned result is byte-identical (no telemetry fields populated by
     the telemetry layer itself beyond what the data path provides).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

import pytest

from backend.core.ouroboros.governance.autonomy.subagent_scheduler import (
    GenerationSubagentExecutor,
)
from backend.core.ouroboros.governance.autonomy.subagent_types import (
    ExecutionGraph,
    WorkUnitResult,
    WorkUnitSpec,
    WorkUnitState,
)


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------
class _Generation:
    """Minimal generation result mirroring GenerationResult's relevant shape."""

    def __init__(
        self,
        *,
        candidates: tuple,
        cost_usd: Optional[float] = None,
        is_noop: bool = False,
    ) -> None:
        self.is_noop = is_noop
        self.candidates = candidates
        if cost_usd is not None:
            self.cost_usd = cost_usd


class _Generator:
    def __init__(self, generation: Any) -> None:
        self._generation = generation

    async def generate(self, ctx: Any, deadline: Any) -> Any:
        return self._generation


class _OkWorktreeManager:
    """Worktree manager whose create()/cleanup() always succeed."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self.create_calls = 0
        self.cleanup_calls = 0

    async def create(self, branch_name: str) -> Path:
        self.create_calls += 1
        self._path.mkdir(parents=True, exist_ok=True)
        return self._path

    async def cleanup(self, worktree_path: Path) -> None:
        self.cleanup_calls += 1


def _candidate() -> dict:
    return {"file_path": "jarvis/a.py", "full_content": "x = 1\n"}


def _make_unit_graph() -> tuple[ExecutionGraph, WorkUnitSpec]:
    unit = WorkUnitSpec(
        unit_id="u1",
        repo="jarvis",
        goal="update a",
        target_files=("jarvis/a.py",),
        owned_paths=("jarvis/a.py",),
    )
    graph = ExecutionGraph(
        graph_id="graph-l3-tel",
        op_id="op-l3-tel",
        planner_id="planner-test",
        schema_version="2d.1",
        concurrency_limit=1,
        units=(unit,),
    )
    return graph, unit


def _executor(
    tmp_path: Path,
    *,
    cost_usd: Optional[float],
    worktree: bool,
) -> tuple[GenerationSubagentExecutor, Optional[_OkWorktreeManager]]:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(exist_ok=True)
    (repo_root / "jarvis").mkdir(exist_ok=True)
    gen = _Generation(candidates=(_candidate(),), cost_usd=cost_usd)
    mgr = _OkWorktreeManager(tmp_path / "wt") if worktree else None
    ex = GenerationSubagentExecutor(
        generator=_Generator(gen),
        validation_runner=None,
        repo_roots={"jarvis": repo_root},
        worktree_manager=mgr,
    )
    return ex, mgr


# ---------------------------------------------------------------------------
# 1. Worktree lifespan create->reap
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_worktree_lifespan_measured_create_to_reap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVIS_L3_TELEMETRY_ENABLED", "true")

    # Drive time.monotonic deterministically: create stamp = 100.0,
    # reap stamp = 103.5 -> lifespan 3.5s.
    import backend.core.ouroboros.governance.autonomy.subagent_scheduler as mod

    seq = iter([100.0, 103.5])
    fixed = [100.0]

    def fake_monotonic() -> float:
        try:
            fixed[0] = next(seq)
        except StopIteration:
            pass
        return fixed[0]

    monkeypatch.setattr(mod.time, "monotonic", fake_monotonic)

    ex, mgr = _executor(tmp_path, cost_usd=0.42, worktree=True)
    graph, unit = _make_unit_graph()
    result = await ex.execute(graph, unit)

    assert result.status is WorkUnitState.COMPLETED
    assert mgr is not None and mgr.create_calls == 1 and mgr.cleanup_calls == 1
    assert result.worktree_lifespan_s is not None
    assert abs(result.worktree_lifespan_s - 3.5) < 1e-6


@pytest.mark.asyncio
async def test_no_worktree_lifespan_is_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No worktree manager -> no lifespan to honestly report."""
    monkeypatch.setenv("JARVIS_L3_TELEMETRY_ENABLED", "true")
    ex, _ = _executor(tmp_path, cost_usd=0.10, worktree=False)
    graph, unit = _make_unit_graph()
    result = await ex.execute(graph, unit)
    assert result.status is WorkUnitState.COMPLETED
    assert result.worktree_lifespan_s is None


# ---------------------------------------------------------------------------
# 2. DW cost threaded from generation result (honest-null absent)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_dw_cost_threaded_from_generation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_L3_TELEMETRY_ENABLED", "true")
    ex, _ = _executor(tmp_path, cost_usd=0.42, worktree=False)
    graph, unit = _make_unit_graph()
    result = await ex.execute(graph, unit)
    assert result.dw_cost_usd is not None
    assert abs(result.dw_cost_usd - 0.42) < 1e-9


@pytest.mark.asyncio
async def test_dw_cost_honest_null_when_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Generation result with no cost_usd attribute -> honest None (no fabrication)."""
    monkeypatch.setenv("JARVIS_L3_TELEMETRY_ENABLED", "true")
    ex, _ = _executor(tmp_path, cost_usd=None, worktree=False)
    graph, unit = _make_unit_graph()
    result = await ex.execute(graph, unit)
    assert result.status is WorkUnitState.COMPLETED
    assert result.dw_cost_usd is None


# ---------------------------------------------------------------------------
# 3. Per-unit telemetry line emits the right values
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_per_unit_line_emitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("JARVIS_L3_TELEMETRY_ENABLED", "true")
    ex, _ = _executor(tmp_path, cost_usd=0.42, worktree=False)
    graph, unit = _make_unit_graph()
    with caplog.at_level(logging.INFO, logger="Ouroboros.SubagentScheduler"):
        await ex.execute(graph, unit)
    lines = [r.message for r in caplog.records if "[L3Telemetry]" in r.message]
    assert any("unit=u1" in m for m in lines), lines
    line = next(m for m in lines if "unit=u1" in m)
    assert "dw_cost_usd=0.42" in line
    assert "status=" in line


@pytest.mark.asyncio
async def test_off_emits_no_lines_byte_identical_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("JARVIS_L3_TELEMETRY_ENABLED", "false")
    ex, _ = _executor(tmp_path, cost_usd=0.42, worktree=False)
    graph, unit = _make_unit_graph()
    with caplog.at_level(logging.INFO, logger="Ouroboros.SubagentScheduler"):
        result = await ex.execute(graph, unit)
    lines = [r.message for r in caplog.records if "[L3Telemetry]" in r.message]
    assert lines == []
    # OFF -> telemetry fields untouched by the telemetry layer.
    assert result.worktree_lifespan_s is None
    assert result.dw_cost_usd is None
    assert result.status is WorkUnitState.COMPLETED


# ---------------------------------------------------------------------------
# 4. Fail-soft: a telemetry exception never affects the unit result
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_telemetry_exception_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVIS_L3_TELEMETRY_ENABLED", "true")
    import backend.core.ouroboros.governance.autonomy.subagent_scheduler as mod

    # Make the per-unit telemetry emit raise; the unit result must survive.
    def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("telemetry blew up")

    monkeypatch.setattr(mod, "_emit_unit_telemetry", boom, raising=True)

    ex, _ = _executor(tmp_path, cost_usd=0.42, worktree=False)
    graph, unit = _make_unit_graph()
    result = await ex.execute(graph, unit)
    assert result.status is WorkUnitState.COMPLETED
    assert result.patch is not None


# ---------------------------------------------------------------------------
# 5. Graph-aggregate line
# ---------------------------------------------------------------------------
def test_graph_aggregate_line(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setenv("JARVIS_L3_TELEMETRY_ENABLED", "true")
    from backend.core.ouroboros.governance.autonomy.subagent_scheduler import (
        _emit_graph_telemetry,
    )

    results = {
        "u1": WorkUnitResult(
            unit_id="u1",
            repo="jarvis",
            status=WorkUnitState.COMPLETED,
            patch=None,
            attempt_count=1,
            started_at_ns=0,
            finished_at_ns=1,
            worktree_lifespan_s=3.5,
            dw_cost_usd=0.42,
        ),
        "u2": WorkUnitResult(
            unit_id="u2",
            repo="jarvis",
            status=WorkUnitState.COMPLETED,
            patch=None,
            attempt_count=1,
            started_at_ns=0,
            finished_at_ns=1,
            worktree_lifespan_s=1.5,
            dw_cost_usd=None,  # honest-null contributes 0 to the sum
        ),
    }
    with caplog.at_level(logging.INFO, logger="Ouroboros.SubagentScheduler"):
        _emit_graph_telemetry("graph-x", results)
    lines = [r.message for r in caplog.records if "[L3Telemetry]" in r.message]
    assert any("graph=graph-x" in m for m in lines), lines
    line = next(m for m in lines if "graph=graph-x" in m)
    assert "units=2" in line
    assert "total_lifespan_s=5.0" in line
    assert "total_dw_cost_usd=0.42" in line


def test_graph_aggregate_off_no_line(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setenv("JARVIS_L3_TELEMETRY_ENABLED", "false")
    from backend.core.ouroboros.governance.autonomy.subagent_scheduler import (
        _emit_graph_telemetry,
    )

    with caplog.at_level(logging.INFO, logger="Ouroboros.SubagentScheduler"):
        _emit_graph_telemetry("graph-x", {})
    assert [r.message for r in caplog.records if "[L3Telemetry]" in r.message] == []
