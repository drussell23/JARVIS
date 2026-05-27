"""Loop-Sink Identifier — Slice 33 Arc 0 (diagnostic-only, no fixes).

Closes the v26 (`bt-2026-05-27-220220`) blind-spot: 81 ControlPlane
starvation events, peak 56 s stall, but the post-stall snapshot stacks
caught the watchdog itself running — the actual blocking call-site was
never identified. Slice 32 confirmed Oracle's parse + visitor walk is
NOT the sink (11,999 process dispatches succeeded; `oracle_slow_call`
fired once with 34 s parent / 110 ms worker).

# What this module does

Provides a precision blocking-time recorder for hand-wired call-sites
across the suspected on-loop sinks. When a wrapped region exceeds the
configured threshold (default 50 ms), emits a structured log line:

    [LoopSink] callsite=<name> blocked_ms=<float> ...

The v27 diagnostic probe ($1 / 10 min) consumes these log lines to
attribute starvation to specific call-sites — and Slice 33 Arc A/B/C
get scoped against named targets instead of speculation.

# What this module does NOT do

  * Does NOT fix any starvation — purely diagnostic. The bindings
    "no euphoria, only artifacts" and "no brute force, no
    workarounds" require evidence before remediation.
  * Does NOT introduce any subsystem coupling — only `time.monotonic()`,
    `logging`, and minimal stdlib. Importable from anywhere in the
    governance tree without cycle risk.
  * Does NOT alter execution paths — wrappers measure and log; they
    never raise into the caller (errors during measurement are
    swallowed and logged at WARN level).

# Public API

  * ``sink_sync(callsite: str, threshold_ms: float = 50.0)`` — sync
    context manager. Use for sync code blocks inside async functions
    where the block may hold the asyncio thread.
  * ``sink_async(callsite: str, threshold_ms: float = 50.0)`` — async
    context manager. Use for entire async fn entries. Measures total
    elapsed wall-clock including any awaits within the block; a long
    elapsed indicates either sync hot-spots OR loop-starvation-inflated
    awaits — both are diagnostically useful.
  * ``@instrument_sync(name=...)`` / ``@instrument_async(name=...)``
    decorator forms.
  * ``get_stats() -> Dict[str, CallsiteStats]`` — cumulative per-site
    stats (count, total_ms, max_ms, p50, p95, p99 from a bounded
    sample ring). Used by the v27 probe runbook to emit a final
    leaderboard.
  * ``reset_stats()`` — for tests + per-soak isolation.

# Env knobs

  * ``JARVIS_LOOP_SINK_ENABLED`` (default ``true``) — master switch.
    Set to ``false`` to disable instrumentation system-wide (legacy
    byte-identical fallback for emergency rollback).
  * ``JARVIS_LOOP_SINK_THRESHOLD_MS`` (default ``50.0``) — global
    minimum blocked_ms before emitting a log line. Per-call-site
    threshold overrides via the ``threshold_ms`` argument.
  * ``JARVIS_LOOP_SINK_SAMPLE_RING_SIZE`` (default ``512``) —
    bounded ring buffer per call-site for p50/p95/p99 calculation.

# Discipline

  * Substrate has ZERO dependencies on governance / orchestrator /
    provider modules.
  * AST pin enforces no `import backend.core.ouroboros.governance`
    inside this module.
  * NEVER raises into the caller. Measurement errors → logged at
    WARN, swallowed.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import threading
import time
from collections import deque
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Deque,
    Dict,
    Iterator,
    Optional,
    TypeVar,
)


logger = logging.getLogger("Ouroboros.LoopSink")


# ============================================================================
# Env resolution
# ============================================================================


_ENABLED_ENV: str = "JARVIS_LOOP_SINK_ENABLED"
_THRESHOLD_MS_ENV: str = "JARVIS_LOOP_SINK_THRESHOLD_MS"
_SAMPLE_RING_SIZE_ENV: str = "JARVIS_LOOP_SINK_SAMPLE_RING_SIZE"

_DEFAULT_THRESHOLD_MS: float = 50.0
_DEFAULT_SAMPLE_RING_SIZE: int = 512


def is_enabled() -> bool:
    """Slice 33 Arc 0 master switch. Default TRUE."""
    raw = os.environ.get(_ENABLED_ENV, "").strip().lower()
    if not raw:
        return True
    return raw not in ("0", "false", "no", "off")


def _resolve_threshold_ms() -> float:
    try:
        raw = os.environ.get(_THRESHOLD_MS_ENV, "").strip()
        if not raw:
            return _DEFAULT_THRESHOLD_MS
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return _DEFAULT_THRESHOLD_MS


def _resolve_sample_ring_size() -> int:
    try:
        raw = os.environ.get(_SAMPLE_RING_SIZE_ENV, "").strip()
        if not raw:
            return _DEFAULT_SAMPLE_RING_SIZE
        return max(8, int(raw))
    except (TypeError, ValueError):
        return _DEFAULT_SAMPLE_RING_SIZE


# ============================================================================
# Per-callsite stats
# ============================================================================


@dataclass
class CallsiteStats:
    """Cumulative blocking-time stats for one call-site.

    ``samples`` is a bounded ring buffer used for p50/p95/p99
    percentile calculation — bounded so a high-traffic site can't
    blow memory. Older samples are evicted when the ring fills.
    """

    callsite: str
    count: int = 0
    total_ms: float = 0.0
    max_ms: float = 0.0
    over_threshold_count: int = 0
    samples: Deque[float] = field(default_factory=deque)

    def record(self, elapsed_ms: float, threshold_ms: float) -> None:
        self.count += 1
        self.total_ms += elapsed_ms
        if elapsed_ms > self.max_ms:
            self.max_ms = elapsed_ms
        if elapsed_ms >= threshold_ms:
            self.over_threshold_count += 1
        self.samples.append(elapsed_ms)

    def mean_ms(self) -> float:
        if self.count == 0:
            return 0.0
        return self.total_ms / self.count

    def percentile_ms(self, q: float) -> float:
        """Bounded-sample percentile. Uses nearest-rank method.

        Returns 0.0 when no samples. Safe for q in (0.0, 1.0].
        """
        n = len(self.samples)
        if n == 0:
            return 0.0
        sorted_samples = sorted(self.samples)
        idx = max(0, min(n - 1, int(round(q * n)) - 1))
        return sorted_samples[idx]

    def snapshot(self) -> Dict[str, float]:
        return {
            "count": float(self.count),
            "total_ms": self.total_ms,
            "mean_ms": self.mean_ms(),
            "max_ms": self.max_ms,
            "p50_ms": self.percentile_ms(0.50),
            "p95_ms": self.percentile_ms(0.95),
            "p99_ms": self.percentile_ms(0.99),
            "over_threshold_count": float(self.over_threshold_count),
        }


# Module-level stats registry. Guarded by a Lock so concurrent
# worker threads can record without tearing the int/float increments.
_stats: Dict[str, CallsiteStats] = {}
_stats_lock = threading.Lock()


def _get_or_create_stats(callsite: str) -> CallsiteStats:
    with _stats_lock:
        existing = _stats.get(callsite)
        if existing is not None:
            return existing
        stats = CallsiteStats(
            callsite=callsite,
            samples=deque(maxlen=_resolve_sample_ring_size()),
        )
        _stats[callsite] = stats
        return stats


def get_stats() -> Dict[str, Dict[str, float]]:
    """Public snapshot — used by v27 probe runbook to emit leaderboard."""
    with _stats_lock:
        return {k: v.snapshot() for k, v in _stats.items()}


def reset_stats() -> None:
    """Clear all recorded stats. Used by tests + per-soak isolation."""
    with _stats_lock:
        _stats.clear()


def get_leaderboard(top_n: int = 20) -> str:
    """Format a human-readable leaderboard sorted by total blocking time.

    The v27 probe runbook emits this at session shutdown to anchor the
    Slice 33 Arc A/B/C scoping decision in data, not speculation.
    """
    snap = get_stats()
    if not snap:
        return "[LoopSink] no instrumented call-sites recorded any samples"
    ranked = sorted(
        snap.items(), key=lambda kv: kv[1]["total_ms"], reverse=True,
    )[:top_n]
    lines = [
        f"[LoopSink] leaderboard (top {len(ranked)} by total blocking time):",
        f"  {'callsite':<50s} {'count':>8s} {'total_ms':>12s} "
        f"{'mean_ms':>10s} {'p95_ms':>10s} {'max_ms':>10s} {'>thresh':>8s}",
    ]
    for callsite, s in ranked:
        lines.append(
            f"  {callsite:<50s} {int(s['count']):>8d} "
            f"{s['total_ms']:>12.1f} {s['mean_ms']:>10.2f} "
            f"{s['p95_ms']:>10.2f} {s['max_ms']:>10.2f} "
            f"{int(s['over_threshold_count']):>8d}"
        )
    return "\n".join(lines)


# ============================================================================
# Core context managers
# ============================================================================


def _emit_blocked(
    callsite: str, elapsed_ms: float, threshold_ms: float, kind: str,
) -> None:
    """Single log line for over-threshold events. Format is stable —
    the v27 probe runbook grep's `[LoopSink] callsite=...` to extract
    the per-call-site empirical data."""
    logger.warning(
        "[LoopSink] callsite=%s kind=%s blocked_ms=%.2f "
        "threshold_ms=%.1f — on-loop call exceeded threshold",
        callsite, kind, elapsed_ms, threshold_ms,
    )


@contextmanager
def sink_sync(
    callsite: str,
    threshold_ms: Optional[float] = None,
) -> Iterator[None]:
    """Sync blocking-time recorder. Use around code regions inside
    async functions where the region runs ENTIRELY on the asyncio
    thread (no awaits inside).

    Example::

        async def my_async_fn(self):
            async for x in iterator:
                ...
                with sink_sync("oracle._scan_for_changes.inner"):
                    # tight CPU loop here
                    expensive_dict_mutations(x)
    """
    if not is_enabled():
        yield
        return
    eff_threshold = (
        threshold_ms if threshold_ms is not None
        else _resolve_threshold_ms()
    )
    t0 = time.monotonic()
    try:
        yield
    finally:
        try:
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            stats = _get_or_create_stats(callsite)
            stats.record(elapsed_ms, eff_threshold)
            if elapsed_ms >= eff_threshold:
                _emit_blocked(callsite, elapsed_ms, eff_threshold, "sync")
        except Exception as exc:  # noqa: BLE001 — never raise from sink
            logger.warning(
                "[LoopSink] internal error in sink_sync(%s): %s",
                callsite, exc,
            )


@asynccontextmanager
async def sink_async(
    callsite: str,
    threshold_ms: Optional[float] = None,
) -> AsyncIterator[None]:
    """Async wall-clock recorder. Use around entire async function
    bodies. Measures total elapsed including any awaits — a long
    elapsed for an async region indicates EITHER sync hot-spots
    inside the region OR loop-starvation-inflated awaits. Both are
    diagnostically useful (the v27 probe leaderboard surfaces both).

    Example::

        async def assess_regression_risk(self, ...):
            async with sink_async("consciousness_bridge.assess_regression_risk"):
                # body
                ...
    """
    if not is_enabled():
        yield
        return
    eff_threshold = (
        threshold_ms if threshold_ms is not None
        else _resolve_threshold_ms()
    )
    t0 = time.monotonic()
    try:
        yield
    finally:
        try:
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            stats = _get_or_create_stats(callsite)
            stats.record(elapsed_ms, eff_threshold)
            if elapsed_ms >= eff_threshold:
                _emit_blocked(callsite, elapsed_ms, eff_threshold, "async")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[LoopSink] internal error in sink_async(%s): %s",
                callsite, exc,
            )


# ============================================================================
# Decorator forms
# ============================================================================


F = TypeVar("F", bound=Callable[..., Any])
AF = TypeVar("AF", bound=Callable[..., Awaitable[Any]])


def instrument_sync(
    name: str,
    threshold_ms: Optional[float] = None,
) -> Callable[[F], F]:
    """Decorator for sync functions. Wraps body in :func:`sink_sync`.

    Example::

        @instrument_sync("oracle.TheOracle.add_node")
        def add_node(self, node_data: NodeData) -> None:
            ...
    """
    def _decorator(fn: F) -> F:
        @functools.wraps(fn)
        def _wrapped(*args: Any, **kwargs: Any) -> Any:
            with sink_sync(name, threshold_ms=threshold_ms):
                return fn(*args, **kwargs)
        return _wrapped  # type: ignore[return-value]
    return _decorator


def instrument_async(
    name: str,
    threshold_ms: Optional[float] = None,
) -> Callable[[AF], AF]:
    """Decorator for async functions. Wraps body in :func:`sink_async`.

    Example::

        @instrument_async("posture_observer.run_one_cycle")
        async def run_one_cycle(self) -> Optional[PostureReading]:
            ...
    """
    def _decorator(fn: AF) -> AF:
        @functools.wraps(fn)
        async def _wrapped(*args: Any, **kwargs: Any) -> Any:
            async with sink_async(name, threshold_ms=threshold_ms):
                return await fn(*args, **kwargs)
        return _wrapped  # type: ignore[return-value]
    return _decorator


# ============================================================================
# Public surface
# ============================================================================


__all__ = [
    "CallsiteStats",
    "get_leaderboard",
    "get_stats",
    "instrument_async",
    "instrument_sync",
    "is_enabled",
    "reset_stats",
    "sink_async",
    "sink_sync",
]
