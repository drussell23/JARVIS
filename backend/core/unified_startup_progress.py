"""
JARVIS Unified Startup Progress Hub v1.0.0
==========================================

Single source of truth for ALL startup progress tracking.
Synchronizes loading_server, startup_progress_api, and startup_progress_broadcaster.

This eliminates the misalignment between different progress tracking systems
by providing ONE central hub that all systems read from and write to.

Architecture:
    +-----------------------+
    |  UnifiedStartupHub    |  <-- Single Source of Truth
    +-----------------------+
             |
    +--------+--------+--------+
    |        |        |        |
    v        v        v        v
 Loading   Startup   Startup   Voice
 Server    API       Broadcast Narrator
 (3001)    (8010)    (WS)      (TTS)

Features:
- Single source of truth for progress state
- Automatic synchronization to all downstream systems
- Dynamic component registration (no hardcoding)
- Accurate "ready" state detection
- Thread-safe async operations
- Progress monotonicity enforcement
- Rich event history
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from weakref import WeakSet

import aiohttp

logger = logging.getLogger(__name__)


class StartupPhase(Enum):
    """Startup phases in order"""
    IDLE = "idle"
    INITIALIZING = "initializing"
    SUPERVISOR = "supervisor"
    BACKEND = "backend"
    COMPONENTS = "components"
    FINALIZING = "finalizing"
    COMPLETE = "complete"
    FAILED = "failed"


class ComponentStatus(Enum):
    """Component initialization status"""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class ComponentInfo:
    """Information about a registered component"""
    name: str
    weight: float = 5.0  # Relative weight for progress calculation (default 5)
    status: ComponentStatus = ComponentStatus.PENDING
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    duration_ms: Optional[float] = None
    message: str = ""
    error: Optional[str] = None
    is_critical: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ProgressEvent:
    """A single progress event for history tracking"""
    timestamp: float
    event_type: str
    progress: float
    phase: str
    component: Optional[str] = None
    message: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "event_type": self.event_type,
            "progress": self.progress,
            "phase": self.phase,
            "component": self.component,
            "message": self.message,
            "metadata": self.metadata
        }


class UnifiedStartupProgressHub:
    """
    The single source of truth for startup progress.

    All progress tracking systems MUST go through this hub to ensure
    consistent state across the loading page, backend API, and voice narrator.
    """

    _instance: Optional["UnifiedStartupProgressHub"] = None
    _lock = asyncio.Lock()

    def __init__(self):
        # Core state
        self._progress: float = 0.0
        self._max_progress: float = 0.0  # Monotonic enforcement
        self._phase: StartupPhase = StartupPhase.IDLE
        self._message: str = "Waiting to start..."
        self._is_ready: bool = False
        self._started_at: Optional[float] = None
        self._completed_at: Optional[float] = None

        # Component tracking (dynamic, not hardcoded)
        self._components: Dict[str, ComponentInfo] = {}
        self._component_order: List[str] = []  # Ordered by registration

        # Event history
        self._events: List[ProgressEvent] = []
        self._max_events = 500

        # Synchronization targets
        self._sync_targets: Set[Callable] = set()
        self._websocket_clients: WeakSet = WeakSet()
        self._loading_server_url: Optional[str] = None

        # Ready state tracking
        self._required_components: Set[str] = set()
        self._ready_callbacks: List[Callable] = []

        # HTTP session for loading server sync
        self._session: Optional[aiohttp.ClientSession] = None

        # Lock for thread safety
        self._state_lock = asyncio.Lock()

        # System info
        self._memory_info: Optional[Dict[str, Any]] = None
        self._startup_mode: Optional[str] = None

    @classmethod
    async def get_instance(cls) -> "UnifiedStartupProgressHub":
        """Get or create the singleton instance"""
        async with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    @classmethod
    def get_instance_sync(cls) -> "UnifiedStartupProgressHub":
        """Synchronous version for non-async contexts"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # =========================================================================
    # Initialization
    # =========================================================================

    async def initialize(
        self,
        loading_server_url: str = "http://localhost:3001",
        required_components: Optional[List[str]] = None,
        component_weights: Optional[Dict[str, float]] = None
    ):
        """
        Initialize the progress hub with ALL components registered UPFRONT.

        This is CRITICAL for accurate progress calculation. By registering all
        components before any complete, the denominator (total weight) is fixed
        and progress increases smoothly.

        Args:
            loading_server_url: URL for the standalone loading server
            required_components: Components that must complete for system to be "ready"
            component_weights: Dict mapping component name to weight (optional)
        """
        self._loading_server_url = loading_server_url
        self._started_at = time.time()
        self._phase = StartupPhase.INITIALIZING

        if required_components:
            self._required_components = set(required_components)

        # Create HTTP session for loading server sync (only if URL is provided)
        if loading_server_url and self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=2.0)
            )

        # CRITICAL: Register ALL components UPFRONT with default weights
        # This ensures the denominator (total_weight) is fixed from the start
        # and progress increases smoothly as components complete.
        #
        # Default weights are calibrated based on typical startup times:
        default_weights = {
            "supervisor": 5.0,      # Quick - just coordination
            "spawning": 5.0,        # Process creation
            "backend": 20.0,        # API server startup
            "database": 8.0,        # Database connections
            "voice": 15.0,          # Voice engine initialization
            "vision": 12.0,         # Vision/camera initialization
            "frontend": 15.0,       # React app startup
            "websocket": 5.0,       # WebSocket connections
            "models": 10.0,         # AI models loading
            "config": 3.0,          # Configuration loading
            "cleanup": 2.0,         # Process cleanup
        }

        # Override with provided weights
        if component_weights:
            default_weights.update(component_weights)

        # Register all known components upfront
        for name, weight in default_weights.items():
            is_required = name in self._required_components if self._required_components else True
            await self.register_component(
                name=name,
                weight=weight,
                is_critical=(name in ("backend", "supervisor")),
                is_required_for_ready=is_required
            )

        # Record initialization event
        await self._record_event("initialize", "Startup hub initialized with all components")
        logger.info(f"[UnifiedProgress] Hub initialized with {len(self._components)} components pre-registered")

    async def shutdown(self):
        """Clean up resources"""
        if self._session:
            await self._session.close()
            self._session = None

    # =========================================================================
    # Component Registration (Dynamic, Not Hardcoded)
    # =========================================================================

    async def register_component(
        self,
        name: str,
        weight: float = 5.0,
        is_critical: bool = False,
        is_required_for_ready: bool = True
    ):
        """
        Dynamically register a component for progress tracking.

        Args:
            name: Unique component identifier
            weight: Relative weight for progress calculation (default 5.0)
            is_critical: If True, failure stops entire startup
            is_required_for_ready: If True, must complete for system to be "ready"
        """
        async with self._state_lock:
            if name not in self._components:
                self._components[name] = ComponentInfo(
                    name=name,
                    weight=weight,
                    is_critical=is_critical
                )
                self._component_order.append(name)

                if is_required_for_ready:
                    self._required_components.add(name)

                logger.debug(f"[UnifiedProgress] Registered component: {name} (weight={weight})")

    async def register_components_batch(
        self,
        components: List[Dict[str, Any]]
    ):
        """
        Register multiple components at once.

        Args:
            components: List of dicts with {name, weight, is_critical, is_required_for_ready}
        """
        for comp in components:
            await self.register_component(
                name=comp.get("name"),
                weight=comp.get("weight", 5.0),
                is_critical=comp.get("is_critical", False),
                is_required_for_ready=comp.get("is_required_for_ready", True)
            )

    def get_component_count(self) -> int:
        """Get total number of registered components"""
        return len(self._components)

    def get_completed_count(self) -> int:
        """Get number of completed components"""
        return sum(
            1 for c in self._components.values()
            if c.status in (ComponentStatus.COMPLETE, ComponentStatus.SKIPPED)
        )

    # =========================================================================
    # Progress Calculation
    # =========================================================================

    def _calculate_progress(self) -> float:
        """
        Calculate overall progress based on component weights.
        Returns a value between 0.0 and 100.0.
        """
        if not self._components:
            return 0.0

        total_weight = sum(c.weight for c in self._components.values())
        if total_weight == 0:
            return 0.0

        completed_weight = sum(
            c.weight for c in self._components.values()
            if c.status == ComponentStatus.COMPLETE
        )

        # Give partial credit for running/failed/skipped
        partial_weight = sum(
            c.weight * 0.5 for c in self._components.values()
            if c.status in (ComponentStatus.RUNNING, ComponentStatus.FAILED, ComponentStatus.SKIPPED)
        )

        raw_progress = ((completed_weight + partial_weight) / total_weight) * 100
        return min(100.0, max(0.0, raw_progress))

    # =========================================================================
    # Component Status Updates
    # =========================================================================

    async def component_start(
        self,
        component: str,
        message: str = ""
    ):
        """Mark a component as starting"""
        async with self._state_lock:
            if component not in self._components:
                await self.register_component(component)

            comp = self._components[component]
            comp.status = ComponentStatus.RUNNING
            comp.started_at = time.time()
            comp.message = message or f"Starting {component}..."

            self._message = comp.message
            self._progress = self._calculate_progress()

            # Enforce monotonic progress
            if self._progress > self._max_progress:
                self._max_progress = self._progress
            else:
                self._progress = self._max_progress

        await self._record_event(
            "component_start",
            comp.message,
            component=component
        )
        await self._sync_all()

    async def component_progress(
        self,
        component: str,
        message: str,
        substep: int = 0,
        total_substeps: int = 1
    ):
        """Update progress within a component"""
        async with self._state_lock:
            if component in self._components:
                self._components[component].message = message
                self._message = message

        await self._record_event(
            "component_progress",
            message,
            component=component,
            metadata={"substep": substep, "total_substeps": total_substeps}
        )
        await self._sync_all()

    async def component_complete(
        self,
        component: str,
        message: str = ""
    ):
        """Mark a component as complete"""
        async with self._state_lock:
            if component not in self._components:
                await self.register_component(component)

            comp = self._components[component]
            comp.status = ComponentStatus.COMPLETE
            comp.completed_at = time.time()
            if comp.started_at:
                comp.duration_ms = (comp.completed_at - comp.started_at) * 1000
            comp.message = message or f"{component} ready"

            self._message = comp.message
            self._progress = self._calculate_progress()

            # Enforce monotonic progress
            if self._progress > self._max_progress:
                self._max_progress = self._progress
            else:
                self._progress = self._max_progress

            # Check if we're truly ready
            self._check_ready_state()

        await self._record_event(
            "component_complete",
            comp.message,
            component=component,
            metadata={"duration_ms": comp.duration_ms}
        )
        await self._sync_all()

    async def component_failed(
        self,
        component: str,
        error: str,
        is_critical: bool = False
    ):
        """Mark a component as failed"""
        async with self._state_lock:
            if component not in self._components:
                await self.register_component(component)

            comp = self._components[component]
            comp.status = ComponentStatus.FAILED
            comp.completed_at = time.time()
            comp.error = error
            comp.is_critical = is_critical

            # Give partial credit for attempting
            self._progress = self._calculate_progress()
            if self._progress > self._max_progress:
                self._max_progress = self._progress

            self._message = f"Failed: {error}"

            if is_critical:
                self._phase = StartupPhase.FAILED

        await self._record_event(
            "component_fail",
            f"Failed: {error}",
            component=component,
            metadata={"error": error, "is_critical": is_critical}
        )
        await self._sync_all()

    async def component_skipped(
        self,
        component: str,
        reason: str = ""
    ):
        """Mark a component as skipped"""
        async with self._state_lock:
            if component not in self._components:
                await self.register_component(component)

            comp = self._components[component]
            comp.status = ComponentStatus.SKIPPED
            comp.message = reason or f"{component} skipped"

            self._progress = self._calculate_progress()
            if self._progress > self._max_progress:
                self._max_progress = self._progress

        await self._record_event(
            "component_skipped",
            comp.message,
            component=component
        )
        await self._sync_all()

    # =========================================================================
    # Phase Management
    # =========================================================================

    async def set_phase(self, phase: StartupPhase, message: str = ""):
        """Update the current startup phase"""
        async with self._state_lock:
            self._phase = phase
            if message:
                self._message = message

        await self._record_event(
            "phase_change",
            message or f"Entering {phase.value} phase",
            metadata={"phase": phase.value}
        )
        await self._sync_all()

    # =========================================================================
    # Ready State Management (Critical for Voice Narration)
    # =========================================================================

    def _check_ready_state(self) -> bool:
        """
        Check if the system is truly ready.
        This is critical for preventing premature "ready" announcements.
        """
        if not self._required_components:
            # If no required components specified, check if all are complete
            all_complete = all(
                c.status in (ComponentStatus.COMPLETE, ComponentStatus.SKIPPED)
                for c in self._components.values()
            )
            self._is_ready = all_complete
        else:
            # Check if all required components are complete
            required_complete = all(
                self._components.get(name, ComponentInfo(name=name)).status
                in (ComponentStatus.COMPLETE, ComponentStatus.SKIPPED)
                for name in self._required_components
            )
            self._is_ready = required_complete

        return self._is_ready

    def is_ready(self) -> bool:
        """
        Check if the system is truly ready.
        Use this before announcing "ready" via voice or UI.
        """
        return self._is_ready and self._phase == StartupPhase.COMPLETE

    async def mark_complete(
        self,
        success: bool = True,
        message: str = ""
    ):
        """
        Mark the startup as complete.
        Only call this when ALL required components are truly ready.
        """
        async with self._state_lock:
            self._completed_at = time.time()

            if success:
                self._phase = StartupPhase.COMPLETE
                self._progress = 100.0
                self._max_progress = 100.0
                self._is_ready = True
                self._message = message or "JARVIS Online"
            else:
                self._phase = StartupPhase.FAILED
                self._is_ready = False
                self._message = message or "Startup failed"

        elapsed = self._completed_at - (self._started_at or self._completed_at)

        await self._record_event(
            "complete" if success else "failed",
            self._message,
            metadata={
                "success": success,
                "elapsed_seconds": elapsed,
                "components_ready": self.get_completed_count(),
                "total_components": self.get_component_count(),
                "redirect_url": "http://localhost:3000"
            }
        )
        await self._sync_all()

        # Fire ready callbacks
        if success:
            for callback in self._ready_callbacks:
                try:
                    if asyncio.iscoroutinefunction(callback):
                        await callback()
                    else:
                        callback()
                except Exception as e:
                    logger.error(f"Ready callback error: {e}")

    def on_ready(self, callback: Callable):
        """Register a callback to be called when system is ready"""
        self._ready_callbacks.append(callback)

    # =========================================================================
    # State Getters
    # =========================================================================

    def get_state(self) -> Dict[str, Any]:
        """Get the complete current state"""
        elapsed = 0.0
        if self._started_at:
            end_time = self._completed_at or time.time()
            elapsed = end_time - self._started_at

        return {
            "progress": self._progress,
            "phase": self._phase.value,
            "stage": self._phase.value,  # Alias for compatibility
            "message": self._message,
            "is_ready": self._is_ready,
            "timestamp": datetime.now().isoformat(),
            "elapsed_seconds": elapsed,
            "components_ready": self.get_completed_count(),
            "total_components": self.get_component_count(),
            "components": {
                name: {
                    "status": comp.status.value,
                    "message": comp.message,
                    "duration_ms": comp.duration_ms,
                    "error": comp.error
                }
                for name, comp in self._components.items()
            },
            "memory": self._memory_info,
            "startup_mode": self._startup_mode,
            "backend_ready": self._phase == StartupPhase.COMPLETE,
            "frontend_ready": self._phase == StartupPhase.COMPLETE
        }

    def get_progress(self) -> float:
        """Get current progress percentage"""
        return self._progress

    def get_phase(self) -> StartupPhase:
        """Get current phase"""
        return self._phase

    def get_message(self) -> str:
        """Get current status message"""
        return self._message

    def get_summary(self) -> str:
        """Get a human-readable progress summary"""
        completed = self.get_completed_count()
        total = self.get_component_count()
        return f"Progress: {self._progress:.0f}% ({completed}/{total} components) - {self._phase.value.upper()}"

    # =========================================================================
    # Event Recording
    # =========================================================================

    async def _record_event(
        self,
        event_type: str,
        message: str,
        component: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ):
        """Record an event to history"""
        event = ProgressEvent(
            timestamp=time.time(),
            event_type=event_type,
            progress=self._progress,
            phase=self._phase.value,
            component=component,
            message=message,
            metadata=metadata or {}
        )

        self._events.append(event)
        if len(self._events) > self._max_events:
            self._events = self._events[-self._max_events:]

    def get_recent_events(self, count: int = 50) -> List[Dict[str, Any]]:
        """Get recent events for clients"""
        return [e.to_dict() for e in self._events[-count:]]

    # =========================================================================
    # Memory/Mode Updates
    # =========================================================================

    async def update_memory_info(
        self,
        available_gb: float,
        used_gb: float,
        total_gb: float,
        pressure_percent: float,
        startup_mode: str
    ):
        """Update memory status"""
        self._memory_info = {
            "available_gb": available_gb,
            "used_gb": used_gb,
            "total_gb": total_gb,
            "pressure_percent": pressure_percent
        }
        self._startup_mode = startup_mode

        await self._record_event(
            "memory_update",
            f"Memory: {available_gb:.1f}GB available, Mode: {startup_mode}",
            metadata=self._memory_info
        )
        await self._sync_all()

    # =========================================================================
    # Synchronization to Downstream Systems
    # =========================================================================

    async def _sync_all(self):
        """Synchronize state to all downstream systems"""
        state = self.get_state()

        # Log progress for debugging
        logger.debug(f"[UnifiedProgress] {self.get_summary()}")

        # Sync to loading server via HTTP
        await self._sync_to_loading_server(state)

        # Sync to registered callbacks
        for callback in self._sync_targets:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(state)
                else:
                    callback(state)
            except Exception as e:
                logger.debug(f"Sync callback error: {e}")

        # Sync to WebSocket clients
        await self._sync_to_websockets(state)

    async def _sync_to_loading_server(self, state: Dict[str, Any]):
        """Sync progress to the standalone loading server"""
        if not self._loading_server_url or not self._session:
            return

        try:
            payload = {
                "stage": state["phase"],
                "message": state["message"],
                "progress": state["progress"],
                "metadata": {
                    "components_ready": state["components_ready"],
                    "total_components": state["total_components"],
                    "is_ready": state["is_ready"],
                    "memory": state.get("memory"),
                    "startup_mode": state.get("startup_mode")
                }
            }

            async with self._session.post(
                f"{self._loading_server_url}/api/update-progress",
                json=payload
            ) as resp:
                if resp.status != 200:
                    logger.debug(f"Loading server sync failed: {resp.status}")
        except asyncio.TimeoutError:
            pass
        except Exception as e:
            logger.debug(f"Loading server sync error: {e}")

    async def _sync_to_websockets(self, state: Dict[str, Any]):
        """Broadcast to all connected WebSocket clients"""
        message = json.dumps(state)
        dead_clients = set()

        for ws in list(self._websocket_clients):
            try:
                await ws.send_text(message)
            except Exception:
                dead_clients.add(ws)

        for ws in dead_clients:
            self._websocket_clients.discard(ws)

    def register_sync_target(self, callback: Callable):
        """Register a callback to receive state updates"""
        self._sync_targets.add(callback)

    def unregister_sync_target(self, callback: Callable):
        """Unregister a sync callback"""
        self._sync_targets.discard(callback)

    def register_websocket(self, websocket):
        """Register a WebSocket for progress updates"""
        self._websocket_clients.add(websocket)

    def unregister_websocket(self, websocket):
        """Unregister a WebSocket"""
        self._websocket_clients.discard(websocket)


# =============================================================================
# Global Access
# =============================================================================

_hub_instance: Optional[UnifiedStartupProgressHub] = None


def get_progress_hub() -> UnifiedStartupProgressHub:
    """Get the global progress hub instance (sync version)"""
    global _hub_instance
    if _hub_instance is None:
        _hub_instance = UnifiedStartupProgressHub.get_instance_sync()
    return _hub_instance


async def get_progress_hub_async() -> UnifiedStartupProgressHub:
    """Get the global progress hub instance (async version)"""
    global _hub_instance
    if _hub_instance is None:
        _hub_instance = await UnifiedStartupProgressHub.get_instance()
    return _hub_instance


# =============================================================================
# Convenience Functions
# =============================================================================

async def start_component(component: str, message: str = ""):
    """Convenience function to mark a component as starting"""
    hub = get_progress_hub()
    await hub.component_start(component, message)


async def complete_component(component: str, message: str = ""):
    """Convenience function to mark a component as complete"""
    hub = get_progress_hub()
    await hub.component_complete(component, message)


async def fail_component(component: str, error: str, is_critical: bool = False):
    """Convenience function to mark a component as failed"""
    hub = get_progress_hub()
    await hub.component_failed(component, error, is_critical)


def is_system_ready() -> bool:
    """Check if the system is truly ready"""
    hub = get_progress_hub()
    return hub.is_ready()


def get_progress_summary() -> str:
    """Get a human-readable progress summary"""
    hub = get_progress_hub()
    return hub.get_summary()
