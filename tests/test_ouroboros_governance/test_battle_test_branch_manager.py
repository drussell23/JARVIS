"""Tests for the Ouroboros Battle Test BranchManager.

BranchManager creates a timestamped accumulation branch, commits
auto-applied operations with structured messages, and provides diff stats.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from backend.core.ouroboros.battle_test.branch_manager import BranchManager


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _init_git_repo(path: Path) -> None:
    """Create a minimal git repo with an initial commit."""
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@example.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Test User"],
        check=True,
        capture_output=True,
    )
    # Create an initial commit so HEAD exists
    readme = path / "README.md"
    readme.write_text("# Test Repo\n")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "chore: initial commit"],
        check=True,
        capture_output=True,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCreateBranch:
    """test_create_branch: verify branch name starts with prefix, git shows correct current branch."""

    def test_create_branch(self, tmp_path):
        _init_git_repo(tmp_path)
        mgr = BranchManager(repo_path=tmp_path)

        name = mgr.create_branch()

        # Branch name must start with the default prefix
        assert name.startswith("ouroboros/battle-test")

        # branch_name property must match
        assert mgr.branch_name == name

        # Git must report this as the current branch
        result = subprocess.run(
            ["git", "-C", str(tmp_path), "branch", "--show-current"],
            capture_output=True,
            text=True,
            check=True,
        )
        assert result.stdout.strip() == name

    def test_create_branch_custom_prefix(self, tmp_path):
        _init_git_repo(tmp_path)
        mgr = BranchManager(repo_path=tmp_path, branch_prefix="custom/prefix")

        name = mgr.create_branch()

        assert name.startswith("custom/prefix")


class TestCommitChanges:
    """test_commit_changes: write a file, commit, verify SHA length and log contains sensor name."""

    def test_commit_changes(self, tmp_path):
        _init_git_repo(tmp_path)
        mgr = BranchManager(repo_path=tmp_path)
        mgr.create_branch()

        # Write a file to commit
        target = tmp_path / "patch.py"
        target.write_text("x = 1\n")

        sha = mgr.commit_operation(
            files=[target],
            sensor="risk_engine",
            description="patch constant to 1",
            op_id="op-abc123",
            risk_tier="SAFE_AUTO",
            composite_score=0.1234,
            technique="direct_edit",
        )

        # SHA must be a non-empty short hex string
        assert isinstance(sha, str)
        assert len(sha) >= 7
        assert all(c in "0123456789abcdef" for c in sha)

        # commit_count increments
        assert mgr.commit_count == 1

        # Git log must contain the sensor name in the commit message
        log = subprocess.run(
            ["git", "-C", str(tmp_path), "log", "--oneline", "-1"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert "risk_engine" in log

    def test_commit_message_structure(self, tmp_path):
        _init_git_repo(tmp_path)
        mgr = BranchManager(repo_path=tmp_path)
        mgr.create_branch()

        target = tmp_path / "change.py"
        target.write_text("y = 2\n")

        mgr.commit_operation(
            files=[target],
            sensor="composite_score",
            description="update threshold",
            op_id="op-xyz789",
            risk_tier="SAFE_AUTO",
            composite_score=0.9876,
            technique="threshold_bump",
        )

        # Read the full commit message
        log_body = subprocess.run(
            ["git", "-C", str(tmp_path), "log", "-1", "--format=%B"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout

        assert "ouroboros(composite_score): update threshold" in log_body
        assert "Operation: op-xyz789" in log_body
        assert "Risk: SAFE_AUTO" in log_body
        assert "Composite Score: 0.9876" in log_body
        assert "Technique: threshold_bump" in log_body
        assert "Auto-applied: true" in log_body


class TestDiffStats:
    """test_diff_stats: commit a file, verify stats have commits >= 1, files_changed >= 1."""

    def test_diff_stats(self, tmp_path):
        _init_git_repo(tmp_path)
        mgr = BranchManager(repo_path=tmp_path)
        mgr.create_branch()

        target = tmp_path / "stats_file.py"
        target.write_text("a = 1\nb = 2\n")

        mgr.commit_operation(
            files=[target],
            sensor="analyzer",
            description="add constants",
            op_id="op-stats1",
            risk_tier="SAFE_AUTO",
            composite_score=0.5,
            technique="insert",
        )

        stats = mgr.get_diff_stats()

        assert isinstance(stats, dict)
        assert stats["commits"] >= 1
        assert stats["files_changed"] >= 1
        assert "insertions" in stats
        assert "deletions" in stats


class TestDirtyRepoAborts:
    """test_dirty_repo_aborts: add dirty file, verify create_branch raises RuntimeError."""

    def test_dirty_repo_aborts(self, tmp_path):
        _init_git_repo(tmp_path)

        # Dirty the repo — staged but not committed file
        dirty = tmp_path / "dirty.py"
        dirty.write_text("dirty content\n")
        subprocess.run(["git", "-C", str(tmp_path), "add", "dirty.py"], check=True, capture_output=True)

        mgr = BranchManager(repo_path=tmp_path)

        with pytest.raises(RuntimeError, match="dirty"):
            mgr.create_branch()

    def test_dirty_repo_unstaged_aborts(self, tmp_path):
        _init_git_repo(tmp_path)

        # Unstaged modification to tracked file
        readme = tmp_path / "README.md"
        readme.write_text("# Modified\n")

        mgr = BranchManager(repo_path=tmp_path)

        with pytest.raises(RuntimeError, match="dirty"):
            mgr.create_branch()


class TestBranchNameUniqueness:
    """test_branch_name_uniqueness: create two branches, verify names differ."""

    def test_branch_name_uniqueness(self, tmp_path):
        _init_git_repo(tmp_path)

        mgr1 = BranchManager(repo_path=tmp_path)
        name1 = mgr1.create_branch()

        # Go back to main/master so we can create a second branch
        default_branch = subprocess.run(
            ["git", "-C", str(tmp_path), "symbolic-ref", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        # Find the initial branch name (main or master)
        branches_output = subprocess.run(
            ["git", "-C", str(tmp_path), "branch"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        # Switch back to a non-ouroboros branch (first one listed that isn't name1)
        for line in branches_output.splitlines():
            candidate = line.strip().lstrip("* ")
            if candidate != name1:
                subprocess.run(
                    ["git", "-C", str(tmp_path), "checkout", candidate],
                    check=True,
                    capture_output=True,
                )
                break

        # Small sleep to ensure timestamp differs (branch names are timestamped)
        import time
        time.sleep(1.1)

        mgr2 = BranchManager(repo_path=tmp_path)
        name2 = mgr2.create_branch()

        assert name1 != name2
