"""
Tests for backend.config.startup_timeouts module.

These tests verify that:
1. Default timeout values are sensible
2. Environment variable overrides work correctly
3. Validation logs warnings and uses defaults for invalid values
4. Timeout relationships are enforced (min < default < max for locks)
5. Validation methods work correctly
6. The singleton pattern works properly

Following 35-point checklist items:
- Item 4: Cross-repo lock timeouts (env-driven)
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

    Removes all JARVIS_ timeout env vars and resets the singleton.
    """
    # Store original values
    original_env = {}
    jarvis_vars = [key for key in os.environ if key.startswith("JARVIS_")]
    for key in jarvis_vars:
        original_env[key] = os.environ.pop(key)

    # Import and reset singleton after cleaning env
    from backend.config.startup_timeouts import reset_timeouts
    reset_timeouts()

    yield

    # Restore original env vars
    for key in jarvis_vars:
        if key in original_env:
            os.environ[key] = original_env[key]

    # Reset singleton again
    reset_timeouts()


@pytest.fixture
def caplog_warning(caplog):
    """Fixture to capture warning-level logs."""
    caplog.set_level(logging.WARNING)
    return caplog


# =============================================================================
# Test: Default Values
# =============================================================================


class TestDefaultValues:
    """Tests for default timeout values."""

    def test_max_timeout_default(self, clean_env) -> None:
        """Test MAX_TIMEOUT has sensible default."""
        from backend.config.startup_timeouts import (
            StartupTimeouts,
            _DEFAULT_MAX_TIMEOUT,
        )
        timeouts = StartupTimeouts()

        assert timeouts.max_timeout == _DEFAULT_MAX_TIMEOUT
        assert timeouts.max_timeout == 900.0  # Must be >= prime_startup_timeout (600.0)

    def test_signal_timeouts_defaults(self, clean_env) -> None:
        """Test signal timeout defaults."""
        from backend.config.startup_timeouts import StartupTimeouts
        timeouts = StartupTimeouts()

        assert timeouts.cleanup_timeout_sigint == 10.0
        assert timeouts.cleanup_timeout_sigterm == 5.0
        assert timeouts.cleanup_timeout_sigkill == 2.0

    def test_port_network_timeouts_defaults(self, clean_env) -> None:
        """Test port/network timeout defaults."""
        from backend.config.startup_timeouts import StartupTimeouts
        timeouts = StartupTimeouts()

        assert timeouts.port_check_timeout == 1.0
        assert timeouts.port_release_wait == 2.0
        assert timeouts.ipc_socket_timeout == 8.0

    def test_tool_timeouts_defaults(self, clean_env) -> None:
        """Test tool timeout defaults."""
        from backend.config.startup_timeouts import StartupTimeouts
        timeouts = StartupTimeouts()

        assert timeouts.lsof_timeout == 5.0
        assert timeouts.docker_check_timeout == 10.0

    def test_health_timeouts_defaults(self, clean_env) -> None:
        """Test health check timeout defaults."""
        from backend.config.startup_timeouts import StartupTimeouts
        timeouts = StartupTimeouts()

        assert timeouts.backend_health_timeout == 30.0
        assert timeouts.frontend_health_timeout == 60.0
        assert timeouts.loading_server_health_timeout == 5.0

    def test_heartbeat_interval_default(self, clean_env) -> None:
        """Test heartbeat interval default."""
        from backend.config.startup_timeouts import StartupTimeouts
        timeouts = StartupTimeouts()

        assert timeouts.heartbeat_interval == 5.0

    def test_trinity_timeouts_defaults(self, clean_env) -> None:
        """Test Trinity component timeout defaults."""
        from backend.config.startup_timeouts import StartupTimeouts
        timeouts = StartupTimeouts()

        assert timeouts.prime_startup_timeout == 600.0
        assert timeouts.reactor_startup_timeout == 120.0
        assert timeouts.reactor_health_timeout == 10.0

    def test_lock_timeouts_defaults(self, clean_env) -> None:
        """Test lock timeout defaults."""
        from backend.config.startup_timeouts import StartupTimeouts
        timeouts = StartupTimeouts()

        assert timeouts.startup_lock_timeout == 30.0
        assert timeouts.takeover_handover_timeout == 15.0
        assert timeouts.max_lock_timeout == 300.0
        assert timeouts.min_lock_timeout == 0.1
        assert timeouts.default_lock_timeout == 5.0
        assert timeouts.stale_lock_retry_timeout == 1.0

    def test_broadcast_timeout_default(self, clean_env) -> None:
        """Test broadcast timeout default."""
        from backend.config.startup_timeouts import StartupTimeouts
        timeouts = StartupTimeouts()

        assert timeouts.broadcast_timeout == 2.0

    def test_async_utility_timeouts_defaults(self, clean_env) -> None:
        """Test async utility timeout defaults."""
        from backend.config.startup_timeouts import StartupTimeouts
        timeouts = StartupTimeouts()

        assert timeouts.process_wait_timeout == 10.0
        assert timeouts.subprocess_timeout == 30.0

    def test_all_defaults_positive(self, clean_env) -> None:
        """Test that all default values are positive."""
        from backend.config.startup_timeouts import StartupTimeouts
        timeouts = StartupTimeouts()

        for name, value in timeouts.to_dict().items():
            assert value > 0, f"{name} should be positive, got {value}"


# =============================================================================
# Test: Environment Variable Overrides
# =============================================================================


class TestEnvOverrides:
    """Tests for environment variable configuration."""

    def test_max_timeout_env_override(self, clean_env) -> None:
        """Test JARVIS_MAX_TIMEOUT env override."""
        from backend.config.startup_timeouts import reset_timeouts
        reset_timeouts()

        with patch.dict(os.environ, {"JARVIS_MAX_TIMEOUT": "600.0"}):
            from backend.config.startup_timeouts import StartupTimeouts
            timeouts = StartupTimeouts()
            assert timeouts.max_timeout == 600.0

    def test_signal_timeout_env_override(self, clean_env) -> None:
        """Test signal timeout env overrides."""
        from backend.config.startup_timeouts import reset_timeouts
        reset_timeouts()

        with patch.dict(os.environ, {
            "JARVIS_CLEANUP_TIMEOUT_SIGINT": "20.0",
            "JARVIS_CLEANUP_TIMEOUT_SIGTERM": "8.0",
            "JARVIS_CLEANUP_TIMEOUT_SIGKILL": "3.0",
        }):
            from backend.config.startup_timeouts import StartupTimeouts
            timeouts = StartupTimeouts()

            assert timeouts.cleanup_timeout_sigint == 20.0
            assert timeouts.cleanup_timeout_sigterm == 8.0
            assert timeouts.cleanup_timeout_sigkill == 3.0

    def test_lock_timeout_env_override(self, clean_env) -> None:
        """Test lock timeout env overrides."""
        from backend.config.startup_timeouts import reset_timeouts
        reset_timeouts()

        with patch.dict(os.environ, {
            "JARVIS_MAX_LOCK_TIMEOUT": "600.0",
            "JARVIS_MIN_LOCK_TIMEOUT": "0.5",
            "JARVIS_DEFAULT_LOCK_TIMEOUT": "10.0",
        }):
            from backend.config.startup_timeouts import StartupTimeouts
            timeouts = StartupTimeouts()

            assert timeouts.max_lock_timeout == 600.0
            assert timeouts.min_lock_timeout == 0.5
            assert timeouts.default_lock_timeout == 10.0

    def test_prime_startup_timeout_env_override(self, clean_env) -> None:
        """Test JARVIS_PRIME_STARTUP_TIMEOUT env override."""
        from backend.config.startup_timeouts import reset_timeouts
        reset_timeouts()

        with patch.dict(os.environ, {"JARVIS_PRIME_STARTUP_TIMEOUT": "900.0"}):
            from backend.config.startup_timeouts import StartupTimeouts
            timeouts = StartupTimeouts()
            assert timeouts.prime_startup_timeout == 900.0

    def test_all_timeouts_can_be_overridden(self, clean_env) -> None:
        """Test that all timeouts can be overridden via env vars."""
        from backend.config.startup_timeouts import reset_timeouts
        reset_timeouts()

        # Note: All individual timeouts must be <= max_timeout to avoid capping
        env_overrides = {
            "JARVIS_MAX_TIMEOUT": "1000.0",  # Set high enough for all other timeouts
            "JARVIS_CLEANUP_TIMEOUT_SIGINT": "15.0",
            "JARVIS_CLEANUP_TIMEOUT_SIGTERM": "7.0",
            "JARVIS_CLEANUP_TIMEOUT_SIGKILL": "3.0",
            "JARVIS_PORT_CHECK_TIMEOUT": "2.0",
            "JARVIS_PORT_RELEASE_WAIT": "3.0",
            "JARVIS_IPC_SOCKET_TIMEOUT": "10.0",
            "JARVIS_LSOF_TIMEOUT": "7.0",
            "JARVIS_DOCKER_CHECK_TIMEOUT": "15.0",
            "JARVIS_BACKEND_HEALTH_TIMEOUT": "45.0",
            "JARVIS_FRONTEND_HEALTH_TIMEOUT": "90.0",
            "JARVIS_LOADING_SERVER_HEALTH_TIMEOUT": "8.0",
            "JARVIS_HEARTBEAT_INTERVAL": "10.0",
            "JARVIS_PRIME_STARTUP_TIMEOUT": "900.0",
            "JARVIS_REACTOR_STARTUP_TIMEOUT": "180.0",
            "JARVIS_REACTOR_HEALTH_TIMEOUT": "15.0",
            "JARVIS_STARTUP_LOCK_TIMEOUT": "45.0",
            "JARVIS_TAKEOVER_HANDOVER_TIMEOUT": "20.0",
            "JARVIS_MAX_LOCK_TIMEOUT": "500.0",
            "JARVIS_MIN_LOCK_TIMEOUT": "0.2",
            "JARVIS_DEFAULT_LOCK_TIMEOUT": "8.0",
            "JARVIS_STALE_LOCK_RETRY_TIMEOUT": "2.0",
            "JARVIS_BROADCAST_TIMEOUT": "3.0",
            "JARVIS_PROCESS_WAIT_TIMEOUT": "15.0",
            "JARVIS_SUBPROCESS_TIMEOUT": "45.0",
        }

        with patch.dict(os.environ, env_overrides):
            from backend.config.startup_timeouts import StartupTimeouts
            timeouts = StartupTimeouts()

            assert timeouts.max_timeout == 1000.0
            assert timeouts.cleanup_timeout_sigint == 15.0
            assert timeouts.cleanup_timeout_sigterm == 7.0
            assert timeouts.cleanup_timeout_sigkill == 3.0
            assert timeouts.port_check_timeout == 2.0
            assert timeouts.port_release_wait == 3.0
            assert timeouts.ipc_socket_timeout == 10.0
            assert timeouts.lsof_timeout == 7.0
            assert timeouts.docker_check_timeout == 15.0
            assert timeouts.backend_health_timeout == 45.0
            assert timeouts.frontend_health_timeout == 90.0
            assert timeouts.loading_server_health_timeout == 8.0
            assert timeouts.heartbeat_interval == 10.0
            assert timeouts.prime_startup_timeout == 900.0
            assert timeouts.reactor_startup_timeout == 180.0
            assert timeouts.reactor_health_timeout == 15.0
            assert timeouts.startup_lock_timeout == 45.0
            assert timeouts.takeover_handover_timeout == 20.0
            assert timeouts.max_lock_timeout == 500.0
            assert timeouts.min_lock_timeout == 0.2
            assert timeouts.default_lock_timeout == 8.0
            assert timeouts.stale_lock_retry_timeout == 2.0
            assert timeouts.broadcast_timeout == 3.0
            assert timeouts.process_wait_timeout == 15.0
            assert timeouts.subprocess_timeout == 45.0


# =============================================================================
# Test: Validation - Invalid Values
# =============================================================================


class TestValidationInvalidValues:
    """Tests for validation of invalid environment values."""

    def test_negative_value_uses_default(self, clean_env, caplog_warning) -> None:
        """Test that negative values trigger warning and use default."""
        from backend.config.startup_timeouts import reset_timeouts, _DEFAULT_MAX_TIMEOUT
        reset_timeouts()

        with patch.dict(os.environ, {"JARVIS_MAX_TIMEOUT": "-5.0"}):
            from backend.config.startup_timeouts import StartupTimeouts
            timeouts = StartupTimeouts()

            assert timeouts.max_timeout == _DEFAULT_MAX_TIMEOUT  # Default (900.0)
            assert "must be positive" in caplog_warning.text

    def test_zero_value_uses_default(self, clean_env, caplog_warning) -> None:
        """Test that zero values trigger warning and use default."""
        from backend.config.startup_timeouts import reset_timeouts
        reset_timeouts()

        with patch.dict(os.environ, {"JARVIS_PORT_CHECK_TIMEOUT": "0"}):
            from backend.config.startup_timeouts import StartupTimeouts
            timeouts = StartupTimeouts()

            assert timeouts.port_check_timeout == 1.0  # Default
            assert "must be positive" in caplog_warning.text

    def test_non_numeric_value_uses_default(self, clean_env, caplog_warning) -> None:
        """Test that non-numeric values trigger warning and use default."""
        from backend.config.startup_timeouts import reset_timeouts
        reset_timeouts()

        with patch.dict(os.environ, {"JARVIS_HEARTBEAT_INTERVAL": "not_a_number"}):
            from backend.config.startup_timeouts import StartupTimeouts
            timeouts = StartupTimeouts()

            assert timeouts.heartbeat_interval == 5.0  # Default
            assert "not a valid number" in caplog_warning.text

    def test_below_minimum_uses_default(self, clean_env, caplog_warning) -> None:
        """Test that values below minimum trigger warning and use default."""
        from backend.config.startup_timeouts import reset_timeouts
        reset_timeouts()

        # min_lock_timeout has min_value=0.01, set to 0.001
        with patch.dict(os.environ, {"JARVIS_MIN_LOCK_TIMEOUT": "0.001"}):
            from backend.config.startup_timeouts import StartupTimeouts
            timeouts = StartupTimeouts()

            assert timeouts.min_lock_timeout == 0.1  # Default
            assert "below minimum" in caplog_warning.text

    def test_empty_string_uses_default(self, clean_env) -> None:
        """Test that empty string uses default (no warning for missing)."""
        from backend.config.startup_timeouts import (
            StartupTimeouts,
            _DEFAULT_MAX_TIMEOUT,
            reset_timeouts,
        )
        reset_timeouts()

        # Empty string should be treated as not set
        with patch.dict(os.environ, {"JARVIS_MAX_TIMEOUT": ""}):
            # Empty string converts to ValueError in float(), so uses default
            timeouts = StartupTimeouts()
            assert timeouts.max_timeout == _DEFAULT_MAX_TIMEOUT


# =============================================================================
# Test: Validation - Lock Timeout Relationships
# =============================================================================


class TestLockTimeoutRelationships:
    """Tests for lock timeout relationship validation."""

    def test_min_less_than_max_valid(self, clean_env) -> None:
        """Test valid min < max relationship is accepted."""
        from backend.config.startup_timeouts import reset_timeouts
        reset_timeouts()

        with patch.dict(os.environ, {
            "JARVIS_MIN_LOCK_TIMEOUT": "0.5",
            "JARVIS_MAX_LOCK_TIMEOUT": "100.0",
        }):
            from backend.config.startup_timeouts import StartupTimeouts
            timeouts = StartupTimeouts()

            assert timeouts.min_lock_timeout == 0.5
            assert timeouts.max_lock_timeout == 100.0

    def test_min_equal_max_adjusted(self, clean_env, caplog_warning) -> None:
        """Test min == max triggers adjustment."""
        from backend.config.startup_timeouts import reset_timeouts
        reset_timeouts()

        with patch.dict(os.environ, {
            "JARVIS_MIN_LOCK_TIMEOUT": "100.0",
            "JARVIS_MAX_LOCK_TIMEOUT": "100.0",
        }):
            from backend.config.startup_timeouts import StartupTimeouts
            timeouts = StartupTimeouts()

            # min should be adjusted to 0.1
            assert timeouts.min_lock_timeout == 0.1
            assert "adjusting min to 0.1" in caplog_warning.text

    def test_min_greater_than_max_adjusted(self, clean_env, caplog_warning) -> None:
        """Test min > max triggers adjustment."""
        from backend.config.startup_timeouts import reset_timeouts
        reset_timeouts()

        with patch.dict(os.environ, {
            "JARVIS_MIN_LOCK_TIMEOUT": "200.0",
            "JARVIS_MAX_LOCK_TIMEOUT": "100.0",
        }):
            from backend.config.startup_timeouts import StartupTimeouts
            timeouts = StartupTimeouts()

            # min should be adjusted to 0.1
            assert timeouts.min_lock_timeout == 0.1
            assert "adjusting min to 0.1" in caplog_warning.text

    def test_default_below_min_adjusted(self, clean_env, caplog_warning) -> None:
        """Test default < min triggers adjustment."""
        from backend.config.startup_timeouts import reset_timeouts
        reset_timeouts()

        with patch.dict(os.environ, {
            "JARVIS_MIN_LOCK_TIMEOUT": "10.0",
            "JARVIS_DEFAULT_LOCK_TIMEOUT": "5.0",
            "JARVIS_MAX_LOCK_TIMEOUT": "300.0",
        }):
            from backend.config.startup_timeouts import StartupTimeouts
            timeouts = StartupTimeouts()

            # default should be adjusted to min
            assert timeouts.default_lock_timeout == 10.0
            assert "adjusting to min" in caplog_warning.text

    def test_default_above_max_adjusted(self, clean_env, caplog_warning) -> None:
        """Test default > max triggers adjustment."""
        from backend.config.startup_timeouts import reset_timeouts
        reset_timeouts()

        with patch.dict(os.environ, {
            "JARVIS_MIN_LOCK_TIMEOUT": "0.1",
            "JARVIS_DEFAULT_LOCK_TIMEOUT": "500.0",
            "JARVIS_MAX_LOCK_TIMEOUT": "300.0",
        }):
            from backend.config.startup_timeouts import StartupTimeouts
            timeouts = StartupTimeouts()

            # default should be adjusted to max
            assert timeouts.default_lock_timeout == 300.0
            assert "adjusting to max" in caplog_warning.text


# =============================================================================
# Test: MAX_TIMEOUT Validation at Initialization
# =============================================================================


class TestMaxTimeoutValidation:
    """Tests for MAX_TIMEOUT validation during __post_init__."""

    def test_timeout_exceeding_max_is_capped(self, clean_env, caplog_warning) -> None:
        """Test that timeouts exceeding max_timeout are capped during init."""
        from backend.config.startup_timeouts import reset_timeouts
        reset_timeouts()

        # Set a low max_timeout but high individual timeout
        with patch.dict(os.environ, {
            "JARVIS_MAX_TIMEOUT": "50.0",
            "JARVIS_PRIME_STARTUP_TIMEOUT": "600.0",  # Exceeds 50.0
        }):
            from backend.config.startup_timeouts import StartupTimeouts
            timeouts = StartupTimeouts()

            # prime_startup_timeout should be capped to max_timeout
            assert timeouts.prime_startup_timeout == 50.0
            assert "capping to max_timeout" in caplog_warning.text

    def test_multiple_timeouts_exceeding_max_all_capped(self, clean_env, caplog_warning) -> None:
        """Test that multiple timeouts exceeding max are all capped."""
        from backend.config.startup_timeouts import reset_timeouts
        reset_timeouts()

        with patch.dict(os.environ, {
            "JARVIS_MAX_TIMEOUT": "100.0",
            "JARVIS_PRIME_STARTUP_TIMEOUT": "600.0",  # Exceeds
            "JARVIS_REACTOR_STARTUP_TIMEOUT": "120.0",  # Exceeds
            "JARVIS_FRONTEND_HEALTH_TIMEOUT": "60.0",  # Within limit
        }):
            from backend.config.startup_timeouts import StartupTimeouts
            timeouts = StartupTimeouts()

            assert timeouts.prime_startup_timeout == 100.0
            assert timeouts.reactor_startup_timeout == 100.0
            assert timeouts.frontend_health_timeout == 60.0  # Not capped

    def test_timeout_at_max_is_not_capped(self, clean_env) -> None:
        """Test that timeouts at exactly max_timeout are not capped."""
        from backend.config.startup_timeouts import reset_timeouts
        reset_timeouts()

        with patch.dict(os.environ, {
            "JARVIS_MAX_TIMEOUT": "100.0",
            "JARVIS_PRIME_STARTUP_TIMEOUT": "100.0",  # Exactly at max
        }):
            from backend.config.startup_timeouts import StartupTimeouts
            timeouts = StartupTimeouts()

            assert timeouts.prime_startup_timeout == 100.0

    def test_timeout_below_max_is_not_capped(self, clean_env) -> None:
        """Test that timeouts below max_timeout are not modified."""
        from backend.config.startup_timeouts import reset_timeouts
        reset_timeouts()

        with patch.dict(os.environ, {
            "JARVIS_MAX_TIMEOUT": "100.0",
            "JARVIS_PRIME_STARTUP_TIMEOUT": "50.0",
        }):
            from backend.config.startup_timeouts import StartupTimeouts
            timeouts = StartupTimeouts()

            assert timeouts.prime_startup_timeout == 50.0


# =============================================================================
# Test: Validation Methods
# =============================================================================


class TestValidationMethods:
    """Tests for explicit validation methods."""

    def test_validate_timeout_positive(self, clean_env) -> None:
        """Test validate_timeout accepts positive values."""
        from backend.config.startup_timeouts import StartupTimeouts
        timeouts = StartupTimeouts()

        result = timeouts.validate_timeout(10.0, "test_timeout")
        assert result == 10.0

    def test_validate_timeout_zero_raises(self, clean_env) -> None:
        """Test validate_timeout raises on zero."""
        from backend.config.startup_timeouts import StartupTimeouts
        timeouts = StartupTimeouts()

        with pytest.raises(ValueError, match="must be positive"):
            timeouts.validate_timeout(0, "test_timeout")

    def test_validate_timeout_negative_raises(self, clean_env) -> None:
        """Test validate_timeout raises on negative."""
        from backend.config.startup_timeouts import StartupTimeouts
        timeouts = StartupTimeouts()

        with pytest.raises(ValueError, match="must be positive"):
            timeouts.validate_timeout(-5.0, "test_timeout")

    def test_validate_timeout_clamps_to_max(self, clean_env, caplog_warning) -> None:
        """Test validate_timeout clamps values above max_timeout."""
        from backend.config.startup_timeouts import StartupTimeouts
        timeouts = StartupTimeouts()

        result = timeouts.validate_timeout(1000.0, "test_timeout")
        assert result == timeouts.max_timeout
        assert "clamping to max" in caplog_warning.text

    def test_validate_lock_timeout_positive(self, clean_env) -> None:
        """Test validate_lock_timeout accepts valid values."""
        from backend.config.startup_timeouts import StartupTimeouts
        timeouts = StartupTimeouts()

        result = timeouts.validate_lock_timeout(10.0)
        assert result == 10.0

    def test_validate_lock_timeout_zero_raises(self, clean_env) -> None:
        """Test validate_lock_timeout raises on zero."""
        from backend.config.startup_timeouts import StartupTimeouts
        timeouts = StartupTimeouts()

        with pytest.raises(ValueError, match="must be positive"):
            timeouts.validate_lock_timeout(0)

    def test_validate_lock_timeout_clamps_to_min(self, clean_env, caplog_warning) -> None:
        """Test validate_lock_timeout clamps values below min."""
        from backend.config.startup_timeouts import StartupTimeouts
        timeouts = StartupTimeouts()

        result = timeouts.validate_lock_timeout(0.01)  # Below min_lock_timeout
        assert result == timeouts.min_lock_timeout
        assert "clamping to min" in caplog_warning.text

    def test_validate_lock_timeout_clamps_to_max(self, clean_env, caplog_warning) -> None:
        """Test validate_lock_timeout clamps values above max."""
        from backend.config.startup_timeouts import StartupTimeouts
        timeouts = StartupTimeouts()

        result = timeouts.validate_lock_timeout(1000.0)  # Above max_lock_timeout
        assert result == timeouts.max_lock_timeout
        assert "clamping to max" in caplog_warning.text


# =============================================================================
# Test: Utility Methods
# =============================================================================


class TestUtilityMethods:
    """Tests for utility methods."""

    def test_get_signal_timeouts(self, clean_env) -> None:
        """Test get_signal_timeouts returns tuple."""
        from backend.config.startup_timeouts import StartupTimeouts
        timeouts = StartupTimeouts()

        result = timeouts.get_signal_timeouts()

        assert isinstance(result, tuple)
        assert len(result) == 3
        assert result == (
            timeouts.cleanup_timeout_sigint,
            timeouts.cleanup_timeout_sigterm,
            timeouts.cleanup_timeout_sigkill,
        )

    def test_to_dict_returns_all_timeouts(self, clean_env) -> None:
        """Test to_dict returns all timeout values."""
        from backend.config.startup_timeouts import StartupTimeouts
        timeouts = StartupTimeouts()

        result = timeouts.to_dict()

        assert isinstance(result, dict)
        # Check all expected keys are present
        expected_keys = [
            "max_timeout",
            "cleanup_timeout_sigint",
            "cleanup_timeout_sigterm",
            "cleanup_timeout_sigkill",
            "port_check_timeout",
            "port_release_wait",
            "ipc_socket_timeout",
            "lsof_timeout",
            "docker_check_timeout",
            "backend_health_timeout",
            "frontend_health_timeout",
            "loading_server_health_timeout",
            "heartbeat_interval",
            "prime_startup_timeout",
            "reactor_startup_timeout",
            "reactor_health_timeout",
            "startup_lock_timeout",
            "takeover_handover_timeout",
            "max_lock_timeout",
            "min_lock_timeout",
            "default_lock_timeout",
            "stale_lock_retry_timeout",
            "broadcast_timeout",
            "process_wait_timeout",
            "subprocess_timeout",
        ]
        for key in expected_keys:
            assert key in result, f"Missing key: {key}"

    def test_to_dict_values_are_floats(self, clean_env) -> None:
        """Test to_dict values are all floats."""
        from backend.config.startup_timeouts import StartupTimeouts
        timeouts = StartupTimeouts()

        result = timeouts.to_dict()

        for key, value in result.items():
            assert isinstance(value, float), f"{key} should be float, got {type(value)}"


# =============================================================================
# Test: Singleton Pattern
# =============================================================================


class TestSingleton:
    """Tests for the module-level singleton."""

    def test_get_timeouts_returns_instance(self, clean_env) -> None:
        """Test get_timeouts returns a StartupTimeouts instance."""
        from backend.config.startup_timeouts import get_timeouts, StartupTimeouts

        result = get_timeouts()

        assert isinstance(result, StartupTimeouts)

    def test_get_timeouts_returns_same_instance(self, clean_env) -> None:
        """Test get_timeouts returns the same instance each time."""
        from backend.config.startup_timeouts import get_timeouts

        first = get_timeouts()
        second = get_timeouts()

        assert first is second

    def test_reset_timeouts_creates_new_instance(self, clean_env) -> None:
        """Test reset_timeouts causes a new instance to be created."""
        from backend.config.startup_timeouts import get_timeouts, reset_timeouts

        first = get_timeouts()
        reset_timeouts()
        second = get_timeouts()

        assert first is not second

    def test_reset_picks_up_env_changes(self, clean_env) -> None:
        """Test that reset_timeouts picks up environment changes."""
        from backend.config.startup_timeouts import get_timeouts, reset_timeouts

        # Get initial instance
        initial = get_timeouts()
        initial_max = initial.max_timeout

        # Change env and reset
        with patch.dict(os.environ, {"JARVIS_MAX_TIMEOUT": "999.0"}):
            reset_timeouts()
            updated = get_timeouts()

            assert updated.max_timeout == 999.0
            assert updated.max_timeout != initial_max


# =============================================================================
# Test: Validation Utilities (Internal)
# =============================================================================


class TestValidationUtilities:
    """Tests for internal validation utilities."""

    def test_get_env_float_valid(self, clean_env) -> None:
        """Test _get_env_float with valid value."""
        from backend.config.startup_timeouts import _get_env_float

        with patch.dict(os.environ, {"TEST_VAR": "42.5"}):
            result = _get_env_float("TEST_VAR", 10.0)
            assert result == 42.5

    def test_get_env_float_missing_uses_default(self, clean_env) -> None:
        """Test _get_env_float uses default when missing."""
        from backend.config.startup_timeouts import _get_env_float

        result = _get_env_float("NONEXISTENT_VAR", 99.0)
        assert result == 99.0

    def test_get_env_float_respects_min(self, clean_env, caplog_warning) -> None:
        """Test _get_env_float respects min_value."""
        from backend.config.startup_timeouts import _get_env_float

        with patch.dict(os.environ, {"TEST_VAR": "0.5"}):
            result = _get_env_float("TEST_VAR", 10.0, min_value=1.0)
            assert result == 10.0  # Falls back to default
            assert "below minimum" in caplog_warning.text

    def test_get_env_float_respects_max(self, clean_env, caplog_warning) -> None:
        """Test _get_env_float respects max_value."""
        from backend.config.startup_timeouts import _get_env_float

        with patch.dict(os.environ, {"TEST_VAR": "1000.0"}):
            result = _get_env_float("TEST_VAR", 10.0, max_value=100.0)
            assert result == 10.0  # Falls back to default
            assert "above maximum" in caplog_warning.text



# =============================================================================
# Test: Edge Cases
# =============================================================================


class TestEdgeCases:
    """Tests for edge cases."""

    def test_whitespace_in_env_value(self, clean_env, caplog_warning) -> None:
        """Test handling of whitespace in env values."""
        from backend.config.startup_timeouts import _get_env_float

        # Whitespace should cause parse failure
        with patch.dict(os.environ, {"TEST_VAR": "  10.0  "}):
            # float("  10.0  ") actually works in Python, so this should succeed
            result = _get_env_float("TEST_VAR", 5.0)
            # Python's float() strips whitespace, so this should work
            assert result == 10.0

    def test_scientific_notation(self, clean_env) -> None:
        """Test handling of scientific notation in env values."""
        from backend.config.startup_timeouts import _get_env_float

        with patch.dict(os.environ, {"TEST_VAR": "1e2"}):
            result = _get_env_float("TEST_VAR", 5.0)
            assert result == 100.0

    def test_very_small_valid_timeout(self, clean_env) -> None:
        """Test very small but valid timeout values."""
        from backend.config.startup_timeouts import _get_env_float

        with patch.dict(os.environ, {"TEST_VAR": "0.001"}):
            result = _get_env_float("TEST_VAR", 1.0, min_value=0.001)
            assert result == 0.001

    def test_multiple_instances_independent(self, clean_env) -> None:
        """Test that multiple StartupTimeouts instances are independent."""
        from backend.config.startup_timeouts import StartupTimeouts

        t1 = StartupTimeouts()
        t2 = StartupTimeouts()

        # Both should have same values (from same env)
        assert t1.max_timeout == t2.max_timeout

        # But they are different objects
        assert t1 is not t2
