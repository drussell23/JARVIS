"""
STT Engine Implementations
Each engine is self-contained and implements the base interface
"""

from .base_engine import BaseSTTEngine, STTResult
from .vosk_engine import VoskEngine
from .wav2vec_engine import Wav2VecEngine
from .whisper_gcp_engine import WhisperGCPEngine
from .whisper_local_engine import WhisperLocalEngine

__all__ = [
    "BaseSTTEngine",
    "STTResult",
    "VoskEngine",
    "Wav2VecEngine",
    "WhisperLocalEngine",
    "WhisperGCPEngine",
    "get_unified_tts_engine",
    "get_tts_engine",
]


def get_unified_tts_engine():
    """Lazy re-export — avoid loading TTS at import time."""
    from .unified_tts_engine import get_unified_tts_engine as _get
    return _get()


def get_tts_engine():
    """Alias for get_unified_tts_engine."""
    return get_unified_tts_engine()
