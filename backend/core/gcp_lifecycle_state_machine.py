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
from typing import Any, Dict, List, Optional

from backend.core.gcp_lifecycle_schema import State, Event, validate_state
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

    @abstractmethod
    async def query_vm_state(self, op_id: str) -> str:
        """Query actual VM state for reconciliation.

        Returns one of: 'running', 'stopped', 'not_found'.
        Used during leader takeover to resolve pending journal entries
        by checking whether a side effect actually completed.
        """
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

        # Register in component_state table with a sentinel journal entry.
        # Uses "gcp_lifecycle_init" action (not "gcp_lifecycle") so recovery
        # replay does not confuse this with a real state transition.
        init_seq = self._journal.fenced_write(
            "gcp_lifecycle_init",
            self._target,
            payload={
                "initial_state": initial_state.value,
                "timestamp": time.time(),
            },
        )
        self._journal.update_component_state(
            self._target,
            initial_state.value,
            seq=init_seq,
            instance_id=target,
        )

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

        # Update component_state projection
        component_kwargs: Dict[str, Any] = {}
        if to_state == State.BOOTING:
            component_kwargs["start_timestamp"] = time.time()
        if event == Event.HEALTH_PROBE_OK:
            component_kwargs["consecutive_failures"] = 0
            component_kwargs["last_probe_category"] = "healthy"
        elif event == Event.HEALTH_UNREACHABLE_CONSECUTIVE:
            component_kwargs["last_probe_category"] = "unreachable"
        elif event == Event.HEALTH_DEGRADED_CONSECUTIVE:
            component_kwargs["last_probe_category"] = "degraded"

        self._journal.update_component_state(
            self._target,
            to_state.value,
            seq=seq,
            **component_kwargs,
        )

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
                    # Update component_state to reflect recovered state
                    self._journal.update_component_state(
                        self._target,
                        recovered_state.value,
                        seq=entry["seq"],
                    )
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

    async def reconcile_on_takeover(self) -> None:
        """Reconcile pending journal entries after leader takeover.

        When a leader crashes and a new leader takes over, there may be
        journal entries with result="pending" whose side effects were
        never confirmed. This method:

        1. Replays gcp_lifecycle entries for this target
        2. For each pending entry with has_side_effect=True:
           - Extracts op_id and queries actual VM state via adapter
           - Marks entry as "committed" (running/stopped) or "failed" (not_found)
           - Journals the reconciliation result
        3. Finds orphaned budget reservations and releases them
        4. Recovers in-memory state from journal via recover_from_journal()
        """
        logger.info(
            "[GCPLifecycle] Reconciling pending entries for %s on leader takeover",
            self._target,
        )

        # Step 1: Replay gcp_lifecycle entries for this target
        lifecycle_entries = await self._journal.replay_from(
            0,
            target_filter=[self._target],
            action_filter=["gcp_lifecycle"],
        )

        pending_with_side_effects: List[dict] = [
            e for e in lifecycle_entries
            if e.get("result") == "pending"
            and e.get("payload", {}).get("has_side_effect") is True
        ]

        # Step 2: Resolve each pending side-effect entry
        reconciled_op_ids: List[str] = []
        failed_op_ids: List[str] = []

        for entry in pending_with_side_effects:
            seq = entry["seq"]
            epoch = entry["epoch"]
            payload = entry.get("payload", {})
            event = payload.get("event", "unknown")

            # Reconstruct op_id: {target}:{event}:{epoch}:{seq}
            op_id = f"{self._target}:{event}:{epoch}:{seq}"

            try:
                vm_state = await self._adapter.query_vm_state(op_id)
            except Exception as exc:
                logger.error(
                    "[GCPLifecycle] Failed to query VM state for op_id=%s: %s",
                    op_id, exc,
                )
                continue

            # Determine result based on actual VM state
            if vm_state == "running":
                result = "committed"
            elif vm_state == "stopped":
                result = "committed"
            elif vm_state == "not_found":
                result = "failed"
            else:
                logger.warning(
                    "[GCPLifecycle] Unknown VM state %r for op_id=%s, skipping",
                    vm_state, op_id,
                )
                continue

            # Mark the journal entry
            self._journal.mark_result(seq, result)

            # Journal the reconciliation
            self._journal.fenced_write(
                "gcp_lifecycle",
                self._target,
                payload={
                    "reconcile_action": "resolve_pending",
                    "original_seq": seq,
                    "op_id": op_id,
                    "vm_state": vm_state,
                    "resolved_as": result,
                    "original_event": event,
                    "timestamp": time.time(),
                },
            )

            if result == "committed":
                reconciled_op_ids.append(op_id)
            else:
                failed_op_ids.append(op_id)

            logger.info(
                "[GCPLifecycle] Reconciled seq=%d op_id=%s: vm_state=%s -> %s",
                seq, op_id, vm_state, result,
            )

        # Step 3: Find and release orphaned budget reservations
        await self._release_orphaned_budgets()

        # Step 4: Recover in-memory state from journal
        await self.recover_from_journal()

        logger.info(
            "[GCPLifecycle] Reconciliation complete for %s: "
            "committed=%d, failed=%d",
            self._target,
            len(reconciled_op_ids),
            len(failed_op_ids),
        )

    async def _release_orphaned_budgets(self) -> None:
        """Release pending budget reservations that have no matching commit/release.

        A budget reservation is orphaned if:
        - action="budget_reserved", result="pending"
        - Its op_id starts with this target's name
        - No corresponding budget_committed or budget_released entry exists
        """
        # Get all budget_reserved entries
        budget_entries = await self._journal.replay_from(
            0,
            action_filter=["budget_reserved"],
        )

        pending_budgets = [
            e for e in budget_entries
            if e.get("result") == "pending"
            and e.get("payload", {}).get("op_id", "").startswith(f"{self._target}:")
        ]

        if not pending_budgets:
            return

        # Get all budget_committed and budget_released entries
        committed_entries = await self._journal.replay_from(
            0,
            action_filter=["budget_committed"],
        )
        released_entries = await self._journal.replay_from(
            0,
            action_filter=["budget_released"],
        )

        # Build set of op_ids that already have a commit or release
        resolved_budget_op_ids = set()
        for e in committed_entries:
            op = e.get("payload", {}).get("op_id")
            if op:
                resolved_budget_op_ids.add(op)
        for e in released_entries:
            op = e.get("payload", {}).get("op_id")
            if op:
                resolved_budget_op_ids.add(op)

        # Release any pending budget that hasn't been committed or released
        for budget_entry in pending_budgets:
            budget_op_id = budget_entry["payload"]["op_id"]
            if budget_op_id not in resolved_budget_op_ids:
                self._journal.release_budget(budget_op_id)
                logger.info(
                    "[GCPLifecycle] Released orphaned budget for op_id=%s",
                    budget_op_id,
                )
