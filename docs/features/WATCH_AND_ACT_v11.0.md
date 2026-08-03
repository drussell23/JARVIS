# Watch & Act - VMSI v11.0 🎯

## The Autonomous Loop is Complete!

**Video Multi-Space Intelligence (VMSI) v11.0** now closes the autonomous loop:
- ✅ **v10.6**: Watch background windows (Passive monitoring)
- ✅ **v11.0**: AUTOMATICALLY ACT when events detected (Active response)

**The Loop**:
```
SpatialAwarenessAgent (The Map) → Finds windows
         ↓
VisualMonitorAgent (The Watcher) → Detects events
         ↓
ClaudeComputerUse (The Actor) → Executes actions
         ↓
COMPLETE AUTONOMOUS OPERATION!
```

---

## 🚀 New Capabilities in v11.0

### 1. Simple Goal Actions
**Voice Command**: "Watch the Terminal for 'Build Complete', then click Deploy"

```python
from backend.neural_mesh.agents.visual_monitor_agent import (
    ActionConfig,
    ActionType
)

# Create action configuration
action_config = ActionConfig(
    action_type=ActionType.SIMPLE_GOAL,
    goal="Click the Deploy button",
    switch_to_window=True,  # Auto-switch to window before acting
    narrate=True  # Voice narration during execution
)

# Start watching with action
result = await visual_monitor_agent.watch_and_alert(
    app_name="Terminal",
    trigger_text="Build Complete",
    action_config=action_config
)
```

**What happens**:
1. JARVIS watches Terminal for "Build Complete"
2. When detected, JARVIS switches to Terminal
3. JARVIS executes "Click the Deploy button" via Computer Use
4. Voice narration: "Build complete detected. Clicking Deploy button..."
5. Success notification

---

### 2. Conditional Branching
**Voice Command**: "Watch Terminal, if Error click Retry, if Success click Deploy"

```python
from backend.neural_mesh.agents.visual_monitor_agent import (
    ActionConfig,
    ActionType,
    ConditionalAction
)

# Create conditional actions
conditions = [
    ConditionalAction(
        trigger_pattern="Error",
        action_goal="Click the Retry button",
        description="Retry on error"
    ),
    ConditionalAction(
        trigger_pattern="Success",
        action_goal="Click the Deploy button",
        description="Deploy on success"
    ),
    ConditionalAction(
        trigger_pattern="Warning",
        action_goal="Click the Continue Anyway button",
        description="Continue despite warnings"
    )
]

action_config = ActionConfig(
    action_type=ActionType.CONDITIONAL,
    conditions=conditions,
    default_action="Click the Cancel button",  # Fallback
    switch_to_window=True,
    narrate=True
)

result = await visual_monitor_agent.watch_and_alert(
    app_name="Terminal",
    trigger_text="Error|Success|Warning",  # Watch for any of these
    action_config=action_config
)
```

**What happens**:
1. JARVIS watches Terminal for any trigger
2. When "Error" detected → Clicks Retry
3. When "Success" detected → Clicks Deploy
4. When "Warning" detected → Clicks Continue Anyway
5. If something else detected → Clicks Cancel (default)

**Smart Matching**:
- Supports regex patterns
- Case-insensitive by default
- Confidence threshold filtering

---

### 3. Complex Workflows
**Voice Command**: "Watch Chrome for 'Submitted', then verify email was sent and notify team on Slack"

```python
action_config = ActionConfig(
    action_type=ActionType.WORKFLOW,
    workflow_goal="Verify submission email was sent, then post success message to #deployments channel on Slack",
    workflow_context={
        "submission_app": "Chrome",
        "email_client": "Mail.app",
        "slack_channel": "#deployments"
    },
    switch_to_window=True,
    narrate=True,
    timeout_seconds=120.0  # 2 minutes for complex workflow
)

result = await visual_monitor_agent.watch_and_alert(
    app_name="Chrome",
    trigger_text="Application Submitted",
    action_config=action_config
)
```

**What happens**:
1. JARVIS watches Chrome for "Application Submitted"
2. When detected, delegates to `AgenticTaskRunner` for complex workflow
3. AgenticTaskRunner:
   - Switches to Mail.app
   - Checks for confirmation email
   - Switches to Slack
   - Posts success message
   - Returns to original window
4. Complete autonomous multi-app workflow!

---

## 📋 Complete API Reference

### ActionType Enum
```python
class ActionType(str, Enum):
    SIMPLE_GOAL = "simple_goal"       # Natural language goal via Computer Use
    CONDITIONAL = "conditional"       # If-then-else branching
    WORKFLOW = "workflow"             # Complex multi-step via AgenticTaskRunner
    NOTIFICATION = "notification"     # Passive mode (v10.6 behavior)
    VOICE_ALERT = "voice_alert"       # Voice only
```

### ActionConfig
```python
@dataclass
class ActionConfig:
    action_type: ActionType              # Type of action

    # Simple goal
    goal: Optional[str] = None           # e.g., "Click Deploy"

    # Conditional
    conditions: List[ConditionalAction] = []
    default_action: Optional[str] = None

    # Workflow
    workflow_goal: Optional[str] = None
    workflow_context: Dict[str, Any] = {}

    # Common settings
    switch_to_window: bool = True        # Auto-switch before acting
    narrate: bool = True                 # Voice narration
    require_confirmation: bool = False   # Ask user first
    timeout_seconds: float = 30.0        # Max execution time
```

### ConditionalAction
```python
@dataclass
class ConditionalAction:
    trigger_pattern: str                 # Text pattern (supports regex)
    action_goal: str                     # Action to execute
    description: str = ""                # Human-readable
    confidence_threshold: float = 0.75   # Min confidence
    case_sensitive: bool = False
    use_regex: bool = False              # Enable regex matching
```

### watch_and_alert() (Upgraded in v11.0)
```python
async def watch_and_alert(
    app_name: str,                       # App to watch
    trigger_text: str,                   # Text to detect
    space_id: Optional[int] = None,      # Specific space (auto-detect if None)
    action_config: Optional[ActionConfig] = None,  # NEW v11.0!
    workflow_goal: Optional[str] = None  # NEW v11.0! Shortcut for workflows
) -> Dict[str, Any]
```

**Returns**:
```python
{
    "success": True,
    "watcher_id": "watcher_992_1735340234",
    "window_id": 992,
    "app_name": "Terminal",
    "space_id": 4,
    "trigger_text": "Build Complete",
    "action_result": {  # NEW v11.0!
        "success": True,
        "action_type": "simple_goal",
        "goal_executed": "Click the Deploy button",
        "duration_ms": 1234.5,
        "narration": ["Switching to Terminal...", "Clicking Deploy button..."]
    }
}
```

---

## 🎯 Real-World Use Cases

### Use Case 1: CI/CD Pipeline Automation
```python
# Watch for build completion, then deploy automatically
action_config = ActionConfig(
    action_type=ActionType.CONDITIONAL,
    conditions=[
        ConditionalAction(
            trigger_pattern="Build.*Successful",
            action_goal="Click the Deploy to Staging button",
            use_regex=True
        ),
        ConditionalAction(
            trigger_pattern="Build.*Failed",
            action_goal="Click View Logs button, then screenshot the error",
            use_regex=True
        )
    ],
    narrate=True
)

await visual_monitor_agent.watch_and_alert(
    app_name="Terminal",
    trigger_text="Build",
    action_config=action_config
)
```

**Autonomous Behavior**:
- Build succeeds → Auto-deploys to staging
- Build fails → Auto-captures error logs
- Zero human intervention required!

---

### Use Case 2: Form Submission Flow
```python
# Watch for form submission, verify success, notify team
action_config = ActionConfig(
    action_type=ActionType.WORKFLOW,
    workflow_goal="""
        1. Verify 'Success' message appears on screen
        2. Take screenshot for records
        3. Switch to Slack
        4. Post confirmation to #submissions channel with screenshot
        5. Return to original window
    """,
    timeout_seconds=60.0
)

await visual_monitor_agent.watch_and_alert(
    app_name="Chrome",
    trigger_text="Form Submitted Successfully",
    action_config=action_config
)
```

---

### Use Case 3: Long-Running Process Monitoring
```python
# Watch Docker build, click notifications when done
action_config = ActionConfig(
    action_type=ActionType.SIMPLE_GOAL,
    goal="Take a screenshot, then click the Desktop notification if present",
    narrate=True
)

await visual_monitor_agent.watch_and_alert(
    app_name="Terminal",
    trigger_text="Successfully built",
    action_config=action_config
)
```

**Perfect for**:
- Docker builds (10-30 min)
- Model training (hours)
- Database migrations (minutes-hours)
- File uploads (varies)

---

## ⚙️ Configuration

### Environment Variables (v11.0 additions)
```bash
# Action execution
export JARVIS_VMSI_ACTION_EXECUTION=true     # Enable Watch & Act
export JARVIS_VMSI_COMPUTER_USE=true         # Use Computer Use
export JARVIS_VMSI_AGENTIC_RUNNER=true       # Use AgenticTaskRunner
export JARVIS_VMSI_ACTION_TIMEOUT=60         # Default action timeout
export JARVIS_VMSI_REQUIRE_CONFIRMATION=false # Auto-execute without asking
export JARVIS_VMSI_AUTO_SWITCH_WINDOW=true   # Auto-switch before acting
```

### VisualMonitorConfig (v11.0)
```python
config = VisualMonitorConfig(
    # Visual monitoring (v10.6)
    default_fps=5,
    default_timeout=300.0,
    max_parallel_watchers=3,
    enable_voice_alerts=True,
    enable_notifications=True,
    enable_cross_repo_sync=True,

    # Action execution (v11.0 NEW!)
    enable_action_execution=True,
    enable_computer_use=True,
    enable_agentic_runner=True,
    action_timeout_seconds=60.0,
    require_confirmation=False,
    auto_switch_to_window=True
)

agent = VisualMonitorAgent(config=config)
```

---

## 🔄 The Complete Autonomous Loop

```
┌─────────────────────────────────────────────────────────────────┐
│                      Watch & Act Loop                            │
└─────────────────────────────────────────────────────────────────┘

You: "Watch Terminal for 'Build Complete', then click Deploy"
                             ↓
        ┌────────────────────────────────────┐
        │  VisualMonitorAgent.watch_and_alert│
        │  - app_name="Terminal"              │
        │  - trigger_text="Build Complete"    │
        │  - action_config=ActionConfig(...)  │
        └────────────────────────────────────┘
                             ↓
        ┌────────────────────────────────────┐
        │  Step 1: Find Window                │
        │  SpatialAwarenessAgent.find_window()│
        │  → Terminal: Window 992, Space 4    │
        └────────────────────────────────────┘
                             ↓
        ┌────────────────────────────────────┐
        │  Step 2: Spawn Watcher              │
        │  VideoWatcher (5 FPS, low-priority) │
        │  → Monitoring Window 992            │
        └────────────────────────────────────┘
                             ↓
        ┌────────────────────────────────────┐
        │  Step 3: Wait for Visual Event      │
        │  OCR scanning frames...             │
        │  [2 minutes later]                  │
        │  ✅ "Build Complete" detected!      │
        └────────────────────────────────────┘
                             ↓
        ┌────────────────────────────────────┐
        │  Step 4: EXECUTE ACTION! 🚀         │
        │  _execute_response()                │
        └────────────────────────────────────┘
                             ↓
        ┌────────────────────────────────────┐
        │  Step 4a: Switch to Window          │
        │  SpatialAwarenessAgent.switch()     │
        │  → Switched to Terminal, Space 4    │
        └────────────────────────────────────┘
                             ↓
        ┌────────────────────────────────────┐
        │  Step 4b: Execute Goal              │
        │  ComputerUse.execute_task()         │
        │  Goal: "Click the Deploy button"    │
        └────────────────────────────────────┘
                             ↓
        ┌────────────────────────────────────┐
        │  Computer Use in Action:            │
        │  1. Screenshot current screen       │
        │  2. Analyze screen with vision      │
        │  3. Locate Deploy button            │
        │  4. Move mouse to button            │
        │  5. Click!                          │
        │  6. Verify action completed         │
        └────────────────────────────────────┘
                             ↓
        ┌────────────────────────────────────┐
        │  Step 5: Voice Narration            │
        │  🔊 "Build complete detected.       │
        │      Clicked Deploy button.         │
        │      Deployment initiated."         │
        └────────────────────────────────────┘
                             ↓
        ┌────────────────────────────────────┐
        │  Step 6: Store in Knowledge Graph   │
        │  Observation:                       │
        │  - Event: Build Complete detected   │
        │  - Action: Deploy clicked           │
        │  - Result: Success                  │
        │  - Duration: 1.2 seconds            │
        └────────────────────────────────────┘
                             ↓
                    MISSION COMPLETE! ✅

You stayed focused on your work the ENTIRE time.
JARVIS handled everything autonomously.
```

---

## 🎓 Advanced Examples

### Multi-Condition Deployment Pipeline
```python
# Complex conditional workflow
deployment_conditions = [
    ConditionalAction(
        trigger_pattern=r"All tests passed.*\((\d+)/\1\)",
        action_goal="Click Deploy to Production button",
        description="Deploy if all tests pass",
        use_regex=True
    ),
    ConditionalAction(
        trigger_pattern=r"Tests passed.*Failed: [1-5]",
        action_goal="Click Deploy to Staging button",
        description="Deploy to staging if only minor failures",
        use_regex=True
    ),
    ConditionalAction(
        trigger_pattern=r"Tests passed.*Failed: ([6-9]|[1-9]\d+)",
        action_goal="Click View Failed Tests, then screenshot",
        description="Review failures if significant",
        use_regex=True
    )
]

action_config = ActionConfig(
    action_type=ActionType.CONDITIONAL,
    conditions=deployment_conditions,
    default_action="Send Slack alert to #dev-ops that tests failed catastrophically",
    narrate=True,
    timeout_seconds=45.0
)
```

### Chained Cross-App Workflow
```python
# Watch for event, then trigger multi-app workflow
action_config = ActionConfig(
    action_type=ActionType.WORKFLOW,
    workflow_goal="""
        Background: Application was just submitted via Chrome.

        Tasks:
        1. Switch to Mail.app
        2. Wait up to 30 seconds for confirmation email
        3. If email received, forward to team@company.com
        4. Switch to Slack
        5. Post in #submissions: "Application submitted and confirmation forwarded"
        6. Switch to Notion
        7. Create new entry in Applications database with timestamp
        8. Return to Chrome
    """,
    workflow_context={
        "apps_to_use": ["Mail", "Slack", "Notion", "Chrome"],
        "confirmation_email_from": "noreply@applicationsite.com"
    },
    timeout_seconds=120.0,
    narrate=True
)

await visual_monitor_agent.watch_and_alert(
    app_name="Chrome",
    trigger_text="Application Submitted Successfully",
    action_config=action_config
)
```

**This is a 4-app autonomous workflow!**

---

## 📊 Statistics & Monitoring

The agent tracks detailed statistics:

```python
stats = {
    "total_watches_started": 47,
    "total_events_detected": 23,
    "total_alerts_sent": 23,
    "total_actions_executed": 21,      # NEW v11.0
    "total_actions_succeeded": 19,     # NEW v11.0
    "total_actions_failed": 2,         # NEW v11.0
    "active_watchers": 3
}
```

Access via Knowledge Graph:
```python
observations = await knowledge_graph.query(
    knowledge_type=KnowledgeType.OBSERVATION,
    filters={"type": "visual_event_detected"}
)

# Each observation now includes action_executed data (v11.0)
for obs in observations:
    print(f"Event: {obs['trigger_text']}")
    print(f"Action: {obs['action_executed']['goal']}")
    print(f"Success: {obs['action_executed']['success']}")
    print(f"Duration: {obs['action_executed']['duration_ms']}ms")
```

---

## 🎯 Best Practices

### 1. Start Simple, Scale Complex
```python
# Start: Simple goal
action_config = ActionConfig(
    action_type=ActionType.SIMPLE_GOAL,
    goal="Click Deploy"
)

# Then: Add conditions
action_config = ActionConfig(
    action_type=ActionType.CONDITIONAL,
    conditions=[...]
)

# Finally: Full workflows
action_config = ActionConfig(
    action_type=ActionType.WORKFLOW,
    workflow_goal="Complex multi-step..."
)
```

### 2. Use Narration for Transparency
```python
action_config = ActionConfig(
    ...,
    narrate=True  # Always enable for monitoring what JARVIS does
)
```

### 3. Set Appropriate Timeouts
```python
# Simple click: 30s default is fine
# Complex workflow: Increase timeout
action_config = ActionConfig(
    action_type=ActionType.WORKFLOW,
    workflow_goal="...",
    timeout_seconds=180.0  # 3 minutes for complex flow
)
```

### 4. Use Confidence Thresholds
```python
conditions = [
    ConditionalAction(
        trigger_pattern="Error",
        action_goal="Retry",
        confidence_threshold=0.9  # High confidence for critical actions
    )
]
```

---

## 🔐 Safety Considerations

### Require Confirmation for Destructive Actions
```python
action_config = ActionConfig(
    action_type=ActionType.SIMPLE_GOAL,
    goal="Click Delete All button",
    require_confirmation=True  # User must approve before executing
)
```

### Use Conditional Logic for Safety
```python
# Only proceed if BOTH conditions met
conditions = [
    ConditionalAction(
        trigger_pattern="Backup Complete.*Success",
        action_goal="Click Delete Old Data button",
        use_regex=True,
        confidence_threshold=0.95  # Very high confidence
    )
]

action_config = ActionConfig(
    action_type=ActionType.CONDITIONAL,
    conditions=conditions,
    default_action="Do nothing - backup not confirmed"  # Safe default
)
```

---

## 🎉 Summary

**VMSI v11.0 "Watch & Act"** completes the autonomous loop:

**Before (v10.6)**:
- ❌ JARVIS watches, you act
- ❌ Interrupts your workflow
- ❌ Manual context switching

**After (v11.0)**:
- ✅ JARVIS watches AND acts
- ✅ Zero interruptions
- ✅ Complete automation
- ✅ True autonomous operation

**You can now say**:
- "Watch Terminal for Build Complete, then click Deploy"
- "If Error click Retry, if Success click Deploy"
- "Watch Chrome for Submitted, verify email, notify Slack"

**And JARVIS will do it ALL automatically!**

The autonomous loop is complete. JARVIS has eyes, proprioception, and now... hands. 🚀

---

## 📚 Related Documentation

- `VMSI_ARCHITECTURE_v10.6.md` - Core architecture
- `VMSI_IMPLEMENTATION_STATUS_v10.6.md` - v10.6 implementation
- `backend/neural_mesh/agents/visual_monitor_agent.py` - Full source code
- `backend/display/computer_use_connector.py` - Computer Use integration
- `backend/neural_mesh/agents/spatial_awareness_agent.py` - Window switching

---

**Version**: 11.0
**Status**: Production Ready
**Autonomous Loop**: ✅ COMPLETE
**Author**: JARVIS AI System
**Date**: 2025-12-28
