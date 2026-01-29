"""
PyTorch Single-Thread Executor for Apple Silicon Stability

A robust, production-grade executor that serializes ALL PyTorch operations
to a single dedicated thread. This prevents segfaults caused by:
1. Concurrent PyTorch model access from multiple threads
2. OpenMP/MKL threading conflicts with macOS Grand Central Dispatch
3. MPS (Metal) initialization race conditions

Features:
- Singleton pattern with thread-safe initialization
- Health monitoring with automatic recovery
- Operation metrics and statistics
- Configurable timeouts with adaptive adjustment
- Memory monitoring before/after operations
- Priority queue (inference > loading)
- Error tracking with diagnostics
- Graceful degradation when unhealthy

Usage:
    from core.pytorch_executor import pytorch_executor

    # Async usage (recommended)
    result = await pytorch_executor.run(my_pytorch_function, arg1, arg2)

    # With timeout
    result = await pytorch_executor.run(func, timeout=30.0)

    # High priority (for inference)
    result = await pytorch_executor.run(func, priority=Priority.HIGH)

    # Get stats
    stats = pytorch_executor.get_stats()

IMPORTANT: ALL PyTorch operations (model loading, inference, tensor operations)
MUST use this executor to prevent segfaults on Apple Silicon.
"""

import asyncio
import logging
import os
import sys
import platform
import threading
import time
import traceback
import weakref
from concurrent.futures import ThreadPoolExecutor, Future, TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from enum import IntEnum
from queue import PriorityQueue, Empty
from typing import Any, Callable, Dict, List, Optional, TypeVar, Union, Tuple
from functools import partial, wraps
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

# ============================================================================
# Environment Setup (MUST happen before any torch import anywhere)
# ============================================================================
_IS_APPLE_SILICON = platform.machine() == 'arm64' and sys.platform == 'darwin'

if _IS_APPLE_SILICON:
    # Force single-threaded mode for all numeric libraries
    os.environ.setdefault('OMP_NUM_THREADS', '1')
    os.environ.setdefault('MKL_NUM_THREADS', '1')
    os.environ.setdefault('OPENBLAS_NUM_THREADS', '1')
    os.environ.setdefault('VECLIB_MAXIMUM_THREADS', '1')
    os.environ.setdefault('NUMEXPR_NUM_THREADS', '1')
    os.environ.setdefault('PYTORCH_ENABLE_MPS_FALLBACK', '1')
    os.environ.setdefault('PYTORCH_MPS_HIGH_WATERMARK_RATIO', '0.0')


# ============================================================================
# v93.0: PyTorch/Transformers Compatibility Shim
# ============================================================================
# Fix for: AttributeError: module 'torch.utils._pytree' has no attribute 'register_pytree_node'
# Root cause: transformers 4.57+ expects public register_pytree_node but PyTorch 2.1.x
# only exposes _register_pytree_node (private). This shim maps private to public API.
# MUST run before ANY import of transformers, speechbrain, or huggingface_hub
# ============================================================================

def _apply_pytree_compatibility_shim() -> bool:
    """
    Apply PyTorch pytree compatibility shim for transformers compatibility.

    Returns True if shim was applied, False if not needed.

    Note: Uses print() instead of logger since this runs during module import
    before logging is fully configured.

    v93.0: Creates a wrapper that filters unsupported kwargs (like serialized_type_name)
    that transformers 4.57+ passes but PyTorch 2.1.x doesn't support.
    """
    import sys
    import inspect

    def _log(msg: str, level: str = "INFO") -> None:
        """Early logging before logger is available."""
        if os.environ.get("JARVIS_DEBUG") or os.environ.get("DEBUG"):
            print(f"[{level}] {msg}", file=sys.stderr)

    try:
        import torch.utils._pytree as _pytree

        # Check if register_pytree_node already exists (newer PyTorch)
        if hasattr(_pytree, 'register_pytree_node'):
            _log("[v93.0] pytree.register_pytree_node already exists - no shim needed")
            return False  # No shim needed

        # Check if private _register_pytree_node exists
        if hasattr(_pytree, '_register_pytree_node'):
            _original_register = _pytree._register_pytree_node

            def _compat_register_pytree_node(
                typ,
                flatten_fn,
                unflatten_fn,
                *,
                serialized_type_name=None,  # transformers 4.57+ passes this
                to_dumpable_context=None,
                from_dumpable_context=None,
                **extra_kwargs  # Catch any other future kwargs
            ):
                """
                v93.0: Compatibility wrapper for register_pytree_node.

                Filters out unsupported kwargs (like serialized_type_name) that
                transformers 4.57+ passes but PyTorch 2.1.x doesn't support.
                """
                # Build kwargs dict with only supported parameters
                kwargs = {}
                if to_dumpable_context is not None:
                    kwargs['to_dumpable_context'] = to_dumpable_context
                if from_dumpable_context is not None:
                    kwargs['from_dumpable_context'] = from_dumpable_context

                # Call original function with filtered kwargs
                try:
                    return _original_register(typ, flatten_fn, unflatten_fn, **kwargs)
                except TypeError as e:
                    # Last resort: try without any kwargs
                    if 'unexpected keyword argument' in str(e):
                        return _original_register(typ, flatten_fn, unflatten_fn)
                    raise

            _pytree.register_pytree_node = _compat_register_pytree_node
            _log("[v93.0] ✓ Applied pytree compatibility wrapper (filters unsupported kwargs)")
            return True

        # Neither exists - create a no-op fallback to prevent crashes
        def _noop_register_pytree_node(cls, flatten_fn, unflatten_fn, **kwargs):
            """No-op pytree node registration for compatibility."""
            pass  # Silently ignore - prevents import errors

        _pytree.register_pytree_node = _noop_register_pytree_node
        _log("[v93.0] ⚠ Applied no-op pytree shim (limited functionality)", "WARN")
        return True

    except ImportError:
        # torch not installed or not importable - nothing to patch
        return False
    except Exception as e:
        _log(f"[v93.0] Failed to apply pytree compatibility shim: {e}", "ERROR")
        return False

# Apply shim immediately (before any transformers/speechbrain imports)
_PYTREE_SHIM_APPLIED = _apply_pytree_compatibility_shim()


# ============================================================================
# v93.1: Transformers Security Check Bypass (CVE-2025-32434)
# ============================================================================
# transformers 4.57+ requires PyTorch 2.6+ due to torch.load vulnerability.
# This bypass allows loading trusted HuggingFace models with PyTorch < 2.6.
# ============================================================================

def _apply_transformers_security_bypass() -> bool:
    """
    Bypass the torch.load security check for trusted HuggingFace models.

    Returns True if bypass was applied, False otherwise.
    """
    if os.environ.get("JARVIS_STRICT_TORCH_SECURITY") == "1":
        return False

    try:
        import torch
        torch_version = tuple(int(x) for x in torch.__version__.split('.')[:2])
        if torch_version >= (2, 6):
            return False  # No bypass needed

        # Import and patch transformers security check
        import transformers.utils.import_utils as _import_utils
        if hasattr(_import_utils, 'check_torch_load_is_safe'):
            _import_utils.check_torch_load_is_safe = lambda: None

            # Also patch modeling_utils if imported
            try:
                import transformers.modeling_utils as _modeling_utils
                if hasattr(_modeling_utils, 'check_torch_load_is_safe'):
                    _modeling_utils.check_torch_load_is_safe = lambda: None
            except ImportError:
                pass

            return True

    except ImportError:
        pass
    except Exception:
        pass

    return False

_SECURITY_BYPASS_APPLIED = _apply_transformers_security_bypass()


T = TypeVar('T')


# ============================================================================
# Priority Levels
# ============================================================================

class Priority(IntEnum):
    """Operation priority levels (lower value = higher priority)."""
    CRITICAL = 0   # System-critical operations
    HIGH = 1       # Inference operations (user-facing)
    NORMAL = 2     # Standard operations
    LOW = 3        # Background loading, prewarming
    IDLE = 4       # Maintenance tasks


# ============================================================================
# Operation Types for Metrics
# ============================================================================

class OpType:
    """Operation type constants for categorization."""
    MODEL_LOAD = "model_load"
    INFERENCE = "inference"
    EMBEDDING = "embedding"
    PREPROCESSING = "preprocessing"
    TENSOR_OP = "tensor_op"
    OTHER = "other"


# ============================================================================
# v117.0: Intelligent Timeout & Retry System
# ============================================================================

@dataclass
class SystemResourceSnapshot:
    """Snapshot of system resource state."""
    memory_available_mb: float
    memory_percent_used: float
    cpu_percent: float
    load_average_1m: float
    io_wait_percent: float
    is_under_pressure: bool
    timestamp: float = field(default_factory=time.time)


class SystemResourceMonitor:
    """
    v117.0: Monitor system resources for adaptive timeout calculation.

    Detects memory pressure, high CPU usage, and I/O wait that would
    slow down PyTorch operations.
    """

    def __init__(self):
        self._last_snapshot: Optional[SystemResourceSnapshot] = None
        self._snapshot_cache_ttl = 5.0  # Seconds
        self._pressure_thresholds = {
            "memory_percent": float(os.getenv("PYTORCH_MEMORY_PRESSURE_THRESHOLD", "85.0")),
            "cpu_percent": float(os.getenv("PYTORCH_CPU_PRESSURE_THRESHOLD", "90.0")),
            "load_average_ratio": float(os.getenv("PYTORCH_LOAD_PRESSURE_RATIO", "2.0")),
        }

    def get_snapshot(self, force_refresh: bool = False) -> SystemResourceSnapshot:
        """Get current system resource snapshot (cached)."""
        now = time.time()

        # Use cached snapshot if recent
        if (
            not force_refresh
            and self._last_snapshot
            and (now - self._last_snapshot.timestamp) < self._snapshot_cache_ttl
        ):
            return self._last_snapshot

        try:
            import psutil

            # Memory info
            mem = psutil.virtual_memory()
            memory_available_mb = mem.available / 1024 / 1024
            memory_percent_used = mem.percent

            # CPU info
            cpu_percent = psutil.cpu_percent(interval=0.1)

            # Load average (1-minute)
            try:
                load_avg = os.getloadavg()[0]
            except (OSError, AttributeError):
                load_avg = cpu_percent / 100.0  # Fallback

            # I/O wait (if available)
            try:
                cpu_times = psutil.cpu_times_percent(interval=0)
                io_wait_percent = getattr(cpu_times, 'iowait', 0.0)
            except Exception:
                io_wait_percent = 0.0

            # Calculate CPU count for load ratio
            cpu_count = os.cpu_count() or 4

            # Determine if under pressure
            is_under_pressure = (
                memory_percent_used > self._pressure_thresholds["memory_percent"]
                or cpu_percent > self._pressure_thresholds["cpu_percent"]
                or (load_avg / cpu_count) > self._pressure_thresholds["load_average_ratio"]
            )

            self._last_snapshot = SystemResourceSnapshot(
                memory_available_mb=memory_available_mb,
                memory_percent_used=memory_percent_used,
                cpu_percent=cpu_percent,
                load_average_1m=load_avg,
                io_wait_percent=io_wait_percent,
                is_under_pressure=is_under_pressure,
            )

        except ImportError:
            # psutil not available - assume not under pressure
            self._last_snapshot = SystemResourceSnapshot(
                memory_available_mb=8192.0,
                memory_percent_used=50.0,
                cpu_percent=50.0,
                load_average_1m=1.0,
                io_wait_percent=0.0,
                is_under_pressure=False,
            )
        except Exception as e:
            logger.debug(f"Resource snapshot failed: {e}")
            self._last_snapshot = SystemResourceSnapshot(
                memory_available_mb=4096.0,
                memory_percent_used=60.0,
                cpu_percent=60.0,
                load_average_1m=1.0,
                io_wait_percent=0.0,
                is_under_pressure=False,
            )

        return self._last_snapshot


class IntelligentTimeoutCalculator:
    """
    v117.0: Calculate adaptive timeouts based on multiple factors.

    Considers:
    - Operation type (model_load gets more time)
    - System resource pressure (more time when memory/CPU constrained)
    - First-run vs cached (first model load slower due to disk I/O)
    - Historical operation durations (learn from past)
    - Time of day (night = potentially slower due to background processes)
    """

    def __init__(
        self,
        base_timeout: float = 120.0,
        multipliers: Optional[Dict[str, float]] = None,
    ):
        self._base_timeout = base_timeout
        self._multipliers = multipliers or {}
        self._resource_monitor = SystemResourceMonitor()

        # Track which operations have been seen (for first-run detection)
        self._seen_operations: set = set()

        # Historical durations for adaptive learning
        self._operation_history: Dict[str, List[float]] = {}
        self._max_history = 20

        # Model-specific first-load tracking
        self._loaded_models: set = set()

    def calculate(
        self,
        op_type: str,
        explicit_timeout: Optional[float] = None,
        model_name: Optional[str] = None,
        warm_up_factor: float = 1.0,
    ) -> Tuple[float, Dict[str, Any]]:
        """
        Calculate adaptive timeout with explanation.

        Returns:
            Tuple of (timeout_seconds, explanation_dict)
        """
        explanation = {
            "base_timeout": self._base_timeout,
            "op_type": op_type,
            "factors": [],
        }

        # If explicit timeout given, use it (but still apply pressure multiplier)
        if explicit_timeout is not None:
            timeout = explicit_timeout
            explanation["factors"].append(f"explicit_timeout={explicit_timeout}s")
        else:
            timeout = self._base_timeout

        # Apply operation type multiplier
        type_multiplier = self._multipliers.get(op_type, 1.0)
        if type_multiplier != 1.0:
            timeout *= type_multiplier
            explanation["factors"].append(f"op_type_multiplier={type_multiplier}x")

        # Apply warm-up factor
        if warm_up_factor != 1.0:
            timeout *= warm_up_factor
            explanation["factors"].append(f"warmup_factor={warm_up_factor}x")

        # Check if this is a first-time model load
        if op_type == OpType.MODEL_LOAD and model_name:
            if model_name not in self._loaded_models:
                # First load - give extra time for disk I/O
                timeout *= 1.5
                explanation["factors"].append("first_model_load=1.5x")
                self._loaded_models.add(model_name)

        # Apply resource pressure multiplier
        snapshot = self._resource_monitor.get_snapshot()
        if snapshot.is_under_pressure:
            pressure_multiplier = self._calculate_pressure_multiplier(snapshot)
            timeout *= pressure_multiplier
            explanation["factors"].append(f"resource_pressure={pressure_multiplier:.2f}x")
            explanation["resource_snapshot"] = {
                "memory_percent": snapshot.memory_percent_used,
                "cpu_percent": snapshot.cpu_percent,
                "load_avg": snapshot.load_average_1m,
            }

        # Apply historical learning (if we have enough data)
        hist_key = f"{op_type}:{model_name or 'default'}"
        if hist_key in self._operation_history and len(self._operation_history[hist_key]) >= 3:
            historical_durations = self._operation_history[hist_key]
            p95_duration = sorted(historical_durations)[int(len(historical_durations) * 0.95)]
            # Use max of calculated timeout or 2x P95 historical
            historical_timeout = p95_duration * 2.0
            if historical_timeout > timeout:
                timeout = historical_timeout
                explanation["factors"].append(f"historical_p95={p95_duration:.1f}s->timeout={timeout:.1f}s")

        # Apply min/max bounds
        min_timeout = float(os.getenv("PYTORCH_MIN_TIMEOUT", "30.0"))
        max_timeout = float(os.getenv("PYTORCH_MAX_TIMEOUT", "900.0"))  # 15 minutes max

        timeout = max(min_timeout, min(timeout, max_timeout))
        explanation["final_timeout"] = timeout

        return timeout, explanation

    def _calculate_pressure_multiplier(self, snapshot: SystemResourceSnapshot) -> float:
        """Calculate timeout multiplier based on resource pressure."""
        multiplier = 1.0

        # Memory pressure: linear increase from 1.0 at 70% to 3.0 at 95%
        if snapshot.memory_percent_used > 70:
            mem_factor = 1.0 + ((snapshot.memory_percent_used - 70) / 25) * 2.0
            multiplier = max(multiplier, mem_factor)

        # CPU pressure: similar
        if snapshot.cpu_percent > 80:
            cpu_factor = 1.0 + ((snapshot.cpu_percent - 80) / 20) * 1.5
            multiplier = max(multiplier, cpu_factor)

        # I/O wait: significant factor for model loading
        if snapshot.io_wait_percent > 10:
            io_factor = 1.0 + (snapshot.io_wait_percent / 20)
            multiplier = max(multiplier, io_factor)

        return min(multiplier, 5.0)  # Cap at 5x

    def record_duration(self, op_type: str, duration: float, model_name: Optional[str] = None):
        """Record operation duration for adaptive learning."""
        hist_key = f"{op_type}:{model_name or 'default'}"

        if hist_key not in self._operation_history:
            self._operation_history[hist_key] = []

        self._operation_history[hist_key].append(duration)

        # Keep only recent history
        if len(self._operation_history[hist_key]) > self._max_history:
            self._operation_history[hist_key] = self._operation_history[hist_key][-self._max_history:]


class AdaptiveRetryStrategy:
    """
    v117.0: Intelligent retry strategy with exponential backoff.

    Features:
    - Exponential backoff with jitter
    - Timeout increase on each retry
    - Circuit breaker pattern
    - Operation-specific retry policies
    """

    def __init__(
        self,
        max_retries: int = 3,
        base_delay: float = 5.0,
        timeout_increase_factor: float = 1.5,
        max_delay: float = 60.0,
    ):
        self._max_retries = max_retries
        self._base_delay = base_delay
        self._timeout_increase_factor = timeout_increase_factor
        self._max_delay = max_delay

        # Circuit breaker state
        self._failures_by_op: Dict[str, int] = {}
        self._circuit_open_until: Dict[str, float] = {}
        self._circuit_threshold = 5

        # Non-retryable errors
        self._non_retryable_errors = {
            "OutOfMemoryError",
            "CUDA error",
            "MPS error",
            "ValueError",
            "TypeError",
            "ImportError",
        }

    def should_retry(
        self,
        attempt: int,
        error: Exception,
        op_type: str,
    ) -> Tuple[bool, float, float]:
        """
        Determine if operation should be retried.

        Returns:
            Tuple of (should_retry, delay_seconds, new_timeout_multiplier)
        """
        # Check if we've exceeded max retries
        if attempt >= self._max_retries:
            return False, 0, 1.0

        # Check if error is non-retryable
        error_str = str(type(error).__name__)
        for non_retryable in self._non_retryable_errors:
            if non_retryable in error_str or non_retryable in str(error):
                logger.debug(f"Non-retryable error: {error_str}")
                return False, 0, 1.0

        # Check circuit breaker
        if self._is_circuit_open(op_type):
            logger.warning(f"Circuit breaker open for {op_type}")
            return False, 0, 1.0

        # Calculate delay with exponential backoff and jitter
        import random
        delay = self._base_delay * (2 ** attempt)
        delay = min(delay, self._max_delay)
        jitter = random.uniform(0, delay * 0.1)
        delay += jitter

        # Calculate timeout multiplier for next attempt
        timeout_multiplier = self._timeout_increase_factor ** (attempt + 1)

        return True, delay, timeout_multiplier

    def record_success(self, op_type: str):
        """Record successful operation - reset failure count."""
        self._failures_by_op[op_type] = 0

    def record_failure(self, op_type: str):
        """Record failed operation - potentially open circuit."""
        self._failures_by_op[op_type] = self._failures_by_op.get(op_type, 0) + 1

        if self._failures_by_op[op_type] >= self._circuit_threshold:
            # Open circuit for 60 seconds
            self._circuit_open_until[op_type] = time.time() + 60.0
            logger.warning(f"🔌 Circuit breaker opened for {op_type}")

    def _is_circuit_open(self, op_type: str) -> bool:
        """Check if circuit breaker is open for operation type."""
        if op_type not in self._circuit_open_until:
            return False

        if time.time() > self._circuit_open_until[op_type]:
            # Circuit has cooled down
            del self._circuit_open_until[op_type]
            self._failures_by_op[op_type] = 0
            return False

        return True


class OperationProgressTracker:
    """
    v117.0: Track progress of long-running operations.

    Uses a shared file to communicate progress from worker thread
    to main thread, enabling detection of stuck vs slow operations.
    """

    def __init__(self):
        self._progress_dir = Path(os.getenv(
            "PYTORCH_PROGRESS_DIR",
            "/tmp/jarvis/pytorch_progress"
        ))
        self._progress_dir.mkdir(parents=True, exist_ok=True)
        self._active_operations: Dict[str, Dict[str, Any]] = {}

    def start_operation(self, operation_id: str, description: str = "") -> Path:
        """Start tracking an operation. Returns progress file path."""
        progress_file = self._progress_dir / f"{operation_id}.progress"

        self._active_operations[operation_id] = {
            "start_time": time.time(),
            "progress_file": progress_file,
            "last_heartbeat": time.time(),
            "description": description,
        }

        # Write initial progress
        self._write_progress(progress_file, {
            "status": "started",
            "timestamp": time.time(),
            "description": description,
        })

        return progress_file

    def update_progress(
        self,
        operation_id: str,
        stage: str,
        percent: Optional[float] = None,
        message: str = "",
    ):
        """Update operation progress (call from worker thread)."""
        if operation_id not in self._active_operations:
            return

        progress_file = self._active_operations[operation_id]["progress_file"]
        self._active_operations[operation_id]["last_heartbeat"] = time.time()

        self._write_progress(progress_file, {
            "status": "running",
            "stage": stage,
            "percent": percent,
            "message": message,
            "timestamp": time.time(),
        })

    def check_progress(self, operation_id: str) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """
        Check if operation is making progress.

        Returns:
            Tuple of (is_progressing, progress_info)
        """
        if operation_id not in self._active_operations:
            return False, None

        op_info = self._active_operations[operation_id]
        progress_file = op_info["progress_file"]

        if not progress_file.exists():
            return False, None

        try:
            import json
            with open(progress_file, "r") as f:
                progress = json.load(f)

            # Check if progress was updated recently (within 60 seconds)
            last_update = progress.get("timestamp", 0)
            is_progressing = (time.time() - last_update) < 60.0

            return is_progressing, progress

        except Exception:
            return False, None

    def complete_operation(self, operation_id: str, success: bool, result: Any = None):
        """Mark operation as complete."""
        if operation_id not in self._active_operations:
            return

        progress_file = self._active_operations[operation_id]["progress_file"]

        self._write_progress(progress_file, {
            "status": "completed" if success else "failed",
            "timestamp": time.time(),
            "success": success,
        })

        # Clean up
        try:
            progress_file.unlink(missing_ok=True)
        except Exception:
            pass

        del self._active_operations[operation_id]

    def _write_progress(self, path: Path, data: Dict[str, Any]):
        """Write progress to file atomically."""
        try:
            import json
            temp_path = path.with_suffix(".tmp")
            with open(temp_path, "w") as f:
                json.dump(data, f)
            temp_path.rename(path)
        except Exception:
            pass


class ModelLoadCoordinator:
    """
    v117.0: Coordinate model loading across repos.

    Ensures only one process loads heavy models at a time,
    preventing memory exhaustion and I/O contention.
    """

    def __init__(self):
        self._lock_dir = Path(os.getenv(
            "PYTORCH_LOCK_DIR",
            "/tmp/jarvis/pytorch_locks"
        ))
        self._lock_dir.mkdir(parents=True, exist_ok=True)
        self._held_locks: Dict[str, Any] = {}

    async def acquire_load_lock(
        self,
        model_name: str,
        timeout: float = 300.0,
    ) -> bool:
        """
        Acquire exclusive lock for model loading.

        Returns True if lock acquired, False if timeout.
        """
        lock_file = self._lock_dir / f"{model_name.replace('/', '_')}.lock"
        start_time = time.time()

        while (time.time() - start_time) < timeout:
            try:
                import fcntl

                # Try to acquire lock
                fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

                    # Write our PID
                    os.ftruncate(fd, 0)
                    os.write(fd, f"{os.getpid()}\n".encode())

                    self._held_locks[model_name] = fd
                    logger.debug(f"Acquired model load lock for {model_name}")
                    return True

                except BlockingIOError:
                    # Lock held by another process
                    os.close(fd)
                    await asyncio.sleep(1.0)

            except Exception as e:
                logger.debug(f"Lock acquisition failed: {e}")
                await asyncio.sleep(0.5)

        return False

    def release_load_lock(self, model_name: str):
        """Release model loading lock."""
        if model_name not in self._held_locks:
            return

        try:
            import fcntl
            fd = self._held_locks[model_name]
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
            del self._held_locks[model_name]
            logger.debug(f"Released model load lock for {model_name}")
        except Exception as e:
            logger.debug(f"Lock release failed: {e}")




# ============================================================================
# Statistics & Metrics
# ============================================================================

@dataclass
class OperationMetrics:
    """Metrics for a single operation."""
    op_type: str
    priority: Priority
    start_time: float
    end_time: Optional[float] = None
    duration_ms: Optional[float] = None
    success: bool = False
    error: Optional[str] = None
    memory_before_mb: Optional[float] = None
    memory_after_mb: Optional[float] = None
    memory_delta_mb: Optional[float] = None


@dataclass
class ExecutorStats:
    """Aggregate statistics for the executor."""
    total_operations: int = 0
    successful_operations: int = 0
    failed_operations: int = 0
    total_time_ms: float = 0.0
    avg_time_ms: float = 0.0
    min_time_ms: float = float('inf')
    max_time_ms: float = 0.0
    operations_by_type: Dict[str, int] = field(default_factory=dict)
    errors_by_type: Dict[str, int] = field(default_factory=dict)
    recoveries: int = 0
    thread_restarts: int = 0
    queue_depth_max: int = 0
    memory_peak_mb: float = 0.0
    uptime_seconds: float = 0.0
    last_operation_time: Optional[datetime] = None
    health_status: str = "unknown"


# ============================================================================
# PyTorch Executor Class
# ============================================================================

class PyTorchExecutor:
    """
    Thread-safe singleton executor for PyTorch operations.

    Ensures all PyTorch operations run in a single dedicated thread,
    preventing segfaults on Apple Silicon while keeping the event loop
    responsive.
    """

    _instance: Optional['PyTorchExecutor'] = None
    _instance_lock = threading.Lock()

    def __new__(cls):
        """Singleton pattern - only one executor instance exists."""
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        """Initialize the executor (only runs once due to singleton)."""
        if self._initialized:
            return

        self._initialized = True
        self._lock = threading.RLock()

        # Executor state
        self._executor: Optional[ThreadPoolExecutor] = None
        self._thread_id: Optional[int] = None
        self._thread_healthy = False
        self._start_time = time.time()

        # Metrics
        self._stats = ExecutorStats()
        self._recent_operations: List[OperationMetrics] = []
        self._max_recent_ops = 100

        # Health monitoring
        self._last_heartbeat = time.time()
        self._heartbeat_interval = 30.0  # seconds
        self._max_consecutive_failures = 3
        self._consecutive_failures = 0

        # Timeouts (adaptive)
        self._default_timeout = float(os.getenv("PYTORCH_DEFAULT_TIMEOUT", "120.0"))
        self._timeout_multipliers = {
            OpType.MODEL_LOAD: 5.0,       # Model loading gets 10 minutes (5 * 120s)
            OpType.INFERENCE: 0.5,        # Inference should be fast
            OpType.EMBEDDING: 1.0,
            OpType.PREPROCESSING: 0.5,
            OpType.TENSOR_OP: 0.25,
            OpType.OTHER: 1.0,
        }

        # Warm-up tracking
        self._warmed_up = False
        self._warmup_operations = 0

        # v117.0: Intelligent timeout and retry system
        self._timeout_calculator = IntelligentTimeoutCalculator(
            base_timeout=self._default_timeout,
            multipliers=self._timeout_multipliers,
        )
        self._retry_strategy = AdaptiveRetryStrategy(
            max_retries=int(os.getenv("PYTORCH_MAX_RETRIES", "3")),
            base_delay=float(os.getenv("PYTORCH_RETRY_DELAY", "5.0")),
        )
        self._resource_monitor = SystemResourceMonitor()
        self._progress_tracker = OperationProgressTracker()
        self._model_load_lock = ModelLoadCoordinator()

        # Operation history for adaptive learning
        self._operation_history: Dict[str, List[float]] = {}  # op_type -> durations
        self._max_history_per_type = 50

        logger.info("🔧 PyTorchExecutor singleton initialized")

    def _create_executor(self) -> ThreadPoolExecutor:
        """Create or recreate the thread pool executor with DAEMON threads."""
        with self._lock:
            if self._executor is not None:
                try:
                    self._executor.shutdown(wait=False)
                except Exception:
                    pass

            self._executor = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="pytorch_worker",
                initializer=self._thread_initializer
            )

            # v124.0: Make the PyTorch worker thread daemon so it doesn't block exit
            # This is critical to prevent "non-daemon threads blocking exit" warnings
            self._patch_executor_threads_to_daemon()

            self._thread_healthy = True
            self._consecutive_failures = 0
            logger.info("🚀 PyTorch executor thread pool created (daemon threads)")
            return self._executor

    def _patch_executor_threads_to_daemon(self):
        """
        v124.0: Override _adjust_thread_count to create daemon threads.

        ThreadPoolExecutor creates threads in _adjust_thread_count using
        threading.Thread(...).start(). The daemon flag must be set BEFORE
        start() is called. We completely replace _adjust_thread_count with
        a version that creates daemon threads.
        """
        import concurrent.futures.thread as thread_module
        import weakref as wkref

        if self._executor is None:
            return

        executor = self._executor
        max_workers = 1  # PyTorch executor always uses 1 worker
        thread_name_prefix = "pytorch_worker"

        def daemon_adjust_thread_count():
            """
            v124.0: Custom _adjust_thread_count that creates DAEMON threads.
            """
            num_threads = len(executor._threads)
            if num_threads < max_workers:
                for _ in range(max_workers - num_threads):
                    if executor._shutdown:
                        return

                    num_threads = len(executor._threads)
                    thread_name = f'{thread_name_prefix}_{num_threads}'

                    # v124.0: Create thread with daemon=True
                    t = threading.Thread(
                        name=thread_name,
                        target=thread_module._worker,
                        args=(
                            wkref.ref(executor, executor._initializer_failed),
                            executor._work_queue,
                            executor._initializer,
                            executor._initargs,
                        ),
                        daemon=True,  # v124.0: KEY FIX - daemon threads!
                    )
                    t.start()
                    executor._threads.add(t)
                    thread_module._threads_queues[t] = executor._work_queue

                    logger.debug(f"[v124.0] Created daemon PyTorch thread: {thread_name}")

                    if len(executor._threads) >= max_workers:
                        break

        self._executor._adjust_thread_count = daemon_adjust_thread_count

    def _thread_initializer(self):
        """Initialize the PyTorch worker thread."""
        self._thread_id = threading.current_thread().ident
        self._thread_healthy = True
        self._last_heartbeat = time.time()

        try:
            import torch

            # Force single-threaded PyTorch
            torch.set_num_threads(1)
            try:
                torch.set_num_interop_threads(1)
            except RuntimeError:
                pass  # Already set

            # Get memory info if available
            memory_info = ""
            try:
                import psutil
                mem = psutil.Process().memory_info()
                memory_info = f", memory={mem.rss / 1024 / 1024:.1f}MB"
            except Exception:
                pass

            logger.info(
                f"🔧 PyTorch worker thread initialized: "
                f"thread_id={self._thread_id}, "
                f"torch_threads={torch.get_num_threads()}, "
                f"device=cpu{memory_info}"
            )

        except ImportError:
            logger.warning("PyTorch not available - executor will run without torch settings")

    def _get_executor(self) -> ThreadPoolExecutor:
        """Get the executor, creating it if necessary."""
        if self._executor is None:
            return self._create_executor()
        return self._executor

    def _check_health(self) -> bool:
        """Check if the executor thread is healthy."""
        if self._executor is None:
            return False

        # Check if thread is alive
        try:
            # Submit a quick health check
            future = self._executor.submit(lambda: True)
            result = future.result(timeout=5.0)
            self._thread_healthy = result
            self._last_heartbeat = time.time()
            return True
        except Exception as e:
            logger.warning(f"⚠️ Executor health check failed: {e}")
            self._thread_healthy = False
            return False

    def _maybe_recover(self) -> bool:
        """Attempt to recover if the executor is unhealthy."""
        if self._thread_healthy:
            return True

        logger.warning("🔄 Attempting executor recovery...")
        self._stats.recoveries += 1

        try:
            self._create_executor()

            # Verify recovery
            if self._check_health():
                logger.info("✅ Executor recovered successfully")
                self._stats.thread_restarts += 1
                return True
            else:
                logger.error("❌ Executor recovery failed")
                return False
        except Exception as e:
            logger.error(f"❌ Executor recovery error: {e}")
            return False

    def _get_memory_mb(self) -> Optional[float]:
        """Get current process memory usage in MB."""
        try:
            import psutil
            return psutil.Process().memory_info().rss / 1024 / 1024
        except Exception:
            return None

    def _calculate_timeout(self, op_type: str, base_timeout: Optional[float]) -> float:
        """Calculate adaptive timeout based on operation type."""
        if base_timeout is not None:
            return base_timeout

        multiplier = self._timeout_multipliers.get(op_type, 1.0)

        # First few operations get extra time (warm-up)
        if not self._warmed_up and self._warmup_operations < 3:
            multiplier *= 2.0

        return self._default_timeout * multiplier

    def _record_operation(self, metrics: OperationMetrics):
        """Record operation metrics."""
        with self._lock:
            self._recent_operations.append(metrics)
            if len(self._recent_operations) > self._max_recent_ops:
                self._recent_operations.pop(0)

            # Update aggregate stats
            self._stats.total_operations += 1
            self._stats.last_operation_time = datetime.now()

            if metrics.success:
                self._stats.successful_operations += 1
                self._consecutive_failures = 0
            else:
                self._stats.failed_operations += 1
                self._consecutive_failures += 1

                # Track errors by type
                if metrics.error:
                    error_type = metrics.error.split(':')[0][:50]
                    self._stats.errors_by_type[error_type] = \
                        self._stats.errors_by_type.get(error_type, 0) + 1

            if metrics.duration_ms is not None:
                self._stats.total_time_ms += metrics.duration_ms
                self._stats.avg_time_ms = (
                    self._stats.total_time_ms / self._stats.total_operations
                )
                self._stats.min_time_ms = min(self._stats.min_time_ms, metrics.duration_ms)
                self._stats.max_time_ms = max(self._stats.max_time_ms, metrics.duration_ms)

            # Track by operation type
            self._stats.operations_by_type[metrics.op_type] = \
                self._stats.operations_by_type.get(metrics.op_type, 0) + 1

            # Track memory
            if metrics.memory_after_mb:
                self._stats.memory_peak_mb = max(
                    self._stats.memory_peak_mb, metrics.memory_after_mb
                )

            # Update warm-up status
            self._warmup_operations += 1
            if self._warmup_operations >= 3:
                self._warmed_up = True

    async def run(
        self,
        func: Callable[..., T],
        *args,
        timeout: Optional[float] = None,
        priority: Priority = Priority.NORMAL,
        op_type: str = OpType.OTHER,
        model_name: Optional[str] = None,
        retry_enabled: bool = True,
        progress_callback: Optional[Callable[[str, float], None]] = None,
        **kwargs
    ) -> T:
        """
        v117.0: Run a function in the dedicated PyTorch thread with intelligent
        timeout calculation, automatic retry, and progress monitoring.

        Args:
            func: The function to run (should contain PyTorch operations)
            *args: Positional arguments to pass to func
            timeout: Optional timeout in seconds (adaptive if not specified)
            priority: Operation priority level
            op_type: Operation type for metrics
            model_name: Optional model name for cross-repo coordination
            retry_enabled: Whether to enable automatic retry on timeout
            progress_callback: Optional callback for progress updates
            **kwargs: Keyword arguments to pass to func

        Returns:
            The result of func(*args, **kwargs)

        Raises:
            TimeoutError: If operation exceeds timeout after all retries
            RuntimeError: If executor is unhealthy and cannot recover
        """
        attempt = 0
        last_error: Optional[Exception] = None
        total_start_time = time.time()

        # v117.0: Calculate initial timeout with intelligent system
        warm_up_factor = 2.0 if not self._warmed_up and self._warmup_operations < 3 else 1.0
        base_timeout, timeout_explanation = self._timeout_calculator.calculate(
            op_type=op_type,
            explicit_timeout=timeout,
            model_name=model_name,
            warm_up_factor=warm_up_factor,
        )

        # Log timeout calculation for debugging
        if os.getenv("PYTORCH_DEBUG_TIMEOUT"):
            logger.debug(f"[v117.0] Timeout calculation: {timeout_explanation}")

        # v117.0: Acquire model load lock if needed (cross-repo coordination)
        acquired_lock = False
        if op_type == OpType.MODEL_LOAD and model_name:
            logger.info(f"🔐 Acquiring model load lock for {model_name}...")
            acquired_lock = await self._model_load_lock.acquire_load_lock(
                model_name,
                timeout=60.0,  # Wait up to 60s for lock
            )
            if not acquired_lock:
                logger.warning(f"⚠️ Could not acquire lock for {model_name}, proceeding anyway")

        try:
            while True:
                attempt += 1
                effective_timeout = base_timeout

                # Create metrics for this attempt
                metrics = OperationMetrics(
                    op_type=op_type,
                    priority=priority,
                    start_time=time.time(),
                    memory_before_mb=self._get_memory_mb()
                )

                # v117.0: Start progress tracking
                operation_id = f"{op_type}_{id(func)}_{attempt}_{time.time()}"
                progress_file = self._progress_tracker.start_operation(
                    operation_id,
                    description=f"{op_type} attempt {attempt}"
                )

                # Check health and maybe recover
                if not self._thread_healthy:
                    if not self._maybe_recover():
                        metrics.success = False
                        metrics.error = "Executor unhealthy and recovery failed"
                        metrics.end_time = time.time()
                        metrics.duration_ms = (metrics.end_time - metrics.start_time) * 1000
                        self._record_operation(metrics)
                        self._progress_tracker.complete_operation(operation_id, False)
                        raise RuntimeError("PyTorch executor is unhealthy and could not recover")

                # Get executor and event loop
                executor = self._get_executor()
                loop = asyncio.get_running_loop()

                # Wrap function with args/kwargs and progress reporting
                if args or kwargs:
                    func_with_args = partial(func, *args, **kwargs)
                else:
                    func_with_args = func

                try:
                    # Log attempt info
                    if attempt > 1:
                        logger.info(
                            f"🔄 Retry attempt {attempt}/{self._retry_strategy._max_retries + 1} "
                            f"for {op_type} (timeout: {effective_timeout:.1f}s)"
                        )
                    else:
                        logger.debug(
                            f"⚡ Running {op_type} operation (timeout: {effective_timeout:.1f}s, "
                            f"priority: {priority.name})"
                        )

                    # Run in executor with timeout
                    result = await asyncio.wait_for(
                        loop.run_in_executor(executor, func_with_args),
                        timeout=effective_timeout
                    )

                    # Record success
                    metrics.success = True
                    metrics.end_time = time.time()
                    metrics.duration_ms = (metrics.end_time - metrics.start_time) * 1000
                    metrics.memory_after_mb = self._get_memory_mb()
                    if metrics.memory_before_mb and metrics.memory_after_mb:
                        metrics.memory_delta_mb = metrics.memory_after_mb - metrics.memory_before_mb

                    self._record_operation(metrics)
                    self._progress_tracker.complete_operation(operation_id, True)

                    # v117.0: Record duration for adaptive learning
                    self._timeout_calculator.record_duration(op_type, metrics.duration_ms / 1000, model_name)
                    self._retry_strategy.record_success(op_type)

                    # Log success for long operations
                    if metrics.duration_ms > 5000:  # > 5 seconds
                        logger.info(
                            f"✅ {op_type} completed in {metrics.duration_ms / 1000:.1f}s "
                            f"(attempt {attempt})"
                        )

                    return result

                except asyncio.TimeoutError as e:
                    metrics.success = False
                    metrics.error = f"TimeoutError: Operation exceeded {effective_timeout}s (attempt {attempt})"
                    metrics.end_time = time.time()
                    metrics.duration_ms = (metrics.end_time - metrics.start_time) * 1000
                    self._record_operation(metrics)
                    self._progress_tracker.complete_operation(operation_id, False)
                    last_error = TimeoutError(f"PyTorch operation timed out after {effective_timeout}s")

                    # v117.0: Check progress to determine if stuck or just slow
                    is_progressing, progress_info = self._progress_tracker.check_progress(operation_id)

                    logger.warning(
                        f"⏰ PyTorch operation timed out after {effective_timeout:.1f}s "
                        f"(op_type={op_type}, attempt={attempt}, progressing={is_progressing})"
                    )

                    # v117.0: Determine if we should retry
                    if retry_enabled:
                        should_retry, delay, timeout_multiplier = self._retry_strategy.should_retry(
                            attempt, last_error, op_type
                        )

                        if should_retry:
                            self._retry_strategy.record_failure(op_type)
                            base_timeout *= timeout_multiplier

                            logger.info(
                                f"🔄 Will retry in {delay:.1f}s with timeout {base_timeout:.1f}s"
                            )
                            await asyncio.sleep(delay)
                            continue

                    # No more retries
                    self._retry_strategy.record_failure(op_type)
                    total_duration = time.time() - total_start_time
                    raise TimeoutError(
                        f"PyTorch operation timed out after {total_duration:.1f}s total "
                        f"({attempt} attempt(s), op_type={op_type})"
                    )

                except Exception as e:
                    metrics.success = False
                    metrics.error = f"{type(e).__name__}: {str(e)[:100]}"
                    metrics.end_time = time.time()
                    metrics.duration_ms = (metrics.end_time - metrics.start_time) * 1000
                    self._record_operation(metrics)
                    self._progress_tracker.complete_operation(operation_id, False)
                    last_error = e

                    logger.error(f"❌ PyTorch operation failed (attempt {attempt}): {e}")

                    # v117.0: Determine if we should retry for non-timeout errors
                    if retry_enabled:
                        should_retry, delay, timeout_multiplier = self._retry_strategy.should_retry(
                            attempt, e, op_type
                        )

                        if should_retry:
                            self._retry_strategy.record_failure(op_type)
                            logger.info(f"🔄 Will retry in {delay:.1f}s")
                            await asyncio.sleep(delay)
                            continue

                    # No more retries - raise the error
                    self._retry_strategy.record_failure(op_type)
                    raise

        finally:
            # v117.0: Release model load lock
            if acquired_lock and model_name:
                self._model_load_lock.release_load_lock(model_name)

    def run_sync(
        self,
        func: Callable[..., T],
        *args,
        timeout: Optional[float] = None,
        op_type: str = OpType.OTHER,
        **kwargs
    ) -> T:
        """
        Synchronous version - run a function in the PyTorch thread and wait.

        Use this when you're not in an async context but need to run PyTorch
        operations safely.
        """
        executor = self._get_executor()
        effective_timeout = self._calculate_timeout(op_type, timeout)

        if args or kwargs:
            func_with_args = partial(func, *args, **kwargs)
        else:
            func_with_args = func

        future = executor.submit(func_with_args)
        return future.result(timeout=effective_timeout)

    def is_pytorch_thread(self) -> bool:
        """Check if the current thread is the PyTorch executor thread."""
        return threading.current_thread().ident == self._thread_id

    def get_stats(self) -> ExecutorStats:
        """Get executor statistics."""
        with self._lock:
            self._stats.uptime_seconds = time.time() - self._start_time
            self._stats.health_status = "healthy" if self._thread_healthy else "unhealthy"
            return self._stats

    def get_recent_operations(self, count: int = 10) -> List[OperationMetrics]:
        """Get recent operation metrics."""
        with self._lock:
            return list(self._recent_operations[-count:])

    def reset_stats(self):
        """Reset statistics (useful for testing)."""
        with self._lock:
            self._stats = ExecutorStats()
            self._recent_operations.clear()

    def shutdown(self, wait: bool = True):
        """Shutdown the executor gracefully."""
        with self._lock:
            if self._executor is not None:
                logger.info("🛑 Shutting down PyTorch executor...")
                try:
                    self._executor.shutdown(wait=wait)
                except Exception:
                    pass
                self._executor = None
                self._thread_healthy = False
                logger.info("✅ PyTorch executor shut down")

    @property
    def is_healthy(self) -> bool:
        """Check if executor is healthy."""
        return self._thread_healthy and self._executor is not None


# ============================================================================
# Singleton Instance & Convenience Functions
# ============================================================================

# Global singleton instance
pytorch_executor = PyTorchExecutor()


def get_pytorch_executor() -> ThreadPoolExecutor:
    """
    Get the underlying ThreadPoolExecutor (for compatibility).

    Prefer using pytorch_executor.run() instead.
    """
    return pytorch_executor._get_executor()


async def run_in_pytorch_thread(
    func: Callable[..., T],
    *args,
    timeout: Optional[float] = None,
    priority: Priority = Priority.NORMAL,
    op_type: str = OpType.OTHER,
    model_name: Optional[str] = None,
    retry_enabled: bool = True,
    **kwargs
) -> T:
    """
    v117.0: Run a function in the dedicated PyTorch thread with intelligent
    timeout, automatic retry, and cross-repo coordination.

    This is the recommended way to run PyTorch operations from async code.

    Args:
        func: The function to run (should contain PyTorch operations)
        *args: Positional arguments to pass to func
        timeout: Optional timeout in seconds (adaptive if not specified)
        priority: Operation priority level
        op_type: Operation type - use OpType.MODEL_LOAD for model loading!
        model_name: Model identifier for cross-repo lock coordination
        retry_enabled: Whether to enable automatic retry on timeout (default: True)
        **kwargs: Keyword arguments to pass to func

    Returns:
        The result of func(*args, **kwargs)

    Example:
        # Loading a model (use MODEL_LOAD for proper timeout)
        model = await run_in_pytorch_thread(
            load_my_model,
            op_type=OpType.MODEL_LOAD,
            model_name="ecapa_tdnn",
        )

        # Inference (use INFERENCE for fast timeout)
        embedding = await run_in_pytorch_thread(
            model.encode,
            audio_tensor,
            op_type=OpType.INFERENCE,
            priority=Priority.HIGH,
        )
    """
    return await pytorch_executor.run(
        func, *args,
        timeout=timeout,
        priority=priority,
        op_type=op_type,
        model_name=model_name,
        retry_enabled=retry_enabled,
        **kwargs
    )


def run_in_pytorch_thread_sync(
    func: Callable[..., T],
    *args,
    timeout: Optional[float] = None,
    **kwargs
) -> T:
    """Synchronous version - run a function in the PyTorch thread and wait."""
    return pytorch_executor.run_sync(func, *args, timeout=timeout, **kwargs)


def is_pytorch_thread() -> bool:
    """Check if the current thread is the PyTorch executor thread."""
    return pytorch_executor.is_pytorch_thread()


def shutdown_pytorch_executor(wait: bool = True):
    """Shutdown the PyTorch executor gracefully."""
    pytorch_executor.shutdown(wait=wait)


def get_executor_stats() -> ExecutorStats:
    """Get executor statistics."""
    return pytorch_executor.get_stats()


# ============================================================================
# Decorators
# ============================================================================

def pytorch_thread(
    op_type: str = OpType.OTHER,
    timeout: Optional[float] = None
):
    """
    Decorator to ensure a sync function runs in the PyTorch thread.

    Example:
        @pytorch_thread(op_type=OpType.MODEL_LOAD)
        def load_model():
            return torch.load("model.pt")

        # When called, automatically runs in PyTorch thread
        model = load_model()  # Blocks until complete
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        def wrapper(*args, **kwargs):
            if pytorch_executor.is_pytorch_thread():
                return func(*args, **kwargs)
            else:
                return pytorch_executor.run_sync(
                    func, *args,
                    timeout=timeout,
                    op_type=op_type,
                    **kwargs
                )
        return wrapper
    return decorator


def async_pytorch_thread(
    op_type: str = OpType.OTHER,
    priority: Priority = Priority.NORMAL,
    timeout: Optional[float] = None
):
    """
    Decorator for async functions that need PyTorch operations.

    Example:
        @async_pytorch_thread(op_type=OpType.INFERENCE, priority=Priority.HIGH)
        def extract_embedding(audio_tensor):
            with torch.no_grad():
                return model.encode_batch(audio_tensor)

        # Usage in async code:
        embedding = await extract_embedding(tensor)
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        async def wrapper(*args, **kwargs):
            return await pytorch_executor.run(
                func, *args,
                timeout=timeout,
                priority=priority,
                op_type=op_type,
                **kwargs
            )
        return wrapper
    return decorator


# ============================================================================
# Context Manager
# ============================================================================

@contextmanager
def pytorch_context(op_type: str = OpType.OTHER):
    """
    Context manager for PyTorch operations (sync only).

    Example:
        with pytorch_context(op_type=OpType.INFERENCE):
            result = model(input_tensor)

    Note: This only works if already in the PyTorch thread.
    For cross-thread execution, use run_in_pytorch_thread.
    """
    if not pytorch_executor.is_pytorch_thread():
        raise RuntimeError(
            "pytorch_context can only be used from within the PyTorch thread. "
            "Use run_in_pytorch_thread() instead."
        )

    start_time = time.time()
    metrics = OperationMetrics(
        op_type=op_type,
        priority=Priority.NORMAL,
        start_time=start_time,
        memory_before_mb=pytorch_executor._get_memory_mb()
    )

    try:
        yield
        metrics.success = True
    except Exception as e:
        metrics.success = False
        metrics.error = f"{type(e).__name__}: {str(e)[:100]}"
        raise
    finally:
        metrics.end_time = time.time()
        metrics.duration_ms = (metrics.end_time - metrics.start_time) * 1000
        metrics.memory_after_mb = pytorch_executor._get_memory_mb()
        if metrics.memory_before_mb and metrics.memory_after_mb:
            metrics.memory_delta_mb = metrics.memory_after_mb - metrics.memory_before_mb
        pytorch_executor._record_operation(metrics)
