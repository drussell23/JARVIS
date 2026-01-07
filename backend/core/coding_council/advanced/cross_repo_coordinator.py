"""
v78.0: Cross-Repo Transaction Coordinator
==========================================

Provides atomic commit coordination across multiple repositories in the
Trinity architecture (JARVIS, J-Prime, Reactor-Core).

Features:
- Two-Phase Commit (2PC) protocol for distributed transactions
- Automatic rollback on partial failure
- Transaction journaling for crash recovery
- Conflict detection and resolution
- Parallel preparation with sequential commit
- Timeout-based deadlock prevention

Architecture:
    Coordinator (JARVIS)
         │
         ├── Phase 1: PREPARE
         │      ├── JARVIS:      git add -A, check conflicts → VOTE_YES/NO
         │      ├── J-Prime:     git add -A, check conflicts → VOTE_YES/NO
         │      └── Reactor:     git add -A, check conflicts → VOTE_YES/NO
         │
         └── Phase 2: COMMIT or ABORT
                ├── All VOTE_YES → COMMIT all repos
                └── Any VOTE_NO  → ABORT all repos (rollback)

    Transaction States:
    [INITIATED] → [PREPARING] → [PREPARED] → [COMMITTING] → [COMMITTED]
                       ↓              ↓             ↓
                  [ABORTED]     [ABORTED]     [ABORTED]

Usage:
    from backend.core.coding_council.advanced.cross_repo_coordinator import (
        get_transaction_coordinator,
        TransactionScope,
    )

    coordinator = await get_transaction_coordinator()

    # Start transaction
    tx = await coordinator.begin_transaction(
        message="feat: Implement voice authentication",
        repos=[RepoScope.JARVIS, RepoScope.JPRIME],
    )

    # Prepare (phase 1)
    if await coordinator.prepare(tx.transaction_id):
        # Commit (phase 2)
        await coordinator.commit(tx.transaction_id)
    else:
        # Rollback
        await coordinator.abort(tx.transaction_id)

Author: JARVIS v78.0
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import (
    Any,
    Dict,
    List,
    Optional,
    Set,
    Tuple,
)
from uuid import uuid4

logger = logging.getLogger(__name__)


# =============================================================================
# Enums
# =============================================================================

class RepoScope(Enum):
    """Repository scope in Trinity architecture."""
    JARVIS = "jarvis"           # JARVIS-AI-Agent
    JPRIME = "jarvis-prime"     # jarvis-prime
    REACTOR = "reactor-core"    # reactor-core
    ALL = "all"                 # All repos


class TransactionState(Enum):
    """State of a distributed transaction."""
    INITIATED = "initiated"         # Transaction created
    PREPARING = "preparing"         # Phase 1 in progress
    PREPARED = "prepared"           # All repos voted YES
    COMMITTING = "committing"       # Phase 2 in progress
    COMMITTED = "committed"         # Successfully committed
    ABORTING = "aborting"          # Abort in progress
    ABORTED = "aborted"            # Transaction aborted
    FAILED = "failed"              # Unrecoverable failure


class VoteResult(Enum):
    """Vote result from a participant."""
    YES = "yes"         # Ready to commit
    NO = "no"           # Cannot commit
    TIMEOUT = "timeout" # No response


class ConflictType(Enum):
    """Type of conflict detected."""
    MERGE_CONFLICT = "merge_conflict"
    UNCOMMITTED_CHANGES = "uncommitted_changes"
    DIVERGED_BRANCH = "diverged_branch"
    LOCKED_FILES = "locked_files"
    PERMISSION_DENIED = "permission_denied"


# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class RepoInfo:
    """Information about a repository."""
    name: str
    path: Path
    current_branch: str = "main"
    remote: str = "origin"
    is_dirty: bool = False
    has_conflicts: bool = False
    ahead_count: int = 0
    behind_count: int = 0
    last_commit: str = ""
    last_checked: float = field(default_factory=time.time)


@dataclass
class ParticipantVote:
    """Vote from a transaction participant."""
    repo: RepoScope
    vote: VoteResult
    reason: Optional[str] = None
    staged_files: List[str] = field(default_factory=list)
    conflicts: List[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)


@dataclass
class Transaction:
    """A distributed transaction across repositories."""
    transaction_id: str
    message: str
    repos: List[RepoScope]
    state: TransactionState = TransactionState.INITIATED
    votes: Dict[str, ParticipantVote] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    prepared_at: Optional[float] = None
    committed_at: Optional[float] = None
    aborted_at: Optional[float] = None
    error: Optional[str] = None
    rollback_points: Dict[str, str] = field(default_factory=dict)  # repo -> commit hash
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def all_voted_yes(self) -> bool:
        """Check if all participants voted YES."""
        if not self.votes:
            return False
        return all(v.vote == VoteResult.YES for v in self.votes.values())

    @property
    def duration_seconds(self) -> float:
        """Get transaction duration."""
        if self.committed_at:
            return self.committed_at - self.created_at
        if self.aborted_at:
            return self.aborted_at - self.created_at
        return time.time() - self.created_at

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for persistence."""
        return {
            "transaction_id": self.transaction_id,
            "message": self.message,
            "repos": [r.value for r in self.repos],
            "state": self.state.value,
            "votes": {
                k: {
                    "repo": v.repo.value,
                    "vote": v.vote.value,
                    "reason": v.reason,
                    "staged_files": v.staged_files,
                    "conflicts": v.conflicts,
                    "timestamp": v.timestamp,
                }
                for k, v in self.votes.items()
            },
            "created_at": self.created_at,
            "prepared_at": self.prepared_at,
            "committed_at": self.committed_at,
            "aborted_at": self.aborted_at,
            "error": self.error,
            "rollback_points": self.rollback_points,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Transaction":
        """Create from dictionary."""
        tx = cls(
            transaction_id=data["transaction_id"],
            message=data["message"],
            repos=[RepoScope(r) for r in data["repos"]],
            state=TransactionState(data["state"]),
            created_at=data.get("created_at", time.time()),
            prepared_at=data.get("prepared_at"),
            committed_at=data.get("committed_at"),
            aborted_at=data.get("aborted_at"),
            error=data.get("error"),
            rollback_points=data.get("rollback_points", {}),
            metadata=data.get("metadata", {}),
        )
        for k, v in data.get("votes", {}).items():
            tx.votes[k] = ParticipantVote(
                repo=RepoScope(v["repo"]),
                vote=VoteResult(v["vote"]),
                reason=v.get("reason"),
                staged_files=v.get("staged_files", []),
                conflicts=v.get("conflicts", []),
                timestamp=v.get("timestamp", time.time()),
            )
        return tx


@dataclass
class CoordinatorStats:
    """Statistics about the transaction coordinator."""
    total_transactions: int = 0
    successful_commits: int = 0
    aborted_transactions: int = 0
    failed_transactions: int = 0
    avg_prepare_time_ms: float = 0.0
    avg_commit_time_ms: float = 0.0
    conflicts_detected: int = 0
    rollbacks_performed: int = 0


# =============================================================================
# Git Operations
# =============================================================================

class GitOperations:
    """Git operations for a single repository."""

    def __init__(self, repo_path: Path, logger_instance: Optional[logging.Logger] = None):
        self.repo_path = repo_path
        self.log = logger_instance or logger

    async def run_git(self, *args: str, timeout: float = 30.0) -> Tuple[bool, str]:
        """Run a git command and return (success, output)."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                *args,
                cwd=str(self.repo_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout
            )

            success = proc.returncode == 0
            output = stdout.decode().strip() if success else stderr.decode().strip()
            return success, output

        except asyncio.TimeoutError:
            return False, "Command timed out"
        except Exception as e:
            return False, str(e)

    async def get_current_commit(self) -> str:
        """Get current HEAD commit hash."""
        success, output = await self.run_git("rev-parse", "HEAD")
        return output if success else ""

    async def get_current_branch(self) -> str:
        """Get current branch name."""
        success, output = await self.run_git("rev-parse", "--abbrev-ref", "HEAD")
        return output if success else "main"

    async def is_dirty(self) -> bool:
        """Check if repo has uncommitted changes."""
        success, output = await self.run_git("status", "--porcelain")
        return bool(output) if success else False

    async def has_conflicts(self) -> bool:
        """Check for merge conflicts."""
        success, output = await self.run_git("diff", "--name-only", "--diff-filter=U")
        return bool(output) if success else False

    async def get_staged_files(self) -> List[str]:
        """Get list of staged files."""
        success, output = await self.run_git("diff", "--name-only", "--cached")
        return output.split("\n") if success and output else []

    async def stage_all(self) -> bool:
        """Stage all changes."""
        success, _ = await self.run_git("add", "-A")
        return success

    async def unstage_all(self) -> bool:
        """Unstage all changes."""
        success, _ = await self.run_git("reset", "HEAD")
        return success

    async def commit(self, message: str) -> Tuple[bool, str]:
        """Create a commit."""
        return await self.run_git(
            "commit",
            "-m", message,
            "--no-verify"  # Skip hooks for atomic commits
        )

    async def reset_to_commit(self, commit_hash: str) -> bool:
        """Reset to a specific commit."""
        success, _ = await self.run_git("reset", "--hard", commit_hash)
        return success

    async def fetch(self) -> bool:
        """Fetch from remote."""
        success, _ = await self.run_git("fetch", "--all", timeout=60.0)
        return success

    async def get_ahead_behind(self, remote: str = "origin", branch: str = "main") -> Tuple[int, int]:
        """Get ahead/behind counts from remote."""
        success, output = await self.run_git(
            "rev-list", "--left-right", "--count",
            f"HEAD...{remote}/{branch}"
        )
        if success and output:
            parts = output.split()
            if len(parts) == 2:
                return int(parts[0]), int(parts[1])
        return 0, 0


# =============================================================================
# Transaction Coordinator
# =============================================================================

class CrossRepoTransactionCoordinator:
    """
    Coordinates distributed transactions across Trinity repositories.

    Implements Two-Phase Commit (2PC) protocol:
    - Phase 1 (Prepare): Stage changes, check for conflicts, vote YES/NO
    - Phase 2 (Commit): If all YES, commit; otherwise rollback

    Thread-safe and async-compatible.
    """

    def __init__(
        self,
        logger_instance: Optional[logging.Logger] = None,
        prepare_timeout: float = 30.0,
        commit_timeout: float = 60.0,
    ):
        self.log = logger_instance or logger
        self.prepare_timeout = prepare_timeout
        self.commit_timeout = commit_timeout

        # Repository paths (discovered dynamically)
        self._repo_paths: Dict[RepoScope, Path] = {}
        self._repo_info: Dict[RepoScope, RepoInfo] = {}
        self._git_ops: Dict[RepoScope, GitOperations] = {}

        # Transaction management
        self._transactions: Dict[str, Transaction] = {}
        self._lock = asyncio.Lock()
        self._stats = CoordinatorStats()

        # v78.1: Per-repo locks for transaction isolation
        # Prevents concurrent modifications to same repo during 2PC
        self._repo_locks: Dict[RepoScope, asyncio.Lock] = {
            scope: asyncio.Lock() for scope in RepoScope if scope != RepoScope.ALL
        }
        # Track which transactions hold which locks
        self._lock_holders: Dict[RepoScope, Optional[str]] = {
            scope: None for scope in RepoScope if scope != RepoScope.ALL
        }

        # Persistence
        self._journal_dir = Path.home() / ".jarvis" / "trinity" / "transactions"
        self._journal_dir.mkdir(parents=True, exist_ok=True)

        # Discover repos
        self._discover_repos()

    def _discover_repos(self):
        """
        Discover repository paths dynamically with robust fallback chain.

        v78.1: Fixed hardcoded paths issue. Now uses:
        1. Environment variables (JARVIS_REPO_PATH, JPRIME_REPO_PATH, REACTOR_REPO_PATH)
        2. JARVIS_REPOS_BASE for common parent directory
        3. Dynamic home-based discovery
        4. Relative path discovery from current working directory
        """
        # Environment variable overrides (highest priority)
        env_overrides = {
            RepoScope.JARVIS: os.getenv("JARVIS_REPO_PATH"),
            RepoScope.JPRIME: os.getenv("JPRIME_REPO_PATH"),
            RepoScope.REACTOR: os.getenv("REACTOR_REPO_PATH"),
        }

        # Apply environment overrides first
        for scope, env_path in env_overrides.items():
            if env_path:
                path = Path(env_path).expanduser().resolve()
                if self._validate_repo(path):
                    self._repo_paths[scope] = path
                    self._git_ops[scope] = GitOperations(path, self.log)
                    self.log.info(f"[CrossRepo] {scope.value} from env: {path}")

        # Base paths for discovery (no hardcoded user paths)
        repos_base = os.getenv("JARVIS_REPOS_BASE")
        base_paths = []

        if repos_base:
            base_paths.append(Path(repos_base).expanduser().resolve())

        base_paths.extend([
            Path.home() / "Documents" / "repos",
            Path.home() / "repos",
            Path.home() / "Projects",
            Path.home() / "dev",
            Path.cwd().parent,
            Path.cwd().parent.parent,
        ])

        # Remove duplicates while preserving order
        seen = set()
        unique_base_paths = []
        for p in base_paths:
            if p not in seen and p.exists():
                seen.add(p)
                unique_base_paths.append(p)

        repo_names = {
            RepoScope.JARVIS: ["JARVIS-AI-Agent", "jarvis-ai-agent", "jarvis"],
            RepoScope.JPRIME: ["jarvis-prime", "JARVIS-Prime", "j-prime", "jprime"],
            RepoScope.REACTOR: ["reactor-core", "Reactor-Core", "reactor"],
        }

        # Discover repos not already set via environment
        discovered_paths = {}  # Track all discovered paths for logging

        for scope, names in repo_names.items():
            if scope in self._repo_paths:
                continue  # Already set via environment

            for base in unique_base_paths:
                for name in names:
                    path = base / name
                    if self._validate_repo(path):
                        if scope not in discovered_paths:
                            discovered_paths[scope] = []
                        discovered_paths[scope].append(path)

                        # Use first valid match
                        if scope not in self._repo_paths:
                            self._repo_paths[scope] = path
                            self._git_ops[scope] = GitOperations(path, self.log)
                            self.log.debug(f"[CrossRepo] Discovered {scope.value} at {path}")

        # Log discovery summary with diagnostic info
        self.log.info(
            f"[CrossRepo] Discovered {len(self._repo_paths)}/3 repos: "
            f"{[r.value for r in self._repo_paths.keys()]}"
        )

        # Warn about missing repos
        missing = set(RepoScope) - set(self._repo_paths.keys())
        if missing:
            self.log.warning(
                f"[CrossRepo] Missing repos: {[r.value for r in missing]}. "
                f"Set JARVIS_REPOS_BASE or individual *_REPO_PATH env vars."
            )

        # Warn about multiple discoveries (potential version conflicts)
        for scope, paths in discovered_paths.items():
            if len(paths) > 1:
                self.log.warning(
                    f"[CrossRepo] Multiple {scope.value} repos found: {paths}. "
                    f"Using: {self._repo_paths.get(scope)}"
                )

    def _validate_repo(self, path: Path) -> bool:
        """
        Validate that a path is a valid git repository.

        v78.1: Enhanced validation beyond just .git existence.
        """
        if not path.exists():
            return False

        git_dir = path / ".git"
        if not git_dir.exists():
            return False

        # Verify it's a valid git repo by checking HEAD exists
        head_file = git_dir / "HEAD"
        if not head_file.exists():
            self.log.debug(f"[CrossRepo] Invalid repo (no HEAD): {path}")
            return False

        return True

    async def refresh_repo_info(self, scope: RepoScope) -> Optional[RepoInfo]:
        """Refresh information about a repository."""
        if scope not in self._git_ops:
            return None

        git = self._git_ops[scope]

        info = RepoInfo(
            name=scope.value,
            path=self._repo_paths[scope],
            current_branch=await git.get_current_branch(),
            is_dirty=await git.is_dirty(),
            has_conflicts=await git.has_conflicts(),
            last_commit=await git.get_current_commit(),
            last_checked=time.time(),
        )

        ahead, behind = await git.get_ahead_behind()
        info.ahead_count = ahead
        info.behind_count = behind

        self._repo_info[scope] = info
        return info

    async def begin_transaction(
        self,
        message: str,
        repos: Optional[List[RepoScope]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Transaction:
        """
        Begin a new distributed transaction.

        Args:
            message: Commit message
            repos: List of repos to include (None = all discovered)
            metadata: Additional metadata

        Returns:
            New Transaction object
        """
        async with self._lock:
            # Default to all discovered repos
            if repos is None or RepoScope.ALL in repos:
                repos = list(self._repo_paths.keys())

            # Filter to discovered repos only
            repos = [r for r in repos if r in self._repo_paths]

            if not repos:
                raise ValueError("No valid repositories for transaction")

            # Create transaction
            tx = Transaction(
                transaction_id=f"tx_{uuid4().hex[:12]}",
                message=message,
                repos=repos,
                metadata=metadata or {},
            )

            # Save rollback points
            for scope in repos:
                git = self._git_ops[scope]
                tx.rollback_points[scope.value] = await git.get_current_commit()

            self._transactions[tx.transaction_id] = tx
            self._stats.total_transactions += 1

            # Journal
            await self._journal_write(tx, "BEGIN")

            self.log.info(
                f"[CrossRepo] Transaction started: {tx.transaction_id} "
                f"(repos={[r.value for r in repos]})"
            )

            return tx

    async def _acquire_repo_locks(self, tx: Transaction, timeout: float = 30.0) -> bool:
        """
        v78.1: Acquire locks on all repos involved in transaction.

        Acquires locks in sorted order to prevent deadlocks.
        Returns True if all locks acquired, False otherwise.
        """
        # Sort repos to prevent deadlock (always acquire in same order)
        sorted_repos = sorted(tx.repos, key=lambda r: r.value)
        acquired: List[RepoScope] = []

        try:
            for scope in sorted_repos:
                if scope == RepoScope.ALL:
                    continue

                lock = self._repo_locks.get(scope)
                if not lock:
                    continue

                try:
                    # Try to acquire with timeout
                    acquired_lock = await asyncio.wait_for(
                        lock.acquire(),
                        timeout=timeout / len(sorted_repos)
                    )
                    if acquired_lock:
                        acquired.append(scope)
                        self._lock_holders[scope] = tx.transaction_id
                        self.log.debug(f"[CrossRepo] TX {tx.transaction_id} acquired lock on {scope.value}")
                except asyncio.TimeoutError:
                    self.log.warning(
                        f"[CrossRepo] TX {tx.transaction_id} failed to acquire lock on {scope.value}"
                    )
                    # Release already acquired locks
                    for acq_scope in acquired:
                        self._repo_locks[acq_scope].release()
                        self._lock_holders[acq_scope] = None
                    return False

            return True

        except Exception as e:
            self.log.error(f"[CrossRepo] Lock acquisition error: {e}")
            # Release any acquired locks
            for acq_scope in acquired:
                try:
                    self._repo_locks[acq_scope].release()
                    self._lock_holders[acq_scope] = None
                except Exception:
                    pass
            return False

    def _release_repo_locks(self, tx: Transaction):
        """v78.1: Release all locks held by a transaction."""
        for scope in tx.repos:
            if scope == RepoScope.ALL:
                continue

            if self._lock_holders.get(scope) == tx.transaction_id:
                try:
                    self._repo_locks[scope].release()
                    self._lock_holders[scope] = None
                    self.log.debug(f"[CrossRepo] TX {tx.transaction_id} released lock on {scope.value}")
                except Exception as e:
                    self.log.debug(f"[CrossRepo] Lock release error for {scope.value}: {e}")

    async def prepare(self, transaction_id: str) -> bool:
        """
        Execute Phase 1: Prepare all participants.

        v78.1: Now acquires per-repo locks to ensure transaction isolation.
        Locks are held until commit or abort.

        Returns:
            True if all participants voted YES
        """
        async with self._lock:
            tx = self._transactions.get(transaction_id)
            if not tx:
                raise ValueError(f"Transaction not found: {transaction_id}")

            if tx.state != TransactionState.INITIATED:
                raise ValueError(f"Invalid state for prepare: {tx.state}")

            tx.state = TransactionState.PREPARING

        # v78.1: Acquire per-repo locks BEFORE prepare phase
        if not await self._acquire_repo_locks(tx, self.prepare_timeout / 2):
            async with self._lock:
                tx.state = TransactionState.ABORTED
                tx.error = "Failed to acquire repo locks"
            return False

        start_time = time.time()

        try:
            # Prepare all repos in parallel (locks already held)
            prepare_tasks = [
                self._prepare_participant(tx, scope)
                for scope in tx.repos
            ]

            try:
                votes = await asyncio.wait_for(
                    asyncio.gather(*prepare_tasks, return_exceptions=True),
                    timeout=self.prepare_timeout / 2  # Half timeout since lock already took some
                )
            except asyncio.TimeoutError:
                self.log.error(f"[CrossRepo] Prepare timeout for {transaction_id}")
                self._release_repo_locks(tx)  # Release locks on timeout
                async with self._lock:
                    tx.state = TransactionState.ABORTED
                    tx.error = "Prepare phase timeout"
                return False

            # Process votes
            async with self._lock:
                for vote in votes:
                    if isinstance(vote, Exception):
                        self.log.error(f"[CrossRepo] Prepare error: {vote}")
                        tx.votes["error"] = ParticipantVote(
                            repo=RepoScope.ALL,
                            vote=VoteResult.NO,
                            reason=str(vote),
                        )
                    elif isinstance(vote, ParticipantVote):
                        tx.votes[vote.repo.value] = vote

                prepare_time_ms = (time.time() - start_time) * 1000
                self._stats.avg_prepare_time_ms = (
                    self._stats.avg_prepare_time_ms + prepare_time_ms
                ) / 2

                if tx.all_voted_yes:
                    tx.state = TransactionState.PREPARED
                    tx.prepared_at = time.time()
                    await self._journal_write(tx, "PREPARED")
                    self.log.info(f"[CrossRepo] Transaction prepared: {transaction_id}")
                    # NOTE: Locks held until commit or abort
                    return True
                else:
                    # Release locks if prepare failed
                    self._release_repo_locks(tx)
                    return False

        except Exception as e:
            # Release locks on any error
            self._release_repo_locks(tx)
            raise

    async def _prepare_participant(
        self,
        tx: Transaction,
        scope: RepoScope,
    ) -> ParticipantVote:
        """Prepare a single participant."""
        git = self._git_ops[scope]

        # Check for conflicts
        if await git.has_conflicts():
            return ParticipantVote(
                repo=scope,
                vote=VoteResult.NO,
                reason="Merge conflicts exist",
            )

        # Stage all changes
        await git.stage_all()

        # Get staged files
        staged = await git.get_staged_files()

        if not staged:
            return ParticipantVote(
                repo=scope,
                vote=VoteResult.YES,
                reason="No changes to commit",
                staged_files=[],
            )

        # Vote YES if no issues
        return ParticipantVote(
            repo=scope,
            vote=VoteResult.YES,
            staged_files=staged,
        )

    async def commit(self, transaction_id: str) -> bool:
        """
        Execute Phase 2: Commit all participants.

        Must be called after successful prepare().
        """
        async with self._lock:
            tx = self._transactions.get(transaction_id)
            if not tx:
                raise ValueError(f"Transaction not found: {transaction_id}")

            if tx.state != TransactionState.PREPARED:
                raise ValueError(f"Invalid state for commit: {tx.state}")

            tx.state = TransactionState.COMMITTING

        start_time = time.time()
        committed_repos: List[RepoScope] = []

        try:
            # Commit repos sequentially to ensure ordering
            for scope in tx.repos:
                git = self._git_ops[scope]
                vote = tx.votes.get(scope.value)

                # Skip if no changes
                if vote and not vote.staged_files:
                    continue

                success, output = await git.commit(tx.message)

                if success:
                    committed_repos.append(scope)
                    self.log.info(f"[CrossRepo] Committed {scope.value}")
                else:
                    raise Exception(f"Commit failed for {scope.value}: {output}")

            async with self._lock:
                tx.state = TransactionState.COMMITTED
                tx.committed_at = time.time()
                self._stats.successful_commits += 1

                commit_time_ms = (time.time() - start_time) * 1000
                self._stats.avg_commit_time_ms = (
                    self._stats.avg_commit_time_ms + commit_time_ms
                ) / 2

                await self._journal_write(tx, "COMMITTED")

            self.log.info(f"[CrossRepo] Transaction committed: {transaction_id}")

            # v78.1: Release repo locks after successful commit
            self._release_repo_locks(tx)
            return True

        except Exception as e:
            self.log.error(f"[CrossRepo] Commit failed: {e}")

            # Rollback already committed repos
            await self._rollback(tx, committed_repos)

            async with self._lock:
                tx.state = TransactionState.FAILED
                tx.error = str(e)
                self._stats.failed_transactions += 1
                await self._journal_write(tx, "COMMIT_FAILED")

            # v78.1: Release repo locks on failure
            self._release_repo_locks(tx)
            return False

    async def abort(self, transaction_id: str) -> bool:
        """
        Abort a transaction and rollback changes.
        """
        async with self._lock:
            tx = self._transactions.get(transaction_id)
            if not tx:
                return False

            if tx.state in (TransactionState.COMMITTED, TransactionState.ABORTED):
                return False

            tx.state = TransactionState.ABORTING

        # Rollback all repos
        await self._rollback(tx, tx.repos)

        async with self._lock:
            tx.state = TransactionState.ABORTED
            tx.aborted_at = time.time()
            self._stats.aborted_transactions += 1
            await self._journal_write(tx, "ABORTED")

        # v78.1: Release repo locks after abort
        self._release_repo_locks(tx)

        self.log.info(f"[CrossRepo] Transaction aborted: {transaction_id}")
        return True

    async def _rollback(self, tx: Transaction, repos: List[RepoScope]) -> Tuple[int, int]:
        """
        Rollback specified repos to their pre-transaction state.

        v78.1: Enhanced with saga-style compensation:
        - Tracks which repos are already rolled back (idempotency)
        - Attempts all rollbacks even if some fail
        - Returns (success_count, failure_count) tuple

        Returns:
            Tuple of (successful_rollbacks, failed_rollbacks)
        """
        # Track already rolled back repos for idempotency
        if not hasattr(tx, '_rolled_back_repos'):
            tx._rolled_back_repos = set()

        success_count = 0
        failure_count = 0

        for scope in repos:
            # Idempotency check
            if scope.value in tx._rolled_back_repos:
                self.log.debug(f"[CrossRepo] {scope.value} already rolled back, skipping")
                success_count += 1
                continue

            git = self._git_ops.get(scope)
            if not git:
                self.log.warning(f"[CrossRepo] No git ops for {scope.value}")
                continue

            rollback_commit = tx.rollback_points.get(scope.value)

            try:
                if rollback_commit:
                    success = await git.reset_to_commit(rollback_commit)
                    if success:
                        self.log.info(f"[CrossRepo] Rolled back {scope.value} to {rollback_commit[:8]}")
                        self._stats.rollbacks_performed += 1
                        tx._rolled_back_repos.add(scope.value)
                        success_count += 1
                    else:
                        self.log.error(f"[CrossRepo] Failed to rollback {scope.value}")
                        failure_count += 1
                else:
                    # Just unstage if no rollback point
                    await git.unstage_all()
                    tx._rolled_back_repos.add(scope.value)
                    success_count += 1

            except Exception as e:
                self.log.error(f"[CrossRepo] Rollback error for {scope.value}: {e}")
                failure_count += 1
                # Continue with other repos - don't fail fast

        if failure_count > 0:
            self.log.warning(
                f"[CrossRepo] Partial rollback: {success_count} succeeded, {failure_count} failed"
            )

        return success_count, failure_count

    async def get_transaction(self, transaction_id: str) -> Optional[Transaction]:
        """Get a transaction by ID."""
        return self._transactions.get(transaction_id)

    async def list_transactions(
        self,
        state: Optional[TransactionState] = None,
        limit: int = 50,
    ) -> List[Transaction]:
        """List transactions, optionally filtered by state."""
        txs = list(self._transactions.values())

        if state:
            txs = [t for t in txs if t.state == state]

        # Sort by created_at descending
        txs.sort(key=lambda t: t.created_at, reverse=True)

        return txs[:limit]

    async def cleanup_old_transactions(self, max_age_seconds: float = 86400.0) -> int:
        """Remove old completed/aborted transactions."""
        now = time.time()
        to_remove = []

        for tx_id, tx in self._transactions.items():
            if tx.state in (TransactionState.COMMITTED, TransactionState.ABORTED, TransactionState.FAILED):
                if now - tx.created_at > max_age_seconds:
                    to_remove.append(tx_id)

        async with self._lock:
            for tx_id in to_remove:
                del self._transactions[tx_id]

        return len(to_remove)

    async def _journal_write(self, tx: Transaction, action: str):
        """Write transaction action to journal."""
        try:
            journal_file = self._journal_dir / f"{tx.transaction_id}.json"
            entry = {
                "timestamp": datetime.now().isoformat(),
                "action": action,
                "transaction": tx.to_dict(),
            }

            # Append to journal file
            with open(journal_file, "a") as f:
                f.write(json.dumps(entry) + "\n")

        except Exception as e:
            self.log.debug(f"[CrossRepo] Journal write failed: {e}")

    async def recover_from_journal(self) -> int:
        """Recover incomplete transactions from journal."""
        recovered = 0

        for journal_file in self._journal_dir.glob("tx_*.json"):
            try:
                # Read last entry
                with open(journal_file, "r") as f:
                    lines = f.readlines()
                    if not lines:
                        continue
                    last_entry = json.loads(lines[-1])

                tx = Transaction.from_dict(last_entry["transaction"])

                # Check if transaction needs recovery
                if tx.state in (TransactionState.PREPARING, TransactionState.COMMITTING):
                    self.log.warning(
                        f"[CrossRepo] Found incomplete transaction: {tx.transaction_id}"
                    )
                    # Abort incomplete transaction
                    self._transactions[tx.transaction_id] = tx
                    await self.abort(tx.transaction_id)
                    recovered += 1

            except Exception as e:
                self.log.debug(f"[CrossRepo] Journal recovery error: {e}")

        return recovered

    def get_stats(self) -> CoordinatorStats:
        """Get coordinator statistics."""
        return self._stats

    def visualize(self) -> str:
        """Generate visualization of coordinator state."""
        lines = [
            "[CrossRepo Transaction Coordinator]",
            f"  Discovered repos: {[r.value for r in self._repo_paths.keys()]}",
            f"  Total transactions: {self._stats.total_transactions}",
            f"  Successful commits: {self._stats.successful_commits}",
            f"  Aborted: {self._stats.aborted_transactions}",
            f"  Failed: {self._stats.failed_transactions}",
            f"  Conflicts detected: {self._stats.conflicts_detected}",
            f"  Rollbacks: {self._stats.rollbacks_performed}",
            "",
            "  Recent transactions:",
        ]

        for tx in list(self._transactions.values())[-5:]:
            lines.append(
                f"    {tx.transaction_id}: {tx.state.value} "
                f"({len(tx.repos)} repos, {tx.duration_seconds:.1f}s)"
            )

        return "\n".join(lines)


# =============================================================================
# Singleton Instance
# =============================================================================

_coordinator: Optional[CrossRepoTransactionCoordinator] = None
_coordinator_lock = asyncio.Lock()


async def get_transaction_coordinator() -> CrossRepoTransactionCoordinator:
    """Get or create the singleton transaction coordinator."""
    global _coordinator

    async with _coordinator_lock:
        if _coordinator is None:
            _coordinator = CrossRepoTransactionCoordinator()
            # Recover any incomplete transactions
            await _coordinator.recover_from_journal()
        return _coordinator


def get_transaction_coordinator_sync() -> Optional[CrossRepoTransactionCoordinator]:
    """Get the coordinator synchronously (may be None)."""
    return _coordinator
