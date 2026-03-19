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
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import (
    Any, Callable, Dict, FrozenSet, Optional,
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
            _fdatasync = getattr(os, "fdatasync", None)
            if _fdatasync is not None:
                try:
                    _fdatasync(fd)
                except OSError:
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
# StartupEventBusAdapter
# ---------------------------------------------------------------------------
# (Defined here so vm_lifecycle_manager.py is the only file that imports
#  startup_telemetry. VMLifecycleManager only holds a LifecycleTelemetrySink.)

class StartupEventBusAdapter:
    """Implements LifecycleTelemetrySink. Bridges LifecycleTransitionEvent → StartupEvent."""
    def __init__(self, bus: Any) -> None:
        self._bus: Any = bus  # StartupEventBus; typed Any to avoid importing StartupEventBus

    async def emit(self, event: LifecycleTransitionEvent) -> None:
        startup_event = self._bus.create_event(
            event_type="vm_lifecycle_transition",
            detail={
                "from_state": event.from_state.value,
                "to_state": event.to_state.value,
                "trigger": event.trigger,
                "reason_code": event.reason_code,
                "strategy": event.strategy,
                "latency_s": event.latency_s,
                "retry_count": event.retry_count,
                "active_work_count": event.active_work_count_at_transition,
                "meaningful_count": event.meaningful_count_at_transition,
                "detail": event.detail,
            },
            phase=None,
            authority_state="",  # lifecycle events carry no routing authority_state
        )
        await self._bus.emit(startup_event)


# ---------------------------------------------------------------------------
# VMLifecycleManager
# ---------------------------------------------------------------------------

class VMLifecycleManager:
    """Single authoritative FSM owner for GCP VM lifecycle (v298.0).

    Construction:
        mgr = VMLifecycleManager(config, controller, telemetry_sink)
        await mgr.start()   # acquires lease, binds event loop
        ...
        await mgr.stop()    # cancels tasks, releases lease

    ensure_warmed(reason) — single canonical warm entrypoint.
    work_slot(ActivityClass) — drain-safe context manager for callers.
    record_activity_from(caller_id) — classified activity signal.
    request_shutdown(reason) — explicit shutdown (e.g., supervisor stop).
    """

    def __init__(
        self,
        config: VMLifecycleConfig,
        controller: VMController,
        telemetry_sink: LifecycleTelemetrySink,
        *,
        _clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._controller = controller
        self._telemetry_sink = telemetry_sink
        self._clock = _clock

        # FSM
        self._state = VMFsmState.COLD
        self._lock = asyncio.Lock()
        self._state_entered_mono: float = 0.0

        # Lease
        self._lease = LifecycleLease(config.lease_dir)
        self._session_id: str = ""

        # ensure_warmed deduplication
        self._warming_future: Optional[asyncio.Future] = None

        # Drain counters
        self._meaningful_count: int = 0
        self._non_meaningful_count: int = 0
        self._drain_clear_event: Optional[asyncio.Event] = None  # initialized in start()

        # Timer tasks
        self._idle_timer_task:    Optional[asyncio.Task] = None
        self._grace_period_task:  Optional[asyncio.Task] = None
        self._max_uptime_task:    Optional[asyncio.Task] = None

        # Timing bookmarks
        self._last_meaningful_mono: float = 0.0
        self._warm_started_mono:    float = 0.0
        self._idle_grace_entered_mono: float = 0.0

        # Failure tracking
        self._last_warming_failure: Optional[Tuple[object, object]] = None
        self._retry_count: int = 0
        self._warm_strike_count: int = 0
        self._warm_backoff_until: float = 0.0

        # Boot mode
        self._boot_mode: BootMode = BootMode.NORMAL
        self._boot_mode_record: Optional[BootModeRecord] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Bind event loop, acquire LifecycleLease, initialize drain event."""
        self._drain_clear_event = asyncio.Event()
        self._drain_clear_event.set()
        self._state = VMFsmState.COLD
        self._state_entered_mono = self._clock()
        session_id = self._lease.acquire()
        self._session_id = session_id
        _log.info("VMLifecycleManager started: session=%s", session_id)

    async def stop(self) -> None:
        """Cancel all background tasks, release lease. Idempotent."""
        for task in (self._idle_timer_task, self._grace_period_task, self._max_uptime_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._idle_timer_task = None
        self._grace_period_task = None
        self._max_uptime_task = None
        self._lease.release()
        _log.info("VMLifecycleManager stopped: session=%s", self._session_id)

    # ------------------------------------------------------------------
    # ensure_warmed — single canonical warm entrypoint
    # ------------------------------------------------------------------

    async def ensure_warmed(self, reason: str) -> bool:
        """Drive COLD → WARMING → READY.

        Concurrent calls collapse: all callers await the same Future.
        If already READY/IN_USE/IDLE_GRACE, returns True immediately.
        Returns False on handshake failure or too many strikes.
        """
        async with self._lock:
            if self._state in (VMFsmState.READY, VMFsmState.IN_USE, VMFsmState.IDLE_GRACE):
                return True
            if self._state == VMFsmState.STOPPING:
                return False
            if self._state == VMFsmState.WARMING:
                # Collapse: return the in-progress future
                assert self._warming_future is not None, "WARMING state must have warming_future"
                fut = self._warming_future
            else:
                # COLD — check backoff
                if self._clock() < self._warm_backoff_until:
                    _log.info("ensure_warmed(%s): backoff active — returning False", reason)
                    return False
                fut = asyncio.get_event_loop().create_future()
                self._warming_future = fut
                self._state = VMFsmState.WARMING
                self._state_entered_mono = self._clock()
                self._retry_count = 0
                asyncio.create_task(self._run_warming(fut, reason))

        # Wait for the warming future outside the lock
        try:
            return await asyncio.shield(fut)
        except Exception:
            return False

    async def _run_warming(self, fut: asyncio.Future, reason: str) -> None:
        """Execute VM start + handshake. Resolves fut on completion."""
        try:
            success, failed_step, failure_class = await self._controller.start_vm()
            async with self._lock:
                if success:
                    self._state = VMFsmState.READY
                    self._state_entered_mono = self._clock()
                    self._warm_started_mono = self._clock()
                    self._last_meaningful_mono = self._clock()
                    self._warming_future = None
                    self._last_warming_failure = None
                    self._warm_strike_count = 0
                    # Spawn idle timer
                    self._idle_timer_task = asyncio.create_task(self._idle_timer_coro())
                    # Spawn max_uptime if configured
                    if self._config.max_uptime_s is not None:
                        self._max_uptime_task = asyncio.create_task(self._max_uptime_coro())
                    event = self._build_transition_event(
                        from_state=VMFsmState.WARMING, to_state=VMFsmState.READY,
                        trigger="handshake_success", reason_code="HANDSHAKE_SUCCESS",
                    )
                else:
                    self._state = VMFsmState.COLD
                    self._state_entered_mono = self._clock()
                    self._warming_future = None
                    self._last_warming_failure = (failed_step, failure_class)
                    self._warm_strike_count += 1
                    if self._warm_strike_count >= self._config.warm_max_strikes:
                        backoff = min(60.0 * (2 ** (self._warm_strike_count - self._config.warm_max_strikes)), 600.0)
                        self._warm_backoff_until = self._clock() + backoff
                        _log.warning("ensure_warmed: %d strikes — backoff %.0fs", self._warm_strike_count, backoff)
                    event = self._build_transition_event(
                        from_state=VMFsmState.WARMING, to_state=VMFsmState.COLD,
                        trigger="handshake_failed", reason_code="HANDSHAKE_FAILED",
                    )
            asyncio.create_task(self._emit_safe(event))
            if not fut.done():
                fut.set_result(success)
        except Exception as exc:
            _log.exception("_run_warming exception: %s", exc)
            async with self._lock:
                self._state = VMFsmState.COLD
                self._warming_future = None
            if not fut.done():
                fut.set_exception(exc)

    # ------------------------------------------------------------------
    # request_shutdown
    # ------------------------------------------------------------------

    async def request_shutdown(self, reason: str = "") -> None:
        """Explicit shutdown: READY/IN_USE/IDLE_GRACE → STOPPING → COLD."""
        async with self._lock:
            if self._state not in (VMFsmState.READY, VMFsmState.IN_USE, VMFsmState.IDLE_GRACE):
                return
            from_state = self._state
            self._state = VMFsmState.STOPPING
            self._state_entered_mono = self._clock()
            for t in (self._idle_timer_task, self._grace_period_task):
                if t and not t.done():
                    t.cancel()
            self._idle_timer_task = None
            self._grace_period_task = None
            event = self._build_transition_event(
                from_state=from_state, to_state=VMFsmState.STOPPING,
                trigger="request_shutdown", reason_code="EXPLICIT_SHUTDOWN",
                detail=reason,
            )
        asyncio.create_task(self._emit_safe(event))
        asyncio.create_task(self._execute_stop())

    async def _execute_stop(self) -> None:
        """Call controller.stop_vm() then transition to COLD."""
        try:
            await self._controller.stop_vm()
        except Exception as exc:
            _log.error("_execute_stop controller error: %s", exc)
        async with self._lock:
            from_state = self._state
            self._state = VMFsmState.COLD
            self._state_entered_mono = self._clock()
            event = self._build_transition_event(
                from_state=from_state, to_state=VMFsmState.COLD,
                trigger="stop_confirmed", reason_code="STOP_CONFIRMED",
            )
        asyncio.create_task(self._emit_safe(event))

    # ------------------------------------------------------------------
    # record_activity_from
    # ------------------------------------------------------------------

    def record_activity_from(self, caller_id: str) -> None:
        """Classify and record activity. MEANINGFUL resets idle timer."""
        source = _ACTIVITY_REGISTRY.get(caller_id)
        if source is None:
            if self._config.strict_drain:
                raise UnregisteredActivitySourceError(caller_id)
            _log.warning("record_activity_from: unregistered caller %r — classifying NON_MEANINGFUL", caller_id)
            activity_class = ActivityClass.NON_MEANINGFUL
        else:
            activity_class = source.activity_class
        self._record_activity(activity_class)

    def _record_activity(self, activity_class: ActivityClass) -> None:
        if activity_class == ActivityClass.MEANINGFUL:
            self._last_meaningful_mono = self._clock()
            # Reset idle timer
            if self._idle_timer_task and not self._idle_timer_task.done():
                self._idle_timer_task.cancel()
            self._idle_timer_task = asyncio.create_task(self._idle_timer_coro())

    # ------------------------------------------------------------------
    # Internal timer coroutines
    # ------------------------------------------------------------------

    async def _idle_timer_coro(self) -> None:
        """Single-shot: fires exactly at inactivity threshold."""
        deadline = self._last_meaningful_mono + self._effective_threshold_s()
        remaining = deadline - self._clock()
        if remaining > 0:
            try:
                await asyncio.sleep(remaining)
            except asyncio.CancelledError:
                return
        await self._on_inactivity_elapsed()

    async def _on_inactivity_elapsed(self) -> None:
        async with self._lock:
            if self._state not in (VMFsmState.READY, VMFsmState.IN_USE):
                return
            from_state = self._state
            if self._state == VMFsmState.READY or (self._state == VMFsmState.IN_USE and self._meaningful_count == 0):
                self._state = VMFsmState.IDLE_GRACE
                self._idle_grace_entered_mono = self._clock()
                self._state_entered_mono = self._clock()
                event = self._build_transition_event(
                    from_state=from_state, to_state=VMFsmState.IDLE_GRACE,
                    trigger="inactivity_threshold_elapsed", reason_code="IDLE_THRESHOLD_ELAPSED",
                )
                self._grace_period_task = asyncio.create_task(self._grace_period_coro())
            else:
                # IN_USE with work in flight — re-arm timer so it fires after the work drains
                self._idle_timer_task = asyncio.create_task(self._idle_timer_coro())
                return
        asyncio.create_task(self._emit_safe(event))

    async def _grace_period_coro(self) -> None:
        """Single-shot grace period: fire after idle_grace_s, then await drain."""
        deadline = self._idle_grace_entered_mono + self._config.idle_grace_s
        remaining = deadline - self._clock()
        if remaining > 0:
            try:
                await asyncio.sleep(remaining)
            except asyncio.CancelledError:
                return
        # Grace elapsed — await drain (event-driven, not polling)
        if not self._drain_clear():
            # Hard cap timer
            hard_cap_task = asyncio.create_task(asyncio.sleep(self._config.drain_hard_cap_s))
            assert self._drain_clear_event is not None
            drain_task = asyncio.create_task(self._drain_clear_event.wait())
            done, pending = await asyncio.wait(
                {hard_cap_task, drain_task}, return_when=asyncio.FIRST_COMPLETED
            )
            for t in pending:
                t.cancel()
            if hard_cap_task in done:
                event = self._build_transition_event(
                    from_state=VMFsmState.IDLE_GRACE, to_state=VMFsmState.IDLE_GRACE,
                    trigger="drain_hard_cap_exceeded",
                    reason_code="MAX_UPTIME_DRAIN_HARD_CAP_EXCEEDED",
                )
                asyncio.create_task(self._emit_safe(event))
                return  # Leave running — never force-stop an active stream
        await self._on_grace_and_drain_complete()

    async def _on_grace_and_drain_complete(self) -> None:
        async with self._lock:
            if self._state != VMFsmState.IDLE_GRACE:
                return
            self._state = VMFsmState.STOPPING
            self._state_entered_mono = self._clock()
            event = self._build_transition_event(
                from_state=VMFsmState.IDLE_GRACE, to_state=VMFsmState.STOPPING,
                trigger="grace_and_drain_complete", reason_code="DRAIN_COMPLETE",
            )
        asyncio.create_task(self._emit_safe(event))
        asyncio.create_task(self._execute_stop())

    async def _max_uptime_coro(self) -> None:
        """Single-shot: fires at max_uptime_s after READY entry.

        Only spawned when max_uptime_s is not None.
        """
        assert self._config.max_uptime_s is not None
        try:
            await asyncio.sleep(self._config.max_uptime_s)
        except asyncio.CancelledError:
            return
        await self._on_max_uptime_elapsed()

    async def _on_max_uptime_elapsed(self) -> None:
        async with self._lock:
            if self._state not in (VMFsmState.READY, VMFsmState.IN_USE):
                return
            from_state = self._state
            self._state = VMFsmState.IDLE_GRACE
            self._idle_grace_entered_mono = self._clock()
            self._state_entered_mono = self._clock()
            if self._idle_timer_task and not self._idle_timer_task.done():
                self._idle_timer_task.cancel()
            self._idle_timer_task = None
            event = self._build_transition_event(
                from_state=from_state, to_state=VMFsmState.IDLE_GRACE,
                trigger="max_uptime_elapsed", reason_code="MAX_UPTIME_ELAPSED",
            )
            self._grace_period_task = asyncio.create_task(self._grace_period_coro())
        asyncio.create_task(self._emit_safe(event))

    def _effective_threshold_s(self) -> float:
        """Return inactivity threshold, optionally reduced by quiet hours factor."""
        if self._config.quiet_hours is not None:
            h = time.localtime().tm_hour
            start_h, end_h = self._config.quiet_hours
            in_quiet = (start_h <= h or h < end_h) if start_h > end_h else (start_h <= h < end_h)
            if in_quiet:
                reduced = self._config.inactivity_threshold_s * self._config.quiet_hours_threshold_factor
                return max(60.0, reduced)
        return self._config.inactivity_threshold_s

    def _drain_clear(self) -> bool:
        return self._meaningful_count == 0

    # ------------------------------------------------------------------
    # Telemetry helpers
    # ------------------------------------------------------------------

    def _build_transition_event(
        self,
        from_state: VMFsmState,
        to_state: VMFsmState,
        trigger: str,
        reason_code: str,
        strategy: Optional[str] = None,
        detail: Optional[str] = None,
    ) -> LifecycleTransitionEvent:
        return LifecycleTransitionEvent(
            session_id=self._session_id,
            timestamp_mono=self._clock(),
            timestamp_wall=time.time(),
            from_state=from_state,
            to_state=to_state,
            trigger=trigger,
            reason_code=reason_code,
            strategy=strategy,
            latency_s=self._clock() - self._state_entered_mono,
            retry_count=self._retry_count,
            active_work_count_at_transition=self._meaningful_count + self._non_meaningful_count,
            meaningful_count_at_transition=self._meaningful_count,
            detail=detail,
        )

    async def _emit_safe(self, event: LifecycleTransitionEvent) -> None:
        try:
            await self._telemetry_sink.emit(event)
        except Exception as exc:
            _log.warning("lifecycle telemetry failed: %s", exc)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def state(self) -> VMFsmState:
        return self._state

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def active_work_count(self) -> int:
        return self._meaningful_count + self._non_meaningful_count

    @property
    def meaningful_work_count(self) -> int:
        return self._meaningful_count

    @property
    def uptime_s(self) -> Optional[float]:
        if self._state in (VMFsmState.COLD, VMFsmState.WARMING):
            return None
        return self._clock() - self._warm_started_mono

    @property
    def boot_mode(self) -> BootMode:
        return self._boot_mode

    @property
    def boot_mode_record(self) -> Optional[BootModeRecord]:
        return self._boot_mode_record

    def set_degraded_boot_mode(self, reason: str, degraded_capabilities: FrozenSet[str]) -> None:
        self._boot_mode = BootMode.DEGRADED
        self._boot_mode_record = BootModeRecord(
            mode=BootMode.DEGRADED,
            reason=reason,
            degraded_capabilities=degraded_capabilities,
            entered_at_wall=time.time(),
        )
        _log.warning("VMLifecycleManager: DEGRADED_BOOT_MODE reason=%s caps=%s", reason, degraded_capabilities)
