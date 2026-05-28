"""DW Per-Shape Statistics — Slice 34 substrate (Phase 0).

Rolling p50/p95/p99 + success-rate aggregator over the
:class:`DWCapacityLedger` event stream. Operates per
``(model_id, route, prompt_size_bucket)`` shape so a 397B on a
20 KB STANDARD op gets its own stats from a 35B on the same shape
or a 397B on a 5 KB IMMEDIATE op.

# Design discipline

  * **Composes existing:** reads via :meth:`DWCapacityLedger.read_recent`
    — no parallel storage. Cached for ``_CACHE_TTL_S`` (default 30s)
    to avoid re-parsing the file on every dispatch.
  * **No hardcoding:** window size, prompt bucket size, cache TTL,
    minimum-samples-for-confidence all env-knobbed.
  * **Fail-closed:** :meth:`stats_for_shape` returns ``None`` when
    insufficient samples — caller must fall through to static formula.
  * **Pure-function inference:** stats are derived from the snapshot;
    no in-memory mutation.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from backend.core.ouroboros.governance.dw_capacity_ledger import (
    DWCallRecord,
    DWCapacityLedger,
    get_default_ledger,
)


logger = logging.getLogger("Ouroboros.DWPerShapeStats")


# ============================================================================
# Env knobs
# ============================================================================


_WINDOW_SIZE_ENV: str = "JARVIS_DW_PER_SHAPE_WINDOW"
_BUCKET_SIZE_ENV: str = "JARVIS_DW_PER_SHAPE_PROMPT_BUCKET_CHARS"
_CACHE_TTL_ENV: str = "JARVIS_DW_PER_SHAPE_CACHE_TTL_S"
_MIN_SAMPLES_ENV: str = "JARVIS_DW_PER_SHAPE_MIN_SAMPLES"

_DEFAULT_WINDOW_SIZE: int = 500
_DEFAULT_BUCKET_SIZE: int = 5000     # 5 KB buckets
_DEFAULT_CACHE_TTL_S: float = 30.0
_DEFAULT_MIN_SAMPLES: int = 10


def _window_size() -> int:
    try:
        raw = os.environ.get(_WINDOW_SIZE_ENV, "").strip()
        if not raw:
            return _DEFAULT_WINDOW_SIZE
        return max(10, int(raw))
    except (TypeError, ValueError):
        return _DEFAULT_WINDOW_SIZE


def _bucket_size() -> int:
    try:
        raw = os.environ.get(_BUCKET_SIZE_ENV, "").strip()
        if not raw:
            return _DEFAULT_BUCKET_SIZE
        return max(1, int(raw))
    except (TypeError, ValueError):
        return _DEFAULT_BUCKET_SIZE


def _cache_ttl_s() -> float:
    try:
        raw = os.environ.get(_CACHE_TTL_ENV, "").strip()
        if not raw:
            return _DEFAULT_CACHE_TTL_S
        return max(1.0, float(raw))
    except (TypeError, ValueError):
        return _DEFAULT_CACHE_TTL_S


def _min_samples() -> int:
    try:
        raw = os.environ.get(_MIN_SAMPLES_ENV, "").strip()
        if not raw:
            return _DEFAULT_MIN_SAMPLES
        return max(1, int(raw))
    except (TypeError, ValueError):
        return _DEFAULT_MIN_SAMPLES


# ============================================================================
# Result type
# ============================================================================


@dataclass(frozen=True)
class ShapeStats:
    """Aggregated per-shape latency + success stats from a recent
    sliding window of the ledger. Frozen — recompute via
    :meth:`DWPerShapeStats.stats_for_shape` if you need fresher data.
    """

    model_id: str
    route: str
    prompt_bucket: int          # prompt_chars rounded down to bucket
    sample_count: int
    success_rate: float          # 0.0 - 1.0
    p50_ms: float
    p95_ms: float
    p99_ms: float
    ttft_p50_ms: float           # 0.0 when no TTFT records
    last_observed_unix: float


# ============================================================================
# Aggregator
# ============================================================================


class DWPerShapeStats:
    """Per-shape rolling aggregator backed by ``DWCapacityLedger``.

    Caches the ledger snapshot for ``_CACHE_TTL_S`` to amortize
    file-read cost across many dispatch decisions. Cache is
    per-instance — the module-level singleton reads it.

    NEVER raises into the caller. On any internal error (ledger
    read failure, malformed data), returns ``None`` from
    :meth:`stats_for_shape` and the caller falls through to the
    static formula in :mod:`dw_adaptive_timeout`.
    """

    def __init__(
        self,
        ledger: Optional[DWCapacityLedger] = None,
    ) -> None:
        self._ledger = ledger or get_default_ledger()
        self._cache: Dict[Tuple[str, str, int], ShapeStats] = {}
        self._cache_built_at: float = 0.0
        self._cache_lock_held: bool = False

    def _maybe_rebuild_cache(self) -> None:
        """Rebuild cache if older than TTL. Single-threaded by
        design (asyncio loop is the only caller path)."""
        now = time.monotonic()
        if (now - self._cache_built_at) < _cache_ttl_s():
            return
        try:
            recs = self._ledger.read_recent(limit=_window_size())
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[DWPerShapeStats] ledger read failed: %s", exc,
            )
            return
        new_cache: Dict[Tuple[str, str, int], List[DWCallRecord]] = {}
        bucket = _bucket_size()
        for r in recs:
            key = (
                r.model_id,
                r.route,
                (r.prompt_chars // bucket) * bucket,
            )
            new_cache.setdefault(key, []).append(r)
        # Build ShapeStats for each non-empty bucket
        self._cache.clear()
        for key, group in new_cache.items():
            n = len(group)
            if n == 0:
                continue
            successes = sum(1 for g in group if g.outcome == "ok")
            latencies = sorted(g.total_elapsed_ms for g in group)
            ttfts = sorted(
                g.ttft_ms for g in group if g.ttft_ms is not None
            )
            last_ts = max(g.timestamp_unix for g in group)
            self._cache[key] = ShapeStats(
                model_id=key[0],
                route=key[1],
                prompt_bucket=key[2],
                sample_count=n,
                success_rate=successes / n,
                p50_ms=_percentile(latencies, 0.50),
                p95_ms=_percentile(latencies, 0.95),
                p99_ms=_percentile(latencies, 0.99),
                ttft_p50_ms=_percentile(ttfts, 0.50),
                last_observed_unix=last_ts,
            )
        self._cache_built_at = now

    def stats_for_shape(
        self,
        *,
        model_id: str,
        route: str,
        prompt_chars: int,
    ) -> Optional[ShapeStats]:
        """Return rolling stats for the given shape, or ``None`` if
        insufficient samples (< ``_min_samples()``). Caller MUST
        treat ``None`` as "no evidence — use static formula."
        """
        self._maybe_rebuild_cache()
        bucket = _bucket_size()
        key = (
            model_id,
            (route or "").strip().lower(),
            (max(0, int(prompt_chars)) // bucket) * bucket,
        )
        stats = self._cache.get(key)
        if stats is None or stats.sample_count < _min_samples():
            return None
        return stats

    def snapshot(self) -> Dict[Tuple[str, str, int], ShapeStats]:
        """Read-only copy of the full cache. For REPL inspection +
        Phase 2 hypothesis-resolution reports."""
        self._maybe_rebuild_cache()
        return dict(self._cache)

    def invalidate(self) -> None:
        """Force cache rebuild on next read. Used by tests."""
        self._cache_built_at = 0.0


def _percentile(sorted_samples: List[float], q: float) -> float:
    n = len(sorted_samples)
    if n == 0:
        return 0.0
    idx = max(0, min(n - 1, int(round(q * n)) - 1))
    return float(sorted_samples[idx])


# ============================================================================
# Module-level singleton
# ============================================================================


_default_stats: Optional[DWPerShapeStats] = None


def get_default_stats() -> DWPerShapeStats:
    """Lazy module singleton — composes the default ledger."""
    global _default_stats
    if _default_stats is None:
        _default_stats = DWPerShapeStats()
    return _default_stats


def reset_for_tests() -> None:
    global _default_stats
    _default_stats = None


__all__ = [
    "DWPerShapeStats",
    "ShapeStats",
    "get_default_stats",
    "reset_for_tests",
]
