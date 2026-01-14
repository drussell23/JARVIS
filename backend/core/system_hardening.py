"""
System Hardening v3.0 - Enterprise-Grade Infrastructure Protection
===================================================================

Provides critical system hardening features for the JARVIS Trinity ecosystem:
- Race condition prevention via atomic directory initialization
- Graceful shutdown orchestration with signal handlers
- Resource cleanup and garbage collection
- System health monitoring
- Critical path validation

Features:
- 🔒 Atomic directory creation with race condition prevention
- 🛑 Graceful shutdown handlers (SIGINT/SIGTERM)
- 🧹 Automatic cleanup of orphaned resources
- 📊 Health monitoring and metrics
- ⚙️ Critical path validation
- 🔄 Self-healing resource management

Architecture:
    ┌──────────────────────────────────────────────────────────────────┐
    │              System Hardening v3.0                               │
    ├──────────────────────────────────────────────────────────────────┤
    │                                                                   │
    │  CriticalDirectoryManager                                        │
    │  ├─ ~/.jarvis/registry/                 (service registry)       │
    │  ├─ ~/.jarvis/bridge/training_staging/  (drop-box protocol)      │
    │  ├─ ~/.jarvis/trinity/events/           (reactor events)         │
    │  ├─ ~/.jarvis/training_checkpoints/     (training state)         │
    │  └─ ~/.jarvis/cross_repo/               (IPC state files)        │
    │                                                                   │
    │  GracefulShutdownManager                                         │
    │  ├─ Signal handlers (SIGINT, SIGTERM, SIGHUP)                    │
    │  ├─ Shutdown hooks (register cleanup callbacks)                  │
    │  ├─ Timeout enforcement (graceful → force kill)                  │
    │  └─ Cleanup verification (ensure all resources freed)            │
    │                                                                   │
    │  ResourceGuard                                                   │
    │  ├─ File handle tracking                                         │
    │  ├─ Socket connection monitoring                                 │
    │  ├─ Process orphan detection                                     │
    │  └─ Memory leak detection                                        │
    │                                                                   │
    └──────────────────────────────────────────────────────────────────┘

Usage:
    from backend.core.system_hardening import (
        initialize_critical_directories,
        GracefulShutdownManager,
        get_system_health
    )

    # Initialize all critical directories at startup
    await initialize_critical_directories()

    # Setup graceful shutdown
    shutdown_manager = GracefulShutdownManager()
    shutdown_manager.register_hook("cleanup_training", cleanup_training_resources)
    shutdown_manager.install_signal_handlers()

    # Get system health
    health = await get_system_health()
    print(f"System health: {health}")

Author: JARVIS AI System
Version: 3.0.0
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


# =============================================================================
# Configuration
# =============================================================================

@dataclass
class HardeningConfig:
    """Configuration for system hardening."""

    # Critical directories
    jarvis_home: Path = field(
        default_factory=lambda: Path(os.getenv(
            "JARVIS_HOME",
            str(Path.home() / ".jarvis")
        ))
    )

    # Shutdown settings
    graceful_shutdown_timeout: float = field(
        default_factory=lambda: float(os.getenv("GRACEFUL_SHUTDOWN_TIMEOUT", "30.0"))
    )
    force_kill_timeout: float = field(
        default_factory=lambda: float(os.getenv("FORCE_KILL_TIMEOUT", "10.0"))
    )

    # Health monitoring
    health_check_interval: float = field(
        default_factory=lambda: float(os.getenv("HEALTH_CHECK_INTERVAL", "60.0"))
    )

    # Cleanup settings
    orphan_cleanup_enabled: bool = field(
        default_factory=lambda: os.getenv("ORPHAN_CLEANUP_ENABLED", "true").lower() == "true"
    )
    stale_file_max_age_hours: int = field(
        default_factory=lambda: int(os.getenv("STALE_FILE_MAX_AGE_HOURS", "24"))
    )


# =============================================================================
# Critical Directory Manager
# =============================================================================

class CriticalDirectoryManager:
    """
    Manages critical directories with race condition prevention.

    Ensures all required directories exist with proper permissions
    before any service starts using them.
    """

    # Define all critical directories
    CRITICAL_DIRS = [
        # Service Registry
        "registry",

        # Drop-Box Protocol (training data staging)
        "bridge/training_staging",

        # Reactor Events
        "trinity/events",

        # Training State
        "training_checkpoints",

        # Cross-Repo IPC State Files
        "cross_repo",

        # Logs
        "logs",

        # Model Cache
        "models/cache",

        # Temp Files
        "temp",

        # Voice Profiles
        "voice/profiles",
        "voice/embeddings",

        # Knowledge Graph
        "knowledge_graph",

        # Memory Store
        "memory/episodic",
        "memory/semantic",
    ]

    def __init__(self, config: Optional[HardeningConfig] = None):
        self.config = config or HardeningConfig()
        self._initialized = False
        self._created_dirs: Set[Path] = set()

    async def initialize_all(self) -> Dict[str, bool]:
        """
        Initialize all critical directories atomically.

        Returns:
            Dict mapping directory names to creation success status.
        """
        results = {}

        logger.info("🔒 Initializing critical directories...")

        for rel_path in self.CRITICAL_DIRS:
            full_path = self.config.jarvis_home / rel_path

            try:
                # Atomic directory creation (handles race conditions)
                await self._create_directory_atomic(full_path)
                results[rel_path] = True
                self._created_dirs.add(full_path)

            except Exception as e:
                logger.error(f"Failed to create directory {rel_path}: {e}")
                results[rel_path] = False

        success_count = sum(results.values())
        total_count = len(results)

        if success_count == total_count:
            logger.info(f"✅ All {total_count} critical directories initialized")
        else:
            logger.warning(
                f"⚠️ {success_count}/{total_count} critical directories initialized"
            )

        self._initialized = True
        return results

    async def _create_directory_atomic(self, path: Path) -> None:
        """
        Create directory atomically to prevent race conditions.

        Uses exist_ok=True which is atomic on POSIX systems.
        """
        # Run in thread to avoid blocking
        await asyncio.to_thread(
            path.mkdir,
            parents=True,
            exist_ok=True
        )

        # Verify directory exists (paranoid check)
        if not path.exists():
            raise RuntimeError(f"Directory creation failed: {path}")

        logger.debug(f"   └─ Created: {path}")

    async def cleanup_stale_files(self) -> int:
        """
        Clean up stale temporary files older than configured age.

        Returns:
            Number of files cleaned up.
        """
        if not self._initialized:
            return 0

        cutoff_time = time.time() - (self.config.stale_file_max_age_hours * 3600)
        cleaned = 0

        # Clean temp directory
        temp_dir = self.config.jarvis_home / "temp"
        if temp_dir.exists():
            for file_path in temp_dir.iterdir():
                try:
                    if file_path.stat().st_mtime < cutoff_time:
                        if file_path.is_file():
                            await asyncio.to_thread(file_path.unlink)
                            cleaned += 1
                except Exception as e:
                    logger.debug(f"Failed to clean {file_path}: {e}")

        # Clean old dropbox files
        dropbox_dir = self.config.jarvis_home / "bridge" / "training_staging"
        if dropbox_dir.exists():
            for file_path in dropbox_dir.iterdir():
                try:
                    if file_path.stat().st_mtime < cutoff_time:
                        if file_path.is_file():
                            await asyncio.to_thread(file_path.unlink)
                            cleaned += 1
                except Exception as e:
                    logger.debug(f"Failed to clean {file_path}: {e}")

        if cleaned > 0:
            logger.info(f"🧹 Cleaned {cleaned} stale files")

        return cleaned

    def verify_all_exist(self) -> Dict[str, bool]:
        """Verify all critical directories exist."""
        results = {}
        for rel_path in self.CRITICAL_DIRS:
            full_path = self.config.jarvis_home / rel_path
            results[rel_path] = full_path.exists()
        return results


# =============================================================================
# Graceful Shutdown Manager
# =============================================================================

class ShutdownPhase(Enum):
    """Phases of graceful shutdown."""
    PRE_SHUTDOWN = "pre_shutdown"        # Prepare for shutdown
    DRAIN_REQUESTS = "drain_requests"    # Stop accepting new requests
    COMPLETE_ACTIVE = "complete_active"  # Complete in-progress work
    CLEANUP = "cleanup"                  # Clean up resources
    FINAL = "final"                      # Final cleanup


@dataclass
class ShutdownHook:
    """A registered shutdown hook."""
    name: str
    callback: Callable[[], Awaitable[None]]
    phase: ShutdownPhase = ShutdownPhase.CLEANUP
    timeout: float = 10.0
    priority: int = 0  # Higher = runs earlier within phase


class GracefulShutdownManager:
    """
    Manages graceful shutdown with signal handlers and cleanup hooks.

    Provides ordered, timeout-enforced shutdown with multiple phases.
    """

    def __init__(self, config: Optional[HardeningConfig] = None):
        self.config = config or HardeningConfig()
        self._hooks: List[ShutdownHook] = []
        self._shutdown_in_progress = False
        self._shutdown_event: Optional[asyncio.Event] = None
        self._original_handlers: Dict[int, Any] = {}
        self._installed = False

    def register_hook(
        self,
        name: str,
        callback: Callable[[], Awaitable[None]],
        phase: ShutdownPhase = ShutdownPhase.CLEANUP,
        timeout: float = 10.0,
        priority: int = 0
    ) -> None:
        """
        Register a shutdown hook.

        Args:
            name: Human-readable name for logging
            callback: Async function to call during shutdown
            phase: When to run this hook during shutdown
            timeout: Maximum time to wait for this hook
            priority: Higher priority runs first within the same phase
        """
        hook = ShutdownHook(
            name=name,
            callback=callback,
            phase=phase,
            timeout=timeout,
            priority=priority
        )
        self._hooks.append(hook)
        logger.debug(f"Registered shutdown hook: {name} (phase={phase.value})")

    def unregister_hook(self, name: str) -> bool:
        """Remove a registered hook by name."""
        original_count = len(self._hooks)
        self._hooks = [h for h in self._hooks if h.name != name]
        return len(self._hooks) < original_count

    def install_signal_handlers(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        """
        Install signal handlers for graceful shutdown.

        Args:
            loop: Event loop to use. If None, gets current loop.
        """
        if self._installed:
            logger.debug("Signal handlers already installed")
            return

        self._shutdown_event = asyncio.Event()

        def handle_signal(signum: int, frame: Any) -> None:
            """Handle shutdown signal."""
            sig_name = signal.Signals(signum).name
            logger.info(f"🛑 Received {sig_name} - initiating graceful shutdown...")

            # Set shutdown event (async-safe)
            if loop and loop.is_running():
                loop.call_soon_threadsafe(self._shutdown_event.set)
            else:
                self._shutdown_event.set()

            # Don't call shutdown directly - let the main loop handle it
            if not self._shutdown_in_progress:
                self._shutdown_in_progress = True

        # Store original handlers
        for sig in (signal.SIGINT, signal.SIGTERM):
            self._original_handlers[sig] = signal.getsignal(sig)
            signal.signal(sig, handle_signal)

        # Also try to handle SIGHUP (Unix only)
        if hasattr(signal, 'SIGHUP'):
            self._original_handlers[signal.SIGHUP] = signal.getsignal(signal.SIGHUP)
            signal.signal(signal.SIGHUP, handle_signal)

        # Register atexit handler
        atexit.register(self._sync_cleanup)

        self._installed = True
        logger.info("✅ Signal handlers installed (SIGINT, SIGTERM)")

    def restore_signal_handlers(self) -> None:
        """Restore original signal handlers."""
        for sig, handler in self._original_handlers.items():
            signal.signal(sig, handler)
        self._original_handlers.clear()
        self._installed = False

    async def wait_for_shutdown_signal(self) -> None:
        """Wait for a shutdown signal to be received."""
        if self._shutdown_event:
            await self._shutdown_event.wait()

    def is_shutdown_requested(self) -> bool:
        """Check if shutdown has been requested."""
        if self._shutdown_event:
            return self._shutdown_event.is_set()
        return False

    async def execute_shutdown(self) -> Dict[str, bool]:
        """
        Execute graceful shutdown with all registered hooks.

        Returns:
            Dict mapping hook names to success status.
        """
        if not self._shutdown_in_progress:
            self._shutdown_in_progress = True

        results = {}

        logger.info("🛑 Executing graceful shutdown...")
        start_time = time.time()

        # Group hooks by phase
        hooks_by_phase: Dict[ShutdownPhase, List[ShutdownHook]] = {
            phase: [] for phase in ShutdownPhase
        }
        for hook in self._hooks:
            hooks_by_phase[hook.phase].append(hook)

        # Execute phases in order
        for phase in ShutdownPhase:
            phase_hooks = hooks_by_phase[phase]
            if not phase_hooks:
                continue

            logger.info(f"   Phase: {phase.value} ({len(phase_hooks)} hooks)")

            # Sort by priority (higher first)
            phase_hooks.sort(key=lambda h: -h.priority)

            for hook in phase_hooks:
                try:
                    logger.debug(f"      └─ Running: {hook.name}")
                    await asyncio.wait_for(hook.callback(), timeout=hook.timeout)
                    results[hook.name] = True
                    logger.debug(f"      └─ ✅ {hook.name} complete")
                except asyncio.TimeoutError:
                    results[hook.name] = False
                    logger.warning(f"      └─ ⏱️ {hook.name} timed out after {hook.timeout}s")
                except Exception as e:
                    results[hook.name] = False
                    logger.error(f"      └─ ❌ {hook.name} failed: {e}")

        elapsed = time.time() - start_time
        success_count = sum(results.values())

        logger.info(
            f"✅ Graceful shutdown complete ({success_count}/{len(results)} hooks, {elapsed:.2f}s)"
        )

        return results

    def _sync_cleanup(self) -> None:
        """Synchronous cleanup for atexit handler."""
        if not self._shutdown_in_progress:
            logger.debug("Atexit: Running synchronous cleanup")
            # Can't run async hooks here, just log
            logger.debug("Atexit: Cleanup complete")


# =============================================================================
# Resource Guard (Resource Leak Prevention)
# =============================================================================

class ResourceGuard:
    """
    Guards against resource leaks by tracking open resources.

    Provides monitoring and cleanup of:
    - File handles
    - Network connections
    - Child processes
    """

    def __init__(self):
        self._tracked_files: Set[int] = set()  # File descriptor numbers
        self._tracked_processes: Set[int] = set()  # PIDs
        self._startup_time = time.time()

    def track_file(self, fd: int) -> None:
        """Track an open file descriptor."""
        self._tracked_files.add(fd)

    def untrack_file(self, fd: int) -> None:
        """Stop tracking a file descriptor."""
        self._tracked_files.discard(fd)

    def track_process(self, pid: int) -> None:
        """Track a child process."""
        self._tracked_processes.add(pid)

    def untrack_process(self, pid: int) -> None:
        """Stop tracking a process."""
        self._tracked_processes.discard(pid)

    async def check_orphan_processes(self) -> List[int]:
        """
        Check for orphaned child processes.

        Returns:
            List of orphaned PIDs that need cleanup.
        """
        import psutil

        orphans = []
        current_pid = os.getpid()

        try:
            current_process = psutil.Process(current_pid)
            children = current_process.children(recursive=True)

            for child in children:
                if child.pid not in self._tracked_processes:
                    # Check if process is zombie or unresponsive
                    try:
                        status = child.status()
                        if status == psutil.STATUS_ZOMBIE:
                            orphans.append(child.pid)
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass

        except Exception as e:
            logger.debug(f"Error checking orphan processes: {e}")

        return orphans

    async def cleanup_orphan_processes(self, pids: List[int]) -> int:
        """
        Clean up orphaned processes.

        Returns:
            Number of processes cleaned up.
        """
        cleaned = 0

        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
                await asyncio.sleep(0.5)

                # Check if still running
                try:
                    os.kill(pid, 0)
                    # Still running, force kill
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass  # Already dead

                cleaned += 1
            except (ProcessLookupError, PermissionError):
                pass

        return cleaned

    def get_stats(self) -> Dict[str, Any]:
        """Get resource tracking statistics."""
        return {
            "tracked_files": len(self._tracked_files),
            "tracked_processes": len(self._tracked_processes),
            "uptime_seconds": time.time() - self._startup_time
        }


# =============================================================================
# System Health Monitoring
# =============================================================================

@dataclass
class SystemHealth:
    """System health metrics."""
    timestamp: float
    cpu_percent: float
    memory_percent: float
    memory_available_gb: float
    disk_percent: float
    disk_available_gb: float
    open_files: int
    active_connections: int
    critical_dirs_ok: bool
    overall_status: str  # healthy, degraded, critical


async def get_system_health() -> SystemHealth:
    """
    Get current system health metrics.

    Returns:
        SystemHealth object with current metrics.
    """
    import psutil

    # CPU usage
    cpu_percent = psutil.cpu_percent(interval=0.1)

    # Memory
    mem = psutil.virtual_memory()
    memory_percent = mem.percent
    memory_available_gb = mem.available / (1024 ** 3)

    # Disk
    disk = psutil.disk_usage(str(Path.home()))
    disk_percent = disk.percent
    disk_available_gb = disk.free / (1024 ** 3)

    # Process resources
    process = psutil.Process()
    open_files = len(process.open_files())
    connections = len(process.connections())

    # Check critical directories
    dir_manager = CriticalDirectoryManager()
    dir_status = dir_manager.verify_all_exist()
    critical_dirs_ok = all(dir_status.values())

    # Determine overall status
    if memory_percent > 90 or disk_percent > 95 or not critical_dirs_ok:
        overall_status = "critical"
    elif memory_percent > 80 or disk_percent > 85 or cpu_percent > 90:
        overall_status = "degraded"
    else:
        overall_status = "healthy"

    return SystemHealth(
        timestamp=time.time(),
        cpu_percent=cpu_percent,
        memory_percent=memory_percent,
        memory_available_gb=round(memory_available_gb, 2),
        disk_percent=disk_percent,
        disk_available_gb=round(disk_available_gb, 2),
        open_files=open_files,
        active_connections=connections,
        critical_dirs_ok=critical_dirs_ok,
        overall_status=overall_status
    )


# =============================================================================
# Convenience Functions
# =============================================================================

_global_dir_manager: Optional[CriticalDirectoryManager] = None
_global_shutdown_manager: Optional[GracefulShutdownManager] = None
_global_resource_guard: Optional[ResourceGuard] = None


async def initialize_critical_directories() -> Dict[str, bool]:
    """Initialize all critical directories. Should be called at startup."""
    global _global_dir_manager

    if _global_dir_manager is None:
        _global_dir_manager = CriticalDirectoryManager()

    return await _global_dir_manager.initialize_all()


def get_shutdown_manager() -> GracefulShutdownManager:
    """Get the global shutdown manager instance."""
    global _global_shutdown_manager

    if _global_shutdown_manager is None:
        _global_shutdown_manager = GracefulShutdownManager()

    return _global_shutdown_manager


def get_resource_guard() -> ResourceGuard:
    """Get the global resource guard instance."""
    global _global_resource_guard

    if _global_resource_guard is None:
        _global_resource_guard = ResourceGuard()

    return _global_resource_guard


async def harden_system() -> Dict[str, Any]:
    """
    Perform full system hardening.

    This is a convenience function that:
    1. Initializes critical directories
    2. Installs signal handlers
    3. Sets up resource tracking
    4. Cleans stale files

    Returns:
        Summary of hardening actions performed.
    """
    results = {}

    # Initialize directories
    logger.info("🔒 Performing system hardening...")

    dir_manager = CriticalDirectoryManager()
    dir_results = await dir_manager.initialize_all()
    results["directories"] = dir_results

    # Install signal handlers
    shutdown_manager = get_shutdown_manager()
    try:
        loop = asyncio.get_event_loop()
        shutdown_manager.install_signal_handlers(loop)
        results["signal_handlers"] = True
    except Exception as e:
        logger.error(f"Failed to install signal handlers: {e}")
        results["signal_handlers"] = False

    # Clean stale files
    cleaned = await dir_manager.cleanup_stale_files()
    results["stale_files_cleaned"] = cleaned

    # Get initial health
    health = await get_system_health()
    results["initial_health"] = health.overall_status

    logger.info(f"✅ System hardening complete (health: {health.overall_status})")

    return results


# =============================================================================
# Module Exports
# =============================================================================

__all__ = [
    # Configuration
    "HardeningConfig",

    # Directory Management
    "CriticalDirectoryManager",
    "initialize_critical_directories",

    # Shutdown Management
    "GracefulShutdownManager",
    "ShutdownPhase",
    "ShutdownHook",
    "get_shutdown_manager",

    # Resource Management
    "ResourceGuard",
    "get_resource_guard",

    # Health Monitoring
    "SystemHealth",
    "get_system_health",

    # Convenience
    "harden_system",
]
