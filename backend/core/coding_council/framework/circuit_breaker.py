"""
v77.0: Circuit Breaker - Gap #9
================================

Circuit breaker pattern for fault tolerance:
- Three states: CLOSED, OPEN, HALF_OPEN
- Configurable failure thresholds
- Automatic recovery attempts
- Fallback support
- Per-service circuit breakers

Author: JARVIS v77.0
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine, Dict, List, Optional, TypeVar

from backend.core.async_safety import LazyAsyncLock

logger = logging.getLogger(__name__)

T = TypeVar("T")


class CircuitState(Enum):
    """State of the circuit breaker."""
    CLOSED = "closed"       # Normal operation
    OPEN = "open"           # Failing, reject calls
    HALF_OPEN = "half_open"  # Testing recovery


class CircuitBreakerError(Exception):
    """Raised when circuit breaker is open."""
    def __init__(self, name: str, state: CircuitState, recovery_in: float):
        self.name = name
        self.state = state
        self.recovery_in = recovery_in
        super().__init__(
            f"Circuit breaker '{name}' is {state.value}. "
            f"Recovery attempt in {recovery_in:.1f}s"
        )


@dataclass
class CircuitBreakerConfig:
    """Configuration for circuit breaker."""
    failure_threshold: int = 5         # Failures before opening
    success_threshold: int = 3         # Successes to close from half-open
    timeout: float = 60.0              # Seconds before trying half-open
    half_open_max_calls: int = 3       # Max calls in half-open state
    excluded_exceptions: tuple = ()    # Exceptions that don't count as failures


@dataclass
class CircuitStats:
    """Statistics for a circuit breaker."""
    total_calls: int = 0
    successful_calls: int = 0
    failed_calls: int = 0
    rejected_calls: int = 0
    last_failure_time: Optional[float] = None
    last_success_time: Optional[float] = None
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    state_changes: List[tuple] = field(default_factory=list)


class CircuitBreaker:
    """
    Circuit breaker for protecting against cascading failures.

    State transitions:
    - CLOSED -> OPEN: When failure_threshold consecutive failures occur
    - OPEN -> HALF_OPEN: After timeout period
    - HALF_OPEN -> CLOSED: After success_threshold consecutive successes
    - HALF_OPEN -> OPEN: On any failure
    """

    def __init__(
        self,
        name: str,
        config: Optional[CircuitBreakerConfig] = None,
        fallback: Optional[Callable[..., Any]] = None,
    ):
        self.name = name
        self.config = config or CircuitBreakerConfig()
        self.fallback = fallback
        self._state = CircuitState.CLOSED
        self._stats = CircuitStats()
        self._opened_at: Optional[float] = None
        self._half_open_calls = 0
        self._lock = asyncio.Lock()
        self._listeners: List[Callable[[CircuitState, CircuitState], Coroutine]] = []

    @property
    def state(self) -> CircuitState:
        return self._state

    @property
    def stats(self) -> CircuitStats:
        return self._stats

    async def call(
        self,
        func: Callable[..., Coroutine],
        *args,
        fallback: Optional[Callable[..., Any]] = None,
        **kwargs,
    ) -> Any:
        """
        Execute a function through the circuit breaker.

        Returns result or raises CircuitBreakerError if open.
        """
        async with self._lock:
            # Check state and update if needed
            await self._check_state()

            if self._state == CircuitState.OPEN:
                self._stats.rejected_calls += 1
                recovery_in = self._get_recovery_time()

                # Try fallback
                fb = fallback or self.fallback
                if fb:
                    logger.debug(f"[CircuitBreaker] {self.name} using fallback")
                    if asyncio.iscoroutinefunction(fb):
                        return await fb(*args, **kwargs)
                    return fb(*args, **kwargs)

                raise CircuitBreakerError(self.name, self._state, recovery_in)

            if self._state == CircuitState.HALF_OPEN:
                if self._half_open_calls >= self.config.half_open_max_calls:
                    raise CircuitBreakerError(
                        self.name, self._state,
                        self.config.timeout
                    )
                self._half_open_calls += 1

        # Execute the call
        self._stats.total_calls += 1

        try:
            result = await func(*args, **kwargs)
            await self._on_success()
            return result

        except Exception as e:
            # Check if this exception should be excluded
            if isinstance(e, self.config.excluded_exceptions):
                await self._on_success()
                raise

            await self._on_failure(e)
            raise

    async def _on_success(self) -> None:
        """Handle successful call."""
        async with self._lock:
            self._stats.successful_calls += 1
            self._stats.consecutive_successes += 1
            self._stats.consecutive_failures = 0
            self._stats.last_success_time = time.time()

            if self._state == CircuitState.HALF_OPEN:
                if self._stats.consecutive_successes >= self.config.success_threshold:
                    await self._transition_to(CircuitState.CLOSED)

    async def _on_failure(self, error: Exception) -> None:
        """Handle failed call."""
        async with self._lock:
            self._stats.failed_calls += 1
            self._stats.consecutive_failures += 1
            self._stats.consecutive_successes = 0
            self._stats.last_failure_time = time.time()

            logger.warning(
                f"[CircuitBreaker] {self.name} failure #{self._stats.consecutive_failures}: {error}"
            )

            if self._state == CircuitState.HALF_OPEN:
                # Any failure in half-open returns to open
                await self._transition_to(CircuitState.OPEN)

            elif self._state == CircuitState.CLOSED:
                if self._stats.consecutive_failures >= self.config.failure_threshold:
                    await self._transition_to(CircuitState.OPEN)

    async def _check_state(self) -> None:
        """Check if state should transition based on time."""
        if self._state == CircuitState.OPEN and self._opened_at:
            if time.time() - self._opened_at >= self.config.timeout:
                await self._transition_to(CircuitState.HALF_OPEN)

    async def _transition_to(self, new_state: CircuitState) -> None:
        """Transition to a new state."""
        old_state = self._state
        self._state = new_state

        self._stats.state_changes.append((time.time(), old_state.value, new_state.value))

        if new_state == CircuitState.OPEN:
            self._opened_at = time.time()
            self._half_open_calls = 0
            logger.warning(f"[CircuitBreaker] {self.name} OPENED after {self._stats.consecutive_failures} failures")

        elif new_state == CircuitState.HALF_OPEN:
            self._half_open_calls = 0
            logger.info(f"[CircuitBreaker] {self.name} entering HALF_OPEN for recovery test")

        elif new_state == CircuitState.CLOSED:
            self._opened_at = None
            self._stats.consecutive_failures = 0
            logger.info(f"[CircuitBreaker] {self.name} CLOSED - recovered successfully")

        # Notify listeners
        for listener in self._listeners:
            try:
                await listener(old_state, new_state)
            except Exception as e:
                logger.error(f"[CircuitBreaker] Listener error: {e}")

    def _get_recovery_time(self) -> float:
        """Get time until next recovery attempt."""
        if self._opened_at is None:
            return 0.0
        elapsed = time.time() - self._opened_at
        return max(0.0, self.config.timeout - elapsed)

    def on_state_change(self, callback: Callable[[CircuitState, CircuitState], Coroutine]) -> None:
        """Register callback for state changes."""
        self._listeners.append(callback)

    async def reset(self) -> None:
        """Manually reset the circuit breaker."""
        async with self._lock:
            await self._transition_to(CircuitState.CLOSED)
            self._stats = CircuitStats()

    def get_info(self) -> Dict[str, Any]:
        """Get circuit breaker information."""
        return {
            "name": self.name,
            "state": self._state.value,
            "stats": {
                "total_calls": self._stats.total_calls,
                "successful": self._stats.successful_calls,
                "failed": self._stats.failed_calls,
                "rejected": self._stats.rejected_calls,
                "consecutive_failures": self._stats.consecutive_failures,
            },
            "recovery_in": self._get_recovery_time() if self._state == CircuitState.OPEN else None,
        }


# Global registry of circuit breakers
_circuit_breakers: Dict[str, CircuitBreaker] = {}
_registry_lock = LazyAsyncLock()  # v100.1: Lazy initialization to avoid "no running event loop" error


async def get_circuit_breaker(
    name: str,
    config: Optional[CircuitBreakerConfig] = None,
) -> CircuitBreaker:
    """Get or create a circuit breaker by name."""
    async with _registry_lock:
        if name not in _circuit_breakers:
            _circuit_breakers[name] = CircuitBreaker(name, config)
        return _circuit_breakers[name]


def circuit_breaker(
    name: str,
    config: Optional[CircuitBreakerConfig] = None,
    fallback: Optional[Callable[..., Any]] = None,
):
    """
    Decorator to protect a function with a circuit breaker.

    Usage:
        @circuit_breaker("my_service")
        async def call_external_service():
            ...
    """
    def decorator(func: Callable[..., Coroutine]) -> Callable[..., Coroutine]:
        cb: Optional[CircuitBreaker] = None

        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            nonlocal cb
            if cb is None:
                cb = await get_circuit_breaker(name, config)
                if fallback:
                    cb.fallback = fallback

            return await cb.call(func, *args, **kwargs)

        # Expose circuit breaker on wrapper
        wrapper.circuit_breaker = lambda: cb
        return wrapper

    return decorator
