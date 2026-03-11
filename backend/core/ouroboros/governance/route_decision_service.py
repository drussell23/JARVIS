"""
RouteDecisionService — Authoritative async task→brain router.

Replaces the regex Layer-1 (Task Gate) in BrainSelector with a
CAI-intent-aware decision produced by IntelligentModelSelector.
Preserves Layers 2 (Resource Gate) and 3 (Cost Gate) from BrainSelector.

Intelligence integration:
  - CAI (Context Awareness Intelligence): intent classification → brain_id
  - SAI (Self-Aware Intelligence): system health → downgrade from 32B if under pressure
  - UAE (Unified Awareness Engine): fusion score → tiebreaker for borderline CAI confidence
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, Optional, Tuple

from backend.core.ouroboros.governance.brain_selector import (
    BrainSelector,
    BrainSelection,
    BrainSelectionResult,
    TaskComplexity,
)
from backend.core.ouroboros.governance.resource_monitor import ResourceSnapshot

logger = logging.getLogger("Ouroboros.RouteDecisionService")

# SAI health-aware downgrade: when SAI reports adaptive_backpressure above this
# threshold, downgrade from 32B to 14B (or 14B to 7B) to reduce VM load.
_SAI_BACKPRESSURE_DOWNGRADE_THRESHOLD = float(
    os.environ.get("OUROBOROS_SAI_DOWNGRADE_THRESHOLD", "0.6")
)

# Downgrade map: brain_id → lighter alternative when SAI indicates pressure
_DOWNGRADE_MAP: Dict[str, str] = {
    "qwen_coder_32b": "qwen_coder_14b",
    "qwen_coder_14b": "qwen_coder",
    "deepseek_r1": "qwen_coder",
}

# Maps CAI intent → (TaskComplexity, brain_id)
#
# brain_id identifiers match BrainSelector's internal brain registry.
# The 32B model handles heavy code and complex tasks; 7B handles light work.
_INTENT_TO_BRAIN: Dict[str, Tuple[TaskComplexity, str]] = {
    "single_line_change":   (TaskComplexity.TRIVIAL,    "phi3_lightweight"),
    "docs_edit":            (TaskComplexity.TRIVIAL,    "phi3_lightweight"),
    "comment_append":       (TaskComplexity.TRIVIAL,    "phi3_lightweight"),
    "code_generation":      (TaskComplexity.HEAVY_CODE, "qwen_coder_32b"),
    "bug_fix":              (TaskComplexity.LIGHT,      "qwen_coder"),
    "segfault_analysis":    (TaskComplexity.COMPLEX,    "qwen_coder_32b"),
    "heavy_refactor":       (TaskComplexity.HEAVY_CODE, "qwen_coder_32b"),
    "architecture_design":  (TaskComplexity.COMPLEX,    "qwen_coder_32b"),
    # voice/fallback intents → light path
    "code_explanation":     (TaskComplexity.LIGHT,      "qwen_coder"),
    "nlp_analysis":         (TaskComplexity.LIGHT,      "qwen_coder"),
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
        self._sai: Optional[Any] = None       # lazy-init SelfAwareIntelligence

    def _get_selector(self) -> Optional[Any]:
        """Lazy-init IntelligentModelSelector to avoid circular imports at module load."""
        if self._selector is None:
            try:
                from backend.intelligence.model_selector import IntelligentModelSelector
                self._selector = IntelligentModelSelector()
            except Exception as exc:
                logger.warning("[RouteDecision] IntelligentModelSelector unavailable: %s", exc)
        return self._selector

    def _get_sai(self) -> Optional[Any]:
        """Lazy-init SelfAwareIntelligence for health-aware routing decisions."""
        if self._sai is None:
            try:
                from backend.intelligence.self_aware_intelligence import SelfAwareIntelligence
                self._sai = SelfAwareIntelligence()
            except Exception as exc:
                logger.debug("[RouteDecision] SAI unavailable: %s", exc)
        return self._sai

    async def _sai_should_downgrade(self) -> bool:
        """Check SAI system health — returns True if VM is under pressure.

        Non-blocking with 200ms timeout.  Returns False if SAI is unavailable
        or times out (fail-open: default to the higher-capability model).
        """
        sai = self._get_sai()
        if sai is None:
            return False
        try:
            state = await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(None, sai.get_cognitive_state),
                timeout=0.2,
            )
            backpressure = state.get("adaptive_backpressure", 0.0) if state else 0.0
            if backpressure > _SAI_BACKPRESSURE_DOWNGRADE_THRESHOLD:
                logger.info(
                    "[RouteDecision] SAI backpressure=%.2f > %.2f — downgrading model",
                    backpressure, _SAI_BACKPRESSURE_DOWNGRADE_THRESHOLD,
                )
                return True
        except (asyncio.TimeoutError, Exception) as exc:
            logger.debug("[RouteDecision] SAI health check skipped: %s", exc)
        return False

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
            # SAI health-aware downgrade: if VM under pressure, prefer lighter model
            if brain_override in _DOWNGRADE_MAP:
                should_downgrade = await self._sai_should_downgrade()
                if should_downgrade:
                    original = brain_override
                    brain_override = _DOWNGRADE_MAP[brain_override]
                    logger.info(
                        "[RouteDecision] SAI downgrade: %s → %s (system pressure)",
                        original, brain_override,
                    )

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

    def decide(
        self,
        intent_type: str,
        complexity: str,
        resource_state: Any,
    ) -> BrainSelection:
        """Synchronous route decision using pre-fetched resource state.

        Parameters
        ----------
        intent_type:
            CAI intent string e.g. "code_generation", "bug_fix".
        complexity:
            Complexity tier string e.g. "trivial", "heavy".
        resource_state:
            ResourceState from TelemetryContextualizer — the caller is
            responsible for fetching this before calling decide().
            RouteDecisionService does NOT query local resource APIs directly.
            All resource data must be pre-fetched via TelemetryContextualizer.
        """
        row = _INTENT_TO_BRAIN.get(intent_type)
        if row:
            task_complexity, brain_id = row
        else:
            task_complexity, brain_id = TaskComplexity.LIGHT, "mistral_planning"

        brains = self._brain_selector._policy.get("brains", {}) if self._brain_selector._policy else {}
        brain_cfg = brains.get(brain_id, {})
        model_alias = brain_cfg.get("model_name", "mistral-7b") if isinstance(brain_cfg, dict) else "mistral-7b"

        return BrainSelection(
            brain_id=brain_id,
            model_alias=model_alias,
            reason_code=f"intent_{intent_type}",
            complexity=complexity,
            intent_type=intent_type,
        )

    @property
    def daily_spend(self) -> float:
        return self._brain_selector.daily_spend

    @property
    def daily_spend_breakdown(self) -> Dict[str, float]:
        return self._brain_selector.daily_spend_breakdown

    def record_cost(self, provider: str, cost_usd: float) -> None:
        self._brain_selector.record_cost(provider, cost_usd)
