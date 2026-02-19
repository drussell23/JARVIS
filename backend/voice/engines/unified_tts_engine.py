"""
Unified Text-to-Speech Engine
Production-grade TTS with multiple provider support and automatic fallback

Features:
- Multiple TTS providers (Google, macOS, pyttsx3, Coqui)
- Automatic fallback on failure
- Voice cloning support (Coqui)
- SSML support where available
- Caching for repeated phrases
- Performance optimization
- Async processing throughout
"""

import asyncio
import hashlib
import io
import logging
import os
import subprocess
import tempfile
import time
from collections import OrderedDict
from pathlib import Path
from typing import AsyncIterator, Dict, List, Optional

import numpy as np
import sounddevice as sd
import soundfile as sf

from .base_tts_engine import BaseTTSEngine, TTSChunk, TTSConfig, TTSEngine, TTSResult

logger = logging.getLogger(__name__)

# =============================================================================
# SINGLETON ACCESSOR
# =============================================================================

_tts_instance: Optional["UnifiedTTSEngine"] = None
_tts_lock: Optional[asyncio.Lock] = None


def _get_tts_lock() -> asyncio.Lock:
    """Lazily create the singleton lock to avoid 'no running event loop' at import."""
    global _tts_lock
    if _tts_lock is None:
        _tts_lock = asyncio.Lock()
    return _tts_lock


async def get_tts_engine(
    preferred_engine: TTSEngine = TTSEngine.MACOS,
    config: Optional[TTSConfig] = None,
) -> "UnifiedTTSEngine":
    """
    Get the global UnifiedTTSEngine singleton.

    First call initializes the engine (loads models, discovers voices).
    Subsequent calls return the cached instance.

    Args:
        preferred_engine: Which TTS backend to try first.
        config: Optional config override (only used on first init).

    Returns:
        Initialized UnifiedTTSEngine singleton.
    """
    global _tts_instance

    if _tts_instance is not None:
        return _tts_instance

    async with _get_tts_lock():
        # Double-checked locking
        if _tts_instance is not None:
            return _tts_instance

        engine = UnifiedTTSEngine(
            preferred_engine=preferred_engine,
            config=config,
        )
        await engine.initialize()
        _tts_instance = engine
        logger.info("[TTS] Singleton engine initialized")
        return _tts_instance


class TTSCache:
    """LRU cache for synthesized speech"""

    def __init__(self, max_size: int = 500):
        self.cache: OrderedDict = OrderedDict()
        self.max_size = max_size
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> Optional[TTSResult]:
        """Get cached result"""
        if key in self.cache:
            self.hits += 1
            self.cache.move_to_end(key)
            return self.cache[key]
        self.misses += 1
        return None

    def put(self, key: str, value: TTSResult):
        """Store result in cache"""
        if key in self.cache:
            self.cache.move_to_end(key)
        else:
            self.cache[key] = value
            if len(self.cache) > self.max_size:
                self.cache.popitem(last=False)

    def get_stats(self) -> Dict:
        """Get cache statistics"""
        total = self.hits + self.misses
        hit_rate = self.hits / total if total > 0 else 0.0
        return {
            "size": len(self.cache),
            "max_size": self.max_size,
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": hit_rate,
        }


class GoogleTTSEngine(BaseTTSEngine):
    """Google Text-to-Speech using gTTS"""

    def __init__(self, config: TTSConfig):
        super().__init__(config)
        self.gtts = None

    async def initialize(self):
        """Initialize gTTS"""
        if self.initialized:
            return

        try:
            from gtts import gTTS

            self.gtts = gTTS
            self.initialized = True
            logger.info("✅ Google TTS (gTTS) initialized")
        except ImportError:
            logger.error("gTTS not installed. Install with: pip install gTTS")
            raise

    async def synthesize(self, text: str) -> TTSResult:
        """Synthesize speech using Google TTS"""
        if not self.initialized:
            await self.initialize()

        start_time = time.time()

        try:
            # Create gTTS object
            loop = asyncio.get_event_loop()
            tts = await loop.run_in_executor(
                None,
                lambda: self.gtts(
                    text=text, lang=self.config.language[:2], slow=self.config.speed < 0.9
                ),
            )

            # Generate audio to buffer
            audio_buffer = io.BytesIO()
            await loop.run_in_executor(None, lambda: tts.write_to_fp(audio_buffer))
            audio_buffer.seek(0)

            # Read audio data
            audio_data, sample_rate = sf.read(audio_buffer)
            duration_ms = (len(audio_data) / sample_rate) * 1000
            latency_ms = (time.time() - start_time) * 1000

            # Convert to bytes
            audio_buffer_out = io.BytesIO()
            sf.write(audio_buffer_out, audio_data, sample_rate, format="WAV")
            audio_bytes = audio_buffer_out.getvalue()

            return TTSResult(
                audio_data=audio_bytes,
                sample_rate=sample_rate,
                duration_ms=duration_ms,
                latency_ms=latency_ms,
                engine=TTSEngine.GTTS,
                voice=f"{self.config.language}_google",
                metadata={
                    "language": self.config.language,
                    "text_length": len(text),
                    "rtf": latency_ms / duration_ms if duration_ms > 0 else 0,
                },
            )

        except Exception as e:
            logger.error(f"Google TTS error: {e}", exc_info=True)
            raise

    async def get_available_voices(self) -> List[str]:
        """Get available voices (languages for gTTS)"""
        return ["en", "es", "fr", "de", "it", "pt", "ru", "ja", "zh"]

    async def cleanup(self):
        """Cleanup resources"""
        self.initialized = False


class MacOSTTSEngine(BaseTTSEngine):
    """macOS native TTS using 'say' command"""

    def __init__(self, config: TTSConfig):
        super().__init__(config)

    async def initialize(self):
        """Initialize macOS TTS"""
        if self.initialized:
            return

        # Check if 'say' command is available
        try:
            result = subprocess.run(["which", "say"], capture_output=True, text=True)
            if result.returncode == 0:
                self.initialized = True
                logger.info("✅ macOS TTS (say) initialized")
            else:
                raise RuntimeError("'say' command not found (not running on macOS?)")
        except Exception as e:
            logger.error(f"macOS TTS initialization failed: {e}")
            raise

    async def synthesize(self, text: str) -> TTSResult:
        """Synthesize speech using macOS 'say' command"""
        if not self.initialized:
            await self.initialize()

        start_time = time.time()

        try:
            # Create temp file for audio output (secure)
            temp_fd, temp_path = tempfile.mkstemp(suffix=".aiff", prefix="jarvis_tts_")
            os.close(temp_fd)  # Close FD, we'll use the path
            temp_file = Path(temp_path)

            # Build command
            cmd = ["say", "-o", str(temp_file)]

            if self.config.voice:
                cmd.extend(["-v", self.config.voice])

            if self.config.speed != 1.0:
                rate = int(175 * self.config.speed)  # Default rate is 175 wpm
                cmd.extend(["-r", str(rate)])

            cmd.append(text)

            # Run synthesis
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None, lambda: subprocess.run(cmd, check=True, capture_output=True)
            )

            # Read audio file
            audio_data, sample_rate = sf.read(temp_file)
            duration_ms = (len(audio_data) / sample_rate) * 1000
            latency_ms = (time.time() - start_time) * 1000

            # Convert to WAV bytes
            audio_buffer = io.BytesIO()
            sf.write(audio_buffer, audio_data, sample_rate, format="WAV")
            audio_bytes = audio_buffer.getvalue()

            # Cleanup temp file
            temp_file.unlink()

            return TTSResult(
                audio_data=audio_bytes,
                sample_rate=sample_rate,
                duration_ms=duration_ms,
                latency_ms=latency_ms,
                engine=TTSEngine.MACOS,
                voice=self.config.voice or "default",
                metadata={
                    "speed": self.config.speed,
                    "text_length": len(text),
                    "rtf": latency_ms / duration_ms if duration_ms > 0 else 0,
                },
            )

        except Exception as e:
            logger.error(f"macOS TTS error: {e}", exc_info=True)
            raise

    async def get_available_voices(self) -> List[str]:
        """Get available macOS voices"""
        try:
            result = subprocess.run(["say", "-v", "?"], capture_output=True, text=True)
            voices = []
            for line in result.stdout.split("\n"):
                if line.strip():
                    voice_name = line.split()[0]
                    voices.append(voice_name)
            return voices
        except Exception:
            return ["Alex", "Samantha", "Victoria", "Daniel"]

    async def cleanup(self):
        """Cleanup resources"""
        self.initialized = False


class Pyttsx3TTSEngine(BaseTTSEngine):
    """Cross-platform TTS using pyttsx3"""

    def __init__(self, config: TTSConfig):
        super().__init__(config)
        self.engine = None

    async def initialize(self):
        """Initialize pyttsx3"""
        if self.initialized:
            return

        try:
            import pyttsx3

            loop = asyncio.get_event_loop()
            self.engine = await loop.run_in_executor(None, pyttsx3.init)

            # Set properties
            if self.config.speed != 1.0:
                rate = self.engine.getProperty("rate")
                self.engine.setProperty("rate", int(rate * self.config.speed))

            if self.config.volume != 1.0:
                self.engine.setProperty("volume", self.config.volume)

            if self.config.voice:
                self.engine.setProperty("voice", self.config.voice)

            self.initialized = True
            logger.info("✅ pyttsx3 TTS initialized")

        except ImportError:
            logger.error("pyttsx3 not installed. Install with: pip install pyttsx3")
            raise
        except Exception as e:
            logger.error(f"pyttsx3 initialization failed: {e}")
            raise

    async def synthesize(self, text: str) -> TTSResult:
        """Synthesize speech using pyttsx3"""
        if not self.initialized:
            await self.initialize()

        start_time = time.time()

        try:
            # Create temp file (secure)
            temp_fd, temp_path = tempfile.mkstemp(suffix=".wav", prefix="jarvis_tts_")
            os.close(temp_fd)  # Close FD, we'll use the path
            temp_file = Path(temp_path)

            # SAFETY: Capture engine reference BEFORE spawning threads
            engine_ref = self.engine
            if engine_ref is None:
                raise RuntimeError("TTS engine not initialized")

            def _save_to_file():
                if engine_ref is None:
                    raise RuntimeError("Engine reference became None during synthesis")
                engine_ref.save_to_file(text, str(temp_file))

            def _run_and_wait():
                if engine_ref is None:
                    raise RuntimeError("Engine reference became None during synthesis")
                engine_ref.runAndWait()

            # Synthesize to file
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, _save_to_file)
            await loop.run_in_executor(None, _run_and_wait)

            # Read audio file
            audio_data, sample_rate = sf.read(temp_file)
            duration_ms = (len(audio_data) / sample_rate) * 1000
            latency_ms = (time.time() - start_time) * 1000

            # Convert to bytes
            audio_buffer = io.BytesIO()
            sf.write(audio_buffer, audio_data, sample_rate, format="WAV")
            audio_bytes = audio_buffer.getvalue()

            # Cleanup
            temp_file.unlink()

            return TTSResult(
                audio_data=audio_bytes,
                sample_rate=sample_rate,
                duration_ms=duration_ms,
                latency_ms=latency_ms,
                engine=TTSEngine.PYTTSX3,
                voice=self.config.voice or "default",
                metadata={
                    "speed": self.config.speed,
                    "volume": self.config.volume,
                    "text_length": len(text),
                    "rtf": latency_ms / duration_ms if duration_ms > 0 else 0,
                },
            )

        except Exception as e:
            logger.error(f"pyttsx3 TTS error: {e}", exc_info=True)
            raise

    async def get_available_voices(self) -> List[str]:
        """Get available voices"""
        try:
            voices = self.engine.getProperty("voices")
            return [v.id for v in voices]
        except Exception:
            return []

    async def cleanup(self):
        """Cleanup resources"""
        if self.engine:
            self.engine.stop()
        self.initialized = False


class UnifiedTTSEngine:
    """
    Unified TTS engine with automatic fallback and provider selection

    Features:
    - Multiple TTS providers
    - Automatic fallback on failure
    - Response caching
    - Performance optimization
    - Voice selection
    """

    def __init__(
        self,
        preferred_engine: TTSEngine = TTSEngine.MACOS,
        config: Optional[TTSConfig] = None,
        enable_cache: bool = True,
    ):
        self.preferred_engine = preferred_engine
        self.config = config or TTSConfig(
            name="unified-tts", engine=preferred_engine, language="en", speed=1.0
        )

        # TTS engines
        self.engines: Dict[TTSEngine, Optional[BaseTTSEngine]] = {
            TTSEngine.PIPER: None,
            TTSEngine.GTTS: None,
            TTSEngine.MACOS: None,
            TTSEngine.PYTTSX3: None,
        }

        self.active_engine = None
        self.fallback_order = [
            TTSEngine.PIPER, TTSEngine.MACOS, TTSEngine.PYTTSX3, TTSEngine.GTTS
        ]

        # Feature flag for AudioBus routing
        self._audio_bus_enabled = os.getenv(
            "JARVIS_AUDIO_BUS_ENABLED", "false"
        ).lower() in ("true", "1", "yes")

        # Caching
        self.cache = TTSCache(max_size=500) if enable_cache else None

        # Performance
        self.total_requests = 0
        self.total_latency_ms = 0.0

        logger.info(f"🔊 Unified TTS Engine initialized (preferred: {preferred_engine.value})")

    async def initialize(self):
        """Initialize preferred and fallback engines"""
        # Try to initialize preferred engine
        try:
            self.active_engine = await self._initialize_engine(self.preferred_engine)
            if self.active_engine:
                logger.info(f"✅ Active TTS engine: {self.preferred_engine.value}")
                return
        except Exception as e:
            logger.warning(f"Failed to initialize {self.preferred_engine.value}: {e}")

        # Try fallback engines
        for engine_type in self.fallback_order:
            if engine_type == self.preferred_engine:
                continue

            try:
                engine = await self._initialize_engine(engine_type)
                if engine:
                    self.active_engine = engine
                    logger.info(f"✅ Using fallback TTS engine: {engine_type.value}")
                    return
            except Exception as e:
                logger.warning(f"Failed to initialize {engine_type.value}: {e}")

        raise RuntimeError("No TTS engines available")

    async def _initialize_engine(self, engine_type: TTSEngine) -> Optional[BaseTTSEngine]:
        """Initialize specific TTS engine"""
        if self.engines[engine_type] is not None:
            return self.engines[engine_type]

        try:
            if engine_type == TTSEngine.PIPER:
                from .piper_tts_engine import PiperTTSEngine
                engine = PiperTTSEngine(self.config)
            elif engine_type == TTSEngine.GTTS:
                engine = GoogleTTSEngine(self.config)
            elif engine_type == TTSEngine.MACOS:
                engine = MacOSTTSEngine(self.config)
            elif engine_type == TTSEngine.PYTTSX3:
                engine = Pyttsx3TTSEngine(self.config)
            else:
                return None

            await engine.initialize()
            self.engines[engine_type] = engine
            return engine

        except Exception as e:
            logger.debug(f"Could not initialize {engine_type.value}: {e}")
            return None

    async def speak(
        self,
        text: str,
        play_audio: bool = True,
        source: Optional[str] = None,
    ) -> TTSResult:
        """
        Synthesize and optionally play speech.

        Integrates with UnifiedSpeechStateManager to prevent JARVIS from
        hearing its own voice (self-voice suppression).

        Args:
            text: Text to synthesize
            play_audio: Whether to play audio immediately
            source: Speech source identifier for state manager

        Returns:
            TTSResult with audio data and metadata
        """
        if not self.active_engine:
            await self.initialize()

        # Check cache first
        if self.cache:
            cache_key = hashlib.md5(
                f"{text}_{self.config.language}_{self.config.speed}".encode(),
                usedforsecurity=False,
            ).hexdigest()
            cached_result = self.cache.get(cache_key)
            if cached_result:
                logger.debug("[TTS Cache HIT]")
                if play_audio:
                    await self._notify_speech_start(text, source)
                    try:
                        await self._play_audio(
                            cached_result.audio_data, cached_result.sample_rate
                        )
                    finally:
                        await self._notify_speech_stop(cached_result.duration_ms)
                return cached_result

        # Synthesize
        try:
            result = await self.active_engine.synthesize(text)
            self.total_requests += 1
            self.total_latency_ms += result.latency_ms

            # Cache the result
            if self.cache:
                self.cache.put(cache_key, result)

            # Play if requested — with speech state tracking
            if play_audio:
                await self._notify_speech_start(text, source)
                try:
                    await self._play_audio(result.audio_data, result.sample_rate)
                finally:
                    await self._notify_speech_stop(result.duration_ms)

            logger.info(
                f"[TTS] Synthesized {len(text)} chars "
                f"(latency: {result.latency_ms:.0f}ms, "
                f"RTF: {result.metadata.get('rtf', 0):.2f}x)"
            )

            return result

        except Exception as e:
            logger.error(f"TTS synthesis error: {e}", exc_info=True)
            raise

    async def _play_audio(self, audio_data: bytes, sample_rate: int):
        """Play audio — routes through AudioBus when enabled, else sounddevice."""
        try:
            # Load audio from bytes
            audio_buffer = io.BytesIO(audio_data)
            data, sr = sf.read(audio_buffer, dtype="float32")
            if sr != sample_rate:
                sample_rate = sr

            if self._audio_bus_enabled:
                try:
                    from backend.audio.audio_bus import get_audio_bus_safe
                    bus = get_audio_bus_safe()
                    if bus is not None and bus.is_running:
                        audio_np = np.asarray(data, dtype=np.float32)
                        await bus.play_audio(audio_np, sample_rate)
                        return
                except ImportError:
                    pass  # AudioBus not available, fall through

            # Legacy path: direct sounddevice playback
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None, lambda: sd.play(data, sample_rate) or sd.wait()
            )

        except Exception as e:
            logger.error(f"Audio playback error: {e}", exc_info=True)

    async def speak_stream(
        self,
        text: str,
        play_audio: bool = True,
        cancel_event: Optional[asyncio.Event] = None,
        source: Optional[str] = None,
    ) -> None:
        """
        Synthesize and stream audio using the active engine's streaming
        capability, routed through AudioBus with barge-in support.

        Falls back to speak() if streaming is not available or AudioBus
        is not running.

        Args:
            text: Text to synthesize
            play_audio: Whether to play audio
            cancel_event: Event signalling barge-in — set to stop playback.
            source: Speech source identifier for state manager.
        """
        if not self.active_engine:
            await self.initialize()

        if not self._audio_bus_enabled:
            await self.speak(text, play_audio=play_audio, source=source)
            return

        try:
            from backend.audio.audio_bus import get_audio_bus_safe
            bus = get_audio_bus_safe()
            if bus is None or not bus.is_running:
                await self.speak(text, play_audio=play_audio, source=source)
                return

            await self._notify_speech_start(text, source)
            play_start = time.time()
            try:
                # Wrap the engine's chunk iterator to convert to numpy arrays
                async def _chunk_to_numpy():
                    async for chunk in self.active_engine.synthesize_stream(text):
                        if not chunk.audio_data:
                            continue
                        audio_buf = io.BytesIO(chunk.audio_data)
                        data, sr = sf.read(audio_buf, dtype="float32")
                        yield np.asarray(data, dtype=np.float32)

                # Use bus.play_stream() which supports cancel events for barge-in
                await bus.play_stream(
                    _chunk_to_numpy(),
                    sample_rate=self.config.sample_rate if hasattr(self.config, 'sample_rate') else 22050,
                    cancel=cancel_event,
                )
            finally:
                actual_duration_ms = (time.time() - play_start) * 1000
                await self._notify_speech_stop(actual_duration_ms)

        except Exception as e:
            logger.warning(f"[UnifiedTTS] Stream failed, falling back: {e}")
            await self.speak(text, play_audio=play_audio, source=source)

    # ---- Speech State Integration ----

    async def _notify_speech_start(
        self, text: str, source: Optional[str] = None
    ) -> None:
        """Notify UnifiedSpeechStateManager that JARVIS started speaking."""
        try:
            from backend.core.unified_speech_state import (
                SpeechSource,
                get_speech_state_manager,
            )

            manager = await get_speech_state_manager()

            # Map source string to SpeechSource enum
            speech_source = SpeechSource.TTS_BACKEND
            if source:
                try:
                    speech_source = SpeechSource(source)
                except ValueError:
                    pass

            await manager.start_speaking(text, source=speech_source)
        except Exception as e:
            # Non-fatal — speech state is best-effort
            logger.debug(f"[TTS] Speech state start_speaking failed: {e}")

    async def _notify_speech_stop(
        self, duration_ms: Optional[float] = None
    ) -> None:
        """Notify UnifiedSpeechStateManager that JARVIS stopped speaking."""
        try:
            from backend.core.unified_speech_state import get_speech_state_manager

            manager = await get_speech_state_manager()
            await manager.stop_speaking(actual_duration_ms=duration_ms)
        except Exception as e:
            logger.debug(f"[TTS] Speech state stop_speaking failed: {e}")

    # ---- Voice Management ----

    async def get_available_voices(self) -> List[str]:
        """Get available voices from active engine"""
        if not self.active_engine:
            await self.initialize()

        return await self.active_engine.get_available_voices()

    def set_voice(self, voice: str):
        """Set voice for synthesis"""
        self.config.voice = voice

    def set_speed(self, speed: float):
        """Set speech rate (0.5 - 2.0)"""
        self.config.speed = max(0.5, min(2.0, speed))

    def set_language(self, language: str):
        """Set language (e.g., 'en', 'es', 'fr')"""
        self.config.language = language

    def get_stats(self) -> Dict:
        """Get performance statistics"""
        stats = {
            "active_engine": self.active_engine.config.engine.value if self.active_engine else None,
            "total_requests": self.total_requests,
            "avg_latency_ms": (
                self.total_latency_ms / self.total_requests if self.total_requests > 0 else 0
            ),
        }

        if self.cache:
            stats["cache"] = self.cache.get_stats()

        return stats

    async def cleanup(self):
        """Cleanup all engines"""
        for engine in self.engines.values():
            if engine:
                await engine.cleanup()

        logger.info("🧹 TTS engines cleaned up")


# Example usage
async def test_tts():
    """Test unified TTS engine"""
    print("🔊 Testing Unified TTS Engine")
    print("=" * 60)

    # Create engine (will auto-detect best available)
    tts = UnifiedTTSEngine()
    await tts.initialize()

    # Test phrases
    phrases = [
        "Hello! I am JARVIS, your voice assistant.",
        "The weather today is sunny with a high of 75 degrees.",
        "Would you like me to read your messages?",
    ]

    for i, phrase in enumerate(phrases, 1):
        print(f'\n{i}. Testing: "{phrase}"')
        result = await tts.speak(phrase, play_audio=True)
        print(f"   ✓ Duration: {result.duration_ms:.0f}ms")
        print(f"   ✓ Latency: {result.latency_ms:.0f}ms")
        print(f"   ✓ Engine: {result.engine.value}")

    # Show stats
    print("\n📊 Performance Stats:")
    stats = tts.get_stats()
    for key, value in stats.items():
        print(f"   {key}: {value}")

    # Cleanup
    await tts.cleanup()
    print("\n✅ Test complete!")


# ============================================================================
# Singleton Factory
# ============================================================================

_tts_singleton: Optional[UnifiedTTSEngine] = None
_tts_singleton_lock = asyncio.Lock()


async def get_unified_tts_engine() -> UnifiedTTSEngine:
    """Get or create the singleton UnifiedTTSEngine.

    Thread-safe, double-checked locking. Returns an initialized engine
    that can be shared across all callers (WebSocket endpoints,
    ConversationPipeline, macOS voice bridge, etc.).
    """
    global _tts_singleton
    if _tts_singleton is not None and _tts_singleton.active_engine is not None:
        return _tts_singleton
    async with _tts_singleton_lock:
        if _tts_singleton is not None and _tts_singleton.active_engine is not None:
            return _tts_singleton
        engine = UnifiedTTSEngine()
        await engine.initialize()
        _tts_singleton = engine
        return _tts_singleton


# Alias for shorter imports
get_tts_engine = get_unified_tts_engine


if __name__ == "__main__":
    asyncio.run(test_tts())
