"""Slice 9 — pre-GATE sandbox write-escape clamp (Slice-8 final review I1).

A model-chosen file_path of ../../x escapes tempfile sandboxes via
`sandbox / rel` + mkdir(parents=True) + write_text — an arbitrary-write
primitive that fires during VALIDATE, before any risk gate. The clamp
rejects the candidate as fc='security' BEFORE any byte lands.

Two variants exercise the SAME contract on both VALIDATE code paths:

- legacy side-sandbox write loop (JARVIS_VALIDATE_CANDIDATE_TREE_ENABLED=false)
- Slice 9 candidate-tree apply loop (flag on) — the tree loop's own clamp
  fires before `apply_full_content` lands a byte in the tree; even if it
  didn't, the tree materialization try is fail-soft and would fall back to
  the legacy path, whose clamp re-raises the same BlockedPathError. Either
  way: escaping candidate -> fc="security", no write lands anywhere.
"""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest


def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (repo / "x.py").write_text("print('orig')\n")
    git("add", "-A")
    git("commit", "-qm", "base")
    return repo


def test_dotdot_candidate_is_security_rejected_before_write(tmp_path, monkeypatch):
    """Legacy path (candidate-tree OFF): the write loop's clamp fires
    before mkdir/write_text — no byte lands outside the sandbox."""
    from backend.core.ouroboros.governance import orchestrator as om
    from backend.core.ouroboros.governance.test_runner import (
        LanguageRouter, PythonAdapter,
    )

    escape_target = tmp_path / "escaped_evil.py"
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("JARVIS_VALIDATE_CANDIDATE_TREE_ENABLED", "false")

    orch = object.__new__(om.Orchestrator)
    orch._config = om.OrchestratorConfig(project_root=repo)
    orch._validation_runner = LanguageRouter(
        repo_root=repo, adapters={"python": PythonAdapter(repo_root=repo)},
    )

    class _Ctx:
        op_id = "op-slice9-clamp"
        intake_evidence_json = ""
        target_files = ("x.py",)

    # Enough ../ to escape any tempdir depth, then an absolute-ish landing
    # inside tmp_path so we can assert nothing was written there.
    rel_escape = "../" * 12 + str(escape_target).lstrip("/")
    candidate = {"file_path": rel_escape, "full_content": "print('pwned')\n"}
    result = asyncio.run(orch._run_validation_core(_Ctx(), candidate, 30.0))

    assert not result.passed
    assert result.failure_class == "security"
    assert not escape_target.exists(), "the escaped write LANDED — clamp inert"


def test_dotdot_candidate_is_security_rejected_before_write_tree_path(tmp_path, monkeypatch):
    """Candidate-tree path (flag ON): the tree apply loop's own clamp
    fires before `apply_full_content` — no byte lands outside the tree,
    and the eventual classification is still fc='security' (whether via
    the tree loop's direct BlockedPathError->fail-soft->legacy-clamp
    re-raise, or a hypothetical direct catch — either way the escaped
    file must never exist)."""
    from backend.core.ouroboros.governance import orchestrator as om
    from backend.core.ouroboros.governance.test_runner import (
        LanguageRouter, PythonAdapter,
    )

    escape_target = tmp_path / "escaped_evil_tree.py"
    repo = _git_repo(tmp_path)
    monkeypatch.setenv("JARVIS_VALIDATE_CANDIDATE_TREE_ENABLED", "true")

    orch = object.__new__(om.Orchestrator)
    orch._config = om.OrchestratorConfig(project_root=repo)
    orch._validation_runner = LanguageRouter(
        repo_root=repo, adapters={"python": PythonAdapter(repo_root=repo)},
    )

    class _Ctx:
        op_id = "op-slice9-clamp-tree"
        intake_evidence_json = ""
        target_files = ("x.py",)

    rel_escape = "../" * 12 + str(escape_target).lstrip("/")
    candidate = {"file_path": rel_escape, "full_content": "print('pwned')\n"}
    result = asyncio.run(orch._run_validation_core(_Ctx(), candidate, 120.0))

    assert not result.passed
    assert result.failure_class == "security"
    assert not escape_target.exists(), "the escaped write LANDED — clamp inert"
