"""How long to wait for an op — measured, not multiplied.

The Sentinel's outcome wait was ``pipeline_budget * 1.2``. A fixed multiplier
is wrong in both directions and there is no value that is right: too small and
it sheds ops that were about to land, too large and a wedged op holds the loop
for the rest of the session. The multiplier does not know what the machine is
doing, and that is the only thing that determines how long an op takes.

This derives the deadline from what has actually been observed:

* **history** — an EWMA of completed op durations, per route. What ops on this
  lane really cost, learned from ops on this lane.
* **generation throughput** — a slow local model moves every deadline out
  together. Tracked as observed GENERATE seconds, because tokens/sec is only
  interesting as the time it produces.
* **validation latency** — composed from the estimator
  ``adaptive_gen_budget`` already feeds, so the reserve and this deadline
  cannot disagree about how long tests take.
* **queue depth** — ops in flight contend for CPU, the filesystem and the
  single local model. N concurrent ops do not cost N times one, but they do
  not cost one either, so depth scales the estimate by a bounded coefficient
  rather than multiplying it outright — the same shape
  ``compute_validation_reserve`` uses for concurrent validations.

## Composed, not reinvented

The EWMA is ``admission_estimator.WaitTimeEstimator`` — thread-safe,
memory-bounded over a closed route vocabulary, contractually never raising —
in a dedicated instance, exactly as ``get_validation_estimator`` does for
validation. A second rolling-average implementation would be a second answer
to the same question.

## Invariants

1. **Floor is the pipeline budget.** An op is never given less than the
   pipeline it runs inside; anything less would shed ops the pipeline itself
   considers live.
2. **Ceiling is the session wall.** A single op can ask for headroom and can
   never escape the session envelope.
3. **Cold start degrades to the old behaviour.** With no observations the
   estimate is the legacy multiplier, so arming this changes nothing until it
   has learned something.
4. **Monotonic in cost.** More history, slower generation or deeper queues
   never produce a *shorter* deadline.
5. **Never raises.** Every read is fail-soft; an unreadable signal contributes
   nothing rather than exploding a deadline computation.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger("Ouroboros.AdaptiveDeadline")

__all__ = [
    "DeadlineEstimate",
    "compute_outcome_deadline",
    "get_outcome_estimator",
    "observe_generation_duration",
    "observe_op_duration",
    "reset_for_tests",
]

#: The cold-start multiplier — the legacy behaviour this replaces, kept as the
#: value used when nothing has been observed yet, so arming the controller is a
#: no-op until it has evidence.
_ENV_COLD_MULTIPLIER = "JARVIS_ADAPTIVE_DEADLINE_COLD_MULTIPLIER"
#: Safety factor applied to the learned mean. An EWMA is a CENTRE; roughly half
#: of ops take longer than it, so waiting exactly the mean sheds half of them.
_ENV_SAFETY = "JARVIS_ADAPTIVE_DEADLINE_SAFETY"
#: How much each additional in-flight op adds, as a fraction. Bounded because
#: concurrent ops overlap rather than serialise.
_ENV_QUEUE_COEFF = "JARVIS_ADAPTIVE_DEADLINE_QUEUE_COEFF"
_ENV_MAX_QUEUE = "JARVIS_ADAPTIVE_DEADLINE_MAX_QUEUE_FACTOR"

_outcome_estimator: Any = None
_generation_estimator: Any = None


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    try:
        raw = (os.environ.get(name, "") or "").strip()
        value = float(raw) if raw else default
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


def _cold_multiplier() -> float:
    return _env_float(_ENV_COLD_MULTIPLIER, 1.2, minimum=1.0)


def _safety_factor() -> float:
    return _env_float(_ENV_SAFETY, 2.0, minimum=1.0)


def _queue_coeff() -> float:
    return _env_float(_ENV_QUEUE_COEFF, 0.35, minimum=0.0)


def _max_queue_factor() -> float:
    return _env_float(_ENV_MAX_QUEUE, 3.0, minimum=1.0)


def get_outcome_estimator() -> Any:
    """EWMA of COMPLETED op durations, per route. Composed, never rebuilt."""
    global _outcome_estimator  # noqa: PLW0603
    if _outcome_estimator is None:
        from backend.core.ouroboros.governance.admission_estimator import (
            WaitTimeEstimator,
        )
        _outcome_estimator = WaitTimeEstimator()
    return _outcome_estimator


def _get_generation_estimator() -> Any:
    global _generation_estimator  # noqa: PLW0603
    if _generation_estimator is None:
        from backend.core.ouroboros.governance.admission_estimator import (
            WaitTimeEstimator,
        )
        _generation_estimator = WaitTimeEstimator()
    return _generation_estimator


def observe_op_duration(route: str, observed_s: float) -> None:
    """Feed a COMPLETED op's wall time back in. NEVER raises.

    Without this the controller is a constant wearing an EWMA's clothes — the
    same failure ``observe_validation_duration`` exists to prevent for the
    validation reserve.
    """
    try:
        if observed_s and observed_s > 0:
            get_outcome_estimator().update_observed(str(route or "default"),
                                                    float(observed_s))
    except Exception:  # noqa: BLE001
        logger.debug("[AdaptiveDeadline] op observation dropped", exc_info=True)


def observe_generation_duration(route: str, observed_s: float) -> None:
    """Feed an observed GENERATE wall time in — the throughput signal.

    Tokens/sec is only interesting as the time it produces, and generation
    seconds are already measured everywhere (``generation_duration_s``), so
    this needs no new instrumentation in the model path.
    """
    try:
        if observed_s and observed_s > 0:
            _get_generation_estimator().update_observed(str(route or "default"),
                                                        float(observed_s))
    except Exception:  # noqa: BLE001
        logger.debug("[AdaptiveDeadline] gen observation dropped", exc_info=True)


@dataclass(frozen=True)
class DeadlineEstimate:
    """The deadline, and every input that produced it — so a shed op can be
    explained without re-deriving the arithmetic from logs."""

    seconds: float
    basis: str
    floor_s: float
    ceiling_s: float
    observed_op_s: float = 0.0
    observed_gen_s: float = 0.0
    validation_s: float = 0.0
    queue_depth: int = 1
    queue_factor: float = 1.0

    def render(self) -> str:
        return (
            f"deadline={self.seconds:.0f}s basis={self.basis} "
            f"op_ewma={self.observed_op_s:.0f}s gen={self.observed_gen_s:.0f}s "
            f"validate={self.validation_s:.0f}s queue={self.queue_depth}"
            f"(x{self.queue_factor:.2f}) "
            f"bounds=[{self.floor_s:.0f},{self.ceiling_s:.0f}]"
        )


def _project(estimator: Any, route: str) -> float:
    try:
        return max(0.0, float(estimator.project_wait(str(route or "default")) or 0.0))
    except Exception:  # noqa: BLE001
        return 0.0


def compute_outcome_deadline(
    *,
    pipeline_budget_s: float,
    wall_ceiling_s: float,
    route: str = "default",
    queue_depth: int = 1,
) -> DeadlineEstimate:
    """How long to wait for an op to reach a terminal state. NEVER raises.

    Bounded by the pipeline budget below and the session wall above, so the
    controller can only choose *within* the envelope the operator declared —
    it can never invent headroom the session does not have.
    """
    floor = max(1.0, float(pipeline_budget_s or 0.0))
    ceiling = max(floor, float(wall_ceiling_s or 0.0) or floor * _cold_multiplier())

    try:
        observed_op = _project(get_outcome_estimator(), route)
        observed_gen = _project(_get_generation_estimator(), route)
        validation_s = 0.0
        try:
            from backend.core.ouroboros.governance.adaptive_gen_budget import (
                get_validation_estimator,
            )
            validation_s = _project(get_validation_estimator(), route)
        except Exception:  # noqa: BLE001
            validation_s = 0.0

        depth = max(1, int(queue_depth or 1))
        queue_factor = min(
            _max_queue_factor(), 1.0 + _queue_coeff() * (depth - 1),
        )

        if observed_op > 0:
            # Best evidence: what ops on this lane actually cost.
            estimate = observed_op * _safety_factor()
            basis = "observed_op_ewma"
        elif observed_gen > 0 or validation_s > 0:
            # No completed op yet, but the two dominant phases are measured.
            # Their sum is a floor on the op, so scale it the same way.
            estimate = (observed_gen + validation_s) * _safety_factor()
            basis = "phase_sum"
        else:
            estimate = floor * _cold_multiplier()
            basis = "cold_start"

        estimate *= queue_factor
        seconds = max(floor, min(ceiling, estimate))
        return DeadlineEstimate(
            seconds=seconds, basis=basis, floor_s=floor, ceiling_s=ceiling,
            observed_op_s=observed_op, observed_gen_s=observed_gen,
            validation_s=validation_s, queue_depth=depth,
            queue_factor=queue_factor,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[AdaptiveDeadline] degraded: %r", exc)
        return DeadlineEstimate(
            seconds=max(floor, min(ceiling, floor * _cold_multiplier())),
            basis="degraded", floor_s=floor, ceiling_s=ceiling,
        )


def reset_for_tests() -> None:
    """Drop the process-wide estimators."""
    global _outcome_estimator, _generation_estimator  # noqa: PLW0603
    _outcome_estimator = None
    _generation_estimator = None
