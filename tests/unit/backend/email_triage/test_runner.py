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


class TestSnapshotCommitPolicy:
    """v1.1.1: Last-good snapshot commit policy."""

    def _make_runner(self, workspace_agent=None, **config_kwargs):
        config = TriageConfig(enabled=True, extraction_enabled=False, **config_kwargs)
        runner = EmailTriageRunner(config=config, workspace_agent=workspace_agent)
        runner._label_map = {
            "jarvis/tier4_noise": "L4", "jarvis/tier3_review": "L3",
            "jarvis/tier2_high": "L2", "jarvis/tier1_critical": "L1",
        }
        runner._apply_label = AsyncMock()
        return runner

    @pytest.mark.asyncio
    async def test_healthy_cycle_commits_snapshot(self):
        """Full successful cycle commits snapshot_committed=True."""
        agent = AsyncMock()
        agent._fetch_unread_emails = AsyncMock(
            return_value={"emails": _sample_emails(2)}
        )
        runner = self._make_runner(workspace_agent=agent)

        report = await runner.run_cycle()
        assert report.snapshot_committed is True
        assert report.degraded is False
        assert runner._committed_snapshot is not None
        assert runner._committed_snapshot["report"].cycle_id == report.cycle_id

    @pytest.mark.asyncio
    async def test_degraded_cycle_preserves_prior(self):
        """When all extract_features fail, prior snapshot preserved."""
        agent = AsyncMock()
        agent._fetch_unread_emails = AsyncMock(
            return_value={"emails": _sample_emails(2)}
        )
        runner = self._make_runner(workspace_agent=agent)

        # First cycle succeeds
        first_report = await runner.run_cycle()
        assert first_report.snapshot_committed is True
        first_snapshot = runner._committed_snapshot
        first_cycle_id = first_snapshot["report"].cycle_id

        # Second cycle: all processing errors
        with patch(
            "autonomy.email_triage.runner.extract_features",
            new_callable=AsyncMock,
            side_effect=RuntimeError("extraction boom"),
        ):
            second_report = await runner.run_cycle()

        assert second_report.degraded is True
        assert second_report.snapshot_committed is False
        assert "error_ratio" in second_report.degraded_reason
        # Prior snapshot preserved
        assert runner._committed_snapshot["report"].cycle_id == first_cycle_id

    @pytest.mark.asyncio
    async def test_first_cycle_commits_with_actual_data(self):
        """No prior snapshot, cycle with emails succeeds — commits."""
        agent = AsyncMock()
        agent._fetch_unread_emails = AsyncMock(
            return_value={"emails": _sample_emails(1)}
        )
        runner = self._make_runner(workspace_agent=agent)
        assert runner._committed_snapshot is None

        report = await runner.run_cycle()
        assert report.snapshot_committed is True
        assert runner._committed_snapshot is not None

    @pytest.mark.asyncio
    async def test_cold_start_dep_unavailable_does_not_commit(self):
        """No prior + workspace_agent unresolved -> no commit (blocker #1)."""
        runner = self._make_runner(workspace_agent=None)
        # Prevent resolve_all from finding a real workspace_agent
        runner._resolver.resolve_all = AsyncMock()
        # Without workspace_agent, _fetch_unread returns []
        report = await runner.run_cycle()
        assert report.snapshot_committed is False
        assert report.degraded is True
        assert report.degraded_reason == "cold_start_dep_unavailable"
        assert runner._committed_snapshot is None

    @pytest.mark.asyncio
    async def test_cold_start_legit_empty_inbox_commits(self):
        """No prior + workspace_agent IS resolved but inbox empty -> commits."""
        agent = AsyncMock()
        agent._fetch_unread_emails = AsyncMock(
            return_value={"emails": []}
        )
        runner = self._make_runner(workspace_agent=agent)

        report = await runner.run_cycle()
        # Legit empty inbox is valid truth — should commit
        assert report.snapshot_committed is True

    @pytest.mark.asyncio
    async def test_fetch_failure_preserves_prior(self):
        """Fetch failure early-returns before commit block, prior preserved."""
        agent = AsyncMock()
        agent._fetch_unread_emails = AsyncMock(
            return_value={"emails": _sample_emails(1)}
        )
        runner = self._make_runner(workspace_agent=agent)

        # First cycle succeeds
        first_report = await runner.run_cycle()
        first_cycle_id = runner._committed_snapshot["report"].cycle_id

        # Second cycle: fetch fails
        runner._fetch_unread = AsyncMock(side_effect=RuntimeError("API error"))
        await runner.run_cycle()

        # Prior snapshot preserved (fetch failure returns before commit block)
        assert runner._committed_snapshot["report"].cycle_id == first_cycle_id

    @pytest.mark.asyncio
    async def test_healthy_after_degraded_advances(self):
        """Degraded preserves prior, then healthy cycle advances snapshot."""
        agent = AsyncMock()
        agent._fetch_unread_emails = AsyncMock(
            return_value={"emails": _sample_emails(2)}
        )
        runner = self._make_runner(workspace_agent=agent)

        # First cycle succeeds
        await runner.run_cycle()
        first_cycle_id = runner._committed_snapshot["report"].cycle_id

        # Second cycle: degraded (all errors)
        with patch(
            "autonomy.email_triage.runner.extract_features",
            new_callable=AsyncMock,
            side_effect=RuntimeError("extraction boom"),
        ):
            degraded_report = await runner.run_cycle()
        assert degraded_report.degraded is True
        assert runner._committed_snapshot["report"].cycle_id == first_cycle_id

        # Third cycle: healthy again
        third_report = await runner.run_cycle()
        assert third_report.snapshot_committed is True
        assert runner._committed_snapshot["report"].cycle_id != first_cycle_id

    @pytest.mark.asyncio
    async def test_snapshot_preserved_event_emitted(self):
        """When degraded, emit_triage_event called with snapshot_preserved."""
        agent = AsyncMock()
        agent._fetch_unread_emails = AsyncMock(
            return_value={"emails": _sample_emails(2)}
        )
        runner = self._make_runner(workspace_agent=agent)

        # First cycle succeeds (establishes prior)
        await runner.run_cycle()

        # Second cycle: all errors -> degraded -> event emitted
        with patch(
            "autonomy.email_triage.runner.extract_features",
            new_callable=AsyncMock,
            side_effect=RuntimeError("extraction boom"),
        ), patch(
            "autonomy.email_triage.runner.emit_triage_event",
        ) as mock_emit:
            await runner.run_cycle()

        # Find the snapshot_preserved event among all emitted events
        preserved_calls = [
            c for c in mock_emit.call_args_list
            if c[0][0] == "snapshot_preserved"
        ]
        assert len(preserved_calls) == 1
        payload = preserved_calls[0][0][1]
        assert "reason" in payload
        assert "prior_cycle_id" in payload

    @pytest.mark.asyncio
    async def test_empty_triaged_with_prior_is_regression(self):
        """All emails filtered (new_triaged empty) but prior had data."""
        agent = AsyncMock()
        agent._fetch_unread_emails = AsyncMock(
            return_value={"emails": _sample_emails(2)}
        )
        runner = self._make_runner(workspace_agent=agent)

        # First cycle succeeds
        await runner.run_cycle()

        # Second cycle: fetch succeeds but 0 emails -> empty triaged
        agent._fetch_unread_emails = AsyncMock(return_value={"emails": []})
        second_report = await runner.run_cycle()
        assert second_report.degraded is True
        assert second_report.degraded_reason == "empty_triaged_regression"

    @pytest.mark.asyncio
    async def test_snapshot_read_returns_defensive_copy(self):
        """Mutating returned triaged_emails does NOT affect internal snapshot."""
        agent = AsyncMock()
        agent._fetch_unread_emails = AsyncMock(
            return_value={"emails": _sample_emails(1)}
        )
        runner = self._make_runner(workspace_agent=agent)

        await runner.run_cycle()
        snapshot = runner.get_triage_snapshot()
        assert snapshot is not None

        # Mutate the returned dict
        snapshot["triaged_emails"]["injected"] = "evil"

        # Internal snapshot should be unaffected
        internal = runner._committed_snapshot
        assert "injected" not in internal["triaged_emails"]

    def test_frozen_report_rejects_mutation(self):
        """TriageCycleReport is frozen — direct assignment raises."""
        report = TriageCycleReport(
            cycle_id="test",
            started_at=0.0,
            completed_at=0.0,
            emails_fetched=0,
            emails_processed=0,
            tier_counts={},
            notifications_sent=0,
            notifications_suppressed=0,
            errors=[],
        )
        with pytest.raises(AttributeError):
            report.cycle_id = "mutated"
