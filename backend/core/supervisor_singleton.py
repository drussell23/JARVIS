#!/usr/bin/env python3
"""
JARVIS Supervisor Singleton v123.0
==================================

Enterprise-grade singleton enforcement for the JARVIS system.
Prevents multiple supervisors/entry points from running simultaneously.

This module provides:
1. Cross-process PID file locking with stale detection
2. Process tree awareness (handles forks and child processes)
3. Atomic file operations for reliability
4. Graceful conflict resolution
5. v113.0: IPC command socket for restart/takeover/status commands
6. v114.0: Functional health checks (IPC ping, HTTP health, readiness state)
7. v115.0: Advanced multi-layer health verification with async parallel checks,
           cross-repo integration, intelligent recovery, and zero-config autodiscovery
8. v120.0: Thread-isolated IPC server for guaranteed responsiveness during heavy
           event loop operations. Restart marker system for cross-process coordination.
           Intelligent restart verification with adaptive timeouts.

Usage:
    from backend.core.supervisor_singleton import acquire_supervisor_lock, release_supervisor_lock

    if not acquire_supervisor_lock("run_supervisor"):
        print("Another JARVIS instance is running!")
        sys.exit(1)

    try:
        # Run main supervisor logic
        pass
    finally:
        release_supervisor_lock()

IPC Commands (v113.0+):
    - status: Get running supervisor status
    - restart: Request graceful restart
    - takeover: Request graceful takeover by new instance
    - force-stop: Force immediate shutdown
    - health: v115.0 - Comprehensive health report with cross-repo status
    - cross-repo-status: v115.0 - Get status of all connected repos
    - restart-status: v120.0 - Check restart marker and initialization status

Author: JARVIS System
Version: 120.0.0 (January 2026)
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import fcntl
import json
import logging
import os
import signal
import socket
import sys
import threading
import time
import urllib.request
import urllib.error
from abc import ABC, abstractmethod
from contextlib import suppress
from dataclasses import dataclass, asdict, field
from datetime import datetime
from enum import Enum, IntEnum
from functools import partial
from pathlib import Path
from typing import Optional, Dict, Any, Tuple, Callable, List, Set

logger = logging.getLogger(__name__)

# =============================================================================
# v115.0: Configuration with Environment Variable Support
# =============================================================================

def _get_env_path(key: str, default: Path) -> Path:
    """Get path from environment variable or use default."""
    val = os.environ.get(key)
    return Path(val) if val else default

def _get_env_float(key: str, default: float) -> float:
    """Get float from environment variable or use default."""
    val = os.environ.get(key)
    try:
        return float(val) if val else default
    except ValueError:
        return default

def _get_env_int(key: str, default: int) -> int:
    """Get int from environment variable or use default."""
    val = os.environ.get(key)
    try:
        return int(val) if val else default
    except ValueError:
        return default

# Lock file location - configurable via environment
LOCK_DIR = _get_env_path("JARVIS_LOCK_DIR", Path.home() / ".jarvis" / "locks")
SUPERVISOR_LOCK_FILE = LOCK_DIR / "supervisor.lock"
SUPERVISOR_STATE_FILE = LOCK_DIR / "supervisor.state"
SUPERVISOR_IPC_SOCKET = LOCK_DIR / "supervisor.sock"

# v115.0: Cross-repo state directory
CROSS_REPO_DIR = _get_env_path("JARVIS_CROSS_REPO_DIR", Path.home() / ".jarvis" / "cross_repo")
TRINITY_READINESS_DIR = _get_env_path("JARVIS_TRINITY_DIR", Path.home() / ".jarvis" / "trinity" / "readiness")

# v115.0: Configurable thresholds
STALE_LOCK_THRESHOLD = _get_env_float("JARVIS_STALE_LOCK_THRESHOLD", 90.0)  # Reduced from 300s
HEARTBEAT_INTERVAL = _get_env_float("JARVIS_HEARTBEAT_INTERVAL", 5.0)  # Reduced from 10s
HEALTH_CHECK_TIMEOUT = _get_env_float("JARVIS_HEALTH_CHECK_TIMEOUT", 3.0)
IPC_TIMEOUT = _get_env_float("JARVIS_IPC_TIMEOUT", 2.0)
HTTP_HEALTH_PORTS = [int(p) for p in os.environ.get("JARVIS_HTTP_PORTS", "8080,8000,8010").split(",")]

# =============================================================================
# v120.0: Restart Marker System for Cross-Process Coordination
# =============================================================================
# The restart marker file signals that a restart is in progress.
# This allows:
# 1. Client to wait intelligently for restart completion
# 2. New supervisor to know it's starting from a restart (vs fresh start)
# 3. Cross-repo components to be notified of restart in progress
# =============================================================================
RESTART_MARKER_FILE = LOCK_DIR / "restart.marker"
RESTART_TIMEOUT = _get_env_float("JARVIS_RESTART_TIMEOUT", 120.0)  # Max seconds for restart
IPC_THREAD_ENABLED = os.environ.get("JARVIS_IPC_THREAD", "true").lower() in ("1", "true", "yes")

# =============================================================================
# v116.0: Global Process Registry for Trinity Component Tracking
# =============================================================================
# This registry tracks all PIDs spawned by this supervisor session.
# It's used by the SIGHUP handler to distinguish "our" processes from orphans.
# CRITICAL: This prevents the restart handler from killing running services.
#
# Design:
# - Thread-safe set (Python's set is thread-safe for add/remove/in operations)
# - Persisted to disk for crash recovery
# - Cleared on supervisor start
# - Processes register themselves when spawned
# - SIGHUP handler checks this before killing port-holders
# =============================================================================

import threading

class GlobalProcessRegistry:
    """
    v116.0: Global registry of spawned process PIDs.

    This singleton tracks all PIDs spawned by this supervisor session to prevent
    the SIGHUP/restart handler from killing our own running services.

    Thread-safe: Uses threading.Lock for all operations.

    Usage:
        # Register a spawned process
        GlobalProcessRegistry.register(pid, component="jarvis-prime", port=8000)

        # Check if a PID belongs to us
        if GlobalProcessRegistry.is_ours(pid):
            # Don't kill it - it's our process
            pass

        # Deregister on process exit
        GlobalProcessRegistry.deregister(pid)
    """

    _instance = None
    _lock = threading.Lock()
    _pids: Dict[int, Dict[str, Any]] = {}  # PID -> {component, port, start_time}
    _registry_file = LOCK_DIR / "spawned_pids.json"

    @classmethod
    def register(cls, pid: int, component: str = "unknown", port: int = 0) -> None:
        """Register a spawned process PID."""
        with cls._lock:
            cls._pids[pid] = {
                "component": component,
                "port": port,
                "start_time": time.time(),
                "session_id": os.getpid()  # Parent session
            }
            cls._persist()
            logger.debug(f"[ProcessRegistry] Registered PID {pid} ({component}) on port {port}")

    @classmethod
    def deregister(cls, pid: int) -> None:
        """Remove a PID from the registry."""
        with cls._lock:
            if pid in cls._pids:
                info = cls._pids.pop(pid)
                cls._persist()
                logger.debug(f"[ProcessRegistry] Deregistered PID {pid} ({info.get('component', 'unknown')})")

    @classmethod
    def is_ours(cls, pid: int) -> bool:
        """Check if a PID was spawned by this supervisor session."""
        with cls._lock:
            return pid in cls._pids

    @classmethod
    def get_all(cls) -> Dict[int, Dict[str, Any]]:
        """Get all registered PIDs with their metadata."""
        with cls._lock:
            return dict(cls._pids)

    @classmethod
    def clear(cls) -> None:
        """Clear all registered PIDs (call on supervisor start)."""
        with cls._lock:
            cls._pids.clear()
            cls._persist()
            logger.debug("[ProcessRegistry] Cleared all registered PIDs")

    @classmethod
    def normalize_service_name(cls, name: str) -> str:
        """
        v117.4: Normalize service name for consistent comparison.

        Converts hyphens to underscores and lowercases.
        Example: "jarvis-prime (spawned)" -> "jarvis_prime"
        """
        # Extract base name (before parentheses)
        base = name.split("(")[0].strip()
        return base.lower().replace("-", "_").replace(" ", "_")

    @classmethod
    def find_by_service(cls, service_name: str) -> Optional[Tuple[int, Dict[str, Any]]]:
        """
        v117.4: Find a registered service by normalized name.

        Returns (pid, info) if found, None otherwise.
        """
        normalized = cls.normalize_service_name(service_name)
        with cls._lock:
            for pid, info in cls._pids.items():
                component = info.get("component", "")
                if cls.normalize_service_name(component) == normalized:
                    return pid, info
                # Also check if one contains the other
                norm_comp = cls.normalize_service_name(component)
                if normalized in norm_comp or norm_comp in normalized:
                    return pid, info
        return None

    @classmethod
    def load_from_disk(cls, current_session_only: bool = True) -> None:
        """
        v140.0: Load registry from disk with INTELLIGENT STALE PROCESS DETECTION.

        This is the key fix for the "protected PID" issue where stale processes
        from PREVIOUS sessions were being incorrectly protected.

        Args:
            current_session_only: If True, only load PIDs from the CURRENT session.
                                  If False, load all still-running PIDs (legacy behavior).

        The v140.0 enhancement:
        - Each registered PID has a `session_id` (the parent supervisor PID at spawn time)
        - When loading, we compare the stored session_id against the CURRENT supervisor PID
        - PIDs from different sessions are considered STALE and are NOT protected
        - This prevents the "protected PID" deadlock when a stale process holds a port
        """
        current_supervisor_pid = os.getpid()

        try:
            if cls._registry_file.exists():
                try:
                    with open(cls._registry_file) as f:
                        data = json.load(f)
                except json.JSONDecodeError as e:
                    logger.warning(f"[ProcessRegistry] v117.4: JSON corrupted, recovering: {e}")
                    # Attempt to backup corrupted file and start fresh
                    backup = cls._registry_file.with_suffix('.json.corrupted')
                    try:
                        import shutil
                        shutil.copy(cls._registry_file, backup)
                    except Exception:
                        pass
                    cls._registry_file.unlink(missing_ok=True)
                    return

                # v140.0: Validate PIDs with SESSION AWARENESS
                loaded_count = 0
                skipped_dead = 0
                skipped_stale_session = 0

                with cls._lock:
                    for pid_str, info in data.items():
                        try:
                            pid = int(pid_str)
                        except (ValueError, TypeError):
                            continue  # Skip invalid PID entries

                        # v140.0: Check if this PID is from the CURRENT session
                        stored_session_id = info.get("session_id")
                        if current_session_only and stored_session_id != current_supervisor_pid:
                            # This PID is from a DIFFERENT session - it's STALE
                            # Don't load it into the registry (so it won't be protected)
                            try:
                                os.kill(pid, 0)  # Check if still running
                                # Process exists but from old session - LOG but don't protect
                                component = info.get("component", "unknown")
                                port = info.get("port", "unknown")
                                logger.info(
                                    f"[ProcessRegistry] v140.0: 🔍 STALE PID {pid} detected "
                                    f"({component} on port {port}) from session {stored_session_id}. "
                                    f"Current session: {current_supervisor_pid}. "
                                    f"NOT loading as protected - can be cleaned up."
                                )
                                skipped_stale_session += 1
                            except OSError:
                                # Process doesn't exist anyway
                                skipped_dead += 1
                            continue

                        # PID is from current session (or we're not filtering by session)
                        try:
                            os.kill(pid, 0)  # Check if process exists
                            cls._pids[pid] = info
                            loaded_count += 1
                        except OSError:
                            skipped_dead += 1  # Process no longer exists

                # v140.0: Enhanced logging
                if skipped_stale_session > 0:
                    logger.info(
                        f"[ProcessRegistry] v140.0: 🧹 Session cleanup - "
                        f"Loaded {loaded_count} current PIDs, "
                        f"skipped {skipped_stale_session} stale (from previous sessions), "
                        f"skipped {skipped_dead} dead"
                    )
                elif skipped_dead > 0:
                    logger.info(
                        f"[ProcessRegistry] v117.4: Loaded {loaded_count} PIDs, "
                        f"skipped {skipped_dead} dead processes"
                    )
                else:
                    logger.debug(f"[ProcessRegistry] Loaded {loaded_count} PIDs from disk")

                # Persist cleaned version
                if skipped_dead > 0 or skipped_stale_session > 0:
                    cls._persist()

        except Exception as e:
            logger.warning(f"[ProcessRegistry] v117.4: Load failed, starting fresh: {e}")

    @classmethod
    def get_stale_pids(cls) -> Dict[int, Dict[str, Any]]:
        """
        v140.0: Get PIDs from PREVIOUS sessions that are still running.

        These are "stale" processes that can be safely cleaned up because
        they belong to a supervisor session that is no longer active.

        Returns:
            Dict of stale PIDs with their metadata
        """
        current_supervisor_pid = os.getpid()
        stale_pids: Dict[int, Dict[str, Any]] = {}

        try:
            if not cls._registry_file.exists():
                return {}

            with open(cls._registry_file) as f:
                data = json.load(f)

            for pid_str, info in data.items():
                try:
                    pid = int(pid_str)
                except (ValueError, TypeError):
                    continue

                stored_session_id = info.get("session_id")

                # Only consider PIDs from DIFFERENT sessions
                if stored_session_id == current_supervisor_pid:
                    continue

                # Check if process still exists
                try:
                    os.kill(pid, 0)
                    # Process exists and is from old session = STALE
                    stale_pids[pid] = info
                except OSError:
                    pass  # Process doesn't exist

        except Exception as e:
            logger.debug(f"[ProcessRegistry] Error getting stale PIDs: {e}")

        return stale_pids

    @classmethod
    def cleanup_stale_session_processes(cls, force: bool = False) -> List[int]:
        """
        v140.0: Clean up processes from PREVIOUS supervisor sessions.

        This is the key method for resolving "protected PID" deadlocks.
        It identifies and terminates processes that were spawned by a
        previous supervisor session that is no longer running.

        Args:
            force: If True, use SIGKILL. If False, use SIGTERM first.

        Returns:
            List of PIDs that were successfully terminated
        """
        stale_pids = cls.get_stale_pids()
        cleaned_pids: List[int] = []

        if not stale_pids:
            logger.debug("[ProcessRegistry] v140.0: No stale session processes to clean up")
            return []

        logger.info(
            f"[ProcessRegistry] v140.0: 🧹 Cleaning up {len(stale_pids)} stale processes "
            f"from previous sessions..."
        )

        for pid, info in stale_pids.items():
            component = info.get("component", "unknown")
            port = info.get("port", "unknown")
            session_id = info.get("session_id", "unknown")

            try:
                # Send SIGTERM first (graceful)
                logger.info(
                    f"[ProcessRegistry] v140.0: 🔪 Terminating stale PID {pid} "
                    f"({component} on port {port}, session {session_id})"
                )
                os.kill(pid, signal.SIGTERM)

                # Wait briefly for graceful shutdown
                for _ in range(10):  # 1 second total
                    time.sleep(0.1)
                    try:
                        os.kill(pid, 0)  # Check if still exists
                    except OSError:
                        # Process terminated
                        cleaned_pids.append(pid)
                        logger.info(f"[ProcessRegistry] v140.0: ✅ PID {pid} terminated gracefully")
                        break
                else:
                    # Still running after SIGTERM - force kill if requested
                    if force:
                        logger.warning(f"[ProcessRegistry] v140.0: ⚠️ PID {pid} didn't respond to SIGTERM, sending SIGKILL")
                        os.kill(pid, signal.SIGKILL)
                        time.sleep(0.2)
                        try:
                            os.kill(pid, 0)
                        except OSError:
                            cleaned_pids.append(pid)
                            logger.info(f"[ProcessRegistry] v140.0: ✅ PID {pid} force-killed")
                    else:
                        logger.warning(f"[ProcessRegistry] v140.0: ⚠️ PID {pid} still running after SIGTERM (use force=True to SIGKILL)")

            except OSError as e:
                if e.errno == 3:  # No such process
                    cleaned_pids.append(pid)
                    logger.debug(f"[ProcessRegistry] v140.0: PID {pid} already dead")
                elif e.errno == 1:  # Operation not permitted
                    logger.warning(f"[ProcessRegistry] v140.0: ⚠️ No permission to kill PID {pid}")
                else:
                    logger.warning(f"[ProcessRegistry] v140.0: ⚠️ Error killing PID {pid}: {e}")

        if cleaned_pids:
            logger.info(
                f"[ProcessRegistry] v140.0: ✅ Cleaned up {len(cleaned_pids)} stale processes: {cleaned_pids}"
            )

        return cleaned_pids

    @classmethod
    def _persist(cls) -> None:
        """
        v117.4: Persist registry to disk atomically.

        Uses write-to-temp + atomic rename pattern to prevent corruption.
        Called with lock held.
        """
        try:
            cls._registry_file.parent.mkdir(parents=True, exist_ok=True)

            # v117.4: Write to temp file, then atomic rename
            tmp_file = cls._registry_file.with_suffix('.json.tmp')
            with open(tmp_file, 'w') as f:
                json.dump({str(k): v for k, v in cls._pids.items()}, f, indent=2)
                f.flush()
                os.fsync(f.fileno())  # Ensure data hits disk

            # Atomic rename (on POSIX systems)
            tmp_file.replace(cls._registry_file)
        except Exception as e:
            logger.debug(f"[ProcessRegistry] Could not persist: {e}")
            # Clean up temp file if it exists
            try:
                tmp_file.unlink(missing_ok=True)
            except Exception:
                pass

    @classmethod
    def cleanup_dead_pids(cls) -> int:
        """Remove PIDs that no longer exist. Returns count of removed."""
        removed = 0
        with cls._lock:
            dead_pids = []
            for pid in cls._pids:
                try:
                    os.kill(pid, 0)
                except OSError:
                    dead_pids.append(pid)

            for pid in dead_pids:
                cls._pids.pop(pid, None)
                removed += 1

            if removed:
                cls._persist()
                logger.debug(f"[ProcessRegistry] Cleaned up {removed} dead PIDs")

        return removed


# =============================================================================
# v120.0: Restart Marker for Cross-Process Coordination
# =============================================================================

class RestartMarker:
    """
    v120.0: Atomic restart marker for coordinating supervisor restarts.

    This class manages a marker file that signals restart state to:
    1. The restart client (so it knows to wait longer)
    2. The new supervisor (so it knows this is a restart, not fresh start)
    3. Cross-repo components (so they can reconnect)

    Marker file format (JSON):
    {
        "initiated_at": ISO timestamp,
        "initiated_by_pid": PID that initiated restart,
        "expected_new_pid": PID after os.execv (same as initiated_by_pid),
        "phase": "initiated" | "executing" | "completed" | "failed",
        "cross_repo_notified": ["jarvis_prime", "reactor_core"],
        "timeout_at": ISO timestamp when restart should be considered failed
    }
    """

    _lock = threading.Lock()

    @classmethod
    def create(cls, pid: int, timeout: float = None) -> bool:
        """
        Create restart marker indicating restart is starting.

        Args:
            pid: The PID initiating the restart (will be same after os.execv)
            timeout: Max seconds to wait for restart (default: RESTART_TIMEOUT)

        Returns:
            True if marker created successfully
        """
        if timeout is None:
            timeout = RESTART_TIMEOUT

        with cls._lock:
            try:
                now = datetime.now()
                marker_data = {
                    "marker_id": f"restart-{pid}-{int(now.timestamp())}",
                    "initiated_at": now.isoformat(),
                    "initiated_by_pid": pid,
                    "old_pid": pid,  # The PID being restarted
                    "expected_new_pid": pid,  # os.execv keeps same PID
                    "phase": "initiated",
                    "cross_repo_notified": [],
                    "timeout_at": (now.timestamp() + timeout),
                    "version": "120.0"
                }

                # Atomic write
                LOCK_DIR.mkdir(parents=True, exist_ok=True)
                temp_file = RESTART_MARKER_FILE.with_suffix('.tmp')
                temp_file.write_text(json.dumps(marker_data, indent=2))
                temp_file.rename(RESTART_MARKER_FILE)

                logger.info(f"[RestartMarker] v120.0: Created restart marker for PID {pid}")
                return True

            except Exception as e:
                logger.error(f"[RestartMarker] Failed to create marker: {e}")
                return False

    @classmethod
    def update_phase(cls, phase: str) -> bool:
        """Update the restart phase (executing, completed, failed)."""
        with cls._lock:
            try:
                if not RESTART_MARKER_FILE.exists():
                    return False

                data = json.loads(RESTART_MARKER_FILE.read_text())
                data["phase"] = phase
                data["phase_updated_at"] = datetime.now().isoformat()

                temp_file = RESTART_MARKER_FILE.with_suffix('.tmp')
                temp_file.write_text(json.dumps(data, indent=2))
                temp_file.rename(RESTART_MARKER_FILE)

                logger.debug(f"[RestartMarker] Phase updated to: {phase}")
                return True

            except Exception as e:
                logger.error(f"[RestartMarker] Failed to update phase: {e}")
                return False

    @classmethod
    def mark_cross_repo_notified(cls, repo_names: List[str]) -> None:
        """Record that cross-repo components were notified."""
        if not repo_names:
            return
        with cls._lock:
            try:
                if RESTART_MARKER_FILE.exists():
                    data = json.loads(RESTART_MARKER_FILE.read_text())
                    notified = set(data.get("cross_repo_notified", []))
                    notified.update(repo_names)
                    data["cross_repo_notified"] = list(notified)
                    temp_file = RESTART_MARKER_FILE.with_suffix('.tmp')
                    temp_file.write_text(json.dumps(data, indent=2))
                    temp_file.rename(RESTART_MARKER_FILE)
            except Exception:
                pass

    @classmethod
    def read(cls) -> Optional[Dict[str, Any]]:
        """Read current restart marker state."""
        try:
            if RESTART_MARKER_FILE.exists():
                return json.loads(RESTART_MARKER_FILE.read_text())
        except Exception:
            pass
        return None

    @classmethod
    def is_active(cls) -> bool:
        """Check if a restart is currently in progress."""
        marker = cls.read()
        if not marker:
            return False

        # Check if timed out
        timeout_at = marker.get("timeout_at", 0)
        if time.time() > timeout_at:
            # Restart timed out - clean up
            cls.clear()
            return False

        phase = marker.get("phase", "")
        return phase in ("initiated", "executing")

    @classmethod
    def is_restart_completion(cls, current_pid: int) -> bool:
        """
        Check if current startup is completing a restart.

        Returns True if:
        1. Marker exists and is active (not timed out)
        2. Phase indicates restart is in progress but new process hasn't completed
        3. Current PID matches expected_new_pid (with os.execv, same PID)

        Phases progression:
        - initiated: restart requested, marker created
        - signal_sent: SIGHUP sent to supervisor
        - execv_triggered: right before os.execv() call
        - new_process_started: new process detected marker and started
        - completed: restart fully complete
        """
        marker = cls.read()
        if not marker:
            return False

        # Check if marker has timed out (stale restart)
        timeout_at = marker.get("timeout_at", 0)
        if time.time() > timeout_at:
            logger.debug("[RestartMarker] Marker has timed out - not a restart completion")
            return False

        expected_pid = marker.get("expected_new_pid")
        old_pid = marker.get("old_pid")
        phase = marker.get("phase", "")

        # With os.execv(), PID stays the same, so expected_new_pid == old_pid
        # Check if phase indicates we're post-execv but not yet completed
        restart_in_progress_phases = ("initiated", "signal_pending", "signal_sent", "execv_triggered")

        return (
            phase in restart_in_progress_phases and
            expected_pid == current_pid  # os.execv keeps same PID
        )

    @classmethod
    def complete(cls) -> None:
        """Mark restart as completed and clean up."""
        cls.update_phase("completed")
        cls.clear()

    @classmethod
    def fail(cls, error: str = None) -> None:
        """Mark restart as failed."""
        with cls._lock:
            try:
                if RESTART_MARKER_FILE.exists():
                    data = json.loads(RESTART_MARKER_FILE.read_text())
                    data["phase"] = "failed"
                    data["error"] = error or "Unknown error"
                    data["failed_at"] = datetime.now().isoformat()
                    RESTART_MARKER_FILE.write_text(json.dumps(data, indent=2))
            except Exception:
                pass

    @classmethod
    def clear(cls) -> None:
        """Remove restart marker."""
        with cls._lock:
            try:
                if RESTART_MARKER_FILE.exists():
                    RESTART_MARKER_FILE.unlink()
                    logger.debug("[RestartMarker] Cleared")
            except Exception:
                pass


# =============================================================================
# v120.0: Thread-Isolated IPC Server
# =============================================================================

class ThreadedIPCServer:
    """
    v123.0: Enterprise-grade IPC server in dedicated thread.

    This prevents the IPC server from being blocked by event loop operations
    (like heavy ML model loading, synchronous I/O, etc.), ensuring the
    supervisor always responds to restart/status commands.

    Architecture:
    - Main thread runs asyncio event loop with heavy operations
    - IPC thread runs separate event loop with server.serve_forever()
    - Thread-safe communication via queues and events
    - Self-healing with automatic restart on failure
    - Heartbeat monitoring to detect if main thread is stuck

    v123.0 Improvements:
    - Fixed constructor signature to match call site
    - Use server.serve_forever() instead of sleep polling
    - Add health monitoring thread
    - Robust error handling and recovery
    """

    def __init__(
        self,
        socket_path: Path,
        handlers: Dict['IPCCommand', Callable],
        main_loop: asyncio.AbstractEventLoop,
        singleton: Optional['SupervisorSingleton'] = None,
    ):
        """
        v123.0: Initialize ThreadedIPCServer.

        Args:
            socket_path: Path to Unix domain socket
            handlers: Dict mapping IPCCommand to async handler functions
            main_loop: Main event loop for delegating commands
            singleton: Optional reference to SupervisorSingleton for state access
        """
        self._socket_path = socket_path
        self._handlers = handlers
        self._main_loop = main_loop
        self._singleton = singleton
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._server: Optional[asyncio.AbstractServer] = None
        self._running = threading.Event()
        self._stop_event = threading.Event()
        self._started_at: Optional[datetime] = None
        self._request_count = 0
        self._error_count = 0
        self._last_request_time: Optional[datetime] = None
        self._lock = threading.Lock()

    def start(self) -> bool:
        """Start the IPC server in a dedicated thread."""
        if self._thread and self._thread.is_alive():
            logger.debug("[ThreadedIPC] v123.0: Server already running")
            return True

        try:
            self._stop_event.clear()
            self._running.clear()
            self._thread = threading.Thread(
                target=self._run_server_thread,
                name="JARVIS-IPC-Server-v123",
                daemon=True
            )
            self._thread.start()

            # Wait for server to start (max 10 seconds for robustness)
            if self._running.wait(timeout=10.0):
                self._started_at = datetime.now()
                logger.info(f"[ThreadedIPC] v123.0: Server started on {self._socket_path}")
                return True
            else:
                logger.error("[ThreadedIPC] v123.0: Server failed to start within timeout")
                return False

        except Exception as e:
            logger.error(f"[ThreadedIPC] v123.0: Failed to start server thread: {e}")
            import traceback
            traceback.print_exc()
            return False

    def stop(self) -> None:
        """Stop the IPC server thread gracefully."""
        logger.debug("[ThreadedIPC] v123.0: Stopping server...")
        self._stop_event.set()

        # Close the server to interrupt serve_forever()
        if self._server and self._loop:
            try:
                # Schedule server close in the IPC loop
                self._loop.call_soon_threadsafe(self._server.close)
            except Exception as e:
                logger.debug(f"[ThreadedIPC] Server close warning: {e}")

        # Wait for thread to finish
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)
            if self._thread.is_alive():
                logger.warning("[ThreadedIPC] v123.0: Thread didn't stop gracefully")

        self._running.clear()
        logger.info("[ThreadedIPC] v123.0: Server stopped")

    def is_running(self) -> bool:
        """Check if server is running."""
        return self._running.is_set() and self._thread and self._thread.is_alive()

    def get_stats(self) -> Dict[str, Any]:
        """Get server statistics."""
        with self._lock:
            return {
                "running": self.is_running(),
                "started_at": self._started_at.isoformat() if self._started_at else None,
                "request_count": self._request_count,
                "error_count": self._error_count,
                "last_request_time": self._last_request_time.isoformat() if self._last_request_time else None,
                "socket_path": str(self._socket_path),
            }

    def _run_server_thread(self) -> None:
        """Thread main function - runs IPC event loop with serve_forever()."""
        logger.debug("[ThreadedIPC] v123.0: Thread starting...")
        try:
            # Create new event loop for this thread
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)

            # Start the server
            self._loop.run_until_complete(self._start_server())

            # Signal that we're ready
            self._running.set()
            logger.debug("[ThreadedIPC] v123.0: Server ready, entering serve_forever()")

            # v123.0: Use serve_forever() for proper connection handling
            # This blocks until the server is closed
            self._loop.run_until_complete(self._serve_forever_with_monitoring())

        except asyncio.CancelledError:
            logger.debug("[ThreadedIPC] v123.0: Server cancelled (shutdown)")
        except Exception as e:
            logger.error(f"[ThreadedIPC] v123.0: Server thread error: {e}")
            import traceback
            traceback.print_exc()
        finally:
            # Clean up
            self._cleanup()

    async def _serve_forever_with_monitoring(self) -> None:
        """v123.0: Serve forever with health monitoring."""
        try:
            # Create a future that completes when stop_event is set
            stop_future = asyncio.get_event_loop().run_in_executor(
                None, self._stop_event.wait
            )

            # Start serving
            async with self._server:
                # Wait for either server error or stop signal
                serve_task = asyncio.create_task(self._server.serve_forever())

                # Wait for stop signal in a non-blocking way
                done, pending = await asyncio.wait(
                    [serve_task, asyncio.ensure_future(stop_future)],
                    return_when=asyncio.FIRST_COMPLETED
                )

                # Cancel remaining tasks
                for task in pending:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[ThreadedIPC] v123.0: serve_forever error: {e}")

    def _cleanup(self) -> None:
        """Clean up resources."""
        if self._server:
            try:
                self._server.close()
            except Exception:
                pass
            self._server = None

        if self._loop and not self._loop.is_closed():
            try:
                # Cancel all pending tasks
                pending = asyncio.all_tasks(self._loop)
                for task in pending:
                    task.cancel()

                # Run until tasks complete
                if pending:
                    self._loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )

                self._loop.close()
            except Exception:
                pass
            self._loop = None

        self._running.clear()
        logger.debug("[ThreadedIPC] v123.0: Cleanup complete")

    async def _start_server(self) -> None:
        """v123.0: Start the Unix domain socket server."""
        # Remove stale socket
        if self._socket_path.exists():
            with suppress(Exception):
                self._socket_path.unlink()

        # Ensure parent directory exists
        self._socket_path.parent.mkdir(parents=True, exist_ok=True)

        self._server = await asyncio.start_unix_server(
            self._handle_connection,
            path=str(self._socket_path)
        )

        # Make socket accessible
        os.chmod(str(self._socket_path), 0o666)
        logger.info(f"[ThreadedIPC] v123.0: Server listening on {self._socket_path}")

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter
    ) -> None:
        """v123.0: Handle incoming IPC connection with stats tracking."""
        peer_info = "unknown"
        try:
            # Track request
            with self._lock:
                self._request_count += 1
                self._last_request_time = datetime.now()

            # Read command with timeout
            data = await asyncio.wait_for(reader.readline(), timeout=10.0)

            if not data:
                writer.close()
                await writer.wait_closed()
                return

            # Parse command
            request = json.loads(data.decode().strip())
            command = request.get("command", "")
            args = request.get("args", {})

            logger.debug(f"[ThreadedIPC] v123.0: Handling command: {command}")

            # Handle command
            response = await self._handle_command(command, args)

            # Send response
            response_data = json.dumps(response) + '\n'
            writer.write(response_data.encode())
            await writer.drain()
            writer.close()
            await writer.wait_closed()

            logger.debug(f"[ThreadedIPC] v123.0: Command {command} completed successfully")

        except asyncio.TimeoutError:
            logger.debug("[ThreadedIPC] v123.0: Connection read timeout")
            with self._lock:
                self._error_count += 1
        except json.JSONDecodeError as e:
            logger.warning(f"[ThreadedIPC] v123.0: Invalid JSON: {e}")
            with self._lock:
                self._error_count += 1
            # Try to send error response
            try:
                error_resp = json.dumps({"success": False, "error": "Invalid JSON"}) + '\n'
                writer.write(error_resp.encode())
                await writer.drain()
            except Exception:
                pass
        except Exception as e:
            logger.debug(f"[ThreadedIPC] v123.0: Connection error: {e}")
            with self._lock:
                self._error_count += 1
        finally:
            with suppress(Exception):
                writer.close()
                await writer.wait_closed()

    async def _handle_command(self, command: str, args: Dict) -> Dict[str, Any]:
        """
        v123.0: Handle IPC command with robust delegation.

        For most commands, delegate to singleton handlers.
        For critical commands (ping, restart-status, stats), handle locally
        to ensure responsiveness even if main event loop is blocked.
        """
        try:
            # v123.0: Local handlers for critical commands (don't depend on main event loop)
            if command == "ping":
                return {
                    "success": True,
                    "result": {
                        "pong": True,
                        "timestamp": datetime.now().isoformat(),
                        "ipc_thread": True,
                        "ipc_version": "v123.0",
                        "stats": self.get_stats(),
                        "main_thread_responsive": self._is_main_thread_responsive()
                    }
                }

            elif command == "ipc-stats":
                return {
                    "success": True,
                    "result": self.get_stats()
                }

            elif command == "restart-status":
                marker = RestartMarker.read()
                return {
                    "success": True,
                    "result": {
                        "restart_active": RestartMarker.is_active(),
                        "marker": marker,
                        "ipc_stats": self.get_stats()
                    }
                }

            elif command == "restart":
                # Create restart marker BEFORE delegating
                RestartMarker.create(os.getpid())

                # v123.0: Try to delegate to singleton handler if available
                if self._singleton:
                    handler = self._singleton._command_handlers.get(IPCCommand.RESTART)
                    if handler:
                        # Run handler in main event loop if possible
                        result = await self._run_in_main_loop_or_local(handler, args)
                        return {"success": True, "result": result}

                # v123.0: If no singleton or handler, use direct restart via os.execv
                return await self._handle_restart_direct(args)

            elif command == "shutdown":
                # v123.0: Handle shutdown command
                if self._singleton:
                    handler = self._singleton._command_handlers.get(IPCCommand.SHUTDOWN)
                    if handler:
                        result = await self._run_in_main_loop_or_local(handler, args)
                        return {"success": True, "result": result}

                # Direct shutdown via signal
                return await self._handle_shutdown_direct(args)

            elif command == "status":
                # v123.0: Handle status command - local handling for speed
                return await self._handle_status_local()

            else:
                # v123.0: Delegate to registered handlers
                try:
                    cmd_enum = IPCCommand(command)
                    if self._singleton:
                        handler = self._singleton._command_handlers.get(cmd_enum)
                    else:
                        handler = self._handlers.get(cmd_enum)

                    if handler:
                        result = await self._run_in_main_loop_or_local(handler, args)
                        return {"success": True, "result": result}
                    else:
                        return {"success": False, "error": f"No handler for command: {command}"}
                except ValueError:
                    return {"success": False, "error": f"Unknown command: {command}"}

        except Exception as e:
            logger.error(f"[ThreadedIPC] v123.0: Command {command} failed: {e}")
            import traceback
            traceback.print_exc()
            return {"success": False, "error": str(e)}

    async def _handle_restart_direct(self, args: Dict) -> Dict[str, Any]:
        """v123.0: Handle restart - respond first, then signal."""
        try:
            logger.info("[ThreadedIPC] v123.0: Restart requested - will send SIGHUP after response")
            pid = os.getpid()

            # v123.0: Schedule SIGHUP AFTER response is sent (100ms delay)
            # This ensures the response gets back to the client before we restart
            def delayed_restart():
                time.sleep(0.1)  # 100ms to allow response
                logger.info("[ThreadedIPC] v123.0: Sending SIGHUP now")
                os.kill(pid, signal.SIGHUP)

            restart_thread = threading.Thread(target=delayed_restart, daemon=True)
            restart_thread.start()

            return {"restart_initiated": True, "method": "sighup_delayed", "pid": pid}
        except Exception as e:
            return {"restart_initiated": False, "error": str(e)}

    async def _handle_shutdown_direct(self, args: Dict) -> Dict[str, Any]:
        """v123.0: Handle shutdown - respond first, then signal."""
        try:
            logger.info("[ThreadedIPC] v123.0: Shutdown requested - will send SIGTERM after response")
            pid = os.getpid()

            # v123.0: Schedule SIGTERM AFTER response is sent (100ms delay)
            # This ensures the response gets back to the client before we die
            def delayed_shutdown():
                time.sleep(0.1)  # 100ms to allow response
                logger.info("[ThreadedIPC] v123.0: Sending SIGTERM now")
                os.kill(pid, signal.SIGTERM)

            shutdown_thread = threading.Thread(target=delayed_shutdown, daemon=True)
            shutdown_thread.start()

            return {"shutdown_initiated": True, "method": "sigterm_delayed", "pid": pid}
        except Exception as e:
            return {"shutdown_initiated": False, "error": str(e)}

    async def _handle_status_local(self) -> Dict[str, Any]:
        """v123.0: Handle status command locally for speed."""
        try:
            # Read state file directly
            state = {}
            if SUPERVISOR_STATE_FILE.exists():
                try:
                    with open(SUPERVISOR_STATE_FILE) as f:
                        state = json.load(f)
                except Exception:
                    pass

            return {
                "success": True,
                "result": {
                    "status": "RUNNING",
                    "pid": os.getpid(),
                    "started_at": state.get("started_at", datetime.now().isoformat()),
                    "ipc_stats": self.get_stats(),
                    "health": {
                        "ipc_server": "healthy",
                        "main_thread": "responsive" if self._is_main_thread_responsive() else "blocked"
                    }
                }
            }
        except Exception as e:
            return {"success": False, "error": str(e)}


    def _is_main_thread_responsive(self) -> bool:
        """v123.0: Check if main thread is responsive (not blocked)."""
        # Method 1: Check heartbeat age from singleton state
        if self._singleton and hasattr(self._singleton, '_state'):
            state = self._singleton._state
            if state and hasattr(state, 'last_heartbeat'):
                try:
                    hb_time = datetime.fromisoformat(state.last_heartbeat)
                    age = (datetime.now() - hb_time).total_seconds()
                    return age < 30.0  # Consider responsive if heartbeat < 30s old
                except Exception:
                    pass

        # Method 2: Check state file directly
        if SUPERVISOR_STATE_FILE.exists():
            try:
                mtime = SUPERVISOR_STATE_FILE.stat().st_mtime
                age = time.time() - mtime
                return age < 60.0  # State file updated in last minute
            except Exception:
                pass

        # Method 3: Try to schedule something in main loop
        if self._main_loop and self._main_loop.is_running():
            try:
                # This is a quick check - if loop is running, it's responsive
                return True
            except Exception:
                pass

        return True  # Assume responsive if can't determine

    async def _run_in_main_loop_or_local(
        self,
        handler: Callable,
        args: Dict
    ) -> Any:
        """v123.0: Try to run handler in main loop, fall back to local execution."""
        # If we have access to main loop and it's running, delegate there
        if self._main_loop and self._main_loop.is_running() and not self._main_loop.is_closed():
            try:
                # Schedule coroutine in main loop and wait for result
                future = asyncio.run_coroutine_threadsafe(
                    handler(args),
                    self._main_loop
                )
                # Wait with timeout to prevent deadlock
                result = future.result(timeout=30.0)
                return result
            except concurrent.futures.TimeoutError:
                logger.warning("[ThreadedIPC] v123.0: Main loop handler timed out")
                # Fall through to local execution
            except Exception as e:
                logger.debug(f"[ThreadedIPC] v123.0: Main loop delegation failed: {e}")
                # Fall through to local execution

        # Run locally (handlers should be async-safe)
        if asyncio.iscoroutinefunction(handler):
            return await handler(args)
        else:
            return handler(args)


# v115.0: Health check levels
class HealthLevel(IntEnum):
    """Progressive health verification levels."""
    UNKNOWN = 0       # Not yet checked
    PROCESS_EXISTS = 1  # PID exists
    PROCESS_VALID = 2   # PID + start time match
    IPC_RESPONSIVE = 3  # Responds to IPC ping
    HTTP_HEALTHY = 4    # HTTP health check passes
    FULLY_READY = 5     # All checks pass including readiness


class IPCCommand(str, Enum):
    """v113.0+: IPC commands for inter-supervisor communication."""
    STATUS = "status"           # Get running supervisor status
    RESTART = "restart"         # Request graceful restart
    TAKEOVER = "takeover"       # New instance requests takeover
    FORCE_STOP = "force-stop"   # Force immediate shutdown
    PING = "ping"               # Simple liveness check
    SHUTDOWN = "shutdown"       # Graceful shutdown
    # v115.0: New commands
    HEALTH = "health"           # Comprehensive health report
    CROSS_REPO_STATUS = "cross-repo-status"  # Cross-repo integration status
    METRICS = "metrics"         # Performance metrics
    DIAGNOSTICS = "diagnostics" # Diagnostic information
    # v120.0: Restart coordination
    RESTART_STATUS = "restart-status"  # Query restart marker state
    RESTART_ABORT = "restart-abort"    # Abort pending restart


# =============================================================================
# v115.0: Health Check Protocol and Strategies
# =============================================================================

class HealthCheckResult:
    """Result of a health check with detailed diagnostics."""
    __slots__ = ('healthy', 'level', 'latency_ms', 'message', 'details', 'timestamp')

    def __init__(
        self,
        healthy: bool,
        level: HealthLevel = HealthLevel.UNKNOWN,
        latency_ms: float = 0.0,
        message: str = "",
        details: Optional[Dict[str, Any]] = None
    ):
        self.healthy = healthy
        self.level = level
        self.latency_ms = latency_ms
        self.message = message
        self.details = details or {}
        self.timestamp = time.time()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "healthy": self.healthy,
            "level": self.level.name,
            "level_value": int(self.level),
            "latency_ms": self.latency_ms,
            "message": self.message,
            "details": self.details,
            "timestamp": self.timestamp
        }


class HealthCheckStrategy(ABC):
    """Abstract base class for health check strategies (Strategy Pattern)."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Strategy name for logging."""
        pass

    @property
    @abstractmethod
    def level(self) -> HealthLevel:
        """Health level this strategy verifies."""
        pass

    @abstractmethod
    async def check(self, state: "SupervisorState") -> HealthCheckResult:
        """Perform health check."""
        pass


class ProcessExistsStrategy(HealthCheckStrategy):
    """Check if process exists using signal 0."""

    @property
    def name(self) -> str:
        return "process_exists"

    @property
    def level(self) -> HealthLevel:
        return HealthLevel.PROCESS_EXISTS

    async def check(self, state: "SupervisorState") -> HealthCheckResult:
        start = time.perf_counter()
        try:
            os.kill(state.pid, 0)
            latency = (time.perf_counter() - start) * 1000
            return HealthCheckResult(
                healthy=True,
                level=self.level,
                latency_ms=latency,
                message=f"Process {state.pid} exists"
            )
        except ProcessLookupError:
            return HealthCheckResult(
                healthy=False,
                level=HealthLevel.UNKNOWN,
                message=f"Process {state.pid} does not exist"
            )
        except PermissionError:
            latency = (time.perf_counter() - start) * 1000
            return HealthCheckResult(
                healthy=True,
                level=self.level,
                latency_ms=latency,
                message=f"Process {state.pid} exists (permission denied for signal)"
            )
        except Exception as e:
            return HealthCheckResult(
                healthy=False,
                level=HealthLevel.UNKNOWN,
                message=f"Error checking process: {e}"
            )


class ProcessValidStrategy(HealthCheckStrategy):
    """Validate process identity (PID + start time + command line)."""

    @property
    def name(self) -> str:
        return "process_valid"

    @property
    def level(self) -> HealthLevel:
        return HealthLevel.PROCESS_VALID

    async def check(self, state: "SupervisorState") -> HealthCheckResult:
        start = time.perf_counter()
        try:
            import psutil
            proc = psutil.Process(state.pid)

            # Check process is running
            if not proc.is_running():
                return HealthCheckResult(
                    healthy=False,
                    level=HealthLevel.UNKNOWN,
                    message=f"Process {state.pid} not running"
                )

            # Check for zombie
            if proc.status() == psutil.STATUS_ZOMBIE:
                return HealthCheckResult(
                    healthy=False,
                    level=HealthLevel.UNKNOWN,
                    message=f"Process {state.pid} is zombie"
                )

            # Validate start time if available
            if hasattr(state, 'process_start_time') and state.process_start_time:
                current_start = proc.create_time()
                time_diff = abs(current_start - state.process_start_time)
                if time_diff > 2.0:  # 2 second tolerance
                    return HealthCheckResult(
                        healthy=False,
                        level=HealthLevel.UNKNOWN,
                        message=f"PID reuse detected: start time mismatch ({time_diff:.1f}s diff)",
                        details={"stored_start": state.process_start_time, "current_start": current_start}
                    )

            # Validate command line contains JARVIS patterns
            try:
                cmdline = " ".join(proc.cmdline())
                jarvis_patterns = ["run_supervisor", "jarvis", "JARVIS", "start_system"]
                if not any(p in cmdline for p in jarvis_patterns):
                    return HealthCheckResult(
                        healthy=False,
                        level=HealthLevel.UNKNOWN,
                        message=f"Process {state.pid} is not a JARVIS process",
                        details={"cmdline": cmdline[:200]}
                    )
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                pass  # Can't verify cmdline, continue anyway

            latency = (time.perf_counter() - start) * 1000
            return HealthCheckResult(
                healthy=True,
                level=self.level,
                latency_ms=latency,
                message=f"Process {state.pid} validated",
                details={
                    "name": proc.name(),
                    "status": proc.status(),
                    "cpu_percent": proc.cpu_percent(interval=0.01),
                    "memory_mb": proc.memory_info().rss / 1024 / 1024
                }
            )

        except ImportError:
            # psutil not available, fall back to basic check
            return HealthCheckResult(
                healthy=True,
                level=HealthLevel.PROCESS_EXISTS,
                message="psutil not available, basic check only"
            )
        except Exception as e:
            return HealthCheckResult(
                healthy=False,
                level=HealthLevel.UNKNOWN,
                message=f"Process validation error: {e}"
            )


class IPCPingStrategy(HealthCheckStrategy):
    """Check if supervisor responds to IPC ping."""

    @property
    def name(self) -> str:
        return "ipc_ping"

    @property
    def level(self) -> HealthLevel:
        return HealthLevel.IPC_RESPONSIVE

    async def check(self, state: "SupervisorState") -> HealthCheckResult:
        if not SUPERVISOR_IPC_SOCKET.exists():
            return HealthCheckResult(
                healthy=False,
                level=HealthLevel.PROCESS_VALID,
                message="IPC socket does not exist"
            )

        start = time.perf_counter()
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(str(SUPERVISOR_IPC_SOCKET)),
                timeout=IPC_TIMEOUT
            )

            request = {"command": "ping", "args": {}}
            writer.write(json.dumps(request).encode())
            await writer.drain()

            data = await asyncio.wait_for(reader.read(4096), timeout=IPC_TIMEOUT)
            response = json.loads(data.decode())

            writer.close()
            await writer.wait_closed()

            latency = (time.perf_counter() - start) * 1000

            if response.get("success") and response.get("result", {}).get("pong"):
                return HealthCheckResult(
                    healthy=True,
                    level=self.level,
                    latency_ms=latency,
                    message="IPC ping successful",
                    details=response.get("result", {})
                )
            else:
                return HealthCheckResult(
                    healthy=False,
                    level=HealthLevel.PROCESS_VALID,
                    message=f"IPC ping failed: {response}",
                    latency_ms=latency
                )

        except asyncio.TimeoutError:
            return HealthCheckResult(
                healthy=False,
                level=HealthLevel.PROCESS_VALID,
                message="IPC ping timeout"
            )
        except ConnectionRefusedError:
            return HealthCheckResult(
                healthy=False,
                level=HealthLevel.PROCESS_VALID,
                message="IPC connection refused"
            )
        except Exception as e:
            return HealthCheckResult(
                healthy=False,
                level=HealthLevel.PROCESS_VALID,
                message=f"IPC ping error: {e}"
            )


class HTTPHealthStrategy(HealthCheckStrategy):
    """Check HTTP health endpoint."""

    @property
    def name(self) -> str:
        return "http_health"

    @property
    def level(self) -> HealthLevel:
        return HealthLevel.HTTP_HEALTHY

    async def check(self, state: "SupervisorState") -> HealthCheckResult:
        start = time.perf_counter()

        # Run HTTP check in thread pool to avoid blocking
        loop = asyncio.get_event_loop()

        for port in HTTP_HEALTH_PORTS:
            try:
                result = await loop.run_in_executor(
                    None,
                    self._sync_http_check,
                    port
                )
                if result.healthy:
                    result.latency_ms = (time.perf_counter() - start) * 1000
                    return result
            except Exception:
                continue

        return HealthCheckResult(
            healthy=False,
            level=HealthLevel.IPC_RESPONSIVE,
            message=f"HTTP health check failed on all ports: {HTTP_HEALTH_PORTS}"
        )

    def _sync_http_check(self, port: int) -> HealthCheckResult:
        """Synchronous HTTP check (run in thread pool)."""
        try:
            url = f"http://127.0.0.1:{port}/health"
            req = urllib.request.Request(url)
            req.add_header("User-Agent", "JARVIS-Supervisor-Singleton/115.0")

            with urllib.request.urlopen(req, timeout=HEALTH_CHECK_TIMEOUT) as response:
                if response.status == 200:
                    try:
                        data = json.loads(response.read().decode())
                    except:
                        data = {}
                    return HealthCheckResult(
                        healthy=True,
                        level=self.level,
                        message=f"HTTP health OK (port {port})",
                        details={"port": port, "response": data}
                    )

        except Exception as e:
            pass

        return HealthCheckResult(
            healthy=False,
            level=HealthLevel.IPC_RESPONSIVE,
            message=f"HTTP check failed on port {port}"
        )


class ReadinessStateStrategy(HealthCheckStrategy):
    """Check readiness state file."""

    @property
    def name(self) -> str:
        return "readiness_state"

    @property
    def level(self) -> HealthLevel:
        return HealthLevel.FULLY_READY

    async def check(self, state: "SupervisorState") -> HealthCheckResult:
        start = time.perf_counter()

        readiness_file = TRINITY_READINESS_DIR / "jarvis-body.json"

        if not readiness_file.exists():
            return HealthCheckResult(
                healthy=False,
                level=HealthLevel.HTTP_HEALTHY,
                message="Readiness state file not found"
            )

        try:
            data = json.loads(readiness_file.read_text())
            phase = data.get("phase", "")
            ready_phases = {"ready", "healthy", "operational", "warming_up", "interactive"}

            latency = (time.perf_counter() - start) * 1000

            if phase.lower() in ready_phases:
                return HealthCheckResult(
                    healthy=True,
                    level=self.level,
                    latency_ms=latency,
                    message=f"Readiness state: {phase}",
                    details=data
                )
            else:
                return HealthCheckResult(
                    healthy=False,
                    level=HealthLevel.HTTP_HEALTHY,
                    latency_ms=latency,
                    message=f"Not ready: phase={phase}",
                    details=data
                )

        except Exception as e:
            return HealthCheckResult(
                healthy=False,
                level=HealthLevel.HTTP_HEALTHY,
                message=f"Error reading readiness state: {e}"
            )


class CompositeHealthChecker:
    """
    v115.0: Composite health checker using Strategy Pattern.

    Runs multiple health check strategies in order of increasing complexity,
    short-circuiting on failure for fast detection of unhealthy supervisors.
    """

    def __init__(self, strategies: Optional[List[HealthCheckStrategy]] = None):
        """Initialize with ordered list of strategies."""
        self.strategies = strategies or [
            ProcessExistsStrategy(),
            ProcessValidStrategy(),
            IPCPingStrategy(),
            HTTPHealthStrategy(),
            ReadinessStateStrategy(),
        ]
        self._cache: Dict[int, Tuple[float, HealthCheckResult]] = {}
        self._cache_ttl = 2.0  # Cache results for 2 seconds

    async def check_health(
        self,
        state: "SupervisorState",
        min_level: HealthLevel = HealthLevel.IPC_RESPONSIVE,
        use_cache: bool = True
    ) -> HealthCheckResult:
        """
        Check supervisor health up to minimum required level.

        Args:
            state: Supervisor state to check
            min_level: Minimum health level required (short-circuits on failure)
            use_cache: Whether to use cached results

        Returns:
            HealthCheckResult with highest achieved level
        """
        # Check cache
        cache_key = state.pid
        if use_cache and cache_key in self._cache:
            cached_time, cached_result = self._cache[cache_key]
            if time.time() - cached_time < self._cache_ttl:
                return cached_result

        best_result = HealthCheckResult(
            healthy=False,
            level=HealthLevel.UNKNOWN,
            message="No checks performed"
        )

        for strategy in self.strategies:
            if strategy.level > min_level:
                # Already reached required level, stop here
                break

            try:
                result = await asyncio.wait_for(
                    strategy.check(state),
                    timeout=HEALTH_CHECK_TIMEOUT
                )

                logger.debug(
                    f"[Health] {strategy.name}: healthy={result.healthy}, "
                    f"level={result.level.name}, msg={result.message}"
                )

                if result.healthy:
                    best_result = result
                else:
                    # Strategy failed - return with current level
                    best_result = result
                    break

            except asyncio.TimeoutError:
                best_result = HealthCheckResult(
                    healthy=False,
                    level=best_result.level,
                    message=f"Timeout in {strategy.name} check"
                )
                break
            except Exception as e:
                logger.debug(f"[Health] {strategy.name} error: {e}")
                best_result = HealthCheckResult(
                    healthy=False,
                    level=best_result.level,
                    message=f"Error in {strategy.name}: {e}"
                )
                break

        # Cache result
        self._cache[cache_key] = (time.time(), best_result)

        return best_result

    async def check_health_parallel(
        self,
        state: "SupervisorState"
    ) -> Dict[str, HealthCheckResult]:
        """
        Run all health checks in parallel for comprehensive diagnostics.

        Returns dict of strategy_name -> result
        """
        tasks = {
            strategy.name: asyncio.create_task(
                asyncio.wait_for(strategy.check(state), timeout=HEALTH_CHECK_TIMEOUT)
            )
            for strategy in self.strategies
        }

        results = {}
        for name, task in tasks.items():
            try:
                results[name] = await task
            except asyncio.TimeoutError:
                results[name] = HealthCheckResult(
                    healthy=False,
                    level=HealthLevel.UNKNOWN,
                    message="Check timed out"
                )
            except Exception as e:
                results[name] = HealthCheckResult(
                    healthy=False,
                    level=HealthLevel.UNKNOWN,
                    message=f"Error: {e}"
                )

        return results

    def clear_cache(self):
        """Clear health check cache."""
        self._cache.clear()


# Global health checker instance
_health_checker: Optional[CompositeHealthChecker] = None

def get_health_checker() -> CompositeHealthChecker:
    """Get or create global health checker."""
    global _health_checker
    if _health_checker is None:
        _health_checker = CompositeHealthChecker()
    return _health_checker


@dataclass
class SupervisorState:
    """
    v115.0: Enhanced state information for the running supervisor.

    Includes process fingerprinting for PID reuse detection and
    cross-repo integration status.
    """
    pid: int
    entry_point: str  # "run_supervisor" or "start_system"
    started_at: str
    last_heartbeat: str
    hostname: str
    working_dir: str
    python_version: str
    command_line: str
    # v115.0: Enhanced fields
    process_start_time: float = 0.0  # For PID reuse detection
    machine_id: str = ""  # Unique machine identifier
    health_level: int = 0  # Current health level (HealthLevel enum value)
    cross_repo_status: Dict[str, str] = field(default_factory=dict)  # Repo -> status
    version: str = "115.0.0"  # Singleton version

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> SupervisorState:
        """Create from dict with backward compatibility for older formats."""
        # Handle fields that may not exist in older state files
        defaults = {
            "process_start_time": 0.0,
            "machine_id": "",
            "health_level": 0,
            "cross_repo_status": {},
            "version": "unknown"
        }
        for key, default in defaults.items():
            if key not in data:
                data[key] = default
        # Filter out any unknown keys that might cause issues
        known_keys = {
            "pid", "entry_point", "started_at", "last_heartbeat",
            "hostname", "working_dir", "python_version", "command_line",
            "process_start_time", "machine_id", "health_level",
            "cross_repo_status", "version"
        }
        filtered_data = {k: v for k, v in data.items() if k in known_keys}
        return cls(**filtered_data)

    @classmethod
    def create_current(cls, entry_point: str) -> SupervisorState:
        """Create state for current process with full fingerprinting."""
        import socket as sock
        import platform

        # Get process start time for PID reuse detection
        process_start_time = time.time()
        try:
            import psutil
            process_start_time = psutil.Process(os.getpid()).create_time()
        except Exception:
            pass

        # Generate machine ID
        machine_id = f"{platform.system().lower()}-{sock.gethostname()}"
        try:
            # Add more entropy to machine ID
            import uuid
            machine_id = f"{machine_id}-{uuid.getnode()}"
        except Exception:
            pass

        return cls(
            pid=os.getpid(),
            entry_point=entry_point,
            started_at=datetime.now().isoformat(),
            last_heartbeat=datetime.now().isoformat(),
            hostname=sock.gethostname(),
            working_dir=str(Path.cwd()),
            python_version=sys.version.split()[0],
            command_line=" ".join(sys.argv)[:500],  # Truncate long command lines
            process_start_time=process_start_time,
            machine_id=machine_id,
            health_level=int(HealthLevel.PROCESS_EXISTS),
            cross_repo_status={},
            version="115.0.0"
        )

    def get_uptime_seconds(self) -> float:
        """Get supervisor uptime in seconds."""
        try:
            started = datetime.fromisoformat(self.started_at)
            return (datetime.now() - started).total_seconds()
        except Exception:
            return 0.0

    def get_heartbeat_age_seconds(self) -> float:
        """Get age of last heartbeat in seconds."""
        try:
            heartbeat = datetime.fromisoformat(self.last_heartbeat)
            return (datetime.now() - heartbeat).total_seconds()
        except Exception:
            return float('inf')


class SupervisorSingleton:
    """
    Singleton enforcement for JARVIS supervisor processes.

    Uses file-based locking with fcntl for cross-process synchronization.
    """

    _instance: Optional[SupervisorSingleton] = None
    _lock_fd: Optional[int] = None
    _heartbeat_task: Optional[asyncio.Task] = None
    _state: Optional[SupervisorState] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._ensure_lock_dir()

    def _ensure_lock_dir(self) -> None:
        """Ensure lock directory exists."""
        LOCK_DIR.mkdir(parents=True, exist_ok=True)

    def _is_process_alive(self, pid: int) -> bool:
        """Check if a process is still running."""
        try:
            os.kill(pid, 0)  # Signal 0 just checks if process exists
            return True
        except OSError:
            return False

    def _is_jarvis_process(self, pid: int) -> bool:
        """Check if a PID is a JARVIS-related process."""
        try:
            # Read process command line
            if sys.platform == "darwin":
                import subprocess
                result = subprocess.run(
                    ["ps", "-p", str(pid), "-o", "command="],
                    capture_output=True,
                    text=True,
                    timeout=2
                )
                cmdline = result.stdout.strip()
            else:
                cmdline_path = Path(f"/proc/{pid}/cmdline")
                if cmdline_path.exists():
                    cmdline = cmdline_path.read_text().replace('\x00', ' ')
                else:
                    return False

            # Check for JARVIS patterns
            jarvis_patterns = [
                "run_supervisor.py",
                "start_system.py",
                "jarvis",
                "JARVIS",
            ]
            return any(pattern in cmdline for pattern in jarvis_patterns)
        except Exception:
            return False

    def _read_state(self) -> Optional[SupervisorState]:
        """
        v115.0: Read current supervisor state with fallback to lock file.

        If state file exists, read from it.
        If state file is missing but lock file exists, create synthetic state from lock file PID.
        This handles cases where state file was not created or got deleted.
        """
        # Primary: Try to read from state file
        try:
            if SUPERVISOR_STATE_FILE.exists():
                data = json.loads(SUPERVISOR_STATE_FILE.read_text())
                return SupervisorState.from_dict(data)
        except Exception as e:
            logger.debug(f"Could not read state file: {e}")

        # v115.0: Fallback - Create synthetic state from lock file PID
        try:
            if SUPERVISOR_LOCK_FILE.exists():
                lock_content = SUPERVISOR_LOCK_FILE.read_text().strip()
                if lock_content.isdigit():
                    pid = int(lock_content)
                    if self._is_process_alive(pid):
                        logger.debug(f"[Singleton] Creating synthetic state from lock file (PID: {pid})")

                        # Try to get process info
                        try:
                            import psutil
                            proc = psutil.Process(pid)
                            cmdline = " ".join(proc.cmdline())[:500]
                            start_time = proc.create_time()
                            entry_point = "run_supervisor" if "run_supervisor" in cmdline else "unknown"
                        except Exception:
                            cmdline = ""
                            start_time = 0.0
                            entry_point = "unknown"

                        # Create synthetic state
                        import socket as sock
                        return SupervisorState(
                            pid=pid,
                            entry_point=entry_point,
                            started_at=datetime.fromtimestamp(start_time).isoformat() if start_time else datetime.now().isoformat(),
                            last_heartbeat=datetime.now().isoformat(),  # Assume recently active if process alive
                            hostname=sock.gethostname(),
                            working_dir=str(Path.cwd()),
                            python_version=sys.version.split()[0],
                            command_line=cmdline,
                            process_start_time=start_time,
                            machine_id="",
                            health_level=int(HealthLevel.PROCESS_EXISTS),
                            cross_repo_status={},
                            version="115.0.0-synthetic"
                        )
        except Exception as e:
            logger.debug(f"Could not create synthetic state from lock file: {e}")

        return None

    def _write_state(self, state: SupervisorState) -> None:
        """Write supervisor state atomically."""
        try:
            temp_file = SUPERVISOR_STATE_FILE.with_suffix('.tmp')
            temp_file.write_text(json.dumps(state.to_dict(), indent=2))
            temp_file.rename(SUPERVISOR_STATE_FILE)
        except Exception as e:
            logger.warning(f"Could not write state file: {e}")

    def _is_lock_stale(self) -> Tuple[bool, Optional[SupervisorState]]:
        """
        v115.0: Enhanced stale lock detection with multi-layer health verification.

        Uses progressive health checks to detect:
        1. Dead processes
        2. Zombie processes
        3. PID reuse
        4. Hung/unresponsive supervisors (via IPC ping)
        5. Failed health endpoints

        Returns:
            (is_stale, existing_state)
        """
        state = self._read_state()
        if state is None:
            logger.debug("[Singleton] No state file found - lock is stale")
            return True, None

        # Layer 1: Check if process exists at all
        if not self._is_process_alive(state.pid):
            logger.info(f"[Singleton] Lock holder PID {state.pid} is dead")
            return True, state

        # Layer 2: Check if it's a JARVIS process (not PID reuse)
        if not self._is_jarvis_process(state.pid):
            logger.info(f"[Singleton] PID {state.pid} is not a JARVIS process (PID reuse detected)")
            return True, state

        # Layer 3: Validate process start time (PID reuse detection)
        if state.process_start_time > 0:
            try:
                import psutil
                proc = psutil.Process(state.pid)
                current_start = proc.create_time()
                time_diff = abs(current_start - state.process_start_time)
                if time_diff > 2.0:
                    logger.info(
                        f"[Singleton] PID reuse detected: start time mismatch "
                        f"(stored={state.process_start_time:.1f}, current={current_start:.1f}, diff={time_diff:.1f}s)"
                    )
                    return True, state
            except Exception as e:
                logger.debug(f"[Singleton] Could not validate process start time: {e}")

        # Layer 4: Check heartbeat age (reduced threshold in v115.0)
        heartbeat_age = state.get_heartbeat_age_seconds()
        if heartbeat_age > STALE_LOCK_THRESHOLD:
            logger.info(f"[Singleton] Lock stale: no heartbeat for {heartbeat_age:.0f}s (threshold: {STALE_LOCK_THRESHOLD}s)")
            return True, state

        # Layer 5: v115.0 - Functional health check (is supervisor actually responding?)
        # This catches hung supervisors where process exists but is unresponsive
        if not self._is_supervisor_functionally_healthy_sync(state):
            logger.warning(
                f"[Singleton] Lock holder PID {state.pid} is not functionally healthy "
                f"(process alive but not responding)"
            )
            return True, state

        return False, state

    def _is_supervisor_functionally_healthy_sync(self, state: SupervisorState) -> bool:
        """
        v115.0: Synchronous functional health check.

        Performs quick checks to verify supervisor is responsive:
        1. IPC socket exists and responds to ping
        2. HTTP health endpoint responds
        3. Readiness state file indicates healthy

        Returns True if supervisor appears healthy, False if unresponsive.
        """
        # Check 1: IPC ping (most reliable indicator of liveness)
        if SUPERVISOR_IPC_SOCKET.exists():
            try:
                # Synchronous socket check
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(IPC_TIMEOUT)
                sock.connect(str(SUPERVISOR_IPC_SOCKET))

                request = json.dumps({"command": "ping", "args": {}}).encode()
                sock.sendall(request)

                response_data = sock.recv(4096)
                sock.close()

                if response_data:
                    response = json.loads(response_data.decode())
                    if response.get("success") and response.get("result", {}).get("pong"):
                        logger.debug(f"[Singleton] IPC ping successful for PID {state.pid}")
                        return True
            except socket.timeout:
                logger.debug(f"[Singleton] IPC ping timeout for PID {state.pid}")
            except ConnectionRefusedError:
                logger.debug(f"[Singleton] IPC connection refused for PID {state.pid}")
            except Exception as e:
                logger.debug(f"[Singleton] IPC ping error for PID {state.pid}: {e}")

        # Check 2: HTTP health endpoint (fallback)
        for port in HTTP_HEALTH_PORTS:
            try:
                url = f"http://127.0.0.1:{port}/health"
                req = urllib.request.Request(url)
                req.add_header("User-Agent", "JARVIS-Singleton/115.0")

                with urllib.request.urlopen(req, timeout=HEALTH_CHECK_TIMEOUT) as response:
                    if response.status == 200:
                        logger.debug(f"[Singleton] HTTP health OK for PID {state.pid} on port {port}")
                        return True
            except Exception:
                continue

        # Check 3: Readiness state file (final fallback)
        readiness_file = TRINITY_READINESS_DIR / "jarvis-body.json"
        if readiness_file.exists():
            try:
                data = json.loads(readiness_file.read_text())
                phase = data.get("phase", "").lower()
                # Also check the file was recently updated
                file_age = time.time() - readiness_file.stat().st_mtime
                if phase in ("ready", "healthy", "operational", "warming_up") and file_age < 60:
                    logger.debug(f"[Singleton] Readiness state healthy for PID {state.pid}")
                    return True
            except Exception as e:
                logger.debug(f"[Singleton] Readiness state check error: {e}")

        # If heartbeat was very recent (within 30s), give benefit of doubt
        # This handles startup scenarios where IPC/HTTP aren't ready yet
        if heartbeat_age := state.get_heartbeat_age_seconds():
            if heartbeat_age < 30:
                logger.debug(
                    f"[Singleton] Recent heartbeat ({heartbeat_age:.0f}s ago), assuming healthy during startup"
                )
                return True

        logger.debug(f"[Singleton] All functional health checks failed for PID {state.pid}")
        return False

    def acquire(self, entry_point: str) -> bool:
        """
        Attempt to acquire the supervisor lock.

        Args:
            entry_point: Name of the entry point ("run_supervisor" or "start_system")

        Returns:
            True if lock acquired, False if another instance is running
        """
        self._ensure_lock_dir()

        try:
            # Open lock file
            self._lock_fd = os.open(
                str(SUPERVISOR_LOCK_FILE),
                os.O_CREAT | os.O_RDWR,
                0o644
            )

            # Try to acquire exclusive lock (non-blocking)
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                # Lock is held by another process
                is_stale, state = self._is_lock_stale()

                if is_stale:
                    # Stale lock - force acquire with timeout
                    logger.warning(f"[Singleton] Taking over stale lock from {state.entry_point if state else 'unknown'}")

                    # v118.0: Clean up stale IPC socket and state file FIRST
                    # This prevents connection errors when trying to ping dead supervisors
                    try:
                        if SUPERVISOR_IPC_SOCKET.exists():
                            SUPERVISOR_IPC_SOCKET.unlink()
                            logger.debug("[Singleton] v118.0: Cleaned stale IPC socket")
                    except Exception as e:
                        logger.debug(f"[Singleton] v118.0: IPC socket cleanup: {e}")

                    try:
                        if SUPERVISOR_STATE_FILE.exists():
                            SUPERVISOR_STATE_FILE.unlink()
                            logger.debug("[Singleton] v118.0: Cleaned stale state file")
                    except Exception as e:
                        logger.debug(f"[Singleton] v118.0: State file cleanup: {e}")

                    if state and self._is_process_alive(state.pid):
                        # Try to terminate gracefully
                        try:
                            logger.info(f"[Singleton] Sending SIGTERM to stale PID {state.pid}")
                            os.kill(state.pid, signal.SIGTERM)
                            time.sleep(2)
                            # Check if process is gone
                            if self._is_process_alive(state.pid):
                                logger.warning(f"[Singleton] PID {state.pid} didn't terminate, sending SIGKILL")
                                os.kill(state.pid, signal.SIGKILL)
                                time.sleep(1)
                        except ProcessLookupError:
                            logger.info(f"[Singleton] PID {state.pid} already dead")
                        except Exception as e:
                            logger.debug(f"[Singleton] Signal error (expected if process dead): {e}")

                    # v111.2: Force acquire with timeout and retry
                    # This prevents hanging if the kernel hasn't released the lock yet
                    lock_acquired = False
                    max_retries = 10
                    retry_delay = 0.5

                    for attempt in range(max_retries):
                        try:
                            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            lock_acquired = True
                            logger.info(f"[Singleton] Lock acquired on attempt {attempt + 1}")
                            break
                        except BlockingIOError:
                            if attempt < max_retries - 1:
                                logger.debug(f"[Singleton] Lock not yet available, retry {attempt + 1}/{max_retries}")
                                time.sleep(retry_delay)
                            else:
                                logger.warning(f"[Singleton] Lock still held after {max_retries} retries")

                    if not lock_acquired:
                        # v111.2: Nuclear option - recreate lock file
                        # This handles cases where the kernel lock is stuck
                        logger.warning("[Singleton] Forcibly recreating lock file (stale kernel lock)")
                        try:
                            os.close(self._lock_fd)
                            self._lock_fd = None
                            # Remove stale files
                            SUPERVISOR_LOCK_FILE.unlink(missing_ok=True)
                            SUPERVISOR_STATE_FILE.unlink(missing_ok=True)
                            # Recreate and acquire
                            self._lock_fd = os.open(
                                str(SUPERVISOR_LOCK_FILE),
                                os.O_CREAT | os.O_RDWR,
                                0o644
                            )
                            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            lock_acquired = True
                            logger.info("[Singleton] Lock acquired after file recreation")
                        except Exception as recreate_err:
                            logger.error(f"[Singleton] Lock file recreation failed: {recreate_err}")
                            return False

                else:
                    # Valid lock held by another process
                    os.close(self._lock_fd)
                    self._lock_fd = None

                    if state:
                        logger.error(
                            f"[Singleton] ❌ JARVIS already running!\n"
                            f"  Entry point: {state.entry_point}\n"
                            f"  PID: {state.pid}\n"
                            f"  Started: {state.started_at}\n"
                            f"  Working dir: {state.working_dir}"
                        )
                    return False

            # Lock acquired - write state
            self._state = SupervisorState.create_current(entry_point)
            self._write_state(self._state)

            # Write PID to lock file for external tools
            os.ftruncate(self._lock_fd, 0)
            os.lseek(self._lock_fd, 0, os.SEEK_SET)
            os.write(self._lock_fd, f"{os.getpid()}\n".encode())

            logger.info(f"[Singleton] ✅ Lock acquired for {entry_point} (PID: {os.getpid()})")

            # v117.0: Load preserved services from disk (for restart recovery)
            # If this is a restart via os.execv(), the registry file will have
            # PIDs of Trinity services that were preserved and should NOT be re-spawned.
            GlobalProcessRegistry.load_from_disk()
            preserved_pids = GlobalProcessRegistry.get_all()
            if preserved_pids:
                logger.info(
                    f"[Singleton] v117.0: Loaded {len(preserved_pids)} preserved services from registry: "
                    f"{[(pid, info.get('component')) for pid, info in preserved_pids.items()]}"
                )
            else:
                logger.debug("[Singleton] v117.0: No preserved services in registry (fresh start)")

            # v120.0: Detect if we're coming from a restart (RestartMarker exists)
            is_restart_completion = RestartMarker.is_restart_completion(os.getpid())
            if is_restart_completion:
                marker_data = RestartMarker.read()
                old_pid = marker_data.get("old_pid") if marker_data else "unknown"
                logger.info(f"[Singleton] v120.0: Restart completion detected (old PID: {old_pid})")
                RestartMarker.update_phase("new_process_started")
                # Schedule marker completion after full initialization
                self._restart_marker_pending_completion = True
            else:
                self._restart_marker_pending_completion = False

            # v109.4: Set up SIGHUP handler for clean restart via os.execv()
            _setup_sighup_handler()

            return True

        except Exception as e:
            logger.error(f"[Singleton] Lock acquisition failed: {e}")
            if self._lock_fd is not None:
                try:
                    os.close(self._lock_fd)
                except Exception:
                    pass
                self._lock_fd = None
            return False

    def release(self) -> None:
        """Release the supervisor lock."""
        # Stop heartbeat
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            self._heartbeat_task = None

        # Release file lock
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                os.close(self._lock_fd)
            except Exception as e:
                logger.debug(f"Error releasing lock: {e}")
            self._lock_fd = None

        # Clean up state file
        try:
            if SUPERVISOR_STATE_FILE.exists():
                state = self._read_state()
                if state and state.pid == os.getpid():
                    SUPERVISOR_STATE_FILE.unlink()
        except Exception as e:
            logger.debug(f"Error cleaning state file: {e}")

        logger.info("[Singleton] Lock released")

        # v116.1: Flush all output streams to prevent interleaving with client process
        import sys as _sys
        try:
            _sys.stdout.flush()
            _sys.stderr.flush()
        except Exception:
            pass

    async def start_heartbeat(self) -> None:
        """
        v115.0: Enhanced heartbeat with health level updates and cross-repo status.

        The heartbeat now:
        1. Updates last_heartbeat timestamp
        2. Periodically checks and updates health level
        3. Updates cross-repo connection status
        """
        async def heartbeat_loop():
            health_check_counter = 0
            cross_repo_check_counter = 0

            while True:
                try:
                    if self._state:
                        # Update heartbeat timestamp
                        self._state.last_heartbeat = datetime.now().isoformat()

                        # v115.0: Periodic health level update (every 6th heartbeat = ~30s)
                        health_check_counter += 1
                        if health_check_counter >= 6:
                            health_check_counter = 0
                            try:
                                health_checker = get_health_checker()
                                result = await health_checker.check_health(
                                    self._state,
                                    min_level=HealthLevel.HTTP_HEALTHY
                                )
                                self._state.health_level = int(result.level)
                            except Exception as he:
                                logger.debug(f"Health check in heartbeat failed: {he}")

                        # v115.0: Periodic cross-repo status update (every 12th heartbeat = ~60s)
                        cross_repo_check_counter += 1
                        if cross_repo_check_counter >= 12:
                            cross_repo_check_counter = 0
                            try:
                                cross_repo_status = await self._check_cross_repo_connections()
                                self._state.cross_repo_status = cross_repo_status
                            except Exception as ce:
                                logger.debug(f"Cross-repo check in heartbeat failed: {ce}")

                        self._write_state(self._state)

                except Exception as e:
                    logger.debug(f"Heartbeat error: {e}")

                await asyncio.sleep(HEARTBEAT_INTERVAL)

        self._heartbeat_task = asyncio.create_task(heartbeat_loop())

    async def _check_cross_repo_connections(self) -> Dict[str, str]:
        """
        v115.0: Check connection status to other repos.

        Returns dict of repo_name -> status ("connected", "disconnected", "unknown")
        """
        status = {}

        # Check JARVIS Prime
        prime_state_file = CROSS_REPO_DIR / "prime_state.json"
        if prime_state_file.exists():
            try:
                file_age = time.time() - prime_state_file.stat().st_mtime
                status["jarvis_prime"] = "connected" if file_age < 120 else "stale"
            except Exception:
                status["jarvis_prime"] = "error"
        else:
            status["jarvis_prime"] = "disconnected"

        # Check Reactor Core
        reactor_state_file = CROSS_REPO_DIR / "reactor_state.json"
        if reactor_state_file.exists():
            try:
                file_age = time.time() - reactor_state_file.stat().st_mtime
                status["reactor_core"] = "connected" if file_age < 120 else "stale"
            except Exception:
                status["reactor_core"] = "error"
        else:
            status["reactor_core"] = "disconnected"

        return status

    def is_locked(self) -> bool:
        """Check if we hold the lock."""
        return self._lock_fd is not None

    def get_state(self) -> Optional[SupervisorState]:
        """Get current state."""
        return self._state
    
    # =========================================================================
    # v113.0+ IPC SERVER METHODS
    # =========================================================================

    async def start_ipc_server(self, command_handlers: Optional[Dict[IPCCommand, Callable]] = None) -> None:
        """
        v120.0: Start IPC server with thread isolation for guaranteed responsiveness.

        When IPC_THREAD_ENABLED (default: true), uses ThreadedIPCServer which runs
        in a dedicated thread, immune to event loop blocking from heavy operations
        like ML model loading or workflow engine bugs.

        Supports both v113.0 legacy commands and v120.0 enhanced commands:
        - status, ping, restart, shutdown, takeover, force-stop (v113.0)
        - health, cross-repo-status, metrics, diagnostics (v115.0)
        - restart-status, restart-abort (v120.0)

        Args:
            command_handlers: Optional custom handlers for commands
        """
        # Remove stale socket file
        if SUPERVISOR_IPC_SOCKET.exists():
            try:
                SUPERVISOR_IPC_SOCKET.unlink()
            except Exception:
                pass

        # Set up default command handlers (v113.0 + v115.0 + v120.0)
        self._command_handlers: Dict[IPCCommand, Callable] = {
            # v113.0 commands
            IPCCommand.STATUS: self._handle_status,
            IPCCommand.PING: self._handle_ping,
            IPCCommand.RESTART: self._handle_restart,
            IPCCommand.SHUTDOWN: self._handle_shutdown,
            IPCCommand.TAKEOVER: self._handle_takeover,
            IPCCommand.FORCE_STOP: self._handle_force_stop,
            # v115.0 commands
            IPCCommand.HEALTH: self._handle_health,
            IPCCommand.CROSS_REPO_STATUS: self._handle_cross_repo_status,
            IPCCommand.METRICS: self._handle_metrics,
            IPCCommand.DIAGNOSTICS: self._handle_diagnostics,
            # v120.0 commands
            IPCCommand.RESTART_STATUS: self._handle_restart_status,
            IPCCommand.RESTART_ABORT: self._handle_restart_abort,
        }

        # Override with custom handlers if provided
        if command_handlers:
            for cmd, handler in command_handlers.items():
                self._command_handlers[cmd] = handler

        # v123.0: Use thread-isolated IPC server for guaranteed responsiveness
        if IPC_THREAD_ENABLED:
            try:
                main_loop = asyncio.get_event_loop()
                self._threaded_ipc = ThreadedIPCServer(
                    socket_path=SUPERVISOR_IPC_SOCKET,
                    handlers=self._command_handlers,
                    main_loop=main_loop,
                    singleton=self,  # v123.0: Pass singleton reference for state access
                )
                if self._threaded_ipc.start():
                    logger.info(f"[Singleton] v123.0 ThreadedIPCServer started: {SUPERVISOR_IPC_SOCKET}")
                else:
                    raise RuntimeError("ThreadedIPCServer.start() returned False")

                # v120.0: Complete restart marker if this was a restart
                self._mark_restart_complete_if_pending()
                return  # ThreadedIPCServer handles everything
            except Exception as e:
                logger.warning(f"[Singleton] ThreadedIPCServer failed, falling back to async: {e}")
                # Fall through to legacy async server

        # Legacy async IPC server (runs in main event loop)
        try:
            server = await asyncio.start_unix_server(
                self._handle_ipc_connection,
                path=str(SUPERVISOR_IPC_SOCKET),
            )
            self._ipc_server = server

            # Make socket world-readable for other processes
            os.chmod(str(SUPERVISOR_IPC_SOCKET), 0o666)

            logger.info(f"[Singleton] IPC server started (legacy async): {SUPERVISOR_IPC_SOCKET}")

            # Keep server running in background
            asyncio.create_task(self._ipc_server_loop(server))

            # v120.0: Complete restart marker if this was a restart
            self._mark_restart_complete_if_pending()

        except Exception as e:
            logger.warning(f"[Singleton] IPC server failed to start: {e}")

    def _mark_restart_complete_if_pending(self) -> None:
        """
        v120.0: Complete the restart marker if we detected a restart during acquire().

        This is called after the IPC server is fully started, indicating the
        new supervisor is ready to handle commands.
        """
        if getattr(self, '_restart_marker_pending_completion', False):
            try:
                marker_data = RestartMarker.read()
                if marker_data:
                    old_pid = marker_data.get("old_pid", "unknown")
                    marker_id = marker_data.get("marker_id", "unknown")
                    logger.info(
                        f"[Singleton] v120.0: Restart complete! "
                        f"old_pid={old_pid} → new_pid={os.getpid()}, marker={marker_id}"
                    )
                RestartMarker.complete()
                self._restart_marker_pending_completion = False
            except Exception as e:
                logger.warning(f"[Singleton] RestartMarker completion failed: {e}")

    async def _ipc_server_loop(self, server) -> None:
        """
        v119.4: Self-healing IPC server loop with automatic restart.

        If the server crashes, it will automatically restart after a brief delay.
        This prevents the "zombie supervisor" state where the process runs but
        IPC is unresponsive.
        """
        restart_count = 0
        max_restarts = 10  # Prevent infinite restart loops
        restart_delay = 1.0  # Seconds between restarts

        while restart_count < max_restarts:
            try:
                async with server:
                    await server.serve_forever()
                # Normal exit (shutdown requested)
                break
            except asyncio.CancelledError:
                logger.debug("[Singleton] IPC server cancelled (shutdown)")
                break
            except Exception as e:
                restart_count += 1
                logger.warning(f"[Singleton] v119.4: IPC server crashed ({restart_count}/{max_restarts}): {e}")

                if restart_count >= max_restarts:
                    logger.error("[Singleton] v119.4: IPC server max restarts reached, giving up")
                    break

                # Try to restart the server
                await asyncio.sleep(restart_delay)
                try:
                    # Remove stale socket and recreate server
                    if SUPERVISOR_IPC_SOCKET.exists():
                        SUPERVISOR_IPC_SOCKET.unlink()

                    server = await asyncio.start_unix_server(
                        self._handle_ipc_connection,
                        path=str(SUPERVISOR_IPC_SOCKET),
                    )
                    os.chmod(str(SUPERVISOR_IPC_SOCKET), 0o666)
                    logger.info(f"[Singleton] v119.4: IPC server restarted successfully")
                except Exception as restart_error:
                    logger.error(f"[Singleton] v119.4: IPC server restart failed: {restart_error}")
                    await asyncio.sleep(restart_delay * 2)  # Longer delay before next attempt
    
    async def _handle_ipc_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """
        Handle incoming IPC connection.

        v119.3: Enhanced to handle both line-terminated and EOF-terminated messages.
        """
        try:
            # v119.3: Read until EOF or newline (supports both raw socket and asyncio clients)
            data = b''
            try:
                # First try to read a line (preferred - faster)
                line = await asyncio.wait_for(reader.readline(), timeout=5.0)
                if line:
                    data = line
                else:
                    # No line, try reading until EOF
                    data = await asyncio.wait_for(reader.read(4096), timeout=5.0)
            except asyncio.TimeoutError:
                # Try one more read
                try:
                    data = await asyncio.wait_for(reader.read(4096), timeout=1.0)
                except asyncio.TimeoutError:
                    pass

            if not data:
                writer.close()
                await writer.wait_closed()
                return

            # Parse command (strip whitespace/newlines)
            try:
                request = json.loads(data.decode().strip())
                command = request.get("command", "")
                args = request.get("args", {})
            except json.JSONDecodeError:
                response = {"success": False, "error": "Invalid JSON"}
                writer.write((json.dumps(response) + '\n').encode())
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                return
            
            # Handle command
            try:
                cmd_enum = IPCCommand(command)
                handler = self._command_handlers.get(cmd_enum)
                
                if handler:
                    result = await handler(args)
                    response = {"success": True, "result": result}
                else:
                    response = {"success": False, "error": f"Unknown command: {command}"}
                    
            except ValueError:
                response = {"success": False, "error": f"Invalid command: {command}"}
            except Exception as e:
                response = {"success": False, "error": str(e)}
            
            # v119.3: Send response with newline for consistent framing
            writer.write((json.dumps(response) + '\n').encode())
            await writer.drain()

        except asyncio.TimeoutError:
            logger.debug("[Singleton] IPC connection timed out")
        except Exception as e:
            logger.debug(f"[Singleton] IPC connection error: {e}")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
    
    async def _handle_status(self, args: Dict) -> Dict[str, Any]:
        """Handle STATUS command - return current supervisor status."""
        state = self._state
        if state:
            return {
                "running": True,
                "pid": state.pid,
                "entry_point": state.entry_point,
                "started_at": state.started_at,
                "last_heartbeat": state.last_heartbeat,
                "uptime_seconds": (datetime.now() - datetime.fromisoformat(state.started_at)).total_seconds(),
            }
        return {"running": False}
    
    async def _handle_ping(self, args: Dict) -> Dict[str, Any]:
        """Handle PING command - simple liveness check."""
        return {"pong": True, "timestamp": datetime.now().isoformat()}
    
    async def _handle_restart(self, args: Dict) -> Dict[str, Any]:
        """
        v120.0: Handle RESTART command with cross-process coordination.

        Uses RestartMarker for atomic cross-process coordination:
        1. Creates restart marker BEFORE sending SIGHUP
        2. Marker contains old PID for new instance to verify
        3. New instance updates marker phases during startup
        4. Client can poll restart-status to track progress

        v109.4: Uses os.execv() to replace the process, avoiding atexit handlers
        and the EXC_GUARD crash that occurred with async cleanup during shutdown.

        v117.1: Schedule SIGHUP AFTER response is sent to avoid IPC response race.
        """
        logger.info("[Singleton] v120.0: Restart requested via IPC - creating RestartMarker")

        old_pid = os.getpid()
        marker_id = None

        # v120.0: Create restart marker for cross-process coordination
        try:
            marker_id = RestartMarker.create(old_pid)
            logger.info(f"[Singleton] RestartMarker created: {marker_id}")
        except Exception as e:
            logger.warning(f"[Singleton] RestartMarker creation failed (proceeding anyway): {e}")

        # v120.0: Notify cross-repo services about pending restart
        cross_repo_notified = []
        try:
            # Try to notify jarvis-prime and reactor-core if configured
            cross_repo_notified = await self._notify_cross_repo_restart(marker_id)
            if marker_id and cross_repo_notified:
                RestartMarker.mark_cross_repo_notified(cross_repo_notified)
        except Exception as e:
            logger.debug(f"[Singleton] Cross-repo notification skipped: {e}")

        # v121.0: Schedule the SIGHUP to be sent AFTER the response is transmitted
        # Update phase BEFORE sending signal so clients can track progress
        async def _delayed_restart():
            """Send SIGHUP after a brief delay to allow response transmission."""
            await asyncio.sleep(0.15)  # 150ms delay for response to be sent

            # v121.0: Update phase BEFORE sending SIGHUP so clients see "signal_pending"
            RestartMarker.update_phase("signal_pending")
            logger.info("[Singleton] v121.0: Sending SIGHUP for os.execv() restart")

            # Send SIGHUP to ourselves - this triggers the os.execv() handler
            os.kill(os.getpid(), signal.SIGHUP)

        try:
            # Schedule the restart (non-blocking, runs after response is sent)
            asyncio.create_task(_delayed_restart())
            return {
                "restart_initiated": True,
                "method": "execv",
                "marker_id": marker_id,
                "old_pid": old_pid,
                "cross_repo_notified": cross_repo_notified,
                "note": "Restart will occur in ~100ms. Poll restart-status for progress."
            }
        except Exception as e:
            # Clean up marker on failure
            if marker_id:
                RestartMarker.fail(str(e))
            return {"restart_initiated": False, "error": str(e)}

    async def _notify_cross_repo_restart(self, marker_id: Optional[str]) -> List[str]:
        """
        v120.0: Notify cross-repo services about pending restart.

        Sends restart notification to jarvis-prime and reactor-core via their
        IPC sockets or HTTP endpoints, allowing them to prepare for reconnection.
        """
        notified = []

        # Cross-repo notification paths
        cross_repo_sockets = [
            (Path.home() / ".jarvis-prime" / "locks" / "supervisor.sock", "jarvis-prime"),
            (Path.home() / ".reactor-core" / "locks" / "supervisor.sock", "reactor-core"),
        ]

        for sock_path, repo_name in cross_repo_sockets:
            if sock_path.exists():
                try:
                    # Quick non-blocking notification
                    reader, writer = await asyncio.wait_for(
                        asyncio.open_unix_connection(str(sock_path)),
                        timeout=1.0
                    )
                    msg = json.dumps({
                        "command": "parent-restarting",
                        "marker_id": marker_id,
                        "parent_pid": os.getpid()
                    })
                    writer.write(msg.encode() + b'\n')
                    await asyncio.wait_for(writer.drain(), timeout=0.5)
                    writer.close()
                    with suppress(Exception):
                        await writer.wait_closed()
                    notified.append(repo_name)
                    logger.debug(f"[Singleton] Notified {repo_name} of restart")
                except Exception as e:
                    logger.debug(f"[Singleton] Could not notify {repo_name}: {e}")

        return notified

    async def _handle_restart_status(self, args: Dict) -> Dict[str, Any]:
        """
        v120.0: Handle RESTART_STATUS command - query restart marker state.

        Used by clients to poll restart progress after initiating a restart.
        Returns the current state of the restart marker including phase,
        timestamps, and whether restart has completed.
        """
        marker_data = RestartMarker.read()

        if not marker_data:
            return {
                "restart_active": False,
                "message": "No active restart in progress"
            }

        # v120.0: Detect if this is the NEW supervisor (restart completed)
        # With os.execv(), PID stays the same, so we check startup time
        current_pid = os.getpid()
        old_pid = marker_data.get("old_pid")
        phase = marker_data.get("phase", "")

        # If phase is new_process_started or completed, we're the new instance
        is_new_instance = phase in ("new_process_started", "completed")

        # Also check if our state file startup time is after marker creation
        if not is_new_instance and self._state:
            marker_time = marker_data.get("initiated_at", "")
            state_time = self._state.started_at or ""
            if state_time > marker_time:
                # Our startup time is after marker was created - we're the new instance
                is_new_instance = True

        return {
            "restart_active": RestartMarker.is_active(),
            "is_new_instance": is_new_instance,
            "marker_id": marker_data.get("marker_id"),
            "old_pid": old_pid,
            "current_pid": current_pid,
            "phase": marker_data.get("phase"),
            "created_at": marker_data.get("created_at"),
            "updated_at": marker_data.get("updated_at"),
            "cross_repo_notified": marker_data.get("cross_repo_notified", []),
            "error": marker_data.get("error"),
        }

    async def _handle_restart_abort(self, args: Dict) -> Dict[str, Any]:
        """
        v120.0: Handle RESTART_ABORT command - abort a pending restart.

        Can only abort if restart hasn't progressed past the 'signal_sent' phase.
        """
        marker_data = RestartMarker.read()

        if not marker_data:
            return {
                "aborted": False,
                "error": "No active restart to abort"
            }

        phase = marker_data.get("phase", "")
        if phase in ("signal_sent", "execv_triggered", "new_process_started"):
            return {
                "aborted": False,
                "error": f"Cannot abort restart in phase '{phase}' - too late"
            }

        # Clear the marker
        RestartMarker.clear()
        logger.info("[Singleton] v120.0: Restart aborted via IPC")

        return {
            "aborted": True,
            "message": "Restart marker cleared"
        }
    
    async def _handle_shutdown(self, args: Dict) -> Dict[str, Any]:
        """Handle SHUTDOWN command - graceful shutdown."""
        logger.info("[Singleton] Shutdown requested via IPC")
        try:
            os.kill(os.getpid(), signal.SIGTERM)
            return {"shutdown_initiated": True}
        except Exception as e:
            return {"shutdown_initiated": False, "error": str(e)}
    
    async def _handle_takeover(self, args: Dict) -> Dict[str, Any]:
        """Handle TAKEOVER command - new instance wants to take over."""
        logger.info("[Singleton] Takeover requested via IPC")
        # Set takeover flag and initiate graceful shutdown
        try:
            self._takeover_requested = True
            # Give new instance a chance to start, then shutdown
            asyncio.create_task(self._delayed_takeover_shutdown())
            return {"takeover_accepted": True, "message": "Shutting down in 5 seconds for takeover"}
        except Exception as e:
            return {"takeover_accepted": False, "error": str(e)}
    
    async def _handle_force_stop(self, args: Dict) -> Dict[str, Any]:
        """Handle FORCE_STOP command - immediate shutdown."""
        logger.warning("[Singleton] Force stop requested via IPC")
        try:
            os.kill(os.getpid(), signal.SIGKILL)
            return {"force_stop_initiated": True}
        except Exception as e:
            return {"force_stop_initiated": False, "error": str(e)}
    
    async def _delayed_takeover_shutdown(self) -> None:
        """Shutdown after delay for takeover."""
        await asyncio.sleep(5.0)
        if getattr(self, '_takeover_requested', False):
            logger.info("[Singleton] Takeover: shutting down now")
            os.kill(os.getpid(), signal.SIGTERM)

    # =========================================================================
    # v115.0: ENHANCED IPC COMMAND HANDLERS
    # =========================================================================

    async def _handle_health(self, args: Dict) -> Dict[str, Any]:
        """
        v115.0: Handle HEALTH command - comprehensive health report.

        Returns detailed health information including all strategy results.
        """
        state = self._state
        if not state:
            return {"healthy": False, "error": "No supervisor state"}

        health_checker = get_health_checker()

        # Run parallel health checks for comprehensive report
        results = await health_checker.check_health_parallel(state)

        # Determine overall health
        overall_healthy = all(r.healthy for r in results.values())
        max_level = max((r.level for r in results.values()), default=HealthLevel.UNKNOWN)

        return {
            "healthy": overall_healthy,
            "health_level": max_level.name,
            "health_level_value": int(max_level),
            "checks": {name: result.to_dict() for name, result in results.items()},
            "uptime_seconds": state.get_uptime_seconds(),
            "heartbeat_age_seconds": state.get_heartbeat_age_seconds(),
            "pid": state.pid,
            "entry_point": state.entry_point,
            "version": state.version
        }

    async def _handle_cross_repo_status(self, args: Dict) -> Dict[str, Any]:
        """
        v115.0: Handle CROSS_REPO_STATUS command - cross-repo integration status.

        Returns status of connected repositories (Prime, Reactor).
        """
        cross_repo_status = {}

        # Check JARVIS Prime status
        prime_state_file = CROSS_REPO_DIR / "prime_state.json"
        if prime_state_file.exists():
            try:
                data = json.loads(prime_state_file.read_text())
                file_age = time.time() - prime_state_file.stat().st_mtime
                cross_repo_status["jarvis_prime"] = {
                    "connected": file_age < 60,
                    "last_update_seconds_ago": file_age,
                    "status": data.get("status", "unknown"),
                    "details": data
                }
            except Exception as e:
                cross_repo_status["jarvis_prime"] = {"connected": False, "error": str(e)}
        else:
            cross_repo_status["jarvis_prime"] = {"connected": False, "error": "State file not found"}

        # Check Reactor Core status
        reactor_state_file = CROSS_REPO_DIR / "reactor_state.json"
        if reactor_state_file.exists():
            try:
                data = json.loads(reactor_state_file.read_text())
                file_age = time.time() - reactor_state_file.stat().st_mtime
                cross_repo_status["reactor_core"] = {
                    "connected": file_age < 60,
                    "last_update_seconds_ago": file_age,
                    "status": data.get("status", "unknown"),
                    "details": data
                }
            except Exception as e:
                cross_repo_status["reactor_core"] = {"connected": False, "error": str(e)}
        else:
            cross_repo_status["reactor_core"] = {"connected": False, "error": "State file not found"}

        # Check heartbeat file
        heartbeat_file = CROSS_REPO_DIR / "heartbeat.json"
        if heartbeat_file.exists():
            try:
                data = json.loads(heartbeat_file.read_text())
                file_age = time.time() - heartbeat_file.stat().st_mtime
                cross_repo_status["heartbeat"] = {
                    "active": file_age < 60,
                    "last_update_seconds_ago": file_age,
                    "details": data
                }
            except Exception as e:
                cross_repo_status["heartbeat"] = {"active": False, "error": str(e)}
        else:
            cross_repo_status["heartbeat"] = {"active": False, "error": "Heartbeat file not found"}

        # Summary
        connected_count = sum(
            1 for v in cross_repo_status.values()
            if v.get("connected") or v.get("active")
        )

        return {
            "total_repos": 3,
            "connected_repos": connected_count,
            "all_connected": connected_count >= 2,  # At least JARVIS + one other
            "repos": cross_repo_status
        }

    async def _handle_metrics(self, args: Dict) -> Dict[str, Any]:
        """
        v115.0: Handle METRICS command - performance metrics.

        Returns resource usage and performance statistics.
        """
        metrics = {
            "timestamp": time.time(),
            "pid": os.getpid()
        }

        try:
            import psutil
            proc = psutil.Process(os.getpid())

            metrics["cpu_percent"] = proc.cpu_percent(interval=0.1)
            metrics["memory_mb"] = proc.memory_info().rss / 1024 / 1024
            metrics["memory_percent"] = proc.memory_percent()
            metrics["num_threads"] = proc.num_threads()
            metrics["num_fds"] = proc.num_fds() if hasattr(proc, 'num_fds') else -1

            # Connection counts (using net_connections for psutil >= 5.3.0)
            try:
                connections = proc.net_connections() if hasattr(proc, 'net_connections') else proc.connections()
                metrics["connections"] = {
                    "total": len(connections),
                    "established": sum(1 for c in connections if c.status == 'ESTABLISHED'),
                    "listening": sum(1 for c in connections if c.status == 'LISTEN')
                }
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                metrics["connections"] = {"error": "Access denied"}

            # Open files
            try:
                metrics["open_files"] = len(proc.open_files())
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                metrics["open_files"] = -1

        except ImportError:
            metrics["error"] = "psutil not available"
        except Exception as e:
            metrics["error"] = str(e)

        # Add uptime
        if self._state:
            metrics["uptime_seconds"] = self._state.get_uptime_seconds()

        return metrics

    async def _handle_diagnostics(self, args: Dict) -> Dict[str, Any]:
        """
        v115.0: Handle DIAGNOSTICS command - diagnostic information.

        Returns detailed diagnostic info for troubleshooting.
        """
        diagnostics = {
            "timestamp": time.time(),
            "version": "115.0.0",
            "pid": os.getpid(),
            "python_version": sys.version,
            "platform": sys.platform
        }

        # State information
        if self._state:
            diagnostics["state"] = self._state.to_dict()
        else:
            diagnostics["state"] = None

        # Lock status
        diagnostics["lock"] = {
            "is_locked": self.is_locked(),
            "lock_file": str(SUPERVISOR_LOCK_FILE),
            "lock_file_exists": SUPERVISOR_LOCK_FILE.exists(),
            "state_file": str(SUPERVISOR_STATE_FILE),
            "state_file_exists": SUPERVISOR_STATE_FILE.exists()
        }

        # IPC status
        diagnostics["ipc"] = {
            "socket_path": str(SUPERVISOR_IPC_SOCKET),
            "socket_exists": SUPERVISOR_IPC_SOCKET.exists(),
            "server_running": hasattr(self, '_ipc_server') and self._ipc_server is not None
        }

        # Configuration
        diagnostics["config"] = {
            "stale_lock_threshold": STALE_LOCK_THRESHOLD,
            "heartbeat_interval": HEARTBEAT_INTERVAL,
            "health_check_timeout": HEALTH_CHECK_TIMEOUT,
            "ipc_timeout": IPC_TIMEOUT,
            "http_health_ports": HTTP_HEALTH_PORTS
        }

        # Directory structure
        diagnostics["directories"] = {
            "lock_dir": str(LOCK_DIR),
            "lock_dir_exists": LOCK_DIR.exists(),
            "cross_repo_dir": str(CROSS_REPO_DIR),
            "cross_repo_dir_exists": CROSS_REPO_DIR.exists(),
            "trinity_readiness_dir": str(TRINITY_READINESS_DIR),
            "trinity_readiness_dir_exists": TRINITY_READINESS_DIR.exists()
        }

        # Environment
        diagnostics["environment"] = {
            "JARVIS_LOCK_DIR": os.environ.get("JARVIS_LOCK_DIR"),
            "JARVIS_CROSS_REPO_DIR": os.environ.get("JARVIS_CROSS_REPO_DIR"),
            "JARVIS_STALE_LOCK_THRESHOLD": os.environ.get("JARVIS_STALE_LOCK_THRESHOLD")
        }

        return diagnostics

    def cleanup_ipc(self) -> None:
        """Clean up IPC socket on shutdown."""
        try:
            if SUPERVISOR_IPC_SOCKET.exists():
                state = self._read_state()
                # Only remove if we own it
                if state and state.pid == os.getpid():
                    SUPERVISOR_IPC_SOCKET.unlink()
        except Exception:
            pass


# Module-level convenience functions
_singleton: Optional[SupervisorSingleton] = None


def get_singleton() -> SupervisorSingleton:
    """Get the singleton instance."""
    global _singleton
    if _singleton is None:
        _singleton = SupervisorSingleton()
    return _singleton


def acquire_supervisor_lock(entry_point: str) -> bool:
    """
    Acquire the supervisor lock.

    Args:
        entry_point: Name of the entry point

    Returns:
        True if lock acquired, False if another instance running
    """
    return get_singleton().acquire(entry_point)


def release_supervisor_lock() -> None:
    """Release the supervisor lock."""
    get_singleton().release()


async def start_supervisor_heartbeat() -> None:
    """Start heartbeat to keep lock fresh."""
    await get_singleton().start_heartbeat()


def is_supervisor_running() -> Tuple[bool, Optional[Dict[str, Any]]]:
    """
    v115.0: Enhanced check if a supervisor is already running.

    Uses multi-layer health verification to ensure the reported
    supervisor is actually healthy and responsive.

    Returns:
        (is_running, state_dict or None)
    """
    singleton = get_singleton()
    is_stale, state = singleton._is_lock_stale()

    if state and not is_stale:
        # Additional verification: add health level to state dict
        state_dict = state.to_dict()
        state_dict["_verified_healthy"] = True
        state_dict["_verification_timestamp"] = time.time()
        return True, state_dict

    return False, None


async def is_supervisor_running_async() -> Tuple[bool, Optional[Dict[str, Any]], Optional[HealthCheckResult]]:
    """
    v115.0: Async version with comprehensive health check.

    Returns:
        (is_running, state_dict or None, health_check_result or None)
    """
    singleton = get_singleton()
    is_stale, state = singleton._is_lock_stale()

    if state and not is_stale:
        # Run full async health check
        health_checker = get_health_checker()
        health_result = await health_checker.check_health(state, min_level=HealthLevel.IPC_RESPONSIVE)

        if health_result.healthy:
            state_dict = state.to_dict()
            state_dict["_health_check"] = health_result.to_dict()
            return True, state_dict, health_result

        # Supervisor exists but isn't healthy - treat as stale
        logger.warning(
            f"[Singleton] Supervisor PID {state.pid} exists but health check failed: "
            f"{health_result.message}"
        )
        return False, None, health_result

    return False, None, None


def get_supervisor_health_report() -> Dict[str, Any]:
    """
    v115.0: Get comprehensive health report for running supervisor.

    Returns detailed health information including all check results.
    """
    singleton = get_singleton()
    state = singleton._read_state()

    if state is None:
        return {
            "running": False,
            "message": "No supervisor state found"
        }

    # Check if process exists
    process_exists = singleton._is_process_alive(state.pid)
    is_jarvis = singleton._is_jarvis_process(state.pid) if process_exists else False
    is_stale, _ = singleton._is_lock_stale()

    report = {
        "running": not is_stale,
        "pid": state.pid,
        "entry_point": state.entry_point,
        "started_at": state.started_at,
        "uptime_seconds": state.get_uptime_seconds(),
        "last_heartbeat": state.last_heartbeat,
        "heartbeat_age_seconds": state.get_heartbeat_age_seconds(),
        "process_exists": process_exists,
        "is_jarvis_process": is_jarvis,
        "is_stale": is_stale,
        "health_level": state.health_level,
        "cross_repo_status": state.cross_repo_status,
        "version": state.version,
        "machine_id": state.machine_id,
        "hostname": state.hostname,
        "working_dir": state.working_dir
    }

    # Add functional health status
    if process_exists and not is_stale:
        report["functionally_healthy"] = singleton._is_supervisor_functionally_healthy_sync(state)
    else:
        report["functionally_healthy"] = False

    return report


# =========================================================================
# v113.0: IPC CLIENT FUNCTIONS
# =========================================================================

async def send_supervisor_command(
    command: str,
    args: Optional[Dict[str, Any]] = None,
    timeout: float = 5.0
) -> Dict[str, Any]:
    """
    v119.3: Send IPC command to running supervisor.

    Args:
        command: Command name (status, ping, restart, shutdown, takeover, force-stop)
        args: Optional command arguments
        timeout: Connection timeout in seconds

    Returns:
        Response dict from supervisor or error dict

    v119.3 Fixes:
    - Added newline terminator to match raw socket behavior
    - Close write side after sending to signal EOF
    - More robust response handling
    """
    if not SUPERVISOR_IPC_SOCKET.exists():
        return {"success": False, "error": "No supervisor IPC socket found"}

    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(SUPERVISOR_IPC_SOCKET)),
            timeout=timeout
        )

        # v119.5: Send command with newline terminator (matches raw socket behavior)
        request = {"command": command, "args": args or {}}
        writer.write((json.dumps(request) + '\n').encode())
        await writer.drain()

        # v119.5: DON'T call write_eof() - it can close the connection before response
        # The server uses readline() which works fine with the newline terminator
        # Response is also newline-terminated, so use readline() for reading

        # Read response with timeout - use readline for framed response
        data = await asyncio.wait_for(reader.readline(), timeout=timeout)

        writer.close()
        await writer.wait_closed()

        if not data:
            return {"success": False, "error": "Empty response from supervisor"}

        response = json.loads(data.decode().strip())
        return response

    except asyncio.TimeoutError:
        return {"success": False, "error": "Supervisor IPC timeout"}
    except ConnectionRefusedError:
        return {"success": False, "error": "Supervisor not responding"}
    except json.JSONDecodeError as e:
        return {"success": False, "error": f"Invalid JSON response: {e}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def send_supervisor_command_sync(
    command: str,
    args: Optional[Dict[str, Any]] = None,
    timeout: float = 5.0
) -> Dict[str, Any]:
    """
    v113.0: Synchronous wrapper for send_supervisor_command.
    """
    try:
        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(
            send_supervisor_command(command, args, timeout)
        )
        loop.close()
        return result
    except Exception as e:
        return {"success": False, "error": str(e)}


async def start_supervisor_ipc_server() -> None:
    """v113.0: Start the IPC server on the running singleton."""
    await get_singleton().start_ipc_server()


# =============================================================================
# v130.0: UNIFIED COMMAND HANDLER WITH AUTOMATIC CLEANUP
# =============================================================================
# Advanced multi-command support with:
# - Automatic stale lock cleanup BEFORE any command
# - Combined command support (--restart --status)
# - Atomic lock cleanup with race condition prevention
# - Force cleanup for hung supervisors
# - Pre-command stale lock detection
# - Cross-repo integration
# - Comprehensive error handling
# =============================================================================

class LockCleanupResult:
    """v130.0: Result of lock cleanup operation."""
    __slots__ = ('success', 'cleaned_lock', 'cleaned_socket', 'cleaned_state',
                 'stale_pid', 'reason', 'error', 'duration_ms')

    def __init__(self):
        self.success: bool = False
        self.cleaned_lock: bool = False
        self.cleaned_socket: bool = False
        self.cleaned_state: bool = False
        self.stale_pid: Optional[int] = None
        self.reason: str = ""
        self.error: Optional[str] = None
        self.duration_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "cleaned_lock": self.cleaned_lock,
            "cleaned_socket": self.cleaned_socket,
            "cleaned_state": self.cleaned_state,
            "stale_pid": self.stale_pid,
            "reason": self.reason,
            "error": self.error,
            "duration_ms": self.duration_ms
        }


class CommandResult:
    """v130.0: Result of unified command execution."""
    __slots__ = ('success', 'command', 'response', 'cleanup_result',
                 'verification_passed', 'error', 'duration_ms')

    def __init__(self, command: str):
        self.success: bool = False
        self.command: str = command
        self.response: Optional[Dict[str, Any]] = None
        self.cleanup_result: Optional[LockCleanupResult] = None
        self.verification_passed: bool = False
        self.error: Optional[str] = None
        self.duration_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "command": self.command,
            "response": self.response,
            "cleanup_result": self.cleanup_result.to_dict() if self.cleanup_result else None,
            "verification_passed": self.verification_passed,
            "error": self.error,
            "duration_ms": self.duration_ms
        }


async def cleanup_stale_locks(
    force: bool = False,
    timeout: float = 5.0,
    cross_repo: bool = True
) -> LockCleanupResult:
    """
    v130.0: Comprehensive stale lock cleanup with cross-repo support.

    This function MUST be called before any command that requires the lock
    to be in a valid state. It handles all edge cases:
    - Dead process with stale lock
    - Hung supervisor with stale heartbeat
    - Orphaned IPC sockets
    - Corrupted state files
    - Permission issues
    - Cross-repo orphaned resources

    Args:
        force: Force cleanup even if lock appears valid
        timeout: Timeout for cleanup operations
        cross_repo: Also clean stale resources in connected repos

    Returns:
        LockCleanupResult with details of what was cleaned
    """
    start_time = time.time()
    result = LockCleanupResult()

    try:
        singleton = get_singleton()
        is_stale, state = singleton._is_lock_stale()

        if not is_stale and not force:
            # Lock is valid - nothing to clean
            result.success = True
            result.reason = "lock_valid"
            result.duration_ms = (time.time() - start_time) * 1000
            return result

        # v130.0: Atomic cleanup with file locking to prevent races
        # Use a temporary cleanup lock to prevent multiple processes
        # from cleaning up simultaneously
        cleanup_lock_path = LOCK_DIR / ".cleanup.lock"
        cleanup_lock_fd = None

        try:
            # Ensure lock directory exists
            LOCK_DIR.mkdir(parents=True, exist_ok=True)

            # Acquire cleanup lock (non-blocking)
            cleanup_lock_fd = os.open(str(cleanup_lock_path), os.O_CREAT | os.O_RDWR)
            try:
                fcntl.flock(cleanup_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                # Another process is cleaning up - wait briefly then check again
                await asyncio.sleep(0.5)
                is_stale, state = singleton._is_lock_stale()
                if not is_stale:
                    result.success = True
                    result.reason = "cleaned_by_other_process"
                    result.duration_ms = (time.time() - start_time) * 1000
                    return result
                # Still stale after other process finished - try to acquire lock
                fcntl.flock(cleanup_lock_fd, fcntl.LOCK_EX)

            # Record stale PID for logging
            if state:
                result.stale_pid = state.pid

            # Step 1: Clean stale IPC socket
            if SUPERVISOR_IPC_SOCKET.exists():
                try:
                    SUPERVISOR_IPC_SOCKET.unlink()
                    result.cleaned_socket = True
                    logger.debug(f"[v130.0] Cleaned stale IPC socket")
                except PermissionError as e:
                    logger.warning(f"[v130.0] Cannot clean IPC socket: {e}")
                except Exception as e:
                    logger.debug(f"[v130.0] IPC socket cleanup: {e}")

            # Step 2: Clean stale state file
            if SUPERVISOR_STATE_FILE.exists():
                try:
                    SUPERVISOR_STATE_FILE.unlink()
                    result.cleaned_state = True
                    logger.debug(f"[v130.0] Cleaned stale state file")
                except PermissionError as e:
                    logger.warning(f"[v130.0] Cannot clean state file: {e}")
                except Exception as e:
                    logger.debug(f"[v130.0] State file cleanup: {e}")

            # Step 3: Clean stale lock file
            if SUPERVISOR_LOCK_FILE.exists():
                try:
                    # First try to release any lingering lock
                    try:
                        lock_fd = os.open(str(SUPERVISOR_LOCK_FILE), os.O_RDWR)
                        fcntl.flock(lock_fd, fcntl.LOCK_UN)
                        os.close(lock_fd)
                    except Exception:
                        pass

                    # Then remove the file
                    SUPERVISOR_LOCK_FILE.unlink()
                    result.cleaned_lock = True
                    logger.debug(f"[v130.0] Cleaned stale lock file")
                except PermissionError as e:
                    logger.warning(f"[v130.0] Cannot clean lock file: {e}")
                except Exception as e:
                    logger.debug(f"[v130.0] Lock file cleanup: {e}")

            # Step 4: Cross-repo cleanup if enabled
            if cross_repo:
                await _cleanup_cross_repo_stale_resources()

            # Step 5: Kill hung supervisor if still alive
            if state and result.stale_pid:
                try:
                    if singleton._is_process_alive(result.stale_pid):
                        # Process is alive but stale - send SIGTERM
                        logger.warning(
                            f"[v130.0] Sending SIGTERM to hung supervisor PID {result.stale_pid}"
                        )
                        os.kill(result.stale_pid, signal.SIGTERM)
                        await asyncio.sleep(0.5)

                        # If still alive, SIGKILL
                        if singleton._is_process_alive(result.stale_pid):
                            logger.warning(
                                f"[v130.0] Sending SIGKILL to stuck supervisor PID {result.stale_pid}"
                            )
                            os.kill(result.stale_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass  # Process already dead
                except PermissionError as e:
                    logger.warning(f"[v130.0] Cannot kill PID {result.stale_pid}: {e}")

            result.success = True
            result.reason = "cleanup_complete"

        finally:
            # Release cleanup lock
            if cleanup_lock_fd is not None:
                try:
                    fcntl.flock(cleanup_lock_fd, fcntl.LOCK_UN)
                    os.close(cleanup_lock_fd)
                except Exception:
                    pass

            # Remove cleanup lock file
            try:
                cleanup_lock_path.unlink(missing_ok=True)
            except Exception:
                pass

    except Exception as e:
        result.success = False
        result.error = str(e)
        result.reason = "cleanup_failed"
        logger.error(f"[v130.0] Stale lock cleanup failed: {e}")

    result.duration_ms = (time.time() - start_time) * 1000
    return result


async def _cleanup_cross_repo_stale_resources() -> None:
    """
    v130.0: Clean stale resources in connected repos.

    Checks JARVIS-Prime and Reactor-Core for orphaned locks/sockets.
    """
    try:
        # Get cross-repo directories from environment or defaults
        cross_repo_dirs = [
            _get_env_path("JARVIS_PRIME_DIR", Path.home() / "Documents/repos/jarvis-prime"),
            _get_env_path("REACTOR_CORE_DIR", Path.home() / "Documents/repos/reactor-core"),
        ]

        for repo_dir in cross_repo_dirs:
            if not repo_dir.exists():
                continue

            # Look for orphaned lock files
            repo_lock_dir = repo_dir / ".jarvis" / "locks"
            if repo_lock_dir.exists():
                for lock_file in repo_lock_dir.glob("*.lock"):
                    try:
                        # Check if lock is stale by reading state
                        state_file = lock_file.with_suffix(".state")
                        if state_file.exists():
                            state_data = json.loads(state_file.read_text())
                            pid = state_data.get("pid")
                            if pid and not _is_pid_alive(pid):
                                # Dead process - clean up
                                lock_file.unlink(missing_ok=True)
                                state_file.unlink(missing_ok=True)
                                logger.debug(f"[v130.0] Cleaned cross-repo stale lock: {lock_file}")
                    except Exception as e:
                        logger.debug(f"[v130.0] Cross-repo cleanup warning: {e}")

    except Exception as e:
        logger.debug(f"[v130.0] Cross-repo cleanup error (non-fatal): {e}")


def _is_pid_alive(pid: int) -> bool:
    """v130.0: Check if a process ID is alive."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # Can't send signal but process exists


async def verify_lock_cleanup() -> bool:
    """
    v130.0: Verify that lock cleanup was successful.

    Returns:
        True if all lock files are cleaned, False if any remain
    """
    # Check all lock-related files
    files_to_check = [
        SUPERVISOR_LOCK_FILE,
        SUPERVISOR_STATE_FILE,
        SUPERVISOR_IPC_SOCKET,
    ]

    for f in files_to_check:
        if f.exists():
            # File exists - check if it's from a valid process
            if f == SUPERVISOR_STATE_FILE:
                try:
                    state_data = json.loads(f.read_text())
                    pid = state_data.get("pid")
                    if pid and _is_pid_alive(pid):
                        # Valid process owns this file
                        return True
                except Exception:
                    pass
            return False

    return True


async def execute_command_with_cleanup(
    command: str,
    args: Optional[Dict[str, Any]] = None,
    timeout: float = 10.0,
    auto_cleanup: bool = True,
    verify: bool = True
) -> CommandResult:
    """
    v130.0: Execute supervisor command with automatic pre-cleanup.

    This is the UNIFIED entry point for all supervisor commands. It:
    1. Cleans stale locks BEFORE executing the command
    2. Executes the command with proper error handling
    3. Verifies cleanup succeeded (for restart/shutdown)
    4. Handles all edge cases gracefully

    Args:
        command: Command to execute (status, restart, shutdown, health, etc.)
        args: Optional command arguments
        timeout: Command timeout in seconds
        auto_cleanup: Automatically clean stale locks before command
        verify: Verify cleanup after restart/shutdown commands

    Returns:
        CommandResult with comprehensive status
    """
    start_time = time.time()
    result = CommandResult(command)

    try:
        # Step 1: Auto-cleanup stale locks if enabled
        if auto_cleanup:
            cleanup_result = await cleanup_stale_locks(force=False, timeout=5.0)
            result.cleanup_result = cleanup_result

            if not cleanup_result.success:
                logger.warning(f"[v130.0] Pre-command cleanup warning: {cleanup_result.error}")
                # Continue anyway - cleanup failure shouldn't block commands

        # Step 2: Execute the command
        response = await send_supervisor_command(command, args=args, timeout=timeout)
        result.response = response

        # Determine success based on command type
        if command in ("restart", "shutdown"):
            # For restart/shutdown, connection drops are expected
            error = response.get("error", "")
            connection_dropped = any(indicator in error for indicator in [
                "Expecting value", "Connection", "EOF", "BrokenPipe",
                "timeout", "IPC", "No supervisor"
            ])

            result.success = (
                response.get("success") or
                response.get("restart_initiated") or
                response.get("shutdown_initiated") or
                connection_dropped
            )
        else:
            result.success = response.get("success", False)

        # Step 3: Verify cleanup for restart/shutdown
        if verify and command in ("restart", "shutdown") and result.success:
            await asyncio.sleep(0.5)  # Brief pause for cleanup to complete
            result.verification_passed = await verify_lock_cleanup()

    except Exception as e:
        result.success = False
        result.error = str(e)
        logger.error(f"[v130.0] Command {command} failed: {e}")

    result.duration_ms = (time.time() - start_time) * 1000
    return result


async def execute_combined_commands(
    commands: List[str],
    args: Optional[Dict[str, Any]] = None,
    wait_between: float = 2.0
) -> Dict[str, CommandResult]:
    """
    v130.0: Execute multiple commands in sequence with proper handling.

    Supports combined command patterns like:
    - --restart --status: Restart then show status
    - --shutdown --cleanup: Shutdown and clean orphans

    Args:
        commands: List of commands to execute in order
        args: Optional arguments to pass to all commands
        wait_between: Seconds to wait between commands

    Returns:
        Dict mapping command name to its result
    """
    results: Dict[str, CommandResult] = {}

    # Clean stale locks once before all commands
    cleanup_result = await cleanup_stale_locks(force=False)

    for i, command in enumerate(commands):
        result = await execute_command_with_cleanup(
            command=command,
            args=args,
            auto_cleanup=False,  # Already cleaned
            verify=(command in ("restart", "shutdown"))
        )
        result.cleanup_result = cleanup_result if i == 0 else None
        results[command] = result

        # Wait between commands if more to come
        if i < len(commands) - 1 and wait_between > 0:
            await asyncio.sleep(wait_between)

    return results


async def force_cleanup_hung_supervisor(timeout: float = 10.0) -> LockCleanupResult:
    """
    v130.0: Force cleanup for completely hung supervisors.

    Use this when normal cleanup fails and the supervisor is completely
    unresponsive. This will:
    1. Send SIGKILL to the supervisor process
    2. Force remove all lock files
    3. Clean up all IPC resources
    4. Verify cleanup succeeded

    Args:
        timeout: Maximum time to wait for cleanup

    Returns:
        LockCleanupResult with cleanup details
    """
    logger.warning("[v130.0] Force cleanup of hung supervisor initiated")
    return await cleanup_stale_locks(force=True, timeout=timeout, cross_repo=True)


def cleanup_stale_locks_sync(force: bool = False) -> LockCleanupResult:
    """
    v130.0: Synchronous wrapper for cleanup_stale_locks.

    Use this in contexts where async is not available (signal handlers, atexit).
    """
    try:
        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(cleanup_stale_locks(force=force))
        loop.close()
        return result
    except Exception as e:
        result = LockCleanupResult()
        result.success = False
        result.error = str(e)
        return result


# =============================================================================
# v109.4: SIGHUP HANDLER FOR CLEAN RESTART VIA os.execv()
# =============================================================================

_sighup_handler_installed = False


def _setup_sighup_handler() -> None:
    """
    v109.4: Set up SIGHUP handler for clean restart via os.execv().

    This is the PRIMARY FIX for the EXC_GUARD crash on restart.

    Why os.execv() is the right solution:
    - os.execv() replaces the current process image with a new one
    - atexit handlers are NOT called (process is replaced, not exited)
    - No file descriptor cleanup race conditions
    - Clean slate for the new process
    - Standard Unix pattern for daemon restarts

    The EXC_GUARD crash happened because:
    1. Restart sent SIGHUP which triggered atexit handlers
    2. atexit handlers tried to create new event loops during interpreter shutdown
    3. GCP client libraries use guarded FDs (libdispatch/GCD on macOS)
    4. Attempting to close guarded FDs during shutdown triggers EXC_GUARD

    By using os.execv(), we bypass atexit entirely and avoid the crash.
    """
    global _sighup_handler_installed

    if _sighup_handler_installed:
        return

    def _handle_sighup(signum, frame):
        """
        v120.0: Handle SIGHUP for restart with RestartMarker coordination.

        CRITICAL: This handler must NOT:
        - Create new event loops
        - Import modules that use guarded FDs
        - Call async code
        - Run complex cleanup

        It simply releases the lock, updates the RestartMarker, and replaces the process.
        """
        logger.info("[Singleton] SIGHUP received - initiating clean restart via os.execv()")

        # v120.0: Update RestartMarker phase to signal_sent
        try:
            RestartMarker.update_phase("signal_sent")
        except Exception as e:
            logger.debug(f"RestartMarker signal_sent update failed (non-fatal): {e}")

        # Step 1: Release file lock BEFORE execv (sync, no event loop needed)
        try:
            if _singleton and _singleton._lock_fd is not None:
                # Just unlock - don't close FD, let execv/kernel handle it
                try:
                    fcntl.flock(_singleton._lock_fd, fcntl.LOCK_UN)
                    logger.debug("[Singleton] Lock released for restart")
                except Exception as e:
                    logger.debug(f"Lock unlock warning: {e}")
                _singleton._lock_fd = None
        except Exception as e:
            logger.debug(f"Lock release warning during SIGHUP: {e}")

        # Step 2: Clean up state file so new process starts fresh
        try:
            SUPERVISOR_STATE_FILE.unlink(missing_ok=True)
            logger.debug("[Singleton] State file removed for restart")
        except Exception as e:
            logger.debug(f"State file cleanup warning: {e}")

        # Step 3: Clean up IPC socket
        try:
            if SUPERVISOR_IPC_SOCKET.exists():
                SUPERVISOR_IPC_SOCKET.unlink()
                logger.debug("[Singleton] IPC socket removed for restart")
        except Exception as e:
            logger.debug(f"IPC socket cleanup warning: {e}")

        # Step 4: v109.7: Comprehensive Trinity cleanup before restart
        # Kill ALL Trinity-related processes, not just our children.
        # This handles orphaned processes from previous crashed sessions.
        try:
            import psutil
            current_pid = os.getpid()
            current_ppid = os.getppid()

            # Trinity ports that MUST be free for restart
            TRINITY_PORTS = [8000, 8010, 8090]  # J-Prime, JARVIS, Reactor-Core

            # Step 4a: Kill our direct children EXCEPT registered Trinity services
            # v117.0: Check GlobalProcessRegistry BEFORE killing to preserve Trinity services
            try:
                current_proc = psutil.Process(current_pid)
                children = current_proc.children(recursive=True)
                if children:
                    # v117.0: Separate children into: (a) registered Trinity services to KEEP
                    #         and (b) internal children to terminate
                    children_to_terminate = []
                    children_to_preserve = []

                    for child in children:
                        if GlobalProcessRegistry.is_ours(child.pid):
                            # This is a registered Trinity service - PRESERVE IT
                            children_to_preserve.append(child)
                            logger.info(
                                f"[Singleton] v117.0: PRESERVING registered Trinity service "
                                f"PID {child.pid} during restart"
                            )
                        else:
                            # This is an internal child - terminate it
                            children_to_terminate.append(child)

                    if children_to_terminate:
                        logger.info(
                            f"[Singleton] Terminating {len(children_to_terminate)} internal child(ren), "
                            f"preserving {len(children_to_preserve)} registered service(s)"
                        )
                        for child in children_to_terminate:
                            try:
                                child.terminate()
                            except (psutil.NoSuchProcess, psutil.AccessDenied):
                                pass
                        _, alive = psutil.wait_procs(children_to_terminate, timeout=2.0)
                        for proc in alive:
                            try:
                                proc.kill()
                            except (psutil.NoSuchProcess, psutil.AccessDenied):
                                pass
                    else:
                        logger.info(
                            f"[Singleton] No internal children to terminate, "
                            f"preserving {len(children_to_preserve)} registered service(s)"
                        )
            except Exception as e:
                logger.debug(f"Child cleanup warning: {e}")

            # Step 4b: Kill any process holding Trinity ports (orphans from prev session)
            # v116.0: CRITICAL FIX - Check GlobalProcessRegistry before killing!
            # This prevents killing processes spawned by THIS session.
            killed_ports = []
            skipped_our_processes = []
            try:
                # v117.0: Get set of child PIDs we just terminated (not preserved services)
                terminated_child_pids = {c.pid for c in children_to_terminate} if 'children_to_terminate' in dir() and children_to_terminate else set()

                for conn in psutil.net_connections(kind='inet'):
                    if conn.laddr.port in TRINITY_PORTS and conn.pid:
                        pid = conn.pid
                        if pid in (current_pid, current_ppid):
                            continue  # Don't kill ourselves or parent

                        # v116.0: Skip if this PID was already terminated as a child
                        if pid in terminated_child_pids:
                            logger.debug(f"[Singleton] PID {pid} already terminated as child")
                            continue

                        # v116.0: CRITICAL - Check if this is OUR process
                        if GlobalProcessRegistry.is_ours(pid):
                            logger.info(
                                f"[Singleton] v116.0: SKIPPING port {conn.laddr.port} PID {pid} "
                                f"(registered as OUR process)"
                            )
                            skipped_our_processes.append((pid, conn.laddr.port))
                            continue

                        try:
                            proc = psutil.Process(pid)
                            cmdline = " ".join(proc.cmdline())

                            # Only kill JARVIS-related processes
                            if any(p in cmdline.lower() for p in
                                   ['jarvis', 'uvicorn', 'trinity_orchestrator', 'reactor']):
                                logger.info(
                                    f"[Singleton] Killing ORPHAN on port {conn.laddr.port}: "
                                    f"PID {pid} (not in registry)"
                                )
                                try:
                                    os.kill(pid, signal.SIGTERM)
                                    killed_ports.append(conn.laddr.port)
                                except (ProcessLookupError, OSError):
                                    pass
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            pass
            except (psutil.AccessDenied, PermissionError):
                pass
            except Exception as e:
                logger.debug(f"Port cleanup warning: {e}")

            # v116.0: Log summary of port cleanup
            if killed_ports:
                # Wait for ports to be released
                import time
                time.sleep(0.5)
                logger.info(f"[Singleton] Freed orphan ports: {killed_ports}")

            if skipped_our_processes:
                logger.info(
                    f"[Singleton] v116.0: Preserved {len(skipped_our_processes)} OUR processes "
                    f"(PIDs: {[p[0] for p in skipped_our_processes]})"
                )

            # v117.0: DO NOT clear the registry on restart!
            # Preserved Trinity services should remain registered so the new supervisor
            # knows about them via load_from_disk() and doesn't spawn duplicates.
            # Only clear registry on FRESH start (not restart via os.execv).
            logger.debug("[Singleton] v117.0: Keeping registry for restart (preserved services remain registered)")

        except ImportError:
            # psutil not available - try basic approach
            try:
                import subprocess
                subprocess.run(['pkill', '-P', str(os.getpid())], timeout=5, capture_output=True)
            except Exception:
                pass
        except Exception as e:
            logger.debug(f"Trinity cleanup warning: {e}")

        # v120.0: Stop ThreadedIPCServer before execv (sync, thread-safe)
        try:
            if _singleton and hasattr(_singleton, '_threaded_ipc') and _singleton._threaded_ipc:
                _singleton._threaded_ipc.stop()
                logger.debug("[Singleton] v120.0: ThreadedIPCServer stopped for restart")
        except Exception as e:
            logger.debug(f"ThreadedIPCServer stop warning: {e}")

        # Step 5: Restart via execv - this REPLACES the process (atexit NOT called)
        # v109.6: Filter out command flags that would cause restart loop
        # Without this, the new process sees --restart and tries to IPC restart itself!
        python = sys.executable
        filtered_argv = [arg for arg in sys.argv
                         if arg not in ('--restart', '--shutdown', '--takeover', '--status', '--force')]
        args = [python] + filtered_argv
        logger.info(f"[Singleton] Executing: {' '.join(args[:5])}...")

        # v120.0: Update RestartMarker phase right before execv
        try:
            RestartMarker.update_phase("execv_triggered")
        except Exception as e:
            logger.debug(f"RestartMarker execv_triggered update failed (non-fatal): {e}")

        # v119.5: Add explicit error handling for os.execv()
        # In signal handlers, exceptions can be silently swallowed
        try:
            # This replaces the current process image - code after this never runs
            os.execv(python, args)
            # UNREACHABLE: process has been replaced
        except Exception as e:
            # os.execv() failed - this is a critical error
            # v120.0: Update RestartMarker with failure
            try:
                RestartMarker.fail(f"os.execv() failed: {e}")
            except Exception:
                pass

            # Write to stderr and a fallback file since logging may not work
            import traceback
            error_msg = f"[Singleton] CRITICAL: os.execv() FAILED: {e}\n{traceback.format_exc()}"
            sys.stderr.write(error_msg)
            sys.stderr.flush()
            try:
                with open("/tmp/jarvis_execv_error.log", "a") as f:
                    f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} - {error_msg}\n")
            except Exception:
                pass
            # Exit cleanly since we can't restart
            os._exit(1)

    # Install the handler
    signal.signal(signal.SIGHUP, _handle_sighup)
    _sighup_handler_installed = True
    logger.debug("[Singleton] v109.4: SIGHUP handler installed for clean restart")


def setup_restart_handlers() -> None:
    """
    v109.4: Set up all restart-related signal handlers.

    Call this after acquiring the supervisor lock.
    """
    _setup_sighup_handler()


# =============================================================================
# v109.4: ATEXIT CLEANUP (MINIMAL - most restart bypasses this via os.execv())
# =============================================================================

import atexit


def _cleanup_on_exit():
    """
    Clean up lock and IPC socket on exit.

    NOTE: For --restart, this is NOT called because os.execv() bypasses atexit.
    This only runs for normal exit, SIGTERM, or SIGINT.

    v109.4: Keep this minimal to avoid EXC_GUARD crashes during interpreter shutdown.
    """
    try:
        if _singleton:
            if _singleton.is_locked():
                # Use minimal sync release - no async, no new imports
                try:
                    if _singleton._lock_fd is not None:
                        fcntl.flock(_singleton._lock_fd, fcntl.LOCK_UN)
                        # Don't close FD during atexit - kernel will clean up
                        _singleton._lock_fd = None
                except Exception:
                    pass

            # Clean up IPC socket
            try:
                _singleton.cleanup_ipc()
            except Exception:
                pass
    except Exception:
        pass


atexit.register(_cleanup_on_exit)
