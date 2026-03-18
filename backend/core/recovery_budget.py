"""RecoveryBudget — P1-3 recovery oscillation / hysteresis guard.

Prevents restart/degrade/recover loops by:

* Classifying failures into reason classes (NETWORK, CONFIG, RESOURCE,
  CRASH, UNKNOWN).
* Maintaining per-class attempt counters with exponential backoff.
* Quarantining a service when QUARANTINE_THRESHOLD attempts of any one
  class are exhausted (committed-off for ``QUARANTINE_DURATION_S``).
* Resetting a class bucket when the service stays healthy for
  ``HEALTH_RESET_AFTER_S`` seconds.

All timing uses ``time.monotonic()`` — never ``time.time()``.
"""

from __future__ import annotations

import enum
import logging
import time
from dataclasses import dataclass
from typing import Dict, Optional

__all__ = [
    "FailureClass",
    "BackoffProfile",
    "BACKOFF_PROFILES",
    "RecoveryBudget",
    "RecoveryBudgetRegistry",
    "get_recovery_budget_registry",
]

logger = logging.getLogger(__name__)

# Number of attempts in any one failure class before quarantine.
QUARANTINE_THRESHOLD: int = 5
# How long the service stays in quarantine (seconds).
QUARANTINE_DURATION_S: float = 600.0
# After this many seconds of clean health, reset attempt counters.
HEALTH_RESET_AFTER_S: float = 300.0


# ---------------------------------------------------------------------------
# Failure classification
# ---------------------------------------------------------------------------


class FailureClass(str, enum.Enum):
    """Broad categories for why a service failed / needed recovery."""

    NETWORK = "network"
    CONFIG = "config"
    RESOURCE = "resource"
    CRASH = "crash"
    UNKNOWN = "unknown"

    @classmethod
    def from_exception(cls, exc: BaseException) -> "FailureClass":
        """Heuristically classify an exception into a FailureClass."""
        name = type(exc).__name__.lower()
        msg = str(exc).lower()
        if any(k in name or k in msg for k in ("timeout", "connect", "network", "dns", "socket")):
            return cls.NETWORK
        if any(k in name or k in msg for k in ("config", "setting", "env", "missing")):
            return cls.CONFIG
        if any(k in name or k in msg for k in ("memory", "oom", "resource", "disk", "quota")):
            return cls.RESOURCE
        if any(k in name or k in msg for k in ("crash", "abort", "segfault", "killed")):
            return cls.CRASH
        return cls.UNKNOWN


# ---------------------------------------------------------------------------
# Backoff profiles
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BackoffProfile:
    """Exponential-backoff configuration for one failure class."""

    base_s: float        # Initial wait after first failure.
    max_s: float         # Cap on backoff (seconds).
    multiplier: float    # Growth factor per attempt.
    jitter: float        # Maximum random jitter fraction (0 = no jitter).


BACKOFF_PROFILES: Dict[FailureClass, BackoffProfile] = {
    FailureClass.NETWORK:  BackoffProfile(base_s=5.0,  max_s=120.0, multiplier=2.0, jitter=0.2),
    FailureClass.CONFIG:   BackoffProfile(base_s=10.0, max_s=300.0, multiplier=2.0, jitter=0.1),
    FailureClass.RESOURCE: BackoffProfile(base_s=30.0, max_s=600.0, multiplier=1.5, jitter=0.1),
    FailureClass.CRASH:    BackoffProfile(base_s=15.0, max_s=240.0, multiplier=2.0, jitter=0.2),
    FailureClass.UNKNOWN:  BackoffProfile(base_s=10.0, max_s=180.0, multiplier=2.0, jitter=0.1),
}


# ---------------------------------------------------------------------------
# Per-failure-class bucket
# ---------------------------------------------------------------------------


@dataclass
class _ClassBucket:
    attempts: int = 0
    last_attempt_mono: float = 0.0

    def backoff_s(self, profile: BackoffProfile) -> float:
        """Return the next backoff delay (seconds) for this bucket."""
        import random
        n = max(0, self.attempts - 1)
        raw = profile.base_s * (profile.multiplier ** n)
        capped = min(raw, profile.max_s)
        jitter_amount = capped * profile.jitter * random.random()
        return capped + jitter_amount


# ---------------------------------------------------------------------------
# RecoveryBudget — per-service
# ---------------------------------------------------------------------------


class RecoveryBudget:
    """Tracks recovery attempt history and enforces quarantine for one service.

    Parameters
    ----------
    service:
        Logical service name (for logging).
    quarantine_threshold:
        Max attempts per class before quarantine.
    quarantine_duration_s:
        How long quarantine lasts (seconds).
    health_reset_after_s:
        Consecutive healthy seconds before attempt counters reset.
    """

    def __init__(
        self,
        service: str,
        quarantine_threshold: int = QUARANTINE_THRESHOLD,
        quarantine_duration_s: float = QUARANTINE_DURATION_S,
        health_reset_after_s: float = HEALTH_RESET_AFTER_S,
    ) -> None:
        self._service = service
        self._threshold = quarantine_threshold
        self._quarantine_duration_s = quarantine_duration_s
        self._health_reset_after_s = health_reset_after_s
        self._buckets: Dict[FailureClass, _ClassBucket] = {
            fc: _ClassBucket() for fc in FailureClass
        }
        # Quarantine state.
        self._quarantine_until_mono: float = 0.0
        # Last time service was confirmed healthy (0 = never).
        self._last_healthy_mono: float = 0.0

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def can_attempt(self, reason: FailureClass) -> bool:
        """Return True if a recovery attempt is allowed right now.

        Checks quarantine state first (whole-service gate), then per-class
        budget, then backoff delay.
        """
        now = time.monotonic()

        # --- Health-based counter reset ---
        self._maybe_reset_on_health(now)

        # --- Quarantine gate (whole-service) ---
        if now < self._quarantine_until_mono:
            remaining = self._quarantine_until_mono - now
            logger.warning(
                "[RecoveryBudget] %s is quarantined for %.0fs more",
                self._service, remaining,
            )
            return False

        # --- Per-class backoff gate ---
        bucket = self._buckets[reason]
        if bucket.attempts == 0:
            return True  # No prior attempts → always allowed.

        profile = BACKOFF_PROFILES[reason]
        backoff = bucket.backoff_s(profile)
        since_last = now - bucket.last_attempt_mono
        if since_last < backoff:
            logger.debug(
                "[RecoveryBudget] %s/%s backoff active (need %.1fs more)",
                self._service, reason.value, backoff - since_last,
            )
            return False

        return True

    def record_attempt(self, reason: FailureClass) -> None:
        """Record that a recovery attempt was made.

        Increments the class bucket and triggers quarantine if the
        threshold is crossed.
        """
        bucket = self._buckets[reason]
        bucket.attempts += 1
        bucket.last_attempt_mono = time.monotonic()

        logger.info(
            "[RecoveryBudget] %s/%s — attempt #%d (threshold=%d)",
            self._service, reason.value, bucket.attempts, self._threshold,
        )

        if bucket.attempts >= self._threshold:
            self._enter_quarantine(reason)

    def record_healthy(self) -> None:
        """Signal that the service is currently healthy.

        Updates the last-healthy timestamp so that, after
        ``health_reset_after_s``, attempt counters are reset.
        """
        self._last_healthy_mono = time.monotonic()

    def is_quarantined(self) -> bool:
        """Return True if the service is currently in committed-off state."""
        return time.monotonic() < self._quarantine_until_mono

    def quarantine_remaining_s(self) -> float:
        """Return seconds remaining in quarantine (0 if not quarantined)."""
        return max(0.0, self._quarantine_until_mono - time.monotonic())

    def attempts_for(self, reason: FailureClass) -> int:
        """Return attempt count for a specific failure class."""
        return self._buckets[reason].attempts

    def reset(self) -> None:
        """Forcibly reset all counters and clear quarantine (operator override)."""
        for bucket in self._buckets.values():
            bucket.attempts = 0
            bucket.last_attempt_mono = 0.0
        self._quarantine_until_mono = 0.0
        logger.info("[RecoveryBudget] %s — all counters manually reset", self._service)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _enter_quarantine(self, reason: FailureClass) -> None:
        until = time.monotonic() + self._quarantine_duration_s
        self._quarantine_until_mono = until
        logger.error(
            "[RecoveryBudget] %s QUARANTINED after %d/%s failures — "
            "committed-off for %.0fs",
            self._service, self._threshold, reason.value,
            self._quarantine_duration_s,
        )

    def _maybe_reset_on_health(self, now: float) -> None:
        """Reset attempt counters if service has been healthy long enough."""
        if (
            self._last_healthy_mono > 0
            and (now - self._last_healthy_mono) >= self._health_reset_after_s
        ):
            changed = any(b.attempts > 0 for b in self._buckets.values())
            if changed:
                for bucket in self._buckets.values():
                    bucket.attempts = 0
                    bucket.last_attempt_mono = 0.0
                logger.info(
                    "[RecoveryBudget] %s — attempt counters reset after %.0fs of health",
                    self._service, now - self._last_healthy_mono,
                )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class RecoveryBudgetRegistry:
    """Process-wide registry of per-service RecoveryBudget instances."""

    def __init__(self) -> None:
        self._budgets: Dict[str, RecoveryBudget] = {}

    def get(self, service: str) -> RecoveryBudget:
        """Return (creating if needed) the RecoveryBudget for *service*."""
        if service not in self._budgets:
            self._budgets[service] = RecoveryBudget(service)
        return self._budgets[service]

    def all_services(self) -> Dict[str, RecoveryBudget]:
        """Return a snapshot dict of all registered budgets."""
        return dict(self._budgets)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_g_registry: Optional[RecoveryBudgetRegistry] = None


def get_recovery_budget_registry() -> RecoveryBudgetRegistry:
    """Return (lazily creating) the process-wide RecoveryBudgetRegistry."""
    global _g_registry
    if _g_registry is None:
        _g_registry = RecoveryBudgetRegistry()
    return _g_registry
