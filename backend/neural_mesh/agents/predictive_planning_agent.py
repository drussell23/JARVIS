"""
JARVIS Neural Mesh - Predictive Planning Agent
===============================================

The "Psychic Brain" of Proactive Parallelism.

This agent transforms vague user intentions into concrete, executable tasks
that can be run in parallel. It combines:
- Temporal awareness (time of day, day of week)
- Spatial awareness (current Space, active apps)
- Memory (recent activities, patterns)
- Contextual reasoning (LLM-powered expansion)

Example:
    User: "Start my day"

    Predictive Agent expands to:
    1. "Open VS Code to workspace"
    2. "Check email for urgent messages"
    3. "Check calendar for today's meetings"
    4. "Open Slack for team messages"
    5. "Open Jira for sprint tasks"

These 5 tasks are then executed in parallel via the AgenticTaskRunner.

Architecture:
    ┌─────────────────────────────────────────────────────────────────────┐
    │                    Proactive Parallelism                            │
    │                                                                     │
    │  User: "Work mode"                                                  │
    │         ↓                                                           │
    │  ┌─────────────────────────────────────────────────────────────┐   │
    │  │         PredictivePlanningAgent ("Psychic Brain")           │   │
    │  │                                                              │   │
    │  │  Input: "Work mode"                                          │   │
    │  │  Context: 9:15 AM, Monday, Space 1, recent: Calendar         │   │
    │  │  Memory: Usually opens VS Code, Slack, Email in morning      │   │
    │  │                                                              │   │
    │  │  → LLM Expansion →                                           │   │
    │  │                                                              │   │
    │  │  Output: ["Open VS Code", "Check Email", "Open Slack",       │   │
    │  │          "Check Calendar", "Open Jira"]                      │   │
    │  └──────────────────────────┬───────────────────────────────────┘   │
    │                             ↓                                       │
    │  ┌─────────────────────────────────────────────────────────────┐   │
    │  │         AgenticTaskRunner ("Parallel Muscle")                │   │
    │  │                                                              │   │
    │  │  Task 1 → Space 2 (Code)  ─┐                                │   │
    │  │  Task 2 → Space 3 (Email) ─┼─→ SpaceLock (serialized)       │   │
    │  │  Task 3 → Space 4 (Slack) ─┤                                │   │
    │  │  Task 4 → Space 1 (Cal)   ─┤                                │   │
    │  │  Task 5 → Space 5 (Jira)  ─┘                                │   │
    │  │                                                              │   │
    │  │  Result: All apps ready in 5-10 seconds                      │   │
    │  └─────────────────────────────────────────────────────────────┘   │
    │                                                                     │
    └─────────────────────────────────────────────────────────────────────┘

Author: JARVIS AI System
Version: 1.0.0 - Proactive Parallelism
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from ..base.base_neural_mesh_agent import BaseNeuralMeshAgent
from ..data_models import (
    AgentMessage,
    KnowledgeType,
    MessageType,
    MessagePriority,
)

logger = logging.getLogger(__name__)


# =============================================================================
# Intent Categories
# =============================================================================

class IntentCategory(str, Enum):
    """Categories of user intent for task expansion."""
    WORK_MODE = "work_mode"  # Start working, get ready for work
    MEETING_PREP = "meeting_prep"  # Prepare for a meeting
    COMMUNICATION = "communication"  # Check messages, emails
    RESEARCH = "research"  # Research a topic
    DEVELOPMENT = "development"  # Code, debug, develop
    BREAK_TIME = "break_time"  # Take a break, relax
    END_OF_DAY = "end_of_day"  # Wrap up, prepare to leave
    CREATIVE = "creative"  # Design, write, create content
    ADMIN = "admin"  # Administrative tasks
    CUSTOM = "custom"  # Custom/unknown intent


# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class TemporalContext:
    """Time-based context for prediction."""
    current_time: datetime
    hour: int
    minute: int
    day_of_week: int  # 0=Monday, 6=Sunday
    day_name: str
    is_morning: bool  # 6-12
    is_afternoon: bool  # 12-17
    is_evening: bool  # 17-21
    is_night: bool  # 21-6
    is_weekend: bool
    is_workday: bool

    @classmethod
    def from_datetime(cls, dt: Optional[datetime] = None) -> "TemporalContext":
        """Create from datetime (defaults to now)."""
        dt = dt or datetime.now()
        hour = dt.hour
        dow = dt.weekday()

        return cls(
            current_time=dt,
            hour=hour,
            minute=dt.minute,
            day_of_week=dow,
            day_name=["Monday", "Tuesday", "Wednesday", "Thursday",
                      "Friday", "Saturday", "Sunday"][dow],
            is_morning=6 <= hour < 12,
            is_afternoon=12 <= hour < 17,
            is_evening=17 <= hour < 21,
            is_night=hour >= 21 or hour < 6,
            is_weekend=dow >= 5,
            is_workday=dow < 5,
        )

    def to_prompt_context(self) -> str:
        """Convert to LLM prompt context."""
        time_of_day = (
            "morning" if self.is_morning else
            "afternoon" if self.is_afternoon else
            "evening" if self.is_evening else "night"
        )
        return (
            f"Time: {self.current_time.strftime('%I:%M %p')} on {self.day_name} "
            f"({time_of_day}, {'weekend' if self.is_weekend else 'workday'})"
        )


@dataclass
class SpatialContext:
    """Space-based context for prediction."""
    current_space_id: int
    total_spaces: int
    focused_app: str
    app_locations: Dict[str, List[int]]  # app -> spaces
    recently_used_apps: List[str]

    def to_prompt_context(self) -> str:
        """Convert to LLM prompt context."""
        return (
            f"Current Space: {self.current_space_id} of {self.total_spaces}, "
            f"Active App: {self.focused_app}, "
            f"Recently Used: {', '.join(self.recently_used_apps[:5])}"
        )


@dataclass
class MemoryContext:
    """Memory-based context for prediction."""
    recent_tasks: List[str]
    common_patterns: Dict[str, List[str]]  # e.g., {"morning": ["email", "calendar"]}
    user_preferences: Dict[str, Any]

    def to_prompt_context(self) -> str:
        """Convert to LLM prompt context."""
        patterns_str = ""
        if self.common_patterns:
            patterns_str = f", Patterns: {json.dumps(self.common_patterns)}"
        return f"Recent Tasks: {', '.join(self.recent_tasks[:5])}{patterns_str}"


@dataclass
class PredictionContext:
    """Full context for prediction."""
    temporal: TemporalContext
    spatial: Optional[SpatialContext]
    memory: Optional[MemoryContext]
    raw_query: str

    def to_full_prompt_context(self) -> str:
        """Convert to full LLM prompt context."""
        parts = [self.temporal.to_prompt_context()]
        if self.spatial:
            parts.append(self.spatial.to_prompt_context())
        if self.memory:
            parts.append(self.memory.to_prompt_context())
        return " | ".join(parts)


@dataclass
class ExpandedTask:
    """A single expanded task from prediction."""
    goal: str
    priority: int  # 1=highest
    target_app: Optional[str]
    estimated_duration_seconds: int
    dependencies: List[str]  # Goals this depends on
    category: IntentCategory

    def to_dict(self) -> Dict[str, Any]:
        return {
            "goal": self.goal,
            "priority": self.priority,
            "target_app": self.target_app,
            "estimated_duration_seconds": self.estimated_duration_seconds,
            "dependencies": self.dependencies,
            "category": self.category.value,
        }


@dataclass
class PredictionResult:
    """Result of predictive planning."""
    original_query: str
    detected_intent: IntentCategory
    confidence: float
    expanded_tasks: List[ExpandedTask]
    reasoning: str
    context_used: str
    timestamp: datetime = field(default_factory=datetime.now)

    @property
    def goals(self) -> List[str]:
        """Get just the goal strings for parallel execution."""
        return [task.goal for task in self.expanded_tasks]

    @property
    def parallel_goals(self) -> List[str]:
        """Get goals that can be executed in parallel (no dependencies)."""
        return [
            task.goal for task in self.expanded_tasks
            if not task.dependencies
        ]


@dataclass
class PredictivePlanningConfig:
    """Configuration for the Predictive Planning Agent."""
    # LLM settings
    model: str = "claude-sonnet-4-20250514"
    max_tokens: int = 1024
    temperature: float = 0.3  # Lower = more deterministic

    # Expansion settings
    max_expanded_tasks: int = 7
    min_confidence_threshold: float = 0.6

    # Context settings
    use_spatial_context: bool = True
    use_memory_context: bool = True

    # Narration
    narrate_predictions: bool = True


# =============================================================================
# Intent Patterns (Heuristic Pre-Processing)
# =============================================================================

INTENT_PATTERNS: Dict[IntentCategory, List[str]] = {
    IntentCategory.WORK_MODE: [
        "start my day", "work mode", "get ready for work", "let's work",
        "morning routine", "begin work", "start working", "productivity mode",
        "focus mode", "deep work", "get to work",
    ],
    IntentCategory.MEETING_PREP: [
        "prepare for meeting", "meeting prep", "get ready for call",
        "standup", "sprint planning", "retro", "one on one", "1:1",
    ],
    IntentCategory.COMMUNICATION: [
        "check messages", "check email", "check slack", "any messages",
        "what's new", "notifications", "inbox", "catch up",
    ],
    IntentCategory.RESEARCH: [
        "research", "look up", "find out", "learn about", "investigate",
        "explore", "study", "understand",
    ],
    IntentCategory.DEVELOPMENT: [
        "code", "debug", "develop", "implement", "fix bug", "write code",
        "programming", "dev mode", "coding session",
    ],
    IntentCategory.BREAK_TIME: [
        "take a break", "relax", "chill", "rest", "pause", "decompress",
        "coffee break", "lunch", "step away",
    ],
    IntentCategory.END_OF_DAY: [
        "end of day", "wrap up", "call it a day", "going home", "sign off",
        "log off", "done for today", "finish up",
    ],
    IntentCategory.CREATIVE: [
        "design", "create", "write", "draft", "compose", "brainstorm",
        "sketch", "prototype", "mockup",
    ],
    IntentCategory.ADMIN: [
        "admin", "expense", "timesheet", "report", "review", "approve",
        "schedule", "organize", "clean up",
    ],
}


# =============================================================================
# Default Task Expansions (Fallback without LLM)
# =============================================================================

DEFAULT_EXPANSIONS: Dict[IntentCategory, List[Dict[str, Any]]] = {
    IntentCategory.WORK_MODE: [
        {"goal": "Open VS Code to the main project", "priority": 1, "target_app": "Visual Studio Code"},
        {"goal": "Check email for urgent messages", "priority": 2, "target_app": "Mail"},
        {"goal": "Check calendar for today's meetings", "priority": 2, "target_app": "Calendar"},
        {"goal": "Open Slack for team updates", "priority": 3, "target_app": "Slack"},
    ],
    IntentCategory.MEETING_PREP: [
        {"goal": "Open calendar and find the meeting", "priority": 1, "target_app": "Calendar"},
        {"goal": "Open meeting notes document", "priority": 2, "target_app": "Notes"},
        {"goal": "Check Slack for meeting context", "priority": 3, "target_app": "Slack"},
    ],
    IntentCategory.COMMUNICATION: [
        {"goal": "Check email inbox for unread messages", "priority": 1, "target_app": "Mail"},
        {"goal": "Check Slack for team messages", "priority": 1, "target_app": "Slack"},
        {"goal": "Review calendar for upcoming events", "priority": 2, "target_app": "Calendar"},
    ],
    IntentCategory.DEVELOPMENT: [
        {"goal": "Open VS Code to the project", "priority": 1, "target_app": "Visual Studio Code"},
        {"goal": "Open terminal for commands", "priority": 2, "target_app": "Terminal"},
        {"goal": "Open browser for documentation", "priority": 3, "target_app": "Safari"},
    ],
    IntentCategory.END_OF_DAY: [
        {"goal": "Check email for anything urgent before leaving", "priority": 1, "target_app": "Mail"},
        {"goal": "Update Jira with today's progress", "priority": 2, "target_app": "Safari"},
        {"goal": "Send end-of-day update to team on Slack", "priority": 3, "target_app": "Slack"},
    ],
}


# =============================================================================
# Predictive Planning Agent
# =============================================================================

class PredictivePlanningAgent(BaseNeuralMeshAgent):
    """
    The "Psychic Brain" - expands vague intentions into concrete parallel tasks.

    This agent is the first stage of Proactive Parallelism:
    1. User gives vague command ("Start my day")
    2. This agent expands it into concrete tasks
    3. Tasks are sent to AgenticTaskRunner for parallel execution

    Capabilities:
    - expand_intent: Turn vague command into task list
    - detect_intent: Classify user intent
    - get_context: Gather temporal, spatial, memory context
    """

    def __init__(self, config: Optional[PredictivePlanningConfig] = None) -> None:
        """Initialize the Predictive Planning Agent."""
        super().__init__(
            agent_name="predictive_planning_agent",
            agent_type="intelligence",
            capabilities={
                "expand_intent",
                "detect_intent",
                "predict_tasks",
                "proactive_planning",
                "psychic_brain",
            },
            version="1.0.0",
        )

        self.config = config or PredictivePlanningConfig()
        self._claude_client = None
        self._spatial_awareness = None
        self._memory_system = None
        self._initialized = False

        # Statistics
        self._predictions_made = 0
        self._tasks_expanded = 0
        self._llm_expansions = 0
        self._fallback_expansions = 0

        # Cache
        self._recent_predictions: List[PredictionResult] = []
        self._max_cache_size = 20

    async def on_initialize(self) -> None:
        """Initialize agent resources."""
        logger.info("Initializing PredictivePlanningAgent v1.0.0 (Psychic Brain)")

        # Initialize Claude client
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if api_key:
            try:
                import anthropic
                self._claude_client = anthropic.AsyncAnthropic(api_key=api_key)
                logger.info("Claude client initialized for LLM expansion")
            except ImportError:
                logger.warning("anthropic package not available")
        else:
            logger.warning("ANTHROPIC_API_KEY not set - using fallback expansions")

        # Initialize spatial awareness connection
        if self.config.use_spatial_context:
            try:
                from .spatial_awareness_agent import SpatialAwarenessAgent
                self._spatial_awareness = SpatialAwarenessAgent()
                await self._spatial_awareness.on_initialize()
                logger.info("Spatial awareness connected")
            except Exception as e:
                logger.debug(f"Spatial awareness not available: {e}")

        self._initialized = True
        logger.info("PredictivePlanningAgent initialized")

    async def on_start(self) -> None:
        """Called when agent starts."""
        logger.info("PredictivePlanningAgent started - ready to expand intents")

    async def on_stop(self) -> None:
        """Cleanup when agent stops."""
        logger.info(
            f"PredictivePlanningAgent stopping - "
            f"Predictions: {self._predictions_made}, "
            f"Tasks expanded: {self._tasks_expanded}, "
            f"LLM: {self._llm_expansions}, Fallback: {self._fallback_expansions}"
        )

    async def execute_task(self, payload: Dict[str, Any]) -> Any:
        """Execute a predictive planning task."""
        action = payload.get("action", "expand_intent")

        if action == "expand_intent":
            query = payload.get("query", "")
            return await self.expand_intent(query)
        elif action == "detect_intent":
            query = payload.get("query", "")
            return await self.detect_intent(query)
        elif action == "get_context":
            return await self.get_prediction_context(payload.get("query", ""))
        else:
            raise ValueError(f"Unknown action: {action}")

    # =========================================================================
    # Core Methods
    # =========================================================================

    async def expand_intent(self, query: str) -> PredictionResult:
        """
        Expand a vague user intent into concrete executable tasks.

        This is the main method - the "Psychic Brain" that turns
        "Start my day" into 5 parallel tasks.

        Args:
            query: User's vague command (e.g., "Work mode")

        Returns:
            PredictionResult with expanded tasks
        """
        self._predictions_made += 1

        # 1. Detect intent category
        intent, confidence = await self.detect_intent(query)
        logger.info(f"Detected intent: {intent.value} (confidence: {confidence:.2f})")

        # 2. Gather context
        context = await self.get_prediction_context(query)

        # 3. Expand tasks
        if self._claude_client and confidence < 0.9:
            # Use LLM for intelligent expansion
            try:
                result = await self._expand_with_llm(query, intent, context)
                self._llm_expansions += 1
            except Exception as e:
                logger.warning(f"LLM expansion failed: {e}, using fallback")
                result = self._expand_with_fallback(query, intent, context)
                self._fallback_expansions += 1
        else:
            # Use fallback expansion
            result = self._expand_with_fallback(query, intent, context)
            self._fallback_expansions += 1

        # Track statistics
        self._tasks_expanded += len(result.expanded_tasks)

        # Cache result
        self._recent_predictions.append(result)
        if len(self._recent_predictions) > self._max_cache_size:
            self._recent_predictions.pop(0)

        # Store in knowledge graph
        if self.knowledge_graph:
            await self.add_knowledge(
                knowledge_type=KnowledgeType.LEARNING,
                data={
                    "type": "intent_expansion",
                    "query": query,
                    "intent": intent.value,
                    "tasks_count": len(result.expanded_tasks),
                    "goals": result.goals,
                },
                confidence=confidence,
            )

        # Narrate if enabled
        if self.config.narrate_predictions:
            await self._narrate_prediction(result)

        return result

    async def detect_intent(self, query: str) -> Tuple[IntentCategory, float]:
        """
        Detect the intent category from user query.

        Uses heuristic pattern matching first, falls back to LLM.

        Args:
            query: User query

        Returns:
            (IntentCategory, confidence)
        """
        query_lower = query.lower().strip()

        # Check heuristic patterns
        for intent, patterns in INTENT_PATTERNS.items():
            for pattern in patterns:
                if pattern in query_lower:
                    return intent, 0.95

        # No heuristic match - use LLM if available
        if self._claude_client:
            try:
                return await self._detect_intent_with_llm(query)
            except Exception as e:
                logger.debug(f"LLM intent detection failed: {e}")

        # Default to custom
        return IntentCategory.CUSTOM, 0.5

    async def get_prediction_context(self, query: str) -> PredictionContext:
        """Gather full context for prediction."""
        # Temporal context (always available)
        temporal = TemporalContext.from_datetime()

        # Spatial context (if available)
        spatial = None
        if self._spatial_awareness and self.config.use_spatial_context:
            try:
                spatial_data = await self._spatial_awareness.execute_task(
                    {"action": "get_spatial_context"}
                )
                if spatial_data and not spatial_data.get("error"):
                    spatial = SpatialContext(
                        current_space_id=spatial_data.get("current_space_id", 1),
                        total_spaces=spatial_data.get("total_spaces", 1),
                        focused_app=spatial_data.get("focused_app", ""),
                        app_locations=spatial_data.get("app_locations", {}),
                        recently_used_apps=list(spatial_data.get("app_locations", {}).keys())[:5],
                    )
            except Exception as e:
                logger.debug(f"Failed to get spatial context: {e}")

        # Memory context (if available)
        memory = None
        if self.config.use_memory_context:
            # Use recent predictions as memory
            recent_goals = []
            for pred in self._recent_predictions[-5:]:
                recent_goals.extend(pred.goals[:2])

            memory = MemoryContext(
                recent_tasks=recent_goals,
                common_patterns=self._get_time_based_patterns(temporal),
                user_preferences={},
            )

        return PredictionContext(
            temporal=temporal,
            spatial=spatial,
            memory=memory,
            raw_query=query,
        )

    # =========================================================================
    # LLM-Powered Expansion
    # =========================================================================

    async def _expand_with_llm(
        self,
        query: str,
        intent: IntentCategory,
        context: PredictionContext,
    ) -> PredictionResult:
        """Expand intent using Claude LLM."""
        system_prompt = """You are JARVIS's Predictive Planning Engine.

Your job is to expand vague user commands into concrete, executable tasks.

Rules:
1. Output 3-7 specific, actionable tasks
2. Each task should be executable by a Computer Use agent
3. Consider the time of day and context
4. Prioritize tasks (1=most important)
5. Identify target apps when possible
6. Tasks should be independent (can run in parallel)

Output Format (JSON):
{
    "reasoning": "Brief explanation of expansion logic",
    "tasks": [
        {
            "goal": "Specific action to take",
            "priority": 1,
            "target_app": "App name or null",
            "estimated_duration_seconds": 30
        }
    ]
}"""

        user_prompt = f"""Expand this command into concrete tasks:

User Command: "{query}"
Detected Intent: {intent.value}
Context: {context.to_full_prompt_context()}

Generate a JSON list of 3-7 executable tasks."""

        try:
            response = await self._claude_client.messages.create(
                model=self.config.model,
                max_tokens=self.config.max_tokens,
                temperature=self.config.temperature,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )

            # Parse response
            content = response.content[0].text

            # Extract JSON from response
            import re
            json_match = re.search(r'\{[\s\S]*\}', content)
            if json_match:
                data = json.loads(json_match.group())

                tasks = []
                for i, task_data in enumerate(data.get("tasks", [])):
                    tasks.append(ExpandedTask(
                        goal=task_data.get("goal", ""),
                        priority=task_data.get("priority", i + 1),
                        target_app=task_data.get("target_app"),
                        estimated_duration_seconds=task_data.get("estimated_duration_seconds", 30),
                        dependencies=[],
                        category=intent,
                    ))

                return PredictionResult(
                    original_query=query,
                    detected_intent=intent,
                    confidence=0.85,
                    expanded_tasks=tasks[:self.config.max_expanded_tasks],
                    reasoning=data.get("reasoning", "LLM expansion"),
                    context_used=context.to_full_prompt_context(),
                )

        except Exception as e:
            logger.warning(f"LLM expansion parsing failed: {e}")
            raise

        # Fallback if parsing fails
        return self._expand_with_fallback(query, intent, context)

    async def _detect_intent_with_llm(self, query: str) -> Tuple[IntentCategory, float]:
        """Detect intent using LLM."""
        categories = [c.value for c in IntentCategory]

        response = await self._claude_client.messages.create(
            model=self.config.model,
            max_tokens=100,
            temperature=0.0,
            messages=[{
                "role": "user",
                "content": f"Classify this intent into one category: {query}\n\nCategories: {categories}\n\nRespond with just the category name."
            }],
        )

        text = response.content[0].text.strip().lower()

        for category in IntentCategory:
            if category.value in text:
                return category, 0.8

        return IntentCategory.CUSTOM, 0.5

    # =========================================================================
    # Fallback Expansion
    # =========================================================================

    def _expand_with_fallback(
        self,
        query: str,
        intent: IntentCategory,
        context: PredictionContext,
    ) -> PredictionResult:
        """Expand intent using default patterns (no LLM)."""
        default_tasks = DEFAULT_EXPANSIONS.get(intent, [])

        if not default_tasks:
            # Generic fallback
            default_tasks = [
                {"goal": query, "priority": 1, "target_app": None}
            ]

        tasks = []
        for i, task_data in enumerate(default_tasks):
            tasks.append(ExpandedTask(
                goal=task_data.get("goal", ""),
                priority=task_data.get("priority", i + 1),
                target_app=task_data.get("target_app"),
                estimated_duration_seconds=task_data.get("estimated_duration_seconds", 30),
                dependencies=[],
                category=intent,
            ))

        return PredictionResult(
            original_query=query,
            detected_intent=intent,
            confidence=0.7,
            expanded_tasks=tasks,
            reasoning=f"Default expansion for {intent.value}",
            context_used=context.to_full_prompt_context(),
        )

    # =========================================================================
    # Helper Methods
    # =========================================================================

    def _get_time_based_patterns(self, temporal: TemporalContext) -> Dict[str, List[str]]:
        """Get common patterns based on time."""
        patterns = {}

        if temporal.is_morning and temporal.is_workday:
            patterns["morning_workday"] = ["email", "calendar", "slack", "code"]
        elif temporal.is_afternoon and temporal.is_workday:
            patterns["afternoon_workday"] = ["meetings", "code", "review"]
        elif temporal.is_evening:
            patterns["evening"] = ["wrap_up", "planning"]

        return patterns

    async def _narrate_prediction(self, result: PredictionResult) -> None:
        """Narrate the prediction using TTS."""
        try:
            from api.async_tts_handler import speak_async

            task_count = len(result.expanded_tasks)
            message = f"Expanding '{result.original_query}' into {task_count} parallel tasks."

            await speak_async(message)
        except Exception as e:
            logger.debug(f"Narration failed: {e}")

    def get_stats(self) -> Dict[str, Any]:
        """Get agent statistics."""
        return {
            "predictions_made": self._predictions_made,
            "tasks_expanded": self._tasks_expanded,
            "llm_expansions": self._llm_expansions,
            "fallback_expansions": self._fallback_expansions,
            "cached_predictions": len(self._recent_predictions),
            "llm_available": self._claude_client is not None,
            "spatial_available": self._spatial_awareness is not None,
        }


# =============================================================================
# Factory Function
# =============================================================================

async def create_predictive_planning_agent(
    config: Optional[PredictivePlanningConfig] = None,
) -> PredictivePlanningAgent:
    """Create a Predictive Planning Agent."""
    agent = PredictivePlanningAgent(config=config)
    await agent.on_initialize()
    return agent


# =============================================================================
# Convenience Functions
# =============================================================================

_predictive_agent: Optional[PredictivePlanningAgent] = None


async def get_predictive_agent() -> PredictivePlanningAgent:
    """Get or create the singleton predictive agent."""
    global _predictive_agent
    if _predictive_agent is None:
        _predictive_agent = await create_predictive_planning_agent()
    return _predictive_agent


async def expand_user_intent(query: str) -> PredictionResult:
    """Convenience function to expand a user intent."""
    agent = await get_predictive_agent()
    return await agent.expand_intent(query)
