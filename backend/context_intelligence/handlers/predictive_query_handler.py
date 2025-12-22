"""
Predictive Query Handler for JARVIS
====================================

High-level handler for predictive/analytical queries that integrates:
- Predictive Analyzer for metrics and insights
- Claude Vision for semantic code analysis
- Context Graph for workspace awareness
- Screen capture for visual analysis

Handles queries like:
- "Am I making progress?"
- "What should I work on next?"
- "Are there any potential bugs?"
- "Explain what this code does"

Author: Derek Russell
Date: 2025-10-19
"""

import asyncio
import logging
import base64
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from backend.context_intelligence.analyzers.predictive_analyzer import (
    PredictiveAnalyzer,
    AnalyticsResult,
    PredictiveQueryType,
    AnalysisScope,
    get_predictive_analyzer,
    initialize_predictive_analyzer
)

logger = logging.getLogger(__name__)


# ============================================================================
# CLAUDE VISION INTEGRATION
# ============================================================================

class ClaudeVisionAnalyzer:
    """
    Integrates Claude Vision API for semantic code analysis

    Uses Claude to analyze:
    - Code screenshots for understanding
    - Terminal output for error analysis
    - IDE views for context understanding
    """

    def __init__(self, api_key: Optional[str] = None):
        """Initialize Claude Vision analyzer"""
        self.api_key = api_key
        self._claude_available = self._check_claude_availability()

    def _check_claude_availability(self) -> bool:
        """Check if Claude API is available"""
        try:
            import anthropic
            return True
        except ImportError:
            logger.warning("[CLAUDE-VISION] Anthropic library not available - install with: pip install anthropic")
            return False

    async def analyze_code_screenshot(
        self,
        image_path: str,
        query: str,
        context: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Analyze a code screenshot using Claude Vision

        Args:
            image_path: Path to screenshot
            query: What to analyze (e.g., "explain this code", "find bugs")
            context: Additional context

        Returns:
            Analysis result with insights
        """
        if not self._claude_available:
            return {
                "success": False,
                "error": "Claude API not available",
                "message": "Install anthropic library for Claude Vision support"
            }

        try:
            import anthropic

            # Read and encode image
            with open(image_path, "rb") as f:
                image_data = base64.b64encode(f.read()).decode("utf-8")

            # Determine image type
            suffix = Path(image_path).suffix.lower()
            media_type = {
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".webp": "image/webp",
                ".gif": "image/gif"
            }.get(suffix, "image/png")

            # Create Claude client
            client = anthropic.Anthropic(api_key=self.api_key)

            # Build prompt based on query type
            prompt = self._build_vision_prompt(query, context)

            # Call Claude Vision API
            logger.info(f"[CLAUDE-VISION] Analyzing screenshot: {image_path}")

            response = client.messages.create(
                model="claude-3-5-sonnet-20241022",
                max_tokens=2048,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": media_type,
                                    "data": image_data
                                }
                            },
                            {
                                "type": "text",
                                "text": prompt
                            }
                        ]
                    }
                ]
            )

            # Extract response
            analysis_text = response.content[0].text

            logger.info(f"[CLAUDE-VISION] Analysis complete, {len(analysis_text)} chars")

            return {
                "success": True,
                "analysis": analysis_text,
                "model": "claude-3-5-sonnet-20241022",
                "timestamp": datetime.now().isoformat()
            }

        except Exception as e:
            logger.error(f"[CLAUDE-VISION] Error analyzing screenshot: {e}")
            return {
                "success": False,
                "error": str(e)
            }

    def _build_vision_prompt(self, query: str, context: Optional[Dict[str, Any]]) -> str:
        """Build a prompt for Claude Vision based on query type"""
        base_prompt = "You are analyzing a screenshot from a developer's workspace.\n\n"

        # Add context if available
        if context:
            if context.get("space_id"):
                base_prompt += f"This is from Space {context['space_id']}.\n"
            if context.get("app_name"):
                base_prompt += f"Application: {context['app_name']}\n"
            base_prompt += "\n"

        # Add query-specific instructions
        query_lower = query.lower()

        if "explain" in query_lower or "what does" in query_lower:
            base_prompt += "Please explain what this code does. Be concise and focus on:\n"
            base_prompt += "- Main purpose and functionality\n"
            base_prompt += "- Key components or functions\n"
            base_prompt += "- Any notable patterns or techniques\n"

        elif "bug" in query_lower or "error" in query_lower or "issue" in query_lower:
            base_prompt += "Analyze this for potential bugs or issues. Look for:\n"
            base_prompt += "- Syntax errors or typos\n"
            base_prompt += "- Logic errors or anti-patterns\n"
            base_prompt += "- Performance issues\n"
            base_prompt += "- Security vulnerabilities\n"
            base_prompt += "- Code smells or maintainability concerns\n"

        elif "improve" in query_lower or "optimize" in query_lower:
            base_prompt += "Suggest improvements for this code. Consider:\n"
            base_prompt += "- Code quality and readability\n"
            base_prompt += "- Performance optimization\n"
            base_prompt += "- Best practices\n"
            base_prompt += "- Maintainability\n"

        elif "test" in query_lower:
            base_prompt += "Analyze the testing approach. Look at:\n"
            base_prompt += "- Test coverage\n"
            base_prompt += "- Test quality and assertions\n"
            base_prompt += "- Missing test cases\n"

        else:
            # Generic analysis
            base_prompt += f"User query: {query}\n\n"
            base_prompt += "Provide a helpful analysis addressing their question.\n"

        return base_prompt

    async def analyze_terminal_output(
        self,
        terminal_text: str,
        query: str = "analyze this terminal output"
    ) -> Dict[str, Any]:
        """
        Analyze terminal output using Claude (text mode)

        Args:
            terminal_text: Terminal output text
            query: What to analyze

        Returns:
            Analysis result
        """
        if not self._claude_available:
            return {
                "success": False,
                "error": "Claude API not available"
            }

        try:
            import anthropic

            client = anthropic.Anthropic(api_key=self.api_key)

            prompt = f"""Analyze this terminal output:

```
{terminal_text}
```

{query}

Provide:
1. What happened (summary)
2. Any errors or warnings
3. Recommended actions (if applicable)

Be concise and actionable."""

            response = client.messages.create(
                model="claude-3-5-sonnet-20241022",
                max_tokens=1024,
                messages=[
                    {
                        "role": "user",
                        "content": prompt
                    }
                ]
            )

            analysis_text = response.content[0].text

            return {
                "success": True,
                "analysis": analysis_text,
                "timestamp": datetime.now().isoformat()
            }

        except Exception as e:
            logger.error(f"[CLAUDE-VISION] Error analyzing terminal: {e}")
            return {
                "success": False,
                "error": str(e)
            }


# ============================================================================
# MAIN PREDICTIVE QUERY HANDLER
# ============================================================================

@dataclass
class PredictiveQueryRequest:
    """Request for predictive query"""
    query: str
    space_id: Optional[int] = None
    capture_screen: bool = False
    use_vision: bool = False
    repo_path: Optional[str] = None
    additional_context: Dict[str, Any] = None


@dataclass
class PredictiveQueryResponse:
    """Response from predictive query"""
    success: bool
    query: str
    response_text: str
    analytics: Optional[AnalyticsResult] = None
    vision_analysis: Optional[Dict[str, Any]] = None
    confidence: float = 0.0
    timestamp: datetime = None
    metadata: Dict[str, Any] = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now()
        if self.metadata is None:
            self.metadata = {}


class PredictiveQueryHandler:
    """
    High-level handler for predictive/analytical queries

    Coordinates:
    - Predictive Analyzer for metrics/insights
    - Claude Vision for semantic analysis
    - Context Graph for workspace awareness
    - Screen capture for visual analysis
    """

    def __init__(
        self,
        context_graph=None,
        claude_api_key: Optional[str] = None,
        enable_vision: bool = True
    ):
        """Initialize the predictive query handler"""
        self.context_graph = context_graph

        # Initialize predictive analyzer
        self.analyzer = get_predictive_analyzer()
        if not self.analyzer:
            self.analyzer = initialize_predictive_analyzer(context_graph)

        # Initialize Claude Vision
        self.vision_analyzer = None
        if enable_vision:
            self.vision_analyzer = ClaudeVisionAnalyzer(api_key=claude_api_key)

        logger.info("[PREDICTIVE-HANDLER] Initialized")

    async def handle_query(self, request: PredictiveQueryRequest) -> PredictiveQueryResponse:
        """
        Handle a predictive/analytical query

        Args:
            request: Query request

        Returns:
            Query response with analysis
        """
        logger.info(f"[PREDICTIVE-HANDLER] Handling query: '{request.query}'")

        try:
            # Build context
            context = await self._build_context(request)

            # Run predictive analysis
            analytics = await self.analyzer.analyze(
                query=request.query,
                scope=AnalysisScope.CURRENT_SPACE if request.space_id else AnalysisScope.ALL_SPACES,
                context=context
            )

            # Run vision analysis if requested
            vision_analysis = None
            if request.use_vision and self.vision_analyzer:
                vision_analysis = await self._run_vision_analysis(request, analytics, context)

            # Combine results
            response_text = await self._combine_results(analytics, vision_analysis, request)

            # Calculate overall confidence
            confidence = analytics.confidence
            if vision_analysis and vision_analysis.get("success"):
                confidence = (confidence + 0.9) / 2  # Average with high vision confidence

            return PredictiveQueryResponse(
                success=True,
                query=request.query,
                response_text=response_text,
                analytics=analytics,
                vision_analysis=vision_analysis,
                confidence=confidence,
                metadata={
                    "space_id": request.space_id,
                    "used_vision": vision_analysis is not None,
                    "query_type": analytics.query_type.value
                }
            )

        except Exception as e:
            logger.error(f"[PREDICTIVE-HANDLER] Error handling query: {e}", exc_info=True)
            return PredictiveQueryResponse(
                success=False,
                query=request.query,
                response_text=f"Error processing query: {str(e)}",
                confidence=0.0
            )

    async def _build_context(self, request: PredictiveQueryRequest) -> Dict[str, Any]:
        """Build context for analysis"""
        context = request.additional_context or {}

        # Add space ID
        if request.space_id:
            context["space_id"] = request.space_id

        # Add repo path
        if request.repo_path:
            context["repo_path"] = request.repo_path
        elif not context.get("repo_path"):
            context["repo_path"] = "."  # Default to cwd

        # Add workspace info from context graph if available
        if self.context_graph and request.space_id:
            try:
                space_info = self.context_graph.get_space_summary(request.space_id)
                if space_info:
                    context["workspace"] = space_info
            except Exception as e:
                logger.warning(f"[PREDICTIVE-HANDLER] Could not get space info: {e}")

        return context

    async def _run_vision_analysis(
        self,
        request: PredictiveQueryRequest,
        analytics: AnalyticsResult,
        context: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Run Claude Vision analysis if appropriate"""
        if not self.vision_analyzer:
            return None

        # Determine if vision analysis would be helpful
        needs_vision = analytics.query_type in [
            PredictiveQueryType.CODE_EXPLANATION,
            PredictiveQueryType.BUG_DETECTION,
            PredictiveQueryType.QUALITY_ASSESSMENT,
            PredictiveQueryType.PATTERN_ANALYSIS
        ]

        if not needs_vision and not request.capture_screen:
            logger.debug("[PREDICTIVE-HANDLER] Vision analysis not needed for this query type")
            return None

        # Get screenshot path
        screenshot_path = await self._get_screenshot(request.space_id)
        if not screenshot_path:
            logger.warning("[PREDICTIVE-HANDLER] No screenshot available for vision analysis")
            return None

        # Run vision analysis
        return await self.vision_analyzer.analyze_code_screenshot(
            image_path=screenshot_path,
            query=request.query,
            context=context
        )

    async def _get_screenshot(self, space_id: Optional[int]) -> Optional[str]:
        """Get screenshot for analysis"""
        try:
            # Try to get from vision system
            from vision.yabai_space_detector import get_yabai_detector

            yabai = get_yabai_detector()
            if not yabai.is_available():
                logger.warning("[PREDICTIVE-HANDLER] Yabai not available for screenshot")
                return None

            # Use space_id if provided, otherwise current space
            if space_id:
                screenshot_path = f"/tmp/jarvis_space_{space_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
            else:
                screenshot_path = f"/tmp/jarvis_current_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"

            # Capture screenshot (implementation depends on your vision system)
            # For now, return None as placeholder
            logger.info(f"[PREDICTIVE-HANDLER] Screenshot would be captured to: {screenshot_path}")
            return None  # TODO: Implement actual screenshot capture

        except Exception as e:
            logger.error(f"[PREDICTIVE-HANDLER] Error capturing screenshot: {e}")
            return None

    async def _combine_results(
        self,
        analytics: AnalyticsResult,
        vision_analysis: Optional[Dict[str, Any]],
        request: PredictiveQueryRequest
    ) -> str:
        """Combine analytics and vision results into unified response"""
        response_parts = []

        # Add analytics response
        if analytics.response_text:
            response_parts.append(analytics.response_text)

        # Add vision analysis if available
        if vision_analysis and vision_analysis.get("success"):
            response_parts.append("\n## 🔍 Visual Analysis\n")
            response_parts.append(vision_analysis.get("analysis", ""))

        # Combine
        return "\n".join(response_parts)

    # ========================================================================
    # CONVENIENCE METHODS
    # ========================================================================

    async def check_progress(self, space_id: Optional[int] = None, repo_path: str = ".") -> PredictiveQueryResponse:
        """Convenience method: Check progress"""
        return await self.handle_query(PredictiveQueryRequest(
            query="Am I making progress?",
            space_id=space_id,
            repo_path=repo_path
        ))

    async def get_next_steps(self, space_id: Optional[int] = None, repo_path: str = ".") -> PredictiveQueryResponse:
        """Convenience method: Get next steps"""
        return await self.handle_query(PredictiveQueryRequest(
            query="What should I work on next?",
            space_id=space_id,
            repo_path=repo_path
        ))

    async def detect_bugs(self, space_id: Optional[int] = None, use_vision: bool = False) -> PredictiveQueryResponse:
        """Convenience method: Detect bugs"""
        return await self.handle_query(PredictiveQueryRequest(
            query="Are there any potential bugs?",
            space_id=space_id,
            use_vision=use_vision,
            capture_screen=use_vision
        ))

    async def explain_code(self, space_id: Optional[int] = None, use_vision: bool = True) -> PredictiveQueryResponse:
        """Convenience method: Explain code"""
        return await self.handle_query(PredictiveQueryRequest(
            query="Explain what this code does",
            space_id=space_id,
            use_vision=use_vision,
            capture_screen=use_vision
        ))


# ============================================================================
# GLOBAL INSTANCE
# ============================================================================

_global_handler: Optional[PredictiveQueryHandler] = None


def get_predictive_handler() -> Optional[PredictiveQueryHandler]:
    """Get the global predictive query handler"""
    return _global_handler


def initialize_predictive_handler(context_graph=None, **kwargs) -> PredictiveQueryHandler:
    """Initialize the global predictive query handler"""
    global _global_handler
    _global_handler = PredictiveQueryHandler(context_graph, **kwargs)
    logger.info("[PREDICTIVE-HANDLER] Global instance initialized")
    return _global_handler


# ============================================================================
# CONVENIENCE FUNCTIONS
# ============================================================================

async def handle_predictive_query(query: str, **kwargs) -> PredictiveQueryResponse:
    """Convenience function to handle a predictive query"""
    handler = get_predictive_handler()
    if not handler:
        handler = initialize_predictive_handler()

    request = PredictiveQueryRequest(query=query, **kwargs)
    return await handler.handle_query(request)
