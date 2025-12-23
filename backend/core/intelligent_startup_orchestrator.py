#!/usr/bin/env python3
"""
JARVIS Intelligent Startup Orchestrator v1.0.0
==============================================

Production-grade, async, parallel startup system that:
- Uses progressive readiness levels for faster startup
- Loads components in parallel with dependency resolution
- Implements intelligent timeout adaptation
- Provides real-time progress to frontend
- Gracefully degrades when services are slow

Architecture:
┌─────────────────────────────────────────────────────────────────────────────┐
│                    Intelligent Startup Orchestrator                          │
├─────────────────────────────────────────────────────────────────────────────┤
│  ┌────────────────┐  ┌────────────────┐  ┌────────────────┐                 │
│  │ Progressive    │  │ Parallel       │  │ Adaptive       │                 │
│  │ Readiness      │  │ Initializer    │  │ Timeout        │                 │
│  │ Levels         │  │                │  │ Manager        │                 │
│  └───────┬────────┘  └───────┬────────┘  └───────┬────────┘                 │
│          └───────────────────┼───────────────────┘                          │
│                              ▼                                               │
│          ┌─────────────────────────────────────────┐                        │
│          │    Readiness Level Definitions          │                        │
│          │  • L0: HTTP responding                  │                        │
│          │  • L1: WebSocket ready (interactive)    │                        │
│          │  • L2: Core services operational        │                        │
│          │  • L3: ML models warming                │                        │
│          │  • L4: Full mode (all features)         │                        │
│          └─────────────────────────────────────────┘                        │
│                              │                                               │
│          ┌───────────────────┼───────────────────┐                          │
│          ▼                   ▼                   ▼                          │
│  ┌────────────────┐  ┌────────────────┐  ┌────────────────┐                 │
│  │ Health         │  │ Progress       │  │ WebSocket      │                 │
│  │ Endpoints      │  │ Broadcast      │  │ Events         │                 │
│  └────────────────┘  └────────────────┘  └────────────────┘                 │
└─────────────────────────────────────────────────────────────────────────────┘

Key Insight:
The frontend only needs WebSocket to be ready for interaction. ML models
can continue loading in the background while the user interacts with JARVIS
in degraded mode.

Author: JARVIS System
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set, Tuple
import threading

logger = logging.getLogger(__name__)


# =============================================================================
# CONFIGURATION - Environment Driven
# =============================================================================

def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except (ValueError, TypeError):
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, default))
    except (ValueError, TypeError):
        return default


def _env_bool(key: str, default: bool) -> bool:
    val = os.environ.get(key, str(default)).lower()
    return val in ('true', '1', 'yes', 'on')


# =============================================================================
# READINESS LEVELS
# =============================================================================

class ReadinessLevel(IntEnum):
    """
    Progressive readiness levels for JARVIS startup.
    
    Higher levels include all capabilities of lower levels.
    The supervisor should accept INTERACTIVE (L2) as "ready for user".
    """
    NOT_STARTED = 0     # Nothing ready
    HTTP = 1            # HTTP server responding to /health
    WEBSOCKET = 2       # WebSocket manager initialized (frontend can connect)
    INTERACTIVE = 3     # Core services ready (can respond to basic commands)
    WARMING = 4         # ML models loading in background
    FULL = 5            # All services ready, optimal performance


# Minimum level for user interaction
MINIMUM_INTERACTION_LEVEL = ReadinessLevel.WEBSOCKET


@dataclass
class ReadinessState:
    """Current readiness state."""
    level: ReadinessLevel
    timestamp: datetime = field(default_factory=datetime.now)
    details: Dict[str, bool] = field(default_factory=dict)
    message: str = ""
    progress_percent: int = 0
    
    @property
    def is_interactive(self) -> bool:
        """True if user can start interacting."""
        return self.level >= MINIMUM_INTERACTION_LEVEL
    
    @property
    def is_fully_ready(self) -> bool:
        """True if all services are ready."""
        return self.level >= ReadinessLevel.FULL
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "level": self.level.name,
            "level_value": int(self.level),
            "is_interactive": self.is_interactive,
            "is_fully_ready": self.is_fully_ready,
            "details": self.details,
            "message": self.message,
            "progress_percent": self.progress_percent,
            "timestamp": self.timestamp.isoformat(),
        }


# =============================================================================
# COMPONENT DEFINITIONS
# =============================================================================

@dataclass
class ComponentDef:
    """Definition of a startup component."""
    name: str
    checker: Optional[Callable[[], Coroutine[Any, Any, bool]]] = None
    initializer: Optional[Callable[[], Coroutine[Any, Any, None]]] = None
    required_for_level: ReadinessLevel = ReadinessLevel.FULL
    dependencies: List[str] = field(default_factory=list)
    timeout: float = 30.0
    is_critical: bool = False  # If True, failure blocks higher levels
    
    # Runtime state
    is_ready: bool = False
    is_loading: bool = False
    error: Optional[str] = None
    start_time: Optional[float] = None
    end_time: Optional[float] = None
    
    @property
    def duration_ms(self) -> Optional[float]:
        if self.start_time and self.end_time:
            return (self.end_time - self.start_time) * 1000
        return None


# =============================================================================
# INTELLIGENT STARTUP ORCHESTRATOR
# =============================================================================

class IntelligentStartupOrchestrator:
    """
    Manages JARVIS startup with progressive readiness levels.
    
    Key Features:
    1. Progressive readiness - user can interact before ML is fully loaded
    2. Parallel component initialization with dependency resolution
    3. Adaptive timeouts based on system conditions
    4. Real-time progress broadcasting via WebSocket
    5. Graceful degradation on component failures
    """
    
    _instance: Optional["IntelligentStartupOrchestrator"] = None
    _lock = threading.Lock()
    
    def __new__(cls) -> "IntelligentStartupOrchestrator":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        
        # Components registry
        self._components: Dict[str, ComponentDef] = {}
        
        # State
        self._current_level = ReadinessLevel.NOT_STARTED
        self._state = ReadinessState(level=ReadinessLevel.NOT_STARTED)
        self._started_at: Optional[datetime] = None
        self._start_time: Optional[float] = None
        
        # Events
        self._level_events: Dict[ReadinessLevel, asyncio.Event] = {
            level: asyncio.Event() for level in ReadinessLevel
        }
        self._listeners: List[Callable[[ReadinessState], Coroutine]] = []
        
        # Configuration
        self._base_timeout = _env_float("STARTUP_BASE_TIMEOUT", 120.0)
        self._ml_timeout = _env_float("STARTUP_ML_TIMEOUT", 180.0)
        self._check_interval = _env_float("STARTUP_CHECK_INTERVAL", 0.5)
        
        # Background tasks
        self._background_tasks: List[asyncio.Task] = []
        
        # Register default components
        self._register_default_components()
        
        self._initialized = True
        logger.info("🚀 IntelligentStartupOrchestrator initialized")
    
    def _register_default_components(self):
        """Register the default JARVIS startup components."""
        # Level 1: HTTP (implicitly ready when server starts)
        
        # Level 2: WebSocket
        self.register_component(ComponentDef(
            name="websocket_manager",
            required_for_level=ReadinessLevel.WEBSOCKET,
            is_critical=True,
            timeout=10.0,
        ))
        
        # Level 3: Interactive
        self.register_component(ComponentDef(
            name="config",
            required_for_level=ReadinessLevel.INTERACTIVE,
            is_critical=True,
            timeout=5.0,
        ))
        
        self.register_component(ComponentDef(
            name="database",
            required_for_level=ReadinessLevel.INTERACTIVE,
            is_critical=False,
            timeout=30.0,
        ))
        
        # Level 4/5: ML and Voice
        self.register_component(ComponentDef(
            name="ml_models",
            required_for_level=ReadinessLevel.WARMING,
            is_critical=False,
            timeout=180.0,
        ))
        
        self.register_component(ComponentDef(
            name="speaker_verification",
            required_for_level=ReadinessLevel.FULL,
            is_critical=False,
            timeout=60.0,
            dependencies=["ml_models"],
        ))
        
        self.register_component(ComponentDef(
            name="voice_unlock",
            required_for_level=ReadinessLevel.FULL,
            is_critical=False,
            timeout=30.0,
            dependencies=["speaker_verification"],
        ))

        # Agentic System (Computer Use + UAE routing)
        self.register_component(ComponentDef(
            name="agentic_system",
            required_for_level=ReadinessLevel.FULL,
            is_critical=False,
            timeout=60.0,
            dependencies=["ml_models"],
        ))

    def register_component(self, component: ComponentDef):
        """Register a component for startup tracking."""
        self._components[component.name] = component
    
    def mark_component_ready(self, name: str, ready: bool = True, error: Optional[str] = None):
        """Mark a component as ready or failed."""
        if name in self._components:
            comp = self._components[name]
            comp.is_ready = ready
            comp.is_loading = False
            comp.error = error
            comp.end_time = time.time()
            
            if ready:
                logger.info(f"✅ Component ready: {name}")
            else:
                logger.warning(f"⚠️ Component failed: {name} - {error}")
    
    def mark_component_loading(self, name: str):
        """Mark a component as currently loading."""
        if name in self._components:
            comp = self._components[name]
            comp.is_loading = True
            comp.start_time = time.time()
    
    async def update_readiness_level(self) -> ReadinessLevel:
        """
        Calculate and update the current readiness level based on component states.
        
        Returns the new readiness level.
        """
        # Start with highest level and work down
        achieved_level = ReadinessLevel.FULL
        
        details = {}
        
        for level in reversed(list(ReadinessLevel)):
            if level == ReadinessLevel.NOT_STARTED:
                continue
            
            # Check all components required for this level
            level_ready = True
            for name, comp in self._components.items():
                if comp.required_for_level <= level:
                    details[name] = comp.is_ready
                    
                    if comp.is_critical and not comp.is_ready:
                        level_ready = False
                        achieved_level = ReadinessLevel(level - 1) if level > 1 else ReadinessLevel.NOT_STARTED
        
        # HTTP is always ready once we're running
        if achieved_level < ReadinessLevel.HTTP:
            achieved_level = ReadinessLevel.HTTP
        
        # Update state
        old_level = self._current_level
        self._current_level = achieved_level
        
        # Calculate progress
        total_components = len(self._components)
        ready_components = sum(1 for c in self._components.values() if c.is_ready)
        progress = int((ready_components / total_components) * 100) if total_components > 0 else 0
        
        # Build message
        if achieved_level >= ReadinessLevel.FULL:
            message = "All systems operational"
        elif achieved_level >= ReadinessLevel.WARMING:
            message = "ML models loading in background"
        elif achieved_level >= ReadinessLevel.INTERACTIVE:
            message = "Ready for basic interaction"
        elif achieved_level >= ReadinessLevel.WEBSOCKET:
            message = "WebSocket ready, services loading"
        elif achieved_level >= ReadinessLevel.HTTP:
            message = "Server starting..."
        else:
            message = "Initializing..."
        
        self._state = ReadinessState(
            level=achieved_level,
            details=details,
            message=message,
            progress_percent=progress,
        )
        
        # Trigger events if level increased
        if achieved_level > old_level:
            for level in range(old_level + 1, achieved_level + 1):
                if level in self._level_events:
                    self._level_events[ReadinessLevel(level)].set()
            
            logger.info(f"📊 Readiness level: {old_level.name} → {achieved_level.name} ({progress}%)")
            
            # Notify listeners
            await self._notify_listeners()
        
        return achieved_level
    
    async def _notify_listeners(self):
        """Notify all registered listeners of state change."""
        for listener in self._listeners:
            try:
                await listener(self._state)
            except Exception as e:
                logger.debug(f"Listener error: {e}")
    
    def add_listener(self, callback: Callable[[ReadinessState], Coroutine]):
        """Add a listener for readiness state changes."""
        self._listeners.append(callback)
    
    async def wait_for_level(self, level: ReadinessLevel, timeout: Optional[float] = None) -> bool:
        """
        Wait until a specific readiness level is achieved.
        
        Args:
            level: The readiness level to wait for
            timeout: Maximum time to wait (None = no timeout)
            
        Returns:
            True if level was achieved, False if timeout
        """
        if self._current_level >= level:
            return True
        
        try:
            if timeout is not None:
                await asyncio.wait_for(
                    self._level_events[level].wait(),
                    timeout=timeout
                )
            else:
                await self._level_events[level].wait()
            return True
        except asyncio.TimeoutError:
            return False
    
    async def wait_for_interactive(self, timeout: Optional[float] = None) -> bool:
        """Wait until the system is ready for user interaction."""
        # Minimum is WEBSOCKET level for interaction
        return await self.wait_for_level(ReadinessLevel.WEBSOCKET, timeout)
    
    def get_state(self) -> ReadinessState:
        """Get current readiness state."""
        return self._state
    
    def get_health_response(self) -> Dict[str, Any]:
        """
        Generate a health response for the /health/ready endpoint.
        
        This is the key integration point - it provides progressive readiness
        that the supervisor can use to determine when JARVIS is "ready enough".
        """
        state = self._state
        
        # Calculate elapsed time
        elapsed = 0.0
        if self._start_time:
            elapsed = time.time() - self._start_time
        
        # Determine status string for compatibility
        if state.level >= ReadinessLevel.FULL:
            status = "ready"
            ready = True
        elif state.level >= ReadinessLevel.WARMING:
            status = "warming_up"
            ready = True  # KEY: warming_up is now ready=True for interaction
        elif state.level >= ReadinessLevel.INTERACTIVE:
            status = "degraded"
            ready = True
        elif state.level >= ReadinessLevel.WEBSOCKET:
            status = "websocket_ready"
            ready = True  # KEY: WebSocket ready = can interact
        elif state.level >= ReadinessLevel.HTTP:
            status = "initializing"
            ready = False
        else:
            status = "starting"
            ready = False
        
        # Build component details
        component_details = {}
        for name, comp in self._components.items():
            component_details[name] = {
                "ready": comp.is_ready,
                "loading": comp.is_loading,
                "error": comp.error,
                "duration_ms": comp.duration_ms,
            }
        
        return {
            "status": status,
            "ready": ready,
            "operational": state.level >= ReadinessLevel.WEBSOCKET,
            "readiness_level": state.level.name,
            "readiness_value": int(state.level),
            "progress_percent": state.progress_percent,
            "message": state.message,
            "details": {
                **state.details,
                "websocket_ready": self._components.get("websocket_manager", ComponentDef(name="websocket_manager")).is_ready,
                "event_loop": True,
            },
            "components": component_details,
            "elapsed_seconds": round(elapsed, 1),
            "ml_warmup_info": {
                "is_warming_up": state.level == ReadinessLevel.WARMING,
                "is_ready": state.level >= ReadinessLevel.FULL,
            },
        }
    
    async def run_startup_sequence(self):
        """
        Run the full startup sequence with parallel initialization.
        
        This should be called from the FastAPI lifespan.
        """
        self._started_at = datetime.now()
        self._start_time = time.time()
        
        logger.info("=" * 60)
        logger.info("🚀 JARVIS Intelligent Startup v1.0 - Progressive Readiness")
        logger.info("=" * 60)
        
        # Immediately mark HTTP as ready (we're running)
        self._current_level = ReadinessLevel.HTTP
        self._level_events[ReadinessLevel.HTTP].set()
        await self.update_readiness_level()
        
        # Start background initialization
        task = asyncio.create_task(self._parallel_initialization())
        self._background_tasks.append(task)
        
        logger.info("✅ HTTP layer ready - startup continuing in background")
    
    async def _parallel_initialization(self):
        """Run component initialization in parallel with dependency resolution."""
        # Group components by level
        levels: Dict[ReadinessLevel, List[ComponentDef]] = {}
        for comp in self._components.values():
            level = comp.required_for_level
            if level not in levels:
                levels[level] = []
            levels[level].append(comp)
        
        # Initialize each level in order
        for level in sorted(levels.keys()):
            components = levels[level]
            
            logger.info(f"📦 Initializing {level.name} components...")
            
            # Run components at this level in parallel
            tasks = []
            for comp in components:
                # Check dependencies
                deps_ready = all(
                    self._components.get(dep, ComponentDef(name=dep)).is_ready
                    for dep in comp.dependencies
                )
                
                if deps_ready:
                    tasks.append(self._initialize_component(comp))
            
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            
            # Update readiness level
            await self.update_readiness_level()
        
        logger.info("✅ All startup tasks completed")
    
    async def _initialize_component(self, comp: ComponentDef):
        """Initialize a single component with timeout protection."""
        self.mark_component_loading(comp.name)
        
        try:
            if comp.initializer:
                await asyncio.wait_for(comp.initializer(), timeout=comp.timeout)
            
            # If no initializer, check if ready
            if comp.checker:
                is_ready = await asyncio.wait_for(comp.checker(), timeout=comp.timeout)
                self.mark_component_ready(comp.name, ready=is_ready)
            else:
                # No initializer or checker - assume ready
                self.mark_component_ready(comp.name, ready=True)
                
        except asyncio.TimeoutError:
            self.mark_component_ready(comp.name, ready=False, error=f"Timeout after {comp.timeout}s")
        except Exception as e:
            self.mark_component_ready(comp.name, ready=False, error=str(e))
    
    async def shutdown(self):
        """Cleanup on shutdown."""
        for task in self._background_tasks:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        
        logger.info("🛑 Startup orchestrator shutdown complete")


# =============================================================================
# SINGLETON ACCESS
# =============================================================================

_orchestrator_instance: Optional[IntelligentStartupOrchestrator] = None
_orchestrator_lock = threading.Lock()


def get_startup_orchestrator() -> IntelligentStartupOrchestrator:
    """Get the singleton startup orchestrator."""
    global _orchestrator_instance
    if _orchestrator_instance is None:
        with _orchestrator_lock:
            if _orchestrator_instance is None:
                _orchestrator_instance = IntelligentStartupOrchestrator()
    return _orchestrator_instance


# =============================================================================
# FASTAPI INTEGRATION HELPERS
# =============================================================================

async def register_with_app(app):
    """
    Register the startup orchestrator with a FastAPI app.
    
    This sets up automatic component tracking based on app.state.
    """
    orchestrator = get_startup_orchestrator()
    
    # Store reference in app state
    app.state.startup_orchestrator = orchestrator
    
    # Setup automatic component detection
    async def check_websocket():
        return hasattr(app.state, "unified_websocket_manager")
    
    async def check_database():
        try:
            from intelligence.learning_database import get_learning_db
            db = get_learning_db()
            return db and db._initialized
        except Exception:
            return False
    
    async def check_ml_models():
        try:
            from voice_unlock.ml_engine_registry import get_ml_warmup_status
            status = get_ml_warmup_status()
            return status.get("is_ready", False)
        except Exception:
            return False
    
    # Update component checkers
    if "websocket_manager" in orchestrator._components:
        orchestrator._components["websocket_manager"].checker = check_websocket
    
    if "database" in orchestrator._components:
        orchestrator._components["database"].checker = check_database
    
    if "ml_models" in orchestrator._components:
        orchestrator._components["ml_models"].checker = check_ml_models
    
    logger.info("🔗 Startup orchestrator registered with FastAPI app")


async def health_ready_progressive():
    """
    Progressive health check for the /health/ready endpoint.
    
    Use this instead of the old health_ready function.
    """
    orchestrator = get_startup_orchestrator()
    return orchestrator.get_health_response()

