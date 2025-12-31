"""
Ghost Hands Orchestrator
=========================

The central coordinator for the Ghost Hands system. Connects all components
(N-Optic Nerve, Background Actuator, Narration Engine) into a unified
intelligent automation system.

Core Concept - Ghost Tasks:
A "Ghost Task" is an automated workflow that:
1. Watches for a trigger condition (via N-Optic Nerve)
2. Executes actions when triggered (via Background Actuator)
3. Narrates the process in real-time (via Narration Engine)
4. All WITHOUT stealing user focus

Example Ghost Task:
```python
ghost = await get_ghost_hands()
await ghost.create_task(
    name="auto_retry_build",
    watch_app="Terminal",
    trigger_text="BUILD FAILED",
    actions=[
        GhostAction.wait(seconds=2),
        GhostAction.narrate_intent("retrying the build"),
        GhostAction.type_text("npm run build"),
        GhostAction.press_key("return"),
        GhostAction.narrate_confirmation("build restarted"),
    ],
    one_shot=False,  # Keep watching for future failures
)
```

Architecture:
    GhostHandsOrchestrator (Singleton)
    ├── N-Optic Nerve (vision)
    │   └── Window watchers + OCR triggers
    ├── Background Actuator (actions)
    │   └── Playwright/AppleScript/CGEvent backends
    ├── Narration Engine (voice)
    │   └── TTS integration
    ├── Yabai Intelligence (space awareness)
    │   └── Multi-Space coordination
    └── Ghost Task Manager
        └── Task lifecycle (create, run, pause, stop)

Author: JARVIS AI System
Version: 1.0.0 - Ghost Hands Edition
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Tuple,
    Union,
)

logger = logging.getLogger(__name__)


# =============================================================================
# Configuration
# =============================================================================

@dataclass
class GhostHandsConfig:
    """Configuration for the Ghost Hands Orchestrator."""

    # Component enablement
    vision_enabled: bool = field(
        default_factory=lambda: os.getenv(
            "JARVIS_GHOST_VISION", "true"
        ).lower() == "true"
    )
    actuator_enabled: bool = field(
        default_factory=lambda: os.getenv(
            "JARVIS_GHOST_ACTUATOR", "true"
        ).lower() == "true"
    )
    narration_enabled: bool = field(
        default_factory=lambda: os.getenv(
            "JARVIS_GHOST_NARRATION", "true"
        ).lower() == "true"
    )
    yabai_enabled: bool = field(
        default_factory=lambda: os.getenv(
            "JARVIS_GHOST_YABAI", "true"
        ).lower() == "true"
    )

    # Task settings
    max_concurrent_tasks: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_GHOST_MAX_TASKS", "10"))
    )
    task_timeout_seconds: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_GHOST_TASK_TIMEOUT", "300"))
    )

    # Safety
    require_confirmation_dangerous: bool = field(
        default_factory=lambda: os.getenv(
            "JARVIS_GHOST_CONFIRM_DANGEROUS", "true"
        ).lower() == "true"
    )


# =============================================================================
# Ghost Action Types
# =============================================================================

class GhostActionType(Enum):
    """Types of actions in a Ghost Task."""
    # Basic actions
    CLICK = auto()
    TYPE_TEXT = auto()
    PRESS_KEY = auto()
    SCROLL = auto()

    # Narration
    NARRATE_PERCEPTION = auto()
    NARRATE_INTENT = auto()
    NARRATE_ACTION = auto()
    NARRATE_CONFIRMATION = auto()
    NARRATE_CUSTOM = auto()

    # Control flow
    WAIT = auto()
    WAIT_FOR_TEXT = auto()
    WAIT_FOR_ELEMENT = auto()
    CONDITIONAL = auto()

    # Advanced
    RUN_SCRIPT = auto()
    SWITCH_SPACE = auto()
    FOCUS_WINDOW = auto()
    SCREENSHOT = auto()


class GhostTaskState(Enum):
    """States of a Ghost Task."""
    CREATED = auto()
    WATCHING = auto()
    TRIGGERED = auto()
    EXECUTING = auto()
    COMPLETED = auto()
    PAUSED = auto()
    FAILED = auto()
    CANCELLED = auto()


@dataclass
class GhostAction:
    """
    A single action within a Ghost Task.

    Factory methods provide convenient action creation.
    """
    action_type: GhostActionType
    params: Dict[str, Any] = field(default_factory=dict)

    # Factory methods for easy action creation
    @classmethod
    def click(
        cls,
        selector: Optional[str] = None,
        coordinates: Optional[Tuple[int, int]] = None,
    ) -> "GhostAction":
        """Click on an element or coordinates."""
        return cls(
            action_type=GhostActionType.CLICK,
            params={"selector": selector, "coordinates": coordinates},
        )

    @classmethod
    def type_text(cls, text: str, selector: Optional[str] = None) -> "GhostAction":
        """Type text into an element."""
        return cls(
            action_type=GhostActionType.TYPE_TEXT,
            params={"text": text, "selector": selector},
        )

    @classmethod
    def press_key(
        cls,
        key: str,
        modifiers: Optional[List[str]] = None,
    ) -> "GhostAction":
        """Press a key with optional modifiers."""
        return cls(
            action_type=GhostActionType.PRESS_KEY,
            params={"key": key, "modifiers": modifiers or []},
        )

    @classmethod
    def scroll(cls, direction: str = "down", amount: int = 100) -> "GhostAction":
        """Scroll in a direction."""
        return cls(
            action_type=GhostActionType.SCROLL,
            params={"direction": direction, "amount": amount},
        )

    @classmethod
    def wait(cls, seconds: float) -> "GhostAction":
        """Wait for a duration."""
        return cls(
            action_type=GhostActionType.WAIT,
            params={"seconds": seconds},
        )

    @classmethod
    def wait_for_text(
        cls,
        text: str,
        timeout_seconds: float = 30.0,
    ) -> "GhostAction":
        """Wait for specific text to appear."""
        return cls(
            action_type=GhostActionType.WAIT_FOR_TEXT,
            params={"text": text, "timeout": timeout_seconds},
        )

    @classmethod
    def narrate_perception(cls, description: str) -> "GhostAction":
        """Narrate a perception."""
        return cls(
            action_type=GhostActionType.NARRATE_PERCEPTION,
            params={"description": description},
        )

    @classmethod
    def narrate_intent(cls, action: str) -> "GhostAction":
        """Narrate an intent."""
        return cls(
            action_type=GhostActionType.NARRATE_INTENT,
            params={"action": action},
        )

    @classmethod
    def narrate_action(cls, action: str) -> "GhostAction":
        """Narrate an action in progress."""
        return cls(
            action_type=GhostActionType.NARRATE_ACTION,
            params={"action": action},
        )

    @classmethod
    def narrate_confirmation(cls, result: str) -> "GhostAction":
        """Narrate a confirmation."""
        return cls(
            action_type=GhostActionType.NARRATE_CONFIRMATION,
            params={"result": result},
        )

    @classmethod
    def narrate_custom(cls, text: str) -> "GhostAction":
        """Narrate custom text."""
        return cls(
            action_type=GhostActionType.NARRATE_CUSTOM,
            params={"text": text},
        )

    @classmethod
    def run_script(cls, script: str, script_type: str = "applescript") -> "GhostAction":
        """Run a custom script."""
        return cls(
            action_type=GhostActionType.RUN_SCRIPT,
            params={"script": script, "type": script_type},
        )

    @classmethod
    def switch_space(cls, space_id: int) -> "GhostAction":
        """Switch to a specific Space."""
        return cls(
            action_type=GhostActionType.SWITCH_SPACE,
            params={"space_id": space_id},
        )

    @classmethod
    def screenshot(cls, save_path: Optional[str] = None) -> "GhostAction":
        """Take a screenshot."""
        return cls(
            action_type=GhostActionType.SCREENSHOT,
            params={"save_path": save_path},
        )


@dataclass
class GhostTask:
    """
    A Ghost Task - an automated workflow triggered by vision events.
    """
    name: str
    watch_app: Optional[str] = None
    watch_window_id: Optional[int] = None
    trigger_text: Optional[List[str]] = None
    trigger_pattern: Optional[str] = None
    actions: List[GhostAction] = field(default_factory=list)
    one_shot: bool = True
    enabled: bool = True
    priority: int = 5

    # Runtime state
    state: GhostTaskState = GhostTaskState.CREATED
    trigger_count: int = 0
    last_triggered: Optional[datetime] = None
    created_at: datetime = field(default_factory=datetime.now)
    error: Optional[str] = None

    # Internal
    _watcher_ids: List[int] = field(default_factory=list)

    def __post_init__(self):
        """Normalize trigger_text to list."""
        if isinstance(self.trigger_text, str):
            self.trigger_text = [self.trigger_text]


@dataclass
class TaskExecutionReport:
    """Report of a Ghost Task execution."""
    task_name: str
    trigger_event: Any
    actions_executed: int
    actions_succeeded: int
    actions_failed: int
    total_duration_ms: float
    errors: List[str] = field(default_factory=list)
    timestamp: datetime = field(default_factory=datetime.now)


# =============================================================================
# Ghost Hands Orchestrator
# =============================================================================

class GhostHandsOrchestrator:
    """
    Ghost Hands Orchestrator: Central coordinator for background automation.

    Connects N-Optic Nerve (vision), Background Actuator (actions), and
    Narration Engine (voice) into a unified intelligent automation system.
    """

    _instance: Optional["GhostHandsOrchestrator"] = None

    def __new__(cls, config: Optional[GhostHandsConfig] = None):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self, config: Optional[GhostHandsConfig] = None):
        if self._initialized:
            return

        self.config = config or GhostHandsConfig()

        # Components (lazy loaded)
        self._n_optic: Optional[Any] = None
        self._actuator: Optional[Any] = None
        self._narration: Optional[Any] = None
        self._yabai: Optional[Any] = None

        # Task management
        self._tasks: Dict[str, GhostTask] = {}
        self._execution_history: List[TaskExecutionReport] = []
        self._max_history = 100

        # State
        self._is_running = False
        self._shutdown_event = asyncio.Event()

        # Statistics
        self._stats = {
            "total_tasks_created": 0,
            "total_triggers": 0,
            "total_actions_executed": 0,
            "successful_executions": 0,
            "failed_executions": 0,
            "start_time": None,
        }

        self._initialized = True
        logger.info("[GHOST-HANDS] Orchestrator initialized")

    @classmethod
    def get_instance(cls, config: Optional[GhostHandsConfig] = None) -> "GhostHandsOrchestrator":
        """Get singleton instance."""
        return cls(config)

    async def start(self) -> bool:
        """Start the Ghost Hands system."""
        if self._is_running:
            return True

        logger.info("[GHOST-HANDS] Starting Ghost Hands system...")

        # Initialize components
        if self.config.vision_enabled:
            await self._init_vision()

        if self.config.actuator_enabled:
            await self._init_actuator()

        if self.config.narration_enabled:
            await self._init_narration()

        if self.config.yabai_enabled:
            await self._init_yabai()

        self._is_running = True
        self._stats["start_time"] = datetime.now()

        # Narrate startup
        if self._narration:
            await self._narration.narrate_greeting()

        logger.info("[GHOST-HANDS] System started")
        return True

    async def stop(self) -> None:
        """Stop the Ghost Hands system."""
        logger.info("[GHOST-HANDS] Stopping Ghost Hands system...")

        self._shutdown_event.set()

        # Cancel all tasks
        for task in self._tasks.values():
            await self._cancel_task(task)

        # Stop components
        if self._n_optic:
            await self._n_optic.stop()

        if self._actuator:
            await self._actuator.stop()

        if self._narration:
            await self._narration.stop()

        self._is_running = False
        logger.info("[GHOST-HANDS] System stopped")

    # =========================================================================
    # Component Initialization
    # =========================================================================

    async def _init_vision(self) -> None:
        """Initialize N-Optic Nerve."""
        try:
            from ghost_hands.n_optic_nerve import get_n_optic_nerve

            self._n_optic = await get_n_optic_nerve()
            self._n_optic.on_event(self._on_vision_event)
            logger.info("[GHOST-HANDS] N-Optic Nerve connected")
        except Exception as e:
            logger.warning(f"[GHOST-HANDS] N-Optic Nerve init failed: {e}")

    async def _init_actuator(self) -> None:
        """Initialize Yabai-Aware Actuator for cross-space capabilities."""
        try:
            # Use Yabai-Aware Actuator for cross-space window targeting
            from ghost_hands.yabai_aware_actuator import get_yabai_actuator

            self._actuator = await get_yabai_actuator()
            logger.info("[GHOST-HANDS] Yabai-Aware Actuator connected (cross-space enabled)")
        except Exception as e:
            logger.warning(f"[GHOST-HANDS] Yabai-Aware Actuator init failed: {e}")
            # Fallback to legacy actuator if Yabai isn't available
            try:
                from ghost_hands.background_actuator import get_background_actuator
                self._actuator = await get_background_actuator()
                logger.info("[GHOST-HANDS] Fallback: Background Actuator connected")
            except Exception as e2:
                logger.error(f"[GHOST-HANDS] All actuators failed: {e2}")

    async def _init_narration(self) -> None:
        """Initialize Narration Engine."""
        try:
            from ghost_hands.narration_engine import get_narration_engine

            self._narration = await get_narration_engine()
            logger.info("[GHOST-HANDS] Narration Engine connected")
        except Exception as e:
            logger.warning(f"[GHOST-HANDS] Narration Engine init failed: {e}")

    async def _init_yabai(self) -> None:
        """Initialize Yabai Intelligence."""
        try:
            from intelligence.yabai_spatial_intelligence import get_yabai_intelligence

            self._yabai = await get_yabai_intelligence()
            logger.info("[GHOST-HANDS] Yabai Intelligence connected")
        except Exception as e:
            logger.warning(f"[GHOST-HANDS] Yabai Intelligence init failed: {e}")

    # =========================================================================
    # Task Management
    # =========================================================================

    async def create_task(
        self,
        name: str,
        watch_app: Optional[str] = None,
        watch_window_id: Optional[int] = None,
        trigger_text: Optional[Union[str, List[str]]] = None,
        trigger_pattern: Optional[str] = None,
        actions: Optional[List[GhostAction]] = None,
        one_shot: bool = True,
        priority: int = 5,
    ) -> GhostTask:
        """
        Create and start a Ghost Task.

        Args:
            name: Unique name for the task
            watch_app: Application name to watch
            watch_window_id: Specific window ID to watch
            trigger_text: Text pattern(s) that trigger the task
            trigger_pattern: Regex pattern that triggers the task
            actions: List of actions to execute when triggered
            one_shot: If True, task stops after first trigger
            priority: Task priority (1-10, lower = higher priority)

        Returns:
            The created GhostTask
        """
        if name in self._tasks:
            raise ValueError(f"Task '{name}' already exists")

        if len(self._tasks) >= self.config.max_concurrent_tasks:
            raise RuntimeError("Maximum concurrent tasks reached")

        task = GhostTask(
            name=name,
            watch_app=watch_app,
            watch_window_id=watch_window_id,
            trigger_text=trigger_text if isinstance(trigger_text, list) else (
                [trigger_text] if trigger_text else None
            ),
            trigger_pattern=trigger_pattern,
            actions=actions or [],
            one_shot=one_shot,
            priority=priority,
        )

        self._tasks[name] = task
        self._stats["total_tasks_created"] += 1

        # Start watching
        await self._start_task_watching(task)

        logger.info(f"[GHOST-HANDS] Created task '{name}'")

        # Narrate task creation
        if self._narration:
            await self._narration.narrate_status(
                f"Ghost task '{name}' is now active"
            )

        return task

    async def _start_task_watching(self, task: GhostTask) -> None:
        """Start vision monitoring for a task."""
        if not self._n_optic or not task.enabled:
            return

        # Build triggers
        from ghost_hands.n_optic_nerve import WatchTrigger, VisionEventType

        triggers = []

        if task.trigger_text:
            for text in task.trigger_text:
                triggers.append(WatchTrigger(
                    name=f"{task.name}:text:{text[:20]}",
                    trigger_type=VisionEventType.TEXT_DETECTED,
                    pattern=text,
                    callback=lambda e, t=task: asyncio.create_task(
                        self._on_task_triggered(t, e)
                    ),
                    one_shot=task.one_shot,
                ))

        if task.trigger_pattern:
            import re
            triggers.append(WatchTrigger(
                name=f"{task.name}:pattern",
                trigger_type=VisionEventType.PATTERN_MATCH,
                pattern=re.compile(task.trigger_pattern),
                callback=lambda e, t=task: asyncio.create_task(
                    self._on_task_triggered(t, e)
                ),
                one_shot=task.one_shot,
            ))

        if not triggers:
            logger.warning(f"[GHOST-HANDS] Task '{task.name}' has no triggers")
            return

        # Start watching
        if task.watch_window_id:
            success = await self._n_optic.watch_window(
                task.watch_window_id,
                triggers,
            )
            if success:
                task._watcher_ids.append(task.watch_window_id)

        elif task.watch_app:
            count = await self._n_optic.watch_app(
                task.watch_app,
                triggers,
                all_windows=True,
            )
            logger.info(
                f"[GHOST-HANDS] Task '{task.name}' watching {count} "
                f"windows of {task.watch_app}"
            )

        task.state = GhostTaskState.WATCHING

    async def pause_task(self, name: str) -> bool:
        """Pause a task (stops watching but keeps task defined)."""
        if name not in self._tasks:
            return False

        task = self._tasks[name]
        task.state = GhostTaskState.PAUSED
        task.enabled = False

        # Stop watchers
        if self._n_optic:
            for window_id in task._watcher_ids:
                await self._n_optic.stop_watching(window_id)
            task._watcher_ids.clear()

        logger.info(f"[GHOST-HANDS] Paused task '{name}'")
        return True

    async def resume_task(self, name: str) -> bool:
        """Resume a paused task."""
        if name not in self._tasks:
            return False

        task = self._tasks[name]
        task.enabled = True

        await self._start_task_watching(task)

        logger.info(f"[GHOST-HANDS] Resumed task '{name}'")
        return True

    async def cancel_task(self, name: str) -> bool:
        """Cancel and remove a task."""
        if name not in self._tasks:
            return False

        task = self._tasks[name]
        await self._cancel_task(task)
        del self._tasks[name]

        logger.info(f"[GHOST-HANDS] Cancelled task '{name}'")
        return True

    async def _cancel_task(self, task: GhostTask) -> None:
        """Internal task cancellation."""
        task.state = GhostTaskState.CANCELLED
        task.enabled = False

        if self._n_optic:
            for window_id in task._watcher_ids:
                await self._n_optic.stop_watching(window_id)

    def get_task(self, name: str) -> Optional[GhostTask]:
        """Get a task by name."""
        return self._tasks.get(name)

    def list_tasks(self) -> List[Dict[str, Any]]:
        """List all tasks with their status."""
        return [
            {
                "name": task.name,
                "state": task.state.name,
                "watch_app": task.watch_app,
                "trigger_count": task.trigger_count,
                "enabled": task.enabled,
                "created_at": task.created_at.isoformat(),
            }
            for task in self._tasks.values()
        ]

    # =========================================================================
    # Event Handling
    # =========================================================================

    def _on_vision_event(self, event: Any) -> None:
        """Handle vision events from N-Optic Nerve."""
        # Forward to narration if enabled
        if self._narration:
            asyncio.create_task(self._narration.on_vision_event(event))

    async def _on_task_triggered(self, task: GhostTask, event: Any) -> None:
        """Handle task trigger event."""
        logger.info(
            f"[GHOST-HANDS] Task '{task.name}' triggered by: "
            f"{event.matched_pattern or 'event'}"
        )

        self._stats["total_triggers"] += 1
        task.trigger_count += 1
        task.last_triggered = datetime.now()
        task.state = GhostTaskState.TRIGGERED

        # Narrate the trigger
        if self._narration:
            await self._narration.narrate_perception(
                f"trigger condition for '{task.name}'",
                location=f"{event.app_name} on Space {event.space_id}",
            )

        # Execute the task
        await self._execute_task(task, event)

    async def _execute_task(self, task: GhostTask, trigger_event: Any) -> TaskExecutionReport:
        """Execute a Ghost Task's actions."""
        task.state = GhostTaskState.EXECUTING
        start_time = datetime.now()

        report = TaskExecutionReport(
            task_name=task.name,
            trigger_event=trigger_event,
            actions_executed=0,
            actions_succeeded=0,
            actions_failed=0,
            total_duration_ms=0,
        )

        try:
            for action in task.actions:
                success = await self._execute_action(action, task, trigger_event)
                report.actions_executed += 1

                if success:
                    report.actions_succeeded += 1
                else:
                    report.actions_failed += 1
                    report.errors.append(f"Action {action.action_type.name} failed")

                self._stats["total_actions_executed"] += 1

            # Task completed
            if report.actions_failed == 0:
                task.state = GhostTaskState.COMPLETED
                self._stats["successful_executions"] += 1
            else:
                task.state = GhostTaskState.FAILED
                self._stats["failed_executions"] += 1

        except Exception as e:
            task.state = GhostTaskState.FAILED
            task.error = str(e)
            report.errors.append(str(e))
            self._stats["failed_executions"] += 1
            logger.error(f"[GHOST-HANDS] Task '{task.name}' execution error: {e}")

        report.total_duration_ms = (
            datetime.now() - start_time
        ).total_seconds() * 1000

        # Record history
        self._execution_history.append(report)
        if len(self._execution_history) > self._max_history:
            self._execution_history = self._execution_history[-self._max_history:]

        # Resume watching if not one-shot
        if not task.one_shot and task.enabled:
            task.state = GhostTaskState.WATCHING

        logger.info(
            f"[GHOST-HANDS] Task '{task.name}' completed: "
            f"{report.actions_succeeded}/{report.actions_executed} actions succeeded"
        )

        return report

    async def _execute_action(
        self,
        action: GhostAction,
        task: GhostTask,
        trigger_event: Any,
    ) -> bool:
        """
        Execute a single action with cross-space window targeting.

        The trigger_event from N-Optic Nerve contains the exact window_id and space_id
        where the trigger was detected. We pass this targeting data to the actuator
        for surgical, cross-space action execution.
        """
        try:
            action_type = action.action_type
            params = action.params

            # Extract targeting data from the Vision Event (THE KEY INSIGHT!)
            # This is what enables cross-space actions - we know exactly which window
            window_id = getattr(trigger_event, 'window_id', None)
            space_id = getattr(trigger_event, 'space_id', None)
            app_name = getattr(trigger_event, 'app_name', None) or task.watch_app

            # Log targeting info for debugging
            if window_id:
                logger.debug(
                    f"[GHOST-HANDS] Action targeting: window={window_id}, "
                    f"space={space_id}, app={app_name}"
                )

            # Narration actions
            if action_type == GhostActionType.NARRATE_PERCEPTION:
                if self._narration:
                    await self._narration.narrate_perception(params.get("description", ""))
                return True

            elif action_type == GhostActionType.NARRATE_INTENT:
                if self._narration:
                    await self._narration.narrate_intent(params.get("action", ""))
                return True

            elif action_type == GhostActionType.NARRATE_ACTION:
                if self._narration:
                    await self._narration.narrate_action(params.get("action", ""))
                return True

            elif action_type == GhostActionType.NARRATE_CONFIRMATION:
                if self._narration:
                    await self._narration.narrate_confirmation(params.get("result", ""))
                return True

            elif action_type == GhostActionType.NARRATE_CUSTOM:
                if self._narration:
                    await self._narration.narrate_custom(params.get("text", ""))
                return True

            # Control flow actions
            elif action_type == GhostActionType.WAIT:
                await asyncio.sleep(params.get("seconds", 1))
                return True

            elif action_type == GhostActionType.WAIT_FOR_TEXT:
                # Wait for text to appear (polling N-Optic Nerve)
                text = params.get("text", "")
                timeout = params.get("timeout", 30)
                # Simplified - actual implementation would poll vision
                await asyncio.sleep(min(timeout, 5))
                return True

            # Actuator actions - NOW WITH CROSS-SPACE TARGETING!
            # The window_id and space_id from trigger_event enable surgical actions
            elif action_type == GhostActionType.CLICK:
                if not self._actuator:
                    return False
                report = await self._actuator.click(
                    app_name=app_name,
                    window_id=window_id,      # THE GOLDEN PATH: exact window targeting
                    space_id=space_id,        # Space hint for faster routing
                    selector=params.get("selector"),
                    coordinates=params.get("coordinates"),
                )
                return report.result.name == "SUCCESS"

            elif action_type == GhostActionType.TYPE_TEXT:
                if not self._actuator:
                    return False
                report = await self._actuator.type_text(
                    text=params.get("text", ""),
                    app_name=app_name,
                    window_id=window_id,      # Cross-space typing
                    space_id=space_id,
                    selector=params.get("selector"),
                )
                return report.result.name == "SUCCESS"

            elif action_type == GhostActionType.PRESS_KEY:
                if not self._actuator:
                    return False
                report = await self._actuator.press_key(
                    key=params.get("key", "return"),
                    app_name=app_name,
                    window_id=window_id,      # Cross-space key press
                    space_id=space_id,
                    modifiers=params.get("modifiers"),
                )
                return report.result.name == "SUCCESS"

            elif action_type == GhostActionType.RUN_SCRIPT:
                if not self._actuator:
                    return False
                report = await self._actuator.run_applescript(
                    script=params.get("script", ""),
                    app_name=app_name,
                    window_id=window_id,      # Context for logging
                )
                return report.result.name == "SUCCESS"

            elif action_type == GhostActionType.SWITCH_SPACE:
                if not self._yabai:
                    return False
                space_id = params.get("space_id", 1)
                # Use Yabai to switch space
                try:
                    import subprocess
                    subprocess.run(
                        ["yabai", "-m", "space", "--focus", str(space_id)],
                        capture_output=True,
                        timeout=5,
                    )
                    return True
                except Exception:
                    return False

            else:
                logger.warning(
                    f"[GHOST-HANDS] Unknown action type: {action_type.name}"
                )
                return False

        except Exception as e:
            logger.error(f"[GHOST-HANDS] Action execution error: {e}")
            return False

    # =========================================================================
    # High-Level API
    # =========================================================================

    async def watch_and_react(
        self,
        app_name: str,
        trigger_text: str,
        reaction: Union[str, List[GhostAction]],
        task_name: Optional[str] = None,
    ) -> GhostTask:
        """
        Convenience method to quickly set up a watch-and-react task.

        Args:
            app_name: Application to watch
            trigger_text: Text that triggers the reaction
            reaction: Either a string to type, or list of GhostActions
            task_name: Optional task name (auto-generated if not provided)

        Returns:
            The created GhostTask
        """
        if isinstance(reaction, str):
            actions = [
                GhostAction.narrate_intent(f"respond to '{trigger_text}'"),
                GhostAction.type_text(reaction),
                GhostAction.press_key("return"),
                GhostAction.narrate_confirmation("response sent"),
            ]
        else:
            actions = reaction

        name = task_name or f"react_{app_name}_{trigger_text[:10]}"

        return await self.create_task(
            name=name,
            watch_app=app_name,
            trigger_text=trigger_text,
            actions=actions,
            one_shot=False,
        )

    async def auto_retry_on_failure(
        self,
        app_name: str,
        failure_text: str,
        retry_command: str,
        max_retries: int = 3,
    ) -> GhostTask:
        """
        Set up automatic retry when a failure is detected.

        Args:
            app_name: Application to watch (e.g., "Terminal")
            failure_text: Text indicating failure (e.g., "BUILD FAILED")
            retry_command: Command to retry (e.g., "npm run build")
            max_retries: Maximum retry attempts
        """
        actions = [
            GhostAction.wait(seconds=2),
            GhostAction.narrate_intent("retry the failed operation"),
            GhostAction.type_text(retry_command),
            GhostAction.press_key("return"),
            GhostAction.narrate_action("retrying"),
        ]

        return await self.create_task(
            name=f"auto_retry_{app_name}",
            watch_app=app_name,
            trigger_text=failure_text,
            actions=actions,
            one_shot=False,  # Keep retrying
        )

    async def notify_on_completion(
        self,
        app_name: str,
        completion_text: str,
        notification: str,
    ) -> GhostTask:
        """
        Set up notification when a task completes.

        Args:
            app_name: Application to watch
            completion_text: Text indicating completion
            notification: What to narrate when complete
        """
        actions = [
            GhostAction.narrate_success(notification),
        ]

        return await self.create_task(
            name=f"notify_{app_name}",
            watch_app=app_name,
            trigger_text=completion_text,
            actions=actions,
            one_shot=True,
        )

    # =========================================================================
    # Statistics and Monitoring
    # =========================================================================

    def get_stats(self) -> Dict[str, Any]:
        """Get orchestrator statistics."""
        component_status = {
            "n_optic": self._n_optic is not None,
            "actuator": self._actuator is not None,
            "narration": self._narration is not None,
            "yabai": self._yabai is not None,
        }

        return {
            **self._stats,
            "is_running": self._is_running,
            "active_tasks": len(self._tasks),
            "watching_tasks": sum(
                1 for t in self._tasks.values()
                if t.state == GhostTaskState.WATCHING
            ),
            "components": component_status,
            "execution_history_size": len(self._execution_history),
        }

    def get_execution_history(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get recent execution history."""
        return [
            {
                "task_name": r.task_name,
                "actions_executed": r.actions_executed,
                "actions_succeeded": r.actions_succeeded,
                "duration_ms": r.total_duration_ms,
                "errors": r.errors,
                "timestamp": r.timestamp.isoformat(),
            }
            for r in self._execution_history[-limit:]
        ]


# =============================================================================
# Convenience Functions
# =============================================================================

async def get_ghost_hands(
    config: Optional[GhostHandsConfig] = None
) -> GhostHandsOrchestrator:
    """Get the Ghost Hands Orchestrator singleton instance."""
    orchestrator = GhostHandsOrchestrator.get_instance(config)
    if not orchestrator._is_running:
        await orchestrator.start()
    return orchestrator


# =============================================================================
# Testing
# =============================================================================

async def test_ghost_hands():
    """Test the Ghost Hands system."""
    print("=" * 60)
    print("Testing Ghost Hands Orchestrator")
    print("=" * 60)

    # Get orchestrator
    ghost = await get_ghost_hands()

    # Show component status
    print("\n1. Component Status:")
    stats = ghost.get_stats()
    for component, status in stats.get("components", {}).items():
        status_str = "connected" if status else "not available"
        print(f"   {component}: {status_str}")

    # Create a simple task
    print("\n2. Creating test task...")
    try:
        task = await ghost.create_task(
            name="test_build_watcher",
            watch_app="Terminal",
            trigger_text=["BUILD FAILED", "ERROR"],
            actions=[
                GhostAction.narrate_perception("a build failure"),
                GhostAction.wait(seconds=1),
                GhostAction.narrate_intent("check the error details"),
            ],
            one_shot=True,
        )
        print(f"   Created task: {task.name} (state: {task.state.name})")
    except Exception as e:
        print(f"   Failed to create task: {e}")

    # List tasks
    print("\n3. Active Tasks:")
    for task_info in ghost.list_tasks():
        print(f"   - {task_info['name']}: {task_info['state']}")

    # Show stats
    print("\n4. Statistics:")
    for key, value in ghost.get_stats().items():
        if key != "components":
            print(f"   {key}: {value}")

    # Cleanup
    await ghost.stop()
    print("\nTest complete!")


if __name__ == "__main__":
    asyncio.run(test_ghost_hands())
