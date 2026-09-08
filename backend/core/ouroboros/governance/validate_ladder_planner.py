"""Whether the VALIDATE ladder can AFFORD its next iteration.

## The defect this closes

``validate_runner`` guards each ladder iteration with::

    if remaining_s <= 0.0:
        ... terminal: validation_budget_exhausted

That asks whether any time is left, never whether there is enough to finish.
With 50 s remaining it starts a full iteration -- three candidates validating
concurrently, each running pytest -- and blows straight through the deadline.

Measured, session ``bt-2026-09-08-202025``, op ``op-01a08280-f4f5``::

    [ValidateRetryFSM] budget_exhausted_pre ... n_cands=3 remaining_s=-138.3
    DECISION outcome=failed reason_code=validation_budget_exhausted duration_s=1462.5

``remaining_s`` is **negative**: the ladder had already overrun by 138 s when it
finally noticed. Everything spent past zero was work nobody could use, and the
op ended with ``best_candidate=None`` despite three generated candidates.

The arithmetic never closed. Measured on this host (learned by
``test_timeout_derivation``'s estimator during that same run) one pytest
invocation costs **280-452 s**. Three candidates per iteration, up to three
iterations, against an op ceiling of 1530 s: the ladder as configured cannot
fit any plausible envelope, and the only question was where it would be cut off.

## What replaces it

An affordability check, before the spend rather than after it. Three outcomes:

* **FULL** -- the whole candidate set fits. Nothing changes.
* **PRUNED** -- the set does not fit but a subset does. Validate the subset.
* **STOP** -- not even one candidate fits. End the ladder cleanly, keeping
  whatever the previous iterations established, instead of starting work that
  will be truncated.

## Pruning does not reduce test coverage

Worth being exact, because "prune" can sound like "skip tests". Every candidate
in an iteration is validated against the SAME resolved test set; running two
candidates instead of three does not skip a single test. What shrinks is the
number of SIBLINGS judged -- the alternatives compared -- not the tests any one
candidate must pass. A pruned iteration applies exactly the same gate to a
smaller field. Coverage per candidate is invariant.

Siblings are kept in generation order. Absent a quality signal at plan time,
the model's own first answer is the honest thing to keep; inventing a ranking
here would be a guess wearing an algorithm's clothes.

## Cost model

* **per-candidate cost** -- the EWMA of observed pytest wall time, composed from
  ``test_timeout_derivation`` so the planner and the timeout it plans around
  cannot disagree about what a run costs.
* **concurrency** -- N candidates validate together (``asyncio.gather``) and
  contend for CPU, the filesystem and one GPU. N concurrent do not cost N times
  one, and they do not cost one either, so N scales the estimate by a bounded
  coefficient -- the same shape ``compute_validation_reserve`` already uses.
* **cold start** -- with no history the planner admits everything, so arming it
  changes nothing until it has measured something.

## Invariants

1. **Never admits more than the caller offered.**
2. **Never admits an iteration it projects cannot finish.** That is the whole
   point; the negative ``remaining_s`` above is the failure it exists to stop.
3. **STOP is a clean end, not a failure.** The caller keeps its best result so
   far; a ladder that stops early has still validated everything it ran.
4. **Cold start is FULL.** No measurement, no pruning.
5. **Monotone.** More remaining time never admits fewer candidates.
6. **Never raises.** Any fault degrades to FULL — today's behaviour.
"""
from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass
from typing import Any, Optional, Sequence, Tuple

logger = logging.getLogger("Ouroboros.ValidateLadder")

__all__ = [
    "FULL",
    "LadderPlan",
    "PRUNED",
    "STOP",
    "concurrency_coefficient",
    "plan_iteration",
    "planner_enabled",
    "projected_iteration_cost_s",
]

FULL = "full"
PRUNED = "pruned"
STOP = "stop"

#: Master switch. Default ON — it closes a measured overrun.
ENV_ENABLED = "JARVIS_VALIDATE_LADDER_PLANNER_ENABLED"
#: Contention coefficient for concurrent candidate validations. Shares the
#: knob ``compute_validation_reserve`` already uses for the same physics, so
#: the reserve and the planner cannot disagree about what concurrency costs.
ENV_CONCURRENCY_K = "JARVIS_VALIDATION_RESERVE_CONCURRENCY_K"
#: Safety multiplier on the projection. A plan made AT the central estimate
#: overruns half the time by construction.
ENV_SAFETY = "JARVIS_VALIDATE_LADDER_SAFETY"

_DEFAULT_CONCURRENCY_K = 0.5
_DEFAULT_SAFETY = 1.25


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    try:
        raw = (os.environ.get(name, "") or "").strip()
        if not raw:
            return default
        val = float(raw)
        if not math.isfinite(val) or val < minimum:
            return default
        return val
    except (TypeError, ValueError):
        return default


def planner_enabled() -> bool:
    raw = (os.environ.get(ENV_ENABLED, "") or "").strip().lower()
    return raw not in ("0", "false", "no", "off")


def concurrency_coefficient() -> float:
    """How much each ADDITIONAL concurrent validation costs, as a fraction of
    the first. Bounded to [0, 1]: zero would claim concurrency is free, and
    above one would claim it is worse than running them serially."""
    k = _env_float(ENV_CONCURRENCY_K, _DEFAULT_CONCURRENCY_K, minimum=0.0)
    return k if 0.0 <= k <= 1.0 else _DEFAULT_CONCURRENCY_K


def _per_candidate_cost_s(shard_size: int) -> Tuple[float, str]:
    """(projected seconds for ONE candidate's validation, basis).

    Composed from the shard estimator ``test_timeout_derivation`` already feeds
    from completed pytest runs — the planner must cost a run the same way the
    runner times it, or the two will disagree about the same subprocess.
    """
    try:
        from backend.core.ouroboros.governance.test_timeout_derivation import (  # noqa: PLC0415
            _project_shard_cost,
        )
        return _project_shard_cost(shard_size)
    except Exception:  # noqa: BLE001 — a cold estimator is not an error
        logger.debug("[ValidateLadder] cost projection degraded", exc_info=True)
        return 0.0, "cold_start"


def projected_iteration_cost_s(n_candidates: int, shard_size: int = 1) -> float:
    """Projected wall time for one iteration of *n_candidates*. ``0.0`` when
    cold. NEVER raises."""
    try:
        n = max(0, int(n_candidates))
        if n <= 0:
            return 0.0
        per, _basis = _per_candidate_cost_s(shard_size)
        if per <= 0.0:
            return 0.0
        k = concurrency_coefficient()
        safety = _env_float(ENV_SAFETY, _DEFAULT_SAFETY, minimum=1.0)
        return per * (1.0 + k * (n - 1)) * safety
    except Exception:  # noqa: BLE001
        logger.debug("[ValidateLadder] iteration cost degraded", exc_info=True)
        return 0.0


@dataclass(frozen=True)
class LadderPlan:
    """How many candidates this iteration may validate, and why."""

    admit: int
    mode: str
    reason: str
    offered: int
    remaining_s: float
    projected_s: float
    per_candidate_s: float
    basis: str

    @property
    def should_stop(self) -> bool:
        return self.mode == STOP

    def render(self) -> str:
        return (
            f"mode={self.mode} admit={self.admit}/{self.offered} "
            f"remaining={self.remaining_s:.0f}s projected={self.projected_s:.0f}s "
            f"per_cand={self.per_candidate_s:.0f}s basis={self.basis} "
            f"reason={self.reason}"
        )


def plan_iteration(
    *,
    remaining_s: float,
    candidates: Sequence[Any],
    shard_size: int = 1,
    iteration: int = 0,
) -> LadderPlan:
    """Decide what the next ladder iteration may attempt.

    Parameters
    ----------
    remaining_s:
        Seconds left on the op's pipeline deadline. The authority.
    candidates:
        The sibling set this iteration would validate.
    shard_size:
        Test files per candidate — drives the learned per-candidate cost.
    iteration:
        Ladder index, for telemetry only. Deliberately NOT part of the
        decision: affordability is about time and cost, and letting the index
        influence it would reintroduce a fixed shape by the back door.

    NEVER raises — any fault returns a FULL plan, which is today's behaviour.
    """
    offered = 0
    try:
        offered = len(candidates or ())
    except Exception:  # noqa: BLE001
        offered = 0

    try:
        rem = float(remaining_s)
        if not math.isfinite(rem):
            rem = 0.0
    except (TypeError, ValueError):
        rem = 0.0

    def _full(reason: str, projected: float = 0.0,
              per: float = 0.0, basis: str = "cold_start") -> LadderPlan:
        return LadderPlan(
            admit=offered, mode=FULL, reason=reason, offered=offered,
            remaining_s=rem, projected_s=projected,
            per_candidate_s=per, basis=basis,
        )

    try:
        if not planner_enabled():
            return _full("planner_disabled")
        if offered <= 0:
            return _full("no_candidates")

        # A budget already at or below zero is the caller's own terminal
        # condition; this planner does not need to invent a second one.
        if rem <= 0.0:
            return LadderPlan(
                admit=0, mode=STOP, reason="budget_already_exhausted",
                offered=offered, remaining_s=rem, projected_s=0.0,
                per_candidate_s=0.0, basis="n/a",
            )

        per, basis = _per_candidate_cost_s(shard_size)
        if per <= 0.0:
            # Cold: no measurement, no pruning. Arming this changes nothing
            # until the estimator has seen a completed run.
            return _full("cold_start_admits_all", 0.0, 0.0, basis)

        k = concurrency_coefficient()
        safety = _env_float(ENV_SAFETY, _DEFAULT_SAFETY, minimum=1.0)

        def _cost(n: int) -> float:
            return per * (1.0 + k * (n - 1)) * safety

        full_cost = _cost(offered)
        if full_cost <= rem:
            return _full("fits", full_cost, per, basis)

        # Prune to the largest sibling count that fits. Monotone in `rem` by
        # construction: `_cost` is increasing in n, so a bigger budget can only
        # ever admit at least as many.
        admit = 0
        for n in range(offered - 1, 0, -1):
            if _cost(n) <= rem:
                admit = n
                break

        if admit <= 0:
            # Not even one candidate fits. STOP is the whole fix: the previous
            # behaviour started the iteration anyway and ended at -138.3s.
            return LadderPlan(
                admit=0, mode=STOP,
                reason=(
                    f"one candidate needs {_cost(1):.0f}s and only {rem:.0f}s "
                    "remain — starting would overrun the op"
                ),
                offered=offered, remaining_s=rem, projected_s=_cost(1),
                per_candidate_s=per, basis=basis,
            )

        return LadderPlan(
            admit=admit, mode=PRUNED,
            reason=(
                f"{offered} siblings project {full_cost:.0f}s against "
                f"{rem:.0f}s remaining — validating {admit} "
                "(same tests per candidate, fewer alternatives compared)"
            ),
            offered=offered, remaining_s=rem, projected_s=_cost(admit),
            per_candidate_s=per, basis=basis,
        )
    except Exception:  # noqa: BLE001 — a planner may never fail a validation
        logger.debug("[ValidateLadder] planning degraded", exc_info=True)
        return _full("planner_degraded")


def admitted_candidates(
    candidates: Sequence[Any], plan: Optional[LadderPlan],
) -> Sequence[Any]:
    """The sibling subset *plan* admits, in generation order. NEVER raises."""
    try:
        if plan is None:
            return candidates
        if plan.admit >= len(candidates or ()):
            return candidates
        return list(candidates)[: max(0, plan.admit)]
    except Exception:  # noqa: BLE001
        return candidates
