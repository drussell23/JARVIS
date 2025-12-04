"""
Voice Unlock Core Module
Contains feature extraction, anti-spoofing, and core utilities
"""

from .feature_extraction import VoiceFeatureExtractor, VoiceFeatures
from .anti_spoofing import AntiSpoofingDetector, SpoofingResult, SpoofType

__all__ = [
    'VoiceFeatureExtractor',
    'VoiceFeatures',
    'AntiSpoofingDetector',
    'SpoofingResult',
    'SpoofType'
]
