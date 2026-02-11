"""
Computer Use Tool for JARVIS Autonomous Agent

This module exposes the Claude Computer Use capabilities as a tool
that can be used by the Neural Mesh, Autonomous Agent, and UAE.

This is the BRIDGE between:
- The Autonomy System (brain - decides WHEN to use computer)
- The Computer Use Connector (hands - executes actions)
- The UAE (awareness - knows WHERE things are)
- The Neural Mesh (coordination - manages agents)

Features:
- Async-first design
- UAE integration for intelligent position resolution
- Neural Mesh agent registration
- Multi-space vision awareness
- Dynamic configuration
- Comprehensive error handling

Usage:
    from backend.autonomy.computer_use_tool import ComputerUseTool, get_computer_use_tool

    tool = get_computer_use_tool()
    result = await tool.run(goal="Open Safari and search for weather")
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from uuid import uuid4

# Import base tool classes
from .langchain_tools import (
    JARVISTool,
    ToolMetadata,
    ToolCategory,
    ToolRiskLevel,
    ToolExecutionMode,
)

# Import configuration
try:
    from backend.core.agentic_config import get_agentic_config, AgenticConfig
except ImportError:
    from core.agentic_config import get_agentic_config, AgenticConfig

# Import Computer Use Connector
try:
    from backend.display.computer_use_connector import (
        ClaudeComputerUseConnector,
        get_computer_use_connector,
        TaskResult,
        TaskStatus,
        ActionType,
    )
    COMPUTER_USE_AVAILABLE = True
except ImportError:
    try:
        from display.computer_use_connector import (
            ClaudeComputerUseConnector,
            get_computer_use_connector,
            TaskResult,
            TaskStatus,
            ActionType,
        )
        COMPUTER_USE_AVAILABLE = True
    except ImportError:
        COMPUTER_USE_AVAILABLE = False
        ClaudeComputerUseConnector = None

# Import UAE for intelligent position resolution
try:
    from backend.intelligence.unified_awareness_engine import (
        UnifiedAwarenessEngine,
        get_uae_engine,
        UnifiedDecision,
        DecisionSource,
    )
    UAE_AVAILABLE = True
except ImportError:
    try:
        from intelligence.unified_awareness_engine import (
            UnifiedAwarenessEngine,
            get_uae_engine,
            UnifiedDecision,
            DecisionSource,
        )
        UAE_AVAILABLE = True
    except ImportError:
        UAE_AVAILABLE = False

# Import Multi-Space Vision
try:
    from backend.vision.multi_space_intelligence import (
        MultiSpaceQueryDetector,
        SpaceQueryType,
        SpaceQueryIntent,
    )
    MULTI_SPACE_AVAILABLE = True
except ImportError:
    try:
        from vision.multi_space_intelligence import (
            MultiSpaceQueryDetector,
            SpaceQueryType,
            SpaceQueryIntent,
        )
        MULTI_SPACE_AVAILABLE = True
    except ImportError:
        MULTI_SPACE_AVAILABLE = False

# Import Neural Mesh for agent registration
try:
    from backend.neural_mesh.base.base_neural_mesh_agent import BaseNeuralMeshAgent
    NEURAL_MESH_AVAILABLE = True
except ImportError:
    try:
        from neural_mesh.base.base_neural_mesh_agent import BaseNeuralMeshAgent
        NEURAL_MESH_AVAILABLE = True
    except ImportError:
        NEURAL_MESH_AVAILABLE = False
        BaseNeuralMeshAgent = object

logger = logging.getLogger(__name__)


# ============================================================================
# Data Classes
# ============================================================================

@dataclass
class ComputerUseResult:
    """Result of a computer use operation."""
    success: bool
    task_id: str
    goal: str
    status: str
    final_message: str
    actions_count: int
    total_duration_ms: float
    learning_insights: List[str]
    confidence: float
    uae_decision: Optional[Dict[str, Any]] = None
    multi_space_context: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class GoalAnalysis:
    """Analysis of a goal to determine the best execution strategy."""
    original_goal: str
    requires_vision: bool
    requires_multi_space: bool
    target_elements: List[str]
    estimated_complexity: str  # simple, moderate, complex
    suggested_approach: str
    space_query_intent: Optional[Any] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


# ============================================================================
# Computer Use Tool
# ============================================================================

class ComputerUseTool(JARVISTool):
    """
    Tool that allows the Autonomous Agent to control the computer interface
    to achieve goals that require visual interaction.

    This is the primary interface between:
    - The AI brain (reasoning, deciding WHAT to do)
    - The computer hands (vision, clicking, typing)

    Features:
    - UAE integration for element position awareness
    - Multi-space vision for cross-desktop operations
    - Dynamic goal analysis
    - Learning from interactions
    - Voice narration for transparency
    """

    def __init__(
        self,
        permission_manager: Optional[Any] = None,
        tts_callback: Optional[Callable[[str], Awaitable[None]]] = None,
        config: Optional[AgenticConfig] = None,
    ):
        """
        Initialize the Computer Use Tool.

        Args:
            permission_manager: Permission manager for action approval
            tts_callback: Text-to-speech callback for narration
            config: Agentic configuration
        """
        self.config = config or get_agentic_config()

        metadata = ToolMetadata(
            name="computer_use",
            description=(
                "Control the computer interface to achieve a goal. "
                "Use this for tasks requiring GUI interaction: clicking buttons, "
                "typing text, navigating apps, changing system settings, "
                "web browsing, and any visual automation. "
                "Input should be a clear, natural language goal describing what to accomplish."
            ),
            category=ToolCategory.AUTOMATION,
            risk_level=ToolRiskLevel.HIGH,  # Controls mouse/keyboard
            requires_permission=self.config.autonomy.require_permission_high_risk,
            execution_mode=ToolExecutionMode.ASYNC,
            capabilities=[
                "ui_automation",
                "vision_analysis",
                "mouse_control",
                "keyboard_control",
                "screen_capture",
                "app_navigation",
                "system_settings",
                "web_browsing",
            ],
            timeout_seconds=self.config.computer_use.api_timeout * 5,  # Allow multiple iterations
            tags=["computer_use", "vision", "automation", "gui"],
        )
        super().__init__(metadata, permission_manager)

        self.tts_callback = tts_callback

        # Lazy-initialized components
        self._connector: Optional[ClaudeComputerUseConnector] = None
        self._uae: Optional[UnifiedAwarenessEngine] = None
        self._multi_space_detector: Optional[MultiSpaceQueryDetector] = None

        # Metrics
        self._total_goals: int = 0
        self._successful_goals: int = 0
        self._failed_goals: int = 0

        logger.info("[ComputerUseTool] Initialized with dynamic configuration")

    async def _ensure_connector(self) -> ClaudeComputerUseConnector:
        """Lazy initialization of the Computer Use connector."""
        if not COMPUTER_USE_AVAILABLE:
            raise RuntimeError("Computer Use connector not available. Check imports.")

        if self._connector is None:
            try:
                self._connector = get_computer_use_connector(
                    tts_callback=self.tts_callback
                )
                logger.info("[ComputerUseTool] Connected to Computer Use backend")
            except Exception as e:
                logger.error(f"[ComputerUseTool] Failed to initialize connector: {e}")
                raise RuntimeError(f"Computer Use subsystem unavailable: {e}")

        return self._connector

    async def _ensure_uae(self) -> Optional[UnifiedAwarenessEngine]:
        """Lazy initialization of UAE."""
        if not UAE_AVAILABLE:
            return None

        if self._uae is None:
            try:
                self._uae = get_uae_engine()
                if not self._uae.is_active:
                    await self._uae.start()
                logger.info("[ComputerUseTool] Connected to UAE")
            except Exception as e:
                logger.warning(f"[ComputerUseTool] Could not initialize UAE: {e}")
                return None

        return self._uae

    def _ensure_multi_space_detector(self) -> Optional[MultiSpaceQueryDetector]:
        """Lazy initialization of multi-space detector."""
        if not MULTI_SPACE_AVAILABLE:
            return None

        if self._multi_space_detector is None:
            try:
                self._multi_space_detector = MultiSpaceQueryDetector()
                logger.info("[ComputerUseTool] Multi-space detector initialized")
            except Exception as e:
                logger.warning(f"[ComputerUseTool] Could not initialize multi-space: {e}")
                return None

        return self._multi_space_detector

    async def _analyze_goal(self, goal: str) -> GoalAnalysis:
        """
        Analyze a goal to determine the best execution strategy.

        Args:
            goal: Natural language goal

        Returns:
            GoalAnalysis with recommended approach
        """
        goal_lower = goal.lower()

        # Check for multi-space indicators
        multi_space_detector = self._ensure_multi_space_detector()
        space_intent = None
        requires_multi_space = False

        if multi_space_detector:
            space_intent = multi_space_detector.detect_intent(goal)
            requires_multi_space = space_intent.query_type in [
                SpaceQueryType.LOCATION_QUERY,
                SpaceQueryType.SPACE_CONTENT,
                SpaceQueryType.ALL_SPACES,
            ]

        # Detect target elements
        target_elements = []
        element_keywords = [
            "button", "menu", "icon", "window", "app", "application",
            "control center", "settings", "preferences", "browser",
            "terminal", "finder", "dock", "notification"
        ]
        for keyword in element_keywords:
            if keyword in goal_lower:
                target_elements.append(keyword)

        # Estimate complexity
        word_count = len(goal.split())
        step_indicators = ["then", "after", "next", "finally", "also", "and"]
        step_count = sum(1 for ind in step_indicators if ind in goal_lower)

        if word_count > 30 or step_count > 2:
            complexity = "complex"
        elif word_count > 15 or step_count > 0:
            complexity = "moderate"
        else:
            complexity = "simple"

        # Determine approach
        if requires_multi_space:
            approach = "multi_space_aware"
        elif len(target_elements) > 1:
            approach = "sequential_actions"
        else:
            approach = "direct_execution"

        return GoalAnalysis(
            original_goal=goal,
            requires_vision=True,  # Computer use always requires vision
            requires_multi_space=requires_multi_space,
            target_elements=target_elements,
            estimated_complexity=complexity,
            suggested_approach=approach,
            space_query_intent=space_intent,
        )

    async def _get_uae_context(
        self,
        goal: str,
        target_elements: List[str]
    ) -> Optional[Dict[str, Any]]:
        """
        Get context from UAE for intelligent positioning.

        Args:
            goal: The goal being executed
            target_elements: Elements that might be targeted

        Returns:
            UAE context dictionary or None
        """
        uae = await self._ensure_uae()
        if not uae:
            return None

        try:
            uae_context = {
                "decisions": {},
                "metrics": uae.get_comprehensive_metrics(),
            }

            # Get positions for known elements
            for element in target_elements:
                element_id = element.replace(" ", "_").lower()
                try:
                    decision = await uae.get_element_position(element_id)
                    if decision and decision.confidence > 0.5:
                        uae_context["decisions"][element_id] = {
                            "position": decision.chosen_position,
                            "confidence": decision.confidence,
                            "source": decision.decision_source.value,
                            "reasoning": decision.reasoning,
                        }
                except Exception as e:
                    logger.debug(f"Could not get UAE position for {element}: {e}")

            return uae_context if uae_context["decisions"] else None

        except Exception as e:
            logger.warning(f"[ComputerUseTool] UAE context error: {e}")
            return None

    async def _execute(
        self,
        goal: str,
        context: Optional[Dict[str, Any]] = None,
        narrate: bool = True,
        use_uae: bool = True,
        force_multi_space: bool = False,
        **kwargs
    ) -> ComputerUseResult:
        """
        Execute a computer control task.

        Args:
            goal: Natural language description of what to accomplish
            context: Additional context for the task
            narrate: Whether to speak actions out loud
            use_uae: Whether to use UAE for intelligent positioning
            force_multi_space: Force multi-space awareness
            **kwargs: Additional arguments

        Returns:
            ComputerUseResult with execution details
        """
        self._total_goals += 1
        start_time = time.time()
        task_id = str(uuid4())

        logger.info(f"[ComputerUseTool] Executing goal: {goal}")

        try:
            # Ensure connector is ready
            connector = await self._ensure_connector()

            # Analyze the goal
            analysis = await self._analyze_goal(goal)
            logger.info(
                f"[ComputerUseTool] Goal analysis: "
                f"complexity={analysis.estimated_complexity}, "
                f"approach={analysis.suggested_approach}"
            )

            # Build enhanced context
            full_context = context.copy() if context else {}
            full_context["goal_analysis"] = {
                "complexity": analysis.estimated_complexity,
                "approach": analysis.suggested_approach,
                "target_elements": analysis.target_elements,
                "requires_multi_space": analysis.requires_multi_space,
            }

            # Get UAE context if enabled
            uae_context = None
            if use_uae and analysis.target_elements:
                uae_context = await self._get_uae_context(goal, analysis.target_elements)
                if uae_context:
                    full_context["uae_hints"] = uae_context["decisions"]
                    logger.info(
                        f"[ComputerUseTool] UAE provided hints for "
                        f"{len(uae_context['decisions'])} elements"
                    )

            # Handle multi-space requirements
            multi_space_context = None
            if analysis.requires_multi_space or force_multi_space:
                if analysis.space_query_intent:
                    multi_space_context = {
                        "query_type": analysis.space_query_intent.query_type.value,
                        "target_space": analysis.space_query_intent.target_space,
                        "target_app": analysis.space_query_intent.target_app,
                    }
                    full_context["multi_space"] = multi_space_context

            # Determine narration setting
            should_narrate = narrate and self.config.computer_use.enable_narration

            # Execute via connector
            result = await connector.execute_task(
                goal=goal,
                context=full_context,
                narrate=should_narrate,
            )

            # Process result
            success = result.status == TaskStatus.SUCCESS
            if success:
                self._successful_goals += 1
            else:
                self._failed_goals += 1

            total_duration = (time.time() - start_time) * 1000

            return ComputerUseResult(
                success=success,
                task_id=result.task_id,
                goal=goal,
                status=result.status.value,
                final_message=result.final_message,
                actions_count=len(result.actions_executed),
                total_duration_ms=total_duration,
                learning_insights=result.learning_insights,
                confidence=result.confidence,
                uae_decision=uae_context,
                multi_space_context=multi_space_context,
                metadata={
                    "analysis": {
                        "complexity": analysis.estimated_complexity,
                        "approach": analysis.suggested_approach,
                    },
                    "narration_log": result.narration_log,
                },
            )

        except Exception as e:
            self._failed_goals += 1
            logger.error(f"[ComputerUseTool] Execution failed: {e}", exc_info=True)

            return ComputerUseResult(
                success=False,
                task_id=task_id,
                goal=goal,
                status="error",
                final_message=f"Execution failed: {str(e)}",
                actions_count=0,
                total_duration_ms=(time.time() - start_time) * 1000,
                learning_insights=[],
                confidence=0.0,
                error=str(e),
            )

    def get_metrics(self) -> Dict[str, Any]:
        """Get tool metrics."""
        base_metrics = self.get_execution_stats()
        return {
            **base_metrics,
            "total_goals": self._total_goals,
            "successful_goals": self._successful_goals,
            "failed_goals": self._failed_goals,
            "success_rate": (
                self._successful_goals / self._total_goals
                if self._total_goals > 0 else 0.0
            ),
            "computer_use_available": COMPUTER_USE_AVAILABLE,
            "uae_available": UAE_AVAILABLE,
            "multi_space_available": MULTI_SPACE_AVAILABLE,
        }


# ============================================================================
# Neural Mesh Agent Wrapper (Optional)
# ============================================================================

if NEURAL_MESH_AVAILABLE:
    class ComputerUseAgent(BaseNeuralMeshAgent):
        """
        Neural Mesh agent wrapper for Computer Use capabilities.

        This allows Computer Use to participate in the multi-agent system.
        """

        def __init__(
            self,
            tool: Optional[ComputerUseTool] = None,
            **kwargs
        ):
            super().__init__(
                agent_name="computer_use_agent",
                agent_type="autonomy",
                capabilities={
                    "ui_automation",
                    "vision",
                    "computer_use",
                    "screen_interaction",
                },
                **kwargs
            )
            self.tool = tool or ComputerUseTool()

        async def process_task(self, task: Dict[str, Any]) -> Dict[str, Any]:
            """Process a task from the Neural Mesh."""
            goal = task.get("goal") or task.get("message", "")
            context = task.get("context", {})

            result = await self.tool.run(goal=goal, context=context)

            return {
                "success": result.success,
                "result": result.final_message,
                "actions_count": result.actions_count,
                "confidence": result.confidence,
            }


# ============================================================================
# Factory Functions
# ============================================================================

_tool_instance: Optional[ComputerUseTool] = None


def get_computer_use_tool(
    tts_callback: Optional[Callable[[str], Awaitable[None]]] = None,
    config: Optional[AgenticConfig] = None,
) -> ComputerUseTool:
    """
    Get or create the Computer Use Tool singleton.

    Args:
        tts_callback: Optional TTS callback for voice narration
        config: Optional configuration override

    Returns:
        ComputerUseTool instance
    """
    global _tool_instance

    if _tool_instance is None:
        _tool_instance = ComputerUseTool(
            tts_callback=tts_callback,
            config=config,
        )

    return _tool_instance


async def execute_computer_task(
    goal: str,
    context: Optional[Dict[str, Any]] = None,
    narrate: bool = True,
) -> ComputerUseResult:
    """
    Convenience function to execute a computer use task.

    Args:
        goal: Natural language goal
        context: Additional context
        narrate: Whether to narrate actions

    Returns:
        ComputerUseResult
    """
    tool = get_computer_use_tool()
    return await tool.run(goal=goal, context=context, narrate=narrate)
