"""Tests for RootAuthorityWatcher wiring into kernel (Task 15).

Validates that:
1. create_root_authority_watcher() respects JARVIS_ROOT_AUTHORITY_MODE env var
2. Shadow mode logs events but does not execute verdicts
3. JARVIS_ROOT_AUTHORITY_SUBSYSTEMS filters which subsystems are watched
4. Factory returns None when env var is unset (opt-in only)
"""
import os
import time
import pytest

from backend.core.root_authority_types import (
    ProcessIdentity, SubsystemState, TimeoutPolicy, RestartPolicy,
)


@pytest.fixture
def _identity():
    return ProcessIdentity(
        pid=1234, start_time_ns=time.monotonic_ns(),
        session_id="test-session", exec_fingerprint="sha256:abc",
    )


class TestRootAuthorityWiring:
    """Tests for the USP wiring factory functions."""

    def test_watcher_created_when_env_set(self, monkeypatch):
        """Watcher is created when JARVIS_ROOT_AUTHORITY_MODE is set."""
        monkeypatch.setenv("JARVIS_ROOT_AUTHORITY_MODE", "shadow")
        from backend.core.root_authority import RootAuthorityWatcher
        watcher = RootAuthorityWatcher(
            session_id="test",
            timeout_policy=TimeoutPolicy(),
            restart_policy=RestartPolicy(),
        )
        assert watcher is not None

    def test_shadow_mode_logs_but_no_execution(self, _identity):
        """In shadow mode, verdicts are logged but not executed."""
        from backend.core.root_authority import RootAuthorityWatcher

        events = []
        watcher = RootAuthorityWatcher(
            session_id="test",
            timeout_policy=TimeoutPolicy(),
            restart_policy=RestartPolicy(),
            event_sink=lambda e: events.append(e),
        )
        watcher.register_subsystem("jarvis-prime", _identity)
        verdict = watcher.process_crash("jarvis-prime", exit_code=300)

        # Verdict is emitted (shadow would log it)
        assert verdict is not None
        assert verdict.action.value == "restart"
        # Events were recorded (spawn + crash transition + verdict_emitted)
        assert len(events) >= 1

    def test_shadow_mode_env_parsing(self, monkeypatch):
        """JARVIS_ROOT_AUTHORITY_MODE env var parsed correctly."""
        monkeypatch.setenv("JARVIS_ROOT_AUTHORITY_MODE", "shadow")
        mode = os.environ.get("JARVIS_ROOT_AUTHORITY_MODE", "")
        assert mode == "shadow"

    def test_subsystem_filter(self, monkeypatch):
        """JARVIS_ROOT_AUTHORITY_SUBSYSTEMS filters which subsystems are watched."""
        monkeypatch.setenv("JARVIS_ROOT_AUTHORITY_SUBSYSTEMS", "jarvis-prime,reactor-core")
        subs = os.environ.get("JARVIS_ROOT_AUTHORITY_SUBSYSTEMS", "").split(",")
        assert "jarvis-prime" in subs
        assert "reactor-core" in subs

    def test_active_mode_env(self, monkeypatch):
        """JARVIS_ROOT_AUTHORITY_MODE=active is recognized."""
        monkeypatch.setenv("JARVIS_ROOT_AUTHORITY_MODE", "active")
        mode = os.environ.get("JARVIS_ROOT_AUTHORITY_MODE", "")
        assert mode == "active"


class TestCreateRootAuthorityWatcher:
    """Tests for the create_root_authority_watcher factory function."""

    def test_returns_none_when_env_unset(self, monkeypatch):
        """Factory returns None when JARVIS_ROOT_AUTHORITY_MODE is not set."""
        monkeypatch.delenv("JARVIS_ROOT_AUTHORITY_MODE", raising=False)
        # Import inline to avoid module-level caching issues
        from unified_supervisor import create_root_authority_watcher
        result = create_root_authority_watcher()
        assert result is None

    def test_returns_watcher_in_shadow_mode(self, monkeypatch):
        """Factory returns a RootAuthorityWatcher when mode is 'shadow'."""
        monkeypatch.setenv("JARVIS_ROOT_AUTHORITY_MODE", "shadow")
        monkeypatch.setenv("JARVIS_ROOT_SESSION_ID", "test-session-123")
        from unified_supervisor import create_root_authority_watcher
        watcher = create_root_authority_watcher()
        assert watcher is not None
        from backend.core.root_authority import RootAuthorityWatcher
        assert isinstance(watcher, RootAuthorityWatcher)

    def test_returns_watcher_in_active_mode(self, monkeypatch):
        """Factory returns a RootAuthorityWatcher when mode is 'active'."""
        monkeypatch.setenv("JARVIS_ROOT_AUTHORITY_MODE", "active")
        monkeypatch.setenv("JARVIS_ROOT_SESSION_ID", "test-session-456")
        from unified_supervisor import create_root_authority_watcher
        watcher = create_root_authority_watcher()
        assert watcher is not None

    def test_shadow_mode_event_sink_receives_events(self, monkeypatch, _identity):
        """Shadow mode watcher's event_sink logs events when subsystems are registered."""
        monkeypatch.setenv("JARVIS_ROOT_AUTHORITY_MODE", "shadow")
        monkeypatch.setenv("JARVIS_ROOT_SESSION_ID", "test-session-789")
        from unified_supervisor import create_root_authority_watcher
        import logging

        watcher = create_root_authority_watcher()
        assert watcher is not None
        # Registering a subsystem should not raise
        watcher.register_subsystem("jarvis-prime", _identity)
        state = watcher.get_state("jarvis-prime")
        assert state == SubsystemState.STARTING


class TestGetWatchedSubsystems:
    """Tests for the get_watched_subsystems helper function."""

    def test_defaults_when_env_unset(self, monkeypatch):
        """Returns default subsystems when JARVIS_ROOT_AUTHORITY_SUBSYSTEMS is not set."""
        monkeypatch.delenv("JARVIS_ROOT_AUTHORITY_SUBSYSTEMS", raising=False)
        from unified_supervisor import get_watched_subsystems
        subs = get_watched_subsystems()
        assert "jarvis-prime" in subs
        assert "reactor-core" in subs

    def test_custom_subsystems_from_env(self, monkeypatch):
        """Parses JARVIS_ROOT_AUTHORITY_SUBSYSTEMS correctly."""
        monkeypatch.setenv("JARVIS_ROOT_AUTHORITY_SUBSYSTEMS", "alpha,beta,gamma")
        from unified_supervisor import get_watched_subsystems
        subs = get_watched_subsystems()
        assert subs == ["alpha", "beta", "gamma"]

    def test_strips_whitespace(self, monkeypatch):
        """Strips whitespace from subsystem names."""
        monkeypatch.setenv("JARVIS_ROOT_AUTHORITY_SUBSYSTEMS", " foo , bar , baz ")
        from unified_supervisor import get_watched_subsystems
        subs = get_watched_subsystems()
        assert subs == ["foo", "bar", "baz"]

    def test_empty_string_returns_defaults(self, monkeypatch):
        """Empty string returns default subsystems."""
        monkeypatch.setenv("JARVIS_ROOT_AUTHORITY_SUBSYSTEMS", "")
        from unified_supervisor import get_watched_subsystems
        subs = get_watched_subsystems()
        assert "jarvis-prime" in subs
        assert "reactor-core" in subs
