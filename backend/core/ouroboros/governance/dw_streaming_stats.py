"""Slice 188 Phase 3 — O(1) streaming statistics for the proactive engine.

Eradicates the algorithmic bottlenecks identified in the Slice-188 research:
  * ``P2Quantile`` — the P² algorithm: a streaming p95 in O(1) per update, constant space, NO
    ``sorted()`` (the DwLatencyTracker hot-path cost).
  * ``DecayingRate`` — an exponentially-decaying event rate (λ) updated incrementally in O(1)
    instead of the predictor's O(n) ``Σ weight·decay`` ring loop. Also durable across restarts
    (decay-forward from the persisted timestamp), which dissolves the cold-start blindness.
  * ``HawkesIntensity`` — a self-exciting point-process intensity. Unlike Poisson (memoryless),
    a rupture RAISES the near-term intensity, then decays — modelling DW's actual bursty,
    correlated rupture STORMS so the cortex can preempt a storm as it begins, not after.

Pure + deterministic + dependency-free (no numpy). NEVER raise out of the read methods.
"""
from __future__ import annotations

import math
from typing import List, Optional


class P2Quantile:
    """P² streaming quantile estimator (Jain & Chlamtac 1985). O(1) update, 5 markers, no sort."""

    def __init__(self, p: float = 0.95) -> None:
        self.p = min(0.999, max(0.001, float(p)))
        self._init: List[float] = []
        self._q: List[float] = []   # marker heights
        self._n: List[float] = []   # marker positions
        self._np: List[float] = []  # desired positions
        self._dn: List[float] = []  # desired-position increments
        self._count = 0

    def update(self, x: float) -> None:
        try:
            x = float(x)
        except Exception:  # noqa: BLE001
            return
        self._count += 1
        if len(self._q) < 5:
            self._init.append(x)
            if len(self._init) == 5:
                self._init.sort()
                self._q = list(self._init)
                self._n = [1.0, 2.0, 3.0, 4.0, 5.0]
                p = self.p
                self._np = [1.0, 1 + 2 * p, 1 + 4 * p, 3 + 2 * p, 5.0]
                self._dn = [0.0, p / 2.0, p, (1 + p) / 2.0, 1.0]
            return

        # locate cell k
        if x < self._q[0]:
            self._q[0] = x
            k = 0
        elif x >= self._q[4]:
            self._q[4] = x
            k = 3
        else:
            k = 3
            for i in range(4):
                if self._q[i] <= x < self._q[i + 1]:
                    k = i
                    break

        for i in range(k + 1, 5):
            self._n[i] += 1
        for i in range(5):
            self._np[i] += self._dn[i]

        for i in range(1, 4):
            d = self._np[i] - self._n[i]
            if (d >= 1 and (self._n[i + 1] - self._n[i]) > 1) or (
                d <= -1 and (self._n[i - 1] - self._n[i]) < -1
            ):
                sd = 1.0 if d >= 0 else -1.0
                qp = self._parabolic(i, sd)
                if self._q[i - 1] < qp < self._q[i + 1]:
                    self._q[i] = qp
                else:
                    self._q[i] = self._q[i] + sd * (
                        self._q[int(i + sd)] - self._q[i]
                    ) / (self._n[int(i + sd)] - self._n[i])
                self._n[i] += sd

    def _parabolic(self, i: int, d: float) -> float:
        n = self._n
        q = self._q
        return q[i] + d / (n[i + 1] - n[i - 1]) * (
            (n[i] - n[i - 1] + d) * (q[i + 1] - q[i]) / (n[i + 1] - n[i])
            + (n[i + 1] - n[i] - d) * (q[i] - q[i - 1]) / (n[i] - n[i - 1])
        )

    def value(self) -> Optional[float]:
        """Current p-quantile estimate. NEVER raises."""
        try:
            if len(self._q) == 5:
                return self._q[2]
            if not self._init:
                return None
            s = sorted(self._init)
            return s[min(len(s) - 1, int(self.p * len(s)))]
        except Exception:  # noqa: BLE001
            return None

    @property
    def count(self) -> int:
        return self._count


class DecayingRate:
    """Exponentially-decaying event rate (λ), O(1) per event. λ decays with half-life ``halflife_s``
    and is incremented by ``weight`` on each event. Durable: ``snapshot()``/``restore()`` carry the
    rate + last-update time across restarts (decay-forward), dissolving cold-start blindness."""

    def __init__(self, halflife_s: float = 300.0) -> None:
        self._halflife = max(1e-3, float(halflife_s))
        self._lambda = 0.0
        self._last_t: Optional[float] = None

    def _decay_to(self, now: float) -> None:
        if self._last_t is None:
            self._last_t = now
            return
        dt = now - self._last_t
        if dt > 0:
            self._lambda *= math.exp(-math.log(2.0) * dt / self._halflife)
            self._last_t = now

    def observe(self, now: float, weight: float = 1.0) -> None:
        try:
            self._decay_to(float(now))
            self._lambda += float(weight)
        except Exception:  # noqa: BLE001
            pass

    def rate(self, now: float) -> float:
        try:
            self._decay_to(float(now))
            return self._lambda
        except Exception:  # noqa: BLE001
            return self._lambda

    def snapshot(self) -> dict:
        return {"lambda": self._lambda, "last_t": self._last_t, "halflife_s": self._halflife}

    def restore(self, snap: dict) -> None:
        try:
            self._lambda = float(snap.get("lambda", 0.0))
            self._last_t = snap.get("last_t", None)
            self._halflife = max(1e-3, float(snap.get("halflife_s", self._halflife)))
        except Exception:  # noqa: BLE001
            pass


class HawkesIntensity:
    """Self-exciting (Hawkes) intensity: λ(t) = μ + Σ α·exp(-β·(t−tᵢ)). A rupture RAISES the
    near-term intensity (excitation ``alpha``) which decays at rate ``beta`` — modelling DW's
    bursty, correlated STORMS (Poisson can't). O(1) incremental via a decaying excitation term."""

    def __init__(self, mu: float = 0.0, alpha: float = 1.0, beta: float = 1.0 / 60.0) -> None:
        self._mu = max(0.0, float(mu))
        self._alpha = max(0.0, float(alpha))
        self._beta = max(1e-6, float(beta))
        self._excite = 0.0
        self._last_t: Optional[float] = None

    def _decay_to(self, now: float) -> None:
        if self._last_t is None:
            self._last_t = now
            return
        dt = now - self._last_t
        if dt > 0:
            self._excite *= math.exp(-self._beta * dt)
            self._last_t = now

    def observe(self, now: float, weight: float = 1.0) -> None:
        try:
            self._decay_to(float(now))
            self._excite += self._alpha * float(weight)
        except Exception:  # noqa: BLE001
            pass

    def intensity(self, now: float) -> float:
        """Current conditional intensity λ(t) — high right after a burst, decaying toward μ."""
        try:
            self._decay_to(float(now))
            return self._mu + self._excite
        except Exception:  # noqa: BLE001
            return self._mu

    def storm_probability(self, now: float, horizon_s: float) -> float:
        """P(≥1 event in the next ``horizon_s``) from the current intensity (Poisson approx over
        the short horizon where intensity is ~constant): 1 − exp(−λ·horizon). NEVER raises."""
        try:
            lam = self.intensity(now)
            return 1.0 - math.exp(-max(0.0, lam) * max(0.0, float(horizon_s)))
        except Exception:  # noqa: BLE001
            return 0.0
