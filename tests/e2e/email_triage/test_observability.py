"""E2E tests for observability and event completeness.

Validates that every step of the pipeline emits the correct structured
events in the right order with the right payloads.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "backend"))

import pytest
from unittest.mock import AsyncMock, patch

from tests.e2e.email_triage.conftest import (
    critical_email,
    high_priority_email,
    routine_email,
    mixed_inbox,
    make_mock_workspace_agent,
    make_mock_router,
    make_mock_notifier,
    make_triage_config,
    controlled_time,
    swap_runner_dep,
)


class TestObservabilityE2E:
    """Validates event sequence, payloads, and partial ordering."""

    @pytest.mark.asyncio
    async def test_full_cycle_event_sequence(self, fresh_runner, capture_events):
        """Healthy 3-email cycle emits correct event sequence."""
        emails = [
            critical_email("msg_obs_1"),
            routine_email("msg_obs_2"),
            high_priority_email("msg_obs_3"),
        ]
        agent = make_mock_workspace_agent(emails)
        config = make_triage_config()

        with controlled_time(hour=10):
            runner = fresh_runner(config=config, workspace_agent=agent)
            await runner.run_cycle()

        # Partial order: CYCLE_STARTED before any EMAIL_TRIAGED
        capture_events.assert_before("triage_cycle_started", "email_triaged")
        # Partial order: all EMAIL_TRIAGED before CYCLE_COMPLETED
        capture_events.assert_before("email_triaged", "triage_cycle_completed")

        # 3 EMAIL_TRIAGED events
        triaged_events = capture_events.find("email_triaged")
        assert len(triaged_events) == 3

        # Each has required fields
        for evt in triaged_events:
            assert "cycle_id" in evt
            assert "message_id" in evt
            assert "score" in evt
            assert "tier" in evt
            assert "action" in evt
            assert "breakdown" in evt

    @pytest.mark.asyncio
    async def test_cycle_completed_event_has_correct_stats(self, fresh_runner, capture_events):
        """CYCLE_COMPLETED payload matches report stats."""
        emails = mixed_inbox()
        agent = make_mock_workspace_agent(emails)
        config = make_triage_config()

        with controlled_time(hour=10):
            runner = fresh_runner(config=config, workspace_agent=agent)
            report = await runner.run_cycle()

        completed_events = capture_events.find("triage_cycle_completed")
        assert len(completed_events) == 1

        payload = completed_events[0]
        assert payload["emails_fetched"] == report.emails_fetched
        assert payload["emails_processed"] == report.emails_processed
        assert payload["tier_counts"] == report.tier_counts
        assert payload["notifications_sent"] == report.notifications_sent
        assert payload["errors"] == len(report.errors)
        assert "duration_ms" in payload
        assert payload["duration_ms"] >= 0

    @pytest.mark.asyncio
    async def test_snapshot_preserved_event_on_degraded_cycle(self, fresh_runner, capture_events):
        """Degraded cycle emits snapshot_preserved event with reason."""
        emails = [routine_email("msg_obs_prior")]
        config = make_triage_config()

        with controlled_time(hour=10) as ctrl:
            # Cycle 1: healthy, establishes prior snapshot
            agent1 = make_mock_workspace_agent(emails)
            runner = fresh_runner(config=config, workspace_agent=agent1)
            await runner.run_cycle()

            # Cycle 2: all extraction fails → degraded
            ctrl.advance(10)
            agent2 = make_mock_workspace_agent(emails)
            swap_runner_dep(runner, "workspace_agent", agent2)

            with patch(
                "autonomy.email_triage.runner.extract_features",
                new_callable=AsyncMock,
                side_effect=RuntimeError("extraction broken"),
            ):
                report = await runner.run_cycle()

        assert report.degraded is True

        preserved_events = capture_events.find("snapshot_preserved")
        assert len(preserved_events) >= 1

        payload = preserved_events[0]
        assert "reason" in payload
        assert "prior_cycle_id" in payload
        assert "error_count" in payload

    @pytest.mark.asyncio
    async def test_notification_delivery_result_events(self, fresh_runner, capture_events):
        """Notification delivery emits delivery_result events."""
        emails = [critical_email("msg_obs_notif")]
        router = make_mock_router(response_json={
            "keywords": ["urgent", "critical", "emergency"],
            "sender_frequency": "frequent",
            "urgency_signals": ["action_required", "escalation", "deadline"],
        })
        agent = make_mock_workspace_agent(emails)
        notifier = make_mock_notifier()
        config = make_triage_config(extraction_enabled=True)

        with controlled_time(hour=10):
            runner = fresh_runner(
                config=config, workspace_agent=agent,
                router=router, notifier=notifier,
            )
            await runner.run_cycle()

        delivery_events = capture_events.find("notification_delivery_result")
        assert len(delivery_events) >= 1

        evt = delivery_events[0]
        assert "message_id" in evt
        assert "channel" in evt
        assert evt["success"] is True
        assert "latency_ms" in evt
        assert evt["latency_ms"] >= 0

    @pytest.mark.asyncio
    async def test_triage_error_event_on_process_failure(self, fresh_runner, capture_events):
        """Extraction failure → triage_error event with correct payload."""
        emails = [critical_email("msg_obs_err"), routine_email("msg_obs_ok")]
        config = make_triage_config()
        agent = make_mock_workspace_agent(emails)

        from autonomy.email_triage.extraction import extract_features as _real_extract

        async def _fail_first(email, router, config):
            if email.get("id") == "msg_obs_err":
                raise RuntimeError("Test error")
            return await _real_extract(email, router, config=config)

        runner = fresh_runner(config=config, workspace_agent=agent)

        with patch("autonomy.email_triage.runner.extract_features", side_effect=_fail_first):
            await runner.run_cycle()

        # triage_error event emitted
        error_events = capture_events.find("triage_error")
        assert len(error_events) >= 1

        err = error_events[0]
        assert err["error_type"] == "process_failed"
        assert err["message_id"] == "msg_obs_err"
        assert "message" in err

        # cycle_completed still emitted
        completed = capture_events.find("triage_cycle_completed")
        assert len(completed) == 1
