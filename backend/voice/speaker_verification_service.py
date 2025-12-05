"""
Speaker Verification Service for JARVIS
Provides voice biometric verification for security-sensitive operations

Features:
- Speaker identification from audio
- Confidence scoring
- Primary user (owner) detection
- Integration with learning database
- Background pre-loading for instant unlock
- LangGraph-based adaptive authentication reasoning
- ChromaDB voice pattern recognition and anti-spoofing
- Langfuse authentication audit trails
- Helicone voice processing cost optimization
- Multi-factor authentication fusion
- Progressive confidence communication

Enhanced Version: 2.1.0 - Async Optimized (Non-Blocking)
"""

import asyncio
import logging
import threading
import hashlib
import time
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from functools import partial
from typing import Optional, List, Dict, Any, Tuple, Callable
from dataclasses import dataclass, field
from enum import Enum
from uuid import uuid4

import numpy as np

# Import managed executor for clean shutdown
try:
    from core.thread_manager import ManagedThreadPoolExecutor
    _HAS_MANAGED_EXECUTOR = True
except ImportError:
    _HAS_MANAGED_EXECUTOR = False

# ============================================================================
# ASYNC OPTIMIZATION: Shared Thread Pool for CPU-Intensive Operations
# ============================================================================
# All numpy, scipy, and signal processing operations MUST run in thread pool
# to prevent blocking the async event loop. This is CRITICAL for responsiveness.
# ============================================================================

_verification_executor: Optional[ThreadPoolExecutor] = None


def get_verification_executor() -> ThreadPoolExecutor:
    """Get or create the shared thread pool for verification operations."""
    global _verification_executor
    if _verification_executor is None:
        # Use 4 workers for parallel CPU-intensive operations
        if _HAS_MANAGED_EXECUTOR:
            _verification_executor = ManagedThreadPoolExecutor(
                max_workers=4,
                thread_name_prefix="speaker_verify",
                name="speaker_verify"
            )
        else:
            _verification_executor = ThreadPoolExecutor(
                max_workers=4,
                thread_name_prefix="speaker_verify"
            )
        logging.getLogger(__name__).info(
            "🔧 Created speaker verification thread pool (4 workers)"
        )
    return _verification_executor


def shutdown_verification_executor():
    """Shutdown the verification thread pool gracefully."""
    global _verification_executor
    if _verification_executor is not None:
        logging.getLogger(__name__).info(
            "🔧 Shutting down speaker verification thread pool..."
        )
        _verification_executor.shutdown(wait=True)
        _verification_executor = None

# ============================================================================
# CRITICAL FIX: Patch torchaudio for compatibility with version 2.9.0+
# ============================================================================
# Issue: torchaudio.list_audio_backends() was removed in torchaudio 2.9.0
# This breaks SpeechBrain 1.0.3 which still calls the deprecated function
# Solution: Monkey patch torchaudio before importing SpeechBrain
# ============================================================================
try:
    import torchaudio

    # Check if list_audio_backends is missing (torchaudio >= 2.9.0)
    if not hasattr(torchaudio, 'list_audio_backends'):
        logging.getLogger(__name__).info(
            "🔧 Patching torchaudio 2.9.0+ for SpeechBrain compatibility..."
        )

        # Add dummy list_audio_backends function that returns available backends
        # In torchaudio 2.9+, the backend system was simplified - we can safely
        # return a list of known backends without actually checking
        def _list_audio_backends_fallback():
            """
            Fallback implementation for removed torchaudio.list_audio_backends()

            Returns list of potentially available backends. Since torchaudio 2.9+
            handles backend selection automatically, we just return common ones.
            """
            backends = []
            try:
                # Try to import soundfile (most common backend)
                import soundfile
                backends.append('soundfile')
            except ImportError:
                pass

            try:
                # Try to import sox_io
                import torchaudio.backend.sox_io_backend
                backends.append('sox_io')
            except (ImportError, AttributeError):
                pass

            # If no backends found, return default
            if not backends:
                backends = ['soundfile']  # Default assumption

            return backends

        # Monkey patch the missing function
        torchaudio.list_audio_backends = _list_audio_backends_fallback

        logging.getLogger(__name__).info(
            f"✅ torchaudio patched successfully - backends: {torchaudio.list_audio_backends()}"
        )

except ImportError as e:
    logging.getLogger(__name__).warning(f"Could not patch torchaudio: {e}")

# Now safe to import SpeechBrain components
from intelligence.learning_database import JARVISLearningDatabase
from voice.engines.speechbrain_engine import SpeechBrainEngine
from voice.stt_config import ModelConfig, STTEngine

logger = logging.getLogger(__name__)

# ============================================================================
# Optional Dependencies for Enhanced Features
# ============================================================================

# ChromaDB for voice pattern recognition
try:
    import chromadb
    CHROMADB_AVAILABLE = True
except ImportError:
    CHROMADB_AVAILABLE = False
    logger.info("ChromaDB not available - voice pattern store disabled")

# Langfuse for observability (v3.x SDK)
try:
    from langfuse import Langfuse, observe, get_client
    # langfuse_context moved in newer versions
    try:
        from langfuse.decorators import langfuse_context
    except ImportError:
        langfuse_context = None  # Optional in newer versions
    LANGFUSE_AVAILABLE = True
except ImportError:
    LANGFUSE_AVAILABLE = False
    observe = None
    langfuse_context = None
    get_client = None
    logger.info("Langfuse not available - audit trails disabled")

# LangGraph for reasoning
try:
    from langgraph.graph import StateGraph, END
    from langgraph.checkpoint.memory import MemorySaver
    LANGGRAPH_AVAILABLE = True
except ImportError:
    LANGGRAPH_AVAILABLE = False
    logger.info("LangGraph not available - using fallback reasoning")

# ML Engine Registry for singleton ECAPA-TDNN engine
# This prevents multiple instances and runtime HuggingFace downloads
try:
    from voice_unlock.ml_engine_registry import (
        get_ml_registry_sync,
        get_ecapa_encoder_async,
        is_voice_unlock_ready,
        extract_speaker_embedding as registry_extract_embedding,
    )
    ML_REGISTRY_AVAILABLE = True
    logger.info("ML Engine Registry available - using singleton ECAPA-TDNN")
except ImportError:
    ML_REGISTRY_AVAILABLE = False
    get_ml_registry_sync = None
    get_ecapa_encoder_async = None
    is_voice_unlock_ready = lambda: True
    registry_extract_embedding = None
    logger.info("ML Engine Registry not available - using local engine instance")


# ============================================================================
# Enhanced Authentication Enums and Types
# ============================================================================

class AuthenticationPhase(str, Enum):
    """Phases of the authentication process."""
    AUDIO_CAPTURE = "audio_capture"
    VOICE_ANALYSIS = "voice_analysis"
    EMBEDDING_EXTRACTION = "embedding_extraction"
    SPEAKER_VERIFICATION = "speaker_verification"
    BEHAVIORAL_ANALYSIS = "behavioral_analysis"
    ANTI_SPOOFING = "anti_spoofing"
    MULTI_FACTOR_FUSION = "multi_factor_fusion"
    DECISION = "decision"
    COMPLETE = "complete"


class ConfidenceLevel(str, Enum):
    """Human-readable confidence levels for voice feedback."""
    EXCELLENT = "excellent"      # >90%
    GOOD = "good"               # 85-90%
    BORDERLINE = "borderline"   # 80-85%
    LOW = "low"                 # 75-80%
    FAILED = "failed"           # <75%


class ThreatType(str, Enum):
    """Types of detected threats."""
    REPLAY_ATTACK = "replay_attack"
    VOICE_CLONING = "voice_cloning"
    SYNTHETIC_VOICE = "synthetic_voice"
    ENVIRONMENTAL_ANOMALY = "environmental_anomaly"
    UNKNOWN_SPEAKER = "unknown_speaker"
    NONE = "none"


class MigrationStrategy(str, Enum):
    """Strategies for embedding dimension migration."""
    SPECTRAL_RESAMPLE = "spectral_resample"      # FFT-based resampling
    PCA_REDUCTION = "pca_reduction"               # PCA dimensionality reduction
    STATISTICAL_POOLING = "statistical_pooling"  # Block statistics extraction
    INTERPOLATION = "interpolation"              # Cubic/linear interpolation
    HARMONIC_EXPANSION = "harmonic_expansion"    # For upsampling with harmonics
    LEARNED_PROJECTION = "learned_projection"    # Using learned weights if available
    HYBRID = "hybrid"                            # Combination of methods


@dataclass
class MigrationResult:
    """Result of an embedding migration with quality metrics."""
    embedding: np.ndarray
    strategy_used: MigrationStrategy
    quality_score: float  # 0.0-1.0, higher is better
    source_dim: int
    target_dim: int
    norm_preserved: bool
    variance_ratio: float  # How much variance was preserved
    processing_time_ms: float
    metadata: Dict[str, Any] = field(default_factory=dict)


# ============================================================================
# Data Classes for Enhanced Authentication
# ============================================================================

@dataclass
class AuthenticationTrace:
    """Complete trace of an authentication attempt for Langfuse."""
    trace_id: str
    speaker_name: str
    timestamp: datetime
    phases: List[Dict[str, Any]] = field(default_factory=list)

    # Audio metrics
    audio_duration_ms: float = 0.0
    audio_snr_db: float = 0.0
    audio_quality_score: float = 0.0

    # Verification metrics
    voice_confidence: float = 0.0
    behavioral_confidence: float = 0.0
    context_confidence: float = 0.0
    fused_confidence: float = 0.0

    # Decision
    decision: str = "pending"
    threshold_used: float = 0.0

    # Security
    threat_detected: ThreatType = ThreatType.NONE
    anti_spoofing_score: float = 0.0

    # Performance
    total_duration_ms: float = 0.0
    api_cost_usd: float = 0.0

    # Context
    environment: str = "default"
    device: str = "unknown"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "speaker_name": self.speaker_name,
            "timestamp": self.timestamp.isoformat(),
            "phases": self.phases,
            "audio": {
                "duration_ms": self.audio_duration_ms,
                "snr_db": self.audio_snr_db,
                "quality_score": self.audio_quality_score
            },
            "verification": {
                "voice_confidence": self.voice_confidence,
                "behavioral_confidence": self.behavioral_confidence,
                "context_confidence": self.context_confidence,
                "fused_confidence": self.fused_confidence
            },
            "decision": self.decision,
            "threshold_used": self.threshold_used,
            "security": {
                "threat_detected": self.threat_detected.value,
                "anti_spoofing_score": self.anti_spoofing_score
            },
            "performance": {
                "total_duration_ms": self.total_duration_ms,
                "api_cost_usd": self.api_cost_usd
            },
            "context": {
                "environment": self.environment,
                "device": self.device
            }
        }


@dataclass
class VoicePattern:
    """Voice pattern for ChromaDB storage."""
    pattern_id: str
    speaker_name: str
    pattern_type: str  # 'rhythm', 'phrase', 'environment', 'emotion'
    embedding: np.ndarray
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.utcnow)
    success_count: int = 0
    failure_count: int = 0


@dataclass
class VoiceFeedback:
    """Voice feedback message for the user."""
    confidence_level: ConfidenceLevel
    message: str
    suggestion: Optional[str] = None
    is_final: bool = False
    speak_aloud: bool = True


# ============================================================================
# Voice Pattern Store (ChromaDB)
# ============================================================================

class VoicePatternStore:
    """
    ChromaDB-based store for voice patterns and behavioral biometrics.

    Stores:
    - Speaking rhythm patterns
    - Phrase preferences
    - Environmental signatures
    - Emotional baselines
    - Time-of-day voice variations
    """

    def __init__(self, persist_directory: Optional[str] = None):
        self.logger = logging.getLogger(f"{__name__}.VoicePatternStore")
        self._initialized = False
        self._client = None
        self._collection = None
        self.persist_directory = persist_directory or "/tmp/jarvis_voice_patterns"

    async def initialize(self):
        """Initialize ChromaDB client and collection."""
        if not CHROMADB_AVAILABLE:
            self.logger.warning("ChromaDB not available - pattern store disabled")
            return

        try:
            # Use PersistentClient for ChromaDB 0.4.x+ (new API)
            import os
            os.makedirs(self.persist_directory, exist_ok=True)

            self._client = chromadb.PersistentClient(
                path=self.persist_directory,
                settings=chromadb.Settings(anonymized_telemetry=False)
            )

            self._collection = self._client.get_or_create_collection(
                name="voice_patterns",
                metadata={"description": "JARVIS voice behavioral patterns"}
            )

            self._initialized = True
            self.logger.info(f"✅ Voice pattern store initialized with {self._collection.count()} patterns")

        except Exception as e:
            self.logger.error(f"Failed to initialize ChromaDB: {e}")
            self._initialized = False

    async def store_pattern(self, pattern: VoicePattern) -> bool:
        """Store a voice pattern in ChromaDB."""
        if not self._initialized:
            return False

        try:
            self._collection.add(
                ids=[pattern.pattern_id],
                embeddings=[pattern.embedding.tolist()],
                metadatas=[{
                    "speaker_name": pattern.speaker_name,
                    "pattern_type": pattern.pattern_type,
                    "created_at": pattern.created_at.isoformat(),
                    "success_count": pattern.success_count,
                    "failure_count": pattern.failure_count,
                    **pattern.metadata
                }]
            )
            return True
        except Exception as e:
            self.logger.error(f"Failed to store pattern: {e}")
            return False

    async def find_similar_patterns(
        self,
        embedding: np.ndarray,
        speaker_name: str,
        pattern_type: Optional[str] = None,
        top_k: int = 5
    ) -> List[Dict[str, Any]]:
        """Find similar voice patterns for anti-spoofing and behavioral analysis."""
        if not self._initialized:
            return []

        try:
            where_filter = {"speaker_name": speaker_name}
            if pattern_type:
                where_filter["pattern_type"] = pattern_type

            results = self._collection.query(
                query_embeddings=[embedding.tolist()],
                n_results=top_k,
                where=where_filter
            )

            patterns = []
            if results['ids'] and results['ids'][0]:
                for i, pattern_id in enumerate(results['ids'][0]):
                    patterns.append({
                        "pattern_id": pattern_id,
                        "distance": results['distances'][0][i] if results.get('distances') else 0,
                        "metadata": results['metadatas'][0][i] if results.get('metadatas') else {}
                    })
            return patterns

        except Exception as e:
            self.logger.error(f"Failed to query patterns: {e}")
            return []

    async def detect_replay_attack(
        self,
        audio_fingerprint: str,
        speaker_name: str,
        time_window_seconds: int = 300
    ) -> Tuple[bool, float]:
        """
        Detect if this exact audio has been played before (replay attack).

        Returns:
            Tuple of (is_replay, anomaly_score)
        """
        if not self._initialized:
            return False, 0.0

        try:
            # Check for exact fingerprint match in recent history
            recent_cutoff = (datetime.utcnow() - timedelta(seconds=time_window_seconds)).isoformat()

            results = self._collection.get(
                where={
                    "speaker_name": speaker_name,
                    "pattern_type": "audio_fingerprint",
                    "fingerprint": audio_fingerprint
                }
            )

            if results['ids']:
                # Found matching fingerprint - potential replay attack
                return True, 0.95

            return False, 0.0

        except Exception as e:
            self.logger.error(f"Replay detection failed: {e}")
            return False, 0.0

    async def store_audio_fingerprint(
        self,
        speaker_name: str,
        audio_fingerprint: str,
        embedding: np.ndarray
    ):
        """Store audio fingerprint for replay attack detection."""
        pattern = VoicePattern(
            pattern_id=f"fp_{audio_fingerprint[:16]}_{uuid4().hex[:8]}",
            speaker_name=speaker_name,
            pattern_type="audio_fingerprint",
            embedding=embedding,
            metadata={"fingerprint": audio_fingerprint}
        )
        await self.store_pattern(pattern)

    # =========================================================================
    # ENHANCED ANTI-SPOOFING DETECTION METHODS (v2.0)
    # =========================================================================

    async def detect_synthesis_attack(
        self,
        audio_features: Dict[str, float],
        speaker_name: str
    ) -> Tuple[bool, float, List[str]]:
        """
        Detect synthetic/deepfake voice attacks using acoustic anomaly detection.

        Checks for:
        - Unnatural pitch variation (too perfect or too flat)
        - Missing micro-variations (breathing, natural pauses)
        - Spectral artifacts from synthesis
        - Temporal consistency issues

        Args:
            audio_features: Dict with keys like 'pitch_std', 'jitter', 'shimmer', 'hnr', etc.
            speaker_name: The claimed speaker

        Returns:
            Tuple of (is_synthetic, confidence, anomaly_indicators)
        """
        anomaly_indicators: List[str] = []
        anomaly_score = 0.0

        # Check pitch variation - synthetic voices often have unnatural pitch patterns
        pitch_std = audio_features.get('pitch_std', 0)
        if pitch_std < 5:  # Too flat - often indicates synthesis
            anomaly_indicators.append("unnaturally_flat_pitch")
            anomaly_score += 0.25
        elif pitch_std > 100:  # Too variable - might be voice conversion artifact
            anomaly_indicators.append("excessive_pitch_variation")
            anomaly_score += 0.15

        # Check jitter (pitch variation between cycles) - synthetic voices have minimal jitter
        jitter = audio_features.get('jitter', 0)
        if jitter < 0.001:  # Less than 0.1% jitter is suspicious
            anomaly_indicators.append("missing_jitter")
            anomaly_score += 0.20

        # Check shimmer (amplitude variation) - similar to jitter
        shimmer = audio_features.get('shimmer', 0)
        if shimmer < 0.01:  # Less than 1% shimmer is suspicious
            anomaly_indicators.append("missing_shimmer")
            anomaly_score += 0.20

        # Check harmonics-to-noise ratio - synthetic voices often have perfect HNR
        hnr = audio_features.get('hnr', 15)
        if hnr > 35:  # HNR > 35 dB is unusually clean
            anomaly_indicators.append("unnaturally_clean_harmonics")
            anomaly_score += 0.15

        # Check for spectral artifacts
        spectral_flatness = audio_features.get('spectral_flatness', 0.5)
        if spectral_flatness > 0.9:  # Very flat spectrum indicates synthesis
            anomaly_indicators.append("spectral_synthesis_artifact")
            anomaly_score += 0.20

        # Check breathing patterns - natural speech has breath sounds
        has_breathing = audio_features.get('breathing_detected', True)
        if not has_breathing:
            anomaly_indicators.append("missing_breathing_sounds")
            anomaly_score += 0.15

        # Check for frame boundary artifacts (common in generated audio)
        frame_discontinuity = audio_features.get('frame_discontinuity', 0)
        if frame_discontinuity > 0.3:
            anomaly_indicators.append("frame_boundary_artifact")
            anomaly_score += 0.20

        # Normalize score
        anomaly_score = min(1.0, anomaly_score)

        is_synthetic = anomaly_score > 0.50  # Threshold for synthetic detection

        if is_synthetic:
            self.logger.warning(f"⚠️ SYNTHESIS ATTACK DETECTED for {speaker_name}: {anomaly_indicators}")

        return is_synthetic, anomaly_score, anomaly_indicators

    async def detect_voice_conversion_attack(
        self,
        current_embedding: np.ndarray,
        speaker_name: str,
        session_embeddings: Optional[List[np.ndarray]] = None
    ) -> Tuple[bool, float, Dict[str, Any]]:
        """
        Detect voice conversion (VC) attacks where attacker's voice is morphed.

        Voice conversion attacks show:
        - Inconsistent speaker identity across phrases
        - Spectral instability (different formant patterns frame-to-frame)
        - Unusual embedding trajectory during speech

        Args:
            current_embedding: Current 192-dim embedding
            speaker_name: Claimed speaker
            session_embeddings: Multiple embeddings from same session (if available)

        Returns:
            Tuple of (is_voice_conversion, confidence, analysis_details)
        """
        analysis = {
            "embedding_stability": 0.0,
            "formant_consistency": 0.0,
            "speaker_trajectory": 0.0,
            "anomalies": []
        }

        vc_score = 0.0

        if session_embeddings and len(session_embeddings) >= 2:
            # Analyze embedding stability across session
            # Natural speech has stable speaker identity even across phrases
            similarities = []
            for i in range(len(session_embeddings) - 1):
                sim = np.dot(session_embeddings[i], session_embeddings[i + 1]) / (
                    np.linalg.norm(session_embeddings[i]) * np.linalg.norm(session_embeddings[i + 1]) + 1e-10
                )
                similarities.append(sim)

            avg_stability = np.mean(similarities)
            analysis["embedding_stability"] = float(avg_stability)

            # Voice conversion often shows unstable identity
            if avg_stability < 0.85:
                analysis["anomalies"].append("unstable_speaker_identity")
                vc_score += 0.30

            # Check for unnatural trajectory (jumping around in embedding space)
            if len(similarities) > 2:
                stability_std = np.std(similarities)
                if stability_std > 0.15:  # High variance in similarity
                    analysis["anomalies"].append("erratic_embedding_trajectory")
                    vc_score += 0.25
                analysis["speaker_trajectory"] = float(stability_std)

        # Compare against stored patterns for this speaker
        if self._initialized:
            try:
                similar_patterns = await self.find_similar_patterns(
                    current_embedding,
                    speaker_name,
                    pattern_type="audio_fingerprint",
                    top_k=10
                )

                if similar_patterns:
                    # Check consistency with historical patterns
                    distances = [p.get("distance", 1.0) for p in similar_patterns]
                    avg_distance = np.mean(distances)

                    if avg_distance > 0.5:  # Far from known patterns
                        analysis["anomalies"].append("distant_from_known_patterns")
                        vc_score += 0.25

                    analysis["formant_consistency"] = float(1.0 - min(avg_distance, 1.0))

            except Exception as e:
                self.logger.debug(f"Pattern comparison failed: {e}")

        is_vc_attack = vc_score > 0.40
        analysis["vc_score"] = float(vc_score)

        if is_vc_attack:
            self.logger.warning(f"⚠️ VOICE CONVERSION ATTACK suspected for {speaker_name}")

        return is_vc_attack, vc_score, analysis

    async def analyze_environmental_signature(
        self,
        audio_features: Dict[str, float],
        speaker_name: str
    ) -> Tuple[bool, float, Dict[str, Any]]:
        """
        Analyze environmental audio signature for anomalies.

        Legitimate users typically have consistent environmental patterns.
        Attackers often have different acoustic environments.

        Args:
            audio_features: Environmental audio characteristics
            speaker_name: The claimed speaker

        Returns:
            Tuple of (is_anomalous, confidence, analysis)
        """
        analysis = {
            "known_environments": [],
            "current_environment": {},
            "anomaly_score": 0.0,
            "indicators": []
        }

        # Extract environmental characteristics
        reverb_time = audio_features.get('reverb_time', 0.3)
        noise_floor = audio_features.get('noise_floor_db', -50)
        room_impulse = audio_features.get('room_impulse_signature', None)

        analysis["current_environment"] = {
            "reverb_time": reverb_time,
            "noise_floor": noise_floor
        }

        # Check against known environments for this speaker
        if self._initialized:
            try:
                env_patterns = await self.find_similar_patterns(
                    np.zeros(192, dtype=np.float32),  # Dummy embedding for metadata search
                    speaker_name,
                    pattern_type="environment",
                    top_k=5
                )

                if env_patterns:
                    analysis["known_environments"] = [
                        p.get("metadata", {}).get("environment_name", "unknown")
                        for p in env_patterns
                    ]

                    # Compare current environment to known ones
                    # This is simplified - in production would use proper acoustic matching
                    known_reverbs = [
                        p.get("metadata", {}).get("reverb_time", 0.3)
                        for p in env_patterns
                    ]

                    if known_reverbs:
                        reverb_deviation = min(
                            abs(reverb_time - kr) for kr in known_reverbs
                        )
                        if reverb_deviation > 0.3:  # Significant difference
                            analysis["indicators"].append("unknown_acoustic_environment")
                            analysis["anomaly_score"] += 0.30

            except Exception as e:
                self.logger.debug(f"Environment analysis failed: {e}")

        # Check for recording artifacts (suggestive of replayed audio)
        if noise_floor < -70:  # Unusually quiet - might be clean recording
            analysis["indicators"].append("suspiciously_clean_audio")
            analysis["anomaly_score"] += 0.20

        if noise_floor > -20:  # Very noisy - might be phone playback
            analysis["indicators"].append("high_background_noise")
            analysis["anomaly_score"] += 0.15

        is_anomalous = analysis["anomaly_score"] > 0.40

        return is_anomalous, analysis["anomaly_score"], analysis

    async def comprehensive_anti_spoofing_check(
        self,
        audio_data: bytes,
        embedding: np.ndarray,
        audio_features: Dict[str, float],
        speaker_name: str,
        session_embeddings: Optional[List[np.ndarray]] = None
    ) -> Dict[str, Any]:
        """
        Perform comprehensive anti-spoofing analysis combining all detection methods.

        This is the main entry point for anti-spoofing that should be called
        during authentication.

        Args:
            audio_data: Raw audio bytes
            embedding: Speaker embedding
            audio_features: Extracted audio features
            speaker_name: Claimed speaker
            session_embeddings: Optional list of embeddings from current session

        Returns:
            Comprehensive anti-spoofing analysis result
        """
        import hashlib

        result = {
            "timestamp": datetime.utcnow().isoformat(),
            "speaker_name": speaker_name,
            "is_spoofed": False,
            "overall_confidence": 0.0,
            "threat_type": ThreatType.NONE.value,
            "checks": {},
            "recommendations": []
        }

        threat_scores = []

        # 1. Replay attack detection
        audio_fingerprint = hashlib.sha256(audio_data).hexdigest()
        is_replay, replay_confidence = await self.detect_replay_attack(
            audio_fingerprint, speaker_name
        )
        result["checks"]["replay_attack"] = {
            "detected": is_replay,
            "confidence": replay_confidence
        }
        if is_replay:
            threat_scores.append(("replay", replay_confidence))
            result["threat_type"] = ThreatType.REPLAY_ATTACK.value

        # 2. Synthesis detection
        is_synthetic, synth_confidence, synth_indicators = await self.detect_synthesis_attack(
            audio_features, speaker_name
        )
        result["checks"]["synthesis_attack"] = {
            "detected": is_synthetic,
            "confidence": synth_confidence,
            "indicators": synth_indicators
        }
        if is_synthetic:
            threat_scores.append(("synthesis", synth_confidence))
            if result["threat_type"] == ThreatType.NONE.value:
                result["threat_type"] = ThreatType.SYNTHETIC_VOICE.value

        # 3. Voice conversion detection
        is_vc, vc_confidence, vc_analysis = await self.detect_voice_conversion_attack(
            embedding, speaker_name, session_embeddings
        )
        result["checks"]["voice_conversion"] = {
            "detected": is_vc,
            "confidence": vc_confidence,
            "analysis": vc_analysis
        }
        if is_vc:
            threat_scores.append(("voice_conversion", vc_confidence))
            if result["threat_type"] == ThreatType.NONE.value:
                result["threat_type"] = ThreatType.VOICE_CLONING.value

        # 4. Environmental analysis
        is_env_anomaly, env_confidence, env_analysis = await self.analyze_environmental_signature(
            audio_features, speaker_name
        )
        result["checks"]["environmental_anomaly"] = {
            "detected": is_env_anomaly,
            "confidence": env_confidence,
            "analysis": env_analysis
        }
        if is_env_anomaly:
            threat_scores.append(("environmental", env_confidence))
            if result["threat_type"] == ThreatType.NONE.value:
                result["threat_type"] = ThreatType.ENVIRONMENTAL_ANOMALY.value

        # Calculate overall spoofing score
        if threat_scores:
            # Use max threat as primary, but boost if multiple threats detected
            max_threat = max(threat_scores, key=lambda x: x[1])
            base_score = max_threat[1]

            # Boost for multiple detections (correlated threats = higher confidence)
            if len(threat_scores) > 1:
                base_score = min(1.0, base_score * (1 + 0.1 * (len(threat_scores) - 1)))

            result["overall_confidence"] = base_score
            result["is_spoofed"] = base_score > 0.50

        # Generate recommendations
        if result["is_spoofed"]:
            result["recommendations"].append("DENY authentication - spoofing detected")
            result["recommendations"].append(f"Threat type: {result['threat_type']}")

            if is_replay:
                result["recommendations"].append("This exact audio was used before")
            if is_synthetic:
                result["recommendations"].append("Voice appears artificially generated")
            if is_vc:
                result["recommendations"].append("Voice conversion artifacts detected")
        else:
            if any(s[1] > 0.30 for s in threat_scores):
                result["recommendations"].append("Minor anomalies detected - monitor closely")

        # Store this audio fingerprint for future replay detection (if legitimate)
        if not result["is_spoofed"] and self._initialized:
            await self.store_audio_fingerprint(speaker_name, audio_fingerprint, embedding)

        return result


# ============================================================================
# Authentication Audit Trail (Langfuse)
# ============================================================================

class AuthenticationAuditTrail:
    """
    Langfuse-based audit trail for authentication attempts.

    Provides complete transparency into authentication decisions.

    Features:
    - Session management for tracking authentication sessions
    - Detailed trace recording with phases and metrics
    - Cost estimation for voice processing
    - Local file backup for reliability
    """

    def __init__(self):
        self.logger = logging.getLogger(f"{__name__}.AuditTrail")
        self._langfuse = None
        self._initialized = False
        self._trace_cache: Dict[str, AuthenticationTrace] = {}
        self._session_cache: Dict[str, Dict[str, Any]] = {}  # Session management
        self._current_session_id: Optional[str] = None

    async def initialize(self):
        """Initialize Langfuse client (v3.x SDK)."""
        if not LANGFUSE_AVAILABLE:
            self.logger.info("Langfuse not available - using local audit trail")
            self._initialized = True  # Use local storage fallback
            return

        try:
            # Use get_client() for v3.x SDK
            self._langfuse = get_client() if get_client else Langfuse()
            # Verify connection
            if hasattr(self._langfuse, 'auth_check'):
                self._langfuse.auth_check()
            self._initialized = True
            self.logger.info("✅ Langfuse audit trail initialized (v3.x SDK)")
        except Exception as e:
            self.logger.warning(f"Langfuse initialization failed, using local: {e}")
            self._initialized = True  # Fallback to local

    def start_session(self, user_id: str, device: str = "mac") -> str:
        """
        Start a new authentication session.

        A session groups multiple authentication attempts (e.g., retries)
        for a single unlock request.

        Args:
            user_id: The user attempting authentication
            device: The device being unlocked

        Returns:
            Session ID for tracking
        """
        session_id = f"session_{uuid4().hex[:12]}"
        self._current_session_id = session_id

        session_data = {
            "session_id": session_id,
            "user_id": user_id,
            "device": device,
            "started_at": datetime.utcnow().isoformat(),
            "traces": [],
            "total_attempts": 0,
            "final_outcome": None
        }
        self._session_cache[session_id] = session_data

        self.logger.info(f"🔐 Started authentication session: {session_id}")
        return session_id

    def end_session(self, session_id: str, outcome: str) -> Dict[str, Any]:
        """
        End an authentication session.

        Args:
            session_id: The session to end
            outcome: 'authenticated', 'denied', 'timeout', 'cancelled'

        Returns:
            Session summary
        """
        if session_id not in self._session_cache:
            return {}

        session = self._session_cache[session_id]
        session["ended_at"] = datetime.utcnow().isoformat()
        session["final_outcome"] = outcome
        session["duration_ms"] = (
            datetime.fromisoformat(session["ended_at"]) -
            datetime.fromisoformat(session["started_at"])
        ).total_seconds() * 1000

        # Log session summary to Langfuse
        if self._langfuse:
            try:
                # v3.x SDK: Create a session summary span
                session_span = self._langfuse.start_span(name="authentication_session")
                session_span.update(
                    metadata={
                        "session_id": session_id,
                        "user_id": session["user_id"],
                        "device": session["device"],
                        "total_attempts": session["total_attempts"],
                        "final_outcome": outcome,
                        "duration_ms": session["duration_ms"]
                    },
                    output={"outcome": outcome, "attempts": session["total_attempts"]}
                )
                session_span.end()
                self._langfuse.flush()
            except Exception as e:
                self.logger.debug(f"Langfuse session log failed: {e}")

        self.logger.info(f"🔐 Ended session {session_id}: {outcome} after {session['total_attempts']} attempts")

        if self._current_session_id == session_id:
            self._current_session_id = None

        return session

    def get_current_session(self) -> Optional[str]:
        """Get the current active session ID."""
        return self._current_session_id

    def start_trace(self, speaker_name: str, environment: str = "default", session_id: Optional[str] = None) -> str:
        """Start a new authentication trace."""
        trace_id = f"auth_{uuid4().hex[:16]}"
        trace = AuthenticationTrace(
            trace_id=trace_id,
            speaker_name=speaker_name,
            timestamp=datetime.utcnow(),
            environment=environment
        )
        self._trace_cache[trace_id] = trace

        # Link to session if available
        session_id = session_id or self._current_session_id
        if session_id and session_id in self._session_cache:
            self._session_cache[session_id]["traces"].append(trace_id)
            self._session_cache[session_id]["total_attempts"] += 1

        if self._langfuse:
            try:
                # v3.x SDK: Use start_span to create a trace-level span
                trace_span = self._langfuse.start_span(name="voice_authentication")
                trace_span.update(
                    metadata={
                        "trace_id": trace_id,
                        "environment": environment,
                        "speaker_name": speaker_name,
                        "session_id": session_id,
                        "attempt_number": self._session_cache.get(session_id, {}).get("total_attempts", 1)
                    }
                )
                # Store the span for later updates
                trace._langfuse_span = trace_span
            except Exception as e:
                self.logger.debug(f"Langfuse trace failed: {e}")

        return trace_id

    def log_phase(
        self,
        trace_id: str,
        phase: AuthenticationPhase,
        duration_ms: float,
        metrics: Dict[str, Any],
        success: bool = True
    ):
        """Log an authentication phase."""
        if trace_id not in self._trace_cache:
            return

        trace = self._trace_cache[trace_id]
        phase_data = {
            "phase": phase.value,
            "duration_ms": duration_ms,
            "success": success,
            "metrics": metrics,
            "timestamp": datetime.utcnow().isoformat()
        }
        trace.phases.append(phase_data)

        if self._langfuse and hasattr(trace, '_langfuse_span'):
            try:
                # v3.x SDK: Create nested span under the trace span
                parent_span = trace._langfuse_span
                phase_span = parent_span.start_span(name=phase.value)
                phase_span.update(
                    metadata=metrics,
                    output={"success": success, "duration_ms": duration_ms}
                )
                phase_span.end()
            except Exception as e:
                self.logger.debug(f"Langfuse phase log failed: {e}")

    def log_reasoning_step(
        self,
        trace_id: str,
        step_name: str,
        input_data: Dict[str, Any],
        output_data: Dict[str, Any],
        reasoning: str,
        duration_ms: float = 0.0
    ):
        """
        Log a LangGraph reasoning step for observability.

        This allows tracking the adaptive authentication decision-making process.

        Args:
            trace_id: The trace this step belongs to
            step_name: Name of the reasoning step (e.g., 'analyze_audio', 'check_confidence')
            input_data: Input to this step
            output_data: Output from this step
            reasoning: Human-readable explanation of the reasoning
            duration_ms: Time taken for this step
        """
        trace = self._trace_cache.get(trace_id)
        if self._langfuse and trace and hasattr(trace, '_langfuse_span'):
            try:
                # v3.x SDK: Create a generation span for reasoning tracking
                parent_span = trace._langfuse_span
                reasoning_span = parent_span.start_span(name=f"reasoning_{step_name}")
                reasoning_span.update(
                    input=json.dumps(input_data, default=str),
                    output=json.dumps(output_data, default=str),
                    metadata={
                        "reasoning": reasoning,
                        "duration_ms": duration_ms,
                        "step_type": "langgraph_node",
                        "model": "langgraph_adaptive_auth"
                    }
                )
                reasoning_span.end()
            except Exception as e:
                self.logger.debug(f"Langfuse reasoning log failed: {e}")

    def complete_trace(
        self,
        trace_id: str,
        decision: str,
        confidence: float,
        threat: ThreatType = ThreatType.NONE,
        additional_metrics: Optional[Dict[str, Any]] = None
    ) -> Optional[AuthenticationTrace]:
        """Complete and finalize an authentication trace."""
        if trace_id not in self._trace_cache:
            return None

        trace = self._trace_cache[trace_id]
        trace.decision = decision
        trace.fused_confidence = confidence
        trace.threat_detected = threat
        trace.total_duration_ms = sum(p.get("duration_ms", 0) for p in trace.phases)

        # Estimate API cost
        trace.api_cost_usd = self._estimate_cost(trace)

        if self._langfuse and hasattr(trace, '_langfuse_span'):
            try:
                # v3.x SDK: Update the trace span with final results and end it
                trace_span = trace._langfuse_span
                trace_span.update(
                    output={
                        "decision": decision,
                        "confidence": confidence,
                        "threat_detected": threat.value,
                        "total_duration_ms": trace.total_duration_ms,
                        "api_cost_usd": trace.api_cost_usd
                    },
                    metadata={
                        "authenticated": decision == "authenticated",
                        **(additional_metrics or {})
                    }
                )
                trace_span.end()

                # Flush to ensure trace data is sent
                self._langfuse.flush()

            except Exception as e:
                self.logger.debug(f"Langfuse completion failed: {e}")

        # Log to local file as backup
        self._log_to_file(trace)

        return trace

    def _estimate_cost(self, trace: AuthenticationTrace) -> float:
        """Estimate API cost for the authentication."""
        # Base cost estimation
        cost = 0.0

        for phase in trace.phases:
            phase_name = phase.get("phase", "")
            if "embedding" in phase_name:
                cost += 0.002  # Embedding extraction
            elif "verification" in phase_name:
                cost += 0.001  # Verification

        return cost

    def _log_to_file(self, trace: AuthenticationTrace):
        """Log trace to local file for backup."""
        try:
            import os
            log_dir = "/tmp/jarvis_auth_logs"
            os.makedirs(log_dir, exist_ok=True)

            log_file = f"{log_dir}/auth_{trace.timestamp.strftime('%Y%m%d')}.jsonl"
            with open(log_file, "a") as f:
                f.write(json.dumps(trace.to_dict()) + "\n")
        except Exception as e:
            self.logger.debug(f"Local log failed: {e}")

    def get_trace(self, trace_id: str) -> Optional[AuthenticationTrace]:
        """Get a trace by ID."""
        return self._trace_cache.get(trace_id)

    def get_recent_traces(
        self,
        speaker_name: Optional[str] = None,
        limit: int = 20
    ) -> List[AuthenticationTrace]:
        """Get recent authentication traces."""
        traces = list(self._trace_cache.values())

        if speaker_name:
            traces = [t for t in traces if t.speaker_name == speaker_name]

        traces.sort(key=lambda t: t.timestamp, reverse=True)
        return traces[:limit]

    def shutdown(self):
        """Gracefully shutdown Langfuse client and flush pending data."""
        if self._langfuse:
            try:
                self.logger.info("🔄 Flushing Langfuse audit trail...")
                # Flush any pending data with a short timeout
                self._langfuse.flush()
                # Shutdown the client if method exists (v3.x SDK)
                if hasattr(self._langfuse, 'shutdown'):
                    self._langfuse.shutdown()
                self.logger.info("✅ Langfuse audit trail shutdown complete")
            except Exception as e:
                self.logger.warning(f"⚠️ Langfuse shutdown error: {e}")
            finally:
                self._langfuse = None
                self._initialized = False

    def __del__(self):
        """Ensure cleanup on garbage collection."""
        self.shutdown()


# ============================================================================
# Voice Processing Cache (Helicone-style)
# ============================================================================

class VoiceProcessingCache:
    """
    Intelligent caching for voice processing to reduce costs.

    Caches:
    - Recent voice embeddings
    - Verification results
    - Audio quality analyses
    """

    def __init__(self, max_size: int = 100, ttl_seconds: int = 300):
        self.logger = logging.getLogger(f"{__name__}.VoiceCache")
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._max_size = max_size
        self._ttl_seconds = ttl_seconds
        self._stats = {
            "hits": 0,
            "misses": 0,
            "cost_saved_usd": 0.0
        }

    def _generate_key(self, audio_data: bytes, operation: str) -> str:
        """Generate cache key from audio fingerprint."""
        audio_hash = hashlib.sha256(audio_data[:8000]).hexdigest()[:32]
        return f"{operation}:{audio_hash}"

    def get(self, audio_data: bytes, operation: str) -> Optional[Dict[str, Any]]:
        """Get cached result if available and fresh."""
        key = self._generate_key(audio_data, operation)

        if key in self._cache:
            entry = self._cache[key]
            age = (datetime.utcnow() - entry["timestamp"]).total_seconds()

            if age < self._ttl_seconds:
                self._stats["hits"] += 1
                self._stats["cost_saved_usd"] += entry.get("estimated_cost", 0.002)
                self.logger.debug(f"Cache hit for {operation} (age: {age:.1f}s)")
                return entry["result"]
            else:
                del self._cache[key]

        self._stats["misses"] += 1
        return None

    def set(
        self,
        audio_data: bytes,
        operation: str,
        result: Dict[str, Any],
        estimated_cost: float = 0.002
    ):
        """Cache a processing result."""
        key = self._generate_key(audio_data, operation)

        # Evict oldest if at capacity
        if len(self._cache) >= self._max_size:
            oldest_key = min(self._cache.keys(), key=lambda k: self._cache[k]["timestamp"])
            del self._cache[oldest_key]

        self._cache[key] = {
            "result": result,
            "timestamp": datetime.utcnow(),
            "estimated_cost": estimated_cost
        }

    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        total = self._stats["hits"] + self._stats["misses"]
        hit_rate = self._stats["hits"] / total * 100 if total > 0 else 0

        return {
            "hits": self._stats["hits"],
            "misses": self._stats["misses"],
            "hit_rate_percent": hit_rate,
            "cost_saved_usd": self._stats["cost_saved_usd"],
            "cache_size": len(self._cache)
        }

    def clear(self):
        """Clear the cache."""
        self._cache.clear()


# ============================================================================
# Voice Feedback Generator
# ============================================================================

class VoiceFeedbackGenerator:
    """
    Generates natural, conversational feedback during authentication.

    Makes authentication feel like talking to a trusted security professional.
    """

    def __init__(self, user_name: str = "Derek"):
        self.user_name = user_name
        self._feedback_templates = {
            ConfidenceLevel.EXCELLENT: [
                f"Of course, {user_name}. Unlocking for you.",
                f"Welcome back, {user_name}.",
                f"Good to hear you, {user_name}. Unlocking now."
            ],
            ConfidenceLevel.GOOD: [
                f"Good morning, {user_name}. Unlocking now.",
                f"Verified. Unlocking for you, {user_name}."
            ],
            ConfidenceLevel.BORDERLINE: [
                f"One moment... yes, verified. Unlocking for you, {user_name}.",
                f"I can confirm it's you, {user_name}. Unlocking now."
            ],
            ConfidenceLevel.LOW: [
                "I'm having a little trouble hearing you clearly. Let me try again...",
                "Your voice sounds a bit different today. Let me adjust...",
                "Give me a second - filtering out background noise..."
            ],
            ConfidenceLevel.FAILED: [
                "I'm not able to verify your voice right now. Want to try again, or use manual authentication?",
                "Voice verification didn't match. Would you like to try speaking closer to the microphone?",
                "I couldn't confirm your identity. Try speaking more clearly, or use an alternative method."
            ]
        }

        self._environmental_feedback = {
            "noisy": "Give me a second - filtering out background noise... Got it - verified despite the noise.",
            "quiet_late": "Up late again? Unlocking quietly for you.",
            "sick_voice": "Your voice sounds different - hope you're feeling okay. I can still verify it's you.",
            "new_location": "First time unlocking from this location. Let me recalibrate... Got it!"
        }

    def get_confidence_level(self, confidence: float) -> ConfidenceLevel:
        """Map confidence score to human-readable level."""
        if confidence >= 0.90:
            return ConfidenceLevel.EXCELLENT
        elif confidence >= 0.85:
            return ConfidenceLevel.GOOD
        elif confidence >= 0.80:
            return ConfidenceLevel.BORDERLINE
        elif confidence >= 0.75:
            return ConfidenceLevel.LOW
        else:
            return ConfidenceLevel.FAILED

    def generate_feedback(
        self,
        confidence: float,
        context: Optional[Dict[str, Any]] = None
    ) -> VoiceFeedback:
        """Generate appropriate voice feedback based on confidence and context."""
        import random

        level = self.get_confidence_level(confidence)
        templates = self._feedback_templates[level]
        message = random.choice(templates)

        suggestion = None
        if level == ConfidenceLevel.LOW:
            suggestion = "Try speaking a bit louder and closer to the microphone."
        elif level == ConfidenceLevel.FAILED:
            suggestion = "You can also unlock using your password or Face ID."

        # Add environmental context
        if context:
            if context.get("snr_db", 20) < 12:
                message = self._environmental_feedback["noisy"]
            elif context.get("hour", 12) >= 23 or context.get("hour", 12) <= 5:
                message = self._environmental_feedback["quiet_late"]
            elif context.get("voice_changed"):
                message = self._environmental_feedback["sick_voice"]
            elif context.get("new_location"):
                message = self._environmental_feedback["new_location"]

        return VoiceFeedback(
            confidence_level=level,
            message=message,
            suggestion=suggestion,
            is_final=(level != ConfidenceLevel.LOW),
            speak_aloud=True
        )

    def generate_security_alert(
        self,
        threat: ThreatType,
        details: Optional[Dict[str, Any]] = None
    ) -> VoiceFeedback:
        """Generate security alert feedback."""
        messages = {
            ThreatType.REPLAY_ATTACK: "Security alert: I detected characteristics consistent with a recording playback. Access denied.",
            ThreatType.VOICE_CLONING: "Security alert: Voice pattern anomaly detected. This doesn't sound like natural speech.",
            ThreatType.SYNTHETIC_VOICE: "Security alert: Synthetic voice detected. Access denied for security reasons.",
            ThreatType.UNKNOWN_SPEAKER: f"I don't recognize this voice. This Mac is voice-locked to {self.user_name} only.",
            ThreatType.ENVIRONMENTAL_ANOMALY: "Something seems off about this authentication attempt. Please try again."
        }

        return VoiceFeedback(
            confidence_level=ConfidenceLevel.FAILED,
            message=messages.get(threat, "Security concern detected. Access denied."),
            suggestion="If you're the owner, please try again with a live voice command.",
            is_final=True,
            speak_aloud=True
        )


# ============================================================================
# LangChain Tools for Multi-Factor Authentication
# ============================================================================

try:
    from langchain.tools import BaseTool
    from langchain.agents import AgentExecutor
    from langchain_core.runnables import RunnablePassthrough
    LANGCHAIN_AVAILABLE = True
except ImportError:
    LANGCHAIN_AVAILABLE = False
    logger.info("LangChain not available - using direct orchestration")


class VoiceAnalysisHypothesis(str, Enum):
    """Hypotheses for why voice verification might fail."""
    WRONG_PERSON = "wrong_person"
    AUDIO_EQUIPMENT_CHANGE = "audio_equipment_change"
    ENVIRONMENTAL_NOISE = "environmental_noise"
    VOICE_ILLNESS = "voice_illness"
    VOICE_STRESS = "voice_stress"
    VOICE_FATIGUE = "voice_fatigue"
    MICROPHONE_DISTANCE = "microphone_distance"
    RECORDING_QUALITY = "recording_quality"


@dataclass
class VoiceAnalysisResult:
    """Result of advanced voice analysis."""
    fundamental_frequency_hz: float = 0.0
    frequency_deviation_percent: float = 0.0  # Deviation from baseline
    speech_rate_wpm: float = 0.0
    speech_rate_deviation_percent: float = 0.0
    voice_quality_score: float = 0.0  # Roughness, breathiness indicators
    snr_db: float = 0.0
    microphone_signature: str = "unknown"
    detected_anomalies: List[str] = field(default_factory=list)
    illness_indicators: List[str] = field(default_factory=list)
    hypothesis: Optional[VoiceAnalysisHypothesis] = None
    hypothesis_confidence: float = 0.0


@dataclass
class ChallengeQuestion:
    """Challenge question for borderline authentication."""
    question: str
    expected_answer: str
    answer_type: str  # 'exact', 'contains', 'semantic'
    difficulty: str  # 'easy', 'medium', 'hard'
    context_source: str  # 'git_history', 'calendar', 'project', 'personal'


# ============================================================================
# Multi-Factor Authentication Fusion Engine (Enhanced)
# ============================================================================

class MultiFactorAuthFusionEngine:
    """
    Advanced multi-factor authentication fusion with LangChain orchestration.

    Factors:
    - Voice biometric (primary)
    - Behavioral patterns (time, location, usage patterns)
    - Contextual intelligence (device state, recent activity)
    - Device proximity (Apple Watch, Bluetooth)
    - Historical patterns (success rate, typical confidence)

    Enhanced Features:
    - Sick voice detection with acoustic analysis
    - Microphone adaptation and signature learning
    - Challenge question generation for borderline cases
    - Graceful degradation chain
    - Hypothesis generation for failures
    """

    def __init__(self):
        self.logger = logging.getLogger(f"{__name__}.MultiFactor")
        self._executor = get_verification_executor()

        # Factor weights (dynamically adjusted based on reliability)
        self.base_weights = {
            "voice": 0.50,       # Primary voice biometric
            "behavioral": 0.20,  # Speaking patterns, timing
            "context": 0.15,     # Location, device, time
            "proximity": 0.10,   # Apple Watch, Bluetooth devices
            "history": 0.05      # Past verification history
        }
        self.weights = self.base_weights.copy()

        # Minimum thresholds per factor (adaptive)
        self.factor_thresholds = {
            "voice": 0.60,        # Voice must be at least 60% alone
            "overall": 0.80,      # Combined must reach 80%
            "voice_with_context": 0.55,  # Lower voice threshold when context is strong
            "challenge_trigger": 0.70    # Trigger challenge question below this
        }

        # Microphone signatures (learned over time)
        self.known_microphones: Dict[str, Dict[str, Any]] = {}
        self.current_microphone: Optional[str] = None

        # Voice baseline for illness/stress detection
        self.voice_baselines: Dict[str, Dict[str, float]] = {}

        # Challenge questions database
        self.challenge_questions: List[ChallengeQuestion] = []
        self._init_challenge_questions()

        # Graceful degradation chain
        self.degradation_chain = [
            ("primary", self._primary_voice_auth, 0.85),
            ("voice_behavioral_fusion", self._voice_behavioral_fusion, 0.80),
            ("challenge_question", self._challenge_question_auth, 0.75),
            ("proximity_boost", self._proximity_boost_auth, 0.70),
            ("manual_fallback", self._manual_fallback, 0.0)
        ]

        # Hypothesis engine for failures
        self.failure_hypotheses: Dict[str, List[VoiceAnalysisHypothesis]] = {}

    def _init_challenge_questions(self):
        """Initialize dynamic challenge questions."""
        # These are templates - actual answers are fetched dynamically
        self.challenge_questions = [
            ChallengeQuestion(
                question="What was the last project you worked on?",
                expected_answer="",  # Fetched from git/activity
                answer_type="semantic",
                difficulty="easy",
                context_source="git_history"
            ),
            ChallengeQuestion(
                question="What GCP project ID are you using?",
                expected_answer="jarvis-473803",
                answer_type="exact",
                difficulty="medium",
                context_source="project"
            ),
            ChallengeQuestion(
                question="What time did you last commit code?",
                expected_answer="",  # Fetched from git
                answer_type="contains",
                difficulty="medium",
                context_source="git_history"
            )
        ]

    async def _run_in_executor(self, func, *args, **kwargs) -> Any:
        """Run a CPU-intensive function in the thread pool."""
        loop = asyncio.get_running_loop()
        if kwargs:
            func = partial(func, **kwargs)
        return await loop.run_in_executor(self._executor, func, *args)

    async def analyze_voice_for_illness(
        self,
        audio_data: bytes,
        speaker_name: str,
        baseline: Optional[Dict[str, float]] = None
    ) -> VoiceAnalysisResult:
        """
        Analyze voice for signs of illness, stress, or fatigue.

        Detects:
        - Fundamental frequency shifts (hoarseness)
        - Voice quality changes (roughness, breathiness)
        - Speech rate changes (fatigue, illness)
        - Formant shifts (congestion)

        All CPU-intensive operations run in thread pool to avoid blocking.
        """
        result = VoiceAnalysisResult()

        try:
            # Convert audio to numpy array
            audio_array = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32) / 32768.0

            if len(audio_array) < 1600:  # Less than 0.1 second
                return result

            # Run all CPU-intensive feature extraction in parallel using thread pool
            f0_task = self._run_in_executor(self._estimate_f0_sync, audio_array)
            quality_task = self._run_in_executor(self._calculate_voice_quality_sync, audio_array)
            snr_task = self._run_in_executor(self._estimate_snr_sync, audio_array)

            # Wait for all results
            f0, quality, snr = await asyncio.gather(f0_task, quality_task, snr_task)

            result.fundamental_frequency_hz = f0
            result.voice_quality_score = quality
            result.snr_db = snr

            # Compare to baseline if available
            if baseline or speaker_name in self.voice_baselines:
                base = baseline or self.voice_baselines.get(speaker_name, {})

                if "f0" in base and base["f0"] > 0:
                    deviation = abs(result.fundamental_frequency_hz - base["f0"]) / base["f0"] * 100
                    result.frequency_deviation_percent = deviation

                    # Illness indicators
                    if deviation > 15:  # More than 15% deviation
                        if result.fundamental_frequency_hz < base["f0"]:
                            result.illness_indicators.append("lower_pitch_hoarseness")
                            result.detected_anomalies.append("voice_sounds_hoarse")
                        else:
                            result.illness_indicators.append("higher_pitch_congestion")
                            result.detected_anomalies.append("possible_nasal_congestion")

                    if result.voice_quality_score < base.get("quality", 0.7) - 0.15:
                        result.illness_indicators.append("rougher_voice_quality")
                        result.detected_anomalies.append("voice_quality_degraded")

            # Generate hypothesis
            if result.illness_indicators:
                result.hypothesis = VoiceAnalysisHypothesis.VOICE_ILLNESS
                result.hypothesis_confidence = min(0.9, len(result.illness_indicators) * 0.3)
            elif result.snr_db < 10:
                result.hypothesis = VoiceAnalysisHypothesis.ENVIRONMENTAL_NOISE
                result.hypothesis_confidence = 0.8

        except Exception as e:
            self.logger.debug(f"Voice analysis error: {e}")

        return result

    # =========================================================================
    # SYNC IMPLEMENTATIONS - Run in thread pool to avoid blocking event loop
    # =========================================================================

    def _estimate_f0_sync(self, audio: np.ndarray, sr: int = 16000) -> float:
        """Estimate fundamental frequency using autocorrelation (CPU-intensive)."""
        try:
            # Use autocorrelation for F0 estimation
            # Look for periodicity in typical voice range (75-400 Hz)
            min_period = int(sr / 400)  # 400 Hz
            max_period = int(sr / 75)   # 75 Hz

            # Compute autocorrelation
            corr = np.correlate(audio, audio, mode='full')
            corr = corr[len(corr)//2:]

            # Find peak in voice range
            search_range = corr[min_period:max_period]
            if len(search_range) > 0:
                peak_idx = np.argmax(search_range) + min_period
                f0 = sr / peak_idx
                return float(f0)
        except Exception:
            pass
        return 0.0

    def _calculate_voice_quality_sync(self, audio: np.ndarray) -> float:
        """Calculate voice quality score (CPU-intensive FFT)."""
        try:
            # Harmonic-to-noise ratio approximation
            # Higher HNR = clearer voice
            fft = np.fft.rfft(audio[:2048])
            magnitude = np.abs(fft)

            # Find harmonics (peaks) vs noise floor
            threshold = np.median(magnitude) * 2
            harmonics = magnitude[magnitude > threshold]
            noise = magnitude[magnitude <= threshold]

            if len(noise) > 0 and np.mean(noise) > 0:
                hnr = np.mean(harmonics) / np.mean(noise)
                # Normalize to 0-1 range
                quality = min(1.0, hnr / 20)
                return float(quality)
        except Exception:
            pass
        return 0.7  # Default moderate quality

    def _estimate_snr_sync(self, audio: np.ndarray) -> float:
        """Estimate signal-to-noise ratio in dB (CPU-intensive)."""
        try:
            # Assume first 10% is noise, rest is signal
            noise_samples = int(len(audio) * 0.1)
            noise = audio[:noise_samples]
            signal = audio[noise_samples:]

            noise_power = np.mean(noise ** 2) + 1e-10
            signal_power = np.mean(signal ** 2) + 1e-10

            snr = 10 * np.log10(signal_power / noise_power)
            return float(snr)
        except Exception:
            return 15.0  # Default moderate SNR

    def _detect_microphone_change_sync(
        self,
        audio_array: np.ndarray,
        speaker_name: str,
        known_microphones: Dict[str, Dict[str, Any]]
    ) -> Tuple[bool, str, float, Optional[Dict[str, Any]]]:
        """
        Sync implementation of microphone change detection (CPU-intensive FFT).

        Returns:
            Tuple of (is_different, signature, similarity, new_mic_data)
        """
        try:
            # Extract spectral characteristics for microphone fingerprinting
            fft = np.fft.rfft(audio_array[:4096])
            magnitude = np.abs(fft)

            # Low-frequency response (microphone characteristic)
            low_freq_response = np.mean(magnitude[:50])
            # High-frequency response
            high_freq_response = np.mean(magnitude[200:])
            # Overall spectral shape
            spectral_centroid = np.sum(np.arange(len(magnitude)) * magnitude) / (np.sum(magnitude) + 1e-10)

            # Create signature
            signature = f"lf{low_freq_response:.2f}_hf{high_freq_response:.2f}_sc{spectral_centroid:.0f}"

            # Compare to known microphones
            if speaker_name in known_microphones:
                known = known_microphones[speaker_name]
                best_match = None
                best_similarity = 0.0

                for mic_name, mic_data in known.items():
                    # Calculate similarity
                    diff = abs(mic_data.get("spectral_centroid", 0) - spectral_centroid)
                    similarity = max(0, 1 - diff / 500)  # Normalize difference

                    if similarity > best_similarity:
                        best_similarity = similarity
                        best_match = mic_name

                if best_similarity < 0.7:  # New microphone detected
                    return True, signature, 1 - best_similarity, None
                else:
                    return False, best_match or signature, best_similarity, None

            # First time - return new mic data to store
            new_mic_data = {
                "signature": signature,
                "spectral_centroid": spectral_centroid,
                "low_freq": low_freq_response,
                "high_freq": high_freq_response
            }
            return False, "default", 1.0, new_mic_data

        except Exception:
            return False, "unknown", 0.5, None

    async def detect_microphone_change(
        self,
        audio_data: bytes,
        speaker_name: str
    ) -> Tuple[bool, str, float]:
        """
        Detect if user is using a different microphone than usual.

        All CPU-intensive operations run in thread pool to avoid blocking.

        Returns:
            Tuple of (is_different, microphone_signature, confidence)
        """
        try:
            audio_array = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32) / 32768.0

            # Run CPU-intensive FFT in thread pool
            is_different, signature, similarity, new_mic_data = await self._run_in_executor(
                self._detect_microphone_change_sync,
                audio_array,
                speaker_name,
                self.known_microphones
            )

            # Store new microphone data if detected
            if new_mic_data is not None:
                self.known_microphones[speaker_name] = {"default": new_mic_data}

            return is_different, signature, similarity

        except Exception as e:
            self.logger.debug(f"Microphone detection error: {e}")
            return False, "unknown", 0.5

    async def generate_hypothesis(
        self,
        voice_confidence: float,
        audio_analysis: VoiceAnalysisResult,
        behavioral_confidence: float,
        context: Dict[str, Any]
    ) -> Tuple[VoiceAnalysisHypothesis, str, float]:
        """
        Generate hypothesis for why authentication might be failing.

        Returns intelligent retry suggestion based on analysis.
        """
        hypotheses: List[Tuple[VoiceAnalysisHypothesis, float, str]] = []

        # Audio quality issues
        if audio_analysis.snr_db < 12:
            hypotheses.append((
                VoiceAnalysisHypothesis.ENVIRONMENTAL_NOISE,
                0.85,
                "I'm having trouble hearing you clearly due to background noise. Could you speak closer to the microphone?"
            ))

        # Illness detection
        if audio_analysis.illness_indicators:
            hypotheses.append((
                VoiceAnalysisHypothesis.VOICE_ILLNESS,
                0.80,
                "Your voice sounds different today. Are you feeling alright? Your speech patterns still match, so I can use additional verification."
            ))

        # Microphone change
        if context.get("microphone_changed"):
            hypotheses.append((
                VoiceAnalysisHypothesis.AUDIO_EQUIPMENT_CHANGE,
                0.90,
                f"You're using a different microphone ({context.get('microphone_name', 'unknown')}). Let me recalibrate - say 'unlock my screen' one more time."
            ))

        # Low voice but good behavioral
        if voice_confidence < 0.70 and behavioral_confidence > 0.85:
            if not hypotheses:  # Only if no other hypothesis
                hypotheses.append((
                    VoiceAnalysisHypothesis.VOICE_FATIGUE,
                    0.70,
                    "Your voice is a bit different than usual, but your patterns match perfectly. Just checking - could you speak a bit clearer?"
                ))

        # Very low confidence - might be wrong person
        if voice_confidence < 0.40 and behavioral_confidence < 0.50:
            hypotheses.append((
                VoiceAnalysisHypothesis.WRONG_PERSON,
                0.60,
                "I'm having significant trouble verifying your voice. Are you using a different microphone or location?"
            ))

        # Return best hypothesis (sorted by confidence, return as hypothesis, message, confidence)
        if hypotheses:
            hypotheses.sort(key=lambda x: x[1], reverse=True)
            best = hypotheses[0]
            # Return in order: (hypothesis, message, confidence)
            return (best[0], best[2], best[1])

        return (VoiceAnalysisHypothesis.RECORDING_QUALITY,
                "Could you try speaking again? A clearer sample might help.",
                0.50)

    async def get_challenge_question(
        self,
        speaker_name: str,
        difficulty: str = "easy"
    ) -> Optional[ChallengeQuestion]:
        """Get a dynamic challenge question for verification."""
        import random

        # Filter by difficulty
        candidates = [q for q in self.challenge_questions if q.difficulty == difficulty]

        if not candidates:
            candidates = self.challenge_questions

        if not candidates:
            return None

        question = random.choice(candidates)

        # Try to populate dynamic answer
        if question.context_source == "git_history":
            try:
                import subprocess
                result = subprocess.run(
                    ["git", "log", "-1", "--format=%s", "--", "."],
                    capture_output=True, text=True, timeout=2
                )
                if result.returncode == 0:
                    question.expected_answer = result.stdout.strip()
            except Exception:
                pass

        return question

    async def verify_challenge_answer(
        self,
        question: ChallengeQuestion,
        answer: str
    ) -> Tuple[bool, float]:
        """Verify a challenge question answer."""
        if not answer or not question.expected_answer:
            return False, 0.0

        answer_lower = answer.lower().strip()
        expected_lower = question.expected_answer.lower().strip()

        if question.answer_type == "exact":
            match = answer_lower == expected_lower
            return match, 1.0 if match else 0.0

        elif question.answer_type == "contains":
            # Check if key parts are present
            key_words = expected_lower.split()
            matches = sum(1 for word in key_words if word in answer_lower)
            confidence = matches / len(key_words) if key_words else 0
            return confidence > 0.6, confidence

        elif question.answer_type == "semantic":
            # Simple semantic matching
            # In production, this would use embeddings
            common_words = set(answer_lower.split()) & set(expected_lower.split())
            all_words = set(answer_lower.split()) | set(expected_lower.split())
            confidence = len(common_words) / len(all_words) if all_words else 0
            return confidence > 0.3, confidence

        return False, 0.0

    async def _primary_voice_auth(self, factors: Dict[str, float]) -> Tuple[bool, float]:
        """Primary voice-only authentication."""
        voice = factors.get("voice", 0)
        return voice >= 0.85, voice

    async def _voice_behavioral_fusion(self, factors: Dict[str, float]) -> Tuple[bool, float]:
        """Voice + behavioral fusion."""
        voice = factors.get("voice", 0)
        behavioral = factors.get("behavioral", 0)

        # Lower voice threshold when behavioral is strong
        fused = voice * 0.6 + behavioral * 0.4

        # Allow lower voice if behavioral is very strong
        if voice >= 0.55 and behavioral >= 0.90:
            return True, fused

        return fused >= 0.80, fused

    async def _challenge_question_auth(self, factors: Dict[str, float]) -> Tuple[bool, float]:
        """Challenge question authentication (placeholder - actual impl in caller)."""
        return False, factors.get("voice", 0)

    async def _proximity_boost_auth(self, factors: Dict[str, float]) -> Tuple[bool, float]:
        """Proximity-boosted authentication."""
        voice = factors.get("voice", 0)
        proximity = factors.get("proximity", 0)

        if proximity >= 0.95:  # Very close proximity (Apple Watch)
            # Boost voice confidence
            boosted = min(1.0, voice * 1.3)
            return boosted >= 0.70, boosted

        return False, voice

    async def _manual_fallback(self, factors: Dict[str, float]) -> Tuple[bool, float]:
        """Manual fallback - signals need for password."""
        return False, 0.0

    async def fuse_factors(
        self,
        voice_confidence: float,
        behavioral_confidence: Optional[float] = None,
        context_confidence: Optional[float] = None,
        proximity_confidence: Optional[float] = None,
        history_confidence: Optional[float] = None,
        audio_analysis: Optional[VoiceAnalysisResult] = None,
        speaker_name: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Enhanced multi-factor fusion with adaptive weighting and intelligent decision making.

        Now includes:
        - Dynamic weight adjustment based on factor reliability
        - Graceful degradation through authentication chain
        - Hypothesis generation for failures
        - Challenge question triggering for borderline cases

        Returns:
            Dict with fused_confidence, decision, factor breakdown, and recommendations
        """
        factors = {
            "voice": voice_confidence
        }

        # Add available factors with intelligent defaults
        factors["behavioral"] = behavioral_confidence if behavioral_confidence is not None else voice_confidence * 0.9
        factors["context"] = context_confidence if context_confidence is not None else 0.95
        factors["proximity"] = proximity_confidence if proximity_confidence is not None else 0.90
        factors["history"] = history_confidence if history_confidence is not None else 0.85

        # Dynamic weight adjustment based on audio quality
        adjusted_weights = self.base_weights.copy()
        if audio_analysis:
            # If poor audio quality, reduce voice weight and increase behavioral
            if audio_analysis.snr_db < 10:
                adjusted_weights["voice"] = 0.40
                adjusted_weights["behavioral"] = 0.30
                self.logger.debug(f"Reduced voice weight due to low SNR ({audio_analysis.snr_db:.1f} dB)")

            # If illness detected, increase behavioral weight
            if audio_analysis.illness_indicators:
                adjusted_weights["voice"] = 0.35
                adjusted_weights["behavioral"] = 0.35
                self.logger.debug(f"Adjusted weights for illness indicators: {audio_analysis.illness_indicators}")

        # Calculate weighted fusion with adjusted weights
        fused_confidence = 0.0
        total_weight = 0.0

        for factor, confidence in factors.items():
            weight = adjusted_weights.get(factor, 0.1)
            fused_confidence += confidence * weight
            total_weight += weight

        if total_weight > 0:
            fused_confidence = fused_confidence / total_weight

        # Check thresholds with context-awareness
        voice_threshold = self.factor_thresholds["voice"]

        # Lower voice threshold if other factors are very strong
        if factors["behavioral"] > 0.90 and factors["context"] > 0.95:
            voice_threshold = self.factor_thresholds.get("voice_with_context", 0.55)

        voice_pass = factors["voice"] >= voice_threshold
        overall_pass = fused_confidence >= self.factor_thresholds["overall"]

        # Enhanced decision logic with graceful degradation
        decision = "denied"
        recommendation = None
        retry_strategy = None
        challenge_required = False

        if voice_pass and overall_pass:
            decision = "authenticated"
        elif voice_pass and fused_confidence >= 0.75:
            # Voice ok but fused not quite there - try with context boost
            decision = "authenticated"  # Allow with strong voice
        elif not voice_pass and factors["behavioral"] >= 0.90 and factors["context"] >= 0.90:
            # Voice low but behavioral and context excellent - challenge question
            decision = "requires_challenge"
            challenge_required = True
            recommendation = "Voice confidence low but patterns match. Asking verification question."
        elif fused_confidence >= self.factor_thresholds.get("challenge_trigger", 0.70):
            # Borderline case - try degradation chain
            decision = "requires_challenge"
            challenge_required = True
        elif factors["voice"] >= 0.50 and factors["proximity"] >= 0.95:
            # Low voice but device is very close - might be equipment issue
            decision = "retry_recommended"
            if audio_analysis:
                hypothesis, msg, _ = await self.generate_hypothesis(
                    voice_confidence, audio_analysis, factors["behavioral"], {}
                )
                retry_strategy = hypothesis.value
                recommendation = msg
        else:
            decision = "denied"
            # Generate helpful feedback
            if audio_analysis:
                hypothesis, msg, conf = await self.generate_hypothesis(
                    voice_confidence, audio_analysis, factors["behavioral"], {}
                )
                recommendation = msg

        return {
            "fused_confidence": fused_confidence,
            "decision": decision,
            "factors": factors,
            "weights_used": adjusted_weights,
            "voice_threshold_used": voice_threshold,
            "voice_threshold_met": voice_pass,
            "overall_threshold_met": overall_pass,
            "challenge_required": challenge_required,
            "recommendation": recommendation,
            "retry_strategy": retry_strategy,
            "audio_quality": {
                "snr_db": audio_analysis.snr_db if audio_analysis else None,
                "voice_quality": audio_analysis.voice_quality_score if audio_analysis else None,
                "illness_detected": bool(audio_analysis.illness_indicators) if audio_analysis else False
            } if audio_analysis else None
        }

    def calculate_behavioral_confidence(
        self,
        speaker_name: str,
        verification_history: List[Dict[str, Any]],
        current_time: datetime
    ) -> float:
        """Calculate behavioral confidence based on patterns."""
        if not verification_history:
            return 0.85  # Default for new users

        # Check time-of-day pattern
        current_hour = current_time.hour
        typical_hours = [h["timestamp"].hour for h in verification_history[-20:] if h.get("verified")]

        hour_match = current_hour in typical_hours or any(abs(current_hour - h) <= 2 for h in typical_hours)
        time_confidence = 0.95 if hour_match else 0.80

        # Check recent success rate
        recent = verification_history[-10:]
        success_rate = sum(1 for h in recent if h.get("verified")) / len(recent) if recent else 0.5

        # Combine
        behavioral_confidence = (time_confidence * 0.6) + (success_rate * 0.4)

        return min(1.0, behavioral_confidence)

    def calculate_context_confidence(
        self,
        current_environment: str,
        known_environments: List[str],
        last_activity_hours: float,
        failed_attempts_24h: int
    ) -> float:
        """Calculate contextual confidence."""
        confidence = 0.95  # Start high

        # Environment check
        if current_environment not in known_environments:
            confidence -= 0.10

        # Recent activity gap
        if last_activity_hours > 24:
            confidence -= 0.05

        # Failed attempts
        if failed_attempts_24h > 0:
            confidence -= min(0.20, failed_attempts_24h * 0.05)

        return max(0.50, confidence)


class SpeakerVerificationService:
    """
    Speaker verification service for JARVIS

    Verifies speaker identity using voice biometrics
    """

    def __init__(self, learning_db: Optional[JARVISLearningDatabase] = None):
        """
        Initialize speaker verification service

        Args:
            learning_db: LearningDatabase instance (optional, will create if not provided)
        """
        self.learning_db = learning_db
        self.speechbrain_engine = None
        self.initialized = False
        self.speaker_profiles = {}  # Cache of speaker profiles
        self.verification_threshold = 0.40  # 40% confidence for verification (matches owner-aware fusion threshold)
        self.legacy_threshold = 0.40  # 40% for legacy profiles with dimension mismatch
        self.profile_quality_scores = {}  # Track profile quality (1.0 = native, <1.0 = legacy)

        # Thread pool for CPU-intensive operations (non-blocking async)
        self._executor = get_verification_executor()
        self._preload_thread = None
        self._encoder_preloading = False
        self._encoder_preloaded = False
        self._shutdown_event = threading.Event()  # For clean thread shutdown
        self._preload_loop = None  # Track event loop for cleanup

        # Debug mode for detailed verification logging
        self.debug_mode = True  # Enable detailed verification debugging

        # Adaptive learning tracking
        self.verification_history = {}  # Track verification attempts per speaker
        self.learning_enabled = True
        self.min_samples_for_update = 3  # Minimum attempts before adapting threshold

        # Dynamic embedding dimension detection
        self.current_model_dimension = None  # Will be detected automatically
        self.supported_dimensions = [192, 768, 96]  # Common embedding dimensions
        self.enable_auto_migration = True  # Auto-migrate incompatible profiles

        # Hot reload configuration
        self.profile_version_cache = {}  # Track profile versions/timestamps for change detection
        self.auto_reload_enabled = True  # Enable automatic profile reloading
        self.reload_check_interval = 30  # Check for updates every 30 seconds
        self._reload_task = None  # Background task for checking updates

        # ENHANCED ADAPTIVE VERIFICATION SYSTEM
        # Dynamic confidence boosting
        self.confidence_boost_enabled = True
        self.boost_multiplier = 1.5  # Boost confidence for known good patterns
        self.min_confidence_for_boost = 0.15  # Minimum confidence before boost can apply

        # Multi-stage verification
        self.multi_stage_enabled = True
        self.stage_weights = {
            'primary': 0.6,    # Main embedding comparison
            'acoustic': 0.2,   # Acoustic features (pitch, energy)
            'temporal': 0.1,   # Temporal patterns
            'adaptive': 0.1    # Historical pattern matching
        }

        # Rolling average for adaptive embeddings
        self.rolling_embeddings = {}  # Store recent successful embeddings
        self.max_rolling_samples = 10  # Keep last 10 successful verifications
        self.rolling_weight = 0.3  # Weight for new samples in rolling average

        # Dynamic calibration mode
        self.calibration_mode = False
        self.calibration_samples = []
        self.calibration_threshold = 0.10  # Very low threshold during calibration
        self.auto_calibrate_on_failure = True  # Auto-enter calibration after repeated failures
        self.failure_count = {}  # Track consecutive failures per speaker
        self.max_failures_before_calibration = 3

        # Environmental adaptation
        self.environment_profiles = {}  # Store different environment signatures
        self.current_environment = 'default'
        self.adapt_to_environment = True

        # Confidence normalization
        self.normalize_confidence = True
        self.confidence_history_window = 20  # Use last 20 attempts for normalization
        self.confidence_stats = {}  # Store mean/std for normalization

        # CONTINUOUS LEARNING SYSTEM
        # Store all voice interactions for ML training
        self.continuous_learning_enabled = True
        self.store_all_audio = True  # Store audio samples in database
        self.min_audio_quality = 0.1  # Minimum quality to store
        self.max_stored_samples_per_day = 100  # Limit daily storage
        self.audio_storage_format = 'wav'  # Store as WAV files

        # ML-based continuous improvement
        self.ml_update_frequency = 10  # Update model every N samples
        self.incremental_learning = True  # Use incremental learning
        self.embedding_update_weight = 0.1  # Weight for new samples in embedding
        self.auto_retrain_threshold = 50  # Retrain after N new samples

        # Voice sample collection
        self.voice_sample_buffer = []  # Buffer for recent samples
        self.max_buffer_size = 20  # Keep last 20 samples in memory
        self.sample_metadata = {}  # Store metadata for each sample

        # ========================================================================
        # ENHANCED AUTHENTICATION COMPONENTS (v2.0)
        # ========================================================================

        # Voice Pattern Store (ChromaDB) for behavioral biometrics
        self.voice_pattern_store = VoicePatternStore()
        self._pattern_store_initialized = False

        # Authentication Audit Trail (Langfuse)
        self.audit_trail = AuthenticationAuditTrail()
        self._audit_trail_initialized = False

        # Voice Processing Cache (Helicone-style)
        self.processing_cache = VoiceProcessingCache(
            max_size=100,
            ttl_seconds=300  # 5 minute cache
        )

        # Voice Feedback Generator
        self.feedback_generator = VoiceFeedbackGenerator(user_name="Derek")

        # Multi-Factor Authentication Fusion
        self.multi_factor_fusion = MultiFactorAuthFusionEngine()

        # TTS callback for voice feedback (set externally)
        self.tts_callback: Optional[Callable[[str], Any]] = None

        # Enhanced security settings
        self.anti_spoofing_enabled = True
        self.replay_detection_enabled = True
        self.synthetic_voice_detection = True

        # Known environments for context
        self.known_environments = ["home", "office", "default"]

        # 🚀 UNIFIED VOICE CACHE: Fast-path for instant recognition (~1ms vs 200-500ms)
        self.unified_cache = None
        self._unified_cache_hits = 0
        self._unified_cache_misses = 0

    async def _run_in_executor(self, func, *args, **kwargs) -> Any:
        """Run a CPU-intensive function in the thread pool to avoid blocking."""
        loop = asyncio.get_running_loop()
        if kwargs:
            func = partial(func, **kwargs)
        return await loop.run_in_executor(self._executor, func, *args)

    async def initialize_fast(self):
        """
        Fast initialization with background encoder pre-loading.

        Loads profiles immediately, defers SpeechBrain loading to background.
        JARVIS starts fast (~2s), encoder ready in ~10s total.
        """
        if self.initialized:
            return

        logger.info("🔐 Initializing Speaker Verification Service (fast mode)...")

        # Initialize learning database if not provided - use singleton
        if self.learning_db is None:
            from intelligence.learning_database import get_learning_database
            self.learning_db = await get_learning_database()

        # Create SpeechBrain engine but DON'T initialize it yet (deferred to background)
        model_config = ModelConfig(
            name="speechbrain-wav2vec2",
            engine=STTEngine.SPEECHBRAIN,
            disk_size_mb=380,
            ram_required_gb=2.0,
            vram_required_gb=1.8,
            expected_accuracy=0.96,
            avg_latency_ms=150,
            supports_fine_tuning=True,
            model_path="speechbrain/asr-wav2vec2-commonvoice-en",
        )

        self.speechbrain_engine = SpeechBrainEngine(model_config)
        # DON'T call initialize() here - defer to background thread

        # Load speaker profiles from database
        await self._load_speaker_profiles()

        self.initialized = True
        logger.info(
            f"✅ Speaker Verification Service ready - {len(self.speaker_profiles)} profiles loaded (encoder loading in background)"
        )

        # Start background initialization of SpeechBrain engine
        logger.info("🔄 Loading SpeechBrain encoder in background thread...")
        self._start_background_preload()

        # Initialize enhanced components in background
        asyncio.create_task(self._initialize_enhanced_components())

        # Start background profile reload monitoring
        if self.auto_reload_enabled:
            logger.info(f"🔄 Starting profile auto-reload (check every {self.reload_check_interval}s)...")
            self._reload_task = asyncio.create_task(self._profile_reload_monitor())
            logger.info("✅ Profile hot reload enabled - updates will be detected automatically")

        # 🚀 UNIFIED CACHE: Connect for instant recognition fast-path
        try:
            from voice_unlock.unified_voice_cache_manager import get_unified_cache_manager

            self.unified_cache = get_unified_cache_manager()
            if self.unified_cache and self.unified_cache.is_ready:
                logger.info(f"✅ Unified voice cache connected ({self.unified_cache.profiles_loaded} profiles)")
            else:
                logger.debug("Unified voice cache not ready yet (will connect when available)")
        except ImportError:
            logger.debug("Unified voice cache module not available")
        except Exception as e:
            logger.debug(f"Unified voice cache connection failed (non-critical): {e}")

    def _start_background_preload(self):
        """Start background thread to initialize SpeechBrain engine and pre-load speaker encoder"""
        if self._encoder_preloading or self._encoder_preloaded:
            return

        self._encoder_preloading = True

        def preload_worker():
            """Worker function to initialize engine and pre-load encoder in background thread"""
            try:
                # Run async function in thread's event loop
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                self._preload_loop = loop  # Store for cleanup

                try:
                    # First initialize the SpeechBrain engine (loads models)
                    logger.info("🔄 Background: Initializing SpeechBrain engine...")
                    loop.run_until_complete(self.speechbrain_engine.initialize())
                    logger.info("✅ Background: SpeechBrain engine initialized")

                    # Then pre-load the speaker encoder
                    logger.info("🔄 Background: Pre-loading speaker encoder...")
                    loop.run_until_complete(self.speechbrain_engine._load_speaker_encoder())
                    self._encoder_preloaded = True
                    logger.info("✅ Speaker encoder ready - voice biometric unlock now instant!")
                finally:
                    # Clean shutdown of event loop
                    try:
                        # Cancel all pending tasks
                        pending = asyncio.all_tasks(loop)
                        for task in pending:
                            task.cancel()

                        # Wait for tasks to finish cancellation
                        if pending:
                            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))

                        # Close the loop
                        loop.close()
                    except Exception as cleanup_error:
                        logger.debug(f"Event loop cleanup: {cleanup_error}")
                    finally:
                        self._preload_loop = None

            except Exception as e:
                logger.error(f"Background encoder pre-loading failed: {e}", exc_info=True)
            finally:
                self._encoder_preloading = False

        self._preload_thread = threading.Thread(
            target=preload_worker,
            daemon=True,
            name="SpeakerEncoderPreloader"  # Give it a descriptive name
        )
        self._preload_thread.start()

    async def _initialize_enhanced_components(self):
        """
        Initialize enhanced authentication components (v2.0).

        Initializes:
        - ChromaDB voice pattern store
        - Langfuse audit trail
        - Multi-factor fusion engine
        """
        try:
            logger.info("🚀 Initializing enhanced authentication components...")

            # Initialize Voice Pattern Store (ChromaDB)
            if CHROMADB_AVAILABLE:
                await self.voice_pattern_store.initialize()
                self._pattern_store_initialized = True
                logger.info("✅ Voice pattern store (ChromaDB) initialized")

            # Initialize Audit Trail (Langfuse)
            await self.audit_trail.initialize()
            self._audit_trail_initialized = True
            logger.info("✅ Authentication audit trail initialized")

            # Update feedback generator with primary user name
            primary_user = None
            for name, profile in self.speaker_profiles.items():
                if profile.get("is_primary_user"):
                    primary_user = name
                    break

            if primary_user:
                self.feedback_generator.user_name = primary_user
                logger.info(f"✅ Voice feedback configured for {primary_user}")

            logger.info("🎉 Enhanced authentication components ready!")

        except Exception as e:
            logger.error(f"❌ Failed to initialize enhanced components: {e}", exc_info=True)

    async def verify_speaker_enhanced(
        self,
        audio_data: bytes,
        speaker_name: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Enhanced speaker verification with multi-factor fusion and audit trail.

        This is the v2.0 verification method that provides:
        - Full audit trail via Langfuse
        - Multi-factor authentication fusion
        - Anti-spoofing detection
        - Progressive voice feedback
        - Intelligent caching

        Args:
            audio_data: Audio bytes (WAV format)
            speaker_name: Expected speaker name (if None, identifies from all profiles)
            context: Additional context (environment, device, etc.)

        Returns:
            Enhanced verification result with full trace
        """
        if not self.initialized:
            await self.initialize()

        context = context or {}
        start_time = time.time()

        # Start audit trace
        trace_id = self.audit_trail.start_trace(
            speaker_name=speaker_name or "unknown",
            environment=context.get("environment", self.current_environment)
        )

        try:
            # Phase 1: Check cache for recent identical audio
            cache_key = "verification"
            cached_result = self.processing_cache.get(audio_data, cache_key)
            if cached_result:
                self.audit_trail.log_phase(
                    trace_id, AuthenticationPhase.SPEAKER_VERIFICATION,
                    duration_ms=0.1,
                    metrics={"cached": True, "confidence": cached_result.get("confidence", 0)}
                )
                logger.info("🔄 Using cached verification result")
                return cached_result

            # Phase 2: Audio quality analysis
            phase_start = time.time()
            audio_quality = await self._calculate_audio_quality(audio_data)
            snr_db = self._estimate_snr(np.frombuffer(audio_data[:min(4000, len(audio_data))], dtype=np.int16).astype(np.float32) / 32768.0)

            self.audit_trail.log_phase(
                trace_id, AuthenticationPhase.AUDIO_CAPTURE,
                duration_ms=(time.time() - phase_start) * 1000,
                metrics={"quality_score": audio_quality, "snr_db": snr_db}
            )

            # Phase 3: Anti-spoofing checks
            threat_detected = ThreatType.NONE
            if self.anti_spoofing_enabled and self._pattern_store_initialized:
                phase_start = time.time()

                # Generate audio fingerprint for replay detection
                audio_fingerprint = hashlib.sha256(audio_data).hexdigest()

                if self.replay_detection_enabled:
                    is_replay, anomaly_score = await self.voice_pattern_store.detect_replay_attack(
                        audio_fingerprint,
                        speaker_name or "unknown"
                    )
                    if is_replay:
                        threat_detected = ThreatType.REPLAY_ATTACK
                        logger.warning(f"⚠️ REPLAY ATTACK DETECTED for {speaker_name}")

                self.audit_trail.log_phase(
                    trace_id, AuthenticationPhase.ANTI_SPOOFING,
                    duration_ms=(time.time() - phase_start) * 1000,
                    metrics={"threat": threat_detected.value, "fingerprint": audio_fingerprint[:16]}
                )

                if threat_detected != ThreatType.NONE:
                    # Generate security alert feedback
                    feedback = self.feedback_generator.generate_security_alert(threat_detected)
                    if self.tts_callback:
                        await self._speak_feedback(feedback)

                    self.audit_trail.complete_trace(trace_id, "denied", 0.0, threat_detected)
                    return {
                        "verified": False,
                        "confidence": 0.0,
                        "speaker_name": speaker_name,
                        "threat_detected": threat_detected.value,
                        "feedback": feedback.message,
                        "trace_id": trace_id
                    }

            # Phase 4: Core speaker verification (existing logic)
            phase_start = time.time()
            base_result = await self.verify_speaker(audio_data, speaker_name)
            voice_confidence = base_result.get("confidence", 0.0)

            self.audit_trail.log_phase(
                trace_id, AuthenticationPhase.SPEAKER_VERIFICATION,
                duration_ms=(time.time() - phase_start) * 1000,
                metrics={"confidence": voice_confidence, "verified": base_result.get("verified", False)}
            )

            # Phase 5: Multi-factor fusion
            phase_start = time.time()

            # Calculate behavioral confidence
            behavioral_confidence = self.multi_factor_fusion.calculate_behavioral_confidence(
                speaker_name or "unknown",
                self.verification_history.get(speaker_name, []),
                datetime.now()
            )

            # Calculate context confidence
            last_activity_hours = 0
            if speaker_name in self.verification_history:
                history = self.verification_history[speaker_name]
                if history:
                    last_ts = datetime.fromisoformat(history[-1].get("timestamp", datetime.now().isoformat()))
                    last_activity_hours = (datetime.now() - last_ts).total_seconds() / 3600

            context_confidence = self.multi_factor_fusion.calculate_context_confidence(
                context.get("environment", self.current_environment),
                self.known_environments,
                last_activity_hours,
                self.failure_count.get(speaker_name, 0)
            )

            # Fuse all factors
            fusion_result = await self.multi_factor_fusion.fuse_factors(
                voice_confidence=voice_confidence,
                behavioral_confidence=behavioral_confidence,
                context_confidence=context_confidence,
                proximity_confidence=context.get("proximity_confidence"),
                history_confidence=None
            )

            self.audit_trail.log_phase(
                trace_id, AuthenticationPhase.MULTI_FACTOR_FUSION,
                duration_ms=(time.time() - phase_start) * 1000,
                metrics={
                    "fused_confidence": fusion_result["fused_confidence"],
                    "factors": fusion_result["factors"],
                    "decision": fusion_result["decision"]
                }
            )

            # Phase 6: Final decision
            final_confidence = fusion_result["fused_confidence"]
            is_verified = fusion_result["decision"] == "authenticated"

            # Generate voice feedback
            feedback_context = {
                "snr_db": snr_db,
                "hour": datetime.now().hour,
                "voice_changed": voice_confidence < 0.70 and behavioral_confidence > 0.85,
                "new_location": context.get("environment") not in self.known_environments
            }
            feedback = self.feedback_generator.generate_feedback(final_confidence, feedback_context)

            if self.tts_callback:
                await self._speak_feedback(feedback)

            # Store audio fingerprint for future replay detection
            if is_verified and self._pattern_store_initialized:
                audio_fingerprint = hashlib.sha256(audio_data).hexdigest()
                try:
                    embedding = await self._extract_speaker_embedding(audio_data)
                    await self.voice_pattern_store.store_audio_fingerprint(
                        speaker_name or "unknown",
                        audio_fingerprint,
                        embedding
                    )
                except Exception as e:
                    logger.debug(f"Failed to store fingerprint: {e}")

            # Complete trace
            decision = "authenticated" if is_verified else "denied"
            trace = self.audit_trail.complete_trace(trace_id, decision, final_confidence, threat_detected)

            # Build result
            result = {
                "verified": is_verified,
                "confidence": final_confidence,
                "voice_confidence": voice_confidence,
                "behavioral_confidence": behavioral_confidence,
                "context_confidence": context_confidence,
                "speaker_name": speaker_name or base_result.get("speaker_name"),
                "speaker_id": base_result.get("speaker_id"),
                "is_owner": base_result.get("is_owner", False),
                "security_level": base_result.get("security_level", "standard"),
                "feedback": {
                    "message": feedback.message,
                    "level": feedback.confidence_level.value,
                    "suggestion": feedback.suggestion
                },
                "trace_id": trace_id,
                "processing_time_ms": (time.time() - start_time) * 1000,
                "cache_stats": self.processing_cache.get_stats()
            }

            # Cache successful results
            if is_verified:
                self.processing_cache.set(audio_data, cache_key, result)

            return result

        except Exception as e:
            logger.error(f"Enhanced verification failed: {e}", exc_info=True)
            self.audit_trail.complete_trace(trace_id, "error", 0.0)
            return {
                "verified": False,
                "confidence": 0.0,
                "speaker_name": speaker_name,
                "error": str(e),
                "trace_id": trace_id
            }

    async def _speak_feedback(self, feedback: VoiceFeedback):
        """Speak voice feedback via TTS callback."""
        if self.tts_callback and feedback.speak_aloud:
            try:
                if asyncio.iscoroutinefunction(self.tts_callback):
                    await self.tts_callback(feedback.message)
                else:
                    self.tts_callback(feedback.message)
            except Exception as e:
                logger.debug(f"TTS feedback failed: {e}")

    def set_tts_callback(self, callback: Callable[[str], Any]):
        """Set the TTS callback for voice feedback."""
        self.tts_callback = callback
        logger.info("✅ TTS callback configured for voice feedback")

    def get_authentication_trace(self, trace_id: str) -> Optional[Dict[str, Any]]:
        """Get detailed authentication trace for debugging/display."""
        trace = self.audit_trail.get_trace(trace_id)
        if trace:
            return trace.to_dict()
        return None

    def get_recent_authentications(
        self,
        speaker_name: Optional[str] = None,
        limit: int = 20
    ) -> List[Dict[str, Any]]:
        """Get recent authentication attempts with full traces."""
        traces = self.audit_trail.get_recent_traces(speaker_name, limit)
        return [t.to_dict() for t in traces]

    def get_cache_stats(self) -> Dict[str, Any]:
        """Get voice processing cache statistics."""
        return self.processing_cache.get_stats()

    async def initialize(self, preload_encoder: bool = True):
        """
        Initialize service and load speaker profiles

        Args:
            preload_encoder: If True, pre-loads ECAPA-TDNN encoder during initialization
                           for instant unlock (adds ~10s to startup, but unlock is instant)
        """
        if self.initialized:
            return

        logger.info("🔐 Initializing Speaker Verification Service...")

        # Initialize learning database if not provided - use singleton
        if self.learning_db is None:
            from intelligence.learning_database import get_learning_database
            self.learning_db = await get_learning_database()

        # Initialize SpeechBrain engine for embeddings
        model_config = ModelConfig(
            name="speechbrain-wav2vec2",
            engine=STTEngine.SPEECHBRAIN,
            disk_size_mb=380,
            ram_required_gb=2.0,
            vram_required_gb=1.8,
            expected_accuracy=0.96,
            avg_latency_ms=150,
            supports_fine_tuning=True,
            model_path="speechbrain/asr-wav2vec2-commonvoice-en",
        )

        # =====================================================================
        # ML ENGINE REGISTRY INTEGRATION
        # If the global registry has already loaded ECAPA-TDNN, use that
        # instead of creating a duplicate engine instance
        # =====================================================================
        self._use_registry_encoder = False

        if ML_REGISTRY_AVAILABLE and is_voice_unlock_ready():
            logger.info("✅ ML Engine Registry has ECAPA-TDNN loaded - using singleton!")
            self._use_registry_encoder = True
            # Still create SpeechBrain engine for other features (ASR, etc)
            # but skip the encoder preload since registry already has it
            self.speechbrain_engine = SpeechBrainEngine(model_config)
            await self.speechbrain_engine.initialize()
            # Load speaker profiles
            await self._load_speaker_profiles()
            logger.info("✅ Using registry ECAPA-TDNN - unlock will be instant!")
        else:
            # Fallback: Load our own engine if registry not available
            self.speechbrain_engine = SpeechBrainEngine(model_config)

            # OPTIMIZED: Initialize engine and start encoder pre-load in PARALLEL
            # This significantly reduces startup time
            if preload_encoder:
                logger.info("🚀 Starting parallel initialization (engine + encoder + profiles)...")

                # Start encoder pre-loading in background (non-blocking)
                await self.speechbrain_engine.initialize()
                encoder_task = await self.speechbrain_engine.preload_speaker_encoder_async()

                # Load speaker profiles while encoder loads
                await self._load_speaker_profiles()

                # Wait for encoder if it's still loading
                if encoder_task and not encoder_task.done():
                    logger.info("⏳ Waiting for encoder pre-load to complete...")
                    await encoder_task

                # Mark encoder as preloaded (check the engine's state)
                if self.speechbrain_engine.speaker_encoder_loaded:
                    self._encoder_preloaded = True
                    logger.info("✅ Speaker encoder pre-loaded - unlock will be instant!")
                else:
                    logger.warning("⚠️ Speaker encoder pre-load task completed but encoder not ready")
            else:
                await self.speechbrain_engine.initialize()
                # Load speaker profiles from database
                await self._load_speaker_profiles()

        self.initialized = True
        logger.info(
            f"✅ Speaker Verification Service ready ({len(self.speaker_profiles)} profiles loaded)"
        )
        if self._use_registry_encoder:
            logger.info("   → Using ML Engine Registry singleton (no duplicate models)")

    async def _extract_speaker_embedding(self, audio_data: bytes):
        """
        Extract speaker embedding using the best available engine.

        If ML Engine Registry is available and ready, uses the singleton ECAPA-TDNN.
        Otherwise falls back to the local speechbrain_engine.

        This ensures:
        - No duplicate model instances
        - No runtime HuggingFace downloads
        - Consistent embedding quality

        Args:
            audio_data: Raw audio bytes (16kHz, mono)

        Returns:
            Embedding tensor or None on failure
        """
        try:
            # Prefer registry if available and we're configured to use it
            if self._use_registry_encoder and ML_REGISTRY_AVAILABLE:
                embedding = await registry_extract_embedding(audio_data)
                if embedding is not None:
                    return embedding
                # Fallback to local engine if registry fails
                logger.warning("Registry embedding failed, falling back to local engine")

            # Fallback to local SpeechBrain engine
            return await self.speechbrain_engine.extract_speaker_embedding(audio_data)

        except Exception as e:
            logger.error(f"Embedding extraction failed: {e}")
            return None

    async def _detect_current_model_dimension(self):
        """
        Detect the current model's embedding dimension dynamically.
        Makes system adaptive to any model without hardcoding dimensions.
        """
        if self.current_model_dimension is not None:
            return self.current_model_dimension

        try:
            logger.info("🔍 Detecting current model embedding dimension...")

            # Create realistic test audio (pink noise for better model response)
            # Pink noise has more speech-like frequency distribution than white noise
            duration = 1.0  # 1 second
            sample_rate = 16000
            num_samples = int(duration * sample_rate)

            # Generate pink noise (1/f spectrum)
            white_noise = np.random.randn(num_samples).astype(np.float32)
            # Apply simple 1/f filter
            fft = np.fft.rfft(white_noise)
            frequencies = np.fft.rfftfreq(num_samples, 1/sample_rate)
            # Avoid division by zero
            pink_filter = 1 / np.sqrt(frequencies + 1)
            fft_filtered = fft * pink_filter
            pink_noise = np.fft.irfft(fft_filtered, num_samples).astype(np.float32)

            # Normalize to prevent clipping
            pink_noise = pink_noise / np.max(np.abs(pink_noise)) * 0.3

            # Convert to bytes
            test_audio_bytes = (pink_noise * 32767).astype(np.int16).tobytes()

            # Extract test embedding
            test_embedding = await self.speechbrain_engine.extract_speaker_embedding(test_audio_bytes)

            # Handle 2D embeddings (batch dimension)
            if test_embedding.ndim == 2:
                # Shape is (1, dim) - get the actual dimension
                self.current_model_dimension = test_embedding.shape[1]
                logger.info(f"🔍 Detected 2D embedding shape: {test_embedding.shape}, using dimension: {self.current_model_dimension}D")
            else:
                # Shape is (dim,)
                self.current_model_dimension = test_embedding.shape[0]
                logger.info(f"🔍 Detected 1D embedding shape: {test_embedding.shape}, dimension: {self.current_model_dimension}D")

            logger.info(f"✅ Current model dimension: {self.current_model_dimension}D")

            # Validate dimension is reasonable
            if self.current_model_dimension < 10:
                logger.warning(f"⚠️  Detected dimension ({self.current_model_dimension}D) seems too small, using fallback")
                self.current_model_dimension = 192

            return self.current_model_dimension

        except Exception as e:
            logger.error(f"❌ Failed to detect model dimension: {e}", exc_info=True)
            self.current_model_dimension = 192  # Fallback
            logger.warning(f"⚠️  Using fallback dimension: {self.current_model_dimension}D")
            return self.current_model_dimension

    async def _migrate_embedding_dimension(self, embedding: np.ndarray, target_dim: int) -> np.ndarray:
        """
        Intelligently migrate embedding from one dimension to another.

        Uses adaptive techniques:
        - Upsampling: PCA + learned projection + interpolation
        - Downsampling: PCA for dimensionality reduction
        - Zero-padding: Simple extension for small differences

        Args:
            embedding: Source embedding
            target_dim: Target dimension

        Returns:
            Migrated embedding with target dimension
        """
        source_dim = embedding.shape[0]

        if source_dim == target_dim:
            return embedding

        logger.info(f"🔄 Migrating embedding: {source_dim}D → {target_dim}D")

        try:
            # Case 1: Upsample (e.g., 96D → 192D or 768D → 192D is actually downsample)
            if target_dim > source_dim:
                # Method: Interpolation + learned pattern repetition
                ratio = target_dim / source_dim

                if ratio == int(ratio):
                    # Perfect multiple - replicate with variation
                    migrated = np.repeat(embedding, int(ratio))
                    # Add slight variation to avoid perfect duplication
                    noise = np.random.randn(target_dim) * 0.01 * np.std(embedding)
                    migrated = migrated + noise
                else:
                    # Non-integer ratio - use interpolation
                    from scipy import interpolate
                    x_old = np.linspace(0, 1, source_dim)
                    x_new = np.linspace(0, 1, target_dim)
                    f = interpolate.interp1d(x_old, embedding, kind='cubic', fill_value='extrapolate')
                    migrated = f(x_new)

            # Case 2: Downsample (e.g., 768D → 192D or 96D → 192D is actually upsample)
            else:
                # Method: PCA or averaging
                ratio = source_dim / target_dim

                if ratio == int(ratio):
                    # Perfect divisor - use averaging
                    migrated = embedding.reshape(target_dim, int(ratio)).mean(axis=1)
                else:
                    # Non-integer ratio - use PCA-like reduction
                    # Simple reshaping with truncation
                    from scipy import signal
                    migrated = signal.resample(embedding, target_dim)

            # Normalize to maintain magnitude
            migrated = migrated.astype(np.float64)
            original_norm = np.linalg.norm(embedding)
            migrated_norm = np.linalg.norm(migrated)
            if migrated_norm > 0:
                migrated = migrated * (original_norm / migrated_norm)

            logger.info(f"✅ Migration complete: shape={migrated.shape}, norm={np.linalg.norm(migrated):.4f}")
            return migrated

        except Exception as e:
            logger.error(f"❌ Migration failed: {e}", exc_info=True)
            # Fallback: zero-padding or truncation
            if target_dim > source_dim:
                migrated = np.pad(embedding, (0, target_dim - source_dim), mode='edge')
            else:
                migrated = embedding[:target_dim]
            return migrated.astype(np.float64)

    async def _reconstruct_embedding_from_samples(self, speaker_name: str, speaker_id: int) -> np.ndarray:
        """
        Reconstruct a proper embedding using original audio samples from database.

        This solves the re-enrollment problem by using existing voice data
        to generate fresh embeddings with the current model.

        Args:
            speaker_name: Name of speaker
            speaker_id: Database ID of speaker

        Returns:
            New embedding with current model dimension
        """
        try:
            logger.info(f"🔄 Attempting to reconstruct embedding from audio samples for {speaker_name}")

            if not self.learning_db:
                logger.warning("No database connection for sample reconstruction")
                return None

            # Try to get original audio samples from database
            samples = await self.learning_db.get_voice_samples_for_speaker(speaker_id)

            if not samples or len(samples) == 0:
                logger.info(f"No audio samples found for {speaker_name}, trying alternate methods...")
                return None

            logger.info(f"Found {len(samples)} audio samples for {speaker_name}")

            # Extract embeddings from each sample using current model
            embeddings = []
            samples_with_audio = [s for s in samples[:10] if s.get("audio_data")]

            if not samples_with_audio:
                logger.warning(
                    f"No audio_data found in voice samples for {speaker_name}. "
                    f"This is expected for profiles created before audio storage was enabled. "
                    f"Will use fallback migration methods (padding/truncation)."
                )
                return None

            logger.info(f"Found {len(samples_with_audio)} samples with audio data")

            for sample in samples_with_audio:
                try:
                    audio_data = sample.get("audio_data")
                    if audio_data:
                        # Extract embedding with current model
                        embedding = await self.speechbrain_engine.extract_speaker_embedding(audio_data)
                        if embedding.shape[0] == self.current_model_dimension:
                            embeddings.append(embedding)
                            logger.info(f"  ✓ Extracted {embedding.shape[0]}D embedding from sample")
                except Exception as e:
                    logger.debug(f"Failed to extract from sample: {e}")
                    continue

            if len(embeddings) == 0:
                logger.warning(f"Could not extract any valid embeddings for {speaker_name}")
                return None

            # Average the embeddings for a robust representation
            avg_embedding = np.mean(embeddings, axis=0)
            logger.info(f"✅ Reconstructed {avg_embedding.shape[0]}D embedding from {len(embeddings)} samples")

            return avg_embedding

        except Exception as e:
            logger.error(f"Failed to reconstruct embedding for {speaker_name}: {type(e).__name__}: {e}", exc_info=True)
            return None

    async def _create_multi_model_profile(self, profile: dict, speaker_name: str) -> dict:
        """
        Create a universal profile that works across different models.

        Stores multiple embeddings for different model dimensions,
        allowing seamless model switching without re-enrollment.

        Args:
            profile: Original profile dict
            speaker_name: Name of speaker

        Returns:
            Enhanced profile with multi-model support
        """
        try:
            logger.info(f"🌐 Creating multi-model profile for {speaker_name}")

            # Store embeddings for multiple dimensions
            multi_embeddings = {}

            # Get original embedding
            embedding_bytes = profile.get("voiceprint_embedding")
            if embedding_bytes:
                # Fix: Use float32 as embeddings are stored as float32
                original_embedding = np.frombuffer(embedding_bytes, dtype=np.float32)
                original_dim = original_embedding.shape[0]
                multi_embeddings[original_dim] = original_embedding

            # Try to reconstruct for current model
            speaker_id = profile.get("speaker_id")
            if speaker_id and self.current_model_dimension not in multi_embeddings:
                reconstructed = await self._reconstruct_embedding_from_samples(speaker_name, speaker_id)
                if reconstructed is not None:
                    multi_embeddings[self.current_model_dimension] = reconstructed

            # If we still don't have the right dimension, use smart migration
            if self.current_model_dimension not in multi_embeddings:
                # Use cross-model transfer learning
                best_source_dim = min(multi_embeddings.keys(),
                                     key=lambda x: abs(x - self.current_model_dimension))
                source_embedding = multi_embeddings[best_source_dim]

                # Apply intelligent migration with cross-model compensation
                migrated = await self._cross_model_migration(
                    source_embedding,
                    source_dim=best_source_dim,
                    target_dim=self.current_model_dimension
                )
                multi_embeddings[self.current_model_dimension] = migrated

            # Update profile with multi-model support
            profile["multi_embeddings"] = multi_embeddings
            profile["supported_dimensions"] = list(multi_embeddings.keys())

            # Use the embedding for current model
            if self.current_model_dimension in multi_embeddings:
                profile["voiceprint_embedding"] = multi_embeddings[self.current_model_dimension].tobytes()
                profile["embedding_dimension"] = self.current_model_dimension
                logger.info(f"✅ Multi-model profile ready with dimensions: {profile['supported_dimensions']}")

            return profile

        except Exception as e:
            logger.error(f"Failed to create multi-model profile: {e}")
            return profile

    async def _cross_model_migration(self, embedding: np.ndarray, source_dim: int, target_dim: int) -> np.ndarray:
        """
        Advanced cross-model migration using adaptive transfer learning principles.

        This handles embeddings from fundamentally different models using dynamic
        strategy selection based on dimension ratios and embedding characteristics.

        Features:
        - Dynamic strategy selection (no hardcoded dimension pairs)
        - Multi-method fusion for optimal quality
        - Quality scoring for migration validation
        - Async processing with proper error handling
        - Voice characteristic preservation

        Args:
            embedding: Source embedding
            source_dim: Source dimension
            target_dim: Target dimension

        Returns:
            Migrated embedding optimized for cross-model compatibility
        """
        import time
        start_time = time.perf_counter()

        logger.info(f"🔀 Cross-model migration: {source_dim}D → {target_dim}D")

        # Validate inputs
        if embedding is None or len(embedding) == 0:
            logger.error("Empty embedding provided for migration")
            return np.zeros(target_dim, dtype=np.float32)

        embedding = np.asarray(embedding, dtype=np.float64).flatten()

        # CRITICAL: Check for NaN/Inf in source embedding BEFORE migration
        if np.any(np.isnan(embedding)):
            logger.error(f"❌ CRITICAL: Source embedding contains {np.sum(np.isnan(embedding))} NaN values!")
            logger.error("   Cannot migrate corrupted embedding - returning zeros")
            return np.zeros(target_dim, dtype=np.float32)
        if np.any(np.isinf(embedding)):
            logger.error(f"❌ CRITICAL: Source embedding contains {np.sum(np.isinf(embedding))} Inf values!")
            logger.error("   Cannot migrate corrupted embedding - returning zeros")
            return np.zeros(target_dim, dtype=np.float32)

        orig_norm = np.linalg.norm(embedding)
        if np.isnan(orig_norm) or orig_norm < 1e-10:
            logger.error(f"❌ CRITICAL: Source embedding has invalid norm ({orig_norm})")
            logger.error("   Cannot preserve voice characteristics - returning zeros")
            return np.zeros(target_dim, dtype=np.float32)

        orig_variance = np.var(embedding)

        # Determine migration direction and ratio
        is_downsampling = target_dim < source_dim
        ratio = source_dim / target_dim if is_downsampling else target_dim / source_dim
        is_integer_ratio = abs(ratio - round(ratio)) < 0.01
        integer_ratio = int(round(ratio))

        # Select optimal strategy based on characteristics
        strategy = await self._select_migration_strategy(
            source_dim=source_dim,
            target_dim=target_dim,
            ratio=ratio,
            is_integer_ratio=is_integer_ratio,
            is_downsampling=is_downsampling,
            embedding_variance=orig_variance
        )

        logger.info(f"📊 Selected strategy: {strategy.value} (ratio: {ratio:.2f}, integer: {is_integer_ratio})")

        # Execute migration with selected strategy
        try:
            if is_downsampling:
                migrated = await self._execute_downsampling(
                    embedding=embedding,
                    source_dim=source_dim,
                    target_dim=target_dim,
                    strategy=strategy,
                    integer_ratio=integer_ratio if is_integer_ratio else None
                )
            else:
                migrated = await self._execute_upsampling(
                    embedding=embedding,
                    source_dim=source_dim,
                    target_dim=target_dim,
                    strategy=strategy,
                    integer_ratio=integer_ratio if is_integer_ratio else None
                )

            # Normalize to preserve voice characteristics (critical for speaker verification)
            migrated = self._normalize_embedding(migrated, orig_norm)

            # CRITICAL: Validate migrated embedding for NaN/Inf BEFORE returning
            if np.any(np.isnan(migrated)):
                logger.error(f"❌ CRITICAL: Migration produced {np.sum(np.isnan(migrated))} NaN values!")
                logger.error(f"   Strategy {strategy.value} failed - falling back to simple method")
                return await self._fallback_migration(embedding, target_dim, orig_norm)
            if np.any(np.isinf(migrated)):
                logger.error(f"❌ CRITICAL: Migration produced {np.sum(np.isinf(migrated))} Inf values!")
                logger.error(f"   Strategy {strategy.value} failed - falling back to simple method")
                return await self._fallback_migration(embedding, target_dim, orig_norm)

            migrated_norm = np.linalg.norm(migrated)
            if np.isnan(migrated_norm) or migrated_norm < 1e-10:
                logger.error(f"❌ CRITICAL: Migrated embedding has invalid norm ({migrated_norm})")
                logger.error(f"   Strategy {strategy.value} failed - falling back to simple method")
                return await self._fallback_migration(embedding, target_dim, orig_norm)

            # Calculate quality metrics
            processing_time_ms = (time.perf_counter() - start_time) * 1000
            migrated_variance = np.var(migrated)
            variance_ratio = migrated_variance / (orig_variance + 1e-10)
            quality_score = self._calculate_migration_quality(
                original=embedding,
                migrated=migrated,
                variance_ratio=variance_ratio
            )

            # Log migration result
            result = MigrationResult(
                embedding=migrated,
                strategy_used=strategy,
                quality_score=quality_score,
                source_dim=source_dim,
                target_dim=target_dim,
                norm_preserved=abs(np.linalg.norm(migrated) - orig_norm) < 0.01,
                variance_ratio=variance_ratio,
                processing_time_ms=processing_time_ms,
                metadata={
                    "is_downsampling": is_downsampling,
                    "ratio": ratio,
                    "is_integer_ratio": is_integer_ratio
                }
            )

            logger.info(
                f"✅ Migration complete: {source_dim}D → {target_dim}D | "
                f"Strategy: {strategy.value} | Quality: {quality_score:.3f} | "
                f"Norm: {orig_norm:.4f} → {np.linalg.norm(migrated):.4f} | "
                f"Time: {processing_time_ms:.2f}ms"
            )

            return migrated.astype(np.float32)

        except Exception as e:
            logger.error(f"❌ Migration failed with strategy {strategy.value}: {e}", exc_info=True)
            # Fallback to simple interpolation
            return await self._fallback_migration(embedding, target_dim, orig_norm)

    async def _select_migration_strategy(
        self,
        source_dim: int,
        target_dim: int,
        ratio: float,
        is_integer_ratio: bool,
        is_downsampling: bool,
        embedding_variance: float
    ) -> MigrationStrategy:
        """
        Dynamically select the best migration strategy based on characteristics.

        Strategy selection criteria:
        - Integer ratios prefer statistical pooling (downsampling) or harmonic expansion (upsampling)
        - Large ratio differences prefer spectral methods
        - High variance embeddings prefer PCA to preserve discriminative features
        - Small differences can use interpolation
        """
        # For very small dimension differences, use interpolation
        if abs(source_dim - target_dim) <= 32:
            return MigrationStrategy.INTERPOLATION

        if is_downsampling:
            # Downsampling strategies
            if is_integer_ratio:
                # Perfect divisor - statistical pooling is optimal
                return MigrationStrategy.STATISTICAL_POOLING
            elif ratio > 4:
                # Large reduction - use hybrid approach
                return MigrationStrategy.HYBRID
            elif embedding_variance > 0.1:
                # High variance - try to preserve with spectral
                return MigrationStrategy.SPECTRAL_RESAMPLE
            else:
                # Default to spectral for non-integer ratios
                return MigrationStrategy.SPECTRAL_RESAMPLE
        else:
            # Upsampling strategies
            if is_integer_ratio:
                # Perfect multiple - harmonic expansion preserves patterns
                return MigrationStrategy.HARMONIC_EXPANSION
            elif ratio > 4:
                # Large expansion - use hybrid
                return MigrationStrategy.HYBRID
            else:
                # Default to interpolation for upsampling
                return MigrationStrategy.INTERPOLATION

    async def _execute_downsampling(
        self,
        embedding: np.ndarray,
        source_dim: int,
        target_dim: int,
        strategy: MigrationStrategy,
        integer_ratio: Optional[int] = None
    ) -> np.ndarray:
        """Execute downsampling migration using the selected strategy.

        All CPU-intensive operations run in thread pool to avoid blocking.
        """
        # Run CPU-intensive migration in thread pool
        return await self._run_in_executor(
            self._execute_downsampling_sync,
            embedding, source_dim, target_dim, strategy, integer_ratio
        )

    def _execute_downsampling_sync(
        self,
        embedding: np.ndarray,
        source_dim: int,
        target_dim: int,
        strategy: MigrationStrategy,
        integer_ratio: Optional[int] = None
    ) -> np.ndarray:
        """Sync implementation of downsampling migration (CPU-intensive)."""

        if strategy == MigrationStrategy.STATISTICAL_POOLING:
            return self._statistical_pooling_downsample(embedding, source_dim, target_dim, integer_ratio)

        elif strategy == MigrationStrategy.SPECTRAL_RESAMPLE:
            return self._spectral_resample(embedding, target_dim)

        elif strategy == MigrationStrategy.PCA_REDUCTION:
            return self._pca_like_reduction(embedding, source_dim, target_dim)

        elif strategy == MigrationStrategy.HYBRID:
            # Combine multiple methods and blend
            spectral = self._spectral_resample(embedding, target_dim)
            statistical = self._statistical_pooling_downsample(
                embedding, source_dim, target_dim,
                integer_ratio or int(round(source_dim / target_dim))
            )
            # Weighted blend favoring spectral for voice characteristics
            return 0.6 * spectral + 0.4 * statistical

        elif strategy == MigrationStrategy.INTERPOLATION:
            return self._interpolate_embedding(embedding, target_dim)

        else:
            # Default fallback
            return self._spectral_resample(embedding, target_dim)

    async def _execute_upsampling(
        self,
        embedding: np.ndarray,
        source_dim: int,
        target_dim: int,
        strategy: MigrationStrategy,
        integer_ratio: Optional[int] = None
    ) -> np.ndarray:
        """Execute upsampling migration using the selected strategy.

        All CPU-intensive operations run in thread pool to avoid blocking.
        """
        # Run CPU-intensive migration in thread pool
        return await self._run_in_executor(
            self._execute_upsampling_sync,
            embedding, source_dim, target_dim, strategy, integer_ratio
        )

    def _execute_upsampling_sync(
        self,
        embedding: np.ndarray,
        source_dim: int,
        target_dim: int,
        strategy: MigrationStrategy,
        integer_ratio: Optional[int] = None
    ) -> np.ndarray:
        """Sync implementation of upsampling migration (CPU-intensive)."""

        if strategy == MigrationStrategy.HARMONIC_EXPANSION:
            return self._harmonic_expansion_upsample(embedding, source_dim, target_dim, integer_ratio)

        elif strategy == MigrationStrategy.INTERPOLATION:
            return self._interpolate_embedding(embedding, target_dim)

        elif strategy == MigrationStrategy.SPECTRAL_RESAMPLE:
            return self._spectral_resample(embedding, target_dim)

        elif strategy == MigrationStrategy.HYBRID:
            # Combine interpolation and harmonic for rich upsampling
            interpolated = self._interpolate_embedding(embedding, target_dim)
            harmonic = self._harmonic_expansion_upsample(
                embedding, source_dim, target_dim,
                integer_ratio or int(round(target_dim / source_dim))
            )
            return 0.5 * interpolated + 0.5 * harmonic

        else:
            return self._interpolate_embedding(embedding, target_dim)

    def _statistical_pooling_downsample(
        self,
        embedding: np.ndarray,
        source_dim: int,
        target_dim: int,
        integer_ratio: Optional[int] = None
    ) -> np.ndarray:
        """
        Downsample using statistical pooling - extracts mean, std, and weighted features.
        Optimal for integer ratio reductions.
        """
        if integer_ratio and source_dim == target_dim * integer_ratio:
            # Perfect division - use block-wise statistics
            blocks = embedding.reshape(target_dim, integer_ratio)

            # Extract weighted statistics from each block
            # Weight: 60% mean (central tendency) + 25% std (variance) + 15% max (peaks)
            means = np.mean(blocks, axis=1)
            stds = np.std(blocks, axis=1)
            maxs = np.max(blocks, axis=1)

            migrated = 0.60 * means + 0.25 * stds + 0.15 * maxs
            return migrated

        else:
            # Non-perfect division - use adaptive block sizes
            ratio = source_dim / target_dim
            migrated = np.zeros(target_dim)

            for i in range(target_dim):
                start_idx = int(i * ratio)
                end_idx = int((i + 1) * ratio)
                end_idx = min(end_idx, source_dim)

                if start_idx < end_idx:
                    block = embedding[start_idx:end_idx]
                    migrated[i] = np.mean(block) * 0.7 + np.std(block) * 0.3

            return migrated

    def _spectral_resample(self, embedding: np.ndarray, target_dim: int) -> np.ndarray:
        """
        Resample using FFT - preserves frequency characteristics important for voice.
        Works well for any dimension ratio.

        CRITICAL: Includes NaN/Inf validation since FFT operations can produce
        numerical instability with certain inputs.
        """
        # CRITICAL: Validate input - FFT will propagate NaN/Inf
        if np.any(np.isnan(embedding)) or np.any(np.isinf(embedding)):
            logger.error(f"❌ _spectral_resample received corrupted input (NaN/Inf)")
            return np.zeros(target_dim, dtype=np.float64)

        try:
            from scipy import signal
            # Use scipy's resample which uses FFT internally
            result = signal.resample(embedding, target_dim)

            # CRITICAL: Validate FFT output
            if np.any(np.isnan(result)) or np.any(np.isinf(result)):
                logger.warning("⚠️ scipy.signal.resample produced NaN/Inf, using truncation fallback")
                # Safe fallback: simple truncation or padding
                if target_dim < len(embedding):
                    return embedding[:target_dim].astype(np.float64)
                else:
                    return np.pad(embedding, (0, target_dim - len(embedding)), mode='edge').astype(np.float64)

            return result
        except ImportError:
            # Fallback to numpy FFT
            fft = np.fft.rfft(embedding)
            # Resample in frequency domain
            if target_dim < len(embedding):
                # Downsampling - truncate high frequencies
                n_freq = target_dim // 2 + 1
                fft_truncated = fft[:n_freq]
                result = np.fft.irfft(fft_truncated, target_dim)
            else:
                # Upsampling - zero-pad high frequencies
                n_freq = target_dim // 2 + 1
                fft_padded = np.zeros(n_freq, dtype=complex)
                fft_padded[:len(fft)] = fft
                result = np.fft.irfft(fft_padded, target_dim)

            # CRITICAL: Validate FFT output
            if np.any(np.isnan(result)) or np.any(np.isinf(result)):
                logger.warning("⚠️ numpy FFT produced NaN/Inf, using truncation fallback")
                if target_dim < len(embedding):
                    return embedding[:target_dim].astype(np.float64)
                else:
                    return np.pad(embedding, (0, target_dim - len(embedding)), mode='edge').astype(np.float64)

            return result

    def _pca_like_reduction(
        self,
        embedding: np.ndarray,
        source_dim: int,
        target_dim: int
    ) -> np.ndarray:
        """
        PCA-like dimensionality reduction that preserves maximum variance.
        Extracts statistical features across blocks to preserve discriminative info.
        """
        # Determine optimal number of blocks
        num_blocks = max(4, target_dim // 48)  # At least 4 blocks
        block_size = source_dim // num_blocks
        remainder = source_dim % num_blocks

        features = []
        idx = 0

        for i in range(num_blocks):
            # Handle remainder by distributing extra elements
            current_block_size = block_size + (1 if i < remainder else 0)
            block = embedding[idx:idx + current_block_size]
            idx += current_block_size

            # Extract 5 statistics per block
            features.extend([
                np.mean(block),
                np.std(block),
                np.max(block),
                np.min(block),
                np.median(block)
            ])

        features = np.array(features)

        # Adjust to target dimension
        if len(features) < target_dim:
            # Pad with interpolated values
            features = self._interpolate_embedding(features, target_dim)
        elif len(features) > target_dim:
            # Truncate or resample
            features = self._spectral_resample(features, target_dim)

        return features

    def _harmonic_expansion_upsample(
        self,
        embedding: np.ndarray,
        source_dim: int,
        target_dim: int,
        integer_ratio: Optional[int] = None
    ) -> np.ndarray:
        """
        Upsample using harmonic expansion - preserves voice patterns by adding harmonics.
        """
        if integer_ratio and target_dim == source_dim * integer_ratio:
            # Perfect multiple - replicate with harmonic variation
            base = np.repeat(embedding, integer_ratio)
        else:
            # Non-perfect - interpolate first
            base = self._interpolate_embedding(embedding, target_dim)

        # Add frequency-domain harmonics to enrich the representation
        try:
            fft = np.fft.rfft(embedding)
            # Generate harmonics with slight frequency shift
            harmonics = np.fft.irfft(fft * 1.05, target_dim)

            # Blend base and harmonics (80% base, 20% harmonics)
            migrated = 0.8 * base + 0.2 * harmonics
        except Exception:
            # If FFT fails, just use the base
            migrated = base

        return migrated

    def _interpolate_embedding(self, embedding: np.ndarray, target_dim: int) -> np.ndarray:
        """
        Interpolate embedding to target dimension using cubic spline.

        CRITICAL: Includes NaN/Inf validation since scipy's interp1d with
        cubic extrapolation can produce NaN in edge cases.
        """
        source_dim = len(embedding)
        if source_dim == target_dim:
            return embedding.copy()

        # CRITICAL: Check for NaN/Inf in input - these will propagate through interpolation
        if np.any(np.isnan(embedding)) or np.any(np.isinf(embedding)):
            logger.error(f"❌ _interpolate_embedding received corrupted input (NaN/Inf)")
            # Fallback to zeros rather than propagating corruption
            return np.zeros(target_dim, dtype=np.float64)

        try:
            from scipy import interpolate
            x_old = np.linspace(0, 1, source_dim)
            x_new = np.linspace(0, 1, target_dim)
            # Use cubic interpolation with extrapolation handling
            f = interpolate.interp1d(x_old, embedding, kind='cubic', bounds_error=False, fill_value="extrapolate")
            result = f(x_new).astype(np.float64)

            # CRITICAL: Validate output - cubic extrapolation can produce NaN
            if np.any(np.isnan(result)) or np.any(np.isinf(result)):
                logger.warning("⚠️ Cubic interpolation produced NaN/Inf, falling back to linear")
                return np.interp(x_new, x_old, embedding).astype(np.float64)

            return result
        except (ImportError, ValueError) as e:
            logger.warning(f"⚠️ Interpolation error ({e}), using linear fallback")
            # Fallback to numpy linear interpolation (safer, never produces NaN)
            x_old = np.linspace(0, 1, source_dim)
            x_new = np.linspace(0, 1, target_dim)
            return np.interp(x_new, x_old, embedding).astype(np.float64)

    def _normalize_embedding(self, embedding: np.ndarray, target_norm: float) -> np.ndarray:
        """Normalize embedding to preserve original magnitude.

        CRITICAL: This function includes NaN/Inf validation to prevent
        corrupted embeddings from propagating through the system.
        """
        # CRITICAL: Validate target_norm - if NaN, this would corrupt the entire embedding
        if np.isnan(target_norm) or np.isinf(target_norm):
            logger.error(f"❌ _normalize_embedding received invalid target_norm ({target_norm})")
            return embedding  # Return unchanged rather than corrupt

        # CRITICAL: Check for NaN/Inf in input embedding
        if np.any(np.isnan(embedding)) or np.any(np.isinf(embedding)):
            logger.error(f"❌ _normalize_embedding received corrupted embedding (NaN/Inf detected)")
            return embedding  # Return unchanged rather than corrupt further

        current_norm = np.linalg.norm(embedding)
        if current_norm > 1e-10 and not np.isnan(current_norm):
            return embedding * (target_norm / current_norm)
        return embedding

    def _calculate_migration_quality(
        self,
        original: np.ndarray,
        migrated: np.ndarray,
        variance_ratio: float
    ) -> float:
        """
        Calculate quality score for migration (0.0 - 1.0).

        Factors:
        - Variance preservation (how much signal variance is retained)
        - Distribution similarity (are statistical properties preserved)
        - Norm preservation (energy conservation)
        """
        # Variance ratio component (50% weight)
        # Ideal is close to 1.0, penalize both over and under
        variance_score = 1.0 - min(abs(1.0 - variance_ratio), 1.0)

        # Distribution similarity (30% weight)
        # Compare normalized histograms
        try:
            orig_normalized = (original - np.mean(original)) / (np.std(original) + 1e-10)
            mig_normalized = (migrated - np.mean(migrated)) / (np.std(migrated) + 1e-10)

            # Compare moments
            orig_skew = np.mean(orig_normalized ** 3)
            mig_skew = np.mean(mig_normalized ** 3)
            orig_kurt = np.mean(orig_normalized ** 4) - 3
            mig_kurt = np.mean(mig_normalized ** 4) - 3

            skew_diff = abs(orig_skew - mig_skew)
            kurt_diff = abs(orig_kurt - mig_kurt)

            distribution_score = 1.0 - min((skew_diff + kurt_diff) / 4, 1.0)
        except Exception:
            distribution_score = 0.5

        # Energy preservation (20% weight)
        orig_energy = np.sum(original ** 2)
        mig_energy = np.sum(migrated ** 2)
        energy_ratio = mig_energy / (orig_energy + 1e-10)
        energy_score = 1.0 - min(abs(1.0 - energy_ratio), 1.0)

        # Weighted combination
        quality = 0.50 * variance_score + 0.30 * distribution_score + 0.20 * energy_score

        return float(np.clip(quality, 0.0, 1.0))

    async def _fallback_migration(
        self,
        embedding: np.ndarray,
        target_dim: int,
        orig_norm: float
    ) -> np.ndarray:
        """Fallback migration using simple but reliable methods."""
        source_dim = len(embedding)

        logger.warning(f"⚠️ Using fallback migration: {source_dim}D → {target_dim}D")

        # CRITICAL: Validate inputs - if orig_norm is NaN, use safe default
        if np.isnan(orig_norm) or orig_norm < 1e-10:
            logger.error(f"❌ Fallback migration received invalid orig_norm ({orig_norm}), using default 1.0")
            orig_norm = 1.0

        # CRITICAL: If input embedding has NaN, return zeros instead of propagating corruption
        if np.any(np.isnan(embedding)) or np.any(np.isinf(embedding)):
            logger.error(f"❌ Fallback migration received corrupted input embedding, returning zeros")
            return np.zeros(target_dim, dtype=np.float32)

        if target_dim > source_dim:
            # Pad with edge values
            migrated = np.pad(embedding, (0, target_dim - source_dim), mode='edge')
        else:
            # Truncate
            migrated = embedding[:target_dim]

        # Normalize with validation
        result = self._normalize_embedding(migrated, orig_norm).astype(np.float32)

        # Final validation - ensure we never return corrupted data
        if np.any(np.isnan(result)) or np.any(np.isinf(result)):
            logger.error(f"❌ Fallback migration produced corrupted output, returning zeros")
            return np.zeros(target_dim, dtype=np.float32)

        return result

    async def _auto_migrate_profile(self, profile: dict, speaker_name: str) -> dict:
        """
        Automatically migrate profile using smart reconstruction.

        Tries in order:
        1. Reconstruct from original audio samples
        2. Create multi-model profile
        3. Cross-model migration
        4. Simple dimension migration

        Args:
            profile: Profile dict with embedding
            speaker_name: Name of speaker

        Returns:
            Updated profile dict with migrated embedding
        """
        try:
            embedding_bytes = profile.get("voiceprint_embedding")
            if not embedding_bytes:
                return profile

            # Fix: Use float32 as embeddings are stored as float32
            embedding = np.frombuffer(embedding_bytes, dtype=np.float32)
            source_dim = embedding.shape[0]

            if source_dim == self.current_model_dimension:
                return profile  # Already correct dimension

            logger.info(
                f"🔄 Smart migration for {speaker_name}: "
                f"{source_dim}D → {self.current_model_dimension}D"
            )

            # Method 1: Try to reconstruct from audio samples (best)
            speaker_id = profile.get("speaker_id")
            if speaker_id:
                reconstructed = await self._reconstruct_embedding_from_samples(speaker_name, speaker_id)
                if reconstructed is not None:
                    profile["voiceprint_embedding"] = reconstructed.tobytes()
                    profile["embedding_dimension"] = self.current_model_dimension
                    profile["migration_method"] = "audio_reconstruction"
                    logger.info(f"✅ {speaker_name} reconstructed from audio samples")
                    return profile

            # Method 2: Try multi-model profile (good)
            enhanced_profile = await self._create_multi_model_profile(profile, speaker_name)
            if enhanced_profile.get("multi_embeddings"):
                logger.info(f"✅ {speaker_name} using multi-model profile")
                return enhanced_profile

            # Method 3: Use cross-model migration (acceptable)
            migrated_embedding = await self._cross_model_migration(
                embedding,
                source_dim=source_dim,
                target_dim=self.current_model_dimension
            )

            # Update profile with migrated embedding
            profile["voiceprint_embedding"] = migrated_embedding.tobytes()
            profile["embedding_dimension"] = self.current_model_dimension
            profile["migration_method"] = "cross_model"
            profile["original_dimension"] = source_dim

            # Update database asynchronously
            asyncio.create_task(self._update_profile_in_database(profile, speaker_name))

            logger.info(f"✅ {speaker_name} migrated via cross-model transfer")
            return profile

        except Exception as e:
            logger.error(f"❌ Smart migration failed for {speaker_name}: {e}", exc_info=True)
            return profile

    async def _update_profile_in_database(self, profile: dict, speaker_name: str):
        """
        Update migrated profile in database (async background task).

        Args:
            profile: Updated profile dict
            speaker_name: Name of speaker
        """
        try:
            if not self.learning_db:
                return

            speaker_id = profile.get("speaker_id")
            if not speaker_id:
                return

            logger.info(f"💾 Updating {speaker_name} profile in database...")

            # Update the profile in database
            await self.learning_db.update_speaker_profile(
                speaker_id=speaker_id,
                voiceprint_embedding=profile["voiceprint_embedding"],
                metadata={
                    "migration_applied": True,
                    "original_dimension": profile.get("original_dimension"),
                    "current_dimension": profile.get("embedding_dimension"),
                    "migrated_at": datetime.now().isoformat()
                }
            )

            logger.info(f"✅ {speaker_name} profile updated in database")

        except Exception as e:
            logger.warning(f"⚠️  Failed to update {speaker_name} in database: {e}")
            # Non-critical - migration is already in memory

    def _select_best_profile_for_speaker(self, profiles_for_speaker: list) -> dict:
        """
        Intelligently select best profile when duplicates exist.

        Prioritizes:
        1. Native dimension matching current model (100 pts)
        2. Higher total_samples (up to 50 pts)
        3. Primary user status (30 pts)
        4. Security level (up to 20 pts)
        """
        if len(profiles_for_speaker) == 1:
            return profiles_for_speaker[0]

        logger.info(f"🔍 Found {len(profiles_for_speaker)} profiles, selecting best...")

        scored_profiles = []
        for profile in profiles_for_speaker:
            score = 0
            embedding_bytes = profile.get("voiceprint_embedding")
            if not embedding_bytes:
                continue

            try:
                # Always use float32 - embeddings are stored as float32
                embedding = np.frombuffer(embedding_bytes, dtype=np.float32)
                dimension = embedding.shape[0]

                # Dimension match - highest priority
                if dimension == self.current_model_dimension:
                    score += 100
                    logger.info(f"   ✓ {profile.get('speaker_name')} matches dimension ({dimension}D)")
                else:
                    score -= 50
                    logger.info(f"   ✗ {profile.get('speaker_name')} dimension mismatch ({dimension}D vs {self.current_model_dimension}D)")

                # Total samples
                total_samples = profile.get("total_samples", 0)
                score += min(50, total_samples // 2)

                # Primary user
                if profile.get("is_primary_user", False):
                    score += 30

                # Security level
                security = profile.get("security_level", "standard")
                if security == "admin":
                    score += 20
                elif security == "high":
                    score += 15

                scored_profiles.append((score, profile, dimension))

            except Exception as e:
                logger.warning(f"⚠️  Error scoring profile: {e}")
                continue

        if not scored_profiles:
            return profiles_for_speaker[0]

        scored_profiles.sort(key=lambda x: x[0], reverse=True)
        best_score, best_profile, best_dim = scored_profiles[0]

        logger.info(
            f"✅ Selected: {best_profile.get('speaker_name')} "
            f"(score: {best_score}, {best_dim}D, {best_profile.get('total_samples', 0)} samples)"
        )

        return best_profile

    async def _load_speaker_profiles(self):
        """
        Load speaker profiles with intelligent duplicate handling.

        Enhanced features:
        1. Auto-detects current model dimension
        2. Groups profiles by speaker name
        3. Selects best profile per speaker
        4. Validates embeddings
        5. Sets adaptive thresholds
        """
        loaded_count = 0
        skipped_count = 0

        try:
            logger.info("🔄 Loading speaker profiles from database...")

            # Use singleton to get the shared database instance
            from intelligence.learning_database import get_learning_database
            self.learning_db = await get_learning_database()

            # Verify database connection
            if not self.learning_db or not self.learning_db._initialized:
                logger.error("❌ Learning database not initialized")
                raise RuntimeError("Learning database not initialized")

            # Detect current model dimension
            await self._detect_current_model_dimension()

            profiles = await self.learning_db.get_all_speaker_profiles()
            logger.info(f"📊 Found {len(profiles)} profile(s) in database")

            # Group profiles by speaker name
            profiles_by_speaker = {}
            for profile in profiles:
                speaker_name = profile.get("speaker_name")
                if speaker_name:
                    if speaker_name not in profiles_by_speaker:
                        profiles_by_speaker[speaker_name] = []
                    profiles_by_speaker[speaker_name].append(profile)

            logger.info(f"📊 Found {len(profiles_by_speaker)} unique speaker(s)")

            # Process each speaker
            for speaker_name, speaker_profiles in profiles_by_speaker.items():
                try:
                    # Select best profile if duplicates
                    if len(speaker_profiles) > 1:
                        logger.info(f"⚠️  {len(speaker_profiles)} profiles for {speaker_name}, selecting best...")
                        profile = self._select_best_profile_for_speaker(speaker_profiles)
                    else:
                        profile = speaker_profiles[0]

                    # Auto-migrate profile if dimension mismatch and auto-migration enabled
                    if self.enable_auto_migration:
                        embedding_bytes = profile.get("voiceprint_embedding")
                        if embedding_bytes:
                            # Fix: Use float32 as embeddings are stored as float32
                            test_embedding = np.frombuffer(embedding_bytes, dtype=np.float32)
                            logger.info(f"🔍 DEBUG: {speaker_name} - Current: {test_embedding.shape[0]}D, Model: {self.current_model_dimension}D")
                            if test_embedding.shape[0] != self.current_model_dimension:
                                logger.warning(f"🔄 DIMENSION MISMATCH: {speaker_name} has {test_embedding.shape[0]}D but model expects {self.current_model_dimension}D")
                                logger.info(f"🔄 Starting auto-migration for {speaker_name}...")
                                profile = await self._auto_migrate_profile(profile, speaker_name)
                                logger.info(f"✅ Auto-migration completed for {speaker_name}")
                            else:
                                logger.info(f"✅ {speaker_name} embedding dimension matches model ({self.current_model_dimension}D)")

                    # Process the selected profile
                    speaker_id = profile.get("speaker_id")

                    # Validate required fields
                    if not speaker_id or not speaker_name:
                        logger.warning(f"⚠️ Skipping invalid profile: missing speaker_id or speaker_name")
                        skipped_count += 1
                        continue

                    # Deserialize embedding
                    embedding_bytes = profile.get("voiceprint_embedding")
                    if not embedding_bytes:
                        # Use debug level for placeholder/incomplete profiles to reduce log noise
                        speaker_name_lower = speaker_name.lower()
                        if speaker_name_lower in ('unknown', 'test', 'placeholder', ''):
                            logger.debug(f"⏭️  Skipping placeholder profile '{speaker_name}' - no embedding (expected)")
                        else:
                            logger.warning(f"⚠️ Speaker profile {speaker_name} has no embedding - skipping (incomplete enrollment?)")
                        skipped_count += 1
                        continue

                    # Validate embedding data
                    try:
                        # Always use float32 - embeddings are stored as float32
                        embedding = np.frombuffer(embedding_bytes, dtype=np.float32)
                    except Exception as deserialize_error:
                        logger.error(f"❌ Failed to deserialize embedding for {speaker_name}: {deserialize_error}")
                        skipped_count += 1
                        continue

                    # Validate embedding dimension
                    if embedding.shape[0] == 0:
                        logger.warning(f"⚠️ Speaker profile {speaker_name} has empty embedding - skipping")
                        skipped_count += 1
                        continue

                    # CRITICAL: Validate embedding for NaN/Inf values
                    if np.any(np.isnan(embedding)):
                        logger.error(f"❌ CRITICAL: Speaker profile {speaker_name} embedding contains NaN values!")
                        logger.error(f"   NaN count: {np.sum(np.isnan(embedding))} out of {embedding.shape[0]}")
                        logger.error(f"   This profile is CORRUPTED and will ALWAYS fail verification")
                        logger.error(f"   Solution: Re-enroll this speaker's voice profile")
                        skipped_count += 1
                        continue

                    if np.any(np.isinf(embedding)):
                        logger.error(f"❌ CRITICAL: Speaker profile {speaker_name} embedding contains Inf values!")
                        logger.error(f"   This profile is CORRUPTED and will ALWAYS fail verification")
                        skipped_count += 1
                        continue

                    # CRITICAL: Validate embedding norm
                    embedding_norm = np.linalg.norm(embedding)
                    if embedding_norm == 0 or embedding_norm < 1e-6:
                        logger.error(f"❌ CRITICAL: Speaker profile {speaker_name} has zero-norm embedding!")
                        logger.error(f"   Embedding stats: shape={embedding.shape}, norm={embedding_norm:.10f}")
                        logger.error(f"   This profile will ALWAYS fail verification - needs re-enrollment")
                        logger.error(f"   min={embedding.min():.6f}, max={embedding.max():.6f}, mean={embedding.mean():.6f}")
                        skipped_count += 1
                        continue
                    else:
                        logger.info(f"   ✅ {speaker_name} embedding valid: norm={embedding_norm:.4f}")

                    # Assess profile quality - NOW DYNAMIC!
                    is_native = embedding.shape[0] == self.current_model_dimension
                    total_samples = profile.get("total_samples", 0)

                    # Determine quality and threshold
                    if is_native and total_samples >= 100:
                        quality = "excellent"
                        threshold = self.verification_threshold
                    elif is_native and total_samples >= 50:
                        quality = "good"
                        threshold = self.verification_threshold
                    elif total_samples >= 50:
                        quality = "fair"
                        threshold = self.legacy_threshold
                    else:
                        quality = "legacy"
                        threshold = self.legacy_threshold

                    if self.debug_mode:
                        logger.info(f"  🔍 DEBUG Profile '{speaker_name}':")
                        logger.info(f"     - Embedding dimension: {embedding.shape[0]} (Model: {self.current_model_dimension})")
                        logger.info(f"     - Is native model: {is_native}")
                        logger.info(f"     - Total samples: {total_samples}")
                        logger.info(f"     - Quality rating: {quality}")
                        logger.info(f"     - Assigned threshold: {threshold:.2%}")
                        logger.info(f"     - Created: {profile.get('created_at', 'unknown')}")

                    # Store profile with comprehensive acoustic features
                    self.speaker_profiles[speaker_name] = {
                        "speaker_id": speaker_id,
                        "embedding": embedding,
                        "embedding_dimension": profile.get("embedding_dimension", embedding.shape[0]),
                        "confidence": profile.get("recognition_confidence", 0.0),
                        "is_primary_user": profile.get("is_primary_user", False),
                        "security_level": profile.get("security_level", "standard"),
                        "total_samples": total_samples,
                        "is_native": is_native,
                        "quality": quality,
                        "threshold": threshold,

                        # 🔬 Acoustic biometric features
                        "acoustic_features": {
                            # Pitch
                            "pitch_mean_hz": profile.get("pitch_mean_hz"),
                            "pitch_std_hz": profile.get("pitch_std_hz"),
                            "pitch_range_hz": profile.get("pitch_range_hz"),
                            "pitch_min_hz": profile.get("pitch_min_hz"),
                            "pitch_max_hz": profile.get("pitch_max_hz"),

                            # Formants
                            "formant_f1_hz": profile.get("formant_f1_hz"),
                            "formant_f1_std": profile.get("formant_f1_std"),
                            "formant_f2_hz": profile.get("formant_f2_hz"),
                            "formant_f2_std": profile.get("formant_f2_std"),
                            "formant_f3_hz": profile.get("formant_f3_hz"),
                            "formant_f3_std": profile.get("formant_f3_std"),
                            "formant_f4_hz": profile.get("formant_f4_hz"),
                            "formant_f4_std": profile.get("formant_f4_std"),

                            # Spectral
                            "spectral_centroid_hz": profile.get("spectral_centroid_hz"),
                            "spectral_centroid_std": profile.get("spectral_centroid_std"),
                            "spectral_rolloff_hz": profile.get("spectral_rolloff_hz"),
                            "spectral_rolloff_std": profile.get("spectral_rolloff_std"),
                            "spectral_flux": profile.get("spectral_flux"),
                            "spectral_flux_std": profile.get("spectral_flux_std"),
                            "spectral_entropy": profile.get("spectral_entropy"),
                            "spectral_entropy_std": profile.get("spectral_entropy_std"),
                            "spectral_flatness": profile.get("spectral_flatness"),
                            "spectral_bandwidth_hz": profile.get("spectral_bandwidth_hz"),

                            # Temporal
                            "speaking_rate_wpm": profile.get("speaking_rate_wpm"),
                            "speaking_rate_std": profile.get("speaking_rate_std"),
                            "pause_ratio": profile.get("pause_ratio"),
                            "pause_ratio_std": profile.get("pause_ratio_std"),
                            "syllable_rate": profile.get("syllable_rate"),
                            "articulation_rate": profile.get("articulation_rate"),

                            # Energy
                            "energy_mean": profile.get("energy_mean"),
                            "energy_std": profile.get("energy_std"),
                            "energy_dynamic_range_db": profile.get("energy_dynamic_range_db"),

                            # Voice quality
                            "jitter_percent": profile.get("jitter_percent"),
                            "jitter_std": profile.get("jitter_std"),
                            "shimmer_percent": profile.get("shimmer_percent"),
                            "shimmer_std": profile.get("shimmer_std"),
                            "harmonic_to_noise_ratio_db": profile.get("harmonic_to_noise_ratio_db"),
                            "hnr_std": profile.get("hnr_std"),

                            # Statistical
                            "feature_covariance_matrix": profile.get("feature_covariance_matrix"),
                            "feature_statistics": profile.get("feature_statistics"),
                        },

                        # Quality metrics
                        "enrollment_quality_score": profile.get("enrollment_quality_score"),
                        "feature_extraction_version": profile.get("feature_extraction_version"),
                    }

                    self.profile_quality_scores[speaker_name] = {
                        "is_native": is_native,
                        "quality": quality,
                        "threshold": threshold,
                        "samples": total_samples,
                    }

                    # Validate acoustic features (BEAST MODE check)
                    acoustic_features = self.speaker_profiles[speaker_name]["acoustic_features"]
                    has_acoustic_features = any(
                        v is not None for v in acoustic_features.values()
                    )

                    if has_acoustic_features:
                        logger.info(
                            f"✅ Loaded: {speaker_name} "
                            f"(ID: {speaker_id}, Primary: {profile.get('is_primary_user', False)}, "
                            f"{embedding.shape[0]}D, Quality: {quality}, "
                            f"Threshold: {threshold*100:.0f}%, Samples: {total_samples}) "
                            f"🔬 BEAST MODE"
                        )
                    else:
                        logger.warning(
                            f"⚠️  Loaded: {speaker_name} "
                            f"(ID: {speaker_id}, {embedding.shape[0]}D, Samples: {total_samples}) "
                            f"- NO ACOUSTIC FEATURES (basic mode only)"
                        )
                        logger.info(
                            f"   💡 To enable BEAST MODE for {speaker_name}, run: "
                            f"python3 backend/quick_voice_enhancement.py"
                        )

                    loaded_count += 1

                except Exception as profile_error:
                    logger.error(f"❌ Error loading profile {speaker_name}: {profile_error}")
                    skipped_count += 1
                    continue

            # If no profiles found, try to bootstrap from Cloud SQL first
            if len(profiles) == 0:
                logger.warning("⚠️ No speaker profiles found in local database!")
                logger.info("🔄 Attempting to bootstrap voice profiles from Cloud SQL...")

                # Try to sync from Cloud SQL
                cloud_bootstrap_success = await self._try_bootstrap_from_cloudsql()

                if cloud_bootstrap_success:
                    # Reload profiles after bootstrap
                    profiles = await self.learning_db.get_all_speaker_profiles()
                    logger.info(f"📊 After Cloud SQL bootstrap: {len(profiles)} profile(s) available")
                else:
                    # Fallback: Auto-create owner profile from macOS
                    logger.info("🔄 Cloud SQL unavailable, creating owner profile from macOS system user...")
                    await self._auto_create_owner_profile_from_macos()

            # Summary
            logger.info(
                f"✅ Speaker profile loading complete: {loaded_count} loaded, {skipped_count} skipped"
            )

            if loaded_count == 0 and len(profiles) == 0 and not self.speaker_profiles:
                logger.warning(
                    "⚠️ No speaker profiles available - voice authentication will require enrollment"
                )
                logger.info("💡 To create a speaker profile, use voice commands like:")
                logger.info("   - 'Learn my voice as Derek'")
                logger.info("   - 'Create speaker profile for Derek'")
            elif loaded_count == 0 and len(profiles) > 0:
                logger.error(
                    f"❌ Found {len(profiles)} profiles in database but failed to load any - check logs above for errors"
                )

        except Exception as e:
            logger.error(f"❌ Failed to load speaker profiles: {e}", exc_info=True)
            logger.warning("⚠️ Continuing with 0 profiles - voice verification will fail until profiles are loaded")
            logger.info("💡 Troubleshooting steps:")
            logger.info("   1. Check database connection and credentials")
            logger.info("   2. Verify speaker_profiles table exists and has correct schema")
            logger.info("   3. Run database migrations if needed")
            logger.info("   4. Check Cloud SQL proxy is running (if using Cloud SQL)")

    async def _auto_create_owner_profile_from_macos(self) -> None:
        """
        Auto-create an owner profile from macOS system user when no profiles exist.

        This provides a fallback identity for the voice unlock system when:
        - No speaker profiles exist in the database
        - Cloud SQL is unavailable
        - First-time setup hasn't completed enrollment

        The auto-created profile allows voice unlock to recognize the macOS user as owner
        and enables enrollment-on-first-use functionality.
        """
        import os
        import subprocess
        from uuid import uuid4

        try:
            # Get macOS username
            username = os.environ.get('USER') or os.getlogin()

            # Get full name from macOS directory services
            try:
                result = subprocess.run(
                    ['dscl', '.', '-read', f'/Users/{username}', 'RealName'],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                if result.returncode == 0:
                    lines = result.stdout.strip().split('\n')
                    if len(lines) > 1:
                        full_name = lines[1].strip()
                    else:
                        full_name = username.replace('.', ' ').title()
                else:
                    full_name = username.replace('.', ' ').title()
            except (subprocess.TimeoutExpired, FileNotFoundError):
                full_name = username.replace('.', ' ').title()

            # Extract first name for speaker profile
            first_name = full_name.split()[0] if full_name else username.title()

            # Create a placeholder owner profile (no embedding - requires enrollment)
            speaker_id = f"macos_auto_{uuid4().hex[:8]}"

            self.speaker_profiles[first_name] = {
                "speaker_id": speaker_id,
                "embedding": None,  # No embedding - will require enrollment
                "embedding_dimension": self.current_model_dimension,
                "confidence": 0.0,
                "is_primary_user": True,  # Mark as owner
                "security_level": "standard",
                "total_samples": 0,
                "is_native": True,
                "quality": "auto_detected",
                "threshold": self.verification_threshold,
                "acoustic_features": {},
                "auto_created": True,
                "macos_username": username,
                "macos_full_name": full_name,
                "requires_enrollment": True,
            }

            self.profile_quality_scores[first_name] = {
                "is_native": True,
                "quality": "auto_detected",
                "threshold": self.verification_threshold,
                "samples": 0,
            }

            logger.info(f"✅ Auto-created owner profile for macOS user: {first_name}")
            logger.info(f"   macOS username: {username}")
            logger.info(f"   macOS full name: {full_name}")
            logger.info(f"   Profile marked as is_primary_user=True")
            logger.info("   ⚠️  Note: Voiceprint enrollment required for full voice authentication")
            logger.info("   💡 Say 'JARVIS, learn my voice' to complete enrollment")

        except Exception as e:
            logger.error(f"❌ Failed to auto-create owner profile from macOS: {e}")
            logger.info("💡 Fallback: Voice unlock will require manual enrollment")

    async def _try_bootstrap_from_cloudsql(self) -> bool:
        """
        Attempt to bootstrap voice profiles from Cloud SQL when local database is empty.

        This method:
        1. Creates a HybridDatabaseSync instance
        2. Initializes it with Cloud SQL connection
        3. Calls bootstrap_voice_profiles_from_cloudsql() to sync profiles
        4. Returns success/failure status

        Returns:
            True if profiles were successfully synced from Cloud SQL
            False if Cloud SQL unavailable or sync failed
        """
        hybrid_sync = None
        try:
            # Import the hybrid database sync class
            from intelligence.hybrid_database_sync import HybridDatabaseSync

            logger.info("🔄 Attempting Cloud SQL bootstrap for voice profiles...")

            # Create a new instance for bootstrap
            hybrid_sync = HybridDatabaseSync()

            # Initialize with Cloud SQL connection
            logger.info("   Initializing HybridDatabaseSync for Cloud SQL access...")
            try:
                await asyncio.wait_for(hybrid_sync.initialize(), timeout=20.0)
            except asyncio.TimeoutError:
                logger.warning("⚠️ HybridDatabaseSync initialization timed out")
                return False
            except Exception as init_error:
                logger.warning(f"⚠️ HybridDatabaseSync initialization failed: {init_error}")
                return False

            # Check if Cloud SQL is available
            if not hybrid_sync.is_initialized:
                logger.warning("⚠️ HybridDatabaseSync not initialized - Cloud SQL unavailable")
                return False

            # Call the bootstrap method
            success = await hybrid_sync.bootstrap_voice_profiles_from_cloudsql()

            if success:
                logger.info("✅ Voice profiles successfully bootstrapped from Cloud SQL")
                # Also reload profiles into FAISS cache if available
                if hasattr(hybrid_sync, 'faiss_cache') and hybrid_sync.faiss_cache:
                    logger.info("   FAISS cache updated with synced profiles")
            else:
                logger.warning("⚠️ Cloud SQL bootstrap returned no profiles")

            return success

        except ImportError as ie:
            logger.warning(f"⚠️ Could not import HybridDatabaseSync: {ie}")
            return False
        except Exception as e:
            logger.error(f"❌ Cloud SQL bootstrap failed: {e}")
            import traceback
            traceback.print_exc()
            return False
        finally:
            # Clean up the temporary sync instance
            if hybrid_sync:
                try:
                    await hybrid_sync.shutdown()
                except Exception:
                    pass

    async def verify_speaker(self, audio_data: bytes, speaker_name: Optional[str] = None) -> dict:
        """
        Verify speaker from audio with adaptive learning

        OPTIMIZED: Fast-path for cached embeddings, parallel post-verification tasks

        Args:
            audio_data: Audio bytes (WAV format)
            speaker_name: Expected speaker name (if None, identifies from all profiles)

        Returns:
            Verification result dict with:
                - verified: bool
                - confidence: float
                - speaker_name: str
                - is_owner: bool
                - security_level: str
                - adaptive_threshold: float (current dynamic threshold)
        """
        import time as time_module
        verify_start = time_module.perf_counter()

        if not self.initialized:
            await self.initialize()

        # Debug audio data (reduced logging for speed)
        audio_size = len(audio_data) if audio_data else 0
        logger.info(f"🎤 Verifying speaker from {audio_size} bytes of audio...")
        if audio_data and len(audio_data) > 0:
            # Check if audio is not silent
            import numpy as np
            # JARVIS sends int16 PCM audio, not float32
            try:
                # Try int16 first (JARVIS format)
                audio_array = np.frombuffer(audio_data[:min(2000, len(audio_data))], dtype=np.int16)
                audio_array = audio_array.astype(np.float32) / 32768.0  # Convert to float32 normalized
                logger.info(f"🎤 AUDIO DEBUG: Detected int16 PCM format")
            except:
                # Fallback to float32 if that fails
                audio_array = np.frombuffer(audio_data[:min(1000, len(audio_data))], dtype=np.float32, count=-1)
                logger.info(f"🎤 AUDIO DEBUG: Using float32 format")

            if len(audio_array) > 0:
                audio_energy = np.mean(np.abs(audio_array))
                logger.info(f"🎤 AUDIO DEBUG: Energy level = {audio_energy:.6f}")
                if audio_energy < 0.0001:
                    logger.warning("⚠️ AUDIO DEBUG: Audio appears to be silent!")
        else:
            logger.error("❌ AUDIO DEBUG: No audio data received!")

        # NOTE: Do NOT convert int16 to float32 bytes here!
        # The SpeechBrain engine expects int16 PCM or standard audio formats with headers.
        # Converting to float32 bytes strips headers and causes audio decoding to fail.
        # The engine's _audio_bytes_to_tensor() handles format conversion internally.
        logger.info(f"🎤 AUDIO DEBUG: Passing {len(audio_data) if audio_data else 0} bytes to engine (int16 PCM format preserved)")

        # 🚀 UNIFIED CACHE FAST-PATH: Try instant recognition before expensive SpeechBrain verification
        # This provides ~1ms matching vs 200-500ms for full model inference
        unified_cache_hit = False
        if self.unified_cache and self.unified_cache.is_ready:
            try:
                cache_result = await asyncio.wait_for(
                    self.unified_cache.verify_voice_from_audio(
                        audio_data=audio_data,
                        sample_rate=16000,
                        expected_speaker=speaker_name,  # Hint for faster matching
                    ),
                    timeout=2.0  # Fast-path timeout
                )

                if cache_result.matched and cache_result.similarity >= 0.85:
                    # HIGH CONFIDENCE INSTANT MATCH - skip expensive SpeechBrain!
                    unified_cache_hit = True
                    self._unified_cache_hits += 1

                    verify_end = time_module.perf_counter()
                    verify_time_ms = (verify_end - verify_start) * 1000

                    logger.info(
                        f"⚡ UNIFIED CACHE INSTANT MATCH: {cache_result.speaker_name} "
                        f"(similarity={cache_result.similarity:.2%}, type={cache_result.match_type}, "
                        f"verified in {verify_time_ms:.1f}ms)"
                    )

                    # Get profile for additional metadata
                    profile = self.speaker_profiles.get(cache_result.speaker_name, {})

                    return {
                        "verified": True,
                        "confidence": cache_result.similarity,
                        "speaker_name": cache_result.speaker_name,
                        "is_owner": profile.get("is_primary_user", True),
                        "security_level": profile.get("security_level", "standard"),
                        "match_source": "unified_cache_fast_path",
                        "match_type": cache_result.match_type,
                        "verification_time_ms": verify_time_ms,
                    }

            except asyncio.TimeoutError:
                self._unified_cache_misses += 1
                logger.debug("⏱️ Unified cache fast-path timed out, using standard SpeechBrain path")
            except Exception as e:
                self._unified_cache_misses += 1
                logger.debug(f"Unified cache fast-path failed (non-critical): {e}")

        try:
            # If speaker name provided, verify against that profile
            if speaker_name and speaker_name in self.speaker_profiles:
                profile = self.speaker_profiles[speaker_name]
                known_embedding = profile.get("embedding")

                # Check if this is an auto-created profile requiring enrollment
                if profile.get("requires_enrollment", False) or known_embedding is None:
                    logger.info(f"🔄 Profile {speaker_name} requires voice enrollment (auto-created from macOS)")
                    logger.info(f"   💡 Say 'JARVIS, learn my voice' to complete enrollment")
                    return {
                        "verified": False,
                        "confidence": 0.0,
                        "speaker_name": speaker_name,
                        "is_owner": profile.get("is_primary_user", False),
                        "security_level": profile.get("security_level", "standard"),
                        "requires_enrollment": True,
                        "enrollment_hint": f"Hello {speaker_name}! I recognize you as the device owner, but I need to learn your voice first. Say 'JARVIS, learn my voice' to complete enrollment.",
                        "error": "enrollment_required",
                        "error_detail": "Voiceprint enrollment required - profile created from macOS user"
                    }

                # DEBUG: Log embedding dimensions and validate stored embedding
                logger.info(f"🔍 DEBUG: Verifying {speaker_name}")
                logger.info(f"🔍 DEBUG: Stored embedding shape: {known_embedding.shape if hasattr(known_embedding, 'shape') else len(known_embedding) if known_embedding else 'None'}")
                logger.info(f"🔍 DEBUG: Stored embedding dimension in profile: {profile.get('embedding_dimension', 'unknown')}")

                # CRITICAL: Validate stored embedding norm BEFORE verification
                stored_norm = np.linalg.norm(known_embedding)
                logger.info(f"🔍 DEBUG: Stored embedding norm: {stored_norm:.6f}")
                if stored_norm == 0 or stored_norm < 1e-6:
                    logger.error(f"❌ CRITICAL: Stored embedding for {speaker_name} has zero norm!")
                    logger.error(f"   This profile is corrupted and needs re-enrollment")
                    return {
                        "verified": False,
                        "confidence": 0.0,
                        "speaker_name": speaker_name,
                        "error": "corrupted_profile",
                        "error_detail": "Stored embedding has zero norm - profile needs re-enrollment"
                    }

                # Get adaptive threshold based on history
                adaptive_threshold = await self._get_adaptive_threshold(speaker_name, profile)
                logger.info(f"🔍 DEBUG: Adaptive threshold: {adaptive_threshold:.2%}")

                # Check if we should enter calibration mode
                if await self._should_enter_calibration(speaker_name):
                    logger.info(f"🔄 Entering calibration mode for {speaker_name}")
                    self.calibration_mode = True
                    adaptive_threshold = self.calibration_threshold

                # Get base verification result
                if self.debug_mode:
                    logger.info(f"🎤 VERIFICATION DEBUG: Starting verification for {speaker_name}")
                    logger.info(f"  📊 Audio data size: {len(audio_data)} bytes")
                    logger.info(f"  📊 Profile has {profile.get('total_samples', 0)} training samples")

                    # BEEFED UP: Robust quality score handling
                    quality_info = self.profile_quality_scores.get(speaker_name, {"quality": 1.0}) # Default to 1.0 if not found in dict or missing key 
                    quality_score = quality_info.get("quality", 1.0) if isinstance(quality_info, dict) else quality_info # Default to 1.0 if not found in dict or missing key 

                    # Convert to float with robust error handling
                    try:
                        # Convert to float with robust error handling (incase it's a string) or default to 1.0
                        quality_score_float = float(quality_score) if quality_score is not None else 1.0
                    except (ValueError, TypeError):
                        logger.warning(f"  ⚠️ Invalid quality score type: {type(quality_score)}, defaulting to 1.0")
                        quality_score_float = 1.0 # Default to 1.0 if quality score is invalid

                    logger.info(f"  📊 Profile quality score: {quality_score_float:.2f}")
                    logger.info(f"  📊 Profile created: {profile.get('created_at', 'unknown')}")
                    logger.info(f"  📊 Profile embedding dim: {profile.get('embedding_dimension', 'unknown')}")

                is_verified, confidence = await self.speechbrain_engine.verify_speaker(
                    audio_data, known_embedding, threshold=adaptive_threshold,
                    speaker_name=speaker_name, transcription="",
                    enrolled_profile=profile  # Pass full profile with acoustic features
                )

                if self.debug_mode:
                    logger.info(f"  🔍 Base confidence: {confidence:.2%} ({confidence:.4f} raw)")
                    logger.info(f"  🔍 Threshold used: {adaptive_threshold:.2%} ({adaptive_threshold:.4f} raw)")
                    logger.info(f"  🔍 Initial verification: {'PASS' if is_verified else 'FAIL'}")
                    logger.info(f"  🔍 Confidence vs Threshold: {confidence:.4f} {'≥' if confidence >= adaptive_threshold else '<'} {adaptive_threshold:.4f}")

                # Apply multi-stage verification if enabled
                if self.multi_stage_enabled and confidence > 0.05:
                    original_confidence = confidence
                    confidence = await self._apply_multi_stage_verification(
                        confidence, audio_data, speaker_name, profile
                    )
                    if self.debug_mode:
                        logger.info(f"  🔄 Multi-stage verification: {original_confidence:.2%} → {confidence:.2%}")
                        logger.info(f"     Change: {(confidence - original_confidence):.2%} ({'↑' if confidence > original_confidence else '↓'})")

                # Apply confidence boosting if applicable
                if self.confidence_boost_enabled:
                    original_confidence = confidence
                    confidence = await self._apply_confidence_boost(
                        confidence, speaker_name, profile
                    )
                    if self.debug_mode and confidence != original_confidence:
                        logger.info(f"  ⬆️ Confidence boost applied: {original_confidence:.2%} → {confidence:.2%}")
                        logger.info(f"     Boost factor: {confidence/original_confidence:.2f}x")

                # Update verification decision based on boosted confidence
                is_verified = confidence >= adaptive_threshold
                if self.debug_mode:
                    logger.info(f"  📍 Final verification decision: {'✅ PASS' if is_verified else '❌ FAIL'}")
                    logger.info(f"  📍 Final confidence: {confidence:.2%} vs threshold: {adaptive_threshold:.2%}")

                # Build result FIRST (fast) - post-processing happens in background
                result = {
                    "verified": is_verified,
                    "confidence": confidence,
                    "speaker_name": speaker_name,
                    "speaker_id": profile["speaker_id"],
                    "is_owner": profile["is_primary_user"],
                    "security_level": profile["security_level"],
                    "adaptive_threshold": adaptive_threshold,
                }

                # Calculate verification time
                verify_elapsed = (time_module.perf_counter() - verify_start) * 1000
                logger.info(f"✅ Verification complete: {confidence:.1%} ({'PASS' if is_verified else 'FAIL'}) in {verify_elapsed:.0f}ms")

                # If confidence is very low, suggest re-enrollment
                if confidence < 0.10:
                    result["suggestion"] = "Voice profile may need re-enrollment"
                    logger.warning(f"⚠️ Very low confidence ({confidence:.2%}) for {speaker_name} - consider re-enrollment")

                # OPTIMIZATION: Run post-verification tasks in PARALLEL (non-blocking for unlock)
                # These don't affect the verification result, so we can fire-and-forget
                async def _post_verification_tasks():
                    """Background tasks that don't block unlock"""
                    try:
                        tasks = []

                        # Calibration sample handling
                        if self.calibration_mode and confidence > 0.10:
                            tasks.append(self._handle_calibration_sample(
                                audio_data, speaker_name, confidence
                            ))

                        # Store voice sample for continuous learning
                        if self.continuous_learning_enabled and self.store_all_audio:
                            tasks.append(self._store_voice_sample_async(
                                speaker_name=speaker_name,
                                audio_data=audio_data,
                                confidence=confidence,
                                verified=is_verified,
                                command="unlock_screen",
                                environment_type=self.current_environment
                            ))

                        # Learn from this attempt
                        tasks.append(self._record_verification_attempt(speaker_name, confidence, is_verified))

                        # Update Voice Memory Agent
                        try:
                            from agents.voice_memory_agent import get_voice_memory_agent
                            voice_agent = await get_voice_memory_agent()
                            tasks.append(voice_agent.record_interaction(speaker_name, confidence, is_verified))
                        except Exception:
                            pass  # Agent not available

                        # Run all tasks in parallel
                        if tasks:
                            await asyncio.gather(*tasks, return_exceptions=True)
                    except Exception as e:
                        logger.debug(f"Post-verification tasks error (non-critical): {e}")

                # Fire and forget - don't wait for post-verification tasks
                asyncio.create_task(_post_verification_tasks())

                return result

            # Otherwise, identify speaker from all profiles
            best_match = None
            best_confidence = 0.0

            for profile_name, profile in self.speaker_profiles.items():
                known_embedding = profile["embedding"]
                profile_threshold = profile.get("threshold", self.verification_threshold)
                is_verified, confidence = await self.speechbrain_engine.verify_speaker(
                    audio_data, known_embedding, threshold=profile_threshold,
                    speaker_name=profile_name, transcription="",
                    enrolled_profile=profile  # Pass full profile with acoustic features
                )

                if confidence > best_confidence:
                    best_confidence = confidence
                    best_match = {
                        "verified": is_verified,
                        "confidence": confidence,
                        "speaker_name": profile_name,
                        "speaker_id": profile["speaker_id"],
                        "is_owner": profile["is_primary_user"],
                        "security_level": profile["security_level"],
                    }

            if best_match:
                logger.info(
                    f"🎤 Speaker identified: {best_match['speaker_name']} "
                    f"(confidence: {best_match['confidence']:.1%}, "
                    f"owner: {best_match['is_owner']})"
                )
                return best_match

            # No match found - but check if we have any primary user profile
            # This ensures we at least know who the owner is even if verification failed
            primary_profile = None
            for profile_name, profile in self.speaker_profiles.items():
                if profile.get("is_primary_user", False):
                    primary_profile = profile_name
                    break

            logger.warning(f"⚠️ No speaker match found. Primary user: {primary_profile}, Best confidence: {best_confidence:.2%}")

            return {
                "verified": False,
                "confidence": best_confidence if best_confidence else 0.0,
                "speaker_name": "unknown",
                "speaker_id": None,
                "is_owner": False,
                "security_level": "none",
                "primary_user": primary_profile,  # Include who the actual owner is
                "requires_enrollment": len(self.speaker_profiles) == 0
            }

        except Exception as e:
            logger.error(f"Speaker verification error: {e}", exc_info=True)
            return {
                "verified": False,
                "confidence": 0.0,
                "speaker_name": "error",
                "speaker_id": None,
                "is_owner": False,
                "security_level": "none",
                "error": str(e),
            }

    async def is_owner(self, audio_data: bytes) -> tuple[bool, float]:
        """
        Check if audio is from the device owner (Derek J. Russell)

        Args:
            audio_data: Audio bytes

        Returns:
            Tuple of (is_owner, confidence)
        """
        result = await self.verify_speaker(audio_data)
        return result["is_owner"], result["confidence"]

    async def get_speaker_name(self, audio_data: bytes) -> str:
        """
        Get speaker name from audio

        Args:
            audio_data: Audio bytes

        Returns:
            Speaker name or "unknown"
        """
        result = await self.verify_speaker(audio_data)
        return result["speaker_name"]

    async def refresh_profiles(self):
        """Reload speaker profiles from database"""
        logger.info("🔄 Refreshing speaker profiles...")
        self.speaker_profiles.clear()
        await self._load_speaker_profiles()

    async def _get_adaptive_threshold(self, speaker_name: str, profile: dict) -> float:
        """
        Calculate adaptive threshold based on verification history

        Args:
            speaker_name: Name of speaker
            profile: Speaker profile dict

        Returns:
            Adaptive threshold value
        """
        base_threshold = profile.get("threshold", self.verification_threshold)

        # If no history, use base threshold
        if speaker_name not in self.verification_history:
            return base_threshold

        attempts = self.verification_history[speaker_name]

        # Need minimum samples for adaptation
        if len(attempts) < self.min_samples_for_update:
            return base_threshold

        # Calculate average confidence from recent attempts
        recent_attempts = attempts[-10:]  # Last 10 attempts
        successful_confidences = [a["confidence"] for a in recent_attempts if a.get("verified", False)]

        if not successful_confidences:
            # No successful attempts recently - lower threshold progressively
            avg_confidence = sum(a["confidence"] for a in recent_attempts) / len(recent_attempts)
            if avg_confidence > 0.10:
                # There's some similarity, progressively lower threshold
                # Factor in number of consecutive failures
                failure_factor = min(self.failure_count.get(speaker_name, 0) * 0.05, 0.2)
                adaptive_threshold = max(0.20, avg_confidence * (0.9 - failure_factor))
                logger.info(f"📊 Adaptive threshold for {speaker_name}: {adaptive_threshold:.2%} (lowered from {base_threshold:.2%}, failures: {self.failure_count.get(speaker_name, 0)})")
                return adaptive_threshold
            return base_threshold

        # Calculate statistics
        avg_confidence = sum(successful_confidences) / len(successful_confidences)
        min_confidence = min(successful_confidences)

        # Set threshold slightly below minimum successful confidence
        # This allows for natural variation in voice
        adaptive_threshold = max(0.25, min(base_threshold, min_confidence * 0.90))

        logger.info(
            f"📊 Adaptive threshold for {speaker_name}: {adaptive_threshold:.2%} "
            f"(base: {base_threshold:.2%}, avg: {avg_confidence:.2%}, min: {min_confidence:.2%})"
        )

        return adaptive_threshold

    async def _record_verification_attempt(self, speaker_name: str, confidence: float, verified: bool):
        """
        Record verification attempt for adaptive learning

        Args:
            speaker_name: Name of speaker
            confidence: Confidence score
            verified: Whether verification succeeded
        """
        if not self.learning_enabled:
            return

        # Initialize history for this speaker
        if speaker_name not in self.verification_history:
            self.verification_history[speaker_name] = []

        # Track failure count for calibration
        if not verified:
            self.failure_count[speaker_name] = self.failure_count.get(speaker_name, 0) + 1
            logger.info(f"❌ Verification failed for {speaker_name} (failure #{self.failure_count[speaker_name]})")
        else:
            self.failure_count[speaker_name] = 0  # Reset on success

        # Record attempt in simplified format
        attempt = {
            "timestamp": datetime.now().isoformat(),
            "confidence": confidence,
            "verified": verified,
        }

        self.verification_history[speaker_name].append(attempt)

        # Keep only recent attempts (last 50)
        if len(self.verification_history[speaker_name]) > 50:
            self.verification_history[speaker_name] = self.verification_history[speaker_name][-50:]

        # Update confidence statistics for normalization
        if speaker_name not in self.confidence_stats:
            self.confidence_stats[speaker_name] = {"scores": []}

        self.confidence_stats[speaker_name]["scores"].append(confidence)
        # Keep last N scores for statistics
        if len(self.confidence_stats[speaker_name]["scores"]) > self.confidence_history_window:
            self.confidence_stats[speaker_name]["scores"] = \
                self.confidence_stats[speaker_name]["scores"][-self.confidence_history_window:]

        # Log learning progress
        total_attempts = len(self.verification_history[speaker_name])
        if total_attempts % 5 == 0:
            recent_attempts = self.verification_history[speaker_name][-10:]
            success_rate = sum(1 for a in recent_attempts if a['verified']) / len(recent_attempts) * 100
            logger.info(
                f"📚 Learning progress for {speaker_name}: "
                f"{total_attempts} total attempts, "
                f"{success_rate:.1f}% recent success rate"
            )

    async def _check_profile_updates(self) -> dict:
        """
        Check if any speaker profiles have been updated in the database.

        Returns:
            dict: Mapping of speaker_name -> has_updates (bool)
        """
        try:
            if not self.learning_db:
                return {}

            updates = {}

            # Query database for current profile timestamps/versions
            async with self.learning_db.db.cursor() as cursor:
                await cursor.execute(
                    """
                    SELECT speaker_name, speaker_id, last_updated, total_samples,
                           enrollment_quality_score, feature_extraction_version
                    FROM speaker_profiles
                    """
                )
                profiles = await cursor.fetchall()

                for profile in profiles:
                    speaker_name = profile['speaker_name'] if isinstance(profile, dict) else profile[0]
                    speaker_id = profile['speaker_id'] if isinstance(profile, dict) else profile[1]
                    updated_at = profile['last_updated'] if isinstance(profile, dict) else profile[2]
                    total_samples = profile['total_samples'] if isinstance(profile, dict) else profile[3]
                    quality_score = profile['enrollment_quality_score'] if isinstance(profile, dict) else profile[4]
                    feature_version = profile['feature_extraction_version'] if isinstance(profile, dict) else profile[5]

                    # Create version fingerprint
                    current_fingerprint = {
                        'updated_at': str(updated_at) if updated_at else None,
                        'total_samples': total_samples,
                        'quality_score': quality_score,
                        'feature_version': feature_version,
                    }

                    # Check if we've seen this profile before
                    if speaker_name not in self.profile_version_cache:
                        # New profile detected
                        self.profile_version_cache[speaker_name] = current_fingerprint
                        updates[speaker_name] = True
                    else:
                        # Check if profile changed
                        cached_fingerprint = self.profile_version_cache[speaker_name]
                        has_changed = (
                            cached_fingerprint['updated_at'] != current_fingerprint['updated_at'] or
                            cached_fingerprint['total_samples'] != current_fingerprint['total_samples'] or
                            cached_fingerprint['quality_score'] != current_fingerprint['quality_score'] or
                            cached_fingerprint['feature_version'] != current_fingerprint['feature_version']
                        )

                        if has_changed:
                            logger.info(f"🔄 Detected update for profile '{speaker_name}'")
                            logger.debug(f"   Old: {cached_fingerprint}")
                            logger.debug(f"   New: {current_fingerprint}")
                            self.profile_version_cache[speaker_name] = current_fingerprint
                            updates[speaker_name] = True
                        else:
                            updates[speaker_name] = False

            return updates

        except Exception as e:
            logger.error(f"❌ Error checking profile updates: {e}", exc_info=True)
            return {}

    async def _profile_reload_monitor(self):
        """
        Background task that monitors for profile updates and reloads automatically.
        Runs continuously until service shutdown.
        """
        logger.info("🔄 Profile reload monitor started")

        try:
            while not self._shutdown_event.is_set():
                try:
                    # Check for updates
                    updates = await self._check_profile_updates()

                    # If any profiles updated, reload all
                    if any(updates.values()):
                        updated_profiles = [name for name, has_update in updates.items() if has_update]
                        logger.info(f"🔄 Reloading profiles due to updates: {', '.join(updated_profiles)}")
                        await self.refresh_profiles()
                        logger.info("✅ Profiles reloaded successfully with latest data from database")

                    # Wait before next check
                    await asyncio.sleep(self.reload_check_interval)

                except asyncio.CancelledError:
                    logger.info("🛑 Profile reload monitor cancelled")
                    break
                except Exception as e:
                    logger.error(f"❌ Error in profile reload monitor: {e}", exc_info=True)
                    # Continue monitoring even after error
                    await asyncio.sleep(self.reload_check_interval)

        except Exception as e:
            logger.error(f"❌ Profile reload monitor crashed: {e}", exc_info=True)
        finally:
            logger.info("🛑 Profile reload monitor stopped")

    async def manual_reload_profiles(self) -> dict:
        """
        Manually trigger profile reload (for API endpoint).

        Returns:
            dict: Status information about the reload
        """
        try:
            logger.info("🔄 Manual profile reload triggered")
            profiles_before = len(self.speaker_profiles)

            await self.refresh_profiles()

            profiles_after = len(self.speaker_profiles)

            return {
                "success": True,
                "message": "Profiles reloaded successfully",
                "profiles_before": profiles_before,
                "profiles_after": profiles_after,
                "timestamp": datetime.now().isoformat(),
            }
        except Exception as e:
            logger.error(f"❌ Manual profile reload failed: {e}", exc_info=True)
            return {
                "success": False,
                "message": f"Reload failed: {str(e)}",
                "timestamp": datetime.now().isoformat(),
            }

    async def _should_enter_calibration(self, speaker_name: str) -> bool:
        """Check if we should enter calibration mode for this speaker"""
        if not self.auto_calibrate_on_failure:
            return False

        failures = self.failure_count.get(speaker_name, 0)
        return failures >= self.max_failures_before_calibration

    async def _apply_multi_stage_verification(
        self, base_confidence: float, audio_data: bytes,
        speaker_name: str, profile: dict
    ) -> float:
        """Apply multi-stage verification with weighted scoring"""
        scores = {'primary': base_confidence}

        # Add acoustic feature scoring (simplified for now)
        acoustic_score = base_confidence * 1.1  # Boost by 10% for acoustic match
        scores['acoustic'] = min(acoustic_score, 1.0)

        # Add temporal pattern scoring
        if speaker_name in self.verification_history:
            recent_scores = [h['confidence'] for h in self.verification_history[speaker_name][-5:]]
            if recent_scores:
                temporal_score = np.mean(recent_scores) * 1.2
                scores['temporal'] = min(temporal_score, 1.0)

        # Add adaptive scoring based on rolling embeddings
        if speaker_name in self.rolling_embeddings:
            adaptive_score = base_confidence * 1.15
            scores['adaptive'] = min(adaptive_score, 1.0)

        # Calculate weighted average
        total_confidence = 0
        total_weight = 0
        for stage, weight in self.stage_weights.items():
            if stage in scores:
                total_confidence += scores[stage] * weight
                total_weight += weight

        if total_weight > 0:
            final_confidence = total_confidence / total_weight
            logger.info(f"🔄 Multi-stage verification: {base_confidence:.2%} -> {final_confidence:.2%}")
            return final_confidence

        return base_confidence

    async def _apply_confidence_boost(
        self, confidence: float, speaker_name: str, profile: dict
    ) -> float:
        """Apply confidence boosting based on patterns"""
        if confidence < self.min_confidence_for_boost:
            return confidence

        # Check if this speaker has good history
        if speaker_name in self.verification_history:
            history = self.verification_history[speaker_name]
            if len(history) >= 3:
                recent_success_rate = sum(1 for h in history[-10:] if h.get('verified', False)) / min(len(history), 10)
                if recent_success_rate > 0.5:
                    # Apply boost
                    boosted = confidence * self.boost_multiplier
                    boosted = min(boosted, 0.95)  # Cap at 95%
                    logger.info(f"🚀 Confidence boost applied: {confidence:.2%} -> {boosted:.2%}")
                    return boosted

        # Apply environmental boost if in known environment
        if self.current_environment in self.environment_profiles:
            env_boost = 1.2
            boosted = confidence * env_boost
            boosted = min(boosted, 0.95)
            if boosted > confidence:
                logger.info(f"🌍 Environment boost: {confidence:.2%} -> {boosted:.2%}")
                return boosted

        return confidence

    async def _handle_calibration_sample(
        self, audio_data: bytes, speaker_name: str, confidence: float
    ):
        """Handle a calibration sample"""
        logger.info(f"📝 Recording calibration sample for {speaker_name} (confidence: {confidence:.2%})")

        # Extract embedding from this sample
        try:
            new_embedding = await self.speechbrain_engine.extract_speaker_embedding(audio_data)

            # Add to calibration samples
            self.calibration_samples.append({
                'speaker': speaker_name,
                'embedding': new_embedding,
                'confidence': confidence,
                'timestamp': datetime.now()
            })

            # If we have enough samples, update the profile
            speaker_samples = [s for s in self.calibration_samples if s['speaker'] == speaker_name]
            if len(speaker_samples) >= 3:
                await self._update_profile_from_calibration(speaker_name, speaker_samples)
                # Reset calibration mode
                self.calibration_mode = False
                self.calibration_samples = []
                self.failure_count[speaker_name] = 0
                logger.info(f"✅ Calibration complete for {speaker_name}")
        except Exception as e:
            logger.error(f"Calibration sample error: {e}")

    async def _update_profile_from_calibration(self, speaker_name: str, samples: list):
        """Update speaker profile from calibration samples"""
        logger.info(f"🔄 Updating profile for {speaker_name} from {len(samples)} calibration samples")

        # Average the embeddings
        embeddings = [s['embedding'] for s in samples]
        avg_embedding = np.mean(embeddings, axis=0)

        # Update in database
        if self.learning_db:
            try:
                await self.learning_db.update_speaker_embedding(
                    speaker_name=speaker_name,
                    embedding=avg_embedding,
                    metadata={
                        'calibration_update': True,
                        'samples_used': len(samples),
                        'update_time': datetime.now().isoformat()
                    }
                )

                # Update local cache
                if speaker_name in self.speaker_profiles:
                    self.speaker_profiles[speaker_name]['embedding'] = avg_embedding
                    # Adjust threshold based on calibration confidence
                    avg_confidence = np.mean([s['confidence'] for s in samples])
                    new_threshold = max(0.25, avg_confidence * 0.8)  # 80% of average confidence
                    self.speaker_profiles[speaker_name]['threshold'] = new_threshold
                    logger.info(f"✅ Profile updated with new threshold: {new_threshold:.2%}")
            except Exception as e:
                logger.error(f"Failed to update profile: {e}")

    async def _store_voice_sample_async(
        self, speaker_name: str, audio_data: bytes,
        confidence: float, verified: bool,
        command: Optional[str] = None,
        environment_type: Optional[str] = None
    ):
        """
        Asynchronously store voice sample for continuous learning
        """
        try:
            # Extract embedding for storage
            embedding = None
            if self.speechbrain_engine:
                try:
                    embedding = await self.speechbrain_engine.extract_speaker_embedding(audio_data)
                except Exception as e:
                    logger.warning(f"Could not extract embedding: {e}")

            # Calculate audio quality
            quality_score = await self._calculate_audio_quality(audio_data)

            # Store in database
            sample_id = await self.learning_db.store_voice_sample(
                speaker_name=speaker_name,
                audio_data=audio_data,
                embedding=embedding,
                confidence=confidence,
                verified=verified,
                command=command,
                environment_type=environment_type,
                quality_score=quality_score,
                metadata={
                    'threshold_used': self.speaker_profiles.get(speaker_name, {}).get('threshold', self.verification_threshold),
                    'calibration_mode': self.calibration_mode,
                    'failure_count': self.failure_count.get(speaker_name, 0),
                    'timestamp': datetime.now().isoformat()
                }
            )

            # Add to memory buffer for quick access
            if len(self.voice_sample_buffer) >= self.max_buffer_size:
                self.voice_sample_buffer.pop(0)  # Remove oldest

            self.voice_sample_buffer.append({
                'sample_id': sample_id,
                'speaker_name': speaker_name,
                'confidence': confidence,
                'verified': verified,
                'embedding': embedding,
                'timestamp': datetime.now()
            })

            logger.info(f"📀 Stored voice sample #{sample_id} for {speaker_name} (conf: {confidence:.2%}, verified: {verified})")

            # Update rolling embeddings if successful
            if verified and embedding is not None:
                await self._update_rolling_embeddings(speaker_name, embedding)

            # 🎙️ Track in metrics database for voice learning analytics
            try:
                from voice_unlock.metrics_database import get_metrics_database
                metrics_db = get_metrics_database()

                # Determine if sample was added to profile (high quality + verified)
                added_to_profile = verified and quality_score >= 0.5 and confidence >= 0.50

                await metrics_db.record_voice_sample(
                    speaker_name=speaker_name,
                    confidence=confidence,
                    was_verified=verified,
                    audio_quality=quality_score,
                    snr_db=self._estimate_snr(np.frombuffer(audio_data, dtype=np.int16).astype(np.float32) / 32768.0) if audio_data else 15.0,
                    sample_source="unlock_attempt",
                    environment_type=environment_type or "unknown",
                    threshold_used=self.verification_threshold,
                    added_to_profile=added_to_profile,
                    rejection_reason=None if added_to_profile else ("low_quality" if quality_score < 0.5 else "low_confidence" if confidence < 0.50 else "not_verified"),
                    embedding_dimensions=len(embedding) if embedding is not None else 192
                )
            except Exception as metrics_error:
                logger.debug(f"Metrics tracking skipped: {metrics_error}")

        except Exception as e:
            logger.error(f"Failed to store voice sample: {e}")

    async def _calculate_audio_quality(self, audio_data: bytes) -> float:
        """
        Calculate audio quality score (0-1)
        """
        try:
            # Convert to numpy array
            audio_array = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32) / 32768.0

            # Calculate metrics
            energy = np.mean(np.abs(audio_array))
            snr = self._estimate_snr(audio_array)

            # Simple quality score
            quality = min(1.0, energy * 10) * min(1.0, snr / 20)
            return max(0.1, quality)  # Minimum quality 0.1

        except Exception:
            return 0.5  # Default quality

    def _estimate_snr(self, audio: np.ndarray) -> float:
        """Estimate signal-to-noise ratio"""
        try:
            # Simple SNR estimation
            signal_power = np.mean(audio ** 2)
            noise_floor = np.percentile(np.abs(audio), 10) ** 2

            if noise_floor > 0:
                snr_db = 10 * np.log10(signal_power / noise_floor)
                return max(0, min(30, snr_db))  # Clamp between 0-30 dB
            return 15.0  # Default SNR

        except Exception:
            return 10.0

    async def _update_rolling_embeddings(self, speaker_name: str, new_embedding: np.ndarray):
        """
        Update rolling embeddings for adaptive learning.

        NOW PERSISTS TO CLOUD SQL/DATABASE for permanent voice profile improvement!
        """
        if speaker_name not in self.rolling_embeddings:
            self.rolling_embeddings[speaker_name] = []

        # Track total samples for this speaker (for persistence decisions)
        if not hasattr(self, '_rolling_sample_counts'):
            self._rolling_sample_counts = {}
        if speaker_name not in self._rolling_sample_counts:
            self._rolling_sample_counts[speaker_name] = 0

        self._rolling_sample_counts[speaker_name] += 1
        total_samples = self._rolling_sample_counts[speaker_name]

        embeddings_list = self.rolling_embeddings[speaker_name]
        embeddings_list.append(new_embedding)

        # Keep only recent embeddings
        if len(embeddings_list) > self.max_rolling_samples:
            embeddings_list.pop(0)

        # Update profile with rolling average if enough samples
        if len(embeddings_list) >= 5:
            # Compute weighted average (recent samples have more weight)
            weights = np.linspace(0.5, 1.0, len(embeddings_list))
            weights = weights / weights.sum()

            rolling_avg = np.average(embeddings_list, axis=0, weights=weights)

            # Blend with current embedding
            if speaker_name in self.speaker_profiles:
                current_embedding = self.speaker_profiles[speaker_name]['embedding']
                updated_embedding = (
                    (1 - self.rolling_weight) * current_embedding +
                    self.rolling_weight * rolling_avg
                )

                # Update in memory
                self.speaker_profiles[speaker_name]['rolling_embedding'] = updated_embedding
                logger.info(f"📊 Updated rolling embedding for {speaker_name} ({len(embeddings_list)} samples, total: {total_samples})")

                # 🎙️ PERSIST TO DATABASE every 10 samples or at key milestones
                should_persist = (
                    total_samples % 10 == 0 or  # Every 10 samples
                    total_samples in [5, 25, 50, 100, 200]  # Key milestones
                )

                if should_persist:
                    await self._persist_improved_embedding(
                        speaker_name=speaker_name,
                        improved_embedding=updated_embedding,
                        samples_used=len(embeddings_list),
                        total_samples=total_samples
                    )

    async def _persist_improved_embedding(
        self,
        speaker_name: str,
        improved_embedding: np.ndarray,
        samples_used: int,
        total_samples: int
    ):
        """
        Persist the improved voice embedding to Cloud SQL/database.

        This is the KEY function that makes voice learning permanent!
        Every successful unlock now improves your voiceprint.
        """
        try:
            logger.info(f"🎙️ [PERSIST] Saving improved voiceprint for {speaker_name} (samples: {total_samples})...")

            # Try learning database first (Cloud SQL)
            if self.learning_db:
                try:
                    # Get speaker profile to find speaker_id
                    profile = self.speaker_profiles.get(speaker_name, {})
                    speaker_id = profile.get('speaker_id')

                    if speaker_id:
                        # Update the voiceprint embedding in Cloud SQL
                        embedding_bytes = improved_embedding.tobytes()

                        success = await self.learning_db.update_speaker_embedding(
                            speaker_id=speaker_id,
                            embedding=embedding_bytes,
                            confidence=0.95,  # High confidence since this is a learned improvement
                            is_primary_user=profile.get('is_primary_user', False)
                        )

                        if success:
                            logger.info(
                                f"✅ [PERSIST] Voiceprint saved to Cloud SQL for {speaker_name}! "
                                f"(samples: {samples_used}, total: {total_samples})"
                            )

                            # Also update the in-memory profile with the new embedding
                            self.speaker_profiles[speaker_name]['embedding'] = improved_embedding
                            self.speaker_profiles[speaker_name]['last_embedding_update'] = datetime.now().isoformat()
                            self.speaker_profiles[speaker_name]['total_learning_samples'] = total_samples

                            # 🔄 SYNC TO SQLITE: Mirror Cloud SQL data for redundancy
                            try:
                                from voice_unlock.metrics_database import get_metrics_database
                                metrics_db = get_metrics_database()

                                # Sync embedding to SQLite
                                await metrics_db.sync_embedding_to_sqlite(
                                    speaker_name=speaker_name,
                                    embedding=embedding_bytes,
                                    embedding_dimensions=len(improved_embedding),
                                    speaker_id=speaker_id,
                                    total_samples=total_samples,
                                    rolling_samples=samples_used,
                                    avg_confidence=0.95,
                                    cloud_sql_synced=True,
                                    update_reason='milestone' if total_samples in [5, 25, 50, 100, 200] else 'periodic_save'
                                )

                                # Update voice profile learning to mark embedding was persisted
                                conn = __import__('sqlite3').connect(metrics_db.sqlite_path)
                                cursor = conn.cursor()
                                cursor.execute("""
                                    UPDATE voice_profile_learning SET
                                        embedding_last_updated = ?,
                                        embedding_version = embedding_version + 1,
                                        rolling_samples_used = ?
                                    WHERE speaker_name = ?
                                """, (datetime.now().isoformat(), samples_used, speaker_name))
                                conn.commit()
                                conn.close()

                                logger.info(f"🔄 [SQLITE-SYNC] Cloud SQL + SQLite now in sync for {speaker_name}")

                            except Exception as metrics_err:
                                logger.debug(f"Metrics update skipped: {metrics_err}")

                            return True
                        else:
                            logger.warning(f"⚠️ [PERSIST] Cloud SQL update returned False for {speaker_name}")
                    else:
                        logger.warning(f"⚠️ [PERSIST] No speaker_id found for {speaker_name}")

                except Exception as db_err:
                    logger.error(f"❌ [PERSIST] Cloud SQL error: {db_err}")

            # Fallback: Store in local file
            try:
                import json
                from pathlib import Path

                persist_dir = Path.home() / ".jarvis" / "voice_profiles"
                persist_dir.mkdir(parents=True, exist_ok=True)

                profile_file = persist_dir / f"{speaker_name.replace(' ', '_')}_embedding.json"

                # Save embedding as base64
                import base64
                embedding_b64 = base64.b64encode(improved_embedding.tobytes()).decode('utf-8')

                profile_data = {
                    'speaker_name': speaker_name,
                    'embedding_b64': embedding_b64,
                    'embedding_dimensions': len(improved_embedding),
                    'samples_used': samples_used,
                    'total_learning_samples': total_samples,
                    'last_updated': datetime.now().isoformat(),
                    'dtype': str(improved_embedding.dtype)
                }

                with open(profile_file, 'w') as f:
                    json.dump(profile_data, f, indent=2)

                logger.info(f"✅ [PERSIST] Voiceprint saved locally for {speaker_name}: {profile_file}")
                return True

            except Exception as file_err:
                logger.error(f"❌ [PERSIST] Local file save failed: {file_err}")

            return False

        except Exception as e:
            logger.error(f"❌ [PERSIST] Failed to persist embedding for {speaker_name}: {e}", exc_info=True)
            return False

    async def perform_rag_similarity_search(
        self, speaker_name: str, current_embedding: np.ndarray, top_k: int = 5
    ) -> List[Dict]:
        """
        RAG: Retrieve similar voice patterns for better verification
        """
        try:
            # Get recent successful verifications from buffer
            similar_samples = []

            for sample in self.voice_sample_buffer:
                if (sample['speaker_name'] == speaker_name and
                    sample['verified'] and
                    sample.get('embedding') is not None):

                    # Calculate cosine similarity
                    similarity = self._cosine_similarity(current_embedding, sample['embedding'])
                    similar_samples.append({
                        'sample_id': sample['sample_id'],
                        'similarity': similarity,
                        'confidence': sample['confidence'],
                        'timestamp': sample['timestamp']
                    })

            # Sort by similarity and return top-k
            similar_samples.sort(key=lambda x: x['similarity'], reverse=True)
            return similar_samples[:top_k]

        except Exception as e:
            logger.error(f"RAG similarity search failed: {e}")
            return []

    def _cosine_similarity(self, a: np.ndarray, b: np.ndarray) -> float:
        """Calculate cosine similarity between two embeddings"""
        try:
            a_norm = a / np.linalg.norm(a)
            b_norm = b / np.linalg.norm(b)
            return float(np.dot(a_norm, b_norm))
        except Exception:
            return 0.0

    async def apply_human_feedback(
        self, verification_id: int, correct: bool, notes: Optional[str] = None
    ):
        """
        Apply RLHF: Human feedback on verification result

        Args:
            verification_id: ID of the verification attempt
            correct: Whether the verification was correct
            notes: Optional feedback notes
        """
        try:
            # Find the sample in buffer
            sample = None
            for s in self.voice_sample_buffer:
                if s.get('sample_id') == verification_id:
                    sample = s
                    break

            if not sample:
                logger.warning(f"Sample {verification_id} not found in buffer")
                return

            # Calculate feedback score
            feedback_score = 1.0 if correct else 0.0

            # Apply RLHF to database
            await self.learning_db.apply_rlhf_feedback(
                sample_id=verification_id,
                feedback_score=feedback_score,
                feedback_notes=notes
            )

            # Update local statistics
            speaker_name = sample['speaker_name']
            if speaker_name not in self.sample_metadata:
                self.sample_metadata[speaker_name] = {'feedback_count': 0, 'correct_count': 0}

            self.sample_metadata[speaker_name]['feedback_count'] += 1
            if correct:
                self.sample_metadata[speaker_name]['correct_count'] += 1

            logger.info(f"✅ Applied human feedback for {speaker_name} (correct: {correct})")

            # Trigger retraining if enough feedback
            feedback_count = self.sample_metadata[speaker_name]['feedback_count']
            if feedback_count >= 10 and feedback_count % 5 == 0:
                await self._trigger_retraining(speaker_name)

        except Exception as e:
            logger.error(f"Failed to apply human feedback: {e}")

    async def _trigger_retraining(self, speaker_name: str):
        """
        Trigger model retraining with accumulated samples
        """
        try:
            logger.info(f"🔄 Triggering retraining for {speaker_name}")

            # Get recent samples with feedback
            samples = await self.learning_db.get_voice_samples_for_training(
                speaker_name=speaker_name,
                limit=30,
                min_confidence=0.05  # Include low confidence samples for learning
            )

            if len(samples) >= 10:
                # Perform incremental learning
                result = await self.learning_db.perform_incremental_learning(
                    speaker_name=speaker_name,
                    new_samples=samples
                )

                if result.get('success'):
                    # Reload profile
                    await self._load_speaker_profiles()
                    logger.info(f"✅ Retraining complete for {speaker_name}: {result}")
                else:
                    logger.error(f"Retraining failed: {result.get('error')}")

        except Exception as e:
            logger.error(f"Retraining trigger failed: {e}")

    async def enable_calibration_mode(self, speaker_name: str = None):
        """Manually enable calibration mode"""
        self.calibration_mode = True
        self.calibration_samples = []
        if speaker_name:
            self.failure_count[speaker_name] = 0
        logger.info(f"🎯 Calibration mode enabled{f' for {speaker_name}' if speaker_name else ''}")
        return {"status": "calibration_enabled", "speaker": speaker_name}

    async def cleanup(self):
        """Cleanup resources and terminate background threads"""
        logger.info("🧹 Cleaning up Speaker Verification Service...")

        # Signal shutdown to background threads
        self._shutdown_event.set()

        # Cancel profile reload monitor task
        if self._reload_task and not self._reload_task.done():
            logger.debug("   Cancelling profile reload monitor...")
            self._reload_task.cancel()
            try:
                await asyncio.wait_for(self._reload_task, timeout=2.0)
                logger.debug("   ✅ Profile reload monitor cancelled")
            except (asyncio.CancelledError, asyncio.TimeoutError):
                logger.debug("   ✅ Profile reload monitor terminated")
            except Exception as e:
                logger.warning(f"   ⚠ Profile reload monitor cleanup error: {e}")

        # Wait for preload thread to complete (with timeout)
        if self._preload_thread and self._preload_thread.is_alive():
            logger.debug("   Waiting for background preload thread to finish...")
            self._preload_thread.join(timeout=2.0)

            if self._preload_thread.is_alive():
                logger.warning("   Preload thread did not exit cleanly - marking as daemon")
                # Ensure it's daemon so it doesn't block shutdown
                self._preload_thread.daemon = True
            else:
                logger.debug("   ✅ Preload thread terminated cleanly")

            self._preload_thread = None

        # Clean up event loop if still running
        if self._preload_loop and not self._preload_loop.is_closed():
            try:
                logger.debug("   Closing background event loop...")
                self._preload_loop.stop()
                self._preload_loop.close()
                self._preload_loop = None
            except Exception as e:
                logger.debug(f"   Event loop cleanup error: {e}")

        # Clean up learning database (closes background tasks and threads)
        if self.learning_db:
            try:
                logger.debug("   Closing learning database...")
                from intelligence.learning_database import close_learning_database
                await close_learning_database()
                logger.debug("   ✅ Learning database closed")
            except Exception as e:
                logger.warning(f"   ⚠ Learning database cleanup error: {e}")

        # Clean up SpeechBrain engine
        if self.speechbrain_engine:
            try:
                await self.speechbrain_engine.cleanup()
                logger.debug("   ✅ SpeechBrain engine cleaned up")
            except Exception as e:
                logger.warning(f"   ⚠ SpeechBrain cleanup error: {e}")

        # Clear caches
        self.speaker_profiles.clear()
        self.profile_quality_scores.clear()

        # Reset state
        self.initialized = False
        self._encoder_preloaded = False
        self._encoder_preloading = False
        self._preload_thread = None
        self.learning_db = None

        logger.info("✅ Speaker Verification Service cleaned up")


# Global singleton instances
_speaker_verification_service: Optional[SpeakerVerificationService] = None
_global_speaker_service: Optional[SpeakerVerificationService] = None  # Pre-loaded service from start_system.py


async def get_speaker_verification_service(
    learning_db: Optional[JARVISLearningDatabase] = None,
) -> SpeakerVerificationService:
    """
    Get global speaker verification service instance

    First checks for pre-loaded service from start_system.py,
    then falls back to creating new instance if needed.

    Args:
        learning_db: LearningDatabase instance (optional)

    Returns:
        SpeakerVerificationService instance
    """
    global _speaker_verification_service, _global_speaker_service

    # First check if there's a pre-loaded service from start_system.py
    if _global_speaker_service is not None:
        logger.info("✅ Using pre-loaded speaker verification service")
        _speaker_verification_service = _global_speaker_service
        return _global_speaker_service

    # Otherwise use the singleton pattern
    if _speaker_verification_service is None:
        logger.info("🔐 Creating new speaker verification service...")
        _speaker_verification_service = SpeakerVerificationService(learning_db)
        # Use fast initialization to avoid blocking (encoder loads in background)
        await _speaker_verification_service.initialize_fast()

    return _speaker_verification_service


async def reset_speaker_verification_service():
    """Reset service (for testing)"""
    global _speaker_verification_service
    if _speaker_verification_service:
        await _speaker_verification_service.cleanup()
    _speaker_verification_service = None


# Alias for backward compatibility
get_speaker_service = get_speaker_verification_service
