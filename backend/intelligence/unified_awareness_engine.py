#!/usr/bin/env python3
"""
Unified Awareness Engine (UAE)
===============================

Production-grade fusion of Context Intelligence and Situational Awareness
into a single self-correcting cognitive system with persistent learning.

UAE = Context (past) × Situation (present) × Learning (memory) = True Intelligence

Features:
- Bidirectional learning between historical context and real-time perception
- Persistent memory with Learning Database integration
- Confidence-weighted decision making
- Priority-based element monitoring
- Self-healing intelligence loop
- Continuous adaptation and improvement
- Zero hardcoding, fully dynamic
- Temporal pattern recognition and prediction

Architecture:
    UnifiedAwarenessEngine
    ├── ContextIntelligenceLayer (historical patterns, intent)
    ├── SituationalAwarenessLayer (real-time perception)
    ├── AwarenessIntegrationLayer (decision fusion)
    ├── LearningDatabaseLayer (persistent memory)
    └── FeedbackLoop (continuous learning)

Author: Derek J. Russell
Date: October 2025
Version: 2.0.0 - Learning Database Integration
"""

import asyncio
import logging
import time
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple, Set, Callable
from enum import Enum
from collections import defaultdict, deque
import numpy as np

# Import SAI components
try:
    from backend.vision.situational_awareness import (
        SituationalAwarenessEngine,
        UIElementPosition,
        ChangeEvent,
        ChangeType,
        get_sai_engine
    )
except ModuleNotFoundError:
    # Try relative import
    from vision.situational_awareness import (
        SituationalAwarenessEngine,
        UIElementPosition,
        ChangeEvent,
        ChangeType,
        get_sai_engine
    )

# Import Learning Database
try:
    from backend.intelligence.learning_database import (
        JARVISLearningDatabase,
        get_learning_database,
        PatternType
    )
except ModuleNotFoundError:
    # Try relative import
    from intelligence.learning_database import (
        JARVISLearningDatabase,
        get_learning_database,
        PatternType
    )

logger = logging.getLogger(__name__)


# ============================================================================
# Data Models
# ============================================================================

class DecisionSource(Enum):
    """Source of decision data"""
    CONTEXT = "context"           # From historical patterns
    SITUATION = "situation"       # From real-time perception
    FUSION = "fusion"             # Weighted combination
    FALLBACK = "fallback"         # Default/emergency


class ConfidenceSource(Enum):
    """What contributes to confidence score"""
    RECENCY = "recency"           # How recent the data is
    CONSISTENCY = "consistency"   # How consistent across observations
    FREQUENCY = "frequency"       # How often element is used
    VERIFICATION = "verification" # Whether verified by vision
    RELIABILITY = "reliability"   # Historical success rate


@dataclass
class ElementPriority:
    """Priority score for monitoring an element"""
    element_id: str
    priority_score: float  # 0.0-1.0
    usage_frequency: int
    last_used: float
    importance: float  # User-defined importance
    failure_rate: float

    def calculate_priority(self) -> float:
        """Calculate overall priority score"""
        # Weighted combination
        recency_weight = 0.3
        frequency_weight = 0.3
        importance_weight = 0.25
        reliability_weight = 0.15

        # Recency score (exponential decay)
        age_hours = (time.time() - self.last_used) / 3600
        recency_score = np.exp(-age_hours / 24)  # Half-life of 24 hours

        # Frequency score (normalized)
        frequency_score = min(self.usage_frequency / 100, 1.0)

        # Reliability score
        reliability_score = 1.0 - self.failure_rate

        # Combined score
        self.priority_score = (
            recency_weight * recency_score +
            frequency_weight * frequency_score +
            importance_weight * self.importance +
            reliability_weight * reliability_score
        )

        return self.priority_score


@dataclass
class ContextualData:
    """Data from Context Intelligence"""
    element_id: str
    expected_position: Optional[Tuple[int, int]]
    confidence: float
    usage_count: int
    last_success: float
    pattern_strength: float  # How strong the pattern is
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SituationalData:
    """Data from Situational Awareness"""
    element_id: str
    detected_position: Optional[Tuple[int, int]]
    confidence: float
    detection_method: str
    detection_time: float
    visual_hash: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class UnifiedDecision:
    """Unified decision combining context and situation"""
    element_id: str
    chosen_position: Tuple[int, int]
    confidence: float
    decision_source: DecisionSource
    context_weight: float
    situation_weight: float
    reasoning: str
    timestamp: float
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ExecutionResult:
    """Result of executing a decision"""
    decision: UnifiedDecision
    success: bool
    execution_time: float
    verification_passed: bool
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


# ============================================================================
# Context Intelligence Layer
# ============================================================================

class ContextIntelligenceLayer:
    """
    Historical pattern learning and contextual understanding with persistent memory

    Manages long-term memory of:
    - Element positions and usage patterns
    - User workflows and preferences
    - Command intent and expected outcomes
    - Reliability and success history
    - Temporal patterns and predictions

    Uses Learning Database for persistent storage and semantic search
    """

    def __init__(self, knowledge_base_path: Optional[Path] = None, learning_db: Optional[JARVISLearningDatabase] = None):
        self.knowledge_base_path = knowledge_base_path or (
            Path.home() / ".jarvis" / "uae_context.json"
        )
        self.knowledge_base_path.parent.mkdir(parents=True, exist_ok=True)

        # Learning Database integration
        self.learning_db = learning_db
        self.db_initialized = False

        # Context data structures (in-memory cache)
        self.element_patterns: Dict[str, ContextualData] = {}
        self.usage_history: deque = deque(maxlen=1000)
        self.success_history: Dict[str, deque] = defaultdict(lambda: deque(maxlen=100))
        self.workflow_patterns: Dict[str, List[str]] = {}

        # Priority management
        self.element_priorities: Dict[str, ElementPriority] = {}

        # Metrics
        self.metrics = {
            'total_predictions': 0,
            'successful_predictions': 0,
            'pattern_updates': 0,
            'learning_events': 0,
            'db_stores': 0,
            'db_retrievals': 0
        }

        self._load_knowledge_base()

        logger.info("[UAE-CI] Context Intelligence Layer initialized with Learning DB integration")

    def _load_knowledge_base(self):
        """Load knowledge base from disk"""
        try:
            if self.knowledge_base_path.exists():
                with open(self.knowledge_base_path, 'r') as f:
                    data = json.load(f)

                # Reconstruct patterns
                for elem_id, pattern_data in data.get('patterns', {}).items():
                    self.element_patterns[elem_id] = ContextualData(**pattern_data)

                # Reconstruct priorities
                for elem_id, priority_data in data.get('priorities', {}).items():
                    self.element_priorities[elem_id] = ElementPriority(**priority_data)

                logger.info(f"[UAE-CI] Loaded {len(self.element_patterns)} patterns")
        except Exception as e:
            logger.error(f"[UAE-CI] Error loading knowledge base: {e}")

    def _save_knowledge_base(self):
        """Save knowledge base to disk"""
        try:
            data = {
                'patterns': {
                    eid: asdict(pattern)
                    for eid, pattern in self.element_patterns.items()
                },
                'priorities': {
                    eid: asdict(priority)
                    for eid, priority in self.element_priorities.items()
                },
                'metrics': self.metrics,
                'last_updated': time.time()
            }

            with open(self.knowledge_base_path, 'w') as f:
                json.dump(data, f, indent=2)

        except Exception as e:
            logger.error(f"[UAE-CI] Error saving knowledge base: {e}")

    async def initialize_db(self, learning_db: JARVISLearningDatabase):
        """Initialize Learning Database connection"""
        self.learning_db = learning_db
        self.db_initialized = True

        # Load patterns from database
        await self._load_patterns_from_db()

        logger.info("[UAE-CI] Learning Database initialized and patterns loaded")

    async def _load_patterns_from_db(self):
        """Load patterns from Learning Database"""
        if not self.learning_db:
            return

        try:
            # Get display patterns from DB
            async with self.learning_db.db.cursor() as cursor:
                await cursor.execute("""
                    SELECT display_name, context, frequency, consecutive_successes
                    FROM display_patterns
                    WHERE frequency >= 2
                    ORDER BY frequency DESC
                    LIMIT 50
                """)
                rows = await cursor.fetchall()

                for row in rows:
                    element_id = row['display_name']
                    # Create pattern from DB data
                    if element_id not in self.element_patterns:
                        self.element_patterns[element_id] = ContextualData(
                            element_id=element_id,
                            expected_position=None,  # Will be learned
                            confidence=min(row['frequency'] / 10.0, 0.9),
                            usage_count=row['frequency'],
                            last_success=time.time(),
                            pattern_strength=min(row['consecutive_successes'] / 5.0, 0.95),
                            metadata={'source': 'learning_db'}
                        )

                logger.info(f"[UAE-CI] Loaded {len(rows)} patterns from Learning Database")

        except Exception as e:
            logger.error(f"[UAE-CI] Error loading patterns from DB: {e}")

    async def get_contextual_data(self, element_id: str) -> Optional[ContextualData]:
        """
        Get contextual data for element with Learning DB fallback

        Args:
            element_id: Element identifier

        Returns:
            Contextual data or None
        """
        # Check in-memory cache first
        if element_id in self.element_patterns:
            pattern = self.element_patterns[element_id]

            # Calculate current confidence based on pattern strength and recency
            age_hours = (time.time() - pattern.last_success) / 3600
            recency_factor = np.exp(-age_hours / 48)  # Decay over 48 hours
            pattern.confidence = pattern.pattern_strength * recency_factor

            self.metrics['total_predictions'] += 1
            return pattern

        # Fallback to Learning Database
        if self.learning_db:
            try:
                self.metrics['db_retrievals'] += 1

                # Check display patterns
                async with self.learning_db.db.cursor() as cursor:
                    await cursor.execute("""
                        SELECT * FROM display_patterns
                        WHERE display_name = ?
                        ORDER BY frequency DESC
                        LIMIT 1
                    """, (element_id,))
                    row = await cursor.fetchone()

                    if row:
                        # Create pattern from DB
                        pattern = ContextualData(
                            element_id=element_id,
                            expected_position=None,
                            confidence=min(row['frequency'] / 10.0, 0.8),
                            usage_count=row['frequency'],
                            last_success=time.time(),
                            pattern_strength=min(row['consecutive_successes'] / 5.0, 0.9),
                            metadata={'source': 'learning_db', 'auto_connect': bool(row['auto_connect'])}
                        )

                        # Cache it
                        self.element_patterns[element_id] = pattern

                        logger.info(f"[UAE-CI] Retrieved pattern for {element_id} from Learning DB")
                        return pattern

            except Exception as e:
                logger.error(f"[UAE-CI] Error retrieving from Learning DB: {e}")

        return None

    async def update_pattern(
        self,
        element_id: str,
        position: Tuple[int, int],
        success: bool,
        metadata: Optional[Dict[str, Any]] = None
    ):
        """
        Update learned pattern for element and store in Learning DB

        Args:
            element_id: Element identifier
            position: Observed position
            success: Whether action succeeded
            metadata: Additional context
        """
        if element_id not in self.element_patterns:
            # Create new pattern
            self.element_patterns[element_id] = ContextualData(
                element_id=element_id,
                expected_position=position,
                confidence=0.5,
                usage_count=1,
                last_success=time.time(),
                pattern_strength=0.5,
                metadata=metadata or {}
            )
        else:
            # Update existing pattern
            pattern = self.element_patterns[element_id]
            pattern.usage_count += 1

            if success:
                # Strengthen pattern
                if pattern.expected_position == position:
                    # Same position - strengthen
                    pattern.pattern_strength = min(pattern.pattern_strength + 0.1, 1.0)
                else:
                    # Position changed - update but weaken slightly
                    pattern.expected_position = position
                    pattern.pattern_strength = max(pattern.pattern_strength - 0.05, 0.3)

                pattern.last_success = time.time()
                self.metrics['successful_predictions'] += 1
            else:
                # Weaken pattern on failure
                pattern.pattern_strength = max(pattern.pattern_strength - 0.15, 0.1)

            if metadata:
                pattern.metadata.update(metadata)

        self.metrics['pattern_updates'] += 1
        self._save_knowledge_base()

        # Store in Learning Database
        if self.learning_db:
            await self._store_pattern_in_db(element_id, position, success, metadata)

        logger.debug(
            f"[UAE-CI] Updated pattern for {element_id}: "
            f"pos={position}, strength={self.element_patterns[element_id].pattern_strength:.2f}"
        )

    async def _store_pattern_in_db(
        self,
        element_id: str,
        position: Tuple[int, int],
        success: bool,
        metadata: Optional[Dict[str, Any]] = None
    ):
        """Store pattern in Learning Database"""
        try:
            self.metrics['db_stores'] += 1

            # Store as display pattern
            context = metadata or {}
            context['position'] = position

            await self.learning_db.learn_display_pattern(
                display_name=element_id,
                context=context
            )

            # Store as general pattern
            pattern_data = {
                'pattern_type': PatternType.CONTEXTUAL.value,
                'pattern_data': {
                    'element_id': element_id,
                    'position': position,
                    'success': success,
                    'timestamp': datetime.now().isoformat()
                },
                'confidence': self.element_patterns[element_id].pattern_strength,
                'success_rate': 1.0 if success else 0.0,
                'metadata': metadata or {}
            }

            await self.learning_db.store_pattern(pattern_data, auto_merge=True)

            logger.debug(f"[UAE-CI] Stored pattern for {element_id} in Learning DB")

        except Exception as e:
            logger.error(f"[UAE-CI] Error storing pattern in Learning DB: {e}")

    async def get_priority_elements(self, top_n: int = 10) -> List[str]:
        """
        Get priority elements for monitoring

        Args:
            top_n: Number of top priority elements

        Returns:
            List of element IDs sorted by priority
        """
        # Update all priorities
        for elem_id, pattern in self.element_patterns.items():
            if elem_id not in self.element_priorities:
                # Calculate initial priority
                success_rate = self._get_success_rate(elem_id)
                self.element_priorities[elem_id] = ElementPriority(
                    element_id=elem_id,
                    priority_score=0.0,
                    usage_frequency=pattern.usage_count,
                    last_used=pattern.last_success,
                    importance=0.5,  # Default importance
                    failure_rate=1.0 - success_rate
                )

            # Recalculate priority
            self.element_priorities[elem_id].calculate_priority()

        # Sort by priority
        sorted_elements = sorted(
            self.element_priorities.values(),
            key=lambda p: p.priority_score,
            reverse=True
        )

        return [p.element_id for p in sorted_elements[:top_n]]

    def _get_success_rate(self, element_id: str) -> float:
        """Calculate success rate for element"""
        if element_id not in self.success_history:
            return 0.5  # Default

        history = self.success_history[element_id]
        if not history:
            return 0.5

        return sum(history) / len(history)

    async def learn_from_execution(self, result: ExecutionResult):
        """
        Learn from execution result and store in Learning DB

        Args:
            result: Execution result
        """
        element_id = result.decision.element_id
        position = result.decision.chosen_position

        # Update success history
        self.success_history[element_id].append(1.0 if result.success else 0.0)

        # Update pattern
        await self.update_pattern(
            element_id,
            position,
            result.success,
            metadata={
                'source': result.decision.decision_source.value,
                'verification': result.verification_passed,
                'execution_time': result.execution_time
            }
        )

        # Update priority failure rate
        if element_id in self.element_priorities:
            self.element_priorities[element_id].failure_rate = 1.0 - self._get_success_rate(element_id)

        # Store action in Learning DB
        if self.learning_db:
            try:
                action_data = {
                    'action_type': 'click_element',
                    'target': element_id,
                    'confidence': result.decision.confidence,
                    'success': result.success,
                    'execution_time': result.execution_time,
                    'params': {
                        'position': position,
                        'decision_source': result.decision.decision_source.value
                    },
                    'result': {
                        'verification_passed': result.verification_passed,
                        'error': result.error
                    }
                }

                await self.learning_db.store_action(action_data, batch=False)

                logger.debug(f"[UAE-CI] Stored action for {element_id} in Learning DB")

            except Exception as e:
                logger.error(f"[UAE-CI] Error storing action in Learning DB: {e}")

        self.metrics['learning_events'] += 1

    def get_metrics(self) -> Dict[str, Any]:
        """Get CI metrics"""
        total = self.metrics['total_predictions']
        success = self.metrics['successful_predictions']

        return {
            **self.metrics,
            'prediction_accuracy': success / total if total > 0 else 0.0,
            'patterns_learned': len(self.element_patterns),
            'priority_elements': len(self.element_priorities)
        }


# ============================================================================
# Situational Awareness Layer
# ============================================================================

class SituationalAwarenessLayer:
    """
    Real-time perception and environmental monitoring

    Wraps SAI engine with UAE-specific interface
    """

    def __init__(self, sai_engine: Optional[SituationalAwarenessEngine] = None):
        self.sai_engine = sai_engine
        self.detection_cache: Dict[str, SituationalData] = {}
        self.monitoring_active = False

        # Metrics
        self.metrics = {
            'detections': 0,
            'cache_hits': 0,
            'cache_misses': 0,
            'environment_changes': 0
        }

        logger.info("[UAE-SAL] Situational Awareness Layer initialized")

    def set_sai_engine(self, sai_engine: SituationalAwarenessEngine):
        """Set SAI engine"""
        self.sai_engine = sai_engine
        logger.info("[UAE-SAL] SAI engine connected")

    async def get_situational_data(
        self,
        element_id: str,
        force_detect: bool = False
    ) -> Optional[SituationalData]:
        """
        Get real-time situational data for element

        Args:
            element_id: Element identifier
            force_detect: Force new detection even if cached

        Returns:
            Situational data or None
        """
        if not self.sai_engine:
            logger.warning("[UAE-SAL] SAI engine not available")
            return None

        # Check cache first
        if not force_detect and element_id in self.detection_cache:
            cached = self.detection_cache[element_id]
            # Check if cache is fresh (< 30 seconds old)
            if time.time() - cached.detection_time < 30:
                self.metrics['cache_hits'] += 1
                return cached

        # Detect using SAI
        self.metrics['cache_misses'] += 1
        position = await self.sai_engine.get_element_position(
            element_id,
            use_cache=True,
            force_detect=force_detect
        )

        if position:
            situational_data = SituationalData(
                element_id=element_id,
                detected_position=position.coordinates,
                confidence=position.confidence,
                detection_method=position.detection_method,
                detection_time=position.timestamp,
                visual_hash=position.visual_hash
            )

            # Cache result
            self.detection_cache[element_id] = situational_data
            self.metrics['detections'] += 1

            return situational_data

        return None

    async def start_monitoring(self, priority_elements: List[str]):
        """
        Start monitoring priority elements

        Args:
            priority_elements: Elements to monitor
        """
        if not self.sai_engine:
            logger.warning("[UAE-SAL] Cannot start monitoring without SAI engine")
            return

        logger.info(f"[UAE-SAL] Starting monitoring of {len(priority_elements)} priority elements")
        self.monitoring_active = True

        # Start SAI monitoring
        if not self.sai_engine.is_monitoring:
            await self.sai_engine.start_monitoring()

    async def stop_monitoring(self):
        """Stop monitoring"""
        if self.sai_engine and self.sai_engine.is_monitoring:
            await self.sai_engine.stop_monitoring()

        self.monitoring_active = False
        logger.info("[UAE-SAL] Monitoring stopped")

    def on_environment_change(self, change: ChangeEvent):
        """Handle environment change from SAI"""
        self.metrics['environment_changes'] += 1

        # Invalidate cache for affected elements
        if change.element_id and change.element_id in self.detection_cache:
            del self.detection_cache[change.element_id]
            logger.info(f"[UAE-SAL] Invalidated cache for {change.element_id} due to {change.change_type.value}")

    def get_metrics(self) -> Dict[str, Any]:
        """Get SAL metrics"""
        total_requests = self.metrics['cache_hits'] + self.metrics['cache_misses']
        cache_hit_rate = self.metrics['cache_hits'] / total_requests if total_requests > 0 else 0.0

        return {
            **self.metrics,
            'cache_hit_rate': cache_hit_rate,
            'monitoring_active': self.monitoring_active
        }


# ============================================================================
# Awareness Integration Layer
# ============================================================================

class AwarenessIntegrationLayer:
    """
    Decision fusion engine combining context and situation

    Makes confidence-weighted decisions by balancing:
    - Historical patterns (context)
    - Real-time perception (situation)
    - Recency vs consistency tradeoffs
    """

    def __init__(self):
        self.decision_history: deque = deque(maxlen=1000)

        # Configuration
        self.config = {
            'context_base_weight': 0.4,
            'situation_base_weight': 0.6,
            'recency_threshold': 60,  # seconds
            'consistency_threshold': 0.8,
            'min_confidence': 0.5
        }

        # Metrics
        self.metrics = {
            'decisions_made': 0,
            'context_chosen': 0,
            'situation_chosen': 0,
            'fusion_chosen': 0,
            'fallback_chosen': 0
        }

        logger.info("[UAE-AIL] Awareness Integration Layer initialized")

    async def make_decision(
        self,
        element_id: str,
        context_data: Optional[ContextualData],
        situation_data: Optional[SituationalData]
    ) -> UnifiedDecision:
        """
        Make unified decision by fusing context and situation

        Args:
            element_id: Element identifier
            context_data: Data from context intelligence
            situation_data: Data from situational awareness

        Returns:
            Unified decision
        """
        self.metrics['decisions_made'] += 1

        # Case 1: Only context available
        if context_data and not situation_data:
            decision = self._decide_from_context(element_id, context_data)
            self.metrics['context_chosen'] += 1

        # Case 2: Only situation available
        elif situation_data and not context_data:
            decision = self._decide_from_situation(element_id, situation_data)
            self.metrics['situation_chosen'] += 1

        # Case 3: Both available - fusion
        elif context_data and situation_data:
            decision = await self._decide_from_fusion(element_id, context_data, situation_data)
            self.metrics['fusion_chosen'] += 1

        # Case 4: Neither available - fallback
        else:
            decision = self._decide_fallback(element_id)
            self.metrics['fallback_chosen'] += 1

        # Record decision
        self.decision_history.append(decision)

        return decision

    def _decide_from_context(
        self,
        element_id: str,
        context_data: ContextualData
    ) -> UnifiedDecision:
        """Decision based only on context"""
        return UnifiedDecision(
            element_id=element_id,
            chosen_position=context_data.expected_position,
            confidence=context_data.confidence,
            decision_source=DecisionSource.CONTEXT,
            context_weight=1.0,
            situation_weight=0.0,
            reasoning="Only context data available",
            timestamp=time.time(),
            metadata={'pattern_strength': context_data.pattern_strength}
        )

    def _decide_from_situation(
        self,
        element_id: str,
        situation_data: SituationalData
    ) -> UnifiedDecision:
        """Decision based only on situation"""
        return UnifiedDecision(
            element_id=element_id,
            chosen_position=situation_data.detected_position,
            confidence=situation_data.confidence,
            decision_source=DecisionSource.SITUATION,
            context_weight=0.0,
            situation_weight=1.0,
            reasoning="Only situational data available",
            timestamp=time.time(),
            metadata={'detection_method': situation_data.detection_method}
        )

    async def _decide_from_fusion(
        self,
        element_id: str,
        context_data: ContextualData,
        situation_data: SituationalData
    ) -> UnifiedDecision:
        """
        Fusion decision weighing both sources

        Strategy:
        1. If positions agree → high confidence
        2. If positions disagree:
           - Prefer situation if very recent (< 60s)
           - Prefer context if highly consistent pattern
           - Otherwise weighted combination
        """
        positions_agree = (
            context_data.expected_position == situation_data.detected_position
        )

        if positions_agree:
            # Positions agree - high confidence
            combined_confidence = (
                context_data.confidence * 0.4 +
                situation_data.confidence * 0.6
            )

            return UnifiedDecision(
                element_id=element_id,
                chosen_position=situation_data.detected_position,
                confidence=min(combined_confidence * 1.2, 1.0),  # Boost for agreement
                decision_source=DecisionSource.FUSION,
                context_weight=0.4,
                situation_weight=0.6,
                reasoning="Context and situation agree",
                timestamp=time.time(),
                metadata={
                    'agreement': True,
                    'context_confidence': context_data.confidence,
                    'situation_confidence': situation_data.confidence
                }
            )

        else:
            # Positions disagree - need to choose
            situation_age = time.time() - situation_data.detection_time

            # Prefer situation if very recent
            if situation_age < self.config['recency_threshold']:
                return UnifiedDecision(
                    element_id=element_id,
                    chosen_position=situation_data.detected_position,
                    confidence=situation_data.confidence,
                    decision_source=DecisionSource.SITUATION,
                    context_weight=0.2,
                    situation_weight=0.8,
                    reasoning=f"Situation is very recent ({situation_age:.0f}s old)",
                    timestamp=time.time(),
                    metadata={'disagreement_reason': 'recency_priority'}
                )

            # Prefer context if highly consistent pattern
            elif context_data.pattern_strength > self.config['consistency_threshold']:
                return UnifiedDecision(
                    element_id=element_id,
                    chosen_position=context_data.expected_position,
                    confidence=context_data.confidence,
                    decision_source=DecisionSource.CONTEXT,
                    context_weight=0.8,
                    situation_weight=0.2,
                    reasoning=f"Context has strong pattern ({context_data.pattern_strength:.2f})",
                    timestamp=time.time(),
                    metadata={'disagreement_reason': 'pattern_priority'}
                )

            # Otherwise weighted combination based on confidence
            else:
                # Weight by confidence
                total_confidence = context_data.confidence + situation_data.confidence
                context_weight = context_data.confidence / total_confidence
                situation_weight = situation_data.confidence / total_confidence

                # Choose higher confidence source
                if situation_data.confidence > context_data.confidence:
                    chosen_position = situation_data.detected_position
                    chosen_confidence = situation_data.confidence
                else:
                    chosen_position = context_data.expected_position
                    chosen_confidence = context_data.confidence

                return UnifiedDecision(
                    element_id=element_id,
                    chosen_position=chosen_position,
                    confidence=chosen_confidence,
                    decision_source=DecisionSource.FUSION,
                    context_weight=context_weight,
                    situation_weight=situation_weight,
                    reasoning="Weighted by confidence (positions disagree)",
                    timestamp=time.time(),
                    metadata={
                        'disagreement': True,
                        'context_pos': context_data.expected_position,
                        'situation_pos': situation_data.detected_position
                    }
                )

    def _decide_fallback(self, element_id: str) -> UnifiedDecision:
        """Fallback decision when no data available"""
        return UnifiedDecision(
            element_id=element_id,
            chosen_position=(0, 0),  # Invalid position
            confidence=0.0,
            decision_source=DecisionSource.FALLBACK,
            context_weight=0.0,
            situation_weight=0.0,
            reasoning="No context or situational data available",
            timestamp=time.time(),
            metadata={'error': 'no_data_available'}
        )

    def get_metrics(self) -> Dict[str, Any]:
        """Get AIL metrics"""
        total = self.metrics['decisions_made']

        return {
            **self.metrics,
            'context_ratio': self.metrics['context_chosen'] / total if total > 0 else 0.0,
            'situation_ratio': self.metrics['situation_chosen'] / total if total > 0 else 0.0,
            'fusion_ratio': self.metrics['fusion_chosen'] / total if total > 0 else 0.0,
            'fallback_ratio': self.metrics['fallback_chosen'] / total if total > 0 else 0.0
        }


# ============================================================================
# Unified Awareness Engine (Main Orchestrator)
# ============================================================================

class UnifiedAwarenessEngine:
    """
    Main orchestrator combining Context Intelligence, Situational Awareness, and Learning Database

    Implements the complete awareness loop with persistent memory:
    1. Analyze context (historical patterns from Learning DB)
    2. Perceive situation (real-time state from SAI)
    3. Fuse decisions (weighted combination)
    4. Execute action
    5. Learn from result (feedback loop + DB storage)

    Features:
    - Bidirectional learning with persistence
    - Confidence-weighted decisions
    - Priority-based monitoring
    - Self-healing intelligence
    - Continuous adaptation
    - Temporal pattern recognition
    - Cross-session memory
    """

    def __init__(
        self,
        sai_engine: Optional[SituationalAwarenessEngine] = None,
        vision_analyzer=None,
        learning_db: Optional[JARVISLearningDatabase] = None,
        multi_space_handler=None
    ):
        """
        Initialize UAE with Learning Database

        Args:
            sai_engine: Situational Awareness Engine
            vision_analyzer: Claude Vision analyzer
            learning_db: Learning Database instance
            multi_space_handler: MultiSpaceQueryHandler for cross-space intelligence
        """
        # Learning Database
        self.learning_db = learning_db

        # Multi-space intelligence
        self.multi_space_handler = multi_space_handler

        # Core layers
        self.context_layer = ContextIntelligenceLayer(learning_db=learning_db)
        self.situation_layer = SituationalAwarenessLayer(sai_engine)
        self.integration_layer = AwarenessIntegrationLayer()

        # Vision analyzer
        self.vision_analyzer = vision_analyzer

        # State
        self.is_active = False
        self.monitoring_task: Optional[asyncio.Task] = None

        # Execution history
        self.execution_history: deque = deque(maxlen=500)

        # Callbacks
        self.decision_callbacks: List[Callable] = []
        self.learning_callbacks: List[Callable] = []

        # Metrics
        self.metrics = {
            'total_executions': 0,
            'successful_executions': 0,
            'failed_executions': 0,
            'learning_cycles': 0,
            'adaptations': 0,
            'db_active': learning_db is not None,
            'multi_space_queries': 0
        }

        logger.info("[UAE] Unified Awareness Engine initialized with Learning Database integration")
        if self.multi_space_handler:
            logger.info("[UAE] Multi-space intelligence integration enabled")

    async def start(self):
        """Start UAE system"""
        if self.is_active:
            logger.warning("[UAE] Already active")
            return

        logger.info("[UAE] Starting Unified Awareness Engine...")

        # Get priority elements from context
        priority_elements = await self.context_layer.get_priority_elements(top_n=10)

        # Start situational monitoring
        await self.situation_layer.start_monitoring(priority_elements)

        # Register SAI change callback
        if self.situation_layer.sai_engine:
            self.situation_layer.sai_engine.register_change_callback(
                self.situation_layer.on_environment_change
            )

        self.is_active = True
        logger.info(f"[UAE] ✅ Active - monitoring {len(priority_elements)} priority elements")

    async def stop(self):
        """Stop UAE system"""
        if not self.is_active:
            return

        logger.info("[UAE] Stopping...")

        await self.situation_layer.stop_monitoring()

        self.is_active = False
        logger.info("[UAE] ✅ Stopped")

    async def get_element_position(
        self,
        element_id: str,
        force_detect: bool = False
    ) -> UnifiedDecision:
        """
        Get element position using unified awareness

        Args:
            element_id: Element identifier
            force_detect: Force new situational detection

        Returns:
            Unified decision with chosen position
        """
        logger.info(f"[UAE] Getting position for: {element_id}")

        # Step 1: Get context data
        context_data = await self.context_layer.get_contextual_data(element_id)
        if context_data:
            logger.debug(
                f"[UAE] Context: pos={context_data.expected_position}, "
                f"conf={context_data.confidence:.2f}"
            )

        # Step 2: Get situational data
        situation_data = await self.situation_layer.get_situational_data(
            element_id,
            force_detect=force_detect
        )
        if situation_data:
            logger.debug(
                f"[UAE] Situation: pos={situation_data.detected_position}, "
                f"conf={situation_data.confidence:.2f}"
            )

        # Step 3: Make unified decision
        decision = await self.integration_layer.make_decision(
            element_id,
            context_data,
            situation_data
        )

        logger.info(
            f"[UAE] Decision: {decision.decision_source.value} → "
            f"{decision.chosen_position} (conf={decision.confidence:.2f})"
        )
        logger.info(f"[UAE] Reasoning: {decision.reasoning}")

        # Trigger callbacks
        for callback in self.decision_callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(decision)
                else:
                    callback(decision)
            except Exception as e:
                logger.error(f"[UAE] Error in decision callback: {e}")

        return decision

    async def execute_and_learn(
        self,
        decision: UnifiedDecision,
        executor: Callable,
        **executor_kwargs
    ) -> ExecutionResult:
        """
        Execute decision and learn from result

        Args:
            decision: Decision to execute
            executor: Async function to execute
            **executor_kwargs: Arguments for executor

        Returns:
            Execution result
        """
        start_time = time.time()

        try:
            # Execute
            logger.info(f"[UAE] Executing decision for {decision.element_id}...")
            result = await executor(
                target=decision.element_id,
                coordinates=decision.chosen_position,
                **executor_kwargs
            )

            # Create execution result
            exec_result = ExecutionResult(
                decision=decision,
                success=result.get('success', False),
                execution_time=time.time() - start_time,
                verification_passed=result.get('verification_passed', False),
                metadata=result
            )

            # Update metrics
            self.metrics['total_executions'] += 1
            if exec_result.success:
                self.metrics['successful_executions'] += 1
            else:
                self.metrics['failed_executions'] += 1

            # Learn from result
            await self._learn_from_execution(exec_result)

            # Store in history
            self.execution_history.append(exec_result)

            return exec_result

        except Exception as e:
            logger.error(f"[UAE] Execution failed: {e}", exc_info=True)

            exec_result = ExecutionResult(
                decision=decision,
                success=False,
                execution_time=time.time() - start_time,
                verification_passed=False,
                error=str(e)
            )

            self.metrics['failed_executions'] += 1
            return exec_result

    async def _learn_from_execution(self, result: ExecutionResult):
        """
        Bidirectional learning from execution result

        Updates both context and situational layers
        """
        logger.info(f"[UAE] Learning from execution: {result.success}")

        # Update context layer
        await self.context_layer.learn_from_execution(result)

        # Update situational layer cache if needed
        if result.success and result.verification_passed:
            # Successful execution confirms position
            self.situation_layer.detection_cache[result.decision.element_id] = SituationalData(
                element_id=result.decision.element_id,
                detected_position=result.decision.chosen_position,
                confidence=1.0,  # Verified
                detection_method="verified_execution",
                detection_time=time.time()
            )

        self.metrics['learning_cycles'] += 1

        # Trigger learning callbacks
        for callback in self.learning_callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(result)
                else:
                    callback(result)
            except Exception as e:
                logger.error(f"[UAE] Error in learning callback: {e}")

    def register_decision_callback(self, callback: Callable):
        """Register callback for decisions"""
        self.decision_callbacks.append(callback)

    def register_learning_callback(self, callback: Callable):
        """Register callback for learning events"""
        self.learning_callbacks.append(callback)

    # =========================================================================
    # Computer Use Action Routing
    # =========================================================================

    async def route_to_computer_use(
        self,
        goal: str,
        context: Optional[Dict[str, Any]] = None,
        narrate: bool = True,
        use_position_hints: bool = True
    ) -> Dict[str, Any]:
        """
        Route an action request to the Computer Use system.

        This method bridges UAE awareness with Computer Use execution,
        providing intelligent position hints and context.

        Args:
            goal: Natural language goal to achieve
            context: Additional context for the task
            narrate: Whether to enable voice narration
            use_position_hints: Whether to provide UAE position hints

        Returns:
            Result dictionary from Computer Use execution
        """
        logger.info(f"[UAE] Routing to Computer Use: {goal}")

        # Lazy import to avoid circular dependencies
        try:
            from backend.autonomy.computer_use_tool import (
                get_computer_use_tool,
                ComputerUseResult,
            )
        except ImportError:
            try:
                from autonomy.computer_use_tool import (
                    get_computer_use_tool,
                    ComputerUseResult,
                )
            except ImportError:
                logger.error("[UAE] Computer Use Tool not available")
                return {
                    "success": False,
                    "error": "Computer Use Tool not available",
                    "goal": goal
                }

        # Build context with UAE hints
        full_context = context.copy() if context else {}

        # Add UAE position hints if available and enabled
        if use_position_hints:
            # Extract potential element targets from goal
            element_hints = await self._extract_element_hints(goal)
            if element_hints:
                full_context["uae_element_hints"] = element_hints
                logger.info(f"[UAE] Providing {len(element_hints)} position hints")

        # Add multi-space context if available
        if self.multi_space_handler:
            try:
                multi_space_context = await self._get_multi_space_context(goal)
                if multi_space_context:
                    full_context["multi_space"] = multi_space_context
                    self.metrics['multi_space_queries'] += 1
            except Exception as e:
                logger.debug(f"[UAE] Multi-space context error: {e}")

        # Get Computer Use Tool
        tool = get_computer_use_tool()

        # Execute via Computer Use
        try:
            result = await tool.run(
                goal=goal,
                context=full_context,
                narrate=narrate,
                use_uae=True,  # Use UAE internally too
            )

            # Learn from result
            if result.success:
                await self._learn_from_computer_use_result(goal, result, full_context)

            return {
                "success": result.success,
                "goal": goal,
                "status": result.status,
                "final_message": result.final_message,
                "actions_count": result.actions_count,
                "duration_ms": result.total_duration_ms,
                "confidence": result.confidence,
                "learning_insights": result.learning_insights,
            }

        except Exception as e:
            logger.error(f"[UAE] Computer Use execution failed: {e}", exc_info=True)
            return {
                "success": False,
                "goal": goal,
                "error": str(e)
            }

    async def _extract_element_hints(self, goal: str) -> Dict[str, Any]:
        """
        Extract element hints from goal using context layer.

        Args:
            goal: Natural language goal

        Returns:
            Dictionary of element hints with positions
        """
        hints = {}

        # Common UI element keywords
        element_keywords = [
            "button", "menu", "icon", "window", "app", "finder", "safari",
            "chrome", "terminal", "settings", "preferences", "control center",
            "dock", "notification", "spotlight", "launchpad"
        ]

        goal_lower = goal.lower()

        for keyword in element_keywords:
            if keyword in goal_lower:
                # Try to get position from context layer
                element_id = keyword.replace(" ", "_")
                context_data = await self.context_layer.get_contextual_data(element_id)

                if context_data and context_data.expected_position:
                    hints[element_id] = {
                        "position": context_data.expected_position,
                        "confidence": context_data.confidence,
                        "source": "context_layer"
                    }

        return hints

    async def _get_multi_space_context(self, goal: str) -> Optional[Dict[str, Any]]:
        """
        Get multi-space context for goal.

        Args:
            goal: Natural language goal

        Returns:
            Multi-space context or None
        """
        if not self.multi_space_handler:
            return None

        try:
            # Check if goal involves other spaces
            space_keywords = ["other desktop", "other space", "desktop 2", "space 2",
                            "different desktop", "switch to", "all desktops"]

            goal_lower = goal.lower()
            involves_multi_space = any(kw in goal_lower for kw in space_keywords)

            if involves_multi_space:
                # Query multi-space handler
                return {
                    "involves_multi_space": True,
                    "goal_keywords": [kw for kw in space_keywords if kw in goal_lower]
                }

            return None

        except Exception as e:
            logger.debug(f"[UAE] Multi-space context error: {e}")
            return None

    async def _learn_from_computer_use_result(
        self,
        goal: str,
        result: Any,
        context: Dict[str, Any]
    ):
        """
        Learn from successful Computer Use execution.

        Args:
            goal: Original goal
            result: ComputerUseResult
            context: Context used
        """
        try:
            # Store learning insights
            if result.learning_insights and self.learning_db:
                for insight in result.learning_insights:
                    pattern_data = {
                        'pattern_type': 'computer_use_insight',
                        'pattern_data': {
                            'goal': goal,
                            'insight': insight,
                            'timestamp': time.time()
                        },
                        'confidence': result.confidence,
                        'success_rate': 1.0 if result.success else 0.0
                    }

                    await self.learning_db.store_pattern(pattern_data, auto_merge=True)

            self.metrics['learning_cycles'] += 1
            logger.debug(f"[UAE] Learned from Computer Use: {len(result.learning_insights)} insights")

        except Exception as e:
            logger.error(f"[UAE] Error learning from Computer Use: {e}")

    async def execute_action(
        self,
        action_type: str,
        target: str,
        parameters: Optional[Dict[str, Any]] = None,
        use_computer_use: bool = True,
        narrate: bool = True
    ) -> Dict[str, Any]:
        """
        Execute an action using the most appropriate method.

        This is the primary action routing method that decides whether
        to use Computer Use or direct execution.

        Args:
            action_type: Type of action (click, type, navigate, etc.)
            target: Target element or description
            parameters: Additional parameters
            use_computer_use: Whether to use Computer Use system
            narrate: Whether to enable voice narration

        Returns:
            Action result
        """
        # Build goal description from action
        if action_type == "click":
            goal = f"Click on {target}"
        elif action_type == "type":
            text = parameters.get("text", "") if parameters else ""
            goal = f"Type '{text}' in {target}"
        elif action_type == "navigate":
            goal = f"Navigate to {target}"
        elif action_type == "open":
            goal = f"Open {target}"
        elif action_type == "close":
            goal = f"Close {target}"
        else:
            goal = f"{action_type} {target}"

        # Add parameters to context
        context = parameters or {}
        context["action_type"] = action_type
        context["target"] = target

        if use_computer_use:
            return await self.route_to_computer_use(
                goal=goal,
                context=context,
                narrate=narrate
            )
        else:
            # Direct element execution (legacy path)
            decision = await self.get_element_position(target)
            if decision.confidence < 0.5:
                return {
                    "success": False,
                    "error": f"Low confidence for element: {target}",
                    "confidence": decision.confidence
                }

            # Execute with existing executor
            return {
                "success": True,
                "position": decision.chosen_position,
                "confidence": decision.confidence,
                "message": "Position resolved - execute action"
            }

    def get_comprehensive_metrics(self) -> Dict[str, Any]:
        """Get comprehensive metrics from all layers"""
        total_exec = self.metrics['total_executions']
        success_rate = (
            self.metrics['successful_executions'] / total_exec
            if total_exec > 0 else 0.0
        )

        return {
            'engine': {
                **self.metrics,
                'success_rate': success_rate,
                'active': self.is_active
            },
            'context_layer': self.context_layer.get_metrics(),
            'situation_layer': self.situation_layer.get_metrics(),
            'integration_layer': self.integration_layer.get_metrics()
        }


# ============================================================================
# Singleton Instance
# ============================================================================

_uae_instance: Optional[UnifiedAwarenessEngine] = None


def get_uae_engine(
    sai_engine: Optional[SituationalAwarenessEngine] = None,
    vision_analyzer=None,
    learning_db: Optional[JARVISLearningDatabase] = None,
    multi_space_handler=None
) -> UnifiedAwarenessEngine:
    """
    Get singleton UAE engine with Learning Database

    Args:
        sai_engine: SAI engine instance
        vision_analyzer: Vision analyzer
        learning_db: Learning Database instance
        multi_space_handler: MultiSpaceQueryHandler for cross-space intelligence

    Returns:
        UnifiedAwarenessEngine instance
    """
    global _uae_instance

    if _uae_instance is None:
        # Create SAI engine if not provided
        if sai_engine is None:
            sai_engine = get_sai_engine(
                vision_analyzer=vision_analyzer,
                monitoring_interval=5.0,  # Enhanced 24/7 mode: 5s interval
                multi_space_handler=multi_space_handler
            )

        _uae_instance = UnifiedAwarenessEngine(
            sai_engine=sai_engine,
            vision_analyzer=vision_analyzer,
            learning_db=learning_db,
            multi_space_handler=multi_space_handler
        )
    else:
        # Update SAI engine if provided
        if sai_engine is not None:
            _uae_instance.situation_layer.set_sai_engine(sai_engine)

        # Update Learning DB if provided
        if learning_db is not None and _uae_instance.learning_db is None:
            _uae_instance.learning_db = learning_db
            _uae_instance.context_layer.learning_db = learning_db
            _uae_instance.metrics['db_active'] = True
            logger.info("[UAE] Learning Database connected to existing UAE instance")

        # Update multi-space handler if provided
        if multi_space_handler is not None and _uae_instance.multi_space_handler is None:
            _uae_instance.multi_space_handler = multi_space_handler
            logger.info("[UAE] Multi-space intelligence connected to existing UAE instance")

    return _uae_instance


# ============================================================================
# Example Usage
# ============================================================================

async def main():
    """Example UAE usage"""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    print("\n" + "=" * 80)
    print("Unified Awareness Engine (UAE) - Demo")
    print("=" * 80)

    # Initialize UAE
    uae = get_uae_engine()

    print("\n✅ UAE initialized")
    print("   Context Intelligence: Loaded")
    print("   Situational Awareness: Loaded")
    print("   Integration Layer: Ready")

    # Start UAE
    await uae.start()

    print("\n🚀 UAE started - monitoring priority elements")

    # Simulate getting element position
    print("\n🎯 Getting position for 'control_center'...")
    decision = await uae.get_element_position("control_center")

    print(f"\n📊 Decision:")
    print(f"   Source: {decision.decision_source.value}")
    print(f"   Position: {decision.chosen_position}")
    print(f"   Confidence: {decision.confidence:.2%}")
    print(f"   Reasoning: {decision.reasoning}")

    # Show metrics
    print("\n📈 Metrics:")
    metrics = uae.get_comprehensive_metrics()
    print(json.dumps(metrics, indent=2, default=str))

    # Stop UAE
    await uae.stop()

    print("\n" + "=" * 80)
    print("✅ Demo complete!")
    print("=" * 80)


if __name__ == "__main__":
    asyncio.run(main())
