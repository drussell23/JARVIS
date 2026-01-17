"""
Ouroboros UI Integration v1.0
=============================

Bridges the Native Self-Improvement Engine with JARVIS UI components:
- Menu bar status indicator
- WebSocket broadcasts
- Event bus integration
- Voice announcements

This makes self-improvement feel like a natural part of JARVIS,
with progress visible in the UI without disrupting the user.

Author: Trinity System
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import logging
import json
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from backend.core.ouroboros.native_integration import (
        ImprovementProgress,
        ImprovementPhase,
        ProgressBroadcaster,
    )

logger = logging.getLogger("Ouroboros.UIIntegration")


# =============================================================================
# UI STATE MAPPING
# =============================================================================

class UIActivityState(str, Enum):
    """Activity states for UI display."""
    IDLE = "idle"
    IMPROVING = "improving"
    TESTING = "testing"
    LEARNING = "learning"
    ERROR = "error"


# Phase to UI activity mapping
PHASE_TO_ACTIVITY = {
    "initializing": UIActivityState.IMPROVING,
    "analyzing": UIActivityState.IMPROVING,
    "generating": UIActivityState.IMPROVING,
    "validating": UIActivityState.TESTING,
    "testing": UIActivityState.TESTING,
    "applying": UIActivityState.IMPROVING,
    "committing": UIActivityState.IMPROVING,
    "learning": UIActivityState.LEARNING,
    "completed": UIActivityState.IDLE,
    "failed": UIActivityState.ERROR,
}


# Phase to icon mapping (for menu bar)
PHASE_ICONS = {
    "initializing": "🔧",
    "analyzing": "🔍",
    "generating": "⚡",
    "validating": "✅",
    "testing": "🧪",
    "applying": "📝",
    "committing": "💾",
    "learning": "🧠",
    "completed": "✨",
    "failed": "❌",
}


# =============================================================================
# UI INTEGRATION CONTROLLER
# =============================================================================

class OuroborosUIController:
    """
    Controller that bridges Ouroboros with JARVIS UI components.

    Handles:
    - Menu bar updates
    - WebSocket broadcasts
    - Event bus publishing
    - Voice announcements
    """

    def __init__(self):
        self.logger = logging.getLogger("Ouroboros.UIController")

        # UI component references (set during integration)
        self._menu_bar_indicator = None
        self._websocket_coordinator = None
        self._event_bus = None
        self._voice_integration = None

        # Progress tracking
        self._active_improvements: Dict[str, Dict[str, Any]] = {}
        self._listener_removal: Optional[Callable[[], None]] = None

        # Configuration
        self._announce_completions = True
        self._broadcast_progress = True

    async def connect(
        self,
        menu_bar=None,
        websocket_coordinator=None,
        event_bus=None,
        voice_integration=None,
    ) -> None:
        """
        Connect to UI components.

        Args:
            menu_bar: Menu bar status indicator instance
            websocket_coordinator: WebSocket coordinator for broadcasts
            event_bus: MacOS event bus for system events
            voice_integration: Voice integration for announcements
        """
        self._menu_bar_indicator = menu_bar
        self._websocket_coordinator = websocket_coordinator
        self._event_bus = event_bus
        self._voice_integration = voice_integration

        # Connect to native self-improvement progress broadcaster
        try:
            from backend.core.ouroboros.native_integration import get_native_self_improvement
            engine = get_native_self_improvement()

            # Add progress listener
            self._listener_removal = engine.progress_broadcaster.add_listener(
                self._on_progress_update
            )

            self.logger.info("Connected Ouroboros UI controller to progress broadcaster")

        except ImportError as e:
            self.logger.warning(f"Could not connect to native self-improvement: {e}")

    async def disconnect(self) -> None:
        """Disconnect from UI components."""
        if self._listener_removal:
            self._listener_removal()
            self._listener_removal = None

        self._menu_bar_indicator = None
        self._websocket_coordinator = None
        self._event_bus = None
        self._voice_integration = None

        self.logger.info("Disconnected Ouroboros UI controller")

    async def _on_progress_update(self, progress: "ImprovementProgress") -> None:
        """
        Handle progress updates from the native self-improvement engine.

        This is called automatically when improvement progress changes.
        """
        task_id = progress.task_id
        phase = progress.phase.value

        # Update local tracking
        self._active_improvements[task_id] = progress.to_dict()

        # Update menu bar
        await self._update_menu_bar(progress)

        # Broadcast via WebSocket
        if self._broadcast_progress:
            await self._broadcast_websocket(progress)

        # Publish to event bus
        await self._publish_event(progress)

        # Voice announcement for key phases
        await self._announce_progress(progress)

        # Clean up completed tasks
        if phase in ("completed", "failed"):
            await asyncio.sleep(5.0)  # Keep in list briefly
            self._active_improvements.pop(task_id, None)

    async def _update_menu_bar(self, progress: "ImprovementProgress") -> None:
        """Update the menu bar status indicator."""
        if not self._menu_bar_indicator:
            return

        try:
            phase = progress.phase.value
            icon = PHASE_ICONS.get(phase, "⏳")
            activity = PHASE_TO_ACTIVITY.get(phase, UIActivityState.IDLE)

            # Build status message
            if phase in ("completed", "failed"):
                message = None  # Clear the activity message
            else:
                message = f"{icon} {progress.message}"

            # Update menu bar (thread-safe method)
            if hasattr(self._menu_bar_indicator, 'set_activity_status'):
                # Main thread execution for PyObjC
                await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: self._menu_bar_indicator.set_activity_status(
                        activity=activity.value,
                        message=message,
                    )
                )

            # Also update stats if available
            if hasattr(self._menu_bar_indicator, 'update_stats'):
                stats = {
                    "ouroboros_active": len(self._active_improvements),
                    "ouroboros_phase": phase,
                    "ouroboros_progress": progress.progress_percent,
                }
                await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: self._menu_bar_indicator.update_stats(stats)
                )

        except Exception as e:
            self.logger.warning(f"Menu bar update failed: {e}")

    async def _broadcast_websocket(self, progress: "ImprovementProgress") -> None:
        """Broadcast progress via WebSocket."""
        if not self._websocket_coordinator:
            return

        try:
            message = {
                "type": "ouroboros_progress",
                "topic": "ouroboros_events",
                "timestamp": datetime.utcnow().isoformat(),
                "payload": progress.to_dict(),
            }

            if hasattr(self._websocket_coordinator, 'broadcast'):
                await self._websocket_coordinator.broadcast(
                    topic="ouroboros_events",
                    message=json.dumps(message),
                )
            elif hasattr(self._websocket_coordinator, 'publish'):
                await self._websocket_coordinator.publish(
                    topic="ouroboros_events",
                    message=message,
                )

        except Exception as e:
            self.logger.warning(f"WebSocket broadcast failed: {e}")

    async def _publish_event(self, progress: "ImprovementProgress") -> None:
        """Publish progress to the event bus."""
        if not self._event_bus:
            return

        try:
            event = {
                "type": "OUROBOROS_PROGRESS",
                "source": "ouroboros",
                "timestamp": datetime.utcnow().isoformat(),
                "data": progress.to_dict(),
            }

            if hasattr(self._event_bus, 'emit'):
                await self._event_bus.emit(event)
            elif hasattr(self._event_bus, 'publish'):
                await self._event_bus.publish("ouroboros", event)

        except Exception as e:
            self.logger.warning(f"Event bus publish failed: {e}")

    async def _announce_progress(self, progress: "ImprovementProgress") -> None:
        """Voice announcement for key progress phases."""
        if not self._voice_integration or not self._announce_completions:
            return

        phase = progress.phase.value

        # Only announce key phases
        if phase == "completed":
            message = f"I've finished improving {progress.target_file}. "
            if progress.iteration == 1:
                message += "Got it on the first try!"
            else:
                message += f"It took {progress.iteration} attempts."

        elif phase == "failed":
            message = (
                f"I wasn't able to improve {progress.target_file}. "
                f"The issue was: {progress.error or 'unknown error'}."
            )
        else:
            return  # Don't announce intermediate phases

        try:
            if hasattr(self._voice_integration, 'announce'):
                await self._voice_integration.announce(
                    message,
                    priority="low",
                    interruptible=True,
                )
            elif hasattr(self._voice_integration, 'speak'):
                await self._voice_integration.speak(message)

        except Exception as e:
            self.logger.warning(f"Voice announcement failed: {e}")

    def get_active_improvements(self) -> List[Dict[str, Any]]:
        """Get list of active improvements for UI display."""
        return list(self._active_improvements.values())

    def get_status(self) -> Dict[str, Any]:
        """Get UI integration status."""
        return {
            "connected_components": {
                "menu_bar": self._menu_bar_indicator is not None,
                "websocket": self._websocket_coordinator is not None,
                "event_bus": self._event_bus is not None,
                "voice": self._voice_integration is not None,
            },
            "active_improvements": len(self._active_improvements),
            "announce_completions": self._announce_completions,
            "broadcast_progress": self._broadcast_progress,
        }


# =============================================================================
# MENU BAR EXTENSION
# =============================================================================

class OuroborosMenuSection:
    """
    Menu section for Ouroboros in the JARVIS menu bar.

    Adds:
    - Active improvements list
    - Quick actions (pause, cancel)
    - Status summary
    """

    def __init__(self, ui_controller: OuroborosUIController):
        self.controller = ui_controller
        self.logger = logging.getLogger("Ouroboros.MenuSection")

    def build_menu_items(self) -> List[Dict[str, Any]]:
        """
        Build menu items for the Ouroboros section.

        Returns list of menu item configurations.
        """
        items = []

        # Header
        items.append({
            "type": "header",
            "title": "🐍 Self-Improvement",
        })

        # Active improvements
        active = self.controller.get_active_improvements()
        if active:
            for improvement in active[:5]:  # Limit to 5
                icon = PHASE_ICONS.get(improvement.get("phase", ""), "⏳")
                target = improvement.get("target_file", "unknown").split("/")[-1]
                progress = improvement.get("progress_percent", 0)

                items.append({
                    "type": "item",
                    "title": f"{icon} {target} ({progress:.0f}%)",
                    "enabled": False,  # Informational only
                    "indent": 1,
                })
        else:
            items.append({
                "type": "item",
                "title": "No active improvements",
                "enabled": False,
                "indent": 1,
            })

        # Separator
        items.append({"type": "separator"})

        # Quick actions
        items.append({
            "type": "item",
            "title": "Improve File...",
            "action": "ouroboros_improve_file",
            "shortcut": "Cmd+Shift+I",
        })

        return items

    def handle_action(self, action: str, context: Optional[Dict] = None) -> None:
        """Handle menu action."""
        if action == "ouroboros_improve_file":
            # This would trigger a file picker dialog
            self.logger.info("User requested file improvement via menu")
            # Integration with actual dialog would go here


# =============================================================================
# GLOBAL INSTANCE
# =============================================================================

_ui_controller: Optional[OuroborosUIController] = None


def get_ouroboros_ui_controller() -> OuroborosUIController:
    """Get the global Ouroboros UI controller."""
    global _ui_controller
    if _ui_controller is None:
        _ui_controller = OuroborosUIController()
    return _ui_controller


async def connect_ouroboros_ui(
    menu_bar=None,
    websocket_coordinator=None,
    event_bus=None,
    voice_integration=None,
) -> OuroborosUIController:
    """
    Connect Ouroboros to UI components.

    Call this during JARVIS startup after UI components are initialized.
    """
    controller = get_ouroboros_ui_controller()
    await controller.connect(
        menu_bar=menu_bar,
        websocket_coordinator=websocket_coordinator,
        event_bus=event_bus,
        voice_integration=voice_integration,
    )
    return controller


async def disconnect_ouroboros_ui() -> None:
    """Disconnect Ouroboros from UI components."""
    global _ui_controller
    if _ui_controller:
        await _ui_controller.disconnect()
        _ui_controller = None


# =============================================================================
# ADVANCED REAL-TIME COMMUNICATION v2.0
# =============================================================================
# This section adds Claude Code-like real-time communication:
# 1. Unified broadcaster connecting progress → WebSocket → Voice
# 2. Intelligent voice narration with context awareness
# 3. Real-time diff streaming to UI
# 4. User approval via WebSocket bridge
# 5. Session memory updates to UI
# =============================================================================

import os
import time
import uuid
import hashlib
from dataclasses import dataclass, field
from typing import Set, Awaitable


class VoiceNarrationPriority(str, Enum):
    """Priority levels for voice narration."""
    CRITICAL = "critical"    # Always speak, interrupt if needed
    HIGH = "high"           # Speak unless user is talking
    NORMAL = "normal"       # Speak when quiet
    LOW = "low"             # Only speak if nothing else queued
    BACKGROUND = "background"  # Never interrupt, may be skipped


@dataclass
class NarrationContext:
    """Context for intelligent voice narration."""
    phase: str
    target_file: str
    progress_percent: float
    iteration: int
    error: Optional[str] = None
    blast_radius: int = 0
    provider: Optional[str] = None
    session_changes_count: int = 0
    is_multi_file: bool = False
    requires_approval: bool = False
    risk_score: float = 0.0


class IntelligentVoiceNarrator:
    """
    Context-aware voice narrator for Ouroboros operations.

    Features:
    - Adapts narration based on operation complexity
    - Throttles updates to avoid overwhelming the user
    - Uses different speaking styles for different phases
    - Remembers recent announcements to avoid repetition
    """

    def __init__(self):
        self.logger = logging.getLogger("Ouroboros.VoiceNarrator")
        self._voice_system = None
        self._last_announcement: Dict[str, float] = {}  # phase -> timestamp
        self._min_interval = float(os.getenv("OUROBOROS_VOICE_INTERVAL", "5.0"))
        self._verbose_mode = os.getenv("OUROBOROS_VERBOSE_VOICE", "false").lower() == "true"
        self._recent_announcements: List[str] = []
        self._max_recent = 10

    def set_voice_system(self, voice_system) -> None:
        """Set the voice system to use."""
        self._voice_system = voice_system

    def _should_announce(self, phase: str, priority: VoiceNarrationPriority) -> bool:
        """Check if we should announce based on throttling rules."""
        if priority == VoiceNarrationPriority.CRITICAL:
            return True

        now = time.time()
        last = self._last_announcement.get(phase, 0)

        if now - last < self._min_interval:
            return False

        return True

    def _generate_message(self, context: NarrationContext) -> Optional[tuple]:
        """
        Generate a natural, contextual message for the current state.

        Returns (message, priority) or None if no message needed.
        """
        phase = context.phase
        target = context.target_file.split("/")[-1] if context.target_file else "the file"

        # Phase-specific message generation
        if phase == "initializing":
            if context.is_multi_file:
                return (
                    f"Starting multi-file improvement. {context.session_changes_count} files in this batch.",
                    VoiceNarrationPriority.NORMAL
                )
            return (f"Analyzing {target}...", VoiceNarrationPriority.LOW)

        elif phase == "analyzing":
            if context.blast_radius > 10:
                return (
                    f"Found {context.blast_radius} files that depend on {target}. "
                    "Blast radius is high, proceeding carefully.",
                    VoiceNarrationPriority.HIGH
                )
            elif context.blast_radius > 0:
                return (
                    f"{context.blast_radius} dependencies detected.",
                    VoiceNarrationPriority.LOW
                )
            return None  # Skip if no blast radius

        elif phase == "generating":
            if context.provider:
                provider_name = context.provider.split("/")[-1]
                return (
                    f"Generating improvements with {provider_name}...",
                    VoiceNarrationPriority.LOW
                )
            return None

        elif phase == "validating":
            if context.requires_approval:
                if context.risk_score > 0.7:
                    return (
                        f"Changes ready for review. Risk score is high at {context.risk_score:.0%}. "
                        "Please review the diff carefully.",
                        VoiceNarrationPriority.HIGH
                    )
                return (
                    "Changes ready for review. Check the diff preview.",
                    VoiceNarrationPriority.NORMAL
                )
            return ("Validating changes...", VoiceNarrationPriority.LOW)

        elif phase == "testing":
            return ("Running tests to verify the changes...", VoiceNarrationPriority.LOW)

        elif phase == "applying":
            if context.is_multi_file:
                return (
                    f"Applying changes to all {context.session_changes_count} files atomically...",
                    VoiceNarrationPriority.NORMAL
                )
            return (f"Applying changes to {target}...", VoiceNarrationPriority.LOW)

        elif phase == "committing":
            return ("Committing the changes...", VoiceNarrationPriority.LOW)

        elif phase == "learning":
            return None  # Silent learning phase

        elif phase == "completed":
            if context.is_multi_file:
                return (
                    f"All done! Successfully improved {context.session_changes_count} files.",
                    VoiceNarrationPriority.NORMAL
                )
            if context.iteration == 1:
                return (
                    f"Finished improving {target}. Got it on the first try!",
                    VoiceNarrationPriority.NORMAL
                )
            return (
                f"Finished improving {target} after {context.iteration} iterations.",
                VoiceNarrationPriority.NORMAL
            )

        elif phase == "failed":
            error_brief = (context.error or "unknown error")[:50]
            return (
                f"Couldn't improve {target}. {error_brief}.",
                VoiceNarrationPriority.HIGH
            )

        return None

    async def announce(self, context: NarrationContext) -> None:
        """
        Make a contextual voice announcement.

        Automatically determines what to say and whether to say it
        based on the current context and throttling rules.
        """
        if not self._voice_system:
            return

        result = self._generate_message(context)
        if not result:
            return

        message, priority = result

        if not self._should_announce(context.phase, priority):
            return

        # Skip if we recently said something too similar
        msg_hash = hashlib.md5(message[:30].encode()).hexdigest()[:8]
        if msg_hash in self._recent_announcements:
            return

        # Track announcement
        self._last_announcement[context.phase] = time.time()
        self._recent_announcements.append(msg_hash)
        if len(self._recent_announcements) > self._max_recent:
            self._recent_announcements.pop(0)

        # Actually speak
        try:
            if hasattr(self._voice_system, 'announce'):
                await self._voice_system.announce(
                    message,
                    priority=priority.value,
                    interruptible=(priority != VoiceNarrationPriority.CRITICAL),
                )
            elif hasattr(self._voice_system, 'speak'):
                await self._voice_system.speak(message)
            elif hasattr(self._voice_system, 'say'):
                await self._voice_system.say(message)

            self.logger.debug(f"Announced [{priority.value}]: {message[:50]}...")

        except Exception as e:
            self.logger.warning(f"Voice announcement failed: {e}")


class DiffStreamBroadcaster:
    """
    Streams diff previews to WebSocket clients in real-time.

    Features:
    - Chunk-by-chunk streaming for visual effect
    - Syntax highlighting hints
    - Line number tracking
    - Risk score annotations
    """

    def __init__(self):
        self.logger = logging.getLogger("Ouroboros.DiffStream")
        self._websocket_manager = None
        self._active_streams: Set[str] = set()

    def set_websocket_manager(self, manager) -> None:
        """Set the WebSocket manager to use."""
        self._websocket_manager = manager

    async def stream_diff_preview(
        self,
        preview_id: str,
        file_path: str,
        chunks: List[Any],  # DiffChunk from native_integration
        risk_score: float,
        goal: str,
    ) -> None:
        """
        Stream a diff preview to all connected clients.

        Args:
            preview_id: Unique ID for this preview
            file_path: Path to the file being modified
            chunks: List of DiffChunk objects
            risk_score: Risk score (0-1) for this change
            goal: The improvement goal
        """
        if not self._websocket_manager:
            self.logger.warning("No WebSocket manager set, cannot stream diff")
            return

        self._active_streams.add(preview_id)

        try:
            # Send preview header
            await self._broadcast({
                "type": "diff_preview_start",
                "preview_id": preview_id,
                "file_path": file_path,
                "risk_score": risk_score,
                "goal": goal,
                "total_chunks": len(chunks),
                "timestamp": datetime.utcnow().isoformat(),
            })

            # Stream each chunk with a small delay for visual effect
            for i, chunk in enumerate(chunks):
                if preview_id not in self._active_streams:
                    # Preview was cancelled
                    break

                chunk_data = {
                    "type": "diff_chunk",
                    "preview_id": preview_id,
                    "chunk_index": i,
                    "line_start": getattr(chunk, 'line_start', 0),
                    "line_end": getattr(chunk, 'line_end', 0),
                    "change_type": getattr(chunk, 'change_type', 'modify'),
                    "old_content": getattr(chunk, 'old_content', ''),
                    "new_content": getattr(chunk, 'new_content', ''),
                    "context_before": getattr(chunk, 'context_before', []),
                    "context_after": getattr(chunk, 'context_after', []),
                }

                await self._broadcast(chunk_data)
                await asyncio.sleep(0.03)  # 30ms between chunks

            # Send preview complete
            await self._broadcast({
                "type": "diff_preview_complete",
                "preview_id": preview_id,
                "requires_approval": True,
            })

        except Exception as e:
            self.logger.error(f"Diff streaming failed: {e}")
            await self._broadcast({
                "type": "diff_preview_error",
                "preview_id": preview_id,
                "error": str(e),
            })

        finally:
            self._active_streams.discard(preview_id)

    async def cancel_stream(self, preview_id: str) -> None:
        """Cancel an active diff stream."""
        self._active_streams.discard(preview_id)
        await self._broadcast({
            "type": "diff_preview_cancelled",
            "preview_id": preview_id,
        })

    async def _broadcast(self, message: Dict[str, Any]) -> None:
        """Broadcast a message to all clients."""
        try:
            if hasattr(self._websocket_manager, 'broadcast'):
                await self._websocket_manager.broadcast(message)
            elif hasattr(self._websocket_manager, 'broadcast_json'):
                await self._websocket_manager.broadcast_json(message)
            elif hasattr(self._websocket_manager, 'send_message'):
                import json
                await self._websocket_manager.send_message(json.dumps(message))
        except Exception as e:
            self.logger.warning(f"Broadcast failed: {e}")


class ApprovalWebSocketBridge:
    """
    Bridges the DiffPreviewEngine approval system with WebSocket.

    Allows users to approve/reject/request refinement via the UI.
    """

    def __init__(self):
        self.logger = logging.getLogger("Ouroboros.ApprovalBridge")
        self._diff_preview_engine = None
        self._pending_approvals: Dict[str, Dict[str, Any]] = {}

    def set_diff_preview_engine(self, engine) -> None:
        """Set the diff preview engine."""
        self._diff_preview_engine = engine

    async def handle_approval_message(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """
        Handle an approval message from the UI.

        Expected message format:
        {
            "type": "diff_approval",
            "preview_id": "preview_xxx",
            "action": "approve" | "reject" | "refine",
            "feedback": "optional feedback for refinement"
        }
        """
        if not self._diff_preview_engine:
            return {"success": False, "error": "Approval system not initialized"}

        preview_id = message.get("preview_id")
        action = message.get("action")
        feedback = message.get("feedback")

        if not preview_id or not action:
            return {"success": False, "error": "Missing preview_id or action"}

        try:
            if action == "approve":
                success = await self._diff_preview_engine.submit_approval(
                    preview_id=preview_id,
                    approved=True,
                    feedback=None,
                )
            elif action == "reject":
                success = await self._diff_preview_engine.submit_approval(
                    preview_id=preview_id,
                    approved=False,
                    feedback=None,
                )
            elif action == "refine":
                if not feedback:
                    return {"success": False, "error": "Feedback required for refinement"}
                success = await self._diff_preview_engine.submit_approval(
                    preview_id=preview_id,
                    approved=False,
                    feedback=feedback,
                )
            else:
                return {"success": False, "error": f"Unknown action: {action}"}

            return {
                "success": success,
                "preview_id": preview_id,
                "action": action,
            }

        except Exception as e:
            self.logger.error(f"Approval handling failed: {e}")
            return {"success": False, "error": str(e)}

    def get_pending_approvals(self) -> List[Dict[str, Any]]:
        """Get list of pending approval requests."""
        if not self._diff_preview_engine:
            return []

        previews = self._diff_preview_engine.get_pending_previews()
        return [
            {
                "preview_id": p.id,
                "files": list(p.files.keys()),
                "total_additions": p.total_additions,
                "total_deletions": p.total_deletions,
                "risk_score": p.risk_score,
                "expires_at": p.expires_at,
            }
            for p in previews
        ]


class SessionMemoryBroadcaster:
    """
    Broadcasts session memory updates to the UI.

    Keeps the UI informed about:
    - Files changed in this session
    - Session duration
    - Rollback availability
    """

    def __init__(self):
        self.logger = logging.getLogger("Ouroboros.SessionBroadcaster")
        self._session_memory = None
        self._websocket_manager = None
        self._broadcast_task: Optional[asyncio.Task] = None
        self._running = False

    def set_session_memory(self, session_memory) -> None:
        """Set the session memory manager."""
        self._session_memory = session_memory

    def set_websocket_manager(self, manager) -> None:
        """Set the WebSocket manager."""
        self._websocket_manager = manager

    async def start(self) -> None:
        """Start broadcasting session updates."""
        self._running = True
        self._broadcast_task = asyncio.create_task(self._broadcast_loop())

    async def stop(self) -> None:
        """Stop broadcasting."""
        self._running = False
        if self._broadcast_task:
            self._broadcast_task.cancel()
            try:
                await self._broadcast_task
            except asyncio.CancelledError:
                pass

    async def _broadcast_loop(self) -> None:
        """Periodically broadcast session state."""
        while self._running:
            try:
                await self._broadcast_session_state()
                await asyncio.sleep(5.0)  # Update every 5 seconds
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.warning(f"Session broadcast error: {e}")
                await asyncio.sleep(10.0)

    async def _broadcast_session_state(self) -> None:
        """Broadcast current session state."""
        if not self._session_memory or not self._websocket_manager:
            return

        try:
            summary = await self._session_memory.get_session_summary()

            message = {
                "type": "session_state",
                "session_id": summary.get("session_id"),
                "duration_seconds": summary.get("duration_seconds", 0),
                "total_changes": summary.get("total_changes", 0),
                "files_modified": summary.get("files_modified", []),
                "successful_changes": summary.get("successful_changes", 0),
                "failed_changes": summary.get("failed_changes", 0),
                "can_rollback": len(summary.get("files_modified", [])) > 0,
                "timestamp": datetime.utcnow().isoformat(),
            }

            if hasattr(self._websocket_manager, 'broadcast'):
                await self._websocket_manager.broadcast(message)

        except Exception as e:
            self.logger.warning(f"Session state broadcast failed: {e}")


class UnifiedRealtimeBroadcaster:
    """
    Master coordinator for all real-time communication.

    Connects:
    - Progress events → Voice + WebSocket + Menu Bar
    - Diff previews → WebSocket streaming
    - Approvals → WebSocket bridge
    - Session memory → WebSocket updates

    This is the single integration point for real-time communication.
    """

    def __init__(self):
        self.logger = logging.getLogger("Ouroboros.RealtimeBroadcaster")

        # Sub-components
        self._voice_narrator = IntelligentVoiceNarrator()
        self._diff_broadcaster = DiffStreamBroadcaster()
        self._approval_bridge = ApprovalWebSocketBridge()
        self._session_broadcaster = SessionMemoryBroadcaster()

        # External integrations (set during connect)
        self._websocket_manager = None
        self._voice_system = None
        self._menu_bar = None
        self._event_bus = None

        # State
        self._connected = False
        self._listener_removals: List[Callable[[], None]] = []

    async def connect(
        self,
        websocket_manager=None,
        voice_system=None,
        menu_bar=None,
        event_bus=None,
    ) -> None:
        """
        Connect to all communication channels.

        Call this during JARVIS startup after all components are initialized.
        """
        self.logger.info("Connecting unified real-time broadcaster...")

        self._websocket_manager = websocket_manager
        self._voice_system = voice_system
        self._menu_bar = menu_bar
        self._event_bus = event_bus

        # Configure sub-components
        self._voice_narrator.set_voice_system(voice_system)
        self._diff_broadcaster.set_websocket_manager(websocket_manager)

        # Connect to enhanced self-improvement engine
        try:
            from backend.core.ouroboros.native_integration import (
                get_enhanced_self_improvement,
            )
            engine = get_enhanced_self_improvement()

            # Set up approval bridge
            self._approval_bridge.set_diff_preview_engine(engine.diff_preview_engine)

            # Set up session broadcaster
            self._session_broadcaster.set_session_memory(engine.session_memory)
            self._session_broadcaster.set_websocket_manager(websocket_manager)

            # Listen for diff streams
            removal = engine.diff_preview_engine.add_stream_listener(
                self._on_diff_chunk
            )
            self._listener_removals.append(removal)

            # Listen for progress updates
            removal = engine.progress_broadcaster.add_listener(
                self._on_progress_update
            )
            self._listener_removals.append(removal)

            # Start session broadcaster
            await self._session_broadcaster.start()

            self._connected = True
            self.logger.info("✓ Unified real-time broadcaster connected")
            self.logger.info(f"  - WebSocket: {'yes' if websocket_manager else 'no'}")
            self.logger.info(f"  - Voice: {'yes' if voice_system else 'no'}")
            self.logger.info(f"  - Menu Bar: {'yes' if menu_bar else 'no'}")
            self.logger.info(f"  - Event Bus: {'yes' if event_bus else 'no'}")

        except Exception as e:
            self.logger.error(f"Failed to connect: {e}")
            import traceback
            self.logger.debug(traceback.format_exc())

    async def disconnect(self) -> None:
        """Disconnect from all channels."""
        self.logger.info("Disconnecting unified real-time broadcaster...")

        await self._session_broadcaster.stop()

        for removal in self._listener_removals:
            try:
                removal()
            except Exception:
                pass
        self._listener_removals.clear()

        self._connected = False
        self.logger.info("Disconnected")

    async def _on_progress_update(self, progress) -> None:
        """Handle progress updates from the self-improvement engine."""
        # Build narration context
        context = NarrationContext(
            phase=progress.phase.value if hasattr(progress.phase, 'value') else str(progress.phase),
            target_file=progress.target_file,
            progress_percent=progress.progress_percent,
            iteration=progress.iteration,
            error=getattr(progress, 'error', None),
            blast_radius=getattr(progress, 'blast_radius', 0),
            provider=getattr(progress, 'provider_used', None),
        )

        # Voice narration
        await self._voice_narrator.announce(context)

        # WebSocket broadcast
        if self._websocket_manager:
            try:
                message = {
                    "type": "improvement_progress",
                    "task_id": progress.task_id,
                    "phase": context.phase,
                    "message": progress.message,
                    "progress_percent": progress.progress_percent,
                    "target_file": progress.target_file,
                    "iteration": progress.iteration,
                    "timestamp": datetime.utcnow().isoformat(),
                }

                if hasattr(self._websocket_manager, 'broadcast'):
                    await self._websocket_manager.broadcast(message)

            except Exception as e:
                self.logger.warning(f"WebSocket broadcast failed: {e}")

        # Menu bar update
        if self._menu_bar:
            try:
                icon = PHASE_ICONS.get(context.phase, "⏳")
                activity = PHASE_TO_ACTIVITY.get(context.phase, UIActivityState.IDLE)

                if hasattr(self._menu_bar, 'set_activity_status'):
                    await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda: self._menu_bar.set_activity_status(
                            activity=activity.value,
                            message=f"{icon} {progress.message}",
                        )
                    )
            except Exception as e:
                self.logger.warning(f"Menu bar update failed: {e}")

    async def _on_diff_chunk(self, chunk) -> None:
        """Handle streaming diff chunks."""
        if not self._websocket_manager:
            return

        try:
            message = {
                "type": "diff_chunk_stream",
                "file_path": getattr(chunk, 'file_path', ''),
                "line_start": getattr(chunk, 'line_start', 0),
                "change_type": getattr(chunk, 'change_type', 'modify'),
                "old_content": getattr(chunk, 'old_content', ''),
                "new_content": getattr(chunk, 'new_content', ''),
                "timestamp": datetime.utcnow().isoformat(),
            }

            if hasattr(self._websocket_manager, 'broadcast'):
                await self._websocket_manager.broadcast(message)

        except Exception as e:
            self.logger.warning(f"Diff chunk broadcast failed: {e}")

    async def handle_websocket_message(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """
        Handle incoming WebSocket messages related to Ouroboros.

        Routes to appropriate handler based on message type.
        """
        msg_type = message.get("type", "")

        if msg_type == "diff_approval":
            return await self._approval_bridge.handle_approval_message(message)

        elif msg_type == "get_pending_approvals":
            return {
                "type": "pending_approvals",
                "approvals": self._approval_bridge.get_pending_approvals(),
            }

        elif msg_type == "get_session_state":
            if self._session_broadcaster._session_memory:
                summary = await self._session_broadcaster._session_memory.get_session_summary()
                return {"type": "session_state", **summary}
            return {"type": "session_state", "error": "No session memory"}

        elif msg_type == "rollback_session":
            if self._session_broadcaster._session_memory:
                results = await self._session_broadcaster._session_memory.rollback_session()
                return {"type": "rollback_result", "results": results}
            return {"type": "rollback_result", "error": "No session memory"}

        else:
            return {"error": f"Unknown message type: {msg_type}"}

    def get_status(self) -> Dict[str, Any]:
        """Get broadcaster status."""
        return {
            "connected": self._connected,
            "channels": {
                "websocket": self._websocket_manager is not None,
                "voice": self._voice_system is not None,
                "menu_bar": self._menu_bar is not None,
                "event_bus": self._event_bus is not None,
            },
            "pending_approvals": len(self._approval_bridge.get_pending_approvals()),
        }


# =============================================================================
# GLOBAL REAL-TIME BROADCASTER
# =============================================================================

_realtime_broadcaster: Optional[UnifiedRealtimeBroadcaster] = None


def get_realtime_broadcaster() -> UnifiedRealtimeBroadcaster:
    """Get the global real-time broadcaster."""
    global _realtime_broadcaster
    if _realtime_broadcaster is None:
        _realtime_broadcaster = UnifiedRealtimeBroadcaster()
    return _realtime_broadcaster


async def connect_realtime_broadcaster(
    websocket_manager=None,
    voice_system=None,
    menu_bar=None,
    event_bus=None,
) -> UnifiedRealtimeBroadcaster:
    """
    Connect the real-time broadcaster to all communication channels.

    Call this during JARVIS startup after all components are initialized.

    Example:
        from backend.core.ouroboros.ui_integration import connect_realtime_broadcaster

        broadcaster = await connect_realtime_broadcaster(
            websocket_manager=ws_manager,
            voice_system=voice_system,
            menu_bar=status_indicator,
            event_bus=event_bus,
        )
    """
    broadcaster = get_realtime_broadcaster()
    await broadcaster.connect(
        websocket_manager=websocket_manager,
        voice_system=voice_system,
        menu_bar=menu_bar,
        event_bus=event_bus,
    )
    return broadcaster


async def disconnect_realtime_broadcaster() -> None:
    """Disconnect the real-time broadcaster."""
    global _realtime_broadcaster
    if _realtime_broadcaster:
        await _realtime_broadcaster.disconnect()
        _realtime_broadcaster = None
