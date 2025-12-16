#!/usr/bin/env python3
"""
Whisper STT Override - Forces JARVIS to use Whisper for transcription

NUMBA CIRCULAR IMPORT FIX:
- Uses lazy import to avoid numba.core.utils circular import errors
- Defers whisper import until first use
"""

import asyncio
import numpy as np
import tempfile
import soundfile as sf
from pathlib import Path

# CRITICAL: DO NOT import whisper at module level!
# whisper -> torch -> numba can trigger circular import errors
# Use lazy import in initialize() instead
_whisper_module = None


def _lazy_import_whisper():
    """
    Lazy import of whisper module to avoid circular import issues with numba.
    """
    global _whisper_module
    if _whisper_module is None:
        try:
            # Pre-import numba to avoid circular import
            try:
                import numba
                _ = numba.__version__
            except ImportError:
                pass
            except Exception:
                pass

            import whisper as whisper_mod
            _whisper_module = whisper_mod
        except Exception as e:
            print(f"❌ Failed to import whisper: {e}")
            raise
    return _whisper_module


class WhisperSTTOverride:
    """Direct Whisper implementation to bypass routing issues"""

    def __init__(self, model_size="base"):
        self.model = None
        self.model_size = model_size

    def initialize(self):
        """Load Whisper model using lazy import"""
        if self.model is None:
            print(f"Loading Whisper {self.model_size} model...")
            whisper = _lazy_import_whisper()
            self.model = whisper.load_model(self.model_size)
            print("✅ Whisper model loaded")

    async def transcribe(self, audio_data):
        """Transcribe audio using Whisper"""
        self.initialize()

        # Save audio to temp file
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            # Convert bytes to numpy array if needed
            if isinstance(audio_data, bytes):
                audio_array = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32) / 32768.0
            else:
                audio_array = audio_data

            # Save as WAV
            sf.write(tmp.name, audio_array, 16000)

            # Transcribe
            result = self.model.transcribe(tmp.name)

            # Clean up
            Path(tmp.name).unlink()

            return result["text"]

# Global instance
_whisper_override = WhisperSTTOverride()

def get_whisper_stt():
    """Get global Whisper STT instance"""
    return _whisper_override

# Monkey-patch the hybrid router to use Whisper
def patch_jarvis_stt():
    """Patch JARVIS to use Whisper for all STT"""
    try:
        # Import the hybrid router
        from backend.voice import hybrid_stt_router

        # Override the transcribe method
        original_transcribe = hybrid_stt_router.HybridSTTRouter.transcribe

        async def whisper_transcribe(self, audio_data, **kwargs):
            """Override transcribe to use Whisper"""
            try:
                text = await _whisper_override.transcribe(audio_data)

                # Create result object
                from backend.voice.stt_config import STTEngine
                from backend.voice.hybrid_stt_router import STTResult

                result = STTResult(
                    text=text,
                    confidence=0.95,  # Whisper doesn't provide confidence
                    engine=STTEngine.WHISPER_LOCAL,
                    model_name="whisper-base",
                    latency_ms=250,
                    audio_duration_ms=3000
                )

                print(f"✅ Whisper transcribed: '{text}'")
                return result

            except Exception as e:
                print(f"❌ Whisper error: {e}")
                # Fall back to original
                return await original_transcribe(self, audio_data, **kwargs)

        # Apply patch
        hybrid_stt_router.HybridSTTRouter.transcribe = whisper_transcribe
        print("✅ JARVIS patched to use Whisper STT")

    except Exception as e:
        print(f"Failed to patch: {e}")

if __name__ == "__main__":
    # Test Whisper
    print("Testing Whisper STT...")
    _whisper_override.initialize()
    print("Whisper ready for transcription")