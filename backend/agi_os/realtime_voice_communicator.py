"""
JARVIS AGI OS - Real-Time Voice Communicator

Advanced async voice communication system using Daniel TTS voice for
real-time proactive communication. Provides:

- Non-blocking async speech synthesis
- Priority-based speech queue
- Voice mode adaptation (normal, urgent, thoughtful, conversational)
- Contextual speech patterns (time of day, user activity)
- Interrupt handling for urgent messages
- Speech memory to avoid repetition
- Integration with approval system for interactive dialogs

Usage:
    from agi_os import get_voice_communicator, VoiceMode

    voice = await get_voice_communicator()

    # Simple speech
    await voice.speak("Good morning, Derek")

    # With mode
    await voice.speak("Alert detected", mode=VoiceMode.URGENT)

    # Priority speech (interrupts queue)
    await voice.speak_priority("Security alert", VoicePriority.CRITICAL)

    # Interactive dialog
    response = await voice.ask_yes_no("Should I fix this error?")
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
import logging
import time
import weakref
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# =============================================================================
# v8.0: UNIFIED SPEECH STATE INTEGRATION
# =============================================================================
# Import the centralized speech state manager for self-voice suppression.
# This ensures ALL parts of the system (backend, frontend, VBI) know when
# JARVIS is speaking and should reject incoming audio.
# =============================================================================
try:
    from core.unified_speech_state import (
        get_speech_state_manager_sync,
        SpeechSource,
    )
    UNIFIED_SPEECH_STATE_AVAILABLE = True
except ImportError:
    UNIFIED_SPEECH_STATE_AVAILABLE = False
    logger.debug("UnifiedSpeechStateManager not available - using local state only")


class VoiceMode(Enum):
    """Voice modes for different contexts."""
    NORMAL = "normal"           # Standard conversational tone
    URGENT = "urgent"           # Faster, more assertive
    THOUGHTFUL = "thoughtful"   # Slower, contemplative
    QUIET = "quiet"             # Softer, slower for late hours
    CONVERSATIONAL = "conversational"  # Natural back-and-forth
    NOTIFICATION = "notification"  # Brief, informative
    APPROVAL = "approval"       # Clear, question-oriented


class VoicePriority(Enum):
    """Priority levels for speech messages."""
    BACKGROUND = 0      # Can wait, don't interrupt
    LOW = 1             # Normal queue
    NORMAL = 2          # Standard priority
    HIGH = 3            # Move to front of queue
    URGENT = 4          # Interrupt non-critical speech
    CRITICAL = 5        # Immediate interrupt, security/safety


@dataclass
class SpeechMessage:
    """Represents a speech message in the queue."""
    text: str
    mode: VoiceMode
    priority: VoicePriority
    timestamp: datetime = field(default_factory=datetime.now)
    callback: Optional[Callable[[], None]] = None
    message_id: str = field(default="")
    context: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.message_id:
            # Generate unique ID based on content hash
            self.message_id = hashlib.md5(
                f"{self.text}{self.timestamp.isoformat()}".encode()
            ).hexdigest()[:12]


@dataclass
class VoiceModeConfig:
    """Configuration for a voice mode."""
    rate: int           # Words per minute
    voice: str          # Voice name (Daniel)
    pause_multiplier: float  # Multiplier for pauses
    pitch_adjustment: int = 0  # Future use


@dataclass
class UserUtterance:
    """Represents a user utterance captured via voice input."""
    text: str
    source: str = "unknown"
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)
    utterance_id: str = field(default="")

    def __post_init__(self) -> None:
        if not self.utterance_id:
            self.utterance_id = hashlib.md5(
                f"{self.text}{self.source}{self.timestamp.isoformat()}".encode()
            ).hexdigest()[:12]


class RealTimeVoiceCommunicator:
    """
    Advanced async voice communicator for AGI OS.

    Provides real-time, non-blocking voice communication with:
    - Priority-based message queue
    - Voice mode adaptation
    - Context-aware speech patterns
    - Speech memory to avoid repetition
    - Interrupt handling
    """

    def __init__(self):
        """Initialize the voice communicator."""
        # Voice configuration
        self._primary_voice = "Daniel"
        self._available_voices: Dict[str, str] = {}
        self._british_voices: List[str] = []

        # Queue management
        self._speech_queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._is_speaking = False
        self._current_message: Optional[SpeechMessage] = None
        self._speech_task: Optional[asyncio.Task] = None
        self._immediate_speech_lock: asyncio.Lock = asyncio.Lock()  # Prevents voice overlap
        self._current_speech_process: Optional[asyncio.subprocess.Process] = None

        # State management
        self._running = False
        self._paused = False
        self._muted = False

        # Speech memory (avoid repetition)
        self._recent_messages: OrderedDict[str, datetime] = OrderedDict()
        self._max_memory_size = 100
        self._repetition_cooldown = timedelta(minutes=5)

        # Voice mode configurations (dynamically adjustable)
        self._mode_configs: Dict[VoiceMode, VoiceModeConfig] = {
            VoiceMode.NORMAL: VoiceModeConfig(rate=175, voice="Daniel", pause_multiplier=1.0),
            VoiceMode.URGENT: VoiceModeConfig(rate=200, voice="Daniel", pause_multiplier=0.7),
            VoiceMode.THOUGHTFUL: VoiceModeConfig(rate=155, voice="Daniel", pause_multiplier=1.3),
            VoiceMode.QUIET: VoiceModeConfig(rate=145, voice="Daniel", pause_multiplier=1.2),
            VoiceMode.CONVERSATIONAL: VoiceModeConfig(rate=170, voice="Daniel", pause_multiplier=1.0),
            VoiceMode.NOTIFICATION: VoiceModeConfig(rate=185, voice="Daniel", pause_multiplier=0.8),
            VoiceMode.APPROVAL: VoiceModeConfig(rate=165, voice="Daniel", pause_multiplier=1.1),
        }

        # Callbacks for events
        self._callbacks: Dict[str, Set[weakref.ref]] = {
            'speech_started': set(),
            'speech_finished': set(),
            'speech_interrupted': set(),
            'queue_empty': set(),
        }

        # Context tracking
        self._last_speech_time: Optional[datetime] = None
        self._speech_count_today = 0
        self._user_preferences: Dict[str, Any] = {}

        # Bidirectional voice loop state
        self._state_lock = asyncio.Lock()
        self._listening_window: Optional[Dict[str, Any]] = None
        self._listening_timeout_task: Optional[asyncio.Task] = None
        self._listening_window_counter = 0
        self._utterance_history: Deque[UserUtterance] = deque(maxlen=250)
        self._pending_utterances: Deque[UserUtterance] = deque(maxlen=250)
        self._utterance_waiters: "OrderedDict[str, Tuple[asyncio.Future, Optional[Callable[[UserUtterance], bool]]]]" = OrderedDict()

        # Lifecycle-managed background tasks (timeouts, delayed opens, async notifications)
        self._background_tasks: Set[asyncio.Task] = set()

        # Initialize voices
        self._discover_voices()

        logger.info("RealTimeVoiceCommunicator initialized with voice: %s", self._primary_voice)

    def _discover_voices(self) -> None:
        """Discover available system voices."""
        try:
            _voice_discover_timeout = float(os.getenv("JARVIS_VOICE_DISCOVER_TIMEOUT", "15"))
            result = subprocess.run(
                ['say', '-v', '?'],
                capture_output=True,
                text=True,
                timeout=_voice_discover_timeout
            )

            for line in result.stdout.split('\n'):
                if line.strip():
                    parts = line.split()
                    if len(parts) >= 2:
                        voice_name = parts[0]
                        lang_desc = ' '.join(parts[1:])
                        self._available_voices[voice_name] = lang_desc

                        # Check for British voices
                        if any(ind in lang_desc for ind in ['en_GB', 'British', 'United Kingdom']):
                            self._british_voices.append(voice_name)
                        elif voice_name in ['Daniel', 'Oliver', 'Kate', 'Serena']:
                            self._british_voices.append(voice_name)

            # Prioritize Daniel
            if 'Daniel' in self._available_voices:
                self._primary_voice = 'Daniel'
            elif self._british_voices:
                self._primary_voice = self._british_voices[0]

            logger.debug("Discovered %d voices, %d British, using: %s",
                        len(self._available_voices), len(self._british_voices), self._primary_voice)

        except Exception as e:
            logger.warning("Failed to discover voices: %s", e)
            self._primary_voice = 'Daniel'  # Fallback

    async def start(self) -> None:
        """Start the voice communicator."""
        if self._running:
            return

        self._running = True
        self._speech_task = asyncio.create_task(
            self._speech_processor(),
            name="agi_os_voice_processor"
        )
        logger.info("Voice communicator started")

    async def stop(self) -> None:
        """Stop the voice communicator gracefully."""
        if not self._running:
            return

        self._running = False

        # Stop any current speech
        await self.stop_speaking()
        await self.close_listening_window(reason="voice_shutdown")

        # Cancel processor task
        if self._speech_task:
            self._speech_task.cancel()
            try:
                await self._speech_task
            except asyncio.CancelledError:
                pass

        # Cancel all background tasks
        pending_tasks = list(self._background_tasks)
        for task in pending_tasks:
            task.cancel()
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)
        self._background_tasks.clear()

        # Release any waiters so shutdown is deterministic
        async with self._state_lock:
            for waiter, _ in self._utterance_waiters.values():
                if not waiter.done():
                    waiter.cancel()
            self._utterance_waiters.clear()
            self._pending_utterances.clear()

        logger.info("Voice communicator stopped")

    async def speak(
        self,
        text: str,
        mode: VoiceMode = VoiceMode.NORMAL,
        priority: VoicePriority = VoicePriority.NORMAL,
        callback: Optional[Callable[[], None]] = None,
        context: Optional[Dict[str, Any]] = None,
        allow_repetition: bool = False
    ) -> str:
        """
        Queue a message for speech.

        Args:
            text: Text to speak
            mode: Voice mode for delivery
            priority: Message priority
            callback: Optional callback when speech completes
            context: Optional context data
            allow_repetition: Allow speaking even if recently said

        Returns:
            Message ID for tracking
        """
        if self._muted:
            logger.debug("Voice muted, skipping: %s", text[:50])
            return ""

        # Check for repetition
        text_hash = hashlib.md5(text.encode()).hexdigest()[:16]
        if not allow_repetition and text_hash in self._recent_messages:
            last_time = self._recent_messages[text_hash]
            if datetime.now() - last_time < self._repetition_cooldown:
                logger.debug("Skipping repeated message: %s", text[:50])
                return ""

        # Apply time-based mode adjustments
        mode = self._adjust_mode_for_context(mode)

        # Process text for natural speech
        processed_text = self._process_text(text, mode)

        # Create message
        message = SpeechMessage(
            text=processed_text,
            mode=mode,
            priority=priority,
            callback=callback,
            context=context or {}
        )

        # Add to queue (priority queue uses tuple comparison)
        # Lower priority value = higher priority, so we negate
        await self._speech_queue.put((
            -priority.value,  # Negate for correct ordering
            message.timestamp.timestamp(),
            message
        ))

        # Update memory
        self._recent_messages[text_hash] = datetime.now()
        self._trim_memory()

        logger.debug("Queued message (priority=%s, mode=%s): %s",
                    priority.name, mode.name, text[:50])

        return message.message_id

    async def speak_priority(
        self,
        text: str,
        priority: VoicePriority = VoicePriority.URGENT,
        mode: Optional[VoiceMode] = None
    ) -> str:
        """
        Speak with priority, potentially interrupting current speech.

        Args:
            text: Text to speak
            priority: Priority level
            mode: Voice mode (auto-selected based on priority if not provided)

        Returns:
            Message ID
        """
        # Auto-select mode based on priority
        if mode is None:
            if priority == VoicePriority.CRITICAL:
                mode = VoiceMode.URGENT
            elif priority == VoicePriority.URGENT:
                mode = VoiceMode.URGENT
            else:
                mode = VoiceMode.NORMAL

        # For critical/urgent, interrupt current speech
        if priority.value >= VoicePriority.URGENT.value:
            await self.interrupt_for_priority(priority)

        return await self.speak(text, mode=mode, priority=priority, allow_repetition=True)

    async def speak_and_wait(
        self,
        text: str,
        mode: VoiceMode = VoiceMode.NORMAL,
        timeout: float = 30.0
    ) -> bool:
        """
        Speak and wait for completion.

        Args:
            text: Text to speak
            mode: Voice mode
            timeout: Maximum wait time

        Returns:
            True if speech completed, False if timed out
        """
        completion_event = asyncio.Event()

        def on_complete():
            completion_event.set()

        message_id = await self.speak(
            text,
            mode=mode,
            priority=VoicePriority.HIGH,
            callback=on_complete
        )

        if not message_id:
            return True  # Was skipped (muted or repetition)

        try:
            await asyncio.wait_for(completion_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            logger.warning("Speech timed out: %s", text[:50])
            return False

    async def ask_yes_no(
        self,
        question: str,
        timeout: float = 30.0,
        default: Optional[bool] = None
    ) -> Optional[bool]:
        """
        Ask a yes/no question and wait for response.

        Speaks the question, opens a listening window, and waits for a
        matching yes/no utterance from the voice input pipeline.

        Args:
            question: Question to ask
            timeout: Response timeout
            default: Default if no response

        Returns:
            True for yes, False for no, None if timed out
        """
        timeout = max(1.0, timeout)
        await self.speak_and_wait(question, mode=VoiceMode.APPROVAL, timeout=timeout + 5.0)
        await self.open_listening_window(
            reason="yes_no_question",
            timeout_seconds=timeout,
            metadata={"question": question, "expect": "yes_no", "close_on_utterance": False},
        )

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        response: Optional[bool] = None

        try:
            while loop.time() < deadline:
                remaining = max(0.1, deadline - loop.time())
                utterance = await self.wait_for_user_utterance(
                    timeout=remaining,
                    matcher=lambda item: self._parse_yes_no_intent(item.text) is not None,
                )
                if utterance is None:
                    break

                response = self._parse_yes_no_intent(utterance.text)
                if response is not None:
                    break
        finally:
            await self.close_listening_window(
                reason="answered" if response is not None else "timeout"
            )

        return response if response is not None else default

    async def interrupt_for_priority(self, priority: VoicePriority) -> None:
        """
        Interrupt current speech if priority warrants it.

        Args:
            priority: Priority of incoming message
        """
        if not self._is_speaking or not self._current_message:
            return

        current_priority = self._current_message.priority

        # Only interrupt if new priority is significantly higher
        if priority.value > current_priority.value + 1:
            logger.info("Interrupting speech for priority %s", priority.name)
            await self.stop_speaking()
            self._fire_callbacks('speech_interrupted', self._current_message)

    async def stop_speaking(self) -> None:
        """Stop any current speech immediately — flush AudioBus or kill say."""
        if self._is_speaking:
            try:
                # Try AudioBus flush first
                _bus_enabled = os.getenv(
                    "JARVIS_AUDIO_BUS_ENABLED", "false"
                ).lower() in ("true", "1", "yes")
                if _bus_enabled:
                    try:
                        from backend.audio.audio_bus import get_audio_bus_safe
                        bus = get_audio_bus_safe()
                        if bus is not None and bus.is_running:
                            bus.flush_playback()
                            self._is_speaking = False
                            return
                    except ImportError:
                        pass
                # Legacy: kill say process
                subprocess.run(['killall', 'say'], capture_output=True, timeout=2)
            except Exception as e:
                logger.debug("Error stopping speech: %s", e)

            self._is_speaking = False

    def pause(self) -> None:
        """Pause speech processing."""
        self._paused = True
        logger.info("Voice communicator paused")

    def resume(self) -> None:
        """Resume speech processing."""
        self._paused = False
        logger.info("Voice communicator resumed")

    def mute(self) -> None:
        """Mute all voice output."""
        self._muted = True
        self._spawn_background_task(
            self.stop_speaking(),
            name="voice_mute_stop_speaking",
        )
        logger.info("Voice communicator muted")

    def unmute(self) -> None:
        """Unmute voice output."""
        self._muted = False
        logger.info("Voice communicator unmuted")

    async def _speech_processor(self) -> None:
        """Background task that processes the speech queue."""
        while self._running:
            try:
                # Wait for message with timeout
                try:
                    priority_tuple = await asyncio.wait_for(
                        self._speech_queue.get(),
                        timeout=1.0
                    )
                    _, _, message = priority_tuple
                except asyncio.TimeoutError:
                    continue

                # Skip if paused
                if self._paused:
                    # Put back in queue
                    await self._speech_queue.put(priority_tuple)
                    await asyncio.sleep(0.5)
                    continue

                # Skip if muted
                if self._muted:
                    continue

                # Speak the message
                self._current_message = message
                self._is_speaking = True
                speech_start_time = time.time()
                
                # v8.0: Notify unified speech state manager
                if UNIFIED_SPEECH_STATE_AVAILABLE:
                    try:
                        manager = get_speech_state_manager_sync()
                        self._spawn_background_task(
                            manager.start_speaking(
                                message.text,
                                source=SpeechSource.TTS_BACKEND,
                            ),
                            name="voice_unified_state_start",
                        )
                    except Exception as e:
                        logger.debug(f"Unified speech state start error: {e}")
                
                self._fire_callbacks('speech_started', message)

                try:
                    await self._speak_message(message)
                except Exception as e:
                    logger.error("Error speaking message: %s", e)
                finally:
                    speech_duration_ms = (time.time() - speech_start_time) * 1000
                    self._is_speaking = False
                    self._current_message = None
                    self._last_speech_time = datetime.now()
                    self._speech_count_today += 1
                    
                    # v8.0: Notify unified speech state manager
                    if UNIFIED_SPEECH_STATE_AVAILABLE:
                        try:
                            manager = get_speech_state_manager_sync()
                            self._spawn_background_task(
                                manager.stop_speaking(actual_duration_ms=speech_duration_ms),
                                name="voice_unified_state_stop",
                            )
                        except Exception as e:
                            logger.debug(f"Unified speech state stop error: {e}")

                    self._fire_callbacks('speech_finished', message)

                    if message.callback:
                        try:
                            message.callback()
                        except Exception as e:
                            logger.error("Error in speech callback: %s", e)

                    await self._handle_post_speech_context(message)

                self._speech_queue.task_done()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception("Speech processor error: %s", e)
                await asyncio.sleep(0.5)

    async def _speak_message(self, message: SpeechMessage) -> None:
        """
        Speak a single message — AudioBus path or macOS say fallback.

        Args:
            message: Message to speak
        """
        _bus_enabled = os.getenv(
            "JARVIS_AUDIO_BUS_ENABLED", "false"
        ).lower() in ("true", "1", "yes")

        if _bus_enabled:
            try:
                from backend.voice.engines.unified_tts_engine import get_tts_engine
                tts = await get_tts_engine()
                await tts.speak(message.text, play_audio=True)
                return
            except Exception as e:
                logger.debug("AudioBus speak failed, falling back: %s", e)

        # Legacy: direct macOS say
        config = self._mode_configs.get(message.mode, self._mode_configs[VoiceMode.NORMAL])
        cmd = [
            'say',
            '-v', config.voice,
            '-r', str(config.rate),
            message.text
        ]
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL
        )
        await process.wait()

    def _process_text(self, text: str, mode: VoiceMode) -> str:
        """
        Process text for natural speech.

        Args:
            text: Raw text
            mode: Voice mode for processing

        Returns:
            Processed text with speech markers
        """
        config = self._mode_configs.get(mode, self._mode_configs[VoiceMode.NORMAL])

        # Basic processing
        processed = text

        # Add pauses based on mode
        if config.pause_multiplier >= 1.0:
            # Add subtle pauses for thoughtful/quiet modes
            replacements = {
                '. ': '. ... ',
                '? ': '? ... ',
                '! ': '! ... ',
                ', ': ', .. ',
            }
        else:
            # Minimal pauses for urgent mode
            replacements = {}

        for old, new in replacements.items():
            processed = processed.replace(old, new)

        # Handle special terms
        processed = processed.replace('JARVIS', '[[rate -15]]JARVIS[[rate +15]]')
        processed = processed.replace('Derek', '[[rate -10]]Derek[[rate +10]]')

        return processed

    def _adjust_mode_for_context(self, mode: VoiceMode) -> VoiceMode:
        """
        Adjust voice mode based on current context.

        Args:
            mode: Requested mode

        Returns:
            Adjusted mode based on time of day, etc.
        """
        hour = datetime.now().hour

        # Late night/early morning: force quiet mode unless urgent
        if (hour >= 23 or hour < 7) and mode not in [VoiceMode.URGENT]:
            return VoiceMode.QUIET

        return mode

    def _trim_memory(self) -> None:
        """Trim speech memory to max size."""
        while len(self._recent_messages) > self._max_memory_size:
            self._recent_messages.popitem(last=False)

    def _spawn_background_task(self, coro: Any, name: str) -> asyncio.Task:
        """Create and track a background task so lifecycle cleanup is deterministic."""
        task = asyncio.create_task(coro, name=name)
        self._background_tasks.add(task)

        def _finalize(completed_task: asyncio.Task) -> None:
            self._background_tasks.discard(completed_task)
            try:
                completed_task.result()
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.debug("Background task %s failed: %s", name, exc)

        task.add_done_callback(_finalize)
        return task

    async def open_listening_window(
        self,
        reason: str = "general",
        timeout_seconds: float = 20.0,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Open a backend-driven listening window and notify connected voice clients.

        The frontend uses this signal to enter "waiting for command" mode without
        requiring a wake word for proactive follow-up interactions.
        """
        timeout_seconds = max(1.0, float(timeout_seconds))
        close_on_utterance = bool((metadata or {}).get("close_on_utterance", True))
        state: Dict[str, Any]

        async with self._state_lock:
            self._listening_window_counter += 1
            window_id = f"listen_{self._listening_window_counter}_{int(time.time() * 1000)}"
            now = datetime.now()

            if self._listening_timeout_task:
                self._listening_timeout_task.cancel()
                self._listening_timeout_task = None

            state = {
                "window_id": window_id,
                "reason": reason,
                "opened_at": now.isoformat(),
                "opened_epoch": time.time(),
                "timeout_seconds": timeout_seconds,
                "close_on_utterance": close_on_utterance,
                "metadata": dict(metadata or {}),
            }
            self._listening_window = state

            self._listening_timeout_task = self._spawn_background_task(
                self._expire_listening_window(window_id, timeout_seconds),
                name=f"voice_listening_timeout_{window_id}",
            )

        await self._broadcast_listening_window_state(opened=True, state=state)
        logger.debug(
            "Opened listening window %s (reason=%s timeout=%.1fs)",
            state["window_id"],
            reason,
            timeout_seconds,
        )
        return state

    async def close_listening_window(
        self,
        reason: str = "completed",
        metadata: Optional[Dict[str, Any]] = None,
        window_id: Optional[str] = None,
    ) -> bool:
        """Close the current listening window and notify connected clients."""
        closed_state: Optional[Dict[str, Any]] = None

        async with self._state_lock:
            if not self._listening_window:
                return False

            if window_id and self._listening_window.get("window_id") != window_id:
                return False

            closed_state = {
                **self._listening_window,
                "closed_at": datetime.now().isoformat(),
                "close_reason": reason,
                "close_metadata": dict(metadata or {}),
            }

            if self._listening_timeout_task:
                self._listening_timeout_task.cancel()
                self._listening_timeout_task = None

            self._listening_window = None

        await self._broadcast_listening_window_state(opened=False, state=closed_state)
        logger.debug(
            "Closed listening window %s (reason=%s)",
            closed_state.get("window_id"),
            reason,
        )
        return True

    async def _expire_listening_window(self, window_id: str, timeout_seconds: float) -> None:
        """Timeout handler for listening window lifecycle."""
        try:
            await asyncio.sleep(timeout_seconds)
            await self.close_listening_window(reason="timeout", window_id=window_id)
        except asyncio.CancelledError:
            pass

    async def register_user_utterance(
        self,
        text: str,
        source: str = "websocket",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[UserUtterance]:
        """
        Register a user utterance from the STT/WebSocket path.

        This is the input side of the bidirectional voice loop.
        """
        normalized = (text or "").strip()
        if not normalized:
            return None

        utterance = UserUtterance(
            text=normalized,
            source=source,
            metadata=dict(metadata or {}),
        )

        matched_waiter = False
        should_close_window = False
        close_reason = "user_utterance"
        active_window_id: Optional[str] = None

        async with self._state_lock:
            self._utterance_history.append(utterance)

            for waiter_id, (future, matcher) in list(self._utterance_waiters.items()):
                if future.done():
                    self._utterance_waiters.pop(waiter_id, None)
                    continue

                try:
                    is_match = matcher is None or matcher(utterance)
                except Exception as exc:
                    logger.debug("Utterance matcher failed for waiter %s: %s", waiter_id, exc)
                    is_match = False

                if is_match:
                    future.set_result(utterance)
                    self._utterance_waiters.pop(waiter_id, None)
                    matched_waiter = True
                    break

            if not matched_waiter:
                self._pending_utterances.append(utterance)

            if self._listening_window:
                active_window_id = self._listening_window.get("window_id")
                self._listening_window["last_utterance_at"] = utterance.timestamp.isoformat()
                self._listening_window["last_utterance_text"] = utterance.text
                if self._listening_window.get("close_on_utterance", True):
                    should_close_window = True

        if should_close_window:
            await self.close_listening_window(
                reason=close_reason,
                metadata={"utterance_id": utterance.utterance_id},
                window_id=active_window_id,
            )

        return utterance

    async def wait_for_user_utterance(
        self,
        timeout: float = 20.0,
        matcher: Optional[Callable[[UserUtterance], bool]] = None,
    ) -> Optional[UserUtterance]:
        """Wait for a matching user utterance with timeout protection."""
        timeout = max(0.1, float(timeout))

        async with self._state_lock:
            for pending in list(self._pending_utterances):
                try:
                    is_match = matcher is None or matcher(pending)
                except Exception as exc:
                    logger.debug("Pending utterance matcher failed: %s", exc)
                    is_match = False
                if is_match:
                    self._pending_utterances.remove(pending)
                    return pending

            future: asyncio.Future = asyncio.get_running_loop().create_future()
            waiter_id = f"waiter_{int(time.time() * 1000)}_{id(future)}"
            self._utterance_waiters[waiter_id] = (future, matcher)

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            async with self._state_lock:
                self._utterance_waiters.pop(waiter_id, None)

    def _parse_yes_no_intent(self, transcript: str) -> Optional[bool]:
        """Parse yes/no intent from natural language."""
        text = (transcript or "").lower()
        if not text:
            return None

        approval_markers = (
            "yes",
            "yeah",
            "yep",
            "sure",
            "ok",
            "okay",
            "affirmative",
            "go ahead",
            "proceed",
            "do it",
            "approved",
        )
        denial_markers = (
            "no",
            "nope",
            "negative",
            "stop",
            "cancel",
            "deny",
            "denied",
            "don't",
            "do not",
            "hold on",
            "wait",
        )

        for marker in denial_markers:
            if marker in text:
                return False
        for marker in approval_markers:
            if marker in text:
                return True
        return None

    def _build_listening_window_context(
        self,
        context: Optional[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """Normalize speech context into listening window parameters."""
        if not context:
            return None

        open_flag = bool(context.get("open_listen_window", False))
        if not open_flag:
            return None

        timeout_seconds = float(context.get("listen_timeout_seconds", 20.0))
        delay_seconds = float(context.get("listen_delay_seconds", 0.15))
        reason = str(context.get("listen_reason", "post_speech_follow_up"))
        close_on_utterance = bool(context.get("listen_close_on_utterance", True))
        metadata = dict(context.get("listen_metadata", {}))
        metadata.update(
            {
                "close_on_utterance": close_on_utterance,
                "speech_context": {
                    k: v
                    for k, v in context.items()
                    if k not in {"listen_metadata"}
                },
            }
        )

        return {
            "reason": reason,
            "timeout_seconds": timeout_seconds,
            "delay_seconds": delay_seconds,
            "metadata": metadata,
        }

    async def _handle_post_speech_context(self, message: SpeechMessage) -> None:
        """Apply optional post-speech behavior from message context."""
        listen_context = self._build_listening_window_context(message.context)
        if not listen_context:
            return

        delay_seconds = max(0.0, float(listen_context.get("delay_seconds", 0.0)))
        if delay_seconds > 0:
            self._spawn_background_task(
                self._open_listening_window_after_delay(
                    delay_seconds=delay_seconds,
                    reason=str(listen_context["reason"]),
                    timeout_seconds=float(listen_context["timeout_seconds"]),
                    metadata=dict(listen_context["metadata"]),
                ),
                name=f"voice_open_window_delayed_{message.message_id}",
            )
            return

        await self.open_listening_window(
            reason=str(listen_context["reason"]),
            timeout_seconds=float(listen_context["timeout_seconds"]),
            metadata=dict(listen_context["metadata"]),
        )

    async def _open_listening_window_after_delay(
        self,
        delay_seconds: float,
        reason: str,
        timeout_seconds: float,
        metadata: Dict[str, Any],
    ) -> None:
        """Delay opening a listening window to avoid clipping speech tails."""
        try:
            await asyncio.sleep(delay_seconds)
            await self.open_listening_window(
                reason=reason,
                timeout_seconds=timeout_seconds,
                metadata=metadata,
            )
        except asyncio.CancelledError:
            pass

    async def _broadcast_listening_window_state(
        self,
        opened: bool,
        state: Dict[str, Any],
    ) -> None:
        """Broadcast listening window state to any connected WebSocket clients."""
        try:
            from api.unified_websocket import get_ws_manager_if_initialized
        except Exception as exc:
            logger.debug("WebSocket manager import unavailable for voice broadcast: %s", exc)
            return

        manager = get_ws_manager_if_initialized()
        if manager is None:
            return

        payload = {
            "type": "voice_listen_window_open" if opened else "voice_listen_window_closed",
            "window_id": state.get("window_id"),
            "reason": state.get("reason"),
            "timeout_seconds": state.get("timeout_seconds"),
            "opened_at": state.get("opened_at"),
            "closed_at": state.get("closed_at"),
            "close_reason": state.get("close_reason"),
            "metadata": state.get("metadata", {}),
            "source": "realtime_voice_communicator",
        }

        try:
            await manager.broadcast(payload)
        except Exception as exc:
            logger.debug("Failed to broadcast listening window state: %s", exc)

    def _fire_callbacks(self, event: str, data: Any = None) -> None:
        """Fire callbacks for an event."""
        if event not in self._callbacks:
            return

        dead_refs = set()
        for ref in self._callbacks[event]:
            callback = ref()
            if callback is None:
                dead_refs.add(ref)
            else:
                try:
                    callback(data)
                except Exception as e:
                    logger.error("Callback error for %s: %s", event, e)

        # Clean up dead references
        self._callbacks[event] -= dead_refs

    def on_event(self, event: str, callback: Callable[[Any], None]) -> None:
        """
        Register a callback for an event.

        Events:
        - speech_started: When speech begins
        - speech_finished: When speech completes
        - speech_interrupted: When speech is interrupted
        - queue_empty: When queue becomes empty

        Args:
            event: Event name
            callback: Callback function
        """
        if event not in self._callbacks:
            raise ValueError(f"Unknown event: {event}")

        self._callbacks[event].add(weakref.ref(callback))

    def set_voice(self, voice_name: str) -> bool:
        """
        Set the primary voice.

        Args:
            voice_name: Name of the voice to use

        Returns:
            True if voice was set, False if not available
        """
        if voice_name in self._available_voices:
            self._primary_voice = voice_name
            for config in self._mode_configs.values():
                config.voice = voice_name
            logger.info("Voice changed to: %s", voice_name)
            return True
        return False

    def configure_mode(
        self,
        mode: VoiceMode,
        rate: Optional[int] = None,
        pause_multiplier: Optional[float] = None
    ) -> None:
        """
        Configure a voice mode's parameters.

        Args:
            mode: Mode to configure
            rate: Speech rate (WPM)
            pause_multiplier: Pause duration multiplier
        """
        if mode not in self._mode_configs:
            return

        config = self._mode_configs[mode]
        if rate is not None:
            config.rate = rate
        if pause_multiplier is not None:
            config.pause_multiplier = pause_multiplier

    def get_status(self) -> Dict[str, Any]:
        """Get current communicator status."""
        listening_window_id = self._listening_window.get("window_id") if self._listening_window else None
        return {
            'running': self._running,
            'is_speaking': self._is_speaking,
            'is_listening_for_input': self._listening_window is not None,
            'listening_window_id': listening_window_id,
            'paused': self._paused,
            'muted': self._muted,
            'queue_size': self._speech_queue.qsize(),
            'primary_voice': self._primary_voice,
            'available_voices': len(self._available_voices),
            'speech_count_today': self._speech_count_today,
            'last_speech_time': self._last_speech_time.isoformat() if self._last_speech_time else None,
        }

    @property
    def is_speaking(self) -> bool:
        """
        Check if JARVIS is currently speaking.

        This property is used for SELF-VOICE SUPPRESSION - when JARVIS is speaking,
        audio input should be ignored to prevent feedback loops where JARVIS
        hears its own voice and tries to process it as a command.

        Returns:
            bool: True if currently speaking, False otherwise
        """
        return self._is_speaking

    @property
    def is_processing_speech(self) -> bool:
        """
        Check if speech is being processed (speaking or has pending queue items).

        More comprehensive check than is_speaking - includes queued items.

        Returns:
            bool: True if speaking or has queued speech
        """
        return self._is_speaking or not self._speech_queue.empty()

    @property
    def is_listening_for_input(self) -> bool:
        """Check if a backend listening window is currently open."""
        return self._listening_window is not None

    # ============== Convenience Methods for AGI OS ==============

    async def greet(self, name: str = "Derek") -> str:
        """Contextual greeting based on time of day."""
        hour = datetime.now().hour

        if 5 <= hour < 12:
            greeting = f"Good morning, {name}."
        elif 12 <= hour < 17:
            greeting = f"Good afternoon, {name}."
        elif 17 <= hour < 22:
            greeting = f"Good evening, {name}."
        else:
            greeting = f"Hello, {name}. Burning the midnight oil?"

        return await self.speak(greeting, mode=VoiceMode.CONVERSATIONAL)

    async def announce_detection(self, what: str, where: str = "") -> str:
        """Announce a detection to the user."""
        location = f" in {where}" if where else ""
        text = f"Sir, I've detected {what}{location}."
        return await self.speak(text, mode=VoiceMode.NOTIFICATION)

    async def request_approval(self, action: str, reason: str = "") -> str:
        """Request approval for an action."""
        reason_text = f" {reason}" if reason else ""
        text = f"Sir, may I proceed with {action}?{reason_text}"
        return await self.speak(text, mode=VoiceMode.APPROVAL, priority=VoicePriority.HIGH)

    async def confirm_action(self, action: str) -> str:
        """Confirm an action is being taken."""
        text = f"Understood. {action}."
        return await self.speak(text, mode=VoiceMode.NORMAL)

    async def report_completion(self, action: str, result: str = "successfully") -> str:
        """Report completion of an action."""
        text = f"Sir, I've completed {action} {result}."
        return await self.speak(text, mode=VoiceMode.NOTIFICATION)

    async def alert(self, message: str, critical: bool = False) -> str:
        """Alert the user to something important."""
        priority = VoicePriority.CRITICAL if critical else VoicePriority.URGENT
        mode = VoiceMode.URGENT
        return await self.speak(message, mode=mode, priority=priority, allow_repetition=True)

    async def thinking(self, about: str = "") -> str:
        """Express that JARVIS is thinking about something."""
        if about:
            text = f"Let me think about {about} for a moment."
        else:
            text = "Give me a moment to analyze this."
        return await self.speak(text, mode=VoiceMode.THOUGHTFUL)

    # ============== VBI (Voice Biometric Intelligence) Methods ==============

    async def get_owner_name(self) -> str:
        """
        Get the current owner's name dynamically via OwnerIdentityService.

        Falls back to macOS system user or "there" if unavailable.

        Returns:
            Owner's first name for personalized greetings
        """
        try:
            from agi_os.owner_identity_service import get_owner_name
            name = await asyncio.wait_for(get_owner_name(), timeout=1.0)
            return name if name else "there"
        except Exception as e:
            logger.debug(f"Failed to get owner name: {e}")
            # Fallback to macOS username
            try:
                import os
                username = os.environ.get('USER', 'there')
                return username.split('.')[0].title()
            except Exception:
                return "there"

    async def speak_immediate(
        self,
        text: str,
        mode: VoiceMode = VoiceMode.NORMAL,
        timeout: float = 15.0,
        interrupt: bool = False
    ) -> bool:
        """
        Speak immediately without queuing - for real-time VBI feedback.

        This bypasses the queue for time-critical VBI narration where
        immediate auditory feedback is essential.

        Uses AudioBus path when enabled (JARVIS_AUDIO_BUS_ENABLED=true) so
        AEC gets the reference signal for echo cancellation. Falls back to
        raw macOS `say` subprocess when AudioBus is not available.

        Uses a lock to prevent voice overlap - only one speech at a time.
        If interrupt=True, flushes AudioBus or kills any current speech.

        Args:
            text: Text to speak
            mode: Voice mode
            timeout: Maximum time to wait for speech
            interrupt: If True, kill current speech before starting

        Returns:
            True if speech completed, False if failed/timed out
        """
        if self._muted:
            return True

        _bus_enabled = os.getenv(
            "JARVIS_AUDIO_BUS_ENABLED", "false"
        ).lower() in ("true", "1", "yes")

        # If interrupt mode, stop any current speech via the correct path
        if interrupt:
            if _bus_enabled:
                # AudioBus path: flush playback (kills TTS stream)
                try:
                    from backend.audio.audio_bus import get_audio_bus_safe
                    bus = get_audio_bus_safe()
                    if bus is not None and bus.is_running:
                        bus.flush_playback()
                except Exception:
                    pass
            elif self._current_speech_process:
                # Legacy path: kill the say subprocess
                try:
                    self._current_speech_process.kill()
                    await asyncio.sleep(0.1)
                except Exception:
                    pass

        # Acquire lock to prevent overlapping speech
        async with self._immediate_speech_lock:
            speech_start_time = time.time()
            try:
                # =========================================================
                # 🔇 CRITICAL: Set is_speaking BEFORE speech starts
                # =========================================================
                self._is_speaking = True
                self._current_message = text

                # v8.0: Notify unified speech state manager BEFORE speech
                if UNIFIED_SPEECH_STATE_AVAILABLE:
                    try:
                        manager = get_speech_state_manager_sync()
                        await manager.start_speaking(
                            text,
                            source=SpeechSource.TTS_BACKEND
                        )
                    except Exception as e:
                        logger.debug(f"Unified speech state start error: {e}")

                logger.debug(f"🔊 [SPEAKING] Starting: {text[:50]}...")

                # =========================================================
                # AudioBus path — TTS singleton routes audio through the
                # bus so AEC gets the reference signal for echo cancellation.
                # =========================================================
                if _bus_enabled:
                    try:
                        from backend.voice.engines.unified_tts_engine import get_tts_engine
                        tts = await get_tts_engine()
                        await asyncio.wait_for(
                            tts.speak(text, play_audio=True, source="tts_backend"),
                            timeout=timeout,
                        )
                    except asyncio.TimeoutError:
                        raise  # Re-raise so outer handler catches it
                    except Exception as e:
                        logger.debug("AudioBus speak_immediate failed, falling back: %s", e)
                        # Fall through to legacy path
                        _bus_enabled = False

                if not _bus_enabled:
                    # Legacy: direct macOS say subprocess
                    config = self._mode_configs.get(mode, self._mode_configs[VoiceMode.NORMAL])
                    cmd = [
                        'say',
                        '-v', config.voice,
                        '-r', str(config.rate),
                        text
                    ]
                    process = await asyncio.create_subprocess_exec(
                        *cmd,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL
                    )
                    self._current_speech_process = process
                    await asyncio.wait_for(process.wait(), timeout=timeout)
                    self._current_speech_process = None

                logger.debug(f"🔊 [SPEAKING] Finished: {text[:50]}...")

                # =========================================================
                # POST-SPEECH BUFFER: Conditional on conversation mode.
                # =========================================================
                # In conversation mode, AEC handles echo suppression at the
                # signal level — no time-based cooldown needed. Without AEC
                # (legacy mode), the mic may still pick up reverb/echo, so
                # keep is_speaking=True for 300ms to reject trailing audio.
                # =========================================================
                _in_conversation_mode = False
                if UNIFIED_SPEECH_STATE_AVAILABLE:
                    try:
                        mgr = get_speech_state_manager_sync()
                        _in_conversation_mode = mgr.conversation_mode
                    except Exception:
                        pass

                if not _in_conversation_mode and not _bus_enabled:
                    await asyncio.sleep(
                        float(os.getenv("JARVIS_POST_SPEECH_BUFFER_S", "0.3"))
                    )

                speech_duration_ms = (time.time() - speech_start_time) * 1000
                self._is_speaking = False
                self._current_message = None

                # v8.0: Notify unified speech state manager AFTER speech
                if UNIFIED_SPEECH_STATE_AVAILABLE:
                    try:
                        manager = get_speech_state_manager_sync()
                        await manager.stop_speaking(actual_duration_ms=speech_duration_ms)
                    except Exception as e:
                        logger.debug(f"Unified speech state stop error: {e}")

                return True

            except asyncio.TimeoutError:
                logger.warning("Immediate speech timed out: %s", text[:50])
                # Clean up whichever path was active
                if _bus_enabled:
                    try:
                        from backend.audio.audio_bus import get_audio_bus_safe
                        bus = get_audio_bus_safe()
                        if bus is not None and bus.is_running:
                            bus.flush_playback()
                    except Exception:
                        pass
                else:
                    try:
                        if self._current_speech_process:
                            self._current_speech_process.kill()
                        self._current_speech_process = None
                    except Exception:
                        pass
                self._is_speaking = False
                self._current_message = None

                if UNIFIED_SPEECH_STATE_AVAILABLE:
                    try:
                        manager = get_speech_state_manager_sync()
                        speech_duration_ms = (time.time() - speech_start_time) * 1000
                        await manager.stop_speaking(actual_duration_ms=speech_duration_ms)
                    except Exception:
                        pass
                return False

            except Exception as e:
                logger.error("Immediate speech error: %s - %s", e, text[:50])
                self._current_speech_process = None
                self._is_speaking = False
                self._current_message = None

                if UNIFIED_SPEECH_STATE_AVAILABLE:
                    try:
                        manager = get_speech_state_manager_sync()
                        speech_duration_ms = (time.time() - speech_start_time) * 1000
                        await manager.stop_speaking(actual_duration_ms=speech_duration_ms)
                    except Exception:
                        pass
                return False

    async def vbi_stage_feedback(
        self,
        stage: str,
        confidence: float = 0.0,
        speaker_name: Optional[str] = None
    ) -> None:
        """
        Provide real-time voice feedback during VBI authentication stages.

        This method gives transparent, human-like narration during voice
        biometric verification, making the authentication feel conversational.

        Args:
            stage: Current VBI stage (init, audio_decode, ecapa_extract, verification,
                   unlock_execute, keychain, wake, typing, verify, complete, failed)
            confidence: Verification confidence score (0.0 to 1.0)
            speaker_name: Identified speaker name (auto-detected if None)
        """
        # Get dynamic speaker name if not provided
        if speaker_name is None:
            speaker_name = await self.get_owner_name()

        # Dynamic, human-like responses based on stage
        # IMPORTANT: Streamlined to avoid redundant/overlapping speech
        # Only speak at KEY stages to prevent "hallucinations"
        feedback_map = {
            # Initial stage - acknowledge command
            "init": f"Just a moment, {speaker_name}.",

            # 🧠 CONTEXT-AWARE: Screen already unlocked
            # This is spoken when user says "unlock" but screen is already open
            "already_unlocked": self._get_already_unlocked_feedback(speaker_name),
            "context_check": None,  # Silent - internal check

            # Silent stages - too fast or redundant
            "audio_decode": None,
            "ecapa_extract": None,  # Silent - very fast, no need to narrate
            "verification": None,  # Silent - avoid double-speaking before unlock

            # Unlock stage - ONLY speak here (combines verification + unlock)
            "unlock_execute": f"Voice verified, {speaker_name}. Unlocking now.",

            # Silent unlock substages
            "keychain": None,  # Silent - security
            "wake": None,  # Silent - too fast
            "typing": None,  # Silent - already said "unlocking"
            "verify": None,  # Silent - handled by complete

            # Completion - success message
            "complete": self._get_completion_feedback(confidence, speaker_name),

            # Failures - only speak on failure
            "failed": self._get_failure_feedback(confidence),
            "error": "I couldn't verify your voice. Please try again.",
        }

        text = feedback_map.get(stage)
        if text:
            # Use interrupt=True to kill any in-progress speech before new message
            logger.info(f"🔊 VBI Stage '{stage}' speaking: {text}")
            await self.speak_immediate(text, mode=VoiceMode.CONVERSATIONAL, interrupt=True)
        else:
            logger.debug(f"🔇 VBI Stage '{stage}' silent (no feedback configured)")

    def _get_verification_feedback(self, confidence: float, speaker_name: str) -> str:
        """Get dynamic feedback based on verification confidence."""
        if confidence >= 0.90:
            return f"Hello, {speaker_name}. Voice recognized."
        elif confidence >= 0.80:
            return f"I recognize you, {speaker_name}."
        elif confidence >= 0.60:
            return f"Verifying your voice, {speaker_name}."
        elif confidence >= 0.40:
            return "Processing voice verification."
        else:
            return "Analyzing voice pattern."

    def _get_already_unlocked_feedback(self, speaker_name: str) -> str:
        """
        Get context-aware feedback when screen is already unlocked.

        This provides intelligent, self-aware responses that make JARVIS
        feel conscious of the current system state.
        """
        import random
        from datetime import datetime

        current_hour = datetime.now().hour

        # Time-aware responses
        if 5 <= current_hour < 12:
            responses = [
                f"Your screen is already unlocked, {speaker_name}. Good morning!",
                f"Morning, {speaker_name}. I notice your screen is already open.",
                f"Already unlocked and ready, {speaker_name}.",
            ]
        elif 12 <= current_hour < 17:
            responses = [
                f"Your screen is already unlocked, {speaker_name}.",
                f"I see your screen is already open, {speaker_name}.",
                f"No unlock needed, {speaker_name}. You're good to go.",
            ]
        elif 17 <= current_hour < 21:
            responses = [
                f"Screen's already unlocked, {speaker_name}.",
                f"Already open, {speaker_name}. Working late?",
                f"Your screen is accessible, {speaker_name}.",
            ]
        else:
            responses = [
                f"Already unlocked, {speaker_name}. Late night session?",
                f"Screen's open, {speaker_name}. Burning the midnight oil?",
                f"Your screen is already accessible, {speaker_name}.",
            ]

        return random.choice(responses)

    def _get_completion_feedback(self, confidence: float, speaker_name: str) -> Optional[str]:
        """
        Get dynamic completion message based on confidence.

        Provides warm, personalized welcome-back message after successful unlock.
        """
        # Provide a warm welcome back message after unlock completes
        if confidence >= 0.90:
            return f"Welcome back, {speaker_name}."
        elif confidence >= 0.70:
            return f"There you go, {speaker_name}."
        elif confidence >= 0.50:
            return f"Unlocked for you, {speaker_name}."
        else:
            return f"Screen unlocked, {speaker_name}."

    def _get_failure_feedback(self, confidence: float) -> str:
        """Get helpful feedback on verification failure."""
        if confidence >= 0.30:
            return "Voice verification didn't quite match. Could you try again?"
        elif confidence >= 0.15:
            return "I'm having trouble verifying your voice. Please speak clearly and try again."
        else:
            return "Voice not recognized. Please ensure you're the registered user."

    async def vbi_lock_feedback(
        self,
        stage: str,
        speaker_name: Optional[str] = None
    ) -> None:
        """
        Provide voice feedback during screen lock.

        Args:
            stage: Lock stage (init, locking, complete, failed)
            speaker_name: User's name (auto-detected if None)
        """
        # Get dynamic speaker name if not provided
        if speaker_name is None:
            speaker_name = await self.get_owner_name()

        feedback_map = {
            "init": f"Locking the screen, {speaker_name}.",
            "locking": None,  # Silent - action in progress
            "complete": f"Screen locked. See you soon, {speaker_name}.",
            "failed": "I couldn't lock the screen. Please try Control Command Q.",
        }

        text = feedback_map.get(stage)
        if text:
            await self.speak_immediate(text, mode=VoiceMode.CONVERSATIONAL)


# ============== Singleton Pattern ==============

_voice_communicator: Optional[RealTimeVoiceCommunicator] = None


async def get_voice_communicator() -> RealTimeVoiceCommunicator:
    """
    Get the global voice communicator instance.

    Returns:
        The RealTimeVoiceCommunicator singleton
    """
    global _voice_communicator

    if _voice_communicator is None:
        _voice_communicator = RealTimeVoiceCommunicator()

    if not _voice_communicator._running:
        await _voice_communicator.start()

    return _voice_communicator


async def stop_voice_communicator() -> None:
    """Stop the global voice communicator."""
    global _voice_communicator

    if _voice_communicator is not None:
        await _voice_communicator.stop()
        _voice_communicator = None


if __name__ == "__main__":
    async def test():
        """Test the voice communicator."""
        voice = await get_voice_communicator()

        print("Testing RealTimeVoiceCommunicator...")

        # Test greeting
        await voice.greet()

        # Test detection
        await voice.announce_detection("an error", "line 42")

        # Test approval request
        await voice.request_approval("fixing this error", "It appears to be a syntax issue.")

        # Test completion
        await voice.report_completion("fixing the error")

        # Wait for queue to empty
        await asyncio.sleep(15)

        await stop_voice_communicator()
        print("Test complete!")

    asyncio.run(test())
