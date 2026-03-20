"""Tests for JprimeLifecycleController - Task 1: LifecycleState, RestartPolicy, LifecycleTransition."""
import asyncio
import pytest
import time
from unittest.mock import AsyncMock, MagicMock, patch
from backend.core.jprime_lifecycle_controller import (
    HealthProbe,
    HealthResult,
    HealthVerdict,
    LifecycleState,
    RestartPolicy,
    LifecycleTransition,
)


class TestLifecycleState:
    def test_all_states_exist(self):
        states = [s.value for s in LifecycleState]
        assert "UNKNOWN" in states
        assert "PROBING" in states
        assert "VM_STARTING" in states
        assert "SVC_STARTING" in states
        assert "READY" in states
        assert "DEGRADED" in states
        assert "UNHEALTHY" in states
        assert "RECOVERING" in states
        assert "COOLDOWN" in states
        assert "TERMINAL" in states

    def test_routable_states(self):
        assert LifecycleState.READY.is_routable is True
        assert LifecycleState.DEGRADED.is_routable is True
        assert LifecycleState.UNKNOWN.is_routable is False
        assert LifecycleState.UNHEALTHY.is_routable is False
        assert LifecycleState.TERMINAL.is_routable is False

    def test_non_routable_states_exhaustive(self):
        """Every state not in {READY, DEGRADED} must not be routable."""
        routable_set = {LifecycleState.READY, LifecycleState.DEGRADED}
        for state in LifecycleState:
            if state in routable_set:
                assert state.is_routable is True, f"{state} should be routable"
            else:
                assert state.is_routable is False, f"{state} should NOT be routable"

    def test_liveness(self):
        assert LifecycleState.READY.is_live is True
        assert LifecycleState.DEGRADED.is_live is True
        assert LifecycleState.SVC_STARTING.is_live is True
        assert LifecycleState.UNHEALTHY.is_live is False
        assert LifecycleState.TERMINAL.is_live is False

    def test_non_live_states_exhaustive(self):
        """Every state not in {READY, DEGRADED, SVC_STARTING} must not be live."""
        live_set = {LifecycleState.READY, LifecycleState.DEGRADED, LifecycleState.SVC_STARTING}
        for state in LifecycleState:
            if state in live_set:
                assert state.is_live is True, f"{state} should be live"
            else:
                assert state.is_live is False, f"{state} should NOT be live"

    def test_state_is_str_enum(self):
        """LifecycleState values are usable as strings."""
        assert LifecycleState.READY == "READY"
        # str(Enum) behavior: on Python 3.11+ returns "ClassName.member",
        # but str(str, Enum) returns the value directly on 3.9/3.10.
        assert str(LifecycleState.READY) in ("READY", "LifecycleState.READY")

    def test_state_count(self):
        """Exactly 10 states defined."""
        assert len(LifecycleState) == 10


class TestRestartPolicy:
    def test_default_policy(self):
        p = RestartPolicy()
        assert p.base_backoff_s == 10.0
        assert p.multiplier == 2.0
        assert p.max_backoff_s == 300.0
        assert p.max_restarts == 5
        assert p.window_s == 1800.0

    def test_from_env(self):
        env = {
            "JPRIME_RESTART_BASE_BACKOFF_S": "5",
            "JPRIME_MAX_RESTARTS_PER_WINDOW": "3",
            "JPRIME_RESTART_WINDOW_S": "600",
        }
        with patch.dict("os.environ", env, clear=False):
            p = RestartPolicy.from_env()
        assert p.base_backoff_s == 5.0
        assert p.max_restarts == 3
        assert p.window_s == 600.0

    def test_from_env_defaults(self):
        """from_env with no env vars uses defaults."""
        with patch.dict("os.environ", {}, clear=True):
            p = RestartPolicy.from_env()
        assert p.base_backoff_s == 10.0
        assert p.max_restarts == 5
        assert p.window_s == 1800.0

    def test_from_env_invalid_values(self):
        """Invalid env var values fall back to defaults."""
        env = {
            "JPRIME_RESTART_BASE_BACKOFF_S": "not_a_number",
            "JPRIME_MAX_RESTARTS_PER_WINDOW": "bad",
        }
        with patch.dict("os.environ", env, clear=False):
            p = RestartPolicy.from_env()
        assert p.base_backoff_s == 10.0
        assert p.max_restarts == 5

    def test_backoff_sequence(self):
        p = RestartPolicy(base_backoff_s=10.0, multiplier=2.0, max_backoff_s=300.0)
        assert p.backoff_for_attempt(1) == 10.0
        assert p.backoff_for_attempt(2) == 20.0
        assert p.backoff_for_attempt(3) == 40.0
        assert p.backoff_for_attempt(4) == 80.0
        assert p.backoff_for_attempt(5) == 160.0
        assert p.backoff_for_attempt(6) == 300.0  # capped

    def test_backoff_capped_at_max(self):
        p = RestartPolicy(base_backoff_s=10.0, multiplier=3.0, max_backoff_s=50.0)
        assert p.backoff_for_attempt(1) == 10.0
        assert p.backoff_for_attempt(2) == 30.0
        assert p.backoff_for_attempt(3) == 50.0  # 90 capped to 50
        assert p.backoff_for_attempt(10) == 50.0  # still capped

    def test_backoff_attempt_zero(self):
        """Attempt 0 should give base_backoff / multiplier (edge case)."""
        p = RestartPolicy(base_backoff_s=10.0, multiplier=2.0, max_backoff_s=300.0)
        # attempt=0 => base * 2^(-1) = 5.0
        assert p.backoff_for_attempt(0) == 5.0

    def test_can_restart(self):
        p = RestartPolicy(max_restarts=3, window_s=60.0)
        now = time.monotonic()
        timestamps = [now - 10, now - 5]
        assert p.can_restart(timestamps, now) is True
        timestamps.append(now - 1)
        assert p.can_restart(timestamps, now) is False

    def test_expired_restarts_not_counted(self):
        p = RestartPolicy(max_restarts=3, window_s=60.0)
        now = time.monotonic()
        timestamps = [now - 120, now - 90, now - 10]
        assert p.can_restart(timestamps, now) is True

    def test_can_restart_empty_timestamps(self):
        p = RestartPolicy(max_restarts=3, window_s=60.0)
        now = time.monotonic()
        assert p.can_restart([], now) is True

    def test_can_restart_all_expired(self):
        p = RestartPolicy(max_restarts=1, window_s=10.0)
        now = time.monotonic()
        timestamps = [now - 100, now - 200, now - 300]
        assert p.can_restart(timestamps, now) is True

    def test_custom_fields(self):
        p = RestartPolicy(
            terminal_cooldown_s=900.0,
            degraded_patience_s=120.0,
        )
        assert p.terminal_cooldown_s == 900.0
        assert p.degraded_patience_s == 120.0


class TestLifecycleTransition:
    def test_transition_fields(self):
        t = LifecycleTransition(
            from_state=LifecycleState.UNHEALTHY,
            to_state=LifecycleState.RECOVERING,
            trigger="auto_recovery",
            reason_code="3_consecutive_failures",
            attempt=1,
        )
        assert t.from_state == LifecycleState.UNHEALTHY
        assert t.to_state == LifecycleState.RECOVERING
        assert t.trigger == "auto_recovery"
        assert t.reason_code == "3_consecutive_failures"
        assert t.attempt == 1

    def test_telemetry_dict(self):
        t = LifecycleTransition(
            from_state=LifecycleState.READY,
            to_state=LifecycleState.DEGRADED,
            trigger="health_check",
            reason_code="3_consecutive_slow",
        )
        d = t.to_telemetry_dict()
        assert d["event"] == "jprime_lifecycle_transition"
        assert d["from_state"] == "READY"
        assert d["to_state"] == "DEGRADED"
        assert "timestamp" in d
        assert isinstance(d["timestamp"], float)
        assert d["trigger"] == "health_check"
        assert d["reason_code"] == "3_consecutive_slow"

    def test_telemetry_dict_all_fields(self):
        t = LifecycleTransition(
            from_state=LifecycleState.RECOVERING,
            to_state=LifecycleState.READY,
            trigger="health_check",
            reason_code="recovery_success",
            root_cause_id="rc-123",
            attempt=3,
            backoff_ms=40000,
            restarts_in_window=2,
            apars_progress=0.75,
            vm_zone="us-central1-b",
            elapsed_in_prev_state_ms=12345.6,
        )
        d = t.to_telemetry_dict()
        assert d["root_cause_id"] == "rc-123"
        assert d["attempt"] == 3
        assert d["backoff_ms"] == 40000
        assert d["restarts_in_window"] == 2
        assert d["apars_progress"] == 0.75
        assert d["vm_zone"] == "us-central1-b"
        assert d["elapsed_in_prev_state_ms"] == 12345.6

    def test_default_optional_fields(self):
        t = LifecycleTransition(
            from_state=LifecycleState.UNKNOWN,
            to_state=LifecycleState.PROBING,
            trigger="boot",
            reason_code="initial_probe",
        )
        assert t.root_cause_id is None
        assert t.attempt == 0
        assert t.backoff_ms is None
        assert t.restarts_in_window == 0
        assert t.apars_progress is None
        assert t.vm_zone is None
        assert t.elapsed_in_prev_state_ms == 0.0

    def test_timestamp_auto_populated(self):
        before = time.time()
        t = LifecycleTransition(
            from_state=LifecycleState.READY,
            to_state=LifecycleState.DEGRADED,
            trigger="test",
            reason_code="test",
        )
        after = time.time()
        assert before <= t.timestamp <= after


class TestHealthVerdict:
    def test_verdict_values(self):
        assert HealthVerdict.READY.value == "READY"
        assert HealthVerdict.ALIVE_NOT_READY.value == "ALIVE_NOT_READY"
        assert HealthVerdict.UNREACHABLE.value == "UNREACHABLE"
        assert HealthVerdict.UNHEALTHY.value == "UNHEALTHY"


class TestHealthProbe:
    @pytest.mark.asyncio
    async def test_ready_verdict(self):
        probe = HealthProbe(host="127.0.0.1", port=8000)
        mock_response = {
            "status": "healthy",
            "ready_for_inference": True,
            "apars": {"total_progress": 100},
        }
        with patch.object(probe, "_http_get", return_value=mock_response):
            result = await probe.check()
        assert result.verdict == HealthVerdict.READY
        assert result.ready_for_inference is True

    @pytest.mark.asyncio
    async def test_alive_not_ready_verdict(self):
        probe = HealthProbe(host="127.0.0.1", port=8000)
        mock_response = {
            "status": "starting",
            "ready_for_inference": False,
            "apars": {"total_progress": 45},
        }
        with patch.object(probe, "_http_get", return_value=mock_response):
            result = await probe.check()
        assert result.verdict == HealthVerdict.ALIVE_NOT_READY
        assert result.apars_progress == 45

    @pytest.mark.asyncio
    async def test_unreachable_on_connection_refused(self):
        probe = HealthProbe(host="127.0.0.1", port=8000)
        with patch.object(probe, "_http_get", side_effect=ConnectionRefusedError()):
            result = await probe.check()
        assert result.verdict == HealthVerdict.UNREACHABLE

    @pytest.mark.asyncio
    async def test_unreachable_on_timeout(self):
        probe = HealthProbe(host="127.0.0.1", port=8000)
        with patch.object(probe, "_http_get", side_effect=asyncio.TimeoutError()):
            result = await probe.check()
        assert result.verdict == HealthVerdict.UNREACHABLE

    @pytest.mark.asyncio
    async def test_unhealthy_on_unexpected_error(self):
        probe = HealthProbe(host="127.0.0.1", port=8000)
        with patch.object(probe, "_http_get", side_effect=ValueError("bad json")):
            result = await probe.check()
        assert result.verdict == HealthVerdict.UNHEALTHY
        assert "bad json" in result.error

    @pytest.mark.asyncio
    async def test_response_time_tracked(self):
        probe = HealthProbe(host="127.0.0.1", port=8000)
        mock_response = {"status": "healthy", "ready_for_inference": True}
        with patch.object(probe, "_http_get", return_value=mock_response):
            result = await probe.check()
        assert result.response_time_ms >= 0

    @pytest.mark.asyncio
    async def test_no_apars_key(self):
        probe = HealthProbe(host="127.0.0.1", port=8000)
        mock_response = {"status": "healthy", "ready_for_inference": True}
        with patch.object(probe, "_http_get", return_value=mock_response):
            result = await probe.check()
        assert result.apars_progress is None


# ---------------------------------------------------------------------------
# Task 3: JprimeLifecycleController tests
# ---------------------------------------------------------------------------

from backend.core.jprime_lifecycle_controller import (
    JprimeLifecycleController,
    get_jprime_lifecycle_controller,
)


class TestControllerStateMachine:
    def _make_controller(self, **overrides):
        """Create a controller with mocked dependencies."""
        policy = RestartPolicy(max_restarts=3, window_s=60.0, base_backoff_s=0.01)
        ctrl = JprimeLifecycleController(
            host="127.0.0.1", port=8000, restart_policy=policy,
        )
        ctrl._probe = AsyncMock()
        ctrl._prime_router_notify = AsyncMock()
        ctrl._mind_client_update = AsyncMock()
        for k, v in overrides.items():
            setattr(ctrl, k, v)
        return ctrl

    @pytest.mark.asyncio
    async def test_initial_state_is_unknown(self):
        ctrl = self._make_controller()
        assert ctrl.state == LifecycleState.UNKNOWN

    @pytest.mark.asyncio
    async def test_probe_ready_transitions_to_ready(self):
        ctrl = self._make_controller()
        ctrl._probe.check.return_value = HealthResult(
            verdict=HealthVerdict.READY, ready_for_inference=True,
        )
        await ctrl._do_probe()
        assert ctrl.state == LifecycleState.READY

    @pytest.mark.asyncio
    async def test_probe_unreachable_transitions_to_unhealthy(self):
        ctrl = self._make_controller()
        ctrl._probe.check.return_value = HealthResult(
            verdict=HealthVerdict.UNREACHABLE, error="connection_refused",
        )
        await ctrl._do_probe()
        assert ctrl.state == LifecycleState.UNHEALTHY

    @pytest.mark.asyncio
    async def test_probe_alive_not_ready_transitions_to_svc_starting(self):
        ctrl = self._make_controller()
        ctrl._probe.check.return_value = HealthResult(
            verdict=HealthVerdict.ALIVE_NOT_READY, apars_progress=45,
        )
        await ctrl._do_probe()
        assert ctrl.state == LifecycleState.SVC_STARTING

    @pytest.mark.asyncio
    async def test_ready_to_unhealthy_after_consecutive_failures(self):
        ctrl = self._make_controller()
        ctrl._state = LifecycleState.READY
        ctrl._state_entered_at = time.monotonic()
        for _ in range(3):
            await ctrl._record_health_result(HealthResult(
                verdict=HealthVerdict.UNREACHABLE,
            ))
        assert ctrl.state == LifecycleState.UNHEALTHY

    @pytest.mark.asyncio
    async def test_ready_to_degraded_after_consecutive_slow(self):
        ctrl = self._make_controller()
        ctrl._state = LifecycleState.READY
        ctrl._state_entered_at = time.monotonic()
        for _ in range(3):
            await ctrl._record_health_result(HealthResult(
                verdict=HealthVerdict.READY, ready_for_inference=True,
                response_time_ms=6000,
            ))
        assert ctrl.state == LifecycleState.DEGRADED

    @pytest.mark.asyncio
    async def test_degraded_to_ready_rolling_window(self):
        ctrl = self._make_controller()
        ctrl._state = LifecycleState.DEGRADED
        ctrl._state_entered_at = time.monotonic()
        results = [
            HealthResult(verdict=HealthVerdict.READY, ready_for_inference=True, response_time_ms=100),
            HealthResult(verdict=HealthVerdict.UNREACHABLE),
            HealthResult(verdict=HealthVerdict.READY, ready_for_inference=True, response_time_ms=100),
            HealthResult(verdict=HealthVerdict.READY, ready_for_inference=True, response_time_ms=100),
        ]
        for r in results:
            await ctrl._record_health_result(r)
        assert ctrl.state == LifecycleState.READY

    @pytest.mark.asyncio
    async def test_unhealthy_to_recovering(self):
        ctrl = self._make_controller()
        ctrl._state = LifecycleState.UNHEALTHY
        ctrl._state_entered_at = time.monotonic()
        await ctrl._evaluate_recovery()
        assert ctrl.state == LifecycleState.RECOVERING

    @pytest.mark.asyncio
    async def test_unhealthy_to_terminal_when_budget_exhausted(self):
        ctrl = self._make_controller()
        ctrl._state = LifecycleState.UNHEALTHY
        ctrl._state_entered_at = time.monotonic()
        now = time.monotonic()
        ctrl._restart_timestamps = [now - i for i in range(3)]
        await ctrl._evaluate_recovery()
        assert ctrl.state == LifecycleState.TERMINAL

    @pytest.mark.asyncio
    async def test_transition_emits_telemetry(self):
        ctrl = self._make_controller()
        await ctrl._transition(
            LifecycleState.PROBING, "boot", "initial_probe",
        )
        assert len(ctrl._transitions) == 1
        assert ctrl._transitions[0].from_state == LifecycleState.UNKNOWN
        assert ctrl._transitions[0].to_state == LifecycleState.PROBING

    @pytest.mark.asyncio
    async def test_same_state_transition_is_noop(self):
        ctrl = self._make_controller()
        ctrl._state = LifecycleState.READY
        ctrl._state_entered_at = time.monotonic()
        await ctrl._transition(LifecycleState.READY, "noop", "already_ready")
        assert len(ctrl._transitions) == 0


class TestControllerIdempotentBoot:
    @pytest.mark.asyncio
    async def test_concurrent_ensure_ready_collapses(self):
        policy = RestartPolicy(max_restarts=3, window_s=60.0, base_backoff_s=0.01)
        ctrl = JprimeLifecycleController(
            host="127.0.0.1", port=8000, restart_policy=policy,
        )
        ctrl._probe = AsyncMock()
        ctrl._probe.check.return_value = HealthResult(
            verdict=HealthVerdict.READY, ready_for_inference=True,
        )
        ctrl._prime_router_notify = AsyncMock()
        ctrl._mind_client_update = AsyncMock()

        results = await asyncio.gather(
            ctrl.ensure_ready(timeout=5),
            ctrl.ensure_ready(timeout=5),
            ctrl.ensure_ready(timeout=5),
        )
        assert all(r == results[0] for r in results)
        # Probe called only once (idempotent)
        assert ctrl._probe.check.call_count <= 2  # probe + maybe one poll

    @pytest.mark.asyncio
    async def test_ensure_ready_during_terminal_returns_level2(self):
        policy = RestartPolicy(max_restarts=3, window_s=60.0)
        ctrl = JprimeLifecycleController(
            host="127.0.0.1", port=8000, restart_policy=policy,
        )
        ctrl._state = LifecycleState.TERMINAL
        ctrl._state_entered_at = time.monotonic()
        result = await ctrl.ensure_ready(timeout=5)
        assert result == "LEVEL_2"

    @pytest.mark.asyncio
    async def test_ensure_ready_returns_level0_on_ready(self):
        policy = RestartPolicy(max_restarts=3, window_s=60.0, base_backoff_s=0.01)
        ctrl = JprimeLifecycleController(
            host="127.0.0.1", port=8000, restart_policy=policy,
        )
        ctrl._probe = AsyncMock()
        ctrl._probe.check.return_value = HealthResult(
            verdict=HealthVerdict.READY, ready_for_inference=True,
        )
        ctrl._prime_router_notify = AsyncMock()
        ctrl._mind_client_update = AsyncMock()
        level = await ctrl.ensure_ready(timeout=10)
        assert level == "LEVEL_0"
        assert ctrl.state == LifecycleState.READY

    @pytest.mark.asyncio
    async def test_ensure_ready_timeout_returns_level2(self):
        policy = RestartPolicy(max_restarts=1, window_s=60.0, base_backoff_s=0.01)
        ctrl = JprimeLifecycleController(
            host="127.0.0.1", port=8000, restart_policy=policy,
        )
        ctrl._probe = AsyncMock()
        ctrl._probe.check.return_value = HealthResult(
            verdict=HealthVerdict.UNREACHABLE, error="timeout",
        )
        ctrl._prime_router_notify = AsyncMock()
        ctrl._mind_client_update = AsyncMock()
        level = await ctrl.ensure_ready(timeout=1.0)
        assert level == "LEVEL_2"


class TestControllerSingleton:
    def test_singleton(self):
        import backend.core.jprime_lifecycle_controller as mod
        mod._controller_instance = None
        c1 = get_jprime_lifecycle_controller()
        c2 = get_jprime_lifecycle_controller()
        assert c1 is c2
        mod._controller_instance = None
