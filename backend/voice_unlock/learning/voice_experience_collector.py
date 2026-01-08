"""
Voice Experience Collector - Collect Experiences for Training Pipeline.
=======================================================================

Collects voice authentication experiences and forwards them to the
Reactor-Core training pipeline via CrossRepoHub.

Features:
1. Automatic experience capture on authentication events
2. Embedding + outcome + trace collection
3. Batch forwarding to Reactor-Core
4. Fallback to experience queue when Reactor unavailable
5. Privacy-aware data handling
6. Experience quality scoring

Architecture:
    ┌─────────────────────────────────────────────────────────────────────┐
    │  VoiceExperienceCollector                                           │
    │  ├── ExperienceCapture (hooks into auth pipeline)                   │
    │  ├── QualityScorer (scores experience usefulness)                   │
    │  ├── PrivacyFilter (removes/masks sensitive data)                   │
    │  └── ReactorForwarder (sends to training pipeline)                  │
    └─────────────────────────────────────────────────────────────────────┘

Author: JARVIS Trinity v81.0 - Unified Learning Loop
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# =============================================================================
# Configuration
# =============================================================================

def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except ValueError:
        return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    val = os.getenv(key, str(default)).lower()
    return val in ("true", "1", "yes", "on")


# =============================================================================
# Types and Enums
# =============================================================================

class ExperienceOutcome(Enum):
    """Outcome of an authentication experience."""
    TRUE_POSITIVE = "true_positive"      # Correct accept
    TRUE_NEGATIVE = "true_negative"      # Correct reject
    FALSE_POSITIVE = "false_positive"    # Incorrect accept
    FALSE_NEGATIVE = "false_negative"    # Incorrect reject
    TIMEOUT = "timeout"
    ERROR = "error"
    UNKNOWN = "unknown"


class ExperienceQuality(Enum):
    """Quality rating of an experience for training."""
    HIGH = "high"          # Clean audio, clear outcome
    MEDIUM = "medium"      # Usable with some noise
    LOW = "low"            # Noisy but still informative
    UNUSABLE = "unusable"  # Should not be used for training


@dataclass
class VoiceExperience:
    """A single voice authentication experience."""
    # Identity
    experience_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str = ""
    user_id: str = ""

    # Embedding (privacy-aware)
    embedding: Optional[List[float]] = None
    embedding_dim: int = 0
    embedding_model: str = "ecapa_tdnn"

    # Outcome
    outcome: ExperienceOutcome = ExperienceOutcome.UNKNOWN
    confidence: float = 0.0
    threshold_used: float = 0.85
    decision: str = ""

    # Quality metrics
    audio_quality: ExperienceQuality = ExperienceQuality.MEDIUM
    snr_db: float = 0.0
    duration_seconds: float = 0.0

    # Context
    fallback_level: str = "PRIMARY"
    retry_count: int = 0
    time_of_day: str = ""
    environment_type: str = ""

    # Trace for debugging
    execution_trace: List[str] = field(default_factory=list)
    phase_timings: Dict[str, float] = field(default_factory=dict)

    # Metadata
    collected_at: float = field(default_factory=time.time)
    forwarded_at: float = 0.0
    privacy_processed: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for storage/transmission."""
        return {
            "experience_id": self.experience_id,
            "session_id": self.session_id,
            "user_id": self.user_id,
            "embedding": self.embedding,
            "embedding_dim": self.embedding_dim,
            "embedding_model": self.embedding_model,
            "outcome": self.outcome.value,
            "confidence": self.confidence,
            "threshold_used": self.threshold_used,
            "decision": self.decision,
            "audio_quality": self.audio_quality.value,
            "snr_db": self.snr_db,
            "duration_seconds": self.duration_seconds,
            "fallback_level": self.fallback_level,
            "retry_count": self.retry_count,
            "time_of_day": self.time_of_day,
            "environment_type": self.environment_type,
            "execution_trace": self.execution_trace,
            "phase_timings": self.phase_timings,
            "collected_at": self.collected_at,
        }


@dataclass
class CollectionStats:
    """Statistics about experience collection."""
    total_collected: int = 0
    total_forwarded: int = 0
    total_queued: int = 0
    total_dropped: int = 0
    by_outcome: Dict[str, int] = field(default_factory=dict)
    by_quality: Dict[str, int] = field(default_factory=dict)
    avg_confidence: float = 0.0
    collection_rate_per_hour: float = 0.0
    last_collection: float = 0.0
    last_forward: float = 0.0


# =============================================================================
# Voice Experience Collector
# =============================================================================

class VoiceExperienceCollector:
    """
    Collects voice authentication experiences for the training pipeline.

    Hooks into the authentication pipeline to capture:
    - Voice embeddings
    - Authentication outcomes
    - Quality metrics
    - Execution traces

    Forwards experiences to Reactor-Core for model training.

    Usage:
        collector = VoiceExperienceCollector()
        await collector.start()

        # After authentication:
        await collector.collect(
            session_id="auth_123",
            user_id="derek",
            embedding=voice_embedding,
            outcome=ExperienceOutcome.TRUE_POSITIVE,
            confidence=0.92,
        )
    """

    # Collection settings
    MIN_AUDIO_QUALITY_FOR_TRAINING = ExperienceQuality.LOW
    BATCH_SIZE = 50
    FLUSH_INTERVAL_SECONDS = 300  # 5 minutes

    def __init__(
        self,
        enable_privacy_filter: bool = True,
        enable_quality_scoring: bool = True,
        forward_to_reactor: bool = True,
    ):
        """
        Initialize the experience collector.

        Args:
            enable_privacy_filter: Apply privacy filters to data
            enable_quality_scoring: Score experience quality
            forward_to_reactor: Forward to Reactor-Core (vs just queue)
        """
        self.enable_privacy = enable_privacy_filter
        self.enable_quality = enable_quality_scoring
        self.forward_to_reactor = forward_to_reactor

        # Experience buffer
        self._buffer: List[VoiceExperience] = []
        self._buffer_lock = asyncio.Lock()

        # Statistics
        self._stats = CollectionStats()

        # Background task
        self._running = False
        self._flush_task: Optional[asyncio.Task] = None

        # Callbacks
        self._on_experience_collected: List[Callable[[VoiceExperience], None]] = []
        self._on_batch_forwarded: List[Callable[[int], None]] = []

        logger.info(
            f"[VoiceExperienceCollector] Initialized "
            f"(privacy={enable_privacy_filter}, quality={enable_quality_scoring})"
        )

    async def start(self) -> None:
        """Start the collector background task."""
        if self._running:
            return

        self._running = True
        self._flush_task = asyncio.create_task(self._flush_loop())
        logger.info("[VoiceExperienceCollector] Started")

    async def stop(self) -> None:
        """Stop the collector and flush remaining experiences."""
        self._running = False

        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass

        # Final flush
        await self._flush_buffer()
        logger.info("[VoiceExperienceCollector] Stopped")

    async def collect(
        self,
        session_id: str,
        user_id: str,
        embedding: Optional[List[float]] = None,
        outcome: ExperienceOutcome = ExperienceOutcome.UNKNOWN,
        confidence: float = 0.0,
        decision: str = "",
        audio_metrics: Optional[Dict[str, float]] = None,
        execution_trace: Optional[List[str]] = None,
        phase_timings: Optional[Dict[str, float]] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """
        Collect a voice authentication experience.

        Args:
            session_id: Authentication session ID
            user_id: User ID
            embedding: Voice embedding (will be privacy-processed)
            outcome: Authentication outcome
            confidence: Confidence score
            decision: Decision made (AUTHENTICATED, REJECTED, etc.)
            audio_metrics: Audio quality metrics
            execution_trace: Execution trace
            phase_timings: Phase timing data
            context: Additional context

        Returns:
            Experience ID if collected, None if dropped
        """
        audio_metrics = audio_metrics or {}
        context = context or {}

        # Create experience
        experience = VoiceExperience(
            session_id=session_id,
            user_id=user_id,
            embedding=embedding,
            embedding_dim=len(embedding) if embedding else 0,
            outcome=outcome,
            confidence=confidence,
            decision=decision,
            snr_db=audio_metrics.get("snr_db", 0.0),
            duration_seconds=audio_metrics.get("duration_seconds", 0.0),
            fallback_level=context.get("fallback_level", "PRIMARY"),
            retry_count=context.get("retry_count", 0),
            time_of_day=context.get("time_of_day", ""),
            environment_type=context.get("environment_type", ""),
            execution_trace=execution_trace or [],
            phase_timings=phase_timings or {},
        )

        # Score quality
        if self.enable_quality:
            experience.audio_quality = self._score_quality(experience, audio_metrics)

        # Drop unusable experiences
        if experience.audio_quality == ExperienceQuality.UNUSABLE:
            self._stats.total_dropped += 1
            logger.debug(
                f"[VoiceExperienceCollector] Dropped unusable experience: "
                f"{session_id}"
            )
            return None

        # Apply privacy filter
        if self.enable_privacy:
            experience = self._apply_privacy_filter(experience)

        # Add to buffer
        async with self._buffer_lock:
            self._buffer.append(experience)
            self._update_stats(experience)

        # Notify callbacks
        for callback in self._on_experience_collected:
            try:
                callback(experience)
            except Exception as e:
                logger.debug(f"[VoiceExperienceCollector] Callback error: {e}")

        # Check if buffer should be flushed
        if len(self._buffer) >= self.BATCH_SIZE:
            asyncio.create_task(self._flush_buffer())

        logger.debug(
            f"[VoiceExperienceCollector] Collected experience: "
            f"{experience.experience_id} ({outcome.value})"
        )

        return experience.experience_id

    def _score_quality(
        self,
        experience: VoiceExperience,
        audio_metrics: Dict[str, float],
    ) -> ExperienceQuality:
        """Score the quality of an experience for training."""
        snr = audio_metrics.get("snr_db", 0.0)
        duration = audio_metrics.get("duration_seconds", 0.0)

        # SNR-based scoring
        if snr >= 20:
            snr_score = 3
        elif snr >= 12:
            snr_score = 2
        elif snr >= 6:
            snr_score = 1
        else:
            snr_score = 0

        # Duration-based scoring
        if 0.5 <= duration <= 5.0:
            duration_score = 2
        elif duration > 5.0:
            duration_score = 1
        else:
            duration_score = 0

        # Outcome clarity
        if experience.outcome in (ExperienceOutcome.TRUE_POSITIVE, ExperienceOutcome.TRUE_NEGATIVE):
            outcome_score = 2
        elif experience.outcome in (ExperienceOutcome.FALSE_POSITIVE, ExperienceOutcome.FALSE_NEGATIVE):
            outcome_score = 1  # Still valuable for learning
        else:
            outcome_score = 0

        # Combined score
        total_score = snr_score + duration_score + outcome_score

        if total_score >= 6:
            return ExperienceQuality.HIGH
        elif total_score >= 4:
            return ExperienceQuality.MEDIUM
        elif total_score >= 2:
            return ExperienceQuality.LOW
        else:
            return ExperienceQuality.UNUSABLE

    def _apply_privacy_filter(self, experience: VoiceExperience) -> VoiceExperience:
        """Apply privacy filters to experience data."""
        # Hash user ID instead of storing plaintext
        if experience.user_id:
            hashed = hashlib.sha256(experience.user_id.encode()).hexdigest()[:16]
            experience.user_id = f"user_{hashed}"

        # Embedding is already abstract, keep as-is

        # Remove any PII from traces
        filtered_trace = []
        for item in experience.execution_trace:
            # Remove any potential PII patterns
            if "@" not in item and not any(c.isdigit() for c in item[:5]):
                filtered_trace.append(item)
        experience.execution_trace = filtered_trace

        experience.privacy_processed = True
        return experience

    def _update_stats(self, experience: VoiceExperience) -> None:
        """Update collection statistics."""
        self._stats.total_collected += 1
        self._stats.last_collection = time.time()

        # By outcome
        outcome_key = experience.outcome.value
        self._stats.by_outcome[outcome_key] = self._stats.by_outcome.get(outcome_key, 0) + 1

        # By quality
        quality_key = experience.audio_quality.value
        self._stats.by_quality[quality_key] = self._stats.by_quality.get(quality_key, 0) + 1

        # Running average confidence
        n = self._stats.total_collected
        self._stats.avg_confidence = (
            self._stats.avg_confidence * (n - 1) + experience.confidence
        ) / n

    async def _flush_loop(self) -> None:
        """Background loop to periodically flush buffer."""
        while self._running:
            try:
                await asyncio.sleep(self.FLUSH_INTERVAL_SECONDS)
                await self._flush_buffer()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[VoiceExperienceCollector] Flush loop error: {e}")

    async def _flush_buffer(self) -> None:
        """Flush experience buffer to Reactor-Core or queue."""
        async with self._buffer_lock:
            if not self._buffer:
                return

            experiences = self._buffer.copy()
            self._buffer.clear()

        logger.info(
            f"[VoiceExperienceCollector] Flushing {len(experiences)} experiences"
        )

        if self.forward_to_reactor:
            success = await self._forward_to_reactor(experiences)
        else:
            success = await self._queue_experiences(experiences)

        if success:
            self._stats.total_forwarded += len(experiences)
            self._stats.last_forward = time.time()

            for callback in self._on_batch_forwarded:
                try:
                    callback(len(experiences))
                except Exception:
                    pass
        else:
            # Fallback to queue
            await self._queue_experiences(experiences)
            self._stats.total_queued += len(experiences)

    async def _forward_to_reactor(self, experiences: List[VoiceExperience]) -> bool:
        """Forward experiences to Reactor-Core."""
        try:
            # Try CrossRepoHub first
            from backend.intelligence.cross_repo_hub import get_cross_repo_hub

            hub = await get_cross_repo_hub()

            for experience in experiences:
                await hub.publish_event(
                    event_type="voice_experience",
                    data=experience.to_dict(),
                    target="reactor_core",
                )

            logger.info(
                f"[VoiceExperienceCollector] Forwarded {len(experiences)} "
                f"experiences to Reactor-Core"
            )
            return True

        except ImportError:
            logger.debug("[VoiceExperienceCollector] CrossRepoHub not available")
            return False

        except Exception as e:
            logger.warning(
                f"[VoiceExperienceCollector] Failed to forward to Reactor: {e}"
            )
            return False

    async def _queue_experiences(self, experiences: List[VoiceExperience]) -> bool:
        """Queue experiences when Reactor is unavailable."""
        try:
            from backend.core.experience_queue import (
                get_experience_queue,
                ExperienceType,
                ExperiencePriority,
            )

            queue = await get_experience_queue()

            for experience in experiences:
                await queue.enqueue(
                    experience_type=ExperienceType.VOICE_EMBEDDING,
                    data=experience.to_dict(),
                    priority=ExperiencePriority.NORMAL,
                )

            logger.info(
                f"[VoiceExperienceCollector] Queued {len(experiences)} experiences"
            )
            return True

        except Exception as e:
            logger.error(f"[VoiceExperienceCollector] Failed to queue: {e}")
            return False

    def on_experience_collected(
        self,
        callback: Callable[[VoiceExperience], None],
    ) -> None:
        """Register callback for experience collection."""
        self._on_experience_collected.append(callback)

    def on_batch_forwarded(
        self,
        callback: Callable[[int], None],
    ) -> None:
        """Register callback for batch forwarding."""
        self._on_batch_forwarded.append(callback)

    def get_stats(self) -> CollectionStats:
        """Get collection statistics."""
        return self._stats


# =============================================================================
# Singleton Access
# =============================================================================

_collector_instance: Optional[VoiceExperienceCollector] = None
_collector_lock = asyncio.Lock()


async def get_voice_experience_collector() -> VoiceExperienceCollector:
    """Get the singleton experience collector."""
    global _collector_instance

    async with _collector_lock:
        if _collector_instance is None:
            _collector_instance = VoiceExperienceCollector()
            await _collector_instance.start()
        return _collector_instance


async def collect_voice_experience(
    session_id: str,
    user_id: str,
    **kwargs,
) -> Optional[str]:
    """Convenience function to collect an experience."""
    collector = await get_voice_experience_collector()
    return await collector.collect(session_id, user_id, **kwargs)
