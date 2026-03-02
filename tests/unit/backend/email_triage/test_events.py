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
    EVENT_DEPENDENCY_UNAVAILABLE,
    EVENT_DEPENDENCY_DEGRADED,
    EVENT_NOTIFICATION_DELIVERY_RESULT,
    _ERROR_EVENTS,
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


class TestNewEventConstants:
    """Three new event constants are defined with correct values."""

    def test_dependency_unavailable_constant(self):
        assert EVENT_DEPENDENCY_UNAVAILABLE == "dependency_unavailable"

    def test_dependency_degraded_constant(self):
        assert EVENT_DEPENDENCY_DEGRADED == "dependency_degraded"

    def test_notification_delivery_result_constant(self):
        assert EVENT_NOTIFICATION_DELIVERY_RESULT == "notification_delivery_result"

    def test_dependency_unavailable_in_error_events(self):
        assert EVENT_DEPENDENCY_UNAVAILABLE in _ERROR_EVENTS

    def test_dependency_degraded_not_in_error_events(self):
        assert EVENT_DEPENDENCY_DEGRADED not in _ERROR_EVENTS

    def test_notification_delivery_result_not_in_error_events(self):
        assert EVENT_NOTIFICATION_DELIVERY_RESULT not in _ERROR_EVENTS

    def test_error_events_contains_triage_error(self):
        """Existing membership unchanged."""
        assert EVENT_TRIAGE_ERROR in _ERROR_EVENTS

    def test_error_events_is_frozenset(self):
        assert isinstance(_ERROR_EVENTS, frozenset)


class TestNewEventEmission:
    """New events emit at the correct log level via emit_triage_event()."""

    def test_dependency_unavailable_emits_warning(self, caplog):
        with caplog.at_level(logging.DEBUG, logger="jarvis.email_triage"):
            emit_triage_event(EVENT_DEPENDENCY_UNAVAILABLE, {
                "service": "cloud_sql",
                "reason": "connection_refused",
            })
        found = [r for r in caplog.records if "dependency_unavailable" in r.message]
        assert len(found) == 1
        assert found[0].levelno == logging.WARNING

    def test_dependency_degraded_emits_info(self, caplog):
        with caplog.at_level(logging.DEBUG, logger="jarvis.email_triage"):
            emit_triage_event(EVENT_DEPENDENCY_DEGRADED, {
                "service": "jprime",
                "fallback": "claude_api",
            })
        found = [r for r in caplog.records if "dependency_degraded" in r.message]
        assert len(found) == 1
        assert found[0].levelno == logging.INFO

    def test_notification_delivery_result_emits_info(self, caplog):
        with caplog.at_level(logging.DEBUG, logger="jarvis.email_triage"):
            emit_triage_event(EVENT_NOTIFICATION_DELIVERY_RESULT, {
                "channel": "voice",
                "success": True,
            })
        found = [r for r in caplog.records if "notification_delivery_result" in r.message]
        assert len(found) == 1
        assert found[0].levelno == logging.INFO

    def test_dependency_unavailable_payload_preserved(self, caplog):
        with caplog.at_level(logging.DEBUG, logger="jarvis.email_triage"):
            emit_triage_event(EVENT_DEPENDENCY_UNAVAILABLE, {
                "service": "cloud_sql",
                "reason": "timeout",
            })
        found = [r for r in caplog.records if "dependency_unavailable" in r.message]
        data = json.loads(found[0].message)
        assert data["event"] == "dependency_unavailable"
        assert data["service"] == "cloud_sql"
        assert data["reason"] == "timeout"
        assert "timestamp" in data


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
