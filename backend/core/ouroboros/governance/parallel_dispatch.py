"""Wave 3 (6) — Parallel L3 fan-out — Slice 1 primitive.

Pure decision module for whether (and how aggressively) to fan out a
multi-file op across L3 worktrees via the existing
:mod:`~backend.core.ouroboros.governance.autonomy.subagent_scheduler`.

Slice 1 scope (2026-04-23, operator-authorized per
``memory/project_wave3_item6_scope.md``):

- Eligibility decision function + deterministic reason codes.
- Four env-flag readers (master / shadow / enforce / max_units).
- Fixed posture weight table (HARDEN 0.5× / MAINTAIN 1.0× /
  CONSOLIDATE 1.0× / EXPLORE 1.5×; emergency-brake on low
  posture confidence).
- One structured log line per decision, formatted to match Wave 1
  Slice 5 Arc B / SensorGovernor telemetry conventions.
- Default-off throughout. Zero phase-dispatcher integration yet.

§4 invariants pinned in tests:

1. MemoryPressureGate sovereignty — CRITICAL pressure forces serial.
2. Posture weighting — HARDEN 0.5× / EXPLORE 1.5× / floors at 1 unit.
3. Authority-import ban — this module imports NONE of orchestrator,
   policy, iron_gate, risk_tier, change_engine, candidate_generator,
   gate. Grep-enforced.
4. Observability — every decision emits a single ``[ParallelDispatch]``
   INFO line with deterministic reason codes.
5. Pure function — same inputs → same output. No hidden state.

This module does NOT submit to the scheduler, does NOT build the
:class:`ExecutionGraph`, and does NOT touch ``phase_dispatcher``.
Those integrations arrive in Slices 2-4 per the scope doc's §9.
"""
from __future__ import annotations

import enum
import logging
import math
import os
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

from backend.core.ouroboros.governance.memory_pressure_gate import (
    FanoutDecision as MemoryFanoutDecision,
    MemoryPressureGate,
    PressureLevel,
    get_default_gate,
)
from backend.core.ouroboros.governance.posture import Posture

logger = logging.getLogger("Ouroboros.ParallelDispatch")


# ---------------------------------------------------------------------------
# Env-flag readers — default off for master/shadow/enforce; 3 for max_units
# ---------------------------------------------------------------------------

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off"})


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if raw in _TRUTHY:
        return True
    if raw in _FALSY:
        return False
    return default


def parallel_dispatch_enabled() -> bool:
    """Master flag — ``JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED`` (default ``false``).

    When ``false`` (graduation default), :func:`is_fanout_eligible` returns
    ``allowed=False`` with ``reason_code=MASTER_OFF`` regardless of op shape
    or memory/posture state. The entire fan-out surface is dead code to
    production until the master flip graduation lands (Slice 5).
    """
    return _env_bool("JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED", False)


def parallel_dispatch_shadow_enabled() -> bool:
    """Shadow sub-flag — ``JARVIS_WAVE3_PARALLEL_DISPATCH_SHADOW`` (default ``false``).

    Shadow mode: the primitive runs + emits telemetry so operators can
    observe eligibility decisions on live ops BEFORE any graph is
    submitted to the scheduler. Slice 3 wires this into phase_dispatcher.
    Slice 1 only exposes the flag; the primitive itself does not behave
    differently under shadow (it is pure).
    """
    return _env_bool("JARVIS_WAVE3_PARALLEL_DISPATCH_SHADOW", False)


def parallel_dispatch_enforce_enabled() -> bool:
    """Enforce sub-flag — ``JARVIS_WAVE3_PARALLEL_DISPATCH_ENFORCE`` (default ``false``).

    Enforce mode: eligible ops actually submit to
    :class:`SubagentScheduler` and run in parallel. Slice 4 wires this
    into phase_dispatcher. Requires master flag to also be ``true``.
    """
    return _env_bool("JARVIS_WAVE3_PARALLEL_DISPATCH_ENFORCE", False)


def parallel_dispatch_max_units(default: int = 3) -> int:
    """Hard ceiling on fan-out degree — ``JARVIS_WAVE3_PARALLEL_MAX_UNITS``.

    Default 3 per operator §12 (b). Env-tunable for boundary tests
    (2 / 3 / 4). Falls back to the code default on any parse error or
    non-positive value; minimum returned is 1.
    """
    raw = os.environ.get("JARVIS_WAVE3_PARALLEL_MAX_UNITS", "").strip()
    if not raw:
        return default
    try:
        v = int(raw)
    except ValueError:
        return default
    if v < 1:
        return 1
    return v


# ---------------------------------------------------------------------------
# Posture weight table — fixed in code per §12 (c)
# ---------------------------------------------------------------------------

# Golden values per operator §12 (c). Tests pin these exact numbers; env
# overrides are intentionally NOT supported in Slice 1 (operator said
# "optional env overrides only if already consistent with Wave 1 posture
# policy — no ad-hoc runtime tuning without tests"). Widening this surface
# is a separate ticket if ever needed.
_POSTURE_WEIGHTS: dict = {
    Posture.HARDEN: 0.5,
    Posture.MAINTAIN: 1.0,
    Posture.CONSOLIDATE: 1.0,
    Posture.EXPLORE: 1.5,
}

# Emergency brake — force serial when posture confidence is below this
# threshold. Matches Wave 1 SensorGovernor's tier structure (0.9 high /
# 0.6 medium / below = untrusted). 0.3 chosen conservatively: posture
# readings below this level shouldn't be steering fan-out decisions at
# all.
POSTURE_CONFIDENCE_FLOOR: float = 0.3


def posture_weight_for(posture: Optional[Posture]) -> float:
    """Look up the fan-out weight for a posture.

    Returns ``1.0`` (neutral) when posture is unknown or missing, matching
    Wave 1 SensorGovernor's ``_default_posture_fn`` fallback contract.
    """
    if posture is None:
        return 1.0
    return _POSTURE_WEIGHTS.get(posture, 1.0)


# ---------------------------------------------------------------------------
# Decision record
# ---------------------------------------------------------------------------


class ReasonCode(str, enum.Enum):
    """Deterministic reason codes for :class:`FanoutEligibility` decisions.

    Each value is stable, grep-friendly, and suitable for telemetry +
    dashboards. New codes added additively; existing codes never
    repurposed.
    """
    ALLOWED = "allowed"
    MASTER_OFF = "master_off"
    EMPTY_CANDIDATE_LIST = "empty_candidate_list"
    SINGLE_FILE_OP = "single_file_op"
    POSTURE_LOW_CONFIDENCE = "posture_low_confidence"
    MEMORY_CRITICAL = "memory_critical"
    MEMORY_CLAMP = "memory_clamp"
    POSTURE_CLAMP = "posture_clamp"
    MAX_UNITS_CLAMP = "max_units_clamp"


@dataclass(frozen=True)
class FanoutEligibility:
    """Immutable eligibility decision for a multi-file op.

    Attributes
    ----------
    allowed:
        ``True`` iff the caller SHOULD fan out to ``n_allowed`` parallel
        units. ``False`` means caller falls through to the serial path
        (which may be the post-#8 dispatcher's sequential per-file walk).
    n_requested:
        The ``n_candidate_files`` value the caller passed in.
    n_allowed:
        The effective fan-out degree. ``n_allowed == 1`` means
        serial-equivalent (fan-out of 1 is meaningless overhead); in that
        case ``allowed`` is always ``False``.
    reason_code:
        Primary cause for the decision — see :class:`ReasonCode`.
    posture:
        Posture read during the decision, or ``None`` if posture store
        was unavailable.
    posture_weight:
        Multiplier applied to the base cap per :data:`_POSTURE_WEIGHTS`.
    posture_confidence:
        Confidence attached to the posture reading, in ``[0, 1]``; may
        be ``None`` if posture was unavailable.
    memory_level:
        :class:`PressureLevel` read from the memory gate during decision.
    memory_n_allowed:
        The ``n_allowed`` value returned by
        :meth:`MemoryPressureGate.can_fanout`; may be ``None`` if the
        gate was not consulted (e.g. master off, empty list).
    base_cap:
        ``min(n_requested, max_units_cap)`` — starting point before
        posture/memory reductions.
    max_units_cap:
        The ``JARVIS_WAVE3_PARALLEL_MAX_UNITS`` value at decision time.
    detail:
        Human-readable amplifier for the reason code (optional).
    """

    allowed: bool
    n_requested: int
    n_allowed: int
    reason_code: ReasonCode
    posture: Optional[Posture] = None
    posture_weight: float = 1.0
    posture_confidence: Optional[float] = None
    memory_level: Optional[PressureLevel] = None
    memory_n_allowed: Optional[int] = None
    base_cap: int = 0
    max_units_cap: int = 0
    detail: str = ""

    def log_line(self, op_id: str) -> str:
        """Single deterministic structured line suitable for logger.info.

        Format mirrors Wave 1 Slice 5 Arc B `memory_fanout_decision` and
        SensorGovernor telemetry: ``key=value`` pairs, space-separated,
        stable key ordering.
        """
        return (
            f"[ParallelDispatch] op={op_id[:16]} "
            f"allowed={str(self.allowed).lower()} "
            f"n_requested={self.n_requested} "
            f"n_allowed={self.n_allowed} "
            f"reason={self.reason_code.value} "
            f"posture={self.posture.value if self.posture else 'none'} "
            f"posture_weight={self.posture_weight:.2f} "
            f"posture_confidence="
            f"{'%.2f' % self.posture_confidence if self.posture_confidence is not None else 'none'} "
            f"memory_level={self.memory_level.value if self.memory_level else 'none'} "
            f"memory_n_allowed="
            f"{self.memory_n_allowed if self.memory_n_allowed is not None else 'none'} "
            f"base_cap={self.base_cap} "
            f"max_units_cap={self.max_units_cap}"
        )


# ---------------------------------------------------------------------------
# Posture reader — module-level default (injectable for tests)
# ---------------------------------------------------------------------------


def _default_posture_fn() -> Tuple[Optional[Posture], Optional[float]]:
    """Default posture reader — pulls current reading from PostureStore.

    Returns ``(posture, confidence)`` or ``(None, None)`` on any error.
    The fallback shape matches Wave 1 SensorGovernor's
    ``_default_posture_fn`` so downstream consumers can treat missing
    posture as neutral (weight 1.0).
    """
    try:
        from backend.core.ouroboros.governance.posture_observer import (
            get_default_store,
        )
        reading = get_default_store().load_current()
        if reading is None:
            return None, None
        return reading.posture, float(reading.confidence)
    except Exception:  # noqa: BLE001 — posture is advisory; never crash caller
        return None, None


# ---------------------------------------------------------------------------
# Public: is_fanout_eligible
# ---------------------------------------------------------------------------


def is_fanout_eligible(
    *,
    op_id: str,
    n_candidate_files: int,
    gate: Optional[MemoryPressureGate] = None,
    posture_fn: Optional[
        Callable[[], Tuple[Optional[Posture], Optional[float]]]
    ] = None,
    emit_log: bool = True,
) -> FanoutEligibility:
    """Decide whether (and how aggressively) to fan out a multi-file op.

    Pure deterministic function. Consumes env flags + injected gate +
    injected posture reader; returns an immutable :class:`FanoutEligibility`
    record. Does NOT submit to the scheduler, does NOT build an
    ExecutionGraph, does NOT touch any orchestrator / phase-dispatcher
    state.

    Parameters
    ----------
    op_id:
        Opaque identifier used only for telemetry tagging.
    n_candidate_files:
        Number of files the caller wishes to fan out across. Must be
        ``>= 0``. ``0`` → ``EMPTY_CANDIDATE_LIST``. ``1`` → ``SINGLE_FILE_OP``.
        ``>= 2`` proceeds to the full decision chain.
    gate:
        Optional :class:`MemoryPressureGate` for dependency injection in
        tests. Default is the module-level singleton.
    posture_fn:
        Optional callable returning ``(posture, confidence)``. Default
        reads the process-wide PostureStore via posture_observer.
    emit_log:
        When ``True`` (default), emits the single ``[ParallelDispatch]``
        INFO line via the module logger. Tests set ``False`` to suppress
        chatter during parametrized matrix runs.

    Returns
    -------
    FanoutEligibility
        Immutable decision record. Caller inspects ``.allowed`` (bool) +
        ``.n_allowed`` (int) to decide action. ``allowed=False`` →
        fall through to the serial path. ``allowed=True`` with
        ``n_allowed=K`` → fan out to K parallel units.

    Notes
    -----
    Evaluation order (first trip wins for short-circuits, else all
    clamps compose):

    1. Master flag off → ``MASTER_OFF`` (serial).
    2. ``n_candidate_files == 0`` → ``EMPTY_CANDIDATE_LIST`` (no-op).
    3. ``n_candidate_files == 1`` → ``SINGLE_FILE_OP`` (no fan-out benefit).
    4. Posture confidence below floor → ``POSTURE_LOW_CONFIDENCE`` (serial).
    5. Memory CRITICAL → ``MEMORY_CRITICAL`` (serial).
    6. Compose base_cap = min(n_candidate_files, max_units_env).
    7. Apply posture weight: clamped = max(1, floor(base_cap * weight)).
       If posture weight reduced the cap, note ``POSTURE_CLAMP``.
    8. Consult memory gate.can_fanout(clamped); take min with memory_n_allowed.
       If memory reduced the cap further, note ``MEMORY_CLAMP``.
    9. If final n_allowed < n_requested, hard ceiling was hit — note
       ``MAX_UNITS_CLAMP`` (when the max_units cap was the binding
       constraint).
    10. ``allowed`` = ``n_allowed >= 2`` (fan-out of 1 is serial-equivalent).
    """
    n_requested = int(n_candidate_files)
    max_units_cap = parallel_dispatch_max_units()

    # 1. Master flag gate.
    if not parallel_dispatch_enabled():
        result = FanoutEligibility(
            allowed=False,
            n_requested=n_requested,
            n_allowed=1,
            reason_code=ReasonCode.MASTER_OFF,
            max_units_cap=max_units_cap,
            detail="JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED=false",
        )
        if emit_log:
            logger.info(result.log_line(op_id))
        return result

    # 2. Empty candidate list — no op.
    if n_requested <= 0:
        result = FanoutEligibility(
            allowed=False,
            n_requested=n_requested,
            n_allowed=0,
            reason_code=ReasonCode.EMPTY_CANDIDATE_LIST,
            max_units_cap=max_units_cap,
            detail="n_candidate_files=0",
        )
        if emit_log:
            logger.info(result.log_line(op_id))
        return result

    # 3. Single-file op — fan-out of 1 is pointless overhead.
    if n_requested == 1:
        result = FanoutEligibility(
            allowed=False,
            n_requested=n_requested,
            n_allowed=1,
            reason_code=ReasonCode.SINGLE_FILE_OP,
            max_units_cap=max_units_cap,
            detail="serial is optimal for single-file ops",
        )
        if emit_log:
            logger.info(result.log_line(op_id))
        return result

    # 4. Posture confidence floor — emergency brake.
    _posture_fn = posture_fn if posture_fn is not None else _default_posture_fn
    posture, posture_confidence = _posture_fn()
    if (
        posture_confidence is not None
        and posture_confidence < POSTURE_CONFIDENCE_FLOOR
    ):
        result = FanoutEligibility(
            allowed=False,
            n_requested=n_requested,
            n_allowed=1,
            reason_code=ReasonCode.POSTURE_LOW_CONFIDENCE,
            posture=posture,
            posture_weight=posture_weight_for(posture),
            posture_confidence=posture_confidence,
            max_units_cap=max_units_cap,
            detail=(
                f"posture confidence {posture_confidence:.2f} "
                f"< floor {POSTURE_CONFIDENCE_FLOOR}"
            ),
        )
        if emit_log:
            logger.info(result.log_line(op_id))
        return result

    # 5. Consult memory gate early — CRITICAL pressure forces serial.
    _gate = gate if gate is not None else get_default_gate()
    memory_probe_decision: MemoryFanoutDecision = _gate.can_fanout(n_requested)
    if memory_probe_decision.level == PressureLevel.CRITICAL:
        result = FanoutEligibility(
            allowed=False,
            n_requested=n_requested,
            n_allowed=1,
            reason_code=ReasonCode.MEMORY_CRITICAL,
            posture=posture,
            posture_weight=posture_weight_for(posture),
            posture_confidence=posture_confidence,
            memory_level=memory_probe_decision.level,
            memory_n_allowed=memory_probe_decision.n_allowed,
            max_units_cap=max_units_cap,
            detail=(
                f"memory pressure CRITICAL "
                f"(free {memory_probe_decision.free_pct:.1f}%)"
            ),
        )
        if emit_log:
            logger.info(result.log_line(op_id))
        return result

    # 6. Compose base cap: min(n_requested, max_units_env).
    base_cap = min(n_requested, max_units_cap)

    # 7. Apply posture weight. Weight floor at 1 unit; never below serial-eq.
    weight = posture_weight_for(posture)
    posture_clamped = max(1, int(math.floor(base_cap * weight)))
    # Posture weight < 1.0 means fewer allowed. Weight > 1.0 may EXPAND
    # but we clamp back to base_cap (posture cannot exceed max_units_cap
    # or n_requested — posture is a throttle, not an amplifier beyond
    # the op's own fileset).
    posture_clamped = min(posture_clamped, base_cap)

    # 8. Consult memory gate at the posture-clamped request.
    memory_decision_at_clamp: MemoryFanoutDecision = _gate.can_fanout(
        posture_clamped
    )
    memory_n_allowed = memory_decision_at_clamp.n_allowed
    memory_level = memory_decision_at_clamp.level

    # 9. Compose final allowed degree.
    n_allowed = min(posture_clamped, memory_n_allowed)
    if n_allowed < 1:
        n_allowed = 1

    # 10. Classify reason for the final allowed value.
    reason: ReasonCode
    detail: str = ""
    if n_allowed >= 2 and n_allowed == n_requested:
        reason = ReasonCode.ALLOWED
    elif n_allowed >= 2 and n_allowed == memory_n_allowed < posture_clamped:
        reason = ReasonCode.MEMORY_CLAMP
        detail = (
            f"memory {memory_level.value} clamped to {memory_n_allowed} "
            f"(posture would allow {posture_clamped})"
        )
    elif n_allowed >= 2 and n_allowed == posture_clamped < base_cap:
        reason = ReasonCode.POSTURE_CLAMP
        detail = (
            f"posture {posture.value if posture else 'none'} × "
            f"{weight:.2f} clamped to {posture_clamped}"
        )
    elif n_allowed >= 2 and n_allowed == max_units_cap < n_requested:
        reason = ReasonCode.MAX_UNITS_CLAMP
        detail = (
            f"JARVIS_WAVE3_PARALLEL_MAX_UNITS={max_units_cap} "
            f"< n_requested={n_requested}"
        )
    elif n_allowed >= 2:
        # Generic allowed with non-specific clamp source.
        reason = ReasonCode.ALLOWED
    else:
        # n_allowed fell to 1 — fan-out would be serial-equivalent.
        # Classify by whichever constraint was PRIMARY (first-in-chain).
        # Order: posture clamped below base_cap FIRST (HARDEN on small ops
        # typically floors here), then memory if it further reduced, then
        # max_units ceiling as the residual.
        if posture_clamped < base_cap:
            reason = ReasonCode.POSTURE_CLAMP
            detail = (
                f"posture {posture.value if posture else 'none'} × "
                f"{weight:.2f} yielded {posture_clamped}"
            )
        elif memory_n_allowed < posture_clamped:
            reason = ReasonCode.MEMORY_CLAMP
            detail = f"memory {memory_level.value} allowed only {memory_n_allowed}"
        else:
            reason = ReasonCode.MAX_UNITS_CLAMP
            detail = "compose clamp to 1"

    result = FanoutEligibility(
        allowed=(n_allowed >= 2),
        n_requested=n_requested,
        n_allowed=n_allowed,
        reason_code=reason,
        posture=posture,
        posture_weight=weight,
        posture_confidence=posture_confidence,
        memory_level=memory_level,
        memory_n_allowed=memory_n_allowed,
        base_cap=base_cap,
        max_units_cap=max_units_cap,
        detail=detail,
    )
    if emit_log:
        logger.info(result.log_line(op_id))
    return result


# ---------------------------------------------------------------------------
# Module public surface — explicit for grep clarity
# ---------------------------------------------------------------------------


__all__ = [
    "FanoutEligibility",
    "POSTURE_CONFIDENCE_FLOOR",
    "ReasonCode",
    "is_fanout_eligible",
    "parallel_dispatch_enabled",
    "parallel_dispatch_enforce_enabled",
    "parallel_dispatch_max_units",
    "parallel_dispatch_shadow_enabled",
    "posture_weight_for",
]
