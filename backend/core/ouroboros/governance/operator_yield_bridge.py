"""Operator-Yield Bridge — spec §5.4, LR-B (Task 8).

Wires operator-presence edges into the EXISTING cooperative park/resume
machinery so the autonomous loop gracefully yields its worker to the human:

* ``operator.active``  → set a module SUSPEND FLAG.  The op's next
  park-decision point (``op_park_store.should_park_for_route(...,
  operator_suspended=True)``) then parks at the next SAFE checkpoint —
  never mid-mutation (the drain-before-park guard at the park-emit seam,
  ``generate_park_wrapper``, awaits ``mutation_critical_section.drain`` first).
  The governor hard-zero (Task 7) already blocks NEW ops, so the worker is
  freed for the operator.
* ``operator.idle``    → clear the flag + RESUME parked ops via
  ``BackgroundAgentPool.submit_for_resume`` (enumerated from the pool's
  ``status == "parked"`` ops).

Architectural reality (READ + confirmed): the existing park is COOPERATIVE —
there is NO safe way to preempt an arbitrary mid-op point.  An op parks only
when it REACHES its park-decision point and the decision returns True.  So the
only viable cross-op trigger is a shared SUSPEND FLAG that the decision point
reads.  This module owns that flag.  This is the intended design, not a
work-around (see spec §5.4).

Discipline
----------
* Gated on ``JARVIS_OPERATOR_YIELD_ENABLED`` (default false) — byte-identical
  no-op when off: ``operator_suspended()`` reports False and the event
  handlers / ``attach`` short-circuit.
* Fail-soft throughout — a missing bus/pool, a misbehaving handler, or a
  ``submit_for_resume`` failure never raises out of the public API.
* Imports only the substrate (``op_park_store`` indirectly via the decision
  function param, the presence topic constants) + stdlib.  Does NOT import the
  orchestrator or the BG pool class — the pool is duck-typed.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

from backend.core.ouroboros.governance.operator_presence import (
    EVENT_OPERATOR_ACTIVE,
    EVENT_OPERATOR_IDLE,
)

logger = logging.getLogger("Ouroboros.OperatorYieldBridge")

_ENV_MASTER = "JARVIS_OPERATOR_YIELD_ENABLED"
_TRUTHY = {"true", "1", "yes", "on"}

# Module-level suspend flag. Set True on operator.active, cleared on
# operator.idle. Read by `operator_suspended()` (which also gates on the
# master flag). Plain bool assignment is atomic under the GIL.
_suspended: bool = False


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------


def _enabled() -> bool:
    """True iff JARVIS_OPERATOR_YIELD_ENABLED is truthy. Read at call time."""
    return (os.environ.get(_ENV_MASTER, "false") or "").strip().lower() in _TRUTHY


# ---------------------------------------------------------------------------
# Flag surface
# ---------------------------------------------------------------------------


def operator_suspended() -> bool:
    """Return True iff the loop should park ops to yield to the operator.

    Gated on the master flag: when ``JARVIS_OPERATOR_YIELD_ENABLED`` is off
    this ALWAYS returns False (byte-identical to pre-Task-8), regardless of the
    raw flag value. Never raises.
    """
    try:
        return _enabled() and _suspended
    except Exception:  # noqa: BLE001 — fail-soft
        return False


def set_operator_active() -> None:
    """Set the suspend flag (raw — not gated). Fail-soft."""
    global _suspended
    try:
        _suspended = True
    except Exception:  # noqa: BLE001
        pass


def set_operator_idle() -> None:
    """Clear the suspend flag (raw — not gated). Fail-soft."""
    global _suspended
    try:
        _suspended = False
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Event handlers
# ---------------------------------------------------------------------------


async def on_operator_active(event: Any = None, *, pool: Any = None) -> None:
    """Handle an ``operator.active`` edge.

    Sets the suspend flag so the next park-decision point parks the in-flight
    op at its next SAFE checkpoint (the actual safe-park + drain happen at the
    park-emit seam). Does NOT itself touch any op — the cooperative checkpoint
    model owns the park. No-op when the yield feature is disabled. Fail-soft.
    """
    if not _enabled():
        return
    try:
        set_operator_active()
        logger.info(
            "[OperatorYield] operator.active — suspend flag SET; in-flight op "
            "will park at next safe checkpoint (worker yields to operator)"
        )
    except Exception:  # noqa: BLE001 — fail-soft
        logger.debug("[OperatorYield] on_operator_active failed (fail-soft)", exc_info=True)


async def on_operator_idle(event: Any = None, *, pool: Any = None) -> None:
    """Handle an ``operator.idle`` edge.

    Clears the suspend flag, then resumes every currently-parked op by
    re-submitting it via ``pool.submit_for_resume``. Ops already in a resume
    dispatch are skipped (no double-resume). No-op when the yield feature is
    disabled. Fail-soft — a single op's resume failure never aborts the rest.
    """
    if not _enabled():
        return
    try:
        set_operator_idle()
    except Exception:  # noqa: BLE001
        pass
    resumed = 0
    try:
        for ctx, attempt_seq in _enumerate_parked(pool):
            try:
                await pool.submit_for_resume(ctx, attempt_seq=attempt_seq)
                resumed += 1
            except Exception:  # noqa: BLE001 — one bad op never blocks the rest
                logger.warning(
                    "[OperatorYield] submit_for_resume failed for op=%s "
                    "(continuing)",
                    str(getattr(ctx, "op_id", "?")),
                    exc_info=True,
                )
    except Exception:  # noqa: BLE001 — fail-soft
        logger.debug("[OperatorYield] on_operator_idle enumerate failed (fail-soft)", exc_info=True)
    logger.info(
        "[OperatorYield] operator.idle — suspend flag CLEARED; resumed %d "
        "parked op(s)",
        resumed,
    )


def _enumerate_parked(pool: Any):
    """Yield ``(ctx, attempt_seq)`` for each parked op the pool tracks.

    Reads ``pool.list_all()`` and selects ops with ``status == "parked"`` whose
    ctx is not already in a resume dispatch (``pool.is_resumed_dispatch``).
    Never raises — a malformed pool yields nothing.
    """
    if pool is None:
        return
    try:
        ops = pool.list_all()
    except Exception:  # noqa: BLE001
        return
    for op in ops or ():
        try:
            if str(getattr(op, "status", "") or "") != "parked":
                continue
            ctx = getattr(op, "context", None)
            if ctx is None:
                continue
            ctx_op_id = str(getattr(ctx, "op_id", "") or "")
            if not ctx_op_id:
                continue
            try:
                if pool.is_resumed_dispatch(ctx_op_id):
                    continue  # already resuming — no double-dispatch
            except Exception:  # noqa: BLE001
                pass
            attempt_seq = max(1, int(getattr(op, "park_attempt_seq", 1) or 1))
            yield ctx, attempt_seq
        except Exception:  # noqa: BLE001
            continue


# ---------------------------------------------------------------------------
# Bus attach
# ---------------------------------------------------------------------------


async def attach(bus: Any = None, pool: Any = None) -> None:
    """Subscribe the two handlers to the operator-presence topics.

    No-op when the yield feature is disabled (nothing subscribed). Resolves the
    real bus singleton via ``get_event_bus_if_exists()`` when ``bus`` is None.
    Binds ``pool`` into each handler via a small closure so the handler signature
    matches the bus's ``handler(event)`` contract. Fail-soft — any subscribe
    failure is logged and swallowed (the loop still runs, just without yield).
    """
    if not _enabled():
        logger.debug("[OperatorYield] JARVIS_OPERATOR_YIELD_ENABLED=false; bridge inactive")
        return

    effective_bus = bus if bus is not None else _get_bus()
    if effective_bus is None:
        logger.debug("[OperatorYield] No bus available; bridge not attached")
        return

    resolved_pool = pool if pool is not None else _get_pool()

    async def _active_handler(event: Any = None) -> None:
        await on_operator_active(event, pool=resolved_pool)

    async def _idle_handler(event: Any = None) -> None:
        await on_operator_idle(event, pool=resolved_pool)

    try:
        await effective_bus.subscribe(EVENT_OPERATOR_ACTIVE, _active_handler)
        await effective_bus.subscribe(EVENT_OPERATOR_IDLE, _idle_handler)
        logger.info(
            "[OperatorYield] attached: subscribed to %s / %s",
            EVENT_OPERATOR_ACTIVE, EVENT_OPERATOR_IDLE,
        )
    except Exception:  # noqa: BLE001 — fail-soft
        logger.debug("[OperatorYield] attach failed (fail-soft)", exc_info=True)


# ---------------------------------------------------------------------------
# Singleton resolution helpers
# ---------------------------------------------------------------------------


def _get_bus() -> Optional[Any]:
    """Resolve the real TrinityEventBus without creating one. None on failure."""
    try:
        from backend.core.trinity_event_bus import get_event_bus_if_exists
        return get_event_bus_if_exists()
    except Exception:  # noqa: BLE001
        return None


def _get_pool() -> Optional[Any]:
    """Resolve the bound BG pool without creating one. None on failure."""
    try:
        from backend.core.ouroboros.governance._governance_state import (
            get_bound_bg_pool,
        )
        return get_bound_bg_pool()
    except Exception:  # noqa: BLE001
        return None
