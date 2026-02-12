"""
JARVIS Neural Mesh - Goal Inference Agent (v2.7)

Advanced ML-powered intent understanding and goal inference system.
Analyzes user commands, context, and behavioral patterns to infer
higher-level goals and anticipate user needs.

This agent was documented but DORMANT (part of the 27% inactive agents).
v2.7: Now fully implemented and activated in the Neural Mesh.

Key Capabilities:
1. Intent Classification - Multi-level intent understanding
2. Goal Extraction - Infer high-level goals from commands
3. Context Integration - Use contextual signals for better inference
4. Predictive Goals - Anticipate likely next goals
5. Goal Chaining - Link related goals into workflows
6. Confidence Scoring - Provide confidence estimates for inferences
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple

from ..base.base_neural_mesh_agent import BaseNeuralMeshAgent
from ..data_models import AgentMessage, KnowledgeType, MessagePriority, MessageType

logger = logging.getLogger(__name__)


class GoalCategory(Enum):
    """Categories of inferred goals."""
    PRODUCTIVITY = "productivity"  # Work-related tasks
    COMMUNICATION = "communication"  # Messaging, calls
    INFORMATION = "information"  # Search, lookup
    ENTERTAINMENT = "entertainment"  # Media, games
    SYSTEM = "system"  # OS operations
    AUTOMATION = "automation"  # Workflow triggers
    LEARNING = "learning"  # Education, skill building
    CREATIVE = "creative"  # Design, content creation
    ADMINISTRATIVE = "administrative"  # Scheduling, organization


class IntentLevel(Enum):
    """Levels of intent understanding."""
    IMMEDIATE = "immediate"  # Direct action needed now
    SHORT_TERM = "short_term"  # Within session
    LONG_TERM = "long_term"  # Across sessions
    HABITUAL = "habitual"  # Regular patterns


@dataclass
class InferredGoal:
    """Represents an inferred user goal."""
    goal_id: str
    category: GoalCategory
    intent_level: IntentLevel
    description: str
    confidence: float
    inferred_at: datetime = field(default_factory=datetime.now)
    source_commands: List[str] = field(default_factory=list)
    context_signals: Dict[str, Any] = field(default_factory=dict)
    sub_goals: List[str] = field(default_factory=list)
    related_goals: List[str] = field(default_factory=list)


@dataclass
class IntentSignal:
    """A signal contributing to intent inference."""
    signal_type: str
    value: Any
    weight: float
    timestamp: datetime = field(default_factory=datetime.now)


class GoalInferenceAgent(BaseNeuralMeshAgent):
    """
    Goal Inference Agent - ML-powered intent understanding.

    v2.7: Previously dormant agent now activated for the Neural Mesh.

    Capabilities:
    - infer_goal: Infer high-level goal from command/context
    - classify_intent: Multi-level intent classification
    - predict_next_goal: Anticipate likely next goals
    - chain_goals: Link related goals into workflows
    - get_goal_history: Retrieve goal inference history
    - get_confidence: Get confidence scores for inferences
    """

    def __init__(self) -> None:
        super().__init__(
            agent_name="goal_inference_agent",
            agent_type="intelligence",
            capabilities={
                "infer_goal",
                "classify_intent",
                "predict_next_goal",
                "chain_goals",
                "get_goal_history",
                "get_confidence",
                "extract_entities",
                "get_inference_stats",
            },
            version="2.7.0",
        )

        # Goal tracking
        self._goals: Dict[str, InferredGoal] = {}
        self._goal_history: List[InferredGoal] = []
        self._goal_chains: Dict[str, List[str]] = defaultdict(list)

        # Intent patterns learned from history
        self._intent_patterns: Dict[str, Dict[str, float]] = defaultdict(lambda: defaultdict(float))

        # Context signals for inference
        self._context_signals: List[IntentSignal] = []

        # Category keywords for classification
        self._category_keywords = self._build_category_keywords()

        # Statistics
        self._inference_count = 0
        self._correct_predictions = 0

        # Background task
        self._analysis_task: Optional[asyncio.Task] = None

    def _build_category_keywords(self) -> Dict[GoalCategory, Set[str]]:
        """Build keyword sets for each category."""
        return {
            GoalCategory.PRODUCTIVITY: {
                "work", "document", "spreadsheet", "presentation", "meeting",
                "deadline", "task", "project", "review", "edit", "write", "report",
            },
            GoalCategory.COMMUNICATION: {
                "email", "message", "call", "chat", "send", "reply", "contact",
                "slack", "teams", "discord", "text", "notification",
            },
            GoalCategory.INFORMATION: {
                "search", "find", "lookup", "what", "where", "when", "how",
                "who", "weather", "news", "stock", "price", "check",
            },
            GoalCategory.ENTERTAINMENT: {
                "play", "music", "video", "movie", "game", "youtube", "spotify",
                "netflix", "stream", "watch", "listen", "podcast",
            },
            GoalCategory.SYSTEM: {
                "open", "close", "launch", "quit", "restart", "settings",
                "volume", "brightness", "wifi", "bluetooth", "unlock", "lock",
            },
            GoalCategory.AUTOMATION: {
                "automate", "workflow", "script", "schedule", "trigger",
                "batch", "routine", "macro", "repeat", "every",
            },
            GoalCategory.LEARNING: {
                "learn", "study", "course", "tutorial", "how to", "teach",
                "understand", "explain", "documentation", "guide",
            },
            GoalCategory.CREATIVE: {
                "design", "create", "draw", "photo", "video", "edit",
                "canvas", "figma", "photoshop", "illustrate", "compose",
            },
            GoalCategory.ADMINISTRATIVE: {
                "calendar", "schedule", "appointment", "reminder", "todo",
                "organize", "plan", "budget", "invoice", "receipt",
            },
        }

    async def on_initialize(self, **kwargs) -> None:
        logger.info("Initializing GoalInferenceAgent v2.7 (previously dormant)")

        # Subscribe to command events for inference
        # Note: Using TASK_ASSIGNED (the correct enum value, not TASK_ASSIGNMENT)
        if self.message_bus:
            await self.subscribe(
                MessageType.TASK_ASSIGNED,
                self._handle_task_for_inference,
            )

            await self.subscribe(
                MessageType.CUSTOM,
                self._handle_context_signal,
            )

        # Start background analysis
        self._analysis_task = asyncio.create_task(
            self._periodic_goal_analysis(),
            name="goal_analysis"
        )

        logger.info("GoalInferenceAgent initialized and ACTIVE")

    async def on_start(self) -> None:
        logger.info("GoalInferenceAgent started - now inferring goals")

    async def on_stop(self) -> None:
        if self._analysis_task:
            self._analysis_task.cancel()
            try:
                await self._analysis_task
            except asyncio.CancelledError:
                pass
        logger.info(
            f"GoalInferenceAgent stopping - inferred {len(self._goals)} goals, "
            f"{self._inference_count} total inferences"
        )

    async def execute_task(self, payload: Dict[str, Any]) -> Any:
        action = payload.get("action", "")

        if action == "infer_goal":
            return await self._infer_goal(payload)
        elif action == "classify_intent":
            return await self._classify_intent(payload)
        elif action == "predict_next_goal":
            return await self._predict_next_goal(payload)
        elif action == "chain_goals":
            return self._chain_goals(payload)
        elif action == "get_goal_history":
            return self._get_goal_history(payload)
        elif action == "get_confidence":
            return self._get_confidence(payload)
        elif action == "extract_entities":
            return self._extract_entities(payload)
        elif action == "get_inference_stats":
            return self._get_inference_stats()
        else:
            raise ValueError(f"Unknown goal inference action: {action}")

    async def _infer_goal(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Infer high-level goal from command and context.

        Uses multi-signal analysis:
        1. Command text analysis (keywords, structure)
        2. Context signals (time, location, recent actions)
        3. Historical patterns (user's typical behavior)
        4. Goal chaining (related recent goals)
        """
        command = payload.get("command", "")
        context = payload.get("context", {})

        self._inference_count += 1
        start_time = time.perf_counter()

        # Classify the intent category
        category, category_confidence = self._classify_category(command)

        # Determine intent level
        intent_level = self._determine_intent_level(command, context)

        # Generate goal description
        description = self._generate_goal_description(command, category)

        # Calculate overall confidence
        confidence = self._calculate_confidence(
            command, category_confidence, context
        )

        # Create goal
        goal_id = hashlib.md5(
            f"{command}_{datetime.now().isoformat()}".encode()
        ).hexdigest()[:12]

        goal = InferredGoal(
            goal_id=goal_id,
            category=category,
            intent_level=intent_level,
            description=description,
            confidence=confidence,
            source_commands=[command],
            context_signals=context,
        )

        # Store goal
        self._goals[goal_id] = goal
        self._goal_history.append(goal)

        # Update patterns
        self._update_intent_patterns(command, category)

        # Check for goal chaining
        related = self._find_related_goals(goal)
        if related:
            goal.related_goals = [g.goal_id for g in related]
            for r in related:
                self._goal_chains[r.goal_id].append(goal_id)

        inference_time = (time.perf_counter() - start_time) * 1000

        logger.info(
            f"Inferred goal: {goal.description} "
            f"(category={category.value}, confidence={confidence:.1%}, "
            f"time={inference_time:.1f}ms)"
        )

        # Publish knowledge about inferred goal
        await self.add_knowledge(
            knowledge_type=KnowledgeType.INSIGHT,
            data={
                "type": "goal_inferred",
                "goal_id": goal_id,
                "category": category.value,
                "description": description,
                "confidence": confidence,
            },
        )

        # v238.0: Broadcast inferred goal for cross-agent awareness
        try:
            await self.broadcast(
                message_type=MessageType.ANNOUNCEMENT,
                payload={
                    "type": "goal_inferred",
                    "goal_id": goal_id,
                    "category": category.value,
                    "description": description,
                    "confidence": confidence,
                },
                priority=MessagePriority.NORMAL,
            )
        except Exception:
            pass  # Best-effort broadcast

        return {
            "goal_id": goal_id,
            "category": category.value,
            "intent_level": intent_level.value,
            "description": description,
            "confidence": confidence,
            "related_goals": goal.related_goals,
            "inference_time_ms": inference_time,
        }

    def _classify_category(self, command: str) -> Tuple[GoalCategory, float]:
        """Classify command into goal category."""
        command_lower = command.lower()
        scores: Dict[GoalCategory, float] = {}

        for category, keywords in self._category_keywords.items():
            score = sum(1 for kw in keywords if kw in command_lower)
            # Normalize by keyword count
            scores[category] = score / len(keywords) if keywords else 0

        # Add learned pattern weights
        for category in GoalCategory:
            pattern_weight = self._intent_patterns.get(command_lower, {}).get(
                category.value, 0
            )
            scores[category] = scores.get(category, 0) + pattern_weight * 0.3

        if not scores or max(scores.values()) == 0:
            return GoalCategory.SYSTEM, 0.3

        best_category = max(scores.keys(), key=lambda k: scores[k])
        confidence = min(0.95, scores[best_category] * 2)

        return best_category, confidence

    def _determine_intent_level(
        self, command: str, context: Dict[str, Any]
    ) -> IntentLevel:
        """Determine the level of intent."""
        # Check for immediate action indicators
        immediate_words = {"now", "immediately", "quick", "urgent", "asap"}
        if any(w in command.lower() for w in immediate_words):
            return IntentLevel.IMMEDIATE

        # Check for scheduling indicators
        schedule_words = {"later", "tomorrow", "next", "schedule", "remind"}
        if any(w in command.lower() for w in schedule_words):
            return IntentLevel.SHORT_TERM

        # Check for habit patterns
        if self._is_habitual_command(command):
            return IntentLevel.HABITUAL

        return IntentLevel.IMMEDIATE

    def _is_habitual_command(self, command: str) -> bool:
        """Check if command matches habitual patterns."""
        # Look for similar commands in history
        similar_count = sum(
            1 for goal in self._goal_history[-100:]
            if self._similarity(command, goal.source_commands[0]) > 0.7
        )
        return similar_count >= 3

    def _similarity(self, a: str, b: str) -> float:
        """Calculate simple word overlap similarity."""
        words_a = set(a.lower().split())
        words_b = set(b.lower().split())
        if not words_a or not words_b:
            return 0.0
        intersection = words_a & words_b
        union = words_a | words_b
        return len(intersection) / len(union)

    def _generate_goal_description(
        self, command: str, category: GoalCategory
    ) -> str:
        """Generate human-readable goal description."""
        category_prefixes = {
            GoalCategory.PRODUCTIVITY: "Complete work task",
            GoalCategory.COMMUNICATION: "Send communication",
            GoalCategory.INFORMATION: "Find information about",
            GoalCategory.ENTERTAINMENT: "Enjoy entertainment",
            GoalCategory.SYSTEM: "Perform system operation",
            GoalCategory.AUTOMATION: "Automate process",
            GoalCategory.LEARNING: "Learn about",
            GoalCategory.CREATIVE: "Create content",
            GoalCategory.ADMINISTRATIVE: "Organize",
        }

        prefix = category_prefixes.get(category, "Complete task")

        # Extract key elements from command
        words = command.split()[:6]
        context = " ".join(words)

        return f"{prefix}: {context}"

    def _calculate_confidence(
        self,
        command: str,
        category_confidence: float,
        context: Dict[str, Any],
    ) -> float:
        """Calculate overall inference confidence."""
        confidence = category_confidence

        # Boost for context availability
        if context:
            confidence += 0.1

        # Boost for pattern matches
        if self._is_habitual_command(command):
            confidence += 0.15

        # Boost for specific action words
        action_words = {"open", "close", "send", "find", "play", "create"}
        if any(w in command.lower() for w in action_words):
            confidence += 0.1

        return min(0.95, max(0.1, confidence))

    def _update_intent_patterns(
        self, command: str, category: GoalCategory
    ) -> None:
        """Update learned intent patterns."""
        # Use command words as pattern keys
        for word in command.lower().split():
            self._intent_patterns[word][category.value] += 0.1

    def _find_related_goals(self, goal: InferredGoal) -> List[InferredGoal]:
        """Find related recent goals."""
        related = []
        recent_goals = self._goal_history[-10:]

        for g in recent_goals:
            if g.goal_id == goal.goal_id:
                continue
            # Same category within 5 minutes
            if (
                g.category == goal.category and
                (goal.inferred_at - g.inferred_at).total_seconds() < 300
            ):
                related.append(g)

        return related

    async def _classify_intent(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Multi-level intent classification."""
        command = payload.get("command", "")
        category, confidence = self._classify_category(command)
        level = self._determine_intent_level(command, payload.get("context", {}))

        return {
            "category": category.value,
            "level": level.value,
            "confidence": confidence,
            "keywords_matched": self._get_matched_keywords(command, category),
        }

    def _get_matched_keywords(
        self, command: str, category: GoalCategory
    ) -> List[str]:
        """Get keywords that matched for classification."""
        keywords = self._category_keywords.get(category, set())
        matched = [kw for kw in keywords if kw in command.lower()]
        return matched

    async def _predict_next_goal(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Predict likely next goals based on patterns."""
        current_goal_id = payload.get("current_goal_id")
        predictions = []

        if current_goal_id and current_goal_id in self._goals:
            current = self._goals[current_goal_id]

            # Find goals that typically follow this category
            follow_patterns = self._analyze_follow_patterns(current.category)
            for category, probability in follow_patterns[:3]:
                predictions.append({
                    "category": category.value,
                    "probability": probability,
                    "reason": f"Often follows {current.category.value}",
                })

        return {
            "predictions": predictions,
            "based_on": current_goal_id,
        }

    def _analyze_follow_patterns(
        self, category: GoalCategory
    ) -> List[Tuple[GoalCategory, float]]:
        """Analyze what goals typically follow a category."""
        follow_counts: Dict[GoalCategory, int] = defaultdict(int)

        for i, goal in enumerate(self._goal_history[:-1]):
            if goal.category == category:
                next_goal = self._goal_history[i + 1]
                follow_counts[next_goal.category] += 1

        total = sum(follow_counts.values()) or 1
        patterns = [
            (cat, count / total)
            for cat, count in follow_counts.items()
        ]
        return sorted(patterns, key=lambda x: x[1], reverse=True)

    def _chain_goals(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Link related goals into workflow chains."""
        goal_ids = payload.get("goal_ids", [])

        chains = []
        for gid in goal_ids:
            if gid in self._goal_chains:
                chains.append({
                    "root": gid,
                    "chain": self._goal_chains[gid],
                })

        return {"chains": chains}

    def _get_goal_history(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Get goal inference history."""
        limit = payload.get("limit", 50)
        category_filter = payload.get("category")

        history = self._goal_history[-limit:]
        if category_filter:
            history = [g for g in history if g.category.value == category_filter]

        return {
            "goals": [
                {
                    "goal_id": g.goal_id,
                    "category": g.category.value,
                    "description": g.description,
                    "confidence": g.confidence,
                    "inferred_at": g.inferred_at.isoformat(),
                }
                for g in history
            ],
            "total": len(history),
        }

    def _get_confidence(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Get confidence scores for a goal."""
        goal_id = payload.get("goal_id")
        if goal_id not in self._goals:
            return {"error": f"Goal {goal_id} not found"}

        goal = self._goals[goal_id]
        return {
            "goal_id": goal_id,
            "confidence": goal.confidence,
            "category_confidence": self._classify_category(
                goal.source_commands[0]
            )[1],
        }

    def _extract_entities(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Extract entities from command."""
        command = payload.get("command", "")
        entities = {
            "apps": [],
            "files": [],
            "contacts": [],
            "times": [],
            "urls": [],
        }

        # Simple entity extraction (could be enhanced with NER)
        words = command.split()
        for word in words:
            if word.endswith(".app") or word in ["Safari", "Chrome", "Slack"]:
                entities["apps"].append(word)
            elif "." in word and "/" not in word:
                entities["files"].append(word)
            elif word.startswith("http"):
                entities["urls"].append(word)

        return entities

    def _get_inference_stats(self) -> Dict[str, Any]:
        """Get inference statistics."""
        category_counts = defaultdict(int)
        for goal in self._goal_history:
            category_counts[goal.category.value] += 1

        return {
            "total_inferences": self._inference_count,
            "unique_goals": len(self._goals),
            "history_size": len(self._goal_history),
            "category_distribution": dict(category_counts),
            "average_confidence": (
                sum(g.confidence for g in self._goal_history) /
                len(self._goal_history)
                if self._goal_history else 0
            ),
        }

    async def _handle_task_for_inference(self, message: AgentMessage) -> None:
        """Handle task assignments for goal inference."""
        if message.payload.get("command"):
            await self._infer_goal({
                "command": message.payload["command"],
                "context": message.payload.get("context", {}),
            })

    async def _handle_context_signal(self, message: AgentMessage) -> None:
        """Handle context signals for inference."""
        if message.payload.get("signal_type"):
            signal = IntentSignal(
                signal_type=message.payload["signal_type"],
                value=message.payload.get("value"),
                weight=message.payload.get("weight", 1.0),
            )
            self._context_signals.append(signal)

            # Keep last 100 signals
            if len(self._context_signals) > 100:
                self._context_signals = self._context_signals[-100:]

    async def _periodic_goal_analysis(self) -> None:
        """Periodic background goal pattern analysis."""
        analysis_interval = float(os.getenv("GOAL_ANALYSIS_INTERVAL", "60.0"))
        max_runtime = float(os.getenv("TIMEOUT_GOAL_ANALYSIS_SESSION", "86400.0"))  # 24 hours
        iteration_timeout = float(os.getenv("TIMEOUT_GOAL_ANALYSIS_ITERATION", "30.0"))
        start = time.monotonic()
        cancelled = False

        while time.monotonic() - start < max_runtime:
            try:
                await asyncio.sleep(analysis_interval)

                # Analyze and consolidate patterns with timeout protection
                async def _analyze_patterns():
                    if len(self._goal_history) > 10:
                        # Find habitual patterns
                        habitual = [
                            g for g in self._goal_history
                            if g.intent_level == IntentLevel.HABITUAL
                        ]
                        if habitual:
                            logger.debug(
                                f"GoalInference: {len(habitual)} habitual patterns detected"
                            )

                await asyncio.wait_for(_analyze_patterns(), timeout=iteration_timeout)

            except asyncio.TimeoutError:
                logger.warning("Goal analysis iteration timed out")
            except asyncio.CancelledError:
                cancelled = True
                break
            except Exception as e:
                logger.warning(f"Goal analysis error: {e}")

        if cancelled:
            logger.info("Goal analysis loop cancelled (shutdown)")
        else:
            logger.info("Goal analysis loop reached max runtime, exiting")
