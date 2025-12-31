"""
Ghost Hands - Background Automation System
===========================================

A comprehensive system for background window automation without stealing focus.

Components:
- N-Optic Nerve: Multi-window parallel vision monitoring
- Background Actuator: Focus-free window automation
- Narration Engine: Real-time humanistic voice feedback
- Orchestrator: Central coordinator tying everything together

Quick Start:
```python
from ghost_hands import get_ghost_hands, GhostAction

# Get the orchestrator
ghost = await get_ghost_hands()

# Create a watch-and-react task
await ghost.watch_and_react(
    app_name="Terminal",
    trigger_text="BUILD FAILED",
    reaction="npm run build",  # Auto-retry
)

# Or create a complex task with multiple actions
await ghost.create_task(
    name="deploy_watcher",
    watch_app="Terminal",
    trigger_text="Deployment successful",
    actions=[
        GhostAction.narrate_success("Deployment completed!"),
        GhostAction.press_key("escape"),
    ],
)
```

Environment Variables:
- JARVIS_GHOST_VISION: Enable vision system (default: true)
- JARVIS_GHOST_ACTUATOR: Enable actuator system (default: true)
- JARVIS_GHOST_NARRATION: Enable narration (default: true)
- JARVIS_GHOST_YABAI: Enable Yabai integration (default: true)
- JARVIS_OPTIC_FPS: Vision capture FPS (default: 10)
- JARVIS_OPTIC_OCR_ENABLED: Enable OCR (default: true)
- JARVIS_NARRATION_VERBOSITY: SILENT/MINIMAL/NORMAL/VERBOSE (default: NORMAL)
- JARVIS_NARRATION_TONE: professional/friendly/concise/detailed (default: friendly)
"""

# Orchestrator (main entry point)
from ghost_hands.orchestrator import (
    GhostHandsOrchestrator,
    GhostHandsConfig,
    GhostAction,
    GhostTask,
    GhostTaskState,
    GhostActionType,
    TaskExecutionReport,
    get_ghost_hands,
)

# N-Optic Nerve (vision)
from ghost_hands.n_optic_nerve import (
    NOpticNerve,
    NOpticConfig,
    VisionEvent,
    VisionEventType,
    WatchTrigger,
    WatchTarget,
    WatcherState,
    get_n_optic_nerve,
    create_success_trigger,
    create_error_trigger,
    create_build_complete_trigger,
)

# Background Actuator (actions)
from ghost_hands.background_actuator import (
    BackgroundActuator,
    ActuatorConfig,
    Action,
    ActionType,
    ActionResult,
    ActionReport,
    get_background_actuator,
)

# Narration Engine (voice)
from ghost_hands.narration_engine import (
    NarrationEngine,
    NarrationConfig,
    NarrationItem,
    NarrationType,
    NarrationPriority,
    VerbosityLevel,
    NarrationTone,
    get_narration_engine,
)

__all__ = [
    # Orchestrator
    "GhostHandsOrchestrator",
    "GhostHandsConfig",
    "GhostAction",
    "GhostTask",
    "GhostTaskState",
    "GhostActionType",
    "TaskExecutionReport",
    "get_ghost_hands",

    # N-Optic Nerve
    "NOpticNerve",
    "NOpticConfig",
    "VisionEvent",
    "VisionEventType",
    "WatchTrigger",
    "WatchTarget",
    "WatcherState",
    "get_n_optic_nerve",
    "create_success_trigger",
    "create_error_trigger",
    "create_build_complete_trigger",

    # Background Actuator
    "BackgroundActuator",
    "ActuatorConfig",
    "Action",
    "ActionType",
    "ActionResult",
    "ActionReport",
    "get_background_actuator",

    # Narration Engine
    "NarrationEngine",
    "NarrationConfig",
    "NarrationItem",
    "NarrationType",
    "NarrationPriority",
    "VerbosityLevel",
    "NarrationTone",
    "get_narration_engine",
]

__version__ = "1.0.0"
