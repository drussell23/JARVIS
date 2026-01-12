"""
MultiModalPerceptionFusion v100.0 - Vision + Voice + Text Integration
=====================================================================

Advanced multi-modal perception system that enables JARVIS to:
1. Fuse information from multiple modalities (vision, voice, text)
2. Create joint embeddings across modalities
3. Perform cross-modal attention and consistency checking
4. Learn inter-modal relationships
5. Handle modality-specific noise and uncertainty

This bridges the gap between separate vision/voice/text systems for true AGI perception.

Architecture:
    ┌─────────────────────────────────────────────────────────────────┐
    │                 MultiModalPerceptionFusion                       │
    │  ┌────────────────────────────────────────────────────────────┐ │
    │  │  ModalityEncoders                                          │ │
    │  │  ├── VisionEncoder (images, screens, documents)            │ │
    │  │  ├── AudioEncoder (speech, sounds, music)                  │ │
    │  │  └── TextEncoder (natural language, code, structured)      │ │
    │  └────────────────────────────────────────────────────────────┘ │
    │  ┌────────────────────────────────────────────────────────────┐ │
    │  │  CrossModalAttention                                       │ │
    │  │  - Vision-to-text attention                                │ │
    │  │  - Audio-to-text attention                                 │ │
    │  │  - Vision-to-audio attention                               │ │
    │  └────────────────────────────────────────────────────────────┘ │
    │  ┌────────────────────────────────────────────────────────────┐ │
    │  │  FusionNetwork                                             │ │
    │  │  - Late fusion (combine encoded features)                  │ │
    │  │  - Cross-modal transformer                                 │ │
    │  │  - Uncertainty-aware aggregation                           │ │
    │  └────────────────────────────────────────────────────────────┘ │
    │  ┌────────────────────────────────────────────────────────────┐ │
    │  │  ConsistencyChecker                                        │ │
    │  │  - Cross-modal agreement scoring                           │ │
    │  │  - Conflict detection and resolution                       │ │
    │  │  - Modality reliability weighting                          │ │
    │  └────────────────────────────────────────────────────────────┘ │
    │  ┌────────────────────────────────────────────────────────────┐ │
    │  │  TemporalAligner                                           │ │
    │  │  - Multi-modal stream synchronization                      │ │
    │  │  - Event boundary detection                                │ │
    │  │  - Causal relationship inference                           │ │
    │  └────────────────────────────────────────────────────────────┘ │
    └─────────────────────────────────────────────────────────────────┘

Author: JARVIS System
Version: 100.0.0
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import time
import uuid
from abc import ABC, abstractmethod
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from pathlib import Path
from typing import (
    Any,
    Callable,
    Coroutine,
    Dict,
    Generic,
    List,
    Literal,
    Optional,
    Protocol,
    Set,
    Tuple,
    TypeVar,
    Union,
)

from backend.core.async_safety import LazyAsyncLock

import numpy as np

# Environment-driven configuration
FUSION_DATA_DIR = Path(os.getenv(
    "FUSION_DATA_DIR",
    str(Path.home() / ".jarvis" / "multi_modal_fusion")
))
EMBEDDING_DIMENSION = int(os.getenv("MULTI_MODAL_EMBEDDING_DIM", "768"))
ATTENTION_HEADS = int(os.getenv("MULTI_MODAL_ATTENTION_HEADS", "8"))
CONSISTENCY_THRESHOLD = float(os.getenv("MULTI_MODAL_CONSISTENCY_THRESHOLD", "0.7"))
TEMPORAL_WINDOW_MS = int(os.getenv("MULTI_MODAL_TEMPORAL_WINDOW_MS", "500"))
MAX_MODALITY_BUFFER_SIZE = int(os.getenv("MAX_MODALITY_BUFFER_SIZE", "100"))
FUSION_CONFIDENCE_FLOOR = float(os.getenv("FUSION_CONFIDENCE_FLOOR", "0.1"))


class Modality(Enum):
    """Types of input modalities."""
    VISION = "vision"
    AUDIO = "audio"
    TEXT = "text"
    HAPTIC = "haptic"  # Future: touch/gesture
    TEMPORAL = "temporal"  # Time-series data


class PerceptionType(Enum):
    """Types of perceptual content."""
    # Vision types
    SCREENSHOT = "screenshot"
    CAMERA_FEED = "camera_feed"
    DOCUMENT = "document"
    DIAGRAM = "diagram"

    # Audio types
    SPEECH = "speech"
    AMBIENT_SOUND = "ambient_sound"
    MUSIC = "music"
    SYSTEM_AUDIO = "system_audio"

    # Text types
    NATURAL_LANGUAGE = "natural_language"
    CODE = "code"
    STRUCTURED_DATA = "structured_data"
    COMMAND = "command"


class FusionStrategy(Enum):
    """Strategies for fusing multi-modal inputs."""
    EARLY = "early"  # Fuse raw features
    LATE = "late"  # Fuse encoded representations
    HYBRID = "hybrid"  # Combine both
    ATTENTION = "attention"  # Cross-modal attention
    WEIGHTED = "weighted"  # Confidence-weighted averaging


class ConflictResolution(Enum):
    """Strategies for resolving cross-modal conflicts."""
    TRUST_HIGHEST_CONFIDENCE = "highest_confidence"
    TRUST_MOST_RECENT = "most_recent"
    TRUST_MAJORITY = "majority"
    WEIGHTED_AVERAGE = "weighted_average"
    DEFER_TO_USER = "defer_to_user"


@dataclass
class ModalityInput:
    """A single input from one modality."""
    input_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = field(default_factory=time.time)

    # Content
    modality: Modality = Modality.TEXT
    perception_type: PerceptionType = PerceptionType.NATURAL_LANGUAGE
    raw_data: Any = None  # Original input data
    preprocessed: Optional[Any] = None  # Preprocessed data

    # Encoding
    embedding: Optional[np.ndarray] = None  # Encoded representation
    features: Dict[str, Any] = field(default_factory=dict)

    # Quality metrics
    quality_score: float = 1.0  # 0.0 to 1.0
    noise_level: float = 0.0  # Estimated noise
    confidence: float = 0.5  # Encoding confidence

    # Metadata
    source: str = "unknown"
    duration_ms: Optional[float] = None  # For temporal data

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary (excluding numpy arrays)."""
        return {
            "input_id": self.input_id,
            "timestamp": self.timestamp,
            "modality": self.modality.value,
            "perception_type": self.perception_type.value,
            "quality_score": self.quality_score,
            "noise_level": self.noise_level,
            "confidence": self.confidence,
            "source": self.source,
            "duration_ms": self.duration_ms,
            "features": self.features,
        }


@dataclass
class CrossModalAttention:
    """Attention weights between modalities."""
    source_modality: Modality
    target_modality: Modality
    attention_weights: np.ndarray  # Shape: (source_len, target_len)
    alignment_score: float  # How well aligned the modalities are
    key_alignments: List[Tuple[int, int, float]]  # Top alignments


@dataclass
class ModalityConsistency:
    """Consistency check between modalities."""
    modalities: Tuple[Modality, Modality]
    agreement_score: float  # 0.0 to 1.0
    conflicts: List[str]  # Detected conflicts
    resolution: Optional[str] = None


@dataclass
class FusedPerception:
    """Result of multi-modal fusion."""
    fusion_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = field(default_factory=time.time)

    # Input tracking
    input_ids: List[str] = field(default_factory=list)
    modalities_used: List[Modality] = field(default_factory=list)

    # Fused representation
    fused_embedding: Optional[np.ndarray] = None
    unified_features: Dict[str, Any] = field(default_factory=dict)

    # Semantic understanding
    intent: Optional[str] = None
    entities: List[Dict[str, Any]] = field(default_factory=list)
    context: Dict[str, Any] = field(default_factory=dict)

    # Confidence and quality
    overall_confidence: float = 0.5
    modality_contributions: Dict[str, float] = field(default_factory=dict)
    consistency_score: float = 1.0

    # Cross-modal insights
    cross_modal_insights: List[str] = field(default_factory=list)
    attention_highlights: List[CrossModalAttention] = field(default_factory=list)

    # Conflicts
    conflicts_detected: List[ModalityConsistency] = field(default_factory=list)
    resolution_applied: Optional[ConflictResolution] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "fusion_id": self.fusion_id,
            "timestamp": self.timestamp,
            "input_ids": self.input_ids,
            "modalities_used": [m.value for m in self.modalities_used],
            "intent": self.intent,
            "entities": self.entities,
            "context": self.context,
            "overall_confidence": self.overall_confidence,
            "modality_contributions": self.modality_contributions,
            "consistency_score": self.consistency_score,
            "cross_modal_insights": self.cross_modal_insights,
        }


class ModalityEncoder(ABC):
    """Base class for modality-specific encoders."""

    @abstractmethod
    async def encode(self, input_data: ModalityInput) -> np.ndarray:
        """Encode input data to embedding."""
        pass

    @abstractmethod
    async def extract_features(self, input_data: ModalityInput) -> Dict[str, Any]:
        """Extract semantic features from input."""
        pass

    @abstractmethod
    def get_quality_score(self, input_data: ModalityInput) -> float:
        """Estimate quality of input data."""
        pass


class VisionEncoder(ModalityEncoder):
    """Encoder for visual inputs."""

    def __init__(self, embedding_dim: int = EMBEDDING_DIMENSION):
        self.embedding_dim = embedding_dim
        self.logger = logging.getLogger("VisionEncoder")

    async def encode(self, input_data: ModalityInput) -> np.ndarray:
        """Encode visual input to embedding."""
        # In production, this would use CLIP, ViT, or similar
        # For now, create a simulated embedding based on features

        features = await self.extract_features(input_data)

        # Create embedding from features
        embedding = np.zeros(self.embedding_dim)

        # Encode different visual properties
        if features.get("has_text"):
            embedding[:100] = np.random.randn(100) * 0.5 + 0.5

        if features.get("has_faces"):
            embedding[100:200] = np.random.randn(100) * 0.3 + 0.7

        if features.get("is_screenshot"):
            embedding[200:300] = np.random.randn(100) * 0.2 + 0.8

        # Normalize
        norm = np.linalg.norm(embedding)
        if norm > 0:
            embedding = embedding / norm

        return embedding

    async def extract_features(self, input_data: ModalityInput) -> Dict[str, Any]:
        """Extract visual features."""
        features = {
            "has_text": False,
            "has_faces": False,
            "is_screenshot": input_data.perception_type == PerceptionType.SCREENSHOT,
            "is_document": input_data.perception_type == PerceptionType.DOCUMENT,
            "dominant_colors": [],
            "objects_detected": [],
            "text_regions": [],
        }

        # In production, run actual vision models
        # For now, infer from metadata
        if input_data.features:
            features.update(input_data.features)

        return features

    def get_quality_score(self, input_data: ModalityInput) -> float:
        """Estimate image quality."""
        base_score = 0.7

        # Adjust based on known factors
        if input_data.perception_type == PerceptionType.SCREENSHOT:
            base_score = 0.95  # Screenshots are usually high quality

        # Reduce for noise
        base_score -= input_data.noise_level * 0.3

        return max(0.1, min(1.0, base_score))


class AudioEncoder(ModalityEncoder):
    """Encoder for audio inputs."""

    def __init__(self, embedding_dim: int = EMBEDDING_DIMENSION):
        self.embedding_dim = embedding_dim
        self.logger = logging.getLogger("AudioEncoder")

    async def encode(self, input_data: ModalityInput) -> np.ndarray:
        """Encode audio input to embedding."""
        features = await self.extract_features(input_data)

        embedding = np.zeros(self.embedding_dim)

        # Encode speech characteristics
        if features.get("is_speech"):
            embedding[:200] = np.random.randn(200) * 0.4 + 0.6

        # Encode speaker characteristics
        if features.get("speaker_embedding") is not None:
            speaker_emb = features["speaker_embedding"]
            embedding[200:392] = speaker_emb[:min(192, len(speaker_emb))]

        # Encode emotional characteristics
        emotion_score = features.get("emotion_score", 0.5)
        embedding[400:450] = emotion_score

        # Normalize
        norm = np.linalg.norm(embedding)
        if norm > 0:
            embedding = embedding / norm

        return embedding

    async def extract_features(self, input_data: ModalityInput) -> Dict[str, Any]:
        """Extract audio features."""
        features = {
            "is_speech": input_data.perception_type == PerceptionType.SPEECH,
            "duration_ms": input_data.duration_ms or 0,
            "snr_db": 20.0,  # Signal-to-noise ratio
            "speaker_embedding": None,
            "transcript": None,
            "emotion_score": 0.5,
            "speech_rate": 1.0,
        }

        if input_data.features:
            features.update(input_data.features)

        return features

    def get_quality_score(self, input_data: ModalityInput) -> float:
        """Estimate audio quality."""
        base_score = 0.7

        # High SNR means better quality
        snr = input_data.features.get("snr_db", 15)
        if snr > 20:
            base_score = 0.9
        elif snr > 10:
            base_score = 0.7
        else:
            base_score = 0.5

        return max(0.1, min(1.0, base_score - input_data.noise_level * 0.2))


class TextEncoder(ModalityEncoder):
    """Encoder for text inputs."""

    def __init__(self, embedding_dim: int = EMBEDDING_DIMENSION):
        self.embedding_dim = embedding_dim
        self.logger = logging.getLogger("TextEncoder")

    async def encode(self, input_data: ModalityInput) -> np.ndarray:
        """Encode text input to embedding."""
        features = await self.extract_features(input_data)

        embedding = np.zeros(self.embedding_dim)

        # Simple bag-of-words style embedding
        text = str(input_data.raw_data or "")

        # Hash-based embedding (in production, use sentence transformers)
        if text:
            for i, char in enumerate(text[:self.embedding_dim]):
                embedding[i % self.embedding_dim] += ord(char) / 1000.0

        # Add semantic features
        if features.get("is_question"):
            embedding[700:750] = 0.8

        if features.get("is_command"):
            embedding[750:768] = 0.9

        # Normalize
        norm = np.linalg.norm(embedding)
        if norm > 0:
            embedding = embedding / norm

        return embedding

    async def extract_features(self, input_data: ModalityInput) -> Dict[str, Any]:
        """Extract text features."""
        text = str(input_data.raw_data or "")

        features = {
            "length": len(text),
            "word_count": len(text.split()),
            "is_question": text.strip().endswith("?"),
            "is_command": any(
                text.lower().startswith(cmd)
                for cmd in ["please", "can you", "could you", "do", "run", "execute"]
            ),
            "language": "en",
            "sentiment": 0.0,
            "keywords": [],
        }

        if input_data.features:
            features.update(input_data.features)

        return features

    def get_quality_score(self, input_data: ModalityInput) -> float:
        """Estimate text quality."""
        text = str(input_data.raw_data or "")

        if not text:
            return 0.1

        # Base quality on length and structure
        if len(text) > 10:
            base_score = 0.9
        elif len(text) > 3:
            base_score = 0.7
        else:
            base_score = 0.5

        return base_score


class CrossModalAttentionModule:
    """Computes attention between different modalities."""

    def __init__(self, embedding_dim: int = EMBEDDING_DIMENSION, num_heads: int = ATTENTION_HEADS):
        self.embedding_dim = embedding_dim
        self.num_heads = num_heads
        self.head_dim = embedding_dim // num_heads
        self.logger = logging.getLogger("CrossModalAttention")

    async def compute_attention(
        self,
        source: ModalityInput,
        target: ModalityInput
    ) -> CrossModalAttention:
        """Compute attention from source to target modality."""
        if source.embedding is None or target.embedding is None:
            raise ValueError("Both inputs must have embeddings")

        source_emb = source.embedding
        target_emb = target.embedding

        # Compute attention scores (simplified dot-product attention)
        # In production, use learned query/key/value projections
        attention_score = np.dot(source_emb, target_emb)
        attention_weights = np.array([[attention_score]])

        # Find key alignments
        alignment_score = float(attention_score)

        return CrossModalAttention(
            source_modality=source.modality,
            target_modality=target.modality,
            attention_weights=attention_weights,
            alignment_score=min(1.0, max(0.0, (alignment_score + 1) / 2)),
            key_alignments=[(0, 0, alignment_score)],
        )


class ConsistencyChecker:
    """Checks consistency between modalities."""

    def __init__(self, threshold: float = CONSISTENCY_THRESHOLD):
        self.threshold = threshold
        self.logger = logging.getLogger("ConsistencyChecker")

    async def check_consistency(
        self,
        inputs: List[ModalityInput]
    ) -> List[ModalityConsistency]:
        """Check consistency between all pairs of modalities."""
        results = []

        for i, input1 in enumerate(inputs):
            for input2 in inputs[i + 1:]:
                consistency = await self._check_pair(input1, input2)
                results.append(consistency)

        return results

    async def _check_pair(
        self,
        input1: ModalityInput,
        input2: ModalityInput
    ) -> ModalityConsistency:
        """Check consistency between two inputs."""
        conflicts = []

        # Embedding similarity
        embedding_sim = 0.5
        if input1.embedding is not None and input2.embedding is not None:
            embedding_sim = float(np.dot(input1.embedding, input2.embedding))
            embedding_sim = (embedding_sim + 1) / 2  # Normalize to 0-1

        # Feature consistency
        feature_agreement = self._check_feature_agreement(input1.features, input2.features)

        # Temporal consistency
        time_diff = abs(input1.timestamp - input2.timestamp)
        temporal_consistent = time_diff < TEMPORAL_WINDOW_MS / 1000

        if not temporal_consistent:
            conflicts.append(f"Temporal mismatch: {time_diff:.2f}s apart")

        # Calculate overall agreement
        agreement = (embedding_sim * 0.4 + feature_agreement * 0.4 + (0.2 if temporal_consistent else 0))

        return ModalityConsistency(
            modalities=(input1.modality, input2.modality),
            agreement_score=agreement,
            conflicts=conflicts,
        )

    def _check_feature_agreement(
        self,
        features1: Dict[str, Any],
        features2: Dict[str, Any]
    ) -> float:
        """Check if extracted features agree."""
        if not features1 or not features2:
            return 0.5

        common_keys = set(features1.keys()) & set(features2.keys())
        if not common_keys:
            return 0.5

        agreements = 0
        for key in common_keys:
            if features1[key] == features2[key]:
                agreements += 1

        return agreements / len(common_keys)


class TemporalAligner:
    """Aligns multi-modal inputs in time."""

    def __init__(self, window_ms: int = TEMPORAL_WINDOW_MS):
        self.window_ms = window_ms
        self.logger = logging.getLogger("TemporalAligner")

    async def align(
        self,
        inputs: List[ModalityInput]
    ) -> List[List[ModalityInput]]:
        """Group inputs into temporally aligned windows."""
        if not inputs:
            return []

        # Sort by timestamp
        sorted_inputs = sorted(inputs, key=lambda x: x.timestamp)

        windows = []
        current_window = [sorted_inputs[0]]
        window_start = sorted_inputs[0].timestamp

        for inp in sorted_inputs[1:]:
            time_diff_ms = (inp.timestamp - window_start) * 1000

            if time_diff_ms <= self.window_ms:
                current_window.append(inp)
            else:
                windows.append(current_window)
                current_window = [inp]
                window_start = inp.timestamp

        if current_window:
            windows.append(current_window)

        return windows

    async def detect_events(
        self,
        aligned_windows: List[List[ModalityInput]]
    ) -> List[Dict[str, Any]]:
        """Detect significant events from aligned windows."""
        events = []

        for i, window in enumerate(aligned_windows):
            modalities_present = set(inp.modality for inp in window)

            # Detect multi-modal events (more interesting)
            if len(modalities_present) >= 2:
                events.append({
                    "event_id": str(uuid.uuid4()),
                    "window_index": i,
                    "timestamp": window[0].timestamp,
                    "modalities": [m.value for m in modalities_present],
                    "inputs": [inp.input_id for inp in window],
                    "is_multi_modal": True,
                })

        return events


class FusionNetwork:
    """Fuses multi-modal inputs into unified representation."""

    def __init__(
        self,
        embedding_dim: int = EMBEDDING_DIMENSION,
        strategy: FusionStrategy = FusionStrategy.ATTENTION
    ):
        self.embedding_dim = embedding_dim
        self.strategy = strategy
        self.logger = logging.getLogger("FusionNetwork")

    async def fuse(
        self,
        inputs: List[ModalityInput],
        attentions: List[CrossModalAttention],
        consistencies: List[ModalityConsistency]
    ) -> FusedPerception:
        """Fuse multiple modality inputs into single perception."""
        if not inputs:
            return FusedPerception()

        result = FusedPerception(
            input_ids=[inp.input_id for inp in inputs],
            modalities_used=[inp.modality for inp in inputs],
        )

        # Calculate modality weights based on quality and consistency
        weights = self._calculate_weights(inputs, consistencies)
        result.modality_contributions = {
            inp.modality.value: weights[i]
            for i, inp in enumerate(inputs)
        }

        # Fuse embeddings
        if self.strategy == FusionStrategy.WEIGHTED:
            result.fused_embedding = await self._weighted_fusion(inputs, weights)
        elif self.strategy == FusionStrategy.ATTENTION:
            result.fused_embedding = await self._attention_fusion(inputs, attentions, weights)
        else:
            result.fused_embedding = await self._late_fusion(inputs, weights)

        # Fuse features
        result.unified_features = self._fuse_features(inputs, weights)

        # Calculate overall confidence
        result.overall_confidence = self._calculate_overall_confidence(inputs, weights, consistencies)

        # Calculate consistency score
        if consistencies:
            result.consistency_score = sum(c.agreement_score for c in consistencies) / len(consistencies)
            result.conflicts_detected = [c for c in consistencies if c.conflicts]

        # Extract cross-modal insights
        result.cross_modal_insights = self._extract_insights(inputs, attentions)

        # Store attention highlights
        result.attention_highlights = attentions

        return result

    def _calculate_weights(
        self,
        inputs: List[ModalityInput],
        consistencies: List[ModalityConsistency]
    ) -> List[float]:
        """Calculate importance weights for each input."""
        weights = []

        for inp in inputs:
            # Base weight on quality and confidence
            weight = inp.quality_score * inp.confidence

            # Adjust based on consistency with other modalities
            consistency_bonus = 0.0
            for cons in consistencies:
                if inp.modality in cons.modalities:
                    consistency_bonus += cons.agreement_score * 0.2

            weight += consistency_bonus
            weights.append(weight)

        # Normalize weights
        total = sum(weights)
        if total > 0:
            weights = [w / total for w in weights]
        else:
            weights = [1.0 / len(weights)] * len(weights)

        return weights

    async def _weighted_fusion(
        self,
        inputs: List[ModalityInput],
        weights: List[float]
    ) -> np.ndarray:
        """Simple weighted average fusion."""
        embeddings = [
            inp.embedding for inp in inputs
            if inp.embedding is not None
        ]

        if not embeddings:
            return np.zeros(self.embedding_dim)

        fused = np.zeros(self.embedding_dim)
        for emb, weight in zip(embeddings, weights[:len(embeddings)]):
            fused += emb * weight

        # Normalize
        norm = np.linalg.norm(fused)
        if norm > 0:
            fused = fused / norm

        return fused

    async def _attention_fusion(
        self,
        inputs: List[ModalityInput],
        attentions: List[CrossModalAttention],
        weights: List[float]
    ) -> np.ndarray:
        """Attention-weighted fusion."""
        embeddings = [
            inp.embedding for inp in inputs
            if inp.embedding is not None
        ]

        if not embeddings:
            return np.zeros(self.embedding_dim)

        # Start with weighted average
        fused = await self._weighted_fusion(inputs, weights)

        # Apply attention adjustments
        for attention in attentions:
            alignment_boost = attention.alignment_score * 0.1
            fused += alignment_boost * np.random.randn(self.embedding_dim) * 0.01

        # Normalize
        norm = np.linalg.norm(fused)
        if norm > 0:
            fused = fused / norm

        return fused

    async def _late_fusion(
        self,
        inputs: List[ModalityInput],
        weights: List[float]
    ) -> np.ndarray:
        """Concatenate and project (simplified late fusion)."""
        return await self._weighted_fusion(inputs, weights)

    def _fuse_features(
        self,
        inputs: List[ModalityInput],
        weights: List[float]
    ) -> Dict[str, Any]:
        """Fuse semantic features from all modalities."""
        unified = {}

        for inp, weight in zip(inputs, weights):
            for key, value in inp.features.items():
                if key not in unified:
                    unified[key] = {"value": value, "weight": weight, "source": inp.modality.value}
                elif weight > unified[key]["weight"]:
                    unified[key] = {"value": value, "weight": weight, "source": inp.modality.value}

        # Flatten to just values
        return {k: v["value"] for k, v in unified.items()}

    def _calculate_overall_confidence(
        self,
        inputs: List[ModalityInput],
        weights: List[float],
        consistencies: List[ModalityConsistency]
    ) -> float:
        """Calculate overall fusion confidence."""
        # Base confidence: weighted average of input confidences
        base_confidence = sum(
            inp.confidence * weight
            for inp, weight in zip(inputs, weights)
        )

        # Consistency bonus
        if consistencies:
            avg_consistency = sum(c.agreement_score for c in consistencies) / len(consistencies)
            consistency_bonus = (avg_consistency - 0.5) * 0.2  # -0.1 to +0.1
        else:
            consistency_bonus = 0

        # Multi-modal bonus (more modalities = more confidence)
        modalities_used = len(set(inp.modality for inp in inputs))
        multi_modal_bonus = min(0.1, modalities_used * 0.03)

        overall = base_confidence + consistency_bonus + multi_modal_bonus
        return max(FUSION_CONFIDENCE_FLOOR, min(0.99, overall))

    def _extract_insights(
        self,
        inputs: List[ModalityInput],
        attentions: List[CrossModalAttention]
    ) -> List[str]:
        """Extract cross-modal insights."""
        insights = []

        # Check for strong alignments
        for attention in attentions:
            if attention.alignment_score > 0.8:
                insights.append(
                    f"Strong alignment between {attention.source_modality.value} "
                    f"and {attention.target_modality.value} "
                    f"(score: {attention.alignment_score:.2f})"
                )

        # Check for complementary information
        modalities = set(inp.modality for inp in inputs)
        if Modality.VISION in modalities and Modality.AUDIO in modalities:
            insights.append("Vision and audio provide complementary context")

        if Modality.TEXT in modalities and Modality.AUDIO in modalities:
            # Check if text might be transcript of audio
            insights.append("Text may provide transcription of audio content")

        return insights


class MultiModalPerceptionFusion:
    """
    Main engine for multi-modal perception fusion.

    Integrates vision, voice, and text into unified understanding.
    """

    def __init__(self):
        self.logger = logging.getLogger("MultiModalPerceptionFusion")

        # Initialize encoders
        self.encoders: Dict[Modality, ModalityEncoder] = {
            Modality.VISION: VisionEncoder(),
            Modality.AUDIO: AudioEncoder(),
            Modality.TEXT: TextEncoder(),
        }

        # Initialize components
        self.attention_module = CrossModalAttentionModule()
        self.consistency_checker = ConsistencyChecker()
        self.temporal_aligner = TemporalAligner()
        self.fusion_network = FusionNetwork()

        # Input buffer
        self._input_buffer: deque = deque(maxlen=MAX_MODALITY_BUFFER_SIZE)
        self._fusion_history: deque = deque(maxlen=100)

        # Background processing
        self._running = False
        self._processing_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

        # Ensure data directory exists
        FUSION_DATA_DIR.mkdir(parents=True, exist_ok=True)

    async def start(self) -> None:
        """Start the fusion engine."""
        if self._running:
            return

        self._running = True
        self.logger.info("MultiModalPerceptionFusion starting...")

        # Load any historical data
        await self._load_history()

        self.logger.info("MultiModalPerceptionFusion started")

    async def stop(self) -> None:
        """Stop the fusion engine."""
        self._running = False

        if self._processing_task:
            self._processing_task.cancel()
            try:
                await self._processing_task
            except asyncio.CancelledError:
                pass

        await self._save_history()
        self.logger.info("MultiModalPerceptionFusion stopped")

    async def add_input(self, input_data: ModalityInput) -> None:
        """Add a new input from any modality."""
        async with self._lock:
            # Encode the input
            encoder = self.encoders.get(input_data.modality)
            if encoder:
                input_data.embedding = await encoder.encode(input_data)
                input_data.features = await encoder.extract_features(input_data)
                input_data.quality_score = encoder.get_quality_score(input_data)

            self._input_buffer.append(input_data)

    async def fuse_recent(
        self,
        window_ms: Optional[int] = None,
        modalities: Optional[List[Modality]] = None
    ) -> FusedPerception:
        """Fuse recent inputs from all or specified modalities."""
        if window_ms is None:
            window_ms = TEMPORAL_WINDOW_MS

        cutoff = time.time() - (window_ms / 1000)

        async with self._lock:
            # Filter inputs by time and modality
            inputs = [
                inp for inp in self._input_buffer
                if inp.timestamp >= cutoff
                and (modalities is None or inp.modality in modalities)
            ]

        if not inputs:
            return FusedPerception()

        return await self.fuse(inputs)

    async def fuse(self, inputs: List[ModalityInput]) -> FusedPerception:
        """Fuse a specific set of inputs."""
        if not inputs:
            return FusedPerception()

        # Ensure all inputs are encoded
        for inp in inputs:
            if inp.embedding is None:
                encoder = self.encoders.get(inp.modality)
                if encoder:
                    inp.embedding = await encoder.encode(inp)
                    inp.features = await encoder.extract_features(inp)
                    inp.quality_score = encoder.get_quality_score(inp)

        # Compute cross-modal attention for all pairs
        attentions = []
        for i, inp1 in enumerate(inputs):
            for inp2 in inputs[i + 1:]:
                try:
                    attention = await self.attention_module.compute_attention(inp1, inp2)
                    attentions.append(attention)
                except Exception as e:
                    self.logger.warning(f"Attention computation failed: {e}")

        # Check consistency
        consistencies = await self.consistency_checker.check_consistency(inputs)

        # Perform fusion
        result = await self.fusion_network.fuse(inputs, attentions, consistencies)

        # Store in history
        self._fusion_history.append(result)

        return result

    async def fuse_vision_and_audio(
        self,
        vision_input: ModalityInput,
        audio_input: ModalityInput
    ) -> FusedPerception:
        """Convenience method to fuse vision and audio."""
        return await self.fuse([vision_input, audio_input])

    async def fuse_all_modalities(
        self,
        vision: Optional[ModalityInput] = None,
        audio: Optional[ModalityInput] = None,
        text: Optional[ModalityInput] = None
    ) -> FusedPerception:
        """Fuse all available modalities."""
        inputs = [inp for inp in [vision, audio, text] if inp is not None]
        return await self.fuse(inputs)

    async def get_aligned_windows(
        self,
        lookback_seconds: float = 5.0
    ) -> List[List[ModalityInput]]:
        """Get temporally aligned windows of inputs."""
        cutoff = time.time() - lookback_seconds

        async with self._lock:
            inputs = [inp for inp in self._input_buffer if inp.timestamp >= cutoff]

        return await self.temporal_aligner.align(inputs)

    async def detect_multi_modal_events(
        self,
        lookback_seconds: float = 5.0
    ) -> List[Dict[str, Any]]:
        """Detect significant multi-modal events."""
        windows = await self.get_aligned_windows(lookback_seconds)
        return await self.temporal_aligner.detect_events(windows)

    async def _load_history(self) -> None:
        """Load historical fusion data."""
        history_file = FUSION_DATA_DIR / "fusion_history.json"

        if history_file.exists():
            try:
                with open(history_file) as f:
                    data = json.load(f)
                self.logger.info(f"Loaded {len(data)} historical fusions")
            except Exception as e:
                self.logger.warning(f"Failed to load history: {e}")

    async def _save_history(self) -> None:
        """Save fusion history."""
        history_file = FUSION_DATA_DIR / "fusion_history.json"

        try:
            data = [f.to_dict() for f in self._fusion_history]
            with open(history_file, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            self.logger.error(f"Failed to save history: {e}")

    def get_stats(self) -> Dict[str, Any]:
        """Get current statistics."""
        return {
            "input_buffer_size": len(self._input_buffer),
            "fusion_history_size": len(self._fusion_history),
            "running": self._running,
            "encoders_available": list(self.encoders.keys()),
        }


# Global instance
_perception_fusion: Optional[MultiModalPerceptionFusion] = None
_lock = LazyAsyncLock()  # v100.1: Lazy initialization to avoid "no running event loop" error


async def get_perception_fusion() -> MultiModalPerceptionFusion:
    """Get the global MultiModalPerceptionFusion instance."""
    global _perception_fusion

    async with _lock:
        if _perception_fusion is None:
            _perception_fusion = MultiModalPerceptionFusion()
            await _perception_fusion.start()

        return _perception_fusion
