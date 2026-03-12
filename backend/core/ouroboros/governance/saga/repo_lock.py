"""RepoLockManager — two-tier repo-level locks for saga exclusivity.

Tier 1 (in-process): asyncio.Lock per repo — prevents concurrent sagas
in the same event loop.

Tier 2 (cross-process): fcntl.flock on <repo_root>/.jarvis/saga.lock —
prevents concurrent sagas across processes and survives crashes.

Acquisition order: always sorted(repo_names) to prevent deadlock.

Platform: macOS and Linux only (fcntl). Not Windows-compatible.
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Dict, List

logger = logging.getLogger("Ouroboros.RepoLock")


class RepoLockManager:
    """Two-tier repo-level locks for saga exclusivity."""

    def __init__(self) -> None:
        self._async_locks: Dict[str, asyncio.Lock] = {}
        self._file_fds: Dict[str, int] = {}
        self._repo_roots: Dict[str, Path] = {}

    async def acquire(self, repos: List[str], repo_roots: Dict[str, Path]) -> None:
        """Acquire both tiers in deterministic sorted order."""
        for repo in sorted(repos):
            await self._acquire_single(repo, repo_roots[repo])

    async def _acquire_single(self, repo: str, root: Path) -> None:
        """Acquire in-process lock, then file lock for a single repo."""
        # Tier 1: in-process
        if repo not in self._async_locks:
            self._async_locks[repo] = asyncio.Lock()
        await self._async_locks[repo].acquire()

        # Tier 2: file lock
        lock_path = root / ".jarvis" / "saga.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_WRONLY)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Write ownership metadata
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            meta = json.dumps({
                "pid": os.getpid(),
                "saga_id": "",
                "acquired_at_ns": time.monotonic_ns(),
            })
            os.write(fd, meta.encode())
            self._file_fds[repo] = fd
            self._repo_roots[repo] = root
        except (OSError, BlockingIOError):
            # Release in-process lock if file lock fails
            self._async_locks[repo].release()
            raise RuntimeError(f"repo_lock_contention:{repo}")

    async def release(self, repos: List[str]) -> None:
        """Release both tiers. Safe to call multiple times."""
        for repo in repos:
            # Tier 2: file lock
            fd = self._file_fds.pop(repo, None)
            if fd is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                    os.close(fd)
                except OSError:
                    pass
            # Remove lock file
            root = self._repo_roots.pop(repo, None)
            if root is not None:
                lock_path = root / ".jarvis" / "saga.lock"
                lock_path.unlink(missing_ok=True)
            # Tier 1: in-process
            lock = self._async_locks.get(repo)
            if lock is not None and lock.locked():
                lock.release()

    def cleanup_stale_locks(self, repo_roots: Dict[str, Path]) -> List[str]:
        """Check for stale lock files with dead PIDs. Remove and return cleaned repos."""
        cleaned: List[str] = []
        for repo, root in repo_roots.items():
            lock_path = root / ".jarvis" / "saga.lock"
            if not lock_path.exists():
                continue
            try:
                data = json.loads(lock_path.read_text())
                pid = data.get("pid", 0)
                if pid and pid != os.getpid():
                    try:
                        os.kill(pid, 0)  # check if alive
                    except ProcessLookupError:
                        # PID is dead — stale lock
                        lock_path.unlink()
                        cleaned.append(repo)
                        logger.warning(
                            "[RepoLock] Removed stale lock for %s (dead PID %d)", repo, pid
                        )
                    except PermissionError:
                        pass  # PID alive but different user
            except (json.JSONDecodeError, OSError):
                # Corrupt lock file — remove it
                lock_path.unlink(missing_ok=True)
                cleaned.append(repo)
        return cleaned

    def detect_orphan_branches(self, repo_roots: Dict[str, Path]) -> List[str]:
        """Scan repos for ouroboros/saga-* branches. Return list for health endpoint."""
        orphans: List[str] = []
        for repo, root in repo_roots.items():
            if not (root / ".git").exists():
                continue
            try:
                result = subprocess.run(
                    ["git", "branch", "--list", "ouroboros/saga-*"],
                    cwd=str(root),
                    capture_output=True,
                    text=True,
                )
                for line in result.stdout.splitlines():
                    branch = line.strip().lstrip("* ")
                    if branch:
                        orphans.append(f"{repo}:{branch}")
            except Exception:
                pass
        return orphans
