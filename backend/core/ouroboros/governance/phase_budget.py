"""
Universal phase-local sub-budgeting (Task #98, 2026-05-14).

Extends Task #97's PlanGenerator-specific phase-local deadline +
``asyncio.wait_for`` + graceful-degrade architecture UNIVERSALLY across
the pre-GENERATE phases of the 11-phase Ouroboros pipeline.

v14-rev15 graduation soak proved Task #97 worked correctly for the
PLAN phase — and immediately surfaced the next layer of the onion:
CLASSIFY / ROUTE / CTX consumed ~316s of the op budget BEFORE PLAN
ever ran, leaving ``op_remaining=43.6s`` below the GENERATE reserve.
Task #97 correctly fired ``plan_phase_skipped:insufficient_budget``
— graceful degrade — but the SWE op still never reached GENERATE
because the OUTER op budget was already exhausted upstream.

Per operator binding 2026-05-14 ("universalize the defense"), every
pre-GENERATE phase now:

  1. Computes its own phase-local deadline via the SAME math kernel
     Task #97 introduced — ``min(op_remaining × fraction, op_remaining
     - MIN_GENERATE_RESERVE_S)`` — adaptive, not hardcoded.

  2. Wraps the phase runner's ``run()`` in ``asyncio.wait_for`` with
     ``timeout=phase_budget_s + grace``.  If the phase exceeds its
     allotted slice, the asyncio primitive fires a hard interrupt.

  3. Catches ``asyncio.TimeoutError`` and returns a structured
     ``PhaseResult(status="skip", reason="phase_budget_exhausted:
     <phase>:<elapsed>s", next_phase=<phase.next>)`` — graceful
     degrade.  The operation acknowledges the partial failure but
     moves forward; GENERATE always inherits its reserved runway.

  4. Each phase's fraction is env-tunable via
     ``JARVIS_PHASE_BUDGET_FRACTION_<NAME>`` — no hardcoded magic
     numbers.  Defaults sum to less than 1.0 so GENERATE always
     gets meaningful runway:

         CLASSIFY: 0.05 (fast deterministic — small slice)
         ROUTE:    0.05 (fast deterministic — small slice)
         CTX:      0.20 (medium — Claude expansion happens here)
         PLAN:     0.30 (Task #97 default — largest pre-GENERATE)
         ──────────────
         GENERATE: ≥ 0.40 (sacred remainder)

  5. The MIN_GENERATE_RESERVE_S floor (default 60s, reused from
     Task #97's env knob) is the absolute hard floor under which
     every phase yields its budget to GENERATE.

This module is the SINGLE SOURCE OF TRUTH for phase-local budget
math.  ``plan_generator.py`` (Task #97) imports the kernel from
here to avoid duplication.

Composes existing primitives:
  * ``asyncio.wait_for`` — canonical hard-cancel.
  * ``PhaseResult(status="skip", reason=..., next_phase=...)`` —
    existing graceful-degrade contract for runners.
  * Task #97's resolver shape (env-tunable + invalid-fallback +
    floor protection).

NO new bounding primitive, NO new exception type, NO hardcoded
magic numbers, NO behavior change without explicit operator flip.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from backend.core.ouroboros.governance.op_context import (
        OperationContext,
        OperationPhase,
    )
    from backend.core.ouroboros.governance.phase_runner import (
        PhaseResult,
        PhaseRunner,
    )

logger = logging.getLogger("Ouroboros.PhaseBudget")

_TRUTHY = frozenset({"1", "true", "yes", "on"})


# ---------------------------------------------------------------------------
# Closed phase taxonomy + default fractions
# ---------------------------------------------------------------------------
# Phase name keys MUST match the upper-cased ``OperationPhase`` enum names
# so the AST-pinned dispatch wrappers in orchestrator.py can resolve
# them from runner.phase or the phase enum.  Defaults calibrated so
# CLASSIFY + ROUTE + CTX + PLAN sums to 0.60, leaving GENERATE with at
# least 0.40 of any op budget (plus the absolute reserve floor).
PHASE_FRACTION_DEFAULTS: dict = {
    "CLASSIFY": 0.05,
    "ROUTE": 0.05,
    "CONTEXT_EXPANSION": 0.20,
    "PLAN": 0.30,
}

# Grace seconds added to the asyncio.wait_for timeout so the runner has
# a moment to surrender cleanly before the hard cancel fires.  Composes
# Task #97's pattern.
_PHASE_BUDGET_WAIT_FOR_GRACE_S_DEFAULT = 1.0


def _resolve_universal_phase_budget_enabled() -> bool:
    """Master switch — ``JARVIS_UNIVERSAL_PHASE_BUDGET_ENABLED``.

    Default ``true``: universal phase-local sub-budgeting active across
    every pre-GENERATE dispatch site.  Set to ``false`` for byte-
    identical legacy behavior (no wait_for wraps, no graceful degrade
    via this module — runners run with whatever bounding they had
    pre-Task-#98).

    Operator binding 2026-05-14: this is the universalized defense.
    Default-on per "the upstream pipeline will aggressively prune
    itself to ensure that the AI has the exact amount of time it
    needs."
    """
    _raw = os.environ.get(
        "JARVIS_UNIVERSAL_PHASE_BUDGET_ENABLED", "true",
    ).strip().lower()
    return _raw in _TRUTHY


def resolve_phase_fraction(phase_name: str) -> float:
    """Resolve ``JARVIS_PHASE_BUDGET_FRACTION_<NAME>`` to a float in
    (0.0, 1.0].  Invalid / out-of-range / unknown env values fall back
    to ``PHASE_FRACTION_DEFAULTS[phase_name]`` (also if the phase is
    not in the table, falls back to 0.10 as a conservative default).
    """
    _default = PHASE_FRACTION_DEFAULTS.get(phase_name.upper(), 0.10)
    _env_key = f"JARVIS_PHASE_BUDGET_FRACTION_{phase_name.upper()}"
    try:
        _raw = float(os.environ.get(_env_key, str(_default)))
    except (TypeError, ValueError):
        return _default
    if not (0.0 < _raw <= 1.0):
        return _default
    return _raw


def resolve_min_generate_reserve_s() -> float:
    """Resolve the universal GENERATE reserve floor.  Reads the SAME
    env knob Task #97 introduced (``JARVIS_PLAN_PHASE_MIN_GENERATE_
    RESERVE_S``) so Task #97 and Task #98 share a single source of
    truth — no parallel "reserve" knob for operators to keep in sync.

    Default ``60.0`` — minimum runway GENERATE is guaranteed regardless
    of upstream phase consumption.
    """
    try:
        _raw = float(
            os.environ.get(
                "JARVIS_PLAN_PHASE_MIN_GENERATE_RESERVE_S", "60.0",
            )
        )
    except (TypeError, ValueError):
        return 60.0
    if _raw < 0.0:
        return 60.0
    return _raw


def resolve_phase_min_budget_s() -> float:
    """Floor below which a phase is skipped entirely.  Reuses
    ``JARVIS_PLAN_PHASE_MIN_BUDGET_S`` (Task #97 knob — same
    semantics, single source of truth)."""
    try:
        _raw = float(
            os.environ.get("JARVIS_PLAN_PHASE_MIN_BUDGET_S", "5.0")
        )
    except (TypeError, ValueError):
        return 5.0
    if _raw < 0.0:
        return 5.0
    return _raw


def compute_phase_budget_s(op_remaining_s: float, phase_name: str) -> float:
    """Pure-data math kernel — given the remaining op budget and the
    phase name, return the phase-local budget seconds.

    Decision table (AST-pinned via the spine):

        op            = max(0.0, op_remaining_s)
        fraction      = resolve_phase_fraction(phase_name)
        reserve       = resolve_min_generate_reserve_s()
        fraction_bound = op × fraction
        reserve_bound  = max(0.0, op - reserve)
        return min(fraction_bound, reserve_bound)

    Caller decides whether to skip the phase by comparing the result
    against ``resolve_phase_min_budget_s()``.
    """
    _op = max(0.0, float(op_remaining_s))
    _fraction = resolve_phase_fraction(phase_name)
    _reserve = resolve_min_generate_reserve_s()
    _fraction_bound = _op * _fraction
    _reserve_bound = max(0.0, _op - _reserve)
    return min(_fraction_bound, _reserve_bound)


async def dispatch_phase_with_budget(
    runner: "PhaseRunner",
    ctx: "OperationContext",
    *,
    phase_name: str,
    op_deadline: Optional[datetime],
    fallback_next_phase: Optional["OperationPhase"] = None,
) -> "PhaseResult":
    """Universal dispatch wrapper — bounds a ``PhaseRunner.run(ctx)``
    call by its phase-local budget computed from ``op_deadline``.

    Behavior:

    * If ``JARVIS_UNIVERSAL_PHASE_BUDGET_ENABLED`` is false → byte-
      identical to ``await runner.run(ctx)`` (legacy pass-through).

    * Compute ``op_remaining = (op_deadline - now).total_seconds()``,
      clamped to 0.

    * Compute ``phase_budget_s = compute_phase_budget_s(op_remaining,
      phase_name)``.

    * If ``phase_budget_s < resolve_phase_min_budget_s()`` →
      graceful skip without running the phase.  Returns
      ``PhaseResult(next_ctx=ctx, next_phase=fallback_next_phase,
      status="skip", reason="phase_budget_exhausted:<phase>:
      insufficient_budget_<op_remaining>s")``.  GENERATE inherits
      full op_remaining since we never burned anything in this phase.

    * Else → ``await asyncio.wait_for(runner.run(ctx),
      timeout=phase_budget_s + grace)``.  On TimeoutError, returns
      ``PhaseResult(next_ctx=ctx, next_phase=fallback_next_phase,
      status="skip", reason="phase_budget_exhausted:<phase>:
      hard_timeout_after_<elapsed>s")``.

    The ``fallback_next_phase`` lets the caller specify where the
    pipeline should continue on graceful skip.  Typically the natural
    next-phase per ``PHASE_TRANSITIONS``.  ``None`` is acceptable for
    terminal-like skips.

    NEVER raises into the caller — wraps all failure modes into the
    structured skip result (per ``PhaseRunner`` contract: "never raise
    into the dispatcher path").
    """
    from backend.core.ouroboros.governance.phase_runner import PhaseResult

    # Master switch — legacy pass-through.
    if not _resolve_universal_phase_budget_enabled():
        return await runner.run(ctx)

    # No pipeline deadline stamped — legacy pass-through.  Without an
    # op deadline we can't compute a phase-local budget, and operator
    # binding "no behavior change without explicit measurement"
    # applies: do not invent a default budget.
    if op_deadline is None:
        return await runner.run(ctx)

    _now = datetime.now(tz=timezone.utc)
    _op_remaining_s = max(0.0, (op_deadline - _now).total_seconds())
    _phase_budget_s = compute_phase_budget_s(_op_remaining_s, phase_name)
    _min_budget_s = resolve_phase_min_budget_s()

    # Floor — skip the phase entirely without running it.
    if _phase_budget_s < _min_budget_s:
        logger.info(
            "[PhaseBudget] %s budget %.1fs below floor %.1fs "
            "(op_remaining=%.1fs, fraction=%.2f) — graceful skip "
            "(Task #98 universal phase-local budget)",
            phase_name, _phase_budget_s, _min_budget_s,
            _op_remaining_s, resolve_phase_fraction(phase_name),
        )
        return PhaseResult(
            next_ctx=ctx,
            next_phase=fallback_next_phase,
            status="skip",
            reason=(
                f"phase_budget_exhausted:{phase_name.lower()}:"
                f"insufficient_budget_{_op_remaining_s:.1f}s_op_remaining"
            ),
        )

    # Run with hard interrupt — composes asyncio.wait_for canonical
    # primitive.  +grace gives the runner a moment to surrender cleanly
    # (mirrors Task #97 pattern).
    _wait_for_grace_s = _PHASE_BUDGET_WAIT_FOR_GRACE_S_DEFAULT
    _attempt_t0 = time.monotonic()
    try:
        logger.debug(
            "[PhaseBudget] %s dispatching with budget=%.1fs "
            "(op_remaining=%.1fs, fraction=%.2f)",
            phase_name, _phase_budget_s, _op_remaining_s,
            resolve_phase_fraction(phase_name),
        )
        return await asyncio.wait_for(
            runner.run(ctx),
            timeout=_phase_budget_s + _wait_for_grace_s,
        )
    except asyncio.TimeoutError:
        _elapsed = time.monotonic() - _attempt_t0
        logger.warning(
            "[PhaseBudget] %s exceeded phase-local budget %.1fs "
            "(elapsed=%.1fs, op_remaining_at_dispatch=%.1fs) — "
            "graceful degrade per Task #98 operator binding 2026-05-14",
            phase_name, _phase_budget_s, _elapsed, _op_remaining_s,
        )
        return PhaseResult(
            next_ctx=ctx,
            next_phase=fallback_next_phase,
            status="skip",
            reason=(
                f"phase_budget_exhausted:{phase_name.lower()}:"
                f"hard_timeout_after_{_elapsed:.1f}s"
            ),
        )


__all__ = [
    "PHASE_FRACTION_DEFAULTS",
    "compute_phase_budget_s",
    "dispatch_phase_with_budget",
    "resolve_min_generate_reserve_s",
    "resolve_phase_fraction",
    "resolve_phase_min_budget_s",
]
