#!/usr/bin/env python3
"""
JARVIS Supervisor Singleton v115.0
==================================

Enterprise-grade singleton enforcement for the JARVIS system.
Prevents multiple supervisors/entry points from running simultaneously.

This module provides:
1. Cross-process PID file locking with stale detection
2. Process tree awareness (handles forks and child processes)
3. Atomic file operations for reliability
4. Graceful conflict resolution
5. v113.0: IPC command socket for restart/takeover/status commands
6. v114.0: Functional health checks (IPC ping, HTTP health, readiness state)
7. v115.0: Advanced multi-layer health verification with async parallel checks,
           cross-repo integration, intelligent recovery, and zero-config autodiscovery

Usage:
    from backend.core.supervisor_singleton import acquire_supervisor_lock, release_supervisor_lock

    if not acquire_supervisor_lock("run_supervisor"):
        print("Another JARVIS instance is running!")
        sys.exit(1)

    try:
        # Run main supervisor logic
        pass
    finally:
        release_supervisor_lock()

IPC Commands (v113.0+):
    - status: Get running supervisor status
    - restart: Request graceful restart
    - takeover: Request graceful takeover by new instance
    - force-stop: Force immediate shutdown
    - health: v115.0 - Comprehensive health report with cross-repo status
    - cross-repo-status: v115.0 - Get status of all connected repos

Author: JARVIS System
Version: 115.0.0 (January 2026)
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import signal
import socket
import sys
import time
import urllib.request
import urllib.error
from abc import ABC, abstractmethod
from dataclasses import dataclass, asdict, field
from datetime import datetime
from enum import Enum, IntEnum
from pathlib import Path
from typing import Optional, Dict, Any, Tuple, Callable, List

logger = logging.getLogger(__name__)

# =============================================================================
# v115.0: Configuration with Environment Variable Support
# =============================================================================

def _get_env_path(key: str, default: Path) -> Path:
    """Get path from environment variable or use default."""
    val = os.environ.get(key)
    return Path(val) if val else default

def _get_env_float(key: str, default: float) -> float:
    """Get float from environment variable or use default."""
    val = os.environ.get(key)
    try:
        return float(val) if val else default
    except ValueError:
        return default

def _get_env_int(key: str, default: int) -> int:
    """Get int from environment variable or use default."""
    val = os.environ.get(key)
    try:
        return int(val) if val else default
    except ValueError:
        return default

# Lock file location - configurable via environment
LOCK_DIR = _get_env_path("JARVIS_LOCK_DIR", Path.home() / ".jarvis" / "locks")
SUPERVISOR_LOCK_FILE = LOCK_DIR / "supervisor.lock"
SUPERVISOR_STATE_FILE = LOCK_DIR / "supervisor.state"
SUPERVISOR_IPC_SOCKET = LOCK_DIR / "supervisor.sock"

# v115.0: Cross-repo state directory
CROSS_REPO_DIR = _get_env_path("JARVIS_CROSS_REPO_DIR", Path.home() / ".jarvis" / "cross_repo")
TRINITY_READINESS_DIR = _get_env_path("JARVIS_TRINITY_DIR", Path.home() / ".jarvis" / "trinity" / "readiness")

# v115.0: Configurable thresholds
STALE_LOCK_THRESHOLD = _get_env_float("JARVIS_STALE_LOCK_THRESHOLD", 90.0)  # Reduced from 300s
HEARTBEAT_INTERVAL = _get_env_float("JARVIS_HEARTBEAT_INTERVAL", 5.0)  # Reduced from 10s
HEALTH_CHECK_TIMEOUT = _get_env_float("JARVIS_HEALTH_CHECK_TIMEOUT", 3.0)
IPC_TIMEOUT = _get_env_float("JARVIS_IPC_TIMEOUT", 2.0)
HTTP_HEALTH_PORTS = [int(p) for p in os.environ.get("JARVIS_HTTP_PORTS", "8080,8000,8010").split(",")]

# v115.0: Health check levels
class HealthLevel(IntEnum):
    """Progressive health verification levels."""
    UNKNOWN = 0       # Not yet checked
    PROCESS_EXISTS = 1  # PID exists
    PROCESS_VALID = 2   # PID + start time match
    IPC_RESPONSIVE = 3  # Responds to IPC ping
    HTTP_HEALTHY = 4    # HTTP health check passes
    FULLY_READY = 5     # All checks pass including readiness


class IPCCommand(str, Enum):
    """v113.0+: IPC commands for inter-supervisor communication."""
    STATUS = "status"           # Get running supervisor status
    RESTART = "restart"         # Request graceful restart
    TAKEOVER = "takeover"       # New instance requests takeover
    FORCE_STOP = "force-stop"   # Force immediate shutdown
    PING = "ping"               # Simple liveness check
    SHUTDOWN = "shutdown"       # Graceful shutdown
    # v115.0: New commands
    HEALTH = "health"           # Comprehensive health report
    CROSS_REPO_STATUS = "cross-repo-status"  # Cross-repo integration status
    METRICS = "metrics"         # Performance metrics
    DIAGNOSTICS = "diagnostics" # Diagnostic information


# =============================================================================
# v115.0: Health Check Protocol and Strategies
# =============================================================================

class HealthCheckResult:
    """Result of a health check with detailed diagnostics."""
    __slots__ = ('healthy', 'level', 'latency_ms', 'message', 'details', 'timestamp')

    def __init__(
        self,
        healthy: bool,
        level: HealthLevel = HealthLevel.UNKNOWN,
        latency_ms: float = 0.0,
        message: str = "",
        details: Optional[Dict[str, Any]] = None
    ):
        self.healthy = healthy
        self.level = level
        self.latency_ms = latency_ms
        self.message = message
        self.details = details or {}
        self.timestamp = time.time()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "healthy": self.healthy,
            "level": self.level.name,
            "level_value": int(self.level),
            "latency_ms": self.latency_ms,
            "message": self.message,
            "details": self.details,
            "timestamp": self.timestamp
        }


class HealthCheckStrategy(ABC):
    """Abstract base class for health check strategies (Strategy Pattern)."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Strategy name for logging."""
        pass

    @property
    @abstractmethod
    def level(self) -> HealthLevel:
        """Health level this strategy verifies."""
        pass

    @abstractmethod
    async def check(self, state: "SupervisorState") -> HealthCheckResult:
        """Perform health check."""
        pass


class ProcessExistsStrategy(HealthCheckStrategy):
    """Check if process exists using signal 0."""

    @property
    def name(self) -> str:
        return "process_exists"

    @property
    def level(self) -> HealthLevel:
        return HealthLevel.PROCESS_EXISTS

    async def check(self, state: "SupervisorState") -> HealthCheckResult:
        start = time.perf_counter()
        try:
            os.kill(state.pid, 0)
            latency = (time.perf_counter() - start) * 1000
            return HealthCheckResult(
                healthy=True,
                level=self.level,
                latency_ms=latency,
                message=f"Process {state.pid} exists"
            )
        except ProcessLookupError:
            return HealthCheckResult(
                healthy=False,
                level=HealthLevel.UNKNOWN,
                message=f"Process {state.pid} does not exist"
            )
        except PermissionError:
            latency = (time.perf_counter() - start) * 1000
            return HealthCheckResult(
                healthy=True,
                level=self.level,
                latency_ms=latency,
                message=f"Process {state.pid} exists (permission denied for signal)"
            )
        except Exception as e:
            return HealthCheckResult(
                healthy=False,
                level=HealthLevel.UNKNOWN,
                message=f"Error checking process: {e}"
            )


class ProcessValidStrategy(HealthCheckStrategy):
    """Validate process identity (PID + start time + command line)."""

    @property
    def name(self) -> str:
        return "process_valid"

    @property
    def level(self) -> HealthLevel:
        return HealthLevel.PROCESS_VALID

    async def check(self, state: "SupervisorState") -> HealthCheckResult:
        start = time.perf_counter()
        try:
            import psutil
            proc = psutil.Process(state.pid)

            # Check process is running
            if not proc.is_running():
                return HealthCheckResult(
                    healthy=False,
                    level=HealthLevel.UNKNOWN,
                    message=f"Process {state.pid} not running"
                )

            # Check for zombie
            if proc.status() == psutil.STATUS_ZOMBIE:
                return HealthCheckResult(
                    healthy=False,
                    level=HealthLevel.UNKNOWN,
                    message=f"Process {state.pid} is zombie"
                )

            # Validate start time if available
            if hasattr(state, 'process_start_time') and state.process_start_time:
                current_start = proc.create_time()
                time_diff = abs(current_start - state.process_start_time)
                if time_diff > 2.0:  # 2 second tolerance
                    return HealthCheckResult(
                        healthy=False,
                        level=HealthLevel.UNKNOWN,
                        message=f"PID reuse detected: start time mismatch ({time_diff:.1f}s diff)",
                        details={"stored_start": state.process_start_time, "current_start": current_start}
                    )

            # Validate command line contains JARVIS patterns
            try:
                cmdline = " ".join(proc.cmdline())
                jarvis_patterns = ["run_supervisor", "jarvis", "JARVIS", "start_system"]
                if not any(p in cmdline for p in jarvis_patterns):
                    return HealthCheckResult(
                        healthy=False,
                        level=HealthLevel.UNKNOWN,
                        message=f"Process {state.pid} is not a JARVIS process",
                        details={"cmdline": cmdline[:200]}
                    )
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                pass  # Can't verify cmdline, continue anyway

            latency = (time.perf_counter() - start) * 1000
            return HealthCheckResult(
                healthy=True,
                level=self.level,
                latency_ms=latency,
                message=f"Process {state.pid} validated",
                details={
                    "name": proc.name(),
                    "status": proc.status(),
                    "cpu_percent": proc.cpu_percent(interval=0.01),
                    "memory_mb": proc.memory_info().rss / 1024 / 1024
                }
            )

        except ImportError:
            # psutil not available, fall back to basic check
            return HealthCheckResult(
                healthy=True,
                level=HealthLevel.PROCESS_EXISTS,
                message="psutil not available, basic check only"
            )
        except Exception as e:
            return HealthCheckResult(
                healthy=False,
                level=HealthLevel.UNKNOWN,
                message=f"Process validation error: {e}"
            )


class IPCPingStrategy(HealthCheckStrategy):
    """Check if supervisor responds to IPC ping."""

    @property
    def name(self) -> str:
        return "ipc_ping"

    @property
    def level(self) -> HealthLevel:
        return HealthLevel.IPC_RESPONSIVE

    async def check(self, state: "SupervisorState") -> HealthCheckResult:
        if not SUPERVISOR_IPC_SOCKET.exists():
            return HealthCheckResult(
                healthy=False,
                level=HealthLevel.PROCESS_VALID,
                message="IPC socket does not exist"
            )

        start = time.perf_counter()
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(str(SUPERVISOR_IPC_SOCKET)),
                timeout=IPC_TIMEOUT
            )

            request = {"command": "ping", "args": {}}
            writer.write(json.dumps(request).encode())
            await writer.drain()

            data = await asyncio.wait_for(reader.read(4096), timeout=IPC_TIMEOUT)
            response = json.loads(data.decode())

            writer.close()
            await writer.wait_closed()

            latency = (time.perf_counter() - start) * 1000

            if response.get("success") and response.get("result", {}).get("pong"):
                return HealthCheckResult(
                    healthy=True,
                    level=self.level,
                    latency_ms=latency,
                    message="IPC ping successful",
                    details=response.get("result", {})
                )
            else:
                return HealthCheckResult(
                    healthy=False,
                    level=HealthLevel.PROCESS_VALID,
                    message=f"IPC ping failed: {response}",
                    latency_ms=latency
                )

        except asyncio.TimeoutError:
            return HealthCheckResult(
                healthy=False,
                level=HealthLevel.PROCESS_VALID,
                message="IPC ping timeout"
            )
        except ConnectionRefusedError:
            return HealthCheckResult(
                healthy=False,
                level=HealthLevel.PROCESS_VALID,
                message="IPC connection refused"
            )
        except Exception as e:
            return HealthCheckResult(
                healthy=False,
                level=HealthLevel.PROCESS_VALID,
                message=f"IPC ping error: {e}"
            )


class HTTPHealthStrategy(HealthCheckStrategy):
    """Check HTTP health endpoint."""

    @property
    def name(self) -> str:
        return "http_health"

    @property
    def level(self) -> HealthLevel:
        return HealthLevel.HTTP_HEALTHY

    async def check(self, state: "SupervisorState") -> HealthCheckResult:
        start = time.perf_counter()

        # Run HTTP check in thread pool to avoid blocking
        loop = asyncio.get_event_loop()

        for port in HTTP_HEALTH_PORTS:
            try:
                result = await loop.run_in_executor(
                    None,
                    self._sync_http_check,
                    port
                )
                if result.healthy:
                    result.latency_ms = (time.perf_counter() - start) * 1000
                    return result
            except Exception:
                continue

        return HealthCheckResult(
            healthy=False,
            level=HealthLevel.IPC_RESPONSIVE,
            message=f"HTTP health check failed on all ports: {HTTP_HEALTH_PORTS}"
        )

    def _sync_http_check(self, port: int) -> HealthCheckResult:
        """Synchronous HTTP check (run in thread pool)."""
        try:
            url = f"http://127.0.0.1:{port}/health"
            req = urllib.request.Request(url)
            req.add_header("User-Agent", "JARVIS-Supervisor-Singleton/115.0")

            with urllib.request.urlopen(req, timeout=HEALTH_CHECK_TIMEOUT) as response:
                if response.status == 200:
                    try:
                        data = json.loads(response.read().decode())
                    except:
                        data = {}
                    return HealthCheckResult(
                        healthy=True,
                        level=self.level,
                        message=f"HTTP health OK (port {port})",
                        details={"port": port, "response": data}
                    )

        except Exception as e:
            pass

        return HealthCheckResult(
            healthy=False,
            level=HealthLevel.IPC_RESPONSIVE,
            message=f"HTTP check failed on port {port}"
        )


class ReadinessStateStrategy(HealthCheckStrategy):
    """Check readiness state file."""

    @property
    def name(self) -> str:
        return "readiness_state"

    @property
    def level(self) -> HealthLevel:
        return HealthLevel.FULLY_READY

    async def check(self, state: "SupervisorState") -> HealthCheckResult:
        start = time.perf_counter()

        readiness_file = TRINITY_READINESS_DIR / "jarvis-body.json"

        if not readiness_file.exists():
            return HealthCheckResult(
                healthy=False,
                level=HealthLevel.HTTP_HEALTHY,
                message="Readiness state file not found"
            )

        try:
            data = json.loads(readiness_file.read_text())
            phase = data.get("phase", "")
            ready_phases = {"ready", "healthy", "operational", "warming_up", "interactive"}

            latency = (time.perf_counter() - start) * 1000

            if phase.lower() in ready_phases:
                return HealthCheckResult(
                    healthy=True,
                    level=self.level,
                    latency_ms=latency,
                    message=f"Readiness state: {phase}",
                    details=data
                )
            else:
                return HealthCheckResult(
                    healthy=False,
                    level=HealthLevel.HTTP_HEALTHY,
                    latency_ms=latency,
                    message=f"Not ready: phase={phase}",
                    details=data
                )

        except Exception as e:
            return HealthCheckResult(
                healthy=False,
                level=HealthLevel.HTTP_HEALTHY,
                message=f"Error reading readiness state: {e}"
            )


class CompositeHealthChecker:
    """
    v115.0: Composite health checker using Strategy Pattern.

    Runs multiple health check strategies in order of increasing complexity,
    short-circuiting on failure for fast detection of unhealthy supervisors.
    """

    def __init__(self, strategies: Optional[List[HealthCheckStrategy]] = None):
        """Initialize with ordered list of strategies."""
        self.strategies = strategies or [
            ProcessExistsStrategy(),
            ProcessValidStrategy(),
            IPCPingStrategy(),
            HTTPHealthStrategy(),
            ReadinessStateStrategy(),
        ]
        self._cache: Dict[int, Tuple[float, HealthCheckResult]] = {}
        self._cache_ttl = 2.0  # Cache results for 2 seconds

    async def check_health(
        self,
        state: "SupervisorState",
        min_level: HealthLevel = HealthLevel.IPC_RESPONSIVE,
        use_cache: bool = True
    ) -> HealthCheckResult:
        """
        Check supervisor health up to minimum required level.

        Args:
            state: Supervisor state to check
            min_level: Minimum health level required (short-circuits on failure)
            use_cache: Whether to use cached results

        Returns:
            HealthCheckResult with highest achieved level
        """
        # Check cache
        cache_key = state.pid
        if use_cache and cache_key in self._cache:
            cached_time, cached_result = self._cache[cache_key]
            if time.time() - cached_time < self._cache_ttl:
                return cached_result

        best_result = HealthCheckResult(
            healthy=False,
            level=HealthLevel.UNKNOWN,
            message="No checks performed"
        )

        for strategy in self.strategies:
            if strategy.level > min_level:
                # Already reached required level, stop here
                break

            try:
                result = await asyncio.wait_for(
                    strategy.check(state),
                    timeout=HEALTH_CHECK_TIMEOUT
                )

                logger.debug(
                    f"[Health] {strategy.name}: healthy={result.healthy}, "
                    f"level={result.level.name}, msg={result.message}"
                )

                if result.healthy:
                    best_result = result
                else:
                    # Strategy failed - return with current level
                    best_result = result
                    break

            except asyncio.TimeoutError:
                best_result = HealthCheckResult(
                    healthy=False,
                    level=best_result.level,
                    message=f"Timeout in {strategy.name} check"
                )
                break
            except Exception as e:
                logger.debug(f"[Health] {strategy.name} error: {e}")
                best_result = HealthCheckResult(
                    healthy=False,
                    level=best_result.level,
                    message=f"Error in {strategy.name}: {e}"
                )
                break

        # Cache result
        self._cache[cache_key] = (time.time(), best_result)

        return best_result

    async def check_health_parallel(
        self,
        state: "SupervisorState"
    ) -> Dict[str, HealthCheckResult]:
        """
        Run all health checks in parallel for comprehensive diagnostics.

        Returns dict of strategy_name -> result
        """
        tasks = {
            strategy.name: asyncio.create_task(
                asyncio.wait_for(strategy.check(state), timeout=HEALTH_CHECK_TIMEOUT)
            )
            for strategy in self.strategies
        }

        results = {}
        for name, task in tasks.items():
            try:
                results[name] = await task
            except asyncio.TimeoutError:
                results[name] = HealthCheckResult(
                    healthy=False,
                    level=HealthLevel.UNKNOWN,
                    message="Check timed out"
                )
            except Exception as e:
                results[name] = HealthCheckResult(
                    healthy=False,
                    level=HealthLevel.UNKNOWN,
                    message=f"Error: {e}"
                )

        return results

    def clear_cache(self):
        """Clear health check cache."""
        self._cache.clear()


# Global health checker instance
_health_checker: Optional[CompositeHealthChecker] = None

def get_health_checker() -> CompositeHealthChecker:
    """Get or create global health checker."""
    global _health_checker
    if _health_checker is None:
        _health_checker = CompositeHealthChecker()
    return _health_checker


@dataclass
class SupervisorState:
    """
    v115.0: Enhanced state information for the running supervisor.

    Includes process fingerprinting for PID reuse detection and
    cross-repo integration status.
    """
    pid: int
    entry_point: str  # "run_supervisor" or "start_system"
    started_at: str
    last_heartbeat: str
    hostname: str
    working_dir: str
    python_version: str
    command_line: str
    # v115.0: Enhanced fields
    process_start_time: float = 0.0  # For PID reuse detection
    machine_id: str = ""  # Unique machine identifier
    health_level: int = 0  # Current health level (HealthLevel enum value)
    cross_repo_status: Dict[str, str] = field(default_factory=dict)  # Repo -> status
    version: str = "115.0.0"  # Singleton version

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> SupervisorState:
        """Create from dict with backward compatibility for older formats."""
        # Handle fields that may not exist in older state files
        defaults = {
            "process_start_time": 0.0,
            "machine_id": "",
            "health_level": 0,
            "cross_repo_status": {},
            "version": "unknown"
        }
        for key, default in defaults.items():
            if key not in data:
                data[key] = default
        # Filter out any unknown keys that might cause issues
        known_keys = {
            "pid", "entry_point", "started_at", "last_heartbeat",
            "hostname", "working_dir", "python_version", "command_line",
            "process_start_time", "machine_id", "health_level",
            "cross_repo_status", "version"
        }
        filtered_data = {k: v for k, v in data.items() if k in known_keys}
        return cls(**filtered_data)

    @classmethod
    def create_current(cls, entry_point: str) -> SupervisorState:
        """Create state for current process with full fingerprinting."""
        import socket as sock
        import platform

        # Get process start time for PID reuse detection
        process_start_time = time.time()
        try:
            import psutil
            process_start_time = psutil.Process(os.getpid()).create_time()
        except Exception:
            pass

        # Generate machine ID
        machine_id = f"{platform.system().lower()}-{sock.gethostname()}"
        try:
            # Add more entropy to machine ID
            import uuid
            machine_id = f"{machine_id}-{uuid.getnode()}"
        except Exception:
            pass

        return cls(
            pid=os.getpid(),
            entry_point=entry_point,
            started_at=datetime.now().isoformat(),
            last_heartbeat=datetime.now().isoformat(),
            hostname=sock.gethostname(),
            working_dir=str(Path.cwd()),
            python_version=sys.version.split()[0],
            command_line=" ".join(sys.argv)[:500],  # Truncate long command lines
            process_start_time=process_start_time,
            machine_id=machine_id,
            health_level=int(HealthLevel.PROCESS_EXISTS),
            cross_repo_status={},
            version="115.0.0"
        )

    def get_uptime_seconds(self) -> float:
        """Get supervisor uptime in seconds."""
        try:
            started = datetime.fromisoformat(self.started_at)
            return (datetime.now() - started).total_seconds()
        except Exception:
            return 0.0

    def get_heartbeat_age_seconds(self) -> float:
        """Get age of last heartbeat in seconds."""
        try:
            heartbeat = datetime.fromisoformat(self.last_heartbeat)
            return (datetime.now() - heartbeat).total_seconds()
        except Exception:
            return float('inf')


class SupervisorSingleton:
    """
    Singleton enforcement for JARVIS supervisor processes.

    Uses file-based locking with fcntl for cross-process synchronization.
    """

    _instance: Optional[SupervisorSingleton] = None
    _lock_fd: Optional[int] = None
    _heartbeat_task: Optional[asyncio.Task] = None
    _state: Optional[SupervisorState] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._ensure_lock_dir()

    def _ensure_lock_dir(self) -> None:
        """Ensure lock directory exists."""
        LOCK_DIR.mkdir(parents=True, exist_ok=True)

    def _is_process_alive(self, pid: int) -> bool:
        """Check if a process is still running."""
        try:
            os.kill(pid, 0)  # Signal 0 just checks if process exists
            return True
        except OSError:
            return False

    def _is_jarvis_process(self, pid: int) -> bool:
        """Check if a PID is a JARVIS-related process."""
        try:
            # Read process command line
            if sys.platform == "darwin":
                import subprocess
                result = subprocess.run(
                    ["ps", "-p", str(pid), "-o", "command="],
                    capture_output=True,
                    text=True,
                    timeout=2
                )
                cmdline = result.stdout.strip()
            else:
                cmdline_path = Path(f"/proc/{pid}/cmdline")
                if cmdline_path.exists():
                    cmdline = cmdline_path.read_text().replace('\x00', ' ')
                else:
                    return False

            # Check for JARVIS patterns
            jarvis_patterns = [
                "run_supervisor.py",
                "start_system.py",
                "jarvis",
                "JARVIS",
            ]
            return any(pattern in cmdline for pattern in jarvis_patterns)
        except Exception:
            return False

    def _read_state(self) -> Optional[SupervisorState]:
        """
        v115.0: Read current supervisor state with fallback to lock file.

        If state file exists, read from it.
        If state file is missing but lock file exists, create synthetic state from lock file PID.
        This handles cases where state file was not created or got deleted.
        """
        # Primary: Try to read from state file
        try:
            if SUPERVISOR_STATE_FILE.exists():
                data = json.loads(SUPERVISOR_STATE_FILE.read_text())
                return SupervisorState.from_dict(data)
        except Exception as e:
            logger.debug(f"Could not read state file: {e}")

        # v115.0: Fallback - Create synthetic state from lock file PID
        try:
            if SUPERVISOR_LOCK_FILE.exists():
                lock_content = SUPERVISOR_LOCK_FILE.read_text().strip()
                if lock_content.isdigit():
                    pid = int(lock_content)
                    if self._is_process_alive(pid):
                        logger.debug(f"[Singleton] Creating synthetic state from lock file (PID: {pid})")

                        # Try to get process info
                        try:
                            import psutil
                            proc = psutil.Process(pid)
                            cmdline = " ".join(proc.cmdline())[:500]
                            start_time = proc.create_time()
                            entry_point = "run_supervisor" if "run_supervisor" in cmdline else "unknown"
                        except Exception:
                            cmdline = ""
                            start_time = 0.0
                            entry_point = "unknown"

                        # Create synthetic state
                        import socket as sock
                        return SupervisorState(
                            pid=pid,
                            entry_point=entry_point,
                            started_at=datetime.fromtimestamp(start_time).isoformat() if start_time else datetime.now().isoformat(),
                            last_heartbeat=datetime.now().isoformat(),  # Assume recently active if process alive
                            hostname=sock.gethostname(),
                            working_dir=str(Path.cwd()),
                            python_version=sys.version.split()[0],
                            command_line=cmdline,
                            process_start_time=start_time,
                            machine_id="",
                            health_level=int(HealthLevel.PROCESS_EXISTS),
                            cross_repo_status={},
                            version="115.0.0-synthetic"
                        )
        except Exception as e:
            logger.debug(f"Could not create synthetic state from lock file: {e}")

        return None

    def _write_state(self, state: SupervisorState) -> None:
        """Write supervisor state atomically."""
        try:
            temp_file = SUPERVISOR_STATE_FILE.with_suffix('.tmp')
            temp_file.write_text(json.dumps(state.to_dict(), indent=2))
            temp_file.rename(SUPERVISOR_STATE_FILE)
        except Exception as e:
            logger.warning(f"Could not write state file: {e}")

    def _is_lock_stale(self) -> Tuple[bool, Optional[SupervisorState]]:
        """
        v115.0: Enhanced stale lock detection with multi-layer health verification.

        Uses progressive health checks to detect:
        1. Dead processes
        2. Zombie processes
        3. PID reuse
        4. Hung/unresponsive supervisors (via IPC ping)
        5. Failed health endpoints

        Returns:
            (is_stale, existing_state)
        """
        state = self._read_state()
        if state is None:
            logger.debug("[Singleton] No state file found - lock is stale")
            return True, None

        # Layer 1: Check if process exists at all
        if not self._is_process_alive(state.pid):
            logger.info(f"[Singleton] Lock holder PID {state.pid} is dead")
            return True, state

        # Layer 2: Check if it's a JARVIS process (not PID reuse)
        if not self._is_jarvis_process(state.pid):
            logger.info(f"[Singleton] PID {state.pid} is not a JARVIS process (PID reuse detected)")
            return True, state

        # Layer 3: Validate process start time (PID reuse detection)
        if state.process_start_time > 0:
            try:
                import psutil
                proc = psutil.Process(state.pid)
                current_start = proc.create_time()
                time_diff = abs(current_start - state.process_start_time)
                if time_diff > 2.0:
                    logger.info(
                        f"[Singleton] PID reuse detected: start time mismatch "
                        f"(stored={state.process_start_time:.1f}, current={current_start:.1f}, diff={time_diff:.1f}s)"
                    )
                    return True, state
            except Exception as e:
                logger.debug(f"[Singleton] Could not validate process start time: {e}")

        # Layer 4: Check heartbeat age (reduced threshold in v115.0)
        heartbeat_age = state.get_heartbeat_age_seconds()
        if heartbeat_age > STALE_LOCK_THRESHOLD:
            logger.info(f"[Singleton] Lock stale: no heartbeat for {heartbeat_age:.0f}s (threshold: {STALE_LOCK_THRESHOLD}s)")
            return True, state

        # Layer 5: v115.0 - Functional health check (is supervisor actually responding?)
        # This catches hung supervisors where process exists but is unresponsive
        if not self._is_supervisor_functionally_healthy_sync(state):
            logger.warning(
                f"[Singleton] Lock holder PID {state.pid} is not functionally healthy "
                f"(process alive but not responding)"
            )
            return True, state

        return False, state

    def _is_supervisor_functionally_healthy_sync(self, state: SupervisorState) -> bool:
        """
        v115.0: Synchronous functional health check.

        Performs quick checks to verify supervisor is responsive:
        1. IPC socket exists and responds to ping
        2. HTTP health endpoint responds
        3. Readiness state file indicates healthy

        Returns True if supervisor appears healthy, False if unresponsive.
        """
        # Check 1: IPC ping (most reliable indicator of liveness)
        if SUPERVISOR_IPC_SOCKET.exists():
            try:
                # Synchronous socket check
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(IPC_TIMEOUT)
                sock.connect(str(SUPERVISOR_IPC_SOCKET))

                request = json.dumps({"command": "ping", "args": {}}).encode()
                sock.sendall(request)

                response_data = sock.recv(4096)
                sock.close()

                if response_data:
                    response = json.loads(response_data.decode())
                    if response.get("success") and response.get("result", {}).get("pong"):
                        logger.debug(f"[Singleton] IPC ping successful for PID {state.pid}")
                        return True
            except socket.timeout:
                logger.debug(f"[Singleton] IPC ping timeout for PID {state.pid}")
            except ConnectionRefusedError:
                logger.debug(f"[Singleton] IPC connection refused for PID {state.pid}")
            except Exception as e:
                logger.debug(f"[Singleton] IPC ping error for PID {state.pid}: {e}")

        # Check 2: HTTP health endpoint (fallback)
        for port in HTTP_HEALTH_PORTS:
            try:
                url = f"http://127.0.0.1:{port}/health"
                req = urllib.request.Request(url)
                req.add_header("User-Agent", "JARVIS-Singleton/115.0")

                with urllib.request.urlopen(req, timeout=HEALTH_CHECK_TIMEOUT) as response:
                    if response.status == 200:
                        logger.debug(f"[Singleton] HTTP health OK for PID {state.pid} on port {port}")
                        return True
            except Exception:
                continue

        # Check 3: Readiness state file (final fallback)
        readiness_file = TRINITY_READINESS_DIR / "jarvis-body.json"
        if readiness_file.exists():
            try:
                data = json.loads(readiness_file.read_text())
                phase = data.get("phase", "").lower()
                # Also check the file was recently updated
                file_age = time.time() - readiness_file.stat().st_mtime
                if phase in ("ready", "healthy", "operational", "warming_up") and file_age < 60:
                    logger.debug(f"[Singleton] Readiness state healthy for PID {state.pid}")
                    return True
            except Exception as e:
                logger.debug(f"[Singleton] Readiness state check error: {e}")

        # If heartbeat was very recent (within 30s), give benefit of doubt
        # This handles startup scenarios where IPC/HTTP aren't ready yet
        if heartbeat_age := state.get_heartbeat_age_seconds():
            if heartbeat_age < 30:
                logger.debug(
                    f"[Singleton] Recent heartbeat ({heartbeat_age:.0f}s ago), assuming healthy during startup"
                )
                return True

        logger.debug(f"[Singleton] All functional health checks failed for PID {state.pid}")
        return False

    def acquire(self, entry_point: str) -> bool:
        """
        Attempt to acquire the supervisor lock.

        Args:
            entry_point: Name of the entry point ("run_supervisor" or "start_system")

        Returns:
            True if lock acquired, False if another instance is running
        """
        self._ensure_lock_dir()

        try:
            # Open lock file
            self._lock_fd = os.open(
                str(SUPERVISOR_LOCK_FILE),
                os.O_CREAT | os.O_RDWR,
                0o644
            )

            # Try to acquire exclusive lock (non-blocking)
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                # Lock is held by another process
                is_stale, state = self._is_lock_stale()

                if is_stale:
                    # Stale lock - force acquire with timeout
                    logger.warning(f"[Singleton] Taking over stale lock from {state.entry_point if state else 'unknown'}")

                    if state and self._is_process_alive(state.pid):
                        # Try to terminate gracefully
                        try:
                            logger.info(f"[Singleton] Sending SIGTERM to stale PID {state.pid}")
                            os.kill(state.pid, signal.SIGTERM)
                            time.sleep(2)
                            # Check if process is gone
                            if self._is_process_alive(state.pid):
                                logger.warning(f"[Singleton] PID {state.pid} didn't terminate, sending SIGKILL")
                                os.kill(state.pid, signal.SIGKILL)
                                time.sleep(1)
                        except ProcessLookupError:
                            logger.info(f"[Singleton] PID {state.pid} already dead")
                        except Exception as e:
                            logger.debug(f"[Singleton] Signal error (expected if process dead): {e}")

                    # v111.2: Force acquire with timeout and retry
                    # This prevents hanging if the kernel hasn't released the lock yet
                    lock_acquired = False
                    max_retries = 10
                    retry_delay = 0.5

                    for attempt in range(max_retries):
                        try:
                            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            lock_acquired = True
                            logger.info(f"[Singleton] Lock acquired on attempt {attempt + 1}")
                            break
                        except BlockingIOError:
                            if attempt < max_retries - 1:
                                logger.debug(f"[Singleton] Lock not yet available, retry {attempt + 1}/{max_retries}")
                                time.sleep(retry_delay)
                            else:
                                logger.warning(f"[Singleton] Lock still held after {max_retries} retries")

                    if not lock_acquired:
                        # v111.2: Nuclear option - recreate lock file
                        # This handles cases where the kernel lock is stuck
                        logger.warning("[Singleton] Forcibly recreating lock file (stale kernel lock)")
                        try:
                            os.close(self._lock_fd)
                            self._lock_fd = None
                            # Remove stale files
                            SUPERVISOR_LOCK_FILE.unlink(missing_ok=True)
                            SUPERVISOR_STATE_FILE.unlink(missing_ok=True)
                            # Recreate and acquire
                            self._lock_fd = os.open(
                                str(SUPERVISOR_LOCK_FILE),
                                os.O_CREAT | os.O_RDWR,
                                0o644
                            )
                            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            lock_acquired = True
                            logger.info("[Singleton] Lock acquired after file recreation")
                        except Exception as recreate_err:
                            logger.error(f"[Singleton] Lock file recreation failed: {recreate_err}")
                            return False

                else:
                    # Valid lock held by another process
                    os.close(self._lock_fd)
                    self._lock_fd = None

                    if state:
                        logger.error(
                            f"[Singleton] ❌ JARVIS already running!\n"
                            f"  Entry point: {state.entry_point}\n"
                            f"  PID: {state.pid}\n"
                            f"  Started: {state.started_at}\n"
                            f"  Working dir: {state.working_dir}"
                        )
                    return False

            # Lock acquired - write state
            self._state = SupervisorState.create_current(entry_point)
            self._write_state(self._state)

            # Write PID to lock file for external tools
            os.ftruncate(self._lock_fd, 0)
            os.lseek(self._lock_fd, 0, os.SEEK_SET)
            os.write(self._lock_fd, f"{os.getpid()}\n".encode())

            logger.info(f"[Singleton] ✅ Lock acquired for {entry_point} (PID: {os.getpid()})")

            # v109.4: Set up SIGHUP handler for clean restart via os.execv()
            _setup_sighup_handler()

            return True

        except Exception as e:
            logger.error(f"[Singleton] Lock acquisition failed: {e}")
            if self._lock_fd is not None:
                try:
                    os.close(self._lock_fd)
                except Exception:
                    pass
                self._lock_fd = None
            return False

    def release(self) -> None:
        """Release the supervisor lock."""
        # Stop heartbeat
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            self._heartbeat_task = None

        # Release file lock
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                os.close(self._lock_fd)
            except Exception as e:
                logger.debug(f"Error releasing lock: {e}")
            self._lock_fd = None

        # Clean up state file
        try:
            if SUPERVISOR_STATE_FILE.exists():
                state = self._read_state()
                if state and state.pid == os.getpid():
                    SUPERVISOR_STATE_FILE.unlink()
        except Exception as e:
            logger.debug(f"Error cleaning state file: {e}")

        logger.info("[Singleton] Lock released")

    async def start_heartbeat(self) -> None:
        """
        v115.0: Enhanced heartbeat with health level updates and cross-repo status.

        The heartbeat now:
        1. Updates last_heartbeat timestamp
        2. Periodically checks and updates health level
        3. Updates cross-repo connection status
        """
        async def heartbeat_loop():
            health_check_counter = 0
            cross_repo_check_counter = 0

            while True:
                try:
                    if self._state:
                        # Update heartbeat timestamp
                        self._state.last_heartbeat = datetime.now().isoformat()

                        # v115.0: Periodic health level update (every 6th heartbeat = ~30s)
                        health_check_counter += 1
                        if health_check_counter >= 6:
                            health_check_counter = 0
                            try:
                                health_checker = get_health_checker()
                                result = await health_checker.check_health(
                                    self._state,
                                    min_level=HealthLevel.HTTP_HEALTHY
                                )
                                self._state.health_level = int(result.level)
                            except Exception as he:
                                logger.debug(f"Health check in heartbeat failed: {he}")

                        # v115.0: Periodic cross-repo status update (every 12th heartbeat = ~60s)
                        cross_repo_check_counter += 1
                        if cross_repo_check_counter >= 12:
                            cross_repo_check_counter = 0
                            try:
                                cross_repo_status = await self._check_cross_repo_connections()
                                self._state.cross_repo_status = cross_repo_status
                            except Exception as ce:
                                logger.debug(f"Cross-repo check in heartbeat failed: {ce}")

                        self._write_state(self._state)

                except Exception as e:
                    logger.debug(f"Heartbeat error: {e}")

                await asyncio.sleep(HEARTBEAT_INTERVAL)

        self._heartbeat_task = asyncio.create_task(heartbeat_loop())

    async def _check_cross_repo_connections(self) -> Dict[str, str]:
        """
        v115.0: Check connection status to other repos.

        Returns dict of repo_name -> status ("connected", "disconnected", "unknown")
        """
        status = {}

        # Check JARVIS Prime
        prime_state_file = CROSS_REPO_DIR / "prime_state.json"
        if prime_state_file.exists():
            try:
                file_age = time.time() - prime_state_file.stat().st_mtime
                status["jarvis_prime"] = "connected" if file_age < 120 else "stale"
            except Exception:
                status["jarvis_prime"] = "error"
        else:
            status["jarvis_prime"] = "disconnected"

        # Check Reactor Core
        reactor_state_file = CROSS_REPO_DIR / "reactor_state.json"
        if reactor_state_file.exists():
            try:
                file_age = time.time() - reactor_state_file.stat().st_mtime
                status["reactor_core"] = "connected" if file_age < 120 else "stale"
            except Exception:
                status["reactor_core"] = "error"
        else:
            status["reactor_core"] = "disconnected"

        return status

    def is_locked(self) -> bool:
        """Check if we hold the lock."""
        return self._lock_fd is not None

    def get_state(self) -> Optional[SupervisorState]:
        """Get current state."""
        return self._state
    
    # =========================================================================
    # v113.0+ IPC SERVER METHODS
    # =========================================================================

    async def start_ipc_server(self, command_handlers: Optional[Dict[IPCCommand, Callable]] = None) -> None:
        """
        v115.0: Start Unix domain socket IPC server for remote commands.

        Supports both v113.0 legacy commands and v115.0 enhanced commands:
        - status, ping, restart, shutdown, takeover, force-stop (v113.0)
        - health, cross-repo-status, metrics, diagnostics (v115.0)

        Args:
            command_handlers: Optional custom handlers for commands
        """
        # Remove stale socket file
        if SUPERVISOR_IPC_SOCKET.exists():
            try:
                SUPERVISOR_IPC_SOCKET.unlink()
            except Exception:
                pass

        # Set up default command handlers (v113.0 + v115.0)
        self._command_handlers: Dict[IPCCommand, Callable] = {
            # v113.0 commands
            IPCCommand.STATUS: self._handle_status,
            IPCCommand.PING: self._handle_ping,
            IPCCommand.RESTART: self._handle_restart,
            IPCCommand.SHUTDOWN: self._handle_shutdown,
            IPCCommand.TAKEOVER: self._handle_takeover,
            IPCCommand.FORCE_STOP: self._handle_force_stop,
            # v115.0 commands
            IPCCommand.HEALTH: self._handle_health,
            IPCCommand.CROSS_REPO_STATUS: self._handle_cross_repo_status,
            IPCCommand.METRICS: self._handle_metrics,
            IPCCommand.DIAGNOSTICS: self._handle_diagnostics,
        }

        # Override with custom handlers if provided
        if command_handlers:
            for cmd, handler in command_handlers.items():
                self._command_handlers[cmd] = handler
        
        # Create and start server
        try:
            server = await asyncio.start_unix_server(
                self._handle_ipc_connection,
                path=str(SUPERVISOR_IPC_SOCKET),
            )
            self._ipc_server = server
            
            # Make socket world-readable for other processes
            os.chmod(str(SUPERVISOR_IPC_SOCKET), 0o666)
            
            logger.info(f"[Singleton] IPC server started: {SUPERVISOR_IPC_SOCKET}")
            
            # Keep server running in background
            asyncio.create_task(self._ipc_server_loop(server))
            
        except Exception as e:
            logger.warning(f"[Singleton] IPC server failed to start: {e}")
    
    async def _ipc_server_loop(self, server) -> None:
        """Run IPC server until shutdown."""
        try:
            async with server:
                await server.serve_forever()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug(f"[Singleton] IPC server ended: {e}")
    
    async def _handle_ipc_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Handle incoming IPC connection."""
        try:
            # Read command (timeout: 5s)
            data = await asyncio.wait_for(reader.read(4096), timeout=5.0)
            if not data:
                writer.close()
                await writer.wait_closed()
                return
            
            # Parse command
            try:
                request = json.loads(data.decode())
                command = request.get("command", "")
                args = request.get("args", {})
            except json.JSONDecodeError:
                response = {"success": False, "error": "Invalid JSON"}
                writer.write(json.dumps(response).encode())
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                return
            
            # Handle command
            try:
                cmd_enum = IPCCommand(command)
                handler = self._command_handlers.get(cmd_enum)
                
                if handler:
                    result = await handler(args)
                    response = {"success": True, "result": result}
                else:
                    response = {"success": False, "error": f"Unknown command: {command}"}
                    
            except ValueError:
                response = {"success": False, "error": f"Invalid command: {command}"}
            except Exception as e:
                response = {"success": False, "error": str(e)}
            
            # Send response
            writer.write(json.dumps(response).encode())
            await writer.drain()
            
        except asyncio.TimeoutError:
            logger.debug("[Singleton] IPC connection timed out")
        except Exception as e:
            logger.debug(f"[Singleton] IPC connection error: {e}")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
    
    async def _handle_status(self, args: Dict) -> Dict[str, Any]:
        """Handle STATUS command - return current supervisor status."""
        state = self._state
        if state:
            return {
                "running": True,
                "pid": state.pid,
                "entry_point": state.entry_point,
                "started_at": state.started_at,
                "last_heartbeat": state.last_heartbeat,
                "uptime_seconds": (datetime.now() - datetime.fromisoformat(state.started_at)).total_seconds(),
            }
        return {"running": False}
    
    async def _handle_ping(self, args: Dict) -> Dict[str, Any]:
        """Handle PING command - simple liveness check."""
        return {"pong": True, "timestamp": datetime.now().isoformat()}
    
    async def _handle_restart(self, args: Dict) -> Dict[str, Any]:
        """
        Handle RESTART command - trigger clean restart via SIGHUP/os.execv().

        v109.4: Uses os.execv() to replace the process, avoiding atexit handlers
        and the EXC_GUARD crash that occurred with async cleanup during shutdown.
        """
        logger.info("[Singleton] Restart requested via IPC - will use os.execv()")
        try:
            # SIGHUP triggers _handle_sighup which calls os.execv()
            os.kill(os.getpid(), signal.SIGHUP)
            return {"restart_initiated": True, "method": "execv"}
        except Exception as e:
            return {"restart_initiated": False, "error": str(e)}
    
    async def _handle_shutdown(self, args: Dict) -> Dict[str, Any]:
        """Handle SHUTDOWN command - graceful shutdown."""
        logger.info("[Singleton] Shutdown requested via IPC")
        try:
            os.kill(os.getpid(), signal.SIGTERM)
            return {"shutdown_initiated": True}
        except Exception as e:
            return {"shutdown_initiated": False, "error": str(e)}
    
    async def _handle_takeover(self, args: Dict) -> Dict[str, Any]:
        """Handle TAKEOVER command - new instance wants to take over."""
        logger.info("[Singleton] Takeover requested via IPC")
        # Set takeover flag and initiate graceful shutdown
        try:
            self._takeover_requested = True
            # Give new instance a chance to start, then shutdown
            asyncio.create_task(self._delayed_takeover_shutdown())
            return {"takeover_accepted": True, "message": "Shutting down in 5 seconds for takeover"}
        except Exception as e:
            return {"takeover_accepted": False, "error": str(e)}
    
    async def _handle_force_stop(self, args: Dict) -> Dict[str, Any]:
        """Handle FORCE_STOP command - immediate shutdown."""
        logger.warning("[Singleton] Force stop requested via IPC")
        try:
            os.kill(os.getpid(), signal.SIGKILL)
            return {"force_stop_initiated": True}
        except Exception as e:
            return {"force_stop_initiated": False, "error": str(e)}
    
    async def _delayed_takeover_shutdown(self) -> None:
        """Shutdown after delay for takeover."""
        await asyncio.sleep(5.0)
        if getattr(self, '_takeover_requested', False):
            logger.info("[Singleton] Takeover: shutting down now")
            os.kill(os.getpid(), signal.SIGTERM)

    # =========================================================================
    # v115.0: ENHANCED IPC COMMAND HANDLERS
    # =========================================================================

    async def _handle_health(self, args: Dict) -> Dict[str, Any]:
        """
        v115.0: Handle HEALTH command - comprehensive health report.

        Returns detailed health information including all strategy results.
        """
        state = self._state
        if not state:
            return {"healthy": False, "error": "No supervisor state"}

        health_checker = get_health_checker()

        # Run parallel health checks for comprehensive report
        results = await health_checker.check_health_parallel(state)

        # Determine overall health
        overall_healthy = all(r.healthy for r in results.values())
        max_level = max((r.level for r in results.values()), default=HealthLevel.UNKNOWN)

        return {
            "healthy": overall_healthy,
            "health_level": max_level.name,
            "health_level_value": int(max_level),
            "checks": {name: result.to_dict() for name, result in results.items()},
            "uptime_seconds": state.get_uptime_seconds(),
            "heartbeat_age_seconds": state.get_heartbeat_age_seconds(),
            "pid": state.pid,
            "entry_point": state.entry_point,
            "version": state.version
        }

    async def _handle_cross_repo_status(self, args: Dict) -> Dict[str, Any]:
        """
        v115.0: Handle CROSS_REPO_STATUS command - cross-repo integration status.

        Returns status of connected repositories (Prime, Reactor).
        """
        cross_repo_status = {}

        # Check JARVIS Prime status
        prime_state_file = CROSS_REPO_DIR / "prime_state.json"
        if prime_state_file.exists():
            try:
                data = json.loads(prime_state_file.read_text())
                file_age = time.time() - prime_state_file.stat().st_mtime
                cross_repo_status["jarvis_prime"] = {
                    "connected": file_age < 60,
                    "last_update_seconds_ago": file_age,
                    "status": data.get("status", "unknown"),
                    "details": data
                }
            except Exception as e:
                cross_repo_status["jarvis_prime"] = {"connected": False, "error": str(e)}
        else:
            cross_repo_status["jarvis_prime"] = {"connected": False, "error": "State file not found"}

        # Check Reactor Core status
        reactor_state_file = CROSS_REPO_DIR / "reactor_state.json"
        if reactor_state_file.exists():
            try:
                data = json.loads(reactor_state_file.read_text())
                file_age = time.time() - reactor_state_file.stat().st_mtime
                cross_repo_status["reactor_core"] = {
                    "connected": file_age < 60,
                    "last_update_seconds_ago": file_age,
                    "status": data.get("status", "unknown"),
                    "details": data
                }
            except Exception as e:
                cross_repo_status["reactor_core"] = {"connected": False, "error": str(e)}
        else:
            cross_repo_status["reactor_core"] = {"connected": False, "error": "State file not found"}

        # Check heartbeat file
        heartbeat_file = CROSS_REPO_DIR / "heartbeat.json"
        if heartbeat_file.exists():
            try:
                data = json.loads(heartbeat_file.read_text())
                file_age = time.time() - heartbeat_file.stat().st_mtime
                cross_repo_status["heartbeat"] = {
                    "active": file_age < 60,
                    "last_update_seconds_ago": file_age,
                    "details": data
                }
            except Exception as e:
                cross_repo_status["heartbeat"] = {"active": False, "error": str(e)}
        else:
            cross_repo_status["heartbeat"] = {"active": False, "error": "Heartbeat file not found"}

        # Summary
        connected_count = sum(
            1 for v in cross_repo_status.values()
            if v.get("connected") or v.get("active")
        )

        return {
            "total_repos": 3,
            "connected_repos": connected_count,
            "all_connected": connected_count >= 2,  # At least JARVIS + one other
            "repos": cross_repo_status
        }

    async def _handle_metrics(self, args: Dict) -> Dict[str, Any]:
        """
        v115.0: Handle METRICS command - performance metrics.

        Returns resource usage and performance statistics.
        """
        metrics = {
            "timestamp": time.time(),
            "pid": os.getpid()
        }

        try:
            import psutil
            proc = psutil.Process(os.getpid())

            metrics["cpu_percent"] = proc.cpu_percent(interval=0.1)
            metrics["memory_mb"] = proc.memory_info().rss / 1024 / 1024
            metrics["memory_percent"] = proc.memory_percent()
            metrics["num_threads"] = proc.num_threads()
            metrics["num_fds"] = proc.num_fds() if hasattr(proc, 'num_fds') else -1

            # Connection counts (using net_connections for psutil >= 5.3.0)
            try:
                connections = proc.net_connections() if hasattr(proc, 'net_connections') else proc.connections()
                metrics["connections"] = {
                    "total": len(connections),
                    "established": sum(1 for c in connections if c.status == 'ESTABLISHED'),
                    "listening": sum(1 for c in connections if c.status == 'LISTEN')
                }
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                metrics["connections"] = {"error": "Access denied"}

            # Open files
            try:
                metrics["open_files"] = len(proc.open_files())
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                metrics["open_files"] = -1

        except ImportError:
            metrics["error"] = "psutil not available"
        except Exception as e:
            metrics["error"] = str(e)

        # Add uptime
        if self._state:
            metrics["uptime_seconds"] = self._state.get_uptime_seconds()

        return metrics

    async def _handle_diagnostics(self, args: Dict) -> Dict[str, Any]:
        """
        v115.0: Handle DIAGNOSTICS command - diagnostic information.

        Returns detailed diagnostic info for troubleshooting.
        """
        diagnostics = {
            "timestamp": time.time(),
            "version": "115.0.0",
            "pid": os.getpid(),
            "python_version": sys.version,
            "platform": sys.platform
        }

        # State information
        if self._state:
            diagnostics["state"] = self._state.to_dict()
        else:
            diagnostics["state"] = None

        # Lock status
        diagnostics["lock"] = {
            "is_locked": self.is_locked(),
            "lock_file": str(SUPERVISOR_LOCK_FILE),
            "lock_file_exists": SUPERVISOR_LOCK_FILE.exists(),
            "state_file": str(SUPERVISOR_STATE_FILE),
            "state_file_exists": SUPERVISOR_STATE_FILE.exists()
        }

        # IPC status
        diagnostics["ipc"] = {
            "socket_path": str(SUPERVISOR_IPC_SOCKET),
            "socket_exists": SUPERVISOR_IPC_SOCKET.exists(),
            "server_running": hasattr(self, '_ipc_server') and self._ipc_server is not None
        }

        # Configuration
        diagnostics["config"] = {
            "stale_lock_threshold": STALE_LOCK_THRESHOLD,
            "heartbeat_interval": HEARTBEAT_INTERVAL,
            "health_check_timeout": HEALTH_CHECK_TIMEOUT,
            "ipc_timeout": IPC_TIMEOUT,
            "http_health_ports": HTTP_HEALTH_PORTS
        }

        # Directory structure
        diagnostics["directories"] = {
            "lock_dir": str(LOCK_DIR),
            "lock_dir_exists": LOCK_DIR.exists(),
            "cross_repo_dir": str(CROSS_REPO_DIR),
            "cross_repo_dir_exists": CROSS_REPO_DIR.exists(),
            "trinity_readiness_dir": str(TRINITY_READINESS_DIR),
            "trinity_readiness_dir_exists": TRINITY_READINESS_DIR.exists()
        }

        # Environment
        diagnostics["environment"] = {
            "JARVIS_LOCK_DIR": os.environ.get("JARVIS_LOCK_DIR"),
            "JARVIS_CROSS_REPO_DIR": os.environ.get("JARVIS_CROSS_REPO_DIR"),
            "JARVIS_STALE_LOCK_THRESHOLD": os.environ.get("JARVIS_STALE_LOCK_THRESHOLD")
        }

        return diagnostics

    def cleanup_ipc(self) -> None:
        """Clean up IPC socket on shutdown."""
        try:
            if SUPERVISOR_IPC_SOCKET.exists():
                state = self._read_state()
                # Only remove if we own it
                if state and state.pid == os.getpid():
                    SUPERVISOR_IPC_SOCKET.unlink()
        except Exception:
            pass


# Module-level convenience functions
_singleton: Optional[SupervisorSingleton] = None


def get_singleton() -> SupervisorSingleton:
    """Get the singleton instance."""
    global _singleton
    if _singleton is None:
        _singleton = SupervisorSingleton()
    return _singleton


def acquire_supervisor_lock(entry_point: str) -> bool:
    """
    Acquire the supervisor lock.

    Args:
        entry_point: Name of the entry point

    Returns:
        True if lock acquired, False if another instance running
    """
    return get_singleton().acquire(entry_point)


def release_supervisor_lock() -> None:
    """Release the supervisor lock."""
    get_singleton().release()


async def start_supervisor_heartbeat() -> None:
    """Start heartbeat to keep lock fresh."""
    await get_singleton().start_heartbeat()


def is_supervisor_running() -> Tuple[bool, Optional[Dict[str, Any]]]:
    """
    v115.0: Enhanced check if a supervisor is already running.

    Uses multi-layer health verification to ensure the reported
    supervisor is actually healthy and responsive.

    Returns:
        (is_running, state_dict or None)
    """
    singleton = get_singleton()
    is_stale, state = singleton._is_lock_stale()

    if state and not is_stale:
        # Additional verification: add health level to state dict
        state_dict = state.to_dict()
        state_dict["_verified_healthy"] = True
        state_dict["_verification_timestamp"] = time.time()
        return True, state_dict

    return False, None


async def is_supervisor_running_async() -> Tuple[bool, Optional[Dict[str, Any]], Optional[HealthCheckResult]]:
    """
    v115.0: Async version with comprehensive health check.

    Returns:
        (is_running, state_dict or None, health_check_result or None)
    """
    singleton = get_singleton()
    is_stale, state = singleton._is_lock_stale()

    if state and not is_stale:
        # Run full async health check
        health_checker = get_health_checker()
        health_result = await health_checker.check_health(state, min_level=HealthLevel.IPC_RESPONSIVE)

        if health_result.healthy:
            state_dict = state.to_dict()
            state_dict["_health_check"] = health_result.to_dict()
            return True, state_dict, health_result

        # Supervisor exists but isn't healthy - treat as stale
        logger.warning(
            f"[Singleton] Supervisor PID {state.pid} exists but health check failed: "
            f"{health_result.message}"
        )
        return False, None, health_result

    return False, None, None


def get_supervisor_health_report() -> Dict[str, Any]:
    """
    v115.0: Get comprehensive health report for running supervisor.

    Returns detailed health information including all check results.
    """
    singleton = get_singleton()
    state = singleton._read_state()

    if state is None:
        return {
            "running": False,
            "message": "No supervisor state found"
        }

    # Check if process exists
    process_exists = singleton._is_process_alive(state.pid)
    is_jarvis = singleton._is_jarvis_process(state.pid) if process_exists else False
    is_stale, _ = singleton._is_lock_stale()

    report = {
        "running": not is_stale,
        "pid": state.pid,
        "entry_point": state.entry_point,
        "started_at": state.started_at,
        "uptime_seconds": state.get_uptime_seconds(),
        "last_heartbeat": state.last_heartbeat,
        "heartbeat_age_seconds": state.get_heartbeat_age_seconds(),
        "process_exists": process_exists,
        "is_jarvis_process": is_jarvis,
        "is_stale": is_stale,
        "health_level": state.health_level,
        "cross_repo_status": state.cross_repo_status,
        "version": state.version,
        "machine_id": state.machine_id,
        "hostname": state.hostname,
        "working_dir": state.working_dir
    }

    # Add functional health status
    if process_exists and not is_stale:
        report["functionally_healthy"] = singleton._is_supervisor_functionally_healthy_sync(state)
    else:
        report["functionally_healthy"] = False

    return report


# =========================================================================
# v113.0: IPC CLIENT FUNCTIONS
# =========================================================================

async def send_supervisor_command(
    command: str,
    args: Optional[Dict[str, Any]] = None,
    timeout: float = 5.0
) -> Dict[str, Any]:
    """
    v113.0: Send IPC command to running supervisor.
    
    Args:
        command: Command name (status, ping, restart, shutdown, takeover, force-stop)
        args: Optional command arguments
        timeout: Connection timeout in seconds
    
    Returns:
        Response dict from supervisor or error dict
    """
    if not SUPERVISOR_IPC_SOCKET.exists():
        return {"success": False, "error": "No supervisor IPC socket found"}
    
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(SUPERVISOR_IPC_SOCKET)),
            timeout=timeout
        )
        
        # Send command
        request = {"command": command, "args": args or {}}
        writer.write(json.dumps(request).encode())
        await writer.drain()
        
        # Read response
        data = await asyncio.wait_for(reader.read(4096), timeout=timeout)
        response = json.loads(data.decode())
        
        writer.close()
        await writer.wait_closed()
        
        return response
        
    except asyncio.TimeoutError:
        return {"success": False, "error": "Supervisor IPC timeout"}
    except ConnectionRefusedError:
        return {"success": False, "error": "Supervisor not responding"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def send_supervisor_command_sync(
    command: str,
    args: Optional[Dict[str, Any]] = None,
    timeout: float = 5.0
) -> Dict[str, Any]:
    """
    v113.0: Synchronous wrapper for send_supervisor_command.
    """
    try:
        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(
            send_supervisor_command(command, args, timeout)
        )
        loop.close()
        return result
    except Exception as e:
        return {"success": False, "error": str(e)}


async def start_supervisor_ipc_server() -> None:
    """v113.0: Start the IPC server on the running singleton."""
    await get_singleton().start_ipc_server()


# =============================================================================
# v109.4: SIGHUP HANDLER FOR CLEAN RESTART VIA os.execv()
# =============================================================================

_sighup_handler_installed = False


def _setup_sighup_handler() -> None:
    """
    v109.4: Set up SIGHUP handler for clean restart via os.execv().

    This is the PRIMARY FIX for the EXC_GUARD crash on restart.

    Why os.execv() is the right solution:
    - os.execv() replaces the current process image with a new one
    - atexit handlers are NOT called (process is replaced, not exited)
    - No file descriptor cleanup race conditions
    - Clean slate for the new process
    - Standard Unix pattern for daemon restarts

    The EXC_GUARD crash happened because:
    1. Restart sent SIGHUP which triggered atexit handlers
    2. atexit handlers tried to create new event loops during interpreter shutdown
    3. GCP client libraries use guarded FDs (libdispatch/GCD on macOS)
    4. Attempting to close guarded FDs during shutdown triggers EXC_GUARD

    By using os.execv(), we bypass atexit entirely and avoid the crash.
    """
    global _sighup_handler_installed

    if _sighup_handler_installed:
        return

    def _handle_sighup(signum, frame):
        """
        Handle SIGHUP for restart - use os.execv() to avoid atexit issues.

        CRITICAL: This handler must NOT:
        - Create new event loops
        - Import modules that use guarded FDs
        - Call async code
        - Run complex cleanup

        It simply releases the lock and replaces the process.
        """
        logger.info("[Singleton] SIGHUP received - initiating clean restart via os.execv()")

        # Step 1: Release file lock BEFORE execv (sync, no event loop needed)
        try:
            if _singleton and _singleton._lock_fd is not None:
                # Just unlock - don't close FD, let execv/kernel handle it
                try:
                    fcntl.flock(_singleton._lock_fd, fcntl.LOCK_UN)
                    logger.debug("[Singleton] Lock released for restart")
                except Exception as e:
                    logger.debug(f"Lock unlock warning: {e}")
                _singleton._lock_fd = None
        except Exception as e:
            logger.debug(f"Lock release warning during SIGHUP: {e}")

        # Step 2: Clean up state file so new process starts fresh
        try:
            SUPERVISOR_STATE_FILE.unlink(missing_ok=True)
            logger.debug("[Singleton] State file removed for restart")
        except Exception as e:
            logger.debug(f"State file cleanup warning: {e}")

        # Step 3: Clean up IPC socket
        try:
            if SUPERVISOR_IPC_SOCKET.exists():
                SUPERVISOR_IPC_SOCKET.unlink()
                logger.debug("[Singleton] IPC socket removed for restart")
        except Exception as e:
            logger.debug(f"IPC socket cleanup warning: {e}")

        # Step 4: v109.5: Kill Trinity subprocesses before restart
        # Without this, J-Prime/Reactor-Core will still be running after execv,
        # causing "address already in use" errors when new supervisor tries to launch them.
        try:
            import psutil
            current_pid = os.getpid()
            current_proc = psutil.Process(current_pid)
            children = current_proc.children(recursive=True)
            if children:
                logger.info(f"[Singleton] Terminating {len(children)} child process(es) before restart")
                for child in children:
                    try:
                        child.terminate()
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
                # Wait briefly for graceful termination
                _, alive = psutil.wait_procs(children, timeout=3.0)
                # Force kill any survivors
                for proc in alive:
                    try:
                        logger.debug(f"[Singleton] Force killing stubborn process PID {proc.pid}")
                        proc.kill()
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
        except ImportError:
            # psutil not available - try basic approach
            try:
                import subprocess
                subprocess.run(['pkill', '-P', str(os.getpid())], timeout=5, capture_output=True)
            except Exception:
                pass
        except Exception as e:
            logger.debug(f"Child process cleanup warning: {e}")

        # Step 5: Restart via execv - this REPLACES the process (atexit NOT called)
        python = sys.executable
        args = [python] + sys.argv
        logger.info(f"[Singleton] Executing: {' '.join(args[:5])}...")

        # This replaces the current process image - code after this never runs
        os.execv(python, args)
        # UNREACHABLE: process has been replaced

    # Install the handler
    signal.signal(signal.SIGHUP, _handle_sighup)
    _sighup_handler_installed = True
    logger.debug("[Singleton] v109.4: SIGHUP handler installed for clean restart")


def setup_restart_handlers() -> None:
    """
    v109.4: Set up all restart-related signal handlers.

    Call this after acquiring the supervisor lock.
    """
    _setup_sighup_handler()


# =============================================================================
# v109.4: ATEXIT CLEANUP (MINIMAL - most restart bypasses this via os.execv())
# =============================================================================

import atexit


def _cleanup_on_exit():
    """
    Clean up lock and IPC socket on exit.

    NOTE: For --restart, this is NOT called because os.execv() bypasses atexit.
    This only runs for normal exit, SIGTERM, or SIGINT.

    v109.4: Keep this minimal to avoid EXC_GUARD crashes during interpreter shutdown.
    """
    try:
        if _singleton:
            if _singleton.is_locked():
                # Use minimal sync release - no async, no new imports
                try:
                    if _singleton._lock_fd is not None:
                        fcntl.flock(_singleton._lock_fd, fcntl.LOCK_UN)
                        # Don't close FD during atexit - kernel will clean up
                        _singleton._lock_fd = None
                except Exception:
                    pass

            # Clean up IPC socket
            try:
                _singleton.cleanup_ipc()
            except Exception:
                pass
    except Exception:
        pass


atexit.register(_cleanup_on_exit)
