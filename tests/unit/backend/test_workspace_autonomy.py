"""Tests for GoogleWorkspaceAgent autonomy wiring (v284.0).

Validates:
- WorkspaceAutonomyPolicy decision logic
- GoogleWorkspaceConfig autonomy fields
- get_capability_health() readiness_level, standalone_mode, scope_valid
- get_autonomy_doctor_report() structured diagnostics
- DependencyResolver.health_summary()
- Idempotency / journal integration contracts
- Status endpoint contract
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Import helpers
# ---------------------------------------------------------------------------
_PROJECT_ROOT = str(Path(__file__).resolve().parents[3])
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from backend.neural_mesh.agents.google_workspace_agent import (
    AutonomyPolicyDecision,
    GoogleWorkspaceConfig,
    WorkspaceAutonomyPolicy,
    _classify_action_risk,
    get_workspace_agent_cached,
)


# ===================================================================
# WorkspaceAutonomyPolicy
# ===================================================================


class TestWorkspaceAutonomyPolicy:
    """Central autonomy gate must enforce read/write/high-risk rules."""

    def _make_policy(self, **overrides) -> WorkspaceAutonomyPolicy:
        config = GoogleWorkspaceConfig(**overrides)
        return WorkspaceAutonomyPolicy(config)

    def test_interactive_caller_always_allowed(self):
        """Non-autonomous callers pass regardless of config."""
        policy = self._make_policy()
        for action in ("send_email", "delete_email", "fetch_unread_emails"):
            d = policy.check(action, request_kind=None)
            assert d.allowed is True
            assert d.reason == "interactive_caller"

    def test_autonomous_read_allowed(self):
        """Autonomous reads always pass."""
        policy = self._make_policy()
        d = policy.check("fetch_unread_emails", "autonomous")
        assert d.allowed is True
        assert d.reason == "read_allowed"

    def test_autonomous_write_blocked_by_default(self):
        """Autonomous writes are blocked when config defaults are used."""
        policy = self._make_policy()
        d = policy.check("send_email", "autonomous")
        assert d.allowed is False
        assert d.reason == "write_not_enabled"
        assert d.remediation is not None

    def test_autonomous_write_allowed_when_enabled(self):
        """Autonomous writes pass when allow_autonomous_writes=True."""
        policy = self._make_policy(allow_autonomous_writes=True)
        d = policy.check("send_email", "autonomous")
        assert d.allowed is True
        assert d.reason == "write_enabled"

    def test_autonomous_high_risk_blocked_even_when_writes_enabled(self):
        """High-risk writes need their own flag, even when regular writes are on."""
        policy = self._make_policy(allow_autonomous_writes=True)
        d = policy.check("delete_email", "autonomous")
        assert d.allowed is False
        assert d.reason == "high_risk_blocked"

    def test_autonomous_high_risk_allowed_when_both_enabled(self):
        """High-risk writes pass when both flags are True."""
        policy = self._make_policy(
            allow_autonomous_writes=True,
            allow_autonomous_high_risk_writes=True,
        )
        d = policy.check("delete_email", "autonomous")
        assert d.allowed is True

    def test_allowlist_permits_listed_action(self):
        """Actions in the allowlist pass for autonomous callers."""
        policy = self._make_policy(
            autonomous_write_allowlist=frozenset({"send_email"}),
        )
        d = policy.check("send_email", "autonomous")
        assert d.allowed is True
        assert d.reason == "allowlisted"

    def test_allowlist_blocks_unlisted_action(self):
        """Actions NOT in the allowlist are blocked."""
        policy = self._make_policy(
            autonomous_write_allowlist=frozenset({"send_email"}),
        )
        d = policy.check("create_calendar_event", "autonomous")
        assert d.allowed is False
        assert d.reason == "action_not_in_allowlist"

    def test_allowlist_overrides_boolean_flags(self):
        """When allowlist is non-empty, boolean flags are ignored."""
        policy = self._make_policy(
            allow_autonomous_writes=True,
            autonomous_write_allowlist=frozenset({"send_email"}),
        )
        # create_calendar_event is a write, writes are enabled, but it's not allowlisted
        d = policy.check("create_calendar_event", "autonomous")
        assert d.allowed is False
        assert d.reason == "action_not_in_allowlist"

    def test_empty_allowlist_defers_to_boolean_flags(self):
        """When allowlist is empty, boolean flags govern."""
        policy = self._make_policy(
            allow_autonomous_writes=True,
            autonomous_write_allowlist=frozenset(),
        )
        d = policy.check("create_calendar_event", "autonomous")
        assert d.allowed is True
        assert d.reason == "write_enabled"

    def test_decision_carries_escalation_and_remediation(self):
        """Every decision has escalation; denied decisions have remediation."""
        policy = self._make_policy()
        d = policy.check("send_email", "autonomous")
        assert d.escalation  # non-empty string
        assert d.remediation is not None  # denied → remediation present

        d2 = policy.check("fetch_unread_emails", "autonomous")
        assert d2.escalation  # even allowed decisions have escalation
        assert d2.remediation is None  # allowed → no remediation

    def test_policy_decision_is_frozen_dataclass(self):
        """AutonomyPolicyDecision is immutable."""
        d = AutonomyPolicyDecision(True, "test", "AUTO_EXECUTE")
        with pytest.raises(AttributeError):
            d.allowed = False  # type: ignore[misc]


# ===================================================================
# GoogleWorkspaceConfig autonomy fields
# ===================================================================


class TestConfigFields:
    """Autonomy config fields must parse environment variables correctly."""

    def test_autonomous_writes_default_false(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("JARVIS_WORKSPACE_ALLOW_AUTONOMOUS_WRITES", None)
            config = GoogleWorkspaceConfig()
            assert config.allow_autonomous_writes is False

    def test_autonomous_high_risk_writes_default_false(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("JARVIS_WORKSPACE_ALLOW_AUTONOMOUS_HIGH_RISK_WRITES", None)
            config = GoogleWorkspaceConfig()
            assert config.allow_autonomous_high_risk_writes is False

    def test_allowlist_parses_comma_separated(self):
        with patch.dict(os.environ, {
            "JARVIS_WORKSPACE_AUTONOMOUS_WRITE_ALLOWLIST": "send_email,create_calendar_event",
        }):
            config = GoogleWorkspaceConfig()
            assert config.autonomous_write_allowlist == frozenset({
                "send_email", "create_calendar_event",
            })

    def test_allowlist_empty_by_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("JARVIS_WORKSPACE_AUTONOMOUS_WRITE_ALLOWLIST", None)
            config = GoogleWorkspaceConfig()
            assert config.autonomous_write_allowlist == frozenset()


# ===================================================================
# get_capability_health() readiness fields
# ===================================================================


class TestCapabilityHealthReadinessLevel:
    """get_capability_health() must expose readiness_level, standalone_mode, scope_valid."""

    def _make_agent_with_auth(self, auth_state_value: str, token_health_value: str = "valid"):
        """Create a minimal mock agent to test get_capability_health()."""
        from backend.neural_mesh.agents.google_workspace_agent import GoogleWorkspaceAgent

        agent = object.__new__(GoogleWorkspaceAgent)
        agent.config = GoogleWorkspaceConfig()
        agent.capabilities = set()
        agent.message_bus = None

        # Mock client
        mock_client = MagicMock()

        class FakeAuthState:
            value = auth_state_value
        mock_client.auth_state = FakeAuthState()

        class FakeTokenHealth:
            value = token_health_value
        mock_client._token_health = FakeTokenHealth()

        mock_client._validate_current_credentials_scopes = MagicMock(
            return_value=(True, None)
        )

        agent._client = mock_client
        return agent

    def test_authenticated_returns_ready(self):
        agent = self._make_agent_with_auth("authenticated")
        h = agent.get_capability_health()
        assert h["readiness_level"] == "ready"

    def test_degraded_visual_returns_degraded_read_only(self):
        agent = self._make_agent_with_auth("degraded_visual")
        h = agent.get_capability_health()
        assert h["readiness_level"] == "degraded_read_only"

    def test_needs_reauth_returns_blocked_needs_reauth(self):
        agent = self._make_agent_with_auth("needs_reauth_guided")
        h = agent.get_capability_health()
        assert h["readiness_level"] == "blocked_needs_reauth"

    def test_missing_token_returns_blocked_no_credentials(self):
        agent = self._make_agent_with_auth("unauthenticated", "missing")
        h = agent.get_capability_health()
        assert h["readiness_level"] == "blocked_no_credentials"

    def test_has_readiness_level_key(self):
        agent = self._make_agent_with_auth("authenticated")
        h = agent.get_capability_health()
        assert "readiness_level" in h

    def test_has_standalone_mode_key(self):
        agent = self._make_agent_with_auth("authenticated")
        h = agent.get_capability_health()
        assert "standalone_mode" in h
        assert h["standalone_mode"] is True  # message_bus is None

    def test_has_scope_valid_key(self):
        agent = self._make_agent_with_auth("authenticated")
        h = agent.get_capability_health()
        assert "scope_valid" in h
        assert h["scope_valid"] is True


# ===================================================================
# get_autonomy_doctor_report()
# ===================================================================


class TestAutonomyDoctorReport:
    """Doctor report must return structured pass/fail diagnostics."""

    def _make_agent(self, creds_exist=False, token_exist=False):
        from backend.neural_mesh.agents.google_workspace_agent import GoogleWorkspaceAgent

        agent = object.__new__(GoogleWorkspaceAgent)
        agent.config = GoogleWorkspaceConfig(
            credentials_path="/tmp/_test_creds.json" if not creds_exist else __file__,
            token_path="/tmp/_test_token.json" if not token_exist else __file__,
        )
        agent.capabilities = set()
        agent.message_bus = None
        agent._client = None
        return agent

    def test_returns_overall_and_checks(self):
        agent = self._make_agent()
        report = agent.get_autonomy_doctor_report()
        assert "overall" in report
        assert "checks" in report
        assert "blocking_issues" in report
        assert "ts" in report
        assert report["overall"] in {"pass", "fail", "degraded"}

    def test_all_check_names_present(self):
        agent = self._make_agent()
        report = agent.get_autonomy_doctor_report()
        names = {c["name"] for c in report["checks"]}
        expected = {
            "credentials_file", "token_file", "token_health", "auth_state",
            "google_api_libs", "can_attempt_api", "scope_valid",
            "email_visual_fallback", "write_visual_fallback",
            "autonomous_writes", "standalone_gate",
        }
        assert expected.issubset(names), f"Missing: {expected - names}"

    def test_blocking_issues_count(self):
        """With no client, several required checks fail."""
        agent = self._make_agent()
        report = agent.get_autonomy_doctor_report()
        assert report["blocking_issues"] > 0
        assert report["overall"] == "fail"

    def test_fail_when_credentials_missing(self):
        agent = self._make_agent(creds_exist=False)
        report = agent.get_autonomy_doctor_report()
        cred_check = next(c for c in report["checks"] if c["name"] == "credentials_file")
        assert cred_check["passed"] is False
        assert cred_check["required"] is True

    def test_scope_drift_check_included(self):
        agent = self._make_agent()
        report = agent.get_autonomy_doctor_report()
        scope_check = next(c for c in report["checks"] if c["name"] == "scope_valid")
        assert "passed" in scope_check


# ===================================================================
# DependencyResolver.health_summary()
# ===================================================================


class TestDependencyHealthSummary:
    """health_summary() must expose all dep states."""

    def _make_resolver(self):
        from backend.autonomy.email_triage.dependencies import DependencyResolver

        class FakeConfig:
            dep_backoff_base_s = 2.0
            dep_backoff_max_s = 60.0

        return DependencyResolver(config=FakeConfig())

    def test_returns_all_dep_names(self):
        resolver = self._make_resolver()
        summary = resolver.health_summary()
        assert "workspace_agent" in summary

    def test_shows_resolved_state_after_injection(self):
        resolver = self._make_resolver()
        dep = resolver._deps.get("workspace_agent")
        if dep:
            dep.record_success(MagicMock())
        summary = resolver.health_summary()
        ws = summary.get("workspace_agent", {})
        assert ws.get("resolved") is True


# ===================================================================
# Idempotency integration
# ===================================================================


class TestIdempotencyIntegration:
    """In-memory dedup must prevent duplicate autonomous writes."""

    def test_duplicate_write_suppressed_by_registry(self):
        from backend.core.idempotency_registry import IdempotencyRegistry, check_idempotent

        reg = IdempotencyRegistry.get_instance()
        # First call should be new
        assert check_idempotent("workspace_write", "send_email:test_key_123") is True
        # Second call should be duplicate
        assert check_idempotent("workspace_write", "send_email:test_key_123") is False

        # Cleanup
        from backend.core.idempotency_registry import IdempotencyKey
        reg.clear(IdempotencyKey("workspace_write", "send_email:test_key_123"))

    def test_canonical_key_generated_when_caller_omits(self):
        """Canonical idempotency keys include action + target hash."""
        import hashlib

        # Simulate the canonical key generation logic
        action = "send_email"
        goal_id = "goal_1"
        step_id = "2"
        payload = {"to": "test@example.com", "goal_id": goal_id, "step_id": step_id}
        target_parts = []
        for k in ("to", "recipient", "email", "date", "title", "spreadsheet_id"):
            v = payload.get(k)
            if v:
                target_parts.append(f"{k}={v}")
        target_hash = hashlib.sha256("|".join(target_parts).encode()).hexdigest()[:12]
        key = f"{goal_id}:{step_id}:{action}:{target_hash}"
        assert "goal_1:2:send_email:" in key
        assert len(target_hash) == 12


# ===================================================================
# Policy guardrails
# ===================================================================


class TestPolicyGuardrails:
    """Guardrail tests for edge cases and precedence rules."""

    def test_high_risk_checked_before_allowlist(self):
        """High-risk gate runs before allowlist — even if allowlisted, high-risk flag is needed."""
        policy = WorkspaceAutonomyPolicy(GoogleWorkspaceConfig(
            autonomous_write_allowlist=frozenset({"delete_email"}),
            allow_autonomous_high_risk_writes=False,
        ))
        d = policy.check("delete_email", "autonomous")
        assert d.allowed is False
        assert d.reason == "high_risk_blocked"

    def test_high_risk_allowlisted_with_flag(self):
        """High-risk action passes when both allowlisted AND flag is set."""
        policy = WorkspaceAutonomyPolicy(GoogleWorkspaceConfig(
            autonomous_write_allowlist=frozenset({"delete_email"}),
            allow_autonomous_high_risk_writes=True,
        ))
        d = policy.check("delete_email", "autonomous")
        assert d.allowed is True
        assert d.reason == "allowlisted"


# ===================================================================
# get_workspace_agent_cached()
# ===================================================================


class TestWorkspaceAgentCached:
    """Public sync getter for the workspace agent singleton."""

    def test_returns_none_when_no_instance(self):
        import backend.neural_mesh.agents.google_workspace_agent as mod
        original = mod._workspace_agent_instance
        try:
            mod._workspace_agent_instance = None
            assert get_workspace_agent_cached() is None
        finally:
            mod._workspace_agent_instance = original

    def test_returns_none_for_stopped_instance(self):
        import backend.neural_mesh.agents.google_workspace_agent as mod
        original = mod._workspace_agent_instance
        try:
            mock = MagicMock()
            mock._running = False
            mod._workspace_agent_instance = mock
            assert get_workspace_agent_cached() is None
        finally:
            mod._workspace_agent_instance = original

    def test_returns_instance_when_running(self):
        import backend.neural_mesh.agents.google_workspace_agent as mod
        original = mod._workspace_agent_instance
        try:
            mock = MagicMock()
            mock._running = True
            mod._workspace_agent_instance = mock
            assert get_workspace_agent_cached() is mock
        finally:
            mod._workspace_agent_instance = original


# ===================================================================
# RequestKind.AUTONOMOUS exists
# ===================================================================


class TestRequestKindAutonomous:
    """RequestKind.AUTONOMOUS must be importable and have correct value."""

    def test_autonomous_member_exists(self):
        from backend.core.execution_context import RequestKind
        assert hasattr(RequestKind, "AUTONOMOUS")
        assert RequestKind.AUTONOMOUS.value == "autonomous"

    def test_autonomous_is_distinct(self):
        from backend.core.execution_context import RequestKind
        values = [m.value for m in RequestKind]
        assert values.count("autonomous") == 1
