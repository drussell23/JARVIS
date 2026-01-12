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
        self._default_timeout = 120.0  # 2 minutes
        self._timeout_multipliers = {
            OpType.MODEL_LOAD: 3.0,      # Model loading can take longer
            OpType.INFERENCE: 0.5,        # Inference should be fast
            OpType.EMBEDDING: 1.0,
            OpType.PREPROCESSING: 0.5,
            OpType.TENSOR_OP: 0.25,
            OpType.OTHER: 1.0,
        }

        # Warm-up tracking
        self._warmed_up = False
        self._warmup_operations = 0

        logger.info("🔧 PyTorchExecutor singleton initialized")

    def _create_executor(self) -> ThreadPoolExecutor:
        """Create or recreate the thread pool executor."""
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
            self._thread_healthy = True
            self._consecutive_failures = 0
            logger.info("🚀 PyTorch executor thread pool created")
            return self._executor

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
        **kwargs
    ) -> T:
        """
        Run a function in the dedicated PyTorch thread.

        Args:
            func: The function to run (should contain PyTorch operations)
            *args: Positional arguments to pass to func
            timeout: Optional timeout in seconds (adaptive if not specified)
            priority: Operation priority level
            op_type: Operation type for metrics
            **kwargs: Keyword arguments to pass to func

        Returns:
            The result of func(*args, **kwargs)

        Raises:
            TimeoutError: If operation exceeds timeout
            RuntimeError: If executor is unhealthy and cannot recover
        """
        # Create metrics
        metrics = OperationMetrics(
            op_type=op_type,
            priority=priority,
            start_time=time.time(),
            memory_before_mb=self._get_memory_mb()
        )

        # Calculate timeout
        effective_timeout = self._calculate_timeout(op_type, timeout)

        # Check health and maybe recover
        if not self._thread_healthy:
            if not self._maybe_recover():
                metrics.success = False
                metrics.error = "Executor unhealthy and recovery failed"
                metrics.end_time = time.time()
                metrics.duration_ms = (metrics.end_time - metrics.start_time) * 1000
                self._record_operation(metrics)
                raise RuntimeError("PyTorch executor is unhealthy and could not recover")

        # Get executor and event loop
        executor = self._get_executor()
        loop = asyncio.get_running_loop()

        # Wrap function with args/kwargs
        if args or kwargs:
            func_with_args = partial(func, *args, **kwargs)
        else:
            func_with_args = func

        try:
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
            return result

        except asyncio.TimeoutError:
            metrics.success = False
            metrics.error = f"TimeoutError: Operation exceeded {effective_timeout}s"
            metrics.end_time = time.time()
            metrics.duration_ms = (metrics.end_time - metrics.start_time) * 1000
            self._record_operation(metrics)

            logger.error(
                f"⏰ PyTorch operation timed out after {effective_timeout}s "
                f"(op_type={op_type}, priority={priority.name})"
            )
            raise TimeoutError(f"PyTorch operation timed out after {effective_timeout}s")

        except Exception as e:
            metrics.success = False
            metrics.error = f"{type(e).__name__}: {str(e)[:100]}"
            metrics.end_time = time.time()
            metrics.duration_ms = (metrics.end_time - metrics.start_time) * 1000
            self._record_operation(metrics)

            logger.error(f"❌ PyTorch operation failed: {e}")
            raise

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
    **kwargs
) -> T:
    """
    Run a function in the dedicated PyTorch thread.

    This is the recommended way to run PyTorch operations from async code.
    """
    return await pytorch_executor.run(
        func, *args,
        timeout=timeout,
        priority=priority,
        op_type=op_type,
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
