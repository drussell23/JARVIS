"""Email triage runner — orchestrates the full triage cycle.

Called by agent_runtime.py housekeeping loop. Coordinates:
1. Fetch unread emails via GoogleWorkspaceAgent
2. Extract features (heuristic + optional J-Prime)
3. Score each email deterministically
4. Apply Gmail labels
5. Decide notification action via policy
6. Emit observability events
"""

from __future__ import annotations

import logging
import time
from typing import Any, ClassVar, Dict, List, Optional
from uuid import uuid4

from autonomy.email_triage.config import TriageConfig, get_triage_config
from autonomy.email_triage.events import (
    emit_triage_event,
    EVENT_CYCLE_STARTED,
    EVENT_EMAIL_TRIAGED,
    EVENT_CYCLE_COMPLETED,
    EVENT_TRIAGE_ERROR,
)
from autonomy.email_triage.extraction import extract_features
from autonomy.email_triage.labels import apply_label, ensure_labels_exist
from autonomy.email_triage.policy import NotificationPolicy
from autonomy.email_triage.schemas import TriageCycleReport, TriagedEmail
from autonomy.email_triage.scoring import score_email

logger = logging.getLogger("jarvis.email_triage.runner")


class EmailTriageRunner:
    """Singleton runner for the email triage cycle."""

    _instance: ClassVar[Optional[EmailTriageRunner]] = None

    def __init__(
        self,
        config: Optional[TriageConfig] = None,
        workspace_agent: Any = None,
        router: Any = None,
    ):
        self._config = config or get_triage_config()
        self._workspace_agent = workspace_agent
        self._router = router
        self._policy = NotificationPolicy(self._config)
        self._label_map: Dict[str, str] = {}
        self._labels_initialized = False

    @classmethod
    def get_instance(cls, **kwargs) -> EmailTriageRunner:
        if cls._instance is None:
            cls._instance = cls(**kwargs)
        return cls._instance

    async def run_cycle(self) -> TriageCycleReport:
        """Execute a single triage cycle."""
        cycle_id = uuid4().hex[:12]
        started_at = time.time()
        errors: List[str] = []
        tier_counts: Dict[int, int] = {}
        notifications_sent = 0
        notifications_suppressed = 0
        emails_fetched = 0
        emails_processed = 0

        if not self._config.enabled:
            return TriageCycleReport(
                cycle_id=cycle_id,
                started_at=started_at,
                completed_at=time.time(),
                emails_fetched=0,
                emails_processed=0,
                tier_counts={},
                notifications_sent=0,
                notifications_suppressed=0,
                errors=[],
                skipped=True,
                skip_reason="disabled",
            )

        emit_triage_event(EVENT_CYCLE_STARTED, {"cycle_id": cycle_id})

        # Ensure labels exist
        if not self._labels_initialized and self._workspace_agent:
            try:
                gmail_svc = getattr(self._workspace_agent, "_gmail_service", None)
                if gmail_svc:
                    self._label_map = await ensure_labels_exist(gmail_svc, self._config)
                    self._labels_initialized = True
            except Exception as e:
                logger.warning("Label init failed: %s", e)
                errors.append(f"label_init: {e}")

        # Fetch unread emails
        try:
            emails = await self._fetch_unread()
            emails_fetched = len(emails)
        except Exception as e:
            logger.warning("Email fetch failed: %s", e)
            errors.append(f"fetch: {e}")
            emit_triage_event(EVENT_TRIAGE_ERROR, {
                "cycle_id": cycle_id,
                "error_type": "fetch_failed",
                "message": str(e),
            })
            return TriageCycleReport(
                cycle_id=cycle_id,
                started_at=started_at,
                completed_at=time.time(),
                emails_fetched=0,
                emails_processed=0,
                tier_counts={},
                notifications_sent=0,
                notifications_suppressed=0,
                errors=errors,
            )

        # Process each email
        for email in emails[: self._config.max_emails_per_cycle]:
            try:
                # Extract features
                features = await extract_features(
                    email, self._router, config=self._config,
                )

                # Score
                scoring = score_email(features, self._config)

                # Apply label
                try:
                    await self._apply_label(
                        email.get("id", ""),
                        scoring.tier_label,
                    )
                except Exception as label_err:
                    errors.append(f"label:{email.get('id', '?')}: {label_err}")

                # Decide notification
                triaged = TriagedEmail(
                    features=features,
                    scoring=scoring,
                    notification_action="",
                    processed_at=time.time(),
                )
                action = self._policy.decide_action(triaged)
                triaged.notification_action = action

                # Track stats
                tier_counts[scoring.tier] = tier_counts.get(scoring.tier, 0) + 1
                if action == "immediate":
                    notifications_sent += 1
                elif action in ("label_only", "quarantine"):
                    if scoring.tier <= 2:
                        notifications_suppressed += 1

                emails_processed += 1

                emit_triage_event(EVENT_EMAIL_TRIAGED, {
                    "cycle_id": cycle_id,
                    "message_id": features.message_id,
                    "score": scoring.score,
                    "tier": scoring.tier,
                    "action": action,
                    "breakdown": scoring.breakdown,
                })

            except Exception as e:
                errors.append(f"process:{email.get('id', '?')}: {e}")
                emails_processed += 1  # Count as processed (attempted)
                emit_triage_event(EVENT_TRIAGE_ERROR, {
                    "cycle_id": cycle_id,
                    "error_type": "process_failed",
                    "message_id": email.get("id", ""),
                    "message": str(e),
                })

        # Flush summary if window elapsed
        if self._policy.should_flush_summary():
            self._policy.flush_summary()

        completed_at = time.time()
        emit_triage_event(EVENT_CYCLE_COMPLETED, {
            "cycle_id": cycle_id,
            "duration_ms": int((completed_at - started_at) * 1000),
            "emails_fetched": emails_fetched,
            "emails_processed": emails_processed,
            "tier_counts": tier_counts,
            "notifications_sent": notifications_sent,
            "errors": len(errors),
        })

        return TriageCycleReport(
            cycle_id=cycle_id,
            started_at=started_at,
            completed_at=completed_at,
            emails_fetched=emails_fetched,
            emails_processed=emails_processed,
            tier_counts=tier_counts,
            notifications_sent=notifications_sent,
            notifications_suppressed=notifications_suppressed,
            errors=errors,
        )

    async def _fetch_unread(self) -> List[Dict[str, Any]]:
        """Fetch unread emails via workspace agent."""
        if self._workspace_agent:
            result = await self._workspace_agent._fetch_unread_emails({
                "limit": self._config.max_emails_per_cycle,
            })
            return result.get("emails", [])
        return []

    async def _apply_label(
        self, message_id: str, label_name: str
    ) -> None:
        """Apply Gmail label to message."""
        if self._workspace_agent and self._label_map:
            gmail_svc = getattr(self._workspace_agent, "_gmail_service", None)
            if gmail_svc:
                await apply_label(gmail_svc, message_id, label_name, self._label_map)
