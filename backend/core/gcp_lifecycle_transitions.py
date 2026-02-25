# backend/core/gcp_lifecycle_transitions.py
"""Deterministic transition table for GCP lifecycle state machine.

Every valid (state, event) pair maps to a TransitionEntry with:
  - next_state: the state to transition to
  - journal_actions: list of action strings to journal
  - has_side_effect: whether this transition triggers external GCP operations

Wildcard transitions (None, event) match any state not covered by an exact match.

Design doc: docs/plans/2026-02-25-journal-backed-gcp-lifecycle-design.md
Section 2: Detailed Transition Matrix.
"""
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional, Set, Tuple

from backend.core.gcp_lifecycle_schema import State, Event


@dataclass(frozen=True)
class TransitionEntry:
    from_state: Optional[State]  # None = wildcard (matches any state)
    event: Event
    next_state: State
    journal_actions: Tuple[str, ...] = ()
    has_side_effect: bool = False


def _t(
    from_state: Optional[State],
    event: Event,
    next_state: State,
    journal_actions: Tuple[str, ...] = (),
    has_side_effect: bool = False,
) -> TransitionEntry:
    return TransitionEntry(
        from_state=from_state,
        event=event,
        next_state=next_state,
        journal_actions=journal_actions,
        has_side_effect=has_side_effect,
    )


# ── Explicit transitions ────────────────────────────────────────────────

_EXPLICIT: List[TransitionEntry] = [
    # IDLE
    _t(State.IDLE, Event.PRESSURE_TRIGGERED, State.TRIGGERING,
       ("budget_check_requested",)),
    _t(State.IDLE, Event.RECONCILE_OBSERVED_RUNNING, State.ACTIVE,
       ("reconcile_adopt",)),

    # TRIGGERING
    _t(State.TRIGGERING, Event.BUDGET_APPROVED, State.PROVISIONING,
       ("budget_reserved", "provision_requested"), has_side_effect=True),
    _t(State.TRIGGERING, Event.BUDGET_DENIED, State.COOLING_DOWN,
       ("budget_denied_logged",)),

    # PROVISIONING
    _t(State.PROVISIONING, Event.VM_CREATE_ACCEPTED, State.BOOTING,
       ("vm_create_accepted",)),
    _t(State.PROVISIONING, Event.VM_CREATE_ALREADY_EXISTS, State.BOOTING,
       ("vm_adopted",)),
    _t(State.PROVISIONING, Event.VM_CREATE_FAILED, State.COOLING_DOWN,
       ("vm_create_failed", "budget_released"), has_side_effect=True),

    # BOOTING
    _t(State.BOOTING, Event.HEALTH_PROBE_OK, State.ACTIVE,
       ("health_confirmed", "routing_switched_to_cloud"), has_side_effect=True),
    _t(State.BOOTING, Event.HANDSHAKE_STARTED, State.HANDSHAKING,
       ("handshake_started",)),
    _t(State.BOOTING, Event.BOOT_DEADLINE_EXCEEDED, State.COOLING_DOWN,
       ("boot_timeout", "budget_released"), has_side_effect=True),

    # HANDSHAKING
    _t(State.HANDSHAKING, Event.HANDSHAKE_SUCCEEDED, State.ACTIVE,
       ("handshake_succeeded", "routing_switched_to_cloud"), has_side_effect=True),
    _t(State.HANDSHAKING, Event.HANDSHAKE_FAILED, State.COOLING_DOWN,
       ("handshake_failed", "budget_released"), has_side_effect=True),
    _t(State.HANDSHAKING, Event.HEALTH_PROBE_OK, State.ACTIVE,
       ("health_confirmed", "routing_switched_to_cloud"), has_side_effect=True),

    # ACTIVE
    _t(State.ACTIVE, Event.PRESSURE_COOLED, State.COOLING_DOWN,
       ("cooldown_started",)),
    _t(State.ACTIVE, Event.SPOT_PREEMPTED, State.TRIGGERING,
       ("preemption_detected", "budget_released", "routing_switched_to_local"),
       has_side_effect=True),
    _t(State.ACTIVE, Event.HEALTH_UNREACHABLE_CONSECUTIVE, State.TRIGGERING,
       ("unreachable_detected", "budget_released", "routing_switched_to_local"),
       has_side_effect=True),
    _t(State.ACTIVE, Event.HEALTH_DEGRADED_CONSECUTIVE, State.DEGRADED,
       ("degraded_detected",)),
    _t(State.ACTIVE, Event.BUDGET_EXHAUSTED_RUNTIME, State.STOPPING,
       ("budget_exhausted", "vm_stop_requested"), has_side_effect=True),
    _t(State.ACTIVE, Event.MANUAL_FORCE_LOCAL, State.STOPPING,
       ("manual_force_local", "vm_stop_requested"), has_side_effect=True),

    # DEGRADED
    _t(State.DEGRADED, Event.HEALTH_PROBE_OK, State.ACTIVE,
       ("health_recovered",)),
    _t(State.DEGRADED, Event.HEALTH_UNREACHABLE_CONSECUTIVE, State.TRIGGERING,
       ("unreachable_from_degraded", "budget_released", "routing_switched_to_local"),
       has_side_effect=True),
    _t(State.DEGRADED, Event.PRESSURE_COOLED, State.COOLING_DOWN,
       ("cooldown_started",)),

    # COOLING_DOWN
    _t(State.COOLING_DOWN, Event.PRESSURE_TRIGGERED, State.TRIGGERING,
       ("retrigger_from_cooldown",)),
    _t(State.COOLING_DOWN, Event.RETRIGGER_DURING_COOLDOWN, State.TRIGGERING,
       ("retrigger_from_cooldown",)),
    _t(State.COOLING_DOWN, Event.COOLDOWN_EXPIRED, State.STOPPING,
       ("cooldown_expired", "vm_stop_requested"), has_side_effect=True),

    # STOPPING
    _t(State.STOPPING, Event.VM_STOPPED, State.IDLE,
       ("vm_stopped", "budget_committed")),
    _t(State.STOPPING, Event.VM_STOP_TIMEOUT, State.IDLE,
       ("vm_stop_timeout", "budget_committed")),

    # LOST
    _t(State.LOST, Event.RECONCILE_OBSERVED_RUNNING, State.ACTIVE,
       ("reconcile_readopt",)),
    _t(State.LOST, Event.RECONCILE_OBSERVED_STOPPED, State.IDLE,
       ("reconcile_confirmed_stopped", "budget_committed")),

    # FAILED
    _t(State.FAILED, Event.PRESSURE_TRIGGERED, State.TRIGGERING,
       ("retry_from_failed",)),
    _t(State.FAILED, Event.RECONCILE_OBSERVED_STOPPED, State.IDLE,
       ("reconcile_confirmed_stopped", "budget_committed")),
]


# ── Wildcard transitions (match any state) ──────────────────────────────

_WILDCARD: List[TransitionEntry] = [
    _t(None, Event.SESSION_SHUTDOWN, State.STOPPING,
       ("session_shutdown_requested", "vm_stop_requested"), has_side_effect=True),
    _t(None, Event.LEASE_LOST, State.IDLE,
       ("lease_lost_halt",)),
    _t(None, Event.FATAL_ERROR, State.COOLING_DOWN,
       ("fatal_error_logged",)),
]


# ── Build lookup dict ───────────────────────────────────────────────────

TRANSITION_TABLE: Dict[Tuple[Optional[State], Event], TransitionEntry] = {}
for _entry in _EXPLICIT:
    TRANSITION_TABLE[(_entry.from_state, _entry.event)] = _entry
for _entry in _WILDCARD:
    TRANSITION_TABLE[(None, _entry.event)] = _entry


def get_transition(state: State, event: Event) -> Optional[TransitionEntry]:
    """Look up a transition: exact match first, then wildcard."""
    exact = TRANSITION_TABLE.get((state, event))
    if exact is not None:
        return exact
    return TRANSITION_TABLE.get((None, event))


# ── Illegal transitions ─────────────────────────────────────────────────

ILLEGAL_TRANSITIONS: FrozenSet[Tuple[State, State]] = frozenset([
    (State.IDLE, State.ACTIVE),           # Cannot skip provisioning
    (State.PROVISIONING, State.ACTIVE),   # Cannot skip booting
    (State.ACTIVE, State.IDLE),           # Must go through STOPPING
    (State.BOOTING, State.PROVISIONING),  # Cannot reverse
])


def is_valid_transition(from_state: State, to_state: State) -> bool:
    """Check whether a from_state -> to_state pair is allowed."""
    if (from_state, to_state) in ILLEGAL_TRANSITIONS:
        return False
    # Check if any transition entry produces this from->to pair
    for key, entry in TRANSITION_TABLE.items():
        if entry.from_state == from_state and entry.next_state == to_state:
            return True
        if entry.from_state is None and entry.next_state == to_state:
            return True
    return False
