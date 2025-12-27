#!/usr/bin/env python3
"""
Workspace Pattern Learner
=========================

Advanced ML-based cross-Space pattern recognition for 24/7 behavioral learning.

This module provides:
- Machine learning-based pattern detection
- Cross-workspace workflow recognition
- Temporal behavioral analysis
- Predictive workspace switching
- App usage pattern clustering
- Context-aware learning

Features:
- Unsupervised learning from workspace usage
- Real-time pattern classification
- Adaptive confidence scoring
- Multi-dimensional behavioral vectors
- Temporal sequence analysis
- Proactive suggestion generation

Author: Derek J. Russell
Date: October 2025
Version: 2.0.0 - ML-Powered Pattern Learning
"""

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple, Set
from dataclasses import dataclass, asdict, field
from collections import defaultdict, deque, Counter
import json
from enum import Enum
import calendar
import numpy as np
from scipy.stats import entropy
from sklearn.cluster import DBSCAN
from sklearn.preprocessing import StandardScaler

logger = logging.getLogger(__name__)


# ============================================================================
# Pattern Types and Data Models
# ============================================================================

class PatternType(Enum):
    """Types of learnable patterns"""
    WORKFLOW = "workflow"                    # Sequential app usage
    TEMPORAL = "temporal"                    # Time-based behaviors
    SPATIAL = "spatial"                      # Space-specific preferences
    APP_AFFINITY = "app_affinity"           # App co-occurrence
    TRANSITION = "transition"                # Space switching patterns
    SESSION = "session"                      # Usage session characteristics
    CONTEXTUAL = "contextual"                # Context-aware behaviors


class ConfidenceLevel(Enum):
    """Confidence levels for learned patterns"""
    LOW = 0.3
    MEDIUM = 0.6
    HIGH = 0.8
    VERY_HIGH = 0.95


@dataclass
class BehavioralVector:
    """Multi-dimensional vector representing user behavior"""
    temporal_features: np.ndarray           # Hour, day, week patterns
    spatial_features: np.ndarray            # Space usage distribution
    app_features: np.ndarray                # App usage frequencies
    transition_features: np.ndarray         # Space transition patterns
    session_features: np.ndarray            # Session characteristics
    timestamp: float
    confidence: float = 0.5


@dataclass
class LearnedPattern:
    """A learned behavioral pattern"""
    pattern_id: str
    pattern_type: PatternType
    confidence: float
    occurrences: int
    first_seen: float
    last_seen: float
    features: Dict[str, Any]
    metadata: Dict[str, Any]
    vector: Optional[BehavioralVector] = None


@dataclass
class WorkflowPattern:
    """Sequential workflow pattern"""
    workflow_id: str
    sequence: List[Tuple[str, int]]         # [(app, space), ...]
    frequency: int
    avg_duration: float
    typical_times: List[int]                # Hours of day
    confidence: float
    triggers: List[str]                     # Common triggers
    last_seen: float = 0.0                  # Timestamp of last observation


@dataclass
class TemporalPattern:
    """Time-based behavioral pattern"""
    hour: int
    day_of_week: int
    actions: List[Dict[str, Any]]
    frequency: int
    confidence: float
    variance: float                         # How consistent is this?


@dataclass
class SpatialPreference:
    """Space-specific usage preference"""
    space_id: int
    preferred_apps: List[Tuple[str, float]] # [(app, score), ...]
    layout_preferences: Dict[str, Any]
    usage_frequency: float
    time_preferences: List[int]


@dataclass
class PredictiveSuggestion:
    """AI-generated proactive suggestion"""
    suggestion_type: str
    target_space: Optional[int]
    target_app: Optional[str]
    action: str
    confidence: float
    reasoning: str
    timestamp: float


# ============================================================================
# Workspace Pattern Learner (ML-Powered)
# ============================================================================

class WorkspacePatternLearner:
    """
    Advanced ML-based pattern learning engine for cross-Space behavioral intelligence

    Uses unsupervised learning to discover:
    - Workflow patterns
    - Temporal behaviors
    - Spatial preferences
    - App affinities
    - Context-aware patterns
    """

    def __init__(
        self,
        learning_db=None,
        min_pattern_occurrences: int = 3,
        confidence_threshold: float = 0.6,
        vector_dimension: int = 64,
        enable_clustering: bool = True,
        enable_predictions: bool = True
    ):
        """
        Initialize Pattern Learner

        Args:
            learning_db: Learning Database instance
            min_pattern_occurrences: Minimum times before pattern is "learned"
            confidence_threshold: Minimum confidence to surface patterns
            vector_dimension: Dimensionality of behavioral vectors
            enable_clustering: Use ML clustering for pattern discovery
            enable_predictions: Generate predictive suggestions
        """
        self.learning_db = learning_db
        self.min_pattern_occurrences = min_pattern_occurrences
        self.confidence_threshold = confidence_threshold
        self.vector_dimension = vector_dimension
        self.enable_clustering = enable_clustering
        self.enable_predictions = enable_predictions

        # Pattern storage
        self.learned_patterns: Dict[str, LearnedPattern] = {}
        self.workflow_patterns: Dict[str, WorkflowPattern] = {}
        self.temporal_patterns: Dict[Tuple[int, int], TemporalPattern] = {}
        self.spatial_preferences: Dict[int, SpatialPreference] = {}

        # Real-time tracking
        self.current_workflow_buffer: deque = deque(maxlen=20)
        self.recent_actions: deque = deque(maxlen=100)
        self.behavioral_vectors: deque = deque(maxlen=500)

        # ML components
        self.scaler = StandardScaler()
        self.clusterer = None
        self.pattern_clusters: Dict[int, List[str]] = defaultdict(list)

        # Statistics
        self.stats = {
            'patterns_learned': 0,
            'workflows_detected': 0,
            'predictions_generated': 0,
            'ml_updates': 0,
            'clustering_runs': 0
        }

        logger.info("[WPL] Workspace Pattern Learner initialized")
        logger.info(f"[WPL] ML clustering: {enable_clustering}")
        logger.info(f"[WPL] Predictive engine: {enable_predictions}")
        logger.info(f"[WPL] Confidence threshold: {confidence_threshold}")

    # ========================================================================
    # Pattern Learning Methods
    # ========================================================================

    async def learn_from_event(self, event_type: str, event_data: Dict[str, Any]):
        """
        Learn from a single event

        Args:
            event_type: Type of event (space_changed, window_focused, etc.)
            event_data: Event metadata
        """
        try:
            # Add to action buffer
            action = {
                'type': event_type,
                'data': event_data,
                'timestamp': time.time()
            }
            self.recent_actions.append(action)

            # Update current workflow
            if event_type in ['space_changed', 'window_focused', 'app_launched']:
                await self._update_workflow_tracking(action)

            # Learn temporal patterns
            await self._learn_temporal_pattern(action)

            # Learn spatial preferences
            if 'space_id' in event_data:
                await self._learn_spatial_preference(event_data)

            # Generate behavioral vector
            if len(self.recent_actions) >= 10:
                vector = await self._generate_behavioral_vector()
                self.behavioral_vectors.append(vector)

                # Run clustering periodically
                if self.enable_clustering and len(self.behavioral_vectors) % 50 == 0:
                    await self._run_clustering()

            # Generate predictions
            if self.enable_predictions:
                await self._generate_predictions()

        except Exception as e:
            logger.error(f"[WPL] Error learning from event: {e}", exc_info=True)

    async def _update_workflow_tracking(self, action: Dict[str, Any]):
        """Track sequential workflow patterns"""
        try:
            data = action['data']

            # Extract app and space
            app_name = data.get('app_name')
            space_id = data.get('space_id')

            if app_name and space_id:
                self.current_workflow_buffer.append((app_name, space_id))

                # Check for repeating sequences
                if len(self.current_workflow_buffer) >= 3:
                    await self._detect_workflow_patterns()

        except Exception as e:
            logger.error(f"[WPL] Error updating workflow: {e}", exc_info=True)

    async def _detect_workflow_patterns(self):
        """Detect repeating workflow sequences"""
        try:
            # Look for sequences of length 3-7
            for seq_len in range(3, min(8, len(self.current_workflow_buffer) + 1)):
                sequence = list(self.current_workflow_buffer)[-seq_len:]
                seq_key = self._sequence_to_key(sequence)

                # Check if we've seen this workflow before
                if seq_key in self.workflow_patterns:
                    workflow = self.workflow_patterns[seq_key]
                    workflow.frequency += 1
                    workflow.last_seen = time.time()
                    workflow.confidence = min(0.99, workflow.confidence + 0.05)

                    logger.debug(f"[WPL] Workflow pattern reinforced: {seq_key} (confidence: {workflow.confidence:.2f})")

                    # Store to Learning DB (Phase 3)
                    if self.learning_db and workflow.confidence >= self.confidence_threshold:
                        await self._store_workflow_to_db(workflow)

                elif len([w for w in self.workflow_patterns.values() if w.sequence == sequence]) == 0:
                    # New potential workflow
                    import time
                    now = datetime.now()
                    workflow = WorkflowPattern(
                        workflow_id=seq_key,
                        sequence=sequence,
                        frequency=1,
                        avg_duration=0.0,
                        typical_times=[now.hour],
                        confidence=0.3,
                        triggers=[],
                        last_seen=time.time()
                    )
                    self.workflow_patterns[seq_key] = workflow

                    logger.info(f"[WPL] New workflow pattern detected: {seq_key}")
                    self.stats['workflows_detected'] += 1

            # Clean up low-confidence workflows
            await self._prune_weak_patterns()

        except Exception as e:
            logger.error(f"[WPL] Error detecting workflows: {e}", exc_info=True)

    async def _learn_temporal_pattern(self, action: Dict[str, Any]):
        """Learn time-based behavioral patterns"""
        try:
            now = datetime.now()
            hour = now.hour
            day_of_week = now.weekday()

            key = (hour, day_of_week)

            if key in self.temporal_patterns:
                pattern = self.temporal_patterns[key]
                pattern.actions.append(action)
                pattern.frequency += 1
                pattern.confidence = min(0.99, pattern.confidence + 0.02)

                # Calculate variance to measure consistency
                pattern.variance = self._calculate_temporal_variance(pattern.actions)

            else:
                # New temporal pattern
                pattern = TemporalPattern(
                    hour=hour,
                    day_of_week=day_of_week,
                    actions=[action],
                    frequency=1,
                    confidence=0.3,
                    variance=0.0
                )
                self.temporal_patterns[key] = pattern

                logger.debug(f"[WPL] New temporal pattern: {hour}:00 on day {day_of_week}")

            # Store to Learning DB
            if self.learning_db and pattern.confidence >= self.confidence_threshold:
                await self._store_temporal_pattern(pattern)

        except Exception as e:
            logger.error(f"[WPL] Error learning temporal pattern: {e}", exc_info=True)

    async def _learn_spatial_preference(self, event_data: Dict[str, Any]):
        """Learn Space-specific usage preferences"""
        try:
            space_id = event_data.get('space_id')
            app_name = event_data.get('app_name')

            if not space_id:
                return

            if space_id in self.spatial_preferences:
                pref = self.spatial_preferences[space_id]
                pref.usage_frequency += 1

                # Update app preferences
                if app_name:
                    app_scores = dict(pref.preferred_apps)
                    app_scores[app_name] = app_scores.get(app_name, 0.0) + 0.1
                    pref.preferred_apps = sorted(app_scores.items(), key=lambda x: x[1], reverse=True)[:10]

                # Update time preferences
                now = datetime.now()
                if now.hour not in pref.time_preferences:
                    pref.time_preferences.append(now.hour)

            else:
                # New spatial preference
                now = datetime.now()
                pref = SpatialPreference(
                    space_id=space_id,
                    preferred_apps=[(app_name, 1.0)] if app_name else [],
                    layout_preferences={},
                    usage_frequency=1,
                    time_preferences=[now.hour]
                )
                self.spatial_preferences[space_id] = pref

                logger.debug(f"[WPL] Learning spatial preferences for Space {space_id}")

        except Exception as e:
            logger.error(f"[WPL] Error learning spatial preference: {e}", exc_info=True)

    # ========================================================================
    # ML & Feature Extraction
    # ========================================================================

    async def _generate_behavioral_vector(self) -> BehavioralVector:
        """Generate multi-dimensional behavioral vector from recent actions"""
        try:
            now = datetime.now()

            # Extract temporal features
            temporal = np.array([
                now.hour / 24.0,
                now.weekday() / 7.0,
                now.day / 31.0,
                now.month / 12.0,
                1.0 if calendar.isleap(now.year) else 0.0
            ])

            # Extract spatial features (space usage distribution)
            space_usage = Counter([
                a['data'].get('space_id')
                for a in list(self.recent_actions)[-20:]
                if 'space_id' in a['data']
            ])
            spatial = np.array([space_usage.get(i, 0) for i in range(10)])
            spatial = spatial / (np.sum(spatial) + 1e-6)  # Normalize

            # Extract app features
            app_usage = Counter([
                a['data'].get('app_name')
                for a in list(self.recent_actions)[-20:]
                if 'app_name' in a['data']
            ])
            # Use entropy as a feature
            app_entropy = entropy(list(app_usage.values()) + [1]) if app_usage else 0.0
            app_diversity = len(app_usage)
            app_features = np.array([app_entropy, app_diversity / 10.0])

            # Extract transition features
            transitions = []
            actions_list = list(self.recent_actions)[-20:]
            for i in range(1, len(actions_list)):
                prev_space = actions_list[i-1]['data'].get('space_id')
                curr_space = actions_list[i]['data'].get('space_id')
                if prev_space and curr_space and prev_space != curr_space:
                    transitions.append(1)
            transition_rate = len(transitions) / len(actions_list) if actions_list else 0.0
            transition_features = np.array([transition_rate])

            # Extract session features
            session_duration = time.time() - actions_list[0]['timestamp'] if actions_list else 0.0
            action_rate = len(actions_list) / (session_duration + 1e-6)
            session_features = np.array([session_duration / 3600.0, action_rate])

            # Combine into vector
            vector = BehavioralVector(
                temporal_features=temporal,
                spatial_features=spatial,
                app_features=app_features,
                transition_features=transition_features,
                session_features=session_features,
                timestamp=time.time(),
                confidence=0.7
            )

            return vector

        except Exception as e:
            logger.error(f"[WPL] Error generating behavioral vector: {e}", exc_info=True)
            # Return zero vector on error
            return BehavioralVector(
                temporal_features=np.zeros(5),
                spatial_features=np.zeros(10),
                app_features=np.zeros(2),
                transition_features=np.zeros(1),
                session_features=np.zeros(2),
                timestamp=time.time(),
                confidence=0.0
            )

    async def _run_clustering(self):
        """Run ML clustering on behavioral vectors"""
        try:
            if len(self.behavioral_vectors) < 20:
                return

            logger.info("[WPL] Running ML clustering on behavioral vectors...")

            # Convert vectors to matrix
            vectors_list = list(self.behavioral_vectors)
            feature_matrix = []

            for vec in vectors_list:
                features = np.concatenate([
                    vec.temporal_features,
                    vec.spatial_features,
                    vec.app_features,
                    vec.transition_features,
                    vec.session_features
                ])
                feature_matrix.append(features)

            X = np.array(feature_matrix)

            # Scale features
            X_scaled = self.scaler.fit_transform(X)

            # Run DBSCAN clustering
            self.clusterer = DBSCAN(eps=0.5, min_samples=3)
            labels = self.clusterer.fit_predict(X_scaled)

            # Analyze clusters
            unique_labels = set(labels)
            n_clusters = len([l for l in unique_labels if l != -1])
            n_noise = list(labels).count(-1)

            logger.info(f"[WPL] Clustering complete: {n_clusters} clusters found, {n_noise} noise points")

            # Update pattern clusters
            self.pattern_clusters.clear()
            for idx, label in enumerate(labels):
                if label != -1:
                    pattern_id = f"cluster_{label}"
                    self.pattern_clusters[label].append(pattern_id)

            self.stats['clustering_runs'] += 1
            self.stats['ml_updates'] += 1

        except Exception as e:
            logger.error(f"[WPL] Error in clustering: {e}", exc_info=True)

    # ========================================================================
    # Predictions & Suggestions
    # ========================================================================

    async def _generate_predictions(self):
        """Generate predictive suggestions based on learned patterns"""
        try:
            if not self.enable_predictions:
                return

            now = datetime.now()
            hour = now.hour
            day = now.weekday()

            suggestions = []

            # Check temporal patterns
            key = (hour, day)
            if key in self.temporal_patterns:
                pattern = self.temporal_patterns[key]
                if pattern.confidence >= self.confidence_threshold:
                    # Generate suggestion based on typical actions
                    suggestion = await self._create_temporal_suggestion(pattern)
                    if suggestion:
                        suggestions.append(suggestion)

            # Check workflow patterns
            if len(self.current_workflow_buffer) >= 2:
                partial_workflow = list(self.current_workflow_buffer)[-2:]
                matching_workflows = self._find_matching_workflows(partial_workflow)

                for workflow in matching_workflows:
                    if workflow.confidence >= self.confidence_threshold:
                        suggestion = await self._create_workflow_suggestion(workflow)
                        if suggestion:
                            suggestions.append(suggestion)

            # Store suggestions to Learning DB
            if suggestions and self.learning_db:
                for suggestion in suggestions:
                    await self._store_suggestion(suggestion)
                    self.stats['predictions_generated'] += 1

        except Exception as e:
            logger.error(f"[WPL] Error generating predictions: {e}", exc_info=True)

    async def _create_temporal_suggestion(self, pattern: TemporalPattern) -> Optional[PredictiveSuggestion]:
        """Create suggestion from temporal pattern"""
        try:
            if not pattern.actions:
                return None

            # Find most common action in this time period
            action_types = Counter([a['type'] for a in pattern.actions])
            most_common = action_types.most_common(1)[0]

            suggestion = PredictiveSuggestion(
                suggestion_type="temporal",
                target_space=None,
                target_app=None,
                action=most_common[0],
                confidence=pattern.confidence,
                reasoning=f"Based on your typical behavior at {pattern.hour}:00 on {['Mon','Tue','Wed','Thu','Fri','Sat','Sun'][pattern.day_of_week]}",
                timestamp=time.time()
            )

            return suggestion

        except Exception as e:
            logger.error(f"[WPL] Error creating temporal suggestion: {e}", exc_info=True)
            return None

    async def _create_workflow_suggestion(self, workflow: WorkflowPattern) -> Optional[PredictiveSuggestion]:
        """Create suggestion from workflow pattern"""
        try:
            # Predict next step in workflow
            current_pos = len(self.current_workflow_buffer)
            if current_pos < len(workflow.sequence):
                next_step = workflow.sequence[current_pos]

                suggestion = PredictiveSuggestion(
                    suggestion_type="workflow",
                    target_space=next_step[1],
                    target_app=next_step[0],
                    action="switch_to",
                    confidence=workflow.confidence,
                    reasoning=f"You typically use {next_step[0]} next in this workflow",
                    timestamp=time.time()
                )

                return suggestion

        except Exception as e:
            logger.error(f"[WPL] Error creating workflow suggestion: {e}", exc_info=True)
            return None

    # ========================================================================
    # Query & Retrieval Methods
    # ========================================================================

    def get_learned_patterns(self, pattern_type: Optional[PatternType] = None, min_confidence: float = 0.6) -> List[LearnedPattern]:
        """Get all learned patterns above confidence threshold"""
        patterns = []

        # Workflow patterns
        for workflow in self.workflow_patterns.values():
            if workflow.confidence >= min_confidence:
                pattern = LearnedPattern(
                    pattern_id=workflow.workflow_id,
                    pattern_type=PatternType.WORKFLOW,
                    confidence=workflow.confidence,
                    occurrences=workflow.frequency,
                    first_seen=0.0,
                    last_seen=time.time(),
                    features={'sequence': workflow.sequence},
                    metadata={'typical_times': workflow.typical_times}
                )
                patterns.append(pattern)

        return patterns

    def get_spatial_insights(self, space_id: int) -> Optional[SpatialPreference]:
        """Get learned insights for a specific Space"""
        return self.spatial_preferences.get(space_id)

    def get_temporal_prediction(self, hour: int, day: int) -> Optional[TemporalPattern]:
        """Get temporal pattern for specific time"""
        return self.temporal_patterns.get((hour, day))

    def get_statistics(self) -> Dict[str, Any]:
        """Get learning statistics"""
        return {
            **self.stats,
            'total_patterns': len(self.workflow_patterns),
            'temporal_patterns': len(self.temporal_patterns),
            'spatial_preferences': len(self.spatial_preferences),
            'behavioral_vectors': len(self.behavioral_vectors),
            'active_clusters': len(self.pattern_clusters)
        }

    # ========================================================================
    # Helper Methods
    # ========================================================================

    def _sequence_to_key(self, sequence: List[Tuple[str, int]]) -> str:
        """Convert sequence to unique key"""
        return "->".join([f"{app}@{space}" for app, space in sequence])

    def _find_matching_workflows(self, partial: List[Tuple[str, int]]) -> List[WorkflowPattern]:
        """Find workflows that start with partial sequence"""
        matches = []
        for workflow in self.workflow_patterns.values():
            if len(workflow.sequence) > len(partial):
                if workflow.sequence[:len(partial)] == partial:
                    matches.append(workflow)
        return matches

    def _calculate_temporal_variance(self, actions: List[Dict[str, Any]]) -> float:
        """Calculate variance in temporal pattern"""
        if len(actions) < 2:
            return 0.0

        timestamps = [a['timestamp'] for a in actions]
        intervals = [timestamps[i+1] - timestamps[i] for i in range(len(timestamps)-1)]

        if not intervals:
            return 0.0

        return float(np.std(intervals))

    async def _prune_weak_patterns(self):
        """Remove low-confidence patterns"""
        # Remove workflows with low confidence and old last_seen
        current_time = time.time()
        to_remove = []

        for wid, workflow in self.workflow_patterns.items():
            # Remove if low confidence and not seen in 24 hours
            if workflow.confidence < 0.4 and (current_time - workflow.last_seen > 86400):
                to_remove.append(wid)

        for wid in to_remove:
            del self.workflow_patterns[wid]

    async def _store_workflow_to_db(self, workflow: WorkflowPattern):
        """Store workflow pattern to Learning DB (Phase 3)"""
        if not self.learning_db:
            return

        try:
            # Extract app and space sequences
            app_sequence = [item[0] for item in workflow.sequence]
            space_sequence = [item[1] for item in workflow.sequence]

            # Create action sequence from workflow
            action_sequence = [
                {'action': 'focus_app', 'app': app, 'space': space}
                for app, space in workflow.sequence
            ]

            await self.learning_db.store_user_workflow(
                workflow_name=workflow.workflow_id,
                action_sequence=action_sequence,
                space_sequence=space_sequence,
                app_sequence=app_sequence,
                duration=workflow.avg_duration,
                success=True,
                confidence=workflow.confidence,
                metadata={'typical_times': workflow.typical_times}
            )
        except Exception as e:
            logger.error(f"[WPL] Error storing workflow: {e}", exc_info=True)

    async def _store_temporal_pattern(self, pattern: TemporalPattern):
        """Store temporal pattern to Learning DB (Phase 3)"""
        if not self.learning_db:
            return

        try:
            await self.learning_db.store_temporal_pattern(
                pattern_type="user_behavior",
                time_of_day=pattern.hour,
                day_of_week=pattern.day_of_week,
                action_type="behavioral_pattern",
                target="workspace",
                frequency=pattern.frequency,
                confidence=pattern.confidence
            )
        except Exception as e:
            logger.error(f"[WPL] Error storing temporal pattern: {e}", exc_info=True)

    async def _store_suggestion(self, suggestion: PredictiveSuggestion):
        """Store prediction to Learning DB (Phase 3)"""
        if not self.learning_db:
            return

        try:
            # Format suggestion text
            suggestion_text = f"{suggestion.action}"
            if suggestion.target_app:
                suggestion_text += f" {suggestion.target_app}"
            if suggestion.target_space:
                suggestion_text += f" on Space {suggestion.target_space}"

            await self.learning_db.store_proactive_suggestion(
                suggestion_type=suggestion.suggestion_type,
                suggestion_text=suggestion_text,
                trigger_pattern_id=None,
                confidence=suggestion.confidence,
                metadata={
                    'target_space': suggestion.target_space,
                    'target_app': suggestion.target_app,
                    'action': suggestion.action,
                    'reasoning': suggestion.reasoning
                }
            )
        except Exception as e:
            logger.error(f"[WPL] Error storing suggestion: {e}", exc_info=True)


# ============================================================================
# Factory Functions
# ============================================================================

_pattern_learner_instance: Optional[WorkspacePatternLearner] = None


async def get_pattern_learner(
    learning_db=None,
    min_pattern_occurrences: int = 3,
    confidence_threshold: float = 0.6
) -> WorkspacePatternLearner:
    """Get or create Pattern Learner singleton"""
    global _pattern_learner_instance

    if _pattern_learner_instance is None:
        _pattern_learner_instance = WorkspacePatternLearner(
            learning_db=learning_db,
            min_pattern_occurrences=min_pattern_occurrences,
            confidence_threshold=confidence_threshold
        )
        logger.info("[WPL] Pattern Learner singleton created")

    return _pattern_learner_instance


def get_pattern_learner_sync() -> Optional[WorkspacePatternLearner]:
    """Get existing Pattern Learner instance (sync)"""
    return _pattern_learner_instance
