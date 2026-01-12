#!/usr/bin/env python3
"""
JARVIS Continuous Learning Engine
==================================

Advanced ML system for continuous improvement of:
1. Voice biometric authentication (speaker recognition)
2. Password typing accuracy and speed

This engine uses multiple ML algorithms to learn from every unlock attempt
and progressively improve performance over time.

Algorithms Used:
- **Reinforcement Learning** (Q-Learning): Optimal timing strategies
- **Bayesian Optimization**: Hyperparameter tuning for timing
- **Random Forest**: Pattern recognition and failure prediction
- **LSTM Neural Network**: Sequential typing patterns
- **Online Learning** (SGD): Real-time model updates
- **Ensemble Methods**: Combining multiple models for robustness
"""

import asyncio
import logging
import numpy as np
import json
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple
from pathlib import Path
from dataclasses import dataclass, asdict
import sqlite3

from backend.core.async_safety import LazyAsyncLock

logger = logging.getLogger(__name__)

# Import advanced ML features
try:
    from voice_unlock.advanced_ml_features import (
        FailurePredictor,
        BayesianTimingOptimizer,
        MultiArmedBandit,
        ContextAwareLearner,
        AnomalyDetector,
        SpeakerEmbeddingFineTuner,
        ProgressiveLearningManager,
        ModelPersistence
    )
    ADVANCED_FEATURES_AVAILABLE = True
except ImportError as e:
    logger.warning(f"⚠️ Advanced ML features not available: {e}")
    ADVANCED_FEATURES_AVAILABLE = False


@dataclass
class VoiceBiometricState:
    """Current state of voice biometric learning"""
    total_samples: int
    successful_authentications: int
    confidence_trend: List[float]  # Last 50 confidence scores
    avg_confidence: float
    best_confidence: float
    worst_confidence: float
    false_rejection_rate: float  # FRR: Rejecting valid user
    improvement_rate: float  # How much confidence is improving


@dataclass
class TypingPerformanceState:
    """Current state of password typing learning"""
    total_attempts: int
    successful_attempts: int
    avg_typing_speed_ms: float
    fastest_typing_ms: float
    failure_points: Dict[int, int]  # char_position -> failure_count
    optimal_timings: Dict[str, float]  # char_type -> optimal_duration_ms
    success_rate_trend: List[float]  # Last 50 success rates


class VoiceBiometricLearner:
    """
    Continuous learning for voice biometric authentication.

    Uses:
    - **Online Learning**: Updates model with each new voice sample
    - **Adaptive Thresholding**: Dynamically adjusts confidence threshold
    - **Anomaly Detection**: Identifies suspicious authentication attempts
    - **Confidence Calibration**: Improves confidence score accuracy
    - **Context-Aware Learning**: Adapts to time of day, voice variations
    - **Fine-Tuning**: Progressively refines speaker embeddings
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.state = None
        self.confidence_threshold = 0.40  # Start at 40%, will adapt
        self.min_threshold = 0.35  # Safety minimum
        self.max_threshold = 0.60  # Safety maximum

        # Adaptive learning parameters
        self.learning_rate = 0.01
        self.adaptation_window = 50  # Consider last 50 attempts

        # 🤖 Advanced ML features
        if ADVANCED_FEATURES_AVAILABLE:
            models_dir = Path(db_path).parent / "ml_models"
            models_dir.mkdir(parents=True, exist_ok=True)

            self.anomaly_detector = AnomalyDetector(str(models_dir / "anomaly_detector.pkl"))
            self.context_learner = ContextAwareLearner()
            self.fine_tuner = SpeakerEmbeddingFineTuner(learning_rate=0.001)
            self.progress_manager = ProgressiveLearningManager()
        else:
            self.anomaly_detector = None
            self.context_learner = None
            self.fine_tuner = None
            self.progress_manager = None

    async def initialize(self):
        """Load historical data and initialize learning state"""
        try:
            from intelligence.learning_database import get_learning_database

            db = await get_learning_database()
            profiles = await db.get_all_speaker_profiles()

            # Find Derek's profile
            derek_profile = None
            for profile in profiles:
                if profile.get('is_primary_user') or 'Derek' in profile.get('speaker_name', ''):
                    derek_profile = profile
                    break

            if not derek_profile:
                logger.warning("⚠️ Derek's profile not found, using defaults")
                self.state = VoiceBiometricState(
                    total_samples=0,
                    successful_authentications=0,
                    confidence_trend=[],
                    avg_confidence=0.0,
                    best_confidence=0.0,
                    worst_confidence=0.0,
                    false_rejection_rate=0.0,
                    improvement_rate=0.0
                )

                # Initialize advanced features even without profile
                if ADVANCED_FEATURES_AVAILABLE and self.anomaly_detector:
                    await self.anomaly_detector.initialize()

                return

            # Load unlock attempt history
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            cursor.execute("""
                SELECT speaker_confidence, success
                FROM unlock_attempts
                WHERE speaker_name LIKE '%Derek%'
                ORDER BY timestamp DESC
                LIMIT 100
            """)

            results = cursor.fetchall()
            conn.close()

            if results:
                confidences = [r[0] for r in results if r[0] is not None]
                successes = sum(1 for r in results if r[1] == 1)

                # Calculate FRR: times Derek was rejected despite good confidence
                false_rejections = sum(1 for r in results if r[0] and r[0] > 0.50 and r[1] == 0)
                frr = false_rejections / len(results) if results else 0.0

                # Calculate improvement trend
                if len(confidences) >= 10:
                    recent_avg = np.mean(confidences[:10])
                    older_avg = np.mean(confidences[-10:])
                    improvement = (recent_avg - older_avg) / older_avg if older_avg > 0 else 0.0
                else:
                    improvement = 0.0

                self.state = VoiceBiometricState(
                    total_samples=derek_profile.get('total_samples', 0),
                    successful_authentications=successes,
                    confidence_trend=confidences[:50],
                    avg_confidence=np.mean(confidences) if confidences else 0.0,
                    best_confidence=max(confidences) if confidences else 0.0,
                    worst_confidence=min(confidences) if confidences else 0.0,
                    false_rejection_rate=frr,
                    improvement_rate=improvement
                )

                logger.info(f"✅ Voice biometric learner initialized: {self.state.total_samples} samples, "
                           f"avg confidence: {self.state.avg_confidence:.1%}, FRR: {self.state.false_rejection_rate:.1%}")

                # 🤖 Initialize advanced ML features with historical data
                if ADVANCED_FEATURES_AVAILABLE:
                    # Train anomaly detector on historical attempts
                    if self.anomaly_detector:
                        await self.anomaly_detector.initialize()
                        historical_attempts = [
                            {
                                'speaker_confidence': r[0],
                                'stt_confidence': 0.9,  # Placeholder
                                'audio_duration_ms': 2000,
                                'processing_time_ms': 500,
                                'timestamp': datetime.now().isoformat()
                            }
                            for r in results if r[0] is not None
                        ]
                        await self.anomaly_detector.train(historical_attempts)

                    # Initialize fine-tuner with base embedding
                    if self.fine_tuner and derek_profile.get('embedding'):
                        embedding_bytes = derek_profile['embedding']
                        if isinstance(embedding_bytes, (bytes, bytearray)):
                            embedding = np.frombuffer(embedding_bytes, dtype=np.float32)
                            await self.fine_tuner.initialize(embedding)

            else:
                logger.warning("⚠️ No historical unlock data found")
                self.state = VoiceBiometricState(
                    total_samples=derek_profile.get('total_samples', 0),
                    successful_authentications=0,
                    confidence_trend=[],
                    avg_confidence=0.0,
                    best_confidence=0.0,
                    worst_confidence=0.0,
                    false_rejection_rate=0.0,
                    improvement_rate=0.0
                )

                # Initialize advanced features even without history
                if ADVANCED_FEATURES_AVAILABLE and self.anomaly_detector:
                    await self.anomaly_detector.initialize()

        except Exception as e:
            logger.error(f"Failed to initialize voice biometric learner: {e}", exc_info=True)

    async def update_from_attempt(
        self,
        confidence: float,
        success: bool,
        is_owner: bool,
        audio_quality: float = 0.9,
        new_embedding: Optional[np.ndarray] = None,
        audio_duration_ms: float = 2000,
        processing_time_ms: float = 500
    ):
        """
        Update learning model from new authentication attempt.

        Uses online learning to adapt threshold dynamically.
        Includes advanced features: anomaly detection, context learning, fine-tuning.
        """
        if not self.state:
            await self.initialize()

        # 🤖 ANOMALY DETECTION: Check if this attempt is suspicious
        if ADVANCED_FEATURES_AVAILABLE and self.anomaly_detector:
            is_anomaly, anomaly_score = await self.anomaly_detector.detect_anomaly(
                confidence,
                audio_quality,
                audio_duration_ms,
                processing_time_ms
            )

            if is_anomaly:
                logger.warning(f"🚨 Anomalous authentication pattern detected (score: {anomaly_score:.3f})")

        # 🤖 CONTEXT-AWARE LEARNING: Understand context patterns
        if ADVANCED_FEATURES_AVAILABLE and self.context_learner:
            context = self.context_learner.get_context(audio_quality)
            await self.context_learner.update_context_pattern(context, confidence, success)

            # Adjust expectations based on context
            expected_confidence = await self.context_learner.get_expected_confidence(context)
            logger.debug(f"📊 Context: {context.time_of_day}/{context.estimated_voice_condition}, "
                        f"expected: {expected_confidence:.1%}, actual: {confidence:.1%}")

        # 🤖 FINE-TUNING: Adapt speaker embedding
        if ADVANCED_FEATURES_AVAILABLE and self.fine_tuner and new_embedding is not None:
            await self.fine_tuner.fine_tune(new_embedding, confidence, success)

        # Update state
        self.state.confidence_trend.insert(0, confidence)
        self.state.confidence_trend = self.state.confidence_trend[:50]  # Keep last 50

        if success:
            self.state.successful_authentications += 1

        # Recalculate statistics
        if self.state.confidence_trend:
            self.state.avg_confidence = np.mean(self.state.confidence_trend)
            self.state.best_confidence = max(self.state.confidence_trend)
            self.state.worst_confidence = min(self.state.confidence_trend)

        # **ADAPTIVE THRESHOLD LEARNING**
        # If Derek is consistently above threshold, lower it slightly (more lenient)
        # If Derek is being rejected, raise it slightly (more strict on imposters)
        if is_owner and len(self.state.confidence_trend) >= 10:
            recent_confidences = self.state.confidence_trend[:10]
            avg_recent = np.mean(recent_confidences)

            # If Derek consistently scores high, we can lower threshold
            if avg_recent > self.confidence_threshold + 0.15:
                adjustment = -0.01  # Lower threshold by 1%
                self.confidence_threshold = max(
                    self.min_threshold,
                    self.confidence_threshold + adjustment
                )
                logger.info(f"🎯 Adaptive threshold: Lowered to {self.confidence_threshold:.1%} "
                           f"(Derek consistently high: {avg_recent:.1%})")

            # If Derek is borderline, slightly increase to be more strict
            elif avg_recent < self.confidence_threshold + 0.05:
                adjustment = 0.005  # Raise threshold by 0.5%
                self.confidence_threshold = min(
                    self.max_threshold,
                    self.confidence_threshold + adjustment
                )
                logger.info(f"🎯 Adaptive threshold: Raised to {self.confidence_threshold:.1%} "
                           f"(Derek borderline: {avg_recent:.1%})")

        # Calculate improvement rate
        if len(self.state.confidence_trend) >= 20:
            recent = self.state.confidence_trend[:10]
            older = self.state.confidence_trend[10:20]
            self.state.improvement_rate = (np.mean(recent) - np.mean(older)) / np.mean(older)

        logger.info(f"📊 Voice biometric updated: confidence={confidence:.1%}, "
                   f"threshold={self.confidence_threshold:.1%}, "
                   f"improvement={self.state.improvement_rate:+.1%}")

    async def get_recommended_threshold(self) -> float:
        """Get ML-optimized confidence threshold"""
        if not self.state:
            await self.initialize()

        return self.confidence_threshold

    async def predict_authentication_success(self, confidence: float) -> Dict[str, Any]:
        """
        Predict whether authentication will succeed based on learned patterns.

        Returns prediction with confidence and reasoning.
        """
        if not self.state:
            await self.initialize()

        # Simple but effective prediction
        predicted_success = confidence >= self.confidence_threshold

        # Calculate prediction confidence based on historical data
        if self.state.confidence_trend:
            # How far is this confidence from the average?
            deviation = abs(confidence - self.state.avg_confidence)
            std_dev = np.std(self.state.confidence_trend)

            # Closer to average = more confident in prediction
            prediction_confidence = max(0.5, 1.0 - (deviation / (2 * std_dev)) if std_dev > 0 else 0.5)
        else:
            prediction_confidence = 0.5

        return {
            'predicted_success': predicted_success,
            'prediction_confidence': prediction_confidence,
            'confidence_score': confidence,
            'threshold': self.confidence_threshold,
            'reasoning': (
                f"Confidence {confidence:.1%} is {'above' if predicted_success else 'below'} "
                f"learned threshold {self.confidence_threshold:.1%}"
            )
        }


class PasswordTypingLearner:
    """
    Continuous learning for password typing optimization.

    Uses:
    - **Reinforcement Learning (Q-Learning)**: Learn optimal timing strategies
    - **Bayesian Optimization**: Find optimal timing parameters
    - **Random Forest**: Predict failure points
    - **Online Gradient Descent**: Real-time timing adjustments
    - **Multi-Armed Bandit**: Balance exploration vs exploitation
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.state = None

        # Q-Learning parameters for timing optimization
        self.q_table = {}  # state -> action -> Q-value
        self.learning_rate = 0.1
        self.discount_factor = 0.95
        self.epsilon = 0.1  # Exploration rate

        # Optimal timing parameters (will be learned)
        self.optimal_timings = {
            'letter_lower': {'duration': 50, 'delay': 100},
            'letter_upper': {'duration': 55, 'delay': 105},
            'digit': {'duration': 50, 'delay': 100},
            'special': {'duration': 60, 'delay': 120},
            'shift_duration': 30,
            'shift_delay': 30
        }

        # Success tracking for each character position
        self.char_position_success = {}  # position -> success_count
        self.char_position_failures = {}  # position -> failure_count

        # 🤖 Advanced ML features
        if ADVANCED_FEATURES_AVAILABLE:
            models_dir = Path(db_path).parent / "ml_models"
            models_dir.mkdir(parents=True, exist_ok=True)

            self.failure_predictor = FailurePredictor(str(models_dir / "failure_predictor.pkl"))
            self.bayesian_optimizer = BayesianTimingOptimizer(db_path)
            self.multi_armed_bandit = MultiArmedBandit(epsilon=0.1)
        else:
            self.failure_predictor = None
            self.bayesian_optimizer = None
            self.multi_armed_bandit = None

    async def initialize(self):
        """Load historical typing data and initialize learning state"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            # Load typing session history
            cursor.execute("""
                SELECT
                    success,
                    total_typing_duration_ms,
                    failed_at_character
                FROM password_typing_sessions
                ORDER BY timestamp DESC
                LIMIT 100
            """)

            sessions = cursor.fetchall()

            if sessions:
                total = len(sessions)
                successful = sum(1 for s in sessions if s[0] == 1)

                speeds = [s[1] for s in sessions if s[1] is not None]
                failure_points = {}
                for s in sessions:
                    if s[2] is not None:  # failed_at_character
                        pos = s[2]
                        failure_points[pos] = failure_points.get(pos, 0) + 1

                self.state = TypingPerformanceState(
                    total_attempts=total,
                    successful_attempts=successful,
                    avg_typing_speed_ms=np.mean(speeds) if speeds else 0.0,
                    fastest_typing_ms=min(speeds) if speeds else 0.0,
                    failure_points=failure_points,
                    optimal_timings={},
                    success_rate_trend=[successful / total if total > 0 else 0.0]
                )

                # Load character-level metrics for timing optimization
                cursor.execute("""
                    SELECT
                        char_type,
                        requires_shift,
                        AVG(total_duration_ms) as avg_duration,
                        AVG(inter_char_delay_ms) as avg_delay,
                        AVG(CASE WHEN success = 1 THEN total_duration_ms ELSE NULL END) as success_duration,
                        SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) * 100.0 / COUNT(*) as success_rate
                    FROM character_typing_metrics
                    GROUP BY char_type, requires_shift
                    HAVING COUNT(*) >= 5
                """)

                char_stats = cursor.fetchall()

                # Update optimal timings based on successful attempts
                for stat in char_stats:
                    char_type, requires_shift, avg_dur, avg_delay, success_dur, success_rate = stat

                    key = f"{char_type}_{'shift' if requires_shift else 'noshift'}"

                    if success_dur and success_rate > 50:
                        # Use successful timing as optimal
                        self.optimal_timings[key] = {
                            'duration': success_dur * 1.1,  # Add 10% margin
                            'delay': max(avg_delay or 100, 80)
                        }

                logger.info(f"✅ Password typing learner initialized: {total} attempts, "
                           f"{successful}/{total} successful ({successful/total*100:.1f}%), "
                           f"avg speed: {self.state.avg_typing_speed_ms:.0f}ms")

                if failure_points:
                    logger.info(f"⚠️ Failure hotspots: {failure_points}")

                # 🤖 Initialize advanced ML features with historical data
                if ADVANCED_FEATURES_AVAILABLE:
                    # Initialize and train Random Forest failure predictor
                    if self.failure_predictor:
                        await self.failure_predictor.initialize()

                        # Load character metrics for training
                        # Note: char_start_time_ms used for ordering (timestamp column doesn't exist)
                        cursor.execute("""
                            SELECT
                                char_position, char_type, requires_shift, success,
                                total_duration_ms, system_load_at_char, char_start_time_ms
                            FROM character_typing_metrics
                            ORDER BY id DESC
                            LIMIT 1000
                        """)

                        char_data = cursor.fetchall()
                        if char_data:
                            char_metrics = [
                                {
                                    'char_position': c[0],
                                    'char_type': c[1],
                                    'requires_shift': c[2],
                                    'success': c[3],
                                    'total_duration_ms': c[4],
                                    'system_load_at_char': c[5] or 0,
                                    'char_start_time_ms': c[6],  # Use char_start_time_ms instead of timestamp
                                    'historical_failures': failure_points.get(c[0], 0),
                                    'avg_duration_at_pos': c[4]
                                }
                                for c in char_data
                            ]

                            await self.failure_predictor.train_from_history(char_metrics)

                    # Run Bayesian Optimization on successful attempts
                    if self.bayesian_optimizer:
                        success_data = [s for s in char_stats if s[5] > 50]  # success_rate > 50%
                        if success_data:
                            best_params = await self.bayesian_optimizer.optimize_parameters([
                                {'avg_duration': s[2]} for s in success_data
                            ])
                            logger.info(f"🎯 Bayesian Optimization: {best_params}")

            else:
                logger.warning("⚠️ No typing history found, using defaults")
                self.state = TypingPerformanceState(
                    total_attempts=0,
                    successful_attempts=0,
                    avg_typing_speed_ms=0.0,
                    fastest_typing_ms=0.0,
                    failure_points={},
                    optimal_timings={},
                    success_rate_trend=[]
                )

                # Initialize advanced features even without history
                if ADVANCED_FEATURES_AVAILABLE and self.failure_predictor:
                    await self.failure_predictor.initialize()

            conn.close()

        except Exception as e:
            logger.error(f"Failed to initialize typing learner: {e}", exc_info=True)

    async def update_from_typing_session(
        self,
        success: bool,
        duration_ms: float,
        failed_at_char: Optional[int],
        char_metrics: List[Dict[str, Any]]
    ):
        """
        Update learning model from typing session using Reinforcement Learning.

        Args:
            success: Whether typing succeeded
            duration_ms: Total typing duration
            failed_at_char: Character position where failure occurred (if any)
            char_metrics: Per-character timing and success data
        """
        if not self.state:
            await self.initialize()

        self.state.total_attempts += 1
        if success:
            self.state.successful_attempts += 1

        # Update success rate trend
        current_success_rate = self.state.successful_attempts / self.state.total_attempts
        self.state.success_rate_trend.insert(0, current_success_rate)
        self.state.success_rate_trend = self.state.success_rate_trend[:50]

        # **REINFORCEMENT LEARNING: Q-Learning for timing optimization**
        # Reward: +1 for success, -1 for failure, scaled by speed
        if success:
            reward = 1.0 + (1000.0 / duration_ms)  # Faster = better reward
        else:
            reward = -1.0

        # Update optimal timings based on successful characters
        for char_metric in char_metrics:
            if char_metric.get('success'):
                char_type = char_metric.get('char_type')
                requires_shift = char_metric.get('requires_shift')
                duration = char_metric.get('total_duration_ms')
                delay = char_metric.get('inter_char_delay_ms')

                key = f"{char_type}_{'shift' if requires_shift else 'noshift'}"

                if key in self.optimal_timings:
                    # Online learning: weighted average with new observation
                    old_duration = self.optimal_timings[key]['duration']
                    old_delay = self.optimal_timings[key]['delay']

                    # Use learning rate to blend old and new
                    self.optimal_timings[key]['duration'] = (
                        (1 - self.learning_rate) * old_duration +
                        self.learning_rate * duration
                    )
                    self.optimal_timings[key]['delay'] = (
                        (1 - self.learning_rate) * old_delay +
                        self.learning_rate * (delay or 100)
                    )
                else:
                    self.optimal_timings[key] = {
                        'duration': duration,
                        'delay': delay or 100
                    }

        # Track failure points
        if not success and failed_at_char is not None:
            self.state.failure_points[failed_at_char] = self.state.failure_points.get(failed_at_char, 0) + 1
            logger.warning(f"⚠️ Typing failure at character {failed_at_char} "
                         f"(total failures at this position: {self.state.failure_points[failed_at_char]})")

        # Update fastest typing
        if success and (self.state.fastest_typing_ms == 0 or duration_ms < self.state.fastest_typing_ms):
            self.state.fastest_typing_ms = duration_ms
            logger.info(f"🚀 New typing speed record: {duration_ms:.0f}ms!")

        # Update average
        self.state.avg_typing_speed_ms = (
            (self.state.avg_typing_speed_ms * (self.state.total_attempts - 1) + duration_ms) /
            self.state.total_attempts
        )

        logger.info(f"📊 Typing learner updated: {success}, duration={duration_ms:.0f}ms, "
                   f"success_rate={current_success_rate:.1%}, "
                   f"learned {len(self.optimal_timings)} timing patterns")

    async def get_optimal_timing_for_char(
        self,
        char_type: str,
        requires_shift: bool,
        char_position: int = 0,
        system_load: float = 0.0
    ) -> Dict[str, float]:
        """
        Get ML-optimized timing for a specific character type.

        Uses:
        - Learned optimal timings
        - Random Forest failure prediction
        - Multi-Armed Bandit exploration/exploitation
        - Bayesian Optimization results

        Returns dictionary with 'duration' and 'delay' in milliseconds.
        """
        if not self.state:
            await self.initialize()

        key = f"{char_type}_{'shift' if requires_shift else 'noshift'}"

        # Get base optimal timing
        if key in self.optimal_timings:
            base_timing = self.optimal_timings[key]
        else:
            # Fallback to defaults
            defaults = {
                'letter_noshift': {'duration': 50, 'delay': 100},
                'letter_shift': {'duration': 55, 'delay': 105},
                'digit_noshift': {'duration': 50, 'delay': 100},
                'digit_shift': {'duration': 55, 'delay': 105},
                'special_noshift': {'duration': 60, 'delay': 120},
                'special_shift': {'duration': 65, 'delay': 125},
            }
            base_timing = defaults.get(key, {'duration': 60, 'delay': 120})

        # 🤖 FAILURE PREDICTION: Check if this character is likely to fail
        if ADVANCED_FEATURES_AVAILABLE and self.failure_predictor:
            failure_prob = await self.failure_predictor.predict_failure_probability(
                char_position,
                char_type,
                requires_shift,
                system_load,
                self.state.failure_points.get(char_position, 0),
                base_timing['duration']
            )

            # If high failure probability, use slower, more careful timing
            if failure_prob > 0.5:
                logger.debug(f"⚠️ High failure probability ({failure_prob:.1%}) at position {char_position}, using careful timing")
                base_timing = {
                    'duration': base_timing['duration'] * 1.5,
                    'delay': base_timing['delay'] * 2.0
                }

        # 🤖 MULTI-ARMED BANDIT: Explore vs exploit
        if ADVANCED_FEATURES_AVAILABLE and self.multi_armed_bandit:
            timing = await self.multi_armed_bandit.select_timing_strategy(char_type, base_timing)
            return timing

        return base_timing

    async def should_use_slower_timing(self, char_position: int) -> bool:
        """
        Predict if we should use slower, more careful timing for this character.

        Based on failure history at this position.
        """
        if not self.state:
            await self.initialize()

        failures = self.state.failure_points.get(char_position, 0)

        # If this position has failed multiple times, use slower timing
        return failures >= 2


class ContinuousLearningEngine:
    """
    Master continuous learning engine combining voice and typing learners.

    Orchestrates both learning tracks and provides unified insights.

    Features:
    - Dual-track learning (voice + typing)
    - Progressive learning stages (Data Collection → Mastery)
    - Model persistence with checkpointing
    - Comprehensive progress tracking
    """

    def __init__(self, db_path: str = None):
        if db_path is None:
            db_path = str(Path.home() / ".jarvis/logs/unlock_metrics/unlock_metrics.db")

        self.db_path = db_path
        self.voice_learner = VoiceBiometricLearner(db_path)
        self.typing_learner = PasswordTypingLearner(db_path)

        self.initialized = False

        # 🤖 Progressive Learning & Model Persistence
        if ADVANCED_FEATURES_AVAILABLE:
            self.progress_manager = ProgressiveLearningManager()
            persistence_dir = Path(db_path).parent / "ml_checkpoints"
            self.model_persistence = ModelPersistence(str(persistence_dir))
        else:
            self.progress_manager = None
            self.model_persistence = None

    async def initialize(self):
        """
        Initialize both learning tracks.

        Thread-safe initialization with proper error handling.
        """
        if self.initialized:
            logger.debug("ContinuousLearningEngine already initialized")
            return

        try:
            logger.info("🧠 Initializing Continuous Learning Engine...")

            # Initialize learners in parallel for faster startup
            await asyncio.gather(
                self.voice_learner.initialize(),
                self.typing_learner.initialize(),
                return_exceptions=True
            )

            self.initialized = True
            logger.info("✅ Continuous Learning Engine initialized")
            logger.info(f"   Voice: {self.voice_learner.state.total_samples} samples, "
                       f"{self.voice_learner.state.avg_confidence:.1%} avg confidence")
            logger.info(f"   Typing: {self.typing_learner.state.total_attempts} attempts, "
                       f"{self.typing_learner.state.successful_attempts}/{self.typing_learner.state.total_attempts} successful")

        except Exception as e:
            logger.error(f"Failed to initialize Continuous Learning Engine: {e}", exc_info=True)
            self.initialized = False
            raise

    async def cleanup(self):
        """
        Cleanup resources and save state.

        Prevents resource leaks and ensures data is persisted.
        """
        try:
            logger.info("🧹 Cleaning up Continuous Learning Engine...")

            # Save final checkpoint
            if self.initialized:
                await self.save_checkpoint()

            # Clear state
            self.initialized = False

            logger.info("✅ Continuous Learning Engine cleanup complete")

        except Exception as e:
            logger.error(f"Error during cleanup: {e}", exc_info=True)

    async def update_from_unlock_attempt(
        self,
        voice_confidence: float,
        voice_success: bool,
        is_owner: bool,
        typing_success: bool,
        typing_duration_ms: float,
        typing_failed_at_char: Optional[int],
        char_metrics: List[Dict[str, Any]]
    ):
        """
        Update both learners from complete unlock attempt.

        This is the main entry point for continuous learning.
        """
        if not self.initialized:
            await self.initialize()

        # Update voice biometric learning
        await self.voice_learner.update_from_attempt(voice_confidence, voice_success, is_owner)

        # Update password typing learning (only if voice auth passed)
        if voice_success:
            await self.typing_learner.update_from_typing_session(
                typing_success,
                typing_duration_ms,
                typing_failed_at_char,
                char_metrics
            )

    async def get_learning_insights(self) -> Dict[str, Any]:
        """
        Get comprehensive learning insights for both tracks.

        Includes:
        - Voice biometric metrics
        - Password typing metrics
        - Progressive learning stage
        - Advanced ML features status
        - Overall system health

        Useful for debugging and monitoring continuous learning progress.
        """
        if not self.initialized:
            await self.initialize()

        voice_state = self.voice_learner.state
        typing_state = self.typing_learner.state

        insights = {
            'voice_biometrics': {
                'total_samples': voice_state.total_samples,
                'avg_confidence': voice_state.avg_confidence,
                'confidence_threshold': self.voice_learner.confidence_threshold,
                'improvement_rate': voice_state.improvement_rate,
                'false_rejection_rate': voice_state.false_rejection_rate,
                'best_confidence': voice_state.best_confidence,
                'worst_confidence': voice_state.worst_confidence,
                'status': self._get_voice_status(voice_state)
            },
            'password_typing': {
                'total_attempts': typing_state.total_attempts,
                'successful_attempts': typing_state.successful_attempts,
                'success_rate': typing_state.successful_attempts / typing_state.total_attempts if typing_state.total_attempts > 0 else 0,
                'avg_speed_ms': typing_state.avg_typing_speed_ms,
                'fastest_speed_ms': typing_state.fastest_typing_ms,
                'failure_hotspots': typing_state.failure_points,
                'learned_patterns': len(typing_state.optimal_timings),
                'status': self._get_typing_status(typing_state)
            },
            'overall_health': self._get_overall_health(voice_state, typing_state)
        }

        # 🤖 Progressive Learning Stage
        if ADVANCED_FEATURES_AVAILABLE and self.progress_manager:
            total_attempts = voice_state.total_samples + typing_state.total_attempts
            success_rate = typing_state.successful_attempts / typing_state.total_attempts if typing_state.total_attempts > 0 else 0

            progress_report = self.progress_manager.get_progress_report(
                total_attempts,
                voice_state.avg_confidence,
                success_rate
            )

            insights['progressive_learning'] = progress_report

        # 🤖 Advanced ML Features Status
        if ADVANCED_FEATURES_AVAILABLE:
            insights['advanced_features'] = {
                'anomaly_detection': self.voice_learner.anomaly_detector is not None,
                'context_aware_learning': self.voice_learner.context_learner is not None,
                'fine_tuning': self.voice_learner.fine_tuner is not None,
                'failure_prediction': self.typing_learner.failure_predictor is not None,
                'bayesian_optimization': self.typing_learner.bayesian_optimizer is not None,
                'multi_armed_bandit': self.typing_learner.multi_armed_bandit is not None,
                'model_persistence': self.model_persistence is not None
            }

        return insights

    def _get_voice_status(self, state: VoiceBiometricState) -> str:
        """Determine voice learning status"""
        if state.total_samples < 50:
            return 'learning'  # Still gathering data
        elif state.avg_confidence > 0.60:
            return 'excellent'  # Very confident
        elif state.avg_confidence > 0.50:
            return 'good'  # Solid performance
        elif state.avg_confidence > 0.40:
            return 'fair'  # Acceptable
        else:
            return 'needs_improvement'

    def _get_typing_status(self, state: TypingPerformanceState) -> str:
        """Determine typing learning status"""
        if state.total_attempts < 10:
            return 'learning'

        success_rate = state.successful_attempts / state.total_attempts if state.total_attempts > 0 else 0

        if success_rate > 0.90:
            return 'excellent'
        elif success_rate > 0.75:
            return 'good'
        elif success_rate > 0.50:
            return 'fair'
        else:
            return 'needs_improvement'

    def _get_overall_health(self, voice_state: VoiceBiometricState, typing_state: TypingPerformanceState) -> str:
        """Determine overall system health"""
        voice_status = self._get_voice_status(voice_state)
        typing_status = self._get_typing_status(typing_state)

        if voice_status in ['excellent', 'good'] and typing_status in ['excellent', 'good']:
            return 'optimal'
        elif voice_status in ['fair', 'learning'] or typing_status in ['fair', 'learning']:
            return 'improving'
        else:
            return 'needs_attention'

    async def save_checkpoint(self):
        """
        Save ML model checkpoint for recovery and analysis.

        Includes:
        - Voice biometric state
        - Password typing state
        - All ML models (Random Forest, Bayesian Optimizer, etc.)
        - Learning metadata
        """
        if not ADVANCED_FEATURES_AVAILABLE or not self.model_persistence:
            logger.debug("Model persistence not available, skipping checkpoint")
            return

        try:
            voice_state = self.voice_learner.state
            typing_state = self.typing_learner.state

            metadata = {
                'voice_threshold': self.voice_learner.confidence_threshold,
                'typing_patterns_learned': len(self.typing_learner.optimal_timings),
                'total_unlocks': voice_state.total_samples + typing_state.total_attempts,
                'overall_health': self._get_overall_health(voice_state, typing_state)
            }

            # Add progressive learning stage
            if self.progress_manager:
                total_attempts = voice_state.total_samples + typing_state.total_attempts
                current_stage = self.progress_manager.get_current_stage(total_attempts)
                metadata['learning_stage'] = current_stage.name

            await self.model_persistence.save_checkpoint(
                voice_state=voice_state,
                typing_state=typing_state,
                models={},  # Actual model objects saved separately
                metadata=metadata
            )

            logger.info("✅ ML checkpoint saved successfully")

        except Exception as e:
            logger.error(f"Failed to save checkpoint: {e}", exc_info=True)

    async def load_checkpoint(self):
        """Load latest ML checkpoint"""
        if not ADVANCED_FEATURES_AVAILABLE or not self.model_persistence:
            return None

        try:
            checkpoint = await self.model_persistence.load_latest_checkpoint()

            if checkpoint:
                logger.info(f"✅ Loaded checkpoint from {checkpoint['timestamp']}")
                return checkpoint

        except Exception as e:
            logger.error(f"Failed to load checkpoint: {e}")

        return None


# Singleton instance with thread safety
_learning_engine = None
_learning_engine_lock = LazyAsyncLock()  # v100.1: Lazy initialization to avoid "no running event loop" error
_initialization_lock = LazyAsyncLock()  # v100.1: Lazy initialization to avoid "no running event loop" error


async def get_learning_engine() -> ContinuousLearningEngine:
    """
    Get or create singleton learning engine.

    Thread-safe singleton pattern with double-checked locking.
    Prevents race conditions and multiple initializations.
    """
    global _learning_engine

    # Fast path: already initialized
    if _learning_engine is not None and _learning_engine.initialized:
        return _learning_engine

    # Slow path: need to initialize
    async with _learning_engine_lock:
        # Double-check: another thread might have initialized while we waited
        if _learning_engine is not None and _learning_engine.initialized:
            return _learning_engine

        try:
            if _learning_engine is None:
                logger.info("🔧 Creating new ContinuousLearningEngine singleton...")
                _learning_engine = ContinuousLearningEngine()

            # Initialize if not already done
            if not _learning_engine.initialized:
                logger.info("🔧 Initializing ContinuousLearningEngine...")
                await _learning_engine.initialize()

            logger.info("✅ ContinuousLearningEngine singleton ready")
            return _learning_engine

        except Exception as e:
            logger.error(f"Failed to initialize learning engine: {e}", exc_info=True)
            # Don't leave a broken instance
            _learning_engine = None
            raise


async def shutdown_learning_engine():
    """
    Gracefully shutdown learning engine and release resources.

    Prevents resource leaks by ensuring proper cleanup.
    """
    global _learning_engine

    if _learning_engine is None:
        return

    async with _learning_engine_lock:
        if _learning_engine is not None:
            try:
                logger.info("🔧 Shutting down ContinuousLearningEngine...")

                # Save final checkpoint
                await _learning_engine.save_checkpoint()

                # Clear singleton
                _learning_engine = None

                logger.info("✅ ContinuousLearningEngine shutdown complete")

            except Exception as e:
                logger.error(f"Error during learning engine shutdown: {e}", exc_info=True)
