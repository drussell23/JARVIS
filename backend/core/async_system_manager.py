#!/usr/bin/env python3
"""
v111.0: Unified Monolith - AsyncSystemManager
=============================================

Enterprise-grade system lifecycle manager for JARVIS.
This module can be imported without side effects and manages
the FastAPI backend lifecycle within the supervisor's event loop.

Key Features:
- Import-safe (no event loop access during import)
- Async-safe (handles running event loop scenarios)
- Graceful shutdown with configurable timeout
- Callback system for lifecycle events (sync + async)
- Health tracking for all managed components
- Thread-safe singleton pattern
- Uvicorn in-process with disabled signal handlers

Architecture:
    ┌─────────────────────────────────────────────────────────────────┐
    │                    AsyncSystemManager                           │
    │  ┌───────────────┐  ┌───────────────┐  ┌───────────────┐        │
    │  │   Lifecycle   │  │    Health     │  │   Callback    │        │
    │  │   State       │  │    Tracker    │  │   Registry    │        │
    │  └───────┬───────┘  └───────┬───────┘  └───────┬───────┘        │
    │          │                  │                  │                │
    │          └──────────────────┼──────────────────┘                │
    │                             │                                   │
    │                    ┌────────▼────────┐                          │
    │                    │ Uvicorn Server  │                          │
    │                    │  (in-process)   │                          │
    │                    │  - No signals   │                          │
    │                    │  - Graceful     │                          │
    │                    └─────────────────┘                          │
    └─────────────────────────────────────────────────────────────────┘

Critical Design Decisions:
1. NO asyncio.Lock() at module level - uses threading.Lock instead
2. NO asyncio.get_event_loop() outside async functions
3. Uses time.monotonic() for elapsed time (NOT loop.time())
4. Uvicorn signal handlers DISABLED - supervisor manages signals
5. Thread-safe singleton with double-checked locking

Author: JARVIS System
Version: 111.0.0 (January 2026)
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Optional,
    Tuple,
    TypeVar,
    Union,
)

# =============================================================================
# CRITICAL: No event loop access during module load
# =============================================================================
# Use time.monotonic() for elapsed time calculations, NOT asyncio.get_event_loop().time()
# This is import-safe as it doesn't require an event loop

logger = logging.getLogger(__name__)

# Type for callback functions
CallbackT = Union[Callable[[], None], Callable[[], Awaitable[None]]]
T = TypeVar("T")


# =============================================================================
# Environment-Driven Configuration (Zero Hardcoding)
# =============================================================================

def _env_float(key: str, default: float) -> float:
    """Get float from environment with default fallback."""
    try:
        return float(os.getenv(key, str(default)))
    except ValueError:
        return default


def _env_int(key: str, default: int) -> int:
    """Get int from environment with default fallback."""
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    """Get bool from environment with default fallback."""
    return os.getenv(key, str(default)).lower() in ("true", "1", "yes", "on")


class SystemManagerConfig:
    """
    Environment-driven configuration for AsyncSystemManager.

    All values configurable via environment variables.
    """
    # Timeouts
    STARTUP_TIMEOUT: float = _env_float("SYSTEM_STARTUP_TIMEOUT", 120.0)
    SHUTDOWN_TIMEOUT: float = _env_float("SYSTEM_SHUTDOWN_TIMEOUT", 30.0)
    CALLBACK_TIMEOUT: float = _env_float("SYSTEM_CALLBACK_TIMEOUT", 10.0)
    HEALTH_CHECK_INTERVAL: float = _env_float("SYSTEM_HEALTH_INTERVAL", 5.0)
    SERVER_READY_TIMEOUT: float = _env_float("SYSTEM_SERVER_READY_TIMEOUT", 30.0)

    # Server configuration
    HOST: str = os.getenv("JARVIS_HOST", "0.0.0.0")
    PORT: int = _env_int("JARVIS_PORT", 8000)
    LOG_LEVEL: str = os.getenv("JARVIS_LOG_LEVEL", "info")
    WORKERS: int = _env_int("JARVIS_WORKERS", 1)

    # Feature flags
    ENABLE_HEALTH_MONITOR: bool = _env_bool("SYSTEM_ENABLE_HEALTH_MONITOR", True)
    ENABLE_METRICS: bool = _env_bool("SYSTEM_ENABLE_METRICS", True)
    GRACEFUL_SHUTDOWN: bool = _env_bool("SYSTEM_GRACEFUL_SHUTDOWN", True)

    # App path
    APP_MODULE: str = os.getenv("JARVIS_APP_MODULE", "backend.main:app")


# =============================================================================
# System Phase Enum
# =============================================================================

class SystemPhase(str, Enum):
    """
    System lifecycle phases.

    State transitions:
        INIT -> STARTING -> RUNNING -> SHUTTING_DOWN -> STOPPED
                   └──────────────────────────────────-> FAILED
    """
    INIT = "init"                   # Manager created, no activity yet
    STARTING = "starting"           # Server startup in progress
    RUNNING = "running"             # Server accepting requests
    SHUTTING_DOWN = "shutting_down" # Graceful shutdown in progress
    STOPPED = "stopped"             # Cleanly stopped
    FAILED = "failed"               # Failed to start or crashed


# =============================================================================
# System State Dataclass
# =============================================================================

@dataclass
class SystemState:
    """
    Immutable snapshot of current system state.

    This is a value object that captures the system state at a point in time.
    Use for health reporting, logging, and external status queries.
    """
    phase: SystemPhase = SystemPhase.INIT
    started_at: Optional[datetime] = None
    stopped_at: Optional[datetime] = None
    uptime_seconds: float = 0.0
    services_healthy: Dict[str, bool] = field(default_factory=dict)
    last_health_check: Optional[datetime] = None
    error: Optional[str] = None
    shutdown_reason: Optional[str] = None
    request_count: int = 0
    active_connections: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """Convert to JSON-serializable dictionary."""
        return {
            "phase": self.phase.value,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "stopped_at": self.stopped_at.isoformat() if self.stopped_at else None,
            "uptime_seconds": round(self.uptime_seconds, 2),
            "services_healthy": self.services_healthy.copy(),
            "last_health_check": self.last_health_check.isoformat() if self.last_health_check else None,
            "error": self.error,
            "shutdown_reason": self.shutdown_reason,
            "request_count": self.request_count,
            "active_connections": self.active_connections,
        }

    @property
    def is_healthy(self) -> bool:
        """Check if all tracked services are healthy."""
        if not self.services_healthy:
            return self.phase == SystemPhase.RUNNING
        return all(self.services_healthy.values())

    @property
    def healthy_services_count(self) -> int:
        """Count of healthy services."""
        return sum(1 for v in self.services_healthy.values() if v)

    @property
    def unhealthy_services(self) -> List[str]:
        """List of unhealthy service names."""
        return [k for k, v in self.services_healthy.items() if not v]


# =============================================================================
# Callback Container
# =============================================================================

@dataclass
class RegisteredCallback:
    """Container for registered lifecycle callbacks."""
    callback: CallbackT
    name: str
    priority: int = 50  # Lower = run first (0-100)
    timeout: float = 10.0
    is_async: bool = False
    registered_at: float = field(default_factory=time.monotonic)

    def __post_init__(self):
        """Detect if callback is async."""
        self.is_async = asyncio.iscoroutinefunction(self.callback)


# =============================================================================
# AsyncSystemManager - Main Class
# =============================================================================

class AsyncSystemManager:
    """
    v111.0: Enterprise-grade async system manager.

    This class manages the JARVIS backend lifecycle and can be
    imported and used without side effects until start() is called.

    Key Features:
    - Import-safe: No event loop access during __init__
    - Thread-safe: Uses threading.Lock for synchronization
    - Async-safe: Handles "already running event loop" scenarios
    - Callback system: Register sync or async callbacks for lifecycle events
    - Health tracking: Monitor service health status
    - Graceful shutdown: Configurable timeout, proper task cancellation

    Usage:
        manager = get_system_manager()

        # Register callbacks
        manager.on_start(lambda: print("Starting!"))
        manager.on_stop(cleanup_resources)

        # Start the server (blocks until shutdown)
        await manager.start()

        # Or wait for shutdown signal
        await manager.wait_for_shutdown()

        # Graceful stop
        await manager.stop()

    Singleton Pattern:
        Use get_system_manager() to get the singleton instance.
        Use reset_system_manager() to reset (for testing only).
    """

    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        app_module: Optional[str] = None,
        log_level: Optional[str] = None,
        **kwargs,
    ):
        """
        Initialize AsyncSystemManager.

        CRITICAL: This method MUST NOT access asyncio event loop.
        All async primitives are created lazily when needed.

        Args:
            host: Server host (default from env JARVIS_HOST)
            port: Server port (default from env JARVIS_PORT)
            app_module: ASGI app module path (default from env JARVIS_APP_MODULE)
            log_level: Uvicorn log level (default from env JARVIS_LOG_LEVEL)
            **kwargs: Additional Uvicorn config options
        """
        # Configuration (all from environment if not provided)
        self._host = host or SystemManagerConfig.HOST
        self._port = port or SystemManagerConfig.PORT
        self._app_module = app_module or SystemManagerConfig.APP_MODULE
        self._log_level = log_level or SystemManagerConfig.LOG_LEVEL
        self._extra_config = kwargs

        # State tracking (no async primitives here!)
        self._phase = SystemPhase.INIT
        self._started_at: Optional[datetime] = None
        self._stopped_at: Optional[datetime] = None
        self._start_monotonic: Optional[float] = None
        self._error: Optional[str] = None
        self._shutdown_reason: Optional[str] = None

        # Health tracking
        self._services_health: Dict[str, bool] = {}
        self._last_health_check: Optional[datetime] = None
        self._request_count = 0
        self._active_connections = 0

        # Callback registries
        self._start_callbacks: List[RegisteredCallback] = []
        self._stop_callbacks: List[RegisteredCallback] = []

        # Thread-safe locks (NOT asyncio.Lock - that requires event loop!)
        self._state_lock = threading.Lock()
        self._callback_lock = threading.Lock()

        # Lazy-initialized async primitives (created when first needed in async context)
        self._shutdown_event: Optional[asyncio.Event] = None
        self._server_ready_event: Optional[asyncio.Event] = None
        self._async_lock: Optional[asyncio.Lock] = None

        # Uvicorn server instance
        self._server: Optional[Any] = None  # uvicorn.Server
        self._server_task: Optional[asyncio.Task] = None

        # Health monitor task
        self._health_monitor_task: Optional[asyncio.Task] = None

        # Shutdown flag (thread-safe)
        self._shutdown_requested = threading.Event()

        logger.debug(f"[AsyncSystemManager] Initialized (host={self._host}, port={self._port})")

    # =========================================================================
    # Lazy Async Primitive Access
    # =========================================================================

    def _get_shutdown_event(self) -> asyncio.Event:
        """Get or create shutdown event (lazy initialization)."""
        if self._shutdown_event is None:
            self._shutdown_event = asyncio.Event()
        return self._shutdown_event

    def _get_server_ready_event(self) -> asyncio.Event:
        """Get or create server ready event (lazy initialization)."""
        if self._server_ready_event is None:
            self._server_ready_event = asyncio.Event()
        return self._server_ready_event

    async def _get_async_lock(self) -> asyncio.Lock:
        """Get or create async lock (lazy initialization)."""
        if self._async_lock is None:
            self._async_lock = asyncio.Lock()
        return self._async_lock

    # =========================================================================
    # State Properties
    # =========================================================================

    @property
    def phase(self) -> SystemPhase:
        """Get current system phase (thread-safe)."""
        with self._state_lock:
            return self._phase

    @property
    def is_running(self) -> bool:
        """Check if system is in RUNNING phase."""
        return self.phase == SystemPhase.RUNNING

    @property
    def is_starting(self) -> bool:
        """Check if system is starting."""
        return self.phase == SystemPhase.STARTING

    @property
    def is_shutting_down(self) -> bool:
        """Check if system is shutting down."""
        return self.phase == SystemPhase.SHUTTING_DOWN

    @property
    def is_stopped(self) -> bool:
        """Check if system is stopped."""
        return self.phase in (SystemPhase.STOPPED, SystemPhase.FAILED)

    @property
    def uptime_seconds(self) -> float:
        """Get uptime in seconds (thread-safe, uses monotonic time)."""
        with self._state_lock:
            if self._start_monotonic is None:
                return 0.0
            if self._stopped_at is not None:
                # Use stored duration if stopped
                return (self._stopped_at - self._started_at).total_seconds() if self._started_at else 0.0
            return time.monotonic() - self._start_monotonic

    @property
    def state(self) -> SystemState:
        """Get immutable snapshot of current system state."""
        with self._state_lock:
            return SystemState(
                phase=self._phase,
                started_at=self._started_at,
                stopped_at=self._stopped_at,
                uptime_seconds=self.uptime_seconds,
                services_healthy=self._services_health.copy(),
                last_health_check=self._last_health_check,
                error=self._error,
                shutdown_reason=self._shutdown_reason,
                request_count=self._request_count,
                active_connections=self._active_connections,
            )

    # =========================================================================
    # Callback Registration
    # =========================================================================

    def on_start(
        self,
        callback: CallbackT,
        name: Optional[str] = None,
        priority: int = 50,
        timeout: float = 10.0,
    ) -> None:
        """
        Register a callback to run when the system starts.

        Callbacks are executed in priority order (lower = first).
        Both sync and async callbacks are supported.

        Args:
            callback: Function to call (sync or async)
            name: Optional name for logging
            priority: Execution priority (0-100, lower = first)
            timeout: Maximum execution time for callback
        """
        cb_name = name or getattr(callback, "__name__", str(callback))
        registered = RegisteredCallback(
            callback=callback,
            name=cb_name,
            priority=priority,
            timeout=timeout,
        )

        with self._callback_lock:
            self._start_callbacks.append(registered)
            self._start_callbacks.sort(key=lambda x: x.priority)

        logger.debug(f"[AsyncSystemManager] Registered start callback: {cb_name} (priority={priority})")

    def on_stop(
        self,
        callback: CallbackT,
        name: Optional[str] = None,
        priority: int = 50,
        timeout: float = 10.0,
    ) -> None:
        """
        Register a callback to run when the system stops.

        Stop callbacks are executed in REVERSE priority order (higher = first).
        This ensures resources are cleaned up in dependency order.

        Args:
            callback: Function to call (sync or async)
            name: Optional name for logging
            priority: Execution priority (0-100, higher = first for stop)
            timeout: Maximum execution time for callback
        """
        cb_name = name or getattr(callback, "__name__", str(callback))
        registered = RegisteredCallback(
            callback=callback,
            name=cb_name,
            priority=priority,
            timeout=timeout,
        )

        with self._callback_lock:
            self._stop_callbacks.append(registered)
            # Sort by reverse priority for stop callbacks
            self._stop_callbacks.sort(key=lambda x: -x.priority)

        logger.debug(f"[AsyncSystemManager] Registered stop callback: {cb_name} (priority={priority})")

    async def _run_callbacks(
        self,
        callbacks: List[RegisteredCallback],
        callback_type: str,
    ) -> List[Tuple[str, Optional[Exception]]]:
        """
        Run registered callbacks with timeout protection.

        Args:
            callbacks: List of callbacks to run
            callback_type: "start" or "stop" for logging

        Returns:
            List of (name, exception) tuples for any failed callbacks
        """
        results: List[Tuple[str, Optional[Exception]]] = []

        with self._callback_lock:
            callbacks_copy = callbacks.copy()

        for cb in callbacks_copy:
            try:
                logger.debug(f"[AsyncSystemManager] Running {callback_type} callback: {cb.name}")

                if cb.is_async:
                    await asyncio.wait_for(
                        cb.callback(),  # type: ignore
                        timeout=cb.timeout,
                    )
                else:
                    # Run sync callback in executor to not block
                    loop = asyncio.get_running_loop()
                    await asyncio.wait_for(
                        loop.run_in_executor(None, cb.callback),
                        timeout=cb.timeout,
                    )

                results.append((cb.name, None))
                logger.debug(f"[AsyncSystemManager] ✅ {callback_type} callback completed: {cb.name}")

            except asyncio.TimeoutError:
                error = TimeoutError(f"Callback {cb.name} timed out after {cb.timeout}s")
                results.append((cb.name, error))
                logger.warning(f"[AsyncSystemManager] ⏱️ {callback_type} callback timed out: {cb.name}")

            except asyncio.CancelledError as ce:
                # Cast to Exception for type compatibility
                results.append((cb.name, Exception(f"Cancelled: {ce}")))
                logger.warning(f"[AsyncSystemManager] 🚫 {callback_type} callback cancelled: {cb.name}")
                raise  # Re-raise CancelledError

            except Exception as e:
                results.append((cb.name, e))
                logger.error(
                    f"[AsyncSystemManager] ❌ {callback_type} callback failed: {cb.name}",
                    exc_info=True,
                )

        return results

    # =========================================================================
    # Health Tracking
    # =========================================================================

    def update_service_health(self, service_name: str, is_healthy: bool) -> None:
        """
        Update health status for a service (thread-safe).

        Args:
            service_name: Name of the service
            is_healthy: Whether the service is healthy
        """
        with self._state_lock:
            self._services_health[service_name] = is_healthy
            self._last_health_check = datetime.now()

    def get_service_health(self, service_name: str) -> Optional[bool]:
        """Get health status for a service."""
        with self._state_lock:
            return self._services_health.get(service_name)

    def increment_request_count(self) -> None:
        """Increment request counter (thread-safe)."""
        with self._state_lock:
            self._request_count += 1

    def update_active_connections(self, delta: int) -> None:
        """Update active connection count (thread-safe)."""
        with self._state_lock:
            self._active_connections = max(0, self._active_connections + delta)

    async def _health_monitor_loop(self) -> None:
        """Background health monitoring loop."""
        interval = SystemManagerConfig.HEALTH_CHECK_INTERVAL

        while not self._shutdown_requested.is_set():
            try:
                # Update health check timestamp
                with self._state_lock:
                    self._last_health_check = datetime.now()

                # Check if server is still responsive
                if self._server and hasattr(self._server, 'started'):
                    self.update_service_health("uvicorn", self._server.started)

                # Wait for next check or shutdown
                try:
                    await asyncio.wait_for(
                        self._get_shutdown_event().wait(),
                        timeout=interval,
                    )
                    break  # Shutdown requested
                except asyncio.TimeoutError:
                    pass  # Normal timeout, continue monitoring

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[AsyncSystemManager] Health monitor error: {e}")
                await asyncio.sleep(interval)

    # =========================================================================
    # Phase Transitions
    # =========================================================================

    def _transition_to(self, new_phase: SystemPhase, reason: Optional[str] = None) -> None:
        """
        Transition to a new phase (thread-safe).

        Args:
            new_phase: Target phase
            reason: Optional reason for transition
        """
        with self._state_lock:
            old_phase = self._phase
            self._phase = new_phase

            if new_phase == SystemPhase.STARTING:
                self._started_at = datetime.now()
                self._start_monotonic = time.monotonic()

            elif new_phase in (SystemPhase.STOPPED, SystemPhase.FAILED):
                self._stopped_at = datetime.now()
                if reason:
                    self._shutdown_reason = reason

            if new_phase == SystemPhase.FAILED and reason:
                self._error = reason

        logger.info(f"[AsyncSystemManager] Phase: {old_phase.value} -> {new_phase.value}" +
                   (f" ({reason})" if reason else ""))

    # =========================================================================
    # Server Lifecycle
    # =========================================================================

    async def start(self) -> None:
        """
        Start the Uvicorn server in-process.

        This method:
        1. Transitions to STARTING phase
        2. Runs start callbacks
        3. Creates Uvicorn server with DISABLED signal handlers
        4. Starts server in background task
        5. Waits for server to be ready
        6. Transitions to RUNNING phase
        7. Starts health monitor

        The server runs until stop() is called or shutdown is signaled.

        Raises:
            RuntimeError: If already running or failed to start
        """
        if self._phase not in (SystemPhase.INIT, SystemPhase.STOPPED, SystemPhase.FAILED):
            raise RuntimeError(f"Cannot start from phase {self._phase.value}")

        self._transition_to(SystemPhase.STARTING)
        self._shutdown_requested.clear()

        # Reset lazy async primitives for fresh start
        self._shutdown_event = None
        self._server_ready_event = None

        try:
            # Run start callbacks
            logger.info("[AsyncSystemManager] Running start callbacks...")
            callback_results = await self._run_callbacks(self._start_callbacks, "start")

            failed = [(name, err) for name, err in callback_results if err]
            if failed:
                logger.warning(f"[AsyncSystemManager] {len(failed)} start callbacks failed")

            # Import uvicorn only when needed
            import uvicorn

            # Create Uvicorn config with DISABLED signal handlers
            config = uvicorn.Config(
                self._app_module,
                host=self._host,
                port=self._port,
                log_level=self._log_level,
                access_log=True,
                workers=1,  # Single worker for in-process
                # CRITICAL: Disable signal handlers - supervisor manages signals
                **self._extra_config,
            )

            # Create server instance
            self._server = uvicorn.Server(config)

            # CRITICAL: Disable Uvicorn's built-in signal handlers
            # The supervisor will manage SIGINT/SIGTERM
            self._server.install_signal_handlers = lambda: None

            # Start server in background task
            self._server_task = asyncio.create_task(
                self._server.serve(),
                name="uvicorn-server",
            )

            # Wait for server to be ready (with timeout)
            ready_timeout = SystemManagerConfig.SERVER_READY_TIMEOUT
            start_wait = time.monotonic()

            while not self._server.started:
                if time.monotonic() - start_wait > ready_timeout:
                    raise TimeoutError(f"Server did not start within {ready_timeout}s")

                if self._server_task.done():
                    # Server task completed unexpectedly
                    exc = self._server_task.exception()
                    if exc:
                        raise exc
                    raise RuntimeError("Server task completed without starting")

                await asyncio.sleep(0.1)

            # Server is ready
            self._get_server_ready_event().set()
            self._transition_to(SystemPhase.RUNNING)
            self.update_service_health("uvicorn", True)

            logger.info(
                f"[AsyncSystemManager] ✅ Server running at http://{self._host}:{self._port}"
            )

            # Start health monitor if enabled
            if SystemManagerConfig.ENABLE_HEALTH_MONITOR:
                self._health_monitor_task = asyncio.create_task(
                    self._health_monitor_loop(),
                    name="health-monitor",
                )

        except Exception as e:
            self._transition_to(SystemPhase.FAILED, str(e))
            logger.error(f"[AsyncSystemManager] ❌ Failed to start: {e}", exc_info=True)
            raise

    async def stop(self, timeout: Optional[float] = None, reason: str = "requested") -> None:
        """
        Gracefully stop the server.

        This method:
        1. Signals shutdown
        2. Transitions to SHUTTING_DOWN phase
        3. Stops accepting new connections
        4. Waits for active requests to complete (with timeout)
        5. Runs stop callbacks
        6. Cancels server task
        7. Transitions to STOPPED phase

        Args:
            timeout: Maximum time to wait for graceful shutdown
            reason: Reason for stopping (for logging)
        """
        if self._phase in (SystemPhase.STOPPED, SystemPhase.FAILED, SystemPhase.SHUTTING_DOWN):
            logger.debug("[AsyncSystemManager] Already stopped or stopping")
            return

        timeout = timeout or SystemManagerConfig.SHUTDOWN_TIMEOUT
        self._transition_to(SystemPhase.SHUTTING_DOWN, reason)

        # Signal shutdown
        self._shutdown_requested.set()
        self._get_shutdown_event().set()

        try:
            # Stop health monitor
            if self._health_monitor_task and not self._health_monitor_task.done():
                self._health_monitor_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._health_monitor_task

            # Signal server to shutdown
            if self._server:
                self._server.should_exit = True

            # Wait for server task to complete (with timeout)
            if self._server_task and not self._server_task.done():
                try:
                    await asyncio.wait_for(self._server_task, timeout=timeout)
                except asyncio.TimeoutError:
                    logger.warning(f"[AsyncSystemManager] Server did not stop within {timeout}s, cancelling")
                    self._server_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await self._server_task
                except asyncio.CancelledError:
                    pass

            # Run stop callbacks
            logger.info("[AsyncSystemManager] Running stop callbacks...")
            callback_results = await self._run_callbacks(self._stop_callbacks, "stop")

            failed = [(name, err) for name, err in callback_results if err]
            if failed:
                logger.warning(f"[AsyncSystemManager] {len(failed)} stop callbacks failed")

            self._transition_to(SystemPhase.STOPPED, reason)
            self.update_service_health("uvicorn", False)

            logger.info(f"[AsyncSystemManager] ✅ Server stopped ({reason})")

        except Exception as e:
            self._transition_to(SystemPhase.FAILED, str(e))
            logger.error(f"[AsyncSystemManager] ❌ Error during shutdown: {e}", exc_info=True)
            raise

    async def wait_for_shutdown(self) -> None:
        """
        Wait for shutdown signal.

        This blocks until either:
        - signal_shutdown() is called
        - stop() is called
        - The server task completes

        Use this in the main coroutine to keep the server running:

            await manager.start()
            await manager.wait_for_shutdown()
            await manager.stop()
        """
        if not self.is_running:
            return

        shutdown_event = self._get_shutdown_event()

        # Wait for either shutdown event or server task completion
        _, pending = await asyncio.wait(
            [
                asyncio.create_task(shutdown_event.wait()),
                self._server_task,
            ] if self._server_task else [
                asyncio.create_task(shutdown_event.wait()),
            ],
            return_when=asyncio.FIRST_COMPLETED,
        )

        # Cancel pending tasks
        for task in pending:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    def signal_shutdown(self, reason: str = "signal") -> None:
        """
        Signal the system to shutdown (thread-safe).

        This is safe to call from any thread, including signal handlers.
        The actual shutdown happens asynchronously.

        Args:
            reason: Reason for shutdown (for logging)
        """
        logger.info(f"[AsyncSystemManager] Shutdown signaled: {reason}")

        with self._state_lock:
            self._shutdown_reason = reason

        self._shutdown_requested.set()

        # Set async event if available (may not be if called before start)
        if self._shutdown_event:
            try:
                # Get the event loop if running
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop.call_soon_threadsafe(self._shutdown_event.set)
                else:
                    self._shutdown_event.set()
            except RuntimeError:
                # No event loop available
                pass

    async def wait_for_ready(self, timeout: Optional[float] = None) -> bool:
        """
        Wait for server to be ready to accept requests.

        Args:
            timeout: Maximum time to wait (default from config)

        Returns:
            True if server is ready, False if timeout
        """
        timeout = timeout or SystemManagerConfig.SERVER_READY_TIMEOUT

        try:
            await asyncio.wait_for(
                self._get_server_ready_event().wait(),
                timeout=timeout,
            )
            return True
        except asyncio.TimeoutError:
            return False

    # =========================================================================
    # Context Manager Support
    # =========================================================================

    async def __aenter__(self) -> "AsyncSystemManager":
        """Async context manager entry - starts the server."""
        await self.start()
        return self

    async def __aexit__(self, exc_type: Any, _exc_val: Any, _exc_tb: Any) -> None:
        """Async context manager exit - stops the server."""
        reason = "exception" if exc_type else "context_exit"
        await self.stop(reason=reason)

    # =========================================================================
    # Status & Debugging
    # =========================================================================

    def get_status(self) -> Dict[str, Any]:
        """
        Get comprehensive system status.

        Returns:
            Dictionary with full system status
        """
        state = self.state

        return {
            "state": state.to_dict(),
            "config": {
                "host": self._host,
                "port": self._port,
                "app_module": self._app_module,
                "log_level": self._log_level,
            },
            "callbacks": {
                "start_count": len(self._start_callbacks),
                "stop_count": len(self._stop_callbacks),
            },
            "server": {
                "running": self._server.started if self._server else False,
                "task_done": self._server_task.done() if self._server_task else True,
            } if self._server else None,
        }

    def __repr__(self) -> str:
        return (
            f"<AsyncSystemManager phase={self.phase.value} "
            f"host={self._host}:{self._port} uptime={self.uptime_seconds:.1f}s>"
        )


# =============================================================================
# Singleton Management
# =============================================================================

_manager_instance: Optional[AsyncSystemManager] = None
_manager_lock = threading.Lock()  # Thread lock, NOT asyncio.Lock!


def get_system_manager(**kwargs) -> AsyncSystemManager:
    """
    Get or create the system manager singleton.

    This function is thread-safe and can be called from any context.

    Args:
        **kwargs: Arguments passed to AsyncSystemManager if creating new instance

    Returns:
        The singleton AsyncSystemManager instance
    """
    global _manager_instance

    # Double-checked locking pattern
    if _manager_instance is None:
        with _manager_lock:
            if _manager_instance is None:
                _manager_instance = AsyncSystemManager(**kwargs)
                logger.debug("[AsyncSystemManager] Singleton created")

    return _manager_instance


def reset_system_manager() -> None:
    """
    Reset the singleton instance.

    WARNING: This is for testing only! Do not use in production.
    The manager must be stopped before resetting.
    """
    global _manager_instance

    with _manager_lock:
        if _manager_instance is not None:
            if _manager_instance.is_running:
                logger.warning(
                    "[AsyncSystemManager] Resetting while running! "
                    "Call stop() first in production."
                )
            _manager_instance = None
            logger.debug("[AsyncSystemManager] Singleton reset")


def is_system_manager_initialized() -> bool:
    """Check if the system manager singleton exists."""
    return _manager_instance is not None


# =============================================================================
# Convenience Functions
# =============================================================================

async def start_system(**kwargs) -> AsyncSystemManager:
    """
    Convenience function to start the system.

    Args:
        **kwargs: Arguments passed to get_system_manager

    Returns:
        The running AsyncSystemManager instance
    """
    manager = get_system_manager(**kwargs)
    await manager.start()
    return manager


async def stop_system(timeout: Optional[float] = None, reason: str = "requested") -> None:
    """
    Convenience function to stop the system.

    Args:
        timeout: Shutdown timeout
        reason: Shutdown reason
    """
    if _manager_instance is not None:
        await _manager_instance.stop(timeout=timeout, reason=reason)


def signal_system_shutdown(reason: str = "signal") -> None:
    """
    Convenience function to signal system shutdown.

    Thread-safe, can be called from signal handlers.

    Args:
        reason: Shutdown reason
    """
    if _manager_instance is not None:
        _manager_instance.signal_shutdown(reason)


# =============================================================================
# Exports
# =============================================================================

__all__ = [
    # Configuration
    "SystemManagerConfig",

    # Enums
    "SystemPhase",

    # Data classes
    "SystemState",
    "RegisteredCallback",

    # Main class
    "AsyncSystemManager",

    # Singleton functions
    "get_system_manager",
    "reset_system_manager",
    "is_system_manager_initialized",

    # Convenience functions
    "start_system",
    "stop_system",
    "signal_system_shutdown",
]
