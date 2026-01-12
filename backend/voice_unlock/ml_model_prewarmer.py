#!/usr/bin/env python3
"""
ML Model Prewarmer for Voice Unlock
===================================

CRITICAL PERFORMANCE FIX: Pre-loads heavy ML models at system startup
to eliminate the 10-15 second model loading delay on first "unlock my screen" command.

Models Preloaded:
1. Whisper (Speech-to-Text) - ~5-10s load time
2. ECAPA-TDNN (Speaker Verification) - ~5-10s load time

Usage:
    # At backend startup (main.py or startup hook):
    await prewarm_voice_unlock_models()

    # Check if models are ready:
    if is_prewarmed():
        # Fast path - models already in memory

Architecture:
- TRUE PARALLEL LOADING via shared thread pool (4 workers)
- Uses singleton ParallelModelLoader for consistent state
- Model caching to prevent redundant loading
- Progress tracking with callbacks
- Non-blocking async prewarming
- Graceful degradation if prewarming fails
- Environment variable control for skipping prewarm in dev mode

Performance:
- Sequential loading: ~15-20 seconds (Whisper + ECAPA one after another)
- Parallel loading: ~8-12 seconds (both models load simultaneously)
- Cached loading: <100ms (instant if already loaded)
"""

import asyncio
import logging
import os
import time
from datetime import datetime
from typing import Dict, Optional, Any, Callable
from dataclasses import dataclass, field

from backend.core.async_safety import LazyAsyncLock

logger = logging.getLogger(__name__)

# =============================================================================
# PREWARMING CONFIGURATION
# =============================================================================
# Note: These timeouts are for background prewarming at system startup.
# They are intentionally long because:
# 1. First-time model downloads from HuggingFace can be slow
# 2. Model loading is CPU-intensive and varies by hardware
# 3. This happens once at startup, not on user requests
PREWARM_TIMEOUT = 120.0  # Total timeout for all model prewarming
WHISPER_PREWARM_TIMEOUT = 60.0  # Timeout for Whisper model specifically
ECAPA_PREWARM_TIMEOUT = 60.0  # Timeout for ECAPA-TDNN + SpeechBrain models

# Environment variable to skip prewarming (for faster dev restarts)
SKIP_PREWARM_ENV = "JARVIS_SKIP_MODEL_PREWARM"

# Flag to use new parallel loader (set to True for enhanced performance)
USE_PARALLEL_LOADER = True


@dataclass
class PrewarmStatus:
    """Status of model prewarming."""
    whisper_loaded: bool = False
    ecapa_loaded: bool = False
    speaker_encoder_loaded: bool = False
    prewarm_started: bool = False
    prewarm_completed: bool = False
    prewarm_start_time: Optional[float] = None
    prewarm_end_time: Optional[float] = None
    errors: Dict[str, str] = field(default_factory=dict)

    @property
    def all_loaded(self) -> bool:
        """Check if all critical models are loaded."""
        return self.whisper_loaded and (self.ecapa_loaded or self.speaker_encoder_loaded)

    @property
    def prewarm_duration_ms(self) -> Optional[float]:
        """Get prewarming duration in milliseconds."""
        if self.prewarm_start_time and self.prewarm_end_time:
            return (self.prewarm_end_time - self.prewarm_start_time) * 1000
        return None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for logging/stats."""
        return {
            "whisper_loaded": self.whisper_loaded,
            "ecapa_loaded": self.ecapa_loaded,
            "speaker_encoder_loaded": self.speaker_encoder_loaded,
            "all_loaded": self.all_loaded,
            "prewarm_completed": self.prewarm_completed,
            "prewarm_duration_ms": self.prewarm_duration_ms,
            "errors": self.errors
        }


# Global prewarm status
_prewarm_status = PrewarmStatus()
_prewarm_lock = LazyAsyncLock()  # v100.1: Lazy initialization to avoid "no running event loop" error


def get_prewarm_status() -> PrewarmStatus:
    """Get current prewarming status."""
    return _prewarm_status


def is_prewarmed() -> bool:
    """Check if models are prewarmed and ready for instant use."""
    return _prewarm_status.all_loaded


async def _prewarm_whisper() -> bool:
    """
    Prewarm Whisper model for speech-to-text.

    This loads the Whisper model into memory so first transcription
    doesn't incur the 5-10 second model load time.
    """
    global _prewarm_status

    try:
        logger.info("🔥 [PREWARM] Loading Whisper model...")
        start = time.time()

        # Import and get the singleton handler
        from voice.whisper_audio_fix import _whisper_handler

        # Load model in thread pool to avoid blocking
        await asyncio.wait_for(
            asyncio.to_thread(_whisper_handler.load_model),
            timeout=WHISPER_PREWARM_TIMEOUT
        )

        load_time = (time.time() - start) * 1000
        _prewarm_status.whisper_loaded = True
        logger.info(f"✅ [PREWARM] Whisper model loaded in {load_time:.0f}ms")
        return True

    except asyncio.TimeoutError:
        _prewarm_status.errors["whisper"] = f"Timeout after {WHISPER_PREWARM_TIMEOUT}s"
        logger.warning(f"⏱️ [PREWARM] Whisper prewarm timed out after {WHISPER_PREWARM_TIMEOUT}s")
        return False
    except Exception as e:
        _prewarm_status.errors["whisper"] = str(e)
        logger.error(f"❌ [PREWARM] Whisper prewarm failed: {e}")
        return False


async def _prewarm_ecapa() -> bool:
    """
    Prewarm ECAPA-TDNN model for speaker verification.

    This loads the SpeechBrain speaker encoder into memory so first
    voice verification doesn't incur the 5-10 second model load time.
    """
    global _prewarm_status

    try:
        logger.info("🔥 [PREWARM] Loading ECAPA-TDNN speaker encoder...")
        start = time.time()

        # Try new speaker verification service first
        try:
            from voice.speaker_verification_service import SpeakerVerificationService

            # Get or create singleton instance
            service = SpeakerVerificationService()

            # The service has an async initialize() method that:
            # 1. Initializes SpeechBrain engine
            # 2. Calls preload_speaker_encoder_async() internally
            # 3. Loads speaker profiles
            await asyncio.wait_for(
                service.initialize(preload_encoder=True),
                timeout=ECAPA_PREWARM_TIMEOUT
            )

            load_time = (time.time() - start) * 1000
            _prewarm_status.ecapa_loaded = True
            _prewarm_status.speaker_encoder_loaded = True
            logger.info(f"✅ [PREWARM] ECAPA-TDNN encoder loaded in {load_time:.0f}ms")
            return True

        except ImportError:
            # Fall back to legacy speaker recognition
            from voice.speaker_recognition import get_speaker_recognition_engine

            engine = get_speaker_recognition_engine()
            await asyncio.wait_for(
                engine.initialize(),
                timeout=ECAPA_PREWARM_TIMEOUT
            )

            load_time = (time.time() - start) * 1000
            _prewarm_status.ecapa_loaded = True
            _prewarm_status.speaker_encoder_loaded = True
            logger.info(f"✅ [PREWARM] Legacy speaker engine loaded in {load_time:.0f}ms")
            return True

    except asyncio.TimeoutError:
        _prewarm_status.errors["ecapa"] = f"Timeout after {ECAPA_PREWARM_TIMEOUT}s"
        logger.warning(f"⏱️ [PREWARM] ECAPA-TDNN prewarm timed out after {ECAPA_PREWARM_TIMEOUT}s")
        return False
    except Exception as e:
        _prewarm_status.errors["ecapa"] = str(e)
        logger.error(f"❌ [PREWARM] ECAPA-TDNN prewarm failed: {e}")
        return False


async def _prewarm_hybrid_stt_router() -> bool:
    """
    Initialize the Hybrid STT Router with model prewarming.

    This ensures the router is ready for instant transcription requests.
    """
    global _prewarm_status

    try:
        logger.info("🔥 [PREWARM] Initializing Hybrid STT Router...")
        start = time.time()

        from voice.hybrid_stt_router import get_hybrid_router

        router = get_hybrid_router()
        await asyncio.wait_for(
            router.initialize(),
            timeout=WHISPER_PREWARM_TIMEOUT
        )

        load_time = (time.time() - start) * 1000

        # Check if Whisper was prewarmed by the router
        stats = router.get_stats()
        if stats.get("whisper_prewarmed"):
            _prewarm_status.whisper_loaded = True
            logger.info(f"✅ [PREWARM] Hybrid STT Router ready (Whisper prewarmed) in {load_time:.0f}ms")
        else:
            logger.warning(f"⚠️ [PREWARM] Hybrid STT Router ready but Whisper NOT prewarmed in {load_time:.0f}ms")

        return True

    except asyncio.TimeoutError:
        logger.warning(f"⏱️ [PREWARM] Hybrid STT Router timed out after {WHISPER_PREWARM_TIMEOUT}s")
        return False
    except Exception as e:
        logger.error(f"❌ [PREWARM] Hybrid STT Router initialization failed: {e}")
        return False


# =============================================================================
# ENHANCED PARALLEL MODEL LOADING
# =============================================================================

async def _prewarm_with_parallel_loader(
    progress_callback: Optional[Callable[[str, str, float], None]] = None
) -> bool:
    """
    Use the ParallelModelLoader for TRUE concurrent model loading.

    This is the optimized path that loads Whisper and ECAPA-TDNN
    simultaneously using a shared thread pool with 4 workers.

    Performance:
    - Sequential: ~15-20 seconds (one model at a time)
    - Parallel: ~8-12 seconds (both models simultaneously)
    - Cached: <100ms (instant if already loaded)

    Args:
        progress_callback: Optional callback(model_name, state, progress_pct)

    Returns:
        True if all models loaded successfully
    """
    global _prewarm_status

    try:
        logger.info("🚀 [PARALLEL-PREWARM] Using ParallelModelLoader for TRUE concurrent loading")

        from voice.parallel_model_loader import (
            get_model_loader,
            load_all_voice_models,
            ModelState
        )

        # Get the global loader instance
        loader = get_model_loader()

        # Check if models are already cached
        whisper_cached = loader.is_cached("whisper")
        ecapa_cached = loader.is_cached("ecapa_encoder")

        if whisper_cached and ecapa_cached:
            logger.info("✅ [PARALLEL-PREWARM] All models already cached - instant path!")
            _prewarm_status.whisper_loaded = True
            _prewarm_status.ecapa_loaded = True
            _prewarm_status.speaker_encoder_loaded = True
            return True

        # Define progress callback wrapper
        def on_progress(model_name: str, state: ModelState, pct: float):
            state_str = state.value if hasattr(state, 'value') else str(state)
            logger.info(f"   [{model_name}] {state_str} ({pct*100:.0f}%)")
            if progress_callback:
                progress_callback(model_name, state_str, pct)

        # Load all models in TRUE parallel
        start = time.time()
        result = await load_all_voice_models()
        total_time = (time.time() - start) * 1000

        # Update prewarm status based on results
        if "whisper" in result.loaded_models:
            _prewarm_status.whisper_loaded = True
        if "ecapa_encoder" in result.loaded_models:
            _prewarm_status.ecapa_loaded = True
            _prewarm_status.speaker_encoder_loaded = True

        # Log detailed results
        logger.info(f"📊 [PARALLEL-PREWARM] Results:")
        logger.info(f"   Total time: {total_time:.0f}ms")
        logger.info(f"   Parallel speedup: {result.parallel_speedup:.2f}x")
        logger.info(f"   Loaded models: {result.loaded_models}")

        if result.failed_models:
            logger.warning(f"   Failed models: {result.failed_models}")
            for model_name in result.failed_models:
                model_result = result.results.get(model_name)
                if model_result and model_result.error:
                    _prewarm_status.errors[model_name] = model_result.error

        # Get loader stats
        stats = loader.get_stats()
        logger.info(f"   Cache hits: {stats['cache_hits']}")
        logger.info(f"   Time saved by parallelism: {stats['total_time_saved_ms']:.0f}ms")

        return result.all_success

    except ImportError as e:
        logger.warning(f"⚠️ [PARALLEL-PREWARM] ParallelModelLoader not available: {e}")
        logger.info("   Falling back to legacy sequential loading...")
        return False
    except Exception as e:
        logger.error(f"❌ [PARALLEL-PREWARM] Error: {e}")
        _prewarm_status.errors["parallel_loader"] = str(e)
        return False


async def prewarm_voice_unlock_models(
    parallel: bool = True,
    skip_whisper: bool = False,
    skip_ecapa: bool = False
) -> PrewarmStatus:
    """
    Prewarm all ML models required for voice unlock.

    Call this at backend startup to ensure models are loaded
    BEFORE the user says "unlock my screen".

    Args:
        parallel: If True, load models in parallel (faster but more memory)
        skip_whisper: Skip Whisper prewarming
        skip_ecapa: Skip ECAPA-TDNN prewarming

    Returns:
        PrewarmStatus with loading results
    """
    global _prewarm_status

    # Check if prewarming should be skipped
    if os.getenv(SKIP_PREWARM_ENV, "").lower() in ("true", "1", "yes"):
        logger.info("⏭️ [PREWARM] Skipping model prewarm (JARVIS_SKIP_MODEL_PREWARM=true)")
        return _prewarm_status

    async with _prewarm_lock:
        # Already completed?
        if _prewarm_status.prewarm_completed:
            logger.debug("[PREWARM] Already completed, returning cached status")
            return _prewarm_status

        # Already in progress? Wait for completion
        if _prewarm_status.prewarm_started and not _prewarm_status.prewarm_completed:
            logger.debug("[PREWARM] Already in progress, waiting...")
            # Release lock and wait a bit
            await asyncio.sleep(0.1)
            return _prewarm_status

        _prewarm_status.prewarm_started = True
        _prewarm_status.prewarm_start_time = time.time()

        logger.info("=" * 60)
        logger.info("🚀 [PREWARM] Starting Voice Unlock ML Model Prewarming")
        logger.info("=" * 60)

        # =====================================================================
        # OPTIMIZED PATH: Use ParallelModelLoader for TRUE concurrent loading
        # =====================================================================
        if USE_PARALLEL_LOADER and parallel and not skip_whisper and not skip_ecapa:
            logger.info("🔥 [PREWARM] Using OPTIMIZED ParallelModelLoader path")
            success = await _prewarm_with_parallel_loader()

            if success:
                logger.info("✅ [PREWARM] ParallelModelLoader completed successfully")
            else:
                logger.warning("⚠️ [PREWARM] ParallelModelLoader failed, falling back to legacy...")
                # Fall through to legacy path below

        # =====================================================================
        # LEGACY PATH: Individual task loading (fallback or specific skip flags)
        # =====================================================================
        if not _prewarm_status.all_loaded:
            prewarm_tasks = []

            # Add prewarming tasks based on configuration
            if not skip_whisper and not _prewarm_status.whisper_loaded:
                # Use the hybrid STT router which handles Whisper prewarming
                prewarm_tasks.append(("Hybrid STT Router", _prewarm_hybrid_stt_router()))

            if not skip_ecapa and not _prewarm_status.ecapa_loaded:
                prewarm_tasks.append(("ECAPA-TDNN", _prewarm_ecapa()))

            if prewarm_tasks:
                if parallel and len(prewarm_tasks) > 1:
                    # Run all prewarming tasks in parallel
                    logger.info(f"🔄 [PREWARM] Loading {len(prewarm_tasks)} models in PARALLEL (legacy)...")

                    results = await asyncio.gather(
                        *[task for _, task in prewarm_tasks],
                        return_exceptions=True
                    )

                    for i, (name, _) in enumerate(prewarm_tasks):
                        if isinstance(results[i], Exception):
                            logger.error(f"❌ [PREWARM] {name} failed: {results[i]}")
                            _prewarm_status.errors[name.lower().replace(" ", "_")] = str(results[i])
                else:
                    # Run sequentially
                    for name, task in prewarm_tasks:
                        try:
                            await task
                        except Exception as e:
                            logger.error(f"❌ [PREWARM] {name} failed: {e}")
                            _prewarm_status.errors[name.lower().replace(" ", "_")] = str(e)

        _prewarm_status.prewarm_end_time = time.time()
        _prewarm_status.prewarm_completed = True

        duration_ms = _prewarm_status.prewarm_duration_ms

        logger.info("=" * 60)
        if _prewarm_status.all_loaded:
            logger.info(f"✅ [PREWARM] All models prewarmed successfully in {duration_ms:.0f}ms")
            logger.info(f"   • Whisper: {'✓' if _prewarm_status.whisper_loaded else '✗'}")
            logger.info(f"   • ECAPA-TDNN: {'✓' if _prewarm_status.ecapa_loaded else '✗'}")
            logger.info("   → First 'unlock my screen' will be INSTANT!")
        else:
            logger.warning(f"⚠️ [PREWARM] Partial prewarm in {duration_ms:.0f}ms - some models may load on first use")
            logger.warning(f"   • Whisper: {'✓' if _prewarm_status.whisper_loaded else '✗ (will load on first use)'}")
            logger.warning(f"   • ECAPA-TDNN: {'✓' if _prewarm_status.ecapa_loaded else '✗ (will load on first use)'}")
            if _prewarm_status.errors:
                logger.warning(f"   • Errors: {_prewarm_status.errors}")
        logger.info("=" * 60)

        return _prewarm_status


async def prewarm_voice_unlock_models_background():
    """
    Start model prewarming in background without blocking.

    Use this at system startup to begin loading models while
    other initialization continues.
    """
    asyncio.create_task(prewarm_voice_unlock_models())
    logger.info("🔄 [PREWARM] Model prewarming started in background")


def reset_prewarm_status():
    """Reset prewarm status (for testing)."""
    global _prewarm_status
    _prewarm_status = PrewarmStatus()
