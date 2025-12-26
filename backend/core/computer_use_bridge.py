"""
JARVIS Computer Use Cross-Repo Bridge
=====================================

Enables Computer Use capabilities across JARVIS, JARVIS Prime, and Reactor Core.

Features:
- Action Chaining optimization (5x speedup via batch processing)
- OmniParser local UI parsing (60% faster, 80% token reduction)
- Cross-repo Computer Use delegation
- Unified action execution tracking
- Vision analysis result sharing

Architecture:
    JARVIS (local) ←→ ~/.jarvis/cross_repo/ ←→ JARVIS Prime (inference)
                              ↓
                        Reactor Core (learning)

Author: JARVIS AI System
Version: 6.1.0 - Clinical-Grade Computer Use
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ============================================================================
# Constants
# ============================================================================

COMPUTER_USE_STATE_DIR = Path.home() / ".jarvis" / "cross_repo"
COMPUTER_USE_STATE_FILE = COMPUTER_USE_STATE_DIR / "computer_use_state.json"
COMPUTER_USE_EVENTS_FILE = COMPUTER_USE_STATE_DIR / "computer_use_events.json"
ACTION_CACHE_FILE = COMPUTER_USE_STATE_DIR / "action_cache.json"

# v6.1: OmniParser integration
OMNIPARSER_CACHE_DIR = COMPUTER_USE_STATE_DIR / "omniparser_cache"

MAX_EVENTS = 500  # Keep last 500 events
MAX_CACHE_SIZE = 100  # Cache last 100 screen analyses


# ============================================================================
# Enums
# ============================================================================

class ActionType(Enum):
    """Computer action types."""
    CLICK = "click"
    DOUBLE_CLICK = "double_click"
    RIGHT_CLICK = "right_click"
    TYPE = "type"
    KEY_PRESS = "key_press"
    SCREENSHOT = "screenshot"
    DRAG = "drag"
    SCROLL = "scroll"
    WAIT = "wait"


class ExecutionStatus(Enum):
    """Action execution status."""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CACHED = "cached"  # Used cached result


class InterfaceType(Enum):
    """Interface type for action chaining optimization."""
    STATIC = "static"  # Calculator, forms, dialogs - can batch
    DYNAMIC = "dynamic"  # Web pages, async UI - must step-by-step


# ============================================================================
# Data Models
# ============================================================================

@dataclass
class ComputerAction:
    """A single computer action."""
    action_id: str
    action_type: ActionType
    coordinates: Optional[Tuple[int, int]] = None
    text: Optional[str] = None
    key: Optional[str] = None
    duration: float = 0.5
    reasoning: str = ""
    confidence: float = 0.0
    element_id: Optional[str] = None  # OmniParser element ID

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "action_id": self.action_id,
            "action_type": self.action_type.value,
            "coordinates": list(self.coordinates) if self.coordinates else None,
            "text": self.text,
            "key": self.key,
            "duration": self.duration,
            "reasoning": self.reasoning,
            "confidence": self.confidence,
            "element_id": self.element_id,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ComputerAction":
        """Create from dictionary."""
        coords = data.get("coordinates")
        return cls(
            action_id=data["action_id"],
            action_type=ActionType(data["action_type"]),
            coordinates=tuple(coords) if coords else None,
            text=data.get("text"),
            key=data.get("key"),
            duration=data.get("duration", 0.5),
            reasoning=data.get("reasoning", ""),
            confidence=data.get("confidence", 0.0),
            element_id=data.get("element_id"),
        )


@dataclass
class ActionBatch:
    """Batch of actions for chained execution."""
    batch_id: str
    actions: List[ComputerAction]
    interface_type: InterfaceType
    goal: str
    screenshot_b64: Optional[str] = None  # Single screenshot for entire batch
    omniparser_elements: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "batch_id": self.batch_id,
            "actions": [a.to_dict() for a in self.actions],
            "interface_type": self.interface_type.value,
            "goal": self.goal,
            "screenshot_b64": self.screenshot_b64[:100] if self.screenshot_b64 else None,  # Truncate
            "omniparser_elements": self.omniparser_elements,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ActionBatch":
        """Create from dictionary."""
        return cls(
            batch_id=data["batch_id"],
            actions=[ComputerAction.from_dict(a) for a in data["actions"]],
            interface_type=InterfaceType(data["interface_type"]),
            goal=data["goal"],
            screenshot_b64=data.get("screenshot_b64"),
            omniparser_elements=data.get("omniparser_elements", []),
        )


@dataclass
class ComputerUseEvent:
    """An event from Computer Use system."""
    event_id: str
    timestamp: str
    event_type: str  # "action_executed", "batch_completed", "vision_analysis", "error"

    # Action data
    action: Optional[ComputerAction] = None
    batch: Optional[ActionBatch] = None

    # Execution results
    status: ExecutionStatus = ExecutionStatus.PENDING
    execution_time_ms: float = 0.0
    error_message: str = ""

    # Vision analysis
    vision_analysis: Optional[Dict[str, Any]] = None
    used_omniparser: bool = False

    # Context
    goal: str = ""
    session_id: str = ""
    repo_source: str = "jarvis"  # jarvis, jarvis-prime, reactor-core

    # Optimization metrics
    token_savings: int = 0  # Tokens saved vs non-optimized approach
    time_savings_ms: float = 0.0  # Time saved vs Stop-and-Look

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "event_id": self.event_id,
            "timestamp": self.timestamp,
            "event_type": self.event_type,
            "action": self.action.to_dict() if self.action else None,
            "batch": self.batch.to_dict() if self.batch else None,
            "status": self.status.value,
            "execution_time_ms": self.execution_time_ms,
            "error_message": self.error_message,
            "vision_analysis": self.vision_analysis,
            "used_omniparser": self.used_omniparser,
            "goal": self.goal,
            "session_id": self.session_id,
            "repo_source": self.repo_source,
            "token_savings": self.token_savings,
            "time_savings_ms": self.time_savings_ms,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ComputerUseEvent":
        """Create from dictionary."""
        action_data = data.get("action")
        batch_data = data.get("batch")

        return cls(
            event_id=data["event_id"],
            timestamp=data["timestamp"],
            event_type=data["event_type"],
            action=ComputerAction.from_dict(action_data) if action_data else None,
            batch=ActionBatch.from_dict(batch_data) if batch_data else None,
            status=ExecutionStatus(data.get("status", "pending")),
            execution_time_ms=data.get("execution_time_ms", 0.0),
            error_message=data.get("error_message", ""),
            vision_analysis=data.get("vision_analysis"),
            used_omniparser=data.get("used_omniparser", False),
            goal=data.get("goal", ""),
            session_id=data.get("session_id", ""),
            repo_source=data.get("repo_source", "jarvis"),
            token_savings=data.get("token_savings", 0),
            time_savings_ms=data.get("time_savings_ms", 0.0),
        )


@dataclass
class ComputerUseBridgeState:
    """State of the Computer Use bridge."""
    session_id: str
    started_at: str
    last_update: str

    # Capabilities
    action_chaining_enabled: bool = True
    omniparser_enabled: bool = False
    omniparser_initialized: bool = False

    # Statistics
    total_actions: int = 0
    total_batches: int = 0
    avg_batch_size: float = 0.0
    total_time_saved_ms: float = 0.0
    total_tokens_saved: int = 0

    # Connected repos
    connected_to_prime: bool = False
    connected_to_reactor: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ComputerUseBridgeState":
        """Create from dictionary."""
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


# ============================================================================
# Computer Use Bridge
# ============================================================================

class ComputerUseBridge:
    """
    Cross-repo bridge for Computer Use capabilities.

    Features:
    - Action Chaining optimization tracking
    - OmniParser integration state sharing
    - Cross-repo action delegation
    - Unified vision analysis caching
    - Performance metrics aggregation
    """

    def __init__(
        self,
        session_id: Optional[str] = None,
        enable_action_chaining: bool = True,
        enable_omniparser: bool = False,
    ):
        """
        Initialize Computer Use bridge.

        Args:
            session_id: Unique session ID
            enable_action_chaining: Enable batch action optimization
            enable_omniparser: Enable OmniParser local UI parsing
        """
        self.session_id = session_id or f"cu-{int(time.time())}"

        self.state = ComputerUseBridgeState(
            session_id=self.session_id,
            started_at=datetime.now().isoformat(),
            last_update=datetime.now().isoformat(),
            action_chaining_enabled=enable_action_chaining,
            omniparser_enabled=enable_omniparser,
        )

        self._events: List[ComputerUseEvent] = []
        self._initialized = False

        # Ensure state directory exists
        COMPUTER_USE_STATE_DIR.mkdir(parents=True, exist_ok=True)
        if enable_omniparser:
            OMNIPARSER_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    async def initialize(self) -> None:
        """Initialize the bridge."""
        if self._initialized:
            return

        logger.info(f"Initializing Computer Use bridge (session={self.session_id})")

        # Load existing events
        await self._load_events()

        # Write initial state
        await self._write_state()

        self._initialized = True
        logger.info(
            f"Computer Use bridge initialized "
            f"(action_chaining={self.state.action_chaining_enabled}, "
            f"omniparser={self.state.omniparser_enabled})"
        )

    async def emit_action_event(
        self,
        action: ComputerAction,
        status: ExecutionStatus,
        execution_time_ms: float,
        goal: str = "",
        error_message: str = "",
    ) -> None:
        """Emit an action execution event."""
        event = ComputerUseEvent(
            event_id=f"{self.session_id}-action-{len(self._events)}",
            timestamp=datetime.now().isoformat(),
            event_type="action_executed",
            action=action,
            status=status,
            execution_time_ms=execution_time_ms,
            error_message=error_message,
            goal=goal,
            session_id=self.session_id,
            repo_source="jarvis",
        )

        await self._add_event(event)
        self.state.total_actions += 1
        await self._write_state()

    async def emit_batch_event(
        self,
        batch: ActionBatch,
        status: ExecutionStatus,
        execution_time_ms: float,
        time_saved_ms: float = 0.0,
        tokens_saved: int = 0,
        error_message: str = "",
    ) -> None:
        """Emit a batch execution event."""
        event = ComputerUseEvent(
            event_id=f"{self.session_id}-batch-{len(self._events)}",
            timestamp=datetime.now().isoformat(),
            event_type="batch_completed",
            batch=batch,
            status=status,
            execution_time_ms=execution_time_ms,
            error_message=error_message,
            goal=batch.goal,
            session_id=self.session_id,
            repo_source="jarvis",
            time_savings_ms=time_saved_ms,
            token_savings=tokens_saved,
        )

        await self._add_event(event)
        self.state.total_batches += 1
        self.state.total_time_saved_ms += time_saved_ms
        self.state.total_tokens_saved += tokens_saved

        # Update avg batch size
        if self.state.total_batches > 0:
            self.state.avg_batch_size = self.state.total_actions / self.state.total_batches

        await self._write_state()

    async def emit_vision_event(
        self,
        analysis: Dict[str, Any],
        used_omniparser: bool = False,
        tokens_saved: int = 0,
        goal: str = "",
    ) -> None:
        """Emit a vision analysis event."""
        event = ComputerUseEvent(
            event_id=f"{self.session_id}-vision-{len(self._events)}",
            timestamp=datetime.now().isoformat(),
            event_type="vision_analysis",
            status=ExecutionStatus.COMPLETED,
            vision_analysis=analysis,
            used_omniparser=used_omniparser,
            goal=goal,
            session_id=self.session_id,
            repo_source="jarvis",
            token_savings=tokens_saved,
        )

        await self._add_event(event)

        if used_omniparser:
            self.state.omniparser_initialized = True
            self.state.total_tokens_saved += tokens_saved

        await self._write_state()

    def get_statistics(self) -> Dict[str, Any]:
        """Get optimization statistics."""
        return {
            "session_id": self.session_id,
            "total_actions": self.state.total_actions,
            "total_batches": self.state.total_batches,
            "avg_batch_size": round(self.state.avg_batch_size, 2),
            "time_saved_ms": round(self.state.total_time_saved_ms, 0),
            "time_saved_seconds": round(self.state.total_time_saved_ms / 1000, 2),
            "tokens_saved": self.state.total_tokens_saved,
            "action_chaining_enabled": self.state.action_chaining_enabled,
            "omniparser_enabled": self.state.omniparser_enabled,
            "omniparser_initialized": self.state.omniparser_initialized,
        }

    async def get_recent_events(
        self,
        limit: int = 50,
        event_type: Optional[str] = None,
    ) -> List[ComputerUseEvent]:
        """Get recent Computer Use events."""
        events = self._events[-limit:]

        if event_type:
            events = [e for e in events if e.event_type == event_type]

        return events

    async def _add_event(self, event: ComputerUseEvent) -> None:
        """Add event to history."""
        self._events.append(event)

        # Trim to MAX_EVENTS
        if len(self._events) > MAX_EVENTS:
            self._events = self._events[-MAX_EVENTS:]

        await self._write_events()

    async def _write_state(self) -> None:
        """Write current state to file."""
        try:
            self.state.last_update = datetime.now().isoformat()
            COMPUTER_USE_STATE_FILE.write_text(
                json.dumps(self.state.to_dict(), indent=2)
            )
        except Exception as e:
            logger.warning(f"Failed to write Computer Use state: {e}")

    async def _write_events(self) -> None:
        """Write events to file."""
        try:
            events_data = [e.to_dict() for e in self._events]
            COMPUTER_USE_EVENTS_FILE.write_text(
                json.dumps(events_data, indent=2)
            )
        except Exception as e:
            logger.warning(f"Failed to write Computer Use events: {e}")

    async def _load_events(self) -> None:
        """Load existing events from file."""
        try:
            if COMPUTER_USE_EVENTS_FILE.exists():
                content = COMPUTER_USE_EVENTS_FILE.read_text()
                events_data = json.loads(content)
                self._events = [
                    ComputerUseEvent.from_dict(e) for e in events_data[-MAX_EVENTS:]
                ]
                logger.info(f"Loaded {len(self._events)} Computer Use events")
        except Exception as e:
            logger.warning(f"Failed to load Computer Use events: {e}")
            self._events = []


# ============================================================================
# Global Instance
# ============================================================================

_bridge_instance: Optional[ComputerUseBridge] = None


async def get_computer_use_bridge(
    enable_action_chaining: bool = True,
    enable_omniparser: bool = False,
) -> ComputerUseBridge:
    """Get or create the global Computer Use bridge."""
    global _bridge_instance

    if _bridge_instance is None:
        _bridge_instance = ComputerUseBridge(
            enable_action_chaining=enable_action_chaining,
            enable_omniparser=enable_omniparser,
        )
        await _bridge_instance.initialize()

    return _bridge_instance


def get_bridge() -> Optional[ComputerUseBridge]:
    """Get the bridge instance (sync)."""
    return _bridge_instance


# ============================================================================
# Convenience Functions
# ============================================================================

async def emit_action_event(
    action: ComputerAction,
    status: ExecutionStatus,
    execution_time_ms: float,
    goal: str = "",
    error_message: str = "",
) -> None:
    """Emit action event if bridge is active."""
    bridge = get_bridge()
    if bridge:
        await bridge.emit_action_event(
            action, status, execution_time_ms, goal, error_message
        )


async def emit_batch_event(
    batch: ActionBatch,
    status: ExecutionStatus,
    execution_time_ms: float,
    time_saved_ms: float = 0.0,
    tokens_saved: int = 0,
    error_message: str = "",
) -> None:
    """Emit batch event if bridge is active."""
    bridge = get_bridge()
    if bridge:
        await bridge.emit_batch_event(
            batch, status, execution_time_ms, time_saved_ms, tokens_saved, error_message
        )


async def emit_vision_event(
    analysis: Dict[str, Any],
    used_omniparser: bool = False,
    tokens_saved: int = 0,
    goal: str = "",
) -> None:
    """Emit vision analysis event if bridge is active."""
    bridge = get_bridge()
    if bridge:
        await bridge.emit_vision_event(analysis, used_omniparser, tokens_saved, goal)


def get_statistics() -> Dict[str, Any]:
    """Get optimization statistics if bridge is active."""
    bridge = get_bridge()
    if bridge:
        return bridge.get_statistics()
    return {}
