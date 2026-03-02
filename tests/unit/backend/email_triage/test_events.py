"""Tests for email triage observability events."""

import logging
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "backend"))

from autonomy.email_triage.events import (
    emit_triage_event,
    EVENT_CYCLE_STARTED,
    EVENT_EMAIL_TRIAGED,
    EVENT_NOTIFICATION_SENT,
    EVENT_NOTIFICATION_SUPPRESSED,
    EVENT_SUMMARY_FLUSHED,
    EVENT_CYCLE_COMPLETED,
    EVENT_TRIAGE_ERROR,
)


class TestEventConstants:
    """All 7 event type constants are defined."""

    def test_all_event_types_defined(self):
        assert EVENT_CYCLE_STARTED == "triage_cycle_started"
        assert EVENT_EMAIL_TRIAGED == "email_triaged"
        assert EVENT_NOTIFICATION_SENT == "notification_sent"
        assert EVENT_NOTIFICATION_SUPPRESSED == "notification_suppressed"
        assert EVENT_SUMMARY_FLUSHED == "summary_flushed"
        assert EVENT_CYCLE_COMPLETED == "triage_cycle_completed"
        assert EVENT_TRIAGE_ERROR == "triage_error"


class TestEmitTriageEvent:
    """emit_triage_event() emits structured JSON to the logger."""

    def test_emits_to_logger(self, caplog):
        with caplog.at_level(logging.INFO, logger="jarvis.email_triage"):
            emit_triage_event(EVENT_CYCLE_STARTED, {"cycle_id": "c1"})
        assert any("triage_cycle_started" in r.message for r in caplog.records)

    def test_payload_in_log(self, caplog):
        with caplog.at_level(logging.INFO, logger="jarvis.email_triage"):
            emit_triage_event(EVENT_EMAIL_TRIAGED, {
                "message_id": "m1",
                "score": 87,
                "tier": 1,
            })
        found = [r for r in caplog.records if "email_triaged" in r.message]
        assert len(found) == 1
        assert "m1" in found[0].message

    def test_error_event(self, caplog):
        with caplog.at_level(logging.WARNING, logger="jarvis.email_triage"):
            emit_triage_event(EVENT_TRIAGE_ERROR, {
                "cycle_id": "c1",
                "error_type": "extraction_failed",
                "message": "J-Prime returned invalid JSON",
            })
        found = [r for r in caplog.records if "triage_error" in r.message]
        assert len(found) == 1

    def test_event_includes_timestamp(self, caplog):
        with caplog.at_level(logging.INFO, logger="jarvis.email_triage"):
            emit_triage_event(EVENT_CYCLE_COMPLETED, {"cycle_id": "c1"})
        found = [r for r in caplog.records if "triage_cycle_completed" in r.message]
        assert len(found) == 1
        msg = found[0].message
        data = json.loads(msg)
        assert "timestamp" in data
        assert "event" in data
