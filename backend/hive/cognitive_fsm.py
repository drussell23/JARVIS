"""
Cognitive State Machine — Dynamic 3-State FSM for Hive Compute Governance
==========================================================================

Governs when agents are allowed to consume LLM compute:
  - BASELINE: zero compute (idle)
  - REM:      cheap 35B triage (background reasoning cycles)
  - FLOW:     expensive 397B reasoning (active engineering)

Follows the PreemptionFsmEngine pattern:
  - ``decide()`` is a **pure** function (no I/O, no mutations).
  - ``apply_last_decision()`` commits the transition and persists state.
  - Crash recovery always resets to BASELINE (safety invariant).

Public surface:
  - CognitiveEvent     — input events that drive transitions
  - CognitiveTransition — immutable record of a decided transition
  - CognitiveFsm       — the state machine itself
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional

from backend.hive.thread_models import CognitiveState

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration — read from environment at module level
# ---------------------------------------------------------------------------

_HIVE_STATE_DIR = Path(
    os.environ.get("JARVIS_HIVE_STATE_DIR", str(Path.home() / ".jarvis" / "hive"))
)

REM_INTERVAL_H: float = float(os.environ.get("JARVIS_HIVE_REM_INTERVAL_H", "6"))
REM_LOAD_THRESHOLD: float = float(
    os.environ.get("JARVIS_HIVE_REM_LOAD_THRESHOLD", "30")
)
FLOW_DEBATE_TIMEOUT_M: float = float(
    os.environ.get("JARVIS_HIVE_FLOW_DEBATE_TIMEOUT_M", "15")
)
FLOW_TOKEN_CEILING: int = int(os.environ.get("JARVIS_HIVE_FLOW_TOKEN_CEILING", "50000"))

# Derived: idle threshold in seconds
_REM_IDLE_THRESHOLD_S: float = REM_INTERVAL_H * 3600.0


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


class CognitiveEvent(str, Enum):
    """Events that drive cognitive state transitions."""

    REM_TRIGGER = "rem_trigger"
    FLOW_TRIGGER = "flow_trigger"
    COUNCIL_ESCALATION = "council_escalation"
    COUNCIL_COMPLETE = "council_complete"
    SPINDOWN = "spindown"
    USER_SPINDOWN = "user_spindown"


# ---------------------------------------------------------------------------
# Valid spindown reasons
# ---------------------------------------------------------------------------

_VALID_SPINDOWN_REASONS = frozenset({
    "pr_merged",
    "debate_timeout",
    "token_budget_exhausted",
    "iron_gate_hard_reject",
    "user_manual_spindown",
})


# ---------------------------------------------------------------------------
# Transition record (frozen dataclass — immutable once created)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CognitiveTransition:
    """Immutable record of a cognitive state transition decision."""

    from_state: CognitiveState
    to_state: CognitiveState
    event: CognitiveEvent
    reason_code: str
    noop: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)
    decided_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# CognitiveFsm — the state machine
# ---------------------------------------------------------------------------


class CognitiveFsm:
    """
    Dynamic 3-state Cognitive FSM for Hive compute governance.

    Pure ``decide()`` followed by ``apply_last_decision()`` for side effects.
    Crash recovery always resets to BASELINE (safety invariant).
    """

    def __init__(
        self,
        state_file: Optional[Path] = None,
        crash_recovery: bool = False,
    ) -> None:
        self._state_file: Path = state_file or (_HIVE_STATE_DIR / "cognitive_state.json")
        self._state: CognitiveState = CognitiveState.BASELINE
        self._last_decision: Optional[CognitiveTransition] = None

        if crash_recovery and self._state_file.exists():
            # Safety invariant: always reset to BASELINE on crash recovery
            logger.warning(
                "Crash recovery: resetting cognitive state to BASELINE (was persisted at %s)",
                self._state_file,
            )
            self._state = CognitiveState.BASELINE
            self._persist_state()
        elif self._state_file.exists() and not crash_recovery:
            self._load_state()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def state(self) -> CognitiveState:
        """Current cognitive state (read-only)."""
        return self._state

    # ------------------------------------------------------------------
    # Pure decision function — NO mutations, NO I/O
    # ------------------------------------------------------------------

    def decide(
        self,
        event: CognitiveEvent,
        *,
        idle_seconds: float = 0.0,
        system_load_pct: float = 0.0,
        graduation_candidates: int = 0,
        spindown_reason: str = "",
    ) -> CognitiveTransition:
        """
        Compute the next transition given the current state and an event.

        This is a **pure** function: it does not mutate ``self._state``,
        perform I/O, or produce any side effects. The caller must invoke
        ``apply_last_decision()`` to commit the result.

        Parameters
        ----------
        event:
            The cognitive event that triggered this decision.
        idle_seconds:
            How long the system has been idle (used for REM entry).
        system_load_pct:
            Current system load percentage (used for REM gating).
        graduation_candidates:
            Number of threads ready for graduation (informational).
        spindown_reason:
            Reason string for SPINDOWN events.
        """
        ctx = {
            "idle_seconds": idle_seconds,
            "system_load_pct": system_load_pct,
            "graduation_candidates": graduation_candidates,
            "spindown_reason": spindown_reason,
        }

        # USER_SPINDOWN from any non-BASELINE state -> BASELINE
        if event == CognitiveEvent.USER_SPINDOWN:
            if self._state == CognitiveState.BASELINE:
                return self._noop(event, ctx)
            decision = CognitiveTransition(
                from_state=self._state,
                to_state=CognitiveState.BASELINE,
                event=event,
                reason_code="USER_MANUAL_SPINDOWN",
                metadata=ctx,
            )
            self._last_decision = decision
            return decision

        # Dispatch to per-state handlers
        if self._state == CognitiveState.BASELINE:
            decision = self._from_baseline(event, ctx)
        elif self._state == CognitiveState.REM:
            decision = self._from_rem(event, ctx)
        elif self._state == CognitiveState.FLOW:
            decision = self._from_flow(event, ctx)
        else:
            # Should never happen, but defensive
            decision = self._noop(event, ctx)

        self._last_decision = decision
        return decision

    # ------------------------------------------------------------------
    # Side-effect applicator
    # ------------------------------------------------------------------

    def apply_last_decision(self) -> Optional[CognitiveTransition]:
        """
        Commit the last decision computed by ``decide()``.

        Mutates internal state and persists to disk. Returns the transition
        that was applied, or None if there was no pending decision.
        """
        if self._last_decision is None:
            return None

        decision = self._last_decision
        self._last_decision = None

        if not decision.noop:
            self._state = decision.to_state
            self._persist_state()
            logger.info(
                "Cognitive FSM: %s -> %s (event=%s, reason=%s)",
                decision.from_state.value,
                decision.to_state.value,
                decision.event.value,
                decision.reason_code,
            )

        return decision

    # ------------------------------------------------------------------
    # Per-state handlers (all pure — no mutations, no I/O)
    # ------------------------------------------------------------------

    def _from_baseline(
        self, event: CognitiveEvent, ctx: Dict[str, Any]
    ) -> CognitiveTransition:
        """Handle events while in BASELINE state."""

        if event == CognitiveEvent.REM_TRIGGER:
            idle_s = ctx["idle_seconds"]
            load_pct = ctx["system_load_pct"]

            # Gate: must meet both idle and load thresholds
            if idle_s < _REM_IDLE_THRESHOLD_S:
                return CognitiveTransition(
                    from_state=CognitiveState.BASELINE,
                    to_state=CognitiveState.BASELINE,
                    event=event,
                    reason_code="T1_BLOCKED_LOW_IDLE",
                    noop=True,
                    metadata=ctx,
                )
            if load_pct >= REM_LOAD_THRESHOLD:
                return CognitiveTransition(
                    from_state=CognitiveState.BASELINE,
                    to_state=CognitiveState.BASELINE,
                    event=event,
                    reason_code="T1_BLOCKED_HIGH_LOAD",
                    noop=True,
                    metadata=ctx,
                )

            # Both conditions met -> transition to REM
            return CognitiveTransition(
                from_state=CognitiveState.BASELINE,
                to_state=CognitiveState.REM,
                event=event,
                reason_code="T1_REM_TRIGGER",
                metadata=ctx,
            )

        if event == CognitiveEvent.FLOW_TRIGGER:
            return CognitiveTransition(
                from_state=CognitiveState.BASELINE,
                to_state=CognitiveState.FLOW,
                event=event,
                reason_code="T2_FLOW_TRIGGER",
                metadata=ctx,
            )

        # SPINDOWN from BASELINE is a noop
        if event == CognitiveEvent.SPINDOWN:
            return self._noop(event, ctx)

        # Any other event from BASELINE is a noop
        return self._noop(event, ctx)

    def _from_rem(
        self, event: CognitiveEvent, ctx: Dict[str, Any]
    ) -> CognitiveTransition:
        """Handle events while in REM state."""

        if event == CognitiveEvent.COUNCIL_ESCALATION:
            return CognitiveTransition(
                from_state=CognitiveState.REM,
                to_state=CognitiveState.FLOW,
                event=event,
                reason_code="T2B_COUNCIL_ESCALATION",
                metadata=ctx,
            )

        if event == CognitiveEvent.COUNCIL_COMPLETE:
            return CognitiveTransition(
                from_state=CognitiveState.REM,
                to_state=CognitiveState.BASELINE,
                event=event,
                reason_code="T3B_COUNCIL_COMPLETE",
                metadata=ctx,
            )

        if event == CognitiveEvent.SPINDOWN:
            return CognitiveTransition(
                from_state=CognitiveState.REM,
                to_state=CognitiveState.BASELINE,
                event=event,
                reason_code="T3_SPINDOWN_REM",
                metadata=ctx,
            )

        # REM_TRIGGER while already in REM: noop
        # FLOW_TRIGGER while in REM: noop (must escalate via COUNCIL_ESCALATION)
        return self._noop(event, ctx)

    def _from_flow(
        self, event: CognitiveEvent, ctx: Dict[str, Any]
    ) -> CognitiveTransition:
        """Handle events while in FLOW state."""

        if event == CognitiveEvent.SPINDOWN:
            reason = ctx.get("spindown_reason", "")
            # Map spindown reasons to reason codes
            reason_map = {
                "pr_merged": "T3_SPINDOWN_PR_MERGED",
                "debate_timeout": "T3_SPINDOWN_DEBATE_TIMEOUT",
                "token_budget_exhausted": "T3_SPINDOWN_TOKEN_BUDGET_EXHAUSTED",
                "iron_gate_hard_reject": "T3_SPINDOWN_IRON_GATE_HARD_REJECT",
                "user_manual_spindown": "USER_MANUAL_SPINDOWN",
            }
            reason_code = reason_map.get(reason, f"T3_SPINDOWN_{reason.upper()}" if reason else "T3_SPINDOWN")

            return CognitiveTransition(
                from_state=CognitiveState.FLOW,
                to_state=CognitiveState.BASELINE,
                event=event,
                reason_code=reason_code,
                metadata=ctx,
            )

        # FLOW_TRIGGER while already in FLOW: noop (no state stacking)
        if event == CognitiveEvent.FLOW_TRIGGER:
            return self._noop(event, ctx)

        # REM_TRIGGER while in FLOW: noop (no downgrade)
        if event == CognitiveEvent.REM_TRIGGER:
            return self._noop(event, ctx)

        # Any other event from FLOW is a noop
        return self._noop(event, ctx)

    # ------------------------------------------------------------------
    # Noop helper
    # ------------------------------------------------------------------

    def _noop(
        self, event: CognitiveEvent, ctx: Dict[str, Any]
    ) -> CognitiveTransition:
        """Create a no-op transition (state unchanged)."""
        decision = CognitiveTransition(
            from_state=self._state,
            to_state=self._state,
            event=event,
            reason_code="NOOP",
            noop=True,
            metadata=ctx,
        )
        self._last_decision = decision
        return decision

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _persist_state(self) -> None:
        """Write current state to JSON file."""
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "state": self._state.value,
                "persisted_at": datetime.now(timezone.utc).isoformat(),
            }
            self._state_file.write_text(json.dumps(payload, indent=2))
        except OSError:
            logger.exception("Failed to persist cognitive state to %s", self._state_file)

    def _load_state(self) -> None:
        """Load state from JSON file."""
        try:
            data = json.loads(self._state_file.read_text())
            self._state = CognitiveState(data["state"])
            logger.info(
                "Loaded cognitive state %s from %s",
                self._state.value,
                self._state_file,
            )
        except (OSError, json.JSONDecodeError, KeyError, ValueError):
            logger.warning(
                "Failed to load cognitive state from %s — defaulting to BASELINE",
                self._state_file,
            )
            self._state = CognitiveState.BASELINE
