"""
Advanced Action Executor for JARVIS Autonomous System.

This module provides advanced action execution capabilities with multi-strategy
retry, circuit breaker pattern, rollback management, and comprehensive error recovery.

Key Features:
    - Multi-strategy retry with exponential backoff
    - Circuit breaker pattern for failure isolation
    - Rollback management for reversible actions
    - Execution context tracking
    - Async-first execution pipeline
    - Comprehensive error recovery

Environment Variables:
    JARVIS_EXECUTOR_MAX_RETRIES: Maximum retry attempts (default: 3)
    JARVIS_EXECUTOR_TIMEOUT: Default timeout in seconds (default: 30)
    JARVIS_EXECUTOR_CIRCUIT_THRESHOLD: Circuit breaker threshold (default: 5)
    JARVIS_EXECUTOR_CIRCUIT_RESET: Circuit reset time in seconds (default: 60)
    JARVIS_EXECUTOR_DRY_RUN: Enable dry-run mode (default: false)
"""

from __future__ import annotations

import asyncio
import logging
import os
import traceback
import weakref
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple

from backend.core.async_safety import LazyAsyncLock
from .action_registry import (
    ActionHandler,
    ActionMetadata,
    ActionRegistry,
    ActionRiskLevel,
    ActionType,
    get_action_registry,
)
from .permission_system import (
    PermissionContext,
    PermissionDecision,
    PermissionSystem,
    get_permission_system,
)
from .safety_validator import (
    SafetyCheckResult,
    SafetyValidator,
    ValidationResult,
    get_safety_validator,
)

logger = logging.getLogger(__name__)


# =============================================================================
# ENUMS
# =============================================================================


class ExecutionStatus(Enum):
    """Status of action execution."""

    PENDING = "pending"
    VALIDATING = "validating"
    PERMISSION_CHECK = "permission_check"
    EXECUTING = "executing"
    RETRYING = "retrying"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ROLLED_BACK = "rolled_back"
    CIRCUIT_OPEN = "circuit_open"
    PERMISSION_DENIED = "permission_denied"
    SAFETY_BLOCKED = "safety_blocked"


class RetryStrategy(Enum):
    """Retry strategies for failed actions."""

    NONE = "none"  # No retry
    IMMEDIATE = "immediate"  # Retry immediately
    LINEAR = "linear"  # Linear backoff
    EXPONENTIAL = "exponential"  # Exponential backoff
    ADAPTIVE = "adaptive"  # Adapt based on failure type


class CircuitBreakerState(Enum):
    """States for the circuit breaker."""

    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Blocking all executions
    HALF_OPEN = "half_open"  # Testing if recovery is possible


class FailureType(Enum):
    """Types of execution failures."""

    TIMEOUT = "timeout"
    VALIDATION_ERROR = "validation_error"
    PERMISSION_DENIED = "permission_denied"
    HANDLER_ERROR = "handler_error"
    RESOURCE_ERROR = "resource_error"
    NETWORK_ERROR = "network_error"
    UNKNOWN = "unknown"


# =============================================================================
# DATA CLASSES
# =============================================================================


@dataclass
class ExecutionContext:
    """Context for action execution."""

    action_type: ActionType
    metadata: ActionMetadata
    params: Dict[str, Any]
    request_id: str = ""
    request_source: str = "unknown"
    priority: int = 5
    dry_run: bool = False
    force: bool = False
    timeout_override: Optional[float] = None

    # Execution state
    attempt: int = 0
    max_retries: int = 3
    retry_strategy: RetryStrategy = RetryStrategy.EXPONENTIAL

    # Environment context
    screen_locked: bool = False
    focus_mode_active: bool = False
    meeting_in_progress: bool = False
    recent_failures: int = 0

    # Timestamps
    created_at: datetime = field(default_factory=datetime.now)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    @property
    def execution_time_ms(self) -> Optional[float]:
        """Calculate execution time in milliseconds."""
        if self.started_at and self.completed_at:
            return (self.completed_at - self.started_at).total_seconds() * 1000
        return None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "action_type": self.action_type.name,
            "params": self.params,
            "request_id": self.request_id,
            "attempt": self.attempt,
            "dry_run": self.dry_run,
            "execution_time_ms": self.execution_time_ms,
        }


@dataclass
class ExecutionResult:
    """Result of action execution."""

    context: ExecutionContext
    status: ExecutionStatus
    success: bool
    result_data: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    error_type: Optional[FailureType] = None
    traceback: Optional[str] = None

    # Validation results
    safety_result: Optional[SafetyCheckResult] = None
    permission_decision: Optional[PermissionDecision] = None

    # Execution metrics
    attempt_count: int = 1
    total_execution_time_ms: float = 0.0
    retry_delays_ms: List[float] = field(default_factory=list)

    # Rollback info
    rollback_available: bool = False
    rollback_data: Optional[Dict[str, Any]] = None
    rolled_back: bool = False

    # Timestamps
    completed_at: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "action_type": self.context.action_type.name,
            "status": self.status.value,
            "success": self.success,
            "result_data": self.result_data,
            "error": self.error,
            "error_type": self.error_type.value if self.error_type else None,
            "attempt_count": self.attempt_count,
            "total_execution_time_ms": self.total_execution_time_ms,
            "rollback_available": self.rollback_available,
            "completed_at": self.completed_at.isoformat(),
        }


@dataclass
class CircuitBreaker:
    """Circuit breaker for an action type."""

    action_type: ActionType
    state: CircuitBreakerState = CircuitBreakerState.CLOSED
    failure_count: int = 0
    success_count: int = 0
    last_failure: Optional[datetime] = None
    last_state_change: datetime = field(default_factory=datetime.now)
    threshold: int = 5
    reset_timeout_seconds: float = 60.0
    half_open_successes_required: int = 2

    def record_success(self) -> None:
        """Record a successful execution."""
        if self.state == CircuitBreakerState.HALF_OPEN:
            self.success_count += 1
            if self.success_count >= self.half_open_successes_required:
                self.state = CircuitBreakerState.CLOSED
                self.failure_count = 0
                self.success_count = 0
                self.last_state_change = datetime.now()
                logger.info(f"Circuit breaker CLOSED for {self.action_type.name}")

    def record_failure(self) -> None:
        """Record a failed execution."""
        self.failure_count += 1
        self.last_failure = datetime.now()

        if self.state == CircuitBreakerState.HALF_OPEN:
            # Failure in half-open state - back to open
            self.state = CircuitBreakerState.OPEN
            self.success_count = 0
            self.last_state_change = datetime.now()
            logger.warning(f"Circuit breaker OPEN for {self.action_type.name} (half-open failure)")

        elif self.failure_count >= self.threshold:
            self.state = CircuitBreakerState.OPEN
            self.last_state_change = datetime.now()
            logger.warning(f"Circuit breaker OPEN for {self.action_type.name} (threshold reached)")

    def can_execute(self) -> Tuple[bool, str]:
        """Check if execution is allowed."""
        if self.state == CircuitBreakerState.CLOSED:
            return True, ""

        if self.state == CircuitBreakerState.OPEN:
            # Check if reset timeout has passed
            time_since_open = (datetime.now() - self.last_state_change).total_seconds()
            if time_since_open >= self.reset_timeout_seconds:
                self.state = CircuitBreakerState.HALF_OPEN
                self.success_count = 0
                self.last_state_change = datetime.now()
                logger.info(f"Circuit breaker HALF-OPEN for {self.action_type.name}")
                return True, ""
            else:
                remaining = self.reset_timeout_seconds - time_since_open
                return False, f"Circuit open, retry in {remaining:.1f}s"

        # Half-open - allow limited executions
        return True, ""

    def reset(self) -> None:
        """Reset the circuit breaker."""
        self.state = CircuitBreakerState.CLOSED
        self.failure_count = 0
        self.success_count = 0
        self.last_failure = None
        self.last_state_change = datetime.now()


@dataclass
class RollbackEntry:
    """Entry for rollback stack."""

    action_type: ActionType
    execution_id: str
    rollback_handler: Callable[..., Awaitable[bool]]
    rollback_data: Dict[str, Any]
    created_at: datetime = field(default_factory=datetime.now)
    expires_at: Optional[datetime] = None

    @property
    def is_expired(self) -> bool:
        """Check if rollback has expired."""
        if self.expires_at is None:
            return False
        return datetime.now() > self.expires_at


@dataclass
class AdvancedExecutorConfig:
    """Configuration for the advanced executor."""

    # Execution settings
    default_timeout_seconds: float = 30.0
    max_retries: int = 3
    default_retry_strategy: RetryStrategy = RetryStrategy.EXPONENTIAL

    # Retry backoff
    initial_retry_delay_ms: float = 100.0
    max_retry_delay_ms: float = 10000.0
    backoff_multiplier: float = 2.0
    jitter_factor: float = 0.1

    # Circuit breaker
    circuit_breaker_threshold: int = 5
    circuit_breaker_reset_seconds: float = 60.0
    circuit_breaker_enabled: bool = True

    # Rollback
    rollback_enabled: bool = True
    rollback_expiry_minutes: int = 30
    max_rollback_stack: int = 50

    # Execution modes
    dry_run_mode: bool = False
    validation_enabled: bool = True
    permission_check_enabled: bool = True

    # Concurrency
    max_concurrent_executions: int = 10

    # History
    history_size: int = 1000

    @classmethod
    def from_env(cls) -> "AdvancedExecutorConfig":
        """Create configuration from environment variables."""
        return cls(
            default_timeout_seconds=float(os.getenv("JARVIS_EXECUTOR_TIMEOUT", "30")),
            max_retries=int(os.getenv("JARVIS_EXECUTOR_MAX_RETRIES", "3")),
            circuit_breaker_threshold=int(os.getenv("JARVIS_EXECUTOR_CIRCUIT_THRESHOLD", "5")),
            circuit_breaker_reset_seconds=float(os.getenv("JARVIS_EXECUTOR_CIRCUIT_RESET", "60")),
            circuit_breaker_enabled=os.getenv("JARVIS_EXECUTOR_CIRCUIT_ENABLED", "true").lower() == "true",
            dry_run_mode=os.getenv("JARVIS_EXECUTOR_DRY_RUN", "false").lower() == "true",
            validation_enabled=os.getenv("JARVIS_EXECUTOR_VALIDATION", "true").lower() == "true",
            permission_check_enabled=os.getenv("JARVIS_EXECUTOR_PERMISSION_CHECK", "true").lower() == "true",
            max_concurrent_executions=int(os.getenv("JARVIS_EXECUTOR_MAX_CONCURRENT", "10")),
        )


class RollbackManager:
    """Manages rollback operations for executed actions."""

    def __init__(self, max_stack_size: int = 50, expiry_minutes: int = 30):
        """Initialize the rollback manager."""
        self._stack: deque[RollbackEntry] = deque(maxlen=max_stack_size)
        self._expiry_minutes = expiry_minutes
        self._lock = asyncio.Lock()

    async def push(
        self,
        action_type: ActionType,
        execution_id: str,
        rollback_handler: Callable[..., Awaitable[bool]],
        rollback_data: Dict[str, Any]
    ) -> None:
        """Push a rollback entry onto the stack."""
        async with self._lock:
            entry = RollbackEntry(
                action_type=action_type,
                execution_id=execution_id,
                rollback_handler=rollback_handler,
                rollback_data=rollback_data,
                expires_at=datetime.now() + timedelta(minutes=self._expiry_minutes)
            )
            self._stack.append(entry)
            logger.debug(f"Rollback entry pushed for {action_type.name}")

    async def rollback_last(self) -> Tuple[bool, str]:
        """Rollback the last action."""
        async with self._lock:
            # Clean expired entries
            self._clean_expired()

            if not self._stack:
                return False, "No actions to rollback"

            entry = self._stack.pop()

            try:
                success = await entry.rollback_handler(**entry.rollback_data)
                if success:
                    logger.info(f"Rolled back action: {entry.action_type.name}")
                    return True, f"Rolled back {entry.action_type.name}"
                else:
                    return False, f"Rollback handler returned failure for {entry.action_type.name}"
            except Exception as e:
                logger.error(f"Rollback error for {entry.action_type.name}: {e}")
                return False, f"Rollback error: {str(e)}"

    async def rollback_by_id(self, execution_id: str) -> Tuple[bool, str]:
        """Rollback a specific execution by ID."""
        async with self._lock:
            self._clean_expired()

            # Find the entry
            entry = None
            for e in self._stack:
                if e.execution_id == execution_id:
                    entry = e
                    break

            if not entry:
                return False, f"No rollback available for execution {execution_id}"

            # Remove from stack
            self._stack = deque(
                [e for e in self._stack if e.execution_id != execution_id],
                maxlen=self._stack.maxlen
            )

            try:
                success = await entry.rollback_handler(**entry.rollback_data)
                if success:
                    logger.info(f"Rolled back execution: {execution_id}")
                    return True, f"Rolled back execution {execution_id}"
                else:
                    return False, f"Rollback handler returned failure"
            except Exception as e:
                logger.error(f"Rollback error for {execution_id}: {e}")
                return False, f"Rollback error: {str(e)}"

    def _clean_expired(self) -> None:
        """Remove expired entries from stack."""
        self._stack = deque(
            [e for e in self._stack if not e.is_expired],
            maxlen=self._stack.maxlen
        )

    @property
    def can_rollback(self) -> bool:
        """Check if rollback is available."""
        return len(self._stack) > 0

    @property
    def rollback_count(self) -> int:
        """Get count of available rollbacks."""
        return len(self._stack)


# =============================================================================
# ADVANCED ACTION EXECUTOR
# =============================================================================


class AdvancedActionExecutor:
    """
    Advanced executor with multi-strategy retry, circuit breaker, and rollback.

    This executor provides comprehensive error recovery and resilience features
    for autonomous action execution.
    """

    def __init__(self, config: Optional[AdvancedExecutorConfig] = None):
        """Initialize the advanced executor."""
        self.config = config or AdvancedExecutorConfig.from_env()

        # Dependencies (lazy loaded)
        self._registry: Optional[ActionRegistry] = None
        self._permission_system: Optional[PermissionSystem] = None
        self._safety_validator: Optional[SafetyValidator] = None

        # Circuit breakers per action type
        self._circuit_breakers: Dict[ActionType, CircuitBreaker] = {}

        # Rollback manager
        self._rollback_manager = RollbackManager(
            max_stack_size=self.config.max_rollback_stack,
            expiry_minutes=self.config.rollback_expiry_minutes
        )

        # Execution tracking
        self._execution_history: deque[ExecutionResult] = deque(maxlen=self.config.history_size)
        self._active_executions: Dict[str, ExecutionContext] = {}
        self._execution_semaphore = asyncio.Semaphore(self.config.max_concurrent_executions)

        # Statistics
        self._total_executions = 0
        self._successful_executions = 0
        self._failed_executions = 0
        self._retried_executions = 0

        # State
        self._is_running = False
        self._lock = asyncio.Lock()
        self._execution_counter = 0

    async def start(self) -> None:
        """Start the executor."""
        if self._is_running:
            return

        logger.info("Starting AdvancedActionExecutor...")

        # Load dependencies
        self._registry = get_action_registry()
        self._permission_system = get_permission_system()
        self._safety_validator = get_safety_validator()

        self._is_running = True
        logger.info(f"AdvancedActionExecutor started (dry_run={self.config.dry_run_mode})")

    async def stop(self) -> None:
        """Stop the executor."""
        if not self._is_running:
            return

        logger.info("Stopping AdvancedActionExecutor...")

        # Wait for active executions to complete
        if self._active_executions:
            logger.info(f"Waiting for {len(self._active_executions)} active executions...")
            await asyncio.sleep(1)  # Give brief time for completion

        self._is_running = False
        logger.info("AdvancedActionExecutor stopped")

    @property
    def is_running(self) -> bool:
        """Check if executor is running."""
        return self._is_running

    async def execute(
        self,
        action_type: ActionType,
        params: Dict[str, Any],
        dry_run: Optional[bool] = None,
        force: bool = False,
        timeout: Optional[float] = None,
        retry_strategy: Optional[RetryStrategy] = None,
        request_source: str = "unknown",
        context_overrides: Optional[Dict[str, Any]] = None
    ) -> ExecutionResult:
        """
        Execute an action with full error recovery and retry logic.

        Args:
            action_type: Type of action to execute
            params: Action parameters
            dry_run: Override dry-run mode
            force: Force execution (skip confirmations)
            timeout: Override timeout
            retry_strategy: Override retry strategy
            request_source: Source of the request
            context_overrides: Additional context

        Returns:
            ExecutionResult with outcome
        """
        # Generate execution ID
        self._execution_counter += 1
        execution_id = f"exec_{datetime.now().strftime('%Y%m%d%H%M%S')}_{self._execution_counter}"

        # Get action metadata
        if not self._registry:
            return self._create_error_result(
                action_type, params, execution_id,
                "Registry not initialized", FailureType.UNKNOWN
            )

        registered = self._registry.get_action(action_type)
        if not registered:
            return self._create_error_result(
                action_type, params, execution_id,
                f"Unknown action type: {action_type.name}", FailureType.VALIDATION_ERROR
            )

        metadata = registered.metadata

        # Create execution context
        context = ExecutionContext(
            action_type=action_type,
            metadata=metadata,
            params=params,
            request_id=execution_id,
            request_source=request_source,
            dry_run=dry_run if dry_run is not None else self.config.dry_run_mode,
            force=force,
            timeout_override=timeout,
            max_retries=metadata.max_retries if retry_strategy != RetryStrategy.NONE else 0,
            retry_strategy=retry_strategy or self.config.default_retry_strategy,
            **(context_overrides or {})
        )

        # Execute with semaphore to limit concurrency
        async with self._execution_semaphore:
            return await self._execute_with_retry(context, registered.handler)

    async def _execute_with_retry(
        self,
        context: ExecutionContext,
        handler: ActionHandler
    ) -> ExecutionResult:
        """Execute action with retry logic."""
        start_time = datetime.now()
        context.started_at = start_time
        retry_delays: List[float] = []

        self._total_executions += 1
        self._active_executions[context.request_id] = context

        try:
            while context.attempt <= context.max_retries:
                context.attempt += 1

                # Check circuit breaker
                if self.config.circuit_breaker_enabled:
                    circuit = self._get_circuit_breaker(context.action_type)
                    can_exec, reason = circuit.can_execute()
                    if not can_exec:
                        return ExecutionResult(
                            context=context,
                            status=ExecutionStatus.CIRCUIT_OPEN,
                            success=False,
                            error=reason,
                            error_type=FailureType.RESOURCE_ERROR,
                            attempt_count=context.attempt,
                            total_execution_time_ms=(datetime.now() - start_time).total_seconds() * 1000,
                            retry_delays_ms=retry_delays
                        )

                # Execute single attempt
                result = await self._execute_single_attempt(context, handler)

                if result.success:
                    # Update circuit breaker
                    if self.config.circuit_breaker_enabled:
                        self._get_circuit_breaker(context.action_type).record_success()

                    # Update statistics
                    self._successful_executions += 1
                    if context.attempt > 1:
                        self._retried_executions += 1

                    # Record in registry
                    if self._registry:
                        registered = self._registry.get_action(context.action_type)
                        if registered:
                            exec_time = (datetime.now() - start_time).total_seconds()
                            registered.record_execution(True, exec_time)

                    result.retry_delays_ms = retry_delays
                    result.total_execution_time_ms = (datetime.now() - start_time).total_seconds() * 1000
                    return result

                # Check if we should retry
                if context.attempt > context.max_retries:
                    break

                if not self._should_retry(result):
                    break

                # Calculate retry delay
                delay_ms = self._calculate_retry_delay(
                    context.attempt,
                    context.retry_strategy
                )
                retry_delays.append(delay_ms)

                logger.info(
                    f"Retrying {context.action_type.name} "
                    f"(attempt {context.attempt + 1}/{context.max_retries + 1}) "
                    f"after {delay_ms:.0f}ms"
                )

                await asyncio.sleep(delay_ms / 1000)

            # All retries exhausted
            if self.config.circuit_breaker_enabled:
                self._get_circuit_breaker(context.action_type).record_failure()

            self._failed_executions += 1

            # Record in registry
            if self._registry:
                registered = self._registry.get_action(context.action_type)
                if registered:
                    exec_time = (datetime.now() - start_time).total_seconds()
                    registered.record_execution(False, exec_time)

            result.attempt_count = context.attempt
            result.retry_delays_ms = retry_delays
            result.total_execution_time_ms = (datetime.now() - start_time).total_seconds() * 1000
            return result

        finally:
            context.completed_at = datetime.now()
            self._active_executions.pop(context.request_id, None)
            self._execution_history.append(result if 'result' in locals() else ExecutionResult(
                context=context,
                status=ExecutionStatus.FAILED,
                success=False,
                error="Execution interrupted"
            ))

    async def _execute_single_attempt(
        self,
        context: ExecutionContext,
        handler: ActionHandler
    ) -> ExecutionResult:
        """Execute a single attempt of an action."""

        # 1. Safety validation
        if self.config.validation_enabled and self._safety_validator:
            safety_result = await self._safety_validator.validate(
                context.action_type,
                context.metadata,
                context.params,
                {
                    "screen_locked": context.screen_locked,
                    "focus_mode_active": context.focus_mode_active,
                    "meeting_in_progress": context.meeting_in_progress,
                    "recent_failures": context.recent_failures,
                }
            )

            if not safety_result.passed:
                return ExecutionResult(
                    context=context,
                    status=ExecutionStatus.SAFETY_BLOCKED,
                    success=False,
                    error=f"Safety validation failed: {safety_result.blocked_by.message if safety_result.blocked_by else 'Unknown'}",
                    error_type=FailureType.VALIDATION_ERROR,
                    safety_result=safety_result,
                    attempt_count=context.attempt
                )

        # 2. Permission check
        if self.config.permission_check_enabled and self._permission_system:
            perm_context = PermissionContext(
                action_type=context.action_type,
                action_category=context.metadata.category,
                risk_level=context.metadata.risk_level,
                parameters=context.params,
                screen_locked=context.screen_locked,
                focus_mode_active=context.focus_mode_active,
                meeting_in_progress=context.meeting_in_progress,
                recent_failures=context.recent_failures,
                request_source=context.request_source
            )

            decision = await self._permission_system.check_permission(perm_context)

            if not decision.allowed:
                return ExecutionResult(
                    context=context,
                    status=ExecutionStatus.PERMISSION_DENIED,
                    success=False,
                    error=f"Permission denied: {decision.message}",
                    error_type=FailureType.PERMISSION_DENIED,
                    permission_decision=decision,
                    attempt_count=context.attempt
                )

            if decision.requires_confirmation and not context.force:
                return ExecutionResult(
                    context=context,
                    status=ExecutionStatus.PERMISSION_DENIED,
                    success=False,
                    error="Action requires user confirmation",
                    error_type=FailureType.PERMISSION_DENIED,
                    permission_decision=decision,
                    attempt_count=context.attempt
                )

        # 3. Execute handler
        timeout = context.timeout_override or context.metadata.timeout_seconds

        if context.dry_run:
            # Dry run - simulate success
            return ExecutionResult(
                context=context,
                status=ExecutionStatus.SUCCESS,
                success=True,
                result_data={
                    "dry_run": True,
                    "message": f"Would execute {context.action_type.name}",
                    "params": context.params
                },
                attempt_count=context.attempt
            )

        try:
            result_data = await asyncio.wait_for(
                handler(**context.params),
                timeout=timeout
            )

            # Check for rollback support
            rollback_available = (
                context.metadata.supports_rollback and
                self.config.rollback_enabled and
                result_data.get("rollback_available", False)
            )

            # Store rollback data if available
            if rollback_available and "rollback_data" in result_data:
                await self._rollback_manager.push(
                    action_type=context.action_type,
                    execution_id=context.request_id,
                    rollback_handler=self._create_rollback_handler(context.action_type),
                    rollback_data=result_data["rollback_data"]
                )

            return ExecutionResult(
                context=context,
                status=ExecutionStatus.SUCCESS,
                success=True,
                result_data=result_data,
                rollback_available=rollback_available,
                rollback_data=result_data.get("rollback_data"),
                attempt_count=context.attempt
            )

        except asyncio.TimeoutError:
            return ExecutionResult(
                context=context,
                status=ExecutionStatus.FAILED,
                success=False,
                error=f"Execution timed out after {timeout}s",
                error_type=FailureType.TIMEOUT,
                attempt_count=context.attempt
            )

        except Exception as e:
            logger.error(f"Handler error for {context.action_type.name}: {e}")
            return ExecutionResult(
                context=context,
                status=ExecutionStatus.FAILED,
                success=False,
                error=str(e),
                error_type=FailureType.HANDLER_ERROR,
                traceback=traceback.format_exc(),
                attempt_count=context.attempt
            )

    def _should_retry(self, result: ExecutionResult) -> bool:
        """Determine if an execution should be retried."""
        if result.success:
            return False

        # Don't retry certain error types
        non_retryable = {
            FailureType.PERMISSION_DENIED,
            FailureType.VALIDATION_ERROR,
        }

        if result.error_type in non_retryable:
            return False

        # Retry timeouts and handler errors
        return result.error_type in {
            FailureType.TIMEOUT,
            FailureType.HANDLER_ERROR,
            FailureType.NETWORK_ERROR,
            FailureType.RESOURCE_ERROR,
        }

    def _calculate_retry_delay(
        self,
        attempt: int,
        strategy: RetryStrategy
    ) -> float:
        """Calculate delay before next retry in milliseconds."""
        import random

        base = self.config.initial_retry_delay_ms

        if strategy == RetryStrategy.IMMEDIATE:
            delay = 0

        elif strategy == RetryStrategy.LINEAR:
            delay = base * attempt

        elif strategy == RetryStrategy.EXPONENTIAL:
            delay = base * (self.config.backoff_multiplier ** (attempt - 1))

        elif strategy == RetryStrategy.ADAPTIVE:
            # Start with exponential, cap early for user-facing actions
            delay = base * (self.config.backoff_multiplier ** (attempt - 1))
            delay = min(delay, 2000)  # Cap at 2 seconds for responsiveness

        else:
            delay = base

        # Apply jitter
        jitter = delay * self.config.jitter_factor * random.random()
        delay = delay + jitter

        # Cap at maximum
        return min(delay, self.config.max_retry_delay_ms)

    def _get_circuit_breaker(self, action_type: ActionType) -> CircuitBreaker:
        """Get or create circuit breaker for action type."""
        if action_type not in self._circuit_breakers:
            self._circuit_breakers[action_type] = CircuitBreaker(
                action_type=action_type,
                threshold=self.config.circuit_breaker_threshold,
                reset_timeout_seconds=self.config.circuit_breaker_reset_seconds
            )
        return self._circuit_breakers[action_type]

    def _create_rollback_handler(
        self,
        action_type: ActionType
    ) -> Callable[..., Awaitable[bool]]:
        """Create a rollback handler for an action type."""
        async def rollback_handler(**rollback_data) -> bool:
            # Generic rollback - specific implementations can be added per action type
            logger.info(f"Executing rollback for {action_type.name}")
            # Rollback logic would go here
            return True

        return rollback_handler

    def _create_error_result(
        self,
        action_type: ActionType,
        params: Dict[str, Any],
        execution_id: str,
        error: str,
        error_type: FailureType
    ) -> ExecutionResult:
        """Create an error result."""
        return ExecutionResult(
            context=ExecutionContext(
                action_type=action_type,
                metadata=ActionMetadata(
                    action_type=action_type,
                    name="unknown",
                    description="",
                    category=ActionRiskLevel.MINIMAL,  # type: ignore
                    risk_level=ActionRiskLevel.MINIMAL
                ),
                params=params,
                request_id=execution_id
            ),
            status=ExecutionStatus.FAILED,
            success=False,
            error=error,
            error_type=error_type
        )

    # =========================================================================
    # Rollback Operations
    # =========================================================================

    async def rollback_last(self) -> Tuple[bool, str]:
        """Rollback the last executed action."""
        return await self._rollback_manager.rollback_last()

    async def rollback_execution(self, execution_id: str) -> Tuple[bool, str]:
        """Rollback a specific execution."""
        return await self._rollback_manager.rollback_by_id(execution_id)

    @property
    def can_rollback(self) -> bool:
        """Check if rollback is available."""
        return self._rollback_manager.can_rollback

    @property
    def rollback_count(self) -> int:
        """Get count of available rollbacks."""
        return self._rollback_manager.rollback_count

    # =========================================================================
    # Circuit Breaker Management
    # =========================================================================

    def reset_circuit_breaker(self, action_type: ActionType) -> None:
        """Reset circuit breaker for an action type."""
        if action_type in self._circuit_breakers:
            self._circuit_breakers[action_type].reset()
            logger.info(f"Reset circuit breaker for {action_type.name}")

    def reset_all_circuit_breakers(self) -> None:
        """Reset all circuit breakers."""
        for action_type, circuit in self._circuit_breakers.items():
            circuit.reset()
        logger.info("Reset all circuit breakers")

    def get_circuit_breaker_status(self) -> Dict[str, Dict[str, Any]]:
        """Get status of all circuit breakers."""
        return {
            action_type.name: {
                "state": circuit.state.value,
                "failure_count": circuit.failure_count,
                "last_failure": circuit.last_failure.isoformat() if circuit.last_failure else None,
            }
            for action_type, circuit in self._circuit_breakers.items()
        }

    # =========================================================================
    # Statistics
    # =========================================================================

    def get_statistics(self) -> Dict[str, Any]:
        """Get executor statistics."""
        return {
            "is_running": self._is_running,
            "total_executions": self._total_executions,
            "successful_executions": self._successful_executions,
            "failed_executions": self._failed_executions,
            "retried_executions": self._retried_executions,
            "success_rate": self._successful_executions / max(1, self._total_executions),
            "retry_rate": self._retried_executions / max(1, self._total_executions),
            "active_executions": len(self._active_executions),
            "history_size": len(self._execution_history),
            "circuit_breakers_open": sum(
                1 for cb in self._circuit_breakers.values()
                if cb.state == CircuitBreakerState.OPEN
            ),
            "rollback_available": self._rollback_manager.rollback_count,
        }

    def get_recent_executions(
        self,
        limit: int = 50,
        status: Optional[ExecutionStatus] = None,
        action_type: Optional[ActionType] = None
    ) -> List[ExecutionResult]:
        """Get recent execution results."""
        results = list(self._execution_history)

        if status:
            results = [r for r in results if r.status == status]

        if action_type:
            results = [r for r in results if r.context.action_type == action_type]

        return results[-limit:]


# =============================================================================
# SINGLETON MANAGEMENT
# =============================================================================


_executor_instance: Optional[AdvancedActionExecutor] = None
_executor_lock = LazyAsyncLock()  # v100.1: Lazy initialization to avoid "no running event loop" error


def get_advanced_executor() -> AdvancedActionExecutor:
    """Get the global advanced executor instance."""
    global _executor_instance
    if _executor_instance is None:
        _executor_instance = AdvancedActionExecutor()
    return _executor_instance


async def start_advanced_executor() -> AdvancedActionExecutor:
    """Start the global advanced executor."""
    async with _executor_lock:
        executor = get_advanced_executor()
        if not executor.is_running:
            await executor.start()
        return executor


async def stop_advanced_executor() -> None:
    """Stop the global advanced executor."""
    async with _executor_lock:
        global _executor_instance
        if _executor_instance and _executor_instance.is_running:
            await _executor_instance.stop()
