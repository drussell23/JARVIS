"""
Cross-Repo Intelligence Hub for JARVIS
=======================================

Unified intelligence hub that connects all integrated systems from:
- Aider: Repository Intelligence (Tree-sitter, PageRank)
- Open Interpreter: Computer Use Refinements (Streaming, Safety)
- MetaGPT: SOP Enforcement (ActionNode, BY_ORDER)
- MemGPT: Unified Memory System (Paging, Archival)
- Fabric: Wisdom Patterns (Optimized Prompts)
- Claude Code: Philosophy and reasoning patterns

This hub provides:
- Central orchestration of all intelligence systems
- Cross-system event bridging
- Unified state management
- LangGraph reasoning integration
- Async parallel processing

Author: JARVIS AI System
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from pathlib import Path
from typing import (
    Any, AsyncIterator, Callable, Dict, Generic, List, Literal,
    Optional, Protocol, Set, Tuple, Type, TypeVar, Union
)
from uuid import uuid4

logger = logging.getLogger(__name__)

T = TypeVar("T")


# ============================================================================
# Configuration (Environment-Driven, No Hardcoding)
# ============================================================================

def _get_env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _get_env_int(key: str, default: int) -> int:
    try:
        return int(_get_env(key, str(default)))
    except ValueError:
        return default


def _get_env_float(key: str, default: float) -> float:
    try:
        return float(_get_env(key, str(default)))
    except ValueError:
        return default


def _get_env_bool(key: str, default: bool = False) -> bool:
    return _get_env(key, str(default)).lower() in ("true", "1", "yes")


def _get_env_path(key: str, default: str) -> Path:
    return Path(os.path.expanduser(_get_env(key, default)))


@dataclass
class CrossRepoHubConfig:
    """Configuration for the cross-repo intelligence hub."""
    # Connected repositories
    jarvis_repo: Path = field(
        default_factory=lambda: _get_env_path(
            "JARVIS_REPO_PATH",
            "~/Documents/repos/JARVIS-AI-Agent"
        )
    )
    jarvis_prime_repo: Path = field(
        default_factory=lambda: _get_env_path(
            "JARVIS_PRIME_REPO_PATH",
            "~/Documents/repos/jarvis-prime"
        )
    )
    reactor_core_repo: Path = field(
        default_factory=lambda: _get_env_path(
            "REACTOR_CORE_REPO_PATH",
            "~/Documents/repos/reactor-core"
        )
    )

    # System toggles
    enable_repository_intelligence: bool = field(
        default_factory=lambda: _get_env_bool("JARVIS_ENABLE_REPO_INTEL", True)
    )
    enable_computer_use: bool = field(
        default_factory=lambda: _get_env_bool("JARVIS_ENABLE_COMPUTER_USE", True)
    )
    enable_sop_enforcement: bool = field(
        default_factory=lambda: _get_env_bool("JARVIS_ENABLE_SOP", True)
    )
    enable_memory_system: bool = field(
        default_factory=lambda: _get_env_bool("JARVIS_ENABLE_MEMORY", True)
    )
    enable_wisdom_patterns: bool = field(
        default_factory=lambda: _get_env_bool("JARVIS_ENABLE_WISDOM", True)
    )

    # Processing settings
    parallel_processing: bool = field(
        default_factory=lambda: _get_env_bool("JARVIS_PARALLEL_PROCESSING", True)
    )
    max_concurrent_tasks: int = field(
        default_factory=lambda: _get_env_int("JARVIS_MAX_CONCURRENT", 5)
    )

    # State persistence
    persist_state: bool = field(
        default_factory=lambda: _get_env_bool("JARVIS_PERSIST_STATE", True)
    )
    state_dir: Path = field(
        default_factory=lambda: _get_env_path("JARVIS_STATE_DIR", "~/.jarvis/hub_state")
    )


# ============================================================================
# Enums
# ============================================================================

class IntelligenceSystem(str, Enum):
    """Available intelligence systems."""
    REPOSITORY = "repository"
    COMPUTER_USE = "computer_use"
    SOP = "sop"
    MEMORY = "memory"
    WISDOM = "wisdom"
    REASONING = "reasoning"
    VBIA = "vbia"  # v6.2: Voice Biometric Intelligent Authentication
    SPATIAL = "spatial"  # v6.2: 3D OS Awareness (Proprioception)
    VOICE_LEARNING = "voice_learning"  # v81.0: Unified Learning Loop


class EventType(str, Enum):
    """Types of events in the hub."""
    # Repository Intelligence
    REPO_MAP_GENERATED = "repo_map_generated"
    REPO_SYMBOL_FOUND = "repo_symbol_found"
    REPO_DEPENDENCY_DETECTED = "repo_dependency_detected"

    # Computer Use
    TOOL_EXECUTED = "tool_executed"
    SAFETY_TRIGGERED = "safety_triggered"
    SCREENSHOT_CAPTURED = "screenshot_captured"

    # SOP
    SOP_STARTED = "sop_started"
    SOP_STEP_COMPLETED = "sop_step_completed"
    SOP_COMPLETED = "sop_completed"
    SOP_FAILED = "sop_failed"

    # Memory
    MEMORY_STORED = "memory_stored"
    MEMORY_RETRIEVED = "memory_retrieved"
    MEMORY_PAGED_OUT = "memory_paged_out"

    # Wisdom
    PATTERN_SELECTED = "pattern_selected"
    PROMPT_ENHANCED = "prompt_enhanced"

    # VBIA (Voice Biometric Intelligent Authentication) - v6.2 NEW
    VBIA_VISUAL_SECURITY = "vbia_visual_security"
    VBIA_VISUAL_THREAT = "vbia_visual_threat"
    VBIA_VISUAL_SAFE = "vbia_visual_safe"
    VBIA_AUTH_STARTED = "vbia_auth_started"
    VBIA_AUTH_SUCCESS = "vbia_auth_success"
    VBIA_AUTH_FAILED = "vbia_auth_failed"
    VBIA_EVIDENCE_COLLECTED = "vbia_evidence_collected"
    VBIA_MULTI_FACTOR_FUSION = "vbia_multi_factor_fusion"
    VBIA_REASONING_STARTED = "vbia_reasoning_started"
    VBIA_REASONING_THOUGHT = "vbia_reasoning_thought"
    VBIA_REASONING_COMPLETED = "vbia_reasoning_completed"
    VBIA_COST_TRACKED = "vbia_cost_tracked"
    VBIA_PATTERN_LEARNED = "vbia_pattern_learned"
    VBIA_SYSTEM_READY = "vbia_system_ready"
    VBIA_SYSTEM_ERROR = "vbia_system_error"

    # Spatial Awareness (3D OS Proprioception) - v6.2 Grand Unification
    SPATIAL_CONTEXT_UPDATED = "spatial_context_updated"
    SPATIAL_APP_SWITCH = "spatial_app_switch"
    SPATIAL_SPACE_TELEPORT = "spatial_space_teleport"
    SPATIAL_WINDOW_FOCUSED = "spatial_window_focused"

    # Cross-System
    CONTEXT_ENRICHED = "context_enriched"
    STATE_SYNCHRONIZED = "state_synchronized"

    # Unified Learning Loop - v81.0 Trinity Integration
    VOICE_EXPERIENCE_COLLECTED = "voice_experience_collected"
    MODEL_UPDATE_AVAILABLE = "model_update_available"
    MODEL_DEPLOYED = "model_deployed"
    MODEL_ROLLBACK = "model_rollback"
    TRAINING_TRIGGERED = "training_triggered"
    AB_TEST_STARTED = "ab_test_started"
    AB_TEST_COMPLETED = "ab_test_completed"


class TaskPriority(int, Enum):
    """Task priority levels."""
    LOW = 0
    NORMAL = 1
    HIGH = 2
    CRITICAL = 3


# ============================================================================
# Data Classes
# ============================================================================

@dataclass
class HubEvent:
    """An event in the intelligence hub."""
    event_type: EventType
    source_system: IntelligenceSystem
    payload: Dict[str, Any]
    timestamp: datetime = field(default_factory=datetime.now)
    event_id: str = field(default_factory=lambda: uuid4().hex)
    correlation_id: Optional[str] = None


@dataclass
class IntelligenceTask:
    """A task to be processed by the hub."""
    task_id: str
    systems: List[IntelligenceSystem]
    input_data: Dict[str, Any]
    priority: TaskPriority = TaskPriority.NORMAL
    timeout_seconds: int = 300
    callback: Optional[Callable] = None
    created_at: datetime = field(default_factory=datetime.now)


@dataclass
class TaskResult:
    """Result of a task execution."""
    task_id: str
    success: bool
    results: Dict[IntelligenceSystem, Any]
    errors: Dict[IntelligenceSystem, str]
    duration_ms: float
    events: List[HubEvent]


@dataclass
class HubState:
    """Current state of the hub."""
    active_systems: Set[IntelligenceSystem]
    pending_tasks: int
    total_events_processed: int
    uptime_seconds: float
    last_activity: datetime
    system_health: Dict[IntelligenceSystem, bool]


# ============================================================================
# System Adapters (Lazy Loading)
# ============================================================================

class RepositoryIntelligenceAdapter:
    """Adapter for repository intelligence system."""

    def __init__(self):
        self._mapper = None
        self._initialized = False

    async def initialize(self) -> None:
        if self._initialized:
            return
        try:
            from backend.intelligence.repository_intelligence import get_repository_mapper
            self._mapper = await get_repository_mapper()
            self._initialized = True
        except ImportError as e:
            logger.warning(f"Repository intelligence not available: {e}")

    async def get_repo_map(
        self,
        repository: str,
        max_tokens: int = 4000,
        mentioned_files: Optional[Set[str]] = None,
        mentioned_symbols: Optional[Set[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        if not self._initialized:
            await self.initialize()
        if not self._mapper:
            return None

        result = await self._mapper.get_repo_map(
            repository=repository,
            max_tokens=max_tokens,
            mentioned_files=mentioned_files or set(),
            mentioned_symbols=mentioned_symbols or set(),
        )
        return result.__dict__ if result else None

    @property
    def is_available(self) -> bool:
        return self._initialized and self._mapper is not None


class ComputerUseAdapter:
    """Adapter for computer use refinements."""

    def __init__(self):
        self._loop = None
        self._initialized = False

    async def initialize(self) -> None:
        if self._initialized:
            return
        try:
            from backend.intelligence.computer_use_refinements import get_computer_use_loop
            self._loop = get_computer_use_loop()
            self._initialized = True
        except ImportError as e:
            logger.warning(f"Computer use refinements not available: {e}")

    async def execute_tool(self, tool_name: str, **kwargs) -> Optional[Dict[str, Any]]:
        if not self._initialized:
            await self.initialize()
        if not self._loop:
            return None

        result = await self._loop.tools.run(tool_name, kwargs)
        return result.to_dict()

    @property
    def is_available(self) -> bool:
        return self._initialized and self._loop is not None


class SOPAdapter:
    """Adapter for SOP enforcement system."""

    def __init__(self):
        self._initialized = False
        self._sops: Dict[str, Any] = {}

    async def initialize(self) -> None:
        if self._initialized:
            return
        try:
            from backend.intelligence.sop_enforcement import (
                create_code_review_sop,
                create_feature_implementation_sop,
            )
            self._sops["code_review"] = create_code_review_sop
            self._sops["feature_implementation"] = create_feature_implementation_sop
            self._initialized = True
        except ImportError as e:
            logger.warning(f"SOP enforcement not available: {e}")

    async def execute_sop(
        self,
        sop_name: str,
        llm: Any,
        context: str,
    ) -> Optional[Dict[str, Any]]:
        if not self._initialized:
            await self.initialize()

        sop_creator = self._sops.get(sop_name)
        if not sop_creator:
            return None

        sop = sop_creator()
        results = await sop.execute(llm, context)
        return {k: v.to_dict() if hasattr(v, 'to_dict') else str(v) for k, v in results.items()}

    @property
    def is_available(self) -> bool:
        return self._initialized

    def list_sops(self) -> List[str]:
        return list(self._sops.keys())


class MemoryAdapter:
    """Adapter for unified memory system."""

    def __init__(self):
        self._memory = None
        self._initialized = False

    async def initialize(self) -> None:
        if self._initialized:
            return
        try:
            from backend.intelligence.unified_memory_system import get_memory_system
            self._memory = await get_memory_system()
            self._initialized = True
        except ImportError as e:
            logger.warning(f"Unified memory system not available: {e}")

    async def store(self, key: str, content: str, metadata: Optional[Dict] = None) -> bool:
        if not self._initialized:
            await self.initialize()
        if not self._memory:
            return False

        await self._memory.archival.insert(content, metadata or {})
        return True

    async def retrieve(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        if not self._initialized:
            await self.initialize()
        if not self._memory:
            return []

        return await self._memory.archival.search(query, limit)

    @property
    def is_available(self) -> bool:
        return self._initialized and self._memory is not None


class WisdomAdapter:
    """Adapter for wisdom patterns system."""

    def __init__(self):
        self._agent = None
        self._initialized = False

    async def initialize(self) -> None:
        if self._initialized:
            return
        try:
            from backend.intelligence.wisdom_patterns import get_wisdom_agent
            self._agent = await get_wisdom_agent()
            self._initialized = True
        except ImportError as e:
            logger.warning(f"Wisdom patterns not available: {e}")

    async def enhance_prompt(
        self,
        task: str,
        pattern_name: Optional[str] = None,
        input_text: str = "",
    ) -> str:
        if not self._initialized:
            await self.initialize()
        if not self._agent:
            return task

        return await self._agent.enhance_prompt(task, pattern_name, input_text)

    async def suggest_pattern(self, task: str) -> Optional[str]:
        if not self._initialized:
            await self.initialize()
        if not self._agent:
            return None

        match = await self._agent.registry.suggest_pattern(task)
        return match.pattern.name if match else None

    @property
    def is_available(self) -> bool:
        return self._initialized and self._agent is not None


class SafeCodeAdapter:
    """Adapter for safe code execution (Open Interpreter pattern)."""

    def __init__(self):
        self._executor = None
        self._initialized = False

    async def initialize(self) -> None:
        if self._initialized:
            return
        try:
            from backend.intelligence.computer_use_refinements import (
                SafeCodeExecutor,
                ComputerUseConfig,
            )
            config = ComputerUseConfig()
            self._executor = SafeCodeExecutor(config)
            self._initialized = True
            logger.info("SafeCodeAdapter initialized")
        except ImportError as e:
            logger.warning(f"Safe code executor not available: {e}")

    async def execute(
        self,
        code: str,
        context: Optional[Dict[str, Any]] = None,
        timeout_sec: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Execute Python code safely."""
        if not self._initialized:
            await self.initialize()
        if not self._executor:
            return {"success": False, "error": "Safe code executor not available"}

        result = await self._executor.execute(code, context, timeout_sec)
        return {
            "success": result.success,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
            "execution_time_ms": result.execution_time_ms,
            "blocked_reason": result.blocked_reason,
        }

    def validate_code(self, code: str) -> Tuple[bool, Optional[str]]:
        """Validate code without executing."""
        if not self._executor:
            return False, "Executor not initialized"
        return self._executor.validate_code(code)

    @property
    def is_available(self) -> bool:
        return self._initialized and self._executor is not None


class ReactorCoreAdapter:
    """
    Adapter for Reactor Core integration.

    Reactor Core is JARVIS's training and learning pipeline:
    - Collects experiences from task execution
    - Triggers training runs based on experience thresholds
    - Manages learning goals and Safe Scout scraping
    """

    def __init__(self, config: Optional["CrossRepoHubConfig"] = None):
        self.config = config or CrossRepoHubConfig()
        self._client = None
        self._initialized = False
        self._is_online = False

    async def initialize(self) -> None:
        if self._initialized:
            return
        try:
            from backend.clients.reactor_core_client import ReactorCoreClient
            self._client = ReactorCoreClient()
            await self._client.connect()
            self._is_online = self._client.is_online
            self._initialized = True
            logger.info(f"ReactorCoreAdapter initialized (online={self._is_online})")
        except ImportError:
            logger.debug("Reactor Core client not available")
        except Exception as e:
            logger.warning(f"Reactor Core connection failed: {e}")
            self._initialized = True  # Mark as initialized but offline

    async def stream_experience(
        self,
        experience: Dict[str, Any],
    ) -> bool:
        """Stream a task experience to Reactor Core for learning."""
        if not self._initialized:
            await self.initialize()
        if not self._client or not self._is_online:
            return False

        try:
            return await self._client.stream_experience(experience)
        except Exception as e:
            logger.debug(f"Experience streaming failed: {e}")
            return False

    async def trigger_training(
        self,
        experience_count: int,
        priority: str = "normal",
        force: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Trigger a training run in Reactor Core."""
        if not self._client or not self._is_online:
            return None

        try:
            from backend.clients.reactor_core_client import TrainingPriority
            priority_enum = TrainingPriority(priority.lower())
            return await self._client.trigger_training(
                experience_count=experience_count,
                priority=priority_enum,
                force=force,
            )
        except Exception as e:
            logger.debug(f"Training trigger failed: {e}")
            return None

    async def add_learning_topic(
        self,
        topic: str,
        category: str = "general",
        priority: int = 5,
    ) -> bool:
        """Add a learning topic for Safe Scout to research."""
        if not self._client or not self._is_online:
            return False

        try:
            return await self._client.add_learning_topic(
                topic=topic,
                category=category,
                priority=priority,
                added_by="cross_repo_hub",
            )
        except Exception as e:
            logger.debug(f"Learning topic submission failed: {e}")
            return False

    @property
    def is_online(self) -> bool:
        return self._is_online

    @property
    def is_available(self) -> bool:
        return self._initialized


class JARVISPrimeAdapter:
    """
    Adapter for JARVIS Prime integration.

    JARVIS Prime is the orchestration layer for complex multi-step tasks:
    - Hierarchical task decomposition
    - Parallel agent coordination
    - Cross-system workflow management
    """

    def __init__(self, config: Optional["CrossRepoHubConfig"] = None):
        self.config = config or CrossRepoHubConfig()
        self._orchestrator = None
        self._client = None
        self._initialized = False
        self._is_online = False

    async def initialize(self) -> None:
        if self._initialized:
            return

        # Try orchestrator first (local integration)
        try:
            from backend.prime.jarvis_prime_orchestrator import JARVISPrimeOrchestrator
            self._orchestrator = JARVISPrimeOrchestrator()
            await self._orchestrator.initialize()
            self._is_online = True
            self._initialized = True
            logger.info("JARVISPrimeAdapter initialized (orchestrator mode)")
            return
        except ImportError:
            pass
        except Exception as e:
            logger.debug(f"JARVIS Prime orchestrator init failed: {e}")

        # Try HTTP client (remote integration)
        try:
            import aiohttp
            prime_url = os.getenv("JARVIS_PRIME_URL", "http://localhost:8002")
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{prime_url}/health", timeout=aiohttp.ClientTimeout(total=2.0)) as resp:
                    if resp.status == 200:
                        self._client = {"url": prime_url}
                        self._is_online = True
                        logger.info(f"JARVISPrimeAdapter initialized (client mode: {prime_url})")
        except Exception as e:
            logger.debug(f"JARVIS Prime client connection failed: {e}")

        self._initialized = True

    async def execute_workflow(
        self,
        workflow_name: str,
        context: Dict[str, Any],
        timeout_sec: float = 300.0,
    ) -> Dict[str, Any]:
        """Execute a workflow via JARVIS Prime."""
        if not self._initialized:
            await self.initialize()

        if self._orchestrator:
            # Direct orchestrator call
            try:
                result = await self._orchestrator.execute_workflow(
                    workflow_name=workflow_name,
                    context=context,
                )
                return result
            except Exception as e:
                return {"success": False, "error": str(e)}

        if self._client:
            # HTTP client call
            try:
                import aiohttp
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        f"{self._client['url']}/api/workflow/execute",
                        json={"workflow": workflow_name, "context": context},
                        timeout=aiohttp.ClientTimeout(total=timeout_sec),
                    ) as resp:
                        if resp.status == 200:
                            return await resp.json()
                        return {"success": False, "error": f"HTTP {resp.status}"}
            except Exception as e:
                return {"success": False, "error": str(e)}

        return {"success": False, "error": "JARVIS Prime not available"}

    async def decompose_task(
        self,
        task: str,
        complexity_threshold: float = 0.5,
    ) -> List[Dict[str, Any]]:
        """Decompose a complex task into subtasks."""
        if not self._initialized:
            await self.initialize()

        if self._orchestrator and hasattr(self._orchestrator, "decompose_task"):
            try:
                return await self._orchestrator.decompose_task(
                    task=task,
                    complexity_threshold=complexity_threshold,
                )
            except Exception as e:
                logger.debug(f"Task decomposition failed: {e}")

        # Fallback: return single task
        return [{"task": task, "priority": 1, "dependencies": []}]

    @property
    def is_online(self) -> bool:
        return self._is_online

    @property
    def is_available(self) -> bool:
        return self._initialized


class SpatialAwarenessAdapter:
    """
    Adapter for 3D OS Awareness (Proprioception).

    v6.2 Grand Unification: Provides spatial context and smart app switching
    via Yabai integration for the entire JARVIS ecosystem.

    Capabilities:
    - Get current spatial context (Space, Window, App)
    - Smart app switching with Yabai teleportation
    - Cross-repo spatial state sharing
    """

    def __init__(self, config: Optional["CrossRepoHubConfig"] = None):
        self.config = config or CrossRepoHubConfig()
        self._proprioception = None
        self._initialized = False
        self._is_available = False

    async def initialize(self) -> None:
        if self._initialized:
            return
        try:
            from core.computer_use_bridge import (
                get_current_context,
                switch_to_app_smart,
                get_spatial_manager,
                SwitchResult,
            )
            self._proprioception = {
                "get_context": get_current_context,
                "switch_to_app": switch_to_app_smart,
                "get_manager": get_spatial_manager,
                "SwitchResult": SwitchResult,
            }
            # Test if spatial manager is available
            manager = await get_spatial_manager()
            self._is_available = manager is not None
            self._initialized = True
            logger.info(f"SpatialAwarenessAdapter initialized (available={self._is_available})")
        except ImportError as e:
            logger.info(f"Spatial Awareness (Proprioception) not available: {e}")
            self._initialized = True

    async def get_spatial_context(self) -> Optional[Dict[str, Any]]:
        """Get current 3D OS context (proprioception)."""
        if not self._initialized:
            await self.initialize()
        if not self._proprioception or not self._is_available:
            return None

        try:
            get_context = self._proprioception["get_context"]
            context = await get_context()
            if context:
                return {
                    "current_space_id": context.current_space_id,
                    "current_display_id": context.current_display_id,
                    "total_spaces": context.total_spaces,
                    "focused_app": context.focused_app,
                    "focused_window": (
                        {
                            "window_id": context.focused_window.window_id,
                            "app_name": context.focused_window.app_name,
                            "title": context.focused_window.title,
                        }
                        if context.focused_window
                        else None
                    ),
                    "app_locations": dict(context.app_locations),
                    "timestamp": context.timestamp,
                    "context_prompt": context.get_context_prompt(),
                }
        except Exception as e:
            logger.debug(f"Error getting spatial context: {e}")
        return None

    async def switch_to_app(
        self,
        app_name: str,
        narrate: bool = True,
    ) -> Dict[str, Any]:
        """Switch to an app using Yabai teleportation."""
        if not self._initialized:
            await self.initialize()
        if not self._proprioception or not self._is_available:
            return {"success": False, "error": "Spatial Awareness not available"}

        try:
            switch_fn = self._proprioception["switch_to_app"]
            SwitchResult = self._proprioception["SwitchResult"]

            result = await switch_fn(app_name, narrate=narrate)

            is_success = result.result in (
                SwitchResult.SUCCESS,
                SwitchResult.ALREADY_FOCUSED,
                SwitchResult.SWITCHED_SPACE,
                SwitchResult.LAUNCHED_APP,
            )

            return {
                "success": is_success,
                "result": result.result.value,
                "app_name": result.app_name,
                "from_space": result.from_space,
                "to_space": result.to_space,
                "space_changed": result.from_space != result.to_space,
                "narration": result.narration,
            }
        except Exception as e:
            logger.debug(f"Error switching to {app_name}: {e}")
            return {"success": False, "error": str(e)}

    async def find_app(self, app_name: str) -> Optional[Dict[str, Any]]:
        """Find which Space(s) an app is on."""
        context = await self.get_spatial_context()
        if not context:
            return None

        app_locations = context.get("app_locations", {})
        for app, spaces in app_locations.items():
            if app.lower() == app_name.lower():
                return {
                    "app_name": app,
                    "spaces": spaces,
                    "found": True,
                    "is_focused": context.get("focused_app", "").lower() == app_name.lower(),
                }
        return {"app_name": app_name, "spaces": [], "found": False}

    @property
    def is_available(self) -> bool:
        return self._initialized and self._is_available


class VoiceLearningAdapter:
    """
    Adapter for Voice Unified Learning Loop.

    v81.0 Trinity Integration: Connects voice authentication experiences
    to the training pipeline via Reactor-Core.

    Capabilities:
    - Collect voice authentication experiences
    - Deploy new voice models with A/B testing
    - Automatic rollback on degradation
    - Forward experiences to Reactor-Core training queue
    """

    def __init__(self, config: Optional["CrossRepoHubConfig"] = None):
        self.config = config or CrossRepoHubConfig()
        self._experience_collector = None
        self._model_deployer = None
        self._ab_testing_manager = None
        self._initialized = False
        self._is_available = False

    async def initialize(self) -> None:
        if self._initialized:
            return
        try:
            from backend.voice_unlock.learning import (
                get_voice_experience_collector,
                get_voice_model_deployer,
            )
            from backend.voice_unlock.testing import get_ab_testing_manager

            self._experience_collector = await get_voice_experience_collector()
            self._model_deployer = await get_voice_model_deployer()
            self._ab_testing_manager = await get_ab_testing_manager()
            self._is_available = True
            self._initialized = True
            logger.info("VoiceLearningAdapter initialized")
        except ImportError as e:
            logger.info(f"Voice Learning Loop not available: {e}")
            self._initialized = True
        except Exception as e:
            logger.warning(f"Voice Learning Loop initialization failed: {e}")
            self._initialized = True

    async def collect_experience(
        self,
        session_id: str,
        user_id: str,
        embedding: List[float],
        outcome: str,
        confidence: float,
        audio_quality_metrics: Optional[Dict[str, float]] = None,
        environmental_context: Optional[Dict[str, Any]] = None,
        reasoning_trace: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[str]:
        """
        Collect a voice authentication experience for training.

        Returns experience_id if successful, None otherwise.
        """
        if not self._initialized:
            await self.initialize()
        if not self._experience_collector:
            return None

        try:
            from backend.voice_unlock.learning import ExperienceOutcome
            outcome_enum = ExperienceOutcome(outcome.lower())
            return await self._experience_collector.collect(
                session_id=session_id,
                user_id=user_id,
                embedding=embedding,
                outcome=outcome_enum,
                confidence=confidence,
                audio_quality_metrics=audio_quality_metrics,
                environmental_context=environmental_context,
                reasoning_trace=reasoning_trace,
            )
        except Exception as e:
            logger.debug(f"Experience collection failed: {e}")
            return None

    async def deploy_model(
        self,
        model_path: str,
        model_type: str,
        strategy: str = "ab_test",
        version_id: Optional[str] = None,
        metrics: Optional[Dict[str, float]] = None,
        traffic_percentage: float = 0.1,
    ) -> Optional[Dict[str, Any]]:
        """
        Deploy a new voice model.

        Strategies: immediate, gradual, ab_test, shadow, canary
        """
        if not self._initialized:
            await self.initialize()
        if not self._model_deployer:
            return None

        try:
            from backend.voice_unlock.learning import (
                ModelType,
                DeploymentStrategy,
            )
            from pathlib import Path

            model_type_enum = ModelType(model_type.lower())
            strategy_enum = DeploymentStrategy(strategy.lower())

            result = await self._model_deployer.deploy_model(
                model_path=Path(model_path),
                model_type=model_type_enum,
                strategy=strategy_enum,
                version_id=version_id,
                metrics=metrics,
                traffic_percentage=traffic_percentage,
            )
            return {
                "success": result.success,
                "version_id": result.version_id,
                "deployment_id": result.deployment_id,
                "ab_test_id": result.ab_test_id,
                "message": result.message,
            }
        except Exception as e:
            logger.debug(f"Model deployment failed: {e}")
            return None

    async def start_ab_test(
        self,
        test_id: str,
        control_version: str,
        treatment_version: str,
        traffic_split: float = 0.1,
        min_sample_size: int = 100,
        confidence_level: float = 0.95,
        metric_name: str = "accuracy",
        duration_hours: float = 24.0,
    ) -> Optional[Dict[str, Any]]:
        """Start an A/B test between two model versions."""
        if not self._initialized:
            await self.initialize()
        if not self._ab_testing_manager:
            return None

        try:
            test = await self._ab_testing_manager.start_test(
                test_id=test_id,
                control_version=control_version,
                treatment_version=treatment_version,
                traffic_split=traffic_split,
                min_sample_size=min_sample_size,
                confidence_level=confidence_level,
                metric_name=metric_name,
                duration_hours=duration_hours,
            )
            if test:
                return {
                    "test_id": test.config.test_id,
                    "status": test.status.value,
                    "control": test.config.control_version,
                    "treatment": test.config.treatment_version,
                    "traffic_split": test.config.traffic_split,
                }
            return None
        except Exception as e:
            logger.debug(f"A/B test start failed: {e}")
            return None

    async def get_ab_test_variant(
        self,
        test_id: str,
        user_id: str,
    ) -> Optional[str]:
        """Get the variant for a user in an A/B test."""
        if not self._ab_testing_manager:
            return None

        try:
            variant = await self._ab_testing_manager.get_variant(test_id, user_id)
            return variant.value if variant else None
        except Exception:
            return None

    async def record_ab_metric(
        self,
        test_id: str,
        user_id: str,
        metric_value: float,
        is_success: bool,
    ) -> bool:
        """Record a metric observation for an A/B test."""
        if not self._ab_testing_manager:
            return False

        try:
            return await self._ab_testing_manager.record_metric(
                test_id=test_id,
                user_id=user_id,
                metric_value=metric_value,
                is_success=is_success,
            )
        except Exception:
            return False

    async def evaluate_ab_test(
        self,
        test_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Evaluate an A/B test and determine winner."""
        if not self._ab_testing_manager:
            return None

        try:
            result = await self._ab_testing_manager.evaluate_test(test_id)
            if result:
                return {
                    "test_id": result.test_id,
                    "winner": result.winner.value if result.winner else None,
                    "control_mean": result.control_mean,
                    "treatment_mean": result.treatment_mean,
                    "control_samples": result.control_samples,
                    "treatment_samples": result.treatment_samples,
                    "p_value": result.p_value,
                    "is_significant": result.is_significant,
                    "effect_size": result.effect_size,
                    "recommendation": result.recommendation,
                }
            return None
        except Exception:
            return None

    async def rollback_model(
        self,
        model_type: str,
        to_version: Optional[str] = None,
    ) -> bool:
        """Rollback to a previous model version."""
        if not self._model_deployer:
            return False

        try:
            from backend.voice_unlock.learning import ModelType
            model_type_enum = ModelType(model_type.lower())
            return await self._model_deployer.rollback(model_type_enum, to_version)
        except Exception:
            return False

    async def get_experience_stats(self) -> Optional[Dict[str, Any]]:
        """Get statistics about collected experiences."""
        if not self._experience_collector:
            return None

        try:
            return await self._experience_collector.get_stats()
        except Exception:
            return None

    @property
    def is_available(self) -> bool:
        return self._initialized and self._is_available


# ============================================================================
# Cross-Repo Intelligence Hub
# ============================================================================

class CrossRepoIntelligenceHub:
    """
    Central hub for cross-repository intelligence coordination.

    Orchestrates all integrated systems:
    - Repository Intelligence (Aider-inspired)
    - Computer Use Refinements (Open Interpreter-inspired)
    - SOP Enforcement (MetaGPT-inspired)
    - Unified Memory (MemGPT-inspired)
    - Wisdom Patterns (Fabric-inspired)
    """

    def __init__(self, config: Optional[CrossRepoHubConfig] = None):
        self.config = config or CrossRepoHubConfig()

        # System adapters (lazy-loaded)
        self._repo_adapter = RepositoryIntelligenceAdapter()
        self._computer_adapter = ComputerUseAdapter()
        self._sop_adapter = SOPAdapter()
        self._memory_adapter = MemoryAdapter()
        self._wisdom_adapter = WisdomAdapter()

        # v6.0: New adapters for enhanced integration
        self._safe_code_adapter = SafeCodeAdapter()
        self._reactor_core_adapter = ReactorCoreAdapter(config)
        self._jarvis_prime_adapter = JARVISPrimeAdapter(config)

        # v6.2: Grand Unification - Spatial Awareness (Proprioception)
        self._spatial_adapter = SpatialAwarenessAdapter(config)

        # v81.0: Voice Unified Learning Loop
        self._voice_learning_adapter = VoiceLearningAdapter(config)

        # Event handling
        self._event_handlers: Dict[EventType, List[Callable]] = {}
        self._event_queue: asyncio.Queue = asyncio.Queue()

        # Task processing
        self._task_queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._active_tasks: Dict[str, IntelligenceTask] = {}
        self._task_semaphore = asyncio.Semaphore(self.config.max_concurrent_tasks)

        # State
        self._start_time = time.time()
        self._events_processed = 0
        self._initialized = False
        self._running = False
        self._lock = asyncio.Lock()

    # -------------------------------------------------------------------------
    # Initialization
    # -------------------------------------------------------------------------

    async def initialize(self) -> None:
        """Initialize all enabled systems."""
        async with self._lock:
            if self._initialized:
                return

            init_tasks = []

            if self.config.enable_repository_intelligence:
                init_tasks.append(self._repo_adapter.initialize())

            if self.config.enable_computer_use:
                init_tasks.append(self._computer_adapter.initialize())

            if self.config.enable_sop_enforcement:
                init_tasks.append(self._sop_adapter.initialize())

            if self.config.enable_memory_system:
                init_tasks.append(self._memory_adapter.initialize())

            if self.config.enable_wisdom_patterns:
                init_tasks.append(self._wisdom_adapter.initialize())

            # v6.2: Always try to initialize spatial awareness (Proprioception)
            init_tasks.append(self._spatial_adapter.initialize())

            # v81.0: Always try to initialize voice learning loop
            init_tasks.append(self._voice_learning_adapter.initialize())

            if init_tasks:
                await asyncio.gather(*init_tasks, return_exceptions=True)

            self._initialized = True
            logger.info(f"CrossRepoIntelligenceHub initialized with {len(self.active_systems)} active systems")

    async def start(self) -> None:
        """Start the hub's background processing."""
        await self.initialize()
        self._running = True
        asyncio.create_task(self._process_events())
        asyncio.create_task(self._process_tasks())

    async def stop(self) -> None:
        """Stop the hub's background processing."""
        self._running = False

    # -------------------------------------------------------------------------
    # Event Handling
    # -------------------------------------------------------------------------

    def subscribe(self, event_type: EventType, handler: Callable) -> None:
        """Subscribe to an event type."""
        if event_type not in self._event_handlers:
            self._event_handlers[event_type] = []
        self._event_handlers[event_type].append(handler)

    def unsubscribe(self, event_type: EventType, handler: Callable) -> None:
        """Unsubscribe from an event type."""
        if event_type in self._event_handlers:
            self._event_handlers[event_type] = [
                h for h in self._event_handlers[event_type] if h != handler
            ]

    async def emit(self, event: HubEvent) -> None:
        """Emit an event to all subscribers."""
        await self._event_queue.put(event)

    async def _process_events(self) -> None:
        """Process events from the queue."""
        while self._running:
            try:
                event = await asyncio.wait_for(
                    self._event_queue.get(),
                    timeout=1.0,
                )
                handlers = self._event_handlers.get(event.event_type, [])
                for handler in handlers:
                    try:
                        if asyncio.iscoroutinefunction(handler):
                            await handler(event)
                        else:
                            handler(event)
                    except Exception as e:
                        logger.error(f"Event handler error: {e}")

                self._events_processed += 1

            except asyncio.TimeoutError:
                continue

    # -------------------------------------------------------------------------
    # Task Processing
    # -------------------------------------------------------------------------

    async def submit_task(self, task: IntelligenceTask) -> str:
        """Submit a task for processing."""
        self._active_tasks[task.task_id] = task
        await self._task_queue.put((
            -task.priority.value,  # Negative for max-heap behavior
            task.created_at.timestamp(),
            task,
        ))
        return task.task_id

    async def _process_tasks(self) -> None:
        """Process tasks from the queue."""
        while self._running:
            try:
                _, _, task = await asyncio.wait_for(
                    self._task_queue.get(),
                    timeout=1.0,
                )
                asyncio.create_task(self._execute_task(task))

            except asyncio.TimeoutError:
                continue

    async def _execute_task(self, task: IntelligenceTask) -> TaskResult:
        """Execute a task across specified systems."""
        async with self._task_semaphore:
            start_time = time.time()
            results: Dict[IntelligenceSystem, Any] = {}
            errors: Dict[IntelligenceSystem, str] = {}
            events: List[HubEvent] = []

            async def run_system(system: IntelligenceSystem):
                try:
                    result = await self._execute_on_system(system, task.input_data)
                    results[system] = result
                except Exception as e:
                    errors[system] = str(e)
                    logger.error(f"System {system.value} error: {e}")

            if self.config.parallel_processing:
                await asyncio.gather(*[
                    run_system(s) for s in task.systems
                ], return_exceptions=True)
            else:
                for system in task.systems:
                    await run_system(system)

            duration_ms = (time.time() - start_time) * 1000

            task_result = TaskResult(
                task_id=task.task_id,
                success=len(errors) == 0,
                results=results,
                errors=errors,
                duration_ms=duration_ms,
                events=events,
            )

            # Cleanup
            self._active_tasks.pop(task.task_id, None)

            # Callback
            if task.callback:
                try:
                    if asyncio.iscoroutinefunction(task.callback):
                        await task.callback(task_result)
                    else:
                        task.callback(task_result)
                except Exception as e:
                    logger.error(f"Task callback error: {e}")

            return task_result

    async def _execute_on_system(
        self,
        system: IntelligenceSystem,
        input_data: Dict[str, Any],
    ) -> Any:
        """Execute on a specific system."""
        if system == IntelligenceSystem.REPOSITORY:
            return await self._repo_adapter.get_repo_map(
                repository=input_data.get("repository", "JARVIS-AI-Agent"),
                max_tokens=input_data.get("max_tokens", 4000),
            )

        elif system == IntelligenceSystem.COMPUTER_USE:
            return await self._computer_adapter.execute_tool(
                tool_name=input_data.get("tool", "screenshot"),
                **input_data.get("tool_params", {}),
            )

        elif system == IntelligenceSystem.SOP:
            return await self._sop_adapter.execute_sop(
                sop_name=input_data.get("sop", "code_review"),
                llm=input_data.get("llm"),
                context=input_data.get("context", ""),
            )

        elif system == IntelligenceSystem.MEMORY:
            action = input_data.get("action", "retrieve")
            if action == "store":
                return await self._memory_adapter.store(
                    key=input_data.get("key", ""),
                    content=input_data.get("content", ""),
                    metadata=input_data.get("metadata"),
                )
            else:
                return await self._memory_adapter.retrieve(
                    query=input_data.get("query", ""),
                    limit=input_data.get("limit", 5),
                )

        elif system == IntelligenceSystem.WISDOM:
            return await self._wisdom_adapter.enhance_prompt(
                task=input_data.get("task", ""),
                pattern_name=input_data.get("pattern"),
                input_text=input_data.get("input", ""),
            )

        return None

    # -------------------------------------------------------------------------
    # Convenience Methods
    # -------------------------------------------------------------------------

    async def enrich_context(
        self,
        task: str,
        repository: str = "JARVIS-AI-Agent",
        include_repo_map: bool = True,
        include_memory: bool = True,
        enhance_with_wisdom: bool = True,
    ) -> Dict[str, Any]:
        """
        Enrich a task context with all available intelligence.

        This is the main entry point for getting comprehensive context
        for any task across all systems.
        """
        await self.initialize()

        context: Dict[str, Any] = {
            "task": task,
            "timestamp": datetime.now().isoformat(),
            "enrichments": {},
        }

        async def add_repo_map():
            if include_repo_map and self._repo_adapter.is_available:
                repo_map = await self._repo_adapter.get_repo_map(repository)
                if repo_map:
                    context["enrichments"]["repository_map"] = repo_map

        async def add_memory():
            if include_memory and self._memory_adapter.is_available:
                memories = await self._memory_adapter.retrieve(task, limit=5)
                if memories:
                    context["enrichments"]["relevant_memories"] = memories

        async def add_wisdom():
            if enhance_with_wisdom and self._wisdom_adapter.is_available:
                pattern = await self._wisdom_adapter.suggest_pattern(task)
                if pattern:
                    enhanced = await self._wisdom_adapter.enhance_prompt(task)
                    context["enrichments"]["wisdom_pattern"] = pattern
                    context["enrichments"]["enhanced_prompt"] = enhanced

        # Run in parallel
        await asyncio.gather(
            add_repo_map(),
            add_memory(),
            add_wisdom(),
            return_exceptions=True,
        )

        # Emit event
        await self.emit(HubEvent(
            event_type=EventType.CONTEXT_ENRICHED,
            source_system=IntelligenceSystem.REASONING,
            payload={"task": task, "enrichment_count": len(context["enrichments"])},
        ))

        return context

    async def execute_with_sop(
        self,
        sop_name: str,
        context: str,
        llm: Any,
        enrich_context: bool = True,
    ) -> Dict[str, Any]:
        """Execute a task using an SOP with full intelligence support."""
        await self.initialize()

        if enrich_context:
            enriched = await self.enrich_context(context)
            context = f"{context}\n\n## Intelligence Context\n{json.dumps(enriched['enrichments'], indent=2)}"

        result = await self._sop_adapter.execute_sop(sop_name, llm, context)

        # Store result in memory
        if result and self._memory_adapter.is_available:
            await self._memory_adapter.store(
                key=f"sop_{sop_name}_{datetime.now().isoformat()}",
                content=json.dumps(result),
                metadata={"sop": sop_name},
            )

        return result or {}

    # -------------------------------------------------------------------------
    # Voice Learning Pipeline (v81.0)
    # -------------------------------------------------------------------------

    async def handle_voice_experience(
        self,
        session_id: str,
        user_id: str,
        embedding: List[float],
        outcome: str,
        confidence: float,
        audio_quality_metrics: Optional[Dict[str, float]] = None,
        environmental_context: Optional[Dict[str, Any]] = None,
        reasoning_trace: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[str]:
        """
        Handle a voice authentication experience.

        Collects the experience and forwards it to Reactor-Core training queue.
        Returns experience_id if successful.
        """
        await self.initialize()

        # Collect experience via voice learning adapter
        experience_id = await self._voice_learning_adapter.collect_experience(
            session_id=session_id,
            user_id=user_id,
            embedding=embedding,
            outcome=outcome,
            confidence=confidence,
            audio_quality_metrics=audio_quality_metrics,
            environmental_context=environmental_context,
            reasoning_trace=reasoning_trace,
        )

        if experience_id:
            # Emit experience collected event
            await self.emit(HubEvent(
                event_type=EventType.VOICE_EXPERIENCE_COLLECTED,
                source_system=IntelligenceSystem.VOICE_LEARNING,
                payload={
                    "experience_id": experience_id,
                    "session_id": session_id,
                    "user_id": user_id,
                    "outcome": outcome,
                    "confidence": confidence,
                },
            ))

            # Also stream to Reactor-Core if available
            if self._reactor_core_adapter.is_online:
                await self._reactor_core_adapter.stream_experience({
                    "type": "voice_authentication",
                    "experience_id": experience_id,
                    "embedding": embedding,
                    "outcome": outcome,
                    "confidence": confidence,
                    "audio_metrics": audio_quality_metrics,
                    "context": environmental_context,
                })

        return experience_id

    async def handle_model_update(
        self,
        model_path: str,
        model_type: str,
        strategy: str = "ab_test",
        metrics: Optional[Dict[str, float]] = None,
        traffic_percentage: float = 0.1,
    ) -> Optional[Dict[str, Any]]:
        """
        Handle a model update from Reactor-Core.

        Deploys the new model with the specified strategy.
        """
        await self.initialize()

        result = await self._voice_learning_adapter.deploy_model(
            model_path=model_path,
            model_type=model_type,
            strategy=strategy,
            metrics=metrics,
            traffic_percentage=traffic_percentage,
        )

        if result and result.get("success"):
            event_type = EventType.MODEL_DEPLOYED
            if result.get("ab_test_id"):
                event_type = EventType.AB_TEST_STARTED

            await self.emit(HubEvent(
                event_type=event_type,
                source_system=IntelligenceSystem.VOICE_LEARNING,
                payload={
                    "model_type": model_type,
                    "strategy": strategy,
                    "version_id": result.get("version_id"),
                    "deployment_id": result.get("deployment_id"),
                    "ab_test_id": result.get("ab_test_id"),
                },
            ))

        return result

    async def trigger_voice_training(
        self,
        force: bool = False,
        priority: str = "normal",
    ) -> Optional[Dict[str, Any]]:
        """
        Trigger voice model training in Reactor-Core.

        Returns training status if triggered.
        """
        await self.initialize()

        # Get experience stats to determine if training is warranted
        stats = await self._voice_learning_adapter.get_experience_stats()
        experience_count = stats.get("total_experiences", 0) if stats else 0

        if not force and experience_count < 100:
            logger.info(f"Skipping training trigger: only {experience_count} experiences")
            return {"triggered": False, "reason": "insufficient_data", "count": experience_count}

        result = await self._reactor_core_adapter.trigger_training(
            experience_count=experience_count,
            priority=priority,
            force=force,
        )

        if result:
            await self.emit(HubEvent(
                event_type=EventType.TRAINING_TRIGGERED,
                source_system=IntelligenceSystem.VOICE_LEARNING,
                payload={
                    "experience_count": experience_count,
                    "priority": priority,
                    "force": force,
                    "training_id": result.get("training_id"),
                },
            ))

        return result

    async def evaluate_voice_ab_test(
        self,
        test_id: str,
        auto_apply: bool = True,
    ) -> Optional[Dict[str, Any]]:
        """
        Evaluate a voice model A/B test.

        If auto_apply=True and there's a clear winner, automatically
        deploys the winning variant.
        """
        await self.initialize()

        result = await self._voice_learning_adapter.evaluate_ab_test(test_id)

        if result:
            await self.emit(HubEvent(
                event_type=EventType.AB_TEST_COMPLETED,
                source_system=IntelligenceSystem.VOICE_LEARNING,
                payload={
                    "test_id": test_id,
                    "winner": result.get("winner"),
                    "is_significant": result.get("is_significant"),
                    "p_value": result.get("p_value"),
                    "recommendation": result.get("recommendation"),
                },
            ))

            # Auto-apply winner if enabled and significant
            if auto_apply and result.get("is_significant") and result.get("winner"):
                winner = result["winner"]
                if winner == "treatment":
                    # Get test config to find treatment version
                    # and deploy it as the new production model
                    logger.info(f"Auto-applying A/B test winner: {test_id} -> treatment")
                    # Note: Actual deployment would need the model path
                    # This is handled by the A/B testing manager's auto-promotion

        return result

    # -------------------------------------------------------------------------
    # State and Health
    # -------------------------------------------------------------------------

    @property
    def active_systems(self) -> Set[IntelligenceSystem]:
        """Get set of active systems."""
        active = set()
        if self._repo_adapter.is_available:
            active.add(IntelligenceSystem.REPOSITORY)
        if self._computer_adapter.is_available:
            active.add(IntelligenceSystem.COMPUTER_USE)
        if self._sop_adapter.is_available:
            active.add(IntelligenceSystem.SOP)
        if self._memory_adapter.is_available:
            active.add(IntelligenceSystem.MEMORY)
        if self._wisdom_adapter.is_available:
            active.add(IntelligenceSystem.WISDOM)
        # v6.2: Spatial Awareness (Proprioception)
        if self._spatial_adapter.is_available:
            active.add(IntelligenceSystem.SPATIAL)
        # v81.0: Voice Learning Loop
        if self._voice_learning_adapter.is_available:
            active.add(IntelligenceSystem.VOICE_LEARNING)
        return active

    def get_state(self) -> HubState:
        """Get current hub state."""
        return HubState(
            active_systems=self.active_systems,
            pending_tasks=self._task_queue.qsize(),
            total_events_processed=self._events_processed,
            uptime_seconds=time.time() - self._start_time,
            last_activity=datetime.now(),
            system_health={
                IntelligenceSystem.REPOSITORY: self._repo_adapter.is_available,
                IntelligenceSystem.COMPUTER_USE: self._computer_adapter.is_available,
                IntelligenceSystem.SOP: self._sop_adapter.is_available,
                IntelligenceSystem.MEMORY: self._memory_adapter.is_available,
                IntelligenceSystem.WISDOM: self._wisdom_adapter.is_available,
                IntelligenceSystem.SPATIAL: self._spatial_adapter.is_available,  # v6.2
                IntelligenceSystem.VOICE_LEARNING: self._voice_learning_adapter.is_available,  # v81.0
            },
        )

    def get_available_sops(self) -> List[str]:
        """Get list of available SOPs."""
        return self._sop_adapter.list_sops() if self._sop_adapter.is_available else []


# ============================================================================
# Singleton and Convenience Functions
# ============================================================================

_hub_instance: Optional[CrossRepoIntelligenceHub] = None


async def get_intelligence_hub() -> CrossRepoIntelligenceHub:
    """Get the singleton intelligence hub."""
    global _hub_instance
    if _hub_instance is None:
        _hub_instance = CrossRepoIntelligenceHub()
        await _hub_instance.initialize()
    return _hub_instance


async def enrich_task_context(
    task: str,
    repository: str = "JARVIS-AI-Agent",
) -> Dict[str, Any]:
    """Convenience function to enrich task context."""
    hub = await get_intelligence_hub()
    return await hub.enrich_context(task, repository)


async def execute_sop(sop_name: str, context: str, llm: Any) -> Dict[str, Any]:
    """Convenience function to execute an SOP."""
    hub = await get_intelligence_hub()
    return await hub.execute_with_sop(sop_name, context, llm)


# ============================================================================
# Module Exports
# ============================================================================

__all__ = [
    # Configuration
    "CrossRepoHubConfig",

    # Enums
    "IntelligenceSystem",
    "EventType",
    "TaskPriority",

    # Data Classes
    "HubEvent",
    "IntelligenceTask",
    "TaskResult",
    "HubState",

    # Core Class
    "CrossRepoIntelligenceHub",

    # Adapters
    "RepositoryIntelligenceAdapter",
    "ComputerUseAdapter",
    "SOPAdapter",
    "MemoryAdapter",
    "WisdomAdapter",
    # v6.0: New adapters
    "SafeCodeAdapter",
    "ReactorCoreAdapter",
    "JARVISPrimeAdapter",

    # v6.2: Grand Unification
    "SpatialAwarenessAdapter",

    # v81.0: Voice Learning Loop
    "VoiceLearningAdapter",

    # Convenience Functions
    "get_intelligence_hub",
    "enrich_task_context",
    "execute_sop",
]
