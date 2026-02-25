"""
JARVIS Startup Transaction Coordinator v1.0
=============================================
Provides cleanup-on-abort semantics for the startup lifecycle.

Root cause cured:
  - 5 abort paths in _startup_impl() exit with `return 1` and zero cleanup
  - Background tasks, loading server, locks, env vars left behind on failure
  - JARVIS_STARTUP_COMPLETE set before verification completes (poisoned signal)
  - Warm restart inherits stale state from partial previous startup

This coordinator does NOT replace the startup flow. It is a side-channel
cleanup registry: phases register artifacts as they create them, commit
when done, and abort paths call abort_with_cleanup() to clean up
uncommitted artifacts in LIFO (reverse) order.

v272.0: Created as part of Phase 9 — transactional lifecycle gaps.
"""

import asyncio
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_CLEANUP_TIMEOUT_S = float(os.environ.get("JARVIS_TXN_CLEANUP_TIMEOUT_S", "5.0"))

# Env vars that must be cleared on any abort (poisoned signal prevention)
_ABORT_CLEAR_ENV_VARS = (
    "JARVIS_STARTUP_COMPLETE",
)


# ---------------------------------------------------------------------------
# Data Model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PhaseArtifact:
    """An artifact created during startup that needs cleanup on abort.

    Frozen so registration records cannot be mutated after creation.
    Cleanup functions are stored but NOT called until abort.
    """
    phase: str                   # "pre_phase", "loading_experience", "preflight", etc.
    artifact_type: str           # "process", "task", "env_var", "lock", "service", "signal"
    description: str             # Human-readable: "Loading server process"
    cleanup_fn: Callable[[], Union[None, Awaitable[None]]]
    registered_at: float = field(default_factory=time.time)


@dataclass
class PhaseCommitRecord:
    """Records that a phase completed successfully."""
    phase: str
    committed_at: float = field(default_factory=time.time)
    artifact_count: int = 0


# ---------------------------------------------------------------------------
# Core Class
# ---------------------------------------------------------------------------

class StartupTransaction:
    """Startup transaction coordinator — cleanup registry for abort paths.

    NOT a replacement for the startup flow. This is a side-channel registry
    that abort paths call to clean up artifacts from uncommitted phases.

    Thread-safe via threading.Lock (same pattern as decision_log.py).
    Cleanup functions can be sync or async.
    """

    _instance: Optional["StartupTransaction"] = None
    _instance_lock = threading.Lock()

    def __init__(self):
        self._artifacts: List[PhaseArtifact] = []
        self._committed: Dict[str, PhaseCommitRecord] = {}
        self._aborted: bool = False
        self._abort_reason: Optional[str] = None
        self._lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> "StartupTransaction":
        """Singleton accessor (thread-safe)."""
        if cls._instance is not None:
            return cls._instance
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def reset(self) -> None:
        """Reset for fresh startup. Called at top of _startup_impl."""
        with self._lock:
            self._artifacts.clear()
            self._committed.clear()
            self._aborted = False
            self._abort_reason = None

    # -----------------------------------------------------------------------
    # Registration API
    # -----------------------------------------------------------------------

    def register_phase_artifact(
        self,
        phase: str,
        artifact_type: str,
        description: str,
        cleanup_fn: Callable,
    ) -> int:
        """Register an artifact that needs cleanup on abort.

        Returns registration index (for diagnostics). Returns -1 if
        registration rejected (already aborted).
        """
        with self._lock:
            if self._aborted:
                logger.warning(
                    "[StartupTxn] Cannot register artifact after abort: %s",
                    description,
                )
                return -1
            artifact = PhaseArtifact(
                phase=phase,
                artifact_type=artifact_type,
                description=description,
                cleanup_fn=cleanup_fn,
            )
            self._artifacts.append(artifact)
            return len(self._artifacts) - 1

    def mark_phase_committed(self, phase: str) -> None:
        """Mark a phase as successfully committed."""
        with self._lock:
            count = sum(1 for a in self._artifacts if a.phase == phase)
            self._committed[phase] = PhaseCommitRecord(
                phase=phase,
                artifact_count=count,
            )
        logger.debug(
            "[StartupTxn] Phase '%s' committed (%d artifacts)", phase, count,
        )

    def is_committed(self, phase: str) -> bool:
        """Check if a phase was successfully committed."""
        with self._lock:
            return phase in self._committed

    # -----------------------------------------------------------------------
    # Abort API
    # -----------------------------------------------------------------------

    async def abort_with_cleanup(self, reason: str) -> Dict[str, Any]:
        """Clean up artifacts from uncommitted phases in LIFO order.

        Idempotent — second call returns immediately.
        Each cleanup call wrapped in try/except with configurable timeout.
        Always clears poisoned readiness signals regardless of phase state.
        """
        with self._lock:
            if self._aborted:
                return {"status": "already_aborted", "reason": self._abort_reason}
            self._aborted = True
            self._abort_reason = reason
            # Snapshot uncommitted artifacts in reverse (LIFO) order
            uncommitted = [
                a for a in reversed(self._artifacts)
                if a.phase not in self._committed
            ]

        logger.warning(
            "[StartupTxn] ABORT (%s) — cleaning %d uncommitted artifacts",
            reason, len(uncommitted),
        )

        results: Dict[str, Any] = {
            "status": "aborted",
            "reason": reason,
            "cleaned": 0,
            "failed": 0,
            "skipped_committed": 0,
            "errors": [],
        }

        for artifact in uncommitted:
            try:
                ret = artifact.cleanup_fn()
                if asyncio.iscoroutine(ret) or asyncio.isfuture(ret):
                    await asyncio.wait_for(ret, timeout=_CLEANUP_TIMEOUT_S)
                results["cleaned"] += 1
                logger.debug("[StartupTxn] Cleaned: %s", artifact.description)
            except asyncio.TimeoutError:
                results["failed"] += 1
                results["errors"].append(
                    f"{artifact.description}: timeout ({_CLEANUP_TIMEOUT_S}s)"
                )
                logger.warning(
                    "[StartupTxn] Cleanup timeout: %s", artifact.description,
                )
            except Exception as e:
                results["failed"] += 1
                results["errors"].append(f"{artifact.description}: {e}")
                logger.warning(
                    "[StartupTxn] Cleanup error: %s: %s", artifact.description, e,
                )

        # Always clear poisoned readiness signals
        for key in _ABORT_CLEAR_ENV_VARS:
            os.environ.pop(key, None)

        logger.info(
            "[StartupTxn] Abort cleanup complete: %d cleaned, %d failed",
            results["cleaned"], results["failed"],
        )
        return results

    # -----------------------------------------------------------------------
    # Query API
    # -----------------------------------------------------------------------

    def get_uncommitted_artifacts(self) -> List[PhaseArtifact]:
        """Get all artifacts from uncommitted phases."""
        with self._lock:
            return [a for a in self._artifacts if a.phase not in self._committed]

    @property
    def phase_count(self) -> int:
        """Number of committed phases."""
        with self._lock:
            return len(self._committed)

    @property
    def artifact_count(self) -> int:
        """Total number of registered artifacts."""
        with self._lock:
            return len(self._artifacts)

    @property
    def is_aborted(self) -> bool:
        """Whether abort has been triggered."""
        return self._aborted


# ---------------------------------------------------------------------------
# Module-level convenience functions
# ---------------------------------------------------------------------------

def get_startup_transaction() -> StartupTransaction:
    """Get the global StartupTransaction singleton."""
    return StartupTransaction.get_instance()
