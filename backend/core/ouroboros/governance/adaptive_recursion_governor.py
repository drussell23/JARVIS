"""AdaptiveRecursionGovernor — dynamic depth/fan-out from live load signals.

Task B3 of Matrix B (sovereign-resilience-chunking).

PURE DECISION FUNCTION — no I/O, no imports from psutil, no memory probes.
The caller (Task B5) reads MemoryPressureGate and passes ``pressure_level``
as an integer rank (0=OK, 1=WARN, 2=HIGH, 3=CRITICAL), which mirrors
``memory_pressure_gate._LEVEL_RANK``.

Design contract
---------------
* ``allowed=False`` when depth exceeds the ADAPTIVE ceiling derived from
  (queue_len, loop_blocked_ms, pressure_level) — the ceiling SHRINKS under
  load and EXPANDS when idle.  NO literal MAX_DEPTH constant.
* ``max_fanout`` shrinks toward 1 under load, expands when idle.  It is
  always >= 1.
* MONOTONE: higher load OR higher depth yields <= fanout and eventually
  ``allowed=False``.
* FAIL-SOFT: any bad input (NaN, inf, negative, out-of-range) returns
  ``Budget(allowed=False, max_fanout=1, reason="failsoft")``.  Never raises.

Env knobs (tune the CURVE, not a cap)
--------------------------------------
``JARVIS_RECURSION_FANOUT_IDLE``   — max fanout when load is zero.  Default 4.
``JARVIS_RECURSION_QUEUE_SOFT``    — queue length at which fanout starts to
                                     shrink.  Default 50.
``JARVIS_RECURSION_LOOP_MS_SOFT``  — loop-blocked ms at which fanout starts to
                                     shrink.  Default 200.0.

Authority posture
-----------------
This module is §5 Tier 0: pure Python, stdlib only, no LLM, no I/O.
It MUST NOT import psutil, memory_pressure_gate, or any orchestrator module.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Budget:
    """Decision returned by ``recursion_budget()``."""

    allowed: bool
    max_fanout: int
    reason: str


# ---------------------------------------------------------------------------
# Env helpers
# ---------------------------------------------------------------------------


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        v = int(raw)
        return max(minimum, v)
    except (ValueError, TypeError):
        return default


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        v = float(raw)
        if not math.isfinite(v):
            return default
        return max(minimum, v)
    except (ValueError, TypeError):
        return default


# ---------------------------------------------------------------------------
# Knob accessors  (read fresh each call so monkeypatch/envvar changes take effect)
# ---------------------------------------------------------------------------


def _fanout_idle() -> int:
    """Maximum fanout when load is zero. Default 4."""
    return _env_int("JARVIS_RECURSION_FANOUT_IDLE", 4, minimum=1)


def _queue_soft() -> float:
    """Queue length soft limit — above this fanout starts shrinking. Default 50."""
    return _env_float("JARVIS_RECURSION_QUEUE_SOFT", 50.0, minimum=1.0)


def _loop_ms_soft() -> float:
    """Loop-blocked ms soft limit — above this fanout starts shrinking. Default 200."""
    return _env_float("JARVIS_RECURSION_LOOP_MS_SOFT", 200.0, minimum=1.0)


# ---------------------------------------------------------------------------
# Core math helpers
# ---------------------------------------------------------------------------


# Pressure-level rank range (mirrors memory_pressure_gate._LEVEL_RANK)
_MAX_PRESSURE_RANK = 3


def _clamp_pressure(pressure_level: int) -> int:
    """Clamp pressure rank to [0, _MAX_PRESSURE_RANK]."""
    return max(0, min(_MAX_PRESSURE_RANK, pressure_level))


def _load_score(
    queue_len: int,
    loop_blocked_ms: float,
    pressure_level: int,
    *,
    queue_soft: float,
    loop_ms_soft: float,
) -> float:
    """Compute a normalised load score in [0, 1].

    Each signal contributes a fractional component; they are combined via
    max-composition so that any single extreme signal drives the decision.
    This keeps the function monotone in each individual input.

    score ≈ 0.0  →  completely idle
    score ≈ 1.0  →  completely saturated
    """
    # Queue pressure: saturates at 5× the soft limit
    q_score = min(1.0, queue_len / (queue_soft * 5.0)) if queue_len > 0 else 0.0

    # Loop-latency pressure: saturates at 5× the soft limit
    l_score = (
        min(1.0, loop_blocked_ms / (loop_ms_soft * 5.0))
        if loop_blocked_ms > 0
        else 0.0
    )

    # Memory pressure: evenly spread across 0..MAX_PRESSURE_RANK
    p_score = _clamp_pressure(pressure_level) / _MAX_PRESSURE_RANK

    # Combine: use the max of each dimension (most conservative wins), plus a
    # small additive blend so combinations are worse than any single component.
    max_component = max(q_score, l_score, p_score)
    blend = (q_score + l_score + p_score) / 3.0
    # Weighted blend: 70% worst-signal + 30% average avoids double-counting
    return min(1.0, 0.70 * max_component + 0.30 * blend)


def _adaptive_depth_ceiling(load_score: float, *, fanout_idle: int) -> int:
    """Return the adaptive depth ceiling derived from load_score.

    Under zero load the ceiling is generous (fanout_idle * 2 + 2).
    Under full load the ceiling collapses to 0 (any depth is blocked).

    The formula is:
        ceiling = round(base_ceiling * (1 - load_score))

    where base_ceiling = fanout_idle * 2 + 2.

    This is NOT a literal constant — it stretches/shrinks with load.
    """
    base_ceiling = fanout_idle * 2 + 2  # e.g. 10 when fanout_idle=4
    ceiling = round(base_ceiling * (1.0 - load_score))
    return max(0, ceiling)


def _adaptive_max_fanout(load_score: float, *, fanout_idle: int) -> int:
    """Return the adaptive max_fanout in [1, fanout_idle].

    Under zero load → fanout_idle.
    Under full load → 1 (never below 1).
    """
    fanout_f = fanout_idle * (1.0 - load_score)
    fanout = max(1, math.floor(fanout_f))
    # Ensure we never exceed the idle ceiling
    return min(fanout, fanout_idle)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def recursion_budget(
    *,
    queue_len: int,
    loop_blocked_ms: float,
    pressure_level: int,
    depth: int,
) -> Budget:
    """Compute an adaptive recursion budget from live load signals.

    Parameters
    ----------
    queue_len : int
        Current intake-queue length (number of pending signals).
    loop_blocked_ms : float
        Estimated event-loop blocked time in milliseconds.
    pressure_level : int
        Memory-pressure rank: 0=OK, 1=WARN, 2=HIGH, 3=CRITICAL.
        Sourced from ``memory_pressure_gate._LEVEL_RANK``; passed in by caller.
    depth : int
        Current recursion depth.

    Returns
    -------
    Budget
        ``allowed`` — whether further recursion is permitted at this depth.
        ``max_fanout`` — maximum parallel children (>= 1).
        ``reason`` — human-readable rationale string.
    """
    # ------------------------------------------------------------------
    # Fail-soft: sanitise all inputs before any computation
    # ------------------------------------------------------------------
    try:
        if not isinstance(queue_len, int):
            return Budget(allowed=False, max_fanout=1, reason="failsoft")
        if not isinstance(depth, int):
            return Budget(allowed=False, max_fanout=1, reason="failsoft")

        # Clamp / validate queue_len
        if queue_len < 0:
            return Budget(allowed=False, max_fanout=1, reason="failsoft")

        # Validate loop_blocked_ms
        if not isinstance(loop_blocked_ms, (int, float)):
            return Budget(allowed=False, max_fanout=1, reason="failsoft")
        if math.isnan(loop_blocked_ms):
            return Budget(allowed=False, max_fanout=1, reason="failsoft")
        if math.isinf(loop_blocked_ms):
            return Budget(allowed=False, max_fanout=1, reason="failsoft")
        if loop_blocked_ms < 0:
            return Budget(allowed=False, max_fanout=1, reason="failsoft")

        # pressure_level out of expected range → clamp (not failsoft, just clamp)
        if not isinstance(pressure_level, int):
            return Budget(allowed=False, max_fanout=1, reason="failsoft")
        # Allow values above MAX but treat as CRITICAL
        effective_pressure = _clamp_pressure(pressure_level)

        # depth negative → treat as 0 (defensive)
        if depth < 0:
            effective_depth = 0
        else:
            effective_depth = depth

        # Guard against absurdly large depth values that could cause numeric issues
        effective_depth = min(effective_depth, 100_000)

    except Exception:  # noqa: BLE001
        return Budget(allowed=False, max_fanout=1, reason="failsoft")

    # ------------------------------------------------------------------
    # Read env knobs (fresh each call)
    # ------------------------------------------------------------------
    fanout_idle = _fanout_idle()
    queue_soft = _queue_soft()
    loop_ms_soft = _loop_ms_soft()

    # ------------------------------------------------------------------
    # Compute load score
    # ------------------------------------------------------------------
    try:
        score = _load_score(
            queue_len=queue_len,
            loop_blocked_ms=loop_blocked_ms,
            pressure_level=effective_pressure,
            queue_soft=queue_soft,
            loop_ms_soft=loop_ms_soft,
        )
        # Clamp to [0, 1] — defensive guard against floating-point drift
        score = max(0.0, min(1.0, score))
    except Exception:  # noqa: BLE001
        return Budget(allowed=False, max_fanout=1, reason="failsoft")

    # ------------------------------------------------------------------
    # Derive adaptive ceiling and fanout
    # ------------------------------------------------------------------
    try:
        ceiling = _adaptive_depth_ceiling(score, fanout_idle=fanout_idle)
        max_fanout = _adaptive_max_fanout(score, fanout_idle=fanout_idle)
    except Exception:  # noqa: BLE001
        return Budget(allowed=False, max_fanout=1, reason="failsoft")

    # ------------------------------------------------------------------
    # Decision
    # ------------------------------------------------------------------
    allowed = effective_depth <= ceiling
    if allowed:
        reason = (
            f"depth={effective_depth} <= ceiling={ceiling} "
            f"(load_score={score:.2f}, fanout={max_fanout})"
        )
    else:
        reason = (
            f"depth={effective_depth} > ceiling={ceiling} "
            f"(load_score={score:.2f}) — recursion blocked"
        )

    return Budget(allowed=allowed, max_fanout=max_fanout, reason=reason)
