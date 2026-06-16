"""Sovereign Execution Boundary (Stage A) — OCA Iron Gate.

The missing rule injected into ``operator_commit_authority.verify_pre_commit``:
an AUTONOMOUS commit that targets the PRIMARY checkout (or the main/master
branch) is DENIED — the loop must commit from an isolated worktree on a
feature branch. This closes the actual revert vector (the loop's branch/file
mutations in the operator's primary tree).

Gated by the existing OCA master flag (``JARVIS_OPERATOR_COMMIT_AUTHORITY_
ENABLED``); master-off is byte-identical (DISABLED). Reuses the existing
``DENIED_SOVEREIGNTY`` verdict (the enum is pinned to exactly 8 members).
Operator channels are unaffected — the rule lives inside the autonomous
branch only.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import (
    operator_commit_authority as oca,
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    for env in (
        "JARVIS_OPERATOR_COMMIT_AUTHORITY_ENABLED",
        "JARVIS_COMMIT_AUTHORITY_GRANTS_PATH",
        "JARVIS_COMMIT_AUTHORITY_SECRET_PATH",
        "JARVIS_COMMIT_AUTHORITY_ENABLE_FILE",
        "JARVIS_LEDGER_SOVEREIGNTY_ENABLED",
        "JARVIS_GOVERNANCE_MANIFEST_ENABLED",
        "JARVIS_EXECUTION_BOUNDARY_ENABLED",
    ):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv(
        "JARVIS_COMMIT_AUTHORITY_GRANTS_PATH", str(tmp_path / "grants.jsonl"),
    )
    monkeypatch.setenv(
        "JARVIS_COMMIT_AUTHORITY_SECRET_PATH", str(tmp_path / "secret"),
    )
    monkeypatch.setenv(
        "JARVIS_COMMIT_AUTHORITY_ENABLE_FILE", str(tmp_path / "enabled"),
    )
    yield


def _on(monkeypatch):
    """Enable OCA master AND the dedicated execution-boundary sub-flag."""
    monkeypatch.setenv("JARVIS_OPERATOR_COMMIT_AUTHORITY_ENABLED", "true")
    monkeypatch.setenv("JARVIS_EXECUTION_BOUNDARY_ENABLED", "true")


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


def _add_worktree(primary: Path, wt: Path, branch="feat") -> Path:
    _git(["worktree", "add", "-b", branch, str(wt)], primary)
    return wt


def _ctx(repo_root, *, channel="autonomous", branch="", staged=()):
    return oca.CommitAuthorityContext(
        channel=channel, repo_root=str(repo_root),
        branch=branch, staged_files=tuple(staged),
    )


# --- the core boundary -----------------------------------------------------
def test_autonomous_commit_in_primary_checkout_denied(tmp_path, monkeypatch):
    _on(monkeypatch)
    primary = _make_primary(tmp_path / "primary")
    v = oca.verify_pre_commit(_ctx(primary))
    assert v.verdict is oca.CommitAuthorityVerdict.DENIED_SOVEREIGNTY
    assert v.authorized() is False


def test_autonomous_commit_in_worktree_authorized(tmp_path, monkeypatch):
    _on(monkeypatch)
    primary = _make_primary(tmp_path / "primary")
    wt = _add_worktree(primary, tmp_path / "wt", branch="feat")
    v = oca.verify_pre_commit(_ctx(wt, branch="feat"))
    # Boundary passes (worktree); ledger_sovereignty master off → the
    # autonomous channel authorizes.
    assert v.verdict is oca.CommitAuthorityVerdict.AUTHORIZED
    assert v.authorized() is True


def test_autonomous_commit_to_main_branch_denied(tmp_path, monkeypatch):
    _on(monkeypatch)
    primary = _make_primary(tmp_path / "primary")
    wt = _add_worktree(primary, tmp_path / "wt", branch="feat")
    # Even in a worktree, targeting main/master is refused (defense in
    # depth — mirrors pre-push protection).
    v = oca.verify_pre_commit(_ctx(wt, branch="main"))
    assert v.verdict is oca.CommitAuthorityVerdict.DENIED_SOVEREIGNTY
    assert v.authorized() is False


# --- gating (byte-identical when master off) -------------------------------
def test_master_off_is_disabled_even_in_primary(tmp_path, monkeypatch):
    primary = _make_primary(tmp_path / "primary")
    v = oca.verify_pre_commit(_ctx(primary))
    assert v.verdict is oca.CommitAuthorityVerdict.DISABLED
    assert v.authorized() is True


# --- operator channels are unaffected by the boundary ----------------------
def test_operator_channel_in_primary_not_boundary_denied(
    tmp_path, monkeypatch,
):
    _on(monkeypatch)
    primary = _make_primary(tmp_path / "primary")
    # An operator (ide) commit in the primary checkout is legitimate; the
    # boundary must NOT fire. It fails for the ORDINARY reason (no grant),
    # proving the boundary is autonomous-only.
    v = oca.verify_pre_commit(_ctx(primary, channel="ide"))
    assert v.verdict is oca.CommitAuthorityVerdict.DENIED_NO_GRANT


def test_oca_on_boundary_off_is_byte_identical_legacy(tmp_path, monkeypatch):
    # OCA master ON but the boundary sub-flag OFF → the autonomous channel
    # keeps its documented legacy contract (authorized when sovereignty
    # off), proving the boundary is a pure additive opt-in.
    monkeypatch.setenv("JARVIS_OPERATOR_COMMIT_AUTHORITY_ENABLED", "true")
    monkeypatch.delenv("JARVIS_EXECUTION_BOUNDARY_ENABLED", raising=False)
    primary = _make_primary(tmp_path / "primary")
    v = oca.verify_pre_commit(_ctx(primary))
    assert v.verdict is oca.CommitAuthorityVerdict.AUTHORIZED


def test_boundary_never_raises_on_garbage_repo_root(tmp_path, monkeypatch):
    _on(monkeypatch)
    # Non-git repo_root → is_primary_checkout False → boundary passes,
    # autonomous authorizes (no wedge on detection failure).
    v = oca.verify_pre_commit(_ctx(tmp_path / "not_git"))
    assert v.verdict is oca.CommitAuthorityVerdict.AUTHORIZED
