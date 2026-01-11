"""
Cross-Repo Voice Client v100.0 - Trinity Voice Integration for External Repos
==============================================================================

Lightweight client for JARVIS Prime and Reactor Core to send voice announcements
to the centralized Trinity Voice Coordinator in JARVIS Body.

Architecture:
- JARVIS Body: Hosts the full Trinity Voice Coordinator (TTS engines, queue, etc.)
- JARVIS Prime: Uses this client to send voice announcements via IPC/HTTP
- Reactor Core: Uses this client to announce training events via IPC/HTTP

Communication Methods (in priority order):
1. Direct coordinator access (if running in same process)
2. Trinity IPC (file-based atomic communication)
3. HTTP REST API (fallback for remote connections)

Features:
- Zero hardcoding - all environment-driven
- Async/parallel execution
- Circuit breaker for resilience
- Correlation ID support for distributed tracing
- Automatic fallback between communication methods
- Health check integration

Author: JARVIS Trinity v100.0
"""

from __future__ import annotations

import os
import asyncio
import logging
import time
import json
import uuid
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, Tuple, List
from pathlib import Path
import aiohttp

logger = logging.getLogger(__name__)


# =============================================================================
# Configuration (Environment-Driven)
# =============================================================================

def _env_str(key: str, default: str) -> str:
    return os.getenv(key, default)


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    value = os.getenv(key)
    if value is None:
        return default
    return value.lower() in ("true", "1", "yes", "on")


@dataclass
class CrossRepoVoiceConfig:
    """Configuration for cross-repo voice client."""
    # JARVIS Body API Configuration
    jarvis_host: str = field(default_factory=lambda: _env_str(
        "JARVIS_HOST", "localhost"
    ))
    jarvis_port: int = field(default_factory=lambda: _env_int(
        "JARVIS_PORT", 8010
    ))

    # Timeout Configuration
    request_timeout: float = field(default_factory=lambda: _env_float(
        "JARVIS_VOICE_REQUEST_TIMEOUT", 10.0
    ))
    connection_timeout: float = field(default_factory=lambda: _env_float(
        "JARVIS_VOICE_CONNECTION_TIMEOUT", 5.0
    ))

    # Retry Configuration
    max_retries: int = field(default_factory=lambda: _env_int(
        "JARVIS_VOICE_CLIENT_MAX_RETRIES", 3
    ))
    retry_delay: float = field(default_factory=lambda: _env_float(
        "JARVIS_VOICE_CLIENT_RETRY_DELAY", 1.0
    ))

    # IPC Configuration
    ipc_enabled: bool = field(default_factory=lambda: _env_bool(
        "JARVIS_VOICE_IPC_ENABLED", True
    ))
    trinity_dir: Path = field(default_factory=lambda: Path(
        _env_str("TRINITY_DIR", str(Path.home() / ".jarvis" / "trinity"))
    ))

    # Fallback Configuration
    fallback_to_http: bool = field(default_factory=lambda: _env_bool(
        "JARVIS_VOICE_FALLBACK_HTTP", True
    ))
    fallback_to_local: bool = field(default_factory=lambda: _env_bool(
        "JARVIS_VOICE_FALLBACK_LOCAL", True
    ))

    @property
    def voice_api_url(self) -> str:
        return f"http://{self.jarvis_host}:{self.jarvis_port}/api/startup-voice"


# =============================================================================
# Voice Enums (Mirror of coordinator enums for client use)
# =============================================================================

class VoiceContext(Enum):
    """Context for voice announcements determines personality."""
    STARTUP = "startup"
    NARRATOR = "narrator"
    RUNTIME = "runtime"
    ALERT = "alert"
    SUCCESS = "success"
    TRINITY = "trinity"


class VoicePriority(Enum):
    """Priority levels for queue scheduling."""
    CRITICAL = 0
    HIGH = 1
    NORMAL = 2
    LOW = 3
    BACKGROUND = 4


# =============================================================================
# Cross-Repo Voice Client
# =============================================================================

class CrossRepoVoiceClient:
    """
    Lightweight client for sending voice announcements from external repos.

    Usage in JARVIS Prime:
        client = CrossRepoVoiceClient(source="jarvis_prime")
        await client.initialize()

        success, reason = await client.announce(
            "Model loaded successfully",
            VoiceContext.SUCCESS,
            VoicePriority.HIGH
        )

    Usage in Reactor Core:
        client = CrossRepoVoiceClient(source="reactor_core")
        await client.announce_training_complete("gemma-2b", accuracy=0.95)
    """

    def __init__(
        self,
        source: str,
        config: Optional[CrossRepoVoiceConfig] = None
    ):
        self.source = source
        self.config = config or CrossRepoVoiceConfig()

        self._session: Optional[aiohttp.ClientSession] = None
        self._ipc_available = False
        self._http_available = False
        self._direct_available = False
        self._initialized = False

        # Circuit breaker state
        self._failure_count = 0
        self._last_failure_time = 0.0
        self._circuit_open = False

        logger.info(f"[CrossRepoVoice:{source}] Client created")

    async def initialize(self) -> bool:
        """Initialize the client and check available communication methods."""
        logger.info(f"[CrossRepoVoice:{self.source}] Initializing...")

        # Check direct coordinator access (same process)
        self._direct_available = await self._check_direct_access()
        if self._direct_available:
            logger.info(f"[CrossRepoVoice:{self.source}] ✓ Direct coordinator access available")

        # Check IPC access
        if self.config.ipc_enabled:
            self._ipc_available = await self._check_ipc_access()
            if self._ipc_available:
                logger.info(f"[CrossRepoVoice:{self.source}] ✓ Trinity IPC available")

        # Check HTTP access
        if self.config.fallback_to_http:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(
                    total=self.config.request_timeout,
                    connect=self.config.connection_timeout
                )
            )
            self._http_available = await self._check_http_access()
            if self._http_available:
                logger.info(f"[CrossRepoVoice:{self.source}] ✓ HTTP API available")

        self._initialized = True

        if not (self._direct_available or self._ipc_available or self._http_available):
            logger.warning(
                f"[CrossRepoVoice:{self.source}] ⚠️ No communication methods available"
            )
            return False

        return True

    async def _check_direct_access(self) -> bool:
        """Check if direct coordinator access is available."""
        try:
            from backend.core.trinity_voice_coordinator import get_voice_coordinator
            # Don't actually initialize, just check import works
            return True
        except ImportError:
            return False

    async def _check_ipc_access(self) -> bool:
        """Check if Trinity IPC is available."""
        try:
            ipc_dir = self.config.trinity_dir
            return ipc_dir.exists() and (ipc_dir / "commands").exists()
        except Exception:
            return False

    async def _check_http_access(self) -> bool:
        """Check if HTTP API is available."""
        if not self._session:
            return False

        try:
            async with self._session.get(
                f"{self.config.voice_api_url}/status",
                timeout=aiohttp.ClientTimeout(total=2.0)
            ) as response:
                return response.status == 200
        except Exception:
            return False

    async def announce(
        self,
        message: str,
        context: VoiceContext = VoiceContext.RUNTIME,
        priority: VoicePriority = VoicePriority.NORMAL,
        metadata: Optional[Dict[str, Any]] = None,
        correlation_id: Optional[str] = None
    ) -> Tuple[bool, str]:
        """
        Send voice announcement to centralized coordinator.

        Args:
            message: Text to speak
            context: Voice context (determines personality)
            priority: Announcement priority
            metadata: Optional metadata
            correlation_id: Optional W3C correlation ID

        Returns:
            (success: bool, reason: str) tuple
        """
        if not self._initialized:
            await self.initialize()

        correlation_id = correlation_id or str(uuid.uuid4())[:16]

        # Check circuit breaker
        if self._circuit_open:
            if time.time() - self._last_failure_time < 60.0:
                return False, "circuit_open"
            self._circuit_open = False

        # Try communication methods in priority order
        methods = [
            ("direct", self._announce_direct),
            ("ipc", self._announce_ipc),
            ("http", self._announce_http),
        ]

        for method_name, method in methods:
            if method_name == "direct" and not self._direct_available:
                continue
            if method_name == "ipc" and not self._ipc_available:
                continue
            if method_name == "http" and not self._http_available:
                continue

            try:
                success, reason = await method(
                    message, context, priority, metadata, correlation_id
                )

                if success:
                    self._failure_count = 0
                    return success, f"{reason} (via {method_name})"

            except Exception as e:
                logger.warning(
                    f"[CrossRepoVoice:{self.source}] {method_name} failed: {e}"
                )
                continue

        # All methods failed
        self._failure_count += 1
        self._last_failure_time = time.time()

        if self._failure_count >= 5:
            self._circuit_open = True
            logger.warning(
                f"[CrossRepoVoice:{self.source}] Circuit breaker opened after "
                f"{self._failure_count} failures"
            )

        return False, "all_methods_failed"

    async def _announce_direct(
        self,
        message: str,
        context: VoiceContext,
        priority: VoicePriority,
        metadata: Optional[Dict[str, Any]],
        correlation_id: str
    ) -> Tuple[bool, str]:
        """Announce via direct coordinator access."""
        from backend.core.trinity_voice_coordinator import (
            get_voice_coordinator,
            VoiceContext as CoordContext,
            VoicePriority as CoordPriority
        )

        coordinator = await get_voice_coordinator()

        # Map enums (they're identical but need explicit conversion)
        coord_context = CoordContext(context.value)
        coord_priority = CoordPriority[priority.name]

        return await coordinator.announce(
            message=message,
            context=coord_context,
            priority=coord_priority,
            source=self.source,
            metadata=metadata,
            correlation_id=correlation_id
        )

    async def _announce_ipc(
        self,
        message: str,
        context: VoiceContext,
        priority: VoicePriority,
        metadata: Optional[Dict[str, Any]],
        correlation_id: str
    ) -> Tuple[bool, str]:
        """Announce via Trinity IPC."""
        try:
            from backend.core.trinity_ipc import (
                get_resilient_trinity_ipc_bus,
                TrinityCommand,
                ComponentType,
            )

            bus = await get_resilient_trinity_ipc_bus()

            # Create voice command
            source_component = {
                "jarvis_prime": ComponentType.JARVIS_PRIME,
                "reactor_core": ComponentType.REACTOR_CORE,
            }.get(self.source, ComponentType.JARVIS_BODY)

            command = TrinityCommand(
                command_type="voice_announce",
                source=source_component,
                target=ComponentType.JARVIS_BODY,
                payload={
                    "message": message,
                    "context": context.value,
                    "priority": priority.name,
                    "source": self.source,
                    "metadata": metadata or {},
                },
                correlation_id=correlation_id,
            )

            # Enqueue command
            command_id = await bus.enqueue_command(command)

            # Wait for response with timeout
            response = await bus.wait_for_response(
                command_id,
                timeout=self.config.request_timeout
            )

            if response and response.get("success"):
                return True, response.get("reason", "ipc_success")
            else:
                return False, response.get("reason", "ipc_failed") if response else "ipc_timeout"

        except ImportError:
            return False, "ipc_not_available"
        except Exception as e:
            return False, f"ipc_error: {e}"

    async def _announce_http(
        self,
        message: str,
        context: VoiceContext,
        priority: VoicePriority,
        metadata: Optional[Dict[str, Any]],
        correlation_id: str
    ) -> Tuple[bool, str]:
        """Announce via HTTP API."""
        if not self._session:
            return False, "http_not_available"

        try:
            async with self._session.post(
                f"{self.config.voice_api_url}/announce",
                params={
                    "message": message,
                    "context": context.value,
                    "priority": priority.name,
                    "source": self.source,
                    "correlation_id": correlation_id,
                }
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    return data.get("status") == "success", data.get("reason", "http_success")
                else:
                    return False, f"http_error_{response.status}"

        except Exception as e:
            return False, f"http_error: {e}"

    # =========================================================================
    # Convenience Methods for Common Announcements
    # =========================================================================

    async def announce_model_loaded(
        self,
        model_name: str,
        load_time_seconds: Optional[float] = None
    ) -> Tuple[bool, str]:
        """Announce model loaded (for JARVIS Prime)."""
        if load_time_seconds:
            message = (
                f"JARVIS Prime: {model_name} loaded in {load_time_seconds:.1f} seconds. "
                f"Ready for edge processing."
            )
        else:
            message = (
                f"JARVIS Prime: {model_name} loaded and ready for edge processing."
            )

        return await self.announce(
            message,
            VoiceContext.STARTUP,
            VoicePriority.HIGH,
            metadata={"model": model_name, "event": "model_loaded"}
        )

    async def announce_inference_complete(
        self,
        model_name: str,
        latency_ms: float
    ) -> Tuple[bool, str]:
        """Announce inference completion (optional, low priority)."""
        return await self.announce(
            f"JARVIS Prime inference complete. {latency_ms:.0f}ms response time.",
            VoiceContext.RUNTIME,
            VoicePriority.LOW,
            metadata={"model": model_name, "latency_ms": latency_ms}
        )

    async def announce_training_started(
        self,
        model_name: str
    ) -> Tuple[bool, str]:
        """Announce training started (for Reactor Core)."""
        return await self.announce(
            f"Reactor Core: Starting training for {model_name}. This may take a while.",
            VoiceContext.NARRATOR,
            VoicePriority.NORMAL,
            metadata={"model": model_name, "event": "training_started"}
        )

    async def announce_training_complete(
        self,
        model_name: str,
        accuracy: Optional[float] = None,
        training_time_minutes: Optional[float] = None
    ) -> Tuple[bool, str]:
        """Announce training completion (for Reactor Core)."""
        parts = [f"Reactor Core: {model_name} training complete."]

        if accuracy is not None:
            parts.append(f"Accuracy: {accuracy:.1%}.")

        if training_time_minutes is not None:
            if training_time_minutes < 60:
                parts.append(f"Training time: {training_time_minutes:.0f} minutes.")
            else:
                hours = training_time_minutes / 60
                parts.append(f"Training time: {hours:.1f} hours.")

        parts.append("Ready for deployment.")

        return await self.announce(
            " ".join(parts),
            VoiceContext.SUCCESS,
            VoicePriority.HIGH,
            metadata={
                "model": model_name,
                "event": "training_complete",
                "accuracy": accuracy,
            }
        )

    async def announce_training_failed(
        self,
        model_name: str,
        error: str
    ) -> Tuple[bool, str]:
        """Announce training failure (for Reactor Core)."""
        return await self.announce(
            f"Reactor Core: {model_name} training failed. Error: {error[:100]}",
            VoiceContext.ALERT,
            VoicePriority.HIGH,
            metadata={"model": model_name, "event": "training_failed", "error": error}
        )

    async def announce_error(
        self,
        error_message: str
    ) -> Tuple[bool, str]:
        """Announce error."""
        return await self.announce(
            f"Alert from {self.source}: {error_message}",
            VoiceContext.ALERT,
            VoicePriority.HIGH,
            metadata={"event": "error"}
        )

    async def announce_ready(self) -> Tuple[bool, str]:
        """Announce component ready."""
        source_name = {
            "jarvis_prime": "JARVIS Prime",
            "reactor_core": "Reactor Core",
        }.get(self.source, self.source)

        return await self.announce(
            f"{source_name} is online and ready.",
            VoiceContext.STARTUP,
            VoicePriority.HIGH,
            metadata={"event": "component_ready"}
        )

    async def close(self):
        """Close the client and cleanup resources."""
        if self._session:
            await self._session.close()
            self._session = None

        logger.info(f"[CrossRepoVoice:{self.source}] Client closed")

    async def __aenter__(self) -> "CrossRepoVoiceClient":
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()


# =============================================================================
# Singleton Factory Functions
# =============================================================================

_clients: Dict[str, CrossRepoVoiceClient] = {}


async def get_voice_client(source: str) -> CrossRepoVoiceClient:
    """Get or create a voice client for a specific source."""
    if source not in _clients:
        client = CrossRepoVoiceClient(source)
        await client.initialize()
        _clients[source] = client

    return _clients[source]


async def get_jarvis_prime_voice() -> CrossRepoVoiceClient:
    """Get voice client for JARVIS Prime."""
    return await get_voice_client("jarvis_prime")


async def get_reactor_core_voice() -> CrossRepoVoiceClient:
    """Get voice client for Reactor Core."""
    return await get_voice_client("reactor_core")


async def close_all_clients():
    """Close all voice clients."""
    for client in _clients.values():
        await client.close()
    _clients.clear()
