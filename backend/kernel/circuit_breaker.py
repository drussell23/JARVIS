"""
JARVIS Kernel Circuit Breaker v1.0.0
=====================================

Enterprise-grade circuit breaker implementation for the JARVIS kernel.
Prevents cascade failures by detecting unhealthy dependencies and failing fast.

This module provides circuit breakers that can be used for:
1. External service calls (JARVIS Prime, Reactor Core, GCP)
2. Internal component health (ML models, Docker, databases)
3. Browser operations (Chrome GPU crashes)

Circuit Breaker States:
    CLOSED   - Normal operation, requests allowed
    OPEN     - Failing fast, requests blocked
    HALF_OPEN - Testing recovery, limited requests allowed

The circuit breaker pattern prevents:
- Cascade failures when a dependency is down
- Resource exhaustion from retry storms
- Thundering herd problems on recovery

Usage:
    from backend.kernel.circuit_breaker import CircuitBreaker, CircuitBreakerConfig
    
    # Create a circuit breaker
    breaker = CircuitBreaker(
        name="jarvis-prime",
        config=CircuitBreakerConfig(failure_threshold=5)
    )
    
    # Use with async operations
    if await breaker.can_execute():
        try:
            result = await call_jarvis_prime()
            await breaker.record_success()
        except Exception as e:
            await breaker.record_failure(str(e))
    else:
        # Use fallback
        result = fallback_response()

Author: JARVIS AI System
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, TypeVar, Generic

logger = logging.getLogger(__name__)

T = TypeVar('T')


# =============================================================================
# CIRCUIT BREAKER STATE
# =============================================================================

class CircuitBreakerState(Enum):
    """Circuit breaker states."""
    CLOSED = "closed"       # Normal operation
    OPEN = "open"           # Blocking requests
    HALF_OPEN = "half_open" # Testing recovery


# =============================================================================
# CONFIGURATION
# =============================================================================

@dataclass
class CircuitBreakerConfig:
    """
    Configuration for circuit breaker behavior.
    
    All values can be overridden via environment variables.
    """
    # Thresholds
    failure_threshold: int = field(
        default_factory=lambda: int(os.getenv("CIRCUIT_FAILURE_THRESHOLD", "5"))
    )
    success_threshold: int = field(
        default_factory=lambda: int(os.getenv("CIRCUIT_SUCCESS_THRESHOLD", "2"))
    )
    
    # Timing
    recovery_timeout_seconds: float = field(
        default_factory=lambda: float(os.getenv("CIRCUIT_RECOVERY_TIMEOUT", "30.0"))
    )
    
    # Half-open behavior
    half_open_max_requests: int = field(
        default_factory=lambda: int(os.getenv("CIRCUIT_HALF_OPEN_MAX", "1"))
    )
    
    # Optional: name for logging
    name: str = "default"


# =============================================================================
# CIRCUIT BREAKER IMPLEMENTATION
# =============================================================================

class CircuitBreaker:
    """
    Atomic circuit breaker with thundering herd prevention.
    
    Key features:
    1. Atomic state transitions prevent race conditions
    2. Limited test requests in HALF_OPEN prevents thundering herd
    3. Configurable thresholds for different use cases
    4. Full observability via state introspection
    
    State Machine:
        CLOSED --[failures >= threshold]--> OPEN
        OPEN --[timeout elapsed]--> HALF_OPEN (only 1 request wins!)
        HALF_OPEN --[success >= threshold]--> CLOSED
        HALF_OPEN --[any failure]--> OPEN
    """
    
    def __init__(
        self,
        name: str = "default",
        config: Optional[CircuitBreakerConfig] = None,
        # Backward-compatible parameters (match original inline implementation)
        failure_threshold: Optional[int] = None,
        recovery_timeout: Optional[float] = None,
        half_open_max_calls: Optional[int] = None,
    ):
        """
        Initialize circuit breaker.
        
        Supports both the new config-based API and backward-compatible
        parameter-based API for drop-in replacement.
        
        Args:
            name: Name for logging and identification
            config: Configuration (uses defaults if None)
            failure_threshold: Number of failures before opening (backward compat)
            recovery_timeout: Seconds before attempting recovery (backward compat)
            half_open_max_calls: Max requests in half-open state (backward compat)
        """
        self._name = name
        
        # If backward-compatible parameters are provided, build config from them
        if failure_threshold is not None or recovery_timeout is not None:
            self._config = CircuitBreakerConfig(
                name=name,
                failure_threshold=failure_threshold if failure_threshold is not None else 5,
                recovery_timeout_seconds=recovery_timeout if recovery_timeout is not None else 30.0,
                half_open_max_requests=half_open_max_calls if half_open_max_calls is not None else 3,
            )
        else:
            self._config = config or CircuitBreakerConfig(name=name)
        self._config.name = name
        
        # State
        self._state = CircuitBreakerState.CLOSED
        self._state_lock = asyncio.Lock()
        
        # Counters
        self._failure_count = 0
        self._success_count = 0
        self._half_open_request_count = 0
        
        # Timestamps
        self._last_failure_time: Optional[datetime] = None
        self._last_success_time: Optional[datetime] = None
        self._last_state_change: datetime = datetime.now()
        
        # History
        self._failure_history: List[Dict[str, Any]] = []
        
        # Expose config values as attributes for backward compatibility
        self.failure_threshold = self._config.failure_threshold
        self.recovery_timeout = self._config.recovery_timeout_seconds
        self.half_open_max_calls = self._config.half_open_max_requests
        
        logger.debug(
            f"[CircuitBreaker:{name}] Initialized with "
            f"failure_threshold={self._config.failure_threshold}"
        )
    
    @property
    def name(self) -> str:
        return self._name
    
    @property
    def state(self) -> CircuitBreakerState:
        return self._state
    
    @property
    def is_closed(self) -> bool:
        return self._state == CircuitBreakerState.CLOSED
    
    @property
    def is_open(self) -> bool:
        return self._state == CircuitBreakerState.OPEN
    
    @property
    def is_half_open(self) -> bool:
        return self._state == CircuitBreakerState.HALF_OPEN
    
    async def can_execute(self) -> bool:
        """
        Check if requests are allowed through the circuit.
        
        Returns True if:
        - CLOSED: always allowed
        - HALF_OPEN: allowed if under max test requests
        - OPEN: allowed if recovery timeout elapsed (transitions to HALF_OPEN)
        
        This is the primary entry point for circuit breaker usage.
        """
        async with self._state_lock:
            if self._state == CircuitBreakerState.CLOSED:
                return True
            
            if self._state == CircuitBreakerState.OPEN:
                # Check if recovery timeout has elapsed
                if self._last_failure_time:
                    elapsed = (datetime.now() - self._last_failure_time).total_seconds()
                    if elapsed >= self._config.recovery_timeout_seconds:
                        # Transition to HALF_OPEN
                        self._state = CircuitBreakerState.HALF_OPEN
                        self._half_open_request_count = 1  # Count this request
                        self._success_count = 0
                        self._last_state_change = datetime.now()
                        
                        logger.info(
                            f"[CircuitBreaker:{self._name}] OPEN -> HALF_OPEN "
                            f"(elapsed={elapsed:.1f}s)"
                        )
                        return True
                return False
            
            if self._state == CircuitBreakerState.HALF_OPEN:
                # Limit test requests to prevent thundering herd
                if self._half_open_request_count >= self._config.half_open_max_requests:
                    logger.debug(
                        f"[CircuitBreaker:{self._name}] HALF_OPEN limit reached "
                        f"({self._half_open_request_count}/{self._config.half_open_max_requests})"
                    )
                    return False
                self._half_open_request_count += 1
                return True
            
            return False
    
    async def record_success(self) -> None:
        """
        Record a successful operation.
        
        In HALF_OPEN: enough successes close the circuit.
        In CLOSED: resets failure count.
        """
        async with self._state_lock:
            self._success_count += 1
            self._last_success_time = datetime.now()
            
            if self._state == CircuitBreakerState.HALF_OPEN:
                # Check if enough successes to close
                if self._success_count >= self._config.success_threshold:
                    self._state = CircuitBreakerState.CLOSED
                    self._failure_count = 0
                    self._success_count = 0
                    self._last_state_change = datetime.now()
                    
                    logger.info(
                        f"[CircuitBreaker:{self._name}] HALF_OPEN -> CLOSED "
                        f"(successes={self._config.success_threshold})"
                    )
            
            elif self._state == CircuitBreakerState.CLOSED:
                # Success resets failure count
                self._failure_count = 0
    
    async def record_failure(self, error: str = "") -> None:
        """
        Record a failed operation.
        
        In HALF_OPEN: any failure immediately reopens the circuit.
        In CLOSED: failures accumulate toward threshold.
        """
        async with self._state_lock:
            self._failure_count += 1
            self._last_failure_time = datetime.now()
            
            # Record in history
            self._failure_history.append({
                "time": datetime.now().isoformat(),
                "error": error[:200],  # Truncate long errors
                "state": self._state.value,
            })
            
            # Keep history bounded
            if len(self._failure_history) > 100:
                self._failure_history = self._failure_history[-50:]
            
            if self._state == CircuitBreakerState.HALF_OPEN:
                # Any failure in HALF_OPEN immediately opens circuit
                self._state = CircuitBreakerState.OPEN
                self._success_count = 0
                self._last_state_change = datetime.now()
                
                logger.warning(
                    f"[CircuitBreaker:{self._name}] HALF_OPEN -> OPEN "
                    f"(failure: {error[:50]})"
                )
            
            elif self._state == CircuitBreakerState.CLOSED:
                # Check if threshold reached
                if self._failure_count >= self._config.failure_threshold:
                    self._state = CircuitBreakerState.OPEN
                    self._last_state_change = datetime.now()
                    
                    logger.warning(
                        f"[CircuitBreaker:{self._name}] CLOSED -> OPEN "
                        f"(failures={self._failure_count})"
                    )
    
    async def reset(self) -> None:
        """Reset circuit breaker to CLOSED state."""
        async with self._state_lock:
            prev_state = self._state
            self._state = CircuitBreakerState.CLOSED
            self._failure_count = 0
            self._success_count = 0
            self._half_open_request_count = 0
            self._last_state_change = datetime.now()
            
            logger.info(f"[CircuitBreaker:{self._name}] Reset: {prev_state.value} -> CLOSED")
    
    def get_state_info(self) -> Dict[str, Any]:
        """Get detailed state information."""
        return {
            "name": self._name,
            "state": self._state.value,
            "failure_count": self._failure_count,
            "success_count": self._success_count,
            "half_open_requests": self._half_open_request_count,
            "last_failure": (
                self._last_failure_time.isoformat() if self._last_failure_time else None
            ),
            "last_success": (
                self._last_success_time.isoformat() if self._last_success_time else None
            ),
            "last_state_change": self._last_state_change.isoformat(),
            "config": {
                "failure_threshold": self._config.failure_threshold,
                "success_threshold": self._config.success_threshold,
                "recovery_timeout": self._config.recovery_timeout_seconds,
            },
            "recent_failures": len(self._failure_history),
        }
    
    # =========================================================================
    # COROUTINE EXECUTION API (for backward compatibility with inline version)
    # =========================================================================
    
    async def execute(self, coro) -> Any:
        """
        Execute a coroutine with circuit breaker protection.
        
        This method wraps a coroutine and handles the circuit breaker
        check, execution, and success/failure recording automatically.
        
        This is the backward-compatible API matching the original inline
        implementation used by resource managers.
        
        Args:
            coro: A coroutine to execute
            
        Returns:
            The result of the coroutine
            
        Raises:
            RuntimeError: If circuit is OPEN
            Any exception from the coroutine
        
        Example:
            result = await breaker.execute(async_operation())
        """
        if not await self.can_execute():
            raise RuntimeError(f"Circuit breaker {self._name} is OPEN")
        
        try:
            result = await coro
            await self.record_success()
            return result
        except Exception as e:
            await self.record_failure(str(e))
            raise
    
    # =========================================================================
    # SYNCHRONOUS API (for backward compatibility)
    # =========================================================================
    
    def can_execute_sync(self) -> bool:
        """
        Synchronous check if requests are allowed.
        
        Note: Does not perform state transitions. Use async version for full functionality.
        """
        if self._state == CircuitBreakerState.CLOSED:
            return True
        
        if self._state == CircuitBreakerState.OPEN:
            if self._last_failure_time:
                elapsed = (datetime.now() - self._last_failure_time).total_seconds()
                if elapsed >= self._config.recovery_timeout_seconds:
                    return True  # Would transition to HALF_OPEN
            return False
        
        if self._state == CircuitBreakerState.HALF_OPEN:
            return self._half_open_request_count < self._config.half_open_max_requests
        
        return False


# =============================================================================
# RETRY WITH BACKOFF
# =============================================================================

@dataclass
class RetryConfig:
    """Configuration for retry with backoff."""
    max_retries: int = 3
    initial_delay: float = 1.0
    max_delay: float = 30.0
    exponential_base: float = 2.0
    jitter: bool = True


class RetryWithBackoff:
    """
    Retry with exponential backoff and optional jitter.
    
    Supports both the new config-based API and backward-compatible
    parameter-based API for drop-in replacement.
    
    Usage (new style):
        retry = RetryWithBackoff(config=RetryConfig(max_retries=3))
        
    Usage (backward-compatible):
        retry = RetryWithBackoff(max_retries=3, base_delay=1.0)
        result = await retry.execute(lambda: async_op(), "operation_name")
    """
    
    def __init__(
        self,
        config: Optional[RetryConfig] = None,
        name: str = "default",
        # Backward-compatible parameters
        max_retries: Optional[int] = None,
        base_delay: Optional[float] = None,
        max_delay: Optional[float] = None,
        exponential_base: Optional[float] = None,
        jitter: Optional[float] = None,
        retry_exceptions: Optional[tuple] = None,
    ):
        # If backward-compatible parameters are provided, build config from them
        if max_retries is not None or base_delay is not None:
            self._config = RetryConfig(
                max_retries=max_retries if max_retries is not None else 3,
                initial_delay=base_delay if base_delay is not None else 1.0,
                max_delay=max_delay if max_delay is not None else 30.0,
                exponential_base=exponential_base if exponential_base is not None else 2.0,
                jitter=jitter is not None and jitter > 0,
            )
        else:
            self._config = config or RetryConfig()
        
        self._name = name
        self._attempt = 0
        self._last_error: Optional[Exception] = None
        self._retry_exceptions = retry_exceptions or (Exception,)
        
        # Expose config values as attributes for backward compatibility
        self.max_retries = self._config.max_retries
        self.base_delay = self._config.initial_delay
        self.max_delay = self._config.max_delay
        self.exponential_base = self._config.exponential_base
        self.jitter = 0.1 if self._config.jitter else 0.0
        self.retry_exceptions = self._retry_exceptions
    
    def should_retry(self, error: Exception) -> bool:
        """Check if should retry based on error type and attempt count."""
        self._last_error = error
        self._attempt += 1
        return self._attempt < self._config.max_retries
    
    def get_delay(self) -> float:
        """Calculate delay for next retry with exponential backoff."""
        delay = self._config.initial_delay * (
            self._config.exponential_base ** (self._attempt - 1)
        )
        delay = min(delay, self._config.max_delay)
        
        if self._config.jitter:
            import random
            delay = delay * (0.5 + random.random())
        
        return delay
    
    async def __aiter__(self):
        """Async iterator for retry attempts."""
        self._attempt = 0
        
        while self._attempt < self._config.max_retries:
            yield self._attempt
            
            if self._attempt > 0:
                delay = self.get_delay()
                logger.debug(
                    f"[Retry:{self._name}] Attempt {self._attempt + 1}/"
                    f"{self._config.max_retries}, delay={delay:.2f}s"
                )
                await asyncio.sleep(delay)
            
            self._attempt += 1
    
    async def execute(
        self,
        coro_factory: Callable[[], Any],
        operation_name: str = "operation",
    ) -> Any:
        """
        Execute an operation with retry logic.
        
        This is the backward-compatible API matching the original inline
        implementation used by various components.
        
        Args:
            coro_factory: A callable that returns a coroutine
            operation_name: Name for logging purposes
            
        Returns:
            The result of the successful coroutine
            
        Raises:
            The last exception if all retries exhausted
        
        Example:
            result = await retry.execute(
                lambda: async_operation(),
                "fetch_data"
            )
        """
        last_exception: Optional[Exception] = None
        
        for attempt in range(self._config.max_retries + 1):
            try:
                return await coro_factory()
            except Exception as e:
                last_exception = e
                self._last_error = e
                self._attempt = attempt + 1
                
                if attempt < self._config.max_retries:
                    delay = self.get_delay()
                    logger.debug(
                        f"[Retry:{self._name}] {operation_name} failed "
                        f"(attempt {attempt + 1}/{self._config.max_retries + 1}), "
                        f"retrying in {delay:.1f}s: {e}"
                    )
                    await asyncio.sleep(delay)
        
        raise last_exception or RuntimeError(f"Retries exhausted for {operation_name}")


# =============================================================================
# CIRCUIT BREAKER REGISTRY
# =============================================================================

class CircuitBreakerRegistry:
    """
    Registry for managing multiple circuit breakers.
    
    Provides centralized access and monitoring for all circuit breakers
    in the system.
    """
    
    _instance: Optional["CircuitBreakerRegistry"] = None
    
    def __new__(cls) -> "CircuitBreakerRegistry":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._breakers: Dict[str, CircuitBreaker] = {}
            cls._instance._lock = threading.Lock()
        return cls._instance
    
    def get_or_create(
        self,
        name: str,
        config: Optional[CircuitBreakerConfig] = None,
    ) -> CircuitBreaker:
        """Get existing circuit breaker or create new one."""
        with self._lock:
            if name not in self._breakers:
                self._breakers[name] = CircuitBreaker(name, config)
            return self._breakers[name]
    
    def get(self, name: str) -> Optional[CircuitBreaker]:
        """Get circuit breaker by name."""
        return self._breakers.get(name)
    
    def get_all_states(self) -> Dict[str, Dict[str, Any]]:
        """Get state info for all circuit breakers."""
        return {
            name: breaker.get_state_info()
            for name, breaker in self._breakers.items()
        }
    
    async def reset_all(self) -> None:
        """Reset all circuit breakers."""
        for breaker in self._breakers.values():
            await breaker.reset()


# =============================================================================
# CONVENIENCE FUNCTIONS
# =============================================================================

_registry: Optional[CircuitBreakerRegistry] = None


def get_registry() -> CircuitBreakerRegistry:
    """Get the circuit breaker registry."""
    global _registry
    if _registry is None:
        _registry = CircuitBreakerRegistry()
    return _registry


def get_circuit_breaker(
    name: str,
    config: Optional[CircuitBreakerConfig] = None,
) -> CircuitBreaker:
    """
    Get or create a circuit breaker by name.
    
    This is the primary way to access circuit breakers.
    """
    return get_registry().get_or_create(name, config)


# =============================================================================
# MODULE INITIALIZATION
# =============================================================================

logger.debug("[KernelCircuitBreaker] Module loaded")
