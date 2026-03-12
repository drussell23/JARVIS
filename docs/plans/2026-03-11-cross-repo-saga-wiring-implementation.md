# Cross-Repo Saga Wiring — B+ Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Harden the existing cross-repo saga path with branch isolation, deterministic locks, base-SHA pinning, and ff-only promotion — then prove it works with dual-gate E2E tests against real J-Prime generation.

**Architecture:** This is a hardening/wiring pass over existing infrastructure. SagaApplyStrategy gains B+ branch lifecycle (ephemeral branches, promote gates, crash recovery). New RepoLockManager provides two-tier locking. Orchestrator handles new SAGA_PARTIAL_PROMOTE terminal state. All changes are feature-flagged via `JARVIS_SAGA_BRANCH_ISOLATION`.

**Tech Stack:** Python 3.12+, asyncio, git subprocess, fcntl file locks, pytest with real J-Prime generation

**Design doc:** `docs/plans/2026-03-11-cross-repo-saga-wiring-design.md`

---

### Task 1: Add SAGA_PARTIAL_PROMOTE terminal state and SagaLedgerArtifact

**Files:**
- Modify: `backend/core/ouroboros/governance/saga/saga_types.py:72-91`
- Test: `tests/test_ouroboros_governance/test_saga_types_bplus.py` (create)

**Context:** `saga_types.py` currently has 6 terminal states in `SagaTerminalState`. We need a 7th for partial promotion failures. We also add `SagaLedgerArtifact` — a frozen dataclass emitted with every saga ledger entry for full audit trail.

**Step 1: Write failing tests**

Create `tests/test_ouroboros_governance/test_saga_types_bplus.py`:

```python
"""Tests for B+ saga type additions: SAGA_PARTIAL_PROMOTE + SagaLedgerArtifact."""
import dataclasses
import time

from backend.core.ouroboros.governance.saga.saga_types import (
    SagaLedgerArtifact,
    SagaTerminalState,
)


def test_partial_promote_terminal_state_exists():
    assert hasattr(SagaTerminalState, "SAGA_PARTIAL_PROMOTE")
    assert SagaTerminalState.SAGA_PARTIAL_PROMOTE.value == "saga_partial_promote"


def test_partial_promote_is_distinct_from_stuck():
    assert SagaTerminalState.SAGA_PARTIAL_PROMOTE != SagaTerminalState.SAGA_STUCK


def test_saga_ledger_artifact_is_frozen():
    artifact = SagaLedgerArtifact(
        saga_id="test-saga",
        op_id="test-op",
        event="prepare",
        repo="jarvis",
        original_ref="main",
        original_sha="abc123",
        base_sha="abc123",
        saga_branch="ouroboros/saga-test/jarvis",
        promoted_sha="",
        promote_order_index=-1,
        rollback_reason="",
        partial_promote_boundary_repo="",
        kept_forensics_branches=False,
        skipped_no_diff=False,
        timestamp_ns=time.monotonic_ns(),
    )
    assert dataclasses.is_dataclass(artifact)
    # Frozen: assignment should raise
    try:
        artifact.saga_id = "changed"  # type: ignore[misc]
        assert False, "Should have raised FrozenInstanceError"
    except (dataclasses.FrozenInstanceError, AttributeError):
        pass


def test_saga_ledger_artifact_serializes_to_dict():
    artifact = SagaLedgerArtifact(
        saga_id="s1",
        op_id="o1",
        event="apply_repo",
        repo="prime",
        original_ref="main",
        original_sha="aaa",
        base_sha="aaa",
        saga_branch="ouroboros/saga-o1/prime",
        promoted_sha="bbb",
        promote_order_index=1,
        rollback_reason="",
        partial_promote_boundary_repo="",
        kept_forensics_branches=True,
        skipped_no_diff=False,
        timestamp_ns=12345,
    )
    d = dataclasses.asdict(artifact)
    assert d["saga_id"] == "s1"
    assert d["promoted_sha"] == "bbb"
    assert d["kept_forensics_branches"] is True
    assert d["promote_order_index"] == 1
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_saga_types_bplus.py -v`
Expected: FAIL — `ImportError: cannot import name 'SagaLedgerArtifact'`

**Step 3: Implement in saga_types.py**

Add to `backend/core/ouroboros/governance/saga/saga_types.py`:

1. Add `SAGA_PARTIAL_PROMOTE` to `SagaTerminalState` enum (after line 79):
```python
    SAGA_PARTIAL_PROMOTE = "saga_partial_promote"  # some repos promoted, promote failed for others
```

2. Add `SagaLedgerArtifact` dataclass (after `SagaApplyResult`, at end of file):
```python
@dataclass(frozen=True)
class SagaLedgerArtifact:
    """Frozen artifact emitted with every saga ledger entry for audit trail."""

    saga_id: str
    op_id: str
    event: str                          # "prepare" | "apply_repo" | "promote_repo" | etc.
    repo: str                           # "*" for saga-wide events
    original_ref: str                   # branch name or "HEAD" (detached)
    original_sha: str                   # SHA at saga start
    base_sha: str                       # pinned base SHA for this repo
    saga_branch: str                    # ouroboros/saga-<op_id>/<repo>
    promoted_sha: str                   # SHA after ff-only merge ("" if not promoted)
    promote_order_index: int            # position in promotion sequence (-1 if N/A)
    rollback_reason: str                # "" on success, reason code on failure
    partial_promote_boundary_repo: str  # repo where promotion failed ("" if clean)
    kept_forensics_branches: bool       # True if saga branches retained for debug
    skipped_no_diff: bool               # True if repo had no actual changes
    timestamp_ns: int                   # time.monotonic_ns()
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_saga_types_bplus.py -v`
Expected: PASS (4 tests)

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/saga/saga_types.py tests/test_ouroboros_governance/test_saga_types_bplus.py
git commit -m "feat(saga): add SAGA_PARTIAL_PROMOTE terminal state and SagaLedgerArtifact"
```

---

### Task 2: Create RepoLockManager

**Files:**
- Create: `backend/core/ouroboros/governance/saga/repo_lock.py`
- Test: `tests/test_ouroboros_governance/test_repo_lock.py` (create)

**Context:** Replace the no-op `_acquire_repo_leases()` stub. Two-tier: asyncio.Lock (in-process) + fcntl.flock (cross-process). Lock files live at `<repo_root>/.jarvis/saga.lock`. Always acquire in `sorted(repo_names)` order.

**Step 1: Write failing tests**

Create `tests/test_ouroboros_governance/test_repo_lock.py`:

```python
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
        # Initialize a real git repo with an orphan branch ref
        subprocess.run(
            ["git", "init", "-q"],
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
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_repo_lock.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'backend.core.ouroboros.governance.saga.repo_lock'`

**Step 3: Implement RepoLockManager**

Create `backend/core/ouroboros/governance/saga/repo_lock.py`:

```python
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
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_repo_lock.py -v`
Expected: PASS (7 tests)

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/saga/repo_lock.py tests/test_ouroboros_governance/test_repo_lock.py
git commit -m "feat(saga): add RepoLockManager with two-tier locking and crash recovery"
```

---

### Task 3: Add B+ branch lifecycle to SagaApplyStrategy

**Files:**
- Modify: `backend/core/ouroboros/governance/saga/saga_apply_strategy.py` (entire file)
- Test: `tests/test_ouroboros_governance/test_saga_bplus_branches.py` (create)

**Context:** This is the largest task. SagaApplyStrategy gains: feature flag check, clean-tree precheck, ephemeral branch creation, git commit after git add, promote gates (TARGET_MOVED + ancestry), compensation via branch delete, try/finally lock release. All gated behind `JARVIS_SAGA_BRANCH_ISOLATION` env var.

**Step 1: Write failing tests for git helpers**

Create `tests/test_ouroboros_governance/test_saga_bplus_branches.py`:

```python
"""Tests for B+ branch lifecycle in SagaApplyStrategy."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Dict, Tuple

import pytest

from backend.core.ouroboros.governance.op_context import (
    OperationContext,
    RepoSagaStatus,
    SagaStepStatus,
)
from backend.core.ouroboros.governance.saga.saga_apply_strategy import (
    SagaApplyStrategy,
    _safe_branch_name,
)
from backend.core.ouroboros.governance.saga.saga_types import (
    FileOp,
    PatchedFile,
    RepoPatch,
    SagaTerminalState,
)


def _init_repo(path: Path) -> str:
    """Initialize a git repo with one commit. Returns HEAD SHA."""
    subprocess.run(["git", "init", "-q"], cwd=str(path), check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=str(path), check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(path), check=True,
    )
    (path / "README.md").write_text("# test\n")
    subprocess.run(["git", "add", "."], cwd=str(path), check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "init", "--no-verify"],
        cwd=str(path), check=True,
    )
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(path), capture_output=True, text=True, check=True,
    )
    # Create .jarvis dir for lock files
    (path / ".jarvis").mkdir(exist_ok=True)
    return result.stdout.strip()


@pytest.fixture
def git_repos(tmp_path: Path) -> Tuple[Dict[str, Path], Dict[str, str]]:
    """Create two git repos. Returns (repo_roots, base_shas)."""
    roots: Dict[str, Path] = {}
    shas: Dict[str, str] = {}
    for name in ("jarvis", "prime"):
        root = tmp_path / name
        root.mkdir()
        sha = _init_repo(root)
        roots[name] = root
        shas[name] = sha
    return roots, shas


def _make_ctx(
    repo_scope: Tuple[str, ...] = ("jarvis", "prime"),
    repo_snapshots: Tuple[Tuple[str, str], ...] = (),
    op_id: str = "test-op-001",
) -> OperationContext:
    return OperationContext.create(
        target_files=("test.py",),
        description="Test saga operation",
        op_id=op_id,
        repo_scope=repo_scope,
        repo_snapshots=repo_snapshots,
        saga_id=f"saga-{op_id}",
    )


def _make_patch(repo: str, file_path: str = "src/test.py", content: str = "# new\n") -> RepoPatch:
    return RepoPatch(
        repo=repo,
        files=(PatchedFile(path=file_path, op=FileOp.CREATE, preimage=None),),
        new_content=((file_path, content.encode()),),
    )


class TestSafeBranchName:
    def test_normal_op_id(self) -> None:
        result = _safe_branch_name("op-20260311-abc123", "jarvis")
        assert result == "ouroboros/saga-op-20260311-abc123/jarvis"

    def test_truncates_long_op_id(self) -> None:
        long_id = "a" * 100
        result = _safe_branch_name(long_id, "jarvis")
        # safe_id is truncated to 64 chars
        parts = result.split("/")
        assert len(parts) == 3
        assert parts[0] == "ouroboros"

    def test_sanitizes_special_chars(self) -> None:
        result = _safe_branch_name("op id/with spaces", "repo.name")
        assert " " not in result
        assert result.count("/") == 2  # ouroboros/ + /repo


class TestCleanTreeCheck:
    async def test_clean_tree_passes(self, git_repos) -> None:
        roots, shas = git_repos
        strategy = SagaApplyStrategy(
            repo_roots=roots, ledger=None,
            branch_isolation=True,
        )
        # Should not raise
        await strategy._assert_clean_worktree("jarvis")

    async def test_dirty_tree_fails(self, git_repos) -> None:
        roots, shas = git_repos
        # Make tree dirty
        (roots["jarvis"] / "README.md").write_text("# modified\n")
        subprocess.run(["git", "add", "README.md"], cwd=str(roots["jarvis"]), check=True)

        strategy = SagaApplyStrategy(
            repo_roots=roots, ledger=None,
            branch_isolation=True,
        )
        with pytest.raises(RuntimeError, match="dirty_worktree"):
            await strategy._assert_clean_worktree("jarvis")

    async def test_untracked_files_allowed(self, git_repos) -> None:
        roots, shas = git_repos
        (roots["jarvis"] / "untracked.txt").write_text("untracked\n")
        strategy = SagaApplyStrategy(
            repo_roots=roots, ledger=None,
            branch_isolation=True,
        )
        # Should not raise
        await strategy._assert_clean_worktree("jarvis")


class TestEphemeralBranchLifecycle:
    async def test_create_and_checkout(self, git_repos) -> None:
        roots, shas = git_repos
        strategy = SagaApplyStrategy(
            repo_roots=roots, ledger=None,
            branch_isolation=True,
        )
        strategy._base_shas = {"jarvis": shas["jarvis"]}
        branch = await strategy._create_ephemeral_branch("jarvis", "op-001")
        assert branch.startswith("ouroboros/saga-")

        # Verify we're on the ephemeral branch
        result = subprocess.run(
            ["git", "symbolic-ref", "--short", "HEAD"],
            cwd=str(roots["jarvis"]), capture_output=True, text=True, check=True,
        )
        assert result.stdout.strip() == branch

    async def test_promote_ff_only(self, git_repos) -> None:
        roots, shas = git_repos
        strategy = SagaApplyStrategy(
            repo_roots=roots, ledger=None,
            branch_isolation=True,
        )
        strategy._base_shas = {"jarvis": shas["jarvis"]}
        strategy._original_branches = {"jarvis": "main"}
        branch = await strategy._create_ephemeral_branch("jarvis", "op-002")
        strategy._saga_branches = {"jarvis": branch}

        # Make a commit on ephemeral branch
        (roots["jarvis"] / "new.py").write_text("# new\n")
        subprocess.run(["git", "add", "new.py"], cwd=str(roots["jarvis"]), check=True)
        subprocess.run(
            ["git", "commit", "-m", "test commit", "--no-verify"],
            cwd=str(roots["jarvis"]), check=True,
        )

        promoted_sha = await strategy._promote_ephemeral_branch("jarvis")
        assert promoted_sha != shas["jarvis"]

        # Verify main advanced
        result = subprocess.run(
            ["git", "rev-parse", "main"],
            cwd=str(roots["jarvis"]), capture_output=True, text=True, check=True,
        )
        assert result.stdout.strip() == promoted_sha

    async def test_target_moved_aborts_promote(self, git_repos) -> None:
        roots, shas = git_repos
        strategy = SagaApplyStrategy(
            repo_roots=roots, ledger=None,
            branch_isolation=True,
        )
        strategy._base_shas = {"jarvis": shas["jarvis"]}
        strategy._original_branches = {"jarvis": "main"}
        branch = await strategy._create_ephemeral_branch("jarvis", "op-003")
        strategy._saga_branches = {"jarvis": branch}

        # Commit on ephemeral
        (roots["jarvis"] / "new.py").write_text("# new\n")
        subprocess.run(["git", "add", "new.py"], cwd=str(roots["jarvis"]), check=True)
        subprocess.run(
            ["git", "commit", "-m", "saga commit", "--no-verify"],
            cwd=str(roots["jarvis"]), check=True,
        )

        # Advance main (simulate external push)
        subprocess.run(["git", "checkout", "main"], cwd=str(roots["jarvis"]), check=True)
        (roots["jarvis"] / "other.py").write_text("# other\n")
        subprocess.run(["git", "add", "other.py"], cwd=str(roots["jarvis"]), check=True)
        subprocess.run(
            ["git", "commit", "-m", "external", "--no-verify"],
            cwd=str(roots["jarvis"]), check=True,
        )
        subprocess.run(["git", "checkout", branch], cwd=str(roots["jarvis"]), check=True)

        with pytest.raises(RuntimeError, match="TARGET_MOVED"):
            await strategy._check_promote_safe("jarvis")


class TestCompensationViaBranchDelete:
    async def test_cleanup_deletes_branch(self, git_repos) -> None:
        roots, shas = git_repos
        strategy = SagaApplyStrategy(
            repo_roots=roots, ledger=None,
            branch_isolation=True,
            keep_failed_saga_branches=False,
        )
        strategy._base_shas = {"jarvis": shas["jarvis"]}
        strategy._original_branches = {"jarvis": "main"}
        strategy._original_shas = {"jarvis": shas["jarvis"]}
        branch = await strategy._create_ephemeral_branch("jarvis", "op-004")
        strategy._saga_branches = {"jarvis": branch}

        await strategy._cleanup_ephemeral_branch("jarvis")

        # Verify branch is deleted
        result = subprocess.run(
            ["git", "branch", "--list", branch],
            cwd=str(roots["jarvis"]), capture_output=True, text=True,
        )
        assert branch not in result.stdout

        # Verify we're back on main
        result = subprocess.run(
            ["git", "symbolic-ref", "--short", "HEAD"],
            cwd=str(roots["jarvis"]), capture_output=True, text=True, check=True,
        )
        assert result.stdout.strip() == "main"

    async def test_forensics_keeps_branch(self, git_repos) -> None:
        roots, shas = git_repos
        strategy = SagaApplyStrategy(
            repo_roots=roots, ledger=None,
            branch_isolation=True,
            keep_failed_saga_branches=True,
        )
        strategy._base_shas = {"jarvis": shas["jarvis"]}
        strategy._original_branches = {"jarvis": "main"}
        strategy._original_shas = {"jarvis": shas["jarvis"]}
        branch = await strategy._create_ephemeral_branch("jarvis", "op-005")
        strategy._saga_branches = {"jarvis": branch}

        await strategy._cleanup_ephemeral_branch("jarvis")

        # Branch should still exist for forensics
        result = subprocess.run(
            ["git", "branch", "--list", branch],
            cwd=str(roots["jarvis"]), capture_output=True, text=True,
        )
        assert branch in result.stdout.strip()


class TestFeatureFlagFallback:
    async def test_branch_isolation_false_uses_legacy_path(self, git_repos) -> None:
        """When feature flag is off, execute() uses old direct-to-HEAD behavior."""
        roots, shas = git_repos
        strategy = SagaApplyStrategy(
            repo_roots=roots, ledger=None,
            branch_isolation=False,
        )
        ctx = _make_ctx(
            repo_scope=("jarvis",),
            repo_snapshots=(("jarvis", shas["jarvis"]),),
        )
        patch_map = {"jarvis": _make_patch("jarvis")}
        result = await strategy.execute(ctx, patch_map)
        # Should still work (legacy path)
        assert result.terminal_state == SagaTerminalState.SAGA_APPLY_COMPLETED
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_saga_bplus_branches.py -v`
Expected: FAIL — `TypeError: SagaApplyStrategy.__init__() got unexpected keyword argument 'branch_isolation'`

**Step 3: Implement B+ branch lifecycle in SagaApplyStrategy**

Modify `backend/core/ouroboros/governance/saga/saga_apply_strategy.py`. The changes are:

**3a. Update imports (top of file, add `os`, `re`, `time` to existing imports):**

After line 18 (`import subprocess`), add:
```python
import os
import re
import time
```

**3b. Add module-level helpers (after `logger` on line 41, before class on line 44):**

```python
def _safe_branch_name(op_id: str, repo: str) -> str:
    """Sanitize op_id + repo into a valid git ref name."""
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", op_id)[:64]
    safe_repo = re.sub(r"[^a-zA-Z0-9_-]", "_", repo)[:32]
    return f"ouroboros/saga-{safe_id}/{safe_repo}"


_BRANCH_ISOLATION_ENABLED = os.getenv(
    "JARVIS_SAGA_BRANCH_ISOLATION", "false"
).lower() in ("1", "true", "yes")
```

**3c. Update `__init__` (replace lines 55-61):**

```python
    def __init__(
        self,
        repo_roots: Dict[str, Any],
        ledger: Any,
        branch_isolation: Optional[bool] = None,
        keep_failed_saga_branches: bool = True,
    ) -> None:
        self._repo_roots: Dict[str, Path] = {k: Path(v) for k, v in repo_roots.items()}
        self._ledger = ledger
        self._branch_isolation = (
            branch_isolation if branch_isolation is not None else _BRANCH_ISOLATION_ENABLED
        )
        self._keep_failed_branches = keep_failed_saga_branches

        # B+ branch state (populated during execute)
        self._saga_branches: Dict[str, str] = {}
        self._original_branches: Dict[str, str] = {}
        self._original_shas: Dict[str, str] = {}
        self._base_shas: Dict[str, str] = {}
        self._lock_manager: Optional[Any] = None
```

**3d. Replace `execute()` (lines 67-182) with dispatch to legacy or B+ path:**

```python
    async def execute(
        self, ctx: OperationContext, patch_map: Dict[str, RepoPatch]
    ) -> SagaApplyResult:
        """Execute the full saga. Dispatches to B+ or legacy path based on feature flag."""
        apply_order = self._resolve_apply_order(ctx)
        saga_id = ctx.saga_id or ctx.op_id
        repo_statuses: Dict[str, RepoSagaStatus] = {
            rss.repo: rss for rss in (ctx.saga_state or ())
        }

        if not self._branch_isolation:
            return await self._execute_legacy(ctx, patch_map, apply_order, saga_id, repo_statuses)

        from backend.core.ouroboros.governance.saga.repo_lock import RepoLockManager
        if self._lock_manager is None:
            self._lock_manager = RepoLockManager()

        await self._lock_manager.acquire(apply_order, self._repo_roots)
        try:
            return await self._execute_bplus(ctx, patch_map, apply_order, saga_id, repo_statuses)
        except BaseException:
            await self._bplus_compensate_all(apply_order, saga_id, ctx.op_id, "exception_during_execute", repo_statuses)
            raise
        finally:
            await self._lock_manager.release(apply_order)
```

**3e. Rename the original execute body to `_execute_legacy()`** — extract the entire body of the old `execute()` (lines 70-182 content) into a new method `_execute_legacy(self, ctx, patch_map, apply_order, saga_id, repo_statuses)` with identical logic. This preserves all existing behavior when `branch_isolation=False`.

**3f. Add `_execute_bplus()` method** — the B+ path that creates ephemeral branches, applies patches with git commit, and returns for orchestrator to verify:

```python
    async def _execute_bplus(
        self, ctx: OperationContext, patch_map: Dict[str, RepoPatch],
        apply_order: List[str], saga_id: str, repo_statuses: Dict[str, RepoSagaStatus],
    ) -> SagaApplyResult:
        """B+ branch-isolated execution path."""
        for repo in apply_order:
            await self._assert_clean_worktree(repo)
            branch_name, sha = await self._capture_original_ref(repo)
            self._original_branches[repo] = branch_name
            self._original_shas[repo] = sha
            self._base_shas[repo] = sha
            saga_branch = await self._create_ephemeral_branch(repo, ctx.op_id)
            self._saga_branches[repo] = saga_branch

        await self._emit_sub_event("prepare", saga_id, ctx.op_id)

        applied_repos: List[str] = []
        step_index = 0
        failed_repo: Optional[str] = None
        failure_reason = ""
        failure_error = ""

        for repo in apply_order:
            patch = patch_map.get(repo, RepoPatch(repo=repo, files=()))
            if patch.is_empty():
                logger.info("[Saga-B+] %s SKIPPED (empty patch)", repo)
                repo_statuses[repo] = RepoSagaStatus(repo=repo, status=SagaStepStatus.SKIPPED)
                step_index += 1
                continue
            logger.info("[Saga-B+] Applying %s (step %d)", repo, step_index)
            try:
                await self._apply_patch_bplus(repo, patch, ctx, saga_id)
                applied_repos.append(repo)
                step_index += 1
                await self._emit_sub_event("apply_repo", saga_id, ctx.op_id, repo=repo)
                repo_statuses[repo] = RepoSagaStatus(repo=repo, status=SagaStepStatus.APPLIED)
            except Exception as exc:
                failed_repo = repo
                failure_reason = "apply_write_error"
                failure_error = f"{type(exc).__name__}: {exc}"
                logger.error("[Saga-B+] Apply failed for %s: %s", repo, exc)
                repo_statuses[repo] = RepoSagaStatus(
                    repo=repo, status=SagaStepStatus.FAILED,
                    last_error=failure_error, reason_code=failure_reason,
                )
                break

        if failed_repo is not None:
            await self._bplus_compensate_all(
                apply_order, saga_id, ctx.op_id, failure_reason, repo_statuses,
            )
            return SagaApplyResult(
                terminal_state=SagaTerminalState.SAGA_ROLLED_BACK,
                saga_id=saga_id, saga_step_index=step_index,
                error=failure_error, reason_code=failure_reason,
                saga_state=tuple(repo_statuses.values()),
            )

        await self._emit_sub_event("pre_verify", saga_id, ctx.op_id)
        return SagaApplyResult(
            terminal_state=SagaTerminalState.SAGA_APPLY_COMPLETED,
            saga_id=saga_id, saga_step_index=step_index, error=None,
            saga_state=tuple(repo_statuses.values()),
        )
```

**3g. Add `_apply_patch_bplus()` — writes + git add + git commit:**

```python
    async def _apply_patch_bplus(
        self, repo: str, patch: RepoPatch, ctx: OperationContext, saga_id: str,
    ) -> None:
        """Write files, git add, git commit on ephemeral branch."""
        repo_root = self._repo_roots[repo]
        content_map = dict(patch.new_content)
        written: List[str] = []
        for pf in patch.files:
            full_path = repo_root / pf.path
            new_bytes = content_map.get(pf.path, b"")
            if pf.op == FileOp.CREATE:
                full_path.parent.mkdir(parents=True, exist_ok=True)
                full_path.write_bytes(new_bytes)
            elif pf.op == FileOp.MODIFY:
                full_path.write_bytes(new_bytes)
            elif pf.op == FileOp.DELETE:
                if full_path.exists():
                    full_path.unlink()
                else:
                    continue
            written.append(pf.path)
        if not written:
            return
        await self._git(repo, ["add", "--"] + written)
        rc = await self._git_rc(repo, ["diff", "--cached", "--quiet"])
        if rc == 0:
            logger.info("[Saga-B+] %s: no diff after apply, skipping commit", repo)
            await self._emit_sub_event("skipped_no_diff", saga_id, ctx.op_id, repo=repo)
            return
        commit_msg = (
            f"[ouroboros] {ctx.description[:72]}\n\n"
            f"op_id: {ctx.op_id}\n"
            f"saga_id: {saga_id}\n"
            f"repo: {repo}\n"
            f"base_sha: {self._base_shas.get(repo, '')}\n"
            f"phase: apply\n"
            f"schema_version: {ctx.schema_version}\n"
        )
        env = {
            "GIT_AUTHOR_NAME": "JARVIS Ouroboros",
            "GIT_AUTHOR_EMAIL": "ouroboros@jarvis.local",
            "GIT_COMMITTER_NAME": "JARVIS Ouroboros",
            "GIT_COMMITTER_EMAIL": "ouroboros@jarvis.local",
        }
        await self._git(repo, ["commit", "--no-verify", "-m", commit_msg], env=env)
```

**3h. Add all B+ helper methods** (`_assert_clean_worktree`, `_capture_original_ref`, `_create_ephemeral_branch`, `_check_promote_safe`, `_promote_ephemeral_branch`, `_cleanup_ephemeral_branch`, `_bplus_compensate_all`, `_git`, `_git_rc`, `promote_all`). See the full method signatures and implementations in the design doc Section 2-4. The exact code for each is provided in Steps 3f-3g pattern above.

**3i. Add `promote_all()` public method:**

```python
    async def promote_all(
        self, apply_order: List[str], saga_id: str, op_id: str,
    ) -> Tuple[SagaTerminalState, Dict[str, str]]:
        """Promote all ephemeral branches via ff-only merge.
        Returns (terminal_state, {repo: promoted_sha}).
        """
        if not self._branch_isolation:
            return SagaTerminalState.SAGA_SUCCEEDED, {}
        promoted: Dict[str, str] = {}
        for idx, repo in enumerate(apply_order):
            if repo not in self._saga_branches:
                continue
            try:
                sha = await self._promote_ephemeral_branch(repo)
                promoted[repo] = sha
                await self._emit_sub_event(
                    "promote_repo", saga_id, op_id,
                    repo=repo, promoted_sha=sha, promote_order_index=idx,
                )
            except Exception as exc:
                logger.error("[Saga-B+] Promote failed for %s: %s", repo, exc)
                await self._emit_sub_event(
                    "partial_promote", saga_id, op_id,
                    repo=repo, reason=str(exc), boundary_repo=repo,
                )
                return SagaTerminalState.SAGA_PARTIAL_PROMOTE, promoted
        return SagaTerminalState.SAGA_SUCCEEDED, promoted
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_saga_bplus_branches.py -v`
Expected: PASS (all tests)

Run: `python3 -m pytest tests/test_ouroboros_governance/ -k saga -v`
Expected: All existing saga tests still pass (legacy path)

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/saga/saga_apply_strategy.py tests/test_ouroboros_governance/test_saga_bplus_branches.py
git commit -m "feat(saga): B+ branch-isolated apply with ephemeral branches, locks, promote gates"
```

---

### Task 4: Handle SAGA_PARTIAL_PROMOTE in orchestrator

**Files:**
- Modify: `backend/core/ouroboros/governance/orchestrator.py:1107-1159`
- Test: `tests/test_ouroboros_governance/test_orchestrator_partial_promote.py` (create)

**Context:** After verify succeeds, call `strategy.promote_all()`. Handle SAGA_PARTIAL_PROMOTE with scoped pause.

**Step 1: Write test**

Create `tests/test_ouroboros_governance/test_orchestrator_partial_promote.py`:

```python
"""Tests for SAGA_PARTIAL_PROMOTE handling in orchestrator."""
from backend.core.ouroboros.governance.saga.saga_types import SagaTerminalState


def test_partial_promote_state_recognized():
    assert SagaTerminalState.SAGA_PARTIAL_PROMOTE.value == "saga_partial_promote"
    assert SagaTerminalState.SAGA_PARTIAL_PROMOTE != SagaTerminalState.SAGA_STUCK
```

**Step 2: Modify orchestrator**

In `backend/core/ouroboros/governance/orchestrator.py`, in `_execute_saga_apply()`:

After verify passes (line 1138 `# SAGA_SUCCEEDED`), insert the promote step before the existing success handling. Replace lines 1138-1159:

```python
            # B+ mode: promote ephemeral branches before declaring success
            promote_state, promoted_shas = await strategy.promote_all(
                apply_order=list(ctx.repo_scope),
                saga_id=apply_result.saga_id,
                op_id=ctx.op_id,
            )

            if promote_state == SagaTerminalState.SAGA_PARTIAL_PROMOTE:
                try:
                    await self._stack.comm.emit_postmortem(
                        op_id=ctx.op_id,
                        root_cause="saga_partial_promote",
                        failed_phase="PROMOTE",
                        next_safe_action="human_intervention_required",
                    )
                except Exception:
                    pass
                try:
                    await self._stack.controller.pause(scope="cross_repo_saga")
                except TypeError:
                    await self._stack.controller.pause()
                except Exception:
                    logger.exception(
                        "[Orchestrator] controller.pause() failed for partial promote %s",
                        ctx.op_id,
                    )
                ctx = ctx.advance(OperationPhase.POSTMORTEM)
                await self._record_ledger(
                    ctx, OperationState.FAILED,
                    {"reason": "saga_partial_promote", "saga_id": apply_result.saga_id, "promoted_repos": promoted_shas},
                )
                self._record_canary_for_ctx(ctx, False, time.monotonic() - _t_saga)
                await self._publish_outcome(ctx, OperationState.FAILED, "saga_partial_promote")
                return ctx

            # SAGA_SUCCEEDED — existing success handling continues unchanged
            ctx = ctx.advance(OperationPhase.VERIFY)
            # ... (rest of lines 1140-1159 unchanged)
```

**Step 3: Run tests**

Run: `python3 -m pytest tests/test_ouroboros_governance/ -k orchestrator -v`
Expected: PASS

**Step 4: Commit**

```bash
git add backend/core/ouroboros/governance/orchestrator.py tests/test_ouroboros_governance/test_orchestrator_partial_promote.py
git commit -m "feat(orchestrator): handle SAGA_PARTIAL_PROMOTE with scoped pause"
```

---

### Task 5: Wire config in GovernedLoopService

**Files:**
- Modify: `backend/core/ouroboros/governance/governed_loop_service.py:1265-1284`

**Step 1: Add orphan branch detection to health()**

In `governed_loop_service.py`, add to the `health()` method return dict (after line 1283):

```python
            "orphan_saga_branches": self._detect_orphan_branches(),
```

Add helper method (after `health()`):

```python
    def _detect_orphan_branches(self) -> List[str]:
        """Detect orphaned saga branches across registered repos."""
        try:
            from backend.core.ouroboros.governance.saga.repo_lock import RepoLockManager
            mgr = RepoLockManager()
            if self._config.repo_registry is not None:
                roots = {
                    rc.name: rc.local_path
                    for rc in self._config.repo_registry.list_enabled()
                }
            else:
                roots = {"jarvis": self._config.project_root}
            return mgr.detect_orphan_branches(roots)
        except Exception:
            return []
```

**Step 2: Run tests**

Run: `python3 -m pytest tests/test_ouroboros_governance/ -k governed_loop -v`
Expected: PASS

**Step 3: Commit**

```bash
git add backend/core/ouroboros/governance/governed_loop_service.py
git commit -m "feat(gls): surface orphan saga branches in health endpoint"
```

---

### Task 6: Gate 1 E2E Tests

**Files:**
- Create: `tests/e2e/__init__.py`
- Create: `tests/e2e/fixtures/__init__.py`
- Create: `tests/e2e/fixtures/sentinel_jarvis.py`
- Create: `tests/e2e/conftest.py`
- Create: `tests/e2e/test_gate1_sentinel.py`

**Step 1: Create fixtures and conftest**

Create `tests/e2e/__init__.py` and `tests/e2e/fixtures/__init__.py` as empty files.

Create `tests/e2e/fixtures/sentinel_jarvis.py`:

```python
"""Sentinel file with deliberately high cyclomatic complexity for E2E testing."""


def process_command(cmd: str, flags: dict) -> str:
    if cmd == "start":
        if flags.get("verbose"):
            if flags.get("debug"):
                return "start-verbose-debug"
            return "start-verbose"
        return "start"
    elif cmd == "stop":
        if flags.get("force"):
            return "stop-force"
        return "stop"
    elif cmd == "restart":
        if flags.get("graceful"):
            if flags.get("timeout"):
                return "restart-graceful-timeout"
            return "restart-graceful"
        return "restart"
    elif cmd == "status":
        if flags.get("json"):
            return "status-json"
        if flags.get("verbose"):
            return "status-verbose"
        return "status"
    elif cmd == "config":
        if flags.get("validate"):
            return "config-validate"
        if flags.get("reset"):
            return "config-reset"
        return "config"
    else:
        return "unknown"
```

Create `tests/e2e/conftest.py`:

```python
"""Shared fixtures for E2E saga tests."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Dict, Tuple

import pytest


def _init_test_repo(path: Path, name: str) -> str:
    """Initialize a git repo with sentinel file. Returns HEAD SHA."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.email", "test@jarvis.local"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.name", "JARVIS Test"], cwd=str(path), check=True)
    sentinel_src = Path(__file__).parent / "fixtures" / "sentinel_jarvis.py"
    if sentinel_src.exists():
        (path / "sentinel.py").write_text(sentinel_src.read_text())
    else:
        (path / "sentinel.py").write_text("def foo():\n    return 1\n")
    (path / ".jarvis").mkdir(exist_ok=True)
    subprocess.run(["git", "add", "."], cwd=str(path), check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", f"init {name}", "--no-verify"],
        cwd=str(path), check=True,
    )
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(path), capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


@pytest.fixture
def e2e_repo_roots(tmp_path: Path) -> Tuple[Dict[str, Path], Dict[str, str]]:
    """Create jarvis + prime + reactor-core git repos."""
    roots: Dict[str, Path] = {}
    shas: Dict[str, str] = {}
    for name in ("jarvis", "prime", "reactor-core"):
        root = tmp_path / name
        sha = _init_test_repo(root, name)
        roots[name] = root
        shas[name] = sha
    return roots, shas


JPRIME_AVAILABLE = os.getenv("JARVIS_PRIME_ENDPOINT", "") != ""

jprime = pytest.mark.skipif(
    not JPRIME_AVAILABLE,
    reason="JARVIS_PRIME_ENDPOINT not set — skipping J-Prime tests",
)
```

**Step 2: Write Gate 1 tests**

Create `tests/e2e/test_gate1_sentinel.py` — see Task 6 tests in the design doc, covering: saga branch lifecycle, commit identity, ff-only promote, rollback on verify failure, TARGET_MOVED abort, dirty tree rejection, deterministic lock order, orphan branch detection. Full test code provided in the design doc.

**Step 3: Run**

Run: `python3 -m pytest tests/e2e/test_gate1_sentinel.py -v`
Expected: PASS (all CI-safe tests)

**Step 4: Commit**

```bash
git add tests/e2e/
git commit -m "test(e2e): Gate 1 deterministic sentinel tests for B+ saga lifecycle"
```

---

### Task 7: Gate 2 Stubs + Synthetic Backlog

**Files:**
- Create: `tests/e2e/test_gate2_backlog.py`
- Create: `tests/e2e/fixtures/synthetic_backlog.json`

**Step 1: Create fixtures and stubs**

Create `tests/e2e/fixtures/synthetic_backlog.json`:
```json
{
  "items": [
    {
      "id": "e2e-backlog-001",
      "description": "Refactor sentinel.py process_command to use dispatch table",
      "target_files": ["sentinel.py"],
      "repo_scope": ["jarvis", "prime", "reactor-core"],
      "priority": "low",
      "source": "e2e_test"
    }
  ]
}
```

Create `tests/e2e/test_gate2_backlog.py`:

```python
"""Gate 2 — Real backlog acceptance E2E tests. Require live J-Prime."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from .conftest import jprime

pytestmark = jprime


@pytest.fixture
def synthetic_backlog() -> dict:
    fixture = Path(__file__).parent / "fixtures" / "synthetic_backlog.json"
    return json.loads(fixture.read_text())


class TestG2RealBacklogE2E:
    async def test_real_generation_and_apply(self, e2e_repo_roots, synthetic_backlog) -> None:
        pytest.skip("Gate 2: run manually after Gate 1 passes with live J-Prime")


class TestG2GenerationVariability:
    async def test_two_runs_both_succeed(self, e2e_repo_roots, synthetic_backlog) -> None:
        pytest.skip("Gate 2: run manually after Gate 1 passes with live J-Prime")


class TestG2FailureTransparency:
    async def test_no_silent_failures(self, e2e_repo_roots, synthetic_backlog) -> None:
        pytest.skip("Gate 2: run manually after Gate 1 passes with live J-Prime")
```

**Step 2: Commit**

```bash
git add tests/e2e/test_gate2_backlog.py tests/e2e/fixtures/synthetic_backlog.json
git commit -m "test(e2e): Gate 2 backlog acceptance test stubs (requires live J-Prime)"
```

---

### Task 8: Full Regression Check

**Step 1: Run all governance tests**

Run: `python3 -m pytest tests/test_ouroboros_governance/ -v --tb=short`
Expected: All existing tests pass

**Step 2: Run new B+ tests**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_saga_types_bplus.py tests/test_ouroboros_governance/test_repo_lock.py tests/test_ouroboros_governance/test_saga_bplus_branches.py -v`
Expected: All PASS

**Step 3: Run Gate 1 E2E**

Run: `python3 -m pytest tests/e2e/test_gate1_sentinel.py -v`
Expected: All PASS

---

## Execution Summary

| Task | Files | What |
|------|-------|------|
| 1 | saga_types.py + test | SAGA_PARTIAL_PROMOTE + SagaLedgerArtifact |
| 2 | repo_lock.py + test | RepoLockManager: two-tier locks |
| 3 | saga_apply_strategy.py + test | B+ branch lifecycle (largest task) |
| 4 | orchestrator.py + test | PARTIAL_PROMOTE handling |
| 5 | governed_loop_service.py | Orphan branch health wiring |
| 6 | tests/e2e/* | Gate 1: 10 deterministic tests |
| 7 | tests/e2e/* | Gate 2: 3 acceptance test stubs |
| 8 | (verification) | Full regression check |

**Total: 8 tasks, ~900 lines of code + tests**
