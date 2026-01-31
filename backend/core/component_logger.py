# backend/core/component_logger.py
"""
ComponentLogger - Registry-aware logging with automatic severity derivation.

Replaces the temporary log_severity_bridge once ComponentRegistry is in use.
This module derives log severity from the ComponentRegistry's criticality
rather than a hardcoded dictionary.

Usage:
    from backend.core.component_logger import ComponentLogger, get_component_logger

    # With explicit registry
    logger = ComponentLogger("jarvis-prime", registry)
    logger.failure("GPU not available")  # Logs at WARNING (DEGRADED_OK)
    logger.info("Model loaded")          # Always INFO

    # With global registry
    logger = get_component_logger("redis")
    logger.startup_failed("Connection refused")  # Logs based on criticality
"""
from __future__ import annotations

import logging
from typing import Optional, Any, Dict

from backend.core.component_registry import (
    ComponentRegistry, Criticality, get_component_registry
)


class ComponentLogger:
    """
    Logger that derives severity from ComponentRegistry.

    The key insight is that log severity for failures should match component
    criticality:
    - REQUIRED components: Failures are errors (system can't function)
    - DEGRADED_OK components: Failures are warnings (system can continue)
    - OPTIONAL components: Failures are info (nice to know)

    Usage:
        logger = ComponentLogger("jarvis-prime", registry)
        logger.failure("GPU not available")  # Logs at WARNING (DEGRADED_OK)
        logger.info("Model loaded")          # Always INFO
    """

    def __init__(self, component_name: str, registry: Optional[ComponentRegistry] = None):
        """Initialize ComponentLogger.

        Args:
            component_name: Name of the component this logger represents
            registry: Optional ComponentRegistry. If not provided, uses global registry.
        """
        self.component = component_name
        self.registry = registry or get_component_registry()
        self._logger = logging.getLogger(f"jarvis.{component_name}")

    def _get_criticality(self) -> Criticality:
        """Get effective criticality for this component.

        Returns:
            Criticality level from registry, or OPTIONAL if not registered.
        """
        try:
            defn = self.registry.get(self.component)
            return defn.effective_criticality
        except KeyError:
            # Component not registered, default to optional
            return Criticality.OPTIONAL

    def failure(
        self,
        message: str,
        error: Optional[Exception] = None,
        **context: Any
    ) -> None:
        """
        Log a failure at appropriate severity based on criticality.

        The severity is automatically derived from the component's criticality:
        - REQUIRED -> ERROR (system cannot function without this)
        - DEGRADED_OK -> WARNING (system can continue in degraded mode)
        - OPTIONAL -> INFO (nice to have, failure is informational)

        Args:
            message: Description of the failure
            error: Optional exception to include traceback
            **context: Additional context to include in log extra
        """
        criticality = self._get_criticality()

        log_kwargs: Dict[str, Any] = {}
        if context:
            log_kwargs["extra"] = context
        if error:
            log_kwargs["exc_info"] = (type(error), error, error.__traceback__)

        full_message = f"{self.component}: {message}"

        if criticality == Criticality.REQUIRED:
            self._logger.error(full_message, **log_kwargs)
        elif criticality == Criticality.DEGRADED_OK:
            self._logger.warning(full_message, **log_kwargs)
        else:
            self._logger.info(f"{full_message} (optional)", **log_kwargs)

    def startup_failed(self, reason: str, error: Optional[Exception] = None) -> None:
        """Convenience method for startup failures.

        Args:
            reason: Why startup failed
            error: Optional exception to include traceback
        """
        self.failure(f"Startup failed: {reason}", error=error, phase="startup")

    def health_check_failed(self, reason: str) -> None:
        """Convenience method for health check failures.

        Args:
            reason: Why health check failed
        """
        self.failure(f"Health check failed: {reason}", phase="health_check")

    # Standard logging methods (always use stated level)
    def debug(self, message: str, **context: Any) -> None:
        """Log at DEBUG level.

        Args:
            message: Log message
            **context: Additional context for extra
        """
        self._logger.debug(f"{self.component}: {message}", extra=context or None)

    def info(self, message: str, **context: Any) -> None:
        """Log at INFO level.

        Args:
            message: Log message
            **context: Additional context for extra
        """
        self._logger.info(f"{self.component}: {message}", extra=context or None)

    def warning(self, message: str, **context: Any) -> None:
        """Log at WARNING level.

        Args:
            message: Log message
            **context: Additional context for extra
        """
        self._logger.warning(f"{self.component}: {message}", extra=context or None)

    def error(self, message: str, **context: Any) -> None:
        """Log at ERROR level.

        Args:
            message: Log message
            **context: Additional context for extra
        """
        self._logger.error(f"{self.component}: {message}", extra=context or None)


def get_component_logger(component_name: str) -> ComponentLogger:
    """Factory function for ComponentLogger.

    Creates a ComponentLogger using the global ComponentRegistry.

    Args:
        component_name: Name of the component

    Returns:
        ComponentLogger instance for the specified component
    """
    return ComponentLogger(component_name)
