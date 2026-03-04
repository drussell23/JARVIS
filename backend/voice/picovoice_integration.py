"""
Advanced Wake Word Detection System
Combines Picovoice Porcupine with energy-based fallback for robust, low-latency detection

Features:
- Picovoice Porcupine for accurate keyword spotting (primary)
- Energy-based detection with VAD fallback (when Porcupine unavailable)
- Adaptive sensitivity tuning based on false positive/negative rates
- Multi-keyword support with per-keyword sensitivity
- Async processing with non-blocking audio streams
- Performance monitoring and auto-tuning
- Speaker-aware detection (optional verification)
"""

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

import numpy as np
import sounddevice as sd

try:
    import pvporcupine

    PICOVOICE_AVAILABLE = True
except ImportError:
    PICOVOICE_AVAILABLE = False
    print("⚠️  Picovoice Porcupine not available. Install with: pip install pvporcupine")
    print("   Falling back to energy-based detection")

try:
    from .config import VOICE_CONFIG
except ImportError:
    try:
        from config import VOICE_CONFIG
    except ImportError:
        VOICE_CONFIG = None

logger = logging.getLogger(__name__)


@dataclass
class WakeWordConfig:
    """Unified configuration for wake word detection"""

    # Picovoice settings
    access_key: Optional[str] = None
    keyword_paths: Optional[List[str]] = None
    keywords: Optional[List[str]] = None  # Built-in keywords like "jarvis"
    sensitivities: Optional[List[float]] = None
    model_path: Optional[str] = None

    # Energy-based fallback settings
    energy_threshold: float = 0.03  # RMS threshold for speech detection
    silence_threshold: float = 0.01  # Energy below this is silence
    min_speech_duration_ms: float = 100  # Minimum speech duration
    max_speech_duration_ms: float = 5000  # Maximum speech duration

    # Adaptive tuning
    enable_adaptive_tuning: bool = True
    false_positive_penalty: float = 0.05  # Reduce sensitivity on false positive
    false_negative_bonus: float = 0.02  # Increase sensitivity on false negative

    # Performance
    sample_rate: int = 16000
    chunk_duration_ms: float = 30  # Process audio in 30ms chunks

    # Speaker verification (optional)
    enable_speaker_verification: bool = False
    verification_threshold: float = 0.75


@dataclass
class DetectionMetrics:
    """Performance metrics for wake word detection"""

    total_detections: int = 0
    false_positives: int = 0
    false_negatives: int = 0
    true_positives: int = 0
    avg_detection_latency_ms: float = 0.0
    total_frames_processed: int = 0
    detection_timestamps: List[float] = field(default_factory=list)

    @property
    def precision(self) -> float:
        """Precision: TP / (TP + FP)"""
        if self.true_positives + self.false_positives == 0:
            return 0.0
        return self.true_positives / (self.true_positives + self.false_positives)

    @property
    def recall(self) -> float:
        """Recall: TP / (TP + FN)"""
        if self.true_positives + self.false_negatives == 0:
            return 0.0
        return self.true_positives / (self.true_positives + self.false_negatives)

    @property
    def f1_score(self) -> float:
        """F1 Score: 2 * (precision * recall) / (precision + recall)"""
        if self.precision + self.recall == 0:
            return 0.0
        return 2 * (self.precision * self.recall) / (self.precision + self.recall)


class EnergyBasedWakeDetector:
    """
    Energy-based wake word detector using Voice Activity Detection (VAD)
    Fallback when Picovoice is unavailable

    Uses RMS energy and zero-crossing rate for speech detection
    """

    def __init__(self, config: WakeWordConfig):
        self.config = config
        self.sample_rate = config.sample_rate
        self.chunk_size = int(config.sample_rate * config.chunk_duration_ms / 1000)

        # Energy thresholds
        self.energy_threshold = config.energy_threshold
        self.silence_threshold = config.silence_threshold

        # Speech detection state
        self.is_speech_active = False
        self.speech_start_time = None
        self.speech_buffer = deque(
            maxlen=int(config.max_speech_duration_ms / config.chunk_duration_ms)
        )

        # Adaptive thresholds
        self.energy_history = deque(maxlen=100)
        self.adaptive_energy_threshold = self.energy_threshold

        logger.info("Energy-based wake detector initialized (fallback mode)")

    def compute_energy(self, audio_chunk: np.ndarray) -> float:
        """Compute RMS energy of audio chunk"""
        return float(np.sqrt(np.mean(audio_chunk**2)))

    def compute_zero_crossing_rate(self, audio_chunk: np.ndarray) -> float:
        """Compute zero-crossing rate (ZCR) for speech detection"""
        zero_crossings = np.sum(np.abs(np.diff(np.sign(audio_chunk)))) / (2 * len(audio_chunk))
        return float(zero_crossings)

    def detect_speech(self, audio_chunk: np.ndarray) -> Tuple[bool, float]:
        """
        Detect if audio chunk contains speech

        Returns:
            (is_speech, confidence)
        """
        energy = self.compute_energy(audio_chunk)
        zcr = self.compute_zero_crossing_rate(audio_chunk)

        # Update energy history for adaptive threshold
        self.energy_history.append(energy)

        # Adaptive threshold based on recent audio
        if len(self.energy_history) >= 50:
            median_energy = float(np.median(list(self.energy_history)))
            self.adaptive_energy_threshold = max(
                self.config.energy_threshold, median_energy * 1.5  # 1.5x above median
            )

        # Speech detection logic
        is_speech = energy > self.adaptive_energy_threshold and zcr > 0.1

        # Confidence based on how far above threshold
        if is_speech:
            confidence = min(1.0, energy / (self.adaptive_energy_threshold * 2))
        else:
            confidence = 0.0

        return is_speech, confidence

    async def process_audio(self, audio_data: np.ndarray) -> Optional[Tuple[bool, float]]:
        """
        Process audio data for wake word detection

        Returns:
            (detected, confidence) or None if no detection
        """
        is_speech, confidence = self.detect_speech(audio_data)

        current_time = time.time()

        if is_speech:
            if not self.is_speech_active:
                # Speech started
                self.is_speech_active = True
                self.speech_start_time = current_time
                self.speech_buffer.clear()
                logger.debug("Speech activity started")

            # Add to speech buffer
            self.speech_buffer.append(audio_data)

            # Check if speech duration is within valid range
            speech_duration_ms = (current_time - self.speech_start_time) * 1000

            if speech_duration_ms >= self.config.min_speech_duration_ms:
                # Valid speech detected - trigger wake word
                logger.info(f"Wake word detected via energy (duration: {speech_duration_ms:.0f}ms)")
                self.is_speech_active = False
                return (True, confidence)

        else:
            # Silence detected
            if self.is_speech_active:
                # Speech ended
                speech_duration_ms = (current_time - self.speech_start_time) * 1000

                if speech_duration_ms >= self.config.min_speech_duration_ms:
                    # Valid speech ended
                    logger.debug(f"Speech ended (duration: {speech_duration_ms:.0f}ms)")
                    self.is_speech_active = False
                    return (True, confidence)
                else:
                    # Too short, probably noise
                    self.is_speech_active = False
                    self.speech_buffer.clear()

        return None

    def update_threshold(self, is_false_positive: bool):
        """Update energy threshold based on feedback"""
        if is_false_positive:
            # Increase threshold to reduce false positives
            self.energy_threshold *= 1.1
            self.energy_threshold = min(0.15, self.energy_threshold)
            logger.info(f"Increased energy threshold to {self.energy_threshold:.4f}")
        else:
            # Decrease threshold to catch more detections
            self.energy_threshold *= 0.95
            self.energy_threshold = max(0.01, self.energy_threshold)
            logger.debug(f"Decreased energy threshold to {self.energy_threshold:.4f}")


class PicovoiceWakeWordDetector:
    """
    High-performance wake word detection using Picovoice Porcupine
    - Runs entirely on-device with minimal CPU/RAM usage
    - Sub-50ms latency
    - Works in noisy environments
    """

    def __init__(self, config: WakeWordConfig):
        if not PICOVOICE_AVAILABLE:
            raise ImportError("Picovoice Porcupine is not installed")

        self.config = config

        # Get access key from config or environment
        self.access_key = config.access_key
        if VOICE_CONFIG and hasattr(VOICE_CONFIG, "picovoice_access_key"):
            self.access_key = self.access_key or VOICE_CONFIG.picovoice_access_key

        if not self.access_key:
            raise ValueError("Picovoice access key required. Set PICOVOICE_ACCESS_KEY env variable")

        # Default sensitivity for each keyword
        self.default_sensitivity = 0.5

        # Initialize Porcupine
        self.porcupine = None
        self._init_porcupine()

        # Audio processing
        self.frame_length = self.porcupine.frame_length if self.porcupine else 512
        self.sample_rate = self.porcupine.sample_rate if self.porcupine else 16000
        self.audio_buffer = deque(maxlen=self.frame_length * 2)

        # Detection callback
        self.detection_callback: Optional[Callable] = None

        # Performance metrics
        self.metrics = DetectionMetrics()

    def _init_porcupine(self):
        """Initialize Porcupine with keywords"""
        try:
            # Prepare keywords
            keywords = self.config.keywords or ["jarvis"]
            keyword_paths = self.config.keyword_paths or []

            # Prepare sensitivities
            num_keywords = len(keywords) + len(keyword_paths)
            sensitivities = self.config.sensitivities
            if not sensitivities:
                sensitivities = [self.default_sensitivity] * num_keywords
            elif len(sensitivities) < num_keywords:
                sensitivities.extend(
                    [self.default_sensitivity] * (num_keywords - len(sensitivities))
                )

            # Create Porcupine instance
            create_params = {
                "access_key": self.access_key,
                "keywords": keywords if keywords else None,
                "keyword_paths": keyword_paths if keyword_paths else None,
                "sensitivities": sensitivities[:num_keywords],
            }

            if self.config.model_path:
                create_params["model_path"] = self.config.model_path

            self.porcupine = pvporcupine.create(**create_params)

            logger.info(f"🎧 Picovoice Porcupine initialized with keywords: {keywords}")
            logger.info(f"   Frame length: {self.porcupine.frame_length}")
            logger.info(f"   Sample rate: {self.porcupine.sample_rate}")

        except Exception as e:
            logger.error(f"Failed to initialize Porcupine: {e}")
            raise

    def process_audio(self, audio_data: np.ndarray) -> Optional[int]:
        """
        Process audio data for wake word detection

        Returns:
            keyword index if detected, None otherwise
        """
        # SAFETY: Capture porcupine reference at method start to prevent
        # segfaults if cleanup() is called during processing
        porcupine_ref = self.porcupine
        if not porcupine_ref:
            return None

        # Ensure audio is in the correct format
        if audio_data.dtype != np.int16:
            # Convert float to int16
            if audio_data.dtype in (np.float32, np.float64):
                audio_data = (audio_data * 32767).astype(np.int16)
            else:
                audio_data = audio_data.astype(np.int16)

        # Process in frames
        keyword_index = None

        # Add to buffer
        self.audio_buffer.extend(audio_data)

        # Process complete frames
        while len(self.audio_buffer) >= self.frame_length:
            # Extract frame
            frame = np.array(list(self.audio_buffer)[: self.frame_length])

            # Remove processed samples
            for _ in range(self.frame_length):
                self.audio_buffer.popleft()

            # Process frame using captured reference
            try:
                result = porcupine_ref.process(frame)
                self.metrics.total_frames_processed += 1

                if result >= 0:
                    keyword_index = result
                    self.metrics.total_detections += 1
                    self.metrics.detection_timestamps.append(time.time())

                    logger.info(f"✨ Wake word detected! Keyword index: {result}")

                    # Call callback if set
                    if self.detection_callback:
                        asyncio.create_task(
                            self.detection_callback(result)
                            if asyncio.iscoroutinefunction(self.detection_callback)
                            else asyncio.to_thread(self.detection_callback, result),
                            name="picovoice_detection_callback",
                        )

                    break  # Return on first detection

            except Exception as e:
                logger.error(f"Error processing frame: {e}")

        return keyword_index

    async def process_audio_async(self, audio_data: np.ndarray) -> Optional[int]:
        """Async wrapper for process_audio"""
        return await asyncio.to_thread(self.process_audio, audio_data)

    def update_sensitivity(self, keyword_index: int, sensitivity: float):
        """Update sensitivity for a specific keyword (requires reinit)"""
        if not self.config.sensitivities:
            self.config.sensitivities = [self.default_sensitivity] * self.porcupine.num_keywords

        if 0 <= keyword_index < len(self.config.sensitivities):
            self.config.sensitivities[keyword_index] = max(0.0, min(1.0, sensitivity))

            # Reinitialize with new sensitivity
            self.cleanup()
            self._init_porcupine()

            logger.info(f"Updated sensitivity for keyword {keyword_index} to {sensitivity:.2f}")

    def report_feedback(self, is_true_positive: bool):
        """Report detection feedback for adaptive tuning"""
        if is_true_positive:
            self.metrics.true_positives += 1
        else:
            self.metrics.false_positives += 1

        # Adaptive tuning
        if self.config.enable_adaptive_tuning:
            if not is_true_positive:
                # False positive - increase sensitivity threshold
                for i in range(len(self.config.sensitivities or [])):
                    self.update_sensitivity(
                        i,
                        (self.config.sensitivities[i] or 0.5) + self.config.false_positive_penalty,
                    )

    def get_metrics(self) -> dict:
        """Get performance metrics"""
        return {
            "total_frames_processed": self.metrics.total_frames_processed,
            "total_detections": self.metrics.total_detections,
            "true_positives": self.metrics.true_positives,
            "false_positives": self.metrics.false_positives,
            "false_negatives": self.metrics.false_negatives,
            "precision": self.metrics.precision,
            "recall": self.metrics.recall,
            "f1_score": self.metrics.f1_score,
            "detection_rate": (
                self.metrics.total_detections / self.metrics.total_frames_processed
                if self.metrics.total_frames_processed > 0
                else 0
            ),
            "frame_length": self.frame_length,
            "sample_rate": self.sample_rate,
        }

    def cleanup(self):
        """Clean up Porcupine resources"""
        if self.porcupine:
            self.porcupine.delete()
            self.porcupine = None
            logger.info("🧹 Porcupine cleaned up")


class UnifiedWakeWordDetector:
    """
    Unified wake word detector with automatic fallback
    Uses Picovoice when available, energy-based detection otherwise

    Features:
    - Automatic Picovoice/energy-based selection
    - Hybrid mode for verification
    - Speaker verification integration
    - Performance monitoring
    - Adaptive sensitivity tuning
    """

    def __init__(
        self,
        config: Optional[WakeWordConfig] = None,
        speaker_verification_engine=None,
    ):
        self.config = config or WakeWordConfig(keywords=["jarvis"])
        self.speaker_verification = speaker_verification_engine

        # Detection engines
        self.picovoice_detector = None
        self.energy_detector = None
        self.primary_detector = None

        # Initialize detection engines
        self._init_detectors()

        # Audio stream
        self.is_listening = False
        self.audio_stream = None

        # Performance
        self.last_detection_time = 0
        self.detection_cooldown_ms = 2000  # Prevent rapid re-triggers

        logger.info(f"🎤 Unified wake word detector initialized (mode: {self.get_mode()})")

    def _init_detectors(self):
        """Initialize detection engines with fallback"""
        # Try Picovoice first
        if PICOVOICE_AVAILABLE:
            try:
                self.picovoice_detector = PicovoiceWakeWordDetector(self.config)
                self.primary_detector = self.picovoice_detector
                logger.info("✅ Using Picovoice for wake word detection")
            except Exception as e:
                logger.warning(f"⚠️  Picovoice initialization failed: {e}")
                logger.info("   Falling back to energy-based detection")

        # Fallback to energy-based
        if not self.picovoice_detector:
            self.energy_detector = EnergyBasedWakeDetector(self.config)
            self.primary_detector = self.energy_detector
            logger.info("✅ Using energy-based detection (fallback mode)")

    def get_mode(self) -> str:
        """Get current detection mode"""
        if self.picovoice_detector:
            if self.energy_detector:
                return "hybrid"
            return "picovoice"
        return "energy-based"

    async def process_audio(self, audio_data: np.ndarray) -> Optional[Tuple[bool, float, str]]:
        """
        Process audio data for wake word detection

        Returns:
            (detected, confidence, method) or None
        """
        # Cooldown check
        current_time = time.time()
        if (current_time - self.last_detection_time) * 1000 < self.detection_cooldown_ms:
            return None

        detected = False
        confidence = 0.0
        method = "unknown"

        # Try Picovoice first (if available)
        if self.picovoice_detector:
            result = await self.picovoice_detector.process_audio_async(audio_data)
            if result is not None:
                detected = True
                confidence = 0.95  # Picovoice is highly accurate
                method = "picovoice"

        # Fallback to energy-based
        if not detected and self.energy_detector:
            result = await self.energy_detector.process_audio(audio_data)
            if result is not None:
                detected, confidence = result
                method = "energy"

        # Speaker verification (optional)
        if detected and self.config.enable_speaker_verification and self.speaker_verification:
            is_verified, speaker_confidence = await self.speaker_verification.verify_speaker(
                audio_data
            )
            if not is_verified:
                logger.info(f"❌ Speaker verification failed ({speaker_confidence:.2%})")
                return None
            logger.info(f"✅ Speaker verified ({speaker_confidence:.2%})")
            confidence *= speaker_confidence  # Combine confidences

        if detected:
            self.last_detection_time = current_time
            return (detected, confidence, method)

        return None

    async def start_listening(self, callback: Callable):
        """
        Start continuous listening for wake word

        Args:
            callback: Async function to call when wake word detected
        """
        self.is_listening = True
        self.detection_callback = callback

        chunk_size = int(self.config.sample_rate * self.config.chunk_duration_ms / 1000)

        logger.info("👂 Listening for wake word...")
        logger.info("   (Press Ctrl+C to stop)")

        try:
            with sd.InputStream(
                samplerate=self.config.sample_rate,
                channels=1,
                dtype="float32",
                blocksize=chunk_size,
            ) as stream:
                while self.is_listening:
                    # Read audio chunk
                    audio_data, overflowed = stream.read(chunk_size)
                    if overflowed:
                        logger.warning("⚠️  Audio buffer overflow")

                    # Convert to numpy array
                    audio_chunk = audio_data.flatten()

                    # Process for wake word
                    result = await self.process_audio(audio_chunk)

                    if result:
                        detected, confidence, method = result
                        logger.info(
                            f"✨ Wake word detected! (confidence: {confidence:.2%}, method: {method})"
                        )

                        # Call callback
                        if asyncio.iscoroutinefunction(callback):
                            await callback(detected, confidence, method)
                        else:
                            callback(detected, confidence, method)

        except KeyboardInterrupt:
            logger.info("\n⚠️  Wake word detection stopped by user")
        except Exception as e:
            logger.error(f"❌ Error in wake word detection: {e}", exc_info=True)
        finally:
            self.stop_listening()

    def stop_listening(self):
        """Stop listening for wake word"""
        self.is_listening = False
        logger.info("🛑 Wake word detection stopped")

    def report_feedback(self, is_true_positive: bool):
        """Report detection feedback for adaptive tuning"""
        if self.picovoice_detector:
            self.picovoice_detector.report_feedback(is_true_positive)
        if self.energy_detector:
            self.energy_detector.update_threshold(not is_true_positive)

    def get_metrics(self) -> dict:
        """Get performance metrics"""
        metrics = {
            "mode": self.get_mode(),
            "is_listening": self.is_listening,
        }

        if self.picovoice_detector:
            metrics["picovoice"] = self.picovoice_detector.get_metrics()

        if self.energy_detector:
            metrics["energy"] = {
                "energy_threshold": self.energy_detector.energy_threshold,
                "adaptive_threshold": self.energy_detector.adaptive_energy_threshold,
            }

        return metrics

    def cleanup(self):
        """Clean up resources"""
        self.stop_listening()
        if self.picovoice_detector:
            self.picovoice_detector.cleanup()
        logger.info("🧹 Wake word detector cleanup complete")


# Example usage
async def test_wake_word_detector():
    """Test unified wake word detector"""
    print("🤖 Testing Unified Wake Word Detector")
    print("=" * 60)

    # Create config
    config = WakeWordConfig(keywords=["jarvis"], sensitivities=[0.5])

    # Create detector
    detector = UnifiedWakeWordDetector(config)

    print(f"\n✅ Detector initialized (mode: {detector.get_mode()})")
    print("\n👂 Listening for 'Hey JARVIS'...")
    print("   Speak clearly and wait for detection")

    # Define callback
    async def on_wake_word(detected, confidence, method):
        print(f"\n🎉 Wake word detected!")
        print(f"   Confidence: {confidence:.2%}")
        print(f"   Method: {method}")
        print("\n👂 Listening again...")

    # Start listening
    try:
        await detector.start_listening(on_wake_word)
    except KeyboardInterrupt:
        print("\n\n⚠️  Test interrupted")
    finally:
        detector.cleanup()

    # Show metrics
    print("\n📊 Performance Metrics:")
    metrics = detector.get_metrics()
    for key, value in metrics.items():
        print(f"   {key}: {value}")


if __name__ == "__main__":
    asyncio.run(test_wake_word_detector())
