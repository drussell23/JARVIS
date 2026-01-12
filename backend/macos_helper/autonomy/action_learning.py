"""
Action Learning System for JARVIS Autonomous Actions.

This module provides machine learning capabilities for action optimization,
including success prediction, pattern learning, and adaptive behavior.

Key Features:
    - Success prediction based on historical patterns
    - Action timing optimization
    - Context-based learning
    - Failure pattern analysis
    - Parameter optimization
    - Adaptive execution strategies

Environment Variables:
    JARVIS_LEARNING_ENABLED: Enable learning system (default: true)
    JARVIS_LEARNING_MIN_SAMPLES: Minimum samples for predictions (default: 10)
    JARVIS_LEARNING_DECAY_FACTOR: Time decay for old data (default: 0.95)
    JARVIS_LEARNING_CONFIDENCE_THRESHOLD: Min confidence for predictions (default: 0.7)
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import statistics
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Set, Tuple

from backend.core.async_safety import LazyAsyncLock
from .action_registry import ActionCategory, ActionRiskLevel, ActionType

logger = logging.getLogger(__name__)


# =============================================================================
# ENUMS
# =============================================================================


class PatternType(Enum):
    """Types of learned patterns."""

    SUCCESS_RATE = "success_rate"
    EXECUTION_TIME = "execution_time"
    FAILURE_CORRELATION = "failure_correlation"
    TIMING_PREFERENCE = "timing_preference"
    PARAMETER_OPTIMIZATION = "parameter_optimization"
    CONTEXT_ASSOCIATION = "context_association"
    SEQUENCE_PATTERN = "sequence_pattern"


class PredictionConfidence(Enum):
    """Confidence levels for predictions."""

    VERY_LOW = 1
    LOW = 2
    MEDIUM = 3
    HIGH = 4
    VERY_HIGH = 5

    @classmethod
    def from_score(cls, score: float) -> "PredictionConfidence":
        """Convert confidence score to level."""
        if score >= 0.9:
            return cls.VERY_HIGH
        elif score >= 0.75:
            return cls.HIGH
        elif score >= 0.5:
            return cls.MEDIUM
        elif score >= 0.25:
            return cls.LOW
        return cls.VERY_LOW


# =============================================================================
# DATA CLASSES
# =============================================================================


@dataclass
class ActionOutcome:
    """Outcome of an action execution for learning."""

    action_type: ActionType
    success: bool
    execution_time_ms: float
    params: Dict[str, Any]
    context: Dict[str, Any]
    error_type: Optional[str] = None
    timestamp: datetime = field(default_factory=datetime.now)
    retry_count: int = 0

    # Temporal context
    hour_of_day: int = field(default_factory=lambda: datetime.now().hour)
    day_of_week: int = field(default_factory=lambda: datetime.now().weekday())

    def to_feature_vector(self) -> Dict[str, float]:
        """Convert to feature vector for learning."""
        features = {
            "success": 1.0 if self.success else 0.0,
            "execution_time_ms": self.execution_time_ms,
            "retry_count": float(self.retry_count),
            "hour_of_day": float(self.hour_of_day),
            "day_of_week": float(self.day_of_week),
        }

        # Add context features
        for key, value in self.context.items():
            if isinstance(value, bool):
                features[f"ctx_{key}"] = 1.0 if value else 0.0
            elif isinstance(value, (int, float)):
                features[f"ctx_{key}"] = float(value)

        return features


@dataclass
class LearningFeatures:
    """Features extracted for learning."""

    action_type: ActionType
    category: ActionCategory
    risk_level: ActionRiskLevel

    # Temporal features
    hour_of_day: int
    day_of_week: int
    is_weekend: bool
    is_business_hours: bool

    # Context features
    screen_locked: bool = False
    focus_mode_active: bool = False
    meeting_in_progress: bool = False
    recent_failures: int = 0
    actions_in_last_minute: int = 0

    # Historical features
    success_rate_24h: float = 1.0
    avg_execution_time_ms: float = 0.0
    failure_streak: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "action_type": self.action_type.name,
            "category": self.category.value,
            "risk_level": self.risk_level.name,
            "hour_of_day": self.hour_of_day,
            "day_of_week": self.day_of_week,
            "is_weekend": self.is_weekend,
            "is_business_hours": self.is_business_hours,
            "screen_locked": self.screen_locked,
            "focus_mode_active": self.focus_mode_active,
            "recent_failures": self.recent_failures,
            "success_rate_24h": self.success_rate_24h,
        }


@dataclass
class PredictionResult:
    """Result of a prediction."""

    action_type: ActionType
    predicted_success_rate: float
    confidence: PredictionConfidence
    confidence_score: float
    recommended_delay_ms: float
    risk_factors: List[str]
    recommendations: List[str]
    similar_outcomes: int
    predicted_at: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "action_type": self.action_type.name,
            "predicted_success_rate": self.predicted_success_rate,
            "confidence": self.confidence.name,
            "confidence_score": self.confidence_score,
            "recommended_delay_ms": self.recommended_delay_ms,
            "risk_factors": self.risk_factors,
            "recommendations": self.recommendations,
            "similar_outcomes": self.similar_outcomes,
        }


@dataclass
class LearnedPattern:
    """A learned pattern from execution history."""

    pattern_type: PatternType
    action_type: ActionType
    pattern_data: Dict[str, Any]
    confidence: float
    sample_count: int
    last_updated: datetime = field(default_factory=datetime.now)
    created_at: datetime = field(default_factory=datetime.now)


@dataclass
class TimingPreference:
    """Learned timing preferences for an action."""

    action_type: ActionType
    best_hours: List[int]
    worst_hours: List[int]
    best_days: List[int]
    success_by_hour: Dict[int, float]
    execution_time_by_hour: Dict[int, float]


@dataclass
class ActionLearningConfig:
    """Configuration for action learning."""

    enabled: bool = True
    min_samples_for_prediction: int = 10
    decay_factor: float = 0.95  # Per-day decay
    confidence_threshold: float = 0.7
    max_history_size: int = 10000
    pattern_update_interval_seconds: float = 300.0

    # Feature weights
    temporal_weight: float = 0.3
    context_weight: float = 0.4
    historical_weight: float = 0.3

    # Timing optimization
    timing_sample_threshold: int = 50
    business_hours_start: int = 9
    business_hours_end: int = 17

    @classmethod
    def from_env(cls) -> "ActionLearningConfig":
        """Create configuration from environment variables."""
        return cls(
            enabled=os.getenv("JARVIS_LEARNING_ENABLED", "true").lower() == "true",
            min_samples_for_prediction=int(os.getenv("JARVIS_LEARNING_MIN_SAMPLES", "10")),
            decay_factor=float(os.getenv("JARVIS_LEARNING_DECAY_FACTOR", "0.95")),
            confidence_threshold=float(os.getenv("JARVIS_LEARNING_CONFIDENCE_THRESHOLD", "0.7")),
            max_history_size=int(os.getenv("JARVIS_LEARNING_MAX_HISTORY", "10000")),
        )


# =============================================================================
# ACTION LEARNING SYSTEM
# =============================================================================


class ActionLearningSystem:
    """
    Machine learning system for action optimization.

    This system learns from execution history to provide predictions,
    recommendations, and adaptive behavior for autonomous actions.
    """

    def __init__(self, config: Optional[ActionLearningConfig] = None):
        """Initialize the learning system."""
        self.config = config or ActionLearningConfig.from_env()

        # Outcome history per action type
        self._outcomes: Dict[ActionType, deque[ActionOutcome]] = defaultdict(
            lambda: deque(maxlen=self.config.max_history_size // 10)
        )

        # Learned patterns
        self._patterns: Dict[Tuple[PatternType, ActionType], LearnedPattern] = {}

        # Timing preferences
        self._timing_preferences: Dict[ActionType, TimingPreference] = {}

        # Action sequences
        self._recent_actions: deque[Tuple[datetime, ActionType]] = deque(maxlen=100)
        self._sequence_patterns: Dict[Tuple[ActionType, ...], int] = defaultdict(int)

        # Failure correlations
        self._failure_contexts: Dict[ActionType, List[Dict[str, Any]]] = defaultdict(list)

        # Statistics
        self._total_outcomes_recorded = 0
        self._predictions_made = 0

        # State
        self._is_running = False
        self._lock = asyncio.Lock()
        self._last_pattern_update = datetime.now()

    async def start(self) -> None:
        """Start the learning system."""
        if self._is_running:
            return

        logger.info("Starting ActionLearningSystem...")
        self._is_running = True

        # Start background pattern learning
        asyncio.create_task(self._pattern_update_loop())

        logger.info(f"ActionLearningSystem started (enabled={self.config.enabled})")

    async def stop(self) -> None:
        """Stop the learning system."""
        if not self._is_running:
            return

        logger.info("Stopping ActionLearningSystem...")
        self._is_running = False
        logger.info("ActionLearningSystem stopped")

    @property
    def is_running(self) -> bool:
        """Check if system is running."""
        return self._is_running

    async def record_outcome(self, outcome: ActionOutcome) -> None:
        """
        Record an action outcome for learning.

        Args:
            outcome: The action outcome to record
        """
        if not self.config.enabled:
            return

        async with self._lock:
            self._outcomes[outcome.action_type].append(outcome)
            self._total_outcomes_recorded += 1

            # Track sequences
            self._recent_actions.append((outcome.timestamp, outcome.action_type))
            self._update_sequences()

            # Track failure contexts
            if not outcome.success:
                self._failure_contexts[outcome.action_type].append(outcome.context)
                # Limit failure context storage
                if len(self._failure_contexts[outcome.action_type]) > 100:
                    self._failure_contexts[outcome.action_type] = \
                        self._failure_contexts[outcome.action_type][-100:]

    async def predict_success(
        self,
        action_type: ActionType,
        params: Dict[str, Any],
        context: Dict[str, Any]
    ) -> PredictionResult:
        """
        Predict the success probability of an action.

        Args:
            action_type: The action type to predict
            params: Action parameters
            context: Execution context

        Returns:
            PredictionResult with prediction and recommendations
        """
        if not self.config.enabled:
            return self._default_prediction(action_type)

        self._predictions_made += 1

        # Get historical outcomes
        outcomes = list(self._outcomes.get(action_type, []))

        if len(outcomes) < self.config.min_samples_for_prediction:
            return self._default_prediction(action_type, len(outcomes))

        # Extract features for current prediction
        now = datetime.now()
        features = LearningFeatures(
            action_type=action_type,
            category=self._get_category(action_type),
            risk_level=self._get_risk_level(action_type),
            hour_of_day=now.hour,
            day_of_week=now.weekday(),
            is_weekend=now.weekday() >= 5,
            is_business_hours=self.config.business_hours_start <= now.hour < self.config.business_hours_end,
            screen_locked=context.get("screen_locked", False),
            focus_mode_active=context.get("focus_mode_active", False),
            meeting_in_progress=context.get("meeting_in_progress", False),
            recent_failures=context.get("recent_failures", 0),
            actions_in_last_minute=self._count_recent_actions(action_type, minutes=1),
        )

        # Calculate base success rate with time decay
        weighted_successes = 0.0
        weighted_total = 0.0

        for outcome in outcomes:
            age_days = (now - outcome.timestamp).total_seconds() / 86400
            weight = self.config.decay_factor ** age_days

            weighted_total += weight
            if outcome.success:
                weighted_successes += weight

        base_success_rate = weighted_successes / weighted_total if weighted_total > 0 else 0.5

        # Apply context-based adjustments
        adjusted_rate = base_success_rate
        risk_factors: List[str] = []
        recommendations: List[str] = []

        # Temporal adjustment
        timing_prefs = self._timing_preferences.get(action_type)
        if timing_prefs:
            hour_rate = timing_prefs.success_by_hour.get(features.hour_of_day)
            if hour_rate is not None:
                temporal_factor = hour_rate / max(0.01, base_success_rate)
                adjusted_rate *= (1 + (temporal_factor - 1) * self.config.temporal_weight)

                if features.hour_of_day in timing_prefs.worst_hours:
                    risk_factors.append(f"Hour {features.hour_of_day}:00 has historically lower success")
                    recommendations.append(f"Consider scheduling for hours {timing_prefs.best_hours}")

        # Context-based adjustment
        context_factor = 1.0

        if features.screen_locked:
            context_factor *= 0.8
            risk_factors.append("Screen is locked")

        if features.focus_mode_active:
            context_factor *= 0.9
            risk_factors.append("Focus mode active")

        if features.meeting_in_progress:
            context_factor *= 0.85
            risk_factors.append("Meeting in progress")

        if features.recent_failures >= 3:
            context_factor *= 0.7
            risk_factors.append("Recent failures detected")
            recommendations.append("Wait for system stability before retrying")

        adjusted_rate *= context_factor

        # Similar context success rate
        similar_outcomes = self._find_similar_outcomes(
            action_type, features, outcomes, max_results=20
        )
        if len(similar_outcomes) >= 5:
            similar_success_rate = sum(1 for o in similar_outcomes if o.success) / len(similar_outcomes)
            adjusted_rate = (adjusted_rate + similar_success_rate) / 2

        # Calculate confidence
        confidence_score = self._calculate_confidence(
            sample_count=len(outcomes),
            similar_count=len(similar_outcomes),
            recency=self._calculate_recency(outcomes),
            consistency=self._calculate_consistency(outcomes)
        )

        # Calculate recommended delay
        recommended_delay = self._calculate_recommended_delay(
            action_type, features, adjusted_rate
        )

        # Generate recommendations
        if adjusted_rate < 0.5:
            recommendations.append("Consider using dry-run mode first")

        if features.actions_in_last_minute > 10:
            recommendations.append("High action frequency detected - consider pacing")
            risk_factors.append("Rapid action rate")

        return PredictionResult(
            action_type=action_type,
            predicted_success_rate=max(0.0, min(1.0, adjusted_rate)),
            confidence=PredictionConfidence.from_score(confidence_score),
            confidence_score=confidence_score,
            recommended_delay_ms=recommended_delay,
            risk_factors=risk_factors,
            recommendations=recommendations,
            similar_outcomes=len(similar_outcomes)
        )

    async def get_optimal_timing(
        self,
        action_type: ActionType
    ) -> Optional[TimingPreference]:
        """
        Get optimal timing for an action based on historical success.

        Args:
            action_type: The action type

        Returns:
            TimingPreference if enough data available
        """
        return self._timing_preferences.get(action_type)

    async def get_failure_correlations(
        self,
        action_type: ActionType
    ) -> Dict[str, float]:
        """
        Get context factors correlated with failures.

        Args:
            action_type: The action type

        Returns:
            Dictionary of context factor to correlation score
        """
        failure_contexts = self._failure_contexts.get(action_type, [])

        if len(failure_contexts) < 5:
            return {}

        correlations: Dict[str, int] = defaultdict(int)
        total_failures = len(failure_contexts)

        for ctx in failure_contexts:
            for key, value in ctx.items():
                if isinstance(value, bool) and value:
                    correlations[key] += 1
                elif isinstance(value, (int, float)) and value > 0:
                    correlations[f"{key}_positive"] += 1

        return {
            key: count / total_failures
            for key, count in correlations.items()
            if count >= 2  # At least 2 occurrences
        }

    async def get_action_sequence_patterns(
        self,
        action_type: ActionType
    ) -> List[Tuple[Tuple[ActionType, ...], int]]:
        """
        Get common action sequences ending with the given action.

        Args:
            action_type: The action type

        Returns:
            List of (sequence, count) tuples
        """
        relevant_patterns = [
            (seq, count)
            for seq, count in self._sequence_patterns.items()
            if seq[-1] == action_type and count >= 3
        ]

        return sorted(relevant_patterns, key=lambda x: x[1], reverse=True)[:10]

    def _default_prediction(
        self,
        action_type: ActionType,
        sample_count: int = 0
    ) -> PredictionResult:
        """Create default prediction when insufficient data."""
        return PredictionResult(
            action_type=action_type,
            predicted_success_rate=0.8,  # Optimistic default
            confidence=PredictionConfidence.LOW,
            confidence_score=0.3,
            recommended_delay_ms=0,
            risk_factors=["Insufficient historical data for accurate prediction"],
            recommendations=["Continue using to improve predictions"],
            similar_outcomes=sample_count
        )

    def _find_similar_outcomes(
        self,
        action_type: ActionType,
        features: LearningFeatures,
        outcomes: List[ActionOutcome],
        max_results: int = 20
    ) -> List[ActionOutcome]:
        """Find outcomes with similar context."""
        scored_outcomes: List[Tuple[float, ActionOutcome]] = []

        for outcome in outcomes:
            similarity = 0.0

            # Hour similarity
            hour_diff = abs(outcome.hour_of_day - features.hour_of_day)
            hour_similarity = 1 - (min(hour_diff, 24 - hour_diff) / 12)
            similarity += hour_similarity * 0.3

            # Day similarity
            if outcome.day_of_week == features.day_of_week:
                similarity += 0.2

            # Context similarity
            ctx_matches = 0
            ctx_total = 0

            if "screen_locked" in outcome.context:
                ctx_total += 1
                if outcome.context["screen_locked"] == features.screen_locked:
                    ctx_matches += 1

            if "focus_mode_active" in outcome.context:
                ctx_total += 1
                if outcome.context["focus_mode_active"] == features.focus_mode_active:
                    ctx_matches += 1

            if ctx_total > 0:
                similarity += (ctx_matches / ctx_total) * 0.5

            scored_outcomes.append((similarity, outcome))

        # Sort by similarity and return top results
        scored_outcomes.sort(key=lambda x: x[0], reverse=True)
        return [o for _, o in scored_outcomes[:max_results]]

    def _calculate_confidence(
        self,
        sample_count: int,
        similar_count: int,
        recency: float,
        consistency: float
    ) -> float:
        """Calculate prediction confidence."""
        # Sample count factor (more samples = higher confidence)
        sample_factor = min(1.0, sample_count / 100)

        # Similar count factor
        similar_factor = min(1.0, similar_count / 20)

        # Combine factors
        confidence = (
            0.3 * sample_factor +
            0.2 * similar_factor +
            0.25 * recency +
            0.25 * consistency
        )

        return max(0.0, min(1.0, confidence))

    def _calculate_recency(self, outcomes: List[ActionOutcome]) -> float:
        """Calculate recency score (how recent the data is)."""
        if not outcomes:
            return 0.0

        now = datetime.now()
        recent_count = sum(
            1 for o in outcomes
            if (now - o.timestamp).total_seconds() < 86400  # Last 24 hours
        )

        return min(1.0, recent_count / 10)

    def _calculate_consistency(self, outcomes: List[ActionOutcome]) -> float:
        """Calculate consistency score (how consistent outcomes are)."""
        if len(outcomes) < 3:
            return 0.5

        success_values = [1 if o.success else 0 for o in outcomes]
        try:
            stdev = statistics.stdev(success_values)
            # Lower stdev = more consistent = higher score
            return max(0.0, 1.0 - (stdev * 2))
        except statistics.StatisticsError:
            return 0.5

    def _calculate_recommended_delay(
        self,
        action_type: ActionType,
        features: LearningFeatures,
        predicted_rate: float
    ) -> float:
        """Calculate recommended delay before execution."""
        delay = 0.0

        # If low success rate, recommend waiting
        if predicted_rate < 0.5:
            delay += 5000  # 5 seconds

        # If many recent actions, recommend throttling
        if features.actions_in_last_minute > 10:
            delay += 2000  # 2 seconds

        # If recent failures, recommend waiting
        if features.recent_failures >= 3:
            delay += 3000  # 3 seconds

        return delay

    def _count_recent_actions(
        self,
        action_type: ActionType,
        minutes: int = 1
    ) -> int:
        """Count recent actions of a given type."""
        cutoff = datetime.now() - timedelta(minutes=minutes)
        return sum(
            1 for ts, at in self._recent_actions
            if at == action_type and ts > cutoff
        )

    def _update_sequences(self) -> None:
        """Update sequence patterns from recent actions."""
        if len(self._recent_actions) < 3:
            return

        # Extract sequences of 2-4 actions
        actions = [at for _, at in self._recent_actions]

        for seq_len in range(2, 5):
            if len(actions) >= seq_len:
                for i in range(len(actions) - seq_len + 1):
                    seq = tuple(actions[i:i + seq_len])
                    self._sequence_patterns[seq] += 1

    def _get_category(self, action_type: ActionType) -> ActionCategory:
        """Get category for action type (simplified)."""
        name = action_type.name.lower()

        if "app" in name or "application" in name:
            return ActionCategory.APPLICATION
        elif "file" in name or "folder" in name:
            return ActionCategory.FILE_SYSTEM
        elif "system" in name:
            return ActionCategory.SYSTEM
        elif "volume" in name or "media" in name or "brightness" in name:
            return ActionCategory.MEDIA
        elif "screen" in name or "display" in name:
            return ActionCategory.DISPLAY
        elif "notification" in name or "dnd" in name:
            return ActionCategory.NOTIFICATION
        elif "security" in name or "lock" in name or "unlock" in name:
            return ActionCategory.SECURITY
        elif "workspace" in name or "focus" in name or "meeting" in name:
            return ActionCategory.PRODUCTIVITY
        elif "web" in name:
            return ActionCategory.NETWORK
        else:
            return ActionCategory.CUSTOM

    def _get_risk_level(self, action_type: ActionType) -> ActionRiskLevel:
        """Get risk level for action type (simplified)."""
        name = action_type.name.lower()

        if "delete" in name or "shutdown" in name or "restart" in name:
            return ActionRiskLevel.HIGH
        elif "close" in name or "quit" in name or "modify" in name:
            return ActionRiskLevel.MODERATE
        elif "open" in name or "focus" in name or "list" in name:
            return ActionRiskLevel.LOW
        elif "status" in name or "get" in name or "info" in name:
            return ActionRiskLevel.MINIMAL
        else:
            return ActionRiskLevel.MODERATE

    async def _pattern_update_loop(self) -> None:
        """Background task to update learned patterns."""
        while self._is_running:
            try:
                await asyncio.sleep(self.config.pattern_update_interval_seconds)
                await self._update_patterns()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Pattern update error: {e}")

    async def _update_patterns(self) -> None:
        """Update all learned patterns from historical data."""
        async with self._lock:
            for action_type, outcomes in self._outcomes.items():
                if len(outcomes) >= self.config.timing_sample_threshold:
                    self._update_timing_preferences(action_type, list(outcomes))

            self._last_pattern_update = datetime.now()

    def _update_timing_preferences(
        self,
        action_type: ActionType,
        outcomes: List[ActionOutcome]
    ) -> None:
        """Update timing preferences for an action type."""
        success_by_hour: Dict[int, List[bool]] = defaultdict(list)
        time_by_hour: Dict[int, List[float]] = defaultdict(list)

        for outcome in outcomes:
            success_by_hour[outcome.hour_of_day].append(outcome.success)
            time_by_hour[outcome.hour_of_day].append(outcome.execution_time_ms)

        # Calculate success rates per hour
        rates_by_hour: Dict[int, float] = {}
        avg_time_by_hour: Dict[int, float] = {}

        for hour in range(24):
            if hour in success_by_hour and len(success_by_hour[hour]) >= 3:
                rates_by_hour[hour] = sum(success_by_hour[hour]) / len(success_by_hour[hour])
                avg_time_by_hour[hour] = statistics.mean(time_by_hour[hour])

        if not rates_by_hour:
            return

        # Find best and worst hours
        sorted_hours = sorted(rates_by_hour.items(), key=lambda x: x[1], reverse=True)
        best_hours = [h for h, _ in sorted_hours[:3]]
        worst_hours = [h for h, _ in sorted_hours[-3:]]

        # Find best days (0=Monday, 6=Sunday)
        success_by_day: Dict[int, List[bool]] = defaultdict(list)
        for outcome in outcomes:
            success_by_day[outcome.day_of_week].append(outcome.success)

        rates_by_day = {
            day: sum(successes) / len(successes)
            for day, successes in success_by_day.items()
            if len(successes) >= 3
        }

        sorted_days = sorted(rates_by_day.items(), key=lambda x: x[1], reverse=True)
        best_days = [d for d, _ in sorted_days[:3]]

        self._timing_preferences[action_type] = TimingPreference(
            action_type=action_type,
            best_hours=best_hours,
            worst_hours=worst_hours,
            best_days=best_days,
            success_by_hour=rates_by_hour,
            execution_time_by_hour=avg_time_by_hour
        )

    def get_statistics(self) -> Dict[str, Any]:
        """Get learning system statistics."""
        return {
            "is_running": self._is_running,
            "enabled": self.config.enabled,
            "total_outcomes_recorded": self._total_outcomes_recorded,
            "predictions_made": self._predictions_made,
            "action_types_tracked": len(self._outcomes),
            "timing_preferences_learned": len(self._timing_preferences),
            "sequence_patterns_learned": len(self._sequence_patterns),
            "last_pattern_update": self._last_pattern_update.isoformat(),
        }

    def get_action_summary(
        self,
        action_type: ActionType
    ) -> Dict[str, Any]:
        """Get learning summary for a specific action type."""
        outcomes = list(self._outcomes.get(action_type, []))

        if not outcomes:
            return {"action_type": action_type.name, "sample_count": 0}

        successes = sum(1 for o in outcomes if o.success)
        exec_times = [o.execution_time_ms for o in outcomes]

        return {
            "action_type": action_type.name,
            "sample_count": len(outcomes),
            "success_rate": successes / len(outcomes),
            "avg_execution_time_ms": statistics.mean(exec_times),
            "min_execution_time_ms": min(exec_times),
            "max_execution_time_ms": max(exec_times),
            "timing_preferences": (
                self._timing_preferences[action_type].__dict__
                if action_type in self._timing_preferences
                else None
            ),
            "failure_correlations_count": len(self._failure_contexts.get(action_type, [])),
        }


# =============================================================================
# SINGLETON MANAGEMENT
# =============================================================================


_learning_system_instance: Optional[ActionLearningSystem] = None
_learning_lock = LazyAsyncLock()  # v100.1: Lazy initialization to avoid "no running event loop" error


def get_action_learning_system() -> ActionLearningSystem:
    """Get the global action learning system instance."""
    global _learning_system_instance
    if _learning_system_instance is None:
        _learning_system_instance = ActionLearningSystem()
    return _learning_system_instance


async def start_action_learning_system() -> ActionLearningSystem:
    """Start the global action learning system."""
    async with _learning_lock:
        system = get_action_learning_system()
        if not system.is_running:
            await system.start()
        return system


async def stop_action_learning_system() -> None:
    """Stop the global action learning system."""
    async with _learning_lock:
        global _learning_system_instance
        if _learning_system_instance and _learning_system_instance.is_running:
            await _learning_system_instance.stop()
