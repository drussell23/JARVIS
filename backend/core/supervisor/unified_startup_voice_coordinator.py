#!/usr/bin/env python3
"""
JARVIS Unified Startup Voice Coordinator v1.0.0
================================================

This module coordinates the two startup voice systems:
1. IntelligentStartupNarrator (startup_narrator.py) - Phase tracking & milestones
2. IntelligentStartupAnnouncer (intelligent_startup_announcer.py) - Dynamic messages

Instead of having these systems work independently (causing overlap and duplication),
this coordinator ensures they work TOGETHER, each handling their specialized role:

┌─────────────────────────────────────────────────────────────────────────────┐
│               UnifiedStartupVoiceCoordinator v1.0.0                         │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  RESPONSIBILITIES:                                                          │
│                                                                             │
│  ┌─────────────────────────────────┐  ┌─────────────────────────────────┐  │
│  │  IntelligentStartupNarrator     │  │  IntelligentStartupAnnouncer    │  │
│  │  (startup_narrator.py)          │  │  (intelligent_startup_announcer)│  │
│  │                                 │  │                                 │  │
│  │  • Phase transitions            │  │  • Context-aware messages       │  │
│  │  • Progress milestones (25/50%) │  │  • User personalization         │  │
│  │  • Warning announcements        │  │  • Time-of-day greetings        │  │
│  │  • Error/recovery states        │  │  • System status synthesis      │  │
│  │  • Quick status updates         │  │  • Calendar integration         │  │
│  │                                 │  │  • Tone adaptation              │  │
│  └─────────────────────────────────┘  └─────────────────────────────────┘  │
│                    │                              │                         │
│                    └──────────────┬───────────────┘                         │
│                                   │                                         │
│                                   ▼                                         │
│                    ┌─────────────────────────────┐                          │
│                    │   SharedStartupContext      │                          │
│                    │   (Real-time shared state)  │                          │
│                    └─────────────────────────────┘                          │
│                                   │                                         │
│                                   ▼                                         │
│                    ┌─────────────────────────────┐                          │
│                    │  UnifiedVoiceOrchestrator   │                          │
│                    │  (Single voice output)      │                          │
│                    └─────────────────────────────┘                          │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘

COMMUNICATION FLOW:
1. Supervisor calls coordinator methods
2. Coordinator decides which system handles the request
3. Systems share context through SharedStartupContext
4. All voice output goes through UnifiedVoiceOrchestrator

WHEN TO USE WHICH SYSTEM:
- Narrator: Phase changes, milestones, warnings, errors (brief, informational)
- Announcer: Completion, greetings, personalized messages (rich, context-aware)

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
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
import weakref

from backend.core.async_safety import LazyAsyncLock

logger = logging.getLogger(__name__)


# =============================================================================
# Enums and Types
# =============================================================================

class AnnouncementType(Enum):
    """Types of announcements that determine which system handles them."""
    # Narrator handles these (brief, informational)
    PHASE_CHANGE = auto()          # "Loading models..."
    PROGRESS_MILESTONE = auto()    # "50 percent complete"
    SLOW_STARTUP = auto()          # "Taking longer than usual"
    WARNING = auto()               # "Some services unavailable"
    ERROR = auto()                 # "Startup failed"
    RECOVERY = auto()              # "Attempting recovery"
    
    # Announcer handles these (rich, context-aware)
    GREETING = auto()              # "Good morning, Derek"
    FULL_COMPLETION = auto()       # Full personalized completion
    PARTIAL_COMPLETION = auto()    # Personalized partial completion
    STATUS_REPORT = auto()         # Detailed system status
    
    # v5.0: Hot Reload (Dev Mode) - Announcer handles with personality
    HOT_RELOAD_DETECTED = auto()   # Code changes detected
    HOT_RELOAD_RESTARTING = auto() # About to restart
    HOT_RELOAD_REBUILDING = auto() # Frontend rebuilding
    HOT_RELOAD_COMPLETE = auto()   # Restart/rebuild done
    
    # Either can handle (coordinator decides based on context)
    QUICK_STATUS = auto()          # Brief status update


class CoordinatorState(Enum):
    """States of the startup coordinator."""
    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    COMPLETING = "completing"
    COMPLETE = "complete"
    PARTIAL = "partial"
    ERROR = "error"


# =============================================================================
# Shared Context
# =============================================================================

@dataclass
class SharedStartupContext:
    """
    Real-time shared state between startup voice systems.
    
    This ensures both systems have access to the same information
    and can make consistent decisions.
    """
    # State
    coordinator_state: CoordinatorState = CoordinatorState.IDLE
    start_time: Optional[datetime] = None
    
    # Progress
    current_progress: float = 0.0
    current_phase: Optional[str] = None
    phases_completed: Set[str] = field(default_factory=set)
    
    # System info
    total_components: int = 0
    active_components: int = 0
    services_ready: List[str] = field(default_factory=list)
    services_failed: List[str] = field(default_factory=list)
    
    # User context (populated by announcer)
    user_name: str = "Sir"
    user_first_name: str = "Sir"
    
    # Announcement tracking (prevents duplicates)
    announcements_made: Set[str] = field(default_factory=set)
    last_announcement_time: Optional[datetime] = None
    last_announcement_type: Optional[AnnouncementType] = None
    
    # Configuration
    voice_enabled: bool = True
    greeting_made: bool = False
    completion_announced: bool = False
    
    @property
    def elapsed_seconds(self) -> float:
        """Get elapsed time since startup started."""
        if not self.start_time:
            return 0.0
        return (datetime.now() - self.start_time).total_seconds()
    
    @property
    def health_ratio(self) -> float:
        """Calculate health ratio 0-1."""
        if self.total_components == 0:
            return 1.0
        return self.active_components / self.total_components
    
    def mark_announced(self, announcement_id: str, ann_type: AnnouncementType) -> bool:
        """
        Mark an announcement as made. Returns False if already announced.
        This prevents duplicate announcements.
        """
        if announcement_id in self.announcements_made:
            return False
        self.announcements_made.add(announcement_id)
        self.last_announcement_time = datetime.now()
        self.last_announcement_type = ann_type
        return True
    
    def should_announce(self, announcement_id: str, min_interval_seconds: float = 5.0) -> bool:
        """Check if we should make this announcement (cooldown check)."""
        if announcement_id in self.announcements_made:
            return False
        if self.last_announcement_time:
            elapsed = (datetime.now() - self.last_announcement_time).total_seconds()
            if elapsed < min_interval_seconds:
                return False
        return True


# =============================================================================
# Event System
# =============================================================================

@dataclass
class StartupEvent:
    """Event for communication between systems."""
    event_type: str
    data: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)
    source: str = "coordinator"


class EventBus:
    """Simple event bus for communication between systems."""
    
    def __init__(self):
        self._listeners: Dict[str, List[Callable]] = {}
        self._event_history: List[StartupEvent] = []
    
    def subscribe(self, event_type: str, callback: Callable) -> None:
        """Subscribe to an event type."""
        if event_type not in self._listeners:
            self._listeners[event_type] = []
        self._listeners[event_type].append(callback)
    
    def unsubscribe(self, event_type: str, callback: Callable) -> None:
        """Unsubscribe from an event type."""
        if event_type in self._listeners:
            self._listeners[event_type] = [
                cb for cb in self._listeners[event_type] if cb != callback
            ]
    
    async def emit(self, event: StartupEvent) -> None:
        """Emit an event to all listeners."""
        self._event_history.append(event)
        if len(self._event_history) > 100:
            self._event_history = self._event_history[-100:]
        
        listeners = self._listeners.get(event.event_type, [])
        for listener in listeners:
            try:
                if asyncio.iscoroutinefunction(listener):
                    await listener(event)
                else:
                    listener(event)
            except Exception as e:
                logger.error(f"Event listener error for {event.event_type}: {e}")
    
    def get_history(self, event_type: Optional[str] = None) -> List[StartupEvent]:
        """Get event history, optionally filtered by type."""
        if event_type:
            return [e for e in self._event_history if e.event_type == event_type]
        return self._event_history.copy()


# =============================================================================
# Main Coordinator
# =============================================================================

class UnifiedStartupVoiceCoordinator:
    """
    Coordinates the two startup voice systems to work together.
    
    This is the SINGLE entry point for all startup voice announcements.
    It decides which system handles each announcement type and ensures
    they share context and don't duplicate efforts.
    """
    
    def __init__(self):
        # Shared context
        self._context = SharedStartupContext()
        self._event_bus = EventBus()
        
        # Component references (lazy initialized)
        self._narrator = None
        self._announcer = None
        self._orchestrator = None
        
        # State
        self._initialized = False
        self._lock = asyncio.Lock()
        
        logger.info("UnifiedStartupVoiceCoordinator created")
    
    async def initialize(self) -> bool:
        """Initialize the coordinator and its subsystems."""
        if self._initialized:
            return True
        
        async with self._lock:
            if self._initialized:
                return True
            
            try:
                # Import and initialize narrator
                from .startup_narrator import get_startup_narrator
                self._narrator = get_startup_narrator()
                
                # Import and initialize announcer
                try:
                    from agi_os.intelligent_startup_announcer import get_intelligent_announcer
                    self._announcer = await get_intelligent_announcer()
                    
                    # v5.0: Link announcer to shared context for bidirectional communication
                    self._announcer.set_coordinator_context(self._context)
                    
                    logger.info("✅ IntelligentStartupAnnouncer connected to coordinator")
                except ImportError as e:
                    logger.warning(f"IntelligentStartupAnnouncer not available: {e}")
                    self._announcer = None
                
                # Get unified voice orchestrator
                from .unified_voice_orchestrator import get_voice_orchestrator
                self._orchestrator = get_voice_orchestrator()
                
                # Subscribe narrator to relevant events
                self._event_bus.subscribe("phase_change", self._handle_phase_change)
                self._event_bus.subscribe("progress_update", self._handle_progress_update)
                self._event_bus.subscribe("completion", self._handle_completion)
                
                self._initialized = True
                logger.info("✅ UnifiedStartupVoiceCoordinator initialized")
                return True
                
            except Exception as e:
                logger.error(f"Failed to initialize coordinator: {e}")
                return False
    
    @property
    def context(self) -> SharedStartupContext:
        """Get the shared context."""
        return self._context
    
    @property
    def event_bus(self) -> EventBus:
        """Get the event bus."""
        return self._event_bus
    
    # =========================================================================
    # Public API - Entry points for all startup announcements
    # =========================================================================
    
    async def start_startup(self) -> None:
        """Signal that startup is beginning."""
        await self.initialize()
        
        self._context.coordinator_state = CoordinatorState.STARTING
        self._context.start_time = datetime.now()
        
        # Start narrator
        if self._narrator:
            await self._narrator.start()
        
        await self._event_bus.emit(StartupEvent("startup_begin", {}))
        logger.info("🚀 Startup voice coordination active")
    
    async def announce_greeting(self) -> None:
        """
        Announce initial greeting.
        Uses ANNOUNCER for personalized, context-aware greeting.
        """
        if not self._context.should_announce("greeting"):
            return
        
        if self._announcer:
            try:
                # Use announcer for rich greeting
                ctx = await self._announcer.gather_full_context()
                self._context.user_name = ctx.user.name
                self._context.user_first_name = ctx.user.first_name
                
                # Generate greeting (just the greeting part)
                message = await self._announcer.generate_startup_message()
                
                self._context.mark_announced("greeting", AnnouncementType.GREETING)
                self._context.greeting_made = True
                
                logger.info(f"[Coordinator] Greeting via announcer: {message}")
                
            except Exception as e:
                logger.error(f"Announcer greeting failed: {e}")
                # Fall through to narrator fallback
        
        # Fallback to narrator if announcer unavailable or failed
        if not self._context.greeting_made and self._narrator:
            await self._narrator.announce_phase(
                self._narrator._current_phase or "starting",
                "JARVIS starting up",
                0,
                context="start"
            )
            self._context.mark_announced("greeting", AnnouncementType.GREETING)
            self._context.greeting_made = True
    
    async def announce_phase(
        self,
        phase: str,
        message: str,
        progress: float,
        context: str = "start",
    ) -> None:
        """
        Announce a phase transition.
        Uses NARRATOR for brief, informational updates.
        """
        # Update shared context
        self._context.current_phase = phase
        self._context.current_progress = progress
        
        if context == "complete":
            self._context.phases_completed.add(phase)
        
        # Check if we should announce (prevents spam)
        announcement_id = f"phase_{phase}_{context}"
        if not self._context.should_announce(announcement_id, min_interval_seconds=3.0):
            logger.debug(f"[Coordinator] Skipping duplicate/rapid phase: {phase}")
            return
        
        # Use narrator for phase announcements
        if self._narrator:
            from .startup_narrator import StartupPhase
            try:
                phase_enum = StartupPhase(phase)
            except ValueError:
                phase_enum = StartupPhase.BACKEND_INIT
            
            await self._narrator.announce_phase(phase_enum, message, progress, context)
            self._context.mark_announced(announcement_id, AnnouncementType.PHASE_CHANGE)
        
        # Emit event for other systems
        await self._event_bus.emit(StartupEvent("phase_change", {
            "phase": phase,
            "message": message,
            "progress": progress,
            "context": context,
        }))
    
    async def announce_progress(self, progress: float, message: Optional[str] = None) -> None:
        """
        Announce progress milestone.
        Uses NARRATOR for brief milestone updates.
        """
        self._context.current_progress = progress
        
        # Only narrator handles milestones
        if self._narrator:
            await self._narrator.announce_progress(progress, message)
        
        await self._event_bus.emit(StartupEvent("progress_update", {
            "progress": progress,
            "message": message,
        }))
    
    async def announce_slow_startup(self) -> None:
        """
        Announce that startup is taking longer than expected.
        Uses NARRATOR for brief warning.
        """
        if not self._context.should_announce("slow_startup"):
            return
        
        if self._narrator:
            await self._narrator.announce_slow_startup()
            self._context.mark_announced("slow_startup", AnnouncementType.SLOW_STARTUP)
    
    async def announce_warning(self, message: str, context: str = "slow") -> None:
        """
        Announce a warning.
        Uses NARRATOR for brief warning.
        """
        announcement_id = f"warning_{context}"
        if not self._context.should_announce(announcement_id):
            return
        
        if self._narrator:
            await self._narrator.announce_warning(message, context)
            self._context.mark_announced(announcement_id, AnnouncementType.WARNING)
    
    async def announce_error(self, error_message: str) -> None:
        """
        Announce an error.
        Uses NARRATOR for error announcement.
        """
        self._context.coordinator_state = CoordinatorState.ERROR
        
        if self._narrator:
            await self._narrator.announce_error(error_message)
        
        await self._event_bus.emit(StartupEvent("error", {"message": error_message}))
    
    async def announce_complete(
        self,
        services_ready: Optional[List[str]] = None,
        services_failed: Optional[List[str]] = None,
        duration_seconds: Optional[float] = None,
    ) -> None:
        """
        Announce FULL completion.
        Uses ANNOUNCER for rich, personalized completion message.
        """
        if self._context.completion_announced:
            logger.debug("[Coordinator] Completion already announced, skipping")
            return
        
        # Update context
        self._context.services_ready = services_ready or []
        self._context.services_failed = services_failed or []
        self._context.total_components = len(self._context.services_ready) + len(self._context.services_failed)
        self._context.active_components = len(self._context.services_ready)
        
        failed_count = len(services_failed) if services_failed else 0
        
        # Determine if this is full or partial completion
        if failed_count > 0:
            self._context.coordinator_state = CoordinatorState.PARTIAL
            await self._announce_partial_completion(
                services_ready, services_failed, duration_seconds
            )
        else:
            self._context.coordinator_state = CoordinatorState.COMPLETE
            await self._announce_full_completion(duration_seconds)
        
        self._context.completion_announced = True
        
        await self._event_bus.emit(StartupEvent("completion", {
            "state": self._context.coordinator_state.value,
            "services_ready": services_ready,
            "services_failed": services_failed,
            "duration": duration_seconds,
        }))
    
    async def _announce_full_completion(self, duration_seconds: Optional[float] = None) -> None:
        """Internal: Announce full completion using ANNOUNCER."""
        if self._announcer:
            try:
                # Use announcer for rich, personalized completion
                from agi_os.intelligent_startup_announcer import StartupType
                
                startup_type = StartupType.COLD_BOOT
                if duration_seconds and duration_seconds > 120:
                    startup_type = StartupType.SLOW_BOOT
                
                message = await self._announcer.generate_startup_message(
                    startup_type=startup_type
                )
                
                logger.info(f"[Coordinator] Full completion via announcer: {message}")
                return
                
            except Exception as e:
                logger.error(f"Announcer completion failed: {e}")
        
        # Fallback to narrator
        if self._narrator:
            await self._narrator.announce_complete(
                duration_seconds=duration_seconds
            )
    
    async def _announce_partial_completion(
        self,
        services_ready: Optional[List[str]],
        services_failed: Optional[List[str]],
        duration_seconds: Optional[float],
    ) -> None:
        """Internal: Announce partial completion using ANNOUNCER."""
        progress = int(self._context.health_ratio * 100)
        
        if self._announcer:
            try:
                # Use announcer for rich, personalized partial completion
                message = await self._announcer.generate_partial_completion_message(
                    services_ready=services_ready,
                    services_failed=services_failed,
                    progress=progress,
                    duration_seconds=duration_seconds,
                )
                
                logger.info(f"[Coordinator] Partial completion via announcer: {message}")
                return
                
            except Exception as e:
                logger.error(f"Announcer partial completion failed: {e}")
        
        # Fallback to narrator
        if self._narrator:
            await self._narrator.announce_partial_complete(
                services_ready=services_ready,
                services_failed=services_failed,
                progress=progress,
                duration_seconds=duration_seconds,
            )
    
    async def announce_recovery(self, success: bool = True) -> None:
        """
        Announce recovery attempt result.
        Uses NARRATOR for recovery announcement.
        """
        if self._narrator:
            await self._narrator.announce_recovery(success)
    
    # =========================================================================
    # v5.0: Hot Reload Announcements (Dev Mode)
    # =========================================================================
    
    async def announce_hot_reload_detected(
        self,
        file_count: int,
        file_types: List[str],
        target: str = "backend",
    ) -> None:
        """
        Announce that code changes were detected.
        Uses ANNOUNCER for personality-driven message.
        
        Args:
            file_count: Number of files changed
            file_types: Types of files (e.g., ["Python", "Rust"])
            target: What's being reloaded ("backend", "frontend", "native", "all")
        """
        announcement_id = f"hot_reload_detected_{time.time()}"
        
        # Don't spam - but hot reload messages are usually important
        if not self._context.should_announce(announcement_id, min_interval_seconds=5.0):
            logger.debug("[Coordinator] Skipping rapid hot reload announcement")
            return
        
        if self._announcer:
            try:
                from agi_os.intelligent_startup_announcer import HotReloadContext
                
                message, _ = await self._announcer.announce_hot_reload(
                    context=HotReloadContext.DETECTED,
                    changed_files=[""] * file_count,
                    file_types=file_types,
                    target=target,
                )
                
                self._context.mark_announced(announcement_id, AnnouncementType.HOT_RELOAD_DETECTED)
                logger.info(f"[Coordinator] Hot reload detected: {message}")
                return
                
            except Exception as e:
                logger.error(f"Hot reload announcement failed: {e}")
        
        # Fallback: Simple narrator message
        if self._narrator:
            await self._narrator._speak(
                f"Code changes detected. {file_count} {', '.join(file_types)} files modified."
            )
    
    async def announce_hot_reload_restarting(
        self,
        target: str = "backend",
    ) -> None:
        """
        Announce that JARVIS is restarting due to code changes.
        Uses ANNOUNCER for personality-driven message.
        
        Args:
            target: What's being restarted
        """
        announcement_id = "hot_reload_restarting"
        
        if self._announcer:
            try:
                from agi_os.intelligent_startup_announcer import HotReloadContext
                
                message, _ = await self._announcer.announce_hot_reload(
                    context=HotReloadContext.RESTARTING,
                    target=target,
                )
                
                self._context.mark_announced(announcement_id, AnnouncementType.HOT_RELOAD_RESTARTING)
                logger.info(f"[Coordinator] Hot reload restarting: {message}")
                return
                
            except Exception as e:
                logger.error(f"Hot reload restart announcement failed: {e}")
        
        # Fallback
        if self._narrator:
            await self._narrator._speak(f"Restarting {target} with your changes.")
    
    async def announce_hot_reload_rebuilding(
        self,
        target: str = "frontend",
    ) -> None:
        """
        Announce that frontend is being rebuilt.
        Uses ANNOUNCER for personality-driven message.
        
        Args:
            target: What's being rebuilt (usually "frontend")
        """
        announcement_id = "hot_reload_rebuilding"
        
        if self._announcer:
            try:
                from agi_os.intelligent_startup_announcer import HotReloadContext
                
                message, _ = await self._announcer.announce_hot_reload(
                    context=HotReloadContext.REBUILDING,
                    target=target,
                )
                
                self._context.mark_announced(announcement_id, AnnouncementType.HOT_RELOAD_REBUILDING)
                logger.info(f"[Coordinator] Hot reload rebuilding: {message}")
                return
                
            except Exception as e:
                logger.error(f"Hot reload rebuild announcement failed: {e}")
        
        # Fallback
        if self._narrator:
            await self._narrator._speak(f"Rebuilding {target}.")
    
    async def announce_hot_reload_complete(
        self,
        target: str = "backend",
        duration_seconds: Optional[float] = None,
    ) -> None:
        """
        Announce that hot reload is complete.
        Uses ANNOUNCER for personality-driven message.
        
        Args:
            target: What was reloaded
            duration_seconds: How long the restart took
        """
        announcement_id = "hot_reload_complete"
        
        if self._announcer:
            try:
                from agi_os.intelligent_startup_announcer import HotReloadContext
                
                message, _ = await self._announcer.announce_hot_reload(
                    context=HotReloadContext.COMPLETE,
                    target=target,
                )
                
                self._context.mark_announced(announcement_id, AnnouncementType.HOT_RELOAD_COMPLETE)
                logger.info(f"[Coordinator] Hot reload complete: {message}")
                return
                
            except Exception as e:
                logger.error(f"Hot reload complete announcement failed: {e}")
        
        # Fallback
        if self._narrator:
            duration_str = f" in {duration_seconds:.1f} seconds" if duration_seconds else ""
            await self._narrator._speak(f"Update complete{duration_str}. Ready.")
    
    async def stop(self) -> None:
        """Stop the coordinator."""
        self._context.coordinator_state = CoordinatorState.IDLE
        
        if self._narrator:
            await self._narrator.stop()
        
        logger.info("UnifiedStartupVoiceCoordinator stopped")
    
    # =========================================================================
    # Event Handlers
    # =========================================================================
    
    async def _handle_phase_change(self, event: StartupEvent) -> None:
        """Handle phase change events."""
        # Update context from event
        self._context.current_phase = event.data.get("phase")
        self._context.current_progress = event.data.get("progress", 0)
    
    async def _handle_progress_update(self, event: StartupEvent) -> None:
        """Handle progress update events."""
        self._context.current_progress = event.data.get("progress", 0)
    
    async def _handle_completion(self, event: StartupEvent) -> None:
        """Handle completion events."""
        self._context.coordinator_state = CoordinatorState(event.data.get("state", "complete"))
    
    # =========================================================================
    # Utility Methods
    # =========================================================================
    
    def get_statistics(self) -> Dict[str, Any]:
        """Get coordinator statistics."""
        return {
            "state": self._context.coordinator_state.value,
            "elapsed_seconds": self._context.elapsed_seconds,
            "progress": self._context.current_progress,
            "phases_completed": list(self._context.phases_completed),
            "announcements_made": len(self._context.announcements_made),
            "greeting_made": self._context.greeting_made,
            "completion_announced": self._context.completion_announced,
            "narrator_available": self._narrator is not None,
            "announcer_available": self._announcer is not None,
            "health_ratio": self._context.health_ratio,
        }


# =============================================================================
# Global Instance and Factory
# =============================================================================

_coordinator_instance: Optional[UnifiedStartupVoiceCoordinator] = None
_coordinator_lock = LazyAsyncLock()  # v100.1: Lazy initialization to avoid "no running event loop" error


async def get_startup_voice_coordinator() -> UnifiedStartupVoiceCoordinator:
    """Get or create the global startup voice coordinator instance."""
    global _coordinator_instance
    
    if _coordinator_instance is None:
        async with _coordinator_lock:
            if _coordinator_instance is None:
                _coordinator_instance = UnifiedStartupVoiceCoordinator()
                await _coordinator_instance.initialize()
    
    return _coordinator_instance


def get_startup_voice_coordinator_sync() -> Optional[UnifiedStartupVoiceCoordinator]:
    """Get the coordinator instance if it exists (sync version)."""
    return _coordinator_instance


# =============================================================================
# Convenience Functions
# =============================================================================

async def start_coordinated_startup() -> None:
    """Start coordinated startup voice."""
    coordinator = await get_startup_voice_coordinator()
    await coordinator.start_startup()


async def announce_coordinated_greeting() -> None:
    """Announce greeting through coordinator."""
    coordinator = await get_startup_voice_coordinator()
    await coordinator.announce_greeting()


async def announce_coordinated_phase(
    phase: str,
    message: str,
    progress: float,
    context: str = "start",
) -> None:
    """Announce phase through coordinator."""
    coordinator = await get_startup_voice_coordinator()
    await coordinator.announce_phase(phase, message, progress, context)


async def announce_coordinated_complete(
    services_ready: Optional[List[str]] = None,
    services_failed: Optional[List[str]] = None,
    duration_seconds: Optional[float] = None,
) -> None:
    """Announce completion through coordinator."""
    coordinator = await get_startup_voice_coordinator()
    await coordinator.announce_complete(services_ready, services_failed, duration_seconds)

