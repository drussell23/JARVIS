#!/usr/bin/env python3
"""
JARVIS Voice Profile Learning Engine
=====================================

ACTIVE continuous learning system that ACTUALLY improves voice recognition over time.

Unlike passive logging, this engine:
1. FUSES new voice embeddings with the stored profile using ML techniques
2. LEARNS temporal patterns (morning voice vs evening voice)
3. UPDATES the reference embedding in the database
4. TRACKS and REPORTS confidence improvements

The core insight: Voice recognition improves when we:
- Collect diverse samples (different times, environments)
- Weight recent high-confidence samples more heavily
- Detect and exclude outliers (coughs, background noise)
- Build temporal models (your voice varies by time of day)

ML Techniques Used:
- **Exponential Moving Average (EMA)**: Smooth embedding updates
- **Mahalanobis Distance**: Outlier detection for bad samples
- **Temporal Clustering**: Time-of-day voice variations
- **Online Learning**: Incremental profile updates
- **Adaptive Weighting**: Confidence-based sample importance
"""

import asyncio
import logging
import numpy as np
import json
import sqlite3
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple
from pathlib import Path
from dataclasses import dataclass, field, asdict
from collections import defaultdict
import os

from backend.core.async_safety import LazyAsyncLock

logger = logging.getLogger(__name__)


def _safe_normalize(embedding: np.ndarray, epsilon: float = 1e-10) -> Optional[np.ndarray]:
    """
    v226.0: L2-normalize an embedding with zero-norm and NaN protection.

    Raw `embedding / np.linalg.norm(embedding)` is the #1 source of NaN in
    the voice pipeline: silent audio → zero vector → division by zero → NaN
    → propagates into reference embeddings, temporal profiles, sample buffers,
    and ultimately into the stored voiceprint (permanent corruption).

    Returns None if the embedding is degenerate (zero-norm, NaN, Inf),
    signaling to the caller that the sample should be discarded.
    """
    if embedding is None:
        return None
    if np.any(np.isnan(embedding)) or np.any(np.isinf(embedding)):
        logger.warning("[SafeNorm] Embedding contains NaN/Inf — discarding")
        return None
    norm = np.linalg.norm(embedding)
    if norm < epsilon:
        logger.warning(f"[SafeNorm] Embedding norm near-zero ({norm:.2e}) — discarding")
        return None
    return embedding / norm


# =============================================================================
# CONFIGURATION - Dynamically loaded, no hardcoding
# =============================================================================
class LearningConfig:
    """Dynamic configuration for voice profile learning with advanced optimization."""

    def __init__(self):
        # Learning rates - ADAPTIVE based on profile maturity
        self.embedding_learning_rate = float(os.getenv('JARVIS_EMBEDDING_LR', '0.1'))
        self.temporal_learning_rate = float(os.getenv('JARVIS_TEMPORAL_LR', '0.05'))

        # ADVANCED: Adaptive learning rate parameters
        self.min_learning_rate = float(os.getenv('JARVIS_MIN_LR', '0.02'))
        self.max_learning_rate = float(os.getenv('JARVIS_MAX_LR', '0.25'))
        self.learning_rate_decay = float(os.getenv('JARVIS_LR_DECAY', '0.995'))  # Per sample decay
        self.mature_profile_samples = int(os.getenv('JARVIS_MATURE_SAMPLES', '50'))

        # Sample quality thresholds - STRICTER for security
        self.min_confidence_for_learning = float(os.getenv('JARVIS_MIN_LEARN_CONF', '0.80'))  # Raised from 0.75
        self.high_quality_threshold = float(os.getenv('JARVIS_HIGH_QUALITY_CONF', '0.92'))  # Raised from 0.90
        self.elite_quality_threshold = float(os.getenv('JARVIS_ELITE_CONF', '0.95'))  # New: Elite samples

        # Update frequency
        self.min_samples_for_update = int(os.getenv('JARVIS_MIN_SAMPLES_UPDATE', '5'))
        self.max_samples_per_update = int(os.getenv('JARVIS_MAX_SAMPLES_UPDATE', '20'))

        # Temporal buckets (hours)
        self.temporal_buckets = {
            'early_morning': (5, 8),    # 5am-8am
            'morning': (8, 12),          # 8am-12pm
            'afternoon': (12, 17),       # 12pm-5pm
            'evening': (17, 21),         # 5pm-9pm
            'night': (21, 24),           # 9pm-12am
            'late_night': (0, 5),        # 12am-5am
        }

        # ADVANCED: Statistical outlier detection parameters
        self.outlier_std_threshold = float(os.getenv('JARVIS_OUTLIER_STD', '2.5'))
        self.outlier_iqr_multiplier = float(os.getenv('JARVIS_OUTLIER_IQR', '1.5'))
        self.min_similarity_threshold = float(os.getenv('JARVIS_MIN_SIMILARITY', '0.40'))

        # ADVANCED: Profile optimization parameters
        self.weak_sample_threshold = float(os.getenv('JARVIS_WEAK_SAMPLE_THRESH', '0.70'))
        self.optimization_interval_samples = int(os.getenv('JARVIS_OPT_INTERVAL', '25'))
        self.min_samples_for_optimization = int(os.getenv('JARVIS_MIN_OPT_SAMPLES', '15'))
        self.target_profile_variance = float(os.getenv('JARVIS_TARGET_VARIANCE', '0.05'))

        # ADVANCED: Multi-factor confidence boosting
        self.enable_multi_factor_boost = os.getenv('JARVIS_MULTI_FACTOR_BOOST', 'true').lower() == 'true'
        self.temporal_match_boost = float(os.getenv('JARVIS_TEMPORAL_BOOST', '0.03'))
        self.behavioral_boost = float(os.getenv('JARVIS_BEHAVIORAL_BOOST', '0.02'))
        self.consistency_boost = float(os.getenv('JARVIS_CONSISTENCY_BOOST', '0.02'))
        self.max_total_boost = float(os.getenv('JARVIS_MAX_BOOST', '0.08'))

        # ADVANCED: Profile quality scoring
        self.diversity_weight = float(os.getenv('JARVIS_DIVERSITY_WEIGHT', '0.25'))
        self.consistency_weight = float(os.getenv('JARVIS_CONSISTENCY_WEIGHT', '0.35'))
        self.coverage_weight = float(os.getenv('JARVIS_COVERAGE_WEIGHT', '0.25'))
        self.recency_weight = float(os.getenv('JARVIS_RECENCY_WEIGHT', '0.15'))

        # Database paths
        self.metrics_db_path = Path(os.getenv(
            'JARVIS_METRICS_DB',
            os.path.expanduser('~/.jarvis/logs/unlock_metrics/unlock_metrics.db')
        ))
        self.learning_db_path = Path(os.getenv(
            'JARVIS_LEARNING_DB',
            os.path.expanduser('~/.jarvis/learning/jarvis_learning.db')
        ))


_config = LearningConfig()


# =============================================================================
# DATA CLASSES
# =============================================================================
@dataclass
class VoiceSample:
    """A voice sample for learning."""
    embedding: np.ndarray
    confidence: float
    timestamp: datetime
    hour_of_day: int
    day_of_week: str
    audio_quality: str
    sample_id: str
    learning_weight: float = 1.0


@dataclass
class TemporalProfile:
    """Time-of-day specific voice profile."""
    time_bucket: str
    embedding: np.ndarray
    sample_count: int
    avg_confidence: float
    last_updated: datetime


@dataclass
class LearningState:
    """Current state of the learning engine."""
    speaker_name: str
    total_samples_learned: int
    confidence_history: List[float]
    current_avg_confidence: float
    confidence_trend: str  # 'improving', 'stable', 'declining'
    temporal_profiles: Dict[str, TemporalProfile]
    last_profile_update: Optional[datetime]
    improvement_since_start: float


# =============================================================================
# VOICE PROFILE LEARNING ENGINE
# =============================================================================
class VoiceProfileLearningEngine:
    """
    Active learning engine that improves voice recognition over time.

    This engine:
    1. Collects high-quality voice samples from successful authentications
    2. Fuses new embeddings into the stored profile using weighted averaging
    3. Builds temporal profiles (morning voice, evening voice, etc.)
    4. Detects and excludes outlier samples
    5. Actually UPDATES the database with improved embeddings
    """

    def __init__(self, speaker_name: str = "Derek J. Russell"):
        self.speaker_name = speaker_name
        self.config = _config
        self.state = None
        self._initialized = False
        self._lock = asyncio.Lock()

        # In-memory sample buffer
        self._sample_buffer: List[VoiceSample] = []
        self._max_buffer_size = 100

        # Current reference embedding
        self._reference_embedding: Optional[np.ndarray] = None
        self._temporal_embeddings: Dict[str, np.ndarray] = {}

    async def initialize(self):
        """Initialize the learning engine with existing data."""
        if self._initialized:
            return

        async with self._lock:
            try:
                # Load existing profile from database
                await self._load_profile()

                # Load historical samples
                await self._load_recent_samples()

                # Initialize state
                await self._initialize_state()

                self._initialized = True
                logger.info(
                    f"🧠 [LEARNING-ENGINE] Initialized for {self.speaker_name} | "
                    f"Samples: {self.state.total_samples_learned} | "
                    f"Avg Confidence: {self.state.current_avg_confidence:.1%}"
                )

            except Exception as e:
                logger.error(f"🧠 [LEARNING-ENGINE] Initialization failed: {e}")
                # Create default state
                self.state = LearningState(
                    speaker_name=self.speaker_name,
                    total_samples_learned=0,
                    confidence_history=[],
                    current_avg_confidence=0.0,
                    confidence_trend='stable',
                    temporal_profiles={},
                    last_profile_update=None,
                    improvement_since_start=0.0
                )
                self._initialized = True

    async def _load_profile(self):
        """Load existing speaker profile from database."""
        try:
            from intelligence.learning_database import get_learning_database
            db = await get_learning_database()

            profiles = await db.get_all_speaker_profiles()
            for profile in profiles:
                if self.speaker_name.lower() in profile.get('speaker_name', '').lower():
                    embedding_data = profile.get('embedding')
                    if embedding_data:
                        if isinstance(embedding_data, (bytes, bytearray)):
                            self._reference_embedding = np.frombuffer(embedding_data, dtype=np.float32)
                        elif isinstance(embedding_data, str):
                            self._reference_embedding = np.array(json.loads(embedding_data), dtype=np.float32)
                        logger.info(f"🧠 [LEARNING-ENGINE] Loaded reference embedding: {self._reference_embedding.shape}")
                    break

        except Exception as e:
            logger.warning(f"🧠 [LEARNING-ENGINE] Could not load profile: {e}")

    async def _load_recent_samples(self):
        """Load recent high-quality samples for learning."""
        try:
            if not self.config.metrics_db_path.exists():
                return

            conn = sqlite3.connect(self.config.metrics_db_path)
            cursor = conn.cursor()

            # Get recent successful VBI attempts with embeddings
            cursor.execute("""
                SELECT
                    embedding_json, confidence, timestamp, hour_of_day,
                    day_of_week, audio_quality, attempt_id
                FROM vbi_unlock_attempts
                WHERE speaker_name LIKE ?
                    AND success = 1
                    AND confidence >= ?
                    AND embedding_json IS NOT NULL
                ORDER BY timestamp DESC
                LIMIT ?
            """, (
                f"%{self.speaker_name.split()[0]}%",
                self.config.min_confidence_for_learning,
                self.config.max_samples_per_update * 2
            ))

            rows = cursor.fetchall()
            conn.close()

            for row in rows:
                try:
                    embedding_json, conf, ts, hour, dow, quality, sample_id = row
                    if embedding_json:
                        embedding = np.array(json.loads(embedding_json), dtype=np.float32)

                        # Calculate learning weight based on confidence
                        weight = self._calculate_sample_weight(conf, quality)

                        sample = VoiceSample(
                            embedding=embedding,
                            confidence=conf,
                            timestamp=datetime.fromisoformat(ts) if isinstance(ts, str) else datetime.now(),
                            hour_of_day=hour or 12,
                            day_of_week=dow or 'Unknown',
                            audio_quality=quality or 'unknown',
                            sample_id=str(sample_id),
                            learning_weight=weight
                        )
                        self._sample_buffer.append(sample)
                except Exception as e:
                    logger.debug(f"Skipping sample: {e}")

            logger.info(f"🧠 [LEARNING-ENGINE] Loaded {len(self._sample_buffer)} recent samples")

        except Exception as e:
            logger.warning(f"🧠 [LEARNING-ENGINE] Could not load samples: {e}")

    async def _initialize_state(self):
        """Initialize learning state from loaded data."""
        confidences = [s.confidence for s in self._sample_buffer]

        # Calculate confidence trend
        if len(confidences) >= 10:
            recent = np.mean(confidences[:5])
            older = np.mean(confidences[5:10])
            if recent > older + 0.02:
                trend = 'improving'
            elif recent < older - 0.02:
                trend = 'declining'
            else:
                trend = 'stable'
        else:
            trend = 'stable'

        # Build temporal profiles from samples
        temporal_profiles = await self._build_temporal_profiles()

        self.state = LearningState(
            speaker_name=self.speaker_name,
            total_samples_learned=len(self._sample_buffer),
            confidence_history=confidences[:50],
            current_avg_confidence=np.mean(confidences) if confidences else 0.0,
            confidence_trend=trend,
            temporal_profiles=temporal_profiles,
            last_profile_update=datetime.now() if self._sample_buffer else None,
            improvement_since_start=0.0
        )

    def _calculate_sample_weight(self, confidence: float, quality: str) -> float:
        """Calculate learning weight for a sample."""
        # Base weight from confidence
        weight = confidence

        # Quality bonus
        quality_bonus = {
            'excellent': 0.2,
            'good': 0.1,
            'fair': 0.0,
            'poor': -0.2,
        }.get(quality, 0.0)

        weight += quality_bonus

        # Clamp to valid range
        return max(0.1, min(1.5, weight))

    def _get_time_bucket(self, hour: int) -> str:
        """Get temporal bucket for hour of day."""
        for bucket, (start, end) in self.config.temporal_buckets.items():
            if start <= hour < end:
                return bucket
        return 'night'

    async def _build_temporal_profiles(self) -> Dict[str, TemporalProfile]:
        """Build time-of-day specific profiles from samples."""
        profiles = {}

        # Group samples by time bucket
        bucket_samples: Dict[str, List[VoiceSample]] = defaultdict(list)
        for sample in self._sample_buffer:
            bucket = self._get_time_bucket(sample.hour_of_day)
            bucket_samples[bucket].append(sample)

        # Build profile for each bucket with enough samples
        for bucket, samples in bucket_samples.items():
            if len(samples) >= 3:
                # Weighted average of embeddings
                weights = np.array([s.learning_weight for s in samples])
                weights = weights / weights.sum()

                embeddings = np.array([s.embedding for s in samples])
                avg_embedding = np.average(embeddings, axis=0, weights=weights)

                profiles[bucket] = TemporalProfile(
                    time_bucket=bucket,
                    embedding=avg_embedding,
                    sample_count=len(samples),
                    avg_confidence=np.mean([s.confidence for s in samples]),
                    last_updated=max(s.timestamp for s in samples)
                )

                self._temporal_embeddings[bucket] = avg_embedding

        return profiles

    # =========================================================================
    # CORE LEARNING METHODS
    # =========================================================================

    async def learn_from_sample(
        self,
        embedding: np.ndarray,
        confidence: float,
        success: bool,
        audio_quality: str = None,
        timestamp: datetime = None
    ) -> Dict[str, Any]:
        """
        Learn from a new voice sample.

        This is the MAIN entry point for continuous learning.
        Call this after every successful VBI authentication.

        Returns:
            Dict with learning results and updated profile info
        """
        if not self._initialized:
            await self.initialize()

        timestamp = timestamp or datetime.now()

        # Only learn from high-quality successful samples
        if not success or confidence < self.config.min_confidence_for_learning:
            return {
                'learned': False,
                'reason': f"Sample not suitable (success={success}, conf={confidence:.1%})",
                'current_avg_confidence': self.state.current_avg_confidence
            }

        # Check for outliers using existing profile
        if self._reference_embedding is not None:
            is_outlier = self._is_outlier(embedding)
            if is_outlier:
                logger.warning(f"🧠 [LEARNING-ENGINE] Outlier sample detected, skipping")
                return {
                    'learned': False,
                    'reason': 'Outlier sample detected',
                    'current_avg_confidence': self.state.current_avg_confidence
                }

        # Create sample
        if audio_quality is None:
            audio_quality = 'excellent' if confidence >= 0.95 else 'good' if confidence >= 0.85 else 'fair'

        sample = VoiceSample(
            embedding=embedding,
            confidence=confidence,
            timestamp=timestamp,
            hour_of_day=timestamp.hour,
            day_of_week=timestamp.strftime('%A'),
            audio_quality=audio_quality,
            sample_id=f"learn_{int(timestamp.timestamp() * 1000)}",
            learning_weight=self._calculate_sample_weight(confidence, audio_quality)
        )

        # Add to buffer
        self._sample_buffer.insert(0, sample)
        if len(self._sample_buffer) > self._max_buffer_size:
            self._sample_buffer = self._sample_buffer[:self._max_buffer_size]

        # Update confidence history
        self.state.confidence_history.insert(0, confidence)
        self.state.confidence_history = self.state.confidence_history[:50]
        self.state.total_samples_learned += 1

        # Update reference embedding using EMA
        old_avg = self.state.current_avg_confidence
        await self._update_reference_embedding(sample)

        # Update temporal profile
        await self._update_temporal_profile(sample)

        # Recalculate statistics
        self.state.current_avg_confidence = np.mean(self.state.confidence_history)
        self._update_confidence_trend()

        # Check if we should write to database
        should_persist = (
            self.state.total_samples_learned % self.config.min_samples_for_update == 0 or
            confidence >= self.config.high_quality_threshold
        )

        if should_persist:
            await self._persist_updated_profile()

        improvement = self.state.current_avg_confidence - old_avg

        logger.info(
            f"🧠 [LEARNING-ENGINE] Learned from sample | "
            f"Confidence: {confidence:.1%} | "
            f"Avg: {self.state.current_avg_confidence:.1%} | "
            f"Trend: {self.state.confidence_trend} | "
            f"Improvement: {improvement:+.2%}"
        )

        return {
            'learned': True,
            'sample_id': sample.sample_id,
            'current_avg_confidence': self.state.current_avg_confidence,
            'confidence_trend': self.state.confidence_trend,
            'total_samples': self.state.total_samples_learned,
            'improvement': improvement,
            'persisted': should_persist
        }

    def _is_outlier(self, embedding: np.ndarray) -> bool:
        """Detect if embedding is an outlier using Mahalanobis-like distance."""
        if self._reference_embedding is None:
            return False

        # v226.0: Safe normalization to prevent NaN from zero-norm vectors
        ref_norm = _safe_normalize(self._reference_embedding)
        emb_norm = _safe_normalize(embedding)
        if ref_norm is None or emb_norm is None:
            # Degenerate embedding — treat as outlier to discard it
            return True
        similarity = np.dot(ref_norm, emb_norm)

        # If similarity is very low, it's likely an outlier
        # For ECAPA embeddings, typical same-speaker similarity is > 0.5
        return similarity < 0.3

    async def _update_reference_embedding(self, sample: VoiceSample):
        """Update reference embedding using Exponential Moving Average."""
        if self._reference_embedding is None:
            self._reference_embedding = sample.embedding.copy()
            return

        # EMA update: new_ref = (1-α)*old_ref + α*new_sample
        # α is adaptive based on sample quality
        alpha = self.config.embedding_learning_rate * sample.learning_weight

        # Weighted update
        self._reference_embedding = (
            (1 - alpha) * self._reference_embedding +
            alpha * sample.embedding
        )

        # Normalize to unit sphere (important for cosine similarity)
        # v226.0: Safe normalization prevents NaN from zero-norm EMA results
        normalized = _safe_normalize(self._reference_embedding)
        if normalized is not None:
            self._reference_embedding = normalized

    async def _update_temporal_profile(self, sample: VoiceSample):
        """Update time-of-day specific profile."""
        bucket = self._get_time_bucket(sample.hour_of_day)

        if bucket in self._temporal_embeddings:
            # EMA update for temporal profile
            alpha = self.config.temporal_learning_rate * sample.learning_weight
            self._temporal_embeddings[bucket] = (
                (1 - alpha) * self._temporal_embeddings[bucket] +
                alpha * sample.embedding
            )
            # v226.0: Safe normalization prevents NaN from zero-norm temporal EMA
            normalized = _safe_normalize(self._temporal_embeddings[bucket])
            if normalized is not None:
                self._temporal_embeddings[bucket] = normalized
        else:
            self._temporal_embeddings[bucket] = sample.embedding.copy()

        # Update state
        if bucket not in self.state.temporal_profiles:
            self.state.temporal_profiles[bucket] = TemporalProfile(
                time_bucket=bucket,
                embedding=self._temporal_embeddings[bucket],
                sample_count=1,
                avg_confidence=sample.confidence,
                last_updated=sample.timestamp
            )
        else:
            prof = self.state.temporal_profiles[bucket]
            prof.sample_count += 1
            prof.avg_confidence = (
                (prof.avg_confidence * (prof.sample_count - 1) + sample.confidence) /
                prof.sample_count
            )
            prof.last_updated = sample.timestamp
            prof.embedding = self._temporal_embeddings[bucket]

    def _update_confidence_trend(self):
        """Update confidence trend based on recent history."""
        if len(self.state.confidence_history) >= 10:
            recent = np.mean(self.state.confidence_history[:5])
            older = np.mean(self.state.confidence_history[5:10])
            if recent > older + 0.02:
                self.state.confidence_trend = 'improving'
            elif recent < older - 0.02:
                self.state.confidence_trend = 'declining'
            else:
                self.state.confidence_trend = 'stable'

    async def _persist_updated_profile(self):
        """Write updated embedding to database."""
        if self._reference_embedding is None:
            return

        try:
            from intelligence.learning_database import get_learning_database
            db = await get_learning_database()

            # Update the speaker profile with new embedding
            embedding_bytes = self._reference_embedding.tobytes()

            # Also store temporal profiles
            temporal_json = json.dumps({
                bucket: {
                    'embedding': emb.tolist(),
                    'sample_count': self.state.temporal_profiles.get(bucket, TemporalProfile(
                        time_bucket=bucket,
                        embedding=emb,
                        sample_count=1,
                        avg_confidence=0.0,
                        last_updated=datetime.now()
                    )).sample_count
                }
                for bucket, emb in self._temporal_embeddings.items()
            })

            await db.update_speaker_profile(
                speaker_name=self.speaker_name,
                embedding=embedding_bytes,
                metadata={
                    'last_learning_update': datetime.now().isoformat(),
                    'total_samples_learned': self.state.total_samples_learned,
                    'current_avg_confidence': self.state.current_avg_confidence,
                    'confidence_trend': self.state.confidence_trend,
                    'temporal_profiles': temporal_json
                }
            )

            self.state.last_profile_update = datetime.now()
            logger.info(f"🧠 [LEARNING-ENGINE] Persisted updated profile to database")

        except Exception as e:
            logger.error(f"🧠 [LEARNING-ENGINE] Failed to persist profile: {e}")

    # =========================================================================
    # QUERY METHODS
    # =========================================================================

    async def get_best_reference_for_time(self, hour: int = None) -> Optional[np.ndarray]:
        """
        Get the best reference embedding for a specific time of day.

        If we have a temporal profile for this time, blend it with
        the main profile for better accuracy.
        """
        if not self._initialized:
            await self.initialize()

        if self._reference_embedding is None:
            return None

        hour = hour if hour is not None else datetime.now().hour
        bucket = self._get_time_bucket(hour)

        if bucket in self._temporal_embeddings:
            # Blend main profile with temporal profile
            # Weight temporal profile more heavily (60/40)
            blended = (
                0.4 * self._reference_embedding +
                0.6 * self._temporal_embeddings[bucket]
            )
            return blended / np.linalg.norm(blended)

        return self._reference_embedding

    async def get_learning_stats(self) -> Dict[str, Any]:
        """Get comprehensive learning statistics."""
        if not self._initialized:
            await self.initialize()

        return {
            'speaker_name': self.speaker_name,
            'total_samples_learned': self.state.total_samples_learned,
            'current_avg_confidence': self.state.current_avg_confidence,
            'confidence_trend': self.state.confidence_trend,
            'improvement_since_start': self.state.improvement_since_start,
            'temporal_profiles': {
                bucket: {
                    'sample_count': prof.sample_count,
                    'avg_confidence': prof.avg_confidence,
                    'last_updated': prof.last_updated.isoformat() if prof.last_updated else None
                }
                for bucket, prof in self.state.temporal_profiles.items()
            },
            'last_profile_update': (
                self.state.last_profile_update.isoformat()
                if self.state.last_profile_update else None
            ),
            'confidence_history_length': len(self.state.confidence_history),
            'has_reference_embedding': self._reference_embedding is not None,
            'temporal_buckets_available': list(self._temporal_embeddings.keys())
        }

    # =========================================================================
    # ADVANCED PROFILE OPTIMIZATION METHODS
    # =========================================================================

    def _get_adaptive_learning_rate(self) -> float:
        """
        Calculate adaptive learning rate based on profile maturity.

        Uses exponential decay: higher rate for new profiles, lower for mature ones.
        This balances rapid learning initially with stability once profile is established.
        """
        if self.state is None:
            return self.config.embedding_learning_rate

        samples = self.state.total_samples_learned

        # Profile maturity factor (0 to 1)
        maturity = min(1.0, samples / self.config.mature_profile_samples)

        # Exponential decay from max to min learning rate
        lr_range = self.config.max_learning_rate - self.config.min_learning_rate
        adaptive_lr = self.config.min_learning_rate + lr_range * (1 - maturity)

        # Apply additional decay based on trend
        if self.state.confidence_trend == 'improving':
            # Keep higher learning rate if improving
            adaptive_lr *= 1.1
        elif self.state.confidence_trend == 'declining':
            # Reduce learning rate if declining (stabilize)
            adaptive_lr *= 0.8

        return max(self.config.min_learning_rate,
                   min(self.config.max_learning_rate, adaptive_lr))

    async def compute_profile_quality_score(self) -> Dict[str, Any]:
        """
        Compute comprehensive profile quality score using statistical analysis.

        Returns a score from 0-1 indicating how robust the profile is for
        achieving 92%+ confidence consistently.

        Components:
        - Diversity: How varied are the samples (different times, conditions)?
        - Consistency: How consistent are the embeddings (low variance)?
        - Coverage: How many temporal buckets are covered?
        - Recency: How recent are the samples?
        """
        if not self._sample_buffer:
            return {
                'quality_score': 0.0,
                'diversity_score': 0.0,
                'consistency_score': 0.0,
                'coverage_score': 0.0,
                'recency_score': 0.0,
                'recommendations': ['Need to enroll voice samples first']
            }

        recommendations = []

        # 1. DIVERSITY SCORE - How varied are samples?
        # Measure variance in confidence, time distribution, quality
        confidences = [s.confidence for s in self._sample_buffer]
        hours = [s.hour_of_day for s in self._sample_buffer]

        # Good diversity = samples across different conditions
        hour_entropy = self._calculate_entropy(hours, bins=6)  # 6 time buckets
        confidence_std = np.std(confidences) if len(confidences) > 1 else 0

        # Optimal: high hour entropy (varied times), low confidence std (consistent quality)
        diversity_score = min(1.0, hour_entropy / 2.0) * (1 - min(1.0, confidence_std * 2))

        if hour_entropy < 1.0:
            recommendations.append("Enroll samples at different times of day (morning, evening, night)")

        # 2. CONSISTENCY SCORE - How consistent are embeddings?
        if len(self._sample_buffer) >= 3:
            embeddings = np.array([s.embedding for s in self._sample_buffer[:20]])

            # Calculate pairwise similarities
            similarities = []
            for i in range(len(embeddings)):
                for j in range(i + 1, len(embeddings)):
                    sim = np.dot(embeddings[i], embeddings[j]) / (
                        np.linalg.norm(embeddings[i]) * np.linalg.norm(embeddings[j])
                    )
                    similarities.append(sim)

            if similarities:
                avg_similarity = np.mean(similarities)
                sim_std = np.std(similarities)

                # High avg similarity + low std = consistent
                consistency_score = max(0, (avg_similarity - 0.5) * 2) * (1 - min(1.0, sim_std * 5))
            else:
                consistency_score = 0.5
        else:
            consistency_score = 0.3
            recommendations.append("Need more voice samples for consistency analysis")

        # 3. COVERAGE SCORE - How many temporal buckets are covered?
        covered_buckets = len(self.state.temporal_profiles) if self.state else 0
        total_buckets = len(self.config.temporal_buckets)
        coverage_score = covered_buckets / total_buckets

        if coverage_score < 0.5:
            missing = set(self.config.temporal_buckets.keys()) - set(self.state.temporal_profiles.keys() if self.state else [])
            recommendations.append(f"Enroll samples during: {', '.join(list(missing)[:3])}")

        # 4. RECENCY SCORE - How recent are samples?
        now = datetime.now()
        ages_days = [(now - s.timestamp).days for s in self._sample_buffer]

        if ages_days:
            avg_age = np.mean(ages_days)
            recency_score = max(0, 1 - (avg_age / 30))  # Decay over 30 days
        else:
            recency_score = 0.0

        if np.mean(ages_days) > 14:
            recommendations.append("Profile samples are aging - consider re-enrolling")

        # Weighted total score
        quality_score = (
            self.config.diversity_weight * diversity_score +
            self.config.consistency_weight * consistency_score +
            self.config.coverage_weight * coverage_score +
            self.config.recency_weight * recency_score
        )

        # Add specific recommendations based on score
        if quality_score < 0.5:
            recommendations.append("Profile needs significant improvement for 92%+ accuracy")
        elif quality_score < 0.7:
            recommendations.append("Profile is moderate - follow recommendations for better accuracy")
        elif quality_score >= 0.85:
            recommendations.append("Profile is excellent - should consistently achieve 92%+ accuracy")

        return {
            'quality_score': quality_score,
            'diversity_score': diversity_score,
            'consistency_score': consistency_score,
            'coverage_score': coverage_score,
            'recency_score': recency_score,
            'sample_count': len(self._sample_buffer),
            'temporal_coverage': covered_buckets,
            'avg_confidence': np.mean(confidences) if confidences else 0,
            'recommendations': recommendations
        }

    def _calculate_entropy(self, values: List[int], bins: int) -> float:
        """Calculate Shannon entropy for distribution analysis."""
        if not values:
            return 0.0

        # Bin the values
        hist, _ = np.histogram(values, bins=bins, range=(0, 24))
        hist = hist / hist.sum() if hist.sum() > 0 else hist

        # Calculate entropy (higher = more diverse)
        entropy = 0.0
        for p in hist:
            if p > 0:
                entropy -= p * np.log2(p)

        return entropy

    async def optimize_profile(self) -> Dict[str, Any]:
        """
        Optimize the voice profile by removing weak samples and recomputing embedding.

        Uses statistical analysis to:
        1. Identify and remove outlier samples
        2. Weight remaining samples by quality
        3. Recompute optimized reference embedding
        4. Update temporal profiles

        Returns optimization results and improvements.
        """
        if not self._initialized:
            await self.initialize()

        if len(self._sample_buffer) < self.config.min_samples_for_optimization:
            return {
                'optimized': False,
                'reason': f'Need at least {self.config.min_samples_for_optimization} samples',
                'current_samples': len(self._sample_buffer)
            }

        original_count = len(self._sample_buffer)
        original_avg_conf = np.mean([s.confidence for s in self._sample_buffer])

        # Step 1: Statistical outlier detection using IQR method
        embeddings = np.array([s.embedding for s in self._sample_buffer])
        confidences = np.array([s.confidence for s in self._sample_buffer])

        # Calculate similarity to mean embedding
        # v226.0: Safe normalization prevents NaN from zero-norm mean/samples
        mean_embedding = np.mean(embeddings, axis=0)
        mean_normalized = _safe_normalize(mean_embedding)
        if mean_normalized is None:
            return {
                'optimized': False,
                'reason': 'Mean embedding is degenerate (zero-norm or NaN)',
                'would_remove': 0,
                'would_remain': original_count
            }
        mean_embedding = mean_normalized

        similarities = []
        for emb in embeddings:
            emb_norm = _safe_normalize(emb)
            if emb_norm is None:
                similarities.append(0.0)  # Degenerate → treated as worst similarity
            else:
                sim = np.dot(mean_embedding, emb_norm)
                similarities.append(sim)
        similarities = np.array(similarities)

        # IQR-based outlier detection
        q1, q3 = np.percentile(similarities, [25, 75])
        iqr = q3 - q1
        lower_bound = q1 - self.config.outlier_iqr_multiplier * iqr
        upper_bound = q3 + self.config.outlier_iqr_multiplier * iqr

        # Identify samples to keep
        keep_mask = (similarities >= lower_bound) & (similarities <= upper_bound)
        keep_mask &= (confidences >= self.config.weak_sample_threshold)

        # Step 2: Keep only good samples
        optimized_buffer = [
            s for s, keep in zip(self._sample_buffer, keep_mask) if keep
        ]

        removed_count = original_count - len(optimized_buffer)

        if len(optimized_buffer) < 5:
            return {
                'optimized': False,
                'reason': 'Too few samples would remain after optimization',
                'would_remove': removed_count,
                'would_remain': len(optimized_buffer)
            }

        # Step 3: Recompute optimized reference embedding
        # Weight by confidence for optimal embedding
        opt_embeddings = np.array([s.embedding for s in optimized_buffer])
        opt_confidences = np.array([s.confidence for s in optimized_buffer])

        # Softmax weighting based on confidence
        weights = np.exp(opt_confidences * 5)  # Temperature scaling
        weights = weights / weights.sum()

        optimized_embedding = np.average(opt_embeddings, axis=0, weights=weights)
        # v226.0: Safe normalization prevents NaN from zero-norm weighted average
        opt_normalized = _safe_normalize(optimized_embedding)
        if opt_normalized is None:
            return {
                'optimized': False,
                'reason': 'Optimized embedding is degenerate (zero-norm or NaN)',
                'would_remove': 0,
                'would_remain': original_count
            }
        optimized_embedding = opt_normalized

        # Step 4: Update state
        old_embedding = self._reference_embedding
        self._reference_embedding = optimized_embedding
        self._sample_buffer = optimized_buffer

        # Rebuild temporal profiles
        self._temporal_embeddings.clear()
        await self._build_temporal_profiles()

        # Persist changes
        await self._persist_updated_profile()

        # Calculate improvement metrics
        new_avg_conf = np.mean([s.confidence for s in self._sample_buffer])

        # Calculate embedding distance from old to new
        if old_embedding is not None:
            embedding_change = 1 - np.dot(
                old_embedding / np.linalg.norm(old_embedding),
                optimized_embedding
            )
        else:
            embedding_change = 1.0

        logger.info(
            f"🧠 [PROFILE-OPTIMIZER] Optimized profile | "
            f"Removed {removed_count} weak samples | "
            f"Remaining: {len(optimized_buffer)} | "
            f"Avg conf: {original_avg_conf:.1%} → {new_avg_conf:.1%} | "
            f"Embedding change: {embedding_change:.2%}"
        )

        return {
            'optimized': True,
            'samples_removed': removed_count,
            'samples_remaining': len(optimized_buffer),
            'original_avg_confidence': original_avg_conf,
            'new_avg_confidence': new_avg_conf,
            'confidence_improvement': new_avg_conf - original_avg_conf,
            'embedding_change': embedding_change,
            'temporal_profiles_rebuilt': len(self._temporal_embeddings)
        }

    async def compute_confidence_boost(
        self,
        base_confidence: float,
        embedding: np.ndarray,
        hour: int = None,
        behavioral_score: float = None
    ) -> Dict[str, Any]:
        """
        Compute multi-factor confidence boost for the current verification.

        Uses physics/statistics to boost confidence when multiple factors align:
        1. Temporal match: Voice matches time-of-day pattern
        2. Behavioral: Matches typical usage patterns
        3. Consistency: Recent verifications are consistent

        Returns boosted confidence and breakdown.
        """
        if not self.config.enable_multi_factor_boost:
            return {
                'boosted_confidence': base_confidence,
                'boost_applied': 0.0,
                'boost_breakdown': {}
            }

        hour = hour if hour is not None else datetime.now().hour
        total_boost = 0.0
        boost_breakdown = {}

        # 1. TEMPORAL MATCH BOOST
        # If current voice matches the time-specific profile well
        bucket = self._get_time_bucket(hour)
        if bucket in self._temporal_embeddings:
            temporal_emb = self._temporal_embeddings[bucket]
            emb_norm = embedding / np.linalg.norm(embedding)
            temp_norm = temporal_emb / np.linalg.norm(temporal_emb)
            temporal_sim = np.dot(emb_norm, temp_norm)

            # Boost if matches temporal profile better than average
            if temporal_sim > 0.85:
                temporal_boost = self.config.temporal_match_boost * (temporal_sim - 0.85) / 0.15
                total_boost += temporal_boost
                boost_breakdown['temporal'] = temporal_boost

        # 2. BEHAVIORAL BOOST
        # If provided behavioral score is high
        if behavioral_score is not None and behavioral_score > 0.8:
            behavioral_boost = self.config.behavioral_boost * (behavioral_score - 0.8) / 0.2
            total_boost += behavioral_boost
            boost_breakdown['behavioral'] = behavioral_boost

        # 3. CONSISTENCY BOOST
        # If recent verifications show consistent pattern
        if self.state and len(self.state.confidence_history) >= 5:
            recent_conf = self.state.confidence_history[:5]
            consistency = 1 - np.std(recent_conf)  # Low std = consistent
            if consistency > 0.9 and np.mean(recent_conf) > 0.85:
                consistency_boost = self.config.consistency_boost * (consistency - 0.9) / 0.1
                total_boost += consistency_boost
                boost_breakdown['consistency'] = consistency_boost

        # Cap total boost
        total_boost = min(total_boost, self.config.max_total_boost)

        boosted_confidence = min(1.0, base_confidence + total_boost)

        if total_boost > 0.01:
            logger.info(
                f"🚀 [CONFIDENCE-BOOST] Applied {total_boost:.1%} boost | "
                f"{base_confidence:.1%} → {boosted_confidence:.1%} | "
                f"Factors: {boost_breakdown}"
            )

        return {
            'boosted_confidence': boosted_confidence,
            'boost_applied': total_boost,
            'boost_breakdown': boost_breakdown,
            'base_confidence': base_confidence
        }

    async def get_enrollment_guidance(self) -> Dict[str, Any]:
        """
        Get intelligent guidance for improving voice profile enrollment.

        Analyzes current profile and provides specific recommendations
        to achieve 92%+ confidence consistently.
        """
        quality = await self.compute_profile_quality_score()

        guidance = {
            'current_quality': quality['quality_score'],
            'target_quality': 0.85,  # Target for 92%+ accuracy
            'gap': max(0, 0.85 - quality['quality_score']),
            'steps': [],
            'priority_areas': []
        }

        # Prioritize based on weakest scores
        scores = [
            ('diversity', quality['diversity_score']),
            ('consistency', quality['consistency_score']),
            ('coverage', quality['coverage_score']),
            ('recency', quality['recency_score'])
        ]
        scores.sort(key=lambda x: x[1])

        for area, score in scores:
            if score < 0.7:
                guidance['priority_areas'].append({
                    'area': area,
                    'score': score,
                    'target': 0.8
                })

        # Generate specific steps
        if quality['diversity_score'] < 0.7:
            guidance['steps'].append({
                'action': 'enroll_diverse_times',
                'description': 'Record samples at different times of day',
                'details': 'Try morning, afternoon, and evening sessions'
            })

        if quality['consistency_score'] < 0.7:
            guidance['steps'].append({
                'action': 'optimize_profile',
                'description': 'Run profile optimization to remove weak samples',
                'command': 'JARVIS, optimize my voice profile'
            })

        if quality['coverage_score'] < 0.5:
            missing = set(self.config.temporal_buckets.keys()) - set(
                self.state.temporal_profiles.keys() if self.state else []
            )
            guidance['steps'].append({
                'action': 'fill_temporal_gaps',
                'description': f'Enroll during: {", ".join(list(missing)[:3])}',
                'missing_buckets': list(missing)
            })

        if quality['sample_count'] < 20:
            guidance['steps'].append({
                'action': 'add_samples',
                'description': f'Add {20 - quality["sample_count"]} more high-quality samples',
                'current': quality['sample_count'],
                'target': 20
            })

        if quality['recency_score'] < 0.6:
            guidance['steps'].append({
                'action': 're_enroll',
                'description': 'Re-enroll with fresh voice samples',
                'reason': 'Existing samples are too old'
            })

        return guidance


# =============================================================================
# GLOBAL INSTANCE & INTEGRATION
# =============================================================================
_learning_engine: Optional[VoiceProfileLearningEngine] = None
_learning_lock = LazyAsyncLock()  # v100.1: Lazy initialization to avoid "no running event loop" error


async def get_learning_engine(speaker_name: str = "Derek J. Russell") -> VoiceProfileLearningEngine:
    """Get or create the global learning engine instance."""
    global _learning_engine

    async with _learning_lock:
        if _learning_engine is None:
            _learning_engine = VoiceProfileLearningEngine(speaker_name)
            await _learning_engine.initialize()

        return _learning_engine


async def learn_from_vbi_unlock(
    embedding: np.ndarray,
    confidence: float,
    success: bool,
    speaker_name: str = "Derek J. Russell",
    audio_quality: str = None
) -> Dict[str, Any]:
    """
    Convenience function to learn from a VBI unlock attempt.

    Call this after every VBI authentication to continuously improve recognition.

    Args:
        embedding: 192-dim ECAPA-TDNN embedding from the attempt
        confidence: Verification confidence score (0.0-1.0)
        success: Whether authentication succeeded
        speaker_name: Name of the speaker
        audio_quality: Optional quality assessment

    Returns:
        Dict with learning results
    """
    engine = await get_learning_engine(speaker_name)
    return await engine.learn_from_sample(
        embedding=embedding,
        confidence=confidence,
        success=success,
        audio_quality=audio_quality
    )


async def get_optimized_reference(hour: int = None) -> Optional[np.ndarray]:
    """
    Get time-optimized reference embedding for verification.

    This returns a blended embedding that's optimized for the current
    time of day based on learned temporal patterns.

    Args:
        hour: Hour of day (0-23), defaults to current hour

    Returns:
        Optimized reference embedding or None if not available
    """
    engine = await get_learning_engine()
    return await engine.get_best_reference_for_time(hour)
