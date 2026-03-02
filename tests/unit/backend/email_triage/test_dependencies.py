"""Tests for email triage dependency resolution with exponential backoff."""

import os
import sys
import time
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "backend"))

from autonomy.email_triage.config import TriageConfig
from autonomy.email_triage.dependencies import DependencyHealth, DependencyResolver


# ---------------------------------------------------------------------------
# TestDependencyHealth
# ---------------------------------------------------------------------------


class TestDependencyHealth:
    """DependencyHealth tracks per-dependency resolution state."""

    def test_initial_state(self):
        dep = DependencyHealth(name="workspace_agent", required=True)
        assert dep.name == "workspace_agent"
        assert dep.required is True
        assert dep.resolved is False
        assert dep.instance is None
        assert dep.last_resolve_at == 0.0
        assert dep.last_resolve_error is None
        assert dep.consecutive_failures == 0
        assert dep.next_attempt_at == 0.0

    def test_record_success(self):
        dep = DependencyHealth(name="router", required=False)
        fake_instance = object()
        dep.record_success(fake_instance)
        assert dep.resolved is True
        assert dep.instance is fake_instance
        assert dep.consecutive_failures == 0
        assert dep.last_resolve_error is None
        assert dep.last_resolve_at > 0.0

    def test_record_failure_increments_consecutive_failures(self):
        dep = DependencyHealth(name="router", required=False)
        dep.record_failure("connection refused", base_s=5.0, max_s=300.0)
        assert dep.resolved is False
        assert dep.instance is None
        assert dep.consecutive_failures == 1
        assert dep.last_resolve_error == "connection refused"

    def test_record_failure_second_time_increments_again(self):
        dep = DependencyHealth(name="router", required=False)
        dep.record_failure("err1", base_s=5.0, max_s=300.0)
        dep.record_failure("err2", base_s=5.0, max_s=300.0)
        assert dep.consecutive_failures == 2
        assert dep.last_resolve_error == "err2"

    def test_backoff_increases_exponentially(self):
        dep = DependencyHealth(name="router", required=False)
        backoffs = []
        for i in range(5):
            before = time.monotonic()
            dep.record_failure("err", base_s=5.0, max_s=300.0)
            # The backoff delay is next_attempt_at - now
            delay = dep.next_attempt_at - before
            backoffs.append(delay)

        # Each backoff should roughly double (with jitter 0.8-1.2x).
        # Base delays (before jitter): 10, 20, 40, 80, 160
        # Minimum with 0.8 jitter: 8, 16, 32, 64, 128
        for i in range(1, len(backoffs)):
            # Later backoff should be larger than the previous (within jitter tolerance)
            # Use a generous lower bound: each step should be at least 1.3x the previous
            assert backoffs[i] > backoffs[i - 1] * 1.3, (
                f"Backoff[{i}]={backoffs[i]:.2f} not > 1.3 * backoff[{i-1}]={backoffs[i-1]:.2f}"
            )

    def test_backoff_capped_at_max(self):
        dep = DependencyHealth(name="router", required=False)
        for _ in range(20):
            dep.record_failure("err", base_s=5.0, max_s=60.0)

        # With max_s=60 and jitter up to 1.2x, delay should never exceed 72
        delay = dep.next_attempt_at - time.monotonic()
        assert delay <= 60.0 * 1.3  # generous bound for timing

    def test_can_attempt_respects_backoff(self):
        dep = DependencyHealth(name="router", required=False)
        dep.record_failure("err", base_s=100.0, max_s=300.0)
        # Backoff = 100 * 2^1 * jitter(0.8-1.2) = 160-240s into the future
        assert dep.can_attempt() is False

    def test_can_attempt_true_initially(self):
        dep = DependencyHealth(name="router", required=False)
        assert dep.can_attempt() is True

    def test_can_attempt_false_when_resolved(self):
        dep = DependencyHealth(name="router", required=False)
        dep.record_success(object())
        # Already resolved, can_attempt should be False
        assert dep.can_attempt() is False

    def test_invalidate_clears_instance(self):
        dep = DependencyHealth(name="router", required=False)
        fake = object()
        dep.record_success(fake)
        assert dep.resolved is True
        assert dep.instance is fake

        dep.invalidate("went away", base_s=5.0, max_s=300.0)
        assert dep.resolved is False
        assert dep.instance is None
        assert dep.consecutive_failures == 1
        assert dep.last_resolve_error == "went away"

    def test_invalidate_sets_backoff(self):
        dep = DependencyHealth(name="router", required=False)
        dep.record_success(object())
        dep.invalidate("gone", base_s=5.0, max_s=300.0)
        assert dep.next_attempt_at > time.monotonic()


# ---------------------------------------------------------------------------
# TestDependencyResolver
# ---------------------------------------------------------------------------


class TestDependencyResolver:
    """DependencyResolver manages workspace_agent, router, notifier."""

    def _make_resolver(self, **kwargs):
        config = TriageConfig()
        return DependencyResolver(config, **kwargs)

    def test_creates_three_deps(self):
        resolver = self._make_resolver()
        assert "workspace_agent" in resolver._deps
        assert "router" in resolver._deps
        assert "notifier" in resolver._deps

    def test_workspace_agent_is_required(self):
        resolver = self._make_resolver()
        assert resolver._deps["workspace_agent"].required is True

    def test_router_is_optional(self):
        resolver = self._make_resolver()
        assert resolver._deps["router"].required is False

    def test_notifier_is_optional(self):
        resolver = self._make_resolver()
        assert resolver._deps["notifier"].required is False

    def test_injected_override_marks_resolved(self):
        fake_agent = MagicMock(name="fake_agent")
        resolver = self._make_resolver(workspace_agent=fake_agent)
        assert resolver._deps["workspace_agent"].resolved is True
        assert resolver._deps["workspace_agent"].instance is fake_agent

    def test_injected_override_for_all(self):
        fake_agent = MagicMock()
        fake_router = MagicMock()
        fake_notifier = MagicMock()
        resolver = self._make_resolver(
            workspace_agent=fake_agent,
            router=fake_router,
            notifier=fake_notifier,
        )
        assert resolver.get("workspace_agent") is fake_agent
        assert resolver.get("router") is fake_router
        assert resolver.get("notifier") is fake_notifier

    @pytest.mark.asyncio
    async def test_resolve_all_success(self):
        fake_agent = MagicMock()
        fake_router = MagicMock()
        fake_notifier = MagicMock()
        with (
            patch(
                "autonomy.email_triage.dependencies._resolve_workspace_agent",
                return_value=fake_agent,
            ),
            patch(
                "autonomy.email_triage.dependencies._resolve_router",
                return_value=fake_router,
            ),
            patch(
                "autonomy.email_triage.dependencies._resolve_notifier",
                return_value=fake_notifier,
            ),
        ):
            resolver = self._make_resolver()
            await resolver.resolve_all()

        assert resolver.get("workspace_agent") is fake_agent
        assert resolver.get("router") is fake_router
        assert resolver.get("notifier") is fake_notifier

    @pytest.mark.asyncio
    async def test_resolve_all_failure_emits_event(self):
        with (
            patch(
                "autonomy.email_triage.dependencies._resolve_workspace_agent",
                side_effect=RuntimeError("not ready"),
            ),
            patch(
                "autonomy.email_triage.dependencies._resolve_router",
                side_effect=RuntimeError("not ready"),
            ),
            patch(
                "autonomy.email_triage.dependencies._resolve_notifier",
                side_effect=RuntimeError("not ready"),
            ),
            patch(
                "autonomy.email_triage.dependencies.emit_triage_event"
            ) as mock_emit,
        ):
            resolver = self._make_resolver()
            await resolver.resolve_all()

        assert resolver.get("workspace_agent") is None
        assert resolver.get("router") is None
        assert resolver.get("notifier") is None

        # Should have emitted EVENT_DEPENDENCY_UNAVAILABLE for each failure
        assert mock_emit.call_count == 3
        for call in mock_emit.call_args_list:
            assert call[0][0] == "dependency_unavailable"

    @pytest.mark.asyncio
    async def test_skips_already_resolved(self):
        fake_agent = MagicMock()
        with (
            patch(
                "autonomy.email_triage.dependencies._resolve_workspace_agent",
            ) as mock_resolve,
            patch(
                "autonomy.email_triage.dependencies._resolve_router",
                return_value=MagicMock(),
            ),
            patch(
                "autonomy.email_triage.dependencies._resolve_notifier",
                return_value=MagicMock(),
            ),
        ):
            resolver = self._make_resolver(workspace_agent=fake_agent)
            await resolver.resolve_all()

        # workspace_agent was injected, so resolver function should not be called
        mock_resolve.assert_not_called()
        assert resolver.get("workspace_agent") is fake_agent

    @pytest.mark.asyncio
    async def test_skips_during_backoff(self):
        with (
            patch(
                "autonomy.email_triage.dependencies._resolve_workspace_agent",
                side_effect=RuntimeError("not ready"),
            ) as mock_resolve,
            patch(
                "autonomy.email_triage.dependencies._resolve_router",
                return_value=MagicMock(),
            ),
            patch(
                "autonomy.email_triage.dependencies._resolve_notifier",
                return_value=MagicMock(),
            ),
            patch("autonomy.email_triage.dependencies.emit_triage_event"),
        ):
            resolver = self._make_resolver()
            # First resolve_all: workspace_agent fails, enters backoff
            await resolver.resolve_all()
            assert mock_resolve.call_count == 1

            # Second resolve_all: workspace_agent in backoff, should be skipped
            await resolver.resolve_all()
            assert mock_resolve.call_count == 1  # Not called again

    def test_get_returns_none_when_unresolved(self):
        resolver = self._make_resolver()
        assert resolver.get("workspace_agent") is None
        assert resolver.get("router") is None
        assert resolver.get("notifier") is None

    def test_get_returns_instance_when_resolved(self):
        fake = MagicMock()
        resolver = self._make_resolver(workspace_agent=fake)
        assert resolver.get("workspace_agent") is fake

    def test_get_unknown_name_returns_none(self):
        resolver = self._make_resolver()
        assert resolver.get("nonexistent") is None

    def test_invalidate_clears_dependency(self):
        fake = MagicMock()
        resolver = self._make_resolver(workspace_agent=fake)
        assert resolver.get("workspace_agent") is fake

        with patch("autonomy.email_triage.dependencies.emit_triage_event") as mock_emit:
            resolver.invalidate("workspace_agent", "connection lost")

        assert resolver.get("workspace_agent") is None
        mock_emit.assert_called_once()
        assert mock_emit.call_args[0][0] == "dependency_unavailable"

    def test_report_degraded_emits_event(self):
        fake = MagicMock()
        resolver = self._make_resolver(router=fake)

        with patch("autonomy.email_triage.dependencies.emit_triage_event") as mock_emit:
            resolver.report_degraded("router", "high latency")

        mock_emit.assert_called_once()
        assert mock_emit.call_args[0][0] == "dependency_degraded"
        payload = mock_emit.call_args[0][1]
        assert payload["dependency"] == "router"
        assert payload["reason"] == "high latency"
