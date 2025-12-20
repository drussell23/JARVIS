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
]
