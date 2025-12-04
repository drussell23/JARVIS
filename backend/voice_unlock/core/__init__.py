"""
Voice Unlock Core Module v2.5

Contains physics-aware feature extraction, anti-spoofing, and Bayesian fusion.

Components:
- PhysicsAwareFeatureExtractor: VTL, RT60, Doppler analysis
- AntiSpoofingDetector: 7-layer spoof detection with physics
- BayesianConfidenceFusion: Multi-factor probability fusion

Usage:
    from backend.voice_unlock.core import (
        get_physics_feature_extractor,
        get_anti_spoofing_detector,
        get_bayesian_fusion
    )

    # Initialize components
    extractor = get_physics_feature_extractor()
    detector = get_anti_spoofing_detector()
    fusion = get_bayesian_fusion()
"""

# Feature extraction
from .feature_extraction import (
    VoiceFeatureExtractor,
    VoiceFeatures,
    PhysicsAwareFeatureExtractor,
    PhysicsAwareFeatures,
    PhysicsConfidenceLevel,
    get_physics_feature_extractor
)

# Anti-spoofing
from .anti_spoofing import (
    AntiSpoofingDetector,
    AntiSpoofingConfig,
    SpoofingResult,
    SpoofType,
    AudioCharacteristics,
    get_anti_spoofing_detector
)

# Bayesian fusion
from .bayesian_fusion import (
    BayesianConfidenceFusion,
    BayesianFusionConfig,
    FusionResult,
    DecisionType,
    EvidenceScore,
    get_bayesian_fusion
)

__all__ = [
    # Feature extraction
    'VoiceFeatureExtractor',
    'VoiceFeatures',
    'PhysicsAwareFeatureExtractor',
    'PhysicsAwareFeatures',
    'PhysicsConfidenceLevel',
    'get_physics_feature_extractor',
    # Anti-spoofing
    'AntiSpoofingDetector',
    'AntiSpoofingConfig',
    'SpoofingResult',
    'SpoofType',
    'AudioCharacteristics',
    'get_anti_spoofing_detector',
    # Bayesian fusion
    'BayesianConfidenceFusion',
    'BayesianFusionConfig',
    'FusionResult',
    'DecisionType',
    'EvidenceScore',
    'get_bayesian_fusion'
]

__version__ = "2.5.0"
