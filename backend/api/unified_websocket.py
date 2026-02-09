"""
Unified WebSocket Handler - Advanced Self-Healing WebSocket System

Features:
- Intelligent self-healing with automatic recovery
- UAE (Unified Awareness Engine) integration for system intelligence
- SAI (Situational Awareness Intelligence) integration for context
- Learning Database integration for pattern recognition
- Dynamic recovery strategies with no hardcoding
- Circuit breaker pattern for resilience
- Predictive disconnection prevention
- Advanced async operations with robust error handling
"""

import asyncio
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Set

# Initialize logger FIRST before using it
logger = logging.getLogger(__name__)

# Import async pipeline for non-blocking WebSocket operations
from core.async_pipeline import get_async_pipeline
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

# Import VBI Debug Tracer for comprehensive voice unlock logging
try:
    from core.vbi_debug_tracer import (
        get_tracer,
        get_orchestrator,
        VBIStage,
        VBIStatus
    )
    VBI_TRACER_AVAILABLE = True
    logger.info("[WS] VBI Debug Tracer available")
except ImportError as e:
    VBI_TRACER_AVAILABLE = False
    logger.warning(f"[WS] VBI Debug Tracer not available: {e}")

# Import ROBUST voice unlock handler (v1.0.0) - guaranteed timeouts, never hangs
try:
    from voice_unlock.intelligent_voice_unlock_service import process_voice_unlock_robust
    ROBUST_UNLOCK_AVAILABLE = True
    logger.info("[WS] ✅ Robust Voice Unlock v1.0.0 available (timeout-protected, parallel)")
except ImportError as e:
    ROBUST_UNLOCK_AVAILABLE = False
    logger.warning(f"[WS] Robust Voice Unlock not available: {e}")

# Import streaming safeguard for command detection
try:
    from voice.streaming_safeguard import (
        StreamingSafeguard,
        CommandDetectionConfig,
        get_streaming_safeguard
    )
    STREAMING_SAFEGUARD_AVAILABLE = True
except ImportError as e:
    logger.warning(f"Streaming safeguard not available: {e}")
    STREAMING_SAFEGUARD_AVAILABLE = False

router = APIRouter()

# Active connections management
active_connections: Dict[str, WebSocket] = {}
connection_capabilities: Dict[str, Set[str]] = {}

# Per-connection streaming safeguards
connection_safeguards: Dict[str, StreamingSafeguard] = {}


# ============================================================================
# ADVANCED CONNECTION HEALTH & SELF-HEALING SYSTEM
# ============================================================================


class ConnectionState(Enum):
    """WebSocket connection states"""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    RECOVERING = "recovering"
    DISCONNECTED = "disconnected"


@dataclass
class ConnectionHealth:
    """
    Real-time health metrics for a WebSocket connection

    v126.0: Added robust connection state tracking to prevent
    sending messages to closed connections (EXC_GUARD fix)
    """

    client_id: str
    websocket: WebSocket
    state: ConnectionState = ConnectionState.HEALTHY
    connection_time: float = field(default_factory=time.time)
    last_message_time: float = field(default_factory=time.time)
    last_ping_time: float = field(default_factory=time.time)
    messages_sent: int = 0
    messages_received: int = 0
    errors: int = 0
    reconnections: int = 0
    health_score: float = 100.0
    latency_ms: float = 0.0
    last_error: Optional[str] = None
    recovery_attempts: int = 0

    # v126.0: Connection lifecycle tracking (prevents sending to closed connections)
    close_sent: bool = False  # True if close message has been sent
    close_received: bool = False  # True if close message has been received
    marked_for_removal: bool = False  # True if scheduled for cleanup
    last_send_error: Optional[str] = None  # Last send error message
    consecutive_send_failures: int = 0  # Count of consecutive send failures

    # Intelligence integration
    uae_context: Optional[Dict] = None
    sai_context: Optional[Dict] = None
    learned_patterns: List[str] = field(default_factory=list)


class UnifiedWebSocketManager:
    """
    Advanced WebSocket Manager with Self-Healing Intelligence

    Integrates with:
    - UAE (Unified Awareness Engine) for system-wide intelligence
    - SAI (Situational Awareness Intelligence) for context awareness
    - Learning Database for pattern recognition and prediction
    """

    def __init__(self):
        # Connection management
        self.connections: Dict[str, WebSocket] = {}
        self.connection_health: Dict[str, ConnectionHealth] = {}
        self.display_monitor = None  # Will be set by main.py

        # Initialize async pipeline for WebSocket operations
        self.pipeline = get_async_pipeline()
        self._register_pipeline_stages()

        # Intelligence integration (injected via dependency injection - no hardcoding)
        self.uae_engine = None  # Will be set by main.py
        self.sai_engine = None  # Will be set by main.py
        self.learning_db = None  # Will be set by main.py

        # Self-healing configuration (dynamic, loaded from environment or config)
        self.config = {
            "health_check_interval": float(os.getenv("WS_HEALTH_CHECK_INTERVAL", "10.0")),
            "message_timeout": float(os.getenv("WS_MESSAGE_TIMEOUT", "60.0")),
            "ping_interval": float(os.getenv("WS_PING_INTERVAL", "20.0")),
            "max_recovery_attempts": int(os.getenv("WS_MAX_RECOVERY_ATTEMPTS", "5")),
            "circuit_breaker_threshold": int(os.getenv("WS_CIRCUIT_BREAKER_THRESHOLD", "3")),
            "circuit_breaker_timeout": float(os.getenv("WS_CIRCUIT_BREAKER_TIMEOUT", "60.0")),
            "predictive_healing_enabled": os.getenv("WS_PREDICTIVE_HEALING", "true").lower() == "true",
            "auto_learning_enabled": os.getenv("WS_AUTO_LEARNING", "true").lower() == "true",
            "max_pattern_history": int(os.getenv("WS_MAX_PATTERN_HISTORY", "1000")),
        }

        # Circuit breaker state
        self.circuit_open = False
        self.circuit_failures = 0
        self.circuit_open_time: Optional[float] = None

        # Background tasks
        self.health_monitor_task: Optional[asyncio.Task] = None
        self.recovery_tasks: Dict[str, asyncio.Task] = {}
        self._shutdown_event = asyncio.Event()

        # Streaming safeguard (command detection for stream closure)
        self.safeguard_enabled = (
            STREAMING_SAFEGUARD_AVAILABLE and
            os.getenv('ENABLE_STREAMING_SAFEGUARD', 'true').lower() == 'true'
        )

        # Learning & patterns (using deque for bounded memory)
        self.disconnection_patterns: deque = deque(maxlen=self.config["max_pattern_history"])
        self.recovery_success_rate: Dict[str, float] = {}

        # Metrics & analytics
        self.metrics = {
            "total_connections": 0,
            "total_disconnections": 0,
            "total_messages_sent": 0,
            "total_messages_received": 0,
            "total_errors": 0,
            "total_recoveries": 0,
            "circuit_breaker_activations": 0,
            "uptime_start": time.time(),
        }

        # ROOT CAUSE FIX: Rate limiting (prevents connection flooding)
        # INCREASED from hardcoded 10/min to configurable 100/min default
        # For local development, this should be generous to avoid blocking legitimate reconnects
        self.connection_rate_limit = int(os.getenv("WS_CONNECTION_RATE_LIMIT", "100"))  # per minute
        self.connection_timestamps: deque = deque(maxlen=int(os.getenv("WS_CONNECTION_HISTORY_SIZE", "200")))

        # Message handlers (dynamically extensible)
        self.handlers = {
            # Voice/JARVIS handlers
            "command": self._handle_voice_command,
            "voice_command": self._handle_voice_command,
            "jarvis_command": self._handle_voice_command,
            # Vision handlers
            "vision_analyze": self._handle_vision_analyze,
            "vision_monitor": self._handle_vision_monitor,
            "workspace_analysis": self._handle_workspace_analysis,
            # Audio/ML handlers
            "ml_audio_stream": self._handle_ml_audio,
            "audio_error": self._handle_audio_error,
            # System handlers
            "model_status": self._handle_model_status,
            "network_status": self._handle_network_status,
            "notification": self._handle_notification,
            # General handlers
            "ping": self._handle_ping,
            "pong": self._handle_pong,
            "subscribe": self._handle_subscribe,
            "unsubscribe": self._handle_unsubscribe,
            "health_check": self._handle_health_check,
            "system_metrics": self._handle_system_metrics,
        }

        logger.info("[UNIFIED-WS] Advanced WebSocket Manager initialized")
        logger.info(
            "[UNIFIED-WS] Self-healing: ✅ | Circuit breaker: ✅ | Predictive healing: ✅ | "
            f"Streaming safeguard: {'✅' if self.safeguard_enabled else '❌'}"
        )

    def set_intelligence_engines(self, uae=None, sai=None, learning_db=None):
        """Inject intelligence engines (dependency injection - no hardcoding)"""
        self.uae_engine = uae
        self.sai_engine = sai
        self.learning_db = learning_db

        logger.info(
            f"[UNIFIED-WS] Intelligence engines set: UAE={'✅' if uae else '❌'}, SAI={'✅' if sai else '❌'}, Learning DB={'✅' if learning_db else '❌'}"
        )

    # =========================================================================
    # v126.0: ROBUST CONNECTION STATE CHECKING & SAFE SENDING
    # =========================================================================

    def _is_connection_open(self, client_id: str) -> bool:
        """
        v126.0: Check if a WebSocket connection is still open and sendable.

        This method prevents the "Cannot call 'send' once a close message
        has been sent" error by checking multiple layers of connection state.

        Returns:
            True if connection is open and messages can be sent
            False if connection is closed, closing, or in error state
        """
        # Check 1: Client still registered
        if client_id not in self.connections:
            return False

        # Check 2: Health tracking exists
        health = self.connection_health.get(client_id)
        if not health:
            return False

        # Check 3: Not marked for removal or close already sent
        if health.marked_for_removal or health.close_sent or health.close_received:
            return False

        # Check 4: Connection state is not disconnected
        if health.state == ConnectionState.DISCONNECTED:
            return False

        # Check 5: Too many consecutive failures indicates dead connection
        if health.consecutive_send_failures >= 3:
            return False

        # Check 6: WebSocket application state (Starlette-specific)
        websocket = self.connections.get(client_id)
        if websocket:
            try:
                # Starlette WebSocket has client_state and application_state
                # client_state: CONNECTING, CONNECTED, DISCONNECTED
                # application_state: CONNECTING, CONNECTED, DISCONNECTED
                from starlette.websockets import WebSocketState

                if hasattr(websocket, 'client_state'):
                    if websocket.client_state != WebSocketState.CONNECTED:
                        logger.debug(
                            f"[UNIFIED-WS] Connection {client_id} client_state={websocket.client_state}"
                        )
                        return False

                if hasattr(websocket, 'application_state'):
                    if websocket.application_state != WebSocketState.CONNECTED:
                        logger.debug(
                            f"[UNIFIED-WS] Connection {client_id} application_state={websocket.application_state}"
                        )
                        return False
            except (ImportError, AttributeError):
                # Fallback: assume open if we can't check state
                pass

        return True

    async def _safe_send_json(
        self,
        client_id: str,
        message: Dict[str, Any],
        mark_failed_on_error: bool = True
    ) -> bool:
        """
        v126.0: Safely send JSON message to a WebSocket connection.

        This method wraps all send operations with proper state checking
        and error handling to prevent sending to closed connections.

        Args:
            client_id: The client ID to send to
            message: The JSON message to send
            mark_failed_on_error: If True, mark connection for cleanup on error

        Returns:
            True if message was sent successfully
            False if send failed or connection was not open
        """
        # Pre-flight check: is connection open?
        if not self._is_connection_open(client_id):
            logger.debug(
                f"[UNIFIED-WS] Skipping send to {client_id}: connection not open"
            )
            return False

        websocket = self.connections.get(client_id)
        if not websocket:
            return False

        health = self.connection_health.get(client_id)

        try:
            await websocket.send_json(message)

            # Success: reset failure counter
            if health:
                health.consecutive_send_failures = 0
                health.messages_sent += 1
                health.last_message_time = time.time()

            return True

        except Exception as e:
            error_msg = str(e)

            # Detect specific close-related errors
            is_close_error = any(phrase in error_msg.lower() for phrase in [
                "close message",
                "connection closed",
                "websocket is closed",
                "websocket disconnected",
                "connection reset",
                "broken pipe",
            ])

            if health:
                health.consecutive_send_failures += 1
                health.last_send_error = error_msg
                health.errors += 1

                if is_close_error:
                    # Mark as closed to prevent future send attempts
                    health.close_sent = True
                    health.state = ConnectionState.DISCONNECTED

                    if mark_failed_on_error:
                        health.marked_for_removal = True

                    logger.debug(
                        f"[UNIFIED-WS] Connection {client_id} marked as closed: {error_msg}"
                    )
                else:
                    # Log other errors at error level
                    logger.error(
                        f"[UNIFIED-WS] Send failed to {client_id}: {error_msg}"
                    )

            return False

    async def _safe_send_to_health(
        self,
        health: ConnectionHealth,
        message: Dict[str, Any],
        mark_failed_on_error: bool = True
    ) -> bool:
        """
        v126.0: Safely send JSON message using ConnectionHealth object.

        Convenience wrapper for _safe_send_json using health object directly.
        """
        return await self._safe_send_json(
            health.client_id,
            message,
            mark_failed_on_error
        )

    async def _cleanup_dead_connections(self) -> int:
        """
        v126.0: Clean up connections marked for removal.

        Called periodically by health monitoring loop to remove dead connections.

        Returns:
            Number of connections cleaned up
        """
        dead_connections = [
            client_id
            for client_id, health in self.connection_health.items()
            if health.marked_for_removal or health.close_sent
        ]

        for client_id in dead_connections:
            try:
                await self.disconnect(client_id)
                logger.debug(f"[UNIFIED-WS] Cleaned up dead connection: {client_id}")
            except Exception as e:
                logger.warning(f"[UNIFIED-WS] Error cleaning up {client_id}: {e}")
                # Force removal from dictionaries
                self.connections.pop(client_id, None)
                self.connection_health.pop(client_id, None)
                connection_capabilities.pop(client_id, None)
                connection_safeguards.pop(client_id, None)

        return len(dead_connections)

    async def start_health_monitoring(self):
        """Start intelligent health monitoring"""
        if self.health_monitor_task is None:
            # v95.13: Register for global shutdown notification
            self._register_global_shutdown_callback()

            self.health_monitor_task = asyncio.create_task(self._health_monitoring_loop())
            logger.info("[UNIFIED-WS] 🏥 Health monitoring started")

    def _register_global_shutdown_callback(self):
        """v95.13: Register callback for global shutdown notification."""
        try:
            from backend.core.resilience.graceful_shutdown import register_async_shutdown_callback
            register_async_shutdown_callback(self._on_global_shutdown)
            logger.debug("[UNIFIED-WS] Registered for global shutdown notification")
        except ImportError:
            logger.debug("[UNIFIED-WS] Global shutdown not available, using local events only")

    async def _on_global_shutdown(self):
        """v95.13: Handle global shutdown notification."""
        logger.info("[UNIFIED-WS] 🛑 Received global shutdown signal")
        # Set local shutdown event to stop background tasks
        self._shutdown_event.set()
        # Trigger graceful shutdown
        await self.shutdown()

    async def stop_health_monitoring(self):
        """Stop health monitoring"""
        if self.health_monitor_task:
            self.health_monitor_task.cancel()
            try:
                await self.health_monitor_task
            except asyncio.CancelledError:
                pass
            self.health_monitor_task = None
            logger.info("[UNIFIED-WS] Health monitoring stopped")

    async def shutdown(self):
        """
        Graceful shutdown handler for resource cleanup

        Performs:
        - Stop health monitoring
        - Cancel all recovery tasks
        - Close all active connections
        - Log final metrics
        - Clear resources
        """
        logger.info("[UNIFIED-WS] 🛑 Starting graceful shutdown...")

        # Signal shutdown to prevent new connections
        self._shutdown_event.set()

        # Stop health monitoring
        await self.stop_health_monitoring()

        # Cancel all recovery tasks
        if self.recovery_tasks:
            logger.info(f"[UNIFIED-WS] Cancelling {len(self.recovery_tasks)} recovery tasks...")
            for task in self.recovery_tasks.values():
                task.cancel()

            # Wait for all to finish and retrieve exceptions to prevent warnings
            results = await asyncio.gather(*self.recovery_tasks.values(), return_exceptions=True)

            # Log any unexpected exceptions (CancelledError is expected)
            for i, result in enumerate(results):
                if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
                    logger.warning(f"[UNIFIED-WS] Recovery task {i} raised unexpected exception: {result}")

            self.recovery_tasks.clear()

        # Collect final metrics before disconnecting
        total_messages_sent = sum(h.messages_sent for h in self.connection_health.values())
        total_messages_received = sum(h.messages_received for h in self.connection_health.values())
        total_errors = sum(h.errors for h in self.connection_health.values())
        avg_health_score = (
            sum(h.health_score for h in self.connection_health.values()) / len(self.connection_health)
            if self.connection_health
            else 0
        )

        logger.info(
            f"[UNIFIED-WS] 📊 Final metrics: "
            f"{len(self.connections)} active connections, "
            f"{total_messages_sent} sent, "
            f"{total_messages_received} received, "
            f"{total_errors} errors, "
            f"avg health: {avg_health_score:.1f}"
        )

        # v126.0: Close all connections gracefully using safe sending
        if self.connections:
            logger.info(f"[UNIFIED-WS] Closing {len(self.connections)} active connections...")

            # Send shutdown notification to all clients
            shutdown_message = {
                "type": "system_shutdown",
                "message": "Server is shutting down gracefully",
                "timestamp": datetime.now().isoformat(),
            }

            for client_id in list(self.connections.keys()):
                # v126.0: Use safe sending to avoid close errors
                await self._safe_send_json(
                    client_id,
                    shutdown_message,
                    mark_failed_on_error=False  # Don't mark, we're closing anyway
                )

                # Disconnect (handles cleanup)
                try:
                    await self.disconnect(client_id)
                except Exception as e:
                    logger.debug(f"[UNIFIED-WS] Error disconnecting {client_id}: {e}")
                    # Force cleanup
                    self.connections.pop(client_id, None)
                    self.connection_health.pop(client_id, None)

        # Log final learning data
        if self.learning_db and self.disconnection_patterns:
            logger.info(
                f"[UNIFIED-WS] 🧠 Collected {len(self.disconnection_patterns)} connection patterns during session"
            )

        # Clear remaining data structures
        self.connection_health.clear()
        self.connections.clear()

        logger.info("[UNIFIED-WS] ✅ Graceful shutdown complete")

    async def _health_monitoring_loop(self):
        """
        v126.0: Continuous health monitoring with predictive healing and dead connection cleanup.

        Enhanced with:
        - Pre-check for connection state before processing
        - Skip connections marked for removal
        - Periodic cleanup of dead connections
        """
        # Maximum run time for health monitoring (default: 24 hours, configurable)
        max_run_time = float(os.getenv("TIMEOUT_HEALTH_MONITOR_MAX", "86400.0"))
        start_time = time.time()
        last_cleanup_time = time.time()
        cleanup_interval = float(os.getenv("WS_CLEANUP_INTERVAL", "30.0"))  # v126.0

        # v95.13: Helper to check both local and global shutdown
        def _is_shutting_down() -> bool:
            if self._shutdown_event.is_set():
                return True
            try:
                from backend.core.resilience.graceful_shutdown import is_global_shutdown_initiated
                return is_global_shutdown_initiated()
            except ImportError:
                return False

        while not _is_shutting_down():
            try:
                # Check if we've exceeded maximum run time
                if time.time() - start_time > max_run_time:
                    logger.info("[UNIFIED-WS] Health monitoring max run time reached, restarting")
                    break

                await asyncio.sleep(self.config["health_check_interval"])

                current_time = time.time()

                # v126.0: Periodic cleanup of dead connections
                if current_time - last_cleanup_time > cleanup_interval:
                    cleaned = await self._cleanup_dead_connections()
                    if cleaned > 0:
                        logger.info(f"[UNIFIED-WS] 🧹 Cleaned up {cleaned} dead connections")
                    last_cleanup_time = current_time

                for client_id, health in list(self.connection_health.items()):
                    # v126.0: Skip connections marked for removal or already closed
                    if health.marked_for_removal or health.close_sent:
                        continue

                    # v126.0: Pre-check if connection is still open
                    if not self._is_connection_open(client_id):
                        health.marked_for_removal = True
                        continue

                    # Check message timeout
                    time_since_message = current_time - health.last_message_time

                    # Log health check details every 30s for debugging
                    if int(current_time) % 30 == 0:
                        logger.debug(
                            f"[UNIFIED-WS] Health check: {client_id} | "
                            f"state={health.state.value} | "
                            f"score={health.health_score:.1f} | "
                            f"time_since_msg={time_since_message:.1f}s | "
                            f"latency={health.latency_ms:.1f}ms | "
                            f"send_failures={health.consecutive_send_failures}"
                        )

                    if time_since_message > self.config["message_timeout"]:
                        # Degraded connection
                        if health.state == ConnectionState.HEALTHY:
                            health.state = ConnectionState.DEGRADED
                            health.health_score = max(0, health.health_score - 20)
                            logger.warning(
                                f"[UNIFIED-WS] ⚠️ Connection {client_id} DEGRADED | "
                                f"No messages for {time_since_message:.1f}s (timeout: {self.config['message_timeout']}s) | "
                                f"Last ping: {current_time - health.last_ping_time:.1f}s ago | "
                                f"Health score: {health.health_score:.1f}"
                            )

                            # Notify SAI of degradation
                            await self._notify_sai("connection_degraded", health)

                            # Attempt preventive recovery
                            await self._preventive_recovery(health)

                    # Send periodic pings (v126.0: uses safe sending internally)
                    time_since_ping = current_time - health.last_ping_time
                    if time_since_ping > self.config["ping_interval"]:
                        await self._send_ping(health)

                    # Predictive healing (UAE-powered)
                    if self.config["predictive_healing_enabled"]:
                        if self.uae_engine:
                            await self._predictive_healing(health)
                        else:
                            # Log once per health check cycle if UAE not available
                            if int(current_time) % 300 == 0:  # Every 5 minutes
                                logger.debug(
                                    "[UNIFIED-WS] Predictive healing enabled but UAE engine not available"
                                )

                # Check circuit breaker
                await self._check_circuit_breaker()

            except Exception as e:
                logger.error(f"[UNIFIED-WS] Health monitoring error: {e}", exc_info=True)

    async def _send_ping(self, health: ConnectionHealth):
        """
        v126.0: Send ping to check connection health with safe sending.

        Uses _safe_send_to_health to prevent sending to closed connections.
        Marks connection for removal on consecutive failures.
        """
        # v126.0: Pre-flight check - don't ping dead connections
        if not self._is_connection_open(health.client_id):
            logger.debug(
                f"[UNIFIED-WS] Skipping ping to {health.client_id}: connection not open"
            )
            # Mark for cleanup if not already
            if not health.marked_for_removal:
                health.marked_for_removal = True
            return

        ping_time = time.time()
        ping_message = {"type": "ping", "timestamp": ping_time}

        # v126.0: Use safe send to prevent close errors
        success = await self._safe_send_to_health(
            health,
            ping_message,
            mark_failed_on_error=True
        )

        if success:
            health.last_ping_time = ping_time
            logger.debug(
                f"[UNIFIED-WS] 🏓 Sent ping to {health.client_id} | "
                f"Health: {health.health_score:.1f} | State: {health.state.value}"
            )
        else:
            # Safe send already logged and marked the connection
            health.health_score = max(0, health.health_score - 10)
            logger.debug(
                f"[UNIFIED-WS] Ping failed for {health.client_id} | "
                f"Failures: {health.consecutive_send_failures} | "
                f"Marked for removal: {health.marked_for_removal}"
            )

    async def _preventive_recovery(self, health: ConnectionHealth):
        """
        v126.0: Attempt preventive recovery before full disconnection.

        Uses safe sending to prevent errors on closed connections.
        """
        # v126.0: Early exit if connection is already dead
        if not self._is_connection_open(health.client_id):
            logger.debug(
                f"[UNIFIED-WS] Skipping recovery for {health.client_id}: connection not open"
            )
            health.state = ConnectionState.DISCONNECTED
            health.marked_for_removal = True
            return

        if health.recovery_attempts >= self.config["max_recovery_attempts"]:
            logger.warning(f"[UNIFIED-WS] Max recovery attempts reached for {health.client_id}")
            health.state = ConnectionState.DISCONNECTED
            health.marked_for_removal = True
            return

        try:
            health.state = ConnectionState.RECOVERING
            health.recovery_attempts += 1

            logger.info(
                f"[UNIFIED-WS] Attempting preventive recovery for {health.client_id} (attempt {health.recovery_attempts})"
            )

            # Strategy 1: Send wake-up ping (uses safe sending)
            await self._send_ping(health)

            # Strategy 2: Notify client of degradation (uses safe sending)
            recovery_msg = {
                "type": "connection_health",
                "state": "degraded",
                "health_score": health.health_score,
                "message": "Connection health degraded, attempting recovery",
            }
            await self._safe_send_to_health(health, recovery_msg, mark_failed_on_error=False)

            # Strategy 3: Log pattern to learning database
            if self.config["auto_learning_enabled"] and self.learning_db:
                await self._log_connection_pattern(health, "preventive_recovery")

            # Wait a bit and check if recovery worked
            await asyncio.sleep(2)

            # v126.0: Check if connection died during recovery
            if health.marked_for_removal or health.close_sent:
                logger.debug(f"[UNIFIED-WS] Connection {health.client_id} closed during recovery")
                health.state = ConnectionState.DISCONNECTED
                return

            if health.state == ConnectionState.RECOVERING:
                # If we received a message during recovery, it worked
                current_time = time.time()
                if current_time - health.last_message_time < 3:
                    health.state = ConnectionState.HEALTHY
                    health.health_score = min(100, health.health_score + 30)
                    health.consecutive_send_failures = 0  # v126.0: Reset failure counter
                    self.metrics["total_recoveries"] += 1
                    logger.info(f"[UNIFIED-WS] ✅ Recovery successful for {health.client_id}")

                    # Notify SAI of recovery
                    await self._notify_sai("connection_recovered", health)
                else:
                    # Recovery didn't work
                    health.health_score = max(0, health.health_score - 15)

        except Exception as e:
            logger.error(f"[UNIFIED-WS] Preventive recovery failed for {health.client_id}: {e}")
            health.errors += 1
            health.health_score = max(0, health.health_score - 20)

    async def _predictive_healing(self, health: ConnectionHealth):
        """
        v126.0: UAE-powered predictive healing to prevent disconnections.

        Uses safe sending to prevent errors on closed connections.
        """
        if not self.uae_engine:
            return

        # v126.0: Skip if connection is already dead
        if not self._is_connection_open(health.client_id):
            return

        try:
            # Gather connection metrics
            metrics = {
                "client_id": health.client_id,
                "health_score": health.health_score,
                "latency_ms": health.latency_ms,
                "messages_sent": health.messages_sent,
                "messages_received": health.messages_received,
                "errors": health.errors,
                "reconnections": health.reconnections,
                "connection_duration": time.time() - health.connection_time,
                "time_since_message": time.time() - health.last_message_time,
                "consecutive_send_failures": health.consecutive_send_failures,  # v126.0
            }

            # Ask UAE to predict disconnection risk
            prediction = await self._ask_uae_prediction(metrics)

            if prediction and prediction.get("risk_level", "low") in ["high", "critical"]:
                logger.warning(
                    f"[UNIFIED-WS] 🔮 UAE predicts disconnection risk: {prediction.get('risk_level')} for {health.client_id}"
                )

                # Apply UAE-suggested recovery strategy
                strategy = prediction.get("suggested_strategy", "ping")

                if strategy == "immediate_reconnect":
                    await self._notify_uae("immediate_reconnect_needed", health)
                    # v126.0: Use safe sending for reconnection advisory
                    await self._safe_send_to_health(
                        health,
                        {
                            "type": "reconnection_advisory",
                            "reason": "predictive_healing",
                            "message": "Connection instability detected, please standby",
                        },
                        mark_failed_on_error=True
                    )
                elif strategy == "increase_pings":
                    # Temporarily increase ping frequency
                    self.config["ping_interval"] = max(5.0, self.config["ping_interval"] / 2)
                    logger.info(
                        f"[UNIFIED-WS] Increased ping frequency to {self.config['ping_interval']}s"
                    )
                elif strategy == "reduce_load":
                    # v126.0: Use safe sending for optimization message
                    await self._safe_send_to_health(
                        health,
                        {
                            "type": "connection_optimization",
                            "action": "reduce_load",
                            "message": "Optimizing connection performance",
                        },
                        mark_failed_on_error=False
                    )

                # Log prediction to learning database
                if self.learning_db:
                    await self._log_uae_prediction(health, prediction)

        except Exception as e:
            logger.error(f"[UNIFIED-WS] Predictive healing error for {health.client_id}: {e}")

    async def _check_circuit_breaker(self):
        """Manage circuit breaker state for system-wide resilience"""
        current_time = time.time()

        if self.circuit_open:
            # Check if timeout has passed
            if (
                self.circuit_open_time
                and (current_time - self.circuit_open_time) > self.config["circuit_breaker_timeout"]
            ):
                # Try half-open state
                logger.info("[UNIFIED-WS] Circuit breaker entering half-open state")
                self.circuit_open = False
                self.circuit_failures = 0
                self.circuit_open_time = None

                # Notify SAI of circuit recovery
                await self._notify_sai("circuit_breaker_half_open", None)
        else:
            # Check if we should open the circuit
            if self.circuit_failures >= self.config["circuit_breaker_threshold"]:
                logger.error(
                    f"[UNIFIED-WS] 🔴 Circuit breaker OPEN (failures: {self.circuit_failures})"
                )
                self.circuit_open = True
                self.circuit_open_time = current_time
                self.metrics["circuit_breaker_activations"] += 1

                # Notify all clients
                await self.broadcast(
                    {
                        "type": "system_status",
                        "status": "degraded",
                        "message": "System experiencing high failure rate, entering protective mode",
                    }
                )

                # Notify SAI
                await self._notify_sai("circuit_breaker_open", None)

    async def _notify_sai(self, event: str, health: Optional[ConnectionHealth]):
        """Notify SAI of connection events for situational awareness"""
        if not self.sai_engine:
            logger.debug(f"[UNIFIED-WS] SAI notification skipped (engine not available): {event}")
            return

        try:
            event_data = {"event": event, "timestamp": time.time(), "source": "unified_websocket"}

            if health:
                event_data["client_id"] = health.client_id
                event_data["health_score"] = health.health_score
                event_data["connection_state"] = health.state.value
                event_data["latency_ms"] = health.latency_ms

            # Call SAI's event notification method
            if hasattr(self.sai_engine, "notify_event"):
                await self.sai_engine.notify_event(event_data)
                logger.debug(f"[UNIFIED-WS] Notified SAI of event: {event}")

        except Exception as e:
            logger.error(f"[UNIFIED-WS] Failed to notify SAI: {e}")

    async def _notify_uae(self, event: str, health: ConnectionHealth):
        """Notify UAE of critical connection events"""
        if not self.uae_engine:
            return

        try:
            event_data = {
                "event": event,
                "timestamp": time.time(),
                "client_id": health.client_id,
                "health_metrics": {
                    "score": health.health_score,
                    "latency": health.latency_ms,
                    "errors": health.errors,
                    "state": health.state.value,
                },
            }

            # Call UAE's notification method
            if hasattr(self.uae_engine, "notify_websocket_event"):
                await self.uae_engine.notify_websocket_event(event_data)
                logger.debug(f"[UNIFIED-WS] Notified UAE of event: {event}")

        except Exception as e:
            logger.error(f"[UNIFIED-WS] Failed to notify UAE: {e}")

    async def _handle_pong(self, client_id: str, message: Dict[str, Any]) -> Dict[str, Any]:
        """Handle pong responses to update latency metrics"""
        if client_id in self.connection_health:
            health = self.connection_health[client_id]

            # Calculate latency
            pong_time = time.time()
            ping_timestamp = message.get("timestamp", health.last_ping_time)
            health.latency_ms = (pong_time - ping_timestamp) * 1000

            # Update health score based on latency
            if health.latency_ms < 100:
                health.health_score = min(100, health.health_score + 2)
            elif health.latency_ms > 500:
                health.health_score = max(0, health.health_score - 5)

            # Update state if recovering
            if health.state == ConnectionState.DEGRADED and health.health_score > 80:
                health.state = ConnectionState.HEALTHY
                logger.info(f"[UNIFIED-WS] Connection {client_id} restored to healthy state")

            logger.debug(
                f"[UNIFIED-WS] Pong from {client_id}: latency={health.latency_ms:.1f}ms, health={health.health_score:.1f}"
            )

        return {
            "type": "pong_ack",
            "latency_ms": (
                self.connection_health[client_id].latency_ms
                if client_id in self.connection_health
                else 0
            ),
        }

    async def _handle_health_check(self, client_id: str, _message: Dict[str, Any]) -> Dict[str, Any]:
        """Handle explicit health check requests"""
        if client_id in self.connection_health:
            health = self.connection_health[client_id]

            return {
                "type": "health_status",
                "client_id": client_id,
                "state": health.state.value,
                "health_score": health.health_score,
                "latency_ms": health.latency_ms,
                "connection_duration": time.time() - health.connection_time,
                "messages_sent": health.messages_sent,
                "messages_received": health.messages_received,
                "errors": health.errors,
            }

        return {"type": "health_status", "error": "Client health data not found"}

    async def _handle_system_metrics(self, _client_id: str, _message: Dict[str, Any]) -> Dict[str, Any]:
        """Handle system metrics requests"""
        metrics = self.get_system_metrics()
        return {"type": "system_metrics", "metrics": metrics, "timestamp": datetime.now().isoformat()}

    async def _ask_uae_prediction(self, metrics: Dict) -> Optional[Dict]:
        """Ask UAE to predict disconnection risk"""
        if not self.uae_engine or not hasattr(self.uae_engine, "predict_connection_risk"):
            return None

        try:
            prediction = await self.uae_engine.predict_connection_risk(metrics)
            return prediction
        except Exception as e:
            logger.error(f"[UNIFIED-WS] UAE prediction failed: {e}")
            return None

    async def _log_connection_pattern(self, health: ConnectionHealth, event_type: str):
        """Log connection patterns to learning database"""
        if not self.learning_db:
            return

        try:
            pattern = {
                "timestamp": time.time(),
                "client_id": health.client_id,
                "event_type": event_type,
                "health_score": health.health_score,
                "latency_ms": health.latency_ms,
                "state": health.state.value,
                "errors": health.errors,
                "recovery_attempts": health.recovery_attempts,
            }

            # Store in learning database
            if hasattr(self.learning_db, "log_websocket_pattern"):
                await self.learning_db.log_websocket_pattern(pattern)
                logger.debug(f"[UNIFIED-WS] Logged connection pattern: {event_type}")

        except Exception as e:
            logger.error(f"[UNIFIED-WS] Failed to log pattern: {e}")

    async def _log_uae_prediction(self, health: ConnectionHealth, prediction: Dict):
        """Log UAE predictions for learning"""
        if not self.learning_db:
            return

        try:
            log_entry = {
                "timestamp": time.time(),
                "client_id": health.client_id,
                "prediction": prediction,
                "actual_state": health.state.value,
                "health_score": health.health_score,
            }

            if hasattr(self.learning_db, "log_uae_prediction"):
                await self.learning_db.log_uae_prediction(log_entry)

        except Exception as e:
            logger.error(f"[UNIFIED-WS] Failed to log UAE prediction: {e}")

    def _register_pipeline_stages(self):
        """Register async pipeline stages for WebSocket operations"""

        # Message processing stage
        self.pipeline.register_stage(
            "message_processing",
            self._process_message_async,
            timeout=60.0,  # Increased from 30s for multi-space vision queries
            retry_count=1,
            required=True,
        )

        # Command execution stage
        self.pipeline.register_stage(
            "command_execution",
            self._execute_command_async,
            timeout=90.0,  # Increased from 45s for complex vision processing
            retry_count=2,
            required=True,
        )

        # Response streaming stage
        self.pipeline.register_stage(
            "response_streaming",
            self._stream_response_async,
            timeout=60.0,
            retry_count=0,
            required=False,  # Optional for non-streaming responses
        )

    async def _process_message_async(self, context):
        """Non-blocking message processing via async pipeline"""
        try:
            message = context.metadata.get("message", {})

            # Parse message type
            msg_type = message.get("type", "")
            context.metadata["msg_type"] = msg_type

            # Validate message
            if not msg_type:
                context.metadata["error"] = "Missing message type"
                return

            # Store for next stage
            context.metadata["validated"] = True

        except Exception as e:
            logger.error(f"Message processing error: {e}")
            context.metadata["error"] = str(e)

    async def _execute_command_async(self, context):
        """Non-blocking command execution via async pipeline"""
        try:
            message = context.metadata.get("message", {})
            msg_type = context.metadata.get("msg_type", "")

            # Route to appropriate handler
            if msg_type == "command" or msg_type == "voice_command":
                # Execute voice command
                from .jarvis_voice_api import JARVISCommand, jarvis_api

                command_text = message.get("command", message.get("text", ""))
                # Get optional audio data if provided
                audio_data = message.get("audio_data")

                # Create properly typed command object
                command_obj = JARVISCommand(text=command_text, audio_data=audio_data)

                result = await jarvis_api.process_command(command_obj)

                context.metadata["response"] = {
                    "type": "response",
                    "text": result.get("response", ""),
                    "status": result.get("status", "success"),
                    "command_type": result.get("command_type", "unknown"),
                    "speak": True,
                }

            elif msg_type == "vision_analyze":
                # Execute vision analysis
                context.metadata["response"] = await self._execute_vision_analysis(message)

            else:
                context.metadata["response"] = {
                    "type": "error",
                    "error": f"Unknown message type: {msg_type}",
                }

        except Exception as e:
            logger.error(f"Command execution error: {e}")
            context.metadata["error"] = str(e)
            context.metadata["response"] = {"type": "error", "error": str(e)}

    async def _stream_response_async(self, context):
        """Non-blocking response streaming via async pipeline"""
        try:
            websocket = context.metadata.get("websocket")
            response = context.metadata.get("response", {})
            stream_mode = context.metadata.get("stream_mode", False)

            if stream_mode and websocket:
                # Stream response in chunks
                response_text = response.get("text", "")
                chunk_size = 50

                for i in range(0, len(response_text), chunk_size):
                    chunk = response_text[i : i + chunk_size]
                    await websocket.send_json(
                        {
                            "type": "stream_chunk",
                            "chunk": chunk,
                            "progress": (i + chunk_size) / len(response_text),
                        }
                    )
                    await asyncio.sleep(0.05)

                # Send completion
                await websocket.send_json(
                    {"type": "stream_complete", "message": "Streaming complete"}
                )
            else:
                # Send complete response
                if websocket and response:
                    await websocket.send_json(response)

            context.metadata["sent"] = True

        except Exception as e:
            logger.error(f"Response streaming error: {e}")
            context.metadata["error"] = str(e)

    async def _execute_vision_analysis(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """Execute vision analysis (helper for command execution)"""
        try:
            from ..main import app

            if hasattr(app.state, "vision_analyzer"):
                analyzer = app.state.vision_analyzer

                screenshot = await analyzer.capture_screen()
                if screenshot:
                    query = message.get("query", "Describe what you see on screen")
                    result = await analyzer.describe_screen(
                        {"screenshot": screenshot, "query": query}
                    )

                    return {
                        "type": "vision_result",
                        "success": result.get("success", False),
                        "description": result.get("description", ""),
                        "timestamp": datetime.now().isoformat(),
                    }

            return {
                "type": "vision_result",
                "success": False,
                "error": "Vision analyzer not available",
            }

        except Exception as e:
            logger.error(f"Vision analysis error: {e}")
            return {"type": "vision_result", "success": False, "error": str(e)}

    async def connect(self, websocket: WebSocket, client_id: str):
        """
        Accept new WebSocket connection with health monitoring and rate limiting.

        ROOT CAUSE FIX v2.0.0:
        - Raises WebSocketDisconnect if connection is rejected (not silent return!)
        - This prevents calling code from trying to use un-accepted websocket
        - Increased rate limit default from 10/min to 100/min
        - All limits configurable via environment variables

        Raises:
            WebSocketDisconnect: If connection is rejected due to shutdown or rate limit
        """
        # v95.13: Check global shutdown signal first (cross-component coordination)
        try:
            from backend.core.resilience.graceful_shutdown import is_global_shutdown_initiated
            if is_global_shutdown_initiated():
                logger.warning(f"[UNIFIED-WS] Rejecting connection from {client_id} - global shutdown in progress")
                await websocket.close(code=1001, reason="Server shutting down")
                raise WebSocketDisconnect(code=1001, reason="Global shutdown in progress")
        except ImportError:
            pass  # Graceful shutdown not available, continue with local check

        # Check if local shutting down
        if self._shutdown_event.is_set():
            logger.warning(f"[UNIFIED-WS] Rejecting connection from {client_id} - system is shutting down")
            await websocket.close(code=1001, reason="Server shutting down")
            # ROOT CAUSE FIX: Raise exception instead of silent return!
            raise WebSocketDisconnect(code=1001, reason="Server shutting down")

        # Rate limiting check
        current_time = time.time()
        self.connection_timestamps.append(current_time)

        # ROOT CAUSE FIX: Configurable rate limit window (not hardcoded 60s)
        rate_limit_window = float(os.getenv("WS_RATE_LIMIT_WINDOW_SECONDS", "60"))
        recent_connections = sum(1 for ts in self.connection_timestamps if current_time - ts < rate_limit_window)

        if recent_connections > self.connection_rate_limit:
            logger.warning(
                f"[UNIFIED-WS] ⚠️ Rate limit exceeded: {recent_connections}/{self.connection_rate_limit} per {rate_limit_window}s"
            )
            logger.info(
                f"[UNIFIED-WS] Tip: Increase WS_CONNECTION_RATE_LIMIT (currently {self.connection_rate_limit}) "
                f"if this is blocking legitimate connections"
            )
            await websocket.close(code=1008, reason="Rate limit exceeded")
            # ROOT CAUSE FIX: Raise exception instead of silent return!
            raise WebSocketDisconnect(code=1008, reason="Rate limit exceeded")

        await websocket.accept()
        self.connections[client_id] = websocket
        connection_capabilities[client_id] = set()

        # Initialize streaming safeguard for this connection
        if self.safeguard_enabled:
            try:
                safeguard = get_streaming_safeguard()
                safeguard.reset()  # Reset for new session
                connection_safeguards[client_id] = safeguard

                logger.debug(f"[UNIFIED-WS] Streaming safeguard initialized for {client_id}")
            except Exception as e:
                logger.error(f"[UNIFIED-WS] Failed to initialize safeguard for {client_id}: {e}")

        # Update metrics
        self.metrics["total_connections"] += 1

        # Create health monitoring for this connection
        health = ConnectionHealth(client_id=client_id, websocket=websocket)
        self.connection_health[client_id] = health

        logger.info(f"[UNIFIED-WS] ✅ Client {client_id} connected (health monitoring: active)")

        # Start health monitoring if not already running
        if not self.health_monitor_task:
            await self.start_health_monitoring()

        # Notify SAI of new connection
        await self._notify_sai("connection_established", health)

        # Log connection to learning database
        if self.learning_db:
            await self._log_connection_pattern(health, "connection_established")

        # Send welcome message with health features
        await websocket.send_json(
            {
                "type": "connection_established",
                "client_id": client_id,
                "timestamp": datetime.now().isoformat(),
                "available_handlers": list(self.handlers.keys()),
                "features": {
                    "self_healing": True,
                    "predictive_healing": self.config["predictive_healing_enabled"],
                    "health_monitoring": True,
                    "circuit_breaker": True,
                },
            }
        )

        # Send current display status if display monitor is available
        if self.display_monitor:
            try:
                available_displays = self.display_monitor.get_available_display_details()
                # Type guard: ensure we have a list before iterating
                if not available_displays:
                    return
                if not isinstance(available_displays, list):
                    return

                logger.info(
                    f"[WS] Sending {len(available_displays)} available displays to new client"
                )
                for display in available_displays:  # type: ignore[misc]
                    await websocket.send_json(
                        {
                            "type": "display_detected",
                            "display_name": display["display_name"],
                            "display_id": display["display_id"],
                            "message": display["message"],
                            "timestamp": datetime.now().isoformat(),
                            "on_connect": True,  # Flag to indicate this is initial status
                        }
                    )
            except Exception as e:
                logger.warning(f"[WS] Failed to send display status to new client: {e}")

    async def disconnect(self, client_id: str):
        """Remove WebSocket connection with learning and SAI notification"""
        # Gather final health metrics before removal
        health = self.connection_health.get(client_id)

        if health:
            # Log disconnection pattern to learning database
            if self.learning_db:
                await self._log_connection_pattern(health, "disconnection")

                # Store final session metrics for learning
                session_summary = {
                    "client_id": client_id,
                    "connection_duration": time.time() - health.connection_time,
                    "total_messages": health.messages_sent + health.messages_received,
                    "errors": health.errors,
                    "reconnections": health.reconnections,
                    "final_health_score": health.health_score,
                    "avg_latency_ms": health.latency_ms,
                    "learned_patterns": health.learned_patterns,
                }

                if hasattr(self.learning_db, "log_session_summary"):
                    await self.learning_db.log_session_summary(session_summary)

            # Notify SAI of disconnection
            await self._notify_sai("connection_disconnected", health)

            # Update global metrics
            self.metrics["total_disconnections"] += 1
            self.metrics["total_messages_sent"] += health.messages_sent
            self.metrics["total_messages_received"] += health.messages_received
            self.metrics["total_errors"] += health.errors

            logger.info(
                f"[UNIFIED-WS] Client {client_id} disconnected (duration: {time.time() - health.connection_time:.1f}s, health: {health.health_score:.1f})"
            )

        # Clean up
        if client_id in self.connections:
            del self.connections[client_id]
        if client_id in connection_capabilities:
            del connection_capabilities[client_id]
        if client_id in self.connection_health:
            del self.connection_health[client_id]
        if client_id in self.recovery_tasks:
            # Cancel any ongoing recovery tasks
            self.recovery_tasks[client_id].cancel()
            del self.recovery_tasks[client_id]
        if client_id in connection_safeguards:
            del connection_safeguards[client_id]

    async def handle_message(self, client_id: str, message: Dict[str, Any]) -> Dict[str, Any]:
        """
        Route message to appropriate handler via async pipeline

        Args:
            client_id: Client identifier
            message: Message to process

        Returns:
            Response dictionary
        """
        msg_type = message.get("type", "")

        # =========================================================================
        # 🔇 SELF-VOICE SUPPRESSION - Prevent JARVIS from hearing its own voice
        # =========================================================================
        # This is the ROOT LEVEL check - we reject audio messages that arrive:
        # 1. While JARVIS is speaking (prevents hearing its own voice)
        # 2. While a VBI session is active (prevents processing during unlock)
        #
        # The check happens HERE (at WebSocket receive) because:
        # 1. This is before ANY audio processing
        # 2. This catches ALL audio messages regardless of type
        # 3. This prevents wasted compute on self-voice audio
        # =========================================================================
        has_audio = message.get("audio_data") is not None
        if has_audio:
            # Check 1: Is a VBI session active? (Blocks ALL audio during unlock)
            try:
                from voice_unlock.intelligent_voice_unlock_service import is_vbi_session_active
                if is_vbi_session_active():
                    logger.warning(
                        f"🔇 [VBI-SESSION-BLOCK] Rejecting audio - VBI session is active"
                    )
                    return {
                        "success": False,
                        "type": "vbi_session_active",
                        "message": "Audio rejected - VBI unlock in progress",
                        "should_retry": False
                    }
            except Exception as e:
                logger.debug(f"[VBI-SESSION] Check failed: {e}")

            # Check 2: Is JARVIS speaking?
            try:
                from agi_os.realtime_voice_communicator import get_voice_communicator
                voice_comm = await asyncio.wait_for(get_voice_communicator(), timeout=0.3)

                if voice_comm and (voice_comm.is_speaking or voice_comm.is_processing_speech):
                    # JARVIS is currently speaking - this audio is likely echo
                    logger.warning(
                        f"🔇 [SELF-VOICE-SUPPRESSION] Rejecting audio message - "
                        f"JARVIS is speaking (is_speaking={voice_comm.is_speaking}, "
                        f"is_processing={voice_comm.is_processing_speech})"
                    )
                    return {
                        "success": False,
                        "type": "self_voice_suppressed",
                        "message": "Audio rejected - JARVIS is currently speaking",
                        "should_retry": False
                    }
            except asyncio.TimeoutError:
                logger.debug("[SELF-VOICE] Voice communicator check timed out")
            except Exception as e:
                logger.debug(f"[SELF-VOICE] Check failed: {e}")

        # Check if message type should use pipeline processing
        pipeline_types = {
            "command",
            "voice_command",
            "jarvis_command",
            "vision_analyze",
            "vision_monitor",
        }

        if msg_type in pipeline_types:
            try:
                # Process through async pipeline for non-blocking execution
                websocket = self.connections.get(client_id)

                # Debug: Log audio data from frontend
                audio_data_received = message.get("audio_data")
                sample_rate_received = message.get("sample_rate")
                mime_type_received = message.get("mime_type")
                audio_source_received = message.get("audio_source", "unknown")  # Track where audio came from

                if audio_data_received:
                    logger.info(
                        f"[WS] Received audio data from frontend: {len(audio_data_received)} bytes, "
                        f"sample_rate={sample_rate_received}Hz, mime_type={mime_type_received}, "
                        f"source={audio_source_received}"
                    )
                    # Continuous buffer audio is always high quality - log this for debugging
                    if audio_source_received == "continuous_buffer":
                        logger.info(
                            f"[WS] Using continuous buffer audio (pre-captured, zero-gap)"
                        )
                else:
                    logger.debug("[WS] No audio data in message from frontend (text command)")

                # For command types, process directly through JARVIS API
                # This bypasses the pipeline stages which aren't properly integrated
                if msg_type in ("command", "voice_command", "jarvis_command"):
                    # Enhanced VBI Debug Logging
                    command_start_time = time.time()
                    command_text = message.get("text", message.get("command", ""))

                    logger.info("=" * 70)
                    logger.info(f"[WS-VBI] COMMAND RECEIVED")
                    logger.info("=" * 70)
                    logger.info(f"   Type: {msg_type}")
                    logger.info(f"   Text: '{command_text}'")
                    logger.info(f"   Client: {client_id}")
                    logger.info(f"   Audio Data: {'YES' if audio_data_received else 'NO'}")
                    if audio_data_received:
                        logger.info(f"   Audio Size: {len(audio_data_received)} bytes")
                        logger.info(f"   Sample Rate: {sample_rate_received}Hz")
                        logger.info(f"   MIME Type: {mime_type_received}")
                        logger.info(f"   Audio Source: {audio_source_received}")
                    logger.info("=" * 70)

                    # Check if this is a voice UNLOCK command (requires VBI verification)
                    # CRITICAL: "lock my screen" is NOT an unlock command - it's a LOCK command!
                    # Only UNLOCK commands need voice biometric verification
                    # LOCK commands should go through standard JARVIS API (no VBI needed)
                    command_lower = command_text.lower()
                    
                    # First check if it's a LOCK command (has "lock" but NOT "unlock")
                    is_lock_command = (
                        "lock" in command_lower and 
                        "unlock" not in command_lower and
                        ("screen" in command_lower or "mac" in command_lower or "computer" in command_lower)
                    )
                    
                    # UNLOCK command detection (only if NOT a lock command)
                    is_unlock_command = not is_lock_command and (
                        "unlock" in command_lower or
                        any(
                            keyword in command_lower
                            for keyword in ["screen unlock", "voice unlock", "let me in"]
                        )
                    )
                    
                    # Log the command classification
                    if is_lock_command:
                        logger.info(f"[WS] 🔒 Detected LOCK command: '{command_text}' - routing to standard API")
                    elif is_unlock_command:
                        logger.info(f"[WS] 🔓 Detected UNLOCK command: '{command_text}' - routing to VBI")

                    # Initialize result to None before processing branches
                    result = None

                    if is_unlock_command and audio_data_received:
                        # ============================================================
                        # ROBUST VOICE UNLOCK v1.0.0 - Primary Handler
                        # ============================================================
                        # Uses timeout-protected parallel processing that NEVER hangs.
                        # Falls back to legacy VBI pipeline only if robust handler fails.
                        # ============================================================

                        if ROBUST_UNLOCK_AVAILABLE:
                            logger.info(f"[WS-ROBUST] Using ROBUST Voice Unlock (timeout-protected)")
                            try:
                                # Create progress callback for WebSocket updates
                                async def robust_progress_callback(progress_data: dict):
                                    try:
                                        if websocket:
                                            normalized = {
                                                "type": "vbi_progress",
                                                "stage": progress_data.get("stage", "unknown"),
                                                "progress": progress_data.get("progress", 0),
                                                "message": progress_data.get("message", ""),
                                                "timestamp": progress_data.get("timestamp", time.time()),
                                            }
                                            await websocket.send_json(normalized)
                                    except Exception as ws_err:
                                        logger.debug(f"[WS-ROBUST] Progress send error: {ws_err}")

                                # Call robust handler with 15s max timeout
                                robust_result = await process_voice_unlock_robust(
                                    command=command_text,
                                    audio_data=audio_data_received,
                                    sample_rate=sample_rate_received or 16000,
                                    mime_type=mime_type_received or "audio/webm",
                                    progress_callback=robust_progress_callback
                                )

                                result = {
                                    "type": "voice_unlock",
                                    "response": robust_result.get("response", ""),
                                    "message": robust_result.get("response", ""),
                                    "success": robust_result.get("success", False),
                                    "command_type": "voice_unlock",
                                    "speaker_name": robust_result.get("speaker_name", ""),
                                    "confidence": robust_result.get("confidence", 0.0),
                                    "trace_id": robust_result.get("trace_id", ""),
                                    "handler": "robust_v1",
                                    "metadata": robust_result
                                }

                                logger.info(f"[WS-ROBUST] Result: success={result['success']}, "
                                           f"confidence={result['confidence']:.1%}, "
                                           f"duration={robust_result.get('total_duration_ms', 0):.0f}ms")

                            except Exception as robust_error:
                                logger.error(f"[WS-ROBUST] Error: {robust_error}", exc_info=True)
                                result = None  # Fall through to legacy VBI

                        # ============================================================
                        # LEGACY VBI Pipeline - Fallback Handler
                        # ============================================================
                        if result is None and VBI_TRACER_AVAILABLE:
                            logger.info(f"[WS-VBI] Falling back to legacy VBI Pipeline Orchestrator")
                            try:
                                orchestrator = get_orchestrator()

                                # Create progress callback to send real-time updates via WebSocket
                                async def vbi_progress_callback(progress_data: dict):
                                    """Send VBI progress updates to frontend in real-time."""
                                    try:
                                        if websocket:
                                            normalized = {
                                                "type": "vbi_progress",
                                                "stage": progress_data.get("stage", progress_data.get("stage_name", "unknown")),
                                                "progress": progress_data.get("progress", 0),
                                                "message": progress_data.get("message", progress_data.get("status", "")),
                                                "timestamp": progress_data.get("timestamp", time.time()),
                                            }
                                            if "confidence" in progress_data:
                                                normalized["confidence"] = progress_data["confidence"]
                                            if "speaker" in progress_data:
                                                normalized["speaker"] = progress_data["speaker"]
                                            await websocket.send_json(normalized)
                                    except Exception as ws_err:
                                        logger.warning(f"[WS-VBI] Failed to send progress update: {ws_err}")

                                vbi_result = await orchestrator.process_voice_unlock(
                                    command=command_text,
                                    audio_data=audio_data_received,
                                    sample_rate=sample_rate_received or 16000,
                                    mime_type=mime_type_received or "audio/webm",
                                    progress_callback=vbi_progress_callback
                                )

                                # Get full trace data for frontend display
                                trace_id = vbi_result.get("trace_id", "")
                                vbi_trace = None
                                if trace_id and VBI_TRACER_AVAILABLE:
                                    try:
                                        tracer = get_tracer()
                                        recent_traces = tracer.get_recent_traces(1)
                                        if recent_traces:
                                            vbi_trace = recent_traces[0]
                                    except Exception as trace_err:
                                        logger.warning(f"[WS-VBI] Could not get trace data: {trace_err}")

                                result = {
                                    "type": "voice_unlock",
                                    "response": vbi_result.get("response", ""),
                                    "message": vbi_result.get("response", ""),
                                    "success": vbi_result.get("success", False),
                                    "command_type": "voice_unlock",
                                    "speaker_name": vbi_result.get("speaker_name", ""),
                                    "confidence": vbi_result.get("confidence", 0.0),
                                    "trace_id": trace_id,
                                    "vbi_trace": vbi_trace,
                                    "handler": "legacy_vbi",
                                    "metadata": vbi_result
                                }

                                logger.info(f"[WS-VBI] Legacy VBI Pipeline Result:")
                                logger.info(f"   Success: {result['success']}")
                                logger.info(f"   Confidence: {result.get('confidence', 0):.1%}")

                            except Exception as vbi_error:
                                logger.error(f"[WS-VBI] VBI Pipeline Error: {vbi_error}", exc_info=True)
                                result = None

                        # No unlock handler available
                        if result is None and not ROBUST_UNLOCK_AVAILABLE and not VBI_TRACER_AVAILABLE:
                            result = None  # Will process through standard path

                    # Standard JARVIS API processing (for non-unlock or no audio)
                    if result is None:
                        try:
                            from .jarvis_voice_api import JARVISCommand, jarvis_api

                            command_obj = JARVISCommand(text=command_text, audio_data=audio_data_received)

                            logger.info(f"[WS] Processing command via jarvis_api: {command_text}")

                            # ===========================================================
                            # v32.0: DYNAMIC TIMEOUT SYSTEM FOR SURVEILLANCE COMMANDS
                            # ===========================================================
                            # PROBLEM: Surveillance commands need 60-90s to:
                            #   1. Discover windows across spaces
                            #   2. Teleport windows to Ghost Display
                            #   3. Spawn watchers for each window
                            # The old fixed 45s timeout was killing these before completion.
                            #
                            # SOLUTION: Detect surveillance intent early and use dynamic timeout
                            # ===========================================================
                            cmd_lower = command_text.lower() if command_text else ""
                            
                            # Surveillance detection (mirrors jarvis_voice_api.py detection)
                            surveillance_keywords = ["watch", "monitor", "scan", "look for", "find", "track", "observe"]
                            surveillance_structures = ["for", "when", "until", "if", "whenever", "while"]
                            multi_targets = ["all", "every", "each", "any"]
                            
                            has_surveillance_keyword = any(kw in cmd_lower for kw in surveillance_keywords)
                            has_surveillance_structure = any(p in cmd_lower for p in surveillance_structures)
                            has_multi_target = any(t in cmd_lower for t in multi_targets)
                            
                            is_surveillance_command = (
                                has_surveillance_keyword and (has_surveillance_structure or has_multi_target)
                            )
                            
                            # Dynamic timeout based on command type
                            if is_surveillance_command:
                                # Surveillance needs more time for window discovery + teleportation + watcher spawn
                                base_timeout = float(os.getenv("WS_SURVEILLANCE_TIMEOUT", "90.0"))
                                logger.info(f"[WS] 👁️ Surveillance command detected - using {base_timeout}s timeout")
                            else:
                                # Standard commands use shorter timeout
                                base_timeout = float(os.getenv("WS_COMMAND_TIMEOUT", "45.0"))
                            
                            # ===========================================================
                            # PROGRESS CALLBACK SYSTEM - Real-time frontend updates
                            # ===========================================================
                            progress_cancelled = asyncio.Event()
                            
                            # Different progress stages for surveillance vs regular commands
                            if is_surveillance_command:
                                progress_stages = [
                                    {"stage": "analyzing", "message": "🧠 Analyzing surveillance request..."},
                                    {"stage": "discovery", "message": "🔍 Discovering windows across spaces..."},
                                    {"stage": "teleport", "message": "👻 Moving windows to Ghost Display..."},
                                    {"stage": "watchers", "message": "👁️ Spawning parallel watchers..."},
                                    {"stage": "validation", "message": "✅ Validating capture streams..."},
                                    {"stage": "monitoring", "message": "🎯 Starting surveillance..."},
                                ]
                            else:
                                progress_stages = [
                                {"stage": "analyzing", "message": "🧠 Analyzing your request..."},
                                {"stage": "processing", "message": "⚙️ Processing command..."},
                                {"stage": "vision_init", "message": "👁️ Initializing vision..."},
                                {"stage": "api_call", "message": "📡 Connecting to AI..."},
                                {"stage": "generating", "message": "✨ Generating response..."},
                            ]

                            async def send_progress_updates():
                                """
                                v32.2: REAL-TIME SURVEILLANCE PROGRESS STREAMING
                                
                                Architecture:
                                - For surveillance: Use event-driven stream (100ms polling)
                                - For regular commands: Use fallback stages (2s interval)
                                - v32.2: Stop fallback once monitoring_active is reached
                                - Prevent stage regression in UI
                                """
                                try:
                                    stage_index = 0
                                    last_real_event_time = 0
                                    events_sent = 0
                                    highest_stage_order = 0  # v32.2: Track highest stage reached
                                    monitoring_active_reached = False  # v32.2: Stop fallback once active
                                    
                                    # v32.2: Stage ordering to prevent regression
                                    stage_order = {
                                        'starting': 1, 'analyzing': 2, 'discovery': 4,
                                        'teleport_start': 6, 'teleport': 6, 'teleport_progress': 7, 'teleport_complete': 8,
                                        'watcher_start': 9, 'watchers': 9, 'watcher_progress': 10, 'watcher_ready': 11,
                                        'validation': 12, 'monitoring': 15, 'monitoring_active': 15,
                                        'detection': 20, 'complete': 25, 'error': 25,
                                    }
                                    
                                    # v32.1: For surveillance commands, subscribe to real progress stream
                                    surveillance_subscriber_id = None
                                    progress_stream = None
                                    
                                    if is_surveillance_command:
                                        try:
                                            from backend.core.surveillance_progress_stream import get_progress_stream
                                            progress_stream = get_progress_stream()
                                            surveillance_subscriber_id = await progress_stream.subscribe()
                                            logger.info(f"[WS] 👁️ Subscribed to surveillance progress stream: {surveillance_subscriber_id}")
                                        except ImportError as e:
                                            logger.debug(f"[WS] Surveillance progress stream not available: {e}")
                                        except Exception as e:
                                            logger.debug(f"[WS] Failed to subscribe to progress stream: {e}")
                                    
                                    try:
                                        while not progress_cancelled.is_set():
                                            sent_real_event = False
                                            
                                            # ═══════════════════════════════════════════════════
                                            # v32.2: REAL-TIME EVENT STREAMING (100ms polling)
                                            # Track highest stage reached to prevent UI regression
                                            # ═══════════════════════════════════════════════════
                                            if surveillance_subscriber_id and progress_stream:
                                                try:
                                                    queue = progress_stream._subscribers.get(surveillance_subscriber_id)
                                                    if queue:
                                                        # Drain all queued events immediately
                                                        while not queue.empty():
                                                            try:
                                                                real_event = queue.get_nowait()
                                                                await websocket.send_json(real_event)
                                                                last_real_event_time = time.time()
                                                                events_sent += 1
                                                                sent_real_event = True
                                                                
                                                                stage_name = real_event.get('stage', 'unknown')
                                                                
                                                                # v32.2: Track highest stage reached
                                                                event_stage_order = stage_order.get(stage_name, 0)
                                                                if event_stage_order > highest_stage_order:
                                                                    highest_stage_order = event_stage_order
                                                                
                                                                # v32.2: Check if monitoring is now active
                                                                if stage_name in ('monitoring', 'monitoring_active', 'detection'):
                                                                    monitoring_active_reached = True
                                                                    logger.info(f"[WS] 🎯 Monitoring ACTIVE - stopping fallback events")
                                                                
                                                                logger.info(f"[WS] 📡 Real-time progress: {stage_name} (order {event_stage_order}, {events_sent} events sent)")
                                                            except asyncio.QueueEmpty:
                                                                break
                                                            except Exception as e:
                                                                logger.debug(f"[WS] Error sending event: {e}")
                                                except Exception as e:
                                                    logger.debug(f"[WS] Error reading progress queue: {e}")
                                            
                                            # ═══════════════════════════════════════════════════
                                            # v32.2: FALLBACK with stage regression prevention
                                            # ═══════════════════════════════════════════════════
                                            time_since_real_event = time.time() - last_real_event_time
                                            
                                            # Determine the fallback stage we would send
                                            if stage_index < len(progress_stages):
                                                fallback_stage = progress_stages[stage_index]
                                            else:
                                                fallback_stage = progress_stages[-1]
                                            
                                            fallback_stage_order = stage_order.get(fallback_stage["stage"], 0)
                                            
                                            # v32.2: Only send fallback if:
                                            # 1. Monitoring is NOT yet active (once active, no more fallback)
                                            # 2. No real event was sent this iteration
                                            # 3. No real events in the last 3 seconds
                                            # 4. Fallback stage is AHEAD of highest real stage (no regression)
                                            fallback_interval = 3.0 if is_surveillance_command else 2.0
                                            
                                            should_send_fallback = (
                                                not monitoring_active_reached and  # v32.2: Stop once monitoring active
                                                not sent_real_event and 
                                                (last_real_event_time == 0 or time_since_real_event > fallback_interval) and
                                                fallback_stage_order >= highest_stage_order  # v32.2: No regression
                                            )
                                            
                                            if should_send_fallback:
                                                await websocket.send_json({
                                                    "type": "processing_progress",
                                                    "stage": fallback_stage["stage"],
                                                    "message": fallback_stage["message"],
                                                    "stage_index": min(stage_index, len(progress_stages) - 1),
                                                    "total_stages": len(progress_stages),
                                                    "is_surveillance": is_surveillance_command,
                                                    "timeout_seconds": base_timeout,
                                                    "timestamp": time.time(),
                                                    "is_fallback": True,  # Signal this is synthetic
                                                })
                                                stage_index += 1
                                                highest_stage_order = max(highest_stage_order, fallback_stage_order)
                                                last_real_event_time = time.time()  # Reset to avoid rapid fallbacks
                                            elif monitoring_active_reached:
                                                # v32.2: Log that we're skipping fallback
                                                logger.debug(f"[WS] Skipping fallback - monitoring already active")

                                            # ═══════════════════════════════════════════════════
                                            # POLL INTERVAL: Fast for real-time, slow for fallback
                                            # ═══════════════════════════════════════════════════
                                            poll_interval = 0.1 if is_surveillance_command else 0.5

                                            try:
                                                await asyncio.wait_for(
                                                    progress_cancelled.wait(),
                                                    timeout=poll_interval
                                                )
                                                break  # Cancelled
                                            except asyncio.TimeoutError:
                                                pass  # Continue polling

                                    finally:
                                        # v32.1: Cleanup surveillance subscription
                                        if surveillance_subscriber_id and progress_stream:
                                            try:
                                                await progress_stream.unsubscribe(surveillance_subscriber_id)
                                                logger.debug(f"[WS] Unsubscribed from surveillance progress: {surveillance_subscriber_id} (sent {events_sent} events)")
                                            except Exception:
                                                pass
                                except Exception as e:
                                    logger.debug(f"[WS] Progress update task ended: {e}")

                            # Start progress updates in background
                            progress_task = asyncio.create_task(send_progress_updates())

                            # ===========================================================
                            # v32.0: PROCESS COMMAND WITH DYNAMIC TIMEOUT
                            # ===========================================================
                            try:
                                jarvis_result = await asyncio.wait_for(
                                    jarvis_api.process_command(command_obj),
                                    timeout=base_timeout
                                )
                            except asyncio.TimeoutError:
                                timeout_msg = (
                                    f"Surveillance setup timed out after {base_timeout:.0f}s. "
                                    "The system may be initializing many windows."
                                    if is_surveillance_command else
                                    "I apologize, but processing took too long. Please try again with a simpler request."
                                )
                                logger.error(f"[WS] Command processing timed out after {base_timeout}s: {command_text}")
                                jarvis_result = {
                                    "response": timeout_msg,
                                    "status": "timeout",
                                    "success": False,
                                    "command_type": "surveillance" if is_surveillance_command else "unknown",
                                }
                            finally:
                                # Cancel progress updates
                                progress_cancelled.set()
                                progress_task.cancel()
                                try:
                                    await progress_task
                                except asyncio.CancelledError:
                                    pass

                            result = {
                                "response": jarvis_result.get("response", ""),
                                "success": jarvis_result.get("success", jarvis_result.get("status") == "success"),
                                "command_type": jarvis_result.get("command_type", "unknown"),
                                "metadata": jarvis_result,
                            }

                            logger.info(f"[WS] JARVIS response: {result.get('response', '')[:100]}")

                        except ImportError as e:
                            logger.error(f"[WS] Failed to import jarvis_voice_api: {e}")
                            result = {"response": "", "success": False, "error": str(e)}
                        except Exception as e:
                            logger.error(f"[WS] Error processing command: {e}", exc_info=True)
                            result = {"response": f"Error processing command: {e}", "success": False}

                    # Log final timing
                    command_duration = (time.time() - command_start_time) * 1000
                    logger.info(f"[WS-VBI] COMMAND COMPLETED in {command_duration:.0f}ms")
                    logger.info("=" * 70)

                else:
                    # For non-command types (vision), use the pipeline
                    result = await self.pipeline.process_async(
                        text=message.get("text", message.get("command", "")),
                        audio_data=audio_data_received,
                        speaker_name=message.get("speaker_name"),
                        metadata={
                            "message": message,
                            "client_id": client_id,
                            "websocket": websocket,
                            "stream_mode": message.get("stream", False),
                            "audio_sample_rate": sample_rate_received,
                            "audio_mime_type": mime_type_received,
                        },
                    )

                # 🛡️ STREAMING SAFEGUARD: Only check AUDIO STREAM transcriptions for commands
                # This safeguard is for detecting when a command appears in live audio transcription
                # NOT for checking regular text command responses (which naturally contain command words)
                is_audio_stream = msg_type == "ml_audio_stream" or bool(audio_data_received)
                if is_audio_stream and self.safeguard_enabled and client_id in connection_safeguards:
                    safeguard = connection_safeguards[client_id]

                    # Extract TRANSCRIPTION text (not response) - only relevant for audio streams
                    transcription_text = result.get("transcription", result.get("text", ""))

                    if transcription_text:
                        # Check for command detection in transcription
                        detection_event = await safeguard.check_transcription(
                            transcription=transcription_text,
                            confidence=result.get("confidence"),
                            metadata={"client_id": client_id, "msg_type": msg_type}
                        )

                        if detection_event:
                            logger.warning(
                                f"[UNIFIED-WS] 🚨 Command detected in stream: '{detection_event.command}' | "
                                f"Transcription: '{transcription_text}' | "
                                f"Closing stream for {client_id}"
                            )

                            # Signal client to stop streaming
                            if websocket:
                                try:
                                    await websocket.send_json({
                                        "type": "stream_stop",
                                        "reason": "command_detected",
                                        "command": detection_event.command,
                                        "message": "Command detected, stopping audio stream"
                                    })
                                except Exception as e:
                                    logger.error(f"Failed to send stream_stop message: {e}")

                # Return response from pipeline
                # First check if we have a direct response
                if result.get("response"):
                    # CRITICAL: Preserve original type for voice_unlock so frontend can route correctly
                    # The frontend looks for type: "voice_unlock" to handle VBI responses
                    original_type = result.get("type")
                    if original_type == "voice_unlock":
                        # Voice unlock response - preserve all VBI data for frontend
                        response_dict = {
                            "type": "voice_unlock",  # MUST be voice_unlock for frontend routing
                            "response": result.get("response"),
                            "message": result.get("response"),  # Alias for compatibility
                            "success": result.get("success", True),
                            "speak": False,  # NO AUDIO for voice unlock (prevent feedback loop)
                            "command_type": "voice_unlock",
                            "speaker_name": result.get("speaker_name", ""),
                            "confidence": result.get("confidence", 0.0),
                            "trace_id": result.get("trace_id", ""),
                            "vbi_trace": result.get("vbi_trace"),
                        }
                        logger.info(f"[WS-VBI] Sending voice_unlock response to frontend: success={response_dict['success']}")
                    else:
                        # Standard command response
                        # v238.0: Echo requestId for frontend dedup.
                        response_dict = {
                            "type": "command_response",
                            "response": result.get("response"),
                            "success": result.get("success", True),
                            "speak": True,  # Enable text-to-speech for all responses
                            "requestId": message.get("requestId"),
                        }

                    # Add additional metadata for lock/unlock commands
                    if result.get("metadata", {}).get("lock_unlock_result"):
                        lock_result = result["metadata"]["lock_unlock_result"]
                        response_dict["action"] = lock_result.get("action", "")
                        response_dict["command_type"] = lock_result.get("type", "system_control")

                    return response_dict

                # Fall back to metadata response
                return result.get("metadata", {}).get(
                    "response", {"type": "error", "error": "No response generated"}
                )

            except Exception as e:
                logger.error(f"Pipeline processing error for {msg_type}: {e}")
                return {"type": "error", "error": str(e), "original_type": msg_type}

        # Fall back to legacy handlers for other message types
        elif msg_type in self.handlers:
            try:
                return await self.handlers[msg_type](client_id, message)
            except Exception as e:
                logger.error(f"Error handling {msg_type}: {e}")
                return {"type": "error", "error": str(e), "original_type": msg_type}
        else:
            return {
                "type": "error",
                "error": f"Unknown message type: {msg_type}",
                "available_types": list(self.handlers.keys()),
            }

    def get_system_metrics(self) -> Dict[str, Any]:
        """
        Get comprehensive system metrics and statistics

        Returns detailed information about:
        - Connection statistics
        - Health metrics
        - Performance data
        - Circuit breaker state
        - Learning patterns
        """
        current_time = time.time()
        uptime = current_time - self.metrics["uptime_start"]

        # Calculate current connection stats
        active_connections = len(self.connections)
        healthy_connections = sum(
            1 for h in self.connection_health.values() if h.state == ConnectionState.HEALTHY
        )
        degraded_connections = sum(
            1 for h in self.connection_health.values() if h.state == ConnectionState.DEGRADED
        )
        recovering_connections = sum(
            1 for h in self.connection_health.values() if h.state == ConnectionState.RECOVERING
        )

        # Calculate average health score
        avg_health_score = (
            sum(h.health_score for h in self.connection_health.values()) / len(self.connection_health)
            if self.connection_health
            else 0
        )

        # Calculate average latency
        avg_latency = (
            sum(h.latency_ms for h in self.connection_health.values()) / len(self.connection_health)
            if self.connection_health
            else 0
        )

        return {
            "uptime_seconds": uptime,
            "uptime_formatted": f"{int(uptime // 3600)}h {int((uptime % 3600) // 60)}m {int(uptime % 60)}s",
            "connections": {
                "active": active_connections,
                "total_lifetime": self.metrics["total_connections"],
                "total_disconnections": self.metrics["total_disconnections"],
                "healthy": healthy_connections,
                "degraded": degraded_connections,
                "recovering": recovering_connections,
            },
            "messages": {
                "total_sent": self.metrics["total_messages_sent"],
                "total_received": self.metrics["total_messages_received"],
                "throughput_per_second": (
                    (self.metrics["total_messages_sent"] + self.metrics["total_messages_received"]) / uptime
                    if uptime > 0
                    else 0
                ),
            },
            "health": {
                "average_score": avg_health_score,
                "average_latency_ms": avg_latency,
                "total_errors": self.metrics["total_errors"],
                "total_recoveries": self.metrics["total_recoveries"],
                "error_rate": (
                    self.metrics["total_errors"] / self.metrics["total_connections"]
                    if self.metrics["total_connections"] > 0
                    else 0
                ),
            },
            "circuit_breaker": {
                "is_open": self.circuit_open,
                "failures": self.circuit_failures,
                "threshold": self.config["circuit_breaker_threshold"],
                "total_activations": self.metrics["circuit_breaker_activations"],
            },
            "learning": {
                "patterns_collected": len(self.disconnection_patterns),
                "max_pattern_history": self.config["max_pattern_history"],
            },
            "intelligence": {
                "uae_available": self.uae_engine is not None,
                "sai_available": self.sai_engine is not None,
                "learning_db_available": self.learning_db is not None,
                "predictive_healing_enabled": self.config["predictive_healing_enabled"],
            },
            "config": {
                "health_check_interval": self.config["health_check_interval"],
                "message_timeout": self.config["message_timeout"],
                "ping_interval": self.config["ping_interval"],
                "connection_rate_limit": self.connection_rate_limit,
            },
        }

    async def broadcast(self, message: Dict[str, Any], capability: Optional[str] = None):
        """
        v126.0: Broadcast message to all connected clients with safe sending.

        Uses _safe_send_json to prevent sending to closed connections.
        Automatically marks failed connections for cleanup.
        """
        disconnected = []

        # v126.0: Iterate over a copy of keys to avoid modification during iteration
        for client_id in list(self.connections.keys()):
            # Skip if capability filter is set and client doesn't have it
            if capability and capability not in connection_capabilities.get(client_id, set()):
                continue

            # v126.0: Use safe sending to prevent close errors
            success = await self._safe_send_json(
                client_id,
                message,
                mark_failed_on_error=True
            )

            if not success:
                disconnected.append(client_id)

        # Clean up disconnected clients
        for client_id in disconnected:
            try:
                await self.disconnect(client_id)
            except Exception as e:
                logger.debug(f"[UNIFIED-WS] Error during broadcast cleanup of {client_id}: {e}")
                # Force cleanup
                self.connections.pop(client_id, None)
                self.connection_health.pop(client_id, None)

    # Handler implementations

    async def _handle_voice_command(
        self, client_id: str, message: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Handle voice/JARVIS commands"""
        try:
            command_text = message.get("command", message.get("text", ""))

            logger.info(f"[WS] Processing voice command: {command_text}")

            # ============== PHASE 2: Hybrid Orchestrator Integration ==============
            # Route commands through hybrid orchestrator for intelligent local/GCP routing
            # based on memory pressure, command complexity, and available resources
            try:
                from backend.core.hybrid_orchestrator import get_orchestrator

                orchestrator = get_orchestrator()

                logger.info(f"[WS] Routing command through hybrid orchestrator...")

                # Execute command with intelligent routing
                result = await orchestrator.execute_command(
                    command=command_text,
                    command_type="voice_command",  # Signals NLP-heavy processing
                    metadata={
                        "client_id": client_id,
                        "source": "websocket",
                        "context": "user_initiated",
                        "timestamp": datetime.now().isoformat(),
                    },
                )

                # Extract response from hybrid orchestrator result
                response_text = result.get(
                    "response", result.get("result", "Command processed, Sir.")
                )
                success = result.get("success", False)
                command_type = result.get("command_type", "voice_command")

                # Log routing decision for monitoring and analytics
                if "routing" in result:
                    routing_info = result["routing"]
                    backend = routing_info.get("backend", "unknown")
                    rule = routing_info.get("rule", "unknown")
                    confidence = routing_info.get("confidence", 0)

                    logger.info(
                        f"[WS] ✅ Routed to {backend.upper()} "
                        f"(rule: {rule}, confidence: {confidence:.2f})"
                    )

                    # Add routing info to response for debugging
                    response_metadata = {
                        "routed_to": backend,
                        "rule": rule,
                        "confidence": confidence,
                    }
                else:
                    response_metadata = {}

                logger.info(f"[WS] Hybrid orchestrator result: {response_text[:100]}")

                return {
                    "type": "response",
                    "text": response_text,
                    "status": "success" if success else "error",
                    "command_type": command_type,
                    "speak": True,
                    "routing": response_metadata,  # Include routing info
                }

            except ImportError as e:
                # Fallback 1: Try unified command processor
                logger.warning(
                    f"[WS] Hybrid orchestrator not available ({e}), trying unified processor..."
                )

                try:
                    from api.unified_command_processor import get_unified_processor

                    processor = get_unified_processor()
                    result = await processor.process_command(command_text, websocket=None)

                    response_text = result.get("response", "Command processed, Sir.")
                    success = result.get("success", False)
                    command_type = result.get("command_type", "unknown")

                    logger.info(f"[WS] Unified processor result: {response_text[:100]}")

                    return {
                        "type": "response",
                        "text": response_text,
                        "status": "success" if success else "error",
                        "command_type": command_type,
                        "speak": True,
                        "routing": {
                            "routed_to": "local_fallback",
                            "reason": "orchestrator_unavailable",
                        },
                    }
                except ImportError:
                    # Fallback 2: Use async pipeline
                    logger.warning(
                        "[WS] Unified processor not available, falling back to async pipeline"
                    )
                    result = await self.pipeline.process_async(text=command_text)

                    response_text = result.get("response", "Command processed, Sir.")
                    success = result.get("success", False)

                    logger.info(f"[WS] Pipeline result: {response_text[:100]}")

                    return {
                        "type": "response",
                        "text": response_text,
                        "status": "success" if success else "error",
                        "command_type": result.get("metadata", {}).get("intent", "unknown"),
                        "speak": True,
                        "routing": {
                            "routed_to": "pipeline_fallback",
                            "reason": "all_processors_unavailable",
                        },
                    }
            # ======================================================================

        except Exception as e:
            logger.error(f"Error processing voice command: {e}", exc_info=True)
            return {
                "type": "response",
                "text": f"I encountered an error: {str(e)}",
                "status": "error",
                "speak": True,
            }

    async def _handle_vision_analyze(
        self, _client_id: str, message: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Handle vision analysis requests"""
        try:
            # Get vision analyzer from app state
            from ..main import app

            if hasattr(app.state, "vision_analyzer"):
                analyzer = app.state.vision_analyzer

                # Perform analysis
                screenshot = await analyzer.capture_screen()
                if screenshot:
                    query = message.get("query", "Describe what you see on screen")
                    result = await analyzer.describe_screen(
                        {"screenshot": screenshot, "query": query}
                    )

                    return {
                        "type": "vision_result",
                        "success": result.get("success", False),
                        "description": result.get("description", ""),
                        "timestamp": datetime.now().isoformat(),
                    }

            return {
                "type": "vision_result",
                "success": False,
                "error": "Vision analyzer not available",
            }

        except Exception as e:
            logger.error(f"Vision analysis error: {e}")
            return {"type": "vision_result", "success": False, "error": str(e)}

    async def _handle_vision_monitor(
        self, client_id: str, message: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Handle continuous vision monitoring"""
        action = message.get("action", "start")

        if action == "start":
            connection_capabilities[client_id].add("vision_monitoring")

            # Start monitoring loop for this client
            asyncio.create_task(self._vision_monitoring_loop(client_id))

            return {"type": "monitor_status", "status": "started", "client_id": client_id}
        elif action == "stop":
            connection_capabilities[client_id].discard("vision_monitoring")
            return {"type": "monitor_status", "status": "stopped", "client_id": client_id}
        else:
            return {"type": "monitor_status", "status": "unknown_action", "client_id": client_id}

    async def _vision_monitoring_loop(self, client_id: str):
        """Continuous vision monitoring loop"""
        while client_id in self.connections and "vision_monitoring" in connection_capabilities.get(
            client_id, set()
        ):
            try:
                # Analyze screen periodically
                await self._handle_vision_analyze(client_id, {"type": "vision_analyze"})
                await asyncio.sleep(5)  # Analyze every 5 seconds
            except Exception as e:
                logger.error(f"Monitoring error for {client_id}: {e}")
                break

    async def _handle_workspace_analysis(
        self, _client_id: str, _message: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Handle workspace analysis requests"""
        # This would integrate with workspace analyzer
        return {
            "type": "workspace_result",
            "analysis": "Workspace analysis placeholder",
            "timestamp": datetime.now().isoformat(),
        }

    async def _handle_ml_audio(self, _client_id: str, message: Dict[str, Any]) -> Dict[str, Any]:
        """Handle ML audio streaming"""
        audio_data = message.get("audio_data", "")

        return {
            "type": "ml_audio_result",
            "status": "processed",
            "has_speech": len(audio_data) > 0,
            "timestamp": datetime.now().isoformat(),
        }

    async def _handle_audio_error(self, _client_id: str, message: Dict[str, Any]) -> Dict[str, Any]:
        """Handle audio error recovery"""
        error_type = message.get("error_type", "unknown")

        return {
            "type": "audio_recovery",
            "strategy": "reconnect" if error_type == "connection" else "retry",
            "delay_ms": 1000,
        }

    async def _handle_model_status(self, _client_id: str, _message: Dict[str, Any]) -> Dict[str, Any]:
        """Handle ML model status requests"""
        # This would integrate with ML model loader
        return {
            "type": "model_status",
            "models_loaded": True,
            "status": "ready",
            "timestamp": datetime.now().isoformat(),
        }

    async def _handle_network_status(
        self, _client_id: str, _message: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Handle network status checks"""
        return {
            "type": "network_status",
            "status": "connected",
            "latency_ms": 50,
            "timestamp": datetime.now().isoformat(),
        }

    async def _handle_notification(self, _client_id: str, _message: Dict[str, Any]) -> Dict[str, Any]:
        """Handle notification detection"""
        # This would integrate with notification detection
        return {
            "type": "notification_result",
            "notifications": [],
            "timestamp": datetime.now().isoformat(),
        }

    async def _handle_ping(self, _client_id: str, message: Dict[str, Any]) -> Dict[str, Any]:
        """Handle ping/pong for connection keep-alive"""
        return {"type": "pong", "timestamp": message.get("timestamp", datetime.now().isoformat())}

    async def _handle_subscribe(self, client_id: str, message: Dict[str, Any]) -> Dict[str, Any]:
        """Handle capability subscription"""
        capabilities = message.get("capabilities", [])

        for cap in capabilities:
            connection_capabilities[client_id].add(cap)

        return {
            "type": "subscription_result",
            "subscribed": capabilities,
            "current_capabilities": list(connection_capabilities[client_id]),
        }

    async def _handle_unsubscribe(self, client_id: str, message: Dict[str, Any]) -> Dict[str, Any]:
        """Handle capability unsubscription"""
        capabilities = message.get("capabilities", [])

        for cap in capabilities:
            connection_capabilities[client_id].discard(cap)

        return {
            "type": "unsubscription_result",
            "unsubscribed": capabilities,
            "current_capabilities": list(connection_capabilities[client_id]),
        }


# Create global manager instance - LAZY initialization to avoid asyncio issues at import time
_ws_manager: Optional[UnifiedWebSocketManager] = None


def get_ws_manager() -> UnifiedWebSocketManager:
    """Get or create the WebSocket manager (lazy initialization)"""
    global _ws_manager
    if _ws_manager is None:
        _ws_manager = UnifiedWebSocketManager()
    return _ws_manager


# Alias for backward compatibility
ws_manager = None  # Will be set lazily


def set_jarvis_instance(jarvis_api):
    """Set the JARVIS instance for the WebSocket pipeline"""
    manager = get_ws_manager()
    if manager and manager.pipeline:
        manager.pipeline.jarvis = jarvis_api
        logger.info("✅ JARVIS instance set in unified WebSocket pipeline")


@router.websocket("/ws")
async def unified_websocket_endpoint(websocket: WebSocket):
    """
    Single unified WebSocket endpoint for all communication with advanced self-healing.

    ROOT CAUSE FIX v2.0.0:
    - Properly handles connection rejection (rate limit, shutdown)
    - Ensures websocket.accept() is called before any receive operations
    - Graceful error handling with proper cleanup
    """
    client_id = f"client_{id(websocket)}_{datetime.now().timestamp()}"

    # Get the manager lazily (now safe since we're in an async context with event loop)
    manager = get_ws_manager()

    # ROOT CAUSE FIX: Wrap connect() in try block since it can now raise WebSocketDisconnect
    try:
        await manager.connect(websocket, client_id)
    except WebSocketDisconnect as e:
        # Connection rejected (rate limit, shutdown, etc.)
        logger.info(f"[UNIFIED-WS] Connection rejected for {client_id}: {e.reason if hasattr(e, 'reason') else e}")
        return  # Exit cleanly, websocket already closed by connect()
    except Exception as e:
        logger.error(f"[UNIFIED-WS] Failed to establish connection for {client_id}: {e}")
        # Try to close websocket gracefully if not already closed
        try:
            await websocket.close(code=1011, reason="Connection failed")
        except Exception:
            pass
        return

    # WebSocket idle timeout protection
    idle_timeout = float(os.getenv("TIMEOUT_WEBSOCKET_IDLE", "300.0"))  # 5 min default

    try:
        while True:
            # Receive message with timeout protection
            try:
                data = await asyncio.wait_for(
                    websocket.receive_json(),
                    timeout=idle_timeout
                )
            except asyncio.TimeoutError:
                logger.info(f"[UNIFIED-WS] WebSocket idle timeout for {client_id}, closing connection")
                break

            # Update health metrics
            if client_id in manager.connection_health:
                health = manager.connection_health[client_id]
                health.last_message_time = time.time()
                health.messages_received += 1

                # Update health score on successful message
                health.health_score = min(100, health.health_score + 1)

                # If recovering, mark as healthy
                if health.state == ConnectionState.RECOVERING:
                    health.state = ConnectionState.HEALTHY
                    logger.info(f"[UNIFIED-WS] Connection {client_id} recovered to healthy state")

            # Handle ping/pong for health monitoring
            if data.get("type") == "ping":
                # v239.0: Use safe send — client may have disconnected
                pong_sent = await manager._safe_send_json(client_id, {
                    "type": "pong",
                    "timestamp": data.get("timestamp"),
                    "server_time": datetime.now().isoformat(),
                })
                if pong_sent:
                    logger.debug(
                        f"[UNIFIED-WS] 🏓 Received ping from {client_id}, sent pong | "
                        f"Health: {manager.connection_health[client_id].health_score:.1f}"
                    )
                else:
                    logger.debug(f"[UNIFIED-WS] Ping from {client_id} but connection closed, exiting loop")
                    break
                continue

            # Log incoming command for debugging
            if data.get("type") == "command" or data.get("type") == "voice_command":
                logger.info(
                    f"[WS] Received command: {data.get('text', data.get('command', 'unknown'))}"
                )

            # Handle message
            response = await manager.handle_message(client_id, data)

            # Update health metrics for sent message
            if client_id in manager.connection_health:
                health = manager.connection_health[client_id]
                health.messages_sent += 1

            # Log outgoing response for debugging lock/unlock
            if "lock" in str(data).lower() or "unlock" in str(data).lower():
                logger.info(f"[WS] Sending lock/unlock response: {response}")

            # Send response
            # v239.0: Use safe send — client may disconnect during handle_message()
            # processing (LLM calls take 2-10s). _safe_send_json handles close
            # detection, health tracking, and error recovery.
            if not await manager._safe_send_json(client_id, response):
                logger.info(f"[UNIFIED-WS] Client {client_id} disconnected during processing, exiting loop")
                break

    except WebSocketDisconnect:
        logger.info(f"[UNIFIED-WS] Client {client_id} disconnected (WebSocketDisconnect)")
    except Exception as e:
        logger.error(f"[UNIFIED-WS] WebSocket error for {client_id}: {e}", exc_info=True)

        # Increment error count
        if client_id in manager.connection_health:
            health = manager.connection_health[client_id]
            health.errors += 1
            health.last_error = str(e)
            health.health_score = max(0, health.health_score - 10)

            # Increment circuit breaker failures
            manager.circuit_failures += 1
    finally:
        await manager.disconnect(client_id)
