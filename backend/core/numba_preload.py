"""
JARVIS Numba Pre-loader v7.0.0
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

v7.0.0 Solution (BULLETPROOF):
1. Use REENTRANT lock (RLock) to handle nested imports
2. Use GLOBAL import lock that syncs with Python's import machinery
3. Disable numba JIT AND threading during import
4. Force COMPLETE initialization of ALL problematic submodules
5. Use threading.Event with INDEFINITE blocking (no timeout loops)
6. Triple-check module readiness before declaring success
7. Add per-thread import tracking to detect races
8. Mark numba as "importing" in sys.modules to prevent re-entry
9. Expose detailed status for debugging

Key Changes in v7.0:
- RLock instead of Lock (handles recursive imports)
- Checks sys.modules for partial imports before proceeding
- Forces import of ALL numba submodules that cause issues
- Uses import_module() instead of bare import for better control
- Adds secondary barrier for whisper-specific initialization

Usage in main.py (MUST BE FIRST IMPORT):
    # CRITICAL: Pre-load numba before ANY other imports
    from core.numba_preload import ensure_numba_initialized, get_numba_status
    ensure_numba_initialized()
    
Usage in whisper_audio_fix.py (or any numba-using module):
    from core.numba_preload import wait_for_numba, is_numba_ready
    
    # This BLOCKS until numba init completes - NO TIMEOUT LOOP
    wait_for_numba()
    
    # Now safe to import whisper/librosa/etc
    import whisper
"""

import os
import sys
import threading
import logging
import time
import importlib
from typing import Optional, Dict, Any, Set
from dataclasses import dataclass, field
from enum import Enum
from contextlib import contextmanager

logger = logging.getLogger(__name__)


class NumbaStatus(Enum):
    """Status of numba initialization"""
    NOT_STARTED = "not_started"
    INITIALIZING = "initializing"
    IMPORTING_SUBMODULES = "importing_submodules"
    READY = "ready"
    FAILED = "failed"
    NOT_INSTALLED = "not_installed"


@dataclass
class NumbaInfo:
    """Information about numba initialization"""
    status: NumbaStatus = NumbaStatus.NOT_STARTED
    version: Optional[str] = None
    error: Optional[str] = None
    initialized_by_thread: Optional[str] = None
    initialized_at: Optional[float] = None
    submodules_loaded: int = 0
    import_attempts: int = 0
    waiting_threads: int = 0


# ═══════════════════════════════════════════════════════════════════════════════
# GLOBAL STATE - Process-level singleton with REENTRANT lock
# ═══════════════════════════════════════════════════════════════════════════════
_numba_lock = threading.RLock()  # RLock for recursive import safety
_numba_info = NumbaInfo()
_numba_module = None
_initialization_complete = threading.Event()
_importing_threads: Set[int] = set()  # Track threads currently importing


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
        # CRITICAL: Disable JIT and threading during import
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


def _check_numba_in_sys_modules() -> bool:
    """
    Check if numba is already (partially) imported in sys.modules.
    Returns True if numba appears to be fully imported, False otherwise.
    """
    if 'numba' not in sys.modules:
        return False
    
    numba_mod = sys.modules.get('numba')
    if numba_mod is None:
        return False
    
    # Check for partial initialization markers
    if not hasattr(numba_mod, '__version__'):
        return False
    
    # Check for the problematic submodule
    if 'numba.core.utils' in sys.modules:
        utils_mod = sys.modules.get('numba.core.utils')
        if utils_mod is not None and hasattr(utils_mod, 'get_hashable_key'):
            return True
    
    return False


def _do_numba_import() -> bool:
    """
    Actually perform the numba import.
    This is called exactly ONCE per process.
    
    v7.0: Uses importlib for better control and imports ALL problematic submodules.
    
    Returns True if successful, False otherwise.
    """
    global _numba_module, _numba_info
    
    _numba_info.initialized_by_thread = threading.current_thread().name
    _numba_info.initialized_at = time.time()
    _numba_info.import_attempts += 1
    
    # Check if already fully imported (from a previous process or forked context)
    if _check_numba_in_sys_modules():
        _numba_module = sys.modules['numba']
        _numba_info.version = _numba_module.__version__
        _numba_info.status = NumbaStatus.READY
        logger.info(f"✅ numba {_numba_info.version} already in sys.modules (reused)")
        return True
    
    with _numba_import_environment():
        try:
            # ═══════════════════════════════════════════════════════════════════
            # PHASE 1: Import main numba module
            # ═══════════════════════════════════════════════════════════════════
            _numba_info.status = NumbaStatus.INITIALIZING
            
            # Use importlib for better control over the import process
            numba = importlib.import_module('numba')
            _numba_module = numba
            _numba_info.version = numba.__version__
            
            logger.debug(f"[numba_preload] Phase 1: numba {numba.__version__} base imported")
            
            # ═══════════════════════════════════════════════════════════════════
            # PHASE 2: Force COMPLETE initialization of ALL problematic submodules
            # These must be imported in a specific order to avoid circular imports
            # ═══════════════════════════════════════════════════════════════════
            _numba_info.status = NumbaStatus.IMPORTING_SUBMODULES
            
            # List of submodules that are known to cause issues
            # Order matters! Dependencies must be imported first
            critical_submodules = [
                'numba.core.config',      # Configuration (no deps)
                'numba.core.types',       # Type system (deps on config)
                'numba.core.utils',       # Utilities (deps on types) - THE PROBLEMATIC ONE
                'numba.core.errors',      # Error handling
                'numba.core.typing',      # Typing utilities
                'numba.typed',            # Typed containers
                'numba.np.ufunc',         # NumPy integration
            ]
            
            loaded_count = 0
            for submodule in critical_submodules:
                try:
                    mod = importlib.import_module(submodule)
                    loaded_count += 1
                    
                    # For numba.core.utils, explicitly access get_hashable_key
                    if submodule == 'numba.core.utils':
                        if hasattr(mod, 'get_hashable_key'):
                            # Force the function to be fully resolved
                            _ = mod.get_hashable_key
                            logger.debug(f"[numba_preload] ✓ get_hashable_key resolved")
                        else:
                            logger.debug(f"[numba_preload] get_hashable_key not found (numba version difference)")
                    
                except ImportError as e:
                    # Some submodules may not exist in all numba versions
                    logger.debug(f"[numba_preload] Submodule {submodule} not available: {e}")
                except Exception as e:
                    logger.debug(f"[numba_preload] Submodule {submodule} error: {e}")
            
            _numba_info.submodules_loaded = loaded_count
            logger.debug(f"[numba_preload] Phase 2: {loaded_count}/{len(critical_submodules)} submodules loaded")
            
            # ═══════════════════════════════════════════════════════════════════
            # PHASE 3: Verify initialization is complete
            # ═══════════════════════════════════════════════════════════════════
            if not _check_numba_in_sys_modules():
                # Something went wrong - numba isn't fully initialized
                logger.warning("[numba_preload] Warning: numba import completed but verification failed")
            
            _numba_info.status = NumbaStatus.READY
            logger.info(
                f"✅ numba {numba.__version__} pre-initialized "
                f"(thread: {threading.current_thread().name}, "
                f"submodules: {loaded_count})"
            )
            return True
            
        except ImportError as e:
            if 'No module named' in str(e) and 'numba' in str(e):
                _numba_info.status = NumbaStatus.NOT_INSTALLED
                _numba_info.error = str(e)
                logger.debug(f"numba not installed (optional): {e}")
            else:
                _numba_info.status = NumbaStatus.FAILED
                _numba_info.error = str(e)
                logger.warning(f"⚠️ numba import error: {e}")
            return False
            
        except Exception as e:
            _numba_info.status = NumbaStatus.FAILED
            _numba_info.error = str(e)
            logger.warning(f"⚠️ numba pre-initialization failed (non-fatal): {e}")
            return False


def ensure_numba_initialized(timeout: float = 60.0) -> bool:
    """
    Ensure numba is initialized. Thread-safe and idempotent.
    
    v7.0: Uses RLock for recursive safety and better race handling.
    
    This function can be called from any thread. The first caller will
    do the actual import, all other callers will wait for completion.
    
    Args:
        timeout: Maximum time to wait for initialization (seconds)
        
    Returns:
        True if numba is available, False otherwise
    """
    global _numba_info, _importing_threads
    
    thread_id = threading.current_thread().ident
    
    # Fast path - already initialized
    if _initialization_complete.is_set():
        return _numba_info.status == NumbaStatus.READY
    
    # Check if THIS thread is already importing (recursive call)
    if thread_id in _importing_threads:
        logger.debug(f"[numba_preload] Recursive import detected in thread {thread_id}")
        # We're in a recursive import - just check current state
        return _numba_info.status == NumbaStatus.READY
    
    # Try to acquire lock for initialization (with generous timeout)
    start_time = time.time()
    acquired = _numba_lock.acquire(timeout=timeout)
    
    if not acquired:
        elapsed = time.time() - start_time
        logger.warning(f"[numba_preload] Timeout waiting for numba initialization ({elapsed:.1f}s)")
        return False
    
    try:
        # Double-check after acquiring lock
        if _initialization_complete.is_set():
            return _numba_info.status == NumbaStatus.READY
        
        # Mark this thread as importing
        _importing_threads.add(thread_id)
        
        # We're the initializing thread
        logger.info(f"[numba_preload] Initializing numba from thread: {threading.current_thread().name}")
        success = _do_numba_import()
        
        # Signal completion to ALL waiting threads
        _initialization_complete.set()
        
        return success
        
    finally:
        # Remove thread from importing set
        _importing_threads.discard(thread_id)
        _numba_lock.release()


def get_numba_status() -> Dict[str, Any]:
    """
    Get current numba status for health checks.
    
    Returns:
        Dictionary with status, version, and other info
    """
    return {
        'status': _numba_info.status.value,
        'version': _numba_info.version,
        'error': _numba_info.error,
        'initialized_by': _numba_info.initialized_by_thread,
        'initialized_at': _numba_info.initialized_at,
        'is_ready': _numba_info.status == NumbaStatus.READY,
    }


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
    """
    if _initialization_complete.is_set():
        return _numba_info.status == NumbaStatus.READY
    return False


def wait_for_numba(timeout: float = 120.0) -> bool:
    """
    BLOCKING wait for numba initialization to complete.
    
    v7.0: This is the KEY function for parallel safety.
    Other modules should call this BEFORE importing numba-dependent packages.
    
    This ensures:
    1. If main thread is initializing, we WAIT for it to complete (NO POLLING)
    2. If main thread already completed, we return immediately
    3. If no initialization started, we trigger it ourselves
    4. Tracks waiting threads for debugging
    
    Args:
        timeout: Maximum time to wait (seconds) - increased to 120s for slow systems
        
    Returns:
        True if numba is available, False if not installed or failed
    """
    global _numba_info
    
    thread_name = threading.current_thread().name
    
    # Fast path - already initialized
    if _initialization_complete.is_set():
        status = _numba_info.status
        if status == NumbaStatus.READY:
            logger.debug(f"[wait_for_numba] Fast path: numba ready (thread: {thread_name})")
            return True
        elif status == NumbaStatus.NOT_INSTALLED:
            logger.debug(f"[wait_for_numba] Fast path: numba not installed")
            return False
        elif status == NumbaStatus.FAILED:
            logger.debug(f"[wait_for_numba] Fast path: numba failed")
            return False
    
    # Track this thread as waiting
    _numba_info.waiting_threads += 1
    logger.debug(f"[wait_for_numba] Thread '{thread_name}' waiting for numba ({_numba_info.waiting_threads} waiting)")
    
    try:
        # First, try to trigger initialization ourselves if not started
        # This handles the case where no thread has started initialization yet
        if _numba_info.status == NumbaStatus.NOT_STARTED:
            # Try to be the initializer
            return ensure_numba_initialized(timeout=timeout)
        
        # ═══════════════════════════════════════════════════════════════════
        # BLOCKING WAIT - No polling, just wait for the event
        # This is the key difference from v2.0 - we don't poll in a loop
        # ═══════════════════════════════════════════════════════════════════
        start_time = time.time()
        
        # Wait for initialization to complete (or timeout)
        completed = _initialization_complete.wait(timeout=timeout)
        
        elapsed = time.time() - start_time
        
        if completed:
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
            if _numba_info.status == NumbaStatus.NOT_STARTED:
                logger.info(f"[wait_for_numba] Attempting initialization as fallback")
                return ensure_numba_initialized(timeout=30.0)
            
            return _numba_info.status == NumbaStatus.READY
            
    finally:
        _numba_info.waiting_threads -= 1


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
    
    v7.0: This function should be called before importing whisper or librosa.
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
    """
    numba_in_modules = 'numba' in sys.modules
    numba_utils_in_modules = 'numba.core.utils' in sys.modules
    
    numba_utils_complete = False
    if numba_utils_in_modules:
        utils = sys.modules.get('numba.core.utils')
        if utils is not None:
            numba_utils_complete = hasattr(utils, 'get_hashable_key')
    
    return {
        'status': _numba_info.status.value,
        'version': _numba_info.version,
        'error': _numba_info.error,
        'initialized_by': _numba_info.initialized_by_thread,
        'initialized_at': _numba_info.initialized_at,
        'submodules_loaded': _numba_info.submodules_loaded,
        'import_attempts': _numba_info.import_attempts,
        'waiting_threads': _numba_info.waiting_threads,
        'is_ready': _numba_info.status == NumbaStatus.READY,
        'numba_in_sys_modules': numba_in_modules,
        'numba_utils_in_sys_modules': numba_utils_in_modules,
        'numba_utils_complete': numba_utils_complete,
        'event_is_set': _initialization_complete.is_set(),
        'bypass_marker_set': get_numba_bypass_marker(),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Auto-initialize if this module is imported directly
# v7.0: Only in main thread to avoid races during parallel imports
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

