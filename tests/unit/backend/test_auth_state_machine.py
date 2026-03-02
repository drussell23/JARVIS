"""Tests for the Google Workspace auth recovery state machine data model.

Validates the 5-state AuthState enum, transition map, action risk
classification, and GoogleWorkspaceConfig defaults introduced in
Section 2 of the autonomy wiring plan.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Import helpers — ensure the project root is on sys.path so the module
# resolves regardless of how pytest is invoked.
# ---------------------------------------------------------------------------
_PROJECT_ROOT = str(Path(__file__).resolve().parents[3])
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from backend.neural_mesh.agents.google_workspace_agent import (
    AuthState,
    AuthTransition,
    GoogleWorkspaceConfig,
    _ACTION_RISK,
    _AUTH_TRANSITIONS,
    _DEGRADED_MESSAGES,
    _classify_action_risk,
)


# ===================================================================
# AuthState enum
# ===================================================================


class TestAuthStateEnum:
    """AuthState must expose exactly 5 logical members (NEEDS_REAUTH is an alias)."""

    EXPECTED_NAMES = {
        "UNAUTHENTICATED",
        "AUTHENTICATED",
        "REFRESHING",
        "DEGRADED_VISUAL",
        "NEEDS_REAUTH_GUIDED",
    }

    def test_all_five_members_exist(self):
        """Every canonical member is accessible by name."""
        for name in self.EXPECTED_NAMES:
            member = AuthState[name]
            assert isinstance(member, AuthState)

    def test_members_are_strings(self):
        """AuthState inherits from str — values are plain strings."""
        for member in AuthState:
            assert isinstance(member, str)
            assert isinstance(member.value, str)

    def test_needs_reauth_alias(self):
        """NEEDS_REAUTH is a backward-compatible alias for NEEDS_REAUTH_GUIDED."""
        assert AuthState.NEEDS_REAUTH is AuthState.NEEDS_REAUTH_GUIDED
        assert AuthState.NEEDS_REAUTH == AuthState.NEEDS_REAUTH_GUIDED
        assert AuthState.NEEDS_REAUTH.value == "needs_reauth_guided"

    def test_unique_values(self):
        """Each canonical member has a distinct string value (alias shares value)."""
        values = [m.value for m in AuthState]
        # AuthState.__members__ includes the alias, but iterating the enum
        # only yields canonical members (aliases are deduplicated).
        assert len(values) == 5

    def test_enum_values_are_snake_case(self):
        """Values follow snake_case convention matching the member names."""
        for member in AuthState:
            assert member.value == member.value.lower()
            assert " " not in member.value


# ===================================================================
# AuthTransition table
# ===================================================================


class TestAuthTransitions:
    """Validate the static transition map."""

    def test_minimum_transition_count(self):
        """At least 7 transitions are defined."""
        assert len(_AUTH_TRANSITIONS) >= 7

    def test_each_entry_is_auth_transition(self):
        """Every entry is an AuthTransition namedtuple."""
        for t in _AUTH_TRANSITIONS:
            assert isinstance(t, AuthTransition)

    def test_fields_are_populated(self):
        """Every transition has non-empty from_state, event, to_state, reason_code."""
        for t in _AUTH_TRANSITIONS:
            assert t.from_state, f"Empty from_state in {t}"
            assert t.event, f"Empty event in {t}"
            assert t.to_state, f"Empty to_state in {t}"
            assert t.reason_code, f"Empty reason_code in {t}"

    def test_key_transitions_present(self):
        """Critical transitions that drive the state machine are present."""
        keys = {(t.from_state, t.event) for t in _AUTH_TRANSITIONS}
        assert ("authenticated", "token_expired") in keys
        assert ("refreshing", "refresh_success") in keys
        assert ("refreshing", "permanent_failure") in keys
        assert ("degraded_visual", "write_action") in keys
        assert ("degraded_visual", "api_probe_success") in keys
        assert ("needs_reauth_guided", "token_healed") in keys

    def test_reason_codes_are_prefixed(self):
        """All reason codes start with 'auth_' for consistent telemetry."""
        for t in _AUTH_TRANSITIONS:
            assert t.reason_code.startswith("auth_"), (
                f"Reason code {t.reason_code!r} does not start with 'auth_'"
            )


# ===================================================================
# Action risk classification
# ===================================================================


class TestActionRiskClassification:
    """Validate the _ACTION_RISK dict and _classify_action_risk helper."""

    def test_read_actions(self):
        """Known read-only actions are classified as 'read'."""
        read_actions = [
            "fetch_unread_emails",
            "check_calendar_events",
            "search_email",
            "get_contacts",
            "workspace_summary",
            "daily_briefing",
            "handle_workspace_query",
            "read_spreadsheet",
        ]
        for action in read_actions:
            assert _classify_action_risk(action) == "read", (
                f"{action} should be 'read', got {_classify_action_risk(action)!r}"
            )

    def test_write_actions(self):
        """Mutating actions are classified as 'write'."""
        write_actions = [
            "send_email",
            "draft_email_reply",
            "create_calendar_event",
            "create_document",
            "write_spreadsheet",
        ]
        for action in write_actions:
            assert _classify_action_risk(action) == "write", (
                f"{action} should be 'write', got {_classify_action_risk(action)!r}"
            )

    def test_high_risk_write_actions(self):
        """Destructive actions are classified as 'high_risk_write'."""
        high_risk_actions = ["delete_email", "delete_event"]
        for action in high_risk_actions:
            assert _classify_action_risk(action) == "high_risk_write", (
                f"{action} should be 'high_risk_write', got {_classify_action_risk(action)!r}"
            )

    def test_unknown_action_defaults_to_write(self):
        """Unrecognized actions default to 'write' (fail-safe)."""
        assert _classify_action_risk("unknown_action") == "write"
        assert _classify_action_risk("") == "write"
        assert _classify_action_risk("do_something_new") == "write"


# ===================================================================
# GoogleWorkspaceConfig defaults
# ===================================================================


class TestGoogleWorkspaceConfig:
    """Validate config field defaults (without env var overrides)."""

    def test_email_visual_fallback_defaults_true(self, monkeypatch):
        """email_visual_fallback_enabled defaults to True (API-degraded auto-fallback)."""
        monkeypatch.delenv("JARVIS_WORKSPACE_EMAIL_VISUAL_FALLBACK", raising=False)
        config = GoogleWorkspaceConfig()
        assert config.email_visual_fallback_enabled is True

    def test_write_visual_fallback_defaults_false(self, monkeypatch):
        """write_visual_fallback_enabled defaults to False (writes are opt-in)."""
        monkeypatch.delenv("JARVIS_WORKSPACE_WRITE_VISUAL_FALLBACK", raising=False)
        config = GoogleWorkspaceConfig()
        assert config.write_visual_fallback_enabled is False

    def test_email_visual_fallback_env_override(self, monkeypatch):
        """email_visual_fallback_enabled can be disabled via env var."""
        monkeypatch.setenv("JARVIS_WORKSPACE_EMAIL_VISUAL_FALLBACK", "false")
        config = GoogleWorkspaceConfig()
        assert config.email_visual_fallback_enabled is False

    def test_write_visual_fallback_env_override(self, monkeypatch):
        """write_visual_fallback_enabled can be enabled via env var."""
        monkeypatch.setenv("JARVIS_WORKSPACE_WRITE_VISUAL_FALLBACK", "true")
        config = GoogleWorkspaceConfig()
        assert config.write_visual_fallback_enabled is True


# ===================================================================
# Degraded message constants
# ===================================================================


class TestDegradedMessages:
    """Validate _DEGRADED_MESSAGES dictionary."""

    def test_degraded_visual_read_message_exists(self):
        assert ("degraded_visual", "read") in _DEGRADED_MESSAGES

    def test_needs_reauth_guided_read_message_exists(self):
        assert ("needs_reauth_guided", "read") in _DEGRADED_MESSAGES

    def test_needs_reauth_guided_write_message_exists(self):
        assert ("needs_reauth_guided", "write") in _DEGRADED_MESSAGES

    def test_messages_are_non_empty_strings(self):
        for key, msg in _DEGRADED_MESSAGES.items():
            assert isinstance(msg, str), f"Message for {key} is not a string"
            assert len(msg) > 10, f"Message for {key} is too short"
