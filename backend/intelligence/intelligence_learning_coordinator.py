"""
Intelligence Learning Coordinator - RAG + RLHF + Multi-Factor Intelligence

Integrates learning database with multi-factor authentication intelligence for:
- RAG (Retrieval-Augmented Generation) - Context-aware authentication decisions
- RLHF (Reinforcement Learning from Human Feedback) - Continuous improvement
- Cross-intelligence correlation learning - Pattern discovery across signals
- Predictive authentication - Anticipate unlock needs
- Adaptive threshold tuning - Self-optimizing security

Architecture:
    Learning Database (SQLite/CloudSQL)
            │
            ├─> Voice Samples + Embeddings
            ├─> Authentication History
            ├─> Multi-Factor Context Records
            └─> RLHF Feedback Scores
                        │
                        v
        Intelligence Learning Coordinator
                        │
            ┌───────────┴───────────┐
            │                       │
    RAG Retrieval Engine    RLHF Feedback Loop
            │                       │
            v                       v
    Similar Context Lookup   Continuous Improvement
            │                       │
            └───────────┬───────────┘
                        │
                        v
            Enhanced Auth Decision
            (Context + Learning)

Author: JARVIS AI Agent
Version: 5.0.0
"""

import asyncio
import logging
import os
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from enum import Enum
from typing import Dict, List, Optional, Tuple, Any
from pathlib import Path
import json

try:
    import numpy as np
    NUMPY_AVAILABLE = True
except ImportError:
    NUMPY_AVAILABLE = False
    np = None

logger = logging.getLogger(__name__)


class AuthOutcome(Enum):
    """Authentication outcome types for RLHF learning."""
    SUCCESS = "success"                  # Authenticated successfully
    FAILURE = "failure"                  # Denied - voice didn't match
    FALSE_POSITIVE = "false_positive"    # Authenticated but shouldn't have
    FALSE_NEGATIVE = "false_negative"    # Denied but should have authenticated
    CHALLENGED = "challenged"            # Required security question


@dataclass
class AuthenticationRecord:
    """Complete authentication record for learning."""
    # Basic info
    timestamp: datetime
    user_id: str
    outcome: AuthOutcome

    # Voice biometric
    voice_confidence: float
    voice_embedding: Optional[bytes]

    # Multi-factor context
    network_ssid_hash: Optional[str]
    network_trust: float
    temporal_confidence: float
    device_state: str
    device_trust: float
    drift_adjustment: float

    # Fusion results
    final_confidence: float
    risk_score: float
    decision: str  # authenticate, challenge, deny, escalate

    # Learning metadata
    was_correct: Optional[bool] = None  # User feedback
    feedback_score: Optional[float] = None  # 0.0-1.0
    feedback_notes: Optional[str] = None
    similar_context_confidence: Optional[float] = None  # RAG similarity

    # IDs for database
    record_id: Optional[int] = None
    voice_sample_id: Optional[int] = None


@dataclass
class LearningInsights:
    """Insights from learning analysis."""
    # Pattern insights
    typical_network_trust: float
    typical_temporal_confidence: float
    typical_device_state: str
    avg_successful_confidence: float

    # Predictions
    predicted_next_unlock_time: Optional[datetime]
    predicted_confidence: float
    confidence_in_prediction: float

    # RAG context
    similar_authentications: List[Dict]
    context_similarity_score: float

    # Recommendations
    should_adjust_thresholds: bool
    recommended_threshold_adjustment: float
    reasoning: List[str]


class IntelligenceLearningCoordinator:
    """
    Coordinates learning across all intelligence signals for authentication.

    Features:
    - RAG: Retrieves similar authentication contexts for informed decisions
    - RLHF: Learns from human feedback (corrections, false positives/negatives)
    - Cross-correlation: Discovers patterns across voice, network, temporal, device
    - Predictive: Anticipates unlock needs based on patterns
    - Adaptive: Self-tunes thresholds based on performance
    """

    def __init__(self, config: Optional[Dict] = None):
        self.config = config or {}

        # Learning configuration
        self.enable_rag = self.config.get('enable_rag', True)
        self.enable_rlhf = self.config.get('enable_rlhf', True)
        self.enable_prediction = self.config.get('enable_prediction', True)
        self.enable_adaptive_thresholds = self.config.get('enable_adaptive_thresholds', True)

        # RAG configuration
        self.rag_k_neighbors = self.config.get('rag_k_neighbors', 5)  # Top-K similar contexts
        self.rag_similarity_threshold = self.config.get('rag_similarity_threshold', 0.75)

        # RLHF configuration
        self.rlhf_learning_rate = self.config.get('rlhf_learning_rate', 0.1)
        self.rlhf_min_samples = self.config.get('rlhf_min_samples', 10)

        # Prediction configuration
        self.prediction_window_days = self.config.get('prediction_window_days', 30)
        self.prediction_min_samples = self.config.get('prediction_min_samples', 20)

        # Adaptive configuration
        self.adaptive_window_days = self.config.get('adaptive_window_days', 7)
        self.adaptive_target_false_positive_rate = self.config.get('target_fpr', 0.01)  # 1% FPR
        self.adaptive_target_false_negative_rate = self.config.get('target_fnr', 0.05)  # 5% FNR

        # Storage
        data_dir = Path(os.getenv('JARVIS_DATA_DIR', Path.home() / '.jarvis')) / 'intelligence'
        data_dir.mkdir(parents=True, exist_ok=True)
        self.learning_file = data_dir / 'authentication_learning.json'

        # Will be lazily loaded
        self.learning_db = None
        self.rag_engine = None

        # Statistics
        self._stats = {
            'total_records': 0,
            'rlhf_feedbacks': 0,
            'threshold_adjustments': 0,
            'predictions_made': 0,
            'prediction_accuracy': 0.0
        }

        logger.info("IntelligenceLearningCoordinator initialized")

    async def initialize(self):
        """Initialize connections to learning database and RAG engine."""
        try:
            # Load learning database
            from intelligence.learning_database import get_learning_database
            self.learning_db = await get_learning_database()
            logger.info("✅ Learning database connected")

            # Load RAG engine if available
            if self.enable_rag:
                try:
                    from engines.rag_engine import get_rag_engine
                    self.rag_engine = await get_rag_engine()
                    logger.info("✅ RAG engine loaded")
                except ImportError:
                    logger.warning("RAG engine not available - continuing without RAG")
                    self.enable_rag = False

        except Exception as e:
            logger.warning(f"Error initializing learning coordinator: {e}")

    async def record_authentication(
        self,
        user_id: str,
        outcome: AuthOutcome,
        voice_confidence: float,
        voice_embedding: Optional[bytes],
        network_context: Dict,
        temporal_context: Dict,
        device_context: Dict,
        drift_adjustment: float,
        final_confidence: float,
        risk_score: float,
        decision: str
    ) -> int:
        """
        Record authentication attempt for learning.

        Args:
            user_id: User identifier
            outcome: Authentication outcome
            voice_confidence: Voice biometric confidence
            voice_embedding: Voice embedding bytes
            network_context: Network intelligence context
            temporal_context: Temporal pattern context
            device_context: Device state context
            drift_adjustment: Voice drift adjustment
            final_confidence: Multi-factor fused confidence
            risk_score: Calculated risk score
            decision: Final authentication decision

        Returns:
            Record ID
        """
        try:
            record = AuthenticationRecord(
                timestamp=datetime.now(),
                user_id=user_id,
                outcome=outcome,
                voice_confidence=voice_confidence,
                voice_embedding=voice_embedding,
                network_ssid_hash=network_context.get('ssid_hash'),
                network_trust=network_context.get('trust_score', 0.5),
                temporal_confidence=temporal_context.get('confidence', 0.5),
                device_state=device_context.get('state', 'unknown'),
                device_trust=device_context.get('trust_score', 0.5),
                drift_adjustment=drift_adjustment,
                final_confidence=final_confidence,
                risk_score=risk_score,
                decision=decision
            )

            # Store to learning database
            if self.learning_db:
                # Store voice sample
                if voice_embedding and outcome in [AuthOutcome.SUCCESS, AuthOutcome.CHALLENGED]:
                    voice_sample_id = await self.learning_db.store_voice_sample(
                        speaker_name=user_id,
                        audio_data=None,  # Not storing raw audio
                        embedding=voice_embedding,
                        confidence=voice_confidence,
                        verified=outcome == AuthOutcome.SUCCESS,
                        quality_score=final_confidence,
                        metadata={
                            'network_trust': record.network_trust,
                            'temporal_confidence': record.temporal_confidence,
                            'device_trust': record.device_trust,
                            'risk_score': risk_score,
                            'decision': decision
                        }
                    )
                    record.voice_sample_id = voice_sample_id

                # Store authentication record
                record_id = await self._store_auth_record(record)
                record.record_id = record_id

                self._stats['total_records'] += 1

                logger.debug(f"Recorded authentication: {outcome.value}, confidence: {final_confidence:.1%}")

                return record_id

        except Exception as e:
            logger.error(f"Error recording authentication: {e}")

        return -1

    async def _store_auth_record(self, record: AuthenticationRecord) -> int:
        """Store authentication record to learning database."""
        # Store as JSON metadata for now
        # Could be enhanced with dedicated table in learning_database.py
        metadata = {
            'timestamp': record.timestamp.isoformat(),
            'user_id': record.user_id,
            'outcome': record.outcome.value,
            'voice_confidence': record.voice_confidence,
            'network_ssid_hash': record.network_ssid_hash,
            'network_trust': record.network_trust,
            'temporal_confidence': record.temporal_confidence,
            'device_state': record.device_state,
            'device_trust': record.device_trust,
            'drift_adjustment': record.drift_adjustment,
            'final_confidence': record.final_confidence,
            'risk_score': record.risk_score,
            'decision': record.decision
        }

        # Write to local file (could be moved to database table)
        try:
            records = []
            if self.learning_file.exists():
                with open(self.learning_file, 'r') as f:
                    records = json.load(f)

            records.append(metadata)

            # Keep last 10000 records
            if len(records) > 10000:
                records = records[-10000:]

            with open(self.learning_file, 'w') as f:
                json.dump(records, f)

            return len(records) - 1

        except Exception as e:
            logger.error(f"Error storing auth record: {e}")
            return -1

    async def apply_rlhf_feedback(
        self,
        record_id: int,
        was_correct: bool,
        feedback_score: float,
        feedback_notes: Optional[str] = None
    ):
        """
        Apply RLHF feedback for continuous improvement.

        Args:
            record_id: Authentication record ID
            was_correct: Was the authentication decision correct?
            feedback_score: Human feedback score (0.0-1.0)
            feedback_notes: Optional notes explaining the feedback
        """
        try:
            # Load record
            records = []
            if self.learning_file.exists():
                with open(self.learning_file, 'r') as f:
                    records = json.load(f)

            if record_id < 0 or record_id >= len(records):
                logger.warning(f"Invalid record ID: {record_id}")
                return

            record = records[record_id]

            # Update with feedback
            record['was_correct'] = was_correct
            record['feedback_score'] = feedback_score
            record['feedback_notes'] = feedback_notes

            # Save back
            with open(self.learning_file, 'w') as f:
                json.dump(records, f)

            # Apply feedback to voice sample if available
            if self.learning_db and 'voice_sample_id' in record:
                voice_sample_id = record.get('voice_sample_id')
                if voice_sample_id:
                    await self.learning_db.apply_rlhf_feedback(
                        sample_id=voice_sample_id,
                        feedback_score=feedback_score,
                        feedback_notes=feedback_notes
                    )

            self._stats['rlhf_feedbacks'] += 1

            logger.info(f"✅ Applied RLHF feedback to record {record_id}: correct={was_correct}, score={feedback_score:.2f}")

            # Trigger adaptive threshold adjustment if enough feedback
            if self.enable_adaptive_thresholds:
                await self._check_and_adjust_thresholds()

        except Exception as e:
            logger.error(f"Error applying RLHF feedback: {e}")

    async def get_rag_context(
        self,
        user_id: str,
        network_context: Dict,
        temporal_context: Dict,
        device_context: Dict
    ) -> Dict[str, Any]:
        """
        Use RAG to retrieve similar authentication contexts.

        Args:
            user_id: User identifier
            network_context: Current network context
            temporal_context: Current temporal context
            device_context: Current device context

        Returns:
            Dict with similar contexts and recommendations
        """
        if not self.enable_rag:
            return {
                'similar_contexts': [],
                'avg_confidence': 0.5,
                'recommendation': 'No RAG context available'
            }

        try:
            # Load historical records
            records = []
            if self.learning_file.exists():
                with open(self.learning_file, 'r') as f:
                    records = json.load(f)

            if not records:
                return {
                    'similar_contexts': [],
                    'avg_confidence': 0.5,
                    'recommendation': 'No historical data'
                }

            # Create context vector for similarity search
            current_context = self._create_context_vector(
                network_context, temporal_context, device_context
            )

            # Find similar contexts
            similar = []
            for record in records[-500:]:  # Last 500 records
                if record['user_id'] != user_id:
                    continue

                record_context = self._create_context_vector(
                    {'ssid_hash': record.get('network_ssid_hash'), 'trust_score': record.get('network_trust')},
                    {'confidence': record.get('temporal_confidence')},
                    {'state': record.get('device_state'), 'trust_score': record.get('device_trust')}
                )

                # Calculate similarity
                similarity = self._cosine_similarity(current_context, record_context)

                if similarity >= self.rag_similarity_threshold:
                    similar.append({
                        'similarity': similarity,
                        'outcome': record['outcome'],
                        'confidence': record['final_confidence'],
                        'decision': record['decision'],
                        'timestamp': record['timestamp']
                    })

            # Sort by similarity
            similar.sort(key=lambda x: x['similarity'], reverse=True)
            similar = similar[:self.rag_k_neighbors]

            # Calculate recommendations
            if similar:
                avg_confidence = sum(s['confidence'] for s in similar) / len(similar)
                success_rate = sum(1 for s in similar if s['outcome'] == 'success') / len(similar)

                recommendation = (
                    f"Found {len(similar)} similar contexts. "
                    f"Avg confidence: {avg_confidence:.1%}, "
                    f"Success rate: {success_rate:.1%}"
                )

                return {
                    'similar_contexts': similar,
                    'avg_confidence': avg_confidence,
                    'success_rate': success_rate,
                    'recommendation': recommendation
                }
            else:
                return {
                    'similar_contexts': [],
                    'avg_confidence': 0.5,
                    'recommendation': 'No similar contexts found'
                }

        except Exception as e:
            logger.error(f"Error getting RAG context: {e}")
            return {
                'similar_contexts': [],
                'avg_confidence': 0.5,
                'recommendation': f'Error: {str(e)}'
            }

    def _create_context_vector(
        self,
        network_context: Dict,
        temporal_context: Dict,
        device_context: Dict
    ) -> List[float]:
        """Create vector representation of context for similarity comparison."""
        return [
            network_context.get('trust_score', 0.5),
            temporal_context.get('confidence', 0.5),
            device_context.get('trust_score', 0.5),
            1.0 if device_context.get('state') == 'stationary' else 0.5,
            1.0 if device_context.get('state') == 'docked' else 0.5
        ]

    def _cosine_similarity(self, vec1: List[float], vec2: List[float]) -> float:
        """Calculate cosine similarity between two vectors."""
        if not NUMPY_AVAILABLE:
            # Simple dot product / magnitudes
            dot_product = sum(a * b for a, b in zip(vec1, vec2))
            mag1 = sum(a ** 2 for a in vec1) ** 0.5
            mag2 = sum(b ** 2 for b in vec2) ** 0.5
            if mag1 == 0 or mag2 == 0:
                return 0.0
            return dot_product / (mag1 * mag2)
        else:
            vec1_np = np.array(vec1)
            vec2_np = np.array(vec2)
            return float(np.dot(vec1_np, vec2_np) / (np.linalg.norm(vec1_np) * np.linalg.norm(vec2_np)))

    async def predict_next_unlock(self, user_id: str) -> Optional[datetime]:
        """
        Predict next unlock time based on historical patterns.

        Args:
            user_id: User identifier

        Returns:
            Predicted next unlock time, or None if not enough data
        """
        if not self.enable_prediction:
            return None

        try:
            # Load historical records
            records = []
            if self.learning_file.exists():
                with open(self.learning_file, 'r') as f:
                    records = json.load(f)

            # Filter successful unlocks for this user
            successful = [
                r for r in records
                if r['user_id'] == user_id and r['outcome'] == 'success'
            ]

            if len(successful) < self.prediction_min_samples:
                return None

            # Analyze patterns
            timestamps = [datetime.fromisoformat(r['timestamp']) for r in successful[-100:]]

            # Calculate average time between unlocks
            intervals = []
            for i in range(1, len(timestamps)):
                interval = (timestamps[i] - timestamps[i-1]).total_seconds()
                if interval < 86400:  # Less than 24 hours
                    intervals.append(interval)

            if not intervals:
                return None

            avg_interval = sum(intervals) / len(intervals)

            # Predict next unlock
            last_unlock = timestamps[-1]
            predicted = last_unlock + timedelta(seconds=avg_interval)

            self._stats['predictions_made'] += 1

            logger.debug(f"Predicted next unlock: {predicted} (avg interval: {avg_interval/60:.1f} minutes)")

            return predicted

        except Exception as e:
            logger.error(f"Error predicting next unlock: {e}")
            return None

    async def _check_and_adjust_thresholds(self):
        """Check if thresholds should be adjusted based on RLHF feedback."""
        try:
            # Load records with feedback
            records = []
            if self.learning_file.exists():
                with open(self.learning_file, 'r') as f:
                    records = json.load(f)

            # Filter recent records with feedback
            cutoff = datetime.now() - timedelta(days=self.adaptive_window_days)
            recent_with_feedback = [
                r for r in records
                if 'was_correct' in r and
                   datetime.fromisoformat(r['timestamp']) >= cutoff
            ]

            if len(recent_with_feedback) < self.rlhf_min_samples:
                return  # Not enough data

            # Calculate error rates
            false_positives = sum(
                1 for r in recent_with_feedback
                if r['decision'] == 'authenticate' and not r['was_correct']
            )
            false_negatives = sum(
                1 for r in recent_with_feedback
                if r['decision'] in ['deny', 'challenge'] and not r['was_correct']
            )

            total = len(recent_with_feedback)
            fpr = false_positives / total if total > 0 else 0
            fnr = false_negatives / total if total > 0 else 0

            # Determine if adjustment needed
            adjustment = 0.0

            if fpr > self.adaptive_target_false_positive_rate:
                # Too many false positives - increase threshold
                adjustment = 0.05
                logger.info(f"Adaptive: FPR {fpr:.1%} > target {self.adaptive_target_false_positive_rate:.1%}, increasing threshold by {adjustment}")

            elif fnr > self.adaptive_target_false_negative_rate:
                # Too many false negatives - decrease threshold
                adjustment = -0.05
                logger.info(f"Adaptive: FNR {fnr:.1%} > target {self.adaptive_target_false_negative_rate:.1%}, decreasing threshold by {adjustment}")

            if adjustment != 0.0:
                # Apply adjustment (in practice, would update fusion engine config)
                self._stats['threshold_adjustments'] += 1
                logger.info(f"✅ Adaptive threshold adjustment: {adjustment:+.2f} (FPR: {fpr:.1%}, FNR: {fnr:.1%})")

                # TODO: Actually apply to fusion engine
                # fusion_engine.adjust_threshold(adjustment)

        except Exception as e:
            logger.error(f"Error checking/adjusting thresholds: {e}")

    async def get_learning_insights(self, user_id: str) -> LearningInsights:
        """
        Get comprehensive learning insights for user.

        Args:
            user_id: User identifier

        Returns:
            LearningInsights with patterns and predictions
        """
        try:
            # Load records
            records = []
            if self.learning_file.exists():
                with open(self.learning_file, 'r') as f:
                    records = json.load(f)

            user_records = [r for r in records if r['user_id'] == user_id]

            if not user_records:
                return LearningInsights(
                    typical_network_trust=0.5,
                    typical_temporal_confidence=0.5,
                    typical_device_state='unknown',
                    avg_successful_confidence=0.5,
                    predicted_next_unlock_time=None,
                    predicted_confidence=0.0,
                    confidence_in_prediction=0.0,
                    similar_authentications=[],
                    context_similarity_score=0.0,
                    should_adjust_thresholds=False,
                    recommended_threshold_adjustment=0.0,
                    reasoning=["No historical data available"]
                )

            # Calculate patterns
            successful = [r for r in user_records if r['outcome'] == 'success']

            typical_network_trust = sum(r['network_trust'] for r in successful) / len(successful) if successful else 0.5
            typical_temporal_confidence = sum(r['temporal_confidence'] for r in successful) / len(successful) if successful else 0.5
            avg_successful_confidence = sum(r['final_confidence'] for r in successful) / len(successful) if successful else 0.5

            # Most common device state
            device_states = [r['device_state'] for r in successful]
            typical_device_state = max(set(device_states), key=device_states.count) if device_states else 'unknown'

            # Predict next unlock
            predicted_time = await self.predict_next_unlock(user_id)

            return LearningInsights(
                typical_network_trust=typical_network_trust,
                typical_temporal_confidence=typical_temporal_confidence,
                typical_device_state=typical_device_state,
                avg_successful_confidence=avg_successful_confidence,
                predicted_next_unlock_time=predicted_time,
                predicted_confidence=avg_successful_confidence,
                confidence_in_prediction=0.8 if len(successful) > 20 else 0.5,
                similar_authentications=[],
                context_similarity_score=0.0,
                should_adjust_thresholds=False,
                recommended_threshold_adjustment=0.0,
                reasoning=[
                    f"Analyzed {len(user_records)} total authentications",
                    f"{len(successful)} successful ({len(successful)/len(user_records):.0%})",
                    f"Typical network trust: {typical_network_trust:.0%}",
                    f"Typical temporal confidence: {typical_temporal_confidence:.0%}",
                    f"Most common device state: {typical_device_state}"
                ]
            )

        except Exception as e:
            logger.error(f"Error getting learning insights: {e}")
            return LearningInsights(
                typical_network_trust=0.5,
                typical_temporal_confidence=0.5,
                typical_device_state='unknown',
                avg_successful_confidence=0.5,
                predicted_next_unlock_time=None,
                predicted_confidence=0.0,
                confidence_in_prediction=0.0,
                similar_authentications=[],
                context_similarity_score=0.0,
                should_adjust_thresholds=False,
                recommended_threshold_adjustment=0.0,
                reasoning=[f"Error: {str(e)}"]
            )

    def get_stats(self) -> Dict:
        """Get learning coordinator statistics."""
        return self._stats.copy()


# Singleton instance
_coordinator_instance: Optional[IntelligenceLearningCoordinator] = None
_coordinator_lock = asyncio.Lock()


async def get_learning_coordinator(config: Optional[Dict] = None) -> IntelligenceLearningCoordinator:
    """
    Get singleton IntelligenceLearningCoordinator instance.

    Args:
        config: Optional configuration (only used on first call)

    Returns:
        IntelligenceLearningCoordinator instance
    """
    global _coordinator_instance

    async with _coordinator_lock:
        if _coordinator_instance is None:
            _coordinator_instance = IntelligenceLearningCoordinator(config)
            await _coordinator_instance.initialize()
            logger.info("Intelligence Learning Coordinator singleton initialized")

        return _coordinator_instance


# Helper function for learning database factory
async def get_learning_database():
    """Get learning database instance."""
    try:
        from intelligence.learning_database import JARVISLearningDatabase
        db = JARVISLearningDatabase()
        await db.initialize()
        return db
    except Exception as e:
        logger.error(f"Error loading learning database: {e}")
        return None


# CLI testing
if __name__ == "__main__":
    import sys

    async def main():
        """Test learning coordinator."""
        logging.basicConfig(
            level=logging.DEBUG,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )

        coordinator = await get_learning_coordinator()

        print("\n" + "="*80)
        print("JARVIS Intelligence Learning Coordinator - Test")
        print("="*80 + "\n")

        # Simulate authentication
        print("Test 1: Recording successful authentication")
        record_id = await coordinator.record_authentication(
            user_id="derek",
            outcome=AuthOutcome.SUCCESS,
            voice_confidence=0.94,
            voice_embedding=None,
            network_context={'ssid_hash': 'abc123', 'trust_score': 0.95},
            temporal_context={'confidence': 0.88},
            device_context={'state': 'stationary', 'trust_score': 0.92},
            drift_adjustment=0.02,
            final_confidence=0.96,
            risk_score=0.08,
            decision='authenticate'
        )
        print(f"Record ID: {record_id}")

        # Get RAG context
        print("\nTest 2: Getting RAG context")
        rag_context = await coordinator.get_rag_context(
            user_id="derek",
            network_context={'ssid_hash': 'abc123', 'trust_score': 0.95},
            temporal_context={'confidence': 0.88},
            device_context={'state': 'stationary', 'trust_score': 0.92}
        )
        print(f"Similar contexts: {len(rag_context['similar_contexts'])}")
        print(f"Recommendation: {rag_context['recommendation']}")

        # Apply RLHF feedback
        print("\nTest 3: Applying RLHF feedback")
        await coordinator.apply_rlhf_feedback(
            record_id=record_id,
            was_correct=True,
            feedback_score=1.0,
            feedback_notes="Perfect authentication"
        )
        print("✅ Feedback applied")

        # Get learning insights
        print("\nTest 4: Getting learning insights")
        insights = await coordinator.get_learning_insights("derek")
        print(f"Typical network trust: {insights.typical_network_trust:.1%}")
        print(f"Typical device state: {insights.typical_device_state}")
        print(f"Avg successful confidence: {insights.avg_successful_confidence:.1%}")
        print(f"\nReasoning:")
        for reason in insights.reasoning:
            print(f"  - {reason}")

        # Statistics
        print("\n" + "="*80)
        stats = coordinator.get_stats()
        print("Statistics:")
        print(f"  Total Records: {stats['total_records']}")
        print(f"  RLHF Feedbacks: {stats['rlhf_feedbacks']}")
        print(f"  Threshold Adjustments: {stats['threshold_adjustments']}")
        print(f"  Predictions Made: {stats['predictions_made']}")
        print("\n" + "="*80 + "\n")

    asyncio.run(main())
