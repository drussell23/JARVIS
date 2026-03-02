"""Notification policy for the email triage system.

Implements the full notification spec:
1. Quiet hours (23:00-08:00) — suppress tier2+, tier1 still notifies
2. Dedup windows (15min tier1, 60min tier2) — keyed by idempotency_key
3. Interrupt budget (3/hr, 12/day) — excess queued for summary
4. Summary windows (30min) — batch tier2 emails
5. Feature flag gating — each tier independently toggleable
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

from autonomy.email_triage.config import TriageConfig
from autonomy.email_triage.schemas import PolicyExplanation, TriagedEmail

if TYPE_CHECKING:
    from autonomy.email_triage.state_store import TriageStateStore

logger = logging.getLogger("jarvis.email_triage.policy")


def _current_hour() -> int:
    """Get current local hour (0-23). Extracted for testability."""
    return datetime.now().hour


class NotificationPolicy:
    """Stateful notification policy engine.

    Supports optional durable state via TriageStateStore. When a state_store
    is provided, dedup and budget checks are backed by SQLite. The in-memory
    caches remain as a fast path and fallback.
    """

    def __init__(
        self,
        config: TriageConfig,
        state_store: Optional[TriageStateStore] = None,
    ):
        self._config = config
        self._state_store: Optional[TriageStateStore] = state_store
        self._dedup_cache: Dict[str, float] = {}
        self._interrupt_timestamps: List[float] = []
        self._summary_buffer: List[TriagedEmail] = []
        self._last_summary_flush: float = time.time()

    @property
    def summary_buffer(self) -> List[TriagedEmail]:
        return self._summary_buffer

    def decide_action(self, triaged: TriagedEmail) -> Tuple[str, PolicyExplanation]:
        """Decide notification action for a triaged email.

        Returns: (action, explanation) where action is one of:
            "immediate" | "summary" | "label_only" | "quarantine"
        """
        tier = triaged.scoring.tier
        score = triaged.scoring.score
        idem_key = triaged.scoring.idempotency_key
        reasons: List[str] = []
        suppressed_by: Optional[str] = None
        quiet_active = self._in_quiet_hours()
        budget_hour, budget_day = self._budget_remaining()
        dedup_hit = self._is_duplicate(tier, idem_key)

        def _explain(action: str) -> Tuple[str, PolicyExplanation]:
            return action, PolicyExplanation(
                action=action,
                reasons=tuple(reasons),
                suppressed_by=suppressed_by,
                tier=tier,
                score=score,
                quiet_hours_active=quiet_active,
                budget_remaining_hour=budget_hour,
                budget_remaining_day=budget_day,
                dedup_hit=dedup_hit,
            )

        # Tier 3: always label only
        if tier == 3:
            reasons.append("tier3_review_only")
            return _explain("label_only")

        # Tier 4: quarantine or label only
        if tier == 4:
            if self._config.quarantine_tier4:
                reasons.append("tier4_quarantine_enabled")
                return _explain("quarantine")
            reasons.append("tier4_noise")
            return _explain("label_only")

        # Tier 1: check if notifications enabled
        if tier == 1 and not self._config.notify_tier1:
            reasons.append("tier1_notifications_disabled")
            suppressed_by = "config_disabled"
            return _explain("label_only")

        # Tier 2: check if notifications enabled
        if tier == 2 and not self._config.notify_tier2:
            reasons.append("tier2_notifications_disabled")
            suppressed_by = "config_disabled"
            return _explain("label_only")

        # Dedup check
        if dedup_hit:
            reasons.append("duplicate_within_window")
            suppressed_by = "dedup_window"
            return _explain("label_only")

        # Quiet hours: suppress tier2, allow tier1
        if tier >= 2 and quiet_active:
            reasons.append("quiet_hours_active")
            suppressed_by = "quiet_hours"
            return _explain("label_only")

        # Budget check: tier1 can exceed budget only by escalation
        if tier == 1:
            reasons.append("tier1_critical")
            if self._budget_allows():
                reasons.append("budget_available")
                self._record_interrupt()
                self._dedup_record(idem_key)
                return _explain("immediate")
            else:
                reasons.append("budget_exhausted_to_summary")
                suppressed_by = "budget_exhausted"
                self._summary_buffer.append(triaged)
                self._dedup_record(idem_key)
                return _explain("summary")

        # Tier 2 -> summary
        if tier == 2:
            reasons.append("tier2_to_summary")
            self._summary_buffer.append(triaged)
            self._dedup_record(idem_key)
            return _explain("summary")

        reasons.append("fallback_label_only")
        return _explain("label_only")

    def flush_summary(self) -> Optional[str]:
        """Flush the summary buffer. Returns formatted summary or None if empty."""
        if not self._summary_buffer:
            return None

        lines = []
        for t in self._summary_buffer:
            lines.append(
                f"- [{t.features.subject}] from {t.features.sender} "
                f"(tier {t.scoring.tier}, score {t.scoring.score})"
            )

        summary = f"Email triage summary ({len(self._summary_buffer)} emails):\n"
        summary += "\n".join(lines)

        self._summary_buffer.clear()
        self._last_summary_flush = time.time()
        return summary

    def should_flush_summary(self) -> bool:
        """Check if summary window has elapsed."""
        return (
            len(self._summary_buffer) > 0
            and (time.time() - self._last_summary_flush) >= self._config.summary_interval_s
        )

    def _in_quiet_hours(self) -> bool:
        """Check if current time is within quiet hours."""
        hour = _current_hour()
        start = self._config.quiet_start_hour
        end = self._config.quiet_end_hour
        if start > end:
            return hour >= start or hour < end
        return start <= hour < end

    def _is_duplicate(self, tier: int, idem_key: str) -> bool:
        """Check if this email was already notified within dedup window."""
        if idem_key not in self._dedup_cache:
            return False
        last_time = self._dedup_cache[idem_key]
        window = self._config.dedup_tier1_s if tier == 1 else self._config.dedup_tier2_s
        return (time.time() - last_time) < window

    def _dedup_record(self, idem_key: str) -> None:
        """Record notification for dedup."""
        self._dedup_cache[idem_key] = time.time()

    def _budget_remaining(self) -> Tuple[int, int]:
        """Return (remaining_hour, remaining_day) interrupt budget."""
        now = time.time()
        hour_ago = now - 3600
        day_ago = now - 86400
        self._interrupt_timestamps = [
            t for t in self._interrupt_timestamps if t > day_ago
        ]
        hour_count = sum(1 for t in self._interrupt_timestamps if t > hour_ago)
        day_count = len(self._interrupt_timestamps)
        return (
            max(0, self._config.max_interrupts_per_hour - hour_count),
            max(0, self._config.max_interrupts_per_day - day_count),
        )

    def _budget_allows(self) -> bool:
        """Check if interrupt budget allows another notification."""
        hour_remaining, day_remaining = self._budget_remaining()
        return hour_remaining > 0 and day_remaining > 0

    def _record_interrupt(self) -> None:
        """Record an interrupt for budget tracking."""
        self._interrupt_timestamps.append(time.time())
