"""
Process-Isolated ML Loader
===========================

CRITICAL FIX for the root cause of unkillable startup hangs.

The Problem:
- SpeechBrain/PyTorch model loading is SYNCHRONOUS
- When called inside async functions, it blocks the event loop
- asyncio.wait_for() timeouts DON'T WORK because the event loop is blocked
- The process enters "uninterruptible sleep" (D state) and can't be killed
- Only a system restart can clear it

The Solution:
- Run ALL ML model loading in SEPARATE PROCESSES (not threads!)
- Multiprocessing allows true process termination via SIGKILL
- Parent process monitors with timeouts and kills if stuck
- Graceful fallback when models fail to load

Usage:
    from core.process_isolated_ml_loader import (
        load_model_isolated,
        load_speechbrain_model,
        cleanup_stuck_ml_processes
    )

    # Load SpeechBrain model with 30s timeout (truly killable)
    model = await load_speechbrain_model(
        model_name="speechbrain/spkrec-ecapa-voxceleb",
        timeout=30.0
    )

    # Or generic model loading
    result = await load_model_isolated(
        loader_func=my_model_loader,
        timeout=30.0,
        operation_name="Custom Model"
    )

Author: JARVIS AI System
Version: 1.0.0
"""

import asyncio
import atexit
import logging
import multiprocessing
import os
import pickle
import signal
import sys
import tempfile
import time
import traceback
import weakref
from concurrent.futures import ProcessPoolExecutor, TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar

import psutil

logger = logging.getLogger(__name__)

T = TypeVar('T')


# =============================================================================
# Configuration
# =============================================================================

@dataclass
class MLLoaderConfig:
    """Configuration for process-isolated ML loading."""
    default_timeout: float = 60.0  # Default timeout for model loading
    max_retries: int = 2  # Max retries on timeout
    retry_delay: float = 1.0  # Delay between retries
    cleanup_on_start: bool = True  # Clean stuck processes on initialization
    max_memory_mb: int = 4096  # Max memory per worker process
    worker_startup_timeout: float = 10.0  # Timeout for worker process to start
    graceful_shutdown_timeout: float = 5.0  # Time to wait for graceful shutdown
    force_kill_delay: float = 2.0  # Delay before SIGKILL after SIGTERM

    # Process identification
    process_marker: str = "JARVIS_ML_LOADER"

    @classmethod
    def from_environment(cls) -> 'MLLoaderConfig':
        """Load configuration from environment variables."""
        return cls(
            default_timeout=float(os.getenv('ML_LOADER_TIMEOUT', '60.0')),
            max_retries=int(os.getenv('ML_LOADER_MAX_RETRIES', '2')),
            cleanup_on_start=os.getenv('ML_LOADER_CLEANUP_ON_START', 'true').lower() == 'true',
            max_memory_mb=int(os.getenv('ML_LOADER_MAX_MEMORY_MB', '4096')),
        )


# Global configuration
_config: Optional[MLLoaderConfig] = None


def get_ml_loader_config() -> MLLoaderConfig:
    """Get current ML loader configuration."""
    global _config
    if _config is None:
        _config = MLLoaderConfig.from_environment()
    return _config


# =============================================================================
# Advanced Self-Healing Process Manager
# =============================================================================

@dataclass
class PortHealthResult:
    """Result of a port health check."""
    port: int
    healthy: bool
    status_code: Optional[int] = None
    response_time_ms: Optional[float] = None
    error: Optional[str] = None
    process_state: Optional[str] = None
    pid: Optional[int] = None
    is_unkillable: bool = False
    checked_at: datetime = field(default_factory=datetime.now)


@dataclass
class SelfHealingConfig:
    """Configuration for self-healing process manager."""
    # Port configuration - loaded dynamically
    primary_port: int = 8011
    fallback_ports: List[int] = field(default_factory=lambda: [8010, 8000, 8001, 8080, 8888])

    # Health check settings
    health_check_timeout: float = 2.0
    health_endpoint: str = "/health"
    protocol: str = "http"
    host: str = "localhost"

    # Self-healing settings
    healing_enabled: bool = True
    healing_interval_seconds: float = 30.0
    max_healing_attempts: int = 3

    # Process cleanup settings
    force_kill_delay: float = 2.0
    graceful_shutdown_timeout: float = 5.0

    # macOS UE state detection
    ue_state_indicators: List[str] = field(default_factory=lambda: [
        'disk-sleep',      # Standard uninterruptible sleep
        'uninterruptible', # Direct UE state indicator
        'D',               # Short form in ps output
        'U',               # macOS specific
    ])

    @classmethod
    def from_config_file(cls, config_path: Optional[str] = None) -> 'SelfHealingConfig':
        """Load configuration from startup_progress_config.json."""
        import json

        # Default config path
        if config_path is None:
            base_dir = Path(__file__).parent.parent
            config_path = base_dir / 'config' / 'startup_progress_config.json'

        config = cls()

        try:
            if Path(config_path).exists():
                with open(config_path, 'r') as f:
                    data = json.load(f)

                backend_config = data.get('backend_config', {})
                config.primary_port = backend_config.get('port', 8011)
                config.fallback_ports = backend_config.get('fallback_ports', [8010, 8000, 8001, 8080, 8888])
                config.host = backend_config.get('host', 'localhost')
                config.protocol = backend_config.get('protocol', 'http')

                logger.debug(f"Loaded self-healing config: primary_port={config.primary_port}")
        except Exception as e:
            logger.warning(f"Could not load config from {config_path}: {e}, using defaults")

        return config


class AdvancedProcessSelfHealer:
    """
    Advanced Self-Healing Process Manager for JARVIS.

    Provides:
    - Dynamic port discovery with parallel health checks (fully async)
    - macOS UE (Uninterruptible Sleep) state detection
    - Self-healing with automatic port failover
    - Continuous monitoring and healing loop
    - No hardcoded values - all configuration from config files

    Usage:
        healer = AdvancedProcessSelfHealer()

        # One-time cleanup and port discovery
        result = await healer.cleanup_and_discover_port()
        print(f"Best port: {result['selected_port']}")

        # Start continuous self-healing (background task)
        await healer.start_healing_loop()
    """

    def __init__(self, config: Optional[SelfHealingConfig] = None):
        self.config = config or SelfHealingConfig.from_config_file()
        self._session: Optional[Any] = None
        self._healing_task: Optional[asyncio.Task] = None
        self._shutdown_event = asyncio.Event()
        self._last_healthy_port: Optional[int] = None
        self._healing_attempts: Dict[int, int] = {}  # port -> attempt count
        self._port_blacklist: set = set()  # Ports with unkillable processes

    async def _get_session(self):
        """Get or create aiohttp session (lazy initialization)."""
        if self._session is None or self._session.closed:
            try:
                import aiohttp
                timeout = aiohttp.ClientTimeout(total=self.config.health_check_timeout)
                self._session = aiohttp.ClientSession(timeout=timeout)
            except ImportError:
                logger.warning("aiohttp not available, using fallback HTTP checks")
                return None
        return self._session

    async def close(self):
        """Clean up resources."""
        if self._session and not self._session.closed:
            await self._session.close()
        self._shutdown_event.set()
        if self._healing_task:
            self._healing_task.cancel()
            try:
                await self._healing_task
            except asyncio.CancelledError:
                pass

    # =========================================================================
    # Port Health Checking (Fully Async with Parallel Scanning)
    # =========================================================================

    async def check_port_health(self, port: int) -> PortHealthResult:
        """
        Check health of a specific port with comprehensive diagnostics.

        Detects:
        - HTTP health status
        - Process state (including UE/uninterruptible)
        - Whether process is killable
        """
        result = PortHealthResult(port=port, healthy=False)
        start_time = time.time()

        # First, check process state on this port
        process_info = await self._get_process_on_port(port)
        if process_info:
            result.pid = process_info.get('pid')
            result.process_state = process_info.get('status')
            result.is_unkillable = self._is_unkillable_state(process_info.get('status', ''))

            if result.is_unkillable:
                result.error = f"Process PID {result.pid} in unkillable state: {result.process_state}"
                logger.warning(f"Port {port}: {result.error}")
                return result

        # Check HTTP health
        session = await self._get_session()
        if session:
            url = f"{self.config.protocol}://{self.config.host}:{port}{self.config.health_endpoint}"

            async def _do_health_check():
                async with session.get(url) as resp:
                    result.status_code = resp.status
                    if resp.status == 200:
                        try:
                            data = await resp.json()
                            if data.get('status') == 'healthy':
                                result.healthy = True
                        except Exception:
                            # Even without JSON, 200 OK is good
                            result.healthy = True

            try:
                await asyncio.wait_for(
                    _do_health_check(),
                    timeout=self.config.health_check_timeout
                )
            except asyncio.TimeoutError:
                result.error = 'timeout'
            except Exception as e:
                error_type = type(e).__name__
                result.error = f'{error_type}: {str(e)[:50]}'
        else:
            # Fallback: socket-based check
            result = await self._socket_health_check(port, result)

        result.response_time_ms = (time.time() - start_time) * 1000
        return result

    async def _socket_health_check(self, port: int, result: PortHealthResult) -> PortHealthResult:
        """Fallback socket-based health check."""
        import socket

        def _check():
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(self.config.health_check_timeout)
                try:
                    s.connect((self.config.host, port))
                    return True
                except (socket.timeout, ConnectionRefusedError):
                    return False

        try:
            is_open = await asyncio.get_event_loop().run_in_executor(None, _check)
            if is_open:
                # Port is open, but we can't verify health without HTTP
                result.healthy = True  # Assume healthy if accepting connections
                result.error = 'socket_only_check'
        except Exception as e:
            result.error = str(e)

        return result

    async def _get_process_on_port(self, port: int) -> Optional[Dict[str, Any]]:
        """Get process information for a process listening on the given port."""
        try:
            for conn in psutil.net_connections(kind='inet'):
                if hasattr(conn.laddr, 'port') and conn.laddr.port == port:
                    if conn.status == 'LISTEN' and conn.pid:
                        try:
                            proc = psutil.Process(conn.pid)
                            return {
                                'pid': conn.pid,
                                'name': proc.name(),
                                'status': proc.status(),
                                'cmdline': ' '.join(proc.cmdline() or [])[:200],
                                'create_time': proc.create_time(),
                                'cpu_percent': proc.cpu_percent(interval=0),
                            }
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            pass
        except Exception as e:
            logger.debug(f"Error getting process on port {port}: {e}")
        return None

    def _is_unkillable_state(self, status: str) -> bool:
        """Check if process status indicates an unkillable (UE) state."""
        if not status:
            return False
        status_lower = status.lower()
        return any(indicator.lower() in status_lower for indicator in self.config.ue_state_indicators)

    async def discover_healthy_port(self, include_blacklisted: bool = False) -> Dict[str, Any]:
        """
        Scan all configured ports IN PARALLEL and return the healthiest one.

        Returns:
            Dict with:
            - selected_port: int or None
            - all_results: List[PortHealthResult]
            - unkillable_ports: List[int]
            - discovery_time_ms: float
        """
        start_time = time.time()

        # Build port list (primary first, then fallbacks)
        all_ports = [self.config.primary_port] + [
            p for p in self.config.fallback_ports if p != self.config.primary_port
        ]

        # Remove blacklisted ports unless specifically requested
        if not include_blacklisted:
            all_ports = [p for p in all_ports if p not in self._port_blacklist]

        # Parallel health checks
        tasks = [self.check_port_health(port) for port in all_ports]
        results: List[PortHealthResult] = await asyncio.gather(*tasks, return_exceptions=True)

        # Process results
        healthy_ports = []
        unkillable_ports = []
        all_results = []

        for result in results:
            if isinstance(result, Exception):
                logger.warning(f"Port check exception: {result}")
                continue

            all_results.append(result)

            if result.is_unkillable:
                unkillable_ports.append(result.port)
                self._port_blacklist.add(result.port)
            elif result.healthy:
                healthy_ports.append(result)

        # Select best port (fastest healthy response)
        selected_port = None
        if healthy_ports:
            # Sort by response time
            healthy_ports.sort(key=lambda r: r.response_time_ms or float('inf'))
            selected_port = healthy_ports[0].port
            self._last_healthy_port = selected_port

        discovery_time_ms = (time.time() - start_time) * 1000

        logger.info(
            f"Port discovery complete in {discovery_time_ms:.0f}ms: "
            f"selected={selected_port}, healthy={len(healthy_ports)}, "
            f"unkillable={len(unkillable_ports)}"
        )

        return {
            'selected_port': selected_port,
            'all_results': all_results,
            'unkillable_ports': unkillable_ports,
            'discovery_time_ms': discovery_time_ms,
        }

    # =========================================================================
    # Process Cleanup and Healing
    # =========================================================================

    async def cleanup_port(self, port: int, force: bool = False) -> Dict[str, Any]:
        """
        Attempt to clean up a process blocking a specific port.

        Returns:
            Dict with cleanup results and whether port is now free.
        """
        result = {
            'port': port,
            'cleaned': False,
            'process_killed': False,
            'was_unkillable': False,
            'error': None,
        }

        process_info = await self._get_process_on_port(port)
        if not process_info:
            result['cleaned'] = True  # No process, port is free
            return result

        pid = process_info['pid']
        status = process_info.get('status', '')

        # Check for unkillable state
        if self._is_unkillable_state(status):
            result['was_unkillable'] = True
            result['error'] = f"Process {pid} is in unkillable state '{status}' - requires system restart"
            self._port_blacklist.add(port)

            if not force:
                logger.warning(f"Port {port}: {result['error']}")
                return result

        # Attempt graceful shutdown first
        try:
            proc = psutil.Process(pid)

            # Send SIGTERM
            logger.info(f"Sending SIGTERM to process {pid} on port {port}")
            proc.terminate()

            # Wait for graceful shutdown
            try:
                await asyncio.wait_for(
                    asyncio.get_event_loop().run_in_executor(
                        None, proc.wait, self.config.graceful_shutdown_timeout
                    ),
                    timeout=self.config.graceful_shutdown_timeout + 1
                )
                result['process_killed'] = True
                result['cleaned'] = True
                logger.info(f"Process {pid} terminated gracefully")
                return result
            except (asyncio.TimeoutError, psutil.TimeoutExpired):
                pass

            # Force kill with SIGKILL
            logger.warning(f"Process {pid} didn't terminate gracefully, sending SIGKILL")
            await asyncio.sleep(self.config.force_kill_delay)

            try:
                proc.kill()
                await asyncio.wait_for(
                    asyncio.get_event_loop().run_in_executor(None, proc.wait, 2.0),
                    timeout=3.0
                )
                result['process_killed'] = True
                result['cleaned'] = True
                logger.info(f"Process {pid} killed with SIGKILL")
            except (psutil.NoSuchProcess, asyncio.TimeoutError):
                # Process might already be gone
                result['process_killed'] = True
                result['cleaned'] = True

        except psutil.NoSuchProcess:
            result['cleaned'] = True  # Process already gone
        except psutil.AccessDenied:
            result['error'] = f"Access denied killing process {pid}"
        except Exception as e:
            result['error'] = str(e)

        return result

    async def cleanup_all_stuck_processes(self) -> Dict[str, Any]:
        """
        Find and clean up all stuck processes (ML loaders, zombies, etc).

        Returns comprehensive cleanup report.
        """
        report = {
            'ml_processes_cleaned': 0,
            'zombies_cleaned': 0,
            'port_processes_cleaned': 0,
            'unkillable_found': [],
            'errors': [],
            'cleanup_time_ms': 0,
        }

        start_time = time.time()

        # 1. Clean stuck ML processes
        stuck_ml = find_stuck_ml_processes()
        for proc_info in stuck_ml:
            pid = proc_info['pid']
            try:
                if kill_process_tree(pid):
                    report['ml_processes_cleaned'] += 1
                    logger.info(f"Killed stuck ML process: {pid}")
            except Exception as e:
                report['errors'].append(f"ML cleanup {pid}: {e}")

        # 2. Clean zombie processes
        try:
            for proc in psutil.process_iter(['pid', 'status', 'cmdline']):
                try:
                    if proc.status() == 'zombie':
                        cmdline = ' '.join(proc.cmdline() or [])
                        if any(kw in cmdline.lower() for kw in ['jarvis', 'python', 'uvicorn']):
                            proc.wait(timeout=0.1)
                            report['zombies_cleaned'] += 1
                except (psutil.NoSuchProcess, psutil.TimeoutExpired):
                    pass
        except Exception as e:
            report['errors'].append(f"Zombie cleanup: {e}")

        # 3. Clean processes on all configured ports
        all_ports = [self.config.primary_port] + self.config.fallback_ports

        for port in all_ports:
            process_info = await self._get_process_on_port(port)
            if process_info:
                status = process_info.get('status', '')
                pid = process_info['pid']

                # Check if stuck (not responding, wrong state, etc)
                is_stuck = (
                    self._is_unkillable_state(status) or
                    status in ['stopped', 'zombie', 'disk-sleep']
                )

                if is_stuck:
                    if self._is_unkillable_state(status):
                        report['unkillable_found'].append({
                            'port': port,
                            'pid': pid,
                            'status': status,
                        })
                        self._port_blacklist.add(port)
                    else:
                        cleanup_result = await self.cleanup_port(port)
                        if cleanup_result['cleaned']:
                            report['port_processes_cleaned'] += 1

        report['cleanup_time_ms'] = (time.time() - start_time) * 1000

        total_cleaned = (
            report['ml_processes_cleaned'] +
            report['zombies_cleaned'] +
            report['port_processes_cleaned']
        )

        if total_cleaned > 0 or report['unkillable_found']:
            logger.info(
                f"Cleanup complete: {total_cleaned} processes cleaned, "
                f"{len(report['unkillable_found'])} unkillable found"
            )

        return report

    # =========================================================================
    # Self-Healing Loop
    # =========================================================================

    async def cleanup_and_discover_port(self) -> Dict[str, Any]:
        """
        Full cleanup and port discovery - main entry point.

        1. Cleans up all stuck processes
        2. Discovers healthy ports
        3. Returns the best available port
        """
        # Phase 1: Cleanup
        cleanup_report = await self.cleanup_all_stuck_processes()

        # Phase 2: Discover
        discovery = await self.discover_healthy_port()

        # Combine results
        return {
            'selected_port': discovery['selected_port'],
            'cleanup': cleanup_report,
            'discovery': discovery,
            'blacklisted_ports': list(self._port_blacklist),
        }

    async def start_healing_loop(self) -> asyncio.Task:
        """
        Start background self-healing loop.

        Periodically checks port health and performs healing as needed.
        """
        if self._healing_task and not self._healing_task.done():
            logger.warning("Healing loop already running")
            return self._healing_task

        async def _healing_loop():
            logger.info(f"Self-healing loop started (interval: {self.config.healing_interval_seconds}s)")

            while not self._shutdown_event.is_set():
                try:
                    # Check if current port is still healthy
                    if self._last_healthy_port:
                        health = await self.check_port_health(self._last_healthy_port)

                        if not health.healthy:
                            logger.warning(f"Port {self._last_healthy_port} became unhealthy, initiating healing")

                            # Track healing attempts
                            attempts = self._healing_attempts.get(self._last_healthy_port, 0)

                            if attempts < self.config.max_healing_attempts:
                                self._healing_attempts[self._last_healthy_port] = attempts + 1

                                # Try cleanup
                                cleanup = await self.cleanup_port(self._last_healthy_port)

                                if not cleanup['cleaned']:
                                    # Find alternative port
                                    discovery = await self.discover_healthy_port()
                                    if discovery['selected_port']:
                                        logger.info(f"Failing over to port {discovery['selected_port']}")
                            else:
                                logger.warning(
                                    f"Max healing attempts reached for port {self._last_healthy_port}, "
                                    "blacklisting and finding alternative"
                                )
                                self._port_blacklist.add(self._last_healthy_port)
                                await self.discover_healthy_port()

                    # Wait for next interval
                    await asyncio.sleep(self.config.healing_interval_seconds)

                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"Error in healing loop: {e}")
                    await asyncio.sleep(5)  # Brief pause on error

            logger.info("Self-healing loop stopped")

        self._healing_task = asyncio.create_task(_healing_loop())
        return self._healing_task

    def get_status(self) -> Dict[str, Any]:
        """Get current healer status."""
        return {
            'last_healthy_port': self._last_healthy_port,
            'blacklisted_ports': list(self._port_blacklist),
            'healing_attempts': dict(self._healing_attempts),
            'healing_loop_running': self._healing_task is not None and not self._healing_task.done(),
            'config': {
                'primary_port': self.config.primary_port,
                'fallback_ports': self.config.fallback_ports,
                'healing_enabled': self.config.healing_enabled,
            }
        }


# Global self-healer instance
_self_healer: Optional[AdvancedProcessSelfHealer] = None


def get_self_healer() -> AdvancedProcessSelfHealer:
    """Get the global self-healer instance."""
    global _self_healer
    if _self_healer is None:
        _self_healer = AdvancedProcessSelfHealer()
    return _self_healer


# =============================================================================
# Exceptions
# =============================================================================

class MLLoadTimeout(Exception):
    """Raised when ML model loading times out."""
    def __init__(self, operation: str, timeout: float, message: str = ""):
        self.operation = operation
        self.timeout = timeout
        self.timestamp = datetime.now()
        super().__init__(message or f"ML operation '{operation}' timed out after {timeout}s")


class MLLoadError(Exception):
    """Raised when ML model loading fails."""
    def __init__(self, operation: str, error: str, message: str = ""):
        self.operation = operation
        self.error = error
        self.timestamp = datetime.now()
        super().__init__(message or f"ML operation '{operation}' failed: {error}")


# =============================================================================
# Process Cleanup Utilities
# =============================================================================

def find_stuck_ml_processes(marker: str = "JARVIS_ML_LOADER") -> List[Dict[str, Any]]:
    """
    Find ML loader processes that appear stuck.

    Returns list of process info dicts with pid, age, status, etc.
    """
    stuck_processes = []

    try:
        for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'status', 'create_time']):
            try:
                info = proc.info
                cmdline = ' '.join(info.get('cmdline', []) or [])

                # Check if this is an ML loader process
                if marker in cmdline or 'speechbrain' in cmdline.lower() or 'torch' in cmdline.lower():
                    age = time.time() - (info.get('create_time', time.time()))
                    status = info.get('status', '')

                    # Consider stuck if:
                    # - In uninterruptible sleep (disk-sleep) state
                    # - Or running for too long with low CPU
                    is_stuck = False
                    stuck_reason = ""

                    if status in ['disk-sleep', 'stopped', 'zombie']:
                        is_stuck = True
                        stuck_reason = f"Process in {status} state"
                    elif age > 120:  # 2 minutes old
                        try:
                            cpu = proc.cpu_percent(interval=0.5)
                            if cpu < 1.0:  # Essentially idle
                                is_stuck = True
                                stuck_reason = f"Idle for {age:.0f}s (CPU: {cpu:.1f}%)"
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            pass

                    if is_stuck or age > 300:  # Always flag if > 5 minutes old
                        stuck_processes.append({
                            'pid': info['pid'],
                            'name': info.get('name', 'unknown'),
                            'status': status,
                            'age_seconds': age,
                            'cmdline': cmdline[:100],
                            'stuck_reason': stuck_reason or f"Age: {age:.0f}s",
                        })

            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

    except Exception as e:
        logger.warning(f"Error finding stuck ML processes: {e}")

    return stuck_processes


def kill_process_tree(pid: int, timeout: float = 5.0) -> bool:
    """
    Kill a process and all its children.

    Uses SIGTERM first, then SIGKILL if needed.
    Returns True if process was killed successfully.
    """
    try:
        parent = psutil.Process(pid)
        children = parent.children(recursive=True)

        # Kill children first
        for child in children:
            try:
                child.terminate()
            except psutil.NoSuchProcess:
                pass

        # Terminate parent
        parent.terminate()

        # Wait for graceful termination
        gone, alive = psutil.wait_procs([parent] + children, timeout=timeout)

        # Force kill any survivors
        for proc in alive:
            try:
                logger.warning(f"Force killing stuck process PID {proc.pid}")
                proc.kill()
            except psutil.NoSuchProcess:
                pass

        # Final wait
        psutil.wait_procs(alive, timeout=2.0)

        return True

    except psutil.NoSuchProcess:
        return True  # Already dead
    except Exception as e:
        logger.error(f"Error killing process tree {pid}: {e}")
        return False


async def cleanup_stuck_ml_processes(
    marker: str = "JARVIS_ML_LOADER",
    max_age_seconds: float = 300.0
) -> int:
    """
    Clean up any stuck ML loader processes.

    Should be called at startup to clear any zombie processes from
    previous crashed sessions.

    Returns number of processes cleaned up.
    """
    stuck = find_stuck_ml_processes(marker)
    cleaned = 0

    for proc_info in stuck:
        if proc_info['age_seconds'] > max_age_seconds or 'disk-sleep' in str(proc_info.get('status', '')):
            pid = proc_info['pid']
            logger.warning(
                f"Cleaning up stuck ML process: PID {pid} "
                f"(age: {proc_info['age_seconds']:.0f}s, reason: {proc_info['stuck_reason']})"
            )

            if await asyncio.to_thread(kill_process_tree, pid):
                cleaned += 1
                logger.info(f"Successfully cleaned up PID {pid}")
            else:
                logger.error(f"Failed to clean up PID {pid} - may require manual intervention")

    if cleaned > 0:
        logger.info(f"Cleaned up {cleaned} stuck ML processes")

    return cleaned


# =============================================================================
# Worker Process Functions (Run in Subprocess)
# =============================================================================

def _worker_load_speechbrain_model(
    model_name: str,
    save_dir: str,
    device: str,
    result_file: str
) -> None:
    """
    Worker function that runs in a separate process to load SpeechBrain model.

    Writes result to a file to avoid pickling the model.
    """
    # Set process title for identification
    try:
        import setproctitle
        setproctitle.setproctitle(f"JARVIS_ML_LOADER: {model_name}")
    except ImportError:
        pass

    result = {
        'success': False,
        'error': None,
        'model_info': None,
        'load_time_ms': 0,
    }

    start_time = time.perf_counter()

    try:
        # Limit torch threads to prevent CPU overload
        import torch
        torch.set_num_threads(2)

        # Import SpeechBrain (v93.0: Updated for SpeechBrain 1.0+ compatibility)
        try:
            from speechbrain.inference import EncoderClassifier
        except ImportError:
            # Fallback for older SpeechBrain versions
            from speechbrain.pretrained import EncoderClassifier

        # Load the model
        logger.info(f"[Worker] Loading SpeechBrain model: {model_name}")

        model = EncoderClassifier.from_hparams(
            source=model_name,
            savedir=save_dir,
            run_opts={"device": device},
        )

        # Get model info (can't pickle the model itself)
        result['success'] = True
        result['model_info'] = {
            'name': model_name,
            'device': device,
            'save_dir': save_dir,
            'loaded': True,
        }
        result['load_time_ms'] = (time.perf_counter() - start_time) * 1000

        logger.info(f"[Worker] Model loaded successfully in {result['load_time_ms']:.0f}ms")

    except Exception as e:
        result['error'] = f"{type(e).__name__}: {str(e)}"
        result['traceback'] = traceback.format_exc()
        logger.error(f"[Worker] Model loading failed: {result['error']}")

    # Write result to file
    try:
        with open(result_file, 'wb') as f:
            pickle.dump(result, f)
    except Exception as e:
        logger.error(f"[Worker] Failed to write result: {e}")


def _worker_generic_loader(
    loader_func_pickled: bytes,
    args: tuple,
    kwargs: dict,
    result_file: str
) -> None:
    """
    Worker function that runs a generic loader function in a separate process.
    """
    result = {
        'success': False,
        'error': None,
        'result': None,
        'load_time_ms': 0,
    }

    start_time = time.perf_counter()

    try:
        # Unpickle the loader function
        loader_func = pickle.loads(loader_func_pickled)

        # Execute the loader
        load_result = loader_func(*args, **kwargs)

        result['success'] = True
        result['result'] = load_result
        result['load_time_ms'] = (time.perf_counter() - start_time) * 1000

    except Exception as e:
        result['error'] = f"{type(e).__name__}: {str(e)}"
        result['traceback'] = traceback.format_exc()

    # Write result to file
    try:
        with open(result_file, 'wb') as f:
            pickle.dump(result, f)
    except Exception as e:
        logger.error(f"[Worker] Failed to write result: {e}")


# =============================================================================
# Main Async Interface
# =============================================================================

class ProcessIsolatedMLLoader:
    """
    Loads ML models in isolated processes with true timeout/kill capability.

    This solves the fundamental problem where asyncio timeouts don't work
    when synchronous PyTorch/SpeechBrain code blocks the event loop.
    """

    def __init__(self, config: Optional[MLLoaderConfig] = None):
        self.config = config or get_ml_loader_config()
        self._initialized = False
        self._active_processes: Dict[int, multiprocessing.Process] = {}
        self._stats = {
            'total_loads': 0,
            'successful_loads': 0,
            'timeout_loads': 0,
            'error_loads': 0,
            'processes_killed': 0,
            'total_load_time_ms': 0.0,
        }

    async def initialize(self) -> None:
        """Initialize the loader, cleaning up any stuck processes."""
        if self._initialized:
            return

        if self.config.cleanup_on_start:
            cleaned = await cleanup_stuck_ml_processes(self.config.process_marker)
            if cleaned > 0:
                logger.info(f"Cleaned up {cleaned} stuck ML processes from previous session")

        self._initialized = True
        logger.info("Process-isolated ML loader initialized")

    async def load_speechbrain_model(
        self,
        model_name: str,
        save_dir: Optional[str] = None,
        device: Optional[str] = None,
        timeout: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        Load a SpeechBrain model in an isolated process.

        Args:
            model_name: HuggingFace model name (e.g., "speechbrain/spkrec-ecapa-voxceleb")
            save_dir: Directory to save model files
            device: Device to load model on ("cpu", "cuda", "mps")
            timeout: Timeout in seconds (default from config)

        Returns:
            Dict with success status and model info

        Raises:
            MLLoadTimeout: If loading times out
            MLLoadError: If loading fails
        """
        await self.initialize()

        timeout = timeout or self.config.default_timeout
        save_dir = save_dir or str(Path.home() / ".jarvis" / "models" / "speechbrain")
        device = device or self._get_optimal_device()

        # Create temp file for result (avoids pickling large model objects)
        result_file = tempfile.mktemp(suffix='.pkl', prefix='jarvis_ml_')

        self._stats['total_loads'] += 1
        start_time = time.perf_counter()

        try:
            logger.info(f"Loading SpeechBrain model in isolated process: {model_name}")

            # Create and start worker process
            process = multiprocessing.Process(
                target=_worker_load_speechbrain_model,
                args=(model_name, save_dir, device, result_file),
                name=f"{self.config.process_marker}_{model_name.replace('/', '_')}"
            )
            process.start()
            self._active_processes[process.pid] = process

            # Wait for completion with timeout
            result = await self._wait_for_process(
                process,
                result_file,
                timeout,
                f"SpeechBrain:{model_name}"
            )

            if result['success']:
                self._stats['successful_loads'] += 1
                self._stats['total_load_time_ms'] += result.get('load_time_ms', 0)
                logger.info(f"SpeechBrain model loaded: {model_name} ({result.get('load_time_ms', 0):.0f}ms)")
            else:
                self._stats['error_loads'] += 1
                raise MLLoadError("SpeechBrain", result.get('error', 'Unknown error'))

            return result

        except asyncio.TimeoutError:
            self._stats['timeout_loads'] += 1
            elapsed = (time.perf_counter() - start_time) * 1000
            logger.error(f"SpeechBrain model load TIMEOUT after {elapsed:.0f}ms: {model_name}")
            raise MLLoadTimeout("SpeechBrain", timeout)

        finally:
            # Cleanup
            if os.path.exists(result_file):
                try:
                    os.remove(result_file)
                except Exception:
                    pass

    async def load_generic(
        self,
        loader_func: Callable[..., T],
        *args,
        timeout: Optional[float] = None,
        operation_name: str = "GenericML",
        **kwargs
    ) -> T:
        """
        Run any ML loading function in an isolated process.

        Args:
            loader_func: Function to execute (must be picklable)
            *args: Arguments for the function
            timeout: Timeout in seconds
            operation_name: Name for logging
            **kwargs: Keyword arguments for the function

        Returns:
            The result of loader_func
        """
        await self.initialize()

        timeout = timeout or self.config.default_timeout
        result_file = tempfile.mktemp(suffix='.pkl', prefix='jarvis_ml_')

        self._stats['total_loads'] += 1
        start_time = time.perf_counter()

        try:
            logger.info(f"Loading {operation_name} in isolated process")

            # Pickle the loader function
            loader_pickled = pickle.dumps(loader_func)

            # Create and start worker process
            process = multiprocessing.Process(
                target=_worker_generic_loader,
                args=(loader_pickled, args, kwargs, result_file),
                name=f"{self.config.process_marker}_{operation_name}"
            )
            process.start()
            self._active_processes[process.pid] = process

            # Wait for completion with timeout
            result = await self._wait_for_process(
                process,
                result_file,
                timeout,
                operation_name
            )

            if result['success']:
                self._stats['successful_loads'] += 1
                self._stats['total_load_time_ms'] += result.get('load_time_ms', 0)
                return result['result']
            else:
                self._stats['error_loads'] += 1
                raise MLLoadError(operation_name, result.get('error', 'Unknown error'))

        except asyncio.TimeoutError:
            self._stats['timeout_loads'] += 1
            raise MLLoadTimeout(operation_name, timeout)

        finally:
            if os.path.exists(result_file):
                try:
                    os.remove(result_file)
                except Exception:
                    pass

    async def _wait_for_process(
        self,
        process: multiprocessing.Process,
        result_file: str,
        timeout: float,
        operation_name: str
    ) -> Dict[str, Any]:
        """Wait for a worker process to complete with timeout."""

        start_time = time.perf_counter()
        check_interval = 0.1  # Check every 100ms

        while True:
            elapsed = time.perf_counter() - start_time

            # Check timeout
            if elapsed > timeout:
                await self._kill_process(process, operation_name)
                raise asyncio.TimeoutError()

            # Check if process completed
            if not process.is_alive():
                break

            # Yield to event loop
            await asyncio.sleep(check_interval)

        # Clean up tracking
        self._active_processes.pop(process.pid, None)

        # Read result from file
        if os.path.exists(result_file):
            try:
                with open(result_file, 'rb') as f:
                    return pickle.load(f)
            except Exception as e:
                return {
                    'success': False,
                    'error': f"Failed to read result: {e}",
                }
        else:
            return {
                'success': False,
                'error': "Worker process did not produce result",
            }

    async def _kill_process(
        self,
        process: multiprocessing.Process,
        operation_name: str
    ) -> None:
        """Kill a worker process that has timed out."""

        pid = process.pid
        logger.warning(f"Killing stuck ML process: {operation_name} (PID {pid})")

        try:
            # Try graceful termination first
            process.terminate()

            # Wait briefly for graceful shutdown
            await asyncio.sleep(self.config.graceful_shutdown_timeout)

            if process.is_alive():
                # Force kill
                logger.warning(f"Force killing PID {pid}")
                process.kill()
                await asyncio.sleep(0.5)

            # Clean up zombie
            process.join(timeout=1.0)

            self._stats['processes_killed'] += 1
            self._active_processes.pop(pid, None)

        except Exception as e:
            logger.error(f"Error killing process {pid}: {e}")

    def _get_optimal_device(self) -> str:
        """Determine optimal device for ML models."""
        try:
            import torch
            if torch.cuda.is_available():
                return "cuda"
            elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
                return "mps"
        except ImportError:
            pass
        return "cpu"

    def get_stats(self) -> Dict[str, Any]:
        """Get loader statistics."""
        return {
            **self._stats,
            'success_rate': (
                self._stats['successful_loads'] / max(1, self._stats['total_loads'])
            ),
            'avg_load_time_ms': (
                self._stats['total_load_time_ms'] / max(1, self._stats['successful_loads'])
            ),
            'active_processes': len(self._active_processes),
        }

    async def shutdown(self) -> None:
        """Shutdown the loader, killing any active processes."""
        logger.info("Shutting down process-isolated ML loader...")

        for pid, process in list(self._active_processes.items()):
            await self._kill_process(process, f"shutdown:{pid}")

        self._active_processes.clear()
        logger.info("ML loader shutdown complete")


# =============================================================================
# Singleton Instance with Proper Cleanup
# =============================================================================

_loader_instance: Optional[ProcessIsolatedMLLoader] = None
_atexit_registered: bool = False


def _sync_cleanup_processes() -> None:
    """
    v93.0: Synchronous cleanup for atexit handler.

    This ensures child processes are killed when the parent exits,
    preventing leaked semaphores and zombie processes.
    """
    global _loader_instance
    if _loader_instance is None:
        return

    try:
        for pid, process in list(_loader_instance._active_processes.items()):
            try:
                if process.is_alive():
                    logger.debug(f"[atexit] Terminating ML process {pid}")
                    process.terminate()
                    process.join(timeout=2.0)

                    if process.is_alive():
                        logger.warning(f"[atexit] Force killing ML process {pid}")
                        process.kill()
                        process.join(timeout=1.0)
            except Exception as e:
                logger.debug(f"[atexit] Error cleaning process {pid}: {e}")

        _loader_instance._active_processes.clear()
        logger.debug("[atexit] ML loader processes cleaned up")
    except Exception as e:
        logger.debug(f"[atexit] Cleanup error: {e}")


def get_ml_loader() -> ProcessIsolatedMLLoader:
    """Get the global ML loader instance."""
    global _loader_instance, _atexit_registered
    if _loader_instance is None:
        _loader_instance = ProcessIsolatedMLLoader()

        # v93.0: Register atexit handler for proper cleanup
        if not _atexit_registered:
            atexit.register(_sync_cleanup_processes)
            _atexit_registered = True
            logger.debug("ML loader atexit cleanup handler registered")

    return _loader_instance


async def load_model_isolated(
    loader_func: Callable[..., T],
    *args,
    timeout: float = 60.0,
    operation_name: str = "MLModel",
    **kwargs
) -> T:
    """
    Convenience function to load any model in an isolated process.

    Usage:
        model = await load_model_isolated(
            my_loader_function,
            model_name="my-model",
            timeout=30.0,
            operation_name="MyModel"
        )
    """
    loader = get_ml_loader()
    return await loader.load_generic(
        loader_func, *args,
        timeout=timeout,
        operation_name=operation_name,
        **kwargs
    )


async def load_speechbrain_model(
    model_name: str,
    save_dir: Optional[str] = None,
    device: Optional[str] = None,
    timeout: float = 60.0
) -> Dict[str, Any]:
    """
    Convenience function to load a SpeechBrain model in an isolated process.

    Usage:
        result = await load_speechbrain_model(
            "speechbrain/spkrec-ecapa-voxceleb",
            timeout=30.0
        )
    """
    loader = get_ml_loader()
    return await loader.load_speechbrain_model(
        model_name=model_name,
        save_dir=save_dir,
        device=device,
        timeout=timeout
    )


# =============================================================================
# Async-Safe Model Loading Wrappers
# =============================================================================

async def load_model_async(
    sync_loader: Callable[[], T],
    timeout: float = 30.0,
    operation_name: str = "Model",
    use_thread: bool = True,
    use_process: bool = False
) -> Optional[T]:
    """
    Universal wrapper to load ANY model asynchronously with timeout.

    This is the recommended way to load ML models in async code.

    Args:
        sync_loader: Synchronous function that loads and returns the model
        timeout: Maximum time to wait
        operation_name: Name for logging
        use_thread: Use thread pool (faster, but can't kill if stuck)
        use_process: Use process pool (slower, but truly killable)

    Returns:
        The loaded model, or None if loading failed/timed out

    Example:
        model = await load_model_async(
            lambda: SomeModel.from_pretrained("model-name"),
            timeout=30.0,
            operation_name="SomeModel"
        )
    """
    logger.info(f"Loading {operation_name} asynchronously (timeout: {timeout}s)...")
    start_time = time.perf_counter()

    try:
        if use_process:
            # Use process isolation for truly killable loading
            result = await load_model_isolated(
                sync_loader,
                timeout=timeout,
                operation_name=operation_name
            )
            elapsed = (time.perf_counter() - start_time) * 1000
            logger.info(f"{operation_name} loaded in process isolation ({elapsed:.0f}ms)")
            return result
        else:
            # Use thread pool - faster but can't kill if truly stuck
            result = await asyncio.wait_for(
                asyncio.to_thread(sync_loader),
                timeout=timeout
            )
            elapsed = (time.perf_counter() - start_time) * 1000
            logger.info(f"{operation_name} loaded in thread pool ({elapsed:.0f}ms)")
            return result

    except asyncio.TimeoutError:
        elapsed = (time.perf_counter() - start_time) * 1000
        logger.error(f"{operation_name} TIMEOUT after {elapsed:.0f}ms")
        return None

    except Exception as e:
        elapsed = (time.perf_counter() - start_time) * 1000
        logger.error(f"{operation_name} FAILED after {elapsed:.0f}ms: {e}")
        return None


# =============================================================================
# Startup Cleanup (Enhanced with Self-Healing)
# =============================================================================

async def cleanup_before_startup(
    port: Optional[int] = None,
    enable_healing_loop: bool = False,
    config: Optional[SelfHealingConfig] = None
) -> Dict[str, Any]:
    """
    Enhanced cleanup with dynamic port discovery and self-healing.

    This function uses the AdvancedProcessSelfHealer to:
    1. Clean up all stuck processes (ML, zombies, port blockers)
    2. Detect macOS UE (unkillable) states and blacklist those ports
    3. Discover the best available healthy port IN PARALLEL
    4. Optionally start a background self-healing loop

    Args:
        port: Deprecated - port is now discovered dynamically from config.
              If provided, it's used as a hint but not required.
        enable_healing_loop: If True, starts background healing loop
        config: Optional custom SelfHealingConfig

    Returns:
        Dict with comprehensive cleanup results including:
        - selected_port: The best available port to use
        - cleanup: Detailed cleanup report
        - discovery: Port discovery results
        - blacklisted_ports: Ports with unkillable processes
        - healer_status: Self-healer state
    """
    logger.info("Performing enhanced pre-startup cleanup with self-healing...")

    # Get or create self-healer with config
    healer = get_self_healer()

    # Override config if provided
    if config:
        healer.config = config

    # If a specific port was requested, use it as primary
    if port is not None:
        logger.debug(f"Port {port} specified as hint, adding to config")
        if port not in healer.config.fallback_ports:
            healer.config.fallback_ports.insert(0, port)

    try:
        # Full cleanup and discovery
        result = await healer.cleanup_and_discover_port()

        # Add ML process cleanup count (separate from port cleanup)
        ml_cleaned = await cleanup_stuck_ml_processes()
        if 'cleanup' not in result:
            result['cleanup'] = {}
        result['cleanup']['ml_processes_via_legacy'] = ml_cleaned

        # Start healing loop if requested
        if enable_healing_loop and healer.config.healing_enabled:
            await healer.start_healing_loop()
            result['healing_loop_started'] = True

        # Add healer status
        result['healer_status'] = healer.get_status()

        # Summary logging
        selected = result.get('selected_port')
        blacklisted = result.get('blacklisted_ports', [])
        cleanup = result.get('cleanup', {})

        total_cleaned = (
            cleanup.get('ml_processes_cleaned', 0) +
            cleanup.get('zombies_cleaned', 0) +
            cleanup.get('port_processes_cleaned', 0) +
            ml_cleaned
        )

        if total_cleaned > 0 or blacklisted:
            logger.info(
                f"Enhanced cleanup complete: {total_cleaned} processes cleaned, "
                f"{len(blacklisted)} ports blacklisted (unkillable), "
                f"selected port: {selected}"
            )
        else:
            logger.info(f"Enhanced cleanup complete: nothing to clean, selected port: {selected}")

        # Backwards compatibility: add legacy result keys
        result['ml_processes_cleaned'] = cleanup.get('ml_processes_cleaned', 0) + ml_cleaned
        result['port_freed'] = cleanup.get('port_processes_cleaned', 0) > 0
        result['zombies_cleaned'] = cleanup.get('zombies_cleaned', 0)

        return result

    except Exception as e:
        logger.error(f"Error in enhanced cleanup: {e}")
        # Fall back to legacy behavior
        return await _legacy_cleanup_before_startup(port or 8011)


async def _legacy_cleanup_before_startup(port: int) -> Dict[str, Any]:
    """
    Legacy cleanup function as fallback if self-healer fails.
    """
    results = {
        'ml_processes_cleaned': 0,
        'port_freed': False,
        'zombies_cleaned': 0,
        'selected_port': port,
        'fallback_used': True,
    }

    logger.warning("Using legacy cleanup (self-healer unavailable)")

    # 1. Clean up stuck ML processes
    results['ml_processes_cleaned'] = await cleanup_stuck_ml_processes()

    # 2. Check if port is blocked by a stuck process
    try:
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            result = s.connect_ex(('localhost', port))
            if result == 0:
                logger.warning(f"Port {port} is in use, checking for stuck process...")

                for conn in psutil.net_connections(kind='inet'):
                    if hasattr(conn.laddr, 'port') and conn.laddr.port == port and conn.status == 'LISTEN':
                        pid = conn.pid
                        if pid:
                            try:
                                proc = psutil.Process(pid)
                                status = proc.status()

                                if status in ['disk-sleep', 'zombie', 'stopped', 'uninterruptible']:
                                    logger.warning(f"Found stuck process on port {port}: PID {pid} ({status})")
                                    if await asyncio.to_thread(kill_process_tree, pid):
                                        results['port_freed'] = True
                                        logger.info(f"Freed port {port} by killing stuck process {pid}")
                            except (psutil.NoSuchProcess, psutil.AccessDenied):
                                pass
    except Exception as e:
        logger.debug(f"Port check error: {e}")

    # 3. Clean up any zombie processes
    try:
        for proc in psutil.process_iter(['pid', 'status', 'cmdline']):
            try:
                if proc.status() == 'zombie':
                    cmdline = ' '.join(proc.cmdline() or [])
                    if any(kw in cmdline.lower() for kw in ['jarvis', 'python', 'uvicorn']):
                        proc.wait(timeout=0.1)
                        results['zombies_cleaned'] += 1
            except (psutil.NoSuchProcess, psutil.TimeoutExpired):
                pass
    except Exception as e:
        logger.debug(f"Zombie cleanup error: {e}")

    total_cleaned = (
        results['ml_processes_cleaned'] +
        results['zombies_cleaned'] +
        (1 if results['port_freed'] else 0)
    )

    if total_cleaned > 0:
        logger.info(f"Legacy cleanup complete: {total_cleaned} items cleaned")

    return results


async def discover_and_start_backend(
    preferred_port: Optional[int] = None,
    start_healing: bool = True
) -> Dict[str, Any]:
    """
    Convenience function for backend startup.

    Performs full cleanup, discovers the best port, and optionally
    starts the self-healing loop.

    Args:
        preferred_port: Optional preferred port (will be checked first)
        start_healing: Whether to start background healing loop

    Returns:
        Dict with:
        - selected_port: Port to use for backend
        - cleanup_report: Cleanup results
        - healer: Reference to self-healer instance
    """
    healer = get_self_healer()

    # If preferred port specified, prioritize it
    if preferred_port and preferred_port != healer.config.primary_port:
        healer.config.primary_port = preferred_port

    # Full cleanup and discovery
    result = await healer.cleanup_and_discover_port()

    # Start healing loop
    if start_healing:
        await healer.start_healing_loop()

    return {
        'selected_port': result['selected_port'],
        'cleanup_report': result.get('cleanup', {}),
        'blacklisted_ports': result.get('blacklisted_ports', []),
        'healer': healer,
    }


# =============================================================================
# Shutdown Manager Integration
# =============================================================================

def register_shutdown_hooks() -> bool:
    """
    v93.0: Register ML loader cleanup with coordinated shutdown manager.

    This ensures proper cleanup during graceful shutdown, complementing
    the atexit handler which handles abrupt termination.

    Returns:
        True if hooks were registered, False if shutdown manager unavailable
    """
    try:
        from backend.core.coordinated_shutdown import (
            get_shutdown_manager,
            ShutdownPhase,
            ShutdownHook,
        )

        manager = get_shutdown_manager()
        if manager is None:
            logger.debug("Shutdown manager not available - using atexit only")
            return False

        async def ml_cleanup_hook():
            """Cleanup ML processes during coordinated shutdown."""
            global _loader_instance
            if _loader_instance is not None:
                logger.info("[Shutdown] Cleaning up ML loader processes...")
                await _loader_instance.shutdown()

        manager.register_hook(
            ShutdownHook(
                name="ml_loader_cleanup",
                callback=ml_cleanup_hook,
                phase=ShutdownPhase.CLEANUP,
                priority=90,  # High priority - clean processes early
                timeout=10.0,
                critical=False,  # Don't block shutdown if this fails
            )
        )
        logger.debug("ML loader shutdown hook registered")
        return True

    except ImportError:
        logger.debug("Shutdown manager not available - using atexit only")
        return False
    except Exception as e:
        logger.debug(f"Failed to register shutdown hook: {e}")
        return False


# =============================================================================
# Module Entry Point (for testing)
# =============================================================================

if __name__ == "__main__":
    async def test_self_healer():
        """Test the Advanced Self-Healing Process Manager."""
        print("=" * 60)
        print("Testing Advanced Self-Healing Process Manager")
        print("=" * 60)

        # Get healer instance
        healer = get_self_healer()
        print(f"\nConfig loaded:")
        print(f"  Primary port: {healer.config.primary_port}")
        print(f"  Fallback ports: {healer.config.fallback_ports}")
        print(f"  UE indicators: {healer.config.ue_state_indicators}")

        # Test parallel port discovery
        print("\n--- Testing Parallel Port Discovery ---")
        discovery = await healer.discover_healthy_port()
        print(f"Discovery completed in {discovery['discovery_time_ms']:.0f}ms")
        print(f"Selected port: {discovery['selected_port']}")
        print(f"Unkillable ports: {discovery['unkillable_ports']}")

        for result in discovery['all_results']:
            status = "HEALTHY" if result.healthy else "UNHEALTHY"
            unkillable = " [UNKILLABLE]" if result.is_unkillable else ""
            time_ms = f"{result.response_time_ms:.0f}ms" if result.response_time_ms else "N/A"
            error = f" ({result.error})" if result.error else ""
            print(f"  Port {result.port}: {status}{unkillable} - {time_ms}{error}")

        # Test full cleanup and discovery
        print("\n--- Testing Full Cleanup and Discovery ---")
        full_result = await healer.cleanup_and_discover_port()
        print(f"Selected port: {full_result['selected_port']}")
        print(f"Blacklisted ports: {full_result['blacklisted_ports']}")
        cleanup = full_result.get('cleanup', {})
        print(f"Cleanup results:")
        print(f"  ML processes cleaned: {cleanup.get('ml_processes_cleaned', 0)}")
        print(f"  Zombies cleaned: {cleanup.get('zombies_cleaned', 0)}")
        print(f"  Port processes cleaned: {cleanup.get('port_processes_cleaned', 0)}")
        print(f"  Unkillable found: {len(cleanup.get('unkillable_found', []))}")

        # Test healer status
        print("\n--- Healer Status ---")
        status = healer.get_status()
        print(f"Last healthy port: {status['last_healthy_port']}")
        print(f"Blacklisted ports: {status['blacklisted_ports']}")
        print(f"Healing loop running: {status['healing_loop_running']}")

        # Clean up
        await healer.close()
        print("\n✓ Self-healer test completed successfully!")

    async def test_ml_loader():
        """Test the ML loader functionality."""
        print("\n" + "=" * 60)
        print("Testing Process-Isolated ML Loader")
        print("=" * 60)

        # Clean up first
        cleaned = await cleanup_stuck_ml_processes()
        print(f"Cleaned {cleaned} stuck processes")

        # Test loading a simple function
        loader = get_ml_loader()

        def slow_function():
            import time
            time.sleep(2)
            return "done"

        try:
            result = await loader.load_generic(
                slow_function,
                timeout=5.0,
                operation_name="SlowTest"
            )
            print(f"Result: {result}")
        except Exception as e:
            print(f"Error: {e}")

        print(f"Stats: {loader.get_stats()}")
        await loader.shutdown()
        print("✓ ML loader test completed!")

    async def main():
        """Run all tests."""
        import sys
        test_type = sys.argv[1] if len(sys.argv) > 1 else "all"

        if test_type in ["self-healer", "healer", "all"]:
            await test_self_healer()

        if test_type in ["ml-loader", "loader", "all"]:
            await test_ml_loader()

        if test_type not in ["self-healer", "healer", "ml-loader", "loader", "all"]:
            print(f"Unknown test type: {test_type}")
            print("Usage: python process_isolated_ml_loader.py [self-healer|ml-loader|all]")

    asyncio.run(main())
