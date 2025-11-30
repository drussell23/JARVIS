#!/usr/bin/env python3
"""
Advanced ML Features for JARVIS Continuous Learning
===================================================

This module provides advanced ML capabilities:
1. Random Forest for failure prediction
2. Bayesian Optimization for hyperparameter tuning
3. Multi-Armed Bandit for exploration/exploitation
4. Context-Aware Learning (time of day, voice variations)
5. Anomaly Detection (One-Class SVM / Isolation Forest)
6. Fine-Tuning mechanism for speaker embeddings
7. Progressive Learning Stages

All features are:
- Fully async
- Dynamic (no hardcoding)
- Robust with error handling
- Database-backed for persistence
"""

import asyncio
import logging
import numpy as np
import pickle
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple
from pathlib import Path
from dataclasses import dataclass, asdict, field
import sqlite3
import json

logger = logging.getLogger(__name__)


# ==================== Random Forest Failure Predictor ====================

class FailurePredictor:
    """
    Random Forest classifier to predict character typing failures.

    Learns patterns like:
    - Which character positions are problematic
    - When system load affects typing
    - Time-of-day correlation with failures
    """

    def __init__(self, model_path: str):
        self.model_path = Path(model_path)
        self.model = None
        self.feature_names = [
            'char_position',
            'char_type_encoded',  # 0=letter, 1=digit, 2=special
            'requires_shift',
            'system_load',
            'hour_of_day',
            'day_of_week',
            'historical_failures_at_position',
            'avg_duration_at_position'
        ]

        # Lazy import to avoid dependency issues
        try:
            from sklearn.ensemble import RandomForestClassifier
            self.RandomForestClassifier = RandomForestClassifier
        except ImportError:
            logger.warning("⚠️ scikit-learn not available, failure prediction disabled")
            self.RandomForestClassifier = None

    async def initialize(self):
        """Load or create Random Forest model"""
        if not self.RandomForestClassifier:
            return

        if self.model_path.exists():
            try:
                with open(self.model_path, 'rb') as f:
                    self.model = pickle.load(f)
                logger.info(f"✅ Loaded Random Forest model from {self.model_path}")
            except Exception as e:
                logger.warning(f"Failed to load model: {e}, creating new one")
                self._create_new_model()
        else:
            self._create_new_model()

    def _create_new_model(self):
        """Create new Random Forest classifier"""
        if not self.RandomForestClassifier:
            return

        self.model = self.RandomForestClassifier(
            n_estimators=100,
            max_depth=10,
            min_samples_split=5,
            class_weight='balanced',
            random_state=42
        )
        logger.info("✅ Created new Random Forest model")

    async def train_from_history(self, char_metrics: List[Dict[str, Any]]):
        """
        Train model on historical character typing data.

        Features:
        - Character position (1-N)
        - Character type (letter/digit/special)
        - Requires shift
        - System load
        - Time of day
        - Historical failure count
        """
        if not self.model or len(char_metrics) < 50:
            logger.debug("Not enough data to train Random Forest (need 50+ samples)")
            return

        try:
            X = []
            y = []

            for metric in char_metrics:
                features = [
                    metric.get('char_position', 0),
                    self._encode_char_type(metric.get('char_type', 'letter')),
                    1 if metric.get('requires_shift') else 0,
                    metric.get('system_load_at_char', 0) or 0,
                    datetime.fromisoformat(metric.get('timestamp', datetime.now().isoformat())).hour,
                    datetime.fromisoformat(metric.get('timestamp', datetime.now().isoformat())).weekday(),
                    metric.get('historical_failures', 0),
                    metric.get('avg_duration_at_pos', 50)
                ]

                X.append(features)
                y.append(0 if metric.get('success') else 1)  # 1 = failure

            X = np.array(X)
            y = np.array(y)

            # Train model
            self.model.fit(X, y)

            # Save model
            self.model_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.model_path, 'wb') as f:
                pickle.dump(self.model, f)

            logger.info(f"✅ Random Forest trained on {len(X)} samples")

        except Exception as e:
            logger.error(f"Failed to train Random Forest: {e}", exc_info=True)

    def _encode_char_type(self, char_type: str) -> int:
        """Encode character type as integer"""
        return {'letter': 0, 'digit': 1, 'special': 2}.get(char_type, 0)

    async def predict_failure_probability(
        self,
        char_position: int,
        char_type: str,
        requires_shift: bool,
        system_load: float,
        historical_failures: int,
        avg_duration: float
    ) -> float:
        """
        Predict probability of failure for this character.

        Returns: float between 0.0 (won't fail) and 1.0 (likely to fail)
        """
        if not self.model:
            return 0.0

        try:
            now = datetime.now()
            features = np.array([[
                char_position,
                self._encode_char_type(char_type),
                1 if requires_shift else 0,
                system_load,
                now.hour,
                now.weekday(),
                historical_failures,
                avg_duration
            ]])

            # Predict probability of failure (class 1)
            proba = self.model.predict_proba(features)[0][1]

            return proba

        except Exception as e:
            logger.error(f"Prediction failed: {e}")
            return 0.0


# ==================== Bayesian Optimization ====================

class BayesianTimingOptimizer:
    """
    Uses Bayesian Optimization to find optimal typing timing parameters.

    Much more efficient than grid search - intelligently explores parameter space.
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.best_params = {
            'key_duration': 50.0,
            'inter_char_delay': 100.0,
            'shift_duration': 30.0
        }
        self.exploration_history = []

        # Bayesian optimization parameters
        self.n_init = 10  # Random exploration first
        self.n_iter = 50  # Then optimize

        # Try to import bayesian-optimization
        try:
            from bayes_opt import BayesianOptimization
            self.BayesianOptimization = BayesianOptimization
        except ImportError:
            logger.warning("⚠️ bayesian-optimization not available, using grid search fallback")
            self.BayesianOptimization = None

    async def optimize_parameters(
        self,
        success_data: List[Dict[str, Any]]
    ) -> Dict[str, float]:
        """
        Find optimal timing parameters using Bayesian Optimization.

        Returns best parameters found.
        """
        if not self.BayesianOptimization or len(success_data) < 20:
            logger.debug("Not enough data for Bayesian Optimization")
            return self.best_params

        try:
            # Define objective function (what we want to maximize)
            def objective(key_duration, inter_char_delay, shift_duration):
                # Simulate success rate with these parameters
                # In practice, this would use historical data
                score = 0.0
                for data in success_data:
                    # Calculate how close our params are to successful attempts
                    param_distance = abs(data.get('avg_duration', 50) - key_duration)
                    if param_distance < 10:
                        score += 1.0

                return score / len(success_data) if success_data else 0.0

            # Define parameter bounds
            pbounds = {
                'key_duration': (30, 100),
                'inter_char_delay': (50, 200),
                'shift_duration': (20, 50)
            }

            # Run optimization
            optimizer = self.BayesianOptimization(
                f=objective,
                pbounds=pbounds,
                random_state=42,
                verbose=0
            )

            optimizer.maximize(
                init_points=self.n_init,
                n_iter=self.n_iter
            )

            # Get best parameters
            self.best_params = {
                'key_duration': optimizer.max['params']['key_duration'],
                'inter_char_delay': optimizer.max['params']['inter_char_delay'],
                'shift_duration': optimizer.max['params']['shift_duration']
            }

            logger.info(f"🎯 Bayesian Optimization found: {self.best_params}")

            return self.best_params

        except Exception as e:
            logger.error(f"Bayesian Optimization failed: {e}", exc_info=True)
            return self.best_params


# ==================== Multi-Armed Bandit ====================

class MultiArmedBandit:
    """
    Balances exploration (trying new timings) vs exploitation (using known good timings).

    Uses epsilon-greedy strategy:
    - 90% of time: Use optimal timing (exploitation)
    - 10% of time: Try random variation (exploration)
    """

    def __init__(self, epsilon: float = 0.1):
        self.epsilon = epsilon  # Exploration rate
        self.arms = {}  # timing_variant -> (total_reward, num_pulls)
        self.best_arm = None

    async def select_timing_strategy(
        self,
        char_type: str,
        optimal_timing: Dict[str, float]
    ) -> Dict[str, float]:
        """
        Choose timing strategy: explore new timings or exploit known good ones.

        Returns: timing parameters to use
        """
        import random

        # Epsilon-greedy selection
        if random.random() < self.epsilon:
            # EXPLORE: Try random variation
            variation = random.uniform(0.8, 1.2)
            timing = {
                'duration': optimal_timing['duration'] * variation,
                'delay': optimal_timing['delay'] * variation
            }
            logger.debug(f"🔍 Exploring new timing for {char_type}: {variation:.2f}x")
        else:
            # EXPLOIT: Use optimal timing
            timing = optimal_timing.copy()

        return timing

    async def update_reward(
        self,
        timing_key: str,
        success: bool,
        speed: float
    ):
        """
        Update bandit with reward from this timing choice.

        Reward = success + speed_bonus
        """
        reward = 1.0 if success else 0.0
        if success and speed < 2000:  # Fast unlock
            reward += 0.5

        if timing_key not in self.arms:
            self.arms[timing_key] = (0.0, 0)

        total_reward, num_pulls = self.arms[timing_key]
        self.arms[timing_key] = (total_reward + reward, num_pulls + 1)

        # Update best arm
        best_avg = max((r/n for r, n in self.arms.values() if n > 0), default=0.0)
        for key, (r, n) in self.arms.items():
            if n > 0 and r/n == best_avg:
                self.best_arm = key
                break


# ==================== Context-Aware Learning ====================

@dataclass
class VoiceContext:
    """Context information for voice biometric learning"""
    time_of_day: str  # 'morning', 'afternoon', 'evening', 'night'
    day_of_week: str
    is_weekend: bool
    estimated_voice_condition: str  # 'fresh', 'tired', 'normal'
    background_noise_level: float  # 0.0 to 1.0


class ContextAwareLearner:
    """
    Learns context-specific patterns for voice biometrics.

    Examples:
    - Derek's voice is lower in the morning
    - Confidence is higher in quiet environments
    - Voice changes when tired
    """

    def __init__(self):
        self.context_patterns = {}  # context_key -> avg_confidence

    def get_context(self, audio_quality: float) -> VoiceContext:
        """Extract context from current environment"""
        now = datetime.now()
        hour = now.hour

        # Determine time of day
        if 5 <= hour < 12:
            time_of_day = 'morning'
        elif 12 <= hour < 17:
            time_of_day = 'afternoon'
        elif 17 <= hour < 21:
            time_of_day = 'evening'
        else:
            time_of_day = 'night'

        # Estimate voice condition based on time
        if hour < 7 or hour > 22:
            voice_condition = 'tired'
        elif 9 <= hour <= 16:
            voice_condition = 'fresh'
        else:
            voice_condition = 'normal'

        return VoiceContext(
            time_of_day=time_of_day,
            day_of_week=now.strftime('%A'),
            is_weekend=now.weekday() >= 5,
            estimated_voice_condition=voice_condition,
            background_noise_level=1.0 - audio_quality
        )

    async def update_context_pattern(
        self,
        context: VoiceContext,
        confidence: float,
        success: bool
    ):
        """Learn how context affects voice recognition"""
        context_key = f"{context.time_of_day}_{context.estimated_voice_condition}"

        if context_key not in self.context_patterns:
            self.context_patterns[context_key] = []

        self.context_patterns[context_key].append(confidence)

        # Keep last 20 per context
        self.context_patterns[context_key] = self.context_patterns[context_key][-20:]

        avg = np.mean(self.context_patterns[context_key])
        logger.debug(f"📊 Context pattern {context_key}: avg confidence {avg:.1%}")

    async def get_expected_confidence(self, context: VoiceContext) -> float:
        """Predict expected confidence for this context"""
        context_key = f"{context.time_of_day}_{context.estimated_voice_condition}"

        if context_key in self.context_patterns and self.context_patterns[context_key]:
            return np.mean(self.context_patterns[context_key])

        # Default expectation
        return 0.50


# ==================== Anomaly Detection ====================

class AnomalyDetector:
    """
    Detects unusual authentication patterns that could indicate:
    - Spoofing attempts
    - Unauthorized access
    - System issues

    Uses Isolation Forest algorithm.
    """

    def __init__(self, model_path: str):
        self.model_path = Path(model_path)
        self.model = None

        try:
            from sklearn.ensemble import IsolationForest
            self.IsolationForest = IsolationForest
        except ImportError:
            logger.warning("⚠️ scikit-learn not available, anomaly detection disabled")
            self.IsolationForest = None

    async def initialize(self):
        """Load or create Isolation Forest model"""
        if not self.IsolationForest:
            return

        if self.model_path.exists():
            try:
                with open(self.model_path, 'rb') as f:
                    self.model = pickle.load(f)
                logger.info(f"✅ Loaded Isolation Forest from {self.model_path}")
            except Exception as e:
                logger.warning(f"Failed to load anomaly detector: {e}")
                self._create_new_model()
        else:
            self._create_new_model()

    def _create_new_model(self):
        """Create new Isolation Forest"""
        if not self.IsolationForest:
            return

        self.model = self.IsolationForest(
            contamination=0.1,  # Expect 10% anomalies
            random_state=42
        )
        logger.info("✅ Created new Isolation Forest for anomaly detection")

    async def train(self, historical_attempts: List[Dict[str, Any]]):
        """Train on normal authentication patterns"""
        if not self.model or len(historical_attempts) < 30:
            return

        try:
            # Extract features
            X = []
            for attempt in historical_attempts:
                features = [
                    attempt.get('speaker_confidence', 0),
                    attempt.get('stt_confidence', 0),
                    attempt.get('audio_duration_ms', 0),
                    attempt.get('processing_time_ms', 0),
                    datetime.fromisoformat(attempt.get('timestamp', datetime.now().isoformat())).hour
                ]
                X.append(features)

            X = np.array(X)
            self.model.fit(X)

            # Save model
            self.model_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.model_path, 'wb') as f:
                pickle.dump(self.model, f)

            logger.info(f"✅ Anomaly detector trained on {len(X)} samples")

        except Exception as e:
            logger.error(f"Anomaly detection training failed: {e}", exc_info=True)

    async def detect_anomaly(
        self,
        speaker_confidence: float,
        stt_confidence: float,
        audio_duration_ms: float,
        processing_time_ms: float
    ) -> Tuple[bool, float]:
        """
        Detect if this attempt is anomalous.

        Returns: (is_anomaly, anomaly_score)
        """
        if not self.model:
            return False, 0.0

        try:
            now = datetime.now()
            features = np.array([[
                speaker_confidence,
                stt_confidence,
                audio_duration_ms,
                processing_time_ms,
                now.hour
            ]])

            # Predict (-1 = anomaly, 1 = normal)
            prediction = self.model.predict(features)[0]
            score = self.model.score_samples(features)[0]

            is_anomaly = prediction == -1

            if is_anomaly:
                logger.warning(f"🚨 Anomaly detected! Score: {score:.3f}")

            return is_anomaly, abs(score)

        except Exception as e:
            logger.error(f"Anomaly detection failed: {e}")
            return False, 0.0


# ==================== Fine-Tuning for Speaker Embeddings ====================

class SpeakerEmbeddingFineTuner:
    """
    Fine-tunes speaker embeddings for improved accuracy.

    Uses online learning to adapt embeddings to Derek's voice variations.
    """

    def __init__(self, learning_rate: float = 0.001):
        self.learning_rate = learning_rate
        self.base_embedding = None
        self.adapted_embedding = None
        self.adaptation_count = 0

    async def initialize(self, base_embedding: np.ndarray):
        """Initialize with base speaker embedding"""
        self.base_embedding = base_embedding.copy()
        self.adapted_embedding = base_embedding.copy()
        logger.info(f"✅ Fine-tuner initialized with {len(base_embedding)}D embedding")

    async def fine_tune(
        self,
        new_embedding: np.ndarray,
        confidence: float,
        success: bool
    ):
        """
        Fine-tune embedding using online learning.

        Higher confidence samples have more influence.
        """
        if self.adapted_embedding is None:
            self.adapted_embedding = new_embedding.copy()
            return

        # Weight by confidence and success
        weight = confidence if success else confidence * 0.5
        effective_lr = self.learning_rate * weight

        # Online gradient descent update
        self.adapted_embedding = (
            (1 - effective_lr) * self.adapted_embedding +
            effective_lr * new_embedding
        )

        self.adaptation_count += 1

        if self.adaptation_count % 10 == 0:
            # Calculate adaptation distance
            distance = np.linalg.norm(self.adapted_embedding - self.base_embedding)
            logger.info(f"🎯 Embedding fine-tuned {self.adaptation_count} times, "
                       f"drift: {distance:.3f}")

    async def get_adapted_embedding(self) -> np.ndarray:
        """Get current fine-tuned embedding"""
        return self.adapted_embedding.copy() if self.adapted_embedding is not None else None


# ==================== Progressive Learning Stages ====================

@dataclass
class LearningStage:
    """Represents a stage in progressive learning"""
    name: str
    min_attempts: int
    max_attempts: int
    target_confidence: float
    target_success_rate: float
    description: str


class ProgressiveLearningManager:
    """
    Manages progressive learning stages:
    1. Data Collection (0-20 attempts)
    2. Pattern Recognition (20-50 attempts)
    3. Optimization (50-100 attempts)
    4. Mastery (100+ attempts)
    """

    def __init__(self):
        self.stages = [
            LearningStage(
                name='data_collection',
                min_attempts=0,
                max_attempts=20,
                target_confidence=0.40,
                target_success_rate=0.60,
                description='Learning Derek\'s voice patterns'
            ),
            LearningStage(
                name='pattern_recognition',
                min_attempts=20,
                max_attempts=50,
                target_confidence=0.50,
                target_success_rate=0.75,
                description='Recognizing voice patterns and optimizing threshold'
            ),
            LearningStage(
                name='optimization',
                min_attempts=50,
                max_attempts=100,
                target_confidence=0.55,
                target_success_rate=0.85,
                description='Fine-tuning for optimal performance'
            ),
            LearningStage(
                name='mastery',
                min_attempts=100,
                max_attempts=float('inf'),
                target_confidence=0.60,
                target_success_rate=0.95,
                description='Near-perfect recognition and typing'
            )
        ]

    def get_current_stage(self, total_attempts: int) -> LearningStage:
        """Get current learning stage based on attempt count"""
        for stage in self.stages:
            if stage.min_attempts <= total_attempts < stage.max_attempts:
                return stage

        return self.stages[-1]  # Mastery

    def get_progress_report(
        self,
        total_attempts: int,
        avg_confidence: float,
        success_rate: float
    ) -> Dict[str, Any]:
        """Get detailed progress report"""
        stage = self.get_current_stage(total_attempts)

        confidence_progress = (avg_confidence / stage.target_confidence) * 100
        success_progress = (success_rate / stage.target_success_rate) * 100
        overall_progress = (confidence_progress + success_progress) / 2

        return {
            'current_stage': stage.name,
            'stage_description': stage.description,
            'total_attempts': total_attempts,
            'attempts_in_stage': total_attempts - stage.min_attempts,
            'attempts_to_next_stage': max(0, stage.max_attempts - total_attempts),
            'confidence_progress': min(100, confidence_progress),
            'success_rate_progress': min(100, success_progress),
            'overall_progress': min(100, overall_progress),
            'targets': {
                'confidence': stage.target_confidence,
                'success_rate': stage.target_success_rate
            },
            'actual': {
                'confidence': avg_confidence,
                'success_rate': success_rate
            },
            'status': self._get_status(overall_progress)
        }

    def _get_status(self, progress: float) -> str:
        """Get status message based on progress"""
        if progress >= 90:
            return 'excellent'
        elif progress >= 70:
            return 'good'
        elif progress >= 50:
            return 'fair'
        else:
            return 'learning'


# ==================== Model Persistence ====================

class ModelPersistence:
    """
    Handles saving and loading of all ML models.

    Provides checkpoint functionality for recovery.
    """

    def __init__(self, base_path: str):
        self.base_path = Path(base_path)
        self.base_path.mkdir(parents=True, exist_ok=True)

    async def save_checkpoint(
        self,
        voice_state: Any,
        typing_state: Any,
        models: Dict[str, Any],
        metadata: Dict[str, Any]
    ):
        """Save complete checkpoint of learning state"""
        try:
            checkpoint = {
                'timestamp': datetime.now().isoformat(),
                'voice_state': asdict(voice_state) if hasattr(voice_state, '__dict__') else voice_state,
                'typing_state': asdict(typing_state) if hasattr(typing_state, '__dict__') else typing_state,
                'metadata': metadata
            }

            checkpoint_path = self.base_path / f"checkpoint_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

            with open(checkpoint_path, 'w') as f:
                json.dump(checkpoint, f, indent=2)

            logger.info(f"✅ Checkpoint saved: {checkpoint_path}")

            # Keep only last 10 checkpoints
            await self._cleanup_old_checkpoints()

        except Exception as e:
            logger.error(f"Failed to save checkpoint: {e}", exc_info=True)

    async def load_latest_checkpoint(self) -> Optional[Dict[str, Any]]:
        """Load most recent checkpoint"""
        try:
            checkpoints = sorted(self.base_path.glob("checkpoint_*.json"))

            if not checkpoints:
                return None

            latest = checkpoints[-1]

            with open(latest, 'r') as f:
                checkpoint = json.load(f)

            logger.info(f"✅ Loaded checkpoint from {checkpoint['timestamp']}")

            return checkpoint

        except Exception as e:
            logger.error(f"Failed to load checkpoint: {e}")
            return None

    async def _cleanup_old_checkpoints(self, keep_last: int = 10):
        """Remove old checkpoints, keeping only recent ones"""
        try:
            checkpoints = sorted(self.base_path.glob("checkpoint_*.json"))

            if len(checkpoints) > keep_last:
                for old_checkpoint in checkpoints[:-keep_last]:
                    old_checkpoint.unlink()
                    logger.debug(f"Removed old checkpoint: {old_checkpoint.name}")

        except Exception as e:
            logger.error(f"Checkpoint cleanup failed: {e}")


# ==================== AAM-Softmax Loss for Speaker Verification ====================

class AAMSoftmaxLoss:
    """
    Additive Angular Margin Softmax (ArcFace) Loss for Speaker Verification.

    Encourages:
    - Small angular distance within same speaker (tight Derek cluster)
    - Large angular margin between different speakers

    The margin penalty forces the model to learn more discriminative embeddings.

    Loss = -log(exp(s*(cos(theta_y + m))) / (exp(s*(cos(theta_y + m))) + sum(exp(s*cos(theta_j)))))

    Where:
    - s: scale factor (controls the "temperature" of softmax)
    - m: additive angular margin (makes same-speaker predictions harder)
    - theta_y: angle between embedding and class center for correct class
    - theta_j: angles for other classes
    """

    def __init__(
        self,
        embedding_dim: int = 192,
        num_classes: int = 2,  # Derek vs non-Derek (can expand)
        scale: float = 30.0,
        margin: float = 0.35,
        easy_margin: bool = False
    ):
        self.embedding_dim = embedding_dim
        self.num_classes = num_classes
        self.scale = scale  # Scale factor (s)
        self.margin = margin  # Angular margin (m)
        self.easy_margin = easy_margin

        # Class weight matrix (learned centers for each class)
        self.weight = np.random.randn(num_classes, embedding_dim) * 0.01
        # Normalize weights
        self._normalize_weights()

        # Precompute margin trigonometry
        self.cos_m = np.cos(margin)
        self.sin_m = np.sin(margin)
        self.th = np.cos(np.pi - margin)  # Threshold for hard margin
        self.mm = np.sin(np.pi - margin) * margin  # Monotonicity margin

        # Training statistics
        self.loss_history: List[float] = []
        self.accuracy_history: List[float] = []

        logger.info(f"✅ AAM-Softmax Loss initialized: dim={embedding_dim}, "
                   f"classes={num_classes}, scale={scale}, margin={margin}")

    def _normalize_weights(self):
        """Normalize weight matrix to unit vectors (project to unit hypersphere)."""
        norms = np.linalg.norm(self.weight, axis=1, keepdims=True)
        self.weight = self.weight / np.maximum(norms, 1e-10)

    def compute_logits(
        self,
        embeddings: np.ndarray,
        labels: np.ndarray
    ) -> Tuple[np.ndarray, np.ndarray]:
        """
        Compute AAM-Softmax logits with angular margin penalty.

        Args:
            embeddings: (batch_size, embedding_dim) normalized embeddings
            labels: (batch_size,) class labels

        Returns:
            Tuple of (modified_logits, original_cosine_similarities)
        """
        # Normalize embeddings to unit length
        emb_norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
        normalized_emb = embeddings / np.maximum(emb_norms, 1e-10)

        # Compute cosine similarities (dot product of normalized vectors)
        # Shape: (batch_size, num_classes)
        cosine = np.dot(normalized_emb, self.weight.T)

        # Clip cosine to avoid numerical issues
        cosine = np.clip(cosine, -1.0 + 1e-7, 1.0 - 1e-7)

        # For each sample, apply margin to the target class
        # Get cosine for target class
        batch_size = embeddings.shape[0]
        target_cosine = cosine[np.arange(batch_size), labels]

        # Convert to angle and add margin
        # cos(theta + m) = cos(theta)*cos(m) - sin(theta)*sin(m)
        sine = np.sqrt(1.0 - target_cosine ** 2)
        phi = target_cosine * self.cos_m - sine * self.sin_m

        if self.easy_margin:
            # Easy margin: only apply when cos(theta) > 0
            phi = np.where(target_cosine > 0, phi, target_cosine)
        else:
            # Hard margin: monotonic decreasing
            phi = np.where(target_cosine > self.th, phi, target_cosine - self.mm)

        # Create one-hot mask and apply phi only to target class
        one_hot = np.zeros_like(cosine)
        one_hot[np.arange(batch_size), labels] = 1.0

        # Modified logits: target class gets phi, others keep cosine
        modified_cosine = (1.0 - one_hot) * cosine + one_hot * phi.reshape(-1, 1)

        # Scale by s
        logits = self.scale * modified_cosine

        return logits, cosine

    def compute_loss(
        self,
        embeddings: np.ndarray,
        labels: np.ndarray
    ) -> Tuple[float, float]:
        """
        Compute AAM-Softmax cross-entropy loss.

        Args:
            embeddings: (batch_size, embedding_dim)
            labels: (batch_size,) integer class labels

        Returns:
            Tuple of (loss, accuracy)
        """
        logits, cosine = self.compute_logits(embeddings, labels)

        # Softmax cross-entropy loss
        # Numerically stable: subtract max for stability
        shifted_logits = logits - np.max(logits, axis=1, keepdims=True)
        exp_logits = np.exp(shifted_logits)
        softmax_probs = exp_logits / np.sum(exp_logits, axis=1, keepdims=True)

        # Cross-entropy
        batch_size = embeddings.shape[0]
        ce_loss = -np.mean(np.log(softmax_probs[np.arange(batch_size), labels] + 1e-10))

        # Accuracy
        predictions = np.argmax(logits, axis=1)
        accuracy = np.mean(predictions == labels)

        # Track history
        self.loss_history.append(ce_loss)
        self.accuracy_history.append(accuracy)

        return ce_loss, accuracy

    def update_weights(
        self,
        embeddings: np.ndarray,
        labels: np.ndarray,
        learning_rate: float = 0.001
    ):
        """
        Update class centers using gradient descent.

        This is a simplified version - in production, use PyTorch/TensorFlow
        with automatic differentiation.
        """
        # Normalize embeddings
        emb_norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
        normalized_emb = embeddings / np.maximum(emb_norms, 1e-10)

        # For each class, update the center towards the mean of its samples
        for class_idx in range(self.num_classes):
            class_mask = labels == class_idx
            if np.sum(class_mask) > 0:
                class_embeddings = normalized_emb[class_mask]
                class_mean = np.mean(class_embeddings, axis=0)

                # Move center towards class mean
                self.weight[class_idx] = (
                    (1 - learning_rate) * self.weight[class_idx] +
                    learning_rate * class_mean
                )

        # Re-normalize weights
        self._normalize_weights()


# ==================== Center Loss for Compact Embeddings ====================

class CenterLoss:
    """
    Center Loss for intra-class compactness.

    Minimizes the distance between embeddings and their class centers,
    creating a tight "Derek cluster" in embedding space.

    Loss = (1/2) * sum(||x_i - c_yi||^2)

    Where:
    - x_i: embedding of sample i
    - c_yi: center of class yi

    Used alongside softmax/AAM-softmax for joint training.
    """

    def __init__(
        self,
        embedding_dim: int = 192,
        num_classes: int = 2,
        alpha: float = 0.5  # Learning rate for center updates
    ):
        self.embedding_dim = embedding_dim
        self.num_classes = num_classes
        self.alpha = alpha

        # Initialize centers (one per class)
        self.centers = np.zeros((num_classes, embedding_dim))
        self.center_counts = np.zeros(num_classes)  # Track samples per center

        # Statistics
        self.loss_history: List[float] = []

        logger.info(f"✅ Center Loss initialized: dim={embedding_dim}, classes={num_classes}")

    def compute_loss(
        self,
        embeddings: np.ndarray,
        labels: np.ndarray
    ) -> float:
        """
        Compute center loss for a batch.

        Args:
            embeddings: (batch_size, embedding_dim)
            labels: (batch_size,) class labels

        Returns:
            Center loss value
        """
        batch_size = embeddings.shape[0]

        # Get centers for each sample's class
        batch_centers = self.centers[labels]

        # Compute L2 distance to centers
        diff = embeddings - batch_centers
        distances = np.sum(diff ** 2, axis=1)

        # Mean center loss
        loss = 0.5 * np.mean(distances)

        self.loss_history.append(loss)

        return loss

    def update_centers(
        self,
        embeddings: np.ndarray,
        labels: np.ndarray
    ):
        """
        Update class centers based on new embeddings.

        Uses momentum-based updates for stability.
        """
        for class_idx in range(self.num_classes):
            class_mask = labels == class_idx
            if np.sum(class_mask) > 0:
                class_embeddings = embeddings[class_mask]

                # Running average update
                n_samples = np.sum(class_mask)
                class_mean = np.mean(class_embeddings, axis=0)

                # Update center with momentum
                if self.center_counts[class_idx] == 0:
                    # First time seeing this class
                    self.centers[class_idx] = class_mean
                else:
                    # Momentum update
                    self.centers[class_idx] = (
                        (1 - self.alpha) * self.centers[class_idx] +
                        self.alpha * class_mean
                    )

                self.center_counts[class_idx] += n_samples

    def get_center_distance(
        self,
        embedding: np.ndarray,
        class_idx: int
    ) -> float:
        """Get distance from embedding to a specific class center."""
        diff = embedding - self.centers[class_idx]
        return float(np.sqrt(np.sum(diff ** 2)))

    def get_inter_class_distance(self) -> float:
        """Get average distance between class centers (want this to be large)."""
        if self.num_classes < 2:
            return 0.0

        distances = []
        for i in range(self.num_classes):
            for j in range(i + 1, self.num_classes):
                diff = self.centers[i] - self.centers[j]
                distances.append(np.sqrt(np.sum(diff ** 2)))

        return float(np.mean(distances)) if distances else 0.0


# ==================== Triplet Loss for Metric Learning ====================

class TripletLoss:
    """
    Triplet Loss for metric learning.

    Uses (anchor, positive, negative) triples to learn embeddings where:
    - Same-speaker (anchor, positive) are close
    - Different-speaker (anchor, negative) are far

    Loss = max(0, d(a, p) - d(a, n) + margin)

    Where:
    - d(a, p): distance between anchor and positive
    - d(a, n): distance between anchor and negative
    - margin: minimum separation between positive and negative distances

    Hard triplet mining: select triplets where d(a, p) > d(a, n) - margin
    (the model is "confused" about these)
    """

    def __init__(
        self,
        margin: float = 0.3,
        distance_metric: str = "cosine"  # "cosine" or "euclidean"
    ):
        self.margin = margin
        self.distance_metric = distance_metric

        # Statistics
        self.loss_history: List[float] = []
        self.num_hard_triplets: List[int] = []

        logger.info(f"✅ Triplet Loss initialized: margin={margin}, metric={distance_metric}")

    def _compute_distance(
        self,
        emb1: np.ndarray,
        emb2: np.ndarray
    ) -> float:
        """Compute distance between two embeddings."""
        if self.distance_metric == "cosine":
            # Cosine distance = 1 - cosine_similarity
            norm1 = np.linalg.norm(emb1)
            norm2 = np.linalg.norm(emb2)
            if norm1 == 0 or norm2 == 0:
                return 1.0
            similarity = np.dot(emb1, emb2) / (norm1 * norm2)
            return float(1.0 - similarity)
        else:
            # Euclidean distance
            return float(np.linalg.norm(emb1 - emb2))

    def compute_loss(
        self,
        anchor: np.ndarray,
        positive: np.ndarray,
        negative: np.ndarray
    ) -> float:
        """
        Compute triplet loss for a single triplet.

        Args:
            anchor: anchor embedding (e.g., one Derek sample)
            positive: positive embedding (another Derek sample)
            negative: negative embedding (non-Derek or synthetic)

        Returns:
            Triplet loss value
        """
        d_ap = self._compute_distance(anchor, positive)
        d_an = self._compute_distance(anchor, negative)

        loss = max(0.0, d_ap - d_an + self.margin)

        self.loss_history.append(loss)

        return loss

    def compute_batch_loss(
        self,
        embeddings: np.ndarray,
        labels: np.ndarray,
        semi_hard_mining: bool = True
    ) -> Tuple[float, int]:
        """
        Compute triplet loss for a batch with triplet mining.

        Args:
            embeddings: (batch_size, embedding_dim)
            labels: (batch_size,) class labels
            semi_hard_mining: if True, select semi-hard triplets

        Returns:
            Tuple of (average_loss, num_valid_triplets)
        """
        batch_size = embeddings.shape[0]

        # Compute all pairwise distances
        distances = np.zeros((batch_size, batch_size))
        for i in range(batch_size):
            for j in range(batch_size):
                distances[i, j] = self._compute_distance(embeddings[i], embeddings[j])

        # Mine triplets
        triplet_losses = []
        num_hard = 0

        for anchor_idx in range(batch_size):
            anchor_label = labels[anchor_idx]

            # Find positives (same class, different sample)
            positive_mask = (labels == anchor_label) & (np.arange(batch_size) != anchor_idx)
            positive_indices = np.where(positive_mask)[0]

            # Find negatives (different class)
            negative_mask = labels != anchor_label
            negative_indices = np.where(negative_mask)[0]

            if len(positive_indices) == 0 or len(negative_indices) == 0:
                continue

            for pos_idx in positive_indices:
                d_ap = distances[anchor_idx, pos_idx]

                for neg_idx in negative_indices:
                    d_an = distances[anchor_idx, neg_idx]

                    if semi_hard_mining:
                        # Semi-hard: d(a,p) < d(a,n) < d(a,p) + margin
                        if d_ap < d_an < d_ap + self.margin:
                            loss = d_ap - d_an + self.margin
                            triplet_losses.append(loss)
                            num_hard += 1
                    else:
                        # All triplets (hard + semi-hard + easy)
                        loss = max(0.0, d_ap - d_an + self.margin)
                        if loss > 0:
                            triplet_losses.append(loss)
                            num_hard += 1

        self.num_hard_triplets.append(num_hard)

        if len(triplet_losses) == 0:
            return 0.0, 0

        avg_loss = np.mean(triplet_losses)
        self.loss_history.append(avg_loss)

        return avg_loss, num_hard

    def generate_synthetic_negative(
        self,
        anchor: np.ndarray,
        perturbation_scale: float = 0.3
    ) -> np.ndarray:
        """
        Generate a synthetic negative by perturbing the anchor.

        Useful when you don't have real negative samples.
        Simulates what a spoofed/synthetic voice might look like.
        """
        # Random perturbation in embedding space
        noise = np.random.randn(*anchor.shape) * perturbation_scale

        # Add noise and normalize
        synthetic = anchor + noise
        synthetic = synthetic / (np.linalg.norm(synthetic) + 1e-10)

        return synthetic


# ==================== Combined Fine-Tuning System ====================

class SpeakerEmbeddingFineTuningSystem:
    """
    Combined fine-tuning system using AAM-Softmax, Center Loss, and Triplet Loss.

    Training strategy:
    1. AAM-Softmax for global speaker discrimination
    2. Center Loss for intra-class compactness (tight Derek cluster)
    3. Triplet Loss for metric learning (push impostors away)

    The combined loss:
    L_total = L_aam_softmax + lambda_center * L_center + lambda_triplet * L_triplet

    This approach:
    - Makes Derek samples cluster tightly (Center Loss)
    - Pushes non-Derek samples far away (Triplet Loss + AAM margin)
    - Learns discriminative class boundaries (AAM-Softmax)
    """

    def __init__(
        self,
        embedding_dim: int = 192,
        num_classes: int = 2,  # Derek (1) vs non-Derek (0)
        aam_scale: float = 30.0,
        aam_margin: float = 0.35,
        center_alpha: float = 0.5,
        triplet_margin: float = 0.3,
        lambda_center: float = 0.003,
        lambda_triplet: float = 0.1,
        learning_rate: float = 0.001
    ):
        self.embedding_dim = embedding_dim
        self.num_classes = num_classes
        self.learning_rate = learning_rate

        # Loss weights
        self.lambda_center = lambda_center
        self.lambda_triplet = lambda_triplet

        # Initialize loss components
        self.aam_softmax = AAMSoftmaxLoss(
            embedding_dim=embedding_dim,
            num_classes=num_classes,
            scale=aam_scale,
            margin=aam_margin
        )

        self.center_loss = CenterLoss(
            embedding_dim=embedding_dim,
            num_classes=num_classes,
            alpha=center_alpha
        )

        self.triplet_loss = TripletLoss(
            margin=triplet_margin,
            distance_metric="cosine"
        )

        # Training state
        self.epoch = 0
        self.total_samples_seen = 0
        self.best_accuracy = 0.0

        # Statistics
        self.training_history: List[Dict[str, float]] = []

        logger.info(f"✅ Speaker Embedding Fine-Tuning System initialized\n"
                   f"   - AAM-Softmax: scale={aam_scale}, margin={aam_margin}\n"
                   f"   - Center Loss: alpha={center_alpha}, weight={lambda_center}\n"
                   f"   - Triplet Loss: margin={triplet_margin}, weight={lambda_triplet}")

    async def train_step(
        self,
        embeddings: np.ndarray,
        labels: np.ndarray,
        use_triplet: bool = True
    ) -> Dict[str, float]:
        """
        Perform one training step with combined losses.

        Args:
            embeddings: (batch_size, embedding_dim) speaker embeddings
            labels: (batch_size,) class labels (1=Derek, 0=non-Derek)
            use_triplet: whether to include triplet loss

        Returns:
            Dictionary of loss values and metrics
        """
        # 1. AAM-Softmax Loss (main classification loss)
        aam_loss, accuracy = self.aam_softmax.compute_loss(embeddings, labels)

        # 2. Center Loss (compactness)
        center_loss = self.center_loss.compute_loss(embeddings, labels)

        # 3. Triplet Loss (metric learning)
        triplet_loss = 0.0
        num_triplets = 0
        if use_triplet and len(embeddings) >= 3:
            triplet_loss, num_triplets = self.triplet_loss.compute_batch_loss(
                embeddings, labels, semi_hard_mining=True
            )

        # Combined loss
        total_loss = (
            aam_loss +
            self.lambda_center * center_loss +
            self.lambda_triplet * triplet_loss
        )

        # Update centers and weights
        self.center_loss.update_centers(embeddings, labels)
        self.aam_softmax.update_weights(embeddings, labels, self.learning_rate)

        # Update statistics
        self.total_samples_seen += len(embeddings)
        if accuracy > self.best_accuracy:
            self.best_accuracy = accuracy

        # Record history
        step_metrics = {
            "total_loss": float(total_loss),
            "aam_loss": float(aam_loss),
            "center_loss": float(center_loss),
            "triplet_loss": float(triplet_loss),
            "accuracy": float(accuracy),
            "num_triplets": num_triplets,
            "inter_class_distance": self.center_loss.get_inter_class_distance(),
            "samples_seen": self.total_samples_seen
        }
        self.training_history.append(step_metrics)

        return step_metrics

    async def fine_tune_on_sample(
        self,
        new_embedding: np.ndarray,
        is_owner: bool,
        confidence: float,
        success: bool
    ) -> Dict[str, float]:
        """
        Fine-tune on a single new sample (online learning).

        Called after each authentication attempt to continuously improve.

        Args:
            new_embedding: The 192-dim embedding from this attempt
            is_owner: True if this is the owner (Derek)
            confidence: Authentication confidence score
            success: Whether authentication succeeded

        Returns:
            Training metrics
        """
        # Only train on high-confidence samples to avoid learning noise
        if confidence < 0.5:
            return {"skipped": True, "reason": "low_confidence"}

        # Weight the learning by confidence and success
        effective_lr = self.learning_rate * confidence
        if not success:
            effective_lr *= 0.3  # Learn less from failures

        # Create batch of size 1
        embeddings = new_embedding.reshape(1, -1)
        labels = np.array([1 if is_owner else 0])

        # Store original learning rate and temporarily modify
        original_lr = self.learning_rate
        self.learning_rate = effective_lr

        # Run training step (without triplet for single sample)
        metrics = await self.train_step(embeddings, labels, use_triplet=False)

        # Restore learning rate
        self.learning_rate = original_lr

        return metrics

    async def evaluate_embedding(
        self,
        embedding: np.ndarray
    ) -> Dict[str, float]:
        """
        Evaluate how well an embedding fits the owner vs non-owner classes.

        Returns distances and scores for classification.
        """
        # Distance to Derek (class 1) center
        derek_distance = self.center_loss.get_center_distance(embedding, 1)

        # Distance to non-Derek (class 0) center
        non_derek_distance = self.center_loss.get_center_distance(embedding, 0)

        # Cosine similarity to Derek class weight
        normalized_emb = embedding / (np.linalg.norm(embedding) + 1e-10)
        derek_cosine = float(np.dot(normalized_emb, self.aam_softmax.weight[1]))
        non_derek_cosine = float(np.dot(normalized_emb, self.aam_softmax.weight[0]))

        # Classification logits
        logits, _ = self.aam_softmax.compute_logits(
            embedding.reshape(1, -1),
            np.array([1])  # Assume Derek for margin computation
        )

        # Softmax probabilities
        exp_logits = np.exp(logits - np.max(logits))
        probs = exp_logits / np.sum(exp_logits)
        derek_prob = float(probs[0, 1])

        return {
            "derek_center_distance": derek_distance,
            "non_derek_center_distance": non_derek_distance,
            "derek_cosine_similarity": derek_cosine,
            "non_derek_cosine_similarity": non_derek_cosine,
            "derek_probability": derek_prob,
            "classification": "derek" if derek_prob > 0.5 else "non_derek",
            "inter_class_distance": self.center_loss.get_inter_class_distance()
        }

    def get_training_summary(self) -> Dict[str, Any]:
        """Get summary of training progress."""
        if not self.training_history:
            return {"status": "no_training_yet"}

        recent = self.training_history[-10:]  # Last 10 steps

        return {
            "total_samples_seen": self.total_samples_seen,
            "best_accuracy": self.best_accuracy,
            "recent_avg_loss": np.mean([h["total_loss"] for h in recent]),
            "recent_avg_accuracy": np.mean([h["accuracy"] for h in recent]),
            "inter_class_distance": self.center_loss.get_inter_class_distance(),
            "aam_softmax_loss_trend": [h["aam_loss"] for h in recent],
            "center_loss_trend": [h["center_loss"] for h in recent]
        }

    async def save_state(self, path: str):
        """Save the fine-tuning system state."""
        try:
            state = {
                "aam_weights": self.aam_softmax.weight.tolist(),
                "center_centers": self.center_loss.centers.tolist(),
                "center_counts": self.center_loss.center_counts.tolist(),
                "epoch": self.epoch,
                "total_samples_seen": self.total_samples_seen,
                "best_accuracy": self.best_accuracy,
                "training_history": self.training_history[-100:]  # Keep last 100
            }

            with open(path, 'w') as f:
                json.dump(state, f, indent=2)

            logger.info(f"✅ Fine-tuning state saved to {path}")

        except Exception as e:
            logger.error(f"Failed to save fine-tuning state: {e}")

    async def load_state(self, path: str) -> bool:
        """Load a previously saved state."""
        try:
            with open(path, 'r') as f:
                state = json.load(f)

            self.aam_softmax.weight = np.array(state["aam_weights"])
            self.center_loss.centers = np.array(state["center_centers"])
            self.center_loss.center_counts = np.array(state["center_counts"])
            self.epoch = state["epoch"]
            self.total_samples_seen = state["total_samples_seen"]
            self.best_accuracy = state["best_accuracy"]
            self.training_history = state.get("training_history", [])

            logger.info(f"✅ Fine-tuning state loaded from {path}")
            return True

        except Exception as e:
            logger.error(f"Failed to load fine-tuning state: {e}")
            return False


# ==================== Score Calibration System ====================

class ScoreCalibrator:
    """
    Calibrates raw model scores to true probabilities using Platt Scaling
    and Isotonic Regression.

    This is CRITICAL for making thresholds like 0.90/0.95/0.98 meaningful.

    Without calibration:
    - Raw score of 0.90 might actually mean 70% probability of correct classification
    - Threshold settings are arbitrary guesses

    With calibration:
    - Calibrated score of 0.90 means ~90% true probability
    - Thresholds become meaningful security decisions

    Calibration Methods:
    1. Platt Scaling: Fits sigmoid p = 1 / (1 + exp(-(a*s + b)))
       - Works well with small calibration sets
       - Assumes sigmoid relationship between score and probability

    2. Isotonic Regression: Non-parametric, fits any monotonic function
       - More flexible, no assumptions about score distribution
       - Needs more calibration data (~100+ samples)

    3. Hybrid: Uses Platt for small datasets, switches to Isotonic with more data
    """

    def __init__(
        self,
        method: str = "hybrid",  # "platt", "isotonic", or "hybrid"
        min_samples_for_isotonic: int = 100
    ):
        self.method = method
        self.min_samples_for_isotonic = min_samples_for_isotonic

        # Platt scaling parameters: p = sigmoid(a * score + b)
        self.platt_a: float = 1.0
        self.platt_b: float = 0.0

        # Isotonic regression model
        self.isotonic_model = None

        # Calibration data storage
        self.calibration_scores: List[float] = []
        self.calibration_labels: List[int] = []  # 1 = genuine Derek, 0 = impostor

        # Statistics
        self.is_calibrated = False
        self.calibration_method_used: Optional[str] = None
        self.calibration_error: float = 0.0  # ECE - Expected Calibration Error

        # Try to import scikit-learn
        try:
            from sklearn.isotonic import IsotonicRegression
            from sklearn.linear_model import LogisticRegression
            self.IsotonicRegression = IsotonicRegression
            self.LogisticRegression = LogisticRegression
            self._sklearn_available = True
        except ImportError:
            logger.warning("⚠️ scikit-learn not available, using numpy fallback for calibration")
            self.IsotonicRegression = None
            self.LogisticRegression = None
            self._sklearn_available = False

        logger.info(f"✅ Score Calibrator initialized: method={method}")

    def add_calibration_sample(
        self,
        raw_score: float,
        is_genuine: bool
    ):
        """
        Add a sample to the calibration dataset.

        Call this after each authentication attempt where you know the true label.

        Args:
            raw_score: The raw model score (e.g., 0.78)
            is_genuine: True if this was actually Derek, False if impostor
        """
        self.calibration_scores.append(float(raw_score))
        self.calibration_labels.append(1 if is_genuine else 0)

        # Periodically recalibrate as we get more data
        if len(self.calibration_scores) % 20 == 0 and len(self.calibration_scores) >= 30:
            asyncio.create_task(self.fit())

    async def fit(self) -> bool:
        """
        Fit the calibration model to collected data.

        Returns True if calibration succeeded.
        """
        n_samples = len(self.calibration_scores)

        if n_samples < 30:
            logger.debug(f"Not enough calibration data ({n_samples}/30)")
            return False

        scores = np.array(self.calibration_scores)
        labels = np.array(self.calibration_labels)

        # Choose method based on data size and configuration
        if self.method == "hybrid":
            if n_samples >= self.min_samples_for_isotonic:
                actual_method = "isotonic"
            else:
                actual_method = "platt"
        else:
            actual_method = self.method

        try:
            if actual_method == "isotonic" and self._sklearn_available:
                await self._fit_isotonic(scores, labels)
            else:
                await self._fit_platt(scores, labels)

            self.is_calibrated = True
            self.calibration_method_used = actual_method

            # Compute calibration error
            self.calibration_error = await self._compute_ece(scores, labels)

            logger.info(f"✅ Calibration complete: method={actual_method}, "
                       f"samples={n_samples}, ECE={self.calibration_error:.4f}")

            return True

        except Exception as e:
            logger.error(f"Calibration failed: {e}", exc_info=True)
            return False

    async def _fit_platt(self, scores: np.ndarray, labels: np.ndarray):
        """
        Fit Platt scaling parameters using logistic regression.

        The model: p = sigmoid(a * score + b)

        We fit a and b to maximize likelihood on the calibration set.
        """
        if self._sklearn_available and self.LogisticRegression:
            # Use sklearn for robust fitting
            model = self.LogisticRegression(solver='lbfgs', max_iter=1000)
            model.fit(scores.reshape(-1, 1), labels)

            self.platt_a = float(model.coef_[0, 0])
            self.platt_b = float(model.intercept_[0])
        else:
            # Numpy fallback: simple gradient descent
            a, b = 1.0, 0.0
            lr = 0.1

            for _ in range(1000):
                logits = a * scores + b
                probs = 1.0 / (1.0 + np.exp(-np.clip(logits, -20, 20)))

                # Gradient of negative log-likelihood
                grad_a = np.mean((probs - labels) * scores)
                grad_b = np.mean(probs - labels)

                a -= lr * grad_a
                b -= lr * grad_b

            self.platt_a = a
            self.platt_b = b

        logger.debug(f"Platt scaling fit: a={self.platt_a:.4f}, b={self.platt_b:.4f}")

    async def _fit_isotonic(self, scores: np.ndarray, labels: np.ndarray):
        """
        Fit isotonic regression model.

        Isotonic regression fits a monotonically increasing function
        that maps scores to probabilities.
        """
        if not self._sklearn_available:
            logger.warning("Isotonic regression not available, falling back to Platt")
            await self._fit_platt(scores, labels)
            return

        self.isotonic_model = self.IsotonicRegression(
            y_min=0.0,
            y_max=1.0,
            out_of_bounds='clip'
        )
        self.isotonic_model.fit(scores, labels)

        logger.debug("Isotonic regression fit complete")

    async def _compute_ece(
        self,
        scores: np.ndarray,
        labels: np.ndarray,
        n_bins: int = 10
    ) -> float:
        """
        Compute Expected Calibration Error (ECE).

        ECE measures how well the calibrated probabilities match
        actual frequencies. Lower is better.

        ECE = sum(|acc(bin_i) - conf(bin_i)| * |bin_i| / N)
        """
        calibrated_probs = np.array([self.calibrate(s) for s in scores])

        bin_boundaries = np.linspace(0, 1, n_bins + 1)
        ece = 0.0
        n_total = len(scores)

        for i in range(n_bins):
            bin_mask = (calibrated_probs >= bin_boundaries[i]) & (calibrated_probs < bin_boundaries[i + 1])
            bin_size = np.sum(bin_mask)

            if bin_size > 0:
                bin_acc = np.mean(labels[bin_mask])
                bin_conf = np.mean(calibrated_probs[bin_mask])
                ece += abs(bin_acc - bin_conf) * bin_size / n_total

        return ece

    def calibrate(self, raw_score: float) -> float:
        """
        Calibrate a raw model score to a true probability.

        Args:
            raw_score: Raw model output (e.g., 0.78)

        Returns:
            Calibrated probability (e.g., 0.85)
        """
        if not self.is_calibrated:
            # No calibration yet, return raw score
            return raw_score

        if self.calibration_method_used == "isotonic" and self.isotonic_model is not None:
            # Use isotonic regression
            return float(self.isotonic_model.predict([raw_score])[0])
        else:
            # Use Platt scaling
            logit = self.platt_a * raw_score + self.platt_b
            return float(1.0 / (1.0 + np.exp(-np.clip(logit, -20, 20))))

    def calibrate_batch(self, raw_scores: np.ndarray) -> np.ndarray:
        """Calibrate a batch of scores."""
        return np.array([self.calibrate(s) for s in raw_scores])

    def get_calibration_stats(self) -> Dict[str, Any]:
        """Get calibration statistics."""
        return {
            "is_calibrated": self.is_calibrated,
            "method": self.calibration_method_used,
            "num_calibration_samples": len(self.calibration_scores),
            "expected_calibration_error": self.calibration_error,
            "platt_a": self.platt_a,
            "platt_b": self.platt_b,
            "has_isotonic_model": self.isotonic_model is not None
        }

    async def save_state(self, path: str):
        """Save calibration state."""
        try:
            state = {
                "is_calibrated": self.is_calibrated,
                "calibration_method_used": self.calibration_method_used,
                "platt_a": self.platt_a,
                "platt_b": self.platt_b,
                "calibration_error": self.calibration_error,
                "calibration_scores": self.calibration_scores[-500:],  # Keep last 500
                "calibration_labels": self.calibration_labels[-500:]
            }

            # Note: Isotonic model needs special handling for persistence
            # sklearn models should be pickled separately

            with open(path, 'w') as f:
                json.dump(state, f, indent=2)

            logger.info(f"✅ Calibration state saved to {path}")

        except Exception as e:
            logger.error(f"Failed to save calibration state: {e}")

    async def load_state(self, path: str) -> bool:
        """Load calibration state."""
        try:
            with open(path, 'r') as f:
                state = json.load(f)

            self.is_calibrated = state["is_calibrated"]
            self.calibration_method_used = state.get("calibration_method_used")
            self.platt_a = state["platt_a"]
            self.platt_b = state["platt_b"]
            self.calibration_error = state.get("calibration_error", 0.0)
            self.calibration_scores = state.get("calibration_scores", [])
            self.calibration_labels = state.get("calibration_labels", [])

            # Refit isotonic if we have data and method was isotonic
            if (self.calibration_method_used == "isotonic" and
                len(self.calibration_scores) >= self.min_samples_for_isotonic):
                await self._fit_isotonic(
                    np.array(self.calibration_scores),
                    np.array(self.calibration_labels)
                )

            logger.info(f"✅ Calibration state loaded from {path}")
            return True

        except Exception as e:
            logger.error(f"Failed to load calibration state: {e}")
            return False


# ==================== Adaptive Threshold Manager ====================

class AdaptiveThresholdManager:
    """
    Manages authentication thresholds that adapt based on:
    1. Calibrated probability scores
    2. False Rejection Rate (FRR) and False Acceptance Rate (FAR)
    3. Security requirements (base, high, critical)
    4. Performance history

    Goal: Eventually run at 0.90/0.95/0.98 thresholds with excellent UX
    because calibration makes those numbers meaningful.

    Current interim thresholds (pre-calibration):
    - base: 0.40 (was 0.90)
    - high: 0.60 (was 0.95)
    - critical: 0.75 (was 0.98)

    Target thresholds (post-calibration):
    - base: 0.90
    - high: 0.95
    - critical: 0.98
    """

    def __init__(
        self,
        calibrator: Optional[ScoreCalibrator] = None,
        initial_base: float = 0.40,
        initial_high: float = 0.60,
        initial_critical: float = 0.75,
        target_base: float = 0.90,
        target_high: float = 0.95,
        target_critical: float = 0.98,
        target_frr: float = 0.05,  # Max 5% false rejection
        target_far: float = 0.001  # Max 0.1% false acceptance
    ):
        self.calibrator = calibrator or ScoreCalibrator()

        # Current thresholds (start low, increase as calibration improves)
        self.thresholds = {
            "base": initial_base,
            "high": initial_high,
            "critical": initial_critical
        }

        # Target thresholds (what we want to reach)
        self.target_thresholds = {
            "base": target_base,
            "high": target_high,
            "critical": target_critical
        }

        # Performance targets
        self.target_frr = target_frr
        self.target_far = target_far

        # Performance tracking
        self.genuine_scores: List[float] = []  # Scores for genuine Derek
        self.impostor_scores: List[float] = []  # Scores for impostors
        self.current_frr: float = 0.0
        self.current_far: float = 0.0

        # History for tracking progress
        self.threshold_history: List[Dict[str, Any]] = []

        logger.info(f"✅ Adaptive Threshold Manager initialized\n"
                   f"   Current: base={initial_base}, high={initial_high}, critical={initial_critical}\n"
                   f"   Target:  base={target_base}, high={target_high}, critical={target_critical}")

    def record_attempt(
        self,
        raw_score: float,
        is_genuine: bool,
        authentication_result: bool
    ):
        """
        Record an authentication attempt for threshold adaptation.

        Args:
            raw_score: Raw model score
            is_genuine: True if this was actually Derek
            authentication_result: True if authentication was granted
        """
        # Store for FRR/FAR calculation
        if is_genuine:
            self.genuine_scores.append(raw_score)
        else:
            self.impostor_scores.append(raw_score)

        # Keep last 200 of each
        self.genuine_scores = self.genuine_scores[-200:]
        self.impostor_scores = self.impostor_scores[-200:]

        # Add to calibrator
        self.calibrator.add_calibration_sample(raw_score, is_genuine)

        # Periodically adapt thresholds
        if (len(self.genuine_scores) + len(self.impostor_scores)) % 20 == 0:
            asyncio.create_task(self.adapt_thresholds())

    async def adapt_thresholds(self):
        """
        Adapt thresholds based on current performance.

        Strategy:
        1. Compute current FRR and FAR
        2. If FRR too high, lower thresholds
        3. If FAR too high, raise thresholds
        4. Gradually move toward target thresholds as calibration improves
        """
        if len(self.genuine_scores) < 20:
            return  # Need more data

        # Calculate current FRR at current base threshold
        genuine_array = np.array(self.genuine_scores)
        self.current_frr = np.mean(genuine_array < self.thresholds["base"])

        # Calculate current FAR at current base threshold
        if len(self.impostor_scores) >= 5:
            impostor_array = np.array(self.impostor_scores)
            self.current_far = np.mean(impostor_array >= self.thresholds["base"])
        else:
            self.current_far = 0.0

        # Adaptation logic
        old_base = self.thresholds["base"]

        # If Derek is getting rejected too often, lower threshold
        if self.current_frr > self.target_frr:
            # Lower threshold to reduce false rejections
            adjustment = -0.02 * (self.current_frr / self.target_frr)
            adjustment = max(adjustment, -0.05)  # Max 5% decrease at once

            self.thresholds["base"] = max(0.30, self.thresholds["base"] + adjustment)

            logger.info(f"📉 Lowering threshold: FRR={self.current_frr:.1%} > target {self.target_frr:.1%}\n"
                       f"   base: {old_base:.2f} → {self.thresholds['base']:.2f}")

        # If calibration is good and FRR is low, can increase toward target
        elif (self.calibrator.is_calibrated and
              self.calibrator.calibration_error < 0.10 and
              self.current_frr < self.target_frr * 0.5):
            # Calibration is good, move toward target
            target = self.target_thresholds["base"]
            gap = target - self.thresholds["base"]

            if gap > 0:
                # Move 5% of the way toward target
                adjustment = gap * 0.05
                self.thresholds["base"] = min(target, self.thresholds["base"] + adjustment)

                logger.info(f"📈 Raising threshold toward target\n"
                           f"   base: {old_base:.2f} → {self.thresholds['base']:.2f} "
                           f"(target: {target:.2f})")

        # Update high and critical relative to base
        self.thresholds["high"] = min(
            self.target_thresholds["high"],
            self.thresholds["base"] + 0.10
        )
        self.thresholds["critical"] = min(
            self.target_thresholds["critical"],
            self.thresholds["base"] + 0.15
        )

        # Record history
        self.threshold_history.append({
            "timestamp": datetime.now().isoformat(),
            "thresholds": self.thresholds.copy(),
            "frr": self.current_frr,
            "far": self.current_far,
            "calibration_error": self.calibrator.calibration_error,
            "num_genuine_samples": len(self.genuine_scores),
            "num_impostor_samples": len(self.impostor_scores)
        })

    def get_threshold(self, security_level: str = "base") -> float:
        """
        Get the current threshold for a security level.

        Args:
            security_level: "base", "high", or "critical"

        Returns:
            Current threshold value
        """
        return self.thresholds.get(security_level, self.thresholds["base"])

    def should_authenticate(
        self,
        raw_score: float,
        security_level: str = "base"
    ) -> Tuple[bool, float, Dict[str, Any]]:
        """
        Determine if authentication should be granted.

        Args:
            raw_score: Raw model score
            security_level: Required security level

        Returns:
            Tuple of (should_authenticate, calibrated_probability, debug_info)
        """
        threshold = self.get_threshold(security_level)
        calibrated_prob = self.calibrator.calibrate(raw_score)

        should_auth = calibrated_prob >= threshold

        debug_info = {
            "raw_score": raw_score,
            "calibrated_probability": calibrated_prob,
            "threshold": threshold,
            "security_level": security_level,
            "calibration_applied": self.calibrator.is_calibrated,
            "current_frr": self.current_frr,
            "current_far": self.current_far,
            "distance_to_target": self.target_thresholds["base"] - threshold
        }

        return should_auth, calibrated_prob, debug_info

    def get_status(self) -> Dict[str, Any]:
        """Get current threshold manager status."""
        return {
            "current_thresholds": self.thresholds,
            "target_thresholds": self.target_thresholds,
            "current_frr": self.current_frr,
            "current_far": self.current_far,
            "target_frr": self.target_frr,
            "target_far": self.target_far,
            "num_genuine_samples": len(self.genuine_scores),
            "num_impostor_samples": len(self.impostor_scores),
            "calibration_status": self.calibrator.get_calibration_stats(),
            "progress_to_target_base": (
                (self.thresholds["base"] - 0.40) /
                (self.target_thresholds["base"] - 0.40) * 100
                if self.target_thresholds["base"] > 0.40 else 100
            )
        }

    async def save_state(self, path: str):
        """Save threshold manager state."""
        try:
            state = {
                "thresholds": self.thresholds,
                "genuine_scores": self.genuine_scores[-200:],
                "impostor_scores": self.impostor_scores[-200:],
                "current_frr": self.current_frr,
                "current_far": self.current_far,
                "threshold_history": self.threshold_history[-50:]
            }

            with open(path, 'w') as f:
                json.dump(state, f, indent=2)

            logger.info(f"✅ Threshold manager state saved to {path}")

        except Exception as e:
            logger.error(f"Failed to save threshold state: {e}")

    async def load_state(self, path: str) -> bool:
        """Load threshold manager state."""
        try:
            with open(path, 'r') as f:
                state = json.load(f)

            self.thresholds = state["thresholds"]
            self.genuine_scores = state.get("genuine_scores", [])
            self.impostor_scores = state.get("impostor_scores", [])
            self.current_frr = state.get("current_frr", 0.0)
            self.current_far = state.get("current_far", 0.0)
            self.threshold_history = state.get("threshold_history", [])

            logger.info(f"✅ Threshold manager state loaded from {path}")
            return True

        except Exception as e:
            logger.error(f"Failed to load threshold state: {e}")
            return False


# ==================== Integrated Calibrated Authentication System ====================

class CalibratedAuthenticationSystem:
    """
    Complete calibrated authentication system combining:
    1. AAM-Softmax + Center Loss + Triplet Loss fine-tuning
    2. Platt/Isotonic score calibration
    3. Adaptive threshold management
    4. Performance monitoring and reporting

    This is the main entry point for the enhanced voice authentication pipeline.

    Usage:
        system = CalibratedAuthenticationSystem()
        await system.initialize()

        # On each authentication attempt:
        result = await system.authenticate(embedding, is_owner_known=True)
        print(f"Authenticated: {result['authenticated']}, Probability: {result['probability']}")
    """

    def __init__(
        self,
        embedding_dim: int = 192,
        persist_dir: str = "/tmp/jarvis_voice_auth"
    ):
        self.embedding_dim = embedding_dim
        self.persist_dir = Path(persist_dir)
        self.persist_dir.mkdir(parents=True, exist_ok=True)

        # Initialize components
        self.fine_tuning = SpeakerEmbeddingFineTuningSystem(
            embedding_dim=embedding_dim
        )
        self.calibrator = ScoreCalibrator(method="hybrid")
        self.threshold_manager = AdaptiveThresholdManager(
            calibrator=self.calibrator
        )

        # Statistics
        self.total_authentications = 0
        self.successful_authentications = 0
        self.authentication_history: List[Dict[str, Any]] = []

        logger.info("✅ Calibrated Authentication System initialized")

    async def initialize(self):
        """Load persisted state if available."""
        try:
            await self.fine_tuning.load_state(str(self.persist_dir / "fine_tuning.json"))
            await self.calibrator.load_state(str(self.persist_dir / "calibration.json"))
            await self.threshold_manager.load_state(str(self.persist_dir / "thresholds.json"))
            logger.info("✅ Loaded persisted authentication state")
        except Exception as e:
            logger.info(f"No persisted state found, starting fresh: {e}")

    async def authenticate(
        self,
        embedding: np.ndarray,
        is_owner_known: Optional[bool] = None,
        security_level: str = "base"
    ) -> Dict[str, Any]:
        """
        Perform calibrated authentication.

        Args:
            embedding: 192-dim speaker embedding
            is_owner_known: If True, this is known to be Derek (for training)
                          If False, known to be impostor
                          If None, unknown (typical case)
            security_level: "base", "high", or "critical"

        Returns:
            Authentication result dictionary
        """
        self.total_authentications += 1

        # 1. Evaluate embedding using fine-tuned model
        eval_result = await self.fine_tuning.evaluate_embedding(embedding)
        raw_score = eval_result["derek_probability"]

        # 2. Determine authentication decision
        should_auth, calibrated_prob, debug_info = self.threshold_manager.should_authenticate(
            raw_score, security_level
        )

        # 3. If we know the true label, record for training
        if is_owner_known is not None:
            self.threshold_manager.record_attempt(
                raw_score=raw_score,
                is_genuine=is_owner_known,
                authentication_result=should_auth
            )

            # Fine-tune on this sample
            await self.fine_tuning.fine_tune_on_sample(
                new_embedding=embedding,
                is_owner=is_owner_known,
                confidence=calibrated_prob,
                success=should_auth == is_owner_known
            )

        # 4. Track statistics
        if should_auth:
            self.successful_authentications += 1

        result = {
            "authenticated": should_auth,
            "raw_score": raw_score,
            "probability": calibrated_prob,
            "threshold": debug_info["threshold"],
            "security_level": security_level,
            "calibration_applied": debug_info["calibration_applied"],
            "embedding_evaluation": eval_result,
            "debug_info": debug_info,
            "total_authentications": self.total_authentications,
            "success_rate": self.successful_authentications / max(self.total_authentications, 1)
        }

        self.authentication_history.append({
            "timestamp": datetime.now().isoformat(),
            **{k: v for k, v in result.items() if k != "embedding_evaluation"}
        })

        # Keep history manageable
        self.authentication_history = self.authentication_history[-100:]

        return result

    async def get_system_status(self) -> Dict[str, Any]:
        """Get comprehensive system status."""
        return {
            "total_authentications": self.total_authentications,
            "successful_authentications": self.successful_authentications,
            "success_rate": self.successful_authentications / max(self.total_authentications, 1),
            "fine_tuning": self.fine_tuning.get_training_summary(),
            "calibration": self.calibrator.get_calibration_stats(),
            "thresholds": self.threshold_manager.get_status()
        }

    async def save_state(self):
        """Persist all system state."""
        await self.fine_tuning.save_state(str(self.persist_dir / "fine_tuning.json"))
        await self.calibrator.save_state(str(self.persist_dir / "calibration.json"))
        await self.threshold_manager.save_state(str(self.persist_dir / "thresholds.json"))
        logger.info("✅ Saved all authentication system state")

    async def get_progress_report(self) -> Dict[str, Any]:
        """
        Get a human-readable progress report toward 0.90/0.95/0.98 thresholds.
        """
        status = await self.get_system_status()
        threshold_status = status["thresholds"]

        current_base = threshold_status["current_thresholds"]["base"]
        target_base = threshold_status["target_thresholds"]["base"]
        progress = threshold_status.get("progress_to_target_base", 0)

        calibration_status = status["calibration"]
        is_calibrated = calibration_status.get("is_calibrated", False)
        ece = calibration_status.get("expected_calibration_error", 1.0)

        report = {
            "summary": "",
            "current_thresholds": threshold_status["current_thresholds"],
            "target_thresholds": threshold_status["target_thresholds"],
            "progress_percent": progress,
            "calibration_quality": "excellent" if ece < 0.05 else "good" if ece < 0.10 else "moderate" if ece < 0.20 else "poor",
            "recommendation": ""
        }

        if progress >= 90:
            report["summary"] = "Near target thresholds - authentication system is well-calibrated"
            report["recommendation"] = "System is performing optimally. Continue monitoring."
        elif progress >= 60:
            report["summary"] = f"Good progress - at {progress:.0f}% toward target thresholds"
            report["recommendation"] = "Continue collecting authentication samples to improve calibration."
        elif is_calibrated:
            report["summary"] = f"Calibration active but thresholds still adapting ({progress:.0f}%)"
            report["recommendation"] = "System is learning. FRR/FAR are being balanced."
        else:
            report["summary"] = "Early learning phase - collecting calibration data"
            report["recommendation"] = f"Need {30 - calibration_status.get('num_calibration_samples', 0)} more samples for initial calibration."

        return report
