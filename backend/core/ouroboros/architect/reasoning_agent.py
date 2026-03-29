"""
ArchitectureReasoningAgent
===========================

Entry point for the Ouroboros architect subsystem.  Determines which
hypotheses warrant architectural design and (v1) logs qualifying hypotheses
while deferring plan generation until the model bridge is complete.

Design principles:
- Threshold filtering is the sole gate in v1.  The agent accepts any object
  exposing ``.gap_type`` and ``.confidence`` attributes (duck typing) so tests
  and downstream callers can pass real ``FeatureHypothesis`` instances or
  lightweight mocks interchangeably.
- No hardcoded model URLs, provider details, or pipeline steps.  Configuration
  flows through ``AgentConfig``; model-specific behaviour lives in the bridge
  layer (not yet built).
- ``design()`` is already ``async`` so the signature is stable.  When the
  model bridge is wired, the body is the only thing that changes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from backend.core.ouroboros.architect.plan import ArchitecturalPlan

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ARCHITECTURAL_GAP_TYPES: frozenset = frozenset({
    "missing_capability",
    "manifesto_violation",
})

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class AgentConfig:
    """Tuneable knobs for :class:`ArchitectureReasoningAgent`.

    Parameters
    ----------
    min_confidence:
        Minimum hypothesis confidence required to proceed to design.
        Hypotheses below this threshold are filtered out immediately.
    model:
        Primary model identifier used when the model bridge is active.
    fallback_model:
        Model identifier used when the primary model is unavailable.
    max_steps:
        Upper bound on the number of ``PlanStep`` objects the agent may
        produce per plan (enforced by the model bridge, advisory in v1).
    """

    min_confidence: float = 0.7
    model: str = "doubleword-397b"
    fallback_model: str = "claude-api"
    max_steps: int = 10


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class ArchitectureReasoningAgent:
    """Decides which hypotheses qualify for architectural design and (v1)
    logs them while deferring actual plan generation.

    Parameters
    ----------
    oracle:
        The Oracle instance used for codebase context lookup.  Injected so the
        agent can request file neighbourhood data when generating plans.
    doubleword:
        The Doubleword provider client.  Injected to support model-driven plan
        generation when the bridge is complete.
    config:
        Agent configuration.  Defaults to :class:`AgentConfig` defaults.
    """

    def __init__(
        self,
        oracle: Any,
        doubleword: Any,
        config: AgentConfig = AgentConfig(),  # noqa: B008
    ) -> None:
        self._oracle = oracle
        self._doubleword = doubleword
        self._config = config

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def should_design(self, hypothesis: Any) -> bool:
        """Return ``True`` if *hypothesis* clears the architectural threshold.

        Two conditions must both hold:

        1. ``hypothesis.gap_type`` is one of the recognised architectural gap
           types (``missing_capability`` or ``manifesto_violation``).
        2. ``hypothesis.confidence`` is at or above ``config.min_confidence``.

        Deliberately duck-typed: accepts any object with ``.gap_type`` and
        ``.confidence`` attributes.
        """
        if hypothesis.gap_type not in _ARCHITECTURAL_GAP_TYPES:
            logger.debug(
                "should_design: filtering hypothesis — gap_type %r not architectural",
                hypothesis.gap_type,
            )
            return False

        if hypothesis.confidence < self._config.min_confidence:
            logger.debug(
                "should_design: filtering hypothesis — confidence %.3f < min %.3f",
                hypothesis.confidence,
                self._config.min_confidence,
            )
            return False

        return True

    async def design(
        self,
        hypothesis: Any,
        snapshot: Any,
        oracle: Any,
    ) -> Optional[ArchitecturalPlan]:
        """Attempt to produce an :class:`ArchitecturalPlan` for *hypothesis*.

        v1 behaviour
        ------------
        - If the hypothesis does not pass :meth:`should_design`, return ``None``
          immediately.
        - If the hypothesis qualifies, log that it is ready for model-based
          design and return ``None``.  Plan generation is deferred until the
          OperationContext / model bridge is wired (Task 9+).

        The ``snapshot`` and ``oracle`` parameters are accepted now so that the
        method signature is stable and callers do not need updating when the
        bridge lands.
        """
        if not self.should_design(hypothesis):
            return None

        logger.info(
            "design: hypothesis %r qualifies (gap_type=%r, confidence=%.3f) — "
            "model integration pending, no plan generated",
            getattr(hypothesis, "hypothesis_id", repr(hypothesis)),
            hypothesis.gap_type,
            hypothesis.confidence,
        )
        # The infrastructure is ready — PlanValidator, PlanStore, and
        # SagaOrchestrator will consume ArchitecturalPlan objects once the
        # model bridge (OperationContext wiring) is complete.
        return None

    def health(self) -> dict:
        """Return a dict describing the agent's configuration and readiness.

        Always includes ``"model_integration": "pending"`` in v1 to signal
        that plan generation is not yet active.
        """
        return {
            "min_confidence": self._config.min_confidence,
            "model": self._config.model,
            "fallback_model": self._config.fallback_model,
            "max_steps": self._config.max_steps,
            "architectural_gap_types": sorted(_ARCHITECTURAL_GAP_TYPES),
            "model_integration": "pending",
        }
