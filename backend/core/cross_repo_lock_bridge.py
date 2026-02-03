"""
Cross-Repo Lock Bridge v1.0
============================

Provides a simple, unified API for acquiring distributed locks across
JARVIS, JARVIS-Prime, and Reactor-Core repositories.

This module can be imported by any of the three repos to participate in
the unified cross-repo locking system.

Usage (from any repo):
    # Option 1: Import directly (if JARVIS is in PYTHONPATH)
    from backend.core.cross_repo_lock_bridge import (
        acquire_trinity_lock,
        TrinityLockManager,
    )

    # Option 2: Add JARVIS repo to path first
    import sys
    sys.path.insert(0, "/path/to/JARVIS-AI-Agent")
    from backend.core.cross_repo_lock_bridge import acquire_trinity_lock

    # Acquire lock
    async with acquire_trinity_lock("training_job", repo="jarvis-prime") as (acquired, meta):
        if acquired:
            print(f"Lock acquired with fencing token {meta.fencing_token}")
            await run_training()

Features:
- Automatic backend selection (Redis → File fallback)
- Cross-repo identification (jarvis, jarvis-prime, reactor-core)
- Fencing tokens for ordering guarantees
- Optional keepalive for long-running operations
- Thread-safe singleton pattern

Author: JARVIS AI System
Version: 1.0.0
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, AsyncIterator, Optional, Tuple

# Import from the main distributed lock manager
from backend.core.distributed_lock_manager import (
    DistributedLockManager,
    LockBackend,
    LockConfig,
    LockMetadata,
    get_lock_manager,
    shutdown_lock_manager,
    TRINITY_LOCK_PREFIX,
)

if TYPE_CHECKING:
    from typing import Dict, Any


__all__ = [
    # Main API
    "acquire_trinity_lock",
    "TrinityLockManager",
    # Re-exports
    "DistributedLockManager",
    "LockConfig",
    "LockMetadata",
    "LockBackend",
    "TRINITY_LOCK_PREFIX",
    # Utilities
    "get_repo_lock_manager",
    "shutdown_repo_locks",
]


# =============================================================================
# Repo Detection
# =============================================================================

def detect_repo_source() -> str:
    """
    Auto-detect which repo we're running from based on environment or path.

    Returns:
        One of: "jarvis", "jarvis-prime", "reactor-core"
    """
    # Check environment variable first
    env_repo = os.getenv("JARVIS_REPO_SOURCE")
    if env_repo:
        return env_repo

    # Try to detect from current working directory
    cwd = os.getcwd().lower()
    if "jarvis-prime" in cwd or "jarvis_prime" in cwd:
        return "jarvis-prime"
    elif "reactor-core" in cwd or "reactor_core" in cwd:
        return "reactor-core"
    elif "jarvis" in cwd:
        return "jarvis"

    # Default to jarvis
    return "jarvis"


# =============================================================================
# Trinity Lock Manager (Cross-Repo Wrapper)
# =============================================================================

class TrinityLockManager:
    """
    Cross-repo lock manager wrapper for Trinity architecture.

    Provides a consistent interface for lock management across:
    - JARVIS (Body): Main orchestration
    - JARVIS-Prime (Mind): LLM inference and learning
    - Reactor-Core (Nerves): Training and fine-tuning

    Usage:
        manager = TrinityLockManager(repo="jarvis-prime")
        await manager.initialize()

        async with manager.lock("model_update") as (acquired, meta):
            if acquired:
                await update_model()
    """

    def __init__(
        self,
        repo: Optional[str] = None,
        backend: LockBackend = LockBackend.AUTO,
        redis_host: Optional[str] = None,
        redis_port: Optional[int] = None,
    ):
        """
        Initialize Trinity lock manager.

        Args:
            repo: Repository source ("jarvis", "jarvis-prime", "reactor-core")
            backend: Lock backend preference (AUTO, REDIS, FILE)
            redis_host: Redis host (defaults to REDIS_HOST env var)
            redis_port: Redis port (defaults to REDIS_PORT env var)
        """
        self.repo = repo or detect_repo_source()

        # Build configuration
        config_kwargs = {
            "repo_source": self.repo,
            "backend": backend,
        }

        if redis_host:
            config_kwargs["redis_host"] = redis_host
        if redis_port:
            config_kwargs["redis_port"] = redis_port

        self.config = LockConfig(**config_kwargs)
        self._manager: Optional[DistributedLockManager] = None
        self._initialized = False

    async def initialize(self) -> None:
        """Initialize the lock manager."""
        if self._initialized:
            return

        self._manager = await get_lock_manager(self.config)
        self._initialized = True

    async def shutdown(self) -> None:
        """Shutdown the lock manager."""
        await shutdown_lock_manager()
        self._initialized = False
        self._manager = None

    @asynccontextmanager
    async def lock(
        self,
        name: str,
        timeout: float = 5.0,
        ttl: float = 10.0,
        enable_keepalive: bool = True,
    ) -> AsyncIterator[Tuple[bool, Optional[LockMetadata]]]:
        """
        Acquire a cross-repo lock.

        Args:
            name: Lock name
            timeout: Max wait time for acquisition
            ttl: Lock time-to-live
            enable_keepalive: Auto-extend TTL while held

        Yields:
            Tuple of (acquired, metadata)
        """
        if not self._initialized:
            await self.initialize()

        if self._manager is None:
            yield False, None
            return

        async with self._manager.acquire_unified(
            name, timeout, ttl, enable_keepalive
        ) as result:
            yield result

    async def get_status(self) -> "Dict[str, Any]":
        """Get lock manager status."""
        if not self._initialized or self._manager is None:
            return {"initialized": False, "repo": self.repo}

        status = await self._manager.get_cross_repo_status()
        status["trinity_repo"] = self.repo
        return status

    @property
    def is_redis_available(self) -> bool:
        """Check if Redis backend is available."""
        if self._manager is None:
            return False
        return self._manager.is_redis_available()

    @property
    def active_backend(self) -> str:
        """Get the active backend name."""
        if self._manager is None:
            return "none"
        return self._manager.get_active_backend().value


# =============================================================================
# Global Instance Management
# =============================================================================

_trinity_managers: dict[str, TrinityLockManager] = {}


async def get_repo_lock_manager(
    repo: Optional[str] = None,
    backend: LockBackend = LockBackend.AUTO,
) -> TrinityLockManager:
    """
    Get or create a Trinity lock manager for a specific repo.

    Args:
        repo: Repository source (auto-detected if not provided)
        backend: Lock backend preference

    Returns:
        TrinityLockManager instance
    """
    repo = repo or detect_repo_source()

    if repo not in _trinity_managers:
        manager = TrinityLockManager(repo=repo, backend=backend)
        await manager.initialize()
        _trinity_managers[repo] = manager

    return _trinity_managers[repo]


async def shutdown_repo_locks() -> None:
    """Shutdown all Trinity lock managers."""
    for manager in _trinity_managers.values():
        await manager.shutdown()
    _trinity_managers.clear()


# =============================================================================
# Convenience Function
# =============================================================================

@asynccontextmanager
async def acquire_trinity_lock(
    name: str,
    repo: Optional[str] = None,
    timeout: float = 5.0,
    ttl: float = 10.0,
    enable_keepalive: bool = True,
) -> AsyncIterator[Tuple[bool, Optional[LockMetadata]]]:
    """
    Convenience function for acquiring cross-repo locks.

    This is the simplest way to acquire a lock from any Trinity repo.

    Args:
        name: Lock name (e.g., "model_update", "training_job")
        repo: Source repo (auto-detected if not provided)
        timeout: Max wait time for acquisition (seconds)
        ttl: Lock time-to-live (seconds)
        enable_keepalive: Auto-extend TTL while lock is held

    Yields:
        Tuple of (acquired: bool, metadata: Optional[LockMetadata])

    Example (from JARVIS-Prime):
        async with acquire_trinity_lock("model_sync", repo="jarvis-prime") as (acquired, meta):
            if acquired:
                # Safe to sync model - we have the lock
                await sync_model_to_gcs()
                print(f"Used fencing token: {meta.fencing_token}")

    Example (from Reactor-Core):
        async with acquire_trinity_lock("training_job", repo="reactor-core") as (acquired, meta):
            if acquired:
                await start_training_pipeline()
    """
    manager = await get_repo_lock_manager(repo)
    async with manager.lock(name, timeout, ttl, enable_keepalive) as result:
        yield result


# =============================================================================
# Standard Lock Names (Cross-Repo Conventions)
# =============================================================================

class TrinityLocks:
    """
    Standard lock names for cross-repo coordination.

    Use these constants for consistency across repos.
    """
    # Model operations
    MODEL_SYNC = "trinity:model_sync"
    MODEL_UPDATE = "trinity:model_update"
    MODEL_DEPLOY = "trinity:model_deploy"

    # Training operations (Reactor-Core)
    TRAINING_JOB = "trinity:training_job"
    TRAINING_DATA_EXPORT = "trinity:training_data_export"
    CHECKPOINT_SAVE = "trinity:checkpoint_save"

    # Inference operations (JARVIS-Prime)
    INFERENCE_BATCH = "trinity:inference_batch"
    CACHE_UPDATE = "trinity:cache_update"

    # Cross-repo state
    STATE_SYNC = "trinity:state_sync"
    CONFIG_UPDATE = "trinity:config_update"
    HEALTH_CHECK = "trinity:health_check"

    # VBIA operations (JARVIS)
    VBIA_EVENTS = "trinity:vbia_events"
    SPEAKER_PROFILE = "trinity:speaker_profile"
    AUTH_STATE = "trinity:auth_state"
