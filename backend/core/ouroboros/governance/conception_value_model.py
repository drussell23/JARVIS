"""Conception Value Model — ranks self-conceived work by expected value (Gap 3).

The organism already CONCEIVES: DreamEngine emits scored ImprovementBlueprints,
OpportunityMiner ranks static candidates, IntentDiscovery / ProactiveExploration
propose proactive work. But those SCORES are discarded (a blueprint's
priority_score is flattened into prompt prose) and there is no unified model of
*what conceived work is worth pursuing*. This module supplies that model — an
authority-free, evidence-grounded expected-value ranker that composes four
existing read-only signals rather than inventing a new one:

  A  alignment    — strategic fit vs the codebase's active goal themes
                    (``semantic_index.score_with_cluster``).
  S  substance    — expected real-code value, verifiable-evidence-first
                    (``signal_value.score_signal`` band over the targets); the
                    KPI this optimises is ``substance_ledger.substance_ratio``.
  F  feasibility  — EARNED autonomy: do we have a track record we can act on in
                    this scope? (``trust_calibration.scope_trust`` — Gap 4 feeds
                    Gap 3: the organism preferentially conceives work in scopes
                    where it has EARNED the ability to land changes autonomously,
                    and backs off a scope that just regressed).
  C  cost         — value-per-dollar efficiency (blueprint ``estimated_cost_usd``).

    EV = cost_factor · Σ(wᵢ·axisᵢ) / Σwᵢ   ∈ [0, 1]

EV becomes the ``priority_hint`` on ``proactive_proposal_surface``, which bridges
conceived proposals into the intake router as ``auto_proposed`` envelopes.

Discipline (mirrors trust_calibration / signal_value):
  * **Authority-free** — imports only read-only scorers; never gate / policy /
    orchestrator / iron_gate. Grep-enforced by the observability import test.
  * **Never raises** — every axis degrades to a NEUTRAL prior (0.5) when its
    source is cold, so a fresh organism ranks by what it CAN see instead of
    collapsing every proposal to zero. Substance is the deliberate exception: a
    *proven* cosmetic band scores below neutral (we don't want to conceive
    cosmetic work), while an indeterminate band stays neutral.
  * **No hardcoded weights** — every weight / coefficient reads from an env var
    with a sensible default.
"""
from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

CONCEPTION_VALUE_SCHEMA_VERSION = "conception_value.1"

_TRUTHY = ("1", "true", "yes", "on")
_NEUTRAL = 0.5  # the prior for a cold / unknowable axis


# ---------------------------------------------------------------------------
# Env surface — every weight / coefficient tunable, nothing hardcoded
# ---------------------------------------------------------------------------


def master_enabled() -> bool:
    """``JARVIS_CONCEPTION_VALUE_MODEL_ENABLED`` (default true)."""
    return os.environ.get(
        "JARVIS_CONCEPTION_VALUE_MODEL_ENABLED", "true",
    ).strip().lower() in _TRUTHY


def _envf(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except (TypeError, ValueError):
        return default


def _w_align() -> float:
    return max(0.0, _envf("JARVIS_CONCEPTION_W_ALIGN", 0.40))


def _w_substance() -> float:
    return max(0.0, _envf("JARVIS_CONCEPTION_W_SUBSTANCE", 0.35))


def _w_feasibility() -> float:
    return max(0.0, _envf("JARVIS_CONCEPTION_W_FEASIBILITY", 0.25))


def _cost_coeff() -> float:
    """Higher → cost penalised harder. cost_factor = 1/(1+coeff·usd)."""
    return max(0.0, _envf("JARVIS_CONCEPTION_COST_COEFF", 2.0))


def _regression_damp() -> float:
    """Feasibility multiplier when the scope has a fresh regression."""
    v = _envf("JARVIS_CONCEPTION_REGRESSION_DAMP", 0.5)
    return min(1.0, max(0.0, v))


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _clamp01(x: float) -> float:
    try:
        return max(0.0, min(1.0, float(x)))
    except (TypeError, ValueError):
        return _NEUTRAL


# TrustLevel string → feasibility axis. UNKNOWN is NEUTRAL, not disqualifying:
# no track record means "uncertain", not "unworthy of conception".
_TRUST_AXIS: Dict[str, float] = {
    "high": 1.0,
    "medium": 0.7,
    "low": 0.3,
    "unknown": _NEUTRAL,
}

# signal_value band → substance axis. INDETERMINATE (0) is a NEW/unresolvable
# target — neutral, not zero. A PROVEN cosmetic band (1) sits below neutral so
# cosmetic conception is out-competed; executable (2) / oracle (3) climb.
_BAND_AXIS: Dict[int, float] = {
    0: _NEUTRAL,   # BAND_INDETERMINATE
    1: 0.33,       # BAND_COSMETIC_CLASS
    2: 0.66,       # BAND_EXECUTABLE
    3: 1.0,        # BAND_ORACLE
}


# ---------------------------------------------------------------------------
# Axis computations (each never raises; degrades to a neutral prior)
# ---------------------------------------------------------------------------


def _alignment_axis(description: str, project_root: Any) -> Tuple[float, bool]:
    """Strategic fit via the recency-weighted semantic centroid/clusters.

    Returns (value, known). ``known`` is False when the index is cold /
    disabled — the caller then treats alignment as a neutral prior instead of
    a hard zero.
    """
    if not (description or "").strip():
        return _NEUTRAL, False
    try:
        from backend.core.ouroboros.governance.semantic_index import (
            get_default_index,
        )

        idx = get_default_index(project_root)
        detail = idx.score_with_cluster(description)
        if not detail:
            return _NEUTRAL, False
        return _clamp01(detail.get("score", 0.0)), True
    except Exception:  # noqa: BLE001 — cold index is not a value verdict
        logger.debug("[Conception] alignment axis cold", exc_info=True)
        return _NEUTRAL, False


def _substance_axis(
    signal_source: str,
    target_files: Sequence[Any],
    project_root: Any,
) -> Tuple[float, int]:
    """Expected real-code value from the verifiable AST/oracle band.

    Returns (value, band). Verifiable-evidence-first — identical philosophy to
    ``signal_value`` itself; we simply project its band onto [0,1].
    """
    try:
        from backend.core.ouroboros.governance.signal_value import score_signal

        band = int(score_signal(
            str(signal_source or ""), tuple(target_files or ()), "", project_root,
        ))
        return _BAND_AXIS.get(band, _NEUTRAL), band
    except Exception:  # noqa: BLE001
        logger.debug("[Conception] substance axis error", exc_info=True)
        return _NEUTRAL, 0


def _feasibility_axis(scope: str) -> Tuple[float, str, bool]:
    """Earned autonomy for ``scope`` — the Gap-4 → Gap-3 composition.

    Returns (value, trust_level, recent_regression). A fresh regression damps
    the axis (env ``JARVIS_CONCEPTION_REGRESSION_DAMP``) so the organism does
    not pile conception onto a scope it just broke.
    """
    try:
        from backend.core.ouroboros.governance import trust_calibration as tc

        st = tc.scope_trust(scope)
        base = _TRUST_AXIS.get(str(st.trust_level).lower(), _NEUTRAL)
        if st.recent_regression:
            base *= _regression_damp()
        return _clamp01(base), str(st.trust_level), bool(st.recent_regression)
    except Exception:  # noqa: BLE001
        logger.debug("[Conception] feasibility axis cold", exc_info=True)
        return _NEUTRAL, "unknown", False


def _cost_factor(cost_usd: float) -> float:
    """Value-per-dollar. cost_usd=0 → 1.0; grows expensive → 0. Never raises."""
    try:
        c = max(0.0, float(cost_usd))
    except (TypeError, ValueError):
        c = 0.0
    return 1.0 / (1.0 + _cost_coeff() * c)


def _infer_scope(target_files: Sequence[Any]) -> str:
    try:
        from backend.core.ouroboros.governance.trust_calibration import (
            _infer_scope_for,
        )

        return _infer_scope_for([str(t) for t in (target_files or ())])
    except Exception:  # noqa: BLE001
        return "unknown"


# ---------------------------------------------------------------------------
# Result artifact
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExpectedValue:
    """Expected-value verdict for one conceived proposal. ``ev`` ∈ [0,1]."""

    ev: float
    alignment: float
    substance: float
    feasibility: float
    cost_factor: float
    scope: str
    trust_level: str
    band: int
    alignment_known: bool
    recent_regression: bool
    rationale: str

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["schema_version"] = CONCEPTION_VALUE_SCHEMA_VERSION
        return d


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def score_proposal(
    *,
    description: str,
    target_files: Sequence[Any] = (),
    signal_source: str = "auto_proposed",
    estimated_cost_usd: float = 0.0,
    scope: Optional[str] = None,
    project_root: Any = None,
    # injectable seams for tests — default to the real read-only scorers
    align_fn: Optional[Callable[[str, Any], Tuple[float, bool]]] = None,
    substance_fn: Optional[Callable[[str, Sequence[Any], Any], Tuple[float, int]]] = None,
    feasibility_fn: Optional[Callable[[str], Tuple[float, str, bool]]] = None,
) -> ExpectedValue:
    """Expected value of a conceived proposal. Never raises.

    When the model is disabled every axis reports its neutral prior and EV is
    the un-weighted-cost-scaled neutral — inert but well-formed.
    """
    root = project_root if project_root is not None else (
        os.environ.get("JARVIS_PROJECT_ROOT", "") or os.getcwd()
    )
    sc = scope or _infer_scope(target_files)

    if not master_enabled():
        cf = _cost_factor(estimated_cost_usd)
        return ExpectedValue(
            ev=_clamp01(_NEUTRAL * cf), alignment=_NEUTRAL, substance=_NEUTRAL,
            feasibility=_NEUTRAL, cost_factor=cf, scope=sc, trust_level="unknown",
            band=0, alignment_known=False, recent_regression=False,
            rationale="model_disabled",
        )

    a, a_known = (align_fn or _alignment_axis)(description, root)
    s, band = (substance_fn or _substance_axis)(signal_source, target_files, root)
    f, trust_level, regressed = (feasibility_fn or _feasibility_axis)(sc)
    cf = _cost_factor(estimated_cost_usd)

    wa, ws, wf = _w_align(), _w_substance(), _w_feasibility()
    wsum = wa + ws + wf
    weighted = (wa * a + ws * s + wf * f) / wsum if wsum > 0 else _NEUTRAL
    ev = _clamp01(cf * weighted)

    rationale = (
        f"A={a:.2f}{'' if a_known else '~'} S={s:.2f}(b{band}) "
        f"F={f:.2f}({trust_level}{'!' if regressed else ''}) "
        f"cost×{cf:.2f} → EV={ev:.3f}"
    )
    return ExpectedValue(
        ev=ev, alignment=a, substance=s, feasibility=f, cost_factor=cf,
        scope=sc, trust_level=trust_level, band=band, alignment_known=a_known,
        recent_regression=regressed, rationale=rationale,
    )


def score_blueprint(blueprint: Any, *, project_root: Any = None) -> ExpectedValue:
    """Adapter: score a DreamEngine ``ImprovementBlueprint`` (the discarded
    scores this model exists to recover). Reads description / target_files /
    estimated_cost_usd off the blueprint; unknown shapes degrade to neutral.
    """
    return score_proposal(
        description=str(getattr(blueprint, "description", "") or getattr(blueprint, "title", "")),
        target_files=getattr(blueprint, "target_files", ()) or (),
        signal_source="auto_proposed",
        estimated_cost_usd=getattr(blueprint, "estimated_cost_usd", 0.0) or 0.0,
        project_root=project_root,
    )


def rank_proposals(
    proposals: Sequence[Tuple[Any, ExpectedValue]],
) -> List[Tuple[Any, ExpectedValue]]:
    """Sort (item, ExpectedValue) pairs by EV desc. Stable; never raises."""
    try:
        return sorted(proposals, key=lambda pe: pe[1].ev, reverse=True)
    except Exception:  # noqa: BLE001
        return list(proposals)


def priority_hint_for(
    *,
    description: str,
    target_files: Sequence[Any] = (),
    signal_source: str = "auto_proposed",
    estimated_cost_usd: float = 0.0,
    project_root: Any = None,
) -> float:
    """Convenience: the EV as a ``proactive_proposal_surface`` priority_hint
    ∈ [0,1]. This is the one-line seam the proposal producers call."""
    return score_proposal(
        description=description, target_files=target_files,
        signal_source=signal_source, estimated_cost_usd=estimated_cost_usd,
        project_root=project_root,
    ).ev


def snapshot() -> Dict[str, Any]:
    """Read-only observability projection of the model's configuration."""
    return {
        "schema_version": CONCEPTION_VALUE_SCHEMA_VERSION,
        "enabled": master_enabled(),
        "weights": {
            "alignment": _w_align(),
            "substance": _w_substance(),
            "feasibility": _w_feasibility(),
        },
        "cost_coeff": _cost_coeff(),
        "regression_damp": _regression_damp(),
        "neutral_prior": _NEUTRAL,
        "axes": {
            "alignment": "semantic_index.score_with_cluster",
            "substance": "signal_value.score_signal",
            "feasibility": "trust_calibration.scope_trust",
            "cost": "1/(1+coeff·usd)",
        },
    }
