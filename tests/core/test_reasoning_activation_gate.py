"""Tests for ReasoningActivationGate."""
import asyncio
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from backend.core.reasoning_activation_gate import (
    GateState,
    GateConfig,
    DepHealth,
    DepStatus,
    CRITICAL_FOR_REASONING,
    DEGRADED_OVERRIDES,
)


class TestGateState:
    def test_all_states(self):
        states = [s.value for s in GateState]
        for expected in ["DISABLED", "WAITING_DEPS", "READY", "ACTIVE", "DEGRADED", "BLOCKED", "TERMINAL"]:
            assert expected in states

    def test_accepts_commands(self):
        assert GateState.ACTIVE.accepts_commands is True
        assert GateState.DEGRADED.accepts_commands is True
        assert GateState.DISABLED.accepts_commands is False
        assert GateState.WAITING_DEPS.accepts_commands is False
        assert GateState.READY.accepts_commands is False
        assert GateState.BLOCKED.accepts_commands is False
        assert GateState.TERMINAL.accepts_commands is False

    def test_state_count(self):
        assert len(GateState) == 7


class TestGateConfig:
    def test_defaults(self):
        c = GateConfig()
        assert c.activation_dwell_s == 5.0
        assert c.min_state_dwell_s == 3.0
        assert c.degrade_threshold == 3
        assert c.block_threshold == 3
        assert c.recovery_threshold == 3
        assert c.max_block_duration_s == 300.0
        assert c.terminal_cooldown_s == 900.0
        assert c.dep_poll_interval_s == 10.0

    def test_from_env(self):
        env = {
            "REASONING_ACTIVATION_DWELL_S": "10",
            "REASONING_DEGRADE_THRESHOLD": "5",
            "REASONING_DEP_POLL_S": "20",
        }
        with patch.dict("os.environ", env, clear=False):
            c = GateConfig.from_env()
        assert c.activation_dwell_s == 10.0
        assert c.degrade_threshold == 5
        assert c.dep_poll_interval_s == 20.0

    def test_from_env_with_invalid_values(self):
        env = {"REASONING_ACTIVATION_DWELL_S": "not_a_number"}
        with patch.dict("os.environ", env, clear=False):
            c = GateConfig.from_env()
        assert c.activation_dwell_s == 5.0  # Falls back to default


class TestDepStatus:
    def test_status_values(self):
        assert DepStatus.HEALTHY.value == "HEALTHY"
        assert DepStatus.DEGRADED.value == "DEGRADED"
        assert DepStatus.UNAVAILABLE.value == "UNAVAILABLE"


class TestDepHealth:
    def test_create(self):
        d = DepHealth(name="jprime_lifecycle", status=DepStatus.HEALTHY)
        assert d.name == "jprime_lifecycle"
        assert d.status == DepStatus.HEALTHY
        assert d.error is None

    def test_with_error(self):
        d = DepHealth(name="coordinator_agent", status=DepStatus.UNAVAILABLE, error="timeout")
        assert d.error == "timeout"


class TestCriticalDeps:
    def test_critical_set(self):
        assert "jprime_lifecycle" in CRITICAL_FOR_REASONING
        assert "coordinator_agent" in CRITICAL_FOR_REASONING
        assert "predictive_planner" in CRITICAL_FOR_REASONING
        assert "proactive_detector" in CRITICAL_FOR_REASONING
        assert len(CRITICAL_FOR_REASONING) == 4


class TestDegradedOverrides:
    def test_overrides_exist(self):
        assert "proactive_threshold_boost" in DEGRADED_OVERRIDES
        assert "auto_expand_threshold" in DEGRADED_OVERRIDES
        assert "expansion_timeout_factor" in DEGRADED_OVERRIDES
        assert "mind_request_timeout_factor" in DEGRADED_OVERRIDES

    def test_override_values(self):
        assert DEGRADED_OVERRIDES["proactive_threshold_boost"] == 0.1
        assert DEGRADED_OVERRIDES["auto_expand_threshold"] == 1.0
        assert DEGRADED_OVERRIDES["expansion_timeout_factor"] == 0.5


# ---------------------------------------------------------------------------
# Task 2: ReasoningActivationGate tests
# ---------------------------------------------------------------------------

from backend.core.reasoning_activation_gate import (
    ReasoningActivationGate,
    get_reasoning_activation_gate,
)


def _mock_dep_statuses(
    jprime="HEALTHY",
    coordinator="HEALTHY",
    planner="HEALTHY",
    detector="HEALTHY",
):
    return {
        "jprime_lifecycle": DepHealth("jprime_lifecycle", DepStatus(jprime)),
        "coordinator_agent": DepHealth("coordinator_agent", DepStatus(coordinator)),
        "predictive_planner": DepHealth("predictive_planner", DepStatus(planner)),
        "proactive_detector": DepHealth("proactive_detector", DepStatus(detector)),
    }


def _fast_config(**overrides):
    """GateConfig with very short timers for tests.

    min_state_dwell_s defaults to 0 so multi-step test transitions
    are not blocked by flap suppression.  The flap suppression test
    explicitly sets a non-zero value.
    """
    defaults = dict(
        activation_dwell_s=0.01,
        min_state_dwell_s=0.0,
        degrade_threshold=3,
        block_threshold=3,
        recovery_threshold=3,
        max_block_duration_s=0.05,
        terminal_cooldown_s=0.05,
        dep_poll_interval_s=0.01,
    )
    defaults.update(overrides)
    return GateConfig(**defaults)


def _make_gate(**config_overrides):
    """Create a gate with fast config and injectable dep checker."""
    gate = ReasoningActivationGate(config=_fast_config(**config_overrides))
    gate._check_all_deps = AsyncMock(return_value=_mock_dep_statuses())
    return gate


class TestGateTransitions:
    """Core FSM transition tests."""

    def test_initial_state_disabled(self):
        gate = _make_gate()
        assert gate.state == GateState.DISABLED

    @pytest.mark.asyncio
    async def test_flags_on_transitions_to_waiting(self):
        gate = _make_gate()
        assert gate.state == GateState.DISABLED
        with patch.dict("os.environ", {
            "JARVIS_REASONING_CHAIN_ENABLED": "true",
        }, clear=False):
            await gate._evaluate_flags()
        assert gate.state == GateState.WAITING_DEPS

    @pytest.mark.asyncio
    async def test_all_deps_healthy_transitions_to_ready(self):
        gate = _make_gate()
        # Move to WAITING_DEPS first
        await gate._try_transition(GateState.WAITING_DEPS, "test", "test_setup")
        assert gate.state == GateState.WAITING_DEPS
        # Now evaluate deps — all healthy should go to READY
        await gate._evaluate_deps()
        assert gate.state == GateState.READY

    @pytest.mark.asyncio
    async def test_ready_dwell_transitions_to_active(self):
        gate = _make_gate(activation_dwell_s=0.01)
        await gate._try_transition(GateState.WAITING_DEPS, "test", "setup")
        await gate._try_transition(GateState.READY, "test", "setup")
        assert gate.state == GateState.READY
        await asyncio.sleep(0.02)
        await gate._evaluate_dwell()
        assert gate.state == GateState.ACTIVE

    @pytest.mark.asyncio
    async def test_active_accepts_commands(self):
        gate = _make_gate()
        await gate._try_transition(GateState.WAITING_DEPS, "test", "setup")
        await gate._try_transition(GateState.READY, "test", "setup")
        await asyncio.sleep(0.02)
        await gate._evaluate_dwell()
        assert gate.state == GateState.ACTIVE
        assert gate.is_active() is True

    @pytest.mark.asyncio
    async def test_degraded_accepts_commands(self):
        gate = _make_gate()
        await gate._try_transition(GateState.DEGRADED, "test", "setup")
        assert gate.is_active() is True

    @pytest.mark.asyncio
    async def test_blocked_rejects_commands(self):
        gate = _make_gate()
        await gate._try_transition(GateState.BLOCKED, "test", "setup")
        assert gate.is_active() is False

    @pytest.mark.asyncio
    async def test_active_to_degraded_on_jprime_degraded(self):
        gate = _make_gate(degrade_threshold=3)
        # Get to ACTIVE
        await gate._try_transition(GateState.WAITING_DEPS, "test", "s")
        await gate._try_transition(GateState.READY, "test", "s")
        await asyncio.sleep(0.02)
        await gate._evaluate_dwell()
        assert gate.state == GateState.ACTIVE

        # 3 consecutive degraded polls
        gate._check_all_deps = AsyncMock(
            return_value=_mock_dep_statuses(jprime="DEGRADED")
        )
        for _ in range(3):
            await gate._evaluate_deps()
        assert gate.state == GateState.DEGRADED

    @pytest.mark.asyncio
    async def test_active_to_blocked_on_dep_unavailable(self):
        gate = _make_gate(block_threshold=3)
        # Get to ACTIVE
        await gate._try_transition(GateState.WAITING_DEPS, "test", "s")
        await gate._try_transition(GateState.READY, "test", "s")
        await asyncio.sleep(0.02)
        await gate._evaluate_dwell()
        assert gate.state == GateState.ACTIVE

        # 3 consecutive unavailable polls
        gate._check_all_deps = AsyncMock(
            return_value=_mock_dep_statuses(jprime="UNAVAILABLE")
        )
        for _ in range(3):
            await gate._evaluate_deps()
        assert gate.state == GateState.BLOCKED

    @pytest.mark.asyncio
    async def test_degraded_to_active_on_recovery(self):
        gate = _make_gate(recovery_threshold=3)
        await gate._try_transition(GateState.DEGRADED, "test", "setup")
        assert gate.state == GateState.DEGRADED

        # 3 consecutive healthy polls
        gate._check_all_deps = AsyncMock(return_value=_mock_dep_statuses())
        for _ in range(3):
            await gate._evaluate_deps()
        assert gate.state == GateState.ACTIVE

    @pytest.mark.asyncio
    async def test_blocked_to_terminal_after_max_duration(self):
        gate = _make_gate(max_block_duration_s=0.01)
        await gate._try_transition(GateState.BLOCKED, "test", "setup")
        assert gate.state == GateState.BLOCKED

        # Still failing deps
        gate._check_all_deps = AsyncMock(
            return_value=_mock_dep_statuses(jprime="UNAVAILABLE")
        )
        await asyncio.sleep(0.02)
        await gate._evaluate_block_duration()
        assert gate.state == GateState.TERMINAL

    @pytest.mark.asyncio
    async def test_terminal_auto_resets(self):
        gate = _make_gate(terminal_cooldown_s=0.01)
        await gate._try_transition(GateState.TERMINAL, "test", "setup")
        assert gate.state == GateState.TERMINAL
        await asyncio.sleep(0.02)
        await gate._evaluate_terminal_cooldown()
        assert gate.state == GateState.WAITING_DEPS

    @pytest.mark.asyncio
    async def test_same_state_noop(self):
        gate = _make_gate()
        assert gate.state == GateState.DISABLED
        result = await gate._try_transition(GateState.DISABLED, "test", "noop")
        assert result is False

    @pytest.mark.asyncio
    async def test_transition_emits_to_transitions_log(self):
        gate = _make_gate()
        await gate._try_transition(GateState.WAITING_DEPS, "test", "check_log")
        assert len(gate._transitions_log) > 0
        entry = gate._transitions_log[-1]
        assert entry["from"] == GateState.DISABLED.value
        assert entry["to"] == GateState.WAITING_DEPS.value
        assert entry["trigger"] == "test"


class TestGateFlapSuppression:
    """Flap suppression via min_state_dwell_s."""

    @pytest.mark.asyncio
    async def test_rapid_transitions_suppressed(self):
        gate = _make_gate(min_state_dwell_s=1.0)  # 1 second dwell
        # First transition succeeds
        result1 = await gate._try_transition(GateState.WAITING_DEPS, "test", "first")
        assert result1 is True
        # Immediate second transition suppressed by dwell
        result2 = await gate._try_transition(GateState.READY, "test", "too_fast")
        assert result2 is False
        assert gate.state == GateState.WAITING_DEPS


class TestGateSequence:
    """gate_sequence counter tests."""

    @pytest.mark.asyncio
    async def test_sequence_increments_on_transition(self):
        gate = _make_gate()
        seq0 = gate.gate_sequence
        await gate._try_transition(GateState.WAITING_DEPS, "test", "inc1")
        seq1 = gate.gate_sequence
        assert seq1 == seq0 + 1
        await asyncio.sleep(0.01)
        await gate._try_transition(GateState.READY, "test", "inc2")
        seq2 = gate.gate_sequence
        assert seq2 == seq1 + 1


class TestGateTelemetry:
    """Telemetry emission tests."""

    @pytest.mark.asyncio
    async def test_transition_emits_envelope(self):
        gate = _make_gate()
        with patch(
            "backend.core.telemetry_contract.get_telemetry_bus"
        ) as mock_get_bus:
            mock_bus = MagicMock()
            mock_get_bus.return_value = mock_bus
            await gate._try_transition(GateState.WAITING_DEPS, "test", "telemetry_test")
            mock_bus.emit.assert_called_once()
            envelope = mock_bus.emit.call_args[0][0]
            assert envelope.event_schema == "reasoning.activation@1.0.0"
            assert envelope.source == "ReasoningActivationGate"
            assert envelope.payload["from_state"] == GateState.DISABLED.value
            assert envelope.payload["to_state"] == GateState.WAITING_DEPS.value


class TestGateSingleton:
    """Singleton accessor tests."""

    def test_singleton(self):
        # Reset module-level singleton for test isolation
        import backend.core.reasoning_activation_gate as mod
        mod._gate_instance = None
        g1 = get_reasoning_activation_gate()
        g2 = get_reasoning_activation_gate()
        assert g1 is g2
        # Clean up
        mod._gate_instance = None


class TestGateDegradedConfig:
    """get_degraded_config() tests."""

    @pytest.mark.asyncio
    async def test_returns_overrides_when_degraded(self):
        gate = _make_gate()
        await gate._try_transition(GateState.DEGRADED, "test", "setup")
        cfg = gate.get_degraded_config()
        assert cfg == DEGRADED_OVERRIDES

    @pytest.mark.asyncio
    async def test_returns_empty_when_not_degraded(self):
        gate = _make_gate()
        await gate._try_transition(GateState.WAITING_DEPS, "test", "setup")
        cfg = gate.get_degraded_config()
        assert cfg == {}


# ---------------------------------------------------------------------------
# Task 3: Gate <-> Orchestrator integration tests
# ---------------------------------------------------------------------------


class TestGateOrchestratorIntegration:
    @pytest.mark.asyncio
    async def test_orchestrator_blocked_when_gate_inactive(self):
        """process() returns None when gate is not active."""
        from backend.core.reasoning_chain_orchestrator import (
            ReasoningChainOrchestrator, ChainConfig, ChainPhase,
        )
        config = ChainConfig(phase=ChainPhase.FULL_ENABLE, proactive_threshold=0.6, active=True)
        orch = ReasoningChainOrchestrator(config=config)
        orch._detector = AsyncMock()
        orch._detector.detect.return_value = MagicMock(
            is_proactive=True, confidence=0.95, signals_detected=[], reasoning="test",
        )

        mock_gate = MagicMock()
        mock_gate.is_active.return_value = False
        with patch("backend.core.reasoning_activation_gate.get_reasoning_activation_gate", return_value=mock_gate):
            result = await orch.process("start my day", context={}, trace_id="t1")
        assert result is None
        orch._detector.detect.assert_not_called()  # Gate blocked before detector

    @pytest.mark.asyncio
    async def test_orchestrator_proceeds_when_gate_active(self):
        """process() runs detector when gate is active."""
        from backend.core.reasoning_chain_orchestrator import (
            ReasoningChainOrchestrator, ChainConfig, ChainPhase,
        )
        config = ChainConfig(phase=ChainPhase.FULL_ENABLE, proactive_threshold=0.6, active=True)
        orch = ReasoningChainOrchestrator(config=config)
        orch._detector = AsyncMock()
        orch._detector.detect.return_value = MagicMock(
            is_proactive=False, confidence=0.1, signals_detected=[], reasoning="test",
        )

        mock_gate = MagicMock()
        mock_gate.is_active.return_value = True
        mock_gate.state = GateState.ACTIVE
        mock_gate.get_degraded_config.return_value = {}
        with patch("backend.core.reasoning_activation_gate.get_reasoning_activation_gate", return_value=mock_gate):
            result = await orch.process("what time is it", context={}, trace_id="t1")
        assert result is None  # Non-proactive, but detector was called
        orch._detector.detect.assert_called_once()
