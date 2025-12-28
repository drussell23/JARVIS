"""
JARVIS Neural Mesh - Visual Monitor Agent v11.0
===============================================

The "Watcher & Actor" of Video Multi-Space Intelligence (VMSI).

This agent provides ACTIVE visual surveillance capabilities:
- Watch background windows for specific events
- AUTOMATICALLY ACT when events are detected (NEW in v11.0!)
- Monitor multiple windows in parallel
- Alert when visual events are detected
- Execute Computer Use actions in response to visual triggers
- Support conditional branching (if Error -> Retry, if Success -> Deploy)
- Integrate with SpatialAwarenessAgent for window location
- Share state across repos (JARVIS ↔ JARVIS Prime ↔ Reactor Core)

Capabilities:
- watch_and_alert: Monitor a window for text/event and alert
- watch_and_act: Monitor AND execute actions when event detected (NEW!)
- watch_multiple: Monitor multiple windows in parallel
- stop_watching: Cancel active watchers
- list_watchers: Get status of all active watchers

Usage from voice:
    Passive (v10.6):
    "Watch the Terminal for 'Build Successful'"

    ACTIVE (v11.0 NEW!):
    "Watch the Terminal for 'Build Complete', then click Deploy"
    "When you see 'Error' in Terminal, click Retry button"
    "Watch Chrome for 'Application Submitted' and Terminal for 'Error'"

This is JARVIS's "second pair of eyes AND hands" - autonomously monitoring
and responding to background activity while you focus on your main work.

Author: JARVIS AI System
Version: 11.0 - Watch & Act (Autonomous Response)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, asdict, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Union

from ..base.base_neural_mesh_agent import BaseNeuralMeshAgent
from ..data_models import (
    AgentMessage,
    KnowledgeType,
    MessageType,
    MessagePriority,
)

logger = logging.getLogger(__name__)


# =============================================================================
# Action Configuration - "Watch & Act" Capabilities (v11.0)
# =============================================================================

class ActionType(str, Enum):
    """Types of actions that can be executed in response to visual events."""
    SIMPLE_GOAL = "simple_goal"  # Natural language goal for Computer Use
    CONDITIONAL = "conditional"  # Conditional branching (if X -> do Y)
    WORKFLOW = "workflow"  # Complex multi-step workflow via AgenticTaskRunner
    NOTIFICATION = "notification"  # Just notify (passive mode)
    VOICE_ALERT = "voice_alert"  # Voice alert only


@dataclass
class ActionConfig:
    """
    Configuration for actions to execute when visual event detected.

    Supports multiple action types from simple to complex:
    - Simple: Natural language goal executed via Computer Use
    - Conditional: If-then-else branching based on trigger
    - Workflow: Complex multi-step via AgenticTaskRunner
    """
    action_type: ActionType = ActionType.NOTIFICATION

    # Simple goal (for SIMPLE_GOAL)
    goal: Optional[str] = None  # e.g., "Click the Deploy button"

    # Conditional branching (for CONDITIONAL)
    conditions: List['ConditionalAction'] = field(default_factory=list)
    default_action: Optional[str] = None  # Fallback if no conditions match

    # Workflow (for WORKFLOW)
    workflow_goal: Optional[str] = None  # Complex goal for AgenticTaskRunner
    workflow_context: Dict[str, Any] = field(default_factory=dict)

    # Common settings
    switch_to_window: bool = True  # Switch to window before acting
    narrate: bool = True  # Voice narration during execution
    require_confirmation: bool = False  # Ask user before acting
    timeout_seconds: float = 30.0  # Max time for action execution


@dataclass
class ConditionalAction:
    """
    Conditional action: If trigger matches pattern, execute action.

    Example:
        ConditionalAction(
            trigger_pattern="Error",
            action_goal="Click the Retry button",
            description="Retry on error"
        )
    """
    trigger_pattern: str  # Text pattern to match (supports regex)
    action_goal: str  # What to do if pattern matches
    description: str = ""  # Human-readable description
    confidence_threshold: float = 0.75  # Min confidence to trigger
    case_sensitive: bool = False
    use_regex: bool = False


@dataclass
class WatchAndActRequest:
    """
    Complete request for watch-and-act operation.

    Combines visual monitoring with automated response.
    """
    # Monitoring config
    app_name: str
    trigger_text: str
    space_id: Optional[int] = None

    # Action config
    action_config: Optional[ActionConfig] = None

    # Monitoring settings
    fps: int = 5
    timeout: float = 300.0  # 5 minutes default

    # Advanced
    multiple_triggers: List[str] = field(default_factory=list)  # Watch for any of these
    stop_on_first: bool = True  # Stop after first trigger


@dataclass
class ActionExecutionResult:
    """Result of action execution after visual event detected."""
    success: bool
    action_type: ActionType
    goal_executed: Optional[str] = None
    error: Optional[str] = None
    duration_ms: float = 0.0
    computer_use_result: Optional[Dict[str, Any]] = None  # Full Computer Use result
    narration: List[str] = field(default_factory=list)


@dataclass
class VisualMonitorConfig:
    """
    Configuration for Visual Monitor Agent.

    Inherits base agent configuration from BaseAgentConfig via composition.
    """
    # Base agent configuration
    heartbeat_interval_seconds: float = 10.0
    message_queue_size: int = 1000
    message_handler_timeout_seconds: float = 10.0
    enable_knowledge_access: bool = True
    knowledge_cache_size: int = 100
    log_messages: bool = True
    log_level: str = "INFO"

    # Visual monitoring specific
    default_fps: int = 5  # Default FPS for watchers
    default_timeout: float = 300.0  # Default timeout (5 minutes)
    max_parallel_watchers: int = 3  # Max simultaneous watchers
    enable_voice_alerts: bool = True  # Speak when events detected
    enable_notifications: bool = True  # macOS notifications
    enable_cross_repo_sync: bool = True  # Share state across repos

    # Cross-repo paths
    cross_repo_dir: str = "~/.jarvis/cross_repo"
    vmsi_state_file: str = "vmsi_state.json"
    sync_interval_seconds: float = 5.0  # How often to sync state

    # v11.0: Action execution ("Watch & Act")
    enable_action_execution: bool = True  # Execute actions on events
    enable_computer_use: bool = True  # Use Computer Use for actions
    enable_agentic_runner: bool = True  # Use AgenticTaskRunner for workflows
    action_timeout_seconds: float = 60.0  # Max time for action execution
    require_confirmation: bool = False  # Ask before executing actions
    auto_switch_to_window: bool = True  # Automatically switch to target window


class VisualMonitorAgent(BaseNeuralMeshAgent):
    """
    Visual Monitor Agent - The "Watcher" of VMSI.

    Provides background visual surveillance for specific windows.

    Capabilities:
    - watch_and_alert: Monitor a window and alert on event
    - watch_multiple: Monitor multiple windows in parallel
    - stop_watching: Stop specific watcher
    - list_watchers: List all active watchers

    Example:
        result = await coordinator.request(
            to_agent="visual_monitor_agent",
            payload={
                "action": "watch_and_alert",
                "app_name": "Terminal",
                "trigger_text": "Build Successful"
            }
        )
    """

    def __init__(self, config: Optional[VisualMonitorConfig] = None) -> None:
        """Initialize the Visual Monitor Agent."""
        super().__init__(
            agent_name="visual_monitor_agent",
            agent_type="visual_monitor",
            capabilities={
                "watch_and_alert",
                "watch_and_act",  # NEW v11.0: Active response
                "watch_multiple",
                "stop_watching",
                "list_watchers",
                "get_watcher_stats",
                "background_surveillance",  # Meta capability
                "autonomous_response",  # NEW v11.0: Act on visual events
            },
            version="11.0",
        )

        self.config = config or VisualMonitorConfig()

        # Lazy-loaded components - Visual
        self._watcher_manager = None
        self._detector = None
        self._spatial_agent = None

        # Lazy-loaded components - Action Execution (v11.0)
        self._computer_use_connector = None  # For executing actions
        self._agentic_task_runner = None  # For complex workflows

        # Active monitoring tasks
        self._watch_tasks: Dict[str, asyncio.Task] = {}
        self._active_watchers: Dict[str, Any] = {}  # watcher_id -> watcher_info

        # Stats
        self._total_watches_started = 0
        self._total_events_detected = 0
        self._total_alerts_sent = 0
        self._total_actions_executed = 0  # NEW v11.0
        self._total_actions_succeeded = 0  # NEW v11.0
        self._total_actions_failed = 0  # NEW v11.0

        # Cross-repo state
        self._state_sync_task: Optional[asyncio.Task] = None

    async def on_initialize(self) -> None:
        """Initialize agent resources."""
        logger.info("Initializing VisualMonitorAgent v11.0 (VMSI Watcher & Actor)")

        # Initialize video watcher manager
        try:
            from backend.vision.macos_video_capture_advanced import get_watcher_manager
            self._watcher_manager = get_watcher_manager()
            logger.info("✓ VideoWatcherManager initialized")
        except Exception as e:
            logger.warning(f"VideoWatcherManager init failed: {e}")

        # Initialize visual event detector
        try:
            from backend.vision.visual_event_detector import create_detector
            self._detector = create_detector()
            logger.info("✓ VisualEventDetector initialized")
        except Exception as e:
            logger.warning(f"VisualEventDetector init failed: {e}")

        # v11.0: Initialize Computer Use connector for action execution
        if self.config.enable_computer_use:
            try:
                from backend.display.computer_use_connector import get_computer_use_connector
                self._computer_use_connector = get_computer_use_connector()
                logger.info("✓ ClaudeComputerUseConnector initialized (Watch & Act enabled)")
            except Exception as e:
                logger.warning(f"ComputerUseConnector init failed: {e}")
                logger.warning("Watch & Act will be limited to passive mode")

        # v11.0: Initialize AgenticTaskRunner for complex workflows
        if self.config.enable_agentic_runner:
            try:
                from backend.core.agentic_task_runner import get_agentic_task_runner
                self._agentic_task_runner = await get_agentic_task_runner()
                logger.info("✓ AgenticTaskRunner initialized (Complex workflows enabled)")
            except Exception as e:
                logger.warning(f"AgenticTaskRunner init failed: {e}")
                logger.warning("Complex workflows will fall back to Computer Use")

        # Ensure cross-repo directory exists
        if self.config.enable_cross_repo_sync:
            cross_repo_path = Path(self.config.cross_repo_dir).expanduser()
            cross_repo_path.mkdir(parents=True, exist_ok=True)
            logger.info(f"Cross-repo directory: {cross_repo_path}")

        # Subscribe to visual monitoring messages (only if connected)
        if self.message_bus:
            try:
                await self.subscribe(
                    MessageType.CUSTOM,
                    self._handle_visual_message,
                )
            except RuntimeError:
                logger.debug("Message bus not available for subscription")

        # Announce availability (only if connected)
        if self.message_bus:
            try:
                await self.broadcast(
                    message_type=MessageType.ANNOUNCEMENT,
                    payload={
                        "agent": self.agent_name,
                        "event": "agent_ready",
                        "capabilities": list(self.capabilities),
                        "watcher_available": self._watcher_manager is not None,
                        "detector_available": self._detector is not None,
                    },
                )
            except RuntimeError:
                logger.debug("Message bus not available for broadcast")

        logger.info(
            f"VisualMonitorAgent initialized - "
            f"Watcher: {'ACTIVE' if self._watcher_manager else 'INACTIVE'}, "
            f"Detector: {'ACTIVE' if self._detector else 'INACTIVE'}"
        )

    async def on_start(self) -> None:
        """Called when agent starts."""
        logger.info("VisualMonitorAgent started - ready for visual monitoring")

        # Start cross-repo state sync
        if self.config.enable_cross_repo_sync:
            self._state_sync_task = asyncio.create_task(self._sync_state_loop())
            logger.info("Cross-repo state sync started")

    async def on_stop(self) -> None:
        """Cleanup when agent stops."""
        logger.info(
            f"VisualMonitorAgent stopping - "
            f"Watches: {self._total_watches_started}, "
            f"Events: {self._total_events_detected}, "
            f"Alerts: {self._total_alerts_sent}"
        )

        # Stop all active watchers
        if self._watcher_manager:
            await self._watcher_manager.stop_all_watchers()

        # Cancel watch tasks
        for task_id, task in self._watch_tasks.items():
            task.cancel()
            logger.debug(f"Cancelled watch task: {task_id}")

        # Stop state sync
        if self._state_sync_task:
            self._state_sync_task.cancel()

        logger.info("All watchers stopped")

    async def execute_task(self, payload: Dict[str, Any]) -> Any:
        """
        Execute a visual monitoring task.

        Supported actions:
        - watch_and_alert: Monitor a window and alert on event
        - watch_multiple: Monitor multiple windows in parallel
        - stop_watching: Stop a specific watcher
        - list_watchers: List all active watchers
        - get_watcher_stats: Get detailed watcher statistics
        """
        action = payload.get("action", "")

        logger.debug(f"VisualMonitorAgent executing: {action}")

        if action == "watch_and_alert":
            app_name = payload.get("app_name", "")
            trigger_text = payload.get("trigger_text", "")
            space_id = payload.get("space_id")
            return await self.watch_and_alert(app_name, trigger_text, space_id)

        elif action == "watch_multiple":
            watch_specs = payload.get("watch_specs", [])
            return await self.watch_multiple(watch_specs)

        elif action == "stop_watching":
            watcher_id = payload.get("watcher_id", "")
            app_name = payload.get("app_name", "")
            return await self.stop_watching(watcher_id=watcher_id, app_name=app_name)

        elif action == "list_watchers":
            return await self.list_watchers()

        elif action == "get_watcher_stats":
            return self.get_stats()

        else:
            raise ValueError(f"Unknown visual monitoring action: {action}")

    async def watch_and_alert(
        self,
        app_name: str,
        trigger_text: str,
        space_id: Optional[int] = None,
        action_config: Optional[ActionConfig] = None,
        workflow_goal: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Watch an app for specific text/event and alert (or ACT!) when found.

        This is the main capability - enables voice commands like:
        - Passive (v10.6): "Watch the Terminal for 'Build Successful'"
        - ACTIVE (v11.0): "Watch the Terminal for 'Build Complete', then click Deploy"

        Args:
            app_name: App to monitor (e.g., "Terminal", "Chrome")
            trigger_text: Text to wait for (e.g., "Build Successful")
            space_id: Optional specific space (auto-detect if None)
            action_config: Optional action to execute when event detected (v11.0)
            workflow_goal: Optional complex workflow goal (v11.0)

        Returns:
            Result with watcher_id, monitoring status, and action result if executed
        """
        if not app_name or not trigger_text:
            return {
                "success": False,
                "error": "app_name and trigger_text are required"
            }

        if not self._watcher_manager or not self._detector:
            return {
                "success": False,
                "error": "Watcher or detector not available"
            }

        try:
            logger.info(f"Starting watch: {app_name} for '{trigger_text}'")
            self._total_watches_started += 1

            # Step 1: Find window using SpatialAwarenessAgent
            window_info = await self._find_window(app_name, space_id)

            if not window_info['found']:
                return {
                    "success": False,
                    "error": f"Could not find {app_name}",
                    "window_info": window_info
                }

            window_id = window_info['window_id']
            detected_space_id = window_info.get('space_id', 0)

            logger.info(
                f"Found {app_name}: Window {window_id}, Space {detected_space_id}"
            )

            # Step 2: Spawn watcher
            watcher = await self._watcher_manager.spawn_watcher(
                window_id=window_id,
                fps=self.config.default_fps,
                app_name=app_name,
                space_id=detected_space_id,
                priority="low",
                timeout=self.config.default_timeout
            )

            # v11.0: Auto-create action_config from workflow_goal if provided
            if workflow_goal and not action_config:
                action_config = ActionConfig(
                    action_type=ActionType.WORKFLOW,
                    workflow_goal=workflow_goal,
                    narrate=self.config.enable_voice_alerts,
                    switch_to_window=self.config.auto_switch_to_window
                )

            # Step 3: Start monitoring task
            task = asyncio.create_task(
                self._monitor_and_alert(
                    watcher=watcher,
                    trigger_text=trigger_text,
                    app_name=app_name,
                    space_id=detected_space_id,
                    action_config=action_config  # v11.0: Pass action config
                )
            )

            self._watch_tasks[watcher.watcher_id] = task
            self._active_watchers[watcher.watcher_id] = {
                'watcher': watcher,
                'app_name': app_name,
                'trigger_text': trigger_text,
                'space_id': detected_space_id,
                'started_at': datetime.now().isoformat(),
                'action_config': action_config,  # v11.0: Store action config
                'will_act': action_config is not None  # v11.0: Flag for active monitoring
            }

            return {
                "success": True,
                "watcher_id": watcher.watcher_id,
                "window_id": window_id,
                "app_name": app_name,
                "space_id": detected_space_id,
                "trigger_text": trigger_text,
                "message": f"Watching {app_name} on Space {detected_space_id}"
            }

        except Exception as e:
            logger.exception(f"Error in watch_and_alert: {e}")
            return {
                "success": False,
                "error": str(e),
                "app_name": app_name,
                "trigger_text": trigger_text
            }

    async def watch_multiple(
        self,
        watch_specs: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        Monitor multiple windows in parallel.

        Args:
            watch_specs: List of watch specifications
                [
                    {"app": "Terminal", "trigger": "Build Successful"},
                    {"app": "Chrome", "trigger": "Application Submitted"}
                ]

        Returns:
            Results for all watchers
        """
        if not watch_specs:
            return {"success": False, "error": "No watch specs provided"}

        logger.info(f"Starting {len(watch_specs)} parallel watchers")

        # Start all watchers in parallel
        tasks = []
        for spec in watch_specs:
            app_name = spec.get('app', '')
            trigger_text = spec.get('trigger', '')
            space_id = spec.get('space_id')

            task = self.watch_and_alert(app_name, trigger_text, space_id)
            tasks.append(task)

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Collect results
        successful = sum(1 for r in results if isinstance(r, dict) and r.get('success'))
        failed = len(results) - successful

        return {
            "success": successful > 0,
            "total_watchers": len(watch_specs),
            "successful": successful,
            "failed": failed,
            "results": results
        }

    async def stop_watching(
        self,
        watcher_id: Optional[str] = None,
        app_name: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Stop a specific watcher.

        Args:
            watcher_id: Specific watcher ID (or)
            app_name: App name (stops all watchers for this app)

        Returns:
            Stop result
        """
        if not watcher_id and not app_name:
            return {"success": False, "error": "watcher_id or app_name required"}

        stopped_count = 0

        if watcher_id:
            # Stop specific watcher
            if watcher_id in self._active_watchers:
                await self._stop_watcher_by_id(watcher_id)
                stopped_count = 1
            else:
                return {
                    "success": False,
                    "error": f"Watcher {watcher_id} not found"
                }

        elif app_name:
            # Stop all watchers for this app
            watcher_ids = [
                wid for wid, info in self._active_watchers.items()
                if info['app_name'].lower() == app_name.lower()
            ]

            for wid in watcher_ids:
                await self._stop_watcher_by_id(wid)
                stopped_count += 1

            if stopped_count == 0:
                return {
                    "success": False,
                    "error": f"No watchers found for {app_name}"
                }

        return {
            "success": True,
            "stopped_count": stopped_count,
            "message": f"Stopped {stopped_count} watcher(s)"
        }

    async def list_watchers(self) -> Dict[str, Any]:
        """List all active watchers."""
        watchers = []

        for watcher_id, info in self._active_watchers.items():
            watcher = info['watcher']
            watchers.append({
                'watcher_id': watcher_id,
                'app_name': info['app_name'],
                'trigger_text': info['trigger_text'],
                'space_id': info['space_id'],
                'started_at': info['started_at'],
                'stats': watcher.get_stats() if hasattr(watcher, 'get_stats') else {}
            })

        return {
            "active_watchers": len(watchers),
            "watchers": watchers,
            "max_parallel": self.config.max_parallel_watchers
        }

    # =========================================================================
    # Internal Methods
    # =========================================================================

    async def _find_window(
        self,
        app_name: str,
        space_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Find window using SpatialAwarenessAgent.

        Returns window_id and space_id.
        """
        # Try to get SpatialAwarenessAgent from coordinator
        if not self._spatial_agent and self.coordinator:
            try:
                # Request spatial awareness
                result = await self.coordinator.request(
                    to_agent="spatial_awareness_agent",
                    payload={
                        "action": "find_window",
                        "app_name": app_name
                    },
                    timeout=5.0
                )

                if result and result.get('found'):
                    # Get first space where app is found
                    spaces = result.get('spaces', [])
                    if spaces:
                        target_space = spaces[0] if not space_id else space_id

                        # Get window ID from yabai (simplified)
                        # In production, this would integrate with yabai properly
                        window_id = self._estimate_window_id(app_name)

                        return {
                            'found': True,
                            'window_id': window_id,
                            'space_id': target_space,
                            'app_name': app_name
                        }

            except Exception as e:
                logger.warning(f"Error finding window via SpatialAwarenessAgent: {e}")

        # Fallback: estimate window ID
        window_id = self._estimate_window_id(app_name)

        return {
            'found': window_id > 0,
            'window_id': window_id,
            'space_id': space_id or 1,
            'app_name': app_name,
            'fallback': True
        }

    def _estimate_window_id(self, app_name: str) -> int:
        """
        Estimate window ID for an app (fallback method).

        In production, this integrates with yabai or spatial awareness.
        For now, returns a placeholder ID.
        """
        # TODO: Integrate with yabai to get actual window ID
        # For now, return a placeholder
        import hashlib
        hash_val = int(hashlib.md5(app_name.encode()).hexdigest()[:8], 16)
        return hash_val % 10000 + 1000  # ID in range 1000-11000

    async def _monitor_and_alert(
        self,
        watcher: Any,
        trigger_text: str,
        app_name: str,
        space_id: int,
        action_config: Optional[ActionConfig] = None
    ):
        """
        Monitor watcher and send alert (or execute action!) when event detected.

        v11.0: Now supports autonomous action execution!

        This runs as a background task.
        """
        action_result = None

        try:
            logger.info(
                f"[{watcher.watcher_id}] Monitoring {app_name} for '{trigger_text}'"
            )

            if action_config:
                logger.info(
                    f"[{watcher.watcher_id}] 🎯 ACTIVE MODE: Will execute action when detected!"
                )

            # Wait for visual event
            result = await self._watcher_manager.wait_for_visual_event(
                watcher=watcher,
                trigger=trigger_text,
                detector=self._detector,
                timeout=self.config.default_timeout
            )

            if result.detected:
                self._total_events_detected += 1

                logger.info(
                    f"[{watcher.watcher_id}] ✅ Event detected! "
                    f"Trigger: '{trigger_text}', Confidence: {result.confidence:.2f}"
                )

                # Send alert
                await self._send_alert(
                    app_name=app_name,
                    trigger_text=trigger_text,
                    space_id=space_id,
                    confidence=result.confidence,
                    detection_time=result.detection_time
                )

                # v11.0: EXECUTE ACTION if configured!
                if action_config and self.config.enable_action_execution:
                    logger.info(
                        f"[{watcher.watcher_id}] 🚀 Executing action: {action_config.action_type.value}"
                    )

                    action_result = await self._execute_response(
                        trigger_text=trigger_text,
                        detected_text=result.trigger,  # Actual detected text
                        action_config=action_config,
                        app_name=app_name,
                        space_id=space_id,
                        confidence=result.confidence
                    )

                    if action_result and action_result.success:
                        logger.info(
                            f"[{watcher.watcher_id}] ✅ Action executed successfully! "
                            f"Goal: {action_result.goal_executed}"
                        )
                        self._total_actions_succeeded += 1
                    else:
                        logger.warning(
                            f"[{watcher.watcher_id}] ❌ Action failed: "
                            f"{action_result.error if action_result else 'Unknown error'}"
                        )
                        self._total_actions_failed += 1

                # Store in knowledge graph (with action result if available)
                if self.knowledge_graph:
                    knowledge_data = {
                        "type": "visual_event_detected",
                        "app_name": app_name,
                        "trigger_text": trigger_text,
                        "space_id": space_id,
                        "confidence": result.confidence,
                        "detection_time": result.detection_time,
                        "timestamp": datetime.now().isoformat(),
                    }

                    # v11.0: Add action execution result
                    if action_result:
                        knowledge_data["action_executed"] = {
                            "success": action_result.success,
                            "action_type": action_result.action_type.value,
                            "goal": action_result.goal_executed,
                            "error": action_result.error,
                            "duration_ms": action_result.duration_ms
                        }

                    await self.add_knowledge(
                        knowledge_type=KnowledgeType.OBSERVATION,
                        data=knowledge_data,
                        confidence=result.confidence,
                    )
            else:
                logger.info(
                    f"[{watcher.watcher_id}] ⏱️ Timeout waiting for '{trigger_text}'"
                )

            # Cleanup
            await self._stop_watcher_by_id(watcher.watcher_id)

        except Exception as e:
            logger.exception(f"Error in monitor_and_alert: {e}")
            await self._stop_watcher_by_id(watcher.watcher_id)

    async def _send_alert(
        self,
        app_name: str,
        trigger_text: str,
        space_id: int,
        confidence: float,
        detection_time: float
    ):
        """Send voice alert and notification when event detected."""
        self._total_alerts_sent += 1

        # Voice alert
        if self.config.enable_voice_alerts:
            await self._send_voice_alert(app_name, trigger_text, space_id)

        # macOS notification
        if self.config.enable_notifications:
            await self._send_notification(app_name, trigger_text, space_id)

        # Broadcast event to other agents (only if connected)
        if self.message_bus:
            try:
                await self.broadcast(
                    message_type=MessageType.ANNOUNCEMENT,
                    payload={
                        "event": "visual_event_detected",
                        "app_name": app_name,
                        "trigger_text": trigger_text,
                        "space_id": space_id,
                        "confidence": confidence,
                        "detection_time": detection_time,
                        "timestamp": datetime.now().isoformat(),
                    },
                )
            except RuntimeError:
                logger.debug("Message bus not available for broadcast")

    async def _send_voice_alert(
        self,
        app_name: str,
        trigger_text: str,
        space_id: int
    ):
        """Send voice alert via JARVIS Voice API."""
        try:
            # TODO: Integrate with JARVIS Voice API
            # For now, log the alert
            narration = f"{trigger_text} detected on {app_name}, Space {space_id}"
            logger.info(f"🔊 Voice Alert: {narration}")

            # In production, would call:
            # await jarvis_voice_api.speak(narration)

        except Exception as e:
            logger.error(f"Error sending voice alert: {e}")

    async def _send_notification(
        self,
        app_name: str,
        trigger_text: str,
        space_id: int
    ):
        """Send macOS notification."""
        try:
            # Use osascript for macOS notification
            import subprocess

            title = f"JARVIS - {app_name}"
            message = f"{trigger_text} (Space {space_id})"

            subprocess.run([
                'osascript', '-e',
                f'display notification "{message}" with title "{title}"'
            ], check=False, capture_output=True)

            logger.info(f"📬 Notification sent: {message}")

        except Exception as e:
            logger.error(f"Error sending notification: {e}")

    # =========================================================================
    # v11.0: Action Execution - "Watch & Act" Core Logic
    # =========================================================================

    async def _execute_response(
        self,
        trigger_text: str,
        detected_text: str,
        action_config: ActionConfig,
        app_name: str,
        space_id: int,
        confidence: float
    ) -> Optional[ActionExecutionResult]:
        """
        Execute configured action in response to visual event detection.

        This is the CORE of "Watch & Act" - the autonomous loop closer!

        Flow:
        1. Switch to target window (via SpatialAwarenessAgent)
        2. Execute action based on type:
           - SIMPLE_GOAL: Use Computer Use for natural language goal
           - CONDITIONAL: Evaluate conditions and execute matching action
           - WORKFLOW: Delegate to AgenticTaskRunner for complex workflows
           - NOTIFICATION/VOICE_ALERT: Passive modes (already handled)
        3. Return execution result

        Args:
            trigger_text: Original trigger pattern
            detected_text: Actual detected text (may differ slightly)
            action_config: Configuration for action to execute
            app_name: Application where event detected
            space_id: Space ID where event detected
            confidence: Detection confidence

        Returns:
            ActionExecutionResult with execution details
        """
        import time
        start_time = time.time()

        logger.info(
            f"[ACTION EXECUTION] Type: {action_config.action_type.value}, "
            f"App: {app_name}, Space: {space_id}"
        )

        try:
            self._total_actions_executed += 1

            # Step 1: Switch to target window if requested
            if action_config.switch_to_window:
                await self._switch_to_app(app_name, space_id)

            # Step 2: Execute action based on type
            if action_config.action_type == ActionType.SIMPLE_GOAL:
                return await self._execute_simple_goal(
                    goal=action_config.goal,
                    app_name=app_name,
                    narrate=action_config.narrate,
                    timeout=action_config.timeout_seconds,
                    start_time=start_time
                )

            elif action_config.action_type == ActionType.CONDITIONAL:
                return await self._execute_conditional(
                    detected_text=detected_text,
                    conditions=action_config.conditions,
                    default_action=action_config.default_action,
                    app_name=app_name,
                    narrate=action_config.narrate,
                    timeout=action_config.timeout_seconds,
                    start_time=start_time
                )

            elif action_config.action_type == ActionType.WORKFLOW:
                return await self._execute_workflow(
                    workflow_goal=action_config.workflow_goal,
                    workflow_context=action_config.workflow_context,
                    app_name=app_name,
                    narrate=action_config.narrate,
                    timeout=action_config.timeout_seconds,
                    start_time=start_time
                )

            elif action_config.action_type in [ActionType.NOTIFICATION, ActionType.VOICE_ALERT]:
                # Passive modes - already handled by _send_alert
                duration_ms = (time.time() - start_time) * 1000
                return ActionExecutionResult(
                    success=True,
                    action_type=action_config.action_type,
                    goal_executed="Passive notification",
                    duration_ms=duration_ms
                )

            else:
                raise ValueError(f"Unknown action type: {action_config.action_type}")

        except Exception as e:
            logger.exception(f"[ACTION EXECUTION] Failed: {e}")
            duration_ms = (time.time() - start_time) * 1000
            return ActionExecutionResult(
                success=False,
                action_type=action_config.action_type,
                error=str(e),
                duration_ms=duration_ms
            )

    async def _switch_to_app(self, app_name: str, space_id: int) -> bool:
        """
        Switch to target app/window using SpatialAwarenessAgent.

        This is the "teleport" step before executing actions.
        """
        try:
            logger.info(f"[TELEPORT] Switching to {app_name} on Space {space_id}")

            if self.coordinator:
                # Use SpatialAwarenessAgent to switch
                result = await self.coordinator.request(
                    to_agent="spatial_awareness_agent",
                    payload={
                        "action": "switch_to_app",
                        "app_name": app_name,
                        "space_id": space_id
                    }
                )

                if result and result.get("success"):
                    logger.info(f"[TELEPORT] ✅ Switched to {app_name}")
                    await asyncio.sleep(0.5)  # Wait for app to focus
                    return True
                else:
                    logger.warning(f"[TELEPORT] Failed to switch: {result}")
                    return False
            else:
                logger.warning("[TELEPORT] No coordinator available - skipping switch")
                return False

        except Exception as e:
            logger.error(f"[TELEPORT] Error switching to app: {e}")
            return False

    async def _execute_simple_goal(
        self,
        goal: Optional[str],
        app_name: str,
        narrate: bool,
        timeout: float,
        start_time: float
    ) -> ActionExecutionResult:
        """
        Execute simple goal using ClaudeComputerUseConnector.

        Example: "Click the Deploy button"
        """
        if not goal:
            raise ValueError("Simple goal action requires a goal")

        if not self._computer_use_connector:
            raise RuntimeError("Computer Use connector not available")

        logger.info(f"[SIMPLE GOAL] Executing: {goal}")

        try:
            # Execute via Computer Use
            task_result = await self._computer_use_connector.execute_task(
                goal=goal,
                context={"app_name": app_name},
                narrate=narrate
            )

            duration_ms = (time.time() - start_time) * 1000
            success = task_result.status.value in ["success", "SUCCESS"]

            return ActionExecutionResult(
                success=success,
                action_type=ActionType.SIMPLE_GOAL,
                goal_executed=goal,
                error=None if success else task_result.final_message,
                duration_ms=duration_ms,
                computer_use_result=asdict(task_result),
                narration=task_result.narration_log if hasattr(task_result, 'narration_log') else []
            )

        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            return ActionExecutionResult(
                success=False,
                action_type=ActionType.SIMPLE_GOAL,
                goal_executed=goal,
                error=str(e),
                duration_ms=duration_ms
            )

    async def _execute_conditional(
        self,
        detected_text: str,
        conditions: List[ConditionalAction],
        default_action: Optional[str],
        app_name: str,
        narrate: bool,
        timeout: float,
        start_time: float
    ) -> ActionExecutionResult:
        """
        Execute conditional action based on detected text.

        Example conditions:
        - If "Error" -> "Click Retry button"
        - If "Success" -> "Click Deploy button"
        """
        logger.info(f"[CONDITIONAL] Evaluating {len(conditions)} conditions")

        # Find matching condition
        matched_condition = None
        for condition in conditions:
            if self._matches_condition(detected_text, condition):
                matched_condition = condition
                logger.info(
                    f"[CONDITIONAL] ✅ Matched: '{condition.trigger_pattern}' "
                    f"-> '{condition.action_goal}'"
                )
                break

        # Determine which action to execute
        if matched_condition:
            action_goal = matched_condition.action_goal
        elif default_action:
            logger.info(f"[CONDITIONAL] No match, using default action")
            action_goal = default_action
        else:
            duration_ms = (time.time() - start_time) * 1000
            return ActionExecutionResult(
                success=False,
                action_type=ActionType.CONDITIONAL,
                error="No conditions matched and no default action provided",
                duration_ms=duration_ms
            )

        # Execute the matched action via simple goal
        logger.info(f"[CONDITIONAL] Executing action: {action_goal}")
        return await self._execute_simple_goal(
            goal=action_goal,
            app_name=app_name,
            narrate=narrate,
            timeout=timeout,
            start_time=start_time
        )

    def _matches_condition(self, detected_text: str, condition: ConditionalAction) -> bool:
        """Check if detected text matches a conditional pattern."""
        import re

        search_text = detected_text if condition.case_sensitive else detected_text.lower()
        pattern = condition.trigger_pattern if condition.case_sensitive else condition.trigger_pattern.lower()

        if condition.use_regex:
            return bool(re.search(pattern, search_text))
        else:
            return pattern in search_text

    async def _execute_workflow(
        self,
        workflow_goal: Optional[str],
        workflow_context: Dict[str, Any],
        app_name: str,
        narrate: bool,
        timeout: float,
        start_time: float
    ) -> ActionExecutionResult:
        """
        Execute complex workflow using AgenticTaskRunner.

        Example: "Check if tests pass, if so deploy to staging, then notify team"
        """
        if not workflow_goal:
            raise ValueError("Workflow action requires a workflow_goal")

        if not self._agentic_task_runner:
            # Fallback to simple goal via Computer Use
            logger.warning("[WORKFLOW] AgenticTaskRunner unavailable, falling back to Computer Use")
            return await self._execute_simple_goal(
                goal=workflow_goal,
                app_name=app_name,
                narrate=narrate,
                timeout=timeout,
                start_time=start_time
            )

        logger.info(f"[WORKFLOW] Executing complex workflow: {workflow_goal}")

        try:
            # Execute via AgenticTaskRunner
            # Note: Implementation depends on AgenticTaskRunner interface
            # This is a placeholder - adjust based on actual interface
            result = await self._agentic_task_runner.execute_task(
                goal=workflow_goal,
                context={
                    "app_name": app_name,
                    **workflow_context
                },
                narrate=narrate,
                timeout=timeout
            )

            duration_ms = (time.time() - start_time) * 1000
            success = getattr(result, 'success', False)

            return ActionExecutionResult(
                success=success,
                action_type=ActionType.WORKFLOW,
                goal_executed=workflow_goal,
                error=None if success else getattr(result, 'error', 'Unknown error'),
                duration_ms=duration_ms,
                computer_use_result=result if hasattr(result, '__dict__') else None
            )

        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            return ActionExecutionResult(
                success=False,
                action_type=ActionType.WORKFLOW,
                goal_executed=workflow_goal,
                error=str(e),
                duration_ms=duration_ms
            )

    async def _stop_watcher_by_id(self, watcher_id: str):
        """Stop watcher by ID and cleanup."""
        if watcher_id in self._active_watchers:
            info = self._active_watchers[watcher_id]
            watcher = info['watcher']

            # Stop watcher
            if self._watcher_manager:
                await self._watcher_manager.stop_watcher(watcher_id)

            # Cancel task
            if watcher_id in self._watch_tasks:
                task = self._watch_tasks[watcher_id]
                if not task.done():
                    task.cancel()
                del self._watch_tasks[watcher_id]

            # Remove from active watchers
            del self._active_watchers[watcher_id]

            logger.info(f"Stopped watcher {watcher_id}")

    async def _sync_state_loop(self):
        """Sync state to cross-repo file periodically."""
        while True:
            try:
                await asyncio.sleep(self.config.sync_interval_seconds)
                await self._sync_state()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in state sync: {e}")

    async def _sync_state(self):
        """Write current state to cross-repo file."""
        try:
            state_file = Path(self.config.cross_repo_dir).expanduser() / self.config.vmsi_state_file

            state = {
                "active_watchers": [
                    {
                        "watcher_id": watcher_id,
                        "app_name": info['app_name'],
                        "trigger_text": info['trigger_text'],
                        "space_id": info['space_id'],
                        "started_at": info['started_at'],
                        "status": "watching",
                        "repo": "JARVIS-AI-Agent"
                    }
                    for watcher_id, info in self._active_watchers.items()
                ],
                "stats": {
                    "total_watches_started": self._total_watches_started,
                    "total_events_detected": self._total_events_detected,
                    "total_alerts_sent": self._total_alerts_sent,
                    "active_watchers": len(self._active_watchers),
                },
                "last_updated": datetime.now().isoformat(),
            }

            # Write to file
            state_file.write_text(json.dumps(state, indent=2))

        except Exception as e:
            logger.error(f"Error syncing state: {e}")

    async def _handle_visual_message(self, message: AgentMessage) -> None:
        """Handle incoming visual monitoring messages."""
        payload = message.payload

        if payload.get("type") != "visual_request":
            return

        action = payload.get("action")
        if not action:
            return

        logger.info(f"Received visual request from {message.from_agent}: {action}")

        try:
            result = await self.execute_task(payload)

            if self.message_bus:
                await self.message_bus.respond(
                    message,
                    payload={
                        "type": "visual_response",
                        "action": action,
                        "result": result,
                    },
                    from_agent=self.agent_name,
                )

        except Exception as e:
            logger.exception(f"Error handling visual message: {e}")
            if self.message_bus:
                await self.message_bus.respond(
                    message,
                    payload={
                        "type": "visual_response",
                        "action": action,
                        "error": str(e),
                    },
                    from_agent=self.agent_name,
                )

    # =========================================================================
    # Convenience methods
    # =========================================================================

    async def watch(
        self,
        app_name: str,
        trigger_text: str,
        space_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """Quick method to start watching."""
        return await self.watch_and_alert(app_name, trigger_text, space_id)

    def get_stats(self) -> Dict[str, Any]:
        """Get agent statistics."""
        return {
            "total_watches_started": self._total_watches_started,
            "total_events_detected": self._total_events_detected,
            "total_alerts_sent": self._total_alerts_sent,
            "active_watchers": len(self._active_watchers),
            "max_parallel": self.config.max_parallel_watchers,
            "capabilities": list(self.capabilities),
            "version": "10.6",
            "watcher_manager_available": self._watcher_manager is not None,
            "detector_available": self._detector is not None,
        }


# =============================================================================
# Factory function for agent registration
# =============================================================================

async def create_visual_monitor_agent(
    config: Optional[VisualMonitorConfig] = None,
) -> VisualMonitorAgent:
    """
    Create a Visual Monitor Agent.

    This is the factory function used by AgentInitializer.

    Args:
        config: Optional configuration

    Returns:
        Configured VisualMonitorAgent
    """
    return VisualMonitorAgent(config=config)
