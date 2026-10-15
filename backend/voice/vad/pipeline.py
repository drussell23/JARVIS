#!/usr/bin/env python3
"""
VAD Pipeline - Orchestrates WebRTC-VAD + Silero VAD
Filters silence and noise before audio reaches Whisper
"""
from __future__ import annotations

import logging
import threading
from typing import Iterator, Optional, Literal
import numpy as np
import asyncio
from dataclasses import dataclass

from .base import VADBase, VADConfig, SpeechSegment
from .webrtc_vad import WebRTCVAD
from .silero_vad import AsyncSileroVAD

logger = logging.getLogger(__name__)


@dataclass
class VADPipelineConfig:
    """Configuration for VAD pipeline"""
    # Primary VAD (fast, lightweight)
    primary_vad: Literal["webrtc", "silero"] = "webrtc"

    # Secondary VAD (more accurate refinement)
    use_secondary_vad: bool = True
    secondary_vad: Literal["silero", "none"] = "silero"

    # VAD settings
    vad_config: VADConfig = None

    # Strategy for combining VADs
    combination_strategy: Literal["sequential", "parallel", "voting"] = "sequential"

    # Minimum speech confidence after VAD filtering
    min_confidence: float = 0.3

    def __post_init__(self):
        if self.vad_config is None:
            self.vad_config = VADConfig()


class VADPipeline:
    """
    Multi-stage VAD pipeline for robust speech detection

    Architecture:
    1. Primary VAD (WebRTC): Fast frame-level filtering
    2. Secondary VAD (Silero): Neural refinement for accuracy
    3. Speech segment extraction and confidence scoring

    Benefits:
    - WebRTC provides fast initial filtering (~1ms/frame)
    - Silero refines decisions for better accuracy (~5-10ms/segment)
    - Combined approach: speed + accuracy
    - Removes silence/noise before Whisper sees audio
    """

    def __init__(self, config: VADPipelineConfig):
        """
        Initialize VAD pipeline

        Args:
            config: Pipeline configuration
        """
        self.config = config
        self.primary_vad: Optional[VADBase] = None
        self.secondary_vad: Optional[VADBase] = None

        self._initialize_vads()

        logger.info(f"🎯 VAD Pipeline initialized:")
        logger.info(f"   Primary: {config.primary_vad}")
        logger.info(f"   Secondary: {config.secondary_vad if config.use_secondary_vad else 'None'}")
        logger.info(f"   Strategy: {config.combination_strategy}")
        logger.info(f"   Min confidence: {config.min_confidence}")

    def _initialize_vads(self):
        """Initialize primary and secondary VAD models"""
        try:
            # Initialize primary VAD
            if self.config.primary_vad == "webrtc":
                self.primary_vad = WebRTCVAD(self.config.vad_config)
                logger.info("✅ Primary VAD (WebRTC) initialized")
            elif self.config.primary_vad == "silero":
                self.primary_vad = AsyncSileroVAD(self.config.vad_config)
                logger.info("✅ Primary VAD (Silero) initialized")

            # Initialize secondary VAD if enabled
            if self.config.use_secondary_vad:
                if self.config.secondary_vad == "silero":
                    self.secondary_vad = AsyncSileroVAD(self.config.vad_config)
                    logger.info("✅ Secondary VAD (Silero) initialized")

        except Exception as e:
            logger.error(f"Failed to initialize VAD models: {e}")
            # Fallback to primary only
            self.config.use_secondary_vad = False
            logger.warning("⚠️ Running with primary VAD only (no secondary refinement)")

    async def filter_audio_async(self, audio: np.ndarray) -> np.ndarray:
        """
        Filter audio to remove silence/noise (async version)

        Process:
        1. Run primary VAD to get initial speech segments
        2. Optionally refine with secondary VAD
        3. Concatenate speech-only segments
        4. Return filtered audio

        Args:
            audio: Raw audio (float32, normalized to [-1, 1])

        Returns:
            Filtered audio containing only speech
        """
        if len(audio) == 0:
            logger.warning("Empty audio provided to VAD pipeline")
            return np.array([], dtype=np.float32)

        def _filter_sync():
            return self.filter_audio(audio)

        return await asyncio.to_thread(_filter_sync)

    def filter_audio(self, audio: np.ndarray) -> np.ndarray:
        """
        Filter audio to remove silence/noise (sync version)

        Args:
            audio: Raw audio (float32, normalized to [-1, 1])

        Returns:
            Filtered audio containing only speech
        """
        if len(audio) == 0:
            return np.array([], dtype=np.float32)

        logger.debug(f"🔊 VAD Pipeline filtering {len(audio)} samples ({len(audio) / self.config.vad_config.sample_rate:.2f}s)")

        # Strategy: Sequential (default and recommended)
        if self.config.combination_strategy == "sequential":
            return self._filter_sequential(audio)

        # Strategy: Parallel (both VADs run independently, take intersection)
        elif self.config.combination_strategy == "parallel":
            return self._filter_parallel(audio)

        # Strategy: Voting (frame-by-frame voting)
        elif self.config.combination_strategy == "voting":
            return self._filter_voting(audio)

        else:
            logger.error(f"Unknown combination strategy: {self.config.combination_strategy}")
            return audio

    def _filter_sequential(self, audio: np.ndarray) -> np.ndarray:
        """
        Sequential filtering: Primary VAD → Secondary VAD

        This is the recommended approach:
        1. WebRTC quickly removes obvious silence
        2. Silero refines the remaining audio for better accuracy

        Args:
            audio: Raw audio

        Returns:
            Filtered audio
        """
        # Stage 1: Primary VAD (fast filtering)
        if self.primary_vad is None:
            logger.warning("Primary VAD not initialized")
            return audio

        primary_filtered = self.primary_vad.filter_silence(audio)

        if len(primary_filtered) == 0:
            logger.info("❌ Primary VAD: No speech detected")
            return np.array([], dtype=np.float32)

        reduction_ratio = len(primary_filtered) / len(audio) * 100
        logger.debug(f"✅ Primary VAD: {reduction_ratio:.1f}% of audio retained")

        # Stage 2: Secondary VAD (refinement) - optional
        if not self.config.use_secondary_vad or self.secondary_vad is None:
            return primary_filtered

        secondary_filtered = self.secondary_vad.filter_silence(primary_filtered)

        if len(secondary_filtered) == 0:
            logger.info("❌ Secondary VAD: No speech detected (filtered out by Silero)")
            return np.array([], dtype=np.float32)

        final_ratio = len(secondary_filtered) / len(audio) * 100
        logger.debug(f"✅ Secondary VAD: {final_ratio:.1f}% of original audio retained")

        return secondary_filtered

    def _filter_parallel(self, audio: np.ndarray) -> np.ndarray:
        """
        Parallel filtering: Run both VADs, take intersection of speech segments

        Args:
            audio: Raw audio

        Returns:
            Filtered audio (intersection of both VADs)
        """
        if self.primary_vad is None:
            return audio

        # Get speech segments from both VADs
        primary_segments = list(self.primary_vad.process_audio(audio))

        if not self.config.use_secondary_vad or self.secondary_vad is None:
            # Only primary available
            filtered = np.concatenate([seg.audio_data for seg in primary_segments]) if primary_segments else np.array([], dtype=np.float32)
            return filtered

        secondary_segments = list(self.secondary_vad.process_audio(audio))

        # Find intersection of segments (both VADs agree on speech)
        intersected_segments = self._intersect_segments(primary_segments, secondary_segments, audio)

        if not intersected_segments:
            logger.info("❌ Parallel VAD: No overlapping speech segments")
            return np.array([], dtype=np.float32)

        # Concatenate intersected segments
        filtered = np.concatenate([seg.audio_data for seg in intersected_segments])
        logger.debug(f"✅ Parallel VAD: {len(intersected_segments)} segments retained")

        return filtered

    def _filter_voting(self, audio: np.ndarray) -> np.ndarray:
        """
        Voting filtering: Frame-by-frame voting between VADs

        Args:
            audio: Raw audio

        Returns:
            Filtered audio (frames where majority vote = speech)
        """
        if self.primary_vad is None:
            return audio

        # Frame-by-frame processing
        frame_size = self.config.vad_config.frame_duration_ms * self.config.vad_config.sample_rate // 1000
        num_frames = len(audio) // frame_size

        speech_frames = []

        for i in range(num_frames):
            start_idx = i * frame_size
            end_idx = start_idx + frame_size
            frame = audio[start_idx:end_idx]

            # Get votes from each VAD
            primary_vote, primary_conf = self.primary_vad.is_speech(frame)
            votes = [primary_vote]

            if self.config.use_secondary_vad and self.secondary_vad:
                secondary_vote, secondary_conf = self.secondary_vad.is_speech(frame)
                votes.append(secondary_vote)

            # Majority vote
            if sum(votes) > len(votes) / 2:
                speech_frames.append(frame)

        if not speech_frames:
            logger.info("❌ Voting VAD: No speech frames detected")
            return np.array([], dtype=np.float32)

        filtered = np.concatenate(speech_frames)
        logger.debug(f"✅ Voting VAD: {len(speech_frames)}/{num_frames} frames retained")

        return filtered

    def _intersect_segments(
        self,
        segments_a: list[SpeechSegment],
        segments_b: list[SpeechSegment],
        audio: np.ndarray
    ) -> list[SpeechSegment]:
        """
        Find intersection of two sets of speech segments

        Args:
            segments_a: Speech segments from first VAD
            segments_b: Speech segments from second VAD
            audio: Original audio for extracting intersected regions

        Returns:
            Intersected speech segments
        """
        intersected = []

        for seg_a in segments_a:
            for seg_b in segments_b:
                # Check if segments overlap
                start = max(seg_a.start_sample, seg_b.start_sample)
                end = min(seg_a.end_sample, seg_b.end_sample)

                if start < end:
                    # Overlapping region exists
                    segment_audio = audio[start:end]
                    duration_ms = (len(segment_audio) / self.config.vad_config.sample_rate) * 1000

                    # Use higher confidence
                    confidence = max(seg_a.confidence, seg_b.confidence)

                    intersected.append(SpeechSegment(
                        start_sample=start,
                        end_sample=end,
                        audio_data=segment_audio,
                        confidence=confidence,
                        duration_ms=duration_ms
                    ))

        return intersected

    def get_speech_segments(self, audio: np.ndarray) -> list[SpeechSegment]:
        """
        Get detailed speech segments with timing and confidence

        Args:
            audio: Raw audio

        Returns:
            List of speech segments
        """
        if self.primary_vad is None:
            return []

        segments = list(self.primary_vad.process_audio(audio))

        # Filter by minimum confidence
        segments = [seg for seg in segments if seg.confidence >= self.config.min_confidence]

        return segments

    def calculate_speech_ratio(self, audio: np.ndarray) -> float:
        """
        Calculate ratio of speech to total audio duration

        Args:
            audio: Raw audio

        Returns:
            Speech ratio (0.0 to 1.0)
        """
        if self.primary_vad is None or len(audio) == 0:
            return 0.0

        return self.primary_vad.get_speech_ratio(audio)


# Global VAD pipeline instance (lazy-initialized)
_vad_pipeline: Optional[VADPipeline] = None
# Construction may run on an executor thread (get_vad_pipeline_async) while
# another caller asks synchronously: one builder, never two Silero loads.
_vad_pipeline_lock = threading.Lock()


def get_vad_pipeline(config: Optional[VADPipelineConfig] = None) -> VADPipeline:
    """
    Get or create global VAD pipeline instance. BLOCKS on first call.

    The first call constructs Silero, which may download it through
    torch.hub; from a coroutine use :func:`get_vad_pipeline_async`.

    Args:
        config: Optional pipeline configuration (used on first call)

    Returns:
        VAD pipeline instance
    """
    global _vad_pipeline

    if _vad_pipeline is None:
        with _vad_pipeline_lock:
            if _vad_pipeline is None:
                if config is None:
                    config = VADPipelineConfig()
                _vad_pipeline = VADPipeline(config)

    return _vad_pipeline


async def get_vad_pipeline_async(
    config: Optional[VADPipelineConfig] = None,
) -> VADPipeline:
    """:func:`get_vad_pipeline` for coroutines: the first construction (a
    model load, possibly a download) runs off the event loop."""
    if _vad_pipeline is not None:
        return _vad_pipeline
    return await asyncio.get_running_loop().run_in_executor(
        None, get_vad_pipeline, config,
    )
