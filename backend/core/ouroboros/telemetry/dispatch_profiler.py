"""Dispatch Profiler — Slice 34 Phase 1 (intra-dispatch telemetry).

Closes the v25→v29 capability-blocker root-cause gap:

  * Slice 34 Phase 0 probe (40/40 OK at 4-8 s p99) FALSIFIED all four
    upstream hypotheses (capacity / budget / prompt / network).
  * Hypothesis classifier verdict: ``harness_variable`` (confidence
    0.90) — "the v25→v29 100% TIMEOUT rate is a harness-side
    variable (orchestrator, sensor load, intake pressure, or GIL
    contention raising effective per-call latency)."
  * §48.7.4 demands stage-gate causal tracing of the dispatch path
    from ``_call_primary`` entry → ``session.post(...)`` exit to
    identify which orchestrator stage consumes the budget.

# Design discipline (operator bindings)

  * **No duplication:** composes the same monotonic-timing +
    structured-emit primitives as :mod:`loop_sink` (Slice 33 Arc 0).
    Same ``time.monotonic()`` cost, same fail-closed contract,
    same async-context-manager shape. The DIFFERENCE is *purpose*:

      * LoopSink fires only ABOVE threshold (50 ms default) —
        OBSERVATIONAL diagnostics for sink identification.
      * DispatchProfiler fires on EVERY stage entry/exit (no
        threshold) — CAUSAL tracing for the named-stage breakdown.

    These are two different telemetry channels with a shared
    measurement primitive. Separate loggers
    (``Ouroboros.DispatchProfiler`` vs ``Ouroboros.LoopSink``) so
    operators can filter / aggregate independently.

  * **Per-op aggregation:** a single op_id produces 5-6 stage
    events. Without aggregation, log grep would have to manually
    join them by op_id. The profiler maintains an in-memory
    per-op accumulator that emits a single structured
    ``[DispatchProfiler] op_summary`` row at op completion — one
    grep-friendly row per dispatch with the full stage breakdown.

  * **No hardcoding:** master flag + per-stage threshold + per-op
    accumulator size all env-knobbed.

  * **Fail-closed:** any internal error logs at WARN and swallows.
    Profiler MUST NEVER affect the dispatch outcome.

  * **Default OFF:** ``JARVIS_DISPATCH_PROFILER_ENABLED`` default
    FALSE — substrate ships separately from production v30
    instrumentation. Operators flip ON for the targeted v30 probe
    soak per §48.7.4 runbook.

# Public surface

  * :func:`dispatch_stage` — async context manager around one named
    stage; records into the per-op accumulator on the context exit.
  * :func:`op_session` — async context manager around an entire
    op's dispatch; on exit, emits the structured per-op summary
    row.
  * :func:`get_recent_op_summaries` — read-only access to recent
    per-op breakdowns for REPL / observability inspection.

# Stage naming convention (operator-bound §48.7.4)

Stages are user-defined strings. The 6 stages instrumented by Slice
34 Phase 2 wiring follow the operator's intent (with renames per
critical-scrutiny findings):

  * ``STAGE_PROMPT_ASSEMBLY``       — candidate_generator prompt build
  * ``STAGE_BUDGET_COMPUTATION``    — Slice 28 _compute_primary_budget
  * ``STAGE_AEGIS_AUTH_LOOKUP``     — Slice 31 session bearer fetch
  * ``STAGE_AEGIS_LEASE_ACQUIRE``   — Slice 2B-ii X-JARVIS-Lease fetch
  * ``STAGE_HTTP_DISPATCH``         — aiohttp session.post(...) await
  * ``STAGE_RESPONSE_PARSE``        — body decode + token accounting
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from threading import Lock
from typing import (
    AsyncIterator,
    Deque,
    Dict,
    List,
    Optional,
)


logger = logging.getLogger("Ouroboros.DispatchProfiler")


# ============================================================================
# Env knobs
# ============================================================================


_ENABLED_ENV: str = "JARVIS_DISPATCH_PROFILER_ENABLED"
_OP_SUMMARY_RING_SIZE_ENV: str = (
    "JARVIS_DISPATCH_PROFILER_OP_SUMMARY_RING_SIZE"
)
_STAGE_LOG_LEVEL_ENV: str = "JARVIS_DISPATCH_PROFILER_STAGE_LOG_LEVEL"

_DEFAULT_OP_SUMMARY_RING_SIZE: int = 256
_DEFAULT_STAGE_LOG_LEVEL: str = "DEBUG"


def is_enabled() -> bool:
    """Default FALSE — substrate ships before behaviour change.
    Operators flip ON for targeted v30 probe per §48.7.4 runbook."""
    raw = os.environ.get(_ENABLED_ENV, "").strip().lower()
    if not raw:
        return False
    return raw in ("1", "true", "yes", "on")


def _op_summary_ring_size() -> int:
    try:
        raw = os.environ.get(_OP_SUMMARY_RING_SIZE_ENV, "").strip()
        if not raw:
            return _DEFAULT_OP_SUMMARY_RING_SIZE
        return max(8, int(raw))
    except (TypeError, ValueError):
        return _DEFAULT_OP_SUMMARY_RING_SIZE


def _stage_log_level() -> int:
    """Per-stage log level — defaults to DEBUG so per-stage rows
    don't flood INFO during normal operation; operators can raise
    to INFO during targeted diagnostic soaks."""
    raw = os.environ.get(_STAGE_LOG_LEVEL_ENV, "").strip().upper()
    if not raw:
        raw = _DEFAULT_STAGE_LOG_LEVEL
    return getattr(logging, raw, logging.DEBUG)


# ============================================================================
# Per-op accumulator
# ============================================================================


@dataclass
class StageRecord:
    """One stage timing within an op's dispatch."""

    stage_name: str
    duration_ms: float
    outcome: str = "ok"               # ok / error
    error_class: str = ""


@dataclass
class OpDispatchSummary:
    """Per-op accumulated stage breakdown. Emitted as a single
    structured log row at op_session() exit + recorded into the
    ring buffer for REPL inspection."""

    op_id: str
    model_id: str
    route: str
    started_unix: float
    total_duration_ms: float = 0.0
    stages: List[StageRecord] = field(default_factory=list)
    outcome: str = "ok"               # ok / error — overall
    error_class: str = ""

    def to_log_kv(self) -> str:
        """Render as key=value pairs for a single grep-friendly row.

        Format::

            op=<op_id> model=<model> route=<route> total_ms=<f>
            outcome=<o> stages=<count>
            stage_<NAME>_ms=<f> stage_<NAME>_outcome=<o>
            ...
        """
        parts = [
            f"op={self.op_id[:16]}",
            f"model={self.model_id}",
            f"route={self.route}",
            f"total_ms={self.total_duration_ms:.1f}",
            f"outcome={self.outcome}",
            f"stages={len(self.stages)}",
        ]
        if self.error_class:
            parts.append(f"error_class={self.error_class}")
        for s in self.stages:
            parts.append(f"stage_{s.stage_name}_ms={s.duration_ms:.1f}")
            if s.outcome != "ok":
                parts.append(
                    f"stage_{s.stage_name}_outcome={s.outcome}"
                )
        return " ".join(parts)


# ============================================================================
# Module-level state
# ============================================================================


# Active per-op accumulators — keyed by (op_id, model_id) so the
# same op_id targeted across multiple models keeps separate stage
# breakdowns. Tracked in a dict guarded by a Lock for thread-safe
# concurrent-op access.
_active_ops: Dict[str, OpDispatchSummary] = {}
_active_ops_lock = Lock()

# Recent completed op summaries for REPL inspection.
_recent_summaries: Deque[OpDispatchSummary] = deque(
    maxlen=_DEFAULT_OP_SUMMARY_RING_SIZE,
)
_recent_summaries_lock = Lock()


def _active_key(op_id: str, model_id: str) -> str:
    return f"{op_id}|{model_id}"


# ============================================================================
# Public API
# ============================================================================


@asynccontextmanager
async def op_session(
    *,
    op_id: str,
    model_id: str,
    route: str,
) -> AsyncIterator[Optional[OpDispatchSummary]]:
    """Async context manager around one op's complete dispatch.

    Inside the ``async with`` body, downstream code can call
    :func:`dispatch_stage` (any number of times) to record stages.
    On exit, a single structured ``[DispatchProfiler] op_summary``
    row is emitted with the full per-stage breakdown.

    Usage::

        async with op_session(op_id=ctx.op_id, model_id=model,
                              route=ctx.provider_route) as summary:
            async with dispatch_stage("STAGE_PROMPT_ASSEMBLY",
                                       op_id=ctx.op_id,
                                       model_id=model):
                prompt = build_prompt(...)
            ...

    NEVER raises into the caller. When disabled, yields ``None``
    and consumers MUST tolerate None gracefully (the
    :func:`dispatch_stage` no-ops too).
    """
    if not is_enabled():
        yield None
        return

    # Fault-tolerant setup — if accumulator init fails (lock fault,
    # OOM, anything), still yield None so the caller body executes
    # normally. NEVER raise from profiler into the dispatch path.
    summary: Optional[OpDispatchSummary] = None
    key: str = ""
    setup_failed = False
    try:
        summary = OpDispatchSummary(
            op_id=op_id,
            model_id=model_id,
            route=route,
            started_unix=time.time(),
        )
        key = _active_key(op_id, model_id)
        with _active_ops_lock:
            _active_ops[key] = summary
    except Exception as exc:  # noqa: BLE001 — fail-closed
        setup_failed = True
        logger.warning(
            "[DispatchProfiler] op_session setup failed: %s — "
            "yielding None to caller", exc,
        )

    t0 = time.monotonic()
    op_outcome = "ok"
    op_error_class = ""
    try:
        yield summary
    except asyncio.CancelledError:
        op_outcome = "cancelled"
        raise
    except Exception as exc:  # noqa: BLE001
        op_outcome = "error"
        op_error_class = type(exc).__name__
        raise
    finally:
        # Skip teardown entirely if setup failed — there's no
        # accumulator to flush + the active_ops dict was never
        # mutated.
        if not setup_failed and summary is not None:
            try:
                with _active_ops_lock:
                    _active_ops.pop(key, None)
                summary.total_duration_ms = (time.monotonic() - t0) * 1000.0
                summary.outcome = op_outcome
                summary.error_class = op_error_class
                with _recent_summaries_lock:
                    _recent_summaries.append(summary)
                logger.info(
                    "[DispatchProfiler] op_summary %s",
                    summary.to_log_kv(),
                )
            except Exception as inner:  # noqa: BLE001 — fail-closed
                logger.warning(
                    "[DispatchProfiler] op_session exit emit failed: %s",
                    inner,
                )


@asynccontextmanager
async def dispatch_stage(
    stage_name: str,
    *,
    op_id: str,
    model_id: str,
) -> AsyncIterator[None]:
    """Async context manager around one dispatch stage.

    Records the stage's duration into the parent op's accumulator
    (if an :func:`op_session` is active) AND emits a per-stage log
    row at the configured level. Both surfaces are independent —
    operators see grep-friendly per-stage rows in real time AND a
    rolled-up per-op summary at op completion.

    NEVER raises into the caller. When disabled, the body runs
    unwrapped (zero overhead beyond the env check).
    """
    if not is_enabled():
        yield
        return

    t0 = time.monotonic()
    stage_outcome = "ok"
    stage_error_class = ""
    try:
        yield
    except asyncio.CancelledError:
        stage_outcome = "cancelled"
        raise
    except Exception as exc:  # noqa: BLE001
        stage_outcome = "error"
        stage_error_class = type(exc).__name__
        raise
    finally:
        try:
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            # Per-stage row
            logger.log(
                _stage_log_level(),
                "[DispatchProfiler] stage op=%s model=%s stage=%s "
                "duration_ms=%.1f outcome=%s%s",
                op_id[:16], model_id, stage_name,
                elapsed_ms, stage_outcome,
                f" error_class={stage_error_class}" if stage_error_class else "",
            )
            # Append to parent op's accumulator if active
            key = _active_key(op_id, model_id)
            with _active_ops_lock:
                summary = _active_ops.get(key)
            if summary is not None:
                summary.stages.append(StageRecord(
                    stage_name=stage_name,
                    duration_ms=elapsed_ms,
                    outcome=stage_outcome,
                    error_class=stage_error_class,
                ))
        except Exception as inner:  # noqa: BLE001 — fail-closed
            logger.warning(
                "[DispatchProfiler] dispatch_stage(%s) exit emit failed: %s",
                stage_name, inner,
            )


def record_stage(
    stage_name: str,
    *,
    op_id: str,
    model_id: str,
    duration_ms: float,
    outcome: str = "ok",
    error_class: str = "",
) -> None:
    """Slice 35 — manual stage record helper for the dual-path
    profiler wiring.

    Use when async-context-manager wrapping would require dangerous
    indent restructuring (e.g., inside large branchy async functions
    like ``DoublewordProvider._generate_realtime``). Caller measures
    ``duration_ms`` themselves via ``time.monotonic()`` deltas.

    Records into the active op accumulator (if an :func:`op_session`
    is active for this op_id + model_id) AND emits a per-stage log
    row. NEVER raises into the caller — internal errors swallowed.
    """
    if not is_enabled():
        return
    try:
        logger.log(
            _stage_log_level(),
            "[DispatchProfiler] stage op=%s model=%s stage=%s "
            "duration_ms=%.1f outcome=%s%s",
            op_id[:16], model_id, stage_name,
            duration_ms, outcome,
            f" error_class={error_class}" if error_class else "",
        )
        # Slice 36 Phase 2 — model_id tolerant lookup. v31 surfaced
        # the bug: Slice 34's op_session uses (op_id, model_id_kwarg
        # from sentinel walker, e.g. "Qwen3.5-35B") but Slice 35's
        # record_stage call sites in DW provider use self._model
        # (provider default, e.g. "Qwen3.5-397B"). Exact key mismatch
        # → record_stage finds no active op → stage drops from
        # summary aggregation (per-stage log row still emitted —
        # that's how we diagnosed in v31).
        #
        # Fix: try exact (op_id, model_id) match first; on miss,
        # fall back to "any active op for this op_id" (typically
        # there's at most one concurrent op per op_id). This
        # preserves model_id when it matches AND tolerates
        # walker-rotation mismatches.
        key = _active_key(op_id, model_id)
        with _active_ops_lock:
            summary = _active_ops.get(key)
            if summary is None:
                # Tolerant fallback — find any active entry for op_id
                _prefix = f"{op_id}|"
                for k, s in _active_ops.items():
                    if k.startswith(_prefix):
                        summary = s
                        break
        if summary is not None:
            summary.stages.append(StageRecord(
                stage_name=stage_name,
                duration_ms=duration_ms,
                outcome=outcome,
                error_class=error_class,
            ))
    except Exception as inner:  # noqa: BLE001 — fail-closed
        logger.warning(
            "[DispatchProfiler] record_stage(%s) failed: %s",
            stage_name, inner,
        )


def get_recent_op_summaries(limit: int = 50) -> List[OpDispatchSummary]:
    """Read-only snapshot of recent per-op dispatch summaries
    (most recent first). For REPL inspection + observability."""
    with _recent_summaries_lock:
        items = list(_recent_summaries)
    return list(reversed(items[-int(max(1, limit)):]))


def reset_for_tests() -> None:
    """Test isolation — clears active accumulators + ring buffer."""
    with _active_ops_lock:
        _active_ops.clear()
    with _recent_summaries_lock:
        _recent_summaries.clear()


__all__ = [
    "OpDispatchSummary",
    "StageRecord",
    "dispatch_stage",
    "get_recent_op_summaries",
    "is_enabled",
    "op_session",
    "reset_for_tests",
]
