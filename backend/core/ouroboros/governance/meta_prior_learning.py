"""
Meta-Prior Learning
====================

Closes §40 Wave 5 #22 — first experimental Wave 5 arc. Per the
operator binding:

  "Track which priors win consensus over time; system learns a
   meta-distribution and surfaces emerging dominant priors."

This substrate is a **pure-function meta-learner** over the
Wave 4 #12 Schelling history ledger. For each ``prior_kind``
observed it computes:

* ``total_sample`` — observations across the entire ledger
* ``recent_sample`` — observations in the last
  ``recent_window_s`` seconds
* ``win_rate_recent`` — fraction of recent observations where
  ``was_accepted=True``
* ``win_rate_historic`` — same fraction across the full ledger
* ``trend`` = recent − historic (positive → rising,
  negative → declining)

These compose into a 4-value :class:`MetaPriorVerdict`
(DORMANT / EMERGING / DOMINANT / DECLINING) and a 4-value
:class:`LearningStage` (COLD_START / BOOTSTRAP / STEADY /
SATURATED — keyed off total sample count).

The substrate is **deterministic** — same ledger corpus →
same meta-distribution. Zero LLM. The verdicts are
*surfaced*; consumer-side action (re-weighting prior
trust scores, retiring dormant priors) stays operator-paced.

Composition contract — pure-function meta-learner:

* :func:`schelling_consensus_prior._load_history` (Wave 4
  #12) — substrate reads the same JSONL audit ledger.
* :func:`cross_process_jsonl.flock_append_line` — optional
  §33.4 audit at ``.jarvis/meta_prior_ledger.jsonl``.

NEVER raises. Empty schelling history / corrupted rows / no
prior_kinds observed all degrade to ``COLD_START`` /
``DORMANT`` verdicts, not exception.

§33.1 cognitive substrate
``JARVIS_META_PRIOR_LEARNING_ENABLED`` default-**FALSE**.

Authority asymmetry (AST-pinned): imports stdlib only at
module-load. ``schelling_consensus_prior`` (Wave 4 #12) +
``cross_process_jsonl`` lazy-imported behind composer helpers.
Does NOT import orchestrator / iron_gate / policy / providers
/ candidate_generator / urgency_router / change_engine /
semantic_guardian / auto_committer / risk_tier_floor.
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


META_PRIOR_LEARNING_SCHEMA_VERSION: str = "meta_prior_learning.1"


# ===========================================================================
# Env knobs
# ===========================================================================


_ENV_MASTER = "JARVIS_META_PRIOR_LEARNING_ENABLED"
_ENV_PERSIST = "JARVIS_META_PRIOR_LEARNING_PERSIST_ENABLED"
_ENV_RECENT_WINDOW_S = "JARVIS_META_PRIOR_LEARNING_RECENT_WINDOW_S"
_ENV_EMERGING_TREND = "JARVIS_META_PRIOR_LEARNING_EMERGING_TREND"
_ENV_DECLINING_TREND = "JARVIS_META_PRIOR_LEARNING_DECLINING_TREND"
_ENV_DOMINANT_RATE = "JARVIS_META_PRIOR_LEARNING_DOMINANT_RATE"
_ENV_MAX_PRIORS = "JARVIS_META_PRIOR_LEARNING_MAX_PRIORS"
_ENV_LEDGER_PATH = "JARVIS_META_PRIOR_LEARNING_LEDGER_PATH"

_DEFAULT_RECENT_WINDOW_S = 86_400  # 24h
_DEFAULT_EMERGING_TREND = 0.10   # +10% rise vs historic = EMERGING
_DEFAULT_DECLINING_TREND = -0.10  # -10% fall = DECLINING
_DEFAULT_DOMINANT_RATE = 0.75    # win_rate ≥ 75% AND positive trend = DOMINANT
_DEFAULT_MAX_PRIORS = 20

_DEFAULT_LEDGER_REL = ".jarvis/meta_prior_ledger.jsonl"

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
    """Sub-flag — gate §33.4 writes. Default TRUE."""
    return _flag(_ENV_PERSIST, default=True)


def _read_clamped_int(name: str, default: int, lo: int, hi: int) -> int:
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


def recent_window_s() -> int:
    """Window for `recent` observations. Default 86400 (24h).
    Clamped to [60, 30 days]."""
    return _read_clamped_int(
        _ENV_RECENT_WINDOW_S, _DEFAULT_RECENT_WINDOW_S,
        60, 30 * 86_400,
    )


def emerging_trend_threshold() -> float:
    """Positive trend threshold for EMERGING. Default 0.10."""
    return _read_clamped_float(
        _ENV_EMERGING_TREND, _DEFAULT_EMERGING_TREND,
        0.0, 1.0,
    )


def declining_trend_threshold() -> float:
    """Negative trend threshold for DECLINING. Default -0.10.
    Clamped to [-1.0, 0.0]."""
    return _read_clamped_float(
        _ENV_DECLINING_TREND, _DEFAULT_DECLINING_TREND,
        -1.0, 0.0,
    )


def dominant_rate_threshold() -> float:
    """Win-rate threshold for DOMINANT verdict. Default 0.75."""
    return _read_clamped_float(
        _ENV_DOMINANT_RATE, _DEFAULT_DOMINANT_RATE,
        0.0, 1.0,
    )


def max_priors() -> int:
    """Cap on returned prior count. Default 20. Clamped to
    [1, 10000]."""
    return _read_clamped_int(
        _ENV_MAX_PRIORS, _DEFAULT_MAX_PRIORS, 1, 10_000,
    )


def ledger_path() -> Path:
    """Audit ledger. Default `.jarvis/meta_prior_ledger.jsonl`."""
    raw = os.environ.get(_ENV_LEDGER_PATH, "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(_DEFAULT_LEDGER_REL)


# ===========================================================================
# Closed taxonomies
# ===========================================================================


class MetaPriorVerdict(str, enum.Enum):
    """Closed 4-value verdict — bytes-pinned via AST."""

    DORMANT = "dormant"
    EMERGING = "emerging"
    DOMINANT = "dominant"
    DECLINING = "declining"


class LearningStage(str, enum.Enum):
    """Closed 4-value stage — bytes-pinned via AST."""

    COLD_START = "cold_start"   # < 10 total observations
    BOOTSTRAP = "bootstrap"     # 10-50
    STEADY = "steady"            # 50-500
    SATURATED = "saturated"     # > 500


_VERDICT_GLYPH: Dict[str, str] = {
    MetaPriorVerdict.DORMANT.value: "○",
    MetaPriorVerdict.EMERGING.value: "↗",
    MetaPriorVerdict.DOMINANT.value: "★",
    MetaPriorVerdict.DECLINING.value: "↘",
}


_STAGE_GLYPH: Dict[str, str] = {
    LearningStage.COLD_START.value: "·",
    LearningStage.BOOTSTRAP.value: "○",
    LearningStage.STEADY.value: "◊",
    LearningStage.SATURATED.value: "▲",
}


def verdict_glyph(verdict: object) -> str:
    """NEVER raises."""
    try:
        if hasattr(verdict, "value"):
            return _VERDICT_GLYPH.get(str(verdict.value), "?")
        return _VERDICT_GLYPH.get(
            str(verdict or "").strip().lower(), "?",
        )
    except Exception:  # noqa: BLE001
        return "?"


def stage_glyph(stage: object) -> str:
    """NEVER raises."""
    try:
        if hasattr(stage, "value"):
            return _STAGE_GLYPH.get(str(stage.value), "?")
        return _STAGE_GLYPH.get(
            str(stage or "").strip().lower(), "?",
        )
    except Exception:  # noqa: BLE001
        return "?"


def _stage_for_sample(n: int) -> LearningStage:
    if n < 10:
        return LearningStage.COLD_START
    if n < 50:
        return LearningStage.BOOTSTRAP
    if n < 500:
        return LearningStage.STEADY
    return LearningStage.SATURATED


# ===========================================================================
# §33.5 frozen versioned artifacts
# ===========================================================================


@dataclass(frozen=True)
class PriorMetaStats:
    """Per-prior meta-distribution stats."""

    prior_kind: str
    total_sample: int
    recent_sample: int
    win_rate_historic: float
    win_rate_recent: float
    trend: float
    verdict: MetaPriorVerdict
    stage: LearningStage
    schema_version: str = META_PRIOR_LEARNING_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "prior_kind": self.prior_kind[:64],
            "total_sample": int(self.total_sample),
            "recent_sample": int(self.recent_sample),
            "win_rate_historic": float(self.win_rate_historic),
            "win_rate_recent": float(self.win_rate_recent),
            "trend": float(self.trend),
            "verdict": self.verdict.value,
            "stage": self.stage.value,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class MetaPriorReport:
    """Aggregate meta-distribution report."""

    evaluated_at_unix: float
    master_enabled: bool
    per_prior: Tuple[PriorMetaStats, ...]
    dominant_count: int
    emerging_count: int
    declining_count: int
    dormant_count: int
    diagnostic: str
    elapsed_s: float
    schema_version: str = META_PRIOR_LEARNING_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "evaluated_at_unix": self.evaluated_at_unix,
            "master_enabled": self.master_enabled,
            "per_prior": [p.to_dict() for p in self.per_prior],
            "dominant_count": int(self.dominant_count),
            "emerging_count": int(self.emerging_count),
            "declining_count": int(self.declining_count),
            "dormant_count": int(self.dormant_count),
            "diagnostic": self.diagnostic[:512],
            "elapsed_s": float(self.elapsed_s),
            "schema_version": self.schema_version,
        }


# ===========================================================================
# Composers — canonical surfaces (lazy-imported)
# ===========================================================================


def _load_schelling_history() -> Tuple[Mapping[str, Any], ...]:
    """Compose Wave 4 #12 ledger reader. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.schelling_consensus_prior import (  # noqa: E501
            _load_history,
        )
        return _load_history()
    except Exception:  # noqa: BLE001
        return ()


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
# Pure aggregator
# ===========================================================================


def _verdict_for_stats(
    total_sample: int,
    recent_rate: float,
    historic_rate: float,
    trend: float,
) -> MetaPriorVerdict:
    """Pure classifier. NEVER raises."""
    if total_sample == 0:
        return MetaPriorVerdict.DORMANT
    dom_t = dominant_rate_threshold()
    emerg_t = emerging_trend_threshold()
    decl_t = declining_trend_threshold()
    if recent_rate >= dom_t and trend >= 0:
        return MetaPriorVerdict.DOMINANT
    if trend >= emerg_t:
        return MetaPriorVerdict.EMERGING
    if trend <= decl_t:
        return MetaPriorVerdict.DECLINING
    return MetaPriorVerdict.DORMANT


def _aggregate_prior_stats(
    rows: Sequence[Mapping[str, Any]],
    *,
    window_seconds: int,
    now_unix: float,
) -> Tuple[PriorMetaStats, ...]:
    """Pure aggregation across all priors. NEVER raises."""
    cutoff = now_unix - window_seconds
    per_prior: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        try:
            if r.get("kind") != "prior_outcome":
                continue
            pk = str(r.get("prior_kind") or "").strip()
            if not pk:
                continue
            accepted = bool(r.get("was_accepted"))
            t = float(r.get("observed_at_unix") or 0.0)
        except Exception:  # noqa: BLE001
            continue
        bucket = per_prior.setdefault(pk, {
            "total": 0,
            "total_accepts": 0,
            "recent": 0,
            "recent_accepts": 0,
        })
        bucket["total"] += 1
        if accepted:
            bucket["total_accepts"] += 1
        if t >= cutoff:
            bucket["recent"] += 1
            if accepted:
                bucket["recent_accepts"] += 1

    out: List[PriorMetaStats] = []
    for pk, b in per_prior.items():
        total = b["total"]
        recent = b["recent"]
        historic_rate = (
            b["total_accepts"] / total if total > 0 else 0.0
        )
        recent_rate = (
            b["recent_accepts"] / recent if recent > 0 else 0.0
        )
        trend = recent_rate - historic_rate
        verdict = _verdict_for_stats(
            total, recent_rate, historic_rate, trend,
        )
        stage = _stage_for_sample(total)
        out.append(PriorMetaStats(
            prior_kind=pk,
            total_sample=total,
            recent_sample=recent,
            win_rate_historic=historic_rate,
            win_rate_recent=recent_rate,
            trend=trend,
            verdict=verdict,
            stage=stage,
        ))
    # Deterministic sort: dominant first, then by historic
    # win_rate desc, then by name.
    _ORDER = {
        MetaPriorVerdict.DOMINANT.value: 0,
        MetaPriorVerdict.EMERGING.value: 1,
        MetaPriorVerdict.DECLINING.value: 2,
        MetaPriorVerdict.DORMANT.value: 3,
    }
    out.sort(key=lambda s: (
        _ORDER.get(s.verdict.value, 99),
        -s.win_rate_historic,
        s.prior_kind,
    ))
    return tuple(out[:max_priors()])


def compute_meta_distribution(
    *,
    rows: Optional[Sequence[Mapping[str, Any]]] = None,
    now_unix: Optional[float] = None,
) -> MetaPriorReport:
    """Top-level evaluator. NEVER raises.

    Parameters
    ----------
    rows:
        Caller-injectable history corpus (testing seam). Default
        composes Wave 4 #12 ``_load_history``.
    now_unix:
        Reference time for window cutoff.
    """
    started = time.time() if now_unix is None else float(now_unix)
    if not master_enabled():
        return MetaPriorReport(
            evaluated_at_unix=started,
            master_enabled=False,
            per_prior=(),
            dominant_count=0,
            emerging_count=0,
            declining_count=0,
            dormant_count=0,
            diagnostic=(
                f"gate disabled via {_ENV_MASTER}=false"
            ),
            elapsed_s=0.0,
        )
    history = (
        rows
        if rows is not None
        else _load_schelling_history()
    )
    stats = _aggregate_prior_stats(
        history,
        window_seconds=recent_window_s(),
        now_unix=started,
    )
    dom = sum(
        1 for s in stats
        if s.verdict is MetaPriorVerdict.DOMINANT
    )
    emerg = sum(
        1 for s in stats
        if s.verdict is MetaPriorVerdict.EMERGING
    )
    decl = sum(
        1 for s in stats
        if s.verdict is MetaPriorVerdict.DECLINING
    )
    dorm = sum(
        1 for s in stats
        if s.verdict is MetaPriorVerdict.DORMANT
    )
    diagnostic = (
        f"{len(stats)} prior(s) tracked: dominant={dom} "
        f"emerging={emerg} declining={decl} dormant={dorm} "
        f"(window={recent_window_s()}s)"
    )
    report = MetaPriorReport(
        evaluated_at_unix=started,
        master_enabled=True,
        per_prior=stats,
        dominant_count=dom,
        emerging_count=emerg,
        declining_count=decl,
        dormant_count=dorm,
        diagnostic=diagnostic,
        elapsed_s=max(0.0, time.time() - started),
    )
    _persist_report(report)
    _publish_event(report)
    return report


def _persist_report(report: MetaPriorReport) -> None:
    """Best-effort §33.4 write. NEVER raises."""
    if not report.per_prior:
        return
    if report.dominant_count == 0 and report.emerging_count == 0:
        # Skip noise — only persist when actionable signal exists
        return
    _flock_append({"kind": "summary", "payload": report.to_dict()})


def _publish_event(report: MetaPriorReport) -> None:
    """Best-effort SSE publish. NEVER raises."""
    if not master_enabled():
        return
    if report.dominant_count == 0 and report.emerging_count == 0:
        return
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            EVENT_TYPE_META_PRIOR_LEARNED,
            publish_task_event,
        )
        publish_task_event(
            EVENT_TYPE_META_PRIOR_LEARNED,
            (
                f"system::meta_prior::"
                f"{report.schema_version}"
            ),
            {
                "prior_count": len(report.per_prior),
                "dominant_count": report.dominant_count,
                "emerging_count": report.emerging_count,
                "declining_count": report.declining_count,
                "dormant_count": report.dormant_count,
                "elapsed_s": report.elapsed_s,
                "schema_version": report.schema_version,
            },
        )
    except Exception:  # noqa: BLE001
        return


def format_meta_prior_panel(
    report: Optional[MetaPriorReport] = None,
) -> str:
    """Operator-facing panel. NEVER raises."""
    if report is None:
        if not master_enabled():
            return (
                f"meta-prior learning: disabled "
                f"({_ENV_MASTER}=false)"
            )
        return "meta-prior learning: no report"
    if not report.master_enabled:
        return (
            f"meta-prior learning: disabled "
            f"({_ENV_MASTER}=false)"
        )
    lines = [
        f"📈 Meta-Prior Learning  "
        f"dominant={report.dominant_count} "
        f"emerging={report.emerging_count} "
        f"declining={report.declining_count} "
        f"dormant={report.dormant_count}",
    ]
    if report.per_prior:
        for s in report.per_prior[:10]:
            vg = verdict_glyph(s.verdict)
            sg = stage_glyph(s.stage)
            lines.append(
                f"  {vg} {s.prior_kind:<20} "
                f"{s.verdict.value:<10} "
                f"recent={s.win_rate_recent:.2f} "
                f"historic={s.win_rate_historic:.2f} "
                f"trend={s.trend:+.2f} "
                f"{sg} {s.stage.value}"
            )
        if len(report.per_prior) > 10:
            lines.append(
                f"  ... (+{len(report.per_prior) - 10} more)"
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
        "meta_prior_learning.py"
    )

    _EXPECTED_VERDICTS = {
        "dormant", "emerging", "dominant", "declining",
    }
    _EXPECTED_STAGES = {
        "cold_start", "bootstrap", "steady", "saturated",
    }

    def _validate_verdict_taxonomy(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "MetaPriorVerdict"
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
                        f"MetaPriorVerdict missing: "
                        f"{sorted(missing)}",
                    )
                if extra:
                    return (
                        f"MetaPriorVerdict drift: "
                        f"{sorted(extra)}",
                    )
                return ()
        return ("MetaPriorVerdict class not found",)

    def _validate_stage_taxonomy(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "LearningStage"
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
                missing = _EXPECTED_STAGES - found
                extra = found - _EXPECTED_STAGES
                if missing:
                    return (
                        f"LearningStage missing: "
                        f"{sorted(missing)}",
                    )
                if extra:
                    return (
                        f"LearningStage drift: "
                        f"{sorted(extra)}",
                    )
                return ()
        return ("LearningStage class not found",)

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
        if "schelling_consensus_prior" not in source:
            violations.append(
                "must compose Wave 4 #12 "
                "schelling_consensus_prior (history source)",
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
                "meta_prior_verdict_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "MetaPriorVerdict 4-value taxonomy bytes-pinned."
            ),
            validate=_validate_verdict_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "meta_prior_stage_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "LearningStage 4-value taxonomy bytes-pinned."
            ),
            validate=_validate_stage_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "meta_prior_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "Substrate purity — pure meta-learner. MUST "
                "NOT import orchestrator / iron_gate / policy "
                "/ etc."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "meta_prior_master_default_false"
            ),
            target_file=target,
            description=(
                "§33.1 cognitive substrate default-FALSE."
            ),
            validate=_validate_master_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "meta_prior_composes_canonical"
            ),
            target_file=target,
            description=(
                "Substrate composes Wave 4 #12 "
                "schelling_consensus_prior + canonical "
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
        "meta_prior_learning.py"
    )

    seeds = [
        FlagSpec(
            name=_ENV_MASTER,
            type=FlagType.BOOL,
            default=False,
            description=(
                "Meta-prior learning master switch. §33.1 "
                "default-FALSE. Closes §40 Wave 5 #22 "
                "(experimental). Tracks per-prior accept-rate "
                "trends over Wave 4 #12 Schelling history → "
                "surfaces emerging / dominant / declining "
                "priors."
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
            name=_ENV_RECENT_WINDOW_S,
            type=FlagType.INT,
            default=_DEFAULT_RECENT_WINDOW_S,
            description=(
                "Window for 'recent' observations. Default "
                "86400 (24h). Clamped to [60, 30 days]."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_RECENT_WINDOW_S}=3600",
        ),
        FlagSpec(
            name=_ENV_EMERGING_TREND,
            type=FlagType.FLOAT,
            default=_DEFAULT_EMERGING_TREND,
            description=(
                "Positive trend threshold for EMERGING. "
                "Default 0.10."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_EMERGING_TREND}=0.15",
        ),
        FlagSpec(
            name=_ENV_DECLINING_TREND,
            type=FlagType.FLOAT,
            default=_DEFAULT_DECLINING_TREND,
            description=(
                "Negative trend threshold for DECLINING. "
                "Default -0.10. Clamped to [-1.0, 0.0]."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_DECLINING_TREND}=-0.20",
        ),
        FlagSpec(
            name=_ENV_DOMINANT_RATE,
            type=FlagType.FLOAT,
            default=_DEFAULT_DOMINANT_RATE,
            description=(
                "Win-rate threshold for DOMINANT verdict. "
                "Default 0.75."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_DOMINANT_RATE}=0.80",
        ),
        FlagSpec(
            name=_ENV_MAX_PRIORS,
            type=FlagType.INT,
            default=_DEFAULT_MAX_PRIORS,
            description=(
                "Cap on returned prior count. Default 20."
            ),
            category=Category.CAPACITY,
            source_file=src,
            example=f"{_ENV_MAX_PRIORS}=50",
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
    "META_PRIOR_LEARNING_SCHEMA_VERSION",
    "MetaPriorVerdict",
    "LearningStage",
    "PriorMetaStats",
    "MetaPriorReport",
    "master_enabled",
    "persistence_enabled",
    "recent_window_s",
    "emerging_trend_threshold",
    "declining_trend_threshold",
    "dominant_rate_threshold",
    "max_priors",
    "ledger_path",
    "verdict_glyph",
    "stage_glyph",
    "compute_meta_distribution",
    "format_meta_prior_panel",
    "register_shipped_invariants",
    "register_flags",
]
