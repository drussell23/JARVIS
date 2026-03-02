"""Tests for the email triage runner (full cycle orchestration)."""

import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

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

        # Artificially age the committed snapshot
        runner._committed_snapshot["committed_at"] = time.monotonic() - 300.0
        result = runner.get_fresh_results(staleness_window_s=120.0)
        assert result is None

    def test_get_fresh_results_returns_none_initially(self):
        """Before any cycle, get_fresh_results returns None."""
        config = TriageConfig(enabled=False)
        runner = EmailTriageRunner(config=config)
        assert runner.get_fresh_results() is None


class TestNotificationDelivery:
    """Runner wires notification delivery for immediate and summary actions."""

    def _make_runner(self, notifier=None, **config_kwargs):
        config = TriageConfig(enabled=True, extraction_enabled=False, **config_kwargs)
        runner = EmailTriageRunner(config=config, notifier=notifier)
        runner._fetch_unread = AsyncMock(return_value=_sample_emails(2))
        runner._label_map = {
            "jarvis/tier4_noise": "L4", "jarvis/tier3_review": "L3",
            "jarvis/tier2_high": "L2", "jarvis/tier1_critical": "L1",
        }
        runner._apply_label = AsyncMock()
        return runner

    @pytest.mark.asyncio
    async def test_deliver_immediate_called_when_notifier_present(self):
        """When notifier is resolved and policy returns 'immediate',
        deliver_immediate is called with the immediate emails."""
        notifier = AsyncMock(return_value=True)
        runner = self._make_runner(notifier=notifier)
        runner._policy.decide_action = MagicMock(return_value="immediate")

        mock_result = MagicMock(success=True)
        with patch(
            "autonomy.email_triage.runner.deliver_immediate",
            new_callable=AsyncMock,
            return_value=[mock_result, mock_result],
        ) as mock_deliver:
            report = await runner.run_cycle()
            mock_deliver.assert_called_once()
            args = mock_deliver.call_args
            assert len(args[0][0]) == 2  # 2 immediate emails
            assert args[0][1] is notifier  # notifier passed through

        assert report.notifications_sent == 2

    @pytest.mark.asyncio
    async def test_no_delivery_without_notifier(self):
        """When notifier is not resolved, deliver_immediate is never called."""
        runner = self._make_runner(notifier=None)
        runner._policy.decide_action = MagicMock(return_value="immediate")
        # Prevent lazy resolution from finding the real notifier
        runner._resolver.resolve_all = AsyncMock()

        with patch(
            "autonomy.email_triage.runner.deliver_immediate",
            new_callable=AsyncMock,
        ) as mock_deliver:
            report = await runner.run_cycle()
            mock_deliver.assert_not_called()

        assert report.notifications_sent == 0

    @pytest.mark.asyncio
    async def test_delivery_failure_does_not_affect_triage_outcome(self):
        """Notification delivery exception is caught and logged as error,
        but triage processing (tier counts, labels) is unaffected."""
        notifier = AsyncMock(return_value=True)
        runner = self._make_runner(notifier=notifier)
        runner._policy.decide_action = MagicMock(return_value="immediate")

        with patch(
            "autonomy.email_triage.runner.deliver_immediate",
            new_callable=AsyncMock,
            side_effect=RuntimeError("notification bridge down"),
        ):
            report = await runner.run_cycle()

        assert report.emails_processed == 2
        assert report.notifications_sent == 0
        assert any("notify_immediate" in e for e in report.errors)

    @pytest.mark.asyncio
    async def test_notifications_sent_reflects_actual_delivery(self):
        """notifications_sent counts successful deliveries, not decisions."""
        notifier = AsyncMock(return_value=True)
        runner = self._make_runner(notifier=notifier)
        runner._policy.decide_action = MagicMock(return_value="immediate")

        success = MagicMock(success=True)
        failure = MagicMock(success=False)
        with patch(
            "autonomy.email_triage.runner.deliver_immediate",
            new_callable=AsyncMock,
            return_value=[success, failure],
        ):
            report = await runner.run_cycle()

        assert report.notifications_sent == 1  # Only 1 of 2 succeeded
