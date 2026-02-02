"""
RecoveryEngine - Error classification and recovery strategy selection.

This module provides:
- ErrorClass: Categories of errors for classification
- RecoveryPhase: When the error occurred (startup vs runtime)
- RecoveryStrategy: How to handle the error
- ErrorClassifier: Classifies exceptions into error classes
- RecoveryEngine: Determines recovery actions based on error classification
"""
from __future__ import annotations

import errno
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional, Type, Union

from backend.core.component_registry import (
    ComponentRegistry,
    Criticality,
)

logger = logging.getLogger("jarvis.recovery_engine")


class ErrorClass(Enum):
    """Categories of errors for classification."""
    # Basic error classes
    TRANSIENT = "transient"                   # Retry is likely to succeed
    TRANSIENT_NETWORK = "transient_network"   # Network-related transient error
    TIMEOUT = "timeout"                       # Operation timed out
    NETWORK = "network"                       # Network connectivity issue
    RESOURCE_EXHAUSTED = "resource_exhausted" # Out of memory/disk/etc
    CONFIGURATION = "configuration"           # Configuration or auth issue
    FATAL = "fatal"                           # Unrecoverable error
    UNKNOWN = "unknown"                       # Unknown error type
    # Semantic error classes
    NEEDS_FALLBACK = "needs_fallback"         # Switch to alternative
    MISSING_RESOURCE = "missing_resource"     # Resource doesn't exist
    RESOURCE_EXHAUSTION = "resource_exhaustion"  # Alias for RESOURCE_EXHAUSTED


class RecoveryPhase(Enum):
    """When the error occurred."""
    STARTUP = "startup"    # During initial startup
    RUNTIME = "runtime"    # During normal operation


class RecoveryStrategy(Enum):
    """How to handle the error."""
    RETRY = "retry"                      # Simple retry (for transient errors)
    FULL_RESTART = "full_restart"        # Restart the component
    FALLBACK_MODE = "fallback_mode"      # Use fallback capability
    DISABLE_AND_CONTINUE = "disable"     # Disable and continue without
    ESCALATE_TO_USER = "escalate"        # Needs human intervention
    CUSTOM_HANDLED = "custom_handled"    # v192.0: Handled by custom recovery handler


@dataclass
class ErrorClassification:
    """Result of classifying an error."""
    error_class: ErrorClass
    suggested_strategy: RecoveryStrategy
    is_retryable: bool
    needs_fallback: bool


@dataclass
class RecoveryAction:
    """Action to take in response to an error."""
    strategy: RecoveryStrategy
    delay: float = 0.0
    fallback_targets: Dict[str, str] = field(default_factory=dict)
    message: Optional[str] = None


class ErrorClassifier:
    """Classifies exceptions into error classes."""

    # Map exception types to error classes
    CLASSIFICATION_RULES: Dict[Union[Type[Exception], str], ErrorClass] = {
        ConnectionRefusedError: ErrorClass.TRANSIENT_NETWORK,
        ConnectionResetError: ErrorClass.TRANSIENT_NETWORK,
        ConnectionAbortedError: ErrorClass.TRANSIENT_NETWORK,
        TimeoutError: ErrorClass.TRANSIENT_NETWORK,
        FileNotFoundError: ErrorClass.MISSING_RESOURCE,
        MemoryError: ErrorClass.RESOURCE_EXHAUSTION,
        # String patterns matched against error messages
        "CloudOffloadRequired": ErrorClass.NEEDS_FALLBACK,
        "GPUNotAvailable": ErrorClass.NEEDS_FALLBACK,
    }

    # Map errno values to error classes
    ERRNO_RULES: Dict[int, ErrorClass] = {
        errno.ECONNREFUSED: ErrorClass.TRANSIENT_NETWORK,
        errno.ECONNRESET: ErrorClass.TRANSIENT_NETWORK,
        errno.ECONNABORTED: ErrorClass.TRANSIENT_NETWORK,
        errno.ETIMEDOUT: ErrorClass.TRANSIENT_NETWORK,
        errno.ENETUNREACH: ErrorClass.TRANSIENT_NETWORK,
        errno.EHOSTUNREACH: ErrorClass.TRANSIENT_NETWORK,
        errno.ENOENT: ErrorClass.MISSING_RESOURCE,
        errno.ENOSPC: ErrorClass.RESOURCE_EXHAUSTION,
        errno.ENOMEM: ErrorClass.RESOURCE_EXHAUSTION,
    }

    def classify(self, error: Exception) -> ErrorClassification:
        """
        Classify an exception into an error class and suggest recovery strategy.

        Args:
            error: The exception to classify

        Returns:
            ErrorClassification with class, strategy, and flags
        """
        error_class = self._determine_error_class(error)
        suggested_strategy = self._suggest_strategy(error_class)
        is_retryable = self._is_retryable(error_class)
        needs_fallback = error_class == ErrorClass.NEEDS_FALLBACK

        return ErrorClassification(
            error_class=error_class,
            suggested_strategy=suggested_strategy,
            is_retryable=is_retryable,
            needs_fallback=needs_fallback,
        )

    def _determine_error_class(self, error: Exception) -> ErrorClass:
        """Determine the error class for an exception."""
        # Check exact exception type first
        for exc_type, error_class in self.CLASSIFICATION_RULES.items():
            if isinstance(exc_type, type) and isinstance(error, exc_type):
                return error_class

        # Check errno for OSError subclasses
        if isinstance(error, OSError) and error.errno is not None:
            if error.errno in self.ERRNO_RULES:
                return self.ERRNO_RULES[error.errno]

        # Check error message for string patterns
        error_message = str(error)
        for pattern, error_class in self.CLASSIFICATION_RULES.items():
            if isinstance(pattern, str) and pattern in error_message:
                return error_class

        # Default to transient network (retryable) for unknown errors
        logger.debug(f"Unknown error type {type(error).__name__}, defaulting to TRANSIENT_NETWORK")
        return ErrorClass.TRANSIENT_NETWORK

    def _suggest_strategy(self, error_class: ErrorClass) -> RecoveryStrategy:
        """Suggest a recovery strategy based on error class."""
        strategy_map = {
            ErrorClass.TRANSIENT_NETWORK: RecoveryStrategy.FULL_RESTART,
            ErrorClass.NEEDS_FALLBACK: RecoveryStrategy.FALLBACK_MODE,
            ErrorClass.MISSING_RESOURCE: RecoveryStrategy.DISABLE_AND_CONTINUE,
            ErrorClass.RESOURCE_EXHAUSTION: RecoveryStrategy.DISABLE_AND_CONTINUE,
        }
        return strategy_map.get(error_class, RecoveryStrategy.DISABLE_AND_CONTINUE)

    def _is_retryable(self, error_class: ErrorClass) -> bool:
        """Determine if an error class is retryable."""
        return error_class in {
            ErrorClass.TRANSIENT_NETWORK,
        }


class RecoveryEngine:
    """
    Determines recovery actions for component failures.

    Uses error classification and component configuration to decide:
    - Whether to retry (with exponential backoff)
    - Whether to use a fallback
    - Whether to disable the component
    - Whether to escalate to the user
    """

    # Exponential backoff multiplier
    BACKOFF_MULTIPLIER = 1.5

    # Type alias for custom recovery handlers
    # Handler signature: async def handler(component: str, error: Exception, context: Dict) -> bool
    # Returns True if handled, False to fall through to standard logic
    CustomRecoveryHandler = Callable[[str, Exception, Dict[str, Any]], Awaitable[bool]]

    def __init__(
        self,
        registry: ComponentRegistry,
        error_classifier: ErrorClassifier,
    ):
        """
        Initialize the recovery engine.

        Args:
            registry: Component registry for looking up definitions
            error_classifier: Classifier for error analysis
        """
        self._registry = registry
        self._classifier = error_classifier
        self._attempt_count: Dict[str, int] = {}
        # v192.0: Custom recovery handlers registry
        # Maps error type names to async handler functions
        self._custom_handlers: Dict[str, List[RecoveryEngine.CustomRecoveryHandler]] = {}

    async def handle_failure(
        self,
        component: str,
        error: Exception,
        phase: RecoveryPhase,
        context: Optional[Dict[str, Any]] = None,
    ) -> RecoveryAction:
        """
        Handle a component failure and determine recovery action.

        Args:
            component: Name of the failed component
            error: The exception that was raised
            phase: Whether this occurred during startup or runtime
            context: Optional additional context for custom recovery handlers

        Returns:
            RecoveryAction describing what to do next

        Raises:
            KeyError: If component is not registered
        """
        # Get component definition
        definition = self._registry.get(component)

        # Classify the error
        classification = self._classifier.classify(error)

        # Track attempts
        if component not in self._attempt_count:
            self._attempt_count[component] = 0

        logger.info(
            f"Handling failure for {component}: {type(error).__name__} - "
            f"class={classification.error_class.value}, "
            f"attempt={self._attempt_count[component] + 1}"
        )

        # v192.0: Try custom recovery handlers first
        # Map error classifications to error type strings for custom handler lookup
        error_types_to_try = self._get_error_types(classification, error)
        for error_type in error_types_to_try:
            # Merge caller context with internal context (caller takes precedence)
            handler_context = {
                "component": component,
                "phase": phase.value,
                "error_class": classification.error_class.value,
                "attempt": self._attempt_count[component] + 1,
            }
            if context:
                handler_context.update(context)
            if await self.try_custom_recovery(error_type, component, error, handler_context):
                logger.info(f"Custom handler handled {error_type} for {component}")
                return RecoveryAction(
                    strategy=RecoveryStrategy.CUSTOM_HANDLED,
                    message=f"Handled by custom recovery handler for '{error_type}'",
                )

        # Check if component has fallback and error needs fallback
        if classification.needs_fallback and definition.fallback_for_capabilities:
            logger.info(f"Using fallback for {component}")
            return RecoveryAction(
                strategy=RecoveryStrategy.FALLBACK_MODE,
                fallback_targets=definition.fallback_for_capabilities,
                message=f"Switching to fallback for capabilities: {list(definition.fallback_for_capabilities.keys())}",
            )

        # Non-retryable errors skip retries
        if not classification.is_retryable:
            return self._handle_exhausted_retries(component, error, definition)

        # Check if retries are available
        max_attempts = definition.retry_max_attempts
        current_attempts = self._attempt_count[component]

        if current_attempts < max_attempts:
            # Retry with exponential backoff
            self._attempt_count[component] += 1
            delay = definition.retry_delay_seconds * (
                self.BACKOFF_MULTIPLIER ** current_attempts
            )

            logger.info(
                f"Will retry {component} after {delay:.2f}s "
                f"(attempt {current_attempts + 1}/{max_attempts})"
            )

            return RecoveryAction(
                strategy=RecoveryStrategy.FULL_RESTART,
                delay=delay,
                message=f"Retrying after {delay:.2f}s (attempt {current_attempts + 1}/{max_attempts})",
            )

        # Retries exhausted
        return self._handle_exhausted_retries(component, error, definition)

    def _get_error_types(
        self,
        classification: ErrorClassification,
        error: Exception,
    ) -> List[str]:
        """
        v192.0: Map error classification to error type strings for custom handler lookup.

        Returns a list of error types to try, in order of specificity:
        1. Exception class name (e.g., "TimeoutError", "MemoryError")
        2. Error class value (e.g., "timeout", "resource_exhausted")
        3. Semantic type based on error characteristics

        Args:
            classification: The error classification result
            error: The original exception

        Returns:
            List of error type strings to try
        """
        error_types = []

        # 1. Exception class name (most specific)
        error_types.append(type(error).__name__.lower())

        # 2. Error class value
        error_types.append(classification.error_class.value)

        # 3. Semantic mappings based on error characteristics
        error_str = str(error).lower()

        # GCP-related errors
        if "gcp" in error_str or "google" in error_str or "cloud" in error_str:
            if classification.error_class == ErrorClass.TIMEOUT:
                error_types.append("gcp_timeout")
            else:
                error_types.append("gcp_error")

        # Memory-related errors
        if (
            classification.error_class in (ErrorClass.RESOURCE_EXHAUSTED, ErrorClass.RESOURCE_EXHAUSTION)
            or "memory" in error_str
            or "ram" in error_str
            or isinstance(error, MemoryError)
        ):
            error_types.append("memory_pressure")

        # Timeout-related (general)
        if classification.error_class == ErrorClass.TIMEOUT:
            error_types.append("timeout")

        # Network-related
        if classification.error_class in (ErrorClass.NETWORK, ErrorClass.TRANSIENT_NETWORK):
            error_types.append("network_error")

        # Remove duplicates while preserving order
        seen = set()
        unique_types = []
        for t in error_types:
            if t not in seen:
                seen.add(t)
                unique_types.append(t)

        return unique_types

    def _handle_exhausted_retries(
        self,
        component: str,
        error: Exception,
        definition,
    ) -> RecoveryAction:
        """Handle the case when retries are exhausted or error is non-retryable."""
        criticality = definition.effective_criticality

        if criticality == Criticality.REQUIRED:
            logger.error(
                f"Required component {component} failed and cannot recover: {error}"
            )
            return RecoveryAction(
                strategy=RecoveryStrategy.ESCALATE_TO_USER,
                message=f"Critical component '{component}' failed: {error}. Manual intervention required.",
            )

        # DEGRADED_OK or OPTIONAL - disable and continue
        logger.warning(
            f"Component {component} ({criticality.value}) failed, disabling: {error}"
        )
        return RecoveryAction(
            strategy=RecoveryStrategy.DISABLE_AND_CONTINUE,
            message=f"Component '{component}' disabled due to: {error}",
        )

    def reset_attempts(self, component: str) -> None:
        """
        Reset the attempt counter for a component.

        Call this when a component successfully recovers to allow
        future failures to retry again.

        Args:
            component: Name of the component to reset
        """
        if component in self._attempt_count:
            logger.debug(f"Resetting attempt count for {component}")
            del self._attempt_count[component]

    def register_custom_recovery(
        self,
        error_type: str,
        handler: CustomRecoveryHandler,
    ) -> None:
        """
        v192.0: Register a custom recovery handler for a specific error type.

        Custom handlers are called BEFORE the standard recovery logic.
        If a handler returns True, it means the error was handled and
        standard recovery logic is skipped.

        Multiple handlers can be registered for the same error type.
        They are called in registration order until one returns True.

        Args:
            error_type: Identifier for the error type (e.g., "gcp_timeout", "memory_pressure")
            handler: Async function that handles the error
                     Signature: async def handler(component: str, error: Exception, context: Dict) -> bool
                     Returns True if handled, False to fall through

        Example:
            async def gcp_timeout_recovery(component: str, error: Exception, context: Dict) -> bool:
                # Custom recovery logic
                return True  # Error was handled

            engine.register_custom_recovery("gcp_timeout", gcp_timeout_recovery)
        """
        if error_type not in self._custom_handlers:
            self._custom_handlers[error_type] = []

        self._custom_handlers[error_type].append(handler)
        logger.info(f"Registered custom recovery handler for error type: {error_type}")

    def get_custom_handlers(self, error_type: str) -> List[CustomRecoveryHandler]:
        """Get all custom handlers for an error type."""
        return self._custom_handlers.get(error_type, [])

    async def try_custom_recovery(
        self,
        error_type: str,
        component: str,
        error: Exception,
        context: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        v192.0: Try custom recovery handlers for an error type.

        Calls all registered handlers for the error type in order.
        Stops and returns True as soon as one handler succeeds.

        Args:
            error_type: Identifier for the error type
            component: Name of the failed component
            error: The exception that occurred
            context: Optional additional context

        Returns:
            True if a handler successfully handled the error, False otherwise
        """
        handlers = self.get_custom_handlers(error_type)
        if not handlers:
            return False

        ctx = context or {}
        for handler in handlers:
            try:
                if await handler(component, error, ctx):
                    logger.info(f"Custom recovery handler succeeded for {error_type} on {component}")
                    return True
            except Exception as handler_error:
                logger.warning(
                    f"Custom recovery handler for {error_type} failed: {handler_error}"
                )

        return False


# =============================================================================
# Factory Function
# =============================================================================

def get_recovery_engine(registry: ComponentRegistry) -> RecoveryEngine:
    """
    Factory function for RecoveryEngine.

    Creates a RecoveryEngine with a default ErrorClassifier.
    This provides a consistent way to instantiate the recovery engine
    across the codebase.

    Args:
        registry: ComponentRegistry for component lookups

    Returns:
        RecoveryEngine instance configured with default error classifier
    """
    classifier = ErrorClassifier()
    return RecoveryEngine(registry, classifier)
