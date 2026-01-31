# tests/unit/backend/core/test_component_logger.py
"""Tests for ComponentLogger - registry-aware logging."""
import pytest
from unittest.mock import patch, MagicMock


class TestComponentLogger:
    """Test ComponentLogger with registry-aware severity derivation."""

    def test_failure_logs_error_for_required(self):
        from backend.core.component_logger import ComponentLogger
        from backend.core.component_registry import (
            get_component_registry, ComponentDefinition,
            Criticality, ProcessType
        )

        registry = get_component_registry()
        registry._reset_for_testing()
        registry.register(ComponentDefinition(
            name="critical-comp",
            criticality=Criticality.REQUIRED,
            process_type=ProcessType.IN_PROCESS,
        ))

        logger = ComponentLogger("critical-comp", registry)

        with patch.object(logger._logger, 'error') as mock_error:
            logger.failure("Something broke")
            mock_error.assert_called_once()

    def test_failure_logs_warning_for_degraded_ok(self):
        from backend.core.component_logger import ComponentLogger
        from backend.core.component_registry import (
            get_component_registry, ComponentDefinition,
            Criticality, ProcessType
        )

        registry = get_component_registry()
        registry._reset_for_testing()
        registry.register(ComponentDefinition(
            name="degradable-comp",
            criticality=Criticality.DEGRADED_OK,
            process_type=ProcessType.IN_PROCESS,
        ))

        logger = ComponentLogger("degradable-comp", registry)

        with patch.object(logger._logger, 'warning') as mock_warning:
            logger.failure("GPU not available")
            mock_warning.assert_called_once()

    def test_failure_logs_info_for_optional(self):
        from backend.core.component_logger import ComponentLogger
        from backend.core.component_registry import (
            get_component_registry, ComponentDefinition,
            Criticality, ProcessType
        )

        registry = get_component_registry()
        registry._reset_for_testing()
        registry.register(ComponentDefinition(
            name="optional-comp",
            criticality=Criticality.OPTIONAL,
            process_type=ProcessType.IN_PROCESS,
        ))

        logger = ComponentLogger("optional-comp", registry)

        with patch.object(logger._logger, 'info') as mock_info:
            logger.failure("Not connected")
            mock_info.assert_called_once()

    def test_failure_defaults_to_optional_for_unregistered(self):
        """Unregistered components should log at INFO level."""
        from backend.core.component_logger import ComponentLogger
        from backend.core.component_registry import get_component_registry

        registry = get_component_registry()
        registry._reset_for_testing()
        # Don't register anything

        logger = ComponentLogger("unknown-comp", registry)

        with patch.object(logger._logger, 'info') as mock_info:
            logger.failure("Component not found")
            mock_info.assert_called_once()
            assert "optional" in str(mock_info.call_args)

    def test_failure_includes_exception_info(self):
        """Exception info should be included when error is provided."""
        from backend.core.component_logger import ComponentLogger
        from backend.core.component_registry import (
            get_component_registry, ComponentDefinition,
            Criticality, ProcessType
        )

        registry = get_component_registry()
        registry._reset_for_testing()
        registry.register(ComponentDefinition(
            name="test-comp",
            criticality=Criticality.REQUIRED,
            process_type=ProcessType.IN_PROCESS,
        ))

        logger = ComponentLogger("test-comp", registry)

        try:
            raise ValueError("Test error")
        except ValueError as e:
            with patch.object(logger._logger, 'error') as mock_error:
                logger.failure("Something broke", error=e)
                call_kwargs = mock_error.call_args[1]
                assert "exc_info" in call_kwargs
                assert call_kwargs["exc_info"][0] is ValueError

    def test_startup_failed_convenience_method(self):
        """startup_failed should call failure with phase=startup."""
        from backend.core.component_logger import ComponentLogger
        from backend.core.component_registry import (
            get_component_registry, ComponentDefinition,
            Criticality, ProcessType
        )

        registry = get_component_registry()
        registry._reset_for_testing()
        registry.register(ComponentDefinition(
            name="test-comp",
            criticality=Criticality.REQUIRED,
            process_type=ProcessType.IN_PROCESS,
        ))

        logger = ComponentLogger("test-comp", registry)

        with patch.object(logger, 'failure') as mock_failure:
            logger.startup_failed("Connection refused")
            mock_failure.assert_called_once()
            call_args = mock_failure.call_args
            assert "Startup failed:" in call_args[0][0]
            assert call_args[1].get("phase") == "startup"

    def test_health_check_failed_convenience_method(self):
        """health_check_failed should call failure with phase=health_check."""
        from backend.core.component_logger import ComponentLogger
        from backend.core.component_registry import (
            get_component_registry, ComponentDefinition,
            Criticality, ProcessType
        )

        registry = get_component_registry()
        registry._reset_for_testing()
        registry.register(ComponentDefinition(
            name="test-comp",
            criticality=Criticality.DEGRADED_OK,
            process_type=ProcessType.IN_PROCESS,
        ))

        logger = ComponentLogger("test-comp", registry)

        with patch.object(logger, 'failure') as mock_failure:
            logger.health_check_failed("Timeout")
            mock_failure.assert_called_once()
            call_args = mock_failure.call_args
            assert "Health check failed:" in call_args[0][0]
            assert call_args[1].get("phase") == "health_check"

    def test_standard_logging_methods(self):
        """Standard logging methods should always use their stated level."""
        from backend.core.component_logger import ComponentLogger
        from backend.core.component_registry import get_component_registry

        registry = get_component_registry()
        registry._reset_for_testing()

        logger = ComponentLogger("test-comp", registry)

        with patch.object(logger._logger, 'debug') as mock_debug:
            logger.debug("Debug message")
            mock_debug.assert_called_once()

        with patch.object(logger._logger, 'info') as mock_info:
            logger.info("Info message")
            mock_info.assert_called_once()

        with patch.object(logger._logger, 'warning') as mock_warning:
            logger.warning("Warning message")
            mock_warning.assert_called_once()

        with patch.object(logger._logger, 'error') as mock_error:
            logger.error("Error message")
            mock_error.assert_called_once()

    def test_get_component_logger_factory(self):
        """Factory function should return ComponentLogger instance."""
        from backend.core.component_logger import get_component_logger

        logger = get_component_logger("test-comp")
        assert logger.component == "test-comp"

    def test_respects_criticality_override_env(self):
        """ComponentLogger should respect env-based criticality override."""
        import os
        from backend.core.component_logger import ComponentLogger
        from backend.core.component_registry import (
            get_component_registry, ComponentDefinition,
            Criticality, ProcessType
        )

        registry = get_component_registry()
        registry._reset_for_testing()
        registry.register(ComponentDefinition(
            name="env-override-comp",
            criticality=Criticality.OPTIONAL,
            process_type=ProcessType.IN_PROCESS,
            criticality_override_env="TEST_COMP_REQUIRED",
        ))

        # Set env to override to required
        os.environ["TEST_COMP_REQUIRED"] = "true"
        try:
            logger = ComponentLogger("env-override-comp", registry)

            with patch.object(logger._logger, 'error') as mock_error:
                logger.failure("Override test")
                mock_error.assert_called_once()
        finally:
            del os.environ["TEST_COMP_REQUIRED"]

    def test_message_includes_component_name(self):
        """Log messages should include the component name."""
        from backend.core.component_logger import ComponentLogger
        from backend.core.component_registry import (
            get_component_registry, ComponentDefinition,
            Criticality, ProcessType
        )

        registry = get_component_registry()
        registry._reset_for_testing()
        registry.register(ComponentDefinition(
            name="my-component",
            criticality=Criticality.REQUIRED,
            process_type=ProcessType.IN_PROCESS,
        ))

        logger = ComponentLogger("my-component", registry)

        with patch.object(logger._logger, 'error') as mock_error:
            logger.failure("Test message")
            call_args = mock_error.call_args[0][0]
            assert "my-component" in call_args
            assert "Test message" in call_args
