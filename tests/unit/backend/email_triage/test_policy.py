"""Tests for notification policy (quiet hours, dedup, budget, summaries)."""

import os
import sys
import time
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "backend"))

from autonomy.email_triage.config import TriageConfig
from autonomy.email_triage.schemas import EmailFeatures, ScoringResult, TriagedEmail
from autonomy.email_triage.policy import NotificationPolicy


def _make_triaged(tier: int, score: int, msg_id: str = "m1") -> TriagedEmail:
    features = EmailFeatures(
        message_id=msg_id,
        sender="a@b.com",
        sender_domain="b.com",
        subject="Test",
        snippet="",
        is_reply=False,
        has_attachment=False,
        label_ids=(),
        keywords=(),
        sender_frequency="first_time",
        urgency_signals=(),
        extraction_confidence=0.0,
    )
    label_map = {1: "jarvis/tier1_critical", 2: "jarvis/tier2_high",
                 3: "jarvis/tier3_review", 4: "jarvis/tier4_noise"}
    scoring = ScoringResult(
        score=score, tier=tier, tier_label=label_map[tier],
        breakdown={}, idempotency_key=f"key_{msg_id}",
    )
    return TriagedEmail(
        features=features, scoring=scoring,
        notification_action="",
        processed_at=time.time(),
    )


class TestNotificationActions:
    """Policy decides correct action for each tier."""

    def test_tier1_gets_immediate(self):
        config = TriageConfig(notify_tier1=True)
        policy = NotificationPolicy(config)
        t = _make_triaged(tier=1, score=90)
        action, _expl = policy.decide_action(t)
        assert action == "immediate"

    def test_tier2_gets_summary(self):
        config = TriageConfig(notify_tier2=True)
        policy = NotificationPolicy(config)
        t = _make_triaged(tier=2, score=70)
        action, _expl = policy.decide_action(t)
        assert action == "summary"

    def test_tier3_gets_label_only(self):
        config = TriageConfig()
        policy = NotificationPolicy(config)
        t = _make_triaged(tier=3, score=50)
        action, _expl = policy.decide_action(t)
        assert action == "label_only"

    def test_tier4_gets_quarantine_when_enabled(self):
        config = TriageConfig(quarantine_tier4=True)
        policy = NotificationPolicy(config)
        t = _make_triaged(tier=4, score=10)
        action, _expl = policy.decide_action(t)
        assert action == "quarantine"

    def test_tier4_gets_label_only_when_quarantine_disabled(self):
        config = TriageConfig(quarantine_tier4=False)
        policy = NotificationPolicy(config)
        t = _make_triaged(tier=4, score=10)
        action, _expl = policy.decide_action(t)
        assert action == "label_only"

    def test_tier1_disabled_gets_label_only(self):
        config = TriageConfig(notify_tier1=False)
        policy = NotificationPolicy(config)
        t = _make_triaged(tier=1, score=95)
        action, _expl = policy.decide_action(t)
        assert action == "label_only"

    def test_tier2_disabled_gets_label_only(self):
        config = TriageConfig(notify_tier2=False)
        policy = NotificationPolicy(config)
        t = _make_triaged(tier=2, score=70)
        action, _expl = policy.decide_action(t)
        assert action == "label_only"


class TestQuietHours:
    """Quiet hours suppress tier2+ but not tier1."""

    def test_tier2_suppressed_during_quiet(self):
        config = TriageConfig(quiet_start_hour=23, quiet_end_hour=8)
        policy = NotificationPolicy(config)
        with patch("autonomy.email_triage.policy._current_hour", return_value=2):
            t = _make_triaged(tier=2, score=70)
            action, _ = policy.decide_action(t)
            assert action == "label_only"

    def test_tier1_still_notifies_during_quiet(self):
        config = TriageConfig(quiet_start_hour=23, quiet_end_hour=8)
        policy = NotificationPolicy(config)
        with patch("autonomy.email_triage.policy._current_hour", return_value=2):
            t = _make_triaged(tier=1, score=90)
            action, _ = policy.decide_action(t)
            assert action == "immediate"

    def test_not_quiet_at_noon(self):
        config = TriageConfig(quiet_start_hour=23, quiet_end_hour=8)
        policy = NotificationPolicy(config)
        with patch("autonomy.email_triage.policy._current_hour", return_value=12):
            t = _make_triaged(tier=2, score=70)
            action, _ = policy.decide_action(t)
            assert action == "summary"


class TestDedup:
    """Same email not re-notified within dedup window."""

    def test_tier1_dedup_within_15min(self):
        config = TriageConfig(dedup_tier1_s=900)
        policy = NotificationPolicy(config)
        t1 = _make_triaged(tier=1, score=90, msg_id="dup1")
        t2 = _make_triaged(tier=1, score=90, msg_id="dup1")

        action1, _ = policy.decide_action(t1)
        assert action1 == "immediate"

        action2, _ = policy.decide_action(t2)
        assert action2 == "label_only"

    def test_different_messages_not_deduped(self):
        config = TriageConfig()
        policy = NotificationPolicy(config)
        t1 = _make_triaged(tier=1, score=90, msg_id="a")
        t2 = _make_triaged(tier=1, score=90, msg_id="b")

        action1, _ = policy.decide_action(t1)
        action2, _ = policy.decide_action(t2)
        assert action1 == "immediate"
        assert action2 == "immediate"


class TestInterruptBudget:
    """Max interrupts per hour enforced."""

    def test_budget_exhaustion(self):
        config = TriageConfig(max_interrupts_per_hour=2, max_interrupts_per_day=10)
        policy = NotificationPolicy(config)

        for i in range(2):
            t = _make_triaged(tier=1, score=90, msg_id=f"msg_{i}")
            action, _ = policy.decide_action(t)
            assert action == "immediate", f"Message {i} should be immediate"

        t3 = _make_triaged(tier=1, score=90, msg_id="msg_overflow")
        action3, _ = policy.decide_action(t3)
        assert action3 in ("summary", "label_only")


class TestSummaryBuffer:
    """Tier 2 emails buffered for summary delivery."""

    def test_tier2_added_to_summary_buffer(self):
        config = TriageConfig()
        policy = NotificationPolicy(config)
        t = _make_triaged(tier=2, score=70)
        policy.decide_action(t)
        assert len(policy.summary_buffer) == 1

    def test_flush_clears_buffer(self):
        config = TriageConfig()
        policy = NotificationPolicy(config)
        t = _make_triaged(tier=2, score=70)
        policy.decide_action(t)
        assert len(policy.summary_buffer) == 1
        summary = policy.flush_summary()
        assert policy.summary_buffer == []
        assert summary is not None
