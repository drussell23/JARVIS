#!/usr/bin/env python3
"""
Robust Whisper audio handler that works with any input format

INTEGRATED FEATURES:
- VAD (Voice Activity Detection) filtering via WebRTC-VAD + Silero VAD
- Audio windowing/truncation to prevent hallucinations (5s global, 2s unlock)
- Silence and noise removal BEFORE Whisper sees audio
- Ultra-low latency for command and unlock flows

ADVANCED LAZY IMPORT SYSTEM:
- Async-safe and thread-safe singleton pattern
- Dynamic configuration from environment variables
- Health monitoring and status tracking
- Circuit breaker pattern for resilience
- Retry with exponential backoff
- Parallel initialization capabilities
- Metrics collection for performance tracking

v10.0: CRITICAL NUMBA FIX (PRODUCTION-GRADE)
- PRE-EMPTIVE JIT disable at module load time
- Early corruption detection BEFORE any numba imports
- Aggressive module clearing on corruption
- Retry logic with progressive cleanup
- Uses centralized numba_preload v10.0 for process-wide safety
"""

# =============================================================================
# CRITICAL: NUMBA PRE-INITIALIZATION v10.0 - MUST BE FIRST
# =============================================================================
# This MUST happen before ANY other imports that might use numba
# (numpy operations, whisper, librosa, scipy with JIT, etc.)
#
# v10.0: Uses ensure_numba_safe_for_whisper() which:
# 1. Sets NUMBA_DISABLE_JIT=1 at module load time (in numba_preload)
# 2. Detects corrupted/partial numba modules before import
# 3. Clears them from sys.modules if corrupted
# 4. Retries with JIT disabled if circular import detected
# 5. Returns a safe state for Whisper to import
#
# This FULLY prevents the circular import error:
#   "cannot import name 'get_hashable_key' from partially initialized module"
# =============================================================================
import os
import sys
import threading

_numba_ready = False
_numba_version = None
_numba_jit_disabled = False
_numba_retry_count = 0
_numba_cleared_modules = 0

# v11.0: CRITICAL - Set environment FIRST before ANY imports
# This is the PRIMARY defense against circular imports - must happen BEFORE
# importing numba_preload or anything else that might touch numba
os.environ.setdefault('NUMBA_DISABLE_JIT', '1')
os.environ.setdefault('NUMBA_NUM_THREADS', '1')
os.environ.setdefault('NUMBA_THREADING_LAYER', 'workqueue')

# v11.0: Check for partial numba import BEFORE importing numba_preload
# This catches the race condition where numba is being imported by another thread
_numba_partial_import = False
if 'numba' in sys.modules:
    _numba_mod = sys.modules.get('numba')
    if _numba_mod is None or not hasattr(_numba_mod, '__version__'):
        _numba_partial_import = True
        _numba_jit_disabled = True

try:
    if _numba_partial_import:
        # Partial import detected - skip numba_preload initialization
        # Just use JIT disabled mode
        if threading.current_thread() is threading.main_thread():
            print("[whisper_audio_fix] ⚠️ numba partial import detected - using JIT disabled mode")
    else:
        # Import the centralized numba preloader with v12.0 compatibility checking
        # NOTE: Just importing numba_preload will:
        # 1. Check version compatibility FIRST (v12.0)
        # 2. Set NUMBA_DISABLE_JIT=1 if incompatible or at module load time
        # 3. Skip numba entirely if version incompatible (no import attempts)
        from core.numba_preload import (
            ensure_numba_safe_for_whisper,
            is_numba_ready,
            get_numba_status,
            is_numba_corrupted,
            clear_corrupted_numba_modules,
            is_numba_skipped,  # v12.0: New function
            get_numba_skip_reason,  # v12.0: New function
        )

        # v12.0: Check if numba is being skipped due to version incompatibility
        if is_numba_skipped():
            skip_reason = get_numba_skip_reason()
            if threading.current_thread() is threading.main_thread():
                print(f"[whisper_audio_fix] ℹ️ numba skipped: {skip_reason}")
                print("[whisper_audio_fix] ✅ Whisper will work fine without numba")
            _numba_ready = False
            _numba_jit_disabled = True
            _numba_retry_count = 0
            _numba_cleared_modules = 0
            # No need to check further - numba is skipped
        else:
            # v11.0: Use the new safe initialization that handles corruption
            # This is the RECOMMENDED approach - it handles all edge cases
            safety_result = ensure_numba_safe_for_whisper()

            _numba_ready = safety_result.get('numba_available', False)
            _numba_jit_disabled = safety_result.get('jit_disabled', False)
            _numba_retry_count = safety_result.get('retry_count', 0)
            _numba_cleared_modules = safety_result.get('cleared_modules', 0)

            if _numba_ready:
                status = get_numba_status()
                _numba_version = status.get('version')
                if threading.current_thread() is threading.main_thread():
                    jit_status = "JIT disabled" if _numba_jit_disabled else "JIT enabled"
                    print(f"[whisper_audio_fix] ✅ numba {_numba_version} ready ({jit_status})")
            elif _numba_jit_disabled:
                # JIT disabled due to corruption or failure - this is OK
                if threading.current_thread() is threading.main_thread():
                    reason = safety_result.get('reason', 'unknown')
                    extra = ""
                    if _numba_retry_count > 0:
                        extra = f", retries: {_numba_retry_count}"
                    if _numba_cleared_modules > 0:
                        extra += f", cleared: {_numba_cleared_modules} modules"
                    print(f"[whisper_audio_fix] ⚠️ numba JIT disabled: {reason}{extra}")
                    print("[whisper_audio_fix] Whisper will work but without numba optimization")
            else:
                # numba not installed or other benign state
                if threading.current_thread() is threading.main_thread():
                    reason = safety_result.get('reason', 'unknown')
                    if 'not_installed' in reason:
                        print("[whisper_audio_fix] numba not installed (optional)")
                    else:
                        print(f"[whisper_audio_fix] numba status: {reason}")

except ImportError as e:
    # numba_preload module doesn't exist OR circular import occurred
    error_str = str(e)
    is_circular = 'partially initialized' in error_str.lower() or 'circular' in error_str.lower()

    if is_circular:
        # v11.0: Circular import - clear numba and use JIT disabled
        if threading.current_thread() is threading.main_thread():
            print(f"[whisper_audio_fix] ⚠️ Circular import during numba_preload: {e}")
        # Clear any partial numba modules
        for mod_name in list(sys.modules.keys()):
            if mod_name == 'numba' or mod_name.startswith('numba.'):
                try:
                    del sys.modules[mod_name]
                except (KeyError, TypeError):
                    pass
        _numba_jit_disabled = True
    else:
        # numba_preload not available - use legacy fallback
        if threading.current_thread() is threading.main_thread():
            print("[whisper_audio_fix] numba_preload not available, using safe fallback")

        try:
            import numba
            _numba_version = numba.__version__
            _numba_ready = False  # JIT disabled, so not "fully" ready
            if threading.current_thread() is threading.main_thread():
                print(f"[whisper_audio_fix] numba {_numba_version} imported (JIT disabled)")
        except ImportError:
            if threading.current_thread() is threading.main_thread():
                print("[whisper_audio_fix] numba not installed (optional)")
        except Exception as inner_e:
            inner_error_str = str(inner_e)
            if 'partially initialized' in inner_error_str.lower():
                # Clear corrupted numba modules
                for mod_name in list(sys.modules.keys()):
                    if mod_name == 'numba' or mod_name.startswith('numba.'):
                        try:
                            del sys.modules[mod_name]
                        except (KeyError, TypeError):
                            pass
            if threading.current_thread() is threading.main_thread():
                print(f"[whisper_audio_fix] ⚠️ numba fallback error: {inner_e}")
        _numba_jit_disabled = True

except Exception as e:
    # Any other error - disable JIT as safety measure
    error_str = str(e)
    if threading.current_thread() is threading.main_thread():
        # v11.0: Check if this is a circular import error and provide helpful message
        if 'partially initialized' in error_str.lower() or 'circular' in error_str.lower():
            print(f"[whisper_audio_fix] ⚠️ numba circular import detected, clearing modules")
            # Clear any corrupted numba modules
            for mod_name in list(sys.modules.keys()):
                if mod_name == 'numba' or mod_name.startswith('numba.'):
                    try:
                        del sys.modules[mod_name]
                    except (KeyError, TypeError):
                        pass
        else:
            print(f"[whisper_audio_fix] ⚠️ numba pre-init error: {e}")
    _numba_jit_disabled = True

# =============================================================================
# NOW SAFE TO IMPORT OTHER MODULES
# =============================================================================
import base64
import numpy as np
import tempfile
import logging
import asyncio
import soundfile as sf
import io
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, Callable
from enum import Enum
from functools import wraps

logger = logging.getLogger(__name__)


# =============================================================================
# DYNAMIC CONFIGURATION - Environment-driven, no hardcoding
# =============================================================================
class WhisperConfig:
    """
    Dynamic configuration for Whisper import and model loading.
    All values can be overridden via environment variables.

    Environment Variables:
        WHISPER_MODEL_SIZE: Model size (tiny, base, small, medium, large) - default: 'base'
        WHISPER_DEVICE: Device selection (auto, cpu, cuda, mps) - default: 'auto'
        WHISPER_FP16: Use FP16 precision - default: false
        WHISPER_FORCE_MPS: Force MPS even if sparse probe fails - default: false
        WHISPER_MPS_SPARSE_FALLBACK: Action on MPS sparse failure ('cpu' or 'error') - default: 'cpu'
        WHISPER_IMPORT_TIMEOUT: Timeout for module import in seconds - default: 30.0
        WHISPER_LOAD_TIMEOUT: Timeout for model loading in seconds - default: 120.0
        WHISPER_RETRY_ATTEMPTS: Number of retry attempts - default: 3
        WHISPER_RETRY_BASE_DELAY: Base delay between retries - default: 1.0
        WHISPER_RETRY_MAX_DELAY: Maximum delay between retries - default: 30.0
        WHISPER_CIRCUIT_FAILURE_THRESHOLD: Failures before circuit opens - default: 3
        WHISPER_CIRCUIT_RECOVERY_TIMEOUT: Timeout before circuit half-opens - default: 60.0
        WHISPER_HEALTH_CHECK_INTERVAL: Health check interval - default: 30.0
        WHISPER_PREWARM_ON_INIT: Prewarm model on init - default: true
        WHISPER_PREWARM_ASYNC: Use async prewarming - default: true
    """

    def __init__(self):
        self._config = {
            # Model configuration
            'model_size': os.getenv('WHISPER_MODEL_SIZE', 'base'),
            'model_device': os.getenv('WHISPER_DEVICE', 'auto'),  # auto, cpu, cuda, mps
            'fp16': os.getenv('WHISPER_FP16', 'false').lower() == 'true',

            # MPS (Apple Silicon) configuration
            'force_mps': os.getenv('WHISPER_FORCE_MPS', 'false').lower() == 'true',
            'mps_sparse_fallback': os.getenv('WHISPER_MPS_SPARSE_FALLBACK', 'cpu').lower(),

            # Import configuration
            'import_timeout': float(os.getenv('WHISPER_IMPORT_TIMEOUT', '30.0')),
            'load_timeout': float(os.getenv('WHISPER_LOAD_TIMEOUT', '120.0')),
            'retry_attempts': int(os.getenv('WHISPER_RETRY_ATTEMPTS', '3')),
            'retry_base_delay': float(os.getenv('WHISPER_RETRY_BASE_DELAY', '1.0')),
            'retry_max_delay': float(os.getenv('WHISPER_RETRY_MAX_DELAY', '30.0')),

            # Circuit breaker configuration
            'circuit_failure_threshold': int(os.getenv('WHISPER_CIRCUIT_FAILURE_THRESHOLD', '3')),
            'circuit_recovery_timeout': float(os.getenv('WHISPER_CIRCUIT_RECOVERY_TIMEOUT', '60.0')),

            # Health monitoring
            'health_check_interval': float(os.getenv('WHISPER_HEALTH_CHECK_INTERVAL', '30.0')),

            # Prewarming
            'prewarm_on_init': os.getenv('WHISPER_PREWARM_ON_INIT', 'true').lower() == 'true',
            'prewarm_async': os.getenv('WHISPER_PREWARM_ASYNC', 'true').lower() == 'true',
        }

    def get(self, key: str, default: Any = None) -> Any:
        return self._config.get(key, default)

    def __getattr__(self, name: str) -> Any:
        if name.startswith('_'):
            return super().__getattribute__(name)
        return self._config.get(name)

    def to_dict(self) -> Dict[str, Any]:
        return dict(self._config)


# Global config instance
_whisper_config = WhisperConfig()


def get_whisper_config() -> WhisperConfig:
    """Get the global Whisper configuration."""
    return _whisper_config


# =============================================================================
# CIRCUIT BREAKER PATTERN - Resilience for import failures
# =============================================================================
class CircuitState(Enum):
    CLOSED = "closed"      # Normal operation
    OPEN = "open"          # Failing, reject requests
    HALF_OPEN = "half_open"  # Testing if recovered


@dataclass
class CircuitBreakerMetrics:
    """Metrics for the circuit breaker."""
    total_attempts: int = 0
    successful_imports: int = 0
    failed_imports: int = 0
    consecutive_failures: int = 0
    last_failure_time: float = 0.0
    last_success_time: float = 0.0
    last_error: Optional[str] = None
    state_changes: int = 0


class ImportCircuitBreaker:
    """
    Circuit breaker for whisper import to prevent repeated failures
    from blocking the system.
    """

    def __init__(self, config: WhisperConfig):
        self.config = config
        self.state = CircuitState.CLOSED
        self.metrics = CircuitBreakerMetrics()
        self._lock = threading.Lock()
        self._async_lock: Optional[asyncio.Lock] = None

    @property
    def is_available(self) -> bool:
        """Check if circuit allows import attempts."""
        with self._lock:
            if self.state == CircuitState.CLOSED:
                return True
            elif self.state == CircuitState.OPEN:
                # Check if recovery timeout has passed
                elapsed = time.time() - self.metrics.last_failure_time
                if elapsed >= self.config.circuit_recovery_timeout:
                    self._transition_to_half_open()
                    return True
                return False
            else:  # HALF_OPEN
                return True

    def record_success(self):
        """Record a successful import."""
        with self._lock:
            self.metrics.total_attempts += 1
            self.metrics.successful_imports += 1
            self.metrics.consecutive_failures = 0
            self.metrics.last_success_time = time.time()

            if self.state == CircuitState.HALF_OPEN:
                self._transition_to_closed()

    def record_failure(self, error: str):
        """Record a failed import."""
        with self._lock:
            self.metrics.total_attempts += 1
            self.metrics.failed_imports += 1
            self.metrics.consecutive_failures += 1
            self.metrics.last_failure_time = time.time()
            self.metrics.last_error = error

            if self.state == CircuitState.HALF_OPEN:
                self._transition_to_open()
            elif self.metrics.consecutive_failures >= self.config.circuit_failure_threshold:
                self._transition_to_open()

    def _transition_to_open(self):
        """Transition to OPEN state."""
        if self.state != CircuitState.OPEN:
            logger.warning(f"🔴 Whisper import circuit breaker: OPEN (failures: {self.metrics.consecutive_failures})")
            self.state = CircuitState.OPEN
            self.metrics.state_changes += 1

    def _transition_to_half_open(self):
        """Transition to HALF_OPEN state."""
        logger.info("🟡 Whisper import circuit breaker: HALF_OPEN (testing recovery)")
        self.state = CircuitState.HALF_OPEN
        self.metrics.state_changes += 1

    def _transition_to_closed(self):
        """Transition to CLOSED state."""
        logger.info("🟢 Whisper import circuit breaker: CLOSED (recovered)")
        self.state = CircuitState.CLOSED
        self.metrics.state_changes += 1
        self.metrics.consecutive_failures = 0

    def reset(self):
        """Force reset the circuit breaker."""
        with self._lock:
            self.state = CircuitState.CLOSED
            self.metrics.consecutive_failures = 0
            logger.info("Whisper import circuit breaker manually reset")

    def to_dict(self) -> Dict[str, Any]:
        """Export circuit breaker state for monitoring."""
        with self._lock:
            return {
                'state': self.state.value,
                'is_available': self.is_available,
                'metrics': {
                    'total_attempts': self.metrics.total_attempts,
                    'successful_imports': self.metrics.successful_imports,
                    'failed_imports': self.metrics.failed_imports,
                    'consecutive_failures': self.metrics.consecutive_failures,
                    'last_error': self.metrics.last_error,
                    'state_changes': self.metrics.state_changes,
                }
            }


# =============================================================================
# ADVANCED LAZY IMPORT MANAGER - Thread-safe, async-safe singleton
# =============================================================================
@dataclass
class WhisperImportStatus:
    """Status of the Whisper import system."""
    module_loaded: bool = False
    model_loaded: bool = False
    import_time_ms: float = 0.0
    load_time_ms: float = 0.0
    numba_version: Optional[str] = None
    whisper_version: Optional[str] = None
    last_health_check: float = 0.0
    health_score: float = 1.0
    error_message: Optional[str] = None


class WhisperImportManager:
    """
    Advanced lazy import manager for Whisper with:
    - Thread-safe and async-safe operations
    - Circuit breaker pattern
    - Retry with exponential backoff
    - Health monitoring
    - Metrics collection
    - Dynamic configuration
    """

    _instance: Optional['WhisperImportManager'] = None
    _instance_lock = threading.Lock()

    def __new__(cls):
        """Singleton pattern - thread-safe."""
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self.config = get_whisper_config()
        self.circuit_breaker = ImportCircuitBreaker(self.config)
        self.status = WhisperImportStatus()

        # Thread synchronization
        self._import_lock = threading.Lock()
        self._async_import_lock: Optional[asyncio.Lock] = None

        # Cached module reference
        self._whisper_module = None
        self._import_error: Optional[str] = None

        # Metrics
        self._import_start_time: float = 0.0

        # v14.0: Auto-reset circuit breaker if numba is now working
        self._try_auto_reset_circuit_breaker()

        self._initialized = True
        logger.info("🎤 WhisperImportManager initialized")

    def _try_auto_reset_circuit_breaker(self):
        """
        v14.0: Check if numba issue is now resolved and reset circuit breaker.

        This helps recover from previous failures when the underlying issue
        (numba circular import) has been fixed.
        """
        try:
            from core.numba_preload import (
                is_numba_ready,
                is_numba_skipped,
                get_numba_status
            )

            status = get_numba_status()

            # If numba is ready or properly skipped, the issue is resolved
            if status.get('is_ready') or is_numba_skipped():
                # Check if we had previous failures
                if self.circuit_breaker.metrics.consecutive_failures > 0:
                    logger.info(
                        f"🔄 Auto-resetting circuit breaker: numba is now "
                        f"{'ready' if status.get('is_ready') else 'properly skipped'} "
                        f"(had {self.circuit_breaker.metrics.consecutive_failures} failures)"
                    )
                    self.circuit_breaker.reset()
        except Exception as e:
            # Don't fail initialization if this check fails
            logger.debug(f"Circuit breaker auto-reset check skipped: {e}")

    @property
    def is_module_loaded(self) -> bool:
        """Check if Whisper module is loaded."""
        return self._whisper_module is not None

    def _ensure_async_lock(self) -> asyncio.Lock:
        """Ensure async lock exists (lazy initialization)."""
        if self._async_import_lock is None:
            self._async_import_lock = asyncio.Lock()
        return self._async_import_lock

    def _pre_import_numba(self) -> Optional[str]:
        """
        Pre-import numba using centralized process-level loader.

        v9.0: Enhanced with corruption detection and recovery.

        The module-level code at the top of this file already called
        ensure_numba_safe_for_whisper() which:
        - Detects corrupted numba modules
        - Clears them from sys.modules
        - Disables JIT if needed

        This method returns the cached result for instant response.

        Returns numba version if successful, None otherwise.
        """
        global _numba_ready, _numba_version, _numba_jit_disabled

        # Check instance cache first
        if hasattr(self, '_numba_version_cache'):
            return self._numba_version_cache

        # Use module-level result (already initialized at import time)
        if _numba_ready and _numba_version:
            self._numba_version_cache = _numba_version
            logger.debug(f"numba {_numba_version} ready (module-level init)")
            return _numba_version

        # v9.0: If JIT was disabled due to corruption, return None but mark as safe
        if _numba_jit_disabled:
            logger.debug("numba JIT disabled (corruption recovery or fallback)")
            self._numba_version_cache = None
            return None

        # Double-check with centralized loader
        try:
            from core.numba_preload import (
                is_numba_ready as check_numba_ready,
                get_numba_status,
                is_numba_corrupted,
                ensure_numba_safe_for_whisper,
            )

            # v9.0: Check for corruption first
            is_corrupted, corruption_reason = is_numba_corrupted()
            if is_corrupted:
                logger.warning(f"numba corruption detected during import check: {corruption_reason}")
                # Use safe initialization to clean up
                safety_result = ensure_numba_safe_for_whisper()
                self._numba_version_cache = None
                return None

            if check_numba_ready():
                status = get_numba_status()
                version = status.get('version')
                self._numba_version_cache = version
                logger.debug(f"numba {version} ready via centralized loader")
                return version
            else:
                status = get_numba_status()
                if status.get('status') == 'not_installed':
                    logger.debug("numba not installed (optional dependency)")
                else:
                    logger.debug(f"numba status: {status.get('status')}")
                self._numba_version_cache = None
                return None

        except ImportError:
            logger.debug("numba_preload not available")
            self._numba_version_cache = None
            return None
        except Exception as e:
            logger.debug(f"numba status check error: {e}")
            self._numba_version_cache = None
            return None

    def _do_import(self) -> Any:
        """
        Perform the actual whisper import.
        This is the core import logic, separated for reuse.
        """
        # Pre-import numba
        numba_version = self._pre_import_numba()
        self.status.numba_version = numba_version

        # Import whisper
        import whisper as whisper_mod

        # Store version info
        if hasattr(whisper_mod, '__version__'):
            self.status.whisper_version = whisper_mod.__version__

        return whisper_mod

    def import_sync(self) -> Any:
        """
        Synchronous import of whisper module.
        Thread-safe with circuit breaker and retry logic.

        Returns:
            The whisper module

        Raises:
            ImportError: If import fails after all retries
        """
        # Fast path - already loaded
        if self._whisper_module is not None:
            return self._whisper_module

        # Check circuit breaker
        if not self.circuit_breaker.is_available:
            raise ImportError(
                f"Whisper import circuit breaker is OPEN. "
                f"Last error: {self.circuit_breaker.metrics.last_error}. "
                f"Will retry after {self.config.circuit_recovery_timeout}s"
            )

        with self._import_lock:
            # Double-check after acquiring lock
            if self._whisper_module is not None:
                return self._whisper_module

            # Re-raise cached error if previous import failed permanently
            if self._import_error is not None and not self.circuit_breaker.is_available:
                raise ImportError(f"Whisper import previously failed: {self._import_error}")

            # Retry with exponential backoff
            last_error = None
            for attempt in range(self.config.retry_attempts):
                try:
                    start_time = time.time()

                    logger.info(f"🔄 Importing whisper (attempt {attempt + 1}/{self.config.retry_attempts})...")
                    self._whisper_module = self._do_import()

                    # Success!
                    elapsed_ms = (time.time() - start_time) * 1000
                    self.status.module_loaded = True
                    self.status.import_time_ms = elapsed_ms
                    self.circuit_breaker.record_success()

                    logger.info(f"✅ Whisper module imported in {elapsed_ms:.0f}ms")
                    return self._whisper_module

                except Exception as e:
                    last_error = str(e)
                    self.circuit_breaker.record_failure(last_error)

                    if attempt < self.config.retry_attempts - 1:
                        # Calculate backoff delay
                        delay = min(
                            self.config.retry_base_delay * (2 ** attempt),
                            self.config.retry_max_delay
                        )
                        logger.warning(
                            f"⚠️ Whisper import attempt {attempt + 1} failed: {e}. "
                            f"Retrying in {delay:.1f}s..."
                        )
                        time.sleep(delay)
                    else:
                        logger.error(f"❌ Whisper import failed after {self.config.retry_attempts} attempts: {e}")

            # All retries exhausted
            self._import_error = last_error
            self.status.error_message = last_error
            raise ImportError(f"Whisper import failed after {self.config.retry_attempts} attempts: {last_error}")

    async def import_async(self) -> Any:
        """
        Asynchronous import of whisper module.
        Non-blocking with circuit breaker and retry logic.

        Returns:
            The whisper module

        Raises:
            ImportError: If import fails after all retries
        """
        # Fast path - already loaded
        if self._whisper_module is not None:
            return self._whisper_module

        # Check circuit breaker
        if not self.circuit_breaker.is_available:
            raise ImportError(
                f"Whisper import circuit breaker is OPEN. "
                f"Last error: {self.circuit_breaker.metrics.last_error}"
            )

        async_lock = self._ensure_async_lock()
        async with async_lock:
            # Double-check after acquiring lock
            if self._whisper_module is not None:
                return self._whisper_module

            # Retry with exponential backoff
            last_error = None
            for attempt in range(self.config.retry_attempts):
                try:
                    start_time = time.time()

                    logger.info(f"🔄 Importing whisper async (attempt {attempt + 1}/{self.config.retry_attempts})...")

                    # Run import in thread pool to avoid blocking
                    self._whisper_module = await asyncio.wait_for(
                        asyncio.to_thread(self._do_import),
                        timeout=self.config.import_timeout
                    )

                    # Success!
                    elapsed_ms = (time.time() - start_time) * 1000
                    self.status.module_loaded = True
                    self.status.import_time_ms = elapsed_ms
                    self.circuit_breaker.record_success()

                    logger.info(f"✅ Whisper module imported async in {elapsed_ms:.0f}ms")
                    return self._whisper_module

                except asyncio.TimeoutError:
                    last_error = f"Import timed out after {self.config.import_timeout}s"
                    self.circuit_breaker.record_failure(last_error)
                    logger.warning(f"⏱️ Whisper import timeout (attempt {attempt + 1})")

                except Exception as e:
                    last_error = str(e)
                    self.circuit_breaker.record_failure(last_error)

                    if attempt < self.config.retry_attempts - 1:
                        delay = min(
                            self.config.retry_base_delay * (2 ** attempt),
                            self.config.retry_max_delay
                        )
                        logger.warning(f"⚠️ Whisper import attempt {attempt + 1} failed: {e}. Retrying in {delay:.1f}s...")
                        await asyncio.sleep(delay)
                    else:
                        logger.error(f"❌ Whisper import failed after {self.config.retry_attempts} attempts: {e}")

            # All retries exhausted
            self._import_error = last_error
            self.status.error_message = last_error
            raise ImportError(f"Whisper import failed: {last_error}")

    def get_health_status(self) -> Dict[str, Any]:
        """Get comprehensive health status of the import manager."""
        now = time.time()

        return {
            'status': self.status.__dict__,
            'circuit_breaker': self.circuit_breaker.to_dict(),
            'config': self.config.to_dict(),
            'uptime_seconds': now - self._import_start_time if self._import_start_time > 0 else 0,
        }

    def reset(self):
        """Reset the import manager state."""
        with self._import_lock:
            self._whisper_module = None
            self._import_error = None
            self.status = WhisperImportStatus()
            self.circuit_breaker.reset()
            logger.info("WhisperImportManager reset")


# Global import manager instance
_import_manager: Optional[WhisperImportManager] = None


def get_import_manager() -> WhisperImportManager:
    """Get the global Whisper import manager."""
    global _import_manager
    if _import_manager is None:
        _import_manager = WhisperImportManager()
    return _import_manager


def _lazy_import_whisper():
    """
    Lazy import of whisper module using the advanced import manager.

    This is the main entry point for synchronous whisper imports.
    Thread-safe with circuit breaker and retry logic.

    Returns:
        The whisper module

    Raises:
        ImportError: If import fails
    """
    return get_import_manager().import_sync()


async def _lazy_import_whisper_async():
    """
    Async lazy import of whisper module using the advanced import manager.

    This is the main entry point for asynchronous whisper imports.
    Non-blocking with circuit breaker and retry logic.

    Returns:
        The whisper module

    Raises:
        ImportError: If import fails
    """
    return await get_import_manager().import_async()

try:
    import librosa
    LIBROSA_AVAILABLE = True
except ImportError:
    LIBROSA_AVAILABLE = False
    logging.warning("librosa not available - will use basic resampling")

# VAD and windowing imports
try:
    from .vad import get_vad_pipeline, VADConfig, VADPipelineConfig
    from .audio_windowing import get_window_manager, WindowConfig
    VAD_AVAILABLE = True
except ImportError:
    VAD_AVAILABLE = False
    logging.warning("VAD/windowing not available - will skip filtering")

logger = logging.getLogger(__name__)

class WhisperAudioHandler:
    """
    Handles any audio format for Whisper transcription

    NOW WITH:
    - VAD filtering (WebRTC-VAD + Silero VAD)
    - Audio windowing (5s global, 2s unlock)
    - Silence removal before transcription
    - Non-blocking async model loading with thread pool
    """

    # Constants for async loading
    MODEL_LOAD_TIMEOUT = 120.0  # 2 minutes timeout for model loading

    def __init__(self):
        self.model = None
        self._model_loading = False
        self._model_load_lock = threading.Lock()
        self._async_model_load_lock = asyncio.Lock() if asyncio.get_event_loop().is_running() else None
        self._model_load_event = threading.Event()

        # Thread pool removed for macOS stability
        # self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="whisper_loader")
        self._executor = None

        # Initialize VAD pipeline (lazy-loaded)
        self.vad_pipeline = None
        self.vad_enabled = VAD_AVAILABLE and os.getenv('ENABLE_VAD', 'true').lower() == 'true'

        # Initialize window manager (lazy-loaded)
        self.window_manager = None
        self.windowing_enabled = VAD_AVAILABLE and os.getenv('ENABLE_WINDOWING', 'true').lower() == 'true'

        logger.info("🎤 Whisper Audio Handler initialized")
        logger.info(f"   VAD enabled: {self.vad_enabled}")
        logger.info(f"   Windowing enabled: {self.windowing_enabled}")

    def _infer_sample_rate(self, audio_bytes: bytes, num_samples: int) -> int:
        """
        Intelligently infer sample rate from audio characteristics.

        Uses multiple heuristics:
        1. Audio duration estimation (if we know expected duration)
        2. Frequency content analysis
        3. Common sample rates for different sources

        Args:
            audio_bytes: Raw audio data
            num_samples: Number of audio samples detected

        Returns:
            Inferred sample rate in Hz
        """
        # Common sample rates to test
        common_rates = [48000, 44100, 32000, 24000, 16000, 22050, 11025, 8000]

        # Heuristic 1: Audio byte size can hint at sample rate
        # Typical voice command is 2-5 seconds
        # For int16 PCM: bytes = samples * 2
        audio_duration_estimates = {}
        for rate in common_rates:
            estimated_duration = num_samples / rate
            # Voice commands typically 1-10 seconds
            if 1.0 <= estimated_duration <= 10.0:
                audio_duration_estimates[rate] = estimated_duration
                logger.debug(f"Sample rate {rate}Hz → {estimated_duration:.2f}s duration")

        # Heuristic 2: Most likely rates based on source
        # Browser MediaRecorder: typically 48kHz or 44.1kHz
        # macOS: 48kHz or 44.1kHz
        # Mobile: 44.1kHz or 48kHz
        # Old hardware: 22.05kHz, 16kHz, 11.025kHz

        if audio_duration_estimates:
            # Choose rate that gives most reasonable duration (2-5 sec preference)
            best_rate = min(audio_duration_estimates.keys(),
                          key=lambda r: abs(audio_duration_estimates[r] - 3.0))
            logger.info(f"🎯 Inferred sample rate: {best_rate}Hz (duration: {audio_duration_estimates[best_rate]:.2f}s)")
            return best_rate

        # Fallback: Use most common browser rate
        logger.warning(f"⚠️ Could not infer sample rate, defaulting to 48000Hz (browser standard)")
        return 48000

    async def normalize_audio(self, audio_bytes: bytes, sample_rate: int = None) -> np.ndarray:
        """
        Universal audio normalization pipeline that:
        1. Auto-detects sample rate from audio bytes OR uses provided rate
        2. Decodes audio format (int16/float32/int8 PCM)
        3. Resamples to 16kHz if needed
        4. Converts stereo to mono
        5. Normalizes to float32 [-1.0, 1.0]

        Args:
            audio_bytes: Raw audio data
            sample_rate: Optional sample rate from frontend (browser-reported)
                        If None, will attempt to infer from audio

        Returns: Normalized float32 numpy array ready for Whisper
        """
        def _normalize_sync():
            logger.info(f"🔊 Audio normalization: {len(audio_bytes)} bytes")

            # Step 1: Try to detect format and decode
            audio_array = None
            detected_sr = None
            detected_format = None

            # Try pydub first (handles WebM/Opus, MP3, MP4, etc.)
            try:
                from pydub import AudioSegment
                import tempfile
                import os

                # Write to temp file for pydub
                with tempfile.NamedTemporaryFile(suffix='.webm', delete=False) as tmp:
                    tmp.write(audio_bytes)
                    tmp_path = tmp.name

                # Load with pydub (uses ffmpeg under the hood)
                audio = AudioSegment.from_file(tmp_path)
                os.unlink(tmp_path)

                # Convert to numpy array
                audio_array = np.array(audio.get_array_of_samples(), dtype=np.float32)

                # Normalize to [-1.0, 1.0]
                if audio.sample_width == 1:  # 8-bit
                    audio_array = audio_array / 128.0
                elif audio.sample_width == 2:  # 16-bit
                    audio_array = audio_array / 32768.0
                elif audio.sample_width == 4:  # 32-bit
                    audio_array = audio_array / 2147483648.0

                # Handle stereo
                if audio.channels == 2:
                    audio_array = audio_array.reshape((-1, 2)).mean(axis=1)

                detected_sr = audio.frame_rate
                detected_format = "pydub (WebM/Opus/MP3/etc)"
                logger.info(f"✅ Decoded with pydub: {detected_sr}Hz, {len(audio_array)} samples, {audio.channels} channels")
            except Exception as e:
                logger.debug(f"pydub decode failed: {e}")

            # Try soundfile second (handles WAV, FLAC, OGG with embedded metadata)
            if audio_array is None:
                try:
                    audio_buf = io.BytesIO(audio_bytes)
                    audio_array, detected_sr = sf.read(audio_buf, dtype='float32')
                    detected_format = "soundfile (with metadata)"
                    logger.info(f"✅ Decoded with soundfile: {detected_sr}Hz, {audio_array.shape}")
                except Exception as e:
                    logger.debug(f"soundfile decode failed: {e}")

            # Step 2: If soundfile fails, try raw PCM formats
            if audio_array is None:
                # Try int16 PCM (most common from browsers)
                try:
                    audio_array = np.frombuffer(audio_bytes, dtype=np.int16)
                    if len(audio_array) > 100:
                        audio_array = audio_array.astype(np.float32) / 32768.0
                        detected_format = "int16 PCM"
                        # Use provided sample rate or infer it
                        if sample_rate:
                            detected_sr = sample_rate
                            logger.info(f"✅ Decoded as int16 PCM: {len(audio_array)} samples, using provided {detected_sr}Hz")
                        else:
                            detected_sr = self._infer_sample_rate(audio_bytes, len(audio_array))
                            logger.info(f"✅ Decoded as int16 PCM: {len(audio_array)} samples, inferred {detected_sr}Hz")
                    else:
                        audio_array = None
                except Exception as e:
                    logger.debug(f"int16 PCM decode failed: {e}")

            # Try float32 PCM
            if audio_array is None:
                try:
                    audio_array = np.frombuffer(audio_bytes, dtype=np.float32)
                    if len(audio_array) > 100:
                        detected_format = "float32 PCM"
                        # Use provided sample rate or infer it
                        if sample_rate:
                            detected_sr = sample_rate
                            logger.info(f"✅ Decoded as float32 PCM: {len(audio_array)} samples, using provided {detected_sr}Hz")
                        else:
                            detected_sr = self._infer_sample_rate(audio_bytes, len(audio_array))
                            logger.info(f"✅ Decoded as float32 PCM: {len(audio_array)} samples, inferred {detected_sr}Hz")
                    else:
                        audio_array = None
                except Exception as e:
                    logger.debug(f"float32 PCM decode failed: {e}")

            # Try int8 PCM
            if audio_array is None:
                try:
                    audio_array = np.frombuffer(audio_bytes, dtype=np.int8)
                    if len(audio_array) > 100:
                        audio_array = audio_array.astype(np.float32) / 128.0
                        detected_format = "int8 PCM"
                        # Use provided sample rate or infer it
                        if sample_rate:
                            detected_sr = sample_rate
                            logger.info(f"✅ Decoded as int8 PCM: {len(audio_array)} samples, using provided {detected_sr}Hz")
                        else:
                            detected_sr = self._infer_sample_rate(audio_bytes, len(audio_array))
                            logger.info(f"✅ Decoded as int8 PCM: {len(audio_array)} samples, inferred {detected_sr}Hz")
                    else:
                        audio_array = None
                except Exception as e:
                    logger.debug(f"int8 PCM decode failed: {e}")

            if audio_array is None:
                logger.error("❌ Could not decode audio in any known format")
                raise ValueError("Audio format not recognized")

            # Step 3: Convert stereo to mono if needed
            if len(audio_array.shape) > 1 and audio_array.shape[1] > 1:
                logger.info(f"Converting stereo ({audio_array.shape[1]} channels) to mono")
                audio_array = np.mean(audio_array, axis=1)

            # Step 4: Validate audio has content
            audio_energy = np.abs(audio_array).mean()
            if audio_energy < 0.001:
                logger.error(f"❌ Audio is silence (energy: {audio_energy:.6f})")
                raise ValueError("Audio contains only silence")

            logger.info(f"✅ Audio energy: {audio_energy:.6f}")

            # Step 5: Resample to 16kHz if needed
            TARGET_SR = 16000
            if detected_sr != TARGET_SR:
                logger.info(f"🔄 Resampling from {detected_sr}Hz to {TARGET_SR}Hz...")

                if LIBROSA_AVAILABLE:
                    # High-quality resampling with librosa
                    audio_array = librosa.resample(
                        audio_array,
                        orig_sr=detected_sr,
                        target_sr=TARGET_SR,
                        res_type='kaiser_best'  # Highest quality
                    )
                    logger.info(f"✅ Resampled with librosa (kaiser_best): {len(audio_array)} samples")
                else:
                    # Fallback: Basic linear interpolation
                    from scipy import signal
                    num_samples = int(len(audio_array) * TARGET_SR / detected_sr)
                    audio_array = signal.resample(audio_array, num_samples)
                    logger.info(f"✅ Resampled with scipy: {len(audio_array)} samples")
            else:
                logger.info(f"✅ Already at {TARGET_SR}Hz - no resampling needed")

            # Step 6: Ensure float32 normalization
            audio_array = audio_array.astype(np.float32)

            # Clip to [-1.0, 1.0] range
            if np.abs(audio_array).max() > 1.0:
                logger.warning(f"Audio exceeded [-1.0, 1.0] range, clipping...")
                audio_array = np.clip(audio_array, -1.0, 1.0)

            logger.info(f"✅ Normalization complete: {len(audio_array)} samples @ 16kHz, float32")
            return audio_array

        # Run in thread pool to avoid blocking event loop
        return await asyncio.to_thread(_normalize_sync)

    def _get_optimal_device(self) -> str:
        """
        Dynamically determine the optimal device for Whisper based on config, hardware,
        and actual PyTorch backend capability probing.

        Whisper uses sparse tensors internally which require SparseMPS backend support.
        This method probes for actual sparse tensor support rather than assuming MPS works.

        Environment variables:
            WHISPER_DEVICE: Force specific device ('cpu', 'cuda', 'mps', or 'auto')
            WHISPER_FORCE_MPS: Set to 'true' to force MPS even if probe fails (risky)
            WHISPER_MPS_SPARSE_FALLBACK: Set to 'cpu' or 'error' for sparse failures

        Returns:
            Device string: 'cuda', 'mps', or 'cpu'
        """
        config = get_whisper_config()
        configured_device = config.model_device

        # Explicit device override
        if configured_device != 'auto':
            logger.info(f"🔧 Using explicitly configured device: {configured_device}")
            return configured_device

        # Auto-detect best device with capability probing
        try:
            import torch

            # Check CUDA first (most reliable for ML workloads)
            if torch.cuda.is_available():
                logger.info("🎮 CUDA GPU detected - using cuda")
                return 'cuda'

            # Check MPS (Apple Silicon) with sparse tensor compatibility probe
            if hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
                # Check if user wants to force MPS despite potential issues
                force_mps = os.getenv('WHISPER_FORCE_MPS', 'false').lower() == 'true'
                if force_mps:
                    logger.warning("🍎 Apple Silicon MPS forced via WHISPER_FORCE_MPS=true (may fail on sparse ops)")
                    return 'mps'

                # Probe for sparse tensor support (Whisper requires this)
                mps_sparse_supported = self._probe_mps_sparse_support(torch)

                if mps_sparse_supported:
                    logger.info("🍎 Apple Silicon MPS with sparse tensor support - using mps")
                    return 'mps'
                else:
                    # MPS available but sparse tensors not supported
                    fallback = os.getenv('WHISPER_MPS_SPARSE_FALLBACK', 'cpu').lower()
                    if fallback == 'error':
                        raise RuntimeError(
                            "MPS available but SparseMPS backend doesn't support required operations. "
                            "Set WHISPER_MPS_SPARSE_FALLBACK=cpu to use CPU fallback, or "
                            "WHISPER_FORCE_MPS=true to attempt MPS anyway (may fail)."
                        )
                    logger.warning(
                        "🍎 Apple Silicon detected but MPS SparseMPS backend lacks required ops "
                        "(aten::_sparse_coo_tensor_with_dims_and_tensors). Falling back to CPU. "
                        "Set WHISPER_FORCE_MPS=true to override (may fail)."
                    )
                    # Fall through to CPU

        except ImportError:
            logger.debug("PyTorch not available for device detection")

        logger.info("💻 Using CPU for Whisper (most compatible)")
        return 'cpu'

    def _probe_mps_sparse_support(self, torch) -> bool:
        """
        Probe whether MPS backend actually supports sparse tensor operations
        required by Whisper (specifically _sparse_coo_tensor_with_dims_and_tensors).

        This is a dynamic runtime check rather than version-based hardcoding,
        ensuring compatibility as PyTorch evolves.

        Args:
            torch: The torch module (passed to avoid re-import)

        Returns:
            True if MPS sparse tensors work, False otherwise
        """
        try:
            # Create a small test sparse tensor on MPS
            # This probes the actual SparseMPS backend capability
            indices = torch.tensor([[0, 1], [0, 1]], device='mps')
            values = torch.tensor([1.0, 2.0], device='mps')
            size = (2, 2)

            # Attempt to create sparse COO tensor - this is what Whisper uses
            sparse_tensor = torch.sparse_coo_tensor(indices, values, size, device='mps')

            # If we get here, sparse tensors work on MPS
            logger.debug("✅ MPS sparse tensor probe successful")
            return True

        except RuntimeError as e:
            error_msg = str(e).lower()
            if 'sparse' in error_msg or 'sparsemps' in error_msg or 'aten::' in error_msg:
                logger.debug(f"❌ MPS sparse tensor probe failed (expected): {e}")
                return False
            # Re-raise unexpected errors
            raise
        except Exception as e:
            # Any other error means MPS sparse is not reliable
            logger.debug(f"❌ MPS sparse tensor probe failed: {type(e).__name__}: {e}")
            return False

    def load_model(self):
        """
        Load Whisper model synchronously (for backward compatibility).

        WARNING: This is a blocking call. In async contexts, use load_model_async() instead.

        Uses the advanced import manager with:
        - Dynamic configuration from environment
        - Circuit breaker for resilience
        - Retry with exponential backoff
        """
        if self.model is not None:
            return self.model

        with self._model_load_lock:
            # Double-check after acquiring lock
            if self.model is not None:
                return self.model

            config = get_whisper_config()
            model_size = config.model_size
            device = self._get_optimal_device()

            logger.info(f"🔧 Loading Whisper model (synchronous)...")
            logger.info(f"   Model size: {model_size} (from WHISPER_MODEL_SIZE env)")
            logger.info(f"   Device: {device}")

            try:
                # Use advanced import manager with circuit breaker and retry
                import_manager = get_import_manager()
                whisper = import_manager.import_sync()

                start_time = time.time()
                self.model = whisper.load_model(model_size, device=device)
                elapsed_ms = (time.time() - start_time) * 1000

                # Update import manager status
                import_manager.status.model_loaded = True
                import_manager.status.load_time_ms = elapsed_ms

                logger.info(f"✅ Whisper model '{model_size}' loaded on {device} in {elapsed_ms:.0f}ms")
            except ImportError as e:
                logger.error(f"❌ Cannot load Whisper model: {e}")
                raise
            except Exception as e:
                logger.error(f"❌ Whisper model loading failed: {e}")
                raise

        return self.model

    async def load_model_async(self, timeout: float = None) -> bool:
        """
        Load Whisper model asynchronously without blocking the event loop.

        Uses the advanced import manager with:
        - Dynamic configuration from environment
        - Async-safe circuit breaker
        - Retry with exponential backoff
        - Non-blocking thread pool execution

        Args:
            timeout: Maximum time to wait for model loading (default: from config)

        Returns:
            True if model loaded successfully, False otherwise
        """
        if self.model is not None:
            return True

        config = get_whisper_config()
        timeout = timeout or config.load_timeout

        # Ensure async lock exists (lazy initialization for when event loop wasn't running at init)
        if self._async_model_load_lock is None:
            self._async_model_load_lock = asyncio.Lock()

        async with self._async_model_load_lock:
            # Double-check after acquiring async lock
            if self.model is not None:
                return True

            # Check if another thread is already loading
            if self._model_loading:
                logger.info("Whisper model already being loaded, waiting...")
                try:
                    await asyncio.wait_for(
                        asyncio.get_event_loop().run_in_executor(
                            None,
                            lambda: self._model_load_event.wait(timeout)
                        ),
                        timeout=timeout
                    )
                    return self.model is not None
                except asyncio.TimeoutError:
                    logger.error(f"Timeout waiting for Whisper model to load ({timeout}s)")
                    return False

            # Set loading flag
            self._model_loading = True
            self._model_load_event.clear()

            model_size = config.model_size
            device = self._get_optimal_device()

            logger.info(f"🔧 Loading Whisper model (async)...")
            logger.info(f"   Model size: {model_size} (from WHISPER_MODEL_SIZE env)")
            logger.info(f"   Device: {device}")
            logger.info(f"   Timeout: {timeout}s")

            try:
                # Get import manager
                import_manager = get_import_manager()

                # Async import with circuit breaker
                whisper = await import_manager.import_async()

                def _load_model_sync():
                    """Synchronous model loading in thread pool."""
                    return whisper.load_model(model_size, device=device)

                # Run model loading in thread pool with timeout
                start_time = time.time()
                self.model = await asyncio.wait_for(
                    asyncio.to_thread(_load_model_sync),
                    timeout=timeout
                )
                elapsed_ms = (time.time() - start_time) * 1000

                # Update import manager status
                import_manager.status.model_loaded = True
                import_manager.status.load_time_ms = elapsed_ms

                logger.info(f"✅ Whisper model '{model_size}' loaded on {device} in {elapsed_ms:.0f}ms (async)")
                return True

            except asyncio.TimeoutError:
                logger.error(f"⏱️ Whisper model loading timed out after {timeout}s")
                return False
            except ImportError as e:
                error_msg = str(e)
                if "circuit breaker" in error_msg.lower():
                    logger.error(f"🔴 Whisper import blocked by circuit breaker: {e}")
                else:
                    logger.error(f"❌ Cannot load Whisper model - import failed: {e}")
                return False
            except Exception as e:
                logger.error(f"❌ Failed to load Whisper model: {e}")
                return False
            finally:
                self._model_loading = False
                self._model_load_event.set()

    def is_model_loaded(self) -> bool:
        """Check if the Whisper model is already loaded."""
        return self.model is not None

    def decode_audio_data(self, audio_data):
        """Convert any input format to bytes"""

        # If already bytes, return as-is
        if isinstance(audio_data, bytes):
            logger.debug("Audio data is already bytes")
            return audio_data

        # If it's a string, try various decodings
        if isinstance(audio_data, str):
            # Try base64 first
            try:
                decoded = base64.b64decode(audio_data)
                logger.debug("Successfully decoded base64 audio")
                return decoded
            except Exception:
                pass

            # Try URL-safe base64
            try:
                decoded = base64.urlsafe_b64decode(audio_data)
                logger.debug("Successfully decoded URL-safe base64 audio")
                return decoded
            except Exception:
                pass

            # Try hex encoding
            try:
                decoded = bytes.fromhex(audio_data)
                logger.debug("Successfully decoded hex audio")
                return decoded
            except Exception:
                pass

            # Try latin-1 encoding as last resort
            try:
                decoded = audio_data.encode('latin-1')
                logger.debug("Encoded string as latin-1")
                return decoded
            except Exception:
                pass

        # If it's a numpy array
        if isinstance(audio_data, np.ndarray):
            if audio_data.dtype == np.float32 or audio_data.dtype == np.float64:
                # Convert float to int16
                audio_data = (audio_data * 32767).astype(np.int16)
            return audio_data.tobytes()

        # Try to convert to bytes
        try:
            return bytes(audio_data)
        except Exception:
            logger.error(f"Cannot convert audio data of type {type(audio_data)} to bytes")
            raise ValueError(f"Unsupported audio data type: {type(audio_data)}")

    async def create_wav_from_normalized_audio(self, audio_array: np.ndarray) -> str:
        """
        Create a temporary WAV file from normalized audio array

        Args:
            audio_array: Normalized float32 array at 16kHz

        Returns:
            Path to temporary WAV file
        """
        def _write_sync():
            with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as tmp:
                sf.write(tmp.name, audio_array, 16000)
                return tmp.name

        return await asyncio.to_thread(_write_sync)

    def _get_vad_pipeline(self):
        """Lazy-load VAD pipeline"""
        if self.vad_pipeline is None and self.vad_enabled:
            try:
                self.vad_pipeline = get_vad_pipeline()
                logger.info("✅ VAD pipeline loaded for Whisper audio handler")
            except Exception as e:
                logger.error(f"Failed to load VAD pipeline: {e}")
                self.vad_enabled = False
        return self.vad_pipeline

    def _get_window_manager(self):
        """Lazy-load window manager"""
        if self.window_manager is None and self.windowing_enabled:
            try:
                self.window_manager = get_window_manager()
                logger.info("✅ Window manager loaded for Whisper audio handler")
            except Exception as e:
                logger.error(f"Failed to load window manager: {e}")
                self.windowing_enabled = False
        return self.window_manager

    async def _apply_vad_and_windowing(
        self,
        audio: np.ndarray,
        mode: str = 'general'
    ) -> np.ndarray:
        """
        Apply VAD filtering and windowing to audio

        Process:
        1. Apply VAD to remove silence/noise (if enabled)
        2. Apply windowing based on mode (if enabled)

        Args:
            audio: Normalized audio (float32, 16kHz)
            mode: 'general', 'unlock', or 'command'

        Returns:
            Filtered and windowed audio ready for Whisper
        """
        original_duration = len(audio) / 16000

        # Step 1: VAD filtering (remove silence/noise)
        if self.vad_enabled:
            vad_pipeline = self._get_vad_pipeline()
            if vad_pipeline:
                try:
                    logger.info(f"🔍 Applying VAD filter ({mode} mode)...")
                    audio = await vad_pipeline.filter_audio_async(audio)

                    if len(audio) == 0:
                        logger.warning("❌ VAD filtered out ALL audio - no speech detected")
                        return audio

                    vad_duration = len(audio) / 16000
                    logger.info(f"✅ VAD filtered: {original_duration:.2f}s → {vad_duration:.2f}s ({vad_duration/original_duration*100:.1f}% retained)")
                except Exception as e:
                    logger.error(f"VAD filtering failed: {e}, proceeding without VAD")

        # Step 2: Windowing (truncation)
        if self.windowing_enabled:
            window_manager = self._get_window_manager()
            if window_manager:
                try:
                    logger.info(f"🪟 Applying windowing ({mode} mode)...")
                    audio = window_manager.prepare_for_transcription(audio, mode=mode)

                    if len(audio) == 0:
                        logger.warning("❌ Windowing resulted in empty audio")
                        return audio

                    final_duration = len(audio) / 16000
                    logger.info(f"✅ Final audio: {final_duration:.2f}s ready for Whisper")
                except Exception as e:
                    logger.error(f"Windowing failed: {e}, proceeding without windowing")

        return audio

    async def transcribe_any_format(
        self,
        audio_data,
        sample_rate: int = None,
        mode: str = 'general',
        bypass_self_voice_check: bool = False
    ):
        """
        Transcribe audio in any format with automatic normalization + VAD + windowing

        NOW INCLUDES:
        - VAD filtering (WebRTC-VAD + Silero VAD) to remove silence/noise
        - Windowing/truncation (5s global, 2s unlock, 3s command)
        - Mode-aware optimization for ultra-low latency
        - Non-blocking async model loading
        - v8.0: Self-voice suppression to prevent JARVIS from hearing itself

        Args:
            audio_data: Audio bytes or base64 string
            sample_rate: Optional sample rate from frontend (browser-reported)
                        If None, will attempt to infer from audio
            mode: Processing mode:
                  - 'general': Standard transcription (5s window)
                  - 'unlock': Ultra-fast unlock flow (2s window)
                  - 'command': Command detection (3s window)
            bypass_self_voice_check: Skip self-voice check for pre-verified audio
        """
        
        # ═══════════════════════════════════════════════════════════════════
        # v8.0: SELF-VOICE SUPPRESSION - WHISPER LEVEL CHECK
        # ═══════════════════════════════════════════════════════════════════
        # This is the DEEPEST level of defense. Even if audio gets past the
        # STT router, we check again here before Whisper processes it.
        # ═══════════════════════════════════════════════════════════════════
        if not bypass_self_voice_check:
            try:
                from core.unified_speech_state import get_speech_state_manager_sync
                speech_manager = get_speech_state_manager_sync()
                rejection = speech_manager.should_reject_audio()
                
                if rejection.reject:
                    logger.warning(
                        f"🔇 [WHISPER-SELF-VOICE] Rejecting audio - "
                        f"reason: {rejection.reason}, details: {rejection.details}"
                    )
                    return None  # Return None to indicate no transcription
            except ImportError:
                pass  # UnifiedSpeechStateManager not available
            except Exception as e:
                logger.debug(f"[WHISPER-SELF-VOICE] Check error (non-fatal): {e}")

        # Load model asynchronously (non-blocking) if not already loaded
        if not self.is_model_loaded():
            model_loaded = await self.load_model_async()
            if not model_loaded:
                logger.error("Failed to load Whisper model for transcription")
                return None

        # SAFETY: Capture model reference BEFORE any async operations or thread spawning
        # to prevent segfaults if model is unloaded during execution
        model = self.model
        if model is None:
            logger.error("Whisper model is None after loading check - race condition?")
            return None

        try:
            # Convert to bytes
            audio_bytes = self.decode_audio_data(audio_data)

            # Normalize audio (use provided sample rate or auto-detect)
            normalized_audio = await self.normalize_audio(audio_bytes, sample_rate=sample_rate)

            # **CRITICAL**: Apply VAD + windowing BEFORE Whisper
            # This prevents hallucinations, reduces latency, and improves accuracy
            normalized_audio = await self._apply_vad_and_windowing(normalized_audio, mode=mode)

            # Check if VAD filtered out all speech
            if len(normalized_audio) == 0:
                logger.warning("⚠️ VAD/windowing resulted in empty audio - no speech to transcribe")
                return None

            # Transcribe with Whisper in thread pool to avoid blocking
            def _transcribe_sync():
                # Double-check model reference is still valid inside thread
                if model is None:
                    raise RuntimeError("Whisper model reference became None during transcription")
                logger.info(f"🎤 Transcribing audio directly (bypass WAV file)")
                logger.info(f"   Audio array shape: {normalized_audio.shape}, dtype: {normalized_audio.dtype}")
                logger.info(f"   Audio min: {normalized_audio.min():.6f}, max: {normalized_audio.max():.6f}")
                logger.info(f"   Audio duration: {len(normalized_audio) / 16000:.2f}s @ 16kHz")

                # CRITICAL FIX: Whisper requires audio to be padded/trimmed to N_SAMPLES
                # Whisper internally pads to 30 seconds (480000 samples at 16kHz)
                # But we need to ensure it's at least long enough to avoid edge cases
                audio_to_transcribe = normalized_audio

                # Ensure minimum length (at least 0.5 seconds)
                min_samples = 8000  # 0.5 seconds at 16kHz
                if len(audio_to_transcribe) < min_samples:
                    logger.warning(f"⚠️ Audio too short ({len(audio_to_transcribe)} samples), padding to {min_samples}")
                    audio_to_transcribe = np.pad(audio_to_transcribe, (0, min_samples - len(audio_to_transcribe)))

                # Pass audio array directly to Whisper
                # Use initial_prompt to guide Whisper toward expected content
                # This PREVENTS hallucinations like "Hey Jarvis, I'm Mark McCree"

                # Mode-specific prompts to bias Whisper toward expected phrases
                mode_prompts = {
                    'unlock': "unlock my screen, unlock screen, unlock, jarvis unlock, open screen",
                    'command': "jarvis, hey jarvis, computer, assistant",
                    'general': ""  # No bias for general transcription
                }
                initial_prompt = mode_prompts.get(mode, "")

                result = model.transcribe(
                    audio_to_transcribe,
                    language="en",
                    fp16=False,
                    word_timestamps=False,  # Faster without word timestamps
                    condition_on_previous_text=False,  # Don't use context from previous
                    temperature=0.0,  # Deterministic (no randomness)
                    initial_prompt=initial_prompt,  # 🔑 KEY FIX: Bias toward expected phrases
                    no_speech_threshold=0.6,  # Higher threshold = more strict speech detection
                    logprob_threshold=-1.0,  # Reject low-confidence outputs
                    compression_ratio_threshold=2.4  # Reject repetitive hallucinations
                )
                logger.info(f"   Raw Whisper result: {result}")
                return result["text"].strip() # Strip leading/trailing whitespace from text

            # Run in thread pool to avoid blocking event loop
            # asyncio.to_thread() is macOS-safe and prevents event loop freeze
            text = await asyncio.to_thread(_transcribe_sync)

            logger.info(f"✅ Transcribed: '{text}'")

            # If Whisper returns empty string, it detected no speech
            # Return None to signal failure so hybrid_stt_router can try other methods
            if not text or text.strip() == "":
                logger.warning("⚠️ Whisper returned empty transcription - no speech detected")
                logger.warning(f"   Audio was {len(audio_bytes)} bytes, normalized to {len(normalized_audio)} samples")
                logger.warning(f"   Sample rate: provided={sample_rate}, final=16000Hz")
                return None

            return text

        except Exception as e:
            logger.error(f"❌ Whisper transcription failed: {e}")
            logger.error("   This indicates audio format issues or invalid audio data")
            # Return None to signal failure - do NOT return hardcoded text
            return None

# Global instance
_whisper_handler = WhisperAudioHandler()

async def transcribe_with_whisper(
    audio_data,
    sample_rate: int = None,
    mode: str = 'general'
):
    """
    Global transcription function with VAD + windowing support

    Args:
        audio_data: Audio bytes or base64 string
        sample_rate: Optional sample rate from frontend (browser-reported)
        mode: Processing mode ('general', 'unlock', or 'command')
              - 'general': 5s window, standard processing
              - 'unlock': 2s window, ultra-fast for unlock flow
              - 'command': 3s window, optimized for command detection
    """
    return await _whisper_handler.transcribe_any_format(
        audio_data,
        sample_rate=sample_rate,
        mode=mode
    )

# Global instance
_whisper_handler = WhisperAudioHandler()

async def transcribe_with_whisper(
    audio_data,
    sample_rate: int = None,
    mode: str = 'general'
):
    """
    Global transcription function with VAD + windowing support

    Args:
        audio_data: Audio bytes or base64 string
        sample_rate: Optional sample rate from frontend (browser-reported)
        mode: Processing mode ('general', 'unlock', or 'command')
              - 'general': 5s window, standard processing
              - 'unlock': 2s window, ultra-fast for unlock flow
              - 'command': 3s window, optimized for command detection
    """
    return await _whisper_handler.transcribe_any_format(
        audio_data,
        sample_rate=sample_rate,
        mode=mode
    )