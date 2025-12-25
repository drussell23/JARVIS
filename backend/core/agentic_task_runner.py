"""
JARVIS Agentic Task Runner - Core Module v2.0
==============================================

The unified agentic execution engine for JARVIS. This module provides:

- AgenticTaskRunner: Main orchestrator for Computer Use execution
- RunnerMode: Execution modes (direct, autonomous, supervised)
- AgenticTaskResult: Result dataclass for task execution

Integration:
    This module is designed to be instantiated and managed by the
    JARVISSupervisor (run_supervisor.py). The TieredCommandRouter
    routes Tier 2 commands to this runner for agentic execution.

Architecture:
    ┌────────────────────────────────────────────────────────────────┐
    │                     JARVISSupervisor                           │
    │  ┌──────────────┐   ┌──────────────────┐   ┌───────────────┐  │
    │  │   Tiered     │ → │  Agentic         │ → │   Computer    │  │
    │  │   Router     │   │  TaskRunner      │   │   Use Tool    │  │
    │  │   (Tier 2)   │   │                  │   │               │  │
    │  └──────────────┘   └────────┬─────────┘   └───────────────┘  │
    │                              │                                 │
    │                    ┌─────────▼─────────┐                       │
    │                    │    Watchdog       │                       │
    │                    │   (Safety)        │                       │
    │                    └───────────────────┘                       │
    └────────────────────────────────────────────────────────────────┘

Author: JARVIS AI System
Version: 2.0.0 (Unified)
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple, TYPE_CHECKING
import uuid

logger = logging.getLogger(__name__)


# =============================================================================
# Neural Mesh Deep Integration Types
# =============================================================================

@dataclass
class NeuralMeshTaskEvent:
    """Event data for Neural Mesh task notifications."""
    task_id: str
    event_type: str  # task_started, task_progress, task_completed, task_failed
    goal: str
    mode: str
    timestamp: float
    metadata: Dict[str, Any] = field(default_factory=dict)

@dataclass
class NeuralMeshContext:
    """Context enrichment from Neural Mesh knowledge graph."""
    similar_goals: List[Dict[str, Any]] = field(default_factory=list)
    pattern_insights: List[str] = field(default_factory=list)
    recommended_actions: List[str] = field(default_factory=list)
    context_score: float = 0.0


# =============================================================================
# Configuration
# =============================================================================

@dataclass
class AgenticRunnerConfig:
    """Configuration for the Agentic Task Runner."""

    # Execution settings
    default_mode: str = field(
        default_factory=lambda: os.getenv("JARVIS_AGENTIC_DEFAULT_MODE", "supervised")
    )
    max_actions_per_task: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_MAX_ACTIONS", "50"))
    )
    task_timeout_seconds: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_TASK_TIMEOUT", "300"))
    )

    # Component toggles
    uae_enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_UAE_ENABLED", "true").lower() == "true"
    )
    neural_mesh_enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_NEURAL_MESH_ENABLED", "true").lower() == "true"
    )
    learning_enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_LEARNING_ENABLED", "true").lower() == "true"
    )

    # Narration
    narrate_by_default: bool = field(
        default_factory=lambda: os.getenv("JARVIS_NARRATE_TASKS", "true").lower() == "true"
    )

    # Watchdog integration
    watchdog_integration: bool = field(
        default_factory=lambda: os.getenv("JARVIS_WATCHDOG_ENABLED", "true").lower() == "true"
    )
    heartbeat_interval: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_HEARTBEAT_INTERVAL", "2.0"))
    )

    # Voice Authentication Layer (v5.0)
    voice_auth_enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_VOICE_AUTH_ENABLED", "true").lower() == "true"
    )
    voice_auth_pre_execution: bool = field(
        default_factory=lambda: os.getenv("JARVIS_VOICE_AUTH_PRE_EXECUTION", "true").lower() == "true"
    )

    # Neural Mesh Deep Integration (v5.0)
    neural_mesh_deep_enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_NEURAL_MESH_DEEP", "true").lower() == "true"
    )
    neural_mesh_task_events: bool = field(
        default_factory=lambda: os.getenv("JARVIS_NM_TASK_EVENTS", "true").lower() == "true"
    )
    neural_mesh_context_query: bool = field(
        default_factory=lambda: os.getenv("JARVIS_NM_CONTEXT_QUERY", "true").lower() == "true"
    )
    neural_mesh_pattern_subscribe: bool = field(
        default_factory=lambda: os.getenv("JARVIS_NM_PATTERN_SUBSCRIBE", "true").lower() == "true"
    )
    neural_mesh_agi_events: bool = field(
        default_factory=lambda: os.getenv("JARVIS_NM_AGI_EVENTS", "true").lower() == "true"
    )

    # v9.4: Neural Mesh Production Integration
    neural_mesh_production: bool = field(
        default_factory=lambda: os.getenv("JARVIS_NM_PRODUCTION", "true").lower() == "true"
    )
    neural_mesh_workflow_execution: bool = field(
        default_factory=lambda: os.getenv("JARVIS_NM_WORKFLOW_EXECUTION", "true").lower() == "true"
    )
    neural_mesh_knowledge_contribute: bool = field(
        default_factory=lambda: os.getenv("JARVIS_NM_KNOWLEDGE_CONTRIBUTE", "true").lower() == "true"
    )
    neural_mesh_agent_delegation: bool = field(
        default_factory=lambda: os.getenv("JARVIS_NM_AGENT_DELEGATION", "true").lower() == "true"
    )
    neural_mesh_use_bridge: bool = field(
        default_factory=lambda: os.getenv("JARVIS_NM_USE_BRIDGE", "true").lower() == "true"
    )

    # Autonomy Components Integration (v6.0)
    phase_manager_enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_PHASE_MANAGER_ENABLED", "true").lower() == "true"
    )
    tool_registry_enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_TOOL_REGISTRY_ENABLED", "true").lower() == "true"
    )
    memory_manager_enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_MEMORY_MANAGER_ENABLED", "true").lower() == "true"
    )
    error_recovery_enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_ERROR_RECOVERY_ENABLED", "true").lower() == "true"
    )
    uae_context_enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_UAE_CONTEXT_ENABLED", "true").lower() == "true"
    )
    intervention_enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_INTERVENTION_ENABLED", "true").lower() == "true"
    )

    # JARVIS Prime Integration (Tier-0 Brain)
    jarvis_prime_enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_PRIME_ENABLED", "true").lower() == "true"
    )
    jarvis_prime_url: str = field(
        default_factory=lambda: os.getenv("JARVIS_PRIME_URL", "http://localhost:8002")
    )
    jarvis_prime_use_cloud_run: bool = field(
        default_factory=lambda: os.getenv("JARVIS_PRIME_USE_CLOUD_RUN", "false").lower() == "true"
    )
    jarvis_prime_cloud_run_url: str = field(
        default_factory=lambda: os.getenv("JARVIS_PRIME_CLOUD_RUN_URL", "")
    )

    # Reactor-Core Integration (v10.0 - "Ignition Key")
    reactor_core_enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_REACTOR_CORE_ENABLED", "true").lower() == "true"
    )
    reactor_core_url: str = field(
        default_factory=lambda: os.getenv("REACTOR_CORE_API_URL", "http://localhost:8003")
    )
    reactor_core_auto_trigger: bool = field(
        default_factory=lambda: os.getenv("REACTOR_CORE_AUTO_TRIGGER", "true").lower() == "true"
    )
    reactor_core_experience_threshold: int = field(
        default_factory=lambda: int(os.getenv("REACTOR_CORE_EXP_THRESHOLD", "100"))
    )
    reactor_core_min_trigger_interval_hours: float = field(
        default_factory=lambda: float(os.getenv("REACTOR_CORE_MIN_INTERVAL_HOURS", "6.0"))
    )


# =============================================================================
# Enums
# =============================================================================

class RunnerMode(str, Enum):
    """Execution modes for the agentic task runner."""
    DIRECT = "direct"           # Computer Use only, no reasoning
    SUPERVISED = "supervised"   # With human checkpoints
    AUTONOMOUS = "autonomous"   # Full reasoning + execution


# =============================================================================
# Result Data Classes
# =============================================================================

@dataclass
class AgenticTaskResult:
    """Result from an agentic task execution."""
    success: bool
    goal: str
    mode: str
    execution_time_ms: float
    actions_count: int
    reasoning_steps: int
    final_message: str
    error: Optional[str] = None
    learning_insights: List[str] = field(default_factory=list)
    screenshots: List[str] = field(default_factory=list)
    uae_used: bool = False
    neural_mesh_used: bool = False
    multi_space_used: bool = False
    watchdog_status: Optional[str] = None
    # Neural Mesh Deep Integration (v5.0)
    neural_mesh_context: Optional[NeuralMeshContext] = None
    neural_mesh_events_sent: int = 0
    pattern_insights_applied: List[str] = field(default_factory=list)
    knowledge_contributions: int = 0


# =============================================================================
# Component Availability Checks
# =============================================================================

def _check_component_availability() -> Dict[str, bool]:
    """Check which components are available."""
    availability = {}

    # Computer Use Tool
    try:
        from autonomy.computer_use_tool import ComputerUseTool
        availability["computer_use_tool"] = True
    except ImportError:
        availability["computer_use_tool"] = False

    # Direct Computer Use Connector
    try:
        from autonomy.claude_computer_use_connector import ClaudeComputerUseConnector
        availability["direct_connector"] = True
    except ImportError:
        availability["direct_connector"] = False

    # Autonomous Agent
    try:
        from autonomy.autonomous_agent import AutonomousAgent
        availability["autonomous_agent"] = True
    except ImportError:
        availability["autonomous_agent"] = False

    # UAE (Unified Awareness Engine)
    try:
        from intelligence.uae_integration import get_uae, get_enhanced_uae, initialize_uae
        from intelligence.unified_awareness_engine import UnifiedAwarenessEngine
        availability["uae"] = True
    except ImportError:
        try:
            # Fallback to legacy path
            from unified_awareness.uae_core import UnifiedAwarenessEngine
            availability["uae"] = True
        except ImportError:
            availability["uae"] = False

    # Neural Mesh
    try:
        from neural_mesh.neural_mesh_coordinator import NeuralMeshCoordinator
        availability["neural_mesh"] = True
    except ImportError:
        availability["neural_mesh"] = False

    # Watchdog
    try:
        from core.agentic_watchdog import AgenticWatchdog
        availability["watchdog"] = True
    except ImportError:
        availability["watchdog"] = False

    # Voice Authentication Layer
    try:
        from core.voice_authentication_layer import VoiceAuthenticationLayer
        availability["voice_auth_layer"] = True
    except ImportError:
        availability["voice_auth_layer"] = False

    # =========================================================================
    # Autonomy Components (v6.0)
    # =========================================================================

    # Phase Manager
    try:
        from autonomy.langgraph_phase_manager import LangGraphPhaseManager
        availability["phase_manager"] = True
    except ImportError:
        availability["phase_manager"] = False

    # Tool Registry
    try:
        from autonomy.unified_tool_registry import UnifiedToolRegistry
        availability["tool_registry"] = True
    except ImportError:
        availability["tool_registry"] = False

    # Memory Manager
    try:
        from autonomy.unified_memory_manager import UnifiedMemoryManager
        availability["memory_manager"] = True
    except ImportError:
        availability["memory_manager"] = False

    # Error Recovery Orchestrator
    try:
        from autonomy.error_recovery_orchestrator import ErrorRecoveryOrchestrator
        availability["error_recovery"] = True
    except ImportError:
        availability["error_recovery"] = False

    # UAE Context Manager
    try:
        from autonomy.uae_context_manager import UAEContextManager
        availability["uae_context"] = True
    except ImportError:
        availability["uae_context"] = False

    # Intervention Orchestrator
    try:
        from autonomy.intervention_orchestrator import InterventionOrchestrator
        availability["intervention"] = True
    except ImportError:
        availability["intervention"] = False

    return availability


# =============================================================================
# Agentic Task Runner
# =============================================================================

class AgenticTaskRunner:
    """
    Main orchestrator for agentic task execution.

    This class manages:
    - Computer Use tool for screen interactions
    - Autonomous Agent for reasoning (optional)
    - UAE for context awareness (optional)
    - Neural Mesh for multi-agent coordination (optional)
    - Watchdog for safety monitoring

    Designed to be instantiated by JARVISSupervisor and used by TieredCommandRouter.
    """

    def __init__(
        self,
        config: Optional[AgenticRunnerConfig] = None,
        tts_callback: Optional[Callable[[str], Awaitable[None]]] = None,
        watchdog: Optional[Any] = None,  # Type hint as Any to avoid circular import
        logger: Optional[logging.Logger] = None,
    ):
        """
        Initialize the Agentic Task Runner.

        Args:
            config: Runner configuration
            tts_callback: Text-to-speech callback for narration
            watchdog: Pre-initialized watchdog instance (from supervisor)
            logger: Logger instance
        """
        self.config = config or AgenticRunnerConfig()
        self.tts_callback = tts_callback
        self._external_watchdog = watchdog  # Watchdog provided by supervisor
        self.logger = logger or logging.getLogger(__name__)

        # Components (lazy initialized)
        self._uae = None
        self._enhanced_uae = None  # Enhanced UAE with chain-of-thought reasoning
        self._neural_mesh = None
        self._autonomous_agent = None
        self._computer_use_tool = None
        self._computer_use_connector = None
        self._watchdog = watchdog  # Use external watchdog if provided
        self._voice_auth_layer = None  # v5.0: Voice Authentication Layer

        # Autonomy Components (v6.0)
        self._phase_manager = None
        self._tool_registry = None
        self._memory_manager = None
        self._error_recovery = None
        self._uae_context = None
        self._intervention = None
        self._jarvis_prime_client = None

        # Reactor-Core Client (v10.0 - "Ignition Key")
        self._reactor_core_client = None
        self._experience_count = 0  # Track experiences for auto-trigger

        # Component availability
        self._availability = _check_component_availability()

        # State
        self._initialized = False
        self._tasks_executed = 0
        self._tasks_succeeded = 0
        self._current_task_id: Optional[str] = None
        self._heartbeat_task: Optional[asyncio.Task] = None

        # Neural Mesh Deep Integration state (v5.0)
        self._nm_pattern_insights: List[str] = []
        self._nm_events_sent: int = 0
        self._nm_knowledge_contributions: int = 0
        self._nm_pattern_subscription_active: bool = False
        self._nm_agi_subscription_active: bool = False

        # v9.4: Neural Mesh Production Integration state
        self._neural_mesh_coordinator = None  # Production coordinator
        self._neural_mesh_bridge = None  # JARVIS bridge for cross-system tasks
        self._nm_production_active: bool = False
        self._nm_workflows_executed: int = 0
        self._nm_agents_delegated: int = 0
        self._nm_knowledge_entries_added: int = 0

        # Autonomy Components Integration state (v6.0)
        self._phase_execution_active: bool = False
        self._current_phase: Optional[str] = None
        self._phase_checkpoints: List[Dict[str, Any]] = []
        self._experience_replay_cache: List[Dict[str, Any]] = []
        self._active_interventions: int = 0

        self.logger.info("[AgenticRunner] Created")
        self._log_availability()

    def _log_availability(self):
        """Log component availability."""
        self.logger.info("[AgenticRunner] Component availability:")
        for name, available in self._availability.items():
            status = "✓" if available else "✗"
            self.logger.debug(f"  {status} {name}")

    # =========================================================================
    # Initialization
    # =========================================================================

    async def initialize(self) -> bool:
        """
        Initialize all available components.

        Returns:
            True if initialization successful
        """
        if self._initialized:
            return True

        self.logger.info("[AgenticRunner] Initializing...")

        try:
            # Initialize Computer Use Tool (required)
            if self._availability.get("computer_use_tool"):
                try:
                    from autonomy.computer_use_tool import get_computer_use_tool
                    self._computer_use_tool = get_computer_use_tool(
                        tts_callback=self.tts_callback,
                    )
                    self.logger.info("[AgenticRunner] ✓ Computer Use Tool")
                except Exception as e:
                    self.logger.warning(f"[AgenticRunner] ✗ Computer Use Tool: {e}")

            # Initialize Direct Connector (fallback)
            if self._availability.get("direct_connector") and not self._computer_use_tool:
                try:
                    from autonomy.claude_computer_use_connector import get_computer_use_connector
                    self._computer_use_connector = get_computer_use_connector(
                        tts_callback=self.tts_callback
                    )
                    self.logger.info("[AgenticRunner] ✓ Direct Connector (fallback)")
                except Exception as e:
                    self.logger.warning(f"[AgenticRunner] ✗ Direct Connector: {e}")

            # Initialize UAE (optional) - connects to intelligence.uae_integration
            if self._availability.get("uae") and self.config.uae_enabled:
                try:
                    # Try the new intelligence module path first
                    from intelligence.uae_integration import get_uae, get_enhanced_uae
                    self._uae = get_uae()
                    if self._uae and not self._uae.is_active:
                        await self._uae.start()

                    # Also get enhanced UAE for chain-of-thought reasoning
                    self._enhanced_uae = get_enhanced_uae()

                    if self._uae:
                        self.logger.info("[AgenticRunner] ✓ UAE (screen awareness)")
                        if self._enhanced_uae:
                            self.logger.info("[AgenticRunner] ✓ Enhanced UAE (chain-of-thought)")
                    else:
                        self.logger.warning("[AgenticRunner] UAE not initialized by supervisor")
                except ImportError:
                    # Fallback to legacy path
                    try:
                        from unified_awareness.uae_core import get_uae_engine
                        self._uae = get_uae_engine()
                        if not self._uae.is_active:
                            await self._uae.start()
                        self.logger.info("[AgenticRunner] ✓ UAE (legacy)")
                    except Exception as e:
                        self.logger.debug(f"[AgenticRunner] ✗ UAE legacy: {e}")
                except Exception as e:
                    self.logger.debug(f"[AgenticRunner] ✗ UAE: {e}")

            # Initialize Neural Mesh (optional)
            if self._availability.get("neural_mesh") and self.config.neural_mesh_enabled:
                try:
                    # v9.4: Try production Neural Mesh first (with coordinator and bridge)
                    if self.config.neural_mesh_production:
                        try:
                            from neural_mesh.neural_mesh_coordinator import (
                                get_neural_mesh,
                                start_neural_mesh,
                                NeuralMeshCoordinator,
                            )
                            from neural_mesh.jarvis_bridge import (
                                get_jarvis_bridge,
                                start_jarvis_neural_mesh,
                                JARVISNeuralMeshBridge,
                            )

                            # Get or start coordinator
                            self._neural_mesh_coordinator = await get_neural_mesh()
                            if not self._neural_mesh_coordinator._running:
                                await self._neural_mesh_coordinator.start()

                            # Also use basic reference for compatibility
                            self._neural_mesh = self._neural_mesh_coordinator

                            self.logger.info("[AgenticRunner] ✓ Neural Mesh Coordinator (production)")

                            # Initialize JARVIS Bridge for cross-system tasks
                            if self.config.neural_mesh_use_bridge:
                                try:
                                    self._neural_mesh_bridge = await get_jarvis_bridge()
                                    if not self._neural_mesh_bridge.is_running:
                                        await self._neural_mesh_bridge.initialize()
                                        await self._neural_mesh_bridge.start()

                                    self._nm_production_active = True
                                    bridge_agents = len(self._neural_mesh_bridge.registered_agents)
                                    self.logger.info(f"[AgenticRunner] ✓ JARVIS Neural Mesh Bridge ({bridge_agents} agents)")
                                except Exception as bridge_error:
                                    self.logger.debug(f"[AgenticRunner] Bridge init skipped: {bridge_error}")

                        except ImportError as ie:
                            self.logger.debug(f"[AgenticRunner] Production Neural Mesh not available: {ie}")
                            # Fallback to basic start_neural_mesh
                            from neural_mesh.neural_mesh_coordinator import start_neural_mesh
                            self._neural_mesh = await start_neural_mesh()
                            self.logger.info("[AgenticRunner] ✓ Neural Mesh (basic)")
                    else:
                        # Basic Neural Mesh
                        from neural_mesh.neural_mesh_coordinator import start_neural_mesh
                        self._neural_mesh = await start_neural_mesh()
                        self.logger.info("[AgenticRunner] ✓ Neural Mesh")

                    # Deep Integration: Setup pattern subscription
                    if self.config.neural_mesh_deep_enabled:
                        await self._setup_neural_mesh_deep_integration()

                    # v9.4: Setup production integrations
                    if self._nm_production_active:
                        await self._setup_neural_mesh_production_integration()

                except Exception as e:
                    self.logger.debug(f"[AgenticRunner] ✗ Neural Mesh: {e}")

            # Initialize Autonomous Agent (optional)
            if self._availability.get("autonomous_agent"):
                try:
                    from autonomy.autonomous_agent import (
                        AutonomousAgent, AgentConfig, AgentMode, AgentPersonality
                    )
                    agent_config = AgentConfig(
                        mode=AgentMode.SUPERVISED,
                        personality=AgentPersonality.HELPFUL,
                    )
                    self._autonomous_agent = AutonomousAgent(config=agent_config)
                    await self._autonomous_agent.initialize()
                    self.logger.info("[AgenticRunner] ✓ Autonomous Agent")
                except Exception as e:
                    self.logger.debug(f"[AgenticRunner] ✗ Autonomous Agent: {e}")

            # Initialize Watchdog if not provided externally
            if not self._watchdog and self._availability.get("watchdog") and self.config.watchdog_integration:
                try:
                    from core.agentic_watchdog import start_watchdog
                    self._watchdog = await start_watchdog(tts_callback=self.tts_callback)
                    self._watchdog.on_kill(self._on_watchdog_kill)
                    self.logger.info("[AgenticRunner] ✓ Watchdog (internal)")
                except Exception as e:
                    self.logger.debug(f"[AgenticRunner] ✗ Watchdog: {e}")

            # Initialize Voice Authentication Layer (v5.0)
            if self._availability.get("voice_auth_layer") and self.config.voice_auth_enabled:
                try:
                    from core.voice_authentication_layer import start_voice_auth_layer
                    self._voice_auth_layer = await start_voice_auth_layer(
                        watchdog=self._watchdog,
                        tts_callback=self.tts_callback,
                    )
                    self.logger.info("[AgenticRunner] ✓ Voice Auth Layer")
                except Exception as e:
                    self.logger.debug(f"[AgenticRunner] ✗ Voice Auth Layer: {e}")

            # =================================================================
            # Initialize Autonomy Components (v6.0)
            # =================================================================

            # Initialize Tool Registry (before phase manager - tools needed for phases)
            if self._availability.get("tool_registry") and self.config.tool_registry_enabled:
                try:
                    from autonomy.unified_tool_registry import start_tool_registry
                    self._tool_registry = await start_tool_registry()
                    self.logger.info("[AgenticRunner] ✓ Tool Registry")
                except Exception as e:
                    self.logger.debug(f"[AgenticRunner] ✗ Tool Registry: {e}")

            # Initialize Memory Manager (before phase manager - memory for context)
            if self._availability.get("memory_manager") and self.config.memory_manager_enabled:
                try:
                    from autonomy.unified_memory_manager import start_memory_manager
                    self._memory_manager = await start_memory_manager()
                    self.logger.info("[AgenticRunner] ✓ Memory Manager")
                except Exception as e:
                    self.logger.debug(f"[AgenticRunner] ✗ Memory Manager: {e}")

            # Initialize Phase Manager (core execution orchestration)
            if self._availability.get("phase_manager") and self.config.phase_manager_enabled:
                try:
                    from autonomy.langgraph_phase_manager import start_phase_manager
                    self._phase_manager = await start_phase_manager()
                    self.logger.info("[AgenticRunner] ✓ Phase Manager")
                except Exception as e:
                    self.logger.debug(f"[AgenticRunner] ✗ Phase Manager: {e}")

            # Initialize Error Recovery Orchestrator (wraps execution)
            if self._availability.get("error_recovery") and self.config.error_recovery_enabled:
                try:
                    from autonomy.error_recovery_orchestrator import start_error_recovery
                    self._error_recovery = await start_error_recovery()
                    # Register component reset handlers
                    if self._error_recovery:
                        self._register_error_recovery_handlers()
                    self.logger.info("[AgenticRunner] ✓ Error Recovery")
                except Exception as e:
                    self.logger.debug(f"[AgenticRunner] ✗ Error Recovery: {e}")

            # Initialize UAE Context Manager (continuous screen monitoring)
            if self._availability.get("uae_context") and self.config.uae_context_enabled:
                try:
                    from autonomy.uae_context_manager import start_uae_context
                    self._uae_context = await start_uae_context()
                    self.logger.info("[AgenticRunner] ✓ UAE Context Manager")
                except Exception as e:
                    self.logger.debug(f"[AgenticRunner] ✗ UAE Context Manager: {e}")

            # Initialize Intervention Orchestrator (proactive assistance)
            if self._availability.get("intervention") and self.config.intervention_enabled:
                try:
                    from autonomy.intervention_orchestrator import start_intervention_orchestrator
                    self._intervention = await start_intervention_orchestrator(
                        tts_callback=self.tts_callback
                    )
                    self.logger.info("[AgenticRunner] ✓ Intervention Orchestrator")
                except Exception as e:
                    self.logger.debug(f"[AgenticRunner] ✗ Intervention Orchestrator: {e}")

            # Initialize JARVIS Prime Client (Tier-0 Brain)
            if self.config.jarvis_prime_enabled:
                try:
                    await self._initialize_jarvis_prime_client()
                    self.logger.info("[AgenticRunner] ✓ JARVIS Prime Client")
                except Exception as e:
                    self.logger.debug(f"[AgenticRunner] ✗ JARVIS Prime Client: {e}")

            # =================================================================
            # Initialize Reactor-Core Client (v10.0 - "Ignition Key")
            # =================================================================
            if self.config.reactor_core_enabled:
                try:
                    from backend.clients.reactor_core_client import (
                        ReactorCoreClient,
                        ReactorCoreConfig,
                    )
                    reactor_config = ReactorCoreConfig(
                        api_url=self.config.reactor_core_url,
                        auto_trigger_enabled=self.config.reactor_core_auto_trigger,
                        experience_threshold=self.config.reactor_core_experience_threshold,
                        min_trigger_interval_hours=self.config.reactor_core_min_trigger_interval_hours,
                    )
                    self._reactor_core_client = ReactorCoreClient(reactor_config)
                    await self._reactor_core_client.initialize()

                    # Register event callbacks
                    self._reactor_core_client.on_training_completed(self._on_training_completed)
                    self._reactor_core_client.on_training_failed(self._on_training_failed)

                    if self._reactor_core_client.is_online:
                        self.logger.info("[AgenticRunner] ✓ Reactor-Core Client (ONLINE)")
                    else:
                        self.logger.info("[AgenticRunner] ✓ Reactor-Core Client (offline - will retry)")
                except Exception as e:
                    self.logger.warning(f"[AgenticRunner] ✗ Reactor-Core Client: {e}")
                    self._reactor_core_client = None

            # Verify we have at least one execution capability
            if not self._computer_use_tool and not self._computer_use_connector:
                self.logger.error("[AgenticRunner] No execution capability available!")
                return False

            self._initialized = True
            self.logger.info("[AgenticRunner] Initialization complete")
            self._log_component_summary()
            return True

        except Exception as e:
            self.logger.error(f"[AgenticRunner] Initialization failed: {e}")
            return False

    def _register_error_recovery_handlers(self):
        """Register component reset handlers for error recovery."""
        if not self._error_recovery:
            return

        try:
            # Computer Use Tool reset handler
            async def reset_computer_use():
                if self._computer_use_tool:
                    self.logger.info("[ErrorRecovery] Resetting Computer Use Tool")
                    if hasattr(self._computer_use_tool, 'reset'):
                        await self._computer_use_tool.reset()
                    return True
                return False

            # UAE reset handler
            async def reset_uae():
                if self._uae:
                    self.logger.info("[ErrorRecovery] Resetting UAE")
                    if hasattr(self._uae, 'reset') or hasattr(self._uae, 'restart'):
                        reset_fn = getattr(self._uae, 'reset', None) or getattr(self._uae, 'restart', None)
                        await reset_fn()
                    return True
                return False

            # Neural Mesh connection reset handler
            async def reset_neural_mesh_connection():
                if self._neural_mesh:
                    self.logger.info("[ErrorRecovery] Resetting Neural Mesh connection")
                    if hasattr(self._neural_mesh, 'bus') and hasattr(self._neural_mesh.bus, 'reconnect'):
                        await self._neural_mesh.bus.reconnect()
                    return True
                return False

            # Phase Manager state reset handler
            async def reset_phase_manager():
                if self._phase_manager:
                    self.logger.info("[ErrorRecovery] Resetting Phase Manager state")
                    if hasattr(self._phase_manager, 'reset_state'):
                        await self._phase_manager.reset_state()
                    self._phase_execution_active = False
                    self._current_phase = None
                    self._phase_checkpoints.clear()
                    return True
                return False

            # Memory Manager cache flush handler
            async def flush_memory_cache():
                if self._memory_manager:
                    self.logger.info("[ErrorRecovery] Flushing Memory Manager cache")
                    if hasattr(self._memory_manager, 'flush_working_memory'):
                        await self._memory_manager.flush_working_memory()
                    return True
                return False

            # Register all handlers
            self._error_recovery.register_reset_handler("computer_use", reset_computer_use)
            self._error_recovery.register_reset_handler("uae", reset_uae)
            self._error_recovery.register_reset_handler("neural_mesh", reset_neural_mesh_connection)
            self._error_recovery.register_reset_handler("phase_manager", reset_phase_manager)
            self._error_recovery.register_reset_handler("memory_cache", flush_memory_cache)

            self.logger.debug("[AgenticRunner] Error recovery handlers registered")

        except Exception as e:
            self.logger.debug(f"[AgenticRunner] Error recovery handler registration failed: {e}")

    async def _initialize_jarvis_prime_client(self):
        """Initialize the JARVIS Prime Tier-0 Brain client."""
        try:
            import aiohttp

            # Determine the URL based on configuration
            if self.config.jarvis_prime_use_cloud_run and self.config.jarvis_prime_cloud_run_url:
                prime_url = self.config.jarvis_prime_cloud_run_url
            else:
                prime_url = self.config.jarvis_prime_url

            # Create an aiohttp session for JARVIS Prime communication
            self._jarvis_prime_client = {
                "url": prime_url,
                "session": aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=30),
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": "JARVIS-AgenticRunner/6.0",
                    }
                ),
                "connected": False,
                "last_health_check": None,
            }

            # Test connection with health check
            try:
                async with self._jarvis_prime_client["session"].get(
                    f"{prime_url}/health",
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as response:
                    if response.status == 200:
                        self._jarvis_prime_client["connected"] = True
                        self._jarvis_prime_client["last_health_check"] = time.time()
                        self.logger.info(f"[AgenticRunner] JARVIS Prime connected at {prime_url}")
                    else:
                        self.logger.debug(f"[AgenticRunner] JARVIS Prime health check returned {response.status}")
            except Exception as health_error:
                self.logger.debug(f"[AgenticRunner] JARVIS Prime not available: {health_error}")
                # Keep client for lazy connection attempts

        except ImportError:
            self.logger.debug("[AgenticRunner] aiohttp not available for JARVIS Prime client")
        except Exception as e:
            self.logger.debug(f"[AgenticRunner] JARVIS Prime client init failed: {e}")

    # =========================================================================
    # Reactor-Core Integration (v10.0 - "Ignition Key")
    # =========================================================================

    async def _record_experience_and_check_training(
        self,
        goal: str,
        result: "AgenticTaskResult",
    ) -> None:
        """
        Record task experience and check if training should be triggered.

        This is the "Ignition Key" - the critical connection between JARVIS
        task execution and the Reactor-Core training pipeline.

        Args:
            goal: The task goal that was executed
            result: The task execution result
        """
        if not self._reactor_core_client:
            return

        try:
            # Increment experience count
            self._experience_count += 1

            # Create experience record
            experience = {
                "goal": goal,
                "success": result.success,
                "mode": result.mode,
                "execution_time_ms": result.execution_time_ms,
                "actions_count": result.actions_count,
                "reasoning_steps": result.reasoning_steps,
                "learning_insights": result.learning_insights,
                "timestamp": datetime.now().isoformat(),
            }

            # Stream experience to Reactor-Core (if online)
            if self._reactor_core_client.is_online:
                streamed = await self._reactor_core_client.stream_experience(experience)
                if streamed:
                    self.logger.debug(f"[ReactorCore] Experience streamed (total: {self._experience_count})")

            # Check if we should trigger training
            await self._check_and_trigger_training()

        except Exception as e:
            # Never crash JARVIS for training issues
            self.logger.debug(f"[ReactorCore] Experience record error: {e}")

    async def _check_and_trigger_training(self) -> None:
        """
        Check if training should be triggered based on experience count.

        Training is triggered when:
        1. Experience count >= threshold
        2. Minimum interval since last trigger has passed
        3. Reactor-Core is online
        4. Auto-trigger is enabled
        """
        if not self._reactor_core_client:
            return

        if not self.config.reactor_core_auto_trigger:
            return

        if not self._reactor_core_client.is_online:
            return

        threshold = self.config.reactor_core_experience_threshold

        if self._experience_count >= threshold:
            try:
                from backend.clients.reactor_core_client import TrainingPriority

                # Determine priority based on experience count
                if self._experience_count >= threshold * 2:
                    priority = TrainingPriority.HIGH
                elif self._experience_count >= threshold * 1.5:
                    priority = TrainingPriority.NORMAL
                else:
                    priority = TrainingPriority.LOW

                job = await self._reactor_core_client.trigger_training(
                    experience_count=self._experience_count,
                    priority=priority,
                )

                if job:
                    self.logger.info(
                        f"[ReactorCore] Training triggered: job_id={job.job_id}, "
                        f"experiences={self._experience_count}"
                    )
                    # Reset counter after successful trigger
                    self._experience_count = 0

                    # Announce training trigger
                    if self.tts_callback and self.config.narrate_by_default:
                        await self.tts_callback(
                            f"Training pipeline triggered with {job.experience_count} experiences"
                        )

            except Exception as e:
                self.logger.debug(f"[ReactorCore] Training trigger error: {e}")

    async def _on_training_completed(self, data: Optional[Dict[str, Any]]) -> None:
        """
        Callback when training completes in Reactor-Core.

        Phase 2: Automatically hot-swap JARVIS Prime to the new model.
        """
        if not data:
            return

        job_id = data.get('job_id', 'unknown')
        metrics = data.get('metrics', {})

        self.logger.info(
            f"[ReactorCore] Training completed: job_id={job_id}, "
            f"metrics={metrics}"
        )

        # Announce completion
        if self.tts_callback and self.config.narrate_by_default:
            await self.tts_callback("Training pipeline completed successfully")

        # ===================================================================
        # Phase 2: Hot-Swap to new model
        # ===================================================================
        await self._auto_swap_jarvis_prime_model(data)

    async def _on_training_failed(self, data: Optional[Dict[str, Any]]) -> None:
        """Callback when training fails in Reactor-Core."""
        if not data:
            return

        error = data.get("error", "unknown error")
        self.logger.warning(f"[ReactorCore] Training failed: {error}")

        # Announce failure (optional - may be noisy)
        if self.tts_callback and self.config.narrate_by_default:
            await self.tts_callback("Training pipeline encountered an error")

    async def _auto_swap_jarvis_prime_model(self, training_data: Dict[str, Any]) -> None:
        """
        Phase 2: Automatically hot-swap JARVIS Prime to the newly trained model.

        This method:
        1. Extracts the output model path from training results
        2. Checks if JARVIS Prime is healthy
        3. Triggers the hot-swap via /model/swap endpoint
        4. Logs the result and announces via TTS

        Args:
            training_data: Training completion data from Reactor-Core
        """
        import os
        from pathlib import Path

        # Check if auto-swap is enabled
        auto_swap_enabled = os.getenv("JARVIS_PRIME_AUTO_SWAP", "true").lower() == "true"
        if not auto_swap_enabled:
            self.logger.info("[HotSwap] Auto-swap disabled via JARVIS_PRIME_AUTO_SWAP=false")
            return

        # Check if Reactor-Core client is available
        if not self._reactor_core_client:
            self.logger.warning("[HotSwap] Reactor-Core client not initialized")
            return

        # Check if JARVIS Prime is healthy before attempting swap
        prime_healthy = await self._reactor_core_client.check_jarvis_prime_health()
        if not prime_healthy:
            self.logger.warning(
                "[HotSwap] JARVIS Prime is not healthy - skipping auto-swap. "
                "Model can be swapped manually when Prime is back online."
            )
            return

        # Get the output model path from training data
        # The model path can come from:
        # 1. training_data['metrics']['output_model_path'] - if Reactor-Core provides it
        # 2. Environment variable REACTOR_CORE_OUTPUT_MODEL_DIR + job_id
        # 3. Default path pattern
        model_path = self._resolve_output_model_path(training_data)

        if not model_path:
            self.logger.warning(
                "[HotSwap] Could not determine output model path from training data. "
                "Set REACTOR_CORE_OUTPUT_MODEL_PATH or ensure Reactor-Core provides it."
            )
            return

        if not Path(model_path).exists():
            self.logger.warning(
                f"[HotSwap] Output model file not found: {model_path}. "
                "Training may still be exporting or path is incorrect."
            )
            return

        # Generate version ID from training job
        job_id = training_data.get('job_id', 'unknown')
        version_id = f"v{job_id}-trained"

        self.logger.info(f"[HotSwap] Initiating hot-swap to {model_path} (version: {version_id})")

        # Announce swap start
        if self.tts_callback and self.config.narrate_by_default:
            await self.tts_callback("Deploying new brain model. Stand by for hot swap.")

        try:
            # Perform the hot-swap
            result = await self._reactor_core_client.swap_jarvis_prime_model(
                model_path=model_path,
                version_id=version_id,
                force=False,
            )

            if result.get('success', False):
                old_version = result.get('old_version', 'unknown')
                new_version = result.get('new_version', version_id)
                duration = result.get('duration_seconds', 0)
                memory_freed = result.get('memory_freed_mb', 0)

                self.logger.info(
                    f"[HotSwap] SUCCESS: {old_version} → {new_version} "
                    f"({duration:.2f}s, freed {memory_freed:.0f}MB)"
                )

                # Announce success
                if self.tts_callback and self.config.narrate_by_default:
                    await self.tts_callback(
                        f"Brain upgrade complete. New model deployed in {duration:.1f} seconds."
                    )

                # Write event to cross-repo bridge
                await self._write_hot_swap_event("swap_completed", {
                    "old_version": old_version,
                    "new_version": new_version,
                    "model_path": model_path,
                    "duration_seconds": duration,
                    "memory_freed_mb": memory_freed,
                    "training_job_id": job_id,
                })

            else:
                error_msg = result.get('error_message', 'Unknown error')
                self.logger.error(f"[HotSwap] FAILED: {error_msg}")

                # Announce failure
                if self.tts_callback and self.config.narrate_by_default:
                    await self.tts_callback("Brain upgrade failed. Continuing with current model.")

                # Write event
                await self._write_hot_swap_event("swap_failed", {
                    "error": error_msg,
                    "model_path": model_path,
                    "training_job_id": job_id,
                })

        except Exception as e:
            self.logger.error(f"[HotSwap] Exception during swap: {e}")
            if self.tts_callback and self.config.narrate_by_default:
                await self.tts_callback("Brain upgrade encountered an error.")

    def _resolve_output_model_path(self, training_data: Dict[str, Any]) -> Optional[str]:
        """
        Resolve the output model path from training data.

        Checks multiple sources in order of priority:
        1. Explicit path in training metrics
        2. Environment variable
        3. Default pattern based on job ID

        Args:
            training_data: Training completion data

        Returns:
            Absolute path to the model file, or None if not found
        """
        import os
        from pathlib import Path

        # Priority 1: Explicit path from training metrics
        metrics = training_data.get('metrics', {})
        if 'output_model_path' in metrics:
            return metrics['output_model_path']

        # Priority 2: Check environment for output directory
        output_dir = os.getenv('REACTOR_CORE_OUTPUT_MODEL_DIR')
        if output_dir:
            job_id = training_data.get('job_id', 'latest')
            # Look for most recent .gguf file in output directory
            output_path = Path(output_dir)
            if output_path.exists():
                gguf_files = list(output_path.glob('*.gguf'))
                if gguf_files:
                    # Return most recently modified
                    return str(max(gguf_files, key=lambda p: p.stat().st_mtime))

        # Priority 3: Check default reactor-core output path
        default_paths = [
            Path.home() / ".jarvis" / "models" / "trained",
            Path.home() / ".reactor-core" / "output" / "models",
            Path("/tmp/reactor-core/output"),
        ]

        for default_path in default_paths:
            if default_path.exists():
                gguf_files = list(default_path.glob('*.gguf'))
                if gguf_files:
                    return str(max(gguf_files, key=lambda p: p.stat().st_mtime))

        return None

    async def _write_hot_swap_event(self, event_type: str, data: Dict[str, Any]) -> None:
        """Write hot-swap event to cross-repo bridge."""
        import json
        import uuid
        from datetime import datetime
        from pathlib import Path

        try:
            bridge_dir = Path.home() / ".jarvis" / "cross_repo" / "hot_swap_events"
            bridge_dir.mkdir(parents=True, exist_ok=True)

            event = {
                "event_id": str(uuid.uuid4())[:8],
                "event_type": event_type,
                "source": "jarvis_agentic_runner",
                "timestamp": datetime.now().isoformat(),
                "payload": data,
            }

            filename = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{event['event_id']}.json"
            filepath = bridge_dir / filename

            with open(filepath, "w") as f:
                json.dump(event, f, indent=2)

        except Exception as e:
            self.logger.debug(f"[HotSwap] Bridge event write error: {e}")

    # =========================================================================
    # Learning Goal Auto-Discovery (v10.1 - Proactive Learning from Failures)
    # =========================================================================

    async def _extract_and_submit_learning_goals(
        self,
        goal: str,
        result: "AgenticTaskResult",
    ) -> None:
        """
        Extract learning topics from failed task and submit to Safe Scout.

        This enables JARVIS to proactively learn from failures by:
        1. Analyzing the failed goal and error message
        2. Extracting relevant learning topics
        3. Submitting them to the learning goals manager
        4. Eventually triggering Safe Scout scraping and training

        Args:
            goal: The failed task goal
            result: The task result with error details
        """
        import os

        # Check if learning goal auto-discovery is enabled
        if not os.getenv("JARVIS_AUTO_LEARN_FROM_FAILURES", "true").lower() == "true":
            return

        try:
            error_text = result.error or ""
            final_message = result.final_message or ""

            # Extract learning topics from the failure
            learning_topics = self._extract_learning_topics_from_failure(
                goal=goal,
                error=error_text,
                final_message=final_message,
            )

            if not learning_topics:
                self.logger.debug("[LearningGoals] No learning topics extracted from failure")
                return

            # Try to submit to learning goals manager
            submitted = await self._submit_learning_goals(learning_topics, goal, error_text)

            if submitted > 0:
                self.logger.info(
                    f"[LearningGoals] Auto-discovered {submitted} learning topic(s) from failure: "
                    f"{[t['topic'] for t in learning_topics[:3]]}"
                )

                # Announce if TTS is available
                if self.tts_callback and self.config.narrate_by_default:
                    await self.tts_callback(
                        f"I've identified {submitted} learning topic{'s' if submitted > 1 else ''} "
                        f"from this failure. Adding to my study queue."
                    )

        except Exception as e:
            self.logger.debug(f"[LearningGoals] Learning goal extraction failed: {e}")

    def _extract_learning_topics_from_failure(
        self,
        goal: str,
        error: str,
        final_message: str,
    ) -> List[Dict[str, Any]]:
        """
        Extract learning topics from failure context using intelligent keyword analysis.

        Uses multiple strategies:
        1. Technology keyword matching (React, Python, Docker, etc.)
        2. Error pattern recognition
        3. Goal phrase extraction
        4. Context-aware topic generation

        Args:
            goal: The failed task goal
            error: The error message
            final_message: The final result message

        Returns:
            List of learning topic dictionaries with topic, category, priority
        """
        topics = []
        combined_text = f"{goal} {error} {final_message}".lower()

        # Technology keywords with their learning topics and categories
        tech_keywords = {
            # Programming Languages
            "python": {"topic": "Python programming", "category": "programming", "priority": 8},
            "javascript": {"topic": "JavaScript", "category": "programming", "priority": 8},
            "typescript": {"topic": "TypeScript", "category": "programming", "priority": 8},
            "rust": {"topic": "Rust programming", "category": "programming", "priority": 7},
            "go": {"topic": "Go programming", "category": "programming", "priority": 7},

            # Frameworks
            "react": {"topic": "React.js", "category": "frontend", "priority": 8},
            "vue": {"topic": "Vue.js", "category": "frontend", "priority": 7},
            "angular": {"topic": "Angular", "category": "frontend", "priority": 7},
            "fastapi": {"topic": "FastAPI", "category": "backend", "priority": 8},
            "django": {"topic": "Django", "category": "backend", "priority": 7},
            "flask": {"topic": "Flask", "category": "backend", "priority": 7},
            "nextjs": {"topic": "Next.js", "category": "frontend", "priority": 8},

            # Infrastructure
            "docker": {"topic": "Docker containerization", "category": "devops", "priority": 8},
            "kubernetes": {"topic": "Kubernetes orchestration", "category": "devops", "priority": 7},
            "terraform": {"topic": "Terraform infrastructure", "category": "devops", "priority": 7},
            "gcp": {"topic": "Google Cloud Platform", "category": "cloud", "priority": 8},
            "aws": {"topic": "Amazon Web Services", "category": "cloud", "priority": 7},
            "azure": {"topic": "Microsoft Azure", "category": "cloud", "priority": 7},

            # Databases
            "sql": {"topic": "SQL databases", "category": "database", "priority": 8},
            "postgresql": {"topic": "PostgreSQL", "category": "database", "priority": 8},
            "mysql": {"topic": "MySQL", "category": "database", "priority": 7},
            "mongodb": {"topic": "MongoDB", "category": "database", "priority": 7},
            "redis": {"topic": "Redis caching", "category": "database", "priority": 7},
            "chromadb": {"topic": "ChromaDB vector database", "category": "database", "priority": 8},

            # AI/ML
            "llm": {"topic": "Large Language Models", "category": "ai", "priority": 9},
            "machine learning": {"topic": "Machine Learning", "category": "ai", "priority": 8},
            "neural network": {"topic": "Neural Networks", "category": "ai", "priority": 8},
            "transformer": {"topic": "Transformer models", "category": "ai", "priority": 8},
            "fine-tuning": {"topic": "LLM fine-tuning", "category": "ai", "priority": 9},
            "embeddings": {"topic": "Vector embeddings", "category": "ai", "priority": 8},

            # APIs
            "api": {"topic": "API design", "category": "backend", "priority": 7},
            "rest": {"topic": "REST API design", "category": "backend", "priority": 7},
            "graphql": {"topic": "GraphQL", "category": "backend", "priority": 7},
            "websocket": {"topic": "WebSocket programming", "category": "backend", "priority": 7},

            # Other
            "git": {"topic": "Git version control", "category": "tools", "priority": 6},
            "testing": {"topic": "Software testing", "category": "quality", "priority": 7},
            "debugging": {"topic": "Debugging techniques", "category": "quality", "priority": 7},
            "security": {"topic": "Application security", "category": "security", "priority": 8},
            "authentication": {"topic": "Authentication systems", "category": "security", "priority": 8},
        }

        # Check for technology keywords
        for keyword, topic_info in tech_keywords.items():
            if keyword in combined_text:
                # Avoid duplicates
                if not any(t["topic"] == topic_info["topic"] for t in topics):
                    topics.append({
                        "topic": topic_info["topic"],
                        "category": topic_info["category"],
                        "priority": topic_info["priority"],
                        "source": "keyword_match",
                        "matched_keyword": keyword,
                    })

        # Error pattern recognition
        error_patterns = {
            "permission denied": {"topic": "File permissions and access control", "category": "systems", "priority": 6},
            "connection refused": {"topic": "Network troubleshooting", "category": "networking", "priority": 7},
            "timeout": {"topic": "Timeout handling and async programming", "category": "programming", "priority": 7},
            "memory": {"topic": "Memory management", "category": "systems", "priority": 7},
            "import error": {"topic": "Python module management", "category": "programming", "priority": 6},
            "module not found": {"topic": "Python dependency management", "category": "programming", "priority": 6},
            "syntax error": {"topic": "Code syntax and linting", "category": "programming", "priority": 5},
            "type error": {"topic": "Type systems and type checking", "category": "programming", "priority": 6},
            "null pointer": {"topic": "Null safety and error handling", "category": "programming", "priority": 7},
            "rate limit": {"topic": "API rate limiting", "category": "backend", "priority": 7},
        }

        error_lower = error.lower()
        for pattern, topic_info in error_patterns.items():
            if pattern in error_lower:
                if not any(t["topic"] == topic_info["topic"] for t in topics):
                    topics.append({
                        "topic": topic_info["topic"],
                        "category": topic_info["category"],
                        "priority": topic_info["priority"],
                        "source": "error_pattern",
                        "matched_pattern": pattern,
                    })

        # Fallback: Extract key phrases from goal if no specific topics found
        if not topics and len(goal.split()) >= 2:
            # Extract first meaningful phrase from goal
            words = [w for w in goal.split() if len(w) > 3]
            if words:
                fallback_topic = " ".join(words[:3])
                topics.append({
                    "topic": fallback_topic.capitalize(),
                    "category": "general",
                    "priority": 5,
                    "source": "goal_phrase",
                })

        # Limit to top 3 topics by priority
        topics.sort(key=lambda t: t.get("priority", 5), reverse=True)
        return topics[:3]

    async def _submit_learning_goals(
        self,
        topics: List[Dict[str, Any]],
        failed_goal: str,
        error: str,
    ) -> int:
        """
        Submit extracted learning topics to the learning goals system.

        Attempts multiple submission methods:
        1. Learning Goals Manager (if available)
        2. Reactor-Core Scout API (if available)
        3. Cross-repo bridge file (fallback)

        Args:
            topics: List of learning topic dictionaries
            failed_goal: The original failed goal
            error: The error message

        Returns:
            Number of topics successfully submitted
        """
        submitted = 0

        # Method 1: Try Learning Goals Manager
        try:
            from autonomy.learning_goals_discovery import get_learning_goals_manager

            manager = get_learning_goals_manager()
            if manager:
                for topic in topics:
                    success = await manager.add_learning_goal(
                        topic=topic["topic"],
                        source="task_failure_auto",
                        priority=topic.get("priority", 5),
                        context={
                            "failed_goal": failed_goal,
                            "error": error[:500],
                            "category": topic.get("category", "general"),
                            "timestamp": datetime.now().isoformat(),
                        }
                    )
                    if success:
                        submitted += 1

                if submitted > 0:
                    return submitted

        except ImportError:
            pass
        except Exception as e:
            self.logger.debug(f"[LearningGoals] Manager submission failed: {e}")

        # Method 2: Try Reactor-Core Scout API
        try:
            if self._reactor_core_client and self._reactor_core_client.is_online:
                for topic in topics:
                    success = await self._reactor_core_client.add_learning_topic(
                        topic=topic["topic"],
                        category=topic.get("category", "general"),
                        priority=topic.get("priority", 5),
                        added_by="jarvis_auto_learn",
                    )
                    if success:
                        submitted += 1

                if submitted > 0:
                    return submitted

        except Exception as e:
            self.logger.debug(f"[LearningGoals] Reactor-Core submission failed: {e}")

        # Method 3: Fallback to cross-repo bridge
        try:
            import json
            from pathlib import Path

            bridge_dir = Path.home() / ".jarvis" / "cross_repo" / "learning_goals"
            bridge_dir.mkdir(parents=True, exist_ok=True)

            for topic in topics:
                event = {
                    "event_id": str(uuid.uuid4())[:8],
                    "event_type": "learning_goal_discovered",
                    "source": "jarvis_auto_learn",
                    "timestamp": datetime.now().isoformat(),
                    "payload": {
                        "topic": topic["topic"],
                        "category": topic.get("category", "general"),
                        "priority": topic.get("priority", 5),
                        "failed_goal": failed_goal,
                        "error_snippet": error[:200],
                    },
                }

                filename = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{event['event_id']}.json"
                filepath = bridge_dir / filename

                with open(filepath, "w") as f:
                    json.dump(event, f, indent=2)

                submitted += 1

        except Exception as e:
            self.logger.debug(f"[LearningGoals] Bridge write failed: {e}")

        return submitted

    async def trigger_training_manual(
        self,
        priority: str = "normal",
        force: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """
        Manually trigger a training run.

        This can be called from external code or via voice command.

        Args:
            priority: Training priority (low, normal, high, urgent)
            force: Force trigger even if minimum interval hasn't passed

        Returns:
            Training job info or None if trigger failed
        """
        if not self._reactor_core_client:
            self.logger.warning("[ReactorCore] Client not initialized")
            return None

        if not self._reactor_core_client.is_online:
            self.logger.warning("[ReactorCore] Reactor-Core is offline")
            return None

        try:
            from backend.clients.reactor_core_client import TrainingPriority

            priority_enum = TrainingPriority(priority.lower())
            job = await self._reactor_core_client.trigger_training(
                experience_count=self._experience_count,
                priority=priority_enum,
                force=force,
            )

            if job:
                self._experience_count = 0  # Reset after trigger
                return job.to_dict()
            return None

        except Exception as e:
            self.logger.error(f"[ReactorCore] Manual trigger error: {e}")
            return None

    def get_reactor_core_status(self) -> Dict[str, Any]:
        """Get Reactor-Core client status."""
        if not self._reactor_core_client:
            return {"enabled": False, "initialized": False}

        return {
            "enabled": self.config.reactor_core_enabled,
            "initialized": self._reactor_core_client.is_initialized,
            "online": self._reactor_core_client.is_online,
            "pending_experiences": self._experience_count,
            "threshold": self.config.reactor_core_experience_threshold,
            "auto_trigger": self.config.reactor_core_auto_trigger,
            **self._reactor_core_client.get_metrics(),
        }

    def _log_component_summary(self):
        """Log a summary of initialized components."""
        core_components = []
        autonomy_components = []
        integrations = []

        # Core components
        if self._computer_use_tool:
            core_components.append("ComputerUseTool")
        if self._computer_use_connector:
            core_components.append("DirectConnector")
        if self._autonomous_agent:
            core_components.append("AutonomousAgent")
        if self._watchdog:
            core_components.append("Watchdog")

        # UAE and Neural Mesh
        if self._uae:
            core_components.append("UAE")
        if self._neural_mesh:
            core_components.append("NeuralMesh")
            if self._nm_pattern_subscription_active:
                core_components.append("NM-Patterns")
            if self._nm_agi_subscription_active:
                core_components.append("NM-AGI")

        # Voice Auth
        if self._voice_auth_layer:
            core_components.append("VoiceAuth")

        # Autonomy Components (v6.0)
        if self._phase_manager:
            autonomy_components.append("PhaseManager")
        if self._tool_registry:
            autonomy_components.append("ToolRegistry")
        if self._memory_manager:
            autonomy_components.append("MemoryManager")
        if self._error_recovery:
            autonomy_components.append("ErrorRecovery")
        if self._uae_context:
            autonomy_components.append("UAEContext")
        if self._intervention:
            autonomy_components.append("Intervention")

        # Integrations
        if self._jarvis_prime_client and self._jarvis_prime_client.get("connected"):
            integrations.append("JARVIS-Prime")
        if self._reactor_core_client:
            status = "online" if self._reactor_core_client.is_online else "offline"
            integrations.append(f"Reactor-Core({status})")

        # Log summary
        self.logger.info(
            f"[AgenticRunner] Components initialized:\n"
            f"  Core: {', '.join(core_components) or 'None'}\n"
            f"  Autonomy: {', '.join(autonomy_components) or 'None'}\n"
            f"  Integrations: {', '.join(integrations) or 'None'}"
        )

    # =========================================================================
    # Watchdog Integration
    # =========================================================================

    def set_watchdog(self, watchdog: Any) -> None:
        """Set the watchdog instance (called by supervisor)."""
        self._watchdog = watchdog
        if watchdog:
            watchdog.on_kill(self._on_watchdog_kill)
            self.logger.info("[AgenticRunner] Watchdog attached from supervisor")

    async def _on_watchdog_kill(self):
        """Called by watchdog when kill switch is triggered."""
        self.logger.warning("[AgenticRunner] Watchdog kill - stopping task")

        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None

        self._current_task_id = None

    async def _heartbeat_loop(self, goal: str, mode: str):
        """Emit heartbeats to watchdog during task execution."""
        if not self._watchdog:
            return

        try:
            from core.agentic_watchdog import Heartbeat, AgenticMode

            actions_count = 0
            while True:
                await asyncio.sleep(self.config.heartbeat_interval)

                if not self._current_task_id:
                    break

                heartbeat = Heartbeat(
                    task_id=self._current_task_id,
                    goal=goal,
                    current_action=f"Executing ({mode})",
                    actions_count=actions_count,
                    timestamp=time.time(),
                    mode=AgenticMode.AUTONOMOUS if mode == "autonomous" else AgenticMode.SUPERVISED,
                )

                self._watchdog.receive_heartbeat(heartbeat)
                actions_count += 1

        except asyncio.CancelledError:
            pass
        except ImportError:
            pass

    async def _start_watchdog_task(self, goal: str, mode: str):
        """Start watchdog monitoring for this task."""
        if not self._watchdog:
            return

        try:
            from core.agentic_watchdog import AgenticMode

            self._current_task_id = f"task_{int(time.time())}_{id(self)}"

            watchdog_mode = AgenticMode.AUTONOMOUS if mode == "autonomous" else AgenticMode.SUPERVISED
            await self._watchdog.task_started(
                task_id=self._current_task_id,
                goal=goal,
                mode=watchdog_mode
            )

            self._heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(goal, mode)
            )

            self.logger.debug(f"[AgenticRunner] Watchdog armed: {self._current_task_id}")
        except ImportError:
            pass

    async def _stop_watchdog_task(self, success: bool):
        """Stop watchdog monitoring for this task."""
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None

        if self._watchdog and self._current_task_id:
            await self._watchdog.task_completed(
                task_id=self._current_task_id,
                success=success
            )

        self._current_task_id = None

    # =========================================================================
    # Task Execution
    # =========================================================================

    async def run(
        self,
        goal: str,
        mode: Optional[RunnerMode] = None,
        context: Optional[Dict[str, Any]] = None,
        narrate: Optional[bool] = None,
    ) -> AgenticTaskResult:
        """
        Execute an agentic task.

        Args:
            goal: Natural language goal to achieve
            mode: Execution mode (defaults to config)
            context: Additional context
            narrate: Whether to enable voice narration

        Returns:
            AgenticTaskResult with execution details
        """
        if not self._initialized:
            await self.initialize()

        # Resolve defaults
        mode = mode or RunnerMode(self.config.default_mode)
        narrate = narrate if narrate is not None else self.config.narrate_by_default

        # Check watchdog permission
        if self._watchdog and not self._watchdog.is_agentic_allowed():
            return AgenticTaskResult(
                success=False,
                goal=goal,
                mode=mode.value,
                execution_time_ms=0,
                actions_count=0,
                reasoning_steps=0,
                final_message="Agentic execution blocked by watchdog safety system",
                error="Watchdog kill switch active or in cooldown",
                watchdog_status="blocked",
            )

        self._tasks_executed += 1
        start_time = time.time()

        self.logger.info(f"[AgenticRunner] Goal: {goal[:50]}...")
        self.logger.info(f"[AgenticRunner] Mode: {mode.value}")

        # Neural Mesh Deep Integration: Query context before execution
        neural_context = None
        if self.config.neural_mesh_deep_enabled and self._neural_mesh:
            try:
                neural_context = await self._query_neural_context(goal)
                # Enrich execution context with neural insights
                if context is None:
                    context = {}
                if neural_context.pattern_insights:
                    context["pattern_insights"] = neural_context.pattern_insights
                if neural_context.recommended_actions:
                    context["recommended_actions"] = neural_context.recommended_actions
                if neural_context.similar_goals:
                    context["similar_executions"] = len(neural_context.similar_goals)
            except Exception as e:
                self.logger.debug(f"Neural context query failed: {e}")

        # Announce start
        if narrate and self.tts_callback:
            await self.tts_callback(f"Starting task: {goal[:50]}")

        # Start watchdog monitoring
        await self._start_watchdog_task(goal, mode.value)

        # Neural Mesh Deep Integration: Publish task_started event
        if self.config.neural_mesh_deep_enabled:
            await self._publish_task_event(
                "task_started",
                goal,
                mode.value,
                {"context_score": neural_context.context_score if neural_context else 0}
            )

        try:
            # Execute based on mode
            if mode == RunnerMode.DIRECT:
                result = await self._execute_direct(goal, context, narrate)
            elif mode == RunnerMode.AUTONOMOUS:
                result = await self._execute_autonomous(goal, context, narrate)
            else:  # SUPERVISED
                result = await self._execute_supervised(goal, context, narrate)

            execution_time = (time.time() - start_time) * 1000
            result.execution_time_ms = execution_time
            result.mode = mode.value

            # Neural Mesh Deep Integration: Attach context and stats to result
            if neural_context:
                result.neural_mesh_context = neural_context
                result.pattern_insights_applied = neural_context.pattern_insights
            result.neural_mesh_events_sent = self._nm_events_sent

            if result.success:
                self._tasks_succeeded += 1

            # Stop watchdog monitoring
            await self._stop_watchdog_task(result.success)

            # Neural Mesh Deep Integration: Publish completion and record learning
            if self.config.neural_mesh_deep_enabled:
                event_type = "task_completed" if result.success else "task_failed"
                await self._publish_task_event(
                    event_type,
                    goal,
                    mode.value,
                    {
                        "execution_time_ms": execution_time,
                        "actions_count": result.actions_count,
                        "success": result.success,
                    }
                )
                # Record comprehensive learning
                contributions = await self._record_comprehensive_learning(goal, result, neural_context)
                result.knowledge_contributions = contributions

            # Announce completion
            if narrate and self.tts_callback:
                status = "completed successfully" if result.success else "encountered an issue"
                await self.tts_callback(f"Task {status}")

            # Reactor-Core Integration: Track experience and check training trigger (v10.0)
            if result.success and self.config.reactor_core_enabled:
                await self._record_experience_and_check_training(goal, result)

            # Learning Goal Auto-Discovery: Extract learning topics from failures (v10.1)
            if not result.success and result.error:
                await self._extract_and_submit_learning_goals(goal, result)

            self.logger.info(f"[AgenticRunner] Complete: success={result.success}, time={execution_time:.0f}ms")
            return result

        except asyncio.TimeoutError:
            await self._stop_watchdog_task(False)
            # Publish timeout event
            if self.config.neural_mesh_deep_enabled:
                await self._publish_task_event("task_failed", goal, mode.value, {"error": "timeout"})
            return AgenticTaskResult(
                success=False,
                goal=goal,
                mode=mode.value,
                execution_time_ms=(time.time() - start_time) * 1000,
                actions_count=0,
                reasoning_steps=0,
                final_message="Task timed out",
                error=f"Timeout after {self.config.task_timeout_seconds}s",
                neural_mesh_context=neural_context,
            )

        except Exception as e:
            self.logger.error(f"[AgenticRunner] Failed: {e}", exc_info=True)
            await self._stop_watchdog_task(False)
            # Publish error event
            if self.config.neural_mesh_deep_enabled:
                await self._publish_task_event("task_failed", goal, mode.value, {"error": str(e)})

            return AgenticTaskResult(
                success=False,
                goal=goal,
                mode=mode.value,
                execution_time_ms=(time.time() - start_time) * 1000,
                actions_count=0,
                reasoning_steps=0,
                final_message=f"Task failed: {str(e)}",
                error=str(e),
                neural_mesh_context=neural_context,
            )

    # =========================================================================
    # Execution Modes
    # =========================================================================

    async def _execute_direct(
        self,
        goal: str,
        context: Optional[Dict[str, Any]],
        narrate: bool,
    ) -> AgenticTaskResult:
        """Execute goal directly via Computer Use (skip reasoning)."""
        self.logger.debug("[AgenticRunner] DIRECT mode")

        context = context or {}

        # Add UAE context if available
        uae_used = False
        if self._uae:
            try:
                context["uae_active"] = True
                uae_used = True
            except Exception as e:
                self.logger.debug(f"UAE context error: {e}")

        # Use Computer Use Tool
        if self._computer_use_tool:
            result = await self._computer_use_tool.run(
                goal=goal,
                context=context,
                narrate=narrate,
            )
            return AgenticTaskResult(
                success=result.success,
                goal=goal,
                mode="direct",
                execution_time_ms=result.total_duration_ms,
                actions_count=result.actions_count,
                reasoning_steps=0,
                final_message=result.final_message,
                learning_insights=result.learning_insights if hasattr(result, 'learning_insights') else [],
                uae_used=uae_used,
                multi_space_used=hasattr(result, 'multi_space_context') and result.multi_space_context is not None,
            )

        # Fallback to direct connector
        if self._computer_use_connector:
            result = await self._computer_use_connector.execute_task(
                goal=goal,
                context=context,
                narrate=narrate,
            )
            # Handle different result structures
            success = getattr(result, 'success', False) or (hasattr(result, 'status') and str(result.status) == "SUCCESS")
            return AgenticTaskResult(
                success=success,
                goal=goal,
                mode="direct",
                execution_time_ms=getattr(result, 'total_duration_ms', 0),
                actions_count=len(getattr(result, 'actions_executed', [])),
                reasoning_steps=0,
                final_message=getattr(result, 'final_message', "Task completed"),
                learning_insights=getattr(result, 'learning_insights', []),
                uae_used=uae_used,
            )

        raise RuntimeError("No computer use capability available")

    async def _execute_autonomous(
        self,
        goal: str,
        context: Optional[Dict[str, Any]],
        narrate: bool,
    ) -> AgenticTaskResult:
        """
        Execute goal with full autonomous reasoning + phase-managed execution.

        Phase Flow (LangGraph-style):
        ANALYZING → PLANNING → EXECUTING → REFLECTING → LEARNING

        Each phase can:
        - Checkpoint state to memory
        - Query JARVIS Prime for muscle-memory patterns
        - Report progress to intervention orchestrator
        - Be recovered by error recovery orchestrator
        """
        self.logger.debug("[AgenticRunner] AUTONOMOUS mode (Phase-Managed)")

        context = context or {}
        reasoning_steps = 0
        phase_results: Dict[str, Any] = {}
        self._phase_execution_active = True
        self._phase_checkpoints.clear()

        try:
            # =================================================================
            # Phase 0: Context Enrichment
            # =================================================================
            self._current_phase = "CONTEXT_ENRICHMENT"
            context = await self._enrich_context(goal, context)

            # Query JARVIS Prime for muscle-memory patterns
            prime_patterns = await self._query_jarvis_prime(goal)
            if prime_patterns:
                context["jarvis_prime_patterns"] = prime_patterns
                self.logger.debug(f"[Phase-0] JARVIS Prime patterns: {len(prime_patterns)}")

            # =================================================================
            # Phase 1: ANALYZING
            # =================================================================
            self._current_phase = "ANALYZING"
            await self._checkpoint_phase("analyzing_start", {"goal": goal, "context_keys": list(context.keys())})

            analyze_result = await self._execute_with_error_recovery(
                self._phase_analyze,
                goal, context, narrate,
                phase_name="ANALYZING"
            )
            if analyze_result:
                phase_results["analyze"] = analyze_result
                reasoning_steps += analyze_result.get("reasoning_steps", 0)
                context.update(analyze_result.get("enriched_context", {}))

            await self._checkpoint_phase("analyzing_complete", analyze_result)

            # =================================================================
            # Phase 2: PLANNING
            # =================================================================
            self._current_phase = "PLANNING"
            await self._checkpoint_phase("planning_start", {"analyze_result": bool(analyze_result)})

            plan_result = await self._execute_with_error_recovery(
                self._phase_plan,
                goal, context, narrate,
                phase_name="PLANNING"
            )
            if plan_result:
                phase_results["plan"] = plan_result
                reasoning_steps += plan_result.get("reasoning_steps", 0)
                context["execution_plan"] = plan_result.get("plan", [])
                context["plan_confidence"] = plan_result.get("confidence", 0.5)

            await self._checkpoint_phase("planning_complete", plan_result)

            # Check for intervention opportunity
            await self._check_intervention_opportunity("pre_execution", goal, context)

            # =================================================================
            # Phase 3: EXECUTING
            # =================================================================
            self._current_phase = "EXECUTING"
            await self._checkpoint_phase("executing_start", {"plan_steps": len(context.get("execution_plan", []))})

            # Main execution with UAE context monitoring
            context["execution_mode"] = "autonomous"
            context["full_reasoning"] = True
            context["phase_managed"] = True

            # Start UAE context monitoring if available
            uae_context_task = None
            if self._uae_context:
                uae_context_task = asyncio.create_task(
                    self._monitor_uae_context_during_execution(goal)
                )

            execute_result = await self._execute_with_error_recovery(
                self._phase_execute,
                goal, context, narrate,
                phase_name="EXECUTING"
            )

            # Stop UAE monitoring
            if uae_context_task:
                uae_context_task.cancel()
                try:
                    await uae_context_task
                except asyncio.CancelledError:
                    pass

            if execute_result:
                phase_results["execute"] = execute_result

            await self._checkpoint_phase("executing_complete", {"success": execute_result is not None})

            # =================================================================
            # Phase 4: REFLECTING
            # =================================================================
            self._current_phase = "REFLECTING"
            await self._checkpoint_phase("reflecting_start", {})

            reflect_result = await self._execute_with_error_recovery(
                self._phase_reflect,
                goal, context, phase_results, execute_result,
                phase_name="REFLECTING"
            )
            if reflect_result:
                phase_results["reflect"] = reflect_result
                reasoning_steps += reflect_result.get("reasoning_steps", 0)

            await self._checkpoint_phase("reflecting_complete", reflect_result)

            # =================================================================
            # Phase 5: LEARNING
            # =================================================================
            self._current_phase = "LEARNING"
            if self.config.learning_enabled and execute_result and execute_result.success:
                await self._checkpoint_phase("learning_start", {})

                learn_result = await self._execute_with_error_recovery(
                    self._phase_learn,
                    goal, context, phase_results, execute_result,
                    phase_name="LEARNING"
                )
                if learn_result:
                    phase_results["learn"] = learn_result

                await self._checkpoint_phase("learning_complete", learn_result)

            # =================================================================
            # Build Final Result
            # =================================================================
            self._phase_execution_active = False
            self._current_phase = None

            if execute_result:
                execute_result.mode = "autonomous"
                execute_result.reasoning_steps = reasoning_steps
                execute_result.neural_mesh_used = self._neural_mesh is not None

                # Add phase metadata to learning insights
                if phase_results.get("reflect", {}).get("insights"):
                    execute_result.learning_insights.extend(
                        phase_results["reflect"]["insights"]
                    )

                return execute_result

            # Fallback if no execute_result
            return AgenticTaskResult(
                success=False,
                goal=goal,
                mode="autonomous",
                execution_time_ms=0,
                actions_count=0,
                reasoning_steps=reasoning_steps,
                final_message="Phase execution did not produce a result",
                error="No execution result from phase pipeline",
            )

        except Exception as e:
            self.logger.error(f"[AgenticRunner] Phase execution failed: {e}", exc_info=True)
            self._phase_execution_active = False
            self._current_phase = None

            # Record failure to memory for future learning
            if self._memory_manager:
                try:
                    await self._memory_manager.record_episode(
                        goal=goal,
                        success=False,
                        error=str(e),
                        phases_completed=list(phase_results.keys()),
                        timestamp=time.time(),
                    )
                except Exception:
                    pass

            return AgenticTaskResult(
                success=False,
                goal=goal,
                mode="autonomous",
                execution_time_ms=0,
                actions_count=0,
                reasoning_steps=reasoning_steps,
                final_message=f"Phase execution failed: {str(e)}",
                error=str(e),
            )

    # =========================================================================
    # Phase Handlers (v6.0)
    # =========================================================================

    async def _phase_analyze(
        self,
        goal: str,
        context: Dict[str, Any],
        narrate: bool,
    ) -> Optional[Dict[str, Any]]:
        """
        ANALYZING phase: Understand the goal and gather context.

        - Query memory for similar past executions
        - Analyze goal complexity and requirements
        - Identify required tools and capabilities
        """
        self.logger.debug("[Phase-ANALYZE] Starting goal analysis")
        result = {
            "reasoning_steps": 0,
            "enriched_context": {},
            "complexity": "medium",
            "required_tools": [],
        }

        # Query episodic memory for similar goals
        if self._memory_manager:
            try:
                similar = await self._memory_manager.find_similar_goals(goal, limit=3)
                if similar:
                    result["enriched_context"]["similar_executions"] = similar
                    # Extract successful patterns
                    successful = [s for s in similar if s.get("success")]
                    if successful:
                        result["enriched_context"]["successful_patterns"] = [
                            s.get("execution_pattern", {}) for s in successful
                        ]
                    result["reasoning_steps"] += 1
            except Exception as e:
                self.logger.debug(f"[Phase-ANALYZE] Memory query failed: {e}")

        # Use autonomous agent for deeper analysis if available
        if self._autonomous_agent:
            try:
                if hasattr(self._autonomous_agent, 'analyze_goal'):
                    analysis = await self._autonomous_agent.analyze_goal(goal, context)
                    if analysis:
                        result["reasoning_steps"] += analysis.get("reasoning_steps", 0)
                        result["complexity"] = analysis.get("complexity", "medium")
                        result["enriched_context"]["goal_analysis"] = analysis.get("analysis", "")
                        result["required_tools"] = analysis.get("required_tools", [])
            except Exception as e:
                self.logger.debug(f"[Phase-ANALYZE] Agent analysis failed: {e}")

        # Query tool registry for available tools
        if self._tool_registry:
            try:
                available_tools = await self._tool_registry.get_tools_for_goal(goal)
                result["enriched_context"]["available_tools"] = [
                    t.name for t in available_tools[:10]
                ]
            except Exception as e:
                self.logger.debug(f"[Phase-ANALYZE] Tool query failed: {e}")

        self.logger.debug(f"[Phase-ANALYZE] Complete: {result['reasoning_steps']} reasoning steps")
        return result

    async def _phase_plan(
        self,
        goal: str,
        context: Dict[str, Any],
        narrate: bool,
    ) -> Optional[Dict[str, Any]]:
        """
        PLANNING phase: Create execution plan based on analysis.

        - Generate step-by-step plan
        - Assign confidence scores
        - Identify potential failure points
        """
        self.logger.debug("[Phase-PLAN] Starting execution planning")
        result = {
            "reasoning_steps": 0,
            "plan": [],
            "confidence": 0.5,
            "risk_points": [],
        }

        # Use phase manager for LangGraph-style planning
        if self._phase_manager:
            try:
                plan_output = await self._phase_manager.create_plan(
                    goal=goal,
                    context=context,
                    available_tools=context.get("available_tools", []),
                )
                if plan_output:
                    result["plan"] = plan_output.get("steps", [])
                    result["confidence"] = plan_output.get("confidence", 0.5)
                    result["reasoning_steps"] += plan_output.get("reasoning_steps", 1)
                    result["risk_points"] = plan_output.get("risks", [])
            except Exception as e:
                self.logger.debug(f"[Phase-PLAN] Phase manager planning failed: {e}")

        # Fallback: Use autonomous agent for planning
        if not result["plan"] and self._autonomous_agent:
            try:
                if hasattr(self._autonomous_agent, 'create_plan'):
                    agent_plan = await self._autonomous_agent.create_plan(goal, context)
                    if agent_plan:
                        result["plan"] = agent_plan.get("plan", [])
                        result["confidence"] = agent_plan.get("confidence", 0.5)
                        result["reasoning_steps"] += 1
            except Exception as e:
                self.logger.debug(f"[Phase-PLAN] Agent planning failed: {e}")

        # Apply successful patterns from memory
        successful_patterns = context.get("successful_patterns", [])
        if successful_patterns and not result["plan"]:
            # Use patterns to bootstrap plan
            result["plan"] = successful_patterns[0].get("steps", [])
            result["confidence"] = 0.7  # Higher confidence from proven patterns
            result["reasoning_steps"] += 1

        self.logger.debug(f"[Phase-PLAN] Complete: {len(result['plan'])} steps, confidence={result['confidence']:.2f}")
        return result

    async def _phase_execute(
        self,
        goal: str,
        context: Dict[str, Any],
        narrate: bool,
    ) -> Optional[AgenticTaskResult]:
        """
        EXECUTING phase: Carry out the plan using Computer Use.

        - Execute actions via Computer Use Tool
        - Monitor progress
        - Handle interruptions
        """
        self.logger.debug("[Phase-EXECUTE] Starting execution")

        # Execute via the direct execution method
        return await self._execute_direct(goal, context, narrate)

    async def _phase_reflect(
        self,
        goal: str,
        context: Dict[str, Any],
        phase_results: Dict[str, Any],
        execute_result: Optional[AgenticTaskResult],
    ) -> Optional[Dict[str, Any]]:
        """
        REFLECTING phase: Analyze what happened and extract insights.

        - Compare outcome to expectations
        - Identify what worked and what didn't
        - Generate improvement suggestions
        """
        self.logger.debug("[Phase-REFLECT] Starting reflection")
        result = {
            "reasoning_steps": 0,
            "insights": [],
            "improvements": [],
            "success_factors": [],
        }

        if not execute_result:
            return result

        # Basic success/failure analysis
        if execute_result.success:
            result["insights"].append(f"Goal achieved in {execute_result.actions_count} actions")
            result["success_factors"].append("execution_complete")
        else:
            result["insights"].append(f"Execution failed: {execute_result.error or 'Unknown error'}")
            result["improvements"].append("Consider alternative approaches")

        # Analyze execution time
        plan = phase_results.get("plan", {})
        expected_steps = len(plan.get("plan", []))
        actual_actions = execute_result.actions_count

        if expected_steps > 0 and actual_actions > expected_steps * 2:
            result["insights"].append(f"Execution took {actual_actions} actions vs {expected_steps} planned")
            result["improvements"].append("Plan may need refinement for efficiency")

        # Use autonomous agent for deeper reflection
        if self._autonomous_agent and hasattr(self._autonomous_agent, 'reflect'):
            try:
                agent_reflection = await self._autonomous_agent.reflect(
                    goal=goal,
                    result=execute_result,
                    context=context,
                )
                if agent_reflection:
                    result["insights"].extend(agent_reflection.get("insights", []))
                    result["improvements"].extend(agent_reflection.get("improvements", []))
                    result["reasoning_steps"] += 1
            except Exception as e:
                self.logger.debug(f"[Phase-REFLECT] Agent reflection failed: {e}")

        result["reasoning_steps"] += 1
        self.logger.debug(f"[Phase-REFLECT] Complete: {len(result['insights'])} insights")
        return result

    async def _phase_learn(
        self,
        goal: str,
        context: Dict[str, Any],
        phase_results: Dict[str, Any],
        execute_result: Optional[AgenticTaskResult],
    ) -> Optional[Dict[str, Any]]:
        """
        LEARNING phase: Consolidate experience for future use.

        - Record to episodic memory
        - Update semantic patterns
        - Contribute to Neural Mesh knowledge graph
        """
        self.logger.debug("[Phase-LEARN] Starting learning consolidation")
        result = {
            "memory_recorded": False,
            "neural_mesh_contributed": False,
            "patterns_updated": 0,
        }

        if not execute_result:
            return result

        # Record to episodic memory
        if self._memory_manager:
            try:
                episode = {
                    "goal": goal,
                    "success": execute_result.success,
                    "actions_count": execute_result.actions_count,
                    "execution_time_ms": execute_result.execution_time_ms,
                    "error": execute_result.error,
                    "insights": phase_results.get("reflect", {}).get("insights", []),
                    "plan_used": context.get("execution_plan", []),
                    "timestamp": time.time(),
                }
                await self._memory_manager.record_episode(
                    category="task_execution",
                    data=episode,
                )
                result["memory_recorded"] = True
            except Exception as e:
                self.logger.debug(f"[Phase-LEARN] Memory recording failed: {e}")

        # Contribute to Neural Mesh knowledge graph
        if self._neural_mesh:
            try:
                await self._record_learning(goal, execute_result)
                result["neural_mesh_contributed"] = True
            except Exception as e:
                self.logger.debug(f"[Phase-LEARN] Neural Mesh contribution failed: {e}")

        # Update tool usage patterns
        if self._tool_registry and execute_result.success:
            try:
                tools_used = context.get("tools_executed", [])
                for tool in tools_used:
                    await self._tool_registry.record_tool_usage(
                        tool_name=tool,
                        goal=goal,
                        success=True,
                    )
                result["patterns_updated"] = len(tools_used)
            except Exception as e:
                self.logger.debug(f"[Phase-LEARN] Tool pattern update failed: {e}")

        self.logger.debug(f"[Phase-LEARN] Complete: memory={result['memory_recorded']}, nm={result['neural_mesh_contributed']}")
        return result

    # =========================================================================
    # Phase Support Methods (v6.0)
    # =========================================================================

    async def _enrich_context(
        self,
        goal: str,
        context: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Enrich execution context with available information."""
        enriched = dict(context)

        # UAE Context enrichment
        if self._uae_context:
            try:
                uae_snapshot = await self._uae_context.get_current_context()
                if uae_snapshot:
                    enriched["uae_context"] = uae_snapshot
                    enriched["screen_state"] = uae_snapshot.get("screen_state", {})
                    enriched["active_application"] = uae_snapshot.get("active_app", "unknown")
            except Exception as e:
                self.logger.debug(f"[Context] UAE enrichment failed: {e}")

        # Add pattern insights from Neural Mesh
        if self._nm_pattern_insights:
            enriched["pattern_insights"] = self._nm_pattern_insights[-5:]

        # Add experience replay context
        if self._experience_replay_cache:
            relevant = [
                exp for exp in self._experience_replay_cache
                if exp.get("goal_similarity", 0) > 0.7
            ][:3]
            if relevant:
                enriched["similar_experiences"] = relevant

        return enriched

    async def _query_jarvis_prime(self, goal: str) -> Optional[List[Dict[str, Any]]]:
        """Query JARVIS Prime for muscle-memory patterns."""
        if not self._jarvis_prime_client or not self._jarvis_prime_client.get("session"):
            return None

        try:
            session = self._jarvis_prime_client["session"]
            url = self._jarvis_prime_client["url"]

            async with session.post(
                f"{url}/v1/chat/completions",
                json={
                    "messages": [
                        {
                            "role": "system",
                            "content": "You are JARVIS Prime, providing muscle-memory patterns for efficient task execution.",
                        },
                        {
                            "role": "user",
                            "content": f"What are the fastest action patterns for: {goal[:200]}",
                        },
                    ],
                    "max_tokens": 200,
                    "temperature": 0.3,
                },
                timeout=aiohttp.ClientTimeout(total=5) if 'aiohttp' in dir() else None,
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                    if content:
                        return [{"pattern": content, "source": "jarvis_prime"}]
        except Exception as e:
            self.logger.debug(f"[JARVIS-Prime] Query failed: {e}")

        return None

    async def _checkpoint_phase(self, checkpoint_name: str, data: Any):
        """Save a phase checkpoint to memory."""
        checkpoint = {
            "name": checkpoint_name,
            "phase": self._current_phase,
            "timestamp": time.time(),
            "data": data,
        }
        self._phase_checkpoints.append(checkpoint)

        # Also persist to memory manager if available
        if self._memory_manager:
            try:
                await self._memory_manager.save_checkpoint(
                    task_id=self._current_task_id or "unknown",
                    checkpoint=checkpoint,
                )
            except Exception:
                pass

    async def _execute_with_error_recovery(
        self,
        phase_fn: Callable,
        *args,
        phase_name: str = "unknown",
        max_retries: int = 3,
        **kwargs,
    ) -> Any:
        """Execute a phase function with error recovery."""
        if self._error_recovery:
            try:
                return await self._error_recovery.execute_with_recovery(
                    operation=lambda: phase_fn(*args, **kwargs),
                    operation_name=f"phase_{phase_name}",
                    max_retries=max_retries,
                )
            except Exception as e:
                self.logger.debug(f"[ErrorRecovery] {phase_name} failed after retries: {e}")
                return None

        # Fallback: Direct execution without recovery
        try:
            return await phase_fn(*args, **kwargs)
        except Exception as e:
            self.logger.debug(f"[Phase-{phase_name}] Failed: {e}")
            return None

    async def _check_intervention_opportunity(
        self,
        stage: str,
        goal: str,
        context: Dict[str, Any],
    ):
        """Check if intervention orchestrator should assist."""
        if not self._intervention:
            return

        try:
            should_intervene = await self._intervention.should_intervene(
                stage=stage,
                goal=goal,
                context=context,
            )
            if should_intervene:
                self._active_interventions += 1
                intervention = await self._intervention.provide_assistance(
                    stage=stage,
                    goal=goal,
                    context=context,
                )
                if intervention:
                    self.logger.info(f"[Intervention] {stage}: {intervention.get('message', 'Assistance provided')}")
        except Exception as e:
            self.logger.debug(f"[Intervention] Check failed: {e}")

    async def _monitor_uae_context_during_execution(self, goal: str):
        """Monitor UAE context changes during execution."""
        if not self._uae_context:
            return

        try:
            while True:
                await asyncio.sleep(2.0)  # Check every 2 seconds

                # Get current context
                current = await self._uae_context.get_current_context()
                if current:
                    # Check for significant changes
                    change_detected = current.get("significant_change", False)
                    if change_detected:
                        self.logger.debug(f"[UAE-Monitor] Significant context change detected")
                        # Could trigger adaptive behavior here

        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.logger.debug(f"[UAE-Monitor] Error: {e}")

    async def _execute_supervised(
        self,
        goal: str,
        context: Optional[Dict[str, Any]],
        narrate: bool,
    ) -> AgenticTaskResult:
        """Execute goal with supervision (may request confirmation)."""
        self.logger.debug("[AgenticRunner] SUPERVISED mode")

        # For now, supervised behaves like direct with logging
        # In full implementation, this would pause for user confirmation
        return await self._execute_direct(goal, context, narrate)

    async def _record_learning(self, goal: str, result: AgenticTaskResult):
        """Record successful execution for future learning."""
        if not self._neural_mesh:
            return

        try:
            knowledge = {
                "goal": goal,
                "mode": result.mode,
                "actions_count": result.actions_count,
                "execution_time_ms": result.execution_time_ms,
                "success": result.success,
                "timestamp": datetime.now().isoformat(),
            }

            if hasattr(self._neural_mesh, 'knowledge_graph'):
                kg = self._neural_mesh.knowledge_graph
                if hasattr(kg, 'add_fact'):
                    await kg.add_fact(
                        subject=goal,
                        predicate="executed_successfully",
                        object_=f"{result.actions_count} actions",
                        metadata=knowledge
                    )
        except Exception as e:
            self.logger.debug(f"Learning recording error: {e}")

    # =========================================================================
    # Neural Mesh Deep Integration (v5.0)
    # =========================================================================

    async def _setup_neural_mesh_deep_integration(self):
        """Setup deep Neural Mesh integration with pattern subscriptions and AGI OS events."""
        if not self._neural_mesh:
            return

        try:
            # Subscribe to pattern insights from PatternRecognitionAgent
            if self.config.neural_mesh_pattern_subscribe:
                await self._subscribe_to_pattern_insights()
                self._nm_pattern_subscription_active = True
                self.logger.debug("[AgenticRunner] ✓ Neural Mesh pattern subscription active")

            # Subscribe to AGI OS events
            if self.config.neural_mesh_agi_events:
                await self._subscribe_to_agi_os_events()
                self._nm_agi_subscription_active = True
                self.logger.debug("[AgenticRunner] ✓ Neural Mesh AGI OS subscription active")

            self.logger.info("[AgenticRunner] ✓ Neural Mesh Deep Integration enabled")

        except Exception as e:
            self.logger.debug(f"[AgenticRunner] Neural Mesh deep integration setup failed: {e}")

    async def _subscribe_to_pattern_insights(self):
        """Subscribe to pattern recognition insights from Neural Mesh."""
        if not self._neural_mesh or not hasattr(self._neural_mesh, 'bus'):
            return

        try:
            from neural_mesh.data_models import MessageType

            async def handle_pattern_insight(message):
                """Handle pattern insight messages from PatternRecognitionAgent."""
                try:
                    payload = message.payload if hasattr(message, 'payload') else {}
                    insight = payload.get('insight', payload.get('pattern', str(payload)))
                    if insight and insight not in self._nm_pattern_insights:
                        self._nm_pattern_insights.append(insight)
                        # Keep only last 20 insights
                        if len(self._nm_pattern_insights) > 20:
                            self._nm_pattern_insights = self._nm_pattern_insights[-20:]
                        self.logger.debug(f"[AgenticRunner] Received pattern insight: {insight[:50]}...")
                except Exception as e:
                    self.logger.debug(f"Pattern insight handling error: {e}")

            # Subscribe to pattern-related messages
            await self._neural_mesh.bus.subscribe(
                "agentic_task_runner",
                MessageType.KNOWLEDGE_SHARED,
                handle_pattern_insight
            )

        except Exception as e:
            self.logger.debug(f"Pattern subscription error: {e}")

    async def _subscribe_to_agi_os_events(self):
        """Subscribe to AGI OS proactive event stream via Neural Mesh."""
        if not self._neural_mesh or not hasattr(self._neural_mesh, 'bus'):
            return

        try:
            from neural_mesh.data_models import MessageType

            async def handle_agi_event(message):
                """Handle AGI OS events for context enrichment."""
                try:
                    payload = message.payload if hasattr(message, 'payload') else {}
                    event_type = payload.get('event_type', 'unknown')
                    self.logger.debug(f"[AgenticRunner] AGI OS event: {event_type}")
                    # Store event for context enrichment during task execution
                except Exception as e:
                    self.logger.debug(f"AGI event handling error: {e}")

            await self._neural_mesh.bus.subscribe(
                "agentic_task_runner",
                MessageType.CONTEXT_UPDATE,
                handle_agi_event
            )

        except Exception as e:
            self.logger.debug(f"AGI OS subscription error: {e}")

    async def _publish_task_event(
        self,
        event_type: str,
        goal: str,
        mode: str,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        """Publish task event to Neural Mesh bus."""
        if not self._neural_mesh or not hasattr(self._neural_mesh, 'bus'):
            return

        if not self.config.neural_mesh_task_events:
            return

        try:
            from neural_mesh.data_models import MessageType, AgentMessage, MessagePriority

            # Map event type to message type
            type_map = {
                "task_started": MessageType.TASK_STARTED,
                "task_progress": MessageType.TASK_PROGRESS,
                "task_completed": MessageType.TASK_COMPLETED,
                "task_failed": MessageType.TASK_FAILED,
            }
            msg_type = type_map.get(event_type, MessageType.CUSTOM)

            event = NeuralMeshTaskEvent(
                task_id=self._current_task_id or f"task_{uuid.uuid4().hex[:8]}",
                event_type=event_type,
                goal=goal,
                mode=mode,
                timestamp=time.time(),
                metadata=metadata or {},
            )

            message = AgentMessage(
                message_id=uuid.uuid4().hex,
                from_agent="agentic_task_runner",
                to_agent="broadcast",
                message_type=msg_type,
                payload={
                    "task_id": event.task_id,
                    "event_type": event.event_type,
                    "goal": event.goal,
                    "mode": event.mode,
                    "timestamp": event.timestamp,
                    "metadata": event.metadata,
                },
                priority=MessagePriority.NORMAL,
            )

            await self._neural_mesh.bus.publish(message)
            self._nm_events_sent += 1
            self.logger.debug(f"[AgenticRunner] Published {event_type} event to Neural Mesh")

        except Exception as e:
            self.logger.debug(f"Task event publish error: {e}")

    async def _query_neural_context(self, goal: str) -> NeuralMeshContext:
        """Query Neural Mesh knowledge graph for context enrichment."""
        context = NeuralMeshContext()

        if not self._neural_mesh or not self.config.neural_mesh_context_query:
            return context

        try:
            # Query for similar past goals
            if hasattr(self._neural_mesh, 'knowledge') or hasattr(self._neural_mesh, 'knowledge_graph'):
                kg = getattr(self._neural_mesh, 'knowledge', None) or getattr(self._neural_mesh, 'knowledge_graph', None)

                if kg and hasattr(kg, 'query') or hasattr(kg, 'search'):
                    query_fn = getattr(kg, 'query', None) or getattr(kg, 'search', None)
                    if query_fn:
                        try:
                            # Query for similar goals
                            results = await query_fn(goal[:100])  # Limit query length
                            if results:
                                for result in results[:5]:  # Top 5 similar
                                    if isinstance(result, dict):
                                        context.similar_goals.append(result)
                                    else:
                                        context.similar_goals.append({"result": str(result)})

                                context.context_score = min(len(context.similar_goals) * 0.2, 1.0)

                        except Exception as query_error:
                            self.logger.debug(f"Knowledge query error: {query_error}")

            # Include recent pattern insights
            if self._nm_pattern_insights:
                context.pattern_insights = self._nm_pattern_insights[-5:]

            # Generate recommended actions based on context
            if context.similar_goals:
                for similar in context.similar_goals[:3]:
                    if isinstance(similar, dict):
                        action = similar.get('action', similar.get('result', ''))
                        if action:
                            context.recommended_actions.append(str(action)[:100])

            self.logger.debug(
                f"[AgenticRunner] Neural context: {len(context.similar_goals)} similar, "
                f"{len(context.pattern_insights)} insights, score={context.context_score:.2f}"
            )

        except Exception as e:
            self.logger.debug(f"Neural context query error: {e}")

        return context

    # =========================================================================
    # v9.4: Neural Mesh Production Integration
    # =========================================================================

    async def _setup_neural_mesh_production_integration(self):
        """Setup v9.4 production Neural Mesh integration with workflows and knowledge."""
        if not self._neural_mesh_coordinator:
            return

        try:
            self.logger.info("[AgenticRunner] Setting up v9.4 production Neural Mesh integration...")

            # Register this runner as an agent in the Neural Mesh
            if hasattr(self._neural_mesh_coordinator, 'registry'):
                from neural_mesh.data_models import AgentInfo, AgentStatus
                agent_info = AgentInfo(
                    agent_id="agentic_task_runner",
                    agent_name="AgenticTaskRunner",
                    agent_type="executor",
                    capabilities={"task_execution", "computer_use", "reasoning"},
                    status=AgentStatus.ONLINE,
                )
                await self._neural_mesh_coordinator.registry.register(agent_info)
                self.logger.debug("[AgenticRunner] Registered in Neural Mesh registry")

            self.logger.info("[AgenticRunner] ✓ v9.4 Production Neural Mesh integration ready")

        except Exception as e:
            self.logger.debug(f"[AgenticRunner] Production integration setup error: {e}")

    async def _execute_multi_agent_workflow(
        self,
        goal: str,
        capabilities_needed: Optional[List[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        v9.4: Execute a multi-agent workflow via Neural Mesh orchestrator.

        Uses the production orchestrator to coordinate multiple agents
        for complex tasks that benefit from multi-agent collaboration.

        Args:
            goal: The task goal
            capabilities_needed: Specific capabilities required

        Returns:
            Workflow result or None if not available
        """
        if not self._neural_mesh_coordinator or not self.config.neural_mesh_workflow_execution:
            return None

        if not hasattr(self._neural_mesh_coordinator, 'orchestrator'):
            return None

        try:
            from neural_mesh.data_models import WorkflowTask, ExecutionStrategy

            capabilities_needed = capabilities_needed or ["analysis", "reasoning"]

            # Create workflow tasks based on goal
            workflow_tasks = []
            for cap in capabilities_needed:
                workflow_tasks.append(WorkflowTask(
                    task_id=f"task_{cap}_{uuid.uuid4().hex[:6]}",
                    required_capability=cap,
                    payload={
                        "action": "analyze",
                        "input": {"goal": goal},
                    },
                    timeout_seconds=30.0,
                ))

            # Execute workflow
            workflow_result = await self._neural_mesh_coordinator.orchestrator.execute_workflow(
                name=f"agentic_workflow_{uuid.uuid4().hex[:8]}",
                tasks=workflow_tasks,
                strategy=ExecutionStrategy.PARALLEL,
            )

            self._nm_workflows_executed += 1
            self.logger.debug(
                f"[AgenticRunner] Workflow executed: {len(workflow_tasks)} tasks, "
                f"success={workflow_result.success}"
            )

            return {
                "workflow_id": workflow_result.workflow_id,
                "success": workflow_result.success,
                "task_count": len(workflow_tasks),
                "duration_ms": workflow_result.duration_ms,
            }

        except Exception as e:
            self.logger.debug(f"Workflow execution error: {e}")
            return None

    async def _contribute_to_knowledge_graph(
        self,
        goal: str,
        result: "AgenticTaskResult",
    ) -> bool:
        """
        v9.4: Contribute task execution experience to Neural Mesh knowledge graph.

        Stores successful execution patterns for future context enrichment.

        Args:
            goal: The executed goal
            result: Execution result

        Returns:
            True if contribution successful
        """
        if not self._neural_mesh_coordinator or not self.config.neural_mesh_knowledge_contribute:
            return False

        if not result.success:
            return False

        try:
            kg = getattr(self._neural_mesh_coordinator, 'knowledge', None)
            if not kg:
                return False

            from neural_mesh.data_models import KnowledgeType

            # Store execution pattern
            await kg.store(
                key=f"execution_{uuid.uuid4().hex[:8]}",
                content={
                    "goal": goal[:500],
                    "mode": result.mode,
                    "actions_count": result.actions_count,
                    "execution_time_ms": result.execution_time_ms,
                    "success": True,
                },
                knowledge_type=KnowledgeType.EXECUTION_PATTERN,
                source="agentic_task_runner",
                tags=["execution", "task", result.mode],
            )

            self._nm_knowledge_entries_added += 1
            self.logger.debug(f"[AgenticRunner] Contributed execution pattern to knowledge graph")
            return True

        except Exception as e:
            self.logger.debug(f"Knowledge contribution error: {e}")
            return False

    async def _delegate_to_specialized_agent(
        self,
        goal: str,
        capability: str,
    ) -> Optional[Dict[str, Any]]:
        """
        v9.4: Delegate task to specialized agent via Neural Mesh registry.

        Finds an agent with the required capability and delegates execution.

        Args:
            goal: The task goal
            capability: Required capability

        Returns:
            Agent result or None if no suitable agent found
        """
        if not self._neural_mesh_bridge or not self.config.neural_mesh_agent_delegation:
            return None

        try:
            # Find agents with capability
            agents = self._neural_mesh_bridge.get_agents_by_capability(capability)
            if not agents:
                self.logger.debug(f"[AgenticRunner] No agents with capability: {capability}")
                return None

            # Use first available agent
            agent = agents[0]

            # Execute task via agent
            task_result = await agent.execute_task({
                "action": "execute",
                "input": {"goal": goal},
            })

            self._nm_agents_delegated += 1
            self.logger.debug(f"[AgenticRunner] Delegated to agent: {agent.agent_name}")

            return {
                "agent": agent.agent_name,
                "result": task_result,
            }

        except Exception as e:
            self.logger.debug(f"Agent delegation error: {e}")
            return None

    async def execute_cross_system_task(
        self,
        goal: str,
        systems: Optional[List[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        v9.4: Execute a task across multiple JARVIS systems via Neural Mesh Bridge.

        This enables collaboration between intelligence, autonomy, and voice systems.

        Args:
            goal: Task to execute
            systems: Systems to involve (intelligence, autonomy, voice)

        Returns:
            Cross-system result or None if bridge not available
        """
        if not self._neural_mesh_bridge:
            return None

        try:
            result = await self._neural_mesh_bridge.execute_cross_system_task(
                task_description=goal,
                systems=systems or ["intelligence", "autonomy"],
            )

            self.logger.debug(f"[AgenticRunner] Cross-system task executed: {result.get('workflow_id')}")
            return result

        except Exception as e:
            self.logger.debug(f"Cross-system task error: {e}")
            return None

    def get_neural_mesh_production_stats(self) -> Dict[str, Any]:
        """Get v9.4 Neural Mesh production integration statistics."""
        return {
            "production_active": self._nm_production_active,
            "coordinator_available": self._neural_mesh_coordinator is not None,
            "bridge_available": self._neural_mesh_bridge is not None,
            "bridge_agents": (
                len(self._neural_mesh_bridge.registered_agents)
                if self._neural_mesh_bridge else 0
            ),
            "workflows_executed": self._nm_workflows_executed,
            "agents_delegated": self._nm_agents_delegated,
            "knowledge_entries_added": self._nm_knowledge_entries_added,
            "events_sent": self._nm_events_sent,
            "pattern_insights": len(self._nm_pattern_insights),
        }

    async def _record_to_training_database(
        self,
        goal: str,
        result: AgenticTaskResult,
        context: Optional[Dict[str, Any]] = None,
    ) -> Optional[int]:
        """
        v9.0: Record experience to SQLite training database for model training.

        This connects to the unified_data_flywheel's training database,
        which feeds into the reactor-core training pipeline.

        Args:
            goal: The user's input/goal
            result: Execution result
            context: Additional context

        Returns:
            Experience ID if recorded, None otherwise
        """
        try:
            # Try to import the unified_data_flywheel
            from autonomy.unified_data_flywheel import get_data_flywheel

            flywheel = get_data_flywheel()
            if not flywheel:
                self.logger.debug("[AgenticRunner] Data Flywheel not available")
                return None

            # Prepare context for storage
            experience_context = {
                "mode": result.mode,
                "execution_time_ms": result.execution_time_ms,
                "actions_count": result.actions_count,
                "reasoning_steps": result.reasoning_steps,
                "success": result.success,
                "uae_used": result.uae_used,
                "neural_mesh_used": result.neural_mesh_used,
                "task_id": self._current_task_id,
                "timestamp": datetime.now().isoformat(),
            }

            # Add neural mesh insights if available
            if result.neural_mesh_context:
                experience_context["pattern_insights"] = result.pattern_insights_applied
                experience_context["context_score"] = result.neural_mesh_context.context_score

            # Merge with provided context
            if context:
                experience_context.update(context)

            # Calculate quality score based on result
            quality_score = 0.5
            if result.success:
                quality_score = 0.8  # Base score for success
                # Bonus for efficient execution
                if result.actions_count < 10:
                    quality_score += 0.1
                # Bonus for using intelligence systems
                if result.neural_mesh_used or result.uae_used:
                    quality_score += 0.05
            else:
                quality_score = 0.3  # Lower score for failures (still useful for learning)

            # Add experience to training database
            experience_id = flywheel.add_experience(
                source="agentic_runner",
                input_text=goal,
                output_text=result.final_message,
                context=experience_context,
                quality_score=min(quality_score, 1.0),
            )

            if experience_id:
                self.logger.debug(f"[AgenticRunner] Recorded experience {experience_id} to training DB")

            return experience_id

        except ImportError:
            self.logger.debug("[AgenticRunner] unified_data_flywheel not available")
            return None
        except Exception as e:
            self.logger.debug(f"[AgenticRunner] Training DB recording error: {e}")
            return None

    async def _record_comprehensive_learning(
        self,
        goal: str,
        result: AgenticTaskResult,
        context: Optional[NeuralMeshContext] = None,
    ):
        """Record comprehensive execution data to Neural Mesh knowledge graph."""
        # v9.0: Also record to training database for model fine-tuning
        await self._record_to_training_database(goal, result)

        if not self._neural_mesh:
            return 0

        contributions = 0

        try:
            kg = getattr(self._neural_mesh, 'knowledge', None) or getattr(self._neural_mesh, 'knowledge_graph', None)

            if kg and hasattr(kg, 'add_fact'):
                # Record execution result
                await kg.add_fact(
                    subject=goal,
                    predicate="executed_with_result",
                    object_=f"{'success' if result.success else 'failure'}: {result.final_message[:100]}",
                    metadata={
                        "task_id": self._current_task_id,
                        "mode": result.mode,
                        "actions_count": result.actions_count,
                        "execution_time_ms": result.execution_time_ms,
                        "success": result.success,
                        "reasoning_steps": result.reasoning_steps,
                        "error": result.error,
                        "timestamp": datetime.now().isoformat(),
                        "context_score": context.context_score if context else 0,
                        "pattern_insights_used": len(context.pattern_insights) if context else 0,
                    }
                )
                contributions += 1

                # Record learning insights if any
                for insight in result.learning_insights:
                    await kg.add_fact(
                        subject=goal,
                        predicate="learning_insight",
                        object_=insight[:200],
                        metadata={"task_id": self._current_task_id}
                    )
                    contributions += 1

                # Record applied pattern insights
                for pattern in result.pattern_insights_applied:
                    await kg.add_fact(
                        subject=goal,
                        predicate="applied_pattern",
                        object_=pattern[:200],
                        metadata={"task_id": self._current_task_id, "success": result.success}
                    )
                    contributions += 1

                self._nm_knowledge_contributions += contributions
                self.logger.debug(f"[AgenticRunner] Recorded {contributions} knowledge contributions")

        except Exception as e:
            self.logger.debug(f"Comprehensive learning recording error: {e}")

        return contributions

    # =========================================================================
    # Lifecycle
    # =========================================================================

    async def shutdown(self):
        """Gracefully shutdown all components."""
        self.logger.info("[AgenticRunner] Shutting down...")

        # Stop heartbeat
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None

        # Don't stop external watchdog (supervisor manages it)
        if self._watchdog and not self._external_watchdog:
            try:
                from core.agentic_watchdog import stop_watchdog
                await stop_watchdog()
            except Exception:
                pass

        # Stop UAE
        if self._uae:
            try:
                if self._uae.is_active:
                    await self._uae.stop()
            except Exception:
                pass

        # Stop Neural Mesh
        if self._neural_mesh:
            try:
                from neural_mesh.neural_mesh_coordinator import stop_neural_mesh
                await stop_neural_mesh()
            except Exception:
                pass

        # Stop Autonomy Components (v6.0)
        await self._shutdown_autonomy_components()

        # Close JARVIS Prime client session
        if self._jarvis_prime_client and self._jarvis_prime_client.get("session"):
            try:
                await self._jarvis_prime_client["session"].close()
            except Exception:
                pass

        self.logger.info("[AgenticRunner] Shutdown complete")

    async def _shutdown_autonomy_components(self):
        """Shutdown all autonomy components gracefully."""
        # Stop Phase Manager
        if self._phase_manager:
            try:
                if hasattr(self._phase_manager, 'stop'):
                    await self._phase_manager.stop()
            except Exception as e:
                self.logger.debug(f"Phase Manager shutdown error: {e}")

        # Stop Tool Registry
        if self._tool_registry:
            try:
                if hasattr(self._tool_registry, 'stop'):
                    await self._tool_registry.stop()
            except Exception as e:
                self.logger.debug(f"Tool Registry shutdown error: {e}")

        # Persist and stop Memory Manager
        if self._memory_manager:
            try:
                if hasattr(self._memory_manager, 'persist'):
                    await self._memory_manager.persist()
                if hasattr(self._memory_manager, 'stop'):
                    await self._memory_manager.stop()
            except Exception as e:
                self.logger.debug(f"Memory Manager shutdown error: {e}")

        # Stop Error Recovery Orchestrator
        if self._error_recovery:
            try:
                if hasattr(self._error_recovery, 'stop'):
                    await self._error_recovery.stop()
            except Exception as e:
                self.logger.debug(f"Error Recovery shutdown error: {e}")

        # Stop UAE Context Manager
        if self._uae_context:
            try:
                if hasattr(self._uae_context, 'stop'):
                    await self._uae_context.stop()
            except Exception as e:
                self.logger.debug(f"UAE Context shutdown error: {e}")

        # Stop Intervention Orchestrator
        if self._intervention:
            try:
                if hasattr(self._intervention, 'stop'):
                    await self._intervention.stop()
            except Exception as e:
                self.logger.debug(f"Intervention Orchestrator shutdown error: {e}")

    def get_stats(self) -> Dict[str, Any]:
        """Get comprehensive runner statistics including all components."""
        # Watchdog status
        watchdog_status = None
        if self._watchdog:
            try:
                status = self._watchdog.get_status()
                watchdog_status = {
                    "mode": status.mode.value,
                    "kill_switch_armed": status.kill_switch_armed,
                    "heartbeat_healthy": status.heartbeat_healthy,
                    "uptime_seconds": status.uptime_seconds,
                }
            except Exception:
                watchdog_status = {"status": "available"}

        # Neural Mesh Deep Integration stats
        neural_mesh_stats = None
        if self._neural_mesh and self.config.neural_mesh_deep_enabled:
            neural_mesh_stats = {
                "deep_integration_enabled": True,
                "pattern_subscription_active": self._nm_pattern_subscription_active,
                "agi_subscription_active": self._nm_agi_subscription_active,
                "events_sent": self._nm_events_sent,
                "knowledge_contributions": self._nm_knowledge_contributions,
                "pattern_insights_cached": len(self._nm_pattern_insights),
            }

        # Phase Manager stats
        phase_manager_stats = None
        if self._phase_manager:
            try:
                phase_manager_stats = {
                    "enabled": True,
                    "current_phase": self._current_phase,
                    "phase_execution_active": self._phase_execution_active,
                    "checkpoints_saved": len(self._phase_checkpoints),
                }
                if hasattr(self._phase_manager, 'get_metrics'):
                    phase_manager_stats.update(self._phase_manager.get_metrics())
            except Exception:
                phase_manager_stats = {"enabled": True, "status": "available"}

        # Tool Registry stats
        tool_registry_stats = None
        if self._tool_registry:
            try:
                tool_registry_stats = {"enabled": True}
                if hasattr(self._tool_registry, 'get_metrics'):
                    tool_registry_stats.update(self._tool_registry.get_metrics())
                elif hasattr(self._tool_registry, 'tool_count'):
                    tool_registry_stats["tool_count"] = self._tool_registry.tool_count
            except Exception:
                tool_registry_stats = {"enabled": True, "status": "available"}

        # Memory Manager stats
        memory_manager_stats = None
        if self._memory_manager:
            try:
                memory_manager_stats = {
                    "enabled": True,
                    "experience_cache_size": len(self._experience_replay_cache),
                }
                if hasattr(self._memory_manager, 'get_metrics'):
                    memory_manager_stats.update(self._memory_manager.get_metrics())
            except Exception:
                memory_manager_stats = {"enabled": True, "status": "available"}

        # Error Recovery stats
        error_recovery_stats = None
        if self._error_recovery:
            try:
                error_recovery_stats = {"enabled": True}
                if hasattr(self._error_recovery, 'get_metrics'):
                    error_recovery_stats.update(self._error_recovery.get_metrics())
            except Exception:
                error_recovery_stats = {"enabled": True, "status": "available"}

        # UAE Context stats
        uae_context_stats = None
        if self._uae_context:
            try:
                uae_context_stats = {"enabled": True}
                if hasattr(self._uae_context, 'get_metrics'):
                    uae_context_stats.update(self._uae_context.get_metrics())
            except Exception:
                uae_context_stats = {"enabled": True, "status": "available"}

        # Intervention Orchestrator stats
        intervention_stats = None
        if self._intervention:
            try:
                intervention_stats = {
                    "enabled": True,
                    "active_interventions": self._active_interventions,
                }
                if hasattr(self._intervention, 'get_metrics'):
                    intervention_stats.update(self._intervention.get_metrics())
            except Exception:
                intervention_stats = {"enabled": True, "status": "available"}

        # JARVIS Prime stats
        jarvis_prime_stats = None
        if self._jarvis_prime_client:
            jarvis_prime_stats = {
                "enabled": True,
                "connected": self._jarvis_prime_client.get("connected", False),
                "url": self._jarvis_prime_client.get("url", "unknown"),
                "last_health_check": self._jarvis_prime_client.get("last_health_check"),
            }

        # Voice Auth stats
        voice_auth_stats = None
        if self._voice_auth_layer:
            try:
                voice_auth_stats = {"enabled": True}
                if hasattr(self._voice_auth_layer, 'get_metrics'):
                    voice_auth_stats.update(self._voice_auth_layer.get_metrics())
            except Exception:
                voice_auth_stats = {"enabled": True, "status": "available"}

        return {
            "version": "6.0.0",
            "initialized": self._initialized,
            "tasks_executed": self._tasks_executed,
            "tasks_succeeded": self._tasks_succeeded,
            "success_rate": (
                self._tasks_succeeded / self._tasks_executed
                if self._tasks_executed > 0 else 0.0
            ),
            "current_task": self._current_task_id,
            "watchdog": watchdog_status,
            "neural_mesh_deep": neural_mesh_stats,
            # Autonomy Components (v6.0)
            "phase_manager": phase_manager_stats,
            "tool_registry": tool_registry_stats,
            "memory_manager": memory_manager_stats,
            "error_recovery": error_recovery_stats,
            "uae_context": uae_context_stats,
            "intervention": intervention_stats,
            "jarvis_prime": jarvis_prime_stats,
            "voice_auth": voice_auth_stats,
            # Component availability summary
            "components": {
                "uae": self._uae is not None,
                "neural_mesh": self._neural_mesh is not None,
                "neural_mesh_deep": self.config.neural_mesh_deep_enabled and self._neural_mesh is not None,
                "autonomous_agent": self._autonomous_agent is not None,
                "computer_use_tool": self._computer_use_tool is not None,
                "direct_connector": self._computer_use_connector is not None,
                "voice_auth_layer": self._voice_auth_layer is not None,
                # Autonomy Components
                "phase_manager": self._phase_manager is not None,
                "tool_registry": self._tool_registry is not None,
                "memory_manager": self._memory_manager is not None,
                "error_recovery": self._error_recovery is not None,
                "uae_context": self._uae_context is not None,
                "intervention": self._intervention is not None,
                "jarvis_prime": self._jarvis_prime_client is not None and self._jarvis_prime_client.get("connected", False),
            },
            "availability": self._availability,
        }

    @property
    def is_ready(self) -> bool:
        """Check if runner is ready to execute tasks."""
        return self._initialized and (self._computer_use_tool is not None or self._computer_use_connector is not None)


# =============================================================================
# Singleton Access (for backward compatibility)
# =============================================================================

_runner_instance: Optional[AgenticTaskRunner] = None


def get_agentic_runner() -> Optional[AgenticTaskRunner]:
    """Get the global runner instance (if set)."""
    return _runner_instance


def set_agentic_runner(runner: AgenticTaskRunner):
    """Set the global runner instance."""
    global _runner_instance
    _runner_instance = runner


async def create_agentic_runner(
    config: Optional[AgenticRunnerConfig] = None,
    tts_callback: Optional[Callable[[str], Awaitable[None]]] = None,
    watchdog: Optional[Any] = None,
) -> AgenticTaskRunner:
    """Create and initialize an agentic runner."""
    runner = AgenticTaskRunner(
        config=config,
        tts_callback=tts_callback,
        watchdog=watchdog,
    )
    await runner.initialize()
    return runner
