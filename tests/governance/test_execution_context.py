"""Sovereign Execution Boundary (Stage A) — canonical execution context.

`execution_context` provides the deterministic primitives the boundary
relies on:
  * is_primary_checkout(repo_root) — git --git-dir vs --git-common-dir;
    True ONLY when affirmatively the primary checkout (not a linked
    worktree). Fail-safe → False on any git error.
  * is_autonomous(repo_root, branch) — cryptographic: autonomous iff NO
    valid HMAC-signed operator-presence marker (reuses OCA's
    valid_operator_presence, NOT a fragile env bool).

TDD red: written before the module exists.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def _git(args, cwd):
    subprocess.run(
        ["git", *args], cwd=str(cwd), check=True,
        capture_output=True, text=True,
    )


def _make_primary(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q"], path)
    _git(["config", "user.email", "t@t"], path)
    _git(["config", "user.name", "t"], path)
    (path / "f.txt").write_text("x")
    _git(["add", "."], path)
    _git(["commit", "-qm", "init"], path)
    return path


def _add_worktree(primary: Path, wt_path: Path, branch="feat") -> Path:
    _git(["worktree", "add", "-b", branch, str(wt_path)], primary)
    return wt_path


# --- is_primary_checkout ---------------------------------------------------
def test_is_primary_checkout_true_in_primary(tmp_path):
    from backend.core.ouroboros.governance import execution_context as ec
    primary = _make_primary(tmp_path / "primary")
    assert ec.is_primary_checkout(primary) is True


def test_is_primary_checkout_false_in_linked_worktree(tmp_path):
    from backend.core.ouroboros.governance import execution_context as ec
    primary = _make_primary(tmp_path / "primary")
    wt = _add_worktree(primary, tmp_path / "wt")
    assert ec.is_primary_checkout(wt) is False


def test_is_primary_checkout_false_on_non_git_dir(tmp_path):
    from backend.core.ouroboros.governance import execution_context as ec
    # Fail-safe: a non-git dir cannot be CONFIRMED primary → False
    # (never block a commit on an unprovable claim).
    assert ec.is_primary_checkout(tmp_path / "not_git") is False


# --- is_autonomous (cryptographic, reuses OCA presence) --------------------
def test_is_autonomous_true_when_no_operator_presence(tmp_path, monkeypatch):
    from backend.core.ouroboros.governance import execution_context as ec
    from backend.core.ouroboros.governance import (
        operator_commit_authority as oca,
    )
    monkeypatch.setattr(
        oca, "valid_operator_presence", lambda *a, **k: False,
    )
    assert ec.is_autonomous(tmp_path, "") is True


def test_is_autonomous_false_when_operator_presence_valid(
    tmp_path, monkeypatch,
):
    from backend.core.ouroboros.governance import execution_context as ec
    from backend.core.ouroboros.governance import (
        operator_commit_authority as oca,
    )
    monkeypatch.setattr(
        oca, "valid_operator_presence", lambda *a, **k: True,
    )
    assert ec.is_autonomous(tmp_path, "") is False


def test_is_autonomous_reuses_oca_primitive_not_env_bool(
    tmp_path, monkeypatch,
):
    # Setting a fragile env bool must NOT flip autonomy — only the
    # crypto presence primitive decides.
    from backend.core.ouroboros.governance import execution_context as ec
    from backend.core.ouroboros.governance import (
        operator_commit_authority as oca,
    )
    monkeypatch.setenv("JARVIS_AUTO", "0")
    monkeypatch.setenv("JARVIS_AUTONOMOUS_EXECUTION", "0")
    monkeypatch.setattr(
        oca, "valid_operator_presence", lambda *a, **k: False,
    )
    assert ec.is_autonomous(tmp_path, "") is True


def test_functions_never_raise(tmp_path):
    from backend.core.ouroboros.governance import execution_context as ec
    # Garbage inputs must not raise.
    assert ec.is_primary_checkout(Path("/nonexistent/xyz")) is False
    assert isinstance(ec.is_autonomous(Path("/nonexistent/xyz"), ""), bool)
