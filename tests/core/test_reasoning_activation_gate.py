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
