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
