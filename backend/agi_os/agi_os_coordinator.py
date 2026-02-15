"""
JARVIS AGI OS - Main Coordinator

The central coordinator for the Autonomous General Intelligence Operating System.
Integrates all JARVIS systems into a cohesive autonomous platform:

- MAS (Multi-Agent System): Neural Mesh agent coordination
- SAI (Self-Aware Intelligence): Self-monitoring and optimization
- CAI (Context Awareness Intelligence): Intent prediction
- UAE (Unified Awareness Engine): Real-time context aggregation
- Voice Communication: Real-time Daniel TTS
- Approval System: Voice-based user approvals
- Event Stream: Event-driven architecture
- Action Orchestration: Detection → Decision → Approval → Execution

This is the main entry point for AGI OS functionality.

Architecture:
    ┌─────────────────────────────────────────────────────────────────┐
    │                      AGI OS Coordinator                         │
    ├─────────────────────────────────────────────────────────────────┤
    │                                                                 │
    │  ┌─────────────────────────────────────────────────────────┐   │
    │  │              Intelligence Layer (MAS+SAI+CAI+UAE)        │   │
    │  │  ┌───────┐ ┌───────┐ ┌───────┐ ┌───────┐ ┌───────────┐  │   │
    │  │  │  UAE  │ │  SAI  │ │  CAI  │ │Neural │ │  Hybrid   │  │   │
    │  │  │Engine │ │System │ │System │ │ Mesh  │ │Orchestrator│  │   │
    │  │  └───────┘ └───────┘ └───────┘ └───────┘ └───────────┘  │   │
    │  └─────────────────────────────────────────────────────────┘   │
    │                              │                                  │
    │  ┌─────────────────────────────────────────────────────────┐   │
    │  │              Autonomous Operation Layer                  │   │
    │  │  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌────────────┐  │   │
    │  │  │ Decision │ │ Approval │ │  Action  │ │ Permission │  │   │
    │  │  │  Engine  │ │ Manager  │ │ Executor │ │  Manager   │  │   │
    │  │  └──────────┘ └──────────┘ └──────────┘ └────────────┘  │   │
    │  └─────────────────────────────────────────────────────────┘   │
    │                              │                                  │
    │  ┌─────────────────────────────────────────────────────────┐   │
    │  │                Communication Layer                       │   │
    │  │  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌────────────┐  │   │
    │  │  │  Voice   │ │  Event   │ │  Screen  │ │   Action   │  │   │
    │  │  │Communicator│ │ Stream  │ │ Analyzer │ │Orchestrator│  │   │
    │  │  └──────────┘ └──────────┘ └──────────┘ └────────────┘  │   │
    │  └─────────────────────────────────────────────────────────┘   │
    │                                                                 │
    └─────────────────────────────────────────────────────────────────┘

Usage:
    from agi_os import get_agi_os, start_agi_os

    # Start AGI OS
    agi = await start_agi_os()

    # Now JARVIS operates autonomously:
    # - Monitors your screen
    # - Detects issues and opportunities
    # - Makes intelligent decisions
    # - Asks for approval when needed (via voice)
    # - Executes approved actions
    # - Learns from your decisions
    # - Reports progress via Daniel voice

    # Stop when done
    await stop_agi_os()
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import Any, Awaitable, Callable, Dict, List, Optional

from .realtime_voice_communicator import (
    RealTimeVoiceCommunicator,
    VoiceMode,
    get_voice_communicator,
    stop_voice_communicator,
)
from .voice_approval_manager import (
    VoiceApprovalManager,
    get_approval_manager,
)
from .proactive_event_stream import (
    ProactiveEventStream,
    AGIEvent,
    EventType,
    EventPriority,
    get_event_stream,
    stop_event_stream,
)
from .intelligent_action_orchestrator import (
    IntelligentActionOrchestrator,
    get_action_orchestrator,
    start_action_orchestrator,
    stop_action_orchestrator,
)
from .owner_identity_service import (
    OwnerIdentityService,
    get_owner_identity,
)

logger = logging.getLogger(__name__)


def _env_bool(key: str, default: bool) -> bool:
    """Read a boolean from env vars. v241.0."""
    val = os.getenv(key, str(default)).lower()
    return val in ("true", "1", "yes")


def _env_float(key: str, default: float) -> float:
    """Read a float from env vars with safe fallback."""
    try:
        return float(os.getenv(key, str(default)))
    except (TypeError, ValueError):
        return default


class AGIOSState(Enum):
    """State of the AGI OS."""
    OFFLINE = "offline"
    INITIALIZING = "initializing"
    ONLINE = "online"
    DEGRADED = "degraded"  # Some components unavailable
    PAUSED = "paused"
    SHUTTING_DOWN = "shutting_down"


@dataclass
class ComponentStatus:
    """Status of a component."""
    name: str
    available: bool
    healthy: bool = True
    last_check: datetime = field(default_factory=datetime.now)
    error: Optional[str] = None


class AGIOSCoordinator:
    """
    Main coordinator for JARVIS AGI OS.

    Integrates all systems:
    - AGI OS components (voice, approval, events, orchestration)
    - Intelligence systems (UAE, SAI, CAI)
    - Neural Mesh (multi-agent coordination)
    - Hybrid Orchestrator (routing and execution)
    - Autonomy systems (decision, permission, execution)
    """

    def __init__(self):
        """Initialize the AGI OS coordinator."""
        # State
        self._state = AGIOSState.OFFLINE
        self._started_at: Optional[datetime] = None

        # AGI OS Components
        self._voice: Optional[RealTimeVoiceCommunicator] = None
        self._approval_manager: Optional[VoiceApprovalManager] = None
        self._event_stream: Optional[ProactiveEventStream] = None
        self._action_orchestrator: Optional[IntelligentActionOrchestrator] = None

        # Intelligence Systems (lazy loaded)
        self._uae_engine: Optional[Any] = None
        self._sai_system: Optional[Any] = None
        self._cai_system: Optional[Any] = None
        self._learning_db: Optional[Any] = None

        # Neural Mesh
        self._neural_mesh: Optional[Any] = None
        self._jarvis_bridge: Optional[Any] = None  # v237.0: JARVISNeuralMeshBridge
        self._event_bridge: Optional[Any] = None  # v237.2: NeuralMesh ↔ EventStream bridge
        self._notification_monitor: Optional[Any] = None  # v237.2: macOS notification listener
        self._system_event_monitor: Optional[Any] = None  # v237.3: macOS system event listener
        self._ghost_hands: Optional[Any] = None  # v237.4: Ghost Hands orchestrator
        self._ghost_display: Optional[Any] = None  # v237.4: Ghost display manager

        # Hybrid Orchestrator
        self._hybrid_orchestrator: Optional[Any] = None

        # Screen Analyzer
        self._screen_analyzer: Optional[Any] = None

        # Owner Identity Service (dynamic voice biometric identification)
        self._owner_identity: Optional[OwnerIdentityService] = None

        # Speaker Verification Service (for voice biometrics)
        self._speaker_verification: Optional[Any] = None

        # v241.0: Agent Runtime reference (lazy-resolved from singleton)
        self._agent_runtime: Optional[Any] = None

        # Component status
        self._component_status: Dict[str, ComponentStatus] = {}

        # Configuration
        self._config = {
            'enable_voice': True,
            'enable_proactive_monitoring': True,
            'enable_autonomous_actions': True,
            'voice_greeting': True,
            'health_check_interval': 30,
        }

        # Background tasks
        self._health_task: Optional[asyncio.Task] = None
        # v251.2: Strong refs prevent GC of fire-and-forget tasks
        self._background_tasks: set = set()

        # v253.4: Track registered callbacks for cleanup on stop()
        self._agent_status_callback: Optional[Any] = None
        self._agent_status_registry: Optional[Any] = None
        self._approval_callback: Optional[Any] = None

        # Statistics
        self._stats = {
            'uptime_seconds': 0,
            'commands_processed': 0,
            'actions_executed': 0,
            'voice_messages': 0,
        }

        logger.info("AGIOSCoordinator created")

    @property
    def state(self) -> AGIOSState:
        """Get current AGI OS state."""
        return self._state

    @property
    def component_status(self) -> Dict[str, ComponentStatus]:
        """Get component status dictionary."""
        return self._component_status

    async def start(self, progress_callback=None, startup_budget_seconds: Optional[float] = None) -> None:
        """
        Start the AGI OS.

        Initializes all components and begins autonomous operation.

        Args:
            progress_callback: Optional async callable(step: str, detail: str)
                Reports initialization progress back to the supervisor so the
                DMS watchdog doesn't trigger a stall during the 60-75s init
                sequence. v250.0: Without this, progress 86→87 gap exceeds
                the 60s stall threshold.
            startup_budget_seconds: Optional total AGI startup budget. When
                provided, per-phase timeouts are scaled to fit this budget.
        """
        if self._state == AGIOSState.ONLINE:
            logger.warning("AGI OS already online")
            return

        self._state = AGIOSState.INITIALIZING
        self._started_at = datetime.now()
        logger.info("Starting AGI OS...")

        # v252.1: Reset notification bridge for warm restarts (stop → start
        # without process exit leaves _shutting_down=True permanently).
        try:
            from agi_os.notification_bridge import reset_notifications
            reset_notifications()
        except ImportError:
            pass

        # v250.1: Store callback as instance attr so sub-methods can report
        # intra-phase progress. Without this, _init_agi_os_components() runs
        # 90+ seconds with zero DMS heartbeats → stall → rollback.
        self._progress_callback = progress_callback
        progress_callback_timeout = max(
            0.1, _env_float("JARVIS_AGI_OS_PROGRESS_CALLBACK_TIMEOUT", 0.75)
        )
        self._progress_callback_timeout = progress_callback_timeout

        async def _report(step: str, detail: str) -> None:
            if progress_callback:
                try:
                    await asyncio.wait_for(
                        progress_callback(step, detail),
                        timeout=progress_callback_timeout,
                    )
                except asyncio.TimeoutError:
                    logger.debug(
                        "Progress callback timed out after %.2fs for step=%s",
                        progress_callback_timeout,
                        step,
                    )
                except Exception as e:
                    logger.debug("Progress callback error (non-fatal): %s", e)

        # v253.1: Top-level AGI startup phases now use explicit per-phase
        # bounded timeouts + heartbeat reporting. This prevents a single
        # hanging sub-phase from holding supervisor startup indefinitely.
        # v253.6: Rebalanced phase defaults to reduce total from 305s→225s.
        # Previous 305s total always triggered budget scaling on the default
        # 110s budget, compressing neural_mesh from 90s to ~30s and causing
        # agent/bridge timeouts. New defaults:
        #   components:  45→30 (parallel init, doesn't need 45s)
        #   intelligence: 60→45 (time-budgeted internally)
        #   neural_mesh: 90 (kept — genuinely needs time for 18+ agents)
        #   hybrid: 20→10 (just object construction)
        #   screen_analyzer: 45→30 (fixed Py3.9 threading, faster now)
        #   components_connected: 45→20 (lightweight wiring)
        phase_timeouts = {
            "agi_os_components": _env_float(
                "JARVIS_AGI_OS_PHASE_COMPONENTS_TIMEOUT", 30.0
            ),
            "intelligence_systems": _env_float(
                "JARVIS_AGI_OS_PHASE_INTELLIGENCE_TIMEOUT", 45.0
            ),
            "neural_mesh": _env_float(
                "JARVIS_AGI_OS_PHASE_NEURAL_MESH_TIMEOUT", 90.0
            ),
            "hybrid_orchestrator": _env_float(
                "JARVIS_AGI_OS_PHASE_HYBRID_TIMEOUT", 10.0
            ),
            "screen_analyzer": _env_float(
                "JARVIS_AGI_OS_PHASE_SCREEN_TIMEOUT", 30.0
            ),
            "components_connected": _env_float(
                "JARVIS_AGI_OS_PHASE_CONNECT_TIMEOUT", 20.0
            ),
        }

        # Keep phase-level budgets aligned with the caller's startup envelope.
        effective_budget = startup_budget_seconds
        if effective_budget is None:
            env_budget = _env_float("JARVIS_AGI_OS_PHASE_TOTAL_BUDGET", 0.0)
            effective_budget = env_budget if env_budget > 0 else None

        if effective_budget is not None:
            phase_budget_reserve = max(
                1.0, _env_float("JARVIS_AGI_OS_PHASE_BUDGET_RESERVE", 8.0)
            )
            phase_min_timeout = max(
                0.5, _env_float("JARVIS_AGI_OS_PHASE_MIN_TIMEOUT", 5.0)
            )
            available_phase_budget = max(
                len(phase_timeouts) * 0.5,
                float(effective_budget) - phase_budget_reserve,
            )
            phase_total = sum(float(v) for v in phase_timeouts.values())

            if phase_total > available_phase_budget:
                keys = list(phase_timeouts.keys())
                min_total = phase_min_timeout * len(keys)
                scaled: Dict[str, float] = {}
                if available_phase_budget <= min_total:
                    even = max(0.5, available_phase_budget / max(1, len(keys)))
                    for key in keys:
                        scaled[key] = even
                else:
                    adjustable_total = sum(
                        max(0.0, float(phase_timeouts[key]) - phase_min_timeout)
                        for key in keys
                    )
                    remainder = available_phase_budget - min_total
                    for key in keys:
                        base = phase_min_timeout
                        headroom = max(
                            0.0, float(phase_timeouts[key]) - phase_min_timeout
                        )
                        if adjustable_total > 0:
                            base += remainder * (headroom / adjustable_total)
                        scaled[key] = max(0.5, base)
                phase_timeouts.update(scaled)
                logger.warning(
                    "AGI startup phase timeouts scaled to fit budget %.1fs "
                    "(original total %.1fs, scaled total %.1fs)",
                    float(effective_budget),
                    phase_total,
                    sum(phase_timeouts.values()),
                )

        # v253.3: Store scaled phase budgets so sub-methods
        # (e.g. _init_agi_os_components) can read the allocated time
        # and dynamically size per-component timeouts.
        self._phase_budgets = dict(phase_timeouts)

        async def _run_phase(
            step_key: str,
            success_detail: str,
            operation: Callable[[], Awaitable[Any]],
            *,
            critical: bool = False,
        ) -> bool:
            timeout_seconds = max(1.0, float(phase_timeouts.get(step_key, 30.0)))
            await _report(step_key, f"Starting {step_key}")
            started = time.monotonic()
            try:
                await self._run_timed_init_step(
                    step_name=f"phase_{step_key}",
                    operation=operation,
                    timeout_seconds=timeout_seconds,
                )
                elapsed = time.monotonic() - started
                await _report(step_key, f"{success_detail} ({elapsed:.1f}s)")
                return True
            except asyncio.TimeoutError as e:
                logger.warning(
                    "AGI OS phase '%s' timed out after %.1fs",
                    step_key,
                    timeout_seconds,
                )
                self._component_status[f"phase_{step_key}"] = ComponentStatus(
                    name=f"phase_{step_key}",
                    available=False,
                    healthy=False,
                    error=str(e),
                )
                if critical:
                    raise
                await _report(
                    step_key,
                    f"{step_key} timed out - continuing in degraded mode",
                )
                return False
            except Exception as e:
                logger.warning("AGI OS phase '%s' failed: %s", step_key, e)
                self._component_status[f"phase_{step_key}"] = ComponentStatus(
                    name=f"phase_{step_key}",
                    available=False,
                    healthy=False,
                    error=str(e),
                )
                if critical:
                    raise
                await _report(
                    step_key,
                    f"{step_key} failed - continuing in degraded mode",
                )
                return False

        # v253.3: Changed from critical=True to critical=False.
        # Each component already handles its own failure gracefully (try/except
        # with ComponentStatus). The phase failing shouldn't kill the entire
        # AGI OS — it should degrade. When budget scaling reduces the phase
        # from 45s to ~15s, critical=True caused guaranteed AGI OS failure
        # even though the individual components were fine.
        await _run_phase(
            "agi_os_components",
            "Core components initialized",
            self._init_agi_os_components,
        )
        await _run_phase(
            "intelligence_systems",
            "Intelligence systems initialized",
            self._init_intelligence_systems,
        )
        await _run_phase(
            "neural_mesh",
            "Neural Mesh initialized",
            self._init_neural_mesh,
        )
        await _run_phase(
            "hybrid_orchestrator",
            "Hybrid orchestrator initialized",
            self._init_hybrid_orchestrator,
        )
        await _run_phase(
            "screen_analyzer",
            "Screen analyzer initialized",
            self._init_screen_analyzer,
        )
        await _run_phase(
            "components_connected",
            "All components connected",
            self._connect_components,
        )

        # Start health monitoring
        self._health_task = asyncio.create_task(
            self._health_monitor_loop(),
            name="agi_os_health_monitor"
        )

        # Determine final state
        self._state = self._determine_health_state()

        # Voice greeting
        if self._config['voice_greeting'] and self._voice:
            await self._announce_startup()

        logger.info("AGI OS started (state=%s)", self._state.value)

    async def stop(self) -> None:
        """
        Stop the AGI OS.

        Gracefully shuts down all components.
        """
        if self._state == AGIOSState.OFFLINE:
            return

        self._state = AGIOSState.SHUTTING_DOWN
        logger.info("Stopping AGI OS...")

        stop_step_timeout = max(0.1, _env_float("AGI_OS_STOP_STEP_TIMEOUT", 8.0))
        health_task_timeout = max(0.1, _env_float("AGI_OS_HEALTH_TASK_STOP_TIMEOUT", 3.0))
        farewell_timeout = max(0.1, _env_float("AGI_OS_SHUTDOWN_ANNOUNCE_TIMEOUT", 2.5))
        mesh_stop_timeout = max(
            stop_step_timeout,
            _env_float("AGI_OS_NEURAL_MESH_STOP_TIMEOUT", 20.0),
        )
        bridge_stop_timeout = max(
            stop_step_timeout,
            _env_float("AGI_OS_NEURAL_BRIDGE_STOP_TIMEOUT", 15.0),
        )
        hybrid_stop_timeout = max(
            stop_step_timeout,
            _env_float("AGI_OS_HYBRID_STOP_TIMEOUT", 12.0),
        )

        async def _run_stop_step(
            name: str,
            step: Callable[[], Awaitable[Any]],
            timeout: Optional[float] = None,
        ) -> None:
            step_timeout = stop_step_timeout if timeout is None else max(0.1, timeout)
            try:
                await asyncio.wait_for(step(), timeout=step_timeout)
            except asyncio.TimeoutError:
                logger.warning("Timed out stopping %s after %.1fs", name, step_timeout)
            except asyncio.CancelledError:
                raise
            except RuntimeError as e:
                if "Event loop is closed" in str(e):
                    logger.debug("Skipping %s stop: event loop already closed", name)
                else:
                    logger.warning("Error stopping %s: %s", name, e)
            except Exception as e:
                logger.warning("Error stopping %s: %s", name, e)

        def _resolve_stop_callable(
            component: Any,
            method_names: List[str],
        ) -> tuple[Optional[Callable[[], Awaitable[Any]]], Optional[str]]:
            """
            Resolve a real stop-like method without triggering proxy __getattr__.
            """
            if component is None:
                return None, None

            for method_name in method_names:
                try:
                    inspect.getattr_static(component, method_name)
                except AttributeError:
                    continue

                method = getattr(component, method_name, None)
                if not callable(method):
                    continue

                async def _invoke(bound_method=method):
                    result = bound_method()
                    if inspect.isawaitable(result):
                        await result
                    return result

                return _invoke, method_name

            return None, None

        async def _announce_shutdown() -> None:
            if not self._voice:
                return

            owner_name = "sir"  # Fallback
            if self._owner_identity:
                try:
                    owner_profile = await self._owner_identity.get_current_owner()
                    if owner_profile and owner_profile.name:
                        owner_name = owner_profile.name
                except Exception:
                    pass

            import random
            shutdown_messages = [
                f"JARVIS going offline. Goodbye, {owner_name}.",
                f"Shutting down now. See you soon, {owner_name}.",
                f"JARVIS offline. Take care, {owner_name}.",
                f"Systems shutting down. Until next time, {owner_name}.",
            ]
            await self._voice.speak(random.choice(shutdown_messages), mode=VoiceMode.QUIET)

        try:
            # Best-effort farewell with a strict timeout so it never blocks teardown.
            if self._voice:
                await _run_stop_step("shutdown announcement", _announce_shutdown, timeout=farewell_timeout)

            # Cancel fire-and-forget AGI tasks first to stop new work during teardown.
            if self._background_tasks:
                for task in list(self._background_tasks):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*list(self._background_tasks), return_exceptions=True)
                self._background_tasks.clear()

            # Cancel health monitor.
            if self._health_task:
                health_task = self._health_task
                self._health_task = None
                task_loop_closed = False
                with contextlib.suppress(Exception):
                    task_loop_closed = health_task.get_loop().is_closed()
                if task_loop_closed:
                    logger.debug(
                        "Skipping AGI OS health task cancellation: task loop already closed"
                    )
                elif not health_task.done():
                    try:
                        health_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await asyncio.wait_for(
                                health_task, timeout=health_task_timeout
                            )
                    except RuntimeError as e:
                        if "Event loop is closed" in str(e):
                            logger.debug(
                                "Skipping AGI OS health task await: event loop closed"
                            )
                        else:
                            raise

            # v253.4: Unregister approval callback before stopping event stream.
            if self._approval_callback and self._approval_manager:
                try:
                    if hasattr(self._approval_manager, 'remove_approval_callback'):
                        self._approval_manager.remove_approval_callback(
                            self._approval_callback,
                        )
                except Exception as e:
                    logger.debug("Approval callback cleanup failed: %s", e)
                self._approval_callback = None

            # v237.2: Stop event bridge first (unsubscribes from bus + event stream).
            if self._event_bridge:
                await _run_stop_step("event bridge", self._event_bridge.stop)

            # v253.4: Parallel monitor shutdown. Components were initialized in
            # parallel (v253.3 asyncio.gather) but stopped sequentially — taking
            # up to 64s worst case (8 × 8s timeout). Now independent monitors
            # stop in parallel: time = max(individual) instead of sum(individual).
            #
            # Group 1 (fully independent): notification_monitor, system_event_monitor,
            #   screen_analyzer — no ordering constraints between them.
            # Group 2 (ordered): Ghost Display THEN Ghost Hands (v237.4 dependency).
            _parallel_stops = []

            if self._notification_monitor:
                _parallel_stops.append(
                    _run_stop_step("NotificationMonitor", self._notification_monitor.stop)
                )

            if self._system_event_monitor:
                _parallel_stops.append(
                    _run_stop_step("SystemEventMonitor", self._system_event_monitor.stop)
                )

            if self._screen_analyzer and hasattr(self._screen_analyzer, 'stop_monitoring'):
                _parallel_stops.append(
                    _run_stop_step("screen analyzer", self._screen_analyzer.stop_monitoring)
                )

            # Ghost Display → Ghost Hands (ordered) wrapped as a single coroutine
            async def _stop_ghost_pair():
                if self._ghost_display and hasattr(self._ghost_display, 'cleanup'):
                    await _run_stop_step("Ghost Display", self._ghost_display.cleanup)
                if self._ghost_hands:
                    _gs, _gm = _resolve_stop_callable(
                        self._ghost_hands, ["stop", "shutdown", "cleanup", "close"]
                    )
                    if _gs:
                        await _run_stop_step(f"Ghost Hands ({_gm})", _gs)

            _parallel_stops.append(_stop_ghost_pair())

            if _parallel_stops:
                await asyncio.gather(*_parallel_stops, return_exceptions=True)

            runtime = self._resolve_agent_runtime()
            if runtime and hasattr(runtime, "stop"):
                await _run_stop_step("agent runtime", runtime.stop)

            # Stop hybrid orchestrator (closes backend client sessions and lifecycle manager).
            if self._hybrid_orchestrator and hasattr(self._hybrid_orchestrator, "stop"):
                await _run_stop_step(
                    "Hybrid Orchestrator",
                    self._hybrid_orchestrator.stop,
                    timeout=hybrid_stop_timeout,
                )

            # v253.4: Parallel shutdown of identity/verification/notification services.
            _svc_stops = []

            if self._owner_identity and hasattr(self._owner_identity, "shutdown"):
                _svc_stops.append(
                    _run_stop_step("Owner Identity Service", self._owner_identity.shutdown)
                )

            _spk_step, _spk_method = _resolve_stop_callable(
                self._speaker_verification,
                ["shutdown", "stop", "cleanup", "close"],
            )
            if _spk_step:
                _svc_stops.append(
                    _run_stop_step(
                        f"Speaker Verification Service ({_spk_method})", _spk_step,
                    )
                )

            async def _stop_notification_bridge():
                try:
                    from agi_os.notification_bridge import shutdown_notifications
                    shutdown_notifications()
                except ImportError:
                    pass
            _svc_stops.append(_run_stop_step("notification bridge", _stop_notification_bridge))

            if _svc_stops:
                await asyncio.gather(*_svc_stops, return_exceptions=True)

            # v253.4: Parallel singleton teardown (independent of each other).
            await asyncio.gather(
                _run_stop_step("action orchestrator", stop_action_orchestrator),
                _run_stop_step("event stream", stop_event_stream),
                _run_stop_step("voice communicator", stop_voice_communicator),
                return_exceptions=True,
            )

            # v253.4: Unregister agent status callback BEFORE stopping bridge/mesh.
            # The callback holds refs to bridge_ref and registry — must be removed
            # before those objects are torn down to prevent use-after-free and
            # callback accumulation across warm restarts.
            if self._agent_status_callback and self._agent_status_registry:
                try:
                    if hasattr(self._agent_status_registry, 'remove_status_change'):
                        self._agent_status_registry.remove_status_change(
                            self._agent_status_callback,
                        )
                except Exception as e:
                    logger.debug("Agent status callback cleanup failed: %s", e)
                self._agent_status_callback = None
                self._agent_status_registry = None

            # v237.0: Stop JARVIS Bridge (stops adapter agents, cancels startup tasks).
            if self._jarvis_bridge:
                await _run_stop_step(
                    "JARVIS Bridge",
                    self._jarvis_bridge.stop,
                    timeout=bridge_stop_timeout,
                )

            # Stop Neural Mesh (stops production agents, bus, registry, orchestrator).
            # Note: bridge.stop() above already calls coordinator.stop() internally.
            # This second call is a defensive no-op when already stopped.
            if self._neural_mesh:
                await _run_stop_step(
                    "Neural Mesh",
                    self._neural_mesh.stop,
                    timeout=mesh_stop_timeout,
                )
        finally:
            self._hybrid_orchestrator = None
            self._jarvis_bridge = None
            self._neural_mesh = None
            self._event_bridge = None
            self._screen_analyzer = None
            self._ghost_hands = None
            self._ghost_display = None
            # v253.4: Clear callback refs (defensive — should be None already)
            self._agent_status_callback = None
            self._agent_status_registry = None
            self._approval_callback = None
            self._state = AGIOSState.OFFLINE
            logger.info("AGI OS stopped")

    def pause(self) -> None:
        """Pause autonomous operation (still responds to direct commands)."""
        if self._state == AGIOSState.ONLINE:
            self._state = AGIOSState.PAUSED
            if self._action_orchestrator:
                self._action_orchestrator.pause()
            logger.info("AGI OS paused")

    def resume(self) -> None:
        """Resume autonomous operation."""
        if self._state == AGIOSState.PAUSED:
            self._state = AGIOSState.ONLINE
            if self._action_orchestrator:
                self._action_orchestrator.resume()
            logger.info("AGI OS resumed")

    async def _report_init_progress(self, component: str, detail: str) -> None:
        """v250.1: Report intra-phase progress during _init_agi_os_components().

        Each sub-component init takes 5-20s. Without reporting after each one,
        the entire 90+ second _init_agi_os_components() looks like a single
        silent block to the DMS watchdog, which triggers stall → rollback.
        """
        cb = getattr(self, '_progress_callback', None)
        if cb:
            try:
                callback_timeout = max(
                    0.1,
                    float(getattr(self, "_progress_callback_timeout", 0.75)),
                )
                await asyncio.wait_for(
                    cb(f"init_{component}", detail),
                    timeout=callback_timeout,
                )
            except asyncio.TimeoutError:
                logger.debug(
                    "Init progress callback timed out after %.2fs (%s)",
                    callback_timeout,
                    component,
                )
            except Exception as e:
                logger.debug("Init progress callback error (non-fatal): %s", e)

    async def _run_timed_init_step(
        self,
        step_name: str,
        operation: Callable[[], Awaitable[Any]],
        timeout_seconds: float,
    ) -> Any:
        """
        Execute an init step with periodic progress heartbeats and hard timeout.

        This prevents silent >60s gaps that trigger supervisor stall detection
        while still enforcing deterministic upper bounds per subsystem.
        """
        heartbeat_seconds = max(1.0, _env_float("JARVIS_AGI_OS_STEP_HEARTBEAT", 10.0))
        cancel_grace_seconds = max(
            0.5, _env_float("JARVIS_AGI_OS_CANCEL_GRACE_TIMEOUT", 5.0)
        )
        started = time.monotonic()
        task = asyncio.create_task(operation(), name=f"agi_os_init_{step_name}")

        # v253.3: Store strong reference to prevent GC if this method
        # raises (TimeoutError, CancelledError) while the task is still
        # running. Without this, "Task was destroyed but it is pending!"
        # warnings appear in logs during shutdown or tight-budget timeouts.
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

        async def _cancel_with_grace() -> None:
            if task.done():
                return
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=cancel_grace_seconds)
            except asyncio.CancelledError:
                return
            except asyncio.TimeoutError:
                logger.warning(
                    "Init step %s did not cancel within %.1fs",
                    step_name,
                    cancel_grace_seconds,
                )
            except Exception:
                return

        try:
            while True:
                elapsed = time.monotonic() - started
                remaining = timeout_seconds - elapsed
                if remaining <= 0:
                    await _cancel_with_grace()
                    raise asyncio.TimeoutError(
                        f"{step_name} initialization timed out after {timeout_seconds:.1f}s"
                    )

                wait_slice = min(heartbeat_seconds, remaining)
                done, _ = await asyncio.wait(
                    {task},
                    timeout=wait_slice,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if task in done:
                    return task.result()

                await self._report_init_progress(
                    step_name,
                    f"{step_name} initializing ({elapsed + wait_slice:.0f}s elapsed)",
                )
        except asyncio.CancelledError:
            await _cancel_with_grace()
            raise
        except Exception:
            await _cancel_with_grace()
            raise

    async def _init_agi_os_components(self) -> None:
        """Initialize core AGI OS components.

        v253.3: Parallel initialization with budget-aware timeouts.

        Previously (v251.2): Components were initialized sequentially, each
        with a fixed 15s timeout. When budget scaling reduced the phase from
        45s to ~15.5s, 8 sequential components couldn't fit — even 2s each
        would total 16s. A single slow component (e.g. Ghost Hands or
        NotificationMonitor) made the math impossible.

        Now: All 8 components run in parallel via asyncio.gather(). The
        per-component timeout is min(configured_timeout, phase_budget - margin).
        Since components run concurrently, total time = max(component times)
        instead of sum(component times). A 15.5s budget easily fits when the
        slowest individual component takes <15s.

        Each component's failure is isolated — one hanging getter doesn't
        block the others. Progress is reported as each component finishes.
        """
        _comp_timeout_base = _env_float("JARVIS_AGI_OS_COMPONENT_TIMEOUT", 15.0)

        # v253.3: Read the scaled phase budget. When the supervisor passes a
        # tight startup_budget_seconds, the phase might be scaled from 45s
        # to as low as 15s. Per-component timeout must fit within this.
        _phase_budget = getattr(self, '_phase_budgets', {}).get(
            "agi_os_components", 45.0
        )
        # Leave 2s margin for gather() overhead and progress reporting
        _comp_timeout = min(_comp_timeout_base, max(3.0, _phase_budget - 2.0))
        logger.info(
            "Component init: phase_budget=%.1fs, per_component_timeout=%.1fs (parallel)",
            _phase_budget, _comp_timeout,
        )

        async def _init_one(
            name: str,
            label: str,
            factory,
            *,
            is_async: bool = True,
        ):
            """Initialize a single component with timeout and error isolation."""
            try:
                if is_async:
                    result = await asyncio.wait_for(
                        factory(), timeout=_comp_timeout,
                    )
                else:
                    result = factory()
                self._component_status[name] = ComponentStatus(
                    name=name, available=True,
                )
                logger.info("%s initialized", label)
                await self._report_init_progress(name, f"{label} done")
                return (name, result)
            except asyncio.TimeoutError:
                logger.warning(
                    "%s timed out after %.1fs", label, _comp_timeout,
                )
                self._component_status[name] = ComponentStatus(
                    name=name,
                    available=False,
                    healthy=False,
                    error=f"timeout ({_comp_timeout:.1f}s)",
                )
                await self._report_init_progress(name, f"{label} timed out")
                return (name, None)
            except Exception as e:
                logger.warning("%s failed: %s", label, e)
                self._component_status[name] = ComponentStatus(
                    name=name,
                    available=False,
                    healthy=False,
                    error=str(e),
                )
                await self._report_init_progress(name, f"{label} failed")
                return (name, None)

        # ── Build list of component init coroutines ──
        coros = []

        if self._config['enable_voice']:
            coros.append(_init_one("voice", "Voice communicator", get_voice_communicator))

        coros.append(_init_one("approval", "Approval manager", get_approval_manager))
        coros.append(_init_one("events", "Event stream", get_event_stream))

        if self._config['enable_autonomous_actions']:
            coros.append(_init_one(
                "orchestrator", "Action orchestrator", start_action_orchestrator,
            ))

        # Lazy-import components (import inside factory lambda)
        async def _get_notification_monitor():
            from macos_helper.notification_monitor import get_notification_monitor
            return await get_notification_monitor(auto_start=True)

        async def _get_system_event_monitor():
            from macos_helper.system_event_monitor import get_system_event_monitor
            return await get_system_event_monitor(auto_start=True)

        async def _get_ghost_hands():
            from ghost_hands.orchestrator import get_ghost_hands
            return await get_ghost_hands()

        coros.append(_init_one(
            "notification_monitor", "NotificationMonitor", _get_notification_monitor,
        ))
        coros.append(_init_one(
            "system_event_monitor", "SystemEventMonitor", _get_system_event_monitor,
        ))
        coros.append(_init_one(
            "ghost_hands", "Ghost Hands", _get_ghost_hands,
        ))

        # Ghost Display is synchronous — wrap in _init_one with is_async=False
        def _get_ghost_display():
            from vision.yabai_space_detector import get_ghost_manager
            return get_ghost_manager()

        coros.append(_init_one(
            "ghost_display", "Ghost Display", _get_ghost_display, is_async=False,
        ))

        # ── Run ALL components in parallel ──
        results = await asyncio.gather(*coros, return_exceptions=True)

        # ── Map results to instance variables ──
        result_map: Dict[str, Any] = {}
        for r in results:
            if isinstance(r, BaseException):
                logger.warning("Component init raised unexpected error: %s", r)
            elif r is not None:
                name, value = r
                result_map[name] = value

        self._voice = result_map.get("voice")
        self._approval_manager = result_map.get("approval")
        self._event_stream = result_map.get("events")
        self._action_orchestrator = result_map.get("orchestrator")
        self._notification_monitor = result_map.get("notification_monitor")
        self._system_event_monitor = result_map.get("system_event_monitor")
        self._ghost_hands = result_map.get("ghost_hands")
        self._ghost_display = result_map.get("ghost_display")

        succeeded = sum(1 for v in result_map.values() if v is not None)
        total = len(coros)
        logger.info(
            "Component init complete: %d/%d succeeded (parallel, %.1fs budget)",
            succeeded, total, _phase_budget,
        )

    async def _init_intelligence_systems(self) -> None:
        """Initialize intelligence systems (UAE, SAI, CAI, voice biometrics).

        v251.1: Time-budget pattern for the 3 timed steps (learning_db,
        speaker_verification, owner_identity).  Previously each had independent
        timeouts (60+90+45=195s) which could exceed the supervisor's outer
        budget on their own.  UAE/SAI/CAI are synchronous imports (<1s).

        Budget allocation for timed steps:
          learning_db:           20%  (DB connection, usually fast)
          speaker_verification:  50%  (heaviest — loads ECAPA-TDNN model)
          owner_identity:        30%  (depends on speaker_verification)
        """
        # Total budget for intelligence timed steps.
        # Default 45s — typical is 15-30s. The 3 sync imports (UAE/SAI/CAI)
        # run first and don't count against this budget.
        # v253.3: Cap intel_budget to the scaled phase timeout. When budget
        # scaling reduces intelligence_systems from 60s to ~20s, the internal
        # budget must match — otherwise _intel_remaining() returns time that
        # the outer phase timeout will cancel before it's used.
        _intel_env = _env_float("JARVIS_AGI_OS_INTEL_BUDGET", 45.0)
        _intel_phase = getattr(self, '_phase_budgets', {}).get(
            "intelligence_systems", 60.0,
        )
        intel_budget = min(_intel_env, max(5.0, _intel_phase - 3.0))
        intel_start = time.monotonic()

        def _intel_remaining() -> float:
            return max(1.0, intel_budget - (time.monotonic() - intel_start))

        # UAE (Unified Awareness Engine) — sync import, no timeout needed
        try:
            from core.hybrid_orchestrator import _get_uae
            self._uae_engine = _get_uae()
            if self._uae_engine:
                self._component_status['uae'] = ComponentStatus(
                    name='uae',
                    available=True
                )
                logger.info("UAE loaded")
        except Exception as e:
            logger.warning("UAE not available: %s", e)
            self._component_status['uae'] = ComponentStatus(
                name='uae',
                available=False,
                error=str(e)
            )
        await self._report_init_progress("uae", "UAE initialization complete")

        # SAI (Self-Aware Intelligence) — sync import, no timeout needed
        try:
            from core.hybrid_orchestrator import _get_sai, get_sai_loader_status
            self._sai_system = _get_sai()
            if self._sai_system:
                self._component_status['sai'] = ComponentStatus(
                    name='sai',
                    available=True
                )
                logger.info("SAI loaded")
            else:
                sai_status = get_sai_loader_status()
                error = sai_status.get("error") or "SAI loader returned no instance"
                self._component_status['sai'] = ComponentStatus(
                    name='sai',
                    available=False,
                    error=str(error),
                )
                logger.warning("SAI unavailable: %s", error)
        except Exception as e:
            logger.warning("SAI not available: %s", e)
            self._component_status['sai'] = ComponentStatus(
                name='sai',
                available=False,
                error=str(e)
            )
        await self._report_init_progress("sai", "SAI initialization complete")

        # CAI (Context Awareness Intelligence) — sync import, no timeout needed
        try:
            from core.hybrid_orchestrator import _get_cai, get_cai_loader_status
            self._cai_system = _get_cai()
            if self._cai_system:
                self._component_status['cai'] = ComponentStatus(
                    name='cai',
                    available=True
                )
                logger.info("CAI loaded")
            else:
                cai_status = get_cai_loader_status()
                error = cai_status.get("error") or "CAI loader returned no instance"
                self._component_status['cai'] = ComponentStatus(
                    name='cai',
                    available=False,
                    error=str(error),
                )
                logger.warning("CAI unavailable: %s", error)
        except Exception as e:
            logger.warning("CAI not available: %s", e)
            self._component_status['cai'] = ComponentStatus(
                name='cai',
                available=False,
                error=str(e)
            )
        await self._report_init_progress("cai", "CAI initialization complete")

        # Learning Database (20% of budget)
        try:
            from core.hybrid_orchestrator import _get_learning_db
            learning_db_timeout = min(
                _env_float("JARVIS_AGI_OS_LEARNING_DB_TIMEOUT", intel_budget * 0.2),
                _intel_remaining(),
            )
            self._learning_db = await self._run_timed_init_step(
                "learning_db",
                _get_learning_db,
                timeout_seconds=learning_db_timeout,
            )
            if self._learning_db:
                self._component_status['learning_db'] = ComponentStatus(
                    name='learning_db',
                    available=True
                )
                logger.info("Learning database loaded")
        except Exception as e:
            logger.warning("Learning database not available: %s", e)
            self._component_status['learning_db'] = ComponentStatus(
                name='learning_db',
                available=False,
                error=str(e)
            )
        await self._report_init_progress("learning_db", "Learning DB initialization complete")

        # Speaker Verification Service (50% of budget — heaviest, loads ECAPA-TDNN)
        # v236.1: Use singleton to avoid duplicate instances (double memory,
        # competing encoder loads, enrollment updates not shared).
        try:
            from voice.speaker_verification_service import get_speaker_verification_service
            speaker_timeout = min(
                _env_float("JARVIS_AGI_OS_SPEAKER_TIMEOUT", intel_budget * 0.5),
                _intel_remaining(),
            )

            async def _load_speaker_verification() -> Any:
                return await get_speaker_verification_service(
                    learning_db=self._learning_db
                )

            self._speaker_verification = await self._run_timed_init_step(
                "speaker_verification",
                _load_speaker_verification,
                timeout_seconds=speaker_timeout,
            )
            self._component_status['speaker_verification'] = ComponentStatus(
                name='speaker_verification',
                available=True
            )
            logger.info("Speaker verification service loaded")
        except Exception as e:
            logger.warning("Speaker verification not available: %s", e)
            self._component_status['speaker_verification'] = ComponentStatus(
                name='speaker_verification',
                available=False,
                error=str(e)
            )
        await self._report_init_progress(
            "speaker_verification",
            "Speaker verification initialization complete",
        )

        # Owner Identity Service (30% of budget)
        try:
            owner_identity_timeout = min(
                _env_float("JARVIS_AGI_OS_OWNER_ID_TIMEOUT", intel_budget * 0.3),
                _intel_remaining(),
            )

            async def _load_owner_identity() -> tuple[Any, Any]:
                service = await get_owner_identity(
                    speaker_verification=self._speaker_verification,
                    learning_db=self._learning_db
                )
                profile = await service.get_current_owner()
                return service, profile

            self._owner_identity, owner_profile = await self._run_timed_init_step(
                "owner_identity",
                _load_owner_identity,
                timeout_seconds=owner_identity_timeout,
            )
            self._component_status['owner_identity'] = ComponentStatus(
                name='owner_identity',
                available=True
            )
            owner_name = getattr(owner_profile, "name", "unknown")
            owner_confidence = getattr(
                getattr(owner_profile, "identity_confidence", None),
                "value",
                "unknown",
            )
            logger.info(
                "Owner identity service loaded - Owner: %s (confidence: %s)",
                owner_name,
                owner_confidence,
            )
        except Exception as e:
            logger.warning("Owner identity service not available: %s", e)
            self._component_status['owner_identity'] = ComponentStatus(
                name='owner_identity',
                available=False,
                error=str(e)
            )
        await self._report_init_progress("owner_identity", "Owner identity initialization complete")

        elapsed = time.monotonic() - intel_start
        logger.info("Intelligence systems initialized in %.1fs (budget: %.0fs)", elapsed, intel_budget)

    async def _init_neural_mesh(self) -> None:
        """Initialize Neural Mesh coordinator and production agents.

        v251.1: Time-budget pattern — a single total budget is split across
        the 3 sequential steps (coordinator, agents, bridge) so the sum of
        inner timeouts never exceeds the outer timeout in the supervisor's
        asyncio.wait_for().  Previously each step had independent 90-120s
        defaults, summing to ~300s — far exceeding the supervisor's 90s
        outer budget, guaranteeing a TimeoutError and agi_os:EROR status.

        Budget allocation (proportional):
          coordinator: 40%   (critical — must complete for agents/bridge)
          agents:      30%   (important but non-fatal)
          bridge:      30%   (important but non-fatal)
        """
        try:
            # Total budget for all neural mesh init steps.
            # Default 75s fits comfortably within the supervisor's 90s outer timeout
            # (JARVIS_AGI_OS_INIT_TIMEOUT default = 60+30 = 90s).
            # v253.3: Cap to scaled phase timeout. When budget scaling reduces
            # neural_mesh phase from 90s to ~30s, internal budget must match.
            _nm_env = _env_float("JARVIS_AGI_OS_NEURAL_MESH_BUDGET", 75.0)
            _nm_phase = getattr(self, '_phase_budgets', {}).get(
                "neural_mesh", 90.0,
            )
            total_budget = min(_nm_env, max(10.0, _nm_phase - 3.0))
            budget_start = time.monotonic()

            def _remaining() -> float:
                """Remaining time in the budget."""
                return max(1.0, total_budget - (time.monotonic() - budget_start))

            # Step 1: Start coordinator (40% of budget)
            from neural_mesh import start_neural_mesh
            mesh_timeout = min(
                _env_float("JARVIS_AGI_OS_NEURAL_MESH_TIMEOUT", total_budget * 0.4),
                _remaining(),
            )
            self._neural_mesh = await self._run_timed_init_step(
                "neural_mesh",
                start_neural_mesh,
                timeout_seconds=mesh_timeout,
            )

            # Step 2: Register production agents (30% of budget, non-fatal)
            n_production = 0
            try:
                from neural_mesh.agents.agent_initializer import initialize_production_agents
                agent_timeout = min(
                    _env_float("JARVIS_AGI_OS_NEURAL_AGENT_TIMEOUT", total_budget * 0.3),
                    _remaining(),
                )

                async def _init_production_agents() -> Any:
                    return await initialize_production_agents(self._neural_mesh)

                registered = await self._run_timed_init_step(
                    "neural_mesh_agents",
                    _init_production_agents,
                    timeout_seconds=agent_timeout,
                )
                n_production = len(registered)
            except Exception as agent_exc:
                logger.warning("Production agent initialization failed (mesh still running): %s", agent_exc)

            # Step 3: Wire system adapters (ALL remaining budget, non-fatal)
            # v253.0: Changed from fixed 30% to ALL remaining budget.
            # The bridge is the last step — giving it a fixed percentage
            # meant overruns in Steps 1-2 could starve it. Now it gets
            # everything that's left (typically 40-50s after Steps 1-2).
            n_adapters = 0
            try:
                from neural_mesh import start_jarvis_neural_mesh
                bridge_timeout = min(
                    _env_float("JARVIS_AGI_OS_NEURAL_BRIDGE_TIMEOUT", _remaining()),
                    _remaining(),
                )
                self._jarvis_bridge = await self._run_timed_init_step(
                    "neural_mesh_bridge",
                    start_jarvis_neural_mesh,
                    timeout_seconds=bridge_timeout,
                )
                n_adapters = len(self._jarvis_bridge.registered_agents) if hasattr(self._jarvis_bridge, 'registered_agents') else 0
            except Exception as bridge_exc:
                logger.warning("JARVIS Neural Mesh Bridge failed (mesh still running): %s", bridge_exc)

            total = len(self._neural_mesh.get_all_agents()) if hasattr(self._neural_mesh, 'get_all_agents') else n_production + n_adapters
            elapsed = time.monotonic() - budget_start
            logger.info(
                "Neural Mesh started: %d agents total (%d production, %d adapters) in %.1fs (budget: %.0fs)",
                total, n_production, n_adapters, elapsed, total_budget,
            )

            self._component_status['neural_mesh'] = ComponentStatus(
                name='neural_mesh',
                available=True
            )
        except Exception as e:
            logger.warning("Neural Mesh not available: %s", e)
            self._component_status['neural_mesh'] = ComponentStatus(
                name='neural_mesh',
                available=False,
                error=str(e)
            )

    async def _init_hybrid_orchestrator(self) -> None:
        """Initialize Hybrid Orchestrator."""
        try:
            from core.hybrid_orchestrator import HybridOrchestrator
            self._hybrid_orchestrator = HybridOrchestrator()
            self._component_status['hybrid'] = ComponentStatus(
                name='hybrid',
                available=True
            )
            logger.info("Hybrid Orchestrator loaded")
        except Exception as e:
            logger.warning("Hybrid Orchestrator not available: %s", e)
            self._component_status['hybrid'] = ComponentStatus(
                name='hybrid',
                available=False,
                error=str(e)
            )

    async def _init_screen_analyzer(self) -> None:
        """Initialize screen analyzer for proactive monitoring.

        v237.3: Fully wired. Uses ClaudeVisionAnalyzer as the vision handler
        (provides both capture_screen() and describe_screen(params) that
        MemoryAwareScreenAnalyzer requires). Connected to EventStream via
        ScreenAnalyzerBridge for proactive detection.
        """
        if not self._config['enable_proactive_monitoring']:
            return

        try:
            from vision.claude_vision_analyzer_main import (
                ClaudeVisionAnalyzer,
                VisionConfig,
            )
            from vision.continuous_screen_analyzer import MemoryAwareScreenAnalyzer
            from .jarvis_integration import connect_screen_analyzer

            # ClaudeVisionAnalyzer provides both capture_screen() and
            # describe_screen(params) — the exact interface MemoryAwareScreenAnalyzer needs
            api_key = os.environ.get('ANTHROPIC_API_KEY', '')
            if not api_key:
                logger.warning("Screen analyzer disabled: ANTHROPIC_API_KEY not set")
                self._component_status['screen_analyzer'] = ComponentStatus(
                    name='screen_analyzer',
                    available=False,
                    error='ANTHROPIC_API_KEY not set'
                )
                return

            await self._report_init_progress("screen_analyzer", "Screen analyzer preflight")

            # Startup pressure guard: avoid initializing heavy continuous vision
            # when system resources are already under stress.
            degraded_mode = False
            try:
                import psutil

                vm = psutil.virtual_memory()
                process_mb = psutil.Process().memory_info().rss / (1024 * 1024)
                available_mb = vm.available / (1024 * 1024)
                cpu_percent = psutil.cpu_percent(interval=0.0)

                min_available_mb = _env_float(
                    "JARVIS_AGI_OS_SCREEN_GUARD_MIN_AVAILABLE_MB", 2200.0
                )
                critical_available_mb = _env_float(
                    "JARVIS_AGI_OS_SCREEN_GUARD_CRITICAL_AVAILABLE_MB", 1400.0
                )
                max_process_mb = _env_float(
                    "JARVIS_AGI_OS_SCREEN_GUARD_MAX_PROCESS_MB", 1600.0
                )
                max_cpu_percent = _env_float(
                    "JARVIS_AGI_OS_SCREEN_GUARD_MAX_CPU_PERCENT", 92.0
                )

                if available_mb <= critical_available_mb:
                    logger.warning(
                        "Screen analyzer deferred due to critical startup pressure "
                        "(available=%.0fMB <= %.0fMB)",
                        available_mb,
                        critical_available_mb,
                    )
                    self._component_status['screen_analyzer'] = ComponentStatus(
                        name='screen_analyzer',
                        available=False,
                        error=(
                            "Deferred: critical startup memory pressure "
                            f"({available_mb:.0f}MB available)"
                        ),
                    )
                    return

                degraded_mode = (
                    available_mb < min_available_mb
                    or process_mb > max_process_mb
                    or cpu_percent > max_cpu_percent
                )
                if degraded_mode:
                    logger.warning(
                        "Screen analyzer startup in degraded mode "
                        "(available=%.0fMB, process=%.0fMB, cpu=%.1f%%)",
                        available_mb,
                        process_mb,
                        cpu_percent,
                    )
            except Exception as pressure_err:
                logger.debug("Screen analyzer pressure preflight unavailable: %s", pressure_err)

            await self._report_init_progress("screen_analyzer", "Building vision handler")

            # v253.3: Sub-timeouts must respect the scaled phase budget.
            # Default sub-totals (20+12+15+15=62s) exceed the phase budget
            # when scaling reduces screen_analyzer from 45s to ~10s.
            # Use time-budget pattern with proportional allocation.
            _sa_phase = getattr(self, '_phase_budgets', {}).get(
                "screen_analyzer", 45.0,
            )
            _sa_budget = max(5.0, _sa_phase - 3.0)  # 3s margin for overhead
            _sa_start = time.monotonic()

            def _sa_remaining() -> float:
                return max(1.0, _sa_budget - (time.monotonic() - _sa_start))

            # Proportional allocation: construct 35%, capture 20%, monitor 25%, bridge 20%
            construct_timeout = min(
                _env_float("JARVIS_AGI_OS_SCREEN_CONSTRUCT_TIMEOUT", 20.0),
                max(3.0, _sa_budget * 0.35),
            )

            # v253.6: Run constructor directly on event loop — NOT in executor.
            # ClaudeVisionAnalyzer.__init__() creates asyncio.Semaphore() which
            # calls asyncio.get_event_loop() internally (Python 3.9). Running in
            # a ThreadPoolExecutor thread causes "no current event loop in thread
            # 'asyncio_2'" RuntimeError. The constructor is pure object setup with
            # no blocking I/O, so threading is unnecessary.
            if degraded_mode:
                degraded_cfg = VisionConfig(
                    enable_video_streaming=False,
                    prefer_video_over_screenshots=False,
                    max_concurrent_requests=int(
                        _env_float(
                            "JARVIS_AGI_OS_SCREEN_DEGRADED_MAX_CONCURRENCY", 2.0
                        )
                    ),
                )
                vision_handler = ClaudeVisionAnalyzer(api_key=api_key, config=degraded_cfg)
            else:
                vision_handler = ClaudeVisionAnalyzer(api_key=api_key)

            # v237.3: Permission pre-check — test capture before starting monitoring.
            # macOS requires Screen Recording permission for Quartz/CGDisplay capture.
            # Without it, capture returns None or unusably small images.
            try:
                await self._report_init_progress("screen_analyzer", "Capture probe")
                capture_probe_timeout = min(
                    _env_float("JARVIS_AGI_OS_SCREEN_CAPTURE_PROBE_TIMEOUT", 12.0),
                    max(2.0, _sa_remaining() * 0.4),  # v253.3: budget-aware
                )
                test_capture = await asyncio.wait_for(
                    vision_handler.capture_screen(),
                    timeout=capture_probe_timeout,
                )
                capture_ok = test_capture is not None
                # Check for permission-denied indicators (1x1 or tiny images)
                if capture_ok and hasattr(test_capture, 'size'):
                    w, h = test_capture.size
                    if w < 100 or h < 100:
                        capture_ok = False
                if capture_ok and hasattr(test_capture, 'shape'):
                    if test_capture.shape[0] < 100 or test_capture.shape[1] < 100:
                        capture_ok = False
            except Exception:
                capture_ok = False

            if not capture_ok:
                logger.warning(
                    "Screen capture permission not granted. "
                    "Grant Screen Recording permission in System Settings > "
                    "Privacy & Security > Screen Recording, then restart JARVIS."
                )
                self._component_status['screen_analyzer'] = ComponentStatus(
                    name='screen_analyzer',
                    available=False,
                    error='Screen Recording permission not granted'
                )
                return

            await self._report_init_progress("screen_analyzer", "Starting monitor loop")
            self._screen_analyzer = MemoryAwareScreenAnalyzer(vision_handler)
            monitor_start_timeout = min(
                _env_float("JARVIS_AGI_OS_SCREEN_MONITOR_START_TIMEOUT", 15.0),
                max(2.0, _sa_remaining() * 0.55),  # v253.3: budget-aware
            )
            await asyncio.wait_for(
                self._screen_analyzer.start_monitoring(),
                timeout=monitor_start_timeout,
            )

            # Bridge to event stream for proactive detection
            await self._report_init_progress("screen_analyzer", "Connecting AGI bridge")
            bridge_timeout = min(
                _env_float("JARVIS_AGI_OS_SCREEN_BRIDGE_TIMEOUT", 15.0),
                max(2.0, _sa_remaining()),  # v253.3: all remaining budget
            )
            await asyncio.wait_for(
                connect_screen_analyzer(
                    self._screen_analyzer,
                    enable_claude_vision=True,
                ),
                timeout=bridge_timeout,
            )

            self._component_status['screen_analyzer'] = ComponentStatus(
                name='screen_analyzer',
                available=True,
                healthy=True
            )
            logger.info("Screen analyzer initialized and monitoring")
        except Exception as e:
            logger.warning("Screen analyzer not available: %s", e)
            self._component_status['screen_analyzer'] = ComponentStatus(
                name='screen_analyzer',
                available=False,
                error=str(e)
            )

    async def _connect_components(self) -> None:
        """Connect components together."""

        # Connect approval callbacks
        if self._approval_manager and self._event_stream:
            def on_approval(request, response):
                # v251.2: Store task ref to prevent GC + log errors
                task = asyncio.create_task(
                    self._event_stream.emit(AGIEvent(
                        event_type=EventType.USER_APPROVED if response.approved else EventType.USER_DENIED,
                        source="approval_manager",
                        data={'request_id': request.request_id},
                        correlation_id=request.request_id,
                    )),
                    name="approval_event_emit",
                )
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)

            self._approval_manager.on_approval(on_approval)
            # v253.4: Store reference for cleanup on stop()
            self._approval_callback = on_approval

        # v237.2: Wire Neural Mesh ↔ ProactiveEventStream bidirectional bridge
        if self._neural_mesh and self._event_stream:
            try:
                from .jarvis_integration import connect_neural_mesh
                self._event_bridge = await connect_neural_mesh(self._neural_mesh)
                logger.info("Neural Mesh ↔ Event Stream bridge connected")
            except Exception as e:
                logger.warning("Event bridge not available: %s", e)

        # v237.4: Initialize Ghost Display with YabaiSpaceDetector
        if self._ghost_display:
            try:
                from vision.yabai_space_detector import get_yabai_detector
                yabai_detector = get_yabai_detector(auto_start=False)
                if yabai_detector:
                    await self._ghost_display.initialize(yabai_detector)
                    logger.info("Ghost Display Manager initialized with YabaiSpaceDetector")
            except Exception as e:
                logger.warning("Ghost Display initialization failed: %s", e)

        # v237.4: Inject UAE into action orchestrator for enriched decisions
        if self._action_orchestrator and self._uae_engine:
            self._action_orchestrator._uae_engine = self._uae_engine
            logger.info("UAE → Action Orchestrator connected")

        # v239.0: Register Neural Mesh agent capabilities as tools in ToolRegistry
        # This bridges the mesh (60+ agents) into the autonomy system so the agent
        # runtime's THINK step can discover capabilities and ACT step can invoke them.
        if self._jarvis_bridge:
            try:
                from autonomy.langchain_tools import (
                    NeuralMeshAgentTool,
                    ToolRegistry,
                )

                registry = ToolRegistry.get_instance()
                default_timeout = float(os.getenv("MESH_TOOL_TIMEOUT", "30"))
                registered = 0

                for agent_name in self._jarvis_bridge.registered_agents:
                    agent = self._jarvis_bridge.get_agent(agent_name)
                    if agent is None:
                        continue
                    for capability in agent.capabilities:
                        try:
                            tool = NeuralMeshAgentTool(
                                agent=agent,
                                capability=capability,
                                timeout_seconds=default_timeout,
                            )
                            registry.register(tool, replace=True)
                            registered += 1
                        except Exception:
                            pass

                logger.info(
                    "Neural Mesh → ToolRegistry: %d capabilities registered from %d agents",
                    registered,
                    len(self._jarvis_bridge.registered_agents),
                )

                if registered == 0:
                    logger.warning(
                        "Neural Mesh → ToolRegistry: 0 capabilities registered — "
                        "agents may have empty capability sets"
                    )

                # v239.0: Health-aware tool lifecycle — deregister on agent death,
                # re-register on agent recovery. Uses AgentRegistry status callbacks.
                coordinator = getattr(self._jarvis_bridge, '_coordinator', None)
                agent_registry = getattr(coordinator, '_registry', None) if coordinator else None
                if agent_registry and hasattr(agent_registry, 'on_status_change'):
                    bridge_ref = self._jarvis_bridge
                    timeout_ref = default_timeout

                    def _on_agent_status_change(agent_info, old_status):
                        """Sync/deregister mesh tools based on agent health."""
                        from neural_mesh.data_models import AgentStatus as AS
                        name = agent_info.agent_name
                        new_status = agent_info.status

                        # Agent went offline/error → remove its tools
                        if new_status in (AS.OFFLINE, AS.ERROR, AS.SHUTTING_DOWN):
                            agent = bridge_ref.get_agent(name)
                            if agent:
                                for cap in agent.capabilities:
                                    tool_name = f"mesh:{name}:{cap}"
                                    registry.unregister(tool_name)
                                logger.info(
                                    "[MeshToolLifecycle] Agent %s → %s: %d tools deregistered",
                                    name, new_status.value, len(agent.capabilities),
                                )

                        # Agent recovered → re-register its tools
                        elif (
                            new_status in (AS.ONLINE, AS.BUSY)
                            and old_status in (AS.OFFLINE, AS.ERROR, AS.INITIALIZING)
                        ):
                            agent = bridge_ref.get_agent(name)
                            if agent:
                                re_registered = 0
                                for cap in agent.capabilities:
                                    try:
                                        tool = NeuralMeshAgentTool(
                                            agent=agent,
                                            capability=cap,
                                            timeout_seconds=timeout_ref,
                                        )
                                        registry.register(tool, replace=True)
                                        re_registered += 1
                                    except Exception:
                                        pass
                                logger.info(
                                    "[MeshToolLifecycle] Agent %s recovered: %d tools re-registered",
                                    name, re_registered,
                                )

                    agent_registry.on_status_change(_on_agent_status_change)
                    # v253.4: Store references for cleanup on stop()
                    self._agent_status_callback = _on_agent_status_change
                    self._agent_status_registry = agent_registry
                    logger.info("[MeshToolLifecycle] Health-aware tool lifecycle active")

            except Exception as e:
                logger.warning("Mesh tool registration failed (non-fatal): %s", e)

        logger.debug("Components connected")

    async def _announce_startup(self) -> None:
        """Announce AGI OS startup via voice with dynamic owner identification.

        v251.2: Wrapped in a hard timeout to prevent voice TTS hangs from
        stalling the entire startup sequence.  Default: 15s.
        """
        announce_timeout = _env_float("JARVIS_AGI_OS_ANNOUNCE_TIMEOUT", 15.0)
        try:
            await asyncio.wait_for(
                self._announce_startup_inner(), timeout=announce_timeout
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Startup announcement timed out after %.0fs (non-fatal)",
                announce_timeout,
            )
        except Exception as e:
            logger.warning("Startup announcement failed (non-fatal): %s", e)

    async def _announce_startup_inner(self) -> None:
        """Inner implementation of startup announcement (no timeout here)."""
        # Count available components
        available = sum(1 for s in self._component_status.values() if s.available)
        total = len(self._component_status)

        # Get owner name dynamically via voice biometrics
        owner_name = "sir"  # Fallback
        if self._owner_identity:
            try:
                owner_profile = await self._owner_identity.get_current_owner()
                if owner_profile and owner_profile.name:
                    owner_name = owner_profile.name
            except Exception as e:
                logger.warning("Could not get owner name: %s", e)

        import random
        if available == total:
            full_online_greetings = [
                f"Good day, {owner_name}. JARVIS is fully online. All systems operational.",
                f"JARVIS online, {owner_name}. All components ready and awaiting your command.",
                f"Hello, {owner_name}. JARVIS is ready to assist. All systems are go.",
                f"JARVIS fully operational, {owner_name}. How may I help you today?",
            ]
            greeting = random.choice(full_online_greetings)
        elif available > total // 2:
            degraded_greetings = [
                f"JARVIS is online, {owner_name}. {available} of {total} components available. Running at reduced capacity.",
                f"Hello, {owner_name}. JARVIS online with {available} of {total} systems ready.",
                f"JARVIS partially online, {owner_name}. Some systems are still initializing.",
            ]
            greeting = random.choice(degraded_greetings)
        else:
            limited_greetings = [
                f"JARVIS starting with limited functionality, {owner_name}. Several systems are unavailable.",
                f"Hello, {owner_name}. JARVIS is online but operating with reduced capabilities.",
            ]
            greeting = random.choice(limited_greetings)

        await self._voice.greet()
        await asyncio.sleep(0.5)
        await self._voice.speak(greeting, mode=VoiceMode.NORMAL)

    def _determine_health_state(self) -> AGIOSState:
        """Determine overall health state.

        v253.4: Fixed else branch — returned DEGRADED when core components
        were down, masking actual OFFLINE state. Correct mapping:
          all available + core OK  → ONLINE
          some missing  + core OK  → DEGRADED (can still serve commands)
          core components down     → OFFLINE  (cannot serve commands)
        """
        available = sum(1 for s in self._component_status.values() if s.available)
        total = len(self._component_status)

        # Core components that must be available for useful operation
        core_available = all(
            self._component_status.get(name, ComponentStatus(name=name, available=False)).available
            for name in ['voice', 'events', 'orchestrator']
        )

        if available == total and core_available:
            return AGIOSState.ONLINE
        elif core_available:
            return AGIOSState.DEGRADED
        else:
            return AGIOSState.OFFLINE

    async def _health_monitor_loop(self) -> None:
        """Background task for health monitoring."""
        while self._state in [AGIOSState.ONLINE, AGIOSState.DEGRADED, AGIOSState.PAUSED]:
            try:
                await asyncio.sleep(self._config['health_check_interval'])

                # Update uptime
                if self._started_at:
                    self._stats['uptime_seconds'] = (
                        datetime.now() - self._started_at
                    ).total_seconds()

                # Check component health
                await self._check_component_health()

                # Update state based on health
                new_state = self._determine_health_state()
                if new_state != self._state and self._state != AGIOSState.PAUSED:
                    logger.info("State changed: %s -> %s", self._state.value, new_state.value)
                    self._state = new_state

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception("Health monitor error: %s", e)

    async def _check_component_health(self) -> None:
        """Check health of all components.

        v253.4: Use _update_component_health() helper instead of direct dict access.
        When _run_phase() times out, it kills the init function before the function
        can register its component_status entry → KeyError. The component instance
        variable (e.g. self._neural_mesh) may already be set from an earlier init
        step, so the `if self._xxx:` guard passes but the status key doesn't exist.
        """
        # Event stream
        if self._event_stream:
            stats = self._event_stream.get_stats()
            self._update_component_health('events', stats.get('running', False))

        # Voice
        if self._voice:
            status = self._voice.get_status()
            self._update_component_health('voice', status.get('running', False))

        # Orchestrator
        if self._action_orchestrator:
            stats = self._action_orchestrator.get_stats()
            self._update_component_health('orchestrator', stats.get('state') == 'running')

        # Neural Mesh
        if self._neural_mesh:
            try:
                health = await self._neural_mesh.health_check()
                self._update_component_health('neural_mesh', health.get('status') == 'healthy')
            except Exception:
                self._update_component_health('neural_mesh', False)

    def _update_component_health(self, name: str, healthy: bool) -> None:
        """Update a component's health status, auto-creating the entry if missing.

        v253.4: When _run_phase() times out, it kills the init function before it
        can register self._component_status[name]. The component instance variable
        may already be partially set, so the health check proceeds but the dict
        key is missing → KeyError. This helper ensures the entry exists.
        """
        status = self._component_status.get(name)
        if status is None:
            self._component_status[name] = ComponentStatus(
                name=name,
                available=True,
                healthy=healthy,
            )
        else:
            status.healthy = healthy

    # ============== Public API ==============

    async def speak(
        self,
        text: str,
        mode: VoiceMode = VoiceMode.NORMAL
    ) -> Optional[str]:
        """
        Speak a message using Daniel voice.

        Args:
            text: Text to speak
            mode: Voice mode

        Returns:
            Message ID or None if voice unavailable
        """
        if self._voice:
            self._stats['voice_messages'] += 1
            return await self._voice.speak(text, mode=mode)
        return None

    async def process_command(self, command: str, source: str = "chat") -> str:
        """Process a user command.

        v241.0: When AGI_OS_NL_GOAL_BRIDGE_ENABLED=true, also submits the
        command as an agent runtime goal for autonomous tracking.  The hybrid
        orchestrator still handles the immediate response (primary path).

        Args:
            command: User command text
            source: Origin of the command ("voice", "chat", "api")

        Returns:
            Response text
        """
        self._stats['commands_processed'] += 1

        # v241.0: Opportunistic goal submission (fire-and-forget)
        goal_id = None
        if _env_bool("AGI_OS_NL_GOAL_BRIDGE_ENABLED", False):
            try:
                goal_id = await self.submit_nl_command(
                    text=command, source=source, skip_inference=False,
                )
            except Exception as e:
                logger.debug("[AGI-OS] NL goal bridge failed (non-blocking): %s", e)

        # Use hybrid orchestrator for command processing (primary path)
        if self._hybrid_orchestrator:
            try:
                result = await self._hybrid_orchestrator.process(command)
                response = result.get('response', "Command processed.")
                if goal_id:
                    response += f"\n\n[Tracking as goal {goal_id}]"
                return response
            except Exception as e:
                logger.error("Command processing error: %s", e)
                return f"Error processing command: {e}"

        return "Command processing not available."

    # ─────────────────────────────────────────────────────────
    # v241.0: NL Command → Goal Bridge
    # ─────────────────────────────────────────────────────────

    def _resolve_agent_runtime(self):
        """Lazily resolve the agent runtime singleton.

        v241.0: Uses module-level get_agent_runtime(). Cached after
        first successful resolve.
        """
        if self._agent_runtime is not None:
            return self._agent_runtime
        try:
            from backend.autonomy.agent_runtime import get_agent_runtime
            runtime = get_agent_runtime()
            if runtime is not None:
                self._agent_runtime = runtime
            return runtime
        except ImportError:
            return None

    async def submit_nl_command(
        self,
        text: str,
        source: str = "voice",
        context: Optional[Dict[str, Any]] = None,
        skip_inference: bool = False,
    ) -> Optional[str]:
        """Submit a natural language command as an agent runtime goal.

        v241.0: NL-to-Goal bridge. Optionally uses GoalInferenceAgent
        from the neural mesh to classify intent and enrich the goal.

        Args:
            text: Natural language command text
            source: Origin ("voice", "chat", "api")
            context: Optional additional context
            skip_inference: If True, skip GoalInferenceAgent classification

        Returns:
            goal_id if successfully submitted, None if unavailable.
        """
        runtime = self._resolve_agent_runtime()
        if runtime is None:
            logger.debug("[AGI-OS] submit_nl_command: agent runtime not available")
            return None

        goal_description = text
        goal_context = dict(context or {})
        goal_context["nl_source"] = source
        goal_context["original_text"] = text
        needs_vision = False

        # Default priority — may be overridden by inference
        try:
            from backend.autonomy.agent_runtime_models import GoalPriority
            goal_priority = GoalPriority.NORMAL
        except ImportError:
            goal_priority = None  # submit_goal will use default

        # ── Optional: GoalInferenceAgent classification ──────
        if not skip_inference and self._jarvis_bridge:
            try:
                inference_result = await self._safe_infer_goal(text, goal_context)
                if inference_result and isinstance(inference_result, dict):
                    inferred_desc = inference_result.get("description")
                    if inferred_desc and isinstance(inferred_desc, str):
                        goal_description = inferred_desc

                    category = inference_result.get("category", "")
                    if goal_priority is not None and category:
                        if category in ("system", "productivity"):
                            goal_priority = GoalPriority.HIGH
                        elif category in ("automation",):
                            goal_priority = GoalPriority.BACKGROUND

                    goal_context["inferred_category"] = category
                    goal_context["inferred_confidence"] = inference_result.get(
                        "confidence", 0.0
                    )
                    goal_context["inferred_level"] = inference_result.get("level", "")

                    if category in ("system",):
                        needs_vision = True
            except Exception as e:
                logger.debug(
                    "[AGI-OS] GoalInferenceAgent classification failed "
                    "(proceeding with raw text): %s", e
                )

        # ── Vision keyword detection in raw text ─────────────
        if not needs_vision:
            vision_keywords = {"open", "click", "navigate", "show", "display", "look at", "screen"}
            text_lower = text.lower()
            needs_vision = any(kw in text_lower for kw in vision_keywords)

        # ── Submit to agent runtime ──────────────────────────
        try:
            submit_kwargs: Dict[str, Any] = {
                "description": goal_description,
                "source": source,
                "context": goal_context,
                "needs_vision": needs_vision,
            }
            if goal_priority is not None:
                submit_kwargs["priority"] = goal_priority

            goal_id = await runtime.submit_goal(**submit_kwargs)
            logger.info(
                "[AGI-OS] NL command submitted as goal %s: %s (source=%s)",
                goal_id, goal_description[:60], source,
            )
            return goal_id
        except Exception as e:
            logger.warning("[AGI-OS] Failed to submit NL command as goal: %s", e)
            return None

    async def _safe_infer_goal(
        self, text: str, context: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Query GoalInferenceAgent with timeout and error isolation.

        v241.0: Uses jarvis_bridge.get_agent() to access the agent
        directly.  Payload uses 'command' key per GoalInferenceAgent's
        _infer_goal() interface.
        """
        timeout = float(os.getenv("AGI_OS_NL_INFERENCE_TIMEOUT", "5.0"))
        try:
            agent = self._jarvis_bridge.get_agent("GoalInferenceAgent")
            if agent is None:
                return None
            result = await asyncio.wait_for(
                agent.execute_task({
                    "action": "infer_goal",
                    "command": text,
                    "context": context,
                }),
                timeout=timeout,
            )
            return result if isinstance(result, dict) else None
        except asyncio.TimeoutError:
            logger.debug("[AGI-OS] GoalInferenceAgent timed out (%.1fs)", timeout)
            return None
        except Exception as e:
            logger.debug("[AGI-OS] GoalInferenceAgent query failed: %s", e)
            return None

    async def trigger_action(
        self,
        action_type: str,
        target: str,
        **kwargs
    ) -> str:
        """
        Manually trigger an autonomous action.

        Args:
            action_type: Type of action
            target: Target of action
            **kwargs: Additional parameters

        Returns:
            Correlation ID for tracking
        """
        if self._action_orchestrator:
            return await self._action_orchestrator.trigger_detection(
                issue_type=action_type,
                location=target,
                description=kwargs.get('description', f"{action_type} on {target}"),
                severity=kwargs.get('severity', 'medium')
            )
        return ""

    def get_status(self) -> Dict[str, Any]:
        """Get comprehensive AGI OS status."""
        return {
            'state': self._state.value,
            'started_at': self._started_at.isoformat() if self._started_at else None,
            'uptime_seconds': self._stats['uptime_seconds'],
            'components': {
                name: {
                    'available': status.available,
                    'healthy': status.healthy,
                    'error': status.error,
                }
                for name, status in self._component_status.items()
            },
            'stats': self._stats.copy(),
            'config': self._config.copy(),
        }

    def get_component(self, name: str) -> Optional[Any]:
        """
        Get a component by name.

        Available components:
        - voice: RealTimeVoiceCommunicator
        - approval: VoiceApprovalManager
        - events: ProactiveEventStream
        - orchestrator: IntelligentActionOrchestrator
        - uae: UnifiedAwarenessEngine
        - sai: SelfAwareIntelligence
        - cai: ContextAwarenessIntelligence
        - neural_mesh: NeuralMeshCoordinator
        - hybrid: HybridOrchestrator
        """
        components = {
            'voice': self._voice,
            'approval': self._approval_manager,
            'events': self._event_stream,
            'orchestrator': self._action_orchestrator,
            'uae': self._uae_engine,
            'sai': self._sai_system,
            'cai': self._cai_system,
            'neural_mesh': self._neural_mesh,
            'hybrid': self._hybrid_orchestrator,
            'learning_db': self._learning_db,
            'ghost_hands': self._ghost_hands,
            'ghost_display': self._ghost_display,
            'agent_runtime': self._resolve_agent_runtime(),
        }
        return components.get(name)

    def configure(self, **kwargs) -> None:
        """
        Configure AGI OS parameters.

        Args:
            enable_voice: Enable/disable voice output
            enable_proactive_monitoring: Enable/disable screen monitoring
            enable_autonomous_actions: Enable/disable autonomous execution
            voice_greeting: Enable/disable startup greeting
            health_check_interval: Seconds between health checks
        """
        for key, value in kwargs.items():
            if key in self._config:
                self._config[key] = value
                logger.info("Config updated: %s = %s", key, value)


# ============== Singleton Pattern ==============

_agi_os: Optional[AGIOSCoordinator] = None


async def get_agi_os() -> AGIOSCoordinator:
    """
    Get the global AGI OS coordinator instance.

    Returns:
        The AGIOSCoordinator singleton
    """
    global _agi_os

    if _agi_os is None:
        _agi_os = AGIOSCoordinator()

    return _agi_os


async def start_agi_os(
    progress_callback=None,
    startup_budget_seconds: Optional[float] = None,
) -> AGIOSCoordinator:
    """
    Get and start the global AGI OS coordinator.

    Args:
        progress_callback: Optional async callable(step: str, detail: str)
            forwarded to AGIOSCoordinator.start() for DMS progress reporting.
        startup_budget_seconds: Optional total startup budget passed through to
            AGIOSCoordinator.start().

    Returns:
        The started AGIOSCoordinator instance
    """
    agi = await get_agi_os()
    if agi._state == AGIOSState.OFFLINE:
        await agi.start(
            progress_callback=progress_callback,
            startup_budget_seconds=startup_budget_seconds,
        )
    return agi


async def stop_agi_os() -> None:
    """Stop the global AGI OS coordinator and mesh singletons."""
    global _agi_os

    coordinator = _agi_os
    _agi_os = None

    if coordinator is not None:
        await coordinator.stop()

    # v257.0: AGI teardown must also stop global Neural Mesh singletons.
    # AGIOSCoordinator.stop() handles its owned coordinator instance, but
    # standalone mesh bridge/monitoring singletons may still be active and can
    # leak background tasks into loop shutdown.
    try:
        try:
            from neural_mesh import stop_jarvis_neural_mesh, stop_neural_mesh
        except ImportError:
            from backend.neural_mesh import stop_jarvis_neural_mesh, stop_neural_mesh

        mesh_stop_timeout = max(
            1.0,
            _env_float("AGI_OS_GLOBAL_MESH_STOP_TIMEOUT", 20.0),
        )
        await asyncio.wait_for(stop_jarvis_neural_mesh(), timeout=mesh_stop_timeout)
        await asyncio.wait_for(stop_neural_mesh(), timeout=mesh_stop_timeout)
    except asyncio.TimeoutError:
        logger.warning(
            "Timed out stopping global Neural Mesh singletons after %.1fs",
            mesh_stop_timeout,
        )
    except Exception as mesh_err:
        logger.debug("Global Neural Mesh singleton teardown skipped: %s", mesh_err)


if __name__ == "__main__":
    async def test():
        """Test the AGI OS coordinator."""
        print("Testing AGIOSCoordinator...")

        agi = await start_agi_os()

        print(f"\nStatus: {agi.get_status()}")

        # Wait for startup speech
        await asyncio.sleep(5)

        # Test speech
        await agi.speak("This is a test of the AGI OS voice system.")

        await asyncio.sleep(3)

        # Test action trigger
        correlation_id = await agi.trigger_action(
            action_type="error",
            target="test.py",
            description="Test error detection"
        )
        print(f"\nTriggered action: {correlation_id}")

        await asyncio.sleep(5)

        print(f"\nFinal status: {agi.get_status()}")

        await stop_agi_os()
        print("\nTest complete!")

    asyncio.run(test())
