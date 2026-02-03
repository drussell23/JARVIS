"""
Async I/O Utilities for Non-Blocking Operations
================================================

This module provides async-safe wrapper utilities for blocking I/O operations
to ensure the event loop is never blocked during startup or other async contexts.

These utilities are designed to be generic, type-safe, and composable.

Key Functions:
    - run_sync: Generic wrapper for any blocking function
    - path_exists: Async-safe file/directory existence check
    - read_file: Async-safe file reading
    - run_subprocess: Async subprocess execution with timeout support

Usage:
    from backend.utils.async_io import (
        run_sync,
        path_exists,
        read_file,
        run_subprocess,
    )

    # Wrap any blocking function
    result = await run_sync(os.listdir, "/some/path")

    # Check if path exists
    if await path_exists("/some/file.txt"):
        content = await read_file("/some/file.txt")

    # Run subprocess with timeout
    result = await run_subprocess(["ls", "-la"], timeout=5.0)

Design Notes:
    - run_sync uses asyncio.to_thread() for Python 3.9+ compatibility
    - run_subprocess uses asyncio.create_subprocess_exec() for true async I/O
    - All functions are type-safe with proper generics
    - Errors from blocking functions are propagated to the caller
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from typing import Any, Callable, TypeVar, Union

# Type variable for generic return type
T = TypeVar("T")


# =============================================================================
# Generic Async Wrapper
# =============================================================================


async def run_sync(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """
    Execute a blocking synchronous function without blocking the event loop.

    This function wraps any blocking operation and runs it in a thread pool
    executor, allowing the event loop to continue processing other tasks.

    Args:
        func: The blocking function to execute
        *args: Positional arguments to pass to the function
        **kwargs: Keyword arguments to pass to the function

    Returns:
        The return value of the function

    Raises:
        Any exception raised by the function is propagated to the caller

    Example:
        # Wrap a blocking file system operation
        files = await run_sync(os.listdir, "/some/path")

        # Wrap a blocking function with kwargs
        result = await run_sync(my_func, arg1, arg2, kwarg1="value")

        # Wrap psutil blocking calls
        cpu_percent = await run_sync(psutil.cpu_percent, interval=0.1)
    """
    # asyncio.to_thread handles both args and kwargs properly
    # and runs the function in the default executor
    return await asyncio.to_thread(func, *args, **kwargs)


# =============================================================================
# File System Utilities
# =============================================================================


async def path_exists(path: Union[str, Path]) -> bool:
    """
    Check if a path exists without blocking the event loop.

    This is an async-safe wrapper around os.path.exists().

    Args:
        path: The file or directory path to check (str or Path)

    Returns:
        True if the path exists, False otherwise

    Example:
        if await path_exists("/var/log/jarvis.log"):
            print("Log file exists")

        # Also works with Path objects
        from pathlib import Path
        if await path_exists(Path.home() / ".config"):
            print("Config directory exists")
    """
    # Convert Path to string for os.path.exists compatibility
    path_str = str(path) if isinstance(path, Path) else path
    return await run_sync(os.path.exists, path_str)


async def read_file(
    path: Union[str, Path],
    encoding: str = "utf-8",
) -> str:
    """
    Read a file's contents without blocking the event loop.

    This is an async-safe wrapper for reading text files.

    Args:
        path: The file path to read (str or Path)
        encoding: The file encoding (default: utf-8)

    Returns:
        The file contents as a string

    Raises:
        FileNotFoundError: If the file doesn't exist
        PermissionError: If the file cannot be read due to permissions
        UnicodeDecodeError: If the file cannot be decoded with the specified encoding

    Example:
        content = await read_file("/etc/hostname")

        # With custom encoding
        content = await read_file("data.txt", encoding="latin-1")

        # With Path object
        content = await read_file(Path.home() / ".bashrc")
    """
    path_obj = Path(path) if isinstance(path, str) else path

    def _read() -> str:
        return path_obj.read_text(encoding=encoding)

    return await run_sync(_read)


# =============================================================================
# Subprocess Utilities
# =============================================================================


async def run_subprocess(
    cmd: list[str],
    timeout: float | None = None,
    **kwargs: Any,
) -> subprocess.CompletedProcess:
    """
    Run a subprocess asynchronously without blocking the event loop.

    This function uses asyncio.create_subprocess_exec() for true async I/O,
    which is more efficient than running subprocess.run() in a thread.

    Args:
        cmd: Command and arguments as a list of strings
        timeout: Maximum execution time in seconds. If exceeded, raises
                 asyncio.TimeoutError. If None, no timeout is applied.
        **kwargs: Additional arguments passed to asyncio.create_subprocess_exec().
                  Common kwargs include:
                  - cwd: Working directory for the subprocess
                  - env: Environment variables dict
                  - stdin: Standard input (default: None)

    Returns:
        subprocess.CompletedProcess with:
        - args: The command that was executed
        - returncode: Exit code of the subprocess
        - stdout: Captured stdout as bytes
        - stderr: Captured stderr as bytes

    Raises:
        asyncio.TimeoutError: If the command exceeds the specified timeout
        FileNotFoundError: If the command executable is not found
        PermissionError: If the command cannot be executed due to permissions

    Example:
        # Basic usage
        result = await run_subprocess(["ls", "-la"])
        print(result.stdout.decode())

        # With timeout
        try:
            result = await run_subprocess(["sleep", "10"], timeout=2.0)
        except asyncio.TimeoutError:
            print("Command timed out")

        # With custom working directory
        result = await run_subprocess(["pwd"], cwd="/tmp")
    """
    # Always capture stdout and stderr
    kwargs.setdefault("stdout", asyncio.subprocess.PIPE)
    kwargs.setdefault("stderr", asyncio.subprocess.PIPE)

    # Create the subprocess
    process = await asyncio.create_subprocess_exec(*cmd, **kwargs)

    try:
        # Wait for completion with optional timeout
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        # Kill the process on timeout
        try:
            process.kill()
            await process.wait()
        except ProcessLookupError:
            pass  # Process already terminated
        raise

    # Return a CompletedProcess for compatibility with subprocess.run()
    # Note: After communicate() completes, returncode is guaranteed to be set
    returncode = process.returncode if process.returncode is not None else -1
    return subprocess.CompletedProcess(
        args=cmd,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


# =============================================================================
# Exports
# =============================================================================

__all__ = [
    "run_sync",
    "path_exists",
    "read_file",
    "run_subprocess",
]
