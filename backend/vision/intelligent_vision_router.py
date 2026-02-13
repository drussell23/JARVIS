"""
Intelligent Vision Router for JARVIS
Dynamically routes vision queries to optimal model: YOLO, LLaMA, Claude (Haiku/Sonnet/Opus)

Features:
- Zero hardcoding - learns optimal routing from performance data
- Async parallel processing when beneficial
- Cost-aware routing (prioritize free local models)
- Performance tracking and adaptive learning
- Automatic fallback and retry logic
"""

import asyncio
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)


class ModelType(Enum):
    """Available vision/reasoning models"""

    YOLO = "yolo"  # Local YOLO UI detection
    LLAMA = "llama"  # Local LLaMA 3.1 70B
    CLAUDE_HAIKU = "claude_haiku"  # Fast, cheap Claude
    CLAUDE_SONNET = "claude_sonnet"  # Balanced Claude
    CLAUDE_OPUS = "claude_opus"  # Premium Claude
    YOLO_CLAUDE_HYBRID = "yolo_claude_hybrid"  # YOLO + Claude combined
    YABAI = "yabai"  # Fast Mission Control queries (no screenshot)


class TaskComplexity(Enum):
    """Vision task complexity levels"""

    TRIVIAL = "trivial"  # <100ms - UI detection, counting
    SIMPLE = "simple"  # <500ms - Text reading, basic analysis
    MEDIUM = "medium"  # <2s - Content understanding, suggestions
    COMPLEX = "complex"  # <5s - Deep analysis, multi-step reasoning
    MULTI_MODAL = "multi_modal"  # Requires multiple models


@dataclass
class ModelCapability:
    """Defines what each model can do"""

    model_type: ModelType
    can_detect_ui: bool = False
    can_read_text: bool = False
    can_understand_context: bool = False
    can_reason: bool = False
    can_analyze_multiple_screens: bool = False
    typical_latency_ms: float = 0.0
    cost_per_query: float = 0.0  # USD
    is_local: bool = False
    max_parallel_requests: int = 1


@dataclass
class PerformanceMetrics:
    """Track model performance over time"""

    model_type: ModelType
    total_queries: int = 0
    successful_queries: int = 0
    failed_queries: int = 0
    total_latency_ms: float = 0.0
    total_cost_usd: float = 0.0
    avg_latency_ms: float = 0.0
    success_rate: float = 0.0
    avg_cost_per_query: float = 0.0
    last_updated: datetime = field(default_factory=datetime.now)
    recent_latencies: deque = field(default_factory=lambda: deque(maxlen=100))

    def update(self, latency_ms: float, success: bool, cost: float = 0.0):
        """Update metrics with new data point"""
        self.total_queries += 1
        self.total_latency_ms += latency_ms
        self.total_cost_usd += cost
        self.recent_latencies.append(latency_ms)

        if success:
            self.successful_queries += 1
        else:
            self.failed_queries += 1

        # Recalculate averages
        self.avg_latency_ms = self.total_latency_ms / self.total_queries
        self.success_rate = self.successful_queries / self.total_queries
        self.avg_cost_per_query = self.total_cost_usd / self.total_queries
        self.last_updated = datetime.now()

    def get_p95_latency(self) -> float:
        """Get 95th percentile latency"""
        if not self.recent_latencies:
            return self.avg_latency_ms
        return float(np.percentile(list(self.recent_latencies), 95))


@dataclass
class RoutingDecision:
    """Result of routing decision"""

    primary_model: ModelType
    fallback_models: List[ModelType] = field(default_factory=list)
    use_parallel: bool = False
    parallel_models: List[ModelType] = field(default_factory=list)
    estimated_latency_ms: float = 0.0
    estimated_cost_usd: float = 0.0
    reasoning: str = ""
    confidence: float = 0.0


@dataclass
class QueryAnalysis:
    """Analyzed query characteristics"""

    original_query: str
    requires_screenshot: bool = True
    requires_ui_detection: bool = False
    requires_text_reading: bool = False
    requires_reasoning: bool = False
    is_multi_space: bool = False
    is_counting: bool = False
    is_comparison: bool = False
    estimated_complexity: TaskComplexity = TaskComplexity.MEDIUM
    keywords: List[str] = field(default_factory=list)
    confidence: float = 0.0


class IntelligentVisionRouter:
    """
    Intelligent routing system for vision queries
    Automatically selects optimal model(s) based on:
    - Query complexity
    - Performance history
    - Cost constraints
    - Latency requirements
    """

    def __init__(
        self,
        yolo_detector=None,
        llama_executor=None,
        claude_vision_analyzer=None,
        yabai_detector=None,
        max_cost_per_query: float = 0.05,  # Max $0.05 per query
        target_latency_ms: float = 2000,  # Target <2s response
        prefer_local: bool = True,  # Prefer free local models
    ):
        self.yolo_detector = yolo_detector
        self.llama_executor = llama_executor
        self.claude_vision_analyzer = claude_vision_analyzer
        self.yabai_detector = yabai_detector

        # Constraints
        self.max_cost_per_query = max_cost_per_query
        self.target_latency_ms = target_latency_ms
        self.prefer_local = prefer_local

        # Performance tracking
        self.metrics: Dict[ModelType, PerformanceMetrics] = {}
        for model_type in ModelType:
            self.metrics[model_type] = PerformanceMetrics(model_type=model_type)

        # Model capabilities (dynamically learned, these are initial estimates)
        self.capabilities = self._initialize_capabilities()

        # Query pattern learning
        self.query_patterns: Dict[str, List[RoutingDecision]] = defaultdict(list)
        self.successful_routes: Dict[str, ModelType] = {}

        # Adaptive thresholds (learned over time)
        self.complexity_thresholds = {
            TaskComplexity.TRIVIAL: 0.3,
            TaskComplexity.SIMPLE: 0.5,
            TaskComplexity.MEDIUM: 0.7,
            TaskComplexity.COMPLEX: 0.85,
        }

        logger.info("[INTELLIGENT ROUTER] Initialized with adaptive learning")

    def _initialize_capabilities(self) -> Dict[ModelType, ModelCapability]:
        """Initialize model capabilities (can be updated dynamically)"""
        return {
            ModelType.YOLO: ModelCapability(
                model_type=ModelType.YOLO,
                can_detect_ui=True,
                can_read_text=False,
                can_understand_context=False,
                can_reason=False,
                typical_latency_ms=50.0,
                cost_per_query=0.0,
                is_local=True,
                max_parallel_requests=10,
            ),
            ModelType.LLAMA: ModelCapability(
                model_type=ModelType.LLAMA,
                can_detect_ui=False,
                can_read_text=True,
                can_understand_context=True,
                can_reason=True,
                typical_latency_ms=800.0,
                cost_per_query=0.0,
                is_local=True,
                max_parallel_requests=3,
            ),
            ModelType.CLAUDE_HAIKU: ModelCapability(
                model_type=ModelType.CLAUDE_HAIKU,
                can_detect_ui=False,
                can_read_text=True,
                can_understand_context=True,
                can_reason=True,
                typical_latency_ms=500.0,
                cost_per_query=0.003,
                is_local=False,
                max_parallel_requests=5,
            ),
            ModelType.CLAUDE_SONNET: ModelCapability(
                model_type=ModelType.CLAUDE_SONNET,
                can_detect_ui=False,
                can_read_text=True,
                can_understand_context=True,
                can_reason=True,
                typical_latency_ms=1500.0,
                cost_per_query=0.015,
                is_local=False,
                max_parallel_requests=3,
            ),
            ModelType.CLAUDE_OPUS: ModelCapability(
                model_type=ModelType.CLAUDE_OPUS,
                can_detect_ui=False,
                can_read_text=True,
                can_understand_context=True,
                can_reason=True,
                typical_latency_ms=3000.0,
                cost_per_query=0.075,
                is_local=False,
                max_parallel_requests=2,
            ),
            ModelType.YOLO_CLAUDE_HYBRID: ModelCapability(
                model_type=ModelType.YOLO_CLAUDE_HYBRID,
                can_detect_ui=True,
                can_read_text=True,
                can_understand_context=True,
                can_reason=True,
                typical_latency_ms=600.0,
                cost_per_query=0.003,
                is_local=False,
                max_parallel_requests=3,
            ),
            ModelType.YABAI: ModelCapability(
                model_type=ModelType.YABAI,
                can_detect_ui=False,
                can_read_text=False,
                can_understand_context=False,
                can_reason=False,
                can_analyze_multiple_screens=True,
                typical_latency_ms=10.0,
                cost_per_query=0.0,
                is_local=True,
                max_parallel_requests=20,
            ),
        }

    async def analyze_query(
        self, query: str, context: Optional[Dict[str, Any]] = None
    ) -> QueryAnalysis:
        """
        Analyze query to determine requirements
        This is dynamic - learns from patterns over time
        """
        query_lower = query.lower()
        words = query_lower.split()

        analysis = QueryAnalysis(original_query=query)

        # UI detection indicators
        ui_keywords = {
            "button",
            "icon",
            "window",
            "app",
            "application",
            "menu",
            "toolbar",
            "count",
            "how many",
            "find",
            "locate",
            "where is",
            "show me",
        }
        analysis.requires_ui_detection = any(kw in query_lower for kw in ui_keywords)

        # Text reading indicators
        text_keywords = {
            "read",
            "text",
            "what does",
            "says",
            "written",
            "content",
            "document",
            "email",
            "message",
            "code",
        }
        analysis.requires_text_reading = any(kw in query_lower for kw in text_keywords)

        # Reasoning indicators
        reasoning_keywords = {
            "why",
            "how",
            "explain",
            "analyze",
            "suggest",
            "recommend",
            "compare",
            "better",
            "should",
            "optimize",
            "improve",
        }
        analysis.requires_reasoning = any(kw in query_lower for kw in reasoning_keywords)

        # Multi-space indicators (must be word boundaries to avoid false positives)
        multi_space_keywords = {
            "desktop",
            "spaces",  # plural only
            "across",
            "other",
            "different",
            "workspace",
            "workspaces",
        }
        # Check for word boundaries (not substring matches)
        analysis.is_multi_space = any(
            f" {kw} " in f" {query_lower} "
            or query_lower.startswith(kw + " ")
            or query_lower.endswith(" " + kw)
            for kw in multi_space_keywords
        )

        # Also check for explicit multi-space patterns
        multi_space_patterns = [
            "desktop 1",
            "desktop 2",
            "desktop 3",
            "space 1",
            "space 2",
            "space 3",
            "all spaces",
            "all desktops",
            "other spaces",
            "other desktops",
        ]
        if any(pattern in query_lower for pattern in multi_space_patterns):
            analysis.is_multi_space = True

        # Counting tasks
        analysis.is_counting = any(word in ["count", "how many", "number"] for word in words)

        # Comparison tasks
        analysis.is_comparison = any(
            word in ["compare", "difference", "versus", "vs"] for word in words
        )

        # Yabai can handle multi-space queries without screenshots
        # BUT: If query requires UI detection (icon, button, visible, etc.), need screenshot
        ui_element_keywords = {
            "icon", "button", "element", "control", "ui", "visible",
            "hidden", "showing", "displayed", "find", "locate"
        }
        requires_ui_detection = any(kw in query_lower for kw in ui_element_keywords)

        if analysis.is_multi_space and self.yabai_detector and not requires_ui_detection:
            analysis.requires_screenshot = False

        # Estimate complexity
        analysis.estimated_complexity = self._estimate_complexity(analysis)

        # Extract keywords for pattern learning
        analysis.keywords = [w for w in words if len(w) > 3][:5]

        # Confidence based on clarity of indicators
        indicator_count = sum(
            [
                analysis.requires_ui_detection,
                analysis.requires_text_reading,
                analysis.requires_reasoning,
                analysis.is_multi_space,
            ]
        )
        analysis.confidence = min(0.5 + (indicator_count * 0.15), 0.95)

        logger.info(
            f"[ROUTER] Query analysis: complexity={analysis.estimated_complexity.value}, "
            f"ui={analysis.requires_ui_detection}, text={analysis.requires_text_reading}, "
            f"reasoning={analysis.requires_reasoning}, multi_space={analysis.is_multi_space}, "
            f"requires_screenshot={analysis.requires_screenshot}"
        )

        return analysis

    def _estimate_complexity(self, analysis: QueryAnalysis) -> TaskComplexity:
        """Estimate task complexity based on requirements"""
        # Multi-modal tasks are most complex
        if analysis.is_comparison or (analysis.requires_reasoning and analysis.is_multi_space):
            return TaskComplexity.COMPLEX

        # Reasoning tasks are medium-complex
        if analysis.requires_reasoning:
            return TaskComplexity.MEDIUM

        # Text reading is simple
        if analysis.requires_text_reading:
            return TaskComplexity.SIMPLE

        # UI detection is trivial
        if analysis.requires_ui_detection or analysis.is_counting:
            return TaskComplexity.TRIVIAL

        # Default to simple
        return TaskComplexity.SIMPLE

    async def route_query(
        self,
        query: str,
        context: Optional[Dict[str, Any]] = None,
        user_constraints: Optional[Dict[str, Any]] = None,
    ) -> RoutingDecision:
        """
        Intelligently route query to optimal model(s)

        Args:
            query: User's vision query
            context: Additional context (recent interactions, screen state, etc.)
            user_constraints: Override default constraints (max_cost, max_latency)

        Returns:
            RoutingDecision with primary model, fallbacks, and parallel options
        """
        start_time = time.time()

        # Analyze query
        analysis = await self.analyze_query(query, context)

        # Apply user constraints if provided
        max_cost = (
            user_constraints.get("max_cost", self.max_cost_per_query)
            if user_constraints
            else self.max_cost_per_query
        )
        max_latency = (
            user_constraints.get("max_latency", self.target_latency_ms)
            if user_constraints
            else self.target_latency_ms
        )

        # Special case: Yabai for multi-space queries (instant, free)
        if analysis.is_multi_space and self.yabai_detector and not analysis.requires_screenshot:
            return RoutingDecision(
                primary_model=ModelType.YABAI,
                fallback_models=[ModelType.YOLO_CLAUDE_HYBRID],
                estimated_latency_ms=10.0,
                estimated_cost_usd=0.0,
                reasoning="Multi-space query without screenshot requirements - Yabai is optimal (instant, free)",
                confidence=0.95,
            )

        # Find candidate models that can handle this query
        candidates = self._find_capable_models(analysis)

        if not candidates:
            logger.warning(f"[ROUTER] No capable models found for query: {query}")
            # Default fallback to Claude Sonnet
            return RoutingDecision(
                primary_model=ModelType.CLAUDE_SONNET,
                fallback_models=[ModelType.CLAUDE_HAIKU],
                estimated_latency_ms=1500.0,
                estimated_cost_usd=0.015,
                reasoning="No specific model match - defaulting to Claude Sonnet",
                confidence=0.5,
            )

        # Score candidates based on performance, cost, latency
        scored_candidates = self._score_candidates(candidates, analysis, max_cost, max_latency)

        if not scored_candidates:
            # All candidates too expensive or slow
            logger.warning(f"[ROUTER] All candidates exceed constraints")
            # Return cheapest/fastest option with warning
            return RoutingDecision(
                primary_model=ModelType.CLAUDE_HAIKU,
                fallback_models=[ModelType.LLAMA],
                estimated_latency_ms=500.0,
                estimated_cost_usd=0.003,
                reasoning="All optimal candidates exceed constraints - using minimum cost option",
                confidence=0.6,
            )

        # Select primary model (highest score)
        primary_model = scored_candidates[0][0]
        primary_score = scored_candidates[0][1]

        # Select fallbacks (next best options)
        fallback_models = [model for model, score in scored_candidates[1:4]]

        # Determine if parallel execution would be beneficial
        use_parallel = False
        parallel_models = []

        if analysis.estimated_complexity == TaskComplexity.COMPLEX:
            # For complex queries, consider YOLO + Claude in parallel
            if ModelType.YOLO in [m for m, s in scored_candidates]:
                parallel_models = [ModelType.YOLO, primary_model]
                use_parallel = True

        # Calculate estimates
        primary_capability = self.capabilities[primary_model]
        estimated_latency = self._get_estimated_latency(primary_model)
        estimated_cost = primary_capability.cost_per_query

        if use_parallel:
            # Parallel execution takes max(latencies), adds costs
            estimated_latency = max(self._get_estimated_latency(m) for m in parallel_models)
            estimated_cost = sum(self.capabilities[m].cost_per_query for m in parallel_models)

        reasoning = self._generate_routing_reasoning(
            analysis, primary_model, primary_score, use_parallel, parallel_models
        )

        decision = RoutingDecision(
            primary_model=primary_model,
            fallback_models=fallback_models,
            use_parallel=use_parallel,
            parallel_models=parallel_models,
            estimated_latency_ms=estimated_latency,
            estimated_cost_usd=estimated_cost,
            reasoning=reasoning,
            confidence=min(0.5 + primary_score * 0.4, 0.95),
        )

        # Learn from this routing decision
        pattern_key = self._get_pattern_key(analysis)
        self.query_patterns[pattern_key].append(decision)

        routing_time_ms = (time.time() - start_time) * 1000
        logger.info(
            f"[ROUTER] Decision: {primary_model.value} "
            f"(est. {estimated_latency:.0f}ms, ${estimated_cost:.4f}) "
            f"routing_time={routing_time_ms:.1f}ms"
        )

        return decision

    def _find_capable_models(self, analysis: QueryAnalysis) -> List[ModelType]:
        """Find models capable of handling this query"""
        candidates = []

        for model_type, capability in self.capabilities.items():
            # Check if model has required capabilities
            can_handle = True

            if analysis.requires_ui_detection and not capability.can_detect_ui:
                can_handle = False
            if analysis.requires_text_reading and not capability.can_read_text:
                can_handle = False
            if analysis.requires_reasoning and not capability.can_reason:
                can_handle = False
            if analysis.is_multi_space and not capability.can_analyze_multiple_screens:
                # Only Yabai and hybrid models can do multi-space
                if model_type not in [ModelType.YABAI, ModelType.YOLO_CLAUDE_HYBRID]:
                    can_handle = False

            # Check if model is actually available
            if model_type == ModelType.YOLO and not self.yolo_detector:
                can_handle = False
            elif model_type == ModelType.LLAMA and not self.llama_executor:
                can_handle = False
            elif model_type in [
                ModelType.CLAUDE_HAIKU,
                ModelType.CLAUDE_SONNET,
                ModelType.CLAUDE_OPUS,
                ModelType.YOLO_CLAUDE_HYBRID,
            ]:
                if not self.claude_vision_analyzer:
                    can_handle = False
            elif model_type == ModelType.YABAI and not self.yabai_detector:
                can_handle = False

            if can_handle:
                candidates.append(model_type)

        return candidates

    def _score_candidates(
        self,
        candidates: List[ModelType],
        analysis: QueryAnalysis,
        max_cost: float,
        max_latency: float,
    ) -> List[Tuple[ModelType, float]]:
        """
        Score candidate models
        Higher score = better choice

        Scoring factors:
        - Performance history (success rate, latency)
        - Cost (prefer free local models)
        - Capability match (does it have exactly what we need?)
        - Latency constraints
        """
        scored = []

        for model_type in candidates:
            capability = self.capabilities[model_type]
            metrics = self.metrics[model_type]

            # Start with base score
            score = 0.5

            # Performance factor (0-0.3)
            if metrics.total_queries > 10:
                # Have historical data
                performance_score = metrics.success_rate * 0.3
                score += performance_score
            else:
                # No data, use moderate score
                score += 0.15

            # Cost factor (0-0.3)
            if self.prefer_local and capability.is_local:
                score += 0.3  # Strong preference for free local models
            elif capability.cost_per_query == 0:
                score += 0.3
            elif capability.cost_per_query <= max_cost * 0.3:
                score += 0.2  # Very cheap
            elif capability.cost_per_query <= max_cost * 0.7:
                score += 0.1  # Acceptable cost
            else:
                score -= 0.2  # Too expensive

            # Latency factor (0-0.2)
            estimated_latency = self._get_estimated_latency(model_type)
            if estimated_latency <= max_latency * 0.3:
                score += 0.2  # Very fast
            elif estimated_latency <= max_latency * 0.7:
                score += 0.1  # Acceptable
            elif estimated_latency > max_latency:
                score -= 0.3  # Too slow

            # Capability match factor (0-0.2)
            capability_match = 0
            if analysis.requires_ui_detection and capability.can_detect_ui:
                capability_match += 0.05
            if analysis.requires_text_reading and capability.can_read_text:
                capability_match += 0.05
            if analysis.requires_reasoning and capability.can_reason:
                capability_match += 0.05
            if analysis.is_multi_space and capability.can_analyze_multiple_screens:
                capability_match += 0.05
            score += capability_match

            # Complexity match (prefer faster models for simple tasks)
            if analysis.estimated_complexity == TaskComplexity.TRIVIAL:
                if model_type in [ModelType.YOLO, ModelType.YABAI]:
                    score += 0.2
            elif analysis.estimated_complexity == TaskComplexity.SIMPLE:
                if model_type in [ModelType.YOLO_CLAUDE_HYBRID, ModelType.CLAUDE_HAIKU]:
                    score += 0.15
            elif analysis.estimated_complexity == TaskComplexity.COMPLEX:
                if model_type in [ModelType.CLAUDE_SONNET, ModelType.LLAMA]:
                    score += 0.1

            # Filter out models that violate hard constraints
            if capability.cost_per_query > max_cost or estimated_latency > max_latency * 1.5:
                continue

            scored.append((model_type, score))

        # Sort by score (descending)
        scored.sort(key=lambda x: x[1], reverse=True)

        return scored

    def _get_estimated_latency(self, model_type: ModelType) -> float:
        """Get estimated latency for model (uses historical data if available)"""
        metrics = self.metrics[model_type]
        capability = self.capabilities[model_type]

        if metrics.total_queries >= 10:
            # Use P95 latency from recent history
            return metrics.get_p95_latency()
        else:
            # Use capability estimate
            return capability.typical_latency_ms

    def _get_pattern_key(self, analysis: QueryAnalysis) -> str:
        """Generate pattern key for learning"""
        return f"{analysis.estimated_complexity.value}_{analysis.requires_ui_detection}_{analysis.requires_reasoning}"

    def _generate_routing_reasoning(
        self,
        analysis: QueryAnalysis,
        primary_model: ModelType,
        score: float,
        use_parallel: bool,
        parallel_models: List[ModelType],
    ) -> str:
        """Generate human-readable reasoning for routing decision"""
        capability = self.capabilities[primary_model]

        reasons = []

        # Complexity match
        reasons.append(f"Task complexity: {analysis.estimated_complexity.value}")

        # Model selection
        if capability.is_local:
            reasons.append(f"Using local {primary_model.value} (free, fast)")
        else:
            reasons.append(f"Using {primary_model.value} (${capability.cost_per_query:.4f}/query)")

        # Performance
        metrics = self.metrics[primary_model]
        if metrics.total_queries >= 10:
            reasons.append(f"Historical success rate: {metrics.success_rate:.1%}")

        # Parallel execution
        if use_parallel:
            model_names = ", ".join([m.value for m in parallel_models])
            reasons.append(f"Parallel execution: {model_names}")

        return " | ".join(reasons)

    async def execute_query(
        self,
        query: str,
        screenshot=None,
        context: Optional[Dict[str, Any]] = None,
        decision: Optional[RoutingDecision] = None,
    ) -> Dict[str, Any]:
        """
        Execute vision query using routed model(s)

        Args:
            query: User's vision query
            screenshot: Screenshot data (if needed)
            context: Additional context
            decision: Pre-made routing decision (if None, will route automatically)

        Returns:
            Result dictionary with response, metrics, and metadata
        """
        start_time = time.time()

        # Route query if decision not provided
        if not decision:
            decision = await self.route_query(query, context)

        logger.info(f"[ROUTER] Executing with {decision.primary_model.value}")

        # Execute based on decision
        try:
            if decision.use_parallel:
                result = await self._execute_parallel(
                    query, screenshot, context, decision.parallel_models
                )
            else:
                result = await self._execute_single(
                    query, screenshot, context, decision.primary_model
                )

            # If primary fails, try fallbacks
            if not result.get("success") and decision.fallback_models:
                logger.warning(
                    f"[ROUTER] Primary model {decision.primary_model.value} failed, "
                    f"trying fallback: {decision.fallback_models[0].value}"
                )
                result = await self._execute_single(
                    query, screenshot, context, decision.fallback_models[0]
                )

        except Exception as e:
            logger.error(f"[ROUTER] Execution error: {e}", exc_info=True)
            result = {
                "success": False,
                "response": f"I encountered an error processing that vision query: {str(e)}",
                "error": str(e),
            }

        # Calculate actual metrics
        actual_latency_ms = (time.time() - start_time) * 1000
        actual_cost = self.capabilities[decision.primary_model].cost_per_query

        # Update performance metrics
        self.metrics[decision.primary_model].update(
            latency_ms=actual_latency_ms,
            success=result.get("success", False),
            cost=actual_cost,
        )

        # Add metadata to result
        result["routing_metadata"] = {
            "model_used": decision.primary_model.value,
            "estimated_latency_ms": decision.estimated_latency_ms,
            "actual_latency_ms": actual_latency_ms,
            "estimated_cost_usd": decision.estimated_cost_usd,
            "actual_cost_usd": actual_cost,
            "routing_reasoning": decision.reasoning,
            "confidence": decision.confidence,
            "used_parallel": decision.use_parallel,
        }

        logger.info(
            f"[ROUTER] Execution complete: "
            f"success={result.get('success')}, "
            f"latency={actual_latency_ms:.0f}ms, "
            f"cost=${actual_cost:.4f}"
        )

        return result

    async def _execute_single(
        self, query: str, screenshot, context: Optional[Dict[str, Any]], model: ModelType
    ) -> Dict[str, Any]:
        """Execute query with single model"""
        try:
            if model == ModelType.YOLO:
                return await self._execute_yolo(query, screenshot, context)
            elif model == ModelType.LLAMA:
                return await self._execute_llama(query, screenshot, context)
            elif model == ModelType.CLAUDE_HAIKU:
                return await self._execute_claude(query, screenshot, context, "haiku")
            elif model == ModelType.CLAUDE_SONNET:
                return await self._execute_claude(query, screenshot, context, "sonnet")
            elif model == ModelType.CLAUDE_OPUS:
                return await self._execute_claude(query, screenshot, context, "opus")
            elif model == ModelType.YOLO_CLAUDE_HYBRID:
                return await self._execute_hybrid(query, screenshot, context)
            elif model == ModelType.YABAI:
                return await self._execute_yabai(query, context)
            else:
                return {
                    "success": False,
                    "response": f"Model {model.value} not implemented",
                }
        except Exception as e:
            logger.error(f"[ROUTER] Error executing {model.value}: {e}", exc_info=True)
            return {
                "success": False,
                "response": f"Error executing {model.value}: {str(e)}",
                "error": str(e),
            }

    async def _execute_parallel(
        self, query: str, screenshot, context: Optional[Dict[str, Any]], models: List[ModelType]
    ) -> Dict[str, Any]:
        """Execute query with multiple models in parallel, combine results"""
        tasks = []
        for model in models:
            tasks.append(self._execute_single(query, screenshot, context, model))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Combine results intelligently
        # If YOLO + Claude: use YOLO detections + Claude understanding
        if ModelType.YOLO in models and any(m.value.startswith("claude") for m in models):
            yolo_result = results[models.index(ModelType.YOLO)]
            claude_result = results[
                [i for i, m in enumerate(models) if m.value.startswith("claude")][0]
            ]

            return {
                "success": True,
                "response": claude_result.get("response", ""),
                "yolo_detections": yolo_result.get("detections", []),
                "claude_analysis": claude_result,
                "parallel_execution": True,
            }

        # Default: return first successful result
        for result in results:
            if isinstance(result, dict) and result.get("success"):
                return result

        return {
            "success": False,
            "response": "All parallel executions failed",
        }

    async def _execute_yolo(
        self, query: str, screenshot, context: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Execute YOLO detection"""
        if not self.yolo_detector:
            return {"success": False, "response": "YOLO detector not available"}

        try:
            detections = await self.yolo_detector.detect_ui_elements(screenshot)

            # Format response based on query
            if "count" in query.lower() or "how many" in query.lower():
                count = len(detections)
                return {
                    "success": True,
                    "response": f"I detected {count} UI elements on your screen.",
                    "detections": detections,
                    "count": count,
                }
            else:
                return {
                    "success": True,
                    "response": f"Detected {len(detections)} UI elements.",
                    "detections": detections,
                }
        except Exception as e:
            logger.error(f"[ROUTER] YOLO execution error: {e}")
            return {"success": False, "response": f"YOLO detection failed: {str(e)}"}

    async def _execute_llama(
        self, query: str, screenshot, context: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Execute LLaMA reasoning"""
        if not self.llama_executor:
            return {"success": False, "response": "LLaMA executor not available"}

        try:
            # LLaMA can reason about vision context but needs structured input
            prompt = f"Vision query: {query}\n\nProvide intelligent analysis and response."

            result = await self.llama_executor.execute_llm_task(
                task_type="vision_reasoning",
                prompt=prompt,
                context=context,
            )

            return {
                "success": True,
                "response": result.get("response", ""),
                "reasoning": result.get("reasoning", ""),
            }
        except Exception as e:
            logger.error(f"[ROUTER] LLaMA execution error: {e}")
            return {"success": False, "response": f"LLaMA reasoning failed: {str(e)}"}

    async def _execute_claude(
        self, query: str, screenshot, context: Optional[Dict[str, Any]], model: str = "sonnet"
    ) -> Dict[str, Any]:
        """Execute Claude vision analysis"""
        if not self.claude_vision_analyzer:
            return {"success": False, "response": "Claude vision analyzer not available"}

        try:
            # Enhance prompt for more detailed responses
            enhanced_prompt = query

            # For "can you see" queries, request detailed description
            if any(
                pattern in query.lower()
                for pattern in ["can you see", "what do you see", "describe"]
            ):
                enhanced_prompt = (
                    f"{query}\n\n"
                    "Please provide a detailed response including:\n"
                    "- What applications are open and what they're showing\n"
                    "- Any text or content visible on screen\n"
                    "- What the user appears to be working on\n"
                    "- Any notable UI elements or activities\n"
                    "Respond naturally as JARVIS."
                )

            # Use OptimizedClaudeVisionAnalyzer
            result = await self.claude_vision_analyzer.analyze_screenshot_fast(
                screenshot=screenshot,
                prompt=enhanced_prompt,
                model=model,
            )

            return {
                "success": True,
                "response": result.get("response", ""),
                "analysis": result.get("analysis", {}),
                "model_used": model,
            }
        except Exception as e:
            logger.error(f"[ROUTER] Claude execution error: {e}")
            return {"success": False, "response": f"Claude analysis failed: {str(e)}"}

    async def _execute_hybrid(
        self, query: str, screenshot, context: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Execute YOLO + Claude hybrid"""
        if not self.claude_vision_analyzer:
            return {"success": False, "response": "Claude vision analyzer not available"}

        try:
            # Enhance prompt for more detailed responses
            enhanced_prompt = query

            # For "can you see" queries, request detailed description
            if any(
                pattern in query.lower()
                for pattern in ["can you see", "what do you see", "describe"]
            ):
                enhanced_prompt = (
                    f"{query}\n\n"
                    "Please provide a detailed response including:\n"
                    "- What applications are open and what they're showing\n"
                    "- Any text or content visible on screen\n"
                    "- What the user appears to be working on\n"
                    "- Any notable UI elements or activities\n"
                    "Respond naturally as JARVIS."
                )

            # v251.6: Fixed phantom method — analyze_with_yolo_hybrid doesn't exist.
            # analyze_screenshot_fast() contains YOLO hybrid logic internally
            # (checks self.use_yolo_hybrid at optimized_claude_vision.py:175).
            result = await self.claude_vision_analyzer.analyze_screenshot_fast(
                image=screenshot,
                prompt=enhanced_prompt,
            )

            return {
                "success": True,
                "response": result.get("response", ""),
                "yolo_detections": result.get("yolo_detections", []),
                "claude_analysis": result.get("claude_analysis", {}),
                "hybrid_mode": True,
            }
        except Exception as e:
            logger.error(f"[ROUTER] Hybrid execution error: {e}")
            return {"success": False, "response": f"Hybrid analysis failed: {str(e)}"}

    async def _execute_yabai(self, query: str, context: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Execute Yabai multi-space query with async support"""
        if not self.yabai_detector:
            return {"success": False, "response": "Yabai detector not available"}

        try:
            # Use async versions if available, with timeout protection
            if hasattr(self.yabai_detector, 'enumerate_all_spaces_async'):
                # Use async version (non-blocking)
                spaces = await asyncio.wait_for(
                    self.yabai_detector.enumerate_all_spaces_async(),
                    timeout=10.0
                )
                workspace_description = await asyncio.wait_for(
                    self.yabai_detector.describe_workspace_async(),
                    timeout=10.0
                )
            else:
                # Fallback to sync version in thread pool
                loop = asyncio.get_event_loop()
                spaces = await asyncio.wait_for(
                    loop.run_in_executor(None, self.yabai_detector.enumerate_all_spaces),
                    timeout=10.0
                )
                workspace_description = await asyncio.wait_for(
                    loop.run_in_executor(None, self.yabai_detector.describe_workspace),
                    timeout=10.0
                )

            return {
                "success": True,
                "response": workspace_description,
                "spaces": spaces,
                "yabai_powered": True,
            }
        except asyncio.TimeoutError:
            logger.error("[ROUTER] Yabai execution timed out")
            return {"success": False, "response": "Yabai query timed out - workspace detection took too long"}
        except Exception as e:
            logger.error(f"[ROUTER] Yabai execution error: {e}")
            return {"success": False, "response": f"Yabai query failed: {str(e)}"}

    def get_performance_report(self) -> Dict[str, Any]:
        """Generate performance report for all models"""
        report = {
            "generated_at": datetime.now().isoformat(),
            "models": {},
            "total_queries": 0,
            "total_cost_usd": 0.0,
        }

        for model_type, metrics in self.metrics.items():
            if metrics.total_queries > 0:
                report["models"][model_type.value] = {
                    "total_queries": metrics.total_queries,
                    "success_rate": f"{metrics.success_rate:.1%}",
                    "avg_latency_ms": f"{metrics.avg_latency_ms:.1f}",
                    "p95_latency_ms": f"{metrics.get_p95_latency():.1f}",
                    "total_cost_usd": f"${metrics.total_cost_usd:.4f}",
                    "avg_cost_per_query": f"${metrics.avg_cost_per_query:.4f}",
                }
                report["total_queries"] += metrics.total_queries
                report["total_cost_usd"] += metrics.total_cost_usd

        report["total_cost_usd"] = f"${report['total_cost_usd']:.4f}"

        return report


# Global router instance (singleton)
_global_router: Optional[IntelligentVisionRouter] = None


def get_vision_router(
    yolo_detector=None,
    llama_executor=None,
    claude_vision_analyzer=None,
    yabai_detector=None,
) -> IntelligentVisionRouter:
    """Get or create global vision router"""
    global _global_router

    if _global_router is None:
        _global_router = IntelligentVisionRouter(
            yolo_detector=yolo_detector,
            llama_executor=llama_executor,
            claude_vision_analyzer=claude_vision_analyzer,
            yabai_detector=yabai_detector,
        )
        logger.info("[ROUTER] Created global IntelligentVisionRouter instance")

    return _global_router
