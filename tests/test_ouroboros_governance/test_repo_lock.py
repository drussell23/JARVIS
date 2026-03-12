"""Tests for RepoLockManager — two-tier saga locking."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.saga.repo_lock import RepoLockManager


@pytest.fixture
def repo_roots(tmp_path: Path) -> dict[str, Path]:
    """Create fake repo roots with .jarvis dirs."""
    roots: dict[str, Path] = {}
    for name in ("jarvis", "prime", "reactor-core"):
        root = tmp_path / name
        root.mkdir()
        (root / ".jarvis").mkdir()
        (root / ".git").mkdir()  # minimal git marker
        roots[name] = root
    return roots


class TestAcquireRelease:
    async def test_acquire_and_release(self, repo_roots: dict[str, Path]) -> None:
        mgr = RepoLockManager()
        repos = ["jarvis", "prime"]
        await mgr.acquire(repos, repo_roots)
        # Lock files should exist
        for name in repos:
            lock_file = repo_roots[name] / ".jarvis" / "saga.lock"
            assert lock_file.exists()
            data = json.loads(lock_file.read_text())
            assert data["pid"] == os.getpid()
        await mgr.release(repos)

    async def test_release_removes_lock_files(self, repo_roots: dict[str, Path]) -> None:
        mgr = RepoLockManager()
        repos = ["jarvis"]
        await mgr.acquire(repos, repo_roots)
        await mgr.release(repos)
        lock_file = repo_roots["jarvis"] / ".jarvis" / "saga.lock"
        assert not lock_file.exists()

    async def test_double_release_is_safe(self, repo_roots: dict[str, Path]) -> None:
        mgr = RepoLockManager()
        repos = ["jarvis"]
        await mgr.acquire(repos, repo_roots)
        await mgr.release(repos)
        await mgr.release(repos)  # should not raise


class TestDeterministicOrder:
    async def test_acquires_in_sorted_order(self, repo_roots: dict[str, Path]) -> None:
        """Locks should be acquired in sorted order regardless of input order."""
        mgr = RepoLockManager()
        acquisition_order: list[str] = []
        orig_acquire_single = mgr._acquire_single

        async def tracking_acquire(repo: str, root: Path) -> None:
            acquisition_order.append(repo)
            await orig_acquire_single(repo, root)

        mgr._acquire_single = tracking_acquire  # type: ignore[assignment]
        await mgr.acquire(["reactor-core", "jarvis", "prime"], repo_roots)
        assert acquisition_order == ["jarvis", "prime", "reactor-core"]
        await mgr.release(["jarvis", "prime", "reactor-core"])


class TestConcurrency:
    async def test_second_acquire_blocks(self, repo_roots: dict[str, Path]) -> None:
        """In-process lock prevents concurrent saga on same repo."""
        mgr = RepoLockManager()
        repos = ["jarvis"]
        await mgr.acquire(repos, repo_roots)

        acquired = False

        async def try_acquire() -> None:
            nonlocal acquired
            mgr2 = RepoLockManager()
            # Share async locks with first manager
            mgr2._async_locks = mgr._async_locks
            await asyncio.wait_for(mgr2.acquire(repos, repo_roots), timeout=0.5)
            acquired = True

        with pytest.raises(asyncio.TimeoutError):
            await try_acquire()
        assert not acquired
        await mgr.release(repos)


class TestStaleLockRecovery:
    def test_dead_pid_lock_cleaned(self, repo_roots: dict[str, Path]) -> None:
        lock_file = repo_roots["jarvis"] / ".jarvis" / "saga.lock"
        lock_file.write_text(json.dumps({
            "pid": 999999999,  # almost certainly dead
            "saga_id": "old-saga",
            "acquired_at_ns": 0,
        }))
        mgr = RepoLockManager()
        cleaned = mgr.cleanup_stale_locks(repo_roots)
        assert "jarvis" in cleaned
        assert not lock_file.exists()


class TestOrphanBranchDetection:
    def test_detect_orphan_branches(self, tmp_path: Path) -> None:
        """Detect leftover ouroboros/saga-* branches."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / ".jarvis").mkdir()
        subprocess.run(
            ["git", "init", "-q"],
            cwd=str(repo_root), check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=str(repo_root), check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=str(repo_root), check=True,
        )
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "init", "-q", "--no-verify"],
            cwd=str(repo_root), check=True,
        )
        subprocess.run(
            ["git", "branch", "ouroboros/saga-old-op/jarvis"],
            cwd=str(repo_root), check=True,
        )

        mgr = RepoLockManager()
        orphans = mgr.detect_orphan_branches({"jarvis": repo_root})
        assert any("ouroboros/saga-old-op" in b for b in orphans)
