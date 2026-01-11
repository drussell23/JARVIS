"""
JARVIS Voice System - Enhanced with professional-grade accuracy
Integrates with Claude API for intelligent voice command processing
"""

# Fix TensorFlow issues before importing ML components
import os

os.environ["TF_CPP_MIN_LOG_LEVEL"] = "3"
os.environ["USE_TORCH"] = "1"
os.environ["USE_TF"] = "0"

import asyncio
import speech_recognition as sr
import pygame
import numpy as np
from typing import Optional, Callable, Dict, List, Tuple, Union, Any
import json
import random
from datetime import datetime
import threading
import queue
import sys
import platform
from anthropic import Anthropic
from dataclasses import dataclass, field
from enum import Enum
import logging
import time
from collections import defaultdict
from functools import wraps

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Import ML trainer
try:
    from voice.voice_ml_trainer import VoiceMLTrainer, VoicePattern

    ML_TRAINING_AVAILABLE = True
except ImportError:
    ML_TRAINING_AVAILABLE = False
    logger.warning(
        "ML training not available. Install required libraries for adaptive learning."
    )

# Import weather service
try:
    from services.weather_service import WeatherService

    WEATHER_SERVICE_AVAILABLE = True
except ImportError:
    WEATHER_SERVICE_AVAILABLE = False
    logger.warning("Weather service not available")

# Use macOS native voice on Mac
if platform.system() == "Darwin":
    from voice.macos_voice import MacOSVoice

    USE_MACOS_VOICE = True
else:
    import pyttsx3

    USE_MACOS_VOICE = False

# JARVIS Personality System Prompt
JARVIS_SYSTEM_PROMPT = """You are JARVIS, Tony Stark's AI assistant. Be concise and helpful.

CRITICAL RULES:
1. Keep responses SHORT (1-2 sentences max unless explaining something complex)
2. Be direct and to the point - no flowery language
3. Don't describe the weather or time unless specifically asked
4. Don't add context about the day/afternoon/evening unless relevant
5. Address the user as "Sir" but don't overuse it
6. Don't use sound effects like "chimes softly" or stage directions
7. No long greetings or farewells

Examples of GOOD responses:
- "Yes, sir?" (when activated)
- "The weather is 24 degrees, overcast clouds."
- "Certainly. The calculation equals 42."
- "I'll check that for you."
- "Done. Anything else?"

Examples of BAD responses (avoid these):
- "Good afternoon, sir. The weather is lovely today..."
- "A gentle chime sounds as I activate..."
- Multiple paragraphs of context
- Asking multiple questions at once
Busy period: "Ready when you are, sir. What's the priority?"

Remember: You're not just an AI following a script - you're JARVIS, a sophisticated assistant with genuine personality. Each interaction should feel fresh and authentic."""

# Voice-specific system prompt for Anthropic
VOICE_OPTIMIZATION_PROMPT = """You are processing voice commands for JARVIS. Voice commands differ from typed text:

Context: This was spoken aloud and may contain:
- Recognition errors
- Informal speech patterns
- Missing punctuation
- Homophones (e.g., "to/too/two")

Previous context: {context}
Voice command: "{command}"
Confidence: {confidence}
Detected intent: {intent}

If confidence is low or the command seems unclear:
1. Provide your best interpretation
2. Ask a clarifying question if needed

Natural response guidelines:
- Vary your greetings - don't use the same pattern
- Keep responses conversational length (what feels natural to say aloud)
- Add personality through word choice and observations
- Reference context when relevant (time, previous conversations, etc.)

Examples of natural variations:
Instead of "How may I assist you?" try:
- "What can I do for you?"
- "What's on your mind?"
- "Need something?"
- "I'm listening."

Respond as JARVIS would - sophisticated, natural, and genuinely helpful."""


# ===================================================================
# ADVANCED ASYNC ARCHITECTURE - Integrated from async_pipeline.py
# Ultra-robust, event-driven, zero-hardcoding async voice system
# ===================================================================

class AdaptiveCircuitBreaker:
    """
    Advanced circuit breaker with adaptive thresholds for voice recognition.
    Prevents system overload and auto-recovers from failures.
    """

    def __init__(self, initial_threshold: int = 5, initial_timeout: int = 30, adaptive: bool = True):
        self.failure_count = 0
        self.success_count = 0
        self.threshold = initial_threshold
        self.timeout = initial_timeout
        self.state = "CLOSED"  # CLOSED, OPEN, HALF_OPEN
        self.last_failure_time = None
        self.adaptive = adaptive
        self.failure_history: List[float] = []
        self.success_rate_history: List[float] = []
        self._total_calls = 0
        self._successful_calls = 0

    @property
    def success_rate(self) -> float:
        """Calculate current success rate"""
        if self._total_calls == 0:
            return 1.0
        return self._successful_calls / self._total_calls

    async def call(self, func: Callable, *args, **kwargs):
        """Execute function with adaptive circuit breaker protection"""
        if self.state == "OPEN":
            if time.time() - self.last_failure_time > self.timeout:
                logger.info(f"[ASYNC] Circuit breaker: transitioning to HALF_OPEN (threshold={self.threshold})")
                self.state = "HALF_OPEN"
            else:
                retry_in = int(self.timeout - (time.time() - self.last_failure_time))
                raise Exception(f"[ASYNC] Circuit breaker is OPEN - voice recognition unavailable (retry in {retry_in}s)")

        try:
            start = time.time()
            result = await func(*args, **kwargs)
            duration = time.time() - start

            self.on_success(duration)
            return result

        except Exception as e:
            self.on_failure()
            raise e

    def on_success(self, duration: float):
        """Handle successful execution with adaptive learning"""
        self.success_count += 1
        self._total_calls += 1
        self._successful_calls += 1
        self.failure_count = max(0, self.failure_count - 1)

        if self.state == "HALF_OPEN":
            logger.info("[ASYNC] Circuit breaker: transitioning to CLOSED")
            self.state = "CLOSED"

        # Adaptive threshold adjustment
        if self.adaptive:
            success_rate = self.success_rate
            self.success_rate_history.append(success_rate)

            # Increase threshold if success rate is high
            if success_rate > 0.95 and self.threshold < 20:
                self.threshold += 1
                logger.debug(f"[ASYNC] Increased circuit breaker threshold to {self.threshold}")

    def on_failure(self):
        """Handle failed execution with adaptive learning"""
        self.failure_count += 1
        self._total_calls += 1
        self.last_failure_time = time.time()
        self.failure_history.append(time.time())

        # Adaptive threshold adjustment
        if self.adaptive and len(self.failure_history) > 10:
            # Check if failure rate is increasing
            recent_failures = [f for f in self.failure_history if time.time() - f < 60]
            if len(recent_failures) > 5:
                self.threshold = max(3, self.threshold - 1)
                logger.debug(f"[ASYNC] Decreased circuit breaker threshold to {self.threshold}")

        if self.failure_count >= self.threshold:
            logger.warning(f"[ASYNC] Circuit breaker: transitioning to OPEN (failures={self.failure_count})")
            self.state = "OPEN"


class AsyncEventBus:
    """
    Event-driven pub/sub system for async voice events.
    Enables decoupled, scalable voice command processing.
    """

    def __init__(self):
        self.subscribers: Dict[str, List[Callable]] = defaultdict(list)
        self.event_history: List[Dict[str, Any]] = []
        self.max_history = 100

    def subscribe(self, event_type: str, handler: Callable):
        """Subscribe to an event type"""
        self.subscribers[event_type].append(handler)
        logger.debug(f"[ASYNC-EVENT] Subscribed to '{event_type}': {handler.__name__}")

    def unsubscribe(self, event_type: str, handler: Callable):
        """Unsubscribe from an event type"""
        if handler in self.subscribers[event_type]:
            self.subscribers[event_type].remove(handler)
            logger.debug(f"[ASYNC-EVENT] Unsubscribed from '{event_type}': {handler.__name__}")

    async def publish(self, event_type: str, data: Any = None):
        """Publish an event to all subscribers"""
        event = {
            "type": event_type,
            "data": data,
            "timestamp": time.time(),
            "id": f"{event_type}_{int(time.time() * 1000)}"
        }

        # Store in history
        self.event_history.append(event)
        if len(self.event_history) > self.max_history:
            self.event_history.pop(0)

        # Notify subscribers asynchronously
        handlers = self.subscribers.get(event_type, [])
        logger.debug(f"[ASYNC-EVENT] Publishing '{event_type}' to {len(handlers)} handlers")

        tasks = []
        for handler in handlers:
            try:
                if asyncio.iscoroutinefunction(handler):
                    tasks.append(asyncio.create_task(handler(data)))
                else:
                    # Run sync handlers in executor
                    tasks.append(asyncio.create_task(
                        asyncio.get_event_loop().run_in_executor(None, handler, data)
                    ))
            except Exception as e:
                logger.error(f"[ASYNC-EVENT] Error publishing to handler {handler.__name__}: {e}")

        # Wait for all handlers to complete
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


@dataclass
class VoiceTask:
    """Represents an async voice recognition task"""
    task_id: str
    text: Optional[str] = None
    confidence: float = 0.0
    timestamp: float = field(default_factory=time.time)
    status: str = "pending"  # pending, processing, completed, failed
    result: Optional[Any] = None
    error: Optional[str] = None
    priority: int = 0  # 0=normal, 1=high, 2=critical
    retries: int = 0
    max_retries: int = 3


class AsyncVoiceQueue:
    """
    Priority-based async queue for voice commands.
    Ensures fair processing and handles backpressure.
    """

    def __init__(self, maxsize: int = 100):
        self.queue: asyncio.PriorityQueue = asyncio.PriorityQueue(maxsize=maxsize)
        self.processing_count = 0
        self.max_concurrent = 3  # Process up to 3 voice commands concurrently
        self.tasks_in_flight: Dict[str, VoiceTask] = {}

    async def enqueue(self, task: VoiceTask):
        """Add task to queue with priority"""
        # Priority queue uses tuples: (priority, task)
        # Lower number = higher priority
        await self.queue.put((-task.priority, task))
        logger.info(f"[ASYNC-QUEUE] Enqueued task {task.task_id} (priority={task.priority}, queue_size={self.queue.qsize()})")

    async def dequeue(self) -> VoiceTask:
        """Get next task from queue"""
        _, task = await self.queue.get()
        self.processing_count += 1
        self.tasks_in_flight[task.task_id] = task
        logger.debug(f"[ASYNC-QUEUE] Dequeued task {task.task_id} (in_flight={self.processing_count})")
        return task

    def complete_task(self, task_id: str):
        """Mark task as completed"""
        if task_id in self.tasks_in_flight:
            del self.tasks_in_flight[task_id]
            self.processing_count = max(0, self.processing_count - 1)
            self.queue.task_done()
            logger.debug(f"[ASYNC-QUEUE] Completed task {task_id} (in_flight={self.processing_count})")

    def is_full(self) -> bool:
        """Check if queue is full"""
        return self.queue.full()

    def size(self) -> int:
        """Get current queue size"""
        return self.queue.qsize()


class VoiceConfidence(Enum):
    """Confidence levels for voice detection"""

    VERY_LOW = 0.2
    LOW = 0.4
    MEDIUM = 0.6
    HIGH = 0.8
    VERY_HIGH = 0.95


@dataclass
class VoiceCommand:
    """Structured voice command data"""

    raw_text: str
    confidence: float
    intent: str
    needs_clarification: bool = False
    timestamp: Optional[datetime] = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now()


class EnhancedVoiceEngine:
    """Enhanced speech recognition with confidence scoring, noise reduction, and ML training"""

    def __init__(
        self,
        ml_trainer: Optional["VoiceMLTrainer"] = None,
        ml_enhanced_system: Optional["MLEnhancedVoiceSystem"] = None,
    ):
        # Speech recognition with multiple engines
        self.recognizer = sr.Recognizer()
        self.microphone = sr.Microphone()

        # ===================================================================
        # ADVANCED ADAPTIVE VOICE RECOGNITION SYSTEM
        # Zero hardcoding - all parameters self-optimize based on USER SUCCESS
        # NO environmental noise feedback - focuses purely on user voice patterns
        # ===================================================================

        # Adaptive configuration storage - all parameters self-tune
        self.adaptive_config = {
            'energy_threshold': {
                'current': 200,
                'min': 50,
                'max': 500,
                'history': [],
                'success_rate_by_value': {}  # Track which values work best
            },
            'pause_threshold': {
                'current': 0.5,
                'min': 0.3,
                'max': 1.2,
                'history': [],
                'success_rate_by_value': {}
            },
            'damping': {
                'current': 0.10,
                'min': 0.05,
                'max': 0.25,
                'history': [],
                'success_rate_by_value': {}
            },
            'energy_ratio': {
                'current': 1.3,
                'min': 1.1,
                'max': 2.0,
                'history': [],
                'success_rate_by_value': {}
            },
            'phrase_time_limit': {
                'current': 8,
                'min': 3,
                'max': 15,
                'history': [],
                'success_rate_by_value': {}
            },
            'timeout': {
                'current': 1,
                'min': 0.5,
                'max': 3,
                'history': [],
                'success_rate_by_value': {}
            },
        }

        # Performance tracking for auto-optimization
        self.performance_metrics = {
            'successful_recognitions': 0,
            'failed_recognitions': 0,
            'low_confidence_count': 0,
            'timeout_count': 0,
            'false_activation_count': 0,  # Triggered when user wasn't speaking
            'average_confidence': [],
            'recognition_times': [],
            'consecutive_failures': 0,  # Track streaks to adapt faster
            'consecutive_successes': 0,
            'first_attempt_success_rate': [],  # Track if command works on first try
        }

        # User voice pattern learning (NO environmental noise!)
        self.user_voice_profile = {
            'average_pitch': None,
            'speech_rate': None,  # Words per minute
            'typical_pause_duration': None,
            'command_length_distribution': [],
            'frequently_misrecognized_words': {},
            'command_start_patterns': [],  # Learn how user starts commands
            'preferred_phrasing': {},  # Learn user's vocabulary
        }

        # Multi-engine configuration with performance tracking
        self.recognition_engines = ['google', 'sphinx', 'whisper']
        self.engine_performance = {
            engine: {
                'success': 0,
                'fail': 0,
                'avg_confidence': [],
                'avg_speed': []
            } for engine in self.recognition_engines
        }
        self.current_engine = 'google'  # Start with Google, adapt based on performance

        # Optimization thread control
        self.optimization_thread = None
        self.optimization_interval = 60  # Optimize every 60 seconds
        self.stop_optimization = False

        # Initialize with adaptive settings
        self._initialize_adaptive_recognition()

        # Start background optimization thread
        self._start_optimization_thread()

        # Text-to-speech
        if USE_MACOS_VOICE:
            self.tts_engine: Union[MacOSVoice, Any] = MacOSVoice()
        else:
            self.tts_engine = pyttsx3.init()
        self._setup_voice()

        # Audio feedback
        pygame.mixer.init()
        self.listening = False

        # Noise profile for reduction
        self.noise_profile = None

        # ML trainer for adaptive learning
        self.ml_trainer = ml_trainer
        self.ml_enhanced_system = ml_enhanced_system
        self.last_audio_data = None  # Store for ML training

        # ===================================================================
        # ADVANCED ASYNC COMPONENTS - Integrated from async_pipeline.py
        # ===================================================================

        # Circuit breaker for fault tolerance
        self.circuit_breaker = AdaptiveCircuitBreaker(
            initial_threshold=5,
            initial_timeout=30,
            adaptive=True
        )
        logger.info("[ASYNC] Initialized adaptive circuit breaker")

        # Event bus for event-driven architecture
        self.event_bus = AsyncEventBus()
        logger.info("[ASYNC] Initialized async event bus")

        # Async queue for command processing
        self.voice_queue = AsyncVoiceQueue(maxsize=100)
        logger.info("[ASYNC] Initialized async voice queue")

        # ===================================================================
        # BACKGROUND SYSTEM MONITOR - Non-blocking CPU/Memory tracking
        # ===================================================================

        # Cached system metrics (updated by background monitor)
        self._cached_cpu_usage = 0.0
        self._cached_memory_usage = 0.0
        self._last_metric_update = time.time()
        self._metric_cache_duration = 1.0  # Cache for 1 second
        self._system_monitor_task = None
        self._monitor_running = False

        logger.info("[SYSTEM-MONITOR] Initialized non-blocking system metrics cache")

        # Subscribe to voice events
        self._setup_event_handlers()

        # Intent patterns for better recognition
        self.intent_patterns = {
            "question": [
                "what",
                "when",
                "where",
                "who",
                "why",
                "how",
                "is",
                "are",
                "can",
                "could",
            ],
            "action": [
                "open",
                "close",
                "start",
                "stop",
                "play",
                "pause",
                "set",
                "turn",
                "activate",
                "launch",
            ],
            "information": [
                "tell",
                "show",
                "find",
                "search",
                "look",
                "get",
                "fetch",
                "explain",
            ],
            "system": [
                "system",
                "status",
                "diagnostic",
                "check",
                "monitor",
                "analyze",
                "report",
            ],
            "conversation": [
                "chat",
                "talk",
                "discuss",
                "explain",
                "describe",
                "hello",
                "hi",
            ],
        }

    def _setup_voice(self):
        """Configure JARVIS voice settings"""
        if USE_MACOS_VOICE:
            # macOS voice is already configured with British accent
            self.tts_engine.setProperty("rate", 175)
        else:
            voices = self.tts_engine.getProperty("voices")

            # Try to find a British male voice
            british_voice = None
            if voices:
                for voice in voices:
                    if any(
                        word in voice.name.lower()
                        for word in ["british", "uk", "english"]
                    ):
                        if "male" in voice.name.lower() or not any(
                            word in voice.name.lower() for word in ["female", "woman"]
                        ):
                            british_voice = voice.id
                            break

            if british_voice:
                self.tts_engine.setProperty("voice", british_voice)

            # Set speech rate and volume for JARVIS-like delivery
            self.tts_engine.setProperty("rate", 175)  # Slightly faster than normal
            self.tts_engine.setProperty("volume", 0.9)

    def calibrate_microphone(self, duration: int = 3):
        """Enhanced calibration with noise profiling"""
        with self.microphone as source:
            print("🎤 Calibrating for ambient noise... Please remain quiet.")

            # Adjust for ambient noise with longer duration
            self.recognizer.adjust_for_ambient_noise(source, duration=duration)

            # Record noise sample for profile
            try:
                print("📊 Creating noise profile...")
                audio = self.recognizer.listen(source, timeout=1, phrase_time_limit=5)
                # Store noise profile for future noise reduction
                self.noise_profile = audio.get_raw_data()
                print("✅ Calibration complete. Noise profile created.")
            except:
                print("✅ Calibration complete.")

    def listen_with_confidence(
        self, timeout: int = 1, phrase_time_limit: int = 8
    ) -> Tuple[Optional[str], float]:
        """Listen for speech and return text with confidence score"""
        with self.microphone as source:
            try:
                # Play listening sound
                self._play_sound("listening")
                self.listening = True

                # Clear the buffer
                self.recognizer.adjust_for_ambient_noise(source, duration=0.5)

                audio = self.recognizer.listen(
                    source, timeout=timeout, phrase_time_limit=phrase_time_limit
                )

                self.listening = False

                # Store audio data for ML training
                self.last_audio_data = np.frombuffer(
                    audio.get_raw_data(), dtype=np.int16
                )

                # Try multiple recognition methods for better accuracy
                recognition_results = []
                confidence = 0.0

                # Google Speech Recognition with alternatives
                try:
                    google_result = self.recognizer.recognize_google(
                        audio, show_all=True, language="en-US"
                    )

                    if google_result and "alternative" in google_result:
                        for i, alternative in enumerate(google_result["alternative"]):
                            text = alternative.get("transcript", "").lower()
                            # Google provides confidence only for the first alternative
                            conf = alternative.get("confidence", 0.8 - (i * 0.1))
                            recognition_results.append((text, conf))
                except Exception as e:
                    logger.debug(f"Google recognition failed: {e}")

                # If we have results, return the best one
                if recognition_results:
                    # Sort by confidence
                    recognition_results.sort(key=lambda x: x[1], reverse=True)
                    best_text, best_confidence = recognition_results[0]

                    # Apply confidence adjustments based on audio quality
                    adjusted_confidence = self._adjust_confidence(
                        audio, best_confidence
                    )

                    # Check ML predictions if available
                    if self.ml_trainer and best_text:
                        predicted_correction = self.ml_trainer.predict_correction(
                            best_text,
                            adjusted_confidence,
                            self.ml_trainer.extract_audio_features(
                                self.last_audio_data
                            ),
                        )
                        if predicted_correction:
                            logger.info(
                                f"ML prediction: '{best_text}' -> '{predicted_correction}'"
                            )
                            # You might want to return the prediction with higher confidence
                            # For now, we'll just log it

                    # Record successful recognition for adaptive learning
                    self._record_recognition_result(
                        success=True,
                        confidence=adjusted_confidence,
                        first_attempt=True  # Assume first attempt in listen_with_confidence
                    )

                    return best_text, adjusted_confidence

                # No results - record failure
                self._record_recognition_result(success=False, first_attempt=True)
                return None, 0.0

            except sr.WaitTimeoutError:
                self.listening = False
                # Record timeout for adaptive optimization
                self.performance_metrics['timeout_count'] += 1
                self._record_recognition_result(success=False, first_attempt=True)
                return None, 0.0
            except sr.UnknownValueError:
                self.listening = False
                # Speech detected but not understood - record failure
                self._record_recognition_result(success=False, first_attempt=True)
                return None, 0.0
            except Exception as e:
                logger.error(f"Error in speech recognition: {e}")
                self.listening = False
                # Unexpected error - record failure
                self._record_recognition_result(success=False, first_attempt=True)
                return None, 0.0

    def _adjust_confidence(self, audio: sr.AudioData, base_confidence: float) -> float:
        """Adjust confidence based on audio quality metrics"""
        try:
            # Convert audio to numpy array
            raw_data = np.frombuffer(audio.get_raw_data(), dtype=np.int16)

            # Calculate audio quality metrics
            energy = np.sqrt(np.mean(raw_data**2))  # RMS energy

            # Very quiet audio is less reliable
            if energy < 100:
                base_confidence *= 0.7
            elif energy > 5000:  # Very loud (possible distortion)
                base_confidence *= 0.9

            # Check for clipping
            if np.any(np.abs(raw_data) > 32000):  # Near max int16 value
                base_confidence *= 0.8

            return min(base_confidence, 1.0)
        except:
            return base_confidence

    def detect_intent(self, text: str) -> str:
        """Detect the intent of the command"""
        if not text:
            return "unknown"

        text_lower = text.lower()

        # Check against intent patterns
        for intent, keywords in self.intent_patterns.items():
            if any(keyword in text_lower.split() for keyword in keywords):
                return intent

        return "conversation"  # Default intent

    def speak(self, text: str, interrupt_callback: Optional[Callable] = None):
        """Convert text to speech with JARVIS voice"""
        # Add subtle processing sound
        self._play_sound("processing")

        # Speak
        if USE_MACOS_VOICE:
            self.tts_engine.say_and_wait(text)
        else:
            self.tts_engine.say(text)
            self.tts_engine.runAndWait()

    # ===================================================================
    # ADAPTIVE RECOGNITION METHODS
    # ===================================================================

    def _initialize_adaptive_recognition(self):
        """Initialize adaptive recognition with current config values"""
        try:
            # Apply current adaptive config to recognizer
            self.recognizer.energy_threshold = self.adaptive_config['energy_threshold']['current']
            self.recognizer.pause_threshold = self.adaptive_config['pause_threshold']['current']
            self.recognizer.dynamic_energy_threshold = True
            self.recognizer.dynamic_energy_adjustment_damping = self.adaptive_config['damping']['current']
            self.recognizer.dynamic_energy_ratio = self.adaptive_config['energy_ratio']['current']

            logger.info(f"[ADAPTIVE] Initialized with: energy={self.recognizer.energy_threshold}, "
                       f"pause={self.recognizer.pause_threshold}")
        except Exception as e:
            logger.error(f"[ADAPTIVE] Failed to initialize: {e}")

    def _start_optimization_thread(self):
        """Start background thread to optimize recognition parameters (DEPRECATED - use async version)"""
        # This method is deprecated but kept for compatibility
        # The async version (_start_optimization_async) should be used instead
        logger.warning("[ADAPTIVE] Sync optimization thread is deprecated. Use async version instead.")

    async def _start_optimization_async(self):
        """Start background async task to optimize recognition parameters (NON-BLOCKING)"""
        async def optimize_loop():
            """Continuous optimization based on performance metrics"""
            while not self.stop_optimization:
                try:
                    await asyncio.sleep(self.optimization_interval)

                    # Run optimization in executor to avoid blocking
                    loop = asyncio.get_event_loop()
                    await loop.run_in_executor(None, self._optimize_parameters)

                except asyncio.CancelledError:
                    logger.info("[ADAPTIVE] Optimization task cancelled")
                    break
                except Exception as e:
                    logger.error(f"[ADAPTIVE] Optimization error: {e}")

        # Create and store the task
        self.optimization_task = asyncio.create_task(optimize_loop())
        logger.info("[ADAPTIVE] Async optimization task started")

    def _optimize_parameters(self):
        """
        Dynamically optimize recognition parameters based on success metrics.
        NO environmental noise - purely based on user recognition success!
        """
        total_attempts = self.performance_metrics['successful_recognitions'] + self.performance_metrics['failed_recognitions']

        if total_attempts < 5:
            # Not enough data yet
            return

        success_rate = self.performance_metrics['successful_recognitions'] / total_attempts

        logger.info(f"[ADAPTIVE] Optimizing... Success rate: {success_rate:.2%}, "
                   f"Successes: {self.performance_metrics['successful_recognitions']}, "
                   f"Failures: {self.performance_metrics['failed_recognitions']}")

        # Adaptive strategy based on failure patterns
        if success_rate < 0.7:  # Less than 70% success - need improvement
            # Too many failures - make system more sensitive
            self._adjust_parameter('energy_threshold', direction='decrease', amount=10)
            self._adjust_parameter('pause_threshold', direction='decrease', amount=0.05)
            self._adjust_parameter('timeout', direction='decrease', amount=0.1)
            logger.info("[ADAPTIVE] Low success rate - increasing sensitivity")

        elif self.performance_metrics['false_activation_count'] > total_attempts * 0.3:
            # Too many false activations - make less sensitive
            self._adjust_parameter('energy_threshold', direction='increase', amount=10)
            self._adjust_parameter('pause_threshold', direction='increase', amount=0.05)
            logger.info("[ADAPTIVE] Too many false activations - decreasing sensitivity")

        elif self.performance_metrics['timeout_count'] > total_attempts * 0.3:
            # Too many timeouts - give more time
            self._adjust_parameter('timeout', direction='increase', amount=0.2)
            self._adjust_parameter('phrase_time_limit', direction='increase', amount=1)
            logger.info("[ADAPTIVE] Too many timeouts - extending time limits")

        # Check first-attempt success rate
        if len(self.performance_metrics['first_attempt_success_rate']) >= 10:
            first_attempt_rate = sum(self.performance_metrics['first_attempt_success_rate'][-10:]) / 10
            if first_attempt_rate < 0.5:  # Less than 50% work on first try
                # Speed up responsiveness
                self._adjust_parameter('pause_threshold', direction='decrease', amount=0.05)
                self._adjust_parameter('timeout', direction='decrease', amount=0.1)
                logger.info(f"[ADAPTIVE] First-attempt rate low ({first_attempt_rate:.2%}) - speeding up")

        # Apply optimized values to recognizer
        self._apply_adaptive_config()

    def _adjust_parameter(self, param_name: str, direction: str, amount: float):
        """Adjust a parameter within its min/max range"""
        config = self.adaptive_config[param_name]
        current = config['current']

        # Calculate new value
        if direction == 'increase':
            new_value = min(current + amount, config['max'])
        else:  # decrease
            new_value = max(current - amount, config['min'])

        # Store old value in history
        config['history'].append(current)
        if len(config['history']) > 100:  # Keep last 100 values
            config['history'].pop(0)

        # Update current value
        config['current'] = new_value

        logger.debug(f"[ADAPTIVE] {param_name}: {current:.3f} → {new_value:.3f}")

    def _apply_adaptive_config(self):
        """Apply current adaptive config to recognizer"""
        try:
            self.recognizer.energy_threshold = self.adaptive_config['energy_threshold']['current']
            self.recognizer.pause_threshold = self.adaptive_config['pause_threshold']['current']
            self.recognizer.dynamic_energy_adjustment_damping = self.adaptive_config['damping']['current']
            self.recognizer.dynamic_energy_ratio = self.adaptive_config['energy_ratio']['current']

            logger.debug("[ADAPTIVE] Applied new config to recognizer")
        except Exception as e:
            logger.error(f"[ADAPTIVE] Failed to apply config: {e}")

    def _record_recognition_result(self, success: bool, confidence: float = 0.0,
                                   first_attempt: bool = False, false_activation: bool = False):
        """
        Record recognition result for adaptive learning.
        This is the key feedback loop - NO environmental noise needed!
        """
        if success:
            self.performance_metrics['successful_recognitions'] += 1
            self.performance_metrics['consecutive_successes'] += 1
            self.performance_metrics['consecutive_failures'] = 0

            if confidence > 0:
                self.performance_metrics['average_confidence'].append(confidence)
                if len(self.performance_metrics['average_confidence']) > 100:
                    self.performance_metrics['average_confidence'].pop(0)
        else:
            self.performance_metrics['failed_recognitions'] += 1
            self.performance_metrics['consecutive_failures'] += 1
            self.performance_metrics['consecutive_successes'] = 0

        if false_activation:
            self.performance_metrics['false_activation_count'] += 1

        if first_attempt:
            self.performance_metrics['first_attempt_success_rate'].append(1 if success else 0)
            if len(self.performance_metrics['first_attempt_success_rate']) > 100:
                self.performance_metrics['first_attempt_success_rate'].pop(0)

        # Track which parameter values lead to success
        for param_name, config in self.adaptive_config.items():
            current_value = config['current']
            value_key = f"{current_value:.2f}"

            if value_key not in config['success_rate_by_value']:
                config['success_rate_by_value'][value_key] = {'success': 0, 'fail': 0}

            if success:
                config['success_rate_by_value'][value_key]['success'] += 1
            else:
                config['success_rate_by_value'][value_key]['fail'] += 1

        # Trigger immediate optimization if we hit a streak
        if self.performance_metrics['consecutive_failures'] >= 3:
            logger.warning("[ADAPTIVE] 3 consecutive failures - triggering immediate optimization")
            self._optimize_parameters()
        elif self.performance_metrics['consecutive_successes'] >= 10:
            logger.info("[ADAPTIVE] 10 consecutive successes - system is well-tuned")

    # ===================================================================
    # ASYNC EVENT & QUEUE METHODS
    # ===================================================================

    def _setup_event_handlers(self):
        """Setup async event handlers for voice events"""
        # Subscribe to voice recognition events
        self.event_bus.subscribe("voice_recognized", self._on_voice_recognized)
        self.event_bus.subscribe("voice_failed", self._on_voice_failed)
        self.event_bus.subscribe("circuit_breaker_open", self._on_circuit_breaker_open)
        logger.info("[ASYNC-EVENT] Setup event handlers for voice processing")

    def _on_voice_recognized(self, data: Dict[str, Any]):
        """Handler for voice recognition success"""
        logger.debug(f"[ASYNC-EVENT] Voice recognized: {data.get('text', 'N/A')}")

    def _on_voice_failed(self, data: Dict[str, Any]):
        """Handler for voice recognition failure"""
        logger.warning(f"[ASYNC-EVENT] Voice recognition failed: {data.get('error', 'Unknown error')}")

    def _on_circuit_breaker_open(self, data: Dict[str, Any]):
        """Handler for circuit breaker opening"""
        logger.error(f"[ASYNC-EVENT] Circuit breaker opened - voice recognition temporarily unavailable")

    async def listen_async(self, timeout: int = 1, phrase_time_limit: int = 8, priority: int = 0) -> Tuple[Optional[str], float]:
        """
        Advanced async voice recognition with circuit breaker and event bus.
        Fully integrated with async_pipeline architecture.
        """
        task_id = f"voice_{int(time.time() * 1000)}"

        # Create voice task
        task = VoiceTask(
            task_id=task_id,
            priority=priority,
            timestamp=time.time()
        )

        try:
            # Check if queue is full
            if self.voice_queue.is_full():
                logger.warning(f"[ASYNC] Voice queue is full ({self.voice_queue.size()}/{100}) - rejecting task")
                await self.event_bus.publish("queue_full", {"task_id": task_id})
                return None, 0.0

            # Enqueue task
            await self.voice_queue.enqueue(task)

            # Execute with circuit breaker protection
            async def recognize_wrapper():
                # Run sync listen_with_confidence in executor
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(
                    None,
                    lambda: self.listen_with_confidence(timeout, phrase_time_limit)
                )
                return result

            # Call with circuit breaker
            text, confidence = await self.circuit_breaker.call(recognize_wrapper)

            # Update task
            task.text = text
            task.confidence = confidence
            task.status = "completed"
            task.result = {"text": text, "confidence": confidence}

            # Publish success event
            if text:
                await self.event_bus.publish("voice_recognized", {
                    "task_id": task_id,
                    "text": text,
                    "confidence": confidence,
                    "timestamp": time.time()
                })

            # Complete task in queue
            self.voice_queue.complete_task(task_id)

            return text, confidence

        except Exception as e:
            logger.error(f"[ASYNC] Error in async voice recognition: {e}")

            # Update task as failed
            task.status = "failed"
            task.error = str(e)

            # Publish failure event
            await self.event_bus.publish("voice_failed", {
                "task_id": task_id,
                "error": str(e),
                "timestamp": time.time()
            })

            # Complete task in queue
            self.voice_queue.complete_task(task_id)

            return None, 0.0

    async def process_voice_queue_worker(self):
        """
        Background worker that processes the voice queue.
        Enables concurrent voice command processing.
        """
        logger.info("[ASYNC-WORKER] Started voice queue worker")

        while True:
            try:
                # Check if we can process more tasks
                if self.voice_queue.processing_count >= self.voice_queue.max_concurrent:
                    await asyncio.sleep(0.1)
                    continue

                # Get next task
                task = await self.voice_queue.dequeue()
                logger.debug(f"[ASYNC-WORKER] Processing task {task.task_id}")

                # Process async
                asyncio.create_task(self._process_voice_task(task))

            except asyncio.CancelledError:
                logger.info("[ASYNC-WORKER] Voice queue worker cancelled")
                break
            except Exception as e:
                logger.error(f"[ASYNC-WORKER] Error in queue worker: {e}")
                await asyncio.sleep(1)

    async def _process_voice_task(self, task: VoiceTask):
        """Process a single voice task"""
        try:
            # Execute voice recognition
            text, confidence = await self.listen_async(priority=task.priority)

            # Update task
            task.text = text
            task.confidence = confidence
            task.status = "completed"

        except Exception as e:
            logger.error(f"[ASYNC] Error processing voice task {task.task_id}: {e}")
            task.status = "failed"
            task.error = str(e)

        finally:
            # Always complete the task
            self.voice_queue.complete_task(task.task_id)

    # ===================================================================
    # BACKGROUND SYSTEM MONITOR - Non-blocking CPU/Memory/Performance tracking
    # ===================================================================

    async def start_system_monitor(self):
        """Start the background system monitor for non-blocking metrics"""
        if self._monitor_running:
            logger.warning("[SYSTEM-MONITOR] Monitor already running")
            return

        self._monitor_running = True
        self._system_monitor_task = asyncio.create_task(self._system_monitor_loop())
        logger.info("[SYSTEM-MONITOR] Started background system monitor")

    async def stop_system_monitor(self):
        """Stop the background system monitor"""
        if not self._monitor_running:
            return

        self._monitor_running = False
        if self._system_monitor_task:
            self._system_monitor_task.cancel()
            try:
                await self._system_monitor_task
            except asyncio.CancelledError:
                pass
        logger.info("[SYSTEM-MONITOR] Stopped background system monitor")

    async def _system_monitor_loop(self):
        """
        Background loop that monitors system metrics non-blockingly.
        Updates cached values for instant access without blocking.
        """
        logger.info("[SYSTEM-MONITOR] Monitor loop started")

        # Track monitoring performance
        monitor_iterations = 0
        monitor_errors = 0
        last_alert_time = 0

        while self._monitor_running:
            try:
                # Run psutil in executor to avoid blocking the event loop
                loop = asyncio.get_event_loop()

                # Get CPU usage non-blockingly (interval=None for instant reading)
                cpu_task = loop.run_in_executor(
                    None,
                    lambda: __import__('psutil').cpu_percent(interval=None)
                )

                # Get memory usage non-blockingly
                memory_task = loop.run_in_executor(
                    None,
                    lambda: __import__('psutil').virtual_memory().percent
                )

                # Wait for both with timeout protection
                try:
                    cpu_usage, memory_usage = await asyncio.wait_for(
                        asyncio.gather(cpu_task, memory_task),
                        timeout=2.0
                    )

                    # Update cached values atomically
                    self._cached_cpu_usage = cpu_usage
                    self._cached_memory_usage = memory_usage
                    self._last_metric_update = time.time()

                    monitor_iterations += 1

                    # Alert on high resource usage (max once per 30 seconds)
                    current_time = time.time()
                    if current_time - last_alert_time > 30:
                        if cpu_usage > 80:
                            logger.warning(f"[SYSTEM-MONITOR] High CPU usage: {cpu_usage:.1f}%")
                            await self.event_bus.publish("high_cpu", {"cpu": cpu_usage})
                            last_alert_time = current_time
                        elif memory_usage > 85:
                            logger.warning(f"[SYSTEM-MONITOR] High memory usage: {memory_usage:.1f}%")
                            await self.event_bus.publish("high_memory", {"memory": memory_usage})
                            last_alert_time = current_time

                    # Log stats every 100 iterations (about 100 seconds)
                    if monitor_iterations % 100 == 0:
                        logger.debug(
                            f"[SYSTEM-MONITOR] Stats - CPU: {cpu_usage:.1f}%, "
                            f"Memory: {memory_usage:.1f}%, Iterations: {monitor_iterations}, "
                            f"Errors: {monitor_errors}"
                        )

                except asyncio.TimeoutError:
                    logger.warning("[SYSTEM-MONITOR] Metrics collection timeout")
                    monitor_errors += 1

                # Update every 1 second (non-blocking sleep)
                await asyncio.sleep(self._metric_cache_duration)

            except asyncio.CancelledError:
                logger.info("[SYSTEM-MONITOR] Monitor loop cancelled")
                break
            except Exception as e:
                logger.error(f"[SYSTEM-MONITOR] Error in monitor loop: {e}")
                monitor_errors += 1
                await asyncio.sleep(5)  # Back off on errors

    def get_cached_cpu_usage(self) -> float:
        """
        Get cached CPU usage (non-blocking, instant).
        Returns cached value updated by background monitor.
        """
        # Check if cache is stale
        cache_age = time.time() - self._last_metric_update
        if cache_age > 5.0:  # Cache older than 5 seconds
            logger.warning(
                f"[SYSTEM-MONITOR] CPU cache is stale ({cache_age:.1f}s old). "
                "Monitor may not be running."
            )

        return self._cached_cpu_usage

    def get_cached_memory_usage(self) -> float:
        """
        Get cached memory usage (non-blocking, instant).
        Returns cached value updated by background monitor.
        """
        cache_age = time.time() - self._last_metric_update
        if cache_age > 5.0:
            logger.warning(
                f"[SYSTEM-MONITOR] Memory cache is stale ({cache_age:.1f}s old). "
                "Monitor may not be running."
            )

        return self._cached_memory_usage

    def get_system_health(self) -> Dict[str, Any]:
        """
        Get comprehensive system health metrics (non-blocking).
        Returns cached metrics with metadata.
        """
        cache_age = time.time() - self._last_metric_update

        return {
            "cpu_percent": self._cached_cpu_usage,
            "memory_percent": self._cached_memory_usage,
            "cache_age_seconds": cache_age,
            "cache_fresh": cache_age < 2.0,
            "monitor_running": self._monitor_running,
            "timestamp": self._last_metric_update,
        }

    def _play_sound(self, sound_type: str):
        """Play UI sounds for feedback"""
        # In a real implementation, you'd have actual sound files
        # For now, we'll just use beeps
        if sound_type == "listening":
            print("🎤 *listening*")
        elif sound_type == "processing":
            print("⚡ *processing*")
        elif sound_type == "error":
            print("❌ *error*")
        elif sound_type == "success":
            print("✅ *success*")


class EnhancedJARVISPersonality:
    """Enhanced JARVIS personality with voice-specific intelligence and ML integration"""

    def __init__(
        self, claude_api_key: str, ml_trainer: Optional["VoiceMLTrainer"] = None
    ):
        self.claude = Anthropic(api_key=claude_api_key)
        self.context = []
        self.voice_context = []  # Separate context for voice commands
        self.user_preferences = {
            "name": "Sir",  # Can be customized
            "work_hours": (9, 18),
            "break_reminder": True,
            "humor_level": "moderate",
        }
        self.last_break = datetime.now()

        # Voice command history for learning patterns
        self.command_history = []

        # ML trainer for adaptive learning
        self.ml_trainer = ml_trainer

        # Voice engine reference (for accessing system metrics)
        self._voice_engine = None

        # Initialize weather service
        if WEATHER_SERVICE_AVAILABLE:
            self.weather_service = WeatherService()
        else:
            self.weather_service = None

    def set_voice_engine(self, voice_engine: "EnhancedVoiceEngine"):
        """Set reference to voice engine for system metrics access"""
        self._voice_engine = voice_engine
        logger.info("[PERSONALITY] Voice engine reference set for system metrics")
    
    def _local_command_interpretation(self, text: str, confidence: float) -> str:
        """Local command interpretation when Claude is unavailable or CPU is high"""
        import re
        text_lower = text.lower()
        
        # Common patterns
        if "open" in text_lower:
            app_matches = re.findall(r'open\s+(\w+)', text_lower)
            if app_matches:
                return f"COMMAND: launch_app({app_matches[0]})"
        elif "close" in text_lower:
            app_matches = re.findall(r'close\s+(\w+)', text_lower)
            if app_matches:
                return f"COMMAND: close_app({app_matches[0]})"
        elif "volume" in text_lower:
            if "up" in text_lower:
                return "COMMAND: increase_volume"
            elif "down" in text_lower:
                return "COMMAND: decrease_volume"
            else:
                level_match = re.search(r'(\d+)', text)
                if level_match:
                    return f"COMMAND: set_volume({level_match.group(1)})"
        elif "brightness" in text_lower:
            if "up" in text_lower or "increase" in text_lower:
                return "COMMAND: increase_brightness"
            elif "down" in text_lower or "decrease" in text_lower:
                return "COMMAND: decrease_brightness"
        elif "weather" in text_lower:
            return "QUERY: weather_info"
        elif "time" in text_lower:
            return "QUERY: current_time"
        elif "date" in text_lower:
            return "QUERY: current_date"
        else:
            return f"UNCLEAR: {text} (confidence: {confidence:.0%})"

    async def process_voice_command(self, command: VoiceCommand) -> str:
        """Process voice command with enhanced intelligence"""
        # Add to command history
        self.command_history.append(command)
        if len(self.command_history) > 50:
            self.command_history = self.command_history[-50:]

        # Get context
        context_info = self._get_context_info()
        recent_context = self._get_recent_voice_context()

        # Log for debugging
        logger.info(
            f"Processing command: '{command.raw_text}' (confidence: {command.confidence})"
        )

        # Determine if we need to use voice optimization
        if (
            command.confidence < VoiceConfidence.HIGH.value
            or command.needs_clarification
        ):
            return await self._optimize_voice_command(
                command, context_info, recent_context
            )
        else:
            return await self._process_clear_command(command.raw_text, context_info)

    async def _optimize_voice_command(
        self, command: VoiceCommand, context_info: str, recent_context: str
    ) -> str:
        """Use Anthropic to interpret unclear voice commands"""
        # Build voice-specific prompt
        prompt = VOICE_OPTIMIZATION_PROMPT.format(
            context=recent_context,
            command=command.raw_text,
            confidence=f"{command.confidence:.2f}",
            intent=command.intent,
        )

        # Add any specific context
        if context_info:
            prompt = f"{context_info}\n\n{prompt}"

        # ===================================================================
        # NON-BLOCKING CPU CHECK - Uses cached value from background monitor
        # This is INSTANT and doesn't block the event loop!
        # ===================================================================

        # Get cached CPU usage (non-blocking, instant)
        cpu_usage = getattr(self, '_voice_engine', None)
        if cpu_usage and hasattr(cpu_usage, 'get_cached_cpu_usage'):
            cpu_usage = cpu_usage.get_cached_cpu_usage()
        else:
            # Fallback if voice engine not set (shouldn't happen)
            cpu_usage = 0.0
            logger.warning("[CPU-CHECK] Voice engine not available, skipping CPU check")

        if cpu_usage > 25:  # Don't call Claude if CPU > 25%
            logger.warning(f"[CPU-CHECK] CPU usage too high ({cpu_usage:.1f}%) - using local response")
            return self._local_command_interpretation(command.raw_text, command.confidence)

        # Log CPU check for monitoring
        logger.debug(f"[CPU-CHECK] CPU usage OK ({cpu_usage:.1f}%) - proceeding with Claude call")

        # Get interpretation from Claude only if CPU is low
        message = await asyncio.to_thread(
            self.claude.messages.create,
            model="claude-3-haiku-20240307",  # Fast model for voice processing
            max_tokens=200,
            temperature=0.3,  # Lower temperature for accuracy
            system=JARVIS_SYSTEM_PROMPT,
            messages=[
                *self.voice_context[-5:],  # Include recent voice context
                {"role": "user", "content": prompt},
            ],
        )

        response = message.content[0].text

        # Update voice context
        self.voice_context.append({"role": "user", "content": command.raw_text})
        self.voice_context.append({"role": "assistant", "content": response})

        # Maintain voice context window
        if len(self.voice_context) > 20:
            self.voice_context = self.voice_context[-20:]

        return response

    async def _process_clear_command(self, command: str, context_info: str) -> str:
        """Process clear commands normally"""
        # Check if this is a weather request FIRST - before adding context
        if await self._is_weather_request(command):
            return await self._handle_weather_request(command)

        # For non-weather requests, use simple prompt without context
        enhanced_prompt = f"User command: {command}"

        # Get response from Claude
        message = await asyncio.to_thread(
            self.claude.messages.create,
            model="claude-3-haiku-20240307",
            max_tokens=300,
            system=JARVIS_SYSTEM_PROMPT,
            messages=[*self.context, {"role": "user", "content": enhanced_prompt}],
        )

        response = message.content[0].text

        # Update context
        self.context.append({"role": "user", "content": command})
        self.context.append({"role": "assistant", "content": response})

        # Maintain context window
        if len(self.context) > 20:
            self.context = self.context[-20:]

        return response

    def _get_recent_voice_context(self) -> str:
        """Get recent voice command context"""
        if not self.command_history:
            return "No recent voice commands"

        recent = self.command_history[-3:]
        context_parts = []

        for cmd in recent:
            time_ago = (datetime.now() - cmd.timestamp).seconds
            if time_ago < 60:
                context_parts.append(f"{time_ago}s ago: '{cmd.raw_text}'")
            elif time_ago < 3600:
                context_parts.append(f"{time_ago//60}m ago: '{cmd.raw_text}'")

        return (
            "Recent commands: " + "; ".join(context_parts)
            if context_parts
            else "No recent commands"
        )

    def _get_context_info(self) -> str:
        """Get contextual information for more intelligent responses"""
        current_time = datetime.now()
        context_parts = []

        # Time and day context
        hour = current_time.hour
        day_name = current_time.strftime("%A")

        # Add natural time context
        if hour < 6:
            context_parts.append("Very early morning hours")
        elif hour < 9:
            context_parts.append("Early morning")
        elif hour < 12:
            context_parts.append("Morning")
        elif hour < 14:
            context_parts.append("Midday")
        elif hour < 17:
            context_parts.append("Afternoon")
        elif hour < 20:
            context_parts.append("Evening")
        elif hour < 23:
            context_parts.append("Late evening")
        else:
            context_parts.append("Late night")

        # Weekend context
        if day_name in ["Saturday", "Sunday"]:
            context_parts.append("Weekend")

        # Work hours context - more natural
        work_start, work_end = self.user_preferences["work_hours"]
        if day_name not in ["Saturday", "Sunday"] and work_start <= hour < work_end:
            context_parts.append("During typical work hours")
        elif hour >= work_end and day_name not in ["Saturday", "Sunday"]:
            context_parts.append("After work hours")

        # Break reminder context - more natural
        if self.user_preferences["break_reminder"]:
            time_since_break = (current_time - self.last_break).seconds / 3600
            if time_since_break > 3:
                context_parts.append("User has been active for extended period")

        # Recent interaction patterns
        if self.command_history:
            # Check if user just started interacting
            if len(self.command_history) == 1:
                context_parts.append("First interaction of this session")
            # Check for rapid commands
            elif len(self.command_history) > 2:
                recent_times = [cmd.timestamp for cmd in self.command_history[-3:]]
                time_diffs = [
                    (recent_times[i + 1] - recent_times[i]).seconds
                    for i in range(len(recent_times) - 1)
                ]
                if all(diff < 30 for diff in time_diffs):
                    context_parts.append("User is actively engaged")

        return "Natural context: " + "; ".join(context_parts) if context_parts else ""

    def get_activation_response(self, confidence: float = 1.0) -> str:
        """Get a contextual activation response based on confidence"""
        if confidence < VoiceConfidence.MEDIUM.value:
            # Low confidence responses
            return random.choice(
                [
                    f"I think I heard you, {self.user_preferences['name']}. How may I assist?",
                    f"Apologies if I misheard, {self.user_preferences['name']}. What can I do for you?",
                    "Pardon me, sir. Could you repeat that?",
                ]
            )

        # Keep responses short and simple
        responses = [
            f"Yes, {self.user_preferences['name']}?",
            "Yes?",
            "Sir?",
            "Listening.",
            "Go ahead.",
            "I'm here.",
        ]

        return random.choice(responses)

    async def _is_weather_request(self, command: str) -> bool:
        """Check if command is asking about weather - FAST"""
        weather_keywords = [
            "weather",
            "temperature",
            "forecast",
            "rain",
            "sunny",
            "cloudy",
            "cold",
            "hot",
            "warm",
            "degrees",
            "celsius",
            "fahrenheit",
            "outside",
            "today",
        ]
        command_lower = command.lower()
        # Quick check for common patterns
        if "weather" in command_lower or "temperature" in command_lower:
            logger.info(f"Weather request detected: '{command}'")
            return True
        is_weather = any(keyword in command_lower for keyword in weather_keywords)
        if is_weather:
            logger.info(f"Weather request detected via keywords: '{command}'")
        return is_weather

    async def _handle_weather_request(self, command: str) -> str:
        """Handle weather requests with real data"""
        logger.info(
            f"Handling weather request. Weather service available: {self.weather_service is not None}"
        )
        if not self.weather_service:
            # Fallback to Claude if weather service not available
            enhanced_prompt = f"User is asking about weather: {command}"
            message = await asyncio.to_thread(
                self.claude.messages.create,
                model="claude-3-haiku-20240307",
                max_tokens=150,
                system="You are JARVIS. Give a brief, direct weather response. Be concise.",
                messages=[{"role": "user", "content": enhanced_prompt}],
            )
            return message.content[0].text

        try:
            # Determine if asking about specific location or current location
            command_lower = command.lower()

            # Extract ANY location from command - no hardcoding!
            location = None

            import re

            # Flexible patterns to extract location after prepositions
            patterns = [
                r"(?:weather|temperature|forecast|rain|snow|sunny|cloudy|hot|cold|warm)(?:\s+(?:is|be|like|today))?\s+(?:in|at|for|around|near)\s+(.+?)(?:\s*\?|$)",
                r"(?:what\'?s|how\'?s|is)\s+(?:the\s+)?(?:weather|temperature|it)\s+(?:like\s+)?(?:in|at|for)\s+(.+?)(?:\s*\?|$)",
                r"(?:in|at|for)\s+(.+?)\s+(?:weather|temperature)",
                r"(.+?)\s+weather(?:\s+like)?(?:\s*\?|$)",
            ]

            for pattern in patterns:
                match = re.search(pattern, command_lower, re.IGNORECASE)
                if match:
                    # Extract everything after the preposition as the location
                    location = match.group(1).strip()
                    # Clean up common endings
                    location = re.sub(
                        r"\s*(today|tomorrow|now|please|thanks|thank you)$",
                        "",
                        location,
                        flags=re.IGNORECASE,
                    )
                    location = location.strip(".,!?")

                    if location:
                        logger.info(f"Extracted location from pattern: '{location}'")
                        break

            # If no location found, check if entire query after "weather" might be a location
            if not location:
                # Simple fallback - take everything after weather-related keywords
                weather_match = re.search(
                    r"(?:weather|temperature|forecast)\s+(?:in|at|for|of)?\s*(.+?)(?:\s*\?|$)",
                    command_lower,
                )
                if weather_match:
                    potential_location = weather_match.group(1).strip()
                    if potential_location and len(potential_location) > 2:
                        location = potential_location
                        logger.info(f"Extracted location from fallback: '{location}'")

            # Get weather data
            if location:
                logger.info(f"Getting weather for location: {location}")
                # Pass the full location string to OpenWeatherMap - it handles cities, states, countries
                weather_data = await self.weather_service.get_weather_by_city(location)
            else:
                logger.info("No location specified, using current location")
                # Use current location
                weather_data = await self.weather_service.get_current_weather()

            # Check for errors first
            if weather_data.get("error"):
                return f"I apologize, sir, but I couldn't find weather information for {location}. Perhaps you could verify the location name?"

            # Format response in JARVIS style
            location = weather_data.get("location", "your location")
            temp = weather_data.get("temperature", 0)
            feels_like = weather_data.get("feels_like", temp)
            description = weather_data.get("description", "unknown conditions")
            wind = weather_data.get("wind_speed", 0)

            # Build JARVIS-style response
            response = f"Currently in {location}, we have {description} "
            response += f"with a temperature of {temp} degrees Celsius"

            if abs(feels_like - temp) > 2:
                response += f", though it feels like {feels_like}"

            response += f". Wind speed is {wind} kilometers per hour. "

            # Add personalized suggestions based on conditions
            hour = datetime.now().hour
            if temp > 25:
                response += "Quite warm today, sir. Perhaps consider lighter attire."
            elif temp < 10:
                response += "Rather chilly, sir. I'd recommend a jacket."
            elif "rain" in description.lower():
                response += "Don't forget an umbrella if you're heading out, sir."
            elif "clear" in description.lower() and temp > 18 and hour < 18:
                response += "Beautiful weather for any outdoor activities you might have planned."

            # Update context with actual weather info
            self.context.append({"role": "user", "content": command})
            self.context.append({"role": "assistant", "content": response})

            return response

        except Exception as e:
            logger.error(f"Error getting weather data: {e}")
            # Fallback to Claude
            return await self._process_clear_command(command, self._get_context_info())


class EnhancedJARVISVoiceAssistant:
    """Enhanced JARVIS Voice Assistant with professional-grade accuracy and ML training"""

    def __init__(self, claude_api_key: str, enable_ml_training: bool = True):
        # Initialize ML enhanced system if available
        self.ml_enhanced_system = None
        self.ml_trainer = None

        if enable_ml_training:
            try:
                # Import ML enhanced system
                from voice.ml_enhanced_voice_system import MLEnhancedVoiceSystem

                self.ml_enhanced_system = MLEnhancedVoiceSystem(claude_api_key)
                self.ml_trainer = self.ml_enhanced_system.ml_trainer
                logger.info(
                    "ML Enhanced Voice System initialized with advanced wake word detection"
                )
            except ImportError:
                logger.warning(
                    "ML Enhanced Voice System not available, falling back to basic ML trainer"
                )
                # Fallback to basic ML trainer
                if ML_TRAINING_AVAILABLE:
                    try:
                        self.ml_trainer = VoiceMLTrainer(claude_api_key)
                        logger.info("Basic ML training system initialized")
                    except Exception as e:
                        logger.error(f"Failed to initialize ML trainer: {e}")

        # Initialize components with ML systems - OPTIMIZED
        # Start personality initialization early (it pre-loads weather)
        self.personality = EnhancedJARVISPersonality(
            claude_api_key, ml_trainer=self.ml_trainer
        )

        # Initialize voice engine with both ML systems
        self.voice_engine = EnhancedVoiceEngine(
            ml_trainer=self.ml_trainer, ml_enhanced_system=self.ml_enhanced_system
        )

        # Wire up voice engine reference to personality for system metrics
        self.personality.set_voice_engine(self.voice_engine)
        logger.info("[JARVIS] Voice engine wired to personality for non-blocking metrics")
        self.running = False
        self.command_queue = queue.Queue()

        # Enhanced wake words with variations
        self.wake_words = {
            "primary": ["jarvis", "hey jarvis", "okay jarvis"],
            "variations": ["jar vis", "hey jar vis", "jarv"],  # Handle speech breaks
            "urgent": ["jarvis emergency", "jarvis urgent"],
        }

        # Confidence thresholds (will be dynamically adjusted by ML system)
        self.wake_word_threshold = 0.6
        self.command_threshold = 0.7

        # If ML enhanced system is available, use its personalized thresholds
        if self.ml_enhanced_system:
            user_thresholds = self.ml_enhanced_system.user_thresholds.get("default")
            if user_thresholds:
                self.wake_word_threshold = user_thresholds.wake_word_threshold
                self.command_threshold = user_thresholds.confidence_threshold

        # Special commands
        self.special_commands = {
            "stop listening": self._stop_listening,
            "goodbye": self._shutdown,
            "shut down": self._shutdown,
            "calibrate": self._calibrate,
            "change my name": self._change_name,
            "improve accuracy": self._improve_accuracy,
            "show my voice stats": self._show_voice_stats,
            "export my voice model": self._export_voice_model,
            "personalized tips": self._get_personalized_tips,
            "ml performance": self._show_ml_performance,
        }

    async def _check_wake_word(
        self, text: str, confidence: float, audio_data: Optional[np.ndarray] = None
    ) -> Tuple[bool, Optional[str]]:
        """Enhanced wake word detection with ML-powered personalization"""
        if not text:
            return False, None

        # If ML enhanced system is available, use it for advanced detection
        if self.ml_enhanced_system and audio_data is not None:
            try:
                # Use ML-enhanced detection with 80%+ false positive reduction
                is_wake_word, ml_confidence, rejection_reason = (
                    await self.ml_enhanced_system.detect_wake_word(
                        audio_data,
                        user_id=(
                            self.ml_trainer.current_user
                            if self.ml_trainer
                            else "default"
                        ),
                    )
                )

                if is_wake_word:
                    # Determine wake type based on text
                    text_lower = text.lower()
                    if any(word in text_lower for word in self.wake_words["urgent"]):
                        return True, "urgent"
                    elif any(word in text_lower for word in self.wake_words["primary"]):
                        return True, "primary"
                    else:
                        return True, "ml_detected"
                else:
                    # Log rejection for learning
                    if rejection_reason:
                        logger.debug(f"Wake word rejected: {rejection_reason}")
                    return False, None

            except Exception as e:
                logger.error(f"ML wake word detection error: {e}")
                # Fall back to traditional detection

        # Traditional detection (fallback or when ML not available)
        text_lower = text.lower()

        # Check urgent wake words first
        for wake_word in self.wake_words["urgent"]:
            if wake_word in text_lower:
                return True, "urgent"

        # Check primary wake words
        for wake_word in self.wake_words["primary"]:
            if wake_word in text_lower:
                # Boost confidence if wake word is at the beginning
                if text_lower.startswith(wake_word):
                    confidence += 0.1
                if confidence >= self.wake_word_threshold:
                    return True, "primary"

        # Check variations (with lower threshold)
        for variation in self.wake_words["variations"]:
            if variation in text_lower and confidence >= (
                self.wake_word_threshold - 0.1
            ):
                return True, "variation"

        return False, None

    async def start(self):
        """Start enhanced JARVIS voice assistant"""
        print("\n=== JARVIS Enhanced Voice System Initializing ===")
        print("🚀 Loading professional-grade voice processing...")

        # ===================================================================
        # START BACKGROUND MONITORS - Non-blocking system optimization
        # ===================================================================

        # Start system monitor for non-blocking CPU/memory tracking
        print("📊 Starting background system monitor...")
        await self.voice_engine.start_system_monitor()

        # Start async optimization task
        print("🔧 Starting adaptive optimization...")
        await self.voice_engine._start_optimization_async()

        logger.info("[JARVIS] All background monitors started successfully")

        # Enhanced calibration
        self.voice_engine.calibrate_microphone(duration=3)

        # Startup greeting
        startup_msg = "JARVIS enhanced voice system online. All systems operational."
        self.voice_engine.speak(startup_msg)

        self.running = True
        print("\n🎤 Say 'JARVIS' to activate...")
        print(
            "💡 Tip: For better accuracy, speak clearly and wait for the listening indicator"
        )

        # Start wake word detection
        await self._wake_word_loop()

    async def _wake_word_loop(self):
        """Enhanced wake word detection loop with ML integration"""
        consecutive_failures = 0

        # Start ML enhanced system if available
        if self.ml_enhanced_system:
            await self.ml_enhanced_system.start()

        while self.running:
            # Listen for wake word with confidence
            text, confidence = self.voice_engine.listen_with_confidence(
                timeout=1, phrase_time_limit=3
            )

            if text:
                # Get audio data for ML processing
                audio_data = getattr(self.voice_engine, "last_audio_data", None)

                # Check for wake word with ML enhancement
                detected, wake_type = await self._check_wake_word(
                    text, confidence, audio_data
                )

                if detected:
                    logger.info(
                        f"Wake word detected: '{text}' (confidence: {confidence:.2f}, type: {wake_type})"
                    )
                    consecutive_failures = 0

                    # Update environmental profile if ML system is available
                    if self.ml_enhanced_system and audio_data is not None:
                        await self.ml_enhanced_system.update_environmental_profile(
                            audio_data
                        )

                    await self._handle_activation(
                        confidence, wake_type or "normal", text
                    )
                else:
                    # Track false positives for ML learning
                    if self.ml_enhanced_system and audio_data is not None:
                        # Check if this was a near-miss
                        if any(
                            word in text.lower()
                            for sublist in self.wake_words.values()
                            for word in (
                                sublist if isinstance(sublist, list) else [sublist]
                            )
                        ):
                            logger.debug(
                                f"Near-miss wake word: '{text}' (confidence: {confidence:.2f})"
                            )
                            # This helps the ML system learn what NOT to accept
                            await self.ml_enhanced_system.process_user_feedback(
                                f"near_miss_{datetime.now().timestamp()}",
                                was_correct=False,
                            )

            # Recalibrate if we're getting too many failures
            consecutive_failures += 1
            if consecutive_failures > 30:  # About 30 seconds of failures
                logger.info("Recalibrating due to consecutive failures")
                self.voice_engine.calibrate_microphone(duration=1)
                consecutive_failures = 0

            # Small delay to prevent CPU overuse
            await asyncio.sleep(0.1)

    async def _handle_activation(
        self, wake_confidence: float, wake_type: str, full_text: Optional[str] = None
    ):
        """Enhanced activation handling"""
        # Check if command was included with wake word
        command_text = None
        command_confidence = wake_confidence

        if full_text:
            # Extract command after wake word
            text_lower = full_text.lower()
            for wake_list in self.wake_words.values():
                for wake_word in (
                    wake_list if isinstance(wake_list, list) else [wake_list]
                ):
                    if wake_word in text_lower:
                        # Find the wake word position and extract everything after it
                        wake_pos = text_lower.find(wake_word)
                        if wake_pos != -1:
                            potential_command = full_text[
                                wake_pos + len(wake_word) :
                            ].strip()
                            if potential_command:
                                command_text = potential_command
                                print(
                                    f"📝 Command detected with wake word: '{command_text}'"
                                )
                                break
                if command_text:
                    break

        if not command_text:
            # No command with wake word, so respond and listen
            if wake_type == "urgent":
                self.voice_engine.speak(
                    "Emergency protocol activated. What's the situation?"
                )
            else:
                response = self.personality.get_activation_response(wake_confidence)
                self.voice_engine.speak(response)

            # Listen for command with confidence scoring
            print("🎤 Listening for command...")
            command_text, command_confidence = self.voice_engine.listen_with_confidence(
                timeout=5, phrase_time_limit=10
            )

        if command_text:
            # Detect intent
            intent = self.voice_engine.detect_intent(command_text)

            # Create structured command
            command = VoiceCommand(
                raw_text=command_text,
                confidence=command_confidence,
                intent=intent,
                needs_clarification=command_confidence < self.command_threshold,
            )

            await self._process_command(command)
        else:
            # Different responses based on context
            if wake_confidence < 0.7:
                self.voice_engine.speak(
                    "I'm having trouble hearing you clearly, sir. Could you speak up?"
                )
            else:
                self.voice_engine.speak("I didn't catch that, sir. Could you repeat?")

    async def _process_command(
        self, command: VoiceCommand, audio_data: Optional[np.ndarray] = None
    ):
        """Process enhanced voice command with ML training"""
        logger.info(
            f"Command: '{command.raw_text}' (confidence: {command.confidence:.2f}, intent: {command.intent})"
        )

        # Store original command for ML training
        original_command = command.raw_text
        success = True
        corrected_text = None

        # Check for special commands first
        for special_cmd, handler in self.special_commands.items():
            if special_cmd in command.raw_text.lower():
                await handler()
                # Train ML on successful special command
                if self.ml_trainer and audio_data is not None:
                    await self.ml_trainer.learn_from_interaction(
                        recognized_text=original_command,
                        confidence=command.confidence,
                        audio_data=audio_data,
                        corrected_text=None,
                        success=True,
                        context="special_command",
                    )
                return

        # Use ML-enhanced conversation if available
        if self.ml_enhanced_system and command.confidence < 0.8:
            # Get recent conversation context
            context = []
            if hasattr(self.personality, "voice_context"):
                context = self.personality.voice_context[-5:]

            # Use ML system for enhanced understanding
            response = (
                await self.ml_enhanced_system.enhance_conversation_with_anthropic(
                    command.raw_text, context, command.confidence
                )
            )
        else:
            # Process with standard enhanced personality
            response = await self.personality.process_voice_command(command)

        # Check if response indicates a clarification was needed
        if "?" in response and command.needs_clarification:
            # Wait for clarification
            self.voice_engine.speak(response)

            # Listen for clarification
            clarification_text, clarification_confidence = (
                self.voice_engine.listen_with_confidence(
                    timeout=5, phrase_time_limit=10
                )
            )

            if clarification_text:
                corrected_text = clarification_text
                # Re-process with clarification
                clarified_command = VoiceCommand(
                    raw_text=clarification_text,
                    confidence=clarification_confidence,
                    intent=self.voice_engine.detect_intent(clarification_text),
                    needs_clarification=False,
                )
                response = await self.personality.process_voice_command(
                    clarified_command
                )

        # Speak final response
        self.voice_engine.speak(response)

        # Train ML system with the interaction
        if self.ml_trainer:
            # Get audio data from voice engine if not provided
            if audio_data is None and hasattr(self.voice_engine, "last_audio_data"):
                audio_data = self.voice_engine.last_audio_data

            if audio_data is not None:
                await self.ml_trainer.learn_from_interaction(
                    recognized_text=original_command,
                    confidence=command.confidence,
                    audio_data=audio_data,
                    corrected_text=corrected_text,
                    success=success,
                    context=f"intent:{command.intent}",
                )

                # Log ML insights periodically
                user_profile = self.ml_trainer.user_profiles.get(
                    self.ml_trainer.current_user, {}
                )
                voice_patterns = (
                    user_profile.get("voice_patterns", [])
                    if isinstance(user_profile, dict)
                    else []
                )
                if len(voice_patterns) % 20 == 0:
                    insights = self.ml_trainer.get_user_insights()
                    logger.info(
                        f"ML Insights - Accuracy: {insights['recent_accuracy']:.2%}, Total interactions: {insights['total_interactions']}"
                    )

    async def _improve_accuracy(self):
        """Guide user through accuracy improvement"""
        self.voice_engine.speak(
            "Let's improve my accuracy. I'll guide you through a quick calibration."
        )
        await asyncio.sleep(1)

        # Recalibrate with user guidance
        self.voice_engine.speak(
            "First, please remain quiet while I calibrate for background noise."
        )
        self.voice_engine.calibrate_microphone(duration=4)

        self.voice_engine.speak(
            "Excellent. Now, please say 'Hey JARVIS' three times, pausing between each."
        )

        # Collect samples
        samples = []
        for i in range(3):
            self.voice_engine.speak(f"Sample {i+1} of 3. Please say 'Hey JARVIS'.")
            text, confidence = self.voice_engine.listen_with_confidence(timeout=5)
            if text:
                samples.append((text, confidence))
                self.voice_engine.speak("Got it.")
            else:
                self.voice_engine.speak("I didn't catch that. Let's try again.")
                i -= 1

        # Analyze samples
        if samples:
            avg_confidence = np.mean([s[1] for s in samples])
            if avg_confidence > 0.8:
                self.voice_engine.speak(
                    f"Excellent! Your voice is coming through clearly with {avg_confidence*100:.0f}% confidence."
                )
            elif avg_confidence > 0.6:
                self.voice_engine.speak(
                    f"Good. I'm detecting your voice with {avg_confidence*100:.0f}% confidence. Try speaking a bit louder or clearer."
                )
            else:
                self.voice_engine.speak(
                    f"I'm having some difficulty. Only {avg_confidence*100:.0f}% confidence. You may want to check your microphone or reduce background noise."
                )

        self.voice_engine.speak("Calibration complete. My accuracy should be improved.")

    async def _stop_listening(self):
        """Temporarily stop listening"""
        self.voice_engine.speak(
            "Going into standby mode, sir. Say 'JARVIS' when you need me."
        )
        # Continue wake word loop

    async def _shutdown(self):
        """Shutdown JARVIS with complete cleanup of all background tasks"""
        self.voice_engine.speak("Shutting down. Goodbye, sir.")

        logger.info("[JARVIS] Starting shutdown sequence...")

        # Stop system monitor
        logger.info("[JARVIS] Stopping system monitor...")
        await self.voice_engine.stop_system_monitor()

        # Stop optimization task
        logger.info("[JARVIS] Stopping optimization task...")
        self.voice_engine.stop_optimization = True
        if hasattr(self.voice_engine, 'optimization_task'):
            self.voice_engine.optimization_task.cancel()
            try:
                await self.voice_engine.optimization_task
            except asyncio.CancelledError:
                pass

        # Stop ML enhanced system if running
        if self.ml_enhanced_system:
            logger.info("[JARVIS] Stopping ML enhanced system...")
            await self.ml_enhanced_system.stop()

        self.running = False
        logger.info("[JARVIS] Shutdown complete")

    async def _calibrate(self):
        """Recalibrate microphone"""
        self.voice_engine.speak("Recalibrating audio sensors.")
        self.voice_engine.calibrate_microphone(duration=3)
        self.voice_engine.speak("Calibration complete.")

    async def _change_name(self):
        """Change how JARVIS addresses the user"""
        self.voice_engine.speak("What would you prefer I call you?")
        name_text, confidence = self.voice_engine.listen_with_confidence(timeout=5)

        if name_text and confidence > 0.5:
            # Clean up the name using AI if confidence is low
            if confidence < 0.8:
                command = VoiceCommand(
                    raw_text=name_text,
                    confidence=confidence,
                    intent="name_change",
                    needs_clarification=True,
                )
                # Process through AI for clarification
                processed = await self.personality.process_voice_command(command)
                # Extract name from response (simplified)
                name = (
                    name_text.replace("call me", "")
                    .replace("my name is", "")
                    .strip()
                    .title()
                )
            else:
                # High confidence - process directly
                name = (
                    name_text.replace("call me", "")
                    .replace("my name is", "")
                    .strip()
                    .title()
                )

            self.personality.user_preferences["name"] = name
            self.voice_engine.speak(
                f"Very well. I shall address you as {name} from now on."
            )
        else:
            self.voice_engine.speak(
                "I didn't catch that. Maintaining current designation."
            )

    async def _show_voice_stats(self):
        """Show user's voice interaction statistics"""
        if not self.ml_trainer:
            self.voice_engine.speak(
                "Voice statistics are not available. ML training is not enabled."
            )
            return

        insights = self.ml_trainer.get_user_insights()

        if "error" in insights:
            self.voice_engine.speak(
                "No voice statistics available yet. Keep using voice commands to build your profile."
            )
            return

        # Prepare summary
        stats_summary = f"""Your voice interaction statistics:
        
Total interactions: {insights['total_interactions']}
Recent accuracy: {insights['recent_accuracy']:.0%}
Most used command: {insights['top_commands'][0][0] if insights['top_commands'] else 'None'}

You've used voice commands {insights['total_interactions']} times with {insights['recent_accuracy']:.0%} accuracy recently."""

        self.voice_engine.speak(stats_summary)

        # Log detailed stats
        logger.info(f"Voice Stats: {json.dumps(insights, indent=2, default=str)}")

    async def _export_voice_model(self):
        """Export user's voice model"""
        if not self.ml_trainer:
            self.voice_engine.speak(
                "Voice model export is not available. ML training is not enabled."
            )
            return

        try:
            export_path = self.ml_trainer.export_user_model()
            if export_path:
                self.voice_engine.speak(
                    f"Your voice model has been exported successfully. Check the models directory."
                )
                logger.info(f"Voice model exported to: {export_path}")
            else:
                self.voice_engine.speak(
                    "Unable to export voice model. No data available."
                )
        except Exception as e:
            logger.error(f"Error exporting voice model: {e}")
            self.voice_engine.speak("There was an error exporting your voice model.")

    async def _get_personalized_tips(self):
        """Get personalized tips based on ML analysis"""
        if not self.ml_trainer:
            self.voice_engine.speak(
                "Personalized tips are not available. ML training is not enabled."
            )
            return

        self.voice_engine.speak(
            "Analyzing your voice patterns to generate personalized tips..."
        )

        try:
            tips = await self.ml_trainer.generate_personalized_tips()
            self.voice_engine.speak(tips)
        except Exception as e:
            logger.error(f"Error generating tips: {e}")
            self.voice_engine.speak(
                "I encountered an error while generating tips. Please try again later."
            )

    async def _show_ml_performance(self):
        """Show ML system performance metrics"""
        if not self.ml_enhanced_system:
            self.voice_engine.speak(
                "ML performance metrics are not available. The enhanced ML system is not enabled."
            )
            return

        # Get performance metrics
        metrics = self.ml_enhanced_system.get_performance_metrics()

        # Format for speech
        performance_summary = f"""ML Performance Report:
        
Total wake word detections: {metrics['total_detections']}
Accuracy: {metrics['precision']:.1%}
False positive reduction: {metrics['false_positive_reduction']:.1f}%
Current noise level: {metrics['environmental_noise']:.3f}
System adaptations: {metrics['adaptations_made']}

Your personalized thresholds have been optimized to reduce false positives by {metrics['false_positive_reduction']:.0f}%."""

        self.voice_engine.speak(performance_summary)

        # Log detailed metrics
        logger.info(f"Detailed ML Performance Metrics: {json.dumps(metrics, indent=2)}")


# =============================================================================
# Trinity Voice Coordinator — Re-export from canonical implementation
# =============================================================================
# NOTE: The canonical TrinityVoiceCoordinator is in backend/core/trinity_voice_coordinator.py
# This re-export provides backward compatibility for any imports from this module.

try:
    from backend.core.trinity_voice_coordinator import (
        TrinityVoiceCoordinator,
        VoiceContext as VoicePersonality,  # Alias for backward compatibility
        VoicePriority,
        get_voice_coordinator as get_trinity_voice_coordinator,
        announce,
    )
    TRINITY_VOICE_AVAILABLE = True
except ImportError:
    TRINITY_VOICE_AVAILABLE = False
    logger.warning("[jarvis_voice] TrinityVoiceCoordinator not available - using fallback")
    
    # Fallback stubs
    class VoicePersonality(Enum):
        """Fallback voice personality enum."""
        STARTUP = "startup"
        NARRATOR = "narrator"
        RUNTIME = "runtime"
        ALERT = "alert"
        CELEBRATION = "celebration"
    
    def get_trinity_voice_coordinator():
        """Fallback that returns None."""
        return None
    
    async def announce(*args, **kwargs):
        """Fallback announce that does nothing."""
        pass


async def main():
    """Main entry point"""
    # Get API key from environment
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        print("Error: ANTHROPIC_API_KEY not set in environment")
        return

    # Initialize enhanced JARVIS
    jarvis = EnhancedJARVISVoiceAssistant(api_key)

    try:
        await jarvis.start()
    except KeyboardInterrupt:
        print("\nShutting down JARVIS...")
        await jarvis._shutdown()
    except Exception as e:
        logger.error(f"Error: {e}")
        print(f"Error: {e}")


if __name__ == "__main__":
    asyncio.run(main())
