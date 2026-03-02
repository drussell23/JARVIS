"""Tests for triage enrichment — pure merge of triage metadata into raw emails.

Enrichment is pure: no side effects, no network, no exceptions.
"""

import os
import sys
import time
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "backend"))

from autonomy.email_triage.enrichment import enrich_with_triage
from autonomy.email_triage.schemas import (
    EmailFeatures,
    ScoringResult,
    TriagedEmail,
    TriageCycleReport,
)


def _make_features(message_id: str = "msg_001") -> EmailFeatures:
    """Build EmailFeatures with sensible defaults."""
    return EmailFeatures(
        message_id=message_id,
        sender="alice@example.com",
        sender_domain="example.com",
        subject="Hello",
        snippet="Just checking in",
        is_reply=False,
        has_attachment=False,
        label_ids=("INBOX", "UNREAD"),
        keywords=(),
        sender_frequency="occasional",
        urgency_signals=(),
        extraction_confidence=0.0,
    )


def _make_triaged(message_id: str, tier: int, score: int) -> TriagedEmail:
    """Create a TriagedEmail with EmailFeatures + ScoringResult."""
    tier_labels = {
        1: "jarvis/tier1_critical",
        2: "jarvis/tier2_high",
        3: "jarvis/tier3_review",
        4: "jarvis/tier4_noise",
    }
    features = _make_features(message_id=message_id)
    scoring = ScoringResult(
        score=score,
        tier=tier,
        tier_label=tier_labels.get(tier, f"jarvis/tier{tier}_unknown"),
        breakdown={"sender": 0.5, "content": 0.3, "urgency": 0.1, "context": 0.1},
        idempotency_key=f"idem_{message_id}",
    )
    return TriagedEmail(
        features=features,
        scoring=scoring,
        notification_action="immediate" if tier <= 2 else "label_only",
        processed_at=time.time(),
    )


def _mock_runner(
    triaged_emails: dict,
    report_at=None,
    schema_version: str = "1.0",
) -> MagicMock:
    """Build a mock runner with triage state attributes."""
    runner = MagicMock()
    runner._triaged_emails = triaged_emails
    runner._last_report_at = report_at if report_at is not None else time.monotonic()
    runner._triage_schema_version = schema_version
    runner._last_report = TriageCycleReport(
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
    return runner


class TestEnrichWithTriage:
    """Tests for enrich_with_triage pure function."""

    def test_enriches_matching_emails(self):
        """Two emails matched: check triage_tier, triage_score, etc."""
        t1 = _make_triaged("msg_a", tier=1, score=92)
        t2 = _make_triaged("msg_b", tier=3, score=45)
        triaged = {"msg_a": t1, "msg_b": t2}
        runner = _mock_runner(triaged)

        emails = [
            {"id": "msg_a", "subject": "Urgent"},
            {"id": "msg_b", "subject": "Newsletter"},
        ]

        result, was_enriched, age = enrich_with_triage(emails, runner, staleness_window_s=120.0)

        assert was_enriched is True
        assert age is not None and age >= 0.0

        assert result[0]["triage_tier"] == 1
        assert result[0]["triage_score"] == 92
        assert result[0]["triage_tier_label"] == "jarvis/tier1_critical"
        assert result[0]["triage_action"] == "immediate"

        assert result[1]["triage_tier"] == 3
        assert result[1]["triage_score"] == 45
        assert result[1]["triage_tier_label"] == "jarvis/tier3_review"
        assert result[1]["triage_action"] == "label_only"

    def test_preserves_email_count(self):
        """5 emails in, only 1 matched, still 5 in output."""
        t1 = _make_triaged("msg_c", tier=2, score=78)
        runner = _mock_runner({"msg_c": t1})

        emails = [
            {"id": "msg_a", "subject": "A"},
            {"id": "msg_b", "subject": "B"},
            {"id": "msg_c", "subject": "C"},
            {"id": "msg_d", "subject": "D"},
            {"id": "msg_e", "subject": "E"},
        ]

        result, was_enriched, _age = enrich_with_triage(emails, runner, staleness_window_s=120.0)

        assert len(result) == 5
        assert was_enriched is True

    def test_unmatched_emails_pass_through(self):
        """Unmatched emails have no triage_tier key."""
        t1 = _make_triaged("msg_x", tier=1, score=90)
        runner = _mock_runner({"msg_x": t1})

        emails = [
            {"id": "msg_x", "subject": "Matched"},
            {"id": "msg_y", "subject": "Unmatched"},
        ]

        result, _enriched, _age = enrich_with_triage(emails, runner, staleness_window_s=120.0)

        assert "triage_tier" in result[0]
        assert "triage_tier" not in result[1]
        # Unmatched should be the original dict (not a copy)
        assert result[1] is emails[1]

    def test_preserves_order(self):
        """Output order matches input order."""
        t1 = _make_triaged("msg_b", tier=2, score=70)
        t2 = _make_triaged("msg_d", tier=4, score=20)
        runner = _mock_runner({"msg_b": t1, "msg_d": t2})

        emails = [
            {"id": "msg_a", "subject": "First"},
            {"id": "msg_b", "subject": "Second"},
            {"id": "msg_c", "subject": "Third"},
            {"id": "msg_d", "subject": "Fourth"},
        ]

        result, _enriched, _age = enrich_with_triage(emails, runner, staleness_window_s=120.0)

        assert [e["id"] for e in result] == ["msg_a", "msg_b", "msg_c", "msg_d"]
        assert [e["subject"] for e in result] == ["First", "Second", "Third", "Fourth"]

    def test_runner_none_returns_unenriched(self):
        """runner=None returns (emails, False, None)."""
        emails = [{"id": "msg_a", "subject": "Test"}]
        result, was_enriched, age = enrich_with_triage(emails, None, staleness_window_s=120.0)

        assert result is emails
        assert was_enriched is False
        assert age is None

    def test_stale_results_return_unenriched(self):
        """Report 300s ago, window 120s: stale -> unenriched."""
        t1 = _make_triaged("msg_a", tier=1, score=95)
        runner = _mock_runner(
            {"msg_a": t1},
            report_at=time.monotonic() - 300.0,
        )

        emails = [{"id": "msg_a", "subject": "Stale"}]
        result, was_enriched, age = enrich_with_triage(emails, runner, staleness_window_s=120.0)

        assert result is emails
        assert was_enriched is False
        assert age is None

    def test_incompatible_schema_version_skips(self):
        """Schema version '99.0' is not in compatible set."""
        t1 = _make_triaged("msg_a", tier=1, score=95)
        runner = _mock_runner({"msg_a": t1}, schema_version="99.0")

        emails = [{"id": "msg_a", "subject": "Bad schema"}]
        result, was_enriched, age = enrich_with_triage(emails, runner, staleness_window_s=120.0)

        assert result is emails
        assert was_enriched is False
        assert age is None

    def test_no_last_report_returns_unenriched(self):
        """_last_report = None -> (emails, False, None)."""
        runner = MagicMock()
        runner._triaged_emails = {"msg_a": _make_triaged("msg_a", 1, 90)}
        runner._last_report_at = time.monotonic()
        runner._triage_schema_version = "1.0"
        runner._last_report = None

        emails = [{"id": "msg_a", "subject": "No report"}]
        result, was_enriched, age = enrich_with_triage(emails, runner, staleness_window_s=120.0)

        assert result is emails
        assert was_enriched is False
        assert age is None

    def test_triage_age_is_positive(self):
        """Report at 10s ago -> age >= 9.0."""
        t1 = _make_triaged("msg_a", tier=2, score=70)
        runner = _mock_runner(
            {"msg_a": t1},
            report_at=time.monotonic() - 10.0,
        )

        emails = [{"id": "msg_a", "subject": "Recent"}]
        _result, was_enriched, age = enrich_with_triage(emails, runner, staleness_window_s=120.0)

        assert was_enriched is True
        assert age is not None
        assert age >= 9.0

    def test_does_not_mutate_original_emails(self):
        """Original email dict is unchanged after enrichment."""
        t1 = _make_triaged("msg_a", tier=1, score=95)
        runner = _mock_runner({"msg_a": t1})

        original = {"id": "msg_a", "subject": "Immutable"}
        original_keys = set(original.keys())
        emails = [original]

        result, _enriched, _age = enrich_with_triage(emails, runner, staleness_window_s=120.0)

        # Original dict must not have triage keys
        assert set(original.keys()) == original_keys
        assert "triage_tier" not in original

        # Result is a different dict
        assert result[0] is not original
        assert "triage_tier" in result[0]

    def test_empty_triaged_emails_returns_with_age(self):
        """Empty _triaged_emails returns (emails, False, age)."""
        runner = _mock_runner({}, report_at=time.monotonic() - 5.0)

        emails = [{"id": "msg_a", "subject": "Test"}]
        result, was_enriched, age = enrich_with_triage(emails, runner, staleness_window_s=120.0)

        assert result is emails
        assert was_enriched is False
        assert age is not None
        assert age >= 4.0
