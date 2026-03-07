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
import math
import time
from dataclasses import replace
from typing import Any, ClassVar, Dict, List, Optional, Tuple
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
from autonomy.email_triage.notifications import deliver_immediate, deliver_summary, replay_outbox
from autonomy.email_triage.scoring import score_email
from autonomy.email_triage.state_store import TriageStateStore
from autonomy.email_triage.outcome_collector import OutcomeCollector
from autonomy.email_triage.weight_adapter import WeightAdapter

# Phase B: Decision-action bridge contracts
from core.contracts.decision_envelope import (
    DecisionType, DecisionSource, OriginComponent,
    EnvelopeFactory, IdempotencyKey,
)
from core.contracts.action_commit_ledger import ActionCommitLedger
from core.contracts.policy_context import PolicyContext
from core.contracts.policy_gate import VerdictAction
from autonomy.contracts.behavioral_health import (
    BehavioralHealthMonitor, ThrottleRecommendation,
)
from autonomy.email_triage.triage_policy_gate import TriagePolicyGate

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
        # Reactor-Core feedback loop (WS5)
        self._outcome_collector: Optional[OutcomeCollector] = None
        self._weight_adapter: Optional[WeightAdapter] = None
        self._prior_triaged: Dict[str, TriagedEmail] = {}
        # v283.0: Warm-up tracking
        self._warmed_up = False
        # C2: Extraction latency tracking for adaptive admission
        self._extraction_latencies_ms: List[float] = []
        self._extraction_p95_ema_ms: float = 0.0
        # Phase B: Decision-action bridge
        self._envelope_factory = EnvelopeFactory()
        self._health_monitor = BehavioralHealthMonitor()
        self._commit_ledger: Optional[ActionCommitLedger] = None
        self._policy_gate = TriagePolicyGate(self._policy, self._config)
        self._runner_id = f"runner-{uuid4().hex[:8]}"
        if self._config.outcome_collection_enabled:
            self._outcome_collector = OutcomeCollector(self._config, state_store)
        if self._config.adaptive_scoring_enabled:
            self._weight_adapter = WeightAdapter(self._config)

    @classmethod
    def get_instance(cls, **kwargs) -> EmailTriageRunner:
        if cls._instance is None:
            cls._instance = cls(**kwargs)
        return cls._instance

    @classmethod
    def get_instance_safe(cls) -> Optional[EmailTriageRunner]:
        """Return the singleton if it exists, else None. Never creates."""
        return cls._instance

    @property
    def is_warmed_up(self) -> bool:
        """True after warm_up() has completed at least once."""
        return self._warmed_up

    async def warm_up(self) -> None:
        """Pre-warm one-time dependencies outside the per-cycle timeout.

        v283.0: Cold-start init (dependency resolution, state store, recovery)
        is a one-time cost that should NOT eat into the recurring 30s per-cycle
        budget.  Callers should ``await runner.warm_up()`` once before the first
        ``run_cycle()`` invocation.

        Idempotent — subsequent calls are no-ops.
        """
        if self._warmed_up:
            return
        self._warmed_up = True

        await self._resolver.resolve_all()
        await self._ensure_state_store()
        await self._cold_start_recovery()
        # Phase B: Initialize action commit ledger
        if self._config.state_persistence_enabled:
            try:
                from pathlib import Path
                parent = Path(self._config.state_db_path).parent if self._config.state_db_path else Path.home() / ".jarvis"
                parent.mkdir(parents=True, exist_ok=True)
                ledger_path = parent / "action_commits.db"
                self._commit_ledger = ActionCommitLedger(ledger_path)
                await self._commit_ledger.start()
                expired = await self._commit_ledger.expire_stale()
                if expired > 0:
                    logger.info("Expired %d stale ledger reservations from prior session", expired)
            except Exception as e:
                logger.warning("Action commit ledger init failed: %s", e)
                self._commit_ledger = None
        logger.info("[EmailTriageRunner] Warm-up complete")

    def set_fencing_token(self, token: int) -> None:
        """Set the current fencing token from the DLM (WS2)."""
        self._current_fencing_token = token

    # ── C2: Throughput hardening ──────────────────────────────

    def _record_extraction_latency(self, latency_ms: float) -> None:
        """Record a single extraction latency and update the EMA p95 estimate."""
        self._extraction_latencies_ms.append(latency_ms)
        # Keep last 100 observations for percentile computation
        if len(self._extraction_latencies_ms) > 100:
            self._extraction_latencies_ms = self._extraction_latencies_ms[-100:]
        # Compute actual p95 from recent observations
        sorted_lats = sorted(self._extraction_latencies_ms)
        idx = max(0, int(len(sorted_lats) * 0.95) - 1)
        observed_p95 = sorted_lats[idx]
        # EMA smooth to dampen outliers
        alpha = self._config.latency_ema_alpha
        if self._extraction_p95_ema_ms <= 0:
            self._extraction_p95_ema_ms = observed_p95
        else:
            self._extraction_p95_ema_ms = alpha * observed_p95 + (1 - alpha) * self._extraction_p95_ema_ms

    def _compute_budget(self, email_count: int) -> Tuple[int, float]:
        """Compute how many emails fit within the cycle budget.

        Returns (admitted_count, required_timeout_s).

        Formula:
            required = ceil(admitted / concurrency) * p95_latency + overhead

        If adaptive_admission is enabled and required > cycle_timeout,
        admitted_count is shrunk to fit.
        """
        concurrency = max(1, self._config.extraction_concurrency)
        overhead = self._config.extraction_fixed_overhead_s
        # Use observed p95 or per-email timeout as estimate
        p95_s = (self._extraction_p95_ema_ms / 1000.0) if self._extraction_p95_ema_ms > 0 else self._config.extraction_per_email_timeout_s
        budget = self._config.cycle_timeout_s

        if not self._config.adaptive_admission:
            admitted = min(email_count, self._config.max_emails_per_cycle)
            required = math.ceil(admitted / concurrency) * p95_s + overhead
            return admitted, required

        # Binary search: largest admitted that fits budget
        max_admit = min(email_count, self._config.max_emails_per_cycle)
        for admitted in range(max_admit, 0, -1):
            required = math.ceil(admitted / concurrency) * p95_s + overhead
            if required <= budget:
                return admitted, required

        # Even 1 email doesn't fit — admit 1 anyway but log the overrun
        required = math.ceil(1 / concurrency) * p95_s + overhead
        return min(1, email_count), required

    @staticmethod
    def _urgency_sort_key(email: Dict[str, Any]) -> int:
        """Sort key for urgent-first extraction ordering.

        Lower value = higher priority (processed first).
        """
        subject = (email.get("subject") or "").lower()
        snippet = (email.get("snippet") or "").lower()
        labels = email.get("labels") or []
        text = f"{subject} {snippet}"

        score = 100  # default: low priority
        if "IMPORTANT" in labels:
            score -= 20
        urgent_terms = ("urgent", "critical", "asap", "emergency", "deadline",
                        "action required", "time-sensitive", "due today")
        for term in urgent_terms:
            if term in text:
                score -= 30
                break
        noise_labels = ("CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_FORUMS")
        if any(nl in labels for nl in noise_labels):
            score += 30
        return score

    async def _extract_one(
        self,
        email: Dict[str, Any],
        router: Any,
        deadline: float,
        sem: asyncio.Semaphore,
    ) -> Tuple[Dict[str, Any], Any, float]:
        """Extract features for one email, bounded by semaphore and per-email timeout.

        Returns (email, features_or_None, latency_ms).
        """
        per_email_timeout = self._config.extraction_per_email_timeout_s
        # Clamp to remaining deadline
        remaining = max(1.0, deadline - time.monotonic())
        effective_timeout = min(per_email_timeout, remaining)

        async with sem:
            start = time.monotonic()
            try:
                features = await asyncio.wait_for(
                    extract_features(
                        email, router,
                        deadline=time.monotonic() + effective_timeout,
                        config=self._config,
                    ),
                    timeout=effective_timeout,
                )
                latency_ms = (time.monotonic() - start) * 1000
                return email, features, latency_ms
            except asyncio.TimeoutError:
                latency_ms = (time.monotonic() - start) * 1000
                logger.warning(
                    "Extraction timeout for %s (%.0fms > %.0fs limit)",
                    email.get("id", "?"), latency_ms, effective_timeout,
                )
                return email, None, latency_ms
            except Exception as e:
                latency_ms = (time.monotonic() - start) * 1000
                logger.warning("Extraction failed for %s: %s", email.get("id", "?"), e)
                return email, None, latency_ms

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

    async def run_cycle(self, *, deadline: Optional[float] = None) -> TriageCycleReport:
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

        # Replay undelivered outbox entries (WS6)
        if (
            self._state_store is not None
            and self._config.outbox_replay_on_start
            and not getattr(self, "_outbox_replayed", False)
        ):
            self._outbox_replayed = True
            notifier_for_replay = self._resolver.get("notifier")
            try:
                replay_stats = await replay_outbox(
                    self._state_store,
                    notifier_for_replay,
                    self._config,
                    budget_s=self._config.notification_budget_s,
                )
                if any(v > 0 for v in replay_stats.values()):
                    logger.info("Outbox replay: %s", replay_stats)
            except Exception as e:
                logger.warning("Outbox replay failed: %s", e)

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

        # Phase B: Behavioral health throttle check
        rec, throttle_reason = self._health_monitor.should_throttle()
        if rec == ThrottleRecommendation.CIRCUIT_BREAK:
            return TriageCycleReport(
                cycle_id=cycle_id, started_at=started_at,
                completed_at=time.time(), emails_fetched=0,
                emails_processed=0, tier_counts={},
                notifications_sent=0, notifications_suppressed=0,
                errors=[], skipped=True,
                skip_reason=f"circuit_break:{throttle_reason}",
            )
        if rec == ThrottleRecommendation.PAUSE_CYCLE:
            return TriageCycleReport(
                cycle_id=cycle_id, started_at=started_at,
                completed_at=time.time(), emails_fetched=0,
                emails_processed=0, tier_counts={},
                notifications_sent=0, notifications_suppressed=0,
                errors=[], skipped=True,
                skip_reason=f"pause:{throttle_reason}",
            )
        # REDUCE_BATCH: get recommended max for admission gate
        _health_report = self._health_monitor.check_health()
        _health_max_emails = _health_report.recommended_max_emails

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

        # Outcome collection for prior cycle (WS5)
        if self._outcome_collector and self._prior_triaged:
            try:
                await self._outcome_collector.check_outcomes_for_cycle(
                    workspace_agent, self._prior_triaged,
                )
            except Exception as e:
                logger.debug("Outcome collection failed: %s", e)

        # Compute adaptive weights for this cycle (WS5)
        adaptive_weights = None
        if self._weight_adapter:
            # Feed adaptation-eligible outcomes from collector
            if self._outcome_collector:
                for rec in self._outcome_collector.get_adaptation_outcomes():
                    self._weight_adapter.record_outcome(rec)
                self._outcome_collector.clear()

            try:
                await self._weight_adapter.compute_adapted_weights(self._state_store)
            except Exception as e:
                logger.debug("Weight adaptation failed: %s", e)

            # Advance shadow cycle and get weights
            self._weight_adapter.advance_shadow_cycle()
            adaptive_weights = self._weight_adapter.get_weights_for_scoring()

        # ── C2: Urgent-first ordering ──────────────────────────
        sorted_emails = sorted(emails, key=self._urgency_sort_key)

        # ── C2: Adaptive admission ─────────────────────────────
        admitted_count, budget_required_s = self._compute_budget(len(sorted_emails))
        admitted_emails = sorted_emails[:admitted_count]
        # Phase B: Apply backpressure from health monitor
        if _health_max_emails is not None:
            effective_max = max(1, min(admitted_count, _health_max_emails))
            if effective_max < admitted_count:
                logger.info("Health backpressure: reducing batch %d -> %d", admitted_count, effective_max)
                admitted_count = effective_max
                admitted_emails = sorted_emails[:admitted_count]
        if len(sorted_emails) > admitted_count:
            logger.info(
                "Adaptive admission: %d/%d emails admitted (p95=%.0fms, budget=%.0fs, required=%.0fs)",
                admitted_count, len(sorted_emails),
                self._extraction_p95_ema_ms, self._config.cycle_timeout_s, budget_required_s,
            )

        # ── C2: Concurrent extraction with bounded concurrency ─
        stage_start = time.monotonic()
        router = self._resolver.get("router")
        sem = asyncio.Semaphore(max(1, self._config.extraction_concurrency))
        extraction_deadline = deadline or (time.monotonic() + self._config.cycle_timeout_s)

        extraction_tasks = [
            self._extract_one(email, router, extraction_deadline, sem)
            for email in admitted_emails
        ]
        extraction_results = await asyncio.gather(*extraction_tasks, return_exceptions=True)
        extract_elapsed_ms = (time.monotonic() - stage_start) * 1000

        # Record per-email latencies for adaptive admission
        extraction_latencies: List[float] = []
        for result in extraction_results:
            if isinstance(result, tuple) and len(result) == 3:
                _, _, lat_ms = result
                if lat_ms > 0:
                    extraction_latencies.append(lat_ms)
                    self._record_extraction_latency(lat_ms)

        # ── Process extraction results: score, label, notify ───
        stage_score_start = time.monotonic()
        new_triaged: Dict[str, TriagedEmail] = {}
        immediate_emails: List[TriagedEmail] = []

        cycle_envelopes: list = []  # Phase B: collect envelopes for health monitor

        for result in extraction_results:
            if isinstance(result, BaseException):
                errors.append(f"extract_gather: {result}")
                emails_processed += 1
                continue

            email, features, _lat_ms = result

            if features is None:
                # Extraction failed/timed out — use heuristic fallback
                from autonomy.email_triage.extraction import _heuristic_features
                features = _heuristic_features(email)
                errors.append(f"extract_fallback:{email.get('id', '?')}")

            try:
                # === ENVELOPE: Extraction ===
                source_enum = _map_extraction_source(features.extraction_source)
                extraction_envelope = self._envelope_factory.create(
                    trace_id=cycle_id,
                    decision_type=DecisionType.EXTRACTION,
                    source=source_enum,
                    origin_component=OriginComponent.EMAIL_TRIAGE_EXTRACTION,
                    payload={
                        "message_id": features.message_id,
                        "extraction_source": features.extraction_source,
                        "extraction_confidence": features.extraction_confidence,
                    },
                    confidence=features.extraction_confidence,
                    config_version=self._triage_schema_version,
                )
                cycle_envelopes.append(extraction_envelope)

                # Get sender reputation bonus (WS5)
                rep_bonus = 0.0
                if self._weight_adapter and self._state_store:
                    try:
                        rep_bonus = await self._weight_adapter.get_sender_reputation_bonus(
                            features.sender_domain, self._state_store,
                        )
                    except Exception:
                        pass

                # Score (with optional adaptive weights)
                scoring = score_email(
                    features, self._config,
                    adaptive_weights=adaptive_weights,
                    sender_reputation_bonus=rep_bonus,
                )

                # === ENVELOPE: Scoring ===
                scoring_envelope = self._envelope_factory.create(
                    trace_id=cycle_id,
                    decision_type=DecisionType.SCORING,
                    source=DecisionSource.HEURISTIC,
                    origin_component=OriginComponent.EMAIL_TRIAGE_SCORING,
                    payload={
                        "message_id": features.message_id,
                        "score": scoring.score,
                        "tier": scoring.tier,
                    },
                    confidence=1.0,
                    config_version=self._config.scoring_version,
                    parent_envelope_id=extraction_envelope.envelope_id,
                )
                cycle_envelopes.append(scoring_envelope)

                # === POLICY GATE (replaces direct decide_action) ===
                policy_context = PolicyContext(
                    tier=scoring.tier, score=scoring.score,
                    message_id=features.message_id,
                    sender_domain=features.sender_domain,
                    is_reply=features.is_reply,
                    has_attachment=features.has_attachment,
                    label_ids=features.label_ids,
                    cycle_id=cycle_id,
                    fencing_token=self._current_fencing_token,
                    config_version=self._config.scoring_version,
                )
                verdict = await self._policy_gate.evaluate(scoring_envelope, policy_context)

                # Derive action from verdict (backwards compat with existing flow)
                action = verdict.reason  # TriagePolicyGate puts action name in reason

                # === LEDGER: Reserve -> Pre-exec -> Execute -> Commit/Abort ===
                idem_key = IdempotencyKey.build(
                    DecisionType.ACTION, features.message_id,
                    "triage", self._config.scoring_version,
                )

                commit_id = None
                if self._commit_ledger:
                    # Duplicate check
                    if await self._commit_ledger.is_duplicate(idem_key):
                        errors.append(f"duplicate:{features.message_id}")
                        emails_processed += 1
                        continue

                    # Reserve (MANDATORY for write actions — fail-closed)
                    try:
                        commit_id = await self._commit_ledger.reserve(
                            envelope=scoring_envelope,
                            action="triage",
                            target_id=features.message_id,
                            fencing_token=self._current_fencing_token,
                            lock_owner=self._runner_id,
                            session_id=cycle_id,
                            idempotency_key=idem_key,
                            lease_duration_s=self._config.ledger_lease_duration_s,
                        )
                    except Exception as e:
                        # FAIL CLOSED: deny action on reserve failure
                        errors.append(f"ledger_reserve:{features.message_id}:{e}")
                        emails_processed += 1
                        continue

                    # Pre-exec invariant check
                    ok, inv_reason = await self._commit_ledger.check_pre_exec_invariants(
                        commit_id, self._current_fencing_token,
                    )
                    if not ok:
                        await self._commit_ledger.abort(commit_id, inv_reason or "invariant_failed")
                        errors.append(f"pre_exec:{features.message_id}:{inv_reason}")
                        emails_processed += 1
                        continue

                # === EXECUTE: Label (existing logic) ===
                action_succeeded = True
                try:
                    await self._apply_label(
                        email.get("id", ""),
                        scoring.tier_label,
                    )
                except Exception as label_err:
                    action_succeeded = False
                    errors.append(f"label:{email.get('id', '?')}: {label_err}")

                # === LEDGER: Commit or Abort ===
                if commit_id and self._commit_ledger:
                    try:
                        if action_succeeded:
                            await self._commit_ledger.commit(
                                commit_id, outcome="success",
                                metadata={"tier": scoring.tier, "action": action},
                            )
                        else:
                            await self._commit_ledger.abort(commit_id, "action_failed")
                    except Exception as e:
                        logger.warning("Ledger commit/abort failed for %s: %s",
                                      features.message_id, e)

                # Decide notification (using verdict action)
                triaged = TriagedEmail(
                    features=features,
                    scoring=scoring,
                    notification_action="",
                    processed_at=time.time(),
                )
                triaged.notification_action = action
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
                emails_processed += 1
                emit_triage_event(EVENT_TRIAGE_ERROR, {
                    "cycle_id": cycle_id,
                    "error_type": "process_failed",
                    "message_id": email.get("id", ""),
                    "message": str(e),
                })

        score_elapsed_ms = (time.monotonic() - stage_score_start) * 1000

        # Deliver notifications (side-effect: failure never changes triage outcome)
        notifier = self._resolver.get("notifier")
        if notifier:
            if immediate_emails:
                # Enqueue to outbox before delivery (WS6)
                outbox_ids: Dict[str, int] = {}  # message_id -> outbox row id
                if self._state_store is not None:
                    for em in immediate_emails:
                        try:
                            oid = await self._state_store.enqueue_notification(
                                message_id=em.features.message_id,
                                action="immediate",
                                tier=em.scoring.tier,
                                sender_domain=em.features.sender_domain,
                                expires_at=time.time() + 2 * self._config.summary_interval_s,
                            )
                            if oid is not None:
                                outbox_ids[em.features.message_id] = oid
                        except Exception as e:
                            logger.debug("Outbox enqueue failed for %s: %s",
                                         em.features.message_id, e)

                try:
                    delivery_results = await deliver_immediate(
                        immediate_emails, notifier, self._config.notification_budget_s,
                    )
                    notifications_sent = sum(1 for r in delivery_results if r.success)

                    # Mark delivered in outbox (WS6)
                    if self._state_store is not None:
                        for dr in delivery_results:
                            oid = outbox_ids.get(dr.message_id)
                            if oid is not None and dr.success:
                                try:
                                    await self._state_store.mark_delivered(oid)
                                except Exception:
                                    pass
                            elif oid is not None and not dr.success:
                                try:
                                    await self._state_store.increment_outbox_attempts(oid)
                                except Exception:
                                    pass
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
        total_duration_ms = (completed_at - started_at) * 1000
        fetch_elapsed_ms = (stage_start - started_at) * 1000  # fetch was before extraction

        # C2: Stage-level latency histograms
        stage_latencies = {
            "fetch_ms": round(fetch_elapsed_ms, 1),
            "extract_ms": round(extract_elapsed_ms, 1),
            "score_label_ms": round(score_elapsed_ms, 1),
            "total_ms": round(total_duration_ms, 1),
        }
        if extraction_latencies:
            sorted_lats = sorted(extraction_latencies)
            stage_latencies["extract_p50_ms"] = round(sorted_lats[len(sorted_lats) // 2], 1)
            stage_latencies["extract_p95_ms"] = round(sorted_lats[max(0, int(len(sorted_lats) * 0.95) - 1)], 1)
            stage_latencies["extract_max_ms"] = round(sorted_lats[-1], 1)

        emit_triage_event(EVENT_CYCLE_COMPLETED, {
            "cycle_id": cycle_id,
            "duration_ms": int(total_duration_ms),
            "emails_fetched": emails_fetched,
            "emails_processed": emails_processed,
            "admitted": admitted_count,
            "tier_counts": tier_counts,
            "notifications_sent": notifications_sent,
            "errors": len(errors),
            "stage_latencies": stage_latencies,
            "extraction_p95_ema_ms": round(self._extraction_p95_ema_ms, 1),
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
            stage_latencies_ms=stage_latencies,
            extraction_p95_ms=self._extraction_p95_ema_ms,
            admitted_count=admitted_count,
            budget_computed_s=budget_required_s,
        )

        # Phase B: Record cycle in behavioral health monitor
        self._health_monitor.record_cycle(report, cycle_envelopes)

        # Phase B: Expire stale ledger reservations
        if self._commit_ledger:
            try:
                await self._commit_ledger.expire_stale()
            except Exception:
                pass

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

        # Save triaged emails for next cycle's outcome collection (WS5)
        if new_triaged:
            self._prior_triaged = dict(new_triaged)

        return report

    async def _fetch_unread(self) -> List[Dict[str, Any]]:
        """Fetch unread emails via workspace agent."""
        agent = self._resolver.get("workspace_agent")
        if agent:
            result = await agent._fetch_unread_emails({
                "limit": self._config.max_emails_per_cycle,
            })
            return result.get("emails", [])
        # v284.0: Log when workspace agent is not resolved with dep health
        logger.warning(
            "[EmailTriage] workspace_agent not resolved — cannot fetch unread emails. "
            "dep_health=%s",
            self._resolver.health_summary() if hasattr(self._resolver, "health_summary") else "N/A",
        )
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


def _map_extraction_source(source_str: str) -> DecisionSource:
    """Map extraction source string to DecisionSource enum."""
    _SOURCE_MAP = {
        "heuristic": DecisionSource.HEURISTIC,
        "jprime_v1": DecisionSource.JPRIME_V1,
        "jprime_degraded_fallback": DecisionSource.JPRIME_DEGRADED,
    }
    return _SOURCE_MAP.get(source_str, DecisionSource.HEURISTIC)
