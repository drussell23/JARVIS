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

import asyncio
import enum
import hashlib
import logging
import math
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Sequence, Tuple

from backend.core.ouroboros.governance.autonomy.subagent_types import (
    ExecutionGraph,
    GraphExecutionPhase,
    GraphExecutionState,
    WorkUnitSpec,
)
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
# Slice 2 — candidate-file container + build_execution_graph
# ---------------------------------------------------------------------------


# Constant name for consumers + tests. Bumped when the candidate → graph
# conversion contract changes in a non-backward-compatible way.
GRAPH_SCHEMA_VERSION: str = "wave3_item6_slice2.v1"

# Planner id stamped on every graph this primitive emits. Lets downstream
# telemetry / audit distinguish parallel-dispatch-generated graphs from
# other producers (e.g. the legacy autonomy graph planner).
PLANNER_ID: str = "parallel_dispatch.v1"

# Default per-unit execution budget in seconds. Mirrors
# ``WorkUnitSpec.timeout_s`` default so Slice 2 does not silently widen
# the scheduler's existing per-unit time budget.
DEFAULT_UNIT_TIMEOUT_S: float = 180.0

# Default per-unit retry budget. Mirrors ``WorkUnitSpec.max_attempts``
# default — scheduler handles retries at the unit level; parallel
# dispatch does not add its own retry layer.
DEFAULT_UNIT_MAX_ATTEMPTS: int = 1


@dataclass(frozen=True)
class CandidateFile:
    """Slim candidate-file container consumed by :func:`build_execution_graph`.

    Mirrors the shape that the multi-file GENERATE path already emits
    (``{file_path, full_content, rationale}``, per CLAUDE.md's "Multi-file
    coordinated generation" spec) WITHOUT importing from
    :mod:`~backend.core.ouroboros.governance.candidate_generator` — that
    module is on the §4 invariant #3 authority-import ban list.

    Slice 3+ translates ``candidate_generator.Candidate.files[i]`` into
    this type at the post-GENERATE seam; Slice 2 only consumes.

    Attributes
    ----------
    file_path:
        Repository-relative POSIX path the unit will own. Must be
        non-empty, unique across the candidate list.
    full_content:
        Desired post-APPLY content. Carried through to the unit so the
        scheduler's per-unit APPLY can write it.
    rationale:
        Human-readable one-line description of why this file changes.
        Threaded into ``WorkUnitSpec.goal`` so §8 observability surfaces
        the per-unit intent.
    """

    file_path: str
    full_content: str
    rationale: str = ""

    def __post_init__(self) -> None:
        if not self.file_path or not self.file_path.strip():
            raise ValueError("CandidateFile.file_path must be non-empty")
        if self.full_content is None:
            raise ValueError(
                f"CandidateFile[{self.file_path!r}].full_content may not be None"
            )


def _unit_id_for(op_id: str, file_path: str) -> str:
    """Compute a deterministic ``unit_id`` from ``(op_id, file_path)``.

    Stable across runs so the same graph inputs yield the same graph
    (supports :attr:`ExecutionGraph.plan_digest` stability). 12-hex-char
    prefix matches the op-id-style-prefix convention already used by
    other autonomy types.
    """
    digest = hashlib.sha256(
        f"{op_id}\x1f{file_path}".encode("utf-8")
    ).hexdigest()
    return f"unit-{digest[:12]}"


def _graph_id_for(op_id: str, eligibility: "FanoutEligibility") -> str:
    """Compute a deterministic ``graph_id`` tied to the op + eligibility."""
    digest = hashlib.sha256(
        (
            f"{op_id}\x1f"
            f"{eligibility.n_requested}\x1f"
            f"{eligibility.n_allowed}\x1f"
            f"{eligibility.reason_code.value}"
        ).encode("utf-8")
    ).hexdigest()
    return f"graph-{digest[:12]}"


def build_execution_graph(
    *,
    op_id: str,
    repo: str,
    candidate_files: Sequence[CandidateFile],
    eligibility: "FanoutEligibility",
    dependency_edges: Optional[Mapping[str, Sequence[str]]] = None,
    per_unit_timeout_s: float = DEFAULT_UNIT_TIMEOUT_S,
    per_unit_max_attempts: int = DEFAULT_UNIT_MAX_ATTEMPTS,
    planner_id: str = PLANNER_ID,
    schema_version: str = GRAPH_SCHEMA_VERSION,
) -> ExecutionGraph:
    """Convert a multi-file candidate list into an :class:`ExecutionGraph`.

    Post-GENERATE seam primitive (per scope §12 (a)). Pure deterministic
    function: identical inputs yield identical ``graph_id`` + ``plan_digest``
    + ``unit_id`` values. Slice 3+ consumes this graph via the existing
    :class:`~backend.core.ouroboros.governance.autonomy.subagent_scheduler.SubagentScheduler`
    without recomputing eligibility — eligibility is already baked into
    ``concurrency_limit`` via ``eligibility.n_allowed``.

    Parameters
    ----------
    op_id:
        Parent operation id. Used for graph/unit-id derivation + op-level
        lineage in scheduler telemetry. Must be non-empty.
    repo:
        Repository tag the units target (typically ``"jarvis"`` for the
        primary repo, matching :class:`WorkUnitSpec.repo`). Must be
        non-empty.
    candidate_files:
        The multi-file GENERATE output to fan out over. Must contain at
        least two entries with unique ``file_path`` values; callers are
        responsible for having already passed :func:`is_fanout_eligible`
        with ``allowed=True``.
    eligibility:
        The ``FanoutEligibility`` record that authorized this fan-out.
        ``eligibility.allowed`` MUST be ``True`` and
        ``eligibility.n_allowed >= 2`` — otherwise the primitive raises
        :class:`ValueError`. ``eligibility.n_allowed`` becomes the graph's
        ``concurrency_limit``.
    dependency_edges:
        Optional mapping ``file_path -> [file_path, ...]`` expressing
        per-unit upstream dependencies. Each key + value must match a
        ``file_path`` present in ``candidate_files``; unknown paths
        raise ``ValueError`` before the graph is constructed. Cycles and
        duplicates are caught by the :class:`ExecutionGraph` validator
        (``_validate_unit_dag``) and surface as ``ValueError`` from its
        ``__post_init__``. When ``None``, every unit is independent
        (fully parallel-safe DAG).
    per_unit_timeout_s:
        Wall-clock budget for each unit's execution. Default matches the
        scheduler's existing :attr:`WorkUnitSpec.timeout_s`.
    per_unit_max_attempts:
        Per-unit retry budget. Default 1 (scheduler handles retries;
        parallel dispatch does not add its own retry layer).
    planner_id:
        Stamped onto the graph for telemetry lineage. Default
        ``"parallel_dispatch.v1"``.
    schema_version:
        Contract version for the candidate → graph conversion. Default
        ``"wave3_item6_slice2.v1"``. Bumped when the conversion contract
        changes incompatibly.

    Returns
    -------
    ExecutionGraph
        Validated DAG (unique unit_ids, known edges, acyclic) ready for
        :meth:`SubagentScheduler.submit`. ``concurrency_limit`` equals
        ``eligibility.n_allowed``. ``plan_digest`` + ``causal_trace_id``
        derived deterministically from inputs.

    Raises
    ------
    ValueError
        On empty candidate list, single-file op, duplicate file_paths,
        ineligible ``eligibility``, unknown dependency edges, cycles, or
        any ``WorkUnitSpec``/``ExecutionGraph`` constructor failure
        (cascaded from :class:`autonomy.subagent_types` validators).
    """
    # --- Input validation (our own contract; subagent_types validates again) ---

    if not op_id or not op_id.strip():
        raise ValueError("build_execution_graph: op_id must be non-empty")
    if not repo or not repo.strip():
        raise ValueError("build_execution_graph: repo must be non-empty")

    # Defensive None check — type annotation says non-Optional but callers
    # in Python land may still pass None accidentally; surface a clear
    # contract error before the first attribute access.
    if eligibility is None:  # type: ignore[unreachable]
        raise ValueError("build_execution_graph: eligibility must not be None")
    if not eligibility.allowed:
        raise ValueError(
            "build_execution_graph: eligibility.allowed=False "
            f"(reason={eligibility.reason_code.value}) — "
            "callers must not build a graph when fan-out is denied"
        )
    if eligibility.n_allowed < 2:
        raise ValueError(
            "build_execution_graph: eligibility.n_allowed must be >= 2; "
            f"got {eligibility.n_allowed} (reason={eligibility.reason_code.value})"
        )

    if not candidate_files:
        raise ValueError("build_execution_graph: candidate_files must be non-empty")

    files_tuple: Tuple[CandidateFile, ...] = tuple(candidate_files)
    if len(files_tuple) < 2:
        raise ValueError(
            "build_execution_graph: fan-out requires >=2 candidate files; "
            f"got {len(files_tuple)}"
        )

    seen_paths: set = set()
    for cf in files_tuple:
        if cf.file_path in seen_paths:
            raise ValueError(
                f"build_execution_graph: duplicate file_path {cf.file_path!r} "
                "in candidate_files"
            )
        seen_paths.add(cf.file_path)

    if per_unit_timeout_s <= 0.0:
        raise ValueError(
            f"build_execution_graph: per_unit_timeout_s must be > 0; "
            f"got {per_unit_timeout_s}"
        )
    if per_unit_max_attempts < 1:
        raise ValueError(
            "build_execution_graph: per_unit_max_attempts must be >= 1; "
            f"got {per_unit_max_attempts}"
        )

    # --- Resolve dependency_edges against concrete file_paths ---

    path_to_unit_id = {
        cf.file_path: _unit_id_for(op_id, cf.file_path) for cf in files_tuple
    }

    if dependency_edges:
        unknown = [
            path
            for path in dependency_edges.keys()
            if path not in path_to_unit_id
        ]
        if unknown:
            raise ValueError(
                "build_execution_graph: dependency_edges references unknown "
                f"file_paths: {sorted(unknown)}"
            )
        for dependent_path, deps in dependency_edges.items():
            for dep_path in deps:
                if dep_path not in path_to_unit_id:
                    raise ValueError(
                        f"build_execution_graph: dependency_edges[{dependent_path!r}] "
                        f"references unknown file_path {dep_path!r}"
                    )
                if dep_path == dependent_path:
                    # _validate_unit_dag handles self-loops via cycle detection,
                    # but we surface a clearer message here.
                    raise ValueError(
                        f"build_execution_graph: dependency_edges[{dependent_path!r}] "
                        "contains self-dependency"
                    )

    # --- Build WorkUnitSpec list in deterministic order ---

    units: list = []
    for cf in files_tuple:
        dep_paths = (
            tuple(dependency_edges.get(cf.file_path, ()))
            if dependency_edges
            else ()
        )
        dependency_ids = tuple(path_to_unit_id[p] for p in dep_paths)
        goal = cf.rationale or f"apply candidate to {cf.file_path}"
        units.append(
            WorkUnitSpec(
                unit_id=path_to_unit_id[cf.file_path],
                repo=repo,
                goal=goal,
                target_files=(cf.file_path,),
                dependency_ids=dependency_ids,
                owned_paths=(cf.file_path,),
                max_attempts=per_unit_max_attempts,
                timeout_s=per_unit_timeout_s,
            )
        )

    # --- Construct ExecutionGraph (cycles + duplicates caught here) ---

    graph = ExecutionGraph(
        graph_id=_graph_id_for(op_id, eligibility),
        op_id=op_id,
        planner_id=planner_id,
        schema_version=schema_version,
        units=tuple(units),
        concurrency_limit=eligibility.n_allowed,
    )
    return graph


# ---------------------------------------------------------------------------
# Slice 3 — shadow-mode evaluation helpers
# ---------------------------------------------------------------------------


def extract_candidate_files(
    generation: Any,
) -> Optional[Tuple[CandidateFile, ...]]:
    """Defensive extraction of candidate files from a GENERATE artifact.

    Shadow-mode consumer — intentionally lenient. Returns ``None`` on any
    unrecognized shape so the shadow hook emits a no-op telemetry line
    rather than crashing the pipeline.

    Accepts :class:`GenerationResult`-like shapes (anything with a
    ``.candidates`` iterable of dicts). Each candidate is inspected for:

    * ``files: [{file_path, full_content, rationale?}, ...]`` — multi-file
      shape (Slice 5 multi-file gen contract).
    * ``{file_path, full_content, ...}`` — single-file legacy shape.

    When a candidate carries a multi-file ``files`` list, returns that
    list as :class:`CandidateFile` tuples. When all candidates are
    single-file, returns a tuple with the unique files across candidates
    (which in practice is just one CandidateFile since each candidate
    describes the same change).

    Parameters
    ----------
    generation:
        The ``generation`` artifact emitted by GENERATE — typically a
        :class:`~backend.core.ouroboros.governance.op_context.GenerationResult`
        but any object with a ``.candidates`` attribute will work.

    Returns
    -------
    Optional[Tuple[CandidateFile, ...]]
        Extracted file list, or ``None`` if the shape doesn't match.
        Never raises.
    """
    try:
        candidates = getattr(generation, "candidates", None)
        if candidates is None:
            return None
        candidates_tuple = tuple(candidates)
        if not candidates_tuple:
            return tuple()  # non-None but empty → caller skips fan-out

        # Multi-file shape: inspect the first candidate for a ``files`` list.
        first = candidates_tuple[0]
        if isinstance(first, dict):
            files_list = first.get("files")
            if isinstance(files_list, list) and files_list:
                extracted: list = []
                seen_paths: set = set()
                for entry in files_list:
                    if not isinstance(entry, dict):
                        continue
                    path = entry.get("file_path")
                    content = entry.get("full_content")
                    if not isinstance(path, str) or not path:
                        continue
                    if content is None:
                        continue
                    if path in seen_paths:
                        continue  # dedupe defensively
                    seen_paths.add(path)
                    extracted.append(
                        CandidateFile(
                            file_path=path,
                            full_content=str(content),
                            rationale=str(entry.get("rationale") or ""),
                        )
                    )
                return tuple(extracted)

            # Single-file legacy shape: collect unique file_paths across
            # all candidates (typically just one in practice).
            path = first.get("file_path")
            content = first.get("full_content")
            if isinstance(path, str) and path and content is not None:
                return (
                    CandidateFile(
                        file_path=path,
                        full_content=str(content),
                        rationale=str(first.get("rationale") or ""),
                    ),
                )
        return None
    except Exception:  # noqa: BLE001 — shadow path must never crash caller
        return None


@dataclass(frozen=True)
class ShadowEvaluation:
    """Record of a single shadow-mode evaluation.

    Returned from :func:`evaluate_shadow_fanout` so callers can inspect
    the outcome in tests without log scraping. Nothing about this record
    is consumed by the production pipeline — Slice 3 is shadow-only.

    Attributes
    ----------
    ran:
        ``True`` if the shadow evaluation was armed (master + shadow both
        on). ``False`` when either flag was off — eligibility + graph
        construction were skipped and no ``[ParallelDispatch]`` telemetry
        emitted.
    skip_reason:
        Non-empty when ``ran=False`` — human-readable note for why the
        shadow hook short-circuited (master off / shadow off / unrecognized
        generation shape / empty candidates).
    eligibility:
        The :class:`FanoutEligibility` computed when ``ran=True``.
    graph:
        The :class:`ExecutionGraph` built when ``eligibility.allowed``.
        ``None`` when eligibility denied fan-out (or when graph build
        itself raised — build errors are caught + logged in shadow).
    graph_id:
        Convenience accessor for telemetry: ``graph.graph_id`` when
        present, empty string otherwise.
    plan_digest:
        Convenience accessor: ``graph.plan_digest`` when present, empty
        string otherwise.
    """

    ran: bool
    skip_reason: str = ""
    eligibility: Optional[FanoutEligibility] = None
    graph: Optional[ExecutionGraph] = None
    graph_id: str = ""
    plan_digest: str = ""


def evaluate_shadow_fanout(
    *,
    op_id: str,
    generation: Any,
    repo: str = "jarvis",
    gate: Optional[MemoryPressureGate] = None,
    posture_fn: Optional[
        Callable[[], Tuple[Optional[Posture], Optional[float]]]
    ] = None,
) -> ShadowEvaluation:
    """Slice 3 — shadow-mode fan-out evaluation for the post-GENERATE seam.

    Evaluates fan-out eligibility + (when allowed) builds the execution
    graph — but does NOT submit to any scheduler. All side effects are
    confined to structured log emission under the ``[ParallelDispatch]``
    tag, preserving the Slice 3 contract ("no silent shadow").

    Guards (first-short-circuit wins):

    1. Master flag (:func:`parallel_dispatch_enabled`) must be on.
    2. Shadow sub-flag (:func:`parallel_dispatch_shadow_enabled`) must
       be on. Enforce flag is intentionally irrelevant here — shadow and
       enforce are mutually exclusive modes; Slice 3 is shadow-only.
    3. ``generation`` artifact must yield an extractable candidate list
       via :func:`extract_candidate_files`.

    On each armed evaluation the module logger emits one or more
    ``[ParallelDispatch]`` lines:

    * The eligibility decision line (same format as
      :func:`is_fanout_eligible`).
    * When a graph is built, an additional
      ``[ParallelDispatch shadow_graph_built]`` line with ``graph_id``,
      ``plan_digest``, ``concurrency_limit``, and ``n_units``.

    Parameters
    ----------
    op_id:
        Parent op id for telemetry tagging + unit id derivation.
    generation:
        GENERATE artifact (``GenerationResult``-like). Defensive
        extraction; unknown shapes yield a ``ran=False`` result with a
        ``skip_reason``.
    repo:
        Repository tag for :class:`WorkUnitSpec`. Default ``"jarvis"``.
    gate, posture_fn:
        Dependency-injection hooks forwarded to
        :func:`is_fanout_eligible`; default to the module-level real gate
        + PostureStore reader.

    Returns
    -------
    ShadowEvaluation
        Always returned; never raises. Shadow never breaks the pipeline.
    """
    if not parallel_dispatch_enabled():
        result = ShadowEvaluation(ran=False, skip_reason="master_off")
        logger.debug(
            "[ParallelDispatch shadow_skipped] op=%s reason=master_off",
            op_id[:16],
        )
        return result

    if not parallel_dispatch_shadow_enabled():
        result = ShadowEvaluation(ran=False, skip_reason="shadow_off")
        logger.debug(
            "[ParallelDispatch shadow_skipped] op=%s reason=shadow_off",
            op_id[:16],
        )
        return result

    files = extract_candidate_files(generation)
    if files is None:
        result = ShadowEvaluation(ran=False, skip_reason="unrecognized_shape")
        logger.info(
            "[ParallelDispatch shadow_skipped] op=%s reason=unrecognized_shape",
            op_id[:16],
        )
        return result

    n_files = len(files)
    # Run eligibility regardless of n_files so the operator gets the
    # explicit reason (SINGLE_FILE_OP / EMPTY_CANDIDATE_LIST) in logs.
    eligibility = is_fanout_eligible(
        op_id=op_id,
        n_candidate_files=n_files,
        gate=gate,
        posture_fn=posture_fn,
        emit_log=True,
    )

    if not eligibility.allowed:
        return ShadowEvaluation(ran=True, eligibility=eligibility)

    # Try to build the graph — any validator error is caught + logged so
    # shadow never escalates into a production crash.
    try:
        graph = build_execution_graph(
            op_id=op_id,
            repo=repo,
            candidate_files=files,
            eligibility=eligibility,
        )
    except Exception as exc:  # noqa: BLE001 — shadow never crashes pipeline
        logger.warning(
            "[ParallelDispatch shadow_graph_build_failed] op=%s error=%s",
            op_id[:16],
            f"{type(exc).__name__}: {exc}",
        )
        return ShadowEvaluation(ran=True, eligibility=eligibility)

    logger.info(
        "[ParallelDispatch shadow_graph_built] op=%s graph_id=%s "
        "plan_digest=%s concurrency_limit=%d n_units=%d",
        op_id[:16],
        graph.graph_id,
        graph.plan_digest[:12],
        graph.concurrency_limit,
        len(graph.units),
    )
    return ShadowEvaluation(
        ran=True,
        eligibility=eligibility,
        graph=graph,
        graph_id=graph.graph_id,
        plan_digest=graph.plan_digest,
    )


# ---------------------------------------------------------------------------
# Slice 4 — enforce-mode submit
# ---------------------------------------------------------------------------


# Default per-graph wait timeout, seconds. Bounded so that a stuck
# graph does not pin a caller past :data:`~Ticket A1`'s
# ``--max-wall-seconds`` session cap — callers should verify their
# outer budget is larger than this value. Env-tunable via
# ``JARVIS_WAVE3_PARALLEL_WAIT_TIMEOUT_S`` for test isolation.
_DEFAULT_WAIT_TIMEOUT_S: float = 900.0  # 15 min default


def parallel_dispatch_wait_timeout_s(default: float = _DEFAULT_WAIT_TIMEOUT_S) -> float:
    """Env-overridable per-graph wait-for-completion budget.

    Ticket A1 discipline: a fan-out's wait must not defeat
    ``--max-wall-seconds``. When the env is unset, returns the
    in-code default (900s). When set but unparseable or non-positive,
    returns the default. Never raises.
    """
    raw = os.environ.get("JARVIS_WAVE3_PARALLEL_WAIT_TIMEOUT_S", "").strip()
    if not raw:
        return default
    try:
        v = float(raw)
    except ValueError:
        return default
    if v <= 0.0:
        return default
    return v


class FanoutOutcome(str, enum.Enum):
    """Terminal classifier for an enforce-mode fan-out evaluation.

    Stable, grep-friendly strings for telemetry + dashboards.
    """
    SKIPPED = "skipped"            # guards denied engagement (master/enforce off / ineligible)
    SUBMIT_DENIED = "submit_denied"  # scheduler returned False (capacity/not running)
    SUBMIT_FAILED = "submit_failed"  # scheduler raised a known-safe exception
    COMPLETED = "completed"          # graph reached COMPLETED phase
    FAILED = "failed"                # graph reached FAILED phase
    CANCELLED = "cancelled"          # graph reached CANCELLED / wait aborted
    TIMEOUT = "timeout"              # wait_for_graph timeout


@dataclass(frozen=True)
class FanoutResult:
    """Immutable result of :func:`enforce_evaluate_fanout`.

    Callers use this to decide whether downstream phases (VALIDATE,
    GATE, APPLY, VERIFY, COMPLETE) should proceed normally (outcome =
    SKIPPED or FAILED/CANCELLED → fall through to sequential) or to
    consume fan-out per-unit results (outcome = COMPLETED → slice4b
    runner may opt-in to per-unit APPLY). Slice 4 does NOT auto-skip
    downstream phases; that wiring lives in later slices / operator-
    authorized follow-ups.

    Attributes
    ----------
    outcome:
        :class:`FanoutOutcome` — primary classifier.
    skip_reason:
        Non-empty when ``outcome == SKIPPED``.
    eligibility:
        Eligibility decision from :func:`is_fanout_eligible` (or
        re-checked post-submit). Non-None when the guard matrix
        engaged past master+enforce gates.
    graph:
        Built graph when eligibility allowed. Non-None from
        eligibility allowed through the final outcome.
    state:
        Terminal :class:`GraphExecutionState` when scheduler completed
        (or wait exited normally). ``None`` on SKIPPED / SUBMIT_DENIED /
        SUBMIT_FAILED / TIMEOUT.
    wait_duration_s:
        Wall-clock seconds spent in :meth:`scheduler.wait_for_graph`.
        ``0.0`` when wait was not entered.
    error:
        Human-readable error message for SUBMIT_FAILED / FAILED /
        CANCELLED / TIMEOUT; empty for success/skipped.
    n_units_requested:
        Units in the built graph (eligibility.n_allowed at build time).
    n_units_completed / n_units_failed / n_units_cancelled:
        Per-unit tallies from terminal GraphExecutionState. All zero
        when no graph was submitted.
    """

    outcome: FanoutOutcome
    skip_reason: str = ""
    eligibility: Optional[FanoutEligibility] = None
    graph: Optional[ExecutionGraph] = None
    state: Optional[GraphExecutionState] = None
    wait_duration_s: float = 0.0
    error: str = ""
    n_units_requested: int = 0
    n_units_completed: int = 0
    n_units_failed: int = 0
    n_units_cancelled: int = 0

    @property
    def graph_id(self) -> str:
        return self.graph.graph_id if self.graph is not None else ""

    @property
    def plan_digest(self) -> str:
        return self.graph.plan_digest if self.graph is not None else ""


def _tally_unit_results(state: GraphExecutionState) -> Tuple[int, int, int]:
    """Return (completed, failed, cancelled) unit tallies from a terminal state."""
    return (
        len(state.completed_units),
        len(state.failed_units),
        len(state.cancelled_units),
    )


async def enforce_evaluate_fanout(
    *,
    op_id: str,
    generation: Any,
    scheduler: Any,
    repo: str = "jarvis",
    gate: Optional[MemoryPressureGate] = None,
    posture_fn: Optional[
        Callable[[], Tuple[Optional[Posture], Optional[float]]]
    ] = None,
    wait_timeout_s: Optional[float] = None,
) -> FanoutResult:
    """Slice 4 — enforce-mode fan-out evaluation + scheduler submit.

    Distinct from :func:`evaluate_shadow_fanout` in that this path
    actually submits the built graph to the injected scheduler and
    awaits terminal state. **Narrow error handling**: only known-safe
    categories are caught (``asyncio.CancelledError`` for cooperative
    cancellation per Ticket A1 discipline, ``asyncio.TimeoutError`` for
    wait-timeout, and specific scheduler-contract returns like
    ``submit(...) -> False``). Unexpected exceptions propagate —
    per-operator directive, enforce must fail loud on the hot path.

    Gates (first-short-circuit):

    1. Master flag (:func:`parallel_dispatch_enabled`) must be on.
    2. Enforce sub-flag (:func:`parallel_dispatch_enforce_enabled`)
       must be on. Shadow flag is independent — enforce wins when
       both are set.
    3. Generation artifact must yield a candidate file list.
    4. :func:`is_fanout_eligible` must return ``allowed=True``.
    5. :func:`build_execution_graph` must succeed.
    6. **Sovereignty re-check**: immediately before ``scheduler.submit``,
       :meth:`MemoryPressureGate.can_fanout` is consulted again with
       ``graph.concurrency_limit``. If the gate clamps below the
       built graph's concurrency (e.g. memory pressure rose since
       eligibility was computed), outcome = ``SUBMIT_DENIED`` without
       submitting. This is the operator-mandated §2 sovereignty
       check — gate must remain authoritative right at submit time.

    Parameters
    ----------
    op_id:
        Parent op id for telemetry and unit-id derivation.
    generation:
        GENERATE artifact (``GenerationResult``-like). Extracted
        defensively via :func:`extract_candidate_files`.
    scheduler:
        Object exposing the :class:`SubagentScheduler` async contract —
        ``submit(graph) -> bool`` + ``wait_for_graph(graph_id,
        timeout_s) -> GraphExecutionState``. Dependency-injected; tests
        pass in a mock. Runtime callers wire in
        :class:`~backend.core.ouroboros.governance.autonomy.subagent_scheduler.SubagentScheduler`.
    repo:
        Repository tag for :class:`WorkUnitSpec`. Default ``"jarvis"``.
    gate:
        Memory pressure gate override. Default: module-level singleton.
    posture_fn:
        Posture reader override. Default: PostureStore via
        :mod:`~backend.core.ouroboros.governance.posture_observer`.
    wait_timeout_s:
        Per-graph wait budget. Default via
        :func:`parallel_dispatch_wait_timeout_s` (env-overridable).

    Returns
    -------
    FanoutResult
        Immutable outcome record. Callers inspect
        :attr:`FanoutResult.outcome` + :attr:`FanoutResult.state` to
        decide next steps.

    Raises
    ------
    Exception
        Unexpected exceptions from :meth:`scheduler.submit` or
        :meth:`scheduler.wait_for_graph` propagate unchanged. Only
        ``asyncio.CancelledError``, ``asyncio.TimeoutError``, and
        scheduler-contract ``False`` returns are caught + classified.
    """
    wait_budget = (
        float(wait_timeout_s)
        if wait_timeout_s is not None
        else parallel_dispatch_wait_timeout_s()
    )

    # Guard 1: master flag.
    if not parallel_dispatch_enabled():
        logger.debug(
            "[ParallelDispatch enforce_skipped] op=%s reason=master_off",
            op_id[:16],
        )
        return FanoutResult(outcome=FanoutOutcome.SKIPPED, skip_reason="master_off")

    # Guard 2: enforce sub-flag.
    if not parallel_dispatch_enforce_enabled():
        logger.debug(
            "[ParallelDispatch enforce_skipped] op=%s reason=enforce_off",
            op_id[:16],
        )
        return FanoutResult(outcome=FanoutOutcome.SKIPPED, skip_reason="enforce_off")

    # Guard 3: candidate extraction.
    files = extract_candidate_files(generation)
    if files is None:
        logger.info(
            "[ParallelDispatch enforce_skipped] op=%s reason=unrecognized_shape",
            op_id[:16],
        )
        return FanoutResult(
            outcome=FanoutOutcome.SKIPPED,
            skip_reason="unrecognized_shape",
        )

    n_files = len(files)

    # Guard 4: eligibility (single-file / empty / memory-critical / posture).
    eligibility = is_fanout_eligible(
        op_id=op_id,
        n_candidate_files=n_files,
        gate=gate,
        posture_fn=posture_fn,
        emit_log=True,
    )
    if not eligibility.allowed:
        return FanoutResult(
            outcome=FanoutOutcome.SKIPPED,
            skip_reason=f"ineligible:{eligibility.reason_code.value}",
            eligibility=eligibility,
        )

    # Guard 5: graph build.
    # A ValueError here is a STRUCTURAL bug (duplicate paths from an
    # earlier slice's shape validation, etc.) — propagate per
    # fail-loud discipline. Do NOT swallow.
    graph = build_execution_graph(
        op_id=op_id,
        repo=repo,
        candidate_files=files,
        eligibility=eligibility,
    )

    # Guard 6: §2 sovereignty re-check right before submit.
    _gate = gate if gate is not None else get_default_gate()
    sovereignty = _gate.can_fanout(graph.concurrency_limit)
    if (
        sovereignty.level == PressureLevel.CRITICAL
        or sovereignty.n_allowed < graph.concurrency_limit
    ):
        logger.warning(
            "[ParallelDispatch enforce_submit_denied] op=%s graph_id=%s "
            "sovereignty_level=%s sovereignty_n_allowed=%d "
            "graph_concurrency_limit=%d reason=sovereignty_clamp",
            op_id[:16],
            graph.graph_id,
            sovereignty.level.value,
            sovereignty.n_allowed,
            graph.concurrency_limit,
        )
        return FanoutResult(
            outcome=FanoutOutcome.SUBMIT_DENIED,
            skip_reason="sovereignty_clamp",
            eligibility=eligibility,
            graph=graph,
            n_units_requested=graph.concurrency_limit,
            error=(
                f"memory gate clamped to n_allowed={sovereignty.n_allowed} "
                f"at submit time (graph needed {graph.concurrency_limit}); "
                f"level={sovereignty.level.value}"
            ),
        )

    # Submit. Narrow exception catches only: scheduler.submit() returning
    # False is the known-safe "not running / over capacity" signal.
    # asyncio.CancelledError is cooperative cancellation per Ticket A1.
    # All other exceptions propagate.
    logger.info(
        "[ParallelDispatch enforce_submit_start] op=%s graph_id=%s "
        "plan_digest=%s concurrency_limit=%d n_units=%d",
        op_id[:16],
        graph.graph_id,
        graph.plan_digest[:12],
        graph.concurrency_limit,
        len(graph.units),
    )
    try:
        accepted = await scheduler.submit(graph)
    except asyncio.CancelledError:
        logger.warning(
            "[ParallelDispatch enforce_cancelled] op=%s graph_id=%s phase=submit",
            op_id[:16],
            graph.graph_id,
        )
        raise
    if not accepted:
        logger.warning(
            "[ParallelDispatch enforce_submit_denied] op=%s graph_id=%s "
            "reason=scheduler_refused",
            op_id[:16],
            graph.graph_id,
        )
        return FanoutResult(
            outcome=FanoutOutcome.SUBMIT_DENIED,
            skip_reason="scheduler_refused",
            eligibility=eligibility,
            graph=graph,
            n_units_requested=graph.concurrency_limit,
            error="scheduler.submit returned False (not running or at capacity)",
        )

    # Wait for terminal. asyncio.TimeoutError from wait_for is the known
    # budget-exhaustion signal; asyncio.CancelledError is cooperative.
    # Both classify the result without re-raising. Scheduler-side
    # exceptions propagate unchanged.
    #
    # W3(7) Slice 5 — race against ambient cancel token. When a Class
    # D/E/F cancel fires while we're waiting on the scheduler, the race
    # surfaces as OperationCancelledError; we cancel the scheduler graph
    # and return a FanoutResult tagged with the cancel record so
    # downstream POSTMORTEM can read it. Master-flag-off →
    # current_cancel_token() returns None → race_or_wait_for falls
    # through to plain wait_for → byte-for-byte pre-W3(7).
    from backend.core.ouroboros.governance.cancel_token import (
        OperationCancelledError as _OpCancelledError,
        current_cancel_token as _curr_cancel_token,
        race_or_wait_for as _race_or_wait_for,
    )

    wait_start = time.monotonic()
    try:
        state = await _race_or_wait_for(
            scheduler.wait_for_graph(graph.graph_id, timeout_s=wait_budget),
            timeout=wait_budget + 5.0,  # outer guard slightly above scheduler's own timeout
            cancel_token=_curr_cancel_token(),
        )
    except _OpCancelledError as _cancel_exc:
        elapsed = time.monotonic() - wait_start
        cancel_record = _cancel_exc.record
        logger.warning(
            "[ParallelDispatch enforce_cancelled] op=%s graph_id=%s "
            "phase=wait elapsed_s=%.1f cancel_origin=%s cancel_id=%s "
            "(W3(7) Slice 5 — Class D/E/F cancel mid-fanout)",
            op_id[:16],
            graph.graph_id,
            elapsed,
            cancel_record.origin,
            cancel_record.cancel_id,
        )
        # Best-effort scheduler graph cancel so worktrees can be reaped.
        try:
            _cancel_method = getattr(scheduler, "cancel_graph", None)
            if _cancel_method is not None:
                _r = _cancel_method(graph.graph_id)
                if asyncio.iscoroutine(_r):
                    await _r
        except Exception:  # noqa: BLE001 — best-effort, never block POSTMORTEM
            pass
        return FanoutResult(
            outcome=FanoutOutcome.CANCELLED,
            eligibility=eligibility,
            graph=graph,
            wait_duration_s=elapsed,
            n_units_requested=graph.concurrency_limit,
            error=(
                f"cancelled mid-fanout: origin={cancel_record.origin} "
                f"cancel_id={cancel_record.cancel_id}"
            ),
        )
    except asyncio.CancelledError:
        elapsed = time.monotonic() - wait_start
        logger.warning(
            "[ParallelDispatch enforce_cancelled] op=%s graph_id=%s "
            "phase=wait elapsed_s=%.1f",
            op_id[:16],
            graph.graph_id,
            elapsed,
        )
        raise
    except asyncio.TimeoutError:
        elapsed = time.monotonic() - wait_start
        logger.warning(
            "[ParallelDispatch enforce_timeout] op=%s graph_id=%s "
            "wait_budget_s=%.1f elapsed_s=%.1f",
            op_id[:16],
            graph.graph_id,
            wait_budget,
            elapsed,
        )
        return FanoutResult(
            outcome=FanoutOutcome.TIMEOUT,
            eligibility=eligibility,
            graph=graph,
            wait_duration_s=elapsed,
            n_units_requested=graph.concurrency_limit,
            error=(
                f"wait_for_graph timeout after {elapsed:.1f}s "
                f"(budget={wait_budget:.1f}s)"
            ),
        )

    wait_elapsed = time.monotonic() - wait_start
    n_completed, n_failed, n_cancelled = _tally_unit_results(state)

    # Classify terminal phase.
    if state.phase == GraphExecutionPhase.COMPLETED:
        logger.info(
            "[ParallelDispatch enforce_completed] op=%s graph_id=%s "
            "wait_s=%.1f n_completed=%d n_failed=%d n_cancelled=%d",
            op_id[:16],
            graph.graph_id,
            wait_elapsed,
            n_completed,
            n_failed,
            n_cancelled,
        )
        return FanoutResult(
            outcome=FanoutOutcome.COMPLETED,
            eligibility=eligibility,
            graph=graph,
            state=state,
            wait_duration_s=wait_elapsed,
            n_units_requested=graph.concurrency_limit,
            n_units_completed=n_completed,
            n_units_failed=n_failed,
            n_units_cancelled=n_cancelled,
        )
    if state.phase == GraphExecutionPhase.FAILED:
        logger.warning(
            "[ParallelDispatch enforce_failed] op=%s graph_id=%s "
            "wait_s=%.1f n_completed=%d n_failed=%d n_cancelled=%d "
            "last_error=%r",
            op_id[:16],
            graph.graph_id,
            wait_elapsed,
            n_completed,
            n_failed,
            n_cancelled,
            state.last_error,
        )
        return FanoutResult(
            outcome=FanoutOutcome.FAILED,
            eligibility=eligibility,
            graph=graph,
            state=state,
            wait_duration_s=wait_elapsed,
            n_units_requested=graph.concurrency_limit,
            n_units_completed=n_completed,
            n_units_failed=n_failed,
            n_units_cancelled=n_cancelled,
            error=state.last_error or "graph phase=FAILED",
        )
    if state.phase == GraphExecutionPhase.CANCELLED:
        logger.warning(
            "[ParallelDispatch enforce_cancelled] op=%s graph_id=%s "
            "wait_s=%.1f n_completed=%d n_failed=%d n_cancelled=%d",
            op_id[:16],
            graph.graph_id,
            wait_elapsed,
            n_completed,
            n_failed,
            n_cancelled,
        )
        return FanoutResult(
            outcome=FanoutOutcome.CANCELLED,
            eligibility=eligibility,
            graph=graph,
            state=state,
            wait_duration_s=wait_elapsed,
            n_units_requested=graph.concurrency_limit,
            n_units_completed=n_completed,
            n_units_failed=n_failed,
            n_units_cancelled=n_cancelled,
            error="graph phase=CANCELLED",
        )

    # Non-terminal phase returned — unexpected contract violation.
    # Fail loud per operator directive.
    raise RuntimeError(
        f"scheduler returned non-terminal GraphExecutionPhase={state.phase.value} "
        f"for graph_id={graph.graph_id}; expected COMPLETED/FAILED/CANCELLED"
    )


# ---------------------------------------------------------------------------
# Module public surface — explicit for grep clarity
# ---------------------------------------------------------------------------


__all__ = [
    "CandidateFile",
    "DEFAULT_UNIT_MAX_ATTEMPTS",
    "DEFAULT_UNIT_TIMEOUT_S",
    "FanoutEligibility",
    "FanoutOutcome",
    "FanoutResult",
    "GRAPH_SCHEMA_VERSION",
    "PLANNER_ID",
    "POSTURE_CONFIDENCE_FLOOR",
    "ReasonCode",
    "ShadowEvaluation",
    "build_execution_graph",
    "enforce_evaluate_fanout",
    "evaluate_shadow_fanout",
    "extract_candidate_files",
    "is_fanout_eligible",
    "parallel_dispatch_enabled",
    "parallel_dispatch_enforce_enabled",
    "parallel_dispatch_max_units",
    "parallel_dispatch_shadow_enabled",
    "parallel_dispatch_wait_timeout_s",
    "posture_weight_for",
]
