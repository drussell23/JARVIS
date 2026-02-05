"""
Advanced Thread Manager - Bulletproof thread lifecycle management
===================================================================

Enterprise-grade thread management system that prevents leaks, ensures
clean shutdown, and provides comprehensive monitoring.

Features:
- Centralized executor registry with coordinated shutdown
- Automatic thread discovery and tracking
- Async/await support with event loop management
- Multi-phase shutdown with escalation
- Thread pool integration
- Deadlock detection and prevention
- Resource leak detection
- Health monitoring and alerting
- Auto-cleanup of orphaned threads
- Stack trace capture for debugging
- Metric collection and reporting
- Configurable policies via environment variables (no hardcoding)
- Thread priority management
- Graceful degradation under load
- Signal handling integration (SIGINT, SIGTERM)
"""

import asyncio
import threading
import logging
import time
import traceback
import signal
import sys
import os
import ctypes
import weakref
import functools
from typing import Dict, List, Optional, Callable, Set, Any, Union, Tuple, TypeVar, Generic
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from enum import Enum, auto
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, Future, wait as futures_wait, FIRST_COMPLETED
from contextlib import contextmanager
import atexit

logger = logging.getLogger(__name__)

# Type variable for generic executor wrapper
T = TypeVar('T')


# =============================================================================
# CONFIGURATION - All values from environment, no hardcoding
# =============================================================================

def _env_float(key: str, default: float) -> float:
    """Get float from environment with default."""
    try:
        return float(os.getenv(key, str(default)))
    except (ValueError, TypeError):
        return default


def _env_int(key: str, default: int) -> int:
    """Get int from environment with default."""
    try:
        return int(os.getenv(key, str(default)))
    except (ValueError, TypeError):
        return default


def _env_bool(key: str, default: bool) -> bool:
    """Get bool from environment with default."""
    val = os.getenv(key, str(default)).lower()
    return val in ('true', '1', 'yes', 'on')


@dataclass
class ExecutorConfig:
    """
    Configuration for executor management - all values from environment.

    Environment Variables:
        EXECUTOR_SHUTDOWN_TIMEOUT: Max seconds to wait for graceful shutdown (default: 5.0)
        EXECUTOR_FORCE_TIMEOUT: Max seconds before force shutdown (default: 2.0)
        EXECUTOR_HEALTH_CHECK_INTERVAL: Seconds between health checks (default: 30.0)
        EXECUTOR_MAX_PENDING_TASKS: Max pending tasks before backpressure (default: 1000)
        EXECUTOR_ENABLE_METRICS: Enable detailed metrics collection (default: True)
        EXECUTOR_LOG_LEVEL: Logging level for executor events (default: INFO)
        EXECUTOR_AUTO_SCALE: Enable auto-scaling of worker threads (default: False)
        EXECUTOR_MIN_WORKERS: Minimum worker threads when auto-scaling (default: 2)
        EXECUTOR_MAX_WORKERS: Maximum worker threads when auto-scaling (default: CPU_COUNT * 2)
    """
    shutdown_timeout: float = field(default_factory=lambda: _env_float('EXECUTOR_SHUTDOWN_TIMEOUT', 5.0))
    force_timeout: float = field(default_factory=lambda: _env_float('EXECUTOR_FORCE_TIMEOUT', 2.0))
    health_check_interval: float = field(default_factory=lambda: _env_float('EXECUTOR_HEALTH_CHECK_INTERVAL', 30.0))
    max_pending_tasks: int = field(default_factory=lambda: _env_int('EXECUTOR_MAX_PENDING_TASKS', 1000))
    enable_metrics: bool = field(default_factory=lambda: _env_bool('EXECUTOR_ENABLE_METRICS', True))
    auto_scale: bool = field(default_factory=lambda: _env_bool('EXECUTOR_AUTO_SCALE', False))
    min_workers: int = field(default_factory=lambda: _env_int('EXECUTOR_MIN_WORKERS', 2))
    max_workers: int = field(default_factory=lambda: _env_int('EXECUTOR_MAX_WORKERS', (os.cpu_count() or 4) * 2))

    @classmethod
    def from_env(cls) -> 'ExecutorConfig':
        """Create config from environment variables."""
        return cls()


# =============================================================================
# EXECUTOR REGISTRY - Centralized lifecycle management
# =============================================================================

class ExecutorState(Enum):
    """Executor lifecycle states."""
    CREATED = auto()
    RUNNING = auto()
    DRAINING = auto()  # Not accepting new tasks, finishing existing
    SHUTTING_DOWN = auto()
    TERMINATED = auto()
    FAILED = auto()


@dataclass
class ExecutorMetrics:
    """Metrics for a single executor."""
    name: str
    created_at: datetime = field(default_factory=datetime.now)
    tasks_submitted: int = 0
    tasks_completed: int = 0
    tasks_failed: int = 0
    tasks_cancelled: int = 0
    total_execution_time: float = 0.0
    peak_pending_tasks: int = 0
    current_pending_tasks: int = 0
    shutdown_duration: Optional[float] = None

    @property
    def avg_execution_time(self) -> float:
        """Average task execution time."""
        completed = self.tasks_completed
        if completed == 0:
            return 0.0
        return self.total_execution_time / completed

    @property
    def success_rate(self) -> float:
        """Task success rate as percentage."""
        total = self.tasks_completed + self.tasks_failed
        if total == 0:
            return 100.0
        return (self.tasks_completed / total) * 100.0

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            'name': self.name,
            'created_at': self.created_at.isoformat(),
            'tasks_submitted': self.tasks_submitted,
            'tasks_completed': self.tasks_completed,
            'tasks_failed': self.tasks_failed,
            'tasks_cancelled': self.tasks_cancelled,
            'avg_execution_time': self.avg_execution_time,
            'success_rate': self.success_rate,
            'peak_pending_tasks': self.peak_pending_tasks,
            'current_pending_tasks': self.current_pending_tasks,
            'shutdown_duration': self.shutdown_duration,
        }


@dataclass
class ExecutorInfo:
    """Comprehensive information about a registered executor."""
    executor_id: int
    executor_ref: weakref.ref
    name: str
    max_workers: int
    state: ExecutorState = ExecutorState.CREATED
    created_at: datetime = field(default_factory=datetime.now)
    creator_stack: Optional[str] = None
    category: str = "general"
    priority: int = 0  # Higher = shutdown later
    metrics: ExecutorMetrics = field(default_factory=lambda: ExecutorMetrics(name="unknown"))
    shutdown_event: Optional[threading.Event] = None
    pending_futures: Set[Future] = field(default_factory=set)

    def __post_init__(self):
        self.metrics = ExecutorMetrics(name=self.name)
        self.shutdown_event = threading.Event()


class ExecutorRegistry:
    """
    Centralized registry for all thread pool executors.

    Provides:
    - Automatic registration and tracking
    - Coordinated multi-phase shutdown
    - Health monitoring
    - Metrics collection
    - Priority-based shutdown ordering

    Thread-safe and async-compatible.
    """

    _instance: Optional['ExecutorRegistry'] = None
    _instance_lock = threading.Lock()

    def __new__(cls) -> 'ExecutorRegistry':
        """Singleton pattern for global registry."""
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        """Initialize registry (only once due to singleton)."""
        if self._initialized:
            return

        self._initialized = True
        self._lock = threading.RLock()
        self._executors: Dict[int, ExecutorInfo] = {}
        self._config = ExecutorConfig.from_env()
        self._shutdown_initiated = False
        self._shutdown_complete = threading.Event()
        self._global_shutdown_event = threading.Event()

        # Metrics aggregation
        self._total_executors_created = 0
        self._total_executors_shutdown = 0
        self._failed_shutdowns = 0

        # Health monitoring
        self._health_check_thread: Optional[threading.Thread] = None
        self._start_health_monitoring()

        # Register signal handlers
        self._register_signal_handlers()

        # Register atexit handler
        atexit.register(self._atexit_handler)

        logger.info("🔧 ExecutorRegistry initialized")
        logger.debug(f"   Config: shutdown_timeout={self._config.shutdown_timeout}s, "
                    f"force_timeout={self._config.force_timeout}s")

    def _register_signal_handlers(self):
        """Register signal handlers for graceful shutdown."""
        def signal_handler(signum, frame):
            sig_name = signal.Signals(signum).name
            logger.info(f"📡 Received {sig_name}, initiating graceful shutdown...")
            self._global_shutdown_event.set()
            # Don't call shutdown directly - let the main thread handle it

        # Only register if we're in the main thread
        if threading.current_thread() is threading.main_thread():
            try:
                signal.signal(signal.SIGTERM, signal_handler)
                # SIGINT is typically handled by the application
            except (ValueError, OSError) as e:
                logger.debug(f"Could not register signal handlers: {e}")

    def _start_health_monitoring(self):
        """Start background health monitoring thread."""
        if self._config.health_check_interval <= 0:
            return

        def health_loop():
            while not self._global_shutdown_event.wait(timeout=self._config.health_check_interval):
                try:
                    self._perform_health_check()
                except Exception as e:
                    logger.error(f"Health check error: {e}")

        self._health_check_thread = threading.Thread(
            target=health_loop,
            name="ExecutorRegistry-HealthCheck",
            daemon=True
        )
        self._health_check_thread.start()

    def _perform_health_check(self):
        """Perform health check on all registered executors."""
        with self._lock:
            dead_executors = []

            for executor_id, info in self._executors.items():
                executor = info.executor_ref()

                if executor is None:
                    # Executor was garbage collected
                    dead_executors.append(executor_id)
                    logger.warning(f"🗑️ Executor '{info.name}' was garbage collected without shutdown")
                    continue

                # Check for stuck tasks
                if info.metrics.current_pending_tasks > self._config.max_pending_tasks:
                    logger.warning(
                        f"⚠️ Executor '{info.name}' has {info.metrics.current_pending_tasks} "
                        f"pending tasks (max: {self._config.max_pending_tasks})"
                    )

            # Clean up dead executors
            for executor_id in dead_executors:
                info = self._executors.pop(executor_id, None)
                if info:
                    info.state = ExecutorState.TERMINATED

    def register(
        self,
        executor: 'ManagedThreadPoolExecutor',
        name: str,
        max_workers: int,
        category: str = "general",
        priority: int = 0,
        capture_stack: bool = True
    ) -> int:
        """
        Register an executor for lifecycle management.

        Args:
            executor: The executor to register
            name: Human-readable name for identification
            max_workers: Number of worker threads
            category: Category for grouping (e.g., "io", "compute", "network")
            priority: Shutdown priority (higher = shutdown later)
            capture_stack: Whether to capture creation stack trace

        Returns:
            Executor ID for later reference
        """
        executor_id = id(executor)

        # Capture creation stack if requested
        creator_stack = None
        if capture_stack:
            try:
                creator_stack = ''.join(traceback.format_stack()[:-2])
            except Exception:
                pass

        info = ExecutorInfo(
            executor_id=executor_id,
            executor_ref=weakref.ref(executor),
            name=name,
            max_workers=max_workers,
            state=ExecutorState.RUNNING,
            creator_stack=creator_stack,
            category=category,
            priority=priority,
        )

        with self._lock:
            self._executors[executor_id] = info
            self._total_executors_created += 1

        logger.debug(f"📝 Registered executor: {name} (id={executor_id}, workers={max_workers})")
        return executor_id

    def unregister(self, executor_id: int) -> Optional[ExecutorInfo]:
        """
        Unregister an executor.

        Args:
            executor_id: The executor ID to unregister

        Returns:
            ExecutorInfo if found, None otherwise
        """
        with self._lock:
            info = self._executors.pop(executor_id, None)
            if info:
                logger.debug(f"📤 Unregistered executor: {info.name}")
            return info

    def get_executor_info(self, executor_id: int) -> Optional[ExecutorInfo]:
        """Get information about a specific executor."""
        with self._lock:
            return self._executors.get(executor_id)

    def get_all_executors(self) -> List[ExecutorInfo]:
        """Get list of all registered executors."""
        with self._lock:
            return list(self._executors.values())

    def get_executors_by_category(self, category: str) -> List[ExecutorInfo]:
        """Get executors filtered by category."""
        with self._lock:
            return [info for info in self._executors.values() if info.category == category]

    def update_metrics(
        self,
        executor_id: int,
        tasks_submitted: int = 0,
        tasks_completed: int = 0,
        tasks_failed: int = 0,
        tasks_cancelled: int = 0,
        execution_time: float = 0.0,
        pending_delta: int = 0
    ):
        """Update metrics for an executor."""
        with self._lock:
            info = self._executors.get(executor_id)
            if info:
                info.metrics.tasks_submitted += tasks_submitted
                info.metrics.tasks_completed += tasks_completed
                info.metrics.tasks_failed += tasks_failed
                info.metrics.tasks_cancelled += tasks_cancelled
                info.metrics.total_execution_time += execution_time
                info.metrics.current_pending_tasks += pending_delta
                info.metrics.peak_pending_tasks = max(
                    info.metrics.peak_pending_tasks,
                    info.metrics.current_pending_tasks
                )

    def is_shutdown_initiated(self) -> bool:
        """Check if shutdown has been initiated."""
        return self._shutdown_initiated

    def shutdown_all(
        self,
        wait: bool = True,
        timeout: Optional[float] = None,
        cancel_pending: bool = True
    ) -> Dict[str, Any]:
        """
        Shutdown all registered executors with coordinated multi-phase approach.

        Args:
            wait: Whether to wait for pending tasks
            timeout: Total timeout (uses config default if None)
            cancel_pending: Whether to cancel pending futures

        Returns:
            Shutdown statistics
        """
        if self._shutdown_initiated:
            logger.warning("Shutdown already initiated")
            return {"already_initiated": True}

        with self._lock:
            self._shutdown_initiated = True
            self._global_shutdown_event.set()

        timeout = timeout or self._config.shutdown_timeout
        start_time = time.time()

        stats = {
            "total_executors": 0,
            "successful": 0,
            "failed": 0,
            "timed_out": 0,
            "by_category": defaultdict(int),
            "duration": 0.0,
            "details": []
        }

        # Get executors sorted by priority (lower priority shuts down first)
        with self._lock:
            executors = sorted(
                self._executors.values(),
                key=lambda x: x.priority
            )
            stats["total_executors"] = len(executors)

        logger.info(f"🛑 Shutting down {len(executors)} executors...")

        # Phase 1: Signal all executors to stop accepting new tasks
        logger.debug("   Phase 1: Signaling executors to drain...")
        for info in executors:
            info.state = ExecutorState.DRAINING
            if info.shutdown_event:
                info.shutdown_event.set()

        # Phase 2: Graceful shutdown with timeout
        logger.debug(f"   Phase 2: Graceful shutdown (timeout={timeout}s)...")
        phase_deadline = time.time() + timeout

        for info in executors:
            executor = info.executor_ref()
            if executor is None:
                stats["successful"] += 1
                continue

            remaining_time = max(0.1, phase_deadline - time.time())
            shutdown_start = time.time()

            try:
                info.state = ExecutorState.SHUTTING_DOWN

                # Cancel pending futures if requested
                if cancel_pending and hasattr(executor, '_pending_futures'):
                    for future in list(getattr(executor, '_pending_futures', [])):
                        if not future.done():
                            future.cancel()
                            info.metrics.tasks_cancelled += 1

                # Shutdown the executor
                executor.shutdown(wait=wait, cancel_futures=cancel_pending)

                info.state = ExecutorState.TERMINATED
                info.metrics.shutdown_duration = time.time() - shutdown_start
                stats["successful"] += 1
                stats["by_category"][info.category] += 1

                logger.debug(f"   ✅ {info.name} shutdown in {info.metrics.shutdown_duration:.2f}s")

            except Exception as e:
                info.state = ExecutorState.FAILED
                stats["failed"] += 1
                self._failed_shutdowns += 1
                logger.error(f"   ❌ Failed to shutdown {info.name}: {e}")
                stats["details"].append({
                    "name": info.name,
                    "error": str(e),
                    "state": "failed"
                })

        # Phase 3: Force shutdown any remaining
        force_timeout = self._config.force_timeout
        logger.debug(f"   Phase 3: Force shutdown check (timeout={force_timeout}s)...")

        with self._lock:
            remaining = [
                info for info in self._executors.values()
                if info.state not in (ExecutorState.TERMINATED, ExecutorState.FAILED)
            ]

        if remaining:
            logger.warning(f"   ⚠️ {len(remaining)} executors still running, forcing shutdown...")
            force_deadline = time.time() + force_timeout

            for info in remaining:
                executor = info.executor_ref()
                if executor is None:
                    continue

                try:
                    # Force shutdown without waiting
                    executor.shutdown(wait=False, cancel_futures=True)
                    info.state = ExecutorState.TERMINATED
                    stats["timed_out"] += 1
                except Exception as e:
                    logger.error(f"   Force shutdown failed for {info.name}: {e}")
                    stats["failed"] += 1

        # Cleanup
        with self._lock:
            self._total_executors_shutdown += stats["successful"]
            self._executors.clear()

        stats["duration"] = time.time() - start_time
        self._shutdown_complete.set()

        logger.info(f"✅ Shutdown complete: {stats['successful']}/{stats['total_executors']} "
                   f"successful in {stats['duration']:.2f}s")

        return stats

    async def shutdown_all_async(
        self,
        timeout: Optional[float] = None,
        cancel_pending: bool = True
    ) -> Dict[str, Any]:
        """
        Async version of shutdown_all.

        Runs the shutdown in a thread pool to avoid blocking the event loop.
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            lambda: self.shutdown_all(wait=True, timeout=timeout, cancel_pending=cancel_pending)
        )

    def _atexit_handler(self):
        """Handler called at process exit."""
        if not self._shutdown_initiated:
            logger.debug("🔧 atexit: Shutting down executors...")
            try:
                self.shutdown_all(wait=False, timeout=2.0, cancel_pending=True)
            except Exception as e:
                logger.error(f"atexit shutdown error: {e}")

    def get_metrics(self) -> Dict[str, Any]:
        """Get aggregated metrics for all executors."""
        with self._lock:
            executor_metrics = [info.metrics.to_dict() for info in self._executors.values()]

            return {
                "total_created": self._total_executors_created,
                "total_shutdown": self._total_executors_shutdown,
                "failed_shutdowns": self._failed_shutdowns,
                "currently_active": len(self._executors),
                "shutdown_initiated": self._shutdown_initiated,
                "executors": executor_metrics
            }

    def get_report(self) -> str:
        """Generate a human-readable report."""
        metrics = self.get_metrics()

        lines = [
            "",
            "=" * 70,
            "🔧 EXECUTOR REGISTRY REPORT",
            "=" * 70,
            f"Total Created:    {metrics['total_created']}",
            f"Total Shutdown:   {metrics['total_shutdown']}",
            f"Failed Shutdowns: {metrics['failed_shutdowns']}",
            f"Currently Active: {metrics['currently_active']}",
            f"Shutdown Status:  {'Initiated' if metrics['shutdown_initiated'] else 'Running'}",
            "",
        ]

        if metrics['executors']:
            lines.append("Active Executors:")
            for em in metrics['executors']:
                lines.append(f"  - {em['name']}:")
                lines.append(f"      Submitted: {em['tasks_submitted']}, "
                           f"Completed: {em['tasks_completed']}, "
                           f"Failed: {em['tasks_failed']}")
                lines.append(f"      Avg Time: {em['avg_execution_time']:.3f}s, "
                           f"Success Rate: {em['success_rate']:.1f}%")

        lines.append("=" * 70)
        lines.append("")

        return "\n".join(lines)


# Global registry instance
def get_executor_registry() -> ExecutorRegistry:
    """Get the global executor registry instance."""
    return ExecutorRegistry()


# =============================================================================
# MANAGED THREAD POOL EXECUTOR
# =============================================================================

class ManagedThreadPoolExecutor(ThreadPoolExecutor):
    """
    ThreadPoolExecutor with centralized lifecycle management and DAEMON worker threads.

    Features:
    - Automatic registration with ExecutorRegistry
    - Task-level metrics collection
    - Graceful shutdown support
    - Backpressure handling
    - Async-friendly submit methods
    - v124.0: DAEMON WORKER THREADS - Workers won't block process exit

    Usage:
        executor = ManagedThreadPoolExecutor(max_workers=4, name='my-pool')
        future = executor.submit(some_function, arg1, arg2)

        # Or async:
        result = await executor.submit_async(some_function, arg1, arg2)
    """

    def __init__(
        self,
        max_workers: Optional[int] = None,
        thread_name_prefix: str = '',
        initializer: Optional[Callable[..., None]] = None,
        initargs: Tuple = (),
        name: Optional[str] = None,
        category: str = "general",
        priority: int = 0,
        name_prefix: Optional[str] = None,  # Backwards compatibility alias
        daemon: bool = True  # v124.0: Default to daemon threads for clean exit
    ):
        """
        Initialize a managed executor with daemon worker threads.

        Args:
            max_workers: Maximum number of worker threads (default: CPU count * 2)
            thread_name_prefix: Prefix for worker thread names
            initializer: Callable to run in each worker thread at start
            initargs: Arguments for initializer
            name: Human-readable name (default: thread_name_prefix or 'ManagedPool')
            category: Category for grouping executors
            priority: Shutdown priority (higher = shutdown later)
            name_prefix: DEPRECATED - Use 'name' instead (kept for backwards compatibility)
            daemon: v124.0 - Whether worker threads should be daemon threads (default: True)
                    Daemon threads automatically exit when the main thread exits,
                    preventing "non-daemon threads blocking exit" warnings.

        This design supports multiple calling conventions:
        - ManagedThreadPoolExecutor(max_workers=8, name='my_pool')  # Preferred
        - ManagedThreadPoolExecutor(max_workers=8, thread_name_prefix='my_pool-')  # Standard ThreadPoolExecutor style
        - ManagedThreadPoolExecutor(max_workers=8, name_prefix='my_pool')  # Backwards compatibility
        """
        # Determine worker count
        if max_workers is None:
            max_workers = min(32, (os.cpu_count() or 1) * 2)

        # Handle backwards compatibility: name_prefix -> name
        if name_prefix is not None and name is None:
            logger.debug(f"Using deprecated 'name_prefix' parameter. Use 'name' instead.")
            name = name_prefix

        self._pool_name = name or thread_name_prefix or 'ManagedPool'
        self._category = category
        self._priority = priority
        self._max_workers = max_workers
        self._use_daemon_threads = daemon

        prefix = thread_name_prefix or f'{self._pool_name}-worker'

        super().__init__(
            max_workers=max_workers,
            thread_name_prefix=prefix,
            initializer=initializer,
            initargs=initargs
        )

        # v124.0: Patch the thread factory to create daemon threads
        # This is the key fix - ThreadPoolExecutor creates non-daemon threads by default
        if self._use_daemon_threads:
            self._patch_thread_factory()

        # Track pending futures for cancellation
        self._pending_futures: Set[Future] = set()
        self._futures_lock = threading.Lock()

        # Register with global registry
        self._registry = get_executor_registry()
        self._executor_id = self._registry.register(
            executor=self,
            name=self._pool_name,
            max_workers=max_workers,
            category=category,
            priority=priority
        )

        logger.debug(f"Created ManagedThreadPoolExecutor: {self._pool_name} "
                    f"(workers={max_workers}, category={category}, daemon={daemon})")

    def _patch_thread_factory(self):
        """
        v124.0: Override _adjust_thread_count to create daemon threads.

        ThreadPoolExecutor creates threads in _adjust_thread_count using
        threading.Thread(...).start(). The daemon flag must be set BEFORE
        start() is called. We completely replace _adjust_thread_count with
        a version that creates daemon threads.

        This is the ONLY reliable way to create daemon threads with
        ThreadPoolExecutor in Python 3.9+.
        """
        import concurrent.futures.thread as thread_module
        import queue

        executor_ref = weakref.ref(self)
        max_workers = self._max_workers
        thread_name_prefix = getattr(self, '_thread_name_prefix', '') or f'{self._pool_name}-worker'

        def daemon_adjust_thread_count():
            """
            v124.0: Custom _adjust_thread_count that creates DAEMON threads.

            Copied from concurrent.futures.thread with daemon=True added.
            """
            executor = executor_ref()
            if executor is None:
                return

            # Prevent multiple simultaneous thread creation
            num_threads = len(executor._threads)
            if num_threads < max_workers:
                # Calculate how many threads need to be created
                for _ in range(max_workers - num_threads):
                    # Check if executor was shut down
                    if executor._shutdown:
                        return

                    num_threads = len(executor._threads)
                    thread_name = f'{thread_name_prefix}_{num_threads}'

                    # v124.0: Create thread with daemon=True
                    t = threading.Thread(
                        name=thread_name,
                        target=thread_module._worker,
                        args=(
                            weakref.ref(executor, executor._initializer_failed),
                            executor._work_queue,
                            executor._initializer,
                            executor._initargs,
                        ),
                        daemon=True,  # v124.0: KEY FIX - daemon threads!
                    )
                    t.start()
                    executor._threads.add(t)
                    thread_module._threads_queues[t] = executor._work_queue

                    # Exit early if max workers reached
                    if len(executor._threads) >= max_workers:
                        break

        self._adjust_thread_count = daemon_adjust_thread_count

    def submit(self, fn: Callable[..., T], *args, **kwargs) -> Future:
        """
        Submit a callable for execution with metrics tracking.

        Args:
            fn: Callable to execute
            *args: Positional arguments for fn
            **kwargs: Keyword arguments for fn

        Returns:
            Future representing the pending execution
        """
        # Check if shutdown initiated
        if self._registry.is_shutdown_initiated():
            raise RuntimeError(f"Executor '{self._pool_name}' is shutting down")

        # Track submission
        self._registry.update_metrics(self._executor_id, tasks_submitted=1, pending_delta=1)

        start_time = time.time()

        # Wrap function to track completion
        @functools.wraps(fn)
        def tracked_fn(*a, **kw):
            try:
                result = fn(*a, **kw)
                execution_time = time.time() - start_time
                self._registry.update_metrics(
                    self._executor_id,
                    tasks_completed=1,
                    execution_time=execution_time,
                    pending_delta=-1
                )
                return result
            except Exception as e:
                self._registry.update_metrics(
                    self._executor_id,
                    tasks_failed=1,
                    pending_delta=-1
                )
                raise

        # Submit to parent
        future = super().submit(tracked_fn, *args, **kwargs)

        # Track future for potential cancellation
        with self._futures_lock:
            self._pending_futures.add(future)
            future.add_done_callback(lambda f: self._remove_future(f))

        return future

    def _remove_future(self, future: Future):
        """Remove a completed future from tracking."""
        with self._futures_lock:
            self._pending_futures.discard(future)

    async def submit_async(self, fn: Callable[..., T], *args, **kwargs) -> T:
        """
        Async submit that awaits the result.

        Args:
            fn: Callable to execute
            *args: Positional arguments
            **kwargs: Keyword arguments

        Returns:
            Result of fn execution
        """
        loop = asyncio.get_event_loop()
        future = self.submit(fn, *args, **kwargs)
        return await loop.run_in_executor(None, future.result)

    def map_async(
        self,
        fn: Callable[[Any], T],
        *iterables,
        timeout: Optional[float] = None
    ) -> List[T]:
        """
        Map function over iterables with timeout support.

        Args:
            fn: Function to apply
            *iterables: Input iterables
            timeout: Timeout for entire operation

        Returns:
            List of results
        """
        futures = [self.submit(fn, *args) for args in zip(*iterables)]

        if timeout is not None:
            done, not_done = futures_wait(futures, timeout=timeout)
            for f in not_done:
                f.cancel()
            return [f.result() for f in done]
        else:
            return [f.result() for f in futures]

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False):
        """
        Shutdown the executor.

        Args:
            wait: Whether to wait for pending tasks
            cancel_futures: Whether to cancel pending futures
        """
        logger.debug(f"Shutting down executor: {self._pool_name}")

        # Cancel pending futures if requested
        if cancel_futures:
            with self._futures_lock:
                for future in list(self._pending_futures):
                    if not future.done():
                        future.cancel()

        # Unregister from registry
        self._registry.unregister(self._executor_id)

        # Call parent shutdown
        super().shutdown(wait=wait, cancel_futures=cancel_futures)

    @property
    def name(self) -> str:
        """Get executor name."""
        return self._pool_name

    @property
    def pending_count(self) -> int:
        """Get count of pending tasks."""
        with self._futures_lock:
            return len(self._pending_futures)

    def get_metrics(self) -> Optional[Dict[str, Any]]:
        """Get metrics for this executor."""
        info = self._registry.get_executor_info(self._executor_id)
        if info:
            return info.metrics.to_dict()
        return None


# Backwards compatibility alias
DaemonThreadPoolExecutor = ManagedThreadPoolExecutor


# =============================================================================
# CONVENIENCE FUNCTIONS
# =============================================================================

def shutdown_all_executors(wait: bool = True, timeout: float = 5.0) -> int:
    """
    Shutdown all registered executors.

    Args:
        wait: Whether to wait for pending tasks
        timeout: Maximum time to wait

    Returns:
        Number of executors shut down
    """
    registry = get_executor_registry()
    stats = registry.shutdown_all(wait=wait, timeout=timeout)
    return stats.get("successful", 0)


async def shutdown_all_executors_async(timeout: float = 5.0) -> Dict[str, Any]:
    """
    Async shutdown of all executors.

    Args:
        timeout: Maximum time to wait

    Returns:
        Shutdown statistics
    """
    registry = get_executor_registry()
    return await registry.shutdown_all_async(timeout=timeout)


def get_daemon_executor(
    max_workers: int = 4,
    name: str = 'jarvis',
    category: str = "general",
    priority: int = 0
) -> ManagedThreadPoolExecutor:
    """
    Get a managed thread pool executor.

    Creates a new executor and registers it for cleanup on shutdown.

    Args:
        max_workers: Maximum number of worker threads
        name: Thread name prefix for identification
        category: Category for grouping
        priority: Shutdown priority

    Returns:
        ManagedThreadPoolExecutor instance
    """
    return ManagedThreadPoolExecutor(
        max_workers=max_workers,
        thread_name_prefix=f'{name}-worker-',
        name=name,
        category=category,
        priority=priority
    )


def get_executor_metrics() -> Dict[str, Any]:
    """Get metrics for all executors."""
    return get_executor_registry().get_metrics()


def print_executor_report():
    """Print executor status report."""
    print(get_executor_registry().get_report())


# =============================================================================
# THREAD STATE AND POLICY (preserved from original for compatibility)
# =============================================================================

class ThreadState(Enum):
    """Thread lifecycle states"""
    CREATED = auto()
    STARTING = auto()
    RUNNING = auto()
    STOPPING = auto()
    STOPPED = auto()
    FAILED = auto()
    LEAKED = auto()


class ShutdownPhase(Enum):
    """Shutdown escalation phases"""
    GRACEFUL = auto()
    FORCEFUL = auto()
    TERMINATE = auto()
    EMERGENCY = auto()


@dataclass
class ThreadPolicy:
    """Configurable thread behavior policy - all values from environment."""
    graceful_shutdown_timeout: float = field(
        default_factory=lambda: _env_float('THREAD_GRACEFUL_TIMEOUT', 5.0))
    forceful_shutdown_timeout: float = field(
        default_factory=lambda: _env_float('THREAD_FORCEFUL_TIMEOUT', 3.0))
    terminate_shutdown_timeout: float = field(
        default_factory=lambda: _env_float('THREAD_TERMINATE_TIMEOUT', 2.0))
    emergency_shutdown_timeout: float = field(
        default_factory=lambda: _env_float('THREAD_EMERGENCY_TIMEOUT', 1.0))
    max_threads: Optional[int] = field(
        default_factory=lambda: _env_int('THREAD_MAX_COUNT', 0) or None)
    max_thread_lifetime: Optional[float] = field(
        default_factory=lambda: _env_float('THREAD_MAX_LIFETIME', 0) or None)
    warn_thread_age: float = field(
        default_factory=lambda: _env_float('THREAD_WARN_AGE', 3600.0))
    enable_health_check: bool = field(
        default_factory=lambda: _env_bool('THREAD_HEALTH_CHECK', True))
    health_check_interval: float = field(
        default_factory=lambda: _env_float('THREAD_HEALTH_INTERVAL', 30.0))
    enable_deadlock_detection: bool = field(
        default_factory=lambda: _env_bool('THREAD_DEADLOCK_DETECTION', True))
    deadlock_check_interval: float = field(
        default_factory=lambda: _env_float('THREAD_DEADLOCK_INTERVAL', 60.0))
    auto_cleanup_orphans: bool = field(
        default_factory=lambda: _env_bool('THREAD_AUTO_CLEANUP', True))
    orphan_check_interval: float = field(
        default_factory=lambda: _env_float('THREAD_ORPHAN_INTERVAL', 60.0))
    force_daemon_on_shutdown: bool = field(
        default_factory=lambda: _env_bool('THREAD_FORCE_DAEMON', True))
    log_thread_creation: bool = field(
        default_factory=lambda: _env_bool('THREAD_LOG_CREATION', True))
    log_thread_completion: bool = field(
        default_factory=lambda: _env_bool('THREAD_LOG_COMPLETION', True))
    log_stack_traces: bool = field(
        default_factory=lambda: _env_bool('THREAD_LOG_STACK', True))
    capture_full_stack: bool = field(
        default_factory=lambda: _env_bool('THREAD_FULL_STACK', False))
    use_thread_pool: bool = field(
        default_factory=lambda: _env_bool('THREAD_USE_POOL', True))
    thread_pool_size: Optional[int] = field(
        default_factory=lambda: _env_int('THREAD_POOL_SIZE', 0) or None)
    recycle_threads: bool = field(
        default_factory=lambda: _env_bool('THREAD_RECYCLE', True))


@dataclass
class ThreadMetrics:
    """Thread performance metrics"""
    total_created: int = 0
    total_completed: int = 0
    total_failed: int = 0
    total_leaked: int = 0
    total_forced_stop: int = 0
    current_active: int = 0
    current_daemon: int = 0
    current_non_daemon: int = 0
    peak_active: int = 0
    avg_lifetime_seconds: float = 0.0
    shutdown_attempts: int = 0
    successful_shutdowns: int = 0
    failed_shutdowns: int = 0


@dataclass
class ThreadInfo:
    """Comprehensive thread information"""
    thread_id: int
    thread: Union[threading.Thread, weakref.ref]
    name: str
    ident: Optional[int] = None
    state: ThreadState = ThreadState.CREATED
    created_at: datetime = field(default_factory=datetime.now)
    started_at: Optional[datetime] = None
    stopped_at: Optional[datetime] = None
    creator: str = "unknown"
    creator_stack: Optional[List[str]] = None
    purpose: str = "Unknown"
    category: str = "general"
    daemon: bool = False
    priority: int = 0
    shutdown_callback: Optional[Callable] = None
    shutdown_event: Optional[Union[threading.Event, asyncio.Event]] = None
    is_async: bool = False
    event_loop: Optional[asyncio.AbstractEventLoop] = None
    last_heartbeat: Optional[datetime] = None
    heartbeat_interval: Optional[float] = None
    health_check_callback: Optional[Callable] = None
    exception: Optional[Exception] = None
    stack_trace: Optional[str] = None
    cpu_time: float = 0.0
    parent_thread_id: Optional[int] = None
    child_thread_ids: Set[int] = field(default_factory=set)


# =============================================================================
# ADVANCED THREAD MANAGER (preserved and enhanced)
# =============================================================================

class AdvancedThreadManager:
    """
    Enterprise-grade thread manager with async support.

    Now integrated with ExecutorRegistry for unified lifecycle management.
    """

    def __init__(self, policy: Optional[ThreadPolicy] = None):
        self.policy = policy or ThreadPolicy()
        self.threads: Dict[int, ThreadInfo] = {}
        self.lock = threading.RLock()
        self.metrics = ThreadMetrics()

        self.shutdown_initiated = False
        self.shutdown_lock = threading.Lock()
        self.shutdown_event = threading.Event()

        # Use ManagedThreadPoolExecutor for integration with registry
        self.thread_pool: Optional[ManagedThreadPoolExecutor] = None
        if self.policy.use_thread_pool:
            pool_size = self.policy.thread_pool_size or (os.cpu_count() or 4) * 2
            self.thread_pool = ManagedThreadPoolExecutor(
                max_workers=pool_size,
                name="AdvancedThreadManager",
                category="thread_manager",
                priority=100  # High priority - shutdown last
            )

        self.health_check_thread: Optional[threading.Thread] = None
        self.deadlock_check_thread: Optional[threading.Thread] = None
        self.orphan_check_thread: Optional[threading.Thread] = None

        self.categories: Dict[str, Set[int]] = defaultdict(set)

        self._start_monitoring()
        atexit.register(self._emergency_cleanup)

        logger.info("🧵 AdvancedThreadManager initialized")

    def _start_monitoring(self):
        """Start background monitoring threads"""
        if self.policy.enable_health_check:
            self.health_check_thread = threading.Thread(
                target=self._health_check_loop,
                name="ThreadManager-HealthCheck",
                daemon=True
            )
            self.health_check_thread.start()

        if self.policy.enable_deadlock_detection:
            self.deadlock_check_thread = threading.Thread(
                target=self._deadlock_check_loop,
                name="ThreadManager-DeadlockCheck",
                daemon=True
            )
            self.deadlock_check_thread.start()

        if self.policy.auto_cleanup_orphans:
            self.orphan_check_thread = threading.Thread(
                target=self._orphan_check_loop,
                name="ThreadManager-OrphanCheck",
                daemon=True
            )
            self.orphan_check_thread.start()

    def _get_caller_info(self, depth: int = 2) -> Tuple[str, Optional[List[str]]]:
        """Get information about caller"""
        try:
            stack = traceback.extract_stack()
            caller = f"{stack[-depth].filename}:{stack[-depth].lineno} in {stack[-depth].name}"

            if self.policy.log_stack_traces:
                if self.policy.capture_full_stack:
                    stack_trace = [f"{frame.filename}:{frame.lineno} in {frame.name}"
                                  for frame in stack]
                else:
                    stack_trace = [f"{frame.filename}:{frame.lineno} in {frame.name}"
                                  for frame in stack[-5:]]
                return caller, stack_trace

            return caller, None
        except Exception:
            return "unknown", None

    def register(
        self,
        thread: threading.Thread,
        purpose: str = "Unknown",
        category: str = "general",
        shutdown_callback: Optional[Callable] = None,
        shutdown_event: Optional[Union[threading.Event, asyncio.Event]] = None,
        is_async: bool = False,
        event_loop: Optional[asyncio.AbstractEventLoop] = None,
        force_daemon: bool = False,
        priority: int = 0,
        heartbeat_interval: Optional[float] = None,
        health_check_callback: Optional[Callable] = None
    ) -> threading.Thread:
        """Register a thread for comprehensive tracking."""
        with self.lock:
            if self.policy.max_threads and len(self.threads) >= self.policy.max_threads:
                raise RuntimeError(
                    f"Thread limit reached: {self.policy.max_threads}. "
                    "Increase max_threads or wait for threads to complete."
                )

            caller, stack_trace = self._get_caller_info(depth=3)

            original_daemon = thread.daemon
            if force_daemon and not thread.daemon and not thread.is_alive():
                thread.daemon = True

            thread_id = id(thread)
            info = ThreadInfo(
                thread_id=thread_id,
                thread=weakref.ref(thread) if self.policy.recycle_threads else thread,
                name=thread.name,
                ident=thread.ident,
                state=ThreadState.CREATED,
                creator=caller,
                creator_stack=stack_trace,
                purpose=purpose,
                category=category,
                daemon=thread.daemon,
                priority=priority,
                shutdown_callback=shutdown_callback,
                shutdown_event=shutdown_event,
                is_async=is_async,
                event_loop=event_loop,
                heartbeat_interval=heartbeat_interval,
                health_check_callback=health_check_callback,
                parent_thread_id=id(threading.current_thread())
            )

            self.threads[thread_id] = info
            self.categories[category].add(thread_id)

            self.metrics.total_created += 1
            self.metrics.current_active += 1
            if thread.daemon:
                self.metrics.current_daemon += 1
            else:
                self.metrics.current_non_daemon += 1

            if self.metrics.current_active > self.metrics.peak_active:
                self.metrics.peak_active = self.metrics.current_active

            if self.policy.log_thread_creation:
                logger.debug(f"📝 Registered thread: {thread.name} "
                           f"(daemon={thread.daemon}, category={category})")

            return thread

    def create_thread(
        self,
        target: Callable,
        name: str,
        purpose: str = "Unknown",
        category: str = "general",
        daemon: bool = True,
        **kwargs
    ) -> threading.Thread:
        """Create and register a new thread."""
        def wrapped_target(*args, **target_kwargs):
            thread_id = id(threading.current_thread())

            try:
                with self.lock:
                    if thread_id in self.threads:
                        self.threads[thread_id].state = ThreadState.RUNNING
                        self.threads[thread_id].started_at = datetime.now()

                result = target(*args, **target_kwargs)

                with self.lock:
                    if thread_id in self.threads:
                        self.threads[thread_id].state = ThreadState.STOPPED
                        self.threads[thread_id].stopped_at = datetime.now()
                        self.metrics.total_completed += 1

                return result

            except Exception as e:
                with self.lock:
                    if thread_id in self.threads:
                        self.threads[thread_id].state = ThreadState.FAILED
                        self.threads[thread_id].exception = e
                        self.threads[thread_id].stack_trace = traceback.format_exc()
                        self.metrics.total_failed += 1
                raise

            finally:
                with self.lock:
                    if thread_id in self.threads:
                        self.metrics.current_active -= 1
                        if self.threads[thread_id].daemon:
                            self.metrics.current_daemon -= 1
                        else:
                            self.metrics.current_non_daemon -= 1

        target_args = kwargs.pop('args', ())
        target_kwargs = kwargs.pop('kwargs', {})

        thread = threading.Thread(
            target=wrapped_target,
            name=name,
            daemon=daemon,
            args=target_args,
            kwargs=target_kwargs
        )

        return self.register(thread, purpose=purpose, category=category, **kwargs)

    def submit_to_pool(self, func: Callable, *args, **kwargs) -> Future:
        """Submit task to thread pool."""
        if not self.thread_pool:
            raise RuntimeError("Thread pool not enabled")
        return self.thread_pool.submit(func, *args, **kwargs)

    def get_active_threads(self, category: Optional[str] = None) -> List[ThreadInfo]:
        """Get list of active threads."""
        with self.lock:
            threads = list(self.threads.values())

            if category:
                threads = [t for t in threads if t.category == category]

            active = []
            for info in threads:
                thread = info.thread() if isinstance(info.thread, weakref.ref) else info.thread
                if thread and thread.is_alive():
                    active.append(info)

            return active

    def _health_check_loop(self):
        """Background health check loop"""
        while not self.shutdown_event.wait(timeout=self.policy.health_check_interval):
            try:
                active = self.get_active_threads()
                now = datetime.now()

                for info in active:
                    thread_age = (now - info.created_at).total_seconds()
                    if thread_age > self.policy.warn_thread_age:
                        logger.warning(f"⚠️ Thread {info.name} running for {thread_age/3600:.1f}h")
            except Exception as e:
                logger.error(f"Health check error: {e}")

    def _deadlock_check_loop(self):
        """Background deadlock detection loop"""
        while not self.shutdown_event.wait(timeout=self.policy.deadlock_check_interval):
            try:
                with self.lock:
                    for info in self.threads.values():
                        if info.state == ThreadState.RUNNING and info.last_heartbeat:
                            age = (datetime.now() - info.last_heartbeat).total_seconds()
                            if age > 60.0:
                                logger.warning(f"⚠️ Potential deadlock: {info.name} "
                                             f"no activity for {age:.1f}s")
            except Exception as e:
                logger.error(f"Deadlock check error: {e}")

    def _orphan_check_loop(self):
        """Background orphan cleanup loop"""
        while not self.shutdown_event.wait(timeout=self.policy.orphan_check_interval):
            try:
                with self.lock:
                    orphans = []
                    for thread_id, info in list(self.threads.items()):
                        thread = info.thread() if isinstance(info.thread, weakref.ref) else info.thread
                        if not thread or not thread.is_alive():
                            if info.state not in (ThreadState.STOPPED, ThreadState.FAILED):
                                orphans.append(thread_id)

                    for thread_id in orphans:
                        info = self.threads.pop(thread_id)
                        self.categories[info.category].discard(thread_id)
                        self.metrics.total_leaked += 1
                        logger.warning(f"🧹 Cleaned up orphaned thread: {info.name}")
            except Exception as e:
                logger.error(f"Orphan check error: {e}")

    async def shutdown_async(self, timeout: Optional[float] = None) -> Dict[str, Any]:
        """Async shutdown with multi-phase escalation."""
        if self.shutdown_initiated:
            return {"already_shutdown": True}

        with self.shutdown_lock:
            self.shutdown_initiated = True
            self.shutdown_event.set()

        logger.info("🛑 Initiating thread manager shutdown...")

        # Also trigger executor registry shutdown
        registry = get_executor_registry()
        executor_stats = await registry.shutdown_all_async(timeout=timeout)

        return {
            "thread_manager": "shutdown",
            "executor_registry": executor_stats
        }

    def shutdown_sync(self, timeout: Optional[float] = None) -> Dict[str, Any]:
        """Synchronous shutdown wrapper."""
        if self.shutdown_initiated:
            return {"already_shutdown": True}

        with self.shutdown_lock:
            self.shutdown_initiated = True
            self.shutdown_event.set()

        # Shutdown executor registry
        registry = get_executor_registry()
        executor_stats = registry.shutdown_all(wait=True, timeout=timeout or 5.0)

        return {
            "thread_manager": "shutdown",
            "executor_registry": executor_stats
        }

    def _emergency_cleanup(self):
        """Emergency cleanup called by atexit."""
        if not self.shutdown_initiated:
            logger.debug("⚠️ Emergency thread manager cleanup")
            try:
                self.shutdown_sync(timeout=2.0)
            except Exception as e:
                logger.error(f"Emergency cleanup failed: {e}")

    def get_metrics(self) -> Dict[str, Any]:
        """Get current metrics."""
        return asdict(self.metrics)

    def get_report(self) -> Dict[str, Any]:
        """Get comprehensive report."""
        active = self.get_active_threads()

        return {
            "metrics": self.get_metrics(),
            "active_threads": len(active),
            "by_category": {
                cat: len([t for t in active if t.category == cat])
                for cat in self.categories.keys()
            },
            "executor_metrics": get_executor_metrics()
        }


# =============================================================================
# GLOBAL INSTANCES AND CONVENIENCE FUNCTIONS
# =============================================================================

_thread_manager: Optional[AdvancedThreadManager] = None
_manager_lock = threading.Lock()


def get_thread_manager(policy: Optional[ThreadPolicy] = None) -> AdvancedThreadManager:
    """Get or create global thread manager."""
    global _thread_manager

    with _manager_lock:
        if _thread_manager is None:
            _thread_manager = AdvancedThreadManager(policy=policy)
        return _thread_manager


def create_managed_thread(target: Callable, name: str, **kwargs) -> threading.Thread:
    """Convenience function to create managed thread."""
    manager = get_thread_manager()
    return manager.create_thread(target=target, name=name, **kwargs)


async def shutdown_all_threads_async(timeout: Optional[float] = None) -> Dict[str, Any]:
    """Async shutdown all threads and executors."""
    manager = get_thread_manager()
    return await manager.shutdown_async(timeout=timeout)


def shutdown_all_threads(timeout: Optional[float] = None) -> Dict[str, Any]:
    """Sync shutdown all threads and executors."""
    manager = get_thread_manager()
    return manager.shutdown_sync(timeout=timeout)


def shutdown_third_party_threads(timeout: float = 5.0) -> Dict[str, Any]:
    """
    Shutdown threads from third-party libraries (PyTorch, database pools, etc).
    
    These libraries create their own threads that don't go through our thread
    manager. This function attempts to cleanly shutdown these threads.
    
    Args:
        timeout: Maximum time to wait for threads to complete
        
    Returns:
        Dict with cleanup statistics
    """
    import gc
    import time
    
    stats = {
        "pytorch_cleaned": False,
        "torch_threads_before": 0,
        "torch_threads_after": 0,
        "db_pools_cleaned": False,
        "remaining_non_daemon": 0,
        "duration": 0.0
    }
    
    start_time = time.time()
    
    # Count threads before
    all_threads = threading.enumerate()
    pytorch_threads = [t for t in all_threads if 'pytorch' in t.name.lower() or 'worker' in t.name.lower()]
    stats["torch_threads_before"] = len(pytorch_threads)
    
    # Phase 1: PyTorch cleanup
    try:
        import torch

        # NOTE: Do NOT call set_num_threads or set_num_interop_threads during shutdown.
        # These can only be called once and before any parallel work starts.
        # Calling them after parallel work has started raises RuntimeError.
        # Instead, we focus on synchronizing and releasing GPU memory.

        # Clear CUDA if available
        if torch.cuda.is_available():
            try:
                torch.cuda.synchronize()  # Wait for all CUDA operations
                torch.cuda.empty_cache()  # Release cached memory
            except Exception as cuda_e:
                logger.debug(f"CUDA cleanup: {cuda_e}")

        # Clear MPS (Apple Silicon) if available
        # v225.0: Enhanced MPS cleanup to prevent "commit an already committed command buffer" crash
        # This crash occurs when Metal command buffers are double-committed during shutdown.
        # The fix involves multiple synchronization passes and a brief wait to let any
        # in-flight operations complete before we release memory.
        if hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
            try:
                # Pass 1: Synchronize to wait for queued operations
                if hasattr(torch.mps, 'synchronize'):
                    torch.mps.synchronize()
                
                # Pass 2: Brief wait for any background Metal operations
                # This is critical - some operations may be scheduled but not yet in the queue
                time.sleep(0.1)  # 100ms for any async Metal operations to complete
                
                # Pass 3: Second synchronize to catch any operations that started during wait
                if hasattr(torch.mps, 'synchronize'):
                    torch.mps.synchronize()
                
                # Pass 4: Now safe to empty cache
                if hasattr(torch.mps, 'empty_cache'):
                    torch.mps.empty_cache()
                
                # Pass 5: Final synchronize after cache clear
                if hasattr(torch.mps, 'synchronize'):
                    torch.mps.synchronize()
                
                logger.debug("MPS cleanup completed with multi-pass synchronization")
            except Exception as mps_e:
                logger.debug(f"MPS cleanup: {mps_e}")

        # Signal completion - PyTorch worker threads are daemon threads
        # and will exit when the main process exits
        stats["pytorch_cleaned"] = True
        logger.debug("PyTorch GPU memory released; daemon threads will exit with process")

    except ImportError:
        logger.debug("PyTorch not installed")
    except Exception as e:
        logger.debug(f"PyTorch cleanup partial: {e}")
    
    # Phase 2: Database pools
    try:
        # SQLAlchemy - sync cleanup
        try:
            from sqlalchemy.orm import close_all_sessions
            close_all_sessions()
            logger.debug("SQLAlchemy sessions closed")
        except ImportError:
            pass
        except Exception as e:
            logger.debug(f"SQLAlchemy cleanup: {e}")

        # Connection managers - use sync accessor to avoid unawaited coroutine
        # v149.0: FIX - Use sync accessor instead of async get_database_adapter
        try:
            from intelligence.cloud_database_adapter import (
                get_database_adapter_sync,
                close_database_adapter_sync,
            )
            adapter = get_database_adapter_sync()
            if adapter is not None:
                # Use the centralized sync close which handles all event loop cases
                closed = close_database_adapter_sync()
                if closed:
                    logger.debug("Database adapter closed via sync accessor")
                else:
                    logger.debug("Database adapter close failed, may complete async")

        except ImportError:
            pass
        except Exception as e:
            logger.debug(f"Database adapter cleanup: {e}")

        stats["db_pools_cleaned"] = True

    except Exception as e:
        logger.debug(f"Database cleanup partial: {e}")
    
    # Phase 3: Force garbage collection
    gc.collect()
    
    # Phase 4: Wait for threads to complete
    deadline = time.time() + timeout
    while time.time() < deadline:
        remaining = [
            t for t in threading.enumerate()
            if t != threading.main_thread() and not t.daemon and t.is_alive()
        ]
        if not remaining:
            break
        time.sleep(0.1)
    
    # Final stats
    all_threads_after = threading.enumerate()
    pytorch_threads_after = [t for t in all_threads_after if 'pytorch' in t.name.lower() or 'worker' in t.name.lower()]
    stats["torch_threads_after"] = len(pytorch_threads_after)
    
    remaining_non_daemon = [
        t for t in all_threads_after
        if t != threading.main_thread() and not t.daemon and t.is_alive()
    ]
    stats["remaining_non_daemon"] = len(remaining_non_daemon)
    stats["duration"] = time.time() - start_time
    
    logger.info(f"Third-party thread cleanup: PyTorch {stats['torch_threads_before']}->{stats['torch_threads_after']}, "
                f"remaining={stats['remaining_non_daemon']}, took {stats['duration']:.2f}s")

    return stats


# =============================================================================
# v124.0: FORCE THREAD TERMINATION - Last Resort Cleanup
# =============================================================================

def _raise_exception_in_thread(thread_id: int, exc_type: type) -> bool:
    """
    v124.0: Raise an exception in a thread using ctypes.

    This is a low-level operation that uses Python's C API to inject
    an exception into a running thread. Use with caution as it can
    cause resource leaks if the thread holds locks or has open files.

    Args:
        thread_id: The thread's native ID
        exc_type: The exception type to raise (e.g., SystemExit)

    Returns:
        True if successful, False otherwise
    """
    try:
        res = ctypes.pythonapi.PyThreadState_SetAsyncExc(
            ctypes.c_ulong(thread_id),
            ctypes.py_object(exc_type)
        )
        if res == 0:
            logger.debug(f"[v124.0] Thread {thread_id} not found")
            return False
        elif res == 1:
            logger.debug(f"[v124.0] Exception raised in thread {thread_id}")
            return True
        else:
            # Multiple threads affected - this is bad, reset
            ctypes.pythonapi.PyThreadState_SetAsyncExc(
                ctypes.c_ulong(thread_id),
                None
            )
            logger.warning(f"[v124.0] PyThreadState_SetAsyncExc returned {res}, reset")
            return False
    except Exception as e:
        logger.debug(f"[v124.0] Failed to raise exception in thread {thread_id}: {e}")
        return False


def force_terminate_thread(thread: threading.Thread, timeout: float = 1.0) -> bool:
    """
    v124.0: Force-terminate a thread by injecting SystemExit.

    This is a last-resort operation. The thread may not clean up properly.

    Args:
        thread: The thread to terminate
        timeout: Time to wait for thread to die after injection

    Returns:
        True if thread terminated, False otherwise
    """
    if not thread.is_alive():
        return True

    thread_id = thread.ident
    if thread_id is None:
        return False

    logger.warning(f"[v124.0] Force-terminating thread: {thread.name} (id={thread_id})")

    # Try to raise SystemExit in the thread
    success = _raise_exception_in_thread(thread_id, SystemExit)
    if not success:
        return False

    # Wait for thread to die
    thread.join(timeout=timeout)
    return not thread.is_alive()


def convert_remaining_to_daemon(exclude_names: Optional[List[str]] = None) -> int:
    """
    v124.0: Convert remaining non-daemon threads to daemon.

    This is a nuclear option that allows sys.exit() to proceed even if
    threads are still running. Those threads will be terminated when
    the process exits.

    WARNING: This can cause issues if threads hold locks or resources.
    Use only as a last resort before process exit.

    Args:
        exclude_names: Thread names to exclude from conversion

    Returns:
        Number of threads converted
    """
    exclude_names = exclude_names or []
    exclude_names.extend(['MainThread'])

    converted = 0
    for thread in threading.enumerate():
        if thread.name in exclude_names:
            continue
        if not thread.daemon and thread.is_alive():
            try:
                # Can only set daemon on threads that haven't started
                # For running threads, we need to use a different approach
                thread.daemon = True
                converted += 1
                logger.debug(f"[v124.0] Converted thread {thread.name} to daemon")
            except RuntimeError:
                # Thread already started - can't change daemon status
                # This is expected - we just log it
                logger.debug(f"[v124.0] Cannot convert running thread {thread.name} to daemon")

    if converted > 0:
        logger.info(f"[v124.0] Converted {converted} threads to daemon")

    return converted


def final_thread_cleanup(
    timeout: float = 5.0,
    force_terminate: bool = True,
    allow_daemon_conversion: bool = False
) -> Dict[str, Any]:
    """
    v124.0: Final comprehensive thread cleanup before process exit.

    This is the LAST function to call before sys.exit(). It:
    1. Shuts down all registered executors
    2. Cleans up third-party threads (PyTorch, DB pools)
    3. Optionally force-terminates stubborn threads
    4. Optionally converts remaining to daemon (nuclear option)

    Args:
        timeout: Total timeout for cleanup
        force_terminate: Whether to force-terminate stubborn threads
        allow_daemon_conversion: Whether to convert remaining to daemon (nuclear)

    Returns:
        Cleanup statistics
    """
    import gc

    stats = {
        "executors_shutdown": 0,
        "third_party_cleaned": False,
        "threads_force_terminated": 0,
        "threads_converted_to_daemon": 0,
        "remaining_non_daemon": 0,
        "remaining_thread_names": [],
        "duration": 0.0,
        "success": False,
    }

    start_time = time.time()
    phase_timeout = timeout / 4

    logger.info("[v124.0] Final thread cleanup starting...")

    # Phase 1: Shutdown executors
    try:
        registry = get_executor_registry()
        executor_stats = registry.shutdown_all(wait=True, timeout=phase_timeout, cancel_pending=True)
        stats["executors_shutdown"] = executor_stats.get("successful", 0)
        logger.info(f"[v124.0] Phase 1: {stats['executors_shutdown']} executors shutdown")
    except Exception as e:
        logger.warning(f"[v124.0] Executor shutdown error: {e}")

    # Phase 2: Third-party cleanup
    try:
        third_party_stats = shutdown_third_party_threads(timeout=phase_timeout)
        stats["third_party_cleaned"] = third_party_stats.get("pytorch_cleaned", False)
        logger.info(f"[v124.0] Phase 2: Third-party cleanup complete")
    except Exception as e:
        logger.warning(f"[v124.0] Third-party cleanup error: {e}")

    # Phase 3: Force terminate stubborn threads
    # v149.0: Skip executor workers and asyncio threads - they should be handled by their managers
    if force_terminate:
        # Patterns for threads that should NOT be force-terminated
        # These are managed by their respective systems
        skip_patterns = (
            'ThreadPoolExecutor',  # Managed by executor.shutdown()
            'asyncio',             # Managed by event loop
            'QueueFeederThread',   # multiprocessing Queue feeder
            'SelectorThread',      # asyncio selector
        )

        remaining = [
            t for t in threading.enumerate()
            if (t != threading.main_thread()
                and not t.daemon
                and t.is_alive()
                and not any(pattern in t.name for pattern in skip_patterns))
        ]

        # Log skipped threads for debugging
        skipped = [
            t.name for t in threading.enumerate()
            if (t != threading.main_thread()
                and not t.daemon
                and t.is_alive()
                and any(pattern in t.name for pattern in skip_patterns))
        ]
        if skipped:
            logger.debug(f"[v149.0] Phase 3: Skipping managed threads: {skipped}")

        for thread in remaining:
            if force_terminate_thread(thread, timeout=1.0):
                stats["threads_force_terminated"] += 1

        if stats["threads_force_terminated"] > 0:
            logger.info(f"[v124.0] Phase 3: Force-terminated {stats['threads_force_terminated']} threads")

    # Phase 4: Convert remaining to daemon (nuclear option)
    if allow_daemon_conversion:
        stats["threads_converted_to_daemon"] = convert_remaining_to_daemon()
        if stats["threads_converted_to_daemon"] > 0:
            logger.info(f"[v124.0] Phase 4: Converted {stats['threads_converted_to_daemon']} threads to daemon")

    # Force garbage collection
    gc.collect()

    # Final count
    remaining = [
        t for t in threading.enumerate()
        if t != threading.main_thread() and not t.daemon and t.is_alive()
    ]
    stats["remaining_non_daemon"] = len(remaining)
    stats["remaining_thread_names"] = [t.name for t in remaining]
    stats["duration"] = time.time() - start_time
    stats["success"] = stats["remaining_non_daemon"] == 0

    if stats["success"]:
        logger.info(f"[v124.0] Final cleanup SUCCESS: All non-daemon threads stopped in {stats['duration']:.2f}s")
    else:
        logger.warning(
            f"[v124.0] Final cleanup: {stats['remaining_non_daemon']} non-daemon threads remaining: "
            f"{stats['remaining_thread_names']}"
        )

    return stats


async def final_thread_cleanup_async(
    timeout: float = 5.0,
    force_terminate: bool = True,
    allow_daemon_conversion: bool = False
) -> Dict[str, Any]:
    """
    v124.0: Async version of final_thread_cleanup.

    Runs the cleanup in a thread pool to avoid blocking the event loop.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        lambda: final_thread_cleanup(
            timeout=timeout,
            force_terminate=force_terminate,
            allow_daemon_conversion=allow_daemon_conversion
        )
    )


# =============================================================================
# HTTP CLIENT REGISTRY - Centralized lifecycle management for aiohttp/httpx
# =============================================================================

class HTTPClientRegistry:
    """
    Centralized registry for HTTP client sessions (aiohttp.ClientSession, httpx.AsyncClient).

    This solves the "Unclosed client session" warnings by:
    1. Tracking all created HTTP clients via weak references
    2. Providing centralized async cleanup during shutdown
    3. Supporting both aiohttp and httpx clients
    4. Graceful degradation when clients are already closed

    Usage:
        # Register a client when created
        from core.thread_manager import get_http_client_registry
        registry = get_http_client_registry()

        session = aiohttp.ClientSession()
        registry.register(session, name="my-service")

        # On shutdown, all registered clients are closed automatically
        await registry.close_all()
    """

    _instance: Optional['HTTPClientRegistry'] = None
    _instance_lock = threading.Lock()

    def __new__(cls) -> 'HTTPClientRegistry':
        """Singleton pattern for global registry."""
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        """Initialize registry (only once due to singleton)."""
        if self._initialized:
            return

        self._initialized = True
        self._lock = threading.RLock()
        self._clients: Dict[int, Dict[str, Any]] = {}
        self._closed = False

        logger.debug("🌐 HTTPClientRegistry initialized")

    def register(
        self,
        client: Any,
        name: str = "unnamed",
        owner: Optional[str] = None
    ) -> int:
        """
        Register an HTTP client for lifecycle management.

        Args:
            client: aiohttp.ClientSession or httpx.AsyncClient instance
            name: Human-readable name for logging
            owner: Optional owner module/class name

        Returns:
            Client ID for later reference
        """
        if self._closed:
            logger.warning(f"Cannot register client '{name}': registry is closed")
            return -1

        client_id = id(client)

        # Determine client type
        client_type = type(client).__module__ + "." + type(client).__name__

        # Get close method
        close_method = None
        if hasattr(client, 'aclose'):  # httpx.AsyncClient
            close_method = 'aclose'
        elif hasattr(client, 'close'):  # aiohttp.ClientSession
            close_method = 'close'

        # Capture creation stack for debugging
        try:
            creator_stack = ''.join(traceback.format_stack()[:-2][-3:])
        except Exception:
            creator_stack = None

        with self._lock:
            self._clients[client_id] = {
                "client_ref": weakref.ref(client),
                "name": name,
                "type": client_type,
                "close_method": close_method,
                "owner": owner,
                "created_at": datetime.now(),
                "creator_stack": creator_stack,
                "closed": False,
            }

        logger.debug(f"📝 Registered HTTP client: {name} ({client_type})")
        return client_id

    def unregister(self, client_id: int) -> bool:
        """
        Unregister an HTTP client.

        Args:
            client_id: The client ID to unregister

        Returns:
            True if found and removed, False otherwise
        """
        with self._lock:
            if client_id in self._clients:
                info = self._clients.pop(client_id)
                logger.debug(f"📤 Unregistered HTTP client: {info['name']}")
                return True
            return False

    def get_all_clients(self) -> List[Dict[str, Any]]:
        """Get list of all registered clients."""
        with self._lock:
            result = []
            for client_id, info in self._clients.items():
                client = info["client_ref"]()
                result.append({
                    "id": client_id,
                    "name": info["name"],
                    "type": info["type"],
                    "alive": client is not None,
                    "closed": info["closed"],
                    "owner": info["owner"],
                    "age_seconds": (datetime.now() - info["created_at"]).total_seconds(),
                })
            return result

    async def close_client(self, client_id: int) -> bool:
        """
        Close a specific client.

        Args:
            client_id: The client ID to close

        Returns:
            True if successfully closed, False otherwise
        """
        with self._lock:
            info = self._clients.get(client_id)
            if not info:
                return False
            if info["closed"]:
                return True  # Already closed

        client = info["client_ref"]()
        if client is None:
            # Client was garbage collected
            with self._lock:
                info["closed"] = True
            return True

        try:
            close_method = info.get("close_method")
            if close_method:
                method = getattr(client, close_method, None)
                if method:
                    result = method()
                    if asyncio.iscoroutine(result):
                        await result
                    with self._lock:
                        info["closed"] = True
                    logger.debug(f"✅ Closed HTTP client: {info['name']}")
                    return True
        except Exception as e:
            logger.debug(f"Error closing HTTP client {info['name']}: {e}")

        return False

    async def close_all(self, timeout: float = 10.0) -> Dict[str, Any]:
        """
        Close all registered HTTP clients.

        Args:
            timeout: Maximum time to wait for all clients to close

        Returns:
            Cleanup statistics
        """
        if self._closed:
            return {"already_closed": True}

        start_time = time.time()
        stats = {
            "total_clients": 0,
            "closed": 0,
            "already_closed": 0,
            "failed": 0,
            "gc_collected": 0,
            "errors": [],
            "duration": 0.0,
        }

        with self._lock:
            clients_to_close = list(self._clients.items())
            stats["total_clients"] = len(clients_to_close)

        if not clients_to_close:
            logger.debug("No HTTP clients to close")
            return stats

        logger.info(f"🌐 Closing {len(clients_to_close)} HTTP client(s)...")

        # Close clients with timeout
        close_tasks = []
        for client_id, info in clients_to_close:
            if info["closed"]:
                stats["already_closed"] += 1
                continue

            client = info["client_ref"]()
            if client is None:
                stats["gc_collected"] += 1
                with self._lock:
                    info["closed"] = True
                continue

            # Create close task
            close_tasks.append(self._close_with_timeout(client_id, info, client, timeout / 2))

        if close_tasks:
            results = await asyncio.gather(*close_tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    stats["failed"] += 1
                    stats["errors"].append(str(result))
                elif result:
                    stats["closed"] += 1
                else:
                    stats["failed"] += 1

        self._closed = True
        stats["duration"] = time.time() - start_time

        logger.info(f"✅ HTTP client cleanup complete: {stats['closed']}/{stats['total_clients']} closed "
                   f"in {stats['duration']:.2f}s")

        return stats

    async def _close_with_timeout(
        self,
        client_id: int,
        info: Dict[str, Any],
        client: Any,
        timeout: float
    ) -> bool:
        """Close a client with timeout."""
        try:
            close_method = info.get("close_method")
            if not close_method:
                return False

            method = getattr(client, close_method, None)
            if not method:
                return False

            result = method()
            if asyncio.iscoroutine(result):
                await asyncio.wait_for(result, timeout=timeout)

            with self._lock:
                info["closed"] = True

            logger.debug(f"   ✅ {info['name']}")
            return True

        except asyncio.TimeoutError:
            logger.warning(f"   ⏱️ Timeout closing {info['name']}")
            return False
        except Exception as e:
            logger.debug(f"   ❌ Error closing {info['name']}: {e}")
            return False

    def get_report(self) -> str:
        """Generate a human-readable report."""
        clients = self.get_all_clients()

        lines = [
            "",
            "=" * 70,
            "🌐 HTTP CLIENT REGISTRY REPORT",
            "=" * 70,
            f"Total Registered: {len(clients)}",
            f"Registry Closed:  {self._closed}",
            "",
        ]

        if clients:
            lines.append("Registered Clients:")
            for client in clients:
                status = "✅ closed" if client["closed"] else ("💀 GC'd" if not client["alive"] else "🔵 open")
                lines.append(f"  - {client['name']}: {client['type']} [{status}] "
                           f"(age: {client['age_seconds']:.1f}s)")

        lines.append("=" * 70)
        lines.append("")

        return "\n".join(lines)


# Global HTTP client registry instance
_http_client_registry: Optional[HTTPClientRegistry] = None
_http_registry_lock = threading.Lock()


def get_http_client_registry() -> HTTPClientRegistry:
    """Get the global HTTP client registry instance."""
    global _http_client_registry

    with _http_registry_lock:
        if _http_client_registry is None:
            _http_client_registry = HTTPClientRegistry()
        return _http_client_registry


def register_http_client(client: Any, name: str = "unnamed", owner: Optional[str] = None) -> int:
    """
    Convenience function to register an HTTP client.

    Args:
        client: aiohttp.ClientSession or httpx.AsyncClient
        name: Human-readable name
        owner: Optional owner module/class

    Returns:
        Client ID
    """
    registry = get_http_client_registry()
    return registry.register(client, name=name, owner=owner)


async def close_all_http_clients(timeout: float = 10.0) -> Dict[str, Any]:
    """
    Convenience function to close all HTTP clients.

    Args:
        timeout: Maximum time to wait

    Returns:
        Cleanup statistics
    """
    registry = get_http_client_registry()
    return await registry.close_all(timeout=timeout)


# =============================================================================
# COMPREHENSIVE SHUTDOWN COORDINATOR - Single point for all cleanup
# =============================================================================

class ShutdownCoordinator:
    """
    Coordinates complete application shutdown including:
    - HTTP clients (aiohttp, httpx)
    - Thread pools (executors)
    - Third-party library threads (PyTorch, etc.)
    - Managed threads
    - Database connections

    Provides a single entry point for clean shutdown with multi-phase
    escalation and comprehensive logging.
    """

    _instance: Optional['ShutdownCoordinator'] = None
    _instance_lock = threading.Lock()

    def __new__(cls) -> 'ShutdownCoordinator':
        """Singleton pattern."""
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        """Initialize coordinator."""
        if self._initialized:
            return

        self._initialized = True
        self._shutdown_started = False
        self._shutdown_complete = False
        self._shutdown_lock = threading.Lock()
        self._shutdown_event = asyncio.Event() if asyncio.get_event_loop().is_running() else None

        logger.debug("🛑 ShutdownCoordinator initialized")

    def is_shutting_down(self) -> bool:
        """Check if shutdown has been initiated."""
        return self._shutdown_started

    def is_shutdown_complete(self) -> bool:
        """Check if shutdown is complete."""
        return self._shutdown_complete

    async def shutdown(
        self,
        timeout: float = 20.0,
        skip_http: bool = False,
        skip_threads: bool = False,
        skip_executors: bool = False,
    ) -> Dict[str, Any]:
        """
        Execute comprehensive shutdown.

        Args:
            timeout: Total timeout for shutdown
            skip_http: Skip HTTP client cleanup
            skip_threads: Skip third-party thread cleanup
            skip_executors: Skip executor shutdown

        Returns:
            Comprehensive shutdown statistics
        """
        with self._shutdown_lock:
            if self._shutdown_started:
                logger.warning("Shutdown already in progress")
                return {"already_started": True}
            self._shutdown_started = True

        start_time = time.time()
        stats = {
            "phases": {},
            "total_duration": 0.0,
            "success": True,
            "errors": [],
        }

        logger.info("=" * 70)
        logger.info("🛑 COMPREHENSIVE SHUTDOWN INITIATED")
        logger.info("=" * 70)

        phase_timeout = timeout / 4  # Divide timeout among phases

        # Phase 1: HTTP Clients
        if not skip_http:
            logger.info("📌 Phase 1/4: HTTP Client Cleanup")
            try:
                http_stats = await close_all_http_clients(timeout=phase_timeout)
                stats["phases"]["http_clients"] = http_stats
                logger.info(f"   ✅ HTTP: {http_stats.get('closed', 0)} clients closed")
            except Exception as e:
                logger.error(f"   ❌ HTTP cleanup error: {e}")
                stats["errors"].append(f"HTTP: {e}")
        else:
            stats["phases"]["http_clients"] = {"skipped": True}

        # Phase 2: Executor Registry
        if not skip_executors:
            logger.info("📌 Phase 2/4: Executor Shutdown")
            try:
                registry = get_executor_registry()
                executor_stats = await registry.shutdown_all_async(timeout=phase_timeout)
                stats["phases"]["executors"] = executor_stats
                logger.info(f"   ✅ Executors: {executor_stats.get('successful', 0)}/{executor_stats.get('total_executors', 0)} shutdown")
            except Exception as e:
                logger.error(f"   ❌ Executor shutdown error: {e}")
                stats["errors"].append(f"Executors: {e}")
        else:
            stats["phases"]["executors"] = {"skipped": True}

        # Phase 3: Thread Manager
        if not skip_threads:
            logger.info("📌 Phase 3/4: Thread Manager Shutdown")
            try:
                thread_stats = await shutdown_all_threads_async(timeout=phase_timeout)
                stats["phases"]["thread_manager"] = thread_stats
                logger.info(f"   ✅ Thread manager shutdown complete")
            except Exception as e:
                logger.error(f"   ❌ Thread manager error: {e}")
                stats["errors"].append(f"Threads: {e}")
        else:
            stats["phases"]["thread_manager"] = {"skipped": True}

        # Phase 4: Third-party Libraries (PyTorch, etc.)
        logger.info("📌 Phase 4/4: Third-party Library Cleanup")
        try:
            # Run in executor to avoid blocking
            loop = asyncio.get_event_loop()
            third_party_stats = await loop.run_in_executor(
                None,
                lambda: shutdown_third_party_threads(timeout=phase_timeout)
            )
            stats["phases"]["third_party"] = third_party_stats
            logger.info(f"   ✅ Third-party: {third_party_stats.get('remaining_non_daemon', 0)} threads remaining")
        except Exception as e:
            logger.error(f"   ❌ Third-party cleanup error: {e}")
            stats["errors"].append(f"Third-party: {e}")

        # Final stats
        stats["total_duration"] = time.time() - start_time
        stats["success"] = len(stats["errors"]) == 0
        self._shutdown_complete = True

        # Log final summary
        logger.info("=" * 70)
        if stats["success"]:
            logger.info(f"✅ SHUTDOWN COMPLETE in {stats['total_duration']:.2f}s")
        else:
            logger.warning(f"⚠️ SHUTDOWN COMPLETED WITH ERRORS in {stats['total_duration']:.2f}s")
            for error in stats["errors"]:
                logger.warning(f"   • {error}")
        logger.info("=" * 70)

        # Final thread report
        remaining = [
            t for t in threading.enumerate()
            if t != threading.main_thread() and not t.daemon and t.is_alive()
        ]
        if remaining:
            logger.warning(f"⚠️ {len(remaining)} non-daemon threads still running:")
            for t in remaining[:10]:  # Log first 10
                logger.warning(f"   • {t.name}")
        else:
            logger.info("✅ All non-daemon threads have stopped")

        return stats

    def shutdown_sync(self, timeout: float = 20.0) -> Dict[str, Any]:
        """
        Synchronous shutdown wrapper.

        Args:
            timeout: Total timeout for shutdown

        Returns:
            Shutdown statistics
        """
        try:
            # Try to get existing event loop
            try:
                loop = asyncio.get_running_loop()
                # We're in an async context - schedule on that loop
                future = asyncio.ensure_future(self.shutdown(timeout=timeout))
                # Can't wait here, return immediately
                return {"scheduled": True, "async": True}
            except RuntimeError:
                pass

            # Create new event loop
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                return loop.run_until_complete(self.shutdown(timeout=timeout))
            finally:
                loop.close()

        except Exception as e:
            logger.error(f"Sync shutdown error: {e}")
            return {"error": str(e)}


# Global shutdown coordinator
_shutdown_coordinator: Optional[ShutdownCoordinator] = None
_coordinator_lock = threading.Lock()


def get_shutdown_coordinator() -> ShutdownCoordinator:
    """Get the global shutdown coordinator instance."""
    global _shutdown_coordinator

    with _coordinator_lock:
        if _shutdown_coordinator is None:
            _shutdown_coordinator = ShutdownCoordinator()
        return _shutdown_coordinator


async def comprehensive_shutdown(timeout: float = 20.0) -> Dict[str, Any]:
    """
    Execute comprehensive application shutdown.

    This is the recommended single entry point for shutdown.
    Handles HTTP clients, executors, threads, and third-party libraries.

    Args:
        timeout: Total timeout for all phases

    Returns:
        Comprehensive shutdown statistics
    """
    coordinator = get_shutdown_coordinator()
    return await coordinator.shutdown(timeout=timeout)


def comprehensive_shutdown_sync(timeout: float = 20.0) -> Dict[str, Any]:
    """
    Synchronous version of comprehensive_shutdown.

    Args:
        timeout: Total timeout for all phases

    Returns:
        Shutdown statistics
    """
    coordinator = get_shutdown_coordinator()
    return coordinator.shutdown_sync(timeout=timeout)


# =============================================================================
# NATIVE LIBRARY SAFETY GUARD - Prevents Segfaults from C Extensions
# =============================================================================
# This provides unified protection for operations involving:
# - PyTorch/SpeechBrain model inference
# - AVFoundation (macOS video/audio APIs)
# - PIL/NumPy array operations
# - PyAudio/SoundDevice callbacks
# - OpenCV operations
# - Any C extension with threading issues
# =============================================================================


class NativeLibraryType(Enum):
    """Types of native libraries requiring protection."""
    PYTORCH = auto()      # PyTorch, SpeechBrain, Transformers
    AVFOUNDATION = auto() # macOS AVCaptureSession, AVAudioSession
    PYAUDIO = auto()      # PortAudio, SoundDevice
    OPENCV = auto()       # cv2, image processing
    NUMPY = auto()        # NumPy array operations in callbacks
    GENERIC = auto()      # Other C extensions


@dataclass
class NativeOperationStats:
    """Statistics for native library operations."""
    library_type: NativeLibraryType
    total_operations: int = 0
    successful_operations: int = 0
    failed_operations: int = 0
    total_time_ms: float = 0.0
    max_time_ms: float = 0.0
    last_error: Optional[str] = None
    last_operation_time: Optional[datetime] = None


class NativeLibrarySafetyGuard:
    """
    Unified protection for native library operations that can cause segfaults.

    The Problem:
    ============
    Native C extensions (PyTorch, AVFoundation, PyAudio, etc.) are not thread-safe.
    Calling them from arbitrary threads, especially during shutdown, causes:
    - SIGSEGV (segmentation fault)
    - Memory corruption
    - GIL deadlocks
    - Resource leaks

    The Solution:
    =============
    This guard provides:
    1. Main thread dispatch for thread-sensitive APIs (AVFoundation)
    2. Single-threaded executor for PyTorch operations
    3. Callback protection with shutdown coordination
    4. Safe cleanup with proper resource release order
    5. Error isolation to prevent cascade failures

    Usage:
    ======
    ```python
    guard = NativeLibrarySafetyGuard.get_instance()

    # For PyTorch operations
    result = await guard.execute_pytorch(
        lambda: model(input_tensor),
        timeout=30.0
    )

    # For AVFoundation (must run on main thread)
    result = await guard.execute_main_thread(
        lambda: capture_session.startRunning()
    )

    # For callback-based APIs (PyAudio, SoundDevice)
    with guard.callback_context(NativeLibraryType.PYAUDIO) as should_process:
        if should_process:
            process_audio(data)
    ```
    """

    _instance: Optional['NativeLibrarySafetyGuard'] = None
    _instance_lock = threading.Lock()

    def __new__(cls) -> 'NativeLibrarySafetyGuard':
        """Singleton pattern."""
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        """Initialize the safety guard."""
        if self._initialized:
            return

        self._initialized = True
        self._lock = threading.RLock()

        # Shutdown coordination
        self._shutdown_requested = threading.Event()
        self._active_operations: Dict[NativeLibraryType, int] = defaultdict(int)
        self._operations_lock = threading.Lock()
        self._all_operations_done = threading.Condition(self._operations_lock)

        # Dedicated executors for different library types
        self._pytorch_executor: Optional[ThreadPoolExecutor] = None
        self._main_thread_queue: Optional[asyncio.Queue] = None
        self._main_thread_loop: Optional[asyncio.AbstractEventLoop] = None

        # Statistics
        self._stats: Dict[NativeLibraryType, NativeOperationStats] = {
            lib_type: NativeOperationStats(library_type=lib_type)
            for lib_type in NativeLibraryType
        }

        # Error tracking
        self._consecutive_errors: Dict[NativeLibraryType, int] = defaultdict(int)
        self._max_consecutive_errors = _env_int('NATIVE_MAX_CONSECUTIVE_ERRORS', 5)

        # Configuration
        self._pytorch_workers = 1  # MUST be 1 for PyTorch thread safety
        self._default_timeout = _env_float('NATIVE_OP_TIMEOUT', 30.0)

        # Register with shutdown coordinator
        self._register_shutdown_handler()

        logger.info("🛡️ NativeLibrarySafetyGuard initialized")

    def _register_shutdown_handler(self):
        """Register shutdown handler with global coordinator."""
        try:
            def on_shutdown():
                self.request_shutdown()
                self.wait_for_operations(timeout=5.0)

            atexit.register(on_shutdown)
        except Exception as e:
            logger.warning(f"Could not register shutdown handler: {e}")

    @classmethod
    def get_instance(cls) -> 'NativeLibrarySafetyGuard':
        """Get the singleton instance."""
        return cls()

    def request_shutdown(self) -> None:
        """Signal that shutdown has been requested."""
        self._shutdown_requested.set()
        logger.info("🛡️ NativeLibrarySafetyGuard shutdown requested")

    def is_shutdown_requested(self) -> bool:
        """Check if shutdown was requested."""
        return self._shutdown_requested.is_set()

    def _get_pytorch_executor(self) -> ThreadPoolExecutor:
        """Get or create the PyTorch executor with daemon threads."""
        with self._lock:
            if self._pytorch_executor is None:
                # v124.0: Use ManagedThreadPoolExecutor for daemon threads
                self._pytorch_executor = ManagedThreadPoolExecutor(
                    max_workers=self._pytorch_workers,
                    thread_name_prefix="PyTorchSafe",
                    name="PyTorchSafe",
                    daemon=True  # Ensure daemon threads for clean exit
                )
            return self._pytorch_executor

    @contextmanager
    def operation_context(self, library_type: NativeLibraryType):
        """
        Context manager for tracking active operations.

        Args:
            library_type: Type of native library being used

        Yields:
            bool: Whether operation should proceed (False if shutdown requested)
        """
        # Fast path: check shutdown first
        if self._shutdown_requested.is_set():
            yield False
            return

        # Register operation
        with self._operations_lock:
            if self._shutdown_requested.is_set():
                yield False
                return
            self._active_operations[library_type] += 1
            self._stats[library_type].total_operations += 1
            self._stats[library_type].last_operation_time = datetime.now()

        start_time = time.monotonic()
        success = False

        try:
            yield True
            success = True
            self._consecutive_errors[library_type] = 0
        except Exception as e:
            self._consecutive_errors[library_type] += 1
            self._stats[library_type].failed_operations += 1
            self._stats[library_type].last_error = str(e)

            if self._consecutive_errors[library_type] >= self._max_consecutive_errors:
                logger.error(
                    f"🛡️ {library_type.name}: {self._consecutive_errors[library_type]} "
                    f"consecutive errors, requesting shutdown"
                )
                self._shutdown_requested.set()
            raise
        finally:
            elapsed_ms = (time.monotonic() - start_time) * 1000

            with self._operations_lock:
                self._active_operations[library_type] -= 1
                if success:
                    self._stats[library_type].successful_operations += 1
                self._stats[library_type].total_time_ms += elapsed_ms
                self._stats[library_type].max_time_ms = max(
                    self._stats[library_type].max_time_ms,
                    elapsed_ms
                )

                # Notify waiters if no more operations
                if sum(self._active_operations.values()) == 0:
                    self._all_operations_done.notify_all()

    @contextmanager
    def callback_context(self, library_type: NativeLibraryType):
        """
        Context manager for callback-based APIs (PyAudio, SoundDevice).

        Same as operation_context but named for clarity in callback code.

        Usage:
            def audio_callback(indata, frames, time_info, status):
                with guard.callback_context(NativeLibraryType.PYAUDIO) as should_process:
                    if not should_process:
                        return  # Shutdown in progress
                    # Process audio...
        """
        with self.operation_context(library_type) as should_process:
            yield should_process

    async def execute_pytorch(
        self,
        func: Callable[[], T],
        timeout: Optional[float] = None,
        name: str = "pytorch_op"
    ) -> T:
        """
        Execute a PyTorch operation on the dedicated single-threaded executor.

        This ensures:
        - All PyTorch operations happen on the same thread
        - No GIL contention with model inference
        - Proper timeout handling without corrupting model state

        Args:
            func: The PyTorch operation to execute
            timeout: Timeout in seconds (default: 30.0)
            name: Operation name for logging

        Returns:
            Result of the function

        Raises:
            asyncio.TimeoutError: If operation times out
            RuntimeError: If shutdown was requested
            Exception: Any exception from the function
        """
        if self._shutdown_requested.is_set():
            raise RuntimeError("NativeLibrarySafetyGuard shutdown in progress")

        timeout = timeout or self._default_timeout
        executor = self._get_pytorch_executor()
        loop = asyncio.get_event_loop()

        def wrapped():
            with self.operation_context(NativeLibraryType.PYTORCH):
                return func()

        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(executor, wrapped),
                timeout=timeout
            )
            return result
        except asyncio.TimeoutError:
            logger.error(f"🛡️ PyTorch operation '{name}' timed out after {timeout}s")
            raise
        except Exception as e:
            logger.error(f"🛡️ PyTorch operation '{name}' failed: {e}")
            raise

    def execute_pytorch_sync(
        self,
        func: Callable[[], T],
        timeout: Optional[float] = None,
        name: str = "pytorch_op"
    ) -> T:
        """
        Synchronous version of execute_pytorch.

        For use in non-async contexts where you still need thread safety.
        """
        if self._shutdown_requested.is_set():
            raise RuntimeError("NativeLibrarySafetyGuard shutdown in progress")

        timeout = timeout or self._default_timeout
        executor = self._get_pytorch_executor()

        def wrapped():
            with self.operation_context(NativeLibraryType.PYTORCH):
                return func()

        future = executor.submit(wrapped)
        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            logger.error(f"🛡️ PyTorch operation '{name}' timed out after {timeout}s")
            future.cancel()
            raise

    async def execute_main_thread(
        self,
        func: Callable[[], T],
        timeout: Optional[float] = None,
        name: str = "main_thread_op"
    ) -> T:
        """
        Execute a function on the main thread.

        Required for:
        - AVFoundation (AVCaptureSession, etc.)
        - AppKit/Cocoa UI operations
        - Any API that requires main thread

        Args:
            func: Function to execute on main thread
            timeout: Timeout in seconds
            name: Operation name for logging

        Returns:
            Result of the function
        """
        if self._shutdown_requested.is_set():
            raise RuntimeError("NativeLibrarySafetyGuard shutdown in progress")

        timeout = timeout or self._default_timeout

        # If already on main thread, execute directly
        if threading.current_thread() is threading.main_thread():
            with self.operation_context(NativeLibraryType.AVFOUNDATION):
                return func()

        # Otherwise, schedule on main thread's event loop
        # This requires the main thread to be running an event loop
        try:
            main_loop = asyncio.get_running_loop()

            # Create a future to hold the result
            result_future: asyncio.Future = main_loop.create_future()

            def execute_and_set_result():
                try:
                    with self.operation_context(NativeLibraryType.AVFOUNDATION):
                        result = func()
                    main_loop.call_soon_threadsafe(
                        result_future.set_result, result
                    )
                except Exception as e:
                    main_loop.call_soon_threadsafe(
                        result_future.set_exception, e
                    )

            # Schedule on main thread
            main_loop.call_soon_threadsafe(execute_and_set_result)

            return await asyncio.wait_for(result_future, timeout=timeout)

        except Exception as e:
            logger.error(f"🛡️ Main thread operation '{name}' failed: {e}")
            raise

    def execute_numpy_safe(
        self,
        func: Callable[[], T],
        name: str = "numpy_op"
    ) -> T:
        """
        Execute a NumPy operation with protection.

        NumPy operations in callbacks can cause issues if the array
        memory is freed by another thread. This provides a safe context.

        Args:
            func: NumPy operation to execute
            name: Operation name for logging

        Returns:
            Result of the function
        """
        with self.operation_context(NativeLibraryType.NUMPY):
            return func()

    def wait_for_operations(self, timeout: float = 5.0) -> bool:
        """
        Wait for all active operations to complete.

        Call this during shutdown AFTER request_shutdown().

        Args:
            timeout: Maximum seconds to wait

        Returns:
            True if all operations completed, False if timeout
        """
        deadline = time.monotonic() + timeout

        with self._operations_lock:
            while sum(self._active_operations.values()) > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    active = dict(self._active_operations)
                    logger.warning(
                        f"🛡️ Timeout waiting for operations: {active}"
                    )
                    return False
                self._all_operations_done.wait(timeout=remaining)

        return True

    def cleanup(self) -> None:
        """Clean up all resources."""
        self.request_shutdown()

        # Wait for operations
        self.wait_for_operations(timeout=5.0)

        # Shutdown PyTorch executor
        with self._lock:
            if self._pytorch_executor is not None:
                self._pytorch_executor.shutdown(wait=True, cancel_futures=True)
                self._pytorch_executor = None

        logger.info("🛡️ NativeLibrarySafetyGuard cleanup complete")

    def get_stats(self) -> Dict[str, Any]:
        """Get statistics for all native library operations."""
        return {
            lib_type.name: {
                'total': stats.total_operations,
                'successful': stats.successful_operations,
                'failed': stats.failed_operations,
                'avg_time_ms': (
                    stats.total_time_ms / stats.successful_operations
                    if stats.successful_operations > 0 else 0
                ),
                'max_time_ms': stats.max_time_ms,
                'last_error': stats.last_error,
                'consecutive_errors': self._consecutive_errors[lib_type],
            }
            for lib_type, stats in self._stats.items()
        }


# Global instance getter
def get_native_library_guard() -> NativeLibrarySafetyGuard:
    """Get the global NativeLibrarySafetyGuard instance."""
    return NativeLibrarySafetyGuard.get_instance()


# =============================================================================
# SIGSEGV SIGNAL HANDLER - Last resort crash recovery
# =============================================================================

class SegfaultHandler:
    """
    Emergency handler for SIGSEGV signals.

    This provides:
    1. Graceful logging of crash information
    2. Stack trace capture
    3. Cleanup attempt before termination
    4. Crash dump for debugging

    Note: This is a last resort - proper fixes should prevent SIGSEGV.
    """

    _installed = False
    _original_handler = None

    @classmethod
    def install(cls) -> bool:
        """
        Install the SIGSEGV handler.

        Returns:
            True if installed successfully
        """
        if cls._installed:
            return True

        try:
            # Only in main thread
            if threading.current_thread() is not threading.main_thread():
                return False

            # Store original handler
            cls._original_handler = signal.signal(
                signal.SIGSEGV,
                cls._handle_sigsegv
            )

            cls._installed = True
            logger.info("🛡️ SIGSEGV handler installed")
            return True

        except Exception as e:
            logger.warning(f"Could not install SIGSEGV handler: {e}")
            return False

    @classmethod
    def _handle_sigsegv(cls, signum, frame):
        """Handle SIGSEGV signal."""
        try:
            # Get thread info
            thread = threading.current_thread()
            thread_id = thread.ident
            thread_name = thread.name

            # Log crash info
            print("\n" + "=" * 70, file=sys.stderr)
            print("🔥 CRITICAL: SIGSEGV (Segmentation Fault) detected!", file=sys.stderr)
            print("=" * 70, file=sys.stderr)
            print(f"Thread: {thread_name} (ID: {thread_id})", file=sys.stderr)
            print(f"Time: {datetime.now().isoformat()}", file=sys.stderr)

            # Try to get stack trace
            if frame:
                print("\nStack trace:", file=sys.stderr)
                traceback.print_stack(frame, file=sys.stderr)

            # Log native guard stats
            try:
                guard = get_native_library_guard()
                stats = guard.get_stats()
                print(f"\nNative library stats: {stats}", file=sys.stderr)
            except Exception:
                pass

            print("=" * 70, file=sys.stderr)
            print("Attempting graceful shutdown...", file=sys.stderr)

            # Try graceful shutdown
            try:
                guard = get_native_library_guard()
                guard.request_shutdown()
            except Exception:
                pass

        except Exception as e:
            print(f"Error in SIGSEGV handler: {e}", file=sys.stderr)

        finally:
            # Re-raise to let default handler terminate
            if cls._original_handler:
                signal.signal(signal.SIGSEGV, cls._original_handler)
            # Exit with error code
            sys.exit(139)  # 128 + 11 (SIGSEGV)

    @classmethod
    def uninstall(cls) -> None:
        """Uninstall the SIGSEGV handler."""
        if not cls._installed:
            return

        try:
            if cls._original_handler:
                signal.signal(signal.SIGSEGV, cls._original_handler)
            cls._installed = False
        except Exception:
            pass


# Auto-install SIGSEGV handler if enabled
if _env_bool('JARVIS_SIGSEGV_HANDLER', True):
    SegfaultHandler.install()
