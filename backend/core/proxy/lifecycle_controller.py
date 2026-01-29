"""
Proxy Lifecycle Controller - Layer 3 of Distributed Proxy System

Manages Cloud SQL proxy process with:
- State machine with enforced valid transitions
- launchd service for macOS persistence (survives reboot/sleep)
- Sleep/wake detection with proactive restart
- Predictive restart using EMA latency tracking
- Integration with existing CloudSQLProxyManager

Author: JARVIS System
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import tempfile
import time
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    AsyncIterator,
    Callable,
    Coroutine,
    Dict,
    Final,
    List,
    Optional,
    Protocol,
    Set,
    TypeVar,
)

if TYPE_CHECKING:
    from .distributed_leader import DistributedProxyLeader

logger = logging.getLogger(__name__)


# =============================================================================
# Configuration (All from Environment - Zero Hardcoding)
# =============================================================================

class ProxyConfig:
    """Configuration loaded entirely from environment variables."""

    # Proxy binary and connection
    PROXY_BINARY: Final[str] = os.getenv(
        "CLOUDSQL_PROXY_BINARY",
        "/opt/homebrew/bin/cloud-sql-proxy"
    )
    PROXY_PORT: Final[int] = int(os.getenv("CLOUDSQL_PROXY_PORT", "5432"))
    PROXY_HOST: Final[str] = os.getenv("CLOUDSQL_PROXY_HOST", "127.0.0.1")

    # GCP configuration
    GCP_PROJECT: Final[str] = os.getenv("GCP_PROJECT", "")
    GCP_REGION: Final[str] = os.getenv("GCP_REGION", "us-west1")
    GCP_INSTANCE: Final[str] = os.getenv("CLOUDSQL_INSTANCE_NAME", "")
    GCP_CREDENTIALS: Final[str] = os.getenv(
        "GOOGLE_APPLICATION_CREDENTIALS",
        str(Path.home() / ".config/gcloud/application_default_credentials.json")
    )

    # v125.0: Graceful degradation mode - allow startup without CloudSQL
    OPTIONAL_MODE: Final[bool] = os.getenv("CLOUDSQL_OPTIONAL", "true").lower() == "true"
    SKIP_IF_UNCONFIGURED: Final[bool] = os.getenv("CLOUDSQL_SKIP_IF_UNCONFIGURED", "true").lower() == "true"

    @classmethod
    def is_configured(cls) -> bool:
        """v125.0: Check if CloudSQL is properly configured."""
        has_connection_string = bool(cls.get_connection_string())
        has_credentials = bool(cls.GCP_CREDENTIALS) and Path(cls.GCP_CREDENTIALS).exists()
        has_binary = bool(cls.PROXY_BINARY) and Path(cls.PROXY_BINARY).exists()
        return bool(has_connection_string and has_credentials and has_binary)

    @classmethod
    def get_configuration_issues(cls) -> List[str]:
        """v125.0: Get list of configuration issues preventing CloudSQL startup."""
        issues = []
        if not cls.get_connection_string():
            issues.append(
                f"No connection string: Set GCP_PROJECT ('{cls.GCP_PROJECT}') and "
                f"CLOUDSQL_INSTANCE_NAME ('{cls.GCP_INSTANCE}')"
            )
        if not cls.GCP_CREDENTIALS:
            issues.append("No GOOGLE_APPLICATION_CREDENTIALS environment variable")
        elif not Path(cls.GCP_CREDENTIALS).exists():
            issues.append(f"Credentials file not found: {cls.GCP_CREDENTIALS}")
        if not cls.PROXY_BINARY:
            issues.append("No CLOUDSQL_PROXY_BINARY environment variable")
        elif not Path(cls.PROXY_BINARY).exists():
            issues.append(f"Proxy binary not found: {cls.PROXY_BINARY}")
        return issues

    # State machine timeouts
    STARTUP_TIMEOUT: Final[float] = float(os.getenv("PROXY_STARTUP_TIMEOUT", "30.0"))
    VERIFICATION_TIMEOUT: Final[float] = float(os.getenv("PROXY_VERIFICATION_TIMEOUT", "15.0"))
    SHUTDOWN_TIMEOUT: Final[float] = float(os.getenv("PROXY_SHUTDOWN_TIMEOUT", "10.0"))
    HEALTH_CHECK_INTERVAL: Final[float] = float(os.getenv("PROXY_HEALTH_CHECK_INTERVAL", "5.0"))

    # Recovery settings
    MAX_RECOVERY_ATTEMPTS: Final[int] = int(os.getenv("PROXY_MAX_RECOVERY_ATTEMPTS", "5"))
    RECOVERY_BACKOFF_BASE: Final[float] = float(os.getenv("PROXY_RECOVERY_BACKOFF_BASE", "1.0"))
    RECOVERY_BACKOFF_MAX: Final[float] = float(os.getenv("PROXY_RECOVERY_BACKOFF_MAX", "60.0"))

    # Sleep/wake detection
    WAKE_DELAY: Final[float] = float(os.getenv("PROXY_WAKE_DELAY", "2.0"))
    WAKE_DETECTION_METHOD: Final[str] = os.getenv("PROXY_WAKE_DETECTION", "auto")  # auto, darwin, polling

    # Predictive restart (EMA-based)
    EMA_ALPHA: Final[float] = float(os.getenv("PROXY_EMA_ALPHA", "0.3"))
    EMA_WARMUP_SAMPLES: Final[int] = int(os.getenv("PROXY_EMA_WARMUP_SAMPLES", "20"))
    DEGRADATION_THRESHOLD: Final[float] = float(os.getenv("PROXY_DEGRADATION_THRESHOLD", "3.0"))
    PREEMPTIVE_RESTART_ENABLED: Final[bool] = os.getenv("PROXY_PREEMPTIVE_RESTART", "true").lower() == "true"

    # launchd configuration
    LAUNCHD_ENABLED: Final[bool] = os.getenv("PROXY_LAUNCHD_ENABLED", "true").lower() == "true"
    LAUNCHD_LABEL: Final[str] = os.getenv("PROXY_LAUNCHD_LABEL", "com.jarvis.cloudsql-proxy")
    LAUNCHD_THROTTLE_INTERVAL: Final[int] = int(os.getenv("PROXY_RESTART_THROTTLE", "30"))

    # State persistence
    STATE_DIR: Final[Path] = Path(os.getenv("JARVIS_STATE_DIR", str(Path.home() / ".jarvis")))
    PID_FILE: Final[Path] = STATE_DIR / "proxy" / "cloudsql-proxy.pid"
    STATE_FILE: Final[Path] = STATE_DIR / "proxy" / "lifecycle_state.json"

    @classmethod
    def get_connection_string(cls) -> str:
        """Build the Cloud SQL connection string."""
        if cls.GCP_PROJECT and cls.GCP_INSTANCE:
            return f"{cls.GCP_PROJECT}:{cls.GCP_REGION}:{cls.GCP_INSTANCE}"
        # Try loading from database config
        config_path = cls.STATE_DIR / "gcp" / "database_config.json"
        if config_path.exists():
            try:
                with open(config_path) as f:
                    config = json.load(f)
                return config.get("connection_name", "")
            except (json.JSONDecodeError, OSError):
                pass
        return ""

    @classmethod
    def get_launchd_plist_path(cls) -> Path:
        """Get the launchd plist file path."""
        return Path.home() / "Library" / "LaunchAgents" / f"{cls.LAUNCHD_LABEL}.plist"


# =============================================================================
# Proxy State Machine
# =============================================================================

class ProxyState(Enum):
    """
    Proxy lifecycle states with strict transition rules.

    State diagram:
    UNKNOWN ──────┬──> STOPPED
                  ├──> STARTING (existing proxy found)
                  └──> READY (existing proxy verified)

    STOPPED ──────────> STARTING

    STARTING ─────┬──> VERIFYING (process started)
                  ├──> STOPPED (start failed)
                  └──> DEAD (crash during start)

    VERIFYING ────┬──> READY (verification passed)
                  ├──> STARTING (verification failed, retry)
                  └──> DEAD (unrecoverable error)

    READY ────────┬──> DEGRADED (latency spike)
                  ├──> STOPPED (graceful shutdown)
                  └──> RECOVERING (health check failed)

    DEGRADED ─────┬──> READY (self-healed)
                  ├──> RECOVERING (still degraded)
                  └──> DEAD (degradation too long)

    RECOVERING ───┬──> STARTING (restart needed)
                  ├──> READY (recovered without restart)
                  └──> DEAD (recovery failed)

    DEAD ─────────────> STARTING (resurrection attempt)
    """
    UNKNOWN = auto()
    STOPPED = auto()
    STARTING = auto()
    VERIFYING = auto()
    READY = auto()
    DEGRADED = auto()
    RECOVERING = auto()
    DEAD = auto()


# Valid state transitions (enforced by state machine)
VALID_TRANSITIONS: Dict[ProxyState, Set[ProxyState]] = {
    ProxyState.UNKNOWN: {ProxyState.STOPPED, ProxyState.STARTING, ProxyState.READY},
    ProxyState.STOPPED: {ProxyState.STARTING},
    ProxyState.STARTING: {ProxyState.VERIFYING, ProxyState.STOPPED, ProxyState.DEAD},
    ProxyState.VERIFYING: {ProxyState.READY, ProxyState.STARTING, ProxyState.DEAD},
    ProxyState.READY: {ProxyState.DEGRADED, ProxyState.STOPPED, ProxyState.RECOVERING},
    ProxyState.DEGRADED: {ProxyState.READY, ProxyState.RECOVERING, ProxyState.DEAD},
    ProxyState.RECOVERING: {ProxyState.STARTING, ProxyState.READY, ProxyState.DEAD},
    ProxyState.DEAD: {ProxyState.STARTING},
}


class InvalidTransitionError(Exception):
    """Raised when an invalid state transition is attempted."""

    def __init__(self, from_state: ProxyState, to_state: ProxyState):
        self.from_state = from_state
        self.to_state = to_state
        valid = VALID_TRANSITIONS.get(from_state, set())
        super().__init__(
            f"Invalid transition: {from_state.name} -> {to_state.name}. "
            f"Valid transitions from {from_state.name}: {[s.name for s in valid]}"
        )


# =============================================================================
# State Transition Events
# =============================================================================

@dataclass(frozen=True)
class StateTransitionEvent:
    """Immutable record of a state transition."""
    timestamp: float
    from_state: ProxyState
    to_state: ProxyState
    reason: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    correlation_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary for persistence."""
        return {
            "timestamp": self.timestamp,
            "from_state": self.from_state.name,
            "to_state": self.to_state.name,
            "reason": reason if (reason := self.reason) else "unspecified",
            "metadata": self.metadata,
            "correlation_id": self.correlation_id,
        }


# =============================================================================
# EMA Latency Tracker (Predictive Health)
# =============================================================================

@dataclass
class EMALatencyTracker:
    """
    Exponential Moving Average tracker for latency-based health prediction.

    Detects degradation trends before complete failure, enabling
    preemptive restart to maintain availability.
    """
    alpha: float = ProxyConfig.EMA_ALPHA
    warmup_samples: int = ProxyConfig.EMA_WARMUP_SAMPLES
    degradation_threshold: float = ProxyConfig.DEGRADATION_THRESHOLD

    # Internal state
    _ema: Optional[float] = field(default=None, init=False)
    _baseline: Optional[float] = field(default=None, init=False)
    _sample_count: int = field(default=0, init=False)
    _warmup_samples_list: List[float] = field(default_factory=list, init=False)
    _baseline_locked: bool = field(default=False, init=False)

    def record(self, latency_ms: float) -> None:
        """Record a latency sample and update EMA."""
        self._sample_count += 1

        # During warmup, collect samples for baseline
        if not self._baseline_locked:
            self._warmup_samples_list.append(latency_ms)
            if len(self._warmup_samples_list) >= self.warmup_samples:
                # Compute robust baseline (median to reduce outlier impact)
                sorted_samples = sorted(self._warmup_samples_list)
                mid = len(sorted_samples) // 2
                self._baseline = sorted_samples[mid]
                self._baseline_locked = True
                logger.info(
                    f"[EMATracker] Baseline established: {self._baseline:.2f}ms "
                    f"(from {self.warmup_samples} samples)"
                )

        # Update EMA
        if self._ema is None:
            self._ema = latency_ms
        else:
            self._ema = self.alpha * latency_ms + (1 - self.alpha) * self._ema

    @property
    def is_warmed_up(self) -> bool:
        """Check if enough samples collected for baseline."""
        return self._baseline_locked

    @property
    def current_ema(self) -> Optional[float]:
        """Get current EMA value."""
        return self._ema

    @property
    def baseline(self) -> Optional[float]:
        """Get baseline latency."""
        return self._baseline

    @property
    def degradation_ratio(self) -> Optional[float]:
        """Get ratio of current EMA to baseline."""
        if self._ema is not None and self._baseline is not None and self._baseline > 0:
            return self._ema / self._baseline
        return None

    def is_degraded(self) -> bool:
        """Check if latency indicates degradation."""
        ratio = self.degradation_ratio
        if ratio is None:
            return False
        return ratio > self.degradation_threshold

    def needs_preemptive_restart(self) -> bool:
        """Check if preemptive restart is recommended."""
        if not ProxyConfig.PREEMPTIVE_RESTART_ENABLED:
            return False
        if not self.is_warmed_up:
            return False
        return self.is_degraded()

    def reset(self) -> None:
        """Reset tracker state (after restart)."""
        self._ema = None
        self._baseline = None
        self._sample_count = 0
        self._warmup_samples_list.clear()
        self._baseline_locked = False

    def get_status(self) -> Dict[str, Any]:
        """Get current tracker status."""
        return {
            "sample_count": self._sample_count,
            "is_warmed_up": self.is_warmed_up,
            "baseline_ms": self._baseline,
            "current_ema_ms": self._ema,
            "degradation_ratio": self.degradation_ratio,
            "is_degraded": self.is_degraded(),
            "preemptive_restart_recommended": self.needs_preemptive_restart(),
        }


# =============================================================================
# Sleep/Wake Detection
# =============================================================================

class SleepWakeDetector(ABC):
    """Abstract base class for sleep/wake detection."""

    @abstractmethod
    async def start(self, callback: Callable[[], Coroutine[Any, Any, None]]) -> None:
        """Start detection, calling callback on wake."""
        pass

    @abstractmethod
    async def stop(self) -> None:
        """Stop detection."""
        pass


class DarwinSleepWakeDetector(SleepWakeDetector):
    """
    macOS sleep/wake detection using Darwin notification center.

    Uses PyObjC to register for system sleep/wake notifications.
    Falls back to polling if PyObjC not available.
    """

    def __init__(self):
        self._callback: Optional[Callable[[], Coroutine[Any, Any, None]]] = None
        self._observer: Any = None
        self._running = False
        self._pyobjc_available = False

        # Try importing PyObjC
        try:
            import Foundation
            import objc
            self._Foundation = Foundation
            self._objc = objc
            self._pyobjc_available = True
        except ImportError:
            logger.warning(
                "[SleepWakeDetector] PyObjC not available, using polling fallback"
            )

    async def start(self, callback: Callable[[], Coroutine[Any, Any, None]]) -> None:
        """Start sleep/wake detection."""
        self._callback = callback
        self._running = True

        if self._pyobjc_available:
            await self._start_darwin_notifications()
        else:
            asyncio.create_task(self._polling_fallback())

    async def _start_darwin_notifications(self) -> None:
        """Register for Darwin system notifications."""
        try:
            # Create notification center observer
            workspace = self._Foundation.NSWorkspace.sharedWorkspace()
            nc = workspace.notificationCenter()

            # Define callback selector
            def wake_handler(notification: Any) -> None:
                """Handle wake notification."""
                logger.info("[SleepWakeDetector] System wake detected")
                if self._callback:
                    asyncio.create_task(self._callback())

            # Register for wake notification
            nc.addObserverForName_object_queue_usingBlock_(
                "NSWorkspaceDidWakeNotification",
                None,
                None,
                wake_handler
            )

            logger.info("[SleepWakeDetector] Darwin wake notifications registered")

        except Exception as e:
            logger.warning(
                f"[SleepWakeDetector] Darwin registration failed: {e}, using polling"
            )
            asyncio.create_task(self._polling_fallback())

    async def _polling_fallback(self) -> None:
        """Poll system uptime to detect sleep/wake cycles."""
        last_time = time.monotonic()

        while self._running:
            await asyncio.sleep(1.0)

            current_time = time.monotonic()
            elapsed = current_time - last_time

            # If elapsed time >> 1 second, system likely slept
            if elapsed > 5.0:
                logger.info(
                    f"[SleepWakeDetector] Detected time jump ({elapsed:.1f}s), "
                    "likely wake from sleep"
                )
                if self._callback:
                    await asyncio.sleep(ProxyConfig.WAKE_DELAY)
                    await self._callback()

            last_time = current_time

    async def stop(self) -> None:
        """Stop detection."""
        self._running = False
        if self._observer:
            # Remove observer
            try:
                workspace = self._Foundation.NSWorkspace.sharedWorkspace()
                nc = workspace.notificationCenter()
                nc.removeObserver_(self._observer)
            except Exception:
                pass


class PollingSleepWakeDetector(SleepWakeDetector):
    """Simple polling-based sleep/wake detector (cross-platform)."""

    def __init__(self):
        self._callback: Optional[Callable[[], Coroutine[Any, Any, None]]] = None
        self._running = False
        self._task: Optional[asyncio.Task[None]] = None

    async def start(self, callback: Callable[[], Coroutine[Any, Any, None]]) -> None:
        """Start polling detection."""
        self._callback = callback
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())

    async def _poll_loop(self) -> None:
        """Poll for time jumps indicating sleep/wake."""
        last_time = time.monotonic()

        while self._running:
            await asyncio.sleep(1.0)

            current_time = time.monotonic()
            elapsed = current_time - last_time

            if elapsed > 5.0:
                logger.info(f"[SleepWakeDetector] Time jump detected: {elapsed:.1f}s")
                if self._callback:
                    await asyncio.sleep(ProxyConfig.WAKE_DELAY)
                    await self._callback()

            last_time = current_time

    async def stop(self) -> None:
        """Stop polling."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass


def create_sleep_wake_detector() -> SleepWakeDetector:
    """Factory function to create appropriate detector."""
    method = ProxyConfig.WAKE_DETECTION_METHOD.lower()

    if method == "darwin":
        return DarwinSleepWakeDetector()
    elif method == "polling":
        return PollingSleepWakeDetector()
    else:  # auto
        # Try Darwin first on macOS
        import platform
        if platform.system() == "Darwin":
            return DarwinSleepWakeDetector()
        return PollingSleepWakeDetector()


# =============================================================================
# launchd Service Manager
# =============================================================================

class LaunchdServiceManager:
    """
    Manages the Cloud SQL proxy as a launchd service for persistence.

    Features:
    - Survives reboots (RunAtLoad)
    - Auto-restarts on crash (KeepAlive)
    - Throttle interval to prevent restart loops
    - Environment variable injection
    """

    def __init__(self):
        self._plist_path = ProxyConfig.get_launchd_plist_path()
        self._label = ProxyConfig.LAUNCHD_LABEL

    def generate_plist(self) -> str:
        """Generate launchd plist XML content."""
        connection_string = ProxyConfig.get_connection_string()
        credentials_path = ProxyConfig.GCP_CREDENTIALS

        # Build proxy arguments
        proxy_args = [
            ProxyConfig.PROXY_BINARY,
            f"--address={ProxyConfig.PROXY_HOST}",
            f"--port={ProxyConfig.PROXY_PORT}",
            connection_string,
        ]

        # Build plist content
        plist = f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{self._label}</string>

    <key>ProgramArguments</key>
    <array>
        {"".join(f"<string>{arg}</string>" for arg in proxy_args)}
    </array>

    <key>EnvironmentVariables</key>
    <dict>
        <key>GOOGLE_APPLICATION_CREDENTIALS</key>
        <string>{credentials_path}</string>
    </dict>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>

    <key>ThrottleInterval</key>
    <integer>{ProxyConfig.LAUNCHD_THROTTLE_INTERVAL}</integer>

    <key>StandardOutPath</key>
    <string>{ProxyConfig.STATE_DIR}/logs/cloudsql-proxy.log</string>

    <key>StandardErrorPath</key>
    <string>{ProxyConfig.STATE_DIR}/logs/cloudsql-proxy.error.log</string>

    <key>ProcessType</key>
    <string>Background</string>

    <key>LowPriorityIO</key>
    <false/>

    <key>Nice</key>
    <integer>-5</integer>
</dict>
</plist>'''
        return plist

    async def install(self) -> bool:
        """Install and load the launchd service."""
        try:
            # Ensure parent directories exist
            self._plist_path.parent.mkdir(parents=True, exist_ok=True)
            log_dir = ProxyConfig.STATE_DIR / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)

            # Unload existing service if present
            await self.unload()

            # Write plist file
            plist_content = self.generate_plist()
            self._plist_path.write_text(plist_content)
            logger.info(f"[LaunchdManager] Wrote plist to {self._plist_path}")

            # Load the service
            result = await asyncio.create_subprocess_exec(
                "launchctl", "load", "-w", str(self._plist_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await result.communicate()

            if result.returncode != 0:
                logger.error(
                    f"[LaunchdManager] Failed to load service: {stderr.decode()}"
                )
                return False

            logger.info(f"[LaunchdManager] Service {self._label} loaded successfully")
            return True

        except Exception as e:
            logger.error(f"[LaunchdManager] Installation failed: {e}")
            return False

    async def unload(self) -> bool:
        """Unload the launchd service."""
        try:
            result = await asyncio.create_subprocess_exec(
                "launchctl", "unload", str(self._plist_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await result.communicate()
            # Ignore errors (service may not be loaded)
            return True
        except Exception as e:
            logger.warning(f"[LaunchdManager] Unload warning: {e}")
            return False

    async def is_running(self) -> bool:
        """Check if the launchd service is running."""
        try:
            result = await asyncio.create_subprocess_exec(
                "launchctl", "list", self._label,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await result.communicate()

            if result.returncode != 0:
                return False

            # Parse output to check PID
            output = stdout.decode()
            # Format: "PID\tStatus\tLabel"
            parts = output.strip().split("\t")
            if len(parts) >= 1 and parts[0] != "-":
                return True
            return False

        except Exception:
            return False

    async def get_pid(self) -> Optional[int]:
        """Get the PID of the running service."""
        try:
            result = await asyncio.create_subprocess_exec(
                "launchctl", "list", self._label,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await result.communicate()

            if result.returncode != 0:
                return None

            output = stdout.decode()
            parts = output.strip().split("\t")
            if len(parts) >= 1 and parts[0] != "-":
                return int(parts[0])
            return None

        except Exception:
            return None

    async def restart(self) -> bool:
        """Restart the launchd service."""
        try:
            # Stop
            await asyncio.create_subprocess_exec(
                "launchctl", "stop", self._label,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.sleep(1.0)

            # Start
            result = await asyncio.create_subprocess_exec(
                "launchctl", "start", self._label,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await result.communicate()

            if result.returncode != 0:
                logger.error(f"[LaunchdManager] Restart failed: {stderr.decode()}")
                return False

            logger.info(f"[LaunchdManager] Service {self._label} restarted")
            return True

        except Exception as e:
            logger.error(f"[LaunchdManager] Restart error: {e}")
            return False

    def is_installed(self) -> bool:
        """Check if the plist file exists."""
        return self._plist_path.exists()


# =============================================================================
# State Change Callback Protocol
# =============================================================================

class StateChangeCallback(Protocol):
    """Protocol for state change callbacks."""

    async def __call__(
        self,
        event: StateTransitionEvent,
    ) -> None:
        """Called when state changes."""
        ...


# =============================================================================
# Proxy Lifecycle Controller
# =============================================================================

class ProxyLifecycleController:
    """
    Main controller managing Cloud SQL proxy lifecycle.

    Features:
    - Strict state machine with valid transitions
    - launchd persistence (optional, macOS)
    - Sleep/wake detection with proactive restart
    - EMA-based predictive restart
    - Event sourcing for audit trail
    """

    def __init__(
        self,
        leader: Optional[DistributedProxyLeader] = None,
        use_launchd: bool = ProxyConfig.LAUNCHD_ENABLED,
    ):
        # State machine
        self._state = ProxyState.UNKNOWN
        self._state_lock = asyncio.Lock()
        self._state_history: List[StateTransitionEvent] = []

        # Process management
        self._process: Optional[asyncio.subprocess.Process] = None
        self._pid: Optional[int] = None
        self._start_time: Optional[float] = None

        # Recovery tracking
        self._recovery_attempts = 0
        self._last_recovery_time: Optional[float] = None

        # Health tracking
        self._latency_tracker = EMALatencyTracker()
        self._last_health_check: Optional[float] = None
        self._consecutive_failures = 0

        # Sleep/wake detection
        self._sleep_wake_detector = create_sleep_wake_detector()

        # launchd management
        self._use_launchd = use_launchd and ProxyConfig.LAUNCHD_ENABLED
        self._launchd_manager = LaunchdServiceManager() if self._use_launchd else None

        # Leader reference (for coordination)
        self._leader = leader

        # Background tasks
        self._health_monitor_task: Optional[asyncio.Task[None]] = None
        self._running = False

        # Callbacks
        self._state_callbacks: List[StateChangeCallback] = []

        # Correlation ID for tracing
        self._correlation_id = ""

    # -------------------------------------------------------------------------
    # State Machine
    # -------------------------------------------------------------------------

    @property
    def state(self) -> ProxyState:
        """Current proxy state."""
        return self._state

    @property
    def is_ready(self) -> bool:
        """Check if proxy is ready for connections."""
        return self._state == ProxyState.READY

    @property
    def is_healthy(self) -> bool:
        """Check if proxy is in a healthy state."""
        return self._state in {ProxyState.READY, ProxyState.DEGRADED}

    @property
    def pid(self) -> Optional[int]:
        """Get proxy process ID."""
        return self._pid

    @property
    def uptime_seconds(self) -> Optional[float]:
        """Get proxy uptime in seconds."""
        if self._start_time is None:
            return None
        return time.monotonic() - self._start_time

    async def _transition_to(
        self,
        new_state: ProxyState,
        reason: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Transition to a new state with validation.

        Returns True if transition was valid and completed.
        """
        async with self._state_lock:
            old_state = self._state

            # Validate transition
            valid_targets = VALID_TRANSITIONS.get(old_state, set())
            if new_state not in valid_targets:
                logger.error(
                    f"[LifecycleController] Invalid transition: {old_state.name} -> {new_state.name}"
                )
                raise InvalidTransitionError(old_state, new_state)

            # Create event
            event = StateTransitionEvent(
                timestamp=time.time(),
                from_state=old_state,
                to_state=new_state,
                reason=reason,
                metadata=metadata or {},
                correlation_id=self._correlation_id,
            )

            # Apply transition
            self._state = new_state
            self._state_history.append(event)

            logger.info(
                f"[LifecycleController] State transition: {old_state.name} -> {new_state.name} "
                f"(reason: {reason})"
            )

            # Persist state
            await self._persist_state()

            # Notify callbacks
            for callback in self._state_callbacks:
                try:
                    await callback(event)
                except Exception as e:
                    logger.error(f"[LifecycleController] Callback error: {e}")

            return True

    def add_state_callback(self, callback: StateChangeCallback) -> None:
        """Register a callback for state changes."""
        self._state_callbacks.append(callback)

    def remove_state_callback(self, callback: StateChangeCallback) -> None:
        """Unregister a state change callback."""
        self._state_callbacks.remove(callback)

    # -------------------------------------------------------------------------
    # State Persistence
    # -------------------------------------------------------------------------

    async def _persist_state(self) -> None:
        """Persist current state to disk."""
        try:
            ProxyConfig.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

            state_data = {
                "state": self._state.name,
                "pid": self._pid,
                "start_time": self._start_time,
                "recovery_attempts": self._recovery_attempts,
                "last_recovery_time": self._last_recovery_time,
                "updated_at": time.time(),
                "latency_tracker": self._latency_tracker.get_status(),
            }

            # Atomic write
            tmp_file = ProxyConfig.STATE_FILE.with_suffix(".tmp")
            with open(tmp_file, "w") as f:
                json.dump(state_data, f, indent=2)
            tmp_file.rename(ProxyConfig.STATE_FILE)

        except Exception as e:
            logger.warning(f"[LifecycleController] Failed to persist state: {e}")

    async def _load_state(self) -> bool:
        """Load persisted state from disk."""
        try:
            if not ProxyConfig.STATE_FILE.exists():
                return False

            with open(ProxyConfig.STATE_FILE) as f:
                state_data = json.load(f)

            # Restore state carefully
            state_name = state_data.get("state", "UNKNOWN")
            try:
                self._state = ProxyState[state_name]
            except KeyError:
                self._state = ProxyState.UNKNOWN

            self._pid = state_data.get("pid")
            self._recovery_attempts = state_data.get("recovery_attempts", 0)
            self._last_recovery_time = state_data.get("last_recovery_time")

            logger.info(
                f"[LifecycleController] Restored state: {self._state.name}, "
                f"PID: {self._pid}"
            )
            return True

        except Exception as e:
            logger.warning(f"[LifecycleController] Failed to load state: {e}")
            return False

    # -------------------------------------------------------------------------
    # Process Management
    # -------------------------------------------------------------------------

    async def _find_existing_proxy(self) -> Optional[int]:
        """Find an existing proxy process."""
        try:
            # Check PID file first
            if ProxyConfig.PID_FILE.exists():
                pid_str = ProxyConfig.PID_FILE.read_text().strip()
                if pid_str:
                    pid = int(pid_str)
                    if self._is_process_running(pid):
                        return pid

            # Check launchd
            if self._launchd_manager:
                pid = await self._launchd_manager.get_pid()
                if pid:
                    return pid

            # Search by process name
            result = await asyncio.create_subprocess_exec(
                "pgrep", "-f", "cloud-sql-proxy",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await result.communicate()

            if result.returncode == 0:
                pids = stdout.decode().strip().split("\n")
                if pids and pids[0]:
                    return int(pids[0])

            return None

        except Exception as e:
            logger.warning(f"[LifecycleController] Error finding proxy: {e}")
            return None

    def _is_process_running(self, pid: int) -> bool:
        """Check if a process with given PID is running."""
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False

    async def _start_proxy_process(self) -> bool:
        """Start the proxy process (direct or via launchd)."""
        try:
            connection_string = ProxyConfig.get_connection_string()
            if not connection_string:
                logger.error("[LifecycleController] No connection string configured")
                return False

            # If using launchd, install and let launchd manage
            if self._use_launchd and self._launchd_manager:
                logger.info("[LifecycleController] Starting via launchd")

                if not self._launchd_manager.is_installed():
                    success = await self._launchd_manager.install()
                    if not success:
                        logger.error("[LifecycleController] Failed to install launchd service")
                        return False
                else:
                    success = await self._launchd_manager.restart()
                    if not success:
                        return False

                # Wait for launchd to start the process
                for _ in range(10):
                    await asyncio.sleep(1.0)
                    pid = await self._launchd_manager.get_pid()
                    if pid:
                        self._pid = pid
                        self._start_time = time.monotonic()
                        await self._write_pid_file()
                        return True

                logger.error("[LifecycleController] launchd failed to start proxy")
                return False

            # Direct process start
            logger.info("[LifecycleController] Starting proxy directly")

            cmd = [
                ProxyConfig.PROXY_BINARY,
                f"--address={ProxyConfig.PROXY_HOST}",
                f"--port={ProxyConfig.PROXY_PORT}",
                connection_string,
            ]

            env = os.environ.copy()
            env["GOOGLE_APPLICATION_CREDENTIALS"] = ProxyConfig.GCP_CREDENTIALS

            self._process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )

            self._pid = self._process.pid
            self._start_time = time.monotonic()
            await self._write_pid_file()

            logger.info(f"[LifecycleController] Proxy started with PID {self._pid}")
            return True

        except Exception as e:
            logger.error(f"[LifecycleController] Failed to start proxy: {e}")
            return False

    async def _write_pid_file(self) -> None:
        """Write current PID to file."""
        try:
            ProxyConfig.PID_FILE.parent.mkdir(parents=True, exist_ok=True)
            ProxyConfig.PID_FILE.write_text(str(self._pid or ""))
        except Exception as e:
            logger.warning(f"[LifecycleController] Failed to write PID file: {e}")

    async def _stop_proxy_process(self, graceful: bool = True) -> bool:
        """Stop the proxy process."""
        try:
            if self._use_launchd and self._launchd_manager:
                await self._launchd_manager.unload()

            if self._pid:
                try:
                    if graceful:
                        os.kill(self._pid, signal.SIGTERM)
                        # Wait for graceful shutdown
                        for _ in range(int(ProxyConfig.SHUTDOWN_TIMEOUT)):
                            await asyncio.sleep(1.0)
                            if not self._is_process_running(self._pid):
                                break
                        else:
                            # Force kill
                            os.kill(self._pid, signal.SIGKILL)
                    else:
                        os.kill(self._pid, signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    pass

            if self._process:
                try:
                    self._process.terminate()
                    await asyncio.wait_for(
                        self._process.wait(),
                        timeout=ProxyConfig.SHUTDOWN_TIMEOUT
                    )
                except asyncio.TimeoutError:
                    self._process.kill()
                except Exception:
                    pass

            self._process = None
            self._pid = None
            self._start_time = None

            # Clean up PID file
            if ProxyConfig.PID_FILE.exists():
                ProxyConfig.PID_FILE.unlink()

            return True

        except Exception as e:
            logger.error(f"[LifecycleController] Stop failed: {e}")
            return False

    # -------------------------------------------------------------------------
    # Health Monitoring
    # -------------------------------------------------------------------------

    async def _health_monitor_loop(self) -> None:
        """Background health monitoring loop."""
        while self._running:
            try:
                await asyncio.sleep(ProxyConfig.HEALTH_CHECK_INTERVAL)

                if not self._running:
                    break

                await self._perform_health_check()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[LifecycleController] Health monitor error: {e}")

    async def _perform_health_check(self) -> None:
        """Perform a single health check iteration."""
        import socket

        if self._state not in {ProxyState.READY, ProxyState.DEGRADED, ProxyState.VERIFYING}:
            return

        start_time = time.monotonic()

        try:
            # TCP connect check
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5.0)
            sock.connect((ProxyConfig.PROXY_HOST, ProxyConfig.PROXY_PORT))
            sock.close()

            latency_ms = (time.monotonic() - start_time) * 1000

            # Record latency
            self._latency_tracker.record(latency_ms)
            self._last_health_check = time.time()
            self._consecutive_failures = 0

            # Check for degradation
            if self._state == ProxyState.READY:
                if self._latency_tracker.needs_preemptive_restart():
                    logger.warning(
                        f"[LifecycleController] Latency degradation detected: "
                        f"{self._latency_tracker.current_ema:.1f}ms "
                        f"(baseline: {self._latency_tracker.baseline:.1f}ms)"
                    )
                    await self._transition_to(
                        ProxyState.DEGRADED,
                        reason="latency_degradation",
                        metadata=self._latency_tracker.get_status(),
                    )
            elif self._state == ProxyState.DEGRADED:
                # Check if recovered
                if not self._latency_tracker.is_degraded():
                    await self._transition_to(
                        ProxyState.READY,
                        reason="latency_recovered",
                        metadata=self._latency_tracker.get_status(),
                    )

        except Exception as e:
            self._consecutive_failures += 1
            logger.warning(
                f"[LifecycleController] Health check failed: {e} "
                f"(consecutive: {self._consecutive_failures})"
            )

            if self._state == ProxyState.READY:
                await self._transition_to(
                    ProxyState.RECOVERING,
                    reason=f"health_check_failed: {e}",
                    metadata={"consecutive_failures": self._consecutive_failures},
                )
            elif self._state == ProxyState.DEGRADED:
                if self._consecutive_failures >= 3:
                    await self._transition_to(
                        ProxyState.RECOVERING,
                        reason=f"degraded_health_check_failed: {e}",
                        metadata={"consecutive_failures": self._consecutive_failures},
                    )
            elif self._state == ProxyState.RECOVERING:
                if self._consecutive_failures >= 5:
                    await self._transition_to(
                        ProxyState.DEAD,
                        reason="recovery_failed",
                        metadata={"consecutive_failures": self._consecutive_failures},
                    )
                else:
                    # Trigger restart
                    await self._attempt_recovery()

    async def _attempt_recovery(self) -> None:
        """Attempt to recover the proxy."""
        self._recovery_attempts += 1
        self._last_recovery_time = time.time()

        if self._recovery_attempts > ProxyConfig.MAX_RECOVERY_ATTEMPTS:
            logger.error(
                f"[LifecycleController] Max recovery attempts ({ProxyConfig.MAX_RECOVERY_ATTEMPTS}) exceeded"
            )
            await self._transition_to(
                ProxyState.DEAD,
                reason="max_recovery_attempts",
                metadata={"attempts": self._recovery_attempts},
            )
            return

        # Calculate backoff
        backoff = min(
            ProxyConfig.RECOVERY_BACKOFF_BASE * (2 ** (self._recovery_attempts - 1)),
            ProxyConfig.RECOVERY_BACKOFF_MAX,
        )
        logger.info(
            f"[LifecycleController] Recovery attempt {self._recovery_attempts}, "
            f"backoff: {backoff:.1f}s"
        )

        await asyncio.sleep(backoff)

        # Restart proxy
        await self._transition_to(
            ProxyState.STARTING,
            reason="recovery_restart",
            metadata={"attempt": self._recovery_attempts},
        )

        await self.start()

    # -------------------------------------------------------------------------
    # Sleep/Wake Handling
    # -------------------------------------------------------------------------

    async def _handle_wake(self) -> None:
        """Handle system wake from sleep."""
        logger.info("[LifecycleController] Handling system wake")

        # Check if proxy is still healthy
        if self._pid and self._is_process_running(self._pid):
            # Verify with health check
            try:
                import socket
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(5.0)
                sock.connect((ProxyConfig.PROXY_HOST, ProxyConfig.PROXY_PORT))
                sock.close()
                logger.info("[LifecycleController] Proxy healthy after wake")
                return
            except Exception:
                pass

        # Proxy needs restart
        logger.warning("[LifecycleController] Proxy unhealthy after wake, restarting")

        if self._state in {ProxyState.READY, ProxyState.DEGRADED}:
            await self._transition_to(
                ProxyState.RECOVERING,
                reason="wake_recovery",
            )

        await self._attempt_recovery()

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    async def initialize(self) -> None:
        """Initialize the controller and discover current state."""
        logger.info("[LifecycleController] Initializing...")

        # Load persisted state
        await self._load_state()

        # Check for existing proxy
        existing_pid = await self._find_existing_proxy()

        if existing_pid:
            logger.info(f"[LifecycleController] Found existing proxy: PID {existing_pid}")
            self._pid = existing_pid
            self._start_time = time.monotonic()  # Approximate

            # Verify it's working
            try:
                import socket
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(5.0)
                sock.connect((ProxyConfig.PROXY_HOST, ProxyConfig.PROXY_PORT))
                sock.close()

                if self._state == ProxyState.UNKNOWN:
                    await self._transition_to(ProxyState.READY, reason="existing_proxy_verified")

            except Exception:
                logger.warning("[LifecycleController] Existing proxy not responding")
                if self._state == ProxyState.UNKNOWN:
                    await self._transition_to(ProxyState.STARTING, reason="existing_proxy_unresponsive")
        else:
            if self._state == ProxyState.UNKNOWN:
                await self._transition_to(ProxyState.STOPPED, reason="no_proxy_found")

        # Start sleep/wake detection
        await self._sleep_wake_detector.start(self._handle_wake)

    async def start(self) -> bool:
        """
        Start the proxy and monitoring.

        v125.0: Enhanced with graceful degradation support.
        If CloudSQL is not configured and CLOUDSQL_OPTIONAL=true, the proxy
        will transition to STOPPED (not DEAD) allowing the system to operate
        without CloudSQL connectivity.
        """
        logger.info("[LifecycleController] Starting proxy lifecycle...")

        # Ensure proper state
        if self._state == ProxyState.UNKNOWN:
            await self.initialize()

        if self._state == ProxyState.READY:
            logger.info("[LifecycleController] Proxy already ready")
            return True

        if self._state not in {ProxyState.STOPPED, ProxyState.STARTING, ProxyState.RECOVERING, ProxyState.DEAD}:
            logger.warning(f"[LifecycleController] Cannot start from state {self._state.name}")
            return False

        # v125.0: Check configuration before attempting start
        if not ProxyConfig.is_configured():
            issues = ProxyConfig.get_configuration_issues()
            logger.warning(
                f"[LifecycleController] v125.0: CloudSQL not properly configured:\n"
                f"  - " + "\n  - ".join(issues)
            )

            if ProxyConfig.SKIP_IF_UNCONFIGURED:
                logger.info(
                    "[LifecycleController] v125.0: CLOUDSQL_SKIP_IF_UNCONFIGURED=true, "
                    "operating without CloudSQL (graceful degradation)"
                )
                # Stay in STOPPED - not DEAD - to allow graceful degradation
                if self._state != ProxyState.STOPPED:
                    await self._transition_to(ProxyState.STOPPED, reason="unconfigured_graceful_skip")
                return False  # Not started, but not a fatal error

            if ProxyConfig.OPTIONAL_MODE:
                logger.warning(
                    "[LifecycleController] v125.0: CLOUDSQL_OPTIONAL=true, "
                    "attempting start anyway but will gracefully degrade on failure"
                )
            else:
                logger.error(
                    "[LifecycleController] v125.0: CloudSQL required but not configured. "
                    "Set CLOUDSQL_OPTIONAL=true or CLOUDSQL_SKIP_IF_UNCONFIGURED=true "
                    "to allow startup without CloudSQL."
                )
                await self._transition_to(ProxyState.DEAD, reason="unconfigured_required")
                return False

        # Transition to STARTING
        if self._state != ProxyState.STARTING:
            await self._transition_to(ProxyState.STARTING, reason="start_requested")

        # Start the process
        success = await self._start_proxy_process()

        if not success:
            # v125.0: Graceful degradation - use STOPPED instead of DEAD if optional
            if ProxyConfig.OPTIONAL_MODE:
                logger.warning(
                    "[LifecycleController] v125.0: Proxy start failed but CLOUDSQL_OPTIONAL=true, "
                    "transitioning to STOPPED for graceful degradation"
                )
                await self._transition_to(ProxyState.STOPPED, reason="start_failed_optional")
            else:
                await self._transition_to(ProxyState.DEAD, reason="start_failed")
            return False

        # Transition to VERIFYING
        await self._transition_to(ProxyState.VERIFYING, reason="process_started")

        # Verify connectivity
        verified = await self._verify_proxy()

        if verified:
            await self._transition_to(ProxyState.READY, reason="verification_passed")
            self._recovery_attempts = 0  # Reset on success

            # Start health monitoring
            self._running = True
            self._health_monitor_task = asyncio.create_task(self._health_monitor_loop())

            return True
        else:
            await self._transition_to(ProxyState.DEAD, reason="verification_failed")
            return False

    async def _verify_proxy(self) -> bool:
        """Verify proxy is accepting connections."""
        import socket

        deadline = time.monotonic() + ProxyConfig.VERIFICATION_TIMEOUT

        while time.monotonic() < deadline:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(2.0)
                sock.connect((ProxyConfig.PROXY_HOST, ProxyConfig.PROXY_PORT))
                sock.close()
                return True
            except Exception:
                await asyncio.sleep(0.5)

        return False

    async def stop(self, graceful: bool = True) -> bool:
        """Stop the proxy and monitoring."""
        logger.info("[LifecycleController] Stopping proxy lifecycle...")

        self._running = False

        # Stop health monitor
        if self._health_monitor_task:
            self._health_monitor_task.cancel()
            try:
                await self._health_monitor_task
            except asyncio.CancelledError:
                pass

        # Stop sleep/wake detection
        await self._sleep_wake_detector.stop()

        # Stop proxy
        if self._state in {ProxyState.READY, ProxyState.DEGRADED, ProxyState.RECOVERING}:
            await self._transition_to(ProxyState.STOPPED, reason="stop_requested")

        success = await self._stop_proxy_process(graceful=graceful)

        return success

    async def restart(self) -> bool:
        """Restart the proxy."""
        logger.info("[LifecycleController] Restarting proxy...")

        await self.stop(graceful=True)

        # Reset latency tracker
        self._latency_tracker.reset()

        return await self.start()

    @asynccontextmanager
    async def managed(self) -> AsyncIterator[ProxyLifecycleController]:
        """Context manager for lifecycle management."""
        try:
            await self.initialize()
            await self.start()
            yield self
        finally:
            await self.stop()

    def get_status(self) -> Dict[str, Any]:
        """Get current controller status."""
        return {
            "state": self._state.name,
            "is_ready": self.is_ready,
            "is_healthy": self.is_healthy,
            "pid": self._pid,
            "uptime_seconds": self.uptime_seconds,
            "recovery_attempts": self._recovery_attempts,
            "last_recovery_time": self._last_recovery_time,
            "consecutive_failures": self._consecutive_failures,
            "last_health_check": self._last_health_check,
            "latency_tracker": self._latency_tracker.get_status(),
            "launchd_enabled": self._use_launchd,
            "launchd_installed": (
                self._launchd_manager.is_installed()
                if self._launchd_manager else False
            ),
        }

    def get_state_history(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Get recent state transition history."""
        return [event.to_dict() for event in self._state_history[-limit:]]


# =============================================================================
# Factory Function
# =============================================================================

async def create_lifecycle_controller(
    leader: Optional[DistributedProxyLeader] = None,
    use_launchd: Optional[bool] = None,
) -> ProxyLifecycleController:
    """
    Factory function to create and initialize a lifecycle controller.

    Args:
        leader: Optional leader reference for coordination
        use_launchd: Override launchd setting (None = use config)

    Returns:
        Initialized ProxyLifecycleController
    """
    controller = ProxyLifecycleController(
        leader=leader,
        use_launchd=use_launchd if use_launchd is not None else ProxyConfig.LAUNCHD_ENABLED,
    )
    await controller.initialize()
    return controller
