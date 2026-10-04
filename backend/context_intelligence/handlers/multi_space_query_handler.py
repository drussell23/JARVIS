"""
Multi-Space Query Handler - Advanced Cross-Space Analysis
==========================================================

Handles complex queries spanning multiple Mission Control spaces:
- "Compare space 3 and space 5"
- "Which space has the error?"
- "Find the terminal across all spaces"
- "What's different between space 1 and space 2?"

Architecture:
    User Query → Intent Detection → Space Resolution → Parallel Capture
         ↓              ↓                  ↓                  ↓
    Parse Query   COMPARE/LOCATE    Extract Spaces    Screenshot All
         ↓              ↓                  ↓                  ↓
    Extract Refs   Determine Type    Space List      Vision Analysis
         ↓              ↓                  ↓                  ↓
         └──────────────┴──────────────────┴────────→ Synthesis
                                                           ↓
                                                    Unified Response

Features:
- ✅ Parallel space capture (async/concurrent)
- ✅ Dynamic space resolution (no hardcoding)
- ✅ Intent-aware comparison (leverages ImplicitReferenceResolver)
- ✅ Cross-space search (find X across all spaces)
- ✅ Difference detection (semantic comparison)
- ✅ Synthesis engine (unified response generation)
"""

import asyncio
import logging
from typing import Dict, List, Optional, Any, Set, Tuple
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import re

from context_intelligence.managers.space_state_manager import (
    get_space_state_manager,
    SpaceState
)

logger = logging.getLogger(__name__)


def normalize_space_ids(space_ids) -> List[int]:
    """Coerce resolved space ids to unique ints, preserving order.

    Every detector keys spaces by int; ids parsed out of a query or handed
    in by a caller may arrive as "2". Normalizing once at the handler's
    entry means no lookup downstream can miss on type. Unparseable ids are
    dropped with a warning rather than analyzed as a space that cannot exist.
    """
    out: List[int] = []
    for raw in space_ids or []:
        try:
            sid = int(raw)
        except (TypeError, ValueError):
            logger.warning(f"[MULTI-SPACE] Dropping non-integer space id {raw!r}")
            continue
        if sid not in out:
            out.append(sid)
    return out


# ============================================================================
# QUERY TYPES
# ============================================================================

class MultiSpaceQueryType(Enum):
    """Types of multi-space queries"""
    COMPARE = "compare"              # Compare 2+ spaces
    SEARCH = "search"                # Find X across all spaces
    DIFFERENCE = "difference"        # What's different between spaces
    SUMMARY = "summary"              # Summarize multiple spaces
    LOCATE = "locate"                # Which space has X?


@dataclass
class SpaceAnalysisResult:
    """Result of analyzing a single space"""
    space_id: int
    success: bool
    app_name: Optional[str] = None
    window_title: Optional[str] = None
    content_type: Optional[str] = None  # error, code, documentation, terminal, browser
    content_summary: str = ""
    ocr_text: str = ""
    entities: List[str] = field(default_factory=list)  # Extracted entities
    errors: List[str] = field(default_factory=list)
    significance: str = "normal"  # critical, high, normal, low
    analysis_time: float = 0.0
    vision_analysis: Optional[Dict[str, Any]] = None


@dataclass
class MultiSpaceQueryResult:
    """Result of multi-space query"""
    query_type: MultiSpaceQueryType
    original_query: str
    spaces_analyzed: List[int]
    results: List[SpaceAnalysisResult]
    comparison: Optional[Dict[str, Any]] = None
    differences: Optional[List[Dict[str, Any]]] = None
    search_matches: Optional[List[Dict[str, Any]]] = None
    synthesis: str = ""
    confidence: float = 0.0
    total_time: float = 0.0


# ============================================================================
# MULTI-SPACE QUERY HANDLER
# ============================================================================

class MultiSpaceQueryHandler:
    """
    Handles queries spanning multiple Mission Control spaces.

    This integrates with:
    - ImplicitReferenceResolver (intent detection)
    - ContextualQueryResolver (space resolution)
    - MultiSpaceContextGraph (context storage)
    - Vision systems (OCR and analysis)
    """

    def __init__(self, context_graph=None, implicit_resolver=None, contextual_resolver=None, learning_db=None, yabai_detector=None, cg_window_detector=None):
        """
        Initialize the multi-space query handler.

        Args:
            context_graph: MultiSpaceContextGraph instance
            implicit_resolver: ImplicitReferenceResolver instance
            contextual_resolver: ContextualQueryResolver instance
            learning_db: JARVISLearningDatabase instance for pattern learning
            yabai_detector: YabaiSpaceDetector for yabai integration
            cg_window_detector: MultiSpaceWindowDetector for Core Graphics windows
        """
        self.context_graph = context_graph
        self.implicit_resolver = implicit_resolver
        self.contextual_resolver = contextual_resolver
        self.space_manager = get_space_state_manager()
        self.learning_db = learning_db
        self.yabai_detector = yabai_detector
        self.cg_window_detector = cg_window_detector

        # Query patterns (dynamic - no hardcoding)
        self._initialize_patterns()

        logger.info("[MULTI-SPACE] Handler initialized")
        if self.learning_db:
            logger.info("[MULTI-SPACE] Learning Database integration enabled")
        if self.yabai_detector:
            logger.info("[MULTI-SPACE] Yabai integration enabled")
        if self.cg_window_detector:
            logger.info("[MULTI-SPACE] Core Graphics window detection enabled")

    def _initialize_patterns(self):
        """Initialize dynamic query patterns"""
        # Comparison patterns
        self.comparison_patterns = [
            r'\bcompare\b.*\b(?:and|vs|versus|with)\b',
            r'\b(?:difference|different)\s+between\b',
            r'\bwhat\'?s\s+(?:different|the difference)\b',
        ]

        # Summary/Overview patterns (for "what's happening" type queries)
        self.summary_patterns = [
            r'\bwhat\'?s\s+happening\s+across\b',
            r'\bwhat\s+is\s+happening\s+across\b',
            r'\bshow\s+(?:me\s+)?all\s+(?:my\s+)?(?:(?:desktop\s+)?spaces?|desktops?)\b',
            r'\boverview\s+of\s+(?:all\s+)?(?:my\s+)?(?:spaces?|desktops?)\b',
            r'\bwhat\'?s\s+on\s+(?:all\s+)?(?:my\s+)?(?:spaces?|desktops?)\b',
            r'\bwhat\'?s\s+in\s+(?:all\s+)?(?:my\s+)?(?:spaces?|desktops?)\b',
            r'\bacross\s+(?:all\s+)?(?:my\s+)?(?:(?:desktop\s+)?spaces?|desktops?)\b',
            r'\ball\s+(?:my\s+)?(?:(?:desktop\s+)?spaces?|desktops?)\b.*\bwhat\b',
        ]

        # Search patterns (looking for specific content)
        self.search_patterns = [
            r'\bfind\s+(?:the\s+)?(\w+)\s+across\b',
            r'\b(?:where|which\s+space)\s+(?:is|has)\b',
            r'\blocate\s+(?:the\s+)?(\w+)\b',
            r'\bsearch\s+(?:for\s+)?(?:the\s+)?(\w+)\s+in\s+all\b',
        ]

        # Space extraction patterns. "Desktop N" is the user-facing name on
        # both macOS (Mission Control) and Windows, so it counts as a space.
        self.space_patterns = [
            r'(?:space|desktop)\s+(\d+)',
            r'(?:space|desktop)s?\s+(\d+)\s+(?:and|&)\s+(\d+)',
            r'(?:space|desktop)s?\s+(\d+),\s*(\d+)(?:,\s*and\s+(\d+))?',
        ]

    async def handle_query(self, query: str, available_spaces: Optional[List[int]] = None) -> MultiSpaceQueryResult:
        """
        Main entry point for multi-space queries.

        Args:
            query: User's natural language query
            available_spaces: Optional list of available spaces (auto-detected if None)

        Returns:
            MultiSpaceQueryResult with comprehensive analysis
        """
        start_time = datetime.now()

        logger.info(f"[MULTI-SPACE] Processing query: '{query}'")

        # Step 1: Classify query type
        query_type = await self._classify_query_type(query)
        logger.debug(f"[MULTI-SPACE] Query type: {query_type.value}")

        # Step 2: Resolve which spaces to analyze
        spaces_to_analyze = normalize_space_ids(
            await self._resolve_spaces(query, query_type, available_spaces)
        )
        logger.info(f"[MULTI-SPACE] Spaces to analyze: {spaces_to_analyze}")

        if not spaces_to_analyze:
            return MultiSpaceQueryResult(
                query_type=query_type,
                original_query=query,
                spaces_analyzed=[],
                results=[],
                synthesis="I couldn't determine which spaces to analyze. Could you specify?",
                confidence=0.0,
                total_time=0.0
            )

        # Step 3: Capture and analyze spaces in parallel
        results = await self._analyze_spaces_parallel(spaces_to_analyze, query)

        # Step 4: Perform query-specific processing
        if query_type == MultiSpaceQueryType.COMPARE:
            comparison = await self._compare_spaces(results, query)
        else:
            comparison = None

        if query_type == MultiSpaceQueryType.DIFFERENCE:
            differences = await self._detect_differences(results)
        else:
            differences = None

        if query_type == MultiSpaceQueryType.SEARCH or query_type == MultiSpaceQueryType.LOCATE:
            search_matches = await self._search_across_spaces(results, query)
        else:
            search_matches = None

        # Step 5: Synthesize unified response
        synthesis = await self._synthesize_response(
            query_type, results, query, comparison, differences, search_matches
        )

        # Calculate confidence
        confidence = self._calculate_confidence(results, query_type)

        total_time = (datetime.now() - start_time).total_seconds()

        # Store query pattern in learning database
        if self.learning_db:
            await self._record_query_pattern(
                query=query,
                query_type=query_type,
                spaces_analyzed=spaces_to_analyze,
                results=results,
                confidence=confidence,
                execution_time=total_time
            )

        return MultiSpaceQueryResult(
            query_type=query_type,
            original_query=query,
            spaces_analyzed=spaces_to_analyze,
            results=results,
            comparison=comparison,
            differences=differences,
            search_matches=search_matches,
            synthesis=synthesis,
            confidence=confidence,
            total_time=total_time
        )

    async def _classify_query_type(self, query: str) -> MultiSpaceQueryType:
        """Classify the type of multi-space query"""
        query_lower = query.lower()

        # Use implicit resolver's intent if available
        if self.implicit_resolver:
            try:
                parsed = self.implicit_resolver.query_analyzer.analyze(query)
                intent = parsed.intent.value

                # Map intent to query type
                if intent == "compare":
                    return MultiSpaceQueryType.COMPARE
                elif intent == "locate":
                    return MultiSpaceQueryType.LOCATE
                elif intent == "summarize":
                    return MultiSpaceQueryType.SUMMARY
            except Exception as e:
                logger.debug(f"[MULTI-SPACE] Could not use implicit resolver intent: {e}")

        # Fallback to pattern matching
        # Check for summary/overview patterns first
        for pattern in self.summary_patterns:
            if re.search(pattern, query_lower):
                return MultiSpaceQueryType.SUMMARY

        for pattern in self.comparison_patterns:
            if re.search(pattern, query_lower):
                # Check if it's asking for differences
                if "different" in query_lower or "difference" in query_lower:
                    return MultiSpaceQueryType.DIFFERENCE
                return MultiSpaceQueryType.COMPARE

        for pattern in self.search_patterns:
            if re.search(pattern, query_lower):
                # "which space has" → LOCATE, "find X across" → SEARCH
                if "which space" in query_lower or "where is" in query_lower:
                    return MultiSpaceQueryType.LOCATE
                return MultiSpaceQueryType.SEARCH

        # Default to comparison if multiple spaces mentioned
        named = self._extract_space_numbers(query)
        if len(named) >= 2:
            return MultiSpaceQueryType.COMPARE
        # One named space and no search pattern ("what's on desktop 2"):
        # describe it. As SEARCH it had no term to match and always
        # answered "No matches found".
        if len(named) == 1:
            return MultiSpaceQueryType.SUMMARY

        return MultiSpaceQueryType.SEARCH  # Default

    async def _resolve_spaces(self, query: str, query_type: MultiSpaceQueryType,
                              available_spaces: Optional[List[int]]) -> List[int]:
        """
        Resolve which spaces to analyze based on query.

        Uses both explicit mentions and contextual resolution.
        """
        # Try explicit space numbers first
        explicit_spaces = self._extract_space_numbers(query)
        if explicit_spaces:
            logger.debug(f"[MULTI-SPACE] Found explicit spaces: {explicit_spaces}")
            return explicit_spaces

        # For search/locate queries, use all available spaces
        if query_type in [MultiSpaceQueryType.SEARCH, MultiSpaceQueryType.LOCATE]:
            if available_spaces:
                logger.debug(f"[MULTI-SPACE] Using all available spaces for search: {available_spaces}")
                return available_spaces
            detected = await self._detect_space_ids()
            if detected:
                return detected
            # No detector answer: probe a bounded default range
            return list(range(1, 11))

        # For comparison, try contextual resolver
        if self.contextual_resolver:
            try:
                resolution = await self.contextual_resolver.resolve_query(query)
                if resolution.success and resolution.resolved_spaces:
                    logger.debug(f"[MULTI-SPACE] Contextual resolver found: {resolution.resolved_spaces}")
                    return resolution.resolved_spaces
            except Exception as e:
                logger.debug(f"[MULTI-SPACE] Contextual resolution failed: {e}")

        # A summary with no named space covers every space that exists
        if query_type == MultiSpaceQueryType.SUMMARY:
            detected = await self._detect_space_ids()
            if detected:
                return detected

        # Fallback: empty list (will trigger clarification)
        return []

    async def _detect_space_ids(self) -> List[int]:
        """Space ids that exist right now, per the injected space detector."""
        detector = self.yabai_detector
        if detector is None:
            return []
        try:
            if hasattr(detector, "enumerate_all_spaces_async"):
                spaces = await detector.enumerate_all_spaces_async()
            else:
                spaces = await asyncio.get_running_loop().run_in_executor(
                    None, detector.enumerate_all_spaces
                )
        except Exception as e:
            logger.debug(f"[MULTI-SPACE] Space detection failed: {e}")
            return []
        return normalize_space_ids(s.get("space_id") for s in spaces or [])

    def _extract_space_numbers(self, query: str) -> List[int]:
        """Extract explicit space numbers from query"""
        spaces = set()

        for pattern in self.space_patterns:
            for match in re.finditer(pattern, query, re.IGNORECASE):
                # Extract all captured groups
                for group in match.groups():
                    if group and group.isdigit():
                        spaces.add(int(group))

        return sorted(list(spaces))

    async def _analyze_spaces_parallel(self, space_ids: List[int], query: str) -> List[SpaceAnalysisResult]:
        """
        Analyze multiple spaces in parallel using async/await.

        This is the core parallel execution engine.
        """
        logger.info(f"[MULTI-SPACE] Starting parallel analysis of {len(space_ids)} spaces")

        # Create async tasks for each space
        tasks = [
            self._analyze_single_space(space_id, query)
            for space_id in space_ids
        ]

        # Execute all tasks concurrently
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Filter out exceptions and failed results
        valid_results = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.warning(f"[MULTI-SPACE] Space {space_ids[i]} analysis failed: {result}")
                # Add failed result
                valid_results.append(SpaceAnalysisResult(
                    space_id=space_ids[i],
                    success=False,
                    content_summary=f"Analysis failed: {str(result)}"
                ))
            else:
                valid_results.append(result)

        logger.info(f"[MULTI-SPACE] Completed analysis: {len(valid_results)}/{len(space_ids)} successful")
        return valid_results

    async def _aggregate_space_data(self, space_id: int) -> Dict[str, Any]:
        """
        Aggregate data from all available sources: context_graph, Yabai, and CG windows.

        Returns unified data structure combining all sources.
        """
        aggregated_data = {
            "space_id": space_id,
            "windows": [],
            "apps": [],
            "window_count": 0,
            "sources_used": []
        }

        # Source 1: Context Graph (historical context and patterns)
        if self.context_graph and space_id in self.context_graph.spaces:
            space_ctx = self.context_graph.spaces[space_id]
            aggregated_data["context"] = {
                "applications": list(space_ctx.applications.keys()) if space_ctx.applications else [],
                "recent_events": space_ctx.get_recent_events(within_seconds=300),
                "patterns": space_ctx.patterns if hasattr(space_ctx, 'patterns') else []
            }
            aggregated_data["sources_used"].append("context_graph")
            logger.debug(f"[MULTI-SPACE] Space {space_id}: Got context from context_graph")

        # Source 2: Yabai (real-time window manager data)
        if self.yabai_detector and self.yabai_detector.is_available():
            try:
                yabai_windows = self.yabai_detector.get_windows_for_space(space_id)
                if yabai_windows:
                    aggregated_data["yabai_windows"] = yabai_windows
                    aggregated_data["window_count"] = max(aggregated_data["window_count"], len(yabai_windows))
                    for window in yabai_windows:
                        if "app" in window and window["app"] not in aggregated_data["apps"]:
                            aggregated_data["apps"].append(window["app"])
                    aggregated_data["sources_used"].append("yabai")
                    logger.debug(f"[MULTI-SPACE] Space {space_id}: Got {len(yabai_windows)} windows from Yabai")
            except Exception as e:
                logger.warning(f"[MULTI-SPACE] Yabai query failed for space {space_id}: {e}")

        # Source 3: Core Graphics (low-level window detection)
        # Reads the typed ``windows`` list (EnhancedWindowInfo), the one shape
        # both detector branches emit. ``spaces[id]`` is a per-space SUMMARY
        # whose nested "windows" are yabai- or kCG-keyed depending on branch,
        # and it was looked up with str(space_id) against int keys -- so this
        # source never contributed a window on any platform.
        if self.cg_window_detector:
            try:
                all_cg_windows = self.cg_window_detector.get_all_windows_across_spaces()
                cg_windows = [
                    {"app_name": w.app_name, "window_title": w.window_title}
                    for w in (all_cg_windows or {}).get("windows", [])
                    if w.space_id is not None and int(w.space_id) == space_id
                ]
                if cg_windows:
                    aggregated_data["cg_windows"] = cg_windows
                    aggregated_data["window_count"] = max(aggregated_data["window_count"], len(cg_windows))
                    aggregated_data["sources_used"].append("core_graphics")
                    logger.debug(f"[MULTI-SPACE] Space {space_id}: Got {len(cg_windows)} windows from Core Graphics")
            except Exception as e:
                logger.warning(f"[MULTI-SPACE] Core Graphics query failed for space {space_id}: {e}")

        # Merge window data from all sources
        all_windows = []

        # Add Yabai windows (most reliable)
        if "yabai_windows" in aggregated_data:
            all_windows.extend(aggregated_data["yabai_windows"])

        # Add CG windows (additional detail)
        if "cg_windows" in aggregated_data:
            for cg_win in aggregated_data["cg_windows"]:
                # Add if not already in list
                if not any(w.get("app") == cg_win.get("app_name") and
                          w.get("title") == cg_win.get("window_title")
                          for w in all_windows):
                    all_windows.append({
                        "app": cg_win.get("app_name"),
                        "title": cg_win.get("window_title"),
                        "source": "cg"
                    })

        aggregated_data["windows"] = all_windows

        logger.info(f"[MULTI-SPACE] Space {space_id}: Aggregated data from {len(aggregated_data['sources_used'])} sources - {aggregated_data['window_count']} windows, {len(aggregated_data['apps'])} apps")

        return aggregated_data

    async def _analyze_single_space(self, space_id: int, query: str) -> SpaceAnalysisResult:
        """
        Analyze a single space using unified data aggregation.

        Combines data from context_graph, Yabai, and Core Graphics windows.
        """
        start_time = datetime.now()

        # Validate space state first
        edge_case_result = await self.space_manager.handle_edge_case(space_id)

        # Handle edge cases
        if edge_case_result.edge_case == "not_exist":
            return SpaceAnalysisResult(
                space_id=space_id,
                success=False,
                content_summary=edge_case_result.message,
                analysis_time=(datetime.now() - start_time).total_seconds()
            )
        elif edge_case_result.edge_case == "empty":
            return SpaceAnalysisResult(
                space_id=space_id,
                success=True,
                content_summary=f"Space {space_id} is empty (no windows)",
                analysis_time=(datetime.now() - start_time).total_seconds()
            )
        elif edge_case_result.edge_case == "minimized_only":
            # Get apps from state info
            apps = edge_case_result.state_info.applications if edge_case_result.state_info else []
            app_list = ", ".join(apps[:2])
            return SpaceAnalysisResult(
                space_id=space_id,
                success=True,
                app_name=apps[0] if apps else "Unknown",
                content_summary=f"Space {space_id} has minimized windows only ({app_list})",
                significance="low",
                analysis_time=(datetime.now() - start_time).total_seconds()
            )
        elif edge_case_result.edge_case == "transitioning":
            if not edge_case_result.success:
                return SpaceAnalysisResult(
                    space_id=space_id,
                    success=False,
                    content_summary=edge_case_result.message,
                    analysis_time=(datetime.now() - start_time).total_seconds()
                )
            logger.info(f"[MULTI-SPACE] Space {space_id} stabilized after transition")

        try:
            # Aggregate data from all sources
            aggregated_data = await self._aggregate_space_data(space_id)

            # Extract unified data
            apps = aggregated_data.get("apps", [])
            window_count = aggregated_data.get("window_count", 0)
            windows = aggregated_data.get("windows", [])
            sources_used = aggregated_data.get("sources_used", [])

            # Handle empty space
            if window_count == 0 and not apps:
                return SpaceAnalysisResult(
                    space_id=space_id,
                    success=True,
                    content_summary="Empty space",
                    analysis_time=(datetime.now() - start_time).total_seconds()
                )

            # Determine primary app
            app_name = apps[0] if apps else "Unknown"

            # Check for errors from context (if available)
            errors = []
            if "context" in aggregated_data and "recent_events" in aggregated_data["context"]:
                for event in aggregated_data["context"]["recent_events"]:
                    if hasattr(event, 'event_type') and event.event_type.value == "error_detected":
                        errors.append(event.details.get("error", "Unknown error"))

            # Determine content type based on primary app
            content_type = "unknown"
            app_lower = app_name.lower()
            if errors:
                content_type = "error"
            elif any(term in app_lower for term in ["terminal", "iterm", "hyper", "wezterm"]):
                content_type = "terminal"
            elif any(term in app_lower for term in ["safari", "chrome", "firefox", "edge", "browser"]):
                content_type = "browser"
            elif any(term in app_lower for term in ["code", "vscode", "pycharm", "sublime", "vim", "cursor", "xcode"]):
                content_type = "code"
            elif any(term in app_lower for term in ["slack", "discord", "teams"]):
                content_type = "communication"
            elif any(term in app_lower for term in ["spotify", "music"]):
                content_type = "media"

            # Build intelligent content summary
            if window_count == 1:
                # Single window - show app and title if available
                window_title = windows[0].get("title", "") if windows else ""
                if window_title:
                    content_summary = f"{app_name}: {window_title}"
                else:
                    content_summary = f"{app_name}"
            elif window_count <= 3:
                # Few windows - list them
                content_summary = f"{window_count} windows: {', '.join(apps)}"
            else:
                # Many windows - summarize
                content_summary = f"{window_count} windows: {', '.join(apps[:3])}"
                if len(apps) > 3:
                    content_summary += f" (+{len(apps) - 3} more)"

            # Add error context if present
            if errors:
                content_summary += f" [⚠️ {len(errors)} error(s)]"

            # Add data source info for debugging
            if sources_used:
                logger.debug(f"[MULTI-SPACE] Space {space_id} analysis used sources: {', '.join(sources_used)}")

            analysis_time = (datetime.now() - start_time).total_seconds()

            return SpaceAnalysisResult(
                space_id=space_id,
                success=True,
                app_name=app_name,
                window_title=windows[0].get("title", "") if windows and len(windows) == 1 else None,
                content_type=content_type,
                content_summary=content_summary,
                errors=errors,
                significance="critical" if errors else "normal",
                analysis_time=analysis_time
            )

        except Exception as e:
            logger.error(f"[MULTI-SPACE] Error analyzing space {space_id}: {e}", exc_info=True)
            return SpaceAnalysisResult(
                space_id=space_id,
                success=False,
                content_summary=f"Analysis error: {str(e)}",
                analysis_time=(datetime.now() - start_time).total_seconds()
            )

    async def _compare_spaces(self, results: List[SpaceAnalysisResult], query: str) -> Dict[str, Any]:
        """
        Compare multiple spaces and identify key differences.
        """
        comparison = {
            "spaces": [r.space_id for r in results],
            "summary": {},
            "differences": [],
            "similarities": []
        }

        # Build summary for each space
        for result in results:
            comparison["summary"][result.space_id] = {
                "app": result.app_name,
                "type": result.content_type,
                "has_errors": len(result.errors) > 0,
                "error_count": len(result.errors),
                "significance": result.significance
            }

        # Find differences
        if len(results) >= 2:
            # Compare first two spaces
            space1, space2 = results[0], results[1]

            if space1.content_type != space2.content_type:
                comparison["differences"].append({
                    "type": "content_type",
                    "space1": space1.content_type,
                    "space2": space2.content_type,
                    "description": f"Space {space1.space_id} is {space1.content_type}, Space {space2.space_id} is {space2.content_type}"
                })

            if space1.app_name != space2.app_name:
                comparison["differences"].append({
                    "type": "application",
                    "space1": space1.app_name,
                    "space2": space2.app_name,
                    "description": f"Space {space1.space_id} has {space1.app_name}, Space {space2.space_id} has {space2.app_name}"
                })

            if len(space1.errors) != len(space2.errors):
                comparison["differences"].append({
                    "type": "errors",
                    "space1": len(space1.errors),
                    "space2": len(space2.errors),
                    "description": f"Space {space1.space_id} has {len(space1.errors)} error(s), Space {space2.space_id} has {len(space2.errors)} error(s)"
                })

        return comparison

    async def _detect_differences(self, results: List[SpaceAnalysisResult]) -> List[Dict[str, Any]]:
        """Detect all differences between spaces"""
        differences = []

        for i, result1 in enumerate(results):
            for result2 in results[i+1:]:
                if result1.content_type != result2.content_type:
                    differences.append({
                        "space1": result1.space_id,
                        "space2": result2.space_id,
                        "difference_type": "content_type",
                        "value1": result1.content_type,
                        "value2": result2.content_type
                    })

                if result1.app_name != result2.app_name:
                    differences.append({
                        "space1": result1.space_id,
                        "space2": result2.space_id,
                        "difference_type": "application",
                        "value1": result1.app_name,
                        "value2": result2.app_name
                    })

        return differences

    async def _search_across_spaces(self, results: List[SpaceAnalysisResult], query: str) -> List[Dict[str, Any]]:
        """
        Search for specific content across all spaces.

        Examples:
        - "Find the terminal"
        - "Which space has the error?"
        """
        query_lower = query.lower()
        matches = []

        # Extract search term
        search_term = self._extract_search_term(query)
        logger.debug(f"[MULTI-SPACE] Searching for: '{search_term}'")

        for result in results:
            if not result.success:
                continue

            # Check for matches
            match_score = 0.0
            match_reasons = []

            # Search in app name
            if search_term and result.app_name and search_term in result.app_name.lower():
                match_score += 0.5
                match_reasons.append(f"App name contains '{search_term}'")

            # Search in content type
            if search_term and result.content_type and search_term in result.content_type.lower():
                match_score += 0.4
                match_reasons.append(f"Content type is '{search_term}'")

            # Search for errors if query mentions error
            if "error" in query_lower and result.errors:
                match_score += 0.6
                match_reasons.append(f"Has {len(result.errors)} error(s)")

            # Search for terminal
            if "terminal" in query_lower and result.content_type == "terminal":
                match_score += 0.8
                match_reasons.append("Is a terminal")

            if match_score > 0:
                matches.append({
                    "space_id": result.space_id,
                    "score": match_score,
                    "reasons": match_reasons,
                    "content": result.content_summary
                })

        # Sort by score
        matches.sort(key=lambda m: m["score"], reverse=True)
        return matches

    def _extract_search_term(self, query: str) -> Optional[str]:
        """Extract the search term from a query"""
        query_lower = query.lower()

        # Try patterns
        patterns = [
            r'find\s+(?:the\s+)?(\w+)',
            r'which\s+space\s+has\s+(?:the\s+)?(\w+)',
            r'locate\s+(?:the\s+)?(\w+)',
            r'where\s+is\s+(?:the\s+)?(\w+)',
        ]

        for pattern in patterns:
            match = re.search(pattern, query_lower)
            if match:
                return match.group(1)

        return None

    async def _synthesize_response(self, query_type: MultiSpaceQueryType,
                                   results: List[SpaceAnalysisResult],
                                   query: str,
                                   comparison: Optional[Dict[str, Any]],
                                   differences: Optional[List[Dict[str, Any]]],
                                   search_matches: Optional[List[Dict[str, Any]]]) -> str:
        """
        Synthesize a unified natural language response.
        """
        if query_type == MultiSpaceQueryType.COMPARE:
            return self._synthesize_comparison_response(results, comparison)
        elif query_type == MultiSpaceQueryType.DIFFERENCE:
            return self._synthesize_difference_response(results, differences)
        elif query_type in [MultiSpaceQueryType.SEARCH, MultiSpaceQueryType.LOCATE]:
            return self._synthesize_search_response(results, search_matches, query)
        elif query_type == MultiSpaceQueryType.SUMMARY:
            return self._synthesize_summary_response(results)
        else:
            # Generic response
            return self._synthesize_generic_response(results)

    def _synthesize_comparison_response(self, results: List[SpaceAnalysisResult],
                                       comparison: Optional[Dict[str, Any]]) -> str:
        """Generate comparison response"""
        if not results:
            return "No spaces to compare."

        if len(results) < 2:
            return f"Space {results[0].space_id}: {results[0].content_summary}"

        # Build comparison
        lines = []
        for result in results:
            if result.success:
                error_part = f" with {len(result.errors)} error(s)" if result.errors else ""
                lines.append(f"Space {result.space_id}: {result.app_name}{error_part}")

        # Add differences
        if comparison and comparison.get("differences"):
            lines.append("\nKey Differences:")
            for diff in comparison["differences"][:3]:  # Top 3
                lines.append(f"  • {diff['description']}")

        return "\n".join(lines)

    def _synthesize_difference_response(self, results: List[SpaceAnalysisResult],
                                       differences: Optional[List[Dict[str, Any]]]) -> str:
        """Generate difference response"""
        if not differences:
            return "The spaces appear similar."

        lines = ["Differences found:"]
        for diff in differences[:5]:  # Top 5
            lines.append(
                f"  • Space {diff['space1']} ({diff['value1']}) vs "
                f"Space {diff['space2']} ({diff['value2']})"
            )

        return "\n".join(lines)

    def _synthesize_search_response(self, results: List[SpaceAnalysisResult],
                                   search_matches: Optional[List[Dict[str, Any]]],
                                   query: str) -> str:
        """Generate search response"""
        if not search_matches:
            return "No matches found across the spaces analyzed."

        # Top match
        top_match = search_matches[0]
        space_id = top_match["space_id"]

        # Find full result
        result = next((r for r in results if r.space_id == space_id), None)

        if result:
            reasons = ", ".join(top_match["reasons"])
            response = f"Found in Space {space_id}: {result.content_summary}\n({reasons})"

            # Add other matches if available
            if len(search_matches) > 1:
                others = [f"Space {m['space_id']}" for m in search_matches[1:3]]
                response += f"\n\nAlso found in: {', '.join(others)}"

            return response
        else:
            return f"Found in Space {space_id}"

    def _synthesize_summary_response(self, results: List[SpaceAnalysisResult]) -> str:
        """Generate summary response"""
        lines = [f"Summary of {len(results)} space(s):"]
        for result in results:
            if result.success:
                lines.append(f"  • Space {result.space_id}: {result.content_summary}")

        return "\n".join(lines)

    def _synthesize_generic_response(self, results: List[SpaceAnalysisResult]) -> str:
        """Generate generic response"""
        return self._synthesize_summary_response(results)

    def _calculate_confidence(self, results: List[SpaceAnalysisResult],
                             query_type: MultiSpaceQueryType) -> float:
        """Calculate confidence in the analysis"""
        if not results:
            return 0.0

        successful = sum(1 for r in results if r.success)
        success_rate = successful / len(results)

        # Base confidence on success rate
        confidence = success_rate * 0.7

        # Boost for complete results
        if all(r.app_name for r in results if r.success):
            confidence += 0.2

        # Boost for specific query types
        if query_type in [MultiSpaceQueryType.COMPARE, MultiSpaceQueryType.DIFFERENCE]:
            if len(results) >= 2:
                confidence += 0.1

        return min(1.0, confidence)

    async def _record_query_pattern(
        self,
        query: str,
        query_type: MultiSpaceQueryType,
        spaces_analyzed: List[int],
        results: List[SpaceAnalysisResult],
        confidence: float,
        execution_time: float
    ):
        """Record multi-space query pattern to learning database"""
        try:
            # Extract apps and window titles from results
            apps_involved = [r.app_name for r in results if r.app_name]
            window_titles = [r.window_title for r in results if r.window_title]

            # Build pattern data
            pattern_data = {
                "query_type": query_type.value,
                "spaces_analyzed": spaces_analyzed,
                "space_count": len(spaces_analyzed),
                "apps_involved": apps_involved,
                "window_titles": window_titles,
                "successful_captures": sum(1 for r in results if r.success),
                "total_captures": len(results)
            }

            # Store as behavioral pattern
            await self.learning_db.store_behavioral_pattern(
                behavior_type="multi_space_query",
                description=f"{query_type.value}: {query}",
                pattern_data=pattern_data,
                confidence=confidence,
                metadata={
                    "execution_time": execution_time,
                    "query": query,
                    "timestamp": datetime.now().isoformat()
                }
            )

            logger.debug(f"[MULTI-SPACE] Recorded query pattern: {query_type.value}")

        except Exception as e:
            logger.warning(f"[MULTI-SPACE] Failed to record query pattern: {e}")


# ============================================================================
# GLOBAL INSTANCE MANAGEMENT
# ============================================================================

_global_handler: Optional[MultiSpaceQueryHandler] = None


def get_multi_space_handler() -> Optional[MultiSpaceQueryHandler]:
    """Get the global multi-space query handler"""
    return _global_handler


def initialize_multi_space_handler(context_graph=None, implicit_resolver=None,
                                   contextual_resolver=None, learning_db=None,
                                   yabai_detector=None, cg_window_detector=None) -> MultiSpaceQueryHandler:
    """Initialize the global multi-space query handler"""
    global _global_handler
    _global_handler = MultiSpaceQueryHandler(
        context_graph,
        implicit_resolver,
        contextual_resolver,
        learning_db,
        yabai_detector,
        cg_window_detector
    )
    logger.info("[MULTI-SPACE] Global handler initialized")
    return _global_handler
