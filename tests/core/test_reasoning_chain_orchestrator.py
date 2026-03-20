"""Tests for ReasoningChainOrchestrator."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from backend.core.reasoning_chain_orchestrator import (
    ChainPhase,
    ChainConfig,
    ChainResult,
    ShadowMetrics,
)


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
