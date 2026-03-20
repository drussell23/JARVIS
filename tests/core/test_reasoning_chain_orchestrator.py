"""Tests for ReasoningChainOrchestrator."""
import asyncio
import pytest
from typing import List
from unittest.mock import AsyncMock, MagicMock, patch
from backend.core.reasoning_chain_orchestrator import (
    ChainPhase,
    ChainConfig,
    ChainResult,
    ChainTelemetry,
    ShadowMetrics,
    ReasoningChainOrchestrator,
    get_reasoning_chain_orchestrator,
)
from backend.core.reasoning_activation_gate import GateState


# ---------------------------------------------------------------------------
# Auto-mock the activation gate as ACTIVE for all tests in this module so
# that existing orchestrator tests are not blocked by the gate (Task 3).
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _mock_gate_active():
    """Ensure the reasoning activation gate reports ACTIVE for every test."""
    mock_gate = MagicMock()
    mock_gate.is_active.return_value = True
    mock_gate.state = GateState.ACTIVE
    mock_gate.get_degraded_config.return_value = {}
    with patch(
        "backend.core.reasoning_activation_gate.get_reasoning_activation_gate",
        return_value=mock_gate,
    ):
        yield mock_gate


class TestChainPhase:
    def test_shadow_phase(self):
        assert ChainPhase.SHADOW.value == "shadow"

    def test_soft_enable_phase(self):
        assert ChainPhase.SOFT_ENABLE.value == "soft_enable"

    def test_full_enable_phase(self):
        assert ChainPhase.FULL_ENABLE.value == "full_enable"


class TestChainConfig:
    def test_default_config(self):
        config = ChainConfig()
        assert config.proactive_threshold == 0.6
        assert config.auto_expand_threshold == 0.85
        assert config.expansion_timeout == 2.0
        assert config.phase == ChainPhase.SHADOW

    def test_from_env_shadow(self):
        env = {
            "JARVIS_REASONING_CHAIN_SHADOW": "true",
            "JARVIS_REASONING_CHAIN_ENABLED": "false",
        }
        with patch.dict("os.environ", env, clear=False):
            config = ChainConfig.from_env()
        assert config.phase == ChainPhase.SHADOW

    def test_from_env_soft_enable(self):
        env = {
            "JARVIS_REASONING_CHAIN_ENABLED": "true",
            "JARVIS_REASONING_CHAIN_AUTO_EXPAND": "false",
        }
        with patch.dict("os.environ", env, clear=False):
            config = ChainConfig.from_env()
        assert config.phase == ChainPhase.SOFT_ENABLE

    def test_from_env_full_enable(self):
        env = {
            "JARVIS_REASONING_CHAIN_ENABLED": "true",
            "JARVIS_REASONING_CHAIN_AUTO_EXPAND": "true",
        }
        with patch.dict("os.environ", env, clear=False):
            config = ChainConfig.from_env()
        assert config.phase == ChainPhase.FULL_ENABLE

    def test_from_env_custom_thresholds(self):
        env = {
            "CHAIN_PROACTIVE_THRESHOLD": "0.7",
            "CHAIN_AUTO_EXPAND_THRESHOLD": "0.9",
            "CHAIN_EXPANSION_TIMEOUT": "3.0",
        }
        with patch.dict("os.environ", env, clear=False):
            config = ChainConfig.from_env()
        assert config.proactive_threshold == 0.7
        assert config.auto_expand_threshold == 0.9
        assert config.expansion_timeout == 3.0

    def test_is_active_when_shadow(self):
        env = {"JARVIS_REASONING_CHAIN_SHADOW": "true", "JARVIS_REASONING_CHAIN_ENABLED": "false"}
        with patch.dict("os.environ", env, clear=False):
            config = ChainConfig.from_env()
        assert config.is_active() is True

    def test_is_active_when_disabled(self):
        env = {"JARVIS_REASONING_CHAIN_SHADOW": "false", "JARVIS_REASONING_CHAIN_ENABLED": "false"}
        with patch.dict("os.environ", env, clear=False):
            config = ChainConfig.from_env()
        assert config.is_active() is False


class TestChainResult:
    def test_single_intent_result(self):
        result = ChainResult(
            handled=True,
            phase=ChainPhase.FULL_ENABLE,
            trace_id="trace-123",
            original_command="start my day",
            expanded_intents=["check email", "check calendar"],
            mind_results=[{"success": True}, {"success": True}],
            audit_trail={},
        )
        assert result.handled is True
        assert result.success_rate == 1.0
        assert len(result.expanded_intents) == 2

    def test_not_handled_result(self):
        result = ChainResult.not_handled(trace_id="t1")
        assert result.handled is False
        assert result.expanded_intents == []

    def test_partial_success_rate(self):
        result = ChainResult(
            handled=True, phase=ChainPhase.FULL_ENABLE, trace_id="t1",
            original_command="test",
            mind_results=[{"success": True}, {"success": False}],
        )
        assert result.success_rate == 0.5

    def test_empty_mind_results_success_rate(self):
        result = ChainResult.not_handled(trace_id="t1")
        assert result.success_rate == 0.0


class TestShadowMetrics:
    def test_record_detection(self):
        m = ShadowMetrics()
        m.record_detection(would_expand=True, actually_expanded=False)
        assert m.total_detections == 1
        assert m.would_expand_count == 1
        assert m.actually_expanded_count == 0

    def test_divergence_rate(self):
        m = ShadowMetrics()
        m.record_detection(would_expand=True, actually_expanded=False)
        m.record_detection(would_expand=False, actually_expanded=False)
        assert m.divergence_rate == 0.5

    def test_empty_divergence_rate(self):
        m = ShadowMetrics()
        assert m.divergence_rate == 0.0

    def test_mind_quality_no_regression(self):
        m = ShadowMetrics()
        for _ in range(20):
            m.record_mind_quality(expanded_score=0.9, single_score=0.8)
        assert m.mind_quality_regressed is False

    def test_mind_quality_regression_detected(self):
        m = ShadowMetrics()
        for _ in range(20):
            m.record_mind_quality(expanded_score=0.5, single_score=0.8)
        assert m.mind_quality_regressed is True

    def test_mind_quality_insufficient_data(self):
        m = ShadowMetrics()
        m.record_mind_quality(expanded_score=0.1, single_score=0.9)
        assert m.mind_quality_regressed is False

    def test_go_no_go_includes_mind_quality(self):
        m = ShadowMetrics()
        status = m.go_no_go_status()
        assert "mind_plan_quality" in status

    def test_latency_p95(self):
        m = ShadowMetrics()
        for i in range(100):
            m.record_latency(float(i))
        assert m.latency_p95_ms >= 94.0

    def test_go_no_go_all_gates(self):
        m = ShadowMetrics()
        status = m.go_no_go_status()
        assert "expansion_accuracy" in status
        assert "false_positive_rate" in status
        assert "latency_p95_ms" in status
        assert "mind_plan_quality" in status
        assert "user_override_rate" in status
        assert "all_gates_pass" in status


class TestChainTelemetry:
    @pytest.mark.asyncio
    async def test_emit_proactive_detection(self):
        telemetry = ChainTelemetry()
        event = await telemetry.emit_proactive_detection(
            trace_id="t1", command="start my day", is_proactive=True,
            confidence=0.92, signals=["workflow_trigger", "multi_task"], latency_ms=15.0,
        )
        assert event["event"] == "proactive_detection"
        assert event["trace_id"] == "t1"
        assert event["is_proactive"] is True
        assert event["confidence"] == 0.92

    @pytest.mark.asyncio
    async def test_emit_intent_expansion(self):
        telemetry = ChainTelemetry()
        event = await telemetry.emit_intent_expansion(
            trace_id="t1", original_query="start my day", expanded_count=3,
            intents=["check email", "check calendar", "open Slack"],
            confidence=0.88, latency_ms=120.0,
        )
        assert event["event"] == "intent_expansion"
        assert event["expanded_count"] == 3

    @pytest.mark.asyncio
    async def test_emit_shadow_divergence(self):
        telemetry = ChainTelemetry()
        event = await telemetry.emit_shadow_divergence(
            trace_id="t1", would_expand=True, actually_expanded=False, match=False,
        )
        assert event["event"] == "expansion_shadow_divergence"
        assert event["would_expand"] is True
        assert event["match"] is False

    @pytest.mark.asyncio
    async def test_emit_coordinator_delegation(self):
        telemetry = ChainTelemetry()
        event = await telemetry.emit_coordinator_delegation(
            trace_id="t1", plan_id="p1", step_id="s1",
            agent_name="GoogleWorkspaceAgent", capability="email_management",
            latency_ms=50.0,
        )
        assert event["event"] == "coordinator_delegation"
        assert event["agent_name"] == "GoogleWorkspaceAgent"

    @pytest.mark.asyncio
    async def test_emit_chain_complete(self):
        telemetry = ChainTelemetry()
        event = await telemetry.emit_chain_complete(
            trace_id="t1", total_intents=3, total_steps=5,
            total_ms=2500.0, success_rate=1.0,
        )
        assert event["event"] == "chain_complete"
        assert event["total_intents"] == 3
        assert event["success_rate"] == 1.0

    @pytest.mark.asyncio
    async def test_reactor_forwarding_best_effort(self):
        """Telemetry forwarding to Reactor is fire-and-forget; failures don't propagate."""
        telemetry = ChainTelemetry()
        event = await telemetry.emit_proactive_detection(
            trace_id="t1", command="test", is_proactive=False,
            confidence=0.1, signals=[], latency_ms=5.0,
        )
        assert event is not None

    @pytest.mark.asyncio
    async def test_all_events_have_timestamp(self):
        """Every event should include a timestamp."""
        telemetry = ChainTelemetry()
        event = await telemetry.emit_proactive_detection(
            trace_id="t1", command="test", is_proactive=False,
            confidence=0.1, signals=[], latency_ms=5.0,
        )
        assert "timestamp" in event
        assert isinstance(event["timestamp"], float)


# ---------------------------------------------------------------------------
# Orchestrator test helpers
# ---------------------------------------------------------------------------


def _mock_detection_result(is_proactive: bool, confidence: float = 0.9):
    """Create a mock ProactiveDetectionResult."""
    return MagicMock(
        is_proactive=is_proactive,
        confidence=confidence,
        signals_detected=["workflow_trigger"] if is_proactive else [],
        suggested_intent="work_mode" if is_proactive else None,
        reasoning="test",
        should_use_expand_and_execute=is_proactive,
    )


def _mock_prediction_result(intents: List[str] = None):
    """Create a mock PredictionResult."""
    intents = intents or ["check email", "check calendar", "open Slack"]
    tasks = []
    for i, intent in enumerate(intents):
        task = MagicMock()
        task.goal = intent
        task.priority = i + 1
        task.target_app = None
        task.category = MagicMock(name="WORK_MODE")
        tasks.append(task)
    result = MagicMock()
    result.original_query = "start my day"
    result.confidence = 0.88
    result.expanded_tasks = tasks
    result.reasoning = "Morning workflow detected"
    return result


# ---------------------------------------------------------------------------
# Orchestrator tests
# ---------------------------------------------------------------------------


class TestOrchestratorShadowPhase:
    @pytest.mark.asyncio
    async def test_shadow_returns_none(self):
        """Shadow mode: detect + expand in background, return None."""
        config = ChainConfig(phase=ChainPhase.SHADOW, proactive_threshold=0.6, active=True)
        orch = ReasoningChainOrchestrator(config=config)
        mock_detector = AsyncMock()
        mock_detector.detect.return_value = _mock_detection_result(True, 0.92)
        orch._detector = mock_detector
        mock_planner = AsyncMock()
        mock_planner.expand_intent.return_value = _mock_prediction_result()
        orch._planner = mock_planner
        result = await orch.process("start my day", context={}, trace_id="t1")
        assert result is None
        mock_detector.detect.assert_called_once_with("start my day")

    @pytest.mark.asyncio
    async def test_shadow_logs_divergence(self):
        """Shadow mode records divergence metrics."""
        config = ChainConfig(phase=ChainPhase.SHADOW, proactive_threshold=0.6, active=True)
        orch = ReasoningChainOrchestrator(config=config)
        mock_detector = AsyncMock()
        mock_detector.detect.return_value = _mock_detection_result(True, 0.92)
        orch._detector = mock_detector
        mock_planner = AsyncMock()
        mock_planner.expand_intent.return_value = _mock_prediction_result()
        orch._planner = mock_planner
        await orch.process("start my day", context={}, trace_id="t1")
        assert orch._shadow_metrics.total_detections == 1
        assert orch._shadow_metrics.would_expand_count == 1


class TestOrchestratorNotProactive:
    @pytest.mark.asyncio
    async def test_non_proactive_returns_none(self):
        config = ChainConfig(phase=ChainPhase.FULL_ENABLE, proactive_threshold=0.6, active=True)
        orch = ReasoningChainOrchestrator(config=config)
        mock_detector = AsyncMock()
        mock_detector.detect.return_value = _mock_detection_result(False, 0.2)
        orch._detector = mock_detector
        result = await orch.process("what time is it", context={}, trace_id="t1")
        assert result is None

    @pytest.mark.asyncio
    async def test_below_threshold_returns_none(self):
        config = ChainConfig(phase=ChainPhase.FULL_ENABLE, proactive_threshold=0.8, active=True)
        orch = ReasoningChainOrchestrator(config=config)
        mock_detector = AsyncMock()
        mock_detector.detect.return_value = _mock_detection_result(True, 0.7)
        orch._detector = mock_detector
        result = await orch.process("maybe start work", context={}, trace_id="t1")
        assert result is None


class TestOrchestratorSoftEnable:
    @pytest.mark.asyncio
    async def test_soft_enable_returns_confirmation(self):
        config = ChainConfig(phase=ChainPhase.SOFT_ENABLE, proactive_threshold=0.6, active=True)
        orch = ReasoningChainOrchestrator(config=config)
        mock_detector = AsyncMock()
        mock_detector.detect.return_value = _mock_detection_result(True, 0.92)
        orch._detector = mock_detector
        mock_planner = AsyncMock()
        mock_planner.expand_intent.return_value = _mock_prediction_result()
        orch._planner = mock_planner
        result = await orch.process("start my day", context={}, trace_id="t1")
        assert result is not None
        assert result.handled is True
        assert result.needs_confirmation is True
        assert len(result.expanded_intents) == 3
        assert "check email" in result.expanded_intents


class TestOrchestratorFullEnable:
    @pytest.mark.asyncio
    async def test_full_enable_expands_and_executes(self):
        config = ChainConfig(phase=ChainPhase.FULL_ENABLE, proactive_threshold=0.6, auto_expand_threshold=0.85, active=True)
        orch = ReasoningChainOrchestrator(config=config)
        mock_detector = AsyncMock()
        mock_detector.detect.return_value = _mock_detection_result(True, 0.92)
        orch._detector = mock_detector
        mock_planner = AsyncMock()
        mock_planner.expand_intent.return_value = _mock_prediction_result()
        orch._planner = mock_planner
        mock_mind = AsyncMock()
        mock_mind.send_command.return_value = {
            "status": "plan_ready",
            "plan": {"sub_goals": [{"goal": "done"}], "plan_id": "p1"},
            "classification": {},
        }
        orch._mind_client = mock_mind
        mock_coordinator = AsyncMock()
        mock_coordinator.execute_task.return_value = {"status": "delegated", "task_id": "t1", "delegated_to": "agent1"}
        orch._coordinator = mock_coordinator
        result = await orch.process("start my day", context={}, trace_id="t1")
        assert result is not None
        assert result.handled is True
        assert result.needs_confirmation is False
        assert mock_mind.send_command.call_count == 3
        assert len(result.mind_results) == 3

    @pytest.mark.asyncio
    async def test_full_enable_below_auto_threshold_asks_confirmation(self):
        config = ChainConfig(phase=ChainPhase.FULL_ENABLE, proactive_threshold=0.6, auto_expand_threshold=0.95, active=True)
        orch = ReasoningChainOrchestrator(config=config)
        mock_detector = AsyncMock()
        mock_detector.detect.return_value = _mock_detection_result(True, 0.88)
        orch._detector = mock_detector
        mock_planner = AsyncMock()
        mock_planner.expand_intent.return_value = _mock_prediction_result()
        orch._planner = mock_planner
        result = await orch.process("start my day", context={}, trace_id="t1")
        assert result.needs_confirmation is True


class TestOrchestratorErrorHandling:
    @pytest.mark.asyncio
    async def test_detector_failure_returns_none(self):
        config = ChainConfig(phase=ChainPhase.FULL_ENABLE, proactive_threshold=0.6, active=True)
        orch = ReasoningChainOrchestrator(config=config)
        mock_detector = AsyncMock()
        mock_detector.detect.side_effect = Exception("detector exploded")
        orch._detector = mock_detector
        result = await orch.process("start my day", context={}, trace_id="t1")
        assert result is None

    @pytest.mark.asyncio
    async def test_planner_failure_returns_none(self):
        config = ChainConfig(phase=ChainPhase.FULL_ENABLE, proactive_threshold=0.6, active=True)
        orch = ReasoningChainOrchestrator(config=config)
        mock_detector = AsyncMock()
        mock_detector.detect.return_value = _mock_detection_result(True, 0.92)
        orch._detector = mock_detector
        mock_planner = AsyncMock()
        mock_planner.expand_intent.side_effect = Exception("planner exploded")
        orch._planner = mock_planner
        result = await orch.process("start my day", context={}, trace_id="t1")
        assert result is None

    @pytest.mark.asyncio
    async def test_mind_failure_for_one_intent_continues_others(self):
        config = ChainConfig(phase=ChainPhase.FULL_ENABLE, proactive_threshold=0.6, active=True)
        orch = ReasoningChainOrchestrator(config=config)
        mock_detector = AsyncMock()
        mock_detector.detect.return_value = _mock_detection_result(True, 0.92)
        orch._detector = mock_detector
        mock_planner = AsyncMock()
        mock_planner.expand_intent.return_value = _mock_prediction_result(["a", "b"])
        orch._planner = mock_planner
        call_count = 0

        async def mind_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return None
            return {"status": "plan_ready", "plan": {"sub_goals": [], "plan_id": "p1"}, "classification": {}}

        mock_mind = AsyncMock()
        mock_mind.send_command.side_effect = mind_side_effect
        orch._mind_client = mock_mind
        result = await orch.process("start my day", context={}, trace_id="t1")
        assert result is not None
        assert result.handled is True

    @pytest.mark.asyncio
    async def test_timeout_returns_none(self):
        config = ChainConfig(phase=ChainPhase.FULL_ENABLE, proactive_threshold=0.6, expansion_timeout=0.01, active=True)
        orch = ReasoningChainOrchestrator(config=config)
        mock_detector = AsyncMock()

        async def slow_detect(cmd):
            await asyncio.sleep(0.1)
            return _mock_detection_result(True, 0.92)

        mock_detector.detect.side_effect = slow_detect
        orch._detector = mock_detector
        result = await orch.process("start my day", context={}, trace_id="t1")
        assert result is None


class TestOrchestratorSingleton:
    def test_singleton(self):
        # Reset singleton for test isolation
        import backend.core.reasoning_chain_orchestrator as mod
        mod._orchestrator_instance = None
        orch1 = get_reasoning_chain_orchestrator()
        orch2 = get_reasoning_chain_orchestrator()
        assert orch1 is orch2
        mod._orchestrator_instance = None  # Clean up


# ---------------------------------------------------------------------------
# End-to-end chain tests
# ---------------------------------------------------------------------------


class TestEndToEndChain:
    """Full pipeline: detect -> expand -> mind -> coordinate -> result."""

    @pytest.mark.asyncio
    async def test_full_pipeline_start_my_day(self):
        """Simulate 'start my day' through the full chain."""
        config = ChainConfig(
            phase=ChainPhase.FULL_ENABLE,
            proactive_threshold=0.5,
            auto_expand_threshold=0.8,
            active=True,
        )
        orch = ReasoningChainOrchestrator(config=config)

        mock_detector = AsyncMock()
        mock_detector.detect.return_value = _mock_detection_result(True, 0.95)
        orch._detector = mock_detector

        mock_planner = AsyncMock()
        mock_planner.expand_intent.return_value = _mock_prediction_result(
            ["check email", "check calendar", "open Slack"]
        )
        orch._planner = mock_planner

        mock_mind = AsyncMock()
        mock_mind.send_command.return_value = {
            "status": "plan_ready",
            "success": True,
            "plan": {
                "sub_goals": [{"goal": "done", "tool_required": "handle_workspace_query"}],
                "plan_id": "p-test",
            },
            "classification": {"brain_used": "qwen-2.5-7b"},
        }
        orch._mind_client = mock_mind

        mock_coord = AsyncMock()
        mock_coord.execute_task.return_value = {
            "status": "delegated",
            "task_id": "t-test",
            "delegated_to": "GoogleWorkspaceAgent",
        }
        orch._coordinator = mock_coord

        result = await orch.process("start my day", context={}, trace_id="e2e-test")

        assert result is not None
        assert result.handled is True
        assert result.needs_confirmation is False
        assert len(result.expanded_intents) == 3
        assert len(result.mind_results) == 3
        assert result.success_rate > 0
        assert result.total_ms > 0
        assert mock_detector.detect.call_count == 1
        assert mock_planner.expand_intent.call_count == 1
        assert mock_mind.send_command.call_count == 3
        assert mock_coord.execute_task.call_count == 3

        # Verify trace_id propagated to Mind
        for call in mock_mind.send_command.call_args_list:
            ctx = call.kwargs.get("context", {})
            assert ctx.get("trace_id") == "e2e-test"
            assert ctx.get("expanded_from_chain") is True

        # Verify audit trail
        assert "detection" in result.audit_trail
        assert result.audit_trail["detection"]["confidence"] == 0.95
        assert result.audit_trail["expansion"]["intent_count"] == 3

    @pytest.mark.asyncio
    async def test_non_proactive_command_passthrough(self):
        """Simple command should not be intercepted."""
        config = ChainConfig(
            phase=ChainPhase.FULL_ENABLE, proactive_threshold=0.6, active=True,
        )
        orch = ReasoningChainOrchestrator(config=config)
        mock_detector = AsyncMock()
        mock_detector.detect.return_value = _mock_detection_result(False, 0.1)
        orch._detector = mock_detector
        result = await orch.process("what time is it", context={}, trace_id="simple")
        assert result is None

    @pytest.mark.asyncio
    async def test_go_no_go_metrics_accumulate(self):
        """Shadow metrics accumulate for go/no-go evaluation."""
        config = ChainConfig(phase=ChainPhase.SHADOW, proactive_threshold=0.5, active=True)
        orch = ReasoningChainOrchestrator(config=config)
        mock_detector = AsyncMock()
        mock_detector.detect.return_value = _mock_detection_result(True, 0.9)
        orch._detector = mock_detector
        mock_planner = AsyncMock()
        mock_planner.expand_intent.return_value = _mock_prediction_result()
        orch._planner = mock_planner

        for i in range(5):
            await orch.process(f"command {i}", context={}, trace_id=f"shadow-{i}")

        assert orch._shadow_metrics.total_detections == 5
        assert orch._shadow_metrics.would_expand_count == 5
        assert orch._shadow_metrics.actually_expanded_count == 0
        assert len(orch._shadow_metrics.latency_samples_ms) == 5

        status = orch._shadow_metrics.go_no_go_status()
        assert "expansion_accuracy" in status
        assert "false_positive_rate" in status
        assert "latency_p95_ms" in status
        assert "mind_plan_quality" in status
