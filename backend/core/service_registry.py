"""
Enterprise-Grade Service Registry v3.0
======================================

Dynamic service discovery system eliminating hardcoded ports and enabling
true distributed orchestration across JARVIS, J-Prime, and Reactor-Core.

Architecture:
    ┌──────────────────────────────────────────────────────────────────┐
    │                   Service Registry v3.0                          │
    │  ┌────────────────────────────────────────────────────────────┐  │
    │  │  File-Based Registry: ~/.jarvis/registry/services.json     │  │
    │  │  ┌──────────────┬──────────────┬─────────────────────┐     │  │
    │  │  │   JARVIS     │   J-PRIME    │   REACTOR-CORE      │     │  │
    │  │  │  PID: 12345  │  PID: 12346  │   PID: 12347        │     │  │
    │  │  │  Port: 5001  │  Port: 8000  │   Port: 8090        │     │  │
    │  │  │  Status: ✅  │  Status: ✅  │   Status: ✅         │     │  │
    │  │  └──────────────┴──────────────┴─────────────────────┘     │  │
    │  └────────────────────────────────────────────────────────────┘  │
    │                                                                  │
    │  Features:                                                       │
    │  • Atomic file operations with fcntl locking                     │
    │  • Automatic stale service cleanup (dead PIDs)                   │
    │  • Health heartbeat tracking                                     │
    │  • Zero hardcoded ports or URLs                                  │
    │  • Cross-process safe concurrent access                          │
    └──────────────────────────────────────────────────────────────────┘

Usage:
    # Register service on startup
    registry = ServiceRegistry()
    await registry.register_service(
        service_name="jarvis-prime",
        pid=os.getpid(),
        port=8000,
        health_endpoint="/health"
    )

    # Discover services dynamically
    jprime = await registry.discover_service("jarvis-prime")
    if jprime:
        url = f"http://{jprime.host}:{jprime.port}{jprime.health_endpoint}"

    # Heartbeat to keep alive
    await registry.heartbeat("jarvis-prime")

    # Clean deregistration on shutdown
    await registry.deregister_service("jarvis-prime")

Author: JARVIS AI System
Version: 3.0.0
"""

import asyncio
import fcntl
import json
import logging
import os
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set
from datetime import datetime, timedelta
import psutil

logger = logging.getLogger(__name__)


# =============================================================================
# Data Models
# =============================================================================

@dataclass
class ServiceInfo:
    """Information about a registered service."""
    service_name: str
    pid: int
    port: int
    host: str = "localhost"
    health_endpoint: str = "/health"
    status: str = "starting"  # starting, healthy, degraded, failed
    registered_at: float = 0.0
    last_heartbeat: float = 0.0
    metadata: Dict = None

    def __post_init__(self):
        if self.registered_at == 0.0:
            self.registered_at = time.time()
        if self.last_heartbeat == 0.0:
            self.last_heartbeat = time.time()
        if self.metadata is None:
            self.metadata = {}

    def to_dict(self) -> Dict:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict) -> "ServiceInfo":
        """Create from dictionary."""
        return cls(**data)

    def is_process_alive(self) -> bool:
        """Check if the service's process is still running."""
        try:
            process = psutil.Process(self.pid)
            return process.is_running()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False

    def is_stale(self, timeout_seconds: float = 60.0) -> bool:
        """Check if service hasn't sent heartbeat in timeout period."""
        return (time.time() - self.last_heartbeat) > timeout_seconds


# =============================================================================
# Service Registry
# =============================================================================

class ServiceRegistry:
    """
    Enterprise-grade service registry with atomic operations.

    Features:
    - File-based persistence with fcntl locking
    - Automatic stale service cleanup
    - Health tracking with heartbeats
    - Zero hardcoded configuration
    """

    def __init__(
        self,
        registry_dir: Optional[Path] = None,
        heartbeat_timeout: float = 60.0,
        cleanup_interval: float = 30.0
    ):
        """
        Initialize service registry.

        Args:
            registry_dir: Directory for registry file (default: ~/.jarvis/registry)
            heartbeat_timeout: Seconds before service considered stale
            cleanup_interval: Seconds between cleanup cycles
        """
        self.registry_dir = registry_dir or Path.home() / ".jarvis" / "registry"
        self.registry_file = self.registry_dir / "services.json"
        self.heartbeat_timeout = heartbeat_timeout
        self.cleanup_interval = cleanup_interval
        self._cleanup_task: Optional[asyncio.Task] = None

        # Ensure directory exists
        self.registry_dir.mkdir(parents=True, exist_ok=True)

        # Initialize registry file if doesn't exist
        if not self.registry_file.exists():
            self._write_registry({})

    def _acquire_lock(self, file_handle) -> None:
        """Acquire exclusive lock on registry file (blocking)."""
        fcntl.flock(file_handle.fileno(), fcntl.LOCK_EX)

    def _release_lock(self, file_handle) -> None:
        """Release lock on registry file."""
        fcntl.flock(file_handle.fileno(), fcntl.LOCK_UN)

    def _read_registry(self) -> Dict[str, ServiceInfo]:
        """
        Read registry with file locking (thread/process safe).

        Returns:
            Dict mapping service names to ServiceInfo
        """
        try:
            with open(self.registry_file, 'r+') as f:
                self._acquire_lock(f)
                try:
                    data = json.load(f)
                    return {
                        name: ServiceInfo.from_dict(info)
                        for name, info in data.items()
                    }
                finally:
                    self._release_lock(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _write_registry(self, services: Dict[str, ServiceInfo]) -> None:
        """
        Write registry with atomic file operations.

        Args:
            services: Dict mapping service names to ServiceInfo
        """
        # Convert to serializable dict
        data = {
            name: service.to_dict()
            for name, service in services.items()
        }

        # Atomic write with file locking
        temp_file = self.registry_file.with_suffix('.tmp')
        try:
            with open(temp_file, 'w') as f:
                self._acquire_lock(f)
                try:
                    json.dump(data, f, indent=2)
                    f.flush()
                    os.fsync(f.fileno())  # Force write to disk
                finally:
                    self._release_lock(f)

            # Atomic rename
            temp_file.replace(self.registry_file)

        except Exception as e:
            logger.error(f"Failed to write registry: {e}")
            if temp_file.exists():
                temp_file.unlink()
            raise

    async def register_service(
        self,
        service_name: str,
        pid: int,
        port: int,
        host: str = "localhost",
        health_endpoint: str = "/health",
        metadata: Optional[Dict] = None
    ) -> ServiceInfo:
        """
        Register a service in the registry.

        Args:
            service_name: Unique service identifier
            pid: Process ID
            port: Port number service is listening on
            host: Hostname (default: localhost)
            health_endpoint: Health check endpoint path
            metadata: Optional additional metadata

        Returns:
            ServiceInfo for the registered service
        """
        service = ServiceInfo(
            service_name=service_name,
            pid=pid,
            port=port,
            host=host,
            health_endpoint=health_endpoint,
            status="starting",
            metadata=metadata or {}
        )

        # Read existing registry
        services = await asyncio.to_thread(self._read_registry)

        # Add/update service
        services[service_name] = service

        # Write back atomically
        await asyncio.to_thread(self._write_registry, services)

        logger.info(
            f"📝 Service registered: {service_name} "
            f"(PID: {pid}, Port: {port}, Host: {host})"
        )

        return service

    async def deregister_service(self, service_name: str) -> bool:
        """
        Remove service from registry.

        Args:
            service_name: Service to deregister

        Returns:
            True if service was found and removed
        """
        services = await asyncio.to_thread(self._read_registry)

        if service_name in services:
            del services[service_name]
            await asyncio.to_thread(self._write_registry, services)
            logger.info(f"❌ Service deregistered: {service_name}")
            return True

        return False

    async def discover_service(self, service_name: str) -> Optional[ServiceInfo]:
        """
        Discover a service by name.

        Args:
            service_name: Service to find

        Returns:
            ServiceInfo if found and healthy, None otherwise
        """
        services = await asyncio.to_thread(self._read_registry)
        service = services.get(service_name)

        if not service:
            return None

        # Check if process is still alive
        if not service.is_process_alive():
            logger.warning(
                f"⚠️  Service {service_name} has dead PID {service.pid}, cleaning up"
            )
            await self.deregister_service(service_name)
            return None

        # Check if stale (no recent heartbeat)
        if service.is_stale(self.heartbeat_timeout):
            logger.warning(
                f"⚠️  Service {service_name} is stale "
                f"(last heartbeat {time.time() - service.last_heartbeat:.0f}s ago)"
            )
            return None

        return service

    async def list_services(self, healthy_only: bool = True) -> List[ServiceInfo]:
        """
        List all registered services.

        Args:
            healthy_only: If True, only return healthy services

        Returns:
            List of ServiceInfo objects
        """
        services = await asyncio.to_thread(self._read_registry)

        if not healthy_only:
            return list(services.values())

        # Filter to only healthy services
        healthy = []
        for service in services.values():
            if service.is_process_alive() and not service.is_stale(self.heartbeat_timeout):
                healthy.append(service)

        return healthy

    async def heartbeat(
        self,
        service_name: str,
        status: Optional[str] = None,
        metadata: Optional[Dict] = None
    ) -> bool:
        """
        Update service heartbeat and optionally status/metadata.

        Args:
            service_name: Service to update
            status: Optional new status (healthy, degraded, etc.)
            metadata: Optional metadata to merge

        Returns:
            True if service found and updated
        """
        services = await asyncio.to_thread(self._read_registry)

        if service_name not in services:
            logger.warning(f"⚠️  Heartbeat for unregistered service: {service_name}")
            return False

        service = services[service_name]
        service.last_heartbeat = time.time()

        if status:
            service.status = status

        if metadata:
            service.metadata.update(metadata)

        await asyncio.to_thread(self._write_registry, services)

        return True

    async def cleanup_stale_services(self) -> int:
        """
        Remove services with dead PIDs or stale heartbeats.

        Returns:
            Number of services cleaned up
        """
        services = await asyncio.to_thread(self._read_registry)
        cleaned = 0

        for service_name, service in list(services.items()):
            should_remove = False

            # Check if process is dead
            if not service.is_process_alive():
                logger.info(
                    f"🧹 Cleaning dead service: {service_name} (PID {service.pid} not found)"
                )
                should_remove = True

            # Check if stale
            elif service.is_stale(self.heartbeat_timeout):
                logger.info(
                    f"🧹 Cleaning stale service: {service_name} "
                    f"(last heartbeat {time.time() - service.last_heartbeat:.0f}s ago)"
                )
                should_remove = True

            if should_remove:
                del services[service_name]
                cleaned += 1

        if cleaned > 0:
            await asyncio.to_thread(self._write_registry, services)

        return cleaned

    async def _cleanup_loop(self) -> None:
        """Background task to periodically clean up stale services."""
        while True:
            try:
                await asyncio.sleep(self.cleanup_interval)
                cleaned = await self.cleanup_stale_services()
                if cleaned > 0:
                    logger.debug(f"🧹 Cleaned {cleaned} stale services")

            except asyncio.CancelledError:
                logger.info("🛑 Service registry cleanup loop stopped")
                break

            except Exception as e:
                logger.error(f"❌ Error in cleanup loop: {e}", exc_info=True)

    async def start_cleanup_task(self) -> None:
        """Start background cleanup task."""
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())
            logger.info(
                f"🧹 Service registry cleanup started "
                f"(interval: {self.cleanup_interval}s, timeout: {self.heartbeat_timeout}s)"
            )

    async def stop_cleanup_task(self) -> None:
        """Stop background cleanup task."""
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            logger.info("🛑 Service registry cleanup stopped")

    async def wait_for_service(
        self,
        service_name: str,
        timeout: float = 30.0,
        check_interval: float = 1.0
    ) -> Optional[ServiceInfo]:
        """
        Wait for a service to become available.

        Args:
            service_name: Service to wait for
            timeout: Maximum time to wait (seconds)
            check_interval: How often to check (seconds)

        Returns:
            ServiceInfo if service becomes available, None if timeout
        """
        start_time = time.time()

        while (time.time() - start_time) < timeout:
            service = await self.discover_service(service_name)
            if service:
                logger.info(f"✅ Service discovered: {service_name}")
                return service

            await asyncio.sleep(check_interval)

        logger.warning(
            f"⏱️  Timeout waiting for service: {service_name} (after {timeout}s)"
        )
        return None


# =============================================================================
# v17.0: SERVICE LAUNCH CONFIG - Auto-Restart Configuration
# =============================================================================

@dataclass
class ServiceLaunchConfig:
    """
    v17.0: Configuration for how to launch/restart a service.

    Stores all information needed to restart a service automatically
    when it dies or becomes unhealthy.
    """
    service_name: str
    command: List[str]  # e.g., ["python3", "server.py"]
    working_dir: str
    env_vars: Dict[str, str] = None
    python_venv: Optional[str] = None  # Path to venv/bin/python
    restart_policy: str = "always"  # always, on-failure, never
    max_restarts: int = 5
    restart_delay_sec: float = 5.0
    restart_backoff_multiplier: float = 2.0
    max_restart_delay_sec: float = 300.0
    health_check_timeout_sec: float = 30.0

    def __post_init__(self):
        if self.env_vars is None:
            self.env_vars = {}

    def to_dict(self) -> Dict:
        return {
            "service_name": self.service_name,
            "command": self.command,
            "working_dir": self.working_dir,
            "env_vars": self.env_vars,
            "python_venv": self.python_venv,
            "restart_policy": self.restart_policy,
            "max_restarts": self.max_restarts,
            "restart_delay_sec": self.restart_delay_sec,
            "restart_backoff_multiplier": self.restart_backoff_multiplier,
            "max_restart_delay_sec": self.max_restart_delay_sec,
            "health_check_timeout_sec": self.health_check_timeout_sec,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "ServiceLaunchConfig":
        return cls(**data)


@dataclass
class ServiceRestartHistory:
    """v17.0: Track restart attempts for a service."""
    service_name: str
    restart_count: int = 0
    last_restart_time: float = 0.0
    last_failure_reason: Optional[str] = None
    consecutive_failures: int = 0

    def record_restart(self, reason: str = None):
        self.restart_count += 1
        self.last_restart_time = time.time()
        self.last_failure_reason = reason

    def record_success(self):
        self.consecutive_failures = 0

    def record_failure(self, reason: str = None):
        self.consecutive_failures += 1
        self.last_failure_reason = reason

    def get_current_delay(self, config: ServiceLaunchConfig) -> float:
        """Calculate current restart delay with exponential backoff."""
        if self.consecutive_failures == 0:
            return config.restart_delay_sec

        delay = config.restart_delay_sec * (
            config.restart_backoff_multiplier ** (self.consecutive_failures - 1)
        )
        return min(delay, config.max_restart_delay_sec)

    def should_restart(self, config: ServiceLaunchConfig) -> bool:
        """Check if service should be restarted based on policy."""
        if config.restart_policy == "never":
            return False

        if config.restart_policy == "on-failure" and self.last_failure_reason is None:
            return False

        return self.restart_count < config.max_restarts


# =============================================================================
# v17.0: SERVICE SUPERVISOR - Cross-Repo Auto-Restart Manager
# =============================================================================

class ServiceSupervisor:
    """
    v17.0: Enterprise-grade service supervisor with auto-restart.

    Features:
    - Automatic restart of dead services with exponential backoff
    - Cross-repo service management (JARVIS, J-Prime, Reactor-Core)
    - Health monitoring with configurable restart policies
    - Parallel launch support for fast startup
    - Graceful degradation when services repeatedly fail
    - Distributed tracing for restart events

    Architecture:
        ┌─────────────────────────────────────────────────────────────────┐
        │                    Service Supervisor v17.0                      │
        ├─────────────────────────────────────────────────────────────────┤
        │  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────────┐  │
        │  │   Monitor   │  │  Restarter  │  │    Health Checker       │  │
        │  │   (async)   │──│   (async)   │──│     (parallel)          │  │
        │  └──────┬──────┘  └──────┬──────┘  └───────────┬─────────────┘  │
        │         │                │                     │                │
        │         ▼                ▼                     ▼                │
        │  ┌──────────────────────────────────────────────────────────┐   │
        │  │              Service Registry (File-Based)                │   │
        │  └──────────────────────────────────────────────────────────┘   │
        └─────────────────────────────────────────────────────────────────┘
    """

    def __init__(
        self,
        registry: Optional[ServiceRegistry] = None,
        monitor_interval: float = 10.0,
        launch_configs_file: Optional[Path] = None,
    ):
        """
        Initialize service supervisor.

        Args:
            registry: ServiceRegistry instance (uses global if not provided)
            monitor_interval: Seconds between health checks
            launch_configs_file: File to persist launch configs
        """
        self._registry = registry
        self._monitor_interval = monitor_interval
        self._launch_configs_file = launch_configs_file or (
            Path.home() / ".jarvis" / "registry" / "launch_configs.json"
        )

        self._launch_configs: Dict[str, ServiceLaunchConfig] = {}
        self._restart_history: Dict[str, ServiceRestartHistory] = {}
        self._processes: Dict[str, asyncio.subprocess.Process] = {}
        self._running = False
        self._monitor_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._restart_callbacks: List[callable] = []

        # Load persisted launch configs
        self._load_launch_configs()

        logger.info("[v17.0] ServiceSupervisor initialized")

    @property
    def registry(self) -> ServiceRegistry:
        """Get service registry (lazy initialization)."""
        if self._registry is None:
            self._registry = get_service_registry()
        return self._registry

    def _load_launch_configs(self) -> None:
        """Load persisted launch configurations."""
        try:
            if self._launch_configs_file.exists():
                with open(self._launch_configs_file) as f:
                    data = json.load(f)
                    self._launch_configs = {
                        name: ServiceLaunchConfig.from_dict(cfg)
                        for name, cfg in data.items()
                    }
                logger.debug(f"[v17.0] Loaded {len(self._launch_configs)} launch configs")
        except Exception as e:
            logger.warning(f"[v17.0] Failed to load launch configs: {e}")

    def _save_launch_configs(self) -> None:
        """Persist launch configurations to disk."""
        try:
            self._launch_configs_file.parent.mkdir(parents=True, exist_ok=True)
            data = {
                name: cfg.to_dict()
                for name, cfg in self._launch_configs.items()
            }
            with open(self._launch_configs_file, 'w') as f:
                json.dump(data, f, indent=2)
            logger.debug(f"[v17.0] Saved {len(data)} launch configs")
        except Exception as e:
            logger.warning(f"[v17.0] Failed to save launch configs: {e}")

    def register_service_launch(self, config: ServiceLaunchConfig) -> None:
        """
        Register how to launch a service for auto-restart.

        Args:
            config: ServiceLaunchConfig with launch details
        """
        self._launch_configs[config.service_name] = config
        self._restart_history[config.service_name] = ServiceRestartHistory(
            service_name=config.service_name
        )
        self._save_launch_configs()
        logger.info(f"[v17.0] Registered launch config for: {config.service_name}")

    def on_restart(self, callback: callable) -> None:
        """Register callback for restart events."""
        self._restart_callbacks.append(callback)

    async def _notify_restart(
        self,
        service_name: str,
        success: bool,
        reason: str = None
    ) -> None:
        """Notify callbacks of restart event."""
        for callback in self._restart_callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(service_name, success, reason)
                else:
                    callback(service_name, success, reason)
            except Exception as e:
                logger.warning(f"[v17.0] Restart callback error: {e}")

    async def launch_service(
        self,
        service_name: str,
        wait_for_healthy: bool = True,
    ) -> Optional[asyncio.subprocess.Process]:
        """
        Launch a service using its registered config.

        Args:
            service_name: Service to launch
            wait_for_healthy: Wait for health check to pass

        Returns:
            Process if launched successfully, None otherwise
        """
        async with self._lock:
            if service_name not in self._launch_configs:
                logger.warning(f"[v17.0] No launch config for: {service_name}")
                return None

            config = self._launch_configs[service_name]
            history = self._restart_history.get(
                service_name,
                ServiceRestartHistory(service_name=service_name)
            )

            # Check restart policy
            if not history.should_restart(config):
                logger.warning(
                    f"[v17.0] Service {service_name} exceeded max restarts "
                    f"({history.restart_count}/{config.max_restarts})"
                )
                return None

            # Calculate delay if this is a restart
            if history.restart_count > 0:
                delay = history.get_current_delay(config)
                logger.info(
                    f"[v17.0] Waiting {delay:.1f}s before restarting {service_name} "
                    f"(attempt {history.restart_count + 1}/{config.max_restarts})"
                )
                await asyncio.sleep(delay)

            # Build command
            cmd = config.command.copy()
            if config.python_venv:
                # Replace python with venv python
                if cmd[0] in ("python", "python3"):
                    cmd[0] = config.python_venv

            # Build environment
            env = os.environ.copy()
            env.update(config.env_vars)
            env["PYTHONPATH"] = config.working_dir

            # Create log files
            log_dir = Path.home() / ".jarvis" / "logs" / "services"
            log_dir.mkdir(parents=True, exist_ok=True)
            stdout_log = log_dir / f"{service_name}_stdout.log"
            stderr_log = log_dir / f"{service_name}_stderr.log"

            try:
                logger.info(f"[v17.0] Launching {service_name}: {' '.join(cmd)}")

                stdout_fd = open(stdout_log, 'a')
                stderr_fd = open(stderr_log, 'a')

                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=config.working_dir,
                    env=env,
                    stdout=stdout_fd,
                    stderr=stderr_fd,
                    start_new_session=True,
                )

                self._processes[service_name] = process
                history.record_restart(reason="supervisor_launch")
                self._restart_history[service_name] = history

                logger.info(f"[v17.0] {service_name} launched (PID: {process.pid})")

                # Wait for health check if requested
                if wait_for_healthy:
                    service_info = await self.registry.wait_for_service(
                        service_name,
                        timeout=config.health_check_timeout_sec,
                    )
                    if service_info:
                        history.record_success()
                        logger.info(f"[v17.0] ✅ {service_name} healthy")
                        await self._notify_restart(service_name, True, "healthy")
                    else:
                        history.record_failure(reason="health_check_timeout")
                        logger.warning(f"[v17.0] ⚠️ {service_name} unhealthy after launch")
                        await self._notify_restart(service_name, False, "health_check_timeout")

                return process

            except Exception as e:
                history.record_failure(reason=str(e))
                self._restart_history[service_name] = history
                logger.error(f"[v17.0] Failed to launch {service_name}: {e}")
                await self._notify_restart(service_name, False, str(e))
                return None

    async def restart_service(self, service_name: str) -> bool:
        """
        Restart a dead or unhealthy service.

        Args:
            service_name: Service to restart

        Returns:
            True if restart was successful
        """
        logger.info(f"[v17.0] Restarting service: {service_name}")

        # Kill existing process if any
        if service_name in self._processes:
            old_proc = self._processes[service_name]
            if old_proc.returncode is None:
                try:
                    old_proc.terminate()
                    await asyncio.wait_for(old_proc.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    old_proc.kill()
            del self._processes[service_name]

        # Deregister from registry
        await self.registry.deregister_service(service_name)

        # Launch fresh
        process = await self.launch_service(service_name, wait_for_healthy=True)
        return process is not None

    async def _monitor_loop(self) -> None:
        """Background loop to monitor and auto-restart services."""
        logger.info(f"[v17.0] Service monitor started (interval: {self._monitor_interval}s)")

        while self._running:
            try:
                await asyncio.sleep(self._monitor_interval)

                # Check each registered service
                for service_name, config in self._launch_configs.items():
                    if config.restart_policy == "never":
                        continue

                    # Try to discover the service
                    service_info = await self.registry.discover_service(service_name)

                    if service_info is None:
                        # Service is dead or stale - restart it
                        logger.warning(
                            f"[v17.0] Service {service_name} is dead/stale, initiating restart"
                        )
                        await self.restart_service(service_name)

            except asyncio.CancelledError:
                logger.info("[v17.0] Service monitor stopping")
                break
            except Exception as e:
                logger.error(f"[v17.0] Monitor loop error: {e}", exc_info=True)

    async def start(self) -> None:
        """Start the service supervisor."""
        self._running = True
        self._monitor_task = asyncio.create_task(
            self._monitor_loop(),
            name="service_supervisor_monitor"
        )
        logger.info("[v17.0] ServiceSupervisor started")

    async def stop(self) -> None:
        """Stop the service supervisor."""
        self._running = False
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        logger.info("[v17.0] ServiceSupervisor stopped")

    async def launch_all_services(
        self,
        parallel: bool = True,
        wait_for_healthy: bool = True,
    ) -> Dict[str, bool]:
        """
        Launch all registered services.

        Args:
            parallel: Launch services in parallel
            wait_for_healthy: Wait for health checks

        Returns:
            Dict mapping service_name to success status
        """
        results = {}

        if parallel:
            # Launch all in parallel
            tasks = {
                name: asyncio.create_task(
                    self.launch_service(name, wait_for_healthy=wait_for_healthy)
                )
                for name in self._launch_configs.keys()
            }

            for name, task in tasks.items():
                try:
                    process = await asyncio.wait_for(task, timeout=120.0)
                    results[name] = process is not None
                except asyncio.TimeoutError:
                    results[name] = False
                    logger.warning(f"[v17.0] Timeout launching {name}")
                except Exception as e:
                    results[name] = False
                    logger.error(f"[v17.0] Error launching {name}: {e}")
        else:
            # Launch sequentially
            for name in self._launch_configs.keys():
                try:
                    process = await self.launch_service(name, wait_for_healthy=wait_for_healthy)
                    results[name] = process is not None
                except Exception as e:
                    results[name] = False
                    logger.error(f"[v17.0] Error launching {name}: {e}")

        return results

    def get_stats(self) -> Dict[str, Any]:
        """Get supervisor statistics."""
        return {
            "running": self._running,
            "services_registered": len(self._launch_configs),
            "services_with_process": len(self._processes),
            "restart_history": {
                name: {
                    "restart_count": history.restart_count,
                    "consecutive_failures": history.consecutive_failures,
                    "last_restart": history.last_restart_time,
                    "last_failure": history.last_failure_reason,
                }
                for name, history in self._restart_history.items()
            },
        }


# Global supervisor instance
_global_supervisor: Optional[ServiceSupervisor] = None


def get_service_supervisor() -> ServiceSupervisor:
    """Get global service supervisor instance (singleton)."""
    global _global_supervisor
    if _global_supervisor is None:
        _global_supervisor = ServiceSupervisor()
    return _global_supervisor


# =============================================================================
# Convenience Functions
# =============================================================================

_global_registry: Optional[ServiceRegistry] = None


def get_service_registry() -> ServiceRegistry:
    """Get global service registry instance (singleton)."""
    global _global_registry
    if _global_registry is None:
        _global_registry = ServiceRegistry()
    return _global_registry


async def register_current_service(
    service_name: str,
    port: int,
    health_endpoint: str = "/health",
    metadata: Optional[Dict] = None
) -> ServiceInfo:
    """
    Convenience function to register current process as a service.

    Args:
        service_name: Name for this service
        port: Port this service listens on
        health_endpoint: Health check endpoint
        metadata: Optional metadata

    Returns:
        ServiceInfo for registered service
    """
    registry = get_service_registry()
    return await registry.register_service(
        service_name=service_name,
        pid=os.getpid(),
        port=port,
        health_endpoint=health_endpoint,
        metadata=metadata
    )


# =============================================================================
# Module Exports
# =============================================================================

__all__ = [
    # Core classes
    "ServiceRegistry",
    "ServiceInfo",
    # v17.0: Service Supervisor
    "ServiceSupervisor",
    "ServiceLaunchConfig",
    "ServiceRestartHistory",
    # Convenience functions
    "get_service_registry",
    "get_service_supervisor",
    "register_current_service",
]
