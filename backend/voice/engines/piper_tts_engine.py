"""
Piper TTS Engine — Local Neural TTS with True Streaming
=========================================================

Piper is a fast, local neural TTS that runs ONNX models on CPU/GPU.
On Apple Silicon it achieves ~50ms time-to-first-audio for short sentences.

Model management:
    Models are auto-downloaded on first use and stored in
    ~/.jarvis/piper_models/. The default voice is "en_US-lessac-medium".

Streaming:
    Piper generates audio in sentence-sized chunks. This engine yields
    each chunk as a TTSChunk, enabling the AudioBus to start playback
    before the full text is synthesized.
"""

import asyncio
import io
import logging
import os
import time
from pathlib import Path
from typing import AsyncIterator, List, Optional

import numpy as np

from .base_tts_engine import BaseTTSEngine, TTSChunk, TTSConfig, TTSEngine, TTSResult

logger = logging.getLogger(__name__)

# Default Piper voice — high quality, ~50MB
_DEFAULT_VOICE = os.getenv("JARVIS_PIPER_VOICE", "en_US-lessac-medium")
_MODELS_DIR = Path(os.getenv(
    "JARVIS_PIPER_MODELS_DIR",
    os.path.expanduser("~/.jarvis/piper_models")
))

# Chunk size for streaming output (in samples)
_STREAM_CHUNK_SAMPLES = int(os.getenv("JARVIS_PIPER_CHUNK_SAMPLES", "8000"))


class PiperTTSEngine(BaseTTSEngine):
    """
    Local neural TTS with true streaming using Piper.

    Features:
        - ~50ms time-to-first-audio on Apple Silicon
        - True streaming via chunked audio generation
        - Automatic model download and caching
        - Multiple voice support
    """

    def __init__(self, config: Optional[TTSConfig] = None):
        if config is None:
            config = TTSConfig(
                name="piper-tts",
                engine=TTSEngine.PIPER,
                language="en",
                voice=_DEFAULT_VOICE,
                sample_rate=22050,
            )
        super().__init__(config)
        self._piper = None
        self._voice_model = None
        self._sample_rate = config.sample_rate

    async def initialize(self) -> None:
        """Load the Piper ONNX model."""
        if self.initialized:
            return

        try:
            import piper

            # Ensure models directory exists
            _MODELS_DIR.mkdir(parents=True, exist_ok=True)

            voice_name = self.config.voice or _DEFAULT_VOICE

            loop = asyncio.get_running_loop()

            def _load_model():
                # piper.PiperVoice.load() handles model download
                model_path = _MODELS_DIR / f"{voice_name}.onnx"
                config_path = _MODELS_DIR / f"{voice_name}.onnx.json"

                if model_path.exists() and config_path.exists():
                    return piper.PiperVoice.load(
                        str(model_path),
                        config_path=str(config_path),
                    )
                else:
                    # Try to download
                    logger.info(
                        f"[PiperTTS] Model '{voice_name}' not found locally, "
                        f"attempting download to {_MODELS_DIR}"
                    )
                    # piper-tts supports downloading models
                    try:
                        from piper.download import ensure_voice_exists, find_voice
                        # Download the voice
                        ensure_voice_exists(
                            voice_name,
                            data_dirs=[str(_MODELS_DIR)],
                            download_dir=str(_MODELS_DIR),
                        )
                        model_path_str, config_path_str = find_voice(
                            voice_name,
                            data_dirs=[str(_MODELS_DIR)],
                        )
                        return piper.PiperVoice.load(
                            model_path_str,
                            config_path=config_path_str,
                        )
                    except (ImportError, Exception) as dl_err:
                        logger.warning(
                            f"[PiperTTS] Auto-download failed: {dl_err}. "
                            f"Please manually download the model."
                        )
                        raise

            self._voice_model = await loop.run_in_executor(None, _load_model)

            # Get the actual sample rate from the loaded model
            if hasattr(self._voice_model, 'config') and self._voice_model.config:
                self._sample_rate = self._voice_model.config.sample_rate

            self.initialized = True
            logger.info(
                f"[PiperTTS] Initialized: voice={self.config.voice}, "
                f"sr={self._sample_rate}"
            )

        except ImportError:
            logger.info("[PiperTTS] piper-tts not installed (pip install piper-tts)")
            raise
        except Exception as e:
            logger.error(f"[PiperTTS] Initialization failed: {e}")
            raise

    async def synthesize(self, text: str) -> TTSResult:
        """Synthesize complete audio from text."""
        if not self.initialized:
            await self.initialize()

        start_time = time.time()

        try:
            loop = asyncio.get_running_loop()

            def _synthesize():
                """Run synthesis in thread pool."""
                audio_chunks = []
                for audio_bytes in self._voice_model.synthesize_stream_raw(text):
                    audio_chunks.append(
                        np.frombuffer(audio_bytes, dtype=np.int16)
                    )

                if not audio_chunks:
                    return np.array([], dtype=np.int16)

                return np.concatenate(audio_chunks)

            audio_i16 = await loop.run_in_executor(None, _synthesize)

            duration_ms = (len(audio_i16) / self._sample_rate) * 1000
            latency_ms = (time.time() - start_time) * 1000

            # Convert to WAV bytes
            import soundfile as sf
            audio_f32 = audio_i16.astype(np.float32) / 32767.0
            buf = io.BytesIO()
            sf.write(buf, audio_f32, self._sample_rate, format="WAV")
            audio_bytes = buf.getvalue()

            return TTSResult(
                audio_data=audio_bytes,
                sample_rate=self._sample_rate,
                duration_ms=duration_ms,
                latency_ms=latency_ms,
                engine=TTSEngine.PIPER,
                voice=self.config.voice or _DEFAULT_VOICE,
                metadata={
                    "text_length": len(text),
                    "rtf": latency_ms / duration_ms if duration_ms > 0 else 0,
                },
            )
        except Exception as e:
            logger.error(f"[PiperTTS] Synthesis error: {e}")
            raise

    async def synthesize_stream(self, text: str) -> AsyncIterator[TTSChunk]:
        """
        Stream synthesized audio in chunks for low-latency playback.

        Piper generates audio in sentence-level chunks internally.
        We yield each chunk as it's generated.
        """
        if not self.initialized:
            await self.initialize()

        loop = asyncio.get_running_loop()
        chunk_queue: asyncio.Queue = asyncio.Queue()

        def _stream_worker():
            """Generate audio chunks in a thread."""
            try:
                chunk_idx = 0
                buffer = np.array([], dtype=np.int16)

                for audio_bytes in self._voice_model.synthesize_stream_raw(text):
                    samples = np.frombuffer(audio_bytes, dtype=np.int16)
                    buffer = np.concatenate([buffer, samples])

                    # Yield chunks of _STREAM_CHUNK_SAMPLES
                    while len(buffer) >= _STREAM_CHUNK_SAMPLES:
                        chunk_data = buffer[:_STREAM_CHUNK_SAMPLES]
                        buffer = buffer[_STREAM_CHUNK_SAMPLES:]

                        # Convert to WAV bytes for the chunk
                        chunk_f32 = chunk_data.astype(np.float32) / 32767.0
                        chunk_buf = io.BytesIO()
                        import soundfile as sf
                        sf.write(
                            chunk_buf, chunk_f32,
                            self._sample_rate, format="WAV"
                        )

                        loop.call_soon_threadsafe(
                            chunk_queue.put_nowait,
                            TTSChunk(
                                audio_data=chunk_buf.getvalue(),
                                chunk_index=chunk_idx,
                                is_final=False,
                                sample_rate=self._sample_rate,
                            ),
                        )
                        chunk_idx += 1

                # Flush remaining buffer
                if len(buffer) > 0:
                    chunk_f32 = buffer.astype(np.float32) / 32767.0
                    chunk_buf = io.BytesIO()
                    import soundfile as sf
                    sf.write(
                        chunk_buf, chunk_f32,
                        self._sample_rate, format="WAV"
                    )
                    loop.call_soon_threadsafe(
                        chunk_queue.put_nowait,
                        TTSChunk(
                            audio_data=chunk_buf.getvalue(),
                            chunk_index=chunk_idx,
                            is_final=True,
                            sample_rate=self._sample_rate,
                            duration_ms=(len(buffer) / self._sample_rate) * 1000,
                        ),
                    )
                else:
                    # Signal completion with empty final chunk
                    loop.call_soon_threadsafe(
                        chunk_queue.put_nowait,
                        TTSChunk(
                            audio_data=b"",
                            chunk_index=chunk_idx,
                            is_final=True,
                            sample_rate=self._sample_rate,
                        ),
                    )

            except Exception as e:
                logger.error(f"[PiperTTS] Stream worker error: {e}")
                loop.call_soon_threadsafe(
                    chunk_queue.put_nowait, None
                )

        # Start generation in thread pool
        gen_task = loop.run_in_executor(None, _stream_worker)

        try:
            while True:
                chunk = await chunk_queue.get()
                if chunk is None:
                    break
                yield chunk
                if chunk.is_final:
                    break
        finally:
            # Ensure the thread completes
            await gen_task

    async def get_available_voices(self) -> List[str]:
        """Get available Piper voices."""
        voices = []

        # List locally downloaded models
        if _MODELS_DIR.exists():
            for f in _MODELS_DIR.glob("*.onnx"):
                voice_name = f.stem
                voices.append(voice_name)

        if not voices:
            # Return known default voices
            voices = [
                "en_US-lessac-medium",
                "en_US-lessac-high",
                "en_US-amy-medium",
                "en_GB-alan-medium",
            ]

        return voices

    async def cleanup(self) -> None:
        """Release model resources."""
        self._voice_model = None
        self._piper = None
        self.initialized = False
        logger.info("[PiperTTS] Cleaned up")
