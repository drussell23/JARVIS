"""Adaptive Cache Economics Engine (ACEE) — frequency-driven cache-write policy.

Replaces the static ``JARVIS_DW_PROMPT_CACHE_MIN_CHARS`` floor, which measured
the wrong variable.

The economics
-------------
A prefix used ``N`` times costs, in input-units:

    cached   = write_mult + (N - 1) * read_mult
    uncached = N

Caching is profitable iff ``cached < uncached``. Solving:

    write_mult - read_mult < N * (1 - read_mult)
    N > (write_mult - read_mult) / (1 - read_mult)

**Every term scales linearly with prefix length, so length CANCELS OUT.**
Profitability depends *only* on reuse count. With the defaults (write 1.25x,
read 0.1x) the break-even is N > 1.278, i.e. **any prefix reused even once**
pays for its write premium.

That is why the character floor was the wrong instrument: it blocked a
1,500-char Sentinel probe firing 40x/hour (hugely profitable) while waving
through a 15,000-char one-off (a guaranteed 0.25x loss). This engine decides on
observed reuse instead, and derives its own threshold from the live multipliers
— no hardcoded count anywhere.

First-sighting policy
---------------------
A prefix's first appearance carries no evidence of reuse, so it is NOT cached:
paying the write premium on a payload that never returns is the one way to lose
money here. From the second sighting within the TTL window the prefix has
demonstrated repetition and is cached. This is deliberately asymmetric — the
downside of a missed cache is a small opportunity cost, the downside of a wasted
write is a real charge.

Bounded by construction: the sliding window evicts on every observation, and a
hard entry cap protects against unbounded growth under signature churn.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from collections import deque
from typing import Deque, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

_SIG_LEN = 16


def acee_enabled() -> bool:
    """Master gate. Default TRUE — the static floor it replaces was actively
    mispricing. Rollback: ``JARVIS_DW_ACEE_ENABLED=false`` restores the legacy
    ``min_chars`` behaviour at the call site."""
    return os.environ.get(
        "JARVIS_DW_ACEE_ENABLED", "true",
    ).strip().lower() in ("1", "true", "yes", "on")


def _window_ttl_s() -> float:
    """Sliding-window span. Defaults to 3600s to MIRROR the provider cache TTL —
    counting reuse over a window longer than the cache itself would predict hits
    that have already expired."""
    try:
        return max(1.0, float(os.environ.get("JARVIS_DW_ACEE_WINDOW_S", "3600")))
    except (TypeError, ValueError):
        return 3600.0


def _max_entries() -> int:
    """Hard cap on tracked observations — the memory-leak backstop."""
    try:
        return max(16, int(os.environ.get("JARVIS_DW_ACEE_MAX_ENTRIES", "4096")))
    except (TypeError, ValueError):
        return 4096


def prompt_signature(prefix: str) -> str:
    """Stable content signature. Truncated SHA-256: collision risk is negligible
    at these volumes and the short form keeps the window compact."""
    try:
        return hashlib.sha256(prefix.encode("utf-8", "replace")).hexdigest()[:_SIG_LEN]
    except Exception:  # noqa: BLE001
        return ""


def break_even_uses(write_mult: float, read_mult: float) -> int:
    """Minimum total uses for a cache write to pay for itself, DERIVED from the
    live multipliers. Never hardcoded.

    Degenerate guards: a read multiplier at or above 1.0 means reads cost as
    much as fresh input, so caching can never profit -> effectively infinite.
    A write multiplier at or below the read multiplier means writing is free
    relative to reading -> profitable immediately."""
    try:
        if read_mult >= 1.0:
            return 1 << 30          # caching can never pay off
        if write_mult <= read_mult:
            return 1
        threshold = (write_mult - read_mult) / (1.0 - read_mult)
        return int(threshold) + 1   # strict inequality -> next whole use
    except Exception:  # noqa: BLE001
        return 2


class PromptSignatureTracker:
    """Thread-safe TTL sliding window over prompt-signature observations.

    Evicts on every observation, so memory is bounded by the window span rather
    than by process lifetime. Synchronous and O(evicted) — it sits on the
    generation path and must never block an event loop."""

    def __init__(
        self, *, ttl_s: Optional[float] = None, max_entries: Optional[int] = None,
    ) -> None:
        self._ttl = ttl_s if ttl_s is not None else _window_ttl_s()
        self._cap = max_entries if max_entries is not None else _max_entries()
        self._events: Deque[Tuple[str, float]] = deque()
        self._counts: Dict[str, int] = {}
        self._lock = threading.Lock()

    def _drop_oldest_locked(self) -> None:
        sig, _ts = self._events.popleft()
        n = self._counts.get(sig, 0) - 1
        if n > 0:
            self._counts[sig] = n
        else:
            self._counts.pop(sig, None)

    def _evict_locked(self, now: float) -> int:
        """TTL eviction only. The hard cap is enforced in :meth:`observe` AFTER
        the append — enforcing it here would leave room for exactly one
        over-cap entry, since the append follows."""
        cutoff = now - self._ttl
        dropped = 0
        while self._events and self._events[0][1] < cutoff:
            self._drop_oldest_locked()
            dropped += 1
        return dropped

    def _enforce_cap_locked(self) -> int:
        """Hard backstop: under pathological churn (every payload unique) the
        TTL alone cannot bound growth, so shed oldest-first regardless of age."""
        dropped = 0
        while len(self._events) > self._cap:
            self._drop_oldest_locked()
            dropped += 1
        return dropped

    def observe(self, signature: str, *, now: Optional[float] = None) -> int:
        """Record one sighting; return how many times this signature has been
        seen within the window INCLUDING this one."""
        if not signature:
            return 0
        ts = time.time() if now is None else float(now)
        with self._lock:
            self._evict_locked(ts)
            self._events.append((signature, ts))
            self._counts[signature] = self._counts.get(signature, 0) + 1
            # Cap AFTER the append, or the window ends up at cap+1.
            self._enforce_cap_locked()
            return self._counts.get(signature, 0)

    def frequency(self, signature: str, *, now: Optional[float] = None) -> int:
        """Sightings within the window WITHOUT recording a new one."""
        if not signature:
            return 0
        ts = time.time() if now is None else float(now)
        with self._lock:
            self._evict_locked(ts)
            return self._counts.get(signature, 0)

    def size(self) -> int:
        with self._lock:
            return len(self._events)

    def distinct(self) -> int:
        with self._lock:
            return len(self._counts)

    def clear(self) -> None:
        with self._lock:
            self._events.clear()
            self._counts.clear()


_DEFAULT_TRACKER: Optional[PromptSignatureTracker] = None
_TRACKER_LOCK = threading.Lock()


def get_default_tracker() -> PromptSignatureTracker:
    """Process-wide tracker. Lazily built so import stays side-effect free."""
    global _DEFAULT_TRACKER
    with _TRACKER_LOCK:
        if _DEFAULT_TRACKER is None:
            _DEFAULT_TRACKER = PromptSignatureTracker()
        return _DEFAULT_TRACKER


def reset_default_tracker() -> None:
    """Test seam — drop the singleton so suites cannot bleed into each other."""
    global _DEFAULT_TRACKER
    with _TRACKER_LOCK:
        _DEFAULT_TRACKER = None


def should_cache_write(
    prefix: str,
    *,
    write_mult: float,
    read_mult: float,
    tracker: Optional[PromptSignatureTracker] = None,
    now: Optional[float] = None,
    observe: bool = True,
) -> Tuple[bool, dict]:
    """Decide whether this prefix earns a cache write.

    Returns ``(decision, telemetry)``. Length is deliberately absent from the
    decision — it cancels out of the profitability inequality. NEVER raises: any
    fault returns ``False`` (behave as if uncached), because the failure mode of
    a bad decision is a real charge."""
    try:
        if not prefix or not isinstance(prefix, str):
            return (False, {"reason": "empty_prefix"})
        trk = tracker if tracker is not None else get_default_tracker()
        sig = prompt_signature(prefix)
        if not sig:
            return (False, {"reason": "signature_failed"})
        uses = (
            trk.observe(sig, now=now) if observe
            else trk.frequency(sig, now=now)
        )
        need = break_even_uses(write_mult, read_mult)
        decision = uses >= need
        return (decision, {
            "signature": sig,
            "uses_in_window": uses,
            "break_even_uses": need,
            "decision": "cache" if decision else "bypass",
            "reason": "reuse_observed" if decision else "insufficient_reuse",
            "chars": len(prefix),
        })
    except Exception:  # noqa: BLE001 — economics never break generation
        return (False, {"reason": "error"})


__all__ = [
    "PromptSignatureTracker",
    "acee_enabled",
    "break_even_uses",
    "get_default_tracker",
    "prompt_signature",
    "reset_default_tracker",
    "should_cache_write",
]
