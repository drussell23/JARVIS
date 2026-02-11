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
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import Any, Dict, List, Optional

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

        # Hybrid Orchestrator
        self._hybrid_orchestrator: Optional[Any] = None

        # Screen Analyzer
        self._screen_analyzer: Optional[Any] = None

        # Owner Identity Service (dynamic voice biometric identification)
        self._owner_identity: Optional[OwnerIdentityService] = None

        # Speaker Verification Service (for voice biometrics)
        self._speaker_verification: Optional[Any] = None

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

    async def start(self) -> None:
        """
        Start the AGI OS.

        Initializes all components and begins autonomous operation.
        """
        if self._state == AGIOSState.ONLINE:
            logger.warning("AGI OS already online")
            return

        self._state = AGIOSState.INITIALIZING
        self._started_at = datetime.now()
        logger.info("Starting AGI OS...")

        # Initialize components in order
        await self._init_agi_os_components()
        await self._init_intelligence_systems()
        await self._init_neural_mesh()
        await self._init_hybrid_orchestrator()
        await self._init_screen_analyzer()
        await self._connect_components()

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

        # Announce shutdown with dynamic owner name
        if self._voice:
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
            await self._voice.speak(
                random.choice(shutdown_messages),
                mode=VoiceMode.QUIET
            )
            await asyncio.sleep(2)

        # Cancel health monitor
        if self._health_task:
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass

        # v237.2: Stop event bridge first (unsubscribes from bus + event stream)
        if self._event_bridge:
            try:
                await self._event_bridge.stop()
            except Exception as e:
                logger.warning("Error stopping event bridge: %s", e)

        # v237.3: Stop macOS monitors via instance .stop() — NOT the singleton
        # destructors (stop_notification_monitor / stop_system_event_monitor).
        # Singleton destructors null the global reference, which breaks other
        # lifecycle managers (e.g. MacOSHelperCoordinator) that hold the same
        # singleton. Instance .stop() is idempotent (checks _running) and
        # preserves the global for potential restart by other coordinators.
        if self._notification_monitor:
            try:
                await self._notification_monitor.stop()
            except Exception as e:
                logger.warning("Error stopping NotificationMonitor: %s", e)

        if self._system_event_monitor:
            try:
                await self._system_event_monitor.stop()
            except Exception as e:
                logger.warning("Error stopping SystemEventMonitor: %s", e)

        # v237.3: Stop screen analyzer (stop monitoring before event stream stops)
        if self._screen_analyzer:
            try:
                await self._screen_analyzer.stop_monitoring()
            except Exception as e:
                logger.warning("Error stopping screen analyzer: %s", e)

        # Stop components in reverse order
        await stop_action_orchestrator()
        await stop_event_stream()
        await stop_voice_communicator()

        # v237.0: Stop JARVIS Bridge (stops adapter agents, cancels startup tasks)
        if self._jarvis_bridge:
            try:
                await self._jarvis_bridge.stop()
            except Exception as e:
                logger.warning("Error stopping JARVIS Bridge: %s", e)

        # Stop Neural Mesh (stops production agents, bus, registry, orchestrator)
        # Note: bridge.stop() above already calls coordinator.stop() internally.
        # This second call is a defensive no-op (coordinator.stop() is idempotent).
        if self._neural_mesh:
            try:
                await self._neural_mesh.stop()
            except Exception as e:
                logger.warning("Error stopping Neural Mesh: %s", e)

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

        # Learning Database
        try:
            from core.hybrid_orchestrator import _get_learning_db
            self._learning_db = await _get_learning_db()
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

        # Speaker Verification Service (for voice biometrics)
        # v236.1: Use singleton to avoid duplicate instances (double memory,
        # competing encoder loads, enrollment updates not shared).
        try:
            from voice.speaker_verification_service import get_speaker_verification_service
            self._speaker_verification = await get_speaker_verification_service(
                learning_db=self._learning_db
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

        # Owner Identity Service (dynamic voice biometric identification)
        try:
            self._owner_identity = await get_owner_identity(
                speaker_verification=self._speaker_verification,
                learning_db=self._learning_db
            )
            owner_profile = await self._owner_identity.get_current_owner()
            self._component_status['owner_identity'] = ComponentStatus(
                name='owner_identity',
                available=True
            )
            logger.info(
                "Owner identity service loaded - Owner: %s (confidence: %s)",
                owner_profile.name,
                owner_profile.identity_confidence.value
            )
        except Exception as e:
            logger.warning("Owner identity service not available: %s", e)
            self._component_status['owner_identity'] = ComponentStatus(
                name='owner_identity',
                available=False,
                error=str(e)
            )

    async def _init_neural_mesh(self) -> None:
        """Initialize Neural Mesh coordinator and production agents."""
        try:
            from neural_mesh import start_neural_mesh
            self._neural_mesh = await start_neural_mesh()

            # v237.0: Register production agents (previously only called from deprecated supervisor)
            n_production = 0
            try:
                from neural_mesh.agents.agent_initializer import initialize_production_agents
                registered = await initialize_production_agents(self._neural_mesh)
                n_production = len(registered)
            except Exception as agent_exc:
                logger.warning("Production agent initialization failed (mesh still running): %s", agent_exc)

            # v237.0: Wire system adapters (voice, vision, intelligence, autonomy)
            # Bridge uses the same coordinator singleton — no conflict with production agents
            n_adapters = 0
            try:
                from neural_mesh import start_jarvis_neural_mesh
                self._jarvis_bridge = await start_jarvis_neural_mesh()
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

    async def process_command(self, command: str) -> str:
        """
        Process a user command.

        Args:
            command: User command text

        Returns:
            Response text
        """
        self._stats['commands_processed'] += 1

        # Use hybrid orchestrator for command processing
        if self._hybrid_orchestrator:
            try:
                result = await self._hybrid_orchestrator.process(command)
                return result.get('response', "Command processed.")
            except Exception as e:
                logger.error("Command processing error: %s", e)
                return f"Error processing command: {e}"

        return "Command processing not available."

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


async def start_agi_os() -> AGIOSCoordinator:
    """
    Get and start the global AGI OS coordinator.

    Returns:
        The started AGIOSCoordinator instance
    """
    agi = await get_agi_os()
    if agi._state == AGIOSState.OFFLINE:
        await agi.start()
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
