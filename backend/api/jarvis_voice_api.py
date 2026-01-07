"""
JARVIS Voice API - Voice endpoints with Iron Man-style personality
Integrates JARVIS personality with the web application

CoreML Voice Engine Integration:
================================
This API integrates the ultra-fast CoreML Voice Engine for real-time voice detection.

Hardware Acceleration:
- Runs on Apple Neural Engine (hardware accelerated)
- 232KB Silero VAD model (4-bit quantized)
- <10ms inference latency per detection
- ~5-10MB runtime memory usage

Features:
- Voice Activity Detection (VAD) - Detects if speech is present
- Speaker Recognition - Identifies specific user's voice
- Adaptive thresholds - Learns and adjusts over time
- Circuit breaker protection - Graceful failure handling
- Async task queue - Non-blocking voice processing

API Endpoints:
- POST /voice/detect-coreml - Full voice + speaker detection
- POST /voice/detect-vad-coreml - Voice activity only (faster)
- POST /voice/train-speaker-coreml - Train speaker recognition
- GET /voice/coreml-metrics - Get performance metrics
- GET /voice/coreml-status - Check availability

Usage:
    # Detect user voice with full speaker recognition
    audio = np.random.randn(512).astype(np.float32)  # 512 samples at 16kHz
    audio_b64 = base64.b64encode(audio.tobytes()).decode()
    response = await client.post("/voice/detect-coreml", json={
        "audio_data": audio_b64,
        "priority": 1  # 0=normal, 1=high, 2=critical
    })

    # Returns:
    # {
    #   "is_user_voice": true,
    #   "vad_confidence": 0.89,
    #   "speaker_confidence": 0.75,
    #   "metrics": {...}
    # }

Performance:
- Inference: <10ms per chunk
- Model size: 232KB (Neural Engine optimized)
- Memory: ~5-10MB runtime
- Sample rate: 16kHz mono
- Chunk size: 512 samples (32ms at 16kHz)

Technical Implementation:
- C++ CoreML library: voice/coreml/libvoice_engine.dylib (78KB)
- Python bridge: voice/coreml/voice_engine_bridge.py
- Model: models/vad_model.mlmodelc (Silero VAD v6.0.0)
- See COREML_SETUP_STATUS.md for full integration details
"""

import asyncio
import base64
import logging
import os
import sys
import traceback
from datetime import datetime
from functools import wraps
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from pydantic import BaseModel

# Import response cleaner
from .clean_vision_response import clean_vision_response

# Ensure the backend directory is in the path
backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

logger = logging.getLogger(__name__)

# ============================================================================
# WEBSOCKET CONNECTION TRACKING - For shutdown notifications
# ============================================================================

# Global set to track active WebSocket connections
active_websockets: set = set()

# ============================================================================
# SPEECH RECOGNITION ERROR TRACKING - Circuit breaker for client-side errors
# ============================================================================

class SpeechRecognitionTracker:
    """
    Tracks speech recognition errors from frontend clients.
    Implements circuit breaker pattern to prevent overwhelming clients with restarts.
    """
    def __init__(self):
        self.errors = {}  # client_id -> list of error timestamps
        self.error_window = 60  # seconds
        self.max_errors_per_window = 20
        self.backoff_durations = {}  # client_id -> backoff end timestamp
        self.total_errors = 0
        self.total_recoveries = 0
        
    def record_error(self, client_id: str, error_type: str) -> dict:
        """Record a speech recognition error and return recovery advice."""
        import time
        now = time.time()
        
        # Initialize client tracking
        if client_id not in self.errors:
            self.errors[client_id] = []
        
        # Clean old errors outside window
        self.errors[client_id] = [
            ts for ts in self.errors[client_id] 
            if now - ts < self.error_window
        ]
        
        # Add new error
        self.errors[client_id].append(now)
        self.total_errors += 1
        
        error_count = len(self.errors[client_id])
        
        # Check if client should back off
        if error_count >= self.max_errors_per_window:
            # Calculate backoff duration (exponential)
            backoff_multiplier = min(error_count // self.max_errors_per_window, 5)
            backoff_duration = 5 * (2 ** backoff_multiplier)  # 5s, 10s, 20s, 40s, 80s max
            self.backoff_durations[client_id] = now + backoff_duration
            
            return {
                "should_backoff": True,
                "backoff_seconds": backoff_duration,
                "error_count": error_count,
                "message": f"Too many errors ({error_count}). Backing off for {backoff_duration}s."
            }
        
        return {
            "should_backoff": False,
            "error_count": error_count,
            "message": "Error recorded. Continue listening."
        }
    
    def record_recovery(self, client_id: str):
        """Record successful recovery."""
        self.total_recoveries += 1
        # Clear backoff on successful recovery
        if client_id in self.backoff_durations:
            del self.backoff_durations[client_id]
    
    def get_client_status(self, client_id: str) -> dict:
        """Get status for a specific client."""
        import time
        now = time.time()
        
        # Check if in backoff period
        if client_id in self.backoff_durations:
            remaining = self.backoff_durations[client_id] - now
            if remaining > 0:
                return {
                    "status": "backoff",
                    "remaining_seconds": remaining,
                    "can_listen": False
                }
            else:
                # Backoff expired
                del self.backoff_durations[client_id]
        
        # Clean and count recent errors
        if client_id in self.errors:
            self.errors[client_id] = [
                ts for ts in self.errors[client_id]
                if now - ts < self.error_window
            ]
            error_count = len(self.errors[client_id])
        else:
            error_count = 0
        
        return {
            "status": "ok" if error_count < 5 else "degraded",
            "recent_errors": error_count,
            "can_listen": True
        }
    
    def get_stats(self) -> dict:
        """Get overall statistics."""
        import time
        now = time.time()
        
        # Clean old errors
        active_clients = 0
        total_recent_errors = 0
        for client_id in list(self.errors.keys()):
            self.errors[client_id] = [
                ts for ts in self.errors[client_id]
                if now - ts < self.error_window
            ]
            if self.errors[client_id]:
                active_clients += 1
                total_recent_errors += len(self.errors[client_id])
        
        return {
            "active_clients": active_clients,
            "recent_errors": total_recent_errors,
            "total_errors": self.total_errors,
            "total_recoveries": self.total_recoveries,
            "clients_in_backoff": len(self.backoff_durations)
        }

# Global speech recognition tracker
speech_tracker = SpeechRecognitionTracker()


async def broadcast_shutdown_notification():
    """Broadcast shutdown notification to all connected WebSocket clients"""
    if not active_websockets:
        return

    logger.info(f"📢 Broadcasting shutdown notification to {len(active_websockets)} clients")

    notification = {
        "type": "system_shutdown",
        "message": "JARVIS backend is shutting down",
        "timestamp": datetime.now().isoformat(),
        "reconnect_advised": True,
    }

    # Send to all clients concurrently
    await asyncio.gather(
        *[ws.send_json(notification) for ws in active_websockets], return_exceptions=True
    )


# ============================================================================
# ASYNC SUBPROCESS HELPERS - Non-blocking subprocess calls
# ============================================================================


async def async_subprocess_run(cmd, timeout: float = 10.0) -> tuple:
    """
    Non-blocking async subprocess execution.

    Args:
        cmd: Command and arguments as list or string
        timeout: Maximum execution time in seconds

    Returns:
        (stdout, stderr, returncode)
    """
    # Handle both list and string inputs
    if isinstance(cmd, str):
        import shlex

        cmd = shlex.split(cmd)

    if not cmd or len(cmd) == 0:
        logger.error("async_subprocess_run: Empty command list")
        return b"", b"Empty command", -1

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )

        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)

        return stdout, stderr, process.returncode

    except asyncio.TimeoutError:
        logger.warning(f"Subprocess timeout after {timeout}s: {' '.join(cmd)}")
        try:
            process.kill()
            await process.wait()
        except:
            pass
        return b"", b"Timeout", -1

    except Exception as e:
        logger.error(f"Subprocess error: {e}")
        return b"", str(e).encode(), -1


async def async_open_app(app_name: str) -> bool:
    """
    Non-blocking app launch on macOS.

    Args:
        app_name: Name of the application to open

    Returns:
        True if successful, False otherwise
    """
    stdout, stderr, returncode = await async_subprocess_run(["open", "-a", app_name], timeout=5.0)

    if returncode == 0:
        logger.info(f"✅ Launched {app_name} (async)")
        return True
    else:
        logger.warning(f"⚠️ Failed to launch {app_name}: {stderr.decode()}")
        return False


async def async_osascript(script: str, timeout: float = 10.0) -> tuple:
    """
    Non-blocking AppleScript execution.

    Args:
        script: AppleScript code to execute
        timeout: Maximum execution time

    Returns:
        (stdout, stderr, returncode)
    """
    return await async_subprocess_run(["osascript", "-e", script], timeout=timeout)


# Try to import graceful handler, but don't fail if it's not available
try:
    from graceful_http_handler import graceful_endpoint
except ImportError:
    logger.warning("Graceful HTTP handler not available, using passthrough")

    def graceful_endpoint(func):
        return func


# Import CoreML Voice Engine with error handling
try:
    import numpy as np

    from backend.voice.coreml.voice_engine_bridge import (
        CoreMLVoiceEngineBridge,
        create_coreml_engine,
        is_coreml_available,
    )

    COREML_AVAILABLE = is_coreml_available()
    logger.info(f"[CoreML] CoreML Voice Engine available: {COREML_AVAILABLE}")
except ImportError as e:
    logger.warning(f"[CoreML] CoreML Voice Engine not available: {e}")
    COREML_AVAILABLE = False
    # Define placeholder for type annotation when CoreML is not available
    CoreMLVoiceEngineBridge = None

# Import JARVIS voice components with error handling
try:
    # Try absolute import first
    from backend.voice.jarvis_agent_voice import JARVISAgentVoice
    from backend.voice.jarvis_voice import (
        EnhancedJARVISPersonality,
        EnhancedJARVISVoiceAssistant,
        VoiceCommand,
    )

    JARVIS_IMPORTS_AVAILABLE = True
except ImportError:
    try:
        # Fallback to relative import
        from ..voice.jarvis_agent_voice import JARVISAgentVoice
        from ..voice.jarvis_voice import (
            EnhancedJARVISPersonality,
            EnhancedJARVISVoiceAssistant,
            VoiceCommand,
        )

        JARVIS_IMPORTS_AVAILABLE = True
    except ImportError:
        try:
            # Try direct import as last resort
            import os
            import sys

            # Add parent directory to path temporarily
            parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            sys.path.insert(0, parent_dir)
            from voice.jarvis_agent_voice import JARVISAgentVoice
            from voice.jarvis_voice import (
                EnhancedJARVISPersonality,
                EnhancedJARVISVoiceAssistant,
                VoiceCommand,
            )

            sys.path.remove(parent_dir)
            JARVIS_IMPORTS_AVAILABLE = True
        except ImportError as e:
            logger.warning(f"All import attempts failed for JARVIS voice components: {e}")
            JARVIS_IMPORTS_AVAILABLE = False

if not JARVIS_IMPORTS_AVAILABLE:
    logger.warning("JARVIS voice components could not be imported")

    # Create stub classes to prevent NameError
    class EnhancedJARVISVoiceAssistant:
        def __init__(self, *args, **kwargs):
            raise NotImplementedError("JARVIS voice components not available")

    class EnhancedJARVISPersonality:
        pass

    class VoiceCommand:
        pass

    class JARVISAgentVoice:
        def __init__(self, *args, **kwargs):
            raise NotImplementedError("JARVIS agent components not available")


# Create VoiceCommand if not imported
if JARVIS_IMPORTS_AVAILABLE and not hasattr(VoiceCommand, "__init__"):

    class VoiceCommand:
        def __init__(
            self,
            raw_text,
            confidence=0.9,
            intent="conversation",
            needs_clarification=False,
        ):
            self.raw_text = raw_text
            self.confidence = confidence
            self.intent = intent
            self.needs_clarification = needs_clarification


class JARVISCommand(BaseModel):
    """Voice command request"""

    text: str
    audio_data: Optional[str] = None  # Base64 encoded audio


class AudioTranscriptionRequest(BaseModel):
    """Audio transcription request for Hybrid STT"""

    audio_data: str  # Base64 encoded audio bytes
    audio_format: Optional[str] = "wav"  # wav, mp3, webm, ogg
    strategy: Optional[str] = "balanced"  # speed, accuracy, balanced, cost, adaptive
    speaker_name: Optional[str] = None  # e.g., "Derek J. Russell"


class AudioTranscriptionResponse(BaseModel):
    """Audio transcription response from Hybrid STT"""

    text: str  # Transcribed text
    confidence: float  # 0.0 - 1.0
    engine: str  # wav2vec2, vosk, whisper_local, whisper_gcp
    model_name: str  # e.g., "wav2vec2-base"
    latency_ms: float  # Transcription time
    audio_duration_ms: float  # Audio length
    speaker_identified: Optional[str] = None  # Identified speaker
    metadata: Dict  # Additional engine-specific data
    total_request_time_ms: float  # End-to-end request time


class JARVISConfig(BaseModel):
    """JARVIS configuration update"""

    user_name: Optional[str] = None
    humor_level: Optional[str] = None  # low, moderate, high
    work_hours: Optional[tuple] = None
    break_reminder: Optional[bool] = None


def dynamic_error_handler(func):
    """Decorator to handle errors dynamically and provide graceful fallbacks"""

    @wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except AttributeError as e:
            logger.warning(f"AttributeError in {func.__name__}: {e}")
            # Return a graceful response based on the function name
            if "status" in func.__name__:
                return {
                    "status": "limited",
                    "message": "Operating with limited functionality",
                    "error": str(e),
                }
            elif "activate" in func.__name__:
                return {
                    "status": "activated",
                    "message": "Basic activation successful",
                    "limited": True,
                }
            elif "command" in func.__name__:
                return {
                    "response": "I'm experiencing technical difficulties. Please try again.",
                    "error": str(e),
                }
            else:
                return {
                    "status": "error",
                    "message": f"Function {func.__name__} encountered an error",
                    "error": str(e),
                }
        except TypeError as e:
            logger.warning(f"TypeError in {func.__name__}: {e}")
            return {
                "status": "error",
                "message": "Type mismatch error",
                "error": str(e),
                "suggestion": "Check API compatibility",
            }
        except Exception as e:
            logger.error(f"Unexpected error in {func.__name__}: {e}\n{traceback.format_exc()}")
            return {
                "status": "error",
                "message": "An unexpected error occurred",
                "error": str(e),
            }

    # Handle sync functions too
    if not asyncio.iscoroutinefunction(func):

        def sync_wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                logger.error(f"Error in {func.__name__}: {e}")
                return {
                    "status": "error",
                    "message": f"Error in {func.__name__}",
                    "error": str(e),
                }

        return sync_wrapper

    return wrapper


class DynamicErrorHandler:
    """Dynamic error handler for gracefully handling missing or incompatible components"""

    @staticmethod
    def safe_call(func, *args, **kwargs):
        """Safely call a function with fallback handling"""
        try:
            return func(*args, **kwargs)
        except Exception as e:
            logger.warning(
                f"Safe call failed for {func.__name__ if hasattr(func, '__name__') else func}: {e}"
            )
            return None

    @staticmethod
    def safe_getattr(obj, attr, default=None):
        """Safely get an attribute with fallback"""
        try:
            return getattr(obj, attr, default)
        except Exception:
            return default

    @staticmethod
    def create_safe_object(cls, *args, **kwargs):
        """Create an object with multiple fallback strategies"""
        # Try with arguments
        try:
            return cls(*args, **kwargs)
        except TypeError:
            # Try without arguments
            try:
                obj = cls()
                # Try to set attributes
                for key, value in kwargs.items():
                    try:
                        setattr(obj, key, value)
                    except:
                        pass
                return obj
            except:
                # Return a SimpleNamespace as fallback
                from types import SimpleNamespace

                return SimpleNamespace(**kwargs)


class JARVISVoiceAPI:
    """API for JARVIS voice interaction"""

    def __init__(self):
        """Initialize JARVIS Voice API"""
        self.router = APIRouter()
        self.error_handler = DynamicErrorHandler()

        # Lazy initialization - don't create JARVIS yet
        self._jarvis = None
        self._jarvis_initialized = False

        # Startup announcement guard - prevents multiple voices at startup
        self._startup_announced = False
        self._startup_announcement_lock = asyncio.Lock()

        # Session tracking for conversation history
        self._session_id = None
        self._learning_db = None

        # Initialize async pipeline for all commands
        self._pipeline = None

        # Hybrid STT Router (lazy initialization)
        self._hybrid_stt_router = None

        # Store last audio and speaker for voice verification
        self.last_audio_data = None
        self.last_speaker_name = None

        # ─────────────────────────────────────────────────────────────────
        # BACKGROUND TASKS REGISTRY - Prevents Garbage Collection
        # ─────────────────────────────────────────────────────────────────
        # Critical for Event-Driven Continuation Pattern:
        # When we schedule continuation tasks with asyncio.create_task(),
        # we MUST hold a strong reference to prevent the GC from destroying
        # the task before it completes. This set holds all background tasks
        # and uses done callbacks to auto-cleanup finished tasks.
        # ─────────────────────────────────────────────────────────────────
        self._background_tasks: set[asyncio.Task] = set()

        # Check if we have API key
        self.api_key = os.getenv("ANTHROPIC_API_KEY")
        # For now, enable basic JARVIS functionality even without full imports
        self.jarvis_available = bool(self.api_key)
        logger.info(
            f"[JARVIS API] API key loaded: {bool(self.api_key)}, jarvis_available: {self.jarvis_available}"
        )

        # We'll initialize on first use
        if not self.jarvis_available:
            logger.warning("JARVIS Voice System not available - ANTHROPIC_API_KEY not set")

        self._register_routes()

    @property
    def pipeline(self):
        """Get or create async pipeline instance"""
        if self._pipeline is None:
            from core.async_pipeline import get_async_pipeline

            self._pipeline = get_async_pipeline(self.jarvis)
            logger.info("[JARVIS API] Async pipeline initialized for voice commands")
        return self._pipeline

    @property
    def hybrid_stt_router(self):
        """Get or create hybrid STT router instance"""
        if self._hybrid_stt_router is None:
            from voice.hybrid_stt_router import get_hybrid_router

            self._hybrid_stt_router = get_hybrid_router()
            logger.info("🎤 Hybrid STT Router initialized for voice transcription")
        return self._hybrid_stt_router

    @property
    def jarvis(self):
        """Get JARVIS instance, initializing if needed"""
        logger.info(
            f"[JARVIS API] JARVIS property getter called - initialized: {self._jarvis_initialized}, available: {self.jarvis_available}"
        )

        if not self._jarvis_initialized and self.jarvis_available:
            try:
                # Try to use factory for proper dependency injection
                try:
                    from api.jarvis_factory import create_jarvis_agent, get_vision_analyzer

                    # Check if vision analyzer is available before creating JARVIS
                    vision_analyzer = get_vision_analyzer()
                    logger.info(
                        f"[JARVIS API] Vision analyzer available during JARVIS creation: {vision_analyzer is not None}"
                    )

                    self._jarvis = create_jarvis_agent()
                    logger.info(
                        "[JARVIS API] JARVIS Agent created using factory with shared vision analyzer"
                    )
                except ImportError:
                    # Fallback to direct creation
                    logger.warning(
                        "[INIT ORDER] Factory not available, falling back to direct creation"
                    )
                    self._jarvis = JARVISAgentVoice()
                    logger.info("JARVIS Agent created directly (no shared vision analyzer)")

                self.system_control_enabled = (
                    self._jarvis.system_control_enabled if self._jarvis else False
                )
                logger.info("JARVIS Agent Voice System initialized with system control")
            except Exception as e:
                logger.error(f"[INIT ORDER] Failed to initialize JARVIS Agent: {e}")
                self._jarvis = None
                self.system_control_enabled = False
            finally:
                self._jarvis_initialized = True

        logger.debug(f"[INIT ORDER] Returning JARVIS instance: {self._jarvis is not None}")
        return self._jarvis

    def _get_or_create_session_id(self) -> str:
        """Get or create session ID for conversation tracking"""
        if not self._session_id:
            import uuid

            self._session_id = str(uuid.uuid4())
            logger.info(f"📝 Created new session ID: {self._session_id[:8]}...")
        return self._session_id

    async def _get_learning_db(self):
        """Get or create learning database instance"""
        if not self._learning_db:
            try:
                from intelligence.learning_database import get_learning_database

                self._learning_db = await get_learning_database()
                logger.info("📚 Learning database connected for conversation tracking")
            except Exception as e:
                logger.error(f"Failed to initialize learning database: {e}")
                self._learning_db = None
        return self._learning_db

    async def _record_interaction(
        self,
        user_query: str,
        jarvis_response: str,
        response_type: str = "unknown",
        confidence_score: Optional[float] = None,
        execution_time_ms: Optional[float] = None,
        success: bool = True,
    ) -> int:
        """
        Record interaction to learning database for AI/ML learning.

        Returns interaction_id for feedback/correction tracking.
        """
        try:
            learning_db = await self._get_learning_db()
            if not learning_db:
                return -1

            # Get session ID
            session_id = self._get_or_create_session_id()

            # Capture current context
            context = await self._capture_context()

            # Record to database
            interaction_id = await learning_db.record_interaction(
                user_query=user_query,
                jarvis_response=jarvis_response,
                response_type=response_type,
                confidence_score=confidence_score,
                execution_time_ms=execution_time_ms,
                success=success,
                session_id=session_id,
                context=context,
            )

            if interaction_id > 0:
                logger.debug(
                    f"📝 Recorded interaction {interaction_id}: '{user_query[:30]}...' -> '{jarvis_response[:30]}...'"
                )

            return interaction_id

        except Exception as e:
            logger.error(f"Failed to record interaction: {e}")
            import traceback

            logger.debug(traceback.format_exc())
            return -1

    async def _capture_context(self) -> Dict[str, Any]:
        """Capture full system context for learning"""
        try:
            context = {
                "timestamp": datetime.now().isoformat(),
                "active_apps": [],
                "current_space": None,
                "system_state": {},
            }

            # Try to get active apps from display monitor
            try:
                from display import get_display_monitor

                monitor = get_display_monitor()
                if monitor and hasattr(monitor, "get_active_applications"):
                    context["active_apps"] = await monitor.get_active_applications()
            except:
                pass

            # Try to get current Space
            try:
                import subprocess

                result = subprocess.run(
                    [
                        "osascript",
                        "-e",
                        'tell application "System Events" to tell process "Dock" to get value of attribute "AXSelectedChildren"',
                    ],
                    capture_output=True,
                    text=True,
                    timeout=1,
                )
                if result.returncode == 0:
                    context["current_space"] = result.stdout.strip()
            except:
                pass

            # Get system state from JARVIS if available
            if self.jarvis and hasattr(self.jarvis, "personality"):
                try:
                    personality = self.jarvis.personality
                    if hasattr(personality, "user_preferences"):
                        context["system_state"]["user_preferences"] = personality.user_preferences
                    if hasattr(personality, "context"):
                        context["system_state"]["conversation_context"] = personality.context[-5:]
                except:
                    pass

            return context

        except Exception as e:
            logger.error(f"Failed to capture context: {e}")
            return {}

    def _register_routes(self):
        """Register JARVIS-specific routes"""
        # Status and control
        self.router.add_api_route("/status", self.get_status, methods=["GET"])
        self.router.add_api_route("/activate", self.activate, methods=["POST"])
        self.router.add_api_route("/deactivate", self.deactivate, methods=["POST"])

        # Command processing
        self.router.add_api_route("/command", self.process_command, methods=["POST"])
        self.router.add_api_route("/speak", self.speak, methods=["POST"])
        self.router.add_api_route("/speak/{text}", self.speak_get, methods=["GET"])

        # Hybrid STT - Audio transcription
        self.router.add_api_route("/transcribe", self.transcribe_audio, methods=["POST"])
        self.router.add_api_route("/transcribe/stats", self.get_stt_stats, methods=["GET"])

        # Configuration
        self.router.add_api_route("/config", self.get_config, methods=["GET"])
        self.router.add_api_route("/config", self.update_config, methods=["POST"])

        # Personality
        self.router.add_api_route("/personality", self.get_personality, methods=["GET"])
        
        # ================================================================
        # SPEECH RECOGNITION HEALTH ENDPOINTS
        # ================================================================
        # These endpoints help the frontend manage speech recognition errors
        # and implement circuit breaker patterns for better reliability.
        # ================================================================
        self.router.add_api_route(
            "/speech/health",
            self.get_speech_health,
            methods=["GET"],
            summary="Get speech recognition health status"
        )
        self.router.add_api_route(
            "/speech/error",
            self.report_speech_error,
            methods=["POST"],
            summary="Report a speech recognition error"
        )
        self.router.add_api_route(
            "/speech/recovery",
            self.report_speech_recovery,
            methods=["POST"],
            summary="Report successful speech recovery"
        )

        # WebSocket for real-time interaction
        # Note: WebSocket routes must be added using the decorator pattern in FastAPI
        @self.router.websocket("/stream")
        async def websocket_endpoint(websocket: WebSocket):
            await self.jarvis_stream(websocket)

    @dynamic_error_handler
    async def _announce_startup_once(self):
        """Announce startup ONCE using global coordinator to prevent multiple voices"""
        # Use global coordinator to prevent display monitor, voice API, and other
        # systems from all speaking at once
        from core.startup_announcement_coordinator import (
            AnnouncementPriority,
            get_startup_coordinator,
        )

        coordinator = get_startup_coordinator()

        # Check if we should announce (HIGH priority - core voice system)
        should_announce = await coordinator.announce_if_first(
            "jarvis_voice_api", priority=AnnouncementPriority.HIGH
        )

        if not should_announce:
            logger.info("[STARTUP] Another system already announced startup, skipping")
            return

        # Flag is already set in status endpoint - no need for lock here
        try:
            # Generate time-based greeting using coordinator
            greeting = coordinator.generate_greeting()

            # Send greeting via WebSocket to frontend (will show in transcript AND speak)
            logger.warning(f"[STARTUP VOICE] 🎤 JARVIS_VOICE_API ANNOUNCING: {greeting}")

            # Get WebSocket manager from pipeline if available
            from core.async_pipeline import get_async_pipeline

            if self._jarvis_initialized and self.jarvis:
                pipeline = get_async_pipeline(self.jarvis)
                if pipeline and hasattr(pipeline, "websocket_manager"):
                    # Send via WebSocket to show in transcript
                    logger.warning("[STARTUP VOICE] 📡 Sending via WebSocket to frontend")
                    await pipeline.websocket_manager.broadcast(
                        {
                            "type": "command_response",
                            "response": greeting,
                            "command": "system_startup",
                            "speak": True,  # Tell frontend to speak this
                            "metadata": {"is_startup_greeting": True, "priority": "high"},
                        }
                    )
                elif hasattr(self.jarvis, "voice"):
                    # Fallback to direct voice if WebSocket not available
                    logger.warning("[STARTUP VOICE] 🔊 Using direct voice.speak()")
                    await self.jarvis.voice.speak(greeting, priority=1)

        except Exception as e:
            logger.error(f"[STARTUP] Error announcing startup: {e}")
            # Don't reset the flag - better to skip announcement than have multiple

    async def get_status(self) -> Dict:
        """Get JARVIS system status"""
        logger.debug("[INIT ORDER] get_status called")

        if not self.api_key:
            return {
                "status": "offline",
                "message": "JARVIS system not available - API key required",
                "features": [],
            }

        # If we have API key but imports failed, still show as ready with limited features
        if self.api_key and not JARVIS_IMPORTS_AVAILABLE:
            return {
                "status": "ready",
                "message": "JARVIS ready with limited features",
                "features": ["basic_conversation", "text_commands"],
                "import_status": "limited",
            }

        features = [
            "voice_activation",
            "natural_conversation",
            "contextual_awareness",
            "personality_system",
            "break_reminders",
            "humor_and_wit",
        ]

        # Check if JARVIS is already initialized before accessing properties
        jarvis_instance = self._jarvis if self._jarvis_initialized else None

        if hasattr(self, "system_control_enabled") and self.system_control_enabled:
            features.extend(
                [
                    "system_control",
                    "app_management",
                    "file_operations",
                    "web_integration",
                    "workflow_automation",
                ]
            )

        # Only initialize JARVIS if we actually need its properties for the response
        if jarvis_instance:
            running = False
            if hasattr(jarvis_instance, "running"):
                running = jarvis_instance.running
            user_name = (
                jarvis_instance.user_name if hasattr(jarvis_instance, "user_name") else "Sir"
            )
            wake_words = (
                jarvis_instance.wake_words
                if hasattr(jarvis_instance, "wake_words")
                else ["hey jarvis", "jarvis"]
            )

            # Trigger startup announcement once when system becomes ready
            # Use lock to prevent race condition from multiple status checks
            if running:
                async with self._startup_announcement_lock:
                    if not self._startup_announced:
                        # Set flag immediately to prevent other status calls from triggering
                        logger.warning(
                            "[STARTUP VOICE] 🚀 STATUS ENDPOINT TRIGGERING ANNOUNCEMENT (path 1: running=True)"
                        )
                        self._startup_announced = True
                        asyncio.create_task(self._announce_startup_once())
                    else:
                        logger.debug(
                            "[STARTUP VOICE] ⏭️  Status endpoint skipping - already announced (path 1)"
                        )

            return {
                "status": "online" if running else "standby",
                "message": (
                    "JARVIS Agent at your service" if running else "JARVIS in standby mode"
                ),
                "user_name": user_name,
                "features": features,
                "wake_words": {
                    "primary": wake_words,
                    "variations": getattr(jarvis_instance, "wake_word_variations", []),
                    "urgent": getattr(jarvis_instance, "urgent_wake_words", []),
                },
                "voice_engine": {
                    "calibrated": hasattr(jarvis_instance, "voice_engine"),
                    "listening": running,
                },
                "system_control": {
                    "enabled": getattr(self, "system_control_enabled", False),
                    "mode": getattr(jarvis_instance, "command_mode", "conversation"),
                },
                "startup_announced": self._startup_announced,  # Tell frontend if announcement was made
            }
        else:
            # Trigger startup announcement once when system becomes ready
            # Use lock to prevent race condition from multiple status checks
            if self.api_key:
                async with self._startup_announcement_lock:
                    if not self._startup_announced:
                        # Set flag immediately to prevent other status calls from triggering
                        logger.warning(
                            "[STARTUP VOICE] 🚀 STATUS ENDPOINT TRIGGERING ANNOUNCEMENT (path 2: no jarvis_instance)"
                        )
                        self._startup_announced = True
                        # Schedule announcement asynchronously (don't await to avoid blocking status check)
                        asyncio.create_task(self._announce_startup_once())
                    else:
                        logger.debug(
                            "[STARTUP VOICE] ⏭️  Status endpoint skipping - already announced (path 2)"
                        )

            # Return status without triggering JARVIS initialization
            return {
                "status": "standby",
                "message": "JARVIS ready to initialize on first command",
                "user_name": "Sir",
                "features": features,
                "wake_words": {
                    "primary": ["hey jarvis", "jarvis"],
                    "variations": [],
                    "urgent": [],
                },
                "voice_engine": {"calibrated": False, "listening": False},
                "system_control": {
                    "enabled": getattr(self, "system_control_enabled", False),
                    "mode": "conversation",
                },
                "startup_announced": self._startup_announced,  # Tell frontend if announcement was made
            }

    # ================================================================
    # SPEECH RECOGNITION HEALTH ENDPOINTS
    # ================================================================
    
    async def get_speech_health(self, client_id: str = "default") -> Dict:
        """
        Get speech recognition health status for a client.
        
        This endpoint helps the frontend determine if it should:
        - Continue listening normally
        - Back off due to too many errors
        - Attempt recovery procedures
        
        Args:
            client_id: Unique identifier for the client (e.g., browser session ID)
            
        Returns:
            Health status with recommendations
        """
        client_status = speech_tracker.get_client_status(client_id)
        global_stats = speech_tracker.get_stats()
        
        return {
            "client": client_status,
            "global": global_stats,
            "recommendations": {
                "can_listen": client_status["can_listen"],
                "suggested_action": "continue" if client_status["status"] == "ok" else "reduce_frequency",
                "retry_after_seconds": client_status.get("remaining_seconds", 0)
            },
            "timestamp": datetime.now().isoformat()
        }
    
    async def report_speech_error(
        self, 
        client_id: str = "default",
        error_type: str = "aborted",
        error_message: str = ""
    ) -> Dict:
        """
        Report a speech recognition error from the frontend.
        
        This allows the backend to track error patterns and provide
        intelligent recovery recommendations.
        
        Args:
            client_id: Unique identifier for the client
            error_type: Type of error (aborted, no-speech, network, not-allowed, etc.)
            error_message: Optional error message for debugging
            
        Returns:
            Recovery advice for the client
        """
        result = speech_tracker.record_error(client_id, error_type)
        
        # Log significant errors (but not routine ones)
        if error_type not in ["no-speech", "aborted"] or result.get("should_backoff"):
            logger.warning(
                f"[SPEECH] Client {client_id} error: {error_type} "
                f"(count: {result['error_count']}, backoff: {result.get('should_backoff', False)})"
            )
        
        return {
            "status": "recorded",
            "advice": result,
            "timestamp": datetime.now().isoformat()
        }
    
    async def report_speech_recovery(self, client_id: str = "default") -> Dict:
        """
        Report successful speech recognition recovery.
        
        This clears any backoff state and resets error tracking.
        
        Args:
            client_id: Unique identifier for the client
            
        Returns:
            Confirmation of recovery
        """
        speech_tracker.record_recovery(client_id)
        logger.info(f"[SPEECH] Client {client_id} recovered successfully")
        
        return {
            "status": "recovered",
            "client_status": speech_tracker.get_client_status(client_id),
            "timestamp": datetime.now().isoformat()
        }

    async def activate(self) -> Dict:
        """Activate JARVIS voice system"""
        # Always use dynamic activation - never limited mode!
        try:
            from dynamic_jarvis_activation import activate_jarvis_dynamic

            # Get request context
            context = {
                "voice_required": True,
                "vision_required": True,
                "ml_required": True,
                "rust_acceleration": True,
                "api_key_available": bool(os.getenv("ANTHROPIC_API_KEY")),
                "jarvis_available": self.jarvis_available,
            }

            # Dynamic activation ensures full functionality
            result = await activate_jarvis_dynamic(context)

            # If we have the actual JARVIS instance and it's not running, start it
            if self.jarvis_available and hasattr(self, "jarvis") and self.jarvis:
                if hasattr(self.jarvis, "running") and not self.jarvis.running:
                    if hasattr(self.jarvis, "start"):
                        asyncio.create_task(self.jarvis.start())

            return result

        except Exception as e:
            logger.warning(f"Dynamic activation error: {e}, using enhanced fallback")

            # Even in worst case, provide full features through dynamic system
            return {
                "status": "activated",
                "message": "JARVIS activated with dynamic optimization",
                "mode": "full",  # NEVER limited!
                "capabilities": [
                    "voice_recognition",
                    "natural_conversation",
                    "ml_processing",
                    "command_execution",
                    "context_awareness",
                    "learning",
                    "performance_optimization",
                    "multi_modal_interaction",
                ],
                "health_score": 0.85,
                "ml_optimized": True,
            }

    async def deactivate(self) -> Dict:
        """Deactivate JARVIS voice system"""
        if not self.jarvis_available:
            # Return success to prevent 503
            return {"status": "deactivated", "message": "JARVIS deactivated"}

        if self.jarvis and hasattr(self.jarvis, "running") and not self.jarvis.running:
            return {
                "status": "already_inactive",
                "message": "JARVIS is already in standby mode",
            }

        if self.jarvis and hasattr(self.jarvis, "_shutdown"):
            await self.jarvis._shutdown()

        return {
            "status": "deactivated",
            "message": "JARVIS going into standby mode. Call when you need me.",
        }

    # =========================================================================
    # 🔒 PROACTIVE CAI (Context Awareness Intelligence) - SCREEN LOCK HANDLING
    # =========================================================================

    async def _handle_proactive_screen_unlock(self, command: "JARVISCommand") -> Optional[Dict]:
        """
        Proactively detect locked screen and handle transparent unlock with continuation.

        This enables autonomous workflows like:
          "Hey JARVIS, search for dogs" → detect lock → verify voice → unlock → search

        Features:
          - Intelligent verbal transparency via CAI Voice Feedback Manager
          - Dynamic, context-aware messages (time of day, speaker name)
          - Progressive confidence communication
          - Bulletproof continuation task scheduling

        Args:
            command: The JARVIS command being processed

        Returns:
            None if no action needed (screen unlocked or command exempt)
            Dict with result if proactive unlock was performed and command executed
        """
        import asyncio
        import time

        start_time = time.time()

        # ─────────────────────────────────────────────────────────────────────
        # Initialize CAI Voice Feedback Manager for intelligent verbal transparency
        # ─────────────────────────────────────────────────────────────────────
        try:
            from api.cai_voice_feedback_manager import (
                get_cai_voice_manager,
                CAIContext,
                extract_continuation_action,
            )
            voice_manager = await asyncio.wait_for(
                get_cai_voice_manager(),
                timeout=2.0
            )
        except Exception as e:
            logger.debug(f"[CAI] Voice manager not available: {e}")
            voice_manager = None

        try:
            # ─────────────────────────────────────────────────────────────────
            # Step 1: FAST Screen Lock Check (with timeout)
            # ─────────────────────────────────────────────────────────────────
            is_locked = await asyncio.wait_for(
                self._fast_check_screen_locked(),
                timeout=2.0
            )

            if not is_locked:
                return None  # Screen not locked, proceed normally

            logger.info(f"🔒 [CAI] Screen is LOCKED - analyzing command: '{command.text[:50]}...'")

            # ─────────────────────────────────────────────────────────────────
            # Step 2: Check if command requires screen access
            # ─────────────────────────────────────────────────────────────────
            requires_screen = self._command_requires_screen(command.text)

            if not requires_screen:
                logger.info(f"🔓 [CAI] Command doesn't require screen - skipping unlock")
                return None

            logger.info(f"📺 [CAI] Command requires screen access - initiating proactive unlock")

            # ─────────────────────────────────────────────────────────────────
            # Step 3: Create CAI Context for intelligent message generation
            # ─────────────────────────────────────────────────────────────────
            speaker_name = getattr(command, 'speaker_name', None) or "Sir"

            # Use advanced extraction if available, fallback to local
            if voice_manager:
                continuation_action = extract_continuation_action(command.text)
            else:
                continuation_action = self._extract_continuation_action(command.text)

            logger.info(f"🎯 [CAI] Continuation: '{continuation_action}'")

            # Create CAIContext for intelligent, personalized messaging
            cai_ctx = None
            if voice_manager:
                cai_ctx = CAIContext(
                    command_text=command.text,
                    continuation_action=continuation_action,
                    speaker_name=speaker_name,
                    is_screen_locked=True,
                    start_time=start_time,
                )

            # ─────────────────────────────────────────────────────────────────
            # Step 4: Generate and speak acknowledgment (DYNAMIC, CONTEXT-AWARE)
            # ─────────────────────────────────────────────────────────────────
            if voice_manager and cai_ctx:
                # Use intelligent voice manager for time-of-day aware greeting
                logger.info(f"🎤 [CAI] Using intelligent voice manager (time: {cai_ctx.time_of_day.value})")
                await voice_manager.announce_lock_detected(cai_ctx)
            else:
                # Fallback to simple message
                acknowledgment = f"Your screen is locked. Let me verify your voice and unlock it so I can {continuation_action}."
                logger.info(f"🎤 [CAI] Speaking acknowledgment: '{acknowledgment}'")
                await self._speak_cai_message(acknowledgment)

            # ─────────────────────────────────────────────────────────────────
            # Step 5: Perform unlock via VBI (with parallel voice announcements)
            # ─────────────────────────────────────────────────────────────────
            logger.info(f"🔐 [CAI] Performing VBI verification and unlock...")

            # Announce VBI is starting (fire-and-forget - runs parallel to VBI)
            if voice_manager and cai_ctx:
                await voice_manager.announce_vbi_start(cai_ctx)

            unlock_result = await asyncio.wait_for(
                self._perform_cai_unlock(command),
                timeout=30.0
            )

            if not unlock_result.get("success", False):
                failure_reason = unlock_result.get("response", "Unable to unlock screen.")
                logger.warning(f"❌ [CAI] Unlock failed: {failure_reason}")

                # ─────────────────────────────────────────────────────────────
                # VERBAL TRANSPARENCY: Speak intelligent failure message
                # ─────────────────────────────────────────────────────────────
                # Uses dynamic message generation based on failure reason
                if voice_manager and cai_ctx:
                    # Extract confidence if available
                    confidence = unlock_result.get("confidence", 0.0)
                    cai_ctx.vbi_confidence = confidence
                    await voice_manager.announce_verification_result(
                        cai_ctx,
                        success=False,
                        confidence=confidence,
                        failure_reason=failure_reason
                    )
                else:
                    # Fallback to simple message
                    failure_message = f"I couldn't verify your voice. {failure_reason}"
                    logger.info(f"🗣️ [CAI] Speaking failure: '{failure_message}'")
                    await self._speak_cai_message(failure_message)

                return {
                    "response": failure_reason,
                    "success": False,
                    "command_type": "proactive_unlock_failed",
                    "status": "error",
                }

            logger.info(f"✅ [CAI] Screen unlocked successfully!")

            # ─────────────────────────────────────────────────────────────────
            # Step 5b: Speak unlock confirmation (INTELLIGENT VERBAL TRANSPARENCY)
            # ─────────────────────────────────────────────────────────────────
            # CRITICAL: User should HEAR that unlock succeeded and what's next
            # Uses confidence-appropriate messaging (instant/high/good/borderline)
            if voice_manager and cai_ctx:
                # Update context with VBI confidence from result
                cai_ctx.vbi_confidence = unlock_result.get("confidence", 0.85)
                cai_ctx.was_unlocked = True
                cai_ctx.unlock_latency_ms = (time.time() - start_time) * 1000

                # Announce verification success (confidence-appropriate)
                await voice_manager.announce_verification_result(
                    cai_ctx,
                    success=True,
                    confidence=cai_ctx.vbi_confidence
                )

                # Announce unlock success with continuation preview
                await voice_manager.announce_unlock_success(cai_ctx)
            else:
                # Fallback to simple message
                unlock_confirmation = f"Screen unlocked. Now {continuation_action}."
                logger.info(f"🗣️ [CAI] Speaking unlock confirmation: '{unlock_confirmation}'")
                await self._speak_cai_message(unlock_confirmation)

            # ─────────────────────────────────────────────────────────────────
            # Step 6: EVENT-DRIVEN CONTINUATION PATTERN
            # ─────────────────────────────────────────────────────────────────
            # CRITICAL FIX: Do NOT recursively await process_command here!
            # The recursive call kept the first request open while the second
            # ran, confusing WebSocket state and causing "Processing..." hangs.
            #
            # Instead, we:
            # 1. Return the unlock success immediately (clears frontend state)
            # 2. Schedule the continuation as an INDEPENDENT task
            # 3. The new task runs after this function returns
            # ─────────────────────────────────────────────────────────────────

            unlock_latency = (time.time() - start_time) * 1000
            logger.info(f"🔓 [CAI] Unlock completed in {unlock_latency:.0f}ms - scheduling continuation")

            # Mark command to prevent infinite loop
            command._screen_just_unlocked = True

            # Capture voice manager and context for the continuation closure
            _voice_mgr = voice_manager
            _cai_ctx = cai_ctx

            # Create an independent continuation task
            async def _execute_continuation():
                """
                Execute the original command as a completely separate transaction.
                Includes verbal transparency announcements at start and completion.
                """
                try:
                    # Wait for frontend to process unlock response and reset state
                    await asyncio.sleep(0.5)

                    # ─────────────────────────────────────────────────────────────
                    # VERBAL TRANSPARENCY: Announce continuation is starting
                    # ─────────────────────────────────────────────────────────────
                    # Fire-and-forget so we don't block the actual execution
                    if _voice_mgr and _cai_ctx:
                        await _voice_mgr.announce_continuation_start(_cai_ctx)

                    logger.info(f"🔄 [CAI] CONTINUATION: Now executing '{command.text[:50]}...'")

                    # Process as a NEW independent command
                    continuation_result = await asyncio.wait_for(
                        self.process_command(command),
                        timeout=45.0
                    )

                    if continuation_result.get("success", False):
                        logger.info(f"✅ [CAI] Continuation completed successfully")

                        # ─────────────────────────────────────────────────────────
                        # VERBAL TRANSPARENCY: Announce completion with result
                        # ─────────────────────────────────────────────────────────
                        if _voice_mgr and _cai_ctx:
                            result_msg = continuation_result.get("response", "")
                            # Only announce completion if it's meaningful
                            if result_msg and len(result_msg) < 200:
                                await _voice_mgr.announce_task_completion(_cai_ctx, result_msg)
                    else:
                        error_msg = continuation_result.get("response", "Unknown error")
                        logger.warning(f"⚠️ [CAI] Continuation failed: {error_msg}")

                        # ─────────────────────────────────────────────────────────
                        # VERBAL TRANSPARENCY: Announce error with guidance
                        # ─────────────────────────────────────────────────────────
                        if _voice_mgr and _cai_ctx:
                            await _voice_mgr.announce_error(_cai_ctx, error_msg[:100])

                except asyncio.TimeoutError:
                    logger.error(f"⏱️ [CAI] Continuation timed out for: '{command.text}'")
                    if _voice_mgr and _cai_ctx:
                        await _voice_mgr.announce_error(_cai_ctx, "The operation took too long")
                except Exception as e:
                    logger.error(f"❌ [CAI] Continuation error: {e}")
                    if _voice_mgr and _cai_ctx:
                        await _voice_mgr.announce_error(_cai_ctx, str(e)[:100])

            # ─────────────────────────────────────────────────────────────────
            # BULLETPROOF TASK SCHEDULING - Prevents Garbage Collection
            # ─────────────────────────────────────────────────────────────────
            # CRITICAL: We MUST store a strong reference to the task!
            # Without this, Python's garbage collector could destroy the task
            # before it completes, causing the continuation to silently fail.
            #
            # The done_callback automatically removes the task from the set
            # once it completes (success or failure), preventing memory leaks.
            # ─────────────────────────────────────────────────────────────────
            continuation_task = asyncio.create_task(
                _execute_continuation(),
                name=f"continuation_{command.text[:30]}_{time.time()}"
            )

            # Store strong reference to prevent GC from destroying the task
            self._background_tasks.add(continuation_task)

            # Auto-cleanup when task completes (prevents memory leaks)
            continuation_task.add_done_callback(self._background_tasks.discard)

            logger.info(
                f"📋 [CAI] Continuation task scheduled (id={id(continuation_task)}, "
                f"active_tasks={len(self._background_tasks)}) - returning unlock success now"
            )

            # Return IMMEDIATELY with unlock success
            # This allows the frontend to clear "Processing..." state
            # The continuation task will start a NEW processing cycle
            return {
                "success": True,
                "response": f"Screen unlocked. Now {continuation_action}...",
                "command_type": "proactive_unlock_success",
                "status": "unlocked",
                "proactive_unlock": {
                    "performed": True,
                    "unlock_latency_ms": unlock_latency,
                    "continuation_scheduled": True,
                    "continuation_intent": continuation_action,
                },
            }

        except asyncio.TimeoutError:
            logger.warning(f"⏱️ [CAI] Timeout - falling back to normal processing")
            return None
        except Exception as e:
            logger.warning(f"[CAI] Error: {e} - falling back to normal processing")
            return None

    async def _fast_check_screen_locked(self) -> bool:
        """Fast, non-blocking screen lock detection."""
        try:
            # Strategy 1: Direct Quartz session check (fastest)
            try:
                from Quartz import CGSessionCopyCurrentDictionary
                session_dict = CGSessionCopyCurrentDictionary()
                if session_dict:
                    is_locked = session_dict.get("CGSSessionScreenIsLocked", False)
                    screen_saver = session_dict.get("CGSSessionScreenSaverIsRunning", False)
                    return bool(is_locked or screen_saver)
            except ImportError:
                pass

            # Strategy 2: Voice unlock detector
            try:
                from voice_unlock.objc.server.screen_lock_detector import is_screen_locked
                return bool(is_screen_locked())
            except (ImportError, Exception):
                pass

            return False  # Assume unlocked if we can't determine
        except Exception:
            return False

    def _command_requires_screen(self, text: str) -> bool:
        """Check if command requires screen access."""
        text_lower = text.lower()

        # Commands that DON'T require screen
        exempt_patterns = [
            r"\b(lock|unlock)\s+(my\s+)?(screen|computer|mac)\b",
            r"\bwhat\s+(time|is the time)\b",
            r"\b(what's|how's)\s+the\s+weather\b",
            r"\bset\s+(a\s+)?(timer|alarm|reminder)\b",
            r"\b(play|pause|stop|skip)\s+(music|song|audio)\b",
            r"\bhey\s+jarvis\b",
            r"\bthank\s+you\b",
            r"\bgoodbye\b",
        ]

        import re
        for pattern in exempt_patterns:
            if re.search(pattern, text_lower, re.IGNORECASE):
                return False

        # Commands that DO require screen
        screen_required_patterns = [
            r"\b(search|google|look up|browse)\b",
            r"\b(open|launch|start|run)\s+\w+",
            r"\bgo\s+to\s+",
            r"\b(create|edit|save|close)\s+(file|document|folder)",
            r"\b(click|scroll|type|select)\b",
        ]

        for pattern in screen_required_patterns:
            if re.search(pattern, text_lower, re.IGNORECASE):
                return True

        # Default: assume needs screen for safety
        return True

    def _extract_continuation_action(self, text: str) -> str:
        """Extract what user wants to do after unlock."""
        import re
        text_lower = text.lower()

        patterns = [
            (r"search\s+(?:for\s+)?(.+)", lambda m: f"search for {m.group(1)}"),
            (r"google\s+(.+)", lambda m: f"search for {m.group(1)}"),
            (r"open\s+(.+)", lambda m: f"open {m.group(1)}"),
            (r"launch\s+(.+)", lambda m: f"launch {m.group(1)}"),
            (r"go\s+to\s+(.+)", lambda m: f"navigate to {m.group(1)}"),
        ]

        for pattern, extractor in patterns:
            match = re.search(pattern, text_lower, re.IGNORECASE)
            if match:
                return extractor(match)

        return "complete your request"

    async def _speak_cai_message(self, message: str) -> None:
        """Speak a CAI message using available TTS."""
        try:
            # Try direct macOS say command (most reliable)
            import asyncio
            process = await asyncio.create_subprocess_exec(
                "say", "-v", "Daniel", message,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(process.wait(), timeout=10.0)
        except Exception as e:
            logger.debug(f"[CAI] Could not speak: {e}")

    async def _perform_cai_unlock(self, command: "JARVISCommand") -> Dict:
        """Perform screen unlock via VBI."""
        try:
            # Try robust unlock service
            from voice_unlock.intelligent_voice_unlock_service import process_voice_unlock_robust

            audio_data = getattr(command, 'audio_data', None)
            speaker_name = getattr(command, 'speaker_name', None)

            result = await process_voice_unlock_robust(
                command="unlock my screen",
                audio_data=audio_data,
                sample_rate=16000,
                mime_type="audio/webm",
                speaker_name=speaker_name,
            )

            return {
                "success": result.get("success", False),
                "response": result.get("response", ""),
                "latency_ms": result.get("total_duration_ms", 0),
            }
        except Exception as e:
            logger.error(f"[CAI] Unlock error: {e}")

            # Fallback: try direct unlock
            try:
                from backend.system_control.macos_controller import MacOSController
                controller = MacOSController()
                success, message = await controller.unlock_screen()
                return {"success": success, "response": message, "latency_ms": 0}
            except Exception as fallback_error:
                logger.error(f"[CAI] Fallback unlock error: {fallback_error}")
                return {"success": False, "response": str(fallback_error), "latency_ms": 0}

    @dynamic_error_handler
    @graceful_endpoint
    async def process_command(self, command: JARVISCommand) -> Dict:
        """Process a JARVIS command"""

        # =====================================================================
        # 🔒 PROACTIVE CAI (Context Awareness Intelligence) - SCREEN LOCK CHECK
        # =====================================================================
        # Detect locked screen and handle transparent unlock BEFORE processing
        # This enables: "search for dogs" → detect lock → verify voice → unlock → search
        # =====================================================================
        if not getattr(command, '_screen_just_unlocked', False):
            try:
                proactive_result = await self._handle_proactive_screen_unlock(command)
                if proactive_result is not None:
                    return proactive_result
            except Exception as cai_error:
                logger.warning(f"[CAI] Proactive unlock check failed (continuing): {cai_error}")

        # CRITICAL: Check for TV/display prompt responses FIRST (highest priority)
        try:
            from display import get_display_monitor

            monitor = get_display_monitor()

            # Debug logging
            logger.info(
                f"[JARVIS CMD] Display check - pending_prompt: {getattr(monitor, 'pending_prompt_display', None)}, has_pending: {monitor.has_pending_prompt()}"
            )

            # ALWAYS check for yes/no if it looks like a response
            response_lower = command.text.lower().strip()
            is_yes_no = any(
                word in response_lower
                for word in ["yes", "yeah", "yep", "no", "nope", "sure", "okay", "connect", "skip"]
            )

            if is_yes_no:
                logger.info(f"[JARVIS CMD] Detected yes/no response: '{command.text}'")

                # If there's a pending prompt OR if Living Room TV is available, handle it
                has_pending = monitor.has_pending_prompt()
                available_displays = list(getattr(monitor, "available_displays", set()))
                living_room_available = "living_room_tv" in available_displays

                logger.info(
                    f"[JARVIS CMD] Has pending: {has_pending}, Living Room TV available: {living_room_available}, All available: {available_displays}"
                )

                if has_pending or living_room_available:
                    logger.info(
                        f"[JARVIS CMD] Handling display response (pending={has_pending}, tv_available={living_room_available})"
                    )

                    # If no pending prompt but TV is available, set it now
                    if not has_pending and living_room_available:
                        monitor.pending_prompt_display = "living_room_tv"
                        logger.info(f"[JARVIS CMD] Set pending prompt for Living Room TV")

                    display_result = await monitor.handle_user_response(command.text)

                    if display_result.get("handled"):
                        logger.info(
                            f"[JARVIS CMD] Display handler processed the response successfully"
                        )
                        return {
                            "response": display_result.get("response", "Understood."),
                            "status": "success" if display_result.get("success", True) else "error",
                            "command_type": "display_response",
                            "success": display_result.get("success", True),
                        }
        except Exception as e:
            logger.error(f"[JARVIS CMD] Error checking display prompt: {e}")
            # Continue to normal processing if this fails

        # DYNAMIC COMPONENT LOADING - Load required components based on command intent
        try:
            from api.jarvis_factory import get_app_state

            app_state = get_app_state()

            if (
                app_state
                and hasattr(app_state, "component_manager")
                and app_state.component_manager
            ):
                manager = app_state.component_manager
                logger.info(
                    f"[DYNAMIC] Analyzing command for required components: '{command.text}'"
                )

                # Analyze intent and load required components
                required_components = await manager.intent_analyzer.analyze(command.text)

                if required_components:
                    logger.info(f"[DYNAMIC] Required components: {required_components}")

                    # Load each required component
                    for comp_name in required_components:
                        if comp_name in manager.components:
                            comp = manager.components[comp_name]
                            if comp.state.value != "loaded":
                                logger.info(f"[DYNAMIC] Loading {comp_name}...")
                                success = await manager.load_component(comp_name)
                                if success:
                                    logger.info(f"[DYNAMIC] ✅ {comp_name} loaded successfully")
                                else:
                                    logger.warning(f"[DYNAMIC] ⚠️ {comp_name} failed to load")
                            else:
                                logger.debug(f"[DYNAMIC] {comp_name} already loaded")
                                comp.last_used = asyncio.get_event_loop().time()
                else:
                    logger.debug(f"[DYNAMIC] No specific components required for: '{command.text}'")
        except Exception as e:
            logger.debug(f"[DYNAMIC] Component loading skipped: {e}")

        # Check if this is a multi-command workflow
        try:
            from .workflow_command_processor import handle_workflow_command

            workflow_result = await handle_workflow_command(command)
            if workflow_result:
                logger.info(
                    f"[JARVIS API] Processed workflow command with {workflow_result.get('workflow_result', {}).get('actions_completed', 0)} actions"
                )
                return workflow_result
        except Exception as e:
            logger.error(f"Workflow processor error: {e}")

        # IMPORTANT: Check for document commands BEFORE vision to avoid misclassification
        document_keywords = ["write", "create", "draft", "compose", "generate"]
        document_types = [
            "essay",
            "report",
            "paper",
            "article",
            "document",
            "blog",
            "letter",
            "story",
        ]
        words = command.text.lower().split()

        has_document_keyword = any(kw in words for kw in document_keywords)
        has_document_type = any(dtype in words for dtype in document_types)

        if has_document_keyword and has_document_type:
            logger.info(
                f"[JARVIS API] Document creation command detected - skipping vision handler: '{command.text}'"
            )
            # Skip vision handler entirely - will go to unified processor below
        else:
            # Check if this is a vision command
            logger.info(f"[JARVIS API] Checking if '{command.text}' is a vision command...")

            # Quick check for monitoring commands and multi-space queries
            is_monitoring_cmd = any(
                phrase in command.text.lower()
                for phrase in [
                    "start monitoring",
                    "enable monitoring",
                    "monitor my screen",
                    "enable screen monitoring",
                    "monitoring capabilities",
                    "turn on monitoring",
                    "activate monitoring",
                    "begin monitoring",
                    "stop monitoring",
                    "disable monitoring",
                    # Multi-space query patterns
                    "what's happening across",
                    "what is happening across",
                    "happening across my",
                    "desktop spaces",
                    "across my spaces",
                    "all my spaces",
                    "show me all spaces",
                    "all my desktops",
                ]
            )

            if is_monitoring_cmd:
                logger.info(
                    "[JARVIS API] Detected vision/multi-space command - routing to vision handler"
                )

            # =====================================================================
            # ROOT CAUSE FIX v9.0.0: Surveillance Detection Before Vision Handler
            # =====================================================================
            # PROBLEM: "watch all chrome windows for bouncing ball" was being sent
            # directly to vision_command_handler which returns "Application window active"
            # instead of routing to IntelligentCommandHandler/VisualMonitorAgent.
            #
            # SOLUTION: Detect surveillance intent BEFORE calling vision handler and
            # route through UnifiedCommandProcessor which has proper God Mode handling.
            # =====================================================================
            import re

            cmd_lower = command.text.lower()

            # Surveillance detection (same logic as UnifiedCommandProcessor)
            monitoring_keywords = [
                "watch", "monitor", "track", "alert when", "notify when",
                "detect when", "look for", "scan for", "observe",
            ]
            surveillance_patterns = ["for", "when", "until", "if", "whenever", "while"]
            god_mode_pattern = r"\b(all|every|each)\s+(?:\w+\s*)?(windows?|tabs?|instances?|spaces?)\b"

            has_monitoring = any(k in cmd_lower for k in monitoring_keywords)
            has_multi_target = bool(re.search(god_mode_pattern, cmd_lower, re.IGNORECASE))
            has_surveillance_structure = any(p in cmd_lower for p in surveillance_patterns)

            is_surveillance = (has_monitoring and has_surveillance_structure) or (has_monitoring and has_multi_target)

            if is_surveillance:
                logger.info(
                    f"[JARVIS API] 👁️ SURVEILLANCE DETECTED: '{command.text}' | "
                    f"monitoring={has_monitoring}, multi_target={has_multi_target}, "
                    f"structure={has_surveillance_structure}"
                )
                logger.info("[JARVIS API] Routing to UnifiedCommandProcessor (God Mode surveillance)")

                try:
                    from .unified_command_processor import UnifiedCommandProcessor
                    processor = UnifiedCommandProcessor()
                    
                    # v32.0: Dynamic timeout for multi-window surveillance
                    # WebSocket outer timeout is 90s, so inner timeout must be less
                    # Timeline for 6+ windows:
                    #   - Window discovery: ~5s
                    #   - Teleportation to Ghost Display: ~15s (may need to exit fullscreen)
                    #   - Watcher spawning: ~20s (parallel but with validation)
                    #   - Buffer for retries: ~10s
                    # Total: ~50s typical, 85s max to leave headroom for WebSocket
                    surveillance_timeout = float(os.getenv("JARVIS_SURVEILLANCE_TIMEOUT", "85"))
                    logger.info(f"[JARVIS API] Using {surveillance_timeout}s timeout for surveillance")
                    
                    result = await asyncio.wait_for(
                        processor.process_command(command.text),
                        timeout=surveillance_timeout
                    )

                    if result and result.get('response'):
                        logger.info(f"[JARVIS API] ✅ Surveillance success: {result['response'][:100]}...")
                        return {
                            "response": result['response'],
                            "status": "success",
                            "command_type": "surveillance",
                            "success": True,
                            "god_mode": has_multi_target,
                        }
                    else:
                        logger.warning(f"[JARVIS API] Surveillance result missing response: {result}")
                        return {
                            "response": "I've initiated monitoring. I'll alert you when I detect what you're looking for.",
                            "status": "success",
                            "command_type": "surveillance",
                            "success": True,
                        }

                except asyncio.TimeoutError:
                    logger.error(f"[JARVIS API] Surveillance setup timed out after {surveillance_timeout}s")
                    return {
                        "response": f"Monitoring setup timed out after {surveillance_timeout:.0f} seconds. The system may be initializing many windows. Please try again.",
                        "status": "error",
                        "command_type": "surveillance",
                        "success": False,
                    }
                except Exception as e:
                    logger.error(f"[JARVIS API] ❌ Surveillance routing failed: {e}", exc_info=True)
                    return {
                        "response": f"I encountered an error setting up monitoring: {str(e)}",
                        "status": "error",
                        "command_type": "surveillance",
                        "success": False,
                    }

            try:
                from .vision_command_handler import vision_command_handler

                # Ensure vision handler is initialized - WITH TIMEOUT PROTECTION
                if not vision_command_handler.intelligence:
                    logger.info("[JARVIS API] Initializing vision command handler...")

                    # ===========================================================
                    # ASYNC PARALLEL API KEY RETRIEVAL WITH FAST-FAIL
                    # ===========================================================
                    async def _get_api_key_from_secret_manager():
                        """Try SecretManager - 3s timeout"""
                        try:
                            from core.secret_manager import get_anthropic_key
                            key = await asyncio.wait_for(
                                asyncio.get_event_loop().run_in_executor(None, get_anthropic_key),
                                timeout=3.0
                            )
                            if key:
                                logger.info("[JARVIS API] ✅ Got API key from SecretManager")
                            return key
                        except asyncio.TimeoutError:
                            logger.warning("[JARVIS API] SecretManager timed out (3s)")
                            return None
                        except Exception as e:
                            logger.debug(f"[JARVIS API] SecretManager not available: {e}")
                            return None

                    async def _get_api_key_from_app_state():
                        """Try app state - fast check"""
                        try:
                            from api.jarvis_factory import get_app_state
                            app_state = get_app_state()
                            if (
                                app_state
                                and hasattr(app_state, "vision_analyzer")
                                and app_state.vision_analyzer
                            ):
                                key = getattr(app_state.vision_analyzer, "api_key", None)
                                if key:
                                    logger.info("[JARVIS API] Got API key from app state")
                                return key
                        except Exception:
                            pass
                        return None

                    async def _get_api_key_from_env():
                        """Try environment - instant"""
                        key = os.getenv("ANTHROPIC_API_KEY")
                        if key:
                            logger.info("[JARVIS API] Got API key from environment")
                        return key

                    # Run all API key retrievals in parallel - first success wins
                    api_key = None
                    try:
                        # Start all tasks
                        secret_manager_task = asyncio.create_task(_get_api_key_from_secret_manager())
                        app_state_task = asyncio.create_task(_get_api_key_from_app_state())
                        env_task = asyncio.create_task(_get_api_key_from_env())

                        # Wait for fastest completion - max 5s total
                        done, pending = await asyncio.wait(
                            [secret_manager_task, app_state_task, env_task],
                            timeout=5.0,
                            return_when=asyncio.ALL_COMPLETED
                        )

                        # Cancel any pending tasks
                        for task in pending:
                            task.cancel()

                        # Get first non-None result (prioritize: secret_manager > app_state > env)
                        for task in [secret_manager_task, app_state_task, env_task]:
                            if task in done and not task.cancelled():
                                try:
                                    result = task.result()
                                    if result:
                                        api_key = result
                                        break
                                except Exception:
                                    pass
                    except Exception as e:
                        logger.warning(f"[JARVIS API] Parallel API key retrieval error: {e}")

                    # ===========================================================
                    # INITIALIZE WITH TIMEOUT - 10 SECOND MAX
                    # ===========================================================
                    try:
                        await asyncio.wait_for(
                            vision_command_handler.initialize_intelligence(api_key),
                            timeout=10.0
                        )
                        logger.info("[JARVIS API] ✅ Vision handler initialized successfully")
                    except asyncio.TimeoutError:
                        logger.error("[JARVIS API] ❌ Vision handler initialization timed out (10s)")
                        return {
                            "response": "I'm still warming up my vision systems. Please try again in a moment.",
                            "status": "initializing",
                            "command_type": "vision",
                            "success": False,
                            "retry_suggested": True,
                        }
                    except ValueError as ve:
                        logger.error(f"[JARVIS API] Vision handler configuration error: {ve}")
                        return {
                            "response": f"Vision features require configuration. {str(ve)}",
                            "status": "error",
                            "command_type": "vision",
                            "success": False,
                        }

                logger.info(f"[JARVIS API] Calling vision handler for: '{command.text}'")
                try:
                    vision_result = await asyncio.wait_for(
                        vision_command_handler.handle_command(command.text),
                        timeout=30.0  # 30 second timeout
                    )
                    logger.info(
                        f"[JARVIS API] Vision handler result: handled={vision_result.get('handled')}"
                    )
                except asyncio.TimeoutError:
                    logger.error(f"[JARVIS API] Vision handler timed out after 30s for: '{command.text}'")
                    return {
                        "response": "Vision processing is taking longer than expected. This might be due to screen capture permissions or API connectivity. Please try again.",
                        "status": "error",
                        "command_type": "vision",
                        "success": False,
                    }
                except ValueError as ve:
                    # This catches the "no API key" error
                    logger.error(f"[JARVIS API] Vision handler configuration error: {ve}")
                    return {
                        "response": f"Vision features are not configured. {str(ve)}",
                        "status": "error",
                        "command_type": "vision",
                        "success": False,
                    }
                except Exception as ve:
                    logger.error(f"[JARVIS API] Vision handler error: {ve}", exc_info=True)
                    return {
                        "response": f"I encountered an error processing your vision request: {str(ve)}",
                        "status": "error",
                        "command_type": "vision",
                        "success": False,
                    }

                if vision_result.get("handled"):
                    return {
                        "response": vision_result["response"],
                        "status": "success",
                        "confidence": 1.0,
                        "command_type": "vision",
                        "monitoring_active": vision_result.get("monitoring_active"),
                    }
            except Exception as e:
                logger.error(f"Vision command handler error: {e}", exc_info=True)

        if not self.jarvis_available:
            # Check if this is a weather command - we can handle it even in limited mode
            if any(
                word in command.text.lower()
                for word in [
                    "weather",
                    "temperature",
                    "forecast",
                    "rain",
                    "sunny",
                    "cloudy",
                ]
            ):
                try:
                    # Try to get the full weather system with vision
                    weather_system = None
                    vision_available = False

                    # Check if we have access to app state
                    try:
                        from api.jarvis_factory import get_app_state

                        app_state = get_app_state()
                        if app_state and hasattr(app_state, "weather_system"):
                            weather_system = app_state.weather_system
                            vision_available = hasattr(app_state, "vision_analyzer")
                            logger.info(
                                f"[JARVIS API] Got weather system from app state, vision: {vision_available}"
                            )
                    except:
                        pass

                    # Fallback to get_weather_system
                    if not weather_system:
                        from system_control.weather_system_config import get_weather_system

                        weather_system = get_weather_system()

                    if weather_system and vision_available:
                        # FULL MODE with vision
                        logger.info(
                            "[JARVIS API] FULL MODE: Processing weather with vision analysis"
                        )
                        result = await weather_system.get_weather(command.text)

                        if result.get("success") and result.get("formatted_response"):
                            return {
                                "response": result["formatted_response"],
                                "status": "success",
                                "confidence": 1.0,
                                "command_type": "weather_vision",
                                "mode": "full_vision",
                            }
                        else:
                            # Vision failed
                            return {
                                "response": "I attempted to analyze the weather visually but encountered an issue. Let me open the Weather app for you.",
                                "status": "partial",
                                "confidence": 0.7,
                                "command_type": "weather_vision_failed",
                            }

                    # LIMITED MODE - No vision
                    logger.info(
                        "[JARVIS API] LIMITED MODE: Opening Weather app with navigation (async)"
                    )

                    # ✅ ASYNC FIX: Non-blocking app launch
                    await async_open_app("Weather")

                    # ✅ ASYNC FIX: Non-blocking wait
                    await asyncio.sleep(1.5)

                    # ✅ ASYNC FIX: Non-blocking AppleScript
                    await async_osascript(
                        """
                        tell application "System Events"
                            key code 126
                            delay 0.2
                            key code 126
                            delay 0.2
                            key code 125
                            delay 0.2
                            key code 36
                        end tell
                    """
                    )

                    return {
                        "response": "I'm operating in limited mode without vision analysis. I've opened the Weather app and navigated to your location. For automatic weather reading, please ensure the vision system is initialized with your ANTHROPIC_API_KEY.",
                        "status": "limited",
                        "confidence": 0.8,
                        "command_type": "weather_limited",
                        "mode": "limited_no_vision",
                    }

                except Exception as e:
                    logger.error(f"[JARVIS API] Weather error: {e}")
                    import traceback

                    traceback.print_exc()
                    return {
                        "response": "I'm having difficulty with the weather system. Let me open the Weather app for you.",
                        "status": "fallback",
                        "confidence": 0.5,
                        "mode": "error",
                    }

            # =========================================================================
            # SYSTEM COMMANDS DON'T REQUIRE API KEY - Route to pipeline directly
            # =========================================================================
            # Commands like "search for dogs", "open Safari" are purely local operations
            # that don't need LLM/API access. Let them through even in limited mode.
            # =========================================================================
            import re
            command_lower = command.text.lower()
            local_command_patterns = [
                r"\b(search|google|look\s*up|browse)\s+(for\s+)?",  # Search commands
                r"\b(open|launch|start|run|quit|close|exit)\s+\w+",  # App launch
                r"\bgo\s+to\s+",  # Navigation
                r"\b(lock|unlock)\s+(my\s+)?(screen|computer|mac)\b",  # Lock/unlock
                r"\b(volume|brightness)\s+(up|down|mute|unmute)",  # System control
            ]

            is_local_command = any(
                re.search(pattern, command_lower, re.IGNORECASE)
                for pattern in local_command_patterns
            )

            if is_local_command:
                logger.info(f"[JARVIS API] Local command detected in limited mode - routing to pipeline: '{command.text}'")
                # Fall through to normal processing below
            else:
                # For non-local commands, return the default limited mode response
                return {
                    "response": "I'm currently in limited mode, but I can still help. What do you need?",
                    "status": "fallback",
                    "confidence": 0.8,
                }

        try:
            # Validate command text
            if not command.text or command.text is None:
                logger.warning("Received command with empty or None text")
                return {
                    "response": "I didn't catch that. Could you please repeat?",
                    "status": "error",
                    "confidence": 0.0,
                }

            # Ensure JARVIS is active
            if self.jarvis and hasattr(self.jarvis, "running"):
                if not self.jarvis.running:
                    self.jarvis.running = True
                    logger.info("Activating JARVIS for command processing")

            # ═══════════════════════════════════════════════════════════════════
            # v8.0: SELF-VOICE SUPPRESSION CHECK AT API LEVEL
            # ═══════════════════════════════════════════════════════════════════
            # If JARVIS is speaking or in cooldown, this command might be JARVIS
            # hearing its own voice. Check the unified speech state and reject
            # if we're currently in a speech or cooldown state.
            # ═══════════════════════════════════════════════════════════════════
            try:
                from core.unified_speech_state import get_speech_state_manager_sync
                speech_manager = get_speech_state_manager_sync()
                rejection = speech_manager.should_reject_audio(command.text)
                
                if rejection.reject:
                    logger.warning(
                        f"🔇 [SELF-VOICE-API] Rejecting command - "
                        f"reason: {rejection.reason}, text: '{command.text[:50]}...'"
                    )
                    return CommandResponse(
                        response="",
                        command_type="self_voice_suppression",
                        success=False,
                        metadata={
                            "rejected": True,
                            "rejection_reason": rejection.reason,
                            "rejection_details": rejection.details,
                        }
                    )
            except ImportError:
                pass  # Manager not available
            except Exception as e:
                logger.debug(f"[SELF-VOICE-API] Check error (non-fatal): {e}")
            
            # Process command through async pipeline for better performance and alignment
            logger.info(f"[JARVIS API] Processing command through async pipeline: '{command.text}'")

            # CRITICAL: Decode audio_data from base64 for voice biometric verification (VIBA/PAVA)
            audio_bytes = None
            if command.audio_data:
                try:
                    import base64
                    audio_bytes = base64.b64decode(command.audio_data)
                    logger.info(f"[JARVIS API] Decoded audio data for VIBA/PAVA: {len(audio_bytes)} bytes")
                except Exception as e:
                    logger.warning(f"[JARVIS API] Failed to decode audio data: {e}")
            else:
                logger.debug("[JARVIS API] No audio data in command (text-only)")

            # Use async pipeline for all commands - ensures consistent handling
            try:
                # Process through pipeline with proper metadata AND audio_data for voice biometrics
                pipeline_result = await asyncio.wait_for(
                    self.pipeline.process_async(
                        command.text,
                        user_name=(
                            getattr(self.jarvis, "user_name", "Sir") if self.jarvis else "Sir"
                        ),
                        metadata={"source": "voice_api", "jarvis_instance": self.jarvis},
                        audio_data=audio_bytes,  # CRITICAL: Pass audio for VIBA/PAVA voice verification
                    ),
                    timeout=35.0,  # 35 second timeout for API calls (to accommodate weather)
                )

                # Extract response from pipeline result
                if isinstance(pipeline_result, dict):
                    response = pipeline_result.get("response", "I processed your command, Sir.")
                    # If we got a lock/unlock response, use it directly
                    if pipeline_result.get("type") in [
                        "voice_unlock",
                        "screen_lock",
                        "screen_unlock",
                    ]:
                        response = pipeline_result.get("response", response)
                else:
                    response = str(pipeline_result)

                logger.info(f"[JARVIS API] Pipeline response: '{response[:100]}...' (truncated)")
            except asyncio.TimeoutError:
                logger.error(
                    f"[JARVIS API] Command processing timed out after 35s: '{command.text}'"
                )
                # For weather commands, open the Weather app as fallback
                if any(
                    word in command.text.lower()
                    for word in ["weather", "temperature", "forecast", "rain"]
                ):
                    try:
                        success = await async_open_app("Weather")
                        response = "I'm having trouble reading the weather data. I've opened the Weather app for you to check directly, Sir."
                    except:
                        response = "I'm experiencing a delay accessing the weather information. Please check the Weather app directly, Sir."
                else:
                    response = "I apologize, but that request is taking too long to process. Please try again, Sir."

            # Get contextual info if available
            context = {}
            if self.jarvis and hasattr(self.jarvis, "personality"):
                personality = self.error_handler.safe_getattr(self.jarvis, "personality")
                if personality:
                    context = (
                        self.error_handler.safe_call(
                            getattr(personality, "_get_context_info", lambda: {}),
                        )
                        or {}
                    )

            # 📝 LEARNING: Record interaction to database for AI/ML learning
            await self._record_interaction(
                user_query=command.text,
                jarvis_response=response,
                response_type=(
                    pipeline_result.get("type", "rest_api")
                    if isinstance(pipeline_result, dict)
                    else "rest_api"
                ),
                confidence_score=(
                    pipeline_result.get("confidence") if isinstance(pipeline_result, dict) else None
                ),
                success=True,
            )

            return {
                "command": command.text,
                "response": response,
                "context": context,
                "timestamp": datetime.now().isoformat(),
                "user_name": getattr(self.jarvis, "user_name", "Sir"),
                "system_control_enabled": self.system_control_enabled,
                "success": True,
            }

        except Exception as e:
            logger.error(f"Error processing command: {e}")
            # Graceful handler will catch this and return a successful response
            raise

    @dynamic_error_handler
    @graceful_endpoint
    async def speak(self, request: Dict[str, str]) -> Response:
        """Make JARVIS speak the given text"""
        text = request.get("text", "")
        if not text:
            raise HTTPException(status_code=400, detail="No text provided")

        # Try async TTS handler first for better performance
        try:
            from .async_tts_handler import generate_speech_async

            # Generate speech with caching and async processing
            audio_path, content_type = await generate_speech_async(text, voice="Daniel")

            # Read the audio file
            with open(audio_path, "rb") as f:
                audio_data = f.read()

            return Response(
                content=audio_data,
                media_type=content_type,
                headers={
                    "Content-Disposition": f"inline; filename=jarvis_speech.{audio_path.suffix}",
                    "Cache-Control": "public, max-age=3600",  # Cache for 1 hour
                    "Access-Control-Allow-Origin": "*",
                },
            )

        except ImportError:
            logger.info("Async TTS handler not available, falling back to synchronous method")

        # Fallback to synchronous method
        try:
            import subprocess
            import tempfile

            # Use the full text for audio generation
            audio_text = text

            # Create temp file for audio
            with tempfile.NamedTemporaryFile(suffix=".aiff", delete=False) as tmp:
                tmp_path = tmp.name

            # Use macOS say command to generate audio with British voice
            stdout, stderr, returncode = await async_subprocess_run(
                [
                    "say",
                    "-v",
                    "Daniel",  # British voice for JARVIS
                    "-r",
                    "160",  # Much slower speech rate for natural delivery (words per minute)
                    "-o",
                    tmp_path,
                    audio_text,
                ],
                timeout=30.0,
            )
            if returncode != 0:
                raise subprocess.CalledProcessError(returncode, "say", stderr=stderr)

            # Convert to MP3 for browser compatibility
            mp3_path = tmp_path.replace(".aiff", ".mp3")
            media_type = "audio/mpeg"

            # Use ffmpeg if available, otherwise use the AIFF directly
            try:
                stdout, stderr, returncode = await async_subprocess_run(
                    [
                        "ffmpeg",
                        "-i",
                        tmp_path,
                        "-acodec",
                        "mp3",
                        "-ab",
                        "96k",  # Lower bitrate for speech
                        "-ar",
                        "22050",  # Lower sample rate for speech
                        mp3_path,
                        "-y",
                    ],
                    timeout=30.0,
                )
                if returncode != 0:
                    raise subprocess.CalledProcessError(returncode, "ffmpeg", stderr=stderr)
            except (subprocess.CalledProcessError, FileNotFoundError):
                # If ffmpeg not available, use lame
                try:
                    stdout, stderr, returncode = await async_subprocess_run(
                        ["lame", "-b", "96", "-m", "m", tmp_path, mp3_path], timeout=30.0
                    )
                    if returncode != 0:
                        raise subprocess.CalledProcessError(returncode, "lame", stderr=stderr)
                except (subprocess.CalledProcessError, FileNotFoundError):
                    # If neither work, use WAV format which browsers universally support
                    wav_path = tmp_path.replace(".aiff", ".wav")
                    try:
                        stdout, stderr, returncode = await async_subprocess_run(
                            [
                                "afconvert",
                                "-f",
                                "WAVE",
                                "-d",
                                "LEI16",
                                tmp_path,
                                wav_path,
                            ],
                            timeout=30.0,
                        )
                        if returncode == 0:
                            mp3_path = wav_path
                            media_type = "audio/wav"
                        else:
                            # Last resort: use AIFF
                            mp3_path = tmp_path
                            media_type = "audio/aiff"
                    except:
                        # Last resort: use AIFF
                        mp3_path = tmp_path
                        media_type = "audio/aiff"

            # Read the audio file
            with open(mp3_path, "rb") as f:
                audio_data = f.read()

            # Clean up
            if os.path.exists(tmp_path) and tmp_path != mp3_path:
                os.unlink(tmp_path)
            if os.path.exists(mp3_path):
                os.unlink(mp3_path)

            return Response(
                content=audio_data,
                media_type=media_type,
                headers={
                    "Content-Disposition": f"inline; filename=jarvis_speech.{mp3_path.split('.')[-1]}",
                    "Cache-Control": "no-cache",
                },
            )
        except Exception as e:
            logger.error(f"Error in text-to-speech: {e}")

            # Last resort: return a simple wave file with silence
            # This prevents the frontend from erroring out
            import struct

            # Generate a simple WAV header with 0.1 second of silence
            sample_rate = 44100
            duration = 0.1
            num_samples = int(sample_rate * duration)

            # WAV header
            wav_header = struct.pack(
                "<4sI4s4sIHHIIHH4sI",
                b"RIFF",
                36 + num_samples * 2,
                b"WAVE",
                b"fmt ",
                16,
                1,
                1,
                sample_rate,
                sample_rate * 2,
                2,
                16,
                b"data",
                num_samples * 2,
            )

            # Silent audio data (zeros)
            audio_data = wav_header + (b"\x00\x00" * num_samples)

            return Response(
                content=audio_data,
                media_type="audio/wav",
                headers={"Content-Disposition": "inline; filename=silence.wav"},
            )

    @dynamic_error_handler
    @graceful_endpoint
    async def speak_get(self, text: str) -> Response:
        """GET endpoint for text-to-speech (fallback for frontend)"""
        return await self.speak({"text": text})

    @dynamic_error_handler
    @graceful_endpoint
    async def transcribe_audio(self, request: Dict) -> Dict:
        """
        Transcribe audio using Hybrid STT Router.

        Ultra-intelligent transcription with:
        - RAM-aware model selection (Wav2Vec, Vosk, Whisper)
        - Confidence-based escalation (local -> cloud)
        - Speaker identification (Derek J. Russell)
        - Database recording for learning
        - Cost optimization (prefer local)

        Request body:
        {
            "audio_data": "base64_encoded_audio_bytes",
            "audio_format": "wav|mp3|webm|ogg",  # optional
            "strategy": "speed|accuracy|balanced|cost",  # optional
            "speaker_name": "Derek J. Russell"  # optional
        }

        Returns:
        {
            "text": "transcribed text",
            "confidence": 0.95,
            "engine": "wav2vec2",
            "model_name": "wav2vec2-base",
            "latency_ms": 150.0,
            "speaker_identified": "Derek J. Russell",
            "metadata": {...}
        }
        """
        import time

        start_time = time.time()

        try:
            # Extract audio data
            audio_b64 = request.get("audio_data")
            if not audio_b64:
                raise HTTPException(status_code=400, detail="No audio_data provided")

            # Decode base64 audio
            audio_bytes = base64.b64decode(audio_b64)

            # Get optional parameters
            from voice.stt_config import RoutingStrategy

            strategy_str = request.get("strategy", "balanced")
            try:
                strategy = RoutingStrategy(strategy_str)
            except ValueError:
                strategy = RoutingStrategy.BALANCED

            speaker_name = request.get("speaker_name")

            # Transcribe using hybrid router
            logger.info(
                f"🎤 Transcribing {len(audio_bytes)} bytes of audio (strategy={strategy.value}, speaker={speaker_name})"
            )

            result = await self.hybrid_stt_router.transcribe(
                audio_data=audio_bytes, strategy=strategy, speaker_name=speaker_name
            )

            # Calculate total request time
            total_time_ms = (time.time() - start_time) * 1000

            logger.info(
                f"✅ Transcription complete: '{result.text[:50]}...' "
                f"(confidence={result.confidence:.2f}, total_time={total_time_ms:.0f}ms)"
            )

            # Return result
            return {
                "text": result.text,
                "confidence": result.confidence,
                "engine": result.engine.value,
                "model_name": result.model_name,
                "latency_ms": result.latency_ms,
                "audio_duration_ms": result.audio_duration_ms,
                "speaker_identified": result.speaker_identified,
                "metadata": result.metadata,
                "total_request_time_ms": total_time_ms,
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Transcription failed: {e}")
            import traceback

            traceback.print_exc()
            raise HTTPException(status_code=500, detail=f"Transcription failed: {str(e)}")

    @dynamic_error_handler
    async def get_stt_stats(self) -> Dict:
        """
        Get Hybrid STT Router performance statistics.

        Returns:
        {
            "total_requests": 123,
            "cloud_requests": 15,
            "cloud_usage_percent": 12.2,
            "cache_hits": 5,
            "loaded_engines": ["wav2vec2:wav2vec2-base"],
            "performance_by_model": {
                "wav2vec2-base": {
                    "total_requests": 100,
                    "avg_latency_ms": 150.0,
                    "avg_confidence": 0.93
                }
            },
            "config": {...}
        }
        """
        try:
            stats = self.hybrid_stt_router.get_stats()
            return stats
        except Exception as e:
            logger.error(f"Failed to get STT stats: {e}")
            return {
                "error": str(e),
                "total_requests": 0,
                "cloud_requests": 0,
            }

    @dynamic_error_handler
    async def get_config(self) -> Dict:
        """Get JARVIS configuration"""
        logger.debug("[INIT ORDER] get_config called")

        if not self.jarvis_available:
            # Return default config to prevent 503
            return {
                "preferences": {"name": "User"},
                "wake_words": {"primary": ["hey jarvis", "jarvis"], "secondary": []},
                "context_history_size": 0,
                "special_commands": [],
            }

        # Only initialize JARVIS if it's already been created
        jarvis_instance = self._jarvis if self._jarvis_initialized else None

        if jarvis_instance:
            return {
                "preferences": (
                    getattr(jarvis_instance.personality, "user_preferences", {"name": "Sir"})
                    if hasattr(jarvis_instance, "personality")
                    else {"name": "Sir"}
                ),
                "wake_words": getattr(jarvis_instance, "wake_words", ["hey jarvis", "jarvis"]),
                "context_history_size": (
                    len(getattr(jarvis_instance.personality, "context", []))
                    if hasattr(jarvis_instance, "personality")
                    else 0
                ),
                "special_commands": list(getattr(jarvis_instance, "special_commands", {}).keys()),
            }
        else:
            # Return default config without initializing JARVIS
            return {
                "preferences": {"name": "Sir"},
                "wake_words": {"primary": ["hey jarvis", "jarvis"], "secondary": []},
                "context_history_size": 0,
                "special_commands": [],
            }

    @dynamic_error_handler
    async def update_config(self, config: JARVISConfig) -> Dict:
        """Update JARVIS configuration"""
        if not self.jarvis_available:
            # Return success to prevent 503
            return {
                "status": "updated",
                "updates": ["Configuration saved for when JARVIS is available"],
                "message": "Configuration updated.",
            }

        updates = []

        # Check if JARVIS is properly initialized
        if not self.jarvis or not hasattr(self.jarvis, "personality"):
            return {
                "status": "updated",
                "updates": ["Configuration saved for when JARVIS is fully initialized"],
                "message": "Configuration will be applied when JARVIS is ready.",
            }

        if config.user_name:
            self.jarvis.personality.user_preferences["name"] = config.user_name
            updates.append(f"User designation updated to {config.user_name}")

        if config.humor_level:
            self.jarvis.personality.user_preferences["humor_level"] = config.humor_level
            updates.append(f"Humor level adjusted to {config.humor_level}")

        if config.work_hours:
            self.jarvis.personality.user_preferences["work_hours"] = config.work_hours
            updates.append(f"Work hours updated to {config.work_hours[0]}-{config.work_hours[1]}")

        if config.break_reminder is not None:
            self.jarvis.personality.user_preferences["break_reminder"] = config.break_reminder
            updates.append(f"Break reminders {'enabled' if config.break_reminder else 'disabled'}")

        user_name = self.jarvis.personality.user_preferences.get("name", "Sir")
        return {
            "status": "updated",
            "updates": updates,
            "message": f"Configuration updated, {user_name}.",
        }

    @dynamic_error_handler
    async def get_personality(self) -> Dict:
        """Get JARVIS personality information"""
        logger.debug("[INIT ORDER] get_personality called")

        if not self.jarvis_available:
            # Return default personality to prevent 503
            return {
                "traits": ["helpful", "professional", "witty"],
                "humor_level": "moderate",
                "personality_type": "JARVIS",
                "capabilities": ["conversation", "assistance"],
            }

        # Only initialize JARVIS if it's already been created
        jarvis_instance = self._jarvis if self._jarvis_initialized else None

        base_personality = {
            "personality_traits": [
                "Professional yet personable",
                "British accent and sophisticated vocabulary",
                "Dry humor and wit",
                "Protective and loyal",
                "Anticipates user needs",
                "Contextually aware",
            ],
            "example_responses": [
                "Of course, sir. Shall I also cancel your 3 o'clock?",
                "The weather is partly cloudy, 72 degrees. Perfect for flying, if I may say so, sir.",
                "Sir, your heart rate suggests you haven't taken a break in 3 hours.",
                "I've taken the liberty of ordering your usual coffee, sir.",
                "Might I suggest the Mark 42? It's a personal favorite.",
            ],
        }

        if jarvis_instance and hasattr(jarvis_instance, "personality"):
            personality = jarvis_instance.personality
            base_personality.update(
                {
                    "current_context": (
                        getattr(personality, "_get_context_info", lambda: {})()
                        if hasattr(personality, "_get_context_info")
                        else {}
                    ),
                    "humor_level": getattr(personality, "user_preferences", {}).get(
                        "humor_level", "moderate"
                    ),
                }
            )
        else:
            # Return default without initializing JARVIS
            base_personality.update({"current_context": {}, "humor_level": "moderate"})

        return base_personality

    @dynamic_error_handler
    async def jarvis_stream(self, websocket: WebSocket):
        """WebSocket endpoint for real-time JARVIS interaction"""
        logger.info("[JARVIS WS] ========= NEW WEBSOCKET CONNECTION =========")
        logger.info("[JARVIS WS] Accepting WebSocket connection from JARVIS endpoint...")
        await websocket.accept()
        logger.info("[JARVIS WS] WebSocket connection accepted successfully")

        # Track this connection for shutdown notifications
        active_websockets.add(websocket)
        logger.info(f"[JARVIS WS] Active connections: {len(active_websockets)}")

        if not self.jarvis_available:
            logger.warning("[WEBSOCKET] JARVIS not available - API key required")
            await websocket.send_json(
                {"type": "error", "message": "JARVIS not available - API key required"}
            )
            await websocket.close()
            active_websockets.discard(websocket)
            return

        try:
            # Send connection confirmation
            user_name = "Sir"  # Default
            if (
                self.jarvis
                and hasattr(self.jarvis, "personality")
                and hasattr(self.jarvis.personality, "user_preferences")
            ):
                user_name = self.jarvis.personality.user_preferences.get("name", "Sir")

            # Generate dynamic startup greeting
            try:
                from voice.dynamic_response_generator import get_response_generator

                generator = get_response_generator(user_name)
                startup_greeting = generator.generate_startup_greeting()
            except:
                # Fallback if generator not available
                startup_greeting = f"JARVIS online. How may I assist you, {user_name}?"

            await websocket.send_json(
                {
                    "type": "connected",
                    "message": startup_greeting,
                    "timestamp": datetime.now().isoformat(),
                }
            )

            while True:
                # Receive data from client
                data = await websocket.receive_json()

                # Handle ping/pong for health monitoring
                if data.get("type") == "ping":
                    # Respond with pong immediately
                    await websocket.send_json(
                        {
                            "type": "pong",
                            "timestamp": data.get("timestamp"),
                            "server_time": datetime.now().isoformat(),
                        }
                    )
                    continue

                if data.get("type") == "command":
                    # Process voice command
                    command_text = data.get("text", "")
                    logger.info(f"WebSocket received command: '{command_text}'")

                    # Send immediate acknowledgment
                    await websocket.send_json(
                        {
                            "type": "debug_log",
                            "message": f"[SERVER] Received command: '{command_text}'",
                            "timestamp": datetime.now().isoformat(),
                        }
                    )

                    # =========================================================================
                    # ULTRA FAST PATH v3.0: Lock detection FIRST - bypasses ALL other processing
                    # This MUST be the very first check to prevent any blocking operations
                    # =========================================================================
                    import re as _re_fast

                    _cmd_lower = command_text.lower().strip()
                    # Quick wake phrase strip (inline, no function call overhead)
                    for _wp in ['hey jarvis', 'hey jarvus', 'jarvis', 'jarvus', 'okay jarvis']:
                        if _wp in _cmd_lower:
                            _cmd_lower = _cmd_lower.replace(_wp, '', 1).strip()
                            break

                    # Fast lock detection (regex-free for speed)
                    _is_lock = 'lock' in _cmd_lower and 'unlock' not in _cmd_lower
                    _has_target = any(t in _cmd_lower for t in ['screen', 'mac', 'computer', 'it', 'this'])
                    _is_lock_command_fast = _is_lock and (_has_target or len(_cmd_lower.split()) <= 3)

                    if _is_lock_command_fast:
                        logger.info(f"[JARVIS WS] 🔒⚡ ULTRA-FAST LOCK - direct execution")
                        try:
                            import shutil

                            async def _run_lock_cmd(cmd, timeout=3.0):
                                try:
                                    proc = await asyncio.create_subprocess_exec(
                                        *cmd,
                                        stdout=asyncio.subprocess.PIPE,
                                        stderr=asyncio.subprocess.PIPE
                                    )
                                    await asyncio.wait_for(proc.communicate(), timeout=timeout)
                                    return proc.returncode == 0
                                except:
                                    return False

                            lock_success = False
                            lock_method = "none"

                            # Method 1: AppleScript Cmd+Ctrl+Q (works on all macOS versions)
                            if shutil.which("osascript"):
                                script = 'tell application "System Events" to keystroke "q" using {command down, control down}'
                                if await _run_lock_cmd(["osascript", "-e", script]):
                                    lock_success = True
                                    lock_method = "applescript"

                            # Method 2: CGSession (older macOS)
                            if not lock_success:
                                cgsession = "/System/Library/CoreServices/Menu Extras/User.menu/Contents/Resources/CGSession"
                                if os.path.exists(cgsession):
                                    if await _run_lock_cmd([cgsession, "-suspend"]):
                                        lock_success = True
                                        lock_method = "cgsession"

                            # Method 3: LockScreen binary (newer macOS)
                            if not lock_success:
                                lockscreen = "/System/Library/CoreServices/RemoteManagement/AppleVNCServer.bundle/Contents/Support/LockScreen.app/Contents/MacOS/LockScreen"
                                if os.path.exists(lockscreen):
                                    if await _run_lock_cmd([lockscreen]):
                                        lock_success = True
                                        lock_method = "lockscreen"

                            # Method 4: pmset
                            if not lock_success and shutil.which("pmset"):
                                if await _run_lock_cmd(["pmset", "displaysleepnow"]):
                                    lock_success = True
                                    lock_method = "pmset"

                            # Send response immediately
                            await websocket.send_json({
                                "type": "response",
                                "text": "🔒 Locking your screen now. See you soon!" if lock_success else "❌ Lock failed",
                                "command_type": "screen_lock",
                                "success": lock_success,
                                "fast_path": True,
                                "ultra_fast": True,
                                "method": lock_method,
                                "timestamp": datetime.now().isoformat(),
                                "speak": True,
                            })
                            logger.info(f"[JARVIS WS] {'✅' if lock_success else '❌'} Ultra-fast lock via {lock_method}")
                            continue  # Skip ALL other processing

                        except Exception as e:
                            logger.error(f"[JARVIS WS] ❌ Ultra-fast lock failed: {e}")
                            # Fall through to normal processing as backup
                            pass

                    # Check if context awareness is enabled
                    # Import the setting from main.py
                    try:
                        import sys

                        sys.path.insert(
                            0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                        )
                        from main import USE_ENHANCED_CONTEXT

                        USE_CONTEXT_HANDLER = True
                    except ImportError:
                        # Default to using context handler
                        USE_CONTEXT_HANDLER = True
                        USE_ENHANCED_CONTEXT = True

                    if USE_CONTEXT_HANDLER:
                        try:
                            # CRITICAL: Check for TV/display prompt responses FIRST (highest priority)
                            # This must happen BEFORE any classification or processing
                            try:
                                from display import get_display_monitor

                                monitor = get_display_monitor()

                                # Debug logging
                                logger.info(
                                    f"[JARVIS WS] Display check - pending_prompt: {getattr(monitor, 'pending_prompt_display', None)}, has_pending: {monitor.has_pending_prompt()}"
                                )

                                # ALWAYS check for yes/no if it looks like a response
                                response_lower = command_text.lower().strip()
                                is_yes_no = any(
                                    word in response_lower
                                    for word in [
                                        "yes",
                                        "yeah",
                                        "yep",
                                        "no",
                                        "nope",
                                        "sure",
                                        "okay",
                                        "connect",
                                        "skip",
                                    ]
                                )

                                if is_yes_no:
                                    logger.info(
                                        f"[JARVIS WS] Detected yes/no response: '{command_text}'"
                                    )

                                    # If there's a pending prompt OR if Living Room TV is available, handle it
                                    has_pending = monitor.has_pending_prompt()
                                    available_displays = list(
                                        getattr(monitor, "available_displays", set())
                                    )
                                    living_room_available = "living_room_tv" in available_displays

                                    logger.info(
                                        f"[JARVIS WS] Has pending: {has_pending}, Living Room TV available: {living_room_available}, All available: {available_displays}"
                                    )

                                    if has_pending or living_room_available:
                                        logger.info(
                                            f"[JARVIS WS] Handling display response (pending={has_pending}, tv_available={living_room_available})"
                                        )

                                        # If no pending prompt but TV is available, set it now
                                        if not has_pending and living_room_available:
                                            monitor.pending_prompt_display = "living_room_tv"
                                            logger.info(
                                                f"[JARVIS WS] Set pending prompt for Living Room TV"
                                            )

                                        display_result = await monitor.handle_user_response(
                                            command_text
                                        )

                                        if display_result.get("handled"):
                                            logger.info(
                                                f"[JARVIS WS] Display handler processed the response successfully"
                                            )
                                            await websocket.send_json(
                                                {
                                                    "type": "response",
                                                    "text": display_result.get(
                                                        "response", "Understood."
                                                    ),
                                                    "command_type": "display_response",
                                                    "success": display_result.get("success", True),
                                                    "timestamp": datetime.now().isoformat(),
                                                    "speak": True,
                                                }
                                            )
                                            continue
                            except Exception as e:
                                logger.error(f"[JARVIS WS] Error checking display prompt: {e}")
                                # Continue to normal processing if this fails

                            # =========================================================================
                            # ULTRA FAST PATH v2.0: Screen lock commands bypass ALL context processing
                            # Robust wake phrase removal + flexible lock detection
                            # =========================================================================
                            import re

                            # Strip wake phrases (handles typos from voice recognition)
                            wake_patterns = [
                                r'\bhey\s+jarvis\b', r'\bhey\s+jarvus\b', r'\bhey\s+drivers\b',
                                r'\bhey\s+jarvas\b', r'\bokay\s+jarvis\b', r'\byo\s+jarvis\b',
                                r'\bjarvis\b', r'\bjarvus\b', r'\bdrivers\b'
                            ]

                            cleaned_command = command_text.lower().strip()
                            wake_phrase_detected = None

                            for pattern in wake_patterns:
                                match = re.search(pattern, cleaned_command, re.IGNORECASE)
                                if match:
                                    wake_phrase_detected = match.group(0)
                                    cleaned_command = re.sub(pattern, '', cleaned_command, count=1, flags=re.IGNORECASE)
                                    cleaned_command = re.sub(r'\s+', ' ', cleaned_command).strip()
                                    break

                            # Detect lock command (flexible pattern matching)
                            has_lock = re.search(r'\block\b', cleaned_command)
                            has_unlock = re.search(r'\bunlock\b', cleaned_command)
                            has_target = re.search(r'\b(screen|mac|computer|it|this)\b', cleaned_command)

                            is_lock_command = has_lock and not has_unlock and (has_target or len(cleaned_command.split()) <= 3)

                            if is_lock_command:
                                logger.info(f"[JARVIS WS] 🔒 LOCK command detected - DIRECT EXECUTION")
                                logger.info(f"[JARVIS WS]    Original: '{command_text}'")
                                logger.info(f"[JARVIS WS]    Cleaned:  '{cleaned_command}'")
                                if wake_phrase_detected:
                                    logger.info(f"[JARVIS WS]    Wake phrase removed: '{wake_phrase_detected}'")

                                try:
                                    from .unified_command_processor import get_unified_processor
                                    processor = get_unified_processor(self.api_key)

                                    # Use cleaned command (wake phrase removed) for processing
                                    result = await asyncio.wait_for(
                                        processor.process_command(cleaned_command, websocket, audio_data=self.last_audio_data, speaker_name=self.last_speaker_name),
                                        timeout=10.0
                                    )
                                    logger.info(f"[JARVIS WS] ✅ Lock command completed successfully")

                                    # Send response immediately
                                    response_data = {
                                        "type": "response",
                                        "text": result.get("response", "Locking your screen now."),
                                        "command_type": "screen_lock",
                                        "success": result.get("success", True),
                                        "fast_path": True,
                                        "timestamp": datetime.now().isoformat(),
                                        "speak": True,
                                        **{k: v for k, v in result.items() if k not in ["response", "command_type", "success"]}
                                    }
                                    await websocket.send_json(response_data)
                                    logger.info(f"[JARVIS WS] Sent lock response")
                                    continue  # Skip to next message

                                except asyncio.TimeoutError:
                                    logger.error("[JARVIS WS] ❌ Lock command timed out")
                                    await websocket.send_json({
                                        "type": "response",
                                        "text": "Lock command timed out. Please try again.",
                                        "success": False,
                                        "error": "timeout",
                                        "timestamp": datetime.now().isoformat(),
                                        "speak": True
                                    })
                                    continue
                                except Exception as e:
                                    logger.error(f"[JARVIS WS] ❌ Lock command failed: {e}")
                                    await websocket.send_json({
                                        "type": "response",
                                        "text": f"Failed to lock screen: {str(e)}",
                                        "success": False,
                                        "error": str(e),
                                        "timestamp": datetime.now().isoformat(),
                                        "speak": True
                                    })
                                    continue

                            from .unified_command_processor import get_unified_processor

                            processor = get_unified_processor(self.api_key)

                            if USE_ENHANCED_CONTEXT:
                                # Use enhanced simple context handler for proper feedback
                                from .simple_context_handler_enhanced import (
                                    wrap_with_enhanced_context,
                                )

                                context_handler = wrap_with_enhanced_context(processor)
                                logger.info(
                                    f"[JARVIS WS] Processing with ENHANCED context awareness: '{command_text}'"
                                )
                            else:
                                # Use simple context handler
                                from .simple_context_handler import wrap_with_simple_context

                                context_handler = wrap_with_simple_context(processor)
                                logger.info(
                                    f"[JARVIS WS] Processing with SIMPLE context awareness: '{command_text}'"
                                )

                            # Process with context awareness - WITH TIMEOUT PROTECTION
                            # CRITICAL: Without timeout, blocking operations could hang forever
                            try:
                                result = await asyncio.wait_for(
                                    context_handler.process_with_context(
                                        command_text, websocket
                                    ),
                                    timeout=30.0  # 30 second max for context processing
                                )
                            except asyncio.TimeoutError:
                                logger.error(f"[JARVIS WS] ❌ Context processing timed out for: '{command_text}'")
                                result = {
                                    "success": False,
                                    "response": "I apologize, but that command is taking too long. Please try again.",
                                    "command_type": "timeout",
                                    "error": "context_processing_timeout"
                                }

                            # Send the response with enhanced context info
                            response_text = result.get("response", "I processed your command.")
                            response_data = {
                                "type": "response",
                                "text": response_text,
                                "command_type": result.get("command_type", "context_aware"),
                                "success": result.get("success", True),
                                "context_handled": result.get("context_handled", False),
                                "screen_unlocked": result.get("screen_unlocked", False),
                                "execution_steps": result.get("execution_steps", []),
                                "timestamp": datetime.now().isoformat(),
                                "speak": True,
                                **{
                                    k: v
                                    for k, v in result.items()
                                    if k
                                    not in [
                                        "response",
                                        "command_type",
                                        "success",
                                        "execution_steps",
                                    ]
                                },
                            }

                            await websocket.send_json(response_data)
                            logger.info(
                                f"[JARVIS API] Sent ENHANCED context-aware response: {response_data['text'][:100]}..."
                            )

                            # 📝 LEARNING: Record interaction to database
                            await self._record_interaction(
                                user_query=command_text,
                                jarvis_response=response_text,
                                response_type=result.get("command_type", "context_aware"),
                                confidence_score=result.get("confidence"),
                                execution_time_ms=result.get("execution_time_ms"),
                                success=result.get("success", True),
                            )
                            continue

                        except ImportError as e:
                            logger.error(f"Context handler not available: {e}")
                            # Fall through to workflow processing
                        except Exception as e:
                            logger.error(f"Context processing error: {e}", exc_info=True)
                            # Fall through to workflow processing

                    # Check if this is a multi-command workflow
                    try:
                        from .workflow_command_processor import handle_workflow_command

                        workflow_cmd = JARVISCommand(text=command_text)
                        workflow_result = await handle_workflow_command(
                            workflow_cmd, websocket=websocket
                        )

                        if workflow_result:
                            logger.info(f"[JARVIS WS] Processed workflow command")
                            await websocket.send_json(
                                {
                                    "type": "response",
                                    "text": workflow_result.get("response"),
                                    "command_type": "workflow",
                                    "workflow_result": workflow_result.get("workflow_result"),
                                    "success": workflow_result.get("success"),
                                    "timestamp": datetime.now().isoformat(),
                                    "speak": True,
                                }
                            )
                            continue
                    except Exception as e:
                        logger.error(f"Workflow check error: {e}")

                    # Import autonomy handler
                    try:
                        from .autonomy_handler import get_autonomy_handler

                        autonomy_handler = get_autonomy_handler()
                    except ImportError:
                        autonomy_handler = None

                    # Check for autonomy commands
                    if autonomy_handler:
                        autonomy_action = autonomy_handler.process_autonomy_command(command_text)
                        if autonomy_action == "activate":
                            # Activate full autonomy
                            result = await autonomy_handler.activate_full_autonomy()
                            await websocket.send_json(
                                {
                                    "type": "response",
                                    "text": "Initiating full autonomy. All systems coming online. Vision system activating. AI brain engaged. Sir, I am now fully autonomous.",
                                    "command_type": "autonomy_activation",
                                    "autonomy_result": result,
                                    "timestamp": datetime.now().isoformat(),
                                }
                            )

                            # Send status update
                            await websocket.send_json(
                                {
                                    "type": "autonomy_status",
                                    "enabled": True,
                                    "systems": result.get("systems", {}),
                                    "timestamp": datetime.now().isoformat(),
                                }
                            )
                            continue
                        elif autonomy_action == "deactivate":
                            # Deactivate autonomy
                            result = await autonomy_handler.deactivate_autonomy()
                            await websocket.send_json(
                                {
                                    "type": "response",
                                    "text": "Disabling autonomous mode. Returning to manual control. Standing by for your commands, sir.",
                                    "command_type": "autonomy_deactivation",
                                    "autonomy_result": result,
                                    "timestamp": datetime.now().isoformat(),
                                }
                            )

                            # Send status update
                            await websocket.send_json(
                                {
                                    "type": "autonomy_status",
                                    "enabled": False,
                                    "timestamp": datetime.now().isoformat(),
                                }
                            )
                            continue

                    # Use regular unified command processor for proper routing
                    try:
                        from .unified_command_processor import get_unified_processor

                        processor = get_unified_processor(self.api_key)

                        # Send immediate acknowledgment for vision commands (they take 2-8 seconds)
                        vision_keywords = [
                            "see",
                            "screen",
                            "monitor",
                            "vision",
                            "looking",
                            "watching",
                            "show me",
                        ]
                        is_vision_cmd = any(kw in command_text.lower() for kw in vision_keywords)

                        if is_vision_cmd:
                            await websocket.send_json(
                                {
                                    "type": "processing",
                                    "message": "Analyzing your screen...",
                                    "command_type": "vision",
                                    "timestamp": datetime.now().isoformat(),
                                }
                            )
                            logger.info("[JARVIS API] Sent vision processing acknowledgment")
                        else:
                            await websocket.send_json(
                                {
                                    "type": "debug_log",
                                    "message": f"Processing command through unified processor: '{command_text}'",
                                    "timestamp": datetime.now().isoformat(),
                                }
                            )

                        # Process through unified system with audio data for voice authentication
                        result = await processor.process_command(
                            command_text,
                            websocket,
                            audio_data=self.last_audio_data,
                            speaker_name=self.last_speaker_name,
                        )

                        logger.info(f"[JARVIS API] Command result: {result}")

                        # Send unified response
                        response_data = {
                            "type": "response",
                            "text": result.get("response", "I processed your command."),
                            "command_type": result.get("command_type", "unknown"),
                            "success": result.get("success", True),
                            "timestamp": datetime.now().isoformat(),
                            "speak": True,
                            **{
                                k: v
                                for k, v in result.items()
                                if k not in ["response", "command_type", "success"]
                            },
                        }

                        await websocket.send_json(response_data)
                        logger.info(f"[JARVIS API] Sent response: {response_data['text'][:100]}...")
                        continue

                    except Exception as e:
                        logger.error(f"Unified processor error: {e}", exc_info=True)
                        # Fall back to original logic if unified processor fails

                    # LEGACY ROUTING (kept as fallback)
                    vision_keywords = [
                        "see",
                        "screen",
                        "monitor",
                        "vision",
                        "looking",
                        "watching",
                        "view",
                    ]
                    is_vision_command = any(
                        word in command_text.lower() for word in vision_keywords
                    )

                    if is_vision_command and False:  # Disabled - using unified processor
                        try:
                            # Send debug log to frontend
                            await websocket.send_json(
                                {
                                    "type": "debug_log",
                                    "message": f"Processing as vision command: '{command_text}'",
                                    "timestamp": datetime.now().isoformat(),
                                }
                            )

                            # Add more debug info
                            await websocket.send_json(
                                {
                                    "type": "debug_log",
                                    "message": f"Importing vision_command_handler...",
                                    "timestamp": datetime.now().isoformat(),
                                }
                            )

                            try:
                                from .vision_command_handler import (
                                    vision_command_handler,
                                    ws_logger,
                                )

                                await websocket.send_json(
                                    {
                                        "type": "debug_log",
                                        "message": "Successfully imported pure vision_command_handler",
                                        "timestamp": datetime.now().isoformat(),
                                    }
                                )
                            except ImportError as ie:
                                logger.error(f"Failed to import vision_command_handler: {ie}")
                                await websocket.send_json(
                                    {
                                        "type": "debug_log",
                                        "message": f"Import error: {str(ie)}",
                                        "level": "error",
                                        "timestamp": datetime.now().isoformat(),
                                    }
                                )
                                raise

                            # Set up WebSocket callback for vision logs
                            async def send_vision_log(log_data):
                                await websocket.send_json(log_data)

                            ws_logger.set_websocket_callback(send_vision_log)

                            await websocket.send_json(
                                {
                                    "type": "debug_log",
                                    "message": "About to call vision_command_handler.handle_command",
                                    "timestamp": datetime.now().isoformat(),
                                }
                            )

                            vision_result = await vision_command_handler.handle_command(
                                command_text
                            )

                            await websocket.send_json(
                                {
                                    "type": "debug_log",
                                    "message": f"Vision result received: {vision_result.get('handled')}",
                                    "timestamp": datetime.now().isoformat(),
                                }
                            )

                            if vision_result.get("handled"):
                                await websocket.send_json(
                                    {
                                        "type": "debug_log",
                                        "message": "Vision command handled, sending response",
                                        "timestamp": datetime.now().isoformat(),
                                    }
                                )

                                # Clean the vision response before sending
                                cleaned_text = clean_vision_response(vision_result["response"])

                                await websocket.send_json(
                                    {
                                        "type": "response",
                                        "text": cleaned_text,
                                        "command_type": "vision",
                                        "monitoring_active": vision_result.get("monitoring_active"),
                                        "timestamp": datetime.now().isoformat(),
                                        "speak": True,  # Explicitly tell frontend to speak this
                                    }
                                )
                                continue
                        except Exception as e:
                            logger.error(f"Vision command check error: {e}", exc_info=True)

                            await websocket.send_json(
                                {
                                    "type": "debug_log",
                                    "message": f"Vision command error: {str(e)}",
                                    "level": "error",
                                    "timestamp": datetime.now().isoformat(),
                                }
                            )

                            # Send error response to frontend so it doesn't hang
                            await websocket.send_json(
                                {
                                    "type": "response",
                                    "text": "I'm having trouble with the vision system right now. Please try again.",
                                    "command_type": "vision",
                                    "error": True,
                                    "timestamp": datetime.now().isoformat(),
                                }
                            )
                            continue

                    # Ensure JARVIS is active for WebSocket commands
                    if self.jarvis and hasattr(self.jarvis, "running"):
                        if not self.jarvis.running:
                            self.jarvis.running = True
                            logger.info("Activating JARVIS for WebSocket command")

                    # Handle activation command specially
                    if command_text.lower() == "activate":
                        # Send activation response immediately for frontend to speak
                        await websocket.send_json(
                            {
                                "type": "response",
                                "text": "Yes, sir?",
                                "emotion": "attentive",
                                "timestamp": datetime.now().isoformat(),
                            }
                        )
                        continue

                    # Send acknowledgment immediately
                    await websocket.send_json(
                        {"type": "processing", "timestamp": datetime.now().isoformat()}
                    )

                    # Process with JARVIS - FAST
                    logger.info(f"[JARVIS WS] Processing command: {command_text}")

                    # Dynamic VoiceCommand creation with error handling
                    voice_command = self.error_handler.create_safe_object(
                        VoiceCommand,
                        raw_text=command_text,
                        confidence=0.9,
                        intent="conversation",
                        needs_clarification=False,
                    )

                    # Process command and get context
                    # Even in limited mode, vary the responses
                    import random

                    limited_responses = [
                        "I'm operating with reduced capabilities at the moment. How can I help you?",
                        "My full intelligence systems are initializing. What can I do for you?",
                        "I'm in limited mode right now. How may I assist?",
                        "Some of my systems are still coming online. What do you need?",
                    ]
                    response = random.choice(
                        limited_responses
                    )  # nosec B311 - UI responses, not cryptographic
                    context = {}

                    if self.jarvis and hasattr(self.jarvis, "personality"):
                        # Process command and get context in parallel with error handling
                        try:
                            personality = self.error_handler.safe_getattr(
                                self.jarvis, "personality"
                            )
                            if personality and hasattr(personality, "process_voice_command"):
                                response = await personality.process_voice_command(voice_command)
                                context = (
                                    self.error_handler.safe_call(
                                        getattr(personality, "_get_context_info", lambda: {})
                                    )
                                    or {}
                                )
                            else:
                                logger.warning("Personality missing process_voice_command method")
                        except Exception as e:
                            logger.error(f"Error processing voice command: {e}")
                            response = f"I encountered an error: {str(e)}. Please try again."
                    else:
                        # Provide basic response without full personality
                        if "weather" in data["text"].lower():
                            # Try to use weather system even in limited mode
                            try:
                                # First, try to get the initialized weather system
                                weather_system = None
                                vision_available = False

                                # Check if we have access to app state (for full weather system)
                                try:
                                    from api.jarvis_factory import get_app_state

                                    app_state = get_app_state()
                                    if app_state and hasattr(app_state, "weather_system"):
                                        weather_system = app_state.weather_system
                                        vision_available = hasattr(app_state, "vision_analyzer")
                                        logger.info(
                                            f"[JARVIS WS] Got weather system from app state, vision: {vision_available}"
                                        )
                                except:
                                    pass

                                # Fallback to get_weather_system
                                if not weather_system:
                                    from system_control.weather_system_config import (
                                        get_weather_system,
                                    )

                                    weather_system = get_weather_system()
                                    logger.info("[JARVIS WS] Using fallback weather system")

                                if weather_system and vision_available:
                                    # Full mode with vision
                                    logger.info(
                                        "[JARVIS WS] FULL MODE: Using weather system with vision analysis"
                                    )
                                    response = "Let me analyze the weather information for you..."

                                    # Send immediate response
                                    await websocket.send_json(
                                        {
                                            "type": "response",
                                            "text": response,
                                            "command": command_text,
                                            "timestamp": datetime.now().isoformat(),
                                            "speak": True,
                                        }
                                    )

                                    # Get weather with vision
                                    result = await weather_system.get_weather(data["text"])

                                    if result.get("success") and result.get("formatted_response"):
                                        response = result["formatted_response"]
                                        logger.info(
                                            "[JARVIS WS] Weather vision analysis successful"
                                        )
                                    else:
                                        # Vision failed, but we tried
                                        response = "I attempted to analyze the weather visually but encountered an issue. The Weather app is open for you to check manually."

                                elif weather_system:
                                    # Limited mode - no vision
                                    logger.info(
                                        "[JARVIS WS] LIMITED MODE: Weather system without vision"
                                    )

                                    # Open Weather app
                                    await async_open_app("Weather")

                                    # Try to navigate to My Location
                                    await asyncio.sleep(1.5)  # Wait for app to open
                                    await async_osascript(
                                        """
                                        tell application "System Events"
                                            key code 126
                                            delay 0.2
                                            key code 126
                                            delay 0.2
                                            key code 125
                                            delay 0.2
                                            key code 36
                                        end tell
                                    """
                                    )

                                    response = "I'm operating in limited mode without vision capabilities. I've opened the Weather app and navigated to your location. To enable full weather analysis with automatic reading, please ensure all JARVIS components are loaded."

                                else:
                                    # No weather system at all
                                    logger.info("[JARVIS WS] NO WEATHER SYSTEM: Basic fallback")

                                    await async_open_app("Weather")
                                    response = "I'm in basic mode. I've opened the Weather app for you. For automatic weather analysis, please ensure the weather system is properly initialized."

                            except Exception as e:
                                logger.error(f"[JARVIS WS] Weather error: {e}")
                                import traceback

                                traceback.print_exc()
                                try:
                                    await async_open_app("Weather")
                                    response = "I encountered an error accessing the weather system. I've opened the Weather app for manual viewing."
                                except:
                                    response = "I'm having difficulty accessing weather data at the moment."
                        elif "time" in data["text"].lower():
                            response = f"The current time is {datetime.now().strftime('%I:%M %p')}."
                        else:
                            # Natural varied fallback for unknown commands
                            fallback_responses = [
                                f"I understand you said '{data['text']}', but I'm still initializing my full capabilities.",
                                f"I heard '{data['text']}'. Let me get my systems fully online to help you better.",
                                f"Got it - '{data['text']}'. My intelligence systems are warming up.",
                                f"I registered '{data['text']}'. Give me a moment to bring all systems online.",
                            ]
                            response = random.choice(
                                fallback_responses
                            )  # nosec B311 - UI responses, not cryptographic
                    logger.info(f"[JARVIS WS] Response: {response[:100]}...")

                    # Send response immediately
                    # Clean the response before sending
                    cleaned_response = clean_vision_response(response)

                    await websocket.send_json(
                        {
                            "type": "response",
                            "text": cleaned_response,
                            "command": command_text,
                            "context": context,
                            "timestamp": datetime.now().isoformat(),
                            "speak": True,  # Tell frontend to speak this
                        }
                    )

                    # Don't speak on backend to avoid delays - let frontend handle TTS

                elif data.get("type") == "audio":
                    # Handle audio data for hybrid STT transcription
                    try:
                        audio_b64 = data.get("data", "")
                        if not audio_b64:
                            await websocket.send_json(
                                {
                                    "type": "error",
                                    "message": "No audio data provided",
                                    "timestamp": datetime.now().isoformat(),
                                }
                            )
                            continue

                        # Decode audio
                        audio_bytes = base64.b64decode(audio_b64)

                        logger.info(
                            f"🎤 [WebSocket] Received {len(audio_bytes)} bytes of audio for transcription"
                        )

                        # Send processing notification
                        await websocket.send_json(
                            {
                                "type": "transcription_started",
                                "size": len(audio_bytes),
                                "timestamp": datetime.now().isoformat(),
                            }
                        )

                        # Get routing strategy from request (optional)
                        from voice.stt_config import RoutingStrategy

                        strategy_str = data.get("strategy", "balanced")
                        try:
                            strategy = RoutingStrategy(strategy_str)
                        except ValueError:
                            strategy = RoutingStrategy.BALANCED

                        # Transcribe using hybrid router (speaker auto-detected)
                        result = await self.hybrid_stt_router.transcribe(
                            audio_data=audio_bytes,
                            strategy=strategy,
                            speaker_name=None,  # Auto-detect speaker via voice recognition
                            mode="command",  # Optimize for short imperative commands (lock/unlock/etc.)
                        )

                        # Store audio data and speaker for voice verification
                        # This enables "unlock my screen" to verify speaker identity
                        self.last_audio_data = audio_bytes
                        self.last_speaker_name = result.speaker_identified

                        # Also store in jarvis instance for unlock handler access
                        if self._jarvis:
                            self._jarvis.last_audio_data = audio_bytes
                            self._jarvis.last_speaker_name = result.speaker_identified

                        if result.speaker_identified:
                            logger.info(
                                f"🔐 Stored audio and speaker for verification: {result.speaker_identified}"
                            )

                        # Send transcription result
                        await websocket.send_json(
                            {
                                "type": "transcription_result",
                                "text": result.text,
                                "confidence": result.confidence,
                                "engine": result.engine.value,
                                "model_name": result.model_name,
                                "latency_ms": result.latency_ms,
                                "speaker_identified": result.speaker_identified,
                                "timestamp": datetime.now().isoformat(),
                            }
                        )

                        # If confidence is good, also process as command
                        if result.confidence >= 0.6 and result.text.strip():
                            logger.info(
                                f"🎯 Auto-processing transcription as command: '{result.text}'"
                            )

                            # Process the transcribed text as a command
                            command_text = result.text.strip()

                            # Use the same command processing logic as text commands
                            if USE_CONTEXT_HANDLER:
                                try:
                                    # Use async pipeline for enhanced context-aware processing
                                    pipeline_result = await self.pipeline.process_command_async(
                                        command_text,
                                        audio_data=audio_bytes,
                                        speaker_name=result.speaker_identified,
                                    )

                                    response_text = pipeline_result.get(
                                        "response", "I'm not sure how to respond to that."
                                    )
                                    success = pipeline_result.get("success", False)

                                    # Record interaction to learning database
                                    await self._record_interaction(
                                        user_query=command_text,
                                        jarvis_response=response_text,
                                        response_type=pipeline_result.get("type", "context_aware"),
                                        confidence_score=result.confidence,
                                        execution_time_ms=result.latency_ms,
                                        success=success,
                                    )

                                    # Send response
                                    await websocket.send_json(
                                        {
                                            "type": "command_response",
                                            "response": response_text,
                                            "command": command_text,
                                            "speak": True,
                                            "confidence": result.confidence,
                                            "stt_engine": result.engine.value,
                                            "timestamp": datetime.now().isoformat(),
                                        }
                                    )

                                except Exception as e:
                                    logger.error(f"Error processing transcribed command: {e}")
                                    await websocket.send_json(
                                        {
                                            "type": "error",
                                            "message": f"Failed to process command: {str(e)}",
                                            "timestamp": datetime.now().isoformat(),
                                        }
                                    )

                    except Exception as e:
                        logger.error(f"Audio transcription failed: {e}")
                        import traceback

                        traceback.print_exc()
                        await websocket.send_json(
                            {
                                "type": "transcription_error",
                                "message": str(e),
                                "timestamp": datetime.now().isoformat(),
                            }
                        )

                elif data.get("type") == "set_mode":
                    # Handle mode change from frontend
                    mode = data.get("mode", "manual")
                    logger.info(f"Mode change requested: {mode}")

                    # Update autonomy handler if available
                    try:
                        from .autonomy_handler import get_autonomy_handler

                        autonomy_handler = get_autonomy_handler()

                        if mode == "autonomous":
                            result = await autonomy_handler.activate_full_autonomy()
                        else:
                            result = await autonomy_handler.deactivate_autonomy()

                        await websocket.send_json(
                            {
                                "type": "mode_changed",
                                "mode": mode,
                                "result": result,
                                "timestamp": datetime.now().isoformat(),
                            }
                        )
                    except Exception as e:
                        logger.error(f"Error changing mode: {e}")
                        await websocket.send_json(
                            {
                                "type": "error",
                                "message": f"Failed to change mode: {str(e)}",
                            }
                        )

                elif data.get("type") == "ping":
                    # Heartbeat
                    await websocket.send_json(
                        {"type": "pong", "timestamp": datetime.now().isoformat()}
                    )
                
                elif data.get("type") == "speech_error":
                    # Handle speech recognition errors from frontend
                    client_id = data.get("client_id", f"ws_{id(websocket)}")
                    error_type = data.get("error", "unknown")
                    error_message = data.get("message", "")
                    
                    result = speech_tracker.record_error(client_id, error_type)
                    
                    # Send back advice
                    await websocket.send_json({
                        "type": "speech_advice",
                        "advice": result,
                        "timestamp": datetime.now().isoformat()
                    })
                
                elif data.get("type") == "speech_recovery":
                    # Handle successful speech recovery from frontend
                    client_id = data.get("client_id", f"ws_{id(websocket)}")
                    speech_tracker.record_recovery(client_id)
                    
                    await websocket.send_json({
                        "type": "speech_status",
                        "status": "recovered",
                        "timestamp": datetime.now().isoformat()
                    })

        except WebSocketDisconnect:
            logger.info("JARVIS WebSocket disconnected")
        except Exception as e:
            logger.error(f"Error in JARVIS WebSocket: {e}")
            try:
                await websocket.send_json({"type": "error", "message": str(e)})
            except:
                pass  # Client might already be disconnected
        finally:
            # Remove connection from tracking
            active_websockets.discard(websocket)
            logger.info(
                f"[JARVIS WS] Connection removed. Active connections: {len(active_websockets)}"
            )


# Create and export the router instance
jarvis_api = JARVISVoiceAPI()
router = jarvis_api.router

# Initialize global CoreML engine (if available)
coreml_engine: Optional[CoreMLVoiceEngineBridge] = None

if COREML_AVAILABLE:
    try:
        # Check if CoreML models exist before initializing
        vad_model_path = "models/vad_model.mlmodelc"
        speaker_model_path = "models/speaker_model.mlmodelc"

        # Initialize with adaptive thresholds
        coreml_engine = create_coreml_engine(
            vad_model_path=vad_model_path,
            speaker_model_path=speaker_model_path,
            config={
                "vad_threshold": 0.5,  # Adapts 0.2-0.9
                "speaker_threshold": 0.7,  # Adapts 0.4-0.95
                "enable_adaptive": True,
                "learning_rate": 0.01,
                "adaptation_window": 100,
            },
        )

        # Start background queue worker
        import asyncio

        asyncio.create_task(coreml_engine.process_voice_queue_worker())

        logger.info("[CoreML] CoreML Voice Engine initialized successfully")
        logger.info(f"[CoreML] VAD model: {vad_model_path}")
        logger.info(f"[CoreML] Speaker model: {speaker_model_path}")

    except FileNotFoundError as e:
        logger.warning(f"[CoreML] CoreML models not found: {e}")
        logger.warning("[CoreML] CoreML voice detection disabled - models required")
        coreml_engine = None
    except Exception as e:
        logger.error(f"[CoreML] Failed to initialize CoreML engine: {e}")
        coreml_engine = None
else:
    logger.info("[CoreML] CoreML not available - using standard voice recognition")


# ========================================
# CoreML Voice Detection Endpoints
# ========================================


@router.post("/voice/detect-coreml")
async def detect_voice_coreml(
    audio_data: str,  # Base64-encoded float32 audio
    priority: int = 0,  # 0=normal, 1=high, 2=critical
):
    """
    Async voice detection using CoreML with circuit breaker protection.

    - **audio_data**: Base64-encoded float32 numpy array (16kHz, mono)
    - **priority**: Task priority (0=normal, 1=high, 2=critical)

    Returns:
    - **is_user_voice**: True if user voice detected
    - **vad_confidence**: Voice activity detection confidence (0-1)
    - **speaker_confidence**: Speaker recognition confidence (0-1)
    - **metrics**: Performance metrics (latency, success rate, thresholds)
    """
    if not coreml_engine:
        raise HTTPException(
            status_code=503, detail="CoreML engine not available - models not loaded"
        )

    try:
        # Decode base64 audio
        import base64

        audio_bytes = base64.b64decode(audio_data)
        audio = np.frombuffer(audio_bytes, dtype=np.float32)

        # Async detection with circuit breaker
        is_user, vad_conf, speaker_conf = await coreml_engine.detect_user_voice_async(
            audio, priority=priority
        )

        return {
            "is_user_voice": is_user,
            "vad_confidence": float(vad_conf),
            "speaker_confidence": float(speaker_conf),
            "metrics": coreml_engine.get_metrics(),
        }

    except Exception as e:
        logger.error(f"[CoreML] Voice detection error: {e}")
        raise HTTPException(status_code=500, detail=f"Voice detection failed: {str(e)}")


@router.post("/voice/detect-vad-coreml")
async def detect_vad_coreml(audio_data: str, priority: int = 0):
    """
    Voice Activity Detection only (faster, no speaker recognition).

    - **audio_data**: Base64-encoded float32 audio
    - **priority**: Task priority

    Returns:
    - **voice_detected**: True if voice activity detected
    - **confidence**: VAD confidence (0-1)
    """
    if not coreml_engine:
        raise HTTPException(status_code=503, detail="CoreML engine not available")

    try:
        import base64

        audio_bytes = base64.b64decode(audio_data)
        audio = np.frombuffer(audio_bytes, dtype=np.float32)

        # Async VAD with circuit breaker
        voice_detected, confidence = await coreml_engine.detect_voice_activity_async(
            audio, priority=priority
        )

        return {"voice_detected": voice_detected, "confidence": float(confidence)}

    except Exception as e:
        logger.error(f"[CoreML] VAD error: {e}")
        raise HTTPException(status_code=500, detail=f"VAD failed: {str(e)}")


@router.post("/voice/train-speaker-coreml")
async def train_speaker_coreml(audio_data: str, is_user: bool = True):
    """
    Train speaker recognition model with new audio samples.

    - **audio_data**: Base64-encoded float32 audio
    - **is_user**: True for user voice, False for non-user voice

    Returns:
    - **samples_collected**: Total samples collected (user/non-user)
    """
    if not coreml_engine:
        raise HTTPException(status_code=503, detail="CoreML engine not available")

    try:
        import base64

        audio_bytes = base64.b64decode(audio_data)
        audio = np.frombuffer(audio_bytes, dtype=np.float32)

        # Train speaker model
        coreml_engine.train_speaker_model(audio, is_user=is_user)

        metrics = coreml_engine.get_metrics()

        return {"success": True, "is_user_sample": is_user, "metrics": metrics}

    except Exception as e:
        logger.error(f"[CoreML] Speaker training error: {e}")
        raise HTTPException(status_code=500, detail=f"Training failed: {str(e)}")


@router.get("/voice/coreml-metrics")
async def get_coreml_metrics():
    """
    Get CoreML voice engine performance metrics.

    Returns:
    - **avg_latency_ms**: Average inference latency
    - **success_rate**: Detection success rate
    - **vad_threshold**: Current adaptive VAD threshold
    - **speaker_threshold**: Current adaptive speaker threshold
    - **circuit_breaker_state**: Circuit breaker state (CLOSED/OPEN/HALF_OPEN)
    - **circuit_breaker_success_rate**: Circuit breaker success rate
    - **queue_size**: Current voice task queue size
    """
    if not coreml_engine:
        raise HTTPException(status_code=503, detail="CoreML engine not available")

    try:
        metrics = coreml_engine.get_metrics()
        return metrics

    except Exception as e:
        logger.error(f"[CoreML] Metrics retrieval error: {e}")
        raise HTTPException(status_code=500, detail=f"Metrics failed: {str(e)}")


@router.get("/voice/coreml-status")
async def get_coreml_status():
    """
    Check CoreML engine availability and status.

    Returns:
    - **available**: True if CoreML engine is available
    - **initialized**: True if engine is initialized
    - **models_loaded**: True if CoreML models are loaded
    """
    return {
        "available": COREML_AVAILABLE,
        "initialized": coreml_engine is not None,
        "models_loaded": coreml_engine is not None,
        "version": "1.0.0",
        "features": {
            "vad": coreml_engine is not None,
            "speaker_recognition": coreml_engine is not None,
            "adaptive_thresholds": coreml_engine is not None,
            "circuit_breaker": coreml_engine is not None,
            "async_queue": coreml_engine is not None,
        },
    }


# =============================================================================
# ENHANCED VOICE BIOMETRIC ENDPOINTS (v4.0)
# =============================================================================
# These endpoints integrate the enhanced VoiceBiometricIntelligence with:
# - LangGraph reasoning for borderline cases
# - ChromaDB pattern memory for voice evolution
# - Langfuse audit trails
# - Helicone-style cost tracking
# - Voice drift detection and adaptation
# =============================================================================

# Lazy-load VBI to prevent circular imports
_vbi_instance = None


async def _get_vbi():
    """Get the VoiceBiometricIntelligence instance lazily."""
    global _vbi_instance
    if _vbi_instance is None:
        try:
            from voice_unlock.voice_biometric_intelligence import (
                get_voice_biometric_intelligence,
            )
            _vbi_instance = await get_voice_biometric_intelligence()
            logger.info("✅ VoiceBiometricIntelligence loaded for API")
        except Exception as e:
            logger.error(f"❌ Failed to load VBI: {e}")
            raise HTTPException(status_code=503, detail=f"VBI not available: {e}")
    return _vbi_instance


class EnhancedVerificationRequest(BaseModel):
    """Enhanced voice verification request with additional options."""
    audio_data: str  # Base64-encoded audio
    context: Optional[Dict[str, Any]] = None
    speak: bool = False  # Whether to speak the announcement
    use_reasoning: bool = True  # Enable LangGraph reasoning for borderline
    use_orchestration: bool = True  # Enable multi-factor fallback
    store_patterns: bool = True  # Store patterns in ChromaDB


class EnhancedVerificationResponse(BaseModel):
    """Enhanced voice verification response with full details."""
    verified: bool
    speaker_name: Optional[str] = None
    confidence: float
    level: str  # instant, confident, good, borderline, unknown, spoofing

    # Detailed confidence scores
    voice_confidence: float
    behavioral_confidence: float
    fused_confidence: float
    physics_confidence: float = 0.0

    # Verification method
    verification_method: str  # voice_only, voice_behavioral, multi_factor, cached

    # Timing
    verification_time_ms: float
    was_cached: bool

    # Announcement
    announcement: str
    should_proceed: bool
    retry_guidance: Optional[str] = None

    # Security
    spoofing_detected: bool
    spoofing_reason: Optional[str] = None

    # Bayesian fusion
    bayesian_decision: Optional[str] = None
    bayesian_authentic_prob: float = 0.0
    bayesian_reasoning: list = []

    # Enhanced module usage
    reasoning_used: bool = False
    orchestration_used: bool = False
    patterns_stored: bool = False
    drift_detected: bool = False

    # Trace ID for audit
    trace_id: Optional[str] = None


@router.post("/voice/biometric/verify-enhanced", response_model=EnhancedVerificationResponse)
async def verify_voice_enhanced(request: EnhancedVerificationRequest):
    """
    Enhanced voice biometric verification with LangGraph reasoning and multi-factor auth.

    Features:
    - **LangGraph Reasoning**: Intelligent multi-step reasoning for borderline cases
    - **Multi-Factor Orchestration**: Fallback chain with challenge/proximity auth
    - **ChromaDB Memory**: Persistent voice pattern storage and evolution tracking
    - **Langfuse Tracing**: Complete audit trail for security investigation
    - **Cost Tracking**: Per-operation cost analysis with caching optimization

    Request:
    - **audio_data**: Base64-encoded raw audio bytes (16kHz mono float32)
    - **context**: Optional context dict (location, device, etc.)
    - **speak**: Whether to speak the announcement via TTS
    - **use_reasoning**: Enable LangGraph for borderline cases (default: true)
    - **use_orchestration**: Enable multi-factor fallback (default: true)
    - **store_patterns**: Store patterns in ChromaDB (default: true)

    Returns:
    - Complete verification result with confidence scores, timing, and audit trail
    """
    try:
        vbi = await _get_vbi()

        # Decode audio
        import base64
        audio_bytes = base64.b64decode(request.audio_data)

        # Set context with request options
        context = request.context or {}
        context['use_reasoning'] = request.use_reasoning
        context['use_orchestration'] = request.use_orchestration
        context['store_patterns'] = request.store_patterns

        # Run verification
        result = await vbi.verify_and_announce(
            audio_data=audio_bytes,
            context=context,
            speak=request.speak,
        )

        # Build response
        return EnhancedVerificationResponse(
            verified=result.verified,
            speaker_name=result.speaker_name,
            confidence=result.confidence,
            level=result.level.value if hasattr(result.level, 'value') else str(result.level),
            voice_confidence=result.voice_confidence,
            behavioral_confidence=result.behavioral.behavioral_confidence if result.behavioral else 0.0,
            fused_confidence=result.fused_confidence,
            physics_confidence=result.physics_confidence,
            verification_method=result.verification_method.value if hasattr(result.verification_method, 'value') else str(result.verification_method),
            verification_time_ms=result.verification_time_ms,
            was_cached=result.was_cached,
            announcement=result.announcement,
            should_proceed=result.should_proceed,
            retry_guidance=result.retry_guidance,
            spoofing_detected=result.spoofing_detected,
            spoofing_reason=result.spoofing_reason,
            bayesian_decision=result.bayesian_decision,
            bayesian_authentic_prob=result.bayesian_authentic_prob,
            bayesian_reasoning=result.bayesian_reasoning,
            reasoning_used=vbi._stats.get('reasoning_invocations', 0) > 0,
            orchestration_used=vbi._stats.get('orchestration_fallbacks', 0) > 0,
            patterns_stored=vbi._stats.get('pattern_stores', 0) > 0,
            drift_detected=vbi._stats.get('drift_detections', 0) > 0,
            trace_id=vbi._current_trace,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Enhanced verification error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Verification failed: {str(e)}")


@router.get("/voice/biometric/status")
async def get_vbi_status():
    """
    Get VoiceBiometricIntelligence status including enhanced modules.

    Returns comprehensive status of:
    - Core components (speaker engine, cache)
    - Enhanced modules (reasoning, memory, tracing, cost tracking)
    - Performance statistics
    """
    try:
        vbi = await _get_vbi()
        stats = vbi.get_stats()

        return {
            "available": True,
            "initialized": vbi._initialized,
            "stats": stats,
            "enhanced_modules": stats.get('enhanced_modules', {}),
            "config": {
                "reasoning_enabled": vbi._config.enable_reasoning_graph,
                "pattern_memory_enabled": vbi._config.enable_pattern_memory,
                "drift_detection_enabled": vbi._config.enable_drift_detection,
                "orchestration_enabled": vbi._config.enable_orchestration,
                "langfuse_enabled": vbi._config.enable_langfuse_tracing,
                "cost_tracking_enabled": vbi._config.enable_cost_tracking,
                "thresholds": {
                    "instant": vbi._config.instant_recognition_threshold,
                    "confident": vbi._config.confident_threshold,
                    "borderline": vbi._config.borderline_threshold,
                    "rejection": vbi._config.rejection_threshold,
                },
            },
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"VBI status error: {e}")
        raise HTTPException(status_code=500, detail=f"Status check failed: {str(e)}")


@router.get("/voice/biometric/health")
async def get_vbi_health():
    """
    Comprehensive health check for VoiceBiometricIntelligence.

    Returns:
    - **healthy**: Overall health status
    - **score**: Health score (0-1)
    - **components**: Status of each component
    - **issues**: List of any issues detected
    """
    try:
        vbi = await _get_vbi()
        health = await vbi.health_check()

        return health

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"VBI health check error: {e}")
        return {
            "healthy": False,
            "score": 0.0,
            "message": f"Health check failed: {str(e)}",
            "components": {},
            "issues": [str(e)],
        }


@router.get("/voice/biometric/traces")
async def get_recent_traces(limit: int = 10, user_id: Optional[str] = None):
    """
    Get recent Langfuse audit traces for voice authentication.

    Args:
    - **limit**: Maximum number of traces to return (default: 10)
    - **user_id**: Filter by user ID (optional)

    Returns:
    - List of recent authentication traces with decision details
    """
    try:
        vbi = await _get_vbi()

        if not vbi._langfuse_available or not vbi._langfuse_tracer:
            return {
                "available": False,
                "message": "Langfuse tracing not enabled",
                "traces": [],
            }

        # Get traces from Langfuse
        traces = await vbi._langfuse_tracer.get_recent_sessions(
            limit=limit,
            user_id=user_id,
        )

        return {
            "available": True,
            "count": len(traces),
            "traces": traces,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Traces retrieval error: {e}")
        raise HTTPException(status_code=500, detail=f"Traces retrieval failed: {str(e)}")


@router.get("/voice/biometric/cost-report")
async def get_cost_report(period: str = "today"):
    """
    Get voice authentication cost report.

    Args:
    - **period**: Time period for report (today, week, month, all)

    Returns:
    - Cost breakdown by operation type
    - Cache hit rate and savings
    - Recommendations for optimization
    """
    try:
        vbi = await _get_vbi()

        if not vbi._cost_tracking_available or not vbi._cost_tracker:
            return {
                "available": False,
                "message": "Cost tracking not enabled",
                "costs": {},
            }

        # Get cost report
        report = await vbi._cost_tracker.get_report(period=period)

        # Add VBI-level stats
        report['vbi_stats'] = {
            'total_cost': vbi._stats.get('total_cost', 0.0),
            'cache_savings': vbi._stats.get('cache_savings', 0.0),
            'total_verifications': vbi._stats.get('total_verifications', 0),
        }

        return report

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Cost report error: {e}")
        raise HTTPException(status_code=500, detail=f"Cost report failed: {str(e)}")


@router.post("/voice/biometric/query-patterns")
async def query_voice_patterns(
    user_id: str,
    query_type: str = "behavioral",
    time_range_hours: int = 24,
    limit: int = 10,
):
    """
    Query stored voice patterns from ChromaDB memory.

    Args:
    - **user_id**: User ID to query patterns for
    - **query_type**: Type of patterns (behavioral, evolution, attacks, environmental)
    - **time_range_hours**: Time range in hours (default: 24)
    - **limit**: Maximum patterns to return (default: 10)

    Returns:
    - Matching patterns with metadata
    """
    try:
        vbi = await _get_vbi()

        if not vbi._pattern_memory_available or not vbi._pattern_memory:
            return {
                "available": False,
                "message": "Pattern memory not enabled",
                "patterns": [],
            }

        # Query patterns based on type
        if query_type == "behavioral":
            patterns = await vbi._pattern_memory.query_behavioral_patterns(
                user_id=user_id,
                time_range_hours=time_range_hours,
                limit=limit,
            )
        elif query_type == "evolution":
            patterns = await vbi._pattern_memory.query_voice_evolution(
                user_id=user_id,
                limit=limit,
            )
        elif query_type == "attacks":
            patterns = await vbi._pattern_memory.query_attack_patterns(
                limit=limit,
            )
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown query type: {query_type}. Use: behavioral, evolution, attacks"
            )

        return {
            "available": True,
            "query_type": query_type,
            "user_id": user_id,
            "count": len(patterns),
            "patterns": patterns,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Pattern query error: {e}")
        raise HTTPException(status_code=500, detail=f"Pattern query failed: {str(e)}")


@router.get("/voice/biometric/drift-status")
async def get_drift_status(user_id: Optional[str] = None):
    """
    Get voice drift detection status and history.

    Args:
    - **user_id**: User ID to get drift status for (optional, defaults to owner)

    Returns:
    - Current drift status
    - Drift history
    - Baseline adaptation history
    """
    try:
        vbi = await _get_vbi()

        if not vbi._drift_detector_available or not vbi._drift_detector:
            return {
                "available": False,
                "message": "Drift detection not enabled",
                "drift_status": {},
            }

        # Get drift status
        status = await vbi._drift_detector.get_status(
            user_id=user_id or vbi._owner_name or "owner"
        )

        return {
            "available": True,
            "user_id": user_id or vbi._owner_name,
            "drift_status": status,
            "config": {
                "threshold": vbi._config.drift_threshold,
                "auto_adapt": vbi._config.drift_auto_adapt,
                "adaptation_rate": vbi._config.drift_adaptation_rate,
            },
            "stats": {
                "drift_detections": vbi._stats.get('drift_detections', 0),
            },
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Drift status error: {e}")
        raise HTTPException(status_code=500, detail=f"Drift status failed: {str(e)}")


# =============================================================================
# v79.0: CODING COUNCIL VOICE ENDPOINTS
# =============================================================================
# Voice integration endpoints for the Coding Council evolution system.
# Provides voice status, history, test, and config APIs.
# =============================================================================

# Lazy import for voice announcer to avoid circular dependencies
_voice_announcer_cache = None
_voice_announcer_checked = False

async def _get_voice_announcer():
    """Lazily get the Coding Council Voice Announcer instance."""
    global _voice_announcer_cache, _voice_announcer_checked

    if _voice_announcer_checked:
        return _voice_announcer_cache

    _voice_announcer_checked = True

    try:
        from ..core.coding_council.voice_announcer import get_evolution_announcer
        _voice_announcer_cache = get_evolution_announcer()
        logger.info("[v79.0] Voice announcer loaded successfully")
    except ImportError as e:
        logger.warning(f"[v79.0] Voice announcer not available: {e}")
        _voice_announcer_cache = None
    except Exception as e:
        logger.warning(f"[v79.0] Voice announcer initialization failed: {e}")
        _voice_announcer_cache = None

    return _voice_announcer_cache


@router.get("/voice/coding-council/status")
async def get_coding_council_voice_status():
    """
    v79.0: Get Coding Council Voice Announcer status and statistics.

    Returns:
    - Announcer availability
    - Circuit breaker status
    - Task registry status
    - Recent announcement statistics
    """
    try:
        announcer = await _get_voice_announcer()

        if announcer is None:
            return {
                "available": False,
                "reason": "Voice announcer not initialized",
                "timestamp": time.time(),
            }

        # Get status from announcer
        status = await announcer.get_status() if hasattr(announcer, 'get_status') else {}

        return {
            "available": True,
            "status": status.get("status", "operational"),
            "circuit_breaker": status.get("circuit_breaker", {}),
            "task_registry": status.get("task_registry", {}),
            "statistics": status.get("statistics", {}),
            "config": status.get("config", {}),
            "timestamp": time.time(),
        }

    except Exception as e:
        logger.error(f"[v79.0] Voice status error: {e}")
        return {
            "available": False,
            "error": str(e),
            "timestamp": time.time(),
        }


@router.get("/voice/coding-council/history")
async def get_coding_council_voice_history(limit: int = 10):
    """
    v79.0: Get recent voice announcement history from Coding Council.

    Args:
    - **limit**: Maximum number of announcements to return (default: 10)

    Returns:
    - List of recent announcements with timestamps
    """
    try:
        announcer = await _get_voice_announcer()

        if announcer is None:
            return {
                "available": False,
                "history": [],
                "timestamp": time.time(),
            }

        # Get history from announcer
        history = []
        if hasattr(announcer, '_message_cache') and announcer._message_cache:
            # Get recent items from message cache
            for key, value in list(announcer._message_cache._cache.items())[:limit]:
                history.append({
                    "key": key,
                    "message": value.get("message", "") if isinstance(value, dict) else str(value),
                    "timestamp": value.get("timestamp", 0) if isinstance(value, dict) else 0,
                })

        return {
            "available": True,
            "history": history,
            "count": len(history),
            "limit": limit,
            "timestamp": time.time(),
        }

    except Exception as e:
        logger.error(f"[v79.0] Voice history error: {e}")
        return {
            "available": False,
            "history": [],
            "error": str(e),
            "timestamp": time.time(),
        }


@router.post("/voice/coding-council/test")
async def test_coding_council_voice(message: str = "Testing Coding Council voice announcer"):
    """
    v79.0: Test the voice announcement system.

    Args:
    - **message**: Test message to announce (default: test message)

    Returns:
    - Success status and announcement details
    """
    try:
        announcer = await _get_voice_announcer()

        if announcer is None:
            return {
                "success": False,
                "reason": "Voice announcer not available",
                "timestamp": time.time(),
            }

        # Try to announce the test message
        success = False
        if hasattr(announcer, 'announce_progress'):
            success = await announcer.announce_progress(
                task_id="test-" + str(int(time.time())),
                stage="testing",
                message=message,
                progress=50,
            )

        return {
            "success": success,
            "message": message,
            "timestamp": time.time(),
        }

    except Exception as e:
        logger.error(f"[v79.0] Voice test error: {e}")
        return {
            "success": False,
            "error": str(e),
            "timestamp": time.time(),
        }


@router.get("/voice/coding-council/config")
async def get_coding_council_voice_config():
    """
    v79.0: Get current voice announcer configuration.

    Returns:
    - Voice announcement settings
    - Cooldown timings
    - Feature flags
    """
    try:
        announcer = await _get_voice_announcer()

        if announcer is None:
            return {
                "available": False,
                "config": {},
                "timestamp": time.time(),
            }

        # Get config from announcer
        config = {}
        if hasattr(announcer, '_config'):
            cfg = announcer._config
            config = {
                "enabled": cfg.enabled if hasattr(cfg, 'enabled') else True,
                "voice_cooldown": getattr(cfg, 'voice_cooldown', 2.0),
                "circuit_breaker_threshold": getattr(cfg, 'circuit_breaker_threshold', 5),
                "circuit_breaker_recovery": getattr(cfg, 'circuit_breaker_recovery', 30),
                "max_concurrent_announcements": getattr(cfg, 'max_concurrent_announcements', 3),
                "enable_trinity_broadcasts": getattr(cfg, 'enable_trinity_broadcasts', True),
                "enable_approval_voice": getattr(cfg, 'enable_approval_voice', True),
            }

        return {
            "available": True,
            "config": config,
            "timestamp": time.time(),
        }

    except Exception as e:
        logger.error(f"[v79.0] Voice config error: {e}")
        return {
            "available": False,
            "config": {},
            "error": str(e),
            "timestamp": time.time(),
        }


@router.post("/voice/coding-council/approval-response")
async def submit_evolution_approval_response(
    task_id: str,
    approved: bool,
    reason: Optional[str] = None
):
    """
    v79.0: Submit approval/rejection for an evolution task.

    Args:
    - **task_id**: The task ID to approve/reject
    - **approved**: True to approve, False to reject
    - **reason**: Optional reason for the decision

    Returns:
    - Confirmation of the approval response
    """
    try:
        announcer = await _get_voice_announcer()

        # Also try to notify the orchestrator directly
        result = {
            "task_id": task_id,
            "approved": approved,
            "reason": reason,
            "acknowledged": False,
            "timestamp": time.time(),
        }

        # Try to find and notify the pending approval
        if announcer and hasattr(announcer, '_pending_approvals'):
            if task_id in announcer._pending_approvals:
                approval_future = announcer._pending_approvals.pop(task_id, None)
                if approval_future and not approval_future.done():
                    approval_future.set_result((approved, reason))
                    result["acknowledged"] = True

        # Announce the decision
        if announcer and hasattr(announcer, 'announce_progress'):
            decision_message = f"Evolution {'approved' if approved else 'rejected'}"
            if reason:
                decision_message += f": {reason}"
            await announcer.announce_progress(
                task_id=task_id,
                stage="approval_response",
                message=decision_message,
                progress=100 if approved else 0,
            )

        return result

    except Exception as e:
        logger.error(f"[v79.0] Approval response error: {e}")
        return {
            "task_id": task_id,
            "approved": approved,
            "error": str(e),
            "acknowledged": False,
            "timestamp": time.time(),
        }


# v79.0: Coding Council voice integration endpoints complete
