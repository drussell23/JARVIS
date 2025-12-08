#!/usr/bin/env python3
"""
Robust Whisper audio handler that works with any input format

INTEGRATED FEATURES:
- VAD (Voice Activity Detection) filtering via WebRTC-VAD + Silero VAD
- Audio windowing/truncation to prevent hallucinations (5s global, 2s unlock)
- Silence and noise removal BEFORE Whisper sees audio
- Ultra-low latency for command and unlock flows
"""

import base64
import numpy as np
import whisper
import tempfile
import logging
import asyncio
import soundfile as sf
import io
import os
import threading
from concurrent.futures import ThreadPoolExecutor

try:
    import librosa
    LIBROSA_AVAILABLE = True
except ImportError:
    LIBROSA_AVAILABLE = False
    logging.warning("librosa not available - will use basic resampling")

# VAD and windowing imports
try:
    from .vad import get_vad_pipeline, VADConfig, VADPipelineConfig
    from .audio_windowing import get_window_manager, WindowConfig
    VAD_AVAILABLE = True
except ImportError:
    VAD_AVAILABLE = False
    logging.warning("VAD/windowing not available - will skip filtering")

logger = logging.getLogger(__name__)

class WhisperAudioHandler:
    """
    Handles any audio format for Whisper transcription

    NOW WITH:
    - VAD filtering (WebRTC-VAD + Silero VAD)
    - Audio windowing (5s global, 2s unlock)
    - Silence removal before transcription
    - Non-blocking async model loading with thread pool
    """

    # Constants for async loading
    MODEL_LOAD_TIMEOUT = 120.0  # 2 minutes timeout for model loading

    def __init__(self):
        self.model = None
        self._model_loading = False
        self._model_load_lock = threading.Lock()
        self._async_model_load_lock = asyncio.Lock() if asyncio.get_event_loop().is_running() else None
        self._model_load_event = threading.Event()

        # Thread pool for CPU-bound model loading (avoid blocking event loop)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="whisper_loader")

        # Initialize VAD pipeline (lazy-loaded)
        self.vad_pipeline = None
        self.vad_enabled = VAD_AVAILABLE and os.getenv('ENABLE_VAD', 'true').lower() == 'true'

        # Initialize window manager (lazy-loaded)
        self.window_manager = None
        self.windowing_enabled = VAD_AVAILABLE and os.getenv('ENABLE_WINDOWING', 'true').lower() == 'true'

        logger.info("🎤 Whisper Audio Handler initialized")
        logger.info(f"   VAD enabled: {self.vad_enabled}")
        logger.info(f"   Windowing enabled: {self.windowing_enabled}")

    def _infer_sample_rate(self, audio_bytes: bytes, num_samples: int) -> int:
        """
        Intelligently infer sample rate from audio characteristics.

        Uses multiple heuristics:
        1. Audio duration estimation (if we know expected duration)
        2. Frequency content analysis
        3. Common sample rates for different sources

        Args:
            audio_bytes: Raw audio data
            num_samples: Number of audio samples detected

        Returns:
            Inferred sample rate in Hz
        """
        # Common sample rates to test
        common_rates = [48000, 44100, 32000, 24000, 16000, 22050, 11025, 8000]

        # Heuristic 1: Audio byte size can hint at sample rate
        # Typical voice command is 2-5 seconds
        # For int16 PCM: bytes = samples * 2
        audio_duration_estimates = {}
        for rate in common_rates:
            estimated_duration = num_samples / rate
            # Voice commands typically 1-10 seconds
            if 1.0 <= estimated_duration <= 10.0:
                audio_duration_estimates[rate] = estimated_duration
                logger.debug(f"Sample rate {rate}Hz → {estimated_duration:.2f}s duration")

        # Heuristic 2: Most likely rates based on source
        # Browser MediaRecorder: typically 48kHz or 44.1kHz
        # macOS: 48kHz or 44.1kHz
        # Mobile: 44.1kHz or 48kHz
        # Old hardware: 22.05kHz, 16kHz, 11.025kHz

        if audio_duration_estimates:
            # Choose rate that gives most reasonable duration (2-5 sec preference)
            best_rate = min(audio_duration_estimates.keys(),
                          key=lambda r: abs(audio_duration_estimates[r] - 3.0))
            logger.info(f"🎯 Inferred sample rate: {best_rate}Hz (duration: {audio_duration_estimates[best_rate]:.2f}s)")
            return best_rate

        # Fallback: Use most common browser rate
        logger.warning(f"⚠️ Could not infer sample rate, defaulting to 48000Hz (browser standard)")
        return 48000

    async def normalize_audio(self, audio_bytes: bytes, sample_rate: int = None) -> np.ndarray:
        """
        Universal audio normalization pipeline that:
        1. Auto-detects sample rate from audio bytes OR uses provided rate
        2. Decodes audio format (int16/float32/int8 PCM)
        3. Resamples to 16kHz if needed
        4. Converts stereo to mono
        5. Normalizes to float32 [-1.0, 1.0]

        Args:
            audio_bytes: Raw audio data
            sample_rate: Optional sample rate from frontend (browser-reported)
                        If None, will attempt to infer from audio

        Returns: Normalized float32 numpy array ready for Whisper
        """
        def _normalize_sync():
            logger.info(f"🔊 Audio normalization: {len(audio_bytes)} bytes")

            # Step 1: Try to detect format and decode
            audio_array = None
            detected_sr = None
            detected_format = None

            # Try pydub first (handles WebM/Opus, MP3, MP4, etc.)
            try:
                from pydub import AudioSegment
                import tempfile
                import os

                # Write to temp file for pydub
                with tempfile.NamedTemporaryFile(suffix='.webm', delete=False) as tmp:
                    tmp.write(audio_bytes)
                    tmp_path = tmp.name

                # Load with pydub (uses ffmpeg under the hood)
                audio = AudioSegment.from_file(tmp_path)
                os.unlink(tmp_path)

                # Convert to numpy array
                audio_array = np.array(audio.get_array_of_samples(), dtype=np.float32)

                # Normalize to [-1.0, 1.0]
                if audio.sample_width == 1:  # 8-bit
                    audio_array = audio_array / 128.0
                elif audio.sample_width == 2:  # 16-bit
                    audio_array = audio_array / 32768.0
                elif audio.sample_width == 4:  # 32-bit
                    audio_array = audio_array / 2147483648.0

                # Handle stereo
                if audio.channels == 2:
                    audio_array = audio_array.reshape((-1, 2)).mean(axis=1)

                detected_sr = audio.frame_rate
                detected_format = "pydub (WebM/Opus/MP3/etc)"
                logger.info(f"✅ Decoded with pydub: {detected_sr}Hz, {len(audio_array)} samples, {audio.channels} channels")
            except Exception as e:
                logger.debug(f"pydub decode failed: {e}")

            # Try soundfile second (handles WAV, FLAC, OGG with embedded metadata)
            if audio_array is None:
                try:
                    audio_buf = io.BytesIO(audio_bytes)
                    audio_array, detected_sr = sf.read(audio_buf, dtype='float32')
                    detected_format = "soundfile (with metadata)"
                    logger.info(f"✅ Decoded with soundfile: {detected_sr}Hz, {audio_array.shape}")
                except Exception as e:
                    logger.debug(f"soundfile decode failed: {e}")

            # Step 2: If soundfile fails, try raw PCM formats
            if audio_array is None:
                # Try int16 PCM (most common from browsers)
                try:
                    audio_array = np.frombuffer(audio_bytes, dtype=np.int16)
                    if len(audio_array) > 100:
                        audio_array = audio_array.astype(np.float32) / 32768.0
                        detected_format = "int16 PCM"
                        # Use provided sample rate or infer it
                        if sample_rate:
                            detected_sr = sample_rate
                            logger.info(f"✅ Decoded as int16 PCM: {len(audio_array)} samples, using provided {detected_sr}Hz")
                        else:
                            detected_sr = self._infer_sample_rate(audio_bytes, len(audio_array))
                            logger.info(f"✅ Decoded as int16 PCM: {len(audio_array)} samples, inferred {detected_sr}Hz")
                    else:
                        audio_array = None
                except Exception as e:
                    logger.debug(f"int16 PCM decode failed: {e}")

            # Try float32 PCM
            if audio_array is None:
                try:
                    audio_array = np.frombuffer(audio_bytes, dtype=np.float32)
                    if len(audio_array) > 100:
                        detected_format = "float32 PCM"
                        # Use provided sample rate or infer it
                        if sample_rate:
                            detected_sr = sample_rate
                            logger.info(f"✅ Decoded as float32 PCM: {len(audio_array)} samples, using provided {detected_sr}Hz")
                        else:
                            detected_sr = self._infer_sample_rate(audio_bytes, len(audio_array))
                            logger.info(f"✅ Decoded as float32 PCM: {len(audio_array)} samples, inferred {detected_sr}Hz")
                    else:
                        audio_array = None
                except Exception as e:
                    logger.debug(f"float32 PCM decode failed: {e}")

            # Try int8 PCM
            if audio_array is None:
                try:
                    audio_array = np.frombuffer(audio_bytes, dtype=np.int8)
                    if len(audio_array) > 100:
                        audio_array = audio_array.astype(np.float32) / 128.0
                        detected_format = "int8 PCM"
                        # Use provided sample rate or infer it
                        if sample_rate:
                            detected_sr = sample_rate
                            logger.info(f"✅ Decoded as int8 PCM: {len(audio_array)} samples, using provided {detected_sr}Hz")
                        else:
                            detected_sr = self._infer_sample_rate(audio_bytes, len(audio_array))
                            logger.info(f"✅ Decoded as int8 PCM: {len(audio_array)} samples, inferred {detected_sr}Hz")
                    else:
                        audio_array = None
                except Exception as e:
                    logger.debug(f"int8 PCM decode failed: {e}")

            if audio_array is None:
                logger.error("❌ Could not decode audio in any known format")
                raise ValueError("Audio format not recognized")

            # Step 3: Convert stereo to mono if needed
            if len(audio_array.shape) > 1 and audio_array.shape[1] > 1:
                logger.info(f"Converting stereo ({audio_array.shape[1]} channels) to mono")
                audio_array = np.mean(audio_array, axis=1)

            # Step 4: Validate audio has content
            audio_energy = np.abs(audio_array).mean()
            if audio_energy < 0.001:
                logger.error(f"❌ Audio is silence (energy: {audio_energy:.6f})")
                raise ValueError("Audio contains only silence")

            logger.info(f"✅ Audio energy: {audio_energy:.6f}")

            # Step 5: Resample to 16kHz if needed
            TARGET_SR = 16000
            if detected_sr != TARGET_SR:
                logger.info(f"🔄 Resampling from {detected_sr}Hz to {TARGET_SR}Hz...")

                if LIBROSA_AVAILABLE:
                    # High-quality resampling with librosa
                    audio_array = librosa.resample(
                        audio_array,
                        orig_sr=detected_sr,
                        target_sr=TARGET_SR,
                        res_type='kaiser_best'  # Highest quality
                    )
                    logger.info(f"✅ Resampled with librosa (kaiser_best): {len(audio_array)} samples")
                else:
                    # Fallback: Basic linear interpolation
                    from scipy import signal
                    num_samples = int(len(audio_array) * TARGET_SR / detected_sr)
                    audio_array = signal.resample(audio_array, num_samples)
                    logger.info(f"✅ Resampled with scipy: {len(audio_array)} samples")
            else:
                logger.info(f"✅ Already at {TARGET_SR}Hz - no resampling needed")

            # Step 6: Ensure float32 normalization
            audio_array = audio_array.astype(np.float32)

            # Clip to [-1.0, 1.0] range
            if np.abs(audio_array).max() > 1.0:
                logger.warning(f"Audio exceeded [-1.0, 1.0] range, clipping...")
                audio_array = np.clip(audio_array, -1.0, 1.0)

            logger.info(f"✅ Normalization complete: {len(audio_array)} samples @ 16kHz, float32")
            return audio_array

        # Run in thread pool to avoid blocking event loop
        return await asyncio.to_thread(_normalize_sync)

    def load_model(self):
        """
        Load Whisper model synchronously (for backward compatibility).

        WARNING: This is a blocking call. In async contexts, use load_model_async() instead.
        """
        if self.model is not None:
            return self.model

        with self._model_load_lock:
            # Double-check after acquiring lock
            if self.model is not None:
                return self.model

            logger.info("Loading Whisper model (synchronous)...")
            self.model = whisper.load_model("base")
            logger.info("Whisper model loaded")

        return self.model

    async def load_model_async(self, timeout: float = None) -> bool:
        """
        Load Whisper model asynchronously without blocking the event loop.

        Uses a thread pool executor to offload the CPU-bound model loading
        to a background thread, keeping the event loop responsive.

        Args:
            timeout: Maximum time to wait for model loading (default: MODEL_LOAD_TIMEOUT)

        Returns:
            True if model loaded successfully, False otherwise
        """
        if self.model is not None:
            return True

        timeout = timeout or self.MODEL_LOAD_TIMEOUT

        # Ensure async lock exists (lazy initialization for when event loop wasn't running at init)
        if self._async_model_load_lock is None:
            self._async_model_load_lock = asyncio.Lock()

        async with self._async_model_load_lock:
            # Double-check after acquiring async lock
            if self.model is not None:
                return True

            # Check if another thread is already loading
            if self._model_loading:
                logger.info("Whisper model already being loaded, waiting...")
                # Wait for the loading to complete with timeout
                try:
                    await asyncio.wait_for(
                        asyncio.get_event_loop().run_in_executor(
                            None,
                            lambda: self._model_load_event.wait(timeout)
                        ),
                        timeout=timeout
                    )
                    return self.model is not None
                except asyncio.TimeoutError:
                    logger.error(f"Timeout waiting for Whisper model to load ({timeout}s)")
                    return False

            # Set loading flag
            self._model_loading = True
            self._model_load_event.clear()

            try:
                logger.info("Loading Whisper model (async, non-blocking)...")

                def _load_model_sync():
                    """Synchronous model loading function for thread pool."""
                    return whisper.load_model("base")

                # Run in thread pool to avoid blocking event loop
                loop = asyncio.get_running_loop()
                self.model = await asyncio.wait_for(
                    loop.run_in_executor(self._executor, _load_model_sync),
                    timeout=timeout
                )

                logger.info("Whisper model loaded (async)")
                return True

            except asyncio.TimeoutError:
                logger.error(f"Whisper model loading timed out after {timeout}s")
                return False
            except Exception as e:
                logger.error(f"Failed to load Whisper model: {e}")
                return False
            finally:
                self._model_loading = False
                self._model_load_event.set()

    def is_model_loaded(self) -> bool:
        """Check if the Whisper model is already loaded."""
        return self.model is not None

    def decode_audio_data(self, audio_data):
        """Convert any input format to bytes"""

        # If already bytes, return as-is
        if isinstance(audio_data, bytes):
            logger.debug("Audio data is already bytes")
            return audio_data

        # If it's a string, try various decodings
        if isinstance(audio_data, str):
            # Try base64 first
            try:
                decoded = base64.b64decode(audio_data)
                logger.debug("Successfully decoded base64 audio")
                return decoded
            except:
                pass

            # Try URL-safe base64
            try:
                decoded = base64.urlsafe_b64decode(audio_data)
                logger.debug("Successfully decoded URL-safe base64 audio")
                return decoded
            except:
                pass

            # Try hex encoding
            try:
                decoded = bytes.fromhex(audio_data)
                logger.debug("Successfully decoded hex audio")
                return decoded
            except:
                pass

            # Try latin-1 encoding as last resort
            try:
                decoded = audio_data.encode('latin-1')
                logger.debug("Encoded string as latin-1")
                return decoded
            except:
                pass

        # If it's a numpy array
        if isinstance(audio_data, np.ndarray):
            if audio_data.dtype == np.float32 or audio_data.dtype == np.float64:
                # Convert float to int16
                audio_data = (audio_data * 32767).astype(np.int16)
            return audio_data.tobytes()

        # Try to convert to bytes
        try:
            return bytes(audio_data)
        except:
            logger.error(f"Cannot convert audio data of type {type(audio_data)} to bytes")
            raise ValueError(f"Unsupported audio data type: {type(audio_data)}")

    async def create_wav_from_normalized_audio(self, audio_array: np.ndarray) -> str:
        """
        Create a temporary WAV file from normalized audio array

        Args:
            audio_array: Normalized float32 array at 16kHz

        Returns:
            Path to temporary WAV file
        """
        def _write_sync():
            with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as tmp:
                sf.write(tmp.name, audio_array, 16000)
                return tmp.name

        return await asyncio.to_thread(_write_sync)

    def _get_vad_pipeline(self):
        """Lazy-load VAD pipeline"""
        if self.vad_pipeline is None and self.vad_enabled:
            try:
                self.vad_pipeline = get_vad_pipeline()
                logger.info("✅ VAD pipeline loaded for Whisper audio handler")
            except Exception as e:
                logger.error(f"Failed to load VAD pipeline: {e}")
                self.vad_enabled = False
        return self.vad_pipeline

    def _get_window_manager(self):
        """Lazy-load window manager"""
        if self.window_manager is None and self.windowing_enabled:
            try:
                self.window_manager = get_window_manager()
                logger.info("✅ Window manager loaded for Whisper audio handler")
            except Exception as e:
                logger.error(f"Failed to load window manager: {e}")
                self.windowing_enabled = False
        return self.window_manager

    async def _apply_vad_and_windowing(
        self,
        audio: np.ndarray,
        mode: str = 'general'
    ) -> np.ndarray:
        """
        Apply VAD filtering and windowing to audio

        Process:
        1. Apply VAD to remove silence/noise (if enabled)
        2. Apply windowing based on mode (if enabled)

        Args:
            audio: Normalized audio (float32, 16kHz)
            mode: 'general', 'unlock', or 'command'

        Returns:
            Filtered and windowed audio ready for Whisper
        """
        original_duration = len(audio) / 16000

        # Step 1: VAD filtering (remove silence/noise)
        if self.vad_enabled:
            vad_pipeline = self._get_vad_pipeline()
            if vad_pipeline:
                try:
                    logger.info(f"🔍 Applying VAD filter ({mode} mode)...")
                    audio = await vad_pipeline.filter_audio_async(audio)

                    if len(audio) == 0:
                        logger.warning("❌ VAD filtered out ALL audio - no speech detected")
                        return audio

                    vad_duration = len(audio) / 16000
                    logger.info(f"✅ VAD filtered: {original_duration:.2f}s → {vad_duration:.2f}s ({vad_duration/original_duration*100:.1f}% retained)")
                except Exception as e:
                    logger.error(f"VAD filtering failed: {e}, proceeding without VAD")

        # Step 2: Windowing (truncation)
        if self.windowing_enabled:
            window_manager = self._get_window_manager()
            if window_manager:
                try:
                    logger.info(f"🪟 Applying windowing ({mode} mode)...")
                    audio = window_manager.prepare_for_transcription(audio, mode=mode)

                    if len(audio) == 0:
                        logger.warning("❌ Windowing resulted in empty audio")
                        return audio

                    final_duration = len(audio) / 16000
                    logger.info(f"✅ Final audio: {final_duration:.2f}s ready for Whisper")
                except Exception as e:
                    logger.error(f"Windowing failed: {e}, proceeding without windowing")

        return audio

    async def transcribe_any_format(
        self,
        audio_data,
        sample_rate: int = None,
        mode: str = 'general'
    ):
        """
        Transcribe audio in any format with automatic normalization + VAD + windowing

        NOW INCLUDES:
        - VAD filtering (WebRTC-VAD + Silero VAD) to remove silence/noise
        - Windowing/truncation (5s global, 2s unlock, 3s command)
        - Mode-aware optimization for ultra-low latency
        - Non-blocking async model loading

        Args:
            audio_data: Audio bytes or base64 string
            sample_rate: Optional sample rate from frontend (browser-reported)
                        If None, will attempt to infer from audio
            mode: Processing mode:
                  - 'general': Standard transcription (5s window)
                  - 'unlock': Ultra-fast unlock flow (2s window)
                  - 'command': Command detection (3s window)
        """

        # Load model asynchronously (non-blocking) if not already loaded
        if not self.is_model_loaded():
            model_loaded = await self.load_model_async()
            if not model_loaded:
                logger.error("Failed to load Whisper model for transcription")
                return None

        # SAFETY: Capture model reference BEFORE any async operations or thread spawning
        # to prevent segfaults if model is unloaded during execution
        model = self.model
        if model is None:
            logger.error("Whisper model is None after loading check - race condition?")
            return None

        try:
            # Convert to bytes
            audio_bytes = self.decode_audio_data(audio_data)

            # Normalize audio (use provided sample rate or auto-detect)
            normalized_audio = await self.normalize_audio(audio_bytes, sample_rate=sample_rate)

            # **CRITICAL**: Apply VAD + windowing BEFORE Whisper
            # This prevents hallucinations, reduces latency, and improves accuracy
            normalized_audio = await self._apply_vad_and_windowing(normalized_audio, mode=mode)

            # Check if VAD filtered out all speech
            if len(normalized_audio) == 0:
                logger.warning("⚠️ VAD/windowing resulted in empty audio - no speech to transcribe")
                return None

            # Transcribe with Whisper in thread pool to avoid blocking
            def _transcribe_sync():
                # Double-check model reference is still valid inside thread
                if model is None:
                    raise RuntimeError("Whisper model reference became None during transcription")
                logger.info(f"🎤 Transcribing audio directly (bypass WAV file)")
                logger.info(f"   Audio array shape: {normalized_audio.shape}, dtype: {normalized_audio.dtype}")
                logger.info(f"   Audio min: {normalized_audio.min():.6f}, max: {normalized_audio.max():.6f}")
                logger.info(f"   Audio duration: {len(normalized_audio) / 16000:.2f}s @ 16kHz")

                # CRITICAL FIX: Whisper requires audio to be padded/trimmed to N_SAMPLES
                # Whisper internally pads to 30 seconds (480000 samples at 16kHz)
                # But we need to ensure it's at least long enough to avoid edge cases
                audio_to_transcribe = normalized_audio

                # Ensure minimum length (at least 0.5 seconds)
                min_samples = 8000  # 0.5 seconds at 16kHz
                if len(audio_to_transcribe) < min_samples:
                    logger.warning(f"⚠️ Audio too short ({len(audio_to_transcribe)} samples), padding to {min_samples}")
                    audio_to_transcribe = np.pad(audio_to_transcribe, (0, min_samples - len(audio_to_transcribe)))

                # Pass audio array directly to Whisper
                # Use initial_prompt to guide Whisper toward expected content
                # This PREVENTS hallucinations like "Hey Jarvis, I'm Mark McCree"

                # Mode-specific prompts to bias Whisper toward expected phrases
                mode_prompts = {
                    'unlock': "unlock my screen, unlock screen, unlock, jarvis unlock, open screen",
                    'command': "jarvis, hey jarvis, computer, assistant",
                    'general': ""  # No bias for general transcription
                }
                initial_prompt = mode_prompts.get(mode, "")

                result = model.transcribe(
                    audio_to_transcribe,
                    language="en",
                    fp16=False,
                    word_timestamps=False,  # Faster without word timestamps
                    condition_on_previous_text=False,  # Don't use context from previous
                    temperature=0.0,  # Deterministic (no randomness)
                    initial_prompt=initial_prompt,  # 🔑 KEY FIX: Bias toward expected phrases
                    no_speech_threshold=0.6,  # Higher threshold = more strict speech detection
                    logprob_threshold=-1.0,  # Reject low-confidence outputs
                    compression_ratio_threshold=2.4  # Reject repetitive hallucinations
                )
                logger.info(f"   Raw Whisper result: {result}")
                return result["text"].strip() # Strip leading/trailing whitespace from text

            text = await asyncio.to_thread(_transcribe_sync)

            logger.info(f"✅ Transcribed: '{text}'")

            # If Whisper returns empty string, it detected no speech
            # Return None to signal failure so hybrid_stt_router can try other methods
            if not text or text.strip() == "":
                logger.warning("⚠️ Whisper returned empty transcription - no speech detected")
                logger.warning(f"   Audio was {len(audio_bytes)} bytes, normalized to {len(normalized_audio)} samples")
                logger.warning(f"   Sample rate: provided={sample_rate}, final=16000Hz")
                return None

            return text

        except Exception as e:
            logger.error(f"❌ Whisper transcription failed: {e}")
            logger.error("   This indicates audio format issues or invalid audio data")
            # Return None to signal failure - do NOT return hardcoded text
            return None

# Global instance
_whisper_handler = WhisperAudioHandler()

async def transcribe_with_whisper(
    audio_data,
    sample_rate: int = None,
    mode: str = 'general'
):
    """
    Global transcription function with VAD + windowing support

    Args:
        audio_data: Audio bytes or base64 string
        sample_rate: Optional sample rate from frontend (browser-reported)
        mode: Processing mode ('general', 'unlock', or 'command')
              - 'general': 5s window, standard processing
              - 'unlock': 2s window, ultra-fast for unlock flow
              - 'command': 3s window, optimized for command detection
    """
    return await _whisper_handler.transcribe_any_format(
        audio_data,
        sample_rate=sample_rate,
        mode=mode
    )