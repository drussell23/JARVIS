"""Tests for email triage notification adapter."""

import asyncio
import os
import sys
import time
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "backend"))

from autonomy.email_triage.notifications import (
    deliver_immediate,
    deliver_summary,
    tier_to_urgency,
)
from autonomy.email_triage.schemas import (
    EmailFeatures,
    ScoringResult,
    TriagedEmail,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_triaged(
    message_id: str = "msg_1",
    sender: str = "alice@example.com",
    subject: str = "Important update",
    tier: int = 1,
    tier_label: str = "jarvis/tier1_critical",
    score: int = 92,
    notification_action: str = "immediate",
) -> TriagedEmail:
    """Build a TriagedEmail fixture with sensible defaults."""
    features = EmailFeatures(
        message_id=message_id,
        sender=sender,
        sender_domain=sender.split("@")[-1],
        subject=subject,
        snippet="Hey, please review this ASAP.",
        is_reply=False,
        has_attachment=False,
        label_ids=("INBOX",),
        keywords=("review",),
        sender_frequency="occasional",
        urgency_signals=("action_required",),
        extraction_confidence=0.95,
    )
    scoring = ScoringResult(
        score=score,
        tier=tier,
        tier_label=tier_label,
        breakdown={"sender": 20.0, "urgency": 30.0},
        idempotency_key="abc123",
    )
    return TriagedEmail(
        features=features,
        scoring=scoring,
        notification_action=notification_action,
        processed_at=time.time(),
    )


# ---------------------------------------------------------------------------
# TestTierToUrgency
# ---------------------------------------------------------------------------


class TestTierToUrgency:
    """tier_to_urgency maps triage tiers to NotificationUrgency integers."""

    def test_tier1_maps_to_urgent(self):
        assert tier_to_urgency(1) == 4

    def test_tier2_maps_to_high(self):
        assert tier_to_urgency(2) == 3

    def test_summary_maps_to_normal(self):
        assert tier_to_urgency(0) == 2

    def test_unknown_tier_maps_to_normal(self):
        assert tier_to_urgency(99) == 2

    def test_tier3_maps_to_normal(self):
        assert tier_to_urgency(3) == 2

    def test_tier4_maps_to_normal(self):
        assert tier_to_urgency(4) == 2

    def test_negative_tier_maps_to_normal(self):
        assert tier_to_urgency(-1) == 2


# ---------------------------------------------------------------------------
# TestDeliverImmediate
# ---------------------------------------------------------------------------


class TestDeliverImmediate:
    """deliver_immediate sends one notification per email via the notifier."""

    @pytest.mark.asyncio
    async def test_delivers_via_notifier(self):
        notifier = AsyncMock(return_value=True)
        email = _make_triaged()
        results = await deliver_immediate([email], notifier, timeout_s=5.0)
        assert len(results) == 1
        assert results[0].success is True
        assert results[0].message_id == "msg_1"
        notifier.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_handles_notifier_failure(self):
        notifier = AsyncMock(side_effect=RuntimeError("bridge down"))
        email = _make_triaged()
        results = await deliver_immediate([email], notifier, timeout_s=5.0)
        assert len(results) == 1
        assert results[0].success is False
        assert results[0].error is not None
        assert "bridge down" in results[0].error

    @pytest.mark.asyncio
    async def test_timeout_returns_failure(self):
        async def slow_notifier(**kwargs):
            await asyncio.sleep(10)
            return True

        email = _make_triaged()
        results = await deliver_immediate([email], slow_notifier, timeout_s=0.1)
        assert len(results) == 1
        assert results[0].success is False
        assert results[0].error is not None

    @pytest.mark.asyncio
    async def test_multiple_emails_parallel(self):
        call_times = []

        async def tracking_notifier(**kwargs):
            call_times.append(time.monotonic())
            await asyncio.sleep(0.05)
            return True

        emails = [_make_triaged(message_id=f"msg_{i}") for i in range(3)]
        results = await deliver_immediate(emails, tracking_notifier, timeout_s=5.0)
        assert len(results) == 3
        assert all(r.success for r in results)
        # All three should be dispatched roughly in parallel
        if len(call_times) == 3:
            assert call_times[-1] - call_times[0] < 0.5

    @pytest.mark.asyncio
    async def test_empty_list_returns_empty(self):
        notifier = AsyncMock(return_value=True)
        results = await deliver_immediate([], notifier, timeout_s=5.0)
        assert results == []
        notifier.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_result_channel_is_bridge(self):
        notifier = AsyncMock(return_value=True)
        email = _make_triaged()
        results = await deliver_immediate([email], notifier, timeout_s=5.0)
        assert results[0].channel == "bridge"

    @pytest.mark.asyncio
    async def test_latency_ms_is_nonnegative(self):
        notifier = AsyncMock(return_value=True)
        email = _make_triaged()
        results = await deliver_immediate([email], notifier, timeout_s=5.0)
        assert results[0].latency_ms >= 0

    @pytest.mark.asyncio
    async def test_notifier_returns_false_is_failure(self):
        notifier = AsyncMock(return_value=False)
        email = _make_triaged()
        results = await deliver_immediate([email], notifier, timeout_s=5.0)
        assert results[0].success is False

    @pytest.mark.asyncio
    async def test_sync_notifier_supported(self):
        """A sync callable should work without crashing."""
        def sync_notifier(**kwargs):
            return True

        email = _make_triaged()
        results = await deliver_immediate([email], sync_notifier, timeout_s=5.0)
        assert len(results) == 1
        assert results[0].success is True

    @pytest.mark.asyncio
    async def test_passes_urgency_to_notifier(self):
        captured = {}

        async def capture_notifier(**kwargs):
            captured.update(kwargs)
            return True

        email = _make_triaged(tier=1)
        await deliver_immediate([email], capture_notifier, timeout_s=5.0)
        assert captured.get("urgency") == 4  # tier 1 -> URGENT

    @pytest.mark.asyncio
    async def test_passes_title_and_message_to_notifier(self):
        captured = {}

        async def capture_notifier(**kwargs):
            captured.update(kwargs)
            return True

        email = _make_triaged(subject="Deadline tomorrow")
        await deliver_immediate([email], capture_notifier, timeout_s=5.0)
        assert "title" in captured
        assert "message" in captured
        assert "Deadline tomorrow" in captured["message"]


# ---------------------------------------------------------------------------
# TestDeliverSummary
# ---------------------------------------------------------------------------


class TestDeliverSummary:
    """deliver_summary sends a single digest notification."""

    @pytest.mark.asyncio
    async def test_delivers_summary(self):
        notifier = AsyncMock(return_value=True)
        emails = [_make_triaged(message_id=f"msg_{i}") for i in range(3)]
        result = await deliver_summary(emails, notifier, timeout_s=5.0)
        assert result.success is True
        assert result.channel == "summary"
        notifier.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_empty_buffer_returns_success(self):
        notifier = AsyncMock(return_value=True)
        result = await deliver_summary([], notifier, timeout_s=5.0)
        assert result.success is True
        assert result.message_id == "summary_empty"
        notifier.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_summary_timeout_returns_failure(self):
        async def slow_notifier(**kwargs):
            await asyncio.sleep(10)
            return True

        emails = [_make_triaged()]
        result = await deliver_summary(emails, slow_notifier, timeout_s=0.1)
        assert result.success is False
        assert result.error is not None

    @pytest.mark.asyncio
    async def test_summary_notifier_failure(self):
        notifier = AsyncMock(side_effect=RuntimeError("bridge down"))
        emails = [_make_triaged()]
        result = await deliver_summary(emails, notifier, timeout_s=5.0)
        assert result.success is False
        assert "bridge down" in result.error

    @pytest.mark.asyncio
    async def test_summary_message_contains_count(self):
        captured = {}

        async def capture_notifier(**kwargs):
            captured.update(kwargs)
            return True

        emails = [_make_triaged(message_id=f"msg_{i}") for i in range(5)]
        await deliver_summary(emails, capture_notifier, timeout_s=5.0)
        assert "5" in captured.get("message", "")

    @pytest.mark.asyncio
    async def test_summary_urgency_is_normal(self):
        captured = {}

        async def capture_notifier(**kwargs):
            captured.update(kwargs)
            return True

        emails = [_make_triaged()]
        await deliver_summary(emails, capture_notifier, timeout_s=5.0)
        assert captured.get("urgency") == 2  # NORMAL

    @pytest.mark.asyncio
    async def test_summary_notifier_returns_false_is_failure(self):
        notifier = AsyncMock(return_value=False)
        emails = [_make_triaged()]
        result = await deliver_summary(emails, notifier, timeout_s=5.0)
        assert result.success is False

    @pytest.mark.asyncio
    async def test_sync_notifier_supported(self):
        def sync_notifier(**kwargs):
            return True

        emails = [_make_triaged()]
        result = await deliver_summary(emails, sync_notifier, timeout_s=5.0)
        assert result.success is True
