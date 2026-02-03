"""
JARVIS Backend Utilities
========================

This package provides utility modules for the JARVIS backend, including:
- Async startup utilities for non-blocking operations
- Async lock wrappers for cross-repo locking
- Audio processing utilities
- Model loading and caching utilities
- Various helper functions

Usage:
    from backend.utils import (
        async_process_wait,
        async_subprocess_run,
        async_check_port,
        StartupFileLock,
        CrossRepoLockManager,
    )
"""

# Async startup utilities - run blocking ops without blocking the event loop
from backend.utils.async_startup import (
    # Process wait
    async_process_wait,
    async_psutil_wait,
    # Subprocess
    async_subprocess_run,
    SubprocessResult,
    # Socket checks
    async_check_port,
    async_check_unix_socket,
    # File I/O
    async_file_read,
    async_file_write,
    async_json_read,
    async_json_write,
    # Executor management
    shutdown_startup_executor,
    # Dedicated executor (for testing/advanced use)
    _STARTUP_EXECUTOR,
)

# Async lock wrapper - non-blocking file locks with stale detection
from backend.utils.async_lock_wrapper import (
    StartupFileLock,
    CrossRepoLockManager,
    MAX_LOCK_TIMEOUT,
    MIN_LOCK_TIMEOUT,
    DEFAULT_LOCK_TIMEOUT,
    STALE_LOCK_RETRY_TIMEOUT,
)


__all__ = [
    # === Async Startup Utilities ===
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
    "_STARTUP_EXECUTOR",
    # === Async Lock Wrapper ===
    "StartupFileLock",
    "CrossRepoLockManager",
    "MAX_LOCK_TIMEOUT",
    "MIN_LOCK_TIMEOUT",
    "DEFAULT_LOCK_TIMEOUT",
    "STALE_LOCK_RETRY_TIMEOUT",
]
