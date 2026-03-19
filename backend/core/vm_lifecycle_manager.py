"""
VMLifecycleManager — v298.0 GCP VM Intelligent Lifecycle Management
====================================================================
Single authoritative FSM owner for the GCP VM lifecycle.

Spec: docs/superpowers/specs/2026-03-19-gcp-vm-intelligent-lifecycle-design.md
Depends on: v297.0 (gcp_readiness_lease, startup_routing_policy)
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import (
    AsyncIterator, Callable, Dict, FrozenSet, List, Optional,
    Protocol, Tuple, runtime_checkable,
)
from uuid import uuid4

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# FSM state
# ---------------------------------------------------------------------------

class VMFsmState(str, Enum):
    COLD        = "cold"
    WARMING     = "warming"
    READY       = "ready"
    IN_USE      = "in_use"
    IDLE_GRACE  = "idle_grace"
    STOPPING    = "stopping"


# ---------------------------------------------------------------------------
# Activity classification
# ---------------------------------------------------------------------------

class ActivityClass(str, Enum):
    MEANINGFUL     = "meaningful"
    NON_MEANINGFUL = "non_meaningful"


@dataclass(frozen=True)
class ActivitySource:
    caller_id: str
    activity_class: ActivityClass
    description: str


_ACTIVITY_REGISTRY: Dict[str, ActivitySource] = {
    "prime_client.execute_request":    ActivitySource("prime_client.execute_request",    ActivityClass.MEANINGFUL,     "HTTP/streaming inference request"),
    "prime_client.stream_chunks":      ActivitySource("prime_client.stream_chunks",      ActivityClass.MEANINGFUL,     "Active SSE stream consumption"),
    "prime_client.websocket_session":  ActivitySource("prime_client.websocket_session",  ActivityClass.MEANINGFUL,     "Open WS session to J-Prime"),
    "prime_client.tool_call_execute":  ActivitySource("prime_client.tool_call_execute",  ActivityClass.MEANINGFUL,     "Tool call round-trip"),
    "health_probe.probe_health":       ActivitySource("health_probe.probe_health",       ActivityClass.NON_MEANINGFUL, "/health ping"),
    "health_probe.probe_capabilities": ActivitySource("health_probe.probe_capabilities", ActivityClass.NON_MEANINGFUL, "/capabilities check"),
}


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class VMLifecycleConfig:
    inactivity_threshold_s: float        # JARVIS_VM_INACTIVITY_THRESHOLD_S   default 1800
    idle_grace_s: float                  # JARVIS_VM_IDLE_GRACE_S             default 300
    warming_await_timeout_s: float       # JARVIS_VM_WARMING_AWAIT_S          default 90
    max_uptime_s: Optional[float]        # JARVIS_VM_MAX_UPTIME_S             optional
    quiet_hours: Optional[Tuple[int, int]]  # JARVIS_VM_QUIET_HOURS="22:6"
    quiet_hours_threshold_factor: float  # JARVIS_VM_QUIET_HOURS_FACTOR       default 0.25
    drain_hard_cap_s: float              # JARVIS_VM_DRAIN_HARD_CAP_S         default 600
    warm_max_strikes: int                # JARVIS_VM_WARM_MAX_STRIKES         default 3
    lease_dir: Path                      # JARVIS_VM_LEASE_DIR      default ~/.jarvis/lifecycle/
    strict_drain: bool                   # JARVIS_VM_STRICT_DRAIN             default False

    @classmethod
    def from_env(cls) -> "VMLifecycleConfig":
        def _env(key: str, default: str) -> str:
            return os.environ.get(key, default)
        quiet_raw = os.environ.get("JARVIS_VM_QUIET_HOURS")
        quiet: Optional[Tuple[int, int]] = None
        if quiet_raw:
            parts = quiet_raw.split(":")
            quiet = (int(parts[0]), int(parts[1]))
        max_uptime_raw = os.environ.get("JARVIS_VM_MAX_UPTIME_S")
        return cls(
            inactivity_threshold_s=float(_env("JARVIS_VM_INACTIVITY_THRESHOLD_S", "1800")),
            idle_grace_s=float(_env("JARVIS_VM_IDLE_GRACE_S", "300")),
            warming_await_timeout_s=float(_env("JARVIS_VM_WARMING_AWAIT_S", "90")),
            max_uptime_s=float(max_uptime_raw) if max_uptime_raw else None,
            quiet_hours=quiet,
            quiet_hours_threshold_factor=float(_env("JARVIS_VM_QUIET_HOURS_FACTOR", "0.25")),
            drain_hard_cap_s=float(_env("JARVIS_VM_DRAIN_HARD_CAP_S", "600")),
            warm_max_strikes=int(_env("JARVIS_VM_WARM_MAX_STRIKES", "3")),
            lease_dir=Path(os.environ.get("JARVIS_VM_LEASE_DIR", Path.home() / ".jarvis" / "lifecycle")),
            strict_drain=os.environ.get("JARVIS_VM_STRICT_DRAIN", "false").lower() == "true",
        )


# ---------------------------------------------------------------------------
# Boot mode
# ---------------------------------------------------------------------------

class BootMode(str, Enum):
    NORMAL   = "normal"
    DEGRADED = "degraded"


@dataclass(frozen=True)
class BootModeRecord:
    mode: BootMode
    reason: str
    degraded_capabilities: FrozenSet[str]
    entered_at_wall: float


# ---------------------------------------------------------------------------
# VMController Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class VMController(Protocol):
    async def start_vm(self) -> Tuple[bool, Optional[object], Optional[object]]:
        """Start VM + handshake. Returns (success, failed_step, failure_class)."""
        ...
    async def stop_vm(self) -> None: ...
    def get_vm_host_port(self) -> Optional[Tuple[str, int]]: ...
    def notify_vm_unreachable(self) -> None: ...


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------

@dataclass
class LifecycleTransitionEvent:
    session_id: str
    timestamp_mono: float
    timestamp_wall: float
    from_state: VMFsmState
    to_state: VMFsmState
    trigger: str
    reason_code: str
    strategy: Optional[str]
    latency_s: float
    retry_count: int
    active_work_count_at_transition: int
    meaningful_count_at_transition: int
    detail: Optional[str] = None


class LifecycleTelemetrySink(Protocol):
    async def emit(self, event: LifecycleTransitionEvent) -> None: ...


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class DualAuthorityError(RuntimeError):
    """Raised when a live incumbent holds the lifecycle lease."""
    def __init__(self, incumbent_pid: int, incumbent_session_id: str,
                 incumbent_acquired_at: float, reason: str) -> None:
        self.incumbent_pid = incumbent_pid
        self.incumbent_session_id = incumbent_session_id
        self.incumbent_acquired_at = incumbent_acquired_at
        self.reason = reason
        super().__init__(
            f"Lifecycle lease held by pid={incumbent_pid} "
            f"session={incumbent_session_id} reason={reason}"
        )


class UnregisteredActivitySourceError(ValueError):
    """Raised in strict_drain mode for caller_ids not in _ACTIVITY_REGISTRY."""
    def __init__(self, caller_id: str) -> None:
        self.caller_id = caller_id
        super().__init__(f"Unregistered activity source: {caller_id!r}")


class VMNotReadyError(RuntimeError):
    """Raised by work_slot() when the VM is not in a usable state."""
    def __init__(self, state: VMFsmState, recovery: object,
                 failure_class: Optional[object] = None, detail: str = "") -> None:
        self.state = state
        self.recovery = recovery
        self.failure_class = failure_class
        self.detail = detail
        super().__init__(f"VM not ready: state={state.value} recovery={recovery} detail={detail}")


class LifecycleFSMError(RuntimeError):
    """Illegal FSM transition attempted."""


# ---------------------------------------------------------------------------
# LifecycleLease — process-level fencing
# ---------------------------------------------------------------------------

class LifecycleLease:
    """File-based exclusive process lease using fcntl.flock.

    Prevents dual-authority across supervisor restarts.
    """
    _LEASE_FILE = "vm_lifecycle.lease"

    def __init__(self, lease_dir: Path) -> None:
        self._lease_dir = lease_dir
        self._lease_path = lease_dir / self._LEASE_FILE
        self._session_id: Optional[str] = None
        self._fd: Optional[int] = None

    def acquire(self) -> str:
        """Acquire the lease. Returns the new session_id.

        Decision tree (see spec section 3.3):
        1. open(O_CREAT|O_RDWR) + flock(LOCK_EX|LOCK_NB)
           └─ LOCK_NB fails → DualAuthorityError(reason="flock_held")
        2. Read & parse existing JSON
           └─ parse failure → treat as stale; overwrite; log WARNING
        3. Check incumbent PID:
           ProcessLookupError → stale → overwrite
           PermissionError   → live  → DualAuthorityError(reason="pid_live")
           success + pid != getpid() → live → DualAuthorityError(reason="pid_live")
           pid == getpid() → self → overwrite (fork edge-case)
        4. Write own record; flush; fdatasync
        5. Register atexit(self.release)
        """
        self._lease_dir.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(self._lease_path), os.O_CREAT | os.O_RDWR, 0o600)
        except OSError as exc:
            raise DualAuthorityError(0, "", 0.0, f"open_failed:{exc}") from exc

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise DualAuthorityError(0, "", 0.0, "flock_held")

        # Try to read existing content
        try:
            raw = os.read(fd, 4096).decode("utf-8").strip()
            if raw:
                try:
                    data = json.loads(raw)
                    inc_pid = int(data.get("pid", 0))
                    inc_sid = str(data.get("session_id", ""))
                    inc_at  = float(data.get("acquired_at", 0.0))
                    if inc_pid and inc_pid != os.getpid():
                        try:
                            os.kill(inc_pid, 0)
                            # kill succeeded → process is alive
                            os.close(fd)
                            raise DualAuthorityError(inc_pid, inc_sid, inc_at, "pid_live")
                        except ProcessLookupError:
                            _log.warning("LifecycleLease: stale incumbent pid=%d — overwriting", inc_pid)
                        except PermissionError:
                            os.close(fd)
                            raise DualAuthorityError(inc_pid, inc_sid, inc_at, "pid_live")
                except (json.JSONDecodeError, ValueError, KeyError):
                    _log.warning("LifecycleLease: corrupt lease file — overwriting")
        except OSError:
            pass  # Empty file

        session_id = uuid4().hex
        record = json.dumps({"pid": os.getpid(), "session_id": session_id, "acquired_at": time.time()})
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            os.write(fd, record.encode("utf-8"))
            try:
                os.fdatasync(fd)
            except (OSError, AttributeError):
                pass
            self._fd = fd
        except BaseException:
            os.close(fd)
            raise

        self._session_id = session_id
        import atexit
        atexit.register(self.release)
        _log.info("LifecycleLease acquired: session=%s pid=%d", session_id, os.getpid())
        return session_id

    def release(self) -> None:
        """Release the lease. Idempotent."""
        if self._fd is None:
            return
        try:
            zero = json.dumps({"pid": 0, "session_id": "", "acquired_at": 0.0})
            os.lseek(self._fd, 0, os.SEEK_SET)
            os.ftruncate(self._fd, 0)
            os.write(self._fd, zero.encode("utf-8"))
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        _log.info("LifecycleLease released: session=%s", self._session_id)

    @property
    def session_id(self) -> Optional[str]:
        return self._session_id


# ---------------------------------------------------------------------------
# VMLifecycleManager — FSM owner (stub for Task 2)
# ---------------------------------------------------------------------------

class VMLifecycleManager:
    """Single authoritative FSM owner for the GCP VM lifecycle.

    Full implementation delivered in Task 2 (v298.0).
    This stub exposes the interface required by T18 so the test
    can import the symbol and confirm the method contract.
    """

    def __init__(
        self,
        config: VMLifecycleConfig,
        controller: VMController,
        telemetry_sink: Optional[LifecycleTelemetrySink] = None,
    ) -> None:
        self._config = config
        self._controller = controller
        self._sink = telemetry_sink
        self._state = VMFsmState.COLD
        self._session_id: Optional[str] = None
        self._lease = LifecycleLease(config.lease_dir)
        self._last_meaningful_activity_mono: float = 0.0
        self._active_work_count: int = 0
        self._meaningful_work_count: int = 0
        self._warming_event: Optional[asyncio.Event] = None
        self._monitor_task: Optional[asyncio.Task] = None
        self._started: bool = False

    # ------------------------------------------------------------------
    # Public state accessor
    # ------------------------------------------------------------------

    @property
    def state(self) -> VMFsmState:
        return self._state

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Acquire lease and start background monitor."""
        if self._started:
            return
        self._warming_event = asyncio.Event()
        self._session_id = self._lease.acquire()
        self._started = True
        _log.info("VMLifecycleManager started: session=%s", self._session_id)

    async def stop(self) -> None:
        """Stop background tasks and release lease."""
        if not self._started:
            return
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except (asyncio.CancelledError, Exception):
                pass
            self._monitor_task = None
        self._lease.release()
        self._started = False
        _log.info("VMLifecycleManager stopped: session=%s", self._session_id)

    # ------------------------------------------------------------------
    # Warming
    # ------------------------------------------------------------------

    async def ensure_warmed(self, caller: str) -> None:
        """Ensure VM is READY; start it if COLD.

        Stub implementation: calls controller.start_vm() and transitions
        COLD → WARMING → READY.  Full retry / strike logic in Task 2.
        """
        if self._state == VMFsmState.READY or self._state == VMFsmState.IN_USE:
            return
        if self._state == VMFsmState.COLD:
            self._state = VMFsmState.WARMING
            _log.info("VMLifecycleManager: warming VM (caller=%s)", caller)
            try:
                success, failed_step, failure_class = await asyncio.wait_for(
                    self._controller.start_vm(),
                    timeout=self._config.warming_await_timeout_s,
                )
            except asyncio.TimeoutError:
                self._state = VMFsmState.COLD
                raise VMNotReadyError(VMFsmState.WARMING, None, detail="warming timeout")
            if success:
                self._state = VMFsmState.READY
                if self._warming_event is not None:
                    self._warming_event.set()
                _log.info("VMLifecycleManager: VM is READY")
            else:
                self._state = VMFsmState.COLD
                raise VMNotReadyError(VMFsmState.COLD, None, failure_class,
                                      detail=f"start_vm failed step={failed_step}")

    # ------------------------------------------------------------------
    # Activity recording
    # ------------------------------------------------------------------

    def record_activity_from(self, caller_id: str) -> None:
        """Record activity from a registered caller.

        Raises UnregisteredActivitySourceError if strict_drain=True and
        caller_id is not in _ACTIVITY_REGISTRY.
        """
        if caller_id not in _ACTIVITY_REGISTRY:
            if self._config.strict_drain:
                raise UnregisteredActivitySourceError(caller_id)
            _log.debug("VMLifecycleManager: unregistered caller %r (strict_drain=False)", caller_id)
            return
        source = _ACTIVITY_REGISTRY[caller_id]
        now = time.monotonic()
        if source.activity_class == ActivityClass.MEANINGFUL:
            self._last_meaningful_activity_mono = now
        _log.debug("VMLifecycleManager: activity from %s (%s)", caller_id, source.activity_class.value)
