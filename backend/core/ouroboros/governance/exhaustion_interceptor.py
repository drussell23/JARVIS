"""Exhaustion interceptor (Phase 3.3 Task 2) -- last-resort local handoff.

When the provider cascade is about to raise ``all_providers_exhausted``, route
the op to the local J-Prime tier with a topologically-pruned payload instead of
crashing the loop. Gated by JARVIS_JPRIME_LASTRESORT_ENABLED (default OFF ->
re-raise, byte-identical legacy). Remote restoration is handled by the EXISTING
dw_transport_recovery + FailbackStateMachine -- no new probe loop here.
"""
from __future__ import annotations

import dataclasses
import logging
import os
import time
from typing import Any, Dict, List, Optional

from backend.core.ouroboros.governance.topological_file_pruner import (
    prune_files_by_centrality,
    local_max_context_tokens,
)

logger = logging.getLogger(__name__)

_TRUE = {"1", "true", "yes", "on"}


def lastresort_enabled() -> bool:
    """Return True iff JARVIS_JPRIME_LASTRESORT_ENABLED is set to a truthy value."""
    return os.environ.get("JARVIS_JPRIME_LASTRESORT_ENABLED", "").strip().lower() in _TRUE


def should_intercept(exc: BaseException, *, jprime: Any) -> bool:
    """Return True iff the last-resort path should fire for this exception.

    Conditions (all must hold):
    - JARVIS_JPRIME_LASTRESORT_ENABLED is truthy
    - jprime handle is not None
    - exc message contains "all_providers_exhausted"
    """
    if not lastresort_enabled() or jprime is None:
        return False
    return "all_providers_exhausted" in str(exc)


def _exhaustion_cause(exc: BaseException) -> str:
    """Extract the cause suffix from the exhaustion error message (after ':')."""
    msg = str(exc)
    if ":" in msg:
        return msg.split(":", 1)[1][:120]
    return "unknown"


async def execute_local_last_resort(
    *,
    jprime: Any,
    context: Any,
    deadline: Any,
    graph_backend: Optional[Any] = None,
    broker: Optional[Any] = None,
    file_tokens: Optional[Dict[str, int]] = None,
    ceiling_tokens: Optional[int] = None,
    original_exc: BaseException,
) -> Any:
    """Health-gate the local tier, prune the payload by topological centrality,
    generate locally, emit the handoff beacon, and return the result.

    Any failure (unhealthy local OR local generate error) re-raises
    ``original_exc`` so the caller's exhaustion contract is preserved (the
    original all_providers_exhausted error is never masked into a different
    error shape).

    Parameters
    ----------
    jprime:
        The local J-Prime provider handle. Must expose
        ``async health_probe() -> bool`` and
        ``async generate(context, deadline) -> obj with .content``.
    context:
        The operation context (dataclass with ``target_files`` and ``op_id``).
    deadline:
        Absolute deadline forwarded to jprime.generate unchanged.
    graph_backend:
        Optional graph backend for centrality scoring. Passed straight to
        ``prune_files_by_centrality``; None => fall back to token-size ranking.
    broker:
        Optional SSE broker for the handoff beacon. None => beacon silently
        suppressed (best-effort; never blocks the handoff).
    file_tokens:
        Per-file token estimates. None treated as empty dict (all files cost 0).
    ceiling_tokens:
        Token ceiling for the pruned payload. None => ``local_max_context_tokens()``.
    original_exc:
        The RuntimeError that triggered the interceptor. Re-raised on any
        local failure so the caller always sees ``all_providers_exhausted``.
    """
    # 1) health gate — only hijack if the local engine is actually available
    try:
        healthy = await jprime.health_probe()
    except Exception:  # noqa: BLE001
        healthy = False
    if not healthy:
        logger.info("[ExhaustionInterceptor] local tier unhealthy -> re-raising exhaustion")
        raise original_exc

    # 2) topological prune of the target files for the local context ceiling
    target_files: List[str] = list(getattr(context, "target_files", ()) or ())
    toks: Dict[str, int] = file_tokens if file_tokens is not None else {}
    prune = prune_files_by_centrality(
        target_files,
        file_tokens=toks,
        graph_backend=graph_backend,
        ceiling_tokens=ceiling_tokens if ceiling_tokens is not None else local_max_context_tokens(),
    )
    local_context = context
    if prune.pruned:
        try:
            local_context = dataclasses.replace(context, target_files=tuple(prune.kept_files))
        except Exception:  # noqa: BLE001 — context not a dataclass / immutable replace failed
            local_context = context

    # 3) telemetry beacon (best-effort, never blocks the handoff)
    _emit_handoff_beacon(broker, context=context, original_exc=original_exc, prune=prune)

    # 4) local generation; any failure re-raises the ORIGINAL exhaustion (not the local error)
    try:
        logger.info(
            "[ExhaustionInterceptor] HANDOFF -> local tier (cause=%s, kept=%d/%d, "
            "tokens %d->%d, discarded=%s)",
            _exhaustion_cause(original_exc),
            len(prune.kept_files),
            len(target_files),
            prune.tokens_before,
            prune.tokens_after,
            prune.discarded_files,
        )
        return await jprime.generate(local_context, deadline)
    except Exception as local_exc:  # noqa: BLE001
        logger.warning(
            "[ExhaustionInterceptor] local handoff failed (%s) -> re-raising exhaustion",
            local_exc,
        )
        raise original_exc


def _emit_handoff_beacon(
    broker: Optional[Any],
    *,
    context: Any,
    original_exc: BaseException,
    prune: Any,
) -> None:
    """Publish an exhaustion_handoff_triggered SSE event to the broker (best-effort)."""
    if broker is None:
        return
    try:
        broker.publish(
            event_type="exhaustion_handoff_triggered",
            op_id=str(getattr(context, "op_id", "") or "")[:48],
            data={
                "cause": _exhaustion_cause(original_exc),
                "tokens_before": prune.tokens_before,
                "tokens_after": prune.tokens_after,
                "discarded_files": list(prune.discarded_files),
                "kept_files": list(prune.kept_files),
                "ts": time.time(),
            },
        )
    except Exception:  # noqa: BLE001
        logger.debug("[ExhaustionInterceptor] beacon publish failed", exc_info=True)
