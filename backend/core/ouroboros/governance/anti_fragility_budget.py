"""
Anti-Fragility Budget Per-Module
=================================

Closes §40 Wave 4 #13 — the sixth and final Wave 4 (Tier 3
calibration learning) arc. Closes Wave 4. Per the operator
binding:

  "Per-module 'stress budget': allows operations that would
   normally fail to complete, but capped per module so a single
   fragile module doesn't dominate failure modes. Composes
   second_order_doll metric + ConvergenceGovernor."

This substrate is a **pure-function stress-budget evaluator**.
For each caller-supplied module_id it composes:

* Wave 4 #9 ``belief_revision_ledger`` — per-domain FALSIFIED
  belief rate → belief-pressure score.
* Wave 1 #15 ``second_order_doll_metric`` — per-Category axis
  stage → doll-fragility score (UNTOUCHED most fragile,
  GRADUATED least fragile).

The combined stress score (0.0–1.0) drives a 4-value
:class:`StressVerdict` and a deterministic budget allowance
that consumer-side throttling (SensorGovernor wiring,
risk_tier_floor, dispatch caps) can read advisory. The
substrate claims no authority over op dispatch — it only
SURFACES the budget.

Anti-fragility framing: modules that survive belief
falsifications + maintain GRADUATED doll stage strengthen
their budget over time (HEALTHY → full budget). Modules
accumulating falsifications + lingering at UNTOUCHED stage
exhaust their budget (EXHAUSTED → 0 remaining). The
threshold-driven transition is the "stress test" — modules
that pass repeatedly become more resilient; modules that
fail shrink their tolerance.

Composition contract — thin pure-function evaluator over
canonical substrates:

* :func:`belief_revision_ledger.evaluate_recent_beliefs` (Wave
  4 #9) — substring-matched against module_id (any belief
  whose ``domain`` OR ``target_files`` contain the module_id
  contributes to that module's pressure).
* :func:`second_order_doll_metric.aggregate_doll_completion`
  (Wave 1 #15) — looked up by Category when module_id matches
  a canonical Category name; otherwise doll_fragility = 0.
* :func:`governance_boundary_gate.is_boundary_crossed` (Wave 2
  #5) — defense-in-depth flag when module_id matches the
  governance cage.
* :func:`cross_process_jsonl.flock_append_line` — optional
  §33.4 audit ledger at
  ``.jarvis/anti_fragility_ledger.jsonl``.

NEVER raises. Empty belief ledger / missing doll snapshot /
caller passes garbage module_id all degrade to HEALTHY
verdict, not exception.

Closed 4-value :class:`StressVerdict`:

  HEALTHY        ✓ stress_score < stressed_threshold
                   (full budget, ops proceed normally)
  STRESSED       ⚠ stress_score in [stressed, exhausted)
                   (budget = max // stressed_divisor)
  EXHAUSTED      🚫 stress_score ≥ exhausted_threshold
                   (budget = 0, consumer should pause)
  DISABLED       ◌ master flag off OR module_id empty

Closed 4-value :class:`DominantSignal`:

  BELIEF_PRESSURE  belief_score >> fragility_score
  DOLL_FRAGILITY   fragility_score >> belief_score
  COMBINED         both sources fire ~equally
  NONE             HEALTHY verdict — no dominant stress

§33.1 cognitive substrate
``JARVIS_ANTI_FRAGILITY_BUDGET_ENABLED`` default-**FALSE** —
operator-paced opt-in. Sub-flag
``JARVIS_ANTI_FRAGILITY_PERSIST_ENABLED`` gates §33.4 writes
(default TRUE).

Authority asymmetry (AST-pinned): imports stdlib only at
module-load. ``belief_revision_ledger`` /
``second_order_doll_metric`` / ``governance_boundary_gate`` /
``cross_process_jsonl`` are all lazy-imported behind composer
helpers. Does NOT import orchestrator / iron_gate / policy /
providers / candidate_generator / urgency_router /
change_engine / semantic_guardian / auto_committer /
risk_tier_floor / sensor_governor.

The substrate is **advisory** — it surfaces budgets; SensorGovernor
or risk_tier_floor consumer-side wiring is out of scope (one-way
cage: budget is the published surface, not a controller).
"""
from __future__ import annotations

import ast
import enum
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any,
    Dict,
    FrozenSet,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

logger = logging.getLogger(__name__)


ANTI_FRAGILITY_SCHEMA_VERSION: str = "anti_fragility.1"


# ===========================================================================
# Env knobs
# ===========================================================================


_ENV_MASTER = "JARVIS_ANTI_FRAGILITY_BUDGET_ENABLED"
_ENV_PERSIST = "JARVIS_ANTI_FRAGILITY_PERSIST_ENABLED"
_ENV_STRESSED_THRESHOLD = "JARVIS_ANTI_FRAGILITY_STRESSED_THRESHOLD"
_ENV_EXHAUSTED_THRESHOLD = "JARVIS_ANTI_FRAGILITY_EXHAUSTED_THRESHOLD"
_ENV_MAX_BUDGET = "JARVIS_ANTI_FRAGILITY_MAX_BUDGET"
_ENV_STRESSED_DIVISOR = "JARVIS_ANTI_FRAGILITY_STRESSED_DIVISOR"
_ENV_BELIEF_WEIGHT = "JARVIS_ANTI_FRAGILITY_BELIEF_WEIGHT"
_ENV_FRAGILITY_WEIGHT = "JARVIS_ANTI_FRAGILITY_FRAGILITY_WEIGHT"
_ENV_LEDGER_PATH = "JARVIS_ANTI_FRAGILITY_LEDGER_PATH"

_DEFAULT_STRESSED_THRESHOLD = 0.25
_DEFAULT_EXHAUSTED_THRESHOLD = 0.50
_DEFAULT_MAX_BUDGET = 100
_DEFAULT_STRESSED_DIVISOR = 4  # STRESSED → max // 4 remaining
_DEFAULT_BELIEF_WEIGHT = 1.0
_DEFAULT_FRAGILITY_WEIGHT = 0.5  # fragility weighted half

_DEFAULT_LEDGER_REL = ".jarvis/anti_fragility_ledger.jsonl"

_TRUTHY: FrozenSet[str] = frozenset({"1", "true", "yes", "on"})


def _flag(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


def master_enabled() -> bool:
    """§33.1 cognitive substrate — default-FALSE.

    Operator-paced opt-in. Returns DISABLED verdict / full
    budget passthrough when off. Flip
    ``JARVIS_ANTI_FRAGILITY_BUDGET_ENABLED=true`` to enable
    per-module stress evaluation.
    """
    return _flag(_ENV_MASTER, default=False)


def persistence_enabled() -> bool:
    """Sub-flag — gate §33.4 JSONL writes. Default TRUE."""
    return _flag(_ENV_PERSIST, default=True)


def _read_clamped_int(
    name: str, default: int, lo: int, hi: int,
) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _read_clamped_float(
    name: str, default: float, lo: float, hi: float,
) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def stressed_threshold() -> float:
    """Stress-score threshold for HEALTHY → STRESSED transition.
    Defaults to 0.25. Clamped to [0.0, 1.0]."""
    return _read_clamped_float(
        _ENV_STRESSED_THRESHOLD,
        _DEFAULT_STRESSED_THRESHOLD,
        0.0, 1.0,
    )


def exhausted_threshold() -> float:
    """Stress-score threshold for STRESSED → EXHAUSTED. Defaults
    to 0.50. Auto-clamped to ≥ stressed_threshold so we never
    have an empty STRESSED band."""
    raw = _read_clamped_float(
        _ENV_EXHAUSTED_THRESHOLD,
        _DEFAULT_EXHAUSTED_THRESHOLD,
        0.0, 1.0,
    )
    return max(raw, stressed_threshold())


def max_budget() -> int:
    """Per-module total budget when HEALTHY. Defaults to 100.
    Clamped to [1, 1_000_000]."""
    return _read_clamped_int(
        _ENV_MAX_BUDGET,
        _DEFAULT_MAX_BUDGET,
        1, 1_000_000,
    )


def stressed_divisor() -> int:
    """Divisor applied to ``max_budget`` when STRESSED.
    Defaults to 4 (STRESSED → max // 4 remaining). Clamped to
    [1, 1000]."""
    return _read_clamped_int(
        _ENV_STRESSED_DIVISOR,
        _DEFAULT_STRESSED_DIVISOR,
        1, 1000,
    )


def belief_weight() -> float:
    """Weight of belief-pressure component in combined stress
    score. Defaults to 1.0. Clamped to [0.0, 10.0]."""
    return _read_clamped_float(
        _ENV_BELIEF_WEIGHT,
        _DEFAULT_BELIEF_WEIGHT,
        0.0, 10.0,
    )


def fragility_weight() -> float:
    """Weight of doll-fragility component in combined stress
    score. Defaults to 0.5 (fragility weighted half so the
    high-signal belief source dominates). Clamped to
    [0.0, 10.0]."""
    return _read_clamped_float(
        _ENV_FRAGILITY_WEIGHT,
        _DEFAULT_FRAGILITY_WEIGHT,
        0.0, 10.0,
    )


def ledger_path() -> Path:
    """Audit-ledger path. Defaults to
    ``.jarvis/anti_fragility_ledger.jsonl``. Operator may
    override via ``JARVIS_ANTI_FRAGILITY_LEDGER_PATH``."""
    raw = os.environ.get(_ENV_LEDGER_PATH, "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(_DEFAULT_LEDGER_REL)


# ===========================================================================
# Closed taxonomies
# ===========================================================================


class StressVerdict(str, enum.Enum):
    """Closed 4-value top-level verdict — bytes-pinned via AST."""

    HEALTHY = "healthy"
    STRESSED = "stressed"
    EXHAUSTED = "exhausted"
    DISABLED = "disabled"


class DominantSignal(str, enum.Enum):
    """Closed 4-value signal-source attribution — bytes-pinned."""

    BELIEF_PRESSURE = "belief_pressure"
    DOLL_FRAGILITY = "doll_fragility"
    COMBINED = "combined"
    NONE = "none"


_VERDICT_GLYPH: Dict[str, str] = {
    StressVerdict.HEALTHY.value: "✓",
    StressVerdict.STRESSED.value: "⚠",
    StressVerdict.EXHAUSTED.value: "🚫",
    StressVerdict.DISABLED.value: "◌",
}


_SIGNAL_GLYPH: Dict[str, str] = {
    DominantSignal.BELIEF_PRESSURE.value: "🧮",
    DominantSignal.DOLL_FRAGILITY.value: "🪆",
    DominantSignal.COMBINED.value: "⚡",
    DominantSignal.NONE.value: "·",
}


def verdict_glyph(verdict: object) -> str:
    """Public glyph accessor. NEVER raises."""
    try:
        if hasattr(verdict, "value"):
            return _VERDICT_GLYPH.get(str(verdict.value), "?")
        return _VERDICT_GLYPH.get(
            str(verdict or "").strip().lower(), "?",
        )
    except Exception:  # noqa: BLE001
        return "?"


def signal_glyph(signal: object) -> str:
    """Public glyph accessor. NEVER raises."""
    try:
        if hasattr(signal, "value"):
            return _SIGNAL_GLYPH.get(str(signal.value), "?")
        return _SIGNAL_GLYPH.get(
            str(signal or "").strip().lower(), "?",
        )
    except Exception:  # noqa: BLE001
        return "?"


# ===========================================================================
# §33.5 frozen versioned artifacts
# ===========================================================================


@dataclass(frozen=True)
class ModuleBudget:
    """Per-module stress + budget — frozen audit record."""

    module_id: str
    stress_score: float
    belief_pressure: float
    doll_fragility: float
    verdict: StressVerdict
    dominant_signal: DominantSignal
    remaining_budget: int
    max_budget: int
    falsified_domain_count: int
    matching_doll_stage: str
    boundary_crossed: bool
    diagnostic: str
    schema_version: str = ANTI_FRAGILITY_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "module_id": self.module_id[:128],
            "stress_score": float(self.stress_score),
            "belief_pressure": float(self.belief_pressure),
            "doll_fragility": float(self.doll_fragility),
            "verdict": self.verdict.value,
            "dominant_signal": self.dominant_signal.value,
            "remaining_budget": int(self.remaining_budget),
            "max_budget": int(self.max_budget),
            "falsified_domain_count": int(
                self.falsified_domain_count,
            ),
            "matching_doll_stage": self.matching_doll_stage[:32],
            "boundary_crossed": bool(self.boundary_crossed),
            "diagnostic": self.diagnostic[:512],
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class AntiFragilityReport:
    """Aggregate evaluation across N modules."""

    evaluated_at_unix: float
    master_enabled: bool
    per_module: Tuple[ModuleBudget, ...]
    healthy_count: int
    stressed_count: int
    exhausted_count: int
    diagnostic: str
    elapsed_s: float
    schema_version: str = ANTI_FRAGILITY_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "evaluated_at_unix": self.evaluated_at_unix,
            "master_enabled": self.master_enabled,
            "per_module": [m.to_dict() for m in self.per_module],
            "healthy_count": int(self.healthy_count),
            "stressed_count": int(self.stressed_count),
            "exhausted_count": int(self.exhausted_count),
            "diagnostic": self.diagnostic[:512],
            "elapsed_s": float(self.elapsed_s),
            "schema_version": self.schema_version,
        }


# ===========================================================================
# Composers — canonical surfaces (lazy-imported)
# ===========================================================================


def _load_falsified_belief_reports() -> Tuple[Any, ...]:
    """Compose Wave 4 #9. NEVER raises. Returns FALSIFIED-only
    reports."""
    try:
        from backend.core.ouroboros.governance.belief_revision_ledger import (  # noqa: E501
            BeliefVerdict,
            evaluate_recent_beliefs,
        )
    except ImportError:
        return ()
    try:
        reports = evaluate_recent_beliefs()
    except Exception:  # noqa: BLE001
        return ()
    out: List[Any] = []
    for r in reports:
        try:
            if r.verdict is BeliefVerdict.FALSIFIED:
                out.append(r)
        except Exception:  # noqa: BLE001
            continue
    return tuple(out)


def _load_doll_snapshot() -> Optional[Any]:
    """Compose Wave 1 #15. NEVER raises. Returns the snapshot
    or None when master is off / unavailable."""
    try:
        from backend.core.ouroboros.governance.second_order_doll_metric import (  # noqa: E501
            aggregate_doll_completion,
        )
    except ImportError:
        return None
    try:
        snap = aggregate_doll_completion()
    except Exception:  # noqa: BLE001
        return None
    try:
        if not getattr(snap, "master_enabled", False):
            return None
        return snap
    except Exception:  # noqa: BLE001
        return None


def _doll_stage_weight_for_category(
    snapshot: Any, module_id: str,
) -> Tuple[float, str]:
    """Look up the canonical _STAGE_WEIGHT for a Category-named
    module. Returns ``(fragility_score_0_to_1, stage_name)``
    where fragility = 1 - stage_weight (UNTOUCHED → most fragile,
    GRADUATED → least fragile). Returns (0.0, "") when no axis
    matches. NEVER raises."""
    if snapshot is None:
        return 0.0, ""
    try:
        from backend.core.ouroboros.governance.second_order_doll_metric import (  # noqa: E501
            _STAGE_WEIGHT,
        )
    except ImportError:
        return 0.0, ""
    try:
        axes = getattr(snapshot, "axes", ())
        target = module_id.strip().lower()
        for axis in axes:
            cat = str(getattr(axis, "category", "") or "").strip().lower()
            if cat == target:
                stage = getattr(axis, "stage", None)
                stage_value = (
                    getattr(stage, "value", "") if stage else ""
                )
                weight = _STAGE_WEIGHT.get(stage_value, 0.0)
                fragility = max(0.0, min(1.0, 1.0 - weight))
                return fragility, str(stage_value)
        return 0.0, ""
    except Exception:  # noqa: BLE001
        return 0.0, ""


def _is_boundary_crossed(module_id: str) -> bool:
    """Compose Wave 2 #5. NEVER raises."""
    if not module_id:
        return False
    try:
        from backend.core.ouroboros.governance.governance_boundary_gate import (  # noqa: E501
            is_boundary_crossed,
        )
        return bool(is_boundary_crossed((module_id,)))
    except Exception:  # noqa: BLE001
        return False


def _flock_append(payload: Mapping[str, Any]) -> bool:
    """Best-effort §33.4 write. NEVER raises."""
    if not master_enabled() or not persistence_enabled():
        return False
    try:
        from backend.core.ouroboros.governance.cross_process_jsonl import (  # noqa: E501
            flock_append_line,
        )
    except ImportError:
        return False
    try:
        target = ledger_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        flock_append_line(target, json.dumps(dict(payload)))
        return True
    except Exception:  # noqa: BLE001
        return False


# ===========================================================================
# Pure pressure aggregation
# ===========================================================================


def _belief_pressure_for_module(
    module_id: str,
    falsified_reports: Sequence[Any],
) -> Tuple[float, int]:
    """Pure aggregation. Returns ``(pressure_score_0_to_1,
    matching_domain_count)``. NEVER raises.

    Algorithm: count FALSIFIED reports whose ``claim.domain``
    OR any ``claim.target_files`` entry contains module_id
    (case-insensitive substring). Pressure score is
    ``min(1.0, matching / max(total_falsified, 1) * 2.0)``
    so even modest matching shares show signal.
    """
    if not module_id:
        return 0.0, 0
    target = module_id.strip().lower()
    if not target:
        return 0.0, 0
    matching = 0
    total = 0
    for r in falsified_reports:
        try:
            total += 1
            claim = getattr(r, "claim", None)
            if claim is None:
                continue
            domain = str(getattr(claim, "domain", "") or "").lower()
            files = getattr(claim, "target_files", ()) or ()
            if target in domain:
                matching += 1
                continue
            for f in files:
                if target in str(f or "").lower():
                    matching += 1
                    break
        except Exception:  # noqa: BLE001
            continue
    if total <= 0:
        return 0.0, matching
    # Scale: a module whose share of falsified beliefs is >= 0.5
    # gets pressure score 1.0; share 0.0 gets 0.0; linear in between
    # with 2x amplification so even minor signal surfaces.
    pressure = min(1.0, (matching / total) * 2.0)
    return pressure, matching


def _classify_dominant_signal(
    belief_score: float, fragility_score: float,
) -> DominantSignal:
    """Pure classifier. NEVER raises."""
    # Tolerance for "combined" classification.
    eps = 0.10
    if belief_score < eps and fragility_score < eps:
        return DominantSignal.NONE
    if abs(belief_score - fragility_score) < eps:
        return DominantSignal.COMBINED
    if belief_score > fragility_score:
        return DominantSignal.BELIEF_PRESSURE
    return DominantSignal.DOLL_FRAGILITY


def _budget_for_verdict(
    verdict: StressVerdict, max_b: int, divisor: int,
) -> int:
    """Pure budget allocator. NEVER raises."""
    if verdict is StressVerdict.HEALTHY:
        return max_b
    if verdict is StressVerdict.STRESSED:
        return max(1, max_b // max(1, divisor))
    if verdict is StressVerdict.EXHAUSTED:
        return 0
    # DISABLED → passthrough = full budget
    return max_b


# ===========================================================================
# Top-level evaluator
# ===========================================================================


def evaluate_module(
    module_id: str,
    *,
    falsified_reports: Optional[Sequence[Any]] = None,
    doll_snapshot: Optional[Any] = None,
    now_unix: Optional[float] = None,
) -> ModuleBudget:
    """Pure per-module evaluator. NEVER raises.

    Parameters
    ----------
    module_id:
        Caller-supplied module identifier. Empty / None → DISABLED.
    falsified_reports:
        Caller-injectable (testing seam). Defaults to canonical
        Wave 4 #9 ``evaluate_recent_beliefs`` filtered to
        FALSIFIED.
    doll_snapshot:
        Caller-injectable (testing seam). Defaults to canonical
        Wave 1 #15 ``aggregate_doll_completion``.
    """
    mid = str(module_id or "").strip()
    started = time.time() if now_unix is None else float(now_unix)
    max_b = max_budget()
    divisor = stressed_divisor()

    if not master_enabled() or not mid:
        return ModuleBudget(
            module_id=mid,
            stress_score=0.0,
            belief_pressure=0.0,
            doll_fragility=0.0,
            verdict=StressVerdict.DISABLED,
            dominant_signal=DominantSignal.NONE,
            remaining_budget=_budget_for_verdict(
                StressVerdict.DISABLED, max_b, divisor,
            ),
            max_budget=max_b,
            falsified_domain_count=0,
            matching_doll_stage="",
            boundary_crossed=False,
            diagnostic=(
                f"gate disabled via {_ENV_MASTER}=false"
                if not master_enabled()
                else "empty module_id"
            ),
        )

    falsified = (
        falsified_reports
        if falsified_reports is not None
        else _load_falsified_belief_reports()
    )
    snapshot = (
        doll_snapshot
        if doll_snapshot is not None
        else _load_doll_snapshot()
    )

    belief, matching = _belief_pressure_for_module(mid, falsified)
    fragility, stage = _doll_stage_weight_for_category(snapshot, mid)
    boundary = _is_boundary_crossed(mid)

    # Combined stress score = weighted average. Both weights are
    # operator-tunable; defaults give belief 2x leverage over
    # fragility (1.0 vs 0.5).
    bw = belief_weight()
    fw = fragility_weight()
    if (bw + fw) > 0:
        combined = (belief * bw + fragility * fw) / (bw + fw)
    else:
        combined = 0.0
    stress = max(0.0, min(1.0, combined))

    s_thresh = stressed_threshold()
    e_thresh = exhausted_threshold()
    if stress >= e_thresh:
        verdict = StressVerdict.EXHAUSTED
    elif stress >= s_thresh:
        verdict = StressVerdict.STRESSED
    else:
        verdict = StressVerdict.HEALTHY

    if verdict is StressVerdict.HEALTHY:
        signal = DominantSignal.NONE
    else:
        signal = _classify_dominant_signal(belief, fragility)

    budget = _budget_for_verdict(verdict, max_b, divisor)

    diagnostic = (
        f"stress={stress:.2f} "
        f"(belief={belief:.2f} matching={matching}, "
        f"fragility={fragility:.2f} stage={stage or 'n/a'}); "
        f"verdict={verdict.value} budget={budget}/{max_b}; "
        f"signal={signal.value}"
        + (" [cage]" if boundary else "")
    )

    return ModuleBudget(
        module_id=mid,
        stress_score=stress,
        belief_pressure=belief,
        doll_fragility=fragility,
        verdict=verdict,
        dominant_signal=signal,
        remaining_budget=budget,
        max_budget=max_b,
        falsified_domain_count=matching,
        matching_doll_stage=stage,
        boundary_crossed=boundary,
        diagnostic=diagnostic,
    )


def evaluate_modules(
    module_ids: Sequence[str],
    *,
    falsified_reports: Optional[Sequence[Any]] = None,
    doll_snapshot: Optional[Any] = None,
    now_unix: Optional[float] = None,
) -> AntiFragilityReport:
    """Top-level aggregator. NEVER raises. Returns one
    :class:`ModuleBudget` per supplied module_id (skipping
    empty / None entries)."""
    started = time.time() if now_unix is None else float(now_unix)

    if not master_enabled():
        return AntiFragilityReport(
            evaluated_at_unix=started,
            master_enabled=False,
            per_module=(),
            healthy_count=0,
            stressed_count=0,
            exhausted_count=0,
            diagnostic=(
                f"gate disabled via {_ENV_MASTER}=false"
            ),
            elapsed_s=0.0,
        )

    # Load canonical sources ONCE for the whole batch — avoids
    # N×eval cost for N modules.
    falsified = (
        falsified_reports
        if falsified_reports is not None
        else _load_falsified_belief_reports()
    )
    snapshot = (
        doll_snapshot
        if doll_snapshot is not None
        else _load_doll_snapshot()
    )

    per_module: List[ModuleBudget] = []
    for mid in module_ids:
        try:
            cleaned = str(mid or "").strip()
        except Exception:  # noqa: BLE001
            continue
        if not cleaned:
            continue
        per_module.append(
            evaluate_module(
                cleaned,
                falsified_reports=falsified,
                doll_snapshot=snapshot,
                now_unix=started,
            ),
        )

    healthy = sum(
        1 for m in per_module if m.verdict is StressVerdict.HEALTHY
    )
    stressed = sum(
        1 for m in per_module if m.verdict is StressVerdict.STRESSED
    )
    exhausted = sum(
        1 for m in per_module if m.verdict is StressVerdict.EXHAUSTED
    )

    diagnostic = (
        f"{len(per_module)} module(s) evaluated: "
        f"healthy={healthy} stressed={stressed} "
        f"exhausted={exhausted}"
    )

    report = AntiFragilityReport(
        evaluated_at_unix=started,
        master_enabled=True,
        per_module=tuple(per_module),
        healthy_count=healthy,
        stressed_count=stressed,
        exhausted_count=exhausted,
        diagnostic=diagnostic,
        elapsed_s=max(0.0, time.time() - started),
    )
    _persist_report(report)
    _publish_evaluation_event(report)
    return report


# ===========================================================================
# §33.4 persistence
# ===========================================================================


def _persist_report(report: AntiFragilityReport) -> None:
    """Best-effort write of summary + per-module rows. NEVER
    raises. Skips when master/persist off or when all modules
    are HEALTHY (no stress to record)."""
    if report.stressed_count == 0 and report.exhausted_count == 0:
        return
    _flock_append({"kind": "summary", "payload": report.to_dict()})
    for m in report.per_module:
        if m.verdict in (
            StressVerdict.STRESSED, StressVerdict.EXHAUSTED,
        ):
            _flock_append(
                {"kind": "module", "payload": m.to_dict()},
            )


# ===========================================================================
# SSE publisher
# ===========================================================================


def _publish_evaluation_event(report: AntiFragilityReport) -> None:
    """Best-effort SSE publish. NEVER raises. Fires only when at
    least one module is non-HEALTHY."""
    if not master_enabled():
        return
    if report.stressed_count == 0 and report.exhausted_count == 0:
        return
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            EVENT_TYPE_ANTI_FRAGILITY_EVALUATED,
            publish_task_event,
        )
        publish_task_event(
            EVENT_TYPE_ANTI_FRAGILITY_EVALUATED,
            (
                f"system::anti_fragility::"
                f"{report.schema_version}"
            ),
            {
                "module_count": len(report.per_module),
                "healthy_count": report.healthy_count,
                "stressed_count": report.stressed_count,
                "exhausted_count": report.exhausted_count,
                "evaluated_at_unix": report.evaluated_at_unix,
                "elapsed_s": report.elapsed_s,
                "schema_version": report.schema_version,
            },
        )
    except Exception:  # noqa: BLE001
        return


# ===========================================================================
# Renderer
# ===========================================================================


def format_anti_fragility_panel(
    report: Optional[AntiFragilityReport] = None,
) -> str:
    """Operator-facing panel. NEVER raises."""
    if report is None:
        if not master_enabled():
            return (
                f"anti-fragility budget: disabled "
                f"({_ENV_MASTER}=false)"
            )
        return "anti-fragility budget: no report"
    if not report.master_enabled:
        return (
            f"anti-fragility budget: disabled "
            f"({_ENV_MASTER}=false)"
        )
    lines = [
        f"🛡 Anti-Fragility Budget  "
        f"healthy={report.healthy_count} "
        f"stressed={report.stressed_count} "
        f"exhausted={report.exhausted_count}",
    ]
    if report.per_module:
        for m in report.per_module[:10]:
            vg = verdict_glyph(m.verdict)
            sg = signal_glyph(m.dominant_signal)
            lines.append(
                f"  {vg} {m.module_id[:24]:<24} "
                f"{m.verdict.value:<10} stress={m.stress_score:.2f} "
                f"budget={m.remaining_budget}/{m.max_budget} "
                f"{sg} {m.dominant_signal.value}"
            )
        if len(report.per_module) > 10:
            lines.append(
                f"  ... (+{len(report.per_module) - 10} more)"
            )
    lines.append(f"  diagnostic: {report.diagnostic}")
    return "\n".join(lines)


# ===========================================================================
# AST pins
# ===========================================================================


def register_shipped_invariants() -> list:
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/"
        "anti_fragility_budget.py"
    )

    _EXPECTED_VERDICTS = {
        "healthy", "stressed", "exhausted", "disabled",
    }
    _EXPECTED_SIGNALS = {
        "belief_pressure", "doll_fragility", "combined", "none",
    }

    def _validate_verdict_taxonomy(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "StressVerdict"
            ):
                found = set()
                for sub in node.body:
                    if (
                        isinstance(sub, ast.Assign)
                        and len(sub.targets) == 1
                        and isinstance(sub.targets[0], ast.Name)
                        and isinstance(sub.value, ast.Constant)
                        and isinstance(sub.value.value, str)
                    ):
                        found.add(sub.value.value)
                missing = _EXPECTED_VERDICTS - found
                extra = found - _EXPECTED_VERDICTS
                if missing:
                    return (
                        f"StressVerdict missing: "
                        f"{sorted(missing)}",
                    )
                if extra:
                    return (
                        f"StressVerdict drift: {sorted(extra)}",
                    )
                return ()
        return ("StressVerdict class not found",)

    def _validate_signal_taxonomy(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "DominantSignal"
            ):
                found = set()
                for sub in node.body:
                    if (
                        isinstance(sub, ast.Assign)
                        and len(sub.targets) == 1
                        and isinstance(sub.targets[0], ast.Name)
                        and isinstance(sub.value, ast.Constant)
                        and isinstance(sub.value.value, str)
                    ):
                        found.add(sub.value.value)
                missing = _EXPECTED_SIGNALS - found
                extra = found - _EXPECTED_SIGNALS
                if missing:
                    return (
                        f"DominantSignal missing: "
                        f"{sorted(missing)}",
                    )
                if extra:
                    return (
                        f"DominantSignal drift: {sorted(extra)}",
                    )
                return ()
        return ("DominantSignal class not found",)

    def _validate_authority_asymmetry(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        forbidden = (
            "backend.core.ouroboros.governance.orchestrator",
            "backend.core.ouroboros.governance.iron_gate",
            "backend.core.ouroboros.governance.policy",
            "backend.core.ouroboros.governance.providers",
            "backend.core.ouroboros.governance.candidate_generator",
            "backend.core.ouroboros.governance.urgency_router",
            "backend.core.ouroboros.governance.change_engine",
            "backend.core.ouroboros.governance.semantic_guardian",
            "backend.core.ouroboros.governance.auto_committer",
            "backend.core.ouroboros.governance.risk_tier_floor",
            "backend.core.ouroboros.governance.sensor_governor",
        )
        violations: List[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if any(mod == f for f in forbidden):
                    violations.append(
                        f"forbidden authority import: {mod}",
                    )
        return tuple(violations)

    def _validate_master_default_false(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "master_enabled"
            ):
                for sub in ast.walk(node):
                    if (
                        isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Name)
                        and sub.func.id == "_flag"
                    ):
                        for kw in sub.keywords:
                            if (
                                kw.arg == "default"
                                and isinstance(kw.value, ast.Constant)
                                and kw.value.value is False
                            ):
                                return ()
                return (
                    "master_enabled() must call _flag(...) "
                    "with default=False per §33.1",
                )
        return ("master_enabled() not found",)

    def _validate_composes_canonical(
        tree: ast.AST, source: str,
    ) -> tuple:
        violations: List[str] = []
        if "belief_revision_ledger" not in source:
            violations.append(
                "must compose Wave 4 #9 belief_revision_ledger "
                "(belief-pressure source)",
            )
        if "second_order_doll_metric" not in source:
            violations.append(
                "must compose Wave 1 #15 "
                "second_order_doll_metric (fragility source)",
            )
        if "governance_boundary_gate" not in source:
            violations.append(
                "must compose Wave 2 #5 "
                "governance_boundary_gate (cage flag)",
            )
        if "cross_process_jsonl" not in source:
            violations.append(
                "must compose canonical cross_process_jsonl "
                "(§33.4 ledger)",
            )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "anti_fragility_verdict_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "StressVerdict 4-value taxonomy bytes-pinned."
            ),
            validate=_validate_verdict_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "anti_fragility_signal_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "DominantSignal 4-value taxonomy bytes-pinned."
            ),
            validate=_validate_signal_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "anti_fragility_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "Substrate purity — advisory evaluator. MUST "
                "NOT import orchestrator / iron_gate / policy "
                "/ providers / candidate_generator / "
                "urgency_router / change_engine / "
                "semantic_guardian / auto_committer / "
                "risk_tier_floor / sensor_governor (one-way "
                "cage: budget is the published surface, not a "
                "controller)."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "anti_fragility_master_default_false"
            ),
            target_file=target,
            description=(
                "§33.1 cognitive substrate default-FALSE."
            ),
            validate=_validate_master_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "anti_fragility_composes_canonical"
            ),
            target_file=target,
            description=(
                "Substrate composes Wave 4 #9 "
                "belief_revision_ledger + Wave 1 #15 "
                "second_order_doll_metric + Wave 2 #5 "
                "governance_boundary_gate + canonical "
                "cross_process_jsonl — no parallel "
                "implementations."
            ),
            validate=_validate_composes_canonical,
        ),
    ]


# ===========================================================================
# FlagRegistry seeds
# ===========================================================================


def register_flags(registry: Any) -> int:
    from backend.core.ouroboros.governance.flag_registry import (
        Category,
        FlagSpec,
        FlagType,
    )

    src = (
        "backend/core/ouroboros/governance/"
        "anti_fragility_budget.py"
    )

    seeds = [
        FlagSpec(
            name=_ENV_MASTER,
            type=FlagType.BOOL,
            default=False,
            description=(
                "Anti-fragility budget master switch. §33.1 "
                "cognitive substrate default-FALSE. When on, "
                "the substrate computes per-module stress + "
                "budget from Wave 4 #9 belief-pressure + Wave "
                "1 #15 doll-fragility. Closes §40 Wave 4 #13 "
                "(PRD v2.99+). Advisory only — consumer-side "
                "throttling (SensorGovernor / risk_tier_floor) "
                "stays out of scope."
            ),
            category=Category.SAFETY,
            source_file=src,
            example=f"{_ENV_MASTER}=true",
        ),
        FlagSpec(
            name=_ENV_PERSIST,
            type=FlagType.BOOL,
            default=True,
            description=(
                "Sub-flag — gate §33.4 JSONL audit writes."
            ),
            category=Category.SAFETY,
            source_file=src,
            example=f"{_ENV_PERSIST}=false",
        ),
        FlagSpec(
            name=_ENV_STRESSED_THRESHOLD,
            type=FlagType.FLOAT,
            default=_DEFAULT_STRESSED_THRESHOLD,
            description=(
                "Stress-score threshold for HEALTHY → STRESSED "
                "transition. Defaults to 0.25. Clamped to "
                "[0.0, 1.0]."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_STRESSED_THRESHOLD}=0.30",
        ),
        FlagSpec(
            name=_ENV_EXHAUSTED_THRESHOLD,
            type=FlagType.FLOAT,
            default=_DEFAULT_EXHAUSTED_THRESHOLD,
            description=(
                "Stress-score threshold for STRESSED → "
                "EXHAUSTED transition. Defaults to 0.50. "
                "Auto-clamped to ≥ stressed_threshold."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_EXHAUSTED_THRESHOLD}=0.75",
        ),
        FlagSpec(
            name=_ENV_MAX_BUDGET,
            type=FlagType.INT,
            default=_DEFAULT_MAX_BUDGET,
            description=(
                "Per-module total budget when HEALTHY. "
                "Defaults to 100. Clamped to [1, 1_000_000]."
            ),
            category=Category.CAPACITY,
            source_file=src,
            example=f"{_ENV_MAX_BUDGET}=200",
        ),
        FlagSpec(
            name=_ENV_STRESSED_DIVISOR,
            type=FlagType.INT,
            default=_DEFAULT_STRESSED_DIVISOR,
            description=(
                "Divisor applied to max_budget when STRESSED. "
                "Defaults to 4 (STRESSED → max // 4 remaining). "
                "Clamped to [1, 1000]."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_STRESSED_DIVISOR}=2",
        ),
        FlagSpec(
            name=_ENV_BELIEF_WEIGHT,
            type=FlagType.FLOAT,
            default=_DEFAULT_BELIEF_WEIGHT,
            description=(
                "Weight of belief-pressure component in "
                "combined stress score. Defaults to 1.0."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_BELIEF_WEIGHT}=1.5",
        ),
        FlagSpec(
            name=_ENV_FRAGILITY_WEIGHT,
            type=FlagType.FLOAT,
            default=_DEFAULT_FRAGILITY_WEIGHT,
            description=(
                "Weight of doll-fragility component in "
                "combined stress score. Defaults to 0.5 "
                "(fragility weighted half so high-signal "
                "belief source dominates)."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_FRAGILITY_WEIGHT}=1.0",
        ),
    ]

    count = 0
    for spec in seeds:
        try:
            registry.register(spec)
            count += 1
        except Exception:  # noqa: BLE001
            continue
    return count


__all__ = [
    "ANTI_FRAGILITY_SCHEMA_VERSION",
    "StressVerdict",
    "DominantSignal",
    "ModuleBudget",
    "AntiFragilityReport",
    "master_enabled",
    "persistence_enabled",
    "stressed_threshold",
    "exhausted_threshold",
    "max_budget",
    "stressed_divisor",
    "belief_weight",
    "fragility_weight",
    "ledger_path",
    "verdict_glyph",
    "signal_glyph",
    "evaluate_module",
    "evaluate_modules",
    "format_anti_fragility_panel",
    "register_shipped_invariants",
    "register_flags",
]
