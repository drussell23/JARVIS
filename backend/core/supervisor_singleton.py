#!/usr/bin/env python3
"""
JARVIS Supervisor Singleton v1.0
================================

Enterprise-grade singleton enforcement for the JARVIS system.
Prevents multiple supervisors/entry points from running simultaneously.

This module provides:
1. Cross-process PID file locking with stale detection
2. Process tree awareness (handles forks and child processes)
3. Atomic file operations for reliability
4. Graceful conflict resolution

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

Author: JARVIS System
Version: 1.0.0 (January 2026)
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, Tuple

logger = logging.getLogger(__name__)

# Lock file location
LOCK_DIR = Path.home() / ".jarvis" / "locks"
SUPERVISOR_LOCK_FILE = LOCK_DIR / "supervisor.lock"
SUPERVISOR_STATE_FILE = LOCK_DIR / "supervisor.state"

# Stale lock detection threshold (seconds)
STALE_LOCK_THRESHOLD = 300  # 5 minutes without heartbeat = stale

# Heartbeat interval
HEARTBEAT_INTERVAL = 10  # seconds


@dataclass
class SupervisorState:
    """State information for the running supervisor."""
    pid: int
    entry_point: str  # "run_supervisor" or "start_system"
    started_at: str
    last_heartbeat: str
    hostname: str
    working_dir: str
    python_version: str
    command_line: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> SupervisorState:
        return cls(**data)

    @classmethod
    def create_current(cls, entry_point: str) -> SupervisorState:
        """Create state for current process."""
        import socket
        return cls(
            pid=os.getpid(),
            entry_point=entry_point,
            started_at=datetime.now().isoformat(),
            last_heartbeat=datetime.now().isoformat(),
            hostname=socket.gethostname(),
            working_dir=str(Path.cwd()),
            python_version=sys.version.split()[0],
            command_line=" ".join(sys.argv),
        )


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
        """Read current supervisor state from file."""
        try:
            if SUPERVISOR_STATE_FILE.exists():
                data = json.loads(SUPERVISOR_STATE_FILE.read_text())
                return SupervisorState.from_dict(data)
        except Exception as e:
            logger.debug(f"Could not read state file: {e}")
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
        Check if the existing lock is stale.

        Returns:
            (is_stale, existing_state)
        """
        state = self._read_state()
        if state is None:
            return True, None

        # Check if process is alive
        if not self._is_process_alive(state.pid):
            logger.info(f"[Singleton] Lock holder PID {state.pid} is dead")
            return True, state

        # Check if it's a JARVIS process
        if not self._is_jarvis_process(state.pid):
            logger.info(f"[Singleton] PID {state.pid} is not a JARVIS process")
            return True, state

        # Check heartbeat age
        try:
            last_heartbeat = datetime.fromisoformat(state.last_heartbeat)
            age = (datetime.now() - last_heartbeat).total_seconds()
            if age > STALE_LOCK_THRESHOLD:
                logger.info(f"[Singleton] Lock stale: no heartbeat for {age:.0f}s")
                return True, state
        except Exception:
            pass

        return False, state

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

    async def start_heartbeat(self) -> None:
        """Start the heartbeat task to keep lock fresh."""
        async def heartbeat_loop():
            while True:
                try:
                    if self._state:
                        self._state.last_heartbeat = datetime.now().isoformat()
                        self._write_state(self._state)
                except Exception as e:
                    logger.debug(f"Heartbeat error: {e}")
                await asyncio.sleep(HEARTBEAT_INTERVAL)

        self._heartbeat_task = asyncio.create_task(heartbeat_loop())

    def is_locked(self) -> bool:
        """Check if we hold the lock."""
        return self._lock_fd is not None

    def get_state(self) -> Optional[SupervisorState]:
        """Get current state."""
        return self._state


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
    Check if a supervisor is already running.

    Returns:
        (is_running, state_dict or None)
    """
    singleton = get_singleton()
    is_stale, state = singleton._is_lock_stale()

    if state and not is_stale:
        return True, state.to_dict()
    return False, None


# Atexit cleanup
import atexit

def _cleanup_on_exit():
    """Clean up lock on exit."""
    try:
        if _singleton and _singleton.is_locked():
            _singleton.release()
    except Exception:
        pass

atexit.register(_cleanup_on_exit)
