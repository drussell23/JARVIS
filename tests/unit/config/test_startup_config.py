"""
Tests for StartupConfig dataclass in backend.config.startup_timeouts.

These tests verify that:
1. Default configuration values are sensible
2. Environment variable overrides work correctly for all config types
3. get_phase_budgets() returns the correct dict structure
4. log_config() method works without errors
5. The singleton pattern works properly
6. create_timeout_calculator() integrates correctly

Following 6 Critical Pillars - Pillar 6: Unified Configuration
"""

import logging
import os
from unittest.mock import patch

import pytest


# =============================================================================
# Test Fixtures
# =============================================================================


@pytest.fixture
def clean_env():
    """
    Fixture to ensure clean environment for each test.

    Removes all JARVIS_ env vars and resets the singletons.
    """
    # Store original values
    original_env = {}
    jarvis_vars = [key for key in os.environ if key.startswith("JARVIS_")]
    for key in jarvis_vars:
        original_env[key] = os.environ.pop(key)

    # Import and reset singletons after cleaning env
    from backend.config.startup_timeouts import reset_timeouts, reset_startup_config
    reset_timeouts()
    reset_startup_config()

    yield

    # Restore original env vars
    for key in jarvis_vars:
        if key in original_env:
            os.environ[key] = original_env[key]

    # Reset singletons again
    reset_timeouts()
    reset_startup_config()


@pytest.fixture
def caplog_info(caplog):
    """Fixture to capture info-level logs."""
    caplog.set_level(logging.INFO)
    return caplog


@pytest.fixture
def caplog_warning(caplog):
    """Fixture to capture warning-level logs."""
    caplog.set_level(logging.WARNING)
    return caplog


# =============================================================================
# Test: Default Values
# =============================================================================


class TestConfigDefaults:
    """Tests for default configuration values."""

    def test_trinity_enabled_default_true(self, clean_env) -> None:
        """Test TRINITY_ENABLED defaults to True."""
        from backend.config.startup_timeouts import StartupConfig

        config = StartupConfig()
        assert config.trinity_enabled is True

    def test_gcp_enabled_default_false(self, clean_env) -> None:
        """Test GCP_ENABLED defaults to False."""
        from backend.config.startup_timeouts import StartupConfig

        config = StartupConfig()
        assert config.gcp_enabled is False

    def test_hollow_ram_threshold_default(self, clean_env) -> None:
        """Test HOLLOW_RAM_THRESHOLD_GB defaults to 32.0."""
        from backend.config.startup_timeouts import (
            StartupConfig,
            _DEFAULT_HOLLOW_RAM_THRESHOLD_GB,
        )

        config = StartupConfig()
        assert config.hollow_ram_threshold_gb == _DEFAULT_HOLLOW_RAM_THRESHOLD_GB
        assert config.hollow_ram_threshold_gb == 32.0

    def test_budgets_composed_correctly(self, clean_env) -> None:
        """Test that PhaseBudgets is composed correctly."""
        from backend.config.startup_timeouts import StartupConfig, PhaseBudgets

        config = StartupConfig()
        assert isinstance(config.budgets, PhaseBudgets)
        assert config.budgets.PRE_TRINITY == 30.0

    def test_timeouts_composed_correctly(self, clean_env) -> None:
        """Test that StartupTimeouts is composed correctly."""
        from backend.config.startup_timeouts import StartupConfig, StartupTimeouts

        config = StartupConfig()
        assert isinstance(config.timeouts, StartupTimeouts)
        assert config.timeouts.max_timeout == 900.0

    def test_all_defaults_are_sensible(self, clean_env) -> None:
        """Test that all default values are sensible for production use."""
        from backend.config.startup_timeouts import StartupConfig

        config = StartupConfig()

        # Feature flags
        assert config.trinity_enabled is True  # Trinity should be on by default
        assert config.gcp_enabled is False  # GCP should be opt-in
        assert config.hollow_ram_threshold_gb > 0  # Must be positive

        # Phase budgets should be positive
        for phase, budget in config.get_phase_budgets().items():
            assert budget > 0, f"Phase {phase} budget should be positive"


# =============================================================================
# Test: Environment Variable Overrides
# =============================================================================


class TestConfigEnvOverride:
    """Tests for environment variable configuration overrides."""

    def test_trinity_enabled_env_override_false(self, clean_env) -> None:
        """Test JARVIS_TRINITY_ENABLED=false override."""
        from backend.config.startup_timeouts import reset_startup_config
        reset_startup_config()

        with patch.dict(os.environ, {"JARVIS_TRINITY_ENABLED": "false"}):
            from backend.config.startup_timeouts import StartupConfig
            config = StartupConfig()
            assert config.trinity_enabled is False

    def test_trinity_enabled_env_override_true(self, clean_env) -> None:
        """Test JARVIS_TRINITY_ENABLED=true override."""
        from backend.config.startup_timeouts import reset_startup_config
        reset_startup_config()

        with patch.dict(os.environ, {"JARVIS_TRINITY_ENABLED": "true"}):
            from backend.config.startup_timeouts import StartupConfig
            config = StartupConfig()
            assert config.trinity_enabled is True

    def test_gcp_enabled_env_override_true(self, clean_env) -> None:
        """Test JARVIS_GCP_ENABLED=true override."""
        from backend.config.startup_timeouts import reset_startup_config
        reset_startup_config()

        with patch.dict(os.environ, {"JARVIS_GCP_ENABLED": "true"}):
            from backend.config.startup_timeouts import StartupConfig
            config = StartupConfig()
            assert config.gcp_enabled is True

    def test_gcp_enabled_env_override_1(self, clean_env) -> None:
        """Test JARVIS_GCP_ENABLED=1 override (numeric true)."""
        from backend.config.startup_timeouts import reset_startup_config
        reset_startup_config()

        with patch.dict(os.environ, {"JARVIS_GCP_ENABLED": "1"}):
            from backend.config.startup_timeouts import StartupConfig
            config = StartupConfig()
            assert config.gcp_enabled is True

    def test_trinity_enabled_env_override_0(self, clean_env) -> None:
        """Test JARVIS_TRINITY_ENABLED=0 override (numeric false)."""
        from backend.config.startup_timeouts import reset_startup_config
        reset_startup_config()

        with patch.dict(os.environ, {"JARVIS_TRINITY_ENABLED": "0"}):
            from backend.config.startup_timeouts import StartupConfig
            config = StartupConfig()
            assert config.trinity_enabled is False

    def test_hollow_ram_threshold_env_override(self, clean_env) -> None:
        """Test JARVIS_HOLLOW_RAM_THRESHOLD_GB override."""
        from backend.config.startup_timeouts import reset_startup_config
        reset_startup_config()

        with patch.dict(os.environ, {"JARVIS_HOLLOW_RAM_THRESHOLD_GB": "64.0"}):
            from backend.config.startup_timeouts import StartupConfig
            config = StartupConfig()
            assert config.hollow_ram_threshold_gb == 64.0

    def test_env_override_yes_no_variants(self, clean_env) -> None:
        """Test yes/no boolean variants."""
        from backend.config.startup_timeouts import reset_startup_config

        # Test "yes"
        reset_startup_config()
        with patch.dict(os.environ, {"JARVIS_GCP_ENABLED": "yes"}):
            from backend.config.startup_timeouts import StartupConfig
            config = StartupConfig()
            assert config.gcp_enabled is True

        # Test "no"
        reset_startup_config()
        with patch.dict(os.environ, {"JARVIS_TRINITY_ENABLED": "no"}):
            config = StartupConfig()
            assert config.trinity_enabled is False

    def test_env_override_on_off_variants(self, clean_env) -> None:
        """Test on/off boolean variants."""
        from backend.config.startup_timeouts import reset_startup_config

        # Test "on"
        reset_startup_config()
        with patch.dict(os.environ, {"JARVIS_GCP_ENABLED": "on"}):
            from backend.config.startup_timeouts import StartupConfig
            config = StartupConfig()
            assert config.gcp_enabled is True

        # Test "off"
        reset_startup_config()
        with patch.dict(os.environ, {"JARVIS_TRINITY_ENABLED": "off"}):
            config = StartupConfig()
            assert config.trinity_enabled is False

    def test_env_override_case_insensitive(self, clean_env) -> None:
        """Test boolean env vars are case-insensitive."""
        from backend.config.startup_timeouts import reset_startup_config

        reset_startup_config()
        with patch.dict(os.environ, {"JARVIS_GCP_ENABLED": "TRUE"}):
            from backend.config.startup_timeouts import StartupConfig
            config = StartupConfig()
            assert config.gcp_enabled is True

        reset_startup_config()
        with patch.dict(os.environ, {"JARVIS_TRINITY_ENABLED": "FALSE"}):
            config = StartupConfig()
            assert config.trinity_enabled is False

    def test_invalid_boolean_uses_default(self, clean_env, caplog_warning) -> None:
        """Test invalid boolean value logs warning and uses default."""
        from backend.config.startup_timeouts import reset_startup_config
        reset_startup_config()

        with patch.dict(os.environ, {"JARVIS_TRINITY_ENABLED": "invalid"}):
            from backend.config.startup_timeouts import StartupConfig
            config = StartupConfig()
            assert config.trinity_enabled is True  # Default
            assert "Invalid boolean value" in caplog_warning.text


# =============================================================================
# Test: get_phase_budgets() Method
# =============================================================================


class TestGetPhaseBudgets:
    """Tests for get_phase_budgets() method."""

    def test_get_phase_budgets_returns_dict(self, clean_env) -> None:
        """Test get_phase_budgets returns a dictionary."""
        from backend.config.startup_timeouts import StartupConfig

        config = StartupConfig()
        result = config.get_phase_budgets()

        assert isinstance(result, dict)

    def test_get_phase_budgets_has_all_phases(self, clean_env) -> None:
        """Test get_phase_budgets includes all expected phases."""
        from backend.config.startup_timeouts import StartupConfig

        config = StartupConfig()
        result = config.get_phase_budgets()

        expected_phases = [
            "PRE_TRINITY",
            "TRINITY_PHASE",
            "GCP_WAIT_BUFFER",
            "POST_TRINITY",
            "DISCOVERY",
            "HEALTH_CHECK",
            "CLEANUP",
        ]

        for phase in expected_phases:
            assert phase in result, f"Missing phase: {phase}"

        # Should have exactly these phases
        assert len(result) == len(expected_phases)

    def test_get_phase_budgets_values_match_budgets(self, clean_env) -> None:
        """Test get_phase_budgets values match budgets object."""
        from backend.config.startup_timeouts import StartupConfig

        config = StartupConfig()
        result = config.get_phase_budgets()

        assert result["PRE_TRINITY"] == config.budgets.PRE_TRINITY
        assert result["TRINITY_PHASE"] == config.budgets.TRINITY_PHASE
        assert result["GCP_WAIT_BUFFER"] == config.budgets.GCP_WAIT_BUFFER
        assert result["POST_TRINITY"] == config.budgets.POST_TRINITY
        assert result["DISCOVERY"] == config.budgets.DISCOVERY
        assert result["HEALTH_CHECK"] == config.budgets.HEALTH_CHECK
        assert result["CLEANUP"] == config.budgets.CLEANUP

    def test_get_phase_budgets_default_values(self, clean_env) -> None:
        """Test get_phase_budgets returns expected default values."""
        from backend.config.startup_timeouts import StartupConfig

        config = StartupConfig()
        result = config.get_phase_budgets()

        # Verify default values match spec
        assert result["PRE_TRINITY"] == 30.0
        assert result["TRINITY_PHASE"] == 300.0
        assert result["GCP_WAIT_BUFFER"] == 120.0
        assert result["POST_TRINITY"] == 60.0
        assert result["DISCOVERY"] == 45.0
        assert result["HEALTH_CHECK"] == 30.0
        assert result["CLEANUP"] == 30.0

    def test_get_phase_budgets_all_values_positive(self, clean_env) -> None:
        """Test all phase budget values are positive floats."""
        from backend.config.startup_timeouts import StartupConfig

        config = StartupConfig()
        result = config.get_phase_budgets()

        for phase, budget in result.items():
            assert isinstance(budget, float), f"{phase} should be float"
            assert budget > 0, f"{phase} should be positive"


# =============================================================================
# Test: log_config() Method
# =============================================================================


class TestLogConfig:
    """Tests for log_config() method."""

    def test_log_config_no_errors(self, clean_env) -> None:
        """Test log_config runs without errors."""
        from backend.config.startup_timeouts import StartupConfig

        config = StartupConfig()
        # Should not raise
        config.log_config()

    def test_log_config_logs_feature_flags(self, clean_env, caplog_info) -> None:
        """Test log_config logs feature flags."""
        from backend.config.startup_timeouts import StartupConfig

        config = StartupConfig()
        config.log_config()

        assert "[StartupConfig]" in caplog_info.text
        assert "Trinity enabled" in caplog_info.text
        assert "GCP enabled" in caplog_info.text

    def test_log_config_logs_phase_budgets(self, clean_env, caplog_info) -> None:
        """Test log_config logs phase budgets."""
        from backend.config.startup_timeouts import StartupConfig

        config = StartupConfig()
        config.log_config()

        assert "Phase budgets" in caplog_info.text
        assert "PRE_TRINITY" in caplog_info.text
        assert "TRINITY_PHASE" in caplog_info.text

    def test_log_config_logs_timeouts(self, clean_env, caplog_info) -> None:
        """Test log_config logs timeout values."""
        from backend.config.startup_timeouts import StartupConfig

        config = StartupConfig()
        config.log_config()

        assert "Max timeout" in caplog_info.text


# =============================================================================
# Test: create_timeout_calculator() Method
# =============================================================================


class TestCreateTimeoutCalculator:
    """Tests for create_timeout_calculator() method."""

    def test_create_timeout_calculator_returns_calculator(self, clean_env) -> None:
        """Test create_timeout_calculator returns a StartupTimeoutCalculator."""
        from backend.config.startup_timeouts import (
            StartupConfig,
            StartupTimeoutCalculator,
        )

        config = StartupConfig()
        calculator = config.create_timeout_calculator()

        assert isinstance(calculator, StartupTimeoutCalculator)

    def test_create_timeout_calculator_uses_config_flags(self, clean_env) -> None:
        """Test calculator uses config's trinity_enabled and gcp_enabled."""
        from backend.config.startup_timeouts import StartupConfig

        # Create config with specific flags
        with patch.dict(os.environ, {
            "JARVIS_TRINITY_ENABLED": "false",
            "JARVIS_GCP_ENABLED": "true",
        }):
            config = StartupConfig()
            calculator = config.create_timeout_calculator()

            # When trinity is disabled, trinity_budget should be 0
            assert calculator.trinity_budget == 0.0

    def test_create_timeout_calculator_accepts_history(self, clean_env) -> None:
        """Test create_timeout_calculator accepts optional history."""
        from typing import Optional

        from backend.config.startup_timeouts import StartupConfig

        class MockHistory:
            def has(self, phase: str) -> bool:
                return phase == "TRINITY_PHASE"

            def get_p95(self, phase: str) -> Optional[float]:
                return 100.0 if phase == "TRINITY_PHASE" else None

        config = StartupConfig()
        mock_history = MockHistory()
        calculator = config.create_timeout_calculator(history=mock_history)

        # With history, effective should use adaptive calculation
        # effective = min(max(base, p95 * 1.2), HARD_CAP)
        # = min(max(300, 100 * 1.2), 900) = min(max(300, 120), 900) = min(300, 900) = 300
        assert calculator.effective("TRINITY_PHASE") == 300.0


# =============================================================================
# Test: Singleton Pattern
# =============================================================================


class TestStartupConfigSingleton:
    """Tests for StartupConfig singleton access."""

    def test_get_startup_config_returns_instance(self, clean_env) -> None:
        """Test get_startup_config returns a StartupConfig instance."""
        from backend.config.startup_timeouts import get_startup_config, StartupConfig

        config = get_startup_config()
        assert isinstance(config, StartupConfig)

    def test_get_startup_config_returns_same_instance(self, clean_env) -> None:
        """Test get_startup_config returns same instance each time."""
        from backend.config.startup_timeouts import get_startup_config

        first = get_startup_config()
        second = get_startup_config()

        assert first is second

    def test_reset_startup_config_creates_new_instance(self, clean_env) -> None:
        """Test reset_startup_config causes new instance creation."""
        from backend.config.startup_timeouts import (
            get_startup_config,
            reset_startup_config,
        )

        first = get_startup_config()
        reset_startup_config()
        second = get_startup_config()

        assert first is not second

    def test_reset_picks_up_env_changes(self, clean_env) -> None:
        """Test reset_startup_config picks up environment changes."""
        from backend.config.startup_timeouts import (
            get_startup_config,
            reset_startup_config,
        )

        # Get initial config
        initial = get_startup_config()
        assert initial.trinity_enabled is True  # Default

        # Change env and reset
        with patch.dict(os.environ, {"JARVIS_TRINITY_ENABLED": "false"}):
            reset_startup_config()
            updated = get_startup_config()

            assert updated.trinity_enabled is False


# =============================================================================
# Test: _get_env_bool() Utility
# =============================================================================


class TestGetEnvBool:
    """Tests for _get_env_bool utility function."""

    def test_get_env_bool_true_values(self, clean_env) -> None:
        """Test _get_env_bool recognizes all true values."""
        from backend.config.startup_timeouts import _get_env_bool

        true_values = ["true", "TRUE", "True", "1", "yes", "YES", "on", "ON"]

        for val in true_values:
            with patch.dict(os.environ, {"TEST_BOOL": val}):
                result = _get_env_bool("TEST_BOOL", False)
                assert result is True, f"'{val}' should be True"

    def test_get_env_bool_false_values(self, clean_env) -> None:
        """Test _get_env_bool recognizes all false values."""
        from backend.config.startup_timeouts import _get_env_bool

        false_values = ["false", "FALSE", "False", "0", "no", "NO", "off", "OFF"]

        for val in false_values:
            with patch.dict(os.environ, {"TEST_BOOL": val}):
                result = _get_env_bool("TEST_BOOL", True)
                assert result is False, f"'{val}' should be False"

    def test_get_env_bool_missing_uses_default(self, clean_env) -> None:
        """Test _get_env_bool uses default when variable not set."""
        from backend.config.startup_timeouts import _get_env_bool

        result = _get_env_bool("NONEXISTENT_VAR", True)
        assert result is True

        result = _get_env_bool("NONEXISTENT_VAR", False)
        assert result is False

    def test_get_env_bool_invalid_logs_warning(self, clean_env, caplog_warning) -> None:
        """Test _get_env_bool logs warning for invalid values."""
        from backend.config.startup_timeouts import _get_env_bool

        with patch.dict(os.environ, {"TEST_BOOL": "maybe"}):
            result = _get_env_bool("TEST_BOOL", True)
            assert result is True  # Uses default
            assert "Invalid boolean value" in caplog_warning.text

    def test_get_env_bool_whitespace_handled(self, clean_env) -> None:
        """Test _get_env_bool handles whitespace."""
        from backend.config.startup_timeouts import _get_env_bool

        with patch.dict(os.environ, {"TEST_BOOL": "  true  "}):
            result = _get_env_bool("TEST_BOOL", False)
            assert result is True


# =============================================================================
# Test: Integration
# =============================================================================


class TestStartupConfigIntegration:
    """Integration tests for StartupConfig with other components."""

    def test_config_budgets_match_calculator_budgets(self, clean_env) -> None:
        """Test config phase budgets match calculator effective budgets."""
        from backend.config.startup_timeouts import StartupConfig

        config = StartupConfig()
        calculator = config.create_timeout_calculator()

        phase_budgets = config.get_phase_budgets()

        # Without history, effective() should return base budget (capped by HARD_CAP)
        for phase, budget in phase_budgets.items():
            effective = calculator.effective(phase)
            assert effective == min(budget, config.budgets.HARD_CAP)

    def test_config_with_all_flags_disabled(self, clean_env) -> None:
        """Test config with trinity and GCP disabled."""
        with patch.dict(os.environ, {
            "JARVIS_TRINITY_ENABLED": "false",
            "JARVIS_GCP_ENABLED": "false",
        }):
            from backend.config.startup_timeouts import StartupConfig

            config = StartupConfig()

            assert config.trinity_enabled is False
            assert config.gcp_enabled is False

            calculator = config.create_timeout_calculator()
            assert calculator.trinity_budget == 0.0

    def test_config_with_all_flags_enabled(self, clean_env) -> None:
        """Test config with trinity and GCP enabled."""
        with patch.dict(os.environ, {
            "JARVIS_TRINITY_ENABLED": "true",
            "JARVIS_GCP_ENABLED": "true",
        }):
            from backend.config.startup_timeouts import StartupConfig

            config = StartupConfig()

            assert config.trinity_enabled is True
            assert config.gcp_enabled is True

            calculator = config.create_timeout_calculator()
            # trinity_budget = TRINITY_PHASE + GCP_WAIT_BUFFER = 300 + 120 = 420
            assert calculator.trinity_budget == 420.0
