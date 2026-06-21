"""
Generate-Park Wrapper — Stage 1.6 Slice 2b
==========================================

The seam that wires the park substrate into the GENERATE phase.  Sits
between :class:`GENERATERunner` (line ~493 in
``phase_runners/generate_runner.py``) and ``orch._generator.generate``.

Three execution paths
---------------------

::

    maybe_park_or_resume(orch, ctx, deadline, gen_timeout, outer_grace_s)
            │
            ├─ RESUME path
            │    └─ master flag on AND BG pool reports is_resumed_dispatch(op_id)
            │       → fetch ParkedOpResult from store
            │       → result.status=="completed" → return result.payload["generation"]
            │       → result.status in {cancelled, ttl_expired, evicted}
            │          → raise asyncio.CancelledError(reason)
            │
            ├─ PARK-EMIT path
            │    └─ master flag on AND should_park_for_route(...) True AND pool bound
            │       → build ParkDescriptor (deadline, gen_timeout, outer_grace, route)
            │       → ParkedOpStore.park(op_id, attempt_seq, descriptor)
            │       → orch._ledger.record(PARKED_GENERATE, entry_id=attempt-N)
            │       → spawn out-of-pool continuation that does the real provider
            │          call, stores result, then BG pool submit_for_resume(ctx)
            │       → raise ParkRequested(signal)  ← BG worker catches, frees slot
            │
            └─ LEGACY path  (master off OR no pool OR not should_park OR …)
                 └─ direct: await asyncio.wait_for(
                        orch._generator.generate(ctx, deadline),
                        timeout=gen_timeout + outer_grace_s,
                    )

Authority + composition discipline
----------------------------------
* Imports only the substrate (op_park_store, park_signal, ledger),
  the bind (`_governance_state.get_bound_bg_pool`), and stdlib.  Does
  NOT import the orchestrator, the BG pool class, or the candidate
  generator — receives ``orch`` as a duck-typed parameter that must
  expose ``._generator.generate(ctx, deadline)`` and (optionally)
  ``._ledger``.
* All env reads at call time (no module-level constants) — preserves
  monkey-patching + hot-reload semantics matching the rest of the
  governance layer.
* §33.5 lossless: the descriptor.payload carries primitives only.  The
  parked generation object itself rides through ``ParkedOpResult.payload``
  as an in-memory Python reference (no serialization needed; Slice 4
  could harden this for cross-process resume).
* Master flag default-FALSE per §33.1 — the wrapper falls back to the
  legacy direct-await path when JARVIS_BG_PARK_ENABLED is off.

Cross-references
----------------
* Slice 1 substrate: ``op_park_store.py``, ``park_signal.py``,
  ``OperationState.PARKED_GENERATE``.
* Slice 2a substrate: ``BackgroundAgentPool`` worker-loop ParkRequested
  handler, ``BackgroundOp.resumed``, ``should_park_for_route``.
* Slice 2b (this module): the actual seam.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger("Ouroboros.GenerateParkWrapper")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def maybe_park_or_resume(
    *,
    orch: Any,
    ctx: Any,
    deadline: datetime,
    gen_timeout: float,
    outer_grace_s: float,
) -> Any:
    """Park-aware GENERATE provider call.

    See module docstring for the three-path decision table.

    Parameters
    ----------
    orch:
        Duck-typed orchestrator with ``._generator.generate(ctx,
        deadline) -> GenerationResult`` and (optionally) ``._ledger``.
    ctx:
        Live OperationContext.  Must expose ``.op_id`` (str) and
        ``.provider_route`` (str) at minimum.
    deadline:
        Wall-clock provider deadline (passed through to the generator).
    gen_timeout:
        Phase generation budget in seconds (the inner cap).
    outer_grace_s:
        Grace seconds added on top of ``gen_timeout`` for the outer
        ``asyncio.wait_for`` gate.  Same shape as the legacy callsite.

    Returns
    -------
    GenerationResult (from the candidate generator, never None).

    Raises
    ------
    ParkRequested:
        On park-emit path — BG worker catches, frees slot.
    asyncio.CancelledError:
        On resume path when the parked result is not "completed"
        (cancelled / ttl_expired / evicted).
    asyncio.TimeoutError:
        On legacy path when the inner wait_for fires (identical to
        pre-1.6 behavior).
    Any:
        Whatever exception ``orch._generator.generate`` would raise
        on the legacy path (provider errors, etc).
    """
    # Lazy imports — keeps this module light if it's never invoked
    # AND avoids any chance of import order with orchestrator.py.
    from backend.core.ouroboros.governance.op_park_store import (
        park_enabled,
        should_park_for_route,
        get_default_store,
        ParkedOpStore,
    )
    from backend.core.ouroboros.governance.park_signal import (
        ParkDescriptor,
        ParkRequested,
        ParkSignal,
    )
    from backend.core.ouroboros.governance._governance_state import (
        get_bound_bg_pool,
    )

    ctx_op_id = str(getattr(ctx, "op_id", "") or "")
    provider_route = str(getattr(ctx, "provider_route", "") or "")
    pool = get_bound_bg_pool()
    master_on = park_enabled()

    # ----------------------------------------------------------------
    # Path 1 — RESUME
    # ----------------------------------------------------------------
    if master_on and pool is not None and ctx_op_id and pool.is_resumed_dispatch(ctx_op_id):
        attempt_seq = pool.get_park_attempt_seq(ctx_op_id)
        token = ParkedOpStore.make_token(ctx_op_id, attempt_seq)
        store = get_default_store()
        logger.info(
            "RESUME path: ctx_op_id=%s attempt=%d token=%s — fetching "
            "parked result from store",
            ctx_op_id, attempt_seq, token,
        )
        result = await store.result_for(token)
        if result is None:
            # Resume race: continuation evicted/dropped before we got
            # here.  Surface as cancellation rather than silently
            # re-issuing the provider call — re-issuing would violate
            # the no-double-dispatch invariant.
            logger.warning(
                "RESUME path: ctx_op_id=%s token=%s — no parked record "
                "(continuation evicted or never admitted); raising "
                "CancelledError",
                ctx_op_id, token,
            )
            raise asyncio.CancelledError(
                f"park resume failed: no record for token={token}"
            )
        if result.status != "completed":
            logger.warning(
                "RESUME path: ctx_op_id=%s token=%s status=%s reason=%r "
                "— park did not complete cleanly; raising CancelledError",
                ctx_op_id, token, result.status, result.reason,
            )
            raise asyncio.CancelledError(
                f"park resume failed: status={result.status} "
                f"reason={result.reason!r}"
            )
        # Materialize.  ``payload["generation"]`` is the in-memory
        # GenerationResult stored by the continuation; primitives
        # rounded by §33.5 are not relevant on this hot path.
        generation = result.payload.get("generation")
        if generation is None:
            logger.error(
                "RESUME path: ctx_op_id=%s token=%s — payload missing "
                "'generation' key; this is a continuation bug",
                ctx_op_id, token,
            )
            raise asyncio.CancelledError(
                f"park resume failed: payload missing generation"
            )
        logger.info(
            "RESUME path: ctx_op_id=%s token=%s — generation materialized "
            "from store",
            ctx_op_id, token,
        )
        return generation

    # ----------------------------------------------------------------
    # Path 2 — PARK-EMIT
    # ----------------------------------------------------------------
    queue_pressure = pool is not None and pool.queue_depth() > 0
    # Sovereign Transport Profiler Matrix (2026-06-20): a known batch-only op MUST
    # detach regardless of queue pressure (its provider call is a minutes-long async
    # batch poll). OperationContext is FROZEN, so we cannot stamp a tag on it (the
    # _resolve_effective_model docstring records the FrozenInstanceError that defeated
    # the old setattr pattern) — and the budget-seam tag would be set INSIDE generate()
    # anyway, AFTER this park decision. So resolve it HERE, directly from the immortal
    # profile: if any ranked DW model for this route is batch-only, the op will dispatch
    # via batch → detach. Over-parking is harmless (the continuation runs generate()
    # out-of-pool either way); under-parking re-wedges the worker. Fail-soft.
    _async_batch = _resolve_async_batch_payload(ctx, provider_route)
    if (
        master_on
        and pool is not None
        and ctx_op_id
        and should_park_for_route(
            provider_route,
            queue_pressure=queue_pressure,
            is_resumed=False,
            async_batch_payload=_async_batch,
        )
    ):
        # Determine attempt_seq.  For the first GENERATE call this is
        # 1; GENERATE_RETRY would bump this.  We resolve via the BG
        # op's ``park_attempt_seq`` (which the worker bumps on each
        # park-emit).  If we can't find a BackgroundOp for this ctx
        # (test harness without a real pool entry), default to 1.
        attempt_seq = _resolve_next_park_attempt(pool, ctx_op_id)
        descriptor = ParkDescriptor(
            kind="generate",
            payload={
                "deadline_iso": deadline.isoformat(),
                "gen_timeout": float(gen_timeout),
                "outer_grace_s": float(outer_grace_s),
                "provider_route": provider_route,
            },
        )
        store = get_default_store()
        token, fresh = await store.park(ctx_op_id, attempt_seq, descriptor)
        signal = ParkSignal(
            op_id=ctx_op_id,
            token=token,
            attempt_seq=attempt_seq,
            descriptor=descriptor,
            park_started_at=time.monotonic(),
        )
        if fresh:
            # First-time park — persist ledger entry + spawn continuation
            await _record_park_ledger(orch, ctx_op_id, attempt_seq, token)
            await _spawn_park_continuation(
                orch=orch,
                ctx=ctx,
                deadline=deadline,
                gen_timeout=gen_timeout,
                outer_grace_s=outer_grace_s,
                attempt_seq=attempt_seq,
                token=token,
                pool=pool,
            )
            logger.info(
                "PARK-EMIT: ctx_op_id=%s attempt=%d token=%s — "
                "continuation spawned, raising ParkRequested",
                ctx_op_id, attempt_seq, token,
            )
        else:
            logger.info(
                "PARK-EMIT idempotent: ctx_op_id=%s attempt=%d token=%s "
                "— already admitted, raising ParkRequested without "
                "respawning continuation",
                ctx_op_id, attempt_seq, token,
            )
        raise ParkRequested(signal)

    # ----------------------------------------------------------------
    # Path 3 — LEGACY direct-await (byte-identical to pre-1.6)
    # ----------------------------------------------------------------
    return await asyncio.wait_for(
        orch._generator.generate(ctx, deadline),
        timeout=gen_timeout + outer_grace_s,
    )


# ---------------------------------------------------------------------------
# Internals — kept here so the seam logic is one file, easy to review
# ---------------------------------------------------------------------------


def _resolve_async_batch_payload(ctx: Any, provider_route: str) -> bool:
    """True iff this op should be treated as an ASYNC_BATCH_PAYLOAD (detach the
    worker for a long async batch poll).

    Resolved directly from the immortal transport profile (NOT a ctx tag —
    OperationContext is frozen, and the budget-seam tag is set inside generate(),
    too late for this pre-dispatch decision). If ANY ranked DW model for this route
    is known batch-only, the op may dispatch via batch → detach. NEVER raises —
    on any failure returns False (legacy in-pool await; never wedges incorrectly,
    just forgoes the optimization)."""
    try:
        from backend.core.ouroboros.governance.dw_transport_profile import (
            get_transport_profile,
        )
        route = (provider_route or "").strip().lower()
        if route not in ("standard", "complex", "background"):
            return False
        from backend.core.ouroboros.governance.provider_topology import (
            get_topology,
        )
        models = get_topology().dw_models_for_route(route) or ()
        if not models:
            return False
        profile = get_transport_profile()
        return any(profile.is_batch_only(m) for m in models)
    except Exception:  # noqa: BLE001 — never raise from the park gate
        return False


def _resolve_next_park_attempt(pool: Any, ctx_op_id: str) -> int:
    """Return the next park attempt sequence for this ctx.

    Reads ``BackgroundOp.park_attempt_seq`` for the live op (if
    findable) and increments.  Defaults to 1 when no live op is
    tracked (test harness path).
    """
    # Pool tracks ops by pool-internal id; we have ctx_op_id.  Walk
    # ``_ops`` looking for a matching ctx.op_id.  Cheap (pool size is
    # bounded by JARVIS_BG_QUEUE_SIZE, default 16).
    try:
        for bg_op in pool._ops.values():
            ctx = getattr(bg_op, "context", None)
            if ctx is None:
                continue
            if str(getattr(ctx, "op_id", "") or "") == ctx_op_id:
                # Bump the BackgroundOp's counter so the next park
                # under this dispatch gets a fresh attempt_seq.
                next_seq = max(1, int(bg_op.park_attempt_seq) + 1)
                bg_op.park_attempt_seq = next_seq
                return next_seq
    except Exception:  # noqa: BLE001
        logger.debug(
            "_resolve_next_park_attempt: pool walk failed for ctx_op_id=%s",
            ctx_op_id, exc_info=True,
        )
    return 1


async def _record_park_ledger(
    orch: Any, ctx_op_id: str, attempt_seq: int, token: str,
) -> None:
    """Best-effort PARKED_GENERATE ledger entry.

    Composes the canonical OperationLedger.  Defensive: any failure
    here is logged and swallowed — losing the ledger entry should NOT
    prevent the park from proceeding (the in-memory store is still
    the source of truth for the resume continuation).
    """
    ledger = getattr(orch, "_ledger", None)
    if ledger is None:
        logger.debug(
            "PARK-EMIT: orch has no ._ledger attribute; skipping "
            "PARKED_GENERATE ledger entry for ctx_op_id=%s",
            ctx_op_id,
        )
        return
    try:
        from backend.core.ouroboros.governance.ledger import (
            LedgerEntry,
            OperationState,
        )
        await ledger.record(LedgerEntry(
            op_id=ctx_op_id,
            state=OperationState.PARKED_GENERATE,
            data={
                "token": token,
                "attempt_seq": attempt_seq,
                "kind": "generate",
            },
            entry_id=f"attempt-{attempt_seq}",
        ))
        logger.debug(
            "PARK-EMIT ledger: ctx_op_id=%s state=PARKED_GENERATE "
            "entry_id=attempt-%d",
            ctx_op_id, attempt_seq,
        )
    except Exception:  # noqa: BLE001 — ledger record is best-effort
        logger.warning(
            "PARK-EMIT ledger record failed for ctx_op_id=%s — park "
            "proceeds (in-memory store is source of truth)",
            ctx_op_id, exc_info=True,
        )


async def _spawn_park_continuation(
    *,
    orch: Any,
    ctx: Any,
    deadline: datetime,
    gen_timeout: float,
    outer_grace_s: float,
    attempt_seq: int,
    token: str,
    pool: Any,
) -> None:
    """Spawn the out-of-pool continuation task.

    The task:
      1. awaits the real provider call (orch._generator.generate)
      2. on success → store.complete(token, payload={"generation": gen})
                    → pool.submit_for_resume(ctx, attempt_seq)
      3. on failure / cancellation → store.cancel(token, reason)
                                  → pool.submit_for_resume(ctx, attempt_seq)
         (the resumed dispatch surfaces the failure to the caller as
          asyncio.CancelledError — see RESUME path)

    The task is registered with ``pool.register_park_continuation``
    so pool.stop() can cancel it gracefully on shutdown.

    Single-flight invariant: ``ParkedOpStore.park`` admits at most
    one record per (op_id, attempt_seq).  This function is only
    invoked when ``fresh=True``, so this is the only continuation
    for this token; no double-spawn.
    """
    from backend.core.ouroboros.governance.op_park_store import (
        get_default_store,
    )

    # Task #88d (2026-05-13) — fourth-layer coherence.
    # v14-rev8 surfaced that even with Task #88/#88b/#88c all firing
    # (inner=outer=floor=360s for thinking-on), the Claude call was
    # cancelled at elapsed=248s while its budget was 357.5s.  The
    # cancel source: THIS continuation's own asyncio.wait_for, whose
    # timeout (``gen_timeout + outer_grace_s``) inherits the LEGACY
    # GENERATE-phase wall (~200s for STANDARD route + 30s grace =
    # ~230s).  That wall was sized for in-pool calls where the BG
    # worker slot is held during the entire await — for an OUT-OF-POOL
    # continuation, the slot is already freed, so the legacy wall
    # doesn't serve its original purpose; it just falsely cancels
    # legitimate thinking-on streams.
    #
    # Same single-policy coherence rule as Task #88/#88b/#88c: when
    # the call is likely-thinking (signal reused for consistency),
    # widen the continuation's outer wait_for to match the inner /
    # outer / floor 360s budget — plus a small grace so the wait_for
    # never wins a race against a legitimate stream completion at the
    # 360s edge.
    #
    # Math after Task #88d:
    #   non-thinking IMMEDIATE: timeout = gen_timeout + outer_grace_s
    #     (legacy preserved)
    #   thinking-on STANDARD:   timeout = max(legacy,
    #     JARVIS_PARK_CONTINUATION_TIMEOUT_THINKING_S=390s)
    # The max() clamp is essential: never SHRINK below the legacy
    # value, only widen.
    _task_complexity = (
        getattr(ctx, "task_complexity", "") or ""
    ).strip().lower()
    _op_route = (
        getattr(ctx, "provider_route", "") or ""
    ).strip().lower()
    _likely_thinking = (
        _task_complexity not in ("", "trivial")
        and _op_route not in ("immediate",)
    )
    _legacy_timeout = gen_timeout + outer_grace_s
    if _likely_thinking:
        _thinking_cont_timeout = float(os.environ.get(
            "JARVIS_PARK_CONTINUATION_TIMEOUT_THINKING_S", "390.0",
        ))
        _continuation_timeout = max(_legacy_timeout, _thinking_cont_timeout)
    else:
        _continuation_timeout = _legacy_timeout
    # Sovereign Transport Profiler Matrix (2026-06-20) — continuation/batch budget
    # coherence. The reason this op was PARKED is that it is batch-bound; its inner
    # provider call gets the batch budget (force_batch → _compute_primary_budget →
    # JARVIS_DW_BATCH_TIMEOUT_S, default 300s). If the out-of-pool continuation's
    # OUTER wait_for stays at the legacy ~185s gen wall, it SEVERS the batch before
    # it completes — the live wedge: 7 batches completed at the DW level (one op at
    # 257s) yet every op errored because the 185s continuation cancelled them first.
    # Widen the continuation to the SAME batch floor (batch_cap + overhead, ~330s)
    # the budget layer already granted — pure alignment, NOT a blind extension.
    # max() only widens, never shrinks. Fail-soft → legacy on any error.
    try:
        if _resolve_async_batch_payload(ctx, _op_route):
            from backend.core.ouroboros.governance.candidate_generator import (
                force_batch_gen_timeout_floor_s as _batch_floor,
            )
            _continuation_timeout = max(_continuation_timeout, _batch_floor())
    except Exception:  # noqa: BLE001 — never raise from the timeout calc
        pass

    async def _continuation() -> None:
        store = get_default_store()
        try:
            # The actual provider call — out-of-pool, slot is free.
            generation = await asyncio.wait_for(
                orch._generator.generate(ctx, deadline),
                timeout=_continuation_timeout,
            )
            await store.complete(
                token, payload={"generation": generation},
            )
            logger.info(
                "park continuation: ctx_op_id=%s token=%s — provider "
                "completed; resubmitting for resume dispatch",
                getattr(ctx, "op_id", "?"), token,
            )
        except asyncio.CancelledError:
            # Cooperative cancellation (pool.stop or explicit cancel).
            # Mark the park cancelled BEFORE re-raising so any awaiter
            # on the store unblocks; the resumed dispatch will surface
            # as CancelledError on the RESUME path.
            await store.cancel(token, reason="continuation_cancelled")
            logger.info(
                "park continuation cancelled: ctx_op_id=%s token=%s "
                "(propagating CancelledError)",
                getattr(ctx, "op_id", "?"), token,
            )
            raise
        except Exception as exc:  # noqa: BLE001 — propagate via store
            await store.cancel(
                token,
                reason=f"continuation_failed:{type(exc).__name__}:{exc}",
            )
            logger.warning(
                "park continuation failed: ctx_op_id=%s token=%s "
                "exc=%s — resumed dispatch will surface as cancellation",
                getattr(ctx, "op_id", "?"), token, exc,
            )
        # Resubmit for resume — done on success AND on failure path
        # (the resumed dispatch sees the cancel/complete status and
        # routes accordingly).  Wrapped in its own try so a submit
        # failure (queue full, pool stopping) doesn't leak.
        try:
            await pool.submit_for_resume(ctx, attempt_seq=attempt_seq)
        except Exception:  # noqa: BLE001
            logger.error(
                "park continuation: submit_for_resume failed for "
                "ctx_op_id=%s token=%s — parked op cannot resume; "
                "store will reap on TTL",
                getattr(ctx, "op_id", "?"), token, exc_info=True,
            )

    task = asyncio.create_task(_continuation())
    pool.register_park_continuation(task)
