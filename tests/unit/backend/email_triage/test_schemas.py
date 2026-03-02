"""Tests for email triage data contracts."""

import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "backend"))

from autonomy.email_triage.schemas import (
    EmailFeatures,
    NotificationDeliveryResult,
    ScoringResult,
    TriagedEmail,
    TriageCycleReport,
)


class TestEmailFeatures:
    """EmailFeatures is frozen and contains all extraction fields."""

    def test_construction_with_all_fields(self):
        f = EmailFeatures(
            message_id="abc123",
            sender="alice@example.com",
            sender_domain="example.com",
            subject="Urgent: Q4 report",
            snippet="Please review the attached...",
            is_reply=False,
            has_attachment=True,
            label_ids=("INBOX", "UNREAD"),
            keywords=("urgent", "report"),
            sender_frequency="frequent",
            urgency_signals=("deadline",),
            extraction_confidence=0.95,
        )
        assert f.message_id == "abc123"
        assert f.sender_domain == "example.com"
        assert f.is_reply is False
        assert f.has_attachment is True
        assert f.keywords == ("urgent", "report")

    def test_frozen_immutability(self):
        f = EmailFeatures(
            message_id="abc123",
            sender="alice@example.com",
            sender_domain="example.com",
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
        try:
            f.message_id = "changed"
            assert False, "Should have raised FrozenInstanceError"
        except AttributeError:
            pass

    def test_heuristic_only_features(self):
        """Features with zero AI extraction (confidence=0.0)."""
        f = EmailFeatures(
            message_id="def456",
            sender="bob@unknown.org",
            sender_domain="unknown.org",
            subject="Hello",
            snippet="Hi there",
            is_reply=False,
            has_attachment=False,
            label_ids=("INBOX",),
            keywords=(),
            sender_frequency="first_time",
            urgency_signals=(),
            extraction_confidence=0.0,
        )
        assert f.extraction_confidence == 0.0
        assert f.keywords == ()


class TestScoringResult:
    """ScoringResult is frozen with score, tier, breakdown, and idempotency key."""

    def test_construction(self):
        r = ScoringResult(
            score=87,
            tier=1,
            tier_label="jarvis/tier1_critical",
            breakdown={"sender": 0.9, "content": 0.85, "urgency": 0.8, "context": 0.7},
            idempotency_key="abc123def456",
        )
        assert r.score == 87
        assert r.tier == 1
        assert r.tier_label == "jarvis/tier1_critical"
        assert "sender" in r.breakdown

    def test_frozen(self):
        r = ScoringResult(
            score=50,
            tier=3,
            tier_label="jarvis/tier3_review",
            breakdown={},
            idempotency_key="x",
        )
        try:
            r.score = 99
            assert False, "Should have raised"
        except AttributeError:
            pass


class TestTriagedEmail:
    """TriagedEmail combines features + scoring + notification action."""

    def test_construction(self):
        features = EmailFeatures(
            message_id="m1",
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
        scoring = ScoringResult(
            score=42,
            tier=3,
            tier_label="jarvis/tier3_review",
            breakdown={},
            idempotency_key="k1",
        )
        t = TriagedEmail(
            features=features,
            scoring=scoring,
            notification_action="label_only",
            processed_at=time.time(),
        )
        assert t.notification_action == "label_only"
        assert t.features.message_id == "m1"
        assert t.scoring.tier == 3


class TestTriageCycleReport:
    """TriageCycleReport summarizes a full triage cycle."""

    def test_skipped_cycle(self):
        r = TriageCycleReport(
            cycle_id="c1",
            started_at=time.time(),
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
        assert r.skipped is True
        assert r.skip_reason == "disabled"

    def test_normal_cycle(self):
        r = TriageCycleReport(
            cycle_id="c2",
            started_at=1000.0,
            completed_at=1005.0,
            emails_fetched=10,
            emails_processed=10,
            tier_counts={1: 1, 2: 3, 3: 4, 4: 2},
            notifications_sent=2,
            notifications_suppressed=1,
            errors=[],
        )
        assert r.emails_processed == 10
        assert r.tier_counts[1] == 1
        assert r.skipped is False

    def test_version_fields_default(self):
        """Version fields have sensible defaults for backward compat."""
        r = TriageCycleReport(
            cycle_id="c3",
            started_at=1000.0,
            completed_at=1005.0,
            emails_fetched=5,
            emails_processed=5,
            tier_counts={2: 5},
            notifications_sent=1,
            notifications_suppressed=0,
            errors=[],
        )
        assert r.triage_schema_version == "1.0"
        assert r.policy_version == "v1"

    def test_version_fields_explicit(self):
        """Version fields can be set explicitly."""
        r = TriageCycleReport(
            cycle_id="c4",
            started_at=2000.0,
            completed_at=2010.0,
            emails_fetched=3,
            emails_processed=3,
            tier_counts={1: 1, 4: 2},
            notifications_sent=1,
            notifications_suppressed=0,
            errors=[],
            triage_schema_version="2.0",
            policy_version="v3",
        )
        assert r.triage_schema_version == "2.0"
        assert r.policy_version == "v3"

    def test_existing_defaults_still_work(self):
        """Existing optional fields (skipped, skip_reason) still default correctly."""
        r = TriageCycleReport(
            cycle_id="c5",
            started_at=0.0,
            completed_at=1.0,
            emails_fetched=0,
            emails_processed=0,
            tier_counts={},
            notifications_sent=0,
            notifications_suppressed=0,
            errors=[],
        )
        assert r.skipped is False
        assert r.skip_reason is None
        assert r.triage_schema_version == "1.0"
        assert r.policy_version == "v1"


class TestNotificationDeliveryResult:
    """NotificationDeliveryResult is a frozen dataclass tracking delivery outcomes."""

    def test_successful_delivery(self):
        result = NotificationDeliveryResult(
            message_id="msg_001",
            channel="voice",
            success=True,
            latency_ms=142,
        )
        assert result.message_id == "msg_001"
        assert result.channel == "voice"
        assert result.success is True
        assert result.latency_ms == 142
        assert result.error is None

    def test_failed_delivery_with_error(self):
        result = NotificationDeliveryResult(
            message_id="msg_002",
            channel="websocket",
            success=False,
            latency_ms=5003,
            error="Connection timeout after 5000ms",
        )
        assert result.success is False
        assert result.error == "Connection timeout after 5000ms"
        assert result.latency_ms == 5003

    def test_frozen_immutability(self):
        result = NotificationDeliveryResult(
            message_id="msg_003",
            channel="macos",
            success=True,
            latency_ms=50,
        )
        try:
            result.success = False
            assert False, "Should have raised FrozenInstanceError"
        except AttributeError:
            pass

    def test_all_channel_types(self):
        """All documented channel types can be used."""
        channels = ("voice", "websocket", "macos", "bridge", "summary")
        for ch in channels:
            r = NotificationDeliveryResult(
                message_id=f"msg_{ch}",
                channel=ch,
                success=True,
                latency_ms=10,
            )
            assert r.channel == ch

    def test_equality(self):
        """Frozen dataclasses support value-based equality."""
        a = NotificationDeliveryResult(
            message_id="msg_eq",
            channel="voice",
            success=True,
            latency_ms=100,
        )
        b = NotificationDeliveryResult(
            message_id="msg_eq",
            channel="voice",
            success=True,
            latency_ms=100,
        )
        assert a == b

    def test_inequality_on_different_fields(self):
        a = NotificationDeliveryResult(
            message_id="msg_x",
            channel="voice",
            success=True,
            latency_ms=100,
        )
        b = NotificationDeliveryResult(
            message_id="msg_x",
            channel="voice",
            success=True,
            latency_ms=200,
        )
        assert a != b
