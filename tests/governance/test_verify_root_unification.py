"""Slice 11 Task 2 — VERIFY-root unification (RED first).

Contract: *the tree you wrote is the tree you judge.* Post-APPLY scoped
verify, the PatchBenchmarker, the Slice-106 containment probe, and the
verify-gate rollback must all anchor ``config.execution_root`` (the Task-1
seam) — never the boot-time observation ``project_root`` — on BOTH
duplicated VERIFY paths (orchestrator inline + the live Slice4bRunner,
the Slice-6 T5 wired-but-inert lesson).

Anchoring discipline mirrors Slice 9's candidate-tree VALIDATE exactly:
``PythonAdapter.run`` ignores ``sandbox_dir`` (orchestrator.py:12275-12316),
so redirected VERIFY needs a per-root ``LanguageRouter(repo_root=exec_root)``
— provided by the new ``Orchestrator._scoped_verify_runner`` — not a
``sandbox_dir`` kwarg bolted onto the boot-time runner.

Run-21 evidence anchor: correct chaos repair applied in the workspace,
VERIFY judged the real tree, pass_rate=0.75 terminal on a correct fix.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.orchestrator import (
    Orchestrator,
    OrchestratorConfig,
)

ENV = "JARVIS_AUTO_COMMIT_WORKSPACE"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    yield


def _bare_orch(project_root: Path, sentinel_runner: object) -> Orchestrator:
    """Attribute-level pin of _scoped_verify_runner only — full Orchestrator
    construction drags the whole governance stack. The orchestrator's OWN
    config class is used: it is a SEPARATE frozen dataclass from
    GovernedLoopConfig, so the Task-1 property must exist on BOTH (each a
    thin delegate to the one canonical seam)."""
    orch = object.__new__(Orchestrator)
    orch._config = OrchestratorConfig(project_root=project_root)
    orch._validation_runner = sentinel_runner
    return orch


class TestOrchestratorConfigExecutionRoot:
    def test_lazy_sequencing_on_orchestrator_config(
        self, tmp_path, monkeypatch
    ):
        """OrchestratorConfig.execution_root must be the same read-time
        dynamic seam as GovernedLoopConfig's — the orchestrator is built
        before the ledger-sovereignty bootloader exports the env."""
        ws = tmp_path / "ws"
        ws.mkdir()
        repo = tmp_path / "repo"
        cfg = OrchestratorConfig(project_root=repo)
        assert cfg.execution_root == repo
        monkeypatch.setenv(ENV, str(ws))
        assert cfg.execution_root == ws
        assert cfg.project_root == repo  # observation role untouched


# ---------------------------------------------------------------------------
# 1. Behavioral: _scoped_verify_runner root selection
# ---------------------------------------------------------------------------


class TestScopedVerifyRunner:
    def test_legacy_returns_boot_runner_and_no_sandbox(self, tmp_path):
        sentinel = object()
        orch = _bare_orch(tmp_path, sentinel)
        runner, sandbox = orch._scoped_verify_runner(Path(tmp_path))
        assert runner is sentinel, (
            "exec_root == project_root must return the boot-time runner "
            "(byte-identical legacy path)"
        )
        assert sandbox is None

    def test_redirected_returns_root_anchored_router(
        self, tmp_path, monkeypatch
    ):
        ws = tmp_path / "ws"
        ws.mkdir()
        repo = tmp_path / "repo"
        repo.mkdir()
        sentinel = object()
        monkeypatch.setenv(ENV, str(ws))
        orch = _bare_orch(repo, sentinel)
        exec_root = orch._config.execution_root
        assert exec_root == ws  # Task-1 seam sanity
        runner, sandbox = orch._scoped_verify_runner(exec_root)
        assert runner is not sentinel, (
            "redirected VERIFY must NOT reuse the boot runner — "
            "PythonAdapter ignores sandbox_dir; anchoring lives in repo_root"
        )
        _root_attr = getattr(runner, "_repo_root", None) or getattr(
            runner, "repo_root", None,
        )
        assert Path(_root_attr) == ws, (
            "per-root router must anchor repo_root at the execution root "
            "(Slice 9 candidate-tree discipline)"
        )
        assert sandbox == ws


# ---------------------------------------------------------------------------
# 2. Wiring pins — BOTH duplicated VERIFY blocks + the shared benchmark
# ---------------------------------------------------------------------------


def _slice_of(path: str, start: str, end: str) -> str:
    src = Path(path).read_text()
    i = src.index(start)
    j = src.index(end, i)
    return src[i:j]


def _assert_only_observation_side_project_root(block: str, path: str) -> None:
    """Every ``.project_root`` read left inside a VERIFY block must belong to
    a known OBSERVATION-side consumer (the cluster-cascade observer feeds
    follow-up exploration against the real tree — it never judges the
    patch). Any other read is the Run-21 wrong-tree class."""
    lines = block.split("\n")
    offenders = [
        n for n, l in enumerate(lines) if ".project_root" in l
    ]
    for n in offenders:
        window = "\n".join(lines[max(0, n - 10): n + 3])
        assert "_cascade_observe" in window, (
            f"{path}: VERIFY-side '.project_root' read outside the "
            f"cascade-observer allowance:\n  {lines[n].strip()}"
        )


ORCH = "backend/core/ouroboros/governance/orchestrator.py"
RUNNER = "backend/core/ouroboros/governance/phase_runners/slice4b_runner.py"


class TestVerifyBlockWiringPins:
    def test_orchestrator_verify_block_uses_execution_root(self):
        block = _slice_of(
            ORCH, "_verify_test_passed = True", "Phase 8b: Auto-commit",
        )
        assert "execution_root" in block, (
            "orchestrator VERIFY block must resolve the execution root"
        )
        assert "_scoped_verify_runner" in block, (
            "scoped verify must select its runner through "
            "_scoped_verify_runner (per-root anchoring)"
        )
        _assert_only_observation_side_project_root(block, ORCH)

    def test_slice4b_verify_block_uses_execution_root(self):
        block = _slice_of(
            RUNNER,
            "Phase 8a: Scoped post-apply test run",
            "Phase 8b: Auto-commit",
        )
        assert "execution_root" in block
        assert "_scoped_verify_runner" in block
        _assert_only_observation_side_project_root(block, RUNNER)

    def test_run_benchmark_uses_execution_root(self):
        src = inspect.getsource(Orchestrator._run_benchmark)
        assert "execution_root" in src, (
            "PatchBenchmarker (the pass_rate source) must judge the tree "
            "APPLY wrote"
        )
        assert ".project_root" not in src

    def test_single_resolution_per_verify_pass(self):
        """Each block resolves the root ONCE into a local (no mid-verify
        env drift between the scoped run and the rollback)."""
        for path, start in (
            (ORCH, "_verify_test_passed = True"),
            (RUNNER, "Phase 8a: Scoped post-apply test run"),
        ):
            block = _slice_of(path, start, "Phase 8b: Auto-commit")
            assert block.count("execution_root") == 1, (
                f"{path}: resolve execution_root exactly once per VERIFY "
                "pass into _exec_root; downstream consumers use the local"
            )
