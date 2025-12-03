"""
Ultra-Advanced Hybrid STT Router
Zero hardcoding, fully async, RAM-aware, cost-optimized
Integrates with learning database for continuous improvement

PERFORMANCE OPTIMIZATIONS:
- Model prewarming during initialization (non-blocking)
- Whisper model caching with singleton pattern
- Speaker ID removed from transcription path (parallel, not serial)
- VAD/windowing pipeline caching
- Granular timeout protection at each stage
- Circuit breaker pattern for fault tolerance
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Optional, Tuple

import psutil

from .stt_config import ModelConfig, RoutingStrategy, STTEngine, get_stt_config

logger = logging.getLogger(__name__)

# =============================================================================
# PERFORMANCE CONSTANTS - Tune these for optimal latency
# =============================================================================
MODEL_PREWARM_TIMEOUT = 15.0  # Max time to prewarm Whisper model during init
TRANSCRIPTION_TIMEOUT = 10.0  # Max time for a single transcription attempt
FALLBACK_TIMEOUT = 5.0  # Max time for fallback transcription
VAD_TIMEOUT = 2.0  # Max time for VAD processing
SPEAKER_ID_TIMEOUT = 3.0  # Max time for speaker identification (parallel)


@dataclass
class STTResult:
    """Result from STT engine"""

    text: str
    confidence: float
    engine: STTEngine
    model_name: str
    latency_ms: float
    audio_duration_ms: float
    metadata: Dict = field(default_factory=dict)
    audio_hash: Optional[str] = None
    speaker_identified: Optional[str] = None
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class SystemResources:
    """Current system resource availability"""

    total_ram_gb: float
    available_ram_gb: float
    ram_percent_used: float
    cpu_percent: float
    gpu_available: bool
    gpu_memory_gb: float = 0.0
    network_available: bool = True


class HybridSTTRouter:
    """
    Ultra-intelligent STT routing system.

    Features:
    - Zero hardcoding (all config-driven)
    - Fully async (non-blocking)
    - RAM-aware (adapts to available resources)
    - Cost-optimized (prefers local, escalates smartly)
    - Learning-enabled (gets better over time)
    - Multi-engine (Wav2Vec, Vosk, Whisper local/GCP)
    - Speaker-aware (Derek gets priority)
    - Confidence-based escalation
    - Automatic fallbacks
    """

    def __init__(self, config=None):
        self.config = config or get_stt_config()
        self.engines = {}  # Lazy-loaded STT engines
        self.performance_stats = {}  # Track engine performance
        self.learning_db = None  # Lazy-loaded learning database

        # Performance tracking
        self.total_requests = 0
        self.cloud_requests = 0
        self.cache_hits = 0

        # Available engines (lazy-loaded)
        self.available_engines = {}
        self._initialized = False

        # Model prewarming state
        self._whisper_prewarmed = False
        self._whisper_handler = None  # Cached WhisperAudioHandler instance
        self._prewarm_lock = asyncio.Lock()  # Prevent concurrent prewarm attempts

        # Circuit breaker for fault tolerance
        self._circuit_breaker_failures = {}
        self._circuit_breaker_last_failure = {}
        self._circuit_breaker_threshold = 3  # failures before circuit opens
        self._circuit_breaker_timeout = 60  # seconds before retry

        logger.info("🎤 Hybrid STT Router initialized")
        logger.info(f"   Strategy: {self.config.default_strategy.value}")
        logger.info(f"   Models configured: {len(self.config.models)}")

    async def initialize(self) -> bool:
        """
        Async initialization - loads engines and connects to learning DB.

        PERFORMANCE OPTIMIZATIONS:
        - Parallel initialization of database and engine discovery
        - Model prewarming (Whisper) in background to eliminate first-request latency
        - Graceful degradation if prewarm fails

        Returns:
            True if initialization succeeded, False otherwise.
        """
        if self._initialized:
            return True

        try:
            init_start = time.time()

            # PARALLEL INIT: Database connection + engine discovery + model prewarm
            await asyncio.gather(
                self._get_learning_db(),
                self._discover_engines(),
                self._prewarm_whisper_model(),  # Non-blocking prewarm
                return_exceptions=True  # Don't fail if one task fails
            )

            self._initialized = True
            init_time = (time.time() - init_start) * 1000
            logger.info(f"✅ Hybrid STT Router fully initialized in {init_time:.0f}ms")
            logger.info(f"   Available engines: {list(self.available_engines.keys())}")
            logger.info(f"   Whisper prewarmed: {self._whisper_prewarmed}")
            return True
        except Exception as e:
            logger.error(f"❌ Hybrid STT Router initialization failed: {e}")
            return False

    async def _prewarm_whisper_model(self):
        """
        Prewarm Whisper model to eliminate first-request latency.

        ROBUST ASYNC IMPLEMENTATION:
        - Non-blocking with strict timeout enforcement
        - Graceful degradation on failure (service continues without prewarm)
        - Background completion handler for timeout cases
        - Cancellation-safe with proper cleanup
        - Never blocks the voice unlock flow
        """
        async with self._prewarm_lock:
            if self._whisper_prewarmed:
                return

            prewarm_start = time.time()
            prewarm_task = None

            try:
                logger.info("🔥 Prewarming Whisper model (async, non-blocking)...")

                # Import and cache the WhisperAudioHandler singleton
                from .whisper_audio_fix import _whisper_handler

                # Create a cancellable task for the prewarm operation
                async def _do_prewarm():
                    await _whisper_handler.load_model_async(timeout=MODEL_PREWARM_TIMEOUT)
                    return _whisper_handler

                prewarm_task = asyncio.create_task(_do_prewarm())

                # Wait with strict timeout - shield prevents cancellation propagation
                self._whisper_handler = await asyncio.wait_for(
                    asyncio.shield(prewarm_task),
                    timeout=MODEL_PREWARM_TIMEOUT
                )
                self._whisper_prewarmed = True

                prewarm_time = (time.time() - prewarm_start) * 1000
                logger.info(f"✅ Whisper model prewarmed in {prewarm_time:.0f}ms")

            except asyncio.TimeoutError:
                elapsed = (time.time() - prewarm_start) * 1000
                logger.warning(
                    f"⏱️ Whisper prewarm timed out after {elapsed:.0f}ms "
                    f"(continuing without prewarm - will load on first use)"
                )
                # Let background task complete for future requests
                if prewarm_task and not prewarm_task.done():
                    prewarm_task.add_done_callback(self._handle_background_prewarm)

            except asyncio.CancelledError:
                logger.info("🚫 Whisper prewarm cancelled")
                if prewarm_task and not prewarm_task.done():
                    prewarm_task.cancel()
                raise

            except Exception as e:
                elapsed = (time.time() - prewarm_start) * 1000
                logger.warning(f"⚠️ Whisper prewarm failed after {elapsed:.0f}ms: {e}")

    def _handle_background_prewarm(self, task: asyncio.Task):
        """Handle background prewarm completion after timeout."""
        try:
            if task.cancelled() or task.exception():
                return
            result = task.result()
            if result and not self._whisper_prewarmed:
                self._whisper_handler = result
                self._whisper_prewarmed = True
                logger.info("✅ Whisper prewarmed (background completion)")
        except Exception:
            pass  # Silently ignore background failures

    def _check_circuit_breaker(self, engine_name: str) -> bool:
        """
        Check if circuit breaker allows operation for given engine.

        Returns:
            True if operation is allowed, False if circuit is open
        """
        current_time = time.time()

        if engine_name in self._circuit_breaker_failures:
            failures = self._circuit_breaker_failures[engine_name]
            last_failure = self._circuit_breaker_last_failure.get(engine_name, 0)

            if failures >= self._circuit_breaker_threshold:
                if current_time - last_failure < self._circuit_breaker_timeout:
                    logger.debug(f"🔴 Circuit breaker OPEN for {engine_name}")
                    return False
                else:
                    # Reset after timeout
                    self._circuit_breaker_failures[engine_name] = 0
                    logger.info(f"🟢 Circuit breaker RESET for {engine_name}")

        return True

    def _record_circuit_breaker_failure(self, engine_name: str):
        """Record a failure for circuit breaker"""
        self._circuit_breaker_failures[engine_name] = \
            self._circuit_breaker_failures.get(engine_name, 0) + 1
        self._circuit_breaker_last_failure[engine_name] = time.time()

    def _record_circuit_breaker_success(self, engine_name: str):
        """Record a success - reset failure count"""
        if engine_name in self._circuit_breaker_failures:
            self._circuit_breaker_failures[engine_name] = 0

    async def _discover_engines(self):
        """Discover and validate available STT engines."""
        discovered = {}

        # Check Vosk (local, fast, low resource)
        try:
            from vosk import Model
            discovered['vosk'] = {
                'type': 'local',
                'loaded': False,
                'ram_required_gb': 0.5
            }
            logger.info("   ✓ Vosk engine available")
        except ImportError:
            logger.debug("   ✗ Vosk not available")

        # Check Whisper (local, accurate, medium resource)
        try:
            import whisper
            discovered['whisper_local'] = {
                'type': 'local',
                'loaded': False,
                'ram_required_gb': 2.0
            }
            logger.info("   ✓ Whisper (local) engine available")
        except ImportError:
            logger.debug("   ✗ Whisper local not available")

        # Check Google Cloud STT (cloud, high accuracy)
        try:
            from google.cloud import speech_v1
            discovered['google_cloud'] = {
                'type': 'cloud',
                'loaded': True,
                'ram_required_gb': 0.1
            }
            logger.info("   ✓ Google Cloud STT available")
        except ImportError:
            logger.debug("   ✗ Google Cloud STT not available")

        # Check SpeechBrain (local, speaker-aware)
        try:
            from speechbrain.inference.interfaces import Pretrained
            discovered['speechbrain'] = {
                'type': 'local',
                'loaded': False,
                'ram_required_gb': 1.5
            }
            logger.info("   ✓ SpeechBrain engine available")
        except ImportError:
            logger.debug("   ✗ SpeechBrain not available")

        self.available_engines = discovered

    async def _get_learning_db(self):
        """Lazy-load learning database"""
        if self.learning_db is None:
            try:
                from intelligence.learning_database import get_learning_database

                self.learning_db = await get_learning_database()
                logger.info("📚 Learning database connected to STT router")
            except Exception as e:
                logger.error(f"Failed to connect learning database: {e}")
                self.learning_db = None
        return self.learning_db

    def _get_system_resources(self) -> SystemResources:
        """Get current system resource availability"""
        mem = psutil.virtual_memory()
        cpu_percent = psutil.cpu_percent(interval=0.1)

        # Check for GPU (Metal on macOS)
        gpu_available = False
        gpu_memory_gb = 0.0
        try:
            import torch

            if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
                gpu_available = True
                # Estimate Metal shared memory (uses system RAM)
                gpu_memory_gb = mem.available / (1024**3) * 0.5  # Estimate 50% shareable
        except ImportError:
            pass

        # Check network
        network_available = True
        try:
            import socket

            socket.create_connection(("8.8.8.8", 53), timeout=1)
        except OSError:
            network_available = False

        return SystemResources(
            total_ram_gb=mem.total / (1024**3),
            available_ram_gb=mem.available / (1024**3),
            ram_percent_used=mem.percent,
            cpu_percent=cpu_percent,
            gpu_available=gpu_available,
            gpu_memory_gb=gpu_memory_gb,
            network_available=network_available,
        )

    def _select_optimal_model(
        self,
        resources: SystemResources,
        strategy: RoutingStrategy,
        speaker_name: Optional[str] = None,
    ) -> ModelConfig:
        """
        Intelligently select best model based on:
        - Available resources
        - Strategy
        - Speaker priority
        - Historical performance
        """
        available_ram = resources.available_ram_gb

        # WHISPER PRIORITY: Always try Whisper first if available
        # This ensures proper transcription instead of [transcription failed]
        whisper_models = ["whisper-base", "whisper-small", "whisper-tiny"]
        for whisper_name in whisper_models:
            whisper_model = self.config.models.get(whisper_name)
            if whisper_model and whisper_model.ram_required_gb <= available_ram:
                logger.info(f"🎯 Whisper priority: selected {whisper_name} for reliable transcription")
                return whisper_model

        if strategy == RoutingStrategy.SPEED:
            # Fastest model that fits
            model = self.config.get_fastest_model(available_ram)
            if model:
                logger.debug(f"🏃 Speed strategy: selected {model.name}")
                return model

        elif strategy == RoutingStrategy.ACCURACY:
            # Best accuracy (may use cloud)
            if speaker_name in self.config.priority_speakers or resources.network_available:
                model = self.config.get_most_accurate_model(available_ram)
                logger.debug(f"🎯 Accuracy strategy: selected {model.name}")
                return model

        elif strategy == RoutingStrategy.COST:
            # Minimize cloud usage
            model = self.config.get_model_for_ram(available_ram)
            if model and not model.requires_internet:
                logger.debug(f"💰 Cost strategy: selected {model.name}")
                return model

        elif strategy == RoutingStrategy.BALANCED or strategy == RoutingStrategy.ADAPTIVE:
            # Smart selection based on context
            if available_ram >= self.config.min_ram_for_wav2vec:
                # Prefer Wav2Vec (fine-tunable on Derek)
                model = self.config.models.get("wav2vec2-base")
                if model and model.ram_required_gb <= available_ram:
                    logger.debug(f"⚖️  Balanced strategy: selected {model.name}")
                    return model

            # Fallback to Vosk if RAM is tight
            model = self.config.models.get("vosk-small")
            if model:
                logger.debug(f"⚖️  Balanced strategy (RAM constrained): selected {model.name}")
                return model

        # Ultimate fallback: smallest model
        return self.config.get_fastest_model(100.0)  # Get ANY model

    async def _load_engine(self, model: ModelConfig):
        """Lazy-load STT engine"""
        engine_key = f"{model.engine.value}:{model.name}"

        if engine_key in self.engines:
            return self.engines[engine_key]

        logger.info(f"🔧 Loading STT engine: {model.name} ({model.engine.value})")

        try:
            if model.engine == STTEngine.WAV2VEC:
                from .engines.wav2vec_engine import Wav2VecEngine

                engine = Wav2VecEngine(model)
                await engine.initialize()

            elif model.engine == STTEngine.VOSK:
                from .engines.vosk_engine import VoskEngine

                engine = VoskEngine(model)
                await engine.initialize()

            elif model.engine == STTEngine.WHISPER_LOCAL:
                from .engines.whisper_local_engine import WhisperLocalEngine

                engine = WhisperLocalEngine(model)
                await engine.initialize()

            elif model.engine == STTEngine.WHISPER_GCP:
                from .engines.whisper_gcp_engine import WhisperGCPEngine

                engine = WhisperGCPEngine(model)
                await engine.initialize()

            elif model.engine == STTEngine.SPEECHBRAIN:
                from .engines.speechbrain_engine import SpeechBrainEngine

                engine = SpeechBrainEngine(model)
                await engine.initialize()

            else:
                raise ValueError(f"Unknown engine: {model.engine}")

            self.engines[engine_key] = engine
            logger.info(f"✅ Engine loaded: {model.name}")
            return engine

        except Exception as e:
            logger.error(f"Failed to load engine {model.name}: {e}")
            return None

    async def _transcribe_with_engine(
        self, audio_data: bytes, model: ModelConfig, timeout_sec: float = 10.0
    ) -> Optional[STTResult]:
        """Transcribe audio with specific engine (with timeout)"""
        start_time = time.time()

        try:
            engine = await self._load_engine(model)
            if engine is None:
                return None

            # Transcribe with timeout
            result = await asyncio.wait_for(engine.transcribe(audio_data), timeout=timeout_sec)

            latency_ms = (time.time() - start_time) * 1000

            # Update performance stats
            if model.name not in self.performance_stats:
                self.performance_stats[model.name] = {
                    "total_requests": 0,
                    "total_latency_ms": 0,
                    "avg_confidence": 0,
                }

            stats = self.performance_stats[model.name]
            stats["total_requests"] += 1
            stats["total_latency_ms"] += latency_ms
            stats["avg_latency_ms"] = stats["total_latency_ms"] / stats["total_requests"]

            logger.debug(
                f"🎤 {model.name}: '{result.text[:50]}...' (confidence={result.confidence:.2f}, latency={latency_ms:.0f}ms)"
            )

            return result

        except asyncio.TimeoutError:
            latency_ms = (time.time() - start_time) * 1000
            logger.warning(f"⏱️  {model.name} timed out after {latency_ms:.0f}ms")
            return None

        except Exception as e:
            logger.error(f"Error transcribing with {model.name}: {e}")
            return None

    async def _identify_speaker(self, audio_data: bytes) -> Optional[str]:
        """
        Identify speaker from voice using advanced speaker recognition.

        Returns speaker name if recognized, None if unknown.
        """
        try:
            from voice.speaker_recognition import get_speaker_recognition_engine

            speaker_engine = get_speaker_recognition_engine()
            await speaker_engine.initialize()

            # Identify speaker from audio
            speaker_name, confidence = await speaker_engine.identify_speaker(audio_data)

            if speaker_name:
                logger.info(f"🎭 Speaker identified: {speaker_name} (confidence: {confidence:.2f})")

                # Check if this is the owner
                if speaker_engine.is_owner(speaker_name):
                    logger.info(f"👑 Owner detected: {speaker_name}")

                return speaker_name
            else:
                logger.info(
                    f"🎭 Unknown speaker (best confidence: {confidence:.2f}, threshold: {speaker_engine.recognition_threshold})"
                )
                return None

        except Exception as e:
            logger.error(f"Speaker identification failed: {e}")
            import traceback

            traceback.print_exc()
            return None

    async def _record_transcription(
        self, audio_data: bytes, result: STTResult, speaker_name: Optional[str] = None
    ) -> int:
        """Record transcription to learning database"""
        try:
            learning_db = await self._get_learning_db()
            if not learning_db:
                return -1

            # Calculate audio duration (estimate from bytes)
            audio_duration_ms = len(audio_data) / (16000 * 2) * 1000  # 16kHz, 16-bit

            transcription_id = await learning_db.record_voice_transcription(
                audio_data=audio_data,
                transcribed_text=result.text,
                confidence_score=result.confidence,
                audio_duration_ms=audio_duration_ms,
            )

            # Also record as voice sample for Derek if identified
            if speaker_name == "Derek J. Russell":
                await learning_db.record_voice_sample(
                    speaker_name=speaker_name,
                    audio_data=audio_data,
                    transcription=result.text,
                    audio_duration_ms=audio_duration_ms,
                    quality_score=result.confidence,
                )

            return transcription_id

        except Exception as e:
            logger.error(f"Failed to record transcription: {e}")
            return -1

    async def transcribe(
        self,
        audio_data: bytes,
        strategy: Optional[RoutingStrategy] = None,
        speaker_name: Optional[str] = None,
        context: Optional[Dict] = None,
        sample_rate: Optional[int] = None,
        mode: str = 'general',
        skip_speaker_id: bool = True,  # PERFORMANCE: Skip speaker ID by default (done separately)
    ) -> STTResult:
        """
        Main transcription entry point with VAD/windowing mode support.

        PERFORMANCE OPTIMIZATIONS (v2.0):
        - Speaker identification REMOVED from critical path (done in parallel by voice unlock)
        - Uses prewarmed Whisper model when available
        - Circuit breaker prevents repeated failures from blocking
        - Granular timeout protection at each stage

        Args:
            audio_data: Raw audio bytes
            strategy: Routing strategy (SPEED/ACCURACY/COST)
            speaker_name: Known speaker name (for priority routing)
            context: Additional context
            sample_rate: Optional sample rate from frontend
            mode: VAD/windowing mode ('general', 'unlock', 'command')
                  - 'general': 5s window, standard processing
                  - 'unlock': 2s window, ultra-fast
                  - 'command': 3s window, optimized for commands
            skip_speaker_id: Skip speaker identification for faster transcription (default: True)
                             Speaker ID should be done in parallel, not serial.

        Returns:
            STTResult with transcription text, confidence, and metadata
        """
        logger.info(f"🎤 Transcribe called: {len(audio_data)} bytes, mode={mode}")

        self.total_requests += 1
        start_time = time.time()

        # Use configured strategy if not specified
        if strategy is None:
            strategy = self.config.default_strategy

        # Get current system resources (fast, ~100ms)
        resources = self._get_system_resources()
        logger.debug(
            f"📊 Resources: RAM {resources.available_ram_gb:.1f}/{resources.total_ram_gb:.1f}GB, "
            f"CPU {resources.cpu_percent:.0f}%"
        )

        # PERFORMANCE FIX: Speaker identification is REMOVED from transcription path
        # It was adding 3-10+ seconds of latency on every transcription request.
        # Speaker ID is now done in PARALLEL by the voice unlock service, not in serial here.
        # If speaker_name is needed, the caller should provide it or do parallel speaker ID.
        if not skip_speaker_id and speaker_name is None:
            try:
                # Only do speaker ID if explicitly requested and with timeout
                speaker_name = await asyncio.wait_for(
                    self._identify_speaker(audio_data),
                    timeout=SPEAKER_ID_TIMEOUT
                )
                if speaker_name:
                    logger.info(f"👤 Speaker identified: {speaker_name}")
            except asyncio.TimeoutError:
                logger.warning(f"⏱️ Speaker ID timed out after {SPEAKER_ID_TIMEOUT}s (skipping)")
            except Exception as e:
                logger.debug(f"Speaker ID failed: {e} (continuing without)")

        # Select primary model
        primary_model = self._select_optimal_model(resources, strategy, speaker_name)
        logger.debug(f"🎯 Primary model: {primary_model.name} ({primary_model.engine})")

        # Try primary model
        primary_result = await self._transcribe_with_engine(
            audio_data, primary_model, timeout_sec=self.config.max_local_latency_ms / 1000
        )

        # Decide if escalation is needed
        should_escalate = False
        if primary_result is None:
            logger.warning("⚠️  Primary model failed, escalating...")
            should_escalate = True
        elif primary_result.confidence < self.config.min_confidence_local:
            logger.info(
                f"📉 Low confidence ({primary_result.confidence:.2f}), escalating for validation..."
            )
            should_escalate = True
        elif (
            speaker_name in self.config.priority_speakers
            and primary_result.confidence < self.config.high_confidence_threshold
        ):
            logger.info(f"👑 Priority speaker {speaker_name}, escalating for best accuracy...")
            should_escalate = True

        final_result = primary_result

        # Escalate to cloud if needed
        if should_escalate and resources.network_available:
            # Get most accurate model (likely GCP)
            cloud_model = self.config.get_most_accurate_model()

            if cloud_model.requires_internet:
                logger.info(f"☁️  Escalating to cloud model: {cloud_model.name}")
                self.cloud_requests += 1
                self.config.increment_cloud_usage()

                cloud_result = await self._transcribe_with_engine(
                    audio_data, cloud_model, timeout_sec=self.config.max_cloud_latency_ms / 1000
                )

                if cloud_result and cloud_result.confidence > (
                    primary_result.confidence if primary_result else 0
                ):
                    logger.info(
                        f"✅ Cloud result better: {cloud_result.confidence:.2f} > {primary_result.confidence if primary_result else 0:.2f}"
                    )
                    final_result = cloud_result
                elif cloud_result:
                    logger.info(
                        f"⚖️  Keeping primary result (confidence: {primary_result.confidence:.2f})"
                    )
                else:
                    logger.warning("☁️  Cloud transcription failed")

        # Fallback if everything failed
        if final_result is None:
            logger.error("❌ All transcription attempts failed, using fallback")
            # Try smallest/fastest model as last resort
            fallback_model = self.config.models.get("vosk-small")
            if fallback_model:
                final_result = await self._transcribe_with_engine(
                    audio_data, fallback_model, timeout_sec=5.0
                )

        # Still no result? Try Whisper directly as last resort with timeout protection
        if final_result is None:
            logger.warning("⚠️  All engines failed, attempting robust Whisper transcription...")

            # Check circuit breaker for whisper fallback
            if not self._check_circuit_breaker("whisper_fallback"):
                logger.error("🔴 Whisper fallback circuit breaker OPEN - too many recent failures")
                final_result = STTResult(
                    text="[transcription unavailable]",
                    confidence=0.0,
                    engine=STTEngine.WHISPER_LOCAL,
                    model_name="fallback-circuit-open",
                    latency_ms=(time.time() - start_time) * 1000,
                    audio_duration_ms=0,
                    metadata={"error": "circuit_breaker_open", "reason": "too_many_failures"}
                )
            else:
                try:
                    # Use prewarmed handler if available, otherwise import fresh
                    if self._whisper_handler:
                        handler = self._whisper_handler
                        logger.debug("Using prewarmed Whisper handler")
                    else:
                        from .whisper_audio_fix import _whisper_handler as handler
                        logger.debug("Using fresh Whisper handler (not prewarmed)")

                    # Transcribe with timeout protection
                    text = await asyncio.wait_for(
                        handler.transcribe_any_format(
                            audio_data,
                            sample_rate=sample_rate,
                            mode=mode  # Pass mode for VAD + windowing
                        ),
                        timeout=FALLBACK_TIMEOUT
                    )

                    if text:
                        final_result = STTResult(
                            text=text,
                            confidence=0.85,  # Whisper doesn't provide confidence
                            engine=STTEngine.WHISPER_LOCAL,
                            model_name="whisper-robust-fallback",
                            latency_ms=(time.time() - start_time) * 1000,
                            audio_duration_ms=3000,  # Assume 3 seconds
                        )
                        self._record_circuit_breaker_success("whisper_fallback")
                        logger.info(f"✅ Robust Whisper succeeded: '{final_result.text}'")
                    else:
                        logger.warning("⚠️ Robust Whisper returned None - no speech detected")
                        self._record_circuit_breaker_failure("whisper_fallback")
                        final_result = STTResult(
                            text="[no speech detected]",
                            confidence=0.0,
                            engine=STTEngine.WHISPER_LOCAL,
                            model_name="fallback",
                            latency_ms=(time.time() - start_time) * 1000,
                            audio_duration_ms=0,
                            metadata={"error": "no_speech", "reason": "whisper_returned_none"}
                        )

                except asyncio.TimeoutError:
                    logger.error(f"⏱️ Whisper fallback timed out after {FALLBACK_TIMEOUT}s")
                    self._record_circuit_breaker_failure("whisper_fallback")
                    final_result = STTResult(
                        text="[transcription timeout]",
                        confidence=0.0,
                        engine=STTEngine.WHISPER_LOCAL,
                        model_name="fallback-timeout",
                        latency_ms=(time.time() - start_time) * 1000,
                        audio_duration_ms=0,
                        metadata={"error": "timeout", "reason": "whisper_fallback_timeout"}
                    )

                except Exception as e:
                    logger.error(f"❌ Robust Whisper fallback failed: {e}")
                    self._record_circuit_breaker_failure("whisper_fallback")
                    final_result = STTResult(
                        text="[transcription failed]",
                        confidence=0.0,
                        engine=STTEngine.WHISPER_LOCAL,
                        model_name="fallback",
                        latency_ms=(time.time() - start_time) * 1000,
                        audio_duration_ms=0,
                        metadata={"error": str(e), "reason": "audio_validation_failed"}
                    )

        # Calculate total latency
        total_latency_ms = (time.time() - start_time) * 1000
        final_result.latency_ms = total_latency_ms

        # Record to database for learning (async, don't block)
        asyncio.create_task(self._record_transcription(audio_data, final_result, speaker_name))

        # Log final result
        logger.info(
            f"🎯 Final transcription: '{final_result.text[:50]}...' "
            f"(model={final_result.model_name}, confidence={final_result.confidence:.2f}, "
            f"latency={total_latency_ms:.0f}ms)"
        )

        return final_result

    async def record_misheard(
        self, transcription_id: int, what_heard: str, what_meant: str
    ) -> bool:
        """Record when user corrects a misheard transcription"""
        try:
            learning_db = await self._get_learning_db()
            if not learning_db:
                return False

            await learning_db.record_misheard_query(
                transcription_id=transcription_id,
                what_jarvis_heard=what_heard,
                what_user_meant=what_meant,
                correction_method="user_correction",
            )

            logger.info(f"📝 Recorded misheard: '{what_heard}' -> '{what_meant}'")
            return True

        except Exception as e:
            logger.error(f"Failed to record misheard: {e}")
            return False

    async def transcribe_fast(
        self,
        audio_data: bytes,
        sample_rate: Optional[int] = None,
        mode: str = 'unlock',
    ) -> STTResult:
        """
        Fast-path transcription using prewarmed Whisper model.

        This method bypasses the complex routing logic and uses the prewarmed
        Whisper model directly for maximum speed. Best for unlock commands
        where latency is critical.

        PERFORMANCE:
        - Uses prewarmed model (no load time)
        - Skips speaker ID (done separately)
        - Skips model selection (uses Whisper directly)
        - Uses unlock mode (2s window) by default

        Args:
            audio_data: Raw audio bytes
            sample_rate: Optional sample rate from frontend
            mode: VAD/windowing mode (default: 'unlock' for 2s window)

        Returns:
            STTResult with transcription
        """
        start_time = time.time()
        self.total_requests += 1

        try:
            # Use prewarmed handler if available
            if self._whisper_handler:
                handler = self._whisper_handler
            else:
                # Fallback to import if not prewarmed
                from .whisper_audio_fix import _whisper_handler as handler

            # Transcribe with timeout
            text = await asyncio.wait_for(
                handler.transcribe_any_format(
                    audio_data,
                    sample_rate=sample_rate,
                    mode=mode
                ),
                timeout=TRANSCRIPTION_TIMEOUT
            )

            latency_ms = (time.time() - start_time) * 1000

            if text:
                return STTResult(
                    text=text,
                    confidence=0.85,
                    engine=STTEngine.WHISPER_LOCAL,
                    model_name="whisper-fast-path",
                    latency_ms=latency_ms,
                    audio_duration_ms=2000,  # 2s unlock window
                )
            else:
                return STTResult(
                    text="[no speech detected]",
                    confidence=0.0,
                    engine=STTEngine.WHISPER_LOCAL,
                    model_name="whisper-fast-path",
                    latency_ms=latency_ms,
                    audio_duration_ms=0,
                    metadata={"error": "no_speech"}
                )

        except asyncio.TimeoutError:
            return STTResult(
                text="[transcription timeout]",
                confidence=0.0,
                engine=STTEngine.WHISPER_LOCAL,
                model_name="whisper-fast-path-timeout",
                latency_ms=(time.time() - start_time) * 1000,
                audio_duration_ms=0,
                metadata={"error": "timeout"}
            )
        except Exception as e:
            logger.error(f"Fast transcription failed: {e}")
            return STTResult(
                text="[transcription failed]",
                confidence=0.0,
                engine=STTEngine.WHISPER_LOCAL,
                model_name="whisper-fast-path-error",
                latency_ms=(time.time() - start_time) * 1000,
                audio_duration_ms=0,
                metadata={"error": str(e)}
            )

    def get_stats(self) -> Dict:
        """Get router performance statistics"""
        return {
            "total_requests": self.total_requests,
            "cloud_requests": self.cloud_requests,
            "cloud_usage_percent": (
                (self.cloud_requests / self.total_requests * 100) if self.total_requests > 0 else 0
            ),
            "cache_hits": self.cache_hits,
            "loaded_engines": list(self.engines.keys()),
            "performance_by_model": self.performance_stats,
            "config": self.config.to_dict(),
            # New: prewarming status
            "whisper_prewarmed": self._whisper_prewarmed,
            "circuit_breaker_failures": dict(self._circuit_breaker_failures),
        }


# Global singleton
_hybrid_router: Optional[HybridSTTRouter] = None


def get_hybrid_router() -> HybridSTTRouter:
    """Get global hybrid STT router instance"""
    global _hybrid_router
    if _hybrid_router is None:
        _hybrid_router = HybridSTTRouter()
    return _hybrid_router
