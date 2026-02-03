"""
Tests for StartupTimeoutCalculator and PhaseBudgets.

These tests verify:
1. PhaseBudgets dataclass with env-overridable values
2. StartupMetricsHistory protocol for adaptive timeouts
3. StartupTimeoutCalculator phase budget calculations
4. Trinity budget calculations with/without GCP
5. Global timeout computation with phase exclusions
6. History-based adaptive effective() calculations

Following TDD - tests written first, then implementation.
"""

import os
from typing import Optional
from unittest.mock import patch

import pytest


# =============================================================================
# Test Fixtures
# =============================================================================


@pytest.fixture
def clean_env():
    """
    Fixture to ensure clean environment for each test.

    Removes all JARVIS_ phase budget env vars.
    """
    # Store original values
    original_env = {}
    phase_vars = [
        "JARVIS_PRE_TRINITY_BUDGET",
        "JARVIS_TRINITY_PHASE_BUDGET",
        "JARVIS_GCP_WAIT_BUFFER",
        "JARVIS_POST_TRINITY_BUDGET",
        "JARVIS_DISCOVERY_BUDGET",
        "JARVIS_HEALTH_CHECK_BUDGET",
        "JARVIS_CLEANUP_BUDGET",
        "JARVIS_SAFETY_MARGIN",
        "JARVIS_STARTUP_HARD_CAP",
    ]
    for key in phase_vars:
        if key in os.environ:
            original_env[key] = os.environ.pop(key)

    yield

    # Restore original env vars
    for key, value in original_env.items():
        os.environ[key] = value

    # Remove any that were set during test
    for key in phase_vars:
        if key in os.environ and key not in original_env:
            del os.environ[key]


class MockMetricsHistory:
    """Mock implementation of StartupMetricsHistory for testing."""

    def __init__(self, data: Optional[dict[str, float]] = None):
        self._data = data or {}

    def has(self, phase: str) -> bool:
        """Return whether history exists for phase."""
        return phase in self._data

    def get_p95(self, phase: str) -> Optional[float]:
        """Return p95 timing for phase."""
        return self._data.get(phase)


# =============================================================================
# Test: PhaseBudgets Default Values
# =============================================================================


class TestPhaseBudgetsDefaults:
    """Tests for PhaseBudgets default values."""

    def test_pre_trinity_default(self, clean_env) -> None:
        """Test PRE_TRINITY default is 30."""
        from backend.config.startup_timeouts import PhaseBudgets

        budgets = PhaseBudgets()
        assert budgets.PRE_TRINITY == 30.0

    def test_trinity_phase_default(self, clean_env) -> None:
        """Test TRINITY_PHASE default is 300."""
        from backend.config.startup_timeouts import PhaseBudgets

        budgets = PhaseBudgets()
        assert budgets.TRINITY_PHASE == 300.0

    def test_gcp_wait_buffer_default(self, clean_env) -> None:
        """Test GCP_WAIT_BUFFER default is 120."""
        from backend.config.startup_timeouts import PhaseBudgets

        budgets = PhaseBudgets()
        assert budgets.GCP_WAIT_BUFFER == 120.0

    def test_post_trinity_default(self, clean_env) -> None:
        """Test POST_TRINITY default is 60."""
        from backend.config.startup_timeouts import PhaseBudgets

        budgets = PhaseBudgets()
        assert budgets.POST_TRINITY == 60.0

    def test_discovery_default(self, clean_env) -> None:
        """Test DISCOVERY default is 45."""
        from backend.config.startup_timeouts import PhaseBudgets

        budgets = PhaseBudgets()
        assert budgets.DISCOVERY == 45.0

    def test_health_check_default(self, clean_env) -> None:
        """Test HEALTH_CHECK default is 30."""
        from backend.config.startup_timeouts import PhaseBudgets

        budgets = PhaseBudgets()
        assert budgets.HEALTH_CHECK == 30.0

    def test_cleanup_default(self, clean_env) -> None:
        """Test CLEANUP default is 30."""
        from backend.config.startup_timeouts import PhaseBudgets

        budgets = PhaseBudgets()
        assert budgets.CLEANUP == 30.0

    def test_safety_margin_default(self, clean_env) -> None:
        """Test SAFETY_MARGIN default is 30."""
        from backend.config.startup_timeouts import PhaseBudgets

        budgets = PhaseBudgets()
        assert budgets.SAFETY_MARGIN == 30.0

    def test_hard_cap_default(self, clean_env) -> None:
        """Test HARD_CAP default is 900."""
        from backend.config.startup_timeouts import PhaseBudgets

        budgets = PhaseBudgets()
        assert budgets.HARD_CAP == 900.0


# =============================================================================
# Test: PhaseBudgets Environment Overrides
# =============================================================================


class TestPhaseBudgetsEnvOverrides:
    """Tests for PhaseBudgets environment variable overrides."""

    def test_phase_budgets_env_override(self, clean_env) -> None:
        """Verify JARVIS_TRINITY_PHASE_BUDGET env var works."""
        with patch.dict(os.environ, {"JARVIS_TRINITY_PHASE_BUDGET": "500.0"}):
            from backend.config.startup_timeouts import PhaseBudgets

            budgets = PhaseBudgets()
            assert budgets.TRINITY_PHASE == 500.0

    def test_pre_trinity_env_override(self, clean_env) -> None:
        """Test JARVIS_PRE_TRINITY_BUDGET env override."""
        with patch.dict(os.environ, {"JARVIS_PRE_TRINITY_BUDGET": "45.0"}):
            from backend.config.startup_timeouts import PhaseBudgets

            budgets = PhaseBudgets()
            assert budgets.PRE_TRINITY == 45.0

    def test_gcp_wait_buffer_env_override(self, clean_env) -> None:
        """Test JARVIS_GCP_WAIT_BUFFER env override."""
        with patch.dict(os.environ, {"JARVIS_GCP_WAIT_BUFFER": "180.0"}):
            from backend.config.startup_timeouts import PhaseBudgets

            budgets = PhaseBudgets()
            assert budgets.GCP_WAIT_BUFFER == 180.0

    def test_safety_margin_env_override(self, clean_env) -> None:
        """Test JARVIS_SAFETY_MARGIN env override."""
        with patch.dict(os.environ, {"JARVIS_SAFETY_MARGIN": "60.0"}):
            from backend.config.startup_timeouts import PhaseBudgets

            budgets = PhaseBudgets()
            assert budgets.SAFETY_MARGIN == 60.0

    def test_hard_cap_env_override(self, clean_env) -> None:
        """Test JARVIS_STARTUP_HARD_CAP env override."""
        with patch.dict(os.environ, {"JARVIS_STARTUP_HARD_CAP": "1200.0"}):
            from backend.config.startup_timeouts import PhaseBudgets

            budgets = PhaseBudgets()
            assert budgets.HARD_CAP == 1200.0


# =============================================================================
# Test: StartupTimeoutCalculator - Trinity Budget
# =============================================================================


class TestTrinityBudget:
    """Tests for StartupTimeoutCalculator.trinity_budget property."""

    def test_trinity_budget_with_gcp(self, clean_env) -> None:
        """Test trinity_enabled=True, gcp_enabled=True returns 420.0 (300+120)."""
        from backend.config.startup_timeouts import StartupTimeoutCalculator

        calculator = StartupTimeoutCalculator(trinity_enabled=True, gcp_enabled=True)
        assert calculator.trinity_budget == 420.0

    def test_trinity_budget_without_gcp(self, clean_env) -> None:
        """Test trinity_enabled=True, gcp_enabled=False returns 300.0."""
        from backend.config.startup_timeouts import StartupTimeoutCalculator

        calculator = StartupTimeoutCalculator(trinity_enabled=True, gcp_enabled=False)
        assert calculator.trinity_budget == 300.0

    def test_trinity_budget_disabled(self, clean_env) -> None:
        """Test trinity_enabled=False returns 0.0."""
        from backend.config.startup_timeouts import StartupTimeoutCalculator

        calculator = StartupTimeoutCalculator(trinity_enabled=False, gcp_enabled=False)
        assert calculator.trinity_budget == 0.0

    def test_trinity_budget_disabled_ignores_gcp(self, clean_env) -> None:
        """Test trinity_enabled=False returns 0.0 even with gcp_enabled=True."""
        from backend.config.startup_timeouts import StartupTimeoutCalculator

        calculator = StartupTimeoutCalculator(trinity_enabled=False, gcp_enabled=True)
        assert calculator.trinity_budget == 0.0


# =============================================================================
# Test: StartupTimeoutCalculator - Global Timeout
# =============================================================================


class TestGlobalTimeout:
    """Tests for StartupTimeoutCalculator.global_timeout property."""

    def test_global_timeout_excludes_disabled_phases(self, clean_env) -> None:
        """Test trinity_enabled=False, gcp_enabled=False returns 225.0.

        Expected: PRE_TRINITY(30) + POST_TRINITY(60) + DISCOVERY(45)
                 + HEALTH_CHECK(30) + CLEANUP(30) + SAFETY_MARGIN(30) = 225.0

        TRINITY_PHASE and GCP_WAIT_BUFFER are excluded when trinity_enabled=False.
        """
        from backend.config.startup_timeouts import StartupTimeoutCalculator

        calculator = StartupTimeoutCalculator(trinity_enabled=False, gcp_enabled=False)
        assert calculator.global_timeout == 225.0

    def test_global_timeout_with_trinity_no_gcp(self, clean_env) -> None:
        """Test global_timeout includes TRINITY_PHASE but not GCP_WAIT_BUFFER.

        Expected: PRE_TRINITY(30) + TRINITY_PHASE(300) + POST_TRINITY(60)
                 + DISCOVERY(45) + HEALTH_CHECK(30) + CLEANUP(30) + SAFETY_MARGIN(30) = 525.0
        """
        from backend.config.startup_timeouts import StartupTimeoutCalculator

        calculator = StartupTimeoutCalculator(trinity_enabled=True, gcp_enabled=False)
        assert calculator.global_timeout == 525.0

    def test_global_timeout_with_trinity_and_gcp(self, clean_env) -> None:
        """Test global_timeout includes both TRINITY_PHASE and GCP_WAIT_BUFFER.

        Expected: PRE_TRINITY(30) + TRINITY_PHASE(300) + GCP_WAIT_BUFFER(120)
                 + POST_TRINITY(60) + DISCOVERY(45) + HEALTH_CHECK(30)
                 + CLEANUP(30) + SAFETY_MARGIN(30) = 645.0
        """
        from backend.config.startup_timeouts import StartupTimeoutCalculator

        calculator = StartupTimeoutCalculator(trinity_enabled=True, gcp_enabled=True)
        assert calculator.global_timeout == 645.0


# =============================================================================
# Test: StartupTimeoutCalculator - Effective Method
# =============================================================================


class TestEffectiveMethod:
    """Tests for StartupTimeoutCalculator.effective() method."""

    def test_effective_fallback_when_no_history(self, clean_env) -> None:
        """Test effective returns base value when no history provided."""
        from backend.config.startup_timeouts import StartupTimeoutCalculator

        calculator = StartupTimeoutCalculator(trinity_enabled=True)
        # No history provided, should return base value
        assert calculator.effective("TRINITY_PHASE") == 300.0
        assert calculator.effective("PRE_TRINITY") == 30.0

    def test_effective_uses_history_p95(self, clean_env) -> None:
        """Test effective uses history p95 * 1.2 when available.

        history with p95=400 -> effective = 480 (400*1.2)
        """
        from backend.config.startup_timeouts import StartupTimeoutCalculator

        history = MockMetricsHistory({"TRINITY_PHASE": 400.0})
        calculator = StartupTimeoutCalculator(trinity_enabled=True, history=history)
        # 400 * 1.2 = 480, which is > base (300) so use 480
        assert calculator.effective("TRINITY_PHASE") == 480.0

    def test_effective_uses_max_of_base_and_history(self, clean_env) -> None:
        """Test effective uses max(base, p95*1.2) when p95*1.2 < base."""
        from backend.config.startup_timeouts import StartupTimeoutCalculator

        # p95=200 -> 200*1.2=240, but base is 300, so should return 300
        history = MockMetricsHistory({"TRINITY_PHASE": 200.0})
        calculator = StartupTimeoutCalculator(trinity_enabled=True, history=history)
        assert calculator.effective("TRINITY_PHASE") == 300.0

    def test_effective_respects_hard_cap(self, clean_env) -> None:
        """Test effective respects HARD_CAP when history is very high.

        history with p95=800 -> 800*1.2=960, but capped to 900
        """
        from backend.config.startup_timeouts import StartupTimeoutCalculator

        history = MockMetricsHistory({"TRINITY_PHASE": 800.0})
        calculator = StartupTimeoutCalculator(trinity_enabled=True, history=history)
        # 800 * 1.2 = 960, capped to HARD_CAP (900)
        assert calculator.effective("TRINITY_PHASE") == 900.0

    def test_effective_no_history_for_phase(self, clean_env) -> None:
        """Test effective returns base when history exists but not for this phase."""
        from backend.config.startup_timeouts import StartupTimeoutCalculator

        # History has data for TRINITY_PHASE but not PRE_TRINITY
        history = MockMetricsHistory({"TRINITY_PHASE": 400.0})
        calculator = StartupTimeoutCalculator(trinity_enabled=True, history=history)
        assert calculator.effective("PRE_TRINITY") == 30.0


# =============================================================================
# Test: StartupTimeoutCalculator - Integration
# =============================================================================


class TestCalculatorIntegration:
    """Integration tests for StartupTimeoutCalculator."""

    def test_trinity_budget_uses_effective(self, clean_env) -> None:
        """Test trinity_budget uses effective() which considers history."""
        from backend.config.startup_timeouts import StartupTimeoutCalculator

        # History shows TRINITY_PHASE took 400s on average
        history = MockMetricsHistory({"TRINITY_PHASE": 400.0})
        calculator = StartupTimeoutCalculator(
            trinity_enabled=True, gcp_enabled=True, history=history
        )
        # effective("TRINITY_PHASE") = max(300, 400*1.2) = 480
        # effective("GCP_WAIT_BUFFER") = 120 (no history)
        # trinity_budget = 480 + 120 = 600
        assert calculator.trinity_budget == 600.0

    def test_global_timeout_uses_effective_for_all_phases(self, clean_env) -> None:
        """Test global_timeout uses effective() for each included phase."""
        from backend.config.startup_timeouts import StartupTimeoutCalculator

        history = MockMetricsHistory({
            "PRE_TRINITY": 50.0,  # 50*1.2=60 > 30, use 60
            "TRINITY_PHASE": 400.0,  # 400*1.2=480 > 300, use 480
        })
        calculator = StartupTimeoutCalculator(
            trinity_enabled=True, gcp_enabled=False, history=history
        )
        # PRE_TRINITY: 60 (from history)
        # TRINITY_PHASE: 480 (from history)
        # POST_TRINITY: 60 (no history, base)
        # DISCOVERY: 45 (no history, base)
        # HEALTH_CHECK: 30 (no history, base)
        # CLEANUP: 30 (no history, base)
        # SAFETY_MARGIN: 30
        # Total: 60 + 480 + 60 + 45 + 30 + 30 + 30 = 735
        assert calculator.global_timeout == 735.0


# =============================================================================
# Test: StartupMetricsHistory Protocol
# =============================================================================


class TestStartupMetricsHistoryProtocol:
    """Tests for StartupMetricsHistory protocol compliance."""

    def test_mock_history_has_method(self) -> None:
        """Test MockMetricsHistory implements has() correctly."""
        history = MockMetricsHistory({"TRINITY_PHASE": 400.0})
        assert history.has("TRINITY_PHASE") is True
        assert history.has("PRE_TRINITY") is False

    def test_mock_history_get_p95_method(self) -> None:
        """Test MockMetricsHistory implements get_p95() correctly."""
        history = MockMetricsHistory({"TRINITY_PHASE": 400.0})
        assert history.get_p95("TRINITY_PHASE") == 400.0
        assert history.get_p95("PRE_TRINITY") is None


# =============================================================================
# Test: Edge Cases
# =============================================================================


class TestEdgeCases:
    """Tests for edge cases."""

    def test_calculator_with_none_history(self, clean_env) -> None:
        """Test calculator works when history is explicitly None."""
        from backend.config.startup_timeouts import StartupTimeoutCalculator

        calculator = StartupTimeoutCalculator(
            trinity_enabled=True, gcp_enabled=True, history=None
        )
        assert calculator.trinity_budget == 420.0
        assert calculator.effective("TRINITY_PHASE") == 300.0

    def test_effective_with_zero_p95(self, clean_env) -> None:
        """Test effective handles zero p95 gracefully (uses base)."""
        from backend.config.startup_timeouts import StartupTimeoutCalculator

        history = MockMetricsHistory({"TRINITY_PHASE": 0.0})
        calculator = StartupTimeoutCalculator(trinity_enabled=True, history=history)
        # 0 * 1.2 = 0, max(300, 0) = 300
        assert calculator.effective("TRINITY_PHASE") == 300.0

    def test_effective_unknown_phase(self, clean_env) -> None:
        """Test effective raises KeyError for unknown phase."""
        from backend.config.startup_timeouts import StartupTimeoutCalculator

        calculator = StartupTimeoutCalculator(trinity_enabled=True)
        with pytest.raises(KeyError):
            calculator.effective("UNKNOWN_PHASE")

    def test_custom_env_values_in_calculator(self, clean_env) -> None:
        """Test calculator picks up custom env values for phase budgets."""
        with patch.dict(os.environ, {
            "JARVIS_TRINITY_PHASE_BUDGET": "400.0",
            "JARVIS_GCP_WAIT_BUFFER": "150.0",
        }):
            from backend.config.startup_timeouts import StartupTimeoutCalculator

            calculator = StartupTimeoutCalculator(
                trinity_enabled=True, gcp_enabled=True
            )
            # 400 + 150 = 550
            assert calculator.trinity_budget == 550.0
