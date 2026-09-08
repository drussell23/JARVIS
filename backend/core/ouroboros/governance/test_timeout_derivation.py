"""How long pytest may run — derived from the op's envelope, not a literal.

## The defect this closes

``PythonAdapter.run`` is handed ``timeout_budget_s`` — the seconds
``LanguageRouter`` has computed are genuinely left for this adapter — and then
does::

    TestRunner(repo_root=..., timeout=min(timeout_budget_s, self._timeout))

``self._timeout`` defaults to a literal ``120.0`` and **every construction site
in production omits it**. So the live budget is admitted and immediately thrown
away: ``min(anything_bigger, 120)`` is 120, forever.

Measured, session ``bt-2026-09-08-193049``, op ``op-01a08280-f4f2``: an op with
a **1530 s** ceiling spent its whole life on three pytest invocations killed at
**120 s**, ``120 s`` and ``109 s``. The retry ladder burned 2 → 1 → 0 → −1,
``micro_fix`` returned ``fixed=False`` both times because there was nothing to
fix — the candidate never received a verdict, it received a stopwatch — and the
op ended ``failed / l2_stopped`` after 1447 s. The generated code was never
judged. This is the same defect shape as the flat 10 s per-test cap closed at
``e13c1097b1``, one layer up: a static number sitting below the suite it must
run, with the correct dynamic value already in the caller's hand.

## What replaces it

Both caps for one pytest invocation, derived together so they cannot drift:

* **``invocation_s``** — the subprocess wall.
* **``per_test_s``** — ``--timeout=``, a FRACTION of the wall above. Previously
  a module constant computed at import time from the same static 120, so
  raising the wall alone would have left the per-test cap pinned at 30 s and
  moved the failure rather than fixing it.

Three inputs, all live:

1. **The budget.** ``timeout_budget_s`` is the hard ceiling. Nothing derived
   here may exceed what the phase actually has; the envelope is authority.
2. **Learned shard cost.** An EWMA of what pytest really costs on this tree,
   bucketed by shard size — because cost is neither flat nor linear in file
   count (collection and import dominate a 1-file shard; execution dominates a
   40-file one). Composed from ``admission_estimator.WaitTimeEstimator``, in a
   dedicated instance, exactly as ``adaptive_gen_budget.get_validation_estimator``
   and ``adaptive_deadline.get_outcome_estimator`` already do. A third rolling
   average would be a third answer to one question.
3. **Ladder depth.** The validate FSM runs ``1 + JARVIS_MAX_VALIDATE_RETRIES``
   invocations inside ONE op budget. A single invocation that swallows the
   whole envelope leaves nothing for the retry that exists to rescue it, so the
   share is derived from the ladder's own knob — the number the FSM itself
   reads — not from an invented fraction.

## Cold start moves UP, deliberately

Every other derivator in this codebase degrades to legacy behaviour when it has
no history. This one does not, and the difference is the whole point: the
legacy value **is the defect**. With no observations there is no evidence that
120 s is right, and there is a real number in hand — the budget. So a cold
start yields ``budget × ladder_share``, floored by the legacy value so the
result is never *worse* than today. The floor is a floor, never a ceiling.

## Invariants

1. **Never exceeds the budget.** The envelope is authority; a derived timeout
   that outlives its phase is a hang wearing a number.
2. **Never below the legacy floor** (when the budget allows it). Strictly
   monotone-improving against the behaviour it replaces.
3. **The whole ladder fits.** ``invocation_s × ladder_depth ≤ budget`` whenever
   the budget can afford the floor, so retries are reachable by construction.
4. **``per_test_s`` is always a fraction of ``invocation_s``.** The two caps
   are computed from one number and cannot disagree.
5. **Monotone in cost.** A bigger shard or a slower observed tree never yields
   a *shorter* timeout.
6. **Never raises.** Every input is fail-soft; an unreadable signal contributes
   nothing rather than exploding a validation.
"""
from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass
from typing import Any, Optional, Tuple

logger = logging.getLogger("Ouroboros.TestTimeout")

__all__ = [
    "TestTimeoutPlan",
    "derive_test_timeouts",
    "get_shard_estimator",
    "ladder_depth",
    "observe_shard_cost",
    "reset_for_tests",
    "shard_bucket",
]


# ---------------------------------------------------------------------------
# Env seams. Shared knobs are READ, never redefined — a second name for the
# ladder depth or the per-test fraction would let two components disagree
# about one number.
# ---------------------------------------------------------------------------

#: The validate FSM's own knob (``orchestrator.GovernedConfig``). Read, not
#: mirrored: the share must track the ladder that actually runs.
ENV_MAX_VALIDATE_RETRIES = "JARVIS_MAX_VALIDATE_RETRIES"
#: The legacy whole-invocation cap. Now a FLOOR, not a ceiling.
ENV_LEGACY_TIMEOUT = "JARVIS_TEST_TIMEOUT_S"
#: Shared with ``test_runner._TEST_PER_TEST_FRACTION`` — same knob, one meaning.
ENV_PER_TEST_FRACTION = "JARVIS_TEST_PER_TEST_FRACTION"
#: Multiplier on the learned cost. A projection is a central estimate; a cap
#: set AT the estimate kills half the runs that were going to pass.
ENV_SAFETY = "JARVIS_TEST_TIMEOUT_SAFETY"
#: Ceiling on the share of the budget one invocation may take, as a guard for
#: the degenerate ``ladder_depth == 1`` case (retries disabled) where the
#: ladder share alone would hand a single invocation the entire envelope and
#: leave the phase no room to record its own result.
ENV_MAX_BUDGET_FRACTION = "JARVIS_TEST_TIMEOUT_MAX_BUDGET_FRACTION"
#: Largest shard bucket tracked separately. Bounds the estimator's key space —
#: the EWMA is "memory-bounded by the size of the route vocabulary", so the
#: vocabulary must stay small.
ENV_BUCKET_CAP = "JARVIS_TEST_SHARD_BUCKET_CAP"

_DEFAULT_MAX_VALIDATE_RETRIES = 2
_DEFAULT_LEGACY_TIMEOUT_S = 120.0
_DEFAULT_PER_TEST_FRACTION = 0.25
_DEFAULT_SAFETY = 2.0
_DEFAULT_MAX_BUDGET_FRACTION = 0.9
_DEFAULT_BUCKET_CAP = 32

#: Absolute lower bound on any derived wall, in seconds. Not a policy number:
#: below this a pytest subprocess cannot reach collection at all, so a smaller
#: value could only ever produce a guaranteed infra failure.
_ABSOLUTE_FLOOR_S = 5.0


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    """Read a float env var. NEVER raises; junk falls back to *default*."""
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


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    """Read an int env var. NEVER raises; junk falls back to *default*."""
    try:
        raw = (os.environ.get(name, "") or "").strip()
        if not raw:
            return default
        val = int(float(raw))
        if val < minimum:
            return default
        return val
    except (TypeError, ValueError):
        return default


def ladder_depth() -> int:
    """How many pytest invocations one op budget must cover.

    ``1 + JARVIS_MAX_VALIDATE_RETRIES`` — the exact expression
    ``validate_runner`` loops over (``for _iter_idx in range(1 +
    orch._config.max_validate_retries)``). Reading the FSM's own knob is what
    keeps the share honest: raise the retries and every invocation's slice
    narrows automatically, with nothing else to remember to change.
    """
    return 1 + _env_int(
        ENV_MAX_VALIDATE_RETRIES, _DEFAULT_MAX_VALIDATE_RETRIES, minimum=0,
    )


def legacy_floor_s() -> float:
    """The value this module replaces, kept as a FLOOR.

    Guarantees the derivation is monotone-improving: it can only ever hand
    pytest *more* wall than the static cap did, never less.
    """
    return _env_float(
        ENV_LEGACY_TIMEOUT, _DEFAULT_LEGACY_TIMEOUT_S, minimum=_ABSOLUTE_FLOOR_S,
    )


def per_test_fraction() -> float:
    """Share of the invocation wall a SINGLE test may hold.

    The same knob ``test_runner`` reads. Bounded to (0, 1]: a per-test cap at
    or above the whole wall means pytest-timeout can never fire before the
    subprocess is killed, which is how a hung test becomes an unattributable
    infra failure instead of a named one.
    """
    frac = _env_float(
        ENV_PER_TEST_FRACTION, _DEFAULT_PER_TEST_FRACTION, minimum=0.0,
    )
    if frac <= 0.0 or frac > 1.0:
        return _DEFAULT_PER_TEST_FRACTION
    return frac


def shard_bucket(shard_size: int) -> str:
    """Estimator key for a shard of *shard_size* test files.

    Bucketed, not per-size: pytest cost is neither flat nor linear in file
    count — a 1-file shard is dominated by collection and imports, a 40-file
    shard by execution — so one global per-file rate mis-predicts both ends.
    Buckets are powers of two up to a cap, which keeps the key space at
    ``log2(cap) + 2`` entries (7 by default) and so keeps the estimator's
    memory bound intact.
    """
    try:
        n = int(shard_size)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        return "python:n0"
    cap = _env_int(ENV_BUCKET_CAP, _DEFAULT_BUCKET_CAP, minimum=1)
    if n > cap:
        return f"python:n{cap}+"
    return f"python:n{1 << (n - 1).bit_length()}"


# ---------------------------------------------------------------------------
# Learned cost — composed, not reinvented
# ---------------------------------------------------------------------------

_shard_estimator: Optional[Any] = None


def get_shard_estimator() -> Any:
    """Rolling EWMA of OBSERVED pytest wall time, per shard bucket.

    Reuses ``admission_estimator.WaitTimeEstimator``: already thread-safe,
    already memory-bounded, already contractually non-raising. A dedicated
    instance keeps shard observations out of ``get_validation_estimator``'s
    state — that one measures the whole VALIDATE phase (n candidates in
    parallel, plus the surrounding runner), this one measures a single pytest
    subprocess. Mixing them would have each answering the other's question.
    """
    global _shard_estimator  # noqa: PLW0603
    if _shard_estimator is None:
        from backend.core.ouroboros.governance.admission_estimator import (
            WaitTimeEstimator,
        )
        _shard_estimator = WaitTimeEstimator()
    return _shard_estimator


def observe_shard_cost(shard_size: int, observed_s: float) -> None:
    """Feed a COMPLETED pytest invocation's wall time back in.

    Without this the derivation is a constant wearing an EWMA's clothes.

    A TIMED-OUT run must never be observed: its duration is the cap we chose,
    not the cost of the work, and feeding it back would let the cap teach
    itself that it was right — a self-confirming ceiling. Callers pass only
    completed runs. NEVER raises.
    """
    try:
        val = float(observed_s)
        if not math.isfinite(val) or val <= 0.0:
            return
        get_shard_estimator().update_observed(shard_bucket(shard_size), val)
    except Exception:  # noqa: BLE001 — telemetry never fails the run it measures
        logger.debug("[TestTimeout] shard observation dropped", exc_info=True)


def _project_shard_cost(shard_size: int) -> Tuple[float, str]:
    """(projected seconds, basis) for a shard of *shard_size* files.

    Exact bucket first. Falling back to a global per-file rate when the bucket
    is cold lets a warm system predict an unseen shard size instead of
    collapsing to cold start, and is why buckets are a refinement of the rate
    rather than a replacement for it.
    """
    try:
        est = get_shard_estimator()
        exact = float(est.project_wait(shard_bucket(shard_size)))
        if exact > 0.0:
            return exact, "observed_bucket"
        rate = float(est.project_wait("python:per_file"))
        if rate > 0.0:
            return rate * max(1, int(shard_size or 1)), "observed_rate"
    except Exception:  # noqa: BLE001 — a cold or unreadable estimator is not an error
        logger.debug("[TestTimeout] projection degraded", exc_info=True)
    return 0.0, "cold_start"


def observe_per_file_rate(shard_size: int, observed_s: float) -> None:
    """Feed the same completed run in as a per-FILE rate.

    Kept separate from :func:`observe_shard_cost` so the two keys stay
    independently interpretable, and bounded to one extra key. NEVER raises.
    """
    try:
        n = max(1, int(shard_size or 1))
        val = float(observed_s)
        if not math.isfinite(val) or val <= 0.0:
            return
        get_shard_estimator().update_observed("python:per_file", val / n)
    except Exception:  # noqa: BLE001
        logger.debug("[TestTimeout] per-file observation dropped", exc_info=True)


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TestTimeoutPlan:
    """Both caps for one pytest invocation, plus why they are what they are.

    ``basis`` is carried so a soak log can distinguish "we had history and used
    it" from "we had none and fell back" without re-deriving anything — the
    distinction that took a live session to establish for the value this
    replaces.
    """

    invocation_s: float
    per_test_s: int
    basis: str
    shard_size: int
    budget_s: float
    ladder_depth: int
    projected_s: float

    def render(self) -> str:
        return (
            f"invocation={self.invocation_s:.0f}s per_test={self.per_test_s}s "
            f"basis={self.basis} shard={self.shard_size} "
            f"budget={self.budget_s:.0f}s ladder={self.ladder_depth} "
            f"projected={self.projected_s:.0f}s"
        )


def derive_test_timeouts(
    *,
    budget_s: float,
    shard_size: int,
    operator_ceiling_s: Optional[float] = None,
) -> TestTimeoutPlan:
    """Derive the invocation wall and per-test cap for one pytest run.

    Parameters
    ----------
    budget_s:
        Seconds genuinely available to this adapter, as computed by
        ``LanguageRouter.run`` (``timeout_budget_s`` minus elapsed). The hard
        ceiling: nothing derived here may exceed it.
    shard_size:
        Number of test files in this invocation. Drives the learned cost.
    operator_ceiling_s:
        An EXPLICIT cap from the operator. ``None`` — the production default —
        means "no static ceiling; the envelope governs". This is the parameter
        whose ``120.0`` default caused the defect; it survives only so an
        operator can still pin a cap deliberately, and a deliberate pin is a
        different thing from a forgotten default.

    NEVER raises: on any internal fault it returns the legacy floor clamped to
    the budget, which is exactly today's behaviour.
    """
    try:
        return _derive(budget_s, shard_size, operator_ceiling_s)
    except Exception:  # noqa: BLE001 — a timeout derivation must never fail a validation
        logger.debug("[TestTimeout] derivation degraded", exc_info=True)
        return _degraded_plan(budget_s, shard_size)


def _coerce_float(value: Any, default: float = 0.0) -> float:
    """Best-effort float. NEVER raises — junk becomes *default*."""
    try:
        out = float(value)
        return out if math.isfinite(out) else default
    except (TypeError, ValueError):
        return default


def _coerce_int(value: Any, default: int = 0) -> int:
    """Best-effort int. NEVER raises — junk becomes *default*."""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _degraded_plan(budget_s: Any, shard_size: Any) -> TestTimeoutPlan:
    """Today's behaviour, for a call this module could not reason about.

    The recovery path has to be at least as total as the thing it recovers
    from: an earlier version rebuilt the plan with bare ``int(shard_size)`` and
    ``float(budget_s)``, so a junk argument raised OUT of the very handler
    whose contract is that it never does. Caught by this module's own
    fail-soft test, not by a live run.
    """
    budget = _coerce_float(budget_s, 0.0)
    fallback = legacy_floor_s()
    if budget > 0.0:
        fallback = min(fallback, budget)
    wall = max(_ABSOLUTE_FLOOR_S, fallback)
    return TestTimeoutPlan(
        invocation_s=wall,
        per_test_s=max(1, int(wall * per_test_fraction())),
        basis="degraded",
        shard_size=max(0, _coerce_int(shard_size, 0)),
        budget_s=budget,
        ladder_depth=1,
        projected_s=0.0,
    )


def _derive(
    budget_s: float, shard_size: int, operator_ceiling_s: Optional[float],
) -> TestTimeoutPlan:
    budget = _coerce_float(budget_s, 0.0)
    shard_size = max(0, _coerce_int(shard_size, 0))
    if budget <= 0.0:
        # No budget is not "a small budget" — it is the router's own signal
        # that the phase is spent, and every ceiling below is expressed as a
        # fraction OF the budget, so zero disables all of them at once and the
        # learned projection escapes unclamped. Measured in this module's own
        # smoke test: a warm estimator turned budget=0 into a 1260s wall, i.e.
        # the exhausted case produced the LONGEST timeout of any input.
        # Answer the floor and let the caller's exhaustion path do its job.
        return TestTimeoutPlan(
            invocation_s=_ABSOLUTE_FLOOR_S,
            per_test_s=max(1, int(_ABSOLUTE_FLOOR_S * per_test_fraction())),
            basis="budget_exhausted",
            shard_size=shard_size,
            budget_s=0.0,
            ladder_depth=ladder_depth(),
            projected_s=0.0,
        )

    depth = ladder_depth()
    floor = legacy_floor_s()
    safety = _env_float(ENV_SAFETY, _DEFAULT_SAFETY, minimum=1.0)
    max_frac = _env_float(
        ENV_MAX_BUDGET_FRACTION, _DEFAULT_MAX_BUDGET_FRACTION, minimum=0.0,
    )
    if max_frac <= 0.0 or max_frac > 1.0:
        max_frac = _DEFAULT_MAX_BUDGET_FRACTION

    # The ladder's fair share. `depth` invocations must fit one op budget, so
    # no single one may claim more than 1/depth of it -- otherwise the retry
    # that exists to rescue a failure can never run, which is precisely the
    # ladder collapse observed in bt-2026-09-08-193049.
    ladder_share = budget / depth if depth > 0 else budget
    # The degenerate guard: with retries disabled `ladder_share` IS the whole
    # budget, and an invocation that consumes the entire envelope leaves the
    # phase no room to record what happened.
    share_cap = min(ladder_share, budget * max_frac)

    projected, basis = _project_shard_cost(shard_size)
    if projected > 0.0:
        # Warm: ask for what this shard has actually cost, plus headroom. A cap
        # set AT the central estimate kills half the runs that would pass.
        want = projected * safety
    else:
        # Cold: there is no evidence for any particular number, and the budget
        # is the one real quantity in hand. Deliberately NOT the legacy value --
        # that value is the defect.
        want = share_cap

    # Floor, then ceilings. Order matters: the floor lifts a small projection
    # up to at least today's behaviour, and the ceilings then clamp the result
    # into what actually exists. A ceiling applied first would let the floor
    # push back out past the budget.
    wall = max(want, floor)
    if share_cap > 0.0:
        # The share is a soft ceiling: it must not pull the wall below the
        # legacy floor, or this "fix" would be a regression on small budgets.
        wall = min(wall, max(share_cap, floor))
    if budget > 0.0:
        wall = min(wall, budget)
    if operator_ceiling_s is not None:
        try:
            explicit = float(operator_ceiling_s)
            if math.isfinite(explicit) and explicit > 0.0:
                wall = min(wall, explicit)
                basis = f"{basis}+operator_cap"
        except (TypeError, ValueError):
            pass
    wall = max(_ABSOLUTE_FLOOR_S, wall)

    per_test = max(1, int(wall * per_test_fraction()))

    return TestTimeoutPlan(
        invocation_s=wall,
        per_test_s=per_test,
        basis=basis,
        shard_size=shard_size,
        budget_s=budget,
        ladder_depth=depth,
        projected_s=projected,
    )


def reset_for_tests() -> None:
    """Forget every observation. Test seam only."""
    global _shard_estimator  # noqa: PLW0603
    _shard_estimator = None
