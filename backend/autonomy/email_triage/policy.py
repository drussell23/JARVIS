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
from typing import Dict, List, Optional

from autonomy.email_triage.config import TriageConfig
from autonomy.email_triage.schemas import TriagedEmail

logger = logging.getLogger("jarvis.email_triage.policy")


def _current_hour() -> int:
    """Get current local hour (0-23). Extracted for testability."""
    return datetime.now().hour


class NotificationPolicy:
    """Stateful notification policy engine."""

    def __init__(self, config: TriageConfig):
        self._config = config
        self._dedup_cache: Dict[str, float] = {}
        self._interrupt_timestamps: List[float] = []
        self._summary_buffer: List[TriagedEmail] = []
        self._last_summary_flush: float = time.time()

    @property
    def summary_buffer(self) -> List[TriagedEmail]:
        return self._summary_buffer

    def decide_action(self, triaged: TriagedEmail) -> str:
        """Decide notification action for a triaged email.

        Returns: "immediate" | "summary" | "label_only" | "quarantine"
        """
        tier = triaged.scoring.tier
        idem_key = triaged.scoring.idempotency_key

        # Tier 3: always label only
        if tier == 3:
            return "label_only"

        # Tier 4: quarantine or label only
        if tier == 4:
            return "quarantine" if self._config.quarantine_tier4 else "label_only"

        # Tier 1: check if notifications enabled
        if tier == 1 and not self._config.notify_tier1:
            return "label_only"

        # Tier 2: check if notifications enabled
        if tier == 2 and not self._config.notify_tier2:
            return "label_only"

        # Dedup check
        if self._is_duplicate(tier, idem_key):
            return "label_only"

        # Quiet hours: suppress tier2, allow tier1
        if tier >= 2 and self._in_quiet_hours():
            return "label_only"

        # Budget check: tier1 can exceed budget only by escalation
        if tier == 1:
            if self._budget_allows():
                self._record_interrupt()
                self._dedup_record(idem_key)
                return "immediate"
            else:
                self._summary_buffer.append(triaged)
                self._dedup_record(idem_key)
                return "summary"

        # Tier 2 -> summary
        if tier == 2:
            self._summary_buffer.append(triaged)
            self._dedup_record(idem_key)
            return "summary"

        return "label_only"

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

    def _budget_allows(self) -> bool:
        """Check if interrupt budget allows another notification."""
        now = time.time()
        hour_ago = now - 3600
        day_ago = now - 86400
        self._interrupt_timestamps = [
            t for t in self._interrupt_timestamps if t > day_ago
        ]
        hour_count = sum(1 for t in self._interrupt_timestamps if t > hour_ago)
        day_count = len(self._interrupt_timestamps)

        return (
            hour_count < self._config.max_interrupts_per_hour
            and day_count < self._config.max_interrupts_per_day
        )

    def _record_interrupt(self) -> None:
        """Record an interrupt for budget tracking."""
        self._interrupt_timestamps.append(time.time())
