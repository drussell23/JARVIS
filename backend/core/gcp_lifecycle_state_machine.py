# backend/core/gcp_lifecycle_state_machine.py
"""GCP lifecycle state machine engine with journal integration.

Every transition is journaled BEFORE the in-memory state update.
Side effects execute through a pluggable SideEffectAdapter.
State can be recovered from journal replay after crash.

Design doc: docs/plans/2026-02-25-journal-backed-gcp-lifecycle-design.md
"""
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, Optional

from backend.core.gcp_lifecycle_schema import State, Event, validate_state, validate_event
from backend.core.gcp_lifecycle_transitions import get_transition

logger = logging.getLogger("jarvis.gcp_lifecycle")


class SideEffectAdapter(ABC):
    """Abstract adapter for GCP side effects.

    Subclass this to wire real GCP operations (create VM, stop VM, etc.)
    or use a no-op adapter for simulation/testing.
    """

    @abstractmethod
    async def execute(self, action: str, op_id: str, **kwargs) -> Dict[str, Any]:
        """Execute a side effect. Returns result dict."""
        ...


@dataclass
class TransitionResult:
    """Result of processing an event."""
    success: bool
    from_state: State
    to_state: Optional[State] = None
    seq: Optional[int] = None
    reason: str = ""


class GCPLifecycleStateMachine:
    """Journal-backed state machine for GCP lifecycle management.

    Usage:
        sm = GCPLifecycleStateMachine(journal, adapter, target="invincible_node")
        result = await sm.handle_event(Event.PRESSURE_TRIGGERED, payload={...})
        assert result.success
        assert sm.state == State.TRIGGERING
    """

    def __init__(
        self,
        journal,
        adapter: SideEffectAdapter,
        target: str,
        initial_state: State = State.IDLE,
    ):
        self._journal = journal
        self._adapter = adapter
        self._target = target
        self._state = initial_state

    @property
    def state(self) -> State:
        return self._state

    @property
    def target(self) -> str:
        return self._target

    async def handle_event(
        self,
        event: Event,
        *,
        payload: Optional[Dict[str, Any]] = None,
    ) -> TransitionResult:
        """Process an event: look up transition, journal it, execute side effects, update state.

        Returns TransitionResult with success=False if no valid transition exists.
        """
        from_state = self._state

        # Look up transition
        transition = get_transition(from_state, event)
        if transition is None:
            return TransitionResult(
                success=False,
                from_state=from_state,
                reason=f"No transition for ({from_state.value}, {event.value})",
            )

        to_state = transition.next_state

        # Build journal payload
        journal_payload = {
            "from_state": from_state.value,
            "to_state": to_state.value,
            "event": event.value,
            "journal_actions": list(transition.journal_actions),
            "has_side_effect": transition.has_side_effect,
            "timestamp": time.time(),
        }
        if payload:
            journal_payload["event_payload"] = payload

        # Journal BEFORE state update (commit-before-mutate)
        seq = self._journal.fenced_write(
            "gcp_lifecycle",
            self._target,
            payload=journal_payload,
        )

        # Execute side effects if any
        if transition.has_side_effect:
            op_id = f"{self._target}:{event.value}:{self._journal.epoch}:{seq}"
            try:
                side_effect_payload = payload or {}
                await self._adapter.execute(
                    action=event.value,
                    op_id=op_id,
                    from_state=from_state.value,
                    to_state=to_state.value,
                    **side_effect_payload,
                )
            except Exception as exc:
                logger.error(
                    "Side effect failed for %s: %s", event.value, exc,
                    exc_info=True,
                )
                # Side effect failure doesn't prevent state transition
                # The journal records the intent; reconciliation handles recovery

        # Update in-memory state AFTER journal commit
        self._state = to_state

        return TransitionResult(
            success=True,
            from_state=from_state,
            to_state=to_state,
            seq=seq,
        )

    async def recover_from_journal(self) -> None:
        """Recover state by replaying journal entries for this target.

        Scans all gcp_lifecycle entries for this target and reconstructs
        the last known state from the most recent transition.
        """
        entries = await self._journal.replay_from(
            0,
            target_filter=[self._target],
            action_filter=["gcp_lifecycle"],
        )

        if not entries:
            logger.info(
                "[GCPLifecycle] No journal entries for %s, staying at %s",
                self._target, self._state.value,
            )
            return

        # Find the last transition entry with a valid to_state
        for entry in reversed(entries):
            entry_payload = entry.get("payload", {})
            if entry_payload and "to_state" in entry_payload:
                try:
                    recovered_state = validate_state(entry_payload["to_state"])
                    self._state = recovered_state
                    logger.info(
                        "[GCPLifecycle] Recovered %s to state=%s from seq=%d",
                        self._target, recovered_state.value, entry["seq"],
                    )
                    return
                except ValueError:
                    logger.warning(
                        "[GCPLifecycle] Invalid state in journal entry seq=%d: %s",
                        entry["seq"], entry_payload.get("to_state"),
                    )

        logger.warning(
            "[GCPLifecycle] No valid state found in %d journal entries for %s",
            len(entries), self._target,
        )
