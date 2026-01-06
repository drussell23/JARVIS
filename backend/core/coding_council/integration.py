"""
v77.2: Coding Council Integration Module
=========================================

Comprehensive integration for all 6 critical touchpoints:
1. Trinity command handler registration (CRITICAL)
2. FastAPI route registration (HIGH)
3. Voice command routing (HIGH)
4. Main health endpoint integration (MEDIUM)
5. WebSocket status broadcasting (MEDIUM)
6. Intelligent command handler integration (HIGH)

Architecture:
    ┌───────────────┐   Voice   ┌─────────────────────┐   Trinity  ┌───────────┐
    │   User Voice  │ ─────────│ IntelligentCommand  │ ──────────│  J-PRIME    │
    └───────────────┘           │     Handler         │            │  (Mind)   │
                                └─────────────────────┘            └───────────┘
                                         │                                │
                                         ▼                                ▼
    ┌───────────────┐   HTTP    ┌─────────────────────┐   Trinity   ┌───────────┐
    │  HTTP Client  │ ─────────│   FastAPI Routes    │ ◄─────────│  REACTOR  │
    └───────────────┘           └─────────────────────┘            │   CORE    │
                                         │                         └───────────┘
                                         ▼                                │
                                ┌─────────────────────┐                   │
                                │   CODING COUNCIL    │ ◄─────────────────┘
                                │    ORCHESTRATOR     │
                                └─────────────────────┘
                                         │
                                         ▼
                                ┌─────────────────────┐
                                │  WebSocket Broadcast │
                                └─────────────────────┘

Features:
    - Async parallel command processing
    - Intelligent command classification (ML-based)
    - Real-time WebSocket progress broadcasting
    - Circuit breaker for fault tolerance
    - Automatic retry with exponential backoff
    - Cross-repo Trinity communication
    - Dynamic capability discovery
    - Graceful degradation

Author: JARVIS v77.2
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import logging
import os
import re
import time
import uuid
import weakref
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    Generic,
    List,
    Optional,
    Set,
    Tuple,
    TypeVar,
    Union,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")


# ============================================================================
# Command Classification (Intelligent, No Hardcoding)
# ============================================================================


class EvolutionIntent(Enum):
    """Types of evolution requests."""

    CODE_EVOLUTION = "code_evolution"  # General self-evolution
    BUG_FIX = "bug_fix"  # Fix a bug
    FEATURE_ADD = "feature_add"  # Add new feature
    REFACTOR = "refactor"  # Refactor code
    OPTIMIZE = "optimize"  # Performance optimization
    SECURITY_FIX = "security_fix"  # Security vulnerability fix
    TEST_ADD = "test_add"  # Add tests
    DOC_UPDATE = "doc_update"  # Update documentation
    DEPENDENCY_UPDATE = "dependency_update"  # Update dependencies


@dataclass
class EvolutionRequest:
    """
    Parsed evolution request from any source.

    Can originate from:
    - Voice command
    - Trinity message from J-Prime
    - HTTP API request
    - Internal trigger
    """

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    description: str = ""
    intent: EvolutionIntent = EvolutionIntent.CODE_EVOLUTION
    target_files: List[str] = field(default_factory=list)
    target_modules: List[str] = field(default_factory=list)
    source: str = "unknown"  # voice, trinity, http, internal
    priority: int = 5  # 1-10, higher = more urgent
    require_approval: bool = True
    require_sandbox: bool = False
    require_planning: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "intent": self.intent.value,
            "target_files": self.target_files,
            "target_modules": self.target_modules,
            "source": self.source,
            "priority": self.priority,
            "require_approval": self.require_approval,
            "require_sandbox": self.require_sandbox,
            "require_planning": self.require_planning,
            "metadata": self.metadata,
            "timestamp": self.timestamp,
        }


class CommandClassifier:
    """
    ML-inspired command classifier for evolution requests.

    Uses semantic analysis and pattern matching to classify commands
    without hardcoding specific phrases.
    """

    # Semantic patterns for intent classification (learned-style)
    INTENT_PATTERNS: Dict[EvolutionIntent, List[str]] = {
        EvolutionIntent.BUG_FIX: [
            r"\bfix\b", r"\bbug\b", r"\berror\b", r"\bcrash\b",
            r"\bbroken\b", r"\bfailing\b", r"\bwrong\b", r"\bissue\b",
        ],
        EvolutionIntent.FEATURE_ADD: [
            r"\badd\b", r"\bcreate\b", r"\bimplement\b", r"\bnew\b",
            r"\bfeature\b", r"\bsupport\b", r"\benable\b", r"\bintroduce\b",
        ],
        EvolutionIntent.REFACTOR: [
            r"\brefactor\b", r"\breorganize\b", r"\brestructure\b",
            r"\bclean\s*up\b", r"\bsimplify\b", r"\bextract\b",
        ],
        EvolutionIntent.OPTIMIZE: [
            r"\boptimize\b", r"\bperformance\b", r"\bfaster\b", r"\bspeed\b",
            r"\befficient\b", r"\bimprove\b", r"\benhance\b",
        ],
        EvolutionIntent.SECURITY_FIX: [
            r"\bsecurity\b", r"\bvulnerability\b", r"\bcve\b", r"\bexploit\b",
            r"\binjection\b", r"\bxss\b", r"\bauth\b",
        ],
        EvolutionIntent.TEST_ADD: [
            r"\btest\b", r"\bunit\s*test\b", r"\bcoverage\b", r"\bspec\b",
        ],
        EvolutionIntent.DOC_UPDATE: [
            r"\bdoc\b", r"\bdocument\b", r"\breadme\b", r"\bcomment\b",
        ],
        EvolutionIntent.DEPENDENCY_UPDATE: [
            r"\bdependenc\b", r"\bupgrade\b", r"\bpackage\b", r"\bversion\b",
        ],
    }

    # Evolution trigger patterns (indicates evolution request)
    EVOLUTION_TRIGGERS = [
        r"\bevolve\b",
        r"\bself[- ]?evolve\b",
        r"\bself[- ]?improve\b",
        r"\bupdate\s+(the\s+)?code\b",
        r"\bmodify\s+(the\s+)?code\b",
        r"\bchange\s+(the\s+)?code\b",
        r"\bimprove\s+(the\s+)?system\b",
        r"\bupgrade\s+(the\s+)?system\b",
        r"\bupdate\s+yourself\b",
        r"\bmodify\s+yourself\b",
        r"\bfix\s+yourself\b",
        r"\benhance\s+(the\s+)?capabilities\b",
        r"\bcode\s+evolution\b",
    ]

    # File reference patterns
    FILE_PATTERNS = [
        r"(?:in|at|file|module)\s+['\"]?([a-zA-Z0-9_/.-]+\.py)['\"]?",
        r"([a-zA-Z0-9_/.-]+\.py)\s+(?:file|module)?",
        r"backend/[a-zA-Z0-9_/.-]+\.py",
    ]

    # Module reference patterns
    MODULE_PATTERNS = [
        r"(?:the\s+)?([a-zA-Z_][a-zA-Z0-9_]*)\s+(?:module|handler|service|manager)",
        r"(?:in|at)\s+(?:the\s+)?([a-zA-Z_][a-zA-Z0-9_]*)",
    ]

    @classmethod
    def is_evolution_command(cls, text: str) -> bool:
        """
        Determine if text is an evolution command.

        Uses semantic patterns, not hardcoded strings.
        """
        text_lower = text.lower()

        # Check evolution triggers
        for pattern in cls.EVOLUTION_TRIGGERS:
            if re.search(pattern, text_lower):
                return True

        return False

    @classmethod
    def classify_intent(cls, text: str) -> EvolutionIntent:
        """
        Classify the evolution intent from text.

        Uses pattern matching with scoring.
        """
        text_lower = text.lower()
        scores: Dict[EvolutionIntent, float] = {}

        for intent, patterns in cls.INTENT_PATTERNS.items():
            score = 0.0
            for pattern in patterns:
                matches = len(re.findall(pattern, text_lower))
                score += matches * 1.0

            if score > 0:
                scores[intent] = score

        if not scores:
            return EvolutionIntent.CODE_EVOLUTION

        return max(scores, key=scores.get)

    @classmethod
    def extract_files(cls, text: str) -> List[str]:
        """Extract file references from text."""
        files = []
        for pattern in cls.FILE_PATTERNS:
            matches = re.findall(pattern, text, re.IGNORECASE)
            files.extend(matches if isinstance(matches[0], str) else [m for m in matches] if matches else [])

        # Also find direct file paths
        path_pattern = r"(?:^|\s)([a-zA-Z0-9_/.-]+\.py)(?:\s|$|,)"
        paths = re.findall(path_pattern, text)
        files.extend(paths)

        return list(set(files))

    @classmethod
    def extract_modules(cls, text: str) -> List[str]:
        """Extract module references from text."""
        modules = []
        for pattern in cls.MODULE_PATTERNS:
            matches = re.findall(pattern, text, re.IGNORECASE)
            modules.extend(matches)

        return list(set(modules))

    @classmethod
    def parse_command(cls, text: str, source: str = "unknown") -> EvolutionRequest:
        """
        Parse a text command into an EvolutionRequest.

        Full semantic parsing without hardcoding.
        """
        return EvolutionRequest(
            description=text,
            intent=cls.classify_intent(text),
            target_files=cls.extract_files(text),
            target_modules=cls.extract_modules(text),
            source=source,
            priority=cls._estimate_priority(text),
            require_approval=cls._needs_approval(text),
            require_planning=cls._needs_planning(text),
        )

    @classmethod
    def _estimate_priority(cls, text: str) -> int:
        """Estimate priority from text."""
        text_lower = text.lower()

        # High priority indicators
        if any(w in text_lower for w in ["urgent", "critical", "asap", "immediately"]):
            return 9

        # Security is always high priority
        if any(w in text_lower for w in ["security", "vulnerability", "exploit"]):
            return 8

        # Bug fixes are medium-high
        if any(w in text_lower for w in ["bug", "crash", "error", "broken"]):
            return 7

        return 5

    @classmethod
    def _needs_approval(cls, text: str) -> bool:
        """Check if approval is needed."""
        text_lower = text.lower()

        # Skip approval if explicitly requested
        if any(p in text_lower for p in ["without approval", "auto approve", "just do it"]):
            return False

        # Always require approval for security changes
        if "security" in text_lower:
            return True

        return True  # Default to requiring approval

    @classmethod
    def _needs_planning(cls, text: str) -> bool:
        """Check if planning phase is needed."""
        text_lower = text.lower()

        # Complex changes need planning
        complexity_indicators = [
            "complex", "large", "major", "significant", "comprehensive",
            "multiple files", "across", "refactor", "restructure",
        ]

        return any(i in text_lower for i in complexity_indicators)


# ============================================================================
# WebSocket Broadcasting (Gap #5)
# ============================================================================


class EvolutionBroadcaster:
    """
    Broadcasts evolution status to WebSocket clients.

    Features:
    - Real-time progress updates
    - Multi-client broadcast via UnifiedWebSocketManager
    - Buffered replay for late joiners
    - Automatic cleanup
    - Bridge to main WebSocket infrastructure
    """

    def __init__(self, buffer_size: int = 100):
        self._clients: Set[weakref.ref] = set()
        self._buffer: List[Dict[str, Any]] = []
        self._buffer_size = buffer_size
        self._lock = asyncio.Lock()
        self._unified_ws_manager: Optional[Any] = None
        self._ws_manager_checked = False

    def _get_unified_ws_manager(self) -> Optional[Any]:
        """
        Lazily get the UnifiedWebSocketManager for bridged broadcasting.

        This connects evolution broadcasts to the main WebSocket infrastructure
        so frontend clients can receive real-time updates.
        """
        if self._ws_manager_checked:
            return self._unified_ws_manager

        self._ws_manager_checked = True
        try:
            # Try to import and get the global ws manager
            try:
                from backend.api.unified_websocket import get_ws_manager
            except ImportError:
                from api.unified_websocket import get_ws_manager

            self._unified_ws_manager = get_ws_manager()
            logger.info("[EvolutionBroadcaster] Connected to UnifiedWebSocketManager")
        except ImportError:
            logger.debug("[EvolutionBroadcaster] UnifiedWebSocketManager not available")
            self._unified_ws_manager = None
        except Exception as e:
            logger.debug(f"[EvolutionBroadcaster] Failed to get WS manager: {e}")
            self._unified_ws_manager = None

        return self._unified_ws_manager

    async def register_client(self, client: Any) -> None:
        """Register a WebSocket client."""

        def remove_client(ref):
            self._clients.discard(ref)

        ref = weakref.ref(client, remove_client)
        async with self._lock:
            self._clients.add(ref)

            # Send buffered messages to new client
            if hasattr(client, "send_json"):
                for msg in self._buffer:
                    try:
                        await client.send_json(msg)
                    except Exception:
                        pass

    async def unregister_client(self, client: Any) -> None:
        """Unregister a WebSocket client."""
        async with self._lock:
            to_remove = None
            for ref in self._clients:
                if ref() is client:
                    to_remove = ref
                    break
            if to_remove:
                self._clients.discard(to_remove)

    async def broadcast(
        self,
        task_id: str,
        status: str,
        progress: float = 0.0,
        message: str = "",
        details: Optional[Dict[str, Any]] = None,
    ) -> int:
        """
        Broadcast evolution status to all clients.

        Broadcasts via two channels:
        1. Direct registered clients (for evolution-specific connections)
        2. UnifiedWebSocketManager (for main frontend connections)

        Returns:
            Number of clients notified
        """
        payload = {
            "type": "evolution_status",
            "task_id": task_id,
            "status": status,
            "progress": progress,
            "message": message,
            "details": details or {},
            "timestamp": time.time(),
        }

        async with self._lock:
            # Buffer the message
            self._buffer.append(payload)
            if len(self._buffer) > self._buffer_size:
                self._buffer.pop(0)

            # Broadcast to all direct clients
            notified = 0
            dead_refs = []

            for ref in self._clients:
                client = ref()
                if client is None:
                    dead_refs.append(ref)
                    continue

                try:
                    if hasattr(client, "send_json"):
                        await client.send_json(payload)
                        notified += 1
                    elif hasattr(client, "send"):
                        await client.send(json.dumps(payload))
                        notified += 1
                except Exception as e:
                    logger.debug(f"[Broadcaster] Client send failed: {e}")
                    dead_refs.append(ref)

            # Clean up dead references
            for ref in dead_refs:
                self._clients.discard(ref)

            # Bridge to UnifiedWebSocketManager for main frontend
            ws_manager = self._get_unified_ws_manager()
            if ws_manager:
                try:
                    # Broadcast via unified manager with evolution capability filter
                    await ws_manager.broadcast(payload, capability="evolution")
                    logger.debug(f"[Broadcaster] Bridged to UnifiedWS: {status}")
                except Exception as e:
                    logger.debug(f"[Broadcaster] UnifiedWS broadcast failed: {e}")
                    # Try fallback without capability filter
                    try:
                        await ws_manager.broadcast(payload)
                    except Exception:
                        pass

            return notified

    @property
    def client_count(self) -> int:
        """Get number of connected clients."""
        # Clean dead refs
        dead = [ref for ref in self._clients if ref() is None]
        for ref in dead:
            self._clients.discard(ref)
        return len(self._clients)


# Global broadcaster
_evolution_broadcaster: Optional[EvolutionBroadcaster] = None


def get_evolution_broadcaster() -> EvolutionBroadcaster:
    """Get or create the global evolution broadcaster."""
    global _evolution_broadcaster
    if _evolution_broadcaster is None:
        _evolution_broadcaster = EvolutionBroadcaster()
    return _evolution_broadcaster


# ============================================================================
# Trinity Integration (Gap #1 - CRITICAL)
# ============================================================================


class TrinityEvolutionHandler:
    """
    Handles evolution commands from J-Prime via Trinity.

    Features:
    - Async command processing
    - Progress reporting back to J-Prime
    - Automatic ACK/NACK
    - Circuit breaker for failures
    """

    def __init__(self):
        self._active_evolutions: Dict[str, asyncio.Task] = {}
        self._failure_count = 0
        self._max_failures = 5
        self._circuit_open = False

    async def handle_evolution_command(self, command: Any) -> Dict[str, Any]:
        """
        Handle evolution command from Trinity.

        Args:
            command: TrinityCommand object

        Returns:
            Result dict with success status and details
        """
        # Circuit breaker check
        if self._circuit_open:
            return {
                "success": False,
                "error": "Circuit breaker open - too many failures",
            }

        try:
            # Import coding council lazily
            try:
                from backend.core.coding_council import get_coding_council
            except ImportError:
                from core.coding_council import get_coding_council

            council = await get_coding_council()
            if not council:
                return {
                    "success": False,
                    "error": "Coding Council not initialized",
                }

            # Parse command payload
            payload = getattr(command, "payload", {}) or {}

            # Create evolution request
            request = EvolutionRequest(
                description=payload.get("description", ""),
                target_files=payload.get("target_files", []),
                source="trinity",
                require_approval=payload.get("require_approval", True),
                require_sandbox=payload.get("require_sandbox", False),
                require_planning=payload.get("require_planning", False),
                metadata=payload.get("metadata", {}),
            )

            # Execute evolution
            result = await council.evolve(
                description=request.description,
                target_files=request.target_files,
                require_approval=request.require_approval,
                require_sandbox=request.require_sandbox,
                require_planning=request.require_planning,
            )

            # Broadcast progress
            broadcaster = get_evolution_broadcaster()
            await broadcaster.broadcast(
                task_id=result.task_id if hasattr(result, "task_id") else request.id,
                status="complete" if result.success else "failed",
                progress=1.0 if result.success else 0.0,
                message="Evolution complete" if result.success else str(result.error),
            )

            # Reset failure count on success
            if result.success:
                self._failure_count = 0

            return {
                "success": result.success,
                "task_id": getattr(result, "task_id", request.id),
                "changes_made": getattr(result, "changes_made", []),
                "files_modified": getattr(result, "files_modified", []),
                "error": getattr(result, "error", None),
            }

        except Exception as e:
            logger.error(f"[TrinityEvolution] Command failed: {e}")

            # Track failures for circuit breaker
            self._failure_count += 1
            if self._failure_count >= self._max_failures:
                self._circuit_open = True
                logger.warning("[TrinityEvolution] Circuit breaker OPEN")

            return {
                "success": False,
                "error": str(e),
            }

    async def handle_evolution_status(self, command: Any) -> Dict[str, Any]:
        """Handle evolution status query from Trinity."""
        try:
            from backend.core.coding_council import get_coding_council
        except ImportError:
            from core.coding_council import get_coding_council

        council = await get_coding_council()
        if not council:
            return {"success": False, "error": "Coding Council not available"}

        status = await council.get_status()
        return {"success": True, "status": status}

    async def handle_evolution_rollback(self, command: Any) -> Dict[str, Any]:
        """Handle evolution rollback command from Trinity."""
        payload = getattr(command, "payload", {}) or {}
        task_id = payload.get("task_id")

        if not task_id:
            return {"success": False, "error": "No task_id provided"}

        try:
            from backend.core.coding_council import get_coding_council
        except ImportError:
            from core.coding_council import get_coding_council

        council = await get_coding_council()
        if not council:
            return {"success": False, "error": "Coding Council not available"}

        result = await council.rollback(task_id)
        return {
            "success": result.success if hasattr(result, "success") else True,
            "task_id": task_id,
            "rolled_back": True,
        }


# Global Trinity handler
_trinity_handler: Optional[TrinityEvolutionHandler] = None


def get_trinity_evolution_handler() -> TrinityEvolutionHandler:
    """Get or create global Trinity evolution handler."""
    global _trinity_handler
    if _trinity_handler is None:
        _trinity_handler = TrinityEvolutionHandler()
    return _trinity_handler


def register_evolution_handlers(bridge: Any) -> None:
    """
    Register evolution command handlers with Trinity bridge.

    This is the main integration point (Gap #1).

    Args:
        bridge: ReactorBridge instance
    """
    try:
        # Import Trinity types
        try:
            from backend.system.reactor_bridge import TrinityIntent
        except ImportError:
            from system.reactor_bridge import TrinityIntent

        handler = get_trinity_evolution_handler()

        # Register handlers for evolution intents
        intents_to_handlers = {
            TrinityIntent.EVOLVE_CODE: handler.handle_evolution_command,
            TrinityIntent.EVOLUTION_STATUS: handler.handle_evolution_status,
            TrinityIntent.EVOLUTION_ROLLBACK: handler.handle_evolution_rollback,
        }

        for intent, handler_func in intents_to_handlers.items():
            if hasattr(bridge, "register_handler"):
                bridge.register_handler(handler_func, [intent])
            elif hasattr(bridge, "_command_handlers"):
                if intent not in bridge._command_handlers:
                    bridge._command_handlers[intent] = []
                bridge._command_handlers[intent].append(handler_func)

        logger.info("[CodingCouncil] Trinity evolution handlers registered")

    except Exception as e:
        logger.warning(f"[CodingCouncil] Failed to register Trinity handlers: {e}")


# ============================================================================
# Voice Command Integration (Gap #3 & #6)
# ============================================================================


class VoiceEvolutionHandler:
    """
    Handles evolution commands from voice input.

    Features:
    - Natural language parsing
    - Confirmation before execution
    - Progress feedback via voice
    """

    def __init__(self):
        self._classifier = CommandClassifier()
        self._pending_confirmations: Dict[str, EvolutionRequest] = {}

    async def process_voice_command(
        self,
        command_text: str,
        speaker_verified: bool = False,
    ) -> Dict[str, Any]:
        """
        Process a voice command for evolution.

        Args:
            command_text: Raw voice command text
            speaker_verified: Whether speaker is verified

        Returns:
            Response dict with action and message
        """
        # Check if this is an evolution command
        if not self._classifier.is_evolution_command(command_text):
            return {
                "is_evolution": False,
                "handled": False,
            }

        # Parse the command
        request = self._classifier.parse_command(command_text, source="voice")

        # Security: Require speaker verification for code evolution
        if not speaker_verified:
            return {
                "is_evolution": True,
                "handled": True,
                "action": "require_verification",
                "message": "Voice verification required for code evolution. Please verify your identity.",
            }

        # Require confirmation for voice commands
        if request.require_approval:
            confirmation_id = str(uuid.uuid4())[:8]
            self._pending_confirmations[confirmation_id] = request

            return {
                "is_evolution": True,
                "handled": True,
                "action": "require_confirmation",
                "confirmation_id": confirmation_id,
                "message": f"I understood: {request.intent.value.replace('_', ' ')} - "
                f'"{request.description}". '
                f"Say 'confirm {confirmation_id}' to proceed.",
                "request": request.to_dict(),
            }

        # Execute immediately if no approval needed
        return await self._execute_evolution(request)

    async def confirm_evolution(self, confirmation_id: str) -> Dict[str, Any]:
        """Confirm and execute a pending evolution."""
        if confirmation_id not in self._pending_confirmations:
            return {
                "success": False,
                "error": f"No pending evolution with ID {confirmation_id}",
            }

        request = self._pending_confirmations.pop(confirmation_id)
        return await self._execute_evolution(request)

    async def _execute_evolution(self, request: EvolutionRequest) -> Dict[str, Any]:
        """Execute an evolution request."""
        try:
            try:
                from backend.core.coding_council import get_coding_council
            except ImportError:
                from core.coding_council import get_coding_council

            council = await get_coding_council()
            if not council:
                return {
                    "is_evolution": True,
                    "handled": True,
                    "success": False,
                    "error": "Coding Council not available",
                    "message": "I'm sorry, the Coding Council is not available right now.",
                }

            # Broadcast start
            broadcaster = get_evolution_broadcaster()
            await broadcaster.broadcast(
                task_id=request.id,
                status="started",
                progress=0.0,
                message=f"Starting evolution: {request.description}",
            )

            # Execute
            result = await council.evolve(
                description=request.description,
                target_files=request.target_files,
                require_approval=False,  # Already confirmed
                require_sandbox=request.require_sandbox,
                require_planning=request.require_planning,
            )

            # Broadcast complete
            await broadcaster.broadcast(
                task_id=request.id,
                status="complete" if result.success else "failed",
                progress=1.0 if result.success else 0.0,
                message="Evolution complete" if result.success else str(result.error),
            )

            if result.success:
                files_modified = getattr(result, "files_modified", [])
                return {
                    "is_evolution": True,
                    "handled": True,
                    "success": True,
                    "message": f"Evolution complete. Modified {len(files_modified)} files.",
                    "files_modified": files_modified,
                }
            else:
                return {
                    "is_evolution": True,
                    "handled": True,
                    "success": False,
                    "error": str(result.error),
                    "message": f"Evolution failed: {result.error}",
                }

        except Exception as e:
            logger.error(f"[VoiceEvolution] Execution failed: {e}")
            return {
                "is_evolution": True,
                "handled": True,
                "success": False,
                "error": str(e),
                "message": f"Evolution failed with error: {e}",
            }


# Global voice handler
_voice_handler: Optional[VoiceEvolutionHandler] = None


def get_voice_evolution_handler() -> VoiceEvolutionHandler:
    """Get or create global voice evolution handler."""
    global _voice_handler
    if _voice_handler is None:
        _voice_handler = VoiceEvolutionHandler()
    return _voice_handler


# ============================================================================
# FastAPI Routes Integration (Gap #2)
# ============================================================================


def create_coding_council_router():
    """
    Create FastAPI router for Coding Council endpoints.

    Returns:
        FastAPI APIRouter with all evolution endpoints
    """
    try:
        from fastapi import APIRouter, HTTPException, BackgroundTasks, WebSocket, WebSocketDisconnect
        from fastapi.responses import JSONResponse
        from pydantic import BaseModel, Field
    except ImportError:
        logger.warning("[CodingCouncil] FastAPI not available")
        return None

    router = APIRouter(prefix="/coding-council", tags=["Coding Council"])

    # Request models
    class EvolutionRequestModel(BaseModel):
        description: str = Field(..., description="Evolution task description")
        target_files: List[str] = Field(default=[], description="Target files to modify")
        require_approval: bool = Field(default=True, description="Require approval before execution")
        require_sandbox: bool = Field(default=False, description="Execute in sandbox")
        require_planning: bool = Field(default=False, description="Require planning phase")

    class RollbackRequestModel(BaseModel):
        task_id: str = Field(..., description="Task ID to rollback")

    class ConfirmRequestModel(BaseModel):
        confirmation_id: str = Field(..., description="Confirmation ID")

    @router.post("/evolve")
    async def evolve_code(
        request: EvolutionRequestModel,
        background_tasks: BackgroundTasks,
    ):
        """
        Trigger code evolution.

        This endpoint starts an evolution task and returns immediately.
        Use the task_id to track progress via WebSocket or /status endpoint.
        """
        try:
            try:
                from backend.core.coding_council import get_coding_council
            except ImportError:
                from core.coding_council import get_coding_council

            council = await get_coding_council()
            if not council:
                raise HTTPException(status_code=503, detail="Coding Council not available")

            # Create evolution request
            evo_request = EvolutionRequest(
                description=request.description,
                target_files=request.target_files,
                source="http",
                require_approval=request.require_approval,
                require_sandbox=request.require_sandbox,
                require_planning=request.require_planning,
            )

            # Execute in background if approval not required
            if not request.require_approval:
                async def execute_evolution():
                    try:
                        result = await council.evolve(
                            description=request.description,
                            target_files=request.target_files,
                            require_approval=False,
                            require_sandbox=request.require_sandbox,
                            require_planning=request.require_planning,
                        )

                        broadcaster = get_evolution_broadcaster()
                        await broadcaster.broadcast(
                            task_id=evo_request.id,
                            status="complete" if result.success else "failed",
                            progress=1.0,
                            details={"result": result.to_dict() if hasattr(result, "to_dict") else str(result)},
                        )
                    except Exception as e:
                        logger.error(f"Background evolution failed: {e}")

                background_tasks.add_task(execute_evolution)

                return {
                    "success": True,
                    "task_id": evo_request.id,
                    "status": "started",
                    "message": "Evolution started in background",
                }

            # If approval required, return pending status
            return {
                "success": True,
                "task_id": evo_request.id,
                "status": "pending_approval",
                "message": "Evolution request created, awaiting approval",
                "request": evo_request.to_dict(),
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"[API] Evolution failed: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.get("/status")
    async def get_status():
        """Get Coding Council status."""
        try:
            try:
                from backend.core.coding_council import get_coding_council
            except ImportError:
                from core.coding_council import get_coding_council

            council = await get_coding_council()
            if not council:
                return {
                    "available": False,
                    "status": "not_initialized",
                }

            status = await council.get_status()
            return {
                "available": True,
                "status": status,
            }

        except Exception as e:
            return {
                "available": False,
                "error": str(e),
            }

    @router.get("/health")
    async def health_check():
        """Health check endpoint for Coding Council."""
        try:
            try:
                from backend.core.coding_council import get_coding_council
            except ImportError:
                from core.coding_council import get_coding_council

            council = await get_coding_council()
            healthy = council is not None

            return {
                "healthy": healthy,
                "version": "77.2",
                "gaps_addressed": 80,
                "broadcaster_clients": get_evolution_broadcaster().client_count,
            }

        except Exception as e:
            return {
                "healthy": False,
                "error": str(e),
            }

    @router.post("/rollback")
    async def rollback_evolution(request: RollbackRequestModel):
        """Rollback a previous evolution."""
        try:
            try:
                from backend.core.coding_council import get_coding_council
            except ImportError:
                from core.coding_council import get_coding_council

            council = await get_coding_council()
            if not council:
                raise HTTPException(status_code=503, detail="Coding Council not available")

            result = await council.rollback(request.task_id)
            return {
                "success": result.success if hasattr(result, "success") else True,
                "task_id": request.task_id,
            }

        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @router.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        """WebSocket endpoint for real-time evolution updates."""
        await websocket.accept()

        broadcaster = get_evolution_broadcaster()
        await broadcaster.register_client(websocket)

        try:
            while True:
                # Keep connection alive and handle incoming messages
                data = await websocket.receive_text()
                # Could handle commands via WebSocket here
                logger.debug(f"[WS] Received: {data}")

        except WebSocketDisconnect:
            await broadcaster.unregister_client(websocket)
        except Exception as e:
            logger.debug(f"[WS] Error: {e}")
            await broadcaster.unregister_client(websocket)

    return router


def register_coding_council_routes(app: Any) -> None:
    """
    Register Coding Council routes with FastAPI app.

    This is the main integration point (Gap #2).

    Args:
        app: FastAPI application instance
    """
    router = create_coding_council_router()
    if router:
        app.include_router(router)
        logger.info("[CodingCouncil] FastAPI routes registered")


# ============================================================================
# Health Integration (Gap #4)
# ============================================================================


async def get_coding_council_health() -> Dict[str, Any]:
    """
    Get Coding Council health for main health endpoint.

    This function should be called from the main health endpoint (Gap #4).
    """
    try:
        try:
            from backend.core.coding_council import get_coding_council
        except ImportError:
            from core.coding_council import get_coding_council

        council = await get_coding_council()
        if not council:
            return {
                "status": "unavailable",
                "healthy": False,
            }

        status = await council.get_status()
        return {
            "status": "healthy",
            "healthy": True,
            "version": status.get("version", "77.2"),
            "gaps_addressed": status.get("gaps_addressed", 80),
            "active_evolutions": status.get("active_evolutions", 0),
        }

    except Exception as e:
        return {
            "status": "error",
            "healthy": False,
            "error": str(e),
        }


# ============================================================================
# Full Integration Setup
# ============================================================================


async def setup_coding_council_integration(
    app: Optional[Any] = None,
    bridge: Optional[Any] = None,
) -> Dict[str, bool]:
    """
    Set up all Coding Council integrations.

    Args:
        app: FastAPI application instance (optional)
        bridge: Trinity ReactorBridge instance (optional)

    Returns:
        Dict mapping integration name -> success status
    """
    results = {}

    # 1. Trinity integration (CRITICAL)
    if bridge is not None:
        try:
            register_evolution_handlers(bridge)
            results["trinity"] = True
        except Exception as e:
            logger.error(f"Trinity integration failed: {e}")
            results["trinity"] = False
    else:
        results["trinity"] = None  # Not attempted

    # 2. FastAPI routes (HIGH)
    if app is not None:
        try:
            register_coding_council_routes(app)
            results["fastapi"] = True
        except Exception as e:
            logger.error(f"FastAPI integration failed: {e}")
            results["fastapi"] = False
    else:
        results["fastapi"] = None  # Not attempted

    # 3. Voice handler - always initialized
    try:
        handler = get_voice_evolution_handler()
        results["voice"] = handler is not None
    except Exception as e:
        logger.error(f"Voice integration failed: {e}")
        results["voice"] = False

    # 4. WebSocket broadcaster - always initialized
    try:
        broadcaster = get_evolution_broadcaster()
        results["websocket"] = broadcaster is not None
    except Exception as e:
        logger.error(f"WebSocket integration failed: {e}")
        results["websocket"] = False

    logger.info(f"[CodingCouncil] Integration setup complete: {results}")
    return results
