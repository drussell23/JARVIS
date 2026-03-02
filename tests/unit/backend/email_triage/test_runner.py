"""Tests for the email triage runner (full cycle orchestration)."""

import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "backend"))

import pytest
from autonomy.email_triage.runner import EmailTriageRunner
from autonomy.email_triage.config import TriageConfig
from autonomy.email_triage.schemas import TriageCycleReport


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


class TestDependencyResolution:
    """Runner uses DependencyResolver for workspace_agent and router."""

    @pytest.mark.asyncio
    async def test_resolves_deps_on_first_cycle(self):
        """When workspace_agent is injected, resolver holds it and
        _fetch_unread returns its data."""
        agent = AsyncMock()
        agent._fetch_unread_emails = AsyncMock(
            return_value={"emails": []}
        )
        config = TriageConfig(enabled=True)
        runner = EmailTriageRunner(config=config, workspace_agent=agent)

        report = await runner.run_cycle()
        assert report.emails_fetched == 0
        assert report.skipped is False

    @pytest.mark.asyncio
    async def test_no_workspace_agent_returns_zero_fetched(self):
        """Without workspace_agent injection (and lazy resolution failing
        in test), fetch returns empty list and report shows 0 fetched."""
        config = TriageConfig(enabled=True)
        runner = EmailTriageRunner(config=config)

        report = await runner.run_cycle()
        assert report.emails_fetched == 0
        assert report.emails_processed == 0

    @pytest.mark.asyncio
    async def test_resolver_accessible(self):
        """Runner exposes its DependencyResolver as _resolver."""
        agent = MagicMock()
        config = TriageConfig(enabled=False)
        runner = EmailTriageRunner(config=config, workspace_agent=agent)

        assert hasattr(runner, "_resolver")
        assert runner._resolver.get("workspace_agent") is agent


class TestTriageCache:
    """Runner maintains a triage cache for enrichment consumers."""

    @pytest.mark.asyncio
    async def test_last_report_populated_after_cycle(self):
        """After a successful cycle, _last_report is populated."""
        config = TriageConfig(enabled=True, extraction_enabled=False)
        runner = EmailTriageRunner(config=config)
        runner._fetch_unread = AsyncMock(return_value=_sample_emails(1))
        runner._label_map = {
            "jarvis/tier4_noise": "L4", "jarvis/tier3_review": "L3",
            "jarvis/tier2_high": "L2", "jarvis/tier1_critical": "L1",
        }
        runner._apply_label = AsyncMock()

        report = await runner.run_cycle()
        assert runner._last_report is not None
        assert runner._last_report.cycle_id == report.cycle_id
        assert runner._last_report_at > 0

    @pytest.mark.asyncio
    async def test_triaged_emails_populated(self):
        """After processing 2 emails, _triaged_emails has 2 entries."""
        config = TriageConfig(enabled=True, extraction_enabled=False)
        runner = EmailTriageRunner(config=config)
        runner._fetch_unread = AsyncMock(return_value=_sample_emails(2))
        runner._label_map = {
            "jarvis/tier4_noise": "L4", "jarvis/tier3_review": "L3",
            "jarvis/tier2_high": "L2", "jarvis/tier1_critical": "L1",
        }
        runner._apply_label = AsyncMock()

        await runner.run_cycle()
        assert len(runner._triaged_emails) == 2

    @pytest.mark.asyncio
    async def test_partial_cycle_preserves_previous_snapshot(self):
        """First cycle succeeds, second fails on fetch -- previous snapshot preserved."""
        config = TriageConfig(enabled=True, extraction_enabled=False)
        runner = EmailTriageRunner(config=config)

        # First cycle succeeds
        runner._fetch_unread = AsyncMock(return_value=_sample_emails(1))
        runner._label_map = {
            "jarvis/tier4_noise": "L4", "jarvis/tier3_review": "L3",
            "jarvis/tier2_high": "L2", "jarvis/tier1_critical": "L1",
        }
        runner._apply_label = AsyncMock()
        first_report = await runner.run_cycle()
        assert runner._last_report is not None
        first_cycle_id = runner._last_report.cycle_id

        # Second cycle fails on fetch
        runner._fetch_unread = AsyncMock(side_effect=RuntimeError("API error"))
        await runner.run_cycle()

        # Previous snapshot preserved
        assert runner._last_report is not None
        assert runner._last_report.cycle_id == first_cycle_id

    @pytest.mark.asyncio
    async def test_get_fresh_results_returns_when_fresh(self):
        """After a cycle, get_fresh_results returns the report."""
        config = TriageConfig(enabled=True, extraction_enabled=False, staleness_window_s=120.0)
        runner = EmailTriageRunner(config=config)
        runner._fetch_unread = AsyncMock(return_value=_sample_emails(1))
        runner._label_map = {
            "jarvis/tier4_noise": "L4", "jarvis/tier3_review": "L3",
            "jarvis/tier2_high": "L2", "jarvis/tier1_critical": "L1",
        }
        runner._apply_label = AsyncMock()

        await runner.run_cycle()
        result = runner.get_fresh_results()
        assert result is not None
        assert isinstance(result, TriageCycleReport)

    @pytest.mark.asyncio
    async def test_get_fresh_results_returns_none_when_stale(self):
        """When _last_report_at is old enough, get_fresh_results returns None."""
        config = TriageConfig(enabled=True, extraction_enabled=False, staleness_window_s=120.0)
        runner = EmailTriageRunner(config=config)
        runner._fetch_unread = AsyncMock(return_value=_sample_emails(1))
        runner._label_map = {
            "jarvis/tier4_noise": "L4", "jarvis/tier3_review": "L3",
            "jarvis/tier2_high": "L2", "jarvis/tier1_critical": "L1",
        }
        runner._apply_label = AsyncMock()

        await runner.run_cycle()
        assert runner._last_report is not None

        # Artificially age the report
        runner._last_report_at = time.monotonic() - 300.0
        result = runner.get_fresh_results(staleness_window_s=120.0)
        assert result is None

    def test_get_fresh_results_returns_none_initially(self):
        """Before any cycle, get_fresh_results returns None."""
        config = TriageConfig(enabled=False)
        runner = EmailTriageRunner(config=config)
        assert runner.get_fresh_results() is None
