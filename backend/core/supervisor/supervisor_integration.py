#!/usr/bin/env python3
"""
JARVIS Supervisor Integration v2.0
===================================

Integration module for connecting the Self-Updating Lifecycle Manager
with the main JARVIS system (start_system.py).

v2.0 CHANGE: Now uses UnifiedVoiceOrchestrator for all TTS output instead
of spawning direct `say` processes. This prevents "multiple voices" issue.

This module provides:
1. Environment detection for supervised vs standalone mode
2. Update intent registration with the event system
3. Exit code helpers for triggering supervisor actions
4. TTS announcements integration (via unified orchestrator)

Usage in start_system.py:
    from backend.core.supervisor.supervisor_integration import (
        setup_supervisor_integration,
        is_supervised,
        trigger_update,
    )

    # During startup
    await setup_supervisor_integration(event_coordinator)

    # When user says "update yourself"
    await trigger_update()

Author: JARVIS System
Version: 2.0.0
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Any, Callable, Optional

# v2.0: Import unified voice orchestrator
from .unified_voice_orchestrator import (
    get_voice_orchestrator,
    VoicePriority,
    VoiceSource,
)

logger = logging.getLogger(__name__)

# Exit codes for supervisor communication
EXIT_CODE_CLEAN = 0
EXIT_CODE_ERROR = 1
EXIT_CODE_UPDATE = 100
EXIT_CODE_ROLLBACK = 101
EXIT_CODE_RESTART = 102


def is_supervised() -> bool:
    """Check if JARVIS is running under the supervisor."""
    return os.environ.get("JARVIS_SUPERVISED") == "1"


def get_supervisor_pid() -> Optional[int]:
    """Get the supervisor's PID if running supervised."""
    pid_str = os.environ.get("JARVIS_SUPERVISOR_PID")
    if pid_str:
        try:
            return int(pid_str)
        except ValueError:
            pass
    return None


async def speak_tts(text: str, voice: str = "Daniel", wait: bool = True) -> None:
    """
    Speak text through unified voice orchestrator.

    v2.0: Now delegates to UnifiedVoiceOrchestrator instead of spawning
    direct `say` processes. This ensures only one voice speaks at a time.

    Args:
        text: Text to speak
        voice: Voice name (ignored - orchestrator uses configured voice)
        wait: Whether to wait for speech to complete
    """
    try:
        orchestrator = get_voice_orchestrator()
        if not orchestrator._running:
            await orchestrator.start()

        await orchestrator.speak(
            text=text,
            priority=VoicePriority.MEDIUM,
            source=VoiceSource.SYSTEM,
            wait=wait,
        )
    except Exception as e:
        logger.debug(f"TTS failed: {e}")


async def trigger_update(
    speak: bool = True,
    summary: Optional[str] = None,
) -> None:
    """
    Trigger a system update by exiting with code 100.
    
    The supervisor will catch this exit code and perform:
    1. git pull
    2. pip install (if requirements changed)
    3. Restart JARVIS
    
    Args:
        speak: Whether to announce the update
        summary: Optional summary of what's being updated
    """
    logger.info("🔄 Update triggered - signaling supervisor")
    
    if speak:
        if summary:
            await speak_tts(
                f"Understood Sir. {summary}. "
                "Initiating update sequence. I'll be back shortly."
            )
        else:
            await speak_tts(
                "Understood Sir. Initiating update sequence. "
                "I'll be back shortly."
            )
        await asyncio.sleep(0.3)  # Let TTS finish
    
    # Exit with update code
    os._exit(EXIT_CODE_UPDATE)


async def trigger_rollback(speak: bool = True) -> None:
    """
    Trigger a rollback to the previous version.
    
    The supervisor will catch exit code 101 and perform:
    1. git reset --hard HEAD@{1}
    2. pip install from snapshot
    3. Restart JARVIS
    """
    logger.info("🔄 Rollback triggered - signaling supervisor")
    
    if speak:
        await speak_tts(
            "Understood. Rolling back to the previous stable version. "
            "Standby for system restart."
        )
        await asyncio.sleep(0.3)
    
    os._exit(EXIT_CODE_ROLLBACK)


async def trigger_restart(speak: bool = True) -> None:
    """
    Trigger a restart without update.
    
    The supervisor will catch exit code 102 and simply
    restart JARVIS without any git operations.
    """
    logger.info("🔄 Restart triggered - signaling supervisor")
    
    if speak:
        await speak_tts("Restarting core systems. Back in a moment.")
        await asyncio.sleep(0.3)
    
    os._exit(EXIT_CODE_RESTART)


async def check_for_updates() -> dict[str, Any]:
    """
    Check if updates are available.
    
    Returns:
        Dict with 'available', 'summary', 'commits_behind'
    """
    try:
        from .update_detector import UpdateDetector
        
        detector = UpdateDetector()
        update_info = await detector.check_for_updates()
        
        if update_info and update_info.available:
            return {
                "available": True,
                "summary": update_info.summary,
                "commits_behind": update_info.commits_behind,
                "current": update_info.current_version,
                "latest": update_info.remote_version,
            }
        
        return {"available": False}
        
    except Exception as e:
        logger.error(f"Update check failed: {e}")
        return {"available": False, "error": str(e)}


async def announce_update_status() -> None:
    """Check for updates and announce if available."""
    status = await check_for_updates()
    
    if status.get("available"):
        summary = status.get("summary", "new changes")
        await speak_tts(
            f"An update is available with {summary}. "
            "Would you like me to update now?"
        )
    else:
        await speak_tts("You're running the latest version.")


class SupervisorIntentHandler:
    """
    Handles update/rollback intents from voice commands.
    
    Register with the JARVIS event system to handle:
    - "update yourself" / "check for updates"
    - "rollback" / "revert update"
    """
    
    def __init__(self):
        self._registered = False
        logger.info("🔧 Supervisor intent handler initialized")
    
    async def handle_update_intent(self, context: Optional[dict] = None) -> str:
        """
        Handle the "update yourself" voice command.
        
        Returns response text (for non-supervised mode) or triggers update.
        """
        if is_supervised():
            # Running under supervisor - trigger update
            await trigger_update()
            return ""  # Won't reach here
        else:
            # Not supervised - inform user
            status = await check_for_updates()
            if status.get("available"):
                return (
                    f"An update is available with {status.get('summary', 'new changes')}. "
                    "To apply it, please restart JARVIS using: python3 run_supervisor.py"
                )
            else:
                return "You're running the latest version. No updates available."
    
    async def handle_rollback_intent(self, context: Optional[dict] = None) -> str:
        """Handle the "rollback" voice command."""
        if is_supervised():
            await trigger_rollback()
            return ""
        else:
            return (
                "Rollback requires running under the supervisor. "
                "Please restart JARVIS using: python3 run_supervisor.py"
            )
    
    async def handle_check_updates_intent(self, context: Optional[dict] = None) -> str:
        """Handle "check for updates" without applying."""
        status = await check_for_updates()
        
        if status.get("available"):
            summary = status.get("summary", "new changes")
            behind = status.get("commits_behind", 0)
            return f"Update available: {summary}. You're {behind} commits behind."
        else:
            return "You're running the latest version."
    
    def register_with_coordinator(
        self,
        event_coordinator: Any,
    ) -> bool:
        """
        Register handlers with JARVISEventCoordinator.
        
        Args:
            event_coordinator: The JARVIS event coordinator instance
            
        Returns:
            True if registration successful
        """
        if self._registered:
            return True
        
        try:
            # Get event bus from coordinator
            event_bus = getattr(event_coordinator, 'event_bus', None)
            if not event_bus:
                logger.warning("Event coordinator has no event_bus")
                return False
            
            # Subscribe to intents
            event_bus.subscribe(
                "intent.update_system",
                lambda ctx: asyncio.create_task(self.handle_update_intent(ctx))
            )
            event_bus.subscribe(
                "intent.rollback_system", 
                lambda ctx: asyncio.create_task(self.handle_rollback_intent(ctx))
            )
            
            self._registered = True
            logger.info("✅ Supervisor intent handlers registered")
            return True
            
        except Exception as e:
            logger.error(f"Failed to register handlers: {e}")
            return False


# Singleton handler
_handler: Optional[SupervisorIntentHandler] = None


def get_intent_handler() -> SupervisorIntentHandler:
    """Get singleton intent handler."""
    global _handler
    if _handler is None:
        _handler = SupervisorIntentHandler()
    return _handler


async def setup_supervisor_integration(
    event_coordinator: Optional[Any] = None,
) -> bool:
    """
    Setup supervisor integration with JARVIS.
    
    Call this during JARVIS startup to:
    1. Register update/rollback intent handlers
    2. Announce supervised mode if applicable
    3. Check for updates on startup
    
    Args:
        event_coordinator: JARVISEventCoordinator instance (optional)
        
    Returns:
        True if running supervised
    """
    supervised = is_supervised()
    
    if supervised:
        logger.info("🔧 Running under supervisor (auto-update enabled)")
        supervisor_pid = get_supervisor_pid()
        if supervisor_pid:
            logger.info(f"   Supervisor PID: {supervisor_pid}")
    else:
        logger.info("🔧 Running standalone (use run_supervisor.py for auto-updates)")
    
    # Register intent handlers if coordinator provided
    if event_coordinator:
        handler = get_intent_handler()
        handler.register_with_coordinator(event_coordinator)
    
    return supervised


# =============================================================================
# SUPERVISOR PROGRESS BRIDGE
# =============================================================================
# This is the coordination layer between start_system.py and the supervisor.
# When running supervised, progress updates go through this bridge which:
# 1. Updates the backend's app.state (exposed via /health/startup)
# 2. The supervisor polls /health/startup and broadcasts to loading page
# This ensures a single source of truth and accurate progress tracking.
# =============================================================================


class SupervisorProgressBridge:
    """
    Coordination bridge for progress reporting when running under supervisor.

    When JARVIS runs under the supervisor (python3 run_supervisor.py):
    - start_system.py uses this bridge to report progress
    - The bridge updates FastAPI's app.state
    - The supervisor polls /health/startup endpoint
    - The supervisor broadcasts to the loading page

    This ensures:
    - Single source of truth (backend's actual state)
    - No duplicate broadcasts
    - Accurate progress tracking
    - Proper coordination between supervisor and start_system.py

    Usage:
        bridge = get_progress_bridge()
        await bridge.report_progress("backend_init", "Starting backend...", 45)
        await bridge.report_component_ready("voice")
        await bridge.mark_complete()
    """

    def __init__(self):
        self._app = None
        self._components_ready: set = set()
        self._components_failed: set = set()
        self._current_phase: str = "INITIALIZING"
        self._current_progress: float = 0.0
        self._current_message: str = "Starting JARVIS..."
        self._is_complete: bool = False
        self._initialized: bool = False

        # Component weights for progress calculation
        self._component_weights = {
            "database": 8,
            "voice": 15,
            "vision": 12,
            "websocket": 5,
            "models": 20,
            "frontend": 15,
            "backend": 20,
            "config": 3,
            "cleanup": 2,
        }
        self._total_weight = sum(self._component_weights.values())

        logger.info("🔧 Supervisor progress bridge initialized")

    def attach_app(self, app) -> None:
        """
        Attach FastAPI app to update its state directly.

        Call this during FastAPI lifespan startup:
            from core.supervisor.supervisor_integration import get_progress_bridge
            bridge = get_progress_bridge()
            bridge.attach_app(app)
        """
        self._app = app
        self._initialized = True
        self._sync_to_app()
        logger.info("📊 Progress bridge attached to FastAPI app")

    def _sync_to_app(self) -> None:
        """Sync current state to FastAPI app.state for /health/startup endpoint."""
        if not self._app:
            return

        self._app.state.startup_phase = self._current_phase
        self._app.state.startup_progress = self._current_progress
        self._app.state.startup_message = self._current_message
        self._app.state.components_ready = self._components_ready.copy()
        self._app.state.components_failed = self._components_failed.copy()
        self._app.state.startup_complete = self._is_complete

    def _calculate_progress(self) -> float:
        """Calculate progress based on ready components."""
        ready_weight = sum(
            self._component_weights.get(c, 0)
            for c in self._components_ready
        )
        return min(100.0, (ready_weight / self._total_weight) * 100.0)

    async def report_progress(
        self,
        phase: str,
        message: str,
        progress: Optional[float] = None,
    ) -> None:
        """
        Report progress update.

        Args:
            phase: Current phase name (e.g., "backend_init", "voice_loading")
            message: Human-readable status message
            progress: Optional explicit progress (0-100). If None, calculated from components.
        """
        self._current_phase = phase
        self._current_message = message

        if progress is not None:
            self._current_progress = min(100.0, max(0.0, progress))
        else:
            self._current_progress = self._calculate_progress()

        self._sync_to_app()

        # Log for visibility (supervisor can see this in stdout)
        logger.debug(f"📊 Progress: {self._current_progress:.0f}% - {phase}: {message}")

    async def report_component_ready(self, component: str, message: Optional[str] = None) -> None:
        """
        Mark a component as ready.

        Args:
            component: Component name (database, voice, vision, etc.)
            message: Optional status message
        """
        if component not in self._components_ready:
            self._components_ready.add(component)
            self._components_failed.discard(component)
            self._current_progress = self._calculate_progress()
            self._current_message = message or f"{component.title()} ready"
            self._sync_to_app()
            logger.info(f"✅ Component ready: {component} ({self._current_progress:.0f}%)")

    async def report_component_failed(self, component: str, error: Optional[str] = None) -> None:
        """Mark a component as failed."""
        if component not in self._components_failed:
            self._components_failed.add(component)
            self._current_message = error or f"{component.title()} failed"
            self._sync_to_app()
            logger.warning(f"❌ Component failed: {component}")

    async def mark_complete(self, success: bool = True, message: Optional[str] = None) -> None:
        """Mark startup as complete."""
        self._is_complete = True
        self._current_phase = "COMPLETE" if success else "FAILED"
        self._current_progress = 100.0 if success else self._current_progress
        self._current_message = message or ("JARVIS is online!" if success else "Startup failed")
        self._sync_to_app()
        logger.info(f"🎉 Startup complete: {self._current_message}")

    def get_status(self) -> dict:
        """Get current status (matches /health/startup format)."""
        return {
            "phase": self._current_phase,
            "progress": self._current_progress,
            "message": self._current_message,
            "components": {
                "ready": list(self._components_ready),
                "failed": list(self._components_failed),
            },
            "ready_for_requests": len(self._components_ready) > 0,
            "full_mode": self._is_complete,
            "is_complete": self._is_complete,
        }

    def is_supervised(self) -> bool:
        """Check if running under supervisor."""
        return os.environ.get("JARVIS_SUPERVISED") == "1"

    def supervisor_controls_loading(self) -> bool:
        """Check if supervisor is handling the loading page."""
        return os.environ.get("JARVIS_SUPERVISOR_LOADING") == "1"


# Singleton bridge
_progress_bridge: Optional[SupervisorProgressBridge] = None


def get_progress_bridge() -> SupervisorProgressBridge:
    """Get singleton progress bridge."""
    global _progress_bridge
    if _progress_bridge is None:
        _progress_bridge = SupervisorProgressBridge()
    return _progress_bridge


async def report_supervised_progress(
    phase: str,
    message: str,
    progress: Optional[float] = None,
) -> None:
    """
    Convenience function to report progress when supervised.

    If not supervised, this is a no-op (caller should use direct broadcast).
    """
    bridge = get_progress_bridge()
    if bridge.is_supervised():
        await bridge.report_progress(phase, message, progress)


async def report_component_ready(component: str, message: Optional[str] = None) -> None:
    """Convenience function to report component ready."""
    bridge = get_progress_bridge()
    await bridge.report_component_ready(component, message)


# Convenience exports
__all__ = [
    "is_supervised",
    "get_supervisor_pid",
    "trigger_update",
    "trigger_rollback",
    "trigger_restart",
    "check_for_updates",
    "announce_update_status",
    "setup_supervisor_integration",
    "get_intent_handler",
    "speak_tts",
    "EXIT_CODE_UPDATE",
    "EXIT_CODE_ROLLBACK",
    "EXIT_CODE_RESTART",
    # New coordination exports
    "SupervisorProgressBridge",
    "get_progress_bridge",
    "report_supervised_progress",
    "report_component_ready",
]
