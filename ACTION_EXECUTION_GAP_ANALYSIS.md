# JARVIS Action Execution Gap Analysis

**Problem:** JARVIS can see and understand, but can't act intelligently on complex tasks.

**Your Example:**
> "Write an essay on AGI in one window and fix the error in my VS Code project using Yabai and Core Graphics"

---

## Current State: What JARVIS Has

### ✅ Layer 1: Vision & Understanding (WORKS)
- **Claude Vision** - Can see your screen
- **YOLO** - Can detect UI elements
- **VSMS** - Tracks UI state
- **UAE/SAI** - Understands context and intent
- **Voice Pipeline** - Hears your commands

### ✅ Layer 4: Low-Level Actions (WORKS)
You already have these execution tools:
- **Yabai** - Window/space management (`backend/autonomy/macos_integration.py`)
- **AppleScript** - Application control (`backend/context_intelligence/executors/action_executor.py`)
- **Core Graphics API** - Mouse/keyboard control
- **Shell Commands** - File operations, builds, tests

---

## The Problem: Missing Middle Layers

### ❌ Layer 2: Intelligence & Coordination (MISSING - from PRD)
These are in the PRD but not implemented yet:
1. **Multi-Agent Orchestrator** - Breaks "write essay + fix error" into subtasks
2. **Communication Bus** - Lets agents coordinate ("Window Agent, switch to VS Code when Essay Agent is done")
3. **Goal Inference System** - Understands you want 2 parallel tasks
4. **Agent Registry** - Discovers which agents can do what

**Why you need this:** Without orchestration, JARVIS can't decompose "write essay AND fix error" into coordinated steps.

### ❌ Layer 3: High-Level Action Agents (MISSING - NOT in PRD!)
These translate high-level intents into low-level action sequences:

#### Missing Agents for Your Example:

**For "Write essay on AGI":**
- ✗ **Content Generation Agent** - Generates essay content using Claude API
- ✗ **Text Editor Agent** - Opens TextEdit/Notes, positions window, handles text input
- ✗ **Typing Agent** - Streams generated text character-by-character via Core Graphics

**For "Fix error in VS Code":**
- ✗ **Code Analysis Agent** - Reads error message from Vision, understands the bug
- ✗ **Code Solution Agent** - Generates fix using Claude Code or local reasoning
- ✗ **IDE Controller Agent** - Navigates VS Code (find file, go to line, select code)
- ✗ **Code Editor Agent** - Applies fix (delete old code, type new code, save file)

**For "Using Yabai and Core Graphics":**
- ✗ **Window Management Agent** - Orchestrates Yabai commands (create space, move window, focus window)
- ✗ **Multi-Window Coordinator** - Manages parallel windows (essay in window 1, VS Code in window 2)

---

## What Each Layer Does

```
┌─────────────────────────────────────────────────────────────────┐
│ USER COMMAND                                                     │
│ "Write essay on AGI in one window and fix error in VS Code"    │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│ ✅ LAYER 1: VISION & UNDERSTANDING (EXISTS)                     │
│ - UAE parses intent: [write_essay, fix_error]                  │
│ - SAI provides context: current_space=3, vscode_visible=yes    │
│ - Vision reads error: "undefined variable 'foo' on line 42"    │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│ ❌ LAYER 2: INTELLIGENCE & COORDINATION (MISSING - IN PRD)     │
│                                                                  │
│ Goal Inference System:                                          │
│   - Detects 2 parallel tasks                                   │
│   - Predicts you want split-screen or separate spaces         │
│                                                                  │
│ Multi-Agent Orchestrator:                                       │
│   - Task 1: write_essay → assign to Essay Writer Agent        │
│   - Task 2: fix_error → assign to Code Fixer Agent            │
│   - Coordinate: Window Management Agent handles layout         │
│                                                                  │
│ Communication Bus:                                              │
│   - Window Agent: "Creating space 4 for essay"                │
│   - Essay Agent: "Essay complete, saved to ~/Documents"        │
│   - Code Agent: "Error fixed in line 42, tests passing"       │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│ ❌ LAYER 3: HIGH-LEVEL ACTION AGENTS (MISSING - NOT IN PRD!)   │
│                                                                  │
│ Essay Writer Agent:                                             │
│   1. Generate essay content via Claude API                     │
│   2. Request Text Editor Agent to open window                  │
│   3. Stream content to Typing Agent                            │
│                                                                  │
│ Code Fixer Agent:                                               │
│   1. Query Code Analysis Agent for error diagnosis             │
│   2. Generate fix: "Change line 42 to: foo = get_foo()"       │
│   3. Request IDE Controller to navigate to line 42            │
│   4. Request Code Editor Agent to apply fix                    │
│                                                                  │
│ Window Management Agent:                                        │
│   1. Create new space (space 4)                                │
│   2. Open TextEdit in space 4                                  │
│   3. Focus VS Code in current space (space 3)                  │
│   4. Coordinate parallel execution                             │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│ ✅ LAYER 4: LOW-LEVEL ACTIONS (EXISTS)                          │
│                                                                  │
│ Yabai (via action_executor.py):                                │
│   - yabai -m space --create                                    │
│   - yabai -m space --focus 4                                   │
│   - yabai -m window --space 4                                  │
│                                                                  │
│ AppleScript (via action_executor.py):                          │
│   - tell application "TextEdit" to activate                    │
│   - tell application "Visual Studio Code" to activate          │
│                                                                  │
│ Core Graphics (via macos_integration.py):                      │
│   - Move mouse to (x, y)                                       │
│   - Type character 'A', 'G', 'I', ' ', 'e', 's', 's', 'a'...  │
│   - Press key combination Cmd+S                                │
│                                                                  │
│ Shell:                                                          │
│   - cd ~/project && npm test (to verify fix)                  │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│ RESULT                                                           │
│ ✓ Essay written in TextEdit on space 4                         │
│ ✓ Error fixed in VS Code on space 3                            │
│ ✓ Tests passing                                                 │
└─────────────────────────────────────────────────────────────────┘
```

---

## What You Need to Add

### Priority 1: Layer 2 Infrastructure (FROM PRD)
**Status:** Specified in `PRD_JARVIS_AUTONOMOUS_NEURAL_MESH.md`, not implemented

**Required Components:**
1. `backend/core/agent_communication_bus.py` - Pub/sub messaging
2. `backend/core/multi_agent_orchestrator.py` - Task decomposition & coordination
3. `backend/core/agent_registry.py` - Agent discovery
4. `backend/core/shared_knowledge_graph.py` - Shared memory
5. `backend/intelligence/goal_inference_system.py` - Intent prediction

**Install:**
```bash
pip install chromadb networkx sentence-transformers transformers torch
```

---

### Priority 2: Layer 3 High-Level Action Agents (NEW - NOT IN PRD!)
**Status:** Not specified anywhere, needs to be added

**Required Agents:**

#### A. Content & Text Agents
```
backend/agents/
├── content_generation_agent.py      # Generates essays, documents, messages
├── text_editor_agent.py             # Controls TextEdit, Notes, Word
├── typing_agent.py                  # Streams text via Core Graphics
└── document_formatter_agent.py      # Applies formatting (bold, headers, etc.)
```

#### B. Code & Development Agents
```
backend/agents/
├── code_analysis_agent.py           # Analyzes errors, suggests fixes
├── code_solution_agent.py           # Generates code fixes
├── ide_controller_agent.py          # Navigates VS Code, PyCharm, etc.
├── code_editor_agent.py             # Applies code changes
├── test_runner_agent.py             # Runs tests, interprets results
└── git_agent.py                     # Commits, pushes, creates PRs
```

#### C. UI & Window Agents
```
backend/agents/
├── window_management_agent.py       # Orchestrates Yabai commands
├── multi_window_coordinator_agent.py # Manages parallel windows
├── app_launcher_agent.py            # Opens/closes apps
└── ui_navigation_agent.py           # Navigates UI elements via Vision + CG
```

#### D. File & System Agents
```
backend/agents/
├── file_manager_agent.py            # Create, read, move, delete files
├── browser_control_agent.py         # Controls Chrome, Safari
└── system_monitor_agent.py          # Monitors system resources
```

---

## Example: How "Write Essay + Fix Error" Would Work

### Step-by-Step Execution:

```python
# 1. USER COMMAND (via voice)
user_command = "Write me an essay on AGI in one window and fix the error in my VS Code project using Yabai"

# 2. LAYER 1: Understanding (EXISTS)
uae_output = {
    "intents": ["write_essay", "fix_error"],
    "context": {
        "topics": ["AGI"],
        "tools": ["yabai"],
        "parallel": True
    }
}
sai_context = {
    "current_space": 3,
    "vscode_visible": True,
    "error_detected": "line 42: undefined variable 'foo'"
}

# 3. LAYER 2: Coordination (MISSING - FROM PRD)
## Goal Inference System
goal_inference.predict_workflow(uae_output, sai_context)
# Output: "User wants 2 parallel tasks in separate windows"

## Multi-Agent Orchestrator
orchestrator.decompose_task({
    "task": "write_essay_and_fix_error",
    "subtasks": [
        {
            "id": "task_1",
            "type": "content_generation",
            "agent": "Essay_Writer_Agent",
            "params": {"topic": "AGI", "length": "500 words"},
            "priority": "normal"
        },
        {
            "id": "task_2",
            "type": "code_fix",
            "agent": "Code_Fixer_Agent",
            "params": {"file": "current", "error": "line 42"},
            "priority": "high"
        },
        {
            "id": "task_3",
            "type": "window_management",
            "agent": "Window_Management_Agent",
            "params": {"layout": "split", "spaces": [3, 4]},
            "priority": "high",
            "depends_on": []  # Runs first
        }
    ]
})

## Communication Bus
communication_bus.publish({
    "to": "Window_Management_Agent",
    "type": "TASK_ASSIGNED",
    "payload": {...}
})
communication_bus.publish({
    "to": "Essay_Writer_Agent",
    "type": "TASK_ASSIGNED",
    "payload": {...}
})
communication_bus.publish({
    "to": "Code_Fixer_Agent",
    "type": "TASK_ASSIGNED",
    "payload": {...}
})

# 4. LAYER 3: High-Level Actions (MISSING - NOT IN PRD)
## Task 3: Window Management Agent (runs first)
class WindowManagementAgent(BaseAgent):
    async def execute_task(self, task):
        # Create new space for essay
        await self.publish_to_action_executor({
            "action": "yabai",
            "command": "yabai -m space --create"
        })
        
        # Focus new space (space 4)
        await self.publish_to_action_executor({
            "action": "yabai",
            "command": "yabai -m space --focus 4"
        })
        
        # Open TextEdit in space 4
        await self.publish_to_action_executor({
            "action": "applescript",
            "command": 'tell application "TextEdit" to activate'
        })
        
        # Notify orchestrator: ready
        await self.publish("Multi_Agent_Orchestrator", "TASK_COMPLETED", {
            "task_id": "task_3",
            "result": "Space 4 ready for essay"
        })

## Task 1: Essay Writer Agent (runs in parallel)
class EssayWriterAgent(BaseAgent):
    async def execute_task(self, task):
        # Generate essay via Claude API
        essay_content = await self.generate_essay(topic="AGI", length=500)
        
        # Wait for window to be ready
        await self.wait_for_message("Window_Management_Agent", "TASK_COMPLETED")
        
        # Type essay via Typing Agent
        await self.publish("Typing_Agent", "TYPE_TEXT", {
            "text": essay_content,
            "target_app": "TextEdit",
            "speed": "fast"
        })
        
        # Save document
        await self.publish_to_action_executor({
            "action": "applescript",
            "command": 'tell application "System Events" to keystroke "s" using command down'
        })
        
        # Notify completion
        await self.publish("Multi_Agent_Orchestrator", "TASK_COMPLETED", {
            "task_id": "task_1",
            "result": f"Essay written ({len(essay_content)} chars)"
        })

## Task 2: Code Fixer Agent (runs in parallel)
class CodeFixerAgent(BaseAgent):
    async def execute_task(self, task):
        # Query Code Analysis Agent
        error_analysis = await self.query_agent("Code_Analysis_Agent", {
            "error": "line 42: undefined variable 'foo'"
        })
        # Response: "Variable 'foo' not defined. Need to call get_foo() first."
        
        # Generate fix
        fix_code = await self.generate_fix(error_analysis)
        # Output: "foo = get_foo()"
        
        # Focus VS Code (space 3)
        await self.publish_to_action_executor({
            "action": "yabai",
            "command": "yabai -m space --focus 3"
        })
        await asyncio.sleep(0.5)  # Wait for space transition
        
        # Navigate to line 42 via IDE Controller
        await self.publish("IDE_Controller_Agent", "GOTO_LINE", {
            "ide": "vscode",
            "line": 42
        })
        
        # Apply fix via Code Editor Agent
        await self.publish("Code_Editor_Agent", "REPLACE_LINE", {
            "line": 42,
            "old_code": "",  # detected by vision
            "new_code": "foo = get_foo()"
        })
        
        # Run tests
        test_result = await self.publish_to_action_executor({
            "action": "shell",
            "command": "npm test",
            "cwd": "/path/to/project"
        })
        
        # Notify completion
        await self.publish("Multi_Agent_Orchestrator", "TASK_COMPLETED", {
            "task_id": "task_2",
            "result": f"Fix applied. Tests: {test_result['status']}"
        })

# 5. LAYER 4: Low-Level Actions (EXISTS)
## ActionExecutor receives commands from agents
action_executor.execute({
    "action": "yabai",
    "command": "yabai -m space --create"
})
# Calls: subprocess.run(["yabai", "-m", "space", "--create"])

action_executor.execute({
    "action": "applescript",
    "command": 'tell application "TextEdit" to activate'
})
# Calls: subprocess.run(["osascript", "-e", "tell application..."])

typing_agent.type_text("Artificial General Intelligence (AGI)...")
# Calls: Core Graphics API to type each character
```

---

## Summary: What's Missing

| Layer | Component | Status | Location |
|-------|-----------|--------|----------|
| 1 | Vision (YOLO, Claude Vision) | ✅ Exists | `backend/vision/` |
| 1 | Understanding (UAE, SAI) | ✅ Exists | `backend/intelligence/` |
| **2** | **Multi-Agent Orchestrator** | ❌ **Missing** | **PRD only** |
| **2** | **Communication Bus** | ❌ **Missing** | **PRD only** |
| **2** | **Goal Inference System** | ❌ **Missing** | **PRD only** |
| **2** | **Agent Registry** | ❌ **Missing** | **PRD only** |
| **3** | **Essay Writer Agent** | ❌ **Missing** | **Not in PRD** |
| **3** | **Code Fixer Agent** | ❌ **Missing** | **Not in PRD** |
| **3** | **Window Management Agent** | ❌ **Missing** | **Not in PRD** |
| **3** | **IDE Controller Agent** | ❌ **Missing** | **Not in PRD** |
| **3** | **Typing Agent** | ❌ **Missing** | **Not in PRD** |
| 4 | Yabai Executor | ✅ Exists | `backend/context_intelligence/executors/action_executor.py` |
| 4 | AppleScript Executor | ✅ Exists | `backend/autonomy/macos_integration.py` |
| 4 | Core Graphics API | ✅ Exists | `backend/autonomy/hardware_control.py` |

---

## Action Items

### To enable your example, you need to implement:

**1. Layer 2 (from PRD) - First priority:**
- [ ] `backend/core/agent_communication_bus.py`
- [ ] `backend/core/multi_agent_orchestrator.py`
- [ ] `backend/core/agent_registry.py`
- [ ] `backend/intelligence/goal_inference_system.py`

**2. Layer 3 (NEW) - Second priority:**
- [ ] `backend/agents/essay_writer_agent.py`
- [ ] `backend/agents/code_fixer_agent.py`
- [ ] `backend/agents/code_analysis_agent.py`
- [ ] `backend/agents/window_management_agent.py`
- [ ] `backend/agents/ide_controller_agent.py`
- [ ] `backend/agents/typing_agent.py`
- [ ] `backend/agents/text_editor_agent.py`

**3. Integration:**
- [ ] Connect all agents to Communication Bus
- [ ] Register agents with Agent Registry
- [ ] Configure Orchestrator routing rules

---

## Time Estimate

- **Layer 2 (Infrastructure):** 4-6 weeks (as per PRD Phase 1)
- **Layer 3 (High-Level Agents):** 3-4 weeks (7 agents × 3-4 days each)
- **Total:** ~8-10 weeks for full autonomous action capability

---

**Bottom Line:**

Your example requires **BOTH**:
1. ✅ Low-level tools (Yabai, AppleScript, Core Graphics) - **You have these**
2. ❌ Coordination infrastructure (Orchestrator, Communication Bus) - **In PRD, not implemented**
3. ❌ High-level action agents (Essay Writer, Code Fixer, etc.) - **NOT in PRD, needs to be added**

The PRD I just created covers #2 but NOT #3. Layer 3 is the missing piece that translates "write essay" into actual typing actions.
