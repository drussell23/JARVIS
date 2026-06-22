"""a1_trace — [A1Trace] breadcrumbs (A1-T4)
=============================================

The A1 milestone proof is a chain of WARNING-level ``[A1Trace]`` lines that
follow a single strategic GOAL across the five intake->FSM hops:

    emit (roadmap) -> ingest (router) -> dequeue (_dispatch_loop)
    -> submit (-> GLS) -> accept (orchestrator CLASSIFY)

WARNING level is load-bearing: ``silent_boot`` redirects INFO to
``debug.log`` and only WARNING+ reaches stdout, so a soak operator can watch
the five ordered lines appear in the terminal. That ordered chain *is* the
A1 milestone proof (the PRD's "trace file-00 enqueued->dispatched").

Design constraints
------------------
- **Fail-soft**: :func:`a1trace` NEVER raises into a hop site.
- **Gated**: ``JARVIS_A1_TRACE_ENABLED`` (default ``"true"``). When disabled
  the helper is a silent no-op (byte-identical to no instrumentation).
- **No external deps**: stdlib ``logging`` / ``os`` only.
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_ENV_ENABLED = "JARVIS_A1_TRACE_ENABLED"


def trace_enabled() -> bool:
    """Return True unless ``JARVIS_A1_TRACE_ENABLED`` is explicitly falsy."""
    val = (os.environ.get(_ENV_ENABLED, "true") or "").strip().lower()
    return val not in {"0", "false", "no", "off"}


def a1trace(hop: str, goal_id: Any, **kw: Any) -> None:
    """Emit one ``[A1Trace] <hop> goal=<id> [k=v ...]`` line at WARNING.

    *hop* is a stable label for the pipeline hop (``emit`` / ``ingest`` /
    ``dequeue`` / ``submit`` / ``accept``). *goal_id* is the stable id that
    threads the chain (the envelope ``causal_id`` / ``ctx.op_id``). Extra
    keyword pairs are appended as ``k=v`` for context (None values skipped).

    Silent no-op when tracing is disabled. NEVER raises.
    """
    if not trace_enabled():
        return
    try:
        msg = f"[A1Trace] {hop} goal={goal_id}"
        extra = " ".join(
            f"{k}={v}" for k, v in kw.items() if v is not None
        )
        if extra:
            msg = f"{msg} {extra}"
        logger.warning(msg)
    except Exception:  # noqa: BLE001 — a breadcrumb must never break a hop
        pass
