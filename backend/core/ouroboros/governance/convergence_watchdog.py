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

class ReductionTracker
    record_pass(lineage_id, parent_chars, max_child_chars) -> WatchdogVerdict
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

_DEFAULT_STALL_RATIO = 0.95
_DEFAULT_STALL_PASSES = 2
_DEFAULT_TRACKER_SIZE = 256


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

    def reset(self, lineage_id: str) -> None:
        """Clear all recorded passes for *lineage_id*. Fail-soft."""
        try:
            self._lineages.pop(lineage_id, None)
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
) -> None:
    """Emit a [SOVEREIGN YIELD] WARNING and best-effort SSE event. Never raises.

    Log format:
        [SOVEREIGN YIELD] op=<op_id> lineage=<lineage_id> stalled reduction
        ratio=<ratio:.3f> passes=<consecutive_stalls> -> structural weight-shed
        (tier=<tier>) parent=<parent_chars> child=<child_chars>
    """
    try:
        logger.warning(
            "[SOVEREIGN YIELD] op=%s lineage=%s stalled reduction ratio=%.3f"
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

    # Best-effort SSE event — lazy import so the module can be used without
    # the full SSE broker stack loaded.
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: PLC0415
            publish_task_event,
        )
        publish_task_event(
            "sovereign_yield",
            op_id,
            {
                "lineage_id": lineage_id,
                "ratio": ratio,
                "consecutive_stalls": consecutive_stalls,
                "parent_chars": parent_chars,
                "child_chars": child_chars,
                "tier": tier,
            },
        )
    except Exception:  # pragma: no cover — fail-soft, SSE stack optional
        pass
