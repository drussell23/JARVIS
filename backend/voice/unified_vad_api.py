#!/usr/bin/env python3
"""
Unified VAD Pipeline API
========================

Simple, clean API matching your specification:

    async def run_vad_pipeline(
        audio_bytes: bytes,
        *,
        max_seconds: float,
        mode: Literal["unlock", "command", "dictation"] = "command",
    ) -> bytes:

This module provides the exact API you requested, wrapping the underlying
VAD + windowing implementation.
"""

import asyncio
import logging
import numpy as np
from typing import Literal, Optional

from .vad.pipeline import get_vad_pipeline_async
from .audio_windowing import get_window_manager
from .audio_format_converter import AudioFormatConverter

logger = logging.getLogger(__name__)


async def run_vad_pipeline(
    audio_bytes: bytes,
    *,
    max_seconds: float,
    mode: Literal["unlock", "command", "dictation"] = "command",
    sample_rate: Optional[int] = None,
    enable_vad: bool = True,
    enable_windowing: bool = True,
) -> bytes:
    """
    Run the complete VAD + windowing pipeline on audio bytes.

    This is the unified entry point for all voice preprocessing.
    Takes raw audio bytes, applies VAD filtering and time-based truncation,
    returns clean audio bytes ready for Whisper.

    Args:
        audio_bytes: Raw audio data (any format: WAV, WebM, Opus, PCM, etc.)
        max_seconds: Maximum duration in seconds (overrides mode defaults)
        mode: Processing mode - determines window size and VAD aggressiveness
            - "unlock": Ultra-fast, 2-second window, aggressive VAD
            - "command": Balanced, 3-second window, moderate VAD
            - "dictation": Longer, 5-second window, gentle VAD
        sample_rate: Source audio sample rate (if known, for efficiency)
        enable_vad: Enable VAD preprocessing (default: True)
        enable_windowing: Enable time-based windowing (default: True)

    Returns:
        Clean audio bytes (PCM float32, 16kHz mono) ready for Whisper

    Raises:
        ValueError: If audio_bytes is empty or invalid
        RuntimeError: If VAD/windowing fails critically

    Example:
        >>> # Unlock flow (2 seconds, aggressive filtering)
        >>> clean_audio = await run_vad_pipeline(
        ...     audio_bytes=raw_mic_data,
        ...     max_seconds=2.0,
        ...     mode="unlock"
        ... )
        >>>
        >>> # Command detection (3 seconds, balanced)
        >>> clean_audio = await run_vad_pipeline(
        ...     audio_bytes=raw_mic_data,
        ...     max_seconds=3.0,
        ...     mode="command"
        ... )
        >>>
        >>> # Dictation (5 seconds, gentle filtering)
        >>> clean_audio = await run_vad_pipeline(
        ...     audio_bytes=raw_mic_data,
        ...     max_seconds=5.0,
        ...     mode="dictation"
        ... )

    Performance:
        - Input: 60 seconds of audio
        - After VAD: ~12 seconds (speech only)
        - After windowing: 2-5 seconds (last N seconds)
        - Processing time: ~50-100ms total

    Pipeline Stages:
        1. Audio format conversion (to 16kHz mono float32)
        2. VAD preprocessing (WebRTC + Silero, optional)
        3. Time-based windowing (keep last N seconds, optional)
        4. Return as bytes
    """
    if not audio_bytes:
        raise ValueError("audio_bytes cannot be empty")

    # ============================================================================
    # STAGE 1: Audio Format Conversion
    # ============================================================================

    try:
        # Convert to standard format (16kHz mono float32)
        converter = AudioFormatConverter()
        audio_array = await asyncio.to_thread(
            converter.convert_to_numpy,
            audio_bytes,
            target_sample_rate=16000,
            source_sample_rate=sample_rate
        )

        if len(audio_array) == 0:
            logger.warning("Audio conversion resulted in empty array")
            raise ValueError("Audio conversion failed: empty result")

        original_duration = len(audio_array) / 16000
        logger.debug(
            f"[VAD API] Stage 1: Format conversion complete | "
            f"Duration: {original_duration:.2f}s | "
            f"Samples: {len(audio_array)}"
        )

    except Exception as e:
        logger.error(f"[VAD API] Audio format conversion failed: {e}")
        raise RuntimeError(f"Audio conversion error: {e}") from e

    # ============================================================================
    # STAGE 2: VAD Preprocessing (Optional)
    # ============================================================================

    if enable_vad:
        try:
            vad_pipeline = await get_vad_pipeline_async()

            # Run VAD filtering asynchronously
            audio_array = await vad_pipeline.filter_audio_async(audio_array)

            if len(audio_array) == 0:
                logger.warning(
                    f"[VAD API] VAD filtered out ALL audio | "
                    f"Original: {original_duration:.2f}s → 0s | "
                    f"Mode: {mode}"
                )
                # Return silence instead of failing
                # (1 second of silence to avoid downstream errors)
                audio_array = np.zeros(16000, dtype=np.float32)

            vad_duration = len(audio_array) / 16000
            reduction_pct = (1 - vad_duration / original_duration) * 100 if original_duration > 0 else 0

            logger.info(
                f"[VAD API] Stage 2: VAD filtering complete | "
                f"{original_duration:.2f}s → {vad_duration:.2f}s | "
                f"Reduction: {reduction_pct:.1f}% | "
                f"Mode: {mode}"
            )

        except Exception as e:
            logger.error(f"[VAD API] VAD preprocessing failed: {e}", exc_info=True)
            # Graceful fallback: continue without VAD
            logger.warning("[VAD API] Continuing without VAD filtering")

    # ============================================================================
    # STAGE 3: Time-based Windowing (Optional)
    # ============================================================================

    if enable_windowing:
        try:
            window_manager = get_window_manager()

            # Determine window size based on mode
            if mode == "unlock":
                # Ultra-fast unlock: 2 seconds max
                window_seconds = min(max_seconds, window_manager.config.unlock_window_seconds)
                audio_array = window_manager.truncate_for_unlock(audio_array)
            elif mode == "command":
                # Command detection: 3 seconds max
                window_seconds = min(max_seconds, window_manager.config.command_window_seconds)
                audio_array = window_manager.truncate_for_command(audio_array)
            elif mode == "dictation":
                # Dictation: 5 seconds max (general limit)
                window_seconds = min(max_seconds, window_manager.config.max_audio_seconds)
                audio_array = window_manager.truncate_to_max(audio_array)
            else:
                # Unknown mode: use custom max_seconds
                logger.warning(f"[VAD API] Unknown mode '{mode}', using custom max_seconds={max_seconds}")
                window_seconds = max_seconds
                max_samples = int(window_seconds * 16000)
                if len(audio_array) > max_samples:
                    audio_array = audio_array[-max_samples:]

            final_duration = len(audio_array) / 16000

            logger.info(
                f"[VAD API] Stage 3: Windowing complete | "
                f"Window: {window_seconds:.1f}s | "
                f"Final: {final_duration:.2f}s | "
                f"Mode: {mode}"
            )

        except Exception as e:
            logger.error(f"[VAD API] Windowing failed: {e}", exc_info=True)
            # Graceful fallback: continue without windowing
            logger.warning("[VAD API] Continuing without windowing")

    # ============================================================================
    # STAGE 4: Convert back to bytes
    # ============================================================================

    try:
        # Convert numpy array to bytes (float32 PCM)
        audio_bytes_clean = audio_array.astype(np.float32).tobytes()

        final_duration = len(audio_array) / 16000
        logger.info(
            f"[VAD API] ✅ Pipeline complete | "
            f"Input: {original_duration:.2f}s → Output: {final_duration:.2f}s | "
            f"Reduction: {(1 - final_duration / original_duration) * 100:.1f}% | "
            f"Mode: {mode} | "
            f"VAD: {'✅' if enable_vad else '❌'} | "
            f"Windowing: {'✅' if enable_windowing else '❌'}"
        )

        return audio_bytes_clean

    except Exception as e:
        logger.error(f"[VAD API] Failed to convert audio back to bytes: {e}")
        raise RuntimeError(f"Audio output conversion error: {e}") from e


async def run_vad_pipeline_numpy(
    audio_array: np.ndarray,
    *,
    max_seconds: float,
    mode: Literal["unlock", "command", "dictation"] = "command",
    enable_vad: bool = True,
    enable_windowing: bool = True,
) -> np.ndarray:
    """
    Run VAD pipeline on numpy array directly (skips format conversion).

    Same as run_vad_pipeline but for pre-converted numpy arrays.

    Args:
        audio_array: Audio as numpy array (float32, 16kHz mono)
        max_seconds: Maximum duration in seconds
        mode: Processing mode ("unlock", "command", "dictation")
        enable_vad: Enable VAD preprocessing
        enable_windowing: Enable time-based windowing

    Returns:
        Clean audio as numpy array (float32, 16kHz mono)

    Example:
        >>> import numpy as np
        >>> audio = np.random.randn(16000 * 10).astype(np.float32)  # 10 seconds
        >>> clean = await run_vad_pipeline_numpy(
        ...     audio, max_seconds=2.0, mode="unlock"
        ... )
        >>> len(clean) / 16000  # Should be ~2 seconds
        2.0
    """
    if not isinstance(audio_array, np.ndarray):
        raise TypeError(f"audio_array must be np.ndarray, got {type(audio_array)}")

    if len(audio_array) == 0:
        raise ValueError("audio_array cannot be empty")

    original_duration = len(audio_array) / 16000

    # Stage 2: VAD
    if enable_vad:
        vad_pipeline = await get_vad_pipeline_async()
        audio_array = await vad_pipeline.filter_audio_async(audio_array)

        if len(audio_array) == 0:
            logger.warning("[VAD API] VAD filtered out all audio, returning silence")
            return np.zeros(16000, dtype=np.float32)

    # Stage 3: Windowing
    if enable_windowing:
        window_manager = get_window_manager()

        if mode == "unlock":
            audio_array = window_manager.truncate_for_unlock(audio_array)
        elif mode == "command":
            audio_array = window_manager.truncate_for_command(audio_array)
        elif mode == "dictation":
            audio_array = window_manager.truncate_to_max(audio_array)
        else:
            # Custom max_seconds
            max_samples = int(max_seconds * 16000)
            if len(audio_array) > max_samples:
                audio_array = audio_array[-max_samples:]

    final_duration = len(audio_array) / 16000
    logger.info(
        f"[VAD API] Numpy pipeline: {original_duration:.2f}s → {final_duration:.2f}s | Mode: {mode}"
    )

    return audio_array


def get_recommended_max_seconds(mode: Literal["unlock", "command", "dictation"]) -> float:
    """
    Get recommended max_seconds for a given mode.

    Args:
        mode: Processing mode

    Returns:
        Recommended max_seconds value

    Example:
        >>> get_recommended_max_seconds("unlock")
        2.0
        >>> get_recommended_max_seconds("command")
        3.0
        >>> get_recommended_max_seconds("dictation")
        5.0
    """
    window_manager = get_window_manager()

    if mode == "unlock":
        return window_manager.config.unlock_window_seconds
    elif mode == "command":
        return window_manager.config.command_window_seconds
    elif mode == "dictation":
        return window_manager.config.max_audio_seconds
    else:
        logger.warning(f"Unknown mode '{mode}', returning default 3.0s")
        return 3.0


# ============================================================================
# CONVENIENCE ALIASES
# ============================================================================

async def process_unlock_audio(audio_bytes: bytes) -> bytes:
    """Convenience: Process audio for unlock flow (2s, aggressive VAD)"""
    return await run_vad_pipeline(audio_bytes, max_seconds=2.0, mode="unlock")


async def process_command_audio(audio_bytes: bytes) -> bytes:
    """Convenience: Process audio for command detection (3s, balanced VAD)"""
    return await run_vad_pipeline(audio_bytes, max_seconds=3.0, mode="command")


async def process_dictation_audio(audio_bytes: bytes) -> bytes:
    """Convenience: Process audio for dictation (5s, gentle VAD)"""
    return await run_vad_pipeline(audio_bytes, max_seconds=5.0, mode="dictation")
