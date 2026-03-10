"""
RouteDecisionService — Authoritative async task→brain router.

Replaces the regex Layer-1 (Task Gate) in BrainSelector with a
CAI-intent-aware decision produced by IntelligentModelSelector.
Preserves Layers 2 (Resource Gate) and 3 (Cost Gate) from BrainSelector.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

from backend.core.ouroboros.governance.brain_selector import (
    BrainSelector,
    BrainSelectionResult,
    TaskComplexity,
)
from backend.core.ouroboros.governance.resource_monitor import ResourceSnapshot

logger = logging.getLogger("Ouroboros.RouteDecisionService")


# Maps CAI intent → (TaskComplexity, brain_id)
#
# NOTE on brain_id namespace: these identifiers ("phi3_lightweight", "mistral_planning",
# "qwen_coder", "deepseek_r1") match BrainSelector's internal brain registry, NOT the
# J-Prime model_catalogue.yaml brain_id field (which uses separate identifiers such as
# "mistral_7b_fallback").  The authoritative bridge between the two namespaces is the
# TaskProfile.model field (e.g. "mistral-7b"), which BrainSelector populates via its
# model_name map and which ModelDispatcher uses for GGUF catalogue lookup.
_INTENT_TO_BRAIN: Dict[str, Tuple[TaskComplexity, str]] = {
    "single_line_change":   (TaskComplexity.TRIVIAL,    "phi3_lightweight"),
    "docs_edit":            (TaskComplexity.TRIVIAL,    "phi3_lightweight"),
    "comment_append":       (TaskComplexity.TRIVIAL,    "phi3_lightweight"),
    "code_generation":      (TaskComplexity.HEAVY_CODE, "qwen_coder"),
    "bug_fix":              (TaskComplexity.LIGHT,      "mistral_planning"),
    "segfault_analysis":    (TaskComplexity.COMPLEX,    "deepseek_r1"),
    "heavy_refactor":       (TaskComplexity.HEAVY_CODE, "qwen_coder"),
    "architecture_design":  (TaskComplexity.COMPLEX,    "deepseek_r1"),
    # voice/fallback intents → light path
    "code_explanation":     (TaskComplexity.LIGHT,      "mistral_planning"),
    "nlp_analysis":         (TaskComplexity.LIGHT,      "mistral_planning"),
}


class RouteDecisionService:
    """
    Async authoritative router: task description → BrainSelectionResult.

    Layer 1 (Task Gate) is now driven by CAI intent via IntelligentModelSelector.
    Layers 2 + 3 (Resource + Cost gates) delegate to the existing BrainSelector.
    """

    def __init__(self, brain_selector: Optional[BrainSelector] = None) -> None:
        self._brain_selector = brain_selector or BrainSelector()
        self._selector: Optional[Any] = None  # lazy-init IntelligentModelSelector

    def _get_selector(self) -> Optional[Any]:
        """Lazy-init IntelligentModelSelector to avoid circular imports at module load."""
        if self._selector is None:
            try:
                from backend.intelligence.model_selector import IntelligentModelSelector
                self._selector = IntelligentModelSelector()
            except Exception as exc:
                logger.warning("[RouteDecision] IntelligentModelSelector unavailable: %s", exc)
        return self._selector

    async def select(
        self,
        description: str,
        target_files: Tuple[str, ...],
        snapshot: ResourceSnapshot,
        blast_radius: int = 1,
    ) -> BrainSelectionResult:
        """
        Async replacement for BrainSelector.select().

        1. Classify intent via CAI (IntelligentModelSelector)
        2. Map intent → (complexity, brain_id)
        3. Delegate Layers 2+3 to BrainSelector with the override complexity
        """
        intent, complexity, brain_override = await self._classify(description)

        logger.debug(
            "[RouteDecision] intent=%s complexity=%s brain_override=%s",
            intent, complexity.value if complexity else None, brain_override,
        )

        # If intent classified successfully, override Layer-1 result
        if complexity is not None and brain_override is not None:
            return self._brain_selector._apply_resource_and_cost_gates(
                brain_override, complexity, description, target_files, snapshot,
                blast_radius, f"cai_intent_{intent}",
            )

        # Fallback: full BrainSelector (regex Layer-1 + gates)
        return self._brain_selector.select(
            description=description,
            target_files=target_files,
            snapshot=snapshot,
            blast_radius=blast_radius,
        )

    async def _classify(
        self, description: str
    ) -> Tuple[str, Optional[TaskComplexity], Optional[str]]:
        """Returns (intent, complexity, brain_id). All three None on failure."""
        selector = self._get_selector()
        if selector is None:
            return "unknown", None, None
        try:
            intent = await selector._classify_intent(description)
            row = _INTENT_TO_BRAIN.get(intent)
            if row is None:
                return intent, None, None
            complexity, brain_id = row
            return intent, complexity, brain_id
        except Exception as exc:
            logger.warning("[RouteDecision] Intent classification failed: %s", exc)
            return "unknown", None, None

    @property
    def daily_spend(self) -> float:
        return self._brain_selector.daily_spend

    @property
    def daily_spend_breakdown(self) -> Dict[str, float]:
        return self._brain_selector.daily_spend_breakdown

    def record_cost(self, provider: str, cost_usd: float) -> None:
        self._brain_selector.record_cost(provider, cost_usd)
