"""
Real-time vision pipeline state machine.

11 states, deterministic transition table, enforced legal transitions.
Every transition emits a telemetry record via the on_transition callback.

Design decisions
----------------
* PRECHECK is the *only* path to ACTING — no bypass.
* STOP is a universal escape hatch from every non-IDLE state.
* Retry count increments only when entering ACTION_TARGETING from
  RETRY_TARGETING; it resets to 0 on any successful WATCHING entry.
* The telemetry record always contains:
    from_state, to_state, event, timestamp (float, time.monotonic()),
    retry_count
"""
from __future__ import annotations

import time
from enum import Enum
from typing import Callable, Dict, Optional, Tuple


# ---------------------------------------------------------------------------
# State and Event enumerations
# ---------------------------------------------------------------------------

class VisionState(str, Enum):
    IDLE             = "IDLE"
    WATCHING         = "WATCHING"
    CHANGE_DETECTED  = "CHANGE_DETECTED"
    ANALYZING        = "ANALYZING"
    ACTION_TARGETING = "ACTION_TARGETING"
    PRECHECK         = "PRECHECK"
    ACTING           = "ACTING"
    VERIFYING        = "VERIFYING"
    RETRY_TARGETING  = "RETRY_TARGETING"
    DEGRADED         = "DEGRADED"
    RECOVERING       = "RECOVERING"
    FAILED           = "FAILED"


class VisionEvent(str, Enum):
    # --- Lifecycle ---
    START                  = "START"
    STOP                   = "STOP"

    # --- Frame capture / motion ---
    MOTION_DETECTED        = "MOTION_DETECTED"
    NO_CHANGE              = "NO_CHANGE"
    SAMPLE_FRAME           = "SAMPLE_FRAME"
    NO_FRAME_TIMEOUT       = "NO_FRAME_TIMEOUT"

    # --- Analysis ---
    ANALYSIS_COMPLETE      = "ANALYSIS_COMPLETE"
    ACTION_REQUESTED       = "ACTION_REQUESTED"
    VISION_UNAVAILABLE     = "VISION_UNAVAILABLE"

    # --- Targeting / burst ---
    BURST_COMPLETE         = "BURST_COMPLETE"
    ESCALATION_EXHAUSTED   = "ESCALATION_EXHAUSTED"

    # --- PRECHECK guards ---
    ALL_GUARDS_PASS        = "ALL_GUARDS_PASS"
    FRESHNESS_FAIL         = "FRESHNESS_FAIL"
    CONFIDENCE_FAIL        = "CONFIDENCE_FAIL"
    RISK_REQUIRES_APPROVAL = "RISK_REQUIRES_APPROVAL"
    APPROVAL_DENIED        = "APPROVAL_DENIED"
    IDEMPOTENCY_HIT        = "IDEMPOTENCY_HIT"
    INTENT_EXPIRED         = "INTENT_EXPIRED"

    # --- Action dispatch ---
    ACTION_DISPATCHED      = "ACTION_DISPATCHED"

    # --- Verification ---
    POSTCONDITION_MET      = "POSTCONDITION_MET"
    POSTCONDITION_FAIL     = "POSTCONDITION_FAIL"

    # --- Retry ---
    RETRY                  = "RETRY"
    RETRY_EXCEEDED         = "RETRY_EXCEEDED"

    # --- Health / degraded ---
    HEALTH_CHECK_PASS      = "HEALTH_CHECK_PASS"
    HEALTH_CHECK_FAIL      = "HEALTH_CHECK_FAIL"
    N_CONSECUTIVE_HEALTHY  = "N_CONSECUTIVE_HEALTHY"

    # --- Human override ---
    USER_ACKNOWLEDGES      = "USER_ACKNOWLEDGES"


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------

class TransitionError(Exception):
    """Raised when an event is illegal for the current state."""

    def __init__(self, state: VisionState, event: VisionEvent) -> None:
        super().__init__(
            f"No legal transition from state={state.value!r} "
            f"on event={event.value!r}"
        )
        self.state = state
        self.event = event


# ---------------------------------------------------------------------------
# Transition table
# ---------------------------------------------------------------------------
#
# Key:   (from_state, event)
# Value: to_state
#
# STOP from every non-IDLE state is added programmatically at module load
# time so it never needs to be maintained by hand.

_TRANSITIONS: Dict[Tuple[VisionState, VisionEvent], VisionState] = {
    # ---- IDLE ----
    (VisionState.IDLE,             VisionEvent.START):               VisionState.WATCHING,

    # ---- WATCHING ----
    (VisionState.WATCHING,         VisionEvent.MOTION_DETECTED):     VisionState.CHANGE_DETECTED,
    (VisionState.WATCHING,         VisionEvent.NO_CHANGE):           VisionState.WATCHING,   # self-loop

    # ---- CHANGE_DETECTED ----
    (VisionState.CHANGE_DETECTED,  VisionEvent.SAMPLE_FRAME):        VisionState.ANALYZING,
    (VisionState.CHANGE_DETECTED,  VisionEvent.NO_FRAME_TIMEOUT):    VisionState.WATCHING,

    # ---- ANALYZING ----
    (VisionState.ANALYZING,        VisionEvent.ANALYSIS_COMPLETE):   VisionState.WATCHING,
    (VisionState.ANALYZING,        VisionEvent.ACTION_REQUESTED):    VisionState.ACTION_TARGETING,
    (VisionState.ANALYZING,        VisionEvent.VISION_UNAVAILABLE):  VisionState.DEGRADED,

    # ---- ACTION_TARGETING ----
    (VisionState.ACTION_TARGETING, VisionEvent.BURST_COMPLETE):      VisionState.PRECHECK,
    (VisionState.ACTION_TARGETING, VisionEvent.ESCALATION_EXHAUSTED):VisionState.FAILED,
    (VisionState.ACTION_TARGETING, VisionEvent.VISION_UNAVAILABLE):  VisionState.DEGRADED,

    # ---- PRECHECK (the only path to ACTING) ----
    (VisionState.PRECHECK,         VisionEvent.ALL_GUARDS_PASS):     VisionState.ACTING,
    (VisionState.PRECHECK,         VisionEvent.FRESHNESS_FAIL):      VisionState.RETRY_TARGETING,
    (VisionState.PRECHECK,         VisionEvent.CONFIDENCE_FAIL):     VisionState.RETRY_TARGETING,
    (VisionState.PRECHECK,         VisionEvent.RISK_REQUIRES_APPROVAL): VisionState.RETRY_TARGETING,
    (VisionState.PRECHECK,         VisionEvent.APPROVAL_DENIED):     VisionState.WATCHING,
    (VisionState.PRECHECK,         VisionEvent.IDEMPOTENCY_HIT):     VisionState.WATCHING,
    (VisionState.PRECHECK,         VisionEvent.INTENT_EXPIRED):      VisionState.WATCHING,

    # ---- ACTING ----
    (VisionState.ACTING,           VisionEvent.ACTION_DISPATCHED):   VisionState.VERIFYING,

    # ---- VERIFYING ----
    (VisionState.VERIFYING,        VisionEvent.POSTCONDITION_MET):   VisionState.WATCHING,
    (VisionState.VERIFYING,        VisionEvent.POSTCONDITION_FAIL):  VisionState.RETRY_TARGETING,

    # ---- RETRY_TARGETING ----
    (VisionState.RETRY_TARGETING,  VisionEvent.RETRY):               VisionState.ACTION_TARGETING,
    (VisionState.RETRY_TARGETING,  VisionEvent.RETRY_EXCEEDED):      VisionState.FAILED,

    # ---- DEGRADED ----
    (VisionState.DEGRADED,         VisionEvent.HEALTH_CHECK_PASS):   VisionState.RECOVERING,
    (VisionState.DEGRADED,         VisionEvent.HEALTH_CHECK_FAIL):   VisionState.DEGRADED,   # self-loop

    # ---- RECOVERING ----
    (VisionState.RECOVERING,       VisionEvent.N_CONSECUTIVE_HEALTHY): VisionState.WATCHING,
    (VisionState.RECOVERING,       VisionEvent.HEALTH_CHECK_FAIL):   VisionState.DEGRADED,

    # ---- FAILED ----
    (VisionState.FAILED,           VisionEvent.USER_ACKNOWLEDGES):   VisionState.WATCHING,
}

# Inject STOP → IDLE for every non-IDLE state
for _s in VisionState:
    if _s != VisionState.IDLE:
        _TRANSITIONS[(_s, VisionEvent.STOP)] = VisionState.IDLE


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

class VisionStateMachine:
    """
    Deterministic state machine for the real-time vision action loop.

    Parameters
    ----------
    on_transition:
        Optional callable that receives a telemetry dict on every
        successful transition.  The dict contains:
            from_state   — VisionState the machine left
            to_state     — VisionState the machine entered
            event        — VisionEvent that triggered the transition
            timestamp    — float (time.monotonic())
            retry_count  — int, current retry count after side-effects
    """

    MAX_RETRIES: int = 2

    def __init__(
        self,
        on_transition: Optional[Callable[[Dict], None]] = None,
    ) -> None:
        self._state: VisionState = VisionState.IDLE
        self._retry_count: int = 0
        self.on_transition = on_transition

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    @property
    def state(self) -> VisionState:
        return self._state

    @property
    def retry_count(self) -> int:
        return self._retry_count

    def transition(self, event: VisionEvent) -> VisionState:
        """
        Apply *event* to the current state.

        Returns the new state on success.
        Raises TransitionError if the (state, event) pair is not in the table.
        """
        key = (self._state, event)
        if key not in _TRANSITIONS:
            raise TransitionError(self._state, event)

        from_state = self._state
        to_state = _TRANSITIONS[key]

        # Side effects — order matters:
        # 1. Increment retry count when re-entering ACTION_TARGETING from retry path.
        # 2. Reset retry count when entering WATCHING (success) or fresh targeting.
        if to_state == VisionState.ACTION_TARGETING:
            if from_state == VisionState.RETRY_TARGETING:
                self._retry_count += 1
            else:
                # Fresh action target from ANALYZING path
                self._retry_count = 0

        if to_state == VisionState.WATCHING:
            self._retry_count = 0

        # Commit state
        self._state = to_state

        # Emit telemetry
        if self.on_transition is not None:
            record: Dict = {
                "from_state":  from_state,
                "to_state":    to_state,
                "event":       event,
                "timestamp":   time.monotonic(),
                "retry_count": self._retry_count,
            }
            self.on_transition(record)

        return self._state
