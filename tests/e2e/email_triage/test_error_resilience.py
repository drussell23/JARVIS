"""E2E tests for error resilience and graceful degradation.

Validates that the system handles partial failures, cascading errors,
and degraded scenarios without crashing or corrupting data.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "backend"))

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from autonomy.email_triage.extraction import extract_features as _real_extract

from tests.e2e.email_triage.conftest import (
    critical_email,
    high_priority_email,
    routine_email,
    generate_emails,
    make_mock_workspace_agent,
    make_mock_notifier,
    make_triage_config,
    controlled_time,
    swap_runner_dep,
)


class TestErrorResilienceE2E:
    """Tests that the system degrades gracefully under failure modes."""

    @pytest.mark.asyncio
    async def test_partial_extraction_failure_processes_rest(self, fresh_runner, capture_events):
        """2 of 5 extraction failures → 3 succeed, snapshot commits."""
        emails = generate_emails(5)
        fail_ids = {emails[1]["id"], emails[3]["id"]}  # Fail on emails 2 and 4
        call_count = {"n": 0}

        async def _selective_fail(email, router, config):
            call_count["n"] += 1
            if email.get("id", "") in fail_ids:
                raise RuntimeError(f"Extraction failed for {email.get('id')}")
            return await _real_extract(email, router, config=config)

        config = make_triage_config()
        agent = make_mock_workspace_agent(emails)
        runner = fresh_runner(config=config, workspace_agent=agent)

        with patch(
            "autonomy.email_triage.runner.extract_features",
            side_effect=_selective_fail,
        ):
            report = await runner.run_cycle()

        # All 5 attempted (emails_processed counts attempts)
        assert report.emails_processed == 5
        # 2 processing errors
        process_errors = [e for e in report.errors if e.startswith("process:")]
        assert len(process_errors) == 2

        # Snapshot still commits (error_ratio = 2/5 = 0.4 < 0.5 threshold)
        assert report.snapshot_committed is True

        # 3 emails successfully triaged
        snapshot = runner.get_triage_snapshot(staleness_window_s=9999)
        assert snapshot is not None
        assert len(snapshot["triaged_emails"]) == 3

    @pytest.mark.asyncio
    async def test_all_extraction_failure_triggers_degraded(self, fresh_runner):
        """All extractions fail → error_ratio=1.0 → degraded, no snapshot."""
        emails = generate_emails(5)
        config = make_triage_config()

        # First do a healthy cycle so there's a prior snapshot
        agent1 = make_mock_workspace_agent([routine_email("msg_prior")])
        runner = fresh_runner(config=config, workspace_agent=agent1)
        report1 = await runner.run_cycle()
        assert report1.snapshot_committed is True

        # Now all-fail cycle
        agent2 = make_mock_workspace_agent(emails)
        swap_runner_dep(runner, "workspace_agent", agent2)

        with patch(
            "autonomy.email_triage.runner.extract_features",
            new_callable=AsyncMock,
            side_effect=RuntimeError("All extraction broken"),
        ):
            report2 = await runner.run_cycle()

        assert report2.degraded is True
        assert report2.snapshot_committed is False
        assert "error_ratio" in (report2.degraded_reason or "")

        # Prior snapshot preserved
        snapshot = runner.get_triage_snapshot(staleness_window_s=9999)
        assert snapshot is not None
        assert "msg_prior" in snapshot["triaged_emails"]

    @pytest.mark.asyncio
    async def test_label_failure_does_not_block_scoring(self, fresh_runner):
        """Label application failure → scoring and notifications still work."""
        emails = [critical_email("msg_label_fail"), routine_email("msg_label_ok")]
        config = make_triage_config()
        agent = make_mock_workspace_agent(emails)
        runner = fresh_runner(config=config, workspace_agent=agent)

        with patch(
            "autonomy.email_triage.runner.apply_label",
            new_callable=AsyncMock,
            side_effect=RuntimeError("Label API error"),
        ):
            report = await runner.run_cycle()

        # Both emails processed and scored
        assert report.emails_processed == 2
        assert report.snapshot_committed is True

        # Label errors captured
        label_errors = [e for e in report.errors if "label:" in e]
        assert len(label_errors) == 2

        # Scoring still happened correctly
        for email in emails:
            triaged = runner.get_triaged_email(email["id"])
            assert triaged is not None
            assert triaged.scoring.tier in (1, 2, 3, 4)

    @pytest.mark.asyncio
    async def test_notifier_timeout_captured_in_errors(self, fresh_runner):
        """Slow notifier → timeout error captured, triage unaffected."""
        emails = [critical_email("msg_slow_notif")]
        router_resp = {
            "keywords": ["urgent", "critical", "emergency"],
            "sender_frequency": "frequent",
            "urgency_signals": ["action_required", "escalation", "deadline"],
        }
        from tests.e2e.email_triage.conftest import make_mock_router
        router = make_mock_router(response_json=router_resp)

        agent = make_mock_workspace_agent(emails)
        # Notifier that takes 10s — will exceed the 0.1s budget
        notifier = make_mock_notifier(delay_s=10.0)
        config = make_triage_config(
            extraction_enabled=True,
            notification_budget_s=0.1,  # Very short budget
        )

        with controlled_time(hour=10):
            runner = fresh_runner(
                config=config, workspace_agent=agent,
                router=router, notifier=notifier,
            )
            report = await runner.run_cycle()

        # Triage scoring unaffected
        triaged = runner.get_triaged_email("msg_slow_notif")
        assert triaged is not None
        assert triaged.scoring.tier == 1
        assert triaged.notification_action == "immediate"

        # Notification error captured
        notify_errors = [e for e in report.errors if "notify" in e.lower()]
        assert len(notify_errors) >= 1

        # Snapshot still committed
        assert report.snapshot_committed is True

    @pytest.mark.asyncio
    async def test_concurrent_errors_across_pipeline_stages(self, fresh_runner):
        """Failures at extraction + labeling + notification all captured cleanly."""
        emails = [
            critical_email("msg_ext_fail"),
            high_priority_email("msg_label_fail"),
            routine_email("msg_ok"),
        ]
        config = make_triage_config()
        agent = make_mock_workspace_agent(emails)
        notifier = make_mock_notifier(should_raise=True)  # All notifications fail

        call_count = {"extract": 0}

        async def _mixed_extract(email, router, config):
            call_count["extract"] += 1
            if email.get("id") == "msg_ext_fail":
                raise RuntimeError("Extraction failed")
            return await _real_extract(email, router, config=config)

        async def _mixed_label(gmail_svc, msg_id, label_name, label_map):
            if msg_id == "msg_label_fail":
                raise RuntimeError("Label API error")

        runner = fresh_runner(config=config, workspace_agent=agent, notifier=notifier)

        with patch("autonomy.email_triage.runner.extract_features", side_effect=_mixed_extract):
            with patch("autonomy.email_triage.runner.apply_label", side_effect=_mixed_label):
                report = await runner.run_cycle()

        # All attempted
        assert report.emails_processed == 3

        # Errors from multiple stages
        assert len(report.errors) >= 2  # At least extraction + label errors

        # Successful email still triaged correctly
        triaged_ok = runner.get_triaged_email("msg_ok")
        assert triaged_ok is not None
        assert triaged_ok.scoring.tier in (1, 2, 3, 4)

    @pytest.mark.asyncio
    async def test_event_emission_on_error_paths(self, fresh_runner, capture_events):
        """Extraction error → EVENT_TRIAGE_ERROR emitted with correct payload."""
        emails = [critical_email("msg_event_err"), routine_email("msg_event_ok")]
        config = make_triage_config()
        agent = make_mock_workspace_agent(emails)

        async def _fail_first(email, router, config):
            if email.get("id") == "msg_event_err":
                raise RuntimeError("Test extraction error")
            return await _real_extract(email, router, config=config)

        runner = fresh_runner(config=config, workspace_agent=agent)

        with patch("autonomy.email_triage.runner.extract_features", side_effect=_fail_first):
            report = await runner.run_cycle()

        # EVENT_TRIAGE_ERROR emitted
        error_events = capture_events.find("triage_error")
        assert len(error_events) >= 1

        err_payload = error_events[0]
        assert err_payload["error_type"] == "process_failed"
        assert err_payload["message_id"] == "msg_event_err"

        # EVENT_CYCLE_COMPLETED still emitted despite errors
        completed_events = capture_events.find("cycle_completed")
        assert len(completed_events) == 1
        assert completed_events[0]["errors"] >= 1
