"""Little's Law idle verifier and ProactiveDrive state machine."""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional, Tuple


@dataclass
class QueueSample:
    timestamp: float
    depth: int
    processing_latency_ms: float


class LittlesLawVerifier:
    """Measures L = lambda*W across a rolling window.

    One instance per repo (JARVIS, Prime, Reactor). The ProactiveDrive
    requires ALL THREE to be idle simultaneously.
    """

    WINDOW_SECONDS = 120.0
    IDLE_L_RATIO = 0.30
    MIN_SAMPLES = 10

    def __init__(self, repo_name: str, max_queue_depth: int) -> None:
        self._repo = repo_name
        self._max_depth = max_queue_depth
        self._samples: Deque[QueueSample] = deque()

    def record(self, depth: int, processing_latency_ms: float) -> None:
        """Called by the event loop on each dequeue operation."""
        now = time.monotonic()
        self._samples.append(QueueSample(now, depth, processing_latency_ms))
        cutoff = now - self.WINDOW_SECONDS
        while self._samples and self._samples[0].timestamp < cutoff:
            self._samples.popleft()

    def compute_L(self) -> Optional[float]:
        """Compute L (average queue occupancy) via Little's Law.
        Returns None if insufficient samples.
        """
        if len(self._samples) < self.MIN_SAMPLES:
            return None
        window = self._samples[-1].timestamp - self._samples[0].timestamp
        if window <= 0:
            return None
        lam = len(self._samples) / window
        W = sum(s.processing_latency_ms for s in self._samples) / len(self._samples) / 1000.0
        return lam * W

    def is_idle(self) -> Tuple[bool, str]:
        """Returns (idle, reason_string) for observability."""
        L = self.compute_L()
        if L is None:
            return False, f"{self._repo}: insufficient samples ({len(self._samples)}/{self.MIN_SAMPLES})"
        threshold = self.IDLE_L_RATIO * self._max_depth
        if L < threshold:
            return True, f"{self._repo}: L={L:.3f} < threshold={threshold:.3f}"
        return False, f"{self._repo}: L={L:.3f} >= threshold={threshold:.3f}"


class ProactiveDrive:
    """State machine for Trinity's proactive mode.

    States: REACTIVE, MEASURING, ELIGIBLE, EXPLORING, COOLDOWN.
    Transitions are guarded by mathematical invariants, not timers.
    """

    STATES = ("REACTIVE", "MEASURING", "ELIGIBLE", "EXPLORING", "COOLDOWN")
    COOLDOWN_SECONDS = 3600.0
    MIN_ELIGIBLE_SECONDS = 60.0

    def __init__(
        self,
        jarvis_verifier: LittlesLawVerifier,
        prime_verifier: LittlesLawVerifier,
        reactor_verifier: LittlesLawVerifier,
    ) -> None:
        self._verifiers = {
            "jarvis": jarvis_verifier,
            "prime": prime_verifier,
            "reactor": reactor_verifier,
        }
        self._state = "REACTIVE"
        self._eligible_since: Optional[float] = None
        self._last_exploration_end: float = 0.0

    @property
    def state(self) -> str:
        return self._state

    def tick(self) -> Tuple[str, str]:
        """Called by a background coroutine every 10 seconds.
        Returns (new_state, reason) for telemetry emission.
        """
        now = time.monotonic()

        if self._state == "COOLDOWN":
            if now - self._last_exploration_end >= self.COOLDOWN_SECONDS:
                self._state = "REACTIVE"
                return self._state, "Cooldown expired"
            return self._state, "Still in cooldown"

        if self._state == "EXPLORING":
            return self._state, "Sentinel active"

        idle_results = {repo: v.is_idle() for repo, v in self._verifiers.items()}
        all_idle = all(ok for ok, _ in idle_results.values())
        reasons = "; ".join(msg for _, msg in idle_results.values())

        if not all_idle:
            self._eligible_since = None
            self._state = "MEASURING"
            return self._state, f"Not idle: {reasons}"

        if self._eligible_since is None:
            self._eligible_since = now
            self._state = "MEASURING"
            return self._state, f"Idle confirmed, starting eligibility timer: {reasons}"

        if now - self._eligible_since >= self.MIN_ELIGIBLE_SECONDS:
            self._state = "ELIGIBLE"
            return self._state, f"Eligible: {reasons}"

        remaining = self.MIN_ELIGIBLE_SECONDS - (now - self._eligible_since)
        return "MEASURING", f"Idle but not yet stable ({remaining:.0f}s remaining)"

    def begin_exploration(self) -> None:
        assert self._state == "ELIGIBLE", f"Cannot begin exploration from {self._state}"
        self._state = "EXPLORING"
        self._eligible_since = None

    def end_exploration(self) -> None:
        assert self._state == "EXPLORING", f"Cannot end exploration from {self._state}"
        self._state = "COOLDOWN"
        self._last_exploration_end = time.monotonic()
