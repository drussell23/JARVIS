"""Tests for root authority contract types (Task 1 of Triple Authority Resolution)."""
import math
import pytest


class TestSchemaVersion:
    def test_schema_version_defined(self):
        from backend.core.root_authority_types import SCHEMA_VERSION
        assert SCHEMA_VERSION == "1.0.0"


class TestLifecycleAction:
    def test_enum_values(self):
        from backend.core.root_authority_types import LifecycleAction
        assert LifecycleAction.DRAIN.value == "drain"
        assert LifecycleAction.TERM.value == "term"
        assert LifecycleAction.GROUP_KILL.value == "group_kill"
        assert LifecycleAction.RESTART.value == "restart"
        assert LifecycleAction.ESCALATE_OPERATOR.value == "escalate_operator"

    def test_enum_count(self):
        from backend.core.root_authority_types import LifecycleAction
        assert len(LifecycleAction) == 5


class TestSubsystemState:
    def test_enum_values(self):
        from backend.core.root_authority_types import SubsystemState
        expected = {
            "STARTING", "HANDSHAKE", "ALIVE", "READY", "DEGRADED",
            "DRAINING", "STOPPED", "CRASHED", "REJECTED",
        }
        assert {s.name for s in SubsystemState} == expected

    def test_terminal_states(self):
        from backend.core.root_authority_types import SubsystemState
        terminal = [s for s in SubsystemState if s.is_terminal]
        names = {s.name for s in terminal}
        assert names == {"STOPPED", "CRASHED", "REJECTED"}

    def test_non_terminal_states(self):
        from backend.core.root_authority_types import SubsystemState
        non_terminal = [s for s in SubsystemState if not s.is_terminal]
        for s in non_terminal:
            assert s.name not in {"STOPPED", "CRASHED", "REJECTED"}


class TestProcessIdentity:
    def test_creation(self):
        from backend.core.root_authority_types import ProcessIdentity
        pi = ProcessIdentity(
            pid=1234,
            start_time_ns=1000000000,
            session_id="sess-abc",
            exec_fingerprint="deadbeef01234567",
        )
        assert pi.pid == 1234
        assert pi.start_time_ns == 1000000000
        assert pi.session_id == "sess-abc"
        assert pi.exec_fingerprint == "deadbeef01234567"

    def test_frozen(self):
        from backend.core.root_authority_types import ProcessIdentity
        pi = ProcessIdentity(
            pid=1, start_time_ns=0, session_id="s", exec_fingerprint="f",
        )
        with pytest.raises(AttributeError):
            pi.pid = 999

    def test_equality(self):
        from backend.core.root_authority_types import ProcessIdentity
        a = ProcessIdentity(pid=1, start_time_ns=0, session_id="s", exec_fingerprint="f")
        b = ProcessIdentity(pid=1, start_time_ns=0, session_id="s", exec_fingerprint="f")
        assert a == b

    def test_inequality(self):
        from backend.core.root_authority_types import ProcessIdentity
        a = ProcessIdentity(pid=1, start_time_ns=0, session_id="s", exec_fingerprint="f")
        c = ProcessIdentity(pid=2, start_time_ns=0, session_id="s", exec_fingerprint="f")
        assert a != c


class TestLifecycleVerdict:
    def test_creation(self):
        from backend.core.root_authority_types import (
            LifecycleVerdict, LifecycleAction, ProcessIdentity,
        )
        ident = ProcessIdentity(
            pid=10, start_time_ns=500, session_id="sess-1", exec_fingerprint="abcd1234abcd1234",
        )
        v = LifecycleVerdict(
            subsystem="reactor-core",
            identity=ident,
            action=LifecycleAction.DRAIN,
            reason="health check timeout",
            reason_code="HEALTH_TIMEOUT",
            correlation_id="corr-001",
            incident_id="inc-001",
            exit_code=None,
            observed_at_ns=999,
            wall_time_utc="2025-01-01T00:00:00Z",
        )
        assert v.subsystem == "reactor-core"
        assert v.action == LifecycleAction.DRAIN
        assert v.reason_code == "HEALTH_TIMEOUT"
        assert v.exit_code is None

    def test_frozen(self):
        from backend.core.root_authority_types import (
            LifecycleVerdict, LifecycleAction, ProcessIdentity,
        )
        ident = ProcessIdentity(pid=1, start_time_ns=0, session_id="s", exec_fingerprint="f" * 16)
        v = LifecycleVerdict(
            subsystem="x", identity=ident, action=LifecycleAction.TERM,
            reason="r", reason_code="RC", correlation_id="c",
            incident_id="i", exit_code=1, observed_at_ns=0,
            wall_time_utc="t",
        )
        with pytest.raises(AttributeError):
            v.action = LifecycleAction.RESTART


class TestExecutionResult:
    def test_creation(self):
        from backend.core.root_authority_types import ExecutionResult
        r = ExecutionResult(
            accepted=True,
            executed=True,
            result="drained successfully",
            new_identity=None,
            error_code=None,
            correlation_id="corr-002",
        )
        assert r.accepted is True
        assert r.executed is True
        assert r.new_identity is None

    def test_with_new_identity(self):
        from backend.core.root_authority_types import ExecutionResult, ProcessIdentity
        new_id = ProcessIdentity(pid=99, start_time_ns=1, session_id="new", exec_fingerprint="a" * 16)
        r = ExecutionResult(
            accepted=True, executed=True, result="restarted",
            new_identity=new_id, error_code=None, correlation_id="c",
        )
        assert r.new_identity.pid == 99

    def test_frozen(self):
        from backend.core.root_authority_types import ExecutionResult
        r = ExecutionResult(
            accepted=True, executed=False, result="",
            new_identity=None, error_code="E001", correlation_id="c",
        )
        with pytest.raises(AttributeError):
            r.accepted = False


class TestTimeoutPolicy:
    def test_defaults(self):
        from backend.core.root_authority_types import TimeoutPolicy
        tp = TimeoutPolicy()
        assert tp.startup_grace_s == 120.0
        assert tp.health_timeout_s == 5.0
        assert tp.health_poll_interval_s == 5.0
        assert tp.drain_timeout_s == 30.0
        assert tp.term_timeout_s == 10.0
        assert tp.degraded_tolerance_s == 60.0
        assert tp.degraded_recovery_check_s == 10.0

    def test_custom_values(self):
        from backend.core.root_authority_types import TimeoutPolicy
        tp = TimeoutPolicy(startup_grace_s=60.0, health_timeout_s=10.0)
        assert tp.startup_grace_s == 60.0
        assert tp.health_timeout_s == 10.0


class TestRestartPolicy:
    def test_defaults(self):
        from backend.core.root_authority_types import RestartPolicy
        rp = RestartPolicy()
        assert rp.max_restarts == 3
        assert rp.window_s == 300.0
        assert rp.base_delay_s == 2.0
        assert rp.max_delay_s == 60.0
        assert rp.jitter_factor == 0.3
        assert 0 in rp.no_restart_exit_codes
        assert 100 in rp.no_restart_exit_codes
        assert 109 in rp.no_restart_exit_codes
        assert 200 in rp.retry_exit_codes
        assert 209 in rp.retry_exit_codes

    def test_compute_delay_no_jitter(self):
        from backend.core.root_authority_types import RestartPolicy
        rp = RestartPolicy(jitter_factor=0.0)
        # attempt 0 -> base_delay * 2^0 = 2.0
        assert rp.compute_delay(0) == 2.0
        # attempt 1 -> base_delay * 2^1 = 4.0
        assert rp.compute_delay(1) == 4.0
        # attempt 2 -> base_delay * 2^2 = 8.0
        assert rp.compute_delay(2) == 8.0

    def test_compute_delay_with_jitter(self):
        from backend.core.root_authority_types import RestartPolicy
        rp = RestartPolicy(jitter_factor=0.3)
        # The delay should be within [base * 2^attempt * (1-jitter), base * 2^attempt * (1+jitter)]
        # but capped at max_delay
        for _ in range(50):
            delay = rp.compute_delay(1)
            base = 2.0 * (2 ** 1)  # 4.0
            assert base * (1 - 0.3) <= delay <= base * (1 + 0.3)

    def test_delay_increases_with_attempts(self):
        from backend.core.root_authority_types import RestartPolicy
        rp = RestartPolicy(jitter_factor=0.0)
        delays = [rp.compute_delay(i) for i in range(5)]
        for i in range(1, len(delays)):
            assert delays[i] > delays[i - 1] or delays[i] == rp.max_delay_s

    def test_delay_capped_at_max(self):
        from backend.core.root_authority_types import RestartPolicy
        rp = RestartPolicy(jitter_factor=0.0, max_delay_s=10.0)
        # attempt 10 -> base_delay * 2^10 = 2048.0 -> capped at 10.0
        assert rp.compute_delay(10) == 10.0

    def test_should_restart_no_restart_exit_codes(self):
        from backend.core.root_authority_types import RestartPolicy
        rp = RestartPolicy()
        assert rp.should_restart(0) is False
        assert rp.should_restart(100) is False
        assert rp.should_restart(105) is False
        assert rp.should_restart(109) is False

    def test_should_restart_retry_exit_codes(self):
        from backend.core.root_authority_types import RestartPolicy
        rp = RestartPolicy()
        assert rp.should_restart(200) is True
        assert rp.should_restart(205) is True
        assert rp.should_restart(209) is True

    def test_should_restart_unknown_exit_codes(self):
        from backend.core.root_authority_types import RestartPolicy
        rp = RestartPolicy()
        # Unknown exit codes default to True (should restart)
        assert rp.should_restart(1) is True
        assert rp.should_restart(42) is True
        assert rp.should_restart(137) is True
        assert rp.should_restart(255) is True


class TestContractGate:
    def test_creation(self):
        from backend.core.root_authority_types import ContractGate
        cg = ContractGate(
            subsystem="jarvis-prime",
            expected_schema_version="1.0.0",
            expected_capability_hash=None,
            required_health_fields=frozenset({"status", "uptime_s"}),
            required_endpoints=frozenset({"/health", "/ready"}),
        )
        assert cg.subsystem == "jarvis-prime"
        assert "status" in cg.required_health_fields

    def test_frozen(self):
        from backend.core.root_authority_types import ContractGate
        cg = ContractGate(
            subsystem="x",
            expected_schema_version="1.0.0",
            expected_capability_hash=None,
            required_health_fields=frozenset(),
            required_endpoints=frozenset(),
        )
        with pytest.raises(AttributeError):
            cg.subsystem = "y"

    def test_schema_compatible_same_version(self):
        from backend.core.root_authority_types import ContractGate
        cg = ContractGate(
            subsystem="x",
            expected_schema_version="1.2.0",
            expected_capability_hash=None,
            required_health_fields=frozenset(),
            required_endpoints=frozenset(),
        )
        assert cg.is_schema_compatible("1.2.0") is True

    def test_schema_compatible_patch_difference(self):
        from backend.core.root_authority_types import ContractGate
        cg = ContractGate(
            subsystem="x",
            expected_schema_version="1.2.0",
            expected_capability_hash=None,
            required_health_fields=frozenset(),
            required_endpoints=frozenset(),
        )
        # Different patch version should be compatible
        assert cg.is_schema_compatible("1.2.5") is True

    def test_schema_compatible_n_minus_1_minor(self):
        from backend.core.root_authority_types import ContractGate
        cg = ContractGate(
            subsystem="x",
            expected_schema_version="1.3.0",
            expected_capability_hash=None,
            required_health_fields=frozenset(),
            required_endpoints=frozenset(),
        )
        # N-1 minor version should be compatible
        assert cg.is_schema_compatible("1.2.0") is True
        # N+1 minor version should also be compatible (we're the N-1)
        assert cg.is_schema_compatible("1.4.0") is True

    def test_schema_incompatible_major_mismatch(self):
        from backend.core.root_authority_types import ContractGate
        cg = ContractGate(
            subsystem="x",
            expected_schema_version="1.2.0",
            expected_capability_hash=None,
            required_health_fields=frozenset(),
            required_endpoints=frozenset(),
        )
        assert cg.is_schema_compatible("2.0.0") is False

    def test_schema_incompatible_too_old_minor(self):
        from backend.core.root_authority_types import ContractGate
        cg = ContractGate(
            subsystem="x",
            expected_schema_version="1.5.0",
            expected_capability_hash=None,
            required_health_fields=frozenset(),
            required_endpoints=frozenset(),
        )
        # Minor version difference > 1 should be incompatible
        assert cg.is_schema_compatible("1.3.0") is False


class TestLifecycleEvent:
    def test_creation(self):
        from backend.core.root_authority_types import (
            LifecycleEvent, ProcessIdentity, SubsystemState, LifecycleAction,
        )
        ident = ProcessIdentity(pid=5, start_time_ns=100, session_id="s1", exec_fingerprint="f" * 16)
        evt = LifecycleEvent(
            event_type="state_change",
            subsystem="reactor-core",
            correlation_id="corr-100",
            session_id="s1",
            identity=ident,
            from_state=SubsystemState.ALIVE,
            to_state=SubsystemState.DEGRADED,
            verdict_action=None,
            reason_code="HEALTH_DEGRADED",
            exit_code=None,
            observed_at_ns=12345,
            wall_time_utc="2025-06-01T12:00:00Z",
            policy_source="timeout_policy",
        )
        assert evt.event_type == "state_change"
        assert evt.from_state == SubsystemState.ALIVE
        assert evt.to_state == SubsystemState.DEGRADED
        assert evt.identity.pid == 5

    def test_frozen(self):
        from backend.core.root_authority_types import LifecycleEvent
        evt = LifecycleEvent(
            event_type="t", subsystem="s", correlation_id="c",
            session_id="s", identity=None, from_state=None,
            to_state=None, verdict_action=None, reason_code=None,
            exit_code=None, observed_at_ns=0, wall_time_utc="",
            policy_source="",
        )
        with pytest.raises(AttributeError):
            evt.event_type = "other"


class TestUtilityFunctions:
    def test_compute_exec_fingerprint(self):
        from backend.core.root_authority_types import compute_exec_fingerprint
        fp = compute_exec_fingerprint("/usr/bin/python3", ["python3", "main.py"])
        assert isinstance(fp, str)
        assert len(fp) == 16
        # All hex characters
        int(fp, 16)

    def test_exec_fingerprint_deterministic(self):
        from backend.core.root_authority_types import compute_exec_fingerprint
        fp1 = compute_exec_fingerprint("/usr/bin/python3", ["python3", "main.py"])
        fp2 = compute_exec_fingerprint("/usr/bin/python3", ["python3", "main.py"])
        assert fp1 == fp2

    def test_exec_fingerprint_different_inputs(self):
        from backend.core.root_authority_types import compute_exec_fingerprint
        fp1 = compute_exec_fingerprint("/usr/bin/python3", ["python3", "a.py"])
        fp2 = compute_exec_fingerprint("/usr/bin/python3", ["python3", "b.py"])
        assert fp1 != fp2

    def test_compute_capability_hash(self):
        from backend.core.root_authority_types import compute_capability_hash
        h = compute_capability_hash({"health": True, "metrics": True})
        assert isinstance(h, str)
        assert len(h) == 16

    def test_capability_hash_deterministic(self):
        from backend.core.root_authority_types import compute_capability_hash
        h1 = compute_capability_hash({"b": 2, "a": 1})
        h2 = compute_capability_hash({"a": 1, "b": 2})
        assert h1 == h2  # Deterministic regardless of key order

    def test_compute_incident_id(self):
        from backend.core.root_authority_types import (
            compute_incident_id, ProcessIdentity,
        )
        ident = ProcessIdentity(pid=1, start_time_ns=0, session_id="s", exec_fingerprint="f" * 16)
        iid = compute_incident_id("reactor-core", ident, "HEALTH_TIMEOUT", 1000000000)
        assert isinstance(iid, str)
        assert len(iid) > 0

    def test_incident_id_dedup_within_bucket(self):
        """Two incidents within the same 60s bucket produce the same ID."""
        from backend.core.root_authority_types import (
            compute_incident_id, ProcessIdentity,
        )
        ident = ProcessIdentity(pid=1, start_time_ns=0, session_id="s", exec_fingerprint="f" * 16)
        # 60s = 60_000_000_000 ns
        t1 = 120_000_000_000  # bucket 2
        t2 = 150_000_000_000  # still bucket 2 (150/60 = 2.5 -> floor = 2)
        id1 = compute_incident_id("sub", ident, "RC", t1)
        id2 = compute_incident_id("sub", ident, "RC", t2)
        assert id1 == id2

    def test_incident_id_different_across_buckets(self):
        """Two incidents in different 60s buckets produce different IDs."""
        from backend.core.root_authority_types import (
            compute_incident_id, ProcessIdentity,
        )
        ident = ProcessIdentity(pid=1, start_time_ns=0, session_id="s", exec_fingerprint="f" * 16)
        t1 = 60_000_000_000   # bucket 1
        t2 = 120_000_000_000  # bucket 2
        id1 = compute_incident_id("sub", ident, "RC", t1)
        id2 = compute_incident_id("sub", ident, "RC", t2)
        assert id1 != id2
