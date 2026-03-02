"""E2E tests for snapshot commit policy and multi-cycle consistency.

Verifies that the commit gate correctly:
- Commits healthy cycles
- Preserves prior snapshots on degraded cycles
- Handles cold-start, staleness, and defensive copies
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "backend"))

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from autonomy.email_triage.runner import EmailTriageRunner

from tests.e2e.email_triage.conftest import (
    critical_email,
    high_priority_email,
    routine_email,
    noise_email,
    mixed_inbox,
    make_mock_workspace_agent,
    make_mock_notifier,
    make_triage_config,
    controlled_time,
    swap_runner_dep,
)


class TestSnapshotConsistencyE2E:
    """Multi-cycle snapshot commit policy tests."""

    @pytest.mark.asyncio
    async def test_healthy_then_degraded_preserves_prior(self, fresh_runner):
        """Cycle 1 healthy → committed. Cycle 2 all-error → degraded, prior preserved."""
        emails = mixed_inbox()
        config = make_triage_config()

        with controlled_time(hour=10) as ctrl:
            # Cycle 1: healthy
            agent1 = make_mock_workspace_agent(emails)
            runner = fresh_runner(config=config, workspace_agent=agent1)
            report1 = await runner.run_cycle()
            assert report1.snapshot_committed is True
            assert report1.degraded is False

            snapshot1 = runner.get_triage_snapshot(staleness_window_s=9999)
            assert snapshot1 is not None
            cycle1_ids = set(snapshot1["triaged_emails"].keys())

            # Cycle 2: all extraction fails
            ctrl.advance(10)
            agent2 = make_mock_workspace_agent(emails)
            swap_runner_dep(runner, "workspace_agent", agent2)

            with patch(
                "autonomy.email_triage.runner.extract_features",
                new_callable=AsyncMock,
                side_effect=RuntimeError("extraction exploded"),
            ):
                report2 = await runner.run_cycle()

            assert report2.degraded is True
            assert report2.snapshot_committed is False

            # Prior snapshot still accessible
            snapshot_after = runner.get_triage_snapshot(staleness_window_s=9999)
            assert snapshot_after is not None
            assert set(snapshot_after["triaged_emails"].keys()) == cycle1_ids

    @pytest.mark.asyncio
    async def test_healthy_degraded_healthy_advances(self, fresh_runner):
        """Cycle 1 healthy → Cycle 2 degraded → Cycle 3 healthy advances snapshot."""
        emails_v1 = [critical_email("msg_v1"), routine_email("msg_v1_r")]
        emails_v2 = [high_priority_email("msg_v2"), noise_email("msg_v2_n")]
        config = make_triage_config()

        with controlled_time(hour=10) as ctrl:
            # Cycle 1: healthy with v1 emails
            agent1 = make_mock_workspace_agent(emails_v1)
            runner = fresh_runner(config=config, workspace_agent=agent1)
            report1 = await runner.run_cycle()
            assert report1.snapshot_committed is True

            # Cycle 2: all extraction fails (degraded)
            ctrl.advance(10)
            agent2 = make_mock_workspace_agent(emails_v1)
            swap_runner_dep(runner, "workspace_agent", agent2)

            with patch(
                "autonomy.email_triage.runner.extract_features",
                new_callable=AsyncMock,
                side_effect=RuntimeError("extraction broken"),
            ):
                report2 = await runner.run_cycle()
            assert report2.degraded is True

            # Cycle 3: healthy with v2 emails → should advance
            ctrl.advance(10)
            agent3 = make_mock_workspace_agent(emails_v2)
            swap_runner_dep(runner, "workspace_agent", agent3)

            report3 = await runner.run_cycle()
            assert report3.snapshot_committed is True

            # Snapshot should have v2 emails, not v1
            snapshot = runner.get_triage_snapshot(staleness_window_s=9999)
            assert snapshot is not None
            assert "msg_v2" in snapshot["triaged_emails"]
            assert "msg_v1" not in snapshot["triaged_emails"]

    @pytest.mark.asyncio
    async def test_cold_start_no_workspace_agent_blocks_snapshot(self, fresh_runner):
        """workspace_agent=None → cold_start_dep_unavailable, no snapshot."""
        config = make_triage_config()
        runner = fresh_runner(config=config, workspace_agent=None)

        # Block the resolver from importing the real GoogleWorkspaceAgent singleton
        with patch(
            "autonomy.email_triage.dependencies._resolve_workspace_agent",
            side_effect=RuntimeError("No agent in test"),
        ):
            report = await runner.run_cycle()

        # No workspace agent → fetch returns [] but commit gate blocks
        assert runner._committed_snapshot is None
        assert report.degraded is True
        assert "cold_start" in (report.degraded_reason or "")

    @pytest.mark.asyncio
    async def test_fetch_failure_returns_early_preserves_prior(self, fresh_runner):
        """Cycle 1 healthy → Cycle 2 fetch raises → prior preserved."""
        emails = [routine_email("msg_fetch_ok")]
        config = make_triage_config()

        with controlled_time(hour=10) as ctrl:
            agent1 = make_mock_workspace_agent(emails)
            runner = fresh_runner(config=config, workspace_agent=agent1)
            report1 = await runner.run_cycle()
            assert report1.snapshot_committed is True

            # Cycle 2: fetch fails
            ctrl.advance(10)
            agent2 = MagicMock()
            agent2._fetch_unread_emails = AsyncMock(
                side_effect=RuntimeError("Gmail API error")
            )
            agent2._gmail_service = MagicMock()
            swap_runner_dep(runner, "workspace_agent", agent2)

            report2 = await runner.run_cycle()

            # Fetch failure returns early with error
            assert len(report2.errors) > 0
            assert any("fetch" in e for e in report2.errors)

            # Prior snapshot preserved
            snapshot = runner.get_triage_snapshot(staleness_window_s=9999)
            assert snapshot is not None
            assert "msg_fetch_ok" in snapshot["triaged_emails"]

    @pytest.mark.asyncio
    async def test_staleness_window_expires_snapshot(self, fresh_runner):
        """Snapshot expires after staleness_window_s."""
        emails = [routine_email("msg_stale")]
        config = make_triage_config(staleness_window_s=120.0)

        with controlled_time(hour=10) as ctrl:
            agent = make_mock_workspace_agent(emails)
            runner = fresh_runner(config=config, workspace_agent=agent)
            await runner.run_cycle()

            # Fresh — should be available
            assert runner.get_triage_snapshot(staleness_window_s=120) is not None
            assert runner.get_fresh_results(staleness_window_s=120) is not None

            # Fast-forward past staleness window
            ctrl.advance(200)

            assert runner.get_triage_snapshot(staleness_window_s=120) is None
            assert runner.get_fresh_results(staleness_window_s=120) is None

    @pytest.mark.asyncio
    async def test_get_triaged_email_returns_correct_data(self, fresh_runner):
        """get_triaged_email returns correct data or None for nonexistent."""
        emails = [
            critical_email("msg_lookup_1"),
            routine_email("msg_lookup_2"),
            noise_email("msg_lookup_3"),
        ]
        config = make_triage_config()
        agent = make_mock_workspace_agent(emails)
        runner = fresh_runner(config=config, workspace_agent=agent)

        await runner.run_cycle()

        # Each email retrievable
        for email in emails:
            triaged = runner.get_triaged_email(email["id"])
            assert triaged is not None
            assert triaged.features.message_id == email["id"]

        # Nonexistent returns None
        assert runner.get_triaged_email("nonexistent_msg") is None

    @pytest.mark.asyncio
    async def test_snapshot_defensive_copy_prevents_mutation(self, fresh_runner):
        """Mutating returned snapshot dict doesn't corrupt internal state."""
        emails = [routine_email("msg_copy_test")]
        config = make_triage_config()
        agent = make_mock_workspace_agent(emails)
        runner = fresh_runner(config=config, workspace_agent=agent)

        await runner.run_cycle()

        # Get snapshot and mutate the returned dict
        snap1 = runner.get_triage_snapshot(staleness_window_s=9999)
        assert snap1 is not None
        snap1["triaged_emails"]["injected_key"] = "bad_data"
        del snap1["triaged_emails"]["msg_copy_test"]

        # Get fresh snapshot — internal data should be unchanged
        snap2 = runner.get_triage_snapshot(staleness_window_s=9999)
        assert snap2 is not None
        assert "msg_copy_test" in snap2["triaged_emails"]
        assert "injected_key" not in snap2["triaged_emails"]

    @pytest.mark.asyncio
    async def test_disabled_config_produces_skipped_report(self, fresh_runner):
        """enabled=False → skipped report, no pipeline execution."""
        config = make_triage_config(enabled=False)
        agent = make_mock_workspace_agent([critical_email()])
        runner = fresh_runner(config=config, workspace_agent=agent)

        report = await runner.run_cycle()

        assert report.skipped is True
        assert report.skip_reason == "disabled"
        assert report.emails_fetched == 0
        assert report.emails_processed == 0
        assert report.notifications_sent == 0
        # No snapshot committed for skipped cycles
        assert runner._committed_snapshot is None
