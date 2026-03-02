"""Tests for workspace post-action verification contract (Section 4, v_autonomy).

Validates _verify_workspace_result and _normalize_workspace_result against
the contract defined in WORKSPACE_RESULT_CONTRACT_VERSION v1.
"""

import sys
import os

# Ensure the repo root is on sys.path so backend.api imports resolve.
_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

from backend.api.unified_command_processor import (
    _verify_workspace_result,
    _normalize_workspace_result,
    WORKSPACE_RESULT_CONTRACT_VERSION,
)


# ── fetch_unread_emails ──────────────────────────────────────────────

class TestFetchUnreadEmails:
    def test_valid_emails_passes(self):
        result = {
            "emails": [
                {"subject": "Hello", "from": "alice@test.com", "snippet": "Hi there"},
                {"subject": "Meeting", "from": "bob@test.com", "snippet": "See you"},
            ]
        }
        code, out = _verify_workspace_result("fetch_unread_emails", result)
        assert code == "verify_passed"
        assert out["_verification"]["passed"] is True
        assert out["_verification"]["contract_version"] == WORKSPACE_RESULT_CONTRACT_VERSION

    def test_empty_list_is_valid(self):
        result = {"emails": []}
        code, out = _verify_workspace_result("fetch_unread_emails", result)
        assert code == "verify_empty_valid"
        assert out["_verification"]["passed"] is True

    def test_missing_emails_key_fails_schema(self):
        result = {"messages": []}
        code, out = _verify_workspace_result("fetch_unread_emails", result)
        assert code == "verify_schema_fail"
        assert out["_verification"]["passed"] is False
        assert "missing_key:emails" in out["_verification"]["failed_check"]

    def test_wrong_type_fails_schema(self):
        result = {"emails": "not-a-list"}
        code, out = _verify_workspace_result("fetch_unread_emails", result)
        assert code == "verify_schema_fail"
        assert out["_verification"]["passed"] is False
        assert "type_mismatch:emails" in out["_verification"]["failed_check"]

    def test_items_missing_required_keys_fails_semantic(self):
        result = {
            "emails": [
                {"subject": "Hello"},  # missing "from"
            ]
        }
        code, out = _verify_workspace_result("fetch_unread_emails", result)
        assert code == "verify_semantic_fail"
        assert out["_verification"]["passed"] is False


# ── send_email ───────────────────────────────────────────────────────

class TestSendEmail:
    def test_valid_message_id_passes(self):
        result = {"message_id": "abc123"}
        code, out = _verify_workspace_result("send_email", result)
        assert code == "verify_passed"
        assert out["_verification"]["passed"] is True

    def test_empty_message_id_fails_semantic(self):
        result = {"message_id": ""}
        code, out = _verify_workspace_result("send_email", result)
        assert code == "verify_semantic_fail"
        assert out["_verification"]["passed"] is False
        assert out["_verification"]["failed_check"] == "semantic_check"


# ── check_calendar_events ────────────────────────────────────────────

class TestCheckCalendarEvents:
    def test_valid_events_passes(self):
        result = {
            "events": [
                {"title": "Standup", "start": "2026-03-01T09:00:00Z"},
            ]
        }
        code, out = _verify_workspace_result("check_calendar_events", result)
        assert code == "verify_passed"
        assert out["_verification"]["passed"] is True


# ── unknown action ───────────────────────────────────────────────────

class TestUnknownAction:
    def test_unknown_action_always_passes(self):
        result = {"foo": "bar", "baz": 42}
        code, out = _verify_workspace_result("totally_unknown_action", result)
        assert code == "verify_passed"
        assert out["_verification"]["passed"] is True


# ── transport failure ────────────────────────────────────────────────

class TestTransportFailure:
    def test_error_with_success_false(self):
        result = {"error": "connection refused", "success": False}
        code, out = _verify_workspace_result("fetch_unread_emails", result)
        assert code == "verify_transport_fail"
        assert out["_verification"]["passed"] is False


# ── normalization ────────────────────────────────────────────────────

class TestNormalization:
    def test_calendar_summary_mapped_to_title(self):
        result = {
            "events": [
                {"summary": "Standup", "start": "2026-03-01T09:00:00Z"},
            ]
        }
        normalized = _normalize_workspace_result("check_calendar_events", result)
        assert normalized["events"][0]["title"] == "Standup"

    def test_calendar_summary_preserved_when_title_exists(self):
        result = {
            "events": [
                {"summary": "Old", "title": "Kept", "start": "2026-03-01T09:00:00Z"},
            ]
        }
        normalized = _normalize_workspace_result("check_calendar_events", result)
        assert normalized["events"][0]["title"] == "Kept"

    def test_normalization_enables_calendar_verification(self):
        """End-to-end: summary-only event should pass after normalization."""
        result = {
            "events": [
                {"summary": "Team Sync", "start": "2026-03-01T10:00:00Z"},
            ]
        }
        code, out = _verify_workspace_result("check_calendar_events", result)
        assert code == "verify_passed"
        assert out["events"][0]["title"] == "Team Sync"


# ── non-dict inputs ─────────────────────────────────────────────────

class TestNonDictInputs:
    def test_string_result_wrapped(self):
        code, out = _verify_workspace_result("fetch_unread_emails", "raw string")
        assert "_raw" in out
        assert out["_raw"] == "raw string"
        # Schema check will fail because wrapped dict has no "emails" key
        assert code == "verify_schema_fail"

    def test_none_result_wrapped(self):
        code, out = _verify_workspace_result("send_email", None)
        assert "_raw" in out
        assert code == "verify_schema_fail"
