# backend/core/ouroboros/governance/vindication_reflector.py
"""
VindicationReflector -- Forward-looking RSI trajectory analysis
================================================================

Answers: "Will this patch make future patches better or worse?"

Based on Fallenstein & Soares' Vingean reflection (cited in Wang's RSI
paper).  Three metrics are weighted and combined into a single
``vindication_score`` in [-1, 1]:

  * Coupling trajectory  (weight 0.40)
  * Blast-radius trajectory (weight 0.35)
  * Complexity / entropy trajectory (weight 0.25)

Advisory only -- never blocks patches.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, List

logger = logging.getLogger("Ouroboros.VindicationReflector")

# ---------------------------------------------------------------------------
# Weight constants
# ---------------------------------------------------------------------------

_W_COUPLING: float = 0.40
_W_BLAST: float = 0.35
_W_ENTROPY: float = 0.25

# ---------------------------------------------------------------------------
# Advisory thresholds
# ---------------------------------------------------------------------------

_VINDICATING_THRESHOLD: float = 0.2
_CONCERNING_THRESHOLD: float = -0.2
_WARNING_THRESHOLD: float = -0.5


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VindicationResult:
    """Immutable result of a vindication reflection pass.

    Attributes
    ----------
    vindication_score:
        Scalar in [-1, 1].  Positive means the patch improves future
        tractability; negative means it degrades it.
    coupling_delta:
        Normalised change in inter-module coupling (negative = improvement).
    blast_radius_delta:
        Normalised change in blast radius (negative = improvement).
    entropy_delta:
        Normalised change in code complexity (negative = improvement).
    advisory:
        Human-readable classification: ``"vindicating"``, ``"neutral"``,
        ``"concerning"``, or ``"warning"``.
    """

    vindication_score: float
    coupling_delta: float
    blast_radius_delta: float
    entropy_delta: float
    advisory: str


# ---------------------------------------------------------------------------
# Neutral sentinel (oracle failure fallback)
# ---------------------------------------------------------------------------

_NEUTRAL = VindicationResult(
    vindication_score=0.0,
    coupling_delta=0.0,
    blast_radius_delta=0.0,
    entropy_delta=0.0,
    advisory="neutral",
)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _clamp(value: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def _advisory(score: float) -> str:
    if score > _VINDICATING_THRESHOLD:
        return "vindicating"
    if score < _WARNING_THRESHOLD:
        return "warning"
    if score < _CONCERNING_THRESHOLD:
        return "concerning"
    return "neutral"


# ---------------------------------------------------------------------------
# VindicationReflector
# ---------------------------------------------------------------------------


class VindicationReflector:
    """Compute forward-looking trajectory scores for a proposed patch.

    Parameters
    ----------
    oracle:
        Any object that exposes::

            get_dependencies(file_path: str)  -> List[Any]
            get_dependents(file_path: str)    -> List[Any]
            compute_blast_radius(file_path: str) -> Any  # .total_affected

        A ``CodebaseKnowledgeGraph`` satisfies this interface.
    """

    def __init__(self, oracle: Any) -> None:
        self._oracle = oracle

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def reflect(
        self,
        target_files: List[str],
        coupling_after: float,
        blast_radius_after: float,
        complexity_after: float,
        complexity_before: float,
    ) -> VindicationResult:
        """Compute a VindicationResult for the given patch.

        Parameters
        ----------
        target_files:
            Files touched by the patch.
        coupling_after:
            Total coupling count (deps + dependents) across all target
            files *after* the patch.
        blast_radius_after:
            Aggregate blast-radius (total_affected) *after* the patch.
        complexity_after:
            Aggregate complexity metric *after* the patch.
        complexity_before:
            Aggregate complexity metric *before* the patch.

        Returns
        -------
        VindicationResult
            ``advisory="neutral"`` and ``score=0.0`` on oracle failure.
        """
        try:
            return self._compute(
                target_files=target_files,
                coupling_after=coupling_after,
                blast_radius_after=blast_radius_after,
                complexity_after=complexity_after,
                complexity_before=complexity_before,
            )
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning(
                "VindicationReflector: oracle error -- returning neutral. %s", exc
            )
            return _NEUTRAL

    # ------------------------------------------------------------------
    # Internal computation
    # ------------------------------------------------------------------

    def _compute(
        self,
        target_files: List[str],
        coupling_after: float,
        blast_radius_after: float,
        complexity_after: float,
        complexity_before: float,
    ) -> VindicationResult:
        # ---- Coupling (before) -----------------------------------------
        coupling_before: float = 0.0
        for fpath in target_files:
            deps = self._oracle.get_dependencies(fpath)
            dependents = self._oracle.get_dependents(fpath)
            coupling_before += len(deps) + len(dependents)

        coupling_delta = _clamp(
            (coupling_after - coupling_before) / max(1.0, coupling_before)
        )

        # ---- Blast radius (before) -------------------------------------
        br_before: float = 0.0
        for fpath in target_files:
            result = self._oracle.compute_blast_radius(fpath)
            br_before = max(br_before, result.total_affected)

        br_delta = _clamp(
            (blast_radius_after - br_before) / max(1.0, br_before)
        )

        # ---- Complexity (entropy) -------------------------------------
        entropy_delta = _clamp(
            (complexity_after - complexity_before) / max(1.0, complexity_before)
        )

        # ---- Vindication score ----------------------------------------
        raw_score = -1.0 * (
            _W_COUPLING * coupling_delta
            + _W_BLAST * br_delta
            + _W_ENTROPY * entropy_delta
        )
        score = round(_clamp(raw_score), 4)

        return VindicationResult(
            vindication_score=score,
            coupling_delta=coupling_delta,
            blast_radius_delta=br_delta,
            entropy_delta=entropy_delta,
            advisory=_advisory(score),
        )
