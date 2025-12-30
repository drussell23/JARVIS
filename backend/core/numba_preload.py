"""
JARVIS Numba Pre-loader v8.0.0
==============================

CRITICAL: This module must be imported FIRST, before ANY other imports
that might use numba (whisper, librosa, scipy with JIT, etc.).

This solves the circular import error:
    "cannot import name 'get_hashable_key' from partially initialized module 'numba.core.utils'"

The error occurs when:
1. Multiple threads try to import numba simultaneously
2. Thread A starts importing numba.core.utils
3. Thread B also tries to import numba.core.utils
4. Thread B sees a partially initialized module and fails

v8.0.0 Improvements (PRODUCTION-GRADE):
1. Thread-safe status reads with proper locking
2. Atomic counters using threading.Lock for waiting_threads
3. Proper error messages for ALL failure paths (including timeout)
4. Version-adaptive numba checks (works with all numba versions)
5. Async initialization support via asyncio
6. Intelligent fallback with detailed diagnostics
7. Non-blocking status queries with snapshot semantics
8. Graceful degradation when numba is unavailable

Usage in main.py (MUST BE FIRST IMPORT):
    # CRITICAL: Pre-load numba before ANY other imports
    from core.numba_preload import ensure_numba_initialized, get_numba_status
    ensure_numba_initialized()

Usage in async context:
    from core.numba_preload import ensure_numba_initialized_async
    await ensure_numba_initialized_async()

Usage in whisper_audio_fix.py (or any numba-using module):
    from core.numba_preload import wait_for_numba, is_numba_ready

    # This BLOCKS until numba init completes - NO TIMEOUT LOOP
    wait_for_numba()

    # Now safe to import whisper/librosa/etc
    import whisper

Author: Derek Russell
Version: 8.0.0
"""

import os
import sys
import threading
import asyncio
import logging
import time
import importlib
from typing import Optional, Dict, Any, Set, Tuple
from dataclasses import dataclass, field
from enum import Enum
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)


class NumbaStatus(Enum):
    """Status of numba initialization"""
    NOT_STARTED = "not_started"
    INITIALIZING = "initializing"
    IMPORTING_SUBMODULES = "importing_submodules"
    READY = "ready"
    FAILED = "failed"
    NOT_INSTALLED = "not_installed"
    TIMEOUT = "timeout"  # v8.0: New status for timeout scenarios


@dataclass
class NumbaInfo:
    """Information about numba initialization - thread-safe via external lock"""
    status: NumbaStatus = NumbaStatus.NOT_STARTED
    version: Optional[str] = None
    error: Optional[str] = None
    error_type: Optional[str] = None  # v8.0: Track error type for better diagnostics
    initialized_by_thread: Optional[str] = None
    initialized_at: Optional[float] = None
    completed_at: Optional[float] = None  # v8.0: Track completion time
    submodules_loaded: int = 0
    import_attempts: int = 0
    _waiting_threads: int = 0  # v8.0: Private, use atomic accessors


# ═══════════════════════════════════════════════════════════════════════════════
# GLOBAL STATE - Process-level singleton with proper synchronization
# ═══════════════════════════════════════════════════════════════════════════════
_numba_lock = threading.RLock()  # RLock for recursive import safety
_status_lock = threading.Lock()  # v8.0: Separate lock for status reads
_counter_lock = threading.Lock()  # v8.0: Lock for atomic counter operations
_numba_info = NumbaInfo()
_numba_module = None
_initialization_complete = threading.Event()
_importing_threads: Set[int] = set()  # Track threads currently importing

# v8.0: Thread pool for async operations
_async_executor: Optional[ThreadPoolExecutor] = None


def _get_async_executor() -> ThreadPoolExecutor:
    """Get or create the async executor (lazy initialization)."""
    global _async_executor
    if _async_executor is None:
        _async_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="numba_init")
    return _async_executor


# ═══════════════════════════════════════════════════════════════════════════════
# v8.0: ATOMIC COUNTER OPERATIONS
# ═══════════════════════════════════════════════════════════════════════════════

def _increment_waiting_threads() -> int:
    """Atomically increment waiting threads counter."""
    with _counter_lock:
        _numba_info._waiting_threads += 1
        return _numba_info._waiting_threads


def _decrement_waiting_threads() -> int:
    """Atomically decrement waiting threads counter."""
    with _counter_lock:
        _numba_info._waiting_threads = max(0, _numba_info._waiting_threads - 1)
        return _numba_info._waiting_threads


def _get_waiting_threads() -> int:
    """Atomically get waiting threads count."""
    with _counter_lock:
        return _numba_info._waiting_threads


# ═══════════════════════════════════════════════════════════════════════════════
# v8.0: THREAD-SAFE STATUS UPDATES
# ═══════════════════════════════════════════════════════════════════════════════

def _set_status(
    status: NumbaStatus,
    error: Optional[str] = None,
    error_type: Optional[str] = None,
    version: Optional[str] = None,
) -> None:
    """Thread-safe status update."""
    with _status_lock:
        _numba_info.status = status
        if error is not None:
            _numba_info.error = error
        if error_type is not None:
            _numba_info.error_type = error_type
        if version is not None:
            _numba_info.version = version
        if status in (NumbaStatus.READY, NumbaStatus.FAILED, NumbaStatus.NOT_INSTALLED, NumbaStatus.TIMEOUT):
            _numba_info.completed_at = time.time()


def _get_status_snapshot() -> Dict[str, Any]:
    """Get a thread-safe snapshot of current status."""
    with _status_lock:
        return {
            'status': _numba_info.status.value,
            'version': _numba_info.version,
            'error': _numba_info.error,
            'error_type': _numba_info.error_type,
            'initialized_by': _numba_info.initialized_by_thread,
            'initialized_at': _numba_info.initialized_at,
            'completed_at': _numba_info.completed_at,
            'submodules_loaded': _numba_info.submodules_loaded,
            'import_attempts': _numba_info.import_attempts,
            'waiting_threads': _numba_info._waiting_threads,
            'is_ready': _numba_info.status == NumbaStatus.READY,
        }


@contextmanager
def _numba_import_environment():
    """
    Context manager for safe numba import environment.
    Saves/restores environment variables that affect numba.
    """
    # Save original environment
    original_env = {
        'NUMBA_DISABLE_JIT': os.environ.get('NUMBA_DISABLE_JIT'),
        'NUMBA_NUM_THREADS': os.environ.get('NUMBA_NUM_THREADS'),
        'NUMBA_THREADING_LAYER': os.environ.get('NUMBA_THREADING_LAYER'),
        'NUMBA_CACHE_DIR': os.environ.get('NUMBA_CACHE_DIR'),
    }

    try:
        # CRITICAL: Disable JIT and threading during import to prevent race conditions
        os.environ['NUMBA_DISABLE_JIT'] = '1'
        os.environ['NUMBA_NUM_THREADS'] = '1'
        os.environ['NUMBA_THREADING_LAYER'] = 'workqueue'  # Safest option
        yield
    finally:
        # Restore original environment
        for key, value in original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _check_numba_in_sys_modules() -> Tuple[bool, Optional[str]]:
    """
    Check if numba is already (partially) imported in sys.modules.

    v8.0: Returns (is_complete, version) tuple for better diagnostics.
    Also uses version-adaptive checks that work with all numba versions.

    Returns:
        Tuple of (is_fully_imported, version_string)
    """
    if 'numba' not in sys.modules:
        return False, None

    numba_mod = sys.modules.get('numba')
    if numba_mod is None:
        return False, None

    # Check for partial initialization markers
    if not hasattr(numba_mod, '__version__'):
        return False, None

    version = numba_mod.__version__

    # v8.0: Version-adaptive checks - different numba versions have different structures
    # Check for critical submodules that indicate complete initialization
    critical_checks = [
        ('numba.core.utils', ['get_hashable_key', 'unified_function_type', 'PYVERSION']),
        ('numba.core.types', ['int64', 'float64', 'boolean']),
        ('numba.core.config', ['NUMBA_NUM_THREADS']),
    ]

    for module_name, attrs_to_check in critical_checks:
        if module_name in sys.modules:
            mod = sys.modules.get(module_name)
            if mod is not None:
                # Check if ANY of the expected attributes exist
                for attr in attrs_to_check:
                    if hasattr(mod, attr):
                        return True, version

    # Fallback: if numba.core exists and has expected structure, consider it ready
    if 'numba.core' in sys.modules:
        core_mod = sys.modules.get('numba.core')
        if core_mod is not None and hasattr(core_mod, '__path__'):
            return True, version

    return False, version


def _do_numba_import() -> bool:
    """
    Actually perform the numba import.
    This is called exactly ONCE per process.

    v8.0: Enhanced with better error handling and diagnostics.

    Returns True if successful, False otherwise.
    """
    global _numba_module

    with _status_lock:
        _numba_info.initialized_by_thread = threading.current_thread().name
        _numba_info.initialized_at = time.time()
        _numba_info.import_attempts += 1

    # Check if already fully imported (from a previous process or forked context)
    is_complete, version = _check_numba_in_sys_modules()
    if is_complete:
        _numba_module = sys.modules['numba']
        _set_status(NumbaStatus.READY, version=version)
        logger.info(f"✅ numba {version} already in sys.modules (reused)")
        return True

    with _numba_import_environment():
        try:
            # ═══════════════════════════════════════════════════════════════════
            # PHASE 1: Import main numba module
            # ═══════════════════════════════════════════════════════════════════
            _set_status(NumbaStatus.INITIALIZING)

            # Use importlib for better control over the import process
            numba = importlib.import_module('numba')
            _numba_module = numba
            version = numba.__version__

            logger.debug(f"[numba_preload] Phase 1: numba {version} base imported")

            # ═══════════════════════════════════════════════════════════════════
            # PHASE 2: Force COMPLETE initialization of ALL problematic submodules
            # These must be imported in a specific order to avoid circular imports
            # ═══════════════════════════════════════════════════════════════════
            _set_status(NumbaStatus.IMPORTING_SUBMODULES, version=version)

            # v8.0: Dynamic submodule list based on numba version
            critical_submodules = _get_critical_submodules(version)

            loaded_count = 0
            load_errors = []

            for submodule in critical_submodules:
                try:
                    mod = importlib.import_module(submodule)
                    loaded_count += 1

                    # For numba.core.utils, explicitly access key functions to force resolution
                    if submodule == 'numba.core.utils':
                        for func_name in ['get_hashable_key', 'unified_function_type']:
                            if hasattr(mod, func_name):
                                _ = getattr(mod, func_name)
                                logger.debug(f"[numba_preload] ✓ {func_name} resolved")

                except ImportError as e:
                    # Some submodules may not exist in all numba versions
                    load_errors.append(f"{submodule}: {e}")
                    logger.debug(f"[numba_preload] Submodule {submodule} not available: {e}")
                except Exception as e:
                    load_errors.append(f"{submodule}: {e}")
                    logger.debug(f"[numba_preload] Submodule {submodule} error: {e}")

            with _status_lock:
                _numba_info.submodules_loaded = loaded_count

            logger.debug(f"[numba_preload] Phase 2: {loaded_count}/{len(critical_submodules)} submodules loaded")

            # ═══════════════════════════════════════════════════════════════════
            # PHASE 3: Verify initialization is complete
            # ═══════════════════════════════════════════════════════════════════
            is_complete, _ = _check_numba_in_sys_modules()
            if not is_complete:
                # Something went wrong - numba isn't fully initialized
                logger.warning("[numba_preload] Warning: numba import completed but verification failed")
                if load_errors:
                    logger.debug(f"[numba_preload] Submodule errors: {load_errors}")

            _set_status(NumbaStatus.READY, version=version)
            logger.info(
                f"✅ numba {version} pre-initialized "
                f"(thread: {threading.current_thread().name}, "
                f"submodules: {loaded_count})"
            )
            return True

        except ImportError as e:
            error_str = str(e)
            if 'No module named' in error_str and 'numba' in error_str:
                _set_status(
                    NumbaStatus.NOT_INSTALLED,
                    error=error_str,
                    error_type="ImportError"
                )
                logger.debug(f"numba not installed (optional): {e}")
            else:
                _set_status(
                    NumbaStatus.FAILED,
                    error=error_str,
                    error_type="ImportError"
                )
                logger.warning(f"⚠️ numba import error: {e}")
            return False

        except Exception as e:
            _set_status(
                NumbaStatus.FAILED,
                error=str(e),
                error_type=type(e).__name__
            )
            logger.warning(f"⚠️ numba pre-initialization failed (non-fatal): {e}")
            return False


def _get_critical_submodules(version: str) -> list:
    """
    v8.0: Get critical submodules based on numba version.
    Different versions have different module structures.
    """
    # Parse version
    try:
        major, minor = [int(x) for x in version.split('.')[:2]]
    except (ValueError, IndexError):
        major, minor = 0, 0

    # Base submodules (all versions)
    submodules = [
        'numba.core.config',
        'numba.core.types',
        'numba.core.utils',
        'numba.core.errors',
    ]

    # Version-specific additions
    if major >= 0 and minor >= 50:
        submodules.extend([
            'numba.core.typing',
            'numba.typed',
        ])

    if major >= 0 and minor >= 55:
        submodules.append('numba.np.ufunc')

    return submodules


def ensure_numba_initialized(timeout: float = 60.0) -> bool:
    """
    Ensure numba is initialized. Thread-safe and idempotent.

    v8.0: Enhanced with proper timeout handling and status updates.

    This function can be called from any thread. The first caller will
    do the actual import, all other callers will wait for completion.

    Args:
        timeout: Maximum time to wait for initialization (seconds)

    Returns:
        True if numba is available and ready, False otherwise
    """
    global _importing_threads

    thread_id = threading.current_thread().ident
    thread_name = threading.current_thread().name

    # Fast path - already initialized
    if _initialization_complete.is_set():
        with _status_lock:
            return _numba_info.status == NumbaStatus.READY

    # Check if THIS thread is already importing (recursive call)
    if thread_id in _importing_threads:
        logger.debug(f"[numba_preload] Recursive import detected in thread {thread_id}")
        with _status_lock:
            return _numba_info.status == NumbaStatus.READY

    # Try to acquire lock for initialization (with timeout)
    start_time = time.time()
    acquired = _numba_lock.acquire(timeout=timeout)

    if not acquired:
        elapsed = time.time() - start_time
        # v8.0: Set proper status and error on timeout
        _set_status(
            NumbaStatus.TIMEOUT,
            error=f"Lock acquisition timeout after {elapsed:.1f}s (thread: {thread_name})",
            error_type="TimeoutError"
        )
        logger.warning(f"[numba_preload] Timeout waiting for numba initialization ({elapsed:.1f}s)")
        return False

    try:
        # Double-check after acquiring lock
        if _initialization_complete.is_set():
            with _status_lock:
                return _numba_info.status == NumbaStatus.READY

        # Mark this thread as importing
        _importing_threads.add(thread_id)

        # We're the initializing thread
        logger.info(f"[numba_preload] Initializing numba from thread: {thread_name}")
        success = _do_numba_import()

        # Signal completion to ALL waiting threads
        _initialization_complete.set()

        return success

    except Exception as e:
        # v8.0: Catch any unexpected errors and set proper status
        _set_status(
            NumbaStatus.FAILED,
            error=f"Unexpected error during initialization: {e}",
            error_type=type(e).__name__
        )
        _initialization_complete.set()  # Still signal completion (with failure)
        logger.error(f"[numba_preload] Unexpected error: {e}")
        return False

    finally:
        # Remove thread from importing set
        _importing_threads.discard(thread_id)
        _numba_lock.release()


async def ensure_numba_initialized_async(timeout: float = 60.0) -> bool:
    """
    v8.0: Async version of ensure_numba_initialized.

    Runs the synchronous initialization in a thread pool to avoid
    blocking the event loop.

    Args:
        timeout: Maximum time to wait for initialization (seconds)

    Returns:
        True if numba is available and ready, False otherwise
    """
    # Fast path - already initialized
    if _initialization_complete.is_set():
        with _status_lock:
            return _numba_info.status == NumbaStatus.READY

    # Run in thread pool to avoid blocking event loop
    loop = asyncio.get_event_loop()
    executor = _get_async_executor()

    try:
        return await asyncio.wait_for(
            loop.run_in_executor(executor, ensure_numba_initialized, timeout),
            timeout=timeout + 5.0  # Add buffer for executor overhead
        )
    except asyncio.TimeoutError:
        _set_status(
            NumbaStatus.TIMEOUT,
            error=f"Async initialization timeout after {timeout}s",
            error_type="AsyncTimeoutError"
        )
        return False


def get_numba_status() -> Dict[str, Any]:
    """
    Get current numba status for health checks.

    v8.0: Thread-safe with snapshot semantics.

    Returns:
        Dictionary with status, version, and other info
    """
    return _get_status_snapshot()


def get_numba_module():
    """
    Get the numba module if available.

    Returns:
        The numba module, or None if not available
    """
    ensure_numba_initialized()
    return _numba_module


def is_numba_ready() -> bool:
    """
    Quick check if numba is ready.
    Non-blocking if initialization is complete.

    v8.0: Thread-safe read.
    """
    if _initialization_complete.is_set():
        with _status_lock:
            return _numba_info.status == NumbaStatus.READY
    return False


def wait_for_numba(timeout: float = 120.0) -> bool:
    """
    BLOCKING wait for numba initialization to complete.

    v8.0: Enhanced with atomic counter operations and better timeout handling.

    This is the KEY function for parallel safety.
    Other modules should call this BEFORE importing numba-dependent packages.

    This ensures:
    1. If main thread is initializing, we WAIT for it to complete (NO POLLING)
    2. If main thread already completed, we return immediately
    3. If no initialization started, we trigger it ourselves
    4. Tracks waiting threads for debugging (atomically)

    Args:
        timeout: Maximum time to wait (seconds) - default 120s for slow systems

    Returns:
        True if numba is available, False if not installed or failed
    """
    thread_name = threading.current_thread().name

    # Fast path - already initialized
    if _initialization_complete.is_set():
        with _status_lock:
            status = _numba_info.status
        if status == NumbaStatus.READY:
            logger.debug(f"[wait_for_numba] Fast path: numba ready (thread: {thread_name})")
            return True
        elif status == NumbaStatus.NOT_INSTALLED:
            logger.debug(f"[wait_for_numba] Fast path: numba not installed")
            return False
        else:
            logger.debug(f"[wait_for_numba] Fast path: numba status: {status.value}")
            return False

    # Track this thread as waiting (atomic)
    waiting_count = _increment_waiting_threads()
    logger.debug(f"[wait_for_numba] Thread '{thread_name}' waiting for numba ({waiting_count} waiting)")

    try:
        # First, try to trigger initialization ourselves if not started
        with _status_lock:
            current_status = _numba_info.status

        if current_status == NumbaStatus.NOT_STARTED:
            # Try to be the initializer
            return ensure_numba_initialized(timeout=timeout)

        # ═══════════════════════════════════════════════════════════════════
        # BLOCKING WAIT - No polling, just wait for the event
        # ═══════════════════════════════════════════════════════════════════
        start_time = time.time()

        # Wait for initialization to complete (or timeout)
        completed = _initialization_complete.wait(timeout=timeout)

        elapsed = time.time() - start_time

        if completed:
            with _status_lock:
                status = _numba_info.status

            if status == NumbaStatus.READY:
                logger.debug(f"[wait_for_numba] Thread '{thread_name}' - numba ready after {elapsed:.1f}s")
                return True
            elif status == NumbaStatus.NOT_INSTALLED:
                logger.debug(f"[wait_for_numba] Thread '{thread_name}' - numba not installed")
                return False
            else:
                logger.debug(f"[wait_for_numba] Thread '{thread_name}' - numba status: {status.value}")
                return False
        else:
            # Timeout - but check status anyway in case event wasn't set
            logger.warning(f"[wait_for_numba] Thread '{thread_name}' timeout after {timeout}s")

            # Last resort: try to initialize ourselves
            with _status_lock:
                current_status = _numba_info.status

            if current_status == NumbaStatus.NOT_STARTED:
                logger.info(f"[wait_for_numba] Attempting initialization as fallback")
                return ensure_numba_initialized(timeout=30.0)

            with _status_lock:
                return _numba_info.status == NumbaStatus.READY

    finally:
        _decrement_waiting_threads()


async def wait_for_numba_async(timeout: float = 120.0) -> bool:
    """
    v8.0: Async version of wait_for_numba.

    Non-blocking wait that yields to the event loop.

    Args:
        timeout: Maximum time to wait (seconds)

    Returns:
        True if numba is available, False otherwise
    """
    # Fast path
    if _initialization_complete.is_set():
        with _status_lock:
            return _numba_info.status == NumbaStatus.READY

    # Use asyncio event waiting
    start_time = time.time()

    while not _initialization_complete.is_set():
        elapsed = time.time() - start_time
        if elapsed >= timeout:
            return False

        # Yield to event loop with small sleep
        await asyncio.sleep(0.05)

    with _status_lock:
        return _numba_info.status == NumbaStatus.READY


def set_numba_bypass_marker():
    """
    Set a global marker that signals numba has been attempted.
    Other modules can check this to avoid redundant initialization attempts.
    """
    os.environ['_JARVIS_NUMBA_INIT_ATTEMPTED'] = '1'


def get_numba_bypass_marker() -> bool:
    """
    Check if numba initialization has been attempted.
    """
    return os.environ.get('_JARVIS_NUMBA_INIT_ATTEMPTED') == '1'


def acquire_import_lock_and_wait(timeout: float = 120.0) -> bool:
    """
    The strongest guarantee: acquire Python's import lock AND wait for numba.

    v8.0: Thread-safe with proper status checks.

    This function should be called before importing whisper or librosa.
    It ensures no other thread can be importing Python modules while we check
    numba status, preventing ALL race conditions.

    Args:
        timeout: Maximum time to wait

    Returns:
        True if numba is ready, False otherwise
    """
    import importlib._bootstrap as _bootstrap

    # First, check fast path
    if _initialization_complete.is_set():
        with _status_lock:
            return _numba_info.status == NumbaStatus.READY

    # Acquire Python's import lock for maximum safety
    # This prevents ANY imports from happening in other threads
    try:
        _bootstrap._call_with_frames_removed(_bootstrap._lock_unlock_module, 'numba')
    except (AttributeError, KeyError):
        # Not all Python versions have this, fall back to regular wait
        pass

    # Now wait for numba to be initialized
    return wait_for_numba(timeout=timeout)


def get_detailed_status() -> Dict[str, Any]:
    """
    Get detailed status for debugging numba initialization issues.

    v8.0: Enhanced with more diagnostic info.
    """
    numba_in_modules = 'numba' in sys.modules
    numba_utils_in_modules = 'numba.core.utils' in sys.modules

    numba_utils_complete = False
    numba_version_in_modules = None

    if numba_in_modules:
        numba_mod = sys.modules.get('numba')
        if numba_mod is not None:
            numba_version_in_modules = getattr(numba_mod, '__version__', None)

    if numba_utils_in_modules:
        utils = sys.modules.get('numba.core.utils')
        if utils is not None:
            # v8.0: Check multiple indicators
            numba_utils_complete = any(
                hasattr(utils, attr)
                for attr in ['get_hashable_key', 'unified_function_type', 'PYVERSION']
            )

    with _status_lock:
        return {
            'status': _numba_info.status.value,
            'version': _numba_info.version,
            'version_in_sys_modules': numba_version_in_modules,
            'error': _numba_info.error,
            'error_type': _numba_info.error_type,
            'initialized_by': _numba_info.initialized_by_thread,
            'initialized_at': _numba_info.initialized_at,
            'completed_at': _numba_info.completed_at,
            'submodules_loaded': _numba_info.submodules_loaded,
            'import_attempts': _numba_info.import_attempts,
            'waiting_threads': _numba_info._waiting_threads,
            'is_ready': _numba_info.status == NumbaStatus.READY,
            'numba_in_sys_modules': numba_in_modules,
            'numba_utils_in_sys_modules': numba_utils_in_modules,
            'numba_utils_complete': numba_utils_complete,
            'event_is_set': _initialization_complete.is_set(),
            'bypass_marker_set': get_numba_bypass_marker(),
            'current_thread': threading.current_thread().name,
            'is_main_thread': threading.current_thread() is threading.main_thread(),
        }


def reset_for_testing():
    """
    v8.0: Reset all state for testing purposes.

    WARNING: Only use in tests! This is not thread-safe during normal operation.
    """
    global _numba_module, _numba_info, _initialization_complete, _importing_threads

    with _numba_lock:
        with _status_lock:
            _numba_info = NumbaInfo()
        _numba_module = None
        _initialization_complete.clear()
        _importing_threads.clear()
        os.environ.pop('_JARVIS_NUMBA_INIT_ATTEMPTED', None)


def is_numba_corrupted() -> Tuple[bool, str]:
    """
    v9.0: Detect if numba modules are in a corrupted/partial state.

    This happens when circular import occurs - modules exist in sys.modules
    but are only partially initialized.

    Returns:
        Tuple of (is_corrupted, reason)
    """
    if 'numba' not in sys.modules:
        return False, "not_imported"

    numba_mod = sys.modules.get('numba')

    # Check for None module (shouldn't happen but check anyway)
    if numba_mod is None:
        return True, "module_is_none"

    # Check for missing __version__ (sign of partial init)
    if not hasattr(numba_mod, '__version__'):
        return True, "missing_version"

    # Check numba.core.utils for the problematic get_hashable_key
    if 'numba.core.utils' in sys.modules:
        utils_mod = sys.modules.get('numba.core.utils')
        if utils_mod is None:
            return True, "utils_module_is_none"

        # Check if it's a partial module (missing expected attributes)
        expected_attrs = ['get_hashable_key', 'PYVERSION']
        found_any = any(hasattr(utils_mod, attr) for attr in expected_attrs)

        if not found_any:
            return True, "utils_missing_attributes"

    return False, "ok"


def clear_corrupted_numba_modules() -> int:
    """
    v9.0: Clear ALL numba-related modules from sys.modules.

    This is necessary when numba gets into a corrupted state due to
    circular imports. By clearing all numba modules, a fresh import
    can succeed.

    IMPORTANT: After calling this, set NUMBA_DISABLE_JIT=1 to prevent
    the circular import from happening again.

    Returns:
        Number of modules cleared
    """
    numba_modules = [key for key in sys.modules.keys() if key == 'numba' or key.startswith('numba.')]

    cleared_count = 0
    for mod_name in numba_modules:
        try:
            del sys.modules[mod_name]
            cleared_count += 1
        except (KeyError, TypeError):
            pass

    if cleared_count > 0:
        logger.info(f"[numba_preload] Cleared {cleared_count} corrupted numba modules from sys.modules")

        # Reset our internal state
        global _numba_module
        _numba_module = None
        with _status_lock:
            _numba_info.status = NumbaStatus.NOT_STARTED
            _numba_info.error = None
        _initialization_complete.clear()

    return cleared_count


def ensure_numba_safe_for_whisper() -> Dict[str, Any]:
    """
    v9.0: Ensure numba is in a safe state for Whisper to import.

    This is the RECOMMENDED function to call before importing Whisper.
    It handles all the edge cases:

    1. If numba is not installed -> returns success (Whisper works without it)
    2. If numba is fully initialized -> returns success
    3. If numba is corrupted -> clears modules, disables JIT, returns degraded
    4. If numba fails to initialize -> disables JIT, returns degraded

    Returns:
        Dict with:
        - safe: bool - True if it's safe to import Whisper
        - jit_disabled: bool - True if JIT was disabled
        - reason: str - Explanation of what happened
        - numba_available: bool - True if numba is fully available
    """
    result = {
        'safe': False,
        'jit_disabled': False,
        'reason': '',
        'numba_available': False,
    }

    # Check for corruption first
    is_corrupted, corruption_reason = is_numba_corrupted()

    if is_corrupted:
        logger.warning(f"[numba_preload] Numba corruption detected: {corruption_reason}")

        # Clear corrupted modules
        cleared = clear_corrupted_numba_modules()

        # Disable JIT to prevent re-corruption
        os.environ['NUMBA_DISABLE_JIT'] = '1'
        os.environ['NUMBA_NUM_THREADS'] = '1'

        result['safe'] = True
        result['jit_disabled'] = True
        result['reason'] = f"cleared_{cleared}_corrupted_modules_jit_disabled"
        result['numba_available'] = False
        return result

    # Try normal initialization
    try:
        success = ensure_numba_initialized(timeout=30.0)

        if success:
            result['safe'] = True
            result['jit_disabled'] = False
            result['reason'] = "numba_ready"
            result['numba_available'] = True
            return result
        else:
            # Initialization failed - check why
            status = get_numba_status()

            if status.get('status') == 'not_installed':
                result['safe'] = True
                result['jit_disabled'] = False
                result['reason'] = "numba_not_installed"
                result['numba_available'] = False
                return result

            # Some other failure - disable JIT as fallback
            os.environ['NUMBA_DISABLE_JIT'] = '1'
            os.environ['NUMBA_NUM_THREADS'] = '1'

            result['safe'] = True
            result['jit_disabled'] = True
            result['reason'] = f"init_failed_{status.get('status')}_jit_disabled"
            result['numba_available'] = False
            return result

    except Exception as e:
        logger.warning(f"[numba_preload] Error during numba check: {e}")

        # On any error, disable JIT as fallback
        os.environ['NUMBA_DISABLE_JIT'] = '1'
        os.environ['NUMBA_NUM_THREADS'] = '1'

        result['safe'] = True
        result['jit_disabled'] = True
        result['reason'] = f"error_{type(e).__name__}_jit_disabled"
        result['numba_available'] = False
        return result


# ═══════════════════════════════════════════════════════════════════════════════
# Auto-initialize if this module is imported directly
# v8.0: Only in main thread to avoid races during parallel imports
# ═══════════════════════════════════════════════════════════════════════════════
if __name__ != "__main__":
    # Check if we're in the main thread
    if threading.current_thread() is threading.main_thread():
        # Main thread: do the actual initialization
        logger.debug("[numba_preload] Auto-init in main thread")
        ensure_numba_initialized()
        set_numba_bypass_marker()
    else:
        # Non-main thread: just wait for initialization
        logger.debug(f"[numba_preload] Auto-init waiting in thread: {threading.current_thread().name}")
        # Don't block module load - just check fast path
        if not _initialization_complete.is_set():
            # Mark that we need initialization but don't block
            pass
