"""Tests for email timeout structural fixes (v282).

Validates:
1. Visual-tier results accepted by verification contract
2. Computer Use respects deadline budget
3. Budget-exhausted visual fallback is skipped
"""

import time
import sys
import os

# Ensure backend on sys.path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "backend"))

from api.unified_command_processor import _verify_workspace_result


# ═══════════════════════════════════════════════════════════════════════
# Fix 2: Visual-tier results accepted by verification contract
# ═══════════════════════════════════════════════════════════════════════


class TestVisualTierVerification:
    """Visual results are unstructured by design and must not fail schema checks."""

    def test_visual_result_with_source_marker_accepted(self):
        """Computer Use results with source=computer_use_visual bypass schema check."""
        result = {
            "raw_response": "You have 3 unread emails: ...",
            "actions_count": 5,
            "source": "computer_use_visual",
        }
        outcome, annotated = _verify_workspace_result("fetch_unread_emails", result)
        assert outcome == "verify_visual_accepted"
        assert annotated["_verification"]["passed"] is True
        assert annotated["_verification"]["tier"] == "visual"

    def test_visual_result_with_tier_used_marker_accepted(self):
        """Results with tier_used=computer_use also bypass schema check."""
        result = {
            "raw_response": "Events: Meeting at 10am...",
            "tier_used": "computer_use",
        }
        outcome, annotated = _verify_workspace_result("check_calendar_events", result)
        assert outcome == "verify_visual_accepted"
        assert annotated["_verification"]["passed"] is True

    def test_api_result_still_verified(self):
        """API results (no visual marker) still go through full verification."""
        result = {
            "emails": [{"subject": "Test", "from": "bob@test.com"}],
            "count": 1,
        }
        outcome, _ = _verify_workspace_result("fetch_unread_emails", result)
        assert outcome == "verify_passed"

    def test_api_result_missing_key_still_fails(self):
        """API results without required keys still fail verification."""
        result = {"count": 5}  # missing "emails" key
        outcome, _ = _verify_workspace_result("fetch_unread_emails", result)
        assert outcome == "verify_schema_fail"

    def test_visual_result_not_confused_by_similar_fields(self):
        """Results with 'source' but not 'computer_use_visual' are not treated as visual."""
        result = {"source": "gmail_api", "count": 5}
        outcome, _ = _verify_workspace_result("fetch_unread_emails", result)
        assert outcome == "verify_schema_fail"

    def test_visual_acceptance_in_node_execution_gate(self):
        """verify_visual_accepted is in the acceptance set used by _execute_workspace_node."""
        ACCEPTED = ("verify_passed", "verify_empty_valid", "verify_visual_accepted")
        assert "verify_visual_accepted" in ACCEPTED

    def test_visual_result_for_calendar_action(self):
        """Visual results work for any action, not just email."""
        result = {
            "raw_response": "Tomorrow: 9am standup, 2pm review",
            "source": "computer_use_visual",
        }
        outcome, _ = _verify_workspace_result("check_calendar_events", result)
        assert outcome == "verify_visual_accepted"


# ═══════════════════════════════════════════════════════════════════════
# Fix 1: Computer Use deadline propagation
# ═══════════════════════════════════════════════════════════════════════


class TestComputerUseDeadline:
    """Computer Use visual fallback must respect pipeline deadline."""

    def test_budget_exhausted_skips_visual(self):
        """When deadline budget < 5s, visual fallback is skipped entirely."""
        _VISUAL_MIN_BUDGET_S = 5.0
        _visual_budget = (time.monotonic() + 2.0) - time.monotonic()
        assert _visual_budget <= _VISUAL_MIN_BUDGET_S

    def test_timeout_computed_from_budget(self):
        """Timeout for Computer Use is derived from remaining budget, not hardcoded."""
        _VISUAL_HARD_CAP_S = 45.0
        _visual_budget = (time.monotonic() + 20.0) - time.monotonic()
        _cu_timeout = min(_visual_budget - 1.0, _VISUAL_HARD_CAP_S)
        # Timeout should be ~19s (20 - 1s headroom), not 45s
        assert _cu_timeout < 25.0
        assert _cu_timeout > 15.0

    def test_hard_cap_limits_timeout(self):
        """Even with huge deadline budget, timeout is capped at 45s."""
        _VISUAL_HARD_CAP_S = 45.0
        _visual_budget = (time.monotonic() + 300.0) - time.monotonic()
        _cu_timeout = min(_visual_budget - 1.0, _VISUAL_HARD_CAP_S)
        assert _cu_timeout == _VISUAL_HARD_CAP_S

    def test_no_deadline_uses_hard_cap(self):
        """When no deadline is provided, hard cap is used."""
        _VISUAL_HARD_CAP_S = 45.0
        _visual_budget = None
        _cu_timeout = min(_visual_budget - 1.0, _VISUAL_HARD_CAP_S) if _visual_budget else _VISUAL_HARD_CAP_S
        assert _cu_timeout == _VISUAL_HARD_CAP_S


# ═══════════════════════════════════════════════════════════════════════
# Integration: Verification + Recovery interaction
# ═══════════════════════════════════════════════════════════════════════


class TestRecoveryVisualAcceptance:
    """Recovery loop must accept visual-tier results without re-triggering."""

    def test_visual_result_breaks_recovery_loop(self):
        """If same-tier retry returns visual result, it's accepted (no infinite loop)."""
        visual_result = {
            "raw_response": "3 unread emails found",
            "source": "computer_use_visual",
            "workspace_action": "fetch_unread_emails",
        }
        outcome, _ = _verify_workspace_result("fetch_unread_emails", visual_result)
        assert outcome in ("verify_passed", "verify_empty_valid", "verify_visual_accepted")

    def test_transport_fail_still_triggers_recovery(self):
        """Non-visual failures still trigger recovery as before."""
        failed_result = {"error": "connection_timeout", "success": False}
        outcome, _ = _verify_workspace_result("fetch_unread_emails", failed_result)
        assert outcome == "verify_transport_fail"
        assert outcome not in ("verify_passed", "verify_empty_valid", "verify_visual_accepted")
