"""
Root Authority Watcher v1.0.0
==============================
Lifecycle state machine for managed subsystems.
Observes health, detects crashes, emits verdicts.
Does NOT execute verdicts (that's ProcessOrchestrator's job).

ZERO imports from orchestrator or USP.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Protocol, Set, runtime_checkable

from backend.core.root_authority_types import (
    ContractGate,
    ExecutionResult,
    LifecycleAction,
    LifecycleEvent,
    LifecycleVerdict,
    ProcessIdentity,
    RestartPolicy,
    SubsystemState,
    TimeoutPolicy,
    compute_incident_id,
)

logger = logging.getLogger(__name__)


@runtime_checkable
class VerdictExecutor(Protocol):
    """Interface that ProcessOrchestrator must implement.

    The watcher decides WHAT to do. The executor decides HOW.
    """

    async def execute_drain(self, subsystem: str, identity: ProcessIdentity,
                            drain_timeout_s: float) -> ExecutionResult: ...

    async def execute_term(self, subsystem: str, identity: ProcessIdentity,
                           term_timeout_s: float) -> ExecutionResult: ...

    async def execute_group_kill(self, subsystem: str,
                                 identity: ProcessIdentity) -> ExecutionResult: ...

    async def execute_restart(self, subsystem: str,
                              delay_s: float) -> ExecutionResult: ...

    def get_current_identity(self, subsystem: str) -> Optional[ProcessIdentity]: ...


class _SubsystemTracker:
    """Internal per-subsystem state tracking."""

    __slots__ = (
        "name", "identity", "state", "restart_count",
        "restart_timestamps", "consecutive_health_failures",
        "degraded_since_ns", "draining_since_ns", "drain_id",
        "last_health_ns", "_restart_policy",
    )

    def __init__(
        self,
        name: str,
        identity: ProcessIdentity,
        restart_policy: RestartPolicy,
        prior_restart_timestamps: Optional[List[float]] = None,
    ):
        self.name = name
        self.identity = identity
        self.state = SubsystemState.STARTING
        self.restart_count = 0
        self.restart_timestamps: List[float] = list(prior_restart_timestamps or [])
        self.consecutive_health_failures = 0
        self.degraded_since_ns: Optional[int] = None
        self.draining_since_ns: Optional[int] = None
        self.drain_id: Optional[str] = None
        self.last_health_ns: Optional[int] = None
        self._restart_policy = restart_policy

    def can_restart(self) -> bool:
        """Check whether a restart is allowed under the policy window."""
        now = time.monotonic()
        window = self._restart_policy.window_s
        recent = [t for t in self.restart_timestamps if now - t < window]
        self.restart_timestamps = recent
        return len(recent) < self._restart_policy.max_restarts

    def record_restart(self) -> None:
        """Record a restart timestamp for rate-limiting."""
        self.restart_timestamps.append(time.monotonic())
        self.restart_count += 1


class RootAuthorityWatcher:
    """Lifecycle state machine for managed subsystems.

    Observes process health and emits :class:`LifecycleVerdict` objects.
    Does **not** execute verdicts -- that responsibility belongs to the
    verdict executor / process orchestrator layer.

    Thread safety: this class is **not** thread-safe.  All public methods
    must be called from the same asyncio task or protected externally.
    """

    def __init__(
        self,
        session_id: str,
        timeout_policy: TimeoutPolicy,
        restart_policy: RestartPolicy,
        contract_gates: Optional[Dict[str, ContractGate]] = None,
        event_sink: Optional[Callable[[LifecycleEvent], None]] = None,
    ):
        self._session_id = session_id
        self._timeout = timeout_policy
        self._restart_policy = restart_policy
        self._contract_gates = contract_gates or {}
        self._event_sink = event_sink
        self._trackers: Dict[str, _SubsystemTracker] = {}
        self._recent_incidents: Set[str] = set()
        self._incident_timestamps: Dict[str, int] = {}
        self.verdicts_coalesced_total: int = 0
        self.verdicts_dropped_total: int = 0

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register_subsystem(self, name: str, identity: ProcessIdentity) -> None:
        """Register (or re-register) a subsystem with the given identity.

        Re-registration preserves prior restart timestamps so that
        the restart-rate window is enforced across process restarts.
        Incident dedup entries for *name* are cleared so that a fresh
        process incarnation is not suppressed by stale dedup state.
        """
        prior_timestamps: Optional[List[float]] = None
        existing = self._trackers.get(name)
        if existing is not None:
            prior_timestamps = list(existing.restart_timestamps)

        self._trackers[name] = _SubsystemTracker(
            name, identity, self._restart_policy,
            prior_restart_timestamps=prior_timestamps,
        )

        # Clear stale incident dedup entries for this subsystem so a new
        # incarnation is not incorrectly coalesced with the prior one.
        stale_ids = [
            iid for iid in self._recent_incidents
            if iid in self._incident_timestamps
        ]
        # We clear ALL recent incidents for simplicity -- a more targeted
        # approach would key by subsystem, but the 60s bucket already
        # provides scoping and re-registration implies a new incarnation.
        self._recent_incidents.clear()
        self._incident_timestamps.clear()

        self._emit_event(
            event_type="spawn",
            subsystem=name,
            identity=identity,
            to_state=SubsystemState.STARTING,
        )

    # ------------------------------------------------------------------
    # State queries
    # ------------------------------------------------------------------

    def get_state(self, name: str) -> SubsystemState:
        """Return the current lifecycle state for *name*.

        Raises :class:`KeyError` if *name* was never registered.
        """
        tracker = self._trackers.get(name)
        if tracker is None:
            raise KeyError(f"Unknown subsystem: {name}")
        return tracker.state

    def get_identity(self, name: str) -> Optional[ProcessIdentity]:
        """Return the :class:`ProcessIdentity` for *name*, or ``None``."""
        tracker = self._trackers.get(name)
        return tracker.identity if tracker else None

    # ------------------------------------------------------------------
    # Health processing
    # ------------------------------------------------------------------

    def process_health_response(
        self, name: str, data: dict,
    ) -> Optional[LifecycleVerdict]:
        """Process a health-check response from subsystem *name*.

        Returns a :class:`LifecycleVerdict` only if an action is needed
        (e.g. degraded SLO exceeded).  Otherwise returns ``None``.
        """
        tracker = self._trackers.get(name)
        if tracker is None:
            return None

        # Identity validation -- stale/misrouted responses are silently
        # dropped so the state machine is not corrupted.
        if not self._validate_identity(tracker, data):
            return None

        tracker.last_health_ns = time.monotonic_ns()
        tracker.consecutive_health_failures = 0

        liveness = data.get("liveness", "down")
        readiness = data.get("readiness", "not_ready")

        if liveness != "up":
            return None

        old_state = tracker.state

        # --- STARTING -> HANDSHAKE/ALIVE ---
        if tracker.state == SubsystemState.STARTING:
            gate = self._contract_gates.get(name)
            if gate:
                tracker.state = SubsystemState.HANDSHAKE
                if not self._check_handshake(name, gate, data):
                    tracker.state = SubsystemState.REJECTED
                    self._emit_transition(name, old_state, tracker.state, tracker.identity)
                    return self._make_verdict(
                        name, tracker, LifecycleAction.ESCALATE_OPERATOR,
                        "Contract handshake failed", "handshake_failed",
                    )
                tracker.state = SubsystemState.ALIVE
            else:
                tracker.state = SubsystemState.ALIVE

        # --- ALIVE / HANDSHAKE -> READY or DEGRADED ---
        if tracker.state in (SubsystemState.ALIVE, SubsystemState.HANDSHAKE):
            if readiness == "ready":
                tracker.state = SubsystemState.READY
                tracker.degraded_since_ns = None
            elif readiness == "degraded":
                tracker.state = SubsystemState.DEGRADED
                tracker.degraded_since_ns = time.monotonic_ns()

        # --- READY -> DEGRADED or ALIVE ---
        elif tracker.state == SubsystemState.READY:
            if readiness == "degraded":
                tracker.state = SubsystemState.DEGRADED
                tracker.degraded_since_ns = time.monotonic_ns()
            elif readiness == "not_ready":
                tracker.state = SubsystemState.ALIVE

        # --- DEGRADED -> READY, ALIVE, or SLO breach ---
        elif tracker.state == SubsystemState.DEGRADED:
            if readiness == "ready":
                tracker.state = SubsystemState.READY
                tracker.degraded_since_ns = None
            elif readiness == "not_ready":
                tracker.state = SubsystemState.ALIVE
                tracker.degraded_since_ns = None
            else:
                # Still degraded -- check SLO tolerance
                if tracker.degraded_since_ns is not None:
                    elapsed_ns = time.monotonic_ns() - tracker.degraded_since_ns
                    elapsed_s = elapsed_ns / 1e9
                    if elapsed_s >= self._timeout.degraded_tolerance_s:
                        if tracker.state != old_state:
                            self._emit_transition(
                                name, old_state, tracker.state, tracker.identity,
                            )
                        return self._make_verdict(
                            name, tracker, LifecycleAction.DRAIN,
                            f"Degraded for {elapsed_s:.1f}s "
                            f"(limit {self._timeout.degraded_tolerance_s}s)",
                            "degraded_slo_exceeded",
                        )

        if tracker.state != old_state:
            self._emit_transition(name, old_state, tracker.state, tracker.identity)

        return None

    def process_health_failure(self, name: str) -> Optional[LifecycleVerdict]:
        """Record a missed health check for *name*.

        Graduated response:
          1 miss  -> warning log
          2 misses -> DEGRADED
          3 misses -> DRAIN verdict
          5+ misses -> GROUP_KILL verdict
        """
        tracker = self._trackers.get(name)
        if tracker is None:
            return None

        # During startup grace we don't penalise missed health checks.
        if tracker.state == SubsystemState.STARTING:
            return None

        tracker.consecutive_health_failures += 1
        n = tracker.consecutive_health_failures

        if n == 1:
            logger.warning("Health check miss for %s (1 consecutive)", name)
        elif n == 2:
            old = tracker.state
            tracker.state = SubsystemState.DEGRADED
            tracker.degraded_since_ns = time.monotonic_ns()
            self._emit_transition(name, old, tracker.state, tracker.identity)
        elif n == 3:
            return self._make_verdict(
                name, tracker, LifecycleAction.DRAIN,
                f"{n} consecutive health failures", "health_timeout",
            )
        elif n >= 5:
            return self._make_verdict(
                name, tracker, LifecycleAction.GROUP_KILL,
                f"{n} consecutive health failures, drain likely stuck",
                "health_timeout_critical",
            )

        return None

    # ------------------------------------------------------------------
    # Crash processing
    # ------------------------------------------------------------------

    def process_crash(
        self, name: str, exit_code: int,
    ) -> Optional[LifecycleVerdict]:
        """Record that subsystem *name* exited with *exit_code*.

        Returns a :class:`LifecycleVerdict` indicating whether to restart,
        escalate, or do nothing (clean exit returns ``None``).
        """
        tracker = self._trackers.get(name)
        if tracker is None:
            return None

        old_state = tracker.state

        # Clean exit (code 0) -- mark stopped, no restart needed.
        if exit_code == 0:
            tracker.state = SubsystemState.STOPPED
            self._emit_transition(name, old_state, tracker.state, tracker.identity)
            return None

        # Abnormal exit -- mark crashed.
        tracker.state = SubsystemState.CRASHED
        self._emit_transition(name, old_state, tracker.state, tracker.identity)

        # Deduplication: compute a bucket-scoped incident ID and suppress
        # duplicate verdicts for the same incident within the same window.
        now_ns = time.monotonic_ns()
        incident_id = compute_incident_id(
            name, tracker.identity, f"crash_exit_{exit_code}", now_ns,
        )
        if incident_id in self._recent_incidents:
            self.verdicts_coalesced_total += 1
            return None
        self._recent_incidents.add(incident_id)
        self._incident_timestamps[incident_id] = now_ns

        # Check whether the exit code is restartable per policy.
        if not self._restart_policy.should_restart(exit_code):
            return self._make_verdict(
                name, tracker, LifecycleAction.ESCALATE_OPERATOR,
                f"Exit code {exit_code} is non-restartable",
                f"crash_exit_{exit_code}",
                exit_code=exit_code,
                incident_id=incident_id,
            )

        # Check restart budget.
        if not tracker.can_restart():
            return self._make_verdict(
                name, tracker, LifecycleAction.ESCALATE_OPERATOR,
                f"Max restarts exceeded "
                f"({self._restart_policy.max_restarts} in "
                f"{self._restart_policy.window_s}s)",
                "max_restarts_exceeded",
                exit_code=exit_code,
                incident_id=incident_id,
            )

        tracker.record_restart()
        return self._make_verdict(
            name, tracker, LifecycleAction.RESTART,
            f"Crash with exit code {exit_code}",
            f"crash_exit_{exit_code}",
            exit_code=exit_code,
            incident_id=incident_id,
        )

    # ------------------------------------------------------------------
    # Identity validation
    # ------------------------------------------------------------------

    def _validate_identity(
        self, tracker: _SubsystemTracker, data: dict,
    ) -> bool:
        """Verify that *data* matches the registered identity of *tracker*.

        Returns ``False`` (and logs a warning) on any mismatch.
        """
        pid = data.get("pid")
        start_time_ns = data.get("start_time_ns")
        session_id = data.get("session_id")
        fingerprint = data.get("exec_fingerprint")

        if pid is not None and pid != tracker.identity.pid:
            logger.warning(
                "PID mismatch for %s: expected %d, got %d",
                tracker.name, tracker.identity.pid, pid,
            )
            return False
        if session_id and session_id != tracker.identity.session_id:
            logger.warning("Session mismatch for %s", tracker.name)
            return False
        if start_time_ns is not None and start_time_ns != tracker.identity.start_time_ns:
            logger.warning("Start time mismatch for %s", tracker.name)
            return False
        if fingerprint and fingerprint != tracker.identity.exec_fingerprint:
            logger.warning(
                "Exec fingerprint mismatch for %s", tracker.name,
            )
            return False
        return True

    # ------------------------------------------------------------------
    # Contract handshake
    # ------------------------------------------------------------------

    def _check_handshake(
        self, name: str, gate: ContractGate, data: dict,
    ) -> bool:
        """Verify that the health response satisfies the contract gate."""
        schema = data.get("schema_version", "")
        if not gate.is_schema_compatible(schema):
            logger.error(
                "Schema incompatible for %s: expected ~%s, got %s",
                name, gate.expected_schema_version, schema,
            )
            return False

        missing = gate.required_health_fields - set(data.keys())
        if missing:
            logger.error(
                "Missing required health fields for %s: %s", name, missing,
            )
            return False

        return True

    # ------------------------------------------------------------------
    # Verdict construction
    # ------------------------------------------------------------------

    def _make_verdict(
        self,
        name: str,
        tracker: _SubsystemTracker,
        action: LifecycleAction,
        reason: str,
        reason_code: str,
        exit_code: Optional[int] = None,
        incident_id: Optional[str] = None,
    ) -> LifecycleVerdict:
        """Build a :class:`LifecycleVerdict` and emit an event."""
        now_ns = time.monotonic_ns()
        if incident_id is None:
            incident_id = compute_incident_id(
                name, tracker.identity, reason_code, now_ns,
            )

        verdict = LifecycleVerdict(
            subsystem=name,
            identity=tracker.identity,
            action=action,
            reason=reason,
            reason_code=reason_code,
            correlation_id=str(uuid.uuid4()),
            incident_id=incident_id,
            exit_code=exit_code,
            observed_at_ns=now_ns,
            wall_time_utc=datetime.now(timezone.utc).isoformat(),
        )

        self._emit_event(
            event_type="verdict_emitted",
            subsystem=name,
            identity=tracker.identity,
            verdict_action=action,
            reason_code=reason_code,
            exit_code=exit_code,
        )

        return verdict

    # ------------------------------------------------------------------
    # Event emission
    # ------------------------------------------------------------------

    def _emit_transition(
        self,
        name: str,
        old: SubsystemState,
        new: SubsystemState,
        identity: ProcessIdentity,
    ) -> None:
        """Emit a ``state_transition`` event."""
        self._emit_event(
            event_type="state_transition",
            subsystem=name,
            identity=identity,
            from_state=old,
            to_state=new,
        )

    def _emit_event(self, event_type: str, subsystem: str, **kwargs: Any) -> None:
        """Construct and dispatch a :class:`LifecycleEvent` to the sink."""
        if self._event_sink is None:
            return

        identity = kwargs.pop("identity", None)

        event = LifecycleEvent(
            event_type=event_type,
            subsystem=subsystem,
            correlation_id=kwargs.pop("correlation_id", str(uuid.uuid4())),
            session_id=self._session_id,
            identity=identity,
            from_state=kwargs.get("from_state"),
            to_state=kwargs.get("to_state"),
            verdict_action=kwargs.get("verdict_action"),
            reason_code=kwargs.get("reason_code"),
            exit_code=kwargs.get("exit_code"),
            observed_at_ns=time.monotonic_ns(),
            wall_time_utc=datetime.now(timezone.utc).isoformat(),
            policy_source="root_authority",
        )
        self._event_sink(event)
