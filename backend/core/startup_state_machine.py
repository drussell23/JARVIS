"""
JARVIS Startup State Machine v2.0.0
===================================

Provides a DAG-driven, wave-based parallel startup architecture:
1. Components declare dependencies → forms a Directed Acyclic Graph
2. Kahn's algorithm computes execution waves (topological layers)
3. Components in the same wave execute concurrently via asyncio.gather
4. Wave N+1 only starts when wave N completes
5. Failed critical dependencies → dependents auto-skipped
6. Event-based notification (no spin-wait polling)

Usage:
    from backend.core.startup_state_machine import get_startup_state_machine

    sm = await get_startup_state_machine()
    sm.register_component("backend", is_critical=True, load_order=30)
    sm.register_component("intelligence", is_critical=False, load_order=40,
                          dependencies=["backend"])

    # Compute waves and run
    waves = sm.compute_waves()
    await sm.run_wave_execution(waves, initializers)
"""

import asyncio
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Callable, Coroutine, Dict, List, Optional, Any, Set, Tuple
from contextlib import asynccontextmanager

from backend.core.async_safety import LazyAsyncLock

# Phase 5A: Bounded queue backpressure
try:
    from backend.core.bounded_queue import BoundedAsyncQueue, OverflowPolicy
except ImportError:
    BoundedAsyncQueue = None

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
    load_order: int = 0       # Lower = load first (tiebreaker within waves)
    dependencies: List[str] = field(default_factory=list)

    @property
    def duration_ms(self) -> Optional[float]:
        if self.start_time and self.end_time:
            return (self.end_time - self.start_time) * 1000
        return None

    @property
    def is_terminal(self) -> bool:
        """True if this component has reached a final state."""
        return self.status in (
            ComponentStatus.READY,
            ComponentStatus.FAILED,
            ComponentStatus.SKIPPED,
        )


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


class CyclicDependencyError(Exception):
    """Raised when the component dependency graph contains a cycle."""
    pass


class DependencyNotReadyError(Exception):
    """Raised when a component start is attempted before dependencies are ready."""
    pass


class StartupStateMachine:
    """
    DAG-driven startup state machine with wave-based parallel execution.

    Key features:
    - Kahn's algorithm topological sort → execution waves
    - Cycle detection at wave computation time
    - Failed critical dependencies → dependents auto-skipped
    - Event-based completion notification (no spin-wait)
    - Graceful degradation if non-critical components fail
    """

    def __init__(self):
        self.phase = StartupPhase.NOT_STARTED
        self.components: Dict[str, ComponentInfo] = {}
        self.started_at: Optional[datetime] = None
        self._start_time: Optional[float] = None
        self._listeners: Set[Callable] = set()
        self._lock = asyncio.Lock()
        self._event_queue: asyncio.Queue = (
            BoundedAsyncQueue(maxsize=500, policy=OverflowPolicy.WARN_AND_BLOCK, name="startup_events")
            if BoundedAsyncQueue is not None else asyncio.Queue()
        )
        self._background_tasks: List[asyncio.Task] = []
        self._ready_event = asyncio.Event()
        self._full_mode_event = asyncio.Event()
        # Event-based completion: keyed by component name
        self._completion_events: Dict[str, asyncio.Event] = {}

        # Register standard components
        self._register_standard_components()

    def _register_standard_components(self):
        """Register the JARVIS startup phases as components with dependencies.

        Maps to the actual _startup_impl() phase ordering:
        - Phases -1..3 are sequential (fatal on failure)
        - After Phase 3 (backend), several phases can run in parallel
        - Phase 6.8 (visual_pipeline) depends on Phase 6.5 (ghost_display)
        - Phase 7 (frontend) depends on backend
        """
        # === Sequential critical phases (must run in order) ===
        self.register_component("clean_slate", is_critical=True, load_order=1)
        self.register_component("loading_experience", is_critical=True, load_order=2,
                                dependencies=["clean_slate"])
        self.register_component("preflight", is_critical=True, load_order=3,
                                dependencies=["loading_experience"])
        self.register_component("resources", is_critical=True, load_order=4,
                                dependencies=["preflight"])
        self.register_component("backend", is_critical=True, load_order=5,
                                dependencies=["resources"])

        # === Parallel non-fatal phases (all depend on backend) ===
        self.register_component("intelligence", is_critical=False, load_order=10,
                                dependencies=["backend"])
        self.register_component("two_tier_security", is_critical=False, load_order=10,
                                dependencies=["backend"])
        self.register_component("trinity", is_critical=False, load_order=11,
                                dependencies=["backend", "two_tier_security"])
        self.register_component("enterprise_services", is_critical=False, load_order=11,
                                dependencies=["backend"])
        self.register_component("ghost_display", is_critical=False, load_order=11,
                                dependencies=["backend"])

        # === Phases that depend on parallel results ===
        self.register_component("agi_os", is_critical=False, load_order=20,
                                dependencies=["backend"])
        self.register_component("visual_pipeline", is_critical=False, load_order=21,
                                dependencies=["ghost_display"])
        self.register_component("frontend", is_critical=False, load_order=30,
                                dependencies=["backend"])

        # === Sub-component tracking (non-phase, for granular status) ===
        self.register_component("voice_orchestrator", is_critical=False, load_order=12,
                                dependencies=["backend"])
        self.register_component("ecapa_verification", is_critical=False, load_order=12,
                                dependencies=["backend"])
        self.register_component("reactor_core", is_critical=False, load_order=15,
                                dependencies=["trinity"])

        # v270.2: Register components the supervisor tracks that were previously
        # undeclared.  Without explicit registration, update_component_sync()
        # auto-registers them as load_order=100 with no dependencies — breaking
        # wave ordering and dependency enforcement.
        self.register_component("loading_server", is_critical=False, load_order=2,
                                dependencies=["clean_slate"])
        self.register_component("jarvis_prime", is_critical=False, load_order=13,
                                dependencies=["trinity"])
        self.register_component("jarvis_body", is_critical=False, load_order=6,
                                dependencies=["backend"])

    def register_component(
        self,
        name: str,
        is_critical: bool = True,
        load_order: int = 50,
        dependencies: Optional[List[str]] = None
    ):
        """Register a component to track during startup."""
        self.components[name] = ComponentInfo(
            name=name,
            is_critical=is_critical,
            load_order=load_order,
            dependencies=dependencies or []
        )
        # Create a completion event for event-based waiting
        if name not in self._completion_events:
            self._completion_events[name] = asyncio.Event()

    # ------------------------------------------------------------------
    # DAG analysis: topological sort via Kahn's algorithm
    # ------------------------------------------------------------------

    def compute_waves(
        self,
        component_names: Optional[Set[str]] = None,
    ) -> List[List[str]]:
        """
        Compute execution waves using Kahn's algorithm (topological sort by layers).

        Each wave is a list of component names that can execute concurrently.
        Wave N+1 only starts after wave N completes.

        Args:
            component_names: Subset of components to include. None = all registered.

        Returns:
            List of waves, each wave is a list of component names.

        Raises:
            CyclicDependencyError: If the dependency graph contains a cycle.
        """
        names = component_names or set(self.components.keys())

        # Build adjacency list and in-degree map (only for requested components)
        in_degree: Dict[str, int] = {n: 0 for n in names}
        dependents: Dict[str, List[str]] = defaultdict(list)  # dep → [things that depend on it]

        for name in names:
            comp = self.components.get(name)
            if not comp:
                continue
            for dep in comp.dependencies:
                if dep in names:
                    in_degree[name] += 1
                    dependents[dep].append(name)
                # If dep not in names, treat as already-satisfied (external dep)

        # Kahn's algorithm: process by layers
        waves: List[List[str]] = []
        queue = deque(n for n, deg in in_degree.items() if deg == 0)

        processed = 0
        while queue:
            # Everything in the current queue forms one wave
            wave = sorted(queue, key=lambda n: self.components.get(
                n, ComponentInfo(name=n)
            ).load_order)
            waves.append(wave)
            next_queue: deque = deque()

            for name in wave:
                processed += 1
                for dependent in dependents.get(name, []):
                    in_degree[dependent] -= 1
                    if in_degree[dependent] == 0:
                        next_queue.append(dependent)

            queue = next_queue

        if processed < len(names):
            # Some nodes were never enqueued → cycle exists
            cycle_members = [n for n in names if in_degree.get(n, 0) > 0]
            raise CyclicDependencyError(
                f"Dependency cycle detected among: {cycle_members}"
            )

        return waves

    def get_dependents(self, name: str) -> Set[str]:
        """Get all transitive dependents of a component (things that depend on it)."""
        result: Set[str] = set()
        queue = deque([name])
        while queue:
            current = queue.popleft()
            for comp_name, comp in self.components.items():
                if current in comp.dependencies and comp_name not in result:
                    result.add(comp_name)
                    queue.append(comp_name)
        return result

    # ------------------------------------------------------------------
    # Wave-based parallel execution
    # ------------------------------------------------------------------

    async def run_wave_execution(
        self,
        waves: List[List[str]],
        initializers: Dict[str, Callable[[], Coroutine]],
        skip_missing: bool = True,
    ) -> bool:
        """
        Execute components wave-by-wave with parallel execution within each wave.

        Args:
            waves: Output of compute_waves().
            initializers: Dict mapping component name → async callable.
            skip_missing: If True, components without initializers are auto-skipped.

        Returns:
            True if no critical components failed.
        """
        critical_failed: List[str] = []
        failed_set: Set[str] = set()

        for wave_idx, wave in enumerate(waves):
            wave_tasks = []
            wave_label = f"Wave {wave_idx} [{', '.join(wave)}]"
            logger.info(f"[StartupDAG] Starting {wave_label}")

            for name in wave:
                # Check if any dependency failed critically → skip this component
                comp = self.components.get(name)
                if comp:
                    failed_deps = [d for d in comp.dependencies if d in failed_set]
                    if failed_deps:
                        reason = f"Dependency failed: {', '.join(failed_deps)}"
                        await self.skip_component(name, reason)
                        failed_set.add(name)
                        self._completion_events.setdefault(name, asyncio.Event()).set()
                        continue

                if name not in initializers:
                    if skip_missing:
                        await self.skip_component(name, "No initializer provided")
                        self._completion_events.setdefault(name, asyncio.Event()).set()
                    continue

                task = asyncio.create_task(
                    self._run_single_component(name, initializers[name]),
                    name=f"startup-{name}",
                )
                wave_tasks.append((name, task))

            # Wait for all tasks in this wave to complete
            if wave_tasks:
                results = await asyncio.gather(
                    *(t for _, t in wave_tasks), return_exceptions=True
                )
                for (name, _), result in zip(wave_tasks, results):
                    if isinstance(result, BaseException):
                        # Task raised an unhandled exception (shouldn't happen
                        # since _run_single_component catches, but defensive)
                        await self.complete_component(name, error=repr(result))
                        comp = self.components.get(name)
                        if comp and comp.is_critical:
                            critical_failed.append(name)
                        failed_set.add(name)

            # After wave completes, check for critical failures
            for name in wave:
                comp = self.components.get(name)
                if comp and comp.status == ComponentStatus.FAILED:
                    failed_set.add(name)
                    if comp.is_critical:
                        critical_failed.append(name)

            if critical_failed:
                logger.error(
                    f"[StartupDAG] Critical failure in {wave_label}: {critical_failed}"
                )
                # Skip all remaining waves' dependents
                break

        return len(critical_failed) == 0

    async def _run_single_component(
        self, name: str, initializer: Callable[[], Coroutine]
    ) -> None:
        """Run a single component initializer with status tracking."""
        await self.start_component(name)
        try:
            await initializer()
            await self.complete_component(name)
        except Exception as e:
            await self.complete_component(name, error=str(e))
        finally:
            self._completion_events.setdefault(name, asyncio.Event()).set()

    async def wait_for_component(self, name: str, timeout: Optional[float] = None) -> bool:
        """Wait for a specific component to reach a terminal state."""
        event = self._completion_events.get(name)
        if event is None:
            return False
        try:
            if timeout:
                await asyncio.wait_for(event.wait(), timeout=timeout)
            else:
                await event.wait()
            return True
        except asyncio.TimeoutError:
            return False

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

    async def transition_to(self, phase: StartupPhase, message: str = ""):
        """Transition to a new startup phase"""
        async with self._lock:
            old_phase = self.phase
            self.phase = phase

            logger.info(f"Startup phase: {old_phase.value} -> {phase.value}")
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
                self._ready_event.set()

            if phase == StartupPhase.FULL_MODE:
                self._full_mode_event.set()

    def get_blocking_dependencies(
        self,
        name: str,
        *,
        allow_skipped: bool = False,
    ) -> List[str]:
        """
        Return dependencies that are not yet in a ready terminal state.

        Args:
            name: Component being evaluated.
            allow_skipped: If True, treat SKIPPED dependencies as satisfied.
        """
        comp = self.components.get(name)
        if comp is None:
            return []

        blocking: List[str] = []
        for dep_name in comp.dependencies:
            dep = self.components.get(dep_name)
            if dep is None:
                # External dependencies are treated as already satisfied.
                continue

            dep_ready = dep.status == ComponentStatus.READY
            if allow_skipped:
                dep_ready = dep_ready or dep.status == ComponentStatus.SKIPPED

            if not dep_ready:
                blocking.append(f"{dep_name}:{dep.status.value}")

        return blocking

    async def start_component(
        self,
        name: str,
        *,
        enforce_dependencies: bool = False,
        allow_skipped_dependencies: bool = False,
    ):
        """Mark a component as starting to load."""
        if name not in self.components:
            self.register_component(name, is_critical=False, load_order=100)

        if enforce_dependencies:
            blocking = self.get_blocking_dependencies(
                name,
                allow_skipped=allow_skipped_dependencies,
            )
            if blocking:
                raise DependencyNotReadyError(
                    f"{name} blocked by dependencies: {', '.join(blocking)}"
                )

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
                logger.warning(f"Component {name} failed: {error}")
            else:
                comp.status = ComponentStatus.READY
                duration = comp.duration_ms
                logger.info(
                    f"Component {name} ready"
                    + (f" ({duration:.0f}ms)" if duration else "")
                )

        # Signal completion event
        self._completion_events.setdefault(name, asyncio.Event()).set()

        await self._broadcast_event({
            "type": "component_complete",
            "component": name,
            "success": error is None,
            "duration_ms": self.components[name].duration_ms if name in self.components else None,
            "error": error,
            "timestamp": time.time()
        })

        # Check if we should transition to FULL_MODE
        await self._check_phase_completion()

    def update_component_sync(
        self,
        name: str,
        status_str: str,
        error: Optional[str] = None,
    ) -> None:
        """Sync update for component status — for use from sync callbacks.

        Maps string status (from _update_component_status) to ComponentStatus enum.
        Does NOT acquire the async lock — only safe for single-threaded event loop usage.
        """
        _STATUS_MAP = {
            "pending": ComponentStatus.PENDING,
            "running": ComponentStatus.LOADING,
            "loading": ComponentStatus.LOADING,
            "complete": ComponentStatus.READY,
            "ready": ComponentStatus.READY,
            "error": ComponentStatus.FAILED,
            "failed": ComponentStatus.FAILED,
            "skipped": ComponentStatus.SKIPPED,
            "degraded": ComponentStatus.FAILED,
        }
        target = _STATUS_MAP.get(status_str)
        if target is None:
            return

        if name not in self.components:
            # Auto-register unknown components as non-critical
            self.register_component(name, is_critical=False, load_order=100)

        comp = self.components[name]

        # Record start time on first LOADING transition
        if target == ComponentStatus.LOADING and comp.start_time is None:
            comp.start_time = time.time()

        comp.status = target

        if target in (ComponentStatus.READY, ComponentStatus.FAILED, ComponentStatus.SKIPPED):
            comp.end_time = time.time()
            if error:
                comp.error = error

        # Fire completion event if terminal
        if comp.is_terminal:
            event = self._completion_events.get(name)
            if event:
                event.set()

    async def skip_component(self, name: str, reason: str = ""):
        """Mark a component as skipped"""
        async with self._lock:
            if name not in self.components:
                return

            comp = self.components[name]
            comp.status = ComponentStatus.SKIPPED
            comp.error = reason or "Skipped"
            logger.info(f"Component {name} skipped: {reason}")

        # Signal completion event
        self._completion_events.setdefault(name, asyncio.Event()).set()

    async def _check_phase_completion(self):
        """Check if all components are done and transition phases"""
        async with self._lock:
            total = len(self.components)
            ready = sum(1 for c in self.components.values()
                       if c.status == ComponentStatus.READY)
            failed = sum(1 for c in self.components.values()
                        if c.status == ComponentStatus.FAILED)
            pending = sum(1 for c in self.components.values()
                         if c.status in (ComponentStatus.PENDING, ComponentStatus.LOADING))

            critical_failed = [
                c.name for c in self.components.values()
                if c.status == ComponentStatus.FAILED and c.is_critical
            ]

            if pending == 0:
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

    # ------------------------------------------------------------------
    # Progress reporting
    # ------------------------------------------------------------------

    def get_progress(self) -> StartupProgress:
        """Get current startup progress"""
        elapsed = (time.time() - self._start_time) if self._start_time else 0

        total = len(self.components)
        done = sum(1 for c in self.components.values() if c.is_terminal)

        phase_weights = {
            StartupPhase.NOT_STARTED: 0.0,
            StartupPhase.SERVER_STARTING: 0.1,
            StartupPhase.CORE_LOADING: 0.2,
            StartupPhase.SERVICES_LOADING: 0.5,
            StartupPhase.FULL_MODE: 1.0,
            StartupPhase.DEGRADED: 0.9,
            StartupPhase.FAILED: 0.0,
        }

        current_task = None
        for comp in self.components.values():
            if comp.status == ComponentStatus.LOADING:
                current_task = comp.name
                break

        return StartupProgress(
            phase=self.phase,
            phase_progress=done / total if total > 0 else 1.0,
            total_progress=phase_weights.get(self.phase, 0) + (
                0.3 * done / total if total > 0 else 0
            ),
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
            loading = [
                c.name for c in self.components.values()
                if c.status == ComponentStatus.LOADING
            ]
            if loading:
                return f"Loading: {', '.join(loading[:3])}..."
            return "Loading services..."
        elif self.phase == StartupPhase.FULL_MODE:
            return "FULL MODE - All systems operational"
        elif self.phase == StartupPhase.DEGRADED:
            failed = [
                c.name for c in self.components.values()
                if c.status == ComponentStatus.FAILED
            ]
            return f"DEGRADED - {len(failed)} component(s) unavailable"
        elif self.phase == StartupPhase.FAILED:
            return "FAILED - Critical systems unavailable"
        return "Unknown state"

    def get_component_summary(self) -> Dict[str, Any]:
        """Get a summary of all component statuses for health endpoints."""
        summary: Dict[str, Any] = {
            "phase": self.phase.value,
            "components": {},
        }
        for name, comp in self.components.items():
            summary["components"][name] = {
                "status": comp.status.value,
                "duration_ms": comp.duration_ms,
                "error": comp.error,
                "is_critical": comp.is_critical,
            }
        return summary

    # ------------------------------------------------------------------
    # Event system
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Legacy compatibility
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def managed_startup(self):
        """Context manager for managed startup (legacy compatibility)."""
        self.started_at = datetime.now()
        self._start_time = time.time()

        await self.transition_to(StartupPhase.SERVER_STARTING, "HTTP server binding...")
        await self.transition_to(StartupPhase.CORE_LOADING)
        await self.start_component("config")
        await self.complete_component("config")
        await self.start_component("logging")
        await self.complete_component("logging")
        await self.transition_to(StartupPhase.SERVICES_LOADING)

        try:
            yield self
        finally:
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
        """Legacy: Run component initializers with dependency ordering.

        Prefer run_wave_execution() for new code — it uses proper
        wave-based parallelism instead of spin-wait polling.
        """
        waves = self.compute_waves(set(initializers.keys()))
        await self.run_wave_execution(waves, initializers)


# ------------------------------------------------------------------
# Singleton
# ------------------------------------------------------------------

_startup_state_machine: Optional[StartupStateMachine] = None
_startup_lock = LazyAsyncLock()


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


def reset_startup_state_machine() -> None:
    """
    v266.1: Reset the singleton for clean restart.

    Root cause: Module-level _startup_state_machine persists across in-process
    restarts. Stale component registrations, completed wave states, and failed
    dependency records from the previous run bleed into the new startup, causing
    components to appear already-registered or pre-failed.

    Must be called during shutdown BEFORE the next startup cycle begins.
    """
    global _startup_state_machine
    _startup_state_machine = None
