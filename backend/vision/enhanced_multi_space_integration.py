#!/usr/bin/env python3
"""
Enhanced Multi-Space Integration
Integrates all improvements into the existing JARVIS vision system
"""

import logging
from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime
import asyncio

# Import new components
from .macos_space_detector import MacOSSpaceDetector, SpaceDetectorAdapter
from .reliable_screenshot_capture import ReliableScreenshotCapture
from .cross_space_context import CrossSpaceContextAnalyzer
from .adaptive_detection_learning import AdaptiveDetectionLearning

# Import existing components
from .multi_space_window_detector import MultiSpaceWindowDetector
from .multi_space_capture_engine import MultiSpaceCaptureEngine
from .multi_space_intelligence import MultiSpaceIntelligenceExtension

logger = logging.getLogger(__name__)

class EnhancedMultiSpaceSystem:
    """
    Enhanced multi-space system with all improvements integrated

    Capability-Namespace: space

    The natural-language door into multi-space intelligence, and the reason the
    capture engine's own `capture_all_spaces` is left undeclared: a model asking
    "what am I working on across my desktops" should hand over a SENTENCE, not
    assemble a `SpaceCaptureRequest`. Everything underneath — native space
    detection, reliable capture, cross-space context analysis — is reached
    through this one string.

    Both methods here are read-only in the sense that matters: they observe
    screens and return a description. Neither holds anything open, so neither is
    a session; the engine's `start_monitoring_session` is where that distinction
    lives.
    """

    def __init__(self, vision_intelligence=None):
        self.vision_intelligence = vision_intelligence

        # Initialize new components
        self.space_detector = MacOSSpaceDetector()
        self.space_adapter = SpaceDetectorAdapter()
        self.screenshot_capture = ReliableScreenshotCapture()
        self.context_analyzer = CrossSpaceContextAnalyzer()
        self.adaptive_learning = AdaptiveDetectionLearning()

        # Keep existing components for compatibility
        self.window_detector = MultiSpaceWindowDetector()
        self.capture_engine = MultiSpaceCaptureEngine()
        self.intelligence = MultiSpaceIntelligenceExtension()

        logger.info("Enhanced Multi-Space System initialized with all improvements")

    async def analyze_desktop_spaces(self, query: str) -> Dict[str, Any]:
        """
        Main entry point for desktop space analysis with all enhancements

        Capability: read-only

        Args:
            query: What to find out across the desktop spaces, in plain
                language (e.g. "what is running on my other desktops")
        """
        start_time = datetime.now()

        # Step 1: Use adaptive learning to detect intent
        intent, confidence = self.adaptive_learning.detect_intent_adaptive(
            query,
            context={'previous_queries': self._get_recent_queries()}
        )

        logger.info(f"[ENHANCED] Detected intent: {intent} (confidence: {confidence:.2f})")

        # Step 2: Get accurate space information using macOS APIs
        spaces = self.space_detector.get_all_spaces()
        space_data = self.space_adapter.detect_spaces_and_windows()

        logger.info(f"[ENHANCED] Detected {len(spaces)} spaces using native APIs")

        # Step 3: Capture screenshots reliably
        screenshots = await self._capture_all_spaces_enhanced()

        # Step 4: Analyze with cross-space context awareness
        context_analysis = self.context_analyzer.analyze_cross_space_context(
            space_data['spaces'],
            screenshots
        )

        # Step 5: Generate intelligent response
        response = await self._generate_enhanced_response(
            query,
            intent,
            spaces,
            screenshots,
            context_analysis
        )

        # Step 6: Learn from the interaction
        success = self._evaluate_response_quality(response)
        self.adaptive_learning.learn_from_interaction(
            query,
            intent,
            success,
            context={'spaces': len(spaces), 'screenshots': len(screenshots)}
        )

        # Calculate processing time
        processing_time = (datetime.now() - start_time).total_seconds()

        return {
            'response': response,
            'intent': intent,
            'confidence': confidence,
            'spaces_detected': len(spaces),
            'context': context_analysis,
            'processing_time': processing_time,
            'success': success
        }

    async def _capture_all_spaces_enhanced(self) -> Dict[int, Any]:
        """
        Capture screenshots using the reliable capture system
        """
        screenshots = {}

        # Use new reliable capture system
        capture_results = self.screenshot_capture.capture_all_spaces()

        for space_id, result in capture_results.items():
            if result.success and result.image:
                screenshots[space_id] = {
                    'image': result.image,
                    'method': result.method,
                    'metadata': result.metadata
                }
                logger.info(f"[ENHANCED] Captured space {space_id} using {result.method}")
            else:
                logger.warning(f"[ENHANCED] Failed to capture space {space_id}: {result.error}")

        # Fallback to existing capture engine if needed
        if not screenshots:
            logger.info("[ENHANCED] Using fallback capture method")
            try:
                fallback_screenshots = await self.capture_engine.capture_all_spaces()
                screenshots = fallback_screenshots
            except Exception as e:
                logger.error(f"[ENHANCED] Fallback capture also failed: {e}")

        return screenshots

    async def _generate_enhanced_response(
        self,
        query: str,
        intent: str,
        spaces: List,
        screenshots: Dict,
        context_analysis: Dict
    ) -> str:
        """
        Generate an intelligent, context-aware response
        """
        # Build comprehensive workspace description
        workspace_description = self._build_workspace_description(
            spaces,
            context_analysis
        )

        # Use vision intelligence if available and screenshots exist
        if self.vision_intelligence and screenshots:
            logger.info("[ENHANCED] Using Claude Vision API for intelligent analysis")

            # Prepare context for Claude
            enhanced_prompt = self._build_enhanced_prompt(
                query,
                workspace_description,
                context_analysis
            )

            # Get Claude's analysis
            try:
                # Use the first available screenshot for now
                # In production, could composite multiple screenshots
                primary_screenshot = next(iter(screenshots.values()))
                if primary_screenshot and 'image' in primary_screenshot:
                    response = await self.vision_intelligence.understand_and_respond(
                        primary_screenshot['image'],
                        enhanced_prompt
                    )
                    return response
            except Exception as e:
                logger.error(f"[ENHANCED] Vision API error: {e}")

        # Fallback to detailed context-based response
        return self._generate_detailed_response(
            query,
            intent,
            workspace_description,
            context_analysis
        )

    def _build_workspace_description(
        self,
        spaces: List,
        context_analysis: Dict
    ) -> str:
        """
        Build a detailed workspace description
        """
        lines = []

        # Overall summary
        activity_summary = context_analysis.get('activity_summary', {})
        lines.append(f"You have {activity_summary.get('active_spaces', 0)} active spaces "
                    f"out of {activity_summary.get('total_spaces', 0)} total spaces.")

        # Workflow information
        workflow = context_analysis.get('workflow', {})
        if workflow:
            lines.append(f"Primary activity: {workflow.get('primary_task', 'General work')}")
            lines.append(f"Workflow type: {workflow.get('workflow_type', 'general')}")
            lines.append(f"Activity intensity: {workflow.get('intensity_level', 0):.0%}")

        # Individual space details
        space_contexts = context_analysis.get('space_contexts', {})
        for space in spaces[:6]:  # Limit to 6 spaces for readability
            space_id = space.space_id
            context = space_contexts.get(space_id)

            if context:
                status = "(CURRENT)" if space.is_current else ""
                # Use the actual workspace name instead of generic "Desktop N"
                space_name = space.space_name if hasattr(space, 'space_name') else f"Desktop {space_id}"
                lines.append(f"\n**{space_name} {status}**")
                lines.append(f"  Purpose: {context.purpose}")
                lines.append(f"  Task: {context.active_task}")
                lines.append(f"  Apps: {', '.join(space.applications[:5])}")

                if context.key_files:
                    lines.append(f"  Files: {', '.join(context.key_files[:3])}")

                if context.context_tags:
                    lines.append(f"  Tags: {', '.join(list(context.context_tags)[:5])}")

        # Insights
        insights = context_analysis.get('insights', [])
        if insights:
            lines.append("\n**Key Insights:**")
            for insight in insights[:3]:
                lines.append(f"  • {insight.description}")

        # Recommendations
        recommendations = context_analysis.get('recommendations', [])
        if recommendations:
            lines.append("\n**Recommendations:**")
            for rec in recommendations[:3]:
                lines.append(f"  • {rec}")

        return '\n'.join(lines)

    def _build_enhanced_prompt(
        self,
        query: str,
        workspace_description: str,
        context_analysis: Dict
    ) -> str:
        """
        Build an enhanced prompt for Claude Vision API
        """
        workflow = context_analysis.get('workflow', {})

        prompt = f"""You are analyzing a macOS desktop with multiple spaces.

IMPORTANT: Use the actual workspace names provided below, NOT generic "Desktop 1", "Desktop 2" labels.

User Query: {query}

Workspace Context:
{workspace_description}

Current Workflow Type: {workflow.get('workflow_type', 'general')}
Primary Task: {workflow.get('primary_task', 'General work')}

When describing the spaces, use the actual workspace names from the context above (like "J.A.R.V.I.S. interface", "Cursor", "Code", "Terminal", "Google Chrome", etc.) instead of generic desktop numbers.

Please provide a detailed, intelligent analysis of what's happening across the desktop spaces.
Focus on:
1. What the user is actively working on
2. How their work is organized across spaces
3. Any patterns or insights about their workflow
4. Specific details about applications, files, and activities
5. Any recommendations for productivity

Be specific and mention actual applications, files, and content you observe."""

        return prompt

    def _generate_detailed_response(
        self,
        query: str,
        intent: str,
        workspace_description: str,
        context_analysis: Dict
    ) -> str:
        """
        Generate a detailed fallback response
        """
        response_parts = []

        # Start with intent-specific introduction
        if intent == 'multi_space_query':
            response_parts.append("Here's what's happening across your desktop spaces:")
        elif intent == 'single_space_query':
            response_parts.append("Here's what's on your current space:")
        else:
            response_parts.append("Here's your workspace analysis:")

        # Add workspace description
        response_parts.append("\n" + workspace_description)

        # Add workflow analysis
        workflow = context_analysis.get('workflow', {})
        if workflow.get('intensity_level', 0) > 0.7:
            response_parts.append(
                f"\nYou appear to be in an intensive {workflow.get('workflow_type', 'work')} session "
                f"with high activity across multiple spaces."
            )

        # Add space relationships
        relationships = context_analysis.get('relationships', {})
        if relationships.get('clusters'):
            response_parts.append(
                f"\nI notice you have related work clustered in spaces: "
                f"{', '.join(str(s) for s in relationships['clusters'][0]['spaces'])}"
            )

        return '\n'.join(response_parts)

    def _evaluate_response_quality(self, response: str) -> bool:
        """
        Evaluate if the response was successful
        """
        # Check if response contains specific details
        quality_indicators = [
            'Desktop' in response,
            'space' in response.lower(),
            any(app in response.lower() for app in ['chrome', 'code', 'terminal', 'cursor']),
            len(response) > 200  # Sufficient detail
        ]

        return sum(quality_indicators) >= 2

    def _get_recent_queries(self) -> List[str]:
        """
        Get recent queries for context
        """
        # In production, this would retrieve from a query history store
        return []

    async def handle_vision_command(self, command: str) -> Dict[str, Any]:
        """
        Main handler for vision commands - replaces existing handler

        Capability: read-only

        Routes to `analyze_desktop_spaces` when its adaptive detector is
        confident the command is multi-space, and otherwise returns
        `handled: False` rather than guessing. Declared alongside the method it
        delegates to because a caller that cannot tell whether a command is
        multi-space is exactly who should be allowed to ask.

        Args:
            command: The user's vision command, verbatim
        """
        # Check if this is a multi-space query using enhanced detection
        intent, confidence = self.adaptive_learning.detect_intent_adaptive(command)

        if intent == 'multi_space_query' and confidence > 0.6:
            logger.info(f"[ENHANCED] Processing multi-space query with confidence {confidence:.2f}")
            return await self.analyze_desktop_spaces(command)

        # Fallback to regular vision handling
        logger.info("[ENHANCED] Falling back to standard vision processing")
        # Call existing vision handler
        return {'handled': False, 'reason': 'Not a multi-space query'}


def integrate_with_existing_system():
    """
    Integration function to replace existing components
    """
    logger.info("Integrating enhanced multi-space system...")

    # This function should be called from the main vision initialization
    # to replace the existing multi-space components with enhanced versions

    # The enhanced system is backward compatible and can be dropped in
    # as a replacement for the existing MultiSpaceWindowDetector,
    # MultiSpaceCaptureEngine, and related components

    return EnhancedMultiSpaceSystem