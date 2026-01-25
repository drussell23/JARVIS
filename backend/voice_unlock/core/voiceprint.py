"""
Voiceprint Core Module
======================

Defines data structures and utilities for voice biometric profiles.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Union
import json
import time
import numpy as np

@dataclass
class VoiceFeatures:
    """Acoustic features extracted from voice audio."""
    pitch: float = 0.0
    jitter: float = 0.0
    shimmer: float = 0.0
    hnr: float = 0.0
    formants: List[float] = field(default_factory=list)
    mfcc: List[float] = field(default_factory=list)
    spectral_centroid: float = 0.0
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "pitch": self.pitch,
            "jitter": self.jitter,
            "shimmer": self.shimmer,
            "hnr": self.hnr,
            "formants": self.formants,
            "mfcc": self.mfcc,
            "spectral_centroid": self.spectral_centroid
        }
        
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'VoiceFeatures':
        return cls(
            pitch=data.get("pitch", 0.0),
            jitter=data.get("jitter", 0.0),
            shimmer=data.get("shimmer", 0.0),
            hnr=data.get("hnr", 0.0),
            formants=data.get("formants", []),
            mfcc=data.get("mfcc", []),
            spectral_centroid=data.get("spectral_centroid", 0.0)
        )

@dataclass
class Voiceprint:
    """
    Represents a speaker's voice biometric profile.
    
    Includes:
    - Deep learning embeddings (ECAPA-TDNN)
    - Acoustic features
    - Metadata
    - Enrollment history
    """
    speaker_id: str
    speaker_name: str
    embedding: List[float]
    features: Optional[VoiceFeatures] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    sample_count: int = 1
    confidence: float = 1.0
    is_primary_user: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "speaker_id": self.speaker_id,
            "speaker_name": self.speaker_name,
            "embedding": self.embedding,
            "features": self.features.to_dict() if self.features else None,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "sample_count": self.sample_count,
            "confidence": self.confidence,
            "is_primary_user": self.is_primary_user,
            "metadata": self.metadata
        }
        
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Voiceprint':
        features = None
        if data.get("features"):
            features = VoiceFeatures.from_dict(data["features"])
            
        return cls(
            speaker_id=data.get("speaker_id", ""),
            speaker_name=data.get("speaker_name", "Unknown"),
            embedding=data.get("embedding", []),
            features=features,
            created_at=data.get("created_at", time.time()),
            updated_at=data.get("updated_at", time.time()),
            sample_count=data.get("sample_count", 1),
            confidence=data.get("confidence", 1.0),
            is_primary_user=data.get("is_primary_user", False),
            metadata=data.get("metadata", {})
        )
        
    def similarity(self, other_embedding: List[float]) -> float:
        """Calculate cosine similarity with another embedding."""
        if not self.embedding or not other_embedding:
            return 0.0
            
        vec1 = np.array(self.embedding)
        vec2 = np.array(other_embedding)
        
        norm1 = np.linalg.norm(vec1)
        norm2 = np.linalg.norm(vec2)
        
        if norm1 == 0 or norm2 == 0:
            return 0.0
            
        return float(np.dot(vec1, vec2) / (norm1 * norm2))
