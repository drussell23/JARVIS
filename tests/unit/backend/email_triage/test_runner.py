"""Tests for the email triage runner (full cycle orchestration)."""

import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "backend"))

import pytest
from autonomy.email_triage.runner import EmailTriageRunner
from autonomy.email_triage.config import TriageConfig


def _sample_emails(count=3):
    return [
        {
            "id": f"msg_{i}",
            "thread_id": f"t_{i}",
            "from": f"sender{i}@example.com",
            "to": "derek@example.com",
            "subject": f"Test email {i}",
            "date": "Mon, 1 Mar 2026 10:00:00",
            "snippet": f"Snippet for email {i}",
            "labels": ["INBOX", "UNREAD"],
        }
        for i in range(count)
    ]


class TestRunCycle:
    """Full triage cycle runs end-to-end."""

    @pytest.mark.asyncio
    async def test_disabled_returns_skipped(self):
        config = TriageConfig(enabled=False)
        runner = EmailTriageRunner(config=config)
        report = await runner.run_cycle()
        assert report.skipped is True
        assert report.skip_reason == "disabled"

    @pytest.mark.asyncio
    async def test_processes_emails(self):
        config = TriageConfig(enabled=True, extraction_enabled=False)
        runner = EmailTriageRunner(config=config)

        # Mock email fetching
        runner._fetch_unread = AsyncMock(return_value=_sample_emails(3))
        # Mock label operations
        runner._label_map = {"jarvis/tier4_noise": "L4", "jarvis/tier3_review": "L3",
                             "jarvis/tier2_high": "L2", "jarvis/tier1_critical": "L1"}
        runner._apply_label = AsyncMock()

        report = await runner.run_cycle()
        assert report.skipped is False
        assert report.emails_fetched == 3
        assert report.emails_processed == 3

    @pytest.mark.asyncio
    async def test_respects_max_per_cycle(self):
        config = TriageConfig(enabled=True, extraction_enabled=False, max_emails_per_cycle=2)
        runner = EmailTriageRunner(config=config)

        runner._fetch_unread = AsyncMock(return_value=_sample_emails(5))
        runner._label_map = {"jarvis/tier4_noise": "L4", "jarvis/tier3_review": "L3",
                             "jarvis/tier2_high": "L2", "jarvis/tier1_critical": "L1"}
        runner._apply_label = AsyncMock()

        report = await runner.run_cycle()
        assert report.emails_fetched == 5
        assert report.emails_processed == 2  # Limited to max

    @pytest.mark.asyncio
    async def test_handles_fetch_failure(self):
        config = TriageConfig(enabled=True)
        runner = EmailTriageRunner(config=config)
        runner._fetch_unread = AsyncMock(side_effect=RuntimeError("API error"))

        report = await runner.run_cycle()
        assert len(report.errors) > 0
        assert report.emails_processed == 0

    @pytest.mark.asyncio
    async def test_single_email_error_does_not_abort_cycle(self):
        config = TriageConfig(enabled=True, extraction_enabled=False)
        runner = EmailTriageRunner(config=config)

        emails = _sample_emails(3)
        runner._fetch_unread = AsyncMock(return_value=emails)
        runner._label_map = {"jarvis/tier4_noise": "L4", "jarvis/tier3_review": "L3",
                             "jarvis/tier2_high": "L2", "jarvis/tier1_critical": "L1"}

        # Second email causes label failure
        call_count = 0
        async def flaky_label(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise RuntimeError("label fail")
        runner._apply_label = flaky_label

        report = await runner.run_cycle()
        # Should still process 3 emails (error on 2nd doesn't stop 3rd)
        assert report.emails_processed == 3
        assert len(report.errors) == 1


class TestRunnerSingleton:
    """Singleton pattern works."""

    def test_get_instance(self):
        EmailTriageRunner._instance = None  # Reset
        r1 = EmailTriageRunner.get_instance()
        r2 = EmailTriageRunner.get_instance()
        assert r1 is r2
        EmailTriageRunner._instance = None  # Clean up
