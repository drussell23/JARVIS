#!/usr/bin/env python3
"""
Intelligent Voice Recognition Router
Cost-aware routing between local (Resemblyzer) and cloud (SpeechBrain)

Strategy:
- Local Resemblyzer (100MB): Fast checks, regular commands (FREE)
- GCP SpeechBrain (2GB): Deep verification, unlock, sensitive ops (~$0.02/hour)
- Auto-shutdown: GCP VM sleeps after 5 minutes of inactivity
- Budget protection: Max 4 hours/day SpeechBrain usage ($2.40/day limit)
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Dict, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)


class VoiceModelType(Enum):
    """Voice recognition model types"""

    RESEMBLYZER_LOCAL = "resemblyzer_local"  # Fast, lightweight, local
    SPEECHBRAIN_CLOUD = "speechbrain_cloud"  # Powerful, cloud-based
    PYANNOTE_LOCAL = "pyannote_local"  # Medium, local (if available)


class VerificationLevel(Enum):
    """Security verification levels"""

    QUICK = "quick"  # Fast check (Resemblyzer)
    STANDARD = "standard"  # Normal verification
    HIGH = "high"  # Sensitive operations (SpeechBrain)
    CRITICAL = "critical"  # Screen unlock, admin commands


@dataclass
class VoiceRecognitionResult:
    """Result from voice recognition"""

    speaker_name: str
    confidence: float
    model_used: VoiceModelType
    embedding: np.ndarray
    latency_ms: float
    cost_cents: float = 0.0


@dataclass
class CloudBudget:
    """Cloud compute budget tracking"""

    daily_limit_cents: float = 240.0  # $2.40/day
    daily_usage_cents: float = 0.0
    last_reset: datetime = None
    total_inference_count: int = 0
    total_saved_cents: float = 0.0  # Money saved by using local


class IntelligentVoiceRouter:
    """
    Routes voice recognition to optimal model based on:
    - Security requirements
    - Cost budget
    - Model availability
    - Latency needs
    """

    def __init__(self):
        # Models (lazy-loaded)
        self.resemblyzer_model = None
        self.pyannote_model = None
        self.speechbrain_client = None  # GCP client

        # Budget tracking
        self.budget = CloudBudget(last_reset=datetime.now())

        # Auto-shutdown tracking
        self.last_cloud_use = None
        self.cloud_idle_shutdown_minutes = 5
        self.shutdown_task = None

        # Statistics
        self.stats = {
            "total_requests": 0,
            "local_requests": 0,
            "cloud_requests": 0,
            "budget_blocks": 0,
            "average_local_latency_ms": 0.0,
            "average_cloud_latency_ms": 0.0,
        }

        # Model costs (per inference)
        self.cost_per_inference = {
            VoiceModelType.RESEMBLYZER_LOCAL: 0.0,  # FREE
            VoiceModelType.PYANNOTE_LOCAL: 0.0,  # FREE
            VoiceModelType.SPEECHBRAIN_CLOUD: 0.5,  # $0.005 per inference (~2 seconds)
        }

        logger.info("🎭 Intelligent Voice Router initialized")

    async def initialize(self):
        """Initialize voice models"""
        logger.info("🚀 Initializing voice recognition models...")

        # Load local Resemblyzer (always available)
        await self._load_resemblyzer()

        # Try to load PyAnnote (optional)
        await self._load_pyannote()

        # Initialize GCP client (but don't load model yet)
        await self._init_gcp_client()

        logger.info("✅ Voice router ready")

    async def _load_resemblyzer(self):
        """Load Resemblyzer encoder (lightweight)"""
        try:
            from resemblyzer import VoiceEncoder

            self.resemblyzer_model = VoiceEncoder()
            logger.info("✅ Resemblyzer loaded (100MB RAM)")
        except Exception as e:
            logger.error(f"Failed to load Resemblyzer: {e}")
            self.resemblyzer_model = None

    async def _load_pyannote(self):
        """Load PyAnnote model (medium weight)"""
        try:
            # PyAnnote is optional - only load if available
            from pyannote.audio import Model

            self.pyannote_model = Model.from_pretrained("pyannote/embedding")
            logger.info("✅ PyAnnote loaded (optional)")
        except Exception as e:
            logger.debug(f"PyAnnote not available (optional): {e}")
            self.pyannote_model = None

    async def _init_gcp_client(self):
        """Initialize GCP SpeechBrain client (lazy-loaded)"""
        try:
            from core.hybrid_orchestrator import get_orchestrator

            self.speechbrain_client = get_orchestrator()
            logger.info("✅ GCP SpeechBrain client initialized (model not loaded)")
        except Exception as e:
            logger.warning(f"GCP client unavailable: {e}")
            self.speechbrain_client = None

    async def recognize_speaker(
        self,
        audio_data: bytes,
        verification_level: VerificationLevel = VerificationLevel.STANDARD,
        force_local: bool = False,
    ) -> VoiceRecognitionResult:
        """
        Recognize speaker using optimal model.

        Args:
            audio_data: Raw audio bytes
            verification_level: Security level required
            force_local: Force local model (ignore cloud)

        Returns:
            VoiceRecognitionResult with speaker info
        """
        self.stats["total_requests"] += 1

        # Reset daily budget if needed
        await self._check_budget_reset()

        # Determine which model to use
        model_type = await self._select_model(verification_level, force_local)

        logger.info(
            f"🎭 Using {model_type.value} for verification level {verification_level.value}"
        )

        # Route to appropriate model
        if model_type == VoiceModelType.RESEMBLYZER_LOCAL:
            result = await self._recognize_resemblyzer(audio_data)
        elif model_type == VoiceModelType.PYANNOTE_LOCAL:
            result = await self._recognize_pyannote(audio_data)
        elif model_type == VoiceModelType.SPEECHBRAIN_CLOUD:
            result = await self._recognize_speechbrain(audio_data)
        else:
            raise ValueError(f"Unknown model type: {model_type}")

        # Update statistics
        await self._update_stats(result)

        # Start shutdown timer for cloud models
        if model_type == VoiceModelType.SPEECHBRAIN_CLOUD:
            await self._schedule_cloud_shutdown()

        return result

    async def _select_model(
        self, verification_level: VerificationLevel, force_local: bool
    ) -> VoiceModelType:
        """
        Select optimal model based on requirements and budget.

        Priority:
        1. Force local if requested
        2. Check budget (block cloud if exceeded)
        3. Match verification level to model capability
        4. Fallback to best available local model
        """
        # Force local takes priority
        if force_local:
            return self._get_best_local_model()

        # Check budget before considering cloud
        if not await self._has_budget():
            logger.warning(f"💰 Daily budget exceeded, using local model")
            self.stats["budget_blocks"] += 1
            return self._get_best_local_model()

        # Route based on verification level
        if verification_level == VerificationLevel.CRITICAL:
            # Critical: Always use SpeechBrain if available and budget allows
            if self.speechbrain_client:
                return VoiceModelType.SPEECHBRAIN_CLOUD
            else:
                logger.warning("SpeechBrain unavailable for CRITICAL verification, using local")
                return self._get_best_local_model()

        elif verification_level == VerificationLevel.HIGH:
            # High: Prefer SpeechBrain but allow PyAnnote fallback
            if self.speechbrain_client and await self._has_budget(reserve_cents=50.0):
                return VoiceModelType.SPEECHBRAIN_CLOUD
            elif self.pyannote_model:
                return VoiceModelType.PYANNOTE_LOCAL
            else:
                return VoiceModelType.RESEMBLYZER_LOCAL

        elif verification_level == VerificationLevel.STANDARD:
            # Standard: PyAnnote if available, else Resemblyzer
            if self.pyannote_model:
                return VoiceModelType.PYANNOTE_LOCAL
            else:
                return VoiceModelType.RESEMBLYZER_LOCAL

        else:  # QUICK
            # Quick: Always use Resemblyzer (fastest)
            return VoiceModelType.RESEMBLYZER_LOCAL

    def _get_best_local_model(self) -> VoiceModelType:
        """Get best available local model"""
        if self.pyannote_model:
            return VoiceModelType.PYANNOTE_LOCAL
        elif self.resemblyzer_model:
            return VoiceModelType.RESEMBLYZER_LOCAL
        else:
            raise RuntimeError("No local voice recognition model available")

    async def _has_budget(self, reserve_cents: float = 0.0) -> bool:
        """Check if we have budget for cloud inference"""
        return (self.budget.daily_usage_cents + reserve_cents) < self.budget.daily_limit_cents

    async def _check_budget_reset(self):
        """Reset budget if new day"""
        now = datetime.now()
        if now.date() > self.budget.last_reset.date():
            logger.info(
                f"💰 Budget reset: Used ${self.budget.daily_usage_cents/100:.2f}, "
                f"Saved ${self.budget.total_saved_cents/100:.2f}"
            )
            self.budget.daily_usage_cents = 0.0
            self.budget.last_reset = now

    async def _recognize_resemblyzer(self, audio_data: bytes) -> VoiceRecognitionResult:
        """Recognize using Resemblyzer (local, fast)"""
        start_time = datetime.now()

        if not self.resemblyzer_model:
            raise RuntimeError("Resemblyzer model not loaded")

        try:
            # Run CPU-intensive operations in thread pool to avoid blocking event loop
            def _extract_embedding_sync():
                import io
                import librosa
                audio_array, sr = librosa.load(io.BytesIO(audio_data), sr=16000)
                embedding = self.resemblyzer_model.embed_utterance(audio_array)
                return embedding

            embedding = await asyncio.to_thread(_extract_embedding_sync)

            # Compare to known profiles (from learning database)
            speaker_name, confidence = await self._match_embedding(
                embedding, model_type=VoiceModelType.RESEMBLYZER_LOCAL
            )

            latency_ms = (datetime.now() - start_time).total_seconds() * 1000

            self.stats["local_requests"] += 1

            return VoiceRecognitionResult(
                speaker_name=speaker_name,
                confidence=confidence,
                model_used=VoiceModelType.RESEMBLYZER_LOCAL,
                embedding=embedding,
                latency_ms=latency_ms,
                cost_cents=0.0,
            )

        except Exception as e:
            logger.error(f"Resemblyzer recognition failed: {e}")
            raise

    async def _recognize_pyannote(self, audio_data: bytes) -> VoiceRecognitionResult:
        """Recognize using PyAnnote (local, medium)"""
        start_time = datetime.now()

        if not self.pyannote_model:
            raise RuntimeError("PyAnnote model not loaded")

        try:
            # Run CPU-intensive operations in thread pool to avoid blocking event loop
            def _extract_embedding_sync():
                import io
                import librosa
                import torch
                torch.set_num_threads(1)  # Prevent thread pool exhaustion
                audio_array, sr = librosa.load(io.BytesIO(audio_data), sr=16000)
                with torch.no_grad():
                    waveform = torch.from_numpy(audio_array).unsqueeze(0)
                    embedding = self.pyannote_model(waveform).squeeze().numpy()
                return embedding

            embedding = await asyncio.to_thread(_extract_embedding_sync)

            # Match to known profiles
            speaker_name, confidence = await self._match_embedding(
                embedding, model_type=VoiceModelType.PYANNOTE_LOCAL
            )

            latency_ms = (datetime.now() - start_time).total_seconds() * 1000

            self.stats["local_requests"] += 1

            return VoiceRecognitionResult(
                speaker_name=speaker_name,
                confidence=confidence,
                model_used=VoiceModelType.PYANNOTE_LOCAL,
                embedding=embedding,
                latency_ms=latency_ms,
                cost_cents=0.0,
            )

        except Exception as e:
            logger.error(f"PyAnnote recognition failed: {e}")
            raise

    async def _recognize_speechbrain(self, audio_data: bytes) -> VoiceRecognitionResult:
        """Recognize using SpeechBrain (GCP, powerful)"""
        start_time = datetime.now()

        if not self.speechbrain_client:
            raise RuntimeError("SpeechBrain client not available")

        try:
            # Send to GCP for processing
            result = await self.speechbrain_client.execute_ml_task(
                task_type="speaker_recognition",
                audio_data=audio_data,
                model_name="speechbrain/spkrec-ecapa-voxceleb",
            )

            embedding = np.array(result["embedding"])
            speaker_name = result.get("speaker_name", "Unknown")
            confidence = result.get("confidence", 0.0)

            latency_ms = (datetime.now() - start_time).total_seconds() * 1000

            # Track cost
            cost = self.cost_per_inference[VoiceModelType.SPEECHBRAIN_CLOUD]
            self.budget.daily_usage_cents += cost
            self.budget.total_inference_count += 1

            self.stats["cloud_requests"] += 1
            self.last_cloud_use = datetime.now()

            logger.info(
                f"💰 SpeechBrain inference: ${cost/100:.4f} "
                f"(daily total: ${self.budget.daily_usage_cents/100:.2f})"
            )

            return VoiceRecognitionResult(
                speaker_name=speaker_name,
                confidence=confidence,
                model_used=VoiceModelType.SPEECHBRAIN_CLOUD,
                embedding=embedding,
                latency_ms=latency_ms,
                cost_cents=cost,
            )

        except Exception as e:
            logger.error(f"SpeechBrain recognition failed: {e}")
            raise

    async def _match_embedding(
        self, embedding: np.ndarray, model_type: VoiceModelType
    ) -> Tuple[str, float]:
        """
        Match embedding to known speaker profiles from learning database.

        Args:
            embedding: Voice embedding vector
            model_type: Which model generated this embedding

        Returns:
            (speaker_name, confidence)
        """
        try:
            from intelligence.learning_database import get_learning_database

            db = await get_learning_database()

            # Get all speaker profiles
            profiles = await db.get_all_speaker_profiles()

            if not profiles:
                return "Unknown", 0.0

            best_match = None
            best_similarity = 0.0

            for profile in profiles:
                # Get stored embedding
                stored_embedding_bytes = profile.get("voiceprint_embedding")
                if not stored_embedding_bytes:
                    continue

                # Deserialize embedding
                stored_embedding = np.frombuffer(stored_embedding_bytes, dtype=np.float32)

                # Compute cosine similarity
                similarity = np.dot(embedding, stored_embedding) / (
                    np.linalg.norm(embedding) * np.linalg.norm(stored_embedding)
                )

                if similarity > best_similarity:
                    best_similarity = similarity
                    best_match = profile["speaker_name"]

            # Threshold for recognition (adjust based on model)
            thresholds = {
                VoiceModelType.RESEMBLYZER_LOCAL: 0.70,
                VoiceModelType.PYANNOTE_LOCAL: 0.75,
                VoiceModelType.SPEECHBRAIN_CLOUD: 0.80,
            }

            threshold = thresholds.get(model_type, 0.75)

            if best_similarity >= threshold:
                return best_match, best_similarity
            else:
                return "Unknown", best_similarity

        except Exception as e:
            logger.error(f"Embedding matching failed: {e}")
            return "Unknown", 0.0

    async def _schedule_cloud_shutdown(self):
        """Schedule GCP VM shutdown after idle period"""
        # Cancel existing shutdown task
        if self.shutdown_task:
            self.shutdown_task.cancel()

        # Schedule new shutdown
        async def shutdown_after_idle():
            await asyncio.sleep(self.cloud_idle_shutdown_minutes * 60)

            # Check if still idle
            if self.last_cloud_use:
                idle_duration = datetime.now() - self.last_cloud_use
                if idle_duration.total_seconds() >= (self.cloud_idle_shutdown_minutes * 60):
                    logger.info(
                        f"💤 Shutting down GCP SpeechBrain after {self.cloud_idle_shutdown_minutes}min idle"
                    )
                    await self._shutdown_gcp_vm()

        self.shutdown_task = asyncio.create_task(shutdown_after_idle())

    async def _shutdown_gcp_vm(self):
        """Shutdown GCP VM to save costs"""
        try:
            if self.speechbrain_client:
                await self.speechbrain_client.shutdown_vm()
                logger.info("✅ GCP VM shutdown (will auto-start on next request)")
        except Exception as e:
            logger.error(f"Failed to shutdown GCP VM: {e}")

    async def _update_stats(self, result: VoiceRecognitionResult):
        """Update router statistics"""
        if result.model_used == VoiceModelType.SPEECHBRAIN_CLOUD:
            # Update cloud latency
            n = self.stats["cloud_requests"]
            self.stats["average_cloud_latency_ms"] = (
                self.stats["average_cloud_latency_ms"] * (n - 1) + result.latency_ms
            ) / n
        else:
            # Update local latency
            n = self.stats["local_requests"]
            self.stats["average_local_latency_ms"] = (
                self.stats["average_local_latency_ms"] * (n - 1) + result.latency_ms
            ) / n

        # Track savings (if we used local instead of cloud)
        if result.cost_cents == 0.0:
            saved = self.cost_per_inference[VoiceModelType.SPEECHBRAIN_CLOUD]
            self.budget.total_saved_cents += saved

    def get_stats(self) -> Dict:
        """Get router statistics"""
        return {
            **self.stats,
            "budget": {
                "daily_limit_dollars": self.budget.daily_limit_cents / 100,
                "daily_usage_dollars": self.budget.daily_usage_cents / 100,
                "total_saved_dollars": self.budget.total_saved_cents / 100,
                "remaining_budget_dollars": (
                    self.budget.daily_limit_cents - self.budget.daily_usage_cents
                )
                / 100,
            },
            "models_available": {
                "resemblyzer": self.resemblyzer_model is not None,
                "pyannote": self.pyannote_model is not None,
                "speechbrain_cloud": self.speechbrain_client is not None,
            },
        }


# Global singleton
_voice_router: Optional[IntelligentVoiceRouter] = None


def get_voice_router() -> IntelligentVoiceRouter:
    """Get global voice router instance"""
    global _voice_router
    if _voice_router is None:
        _voice_router = IntelligentVoiceRouter()
    return _voice_router
