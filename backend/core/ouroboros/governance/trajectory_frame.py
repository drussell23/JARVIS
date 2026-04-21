"""
TrajectoryFrame — Slice 1 of the Operator Trajectory View arc.
===============================================================

A frozen single-glance snapshot of "what is O+V doing right now."
Answers the five questions an operator asks at a glance:

  * **who**  — which op is active? (``op_id``, ``phase``)
  * **what** — which paths / tools / subject? (``target_paths``,
               ``active_tools``, ``subject``)
  * **why**  — what kicked it off? (``trigger_source``,
               ``trigger_reason``)
  * **when** — when did it start, when is it expected to finish?
               (``started_at_iso``, ``eta_seconds``, ``deadline_at_iso``)
  * **cost** — what's it costing so far, what's the budget?
               (``cost_spent_usd``, ``cost_budget_usd``)

Plus a ``next_step`` narrative (what will the agent do next?) and a
``confidence`` float (how sure are we about the ETA / cost
projections?).

Discipline
----------

* **§1 read-only** — frames carry NO control surface. No callbacks,
  no handlers, no references to the orchestrator.
* **§5 deterministic** — every field is extracted from typed
  suppliers via pure code. A frame is a function of (suppliers,
  timestamp), nothing else.
* **§7 fail-closed** — any supplier returning ``None`` surfaces as
  ``"unknown"`` / ``0.0`` / empty tuple. A frame ALWAYS builds; it
  never raises just because one supplier is missing.
* **§8 observable** — every frame carries a monotonic ``sequence``
  number + wall-clock iso so Slice 4's stream can order them.
"""
from __future__ import annotations

import enum
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, FrozenSet, Mapping, Optional, Tuple

logger = logging.getLogger("Ouroboros.TrajectoryFrame")


TRAJECTORY_FRAME_SCHEMA_VERSION: str = "trajectory_frame.v1"


# ---------------------------------------------------------------------------
# Phase enum — narrow, mirrors common Ouroboros phase names
# ---------------------------------------------------------------------------


class TrajectoryPhase(str, enum.Enum):
    """Narrow phase taxonomy the trajectory view surfaces.

    The full Ouroboros pipeline has more phases (CLASSIFY, ROUTE,
    CONTEXT_EXPANSION, PLAN, GENERATE, VALIDATE, GATE, APPROVE, APPLY,
    VERIFY, COMPLETE) — those map onto the six presentation-level
    phases below. Callers pass the raw phase string; the builder
    collapses via :func:`phase_from_raw`.
    """

    IDLE = "idle"
    CLASSIFYING = "classifying"
    PLANNING = "planning"
    GENERATING = "generating"
    APPLYING = "applying"
    VERIFYING = "verifying"
    COMPLETE = "complete"
    UNKNOWN = "unknown"


_PHASE_MAP: Dict[str, TrajectoryPhase] = {
    # Raw-name → presentation phase
    "idle":               TrajectoryPhase.IDLE,
    "classify":           TrajectoryPhase.CLASSIFYING,
    "classifying":        TrajectoryPhase.CLASSIFYING,
    "clarify":            TrajectoryPhase.CLASSIFYING,
    "route":              TrajectoryPhase.CLASSIFYING,
    "context_expansion":  TrajectoryPhase.PLANNING,
    "plan":               TrajectoryPhase.PLANNING,
    "planning":           TrajectoryPhase.PLANNING,
    "generate":           TrajectoryPhase.GENERATING,
    "generating":         TrajectoryPhase.GENERATING,
    "generate_retry":     TrajectoryPhase.GENERATING,
    "validate":           TrajectoryPhase.GENERATING,
    "gate":               TrajectoryPhase.GENERATING,
    "approve":            TrajectoryPhase.GENERATING,
    "apply":              TrajectoryPhase.APPLYING,
    "applying":           TrajectoryPhase.APPLYING,
    "verify":             TrajectoryPhase.VERIFYING,
    "verifying":          TrajectoryPhase.VERIFYING,
    "complete":           TrajectoryPhase.COMPLETE,
    "postmortem":         TrajectoryPhase.COMPLETE,
    "done":               TrajectoryPhase.COMPLETE,
}


def phase_from_raw(raw: Optional[str]) -> TrajectoryPhase:
    """Map a raw phase string to the presentation-level enum.

    Unknown / None / empty → :attr:`TrajectoryPhase.UNKNOWN` (NOT
    IDLE — IDLE is an explicit "nothing is happening" state, UNKNOWN
    is "we couldn't figure out what's happening," which is an
    operator-visible yellow flag).
    """
    if not raw:
        return TrajectoryPhase.UNKNOWN
    return _PHASE_MAP.get(raw.strip().lower(), TrajectoryPhase.UNKNOWN)


# ---------------------------------------------------------------------------
# Confidence bands — narrative hints for the UI
# ---------------------------------------------------------------------------


class Confidence(str, enum.Enum):
    HIGH = "high"      # ≥ 0.8
    MEDIUM = "medium"  # ≥ 0.5
    LOW = "low"        # ≥ 0.2
    UNKNOWN = "unknown"  # < 0.2 or missing


def confidence_band(value: Optional[float]) -> Confidence:
    if value is None:
        return Confidence.UNKNOWN
    if value >= 0.8:
        return Confidence.HIGH
    if value >= 0.5:
        return Confidence.MEDIUM
    if value >= 0.2:
        return Confidence.LOW
    return Confidence.UNKNOWN


# ---------------------------------------------------------------------------
# TrajectoryFrame
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TrajectoryFrame:
    """One glanceable snapshot.

    All fields default to their "unknown" sentinel so a frame can
    always be built even when suppliers are missing. The narrative
    methods handle the ``unknown`` cases gracefully.
    """

    # --- identity + time ------------------------------------------------
    sequence: int = 0
    snapshot_at_iso: str = ""
    snapshot_at_ts: float = 0.0

    # --- who ------------------------------------------------------------
    op_id: str = ""
    phase: TrajectoryPhase = TrajectoryPhase.UNKNOWN
    raw_phase: str = ""

    # --- what -----------------------------------------------------------
    subject: str = ""
    target_paths: Tuple[str, ...] = ()
    active_tools: Tuple[str, ...] = ()

    # --- why ------------------------------------------------------------
    trigger_source: str = ""
    trigger_reason: str = ""

    # --- when -----------------------------------------------------------
    started_at_iso: str = ""
    started_at_ts: float = 0.0
    eta_seconds: Optional[float] = None
    deadline_at_iso: str = ""

    # --- cost -----------------------------------------------------------
    cost_spent_usd: float = 0.0
    cost_budget_usd: Optional[float] = None

    # --- forward --------------------------------------------------------
    next_step: str = ""
    confidence: Optional[float] = None

    # --- status flags ---------------------------------------------------
    is_idle: bool = False
    is_blocked: bool = False
    blocked_reason: str = ""

    schema_version: str = TRAJECTORY_FRAME_SCHEMA_VERSION

    # --- convenience helpers --------------------------------------------

    @property
    def confidence_band(self) -> Confidence:
        return confidence_band(self.confidence)

    @property
    def has_op(self) -> bool:
        return bool(self.op_id) and not self.is_idle

    @property
    def cost_remaining_usd(self) -> Optional[float]:
        if self.cost_budget_usd is None:
            return None
        return max(0.0, self.cost_budget_usd - self.cost_spent_usd)

    @property
    def cost_used_ratio(self) -> Optional[float]:
        if self.cost_budget_usd is None or self.cost_budget_usd <= 0:
            return None
        return self.cost_spent_usd / self.cost_budget_usd

    # --- narrative renderings ------------------------------------------

    def one_line_summary(self) -> str:
        """Compact single-line rendering — the status-bar form.

        Examples::

            idle
            op-abc123 classifying (sensor=test_failure)  ETA 12s  $0.003
            op-abc123 applying backend/foo.py  ETA 45s  $0.012 / $0.50
        """
        if self.is_idle or not self.has_op:
            return "idle"
        parts = [f"{_short_op_id(self.op_id)} {self.phase.value}"]
        if self.target_paths:
            parts.append(self.target_paths[0])
            if len(self.target_paths) > 1:
                parts[-1] += f" (+{len(self.target_paths) - 1})"
        if self.trigger_source:
            parts.append(f"sensor={self.trigger_source}")
        if self.eta_seconds is not None:
            parts.append(f"ETA {_fmt_duration(self.eta_seconds)}")
        cost_str = _fmt_cost(self.cost_spent_usd, self.cost_budget_usd)
        if cost_str:
            parts.append(cost_str)
        if self.is_blocked:
            parts.append(f"BLOCKED: {self.blocked_reason or 'unknown'}")
        return "  ".join(parts)

    def narrative(self) -> str:
        """Expanded multi-line rendering — matches the gap-quote shape.

        ``currently: op-X, analyzing path Y because sensor Z fired,
        ETA W seconds, cost $C.``
        """
        if self.is_idle:
            return "currently: idle"
        if not self.has_op:
            return "currently: unknown"
        phase_verb = _phase_verb(self.phase)
        path_phrase = ""
        if self.target_paths:
            if len(self.target_paths) == 1:
                path_phrase = f" path {self.target_paths[0]}"
            else:
                path_phrase = (
                    f" {len(self.target_paths)} paths "
                    f"({self.target_paths[0]}, ...)"
                )
        why_phrase = ""
        if self.trigger_source:
            reason = self.trigger_reason or "fired"
            why_phrase = f" because sensor {self.trigger_source} {reason}"
        when_phrase = ""
        if self.eta_seconds is not None:
            when_phrase = f", ETA {_fmt_duration(self.eta_seconds)}"
        cost_phrase = ""
        if self.cost_spent_usd > 0 or self.cost_budget_usd is not None:
            cost_phrase = f", cost {_fmt_cost(self.cost_spent_usd, self.cost_budget_usd)}"
        line = (
            f"currently: {_short_op_id(self.op_id)}, "
            f"{phase_verb}{path_phrase}{why_phrase}{when_phrase}{cost_phrase}"
        )
        if self.is_blocked:
            line += f"  [BLOCKED: {self.blocked_reason or 'unknown'}]"
        if self.next_step:
            line += f"  →  {self.next_step}"
        return line

    # --- projection for SSE / IDE --------------------------------------

    def project(self) -> Dict[str, Any]:
        """JSON-safe projection for over-the-wire / GET endpoints.

        Excludes raw data that isn't safe for logs; bounds narrative
        strings at conservative caps.
        """
        return {
            "schema_version": self.schema_version,
            "sequence": self.sequence,
            "snapshot_at_iso": self.snapshot_at_iso,
            "snapshot_at_ts": self.snapshot_at_ts,
            "op_id": self.op_id,
            "phase": self.phase.value,
            "raw_phase": self.raw_phase,
            "subject": self.subject[:200],
            "target_paths": list(self.target_paths)[:20],
            "active_tools": list(self.active_tools)[:20],
            "trigger_source": self.trigger_source,
            "trigger_reason": self.trigger_reason[:500],
            "started_at_iso": self.started_at_iso,
            "started_at_ts": self.started_at_ts,
            "eta_seconds": self.eta_seconds,
            "deadline_at_iso": self.deadline_at_iso,
            "cost_spent_usd": self.cost_spent_usd,
            "cost_budget_usd": self.cost_budget_usd,
            "cost_used_ratio": self.cost_used_ratio,
            "cost_remaining_usd": self.cost_remaining_usd,
            "next_step": self.next_step[:500],
            "confidence": self.confidence,
            "confidence_band": self.confidence_band.value,
            "is_idle": self.is_idle,
            "is_blocked": self.is_blocked,
            "blocked_reason": self.blocked_reason[:200],
            "has_op": self.has_op,
            "one_line_summary": self.one_line_summary(),
            "narrative": self.narrative(),
        }


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def idle_frame(*, sequence: int = 0, now_ts: Optional[float] = None) -> TrajectoryFrame:
    """Build an explicit ``idle`` frame."""
    ts = now_ts if now_ts is not None else _now_ts()
    return TrajectoryFrame(
        sequence=sequence,
        snapshot_at_iso=_iso_for(ts),
        snapshot_at_ts=ts,
        phase=TrajectoryPhase.IDLE,
        is_idle=True,
    )


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _now_ts() -> float:
    import time
    return time.time()


def _iso_for(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).replace(
        microsecond=0,
    ).isoformat()


def _short_op_id(op_id: str, *, prefix_chars: int = 10) -> str:
    """First N chars of an op_id, unchanged otherwise."""
    if not op_id:
        return "op-?"
    return op_id[:prefix_chars] if len(op_id) > prefix_chars + 2 else op_id


_PHASE_VERB: Dict[TrajectoryPhase, str] = {
    TrajectoryPhase.IDLE: "idle",
    TrajectoryPhase.CLASSIFYING: "classifying",
    TrajectoryPhase.PLANNING: "planning",
    TrajectoryPhase.GENERATING: "generating",
    TrajectoryPhase.APPLYING: "applying",
    TrajectoryPhase.VERIFYING: "verifying",
    TrajectoryPhase.COMPLETE: "completing",
    TrajectoryPhase.UNKNOWN: "working on",
}


def _phase_verb(phase: TrajectoryPhase) -> str:
    return _PHASE_VERB.get(phase, "working on")


def _fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds / 60)}m"
    return f"{seconds / 3600:.1f}h"


def _fmt_cost(spent: float, budget: Optional[float]) -> str:
    if spent <= 0 and budget is None:
        return ""
    if budget is None or budget <= 0:
        return f"${spent:.3f}"
    return f"${spent:.3f} / ${budget:.2f}"


__all__ = [
    "Confidence",
    "TRAJECTORY_FRAME_SCHEMA_VERSION",
    "TrajectoryFrame",
    "TrajectoryPhase",
    "confidence_band",
    "idle_frame",
    "phase_from_raw",
]

_ = (field, FrozenSet, Mapping)  # silence unused-import guards
