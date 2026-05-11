"""
Predictive Postmortem
=====================

Closes §40 Wave 5 #18. Per the operator binding:

  "Predict failure likelihood BEFORE the postmortem fires.
   Compose belief-drift + meta-recurrence + calibration-decay
   into a forward-looking risk forecast."

Pure-function forecaster over three Wave 4 signal sources:

* Wave 4 #9 ``belief_revision_ledger`` — count of FALSIFIED
  reports → ``belief_drift_score``.
* Wave 4 #11 ``postmortem_fusion`` — count of FUSED meta-
  postmortems → ``meta_recurrence_score``.
* Wave 4 #14 ``mirror_self_test`` — accuracy on OUTCOME
  dimension → ``calibration_decay_score``.

The three component scores combine via operator-tunable
weights into a single 0..1 ``forecast_score``. Closed 4-value
:class:`ForecastVerdict` (LOW / MODERATE / HIGH / CRITICAL)
indexes risk band; closed 4-value :class:`RiskFactor`
(BELIEF_DRIFT / META_RECURRENCE / CALIBRATION_DECAY / NONE)
attributes the dominant signal.

Deterministic — same Wave 4 signal corpus → same forecast.
Zero LLM. Forecast is *surfaced*; consumer-side action (raising
risk_tier, throttling dispatch) stays operator-paced.

§33.1 ``JARVIS_PREDICTIVE_POSTMORTEM_ENABLED`` default-FALSE.

Authority asymmetry: imports stdlib + lazy-imported Wave 4
substrates + cross_process_jsonl + governance_boundary_gate.
"""
from __future__ import annotations

import ast
import enum
import json
import logging
import os
import time
from dataclasses import dataclass
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


PREDICTIVE_POSTMORTEM_SCHEMA_VERSION: str = "predictive_postmortem.1"


_ENV_MASTER = "JARVIS_PREDICTIVE_POSTMORTEM_ENABLED"
_ENV_PERSIST = "JARVIS_PREDICTIVE_POSTMORTEM_PERSIST_ENABLED"
_ENV_BELIEF_WEIGHT = "JARVIS_PREDICTIVE_POSTMORTEM_BELIEF_WEIGHT"
_ENV_META_WEIGHT = "JARVIS_PREDICTIVE_POSTMORTEM_META_WEIGHT"
_ENV_CALIB_WEIGHT = "JARVIS_PREDICTIVE_POSTMORTEM_CALIB_WEIGHT"
_ENV_MODERATE_THRESHOLD = (
    "JARVIS_PREDICTIVE_POSTMORTEM_MODERATE_THRESHOLD"
)
_ENV_HIGH_THRESHOLD = (
    "JARVIS_PREDICTIVE_POSTMORTEM_HIGH_THRESHOLD"
)
_ENV_CRITICAL_THRESHOLD = (
    "JARVIS_PREDICTIVE_POSTMORTEM_CRITICAL_THRESHOLD"
)
_ENV_LEDGER_PATH = "JARVIS_PREDICTIVE_POSTMORTEM_LEDGER_PATH"

_DEFAULT_BELIEF_WEIGHT = 1.0
_DEFAULT_META_WEIGHT = 1.5  # recurrence is louder signal
_DEFAULT_CALIB_WEIGHT = 1.0
_DEFAULT_MODERATE = 0.25
_DEFAULT_HIGH = 0.50
_DEFAULT_CRITICAL = 0.75

_DEFAULT_LEDGER_REL = ".jarvis/predictive_postmortem_ledger.jsonl"

_TRUTHY: FrozenSet[str] = frozenset({"1", "true", "yes", "on"})


def _flag(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


def master_enabled() -> bool:
    """§33.1 cognitive substrate — default-FALSE."""
    return _flag(_ENV_MASTER, default=False)


def persistence_enabled() -> bool:
    return _flag(_ENV_PERSIST, default=True)


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


def belief_weight() -> float:
    return _read_clamped_float(
        _ENV_BELIEF_WEIGHT, _DEFAULT_BELIEF_WEIGHT, 0.0, 10.0,
    )


def meta_weight() -> float:
    return _read_clamped_float(
        _ENV_META_WEIGHT, _DEFAULT_META_WEIGHT, 0.0, 10.0,
    )


def calibration_weight() -> float:
    return _read_clamped_float(
        _ENV_CALIB_WEIGHT, _DEFAULT_CALIB_WEIGHT, 0.0, 10.0,
    )


def moderate_threshold() -> float:
    return _read_clamped_float(
        _ENV_MODERATE_THRESHOLD, _DEFAULT_MODERATE, 0.0, 1.0,
    )


def high_threshold() -> float:
    raw = _read_clamped_float(
        _ENV_HIGH_THRESHOLD, _DEFAULT_HIGH, 0.0, 1.0,
    )
    return max(raw, moderate_threshold())


def critical_threshold() -> float:
    raw = _read_clamped_float(
        _ENV_CRITICAL_THRESHOLD, _DEFAULT_CRITICAL, 0.0, 1.0,
    )
    return max(raw, high_threshold())


def ledger_path() -> Path:
    raw = os.environ.get(_ENV_LEDGER_PATH, "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(_DEFAULT_LEDGER_REL)


# Closed taxonomies


class ForecastVerdict(str, enum.Enum):
    """Closed 4-value verdict — bytes-pinned via AST."""

    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"
    CRITICAL = "critical"


class RiskFactor(str, enum.Enum):
    """Closed 4-value factor — bytes-pinned via AST."""

    BELIEF_DRIFT = "belief_drift"
    META_RECURRENCE = "meta_recurrence"
    CALIBRATION_DECAY = "calibration_decay"
    NONE = "none"


_VERDICT_GLYPH: Dict[str, str] = {
    ForecastVerdict.LOW.value: "✓",
    ForecastVerdict.MODERATE.value: "⚠",
    ForecastVerdict.HIGH.value: "🔥",
    ForecastVerdict.CRITICAL.value: "💀",
}


_FACTOR_GLYPH: Dict[str, str] = {
    RiskFactor.BELIEF_DRIFT.value: "🧮",
    RiskFactor.META_RECURRENCE.value: "🧬",
    RiskFactor.CALIBRATION_DECAY.value: "🪞",
    RiskFactor.NONE.value: "·",
}


def verdict_glyph(verdict: object) -> str:
    try:
        if hasattr(verdict, "value"):
            return _VERDICT_GLYPH.get(str(verdict.value), "?")
        return _VERDICT_GLYPH.get(
            str(verdict or "").strip().lower(), "?",
        )
    except Exception:  # noqa: BLE001
        return "?"


def factor_glyph(factor: object) -> str:
    try:
        if hasattr(factor, "value"):
            return _FACTOR_GLYPH.get(str(factor.value), "?")
        return _FACTOR_GLYPH.get(
            str(factor or "").strip().lower(), "?",
        )
    except Exception:  # noqa: BLE001
        return "?"


# §33.5 frozen artifacts


@dataclass(frozen=True)
class RiskForecast:
    """Aggregate forecast report."""

    evaluated_at_unix: float
    master_enabled: bool
    forecast_score: float
    belief_drift_score: float
    meta_recurrence_score: float
    calibration_decay_score: float
    verdict: ForecastVerdict
    dominant_factor: RiskFactor
    falsified_belief_count: int
    fused_meta_count: int
    outcome_accuracy: float
    diagnostic: str
    elapsed_s: float
    schema_version: str = PREDICTIVE_POSTMORTEM_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "evaluated_at_unix": self.evaluated_at_unix,
            "master_enabled": self.master_enabled,
            "forecast_score": float(self.forecast_score),
            "belief_drift_score": float(self.belief_drift_score),
            "meta_recurrence_score": float(
                self.meta_recurrence_score,
            ),
            "calibration_decay_score": float(
                self.calibration_decay_score,
            ),
            "verdict": self.verdict.value,
            "dominant_factor": self.dominant_factor.value,
            "falsified_belief_count": int(
                self.falsified_belief_count,
            ),
            "fused_meta_count": int(self.fused_meta_count),
            "outcome_accuracy": float(self.outcome_accuracy),
            "diagnostic": self.diagnostic[:512],
            "elapsed_s": float(self.elapsed_s),
            "schema_version": self.schema_version,
        }


# Composers


def _load_belief_drift() -> Tuple[float, int]:
    """Returns (drift_score, falsified_count). NEVER raises.

    drift_score = min(1.0, falsified_count / 10.0) — saturates
    at 10 falsified beliefs."""
    try:
        from backend.core.ouroboros.governance.belief_revision_ledger import (  # noqa: E501
            BeliefVerdict,
            evaluate_recent_beliefs,
        )
    except ImportError:
        return 0.0, 0
    try:
        reports = evaluate_recent_beliefs()
    except Exception:  # noqa: BLE001
        return 0.0, 0
    count = 0
    for r in reports:
        try:
            if r.verdict is BeliefVerdict.FALSIFIED:
                count += 1
        except Exception:  # noqa: BLE001
            continue
    return min(1.0, count / 10.0), count


def _load_meta_recurrence() -> Tuple[float, int]:
    """Returns (recurrence_score, fused_count). NEVER raises.

    recurrence_score = min(1.0, fused_count / 5.0) — saturates
    at 5 fused meta-postmortems."""
    try:
        from backend.core.ouroboros.governance.postmortem_fusion import (  # noqa: E501
            fuse_recent_postmortems,
        )
    except ImportError:
        return 0.0, 0
    try:
        report = fuse_recent_postmortems()
        fused = len(getattr(report, "meta_postmortems", ()) or ())
        return min(1.0, fused / 5.0), fused
    except Exception:  # noqa: BLE001
        return 0.0, 0


def _load_calibration_decay() -> Tuple[float, float]:
    """Returns (decay_score, outcome_accuracy). NEVER raises.

    decay_score = 1.0 - outcome_accuracy. When uncalibrated
    (no actuals recorded), returns 0.0 decay + 0.0 accuracy."""
    try:
        from backend.core.ouroboros.governance.mirror_self_test import (  # noqa: E501
            PredictionDimension,
            compute_calibration,
        )
    except ImportError:
        return 0.0, 0.0
    try:
        rep = compute_calibration(PredictionDimension.OUTCOME)
        accuracy = float(getattr(rep, "accuracy", 0.0) or 0.0)
        sample = int(getattr(rep, "sample_count", 0) or 0)
        if sample == 0:
            return 0.0, 0.0
        decay = max(0.0, 1.0 - accuracy)
        return decay, accuracy
    except Exception:  # noqa: BLE001
        return 0.0, 0.0


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


def _classify_factor(
    belief: float, meta: float, calib: float,
) -> RiskFactor:
    """Pure classifier. NEVER raises."""
    eps = 0.05
    if belief < eps and meta < eps and calib < eps:
        return RiskFactor.NONE
    # Pick whichever component is highest (with weights applied
    # only if they exceed others by tolerance).
    best = max(belief, meta, calib)
    if best == meta:
        return RiskFactor.META_RECURRENCE
    if best == belief:
        return RiskFactor.BELIEF_DRIFT
    return RiskFactor.CALIBRATION_DECAY


def _verdict_for_score(score: float) -> ForecastVerdict:
    if score >= critical_threshold():
        return ForecastVerdict.CRITICAL
    if score >= high_threshold():
        return ForecastVerdict.HIGH
    if score >= moderate_threshold():
        return ForecastVerdict.MODERATE
    return ForecastVerdict.LOW


def forecast_postmortem_risk(
    *,
    belief_drift_score: Optional[float] = None,
    meta_recurrence_score: Optional[float] = None,
    calibration_decay_score: Optional[float] = None,
    falsified_count: Optional[int] = None,
    fused_count: Optional[int] = None,
    outcome_accuracy: Optional[float] = None,
    now_unix: Optional[float] = None,
) -> RiskForecast:
    """Top-level forecaster. NEVER raises. All component scores
    are caller-injectable (testing seam); defaults compose the
    real Wave 4 substrates."""
    started = time.time() if now_unix is None else float(now_unix)

    if not master_enabled():
        return RiskForecast(
            evaluated_at_unix=started,
            master_enabled=False,
            forecast_score=0.0,
            belief_drift_score=0.0,
            meta_recurrence_score=0.0,
            calibration_decay_score=0.0,
            verdict=ForecastVerdict.LOW,
            dominant_factor=RiskFactor.NONE,
            falsified_belief_count=0,
            fused_meta_count=0,
            outcome_accuracy=0.0,
            diagnostic=(
                f"gate disabled via {_ENV_MASTER}=false"
            ),
            elapsed_s=0.0,
        )

    # Resolve component scores.
    if belief_drift_score is None or falsified_count is None:
        bs, bc = _load_belief_drift()
    else:
        bs, bc = belief_drift_score, falsified_count
    if meta_recurrence_score is None or fused_count is None:
        ms, mc = _load_meta_recurrence()
    else:
        ms, mc = meta_recurrence_score, fused_count
    if calibration_decay_score is None or outcome_accuracy is None:
        cs, oa = _load_calibration_decay()
    else:
        cs, oa = calibration_decay_score, outcome_accuracy

    bw = belief_weight()
    mw = meta_weight()
    cw = calibration_weight()
    total_weight = bw + mw + cw
    if total_weight > 0:
        forecast = (bs * bw + ms * mw + cs * cw) / total_weight
    else:
        forecast = 0.0
    forecast = max(0.0, min(1.0, forecast))

    verdict = _verdict_for_score(forecast)
    factor = _classify_factor(bs, ms, cs)

    diagnostic = (
        f"forecast={forecast:.2f} "
        f"(belief={bs:.2f}×{bw:.1f} "
        f"meta={ms:.2f}×{mw:.1f} "
        f"calib={cs:.2f}×{cw:.1f}); "
        f"verdict={verdict.value} factor={factor.value}"
    )

    report = RiskForecast(
        evaluated_at_unix=started,
        master_enabled=True,
        forecast_score=forecast,
        belief_drift_score=bs,
        meta_recurrence_score=ms,
        calibration_decay_score=cs,
        verdict=verdict,
        dominant_factor=factor,
        falsified_belief_count=bc,
        fused_meta_count=mc,
        outcome_accuracy=oa,
        diagnostic=diagnostic,
        elapsed_s=max(0.0, time.time() - started),
    )
    _persist_report(report)
    _publish_event(report)
    return report


def _persist_report(report: RiskForecast) -> None:
    """Best-effort §33.4 write. NEVER raises. Only persists
    when forecast is non-LOW (silence on quiet windows)."""
    if report.verdict is ForecastVerdict.LOW:
        return
    _flock_append({"kind": "forecast", "payload": report.to_dict()})


def _publish_event(report: RiskForecast) -> None:
    """Best-effort SSE publish. NEVER raises."""
    if not master_enabled():
        return
    if report.verdict is ForecastVerdict.LOW:
        return
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            EVENT_TYPE_PREDICTIVE_POSTMORTEM_FORECASTED,
            publish_task_event,
        )
        publish_task_event(
            EVENT_TYPE_PREDICTIVE_POSTMORTEM_FORECASTED,
            (
                f"system::predictive_postmortem::"
                f"{report.schema_version}"
            ),
            {
                "forecast_score": report.forecast_score,
                "verdict": report.verdict.value,
                "dominant_factor": report.dominant_factor.value,
                "falsified_belief_count": (
                    report.falsified_belief_count
                ),
                "fused_meta_count": report.fused_meta_count,
                "outcome_accuracy": report.outcome_accuracy,
                "elapsed_s": report.elapsed_s,
                "schema_version": report.schema_version,
            },
        )
    except Exception:  # noqa: BLE001
        return


def format_forecast_panel(
    report: Optional[RiskForecast] = None,
) -> str:
    """NEVER raises."""
    if report is None:
        if not master_enabled():
            return (
                f"predictive postmortem: disabled "
                f"({_ENV_MASTER}=false)"
            )
        return "predictive postmortem: no report"
    if not report.master_enabled:
        return (
            f"predictive postmortem: disabled "
            f"({_ENV_MASTER}=false)"
        )
    vg = verdict_glyph(report.verdict)
    fg = factor_glyph(report.dominant_factor)
    lines = [
        f"🔮 Predictive Postmortem  {vg} {report.verdict.value}",
        f"  forecast_score        : {report.forecast_score:.2f}",
        f"  belief_drift          : {report.belief_drift_score:.2f}"
        f"  ({report.falsified_belief_count} falsified)",
        f"  meta_recurrence       : "
        f"{report.meta_recurrence_score:.2f} "
        f"({report.fused_meta_count} fused)",
        f"  calibration_decay     : "
        f"{report.calibration_decay_score:.2f} "
        f"(accuracy={report.outcome_accuracy:.2f})",
        f"  dominant_factor       : {fg} {report.dominant_factor.value}",
        f"  diagnostic            : {report.diagnostic}",
    ]
    return "\n".join(lines)


# AST pins


def register_shipped_invariants() -> list:
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/"
        "predictive_postmortem.py"
    )

    _EXPECTED_VERDICTS = {
        "low", "moderate", "high", "critical",
    }
    _EXPECTED_FACTORS = {
        "belief_drift", "meta_recurrence",
        "calibration_decay", "none",
    }

    def _validate_verdict_taxonomy(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "ForecastVerdict"
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
                        f"ForecastVerdict missing: "
                        f"{sorted(missing)}",
                    )
                if extra:
                    return (
                        f"ForecastVerdict drift: "
                        f"{sorted(extra)}",
                    )
                return ()
        return ("ForecastVerdict class not found",)

    def _validate_factor_taxonomy(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "RiskFactor"
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
                missing = _EXPECTED_FACTORS - found
                extra = found - _EXPECTED_FACTORS
                if missing:
                    return (
                        f"RiskFactor missing: "
                        f"{sorted(missing)}",
                    )
                if extra:
                    return (
                        f"RiskFactor drift: {sorted(extra)}",
                    )
                return ()
        return ("RiskFactor class not found",)

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
                "must compose Wave 4 #9 belief_revision_ledger",
            )
        if "postmortem_fusion" not in source:
            violations.append(
                "must compose Wave 4 #11 postmortem_fusion",
            )
        if "mirror_self_test" not in source:
            violations.append(
                "must compose Wave 4 #14 mirror_self_test",
            )
        if "cross_process_jsonl" not in source:
            violations.append(
                "must compose cross_process_jsonl",
            )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "predictive_postmortem_verdict_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "ForecastVerdict 4-value taxonomy bytes-pinned."
            ),
            validate=_validate_verdict_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "predictive_postmortem_factor_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "RiskFactor 4-value taxonomy bytes-pinned."
            ),
            validate=_validate_factor_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "predictive_postmortem_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "Substrate purity — pure forecaster. MUST NOT "
                "import orchestrator / iron_gate / etc."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "predictive_postmortem_master_default_false"
            ),
            target_file=target,
            description=(
                "§33.1 cognitive substrate default-FALSE."
            ),
            validate=_validate_master_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "predictive_postmortem_composes_canonical"
            ),
            target_file=target,
            description=(
                "Substrate composes Wave 4 #9 + #11 + #14 + "
                "cross_process_jsonl."
            ),
            validate=_validate_composes_canonical,
        ),
    ]


def register_flags(registry: Any) -> int:
    from backend.core.ouroboros.governance.flag_registry import (
        Category,
        FlagSpec,
        FlagType,
    )

    src = (
        "backend/core/ouroboros/governance/"
        "predictive_postmortem.py"
    )

    seeds = [
        FlagSpec(
            name=_ENV_MASTER,
            type=FlagType.BOOL,
            default=False,
            description=(
                "Predictive postmortem master switch. §33.1 "
                "default-FALSE. Closes §40 Wave 5 #18. "
                "Forecast composes Wave 4 #9 belief-drift + "
                "#11 meta-recurrence + #14 calibration-decay "
                "→ 4-value verdict (LOW / MODERATE / HIGH / "
                "CRITICAL)."
            ),
            category=Category.EXPERIMENTAL,
            source_file=src,
            example=f"{_ENV_MASTER}=true",
        ),
        FlagSpec(
            name=_ENV_PERSIST,
            type=FlagType.BOOL,
            default=True,
            description="Sub-flag — gate §33.4 writes.",
            category=Category.SAFETY,
            source_file=src,
            example=f"{_ENV_PERSIST}=false",
        ),
        FlagSpec(
            name=_ENV_BELIEF_WEIGHT,
            type=FlagType.FLOAT,
            default=_DEFAULT_BELIEF_WEIGHT,
            description="Belief-drift component weight (default 1.0).",
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_BELIEF_WEIGHT}=2.0",
        ),
        FlagSpec(
            name=_ENV_META_WEIGHT,
            type=FlagType.FLOAT,
            default=_DEFAULT_META_WEIGHT,
            description=(
                "Meta-recurrence component weight "
                "(default 1.5)."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_META_WEIGHT}=2.0",
        ),
        FlagSpec(
            name=_ENV_CALIB_WEIGHT,
            type=FlagType.FLOAT,
            default=_DEFAULT_CALIB_WEIGHT,
            description=(
                "Calibration-decay component weight "
                "(default 1.0)."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_CALIB_WEIGHT}=1.5",
        ),
        FlagSpec(
            name=_ENV_MODERATE_THRESHOLD,
            type=FlagType.FLOAT,
            default=_DEFAULT_MODERATE,
            description=(
                "Threshold for LOW → MODERATE (default 0.25)."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_MODERATE_THRESHOLD}=0.30",
        ),
        FlagSpec(
            name=_ENV_HIGH_THRESHOLD,
            type=FlagType.FLOAT,
            default=_DEFAULT_HIGH,
            description=(
                "Threshold for MODERATE → HIGH (default 0.50). "
                "Auto-clamped ≥ moderate_threshold."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_HIGH_THRESHOLD}=0.60",
        ),
        FlagSpec(
            name=_ENV_CRITICAL_THRESHOLD,
            type=FlagType.FLOAT,
            default=_DEFAULT_CRITICAL,
            description=(
                "Threshold for HIGH → CRITICAL (default 0.75). "
                "Auto-clamped ≥ high_threshold."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_CRITICAL_THRESHOLD}=0.85",
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
    "PREDICTIVE_POSTMORTEM_SCHEMA_VERSION",
    "ForecastVerdict",
    "RiskFactor",
    "RiskForecast",
    "master_enabled",
    "persistence_enabled",
    "belief_weight",
    "meta_weight",
    "calibration_weight",
    "moderate_threshold",
    "high_threshold",
    "critical_threshold",
    "ledger_path",
    "verdict_glyph",
    "factor_glyph",
    "forecast_postmortem_risk",
    "format_forecast_panel",
    "register_shipped_invariants",
    "register_flags",
]
