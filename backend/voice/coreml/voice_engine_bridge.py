"""
CoreML Voice Engine - Python Bridge
====================================

Python wrapper for the C++ CoreML voice engine using ctypes.
Provides seamless integration with jarvis_voice.py.

Integrated with async_pipeline.py for enterprise-grade async processing:
- AdaptiveCircuitBreaker for fault tolerance
- AsyncEventBus for event-driven architecture
- VoiceTask for async task management
- AsyncVoiceQueue for priority-based processing
"""

import ctypes
import os
import logging
import numpy as np
import asyncio
import time
from typing import Optional, Tuple, Dict, Any, Callable, List
from pathlib import Path
from dataclasses import dataclass, field
from collections import defaultdict
from functools import wraps

# Import async pipeline components
import sys
backend_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from core.async_pipeline import AdaptiveCircuitBreaker, AsyncEventBus

logger = logging.getLogger(__name__)


class AdaptiveConfig(ctypes.Structure):
    """Python wrapper for AdaptiveConfig struct"""
    _fields_ = [
        ("vad_threshold", ctypes.c_float),
        ("vad_threshold_min", ctypes.c_float),
        ("vad_threshold_max", ctypes.c_float),
        ("speaker_threshold", ctypes.c_float),
        ("speaker_threshold_min", ctypes.c_float),
        ("speaker_threshold_max", ctypes.c_float),
        ("sample_rate", ctypes.c_int),
        ("frame_size", ctypes.c_int),
        ("hop_length", ctypes.c_int),
        ("enable_adaptive", ctypes.c_bool),
        ("learning_rate", ctypes.c_float),
        ("adaptation_window", ctypes.c_int),
    ]


# ============================================================================
# ASYNC VOICE TASK - For async queue processing
# ============================================================================

@dataclass
class VoiceTask:
    """Represents an async voice detection task"""
    task_id: str
    audio: Optional[np.ndarray] = None
    vad_confidence: float = 0.0
    speaker_confidence: float = 0.0
    is_user_voice: bool = False
    timestamp: float = field(default_factory=time.time)
    status: str = "pending"  # pending, processing, completed, failed
    error: Optional[str] = None
    priority: int = 0  # 0=normal, 1=high, 2=critical
    retries: int = 0
    max_retries: int = 3


# ============================================================================
# ASYNC VOICE QUEUE - Priority-based async queue
# ============================================================================

class AsyncVoiceQueue:
    """Priority-based async queue for voice detection tasks"""

    def __init__(self, maxsize: int = 100):
        self.queue: asyncio.PriorityQueue = asyncio.PriorityQueue(maxsize=maxsize)
        self.max_concurrent = 3
        self.in_flight: Dict[str, VoiceTask] = {}
        self._lock = asyncio.Lock()

    async def enqueue(self, task: VoiceTask) -> bool:
        """Add task to queue"""
        if self.is_full():
            logger.warning(f"[ASYNC-QUEUE] Queue full - rejecting task {task.task_id}")
            return False

        # Priority queue: lower number = higher priority
        # Negate priority so higher numbers come first
        priority = -task.priority
        await self.queue.put((priority, task.timestamp, task))

        logger.debug(f"[ASYNC-QUEUE] Enqueued task {task.task_id} (priority={task.priority}, queue_size={self.queue.qsize()})")
        return True

    async def dequeue(self) -> Optional[VoiceTask]:
        """Get next highest-priority task"""
        try:
            _, _, task = await self.queue.get()
            async with self._lock:
                self.in_flight[task.task_id] = task
            logger.debug(f"[ASYNC-QUEUE] Dequeued task {task.task_id} (in_flight={len(self.in_flight)})")
            return task
        except asyncio.QueueEmpty:
            return None

    async def complete_task(self, task_id: str):
        """Mark task as completed"""
        async with self._lock:
            if task_id in self.in_flight:
                del self.in_flight[task_id]
                logger.debug(f"[ASYNC-QUEUE] Completed task {task_id} (in_flight={len(self.in_flight)})")

    def is_full(self) -> bool:
        """Check if queue is at capacity"""
        return self.queue.qsize() >= self.queue.maxsize

    def size(self) -> int:
        """Get current queue size"""
        return self.queue.qsize()


class CoreMLVoiceEngineBridge:
    """
    Python bridge to C++ CoreML Voice Engine.

    Provides ultra-fast voice activity detection and speaker recognition
    using Apple's CoreML framework on the Neural Engine.
    """

    def __init__(
        self,
        vad_model_path: str,
        speaker_model_path: str,
        config: Optional[Dict[str, Any]] = None
    ):
        """
        Initialize CoreML Voice Engine.

        Args:
            vad_model_path: Path to VAD CoreML model (.mlmodelc)
            speaker_model_path: Path to Speaker Recognition CoreML model (.mlmodelc)
            config: Optional configuration dictionary
        """
        # Load shared library
        lib_path = self._find_library()
        self.lib = ctypes.CDLL(lib_path)

        # Define C++ function signatures
        self._setup_function_signatures()

        # Create adaptive config
        self.config = self._create_config(config or {})

        # Initialize C++ engine
        vad_path_bytes = vad_model_path.encode('utf-8')
        speaker_path_bytes = speaker_model_path.encode('utf-8') if speaker_model_path else b""

        self.engine = self.lib.CoreMLVoiceEngine_create(
            vad_path_bytes,
            speaker_path_bytes,
            ctypes.byref(self.config)
        )

        if not self.engine:
            raise RuntimeError("Failed to initialize CoreML Voice Engine")

        # Initialize async components
        self.circuit_breaker = AdaptiveCircuitBreaker(
            initial_threshold=5,
            initial_timeout=30,
            adaptive=True
        )
        self.event_bus = AsyncEventBus()
        self.voice_queue = AsyncVoiceQueue(maxsize=100)
        self._setup_event_handlers()

        logger.info(f"[CoreML-Bridge] Initialized with VAD: {vad_model_path}")
        logger.info(f"[CoreML-Bridge] Speaker model: {speaker_model_path}")
        logger.info(f"[CoreML-Bridge] VAD threshold: {self.config.vad_threshold:.3f}")
        logger.info(f"[CoreML-Bridge] Speaker threshold: {self.config.speaker_threshold:.3f}")
        logger.info(f"[CoreML-ASYNC] Initialized circuit breaker and event bus")

    def _find_library(self) -> str:
        """Find the compiled shared library"""
        # Look in common locations
        search_paths = [
            Path(__file__).parent / "build" / "libvoice_engine.dylib",
            Path(__file__).parent / "libvoice_engine.dylib",
            Path(__file__).parent.parent.parent / "lib" / "libvoice_engine.dylib",
        ]

        for path in search_paths:
            if path.exists():
                logger.info(f"[CoreML-Bridge] Found library at {path}")
                return str(path)

        raise FileNotFoundError(
            "CoreML Voice Engine library not found. "
            "Please compile voice_engine.mm first using CMake."
        )

    def _setup_function_signatures(self):
        """Define C function signatures for ctypes"""

        # CoreMLVoiceEngine_create(vad_path, speaker_path, config) -> void*
        self.lib.CoreMLVoiceEngine_create.argtypes = [
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.POINTER(AdaptiveConfig)
        ]
        self.lib.CoreMLVoiceEngine_create.restype = ctypes.c_void_p

        # CoreMLVoiceEngine_destroy(engine) -> void
        self.lib.CoreMLVoiceEngine_destroy.argtypes = [ctypes.c_void_p]
        self.lib.CoreMLVoiceEngine_destroy.restype = None

        # CoreMLVoiceEngine_detect_voice_activity(engine, audio, size, confidence) -> bool
        self.lib.CoreMLVoiceEngine_detect_voice_activity.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_float),
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_float)
        ]
        self.lib.CoreMLVoiceEngine_detect_voice_activity.restype = ctypes.c_bool

        # CoreMLVoiceEngine_recognize_speaker(engine, audio, size, confidence) -> bool
        self.lib.CoreMLVoiceEngine_recognize_speaker.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_float),
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_float)
        ]
        self.lib.CoreMLVoiceEngine_recognize_speaker.restype = ctypes.c_bool

        # CoreMLVoiceEngine_detect_user_voice(engine, audio, size, vad_conf, speaker_conf) -> bool
        self.lib.CoreMLVoiceEngine_detect_user_voice.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_float),
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_float),
            ctypes.POINTER(ctypes.c_float)
        ]
        self.lib.CoreMLVoiceEngine_detect_user_voice.restype = ctypes.c_bool

        # CoreMLVoiceEngine_train_speaker_model(engine, audio, size, label) -> void
        self.lib.CoreMLVoiceEngine_train_speaker_model.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_float),
            ctypes.c_size_t,
            ctypes.c_bool
        ]
        self.lib.CoreMLVoiceEngine_train_speaker_model.restype = None

        # CoreMLVoiceEngine_update_adaptive_thresholds(engine, success, vad_conf, speaker_conf) -> void
        self.lib.CoreMLVoiceEngine_update_adaptive_thresholds.argtypes = [
            ctypes.c_void_p,
            ctypes.c_bool,
            ctypes.c_float,
            ctypes.c_float
        ]
        self.lib.CoreMLVoiceEngine_update_adaptive_thresholds.restype = None

        # CoreMLVoiceEngine_get_avg_latency_ms(engine) -> double
        self.lib.CoreMLVoiceEngine_get_avg_latency_ms.argtypes = [ctypes.c_void_p]
        self.lib.CoreMLVoiceEngine_get_avg_latency_ms.restype = ctypes.c_double

        # CoreMLVoiceEngine_get_success_rate(engine) -> float
        self.lib.CoreMLVoiceEngine_get_success_rate.argtypes = [ctypes.c_void_p]
        self.lib.CoreMLVoiceEngine_get_success_rate.restype = ctypes.c_float

    def _create_config(self, config_dict: Dict[str, Any]) -> AdaptiveConfig:
        """Create AdaptiveConfig from dictionary"""
        config = AdaptiveConfig()

        # Set defaults or user values
        config.vad_threshold = config_dict.get('vad_threshold', 0.5)
        config.vad_threshold_min = config_dict.get('vad_threshold_min', 0.2)
        config.vad_threshold_max = config_dict.get('vad_threshold_max', 0.9)

        config.speaker_threshold = config_dict.get('speaker_threshold', 0.7)
        config.speaker_threshold_min = config_dict.get('speaker_threshold_min', 0.4)
        config.speaker_threshold_max = config_dict.get('speaker_threshold_max', 0.95)

        config.sample_rate = config_dict.get('sample_rate', 16000)
        config.frame_size = config_dict.get('frame_size', 512)
        config.hop_length = config_dict.get('hop_length', 160)

        config.enable_adaptive = config_dict.get('enable_adaptive', True)
        config.learning_rate = config_dict.get('learning_rate', 0.01)
        config.adaptation_window = config_dict.get('adaptation_window', 100)

        return config

    def detect_voice_activity(self, audio: np.ndarray) -> Tuple[bool, float]:
        """
        Detect voice activity in audio.

        Args:
            audio: Audio samples as numpy array (float32, mono, 16kHz)

        Returns:
            (has_voice, confidence): Tuple of detection result and confidence
        """
        # Convert to ctypes array
        audio_data = audio.astype(np.float32)
        audio_ptr = audio_data.ctypes.data_as(ctypes.POINTER(ctypes.c_float))

        # Call C++ function
        confidence = ctypes.c_float()
        result = self.lib.CoreMLVoiceEngine_detect_voice_activity(
            self.engine,
            audio_ptr,
            len(audio_data),
            ctypes.byref(confidence)
        )

        return bool(result), confidence.value

    def recognize_speaker(self, audio: np.ndarray) -> Tuple[bool, float]:
        """
        Recognize if speaker is the trained user.

        Args:
            audio: Audio samples as numpy array (float32, mono, 16kHz)

        Returns:
            (is_user, confidence): Tuple of recognition result and confidence
        """
        audio_data = audio.astype(np.float32)
        audio_ptr = audio_data.ctypes.data_as(ctypes.POINTER(ctypes.c_float))

        confidence = ctypes.c_float()
        result = self.lib.CoreMLVoiceEngine_recognize_speaker(
            self.engine,
            audio_ptr,
            len(audio_data),
            ctypes.byref(confidence)
        )

        return bool(result), confidence.value

    def detect_user_voice(self, audio: np.ndarray) -> Tuple[bool, float, float]:
        """
        Combined VAD + Speaker Recognition.

        This is the main method for fast voice detection.

        Args:
            audio: Audio samples as numpy array (float32, mono, 16kHz)

        Returns:
            (is_user_voice, vad_confidence, speaker_confidence)
        """
        audio_data = audio.astype(np.float32)
        audio_ptr = audio_data.ctypes.data_as(ctypes.POINTER(ctypes.c_float))

        vad_conf = ctypes.c_float()
        speaker_conf = ctypes.c_float()

        result = self.lib.CoreMLVoiceEngine_detect_user_voice(
            self.engine,
            audio_ptr,
            len(audio_data),
            ctypes.byref(vad_conf),
            ctypes.byref(speaker_conf)
        )

        return bool(result), vad_conf.value, speaker_conf.value

    def train_speaker_model(self, audio: np.ndarray, is_user: bool):
        """
        Train speaker model with new sample.

        Args:
            audio: Audio sample of voice
            is_user: True if this is the user's voice, False otherwise
        """
        audio_data = audio.astype(np.float32)
        audio_ptr = audio_data.ctypes.data_as(ctypes.POINTER(ctypes.c_float))

        self.lib.CoreMLVoiceEngine_train_speaker_model(
            self.engine,
            audio_ptr,
            len(audio_data),
            is_user
        )

        logger.debug(f"[CoreML-Bridge] Trained speaker model - Label: {'USER' if is_user else 'OTHER'}")

    def update_adaptive_thresholds(
        self,
        success: bool,
        vad_confidence: float,
        speaker_confidence: float
    ):
        """
        Update adaptive thresholds based on performance.

        Args:
            success: Whether the detection was successful
            vad_confidence: VAD confidence from last detection
            speaker_confidence: Speaker confidence from last detection
        """
        self.lib.CoreMLVoiceEngine_update_adaptive_thresholds(
            self.engine,
            success,
            vad_confidence,
            speaker_confidence
        )

    def get_metrics(self) -> Dict[str, Any]:
        """Get performance metrics"""
        avg_latency = self.lib.CoreMLVoiceEngine_get_avg_latency_ms(self.engine)
        success_rate = self.lib.CoreMLVoiceEngine_get_success_rate(self.engine)

        return {
            'avg_latency_ms': avg_latency,
            'success_rate': success_rate,
            'vad_threshold': self.config.vad_threshold,
            'speaker_threshold': self.config.speaker_threshold,
            'circuit_breaker_state': self.circuit_breaker.state,
            'circuit_breaker_success_rate': self.circuit_breaker.success_rate,
            'queue_size': self.voice_queue.size()
        }

    # ========================================================================
    # ASYNC METHODS - Integration with async_pipeline.py
    # ========================================================================

    def _setup_event_handlers(self):
        """Setup async event handlers for voice events"""
        self.event_bus.subscribe("voice_detected", self._on_voice_detected)
        self.event_bus.subscribe("voice_failed", self._on_voice_failed)
        self.event_bus.subscribe("circuit_breaker_open", self._on_circuit_breaker_open)
        logger.debug("[CoreML-ASYNC-EVENT] Setup event handlers for voice processing")

    async def _on_voice_detected(self, data: Dict[str, Any]):
        """Handle voice detected event"""
        task_id = data.get('task_id', 'unknown')
        vad_conf = data.get('vad_confidence', 0.0)
        speaker_conf = data.get('speaker_confidence', 0.0)
        logger.info(f"[CoreML-ASYNC-EVENT] Voice detected: task={task_id}, VAD={vad_conf:.3f}, Speaker={speaker_conf:.3f}")

    async def _on_voice_failed(self, data: Dict[str, Any]):
        """Handle voice detection failure"""
        task_id = data.get('task_id', 'unknown')
        error = data.get('error', 'Unknown error')
        logger.warning(f"[CoreML-ASYNC-EVENT] Voice detection failed: task={task_id}, error={error}")

    async def _on_circuit_breaker_open(self, data: Dict[str, Any]):
        """Handle circuit breaker open event"""
        threshold = data.get('threshold', 0)
        logger.warning(f"[CoreML-ASYNC-EVENT] Circuit breaker OPEN (threshold={threshold})")

    async def detect_voice_activity_async(self, audio: np.ndarray, priority: int = 0) -> Tuple[bool, float]:
        """
        Async voice activity detection with circuit breaker protection.

        Args:
            audio: Audio samples as numpy array
            priority: Task priority (0=normal, 1=high, 2=critical)

        Returns:
            (has_voice, confidence)
        """
        task_id = f"vad_{int(time.time() * 1000)}"

        # Create task
        task = VoiceTask(
            task_id=task_id,
            audio=audio,
            priority=priority
        )

        # Check queue capacity
        if self.voice_queue.is_full():
            await self.event_bus.publish("queue_full", {"task_id": task_id})
            raise RuntimeError("Voice queue is full")

        # Enqueue
        await self.voice_queue.enqueue(task)

        # Execute with circuit breaker protection
        async def vad_wrapper():
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None,
                lambda: self.detect_voice_activity(audio)
            )

        try:
            has_voice, confidence = await self.circuit_breaker.call(vad_wrapper)

            # Update task
            task.vad_confidence = confidence
            task.is_user_voice = has_voice
            task.status = "completed"

            # Publish success event
            await self.event_bus.publish("voice_detected", {
                "task_id": task_id,
                "vad_confidence": confidence,
                "speaker_confidence": 0.0,
                "timestamp": time.time()
            })

            # Complete task
            await self.voice_queue.complete_task(task_id)

            return has_voice, confidence

        except Exception as e:
            task.status = "failed"
            task.error = str(e)

            # Publish failure event
            await self.event_bus.publish("voice_failed", {
                "task_id": task_id,
                "error": str(e),
                "timestamp": time.time()
            })

            await self.voice_queue.complete_task(task_id)
            raise

    async def detect_user_voice_async(self, audio: np.ndarray, priority: int = 0) -> Tuple[bool, float, float]:
        """
        Async combined VAD + Speaker Recognition with full async pipeline integration.

        Args:
            audio: Audio samples as numpy array
            priority: Task priority (0=normal, 1=high, 2=critical)

        Returns:
            (is_user_voice, vad_confidence, speaker_confidence)
        """
        task_id = f"voice_{int(time.time() * 1000)}"

        # Create task
        task = VoiceTask(
            task_id=task_id,
            audio=audio,
            priority=priority
        )

        # Check queue capacity
        if self.voice_queue.is_full():
            await self.event_bus.publish("queue_full", {"task_id": task_id})
            logger.warning(f"[CoreML-ASYNC] Queue full - rejecting task {task_id}")
            return False, 0.0, 0.0

        # Enqueue
        await self.voice_queue.enqueue(task)

        # Execute with circuit breaker protection
        async def detect_wrapper():
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None,
                lambda: self.detect_user_voice(audio)
            )

        try:
            is_user, vad_conf, speaker_conf = await self.circuit_breaker.call(detect_wrapper)

            # Update task
            task.vad_confidence = vad_conf
            task.speaker_confidence = speaker_conf
            task.is_user_voice = is_user
            task.status = "completed"

            # Publish success event
            await self.event_bus.publish("voice_detected", {
                "task_id": task_id,
                "is_user_voice": is_user,
                "vad_confidence": vad_conf,
                "speaker_confidence": speaker_conf,
                "timestamp": time.time()
            })

            # Update adaptive thresholds
            self.update_adaptive_thresholds(is_user, vad_conf, speaker_conf)

            # Complete task
            await self.voice_queue.complete_task(task_id)

            logger.debug(f"[CoreML-ASYNC] Detected user voice: {is_user} (VAD={vad_conf:.3f}, Speaker={speaker_conf:.3f})")

            return is_user, vad_conf, speaker_conf

        except Exception as e:
            task.status = "failed"
            task.error = str(e)

            # Publish failure event
            await self.event_bus.publish("voice_failed", {
                "task_id": task_id,
                "error": str(e),
                "timestamp": time.time()
            })

            await self.voice_queue.complete_task(task_id)

            logger.error(f"[CoreML-ASYNC] Voice detection failed: {e}")
            return False, 0.0, 0.0

    async def process_voice_queue_worker(self):
        """
        Background worker for processing voice queue.
        Runs continuously and processes up to max_concurrent tasks.
        """
        logger.info("[CoreML-ASYNC-WORKER] Started voice queue worker")

        session_timeout = float(os.getenv("TIMEOUT_VOICE_SESSION", "300.0"))  # 5 min default
        session_start = time.monotonic()

        while time.monotonic() - session_start < session_timeout:
            try:
                # Check if we have capacity
                if len(self.voice_queue.in_flight) >= self.voice_queue.max_concurrent:
                    await asyncio.sleep(0.1)
                    continue

                # Get next task with timeout protection
                try:
                    task = await asyncio.wait_for(
                        self.voice_queue.dequeue(),
                        timeout=float(os.getenv("TIMEOUT_VOICE_READ", "10.0"))
                    )
                except asyncio.TimeoutError:
                    logger.debug("[CoreML-ASYNC-WORKER] Queue idle, continuing...")
                    continue

                if not task:
                    await asyncio.sleep(0.1)
                    continue

                # Process task
                logger.debug(f"[CoreML-ASYNC-WORKER] Processing task {task.task_id}")

                # Create async task for this voice detection
                asyncio.create_task(self._process_voice_task(task))

            except asyncio.CancelledError:
                logger.info("[CoreML-ASYNC-WORKER] Worker cancelled - shutting down")
                break
            except Exception as e:
                logger.error(f"[CoreML-ASYNC-WORKER] Worker error: {e}")
                await asyncio.sleep(1)  # Backoff on errors

        logger.info("[CoreML-ASYNC-WORKER] Session timeout reached - shutting down")

    async def _process_voice_task(self, task: VoiceTask):
        """Process a single voice task"""
        try:
            task.status = "processing"

            # Run detection
            is_user, vad_conf, speaker_conf = await self.detect_user_voice_async(
                task.audio,
                priority=task.priority
            )

            task.is_user_voice = is_user
            task.vad_confidence = vad_conf
            task.speaker_confidence = speaker_conf
            task.status = "completed"

        except Exception as e:
            task.status = "failed"
            task.error = str(e)
            logger.error(f"[CoreML-ASYNC] Task {task.task_id} failed: {e}")

    def __del__(self):
        """Cleanup C++ resources"""
        if hasattr(self, 'engine') and self.engine:
            self.lib.CoreMLVoiceEngine_destroy(self.engine)
            logger.debug("[CoreML-Bridge] Destroyed C++ engine")


# ============================================================================
# CONVENIENCE FUNCTIONS
# ============================================================================

def create_coreml_engine(
    vad_model_path: str,
    speaker_model_path: str,
    config: Optional[Dict[str, Any]] = None
) -> CoreMLVoiceEngineBridge:
    """
    Create a CoreML Voice Engine instance.

    Args:
        vad_model_path: Path to VAD model (.mlmodelc)
        speaker_model_path: Path to Speaker Recognition model (.mlmodelc)
        config: Optional configuration

    Returns:
        CoreMLVoiceEngineBridge instance
    """
    return CoreMLVoiceEngineBridge(vad_model_path, speaker_model_path, config)


def is_coreml_available() -> bool:
    """
    Check if CoreML library is available.

    Returns:
        True if library is found and loadable
    """
    try:
        bridge = CoreMLVoiceEngineBridge.__new__(CoreMLVoiceEngineBridge)
        bridge._find_library()
        return True
    except FileNotFoundError:
        return False


# ============================================================================
# EXAMPLE USAGE
# ============================================================================

if __name__ == "__main__":
    import time

    # Example: Initialize CoreML engine
    try:
        engine = create_coreml_engine(
            vad_model_path="/path/to/vad_model.mlmodelc",
            speaker_model_path="/path/to/speaker_model.mlmodelc",
            config={
                'vad_threshold': 0.5,
                'speaker_threshold': 0.7,
                'sample_rate': 16000,
                'enable_adaptive': True
            }
        )

        # Generate test audio (1 second of random noise)
        test_audio = np.random.randn(16000).astype(np.float32)

        # Test VAD
        has_voice, vad_conf = engine.detect_voice_activity(test_audio)
        print(f"Voice detected: {has_voice}, Confidence: {vad_conf:.3f}")

        # Test combined detection
        is_user, vad_conf, speaker_conf = engine.detect_user_voice(test_audio)
        print(f"User voice: {is_user}, VAD: {vad_conf:.3f}, Speaker: {speaker_conf:.3f}")

        # Get metrics
        metrics = engine.get_metrics()
        print(f"Metrics: {metrics}")

    except Exception as e:
        print(f"Error: {e}")
        print("Make sure to compile the C++ library first using CMake")
