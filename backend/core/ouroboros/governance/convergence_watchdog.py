"""convergence_watchdog.py — Reduction-trajectory stall detection + [SOVEREIGN YIELD] telemetry.

Task T1 of the Autonomous Convergence Watchdog.

Public API
----------
WatchdogVerdict
    Frozen dataclass: stalled, ratio, consecutive_stalls, passes.

stall_ratio_threshold() -> float
    JARVIS_WATCHDOG_STALL_RATIO (default 0.95).

stall_passes_threshold() -> int
    JARVIS_WATCHDOG_STALL_PASSES (default 2).

watchdog_enabled() -> bool
    JARVIS_CONVERGENCE_WATCHDOG_ENABLED (default true).

max_self_heal_hops() -> int
    JARVIS_WATCHDOG_MAX_SELF_HEAL_HOPS (default 3, clamp >=1). The real
    termination bound on watchdog self-heal per invariant lineage.

class ReductionTracker
    record_pass(lineage_id, parent_chars, max_child_chars) -> WatchdogVerdict
    record_self_heal_hop(lineage_id) -> int  # bounded per-lineage hop counter
    reset(lineage_id)

get_reduction_tracker() -> ReductionTracker
    Process-global singleton.

emit_sovereign_yield(op_id, *, lineage_id, ratio, consecutive_stalls,
                     parent_chars, child_chars, tier) -> None
    Emit WARNING + best-effort SSE event. Never raises.
"""
from __future__ import annotations

import collections
import logging
import os
from dataclasses import dataclass
from typing import Dict

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Env-driven thresholds
# ---------------------------------------------------------------------------

_ENV_STALL_RATIO = "JARVIS_WATCHDOG_STALL_RATIO"
_ENV_STALL_PASSES = "JARVIS_WATCHDOG_STALL_PASSES"
_ENV_ENABLED = "JARVIS_CONVERGENCE_WATCHDOG_ENABLED"
_ENV_TRACKER_SIZE = "JARVIS_WATCHDOG_TRACKER_SIZE"
_ENV_MAX_SELF_HEAL_HOPS = "JARVIS_WATCHDOG_MAX_SELF_HEAL_HOPS"

_DEFAULT_STALL_RATIO = 0.95
_DEFAULT_STALL_PASSES = 2
_DEFAULT_TRACKER_SIZE = 256
_DEFAULT_MAX_SELF_HEAL_HOPS = 3


def stall_ratio_threshold() -> float:
    """Return the stall ratio threshold from env (default 0.95)."""
    try:
        return float(os.environ.get(_ENV_STALL_RATIO, _DEFAULT_STALL_RATIO))
    except (ValueError, TypeError):
        return _DEFAULT_STALL_RATIO


def stall_passes_threshold() -> int:
    """Return the consecutive stall passes threshold from env (default 2)."""
    try:
        return int(os.environ.get(_ENV_STALL_PASSES, _DEFAULT_STALL_PASSES))
    except (ValueError, TypeError):
        return _DEFAULT_STALL_PASSES


def watchdog_enabled() -> bool:
    """Return True if JARVIS_CONVERGENCE_WATCHDOG_ENABLED is not 'false'/'0' (default true)."""
    val = os.environ.get(_ENV_ENABLED, "true").strip().lower()
    return val not in ("false", "0", "no", "off")


def max_self_heal_hops() -> int:
    """Return the max self-heal re-injections per invariant lineage.

    Reads ``JARVIS_WATCHDOG_MAX_SELF_HEAL_HOPS`` (default 3). Clamped to >=1.
    Fail-soft: any parse error returns the default (3).

    This is the REAL mathematical termination bound on the watchdog self-heal:
    because the lineage id is invariant across re-injection, a bounded
    per-lineage counter caps the number of shed-and-continue hops at a small
    constant REGARDLESS of the shed/envelope truncation dynamics (the
    title-prefix window shift that defeated the fixpoint-only guard).
    """
    try:
        return max(1, int(os.environ.get(
            _ENV_MAX_SELF_HEAL_HOPS, _DEFAULT_MAX_SELF_HEAL_HOPS,
        )))
    except (ValueError, TypeError):
        return _DEFAULT_MAX_SELF_HEAL_HOPS


# ---------------------------------------------------------------------------
# Verdict dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WatchdogVerdict:
    """Immutable verdict from a single record_pass call."""

    stalled: bool
    ratio: float
    consecutive_stalls: int
    passes: int


_SAFE_VERDICT = WatchdogVerdict(False, 0.0, 0, 0)


# ---------------------------------------------------------------------------
# ReductionTracker — bounded lineage map mirroring AttemptLedger pattern
# ---------------------------------------------------------------------------

def _lineage_deque_maxlen() -> int:
    """Small per-lineage deque: passes_threshold + 3 to keep memory tiny."""
    return stall_passes_threshold() + 3


class ReductionTracker:
    """Tracks reduction ratios per lineage and detects stall trajectories.

    Mirrors the bounded-FIFO + shadow-set pattern from ``recursion_dedup.py``
    (AttemptLedger).  The outer dict is bounded to ``JARVIS_WATCHDOG_TRACKER_SIZE``
    lineages (oldest evicted via OrderedDict move-to-end discipline); each
    lineage holds a small bounded deque of recent ratios.
    """

    def __init__(self) -> None:
        max_lineages = int(os.environ.get(_ENV_TRACKER_SIZE, _DEFAULT_TRACKER_SIZE))
        self._max_lineages: int = max(1, max_lineages)
        # OrderedDict used for LRU eviction of oldest lineages.
        self._lineages: Dict[str, collections.deque] = collections.OrderedDict()
        # Bounded per-lineage cumulative self-heal hop counter. SAME cap +
        # LRU-eviction discipline as ``_lineages`` (no new unbounded store);
        # this is the structural termination bound on watchdog self-heal.
        self._self_heal_hops: Dict[str, int] = collections.OrderedDict()

    # ------------------------------------------------------------------
    def record_pass(
        self,
        lineage_id: str,
        parent_chars: int,
        max_child_chars: int,
    ) -> WatchdogVerdict:
        """Record one decompose pass for *lineage_id* and return a verdict.

        ratio = max_child_chars / max(1, parent_chars).
        consecutive_stalls = trailing run of ratios >= stall_ratio_threshold().
        stalled = consecutive_stalls >= stall_passes_threshold().

        Fail-soft: any exception returns WatchdogVerdict(False, 0.0, 0, 0).
        """
        try:
            ratio = max_child_chars / max(1, parent_chars)
            threshold = stall_ratio_threshold()
            passes_needed = stall_passes_threshold()
            deque_maxlen = passes_needed + 3

            # Ensure lineage exists; evict oldest if over capacity.
            if lineage_id not in self._lineages:
                if len(self._lineages) >= self._max_lineages:
                    self._lineages.popitem(last=False)  # evict oldest
                self._lineages[lineage_id] = collections.deque(maxlen=deque_maxlen)
            else:
                # Move to end to mark as recently used.
                self._lineages.move_to_end(lineage_id)

            dq = self._lineages[lineage_id]
            dq.append(ratio)

            total_passes = len(dq)

            # Count trailing run of stalls.
            consecutive_stalls = 0
            for r in reversed(dq):
                if r >= threshold:
                    consecutive_stalls += 1
                else:
                    break

            stalled = consecutive_stalls >= passes_needed

            return WatchdogVerdict(
                stalled=stalled,
                ratio=ratio,
                consecutive_stalls=consecutive_stalls,
                passes=total_passes,
            )
        except Exception:  # pragma: no cover — fail-soft
            logger.debug("[ConvergenceWatchdog] record_pass fail-soft", exc_info=True)
            return _SAFE_VERDICT

    def record_self_heal_hop(self, lineage_id: str) -> int:
        """Increment and return the cumulative self-heal count for *lineage_id*.

        Bounded per-lineage counter mirroring the LRU-eviction discipline of
        ``record_pass`` (same ``_max_lineages`` cap, oldest evicted). Because
        the lineage id is INVARIANT across re-injection, this caps the number
        of watchdog self-heal hops per lineage at a small constant regardless
        of the shed/envelope truncation dynamics.

        Fail-soft: any exception returns ``_DEFAULT_MAX_SELF_HEAL_HOPS + 1`` so
        the caller treats it as over-budget and falls to advisor_blocked (the
        de-dup final backstop) -- a failure NEVER grants an unbounded hop.
        """
        try:
            if lineage_id in self._self_heal_hops:
                self._self_heal_hops.move_to_end(lineage_id)
                self._self_heal_hops[lineage_id] += 1
            else:
                if len(self._self_heal_hops) >= self._max_lineages:
                    self._self_heal_hops.popitem(last=False)  # evict oldest
                self._self_heal_hops[lineage_id] = 1
            return self._self_heal_hops[lineage_id]
        except Exception:  # pragma: no cover — fail-soft -> over-budget
            logger.debug(
                "[ConvergenceWatchdog] record_self_heal_hop fail-soft",
                exc_info=True,
            )
            return _DEFAULT_MAX_SELF_HEAL_HOPS + 1

    def reset(self, lineage_id: str) -> None:
        """Clear all recorded passes + self-heal hops for *lineage_id*. Fail-soft."""
        try:
            self._lineages.pop(lineage_id, None)
        except Exception:  # pragma: no cover
            pass
        try:
            self._self_heal_hops.pop(lineage_id, None)
        except Exception:  # pragma: no cover
            pass


# ---------------------------------------------------------------------------
# Process-global singleton (mirrors get_attempt_ledger pattern)
# ---------------------------------------------------------------------------

_REDUCTION_TRACKER_SINGLETON: ReductionTracker | None = None


def get_reduction_tracker() -> ReductionTracker:
    """Return the process-global ReductionTracker, creating it on first use."""
    global _REDUCTION_TRACKER_SINGLETON
    if _REDUCTION_TRACKER_SINGLETON is None:
        _REDUCTION_TRACKER_SINGLETON = ReductionTracker()
    return _REDUCTION_TRACKER_SINGLETON


# ---------------------------------------------------------------------------
# LaneDilationTracker — bounded per-op deadline-dilation hop counter (T5)
# ---------------------------------------------------------------------------
#
# Dynamic Lane Escalation (Part 2, T5). After T4 rotates a wedged op off the
# batch lane onto realtime, the realtime lane can ALSO time out -- "lane
# collapse" (both transport lanes exhausted by TIMEOUT for the SAME op). Rather
# than re-attempt forever at the same too-small deadline, the dispatcher DILATES
# (increases) the per-op generation deadline a BOUNDED number of times. This
# tracker is the structural termination bound: a per-op hop counter reusing the
# EXACT bounded-LRU discipline of ReductionTracker.record_self_heal_hop (no new
# persistent store, no background task) -- once the op exceeds the env-tunable
# hop cap, no further dilation happens and the op falls through to the existing
# immortal-queue / DLQ backstop unchanged.

_ENV_LANE_DILATION_TRACKER_SIZE = "JARVIS_LANE_DILATION_TRACKER_SIZE"
_ENV_LANE_DILATION_FACTOR = "JARVIS_LANE_DILATION_FACTOR"
_ENV_LANE_DILATION_MAX_HOPS = "JARVIS_LANE_DILATION_MAX_HOPS"
_ENV_LANE_DILATION_MAX_S = "JARVIS_LANE_DILATION_MAX_S"

_DEFAULT_LANE_DILATION_TRACKER_SIZE = 256
_DEFAULT_LANE_DILATION_FACTOR = 1.5
_DEFAULT_LANE_DILATION_MAX_HOPS = 2


def lane_dilation_factor() -> float:
    """Per-collapse deadline multiplier (default 1.5). Clamped to >= 1.0 so a
    dilation can never SHRINK a deadline. Fail-soft: any parse error -> default."""
    try:
        val = float(os.environ.get(
            _ENV_LANE_DILATION_FACTOR, _DEFAULT_LANE_DILATION_FACTOR,
        ))
        return val if val >= 1.0 else _DEFAULT_LANE_DILATION_FACTOR
    except (ValueError, TypeError):
        return _DEFAULT_LANE_DILATION_FACTOR


def lane_dilation_max_hops() -> int:
    """Max deadline dilations per op (default 2). Clamped to >= 0 (0 disables
    dilation entirely -> immediate backstop). Fail-soft: parse error -> default.

    This is the REAL termination bound: rotate -> dilate(xfactor) ->
    dilate(xfactor again, capped) -> give up to the existing immortal/DLQ
    backstop. The cap is env-tunable, never a magic literal."""
    try:
        return max(0, int(os.environ.get(
            _ENV_LANE_DILATION_MAX_HOPS, _DEFAULT_LANE_DILATION_MAX_HOPS,
        )))
    except (ValueError, TypeError):
        return _DEFAULT_LANE_DILATION_MAX_HOPS


def lane_dilation_max_s(base_deadline_s: float) -> float:
    """Absolute ceiling for a dilated deadline (seconds).

    Reads ``JARVIS_LANE_DILATION_MAX_S`` when set; otherwise defaults to
    ``base_deadline_s * 3`` (so the dilation can at most triple the base
    deadline regardless of factor x hops). Fail-soft: any parse error or a
    non-positive override -> the base*3 default."""
    default_cap = max(0.0, base_deadline_s) * 3.0
    raw = os.environ.get(_ENV_LANE_DILATION_MAX_S)
    if raw is None:
        return default_cap
    try:
        val = float(raw)
        return val if val > 0 else default_cap
    except (ValueError, TypeError):
        return default_cap


class LaneDilationTracker:
    """Bounded per-op deadline-dilation hop counter (lane collapse, T5).

    Mirrors ``ReductionTracker``'s LRU discipline EXACTLY: the outer dict is
    bounded to ``JARVIS_LANE_DILATION_TRACKER_SIZE`` ops (oldest evicted via
    OrderedDict popitem(last=False)); each op holds a single small int hop
    count. The op_id is INVARIANT across the immortal queue's re-attempts, so a
    bounded per-op counter caps the number of deadline dilations at a small
    constant regardless of retry dynamics -- the structural no-infinite-loop
    guarantee.
    """

    def __init__(self) -> None:
        try:
            max_ops = int(os.environ.get(
                _ENV_LANE_DILATION_TRACKER_SIZE,
                _DEFAULT_LANE_DILATION_TRACKER_SIZE,
            ))
        except (ValueError, TypeError):
            max_ops = _DEFAULT_LANE_DILATION_TRACKER_SIZE
        self._max_ops: int = max(1, max_ops)
        self._hops: Dict[str, int] = collections.OrderedDict()

    def record_dilation_hop(self, op_id: str) -> int:
        """Increment and return the cumulative dilation hop count for *op_id*.

        Bounded per-op counter mirroring ``record_self_heal_hop``: same LRU
        eviction (oldest popped when over capacity), move-to-end on touch.

        Fail-soft: any exception returns ``lane_dilation_max_hops() + 1`` so the
        caller treats it as over-budget and STOPS dilating (falls to backstop)
        -- a failure NEVER grants an unbounded dilation."""
        try:
            if op_id in self._hops:
                self._hops.move_to_end(op_id)
                self._hops[op_id] += 1
            else:
                if len(self._hops) >= self._max_ops:
                    self._hops.popitem(last=False)  # evict oldest
                self._hops[op_id] = 1
            return self._hops[op_id]
        except Exception:  # pragma: no cover -- fail-soft -> over-budget
            logger.debug(
                "[ConvergenceWatchdog] record_dilation_hop fail-soft",
                exc_info=True,
            )
            return lane_dilation_max_hops() + 1

    def hops(self, op_id: str) -> int:
        """Return the current dilation hop count for *op_id* (0 if never
        dilated). Fail-soft -> 0. Read-only: does NOT touch LRU order."""
        try:
            return int(self._hops.get(op_id, 0))
        except Exception:  # pragma: no cover
            return 0

    def reset(self, op_id: str) -> None:
        """Clear the dilation hops for *op_id* (e.g. on terminal success).
        Fail-soft."""
        try:
            self._hops.pop(op_id, None)
        except Exception:  # pragma: no cover
            pass


_LANE_DILATION_TRACKER_SINGLETON: "LaneDilationTracker | None" = None


def get_lane_dilation_tracker() -> "LaneDilationTracker":
    """Return the process-global LaneDilationTracker (mirrors
    get_reduction_tracker singleton pattern)."""
    global _LANE_DILATION_TRACKER_SINGLETON
    if _LANE_DILATION_TRACKER_SINGLETON is None:
        _LANE_DILATION_TRACKER_SINGLETON = LaneDilationTracker()
    return _LANE_DILATION_TRACKER_SINGLETON


def compute_dilated_deadline(base_deadline_s: float, hops: int) -> float:
    """Pure: the dilated deadline for an op that has dilated ``hops`` times.

    = ``base_deadline_s * (lane_dilation_factor() ** hops)`` capped at
    :func:`lane_dilation_max_s`. ``hops <= 0`` returns the base deadline
    unchanged (byte-identical, no dilation). Fail-soft: any error returns the
    base deadline -- the op is NEVER lost, just runs at the legacy deadline."""
    try:
        if hops <= 0 or base_deadline_s <= 0:
            return max(0.0, base_deadline_s)
        factor = lane_dilation_factor()
        dilated = base_deadline_s * (factor ** hops)
        cap = lane_dilation_max_s(base_deadline_s)
        return max(base_deadline_s, min(dilated, cap))
    except Exception:  # pragma: no cover -- fail-soft -> legacy deadline
        logger.debug(
            "[ConvergenceWatchdog] compute_dilated_deadline fail-soft",
            exc_info=True,
        )
        return max(0.0, base_deadline_s)


# ---------------------------------------------------------------------------
# Sovereign yield telemetry
# ---------------------------------------------------------------------------

def emit_sovereign_yield(
    op_id: str,
    *,
    lineage_id: str,
    ratio: float,
    consecutive_stalls: int,
    parent_chars: int,
    child_chars: int,
    tier: str,
    reason: str | None = None,
) -> None:
    """Emit a [SOVEREIGN YIELD] WARNING and best-effort SSE event. Never raises.

    Log format (default):
        [SOVEREIGN YIELD] op=<op_id> lineage=<lineage_id> stalled reduction
        ratio=<ratio:.3f> passes=<consecutive_stalls> -> structural weight-shed
        (tier=<tier>) parent=<parent_chars> child=<child_chars>

    Adaptive Epistemic Feedback Matrix (T3): when ``reason`` is supplied the
    label becomes ``[SOVEREIGN YIELD: <reason>]`` (e.g. ``UNRESOLVABLE_PATH``)
    so the graceful-semantic-pivot yield is grep-distinguishable from the
    weight-shed yield. ``reason=None`` (default) is byte-identical to the
    legacy format. ``reason`` also rides the SSE payload when set.
    """
    try:
        _label = (
            f"[SOVEREIGN YIELD: {reason}]" if reason else "[SOVEREIGN YIELD]"
        )
        logger.warning(
            _label + " op=%s lineage=%s stalled reduction ratio=%.3f"
            " passes=%d -> structural weight-shed (tier=%s) parent=%d child=%d",
            op_id,
            lineage_id,
            ratio,
            consecutive_stalls,
            tier,
            parent_chars,
            child_chars,
        )
    except Exception:  # pragma: no cover
        pass

    # Best-effort SSE event -- lazy import so the module can be used without
    # the full SSE broker stack loaded.
    # Command Node Phase 1 (2026-06-23): upgraded to use the dedicated
    # publish_sovereign_yield helper (EVENT_TYPE_SOVEREIGN_YIELD is now in
    # _VALID_EVENT_TYPES so the event is no longer silently dropped).
    _yield_reason = reason or tier
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: PLC0415
            publish_sovereign_yield as _pub_yield,
        )
        _pub_yield(op_id, _yield_reason)
    except Exception:  # pragma: no cover -- fail-soft, SSE stack optional
        pass
