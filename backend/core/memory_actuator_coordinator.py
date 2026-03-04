"""Memory Actuator Coordinator — serializes competing memory actions.

v260.2: Prevents tug-of-war between process_cleanup_manager,
resource_governor, gcp_vm_manager, and DisplayPressureController.

Only one actuator acts per evaluation cycle.  Actions are ordered by
priority (least disruptive first).  Stale decisions are rejected.
Failed actions are quarantined after exceeding their failure budget.

Design invariants:
* submit() is synchronous and O(1) — never blocks the caller.
* drain_pending() returns actions sorted by priority (ascending).
* Quarantined actions are silently dropped on submit.
* Shadow mode flags actions but never suppresses them from drain.
"""
from __future__ import annotations

import dataclasses
import logging
import threading
import time
import uuid
from collections import defaultdict
from typing import Dict, List, Optional

from backend.core.memory_types import ActuatorAction, DecisionEnvelope

logger = logging.getLogger(__name__)


@dataclasses.dataclass
class PendingAction:
    """A submitted actuator action awaiting execution."""
    decision_id: str
    action: ActuatorAction
    envelope: DecisionEnvelope
    source: str
    submitted_at: float
    shadow: bool = False


class MemoryActuatorCoordinator:
    """Serializes memory actuator requests across the system.

    Thread-safe: submit() and drain_pending() can be called from any thread.
    """

    def __init__(
        self,
        *,
        failure_budget: int = 3,
        quarantine_seconds: float = 300.0,
        shadow_mode: bool = False,
    ) -> None:
        self._lock = threading.Lock()
        self._pending: List[PendingAction] = []
        self._failure_budget = failure_budget
        self._quarantine_seconds = quarantine_seconds
        self._shadow_mode = shadow_mode

        # Epoch/sequence tracking for staleness checks
        self._current_epoch: int = 0
        self._current_sequence: int = 0

        # Failure tracking per action type
        self._failure_counts: Dict[ActuatorAction, int] = defaultdict(int)
        self._quarantine_until: Dict[ActuatorAction, float] = {}

        # Stats
        self._total_submitted: int = 0
        self._total_rejected_stale: int = 0
        self._total_rejected_quarantined: int = 0

    def advance_epoch(self, epoch: int, sequence: int) -> None:
        """Update current epoch/sequence (called by broker on new snapshot)."""
        with self._lock:
            self._current_epoch = epoch
            self._current_sequence = sequence

    def submit(
        self,
        action: ActuatorAction,
        envelope: DecisionEnvelope,
        source: str,
    ) -> Optional[str]:
        """Submit an actuator action request.

        Returns decision_id if accepted, None if rejected (stale or quarantined).
        """
        with self._lock:
            # Reject stale decisions
            if envelope.is_stale(
                current_epoch=self._current_epoch,
                current_sequence=self._current_sequence,
            ):
                self._total_rejected_stale += 1
                logger.debug(
                    "[ActuatorCoord] Rejected stale %s from %s "
                    "(envelope epoch=%d seq=%d, current epoch=%d seq=%d)",
                    action.value, source,
                    envelope.epoch, envelope.sequence,
                    self._current_epoch, self._current_sequence,
                )
                return None

            # Reject quarantined actions
            if self.is_quarantined(action):
                self._total_rejected_quarantined += 1
                logger.debug(
                    "[ActuatorCoord] Rejected quarantined %s from %s",
                    action.value, source,
                )
                return None

            decision_id = f"dec-{uuid.uuid4().hex[:12]}"
            self._pending.append(PendingAction(
                decision_id=decision_id,
                action=action,
                envelope=envelope,
                source=source,
                submitted_at=time.monotonic(),
                shadow=self._shadow_mode,
            ))
            self._total_submitted += 1
            return decision_id

    def drain_pending(self) -> List[PendingAction]:
        """Return all pending actions sorted by priority, clearing the queue."""
        with self._lock:
            actions = sorted(self._pending, key=lambda a: a.action.priority)
            self._pending = []
            return actions

    def report_failure(self, action: ActuatorAction, reason: str) -> None:
        """Report a failed actuator action.  Quarantines after failure_budget."""
        with self._lock:
            self._failure_counts[action] += 1
            if self._failure_counts[action] >= self._failure_budget:
                self._quarantine_until[action] = (
                    time.monotonic() + self._quarantine_seconds
                )
                logger.warning(
                    "[ActuatorCoord] Quarantined %s for %.0fs after %d failures: %s",
                    action.value, self._quarantine_seconds,
                    self._failure_counts[action], reason,
                )

    def report_success(self, action: ActuatorAction) -> None:
        """Report a successful actuator action.  Resets failure counter."""
        with self._lock:
            self._failure_counts[action] = 0
            self._quarantine_until.pop(action, None)

    def is_quarantined(self, action: ActuatorAction) -> bool:
        """Check if an action type is currently quarantined."""
        deadline = self._quarantine_until.get(action)
        if deadline is None:
            return False
        if time.monotonic() >= deadline:
            # Quarantine expired — clear it
            self._quarantine_until.pop(action, None)
            self._failure_counts[action] = 0
            return False
        return True

    def get_stats(self) -> Dict[str, int]:
        """Return coordinator statistics."""
        with self._lock:
            return {
                "total_submitted": self._total_submitted,
                "total_rejected_stale": self._total_rejected_stale,
                "total_rejected_quarantined": self._total_rejected_quarantined,
                "pending_count": len(self._pending),
                "quarantined_actions": [
                    a.value for a in self._quarantine_until
                    if self.is_quarantined(a)
                ],
                "shadow_mode": self._shadow_mode,
            }
