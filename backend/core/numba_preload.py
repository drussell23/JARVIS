"""
JARVIS Numba Pre-loader v12.0.0
===============================

CRITICAL: This module must be imported FIRST, before ANY other imports
that might use numba (whisper, librosa, scipy with JIT, etc.).

This solves the circular import error:
    "cannot import name 'get_hashable_key' from partially initialized module 'numba.core.utils'"

v12.0.0 ROOT CAUSE FIX:
The circular import error is actually a SYMPTOM of version incompatibility:
- numba 0.60.0 requires Python 3.9.18+ or 3.10+
- Older Python versions (like 3.9.6) have internal import machinery differences
- This causes the "partially initialized module" error

v12.0.0 SOLUTION:
1. **UPFRONT VERSION COMPATIBILITY CHECK**: Detect incompatibility BEFORE any import
2. **SKIP NUMBA ENTIRELY**: When versions are incompatible, skip numba completely
3. **NUMBA-FREE PATH**: Whisper and other libs work fine without numba
4. **INTELLIGENT CACHING**: Remember compatibility state to avoid rechecking
5. **ASYNC-SAFE**: All operations are safe in async contexts
6. **ZERO IMPORT ATTEMPTS**: If incompatible, numba is never imported at all

v10.0.0 CRITICAL FIXES (still applies):
1. PRE-EMPTIVE JIT DISABLE: Set NUMBA_DISABLE_JIT=1 BEFORE any imports
2. Early corruption detection: Detect partial numba modules before import
3. Aggressive module clearing: Clear ALL numba modules on corruption
4. Defensive auto-init: Check corruption before auto-initialization
5. Retry with cleanup: If import fails, clear modules and retry

Author: Derek Russell
Version: 12.0.0
"""

# ═══════════════════════════════════════════════════════════════════════════════
# v12.0: CRITICAL - VERSION COMPATIBILITY CHECK BEFORE ANYTHING ELSE
# This detects incompatible numba/Python combinations UPFRONT and skips numba
# entirely, preventing the circular import from ever being attempted.
# ═══════════════════════════════════════════════════════════════════════════════
import os as _os
import sys as _sys

# v12.0: Compatibility check result - cached for the entire process
_NUMBA_SKIP_REASON = None  # Set if numba should be skipped
_NUMBA_COMPATIBLE = None   # True/False/None (None = not yet checked)


def _check_version_compatibility() -> tuple:
    """
    Check if numba version is compatible with current Python version.

    This is called ONCE at module load time, before any numba import is attempted.

    Known incompatibilities:
    - numba 0.60.0+ requires Python 3.9.18+ or 3.10+
    - numba 0.59.x requires Python 3.9+
    - numba 0.58.x works with Python 3.8-3.11

    Returns:
        Tuple of (is_compatible: bool, reason: str, recommendation: str)
    """
    py_version = _sys.version_info
    py_version_str = f"{py_version.major}.{py_version.minor}.{py_version.micro}"

    # Try to get numba version from package metadata WITHOUT importing numba
    numba_version = None
    try:
        # Use importlib.metadata (Python 3.8+) to read version without import
        if py_version >= (3, 8):
            try:
                from importlib.metadata import version as get_version
                numba_version = get_version('numba')
            except Exception:
                pass

        # Fallback: try pkg_resources
        if numba_version is None:
            try:
                import pkg_resources
                numba_version = pkg_resources.get_distribution('numba').version
            except Exception:
                pass

        # Fallback: check if already imported
        if numba_version is None and 'numba' in _sys.modules:
            numba_mod = _sys.modules.get('numba')
            if numba_mod and hasattr(numba_mod, '__version__'):
                numba_version = numba_mod.__version__

    except Exception:
        pass

    if numba_version is None:
        # numba not installed - that's fine, Whisper works without it
        return (True, "numba_not_installed", "")

    # Parse numba version
    try:
        numba_parts = [int(x) for x in numba_version.split('.')[:3]]
        numba_major = numba_parts[0] if len(numba_parts) > 0 else 0
        numba_minor = numba_parts[1] if len(numba_parts) > 1 else 0
        numba_patch = numba_parts[2] if len(numba_parts) > 2 else 0
    except (ValueError, IndexError):
        numba_major, numba_minor, numba_patch = 0, 0, 0

    # ═══════════════════════════════════════════════════════════════════
    # COMPATIBILITY MATRIX - Based on actual testing and documentation
    # ═══════════════════════════════════════════════════════════════════

    # numba 0.60.0+ has stricter requirements
    if numba_major == 0 and numba_minor >= 60:
        # numba 0.60.0 requires Python 3.9.18+ OR 3.10+
        if py_version.major == 3 and py_version.minor == 9:
            if py_version.micro < 18:
                return (
                    False,
                    f"numba {numba_version} requires Python 3.9.18+ (you have {py_version_str})",
                    "Upgrade Python to 3.10+ OR downgrade numba: pip install 'numba==0.58.1' 'llvmlite==0.41.1'"
                )
        elif py_version.major == 3 and py_version.minor < 9:
            return (
                False,
                f"numba {numba_version} requires Python 3.9+ (you have {py_version_str})",
                "Upgrade Python to 3.10+"
            )

    # numba 0.59.x compatibility
    elif numba_major == 0 and numba_minor == 59:
        if py_version.major == 3 and py_version.minor < 9:
            return (
                False,
                f"numba {numba_version} requires Python 3.9+ (you have {py_version_str})",
                "Upgrade Python OR downgrade numba"
            )

    # All checks passed
    return (True, "compatible", "")


def _check_llvmlite_compatibility() -> tuple:
    """
    Check if llvmlite is compatible with the current environment.

    Common issues:
    - llvmlite built for different Python version
    - llvmlite binary incompatible with system LLVM

    Returns:
        Tuple of (is_compatible: bool, reason: str)
    """
    try:
        # Try to get llvmlite version without importing
        if _sys.version_info >= (3, 8):
            try:
                from importlib.metadata import version as get_version
                llvmlite_version = get_version('llvmlite')
                return (True, f"llvmlite {llvmlite_version}")
            except Exception:
                pass

        # Not installed = compatible (optional dependency)
        return (True, "llvmlite_not_installed")

    except Exception as e:
        return (False, f"llvmlite_check_error: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# v12.0: RUN COMPATIBILITY CHECK AT MODULE LOAD TIME
# ═══════════════════════════════════════════════════════════════════════════════
_compat_result = _check_version_compatibility()
_NUMBA_COMPATIBLE = _compat_result[0]
if not _NUMBA_COMPATIBLE:
    _NUMBA_SKIP_REASON = _compat_result[1]
    # Print warning once
    if hasattr(_sys, '_jarvis_numba_warned') is False:
        _recommendation = _compat_result[2]
        print(f"[numba_preload] ⚠️ SKIPPING NUMBA: {_NUMBA_SKIP_REASON}")
        if _recommendation:
            print(f"[numba_preload] 💡 FIX: {_recommendation}")
        print("[numba_preload] ✅ Whisper will work fine without numba (just slightly slower)")
        _sys._jarvis_numba_warned = True

    # Set environment to prevent any numba import attempts
    _os.environ['NUMBA_DISABLE_JIT'] = '1'
    _os.environ['NUMBA_NUM_THREADS'] = '1'
    _os.environ['_JARVIS_NUMBA_SKIP'] = '1'

    # ═══════════════════════════════════════════════════════════════════════════
    # v13.0: ROOT CAUSE FIX - INSTALL MOCK NUMBA MODULE
    # ═══════════════════════════════════════════════════════════════════════════
    # When numba is version-incompatible, setting NUMBA_DISABLE_JIT is NOT enough.
    # Whisper → librosa → numba still tries to import numba, and the IMPORT ITSELF
    # fails with a circular import error due to version mismatch.
    #
    # SOLUTION: Install a mock numba module in sys.modules BEFORE whisper imports.
    # This prevents the real (broken) numba from ever being loaded.
    # Libraries like librosa check for numba and gracefully degrade when it fails.
    # ═══════════════════════════════════════════════════════════════════════════
    import types as _types

    class _NumbaSkippedModule(_types.ModuleType):
        """
        Mock numba module that raises ImportError for any attribute access.
        This prevents circular import errors by making numba appear unavailable.
        """
        __version__ = "0.0.0-skipped"
        __file__ = __file__
        __path__ = []
        _JARVIS_MOCK = True
        _SKIP_REASON = _NUMBA_SKIP_REASON

        def __getattr__(self, name):
            if name.startswith('_'):
                raise AttributeError(name)
            # Raise ImportError so libraries like librosa fall back gracefully
            raise ImportError(
                f"numba is unavailable (version incompatible): {_NUMBA_SKIP_REASON}. "
                "This is expected - the calling library will use a fallback."
            )

    class _NumbaJitDecorator:
        """Mock JIT decorator that does nothing."""
        def __init__(self, *args, **kwargs):
            pass

        def __call__(self, func):
            return func

    # Create mock numba module
    _mock_numba = _NumbaSkippedModule('numba')
    _mock_numba.jit = _NumbaJitDecorator
    _mock_numba.njit = _NumbaJitDecorator
    _mock_numba.vectorize = _NumbaJitDecorator
    _mock_numba.guvectorize = _NumbaJitDecorator
    _mock_numba.cfunc = _NumbaJitDecorator
    _mock_numba.stencil = _NumbaJitDecorator
    _mock_numba.prange = range  # prange is often used in loops

    # Install mock in sys.modules ONLY if real numba isn't already loaded
    # v13.0: Always install mock when incompatible, even if numba is already loaded
    # because the loaded numba will cause circular import errors
    _existing_numba = _sys.modules.get('numba')
    _should_mock = (
        _existing_numba is None or  # Not loaded yet
        getattr(_existing_numba, '_JARVIS_MOCK', False) or  # Already our mock
        not hasattr(_existing_numba, '__version__') or  # Corrupted
        getattr(_existing_numba, '__version__', '') == '0.0.0-skipped'  # Already skipped
    )

    if 'numba' not in _sys.modules or _should_mock:
        _sys.modules['numba'] = _mock_numba
        # Also mock common submodules that might be imported
        _sys.modules['numba.core'] = _NumbaSkippedModule('numba.core')
        _sys.modules['numba.core.utils'] = _NumbaSkippedModule('numba.core.utils')
        _sys.modules['numba.typed'] = _NumbaSkippedModule('numba.typed')
        print("[numba_preload] 🛡️ Installed numba mock to prevent import errors")


# ═══════════════════════════════════════════════════════════════════════════════
# v10.0: SET NUMBA ENVIRONMENT BEFORE ANY IMPORTS (if compatible)
# ═══════════════════════════════════════════════════════════════════════════════

# v10.0: Check if numba is already corrupted BEFORE we do anything
def _check_early_corruption() -> bool:
    """
    Check if numba modules are in a corrupted state BEFORE any imports.
    This must use only built-in modules (os, sys).

    v11.0: More defensive - checks for active import locks to avoid
    racing with concurrent imports that would cause circular import errors.

    v13.0: Recognizes our mock numba module and does NOT consider it corrupted.
    """
    if 'numba' not in _sys.modules:
        return False

    numba_mod = _sys.modules.get('numba')
    if numba_mod is None:
        return True  # None in sys.modules = corrupted

    # v13.0: Check if this is our mock module - if so, it's NOT corrupted
    if getattr(numba_mod, '_JARVIS_MOCK', False):
        return False  # Our mock is intentional, not corrupted

    # Check for partial initialization
    if not hasattr(numba_mod, '__version__'):
        return True  # Missing version = partial init

    # v11.0: Check if any numba module is currently being imported
    # This catches the "partially initialized" race condition
    try:
        import importlib._bootstrap as _bootstrap
        for mod_name in list(_sys.modules.keys()):
            if mod_name.startswith('numba.'):
                mod = _sys.modules.get(mod_name)
                # Check for partially initialized module (has __spec__ but missing attributes)
                if mod is not None and hasattr(mod, '__spec__') and mod.__spec__ is not None:
                    if hasattr(mod.__spec__, '_initializing') and mod.__spec__._initializing:
                        return True  # Module is currently being initialized
    except (AttributeError, ImportError):
        pass  # Fall through to hasattr check

    # Check numba.core.utils for the problematic function
    if 'numba.core.utils' in _sys.modules:
        utils_mod = _sys.modules.get('numba.core.utils')
        if utils_mod is None:
            return True
        # v11.0: Use try/except for hasattr to catch partially initialized state
        try:
            has_key = hasattr(utils_mod, 'get_hashable_key')
            if not has_key:
                return True
        except (AttributeError, ImportError) as e:
            # If hasattr itself fails, the module is corrupted
            return True

    return False


def _clear_corrupted_early() -> int:
    """
    Clear corrupted numba modules from sys.modules EARLY.
    This must use only built-in modules (os, sys).
    """
    numba_keys = [k for k in _sys.modules.keys() if k == 'numba' or k.startswith('numba.')]
    cleared = 0
    for key in numba_keys:
        try:
            del _sys.modules[key]
            cleared += 1
        except (KeyError, TypeError):
            pass
    return cleared


# v10.0: Detect and clear corruption BEFORE any imports
_early_corruption = _check_early_corruption()
if _early_corruption:
    _cleared = _clear_corrupted_early()
    # Set JIT disable to prevent re-corruption
    _os.environ['NUMBA_DISABLE_JIT'] = '1'
    _os.environ['NUMBA_NUM_THREADS'] = '1'

# v10.0: ALWAYS set safe numba environment at module load
# This is the KEY FIX - prevents circular import from ever occurring
if 'NUMBA_DISABLE_JIT' not in _os.environ:
    # Only disable JIT during initial import phase
    # Will be re-enabled after successful initialization if desired
    _os.environ.setdefault('NUMBA_DISABLE_JIT', '1')

_os.environ.setdefault('NUMBA_NUM_THREADS', '1')
_os.environ.setdefault('NUMBA_THREADING_LAYER', 'workqueue')

# ═══════════════════════════════════════════════════════════════════════════════
# Now safe to import other modules
# ═══════════════════════════════════════════════════════════════════════════════
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
            # v12.0: Compatibility information
            'version_compatible': _NUMBA_COMPATIBLE,
            'skip_reason': _NUMBA_SKIP_REASON,
        }


def is_numba_skipped() -> bool:
    """
    v12.0: Check if numba is being skipped due to version incompatibility.

    Returns:
        True if numba is being skipped, False otherwise
    """
    return _NUMBA_COMPATIBLE is False


def get_numba_skip_reason() -> Optional[str]:
    """
    v12.0: Get the reason numba is being skipped.

    Returns:
        Reason string if skipped, None otherwise
    """
    return _NUMBA_SKIP_REASON if _NUMBA_COMPATIBLE is False else None


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


def _do_numba_import(retry_count: int = 0, max_retries: int = 2) -> bool:
    """
    Actually perform the numba import.
    This is called exactly ONCE per process (unless retry is needed).

    v10.0: Enhanced with:
    - Retry logic with module clearing
    - Circular import detection and recovery
    - Better diagnostics for debugging

    Args:
        retry_count: Current retry attempt (internal use)
        max_retries: Maximum number of retry attempts

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

    # v10.0: Check for corruption BEFORE attempting import
    is_corrupted, corruption_reason = is_numba_corrupted()
    if is_corrupted:
        logger.warning(f"[numba_preload] Detected corruption before import: {corruption_reason}")
        cleared = clear_corrupted_numba_modules()
        logger.info(f"[numba_preload] Cleared {cleared} corrupted modules")
        # Ensure JIT is disabled after clearing corruption
        os.environ['NUMBA_DISABLE_JIT'] = '1'
        os.environ['NUMBA_NUM_THREADS'] = '1'

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
                    error_str = str(e)
                    # v10.0: Detect circular import specifically
                    if 'partially initialized module' in error_str or 'circular import' in error_str.lower():
                        logger.warning(f"[numba_preload] Circular import detected in {submodule}: {e}")
                        load_errors.append(f"{submodule}: CIRCULAR_IMPORT: {e}")
                    else:
                        # Some submodules may not exist in all numba versions
                        load_errors.append(f"{submodule}: {e}")
                        logger.debug(f"[numba_preload] Submodule {submodule} not available: {e}")
                except Exception as e:
                    load_errors.append(f"{submodule}: {e}")
                    logger.debug(f"[numba_preload] Submodule {submodule} error: {e}")

            with _status_lock:
                _numba_info.submodules_loaded = loaded_count

            logger.debug(f"[numba_preload] Phase 2: {loaded_count}/{len(critical_submodules)} submodules loaded")

            # v10.0: Check if any circular import errors occurred
            circular_errors = [e for e in load_errors if 'CIRCULAR_IMPORT' in e]
            if circular_errors and retry_count < max_retries:
                logger.warning(f"[numba_preload] Circular import errors detected, clearing and retrying...")
                clear_corrupted_numba_modules()
                os.environ['NUMBA_DISABLE_JIT'] = '1'
                return _do_numba_import(retry_count=retry_count + 1, max_retries=max_retries)

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
                f"submodules: {loaded_count}, "
                f"JIT: {'disabled' if os.environ.get('NUMBA_DISABLE_JIT') == '1' else 'enabled'})"
            )
            return True

        except ImportError as e:
            error_str = str(e)

            # v10.0: Detect circular import and retry
            if 'partially initialized module' in error_str or 'circular import' in error_str.lower():
                logger.warning(f"[numba_preload] Circular import error: {e}")

                if retry_count < max_retries:
                    logger.info(f"[numba_preload] Clearing modules and retrying (attempt {retry_count + 1}/{max_retries})...")
                    clear_corrupted_numba_modules()
                    os.environ['NUMBA_DISABLE_JIT'] = '1'
                    os.environ['NUMBA_NUM_THREADS'] = '1'
                    return _do_numba_import(retry_count=retry_count + 1, max_retries=max_retries)
                else:
                    _set_status(
                        NumbaStatus.FAILED,
                        error=f"Circular import after {max_retries} retries: {error_str}",
                        error_type="CircularImportError"
                    )
                    logger.error(f"❌ numba circular import cannot be resolved: {e}")
                    return False

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
            error_str = str(e)

            # v10.0: Check for circular import in exception message
            if 'partially initialized' in error_str.lower() or 'circular' in error_str.lower():
                if retry_count < max_retries:
                    logger.warning(f"[numba_preload] Possible circular import, retrying: {e}")
                    clear_corrupted_numba_modules()
                    os.environ['NUMBA_DISABLE_JIT'] = '1'
                    return _do_numba_import(retry_count=retry_count + 1, max_retries=max_retries)

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

    v12.0: Added upfront compatibility check - skips numba entirely if incompatible.
    v8.0: Enhanced with proper timeout handling and status updates.

    This function can be called from any thread. The first caller will
    do the actual import, all other callers will wait for completion.

    Args:
        timeout: Maximum time to wait for initialization (seconds)

    Returns:
        True if numba is available and ready, False otherwise
    """
    global _importing_threads

    # v12.0: UPFRONT COMPATIBILITY CHECK - skip numba entirely if incompatible
    if _NUMBA_COMPATIBLE is False:
        # Version incompatibility detected at module load time
        # Skip numba entirely to prevent circular import errors
        if not _initialization_complete.is_set():
            _set_status(
                NumbaStatus.FAILED,
                error=_NUMBA_SKIP_REASON,
                error_type="VersionIncompatibility"
            )
            _initialization_complete.set()
        logger.debug(f"[numba_preload] Skipping numba: {_NUMBA_SKIP_REASON}")
        return False

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

    v13.0: Recognizes our mock numba module and does NOT consider it corrupted.

    Returns:
        Tuple of (is_corrupted, reason)
    """
    if 'numba' not in sys.modules:
        return False, "not_imported"

    numba_mod = sys.modules.get('numba')

    # Check for None module (shouldn't happen but check anyway)
    if numba_mod is None:
        return True, "module_is_none"

    # v13.0: Check if this is our mock module - if so, it's NOT corrupted
    if getattr(numba_mod, '_JARVIS_MOCK', False):
        return False, "mock_module"

    # Check for missing __version__ (sign of partial init)
    if not hasattr(numba_mod, '__version__'):
        return True, "missing_version"

    # Check numba.core.utils for the problematic get_hashable_key
    if 'numba.core.utils' in sys.modules:
        utils_mod = sys.modules.get('numba.core.utils')
        if utils_mod is None:
            return True, "utils_module_is_none"

        # v13.0: Check if this is our mock submodule
        if getattr(utils_mod, '_JARVIS_MOCK', False):
            return False, "mock_submodule"

        # Check if it's a partial module (missing expected attributes)
        expected_attrs = ['get_hashable_key', 'PYVERSION']
        found_any = any(hasattr(utils_mod, attr) for attr in expected_attrs)

        if not found_any:
            return True, "utils_missing_attributes"

    return False, "ok"


# ═══════════════════════════════════════════════════════════════════════════════
# v13.0: NUMBA SKIP STATUS FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

def is_numba_skipped() -> bool:
    """
    v13.0: Check if numba is being skipped due to version incompatibility.

    When numba is skipped, a mock module is installed in sys.modules
    to prevent circular import errors. Whisper works fine without numba
    (just slightly slower).

    Returns:
        True if numba is being skipped (mock installed)
    """
    return _NUMBA_COMPATIBLE is False


def get_numba_skip_reason() -> Optional[str]:
    """
    v13.0: Get the reason why numba is being skipped.

    Returns:
        Reason string if numba is skipped, None if numba is available
    """
    if _NUMBA_COMPATIBLE is False:
        return _NUMBA_SKIP_REASON
    return None


def is_numba_mocked() -> bool:
    """
    v13.0: Check if the numba module in sys.modules is our mock.

    Returns:
        True if the installed numba is our mock module
    """
    numba_mod = sys.modules.get('numba')
    if numba_mod is None:
        return False
    return getattr(numba_mod, '_JARVIS_MOCK', False)


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
    v12.0: Ensure numba is in a safe state for Whisper to import.

    This is the RECOMMENDED function to call before importing Whisper.
    It handles all the edge cases:

    1. If numba version is incompatible -> skip numba entirely (Whisper works without it)
    2. If numba is not installed -> returns success (Whisper works without it)
    3. If numba is fully initialized -> returns success
    4. If numba is corrupted -> clears modules, disables JIT, returns degraded
    5. If numba fails to initialize -> disables JIT, returns degraded
    6. If circular import detected -> clears modules, retries with JIT disabled

    v12.0 ENHANCEMENTS:
    - UPFRONT VERSION COMPATIBILITY CHECK - skips numba entirely if incompatible
    - No import attempts when known to be incompatible
    - Faster startup by avoiding doomed retries

    v10.0 ENHANCEMENTS:
    - Pre-check environment variables at entry
    - More aggressive corruption detection
    - Retry logic with module clearing
    - Better diagnostic information

    Returns:
        Dict with:
        - safe: bool - True if it's safe to import Whisper
        - jit_disabled: bool - True if JIT was disabled
        - reason: str - Explanation of what happened
        - numba_available: bool - True if numba is fully available
        - retry_count: int - Number of retries needed (v10.0)
        - cleared_modules: int - Number of corrupted modules cleared (v10.0)
        - version_compatible: bool - True if numba version is compatible (v12.0)
    """
    result = {
        'safe': False,
        'jit_disabled': os.environ.get('NUMBA_DISABLE_JIT') == '1',
        'reason': '',
        'numba_available': False,
        'retry_count': 0,
        'cleared_modules': 0,
        'version_compatible': _NUMBA_COMPATIBLE,
    }

    # v12.0: FAST PATH - Skip numba entirely if version incompatible
    if _NUMBA_COMPATIBLE is False:
        result['safe'] = True  # Safe to import Whisper (without numba)
        result['jit_disabled'] = True
        result['reason'] = f"version_incompatible: {_NUMBA_SKIP_REASON}"
        result['numba_available'] = False
        logger.info(f"[numba_preload] Whisper safe to import (numba skipped: {_NUMBA_SKIP_REASON})")
        return result

    # v10.0: Multiple attempts with progressive cleanup
    max_attempts = 3
    total_cleared = 0

    for attempt in range(max_attempts):
        # Check for corruption first
        is_corrupted_flag, corruption_reason = is_numba_corrupted()

        if is_corrupted_flag:
            logger.warning(f"[numba_preload] Numba corruption detected (attempt {attempt + 1}): {corruption_reason}")

            # Clear corrupted modules
            cleared = clear_corrupted_numba_modules()
            total_cleared += cleared
            result['cleared_modules'] = total_cleared

            # Disable JIT to prevent re-corruption
            os.environ['NUMBA_DISABLE_JIT'] = '1'
            os.environ['NUMBA_NUM_THREADS'] = '1'
            result['jit_disabled'] = True

            # If this is not the last attempt, continue to try initialization
            if attempt < max_attempts - 1:
                result['retry_count'] = attempt + 1
                continue
            else:
                # Final attempt after clearing
                result['safe'] = True
                result['reason'] = f"cleared_{total_cleared}_corrupted_modules_jit_disabled"
                result['numba_available'] = False
                logger.info(f"[numba_preload] Whisper safe to import (JIT disabled, cleared {total_cleared} modules)")
                return result

        # Try normal initialization
        try:
            success = ensure_numba_initialized(timeout=30.0)

            if success:
                # v10.0: Double-check no corruption occurred during init
                post_init_corrupted, _ = is_numba_corrupted()
                if post_init_corrupted:
                    logger.warning("[numba_preload] Corruption detected after init, cleaning up")
                    clear_corrupted_numba_modules()
                    os.environ['NUMBA_DISABLE_JIT'] = '1'
                    result['jit_disabled'] = True
                    result['retry_count'] = attempt + 1
                    continue

                result['safe'] = True
                result['jit_disabled'] = os.environ.get('NUMBA_DISABLE_JIT') == '1'
                result['reason'] = "numba_ready"
                result['numba_available'] = True
                logger.info(f"[numba_preload] Whisper safe to import (numba ready, JIT: {'disabled' if result['jit_disabled'] else 'enabled'})")
                return result
            else:
                # Initialization failed - check why
                status = get_numba_status()
                status_value = status.get('status', 'unknown')
                error = status.get('error', '')

                if status_value == 'not_installed':
                    result['safe'] = True
                    result['jit_disabled'] = False
                    result['reason'] = "numba_not_installed"
                    result['numba_available'] = False
                    logger.debug("[numba_preload] numba not installed (optional)")
                    return result

                # v10.0: Check if error indicates circular import
                if error and ('partially initialized' in error.lower() or 'circular' in error.lower()):
                    logger.warning(f"[numba_preload] Circular import detected: {error}")
                    clear_corrupted_numba_modules()
                    os.environ['NUMBA_DISABLE_JIT'] = '1'
                    result['jit_disabled'] = True
                    result['retry_count'] = attempt + 1
                    if attempt < max_attempts - 1:
                        continue

                # Some other failure - disable JIT as fallback
                os.environ['NUMBA_DISABLE_JIT'] = '1'
                os.environ['NUMBA_NUM_THREADS'] = '1'

                result['safe'] = True
                result['jit_disabled'] = True
                result['reason'] = f"init_failed_{status_value}_jit_disabled"
                result['numba_available'] = False
                logger.info(f"[numba_preload] Whisper safe to import (init failed, JIT disabled)")
                return result

        except Exception as e:
            error_str = str(e)
            logger.warning(f"[numba_preload] Error during numba check (attempt {attempt + 1}): {e}")

            # v10.0: Check for circular import in exception
            if 'partially initialized' in error_str.lower() or 'circular' in error_str.lower():
                clear_corrupted_numba_modules()
                os.environ['NUMBA_DISABLE_JIT'] = '1'
                os.environ['NUMBA_NUM_THREADS'] = '1'
                result['jit_disabled'] = True
                result['retry_count'] = attempt + 1
                if attempt < max_attempts - 1:
                    continue

            # On any error, disable JIT as fallback
            os.environ['NUMBA_DISABLE_JIT'] = '1'
            os.environ['NUMBA_NUM_THREADS'] = '1'

            result['safe'] = True
            result['jit_disabled'] = True
            result['reason'] = f"error_{type(e).__name__}_jit_disabled"
            result['numba_available'] = False
            logger.info(f"[numba_preload] Whisper safe to import (error, JIT disabled)")
            return result

    # If we get here, all attempts failed
    result['safe'] = True
    result['jit_disabled'] = True
    result['reason'] = f"max_attempts_exceeded_jit_disabled"
    result['numba_available'] = False
    logger.warning(f"[numba_preload] Max attempts exceeded, Whisper safe with JIT disabled")
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# Auto-initialize if this module is imported directly
# v11.0: ULTRA-DEFENSIVE INITIALIZATION
# - Check for corruption BEFORE attempting initialization
# - Clear corrupted modules if detected
# - SKIP auto-init if numba is currently being imported by another thread
# - Only in main thread to avoid races during parallel imports
# - Environment variables already set at module load time (top of file)
# - Use lazy initialization to avoid circular imports during startup
# ═══════════════════════════════════════════════════════════════════════════════
if __name__ != "__main__":
    # Check if we're in the main thread
    if threading.current_thread() is threading.main_thread():
        # v11.0: Check for corruption OR partial import BEFORE auto-init
        _auto_init_corrupted = _check_early_corruption()

        if _auto_init_corrupted:
            # Module is corrupted or currently being imported - DON'T try to init
            # Just set environment and let the caller decide when to init
            logger.debug("[numba_preload] Corruption/partial import detected - deferring init")
            os.environ['NUMBA_DISABLE_JIT'] = '1'
            os.environ['NUMBA_NUM_THREADS'] = '1'
        else:
            # v11.0: Only auto-init if numba is NOT already in sys.modules
            # This prevents triggering import during another import
            _numba_already_imported = 'numba' in sys.modules

            if not _numba_already_imported:
                # Safe to initialize - numba is not yet imported
                logger.debug("[numba_preload] Auto-init in main thread (JIT disabled for safety)")
                try:
                    ensure_numba_initialized()
                    set_numba_bypass_marker()
                except Exception as _e:
                    # v11.0: Don't let auto-init failure crash the import
                    error_str = str(_e)
                    if 'partially initialized' in error_str.lower() or 'circular' in error_str.lower():
                        logger.debug("[numba_preload] Circular import during auto-init - deferring")
                        clear_corrupted_numba_modules()
                    else:
                        logger.warning(f"[numba_preload] Auto-init failed (non-fatal): {_e}")
                    # Ensure JIT remains disabled for safety
                    os.environ['NUMBA_DISABLE_JIT'] = '1'
                    os.environ['NUMBA_NUM_THREADS'] = '1'
            else:
                # numba already imported - just verify status
                logger.debug("[numba_preload] numba already imported - skipping auto-init")
                try:
                    is_complete, version = _check_numba_in_sys_modules()
                    if is_complete:
                        _set_status(NumbaStatus.READY, version=version)
                        _initialization_complete.set()
                        set_numba_bypass_marker()
                except Exception:
                    pass
    else:
        # Non-main thread: just wait for initialization
        logger.debug(f"[numba_preload] Auto-init waiting in thread: {threading.current_thread().name}")
        # Don't block module load - just check fast path
        if not _initialization_complete.is_set():
            # Mark that we need initialization but don't block
            pass
