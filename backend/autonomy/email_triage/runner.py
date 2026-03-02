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
from dataclasses import replace
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
    EVENT_SNAPSHOT_PRESERVED,
    EVENT_SNAPSHOT_RESTORED,
)
from autonomy.email_triage.extraction import extract_features
from autonomy.email_triage.labels import apply_label, ensure_labels_exist
from autonomy.email_triage.policy import NotificationPolicy
from autonomy.email_triage.schemas import TriageCycleReport, TriagedEmail
from autonomy.email_triage.notifications import deliver_immediate, deliver_summary
from autonomy.email_triage.scoring import score_email
from autonomy.email_triage.state_store import TriageStateStore

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
        state_store: Optional[TriageStateStore] = None,
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
        self._committed_snapshot: Optional[Dict[str, Any]] = None
        # Durable state (WS1)
        self._state_store: Optional[TriageStateStore] = state_store
        self._state_store_initialized = False
        self._cold_start_recovered = False
        # Fencing (WS2)
        self._current_fencing_token: int = 0
        self._last_committed_fencing_token: int = 0

    @classmethod
    def get_instance(cls, **kwargs) -> EmailTriageRunner:
        if cls._instance is None:
            cls._instance = cls(**kwargs)
        return cls._instance

    @classmethod
    def get_instance_safe(cls) -> Optional[EmailTriageRunner]:
        """Return the singleton if it exists, else None. Never creates."""
        return cls._instance

    def set_fencing_token(self, token: int) -> None:
        """Set the current fencing token from the DLM (WS2)."""
        self._current_fencing_token = token

    async def _ensure_state_store(self) -> None:
        """Lazily initialize and open the state store if persistence is enabled."""
        if self._state_store_initialized:
            return
        self._state_store_initialized = True

        if not self._config.state_persistence_enabled:
            self._state_store = None
            return

        if self._state_store is None:
            try:
                self._state_store = TriageStateStore(
                    db_path=self._config.state_db_path,
                )
                await self._state_store.open()
            except Exception as e:
                logger.warning("State store init failed (falling back to in-memory): %s", e)
                self._state_store = None
                return

        if not self._state_store.is_open:
            try:
                await self._state_store.open()
            except Exception as e:
                logger.warning("State store open failed: %s", e)
                self._state_store = None

    async def _cold_start_recovery(self) -> None:
        """Recover state from the durable store on first cycle."""
        if self._cold_start_recovered or self._state_store is None:
            return
        self._cold_start_recovered = True

        try:
            snapshot_data = await self._state_store.load_latest_snapshot()
            if snapshot_data is None:
                return

            # Restore fencing token
            stored_token = snapshot_data.get("fencing_token", 0)
            self._last_committed_fencing_token = stored_token

            # Check if this is a cross-session recovery
            stored_session = snapshot_data.get("session_id", "")
            current_session = self._state_store.session_id
            is_cold_recovery = stored_session != current_session

            committed_at_epoch = snapshot_data["committed_at_epoch"]

            # Rebuild _committed_snapshot from stored data (minimal form)
            # We can't fully restore TriagedEmail objects, but we can restore
            # the snapshot dict that consumers read.
            triaged_min = snapshot_data.get("triaged_emails_min", {})
            report_summary = snapshot_data.get("report_summary", {})

            self._committed_snapshot = {
                "report": None,  # Full report not persisted
                "triaged_emails": {},  # Will be repopulated on next healthy cycle
                "schema_version": self._triage_schema_version,
                "committed_at": committed_at_epoch,
                "restored_from_db": True,
                "cold_recovery": is_cold_recovery,
                "stored_triaged_min": triaged_min,
            }

            emit_triage_event(EVENT_SNAPSHOT_RESTORED, {
                "stored_cycle_id": snapshot_data.get("cycle_id", ""),
                "stored_session_id": stored_session,
                "current_session_id": current_session,
                "cold_recovery": is_cold_recovery,
                "age_s": time.time() - committed_at_epoch,
                "restored_fencing_token": stored_token,
            })
            logger.info(
                "Restored snapshot from DB: cycle=%s, cold=%s, age=%.1fs",
                snapshot_data.get("cycle_id", "?"),
                is_cold_recovery,
                time.time() - committed_at_epoch,
            )
        except Exception as e:
            logger.warning("Cold-start recovery failed: %s", e)

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

        # Initialize state store + cold-start recovery (first cycle only)
        await self._ensure_state_store()
        await self._cold_start_recovery()

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
        immediate_emails: List[TriagedEmail] = []
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
                action, explanation = self._policy.decide_action(triaged)
                triaged.notification_action = action
                triaged.policy_explanation = explanation
                new_triaged[features.message_id] = triaged

                # Track stats
                tier_counts[scoring.tier] = tier_counts.get(scoring.tier, 0) + 1
                if action == "immediate":
                    immediate_emails.append(triaged)
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

        # Deliver notifications (side-effect: failure never changes triage outcome)
        notifier = self._resolver.get("notifier")
        if notifier:
            if immediate_emails:
                try:
                    delivery_results = await deliver_immediate(
                        immediate_emails, notifier, self._config.notification_budget_s,
                    )
                    notifications_sent = sum(1 for r in delivery_results if r.success)
                except Exception as e:
                    logger.warning("Immediate notification delivery failed: %s", e)
                    errors.append(f"notify_immediate: {e}")

            if self._policy.should_flush_summary():
                summary_emails = list(self._policy.summary_buffer)
                self._policy.flush_summary()
                try:
                    await deliver_summary(
                        summary_emails, notifier, self._config.summary_budget_s,
                    )
                except Exception as e:
                    logger.warning("Summary notification delivery failed: %s", e)
                    errors.append(f"notify_summary: {e}")
        else:
            # No notifier — flush summary to prevent unbounded buffer growth
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

        # Commit policy gate — preserve prior snapshot on degraded cycles
        should_commit, commit_reason = self._should_commit_snapshot(report, new_triaged)

        if should_commit:
            committed_at = time.monotonic()
            async with self._report_lock:
                self._last_report = report
                self._last_report_at = committed_at
                self._triaged_emails = new_triaged
                # Pre-built snapshot for GIL-atomic reads (defensive copy)
                self._committed_snapshot = {
                    "report": report,
                    "triaged_emails": dict(new_triaged),
                    "schema_version": self._triage_schema_version,
                    "committed_at": committed_at,
                }
            report = replace(report, snapshot_committed=True)

            # Persist to durable state store (best-effort)
            if self._state_store is not None:
                try:
                    db_committed, db_reason = await self._state_store.save_snapshot(
                        cycle_id=cycle_id,
                        report=report,
                        triaged_emails=new_triaged,
                        fencing_token=self._current_fencing_token,
                    )
                    if db_committed:
                        self._last_committed_fencing_token = self._current_fencing_token
                    else:
                        logger.warning("State store rejected snapshot: %s", db_reason)
                except Exception as e:
                    logger.warning("State store save failed (in-memory still committed): %s", e)

                # Update sender reputation
                try:
                    for triaged in new_triaged.values():
                        await self._state_store.update_sender_reputation(
                            triaged.features.sender_domain,
                            triaged.scoring.tier,
                            triaged.scoring.score,
                        )
                except Exception as e:
                    logger.debug("Sender reputation update failed: %s", e)

                # Run GC
                try:
                    await self._state_store.run_gc(
                        snapshot_retention=self._config.snapshot_retention_count,
                    )
                except Exception as e:
                    logger.debug("State store GC failed: %s", e)
        else:
            report = replace(
                report, degraded=True, degraded_reason=commit_reason,
                snapshot_committed=False,
            )
            prior_id = self._last_report.cycle_id if self._last_report else None
            emit_triage_event(EVENT_SNAPSHOT_PRESERVED, {
                "cycle_id": cycle_id,
                "reason": commit_reason,
                "prior_cycle_id": prior_id,
                "emails_fetched": emails_fetched,
                "emails_processed": emails_processed,
                "error_count": len(errors),
            })
            logger.info(
                "Snapshot preserved (prior %s): reason=%s, fetched=%d, processed=%d, errors=%d",
                prior_id or "none", commit_reason, emails_fetched, emails_processed, len(errors),
            )

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

    def _should_commit_snapshot(
        self,
        report: TriageCycleReport,
        new_triaged: Dict[str, TriagedEmail],
    ) -> tuple:
        """Decide whether this cycle's results should replace the current snapshot.

        Returns (should_commit: bool, reason: str).

        Policy:
        - Skipped cycles never commit.
        - Cold-start with required dep unavailable: don't commit
          (blocker #1: workspace_agent unresolved means we can't trust
          the empty result — distinct from a legit empty inbox).
        - No prior with actual data: always commit (partial beats nothing).
        - Empty triaged when prior had data: regression, don't commit.
        - Error ratio > threshold: degraded, don't commit.
        """
        if report.skipped:
            return False, "skipped"

        has_prior = self._committed_snapshot is not None and len(
            self._committed_snapshot.get("triaged_emails", {})
        ) > 0

        # Cold-start false-truth prevention (blocker #1):
        # Only block when required dependency (workspace_agent) was unavailable.
        # A legit empty inbox (fetch succeeded, 0 emails) IS valid truth.
        if not has_prior and self._resolver.get("workspace_agent") is None:
            return False, "cold_start_dep_unavailable"

        # No prior with actual data => always commit
        if not has_prior:
            return True, "no_prior_snapshot"

        # Error ratio: count processing errors vs fetched.
        # Check BEFORE empty-triaged so "all failed" reports as error_ratio,
        # not as empty_triaged_regression.
        if report.emails_fetched > 0:
            process_errors = sum(1 for e in report.errors if e.startswith("process:"))
            error_ratio = process_errors / report.emails_fetched
            if error_ratio > self._config.commit_error_threshold:
                return False, f"error_ratio:{error_ratio:.2f}"

        # Empty triaged when prior had data => regression (filtering, not errors)
        if len(new_triaged) == 0:
            return False, "empty_triaged_regression"

        return True, "healthy"

    def get_fresh_results(
        self, staleness_window_s: Optional[float] = None,
    ) -> Optional[TriageCycleReport]:
        """Return last report if within staleness window, else None."""
        snapshot = self._committed_snapshot
        if snapshot is None:
            return None
        window = (
            staleness_window_s
            if staleness_window_s is not None
            else self._config.staleness_window_s
        )
        committed_at = snapshot.get("committed_at", 0.0)
        age = time.monotonic() - committed_at
        if age > window:
            return None
        return snapshot.get("report")

    def get_triage_snapshot(
        self,
        staleness_window_s: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """Return an atomic snapshot of the last triage cycle.

        Reads a single pre-built dict reference (GIL-atomic) to prevent
        tearing under concurrent writes from run_cycle().

        Returns defensive copies so callers cannot mutate internal truth.
        """
        snapshot = self._committed_snapshot
        if snapshot is None:
            return None
        window = (
            staleness_window_s
            if staleness_window_s is not None
            else self._config.staleness_window_s
        )
        committed_at = snapshot.get("committed_at", 0.0)
        age = time.monotonic() - committed_at
        if age > window:
            return None
        # Defensive copy: shallow-copy triaged_emails so callers can't mutate
        return {
            "report": snapshot["report"],
            "triaged_emails": dict(snapshot["triaged_emails"]),
            "schema_version": snapshot["schema_version"],
            "committed_at": committed_at,
            "age_s": age,
        }

    def get_triaged_email(self, message_id: str) -> Optional[TriagedEmail]:
        """Return a single triaged email by message ID, or None."""
        snapshot = self._committed_snapshot
        if snapshot is None:
            return None
        return snapshot.get("triaged_emails", {}).get(message_id)
