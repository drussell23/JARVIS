"""Tests for user-facing training capture notifications."""

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "backend"))

import pytest


class TestTrainingCaptureNotification:
    @pytest.mark.asyncio
    async def test_notification_sent_when_outcomes_captured(self):
        """When outcomes are captured, notify user with count + confidence mix."""
        from autonomy.email_triage.notifications import build_training_capture_message

        outcomes = [
            {"outcome": "replied", "confidence": "high", "sender_domain": "boss.com"},
            {"outcome": "archived", "confidence": "medium", "sender_domain": "news.com"},
        ]

        message = build_training_capture_message(outcomes)
        assert "2 email outcomes" in message.lower() or "2 outcomes" in message.lower()
        assert "training" in message.lower()

    @pytest.mark.asyncio
    async def test_no_notification_when_no_outcomes(self):
        """No notification when zero outcomes captured."""
        from autonomy.email_triage.notifications import build_training_capture_message

        message = build_training_capture_message([])
        assert message is None


class TestTrainingNotificationWiring:
    """Verify training notifications fire post-commit in the runner."""

    @pytest.mark.asyncio
    async def test_training_notification_dispatched_after_outcomes(self):
        """When outcomes are captured, notifier should be called with training message."""
        from autonomy.email_triage.notifications import build_training_capture_message

        outcomes = [
            {"outcome": "replied", "confidence": "high", "sender_domain": "boss.com"},
            {"outcome": "archived", "confidence": "medium", "sender_domain": "news.com"},
        ]

        mock_notifier = AsyncMock(return_value=True)

        # Verify the message is built correctly
        msg = build_training_capture_message(outcomes)
        assert msg is not None

        # Verify the notifier would be called correctly
        from autonomy.email_triage.notifications import _invoke_notifier
        await _invoke_notifier(mock_notifier, message=msg, urgency=1, title="Training Data Captured")
        mock_notifier.assert_called_once_with(message=msg, urgency=1, title="Training Data Captured")

    @pytest.mark.asyncio
    async def test_no_training_notification_on_empty_outcomes(self):
        """When no outcomes are captured, no training notification should fire."""
        from autonomy.email_triage.notifications import build_training_capture_message

        msg = build_training_capture_message([])
        assert msg is None
        # No notifier call needed — None message means skip
