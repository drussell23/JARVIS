"""
Async Startup Utilities for Non-Blocking Operations
====================================================

This module provides async wrappers for blocking operations commonly used during
JARVIS startup. By running blocking operations in a dedicated bounded ThreadPoolExecutor,
we ensure the event loop remains responsive for:
- Progress broadcasts
- Health checks
- WebSocket heartbeats
- Concurrent startup operations

The dedicated executor is bounded (max_workers=4) to prevent exhausting the default
thread pool when many startup operations run concurrently.

Usage:
    from backend.utils.async_startup import (
        async_process_wait,
        async_subprocess_run,
        async_check_port,
        async_file_read,
        async_json_read,
    )

    # Wait for process without blocking
    finished = await async_process_wait(pid, timeout=10.0)

    # Run subprocess without blocking
    result = await async_subprocess_run(["ls", "-la"], timeout=5.0)

    # Check if port is listening without blocking
    available = await async_check_port("localhost", 8080, timeout=1.0)

    # Read file without blocking
    content = await async_file_read("/path/to/file.txt")

    # Read JSON without blocking
    data = await async_json_read("/path/to/config.json")

Following 35-point checklist items:
- Item 1-2: Event loop never blocked by sync calls
- Item 9: Bounded executor (max 4 workers)
- Item 32: Dedicated startup executor
"""

from __future__ import annotations

import asyncio
import atexit
import json
import logging
import os
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

logger = logging.getLogger(__name__)

# =============================================================================
# DEDICATED STARTUP EXECUTOR
# =============================================================================

# Bounded executor for startup blocking operations (prevents exhausting default pool)
# v242.4: Daemon thread factory — prevents startup_async__0 from blocking
# process exit. Non-daemon workers forced os._exit(1) which skips atexit handlers.
# v242.5: Fixed — must set daemon BEFORE Thread.start(), not after.
# Previous patch tried to set daemon on already-started threads →
# RuntimeError: "cannot set daemon status of active thread".
_STARTUP_EXECUTOR = ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix="startup_async_"
)

try:
    import concurrent.futures.thread as _cft_module
    import threading as _threading
    import weakref as _weakref

    def _make_daemon_adjuster(exc):
        def _adjust():
            if exc._idle_semaphore.acquire(timeout=0):
                return
            def weakref_cb(_, q=exc._work_queue):
                q.put(None)
            num_threads = len(exc._threads)
            if num_threads < exc._max_workers:
                t = _threading.Thread(
                    target=_cft_module._worker,
                    args=(
                        _weakref.ref(exc, weakref_cb),
                        exc._work_queue,
                        exc._initializer,
                        exc._initargs,
                    ),
                    name=f"{exc._thread_name_prefix or 'pool'}_{num_threads}",
                )
                t.daemon = True  # Set BEFORE start (key fix)
                t.start()
                exc._threads.add(t)
                _cft_module._threads_queues[t] = exc._work_queue
        return _adjust

    _STARTUP_EXECUTOR._adjust_thread_count = _make_daemon_adjuster(_STARTUP_EXECUTOR)
except (ImportError, AttributeError):
    logger.debug("[async_startup] CPython thread internals unavailable — daemon patch skipped")


def shutdown_startup_executor() -> None:
    """
    Shutdown the startup executor.

    Call this during application shutdown to clean up executor threads.
    Safe to call multiple times.
    """
    try:
        _STARTUP_EXECUTOR.shutdown(wait=False)
        logger.debug("[async_startup] Startup executor shutdown complete")
    except Exception as e:
        logger.warning(f"[async_startup] Error shutting down executor: {e}")


# Register cleanup on process exit
atexit.register(shutdown_startup_executor)


# =============================================================================
# RESULT DATACLASS
# =============================================================================


@dataclass
class SubprocessResult:
    """
    Result of an async subprocess execution.

    Attributes:
        returncode: Exit code (None if timed out or error)
        stdout: Captured stdout bytes (None if not captured)
        stderr: Captured stderr bytes (None if not captured)
        command: The command that was executed
        duration_seconds: How long the subprocess took
        timed_out: Whether the process was killed due to timeout
        error: Exception message if an error occurred
    """
    returncode: Optional[int] = None
    stdout: Optional[bytes] = None
    stderr: Optional[bytes] = None
    command: List[str] = field(default_factory=list)
    duration_seconds: float = 0.0
    timed_out: bool = False
    error: Optional[str] = None

    @property
    def success(self) -> bool:
        """Check if subprocess completed successfully."""
        return self.returncode == 0 and not self.timed_out and self.error is None


# =============================================================================
# PROCESS WAIT UTILITIES
# =============================================================================


def _blocking_process_wait(pid: int, timeout: float) -> bool:
    """
    Blocking wait for a process to exit.

    This runs in the executor thread. Uses os.kill(pid, 0) polling
    since we may not have a process handle.

    Returns:
        True if process exited, False if timeout or process doesn't exist
    """
    deadline = time.monotonic() + timeout
    poll_interval = 0.05  # 50ms polling

    while time.monotonic() < deadline:
        try:
            # os.kill with signal 0 checks if process exists
            os.kill(pid, 0)
            # Process still running, wait and retry
            time.sleep(poll_interval)
        except ProcessLookupError:
            # Process no longer exists - it exited
            return True
        except PermissionError:
            # Process exists but we can't signal it (running as different user)
            # Still means it's running
            time.sleep(poll_interval)
        except OSError:
            # Other OS error, treat as process gone
            return True

    # Timeout reached, process still running
    return False


async def async_process_wait(pid: int, timeout: float = 10.0) -> bool:
    """
    Wait for a process to exit without blocking the event loop.

    Args:
        pid: Process ID to wait for
        timeout: Maximum time to wait in seconds

    Returns:
        True if process exited, False if timed out or doesn't exist
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _STARTUP_EXECUTOR,
        _blocking_process_wait,
        pid,
        timeout
    )


def _blocking_psutil_wait(proc: Any, timeout: float) -> bool:
    """
    Blocking wait using psutil.Process.wait().

    Args:
        proc: psutil.Process instance
        timeout: Maximum time to wait

    Returns:
        True if process exited, False if timed out
    """
    try:
        proc.wait(timeout=timeout)
        return True
    except Exception:
        # TimeoutExpired, NoSuchProcess, or other error
        return False


async def async_psutil_wait(proc: Any, timeout: float = 10.0) -> bool:
    """
    Wait for a psutil.Process to exit without blocking the event loop.

    Args:
        proc: psutil.Process instance
        timeout: Maximum time to wait in seconds

    Returns:
        True if process exited, False if timed out
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _STARTUP_EXECUTOR,
        _blocking_psutil_wait,
        proc,
        timeout
    )


# =============================================================================
# SUBPROCESS UTILITIES
# =============================================================================


def _blocking_subprocess_run(
    cmd: Sequence[str],
    timeout: float,
    cwd: Optional[str],
    env: Optional[Dict[str, str]],
) -> SubprocessResult:
    """
    Blocking subprocess.run wrapper.

    This runs in the executor thread.
    """
    import subprocess

    cmd_list = list(cmd)
    result = SubprocessResult(command=cmd_list)
    start_time = time.monotonic()

    try:
        completed = subprocess.run(
            cmd_list,
            capture_output=True,
            timeout=timeout,
            cwd=cwd,
            env=env,
        )
        result.returncode = completed.returncode
        result.stdout = completed.stdout
        result.stderr = completed.stderr
    except subprocess.TimeoutExpired as e:
        result.timed_out = True
        result.error = f"Command timed out after {timeout}s"
        result.stdout = e.stdout
        result.stderr = e.stderr
    except Exception as e:
        result.error = str(e)

    result.duration_seconds = time.monotonic() - start_time
    return result


async def async_subprocess_run(
    cmd: Sequence[str],
    timeout: float = 30.0,
    cwd: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
) -> SubprocessResult:
    """
    Run a subprocess without blocking the event loop.

    Uses the dedicated startup executor to run subprocess.run() in a thread.

    Args:
        cmd: Command and arguments to execute
        timeout: Maximum execution time in seconds
        cwd: Working directory for the subprocess
        env: Environment variables (None to inherit)

    Returns:
        SubprocessResult with returncode, stdout, stderr, etc.

    Example:
        result = await async_subprocess_run(["ls", "-la"], timeout=5.0)
        if result.success:
            print(result.stdout.decode())
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _STARTUP_EXECUTOR,
        _blocking_subprocess_run,
        cmd,
        timeout,
        cwd,
        env,
    )


# =============================================================================
# SOCKET CHECK UTILITIES
# =============================================================================


def _blocking_check_port(host: str, port: int, timeout: float) -> bool:
    """
    Blocking check if a TCP port is listening.

    Returns True if connection succeeds (something listening), False otherwise.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            result = sock.connect_ex((host, port))
            return result == 0
    except Exception:
        return False


async def async_check_port(host: str, port: int, timeout: float = 1.0) -> bool:
    """
    Check if a TCP port is listening without blocking the event loop.

    Args:
        host: Host address to check
        port: Port number to check
        timeout: Connection timeout in seconds

    Returns:
        True if something is listening on the port, False otherwise

    Example:
        if await async_check_port("localhost", 8080):
            print("Server is ready")
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _STARTUP_EXECUTOR,
        _blocking_check_port,
        host,
        port,
        timeout,
    )


def _blocking_check_unix_socket(socket_path: str, timeout: float) -> bool:
    """
    Blocking check if a Unix socket is listening.

    Returns True if connection succeeds, False otherwise.
    """
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            result = sock.connect_ex(socket_path)
            return result == 0
    except Exception:
        return False


async def async_check_unix_socket(socket_path: str, timeout: float = 1.0) -> bool:
    """
    Check if a Unix socket is listening without blocking the event loop.

    Args:
        socket_path: Path to the Unix socket
        timeout: Connection timeout in seconds

    Returns:
        True if something is listening on the socket, False otherwise

    Example:
        if await async_check_unix_socket("/tmp/jarvis.sock"):
            print("Socket server is ready")
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _STARTUP_EXECUTOR,
        _blocking_check_unix_socket,
        socket_path,
        timeout,
    )


# =============================================================================
# FILE I/O UTILITIES
# =============================================================================


def _blocking_file_read(path: str, encoding: str) -> str:
    """Blocking file read. Raises FileNotFoundError if file doesn't exist."""
    with open(path, "r", encoding=encoding) as f:
        return f.read()


async def async_file_read(path: str, encoding: str = "utf-8") -> str:
    """
    Read a file without blocking the event loop.

    Args:
        path: Path to the file to read
        encoding: File encoding (default: utf-8)

    Returns:
        File contents as a string

    Raises:
        FileNotFoundError: If the file doesn't exist

    Example:
        content = await async_file_read("/path/to/config.txt")
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _STARTUP_EXECUTOR,
        _blocking_file_read,
        path,
        encoding,
    )


def _blocking_file_write(path: str, content: str, encoding: str) -> None:
    """Blocking file write."""
    # Ensure parent directory exists
    parent = Path(path).parent
    parent.mkdir(parents=True, exist_ok=True)

    with open(path, "w", encoding=encoding) as f:
        f.write(content)


async def async_file_write(path: str, content: str, encoding: str = "utf-8") -> None:
    """
    Write a file without blocking the event loop.

    Creates parent directories if they don't exist.

    Args:
        path: Path to the file to write
        content: Content to write
        encoding: File encoding (default: utf-8)

    Example:
        await async_file_write("/path/to/output.txt", "Hello, World!")
    """
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        _STARTUP_EXECUTOR,
        _blocking_file_write,
        path,
        content,
        encoding,
    )


def _blocking_json_read(path: str) -> Dict[str, Any]:
    """Blocking JSON read. Raises FileNotFoundError or JSONDecodeError."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


async def async_json_read(path: str) -> Dict[str, Any]:
    """
    Read a JSON file without blocking the event loop.

    Args:
        path: Path to the JSON file

    Returns:
        Parsed JSON data as a dictionary

    Raises:
        FileNotFoundError: If the file doesn't exist
        json.JSONDecodeError: If the file is not valid JSON

    Example:
        config = await async_json_read("/path/to/config.json")
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _STARTUP_EXECUTOR,
        _blocking_json_read,
        path,
    )


def _blocking_json_write(path: str, data: Any) -> None:
    """Blocking JSON write."""
    # Ensure parent directory exists
    parent = Path(path).parent
    parent.mkdir(parents=True, exist_ok=True)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)


async def async_json_write(path: str, data: Any) -> None:
    """
    Write a JSON file without blocking the event loop.

    Creates parent directories if they don't exist.

    Args:
        path: Path to the JSON file
        data: Data to serialize as JSON

    Example:
        await async_json_write("/path/to/output.json", {"key": "value"})
    """
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        _STARTUP_EXECUTOR,
        _blocking_json_write,
        path,
        data,
    )


# =============================================================================
# EXPORTS
# =============================================================================

__all__ = [
    # Process wait
    "async_process_wait",
    "async_psutil_wait",
    # Subprocess
    "async_subprocess_run",
    "SubprocessResult",
    # Socket checks
    "async_check_port",
    "async_check_unix_socket",
    # File I/O
    "async_file_read",
    "async_file_write",
    "async_json_read",
    "async_json_write",
    # Executor management
    "shutdown_startup_executor",
    # For testing
    "_STARTUP_EXECUTOR",
]
