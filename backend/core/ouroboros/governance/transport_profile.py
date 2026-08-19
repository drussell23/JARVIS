"""How far away is the serving host, and how erratically does it deliver?

THE DEFECT THIS PREVENTS
------------------------
The streaming watchdog derives its steady-state deadline from MEASURED
per-token cost, and on a fast host that computes to ~2.0s. Over a Tailscale
DERP relay a 2-second gap between chunks can be entirely legitimate: WireGuard
over TCP relays bunch packets, so tokens arrive in bursts separated by silence
that has nothing to do with the model. The watchdog would sever a healthy
stream, penalise the physics ledger, and fail over to the laptop -- because
the operator was on hotel wifi.

Worse, the same host has TWO transports. Tailscale negotiates direct when it
can and relays when it cannot, and it can switch MID-STREAM. So a single
pre-flight RTT ping is not enough: a stream that begins direct at 0.4ms can be
relayed at 90ms three chunks later, and a deadline computed once at the start
would be wrong for the rest of the stream.

WHY JACOBSON/KARELS AND NOT A NEW ESTIMATOR
-------------------------------------------
This is exactly TCP's retransmission-timeout problem: pick a wait threshold
from noisy round-trip samples such that normal jitter never trips it. That
algorithm has been load-bearing on every TCP stack since 1988::

    rttvar = (1 - beta) * rttvar + beta * |srtt - sample|      (beta = 1/4)
    srtt   = (1 - alpha) * srtt  + alpha * sample              (alpha = 1/8)
    budget = srtt + K * rttvar                                 (K = 4)

`rttvar` is a mean-deviation tracker, and it is the part that handles
BURSTING. Packet bunching produces samples far from `srtt` in both directions;
each one inflates `rttvar`, which widens the budget superlinearly relative to
the mean. A relay that delivers 5 chunks instantly then pauses 800ms does not
move `srtt` much, but it moves `rttvar` a lot -- which is the correct response,
because the thing that changed is the VARIANCE, not the average.

MID-STREAM DEGRADATION, AND WHY THE ADAPTATION IS ASYMMETRIC
------------------------------------------------------------
Standard Jacobson/Karels adapts at one rate. Here the two directions have
different costs:

  * a budget that is too LOOSE delays detection of a genuinely wedged peer --
    recoverable, and the op still completes or fails on its own route budget;
  * a budget that is too TIGHT severs a HEALTHY stream, writes a timeout
    penalty into the physics ledger for a host that was fine, and re-routes
    work that did not need re-routing. That corruption then outlives the
    network event that caused it.

So samples that ARGUE FOR A WIDER budget are applied at a faster alpha than
samples that argue for a narrower one. Concretely: when the path degrades
direct -> DERP, the first few oversized gaps expand the budget within one or
two chunks. When it recovers DERP -> direct, the budget contracts slowly over
many chunks. Fast to distrust the network, slow to trust it again -- the same
asymmetry as the profiler's EWMA, chosen for the same reason.

THE TRANSPORT CLASS IS MEASURED, NOT DECLARED
---------------------------------------------
`transport_class()` buckets the smoothed RTT rather than shelling out to
`tailscale status --json`. Three reasons, in order of weight:

  1. it is a MEASUREMENT of the property that actually matters (latency),
     not a declaration about the tool that happens to provide it;
  2. it needs no Tailscale CLI on PATH, no subprocess on a hot path, and no
     parsing of another program's JSON contract;
  3. it generalises -- plain LAN, a different VPN, or a future transport all
     bucket correctly without new code.

Buckets carry HYSTERESIS. Without it an RTT hovering on a boundary would flip
the class every few seconds, and since the class is part of the physics-ledger
key, that would shred one host's measurements across two entries -- the same
conflation bug the hardware signature exists to prevent, one level down.

Python 3.9+, stdlib only. Never raises.
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("Ouroboros.TransportProfile")

ENABLED_ENV = "JARVIS_TRANSPORT_PROFILE_ENABLED"
K_ENV = "JARVIS_TRANSPORT_VARIANCE_K"
FLOOR_ENV = "JARVIS_TRANSPORT_MIN_FLOOR_S"

_TRUTHY = ("1", "true", "yes", "on")

#: Jacobson/Karels constants. Named rather than inlined so the provenance of
#: each is visible: these are RFC 6298's values, not tuning we invented.
_ALPHA = 1.0 / 8.0          # smoothing for the mean
_BETA = 1.0 / 4.0           # smoothing for the mean deviation
_ALPHA_EXPAND = 1.0 / 2.0   # OUR addition: faster when widening (see docstring)

#: (upper_bound_ms, class). Ordered. A smoothed RTT below the bound lands in
#: that class. Chosen to separate the transports that actually behave
#: differently, not to be pretty.
_CLASS_BUCKETS: Tuple[Tuple[float, str], ...] = (
    (2.0, "local"),    # loopback / same machine
    (15.0, "lan"),     # same subnet, or Tailscale direct on a LAN
    (60.0, "near"),    # direct over WAN, or a close relay
    (float("inf"), "far"),   # DERP relay, cellular, hotel wifi
)

#: Fraction of a bucket's width a sample must cross BEFORE the class changes.
#: Pure hysteresis: prevents a boundary-hugging RTT from splitting one host's
#: physics across two ledger keys.
_CLASS_HYSTERESIS = 0.25


def transport_profile_enabled() -> bool:
    """Master gate. Default ON. OFF returns a neutral profile whose floor is
    the static base, i.e. byte-identical prior behaviour. NEVER raises."""
    try:
        return os.environ.get(ENABLED_ENV, "1").strip().lower() in _TRUTHY
    except Exception:  # noqa: BLE001
        return True


def variance_k() -> float:
    """Multiplier on the mean deviation. Default 4.0 (RFC 6298's K).

    Raising it buys tolerance for bursty transports at the cost of slower
    stall detection. 4 is the value TCP has used for decades against exactly
    this kind of noise."""
    try:
        return max(1.0, float(os.environ.get(K_ENV, "4.0")))
    except (TypeError, ValueError):
        return 4.0


def min_floor_s() -> float:
    """Absolute lower bound on the transport contribution. Default 0.25s."""
    try:
        return max(0.0, float(os.environ.get(FLOOR_ENV, "0.25")))
    except (TypeError, ValueError):
        return 0.25


@dataclass(frozen=True)
class TransportReading:
    srtt_ms: float
    rttvar_ms: float
    budget_ms: float
    transport_class: str
    samples: int
    provenance: str          # "measured" | "seeded"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "srtt_ms": round(self.srtt_ms, 2),
            "rttvar_ms": round(self.rttvar_ms, 2),
            "budget_ms": round(self.budget_ms, 2),
            "transport_class": self.transport_class,
            "samples": self.samples,
            "provenance": self.provenance,
        }


class TransportProfile:
    """Rolling latency + jitter tracker for ONE endpoint. Thread-safe."""

    __slots__ = ("_lock", "_srtt", "_rttvar", "_n", "_class", "_endpoint")

    def __init__(self, endpoint: str = "") -> None:
        self._lock = threading.Lock()
        self._srtt: Optional[float] = None
        self._rttvar: float = 0.0
        self._n: int = 0
        self._class: str = ""
        self._endpoint = endpoint

    def observe(self, sample_ms: float) -> None:
        """Fold one inter-arrival (or RTT) sample in. NEVER raises.

        Called per streamed chunk, so it must stay O(1) and allocation-free.
        """
        try:
            s = float(sample_ms)
            if s < 0.0:
                return
            with self._lock:
                if self._srtt is None:
                    # RFC 6298 first-sample rule.
                    self._srtt = s
                    self._rttvar = s / 2.0
                else:
                    deviation = abs(self._srtt - s)
                    self._rttvar = ((1 - _BETA) * self._rttvar
                                    + _BETA * deviation)
                    # ASYMMETRY. A sample above the current mean argues that
                    # the path got worse; adopt it fast so a direct -> DERP
                    # switch widens the budget within a chunk or two. A sample
                    # below argues it got better; adopt it slowly, because
                    # being wrong in that direction severs healthy streams.
                    alpha = _ALPHA_EXPAND if s > self._srtt else _ALPHA
                    self._srtt = (1 - alpha) * self._srtt + alpha * s
                self._n += 1
        except Exception:  # noqa: BLE001
            pass

    def reading(self) -> TransportReading:
        """Current smoothed view. NEVER raises."""
        try:
            with self._lock:
                srtt = self._srtt
                rttvar = self._rttvar
                n = self._n
            if srtt is None:
                return TransportReading(0.0, 0.0, 0.0, "unknown", 0, "seeded")
            budget = srtt + variance_k() * rttvar
            return TransportReading(srtt, rttvar, budget,
                                    self._classify(srtt), n, "measured")
        except Exception:  # noqa: BLE001
            return TransportReading(0.0, 0.0, 0.0, "unknown", 0, "seeded")

    def _classify(self, srtt_ms: float) -> str:
        """Bucket the smoothed RTT, WITH HYSTERESIS.

        A class that flips on a boundary-hugging RTT would split one host's
        measurements across two physics-ledger keys — the same conflation the
        hardware signature exists to prevent, one level down. So a sample must
        cross a boundary by a margin before the class follows it.
        """
        try:
            with self._lock:
                current = self._class
            chosen = "far"
            for bound, name in _CLASS_BUCKETS:
                if srtt_ms < bound:
                    chosen = name
                    break
            if current and chosen != current:
                # Find the boundary between current and chosen, and require
                # the sample to be clearly past it.
                for i, (bound, name) in enumerate(_CLASS_BUCKETS):
                    if name == current and bound != float("inf"):
                        lo = _CLASS_BUCKETS[i - 1][0] if i else 0.0
                        width = max(1.0, bound - lo)
                        margin = width * _CLASS_HYSTERESIS
                        if lo - margin <= srtt_ms <= bound + margin:
                            return current      # inside the sticky band
                        break
            with self._lock:
                self._class = chosen
            return chosen
        except Exception:  # noqa: BLE001
            return "unknown"

    def floor_s(self) -> float:
        """Transport contribution to the inter-token stall floor, in seconds.

        This is ADDED to (not substituted for) the model-derived budget: a
        stall deadline must cover the time the model needs AND the time the
        network takes to deliver it. NEVER raises.
        """
        try:
            if not transport_profile_enabled():
                return 0.0
            r = self.reading()
            if r.provenance != "measured":
                return 0.0
            return max(min_floor_s(), r.budget_ms / 1000.0)
        except Exception:  # noqa: BLE001
            return 0.0

    def snapshot(self) -> Dict[str, Any]:
        d = self.reading().to_dict()
        d["endpoint"] = self._endpoint
        d["floor_s"] = round(self.floor_s(), 3)
        return d


_PROFILES: Dict[str, TransportProfile] = {}
_LOCK = threading.Lock()


def profile_for(endpoint: str) -> TransportProfile:
    """Per-endpoint profile. Two hosts have two networks. NEVER raises."""
    key = str(endpoint or "local")
    with _LOCK:
        p = _PROFILES.get(key)
        if p is None:
            p = TransportProfile(key)
            _PROFILES[key] = p
        return p


def transport_class_for(endpoint: str) -> str:
    """Measured transport class, or "" when unknown.

    Returned EMPTY rather than guessing, so a caller building a ledger key can
    omit the dimension instead of inventing one — an unmeasured transport must
    not silently become a bucket that later real measurements are mixed into.
    """
    try:
        if not transport_profile_enabled():
            return ""
        r = profile_for(endpoint).reading()
        return "" if r.provenance != "measured" else r.transport_class
    except Exception:  # noqa: BLE001
        return ""


def reset_for_tests() -> None:
    with _LOCK:
        _PROFILES.clear()
