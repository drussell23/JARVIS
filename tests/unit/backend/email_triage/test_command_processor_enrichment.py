"""Tests for triage enrichment integration in the command processor.

These tests verify the enrichment contract — that enrich_with_triage()
produces the fields the compose template expects. They test the enrichment
function directly (not the command processor), since the CP is too large
to unit-test in isolation.
"""

import os
import sys
import time
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "backend"))

from autonomy.email_triage.enrichment import enrich_with_triage
from autonomy.email_triage.schemas import (
    EmailFeatures, ScoringResult, TriagedEmail, TriageCycleReport,
)


def _make_triaged(message_id: str, tier: int, score: int) -> TriagedEmail:
    features = EmailFeatures(
        message_id=message_id, sender="a@b.com", sender_domain="b.com",
        subject="Test", snippet="snippet", is_reply=False, has_attachment=False,
        label_ids=(), keywords=(), sender_frequency="first_time",
        urgency_signals=(), extraction_confidence=0.0,
    )
    scoring = ScoringResult(
        score=score, tier=tier, tier_label=f"jarvis/tier{tier}_label",
        breakdown={}, idempotency_key=f"key_{message_id}",
    )
    return TriagedEmail(
        features=features, scoring=scoring,
        notification_action="label_only", processed_at=1000.0,
    )


def _mock_runner(triaged_emails: dict, report_at=None, schema_version="1.0"):
    """Build a mock runner with get_triage_snapshot() accessor."""
    runner = MagicMock()
    _report_at = report_at if report_at is not None else time.monotonic()
    _report = TriageCycleReport(
        cycle_id="test_cycle",
        started_at=time.time() - 5.0,
        completed_at=time.time(),
        emails_fetched=len(triaged_emails),
        emails_processed=len(triaged_emails),
        tier_counts={},
        notifications_sent=0,
        notifications_suppressed=0,
        errors=[],
    )

    def get_triage_snapshot(staleness_window_s=None):
        window = staleness_window_s if staleness_window_s is not None else 120.0
        age = time.monotonic() - _report_at
        if age > window:
            return None
        return {
            "report": _report,
            "triaged_emails": dict(triaged_emails),
            "schema_version": schema_version,
            "age_s": age,
        }

    runner.get_triage_snapshot = get_triage_snapshot
    return runner


class TestCommandProcessorEnrichmentContract:
    """Enrichment integration matches the command processor contract."""

    def test_enrichment_adds_structured_context(self):
        """Enriched emails have triage fields suitable for compose context."""
        emails = [{"id": "msg_1", "subject": "Urgent"}]
        runner = _mock_runner({
            "msg_1": _make_triaged("msg_1", tier=1, score=92),
        })
        result, enriched, age = enrich_with_triage(emails, runner, staleness_window_s=120.0)
        assert enriched is True
        assert result[0]["triage_tier"] == 1
        assert result[0]["triage_score"] == 92
        assert result[0]["triage_tier_label"] == "jarvis/tier1_label"
        assert "triage_action" in result[0]

    def test_unenriched_emails_have_no_triage_fields(self):
        """When triage unavailable, emails have no triage_* keys."""
        emails = [{"id": "msg_1", "subject": "Hello"}]
        result, enriched, age = enrich_with_triage(emails, None, staleness_window_s=120.0)
        assert enriched is False
        assert "triage_tier" not in result[0]

    def test_compose_context_can_build_tier_summary(self):
        """Enriched email list supports building tier summary for compose."""
        emails = [
            {"id": f"msg_{i}", "subject": f"Email {i}"}
            for i in range(5)
        ]
        runner = _mock_runner({
            "msg_0": _make_triaged("msg_0", 1, 95),
            "msg_1": _make_triaged("msg_1", 2, 72),
            "msg_2": _make_triaged("msg_2", 3, 45),
            "msg_3": _make_triaged("msg_3", 3, 40),
            "msg_4": _make_triaged("msg_4", 4, 15),
        })
        result, enriched, age = enrich_with_triage(emails, runner, staleness_window_s=120.0)
        # Build tier summary like compose would
        tier_summary = {}
        for e in result:
            t = e.get("triage_tier")
            if t is not None:
                tier_summary[t] = tier_summary.get(t, 0) + 1
        assert tier_summary == {1: 1, 2: 1, 3: 2, 4: 1}

    def test_triage_available_and_age_fields(self):
        """Test the triage_available and triage_age_s fields the CP would set."""
        emails = [{"id": "msg_1", "subject": "Test"}]
        runner = _mock_runner({
            "msg_1": _make_triaged("msg_1", tier=2, score=75),
        })
        result, enriched, age = enrich_with_triage(emails, runner, staleness_window_s=120.0)
        assert enriched is True
        assert age is not None
        # CP would set these on _artifacts:
        triage_available = enriched
        triage_age_s = round(age, 1) if age else None
        assert triage_available is True
        assert triage_age_s is not None
        assert triage_age_s >= 0.0

    def test_graceful_when_runner_is_none(self):
        """No runner (pre-first-cycle) returns unenriched safely."""
        emails = [{"id": "msg_1", "subject": "Test"}]
        result, enriched, age = enrich_with_triage(emails, None, staleness_window_s=120.0)
        assert result is emails
        assert enriched is False
        assert age is None
