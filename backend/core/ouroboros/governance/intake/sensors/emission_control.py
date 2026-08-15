"""emission_control — what every proactive sensor needs to not become noise.

THE SHAPE THAT KEEPS RECURRING
------------------------------
A sensor that finds a persistent condition and emits one signal per finding
is a denial-of-service against O+V's own intake: the queue fills with a
hundred rows, real work sits behind them, and the operator learns to skip the
whole channel. Every proactive sensor here therefore composes the same two
bounds, and they are NOT redundant:

    a per-scan CAP        bounds a burst of findings in one reading
    a token BUCKET        bounds a sustained drip of DISTINCT findings

Either alone leaves the other attack open — a burst defeats the bucket's
average, and a slow drip of distinct findings defeats the cap.

``cage_hygiene_sensor`` and ``liveness_sensor`` each grew their own copy of
the bucket, byte-for-byte apart from the env names they read. This is the
third arrival of the same primitive, which is the point at which a copy stops
being cheaper than an abstraction: the two copies had already begun to drift
(one re-read its capacity on every take, the other did not), and a fix to the
refill arithmetic would have had to land twice.

WHY THE LIMITS ARE CALLABLES AND NOT NUMBERS
--------------------------------------------
Every knob in this repo is env-driven and live: an operator who widens a
bucket mid-session must not have to restart the daemon to be believed. So the
bucket holds *functions* that read the environment, not values captured at
construction. Each sensor keeps its own env vocabulary — the abstraction is
the arithmetic, never the policy.

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import os
import time
from typing import Callable

EMISSION_CONTROL_SCHEMA_VERSION: str = "emission_control.1"

__all__ = [
    "EMISSION_CONTROL_SCHEMA_VERSION",
    "TokenBucket",
    "env_flag",
    "env_num",
]


def env_flag(name: str, default: str = "1") -> bool:
    """A boolean knob. NEVER raises — an unreadable environment is not a
    reason for a sensor to change behaviour, so the default stands."""
    try:
        return os.environ.get(name, default).strip().lower() not in (
            "0", "false", "no", "off", "")
    except Exception:  # noqa: BLE001
        return default.strip().lower() not in ("0", "false", "no", "off", "")


def env_num(name: str, default: float, lo: float, hi: float) -> float:
    """A numeric knob, CLAMPED. NEVER raises.

    Clamping rather than validating is deliberate: a typo'd interval of ``0``
    would otherwise turn a five-minute audit into a spin loop, and a sensor
    that can be turned into a fork bomb by a stray keystroke is a liability
    regardless of how correct its findings are.
    """
    try:
        raw = os.environ.get(name, "")
        return min(hi, max(lo, float(raw.strip() or default)))
    except Exception:  # noqa: BLE001
        return min(hi, max(lo, default))


class TokenBucket:
    """Bounds emissions per unit TIME. NEVER raises.

    ``capacity`` and ``refill_per_s`` are read on every take so a live env
    change is honoured without a restart. A bucket whose limits were frozen
    at construction would silently ignore the operator who just widened it,
    which is the failure mode that makes people distrust knobs.

    Refusal is expressed as ``False`` rather than an exception: the caller is
    always a best-effort emission path, and a throttle that can raise turns a
    rate limit into an outage.
    """

    __slots__ = ("_capacity", "_refill_per_s", "_tokens", "_last")

    def __init__(self, capacity: Callable[[], float],
                 refill_per_s: Callable[[], float]) -> None:
        self._capacity = capacity
        self._refill_per_s = refill_per_s
        self._tokens = self._cap()
        self._last = time.monotonic()

    def _cap(self) -> float:
        try:
            return max(0.0, float(self._capacity()))
        except Exception:  # noqa: BLE001
            return 0.0

    def _refill(self) -> float:
        try:
            return max(0.0, float(self._refill_per_s()))
        except Exception:  # noqa: BLE001
            return 0.0

    def take(self) -> bool:
        """Consume one token, or refuse."""
        try:
            now = time.monotonic()
            cap = self._cap()
            self._tokens = min(cap, self._tokens + (now - self._last) * self._refill())
            self._last = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            return False
        except Exception:  # noqa: BLE001
            return False

    @property
    def tokens(self) -> float:
        return self._tokens
