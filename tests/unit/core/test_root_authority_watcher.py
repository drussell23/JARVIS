"""Tests for RootAuthorityWatcher state machine and verdict emission."""
import asyncio
import time
import pytest
from unittest.mock import AsyncMock, MagicMock

from backend.core.root_authority_types import (
    ProcessIdentity, LifecycleAction, SubsystemState,
    TimeoutPolicy, RestartPolicy, LifecycleVerdict,
)


@pytest.fixture
def sample_identity():
    return ProcessIdentity(
        pid=1234, start_time_ns=time.monotonic_ns(),
        session_id="test-session", exec_fingerprint="sha256:abc123"
    )


@pytest.fixture
def sample_policy():
    return TimeoutPolicy(
        startup_grace_s=5.0, health_timeout_s=1.0,
        health_poll_interval_s=0.5, drain_timeout_s=5.0,
        term_timeout_s=2.0, degraded_tolerance_s=3.0,
        degraded_recovery_check_s=1.0,
    )


@pytest.fixture
def sample_restart_policy():
    return RestartPolicy(max_restarts=2, window_s=60.0, jitter_factor=0.0)


class TestWatcherStateTransitions:
    @pytest.mark.asyncio
    async def test_initial_state_is_starting(self, sample_identity, sample_policy, sample_restart_policy):
        from backend.core.root_authority import RootAuthorityWatcher
        watcher = RootAuthorityWatcher(
            session_id="test-session",
            timeout_policy=sample_policy,
            restart_policy=sample_restart_policy,
        )
        watcher.register_subsystem("jarvis-prime", sample_identity)
        assert watcher.get_state("jarvis-prime") == SubsystemState.STARTING

    @pytest.mark.asyncio
    async def test_transition_to_alive_on_health_up(self, sample_identity, sample_policy, sample_restart_policy):
        from backend.core.root_authority import RootAuthorityWatcher
        watcher = RootAuthorityWatcher(
            session_id="test-session",
            timeout_policy=sample_policy,
            restart_policy=sample_restart_policy,
        )
        watcher.register_subsystem("jarvis-prime", sample_identity)
        watcher.process_health_response("jarvis-prime", {
            "liveness": "up", "readiness": "not_ready",
            "session_id": "test-session", "pid": 1234,
            "start_time_ns": sample_identity.start_time_ns,
            "exec_fingerprint": "sha256:abc123",
            "schema_version": "1.0.0",
        })
        state = watcher.get_state("jarvis-prime")
        assert state in (SubsystemState.ALIVE, SubsystemState.HANDSHAKE)

    @pytest.mark.asyncio
    async def test_transition_to_ready(self, sample_identity, sample_policy, sample_restart_policy):
        from backend.core.root_authority import RootAuthorityWatcher
        watcher = RootAuthorityWatcher(
            session_id="test-session",
            timeout_policy=sample_policy,
            restart_policy=sample_restart_policy,
        )
        watcher.register_subsystem("jarvis-prime", sample_identity)
        watcher.process_health_response("jarvis-prime", {
            "liveness": "up", "readiness": "ready",
            "session_id": "test-session", "pid": 1234,
            "start_time_ns": sample_identity.start_time_ns,
            "exec_fingerprint": "sha256:abc123",
            "schema_version": "1.0.0",
        })
        assert watcher.get_state("jarvis-prime") in (
            SubsystemState.READY, SubsystemState.ALIVE, SubsystemState.HANDSHAKE
        )

    @pytest.mark.asyncio
    async def test_crash_detection_emits_verdict(self, sample_identity, sample_policy, sample_restart_policy):
        from backend.core.root_authority import RootAuthorityWatcher
        watcher = RootAuthorityWatcher(
            session_id="test-session",
            timeout_policy=sample_policy,
            restart_policy=sample_restart_policy,
        )
        watcher.register_subsystem("jarvis-prime", sample_identity)
        verdict = watcher.process_crash("jarvis-prime", exit_code=300)
        assert verdict is not None
        assert verdict.action == LifecycleAction.RESTART
        assert verdict.reason_code == "crash_exit_300"
        assert watcher.get_state("jarvis-prime") == SubsystemState.CRASHED

    @pytest.mark.asyncio
    async def test_clean_exit_no_restart(self, sample_identity, sample_policy, sample_restart_policy):
        from backend.core.root_authority import RootAuthorityWatcher
        watcher = RootAuthorityWatcher(
            session_id="test-session",
            timeout_policy=sample_policy,
            restart_policy=sample_restart_policy,
        )
        watcher.register_subsystem("jarvis-prime", sample_identity)
        verdict = watcher.process_crash("jarvis-prime", exit_code=0)
        assert verdict is None or verdict.action == LifecycleAction.ESCALATE_OPERATOR
        assert watcher.get_state("jarvis-prime") == SubsystemState.STOPPED

    @pytest.mark.asyncio
    async def test_config_error_no_restart(self, sample_identity, sample_policy, sample_restart_policy):
        from backend.core.root_authority import RootAuthorityWatcher
        watcher = RootAuthorityWatcher(
            session_id="test-session",
            timeout_policy=sample_policy,
            restart_policy=sample_restart_policy,
        )
        watcher.register_subsystem("jarvis-prime", sample_identity)
        verdict = watcher.process_crash("jarvis-prime", exit_code=101)
        assert verdict is not None
        assert verdict.action == LifecycleAction.ESCALATE_OPERATOR

    @pytest.mark.asyncio
    async def test_identity_mismatch_ignored(self, sample_identity, sample_policy, sample_restart_policy):
        from backend.core.root_authority import RootAuthorityWatcher
        watcher = RootAuthorityWatcher(
            session_id="test-session",
            timeout_policy=sample_policy,
            restart_policy=sample_restart_policy,
        )
        watcher.register_subsystem("jarvis-prime", sample_identity)
        watcher.process_health_response("jarvis-prime", {
            "liveness": "up", "readiness": "ready",
            "session_id": "test-session", "pid": 9999,
            "start_time_ns": sample_identity.start_time_ns,
            "exec_fingerprint": "sha256:abc123",
            "schema_version": "1.0.0",
        })
        assert watcher.get_state("jarvis-prime") == SubsystemState.STARTING

    @pytest.mark.asyncio
    async def test_max_restarts_escalates(self, sample_identity, sample_policy, sample_restart_policy):
        from backend.core.root_authority import RootAuthorityWatcher
        watcher = RootAuthorityWatcher(
            session_id="test-session",
            timeout_policy=sample_policy,
            restart_policy=sample_restart_policy,  # max_restarts=2
        )
        watcher.register_subsystem("jarvis-prime", sample_identity)
        watcher.process_crash("jarvis-prime", exit_code=300)
        watcher.register_subsystem("jarvis-prime", sample_identity)
        watcher.process_crash("jarvis-prime", exit_code=300)
        watcher.register_subsystem("jarvis-prime", sample_identity)
        verdict = watcher.process_crash("jarvis-prime", exit_code=300)
        assert verdict is not None
        assert verdict.action == LifecycleAction.ESCALATE_OPERATOR


class TestVerdictDeduplication:
    @pytest.mark.asyncio
    async def test_duplicate_crash_verdicts_coalesced(self, sample_identity, sample_policy, sample_restart_policy):
        from backend.core.root_authority import RootAuthorityWatcher
        watcher = RootAuthorityWatcher(
            session_id="test-session",
            timeout_policy=sample_policy,
            restart_policy=sample_restart_policy,
        )
        watcher.register_subsystem("jarvis-prime", sample_identity)
        v1 = watcher.process_crash("jarvis-prime", exit_code=300)
        v2 = watcher.process_crash("jarvis-prime", exit_code=300)
        assert v1 is not None
        assert v2 is None


class TestWatcherObservability:
    @pytest.mark.asyncio
    async def test_events_emitted_on_state_change(self, sample_identity, sample_policy, sample_restart_policy):
        from backend.core.root_authority import RootAuthorityWatcher
        events = []
        watcher = RootAuthorityWatcher(
            session_id="test-session",
            timeout_policy=sample_policy,
            restart_policy=sample_restart_policy,
            event_sink=lambda e: events.append(e),
        )
        watcher.register_subsystem("jarvis-prime", sample_identity)
        watcher.process_crash("jarvis-prime", exit_code=300)
        assert len(events) >= 2
        event_types = {e.event_type for e in events}
        assert "state_transition" in event_types
