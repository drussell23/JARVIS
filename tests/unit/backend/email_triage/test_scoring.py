"""Tests for deterministic email scoring engine.

Scoring is pure: same inputs -> same output. No I/O, no randomness.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "backend"))

from autonomy.email_triage.schemas import EmailFeatures
from autonomy.email_triage.config import TriageConfig
from autonomy.email_triage.scoring import score_email


def _make_features(**overrides) -> EmailFeatures:
    """Helper to build EmailFeatures with defaults."""
    defaults = dict(
        message_id="test_msg_001",
        sender="alice@example.com",
        sender_domain="example.com",
        subject="Hello",
        snippet="Just checking in",
        is_reply=False,
        has_attachment=False,
        label_ids=("INBOX", "UNREAD"),
        keywords=(),
        sender_frequency="occasional",
        urgency_signals=(),
        extraction_confidence=0.0,
    )
    defaults.update(overrides)
    for key in ("label_ids", "keywords", "urgency_signals"):
        if isinstance(defaults[key], list):
            defaults[key] = tuple(defaults[key])
    return EmailFeatures(**defaults)


class TestScoreEmailDeterminism:
    """Same inputs must always produce same output."""

    def test_same_inputs_same_score(self):
        config = TriageConfig()
        f = _make_features()
        r1 = score_email(f, config)
        r2 = score_email(f, config)
        assert r1.score == r2.score
        assert r1.tier == r2.tier
        assert r1.idempotency_key == r2.idempotency_key

    def test_different_message_id_different_idempotency_key(self):
        config = TriageConfig()
        f1 = _make_features(message_id="msg_a")
        f2 = _make_features(message_id="msg_b")
        r1 = score_email(f1, config)
        r2 = score_email(f2, config)
        assert r1.idempotency_key != r2.idempotency_key
        assert r1.score == r2.score


class TestTierMapping:
    """Score-to-tier mapping matches spec thresholds."""

    def test_tier1_critical(self):
        config = TriageConfig()
        f = _make_features(
            sender_frequency="frequent",
            keywords=("urgent", "deadline", "action_required"),
            urgency_signals=("deadline", "action_required", "escalation"),
            subject="URGENT: Action Required - Server Down",
            is_reply=True,
            has_attachment=True,
            label_ids=("INBOX", "IMPORTANT"),
        )
        r = score_email(f, config)
        assert r.tier == 1
        assert r.score >= 85
        assert r.tier_label == "jarvis/tier1_critical"

    def test_tier4_noise(self):
        config = TriageConfig()
        f = _make_features(
            sender="noreply@marketing.spam.com",
            sender_domain="spam.com",
            sender_frequency="first_time",
            subject="50% off everything!",
            keywords=("sale", "discount", "unsubscribe"),
            label_ids=("CATEGORY_PROMOTIONS",),
            urgency_signals=(),
        )
        r = score_email(f, config)
        assert r.tier == 4
        assert r.score < 35
        assert r.tier_label == "jarvis/tier4_noise"

    def test_tier2_high(self):
        config = TriageConfig()
        f = _make_features(
            sender_frequency="frequent",
            keywords=("urgent", "deadline"),
            urgency_signals=("deadline",),
            subject="Meeting tomorrow at 10am - deadline approaching",
            is_reply=True,
        )
        r = score_email(f, config)
        assert r.tier in (1, 2)
        assert r.score >= 65

    def test_tier3_review(self):
        config = TriageConfig()
        f = _make_features(
            sender_frequency="occasional",
            keywords=("newsletter",),
            subject="Weekly Team Update",
        )
        r = score_email(f, config)
        assert r.tier in (3, 4)
        assert r.score < 65


class TestScoreBoundaries:
    """Score is always 0-100, tier is always 1-4."""

    def test_score_range(self):
        config = TriageConfig()
        f_low = _make_features(
            sender="x@x.x",
            sender_domain="x.x",
            sender_frequency="first_time",
            subject="",
            snippet="",
            keywords=(),
            urgency_signals=(),
            label_ids=("CATEGORY_PROMOTIONS",),
        )
        r_low = score_email(f_low, config)
        assert 0 <= r_low.score <= 100
        assert 1 <= r_low.tier <= 4

        f_high = _make_features(
            sender_frequency="frequent",
            keywords=("urgent", "critical", "immediate", "action_required"),
            urgency_signals=("deadline", "action_required", "escalation"),
            subject="CRITICAL: Immediate Action Required - Security Breach",
            is_reply=True,
            has_attachment=True,
        )
        r_high = score_email(f_high, config)
        assert 0 <= r_high.score <= 100
        assert 1 <= r_high.tier <= 4


class TestScoringBreakdown:
    """Breakdown dict contains all 4 factors."""

    def test_breakdown_keys(self):
        config = TriageConfig()
        f = _make_features()
        r = score_email(f, config)
        assert "sender" in r.breakdown
        assert "content" in r.breakdown
        assert "urgency" in r.breakdown
        assert "context" in r.breakdown

    def test_breakdown_values_in_range(self):
        config = TriageConfig()
        f = _make_features()
        r = score_email(f, config)
        for factor, val in r.breakdown.items():
            assert 0.0 <= val <= 1.0, f"{factor} out of range: {val}"
