"""
Error Recovery Orchestrator
===========================

Provides systematic error handling for autonomous tasks:
- Error classification (transient, permanent, recoverable)
- Component reset strategies
- Graceful degradation logic
- Adaptive retry with exponential backoff

v1.0: Initial implementation with error classification and recovery strategies.

Author: JARVIS AI System
"""

import asyncio
import logging
import os
import time
import traceback
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Tuple, Type

# =============================================================================
# Configuration
# =============================================================================


@dataclass
class ErrorRecoveryConfig:
    """Configuration for the Error Recovery Orchestrator."""

    # Retry settings
    max_retries: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_ERROR_MAX_RETRIES", "3"))
    )
    initial_backoff: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_ERROR_INITIAL_BACKOFF", "1.0"))
    )
    max_backoff: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_ERROR_MAX_BACKOFF", "60.0"))
    )
    backoff_multiplier: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_ERROR_BACKOFF_MULTIPLIER", "2.0"))
    )

    # Degradation settings
    graceful_degradation_enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_GRACEFUL_DEGRADATION", "true").lower() == "true"
    )
    degradation_threshold: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_DEGRADATION_THRESHOLD", "5"))
    )

    # Circuit breaker settings
    circuit_breaker_enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_CIRCUIT_BREAKER", "true").lower() == "true"
    )
    circuit_breaker_threshold: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_CIRCUIT_THRESHOLD", "5"))
    )
    circuit_breaker_reset_time: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_CIRCUIT_RESET_TIME", "60.0"))
    )


# =============================================================================
# Error Classification
# =============================================================================


class ErrorType(Enum):
    """Classification of error types."""

    TRANSIENT = auto()  # Temporary, retry likely to succeed
    RECOVERABLE = auto()  # Can be fixed with specific action
    PERMANENT = auto()  # Cannot be recovered automatically
    UNKNOWN = auto()  # Unknown error type


class ErrorSeverity(Enum):
    """Severity levels for errors."""

    LOW = 1  # Minor issue, continue with degraded functionality
    MEDIUM = 2  # Moderate issue, retry or fallback needed
    HIGH = 3  # Significant issue, user notification recommended
    CRITICAL = 4  # System failure, immediate action required


class RecoveryAction(Enum):
    """Possible recovery actions."""

    RETRY = auto()  # Retry the operation
    RETRY_WITH_BACKOFF = auto()  # Retry with exponential backoff
    RESET_COMPONENT = auto()  # Reset the failing component
    FALLBACK = auto()  # Use fallback method
    DEGRADE = auto()  # Continue with reduced functionality
    ABORT = auto()  # Abort the operation
    ESCALATE = auto()  # Escalate to user/admin


@dataclass
class ClassifiedError:
    """A classified error with recovery information."""

    original_exception: Exception
    error_type: ErrorType
    severity: ErrorSeverity
    recommended_action: RecoveryAction
    component: str
    context: Dict[str, Any]
    retry_count: int
    timestamp: float = field(default_factory=time.time)
    stack_trace: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "error_type": self.error_type.name,
            "severity": self.severity.name,
            "recommended_action": self.recommended_action.name,
            "component": self.component,
            "message": str(self.original_exception),
            "retry_count": self.retry_count,
            "timestamp": self.timestamp,
        }


@dataclass
class RecoveryResult:
    """Result of a recovery attempt."""

    success: bool
    action_taken: RecoveryAction
    retry_count: int
    result: Any
    error: Optional[str] = None
    degraded: bool = False
    duration: float = 0.0


# =============================================================================
# Error Patterns
# =============================================================================


# Known transient error patterns
TRANSIENT_PATTERNS = [
    "timeout",
    "connection reset",
    "temporarily unavailable",
    "rate limit",
    "too many requests",
    "service unavailable",
    "503",
    "429",
    "connection refused",
    "network unreachable",
]

# Known recoverable error patterns
RECOVERABLE_PATTERNS = [
    "authentication",
    "authorization",
    "permission denied",
    "not found",
    "invalid state",
    "stale",
]

# Known permanent error patterns
PERMANENT_PATTERNS = [
    "invalid argument",
    "unsupported",
    "not implemented",
    "fatal",
    "corruption",
]


# =============================================================================
# Error Recovery Orchestrator
# =============================================================================


class ErrorRecoveryOrchestrator:
    """
    Orchestrates error recovery for autonomous tasks.

    Features:
    - Automatic error classification
    - Exponential backoff retry
    - Circuit breaker pattern
    - Graceful degradation
    - Component reset strategies
    """

    def __init__(
        self,
        config: Optional[ErrorRecoveryConfig] = None,
        logger: Optional[logging.Logger] = None,
    ):
        """Initialize the error recovery orchestrator."""
        self.config = config or ErrorRecoveryConfig()
        self.logger = logger or logging.getLogger(__name__)

        # Circuit breaker state per component
        self._circuit_state: Dict[str, Dict[str, Any]] = {}

        # Recovery handlers per component
        self._recovery_handlers: Dict[str, Callable] = {}
        self._reset_handlers: Dict[str, Callable] = {}
        self._fallback_handlers: Dict[str, Callable] = {}

        # Error history
        self._error_history: List[ClassifiedError] = []
        self._max_history = 100

        # Statistics
        self._stats = {
            "total_errors": 0,
            "transient_errors": 0,
            "recoverable_errors": 0,
            "permanent_errors": 0,
            "successful_recoveries": 0,
            "failed_recoveries": 0,
            "circuit_breaks": 0,
            "degradations": 0,
        }

        self._initialized = False

    async def initialize(self) -> bool:
        """Initialize the orchestrator."""
        if self._initialized:
            return True

        try:
            self.logger.info("[ErrorRecovery] Initializing Error Recovery Orchestrator...")
            self._initialized = True
            self.logger.info("[ErrorRecovery] ✓ Initialized")
            return True

        except Exception as e:
            self.logger.error(f"[ErrorRecovery] Initialization failed: {e}")
            return False

    async def shutdown(self) -> None:
        """Shutdown the orchestrator."""
        if not self._initialized:
            return

        self.logger.info("[ErrorRecovery] Shutting down...")
        self._error_history.clear()
        self._circuit_state.clear()
        self._initialized = False
        self.logger.info("[ErrorRecovery] ✓ Shutdown complete")

    # =========================================================================
    # Error Classification
    # =========================================================================

    def classify_error(
        self,
        exception: Exception,
        component: str = "unknown",
        context: Optional[Dict[str, Any]] = None,
        retry_count: int = 0,
    ) -> ClassifiedError:
        """Classify an error and determine recovery action."""
        error_msg = str(exception).lower()
        error_type = ErrorType.UNKNOWN
        severity = ErrorSeverity.MEDIUM
        action = RecoveryAction.ABORT

        # Check for transient errors
        if any(pattern in error_msg for pattern in TRANSIENT_PATTERNS):
            error_type = ErrorType.TRANSIENT
            severity = ErrorSeverity.LOW
            if retry_count < self.config.max_retries:
                action = RecoveryAction.RETRY_WITH_BACKOFF
            else:
                action = RecoveryAction.FALLBACK

        # Check for recoverable errors
        elif any(pattern in error_msg for pattern in RECOVERABLE_PATTERNS):
            error_type = ErrorType.RECOVERABLE
            severity = ErrorSeverity.MEDIUM
            action = RecoveryAction.RESET_COMPONENT

        # Check for permanent errors
        elif any(pattern in error_msg for pattern in PERMANENT_PATTERNS):
            error_type = ErrorType.PERMANENT
            severity = ErrorSeverity.HIGH
            action = RecoveryAction.ABORT

        # Default handling
        else:
            # Try to infer from exception type
            if isinstance(exception, (asyncio.TimeoutError, TimeoutError)):
                error_type = ErrorType.TRANSIENT
                action = RecoveryAction.RETRY_WITH_BACKOFF
            elif isinstance(exception, (ConnectionError, OSError)):
                error_type = ErrorType.TRANSIENT
                action = RecoveryAction.RETRY_WITH_BACKOFF
            elif isinstance(exception, (ValueError, TypeError)):
                error_type = ErrorType.PERMANENT
                action = RecoveryAction.ABORT

        # Check circuit breaker
        if self._is_circuit_open(component):
            action = RecoveryAction.FALLBACK
            severity = ErrorSeverity.HIGH

        # Consider graceful degradation
        if (
            self.config.graceful_degradation_enabled
            and retry_count >= self.config.degradation_threshold
        ):
            action = RecoveryAction.DEGRADE

        classified = ClassifiedError(
            original_exception=exception,
            error_type=error_type,
            severity=severity,
            recommended_action=action,
            component=component,
            context=context or {},
            retry_count=retry_count,
            stack_trace=traceback.format_exc(),
        )

        # Update stats
        self._stats["total_errors"] += 1
        if error_type == ErrorType.TRANSIENT:
            self._stats["transient_errors"] += 1
        elif error_type == ErrorType.RECOVERABLE:
            self._stats["recoverable_errors"] += 1
        elif error_type == ErrorType.PERMANENT:
            self._stats["permanent_errors"] += 1

        # Store in history
        self._error_history.append(classified)
        if len(self._error_history) > self._max_history:
            self._error_history.pop(0)

        return classified

    # =========================================================================
    # Recovery Execution
    # =========================================================================

    async def execute_with_recovery(
        self,
        operation: Callable,
        component: str,
        *args,
        fallback: Optional[Callable] = None,
        context: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> RecoveryResult:
        """
        Execute an operation with automatic error recovery.

        Args:
            operation: Async function to execute
            component: Component identifier
            fallback: Optional fallback function
            context: Additional context for error handling

        Returns:
            RecoveryResult with outcome
        """
        retry_count = 0
        backoff = self.config.initial_backoff
        start_time = time.time()

        iteration_timeout = float(os.getenv("TIMEOUT_ERROR_RECOVERY_ITERATION", "60.0"))
        while True:
            try:
                # Check circuit breaker
                if self._is_circuit_open(component):
                    self.logger.warning(f"[ErrorRecovery] Circuit open for {component}, using fallback")
                    if fallback:
                        result = await fallback(*args, **kwargs)
                        return RecoveryResult(
                            success=True,
                            action_taken=RecoveryAction.FALLBACK,
                            retry_count=retry_count,
                            result=result,
                            degraded=True,
                            duration=time.time() - start_time,
                        )
                    return RecoveryResult(
                        success=False,
                        action_taken=RecoveryAction.ABORT,
                        retry_count=retry_count,
                        result=None,
                        error="Circuit breaker open",
                        duration=time.time() - start_time,
                    )

                # Execute operation with timeout
                result = await asyncio.wait_for(
                    operation(*args, **kwargs),
                    timeout=iteration_timeout
                )

                # Success - reset circuit breaker
                self._record_success(component)
                self._stats["successful_recoveries"] += 1

                return RecoveryResult(
                    success=True,
                    action_taken=RecoveryAction.RETRY if retry_count > 0 else RecoveryAction.RETRY,
                    retry_count=retry_count,
                    result=result,
                    duration=time.time() - start_time,
                )

            except Exception as e:
                # Classify error
                classified = self.classify_error(
                    e, component, context, retry_count
                )

                self.logger.warning(
                    f"[ErrorRecovery] Error in {component}: {e} "
                    f"(type={classified.error_type.name}, action={classified.recommended_action.name})"
                )

                # Record failure for circuit breaker
                self._record_failure(component)

                # Handle based on recommended action
                if classified.recommended_action == RecoveryAction.ABORT:
                    self._stats["failed_recoveries"] += 1
                    return RecoveryResult(
                        success=False,
                        action_taken=RecoveryAction.ABORT,
                        retry_count=retry_count,
                        result=None,
                        error=str(e),
                        duration=time.time() - start_time,
                    )

                elif classified.recommended_action == RecoveryAction.RETRY_WITH_BACKOFF:
                    retry_count += 1
                    if retry_count > self.config.max_retries:
                        # Try fallback
                        if fallback:
                            try:
                                result = await fallback(*args, **kwargs)
                                self._stats["successful_recoveries"] += 1
                                return RecoveryResult(
                                    success=True,
                                    action_taken=RecoveryAction.FALLBACK,
                                    retry_count=retry_count,
                                    result=result,
                                    degraded=True,
                                    duration=time.time() - start_time,
                                )
                            except Exception as fb_e:
                                self.logger.error(f"[ErrorRecovery] Fallback failed: {fb_e}")

                        self._stats["failed_recoveries"] += 1
                        return RecoveryResult(
                            success=False,
                            action_taken=RecoveryAction.ABORT,
                            retry_count=retry_count,
                            result=None,
                            error=f"Max retries exceeded: {e}",
                            duration=time.time() - start_time,
                        )

                    # Wait with backoff
                    self.logger.info(
                        f"[ErrorRecovery] Retry {retry_count}/{self.config.max_retries} "
                        f"after {backoff:.1f}s backoff"
                    )
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * self.config.backoff_multiplier, self.config.max_backoff)
                    continue

                elif classified.recommended_action == RecoveryAction.RESET_COMPONENT:
                    # Try to reset component
                    if component in self._reset_handlers:
                        try:
                            await self._reset_handlers[component]()
                            retry_count += 1
                            continue
                        except Exception as reset_e:
                            self.logger.error(f"[ErrorRecovery] Reset failed: {reset_e}")

                    # Fall through to fallback
                    if fallback:
                        try:
                            result = await fallback(*args, **kwargs)
                            return RecoveryResult(
                                success=True,
                                action_taken=RecoveryAction.FALLBACK,
                                retry_count=retry_count,
                                result=result,
                                degraded=True,
                                duration=time.time() - start_time,
                            )
                        except Exception:
                            pass

                    self._stats["failed_recoveries"] += 1
                    return RecoveryResult(
                        success=False,
                        action_taken=RecoveryAction.ABORT,
                        retry_count=retry_count,
                        result=None,
                        error=str(e),
                        duration=time.time() - start_time,
                    )

                elif classified.recommended_action == RecoveryAction.DEGRADE:
                    self._stats["degradations"] += 1
                    # Return partial success with degraded flag
                    return RecoveryResult(
                        success=True,
                        action_taken=RecoveryAction.DEGRADE,
                        retry_count=retry_count,
                        result=None,
                        degraded=True,
                        duration=time.time() - start_time,
                    )

                elif classified.recommended_action == RecoveryAction.FALLBACK:
                    if fallback:
                        try:
                            result = await fallback(*args, **kwargs)
                            self._stats["successful_recoveries"] += 1
                            return RecoveryResult(
                                success=True,
                                action_taken=RecoveryAction.FALLBACK,
                                retry_count=retry_count,
                                result=result,
                                degraded=True,
                                duration=time.time() - start_time,
                            )
                        except Exception as fb_e:
                            self.logger.error(f"[ErrorRecovery] Fallback failed: {fb_e}")

                    self._stats["failed_recoveries"] += 1
                    return RecoveryResult(
                        success=False,
                        action_taken=RecoveryAction.ABORT,
                        retry_count=retry_count,
                        result=None,
                        error=str(e),
                        duration=time.time() - start_time,
                    )

    # =========================================================================
    # Circuit Breaker
    # =========================================================================

    def _is_circuit_open(self, component: str) -> bool:
        """Check if circuit breaker is open for a component."""
        if not self.config.circuit_breaker_enabled:
            return False

        state = self._circuit_state.get(component)
        if not state:
            return False

        if state.get("open", False):
            # Check if reset time has passed
            if time.time() - state.get("opened_at", 0) > self.config.circuit_breaker_reset_time:
                state["open"] = False
                state["failures"] = 0
                return False
            return True

        return False

    def _record_failure(self, component: str) -> None:
        """Record a failure for circuit breaker."""
        if not self.config.circuit_breaker_enabled:
            return

        if component not in self._circuit_state:
            self._circuit_state[component] = {"failures": 0, "open": False}

        state = self._circuit_state[component]
        state["failures"] = state.get("failures", 0) + 1

        if state["failures"] >= self.config.circuit_breaker_threshold:
            state["open"] = True
            state["opened_at"] = time.time()
            self._stats["circuit_breaks"] += 1
            self.logger.warning(f"[ErrorRecovery] Circuit breaker OPEN for {component}")

    def _record_success(self, component: str) -> None:
        """Record a success, resetting circuit breaker state."""
        if component in self._circuit_state:
            self._circuit_state[component] = {"failures": 0, "open": False}

    # =========================================================================
    # Handler Registration
    # =========================================================================

    def register_reset_handler(self, component: str, handler: Callable) -> None:
        """Register a reset handler for a component."""
        self._reset_handlers[component] = handler

    def register_fallback_handler(self, component: str, handler: Callable) -> None:
        """Register a fallback handler for a component."""
        self._fallback_handlers[component] = handler

    # =========================================================================
    # Statistics
    # =========================================================================

    def get_stats(self) -> Dict[str, Any]:
        """Get error recovery statistics."""
        return {
            **self._stats,
            "circuit_states": {
                comp: {"open": state.get("open", False), "failures": state.get("failures", 0)}
                for comp, state in self._circuit_state.items()
            },
            "recent_errors": [e.to_dict() for e in self._error_history[-10:]],
        }

    def get_error_history(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Get recent error history."""
        return [e.to_dict() for e in self._error_history[-limit:]]

    @property
    def is_ready(self) -> bool:
        """Check if orchestrator is ready."""
        return self._initialized


# =============================================================================
# Module-level Singleton Access
# =============================================================================

_orchestrator_instance: Optional[ErrorRecoveryOrchestrator] = None


def get_error_orchestrator() -> Optional[ErrorRecoveryOrchestrator]:
    """Get the global error recovery orchestrator."""
    return _orchestrator_instance


def set_error_orchestrator(orchestrator: ErrorRecoveryOrchestrator) -> None:
    """Set the global error recovery orchestrator."""
    global _orchestrator_instance
    _orchestrator_instance = orchestrator


async def start_error_orchestrator(
    config: Optional[ErrorRecoveryConfig] = None,
) -> ErrorRecoveryOrchestrator:
    """Start and initialize the error recovery orchestrator."""
    global _orchestrator_instance

    if _orchestrator_instance is not None:
        return _orchestrator_instance

    orchestrator = ErrorRecoveryOrchestrator(config=config)
    await orchestrator.initialize()
    _orchestrator_instance = orchestrator

    return orchestrator


async def stop_error_orchestrator() -> None:
    """Stop the global error recovery orchestrator."""
    global _orchestrator_instance

    if _orchestrator_instance is not None:
        await _orchestrator_instance.shutdown()
        _orchestrator_instance = None
