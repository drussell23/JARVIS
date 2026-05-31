"""
Cross-Repo Cleanup Coordinator v1.0 - Unified Resource Management Across JARVIS Ecosystem
===========================================================================================

This module provides centralized cleanup coordination across the JARVIS ecosystem:
- JARVIS (main AI agent)
- jarvis-prime (local LLM inference)
- reactor-core (training pipeline)

It ensures that:
1. Multiprocessing resources (semaphores, pools) are tracked globally
2. Cleanup happens in the correct order on shutdown
3. Cross-repo resources can be released even if one process dies unexpectedly
4. File-based coordination works when processes can't communicate directly

ROOT CAUSE FIX:
    The "leaked semaphore" warning occurs because:
    1. Multiple repos create their own SentenceTransformer/ML model instances
    2. Each instance may spawn internal multiprocessing pools
    3. When any process dies unexpectedly, its pools aren't cleaned up
    4. The resource_tracker warns about orphaned semaphores

SOLUTION:
    1. Centralized resource registry (file-based for cross-process access)
    2. Unified cleanup protocol that handles unexpected process death
    3. Each repo registers its resources with this coordinator
    4. Emergency cleanup scans for and removes orphaned resources

Usage:
    # In any JARVIS repo:
    from backend.core.cross_repo_cleanup import (
        register_resource,
        unregister_resource,
        cleanup_all_resources,
    )
    
    # Register a resource
    register_resource("jarvis-prime", "semaphore", "model_loader_sem")
    
    # Cleanup during shutdown
    await cleanup_all_resources()

Author: JARVIS System  
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import atexit
import gc
import json
import logging
import os
import signal
import socket
import threading
import time
from contextlib import suppress
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

# =============================================================================
# CONFIGURATION
# =============================================================================

JARVIS_HOME = Path.home() / ".jarvis"
REGISTRY_FILE = JARVIS_HOME / "resource_registry.json"
CLEANUP_SOCKET = "/tmp/jarvis_cleanup_coordinator.sock"
REGISTRY_LOCK_FILE = JARVIS_HOME / "resource_registry.lock"
CLEANUP_TIMEOUT = float(os.getenv("JARVIS_CLEANUP_TIMEOUT", "10.0"))

# Known JARVIS repos
KNOWN_REPOS = ["jarvis", "jarvis-prime", "reactor-core"]

# Resource types that need special cleanup
RESOURCE_TYPES = {
    "semaphore": {"cleanup_priority": 10, "requires_gc": True},
    "process_pool": {"cleanup_priority": 5, "requires_gc": True},
    "thread_pool": {"cleanup_priority": 20, "requires_gc": False},
    "model": {"cleanup_priority": 15, "requires_gc": True},
    "socket": {"cleanup_priority": 25, "requires_gc": False},
    "file_handle": {"cleanup_priority": 30, "requires_gc": False},
}


@dataclass
class ResourceEntry:
    """A tracked cross-repo resource."""
    repo: str
    resource_type: str
    resource_id: str
    pid: int
    created_at: float
    metadata: Dict[str, Any] = None
    
    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        if d["metadata"] is None:
            d["metadata"] = {}
        return d
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ResourceEntry":
        return cls(
            repo=data["repo"],
            resource_type=data["resource_type"],
            resource_id=data["resource_id"],
            pid=data["pid"],
            created_at=data.get("created_at", time.time()),
            metadata=data.get("metadata", {}),
        )


# =============================================================================
# FILE-BASED REGISTRY (Cross-Process Accessible)
# =============================================================================

class FileLock:
    """Simple file-based lock for cross-process coordination."""
    
    def __init__(self, lock_path: Path):
        self.lock_path = lock_path
        self._fd = None
    
    def acquire(self, timeout: float = 5.0) -> bool:
        """Acquire the file lock with timeout."""
        import fcntl
        
        start = time.time()
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        
        while time.time() - start < timeout:
            try:
                self._fd = open(self.lock_path, 'w')
                fcntl.flock(self._fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                # Write our PID for debugging
                self._fd.write(str(os.getpid()))
                self._fd.flush()
                return True
            except (IOError, OSError):
                if self._fd:
                    self._fd.close()
                    self._fd = None
                time.sleep(0.1)
        
        return False
    
    def release(self) -> None:
        """Release the file lock."""
        import fcntl
        
        if self._fd:
            try:
                fcntl.flock(self._fd.fileno(), fcntl.LOCK_UN)
                self._fd.close()
            except Exception:
                pass
            finally:
                self._fd = None
    
    def __enter__(self):
        if not self.acquire():
            raise TimeoutError("Could not acquire file lock")
        return self
    
    def __exit__(self, *args):
        self.release()


class ResourceRegistry:
    """
    File-based resource registry for cross-process tracking.
    
    Uses a JSON file with file locking for safe concurrent access.
    """
    
    def __init__(self, registry_path: Path = REGISTRY_FILE):
        self.registry_path = registry_path
        self.lock = FileLock(REGISTRY_LOCK_FILE)
        self._ensure_dirs()
    
    def _ensure_dirs(self) -> None:
        """Ensure the registry directory exists."""
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
    
    def _read_registry(self) -> Dict[str, List[Dict[str, Any]]]:
        """Read the registry file."""
        if not self.registry_path.exists():
            return {"resources": [], "metadata": {"version": 1, "updated_at": time.time()}}
        
        try:
            with open(self.registry_path, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {"resources": [], "metadata": {"version": 1, "updated_at": time.time()}}
    
    def _write_registry(self, data: Dict[str, Any]) -> None:
        """Write the registry file atomically."""
        data["metadata"]["updated_at"] = time.time()
        
        # Write to temp file first, then rename (atomic on POSIX)
        temp_path = self.registry_path.with_suffix('.tmp')
        with open(temp_path, 'w') as f:
            json.dump(data, f, indent=2)
        temp_path.rename(self.registry_path)
    
    def register(self, entry: ResourceEntry) -> bool:
        """Register a resource in the cross-repo registry."""
        try:
            with self.lock:
                data = self._read_registry()
                
                # Check for duplicates
                for existing in data["resources"]:
                    if (existing["repo"] == entry.repo and 
                        existing["resource_type"] == entry.resource_type and
                        existing["resource_id"] == entry.resource_id):
                        # Update existing entry
                        existing.update(entry.to_dict())
                        self._write_registry(data)
                        return True
                
                # Add new entry
                data["resources"].append(entry.to_dict())
                self._write_registry(data)
                return True
                
        except Exception as e:
            logger.error(f"[CrossRepoCleanup] Failed to register resource: {e}")
            return False
    
    def unregister(self, repo: str, resource_type: str, resource_id: str) -> bool:
        """Unregister a resource from the cross-repo registry."""
        try:
            with self.lock:
                data = self._read_registry()
                
                original_count = len(data["resources"])
                data["resources"] = [
                    r for r in data["resources"]
                    if not (r["repo"] == repo and 
                           r["resource_type"] == resource_type and
                           r["resource_id"] == resource_id)
                ]
                
                if len(data["resources"]) < original_count:
                    self._write_registry(data)
                    return True
                    
                return False
                
        except Exception as e:
            logger.error(f"[CrossRepoCleanup] Failed to unregister resource: {e}")
            return False
    
    def get_resources_by_repo(self, repo: str) -> List[ResourceEntry]:
        """Get all resources registered by a specific repo."""
        try:
            data = self._read_registry()
            return [
                ResourceEntry.from_dict(r) 
                for r in data["resources"] 
                if r["repo"] == repo
            ]
        except Exception:
            return []
    
    def get_resources_by_pid(self, pid: int) -> List[ResourceEntry]:
        """Get all resources registered by a specific process."""
        try:
            data = self._read_registry()
            return [
                ResourceEntry.from_dict(r) 
                for r in data["resources"] 
                if r["pid"] == pid
            ]
        except Exception:
            return []
    
    def get_orphaned_resources(self) -> List[ResourceEntry]:
        """
        Find resources whose owning process is no longer running.
        
        These are likely leaked and need cleanup.
        """
        orphaned = []
        
        try:
            import psutil
            
            data = self._read_registry()
            running_pids = {p.pid for p in psutil.process_iter(['pid'])}
            
            for r in data["resources"]:
                if r["pid"] not in running_pids:
                    orphaned.append(ResourceEntry.from_dict(r))
            
        except ImportError:
            # psutil not available - can't detect orphans
            pass
        except Exception as e:
            logger.debug(f"[CrossRepoCleanup] Error detecting orphaned resources: {e}")
        
        return orphaned
    
    def cleanup_orphaned(self) -> int:
        """Remove orphaned resources from the registry."""
        try:
            orphaned = self.get_orphaned_resources()
            if not orphaned:
                return 0
            
            with self.lock:
                data = self._read_registry()
                orphaned_ids = {
                    (r.repo, r.resource_type, r.resource_id) for r in orphaned
                }
                
                original_count = len(data["resources"])
                data["resources"] = [
                    r for r in data["resources"]
                    if (r["repo"], r["resource_type"], r["resource_id"]) not in orphaned_ids
                ]
                
                removed = original_count - len(data["resources"])
                if removed > 0:
                    self._write_registry(data)
                    logger.info(f"[CrossRepoCleanup] Cleaned up {removed} orphaned resources")
                
                return removed
                
        except Exception as e:
            logger.error(f"[CrossRepoCleanup] Orphan cleanup failed: {e}")
            return 0
    
    def get_all_resources(self) -> List[ResourceEntry]:
        """Get all registered resources."""
        try:
            data = self._read_registry()
            return [ResourceEntry.from_dict(r) for r in data["resources"]]
        except Exception:
            return []
    
    def clear_repo_resources(self, repo: str) -> int:
        """Remove all resources registered by a specific repo."""
        try:
            with self.lock:
                data = self._read_registry()
                original_count = len(data["resources"])
                data["resources"] = [r for r in data["resources"] if r["repo"] != repo]
                removed = original_count - len(data["resources"])
                
                if removed > 0:
                    self._write_registry(data)
                
                return removed
                
        except Exception as e:
            logger.error(f"[CrossRepoCleanup] Failed to clear repo resources: {e}")
            return 0


# =============================================================================
# CROSS-REPO CLEANUP COORDINATOR
# =============================================================================

class CrossRepoCleanupCoordinator:
    """
    Coordinates cleanup across the JARVIS ecosystem.
    
    Features:
    - Tracks resources across all JARVIS repos
    - Provides unified cleanup protocol
    - Handles orphaned resources from crashed processes
    - Supports both sync and async cleanup
    """
    
    _instance: Optional["CrossRepoCleanupCoordinator"] = None
    _instance_lock = threading.Lock()
    
    def __new__(cls) -> "CrossRepoCleanupCoordinator":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance
    
    def __init__(self):
        if getattr(self, "_initialized", False):
            return
        
        self.registry = ResourceRegistry()
        self._repo_name = self._detect_repo_name()
        self._cleanup_callbacks: Dict[str, Callable[[], None]] = {}
        self._async_cleanup_callbacks: Dict[str, Callable[[], Any]] = {}
        self._shutdown_started = False
        self._cleaned_up = False
        
        # Register atexit handler
        atexit.register(self._sync_emergency_cleanup)
        
        # Register signal handlers for graceful cleanup
        self._register_signal_handlers()
        
        self._initialized = True
        logger.info(f"[CrossRepoCleanup] Initialized for repo: {self._repo_name}")
    
    def _detect_repo_name(self) -> str:
        """Auto-detect which JARVIS repo we're running in."""
        cwd = Path.cwd()
        script_path = Path(__file__).resolve()
        
        if "jarvis-prime" in str(script_path) or "jarvis-prime" in str(cwd):
            return "jarvis-prime"
        elif "reactor-core" in str(script_path) or "reactor-core" in str(cwd):
            return "reactor-core"
        else:
            return "jarvis"
    
    def _register_signal_handlers(self) -> None:
        """Register signal handlers for graceful cleanup."""
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                previous = signal.getsignal(sig)
                
                def handler(signum, frame, prev=previous):
                    self._handle_shutdown_signal(signum, frame, prev)
                
                signal.signal(sig, handler)
            except Exception:
                pass  # May fail in non-main thread
    
    def _handle_shutdown_signal(self, signum, frame, previous_handler) -> None:
        """Handle shutdown signal with cleanup."""
        logger.info(f"[CrossRepoCleanup] Received signal {signum}, starting cleanup...")
        self.cleanup_sync()
        
        # Call previous handler if it exists
        if previous_handler and callable(previous_handler):
            previous_handler(signum, frame)
    
    def register_resource(
        self,
        resource_type: str,
        resource_id: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Register a resource for tracking and cleanup.
        
        Args:
            resource_type: Type of resource (semaphore, process_pool, etc.)
            resource_id: Unique identifier for this resource
            metadata: Optional additional metadata
        """
        entry = ResourceEntry(
            repo=self._repo_name,
            resource_type=resource_type,
            resource_id=resource_id,
            pid=os.getpid(),
            created_at=time.time(),
            metadata=metadata or {},
        )
        
        success = self.registry.register(entry)
        if success:
            logger.debug(f"[CrossRepoCleanup] Registered {resource_type}:{resource_id}")
        return success
    
    def unregister_resource(self, resource_type: str, resource_id: str) -> bool:
        """Unregister a resource (call when resource is cleaned up)."""
        success = self.registry.unregister(self._repo_name, resource_type, resource_id)
        if success:
            logger.debug(f"[CrossRepoCleanup] Unregistered {resource_type}:{resource_id}")
        return success
    
    def register_cleanup_callback(
        self,
        name: str,
        callback: Callable[[], None],
    ) -> None:
        """Register a synchronous cleanup callback."""
        self._cleanup_callbacks[name] = callback
    
    def register_async_cleanup_callback(
        self,
        name: str,
        callback: Callable[[], Any],
    ) -> None:
        """Register an async cleanup callback."""
        self._async_cleanup_callbacks[name] = callback
    
    def cleanup_sync(self, timeout: float = CLEANUP_TIMEOUT) -> Dict[str, Any]:
        """
        Synchronous cleanup of all registered resources.
        
        Called by atexit and signal handlers.
        """
        if self._cleaned_up:
            return {"already_cleaned": True}
        
        self._shutdown_started = True
        results = {
            "callbacks_executed": 0,
            "callbacks_failed": 0,
            "resources_cleared": 0,
            "gc_collected": 0,
        }
        
        # Execute sync callbacks
        for name, callback in self._cleanup_callbacks.items():
            try:
                callback()
                results["callbacks_executed"] += 1
            except Exception as e:
                logger.error(f"[CrossRepoCleanup] Callback {name} failed: {e}")
                results["callbacks_failed"] += 1
        
        # Clear our resources from registry
        results["resources_cleared"] = self.registry.clear_repo_resources(self._repo_name)
        
        # Force garbage collection for semaphore cleanup
        results["gc_collected"] = gc.collect()
        
        # Clean up any torch.multiprocessing resources
        self._cleanup_torch_multiprocessing()
        
        self._cleaned_up = True
        logger.info(f"[CrossRepoCleanup] Sync cleanup complete: {results}")
        return results
    
    async def cleanup_async(self, timeout: float = CLEANUP_TIMEOUT) -> Dict[str, Any]:
        """
        Asynchronous cleanup of all registered resources.
        
        Preferred when running in async context.
        """
        if self._cleaned_up:
            return {"already_cleaned": True}
        
        self._shutdown_started = True
        results = {
            "sync_callbacks_executed": 0,
            "async_callbacks_executed": 0,
            "callbacks_failed": 0,
            "resources_cleared": 0,
            "gc_collected": 0,
        }
        
        # Execute async callbacks first
        for name, callback in self._async_cleanup_callbacks.items():
            try:
                result = callback()
                if asyncio.iscoroutine(result):
                    await asyncio.wait_for(result, timeout=timeout/len(self._async_cleanup_callbacks) if self._async_cleanup_callbacks else timeout)
                results["async_callbacks_executed"] += 1
            except asyncio.TimeoutError:
                logger.warning(f"[CrossRepoCleanup] Async callback {name} timed out")
                results["callbacks_failed"] += 1
            except Exception as e:
                logger.error(f"[CrossRepoCleanup] Async callback {name} failed: {e}")
                results["callbacks_failed"] += 1
        
        # Execute sync callbacks in executor
        loop = asyncio.get_running_loop()
        for name, callback in self._cleanup_callbacks.items():
            try:
                await asyncio.wait_for(
                    loop.run_in_executor(None, callback),
                    timeout=timeout/len(self._cleanup_callbacks) if self._cleanup_callbacks else timeout
                )
                results["sync_callbacks_executed"] += 1
            except Exception as e:
                logger.error(f"[CrossRepoCleanup] Callback {name} failed: {e}")
                results["callbacks_failed"] += 1
        
        # Clear our resources from registry
        results["resources_cleared"] = self.registry.clear_repo_resources(self._repo_name)
        
        # Force garbage collection
        results["gc_collected"] = gc.collect()
        
        # Cleanup torch resources
        self._cleanup_torch_multiprocessing()
        
        self._cleaned_up = True
        logger.info(f"[CrossRepoCleanup] Async cleanup complete: {results}")
        return results
    
    def _cleanup_torch_multiprocessing(self) -> int:
        """Clean up torch.multiprocessing resources."""
        cleaned = 0
        
        try:
            import torch.multiprocessing as mp
            
            # Terminate any active child processes
            if hasattr(mp, 'active_children'):
                for child in mp.active_children():
                    try:
                        if child.is_alive():
                            child.terminate()
                            child.join(timeout=1.0)
                            cleaned += 1
                    except Exception:
                        pass
        except ImportError:
            pass
        except Exception as e:
            logger.debug(f"[CrossRepoCleanup] torch.multiprocessing cleanup error: {e}")
        
        return cleaned
    
    def cleanup_orphaned_resources(self) -> int:
        """Clean up resources from processes that have died."""
        return self.registry.cleanup_orphaned()
    
    def _sync_emergency_cleanup(self) -> None:
        """Emergency cleanup for atexit — budget-bounded (Slice 48).

        Runs at atexit, so there is NO running event loop (asyncio.to_thread is
        unavailable). The cleanup body — callbacks + registry clear + GC — is
        run in a daemon thread joined for at most
        ``JARVIS_CROSS_REPO_EMERGENCY_CLEANUP_BUDGET_S`` (default 5s). If a
        registry ``open()``/``stat`` stalls (e.g. the post-host-resume FS that
        blocked v43 for 38.6s), the join times out and teardown proceeds — the
        leaked daemon thread dies with the process. This keeps the atexit path
        comfortably under the 30s BoundedShutdownWatchdog deadline so a slow
        cleanup can never force an unclean os._exit(75).
        """
        if self._cleaned_up:
            return
        # Mark cleaned up BEFORE dispatching so a stalled thread can never be
        # re-entered (idempotent even if the budget is exceeded).
        self._cleaned_up = True

        def _run_body() -> None:
            try:
                for callback in self._cleanup_callbacks.values():
                    with suppress(Exception):
                        callback()
                self.registry.clear_repo_resources(self._repo_name)
                gc.collect()
            except Exception:
                pass  # Swallow all errors during emergency cleanup

        budget_s = 5.0
        raw = os.environ.get(
            "JARVIS_CROSS_REPO_EMERGENCY_CLEANUP_BUDGET_S", "",
        ).strip()
        if raw:
            with suppress(TypeError, ValueError):
                budget_s = max(0.1, float(raw))

        worker = threading.Thread(
            target=_run_body,
            name="cross-repo-emergency-cleanup",
            daemon=True,
        )
        worker.start()
        worker.join(timeout=budget_s)
        if worker.is_alive():
            with suppress(Exception):
                logger.warning(
                    "[CrossRepoCleanup] emergency cleanup exceeded %.1fs "
                    "budget — abandoning to stay under shutdown deadline",
                    budget_s,
                )
    
    def get_status(self) -> Dict[str, Any]:
        """Get coordinator status."""
        return {
            "repo": self._repo_name,
            "shutdown_started": self._shutdown_started,
            "cleaned_up": self._cleaned_up,
            "sync_callbacks": list(self._cleanup_callbacks.keys()),
            "async_callbacks": list(self._async_cleanup_callbacks.keys()),
            "registered_resources": len(self.registry.get_resources_by_repo(self._repo_name)),
            "total_resources": len(self.registry.get_all_resources()),
            "orphaned_resources": len(self.registry.get_orphaned_resources()),
        }


# =============================================================================
# GLOBAL ACCESS FUNCTIONS
# =============================================================================

_coordinator: Optional[CrossRepoCleanupCoordinator] = None


def get_cleanup_coordinator() -> CrossRepoCleanupCoordinator:
    """Get the global CrossRepoCleanupCoordinator singleton."""
    global _coordinator
    if _coordinator is None:
        _coordinator = CrossRepoCleanupCoordinator()
    return _coordinator


def register_resource(
    resource_type: str,
    resource_id: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> bool:
    """
    Convenience function to register a resource.
    
    Usage:
        register_resource("semaphore", "model_loader_sem")
    """
    return get_cleanup_coordinator().register_resource(resource_type, resource_id, metadata)


def unregister_resource(resource_type: str, resource_id: str) -> bool:
    """Convenience function to unregister a resource."""
    return get_cleanup_coordinator().unregister_resource(resource_type, resource_id)


def register_cleanup_callback(name: str, callback: Callable[[], None]) -> None:
    """Convenience function to register a cleanup callback."""
    get_cleanup_coordinator().register_cleanup_callback(name, callback)


async def cleanup_all_resources(timeout: float = CLEANUP_TIMEOUT) -> Dict[str, Any]:
    """Async cleanup of all resources."""
    return await get_cleanup_coordinator().cleanup_async(timeout)


def cleanup_all_resources_sync(timeout: float = CLEANUP_TIMEOUT) -> Dict[str, Any]:
    """Sync cleanup of all resources."""
    return get_cleanup_coordinator().cleanup_sync(timeout)


def cleanup_orphaned() -> int:
    """Clean up orphaned resources from dead processes."""
    return get_cleanup_coordinator().cleanup_orphaned_resources()


# =============================================================================
# EXPORTS
# =============================================================================

__all__ = [
    "CrossRepoCleanupCoordinator",
    "ResourceRegistry",
    "ResourceEntry",
    "get_cleanup_coordinator",
    "register_resource",
    "unregister_resource",
    "register_cleanup_callback",
    "cleanup_all_resources",
    "cleanup_all_resources_sync",
    "cleanup_orphaned",
    "KNOWN_REPOS",
    "RESOURCE_TYPES",
]
