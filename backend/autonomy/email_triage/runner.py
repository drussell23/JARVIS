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

import asyncio
import logging
import time
from typing import Any, ClassVar, Dict, List, Optional
from uuid import uuid4

from autonomy.email_triage.config import TriageConfig, get_triage_config
from autonomy.email_triage.dependencies import DependencyResolver
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
        notifier: Any = None,
    ):
        self._config = config or get_triage_config()
        self._resolver = DependencyResolver(
            self._config,
            workspace_agent=workspace_agent,
            router=router,
            notifier=notifier,
        )
        self._policy = NotificationPolicy(self._config)
        self._label_map: Dict[str, str] = {}
        self._labels_initialized = False
        # Triage cache (read by command processor enrichment)
        self._last_report: Optional[TriageCycleReport] = None
        self._last_report_at: float = 0.0  # monotonic
        self._triaged_emails: Dict[str, TriagedEmail] = {}
        self._report_lock = asyncio.Lock()
        self._triage_schema_version: str = "1.0"

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

        # Resolve dependencies (lazy, with backoff)
        await self._resolver.resolve_all()

        # Ensure labels exist
        workspace_agent = self._resolver.get("workspace_agent")
        if not self._labels_initialized and workspace_agent:
            try:
                gmail_svc = getattr(workspace_agent, "_gmail_service", None)
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
        new_triaged: Dict[str, TriagedEmail] = {}
        for email in emails[: self._config.max_emails_per_cycle]:
            try:
                # Extract features
                features = await extract_features(
                    email, self._resolver.get("router"), config=self._config,
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
                new_triaged[features.message_id] = triaged

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

        report = TriageCycleReport(
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

        # Commit triage snapshot atomically (partial-cycle semantics)
        async with self._report_lock:
            self._last_report = report
            self._last_report_at = time.monotonic()
            self._triaged_emails = new_triaged

        return report

    async def _fetch_unread(self) -> List[Dict[str, Any]]:
        """Fetch unread emails via workspace agent."""
        agent = self._resolver.get("workspace_agent")
        if agent:
            result = await agent._fetch_unread_emails({
                "limit": self._config.max_emails_per_cycle,
            })
            return result.get("emails", [])
        return []

    async def _apply_label(
        self, message_id: str, label_name: str
    ) -> None:
        """Apply Gmail label to message."""
        agent = self._resolver.get("workspace_agent")
        if agent and self._label_map:
            gmail_svc = getattr(agent, "_gmail_service", None)
            if gmail_svc:
                await apply_label(gmail_svc, message_id, label_name, self._label_map)

    def get_fresh_results(
        self, staleness_window_s: Optional[float] = None,
    ) -> Optional[TriageCycleReport]:
        """Return last report if within staleness window, else None."""
        if self._last_report is None:
            return None
        window = (
            staleness_window_s
            if staleness_window_s is not None
            else self._config.staleness_window_s
        )
        age = time.monotonic() - self._last_report_at
        if age > window:
            return None
        return self._last_report
