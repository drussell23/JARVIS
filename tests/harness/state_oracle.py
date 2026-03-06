"""StateOracle protocol and MockStateOracle in-memory implementation.

The StateOracle is the single source of truth for the test harness.
It owns the monotonic event sequence counter, stores component statuses,
routing decisions, contract statuses, and an append-only event log.

MockStateOracle provides a fully in-memory implementation suitable for
unit tests and mock-mode integration tests.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from typing import Any, Callable, Dict, List, Optional

from typing_extensions import Protocol, runtime_checkable

from tests.harness.types import (
    ComponentStatus,
    ContractReasonCode,
    ContractStatus,
    ObservedEvent,
    OracleObservation,
)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class OracleTimeoutError(Exception):
    """Raised by wait_until when the deadline is exceeded."""


class OracleDivergenceError(Exception):
    """Raised when oracle sources disagree in strict mode."""


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class StateOracleProtocol(Protocol):
    """Read interface for the test harness state oracle."""

    def component_status(self, name: str) -> OracleObservation:
        ...

    def routing_decision(self) -> OracleObservation:
        ...

    def epoch(self) -> int:
        ...

    def contract_status(self, contract_name: str) -> ContractStatus:
        ...

    def store_revision(self, store_name: str) -> int:
        ...

    def event_log(self, since_phase: Optional[str] = None) -> List[ObservedEvent]:
        ...

    def current_seq(self) -> int:
        ...

    def current_phase(self) -> str:
        ...

    async def wait_until(
        self,
        predicate: Callable[[], bool],
        deadline: float,
        description: str,
    ) -> None:
        ...

    def emit_event(
        self,
        *,
        source: str,
        event_type: str,
        component: Optional[str],
        old_value: Optional[str],
        new_value: str,
        trace_root_id: str,
        trace_id: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> int:
        ...

    def fence_phase(self, phase: str, boundary_seq: int) -> None:
        ...


# ---------------------------------------------------------------------------
# MockStateOracle -- in-memory implementation
# ---------------------------------------------------------------------------

class MockStateOracle:
    """In-memory StateOracle for unit and mock-mode integration tests.

    Owns the monotonic oracle_event_seq counter via ``itertools.count(1)``.
    All state mutations emit events and wake any ``wait_until`` waiters.
    """

    def __init__(self) -> None:
        self._seq_counter = itertools.count(1)
        self._last_seq: int = 0

        # State stores
        self._component_statuses: Dict[str, ComponentStatus] = {}
        self._routing: Optional[str] = None
        self._epoch: int = 0
        self._contracts: Dict[str, ContractStatus] = {}
        self._store_revisions: Dict[str, int] = {}

        # Event log (append-only)
        self._events: List[ObservedEvent] = []

        # Phase tracking
        self._phase: str = "setup"
        self._phase_boundaries: Dict[str, int] = {}

        # State-change notification for wait_until
        self._state_changed: asyncio.Event = asyncio.Event()

        # Callbacks
        self._on_change_callbacks: List[Callable[[], None]] = []

    # -------------------------------------------------------------------
    # Read interface
    # -------------------------------------------------------------------

    def component_status(self, name: str) -> OracleObservation:
        status = self._component_statuses.get(name, ComponentStatus.UNKNOWN)
        return OracleObservation(
            value=status,
            observed_at_mono=time.monotonic(),
            observation_quality="fresh",
            source="mock_oracle",
        )

    def routing_decision(self) -> OracleObservation:
        return OracleObservation(
            value=self._routing,
            observed_at_mono=time.monotonic(),
            observation_quality="fresh",
            source="mock_oracle",
        )

    def epoch(self) -> int:
        return self._epoch

    def contract_status(self, contract_name: str) -> ContractStatus:
        return self._contracts.get(
            contract_name,
            ContractStatus(
                compatible=False,
                reason_code=ContractReasonCode.HANDSHAKE_MISSING,
                detail="No contract registered",
            ),
        )

    def store_revision(self, store_name: str) -> int:
        return self._store_revisions.get(store_name, 0)

    def event_log(self, since_phase: Optional[str] = None) -> List[ObservedEvent]:
        if since_phase is None:
            return list(self._events)
        boundary_seq = self._phase_boundaries.get(since_phase)
        if boundary_seq is None:
            return list(self._events)
        return [ev for ev in self._events if ev.oracle_event_seq > boundary_seq]

    def current_seq(self) -> int:
        return self._last_seq

    def current_phase(self) -> str:
        return self._phase

    # -------------------------------------------------------------------
    # Mutators
    # -------------------------------------------------------------------

    def set_phase(self, phase: str) -> None:
        self._phase = phase

    def set_component_status(self, name: str, status: ComponentStatus) -> None:
        old_status = self._component_statuses.get(name, ComponentStatus.UNKNOWN)
        self._component_statuses[name] = status
        self._emit(
            source="mock_oracle",
            event_type="state_change",
            component=name,
            old_value=old_status.value,
            new_value=status.value,
            trace_root_id="",
            trace_id="",
        )
        self._notify_state_changed()

    def set_routing_decision(self, decision: str) -> None:
        old = self._routing
        self._routing = decision
        self._emit(
            source="mock_oracle",
            event_type="routing_change",
            component=None,
            old_value=old,
            new_value=decision,
            trace_root_id="",
            trace_id="",
        )
        self._notify_state_changed()

    def set_epoch(self, epoch: int) -> None:
        self._epoch = epoch

    def set_contract_status(self, contract_name: str, status: ContractStatus) -> None:
        self._contracts[contract_name] = status

    def set_store_revision(self, store_name: str, revision: int) -> None:
        self._store_revisions[store_name] = revision

    def on_state_change(self, callback: Callable[[], None]) -> None:
        """Register a callback invoked on any state mutation."""
        self._on_change_callbacks.append(callback)

    def fence_phase(self, phase: str, boundary_seq: int) -> None:
        """Record a phase boundary. Events with seq <= boundary_seq are excluded
        when filtering by this phase via ``event_log(since_phase=phase)``."""
        self._phase = phase
        self._phase_boundaries[phase] = boundary_seq

    # -------------------------------------------------------------------
    # Event emission
    # -------------------------------------------------------------------

    def emit_event(
        self,
        *,
        source: str,
        event_type: str,
        component: Optional[str],
        old_value: Optional[str],
        new_value: str,
        trace_root_id: str,
        trace_id: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> int:
        """External callers use this to emit events. The oracle assigns the seq."""
        return self._emit(
            source=source,
            event_type=event_type,
            component=component,
            old_value=old_value,
            new_value=new_value,
            trace_root_id=trace_root_id,
            trace_id=trace_id,
            metadata=metadata,
        )

    def _emit(
        self,
        *,
        source: str,
        event_type: str,
        component: Optional[str],
        old_value: Optional[str],
        new_value: str,
        trace_root_id: str,
        trace_id: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> int:
        """Internal event creation. Assigns a monotonic sequence number."""
        seq = next(self._seq_counter)
        self._last_seq = seq
        event = ObservedEvent(
            oracle_event_seq=seq,
            timestamp_mono=time.monotonic(),
            source=source,
            event_type=event_type,
            component=component,
            old_value=old_value,
            new_value=new_value,
            epoch=self._epoch,
            scenario_phase=self._phase,
            trace_root_id=trace_root_id,
            trace_id=trace_id,
            metadata=metadata if metadata is not None else {},
        )
        self._events.append(event)
        return seq

    # -------------------------------------------------------------------
    # Async wait
    # -------------------------------------------------------------------

    async def wait_until(
        self,
        predicate: Callable[[], bool],
        deadline: float,
        description: str,
    ) -> None:
        """Poll predicate, waking on state changes. Raises OracleTimeoutError
        if the deadline (in seconds) is exceeded."""
        if predicate():
            return

        deadline_mono = time.monotonic() + deadline

        while True:
            remaining = deadline_mono - time.monotonic()
            if remaining <= 0:
                raise OracleTimeoutError(
                    f"Timed out after {deadline}s waiting for: {description}"
                )

            # Clear and wait for next state change or timeout
            self._state_changed.clear()
            try:
                await asyncio.wait_for(self._state_changed.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                # Final check before raising
                if predicate():
                    return
                raise OracleTimeoutError(
                    f"Timed out after {deadline}s waiting for: {description}"
                )

            if predicate():
                return

    # -------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------

    def _notify_state_changed(self) -> None:
        """Wake any wait_until waiters and invoke on_change callbacks."""
        self._state_changed.set()
        for cb in self._on_change_callbacks:
            cb()
