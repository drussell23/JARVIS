"""AdmissionGate — Slice 1 pure-stdlib primitive.

Architectural fix for the IMMEDIATE-route saturation pathology
empirically observed on session ``bt-2026-05-02-234923``:

  Two ``EXHAUSTION cause=fallback_failed`` events fired with
  ``sem_wait_total_s=146`` and ``pre_sem_remaining_s=120`` —
  i.e., ops spent more time WAITING on the Claude-API connection-
  pool semaphore than they had budget for the call itself. Late-
  arriving IMMEDIATE ops should never have entered the queue.

The fix is **load shedding via pre-admission viability check**.
Before an op acquires ``_fallback_sem``, evaluate whether its
remaining budget can cover the projected semaphore wait + a
minimum viable Claude call. If not, refuse admission with a
distinct structural cause (``pre_admission_shed``) rather than
let the op consume a slot it can't use, then time out 26+
seconds later having starved a different op of a chance.

This is Slice 1 of a 3-slice arc:

  * Slice 1 (THIS) — pure-stdlib decision primitive. Ships the
    closed-taxonomy enums + frozen dataclasses + total
    ``compute_admission_decision()``. NO behavior change yet —
    the caller (``CandidateGenerator._call_fallback``) is wired
    in Slice 2.
  * Slice 2 — ``WaitTimeEstimator`` (rolling EWMA per route) +
    integration into ``_call_fallback`` between
    ``_pre_sem_remaining`` computation and ``async with
    self._fallback_sem:``. New exhaustion cause
    ``pre_admission_shed`` distinct from ``fallback_failed``.
  * Slice 3 — graduation: 4 AST pins (vocabulary + caller-side
    invocation regression pin + total-function pin + no-imports
    pin), 4 FlagRegistry seeds, 1 SSE event, 1 GET route at
    ``/observability/admission-gate``. Master flag default-TRUE
    post-graduation (safety infrastructure).

## Strict design constraints (per operator directives)

* **No hardcoding.** Every threshold reads from environment with
  documented floor/ceiling clamps. Slice 3 registers all flags
  in FlagRegistry via the standard discovery contract.

* **No workarounds.** This is NOT a timeout extension. The 120s
  ``_FALLBACK_MAX_TIMEOUT_S`` stays intact. We refuse ops up
  front when their budget can't cover the projected wait —
  shedding load BEFORE it consumes resources.

* **NEVER raises into callers.** Every failure mode collapses to
  a closed-enum :class:`AdmissionDecision` with a sanitized
  reason string. Garbage input collapses to
  ``AdmissionDecision.FAILED`` (caller's degradation path: treat
  as ADMIT to preserve pre-Slice-1 behavior — fail-open).

* **Pure-stdlib.** No ``backend.*`` imports anywhere. No
  ``asyncio`` (the substrate is sync; integration in Slice 2
  remains sync — admission decision happens BEFORE the
  ``async with sem`` block, where calling sync code is
  contractually safe).

* **Caller-agnostic substrate.** AST-pinned in Slice 3:
  this module imports nothing from ``candidate_generator`` /
  ``providers`` / ``orchestrator``. The dependency direction is
  one-way (caller imports us; we import nothing back).

## Authority invariants (AST-pinned in Slice 3)

* MUST NOT import: ``candidate_generator`` / ``providers`` /
  ``orchestrator`` / ``urgency_router`` / ``policy`` /
  ``iron_gate`` / ``risk_tier`` / ``change_engine`` /
  ``candidate_generator`` / ``gate`` / ``yaml_writer`` /
  ``asyncio``.
* No ``exec`` / ``eval`` / ``compile``.
"""
from __future__ import annotations

import enum
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

logger = logging.getLogger(__name__)


ADMISSION_GATE_SCHEMA_VERSION: str = "admission_gate.v1"


# ---------------------------------------------------------------------------
# Env knobs — every tunable parameter reads from environment with
# documented floor/ceiling clamps. No hardcoded behavior constants.
# Slice 3 graduation registers these in FlagRegistry.
# ---------------------------------------------------------------------------


def _env_float_clamped(
    name: str, default: float, *, floor: float, ceiling: float,
) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(floor, min(ceiling, float(raw)))
    except (TypeError, ValueError):
        return default


def _env_int_clamped(
    name: str, default: int, *, floor: int, ceiling: int,
) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(floor, min(ceiling, int(raw)))
    except (TypeError, ValueError):
        return default


def admission_gate_enabled() -> bool:
    """Master switch — ``JARVIS_ADMISSION_GATE_ENABLED`` (default
    FALSE until Slice 3 graduation).

    Default-false is the conservative disposition: Slice 1 + 2
    ship the substrate but don't change behavior until operators
    explicitly opt in via env or until Slice 3 flips the
    graduated default.

    Asymmetric env semantics: empty/whitespace = unset = current
    default; explicit ``0`` / ``false`` / ``no`` / ``off``
    evaluates false; explicit truthy values evaluate true.
    Re-read on every call so flips hot-revert without restart."""
    raw = os.environ.get(
        "JARVIS_ADMISSION_GATE_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return False  # default-false until Slice 3 graduation
    return raw in ("1", "true", "yes", "on")


def min_viable_call_s() -> float:
    """``JARVIS_ADMISSION_MIN_VIABLE_CALL_S`` — minimum Claude-
    fallback call duration we need to leave AFTER the projected
    semaphore wait. Default 25.0 seconds, clamped [10.0, 60.0].

    Rationale: an IMMEDIATE-route Claude call with extended-
    thinking + Venom tool loop typically needs 25-90 seconds for
    one round. 25s is the floor where ANY useful work can land
    (single tool round, no thinking budget). Setting this lower
    risks admitting ops that will time out at the API layer
    instead of at the gate — defeating the gate's purpose."""
    return _env_float_clamped(
        "JARVIS_ADMISSION_MIN_VIABLE_CALL_S",
        25.0, floor=10.0, ceiling=60.0,
    )


def budget_safety_factor() -> float:
    """``JARVIS_ADMISSION_BUDGET_SAFETY_FACTOR`` — multiplier on
    the projected semaphore wait time when checking budget
    viability. Default 1.2, clamped [1.0, 3.0].

    Rationale: the projected wait is an estimate (rolling EWMA
    from Slice 2). The safety factor accounts for variance — if
    the EWMA says we'll wait 60s on average, with 1.2x we require
    72s of headroom for the wait alone, then the
    ``min_viable_call_s`` floor on top. Higher safety factor
    means MORE shedding (more conservative); lower means LESS
    shedding (more aggressive admission)."""
    return _env_float_clamped(
        "JARVIS_ADMISSION_BUDGET_SAFETY_FACTOR",
        1.2, floor=1.0, ceiling=3.0,
    )


def queue_depth_hard_cap() -> int:
    """``JARVIS_ADMISSION_QUEUE_DEPTH_HARD_CAP`` — absolute
    upper bound on the semaphore queue depth. Default 16, clamped
    [1, 128].

    Rationale: even when budgets look ok, a runaway queue is
    dangerous (memory, head-of-line blocking, observability
    distortion). The hard cap is the second-line defense: any op
    arriving when the queue depth is at or above this value is
    SHED_QUEUE_DEEP regardless of budget math. The Slice 2
    integration will pass the live ``_fallback_sem`` queue depth
    to this gate."""
    return _env_int_clamped(
        "JARVIS_ADMISSION_QUEUE_DEPTH_HARD_CAP",
        16, floor=1, ceiling=128,
    )


# ---------------------------------------------------------------------------
# Closed 5-value taxonomy of AdmissionDecision (J.A.R.M.A.T.R.I.X.)
# ---------------------------------------------------------------------------


class AdmissionDecision(str, enum.Enum):
    """Outcome of one admission-gate evaluation. Closed taxonomy —
    every :func:`compute_admission_decision` invocation returns
    exactly one. Slice 3 AST-pins the literal vocabulary against
    silent expansion.

    ``ADMIT``                      — op cleared the gate; caller
                                     proceeds to ``async with
                                     self._fallback_sem:``
                                     normally.
    ``SHED_BUDGET_INSUFFICIENT``   — projected wait + min viable
                                     call exceeds remaining
                                     budget. Caller raises
                                     EXHAUSTION with cause
                                     ``pre_admission_shed`` —
                                     distinct from
                                     ``fallback_failed`` so
                                     observability can tell the
                                     difference between
                                     "tried and timed out" and
                                     "structurally rejected
                                     before trying."
    ``SHED_QUEUE_DEEP``            — semaphore queue depth at or
                                     above the hard cap. Caller
                                     raises EXHAUSTION with the
                                     same ``pre_admission_shed``
                                     cause but with a distinct
                                     reason tag.
    ``DISABLED``                   — master flag off. Caller
                                     treats as ADMIT (preserves
                                     pre-Slice-1 behavior). The
                                     DISABLED outcome is
                                     informational for telemetry.
    ``FAILED``                     — defensive sentinel: garbage
                                     input (None ctx, NaN values,
                                     etc.). Caller treats as
                                     ADMIT (fail-open — never
                                     let a bug in the gate
                                     ITSELF starve a legitimate
                                     op). The FAILED outcome is
                                     surfaced via observability so
                                     operators can see the gate
                                     had a problem.
    """

    ADMIT = "admit"
    SHED_BUDGET_INSUFFICIENT = "shed_budget_insufficient"
    SHED_QUEUE_DEEP = "shed_queue_deep"
    DISABLED = "disabled"
    FAILED = "failed"


# Outcomes the caller treats as "PROCEED with sem.acquire" — i.e.,
# DON'T raise EXHAUSTION at the call site. Pinned as a frozenset
# so the wiring code (Slice 2) can do an explicit membership check
# and so Slice 3's AST validator can pin the literal value set
# against silent expansion. Fail-open discipline: DISABLED + FAILED
# both proceed to admission to preserve pre-Slice-1 behavior.
_PROCEED_OUTCOMES: frozenset = frozenset({
    AdmissionDecision.ADMIT,
    AdmissionDecision.DISABLED,
    AdmissionDecision.FAILED,
})


# Outcomes that are ACTIVE shedding decisions — caller raises
# EXHAUSTION with cause=pre_admission_shed. Inverse of
# _PROCEED_OUTCOMES. Used by Slice 2's wire-up + Slice 3's
# AST validator.
_SHED_OUTCOMES: frozenset = frozenset({
    AdmissionDecision.SHED_BUDGET_INSUFFICIENT,
    AdmissionDecision.SHED_QUEUE_DEEP,
})


# ---------------------------------------------------------------------------
# Frozen input + output dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdmissionContext:
    """Read-only snapshot the caller passes to the gate. Frozen
    so a misbehaving gate evaluation can't mutate shared state.

    ``route`` is a string (e.g., ``"immediate"`` /
    ``"standard"`` / ``"complex"``) — the substrate stays
    free of ``ProviderRoute`` enum coupling so the gate works
    with any callable that supplies a route string. Slice 2's
    integration passes the lower-cased ProviderRoute value.

    ``remaining_s`` is the wall-clock budget the caller has
    LEFT before its outer deadline fires (typically
    ``self._remaining_seconds(deadline)`` from
    ``CandidateGenerator``).

    ``queue_depth`` is the LIVE depth of the semaphore queue
    AT EVALUATION TIME (``_fallback_concurrency -
    _fallback_sem._value`` in CandidateGenerator's accessors;
    the difference is "ops currently waiting OR holding").

    ``projected_wait_s`` is the caller's best estimate of how
    long the semaphore acquisition will take. Slice 1 accepts
    this as an opaque input; Slice 2's WaitTimeEstimator
    computes it from rolling per-route EWMA of observed
    ``sem_wait_total_s`` values."""

    route: str
    remaining_s: float
    queue_depth: int
    projected_wait_s: float
    op_id: str = ""
    schema_version: str = ADMISSION_GATE_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "route": self.route,
            "remaining_s": self.remaining_s,
            "queue_depth": self.queue_depth,
            "projected_wait_s": self.projected_wait_s,
            "op_id": self.op_id,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class AdmissionRecord:
    """Result of one admission-gate evaluation. Frozen for safe
    propagation across observability surfaces (Slice 3 SSE +
    GET route + ring buffer).

    ``required_budget_s`` is the budget threshold the gate
    computed: ``projected_wait_s × budget_safety_factor +
    min_viable_call_s``. Surfaced for operator observability so
    the math is auditable.

    ``decided_at_ts`` is stamped by the caller (typically
    ``time.time()`` at call site) — the substrate doesn't read
    the clock so the function stays bit-deterministic for
    testing."""

    decision: AdmissionDecision
    reason: str
    route: str
    remaining_s: float
    queue_depth: int
    projected_wait_s: float
    required_budget_s: float
    op_id: str = ""
    decided_at_ts: float = 0.0
    schema_version: str = ADMISSION_GATE_SCHEMA_VERSION

    def proceeds(self) -> bool:
        """True iff the caller should PROCEED with sem.acquire.
        Equivalent to ``decision in _PROCEED_OUTCOMES``. Exposed
        as a method so call sites read declaratively and the
        ``_PROCEED_OUTCOMES`` invariant has a single source of
        truth (Slice 3 AST-pins this method's body)."""
        return self.decision in _PROCEED_OUTCOMES

    def is_shed(self) -> bool:
        """True iff the caller should raise EXHAUSTION with
        cause=pre_admission_shed. Inverse of :meth:`proceeds`
        for the two SHED_* cases (NOT for DISABLED/FAILED)."""
        return self.decision in _SHED_OUTCOMES

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision": self.decision.value,
            "reason": self.reason,
            "route": self.route,
            "remaining_s": self.remaining_s,
            "queue_depth": self.queue_depth,
            "projected_wait_s": self.projected_wait_s,
            "required_budget_s": self.required_budget_s,
            "op_id": self.op_id,
            "decided_at_ts": self.decided_at_ts,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(
        cls, raw: Mapping[str, Any],
    ) -> Optional["AdmissionRecord"]:
        """Schema-tolerant reconstruction. Returns ``None`` on
        schema mismatch / malformed shape. NEVER raises."""
        try:
            if not isinstance(raw, Mapping):
                return None
            if (
                raw.get("schema_version")
                != ADMISSION_GATE_SCHEMA_VERSION
            ):
                return None
            return cls(
                decision=AdmissionDecision(str(raw["decision"])),
                reason=str(raw.get("reason", ""))[:200],
                route=str(raw.get("route", "")),
                remaining_s=float(raw.get("remaining_s", 0.0)),
                queue_depth=int(raw.get("queue_depth", 0)),
                projected_wait_s=float(
                    raw.get("projected_wait_s", 0.0),
                ),
                required_budget_s=float(
                    raw.get("required_budget_s", 0.0),
                ),
                op_id=str(raw.get("op_id", "")),
                decided_at_ts=float(
                    raw.get("decided_at_ts", 0.0),
                ),
            )
        except (KeyError, ValueError, TypeError):
            return None


# ---------------------------------------------------------------------------
# Total decision function
# ---------------------------------------------------------------------------


def compute_admission_decision(
    ctx: Optional[AdmissionContext],
    *,
    enabled: bool,
    min_viable_call_s_value: Optional[float] = None,
    budget_safety_factor_value: Optional[float] = None,
    queue_depth_hard_cap_value: Optional[int] = None,
    decided_at_ts: float = 0.0,
) -> AdmissionRecord:
    """Pure decision function over ``(ctx, env_knobs)``. NEVER
    raises into the caller — every failure mode collapses to a
    closed-enum :class:`AdmissionDecision`.

    Decision tree (top-down, first match wins; later checks assume
    earlier checks didn't trigger):

      1. ``enabled`` is False                     → DISABLED
         (caller treats as ADMIT — preserves
         pre-Slice-1 behavior)
      2. ``ctx`` is None / shape-broken          → FAILED
         (defensive; caller treats as ADMIT —
         fail-open so the gate cannot itself be
         the cause of a starved op)
      3. ``ctx.queue_depth >= queue_depth_hard_cap`` → SHED_QUEUE_DEEP
         (second-line defense: even when budget math
         says ok, a runaway queue is unsafe)
      4. ``ctx.remaining_s <
         (ctx.projected_wait_s × safety_factor +
          min_viable_call_s)``                    → SHED_BUDGET_INSUFFICIENT
         (the structural fix for the
         bt-2026-05-02-234923 reproduction —
         remaining budget can't cover the wait
         plus a min-viable call, so refuse
         admission BEFORE entering the wait)
      5. Otherwise                                → ADMIT

    Inputs other than ``ctx`` and ``enabled`` default to
    ``None``, which means "read from env knob" — the env
    knobs are the canonical source of truth, so production
    callers pass ``enabled=admission_gate_enabled()`` and let
    the rest default. Tests override the env knobs by passing
    explicit values.

    The ``decided_at_ts`` field is stamped on the record from
    the caller-supplied value — the substrate does NOT read the
    clock so the function stays bit-deterministic.
    """
    # Resolve env-knob overrides (None → read from env).
    if min_viable_call_s_value is None:
        try:
            min_viable_call_s_value = min_viable_call_s()
        except Exception:  # noqa: BLE001 — defensive
            min_viable_call_s_value = 25.0
    if budget_safety_factor_value is None:
        try:
            budget_safety_factor_value = budget_safety_factor()
        except Exception:  # noqa: BLE001 — defensive
            budget_safety_factor_value = 1.2
    if queue_depth_hard_cap_value is None:
        try:
            queue_depth_hard_cap_value = queue_depth_hard_cap()
        except Exception:  # noqa: BLE001 — defensive
            queue_depth_hard_cap_value = 16

    # Defensive coercion: caller-supplied overrides may be garbage.
    try:
        min_viable_call_s_value = float(min_viable_call_s_value)
        if min_viable_call_s_value <= 0:
            min_viable_call_s_value = 25.0
    except (TypeError, ValueError):
        min_viable_call_s_value = 25.0
    try:
        budget_safety_factor_value = float(
            budget_safety_factor_value,
        )
        if budget_safety_factor_value < 1.0:
            budget_safety_factor_value = 1.2
    except (TypeError, ValueError):
        budget_safety_factor_value = 1.2
    try:
        queue_depth_hard_cap_value = int(queue_depth_hard_cap_value)
        if queue_depth_hard_cap_value < 1:
            queue_depth_hard_cap_value = 16
    except (TypeError, ValueError):
        queue_depth_hard_cap_value = 16

    # Step 1: master flag off.
    if not enabled:
        return AdmissionRecord(
            decision=AdmissionDecision.DISABLED,
            reason="gate_disabled",
            route=(ctx.route if ctx is not None else ""),
            remaining_s=(
                ctx.remaining_s if ctx is not None else 0.0
            ),
            queue_depth=(
                ctx.queue_depth if ctx is not None else 0
            ),
            projected_wait_s=(
                ctx.projected_wait_s if ctx is not None else 0.0
            ),
            required_budget_s=0.0,
            op_id=(ctx.op_id if ctx is not None else ""),
            decided_at_ts=decided_at_ts,
        )

    # Step 2: shape-broken input → FAILED (caller fail-opens).
    if ctx is None:
        return AdmissionRecord(
            decision=AdmissionDecision.FAILED,
            reason="ctx_is_none",
            route="",
            remaining_s=0.0,
            queue_depth=0,
            projected_wait_s=0.0,
            required_budget_s=0.0,
            decided_at_ts=decided_at_ts,
        )

    # Defensive: coerce ctx fields. NaN / negative values surface
    # as FAILED (defensive — caller fail-opens but observability
    # sees the broken input).
    try:
        _remaining = float(ctx.remaining_s)
        _projected_wait = float(ctx.projected_wait_s)
        _depth = int(ctx.queue_depth)
        # NaN check (NaN != NaN)
        if (
            _remaining != _remaining  # NaN
            or _projected_wait != _projected_wait
            or _remaining < 0.0
            or _projected_wait < 0.0
            or _depth < 0
        ):
            return AdmissionRecord(
                decision=AdmissionDecision.FAILED,
                reason="ctx_field_invalid",
                route=str(ctx.route),
                remaining_s=0.0,
                queue_depth=0,
                projected_wait_s=0.0,
                required_budget_s=0.0,
                op_id=str(ctx.op_id),
                decided_at_ts=decided_at_ts,
            )
    except (TypeError, ValueError):
        return AdmissionRecord(
            decision=AdmissionDecision.FAILED,
            reason="ctx_field_uncoercible",
            route="",
            remaining_s=0.0,
            queue_depth=0,
            projected_wait_s=0.0,
            required_budget_s=0.0,
            decided_at_ts=decided_at_ts,
        )

    # Compute the budget threshold ONCE — surfaced via the
    # record's required_budget_s so observability can audit the
    # math.
    required_budget_s = (
        _projected_wait * budget_safety_factor_value
        + min_viable_call_s_value
    )

    # Step 3: queue depth hard cap.
    if _depth >= queue_depth_hard_cap_value:
        return AdmissionRecord(
            decision=AdmissionDecision.SHED_QUEUE_DEEP,
            reason=(
                f"queue_depth_at_hard_cap:"
                f"depth={_depth}>={queue_depth_hard_cap_value}"
            ),
            route=str(ctx.route),
            remaining_s=_remaining,
            queue_depth=_depth,
            projected_wait_s=_projected_wait,
            required_budget_s=required_budget_s,
            op_id=str(ctx.op_id),
            decided_at_ts=decided_at_ts,
        )

    # Step 4: budget viability.
    if _remaining < required_budget_s:
        return AdmissionRecord(
            decision=AdmissionDecision.SHED_BUDGET_INSUFFICIENT,
            reason=(
                f"budget_below_required:"
                f"remaining={_remaining:.2f}<"
                f"required={required_budget_s:.2f} "
                f"(wait={_projected_wait:.2f}*"
                f"{budget_safety_factor_value:.2f}+"
                f"min={min_viable_call_s_value:.2f})"
            ),
            route=str(ctx.route),
            remaining_s=_remaining,
            queue_depth=_depth,
            projected_wait_s=_projected_wait,
            required_budget_s=required_budget_s,
            op_id=str(ctx.op_id),
            decided_at_ts=decided_at_ts,
        )

    # Step 5: ADMIT.
    return AdmissionRecord(
        decision=AdmissionDecision.ADMIT,
        reason="admitted",
        route=str(ctx.route),
        remaining_s=_remaining,
        queue_depth=_depth,
        projected_wait_s=_projected_wait,
        required_budget_s=required_budget_s,
        op_id=str(ctx.op_id),
        decided_at_ts=decided_at_ts,
    )


__all__ = [
    "ADMISSION_GATE_SCHEMA_VERSION",
    "AdmissionContext",
    "AdmissionDecision",
    "AdmissionRecord",
    "admission_gate_enabled",
    "budget_safety_factor",
    "compute_admission_decision",
    "min_viable_call_s",
    "queue_depth_hard_cap",
]
