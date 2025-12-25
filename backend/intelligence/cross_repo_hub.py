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

    # Cross-System
    CONTEXT_ENRICHED = "context_enriched"
    STATE_SYNCHRONIZED = "state_synchronized"


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

    # Convenience Functions
    "get_intelligence_hub",
    "enrich_task_context",
    "execute_sop",
]
