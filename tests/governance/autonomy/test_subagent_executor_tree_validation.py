"""An L3 unit's candidate is validated IN ITS WORKTREE, and a failure says why.

Every failed unit of 2026-09-07 was the ``test_speech_provider.py`` slice:
the executor wrote the candidate as a bare file in ``/tmp``, ran it outside
the tree (no differential baseline), and reported the literal
``"validation failed"``. These tests pin the candidate-tree contract for a
unit: real relative path inside the unit's worktree, tree restored so the
patch is computed against the pre-image, ambient-red tests excluded for a
test-authoring candidate, and an evidence-bearing failure text.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from backend.core.ouroboros.governance.autonomy.subagent_scheduler import (
    GenerationSubagentExecutor,
)
from backend.core.ouroboros.governance.autonomy.subagent_types import (
    ExecutionGraph,
    WorkUnitSpec,
    WorkUnitState,
)
from backend.core.ouroboros.governance.test_runner import (
    AdapterResult,
    MultiAdapterResult,
    TestResult,
    failure_digest,
)

REL = "tests/pkg/test_thing.py"
ORIGINAL = "def test_old():\n    assert 1 == 1\n"
CANDIDATE = ORIGINAL + "\n\ndef test_new():\n    assert 2 == 2\n"


def _multi(passed: bool, failed: Tuple[str, ...] = (), *, timed_out: bool = False, stdout: str = "") -> MultiAdapterResult:
    tr = TestResult(
        passed=passed, total=2, failed=len(failed), failed_tests=failed,
        duration_seconds=0.1, stdout=stdout, flake_suspected=False, timed_out=timed_out,
    )
    ar = AdapterResult(
        adapter="python", passed=passed,
        failure_class="none" if passed else ("infra" if timed_out else "test"),
        test_result=tr, duration_s=0.1,
    )
    return MultiAdapterResult(
        passed=passed, adapter_results=(ar,), dominant_failure=None if passed else ar, total_duration_s=0.1,
    )


class _Runner:
    """Records every run; returns scripted verdicts in order."""

    def __init__(self, verdicts: List[MultiAdapterResult]) -> None:
        self.verdicts = list(verdicts)
        self.calls: List[Dict[str, Any]] = []

    async def run(self, *, changed_files, sandbox_dir, timeout_budget_s, op_id, original_paths=None):
        content = Path(changed_files[0]).read_text(encoding="utf-8") if Path(changed_files[0]).is_file() else None
        self.calls.append({
            "changed_files": tuple(changed_files), "sandbox_dir": Path(sandbox_dir),
            "original_paths": original_paths, "content_at_run": content,
        })
        return self.verdicts.pop(0)


class _Gen:
    def __init__(self, content: str = CANDIDATE, file_path: str = REL) -> None:
        self.content, self.file_path = content, file_path

    async def generate(self, ctx: Any, deadline: Any) -> Any:
        class _G:
            is_noop = False
            cost_usd = 0.0
            candidates = [{"file_path": self.file_path, "full_content": self.content}]
        return _G()


class _WT:
    """Worktree manager whose worktree is a pre-populated directory."""

    def __init__(self, path: Path) -> None:
        self.path, self.cleaned = path, 0

    async def create(self, branch_name: str) -> Path:
        return self.path

    async def cleanup(self, worktree_path: Path) -> None:
        self.cleaned += 1


def _graph(op_id: str = "op-tree") -> Tuple[ExecutionGraph, WorkUnitSpec]:
    unit = WorkUnitSpec(unit_id="u1", repo="jarvis", goal="author test_new", target_files=(REL,), owned_paths=(REL,))
    graph = ExecutionGraph(graph_id="g1", op_id=op_id, planner_id="p", schema_version="2d.1", concurrency_limit=1, units=(unit,))
    return graph, unit


def _setup(tmp_path: Path) -> Tuple[Path, Path]:
    repo = tmp_path / "repo"
    wt = tmp_path / "repo" / ".worktrees" / "u1"
    (wt / "tests/pkg").mkdir(parents=True)
    (wt / REL).write_text(ORIGINAL, encoding="utf-8")
    return repo, wt


@pytest.mark.asyncio
async def test_candidate_is_validated_at_its_real_path_and_tree_restored(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_DIFFERENTIAL_VALIDATE_ENABLED", "false")
    repo, wt = _setup(tmp_path)
    runner = _Runner([_multi(True)])
    ex = GenerationSubagentExecutor(generator=_Gen(), validation_runner=runner, repo_roots={"jarvis": repo}, worktree_manager=_WT(wt))
    graph, unit = _graph()
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.test_runner.tree_language_router", lambda tree, base: runner,
    )
    res = await ex.execute(graph, unit)
    assert res.status is WorkUnitState.COMPLETED, res.error
    call = runner.calls[0]
    assert call["changed_files"] == (wt / REL,), "validated inside the unit's worktree at the real relative path"
    assert call["sandbox_dir"] == wt
    assert call["original_paths"] == {wt / REL: wt / REL}
    assert call["content_at_run"] == CANDIDATE, "the CANDIDATE was under test, not the on-disk file"
    assert (wt / REL).read_text(encoding="utf-8") == ORIGINAL, "tree restored: the patch is computed against the pre-image"
    assert res.patch is not None and res.patch.files, "patch produced from pre-image → candidate"


@pytest.mark.asyncio
async def test_failure_carries_the_runner_evidence(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_DIFFERENTIAL_VALIDATE_ENABLED", "false")
    repo, wt = _setup(tmp_path)
    runner = _Runner([_multi(False, ("tests/pkg/test_thing.py::test_new",), stdout="E  assert 2 == 3")])
    ex = GenerationSubagentExecutor(generator=_Gen(), validation_runner=runner, repo_roots={"jarvis": repo}, worktree_manager=_WT(wt))
    monkeypatch.setattr("backend.core.ouroboros.governance.test_runner.tree_language_router", lambda t, b: runner)
    res = await ex.execute(*_graph())
    assert res.status is WorkUnitState.FAILED
    assert res.failure_class == "test"
    assert res.error != "validation failed"
    assert "tests/pkg/test_thing.py::test_new" in res.error and "assert 2 == 3" in res.error
    assert (wt / REL).read_text(encoding="utf-8") == ORIGINAL


@pytest.mark.asyncio
async def test_ambient_red_is_excluded_for_a_test_authoring_candidate(tmp_path: Path, monkeypatch) -> None:
    """The baseline run (before the candidate lands) is red on test_old; the
    candidate run fails on exactly that id → differential verdict passes."""
    monkeypatch.setenv("JARVIS_DIFFERENTIAL_VALIDATE_ENABLED", "true")
    repo, wt = _setup(tmp_path)
    ambient = ("tests/pkg/test_thing.py::test_old",)
    runner = _Runner([_multi(False, ambient), _multi(False, ambient)])
    ex = GenerationSubagentExecutor(generator=_Gen(), validation_runner=runner, repo_roots={"jarvis": repo}, worktree_manager=_WT(wt))
    monkeypatch.setattr("backend.core.ouroboros.governance.test_runner.tree_language_router", lambda t, b: runner)
    res = await ex.execute(*_graph())
    assert res.status is WorkUnitState.COMPLETED, res.error
    assert len(runner.calls) == 2
    assert runner.calls[0]["content_at_run"] == ORIGINAL, "baseline measured BEFORE the candidate lands"
    assert runner.calls[1]["content_at_run"] == CANDIDATE


@pytest.mark.asyncio
async def test_new_failure_is_not_masked_by_the_baseline(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_DIFFERENTIAL_VALIDATE_ENABLED", "true")
    repo, wt = _setup(tmp_path)
    runner = _Runner([
        _multi(False, ("tests/pkg/test_thing.py::test_old",)),
        _multi(False, ("tests/pkg/test_thing.py::test_old", "tests/pkg/test_thing.py::test_new")),
    ])
    ex = GenerationSubagentExecutor(generator=_Gen(), validation_runner=runner, repo_roots={"jarvis": repo}, worktree_manager=_WT(wt))
    monkeypatch.setattr("backend.core.ouroboros.governance.test_runner.tree_language_router", lambda t, b: runner)
    res = await ex.execute(*_graph())
    assert res.status is WorkUnitState.FAILED
    assert "test_new" in res.error


@pytest.mark.asyncio
async def test_escaping_path_is_refused_before_any_write(tmp_path: Path, monkeypatch) -> None:
    repo, wt = _setup(tmp_path)
    runner = _Runner([_multi(True)])
    ex = GenerationSubagentExecutor(
        generator=_Gen(file_path="../outside/test_x.py"), validation_runner=runner,
        repo_roots={"jarvis": repo}, worktree_manager=_WT(wt),
    )
    graph, unit = _graph()
    unit = WorkUnitSpec(unit_id="u1", repo="jarvis", goal="g", target_files=("../outside/test_x.py",), owned_paths=("../outside/test_x.py",))
    res = await ex.execute(graph, unit)
    assert res.status is WorkUnitState.FAILED
    assert res.failure_class == "security"
    assert runner.calls == []
    assert not (tmp_path / "repo" / "outside").exists()


@pytest.mark.asyncio
async def test_new_file_candidate_is_removed_after_validation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_DIFFERENTIAL_VALIDATE_ENABLED", "true")
    repo, wt = _setup(tmp_path)
    new_rel = "tests/pkg/test_fresh.py"
    runner = _Runner([_multi(True)])
    ex = GenerationSubagentExecutor(
        generator=_Gen(content="def test_fresh():\n    assert True\n", file_path=new_rel),
        validation_runner=runner, repo_roots={"jarvis": repo}, worktree_manager=_WT(wt),
    )
    monkeypatch.setattr("backend.core.ouroboros.governance.test_runner.tree_language_router", lambda t, b: runner)
    graph, _ = _graph()
    unit = WorkUnitSpec(unit_id="u1", repo="jarvis", goal="g", target_files=(new_rel,), owned_paths=(new_rel,))
    res = await ex.execute(graph, unit)
    assert res.status is WorkUnitState.COMPLETED, res.error
    assert len(runner.calls) == 1, "no baseline for a file that does not exist yet"
    assert not (wt / new_rel).exists(), "a new-file candidate leaves no residue in the tree"


@pytest.mark.asyncio
async def test_no_worktree_keeps_legacy_tmp_path_but_reports_evidence(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    runner = _Runner([_multi(False, ("x::test_a",), timed_out=True)])
    ex = GenerationSubagentExecutor(generator=_Gen(), validation_runner=runner, repo_roots={"jarvis": repo}, worktree_manager=None)
    res = await ex.execute(*_graph())
    assert res.status is WorkUnitState.FAILED
    assert res.failure_class == "infra"
    assert "timed out" in res.error and "x::test_a" in res.error
    assert runner.calls[0]["changed_files"][0].name == "test_thing.py"
    assert "ouroboros_l3_validate_" in str(runner.calls[0]["sandbox_dir"])


def test_failure_digest_shapes() -> None:
    assert failure_digest(_multi(True)) == ""
    d = failure_digest(_multi(False, tuple(f"t.py::test_{i}" for i in range(8)), stdout="tail text"))
    assert d.startswith("python test; 8 failed: t.py::test_0")
    assert "(+2)" in d and d.endswith("tail text")
    assert failure_digest(None) == "validation failed"
    assert "no tests collected" in failure_digest(MultiAdapterResult(
        passed=False, adapter_results=(), total_duration_s=0.0,
        dominant_failure=AdapterResult(
            adapter="python", passed=False, failure_class="test", duration_s=0.0,
            test_result=TestResult(passed=False, total=0, failed=0, failed_tests=(), duration_seconds=0.0, stdout="", flake_suspected=False),
        ),
    ))
