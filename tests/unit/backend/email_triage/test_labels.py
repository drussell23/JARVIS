"""Tests for Gmail label management."""

import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "backend"))

import pytest
from autonomy.email_triage.labels import ensure_labels_exist, apply_label
from autonomy.email_triage.config import TriageConfig


def _mock_gmail_service(existing_labels=None):
    """Create a mock Gmail service with label support."""
    svc = MagicMock()
    labels = existing_labels or []
    svc.users().labels().list(userId="me").execute.return_value = {
        "labels": [{"name": l, "id": f"Label_{i}"} for i, l in enumerate(labels)]
    }
    svc.users().labels().create(userId="me", body=MagicMock()).execute.return_value = {
        "id": "Label_new", "name": "created"
    }
    svc.users().messages().modify(userId="me", id=MagicMock(), body=MagicMock()).execute.return_value = {}
    # Reset call counts accumulated during mock setup
    svc.reset_mock()
    # Re-set return values after reset
    svc.users().labels().list(userId="me").execute.return_value = {
        "labels": [{"name": l, "id": f"Label_{i}"} for i, l in enumerate(labels)]
    }
    svc.users().labels().create(userId="me", body=MagicMock()).execute.return_value = {
        "id": "Label_new", "name": "created"
    }
    svc.users().messages().modify(userId="me", id=MagicMock(), body=MagicMock()).execute.return_value = {}
    svc.reset_mock()
    return svc


class TestEnsureLabelsExist:
    """Label creation is idempotent."""

    @pytest.mark.asyncio
    async def test_creates_missing_labels(self):
        svc = _mock_gmail_service(existing_labels=[])
        config = TriageConfig()
        label_map = await ensure_labels_exist(svc, config)
        assert len(label_map) == 4  # All 4 tiers

    @pytest.mark.asyncio
    async def test_skips_existing_labels(self):
        svc = _mock_gmail_service(existing_labels=[
            "jarvis/tier1_critical", "jarvis/tier2_high",
            "jarvis/tier3_review", "jarvis/tier4_noise",
        ])
        config = TriageConfig()
        label_map = await ensure_labels_exist(svc, config)
        assert len(label_map) == 4
        # create() should NOT have been called for any
        assert svc.users().labels().create.call_count == 0


class TestApplyLabel:
    """Label application is idempotent."""

    @pytest.mark.asyncio
    async def test_applies_label(self):
        svc = _mock_gmail_service()
        label_map = {"jarvis/tier1_critical": "Label_0"}
        await apply_label(svc, "msg_001", "jarvis/tier1_critical", label_map)
        # Should have called modify
        svc.users().messages().modify.assert_called()

    @pytest.mark.asyncio
    async def test_missing_label_in_map_logs_warning(self, caplog):
        import logging
        svc = _mock_gmail_service()
        label_map = {}
        with caplog.at_level(logging.WARNING):
            await apply_label(svc, "msg_001", "jarvis/tier1_critical", label_map)
        assert any("not found in label map" in r.message for r in caplog.records)
