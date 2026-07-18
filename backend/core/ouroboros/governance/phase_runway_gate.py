"""Predictive Phase-Aware Checkpointing — the FSM anticipates the wall-clock
boundary and gracefully suspends itself BEFORE entering a high-latency phase it
cannot finish within the remaining runway.

Root-cause fix for two chaos-soak findings (2026-07-18):

  * **Throughput starvation at the bg_timebox ceiling.** An op that cannot
    complete its APPLY+VERIFY tail within the remaining runway burns that runway
    retrying, then gets hard-killed mid-phase — worst case a *severed mid-APPLY
    working tree*. Instead we suspend cleanly at the phase boundary and resume
    on the next ignition (the boundary-crossing that IS already proven live).
  * **The atomic workspace stash was only unit-proven.** A graceful suspend that
    lands during a dirty *multi-file* APPLY exercises ``create_stash_ref`` live
    — the organic validation the two prior soaks never hit (both cut at GENERATE,
    pre-mutation → clean tree).

Design (mandate-faithful):

  * **DRY.** Reuses :class:`admission_estimator.WaitTimeEstimator` — the codebase's
    canonical per-key EWMA — keyed by FSM phase name. NO new metrics engine.
    Reuses ``fsm_checkpoint`` for the signed per-op checkpoint + atomic git stash
    (``capture_single_op``). The per-phase latency is fed from the ONE canonical
    transition choke, ``OperationContext.advance()``.
  * **Watchdog Isolation Invariant (Slice 47) preserved.** The wall-clock
    watchdog stays BLIND to phase/op state. This predictive intelligence lives
    ORCHESTRATOR-side (which legitimately owns phase transitions) and reads only
    the *decoupled* deadline seam — ``OUROBOROS_BATTLE_WALL_DEADLINE_MONOTONIC``
    (a plain monotonic timestamp string the harness publishes) and
    ``ctx.pipeline_deadline`` (the per-op budget). It NEVER holds a live handle
    into the watchdog. The watchdog remains a pure time-only backstop.
  * **Fail-open.** Unknown runway / cold-start EWMA / disabled flag / failed
    checkpoint write → NEVER suspend. We suspend ONLY on positive evidence the
    tail will not fit. The hard cap remains the ultimate backstop; a stranded op
    (suspended with no resumable checkpoint) is strictly worse than a hard-kill
    the stash still covers, so every uncertain case proceeds into the phase.

Authority invariants (this is a leaf — enforced by review, mirrors
``admission_estimator``):
  * MUST NOT import: ``orchestrator`` / ``candidate_generator`` / ``providers`` /
    ``governed_loop_service`` / ``policy`` / ``iron_gate`` / ``risk_tier``. It is
    consumed by the orchestrator, never the reverse.
  * ``fsm_checkpoint`` is imported lazily (inside the suspend call) to keep the
    module import-cheap and cycle-free.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

PHASE_RUNWAY_GATE_SCHEMA_VERSION: str = "phase_runway_gate.v1"

# The irreversible, latency-heavy tail projected at the pre-APPLY boundary.
# APPLY itself (disk writes) is fast; VERIFY (scoped test run) is the slow part,
# so gating on APPLY alone would systematically under-project. VISUAL_VERIFY is
# included when armed. COMPLETE is effectively instantaneous.
PRE_APPLY_TAIL_PHASES: Tuple[str, ...] = ("APPLY", "VERIFY", "VISUAL_VERIFY")


# ---------------------------------------------------------------------------
# Env knobs — clamped, hot-revertable. All read live (no cached module state)
# so a `/flag` flip takes effect on the next op.
# ---------------------------------------------------------------------------


_FALSY = ("0", "false", "no", "off")


def predictive_checkpoint_enabled() -> bool:
    """``JARVIS_PREDICTIVE_CHECKPOINT_ENABLED`` — master switch, default ON.
    Off → :func:`evaluate` always returns a proceed verdict (byte-for-byte the
    legacy path). NEVER raises."""
    return os.environ.get(
        "JARVIS_PREDICTIVE_CHECKPOINT_ENABLED", "true",
    ).strip().lower() not in _FALSY


def phase_latency_alpha() -> float:
    """``JARVIS_PHASE_LATENCY_EWMA_ALPHA`` — EWMA weight on the new phase-latency
    sample. Default 0.3, clamped [0.05, 0.95] (matches the admission estimator's
    band). NEVER raises."""
    raw = os.environ.get("JARVIS_PHASE_LATENCY_EWMA_ALPHA")
    if raw is None:
        return 0.3
    try:
        return max(0.05, min(0.95, float(raw)))
    except (TypeError, ValueError):
        return 0.3


def min_samples() -> int:
    """``JARVIS_PREDICTIVE_MIN_SAMPLES`` — minimum EWMA samples for a phase before
    its projection is trusted. Below this the phase is treated as UNKNOWN (its
    cost is not counted — the safe, under-projecting direction). Default 3,
    floor 1. NEVER raises."""
    try:
        n = int(os.environ.get("JARVIS_PREDICTIVE_MIN_SAMPLES", "3").strip())
        return n if n >= 1 else 3
    except (TypeError, ValueError):
        return 3


def safety_factor() -> float:
    """``JARVIS_PREDICTIVE_SAFETY_FACTOR`` — headroom multiplier on the projected
    tail. 1.25 = require 25% slack over the mean projection (EWMA is a mean;
    real durations overshoot it half the time). Default 1.25, clamped
    [1.0, 5.0]. NEVER raises."""
    try:
        v = float(os.environ.get("JARVIS_PREDICTIVE_SAFETY_FACTOR", "1.25"))
        return max(1.0, min(5.0, v))
    except (TypeError, ValueError):
        return 1.25


def stash_reserve_s() -> float:
    """``JARVIS_PREDICTIVE_STASH_RESERVE_S`` — seconds reserved for the suspend
    itself (git stash create + HMAC-sign + atomic write). Added to the threshold
    so we suspend with time still on the clock to actually persist. Default 8.0,
    clamped [0.0, 120.0]. NEVER raises."""
    try:
        v = float(os.environ.get("JARVIS_PREDICTIVE_STASH_RESERVE_S", "8.0"))
        return max(0.0, min(120.0, v))
    except (TypeError, ValueError):
        return 8.0


def projection_ceiling_s() -> float:
    """``JARVIS_PREDICTIVE_PROJECTION_CEILING_S`` — per-phase projection clamp. A
    single pathological phase-latency spike cannot wedge the gate into
    permanently suspending every future op. Default 900.0, floor 1.0. NEVER
    raises."""
    try:
        v = float(os.environ.get("JARVIS_PREDICTIVE_PROJECTION_CEILING_S", "900.0"))
        return v if v >= 1.0 else 900.0
    except (TypeError, ValueError):
        return 900.0


# ---------------------------------------------------------------------------
# Per-phase EWMA — a dedicated WaitTimeEstimator instance keyed by phase name.
# Process-wide singleton so the advance()-side feed and the orchestrator-side
# read agree on one instance without threading a reference through the FSM.
# ---------------------------------------------------------------------------

_ESTIMATOR: Any = None  # WaitTimeEstimator | None
_ESTIMATOR_LOCK = threading.Lock()


def get_phase_latency_estimator() -> Any:
    """Lazy process-wide :class:`WaitTimeEstimator` singleton, keyed by phase
    name. Returns ``None`` only if the estimator module is somehow unavailable
    (then every caller degrades to the fail-open proceed path). NEVER raises."""
    global _ESTIMATOR
    if _ESTIMATOR is not None:
        return _ESTIMATOR
    try:
        with _ESTIMATOR_LOCK:
            if _ESTIMATOR is None:
                from backend.core.ouroboros.governance.admission_estimator import (  # noqa: PLC0415
                    WaitTimeEstimator,
                )
                _ESTIMATOR = WaitTimeEstimator(alpha=phase_latency_alpha())
        return _ESTIMATOR
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug("[PhaseRunway] estimator init degraded: %s", exc)
        return None


def record_phase_duration(phase: str, duration_s: float) -> None:
    """Feed one completed-phase wall-clock duration into the per-phase EWMA.
    Called from the ``OperationContext.advance()`` transition choke for the phase
    just LEFT. Silently no-ops on garbage (the estimator itself rejects NaN /
    negative / non-numeric). NEVER raises."""
    try:
        est = get_phase_latency_estimator()
        if est is None:
            return
        key = _phase_key(phase)
        if not key:
            return
        est.update_observed(key, duration_s)
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug("[PhaseRunway] record_phase_duration degraded: %s", exc)


def _phase_key(phase: Any) -> str:
    try:
        # Accept an OperationPhase enum, its .name, or a bare string.
        name = getattr(phase, "name", None)
        raw = name if isinstance(name, str) else phase
        return str(raw or "").strip().upper()
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------------------
# Runway computation — the DECOUPLED read (Watchdog Isolation Invariant).
# ---------------------------------------------------------------------------


def _session_wall_remaining_s() -> Optional[float]:
    """Seconds left on the SESSION wall-clock cap, from the harness-published
    decoupled seam ``OUROBOROS_BATTLE_WALL_DEADLINE_MONOTONIC`` (``repr`` of a
    ``time.monotonic()`` deadline). Returns None when unset/malformed — i.e. no
    session cap in effect (production, or a legacy 3-way-race soak). NEVER
    raises."""
    raw = os.environ.get("OUROBOROS_BATTLE_WALL_DEADLINE_MONOTONIC", "").strip()
    if not raw:
        return None
    try:
        deadline_mono = float(raw)
    except (TypeError, ValueError):
        return None
    try:
        return deadline_mono - time.monotonic()
    except Exception:  # noqa: BLE001
        return None


def _op_deadline_remaining_s(ctx: Any) -> Optional[float]:
    """Seconds left on the per-op ``ctx.pipeline_deadline`` (a UTC datetime,
    stamped once at submit). Returns None when absent. NEVER raises."""
    try:
        deadline = getattr(ctx, "pipeline_deadline", None)
        if deadline is None:
            return None
        if not isinstance(deadline, datetime):
            return None
        now = datetime.now(tz=timezone.utc)
        # Guard naive datetimes (treat as UTC) so subtraction never raises.
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        return (deadline - now).total_seconds()
    except Exception:  # noqa: BLE001
        return None


def remaining_runway_s(ctx: Any) -> Optional[float]:
    """The tightest remaining runway across ALL applicable decoupled deadlines:
    ``min(session_wall_remaining, op_pipeline_deadline_remaining)``. Returns None
    ONLY when neither source is available — in which case the caller fails open
    (never suspends). NEVER raises."""
    candidates = [
        _session_wall_remaining_s(),
        _op_deadline_remaining_s(ctx),
    ]
    present = [c for c in candidates if c is not None]
    if not present:
        return None
    return min(present)


# ---------------------------------------------------------------------------
# The verdict.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunwayVerdict:
    """The pre-phase predictive-checkpoint decision + full telemetry. Emitted on
    EVERY evaluation (§7 Absolute Observability) whether it suspends or proceeds,
    so the operator can see the gate's reasoning."""

    should_suspend: bool
    reason: str
    phases: Tuple[str, ...] = ()
    runway_s: Optional[float] = None
    projected_s: float = 0.0
    threshold_s: float = 0.0
    known_phases: Tuple[str, ...] = ()
    unknown_phases: Tuple[str, ...] = ()
    detail: Dict[str, Any] = field(default_factory=dict)

    def as_telemetry(self) -> Dict[str, Any]:
        return {
            "should_suspend": self.should_suspend,
            "reason": self.reason,
            "phases": list(self.phases),
            "runway_s": (round(self.runway_s, 2) if self.runway_s is not None else None),
            "projected_s": round(self.projected_s, 2),
            "threshold_s": round(self.threshold_s, 2),
            "known_phases": list(self.known_phases),
            "unknown_phases": list(self.unknown_phases),
            "schema_version": PHASE_RUNWAY_GATE_SCHEMA_VERSION,
            **self.detail,
        }


def _proceed(reason: str, **kw: Any) -> RunwayVerdict:
    return RunwayVerdict(should_suspend=False, reason=reason, **kw)


def evaluate(
    ctx: Any,
    phases: Sequence[str] = PRE_APPLY_TAIL_PHASES,
) -> RunwayVerdict:
    """Decide whether to predictively suspend *ctx* BEFORE entering *phases*.

    The heuristic (all fail-open):

      1. disabled            → proceed
      2. runway unknown      → proceed (no deadline in effect)
      3. all phases cold     → proceed (no evidence to project on)
      4. projected tail      = Σ over KNOWN phases of min(EWMA, ceiling)
                               (unknown phases contribute 0 — under-projects,
                                the safe direction)
      5. threshold           = projected * safety_factor + stash_reserve
      6. suspend  IFF  runway < threshold

    NEVER raises — any internal error degrades to a proceed verdict.
    """
    try:
        _phases = tuple(_phase_key(p) for p in phases if _phase_key(p))
        if not predictive_checkpoint_enabled():
            return _proceed("disabled", phases=_phases)

        runway = remaining_runway_s(ctx)
        if runway is None:
            return _proceed("no_deadline", phases=_phases)

        est = get_phase_latency_estimator()
        if est is None:
            return _proceed("estimator_unavailable", phases=_phases, runway_s=runway)

        _min = min_samples()
        _ceil = projection_ceiling_s()
        try:
            counts = est.stats().get("sample_counts", {}) or {}
        except Exception:  # noqa: BLE001
            counts = {}

        projected = 0.0
        known: Tuple[str, ...] = ()
        unknown: Tuple[str, ...] = ()
        for p in _phases:
            key = p.lower()  # WaitTimeEstimator lower-cases its keys
            n = int(counts.get(key, 0) or 0)
            if n >= _min:
                projected += min(float(est.project_wait(p)), _ceil)
                known = known + (p,)
            else:
                unknown = unknown + (p,)

        if not known:
            return _proceed(
                "cold_start", phases=_phases, runway_s=runway,
                unknown_phases=unknown,
                detail={"min_samples": _min},
            )

        threshold = projected * safety_factor() + stash_reserve_s()
        should = runway < threshold
        return RunwayVerdict(
            should_suspend=should,
            reason=("runway_shortfall" if should else "fits"),
            phases=_phases,
            runway_s=runway,
            projected_s=projected,
            threshold_s=threshold,
            known_phases=known,
            unknown_phases=unknown,
            detail={
                "safety_factor": safety_factor(),
                "stash_reserve_s": stash_reserve_s(),
                "min_samples": _min,
            },
        )
    except Exception as exc:  # noqa: BLE001 — defensive: fail open
        logger.debug("[PhaseRunway] evaluate degraded (proceeding): %s", exc)
        return _proceed("evaluate_error")


# ---------------------------------------------------------------------------
# The suspend — persist the resumable state, then the caller ends the op.
# ---------------------------------------------------------------------------


def predictive_suspend(
    ctx: Any,
    phase: str,
    verdict: "Optional[RunwayVerdict]" = None,
    *,
    base_dir: "Optional[str]" = None,
) -> "Optional[str]":
    """Persist a signed, resumable checkpoint for THIS op (its ctx + the current
    dirty working-tree stash) so it can resume next ignition instead of entering
    *phase*. Delegates entirely to ``fsm_checkpoint.capture_single_op`` (DRY: all
    checkpoint/VC logic lives there). Returns the checkpoint path on success, or
    None (fail-soft — the caller MUST then proceed into the phase, never strand
    the op). NEVER raises.

    Note: unlike the session-wide ``capture_inflight``, this checkpoints exactly
    the given op straight from its ctx — it does NOT depend on the op being in
    the in-flight registry, so it is correct to call mid-session for one op while
    others keep running.
    """
    try:
        from backend.core.ouroboros.governance.fsm_checkpoint import (  # noqa: PLC0415
            capture_single_op,
        )
        path = capture_single_op(
            ctx, phase=_phase_key(phase) or None,
            reason="predictive_phase_aware", base_dir=base_dir,
        )
        if path:
            _t = verdict.as_telemetry() if verdict is not None else {}
            logger.warning(
                "[PhaseRunway] PREDICTIVE SUSPEND op=%s before phase=%s "
                "runway=%.1fs projected=%.1fs threshold=%.1fs -> signed "
                "checkpoint %s (resumes next ignition)",
                getattr(ctx, "op_id", "?"), _phase_key(phase),
                _t.get("runway_s") or -1.0, _t.get("projected_s") or -1.0,
                _t.get("threshold_s") or -1.0, path,
            )
        return path
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug("[PhaseRunway] predictive_suspend degraded: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Observability snapshot (consumed by the /observability surface if wired).
# ---------------------------------------------------------------------------


def stats() -> Dict[str, Any]:
    """Read-only snapshot of the gate's config + per-phase EWMA. NEVER raises."""
    try:
        est = get_phase_latency_estimator()
        est_stats = est.stats() if est is not None else {}
        return {
            "enabled": predictive_checkpoint_enabled(),
            "alpha": phase_latency_alpha(),
            "min_samples": min_samples(),
            "safety_factor": safety_factor(),
            "stash_reserve_s": stash_reserve_s(),
            "projection_ceiling_s": projection_ceiling_s(),
            "pre_apply_tail_phases": list(PRE_APPLY_TAIL_PHASES),
            "phase_ewma": est_stats,
            "schema_version": PHASE_RUNWAY_GATE_SCHEMA_VERSION,
        }
    except Exception:  # noqa: BLE001
        return {"schema_version": PHASE_RUNWAY_GATE_SCHEMA_VERSION}


def _reset_for_tests() -> None:
    """Test helper — drop the singleton so a fresh estimator is built. Production
    code MUST NOT call this. NEVER raises."""
    global _ESTIMATOR
    try:
        with _ESTIMATOR_LOCK:
            _ESTIMATOR = None
    except Exception:  # noqa: BLE001
        _ESTIMATOR = None
