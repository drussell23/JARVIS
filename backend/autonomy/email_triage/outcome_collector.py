"""Outcome collector for the email triage feedback loop (WS5).

Captures user outcomes (replied, relabeled, deleted, archived, opened, ignored)
and funnels them to the state store's sender_reputation table and (optionally)
the Reactor-Core ExperienceDataQueue.

Outcome confidence tiers (Gate #4):
  HIGH   — replied, relabeled, deleted  → feed adaptation at 1.0x
  MEDIUM — archived                     → feed adaptation at 0.5x
  LOW    — opened, ignored              → recorded, NOT used for adaptation
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from autonomy.email_triage.config import TriageConfig
from autonomy.email_triage.events import EVENT_OUTCOME_CAPTURED, emit_triage_event
from autonomy.email_triage.schemas import TriagedEmail

logger = logging.getLogger("jarvis.email_triage.outcome_collector")

# ---------------------------------------------------------------------------
# Outcome confidence classification
# ---------------------------------------------------------------------------

CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_LOW = "low"

# Maps outcome name -> confidence level
_OUTCOME_CONFIDENCE: Dict[str, str] = {
    "replied": CONFIDENCE_HIGH,
    "relabeled": CONFIDENCE_HIGH,
    "deleted": CONFIDENCE_HIGH,
    "archived": CONFIDENCE_MEDIUM,
    "opened": CONFIDENCE_LOW,
    "ignored": CONFIDENCE_LOW,
}


def outcome_confidence(outcome: str) -> str:
    """Return the confidence level for an outcome name."""
    return _OUTCOME_CONFIDENCE.get(outcome, CONFIDENCE_LOW)


def feeds_adaptation(outcome: str) -> bool:
    """Return True if this outcome should feed weight adaptation (Gate #4)."""
    conf = outcome_confidence(outcome)
    return conf in (CONFIDENCE_HIGH, CONFIDENCE_MEDIUM)


def adaptation_weight(outcome: str) -> float:
    """Return the adaptation weight multiplier for an outcome.

    HIGH=1.0, MEDIUM=0.5, LOW=0.0 (excluded).
    """
    conf = outcome_confidence(outcome)
    if conf == CONFIDENCE_HIGH:
        return 1.0
    elif conf == CONFIDENCE_MEDIUM:
        return 0.5
    return 0.0


# ---------------------------------------------------------------------------
# OutcomeCollector
# ---------------------------------------------------------------------------


class OutcomeCollector:
    """Captures user outcomes for triaged emails and records them durably."""

    def __init__(
        self,
        config: TriageConfig,
        state_store: Any = None,
    ):
        self._config = config
        self._state_store = state_store
        self._recorded_outcomes: List[Dict[str, Any]] = []

    async def record_outcome(
        self,
        message_id: str,
        outcome: str,
        sender_domain: str,
        tier: int,
        score: int,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Record a single outcome for a triaged email.

        Updates sender_reputation in the state store and emits an event.

        Args:
            message_id: The email's message ID.
            outcome: Outcome name (replied, relabeled, deleted, archived, opened, ignored).
            sender_domain: Sender's domain for reputation tracking.
            tier: The triage tier assigned to this email.
            score: The triage score assigned to this email.
            metadata: Optional extra data.
        """
        confidence = outcome_confidence(outcome)
        record = {
            "message_id": message_id,
            "outcome": outcome,
            "confidence": confidence,
            "sender_domain": sender_domain,
            "tier": tier,
            "score": score,
            "timestamp": time.time(),
            "feeds_adaptation": feeds_adaptation(outcome),
            "adaptation_weight": adaptation_weight(outcome),
        }
        if metadata:
            record["metadata"] = metadata

        self._recorded_outcomes.append(record)

        # Update sender reputation in state store
        if self._state_store is not None:
            try:
                await self._state_store.update_sender_reputation(
                    sender_domain, tier, score,
                )
            except Exception as e:
                logger.debug("Sender reputation update failed: %s", e)

        # Emit event
        emit_triage_event(EVENT_OUTCOME_CAPTURED, {
            "message_id": message_id,
            "outcome": outcome,
            "confidence": confidence,
            "tier": tier,
            "feeds_adaptation": record["feeds_adaptation"],
        })

        # Enqueue to ExperienceDataQueue if available (best-effort)
        try:
            await self._enqueue_to_reactor_core(record)
        except Exception as e:
            logger.debug("Reactor-Core enqueue failed (non-fatal): %s", e)

    async def _enqueue_to_reactor_core(self, record: Dict[str, Any]) -> None:
        """Best-effort enqueue to the Reactor-Core ExperienceDataQueue.

        Imports lazily to avoid hard dependency on core.experience_queue.
        """
        try:
            from core.experience_queue import (
                ExperiencePriority,
                ExperienceType,
                enqueue_experience,
            )
        except ImportError:
            return  # ExperienceQueue not available

        await enqueue_experience(
            experience_type=ExperienceType.BEHAVIORAL_EVENT,
            data={
                "source": "email_triage",
                "outcome": record["outcome"],
                "confidence": record["confidence"],
                "tier": record["tier"],
                "sender_domain": record["sender_domain"],
            },
            priority=ExperiencePriority.NORMAL,
        )

    def get_adaptation_outcomes(self) -> List[Dict[str, Any]]:
        """Return only HIGH+MEDIUM confidence outcomes for weight adaptation (Gate #4).

        LOW-confidence outcomes (opened, ignored) are excluded.
        """
        return [
            r for r in self._recorded_outcomes
            if r.get("feeds_adaptation", False)
        ]

    def get_all_outcomes(self) -> List[Dict[str, Any]]:
        """Return all recorded outcomes regardless of confidence."""
        return list(self._recorded_outcomes)

    def clear(self) -> None:
        """Clear recorded outcomes (after processing)."""
        self._recorded_outcomes.clear()

    @staticmethod
    def _classify_outcome(original: set, current: set) -> Optional[str]:
        """Classify outcome from label delta.

        Priority order:
          1. replied  — SENT label added (user replied to the thread)
          2. deleted  — TRASH label present (user trashed the email)
          3. archived — INBOX removed without trashing (user archived)
          4. relabeled — any other label change (user re-categorized)
          5. None     — no change detected
        """
        if "SENT" in current and "SENT" not in original:
            return "replied"
        if "TRASH" in current:
            return "deleted"
        if "INBOX" in original and "INBOX" not in current:
            return "archived"
        if current != original:
            return "relabeled"
        return None

    async def check_outcomes_for_cycle(
        self,
        workspace_agent: Any,
        prior_triaged: Dict[str, TriagedEmail],
    ) -> List[Dict[str, Any]]:
        """Poll Gmail for label/status changes on previously triaged emails.

        Checks the prior cycle's triaged emails for user actions since triage.
        This is a best-effort heuristic — real Gmail API limitations mean
        some outcomes (especially "opened") are low-confidence.

        Args:
            workspace_agent: The GoogleWorkspaceAgent for API calls.
            prior_triaged: Dict of message_id -> TriagedEmail from prior cycle(s).

        Returns:
            List of outcome records captured this check.
        """
        if workspace_agent is None or not prior_triaged:
            return []

        if not hasattr(workspace_agent, "get_message_labels"):
            return []

        captured: List[Dict[str, Any]] = []

        for msg_id, triaged in prior_triaged.items():
            try:
                current_labels = await workspace_agent.get_message_labels(msg_id)
            except Exception as exc:
                logger.debug(
                    "Failed to fetch labels for %s: %s", msg_id, exc,
                )
                continue

            original_labels = set(triaged.features.label_ids)
            current_labels = set(current_labels)

            outcome = self._classify_outcome(original_labels, current_labels)
            if outcome is None:
                continue

            metadata = {
                "original_labels": sorted(original_labels),
                "current_labels": sorted(current_labels),
            }

            await self.record_outcome(
                message_id=msg_id,
                outcome=outcome,
                sender_domain=triaged.features.sender_domain,
                tier=triaged.scoring.tier,
                score=triaged.scoring.score,
                metadata=metadata,
            )

            # The last recorded outcome is the one we just appended
            captured.append(self._recorded_outcomes[-1])

        return captured
