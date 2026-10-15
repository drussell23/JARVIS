#!/usr/bin/env python3
"""
VAD (Voice Activity Detection) Module

Provides multi-stage VAD pipeline for filtering silence and noise
before audio reaches Whisper transcription.

Components:
- base.py: Abstract base class and common utilities
- webrtc_vad.py: Fast WebRTC-based frame-level VAD
- silero_vad.py: Neural network-based VAD for accuracy
- pipeline.py: Orchestrates WebRTC + Silero for optimal results
"""

from .base import VADBase, VADConfig, SpeechSegment
from .webrtc_vad import WebRTCVAD
from .silero_vad import SileroVAD, AsyncSileroVAD
from .pipeline import VADPipeline, VADPipelineConfig, get_vad_pipeline, get_vad_pipeline_async

__all__ = [
    # Base classes
    "VADBase",
    "VADConfig",
    "SpeechSegment",

    # VAD implementations
    "WebRTCVAD",
    "SileroVAD",
    "AsyncSileroVAD",

    # Pipeline
    "VADPipeline",
    "VADPipelineConfig",
    "get_vad_pipeline",
    "get_vad_pipeline_async",
]
