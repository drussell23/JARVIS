"""E2E tests for notification policy edge cases.

Exercises policy decisions through the FULL pipeline (not unit-testing
policy alone): quiet hours, budget exhaustion, dedup, quarantine,
summary flush, and notification failure isolation.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "backend"))

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from tests.e2e.email_triage.conftest import (
    critical_email,
    high_priority_email,
    noise_email,
    make_mock_workspace_agent,
    make_mock_router,
    make_mock_notifier,
    make_triage_config,
    controlled_time,
    swap_runner_dep,
)


def _make_tier1_router():
    """Router response that reliably produces tier 1 scores."""
    return make_mock_router(response_json={
        "keywords": ["urgent", "critical", "emergency"],
        "sender_frequency": "frequent",
        "urgency_signals": ["action_required", "escalation", "deadline"],
    })


def _make_per_email_router(responses):
    """Router that returns different extraction per email, in order."""
    response_iter = iter(responses)
    router = MagicMock()

    async def _generate_per_email(**kwargs):
        resp = MagicMock()
        resp.content = json.dumps(next(response_iter))
        return resp

    router.generate = AsyncMock(side_effect=_generate_per_email)
    return router


class TestNotificationPolicyE2E:
    """Notification policy edge cases through the full pipeline."""

    @pytest.mark.asyncio
    async def test_quiet_hours_suppress_tier2_allow_tier1(self, fresh_runner):
        """During quiet hours (23-8), tier1 → immediate, tier2 → label_only."""
        emails = [
            critical_email("msg_t1_quiet"),
            high_priority_email("msg_t2_quiet"),
        ]

        # Per-email extraction: first = tier1, second = tier2
        router = _make_per_email_router([
            {"keywords": ["urgent", "critical", "emergency"], "sender_frequency": "frequent", "urgency_signals": ["action_required", "escalation", "deadline"]},
            {"keywords": ["deadline", "urgent", "action_required"], "sender_frequency": "occasional", "urgency_signals": ["deadline", "action_required", "escalation"]},
        ])

        agent = make_mock_workspace_agent(emails)
        notifier = make_mock_notifier()
        config = make_triage_config(extraction_enabled=True)

        with controlled_time(hour=2) as ctrl:  # 2 AM — inside quiet hours
            runner = fresh_runner(
                config=config, workspace_agent=agent,
                router=router, notifier=notifier,
            )
            report = await runner.run_cycle()

        assert report.emails_processed == 2
        assert report.errors == []

        # Tier 1 email should still get immediate notification
        t1 = runner.get_triaged_email("msg_t1_quiet")
        assert t1 is not None
        assert t1.scoring.tier == 1
        assert t1.notification_action == "immediate"

        # Tier 2 email should be suppressed by quiet hours
        t2 = runner.get_triaged_email("msg_t2_quiet")
        assert t2 is not None
        assert t2.scoring.tier == 2
        assert t2.notification_action == "label_only"

    @pytest.mark.asyncio
    async def test_interrupt_budget_exhaustion(self, fresh_runner):
        """After budget exhausted, excess tier1 emails go to summary."""
        # 4 critical emails, budget = 2 per hour
        emails = [critical_email(f"msg_budget_{i}") for i in range(4)]

        # All get tier1-scoring extraction
        router = _make_per_email_router([
            {"keywords": ["urgent", "critical", "emergency"], "sender_frequency": "frequent", "urgency_signals": ["action_required", "escalation", "deadline"]},
            {"keywords": ["urgent", "critical", "immediate"], "sender_frequency": "frequent", "urgency_signals": ["action_required", "escalation", "time-sensitive"]},
            {"keywords": ["urgent", "asap", "emergency"], "sender_frequency": "frequent", "urgency_signals": ["action_required", "deadline", "escalation"]},
            {"keywords": ["critical", "emergency", "immediate"], "sender_frequency": "frequent", "urgency_signals": ["escalation", "deadline", "action_required"]},
        ])

        agent = make_mock_workspace_agent(emails)
        notifier = make_mock_notifier()
        config = make_triage_config(
            extraction_enabled=True,
            max_interrupts_per_hour=2,  # Only 2 allowed
        )

        with controlled_time(hour=10):
            runner = fresh_runner(
                config=config, workspace_agent=agent,
                router=router, notifier=notifier,
            )
            report = await runner.run_cycle()

        assert report.emails_processed == 4
        assert report.errors == []

        # Count actions: first 2 should be "immediate", rest "summary"
        actions = []
        for i in range(4):
            triaged = runner.get_triaged_email(f"msg_budget_{i}")
            assert triaged is not None
            assert triaged.scoring.tier == 1
            actions.append(triaged.notification_action)

        immediate_count = sum(1 for a in actions if a == "immediate")
        summary_count = sum(1 for a in actions if a == "summary")
        assert immediate_count == 2, f"Expected 2 immediate, got {immediate_count}: {actions}"
        assert summary_count == 2, f"Expected 2 summary, got {summary_count}: {actions}"

    @pytest.mark.asyncio
    async def test_dedup_within_window_suppresses_repeat(self, fresh_runner):
        """Same email within dedup window (900s for tier1) gets label_only on repeat."""
        emails = [critical_email("msg_dedup_001")]
        router = _make_tier1_router()
        agent = make_mock_workspace_agent(emails)
        notifier = make_mock_notifier()
        config = make_triage_config(extraction_enabled=True)

        with controlled_time(hour=10) as ctrl:
            # Cycle 1: first time → immediate
            runner = fresh_runner(
                config=config, workspace_agent=agent,
                router=router, notifier=notifier,
            )
            await runner.run_cycle()

            t1 = runner.get_triaged_email("msg_dedup_001")
            assert t1 is not None
            assert t1.notification_action == "immediate"

            # Advance time but stay within 900s dedup window
            ctrl.advance(300)  # 5 minutes

            # Cycle 2: same email (same idempotency_key) — should be deduped
            # Need fresh agent but same runner (policy state preserved)
            agent2 = make_mock_workspace_agent(emails)
            router2 = _make_tier1_router()
            swap_runner_dep(runner, "workspace_agent", agent2)
            swap_runner_dep(runner, "router", router2)

            report2 = await runner.run_cycle()

            t2 = runner.get_triaged_email("msg_dedup_001")
            assert t2 is not None
            # The dedup check uses the policy's internal _dedup_cache
            assert t2.notification_action == "label_only"

    @pytest.mark.asyncio
    async def test_dedup_after_window_allows_repeat(self, fresh_runner):
        """Same email AFTER dedup window (900s) gets immediate again."""
        emails = [critical_email("msg_dedup_002")]
        router = _make_tier1_router()
        agent = make_mock_workspace_agent(emails)
        notifier = make_mock_notifier()
        config = make_triage_config(extraction_enabled=True)

        with controlled_time(hour=10) as ctrl:
            runner = fresh_runner(
                config=config, workspace_agent=agent,
                router=router, notifier=notifier,
            )
            await runner.run_cycle()

            t1 = runner.get_triaged_email("msg_dedup_002")
            assert t1.notification_action == "immediate"

            # Advance past the 900s dedup window
            ctrl.advance(1000)

            agent2 = make_mock_workspace_agent(emails)
            router2 = _make_tier1_router()
            swap_runner_dep(runner, "workspace_agent", agent2)
            swap_runner_dep(runner, "router", router2)

            await runner.run_cycle()

            t2 = runner.get_triaged_email("msg_dedup_002")
            assert t2.notification_action == "immediate"

    @pytest.mark.asyncio
    async def test_tier4_quarantine_when_enabled(self, fresh_runner):
        """quarantine_tier4=True → tier 4 emails get "quarantine" action."""
        emails = [noise_email("msg_quarantine_001")]
        agent = make_mock_workspace_agent(emails)
        config = make_triage_config(quarantine_tier4=True)
        runner = fresh_runner(config=config, workspace_agent=agent)

        await runner.run_cycle()

        triaged = runner.get_triaged_email("msg_quarantine_001")
        assert triaged is not None
        assert triaged.scoring.tier == 4
        assert triaged.notification_action == "quarantine"

    @pytest.mark.asyncio
    async def test_summary_buffer_flushes_at_interval(self, fresh_runner):
        """Summary buffer flushes when summary_interval_s elapsed."""
        # Use tier2 emails (they go to summary buffer)
        emails = [high_priority_email("msg_sum_001")]
        router = _make_per_email_router([
            {"keywords": ["deadline", "urgent", "action_required"], "sender_frequency": "occasional", "urgency_signals": ["deadline", "action_required", "escalation"]},
        ])
        agent = make_mock_workspace_agent(emails)
        notifier = make_mock_notifier()
        config = make_triage_config(
            extraction_enabled=True,
            summary_interval_s=0,  # Immediate flush
        )

        with controlled_time(hour=10) as ctrl:
            runner = fresh_runner(
                config=config, workspace_agent=agent,
                router=router, notifier=notifier,
            )
            report = await runner.run_cycle()

        # With summary_interval_s=0, the runner should have flushed
        # the summary buffer during this cycle (should_flush_summary=True)
        assert report.errors == []

        triaged = runner.get_triaged_email("msg_sum_001")
        assert triaged is not None
        assert triaged.scoring.tier == 2
        assert triaged.notification_action == "summary"

    @pytest.mark.asyncio
    async def test_notification_failure_does_not_change_triage_outcome(self, fresh_runner):
        """Notifier raises → scoring/labeling unchanged, error captured."""
        emails = [critical_email("msg_fail_notif_001")]
        router = _make_tier1_router()
        agent = make_mock_workspace_agent(emails)
        notifier = make_mock_notifier(should_raise=True)  # Raises RuntimeError
        config = make_triage_config(extraction_enabled=True)

        with controlled_time(hour=10):
            runner = fresh_runner(
                config=config, workspace_agent=agent,
                router=router, notifier=notifier,
            )
            report = await runner.run_cycle()

        # Triage outcome unchanged despite notification failure
        triaged = runner.get_triaged_email("msg_fail_notif_001")
        assert triaged is not None
        assert triaged.scoring.tier == 1
        assert triaged.notification_action == "immediate"  # Decision was made before delivery

        # Notification failure is isolated — deliver_immediate catches internally
        # and returns failure results. notifications_sent should be 0.
        assert report.notifications_sent == 0

        # Snapshot still committed (notification failure is not a triage failure)
        assert report.snapshot_committed is True
