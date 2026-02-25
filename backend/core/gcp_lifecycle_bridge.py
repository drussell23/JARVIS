# backend/core/gcp_lifecycle_bridge.py
"""GCP lifecycle bridge: thin integration layer between unified_supervisor and
the journal-backed state machine.

Exposes simple, fire-and-forget methods that the supervisor calls at key
lifecycle moments. All methods are no-ops when JARVIS_GCP_LIFECYCLE_V2 is
disabled (the default), so the bridge can be wired in without affecting
the legacy code path.

Usage:
    bridge = await get_lifecycle_bridge()
    await bridge.notify_pressure_triggered(reason="memory_pressure", ...)
    state = bridge.get_current_state()
"""
import asyncio
import logging
import os
import socket
from pathlib import Path
from typing import Any, Dict, Optional

from backend.core.gcp_lifecycle_schema import Event, State
from backend.core.gcp_lifecycle_state_machine import (
    GCP_LIFECYCLE_V2_ENABLED,
    GCPLifecycleStateMachine,
    SideEffectAdapter,
    TransitionResult,
)

logger = logging.getLogger("jarvis.gcp_lifecycle_bridge")

# ── Module-level singleton ────────────────────────────────────────────

_bridge_instance: Optional["GCPLifecycleBridge"] = None
# NOTE: asyncio.Lock() cannot be created at module level on Python 3.9
# because it binds to the running event loop. Lazy-init in get_lifecycle_bridge().
_bridge_lock: Optional[asyncio.Lock] = None

_V2_ENABLED = GCP_LIFECYCLE_V2_ENABLED

# Default journal database path
_DEFAULT_DB_DIR = Path(
    os.environ.get(
        "JARVIS_JOURNAL_DB_DIR",
        os.path.expanduser("~/.jarvis/control"),
    )
)


async def get_lifecycle_bridge() -> "GCPLifecycleBridge":
    """Return the singleton bridge, creating it lazily on first call.

    Thread-safe via asyncio.Lock. When V2 is disabled, returns a
    bridge whose methods are all no-ops (no journal or state machine
    is created).
    """
    global _bridge_instance, _bridge_lock
    if _bridge_instance is not None:
        return _bridge_instance

    # Lazy-init the lock on first use (Python 3.9 safe — avoids binding
    # to the wrong event loop at module import time).
    if _bridge_lock is None:
        _bridge_lock = asyncio.Lock()

    lock = _bridge_lock
    async with lock:
        if _bridge_instance is not None:
            return _bridge_instance
        _bridge_instance = GCPLifecycleBridge()
        if _V2_ENABLED:
            await _bridge_instance.initialize()
        return _bridge_instance


def get_lifecycle_bridge_sync() -> Optional["GCPLifecycleBridge"]:
    """Non-async accessor for contexts that cannot await.

    Returns None if the bridge has not been initialized yet.
    """
    return _bridge_instance


def reset_lifecycle_bridge() -> None:
    """Reset the singleton (for testing only)."""
    global _bridge_instance, _bridge_lock
    _bridge_instance = None
    _bridge_lock = None


def notify_bridge(method_name: str, **kwargs) -> None:
    """Fire-and-forget lifecycle notification for non-async callers.

    Safe to call from anywhere — swallows all errors, no-ops when V2 is
    disabled or bridge not yet initialized.  Schedules the notification
    as a background task on the running event loop.

    Usage from gcp_vm_manager / supervisor_gcp_controller::

        from backend.core.gcp_lifecycle_bridge import notify_bridge
        notify_bridge("notify_vm_create_accepted", vm_name=name)
    """
    bridge = _bridge_instance
    if bridge is None:
        return

    method = getattr(bridge, method_name, None)
    if method is None:
        return

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    async def _fire() -> None:
        try:
            await asyncio.wait_for(method(**kwargs), timeout=2.0)
        except Exception:
            pass  # fire-and-forget — errors logged inside _emit()

    loop.create_task(_fire())


class GCPLifecycleBridge:
    """Thin bridge between unified_supervisor and the V2 lifecycle engine.

    All public ``notify_*`` methods:
    - Accept kwargs matching what the supervisor already knows
    - Map to the appropriate Event enum value
    - Call ``handle_event`` on the state machine
    - Return TransitionResult (or a stub result when V2 is disabled)

    The bridge owns the journal + state machine + adapter lifecycle.
    """

    def __init__(self) -> None:
        self._journal: Optional[Any] = None
        self._adapter: Optional[Any] = None
        self._engine: Optional[GCPLifecycleStateMachine] = None
        self._initialized = False
        self._target = "invincible_node"

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def initialize(
        self,
        *,
        db_path: Optional[Path] = None,
        gcp_vm_manager: Optional[Any] = None,
    ) -> None:
        """Create journal, adapter, and state machine.

        Only called when V2 is enabled. The ``gcp_vm_manager`` parameter
        allows injecting the real GCP VM manager; when None a no-op
        adapter is used (safe for testing and early startup).
        """
        if self._initialized:
            return

        from backend.core.orchestration_journal import OrchestrationJournal
        from backend.core.gcp_lifecycle_adapter import GCPLifecycleAdapter

        # Journal
        journal = OrchestrationJournal()
        path = db_path or _DEFAULT_DB_DIR / "orchestration.db"
        await journal.initialize(path)

        # Acquire lease (unique holder per process)
        holder_id = f"bridge:{os.getpid()}:{socket.gethostname()}"
        acquired = await journal.acquire_lease(holder_id)
        if not acquired:
            logger.warning(
                "[GCPLifecycleBridge] Failed to acquire journal lease — "
                "another leader may be active. Running in read-only mode."
            )

        # Adapter
        if gcp_vm_manager is not None:
            adapter = GCPLifecycleAdapter(journal, gcp_vm_manager)
        else:
            adapter = _NoOpAdapter()

        # State machine
        engine = GCPLifecycleStateMachine(
            journal, adapter, target=self._target,
        )

        # Attempt crash recovery
        await engine.recover_from_journal()

        self._journal = journal
        self._adapter = adapter
        self._engine = engine
        self._initialized = True

        # Register lease-lost callback so the state machine learns
        # immediately when another leader steals the journal lease.
        if hasattr(journal, "on_lease_lost"):
            journal.on_lease_lost(self._on_lease_lost)

        logger.info(
            "[GCPLifecycleBridge] Initialized (state=%s, holder=%s)",
            engine.state.value, holder_id,
        )

    async def shutdown(self) -> None:
        """Graceful shutdown: emit SESSION_SHUTDOWN, close journal."""
        if not self._initialized or not _V2_ENABLED:
            return

        engine = self._engine
        journal = self._journal

        if engine is not None:
            try:
                await engine.handle_event(Event.SESSION_SHUTDOWN)
            except Exception as exc:
                logger.error(
                    "[GCPLifecycleBridge] Error during shutdown event: %s", exc,
                )

        if journal is not None:
            try:
                await journal.close()
            except Exception as exc:
                logger.error(
                    "[GCPLifecycleBridge] Error closing journal: %s", exc,
                )

        self._initialized = False
        logger.info("[GCPLifecycleBridge] Shutdown complete")

    def _on_lease_lost(self, new_holder: str) -> None:
        """Callback invoked by OrchestrationJournal when lease is stolen."""
        notify_bridge("notify_lease_lost", new_holder=new_holder)

    # ── State query ───────────────────────────────────────────────────

    def _get_engine(self) -> Optional[GCPLifecycleStateMachine]:
        """Return engine if V2 is enabled and bridge initialized."""
        if not _V2_ENABLED or not self._initialized:
            return None
        return self._engine

    def get_current_state(self) -> str:
        """Return the current lifecycle state as a string.

        Returns ``"disabled"`` when V2 is off, ``"uninitialized"``
        before ``initialize()`` is called.
        """
        if not _V2_ENABLED:
            return "disabled"
        engine = self._get_engine()
        if engine is None:
            return "uninitialized"
        return engine.state.value

    def is_active(self) -> bool:
        """True when the GCP VM is considered active/healthy."""
        engine = self._get_engine()
        if engine is None:
            return False
        return engine.state in (State.ACTIVE, State.DEGRADED)

    def is_provisioning(self) -> bool:
        """True when a VM is being created or is booting."""
        engine = self._get_engine()
        if engine is None:
            return False
        return engine.state in (
            State.TRIGGERING, State.PROVISIONING, State.BOOTING, State.HANDSHAKING,
        )

    # ── Notify methods (supervisor-facing) ────────────────────────────

    async def notify_pressure_triggered(
        self, *, reason: str = "", **kwargs,
    ) -> TransitionResult:
        """Memory/load pressure detected — begin VM provisioning flow."""
        return await self._emit(
            Event.PRESSURE_TRIGGERED,
            payload={"reason": reason, **kwargs},
        )

    async def notify_budget_approved(self, **kwargs) -> TransitionResult:
        """Budget gate passed — proceed to VM creation."""
        return await self._emit(Event.BUDGET_APPROVED, payload=kwargs)

    async def notify_budget_denied(self, **kwargs) -> TransitionResult:
        """Budget gate denied — enter cooldown."""
        return await self._emit(Event.BUDGET_DENIED, payload=kwargs)

    async def notify_vm_create_accepted(self, **kwargs) -> TransitionResult:
        """GCP accepted the VM create request."""
        return await self._emit(Event.VM_CREATE_ACCEPTED, payload=kwargs)

    async def notify_vm_create_failed(self, **kwargs) -> TransitionResult:
        """VM creation failed."""
        return await self._emit(Event.VM_CREATE_FAILED, payload=kwargs)

    async def notify_vm_ready(
        self, *, ip: Optional[str] = None, **kwargs,
    ) -> TransitionResult:
        """VM is healthy and ready to serve traffic.

        Maps to HEALTH_PROBE_OK (not VM_READY) because the transition
        table uses HEALTH_PROBE_OK as the event that moves BOOTING -> ACTIVE.
        Event.VM_READY exists in the schema but has no defined transitions.
        """
        return await self._emit(
            Event.HEALTH_PROBE_OK,
            payload={"ip": ip, **kwargs},
        )

    async def notify_vm_unhealthy(self, **kwargs) -> TransitionResult:
        """Consecutive health probe failures."""
        return await self._emit(
            Event.HEALTH_UNREACHABLE_CONSECUTIVE, payload=kwargs,
        )

    async def notify_vm_degraded(self, **kwargs) -> TransitionResult:
        """VM is degraded but reachable."""
        return await self._emit(
            Event.HEALTH_DEGRADED_CONSECUTIVE, payload=kwargs,
        )

    async def notify_vm_stopped(self, **kwargs) -> TransitionResult:
        """VM has stopped (confirmed)."""
        return await self._emit(Event.VM_STOPPED, payload=kwargs)

    async def notify_spot_preempted(self, **kwargs) -> TransitionResult:
        """Spot VM was preempted by GCP."""
        return await self._emit(Event.SPOT_PREEMPTED, payload=kwargs)

    async def notify_session_shutdown(self) -> TransitionResult:
        """JARVIS session is shutting down."""
        return await self._emit(Event.SESSION_SHUTDOWN)

    async def notify_pressure_cooled(self, **kwargs) -> TransitionResult:
        """Memory/load pressure has subsided."""
        return await self._emit(Event.PRESSURE_COOLED, payload=kwargs)

    async def notify_cooldown_expired(self, **kwargs) -> TransitionResult:
        """Cooldown timer has elapsed."""
        return await self._emit(Event.COOLDOWN_EXPIRED, payload=kwargs)

    async def notify_handshake_started(self, **kwargs) -> TransitionResult:
        """Handshake with VM has begun."""
        return await self._emit(Event.HANDSHAKE_STARTED, payload=kwargs)

    async def notify_handshake_succeeded(self, **kwargs) -> TransitionResult:
        """Handshake completed successfully."""
        return await self._emit(Event.HANDSHAKE_SUCCEEDED, payload=kwargs)

    async def notify_handshake_failed(self, **kwargs) -> TransitionResult:
        """Handshake failed."""
        return await self._emit(Event.HANDSHAKE_FAILED, payload=kwargs)

    async def notify_lease_lost(self, **kwargs) -> TransitionResult:
        """Journal lease was lost to another leader."""
        return await self._emit(Event.LEASE_LOST, payload=kwargs)

    async def notify_boot_deadline_exceeded(self, **kwargs) -> TransitionResult:
        """VM boot deadline has been exceeded."""
        return await self._emit(Event.BOOT_DEADLINE_EXCEEDED, payload=kwargs)

    async def notify_budget_exhausted(self, **kwargs) -> TransitionResult:
        """Runtime budget exhausted — must stop VM."""
        return await self._emit(Event.BUDGET_EXHAUSTED_RUNTIME, payload=kwargs)

    # ── Internal ──────────────────────────────────────────────────────

    async def _emit(
        self,
        event: Event,
        *,
        payload: Optional[Dict[str, Any]] = None,
    ) -> TransitionResult:
        """Emit an event to the state machine, or return a stub when V2 is off."""
        engine = self._get_engine()
        if engine is None:
            return TransitionResult(
                success=False,
                from_state=State.IDLE,
                reason="v2_disabled" if not _V2_ENABLED else "not_initialized",
            )

        try:
            return await engine.handle_event(event, payload=payload)
        except Exception as exc:
            logger.error(
                "[GCPLifecycleBridge] Event %s failed: %s",
                event.value, exc, exc_info=True,
            )
            return TransitionResult(
                success=False,
                from_state=engine.state,
                reason=f"exception: {exc}",
            )


class _NoOpAdapter(SideEffectAdapter):
    """Adapter stub used when no real GCP VM manager is available."""

    async def execute(self, action: str, op_id: str, **kwargs) -> Dict[str, Any]:
        logger.debug("[NoOpAdapter] execute(%s, %s)", action, op_id)
        return {"status": "no_op", "action": action}

    async def query_vm_state(self, op_id: str) -> str:
        return "not_found"
