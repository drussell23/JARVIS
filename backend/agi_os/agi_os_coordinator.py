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

    async def start(self, progress_callback=None) -> None:
        """
        Start the AGI OS.

        Initializes all components and begins autonomous operation.

        Args:
            progress_callback: Optional async callable(step: str, detail: str)
                Reports initialization progress back to the supervisor so the
                DMS watchdog doesn't trigger a stall during the 60-75s init
                sequence. v250.0: Without this, progress 86→87 gap exceeds
                the 60s stall threshold.
        """
        if self._state == AGIOSState.ONLINE:
            logger.warning("AGI OS already online")
            return

        self._state = AGIOSState.INITIALIZING
        self._started_at = datetime.now()
        logger.info("Starting AGI OS...")

        # v250.1: Store callback as instance attr so sub-methods can report
        # intra-phase progress. Without this, _init_agi_os_components() runs
        # 90+ seconds with zero DMS heartbeats → stall → rollback.
        self._progress_callback = progress_callback

        async def _report(step: str, detail: str) -> None:
            if progress_callback:
                try:
                    await progress_callback(step, detail)
                except Exception as e:
                    logger.debug("Progress callback error (non-fatal): %s", e)

        # Initialize components in order, reporting progress after each phase
        await self._init_agi_os_components()
        await _report("agi_os_components", "Core components initialized")

        await self._init_intelligence_systems()
        await _report("intelligence_systems", "Intelligence systems initialized")

        await self._init_neural_mesh()
        await _report("neural_mesh", "Neural Mesh initialized")

        await self._init_hybrid_orchestrator()
        await _report("hybrid_orchestrator", "Hybrid orchestrator initialized")

        await self._init_screen_analyzer()
        await _report("screen_analyzer", "Screen analyzer initialized")

        await self._connect_components()
        await _report("components_connected", "All components connected")

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

        stop_step_timeout = max(0.1, _env_float("AGI_OS_STOP_STEP_TIMEOUT", 3.0))
        health_task_timeout = max(0.1, _env_float("AGI_OS_HEALTH_TASK_STOP_TIMEOUT", 1.5))
        farewell_timeout = max(0.1, _env_float("AGI_OS_SHUTDOWN_ANNOUNCE_TIMEOUT", 2.5))

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
            except Exception as e:
                logger.warning("Error stopping %s: %s", name, e)

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

            # Cancel health monitor.
            if self._health_task:
                self._health_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.wait_for(self._health_task, timeout=health_task_timeout)
                self._health_task = None

            # v237.2: Stop event bridge first (unsubscribes from bus + event stream).
            if self._event_bridge:
                await _run_stop_step("event bridge", self._event_bridge.stop)

            # v237.3: Stop macOS monitors via instance .stop() — NOT singleton destructors.
            if self._notification_monitor:
                await _run_stop_step("NotificationMonitor", self._notification_monitor.stop)

            if self._system_event_monitor:
                await _run_stop_step("SystemEventMonitor", self._system_event_monitor.stop)

            # v237.4: Stop Ghost Display before Ghost Hands.
            if self._ghost_display and hasattr(self._ghost_display, 'cleanup'):
                await _run_stop_step("Ghost Display", self._ghost_display.cleanup)

            if self._ghost_hands:
                await _run_stop_step("Ghost Hands", self._ghost_hands.stop)

            # v237.3: Stop screen analyzer before event stream shutdown.
            if self._screen_analyzer and hasattr(self._screen_analyzer, 'stop_monitoring'):
                await _run_stop_step("screen analyzer", self._screen_analyzer.stop_monitoring)

            runtime = self._resolve_agent_runtime()
            if runtime and hasattr(runtime, "stop"):
                await _run_stop_step("agent runtime", runtime.stop)

            # Stop singleton-managed components in reverse order.
            await _run_stop_step("action orchestrator", stop_action_orchestrator)
            await _run_stop_step("event stream", stop_event_stream)
            await _run_stop_step("voice communicator", stop_voice_communicator)

            # v237.0: Stop JARVIS Bridge (stops adapter agents, cancels startup tasks).
            if self._jarvis_bridge:
                await _run_stop_step("JARVIS Bridge", self._jarvis_bridge.stop)

            # Stop Neural Mesh (stops production agents, bus, registry, orchestrator).
            # Note: bridge.stop() above already calls coordinator.stop() internally.
            # This second call is a defensive no-op when already stopped.
            if self._neural_mesh:
                await _run_stop_step("Neural Mesh", self._neural_mesh.stop)
        finally:
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
                await cb(f"init_{component}", detail)
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
        started = time.monotonic()
        task = asyncio.create_task(operation(), name=f"agi_os_init_{step_name}")

        try:
            while True:
                elapsed = time.monotonic() - started
                remaining = timeout_seconds - elapsed
                if remaining <= 0:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task
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
        except Exception:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            raise

    async def _init_agi_os_components(self) -> None:
        """Initialize core AGI OS components."""
        # Voice communicator
        if self._config['enable_voice']:
            try:
                self._voice = await get_voice_communicator()
                self._component_status['voice'] = ComponentStatus(
                    name='voice',
                    available=True
                )
                logger.info("Voice communicator initialized")
            except Exception as e:
                logger.warning("Voice communicator failed: %s", e)
                self._component_status['voice'] = ComponentStatus(
                    name='voice',
                    available=False,
                    healthy=False,
                    error=str(e)
                )
        await self._report_init_progress("voice", "Voice communicator done")

        # Approval manager
        try:
            self._approval_manager = await get_approval_manager()
            self._component_status['approval'] = ComponentStatus(
                name='approval',
                available=True
            )
            logger.info("Approval manager initialized")
        except Exception as e:
            logger.warning("Approval manager failed: %s", e)
            self._component_status['approval'] = ComponentStatus(
                name='approval',
                available=False,
                healthy=False,
                error=str(e)
            )
        await self._report_init_progress("approval", "Approval manager done")

        # Event stream
        try:
            self._event_stream = await get_event_stream()
            self._component_status['events'] = ComponentStatus(
                name='events',
                available=True
            )
            logger.info("Event stream initialized")
        except Exception as e:
            logger.warning("Event stream failed: %s", e)
            self._component_status['events'] = ComponentStatus(
                name='events',
                available=False,
                healthy=False,
                error=str(e)
            )
        await self._report_init_progress("events", "Event stream done")

        # Action orchestrator
        if self._config['enable_autonomous_actions']:
            try:
                self._action_orchestrator = await start_action_orchestrator()
                self._component_status['orchestrator'] = ComponentStatus(
                    name='orchestrator',
                    available=True
                )
                logger.info("Action orchestrator initialized")
            except Exception as e:
                logger.warning("Action orchestrator failed: %s", e)
                self._component_status['orchestrator'] = ComponentStatus(
                    name='orchestrator',
                    available=False,
                    healthy=False,
                    error=str(e)
                )
        await self._report_init_progress("orchestrator", "Action orchestrator done")

        # v237.2: Start macOS notification monitor
        try:
            from macos_helper.notification_monitor import get_notification_monitor
            self._notification_monitor = await get_notification_monitor(auto_start=True)
            self._component_status['notification_monitor'] = ComponentStatus(
                name='notification_monitor',
                available=True
            )
            logger.info("NotificationMonitor started")
        except Exception as e:
            logger.warning("NotificationMonitor not available: %s", e)
            self._component_status['notification_monitor'] = ComponentStatus(
                name='notification_monitor',
                available=False,
                error=str(e)
            )
        await self._report_init_progress("notification_monitor", "Notification monitor done")

        # v237.3: Start macOS system event monitor (app focus, idle, sleep/wake, spaces)
        try:
            from macos_helper.system_event_monitor import get_system_event_monitor
            self._system_event_monitor = await get_system_event_monitor(auto_start=True)
            self._component_status['system_event_monitor'] = ComponentStatus(
                name='system_event_monitor',
                available=True
            )
            logger.info("SystemEventMonitor started")
        except Exception as e:
            logger.warning("SystemEventMonitor not available: %s", e)
            self._component_status['system_event_monitor'] = ComponentStatus(
                name='system_event_monitor',
                available=False,
                error=str(e)
            )
        await self._report_init_progress("system_event_monitor", "System event monitor done")

        # v237.4: Start Ghost Hands orchestrator (background automation)
        try:
            from ghost_hands.orchestrator import get_ghost_hands
            self._ghost_hands = await get_ghost_hands()
            self._component_status['ghost_hands'] = ComponentStatus(
                name='ghost_hands',
                available=True
            )
            logger.info("Ghost Hands orchestrator started")
        except Exception as e:
            logger.warning("Ghost Hands not available: %s", e)
            self._component_status['ghost_hands'] = ComponentStatus(
                name='ghost_hands',
                available=False,
                error=str(e)
            )
        await self._report_init_progress("ghost_hands", "Ghost Hands done")

        # v237.4: Ghost Mode Display (virtual display management)
        try:
            from vision.yabai_space_detector import get_ghost_manager
            self._ghost_display = get_ghost_manager()
            self._component_status['ghost_display'] = ComponentStatus(
                name='ghost_display',
                available=True
            )
            logger.info("Ghost Display Manager registered")
        except Exception as e:
            logger.warning("Ghost Display Manager not available: %s", e)
            self._component_status['ghost_display'] = ComponentStatus(
                name='ghost_display',
                available=False,
                error=str(e)
            )
        await self._report_init_progress("ghost_display", "Ghost Display done")

    async def _init_intelligence_systems(self) -> None:
        """Initialize intelligence systems (UAE, SAI, CAI)."""
        # UAE (Unified Awareness Engine)
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

        # SAI (Self-Aware Intelligence)
        try:
            from core.hybrid_orchestrator import _get_sai
            self._sai_system = _get_sai()
            if self._sai_system:
                self._component_status['sai'] = ComponentStatus(
                    name='sai',
                    available=True
                )
                logger.info("SAI loaded")
        except Exception as e:
            logger.warning("SAI not available: %s", e)
            self._component_status['sai'] = ComponentStatus(
                name='sai',
                available=False,
                error=str(e)
            )
        await self._report_init_progress("sai", "SAI initialization complete")

        # CAI (Context Awareness Intelligence)
        try:
            from core.hybrid_orchestrator import _get_cai
            self._cai_system = _get_cai()
            if self._cai_system:
                self._component_status['cai'] = ComponentStatus(
                    name='cai',
                    available=True
                )
                logger.info("CAI loaded")
        except Exception as e:
            logger.warning("CAI not available: %s", e)
            self._component_status['cai'] = ComponentStatus(
                name='cai',
                available=False,
                error=str(e)
            )
        await self._report_init_progress("cai", "CAI initialization complete")

        # Learning Database
        try:
            from core.hybrid_orchestrator import _get_learning_db
            learning_db_timeout = _env_float("JARVIS_AGI_OS_LEARNING_DB_TIMEOUT", 60.0)
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

        # Speaker Verification Service (for voice biometrics)
        # v236.1: Use singleton to avoid duplicate instances (double memory,
        # competing encoder loads, enrollment updates not shared).
        try:
            from voice.speaker_verification_service import get_speaker_verification_service
            speaker_timeout = _env_float("JARVIS_AGI_OS_SPEAKER_TIMEOUT", 90.0)

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

        # Owner Identity Service (dynamic voice biometric identification)
        try:
            owner_identity_timeout = _env_float("JARVIS_AGI_OS_OWNER_ID_TIMEOUT", 45.0)

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

    async def _init_neural_mesh(self) -> None:
        """Initialize Neural Mesh coordinator and production agents."""
        try:
            from neural_mesh import start_neural_mesh
            mesh_timeout = _env_float("JARVIS_AGI_OS_NEURAL_MESH_TIMEOUT", 120.0)
            self._neural_mesh = await self._run_timed_init_step(
                "neural_mesh",
                start_neural_mesh,
                timeout_seconds=mesh_timeout,
            )

            # v237.0: Register production agents (previously only called from deprecated supervisor)
            n_production = 0
            try:
                from neural_mesh.agents.agent_initializer import initialize_production_agents
                agent_timeout = _env_float("JARVIS_AGI_OS_NEURAL_AGENT_TIMEOUT", 90.0)

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

            # v237.0: Wire system adapters (voice, vision, intelligence, autonomy)
            # Bridge uses the same coordinator singleton — no conflict with production agents
            n_adapters = 0
            try:
                from neural_mesh import start_jarvis_neural_mesh
                bridge_timeout = _env_float("JARVIS_AGI_OS_NEURAL_BRIDGE_TIMEOUT", 90.0)
                self._jarvis_bridge = await self._run_timed_init_step(
                    "neural_mesh_bridge",
                    start_jarvis_neural_mesh,
                    timeout_seconds=bridge_timeout,
                )
                n_adapters = len(self._jarvis_bridge.registered_agents) if hasattr(self._jarvis_bridge, 'registered_agents') else 0
            except Exception as bridge_exc:
                logger.warning("JARVIS Neural Mesh Bridge failed (mesh still running): %s", bridge_exc)

            total = len(self._neural_mesh.get_all_agents()) if hasattr(self._neural_mesh, 'get_all_agents') else n_production + n_adapters
            logger.info("Neural Mesh started: %d agents total (%d production, %d adapters)", total, n_production, n_adapters)

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
            from vision.claude_vision_analyzer_main import ClaudeVisionAnalyzer
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

            vision_handler = ClaudeVisionAnalyzer(api_key=api_key)

            # v237.3: Permission pre-check — test capture before starting monitoring.
            # macOS requires Screen Recording permission for Quartz/CGDisplay capture.
            # Without it, capture returns None or unusably small images.
            try:
                test_capture = await vision_handler.capture_screen()
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

            self._screen_analyzer = MemoryAwareScreenAnalyzer(vision_handler)
            await self._screen_analyzer.start_monitoring()

            # Bridge to event stream for proactive detection
            await connect_screen_analyzer(
                self._screen_analyzer,
                enable_claude_vision=True,
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
                # Emit event for approval decisions
                asyncio.create_task(
                    self._event_stream.emit(AGIEvent(
                        event_type=EventType.USER_APPROVED if response.approved else EventType.USER_DENIED,
                        source="approval_manager",
                        data={'request_id': request.request_id},
                        correlation_id=request.request_id,
                    ))
                )

            self._approval_manager.on_approval(on_approval)

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
                    logger.info("[MeshToolLifecycle] Health-aware tool lifecycle active")

            except Exception as e:
                logger.warning("Mesh tool registration failed (non-fatal): %s", e)

        logger.debug("Components connected")

    async def _announce_startup(self) -> None:
        """Announce AGI OS startup via voice with dynamic owner identification."""
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
        """Determine overall health state."""
        available = sum(1 for s in self._component_status.values() if s.available)
        total = len(self._component_status)

        # Core components that must be available
        core_available = all(
            self._component_status.get(name, ComponentStatus(name=name, available=False)).available
            for name in ['voice', 'events', 'orchestrator']
        )

        if available == total and core_available:
            return AGIOSState.ONLINE
        elif available > 0 and core_available:
            return AGIOSState.DEGRADED
        else:
            return AGIOSState.DEGRADED

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
        """Check health of all components."""
        # Event stream
        if self._event_stream:
            stats = self._event_stream.get_stats()
            self._component_status['events'].healthy = stats.get('running', False)

        # Voice
        if self._voice:
            status = self._voice.get_status()
            self._component_status['voice'].healthy = status.get('running', False)

        # Orchestrator
        if self._action_orchestrator:
            stats = self._action_orchestrator.get_stats()
            self._component_status['orchestrator'].healthy = stats.get('state') == 'running'

        # Neural Mesh
        if self._neural_mesh:
            try:
                health = await self._neural_mesh.health_check()
                self._component_status['neural_mesh'].healthy = health.get('status') == 'healthy'
            except Exception:
                self._component_status['neural_mesh'].healthy = False

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


async def start_agi_os(progress_callback=None) -> AGIOSCoordinator:
    """
    Get and start the global AGI OS coordinator.

    Args:
        progress_callback: Optional async callable(step: str, detail: str)
            forwarded to AGIOSCoordinator.start() for DMS progress reporting.

    Returns:
        The started AGIOSCoordinator instance
    """
    agi = await get_agi_os()
    if agi._state == AGIOSState.OFFLINE:
        await agi.start(progress_callback=progress_callback)
    return agi


async def stop_agi_os() -> None:
    """Stop the global AGI OS coordinator."""
    global _agi_os

    if _agi_os is not None:
        await _agi_os.stop()
        _agi_os = None


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
