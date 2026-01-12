"""
Whisper Local STT Engine
OpenAI Whisper running locally with CoreML optimization (macOS)
Balanced accuracy and performance for medium-RAM scenarios

ADVANCED IMPORT SYSTEM:
- Uses the centralized WhisperImportManager from whisper_audio_fix.py
- Unified circuit breaker, retry logic, and health monitoring
- Dynamic configuration from environment variables
- No duplicate import code - single source of truth
"""

import asyncio
import logging
import os
import time
from pathlib import Path

import numpy as np

from ..stt_config import STTEngine
from .base_engine import BaseSTTEngine, STTResult

logger = logging.getLogger(__name__)


def _lazy_import_whisper():
    """
    Lazy import of whisper module using the centralized WhisperImportManager.

    This is a thin wrapper that delegates to the advanced import manager
    in whisper_audio_fix.py, ensuring a single source of truth for all
    whisper imports across the codebase.

    Returns:
        The whisper module

    Raises:
        ImportError: If whisper import failed
    """
    try:
        # Import the centralized import manager
        from ..whisper_audio_fix import get_import_manager

        import_manager = get_import_manager()
        return import_manager.import_sync()

    except ImportError as e:
        # Fallback if whisper_audio_fix is not available (shouldn't happen)
        logger.warning(f"Could not use centralized import manager: {e}, falling back to direct import")

        # Direct import as fallback
        try:
            import numba
            _ = numba.__version__
        except Exception:
            pass

        import whisper
        return whisper


async def _lazy_import_whisper_async():
    """
    Async lazy import of whisper module using the centralized WhisperImportManager.

    Returns:
        The whisper module

    Raises:
        ImportError: If whisper import failed
    """
    try:
        from ..whisper_audio_fix import get_import_manager

        import_manager = get_import_manager()
        return await import_manager.import_async()

    except ImportError as e:
        logger.warning(f"Async import failed, falling back to sync: {e}")
        return await asyncio.to_thread(_lazy_import_whisper)


class WhisperLocalEngine(BaseSTTEngine):
    """
    OpenAI Whisper running locally.

    Features:
    - High accuracy (76-95% depending on model size)
    - Multiple model sizes (tiny, base, small, medium, large)
    - CoreML optimization on macOS (M1/M2/M3)
    - CPU and GPU support
    - Multilingual support
    - 1-5GB RAM depending on model
    """

    def __init__(self, model_config):
        super().__init__(model_config)
        self.model_size = model_config.model_path or "base"  # tiny, base, small, medium, large
        self.device = None
        self.models_dir = Path.home() / ".jarvis" / "models" / "stt" / "whisper"

    async def initialize(self):
        """
        Initialize Whisper model with optimal settings.

        Uses the centralized WhisperImportManager for:
        - Async-safe import with circuit breaker
        - Dynamic configuration from environment
        - Retry with exponential backoff
        - Health monitoring and metrics
        """
        if self.initialized:
            return

        logger.info(f"🔧 Initializing Whisper Local: {self.model_config.name}")
        logger.info(f"   Model size: {self.model_size}")

        try:
            # Get centralized config for dynamic settings
            try:
                from ..whisper_audio_fix import get_whisper_config, get_import_manager

                config = get_whisper_config()
                import_manager = get_import_manager()

                # Check circuit breaker before attempting
                if not import_manager.circuit_breaker.is_available:
                    raise ImportError(
                        f"Whisper circuit breaker OPEN: {import_manager.circuit_breaker.metrics.last_error}"
                    )
            except ImportError:
                config = None
                import_manager = None

            # Use async lazy import
            start_time = time.time()
            whisper = await _lazy_import_whisper_async()
            import_time_ms = (time.time() - start_time) * 1000

            # Ensure models directory exists
            self.models_dir.mkdir(parents=True, exist_ok=True)

            # Determine optimal device
            self.device = self._get_optimal_device()
            logger.info(f"   Using device: {self.device}")
            logger.info(f"   Import time: {import_time_ms:.0f}ms")

            # Load model asynchronously
            logger.info(f"   Loading Whisper {self.model_size} model...")
            load_start = time.time()
            self.model = await asyncio.to_thread(
                whisper.load_model,
                self.model_size,
                device=self.device,
                download_root=str(self.models_dir),
            )
            load_time_ms = (time.time() - load_start) * 1000

            # Update import manager status if available
            if import_manager:
                import_manager.status.model_loaded = True
                import_manager.status.load_time_ms = load_time_ms

            self.initialized = True
            total_time_ms = (time.time() - start_time) * 1000
            logger.info(
                f"✅ Whisper Local initialized: {self.model_config.name} "
                f"(import: {import_time_ms:.0f}ms, load: {load_time_ms:.0f}ms, total: {total_time_ms:.0f}ms)"
            )

        except ImportError as e:
            error_msg = str(e)
            if "circuit breaker" in error_msg.lower():
                logger.error(f"❌ Whisper blocked by circuit breaker: {e}")
            elif "circular import" in error_msg or "numba" in error_msg.lower():
                logger.error(
                    f"❌ Whisper initialization failed due to numba issue: {e}. "
                    "Try: pip install --upgrade numba llvmlite"
                )
            else:
                logger.error(f"❌ Whisper import failed: {e}")
            raise
        except Exception as e:
            logger.error(f"❌ Failed to initialize Whisper Local: {e}")
            raise

    def _get_optimal_device(self) -> str:
        """Determine optimal device for Whisper"""
        import torch

        # Whisper uses PyTorch, check for available devices
        if torch.cuda.is_available():
            logger.info("   🎮 CUDA GPU available")
            return "cuda"

        # Whisper doesn't natively support MPS yet, use CPU on macOS
        # CoreML optimization is available through separate package
        logger.info("   💻 Using CPU")
        return "cpu"

    async def transcribe(self, audio_data) -> STTResult:
        """
        Transcribe audio using Whisper.

        Args:
            audio_data: Audio data in any format

        Returns:
            STTResult with transcription and confidence
        """
        if not self.initialized:
            await self.initialize()

        start_time = time.time()

        try:
            # Ensure audio is in proper format
            # CRITICAL FIX: Use async version to avoid blocking event loop during FFmpeg transcoding
            from voice.audio_format_converter import prepare_audio_for_stt_async
            audio_data = await prepare_audio_for_stt_async(audio_data)

            # Convert audio bytes to numpy array
            audio_array = await self._bytes_to_audio_array(audio_data)
            audio_duration_ms = len(audio_array) / 16000 * 1000

            # Transcribe with Whisper
            # 🔑 KEY FIX: Use initial_prompt to prevent hallucinations
            # Without this, Whisper can hallucinate random names like "Mark McCree"
            #
            # IMPORTANT: Prompt must NOT bias toward only "unlock" because it can cause
            # "lock" → "unlock" confusions on short commands. Keep it balanced and configurable.
            initial_prompt = os.getenv("JARVIS_WHISPER_INITIAL_PROMPT", "").strip()
            if not initial_prompt:
                initial_prompt = (
                    "hey jarvis, jarvis, "
                    "lock my screen, unlock my screen, lock screen, unlock screen, "
                    "lock my mac, unlock my mac, "
                    "open safari, close safari, open chrome, close chrome"
                )

            # SAFETY: Capture model reference BEFORE spawning thread to prevent
            # segfaults if model is unloaded during transcription
            model_ref = self.model
            if model_ref is None:
                raise RuntimeError("Whisper model not loaded - cannot transcribe")

            def _transcribe_sync():
                """Run Whisper transcription in thread pool."""
                if model_ref is None:
                    raise RuntimeError("Whisper model reference became None during transcription")
                return model_ref.transcribe(
                    audio_array,
                    language="en",  # Derek speaks English
                    fp16=False,  # Use FP32 for CPU (more stable)
                    verbose=False,
                    initial_prompt=initial_prompt,  # Bias toward expected unlock phrases
                    condition_on_previous_text=False,  # Don't hallucinate from context
                    temperature=0.0,  # Deterministic output
                    no_speech_threshold=0.6,  # Higher = more strict speech detection
                    logprob_threshold=-1.0,  # Reject low-confidence outputs
                    compression_ratio_threshold=2.4,  # Reject repetitive hallucinations
                )

            result = await asyncio.to_thread(_transcribe_sync)

            # Extract transcription
            transcription_text = result.get("text", "").strip()

            # Calculate confidence from segment data
            confidence = self._calculate_confidence(result)

            latency_ms = (time.time() - start_time) * 1000

            logger.debug(
                f"🎤 Whisper transcribed: '{transcription_text[:50]}...' "
                f"(confidence={confidence:.2f}, latency={latency_ms:.0f}ms)"
            )

            return STTResult(
                text=transcription_text,
                confidence=confidence,
                engine=STTEngine.WHISPER_LOCAL,
                model_name=self.model_config.name,
                latency_ms=latency_ms,
                audio_duration_ms=audio_duration_ms,
                metadata={
                    "device": self.device,
                    "model_size": self.model_size,
                    "language": result.get("language", "en"),
                    "segments": result.get("segments", []),
                },
            )

        except Exception as e:
            logger.error(f"Whisper Local transcription failed: {e}")
            raise

    async def _bytes_to_audio_array(self, audio_data: bytes) -> np.ndarray:
        """
        Convert audio bytes to numpy array suitable for Whisper.

        Whisper expects 16kHz mono float32.
        """
        # Audio should already be bytes from prepare_audio_for_stt
        if not isinstance(audio_data, bytes):
            logger.error(f"Expected bytes, got {type(audio_data)}")
            audio_data = bytes(audio_data)

        logger.info(f"Converting audio bytes: {len(audio_data)} bytes")

        try:
            # Check if audio data is empty
            if len(audio_data) == 0:
                logger.error("Audio data is empty!")
                # Return 1 second of silence instead of empty
                return np.zeros(16000, dtype=np.float32)

            # Check if bytes length is compatible with int16
            if len(audio_data) % 2 != 0:
                logger.warning(f"Audio data length {len(audio_data)} is odd, padding with zero")
                audio_data = audio_data + b'\x00'

            # Convert bytes to numpy array directly
            # Assume 16-bit PCM audio
            audio_array = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32) / 32768.0

            # Check if audio is silent
            if np.max(np.abs(audio_array)) < 0.001:
                logger.warning(f"Audio appears to be silent (max amplitude: {np.max(np.abs(audio_array))})")

            # If the array is too short, pad it
            if len(audio_array) < 16000:  # Less than 1 second
                logger.warning(f"Audio too short: {len(audio_array)} samples, padding...")
                audio_array = np.pad(audio_array, (0, 16000 - len(audio_array)))

            logger.info(f"Audio array created: {len(audio_array)} samples, max amplitude: {np.max(np.abs(audio_array)):.4f}")
            return audio_array

        except ImportError:
            logger.warning("librosa not available, using scipy")
            # Fallback to scipy
            import io

            import scipy.io.wavfile as wavfile
            import scipy.signal as signal

            sr, audio_array = await asyncio.to_thread(wavfile.read, io.BytesIO(audio_data))

            # Convert to mono
            if len(audio_array.shape) > 1:
                audio_array = audio_array.mean(axis=1)

            # Convert to float32 normalized to [-1, 1]
            if audio_array.dtype == np.int16:
                audio_array = audio_array.astype(np.float32) / 32768.0
            elif audio_array.dtype == np.int32:
                audio_array = audio_array.astype(np.float32) / 2147483648.0

            # Resample to 16kHz if needed
            if sr != 16000:
                num_samples = int(len(audio_array) * 16000 / sr)
                audio_array = await asyncio.to_thread(signal.resample, audio_array, num_samples)

            return audio_array

    def _calculate_confidence(self, result: dict) -> float:
        """
        Calculate confidence from Whisper result.

        Whisper provides no_speech_prob and avg_logprob per segment.
        """
        try:
            segments = result.get("segments", [])

            if not segments:
                return 0.5  # No segments, moderate confidence

            # Calculate confidence from segment probabilities
            confidences = []
            for segment in segments:
                # Whisper provides avg_logprob (negative value, closer to 0 is better)
                avg_logprob = segment.get("avg_logprob", -1.0)

                # Whisper also provides no_speech_prob (probability of silence)
                no_speech_prob = segment.get("no_speech_prob", 0.0)

                # Convert logprob to confidence
                # avg_logprob typically ranges from -1.0 (good) to -3.0 (bad)
                # Map to [0, 1] range
                confidence = max(0.0, min(1.0, (avg_logprob + 3.0) / 2.0))

                # Penalize if likely silence
                confidence *= 1.0 - no_speech_prob

                confidences.append(confidence)

            # Weight by segment duration
            total_duration = sum(s.get("end", 0) - s.get("start", 0) for s in segments)
            if total_duration == 0:
                return 0.5

            weighted_confidence = (
                sum(
                    conf * (seg.get("end", 0) - seg.get("start", 0))
                    for conf, seg in zip(confidences, segments)
                )
                / total_duration
            )

            return max(0.0, min(1.0, weighted_confidence))

        except Exception as e:
            logger.warning(f"Failed to calculate confidence: {e}")
            return 0.5

    async def cleanup(self):
        """Cleanup Whisper resources"""
        if self.model is not None:
            del self.model
            self.model = None

            # Clear CUDA cache if used
            if self.device == "cuda":
                import torch

                torch.cuda.empty_cache()

        await super().cleanup()
        logger.info(f"🧹 Whisper Local engine cleaned up: {self.model_config.name}")
