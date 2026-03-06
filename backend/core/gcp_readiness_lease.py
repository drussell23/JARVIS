"""Lease-based VM readiness with 3-part handshake for Disease 10 Startup Sequencing.

Replaces binary ready/not-ready semantics with a structured handshake
protocol that probes health, capabilities, and model warmth in sequence.
A successfully acquired lease has a configurable TTL and can be refreshed
(via a lightweight health-only re-probe) or revoked immediately.

Failure classification is baked in: every failed step or timeout is tagged
with a :class:`ReadinessFailureClass` so upstream code can decide on the
correct recovery strategy without string-matching error messages.
"""

from __future__ import annotations

import abc
import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

__all__ = [
    "HandshakeStep",
    "ReadinessFailureClass",
    "LeaseStatus",
    "HandshakeResult",
    "ReadinessProber",
    "GCPReadinessLease",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class HandshakeStep(str, Enum):
    """Individual steps of the 3-part readiness handshake."""

    HEALTH = "health"
    CAPABILITIES = "capabilities"
    WARM_MODEL = "warm_model"


class ReadinessFailureClass(str, Enum):
    """Machine-readable classification for readiness probe failures."""

    NETWORK = "network"
    QUOTA = "quota"
    RESOURCE = "resource"
    PREEMPTION = "preemption"
    SCHEMA_MISMATCH = "schema_mismatch"
    TIMEOUT = "timeout"


class LeaseStatus(str, Enum):
    """Lifecycle status of a readiness lease."""

    INACTIVE = "inactive"
    ACTIVE = "active"
    EXPIRED = "expired"
    FAILED = "failed"
    REVOKED = "revoked"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class HandshakeResult:
    """Result of a single handshake probe step."""

    step: HandshakeStep
    passed: bool
    failure_class: Optional[ReadinessFailureClass] = None
    detail: str = ""
    data: Optional[Dict[str, Any]] = None
    timestamp: float = field(default_factory=time.monotonic)


# ---------------------------------------------------------------------------
# Abstract base — prober interface
# ---------------------------------------------------------------------------

class ReadinessProber(abc.ABC):
    """Abstract interface for probing VM readiness across 3 dimensions."""

    @abc.abstractmethod
    async def probe_health(
        self, host: str, port: int, timeout: float,
    ) -> HandshakeResult:
        """Check basic VM health (network reachable, process alive)."""

    @abc.abstractmethod
    async def probe_capabilities(
        self, host: str, port: int, timeout: float,
    ) -> HandshakeResult:
        """Verify the VM exposes the expected API capabilities."""

    @abc.abstractmethod
    async def probe_warm_model(
        self, host: str, port: int, timeout: float,
    ) -> HandshakeResult:
        """Confirm the inference model is loaded and warm."""


# ---------------------------------------------------------------------------
# Lease
# ---------------------------------------------------------------------------

class GCPReadinessLease:
    """Lease-based VM readiness contract with 3-part handshake.

    Acquire the lease via :meth:`acquire` (runs the full 3-step handshake).
    The lease is valid for ``ttl_seconds`` after acquisition and can be
    extended via :meth:`refresh` (health-only re-probe) or terminated
    immediately via :meth:`revoke`.
    """

    def __init__(self, prober: ReadinessProber, ttl_seconds: float) -> None:
        self._prober = prober
        self._ttl_seconds = ttl_seconds

        self._status: LeaseStatus = LeaseStatus.INACTIVE
        self._acquired_at: Optional[float] = None
        self._host: Optional[str] = None
        self._port: Optional[int] = None
        self._last_failure_class: Optional[ReadinessFailureClass] = None
        self._handshake_log: List[HandshakeResult] = []

    # -- Properties ---------------------------------------------------------

    @property
    def status(self) -> LeaseStatus:
        """Current lease status, checking TTL expiry dynamically."""
        if self._status == LeaseStatus.ACTIVE and self._acquired_at is not None:
            elapsed = time.monotonic() - self._acquired_at
            if elapsed > self._ttl_seconds:
                self._status = LeaseStatus.EXPIRED
                logger.info(
                    "Lease expired: %.3fs elapsed, TTL was %.3fs",
                    elapsed, self._ttl_seconds,
                )
        return self._status

    @property
    def is_valid(self) -> bool:
        """True only when the lease is currently ACTIVE (and not expired)."""
        return self.status == LeaseStatus.ACTIVE

    @property
    def host(self) -> Optional[str]:
        """Host associated with the lease, or None if never acquired."""
        return self._host

    @property
    def port(self) -> Optional[int]:
        """Port associated with the lease, or None if never acquired."""
        return self._port

    @property
    def last_failure_class(self) -> Optional[ReadinessFailureClass]:
        """Failure class from the most recent failed step, if any."""
        return self._last_failure_class

    @property
    def handshake_log(self) -> List[HandshakeResult]:
        """Return a *copy* of the handshake log from the last acquire attempt."""
        return list(self._handshake_log)

    # -- Public methods -----------------------------------------------------

    async def acquire(
        self,
        host: str,
        port: int,
        timeout_per_step: float,
    ) -> bool:
        """Run the full 3-step handshake and acquire the lease on success.

        Steps are executed sequentially: health -> capabilities -> warm_model.
        Each step is wrapped in ``asyncio.wait_for(timeout_per_step)``.

        On any step failure the lease is marked FAILED with
        :attr:`last_failure_class` set.  On timeout, the failure class is
        :attr:`ReadinessFailureClass.TIMEOUT`.

        Returns True if all 3 steps passed and the lease is now ACTIVE.
        """
        self._handshake_log.clear()
        self._host = host
        self._port = port
        self._last_failure_class = None

        probe_methods = [
            (HandshakeStep.HEALTH, self._prober.probe_health),
            (HandshakeStep.CAPABILITIES, self._prober.probe_capabilities),
            (HandshakeStep.WARM_MODEL, self._prober.probe_warm_model),
        ]

        for step, probe_fn in probe_methods:
            result = await self._run_probe(
                step, probe_fn, host, port, timeout_per_step,
            )
            self._handshake_log.append(result)

            if not result.passed:
                self._status = LeaseStatus.FAILED
                self._last_failure_class = result.failure_class
                logger.warning(
                    "Handshake step %s failed: %s (class=%s)",
                    step.value, result.detail, result.failure_class,
                )
                return False

        # All steps passed — activate the lease.
        self._status = LeaseStatus.ACTIVE
        self._acquired_at = time.monotonic()
        logger.info(
            "Lease acquired for %s:%d (TTL=%.1fs)",
            host, port, self._ttl_seconds,
        )
        return True

    async def refresh(self, timeout_per_step: float) -> bool:
        """Re-run the health probe only and extend the lease TTL on success.

        On failure the lease transitions to FAILED.

        Returns True if the health probe passed and the TTL was extended.
        """
        if self._host is None or self._port is None:
            logger.warning("Cannot refresh lease: no host/port (never acquired)")
            self._status = LeaseStatus.FAILED
            return False

        result = await self._run_probe(
            HandshakeStep.HEALTH,
            self._prober.probe_health,
            self._host,
            self._port,
            timeout_per_step,
        )

        if result.passed:
            self._acquired_at = time.monotonic()
            self._status = LeaseStatus.ACTIVE
            logger.info("Lease refreshed for %s:%d", self._host, self._port)
            return True

        self._status = LeaseStatus.FAILED
        self._last_failure_class = result.failure_class
        logger.warning(
            "Lease refresh failed: %s (class=%s)",
            result.detail, result.failure_class,
        )
        return False

    def revoke(self, reason: str = "") -> None:
        """Immediately invalidate the lease."""
        self._status = LeaseStatus.REVOKED
        logger.info("Lease revoked: %s", reason or "(no reason)")

    # -- Internal helpers ---------------------------------------------------

    async def _run_probe(
        self,
        step: HandshakeStep,
        probe_fn,
        host: str,
        port: int,
        timeout: float,
    ) -> HandshakeResult:
        """Execute a single probe with a timeout guard.

        If the probe exceeds *timeout*, a TIMEOUT failure is returned.
        """
        try:
            result = await asyncio.wait_for(
                probe_fn(host, port, timeout),
                timeout=timeout,
            )
            return result
        except asyncio.TimeoutError:
            logger.warning(
                "Probe %s timed out after %.3fs", step.value, timeout,
            )
            return HandshakeResult(
                step=step,
                passed=False,
                failure_class=ReadinessFailureClass.TIMEOUT,
                detail=f"probe timed out after {timeout:.3f}s",
            )
        except Exception as exc:
            logger.exception("Probe %s raised unexpected error", step.value)
            return HandshakeResult(
                step=step,
                passed=False,
                failure_class=ReadinessFailureClass.NETWORK,
                detail=f"unexpected error: {exc}",
            )
