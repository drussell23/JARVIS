"""Slice 9 — VALIDATE must exercise the CANDIDATE, not the broken tree.

Run #19: the correct source repair failed VALIDATE with fc=test because
pytest ran from the main repo root against the still-broken working tree
while the candidate's fix sat inert in a side sandbox. This test builds a
tiny broken repo + a correct candidate and drives the REAL
_run_validation_core seam: candidate-tree ON -> passes; OFF -> the legacy
behavior fails (pinning exactly the Run-19 class)."""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def broken_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "tests").mkdir()
    def git(*args):
        subprocess.run(["git", *args], cwd=repo, check=True,
                       capture_output=True)
    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (repo / "pytest.ini").write_text("[pytest]\naddopts = -p no:cacheprovider\n")
    (repo / "pkg" / "__init__.py").write_text("")
    (repo / "pkg" / "mod.py").write_text("def f():\n    return 1\n")
    (repo / "tests" / "test_mod.py").write_text(
        "import sys, pathlib\n"
        "sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))\n"
        "from pkg.mod import f\n\n"
        "def test_f():\n    assert f() == 1\n"
    )
    git("add", "-A")
    git("commit", "-qm", "base")
    # Break the WORKING TREE (uncommitted — the battle-test chaos shape).
    (repo / "pkg" / "mod.py").write_text("def f():\n    return 2\n")
    return repo


_FIXED = "def f():\n    return 1\n"
_BROKEN = "def f():\n    return 2\n"


def _run_validation_for(repo, file_rel: str, content: str, monkeypatch, tree_enabled: bool):
    monkeypatch.setenv(
        "JARVIS_VALIDATE_CANDIDATE_TREE_ENABLED",
        "true" if tree_enabled else "false",
    )
    from backend.core.ouroboros.governance import orchestrator as om

    orch = object.__new__(om.Orchestrator)
    # Minimal config seam: _run_validation_core reads project_root and the
    # boot-time validation runner. Verified by reading _run_validation_core
    # (only self._config, self._validation_runner, and three @staticmethods
    # are dereferenced before the candidate-tree block).
    orch._config = om.OrchestratorConfig(project_root=repo)
    from backend.core.ouroboros.governance.test_runner import (
        LanguageRouter, PythonAdapter,
    )
    orch._validation_runner = LanguageRouter(
        repo_root=repo, adapters={"python": PythonAdapter(repo_root=repo)},
    )

    class _Ctx:
        op_id = "op-slice9-tree-pin"
        intake_evidence_json = ""
        target_files = ("pkg/mod.py", "tests/test_mod.py")

    candidate = {"file_path": file_rel, "full_content": content}
    return asyncio.run(orch._run_validation_core(_Ctx(), candidate, 120.0))


def _run_validation(repo, monkeypatch, tree_enabled: bool):
    return _run_validation_for(repo, "pkg/mod.py", _FIXED, monkeypatch, tree_enabled)


def test_candidate_tree_on_correct_repair_validates_green(broken_repo, monkeypatch):
    result = _run_validation(broken_repo, monkeypatch, tree_enabled=True)
    assert result.passed, f"fc={result.failure_class} err={result.error}"


def test_candidate_tree_on_wrong_repair_fails(broken_repo, monkeypatch):
    """Wrong candidate (keeps the broken content) must FAIL fc=test —
    proving the tree run exercises the candidate, not vacuously passing."""
    result = _run_validation_for(
        broken_repo, "pkg/mod.py", _BROKEN,
        monkeypatch, tree_enabled=True,
    )
    assert not result.passed
    assert result.failure_class == "test"


def test_legacy_path_pins_run19_class(broken_repo, monkeypatch):
    """Flag OFF: the legacy side-sandbox path fails fc=test on the correct
    repair — THE Run-19 class, pinned so we notice if legacy semantics
    ever silently change."""
    result = _run_validation(broken_repo, monkeypatch, tree_enabled=False)
    assert not result.passed
    assert result.failure_class == "test"
