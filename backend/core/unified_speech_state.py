"""
Unified Speech State Manager v1.0
=================================

CRITICAL COMPONENT: Prevents JARVIS from hearing its own voice.

The Problem:
When JARVIS speaks, the microphone picks up the audio. Without proper
coordination, this creates a feedback loop where JARVIS:
1. Speaks a response
2. Microphone picks it up
3. Transcription treats it as user input
4. JARVIS tries to process its own speech as a command
5. Repeat → "hallucinations" and broken workflow

The Solution:
This module provides a SINGLE SOURCE OF TRUTH for speech state across:
- Backend TTS (realtime_voice_communicator, cai_voice_feedback_manager)
- Backend audio processing (whisper, hybrid STT)
- Frontend (via WebSocket broadcast)
- Voice Unlock (VBI self-voice check)

Architecture:
┌─────────────────────────────────────────────────────────────────────┐
│                    UnifiedSpeechStateManager                         │
│                         (Singleton)                                  │
├─────────────────────────────────────────────────────────────────────┤
│  State:                                                              │
│    - is_speaking: bool                                               │
│    - speech_started_at: Optional[float]                             │
│    - speech_ended_at: Optional[float]                               │
│    - last_spoken_text: str                                          │
│    - cooldown_until: float                                          │
├─────────────────────────────────────────────────────────────────────┤
│  Methods:                                                            │
│    - start_speaking(text, source) → Sets state, broadcasts          │
│    - stop_speaking() → Clears state, starts cooldown, broadcasts    │
│    - should_reject_audio() → Thread-safe check with reasons         │
│    - register_listener() → For WebSocket broadcast integration      │
├─────────────────────────────────────────────────────────────────────┤
│  Broadcasts (WebSocket):                                             │
│    - { type: "speech_state", is_speaking: true/false, ... }         │
│    - { type: "speech_cooldown", remaining_ms: int }                 │
└─────────────────────────────────────────────────────────────────────┘

Usage:
    from core.unified_speech_state import get_speech_state_manager
    
    manager = await get_speech_state_manager()
    
    # When JARVIS starts speaking
    await manager.start_speaking("Hello, how can I help?", source="tts")
    
    # When JARVIS stops speaking
    await manager.stop_speaking()
    
    # Before processing audio
    rejection = manager.should_reject_audio()
    if rejection.reject:
        logger.warning(f"Rejecting audio: {rejection.reason}")
        return
"""

import asyncio
import logging
import threading
import time
import weakref
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set
import difflib

logger = logging.getLogger(__name__)


# =============================================================================
# CONFIGURATION
# =============================================================================

class SpeechStateConfig:
    """Configuration for speech state management."""
    
    # Cooldown after speech ends (milliseconds)
    # This accounts for audio echo/reverb picked up after TTS completes
    DEFAULT_COOLDOWN_MS: int = 1500  # 1.5 seconds
    
    # Extended cooldown for longer speeches (based on text length)
    EXTENDED_COOLDOWN_PER_CHAR_MS: float = 5.0  # 5ms per character
    MAX_COOLDOWN_MS: int = 3000  # 3 seconds max
    
    # Similarity threshold for detecting echoed text
    # If transcription is >60% similar to recent speech, likely echo
    SIMILARITY_THRESHOLD: float = 0.6
    
    # How long to keep recent spoken texts in memory
    RECENT_TEXTS_RETENTION_MS: int = 10000  # 10 seconds
    MAX_RECENT_TEXTS: int = 20
    
    # Broadcast throttle (prevent spamming WebSocket)
    MIN_BROADCAST_INTERVAL_MS: int = 50


# =============================================================================
# DATA CLASSES
# =============================================================================

class SpeechSource(Enum):
    """Source of speech output."""
    TTS_BACKEND = "tts_backend"           # Backend realtime_voice_communicator
    TTS_FRONTEND = "tts_frontend"         # Frontend Web Speech API
    CAI_FEEDBACK = "cai_feedback"         # CAI voice feedback manager
    SUPERVISOR = "supervisor"             # Supervisor narrator
    VBI_FEEDBACK = "vbi_feedback"         # Voice biometric feedback
    SYSTEM = "system"                     # System announcements
    UNKNOWN = "unknown"


@dataclass
class SpokenText:
    """Record of a spoken text with metadata."""
    text: str
    text_lower: str = field(init=False)
    text_keywords: Set[str] = field(init=False)
    timestamp_ms: float = field(default_factory=lambda: time.time() * 1000)
    source: SpeechSource = SpeechSource.UNKNOWN
    duration_ms: float = 0.0
    
    def __post_init__(self):
        self.text_lower = self.text.lower().strip()
        # Extract keywords for fast similarity check
        self.text_keywords = set(
            word for word in self.text_lower.split()
            if len(word) > 2
        )


@dataclass
class AudioRejection:
    """Result of should_reject_audio check."""
    reject: bool
    reason: str
    details: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "reject": self.reject,
            "reason": self.reason,
            "details": self.details
        }


@dataclass
class SpeechState:
    """Current speech state."""
    is_speaking: bool = False
    speech_started_at: Optional[float] = None
    speech_ended_at: Optional[float] = None
    current_text: str = ""
    current_source: SpeechSource = SpeechSource.UNKNOWN
    cooldown_until: float = 0.0
    recent_texts: List[SpokenText] = field(default_factory=list)
    
    def is_in_cooldown(self) -> bool:
        """Check if currently in post-speech cooldown."""
        return time.time() * 1000 < self.cooldown_until
    
    def get_cooldown_remaining_ms(self) -> int:
        """Get remaining cooldown time in milliseconds."""
        remaining = self.cooldown_until - (time.time() * 1000)
        return max(0, int(remaining))
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "is_speaking": self.is_speaking,
            "speech_started_at": self.speech_started_at,
            "speech_ended_at": self.speech_ended_at,
            "current_text": self.current_text[:100] if self.current_text else "",
            "current_source": self.current_source.value,
            "cooldown_until": self.cooldown_until,
            "in_cooldown": self.is_in_cooldown(),
            "cooldown_remaining_ms": self.get_cooldown_remaining_ms(),
            "recent_texts_count": len(self.recent_texts),
        }


# =============================================================================
# UNIFIED SPEECH STATE MANAGER (SINGLETON)
# =============================================================================

class UnifiedSpeechStateManager:
    """
    Singleton manager for tracking JARVIS speech state across the system.
    
    Thread-safe and async-compatible. Provides:
    - Centralized speaking state tracking
    - Post-speech cooldown management
    - Audio rejection with detailed reasons
    - WebSocket broadcast for frontend sync
    - Similarity detection for echo rejection
    """
    
    _instance: Optional["UnifiedSpeechStateManager"] = None
    _lock: threading.Lock = threading.Lock()
    _async_lock: Optional[asyncio.Lock] = None
    
    def __new__(cls) -> "UnifiedSpeechStateManager":
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
            
        self._state = SpeechState()
        self._listeners: Set[weakref.ref] = set()
        self._websocket_broadcasters: List[Callable] = []
        self._last_broadcast_time: float = 0
        self._config = SpeechStateConfig()
        
        # Thread-safe state access
        self._state_lock = threading.RLock()
        
        # Stats
        self._stats = {
            "speeches_started": 0,
            "speeches_completed": 0,
            "audio_rejections": 0,
            "echo_detections": 0,
        }
        
        self._initialized = True
        logger.info("🔇 UnifiedSpeechStateManager initialized (self-voice suppression active)")
    
    @classmethod
    async def get_instance(cls) -> "UnifiedSpeechStateManager":
        """Get or create the singleton instance (async-safe)."""
        if cls._async_lock is None:
            cls._async_lock = asyncio.Lock()
        
        async with cls._async_lock:
            instance = cls()
            return instance
    
    # =========================================================================
    # SPEECH STATE MANAGEMENT
    # =========================================================================
    
    async def start_speaking(
        self,
        text: str,
        source: SpeechSource = SpeechSource.UNKNOWN,
        estimated_duration_ms: Optional[float] = None
    ) -> None:
        """
        Signal that JARVIS has started speaking.
        
        CRITICAL: Call this BEFORE TTS audio starts playing.
        
        Args:
            text: The text being spoken
            source: Where the speech originated
            estimated_duration_ms: Optional estimated speech duration
        """
        now_ms = time.time() * 1000
        
        with self._state_lock:
            self._state.is_speaking = True
            self._state.speech_started_at = now_ms
            self._state.speech_ended_at = None
            self._state.current_text = text
            self._state.current_source = source
            
            # Add to recent texts
            spoken = SpokenText(
                text=text,
                source=source,
                duration_ms=estimated_duration_ms or 0.0
            )
            self._state.recent_texts.append(spoken)
            
            # Trim old texts
            self._cleanup_recent_texts()
            
            self._stats["speeches_started"] += 1
        
        logger.info(
            f"🔇 [SPEECH START] source={source.value}, text='{text[:50]}...'"
        )
        
        # Broadcast to all listeners
        await self._broadcast_state_change("speech_started")
    
    async def stop_speaking(
        self,
        actual_duration_ms: Optional[float] = None
    ) -> None:
        """
        Signal that JARVIS has stopped speaking.
        
        CRITICAL: Call this AFTER TTS audio completes.
        Starts the post-speech cooldown to catch echo/reverb.
        
        Args:
            actual_duration_ms: Actual speech duration for adaptive cooldown
        """
        now_ms = time.time() * 1000
        
        with self._state_lock:
            self._state.is_speaking = False
            self._state.speech_ended_at = now_ms
            
            # Calculate adaptive cooldown
            cooldown_ms = self._calculate_cooldown(actual_duration_ms)
            self._state.cooldown_until = now_ms + cooldown_ms
            
            # Update duration in recent texts
            if self._state.recent_texts:
                last_text = self._state.recent_texts[-1]
                if actual_duration_ms:
                    last_text.duration_ms = actual_duration_ms
            
            self._stats["speeches_completed"] += 1
            
            current_text = self._state.current_text
            self._state.current_text = ""
            self._state.current_source = SpeechSource.UNKNOWN
        
        logger.info(
            f"🔇 [SPEECH END] cooldown={cooldown_ms}ms, text='{current_text[:50]}...'"
        )
        
        # Broadcast to all listeners
        await self._broadcast_state_change("speech_ended")
    
    def _calculate_cooldown(self, actual_duration_ms: Optional[float] = None) -> float:
        """Calculate adaptive cooldown based on speech characteristics."""
        base_cooldown = self._config.DEFAULT_COOLDOWN_MS
        
        # Extend cooldown based on text length
        text_len = len(self._state.current_text)
        text_extension = min(
            text_len * self._config.EXTENDED_COOLDOWN_PER_CHAR_MS,
            self._config.MAX_COOLDOWN_MS - base_cooldown
        )
        
        # If we know actual duration, use that for better estimate
        if actual_duration_ms:
            # Cooldown should be ~20% of speech duration
            duration_based = actual_duration_ms * 0.2
            return min(
                max(base_cooldown, duration_based),
                self._config.MAX_COOLDOWN_MS
            )
        
        return min(base_cooldown + text_extension, self._config.MAX_COOLDOWN_MS)
    
    def _cleanup_recent_texts(self) -> None:
        """Remove old texts from memory."""
        now_ms = time.time() * 1000
        cutoff = now_ms - self._config.RECENT_TEXTS_RETENTION_MS
        
        self._state.recent_texts = [
            t for t in self._state.recent_texts
            if t.timestamp_ms > cutoff
        ][-self._config.MAX_RECENT_TEXTS:]
    
    # =========================================================================
    # AUDIO REJECTION (Core Self-Voice Suppression)
    # =========================================================================
    
    def should_reject_audio(
        self,
        transcribed_text: Optional[str] = None
    ) -> AudioRejection:
        """
        Check if incoming audio should be rejected.
        
        Thread-safe. Call this BEFORE processing any audio input.
        
        Args:
            transcribed_text: Optional transcribed text for similarity check
            
        Returns:
            AudioRejection with reject=True/False and reason
        """
        with self._state_lock:
            now_ms = time.time() * 1000
            
            # Check 1: Currently speaking
            if self._state.is_speaking:
                self._stats["audio_rejections"] += 1
                return AudioRejection(
                    reject=True,
                    reason="jarvis_speaking",
                    details={
                        "speaking_for_ms": now_ms - (self._state.speech_started_at or now_ms),
                        "current_text": self._state.current_text[:50],
                    }
                )
            
            # Check 2: In cooldown period
            if self._state.is_in_cooldown():
                remaining_ms = self._state.get_cooldown_remaining_ms()
                self._stats["audio_rejections"] += 1
                return AudioRejection(
                    reject=True,
                    reason="cooldown_active",
                    details={
                        "remaining_ms": remaining_ms,
                        "total_cooldown_ms": self._state.cooldown_until - (self._state.speech_ended_at or 0),
                    }
                )
            
            # Check 3: Similarity to recent speech (if text provided)
            if transcribed_text:
                similarity_result = self._check_similarity(transcribed_text)
                if similarity_result.reject:
                    self._stats["echo_detections"] += 1
                    self._stats["audio_rejections"] += 1
                    return similarity_result
            
            # All checks passed - audio is OK to process
            return AudioRejection(
                reject=False,
                reason="ok",
                details={"recent_texts_count": len(self._state.recent_texts)}
            )
    
    def _check_similarity(self, transcribed_text: str) -> AudioRejection:
        """Check if transcribed text is similar to recent speech (echo detection)."""
        if not transcribed_text:
            return AudioRejection(reject=False, reason="ok")
        
        text_lower = transcribed_text.lower().strip()
        text_keywords = set(word for word in text_lower.split() if len(word) > 2)
        
        for spoken in self._state.recent_texts:
            # Quick keyword overlap check first
            if text_keywords and spoken.text_keywords:
                overlap = len(text_keywords & spoken.text_keywords)
                keyword_ratio = overlap / max(len(text_keywords), 1)
                
                if keyword_ratio > 0.5:  # >50% keyword overlap
                    # Do full similarity check
                    similarity = difflib.SequenceMatcher(
                        None, text_lower, spoken.text_lower
                    ).ratio()
                    
                    if similarity > self._config.SIMILARITY_THRESHOLD:
                        return AudioRejection(
                            reject=True,
                            reason="echo_detected",
                            details={
                                "similarity": round(similarity * 100, 1),
                                "transcribed": text_lower[:50],
                                "matched_speech": spoken.text_lower[:50],
                                "age_ms": time.time() * 1000 - spoken.timestamp_ms,
                            }
                        )
        
        return AudioRejection(reject=False, reason="ok")
    
    # =========================================================================
    # STATE ACCESS (Thread-Safe)
    # =========================================================================
    
    @property
    def is_speaking(self) -> bool:
        """Check if JARVIS is currently speaking."""
        with self._state_lock:
            return self._state.is_speaking
    
    @property
    def is_in_cooldown(self) -> bool:
        """Check if in post-speech cooldown."""
        with self._state_lock:
            return self._state.is_in_cooldown()
    
    @property
    def is_busy(self) -> bool:
        """Check if speaking OR in cooldown (should reject audio)."""
        with self._state_lock:
            return self._state.is_speaking or self._state.is_in_cooldown()
    
    def get_state(self) -> SpeechState:
        """Get full state (thread-safe copy)."""
        with self._state_lock:
            # Return a copy to prevent external modification
            state = SpeechState(
                is_speaking=self._state.is_speaking,
                speech_started_at=self._state.speech_started_at,
                speech_ended_at=self._state.speech_ended_at,
                current_text=self._state.current_text,
                current_source=self._state.current_source,
                cooldown_until=self._state.cooldown_until,
            )
            return state
    
    @property
    def last_spoken_text(self) -> str:
        """Get the last spoken text."""
        with self._state_lock:
            if self._state.recent_texts:
                return self._state.recent_texts[-1].text
            return ""
    
    @property
    def last_spoken_timestamp(self) -> float:
        """Get the timestamp of last spoken text (unix seconds)."""
        with self._state_lock:
            if self._state.recent_texts:
                return self._state.recent_texts[-1].timestamp_ms / 1000.0
            return 0.0
    
    def get_stats(self) -> Dict[str, Any]:
        """Get statistics."""
        with self._state_lock:
            return {
                **self._stats,
                "current_state": self._state.to_dict(),
            }
    
    # =========================================================================
    # WEBSOCKET BROADCAST INTEGRATION
    # =========================================================================
    
    def register_websocket_broadcaster(
        self,
        broadcaster: Callable[[Dict[str, Any]], Any]
    ) -> None:
        """
        Register a function to broadcast state changes via WebSocket.
        
        The broadcaster will be called with a dict containing:
        - type: "speech_state_change"
        - event: "speech_started" | "speech_ended"
        - state: current state dict
        
        Args:
            broadcaster: Async or sync callable that sends to WebSocket
        """
        self._websocket_broadcasters.append(broadcaster)
        logger.debug(f"Registered WebSocket broadcaster (total: {len(self._websocket_broadcasters)})")
    
    def unregister_websocket_broadcaster(
        self,
        broadcaster: Callable[[Dict[str, Any]], Any]
    ) -> None:
        """Unregister a WebSocket broadcaster."""
        if broadcaster in self._websocket_broadcasters:
            self._websocket_broadcasters.remove(broadcaster)
    
    async def _broadcast_state_change(self, event: str) -> None:
        """Broadcast state change to all registered listeners."""
        now = time.time() * 1000
        
        # Throttle broadcasts
        if now - self._last_broadcast_time < self._config.MIN_BROADCAST_INTERVAL_MS:
            return
        self._last_broadcast_time = now
        
        with self._state_lock:
            state_dict = self._state.to_dict()
        
        message = {
            "type": "speech_state_change",
            "event": event,
            "state": state_dict,
            "timestamp": now,
        }
        
        for broadcaster in self._websocket_broadcasters:
            try:
                result = broadcaster(message)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.debug(f"WebSocket broadcast error: {e}")


# =============================================================================
# SINGLETON ACCESS
# =============================================================================

_manager_instance: Optional[UnifiedSpeechStateManager] = None
_manager_lock = asyncio.Lock()
_broadcast_registered = False


async def get_speech_state_manager() -> UnifiedSpeechStateManager:
    """
    Get the global UnifiedSpeechStateManager instance.
    
    Usage:
        manager = await get_speech_state_manager()
        
        # When JARVIS starts speaking
        await manager.start_speaking("Hello!", source=SpeechSource.TTS_BACKEND)
        
        # When JARVIS stops speaking  
        await manager.stop_speaking()
        
        # Before processing audio
        rejection = manager.should_reject_audio(transcribed_text)
        if rejection.reject:
            return  # Don't process
    """
    global _manager_instance, _broadcast_registered
    
    if _manager_instance is None:
        async with _manager_lock:
            if _manager_instance is None:
                _manager_instance = UnifiedSpeechStateManager()
                
                # Register with broadcast manager for WebSocket updates
                if not _broadcast_registered:
                    try:
                        from api.broadcast_router import manager as broadcast_manager
                        
                        async def broadcast_speech_state(message: Dict[str, Any]) -> None:
                            """Broadcast speech state changes to all clients."""
                            await broadcast_manager.broadcast(message)
                        
                        _manager_instance.register_websocket_broadcaster(broadcast_speech_state)
                        _broadcast_registered = True
                        logger.info("🔇 Speech state manager registered with WebSocket broadcaster")
                    except ImportError:
                        logger.debug("Broadcast router not available - WebSocket updates disabled")
                    except Exception as e:
                        logger.debug(f"Failed to register broadcaster: {e}")
    
    return _manager_instance


def get_speech_state_manager_sync() -> UnifiedSpeechStateManager:
    """
    Synchronous version for non-async contexts.
    
    Note: Prefer the async version when possible.
    WebSocket broadcast registration only happens in async version.
    """
    global _manager_instance
    
    if _manager_instance is None:
        _manager_instance = UnifiedSpeechStateManager()
    
    return _manager_instance


# =============================================================================
# INTEGRATION HELPER: Register with existing WebSocket service
# =============================================================================

async def register_with_websocket_service() -> bool:
    """
    Register speech state broadcasts with the unified WebSocket service.
    
    Call this during app startup to enable frontend synchronization.
    
    Returns:
        True if registration successful
    """
    try:
        manager = await get_speech_state_manager()
        
        from api.broadcast_router import manager as broadcast_manager
        
        async def broadcast_speech_state(message: Dict[str, Any]) -> None:
            await broadcast_manager.broadcast(message)
        
        manager.register_websocket_broadcaster(broadcast_speech_state)
        logger.info("✅ Speech state registered with WebSocket broadcaster")
        return True
    except Exception as e:
        logger.warning(f"Failed to register speech state broadcaster: {e}")
        return False

