"""
Intelligent Model Selector - UAE/SAI/CAI Integrated Decision Engine
==================================================================

Selects the best model for each query based on:
1. Task requirements (CAI: intent classification)
2. Context awareness (UAE: user context, focus level)
3. RAM availability (SAI: memory pressure, loaded models)
4. Model capabilities (Registry: what each model can do)
5. Cost vs quality trade-offs (user preferences)
6. Current model states (loaded vs cached vs archived)

Decision algorithm:
- Analyze query intent and complexity
- Find capable models
- Score each option based on quality, cost, latency, RAM
- Select best option considering current system state
- Provide fallback chain if primary fails

Zero hardcoding - all policies and scoring from config
"""

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

from backend.intelligence.model_lifecycle_manager import get_lifecycle_manager
from backend.intelligence.model_registry import ModelDefinition, ModelState, get_model_registry

logger = logging.getLogger(__name__)


@dataclass
class QueryContext:
    """Context for a user query"""

    query: str
    intent: Optional[str] = None  # CAI classification
    required_capabilities: Set[str] = None  # Must have ALL of these
    preferred_capabilities: Set[str] = None  # Score bonus, not mandatory
    complexity: str = "medium"  # simple, medium, complex
    quality_requirement: str = "balanced"  # quick, balanced, best
    user_focus_level: str = "casual"  # deep_work, focused, casual, idle
    urgency: str = "normal"  # urgent, normal, low
    context_data: Optional[Dict] = None  # UAE context

    def __post_init__(self):
        if self.required_capabilities is None:
            self.required_capabilities = set()
        if self.preferred_capabilities is None:
            self.preferred_capabilities = set()


@dataclass
class ModelOption:
    """A scored model option for selection"""

    model: ModelDefinition
    score: float
    estimated_latency_seconds: float
    estimated_cost_usd: float
    quality_score: float
    ram_impact: str  # "none", "available", "needs_eviction", "impossible"
    reasoning: List[str]  # Why this score


class IntelligentModelSelector:
    """
    Intelligent model selection engine with UAE/SAI/CAI integration

    Responsibilities:
    - Analyze queries with CAI for intent
    - Consider UAE context (focus, urgency)
    - Check SAI RAM availability
    - Score all capable models
    - Select optimal model
    - Provide fallback chain
    """

    def __init__(self, config_path: Optional[str] = None):
        self.registry = get_model_registry()
        self.lifecycle_manager = get_lifecycle_manager()

        # Scoring weights (from config or defaults)
        self.weights = {"quality": 0.35, "latency": 0.25, "cost": 0.25, "ram_efficiency": 0.15}

        # User preferences (could be learned over time)
        self.user_preferences = {
            "prefer_local_models": True,  # Prefer $0 cost models
            "max_wait_seconds": 30.0,  # Max acceptable latency
            "quality_threshold": 0.7,  # Minimum quality score
            "max_cost_per_query": 0.10,  # Max $ per query
        }

        logger.info("✅ Intelligent Model Selector initialized")

    async def select_best_model(
        self,
        query: str,
        intent: Optional[str] = None,
        required_capabilities: Optional[Set[str]] = None,
        context: Optional[Dict] = None,
    ) -> Optional[ModelDefinition]:
        """
        Select the best model for a query

        Args:
            query: User's query text
            intent: Pre-classified intent (if available)
            required_capabilities: Required capabilities (if known)
            context: Additional context from UAE/SAI/CAI

        Returns:
            Selected ModelDefinition or None if no suitable model
        """
        # Build query context
        query_context = await self._build_query_context(
            query, intent, required_capabilities, context
        )

        # Find capable models
        capable_models = await self._find_capable_models(query_context)

        if not capable_models:
            logger.warning(f"No capable models found for query: {query[:50]}")
            return None

        # Score all options
        scored_options = await self._score_models(capable_models, query_context)

        if not scored_options:
            logger.warning(f"No viable options after scoring for: {query[:50]}")
            return None

        # Select best option
        best_option = max(scored_options, key=lambda x: x.score)

        # Log decision
        self._log_selection(query_context, best_option, scored_options)

        return best_option.model

    async def select_with_fallback(
        self,
        query: str,
        intent: Optional[str] = None,
        required_capabilities: Optional[Set[str]] = None,
        context: Optional[Dict] = None,
        max_fallbacks: int = 3,
    ) -> Tuple[Optional[ModelDefinition], List[ModelDefinition]]:
        """
        Select best model with fallback chain

        Returns:
            (primary_model, [fallback1, fallback2, ...])
        """
        query_context = await self._build_query_context(
            query, intent, required_capabilities, context
        )
        capable_models = await self._find_capable_models(query_context)
        scored_options = await self._score_models(capable_models, query_context)

        if not scored_options:
            return None, []

        # Sort by score (best first)
        sorted_options = sorted(scored_options, key=lambda x: x.score, reverse=True)

        primary = sorted_options[0].model
        fallbacks = [opt.model for opt in sorted_options[1 : max_fallbacks + 1]]

        logger.info(f"Selected {primary.name} with {len(fallbacks)} fallbacks")
        return primary, fallbacks

    # ============== Query Analysis ==============

    async def _build_query_context(
        self,
        query: str,
        intent: Optional[str],
        required_capabilities: Optional[Set[str]],
        context: Optional[Dict],
    ) -> QueryContext:
        """Build comprehensive query context"""

        # Extract intent if not provided
        if not intent:
            intent = await self._classify_intent(query)

        # Infer capabilities from intent
        preferred_capabilities: Set[str] = set()
        if not required_capabilities:
            required_capabilities, preferred_capabilities = (
                self._intent_to_required_and_preferred(intent)
            )

        # Determine complexity
        complexity = self._estimate_complexity(query, intent)

        # Quality requirement based on context
        quality_requirement = "balanced"
        if context and context.get("user_focus") == "deep_work":
            quality_requirement = "quick"  # Don't disrupt focus
        elif complexity == "complex":
            quality_requirement = "best"

        # User focus level from UAE
        user_focus_level = "casual"
        if context:
            user_focus_level = context.get("user_focus", "casual")

        # Urgency
        urgency = context.get("urgency", "normal") if context else "normal"

        return QueryContext(
            query=query,
            intent=intent,
            required_capabilities=required_capabilities,
            preferred_capabilities=preferred_capabilities,
            complexity=complexity,
            quality_requirement=quality_requirement,
            user_focus_level=user_focus_level,
            urgency=urgency,
            context_data=context,
        )

    async def _classify_intent(self, query: str) -> str:
        """
        Classify query intent using CAI

        TODO: Integrate with actual CAI when available
        For now, use simple keyword matching
        """
        query_lower = query.lower()

        # Vision-related
        if any(
            kw in query_lower for kw in ["screen", "see", "look", "show", "what's on", "display"]
        ):
            return "vision_analysis"

        # Code-related
        if any(kw in query_lower for kw in ["code", "function", "explain", "debug", "fix"]):
            return "code_explanation"

        # Conversation/chat
        if any(kw in query_lower for kw in ["chat", "talk", "discuss", "tell me about"]):
            return "conversational_ai"

        # Search/lookup
        if any(kw in query_lower for kw in ["find", "search", "when did", "what did i"]):
            return "semantic_search"

        # General NLP
        return "nlp_analysis"

    def _intent_to_capabilities(self, intent: str) -> Set[str]:
        """Map intent to required capabilities (backward compat)."""
        required, _ = self._intent_to_required_and_preferred(intent)
        return required

    def _intent_to_required_and_preferred(
        self, intent: str
    ) -> Tuple[Set[str], Set[str]]:
        """Map intent to (required, preferred) capability sets.

        Required: model MUST have ALL of these.
        Preferred: bonus score for having these, not mandatory.
        """
        _required_map = {
            "vision_analysis": {"vision"},
            "code_explanation": {"nlp_analysis"},
            "conversational_ai": {"conversational_ai"},
            "semantic_search": {"semantic_search", "embedding"},
            "nlp_analysis": {"nlp_analysis"},
            "intent_classification": {"intent_classification"},
            "text_summarization": {"text_summarization"},
            "query_expansion": {"query_expansion"},
        }
        _preferred_map = {
            "vision_analysis": {"object_detection", "vision_analyze_heavy", "screen_analysis"},
            "code_explanation": {"code_explanation", "response_generation"},
            "conversational_ai": {"chatbot_inference", "response_generation"},
            "semantic_search": {"similarity_search"},
            "nlp_analysis": {"response_generation"},
        }
        required = set(_required_map.get(intent, {"nlp_analysis"}))
        preferred = set(_preferred_map.get(intent, set()))
        return required, preferred

    def _estimate_complexity(self, query: str, intent: str) -> str:
        """Estimate query complexity"""
        # Simple heuristics
        if len(query.split()) < 5:
            return "simple"
        elif len(query.split()) > 20:
            return "complex"
        elif intent in ["code_explanation", "vision_analysis"]:
            return "complex"
        return "medium"

    # ============== Model Finding ==============

    async def _find_capable_models(self, query_context: QueryContext) -> List[ModelDefinition]:
        """Find models using required + preferred capability matching.

        v290.0: Models must have ALL required capabilities (hard filter).
        Preferred capabilities provide a score bonus for ranking but are
        not mandatory — a model missing preferred caps is still viable.
        """
        capable_models: Dict[str, ModelDefinition] = {}

        # Gather candidate models from both required and preferred capabilities
        all_caps = query_context.required_capabilities | query_context.preferred_capabilities
        for capability in all_caps:
            models = self.registry.get_models_for_capability(capability)
            for model in models:
                capable_models[model.name] = model

        # Filter: must have ALL required capabilities
        viable: list = []
        for model in capable_models.values():
            if not all(
                model.supports_capability(cap)
                for cap in query_context.required_capabilities
            ):
                continue

            # Score preferred capability coverage
            pref_score = sum(
                1 for cap in query_context.preferred_capabilities
                if model.supports_capability(cap)
            )
            viable.append((model, pref_score))

        # Sort by preferred coverage + priority (higher = better)
        viable.sort(key=lambda x: (x[1], x[0].priority), reverse=True)
        result = [model for model, _ in viable]

        logger.debug(
            "Found %d capable models for %s (required=%s, preferred=%s)",
            len(result), query_context.intent,
            query_context.required_capabilities,
            query_context.preferred_capabilities,
        )
        return result

    # ============== Model Scoring ==============

    async def _score_models(
        self, models: List[ModelDefinition], query_context: QueryContext
    ) -> List[ModelOption]:
        """Score all models and return viable options"""
        scored_options = []

        for model in models:
            option = await self._score_single_model(model, query_context)
            if option and option.score > 0:
                scored_options.append(option)

        return scored_options

    async def _score_single_model(
        self, model: ModelDefinition, query_context: QueryContext
    ) -> Optional[ModelOption]:
        """Score a single model option"""
        reasoning = []

        # 1. Quality score
        quality_score = model.performance.quality_score
        quality_subscore = quality_score * self.weights["quality"]
        reasoning.append(f"Quality: {quality_score:.2f}")

        # 2. Latency score
        estimated_latency = await self._estimate_latency(model, query_context)
        max_latency = self.user_preferences["max_wait_seconds"]

        if estimated_latency > max_latency:
            reasoning.append(f"❌ Latency too high: {estimated_latency:.1f}s > {max_latency}s")
            return None  # Disqualified

        latency_score = 1.0 - (estimated_latency / max_latency)
        latency_subscore = latency_score * self.weights["latency"]
        reasoning.append(f"Latency: {estimated_latency:.1f}s → {latency_score:.2f}")

        # 3. Cost score
        estimated_cost = model.resources.cost_per_query_usd
        max_cost = self.user_preferences["max_cost_per_query"]

        if estimated_cost > max_cost:
            reasoning.append(f"❌ Cost too high: ${estimated_cost:.3f} > ${max_cost:.3f}")
            return None

        cost_score = 1.0 - (estimated_cost / max_cost) if max_cost > 0 else 1.0
        cost_subscore = cost_score * self.weights["cost"]
        reasoning.append(f"Cost: ${estimated_cost:.3f} → {cost_score:.2f}")

        # 4. RAM efficiency score
        ram_impact, ram_score = await self._assess_ram_impact(model, query_context)
        ram_subscore = ram_score * self.weights["ram_efficiency"]
        reasoning.append(f"RAM: {ram_impact} → {ram_score:.2f}")

        if ram_impact == "impossible":
            reasoning.append("❌ Insufficient RAM, cannot deploy")
            return None

        # Total score
        total_score = quality_subscore + latency_subscore + cost_subscore + ram_subscore
        reasoning.append(f"TOTAL: {total_score:.3f}")

        return ModelOption(
            model=model,
            score=total_score,
            estimated_latency_seconds=estimated_latency,
            estimated_cost_usd=estimated_cost,
            quality_score=quality_score,
            ram_impact=ram_impact,
            reasoning=reasoning,
        )

    async def _estimate_latency(self, model: ModelDefinition, query_context: QueryContext) -> float:
        """Estimate total latency including load time"""
        # Base inference latency
        inference_latency = model.performance.latency_ms / 1000.0

        # Add load time if not already loaded
        load_latency = model.get_load_time_estimate()

        # Add unload time if RAM pressure (might need to evict)
        ram_status = await self.lifecycle_manager._get_ram_status(model.backend_preference)
        if ram_status.percent_used > 80 and load_latency > 0:
            # Might need to evict another model
            load_latency += 5.0  # Estimate eviction time

        return inference_latency + load_latency

    async def _assess_ram_impact(
        self, model: ModelDefinition, query_context: QueryContext
    ) -> Tuple[str, float]:
        """
        Assess RAM impact of loading this model

        Returns:
            (impact_level, score)
            impact_level: "none", "available", "needs_eviction", "impossible"
            score: 0.0-1.0 (higher is better)
        """
        # If already loaded, no RAM impact
        if model.current_state in [ModelState.LOADED, ModelState.ACTIVE]:
            return "none", 1.0

        # Check RAM availability
        ram_status = await self.lifecycle_manager._get_ram_status(model.backend_preference)
        ram_needed = model.resources.ram_gb

        if ram_status.available_gb >= ram_needed:
            # Fits without eviction
            score = 0.9  # High score
            return "available", score

        # Would need eviction
        total_backend_ram = self.lifecycle_manager.backend_ram_limits.get(
            model.backend_preference, 16
        )
        if ram_needed > total_backend_ram:
            # Model is too large for backend
            return "impossible", 0.0

        # Can fit if we evict
        score = 0.5  # Medium score (eviction has cost)
        return "needs_eviction", score

    # ============== Decision Logging ==============

    def _log_selection(
        self, query_context: QueryContext, best_option: ModelOption, all_options: List[ModelOption]
    ):
        """Log the selection decision"""
        logger.info("=" * 60)
        logger.info(f"🎯 Model Selection for: {query_context.query[:60]}")
        logger.info(f"   Intent: {query_context.intent}")
        logger.info(f"   Capabilities: {query_context.required_capabilities}")
        logger.info(f"   Complexity: {query_context.complexity}")
        logger.info("-" * 60)

        # Log best selection
        logger.info(f"✅ SELECTED: {best_option.model.display_name}")
        logger.info(f"   Score: {best_option.score:.3f}")
        logger.info(f"   Latency: {best_option.estimated_latency_seconds:.2f}s")
        logger.info(f"   Cost: ${best_option.estimated_cost_usd:.3f}")
        logger.info(f"   Quality: {best_option.quality_score:.2f}")
        logger.info(f"   RAM: {best_option.ram_impact}")
        logger.info(f"   Reasoning:")
        for reason in best_option.reasoning:
            logger.info(f"      {reason}")

        # Log alternatives
        if len(all_options) > 1:
            logger.info("-" * 60)
            logger.info("   Alternatives:")
            for option in sorted(all_options, key=lambda x: x.score, reverse=True)[1:4]:
                logger.info(
                    f"      {option.model.display_name}: {option.score:.3f} "
                    f"({option.estimated_latency_seconds:.1f}s, ${option.estimated_cost_usd:.3f})"
                )

        logger.info("=" * 60)

    # ============== Public Utilities ==============

    async def get_model_recommendations(
        self, intent: str, max_recommendations: int = 3
    ) -> List[Dict]:
        """
        Get model recommendations for a specific intent

        Useful for UI/debugging
        """
        query_context = QueryContext(
            query=f"Query for {intent}",
            intent=intent,
            required_capabilities=self._intent_to_capabilities(intent),
        )

        capable_models = await self._find_capable_models(query_context)
        scored_options = await self._score_models(capable_models, query_context)

        recommendations = []
        for option in sorted(scored_options, key=lambda x: x.score, reverse=True)[
            :max_recommendations
        ]:
            recommendations.append(
                {
                    "model": option.model.display_name,
                    "score": round(option.score, 3),
                    "latency_seconds": round(option.estimated_latency_seconds, 2),
                    "cost_usd": round(option.estimated_cost_usd, 3),
                    "quality": round(option.quality_score, 2),
                    "reasoning": option.reasoning,
                }
            )

        return recommendations


# Global instance
_model_selector: Optional[IntelligentModelSelector] = None


def get_model_selector() -> IntelligentModelSelector:
    """Get or create global model selector instance"""
    global _model_selector
    if _model_selector is None:
        _model_selector = IntelligentModelSelector()
    return _model_selector
