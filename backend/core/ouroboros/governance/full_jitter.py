"""Phase 12.2 Slice A — Full-Jitter Exponential Backoff.

Single-source-of-truth helper for retry desynchronization. Replaces
exact-exponential backoff at every retry site so our retry waveform
doesn't synchronize with the global thundering herd of similar
agentic systems retrying the same DW endpoint.

The math (operator directive 2026-04-27):

    delay = random.uniform(0, min(cap_s, base_s * 2^attempt))

vs the rejected exact-exponential form:

    delay = min(cap_s, base_s * 2^attempt)

Why full jitter beats exact exponential under Little's Law:

  After a transient outage, every client doing exact-exponential
  retries synchronizes at the same offsets (t+10s, t+30s, t+70s ...).
  The recovered endpoint sees retry pulses each containing thousands
  of synchronized arrivals — queue depth = arrival rate × service
  time blows past server capacity instantly, the endpoint crashes
  again, the cycle repeats.

  Full jitter desynchronizes the waveform. Each retry falls into a
  uniform random window across the entire backoff range. Our payloads
  slip into the micro-gaps of DW's queue backlog instead of stacking
  on the herd's arrival wavefronts.

Authority surface:
  * ``full_jitter_backoff_s(attempt, *, base_s, cap_s, rng=None)`` —
    pure function, NEVER raises, deterministic when ``rng`` is a
    seeded ``random.Random`` instance.
  * ``full_jitter_enabled()`` — re-read at call time.

NEVER imports network code, NEVER allocates state, NEVER raises.
Pure stdlib (``random`` only).
"""
from __future__ import annotations

import os
import random as _random
from typing import Optional


# ---------------------------------------------------------------------------
# Master flag
# ---------------------------------------------------------------------------


def full_jitter_enabled() -> bool:
    """``JARVIS_TOPOLOGY_FULL_JITTER_ENABLED`` (default ``true`` —
    graduated in Phase 12.2 Slice E).

    Re-read at call time so monkeypatch works in tests + operators
    can flip live without re-init. Hot-revert path: ``export
    JARVIS_TOPOLOGY_FULL_JITTER_ENABLED=false`` returns retry sites
    to exact-exponential behavior immediately.

    The flag governs whether callers USE full_jitter_backoff_s. The
    function itself works regardless — callers branch on this flag
    when integrating it as a drop-in replacement for legacy backoff
    formulas, so a single env var unifies the rollout."""
    raw = os.environ.get(
        "JARVIS_TOPOLOGY_FULL_JITTER_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True  # graduated default
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Tunables (env-readable defaults; callers may override per-callsite)
# ---------------------------------------------------------------------------


def _default_base_s() -> float:
    """``JARVIS_TOPOLOGY_BACKOFF_BASE_S`` (default 10.0)."""
    try:
        return float(
            os.environ.get(
                "JARVIS_TOPOLOGY_BACKOFF_BASE_S", "10.0",
            ).strip()
        )
    except (ValueError, TypeError):
        return 10.0


def _default_cap_s() -> float:
    """``JARVIS_TOPOLOGY_BACKOFF_CAP_S`` (default 300.0).

    Maximum delay regardless of attempt count. A sentinel circuit
    breaker that keeps failing for 5+ minutes has bigger problems
    than a longer backoff can solve."""
    try:
        return float(
            os.environ.get(
                "JARVIS_TOPOLOGY_BACKOFF_CAP_S", "300.0",
            ).strip()
        )
    except (ValueError, TypeError):
        return 300.0


# ---------------------------------------------------------------------------
# Core function
# ---------------------------------------------------------------------------


def full_jitter_backoff_s(
    attempt: int,
    *,
    base_s: Optional[float] = None,
    cap_s: Optional[float] = None,
    rng: Optional[_random.Random] = None,
) -> float:
    """Compute a full-jitter backoff delay in seconds.

    Returns a uniform random float in ``[0, min(cap_s, base_s *
    2^max(0, attempt))]``.

    Parameters
    ----------
    attempt : int
        Retry attempt number (0-indexed). Negative values are clamped
        to 0 for safety. attempt=0 → range [0, base_s]. attempt=1 →
        [0, 2*base_s]. attempt=N → [0, min(cap, base*2^N)].
    base_s : float, optional
        Base delay scalar. Defaults to env ``JARVIS_TOPOLOGY_BACKOFF_BASE_S``
        (10.0).
    cap_s : float, optional
        Maximum delay regardless of attempt. Defaults to env
        ``JARVIS_TOPOLOGY_BACKOFF_CAP_S`` (300.0).
    rng : random.Random, optional
        Random source. ``None`` uses the module-level ``random``,
        which is non-deterministic. For test pinning, pass a seeded
        ``random.Random(seed)`` — same seed produces same delay
        sequence.

    Returns
    -------
    float
        Backoff delay in seconds, in [0, min(cap_s, base_s*2^attempt)].

    Notes
    -----
    NEVER raises. Negative ``attempt`` clamps to 0. Non-positive
    ``base_s`` / ``cap_s`` coerce to env defaults. Overflow on
    ``2^attempt`` clamps to ``cap_s``."""
    a = max(0, int(attempt) if not isinstance(attempt, bool) else 0)
    if base_s is None or base_s <= 0:
        base_s = _default_base_s()
    if cap_s is None or cap_s <= 0:
        cap_s = _default_cap_s()

    # 2^attempt overflow protection — for attempt large enough that
    # 2^attempt > cap_s/base_s, the upper bound is just cap_s. This
    # avoids OverflowError on absurd attempt values (e.g. attempt=1000)
    # without polluting the formula.
    try:
        scaled = base_s * (2 ** a)
    except OverflowError:
        scaled = cap_s
    upper = min(cap_s, scaled)

    if upper <= 0:
        return 0.0

    source = rng if rng is not None else _random
    return source.uniform(0.0, upper)


__all__ = [
    "full_jitter_backoff_s",
    "full_jitter_enabled",
]
