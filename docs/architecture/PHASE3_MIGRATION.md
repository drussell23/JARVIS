# Phase 3: Agent Consolidation -- Migration Status

**Date**: 2026-03-24
**Status**: In Progress -- Facade deployed, first vertical pending activation

## Architecture

```
User Command
  |
  v
RuntimeTaskOrchestrator._resolve_step()
  |
  +-- Check 0: Core Context Facade (NEW)
  |     |
  |     +-- JARVIS_CTX_EXECUTOR=true?   --> Executor context (vision, clicks, typing)
  |     +-- JARVIS_CTX_COMMUNICATOR=true? --> Communicator context (email, calendar)
  |     +-- JARVIS_CTX_DEVELOPER=true?    --> Developer context (code, errors)
  |     +-- JARVIS_CTX_OBSERVER=true?     --> Observer context (monitoring)
  |     |
  |     +-- Flag off or error? --> return None (fall through)
  |
  +-- Check 1: AgentRegistry (LEGACY) -- existing 39 agents
  +-- Check 1.5: Vision Action detection (LEGACY)
  +-- Check 2: Ouroboros governance
  +-- Check 3: Ephemeral tool synthesis
```

## Feature Flags

| Flag | Default | Context | Status |
|------|---------|---------|--------|
| `JARVIS_CTX_EXECUTOR` | false | Executor (vision/UI) | Ready to test |
| `JARVIS_CTX_COMMUNICATOR` | false | Communicator (email/calendar) | Ready to test |
| `JARVIS_CTX_DEVELOPER` | false | Developer (code/errors) | Ready to test |
| `JARVIS_CTX_OBSERVER` | false | Observer (monitoring) | Ready to test |

To enable a vertical, add to `backend/.env`:
```
JARVIS_CTX_EXECUTOR=true
```

## Entrypoints Touched

| File | Line | Change |
|------|------|--------|
| `backend/core/runtime_task_orchestrator.py` | ~290 | Added Check 0: facade dispatch before legacy agent check |
| `backend/core_contexts/facade.py` | (new) | Dispatch facade with per-vertical flags |

## Legacy Agent Import Report (28 direct imports)

Excluding venv, tests, and core_contexts:

| Import Site | Agent Imported | Needed Until |
|------------|---------------|-------------|
| agi_os_coordinator.py:1657 | agent_initializer | Phase 3 complete |
| unified_command_processor.py:4596 | GoogleWorkspaceAgent | JARVIS_CTX_COMMUNICATOR=true |
| email_triage/dependencies.py:107 | GoogleWorkspaceAgent | JARVIS_CTX_COMMUNICATOR=true |
| agentic_task_runner.py:413 | AutonomousAgent | JARVIS_CTX_DEVELOPER=true |
| agentic_task_runner.py:5007 | SpatialAwarenessAgent | JARVIS_CTX_EXECUTOR=true |
| agentic_task_runner.py:5178 | PredictivePlanningAgent | JARVIS_CTX_EXECUTOR=true |
| reasoning_activation_gate.py:380 | agent_initializer | Phase 3 complete |
| reasoning_chain_orchestrator.py:441 | PredictivePlanningAgent | JARVIS_CTX_EXECUTOR=true |
| reasoning_chain_orchestrator.py:459 | agent_initializer | Phase 3 complete |
| workspace_routing_intelligence.py:471 | SpatialAwarenessAgent | JARVIS_CTX_EXECUTOR=true |
| main.py:6760 | GoogleWorkspaceAgent | JARVIS_CTX_COMMUNICATOR=true |
| autonomy_adapter.py:14,154,1094 | AutonomousAgent | JARVIS_CTX_DEVELOPER=true |
| voice_adapter.py:14,153,970 | VoiceMemoryAgent | JARVIS_CTX_OBSERVER=true |
| agent_initializer.py:34 | ComputerUseAgent | JARVIS_CTX_EXECUTOR=true |
| native_app_control_agent.py:218 | AppInventoryService | Internal (keep) |
| visual_browser_agent.py:530 | AccessibilityResolver | Internal (keep) |
| visual_monitor_agent.py:194,1401 | AdaptiveResourceGovernor, SpatialAwarenessAgent | Internal (keep) |
| cross_repo_startup_orchestrator.py:25049,25060 | GoogleWorkspaceAgent | JARVIS_CTX_COMMUNICATOR=true |
| trinity_handlers.py:58,62 | VisualMonitorAgent | JARVIS_CTX_OBSERVER=true |

## Migration Order (Recommended)

1. **Executor** (JARVIS_CTX_EXECUTOR=true) -- lowest risk, lean loop proven
2. **Communicator** (JARVIS_CTX_COMMUNICATOR=true) -- GoogleWorkspaceAgent has most imports
3. **Observer** (JARVIS_CTX_OBSERVER=true) -- VisualMonitorAgent + VoiceMemoryAgent
4. **Developer** (JARVIS_CTX_DEVELOPER=true) -- AutonomousAgent + error analysis

After each vertical is confirmed working, update import sites to use
Core Context tools directly, then the legacy agent can be moved to
`backend/legacy_agents/`.

## What NOT To Do Yet

- Do NOT delete any legacy agent files
- Do NOT move files to legacy_agents/ without updating all imports
- Do NOT enable all flags at once -- one vertical at a time
- Do NOT remove agent_initializer.py or the Neural Mesh registry
