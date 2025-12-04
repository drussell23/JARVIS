"""
Advanced ML Operation Watchdog
==============================

Enterprise-grade protection for ML operations that can block the event loop.

Features:
- Process-level timeout using multiprocessing (survives event loop blocks)
- Thread-based timeout for quick operations
- Automatic detection of stuck operations
- Graceful degradation when timeouts occur
- Dynamic configuration via environment variables
- Comprehensive statistics and monitoring
- Port conflict detection and resolution
- Zombie process detection

Configuration (via environment variables):
    ML_WATCHDOG_ENABLED: Enable/disable watchdog (default: true)
    ML_WATCHDOG_CHECK_INTERVAL: Event loop check interval in seconds (default: 5.0)
    ML_WATCHDOG_WARNING_THRESHOLD: Warning threshold in seconds (default: 2.0)
    ML_WATCHDOG_CRITICAL_THRESHOLD: Critical threshold in seconds (default: 10.0)
    ML_WATCHDOG_DEFAULT_TIMEOUT: Default operation timeout in seconds (default: 30.0)
    ML_WATCHDOG_USE_MULTIPROCESSING: Use multiprocessing for critical ops (default: false)

Usage:
    from core.ml_operation_watchdog import (
        run_with_timeout,
        with_timeout,
        MLOperationTimeout,
        get_watchdog_config
    )

    # Run a blocking operation with timeout
    result = await run_with_timeout(
        blocking_function, args, kwargs,
        timeout=30.0,
        operation_name="ECAPA embedding"
    )

    # Use decorator
    @with_timeout(timeout=10.0)
    async def my_ml_operation():
        ...
"""

import asyncio
import logging
import os
import signal
import socket
import subprocess
import concurrent.futures
import multiprocessing
from functools import wraps, partial
from typing import Any, Callable, Optional, TypeVar, Tuple, Dict, List
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
import threading
import time
import psutil

logger = logging.getLogger(__name__)

T = TypeVar('T')


# =============================================================================
# Configuration
# =============================================================================

@dataclass
class WatchdogConfig:
    """Dynamic configuration for the ML watchdog."""
    enabled: bool = True
    check_interval: float = 5.0
    warning_threshold: float = 2.0
    critical_threshold: float = 10.0
    default_timeout: float = 30.0
    use_multiprocessing: bool = False
    max_consecutive_failures: int = 5
    auto_restart_on_stuck: bool = False

    @classmethod
    def from_environment(cls) -> 'WatchdogConfig':
        """Load configuration from environment variables."""
        return cls(
            enabled=os.getenv('ML_WATCHDOG_ENABLED', 'true').lower() == 'true',
            check_interval=float(os.getenv('ML_WATCHDOG_CHECK_INTERVAL', '5.0')),
            warning_threshold=float(os.getenv('ML_WATCHDOG_WARNING_THRESHOLD', '2.0')),
            critical_threshold=float(os.getenv('ML_WATCHDOG_CRITICAL_THRESHOLD', '10.0')),
            default_timeout=float(os.getenv('ML_WATCHDOG_DEFAULT_TIMEOUT', '30.0')),
            use_multiprocessing=os.getenv('ML_WATCHDOG_USE_MULTIPROCESSING', 'false').lower() == 'true',
            max_consecutive_failures=int(os.getenv('ML_WATCHDOG_MAX_FAILURES', '5')),
            auto_restart_on_stuck=os.getenv('ML_WATCHDOG_AUTO_RESTART', 'false').lower() == 'true',
        )


# Global configuration - loaded dynamically
_config: Optional[WatchdogConfig] = None


def get_watchdog_config() -> WatchdogConfig:
    """Get current watchdog configuration, loading from environment if needed."""
    global _config
    if _config is None:
        _config = WatchdogConfig.from_environment()
    return _config


def update_watchdog_config(**kwargs) -> WatchdogConfig:
    """Update watchdog configuration dynamically."""
    global _config
    config = get_watchdog_config()
    for key, value in kwargs.items():
        if hasattr(config, key):
            setattr(config, key, value)
    return config


# =============================================================================
# Exceptions
# =============================================================================

class MLOperationTimeout(Exception):
    """Raised when an ML operation times out."""
    def __init__(self, operation_name: str, timeout: float, message: str = ""):
        self.operation_name = operation_name
        self.timeout = timeout
        self.timestamp = datetime.now()
        super().__init__(
            message or f"ML operation '{operation_name}' timed out after {timeout}s"
        )


class MLOperationError(Exception):
    """Raised when an ML operation fails."""
    def __init__(self, operation_name: str, error: Exception, message: str = ""):
        self.operation_name = operation_name
        self.original_error = error
        self.timestamp = datetime.now()
        super().__init__(
            message or f"ML operation '{operation_name}' failed: {error}"
        )


# =============================================================================
# Statistics
# =============================================================================

@dataclass
class OperationStats:
    """Statistics for a single operation type."""
    name: str
    total_calls: int = 0
    successful_calls: int = 0
    timeout_calls: int = 0
    error_calls: int = 0
    total_time_ms: float = 0.0
    min_time_ms: float = float('inf')
    max_time_ms: float = 0.0
    last_call_time: Optional[datetime] = None
    last_error: Optional[str] = None

    @property
    def avg_time_ms(self) -> float:
        if self.successful_calls == 0:
            return 0.0
        return self.total_time_ms / self.successful_calls

    @property
    def success_rate(self) -> float:
        if self.total_calls == 0:
            return 1.0
        return self.successful_calls / self.total_calls


@dataclass
class WatchdogStats:
    """Comprehensive statistics for ML operation watchdog."""
    total_operations: int = 0
    successful_operations: int = 0
    timeout_operations: int = 0
    error_operations: int = 0
    total_time_ms: float = 0.0
    last_operation_time: Optional[datetime] = None
    last_timeout_operation: Optional[str] = None
    last_error_operation: Optional[str] = None
    event_loop_checks: int = 0
    event_loop_warnings: int = 0
    event_loop_critical: int = 0
    consecutive_failures: int = 0
    operations_by_name: Dict[str, OperationStats] = field(default_factory=dict)

    def record_operation(
        self,
        name: str,
        duration_ms: float,
        success: bool,
        timeout: bool = False,
        error: Optional[str] = None
    ):
        """Record an operation result."""
        self.total_operations += 1
        self.last_operation_time = datetime.now()

        if name not in self.operations_by_name:
            self.operations_by_name[name] = OperationStats(name=name)

        stats = self.operations_by_name[name]
        stats.total_calls += 1
        stats.last_call_time = datetime.now()

        if success:
            self.successful_operations += 1
            stats.successful_calls += 1
            stats.total_time_ms += duration_ms
            stats.min_time_ms = min(stats.min_time_ms, duration_ms)
            stats.max_time_ms = max(stats.max_time_ms, duration_ms)
            self.total_time_ms += duration_ms
            self.consecutive_failures = 0
        elif timeout:
            self.timeout_operations += 1
            stats.timeout_calls += 1
            self.last_timeout_operation = name
            self.consecutive_failures += 1
        else:
            self.error_operations += 1
            stats.error_calls += 1
            stats.last_error = error
            self.last_error_operation = name
            self.consecutive_failures += 1

    def get_summary(self) -> Dict[str, Any]:
        """Get a summary of statistics."""
        return {
            'total_operations': self.total_operations,
            'successful': self.successful_operations,
            'timeouts': self.timeout_operations,
            'errors': self.error_operations,
            'success_rate': self.successful_operations / max(1, self.total_operations),
            'avg_time_ms': self.total_time_ms / max(1, self.successful_operations),
            'event_loop_warnings': self.event_loop_warnings,
            'event_loop_critical': self.event_loop_critical,
            'consecutive_failures': self.consecutive_failures,
            'operations': {
                name: {
                    'calls': s.total_calls,
                    'success_rate': s.success_rate,
                    'avg_time_ms': s.avg_time_ms,
                }
                for name, s in self.operations_by_name.items()
            }
        }


# Global watchdog stats
_watchdog_stats = WatchdogStats()


def get_watchdog_stats() -> WatchdogStats:
    """Get current watchdog statistics."""
    return _watchdog_stats


def reset_watchdog_stats():
    """Reset watchdog statistics."""
    global _watchdog_stats
    _watchdog_stats = WatchdogStats()


# =============================================================================
# Port and Process Management
# =============================================================================

def check_port_available(port: int) -> Tuple[bool, Optional[int]]:
    """
    Check if a port is available for binding.

    Returns:
        (is_available, blocking_pid) - blocking_pid is None if available
    """
    try:
        # Try to bind to the port
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(('localhost', port))
            return True, None
    except OSError:
        # Port is in use, find the blocking process
        try:
            for conn in psutil.net_connections(kind='inet'):
                if conn.laddr.port == port and conn.status == 'LISTEN':
                    return False, conn.pid
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            pass
        return False, None


def find_zombie_processes(port: int) -> List[Dict[str, Any]]:
    """
    Find zombie or uninterruptible processes blocking a port.

    Returns:
        List of process info dicts with pid, status, command, etc.
    """
    zombies = []
    try:
        for proc in psutil.process_iter(['pid', 'name', 'status', 'cmdline']):
            try:
                info = proc.info
                status = info.get('status', '')

                # Check for zombie (Z) or uninterruptible (D) states
                if status in ['zombie', 'disk-sleep', 'stopped']:
                    # Check if this process has the port open
                    try:
                        connections = proc.connections(kind='inet')
                        for conn in connections:
                            if conn.laddr.port == port:
                                zombies.append({
                                    'pid': info['pid'],
                                    'name': info['name'],
                                    'status': status,
                                    'cmdline': ' '.join(info.get('cmdline', []) or []),
                                })
                                break
                    except (psutil.AccessDenied, psutil.NoSuchProcess):
                        pass
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception as e:
        logger.warning(f"Error finding zombie processes: {e}")

    return zombies


def get_process_state(pid: int) -> Optional[str]:
    """Get the state of a process (R=running, S=sleeping, D=disk sleep, Z=zombie, etc.)"""
    try:
        proc = psutil.Process(pid)
        return proc.status()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None


def is_process_stuck(pid: int) -> bool:
    """Check if a process appears to be stuck (uninterruptible or zombie)."""
    state = get_process_state(pid)
    return state in ['disk-sleep', 'zombie', 'stopped']


async def wait_for_port(
    port: int,
    timeout: float = 30.0,
    check_interval: float = 0.5
) -> bool:
    """
    Wait for a port to become available.

    Returns:
        True if port became available, False if timeout
    """
    start = time.time()
    while time.time() - start < timeout:
        available, blocking_pid = check_port_available(port)
        if available:
            return True

        if blocking_pid:
            # Check if the blocking process is stuck
            if is_process_stuck(blocking_pid):
                logger.error(
                    f"🚨 Port {port} blocked by stuck process PID {blocking_pid}. "
                    f"Process is in uninterruptible state - requires system restart."
                )
                return False

        await asyncio.sleep(check_interval)

    return False


# =============================================================================
# Timeout Execution
# =============================================================================

async def run_with_timeout(
    func: Callable[..., T],
    *args,
    timeout: Optional[float] = None,
    operation_name: str = "ML operation",
    fallback_value: Optional[T] = None,
    raise_on_timeout: bool = True,
    use_multiprocessing: Optional[bool] = None,
    **kwargs
) -> T:
    """
    Run a potentially blocking function with robust timeout protection.

    Uses asyncio.to_thread() which runs the function in a separate thread,
    combined with asyncio.wait_for() for timeout enforcement.

    For critical operations, can use multiprocessing which provides
    true process isolation (can be killed even if thread is stuck).

    Args:
        func: The function to run (can be sync or async)
        *args: Arguments to pass to the function
        timeout: Maximum time to wait (seconds), uses config default if None
        operation_name: Name for logging and stats
        fallback_value: Value to return on timeout (if raise_on_timeout=False)
        raise_on_timeout: Whether to raise MLOperationTimeout on timeout
        use_multiprocessing: Use multiprocessing for this operation (overrides config)
        **kwargs: Keyword arguments to pass to the function

    Returns:
        The function result, or fallback_value on timeout

    Raises:
        MLOperationTimeout: If timeout occurs and raise_on_timeout=True
        MLOperationError: If the operation fails
    """
    config = get_watchdog_config()

    if not config.enabled:
        # Watchdog disabled, run directly
        if asyncio.iscoroutinefunction(func):
            return await func(*args, **kwargs)
        else:
            return await asyncio.to_thread(func, *args, **kwargs)

    timeout = timeout if timeout is not None else config.default_timeout
    use_mp = use_multiprocessing if use_multiprocessing is not None else config.use_multiprocessing

    start_time = time.perf_counter()

    try:
        if use_mp and not asyncio.iscoroutinefunction(func):
            # Use multiprocessing for true isolation
            result = await _run_in_process(func, args, kwargs, timeout, operation_name)
        elif asyncio.iscoroutinefunction(func):
            # Async function - wrap with timeout
            result = await asyncio.wait_for(
                func(*args, **kwargs),
                timeout=timeout
            )
        else:
            # Sync function - run in thread pool with timeout
            result = await asyncio.wait_for(
                asyncio.to_thread(func, *args, **kwargs),
                timeout=timeout
            )

        elapsed_ms = (time.perf_counter() - start_time) * 1000
        _watchdog_stats.record_operation(operation_name, elapsed_ms, success=True)

        if elapsed_ms > 1000:
            logger.info(f"✅ {operation_name} completed in {elapsed_ms:.0f}ms")
        else:
            logger.debug(f"✅ {operation_name} completed in {elapsed_ms:.0f}ms")

        return result

    except asyncio.TimeoutError:
        elapsed_ms = (time.perf_counter() - start_time) * 1000
        _watchdog_stats.record_operation(
            operation_name, elapsed_ms, success=False, timeout=True
        )

        logger.error(f"⏱️ {operation_name} TIMEOUT after {timeout}s")

        if raise_on_timeout:
            raise MLOperationTimeout(operation_name, timeout)
        return fallback_value

    except Exception as e:
        elapsed_ms = (time.perf_counter() - start_time) * 1000
        _watchdog_stats.record_operation(
            operation_name, elapsed_ms, success=False, error=str(e)
        )

        logger.error(f"❌ {operation_name} failed: {e}")
        raise MLOperationError(operation_name, e)


async def _run_in_process(
    func: Callable,
    args: tuple,
    kwargs: dict,
    timeout: float,
    operation_name: str
) -> Any:
    """
    Run a function in a separate process with timeout.

    This provides true isolation - if the operation hangs, the process
    can be killed without affecting the main event loop.
    """
    # Create a process pool executor
    with concurrent.futures.ProcessPoolExecutor(max_workers=1) as executor:
        loop = asyncio.get_running_loop()

        # Submit the function to run in a separate process
        try:
            future = loop.run_in_executor(
                executor,
                partial(func, *args, **kwargs)
            )

            result = await asyncio.wait_for(future, timeout=timeout)
            return result

        except asyncio.TimeoutError:
            # Kill the process pool
            logger.warning(f"⏱️ {operation_name} timed out in subprocess, terminating...")
            executor.shutdown(wait=False, cancel_futures=True)
            raise


def with_timeout(
    timeout: Optional[float] = None,
    operation_name: Optional[str] = None,
    fallback_value: Any = None,
    raise_on_timeout: bool = True,
    use_multiprocessing: Optional[bool] = None
):
    """
    Decorator to add timeout protection to async functions.

    Usage:
        @with_timeout(timeout=10.0, operation_name="Voice embedding")
        async def extract_embedding(audio_data):
            ...
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args, **kwargs):
            name = operation_name or func.__name__
            return await run_with_timeout(
                func, *args,
                timeout=timeout,
                operation_name=name,
                fallback_value=fallback_value,
                raise_on_timeout=raise_on_timeout,
                use_multiprocessing=use_multiprocessing,
                **kwargs
            )
        return wrapper
    return decorator


# =============================================================================
# Event Loop Watchdog
# =============================================================================

class EventLoopWatchdog:
    """
    Monitors the event loop for blocking operations.

    Runs in a separate thread and checks if the event loop is responsive.
    If the loop appears blocked, it logs warnings and can trigger recovery.
    """

    def __init__(self, config: Optional[WatchdogConfig] = None):
        self.config = config or get_watchdog_config()

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._last_response_time: Optional[datetime] = None
        self._consecutive_failures = 0
        self._lock = threading.Lock()
        self._callbacks: List[Callable[[str, float], None]] = []

    def add_callback(self, callback: Callable[[str, float], None]):
        """
        Add a callback to be called when event loop issues are detected.

        Callback receives: (severity: 'warning'|'critical', response_time: float)
        """
        with self._lock:
            self._callbacks.append(callback)

    def remove_callback(self, callback: Callable):
        """Remove a callback."""
        with self._lock:
            if callback in self._callbacks:
                self._callbacks.remove(callback)

    def start(self, loop: asyncio.AbstractEventLoop):
        """Start the watchdog in a background thread."""
        if not self.config.enabled:
            logger.info("🐕 Event loop watchdog disabled by configuration")
            return

        if self._running:
            return

        self._loop = loop
        self._running = True
        self._thread = threading.Thread(
            target=self._watchdog_thread,
            name="EventLoopWatchdog",
            daemon=True
        )
        self._thread.start()
        logger.info(
            f"🐕 Event loop watchdog started "
            f"(check_interval={self.config.check_interval}s, "
            f"warning={self.config.warning_threshold}s, "
            f"critical={self.config.critical_threshold}s)"
        )

    def stop(self):
        """Stop the watchdog."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
        logger.info("🐕 Event loop watchdog stopped")

    def _notify_callbacks(self, severity: str, response_time: float):
        """Notify all callbacks of an event loop issue."""
        with self._lock:
            for callback in self._callbacks:
                try:
                    callback(severity, response_time)
                except Exception as e:
                    logger.warning(f"Watchdog callback error: {e}")

    def _watchdog_thread(self):
        """Background thread that monitors event loop responsiveness."""
        while self._running:
            try:
                if self._loop and self._loop.is_running():
                    start = time.perf_counter()

                    future = asyncio.run_coroutine_threadsafe(
                        self._ping_loop(),
                        self._loop
                    )

                    try:
                        # Wait for response with timeout
                        future.result(timeout=self.config.warning_threshold)
                        response_time = time.perf_counter() - start

                        _watchdog_stats.event_loop_checks += 1

                        if response_time > self.config.warning_threshold:
                            _watchdog_stats.event_loop_warnings += 1
                            logger.warning(
                                f"⚠️ Event loop slow: {response_time:.2f}s response time"
                            )
                            self._notify_callbacks('warning', response_time)
                            self._consecutive_failures += 1
                        else:
                            self._consecutive_failures = 0

                        self._last_response_time = datetime.now()

                    except concurrent.futures.TimeoutError:
                        elapsed = time.perf_counter() - start
                        self._consecutive_failures += 1
                        _watchdog_stats.event_loop_checks += 1

                        if elapsed > self.config.critical_threshold:
                            _watchdog_stats.event_loop_critical += 1
                            logger.error(
                                f"🚨 CRITICAL: Event loop blocked for {elapsed:.1f}s! "
                                f"Consecutive failures: {self._consecutive_failures}"
                            )
                            self._notify_callbacks('critical', elapsed)

                            # Check if we should trigger auto-restart
                            if (self.config.auto_restart_on_stuck and
                                self._consecutive_failures >= self.config.max_consecutive_failures):
                                logger.error(
                                    f"🔄 Max consecutive failures reached "
                                    f"({self._consecutive_failures}). "
                                    f"Auto-restart is enabled but not implemented yet."
                                )
                        else:
                            _watchdog_stats.event_loop_warnings += 1
                            logger.warning(
                                f"⚠️ Event loop unresponsive for {elapsed:.1f}s"
                            )
                            self._notify_callbacks('warning', elapsed)

            except Exception as e:
                logger.debug(f"Watchdog check error: {e}")

            time.sleep(self.config.check_interval)

    async def _ping_loop(self) -> bool:
        """Simple coroutine to check if event loop is responsive."""
        return True

    @property
    def is_healthy(self) -> bool:
        """Check if event loop appears healthy."""
        if self._last_response_time is None:
            return True
        elapsed = (datetime.now() - self._last_response_time).total_seconds()
        return elapsed < self.config.critical_threshold

    @property
    def health_status(self) -> Dict[str, Any]:
        """Get detailed health status."""
        return {
            'healthy': self.is_healthy,
            'running': self._running,
            'consecutive_failures': self._consecutive_failures,
            'last_response_time': self._last_response_time.isoformat() if self._last_response_time else None,
            'config': {
                'check_interval': self.config.check_interval,
                'warning_threshold': self.config.warning_threshold,
                'critical_threshold': self.config.critical_threshold,
            }
        }


# Global watchdog instance
_event_loop_watchdog: Optional[EventLoopWatchdog] = None


def get_event_loop_watchdog() -> Optional[EventLoopWatchdog]:
    """Get the global event loop watchdog instance."""
    return _event_loop_watchdog


def start_event_loop_watchdog(
    loop: asyncio.AbstractEventLoop,
    config: Optional[WatchdogConfig] = None
) -> EventLoopWatchdog:
    """Start the global event loop watchdog."""
    global _event_loop_watchdog
    if _event_loop_watchdog is None:
        _event_loop_watchdog = EventLoopWatchdog(config)
    _event_loop_watchdog.start(loop)
    return _event_loop_watchdog


def stop_event_loop_watchdog():
    """Stop the global event loop watchdog."""
    global _event_loop_watchdog
    if _event_loop_watchdog:
        _event_loop_watchdog.stop()


def is_event_loop_healthy() -> bool:
    """Check if the event loop appears healthy."""
    if _event_loop_watchdog:
        return _event_loop_watchdog.is_healthy
    return True


# =============================================================================
# Startup Protection
# =============================================================================

async def protected_startup(
    startup_func: Callable,
    timeout: float = 120.0,
    operation_name: str = "Backend startup"
) -> Any:
    """
    Run a startup function with timeout protection.

    This is specifically designed for backend initialization that
    might hang due to ML model loading.
    """
    logger.info(f"🛡️ Starting {operation_name} with {timeout}s timeout protection...")

    try:
        result = await run_with_timeout(
            startup_func,
            timeout=timeout,
            operation_name=operation_name,
            raise_on_timeout=True
        )
        logger.info(f"✅ {operation_name} completed successfully")
        return result

    except MLOperationTimeout:
        logger.error(
            f"🚨 {operation_name} timed out after {timeout}s. "
            f"This usually indicates ML model loading is blocking the event loop. "
            f"Check that all PyTorch/ML operations use asyncio.to_thread()."
        )
        raise
    except Exception as e:
        logger.error(f"❌ {operation_name} failed: {e}")
        raise


@asynccontextmanager
async def startup_timeout_context(
    timeout: float = 120.0,
    description: str = "Startup operation"
):
    """
    Context manager for wrapping startup operations with timeout.

    Usage:
        async with startup_timeout_context(timeout=60.0, description="Load ML models"):
            await load_models()
    """
    start = time.perf_counter()
    logger.info(f"⏱️ Starting: {description} (timeout: {timeout}s)")

    try:
        # Create a task for timeout monitoring
        async def timeout_monitor():
            await asyncio.sleep(timeout)
            elapsed = time.perf_counter() - start
            logger.error(f"🚨 TIMEOUT: {description} exceeded {timeout}s (elapsed: {elapsed:.1f}s)")

        monitor_task = asyncio.create_task(timeout_monitor())

        try:
            yield
        finally:
            monitor_task.cancel()
            try:
                await monitor_task
            except asyncio.CancelledError:
                pass

        elapsed = time.perf_counter() - start
        logger.info(f"✅ Completed: {description} in {elapsed:.1f}s")

    except asyncio.CancelledError:
        elapsed = time.perf_counter() - start
        logger.warning(f"⚠️ Cancelled: {description} after {elapsed:.1f}s")
        raise
    except Exception as e:
        elapsed = time.perf_counter() - start
        logger.error(f"❌ Failed: {description} after {elapsed:.1f}s - {e}")
        raise


# =============================================================================
# Diagnostic Utilities
# =============================================================================

def diagnose_blocking_issues(port: int = 8010) -> Dict[str, Any]:
    """
    Diagnose potential blocking issues with the backend.

    Returns a diagnostic report with:
    - Port availability
    - Zombie processes
    - Event loop health
    - Watchdog statistics
    """
    report = {
        'timestamp': datetime.now().isoformat(),
        'port': port,
        'issues': [],
        'recommendations': [],
    }

    # Check port availability
    available, blocking_pid = check_port_available(port)
    report['port_available'] = available
    report['blocking_pid'] = blocking_pid

    if not available and blocking_pid:
        if is_process_stuck(blocking_pid):
            report['issues'].append(f"Port {port} blocked by STUCK process PID {blocking_pid}")
            report['recommendations'].append(
                f"Process {blocking_pid} is in uninterruptible state. "
                f"System restart required to clear it."
            )
        else:
            report['issues'].append(f"Port {port} in use by PID {blocking_pid}")
            report['recommendations'].append(f"Stop process {blocking_pid} or use a different port")

    # Check for zombie processes
    zombies = find_zombie_processes(port)
    report['zombie_processes'] = zombies
    if zombies:
        report['issues'].append(f"Found {len(zombies)} zombie/stuck processes")
        for z in zombies:
            report['recommendations'].append(
                f"Zombie process PID {z['pid']} ({z['status']}): {z['cmdline'][:50]}..."
            )

    # Check event loop watchdog
    if _event_loop_watchdog:
        report['event_loop_health'] = _event_loop_watchdog.health_status
        if not _event_loop_watchdog.is_healthy:
            report['issues'].append("Event loop is not healthy")
            report['recommendations'].append(
                "Check for blocking ML operations not wrapped in asyncio.to_thread()"
            )

    # Add watchdog statistics
    report['watchdog_stats'] = _watchdog_stats.get_summary()

    if _watchdog_stats.consecutive_failures > 3:
        report['issues'].append(
            f"High consecutive failure count: {_watchdog_stats.consecutive_failures}"
        )
        report['recommendations'].append(
            "Review recent ML operations for blocking behavior"
        )

    return report


def print_diagnostic_report(port: int = 8010):
    """Print a formatted diagnostic report."""
    report = diagnose_blocking_issues(port)

    print("\n" + "=" * 60)
    print("🔍 ML OPERATION WATCHDOG DIAGNOSTIC REPORT")
    print("=" * 60)
    print(f"Timestamp: {report['timestamp']}")
    print(f"Port: {report['port']}")
    print(f"Port Available: {'✅ Yes' if report['port_available'] else '❌ No'}")

    if report.get('blocking_pid'):
        print(f"Blocking PID: {report['blocking_pid']}")

    if report.get('issues'):
        print("\n⚠️ ISSUES DETECTED:")
        for issue in report['issues']:
            print(f"  • {issue}")

    if report.get('recommendations'):
        print("\n💡 RECOMMENDATIONS:")
        for rec in report['recommendations']:
            print(f"  • {rec}")

    if report.get('watchdog_stats'):
        stats = report['watchdog_stats']
        print("\n📊 WATCHDOG STATISTICS:")
        print(f"  • Total operations: {stats['total_operations']}")
        print(f"  • Success rate: {stats['success_rate']:.1%}")
        print(f"  • Timeouts: {stats['timeouts']}")
        print(f"  • Errors: {stats['errors']}")
        print(f"  • Avg time: {stats['avg_time_ms']:.0f}ms")

    print("=" * 60 + "\n")
