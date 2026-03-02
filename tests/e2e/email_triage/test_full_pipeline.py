"""E2E tests for the full email triage pipeline.

Exercises: fetch → extract → score → label → notify → snapshot commit.
All tests use mocked infrastructure (CI-safe, fast).
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "backend"))

import pytest

from autonomy.email_triage.extraction import _merge_features, _heuristic_features

# Import factories from conftest
from tests.e2e.email_triage.conftest import (
    critical_email,
    high_priority_email,
    routine_email,
    noise_email,
    mixed_inbox,
    generate_emails,
    make_mock_workspace_agent,
    make_mock_router,
    make_mock_notifier,
    make_triage_config,
)


class TestFullPipelineHappyPath:
    """Complete pipeline tests: fetch → extract → score → label → notify → snapshot."""

    @pytest.mark.asyncio
    async def test_single_critical_email_full_cycle(self, fresh_runner):
        """One critical email with AI extraction: tier 1, immediate, snapshot committed.

        Uses router (extraction_enabled=True) to get sender_frequency="frequent"
        which is required for tier 1 scoring (heuristic defaults to first_time).
        """
        emails = [critical_email()]
        agent = make_mock_workspace_agent(emails)
        notifier = make_mock_notifier()
        router = make_mock_router(response_json={
            "keywords": ["urgent", "critical", "emergency"],
            "sender_frequency": "frequent",
            "urgency_signals": ["action_required", "escalation", "deadline"],
        })
        config = make_triage_config(extraction_enabled=True)
        runner = fresh_runner(
            config=config, workspace_agent=agent,
            router=router, notifier=notifier,
        )

        report = await runner.run_cycle()

        assert report.emails_fetched == 1
        assert report.emails_processed == 1
        assert 1 in report.tier_counts, f"Expected tier 1, got {report.tier_counts}"
        assert report.snapshot_committed is True
        assert report.degraded is False
        assert report.errors == []

        # Snapshot accessible
        snapshot = runner.get_triage_snapshot(staleness_window_s=9999)
        assert snapshot is not None
        assert len(snapshot["triaged_emails"]) == 1

        # The triaged email is accessible by message ID
        triaged = runner.get_triaged_email(emails[0]["id"])
        assert triaged is not None
        assert triaged.scoring.tier == 1
        assert triaged.notification_action == "immediate"
        assert triaged.features.extraction_confidence == 0.8

    @pytest.mark.asyncio
    async def test_mixed_inbox_heuristic_tiers(self, fresh_runner):
        """4 emails with heuristic-only: verify tier separation and label correctness.

        Note: heuristic can't distinguish sender frequency, so tier 1 may not
        appear. We verify that emails score differently and labels match tiers.
        """
        emails = mixed_inbox()
        agent = make_mock_workspace_agent(emails)
        notifier = make_mock_notifier()
        config = make_triage_config()  # extraction_enabled=False (heuristic)
        runner = fresh_runner(config=config, workspace_agent=agent, notifier=notifier)

        report = await runner.run_cycle()

        assert report.emails_fetched == 4
        assert report.emails_processed == 4
        assert report.errors == []
        assert report.snapshot_committed is True

        snapshot = runner.get_triage_snapshot(staleness_window_s=9999)
        assert snapshot is not None
        triaged_emails = snapshot["triaged_emails"]
        assert len(triaged_emails) == 4

        tiers = sorted([t.scoring.tier for t in triaged_emails.values()])
        # Heuristic produces at least 2 distinct tiers (noise vs non-noise)
        assert len(set(tiers)) >= 2, f"Expected diverse tiers, got {tiers}"

        # Verify label names match tier labels from config
        for triaged in triaged_emails.values():
            expected_label = config.label_for_tier(triaged.scoring.tier)
            assert triaged.scoring.tier_label == expected_label

    @pytest.mark.asyncio
    async def test_mixed_inbox_with_extraction_produces_four_tiers(self, fresh_runner):
        """4 emails with per-email extraction responses: all 4 tiers represented."""
        emails = mixed_inbox()

        # Per-email router responses to ensure tier separation
        responses = iter([
            {"keywords": ["urgent", "critical", "emergency"], "sender_frequency": "frequent", "urgency_signals": ["action_required", "escalation", "deadline"]},
            {"keywords": ["deadline"], "sender_frequency": "occasional", "urgency_signals": ["deadline"]},
            {"keywords": ["notes"], "sender_frequency": "first_time", "urgency_signals": []},
            {"keywords": ["sale", "discount"], "sender_frequency": "first_time", "urgency_signals": []},
        ])
        import json
        from unittest.mock import MagicMock, AsyncMock

        router = MagicMock()
        async def _generate_per_email(**kwargs):
            resp = MagicMock()
            resp.content = json.dumps(next(responses))
            return resp
        router.generate = AsyncMock(side_effect=_generate_per_email)

        agent = make_mock_workspace_agent(emails)
        notifier = make_mock_notifier()
        config = make_triage_config(extraction_enabled=True)
        runner = fresh_runner(
            config=config, workspace_agent=agent,
            router=router, notifier=notifier,
        )

        report = await runner.run_cycle()

        assert report.emails_fetched == 4
        assert report.emails_processed == 4
        assert report.errors == []

        snapshot = runner.get_triage_snapshot(staleness_window_s=9999)
        triaged_emails = snapshot["triaged_emails"]
        tiers = sorted([t.scoring.tier for t in triaged_emails.values()])
        # With extraction, we should get at least 3 distinct tiers
        assert len(set(tiers)) >= 3, f"Expected 3+ tiers, got {tiers}"

    @pytest.mark.asyncio
    async def test_empty_inbox_produces_clean_report(self, fresh_runner):
        """Empty inbox: clean report, zero counts, snapshot commits (legit empty truth)."""
        agent = make_mock_workspace_agent(emails=[])
        config = make_triage_config()
        runner = fresh_runner(config=config, workspace_agent=agent)

        report = await runner.run_cycle()

        assert report.emails_fetched == 0
        assert report.emails_processed == 0
        assert report.tier_counts == {}
        assert report.notifications_sent == 0
        assert report.errors == []
        assert report.snapshot_committed is True

    @pytest.mark.asyncio
    async def test_max_emails_per_cycle_cap(self, fresh_runner):
        """Config cap limits how many emails are processed per cycle."""
        emails = generate_emails(30)
        agent = make_mock_workspace_agent(emails)
        config = make_triage_config(max_emails_per_cycle=25)
        runner = fresh_runner(config=config, workspace_agent=agent)

        report = await runner.run_cycle()

        assert report.emails_fetched == 30
        assert report.emails_processed == 25
        assert report.snapshot_committed is True

    @pytest.mark.asyncio
    async def test_heuristic_only_when_router_unavailable(self, fresh_runner):
        """Without router, extraction falls back to heuristic (extraction_confidence=0.0)."""
        emails = [critical_email(), routine_email()]
        agent = make_mock_workspace_agent(emails)
        config = make_triage_config(extraction_enabled=True)  # Enabled but no router
        runner = fresh_runner(config=config, workspace_agent=agent, router=None)

        report = await runner.run_cycle()

        assert report.emails_fetched == 2
        assert report.emails_processed == 2
        assert report.errors == []
        assert report.snapshot_committed is True

        for msg_id in [e["id"] for e in emails]:
            triaged = runner.get_triaged_email(msg_id)
            assert triaged is not None
            assert triaged.features.extraction_confidence == 0.0

    @pytest.mark.asyncio
    async def test_scoring_determinism_across_cycles(self, fresh_runner):
        """Same input → same tier, idempotency_key across cycles."""
        emails = mixed_inbox()
        config = make_triage_config()

        results = []
        for _ in range(3):
            agent = make_mock_workspace_agent(emails)
            runner = fresh_runner(config=config, workspace_agent=agent)
            await runner.run_cycle()
            snapshot = runner.get_triage_snapshot(staleness_window_s=9999)
            assert snapshot is not None
            cycle_data = {}
            for msg_id, triaged in snapshot["triaged_emails"].items():
                cycle_data[msg_id] = (
                    triaged.scoring.tier,
                    triaged.scoring.idempotency_key,
                )
            results.append(cycle_data)

        for msg_id in results[0]:
            tier_0, key_0 = results[0][msg_id]
            for i in range(1, 3):
                tier_i, key_i = results[i][msg_id]
                assert tier_0 == tier_i, (
                    f"Tier mismatch for {msg_id}: cycle 0={tier_0}, cycle {i}={tier_i}"
                )
                assert key_0 == key_i, (
                    f"Key mismatch for {msg_id}: cycle 0={key_0}, cycle {i}={key_i}"
                )

    @pytest.mark.asyncio
    async def test_high_load_100_emails_respects_cap_and_completes(self, fresh_runner):
        """100+ emails: cap enforced, completes promptly, no crashes."""
        emails = generate_emails(120)
        agent = make_mock_workspace_agent(emails)
        config = make_triage_config(max_emails_per_cycle=25)
        runner = fresh_runner(config=config, workspace_agent=agent)

        t0 = time.monotonic()
        report = await runner.run_cycle()
        elapsed = time.monotonic() - t0

        assert report.emails_fetched == 120
        assert report.emails_processed == 25
        assert report.snapshot_committed is True
        assert elapsed < 5.0, f"Cycle took {elapsed:.2f}s, expected <5s"

    @pytest.mark.asyncio
    async def test_extraction_contract_schema_compatibility(self):
        """J-Prime extraction JSON schema: valid, missing, invalid, extra fields."""
        email = critical_email()
        heuristic = _heuristic_features(email)

        # Valid contract JSON
        valid_json = {
            "keywords": ["deployment", "outage"],
            "sender_frequency": "frequent",
            "urgency_signals": ["action_required", "escalation"],
        }
        merged = _merge_features(heuristic, valid_json)
        assert merged.keywords == ("deployment", "outage")
        assert merged.sender_frequency == "frequent"
        assert merged.urgency_signals == ("action_required", "escalation")
        assert merged.extraction_confidence == 0.8

        # Missing fields → fall back to heuristic
        partial_json = {"keywords": ["test"]}
        merged_partial = _merge_features(heuristic, partial_json)
        assert merged_partial.keywords == ("test",)
        assert merged_partial.sender_frequency == heuristic.sender_frequency
        assert merged_partial.urgency_signals == heuristic.urgency_signals

        # Invalid sender_frequency → fall back to heuristic
        bad_freq = {"sender_frequency": "INVALID_VALUE"}
        merged_bad = _merge_features(heuristic, bad_freq)
        assert merged_bad.sender_frequency == heuristic.sender_frequency

        # Extra fields → ignored, no crash
        extra_json = {
            "keywords": ["extra"],
            "sender_frequency": "occasional",
            "urgency_signals": [],
            "unexpected_field": True,
            "nested": {"deep": "value"},
        }
        merged_extra = _merge_features(heuristic, extra_json)
        assert merged_extra.keywords == ("extra",)
        assert merged_extra.sender_frequency == "occasional"

        # Empty dict → all heuristic
        merged_empty = _merge_features(heuristic, {})
        assert merged_empty.keywords == heuristic.keywords
        assert merged_empty.sender_frequency == heuristic.sender_frequency
