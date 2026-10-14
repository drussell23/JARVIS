"""
STT Engine Implementations
Each engine is self-contained and implements the base interface
"""

import importlib

from .base_engine import BaseSTTEngine, STTResult

# Each STT engine pulls a heavy optional stack (torch, vosk, whisper). Loaded
# when named, not when the package is: importing ANY module here -- the TTS
# engine included -- used to require torch, so a host without it had no
# UnifiedTTSEngine at all.
_LAZY_ENGINES = {
    "VoskEngine": ".vosk_engine",
    "Wav2VecEngine": ".wav2vec_engine",
    "WhisperGCPEngine": ".whisper_gcp_engine",
    "WhisperLocalEngine": ".whisper_local_engine",
}


def __getattr__(name):
    module = _LAZY_ENGINES.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(module, __name__), name)
    globals()[name] = value
    return value

__all__ = [
    "BaseSTTEngine",
    "STTResult",
    "VoskEngine",
    "Wav2VecEngine",
    "WhisperLocalEngine",
    "WhisperGCPEngine",
    "get_unified_tts_engine",
    "get_tts_engine",
    # R&D — Voice.ai WebSocket streaming provider (sandbox only)
    "VoiceAIProvider",
    "build_voiceai_provider",
]


def get_unified_tts_engine():
    """Lazy re-export — avoid loading TTS at import time."""
    from .unified_tts_engine import get_unified_tts_engine as _get
    return _get()


def get_tts_engine():
    """Alias for get_unified_tts_engine."""
    return get_unified_tts_engine()


def VoiceAIProvider():
    """Lazy re-export — avoids loading aiohttp at import time."""
    from .voiceai_tts_engine import VoiceAIProvider as _V
    return _V


def build_voiceai_provider():
    """Lazy re-export — construct a VoiceAIProvider from environment."""
    from .voiceai_tts_engine import build_voiceai_provider as _b
    return _b()
