"""
Partition-Aware Health Assessment v1.0
======================================

Phase 12 Disease 1: Partial partition handling.

Root cause: Components make health decisions based on outbound reachability
alone. When A can reach B but B cannot reach A (asymmetric partition),
both sides make conflicting recovery decisions — A promotes while B demotes,
causing flapping, env var inconsistency, and cascading failures.

This module provides:
  1. HealthVerdict enum — richer than bool (healthy/unhealthy).
  2. PartitionDetector — tracks bidirectional reachability history
     and detects asymmetric partitions via reverse-path verification.
  3. CoordinatedHealthDecision — multi-signal verdict (outbound check +
     reverse ping + recent history) that prevents unilateral promotion/demotion.
  4. AtomicEndpointState — versioned/generational endpoint state that
     replaces scattered env var mutations with a single atomic snapshot.

All convenience functions are FAIL-OPEN: on import errors or exceptions
they return non-blocking defaults so JARVIS never hangs on partition logic.

v276.0 Phase 12 hardening.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# ============================================================================
# Health Verdict
# ============================================================================

class HealthVerdict(IntEnum):
    """
    Richer-than-boolean health assessment.

    Ordering: HEALTHY > DEGRADED > UNKNOWN > PARTITIONED > UNREACHABLE
    Higher is healthier.  Comparisons like ``verdict >= HealthVerdict.DEGRADED``
    work naturally.
    """
    UNREACHABLE = 0
    PARTITIONED = 1
    UNKNOWN = 2
    DEGRADED = 3
    HEALTHY = 4


# ============================================================================
# Reachability Record
# ============================================================================

@dataclass
class _ReachabilityRecord:
    """Single directional reachability observation."""
    timestamp_mono: float
    reachable: bool
    latency_ms: float = 0.0
    error: str = ""


# ============================================================================
# Partition Detector
# ============================================================================

class PartitionDetector:
    """
    Detects asymmetric network partitions between two endpoints.

    Maintains a sliding window of reachability observations for both
    the forward path (self → remote) and reverse path (remote → self).
    An asymmetric partition is detected when one direction is consistently
    reachable while the other is not.

    Thread-safe.  All public methods are safe to call from any thread.
    """

    def __init__(
        self,
        component_id: str = "local",
        window_size: int = 0,
        partition_threshold: float = 0.0,
    ):
        self._component_id = component_id
        self._window_size = window_size or int(
            os.environ.get("JARVIS_PARTITION_WINDOW_SIZE", "10")
        )
        self._partition_threshold = partition_threshold or float(
            os.environ.get("JARVIS_PARTITION_THRESHOLD", "0.7")
        )
        # Forward: self → remote
        self._forward: List[_ReachabilityRecord] = []
        # Reverse: remote → self (from reverse-ping responses)
        self._reverse: List[_ReachabilityRecord] = []
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Record observations
    # ------------------------------------------------------------------

    def record_forward(self, reachable: bool, latency_ms: float = 0.0, error: str = "") -> None:
        """Record an outbound reachability observation (self → remote)."""
        rec = _ReachabilityRecord(
            timestamp_mono=time.monotonic(),
            reachable=reachable,
            latency_ms=latency_ms,
            error=error,
        )
        with self._lock:
            self._forward.append(rec)
            if len(self._forward) > self._window_size:
                self._forward = self._forward[-self._window_size:]

    def record_reverse(self, reachable: bool, latency_ms: float = 0.0, error: str = "") -> None:
        """Record a reverse reachability observation (remote → self)."""
        rec = _ReachabilityRecord(
            timestamp_mono=time.monotonic(),
            reachable=reachable,
            latency_ms=latency_ms,
            error=error,
        )
        with self._lock:
            self._reverse.append(rec)
            if len(self._reverse) > self._window_size:
                self._reverse = self._reverse[-self._window_size:]

    # ------------------------------------------------------------------
    # Assessment
    # ------------------------------------------------------------------

    def _reachability_ratio(self, records: List[_ReachabilityRecord]) -> float:
        """Fraction of recent observations that were reachable (0.0–1.0)."""
        if not records:
            return 0.5  # unknown → neutral
        window = records[-self._window_size:]
        return sum(1 for r in window if r.reachable) / len(window)

    def is_partitioned(self) -> Tuple[bool, str]:
        """
        Returns (is_partitioned, reason).

        An asymmetric partition is detected when one direction is mostly
        reachable (>= threshold) and the other is mostly unreachable
        (< 1 - threshold).
        """
        with self._lock:
            fwd_ratio = self._reachability_ratio(self._forward)
            rev_ratio = self._reachability_ratio(self._reverse)

        inv_threshold = 1.0 - self._partition_threshold

        # Case 1: forward OK but reverse failing
        if fwd_ratio >= self._partition_threshold and rev_ratio <= inv_threshold:
            return True, (
                f"Asymmetric partition: forward={fwd_ratio:.0%} OK, "
                f"reverse={rev_ratio:.0%} failing"
            )

        # Case 2: reverse OK but forward failing
        if rev_ratio >= self._partition_threshold and fwd_ratio <= inv_threshold:
            return True, (
                f"Asymmetric partition: reverse={rev_ratio:.0%} OK, "
                f"forward={fwd_ratio:.0%} failing"
            )

        return False, ""

    def assess(self) -> HealthVerdict:
        """
        Compute a HealthVerdict from current observations.

        - Both directions good → HEALTHY
        - Both directions bad → UNREACHABLE
        - Asymmetric → PARTITIONED
        - Insufficient data → UNKNOWN
        - Forward OK but no reverse data → DEGRADED (can't confirm bidirectional)
        """
        with self._lock:
            fwd_ratio = self._reachability_ratio(self._forward)
            rev_ratio = self._reachability_ratio(self._reverse)
            fwd_count = len(self._forward)
            rev_count = len(self._reverse)

        # Need at least 1 forward observation to say anything
        if fwd_count == 0:
            return HealthVerdict.UNKNOWN

        # Both directions reachable
        if fwd_ratio >= self._partition_threshold:
            if rev_count == 0:
                # Forward OK but no reverse data — can't confirm bidirectional
                return HealthVerdict.DEGRADED
            if rev_ratio >= self._partition_threshold:
                return HealthVerdict.HEALTHY
            # Forward OK, reverse failing → partition
            return HealthVerdict.PARTITIONED

        # Forward failing
        if rev_count > 0 and rev_ratio >= self._partition_threshold:
            # Reverse OK, forward failing → partition
            return HealthVerdict.PARTITIONED

        return HealthVerdict.UNREACHABLE

    def reset(self) -> None:
        """Clear all observations (e.g. after endpoint change)."""
        with self._lock:
            self._forward.clear()
            self._reverse.clear()


# ============================================================================
# Coordinated Health Decision
# ============================================================================

@dataclass
class CoordinatedHealthDecision:
    """
    Multi-signal health decision that prevents unilateral promotion/demotion.

    Instead of ``if health_check_ok: promote()``, consumers build a decision
    from multiple signals and check ``decision.should_promote()`` /
    ``decision.should_demote()``.
    """

    outbound_ok: bool = False
    reverse_ok: Optional[bool] = None  # None = not checked
    partition_verdict: HealthVerdict = HealthVerdict.UNKNOWN
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    last_transition_mono: float = 0.0
    cooldown_s: float = 0.0

    def should_promote(self) -> Tuple[bool, str]:
        """
        Returns (should_promote, reason).

        Promotion requires:
          1. Outbound health check passed
          2. No asymmetric partition detected
          3. Cooldown elapsed since last transition
          4. At least 2 consecutive successes (anti-flap)
        """
        if not self.outbound_ok:
            return False, "outbound health check failed"

        if self.partition_verdict == HealthVerdict.PARTITIONED:
            return False, "asymmetric partition detected"

        if self.partition_verdict == HealthVerdict.UNREACHABLE:
            return False, "endpoint unreachable"

        # Cooldown
        if self.cooldown_s > 0 and self.last_transition_mono > 0:
            elapsed = time.monotonic() - self.last_transition_mono
            if elapsed < self.cooldown_s:
                return False, f"within cooldown ({elapsed:.0f}s < {self.cooldown_s:.0f}s)"

        # Anti-flap: require 2+ consecutive successes
        min_successes = int(os.environ.get("JARVIS_PROMOTE_MIN_SUCCESSES", "2"))
        if self.consecutive_successes < min_successes:
            return False, (
                f"insufficient consecutive successes "
                f"({self.consecutive_successes} < {min_successes})"
            )

        return True, "all checks passed"

    def should_demote(self) -> Tuple[bool, str]:
        """
        Returns (should_demote, reason).

        Demotion requires:
          1. Outbound health check failed OR partition detected
          2. Cooldown elapsed since last transition
          3. At least 3 consecutive failures (anti-flap)
        """
        # Partition is grounds for demotion even if outbound seems OK
        if self.partition_verdict == HealthVerdict.PARTITIONED:
            # Still respect cooldown
            if self.cooldown_s > 0 and self.last_transition_mono > 0:
                elapsed = time.monotonic() - self.last_transition_mono
                if elapsed < self.cooldown_s:
                    return False, f"partition detected but within cooldown ({elapsed:.0f}s)"
            return True, "asymmetric partition detected"

        if self.outbound_ok:
            return False, "outbound health check passed"

        # Cooldown
        if self.cooldown_s > 0 and self.last_transition_mono > 0:
            elapsed = time.monotonic() - self.last_transition_mono
            if elapsed < self.cooldown_s:
                return False, f"within cooldown ({elapsed:.0f}s < {self.cooldown_s:.0f}s)"

        # Anti-flap: require 3+ consecutive failures
        min_failures = int(os.environ.get("JARVIS_DEMOTE_MIN_FAILURES", "3"))
        if self.consecutive_failures < min_failures:
            return False, (
                f"insufficient consecutive failures "
                f"({self.consecutive_failures} < {min_failures})"
            )

        return True, "outbound failed with sufficient consecutive failures"


# ============================================================================
# Atomic Endpoint State
# ============================================================================

@dataclass
class AtomicEndpointState:
    """
    Versioned, generational endpoint state.

    Replaces scattered env var mutations (JARVIS_PRIME_URL, JARVIS_INVINCIBLE_NODE_IP,
    JARVIS_HOLLOW_CLIENT_ACTIVE, etc.) with a single atomic snapshot.
    Consumers read from the snapshot; only the coordinator writes to it.

    The ``generation`` counter monotonically increases on every state change.
    Stale updates (generation < current) are silently dropped.

    Thread-safe.
    """

    host: Optional[str] = None
    port: int = 8000
    is_gcp: bool = False
    generation: int = 0
    source: str = ""
    timestamp_mono: float = field(default_factory=time.monotonic)

    # Class-level singleton
    _instance: Optional["AtomicEndpointState"] = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @classmethod
    def get_current(cls) -> "AtomicEndpointState":
        """Get the current endpoint state (singleton). Fail-open: returns empty state."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def update(
        cls,
        host: Optional[str],
        port: int,
        is_gcp: bool,
        source: str = "",
    ) -> "AtomicEndpointState":
        """
        Atomically update the endpoint state. Increments generation.

        Returns the new state.
        """
        current = cls.get_current()
        with current._lock:
            new_gen = current.generation + 1
            new_state = AtomicEndpointState(
                host=host,
                port=port,
                is_gcp=is_gcp,
                generation=new_gen,
                source=source,
                timestamp_mono=time.monotonic(),
            )
            cls._instance = new_state
            logger.debug(
                "[AtomicEndpointState] Updated gen=%d host=%s port=%d gcp=%s source=%s",
                new_gen, host, port, is_gcp, source,
            )
            return new_state

    @classmethod
    def try_update(
        cls,
        host: Optional[str],
        port: int,
        is_gcp: bool,
        expected_generation: int,
        source: str = "",
    ) -> Tuple[bool, "AtomicEndpointState"]:
        """
        CAS-style update: only succeeds if current generation matches expected.

        Returns (success, current_state).
        """
        current = cls.get_current()
        with current._lock:
            if current.generation != expected_generation:
                logger.debug(
                    "[AtomicEndpointState] CAS failed: expected gen=%d, current gen=%d",
                    expected_generation, current.generation,
                )
                return False, current
            new_state = AtomicEndpointState(
                host=host,
                port=port,
                is_gcp=is_gcp,
                generation=current.generation + 1,
                source=source,
                timestamp_mono=time.monotonic(),
            )
            cls._instance = new_state
            return True, new_state

    @classmethod
    def reset(cls) -> None:
        """Reset to empty state (for testing)."""
        cls._instance = None

    @property
    def url(self) -> Optional[str]:
        """Full URL if host is set, else None."""
        if self.host:
            return f"http://{self.host}:{self.port}"
        return None

    def is_stale_for(self, generation: int) -> bool:
        """Check if this state is stale relative to a given generation."""
        return self.generation < generation


# ============================================================================
# Convenience Functions (FAIL-OPEN)
# ============================================================================

def assess_endpoint_health(detector: Optional[PartitionDetector] = None) -> HealthVerdict:
    """
    Fail-open health assessment.

    Returns HealthVerdict.UNKNOWN on any error (never blocks).
    """
    try:
        if detector is None:
            return HealthVerdict.UNKNOWN
        return detector.assess()
    except Exception:
        return HealthVerdict.UNKNOWN


def is_partition_detected(detector: Optional[PartitionDetector] = None) -> Tuple[bool, str]:
    """
    Fail-open partition check.

    Returns (False, "") on any error (never blocks promotion).
    """
    try:
        if detector is None:
            return False, ""
        return detector.is_partitioned()
    except Exception:
        return False, ""


def build_health_decision(
    outbound_ok: bool,
    detector: Optional[PartitionDetector] = None,
    consecutive_failures: int = 0,
    consecutive_successes: int = 0,
    last_transition_mono: float = 0.0,
    cooldown_s: float = 0.0,
) -> CoordinatedHealthDecision:
    """
    Build a CoordinatedHealthDecision from available signals.

    Fail-open: on any error, returns a decision with only outbound_ok populated.
    """
    try:
        verdict = assess_endpoint_health(detector)
        reverse_ok: Optional[bool] = None
        if detector is not None:
            # Reverse is OK if verdict is HEALTHY (both directions good)
            reverse_ok = verdict == HealthVerdict.HEALTHY

        return CoordinatedHealthDecision(
            outbound_ok=outbound_ok,
            reverse_ok=reverse_ok,
            partition_verdict=verdict,
            consecutive_failures=consecutive_failures,
            consecutive_successes=consecutive_successes,
            last_transition_mono=last_transition_mono,
            cooldown_s=cooldown_s,
        )
    except Exception:
        return CoordinatedHealthDecision(outbound_ok=outbound_ok)
