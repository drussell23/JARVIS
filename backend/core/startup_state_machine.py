"""
JARVIS Startup State Machine v1.0.0
===================================

Provides a robust, async, parallel startup architecture where:
1. Uvicorn starts IMMEDIATELY with minimal health endpoint
2. Heavy initialization runs in background tasks
3. Startup progress is tracked via state machine
4. Events are broadcast via WebSocket
5. Health endpoints report startup progress

Architecture:
    +-------------------+
    | SERVER_STARTING   |  <- Uvicorn starting, health available immediately
    +-------------------+
              |
              v
    +-------------------+
    | CORE_LOADING      |  <- Basic services (config, logging)
    +-------------------+
              |
              v (parallel)
    +-------------------+
    | SERVICES_LOADING  |  <- Cloud SQL, Redis, ML models (async)
    +-------------------+
              |
              v
    +-------------------+
    | FULL_MODE         |  <- All services ready
    +-------------------+

Usage:
    from core.startup_state_machine import get_startup_state_machine

    startup = await get_startup_state_machine()

    # In lifespan:
    async with startup.managed_startup():
        # Server is serving requests while this runs
        yield
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Callable, Dict, List, Optional, Any, Set
from contextlib import asynccontextmanager

from backend.core.async_safety import LazyAsyncLock

logger = logging.getLogger(__name__)


class StartupPhase(Enum):
    """Startup phases in order"""
    NOT_STARTED = "not_started"
    SERVER_STARTING = "server_starting"  # Uvicorn binding to port
    CORE_LOADING = "core_loading"        # Basic services (config, logging)
    SERVICES_LOADING = "services_loading" # Heavy services (async, parallel)
    FULL_MODE = "full_mode"              # All ready
    DEGRADED = "degraded"                # Some services failed
    FAILED = "failed"                    # Critical failure


class ComponentStatus(Enum):
    """Status of individual components"""
    PENDING = "pending"
    LOADING = "loading"
    READY = "ready"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class ComponentInfo:
    """Information about a startup component"""
    name: str
    status: ComponentStatus = ComponentStatus.PENDING
    start_time: Optional[float] = None
    end_time: Optional[float] = None
    error: Optional[str] = None
    is_critical: bool = True  # If True, failure prevents FULL_MODE
    load_order: int = 0       # Lower = load first
    dependencies: List[str] = field(default_factory=list)

    @property
    def duration_ms(self) -> Optional[float]:
        if self.start_time and self.end_time:
            return (self.end_time - self.start_time) * 1000
        return None


@dataclass
class StartupProgress:
    """Current startup progress snapshot"""
    phase: StartupPhase
    phase_progress: float  # 0.0 to 1.0
    total_progress: float  # 0.0 to 1.0
    components: Dict[str, ComponentInfo]
    started_at: datetime
    current_task: Optional[str]
    elapsed_ms: float
    ready_for_requests: bool  # True once basic health works
    message: str


class StartupStateMachine:
    """
    Manages JARVIS startup with parallel initialization.

    Key features:
    - Server starts serving health endpoint IMMEDIATELY
    - Heavy ML models load in background
    - WebSocket broadcasts startup events
    - Graceful degradation if components fail
    """

    def __init__(self):
        self.phase = StartupPhase.NOT_STARTED
        self.components: Dict[str, ComponentInfo] = {}
        self.started_at: Optional[datetime] = None
        self._start_time: Optional[float] = None
        self._listeners: Set[Callable] = set()
        self._lock = asyncio.Lock()
        self._event_queue: asyncio.Queue = asyncio.Queue()
        self._background_tasks: List[asyncio.Task] = []
        self._ready_event = asyncio.Event()
        self._full_mode_event = asyncio.Event()

        # Register standard components
        self._register_standard_components()

    def _register_standard_components(self):
        """Register the standard JARVIS startup components"""
        # Core (blocking, must complete before services)
        self.register_component("config", is_critical=True, load_order=1)
        self.register_component("logging", is_critical=True, load_order=2)

        # Services (can load in parallel after core)
        self.register_component("cloud_sql_proxy", is_critical=False, load_order=10, dependencies=["config"])
        self.register_component("learning_database", is_critical=False, load_order=11, dependencies=["cloud_sql_proxy"])
        self.register_component("redis", is_critical=False, load_order=10)
        self.register_component("prometheus", is_critical=False, load_order=10)

        # ML Models (heavy, load in parallel)
        self.register_component("ecapa_local", is_critical=False, load_order=20, dependencies=["config"])
        self.register_component("ecapa_cloud", is_critical=False, load_order=20)
        self.register_component("wav2vec2", is_critical=False, load_order=20)
        self.register_component("speaker_verification", is_critical=False, load_order=25, dependencies=["learning_database", "ecapa_local"])

        # Voice System (depends on ML models)
        self.register_component("voice_capture", is_critical=False, load_order=30)
        self.register_component("voice_unlock", is_critical=False, load_order=31, dependencies=["speaker_verification"])

        # Integration (depends on multiple services)
        self.register_component("autonomous_orchestrator", is_critical=False, load_order=40)
        self.register_component("hybrid_coordination", is_critical=False, load_order=40)

    def register_component(
        self,
        name: str,
        is_critical: bool = True,
        load_order: int = 50,
        dependencies: List[str] = None
    ):
        """Register a component to track during startup"""
        self.components[name] = ComponentInfo(
            name=name,
            is_critical=is_critical,
            load_order=load_order,
            dependencies=dependencies or []
        )

    async def transition_to(self, phase: StartupPhase, message: str = ""):
        """Transition to a new startup phase"""
        async with self._lock:
            old_phase = self.phase
            self.phase = phase

            logger.info(f"🔄 Startup phase: {old_phase.value} -> {phase.value}")
            if message:
                logger.info(f"   {message}")

            # Broadcast event
            await self._broadcast_event({
                "type": "phase_change",
                "old_phase": old_phase.value,
                "new_phase": phase.value,
                "message": message,
                "timestamp": time.time()
            })

            # Update ready events
            if phase in (StartupPhase.CORE_LOADING, StartupPhase.SERVICES_LOADING,
                        StartupPhase.FULL_MODE, StartupPhase.DEGRADED):
                self._ready_event.set()  # Server can accept requests

            if phase == StartupPhase.FULL_MODE:
                self._full_mode_event.set()

    async def start_component(self, name: str):
        """Mark a component as starting to load"""
        if name not in self.components:
            self.register_component(name, is_critical=False, load_order=100)

        async with self._lock:
            comp = self.components[name]
            comp.status = ComponentStatus.LOADING
            comp.start_time = time.time()

        await self._broadcast_event({
            "type": "component_loading",
            "component": name,
            "timestamp": time.time()
        })

    async def complete_component(self, name: str, error: Optional[str] = None):
        """Mark a component as completed (success or failure)"""
        async with self._lock:
            if name not in self.components:
                return

            comp = self.components[name]
            comp.end_time = time.time()

            if error:
                comp.status = ComponentStatus.FAILED
                comp.error = error
                logger.warning(f"❌ Component {name} failed: {error}")
            else:
                comp.status = ComponentStatus.READY
                logger.info(f"✅ Component {name} ready ({comp.duration_ms:.0f}ms)")

        await self._broadcast_event({
            "type": "component_complete",
            "component": name,
            "success": error is None,
            "duration_ms": comp.duration_ms,
            "error": error,
            "timestamp": time.time()
        })

        # Check if we should transition to FULL_MODE
        await self._check_phase_completion()

    async def skip_component(self, name: str, reason: str = ""):
        """Mark a component as skipped"""
        async with self._lock:
            if name not in self.components:
                return

            comp = self.components[name]
            comp.status = ComponentStatus.SKIPPED
            comp.error = reason or "Skipped"
            logger.info(f"⏭️  Component {name} skipped: {reason}")

    async def _check_phase_completion(self):
        """Check if all components are done and transition phases"""
        async with self._lock:
            # Count statuses
            total = len(self.components)
            ready = sum(1 for c in self.components.values() if c.status == ComponentStatus.READY)
            failed = sum(1 for c in self.components.values() if c.status == ComponentStatus.FAILED)
            pending = sum(1 for c in self.components.values() if c.status in (ComponentStatus.PENDING, ComponentStatus.LOADING))
            skipped = sum(1 for c in self.components.values() if c.status == ComponentStatus.SKIPPED)

            # Check critical failures
            critical_failed = [
                c.name for c in self.components.values()
                if c.status == ComponentStatus.FAILED and c.is_critical
            ]

            if pending == 0:
                # All components processed
                if critical_failed:
                    await self.transition_to(
                        StartupPhase.FAILED,
                        f"Critical components failed: {', '.join(critical_failed)}"
                    )
                elif failed > 0:
                    await self.transition_to(
                        StartupPhase.DEGRADED,
                        f"{ready}/{total} components ready, {failed} failed"
                    )
                else:
                    await self.transition_to(
                        StartupPhase.FULL_MODE,
                        f"All {ready} components ready"
                    )

    def get_progress(self) -> StartupProgress:
        """Get current startup progress"""
        elapsed = (time.time() - self._start_time) if self._start_time else 0

        # Calculate progress
        total = len(self.components)
        done = sum(
            1 for c in self.components.values()
            if c.status in (ComponentStatus.READY, ComponentStatus.FAILED, ComponentStatus.SKIPPED)
        )

        phase_weights = {
            StartupPhase.NOT_STARTED: 0.0,
            StartupPhase.SERVER_STARTING: 0.1,
            StartupPhase.CORE_LOADING: 0.2,
            StartupPhase.SERVICES_LOADING: 0.5,
            StartupPhase.FULL_MODE: 1.0,
            StartupPhase.DEGRADED: 0.9,
            StartupPhase.FAILED: 0.0,
        }

        # Find current task
        current_task = None
        for comp in self.components.values():
            if comp.status == ComponentStatus.LOADING:
                current_task = comp.name
                break

        return StartupProgress(
            phase=self.phase,
            phase_progress=done / total if total > 0 else 1.0,
            total_progress=phase_weights.get(self.phase, 0) + (0.3 * done / total if total > 0 else 0),
            components=dict(self.components),
            started_at=self.started_at or datetime.now(),
            current_task=current_task,
            elapsed_ms=elapsed * 1000,
            ready_for_requests=self._ready_event.is_set(),
            message=self._get_status_message()
        )

    def _get_status_message(self) -> str:
        """Get human-readable status message"""
        if self.phase == StartupPhase.NOT_STARTED:
            return "System not started"
        elif self.phase == StartupPhase.SERVER_STARTING:
            return "Starting HTTP server..."
        elif self.phase == StartupPhase.CORE_LOADING:
            return "Loading core services..."
        elif self.phase == StartupPhase.SERVICES_LOADING:
            loading = [c.name for c in self.components.values() if c.status == ComponentStatus.LOADING]
            if loading:
                return f"Loading: {', '.join(loading[:3])}..."
            return "Loading services..."
        elif self.phase == StartupPhase.FULL_MODE:
            return "FULL MODE - All systems operational"
        elif self.phase == StartupPhase.DEGRADED:
            failed = [c.name for c in self.components.values() if c.status == ComponentStatus.FAILED]
            return f"DEGRADED - {len(failed)} component(s) unavailable"
        elif self.phase == StartupPhase.FAILED:
            return "FAILED - Critical systems unavailable"
        return "Unknown state"

    def add_listener(self, callback: Callable):
        """Add a startup event listener"""
        self._listeners.add(callback)

    def remove_listener(self, callback: Callable):
        """Remove a startup event listener"""
        self._listeners.discard(callback)

    async def _broadcast_event(self, event: Dict[str, Any]):
        """Broadcast event to all listeners"""
        for listener in self._listeners:
            try:
                if asyncio.iscoroutinefunction(listener):
                    await listener(event)
                else:
                    listener(event)
            except Exception as e:
                logger.warning(f"Event listener error: {e}")

    async def wait_for_ready(self, timeout: float = 30.0) -> bool:
        """Wait until server is ready for basic requests"""
        try:
            await asyncio.wait_for(self._ready_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def wait_for_full_mode(self, timeout: float = 300.0) -> bool:
        """Wait until FULL_MODE is reached"""
        try:
            await asyncio.wait_for(self._full_mode_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    @asynccontextmanager
    async def managed_startup(self):
        """
        Context manager for managed startup.

        Usage:
            async with startup.managed_startup():
                yield  # Server starts serving requests here
        """
        self.started_at = datetime.now()
        self._start_time = time.time()

        await self.transition_to(StartupPhase.SERVER_STARTING, "HTTP server binding...")

        # Immediately mark core as loading
        await self.transition_to(StartupPhase.CORE_LOADING)
        await self.start_component("config")
        await self.complete_component("config")
        await self.start_component("logging")
        await self.complete_component("logging")

        # Transition to services loading - now the server can serve requests
        await self.transition_to(StartupPhase.SERVICES_LOADING)

        try:
            # The server is now serving requests
            yield self
        finally:
            # Cleanup background tasks
            for task in self._background_tasks:
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

    async def run_parallel_initialization(
        self,
        initializers: Dict[str, Callable],
        max_concurrent: int = 4
    ):
        """
        Run component initializers in parallel with dependency ordering.

        Args:
            initializers: Dict mapping component name to async initializer function
            max_concurrent: Maximum concurrent initializations
        """
        semaphore = asyncio.Semaphore(max_concurrent)
        completed = set()

        async def run_initializer(name: str, func: Callable):
            """Run a single initializer with dependency waiting"""
            # Wait for dependencies
            deps = self.components.get(name, ComponentInfo(name=name)).dependencies
            for dep in deps:
                while dep not in completed:
                    await asyncio.sleep(0.1)

            async with semaphore:
                await self.start_component(name)
                try:
                    await func()
                    await self.complete_component(name)
                except Exception as e:
                    await self.complete_component(name, error=str(e))
                finally:
                    completed.add(name)

        # Sort by load_order
        sorted_names = sorted(
            initializers.keys(),
            key=lambda n: self.components.get(n, ComponentInfo(name=n)).load_order
        )

        # Create tasks
        tasks = []
        for name in sorted_names:
            if name in initializers:
                task = asyncio.create_task(run_initializer(name, initializers[name]))
                tasks.append(task)
                self._background_tasks.append(task)

        # Wait for all to complete
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


# Singleton instance
_startup_state_machine: Optional[StartupStateMachine] = None
_startup_lock = LazyAsyncLock()  # v100.1: Lazy initialization to avoid "no running event loop" error


async def get_startup_state_machine() -> StartupStateMachine:
    """Get or create the singleton startup state machine"""
    global _startup_state_machine

    async with _startup_lock:
        if _startup_state_machine is None:
            _startup_state_machine = StartupStateMachine()
        return _startup_state_machine


def get_startup_state_machine_sync() -> Optional[StartupStateMachine]:
    """Get the startup state machine synchronously (may be None)"""
    return _startup_state_machine
