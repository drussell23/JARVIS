"""
Query Complexity Manager for JARVIS
====================================

Classifies and routes queries based on complexity levels:

Level 1: Simple (Single Space, Single Question)
    - Examples: "What's in space 3?", "Show me space 1"
    - Processing: Parse → Capture → OCR → Return
    - Latency: 2-4s, API Calls: 1

Level 2: Moderate (Multi-Space or Comparison)
    - Examples: "Compare space 1 and 2", "What's different?"
    - Processing: Capture multiple → Compare → Return
    - Latency: 4-8s, API Calls: 2-3

Level 3: Complex (Temporal or Historical)
    - Examples: "What changed since 10 minutes ago?", "Show me the error history"
    - Processing: Load history → Compare → Analyze trends
    - Latency: 8-15s, API Calls: 3-5

Level 4: Advanced (Cross-Space Intelligence)
    - Examples: "Find the error across all spaces", "Which space has Chrome?"
    - Processing: Scan all spaces → Aggregate → Analyze
    - Latency: 15-30s, API Calls: 5-10

Level 5: Expert (Complex Multi-Step Reasoning)
    - Examples: "Track the bug from when it appeared until now"
    - Processing: Multi-step analysis with reasoning
    - Latency: 30s+, API Calls: 10+

Uses ImplicitReferenceResolver for resolving ambiguous queries.

Author: Derek Russell
Date: 2025-10-19
"""

import asyncio
import logging
import re
import time
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum

logger = logging.getLogger(__name__)

# Import Implicit Reference Resolver - use the new resolvers module
try:
    from context_intelligence.resolvers import (
        get_implicit_reference_resolver,
        is_implicit_resolver_available,
        ImplicitReferenceResolver
    )
    IMPLICIT_RESOLVER_AVAILABLE = is_implicit_resolver_available()
except ImportError:
    IMPLICIT_RESOLVER_AVAILABLE = False
    get_implicit_reference_resolver = lambda: None
    logger.debug("ImplicitReferenceResolver deferred - will be available after initialization")


# ============================================================================
# QUERY COMPLEXITY CLASSIFICATION
# ============================================================================

class QueryComplexity(Enum):
    """Query complexity levels"""
    SIMPLE = 1          # Single space, single question
    MODERATE = 2        # Multi-space or comparison
    COMPLEX = 3         # Temporal or historical
    ADVANCED = 4        # Cross-space intelligence
    EXPERT = 5          # Complex multi-step reasoning


@dataclass
class ComplexityMetrics:
    """Metrics for query complexity"""
    level: QueryComplexity
    estimated_latency: Tuple[float, float]  # (min, max) in seconds
    estimated_api_calls: Tuple[int, int]    # (min, max) API calls
    spaces_involved: int
    requires_history: bool
    requires_comparison: bool
    requires_reasoning: bool
    confidence: float  # 0.0-1.0


@dataclass
class ClassifiedQuery:
    """Classified query with metadata"""
    original_query: str
    resolved_query: str  # After reference resolution
    complexity: ComplexityMetrics
    query_type: str  # "single_space", "multi_space", "temporal", "cross_space", "reasoning"
    entities: Dict[str, Any]  # Extracted entities (spaces, apps, errors, etc.)
    intent: str  # "view", "compare", "analyze", "find", "track"
    metadata: Dict[str, Any] = field(default_factory=dict)


class QueryComplexityClassifier:
    """
    Classifies query complexity based on pattern analysis

    Uses linguistic patterns, entity extraction, and context to determine
    how complex a query is and what processing it requires.
    """

    def __init__(self):
        """Initialize query complexity classifier"""
        self.patterns = self._initialize_patterns()
        logger.info("[QUERY-CLASSIFIER] Initialized")

    def _initialize_patterns(self) -> Dict[str, List[re.Pattern]]:
        """Initialize regex patterns for query classification"""
        return {
            # Level 1: Simple (Single Space, Single Question)
            "simple_space_view": [
                re.compile(r"what'?s?\s+in\s+space\s+(\d+)", re.I),
                re.compile(r"show\s+(?:me\s+)?space\s+(\d+)", re.I),
                re.compile(r"display\s+space\s+(\d+)", re.I),
                re.compile(r"view\s+space\s+(\d+)", re.I),
                re.compile(r"capture\s+space\s+(\d+)", re.I),
                re.compile(r"space\s+(\d+)", re.I),
            ],

            # Level 2: Moderate (Multi-Space or Comparison)
            "moderate_comparison": [
                re.compile(r"compare\s+space\s+(\d+)\s+(?:and|with|to)\s+(?:space\s+)?(\d+)", re.I),
                re.compile(r"difference\s+between\s+space\s+(\d+)\s+and\s+(\d+)", re.I),
                re.compile(r"what'?s?\s+different", re.I),
                re.compile(r"show\s+(?:me\s+)?(?:the\s+)?changes?", re.I),
                re.compile(r"what\s+changed", re.I),
            ],
            "moderate_multi_space": [
                re.compile(r"spaces?\s+(\d+)(?:\s*,\s*(\d+))+", re.I),  # "space 1, 2, 3"
                re.compile(r"all\s+spaces?", re.I),
                re.compile(r"every\s+space", re.I),
            ],

            # Level 3: Complex (Temporal or Historical)
            "complex_temporal": [
                re.compile(r"what\s+changed\s+(?:in\s+the\s+)?(?:last|past)\s+(\d+)\s+(second|minute|hour)s?", re.I),
                re.compile(r"since\s+(\d+)\s+(second|minute|hour)s?\s+ago", re.I),
                re.compile(r"(?:show|get)\s+(?:me\s+)?(?:the\s+)?history", re.I),
                re.compile(r"timeline", re.I),
                re.compile(r"what\s+happened\s+(?:when|since)", re.I),
                re.compile(r"before\s+and\s+after", re.I),
            ],
            "complex_error_tracking": [
                re.compile(r"error\s+history", re.I),
                re.compile(r"track\s+(?:the\s+)?error", re.I),
                re.compile(r"when\s+did\s+(?:the\s+)?error\s+(?:appear|start|occur)", re.I),
                re.compile(r"find\s+(?:the\s+)?(?:bug|error|issue)", re.I),
            ],

            # Level 4: Advanced (Cross-Space Intelligence)
            "advanced_cross_space": [
                re.compile(r"find\s+.*\s+across\s+(?:all\s+)?spaces?", re.I),
                re.compile(r"which\s+space\s+has", re.I),
                re.compile(r"where\s+is\s+(?:the\s+)?(\w+)", re.I),
                re.compile(r"locate\s+(?:the\s+)?(\w+)", re.I),
                re.compile(r"search\s+(?:all\s+)?spaces?\s+for", re.I),
            ],
            "advanced_aggregation": [
                re.compile(r"summarize\s+(?:all\s+)?spaces?", re.I),
                re.compile(r"overview\s+of\s+all\s+spaces?", re.I),
                re.compile(r"what'?s?\s+happening\s+everywhere", re.I),
            ],

            # Level 5: Expert (Complex Multi-Step Reasoning)
            "expert_reasoning": [
                re.compile(r"track\s+.*\s+from\s+.*\s+(?:to|until)", re.I),
                re.compile(r"analyze\s+(?:the\s+)?(?:trend|pattern)", re.I),
                # v236.0: Narrowed — bare "explain how X works" is COMPLEX, not EXPERT.
                # EXPERT requires multi-step causality/analysis indicators.
                re.compile(r"explain\s+(?:why|how)\s+.*\b(?:interacts?|affects?|causes?|leads?\s+to|relates?)\b", re.I),
                re.compile(r"root\s+cause", re.I),
                re.compile(r"investigate", re.I),
            ],

            # Intent patterns
            "intent_view": [
                re.compile(r"show|display|view|see|look|capture", re.I),
            ],
            "intent_compare": [
                re.compile(r"compare|difference|contrast|vs", re.I),
            ],
            "intent_analyze": [
                re.compile(r"analyze|examine|investigate|study|understand", re.I),
            ],
            "intent_find": [
                re.compile(r"find|search|locate|where|which", re.I),
            ],
            "intent_track": [
                re.compile(r"track|follow|monitor|trace|history|timeline", re.I),
            ],
        }

    def classify(self, query: str, context: Optional[Dict[str, Any]] = None) -> ComplexityMetrics:
        """
        Classify query complexity

        Args:
            query: User query string
            context: Optional context (recent spaces, history, etc.)

        Returns:
            ComplexityMetrics with classification details
        """
        logger.info(f"[QUERY-CLASSIFIER] Classifying: '{query}'")

        # Extract entities
        entities = self._extract_entities(query, context)

        # Determine complexity level
        level = self._determine_complexity_level(query, entities)

        # Calculate metrics based on level
        metrics = self._calculate_metrics(level, entities, query)

        logger.info(f"[QUERY-CLASSIFIER] ✅ Classified as {level.name} (confidence={metrics.confidence:.2f})")

        return metrics

    def _extract_entities(self, query: str, context: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Extract entities from query"""
        entities = {
            "spaces": [],
            "time_references": [],
            "apps": [],
            "errors": [],
            "windows": [],
            "has_temporal": False,
            "has_comparison": False,
            "has_all_spaces": False,
        }

        # Extract space numbers
        space_matches = re.findall(r"space\s+(\d+)", query, re.I)
        entities["spaces"] = [int(s) for s in space_matches]

        # Check for "all spaces"
        if re.search(r"\ball\s+spaces?\b", query, re.I) or re.search(r"\bevery\s+space\b", query, re.I):
            entities["has_all_spaces"] = True

        # Extract time references
        time_patterns = [
            (r"(\d+)\s+(second|minute|hour)s?", "relative"),
            (r"since\s+(\d+)", "since"),
            (r"last\s+(\d+)", "last"),
        ]
        for pattern, time_type in time_patterns:
            matches = re.findall(pattern, query, re.I)
            if matches:
                entities["time_references"].extend(matches)
                entities["has_temporal"] = True

        # Check for comparison words
        if re.search(r"\b(compare|difference|changed|different|vs|versus)\b", query, re.I):
            entities["has_comparison"] = True

        # Extract application names (common apps)
        common_apps = ["chrome", "safari", "firefox", "vscode", "terminal", "slack", "discord", "zoom"]
        for app in common_apps:
            if re.search(rf"\b{app}\b", query, re.I):
                entities["apps"].append(app)

        # Check for error/bug mentions
        if re.search(r"\b(error|bug|issue|problem|crash|fail)\b", query, re.I):
            entities["errors"].append("error_mentioned")

        return entities

    def _determine_complexity_level(self, query: str, entities: Dict[str, Any]) -> QueryComplexity:
        """Determine complexity level based on patterns and entities"""

        # Level 5: Expert (Complex Multi-Step Reasoning)
        for pattern in self.patterns["expert_reasoning"]:
            if pattern.search(query):
                return QueryComplexity.EXPERT

        # Level 4: Advanced (Cross-Space Intelligence)
        if entities["has_all_spaces"] or len(entities["spaces"]) > 3:
            return QueryComplexity.ADVANCED

        for pattern in self.patterns["advanced_cross_space"]:
            if pattern.search(query):
                return QueryComplexity.ADVANCED

        for pattern in self.patterns["advanced_aggregation"]:
            if pattern.search(query):
                return QueryComplexity.ADVANCED

        # Level 3: Complex (Temporal or Historical)
        if entities["has_temporal"]:
            return QueryComplexity.COMPLEX

        for pattern in self.patterns["complex_temporal"]:
            if pattern.search(query):
                return QueryComplexity.COMPLEX

        for pattern in self.patterns["complex_error_tracking"]:
            if pattern.search(query):
                return QueryComplexity.COMPLEX

        # Level 2: Moderate (Multi-Space or Comparison)
        if entities["has_comparison"]:
            return QueryComplexity.MODERATE

        if len(entities["spaces"]) >= 2:
            return QueryComplexity.MODERATE

        for pattern in self.patterns["moderate_comparison"]:
            if pattern.search(query):
                return QueryComplexity.MODERATE

        for pattern in self.patterns["moderate_multi_space"]:
            if pattern.search(query):
                return QueryComplexity.MODERATE

        # ================================================================
        # GENERAL NL QUERY CLASSIFICATION (non-space queries)
        # v236.0: For queries like "what is 5+5?" that don't match any
        # space-based patterns above.
        #
        # PRIORITY ORDER: EXPERT → COMPLEX → SIMPLE (descending)
        # This prevents compound queries like "what is 5+5 and explain
        # the concept" from being classified as SIMPLE just because the
        # math regex fires. Higher complexity wins.
        # ================================================================

        # Check EXPERT first (highest priority)
        _expert_indicators = any([
            bool(re.search(r"\bdesign\s+(?:a|an)\b.*\b(?:system|architecture|solution)\b", query, re.I)),
            bool(re.search(r"\bimplement\b.*\bfrom\s+scratch\b", query, re.I)),
            bool(re.search(r"\b(?:refactor|debug)\b.*\b(?:code|system|architecture)\b", query, re.I)),
        ])
        if _expert_indicators:
            return QueryComplexity.EXPERT

        # Check COMPLEX second
        _complex_indicators = any([
            bool(re.search(r"\bexplain\s+(?:how|why|the|what)\b", query, re.I)),
            bool(re.search(r"\bwrite\s+(?:a|an|me)\b", query, re.I)),
            bool(re.search(r"\bcompare\b.*\b(?:and|vs|versus|with)\b", query, re.I)),
            bool(re.search(r"\bwhat\s+are\s+the\s+(?:pros|cons|differences|advantages|steps)\b", query, re.I)),
            bool(re.search(r"\b(?:step.by.step|in\s+detail|thoroughly)\b", query, re.I)),
        ])
        if _complex_indicators:
            return QueryComplexity.COMPLEX

        # Check SIMPLE last (only if no complex/expert indicators)
        _simple_indicators = any([
            # Math expressions: 5+5, 10*3, 2^8
            bool(re.search(r"\d+\s*[\+\-\*\/\^%]\s*\d+", query)),
            # Short factual: "what is X", "what's X", "who is X", "define X"
            bool(re.search(r"^(?:what(?:'?s|\s+is)|who\s+is|define|spell|translate)\s+", query, re.I))
            and len(query.split()) <= 8,
            # Yes/no questions under 8 words
            bool(re.search(r"^(?:is|are|was|were|do|does|did|can|could|will|would|should)\s+", query, re.I))
            and len(query.split()) <= 8,
        ])
        if _simple_indicators:
            return QueryComplexity.SIMPLE

        # Length-based fallback
        word_count = len(query.split())
        if word_count <= 6:
            return QueryComplexity.SIMPLE
        elif word_count <= 15:
            return QueryComplexity.MODERATE
        else:
            return QueryComplexity.COMPLEX

    def _calculate_metrics(
        self,
        level: QueryComplexity,
        entities: Dict[str, Any],
        query: str
    ) -> ComplexityMetrics:
        """Calculate metrics for complexity level"""

        # Base metrics by level
        metrics_by_level = {
            QueryComplexity.SIMPLE: {
                "latency": (2.0, 4.0),
                "api_calls": (1, 1),
            },
            QueryComplexity.MODERATE: {
                "latency": (4.0, 8.0),
                "api_calls": (2, 3),
            },
            QueryComplexity.COMPLEX: {
                "latency": (8.0, 15.0),
                "api_calls": (3, 5),
            },
            QueryComplexity.ADVANCED: {
                "latency": (15.0, 30.0),
                "api_calls": (5, 10),
            },
            QueryComplexity.EXPERT: {
                "latency": (30.0, 60.0),
                "api_calls": (10, 20),
            },
        }

        base = metrics_by_level[level]

        # Calculate spaces involved
        spaces_count = len(entities["spaces"])
        if entities["has_all_spaces"]:
            spaces_count = 10  # Estimate

        # Calculate confidence based on pattern matches
        confidence = self._calculate_confidence(query, level)

        return ComplexityMetrics(
            level=level,
            estimated_latency=base["latency"],
            estimated_api_calls=base["api_calls"],
            spaces_involved=spaces_count,
            requires_history=entities["has_temporal"],
            requires_comparison=entities["has_comparison"],
            requires_reasoning=(level == QueryComplexity.EXPERT),
            confidence=confidence
        )

    def _calculate_confidence(self, query: str, level: QueryComplexity) -> float:
        """Calculate confidence score for classification"""
        # Simple heuristic: count how many patterns match for this level
        pattern_groups = {
            QueryComplexity.SIMPLE: ["simple_space_view"],
            QueryComplexity.MODERATE: ["moderate_comparison", "moderate_multi_space"],
            QueryComplexity.COMPLEX: ["complex_temporal", "complex_error_tracking"],
            QueryComplexity.ADVANCED: ["advanced_cross_space", "advanced_aggregation"],
            QueryComplexity.EXPERT: ["expert_reasoning"],
        }

        matches = 0
        total = 0

        for group in pattern_groups.get(level, []):
            for pattern in self.patterns.get(group, []):
                total += 1
                if pattern.search(query):
                    matches += 1

        if total == 0:
            return 0.5  # Default medium confidence

        confidence = matches / total
        return max(0.3, min(1.0, confidence + 0.3))  # Boost and clamp


# ============================================================================
# QUERY ROUTER
# ============================================================================

class QueryRouter:
    """
    Routes queries to appropriate handlers based on complexity

    Integrates with existing query handlers and provides intelligent routing.
    """

    def __init__(self):
        """Initialize query router"""
        self.classifier = QueryComplexityClassifier()
        logger.info("[QUERY-ROUTER] Initialized")

    def classify_and_route(
        self,
        query: str,
        context: Optional[Dict[str, Any]] = None
    ) -> ClassifiedQuery:
        """
        Classify query and determine routing

        Args:
            query: User query string
            context: Optional context

        Returns:
            ClassifiedQuery with routing information
        """
        # Classify complexity
        complexity = self.classifier.classify(query, context)

        # Extract entities
        entities = self.classifier._extract_entities(query, context)

        # Determine query type
        query_type = self._determine_query_type(complexity.level, entities)

        # Determine intent
        intent = self._determine_intent(query)

        return ClassifiedQuery(
            original_query=query,
            resolved_query=query,  # Will be updated by reference resolver
            complexity=complexity,
            query_type=query_type,
            entities=entities,
            intent=intent
        )

    def _determine_query_type(self, level: QueryComplexity, entities: Dict[str, Any]) -> str:
        """Determine query type for routing"""
        if level == QueryComplexity.EXPERT:
            return "reasoning"
        elif level == QueryComplexity.ADVANCED:
            return "cross_space"
        elif level == QueryComplexity.COMPLEX:
            return "temporal"
        elif level == QueryComplexity.MODERATE:
            if entities["has_comparison"]:
                return "comparison"
            else:
                return "multi_space"
        else:
            return "single_space"

    def _determine_intent(self, query: str) -> str:
        """Determine user intent"""
        patterns = self.classifier.patterns

        if any(p.search(query) for p in patterns["intent_track"]):
            return "track"
        elif any(p.search(query) for p in patterns["intent_find"]):
            return "find"
        elif any(p.search(query) for p in patterns["intent_analyze"]):
            return "analyze"
        elif any(p.search(query) for p in patterns["intent_compare"]):
            return "compare"
        elif any(p.search(query) for p in patterns["intent_view"]):
            return "view"
        else:
            return "unknown"


# ============================================================================
# QUERY COMPLEXITY MANAGER
# ============================================================================

class QueryComplexityManager:
    """
    Manages query complexity classification and routing

    Main coordinator that:
    1. Resolves ambiguous references using ImplicitReferenceResolver
    2. Classifies query complexity
    3. Routes to appropriate handler
    4. Provides latency and cost estimates
    """

    def __init__(self, implicit_resolver: Optional[Any] = None):
        """
        Initialize query complexity manager

        Args:
            implicit_resolver: Optional ImplicitReferenceResolver instance
        """
        self.router = QueryRouter()
        self.implicit_resolver = implicit_resolver

        if implicit_resolver:
            logger.info("✅ Implicit Reference Resolver available for query resolution")
        else:
            logger.warning("Implicit Reference Resolver not available")

        logger.info("[QUERY-COMPLEXITY] Manager initialized")

    async def process_query(
        self,
        query: str,
        context: Optional[Dict[str, Any]] = None
    ) -> ClassifiedQuery:
        """
        Process query with reference resolution and complexity classification

        Args:
            query: User query string
            context: Optional context

        Returns:
            ClassifiedQuery with full metadata
        """
        start_time = time.time()

        logger.info(f"[QUERY-COMPLEXITY] Processing: '{query}'")

        # Step 1: Resolve ambiguous references
        resolved_query = query
        if self.implicit_resolver:
            try:
                resolved_query = await self._resolve_references(query, context)
                if resolved_query != query:
                    logger.info(f"[QUERY-COMPLEXITY] Resolved: '{query}' → '{resolved_query}'")
            except Exception as e:
                logger.warning(f"Reference resolution failed: {e}")

        # Step 2: Classify and route
        classified = self.router.classify_and_route(resolved_query, context)
        classified.resolved_query = resolved_query

        # Step 3: Add processing metadata
        processing_time = time.time() - start_time
        classified.metadata.update({
            "processing_time": processing_time,
            "timestamp": datetime.now().isoformat(),
            "resolver_used": self.implicit_resolver is not None
        })

        logger.info(
            f"[QUERY-COMPLEXITY] ✅ Classified as {classified.complexity.level.name} "
            f"(type={classified.query_type}, intent={classified.intent}, "
            f"latency={classified.complexity.estimated_latency[0]:.1f}-{classified.complexity.estimated_latency[1]:.1f}s)"
        )

        return classified

    async def _resolve_references(self, query: str, context: Optional[Dict[str, Any]]) -> str:
        """Resolve ambiguous references in query"""
        if not self.implicit_resolver:
            return query

        try:
            # Use implicit resolver to resolve references
            # Note: This is a simplified interface - adjust based on actual ImplicitReferenceResolver API
            resolved = await asyncio.to_thread(
                self.implicit_resolver.resolve_query,
                query,
                context or {}
            )
            return resolved if resolved else query
        except Exception as e:
            logger.error(f"Reference resolution error: {e}")
            return query

    def get_handler_recommendation(self, classified: ClassifiedQuery) -> str:
        """
        Get recommended handler for classified query

        Args:
            classified: ClassifiedQuery

        Returns:
            Handler name
        """
        recommendations = {
            "single_space": "single_space_handler",
            "multi_space": "multi_space_query_handler",
            "comparison": "multi_space_query_handler",
            "temporal": "temporal_query_handler",
            "cross_space": "multi_space_query_handler",
            "reasoning": "advanced_reasoning_handler",
        }

        handler = recommendations.get(classified.query_type, "single_space_handler")

        logger.info(f"[QUERY-COMPLEXITY] Recommended handler: {handler}")

        return handler


# ============================================================================
# GLOBAL INSTANCE
# ============================================================================

_global_manager: Optional[QueryComplexityManager] = None


def get_query_complexity_manager() -> Optional[QueryComplexityManager]:
    """Get the global query complexity manager instance"""
    return _global_manager


def initialize_query_complexity_manager(
    implicit_resolver: Optional[Any] = None
) -> QueryComplexityManager:
    """Initialize the global query complexity manager"""
    global _global_manager

    # Get implicit resolver if not provided
    if implicit_resolver is None and IMPLICIT_RESOLVER_AVAILABLE:
        implicit_resolver = get_implicit_reference_resolver()

    _global_manager = QueryComplexityManager(implicit_resolver=implicit_resolver)
    logger.info("[QUERY-COMPLEXITY] Global instance initialized")
    return _global_manager
