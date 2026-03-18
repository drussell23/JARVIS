"""backend/core/component_circuit_breaker.py — Disease 4: component-level circuit breakers.

Without per-component isolation, one slow or crashing component can hold a
concurrency semaphore slot indefinitely, starving other components or causing
the whole startup phase to time out and receive SIGKILL.

Design:
* ``BreakerState``             — CLOSED / OPEN / HALF_OPEN
* ``ComponentState``           — HEALTHY / DEGRADED / FAILED (coarser view)
* ``BreakerConfig``            — immutable per-component policy
* ``ComponentCircuitBreaker``  — standard circuit breaker for one component
* ``CircuitBreakerRegistry``   — process-wide collection; DMS queries all_failed()
* ``get_circuit_breaker_registry()`` — module-level singleton
"""
from __future__ import annotations

import enum
import logging
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

__all__ = [
    "BreakerState",
    "ComponentState",
    "BreakerConfig",
    "ComponentCircuitBreaker",
    "CircuitBreakerRegistry",
    "get_circuit_breaker_registry",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class BreakerState(str, enum.Enum):
    CLOSED = "closed"       # normal — calls allowed
    OPEN = "open"           # tripped — failing fast, waiting for recovery timeout
    HALF_OPEN = "half_open" # probing — one test call allowed


class ComponentState(str, enum.Enum):
    """Coarser health view used by orchestrators."""
    HEALTHY = "healthy"    # CLOSED with no recent failures
    DEGRADED = "degraded"  # HALF_OPEN, or CLOSED with some failures
    FAILED = "failed"      # OPEN (hard-tripped)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BreakerConfig:
    """Immutable per-component circuit breaker policy.

    Parameters
    ----------
    failure_threshold:
        Consecutive failures required to trip the breaker OPEN.
    recovery_timeout_s:
        Seconds after tripping before transitioning to HALF_OPEN.
    half_open_max_calls:
        Probe calls allowed during HALF_OPEN before committing to CLOSED/OPEN.
    """

    failure_threshold: int = 3
    recovery_timeout_s: float = 60.0
    half_open_max_calls: int = 1


# ---------------------------------------------------------------------------
# Mutable internal stats (not frozen — mutated on every call)
# ---------------------------------------------------------------------------


@dataclass
class _Stats:
    consecutive_failures: int = 0
    total_failures: int = 0
    total_successes: int = 0
    total_rejected: int = 0
    tripped_at_mono: Optional[float] = None
    last_failure_reason: str = ""
    half_open_probes: int = 0


# ---------------------------------------------------------------------------
# Per-component circuit breaker
# ---------------------------------------------------------------------------


class ComponentCircuitBreaker:
    """Standard three-state circuit breaker for one startup component.

    Usage::

        breaker = registry.get_or_create("neural_mesh")
        allowed, reason = breaker.can_execute()
        if not allowed:
            # skip or raise MemoryGateRefused
            return

        try:
            await init_neural_mesh()
            breaker.record_success()
        except Exception as exc:
            breaker.record_failure(exc)
            raise
    """

    def __init__(self, component: str, config: BreakerConfig) -> None:
        self.component = component
        self._config = config
        self._state: BreakerState = BreakerState.CLOSED
        self._stats = _Stats()

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------

    def can_execute(self) -> Tuple[bool, str]:
        """Return ``(True, "")`` if the component may attempt initialisation.

        * CLOSED    → always allowed.
        * OPEN      → check recovery timeout; if elapsed, advance to HALF_OPEN.
        * HALF_OPEN → allowed up to ``half_open_max_calls`` probe calls.
        """
        if self._state == BreakerState.CLOSED:
            return True, ""

        if self._state == BreakerState.OPEN:
            elapsed = time.monotonic() - (self._stats.tripped_at_mono or 0.0)
            if elapsed >= self._config.recovery_timeout_s:
                self._state = BreakerState.HALF_OPEN
                self._stats.half_open_probes = 0
                logger.info(
                    "[CircuitBreaker] '%s' → HALF_OPEN after %.0fs",
                    self.component, elapsed,
                )
            else:
                self._stats.total_rejected += 1
                remaining = self._config.recovery_timeout_s - elapsed
                return False, (
                    f"breaker OPEN for '{self.component}' — "
                    f"{remaining:.0f}s until HALF_OPEN probe"
                )

        # HALF_OPEN
        if self._stats.half_open_probes < self._config.half_open_max_calls:
            self._stats.half_open_probes += 1
            return True, ""

        self._stats.total_rejected += 1
        return False, (
            f"breaker HALF_OPEN for '{self.component}' — probe limit "
            f"({self._config.half_open_max_calls}) reached, awaiting outcome"
        )

    def record_success(self) -> None:
        """Call after successful initialisation.  Closes the breaker."""
        was_open = self._state != BreakerState.CLOSED
        self._stats.consecutive_failures = 0
        self._stats.total_successes += 1
        self._state = BreakerState.CLOSED
        if was_open:
            logger.info("[CircuitBreaker] '%s' → CLOSED (recovered)", self.component)

    def record_failure(self, exc: Optional[BaseException] = None) -> None:
        """Call after failed initialisation.  May trip the breaker OPEN."""
        self._stats.consecutive_failures += 1
        self._stats.total_failures += 1
        self._stats.last_failure_reason = str(exc) if exc else "unknown"

        if self._stats.consecutive_failures >= self._config.failure_threshold:
            if self._state != BreakerState.OPEN:
                self._stats.tripped_at_mono = time.monotonic()
                self._state = BreakerState.OPEN
                logger.error(
                    "[CircuitBreaker] '%s' → OPEN after %d consecutive failures "
                    "(last: %s)",
                    self.component,
                    self._stats.consecutive_failures,
                    self._stats.last_failure_reason,
                )
        else:
            logger.warning(
                "[CircuitBreaker] '%s' failure %d/%d — %s",
                self.component,
                self._stats.consecutive_failures,
                self._config.failure_threshold,
                self._stats.last_failure_reason,
            )

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------

    @property
    def state(self) -> BreakerState:
        return self._state

    @property
    def component_state(self) -> ComponentState:
        if self._state == BreakerState.OPEN:
            return ComponentState.FAILED
        if self._state == BreakerState.CLOSED and self._stats.consecutive_failures == 0:
            return ComponentState.HEALTHY
        return ComponentState.DEGRADED

    @property
    def consecutive_failures(self) -> int:
        return self._stats.consecutive_failures

    @property
    def total_failures(self) -> int:
        return self._stats.total_failures

    @property
    def total_successes(self) -> int:
        return self._stats.total_successes

    @property
    def total_rejected(self) -> int:
        return self._stats.total_rejected

    @property
    def last_failure_reason(self) -> str:
        return self._stats.last_failure_reason

    @property
    def config(self) -> BreakerConfig:
        return self._config


# ---------------------------------------------------------------------------
# Process-wide registry
# ---------------------------------------------------------------------------


class CircuitBreakerRegistry:
    """Manages ComponentCircuitBreaker instances for all startup components."""

    def __init__(self) -> None:
        self._breakers: Dict[str, ComponentCircuitBreaker] = {}

    def get_or_create(
        self,
        component: str,
        config: Optional[BreakerConfig] = None,
    ) -> ComponentCircuitBreaker:
        """Return existing breaker or create with *config* (default policy if None)."""
        if component not in self._breakers:
            cfg = config if config is not None else BreakerConfig()
            self._breakers[component] = ComponentCircuitBreaker(component, cfg)
            logger.debug(
                "[CBRegistry] registered '%s' threshold=%d recovery=%.0fs",
                component, cfg.failure_threshold, cfg.recovery_timeout_s,
            )
        return self._breakers[component]

    def get(self, component: str) -> Optional[ComponentCircuitBreaker]:
        return self._breakers.get(component)

    def all_failed(self) -> List[ComponentCircuitBreaker]:
        """Breakers currently in OPEN state."""
        return [b for b in self._breakers.values() if b.state == BreakerState.OPEN]

    def all_degraded(self) -> List[ComponentCircuitBreaker]:
        """Breakers in DEGRADED component state."""
        return [
            b for b in self._breakers.values()
            if b.component_state == ComponentState.DEGRADED
        ]

    def all_healthy(self) -> List[ComponentCircuitBreaker]:
        return [
            b for b in self._breakers.values()
            if b.component_state == ComponentState.HEALTHY
        ]

    def snapshot(self) -> Dict[str, ComponentState]:
        """Component name → ComponentState snapshot."""
        return {name: b.component_state for name, b in self._breakers.items()}

    def reset_all(self) -> None:
        """Clear all breakers — call between DMS restart cycles."""
        count = len(self._breakers)
        self._breakers.clear()
        logger.info("[CBRegistry] reset — cleared %d breakers", count)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_g_registry: Optional[CircuitBreakerRegistry] = None


def get_circuit_breaker_registry() -> CircuitBreakerRegistry:
    """Return (lazily creating) the process-wide CircuitBreakerRegistry."""
    global _g_registry
    if _g_registry is None:
        _g_registry = CircuitBreakerRegistry()
    return _g_registry
