# tests/unit/core/test_contract_gating.py
"""Tests for contract hash gating at boot handshake."""
import os
import time
import pytest

from backend.core.root_authority_types import (
    ProcessIdentity, SubsystemState, ContractGate,
    TimeoutPolicy, RestartPolicy, LifecycleAction,
)


@pytest.fixture
def identity():
    return ProcessIdentity(
        pid=100, start_time_ns=time.monotonic_ns(),
        session_id="test", exec_fingerprint="sha256:abc"
    )


@pytest.fixture
def watcher():
    from backend.core.root_authority import RootAuthorityWatcher
    gate = ContractGate(
        subsystem="test-svc",
        expected_schema_version="1.0.0",
        expected_capability_hash="sha256:expected123",
        required_health_fields=frozenset({"liveness", "readiness", "session_id"}),
        required_endpoints=frozenset({"/health"}),
    )
    return RootAuthorityWatcher(
        session_id="test",
        timeout_policy=TimeoutPolicy(),
        restart_policy=RestartPolicy(),
        contract_gates={"test-svc": gate},
    )


class TestContractGating:
    def test_handshake_passes_with_matching_contract(self, watcher, identity):
        """Handshake succeeds when schema, capability hash, and fields match."""
        watcher.register_subsystem("test-svc", identity)
        verdict = watcher.process_health_response("test-svc", {
            "liveness": "up", "readiness": "ready",
            "session_id": "test", "pid": 100,
            "start_time_ns": identity.start_time_ns,
            "exec_fingerprint": "sha256:abc",
            "schema_version": "1.0.0",
            "capability_hash": "sha256:expected123",
        })
        assert verdict is None
        assert watcher.get_state("test-svc") in (
            SubsystemState.ALIVE, SubsystemState.READY
        )

    def test_handshake_fails_schema_mismatch(self, watcher, identity):
        """Handshake fails when schema version is incompatible."""
        watcher.register_subsystem("test-svc", identity)
        verdict = watcher.process_health_response("test-svc", {
            "liveness": "up", "readiness": "ready",
            "session_id": "test", "pid": 100,
            "start_time_ns": identity.start_time_ns,
            "exec_fingerprint": "sha256:abc",
            "schema_version": "2.0.0",  # Major version mismatch
            "capability_hash": "sha256:expected123",
        })
        assert verdict is not None
        assert verdict.action == LifecycleAction.ESCALATE_OPERATOR
        assert watcher.get_state("test-svc") == SubsystemState.REJECTED

    def test_handshake_fails_missing_fields(self, watcher, identity):
        """Handshake fails when required health fields are missing."""
        watcher.register_subsystem("test-svc", identity)
        verdict = watcher.process_health_response("test-svc", {
            "liveness": "up",
            # missing readiness and session_id
            "pid": 100,
            "start_time_ns": identity.start_time_ns,
            "exec_fingerprint": "sha256:abc",
            "schema_version": "1.0.0",
        })
        assert verdict is not None
        assert verdict.action == LifecycleAction.ESCALATE_OPERATOR
        assert watcher.get_state("test-svc") == SubsystemState.REJECTED

    def test_handshake_fails_capability_hash_mismatch(self, watcher, identity):
        """Handshake fails when capability hash doesn't match."""
        watcher.register_subsystem("test-svc", identity)
        verdict = watcher.process_health_response("test-svc", {
            "liveness": "up", "readiness": "ready",
            "session_id": "test", "pid": 100,
            "start_time_ns": identity.start_time_ns,
            "exec_fingerprint": "sha256:abc",
            "schema_version": "1.0.0",
            "capability_hash": "sha256:WRONG",
        })
        assert verdict is not None
        assert verdict.action == LifecycleAction.ESCALATE_OPERATOR
        assert watcher.get_state("test-svc") == SubsystemState.REJECTED

    def test_emergency_bypass(self, watcher, identity, monkeypatch):
        """JARVIS_CONTRACT_BYPASS skips contract gating for named subsystem."""
        monkeypatch.setenv("JARVIS_CONTRACT_BYPASS", "test-svc")
        watcher.register_subsystem("test-svc", identity)
        verdict = watcher.process_health_response("test-svc", {
            "liveness": "up", "readiness": "ready",
            "session_id": "test", "pid": 100,
            "start_time_ns": identity.start_time_ns,
            "exec_fingerprint": "sha256:abc",
            "schema_version": "2.0.0",  # Would normally fail
            "capability_hash": "sha256:WRONG",  # Would normally fail
        })
        assert verdict is None  # Bypass means no rejection
        assert watcher.get_state("test-svc") != SubsystemState.REJECTED

    def test_n_minus_1_minor_version_passes(self, watcher, identity):
        """Schema version with minor off by 1 should pass."""
        watcher.register_subsystem("test-svc", identity)
        verdict = watcher.process_health_response("test-svc", {
            "liveness": "up", "readiness": "ready",
            "session_id": "test", "pid": 100,
            "start_time_ns": identity.start_time_ns,
            "exec_fingerprint": "sha256:abc",
            "schema_version": "1.1.0",  # minor +1, should be compatible
            "capability_hash": "sha256:expected123",
        })
        assert verdict is None
        assert watcher.get_state("test-svc") != SubsystemState.REJECTED
