# tests/unit/backend/core/test_log_severity_bridge.py
"""Tests for log severity bridge - temporary module until ComponentRegistry exists."""
import pytest
from unittest.mock import patch, MagicMock


class TestComponentCriticality:
    """Test criticality lookup and normalization."""

    def test_normalize_component_name_kebab_case(self):
        from backend.core.log_severity_bridge import _normalize_component_name
        assert _normalize_component_name("jarvis_prime") == "jarvis-prime"
        assert _normalize_component_name("JARVIS_PRIME") == "jarvis-prime"
        assert _normalize_component_name("Jarvis Prime") == "jarvis-prime"
        assert _normalize_component_name("jarvis-prime") == "jarvis-prime"

    def test_get_criticality_returns_default_for_unknown(self):
        from backend.core.log_severity_bridge import _get_criticality
        assert _get_criticality("unknown-component") == "optional"

    def test_get_criticality_returns_required_for_core(self):
        from backend.core.log_severity_bridge import _get_criticality
        assert _get_criticality("jarvis-core") == "required"
        assert _get_criticality("backend") == "required"

    def test_get_criticality_returns_degraded_ok_for_prime(self):
        from backend.core.log_severity_bridge import _get_criticality
        assert _get_criticality("jarvis-prime") == "degraded_ok"
        assert _get_criticality("voice-unlock") == "degraded_ok"

    def test_get_criticality_respects_env_override(self):
        from backend.core.log_severity_bridge import _get_criticality
        import os
        os.environ["REDIS_CRITICALITY"] = "required"
        try:
            assert _get_criticality("redis") == "required"
        finally:
            del os.environ["REDIS_CRITICALITY"]


class TestLogComponentFailure:
    """Test the log_component_failure bridge function."""

    def test_required_component_logs_error(self):
        from backend.core.log_severity_bridge import log_component_failure
        with patch('backend.core.log_severity_bridge.logger') as mock_logger:
            log_component_failure("jarvis-core", "Startup failed")
            mock_logger.error.assert_called_once()
            assert "jarvis-core" in str(mock_logger.error.call_args)

    def test_optional_component_logs_info(self):
        from backend.core.log_severity_bridge import log_component_failure
        with patch('backend.core.log_severity_bridge.logger') as mock_logger:
            log_component_failure("redis", "Connection refused")
            mock_logger.info.assert_called_once()
            assert "optional" in str(mock_logger.info.call_args)

    def test_degraded_ok_component_logs_warning(self):
        from backend.core.log_severity_bridge import log_component_failure
        with patch('backend.core.log_severity_bridge.logger') as mock_logger:
            log_component_failure("jarvis-prime", "GPU not available")
            mock_logger.warning.assert_called_once()

    def test_exception_info_included_when_provided(self):
        from backend.core.log_severity_bridge import log_component_failure
        with patch('backend.core.log_severity_bridge.logger') as mock_logger:
            try:
                raise ValueError("Test error")
            except ValueError as e:
                log_component_failure("jarvis-core", "Failed", error=e)

            call_kwargs = mock_logger.error.call_args[1]
            assert "exc_info" in call_kwargs
