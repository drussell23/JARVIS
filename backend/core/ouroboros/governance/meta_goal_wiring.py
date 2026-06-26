"""Live wiring for the Adaptive Meta-Goal Aggregator (the no-caller fix).

THE GAP
-------
:mod:`~backend.core.ouroboros.governance.meta_goal_aggregator` was fully built
and tested but had **no live caller** -- nothing fed it the pooled single-file
ops, and nothing dispatched the Meta-Goal DAGs it produced. So N isolated
single-file ops still dispatched serially (``single_file_op``, no fan-out).
This module is **pure wiring** -- it adds no aggregator logic. It only:

1. **Feeds** the aggregator: :func:`pooled_op_from_ctx` turns a pre-generation
   single-file :class:`OperationContext` into a
   :class:`~backend.core.ouroboros.governance.meta_goal_aggregator.PooledOp`.
   (Multi-file / no-target ops return ``None`` -> the caller keeps the legacy
   single-file dispatch unchanged.)
2. **Drains + dispatches**: :func:`dispatch_ready_bundles` calls the
   aggregator's existing :meth:`MetaGoalAggregator.drain_ready_bundles` and
   routes each ready Meta-Goal as ONE op into the EXISTING fan-out path
   (:func:`~backend.core.ouroboros.governance.parallel_dispatch.enforce_evaluate_fanout`
   with ``force=True``) -> swarm -> DAGComposer partial-recompose. No new
   dispatcher, no new scheduler, no new pool.
3. **Runs the loop**: :func:`start_meta_goal_drain_loop` /
   :func:`stop_meta_goal_drain_loop` are mixin-style methods the
   GovernedLoopService binds to drive a short background drain tick ALONGSIDE
   the failover / sensor loops (the same boot site that starts the failover
   loop). The aggregator's own master flag (``JARVIS_META_GOAL_AGGREGATOR_
   ENABLED``, default ``false``) gates everything.

Gating + fail-soft
------------------
- **Master OFF -> byte-identical**: ``drain_ready_bundles`` returns ``[]`` when
  the master flag is off, so no Meta-Goal ever forms and no fan-out is
  triggered. The drain loop is never even started (see
  :func:`start_meta_goal_drain_loop`). Pooled ops -- if any were offered -- are
  consumed by the legacy single-file path via :meth:`pending_ops`.
- **Aggregator error -> legacy fall-through**: a ``drain_ready_bundles`` raise
  is swallowed (returns ``[]``); a per-bundle dispatch raise is swallowed and
  the next bundle still dispatches. The op is never lost -- an un-bundled op
  stays pooled (retrievable via :meth:`pending_ops`) for the legacy single-file
  flush; a bundle that failed to dispatch leaves its ops drained-but-not-
  fanned-out (the swarm/L2/DLQ owns the follow-up, exactly as a normal failed
  fan-out).
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Callable, List, Optional, Tuple

import time

from backend.core.ouroboros.governance.meta_goal_aggregator import (
    MetaGoalAggregator,
    MetaGoalBundle,
    PooledOp,
    meta_goal_aggregator_enabled,
    meta_goal_coalesce_window_s,
)
from backend.core.ouroboros.governance.parallel_dispatch import (
    FanoutResult,
    enforce_evaluate_fanout,
)

logger = logging.getLogger("Ouroboros.MetaGoalWiring")

_TICK_INTERVAL_FLAG = "JARVIS_META_GOAL_TICK_INTERVAL_S"
_DEFAULT_TICK_INTERVAL_S = 5.0

# CONSTRAINT 4 -- absolute anti-zombie ceiling on a proof-in-flight hold.
_PROOF_MAX_WAIT_FLAG = "JARVIS_META_GOAL_PROOF_MAX_WAIT_S"
_DEFAULT_PROOF_MAX_WAIT_S = 15.0


def proof_max_wait_s() -> float:
    """Absolute ceiling (seconds) on how long an op may pause the coalescing
    window while its disjointness proof is in-flight. Default 15.0; ``0`` ->
    no hold at all (immediate fail-closed). Env-tunable, never hardcoded."""
    raw = os.environ.get(_PROOF_MAX_WAIT_FLAG, "").strip()
    if not raw:
        return _DEFAULT_PROOF_MAX_WAIT_S
    try:
        return max(0.0, float(raw))
    except ValueError:
        return _DEFAULT_PROOF_MAX_WAIT_S


def mark_proof_in_flight(host: Any, op_id: str) -> None:
    """Record that a disjointness proof (a CollisionMatrix JIT build) is
    in-flight for ``op_id`` -- the coalescing window PAUSES its age-out
    countdown for this op until the proof resolves OR the absolute ceiling
    (:func:`proof_max_wait_s`) elapses. Stamped when the self-warming JIT is
    triggered for the op's files. Idempotent."""
    state = getattr(host, "_meta_goal_proof_in_flight", None)
    if state is None:
        state = {}
        host._meta_goal_proof_in_flight = state
    if op_id not in state:
        state[op_id] = time.monotonic()


def clear_proof_in_flight(host: Any, op_id: str) -> None:
    """Clear the in-flight proof marker for ``op_id`` (proof resolved). The op
    can then bundle (if disjoint) or fall through (genuinely single/coupled).
    Idempotent."""
    state = getattr(host, "_meta_goal_proof_in_flight", None)
    if state is not None:
        state.pop(op_id, None)


# ---------------------------------------------------------------------------
# Feed: OperationContext -> PooledOp
# ---------------------------------------------------------------------------


def pooled_op_from_ctx(ctx: Any) -> Optional[PooledOp]:
    """Build a :class:`PooledOp` from a pre-generation single-file op context.

    Returns ``None`` (keep legacy single-file dispatch) when the ctx is NOT a
    clean single-file op:

    - more than one ``target_files`` entry -> a genuinely coupled/multi-file
      op (the aggregator must never co-bundle it; it owns its own fan-out).
    - zero ``target_files`` -> nothing to disjoint-partition on.
    - missing ``op_id`` -> cannot key the pool.

    Never raises -- a malformed ctx yields ``None`` and the caller falls
    through to the legacy path.
    """
    try:
        op_id = str(getattr(ctx, "op_id", "") or "").strip()
        if not op_id:
            return None
        target_files = tuple(getattr(ctx, "target_files", None) or ())
        if len(target_files) != 1:
            return None
        file_path = str(target_files[0] or "").strip()
        if not file_path:
            return None
        goal = str(getattr(ctx, "goal", "") or "")
        repo = str(getattr(ctx, "repo", "") or "jarvis")
        return PooledOp(
            op_id=op_id,
            file_path=file_path,
            rationale=goal,
            repo=repo,
        )
    except Exception:  # noqa: BLE001 -- feed is best-effort; legacy fallthrough
        logger.debug("pooled_op_from_ctx failed; op stays single-file", exc_info=True)
        return None


def offer_ctx(host: Any, ctx: Any) -> bool:
    """Feed a single-file op into the aggregator pool (the intake seam).

    Returns ``True`` IFF the op was pooled for possible Meta-Goal aggregation
    (the caller must then NOT also submit it directly -- the drain loop owns
    its dispatch: it either fans out as part of a Meta-Goal or is flushed to
    the legacy single-file path once it ages past the coalescing window).
    Returns ``False`` (caller keeps the EXISTING single-file dispatch
    unchanged) when:

    - the master flag is OFF (byte-identical),
    - the drain loop / aggregator is not wired (no consumer),
    - the ctx is not a clean single-file op (:func:`pooled_op_from_ctx` -> None),
    - anything raises (fail-soft -> legacy path; op never lost).

    The original ``ctx`` is retained on the host so an un-bundled (aged-out)
    op can be flushed to ``_bg_pool.submit`` with its full identity intact.
    """
    try:
        if not meta_goal_aggregator_enabled():
            return False
        agg = getattr(host, "_meta_goal_aggregator", None)
        if agg is None:
            return False
        op = pooled_op_from_ctx(ctx)
        if op is None:
            return False
        pending = getattr(host, "_meta_goal_pending_ctx", None)
        if pending is None:
            pending = {}
            host._meta_goal_pending_ctx = pending
        agg.offer(op)
        pending[op.op_id] = ctx
        logger.debug(
            "[MetaGoalWiring] pooled single-file op=%s file=%s for aggregation",
            op.op_id, op.file_path,
        )
        return True
    except Exception:  # noqa: BLE001 -- feed is best-effort; legacy fallthrough
        logger.debug(
            "[MetaGoalWiring] offer_ctx failed; op falls through to legacy "
            "single-file dispatch (not lost)",
            exc_info=True,
        )
        return False


async def _flush_aged_ops(host: Any, aggregator: MetaGoalAggregator) -> None:
    """Flush un-bundled ops that have aged past the coalescing window to the
    legacy single-file pool (``_bg_pool.submit``), so a genuinely-single or
    coupled op is NEVER stranded in the pool. Fail-soft per op.

    An op is flushed when it is still pooled (was not part of a bundle) AND
    older than the coalescing window -- i.e. the aggregator had its full
    window to find it a disjoint sibling and could not. We remove it from the
    aggregator pool by re-offering the surviving newer ops (the aggregator's
    ``offer`` is idempotent on op_id; we never mutate its internals).
    """
    pool = getattr(host, "_bg_pool", None)
    pending = getattr(host, "_meta_goal_pending_ctx", None)
    if pending is None:
        host._meta_goal_pending_ctx = pending = {}

    try:
        pooled = aggregator.pending_ops()
    except Exception:  # noqa: BLE001
        return

    # Drop ctx entries whose op is no longer pooled (it got bundled + drained):
    pooled_ids = {op.op_id for op in pooled}
    for stale in [oid for oid in pending if oid not in pooled_ids]:
        pending.pop(stale, None)

    if not pooled:
        return

    now = time.monotonic()
    window_s = meta_goal_coalesce_window_s()
    aged = [op for op in pooled if (now - op.offered_at) > window_s]
    if not aged:
        return

    # CONSTRAINT 4 -- state-aware window: an op whose disjointness proof is
    # in-flight PAUSES its age-out until the proof resolves OR the absolute
    # anti-zombie ceiling elapses (a hung JIT must not pause forever). When the
    # ceiling is exceeded we FAIL-CLOSED: release the hold + flush to legacy
    # single-file (COLLIDE / single_file_op), never an infinite zombie hold.
    proof_state = getattr(host, "_meta_goal_proof_in_flight", None) or {}
    ceiling = proof_max_wait_s()
    held: List[str] = []
    releasable_aged = []
    for op in aged:
        started = proof_state.get(op.op_id)
        if started is not None and ceiling > 0.0 and (now - started) < ceiling:
            # Proof still building within the ceiling -> hold (do NOT age out).
            held.append(op.op_id)
            continue
        if started is not None:
            # Ceiling exceeded -> fail-closed: drop the hold, let it flush.
            logger.warning(
                "[MetaGoalWiring] op=%s proof in-flight exceeded ceiling "
                "%.1fs -> fail-closed release -> legacy single-file dispatch",
                op.op_id, ceiling,
            )
            proof_state.pop(op.op_id, None)
        releasable_aged.append(op)

    if held:
        logger.debug(
            "[MetaGoalWiring] %d op(s) held (proof in-flight, within ceiling)",
            len(held),
        )

    aged = releasable_aged
    if not aged:
        return

    flushed_ids: List[str] = []
    for op in aged:
        ctx = pending.get(op.op_id)
        if ctx is None or pool is None:
            # No retained ctx / no pool to flush into -> drop it from the pool
            # so it cannot grow unbounded; the legacy intake owns the op.
            flushed_ids.append(op.op_id)
            pending.pop(op.op_id, None)
            continue
        try:
            await pool.submit(ctx)
            flushed_ids.append(op.op_id)
            pending.pop(op.op_id, None)
            logger.info(
                "[MetaGoalWiring] op=%s aged out of coalescing window -> "
                "legacy single-file dispatch (no disjoint sibling found)",
                op.op_id,
            )
        except Exception:  # noqa: BLE001 -- never lose the op; retry next tick
            logger.warning(
                "[MetaGoalWiring] legacy flush submit failed for aged op=%s "
                "(stays pooled, retried next tick)",
                op.op_id, exc_info=True,
            )
            # keep ctx + leave op pooled -> retried next tick (never lost)

    if flushed_ids:
        aggregator.drop_ops(flushed_ids)


# ---------------------------------------------------------------------------
# Bundle -> synthetic generation (the EXISTING fan-out path input shape)
# ---------------------------------------------------------------------------


def synthetic_generation_for_bundle(bundle: MetaGoalBundle) -> Any:
    """A ``generation``-shaped artifact for one Meta-Goal bundle.

    :func:`enforce_evaluate_fanout` re-extracts the candidate files from a
    ``generation`` (via
    :func:`~backend.core.ouroboros.governance.parallel_dispatch.extract_candidate_files`)
    and rebuilds the multi-unit graph itself -- so we hand it the EXACT shape
    GENERATE emits for a coordinated multi-file candidate: ONE candidate
    carrying a ``files: [{file_path, full_content, rationale}, ...]`` list, one
    entry per bundled single-file op. The aggregator already proved these ops
    are pairwise-disjoint, so the rebuilt graph is the same multi-node DAG.
    """

    class _SyntheticGeneration:
        __slots__ = ("candidates",)

        def __init__(self, candidates: Tuple[dict, ...]) -> None:
            self.candidates = candidates

    files = [
        {
            "file_path": u.target_files[0],
            "full_content": "",
            "rationale": u.goal or f"fix {u.target_files[0]}",
        }
        for u in bundle.graph.units
        if u.target_files
    ]
    return _SyntheticGeneration(candidates=({"files": files},))


# ---------------------------------------------------------------------------
# Drain + dispatch each ready Meta-Goal into the EXISTING fan-out path
# ---------------------------------------------------------------------------


async def dispatch_ready_bundles(
    aggregator: MetaGoalAggregator,
    *,
    scheduler: Any,
    gate: Any = None,
    posture_fn: Optional[Callable[[], Tuple[Any, Any]]] = None,
    repo: str = "jarvis",
    wait_timeout_s: Optional[float] = None,
) -> List[FanoutResult]:
    """Drain ready Meta-Goal bundles and dispatch each via the swarm fan-out.

    Returns the :class:`FanoutResult` of every bundle that reached the swarm
    (empty when nothing was ready / master-off / fail-soft swallow).

    Fail-soft at TWO levels:

    1. ``drain_ready_bundles`` raising -> swallow, return ``[]`` (ops stay
       pooled for the legacy single-file flush -- never lost).
    2. a single bundle's ``enforce_evaluate_fanout`` raising -> swallow that
       bundle, continue with the rest (one bad bundle never sinks the others).

    When the scheduler is ``None`` the function is a no-op -- without a
    scheduler there is nothing to fan out into, so we leave the ops pooled.
    """
    if scheduler is None:
        return []
    try:
        bundles = aggregator.drain_ready_bundles()
    except Exception:  # noqa: BLE001 -- aggregator error -> legacy fallthrough
        logger.warning(
            "[MetaGoalWiring] drain_ready_bundles failed; pooled ops fall "
            "through to legacy single-file dispatch (op not lost)",
            exc_info=True,
        )
        return []

    if not bundles:
        return []

    results: List[FanoutResult] = []
    for bundle in bundles:
        try:
            generation = synthetic_generation_for_bundle(bundle)
            result = await enforce_evaluate_fanout(
                op_id=bundle.meta_goal_id,
                generation=generation,
                scheduler=scheduler,
                repo=repo,
                gate=gate,
                posture_fn=posture_fn,
                wait_timeout_s=wait_timeout_s,
                force=True,
            )
            results.append(result)
            logger.info(
                "[MetaGoalWiring] meta=%s dispatched -> fan-out outcome=%s "
                "n_units=%d (run-#3 single_file_op fix: %d disjoint ops -> 1 "
                "fan-out)",
                bundle.meta_goal_id,
                getattr(result.outcome, "value", result.outcome),
                len(bundle.graph.units),
                len(bundle.graph.units),
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- one bad bundle never sinks the rest
            logger.warning(
                "[MetaGoalWiring] meta=%s fan-out dispatch failed (fail-soft); "
                "remaining bundles still dispatch",
                bundle.meta_goal_id,
                exc_info=True,
            )
    return results


# ---------------------------------------------------------------------------
# Drain-loop lifecycle (mixin-style methods the GovernedLoopService binds)
# ---------------------------------------------------------------------------


def meta_goal_tick_interval_s() -> float:
    """Drain-loop cadence -- ``JARVIS_META_GOAL_TICK_INTERVAL_S`` (default 5s)."""
    raw = os.environ.get(_TICK_INTERVAL_FLAG, "").strip()
    if not raw:
        return _DEFAULT_TICK_INTERVAL_S
    try:
        v = float(raw)
    except ValueError:
        return _DEFAULT_TICK_INTERVAL_S
    return max(0.01, v)


def _resolve_gate(host: Any) -> Any:
    gate = getattr(host, "_memory_pressure_gate", None)
    if gate is not None:
        return gate
    try:
        from backend.core.ouroboros.governance.memory_pressure_gate import (
            get_default_gate,
        )

        return get_default_gate()
    except Exception:  # noqa: BLE001
        return None


def _resolve_posture_fn(host: Any) -> Optional[Callable[[], Tuple[Any, Any]]]:
    fn = getattr(host, "_posture_fn", None)
    if callable(fn):
        return fn
    return None


def start_meta_goal_drain_loop(host: Any) -> None:
    """Start the Meta-Goal aggregator drain loop as a peer background task.

    Bound onto :class:`GovernedLoopService` as ``_start_meta_goal_drain_loop``.
    Mirrors ``_start_failover_loop``: gated on
    ``JARVIS_META_GOAL_AGGREGATOR_ENABLED`` (default OFF) -> when OFF, NO task
    is created and NO aggregator is wired (byte-identical). Idempotent. Fully
    fail-soft -- a wiring error is logged and swallowed; it NEVER blocks boot.
    """
    try:
        if not meta_goal_aggregator_enabled():
            return  # OFF byte-identical: no aggregator, no loop.
        existing = getattr(host, "_meta_goal_drain_task", None)
        if existing is not None and not existing.done():
            return  # idempotent -- already running.
        agg = getattr(host, "_meta_goal_aggregator", None)
        if agg is None:
            agg = MetaGoalAggregator(
                gate=_resolve_gate(host),
                posture_fn=_resolve_posture_fn(host),
                oracle=getattr(host, "_oracle", None),
            )
            host._meta_goal_aggregator = agg
        host._meta_goal_drain_task = asyncio.create_task(
            _meta_goal_drain_loop(host),
            name="meta_goal_drain_loop",
        )
        logger.info(
            "[MetaGoalWiring] Meta-Goal aggregator drain loop started "
            "(JARVIS_META_GOAL_AGGREGATOR_ENABLED=true) -- pooled disjoint "
            "single-file ops now fan out via the swarm",
        )
    except Exception as exc:  # noqa: BLE001 -- never block boot
        logger.warning(
            "[MetaGoalWiring] drain loop start failed (non-fatal): %r", exc,
        )
        host._meta_goal_drain_task = None


async def _prewarm_and_mark_proofs(host: Any, aggregator: Any) -> None:
    """Async self-warming pass run before the (sync) disjointness partition.

    For every pooled op, mark its disjointness proof in-flight (so the
    coalescing window pauses) and JIT-warm the Oracle for the op's file via
    :func:`prewarm_collision_files` (reuses ``ensure_file_indexed``). This
    moves the JIT off the sync ``_coupled_files`` path onto the async loop --
    the partition that follows then reads a warmed index. Gated OFF -> no-op
    (no proofs marked, no warm). Fully fail-soft."""
    try:
        from backend.core.ouroboros.governance.collision_matrix import (
            prewarm_collision_files,
            self_warming_enabled,
        )
        if not self_warming_enabled():
            return
        oracle = getattr(aggregator, "_oracle", None) or getattr(host, "_oracle", None)
        if oracle is None:
            return
        try:
            pooled = aggregator.pending_ops()
        except Exception:  # noqa: BLE001
            return
        if not pooled:
            return
        files = []
        for op in pooled:
            mark_proof_in_flight(host, op.op_id)
            if getattr(op, "file_path", None):
                files.append(op.file_path)
        if files:
            await prewarm_collision_files(oracle, files)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 -- never break the drain loop
        logger.debug("[MetaGoalWiring] prewarm/mark proofs failed", exc_info=True)


def _clear_all_proofs(host: Any) -> None:
    """Clear every in-flight proof marker after a drain tick (proofs resolved
    synchronously during the partition). Fail-soft; idempotent."""
    state = getattr(host, "_meta_goal_proof_in_flight", None)
    if isinstance(state, dict):
        state.clear()


async def _meta_goal_drain_loop(host: Any) -> None:
    """Tick :func:`dispatch_ready_bundles` until shutdown. Fully fail-soft;
    CancelledError exits cleanly (uses ``asyncio.sleep``, Python 3.9+ safe)."""
    try:
        while True:
            try:
                interval = meta_goal_tick_interval_s()
                agg = getattr(host, "_meta_goal_aggregator", None)
                scheduler = getattr(host, "_subagent_scheduler", None)
                if agg is not None:
                    # Self-Warming Oracle seam: async-JIT-warm the Oracle for
                    # the pooled ops' files BEFORE the (sync) disjointness
                    # partition, marking each op's proof in-flight so the
                    # coalescing window pauses during the warm (bounded by the
                    # absolute ceiling). No-op when self-warming is OFF.
                    await _prewarm_and_mark_proofs(host, agg)
                if agg is not None and scheduler is not None:
                    await dispatch_ready_bundles(
                        agg,
                        scheduler=scheduler,
                        gate=_resolve_gate(host),
                        posture_fn=_resolve_posture_fn(host),
                    )
                if agg is not None:
                    # Proofs resolved this tick -> clear the holds so resolved
                    # ops can bundle or genuinely fall through next pass.
                    _clear_all_proofs(host)
                if agg is not None:
                    # Flush genuinely-single / coupled ops that aged past the
                    # coalescing window to the legacy single-file path so they
                    # are never stranded. Fail-soft.
                    await _flush_aged_ops(host, agg)
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 -- loop never dies
                logger.debug(
                    "[MetaGoalWiring] drain loop iteration error: %r", exc,
                )
                try:
                    await asyncio.sleep(_DEFAULT_TICK_INTERVAL_S)
                except asyncio.CancelledError:
                    raise
    except asyncio.CancelledError:
        return


async def stop_meta_goal_drain_loop(host: Any) -> None:
    """Cancel the drain loop cleanly on shutdown. Fail-soft; idempotent;
    never raises into the shutdown path. Bound as ``_stop_meta_goal_drain_loop``.
    """
    task = getattr(host, "_meta_goal_drain_task", None)
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 -- never block shutdown
            logger.debug(
                "[MetaGoalWiring] drain loop cancel swallowed", exc_info=True,
            )
    host._meta_goal_drain_task = None


__all__ = [
    "clear_proof_in_flight",
    "dispatch_ready_bundles",
    "mark_proof_in_flight",
    "meta_goal_tick_interval_s",
    "pooled_op_from_ctx",
    "proof_max_wait_s",
    "start_meta_goal_drain_loop",
    "stop_meta_goal_drain_loop",
    "synthetic_generation_for_bundle",
]
