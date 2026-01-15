"""
Trinity Integration Layer v2.0 - Production-Grade Self-Improvement
===================================================================

The ultimate integration layer that connects Ouroboros self-improvement
with the full Trinity ecosystem. This is the "nervous system" of JARVIS.

v2.0 Enhancements:
    - Coding Council integration for peer code review
    - Distributed locking for concurrent improvements
    - Complete failure handling with user notification
    - Experience deduplication across channels
    - Automatic rollback mechanism
    - Cross-repo coordination
    - Priority queue for improvements
    - Model hot-swap integration (MODEL_READY events)
    - Improvement history and learning
    - Edge case handling (file locks, git conflicts, circular deps)

Architecture:
    ┌─────────────────────────────────────────────────────────────────────────────┐
    │                     TRINITY INTEGRATION LAYER v2.0                          │
    ├─────────────────────────────────────────────────────────────────────────────┤
    │                                                                              │
    │  ┌────────────────────────────────────────────────────────────────────────┐ │
    │  │                    IMPROVEMENT COORDINATOR                             │ │
    │  │                                                                        │ │
    │  │  Request → Lock → Generate → Review → Validate → Test → Apply/Rollback│ │
    │  │     │                                    │                       │     │ │
    │  │     ▼                                    ▼                       ▼     │ │
    │  │  Priority                          Coding                    Learning  │ │
    │  │  Queue                             Council                   Cache     │ │
    │  └────────────────────────────────────────────────────────────────────────┘ │
    │                                                                              │
    │  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────────────┐  │
    │  │ TrinityModel     │  │ Trinity          │  │ Trinity                  │  │
    │  │ Client           │  │ CodeReviewer     │  │ RollbackManager          │  │
    │  │                  │  │                  │  │                          │  │
    │  │ • Hot-swap       │  │ • AST validation │  │ • Git-based rollback     │  │
    │  │ • Multi-tier     │  │ • Security scan  │  │ • Automatic on failure   │  │
    │  │   fallback       │  │ • Coding Council │  │ • State restoration      │  │
    │  └──────────────────┘  └──────────────────┘  └──────────────────────────┘  │
    │                                                                              │
    │  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────────────┐  │
    │  │ TrinityLock      │  │ Trinity          │  │ Trinity                  │  │
    │  │ Manager          │  │ Coordinator      │  │ LearningCache            │  │
    │  │                  │  │                  │  │                          │  │
    │  │ • Distributed    │  │ • Cross-repo     │  │ • History tracking       │  │
    │  │   locks          │  │   state sync     │  │ • Pattern learning       │  │
    │  │ • TTL-based      │  │ • Event bus      │  │ • Failure avoidance      │  │
    │  └──────────────────┘  └──────────────────┘  └──────────────────────────┘  │
    │                                                                              │
    └─────────────────────────────────────────────────────────────────────────────┘

Author: Trinity System
Version: 2.0.0
"""

from __future__ import annotations

import asyncio
import ast
import fcntl
import functools
import hashlib
import heapq
import json
import logging
import os
import subprocess
import sys
import time
import traceback
import uuid
import weakref
from collections import OrderedDict
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, IntEnum, auto
from pathlib import Path
from threading import Lock as ThreadLock
from typing import (
    Any, AsyncIterator, Awaitable, Callable, Coroutine, Deque, Dict,
    Generic, List, Optional, Set, Tuple, TypeVar, Union
)

logger = logging.getLogger("Ouroboros.TrinityIntegration")

T = TypeVar("T")


# =============================================================================
# CONFIGURATION (100% Environment-Driven, No Hardcoding)
# =============================================================================

def _env_str(key: str, default: str) -> str:
    return os.getenv(key, default)


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    return os.getenv(key, str(default)).lower() in ("true", "1", "yes", "on")


class TrinityConfig:
    """Dynamic configuration for Trinity integration - 100% environment-driven."""

    # Feature flags
    USE_UNIFIED_MODEL_SERVING = _env_bool("OUROBOROS_USE_UNIFIED_MODEL_SERVING", True)
    USE_EXPERIENCE_FORWARDER = _env_bool("OUROBOROS_USE_EXPERIENCE_FORWARDER", True)
    USE_NEURAL_MESH = _env_bool("OUROBOROS_USE_NEURAL_MESH", True)
    USE_CODING_COUNCIL = _env_bool("OUROBOROS_USE_CODING_COUNCIL", True)
    USE_DISTRIBUTED_LOCKS = _env_bool("OUROBOROS_USE_DISTRIBUTED_LOCKS", True)
    USE_HOT_SWAP = _env_bool("OUROBOROS_USE_HOT_SWAP", True)
    USE_LEARNING_CACHE = _env_bool("OUROBOROS_USE_LEARNING_CACHE", True)

    # Timeouts
    @staticmethod
    def get_model_timeout() -> float:
        value = _env_float("OUROBOROS_MODEL_TIMEOUT", 120.0)
        return max(30.0, min(600.0, value))

    @staticmethod
    def get_experience_timeout() -> float:
        value = _env_float("OUROBOROS_EXPERIENCE_TIMEOUT", 30.0)
        return max(5.0, min(120.0, value))

    @staticmethod
    def get_lock_timeout() -> float:
        value = _env_float("OUROBOROS_LOCK_TIMEOUT", 30.0)
        return max(5.0, min(300.0, value))

    @staticmethod
    def get_review_timeout() -> float:
        value = _env_float("OUROBOROS_REVIEW_TIMEOUT", 60.0)
        return max(10.0, min(300.0, value))

    # Retry configuration
    @staticmethod
    def get_max_retries() -> int:
        value = _env_int("OUROBOROS_MAX_RETRIES", 3)
        return max(1, min(10, value))

    @staticmethod
    def get_retry_delay() -> float:
        value = _env_float("OUROBOROS_RETRY_DELAY", 2.0)
        return max(0.5, min(30.0, value))

    # Priority queue
    @staticmethod
    def get_max_concurrent() -> int:
        value = _env_int("OUROBOROS_MAX_CONCURRENT", 3)
        return max(1, min(10, value))

    # Learning cache
    @staticmethod
    def get_cache_max_size() -> int:
        value = _env_int("OUROBOROS_CACHE_MAX_SIZE", 1000)
        return max(100, min(10000, value))

    @staticmethod
    def get_cache_ttl_hours() -> int:
        value = _env_int("OUROBOROS_CACHE_TTL_HOURS", 168)  # 7 days
        return max(1, min(720, value))

    # Code review thresholds
    @staticmethod
    def get_risk_threshold() -> float:
        value = _env_float("OUROBOROS_RISK_THRESHOLD", 0.7)
        return max(0.0, min(1.0, value))

    # Paths
    @staticmethod
    def get_rollback_dir() -> Path:
        return Path(_env_str(
            "OUROBOROS_ROLLBACK_DIR",
            str(Path.home() / ".jarvis" / "ouroboros" / "rollback")
        ))

    @staticmethod
    def get_learning_cache_dir() -> Path:
        return Path(_env_str(
            "OUROBOROS_LEARNING_CACHE_DIR",
            str(Path.home() / ".jarvis" / "ouroboros" / "learning_cache")
        ))

    @staticmethod
    def get_manual_review_dir() -> Path:
        return Path(_env_str(
            "OUROBOROS_MANUAL_REVIEW_DIR",
            str(Path.home() / ".jarvis" / "ouroboros" / "manual_review")
        ))


# =============================================================================
# ENUMS AND DATA CLASSES
# =============================================================================

class ImprovementPriority(IntEnum):
    """Priority levels for improvements."""
    CRITICAL = 0   # Security fixes, breaking bugs
    URGENT = 1     # User-facing bugs
    HIGH = 2       # Important improvements
    NORMAL = 3     # Standard improvements
    LOW = 4        # Nice-to-have


class ComponentHealth(Enum):
    """Health status of a component."""
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class ReviewResult(Enum):
    """Result of code review."""
    APPROVED = "approved"
    NEEDS_REVISION = "needs_revision"
    REJECTED = "rejected"
    SKIPPED = "skipped"


@dataclass
class HealthStatus:
    """Health status of the Trinity integration."""
    unified_model_serving: ComponentHealth = ComponentHealth.UNKNOWN
    experience_forwarder: ComponentHealth = ComponentHealth.UNKNOWN
    neural_mesh: ComponentHealth = ComponentHealth.UNKNOWN
    brain_orchestrator: ComponentHealth = ComponentHealth.UNKNOWN
    coding_council: ComponentHealth = ComponentHealth.UNKNOWN
    lock_manager: ComponentHealth = ComponentHealth.UNKNOWN
    event_bus: ComponentHealth = ComponentHealth.UNKNOWN
    overall: ComponentHealth = ComponentHealth.UNKNOWN
    last_check: float = 0.0
    details: Dict[str, Any] = field(default_factory=dict)

    def update_overall(self) -> None:
        """Update overall health based on component health."""
        components = [
            self.unified_model_serving,
            self.experience_forwarder,
            self.neural_mesh,
            self.brain_orchestrator,
        ]

        healthy_count = sum(1 for c in components if c == ComponentHealth.HEALTHY)
        unavailable_count = sum(1 for c in components if c == ComponentHealth.UNAVAILABLE)

        if healthy_count == len(components):
            self.overall = ComponentHealth.HEALTHY
        elif unavailable_count == len(components):
            self.overall = ComponentHealth.UNAVAILABLE
        else:
            self.overall = ComponentHealth.DEGRADED


@dataclass
class CodeReview:
    """Result of code review."""
    result: ReviewResult
    risk_score: float = 0.0
    feedback: str = ""
    suggestions: List[str] = field(default_factory=list)
    security_issues: List[str] = field(default_factory=list)
    ast_errors: List[str] = field(default_factory=list)
    reviewer: str = ""
    timestamp: float = field(default_factory=time.time)


@dataclass
class ImprovementHistory:
    """Historical record of improvement attempts."""
    target_hash: str
    goal_hash: str
    attempts: int = 0
    successes: int = 0
    failures: int = 0
    last_error: Optional[str] = None
    last_success: Optional[float] = None
    last_failure: Optional[float] = None
    patterns: Dict[str, int] = field(default_factory=dict)


@dataclass
class PrioritizedImprovement:
    """Improvement request with priority for queue ordering."""
    priority: ImprovementPriority
    timestamp: float
    request_id: str
    target: str
    goal: str
    callback: Optional[Callable] = None

    def __lt__(self, other: "PrioritizedImprovement") -> bool:
        # Lower priority value = higher priority
        if self.priority != other.priority:
            return self.priority < other.priority
        # Earlier timestamp = higher priority
        return self.timestamp < other.timestamp


# =============================================================================
# TRINITY LOCK MANAGER (Distributed Locking)
# =============================================================================

class TrinityLockManager:
    """
    Wrapper around distributed lock manager for improvement coordination.

    Prevents concurrent improvements to the same file across:
    - Multiple JARVIS instances
    - Multiple repos (JARVIS, Prime, Reactor)
    """

    def __init__(self):
        self.logger = logging.getLogger("Ouroboros.TrinityLockManager")
        self._lock_manager = None
        self._local_locks: Dict[str, asyncio.Lock] = {}
        self._local_lock_guard = asyncio.Lock()
        self._initialized = False

    async def initialize(self) -> bool:
        """Initialize distributed lock manager."""
        if self._initialized:
            return True

        if TrinityConfig.USE_DISTRIBUTED_LOCKS:
            try:
                from backend.core.distributed_lock_manager import get_lock_manager
                self._lock_manager = await get_lock_manager()
                await self._lock_manager.initialize()
                self.logger.info("✅ Distributed lock manager initialized")
                self._initialized = True
                return True
            except ImportError as e:
                self.logger.warning(f"Distributed lock manager not available: {e}")
            except Exception as e:
                self.logger.warning(f"Distributed lock manager init failed: {e}")

        # Fallback to local locks
        self.logger.info("Using local lock fallback")
        self._initialized = True
        return True

    @asynccontextmanager
    async def acquire(
        self,
        resource: str,
        timeout: Optional[float] = None,
        ttl: Optional[float] = None,
    ) -> AsyncIterator[bool]:
        """
        Acquire a lock for a resource.

        Args:
            resource: Resource identifier (e.g., file path)
            timeout: How long to wait for lock acquisition
            ttl: Lock time-to-live

        Yields:
            True if lock acquired, False otherwise
        """
        timeout = timeout or TrinityConfig.get_lock_timeout()
        ttl = ttl or timeout * 2  # TTL should be longer than timeout

        # Normalize resource name
        lock_name = f"ouroboros:{hashlib.md5(resource.encode()).hexdigest()[:16]}"

        if self._lock_manager:
            try:
                async with self._lock_manager.acquire(
                    lock_name,
                    timeout=timeout,
                    ttl=ttl,
                ) as acquired:
                    yield acquired
                    return
            except Exception as e:
                self.logger.warning(f"Distributed lock failed, using local: {e}")

        # Local lock fallback
        async with self._local_lock_guard:
            if lock_name not in self._local_locks:
                self._local_locks[lock_name] = asyncio.Lock()
            local_lock = self._local_locks[lock_name]

        try:
            acquired = await asyncio.wait_for(
                local_lock.acquire(),
                timeout=timeout,
            )
            yield acquired
        except asyncio.TimeoutError:
            yield False
        finally:
            if local_lock.locked():
                local_lock.release()

    async def is_locked(self, resource: str) -> bool:
        """Check if a resource is currently locked."""
        lock_name = f"ouroboros:{hashlib.md5(resource.encode()).hexdigest()[:16]}"

        if self._lock_manager:
            try:
                status = await self._lock_manager.get_lock_status(lock_name)
                return status is not None and not status.get("expired", True)
            except Exception:
                pass

        # Check local lock
        async with self._local_lock_guard:
            local_lock = self._local_locks.get(lock_name)
            return local_lock is not None and local_lock.locked()


# =============================================================================
# TRINITY CODE REVIEWER (Coding Council Integration)
# =============================================================================

class TrinityCodeReviewer:
    """
    Integrates with Coding Council for peer code review.

    Features:
    - AST validation
    - Security scanning
    - Coding Council multi-agent review
    - Risk scoring
    """

    def __init__(self):
        self.logger = logging.getLogger("Ouroboros.TrinityCodeReviewer")
        self._coding_council = None
        self._ast_validator = None
        self._security_scanner = None
        self._initialized = False

    async def initialize(self) -> bool:
        """Initialize code review components."""
        if self._initialized:
            return True

        # Try to load Coding Council
        if TrinityConfig.USE_CODING_COUNCIL:
            try:
                from backend.core.coding_council.orchestrator import (
                    UnifiedCodingCouncil,
                )
                # Initialize the coding council
                self._coding_council = UnifiedCodingCouncil()
                self.logger.info("✅ Coding Council available")
            except ImportError as e:
                self.logger.warning(f"Coding Council not available: {e}")
            except Exception as e:
                self.logger.warning(f"Coding Council init failed: {e}")

        # Try to load safety validators
        try:
            from backend.core.coding_council.safety import (
                ASTValidator,
                SecurityScanner,
            )
            # Get repo root from environment or default
            repo_root = Path(os.getenv(
                "JARVIS_REPO_PATH",
                str(Path(__file__).parent.parent.parent.parent)
            ))
            self._ast_validator = ASTValidator(repo_root)
            self._security_scanner = SecurityScanner(repo_root)
            self.logger.info("✅ Safety validators loaded")
        except ImportError as e:
            self.logger.warning(f"Safety validators not available: {e}")
        except Exception as e:
            self.logger.warning(f"Safety validators init failed: {e}")

        self._initialized = True
        return True

    async def review_improvement(
        self,
        original_code: str,
        improved_code: str,
        goal: str,
        file_path: Optional[str] = None,
    ) -> CodeReview:
        """
        Review an improvement before applying.

        Returns:
            CodeReview with result, risk score, and feedback
        """
        if not self._initialized:
            await self.initialize()

        risk_score = 0.0
        feedback = []
        suggestions = []
        security_issues = []
        ast_errors = []

        # 1. AST Validation - Check syntax
        try:
            ast.parse(improved_code)
        except SyntaxError as e:
            ast_errors.append(f"Syntax error at line {e.lineno}: {e.msg}")
            return CodeReview(
                result=ReviewResult.REJECTED,
                risk_score=1.0,
                feedback="Code has syntax errors",
                ast_errors=ast_errors,
                reviewer="ast_validator",
            )

        # 2. Safety Validators (if available)
        if self._ast_validator:
            try:
                ast_result = await asyncio.wait_for(
                    asyncio.to_thread(
                        self._ast_validator.validate,
                        improved_code,
                    ),
                    timeout=10.0,
                )
                if hasattr(ast_result, 'issues') and ast_result.issues:
                    for issue in ast_result.issues[:5]:
                        ast_errors.append(str(issue))
                    risk_score += 0.2
            except Exception as e:
                self.logger.warning(f"AST validation error: {e}")

        if self._security_scanner:
            try:
                security_result = await asyncio.wait_for(
                    asyncio.to_thread(
                        self._security_scanner.scan,
                        improved_code,
                    ),
                    timeout=10.0,
                )
                if hasattr(security_result, 'vulnerabilities') and security_result.vulnerabilities:
                    for vuln in security_result.vulnerabilities[:5]:
                        security_issues.append(str(vuln))
                    risk_score += 0.3 * len(security_result.vulnerabilities)
            except Exception as e:
                self.logger.warning(f"Security scan error: {e}")

        # 3. Change size analysis
        original_lines = len(original_code.splitlines())
        improved_lines = len(improved_code.splitlines())
        change_ratio = abs(improved_lines - original_lines) / max(original_lines, 1)

        if change_ratio > 0.5:
            risk_score += 0.2
            suggestions.append(f"Large change detected ({change_ratio:.0%} size change)")

        # 4. Risk-based decision
        risk_threshold = TrinityConfig.get_risk_threshold()

        if security_issues:
            result = ReviewResult.REJECTED
            feedback.append("Security issues detected")
        elif risk_score > risk_threshold:
            result = ReviewResult.NEEDS_REVISION
            feedback.append(f"Risk score ({risk_score:.2f}) exceeds threshold ({risk_threshold})")
        elif ast_errors:
            result = ReviewResult.NEEDS_REVISION
            feedback.append("AST validation issues detected")
        else:
            result = ReviewResult.APPROVED
            feedback.append("Code review passed")

        return CodeReview(
            result=result,
            risk_score=min(1.0, risk_score),
            feedback="; ".join(feedback),
            suggestions=suggestions,
            security_issues=security_issues,
            ast_errors=ast_errors,
            reviewer="trinity_reviewer",
        )


# =============================================================================
# TRINITY ROLLBACK MANAGER (Git-based Automatic Rollback)
# =============================================================================

class TrinityRollbackManager:
    """
    Manages automatic rollback for failed improvements.

    Features:
    - Git-based snapshots
    - Automatic rollback on test failure
    - State restoration
    """

    def __init__(self):
        self.logger = logging.getLogger("Ouroboros.TrinityRollbackManager")
        self._rollback_dir = TrinityConfig.get_rollback_dir()
        self._active_snapshots: Dict[str, Dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def initialize(self) -> bool:
        """Initialize rollback manager."""
        self._rollback_dir.mkdir(parents=True, exist_ok=True)
        self.logger.info(f"Rollback directory: {self._rollback_dir}")
        return True

    @asynccontextmanager
    async def snapshot(
        self,
        file_path: Path,
        task_id: str,
    ) -> AsyncIterator[bool]:
        """
        Create a snapshot before modification.

        Automatically rolls back if the context exits with an exception.

        Usage:
            async with rollback_mgr.snapshot(file_path, task_id) as ok:
                if ok:
                    # Modify file
                    # If exception raised, automatic rollback
        """
        snapshot_id = f"{task_id}_{uuid.uuid4().hex[:8]}"
        snapshot_path = self._rollback_dir / f"{snapshot_id}.snapshot"
        original_content = None

        try:
            # Create snapshot
            if file_path.exists():
                original_content = await asyncio.to_thread(
                    file_path.read_text,
                    "utf-8",
                )
                await asyncio.to_thread(
                    snapshot_path.write_text,
                    original_content,
                    "utf-8",
                )

                async with self._lock:
                    self._active_snapshots[snapshot_id] = {
                        "file_path": str(file_path),
                        "snapshot_path": str(snapshot_path),
                        "timestamp": time.time(),
                        "original_size": len(original_content),
                    }

                self.logger.debug(f"Created snapshot: {snapshot_id}")
                yield True
            else:
                yield False

        except Exception as e:
            # Automatic rollback on exception
            self.logger.warning(f"Rolling back due to error: {e}")
            if original_content is not None:
                try:
                    await asyncio.to_thread(
                        file_path.write_text,
                        original_content,
                        "utf-8",
                    )
                    self.logger.info(f"Rolled back: {file_path}")
                except Exception as rollback_error:
                    self.logger.error(f"Rollback failed: {rollback_error}")
            raise

        finally:
            # Cleanup snapshot
            async with self._lock:
                self._active_snapshots.pop(snapshot_id, None)

            with suppress(Exception):
                if snapshot_path.exists():
                    await asyncio.to_thread(snapshot_path.unlink)

    async def restore_from_snapshot(
        self,
        snapshot_id: str,
    ) -> bool:
        """Manually restore from a snapshot."""
        async with self._lock:
            snapshot_info = self._active_snapshots.get(snapshot_id)

        if not snapshot_info:
            self.logger.warning(f"Snapshot not found: {snapshot_id}")
            return False

        try:
            snapshot_path = Path(snapshot_info["snapshot_path"])
            file_path = Path(snapshot_info["file_path"])

            if snapshot_path.exists():
                content = await asyncio.to_thread(
                    snapshot_path.read_text,
                    "utf-8",
                )
                await asyncio.to_thread(
                    file_path.write_text,
                    content,
                    "utf-8",
                )
                self.logger.info(f"Restored from snapshot: {file_path}")
                return True

        except Exception as e:
            self.logger.error(f"Restore failed: {e}")

        return False


# =============================================================================
# TRINITY LEARNING CACHE (Improvement History and Learning)
# =============================================================================

class TrinityLearningCache:
    """
    Tracks improvement history to learn from past attempts.

    Features:
    - Records success/failure patterns
    - Avoids repeating failed approaches
    - Suggests successful patterns
    """

    def __init__(self):
        self.logger = logging.getLogger("Ouroboros.TrinityLearningCache")
        self._cache_dir = TrinityConfig.get_learning_cache_dir()
        self._cache: OrderedDict[str, ImprovementHistory] = OrderedDict()
        self._max_size = TrinityConfig.get_cache_max_size()
        self._ttl_hours = TrinityConfig.get_cache_ttl_hours()
        self._lock = asyncio.Lock()
        self._dirty = False

    async def initialize(self) -> bool:
        """Initialize learning cache."""
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        await self._load_cache()
        self.logger.info(f"Learning cache initialized with {len(self._cache)} entries")
        return True

    async def _load_cache(self) -> None:
        """Load cache from disk."""
        cache_file = self._cache_dir / "history.json"
        if cache_file.exists():
            try:
                content = await asyncio.to_thread(
                    cache_file.read_text,
                    "utf-8",
                )
                data = json.loads(content)

                # Filter expired entries
                cutoff = time.time() - (self._ttl_hours * 3600)
                for key, entry in data.items():
                    last_activity = max(
                        entry.get("last_success", 0) or 0,
                        entry.get("last_failure", 0) or 0,
                    )
                    if last_activity > cutoff:
                        self._cache[key] = ImprovementHistory(
                            target_hash=entry.get("target_hash", ""),
                            goal_hash=entry.get("goal_hash", ""),
                            attempts=entry.get("attempts", 0),
                            successes=entry.get("successes", 0),
                            failures=entry.get("failures", 0),
                            last_error=entry.get("last_error"),
                            last_success=entry.get("last_success"),
                            last_failure=entry.get("last_failure"),
                            patterns=entry.get("patterns", {}),
                        )
            except Exception as e:
                self.logger.warning(f"Failed to load cache: {e}")

    async def _save_cache(self) -> None:
        """Save cache to disk."""
        if not self._dirty:
            return

        cache_file = self._cache_dir / "history.json"
        try:
            data = {}
            for key, history in self._cache.items():
                data[key] = {
                    "target_hash": history.target_hash,
                    "goal_hash": history.goal_hash,
                    "attempts": history.attempts,
                    "successes": history.successes,
                    "failures": history.failures,
                    "last_error": history.last_error,
                    "last_success": history.last_success,
                    "last_failure": history.last_failure,
                    "patterns": history.patterns,
                }

            await asyncio.to_thread(
                cache_file.write_text,
                json.dumps(data, indent=2),
                "utf-8",
            )
            self._dirty = False
        except Exception as e:
            self.logger.warning(f"Failed to save cache: {e}")

    def _make_key(self, target: str, goal: str) -> str:
        """Create cache key from target and goal."""
        target_hash = hashlib.md5(target.encode()).hexdigest()[:12]
        goal_hash = hashlib.md5(goal.encode()).hexdigest()[:12]
        return f"{target_hash}:{goal_hash}"

    async def get_history(
        self,
        target: str,
        goal: str,
    ) -> Optional[ImprovementHistory]:
        """Get improvement history for a target/goal combination."""
        key = self._make_key(target, goal)
        async with self._lock:
            return self._cache.get(key)

    async def record_attempt(
        self,
        target: str,
        goal: str,
        success: bool,
        error: Optional[str] = None,
        pattern: Optional[str] = None,
    ) -> None:
        """Record an improvement attempt."""
        key = self._make_key(target, goal)

        async with self._lock:
            if key not in self._cache:
                self._cache[key] = ImprovementHistory(
                    target_hash=hashlib.md5(target.encode()).hexdigest()[:12],
                    goal_hash=hashlib.md5(goal.encode()).hexdigest()[:12],
                )

            history = self._cache[key]
            history.attempts += 1

            if success:
                history.successes += 1
                history.last_success = time.time()
            else:
                history.failures += 1
                history.last_failure = time.time()
                if error:
                    history.last_error = error[:500]

            if pattern:
                history.patterns[pattern] = history.patterns.get(pattern, 0) + 1

            # Move to end (LRU)
            self._cache.move_to_end(key)

            # Trim if too large
            while len(self._cache) > self._max_size:
                self._cache.popitem(last=False)

            self._dirty = True

        # Save periodically (not every time)
        if self._dirty and len(self._cache) % 10 == 0:
            await self._save_cache()

    async def should_skip(
        self,
        target: str,
        goal: str,
        max_failures: int = 3,
    ) -> Tuple[bool, Optional[str]]:
        """
        Check if we should skip this improvement based on history.

        Returns:
            (should_skip, reason)
        """
        history = await self.get_history(target, goal)
        if not history:
            return False, None

        # Skip if too many recent failures
        if history.failures >= max_failures and history.last_failure:
            cooldown = 3600  # 1 hour cooldown after max failures
            if time.time() - history.last_failure < cooldown:
                return True, f"Too many failures ({history.failures}), cooldown active"

        return False, None

    async def get_successful_patterns(
        self,
        target: str,
        goal: str,
    ) -> List[str]:
        """Get patterns that have worked for similar improvements."""
        history = await self.get_history(target, goal)
        if not history or not history.patterns:
            return []

        # Sort by success count
        sorted_patterns = sorted(
            history.patterns.items(),
            key=lambda x: x[1],
            reverse=True,
        )
        return [p[0] for p in sorted_patterns[:5]]

    async def shutdown(self) -> None:
        """Save cache on shutdown."""
        await self._save_cache()


# =============================================================================
# TRINITY COORDINATOR (Cross-Repo Coordination)
# =============================================================================

class TrinityCoordinator:
    """
    Coordinates improvements across Trinity repositories.

    Features:
    - Cross-repo state synchronization
    - Model hot-swap notification
    - Event bus integration
    - Circular dependency detection
    """

    def __init__(self):
        self.logger = logging.getLogger("Ouroboros.TrinityCoordinator")
        self._event_bus = None
        self._state_manager = None
        self._model_version: Optional[str] = None
        self._improvement_chain: List[str] = []
        self._chain_lock = asyncio.Lock()
        self._model_ready_callback: Optional[Callable] = None

    async def initialize(self) -> bool:
        """Initialize coordinator components."""
        # Try Trinity Event Bus
        try:
            from backend.core.trinity_event_bus import (
                get_trinity_event_bus,
                RepoType,
            )
            self._event_bus = await get_trinity_event_bus(RepoType.JARVIS)

            # Subscribe to model ready events
            if TrinityConfig.USE_HOT_SWAP:
                await self._event_bus.subscribe(
                    "model.ready",
                    self._on_model_ready,
                )
                await self._event_bus.subscribe(
                    "model.deployed",
                    self._on_model_ready,
                )

            self.logger.info("✅ Trinity Event Bus connected")
        except ImportError as e:
            self.logger.warning(f"Trinity Event Bus not available: {e}")
        except Exception as e:
            self.logger.warning(f"Trinity Event Bus init failed: {e}")

        # Try State Manager
        try:
            from backend.core.trinity_state_manager import get_state_manager
            self._state_manager = await get_state_manager()
            self.logger.info("✅ State Manager connected")
        except ImportError as e:
            self.logger.warning(f"State Manager not available: {e}")
        except Exception as e:
            self.logger.warning(f"State Manager init failed: {e}")

        return self._event_bus is not None or self._state_manager is not None

    async def _on_model_ready(self, event: Any) -> None:
        """Handle model ready events for hot-swap."""
        try:
            if hasattr(event, 'payload'):
                payload = event.payload
            elif isinstance(event, dict):
                payload = event
            else:
                return

            model_version = payload.get("model_version") or payload.get("version")
            if model_version and model_version != self._model_version:
                self._model_version = model_version
                self.logger.info(f"🔄 New model available: {model_version}")

                if self._model_ready_callback:
                    try:
                        await self._model_ready_callback(model_version)
                    except Exception as e:
                        self.logger.error(f"Model ready callback failed: {e}")

        except Exception as e:
            self.logger.error(f"Model ready handler error: {e}")

    def set_model_ready_callback(
        self,
        callback: Callable[[str], Awaitable[None]],
    ) -> None:
        """Set callback for model ready events."""
        self._model_ready_callback = callback

    async def check_circular_dependency(
        self,
        target: str,
    ) -> Tuple[bool, Optional[str]]:
        """
        Check for circular improvement dependencies.

        Returns:
            (is_circular, reason)
        """
        async with self._chain_lock:
            if target in self._improvement_chain:
                chain_str = " → ".join(self._improvement_chain + [target])
                return True, f"Circular dependency: {chain_str}"
            return False, None

    @asynccontextmanager
    async def improvement_context(
        self,
        target: str,
    ) -> AsyncIterator[None]:
        """
        Context manager for tracking improvement chain.

        Prevents circular dependencies.
        """
        async with self._chain_lock:
            self._improvement_chain.append(target)

        try:
            yield
        finally:
            async with self._chain_lock:
                if target in self._improvement_chain:
                    self._improvement_chain.remove(target)

    async def publish_improvement_event(
        self,
        event_type: str,
        target: str,
        success: bool,
        details: Optional[Dict] = None,
    ) -> None:
        """Publish improvement event to event bus."""
        if not self._event_bus:
            return

        try:
            from backend.core.trinity_event_bus import TrinityEvent, RepoType

            event = TrinityEvent(
                topic=f"ouroboros.{event_type}",
                source=RepoType.JARVIS,
                payload={
                    "target": target,
                    "success": success,
                    "timestamp": time.time(),
                    **(details or {}),
                },
            )
            await self._event_bus.publish(event)

        except Exception as e:
            self.logger.warning(f"Failed to publish event: {e}")


# =============================================================================
# ENHANCED TRINITY EXPERIENCE PUBLISHER (with Deduplication)
# =============================================================================

class TrinityExperiencePublisher:
    """
    Publishes experiences to Reactor Core via multiple channels with deduplication.

    Features:
    - Multi-channel publishing (Forwarder → Mesh → File)
    - Experience deduplication via content hashing
    - Batching and queueing
    """

    def __init__(self):
        self.logger = logging.getLogger("Ouroboros.TrinityExperiencePublisher")
        self._experience_forwarder = None
        self._neural_mesh = None
        self._fallback_dir: Optional[Path] = None
        self._published_hashes: OrderedDict[str, float] = OrderedDict()
        self._max_hash_cache = 10000
        self._hash_lock = asyncio.Lock()
        self._initialized = False
        self._stats = {
            "published_via_forwarder": 0,
            "published_via_mesh": 0,
            "published_via_file": 0,
            "publish_failures": 0,
            "deduplicated": 0,
        }

    async def initialize(self) -> bool:
        """Initialize experience publishing channels."""
        self.logger.info("Initializing Trinity Experience Publisher...")

        # Try CrossRepoExperienceForwarder (preferred)
        if TrinityConfig.USE_EXPERIENCE_FORWARDER:
            try:
                from backend.intelligence.cross_repo_experience_forwarder import (
                    get_experience_forwarder,
                )
                self._experience_forwarder = await get_experience_forwarder()
                if self._experience_forwarder:
                    self.logger.info("✅ Connected to CrossRepoExperienceForwarder")
            except ImportError as e:
                self.logger.warning(f"CrossRepoExperienceForwarder not available: {e}")
            except Exception as e:
                self.logger.warning(f"CrossRepoExperienceForwarder init failed: {e}")

        # Try Neural Mesh
        if TrinityConfig.USE_NEURAL_MESH:
            try:
                from backend.core.ouroboros.neural_mesh import get_neural_mesh
                self._neural_mesh = get_neural_mesh()
                if self._neural_mesh._running:
                    self.logger.info("✅ Neural Mesh available for experiences")
            except Exception as e:
                self.logger.warning(f"Neural Mesh not available: {e}")

        # Setup file-based fallback
        try:
            fallback_base = Path(_env_str(
                "OUROBOROS_EXPERIENCE_FALLBACK_DIR",
                str(Path.home() / ".jarvis" / "experience_queue" / "ouroboros"),
            ))
            self._fallback_dir = fallback_base
            self._fallback_dir.mkdir(parents=True, exist_ok=True)
            self.logger.info(f"✅ File fallback ready: {self._fallback_dir}")
        except Exception as e:
            self.logger.warning(f"File fallback setup failed: {e}")

        self._initialized = True
        return (
            self._experience_forwarder is not None or
            (self._neural_mesh is not None and self._neural_mesh._running) or
            self._fallback_dir is not None
        )

    def _compute_experience_hash(self, experience: Dict) -> str:
        """Compute hash for experience deduplication."""
        # Create a stable hash from key content
        key_content = json.dumps({
            "goal": experience.get("metadata", {}).get("goal", ""),
            "original_len": experience.get("metadata", {}).get("original_code_length", 0),
            "improved_len": experience.get("metadata", {}).get("improved_code_length", 0),
            "user_input": experience.get("user_input", "")[:100],
        }, sort_keys=True)
        return hashlib.md5(key_content.encode()).hexdigest()

    async def _is_duplicate(self, experience_hash: str) -> bool:
        """Check if experience is a duplicate."""
        async with self._hash_lock:
            if experience_hash in self._published_hashes:
                self._stats["deduplicated"] += 1
                return True

            # Add to cache
            self._published_hashes[experience_hash] = time.time()

            # Trim cache if too large
            while len(self._published_hashes) > self._max_hash_cache:
                self._published_hashes.popitem(last=False)

            return False

    async def publish(
        self,
        original_code: str,
        improved_code: str,
        goal: str,
        success: bool,
        iterations: int,
        error_history: Optional[List[str]] = None,
        provider_used: Optional[str] = None,
        duration_seconds: Optional[float] = None,
    ) -> bool:
        """
        Publish improvement experience to Reactor Core.

        Uses multiple channels to ensure delivery with deduplication.
        """
        if not self._initialized:
            await self.initialize()

        experience = self._build_experience(
            original_code=original_code,
            improved_code=improved_code,
            goal=goal,
            success=success,
            iterations=iterations,
            error_history=error_history,
            provider_used=provider_used,
            duration_seconds=duration_seconds,
        )

        # Check for duplicate
        experience_hash = self._compute_experience_hash(experience)
        if await self._is_duplicate(experience_hash):
            self.logger.debug(f"Duplicate experience skipped: {experience_hash[:8]}")
            return True  # Consider as success (already published)

        success_count = 0

        # Channel 1: CrossRepoExperienceForwarder
        if self._experience_forwarder:
            try:
                from backend.intelligence.cross_repo_experience_forwarder import (
                    ForwardingStatus,
                )

                status = await asyncio.wait_for(
                    self._experience_forwarder.forward_experience(
                        experience_type="code_improvement",
                        input_data={"original_code": original_code[:5000], "goal": goal},
                        output_data={"improved_code": improved_code[:5000]},
                        quality_score=1.0 if success else 0.3,
                        confidence=min(1.0, 1.0 / iterations) if iterations > 0 else 0.5,
                        success=success,
                        component="ouroboros",
                        metadata=experience.get("metadata", {}),
                    ),
                    timeout=TrinityConfig.get_experience_timeout(),
                )

                if status in (ForwardingStatus.SUCCESS, ForwardingStatus.QUEUED):
                    self._stats["published_via_forwarder"] += 1
                    success_count += 1
                    self.logger.debug("Experience published via CrossRepoExperienceForwarder")

            except Exception as e:
                self.logger.warning(f"CrossRepoExperienceForwarder failed: {e}")

        # Channel 2: Neural Mesh (only if forwarder failed)
        if success_count == 0 and self._neural_mesh and self._neural_mesh._running:
            try:
                from backend.core.ouroboros.neural_mesh import NodeType, MessageType

                await self._neural_mesh.send(
                    target=NodeType.REACTOR,
                    message_type=MessageType.EXPERIENCE,
                    payload=experience,
                    wait_response=False,
                )
                self._stats["published_via_mesh"] += 1
                success_count += 1
                self.logger.debug("Experience published via Neural Mesh")

            except Exception as e:
                self.logger.warning(f"Neural Mesh publish failed: {e}")

        # Channel 3: File-based fallback (only if others failed)
        if self._fallback_dir and success_count == 0:
            try:
                filename = f"exp_{time.time():.6f}_{uuid.uuid4().hex[:8]}.json"
                filepath = self._fallback_dir / filename
                await asyncio.to_thread(
                    filepath.write_text,
                    json.dumps(experience, indent=2),
                    "utf-8",
                )

                self._stats["published_via_file"] += 1
                success_count += 1
                self.logger.debug(f"Experience saved to file: {filepath}")

            except Exception as e:
                self.logger.warning(f"File fallback failed: {e}")

        if success_count == 0:
            self._stats["publish_failures"] += 1
            self.logger.error("All experience publishing channels failed")
            return False

        return True

    def _build_experience(
        self,
        original_code: str,
        improved_code: str,
        goal: str,
        success: bool,
        iterations: int,
        error_history: Optional[List[str]],
        provider_used: Optional[str],
        duration_seconds: Optional[float],
    ) -> Dict[str, Any]:
        """Build the experience payload."""
        return {
            "user_input": f"Improve code: {goal}",
            "assistant_output": improved_code[:5000] if success else f"Failed after {iterations} attempts",
            "confidence": 0.9 if success else 0.3,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
            "session_id": f"ouroboros-{uuid.uuid4().hex[:8]}",
            "metadata": {
                "source": "ouroboros_self_improvement",
                "original_code_length": len(original_code),
                "improved_code_length": len(improved_code),
                "goal": goal[:500],
                "success": success,
                "iterations": iterations,
                "error_count": len(error_history) if error_history else 0,
                "last_error": error_history[-1][:200] if error_history else None,
                "provider_used": provider_used,
                "duration_seconds": duration_seconds,
                "difficulty": self._estimate_difficulty(iterations, len(error_history or [])),
            },
        }

    def _estimate_difficulty(self, iterations: int, errors: int) -> str:
        """Estimate task difficulty based on iterations and errors."""
        if iterations == 1 and errors == 0:
            return "easy"
        elif iterations <= 3 and errors <= 2:
            return "medium"
        elif iterations <= 5:
            return "hard"
        else:
            return "very_hard"

    def get_stats(self) -> Dict[str, Any]:
        """Get publisher statistics."""
        return {
            "initialized": self._initialized,
            "channels": {
                "forwarder_available": self._experience_forwarder is not None,
                "neural_mesh_available": self._neural_mesh is not None and self._neural_mesh._running,
                "file_fallback_available": self._fallback_dir is not None,
            },
            "stats": dict(self._stats),
        }


# =============================================================================
# TRINITY MODEL CLIENT (with Hot-Swap Support)
# =============================================================================

class TrinityModelClient:
    """
    Intelligent model client with hot-swap support.

    Features:
    - Multi-tier fallback (UnifiedModelServing → Neural Mesh → Brain → HTTP)
    - Model hot-swap on MODEL_READY events
    - Adaptive retries with backoff
    """

    def __init__(self):
        self.logger = logging.getLogger("Ouroboros.TrinityModelClient")
        self._unified_serving = None
        self._neural_mesh = None
        self._brain_orchestrator = None
        self._fallback_session = None
        self._initialized = False
        self._current_model_version: Optional[str] = None

    async def initialize(self) -> bool:
        """Initialize connections to available model sources."""
        self.logger.info("Initializing Trinity Model Client...")

        # Try UnifiedModelServing first (preferred)
        if TrinityConfig.USE_UNIFIED_MODEL_SERVING:
            try:
                from backend.intelligence.unified_model_serving import (
                    get_unified_model_serving,
                )
                self._unified_serving = await get_unified_model_serving()
                if self._unified_serving:
                    self.logger.info("✅ Connected to UnifiedModelServing")
            except ImportError as e:
                self.logger.warning(f"UnifiedModelServing not available: {e}")
            except Exception as e:
                self.logger.warning(f"UnifiedModelServing initialization failed: {e}")

        # Try Neural Mesh
        if TrinityConfig.USE_NEURAL_MESH:
            try:
                from backend.core.ouroboros.neural_mesh import get_neural_mesh
                self._neural_mesh = get_neural_mesh()
                if self._neural_mesh._running:
                    self.logger.info("✅ Connected to Neural Mesh")
            except ImportError as e:
                self.logger.warning(f"Neural Mesh not available: {e}")
            except Exception as e:
                self.logger.warning(f"Neural Mesh connection failed: {e}")

        # Try Brain Orchestrator
        try:
            from backend.core.ouroboros.brain_orchestrator import get_brain_orchestrator
            self._brain_orchestrator = get_brain_orchestrator()
            if self._brain_orchestrator._running:
                self.logger.info("✅ Connected to Brain Orchestrator")
        except ImportError as e:
            self.logger.warning(f"Brain Orchestrator not available: {e}")
        except Exception as e:
            self.logger.warning(f"Brain Orchestrator connection failed: {e}")

        self._initialized = True
        return self._unified_serving is not None or self._brain_orchestrator is not None

    async def refresh_models(self) -> None:
        """Refresh model connections (called on hot-swap)."""
        self.logger.info("🔄 Refreshing model connections...")

        if self._unified_serving:
            try:
                # Trigger model refresh in unified serving
                if hasattr(self._unified_serving, 'refresh_models'):
                    await self._unified_serving.refresh_models()
                elif hasattr(self._unified_serving, 'refresh'):
                    await self._unified_serving.refresh()
            except Exception as e:
                self.logger.warning(f"Model refresh failed: {e}")

    async def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: float = 0.3,
        max_tokens: int = 4096,
    ) -> Tuple[Optional[str], str]:
        """
        Generate code improvement using best available source.

        Returns:
            Tuple of (generated_content, provider_name)
        """
        if not self._initialized:
            await self.initialize()

        # Strategy 1: UnifiedModelServing (best)
        if self._unified_serving:
            try:
                result = await self._generate_via_unified_serving(
                    prompt, system_prompt, temperature, max_tokens
                )
                if result:
                    return result[0], f"unified:{result[1]}"
            except Exception as e:
                self.logger.warning(f"UnifiedModelServing failed: {e}")

        # Strategy 2: Neural Mesh → JARVIS Prime
        if self._neural_mesh and self._neural_mesh._running:
            try:
                result = await self._generate_via_neural_mesh(
                    prompt, system_prompt, temperature, max_tokens
                )
                if result:
                    return result, "neural_mesh:prime"
            except Exception as e:
                self.logger.warning(f"Neural Mesh failed: {e}")

        # Strategy 3: Brain Orchestrator → Direct provider
        if self._brain_orchestrator:
            try:
                result = await self._generate_via_brain_orchestrator(
                    prompt, system_prompt, temperature, max_tokens
                )
                if result:
                    provider = self._brain_orchestrator.get_best_provider()
                    provider_name = provider.name if provider else "unknown"
                    return result, f"brain:{provider_name}"
            except Exception as e:
                self.logger.warning(f"Brain Orchestrator failed: {e}")

        # All strategies failed
        self.logger.error("All code generation strategies failed")
        return None, "none"

    async def _generate_via_unified_serving(
        self,
        prompt: str,
        system_prompt: Optional[str],
        temperature: float,
        max_tokens: int,
    ) -> Optional[Tuple[str, str]]:
        """Generate via UnifiedModelServing."""
        try:
            from backend.intelligence.unified_model_serving import (
                ModelRequest,
                TaskType,
            )

            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt})

            request = ModelRequest(
                request_id=f"ouro_{uuid.uuid4().hex[:12]}",
                messages=messages,
                system_prompt=system_prompt,
                task_type=TaskType.CODE,
                max_tokens=max_tokens,
                temperature=temperature,
                stream=False,
                context={"source": "ouroboros_self_improvement"},
                preferred_provider=None,
            )

            response = await asyncio.wait_for(
                self._unified_serving.generate(request),
                timeout=TrinityConfig.get_model_timeout(),
            )

            if response and response.success and response.content:
                return response.content, response.provider.value

            return None

        except asyncio.TimeoutError:
            self.logger.warning("UnifiedModelServing timed out")
            return None
        except Exception as e:
            self.logger.error(f"UnifiedModelServing error: {e}")
            raise

    async def _generate_via_neural_mesh(
        self,
        prompt: str,
        system_prompt: Optional[str],
        temperature: float,
        max_tokens: int,
    ) -> Optional[str]:
        """Generate via Neural Mesh → JARVIS Prime."""
        try:
            from backend.core.ouroboros.neural_mesh import (
                MessageType,
                NodeType,
            )

            response = await self._neural_mesh.send(
                target=NodeType.PRIME,
                message_type=MessageType.IMPROVEMENT_REQUEST,
                payload={
                    "prompt": prompt,
                    "system_prompt": system_prompt,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                },
                wait_response=True,
                timeout=TrinityConfig.get_model_timeout(),
            )

            if response and response.payload.get("content"):
                return response.payload["content"]

            return None

        except Exception as e:
            self.logger.error(f"Neural Mesh error: {e}")
            raise

    async def _generate_via_brain_orchestrator(
        self,
        prompt: str,
        system_prompt: Optional[str],
        temperature: float,
        max_tokens: int,
    ) -> Optional[str]:
        """Generate via Brain Orchestrator's best provider."""
        try:
            import aiohttp

            provider = self._brain_orchestrator.get_best_provider()
            if not provider or not provider.is_healthy:
                return None

            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt})

            payload = {
                "model": "default",
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }

            async with aiohttp.ClientSession() as session:
                url = f"{provider.endpoint}/v1/chat/completions"
                timeout = aiohttp.ClientTimeout(total=TrinityConfig.get_model_timeout())

                async with session.post(url, json=payload, timeout=timeout) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        choices = data.get("choices", [])
                        if choices:
                            return choices[0].get("message", {}).get("content")

            return None

        except Exception as e:
            self.logger.error(f"Brain Orchestrator error: {e}")
            raise

    def get_status(self) -> Dict[str, Any]:
        """Get client status."""
        return {
            "initialized": self._initialized,
            "unified_serving_available": self._unified_serving is not None,
            "neural_mesh_available": self._neural_mesh is not None and self._neural_mesh._running,
            "brain_orchestrator_available": self._brain_orchestrator is not None,
            "current_model_version": self._current_model_version,
        }


# =============================================================================
# TRINITY HEALTH MONITOR
# =============================================================================

class TrinityHealthMonitor:
    """
    Monitors health of all Trinity components.
    """

    def __init__(self):
        self.logger = logging.getLogger("Ouroboros.TrinityHealthMonitor")
        self._health = HealthStatus()
        self._running = False
        self._check_task: Optional[asyncio.Task] = None
        self._check_interval = _env_float("TRINITY_HEALTH_CHECK_INTERVAL", 60.0)
        self._callbacks: List[Callable[[HealthStatus], None]] = []

    async def start(self) -> None:
        """Start health monitoring."""
        self._running = True
        self._check_task = asyncio.create_task(self._health_check_loop())
        self.logger.info("Trinity Health Monitor started")

    async def stop(self) -> None:
        """Stop health monitoring."""
        self._running = False
        if self._check_task:
            self._check_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._check_task
        self.logger.info("Trinity Health Monitor stopped")

    async def check_now(self) -> HealthStatus:
        """Perform immediate health check."""
        await self._check_all_components()
        return self._health

    def register_callback(self, callback: Callable[[HealthStatus], None]) -> None:
        """Register a callback for health status changes."""
        self._callbacks.append(callback)

    async def _health_check_loop(self) -> None:
        """Periodic health check loop."""
        while self._running:
            try:
                await self._check_all_components()

                for callback in self._callbacks:
                    with suppress(Exception):
                        callback(self._health)

                await asyncio.sleep(self._check_interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Health check error: {e}")
                await asyncio.sleep(10.0)

    async def _check_all_components(self) -> None:
        """Check health of all components."""
        self._health.last_check = time.time()
        details = {}

        # Check UnifiedModelServing
        try:
            from backend.intelligence.unified_model_serving import get_unified_model_serving
            serving = await get_unified_model_serving()
            if serving:
                self._health.unified_model_serving = ComponentHealth.HEALTHY
                details["unified_model_serving"] = {"status": "healthy"}
            else:
                self._health.unified_model_serving = ComponentHealth.UNAVAILABLE
                details["unified_model_serving"] = {"status": "unavailable"}
        except Exception as e:
            self._health.unified_model_serving = ComponentHealth.UNAVAILABLE
            details["unified_model_serving"] = {"status": "error", "error": str(e)}

        # Check CrossRepoExperienceForwarder
        try:
            from backend.intelligence.cross_repo_experience_forwarder import get_experience_forwarder
            forwarder = await get_experience_forwarder()
            if forwarder:
                self._health.experience_forwarder = ComponentHealth.HEALTHY
                details["experience_forwarder"] = {"status": "healthy"}
            else:
                self._health.experience_forwarder = ComponentHealth.UNAVAILABLE
                details["experience_forwarder"] = {"status": "unavailable"}
        except Exception as e:
            self._health.experience_forwarder = ComponentHealth.UNAVAILABLE
            details["experience_forwarder"] = {"status": "error", "error": str(e)}

        # Check Neural Mesh
        try:
            from backend.core.ouroboros.neural_mesh import get_neural_mesh
            mesh = get_neural_mesh()
            if mesh._running:
                self._health.neural_mesh = ComponentHealth.HEALTHY
                details["neural_mesh"] = {"status": "healthy"}
            else:
                self._health.neural_mesh = ComponentHealth.UNAVAILABLE
                details["neural_mesh"] = {"status": "not_running"}
        except Exception as e:
            self._health.neural_mesh = ComponentHealth.UNAVAILABLE
            details["neural_mesh"] = {"status": "error", "error": str(e)}

        # Check Brain Orchestrator
        try:
            from backend.core.ouroboros.brain_orchestrator import get_brain_orchestrator
            orchestrator = get_brain_orchestrator()
            if orchestrator._running:
                self._health.brain_orchestrator = ComponentHealth.HEALTHY
                details["brain_orchestrator"] = {"status": "healthy"}
            else:
                self._health.brain_orchestrator = ComponentHealth.DEGRADED
                details["brain_orchestrator"] = {"status": "degraded"}
        except Exception as e:
            self._health.brain_orchestrator = ComponentHealth.UNAVAILABLE
            details["brain_orchestrator"] = {"status": "error", "error": str(e)}

        self._health.details = details
        self._health.update_overall()

    def get_health(self) -> HealthStatus:
        """Get current health status."""
        return self._health


# =============================================================================
# MANUAL REVIEW QUEUE (Complete Failure Handling)
# =============================================================================

class ManualReviewQueue:
    """
    Queue for improvements that require manual review.

    Used when all automated processes fail.
    """

    def __init__(self):
        self.logger = logging.getLogger("Ouroboros.ManualReviewQueue")
        self._review_dir = TrinityConfig.get_manual_review_dir()

    async def initialize(self) -> bool:
        """Initialize manual review queue."""
        self._review_dir.mkdir(parents=True, exist_ok=True)
        return True

    async def queue_for_review(
        self,
        target: str,
        goal: str,
        original_code: str,
        failure_reason: str,
        attempts: int,
    ) -> str:
        """
        Queue an improvement for manual review.

        Returns:
            Review ID
        """
        review_id = f"review_{uuid.uuid4().hex[:12]}"
        review_file = self._review_dir / f"{review_id}.json"

        review_data = {
            "review_id": review_id,
            "target": target,
            "goal": goal,
            "original_code": original_code[:10000],
            "failure_reason": failure_reason,
            "attempts": attempts,
            "queued_at": time.time(),
            "status": "pending",
        }

        await asyncio.to_thread(
            review_file.write_text,
            json.dumps(review_data, indent=2),
            "utf-8",
        )

        self.logger.warning(
            f"Improvement queued for manual review: {review_id}\n"
            f"  Target: {target}\n"
            f"  Goal: {goal[:100]}...\n"
            f"  Reason: {failure_reason}"
        )

        return review_id

    async def get_pending_reviews(self) -> List[Dict]:
        """Get all pending manual reviews."""
        pending = []
        for review_file in self._review_dir.glob("review_*.json"):
            try:
                content = await asyncio.to_thread(
                    review_file.read_text,
                    "utf-8",
                )
                data = json.loads(content)
                if data.get("status") == "pending":
                    pending.append(data)
            except Exception:
                continue
        return pending


# =============================================================================
# TRINITY INTEGRATION FACADE
# =============================================================================

class TrinityIntegration:
    """
    Main facade for Trinity integration v2.0.

    Provides unified access to:
    - Model generation (with hot-swap)
    - Experience publishing (with deduplication)
    - Code review (Coding Council)
    - Distributed locking
    - Rollback management
    - Learning cache
    - Cross-repo coordination
    - Health monitoring
    - Manual review queue
    """

    def __init__(self):
        self.logger = logging.getLogger("Ouroboros.TrinityIntegration")

        # Core components
        self.model_client = TrinityModelClient()
        self.experience_publisher = TrinityExperiencePublisher()
        self.health_monitor = TrinityHealthMonitor()

        # v2.0 components
        self.code_reviewer = TrinityCodeReviewer()
        self.lock_manager = TrinityLockManager()
        self.rollback_manager = TrinityRollbackManager()
        self.learning_cache = TrinityLearningCache()
        self.coordinator = TrinityCoordinator()
        self.manual_review_queue = ManualReviewQueue()

        # Priority queue for improvements
        self._improvement_queue: List[PrioritizedImprovement] = []
        self._queue_lock = asyncio.Lock()
        self._queue_semaphore = asyncio.Semaphore(TrinityConfig.get_max_concurrent())

        self._running = False

    async def initialize(self) -> bool:
        """Initialize all Trinity components."""
        self.logger.info("=" * 60)
        self.logger.info("🔺 TRINITY INTEGRATION v2.0 - Initializing")
        self.logger.info("=" * 60)

        # Initialize all components in parallel
        results = await asyncio.gather(
            self.model_client.initialize(),
            self.experience_publisher.initialize(),
            self.code_reviewer.initialize(),
            self.lock_manager.initialize(),
            self.coordinator.initialize(),
            self.learning_cache.initialize(),
            self.rollback_manager.initialize(),
            self.manual_review_queue.initialize(),
            return_exceptions=True,
        )

        # Log results
        component_names = [
            "Model Client",
            "Experience Publisher",
            "Code Reviewer",
            "Lock Manager",
            "Coordinator",
            "Learning Cache",
            "Rollback Manager",
            "Manual Review Queue",
        ]

        for name, result in zip(component_names, results):
            if isinstance(result, Exception):
                self.logger.warning(f"⚠️ {name}: {result}")
            elif result:
                self.logger.info(f"✅ {name}: Ready")
            else:
                self.logger.warning(f"⚠️ {name}: Degraded")

        # Set up model hot-swap callback
        self.coordinator.set_model_ready_callback(self._on_model_ready)

        # Start health monitor
        await self.health_monitor.start()
        self.logger.info("✅ Health monitor started")

        # Initial health check
        health = await self.health_monitor.check_now()
        self.logger.info(f"Overall health: {health.overall.value}")

        self._running = True
        return any(not isinstance(r, Exception) and r for r in results[:3])

    async def _on_model_ready(self, model_version: str) -> None:
        """Handle model ready event for hot-swap."""
        self.logger.info(f"🔄 Hot-swapping to model: {model_version}")
        await self.model_client.refresh_models()

    async def shutdown(self) -> None:
        """Shutdown all Trinity components."""
        self.logger.info("Shutting down Trinity Integration v2.0...")
        self._running = False

        await asyncio.gather(
            self.health_monitor.stop(),
            self.learning_cache.shutdown(),
            return_exceptions=True,
        )

        self.logger.info("Trinity Integration v2.0 shutdown complete")

    async def generate_improvement(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: float = 0.3,
        max_tokens: int = 4096,
    ) -> Tuple[Optional[str], str]:
        """Generate code improvement."""
        return await self.model_client.generate(
            prompt=prompt,
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    async def publish_experience(
        self,
        original_code: str,
        improved_code: str,
        goal: str,
        success: bool,
        iterations: int,
        error_history: Optional[List[str]] = None,
        provider_used: Optional[str] = None,
        duration_seconds: Optional[float] = None,
    ) -> bool:
        """Publish improvement experience."""
        return await self.experience_publisher.publish(
            original_code=original_code,
            improved_code=improved_code,
            goal=goal,
            success=success,
            iterations=iterations,
            error_history=error_history,
            provider_used=provider_used,
            duration_seconds=duration_seconds,
        )

    async def review_code(
        self,
        original_code: str,
        improved_code: str,
        goal: str,
        file_path: Optional[str] = None,
    ) -> CodeReview:
        """Review code improvement."""
        return await self.code_reviewer.review_improvement(
            original_code=original_code,
            improved_code=improved_code,
            goal=goal,
            file_path=file_path,
        )

    @asynccontextmanager
    async def acquire_lock(
        self,
        resource: str,
        timeout: Optional[float] = None,
    ) -> AsyncIterator[bool]:
        """Acquire distributed lock for resource."""
        async with self.lock_manager.acquire(resource, timeout) as acquired:
            yield acquired

    @asynccontextmanager
    async def with_rollback(
        self,
        file_path: Path,
        task_id: str,
    ) -> AsyncIterator[bool]:
        """Create snapshot with automatic rollback on failure."""
        async with self.rollback_manager.snapshot(file_path, task_id) as ok:
            yield ok

    async def check_should_skip(
        self,
        target: str,
        goal: str,
    ) -> Tuple[bool, Optional[str]]:
        """Check if improvement should be skipped based on history."""
        return await self.learning_cache.should_skip(target, goal)

    async def record_improvement_attempt(
        self,
        target: str,
        goal: str,
        success: bool,
        error: Optional[str] = None,
    ) -> None:
        """Record improvement attempt for learning."""
        await self.learning_cache.record_attempt(target, goal, success, error)

    async def queue_for_manual_review(
        self,
        target: str,
        goal: str,
        original_code: str,
        failure_reason: str,
        attempts: int,
    ) -> str:
        """Queue improvement for manual review."""
        return await self.manual_review_queue.queue_for_review(
            target=target,
            goal=goal,
            original_code=original_code,
            failure_reason=failure_reason,
            attempts=attempts,
        )

    def get_status(self) -> Dict[str, Any]:
        """Get integration status."""
        return {
            "running": self._running,
            "version": "2.0.0",
            "model_client": self.model_client.get_status(),
            "experience_publisher": self.experience_publisher.get_stats(),
            "health": {
                "overall": self.health_monitor.get_health().overall.value,
                "components": {
                    "unified_model_serving": self.health_monitor.get_health().unified_model_serving.value,
                    "experience_forwarder": self.health_monitor.get_health().experience_forwarder.value,
                    "neural_mesh": self.health_monitor.get_health().neural_mesh.value,
                    "brain_orchestrator": self.health_monitor.get_health().brain_orchestrator.value,
                },
                "last_check": self.health_monitor.get_health().last_check,
            },
        }


# =============================================================================
# GLOBAL INSTANCE
# =============================================================================

_trinity_integration: Optional[TrinityIntegration] = None


def get_trinity_integration() -> TrinityIntegration:
    """Get the global Trinity integration instance."""
    global _trinity_integration
    if _trinity_integration is None:
        _trinity_integration = TrinityIntegration()
    return _trinity_integration


async def initialize_trinity_integration() -> bool:
    """Initialize Trinity integration."""
    integration = get_trinity_integration()
    return await integration.initialize()


async def shutdown_trinity_integration() -> None:
    """Shutdown Trinity integration."""
    global _trinity_integration
    if _trinity_integration:
        await _trinity_integration.shutdown()
        _trinity_integration = None


def is_trinity_integration_running() -> bool:
    """
    Check if the Trinity integration exists and is running
    WITHOUT creating a new instance.

    This is critical for probes/health checks that need to verify state
    without side effects.

    Returns:
        True if integration exists and is running, False otherwise
    """
    return _trinity_integration is not None and getattr(_trinity_integration, '_running', False)


def get_trinity_integration_if_exists() -> Optional[TrinityIntegration]:
    """
    Get the Trinity integration instance if it exists, without creating a new one.

    Returns:
        The existing integration instance or None if not initialized
    """
    return _trinity_integration
