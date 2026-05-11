"""
Cognitive Load Shedding
=======================

Closes §40 Wave 5 #21. Per the operator binding:

  "When system overloaded (high stress + high forecast), shed
   low-priority sensor work to focus capacity on
   IMMEDIATE/STANDARD ops."

Pure-function load evaluator. Composes:

* Wave 4 #13 ``anti_fragility_budget`` — aggregate stress
  signal across operator-supplied modules. Returns
  ``stressed_count`` + ``exhausted_count``.
* Wave 5 #18 ``predictive_postmortem`` — forward-looking risk
  forecast verdict.

The two signals combine into a single 0..1 ``load_score`` →
4-value :class:`LoadVerdict` (NORMAL / ELEVATED / OVERLOADED /
DISABLED) → 4-value :class:`ShedKind` advisory (NO_SHED /
SPECULATIVE_SHED / BACKGROUND_SHED / FULL_SHED) that
consumer-side throttling (SensorGovernor / dispatch caps)
reads as a recommendation.

Substrate is **advisory** — surfaces the shed-level signal;
NEVER directly modifies sensor state. Operator wires
SensorGovernor or sensor priority filters to consume the
surface.

§33.1 ``JARVIS_COGNITIVE_LOAD_SHEDDING_ENABLED`` default-FALSE.

Authority asymmetry (AST-pinned): no orchestrator / iron_gate /
policy / providers / candidate_generator / urgency_router /
change_engine / semantic_guardian / auto_committer /
risk_tier_floor / sensor_governor.
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


COGNITIVE_LOAD_SHEDDING_SCHEMA_VERSION: str = "cognitive_load_shedding.1"


_ENV_MASTER = "JARVIS_COGNITIVE_LOAD_SHEDDING_ENABLED"
_ENV_PERSIST = "JARVIS_COGNITIVE_LOAD_SHEDDING_PERSIST_ENABLED"
_ENV_STRESS_WEIGHT = "JARVIS_COGNITIVE_LOAD_SHEDDING_STRESS_WEIGHT"
_ENV_FORECAST_WEIGHT = "JARVIS_COGNITIVE_LOAD_SHEDDING_FORECAST_WEIGHT"
_ENV_ELEVATED_THRESHOLD = (
    "JARVIS_COGNITIVE_LOAD_SHEDDING_ELEVATED_THRESHOLD"
)
_ENV_OVERLOADED_THRESHOLD = (
    "JARVIS_COGNITIVE_LOAD_SHEDDING_OVERLOADED_THRESHOLD"
)
_ENV_LEDGER_PATH = "JARVIS_COGNITIVE_LOAD_SHEDDING_LEDGER_PATH"

_DEFAULT_STRESS_WEIGHT = 1.0
_DEFAULT_FORECAST_WEIGHT = 1.0
_DEFAULT_ELEVATED = 0.30
_DEFAULT_OVERLOADED = 0.65

_DEFAULT_LEDGER_REL = ".jarvis/cognitive_load_shedding_ledger.jsonl"

_TRUTHY: FrozenSet[str] = frozenset({"1", "true", "yes", "on"})


def _flag(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


def master_enabled() -> bool:
    """§33.1 — default-FALSE."""
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


def stress_weight() -> float:
    return _read_clamped_float(
        _ENV_STRESS_WEIGHT, _DEFAULT_STRESS_WEIGHT, 0.0, 10.0,
    )


def forecast_weight() -> float:
    return _read_clamped_float(
        _ENV_FORECAST_WEIGHT, _DEFAULT_FORECAST_WEIGHT, 0.0, 10.0,
    )


def elevated_threshold() -> float:
    return _read_clamped_float(
        _ENV_ELEVATED_THRESHOLD, _DEFAULT_ELEVATED, 0.0, 1.0,
    )


def overloaded_threshold() -> float:
    raw = _read_clamped_float(
        _ENV_OVERLOADED_THRESHOLD, _DEFAULT_OVERLOADED, 0.0, 1.0,
    )
    return max(raw, elevated_threshold())


def ledger_path() -> Path:
    raw = os.environ.get(_ENV_LEDGER_PATH, "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(_DEFAULT_LEDGER_REL)


# Closed taxonomies


class LoadVerdict(str, enum.Enum):
    """Closed 4-value verdict — bytes-pinned via AST."""

    NORMAL = "normal"
    ELEVATED = "elevated"
    OVERLOADED = "overloaded"
    DISABLED = "disabled"


class ShedKind(str, enum.Enum):
    """Closed 4-value shed advisory — bytes-pinned via AST."""

    NO_SHED = "no_shed"
    SPECULATIVE_SHED = "speculative_shed"
    BACKGROUND_SHED = "background_shed"
    FULL_SHED = "full_shed"


_VERDICT_GLYPH: Dict[str, str] = {
    LoadVerdict.NORMAL.value: "✓",
    LoadVerdict.ELEVATED.value: "⚠",
    LoadVerdict.OVERLOADED.value: "🔥",
    LoadVerdict.DISABLED.value: "◌",
}


_SHED_GLYPH: Dict[str, str] = {
    ShedKind.NO_SHED.value: "·",
    ShedKind.SPECULATIVE_SHED.value: "▽",
    ShedKind.BACKGROUND_SHED.value: "▼",
    ShedKind.FULL_SHED.value: "🛑",
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


def shed_glyph(shed: object) -> str:
    try:
        if hasattr(shed, "value"):
            return _SHED_GLYPH.get(str(shed.value), "?")
        return _SHED_GLYPH.get(
            str(shed or "").strip().lower(), "?",
        )
    except Exception:  # noqa: BLE001
        return "?"


def _shed_for_verdict(verdict: LoadVerdict) -> ShedKind:
    """Deterministic mapping verdict → recommended shed kind."""
    if verdict is LoadVerdict.OVERLOADED:
        return ShedKind.FULL_SHED
    if verdict is LoadVerdict.ELEVATED:
        return ShedKind.SPECULATIVE_SHED
    return ShedKind.NO_SHED


# §33.5 frozen artifact


@dataclass(frozen=True)
class LoadShedReport:
    """Aggregate load + shed report."""

    evaluated_at_unix: float
    master_enabled: bool
    load_score: float
    stress_score: float
    forecast_score: float
    verdict: LoadVerdict
    shed_kind: ShedKind
    stressed_count: int
    exhausted_count: int
    forecast_verdict: str
    diagnostic: str
    elapsed_s: float
    schema_version: str = COGNITIVE_LOAD_SHEDDING_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "evaluated_at_unix": self.evaluated_at_unix,
            "master_enabled": self.master_enabled,
            "load_score": float(self.load_score),
            "stress_score": float(self.stress_score),
            "forecast_score": float(self.forecast_score),
            "verdict": self.verdict.value,
            "shed_kind": self.shed_kind.value,
            "stressed_count": int(self.stressed_count),
            "exhausted_count": int(self.exhausted_count),
            "forecast_verdict": self.forecast_verdict[:32],
            "diagnostic": self.diagnostic[:512],
            "elapsed_s": float(self.elapsed_s),
            "schema_version": self.schema_version,
        }


# Composers


def _load_anti_fragility(
    modules: Sequence[str],
) -> Tuple[float, int, int]:
    """Returns (stress_score, stressed_count, exhausted_count).
    NEVER raises. stress_score = (stressed + 2×exhausted) /
    max(1, len(modules)) — saturates at 2 when all modules
    are EXHAUSTED. Clamped to [0.0, 1.0]."""
    if not modules:
        return 0.0, 0, 0
    try:
        from backend.core.ouroboros.governance.anti_fragility_budget import (  # noqa: E501
            evaluate_modules,
        )
    except ImportError:
        return 0.0, 0, 0
    try:
        report = evaluate_modules(modules)
    except Exception:  # noqa: BLE001
        return 0.0, 0, 0
    stressed = int(getattr(report, "stressed_count", 0) or 0)
    exhausted = int(getattr(report, "exhausted_count", 0) or 0)
    n = max(1, len(modules))
    raw = (stressed + 2 * exhausted) / float(n)
    score = max(0.0, min(1.0, raw / 2.0))
    return score, stressed, exhausted


def _load_forecast() -> Tuple[float, str]:
    """Returns (forecast_score, verdict_value). NEVER raises."""
    try:
        from backend.core.ouroboros.governance.predictive_postmortem import (  # noqa: E501
            forecast_postmortem_risk,
        )
    except ImportError:
        return 0.0, ""
    try:
        rep = forecast_postmortem_risk()
        return (
            float(getattr(rep, "forecast_score", 0.0) or 0.0),
            str(
                getattr(getattr(rep, "verdict", None), "value", "")
                or "",
            ),
        )
    except Exception:  # noqa: BLE001
        return 0.0, ""


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


def _verdict_for_score(score: float) -> LoadVerdict:
    if score >= overloaded_threshold():
        return LoadVerdict.OVERLOADED
    if score >= elevated_threshold():
        return LoadVerdict.ELEVATED
    return LoadVerdict.NORMAL


def evaluate_cognitive_load(
    *,
    modules: Optional[Sequence[str]] = None,
    stress_score_override: Optional[float] = None,
    forecast_score_override: Optional[float] = None,
    forecast_verdict_override: Optional[str] = None,
    stressed_count_override: Optional[int] = None,
    exhausted_count_override: Optional[int] = None,
    now_unix: Optional[float] = None,
) -> LoadShedReport:
    """Top-level load evaluator. NEVER raises.

    Parameters
    ----------
    modules:
        Module list to evaluate via Wave 4 #13. Empty list /
        None → stress_score = 0. Operator wires real module
        inventory at integration time.
    *_override:
        Testing seams; default composes real substrates.
    """
    started = time.time() if now_unix is None else float(now_unix)

    if not master_enabled():
        return LoadShedReport(
            evaluated_at_unix=started,
            master_enabled=False,
            load_score=0.0,
            stress_score=0.0,
            forecast_score=0.0,
            verdict=LoadVerdict.DISABLED,
            shed_kind=ShedKind.NO_SHED,
            stressed_count=0,
            exhausted_count=0,
            forecast_verdict="",
            diagnostic=(
                f"gate disabled via {_ENV_MASTER}=false"
            ),
            elapsed_s=0.0,
        )

    if (
        stress_score_override is None
        or stressed_count_override is None
        or exhausted_count_override is None
    ):
        ss, sc, ec = _load_anti_fragility(modules or ())
    else:
        ss = stress_score_override
        sc = stressed_count_override
        ec = exhausted_count_override

    if (
        forecast_score_override is None
        or forecast_verdict_override is None
    ):
        fs, fv = _load_forecast()
    else:
        fs = forecast_score_override
        fv = forecast_verdict_override

    sw = stress_weight()
    fw = forecast_weight()
    total = sw + fw
    if total > 0:
        load = (ss * sw + fs * fw) / total
    else:
        load = 0.0
    load = max(0.0, min(1.0, load))

    verdict = _verdict_for_score(load)
    shed = _shed_for_verdict(verdict)

    diagnostic = (
        f"load={load:.2f} (stress={ss:.2f}×{sw:.1f} "
        f"forecast={fs:.2f}×{fw:.1f}); "
        f"verdict={verdict.value} shed={shed.value}"
    )

    report = LoadShedReport(
        evaluated_at_unix=started,
        master_enabled=True,
        load_score=load,
        stress_score=ss,
        forecast_score=fs,
        verdict=verdict,
        shed_kind=shed,
        stressed_count=sc,
        exhausted_count=ec,
        forecast_verdict=fv,
        diagnostic=diagnostic,
        elapsed_s=max(0.0, time.time() - started),
    )
    _persist_report(report)
    _publish_event(report)
    return report


def _persist_report(report: LoadShedReport) -> None:
    """Best-effort §33.4 write. NEVER raises. Skips NORMAL."""
    if report.verdict is LoadVerdict.NORMAL:
        return
    _flock_append({"kind": "load_shed", "payload": report.to_dict()})


def _publish_event(report: LoadShedReport) -> None:
    """Best-effort SSE publish. NEVER raises."""
    if not master_enabled():
        return
    if report.verdict is LoadVerdict.NORMAL:
        return
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            EVENT_TYPE_COGNITIVE_LOAD_SHED_TRIGGERED,
            publish_task_event,
        )
        publish_task_event(
            EVENT_TYPE_COGNITIVE_LOAD_SHED_TRIGGERED,
            (
                f"system::cognitive_load::"
                f"{report.schema_version}"
            ),
            {
                "load_score": report.load_score,
                "verdict": report.verdict.value,
                "shed_kind": report.shed_kind.value,
                "stressed_count": report.stressed_count,
                "exhausted_count": report.exhausted_count,
                "elapsed_s": report.elapsed_s,
                "schema_version": report.schema_version,
            },
        )
    except Exception:  # noqa: BLE001
        return


def format_load_panel(
    report: Optional[LoadShedReport] = None,
) -> str:
    """NEVER raises."""
    if report is None:
        if not master_enabled():
            return (
                f"cognitive load shedding: disabled "
                f"({_ENV_MASTER}=false)"
            )
        return "cognitive load shedding: no report"
    if not report.master_enabled:
        return (
            f"cognitive load shedding: disabled "
            f"({_ENV_MASTER}=false)"
        )
    vg = verdict_glyph(report.verdict)
    sg = shed_glyph(report.shed_kind)
    lines = [
        f"🧠 Cognitive Load  {vg} {report.verdict.value} "
        f"  {sg} {report.shed_kind.value}",
        f"  load_score      : {report.load_score:.2f}",
        f"  stress_score    : {report.stress_score:.2f} "
        f"(stressed={report.stressed_count} "
        f"exhausted={report.exhausted_count})",
        f"  forecast_score  : {report.forecast_score:.2f} "
        f"({report.forecast_verdict or 'n/a'})",
        f"  diagnostic      : {report.diagnostic}",
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
        "cognitive_load_shedding.py"
    )

    _EXPECTED_VERDICTS = {
        "normal", "elevated", "overloaded", "disabled",
    }
    _EXPECTED_SHEDS = {
        "no_shed", "speculative_shed",
        "background_shed", "full_shed",
    }

    def _validate_verdict_taxonomy(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "LoadVerdict"
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
                        f"LoadVerdict missing: {sorted(missing)}",
                    )
                if extra:
                    return (
                        f"LoadVerdict drift: {sorted(extra)}",
                    )
                return ()
        return ("LoadVerdict class not found",)

    def _validate_shed_taxonomy(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "ShedKind"
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
                missing = _EXPECTED_SHEDS - found
                extra = found - _EXPECTED_SHEDS
                if missing:
                    return (
                        f"ShedKind missing: {sorted(missing)}",
                    )
                if extra:
                    return (
                        f"ShedKind drift: {sorted(extra)}",
                    )
                return ()
        return ("ShedKind class not found",)

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
        if "anti_fragility_budget" not in source:
            violations.append(
                "must compose Wave 4 #13 anti_fragility_budget",
            )
        if "predictive_postmortem" not in source:
            violations.append(
                "must compose Wave 5 #18 predictive_postmortem",
            )
        if "cross_process_jsonl" not in source:
            violations.append(
                "must compose cross_process_jsonl",
            )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "cognitive_load_shedding_verdict_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "LoadVerdict 4-value taxonomy bytes-pinned."
            ),
            validate=_validate_verdict_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "cognitive_load_shedding_shed_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "ShedKind 4-value taxonomy bytes-pinned."
            ),
            validate=_validate_shed_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "cognitive_load_shedding_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "Substrate is advisory — MUST NOT import "
                "sensor_governor (one-way cage)."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "cognitive_load_shedding_master_default_false"
            ),
            target_file=target,
            description="§33.1 default-FALSE.",
            validate=_validate_master_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "cognitive_load_shedding_composes_canonical"
            ),
            target_file=target,
            description=(
                "Composes Wave 4 #13 + Wave 5 #18 + "
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
        "cognitive_load_shedding.py"
    )

    seeds = [
        FlagSpec(
            name=_ENV_MASTER,
            type=FlagType.BOOL,
            default=False,
            description=(
                "Cognitive load shedding master. §33.1 "
                "default-FALSE. Closes §40 Wave 5 #21. "
                "Composes Wave 4 #13 + Wave 5 #18 into "
                "shed-level advisory signal."
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
            name=_ENV_STRESS_WEIGHT,
            type=FlagType.FLOAT,
            default=_DEFAULT_STRESS_WEIGHT,
            description="Stress component weight (default 1.0).",
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_STRESS_WEIGHT}=1.5",
        ),
        FlagSpec(
            name=_ENV_FORECAST_WEIGHT,
            type=FlagType.FLOAT,
            default=_DEFAULT_FORECAST_WEIGHT,
            description="Forecast component weight (default 1.0).",
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_FORECAST_WEIGHT}=2.0",
        ),
        FlagSpec(
            name=_ENV_ELEVATED_THRESHOLD,
            type=FlagType.FLOAT,
            default=_DEFAULT_ELEVATED,
            description=(
                "NORMAL→ELEVATED threshold (default 0.30)."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_ELEVATED_THRESHOLD}=0.35",
        ),
        FlagSpec(
            name=_ENV_OVERLOADED_THRESHOLD,
            type=FlagType.FLOAT,
            default=_DEFAULT_OVERLOADED,
            description=(
                "ELEVATED→OVERLOADED threshold (default 0.65). "
                "Auto-clamped ≥ elevated_threshold."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_OVERLOADED_THRESHOLD}=0.75",
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
    "COGNITIVE_LOAD_SHEDDING_SCHEMA_VERSION",
    "LoadVerdict",
    "ShedKind",
    "LoadShedReport",
    "master_enabled",
    "persistence_enabled",
    "stress_weight",
    "forecast_weight",
    "elevated_threshold",
    "overloaded_threshold",
    "ledger_path",
    "verdict_glyph",
    "shed_glyph",
    "evaluate_cognitive_load",
    "format_load_panel",
    "register_shipped_invariants",
    "register_flags",
]
