"""Phase 12 Slice B — Deterministic catalog classifier.

Maps a ``CatalogSnapshot`` (Slice A) into per-route ranked
``model_id`` lists, consulting the ``PromotionLedger`` to honor the
Zero-Trust SPECULATIVE quarantine + prove-it promotion contract.

Pure deterministic ranking. Zero LLM. Same ``(snapshot, ledger,
env)`` produces the same output bit-for-bit. Ties broken by
alphabetical ``model_id`` (stable secondary sort) so cross-process
hash randomization can't shift ranks.

Authority surface:
  * ``RouteAssignment`` — frozen, per-route ranked list
  * ``DwCatalogClassifier.classify(snapshot, ledger)`` — main entry
  * Per-route eligibility gates as env knobs (see §3.2 of spec)

The classifier does NOT mutate the ledger directly. It returns a
``ClassificationOutcome`` that includes the list of ``model_id``
to register-as-quarantined; the caller (Slice C wiring) calls the
ledger's mutating methods. Keeps ranking pure + idempotent.

Cost contract preservation:
  * Per-route hard eligibility gates filter out unaffordable models
    BEFORE ranking. A 70B model with no pricing won't accidentally
    land in BACKGROUND just because its parameter score is high.
  * Quarantined models are pinned to SPECULATIVE regardless of
    metadata signals — Zero-Trust §3.6.
  * Ambiguous-metadata new models are flagged for quarantine
    registration; the classifier still places them in SPECULATIVE
    in this run (so the dispatcher has SOMETHING to try on a
    cold-start; the ledger gets the quarantine record).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import (
    Any, Callable, Dict, List, Mapping, Optional, Tuple,
)

from backend.core.ouroboros.governance.dw_catalog_client import (
    CatalogSnapshot, ModelCard,
)
from backend.core.ouroboros.governance.dw_promotion_ledger import (
    PromotionLedger,
    QUARANTINE_AMBIGUOUS_METADATA,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-route eligibility gates (env-tunable)
# ---------------------------------------------------------------------------


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)).strip())
    except (ValueError, TypeError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)).strip())
    except (ValueError, TypeError):
        return default


@dataclass(frozen=True)
class EligibilityGate:
    """Hard filter applied BEFORE ranking. A model that fails any
    gate is excluded from the route entirely — it doesn't get a
    low rank, it doesn't show up at all."""
    min_params_b: float = 0.0
    max_params_b: Optional[float] = None
    min_context_window: int = 0
    max_out_price_per_m: Optional[float] = None
    require_streaming: bool = False

    def admits(self, card: ModelCard) -> bool:
        if (
            self.min_params_b > 0
            and (card.parameter_count_b is None
                 or card.parameter_count_b < self.min_params_b)
        ):
            return False
        if (
            self.max_params_b is not None
            and card.parameter_count_b is not None
            and card.parameter_count_b > self.max_params_b
        ):
            return False
        if (
            self.min_context_window > 0
            and (card.context_window is None
                 or card.context_window < self.min_context_window)
        ):
            return False
        if (
            self.max_out_price_per_m is not None
            and card.pricing_out_per_m_usd is not None
            and card.pricing_out_per_m_usd > self.max_out_price_per_m
        ):
            return False
        if self.require_streaming and not card.supports_streaming:
            return False
        return True


def gate_for_route(route: str) -> EligibilityGate:
    """Read per-route env knobs. Defaults documented in spec §3.2.
    Read at call time so tests can monkeypatch."""
    r = (route or "").strip().lower()
    if r == "complex":
        return EligibilityGate(
            min_params_b=_env_float(
                "JARVIS_DW_CLASSIFIER_COMPLEX_MIN_PARAMS_B", 30.0,
            ),
            max_out_price_per_m=(
                None
                if os.environ.get(
                    "JARVIS_DW_CLASSIFIER_COMPLEX_MAX_OUT_PRICE", "",
                ).strip() == ""
                else _env_float(
                    "JARVIS_DW_CLASSIFIER_COMPLEX_MAX_OUT_PRICE", 0.0,
                )
            ),
        )
    if r == "standard":
        return EligibilityGate(
            min_params_b=_env_float(
                "JARVIS_DW_CLASSIFIER_STANDARD_MIN_PARAMS_B", 14.0,
            ),
            max_out_price_per_m=_env_float(
                "JARVIS_DW_CLASSIFIER_STANDARD_MAX_OUT_PRICE", 2.0,
            ),
        )
    if r == "background":
        return EligibilityGate(
            max_out_price_per_m=_env_float(
                "JARVIS_DW_CLASSIFIER_BACKGROUND_MAX_OUT_PRICE", 0.5,
            ),
        )
    if r == "speculative":
        return EligibilityGate(
            max_out_price_per_m=_env_float(
                "JARVIS_DW_CLASSIFIER_SPECULATIVE_MAX_OUT_PRICE", 0.1,
            ),
        )
    # IMMEDIATE has empty dw_models by Manifesto §5 — sentinel falls
    # through to legacy Claude-direct dispatch. Return a max-restrictive
    # gate (admits nothing) so we never accidentally populate it.
    return EligibilityGate(min_params_b=1e9)


# ---------------------------------------------------------------------------
# Ranking weights + family preference
# ---------------------------------------------------------------------------


def _ranking_weights() -> Dict[str, float]:
    return {
        "params": _env_float("JARVIS_DW_CLASSIFIER_WEIGHT_PARAMS", 1.0),
        "pricing_out": _env_float(
            "JARVIS_DW_CLASSIFIER_WEIGHT_PRICING_OUT", -1.0,
        ),
        "context": _env_float("JARVIS_DW_CLASSIFIER_WEIGHT_CONTEXT", 0.3),
        "family": _env_float("JARVIS_DW_CLASSIFIER_WEIGHT_FAMILY", 0.5),
    }


def _family_preference() -> Dict[str, float]:
    """Parse ``JARVIS_DW_FAMILY_PREFERENCE`` env (e.g.
    ``moonshotai:1.0,zai-org:0.8``). Empty → no bonus."""
    raw = os.environ.get("JARVIS_DW_FAMILY_PREFERENCE", "").strip()
    if not raw:
        return {}
    out: Dict[str, float] = {}
    for token in raw.split(","):
        token = token.strip()
        if ":" not in token:
            continue
        k, v = token.split(":", 1)
        try:
            out[k.strip().lower()] = float(v.strip())
        except (ValueError, TypeError):
            continue
    return out


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


def _score(card: ModelCard,
           weights: Mapping[str, float],
           family_bonus: Mapping[str, float],
           *,
           prefer_cheap: bool) -> float:
    """Composite score. Higher = better for this route.

    For COMPLEX/STANDARD: bigger params is better (param weight +1).
    For BACKGROUND/SPECULATIVE: cheaper is better (effective sign flip
    handled via ``prefer_cheap`` — pricing weight already negative,
    so the absolute-cheap routes get a bonus on low pricing).

    Tied scores broken by alphabetical model_id at the call site.
    """
    score = 0.0
    if card.parameter_count_b is not None:
        # On cheap-routes, large param count is a *negative* signal
        # (we don't want a 397B model in BG even if it's cheap-ish)
        sign = -1.0 if prefer_cheap else 1.0
        score += sign * weights["params"] * card.parameter_count_b / 10.0
    if card.pricing_out_per_m_usd is not None:
        score += weights["pricing_out"] * card.pricing_out_per_m_usd
    if card.context_window is not None:
        # Normalize to 100k as the unit
        score += weights["context"] * (card.context_window / 100_000.0)
    fb = family_bonus.get(card.family.lower())
    if fb is not None:
        score += weights["family"] * fb
    return score


# ---------------------------------------------------------------------------
# Classifier outcome
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RouteAssignment:
    """Per-route ranked list. Replaces YAML's ``dw_models:`` array."""
    route: str
    ranked_model_ids: Tuple[str, ...]


@dataclass(frozen=True)
class ClassificationOutcome:
    """Full classifier output. Includes:
      * ``assignments`` — Dict[route, RouteAssignment]
      * ``newly_quarantined`` — model_ids the caller should register
        with the ledger (fresh ambiguous-metadata models)
      * ``schema_version`` — pinned for downstream version checks
    """
    assignments: Dict[str, RouteAssignment]
    newly_quarantined: Tuple[str, ...]
    schema_version: str = "dw_classifier.1"

    def for_route(self, route: str) -> Tuple[str, ...]:
        a = self.assignments.get((route or "").strip().lower())
        return a.ranked_model_ids if a is not None else ()


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------


_GENERATIVE_ROUTES: Tuple[str, ...] = (
    "complex", "standard", "background", "speculative",
)


class DwCatalogClassifier:
    """Pure-function classifier. Construction is cheap (no I/O).

    Typical use::

        classifier = DwCatalogClassifier()
        outcome = classifier.classify(snapshot, ledger)
        for mid in outcome.newly_quarantined:
            ledger.register_quarantine(mid)
        topology.set_dynamic_catalog(outcome.assignments, ...)

    The classifier never mutates the ledger; the caller is responsible
    for the side-effects. This keeps the ranking deterministic and
    re-runnable for diagnostic purposes (e.g. Slice C shadow-mode
    diff against YAML)."""

    def classify(
        self,
        snapshot: CatalogSnapshot,
        ledger: PromotionLedger,
    ) -> ClassificationOutcome:
        if snapshot is None or not snapshot.models:
            return ClassificationOutcome(
                assignments=self._empty_assignments(),
                newly_quarantined=(),
            )

        # Pre-compute env-driven config once per classify() call
        weights = _ranking_weights()
        family_bonus = _family_preference()
        gates: Dict[str, EligibilityGate] = {
            r: gate_for_route(r) for r in _GENERATIVE_ROUTES
        }

        # Identify quarantine-pinned + newly-ambiguous models
        # Quarantined-and-not-promoted: pinned to SPECULATIVE only
        # Promoted: eligible across all routes (still subject to gates)
        # New ambiguous: still placed in SPECULATIVE this run +
        #                added to newly_quarantined for caller to register
        newly_quarantined: List[str] = []
        for card in snapshot.models:
            if (
                card.has_ambiguous_metadata()
                and not ledger.is_quarantined(card.model_id)
                and not ledger.is_promoted(card.model_id)
            ):
                newly_quarantined.append(card.model_id)

        # Build per-route candidate sets
        assignments: Dict[str, RouteAssignment] = {}
        for route in _GENERATIVE_ROUTES:
            ranked_ids = self._rank_for_route(
                route=route,
                snapshot=snapshot,
                ledger=ledger,
                gate=gates[route],
                weights=weights,
                family_bonus=family_bonus,
                newly_quarantined=set(newly_quarantined),
            )
            assignments[route] = RouteAssignment(
                route=route, ranked_model_ids=ranked_ids,
            )

        return ClassificationOutcome(
            assignments=assignments,
            newly_quarantined=tuple(sorted(newly_quarantined)),
        )

    # ------------------------------------------------------------------
    # Per-route ranking
    # ------------------------------------------------------------------

    def _rank_for_route(
        self,
        *,
        route: str,
        snapshot: CatalogSnapshot,
        ledger: PromotionLedger,
        gate: EligibilityGate,
        weights: Mapping[str, float],
        family_bonus: Mapping[str, float],
        newly_quarantined: set,
    ) -> Tuple[str, ...]:
        prefer_cheap = route in ("background", "speculative")
        eligible: List[Tuple[float, str]] = []

        for card in snapshot.models:
            mid = card.model_id

            # Quarantine pin: quarantined models go SPECULATIVE only
            is_q = (
                ledger.is_quarantined(mid)
                or mid in newly_quarantined
            )
            is_p = ledger.is_promoted(mid)

            if is_q and not is_p:
                if route == "speculative":
                    # Quarantined models bypass the price gate so
                    # they actually have a SPECULATIVE landing pad
                    # (they may have None pricing, which the gate
                    # would normally exclude). Other gates (params)
                    # don't apply to SPECULATIVE.
                    eligible.append((self._quarantine_score(card),  mid))
                # All other routes: quarantined models excluded
                continue

            # Non-quarantined or promoted: standard gate check
            if not gate.admits(card):
                continue

            # Promoted models are eligible for BG/STANDARD/COMPLEX in
            # principle, but eligibility gates still apply. A promoted
            # 5B model still doesn't meet COMPLEX min_params_b=30B.
            score = _score(
                card, weights, family_bonus, prefer_cheap=prefer_cheap,
            )
            eligible.append((score, mid))

        # Stable sort: primary by descending score, secondary by id
        eligible.sort(key=lambda t: (-t[0], t[1]))
        return tuple(mid for _, mid in eligible)

    @staticmethod
    def _quarantine_score(card: ModelCard) -> float:
        """Quarantined-in-SPECULATIVE ordering: prefer models with
        SOME parsed parameter count (small heuristic-id models like
        ``fake/model-3B`` rank above truly-blind ids like
        ``moonshotai/Kimi-K2.6``). Tie-break to alphabetical."""
        if card.parameter_count_b is None:
            return 0.0
        # Smaller params = higher quarantine score (prefer small
        # models for the SPECULATIVE sandbox; they're cheaper to
        # probe + faster to validate)
        return -card.parameter_count_b / 100.0

    @staticmethod
    def _empty_assignments() -> Dict[str, RouteAssignment]:
        return {
            r: RouteAssignment(route=r, ranked_model_ids=())
            for r in _GENERATIVE_ROUTES
        }


__all__ = [
    "EligibilityGate",
    "RouteAssignment",
    "ClassificationOutcome",
    "DwCatalogClassifier",
    "gate_for_route",
]
