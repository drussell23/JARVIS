"""
Resilience Core Types - Foundational Types for Resilience Primitives
=====================================================================

This module provides the core types used across resilience primitives:
- CircuitState: States for circuit breaker pattern
- CapabilityState: States for capability degradation/upgrade management
- RecoveryState: States for background recovery processes
- HealthCheckable: Protocol for components that can report health
- Recoverable: Protocol for components that can self-recover

These types form the foundation for:
- RetryPolicy: Exponential backoff with jitter
- CircuitBreaker: State transitions and failure tracking
- HealthProbe: Cached health checks
- BackgroundRecovery: Adaptive recovery attempts
- CapabilityUpgrade: Hot-swapping between degraded and full modes
"""

from enum import Enum, auto
from typing import Protocol, runtime_checkable


class CircuitState(Enum):
    """
    Circuit breaker states for controlling request flow.

    The circuit breaker pattern prevents cascading failures by:
    - CLOSED: Normal operation - requests flow through
    - OPEN: Failing - requests are rejected immediately
    - HALF_OPEN: Testing recovery - limited requests allowed to probe health

    State transitions:
        CLOSED -> OPEN: When failure threshold exceeded
        OPEN -> HALF_OPEN: After recovery timeout expires
        HALF_OPEN -> CLOSED: If probe requests succeed
        HALF_OPEN -> OPEN: If probe requests fail
    """
    CLOSED = auto()      # Normal operation
    OPEN = auto()        # Failing, requests rejected
    HALF_OPEN = auto()   # Testing recovery


class CapabilityState(Enum):
    """
    States for managing capability degradation and upgrades.

    Supports graceful degradation patterns:
    - DEGRADED: Using fallback/cached/local capability
    - UPGRADING: Attempting to restore full capability
    - FULL: Full capability active (e.g., cloud API available)
    - MONITORING: Full mode but watching for regression

    State transitions:
        FULL -> DEGRADED: When primary capability fails
        DEGRADED -> UPGRADING: When recovery attempt begins
        UPGRADING -> FULL: When upgrade succeeds
        UPGRADING -> DEGRADED: When upgrade fails
        FULL -> MONITORING: After recovery to watch for regression
        MONITORING -> FULL: After stability period
        MONITORING -> DEGRADED: If regression detected
    """
    DEGRADED = auto()    # Using fallback
    UPGRADING = auto()   # Attempting upgrade
    FULL = auto()        # Full capability active
    MONITORING = auto()  # Full mode, monitoring for regression


class RecoveryState(Enum):
    """
    States for background recovery processes.

    Controls recovery loop behavior:
    - IDLE: Not running, no recovery needed
    - RECOVERING: Actively attempting recovery
    - PAUSED: Paused due to safety valve (too many attempts)
    - SUCCEEDED: Recovery completed successfully

    State transitions:
        IDLE -> RECOVERING: When recovery needed
        RECOVERING -> SUCCEEDED: When recovery works
        RECOVERING -> PAUSED: When safety valve triggers
        RECOVERING -> IDLE: When cancelled
        PAUSED -> RECOVERING: After pause duration
        PAUSED -> IDLE: When cancelled
        SUCCEEDED -> IDLE: After cleanup/reset
    """
    IDLE = auto()        # Not running
    RECOVERING = auto()  # Actively attempting recovery
    PAUSED = auto()      # Paused due to safety valve
    SUCCEEDED = auto()   # Recovery completed


@runtime_checkable
class HealthCheckable(Protocol):
    """
    Protocol for components that can report their health status.

    Implementing classes must provide an async check() method that:
    - Returns True if the component is healthy
    - Returns False if the component is unhealthy
    - Should be lightweight and fast (cached if expensive)
    - Should not raise exceptions (return False on error)

    Example implementation:
        class DatabaseConnection:
            async def check(self) -> bool:
                try:
                    await self.pool.execute("SELECT 1")
                    return True
                except Exception:
                    return False
    """

    async def check(self) -> bool:
        """
        Check if the component is healthy.

        Returns:
            True if healthy, False otherwise
        """
        ...


@runtime_checkable
class Recoverable(Protocol):
    """
    Protocol for components that can attempt self-recovery.

    Implementing classes must provide an async recover() method that:
    - Attempts to recover the component to a working state
    - Returns True if recovery succeeded
    - Returns False if recovery failed
    - May be called multiple times (should be idempotent)
    - Should handle its own error cases gracefully

    Example implementation:
        class ServiceConnection:
            async def recover(self) -> bool:
                try:
                    await self.disconnect()
                    await self.connect()
                    return await self.ping()
                except Exception:
                    return False
    """

    async def recover(self) -> bool:
        """
        Attempt to recover the component.

        Returns:
            True if recovery succeeded, False otherwise
        """
        ...


__all__ = [
    "CircuitState",
    "CapabilityState",
    "RecoveryState",
    "HealthCheckable",
    "Recoverable",
]
