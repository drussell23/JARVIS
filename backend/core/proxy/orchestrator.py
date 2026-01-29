"""
Unified Proxy Orchestrator - Central coordination for all proxy layers

Integrates:
- Layer 1: Distributed Leader Election
- Layer 2: Async Startup Barrier
- Layer 3: Proxy Lifecycle Controller
- Layer 4: Health Aggregator

Provides single entry point for proxy management across JARVIS ecosystem.

Author: JARVIS System
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Dict,
    Final,
    List,
    Optional,
    Set,
)

from .distributed_leader import (
    DistributedProxyLeader,
    ElectionResult,
    LeaderState,
    StaleFileCleanupResult,
    create_proxy_leader,
)
from .health_aggregator import (
    AnomalyType,
    HealthSnapshot,
    UnifiedHealthAggregator,
    create_health_aggregator,
)
from .lifecycle_controller import (
    ProxyLifecycleController,
    ProxyState,
    StateTransitionEvent,
    create_lifecycle_controller,
)
from .startup_barrier import (
    AsyncStartupBarrier,
    ComponentManifest,
    DependencyType,
    VerificationStage,
    create_startup_barrier,
)

logger = logging.getLogger(__name__)


# =============================================================================
# Configuration
# =============================================================================

class OrchestratorConfig:
    """Configuration loaded from environment variables."""

    # Repository identification
    REPO_NAME: Final[str] = os.getenv("JARVIS_REPO_NAME", "jarvis")

    # Startup behavior
    CLOUDSQL_REQUIRED: Final[bool] = os.getenv("CLOUDSQL_REQUIRED", "true").lower() == "true"
    PARALLEL_STARTUP: Final[bool] = os.getenv("PARALLEL_STARTUP", "true").lower() == "true"
    STARTUP_TIMEOUT: Final[float] = float(os.getenv("ORCHESTRATOR_STARTUP_TIMEOUT", "120.0"))

    # Cross-repo startup
    START_PRIME: Final[bool] = os.getenv("START_JARVIS_PRIME", "false").lower() == "true"
    START_REACTOR: Final[bool] = os.getenv("START_REACTOR_CORE", "false").lower() == "true"
    PRIME_PATH: Final[str] = os.getenv("JARVIS_PRIME_PATH", "")
    REACTOR_PATH: Final[str] = os.getenv("REACTOR_CORE_PATH", "")

    # State persistence
    STATE_DIR: Final[Path] = Path(os.getenv("JARVIS_STATE_DIR", str(Path.home() / ".jarvis")))
    CROSS_REPO_STATE_FILE: Final[Path] = STATE_DIR / "cross_repo" / "unified_state.json"


# =============================================================================
# Orchestrator State
# =============================================================================

class OrchestratorState(Enum):
    """Orchestrator lifecycle states."""
    UNINITIALIZED = auto()
    ELECTING_LEADER = auto()
    STARTING_PROXY = auto()
    VERIFYING_PROXY = auto()
    INITIALIZING_COMPONENTS = auto()
    STARTING_CHILD_REPOS = auto()
    RUNNING = auto()
    SHUTTING_DOWN = auto()
    STOPPED = auto()
    FAILED = auto()


# =============================================================================
# Startup Phase
# =============================================================================

@dataclass
class StartupPhase:
    """Tracks a startup phase for observability."""
    name: str
    started_at: float
    completed_at: Optional[float] = None
    success: bool = False
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def duration_seconds(self) -> Optional[float]:
        """Get phase duration."""
        if self.completed_at:
            return self.completed_at - self.started_at
        return None

    def complete(self, success: bool, error: Optional[str] = None) -> None:
        """Mark phase complete."""
        self.completed_at = time.time()
        self.success = success
        self.error = error

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "name": self.name,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_seconds": self.duration_seconds,
            "success": self.success,
            "error": self.error,
            "metadata": self.metadata,
        }


# =============================================================================
# Unified Proxy Orchestrator
# =============================================================================

class UnifiedProxyOrchestrator:
    """
    Central orchestrator coordinating all proxy management layers.

    Startup Sequence:
    1. Leader Election - Determine which repo manages the proxy
    2. Proxy Lifecycle - Start and verify proxy (leader only)
    3. Startup Barrier - Block until proxy verified ready
    4. Component Init - Initialize dependent components in waves
    5. Child Repos - Start Prime/Reactor if leader (optional)

    As Follower:
    - Skip proxy lifecycle (leader manages)
    - Wait for proxy ready via barrier
    - Initialize components normally
    """

    def __init__(
        self,
        repo_name: str = OrchestratorConfig.REPO_NAME,
        components: Optional[List[ComponentManifest]] = None,
    ):
        self._repo_name = repo_name
        self._components = components or []

        # State machine
        self._state = OrchestratorState.UNINITIALIZED
        self._state_lock = asyncio.Lock()

        # Core components (created during startup)
        self._leader: Optional[DistributedProxyLeader] = None
        self._lifecycle: Optional[ProxyLifecycleController] = None
        self._barrier: Optional[AsyncStartupBarrier] = None
        self._health: Optional[UnifiedHealthAggregator] = None
        self._trinity: Optional[Any] = None  # UnifiedTrinityCoordinator

        # Child process management
        self._child_processes: Dict[str, asyncio.subprocess.Process] = {}

        # Startup tracking
        self._startup_phases: List[StartupPhase] = []
        self._startup_start_time: Optional[float] = None
        self._startup_complete_time: Optional[float] = None

        # Shutdown handling
        self._shutdown_event = asyncio.Event()
        self._running = False

    # -------------------------------------------------------------------------
    # State Management
    # -------------------------------------------------------------------------

    @property
    def state(self) -> OrchestratorState:
        """Current orchestrator state."""
        return self._state

    @property
    def is_leader(self) -> bool:
        """Check if this instance is the leader."""
        return self._leader is not None and self._leader.state == LeaderState.LEADER

    @property
    def is_running(self) -> bool:
        """Check if orchestrator is running."""
        return self._state == OrchestratorState.RUNNING

    async def _transition_to(
        self,
        new_state: OrchestratorState,
        reason: str = "",
    ) -> None:
        """Transition to a new orchestrator state."""
        async with self._state_lock:
            old_state = self._state
            self._state = new_state
            logger.info(
                f"[Orchestrator] State: {old_state.name} → {new_state.name} "
                f"({reason if reason else 'no reason'})"
            )

    def _start_phase(self, name: str, metadata: Optional[Dict[str, Any]] = None) -> StartupPhase:
        """Start tracking a new startup phase."""
        phase = StartupPhase(
            name=name,
            started_at=time.time(),
            metadata=metadata or {},
        )
        self._startup_phases.append(phase)
        logger.info(f"[Orchestrator] Phase started: {name}")
        return phase

    def _complete_phase(
        self,
        phase: StartupPhase,
        success: bool,
        error: Optional[str] = None,
    ) -> None:
        """Complete a startup phase."""
        phase.complete(success, error)
        if success:
            logger.info(
                f"[Orchestrator] Phase completed: {phase.name} "
                f"({phase.duration_seconds:.2f}s)"
            )
        else:
            logger.error(
                f"[Orchestrator] Phase failed: {phase.name} - {error}"
            )

    # -------------------------------------------------------------------------
    # Startup Sequence
    # -------------------------------------------------------------------------

    async def start(self) -> bool:
        """
        Execute full startup sequence.

        Returns True if startup succeeded, False otherwise.
        """
        self._startup_start_time = time.time()
        self._running = True

        try:
            # Phase 0: Leader Election
            await self._transition_to(OrchestratorState.ELECTING_LEADER)
            phase = self._start_phase("leader_election")

            # v117.0: Create leader and register callback BEFORE starting
            # This fixes race condition where election completes before callback is registered
            # Factory now returns (leader, cleanup_result) tuple and auto-cleans stale files
            self._leader, cleanup_result = await create_proxy_leader(repo_name=self._repo_name)

            if cleanup_result and cleanup_result.total_removed > 0:
                logger.info(
                    f"[Orchestrator] Cleaned {cleanup_result.total_removed} stale files "
                    f"before leader election"
                )

            # Set up election completion tracking BEFORE starting
            election_complete = asyncio.Event()
            election_state_captured: List[LeaderState] = []

            async def on_leader_state_change(new_state: LeaderState) -> None:
                """Callback for state changes - registered BEFORE start()."""
                logger.debug(f"[Orchestrator] Leader state callback fired: {new_state}")
                if new_state in {LeaderState.LEADER, LeaderState.FOLLOWER}:
                    election_state_captured.append(new_state)
                    election_complete.set()

            # v117.0: CRITICAL - Register callback BEFORE starting election
            self._leader.add_state_callback(on_leader_state_change)

            # Now start the election
            logger.debug("[Orchestrator] Starting leader election...")
            election_success = await self._leader.start()

            # v117.0: Check if election already completed during start()
            if election_complete.is_set():
                logger.debug("[Orchestrator] Election completed synchronously during start()")
            elif election_success:
                # Election started but callback not fired yet - wait with timeout
                try:
                    # v117.0: Increased timeout and made configurable
                    election_timeout = float(os.getenv("ORCHESTRATOR_ELECTION_TIMEOUT", "60.0"))
                    await asyncio.wait_for(election_complete.wait(), timeout=election_timeout)
                except asyncio.TimeoutError:
                    # v117.0: Check state directly before failing - callback might have been missed
                    current_state = self._leader.state
                    if current_state in (LeaderState.LEADER, LeaderState.FOLLOWER):
                        logger.warning(
                            f"[Orchestrator] Election callback may have been missed, "
                            f"but state is valid: {current_state}"
                        )
                        election_state_captured.append(current_state)
                    else:
                        logger.error(
                            f"[Orchestrator] Election timeout - state={current_state}, "
                            f"callback_fired={len(election_state_captured) > 0}"
                        )
                        self._complete_phase(phase, False, "Election timeout")
                        await self._transition_to(OrchestratorState.FAILED, "election_timeout")
                        return False
            else:
                # Election failed completely
                logger.error("[Orchestrator] Election start() returned False")
                self._complete_phase(phase, False, "Election start failed")
                await self._transition_to(OrchestratorState.FAILED, "election_start_failed")
                return False

            is_leader = self._leader.state == LeaderState.LEADER
            self._complete_phase(phase, True)
            phase.metadata["is_leader"] = is_leader

            logger.info(
                f"[Orchestrator] Election complete: {'LEADER' if is_leader else 'FOLLOWER'}"
            )

            # Phase 1: Proxy Lifecycle (Leader only manages, but all verify)
            await self._transition_to(OrchestratorState.STARTING_PROXY)
            phase = self._start_phase("proxy_startup", {"is_leader": is_leader})

            # Create lifecycle controller
            self._lifecycle = await create_lifecycle_controller(
                leader=self._leader,
                use_launchd=is_leader,  # Only leader installs launchd
            )

            # Connect lifecycle state changes to health aggregator
            async def on_proxy_state_change(event: StateTransitionEvent) -> None:
                if self._health:
                    await self._health.log_state_change(
                        from_state=event.from_state.name,
                        to_state=event.to_state.name,
                        reason=event.reason,
                        metadata=event.metadata,
                    )

            self._lifecycle.add_state_callback(on_proxy_state_change)

            if is_leader:
                # Leader starts the proxy
                success = await self._lifecycle.start()
                if not success:
                    self._complete_phase(phase, False, "Proxy start failed")
                    if OrchestratorConfig.CLOUDSQL_REQUIRED:
                        await self._transition_to(OrchestratorState.FAILED, "proxy_start_failed")
                        return False
                    else:
                        logger.warning(
                            "[Orchestrator] Proxy failed but CLOUDSQL_REQUIRED=false, continuing"
                        )
            else:
                # Follower just initializes to observe state
                await self._lifecycle.initialize()

            self._complete_phase(phase, True)

            # Phase 2: Verification Barrier
            await self._transition_to(OrchestratorState.VERIFYING_PROXY)
            phase = self._start_phase("proxy_verification")

            # Create startup barrier with lifecycle reference
            self._barrier = await create_startup_barrier(
                lifecycle_controller=self._lifecycle,
                components=self._components,
            )

            # Ensure CloudSQL is verified ready (all repos do this)
            cloudsql_ready = await self._barrier.ensure_cloudsql_ready(
                timeout=OrchestratorConfig.STARTUP_TIMEOUT / 2
            )

            if not cloudsql_ready:
                self._complete_phase(phase, False, "CloudSQL verification failed")
                if OrchestratorConfig.CLOUDSQL_REQUIRED:
                    await self._transition_to(OrchestratorState.FAILED, "cloudsql_not_ready")
                    return False
                else:
                    logger.warning(
                        "[Orchestrator] CloudSQL not ready but CLOUDSQL_REQUIRED=false"
                    )

            self._complete_phase(phase, True)

            # Phase 3: Start Health Aggregator
            self._health = await create_health_aggregator(
                source_repo=self._repo_name,
                lifecycle_controller=self._lifecycle,
                auto_start=True,
            )

            # Register anomaly callback
            async def on_anomaly(
                snapshot: HealthSnapshot,
                anomaly_type: AnomalyType,
                details: Dict[str, Any],
            ) -> None:
                logger.warning(
                    f"[Orchestrator] Anomaly: {anomaly_type.name} - {details}"
                )
                # Could trigger recovery actions here

            self._health.add_anomaly_callback(on_anomaly)

            # Phase 4: Component Initialization
            await self._transition_to(OrchestratorState.INITIALIZING_COMPONENTS)
            phase = self._start_phase("component_initialization")

            if self._components:
                succeeded, failed, skipped = await self._barrier.initialize_all()
                phase.metadata["succeeded"] = succeeded
                phase.metadata["failed"] = failed
                phase.metadata["skipped"] = skipped

                if failed > 0:
                    # Check if any required components failed
                    required_failed = any(
                        comp.required and not self._barrier._init_results.get(comp.name, (False,))[0]
                        for comp in self._components
                    )
                    if required_failed:
                        self._complete_phase(phase, False, f"{failed} required components failed")
                        await self._transition_to(OrchestratorState.FAILED, "component_init_failed")
                        return False

            self._complete_phase(phase, True)

            # Phase 5: Cross-Repo Startup via Trinity Coordinator (Leader only)
            if is_leader:
                await self._transition_to(OrchestratorState.STARTING_CHILD_REPOS)
                phase = self._start_phase("trinity_coordination")

                try:
                    from .trinity_coordinator import (
                        get_trinity_coordinator,
                        integrate_with_proxy_orchestrator,
                    )

                    # Get Trinity coordinator (single source of truth)
                    self._trinity = await get_trinity_coordinator(
                        is_leader=True,
                        auto_register=True,
                    )

                    # Integrate proxy health with Trinity
                    await integrate_with_proxy_orchestrator(self._trinity)

                    # Start Trinity coordination (handles Prime/Reactor)
                    await self._trinity.start()

                    self._complete_phase(phase, True)

                except ImportError as e:
                    logger.debug(f"[Orchestrator] Trinity coordinator not available: {e}")
                    # Fall back to legacy child repo start
                    await self._start_child_repos()
                    self._complete_phase(phase, True, metadata={"fallback": True})

                except Exception as e:
                    logger.warning(f"[Orchestrator] Trinity startup warning: {e}")
                    self._complete_phase(phase, True, error=str(e))

            # Complete!
            await self._transition_to(OrchestratorState.RUNNING)
            self._startup_complete_time = time.time()

            total_duration = self._startup_complete_time - self._startup_start_time
            logger.info(
                f"[Orchestrator] ✅ Startup complete in {total_duration:.2f}s "
                f"(role: {'LEADER' if is_leader else 'FOLLOWER'})"
            )

            # Persist state
            await self._persist_state()

            return True

        except Exception as e:
            logger.exception(f"[Orchestrator] Startup failed: {e}")
            await self._transition_to(OrchestratorState.FAILED, str(e))
            return False

    async def _start_child_repos(self) -> None:
        """Start child repository processes (Prime, Reactor)."""
        repos_to_start = []

        if OrchestratorConfig.START_PRIME and OrchestratorConfig.PRIME_PATH:
            repos_to_start.append(("prime", OrchestratorConfig.PRIME_PATH))

        if OrchestratorConfig.START_REACTOR and OrchestratorConfig.REACTOR_PATH:
            repos_to_start.append(("reactor", OrchestratorConfig.REACTOR_PATH))

        for repo_name, repo_path in repos_to_start:
            phase = self._start_phase(f"start_{repo_name}", {"path": repo_path})

            try:
                repo_dir = Path(repo_path)
                if not repo_dir.exists():
                    logger.warning(f"[Orchestrator] {repo_name} path not found: {repo_path}")
                    self._complete_phase(phase, False, "Path not found")
                    continue

                # Start the subprocess
                process = await asyncio.create_subprocess_exec(
                    "python3", "run_supervisor.py",
                    cwd=str(repo_dir),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env={
                        **os.environ,
                        "JARVIS_REPO_NAME": repo_name,
                        "START_JARVIS_PRIME": "false",  # Don't cascade
                        "START_REACTOR_CORE": "false",
                    },
                )

                self._child_processes[repo_name] = process
                logger.info(
                    f"[Orchestrator] Started {repo_name} (PID: {process.pid})"
                )
                self._complete_phase(phase, True)

            except Exception as e:
                logger.error(f"[Orchestrator] Failed to start {repo_name}: {e}")
                self._complete_phase(phase, False, str(e))

    # -------------------------------------------------------------------------
    # Shutdown
    # -------------------------------------------------------------------------

    async def stop(self, graceful: bool = True) -> None:
        """
        Shutdown the orchestrator and all components.

        Args:
            graceful: If True, wait for components to finish cleanly
        """
        if self._state == OrchestratorState.STOPPED:
            return

        await self._transition_to(OrchestratorState.SHUTTING_DOWN)
        self._running = False
        self._shutdown_event.set()

        logger.info("[Orchestrator] Starting shutdown...")

        # Stop Trinity coordinator (handles child processes)
        if self._trinity:
            try:
                await self._trinity.stop(graceful=graceful)
            except Exception as e:
                logger.warning(f"[Orchestrator] Trinity shutdown warning: {e}")

        # Stop health aggregator
        if self._health:
            await self._health.stop()

        # Stop child processes (legacy fallback)
        for repo_name, process in self._child_processes.items():
            try:
                if graceful:
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=10.0)
                    except asyncio.TimeoutError:
                        process.kill()
                else:
                    process.kill()
                logger.info(f"[Orchestrator] Stopped child: {repo_name}")
            except Exception as e:
                logger.warning(f"[Orchestrator] Error stopping {repo_name}: {e}")

        # Stop lifecycle controller (leader stops proxy)
        if self._lifecycle:
            if self.is_leader:
                await self._lifecycle.stop(graceful=graceful)
            # Followers just detach

        # Stop leader election
        if self._leader:
            await self._leader.stop()

        await self._transition_to(OrchestratorState.STOPPED)
        logger.info("[Orchestrator] Shutdown complete")

    async def wait_for_shutdown(self) -> None:
        """Wait for shutdown signal."""
        await self._shutdown_event.wait()

    # -------------------------------------------------------------------------
    # State Persistence
    # -------------------------------------------------------------------------

    async def _persist_state(self) -> None:
        """Persist orchestrator state to shared file."""
        try:
            OrchestratorConfig.CROSS_REPO_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

            state_data = {
                "version": "1.0",
                "last_updated": time.time(),
                "leader": {
                    "id": self._leader._identity.to_string() if self._leader and self._leader._identity else None,
                    "repo": self._repo_name if self.is_leader else None,
                    "heartbeat": time.time() if self.is_leader else None,
                    "proxy_state": (
                        self._lifecycle.state.name
                        if self._lifecycle else "UNKNOWN"
                    ),
                } if self.is_leader else None,
                "followers": [],  # TODO: Track followers
                "proxy": {
                    "state": self._lifecycle.state.name if self._lifecycle else "UNKNOWN",
                    "pid": self._lifecycle.pid if self._lifecycle else None,
                    "port": 5432,
                    "uptime_seconds": self._lifecycle.uptime_seconds if self._lifecycle else None,
                    "launchd_managed": self.is_leader,
                },
                "startup": {
                    "total_duration_seconds": (
                        self._startup_complete_time - self._startup_start_time
                        if self._startup_complete_time and self._startup_start_time
                        else None
                    ),
                    "phases": [p.to_dict() for p in self._startup_phases],
                },
            }

            # Atomic write
            tmp_file = OrchestratorConfig.CROSS_REPO_STATE_FILE.with_suffix(".tmp")
            with open(tmp_file, "w") as f:
                json.dump(state_data, f, indent=2)
            tmp_file.rename(OrchestratorConfig.CROSS_REPO_STATE_FILE)

        except Exception as e:
            logger.warning(f"[Orchestrator] Failed to persist state: {e}")

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    def register_component(self, manifest: ComponentManifest) -> None:
        """Register a component for managed initialization."""
        self._components.append(manifest)
        if self._barrier:
            self._barrier.register_component(manifest)

    async def ensure_ready(self, timeout: Optional[float] = None) -> bool:
        """
        Ensure the system is ready (blocking call).

        Useful for code that needs to wait for full initialization.
        """
        if self._state == OrchestratorState.RUNNING:
            return True

        if self._state in {OrchestratorState.FAILED, OrchestratorState.STOPPED}:
            return False

        # Wait for startup to complete
        deadline = time.monotonic() + (timeout or OrchestratorConfig.STARTUP_TIMEOUT)
        while time.monotonic() < deadline:
            if self._state == OrchestratorState.RUNNING:
                return True
            if self._state in {OrchestratorState.FAILED, OrchestratorState.STOPPED}:
                return False
            await asyncio.sleep(0.5)

        return False

    def get_status(self) -> Dict[str, Any]:
        """Get comprehensive orchestrator status."""
        return {
            "state": self._state.name,
            "repo_name": self._repo_name,
            "is_leader": self.is_leader,
            "running": self._running,
            "startup": {
                "start_time": self._startup_start_time,
                "complete_time": self._startup_complete_time,
                "total_duration": (
                    self._startup_complete_time - self._startup_start_time
                    if self._startup_complete_time and self._startup_start_time
                    else None
                ),
                "phases": [p.to_dict() for p in self._startup_phases],
            },
            "leader": self._leader.get_status() if self._leader else None,
            "lifecycle": self._lifecycle.get_status() if self._lifecycle else None,
            "barrier": self._barrier.get_status() if self._barrier else None,
            "health": self._health.get_status() if self._health else None,
            "child_processes": {
                name: proc.pid for name, proc in self._child_processes.items()
            },
        }

    async def diagnose(self, failure_time: Optional[float] = None) -> Dict[str, Any]:
        """Run diagnosis on a failure."""
        if self._health:
            return await self._health.diagnose(failure_time)
        return {"error": "Health aggregator not available"}

    @asynccontextmanager
    async def managed(self) -> AsyncIterator[UnifiedProxyOrchestrator]:
        """
        Context manager for orchestrator lifecycle.

        Usage:
            async with UnifiedProxyOrchestrator().managed() as orchestrator:
                # System is fully started
                await orchestrator.wait_for_shutdown()
        """
        try:
            success = await self.start()
            if not success:
                raise RuntimeError("Orchestrator startup failed")
            yield self
        finally:
            await self.stop()


# =============================================================================
# Signal Handlers
# =============================================================================

def setup_signal_handlers(orchestrator: UnifiedProxyOrchestrator) -> None:
    """Setup graceful shutdown signal handlers."""
    loop = asyncio.get_event_loop()

    def signal_handler(signum: int) -> None:
        logger.info(f"[Orchestrator] Received signal {signum}, initiating shutdown")
        asyncio.create_task(orchestrator.stop(graceful=True))

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda s=sig: signal_handler(s))


# =============================================================================
# Factory Function
# =============================================================================

async def create_orchestrator(
    repo_name: str = OrchestratorConfig.REPO_NAME,
    components: Optional[List[ComponentManifest]] = None,
    auto_start: bool = False,
) -> UnifiedProxyOrchestrator:
    """
    Factory function to create an orchestrator.

    Args:
        repo_name: Repository name for identification
        components: Optional list of components to initialize
        auto_start: If True, start immediately

    Returns:
        Configured UnifiedProxyOrchestrator
    """
    orchestrator = UnifiedProxyOrchestrator(
        repo_name=repo_name,
        components=components,
    )

    if auto_start:
        await orchestrator.start()

    return orchestrator


# =============================================================================
# Main Entry Point (for testing)
# =============================================================================

async def main() -> None:
    """Main entry point for standalone testing."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    orchestrator = await create_orchestrator(auto_start=False)
    setup_signal_handlers(orchestrator)

    async with orchestrator.managed():
        logger.info("[Main] Orchestrator running, press Ctrl+C to stop")
        await orchestrator.wait_for_shutdown()


if __name__ == "__main__":
    asyncio.run(main())
