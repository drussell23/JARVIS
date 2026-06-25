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

import collections
import logging
import os
import time
from typing import Any, Dict

logger = logging.getLogger(__name__)

_ENV_ENABLED = "JARVIS_A1_TRACE_ENABLED"

# --- deep emit-hop probe (Run #17 diagnosis) ------------------------------
#
# Run #17 failed the A1 auditor on ``a1trace:missing_or_out_of_order:emit``:
# the first hop (roadmap_orchestrator emitting a strategic GOAL) was missing
# or out of order vs the ``ingest`` hop. The probe below records the exact
# emit state per goal so the verdict self-diagnoses to one of:
#   - orchestrator-off  (the original A1 gap — emit never fires)
#   - non-roadmap source (sensor/TestFailure ops ingest WITHOUT an emit)
#   - genuine reorder    (emit fired but after ingest)
#
# Observe-only + fail-soft: a probe error NEVER affects emit/ingest/the loop.
_ENV_EMIT_PROBE = "JARVIS_A1_EMIT_PROBE_ENABLED"
_ENV_ROADMAP_ENABLED = "JARVIS_ROADMAP_ORCHESTRATOR_ENABLED"

# Bounded ring of goal_id -> (emit_ts_monotonic, source); prevents unbounded
# growth on long soaks. Newest-wins eviction (FIFO) via OrderedDict.
_EMIT_LEDGER_MAX = 4096
_emit_ledger: "collections.OrderedDict[str, tuple]" = collections.OrderedDict()


def trace_enabled() -> bool:
    """Return True unless ``JARVIS_A1_TRACE_ENABLED`` is explicitly falsy."""
    val = (os.environ.get(_ENV_ENABLED, "true") or "").strip().lower()
    return val not in {"0", "false", "no", "off"}


def emit_probe_enabled() -> bool:
    """Probe defaults ON; OFF only when the master trace flag or the probe
    flag is explicitly falsy. Riding the master flag keeps OFF byte-identical.
    """
    if not trace_enabled():
        return False
    val = (os.environ.get(_ENV_EMIT_PROBE, "true") or "").strip().lower()
    return val not in {"0", "false", "no", "off"}


def _roadmap_enabled_repr() -> bool:
    """Live read of the roadmap-orchestrator master flag (no hardcoding)."""
    val = (os.environ.get(_ENV_ROADMAP_ENABLED, "") or "").strip().lower()
    return val in {"1", "true", "yes", "on"}


def reset_emit_probe() -> None:
    """Clear the per-goal emit ledger (test hook + boot reset)."""
    try:
        _emit_ledger.clear()
    except Exception:  # noqa: BLE001
        pass


def _emit_ledger_size() -> int:
    return len(_emit_ledger)


def emit_probe(goal_id: Any, *, source: str = "?") -> None:
    """Record the emit hop for *goal_id* and log a structured probe line.

    Captures the monotonic emit timestamp + source + whether the roadmap
    orchestrator is ENABLED, so a missing/out-of-order emit later self-
    explains. Observe-only (returns ``None``), fail-soft (NEVER raises).
    """
    if not emit_probe_enabled():
        return
    try:
        gid = str(goal_id)
        ts = time.monotonic()
        _emit_ledger[gid] = (ts, source)
        _emit_ledger.move_to_end(gid)
        while len(_emit_ledger) > _EMIT_LEDGER_MAX:
            _emit_ledger.popitem(last=False)
        orch = _roadmap_enabled_repr()
        logger.warning(
            "[A1Trace][emit-probe] EMIT goal=%s source=%s emit_ts=%.6f "
            "orchestrator_enabled=%s",
            gid, source, ts, orch,
        )
    except Exception:  # noqa: BLE001 — a probe must never break a hop
        pass


def probe_ingest_order(goal_id: Any) -> None:
    """At the ingest hop, emit the order-assertion line for *goal_id*.

    Compares the prior emit_ts (if any) against this ingest_ts so the
    auditor's ``missing_or_out_of_order:emit`` verdict is immediately
    traceable. If no prior emit was recorded, logs ``MISSING`` (the Run-#17
    mode: an ingest without a roadmap emit — likely a sensor/TestFailure op).

    Observe-only (returns ``None``), fail-soft (NEVER raises).
    """
    if not emit_probe_enabled():
        return
    try:
        gid = str(goal_id)
        ingest_ts = time.monotonic()
        record = _emit_ledger.get(gid)
        if record is None:
            logger.warning(
                "[A1Trace][emit-probe] MISSING goal=%s emit_ts=MISSING "
                "ingest_ts=%.6f ordered=False source=non-roadmap "
                "(ingest without prior emit -- sensor/non-roadmap source?)",
                gid, ingest_ts,
            )
            return
        emit_ts, source = record
        ordered = emit_ts <= ingest_ts
        logger.warning(
            "[A1Trace][emit-probe] goal=%s emit_ts=%.6f ingest_ts=%.6f "
            "ordered=%s source=%s",
            gid, emit_ts, ingest_ts, ordered, source,
        )
    except Exception:  # noqa: BLE001 — a probe must never break a hop
        pass


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
