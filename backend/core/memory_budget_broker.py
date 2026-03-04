"""MemoryBudgetBroker -- single admission authority for memory-intensive operations.

Every model loader (LLM, Whisper, ECAPA, SentenceTransformer) must acquire a
transactional ``BudgetGrant`` from this broker before allocating memory.  The
broker consumes ``MemoryQuantizer.snapshot()`` as its sole signal source and
enforces phase policies, concurrent grant limits, swap hysteresis, and signal
quality gates.

Grant lifecycle::

    request()  -->  BudgetGrant(GRANTED)
                        |
              commit()  |  rollback()
                |               |
        BudgetGrant(ACTIVE)   ROLLED_BACK (terminal)
                |
            release()
                |
        BudgetGrant(RELEASED) (terminal)

All grant operations are idempotent on terminal states and validate epoch
to prevent stale grants from a previous supervisor run.

Public API
----------
Classes:
    MemoryBudgetBroker, BudgetGrant, PhasePolicy

Errors:
    BudgetDeniedError, StaleEpochError, ConstraintViolationError

Singletons:
    get_memory_budget_broker(), init_memory_budget_broker()
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import threading
import time
import uuid
import warnings
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Set

from backend.core.memory_actuator_coordinator import MemoryActuatorCoordinator
from backend.core.memory_types import (
    BudgetPriority,
    ConfigProof,
    DegradationOption,
    LeaseState,
    MemoryBudgetEventType,
    MemorySnapshot,
    PressurePolicy,
    PressureTier,
    SignalQuality,
    StartupPhase,
)

logger = logging.getLogger(__name__)


# ===================================================================
# Ghost display connectivity helper
# ===================================================================


async def _query_ghost_display_connected() -> bool:
    """Check whether the ghost display is still connected via BetterDisplay CLI.

    Returns ``True`` when the phantom hardware manager reports a connected
    display, ``False`` on any error or disconnection.
    """
    try:
        from backend.system.phantom_hardware_manager import get_phantom_manager

        mgr = get_phantom_manager()
        mode = await mgr.get_current_mode_async()
        return mode.get("connected", False)
    except Exception:
        return False


# ===================================================================
# Errors
# ===================================================================


class BudgetDeniedError(Exception):
    """Raised when a memory budget request cannot be satisfied."""

    def __init__(self, reason: str, snapshot_id: Optional[str] = None) -> None:
        self.reason = reason
        self.snapshot_id = snapshot_id
        super().__init__(reason)


class StaleEpochError(Exception):
    """Raised when a grant's epoch does not match the broker's current epoch."""
    pass


class ConstraintViolationError(Exception):
    """Raised when a grant operation violates a constraint."""
    pass


# ===================================================================
# PhasePolicy
# ===================================================================


@dataclasses.dataclass(frozen=True)
class PhasePolicy:
    """Policy governing memory grants during a startup phase.

    Attributes:
        max_concurrent: Maximum number of simultaneous non-terminal grants.
        budget_cap_pct: Fraction of physical_total that may be committed.
        allowed_priorities: Set of ``BudgetPriority`` values permitted in
            this phase.  Requests with a priority outside this set are
            denied immediately.
    """
    max_concurrent: int
    budget_cap_pct: float
    allowed_priorities: FrozenSet[BudgetPriority]


def _load_phase_cap(env_var: str, default: float) -> float:
    """Read a phase cap percentage from env, falling back to *default*."""
    raw = os.environ.get(env_var)
    if raw is None:
        return default
    try:
        val = float(raw)
        if not 0.0 < val <= 1.0:
            logger.warning(
                "env %s=%s out of (0,1] range, using default %.2f",
                env_var, raw, default,
            )
            return default
        return val
    except ValueError:
        logger.warning(
            "env %s=%s is not a valid float, using default %.2f",
            env_var, raw, default,
        )
        return default


def _build_phase_policies() -> Dict[StartupPhase, PhasePolicy]:
    """Build phase policies with caps configurable via env vars."""
    all_priorities = frozenset(BudgetPriority)
    return {
        StartupPhase.BOOT_CRITICAL: PhasePolicy(
            max_concurrent=1,
            budget_cap_pct=_load_phase_cap(
                "JARVIS_MCP_PHASE_CAP_BOOT_CRITICAL", 0.60,
            ),
            allowed_priorities=frozenset({BudgetPriority.BOOT_CRITICAL}),
        ),
        StartupPhase.BOOT_OPTIONAL: PhasePolicy(
            max_concurrent=2,
            budget_cap_pct=_load_phase_cap(
                "JARVIS_MCP_PHASE_CAP_BOOT_OPTIONAL", 0.70,
            ),
            allowed_priorities=frozenset({
                BudgetPriority.BOOT_CRITICAL,
                BudgetPriority.BOOT_OPTIONAL,
            }),
        ),
        StartupPhase.RUNTIME_INTERACTIVE: PhasePolicy(
            max_concurrent=3,
            budget_cap_pct=_load_phase_cap(
                "JARVIS_MCP_PHASE_CAP_RUNTIME_INTERACTIVE", 0.80,
            ),
            allowed_priorities=frozenset({
                BudgetPriority.BOOT_CRITICAL,
                BudgetPriority.BOOT_OPTIONAL,
                BudgetPriority.RUNTIME_INTERACTIVE,
            }),
        ),
        StartupPhase.BACKGROUND: PhasePolicy(
            max_concurrent=2,
            budget_cap_pct=_load_phase_cap(
                "JARVIS_MCP_PHASE_CAP_BACKGROUND", 0.70,
            ),
            allowed_priorities=all_priorities,
        ),
    }


# ===================================================================
# BudgetGrant
# ===================================================================


class BudgetGrant:
    """A transactional memory lease issued by the broker.

    Lifecycle: GRANTED -> ACTIVE (via commit) -> RELEASED (via release)
                     \\-> ROLLED_BACK (via rollback)

    All state-transition methods are idempotent on terminal states and
    validate the broker epoch before mutating.
    """

    def __init__(
        self,
        broker: MemoryBudgetBroker,
        lease_id: str,
        component_id: str,
        granted_bytes: int,
        priority: BudgetPriority,
        phase: StartupPhase,
        epoch: int,
        ttl_seconds: float,
        *,
        degraded: bool = False,
        degradation_applied: Optional[DegradationOption] = None,
        snapshot_id: Optional[str] = None,
        trace_id: Optional[str] = None,
    ) -> None:
        self.broker = broker
        self.lease_id = lease_id
        self.component_id = component_id
        self.granted_bytes = granted_bytes
        self.actual_bytes: Optional[int] = None
        self.priority = priority
        self.phase = phase
        self.epoch = epoch
        self.ttl_seconds = ttl_seconds
        self.state = LeaseState.GRANTED
        self.degraded = degraded
        self.degradation_applied = degradation_applied
        self.config_proof: Optional[ConfigProof] = None
        self.snapshot_id = snapshot_id
        self.trace_id = trace_id

        self._created_at = time.monotonic()
        self._last_heartbeat = self._created_at
        self._committed = False
        self._released = False

    # --- Epoch validation ---

    def _check_epoch(self) -> None:
        """Raise ``StaleEpochError`` if this grant's epoch is stale."""
        if self.epoch != self.broker._epoch:
            raise StaleEpochError(
                f"Grant epoch {self.epoch} != broker epoch {self.broker._epoch}"
            )

    # --- State transitions ---

    async def heartbeat(self) -> None:
        """Extend the TTL of this grant.  Idempotent on terminal states."""
        if self.state.is_terminal:
            return
        self._check_epoch()
        self._last_heartbeat = time.monotonic()
        logger.debug(
            "Heartbeat for lease %s (component=%s)",
            self.lease_id, self.component_id,
        )

    async def commit(
        self,
        actual_bytes: int,
        config_proof: Optional[ConfigProof] = None,
    ) -> None:
        """Transition GRANTED -> ACTIVE.  Idempotent if already ACTIVE."""
        if self.state == LeaseState.ACTIVE:
            return  # idempotent
        if self.state.is_terminal:
            return  # no-op on terminal
        self._check_epoch()
        if self.state != LeaseState.GRANTED:
            raise ConstraintViolationError(
                f"Cannot commit lease in state {self.state.value}"
            )
        self.actual_bytes = actual_bytes
        self.config_proof = config_proof
        self.state = LeaseState.ACTIVE
        self._committed = True
        logger.info(
            "Committed lease %s (component=%s, actual=%d bytes)",
            self.lease_id, self.component_id, actual_bytes,
        )
        self.broker._persist_leases()

    async def rollback(self, reason: str = "") -> None:
        """Transition GRANTED -> ROLLED_BACK.  Idempotent on terminal."""
        if self.state.is_terminal:
            return  # idempotent
        self._check_epoch()
        self.state = LeaseState.ROLLED_BACK
        self.broker._remove_lease(self.lease_id)
        logger.info(
            "Rolled back lease %s (component=%s, reason=%s)",
            self.lease_id, self.component_id, reason,
        )
        self.broker._persist_leases()

    async def release(self) -> None:
        """Transition ACTIVE -> RELEASED.  Idempotent on terminal."""
        if self.state.is_terminal:
            return  # idempotent
        self._check_epoch()
        if self.state != LeaseState.ACTIVE:
            raise ConstraintViolationError(
                f"Cannot release lease in state {self.state.value} "
                f"(must be ACTIVE)"
            )
        self.state = LeaseState.RELEASED
        self._released = True
        self.broker._remove_lease(self.lease_id)
        logger.info(
            "Released lease %s (component=%s)",
            self.lease_id, self.component_id,
        )
        self.broker._persist_leases()

    # --- Context manager ---

    async def __aenter__(self) -> BudgetGrant:
        return self

    async def __aexit__(
        self,
        exc_type: Optional[type],
        exc_val: Optional[BaseException],
        exc_tb: Any,
    ) -> None:
        if self.state.is_terminal:
            return
        # If an exception occurred and we haven't committed, rollback.
        if exc_type is not None:
            await self.rollback(
                reason=f"exception: {exc_type.__name__}: {exc_val}"
            )
            return
        # If we exited normally but never committed or rolled back, warn.
        if self.state == LeaseState.GRANTED:
            warnings.warn(
                f"BudgetGrant {self.lease_id} for {self.component_id} "
                f"was neither committed nor rolled back; auto-rolling back.",
                ResourceWarning,
                stacklevel=2,
            )
            await self.rollback(reason="auto-rollback: context exit without commit")

    # --- Properties ---

    @property
    def effective_bytes(self) -> int:
        """Return actual_bytes if committed, else granted_bytes."""
        if self.actual_bytes is not None:
            return self.actual_bytes
        return self.granted_bytes

    @property
    def is_expired(self) -> bool:
        """Return True if the TTL has elapsed since last heartbeat."""
        return (time.monotonic() - self._last_heartbeat) > self.ttl_seconds

    def __repr__(self) -> str:
        return (
            f"<BudgetGrant lease={self.lease_id} component={self.component_id} "
            f"state={self.state.value} granted={self.granted_bytes} "
            f"actual={self.actual_bytes}>"
        )


# ===================================================================
# MemoryBudgetBroker
# ===================================================================


class MemoryBudgetBroker:
    """Single admission authority for all memory-intensive operations.

    The broker holds a reference to a ``MemoryQuantizer`` for live snapshots
    and an ``epoch`` that fences stale grants from prior supervisor runs.

    Thread-safety: not thread-safe.  Designed for single-event-loop usage.
    All public methods are either sync (for reads) or async (for mutations).
    """

    def __init__(self, quantizer: Any, epoch: int, *, lease_file: Optional[Path] = None) -> None:
        self._quantizer = quantizer
        self._epoch = epoch
        self._phase = StartupPhase.BOOT_CRITICAL
        self._phase_policies = _build_phase_policies()
        self._leases: Dict[str, BudgetGrant] = {}
        self._event_log: List[Dict[str, Any]] = []
        self._lease_file: Path = (
            lease_file if lease_file is not None
            else Path("~/.jarvis/memory/leases.json").expanduser()
        )

        self._pressure_observers: List[Any] = []  # async callables (tier, snapshot)

        self._coordinator = MemoryActuatorCoordinator()
        self._sequence: int = 0
        self._seq_lock = threading.Lock()
        self._policy = PressurePolicy.for_ram_gb(self._detect_total_ram_gb())

        # Wire the quantizer to read committed_bytes from us
        if hasattr(quantizer, "set_broker_ref"):
            quantizer.set_broker_ref(self)

        logger.info(
            "MemoryBudgetBroker initialized (epoch=%d, phase=%s)",
            epoch, self._phase.name,
        )

    # --- Phase management ---

    def set_phase(self, phase: StartupPhase) -> None:
        """Called by supervisor to advance the lifecycle phase."""
        old = self._phase
        self._phase = phase
        logger.info(
            "Phase transition: %s -> %s", old.name, phase.name,
        )
        self._emit_event(
            MemoryBudgetEventType.PHASE_TRANSITION,
            {"old_phase": old.name, "new_phase": phase.name},
        )

    @property
    def current_phase(self) -> StartupPhase:
        return self._phase

    # --- Coordinator / sequence / policy ---

    @property
    def coordinator(self) -> MemoryActuatorCoordinator:
        """The shared actuator coordinator."""
        return self._coordinator

    @property
    def current_epoch(self) -> int:
        """Current supervisor run epoch."""
        return self._epoch

    @property
    def current_sequence(self) -> int:
        """Current monotonic sequence number."""
        return self._sequence

    @property
    def policy(self) -> PressurePolicy:
        """The active pressure policy."""
        return self._policy

    def _advance_sequence(self) -> int:
        """Advance the sequence counter and sync with coordinator."""
        with self._seq_lock:
            self._sequence += 1
            self._coordinator.advance_epoch(self._epoch, self._sequence)
            return self._sequence

    @staticmethod
    def _detect_total_ram_gb() -> float:
        """Detect total system RAM in GB."""
        try:
            import psutil
            return psutil.virtual_memory().total / (1024 ** 3)
        except Exception:
            logger.warning(
                "Could not detect system RAM via psutil; defaulting to 16 GB",
                exc_info=True,
            )
            return 16.0

    # --- Committed bytes ---

    def get_committed_bytes(self) -> int:
        """Sum of effective_bytes for GRANTED and ACTIVE leases."""
        total = 0
        for grant in self._leases.values():
            if grant.state in (LeaseState.GRANTED, LeaseState.ACTIVE):
                total += grant.effective_bytes
        return total

    # --- Internal lease management ---

    def _remove_lease(self, lease_id: str) -> None:
        """Remove a lease from the active set."""
        self._leases.pop(lease_id, None)

    def _count_non_terminal(self) -> int:
        """Count leases that are GRANTED or ACTIVE (non-terminal)."""
        return sum(
            1 for g in self._leases.values()
            if not g.state.is_terminal
        )

    # --- Lease persistence ---

    def _persist_leases(self) -> None:
        """Persist current lease state to disk atomically."""
        data = {
            "schema_version": "1.0",
            "broker_epoch": self._epoch,
            "leases": [
                {
                    "lease_id": g.lease_id,
                    "component_id": g.component_id,
                    "granted_bytes": g.granted_bytes,
                    "actual_bytes": g.actual_bytes,
                    "state": g.state.value,
                    "priority": g.priority.name,
                    "phase": g.phase.name,
                    "epoch": g.epoch,
                    "pid": os.getpid(),
                }
                for g in self._leases.values()
                if not g.state.is_terminal
            ],
        }
        try:
            self._lease_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._lease_file.with_suffix(".tmp")
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(str(tmp), str(self._lease_file))
        except Exception:
            logger.warning("Failed to persist leases", exc_info=True)

    async def reconcile_stale_leases(self) -> Dict[str, Any]:
        """Reconcile stale leases from a prior crash or epoch.

        Reads the lease file, identifies leases from a different epoch
        or with dead PIDs, and reclaims their committed bytes.
        """
        report: Dict[str, Any] = {"stale": 0, "reclaimed_bytes": 0, "corrupted": False}

        if not self._lease_file.exists():
            return report

        try:
            raw = self._lease_file.read_text()
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            logger.warning("Corrupted lease file, resetting")
            report["corrupted"] = True
            self._persist_leases()  # overwrite with clean state
            return report

        if not isinstance(data, dict):
            report["corrupted"] = True
            self._persist_leases()
            return report

        file_epoch = data.get("broker_epoch", 0)
        leases = data.get("leases", [])

        for lease_data in leases:
            component_id = lease_data.get("component_id", "")
            state = lease_data.get("state", "")
            lease_epoch = lease_data.get("epoch", 0)
            pid = lease_data.get("pid", 0)
            actual = lease_data.get("actual_bytes") or lease_data.get("granted_bytes", 0)

            # Special handling for display leases: check live connectivity
            # rather than relying solely on PID / epoch heuristics.
            if component_id.startswith("display:"):
                connected = await _query_ghost_display_connected()
                if connected:
                    # Display is still active -- restore (keep) the lease.
                    logger.info(
                        "Display lease %s (%s) restored -- display still connected",
                        lease_data.get("lease_id", "?"), component_id,
                    )
                    continue
                else:
                    # Display disconnected -- reclaim its memory.
                    report["stale"] += 1
                    report["reclaimed_bytes"] += actual
                    logger.info(
                        "Display lease %s (%s) reclaimed -- display disconnected",
                        lease_data.get("lease_id", "?"), component_id,
                    )
                    continue

            is_stale = False
            if lease_epoch != self._epoch:
                is_stale = True
            elif state in ("granted", "active"):
                # Check if the PID is still alive
                try:
                    os.kill(pid, 0)
                except (OSError, ProcessLookupError):
                    is_stale = True

            if is_stale:
                report["stale"] += 1
                report["reclaimed_bytes"] += actual

        # Overwrite with current clean state
        self._persist_leases()
        return report

    # --- Grant evaluation ---

    async def _evaluate_grant(
        self,
        component: str,
        bytes_requested: int,
        priority: BudgetPriority,
        phase: StartupPhase,
        ttl_seconds: float,
        can_degrade: bool,
        degradation_options: Optional[List[DegradationOption]],
        trace_id: Optional[str],
    ) -> Optional[BudgetGrant]:
        """Core evaluation logic. Returns a BudgetGrant or None."""
        policy = self._phase_policies[self._phase]

        # 1. Check priority is allowed in current phase
        if priority not in policy.allowed_priorities:
            raise BudgetDeniedError(
                f"Priority {priority.name} not allowed in phase "
                f"{self._phase.name} (allowed: "
                f"{', '.join(p.name for p in sorted(policy.allowed_priorities))})",
            )

        # 2. Check concurrent grant count vs phase limit
        if self._count_non_terminal() >= policy.max_concurrent:
            raise BudgetDeniedError(
                f"Concurrent grant limit reached: {policy.max_concurrent} "
                f"active in phase {self._phase.name}",
            )

        # 5. Take fresh snapshot from quantizer
        snapshot = await self._quantizer.snapshot()

        # 3. Check swap hysteresis (block BACKGROUND if active)
        if (
            priority == BudgetPriority.BACKGROUND
            and snapshot.swap_hysteresis_active
        ):
            self._emit_event(
                MemoryBudgetEventType.SWAP_HYSTERESIS_TRIP,
                {"component": component, "swap_growth_bps": snapshot.swap_growth_rate_bps},
            )
            raise BudgetDeniedError(
                f"Swap hysteresis active (growth rate "
                f"{snapshot.swap_growth_rate_bps:.0f} B/s); "
                f"BACKGROUND grants blocked",
                snapshot_id=snapshot.snapshot_id,
            )

        # 4. Check signal quality (block non-critical if FALLBACK)
        if (
            snapshot.signal_quality == SignalQuality.FALLBACK
            and priority != BudgetPriority.BOOT_CRITICAL
        ):
            raise BudgetDeniedError(
                f"Signal quality is FALLBACK; only BOOT_CRITICAL "
                f"grants allowed",
                snapshot_id=snapshot.snapshot_id,
            )

        # 6. Calculate effective headroom
        budget_cap_bytes = int(snapshot.physical_total * policy.budget_cap_pct)
        committed = self.get_committed_bytes()
        cap_headroom = max(0, budget_cap_bytes - committed)
        effective_headroom = min(snapshot.headroom_bytes, cap_headroom)

        # 7. If bytes_requested <= headroom: issue grant
        if bytes_requested <= effective_headroom:
            return self._issue_grant(
                component=component,
                granted_bytes=bytes_requested,
                priority=priority,
                phase=phase,
                ttl_seconds=ttl_seconds,
                degraded=False,
                degradation_applied=None,
                snapshot_id=snapshot.snapshot_id,
                trace_id=trace_id,
            )

        # 8. If can_degrade: try degradation_options in order
        if can_degrade and degradation_options:
            for option in degradation_options:
                if option.bytes_required <= effective_headroom:
                    grant = self._issue_grant(
                        component=component,
                        granted_bytes=option.bytes_required,
                        priority=priority,
                        phase=phase,
                        ttl_seconds=ttl_seconds,
                        degraded=True,
                        degradation_applied=option,
                        snapshot_id=snapshot.snapshot_id,
                        trace_id=trace_id,
                    )
                    self._emit_event(
                        MemoryBudgetEventType.GRANT_DEGRADED,
                        {
                            "component": component,
                            "original_bytes": bytes_requested,
                            "degraded_bytes": option.bytes_required,
                            "option_name": option.name,
                        },
                    )
                    return grant

        # 10. Insufficient headroom
        return None

    def _issue_grant(
        self,
        component: str,
        granted_bytes: int,
        priority: BudgetPriority,
        phase: StartupPhase,
        ttl_seconds: float,
        degraded: bool,
        degradation_applied: Optional[DegradationOption],
        snapshot_id: Optional[str],
        trace_id: Optional[str],
    ) -> BudgetGrant:
        """Create and register a new BudgetGrant."""
        lease_id = f"lease_{uuid.uuid4().hex[:12]}"
        grant = BudgetGrant(
            broker=self,
            lease_id=lease_id,
            component_id=component,
            granted_bytes=granted_bytes,
            priority=priority,
            phase=phase,
            epoch=self._epoch,
            ttl_seconds=ttl_seconds,
            degraded=degraded,
            degradation_applied=degradation_applied,
            snapshot_id=snapshot_id,
            trace_id=trace_id,
        )
        self._leases[lease_id] = grant
        self._emit_event(
            MemoryBudgetEventType.GRANT_ISSUED,
            {
                "lease_id": lease_id,
                "component": component,
                "granted_bytes": granted_bytes,
                "priority": priority.name,
                "degraded": degraded,
            },
        )
        logger.info(
            "Grant issued: lease=%s component=%s bytes=%d priority=%s degraded=%s",
            lease_id, component, granted_bytes, priority.name, degraded,
        )
        self._persist_leases()
        return grant

    # --- Public API ---

    async def request(
        self,
        component: str,
        bytes_requested: int,
        priority: BudgetPriority,
        phase: StartupPhase,
        *,
        ttl_seconds: float = 120.0,
        can_degrade: bool = False,
        degradation_options: Optional[List[DegradationOption]] = None,
        deadline: Optional[float] = None,
        trace_id: Optional[str] = None,
    ) -> BudgetGrant:
        """Block until grant is issued or deadline expires.

        Raises ``BudgetDeniedError`` if the request cannot be satisfied.
        """
        self._emit_event(
            MemoryBudgetEventType.GRANT_REQUESTED,
            {
                "component": component,
                "bytes_requested": bytes_requested,
                "priority": priority.name,
                "phase": phase.name,
            },
        )

        grant = await self._evaluate_grant(
            component=component,
            bytes_requested=bytes_requested,
            priority=priority,
            phase=phase,
            ttl_seconds=ttl_seconds,
            can_degrade=can_degrade,
            degradation_options=degradation_options,
            trace_id=trace_id,
        )
        if grant is not None:
            return grant

        # Grant evaluation returned None -- insufficient headroom
        self._emit_event(
            MemoryBudgetEventType.GRANT_DENIED,
            {
                "component": component,
                "bytes_requested": bytes_requested,
                "reason": "insufficient headroom",
            },
        )
        raise BudgetDeniedError(
            f"Insufficient headroom for {component}: "
            f"requested {bytes_requested} bytes",
        )

    async def try_request(
        self,
        component: str,
        bytes_requested: int,
        priority: BudgetPriority,
        phase: StartupPhase,
        *,
        ttl_seconds: float = 120.0,
        can_degrade: bool = False,
        degradation_options: Optional[List[DegradationOption]] = None,
        trace_id: Optional[str] = None,
    ) -> Optional[BudgetGrant]:
        """Non-blocking: returns grant or None."""
        try:
            return await self.request(
                component=component,
                bytes_requested=bytes_requested,
                priority=priority,
                phase=phase,
                ttl_seconds=ttl_seconds,
                can_degrade=can_degrade,
                degradation_options=degradation_options,
                trace_id=trace_id,
            )
        except BudgetDeniedError:
            return None

    # --- Inspection ---

    def get_active_leases(self) -> List[BudgetGrant]:
        """Return all non-terminal leases."""
        return [
            g for g in self._leases.values()
            if not g.state.is_terminal
        ]

    def get_status(self) -> Dict[str, Any]:
        """Return a status dict for observability."""
        committed = self.get_committed_bytes()
        policy = self._phase_policies[self._phase]
        return {
            "epoch": self._epoch,
            "phase": self._phase.name,
            "committed_bytes": committed,
            "active_leases": len(self.get_active_leases()),
            "max_concurrent": policy.max_concurrent,
            "budget_cap_pct": policy.budget_cap_pct,
            "leases": [
                {
                    "lease_id": g.lease_id,
                    "component": g.component_id,
                    "state": g.state.value,
                    "granted_bytes": g.granted_bytes,
                    "actual_bytes": g.actual_bytes,
                    "priority": g.priority.name,
                }
                for g in self._leases.values()
            ],
        }

    # --- Pressure observer pattern ---

    def register_pressure_observer(self, callback: Any) -> None:
        """Register an async callback for pressure tier changes.

        Callback signature: async def callback(tier: PressureTier, snapshot: MemorySnapshot)
        """
        if callback not in self._pressure_observers:
            self._pressure_observers.append(callback)

    def unregister_pressure_observer(self, callback: Any) -> None:
        """Remove a previously registered pressure observer."""
        try:
            self._pressure_observers.remove(callback)
        except ValueError:
            pass

    async def notify_pressure_observers(
        self, tier: "PressureTier", snapshot: Any,
    ) -> None:
        """Notify all registered observers of a pressure tier change.

        Observer exceptions are caught and logged -- one bad observer
        must never block others.
        """
        self._advance_sequence()
        for obs in self._pressure_observers:
            try:
                await obs(tier, snapshot)
            except Exception:
                logger.warning(
                    "Pressure observer %s raised exception", obs, exc_info=True,
                )

    # --- Lease amendment ---

    async def amend_lease_bytes(
        self, lease_id: str, new_bytes: int,
    ) -> None:
        """Atomically swap the granted_bytes of an active lease.

        Used for display resolution changes -- the lease stays ACTIVE,
        only the byte reservation changes.  No temporary release window.

        Raises KeyError if lease not found.
        Raises ValueError if the lease is in a terminal state.
        """
        grant = self._leases.get(lease_id)
        if grant is None:
            raise KeyError(f"Unknown lease: {lease_id}")
        if grant.state.is_terminal:
            raise ValueError(f"Cannot amend lease in terminal state: {grant.state.value}")
        old_bytes = grant.granted_bytes
        grant.granted_bytes = new_bytes
        grant.actual_bytes = new_bytes
        self._emit_event(MemoryBudgetEventType.GRANT_DEGRADED, {
            "lease_id": lease_id,
            "component": grant.component_id,
            "old_bytes": old_bytes,
            "new_bytes": new_bytes,
        })
        self._persist_leases()

    # --- Event emission ---

    def _emit_event(
        self, event_type: MemoryBudgetEventType, data: Dict[str, Any],
    ) -> None:
        """Emit a structured event for observability."""
        event = {
            "type": event_type.value,
            "timestamp": time.time(),
            "epoch": self._epoch,
            "phase": self._phase.name,
            **data,
        }
        self._event_log.append(event)
        logger.debug("Event: %s", event)


# ===================================================================
# Singleton
# ===================================================================

_broker_instance: Optional[MemoryBudgetBroker] = None


def get_memory_budget_broker() -> Optional[MemoryBudgetBroker]:
    """Return the current broker instance, or None if not initialized."""
    return _broker_instance


async def init_memory_budget_broker(
    quantizer: Any, epoch: int, *, lease_file: Optional[Path] = None,
) -> MemoryBudgetBroker:
    """Initialize the global MemoryBudgetBroker singleton.

    Returns the newly created broker instance.
    """
    global _broker_instance
    _broker_instance = MemoryBudgetBroker(quantizer, epoch, lease_file=lease_file)
    logger.info("Global MemoryBudgetBroker initialized (epoch=%d)", epoch)
    return _broker_instance
