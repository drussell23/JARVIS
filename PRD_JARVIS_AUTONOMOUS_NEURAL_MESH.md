# Product Requirements Document: JARVIS Autonomous Neural Mesh System

**Document Version:** 1.0  
**Date:** November 26, 2025  
**Project:** JARVIS Multi-Agent System - Autonomy Infrastructure  
**Status:** Ready for Implementation

---

## Executive Summary

This PRD defines the requirements for implementing the **Neural Mesh Core Infrastructure** that will enable JARVIS to operate autonomously. Currently, JARVIS has 60+ specialized AI agents across three tiers, but lacks the fundamental communication and coordination infrastructure needed for autonomous operation. Many critical "brain" agents (Goal Inference, Activity Recognition, Workflow Pattern, Autonomous Decision) are dormant and disconnected.

**Goal:** Transform JARVIS from a reactive system into an autonomous, proactive AI assistant by implementing core infrastructure and activating intelligent agents.

**Target Timeline:** 3-6 months (Phases 1-2)

---

## Background & Context

### Current State
- **60+ specialized AI agents** organized in 3 tiers (Master Intelligence, Core Domain, Specialized Sub-Agents)
- **Active Components:** UAE (Unified Awareness Engine), SAI (Situational Awareness Intelligence), Claude Vision, VSMS (Visual State Management), Voice Pipeline
- **Dormant "Brain" Agents:** Goal Inference System, Activity Recognition Engine, Workflow Pattern Engine, Predictive Precomputation Engine, Autonomous Decision Engine, Autonomous Behaviors Manager
- **Missing Infrastructure:** No inter-agent communication system, no shared memory, no orchestration layer

### Problem Statement
JARVIS agents operate in isolation without:
1. A way to communicate with each other (no messaging bus)
2. Shared memory to store and retrieve learned patterns (no knowledge graph)
3. Coordination mechanism for multi-agent workflows (no orchestrator)
4. Discovery mechanism for agent capabilities (no registry)

### Technology Stack
- **Language:** Python 3.x
- **Framework:** FastAPI
- **Database:** SQLite (local), Cloud SQL (cloud)
- **Voice:** Whisper
- **Vision:** YOLO, Claude Vision API
- **Platform:** macOS (Yabai, Core Graphics API, AppleScript)
- **Cloud:** GCP (project: jarvis-473803)

---

## Goals & Objectives

### Primary Goals
1. **Enable Inter-Agent Communication:** Implement a pub/sub messaging bus for asynchronous agent communication
2. **Create Shared Memory:** Build a knowledge graph for persistent, queryable agent memory
3. **Coordinate Multi-Agent Tasks:** Implement an orchestrator for task decomposition and agent coordination
4. **Dynamic Agent Discovery:** Build a registry for agent registration and capability discovery
5. **Activate Intelligent Agents:** Wire up dormant brain agents to the neural mesh

### Success Metrics
- ✅ All 60+ agents migrated to BaseAgent standard
- ✅ Communication Bus handles 1000+ messages/sec with <10ms latency
- ✅ Knowledge Graph stores and retrieves patterns with <50ms query time
- ✅ Orchestrator coordinates 5+ agents in parallel workflows
- ✅ Goal Inference System predicts user intent with >70% accuracy
- ✅ Activity Recognition Engine identifies workflows with >80% accuracy
- ✅ System operates autonomously for 4+ hours without user intervention

---

## Functional Requirements

### FR-1: Agent Communication Bus
**Priority:** P0 (Critical)

**Description:** A publish-subscribe messaging system for asynchronous inter-agent communication.

**Requirements:**
- FR-1.1: Support pub/sub pattern with topic-based routing
- FR-1.2: Handle message types: `TASK_ASSIGNED`, `TASK_COMPLETED`, `TASK_FAILED`, `QUERY`, `RESPONSE`, `EVENT`, `CUSTOM`
- FR-1.3: Support message priorities: `CRITICAL`, `HIGH`, `NORMAL`, `LOW`
- FR-1.4: Guarantee at-least-once delivery for CRITICAL/HIGH priority messages
- FR-1.5: Store message history for debugging (last 1000 messages per agent)
- FR-1.6: Support both local (in-process) and distributed (Redis/RabbitMQ) backends
- FR-1.7: Provide async API: `publish()`, `subscribe()`, `unsubscribe()`

**Acceptance Criteria:**
- Agent A can publish a message that Agent B receives within 10ms
- Message bus handles 1000 messages/sec without dropping messages
- Failed deliveries are retried 3 times with exponential backoff
- Message history is queryable via `get_message_history(agent_name, limit)`

---

### FR-2: Shared Knowledge Graph
**Priority:** P0 (Critical)

**Description:** A centralized, persistent memory system for storing and retrieving learned patterns, facts, and solutions.

**Requirements:**
- FR-2.1: Store knowledge types: `workflow_pattern`, `ui_pattern`, `user_preference`, `error_solution`, `optimization`, `automation_rule`
- FR-2.2: Support vector embeddings for semantic search (using sentence-transformers)
- FR-2.3: Support graph relationships for connected knowledge (using networkx)
- FR-2.4: Provide async API: `add_knowledge()`, `query_knowledge()`, `update_knowledge()`, `delete_knowledge()`
- FR-2.5: Return similarity scores for semantic queries
- FR-2.6: Support filtering by knowledge type, timestamp, source agent
- FR-2.7: Persist to disk (ChromaDB for vectors, SQLite for graph)
- FR-2.8: Support hybrid local/cloud storage

**Data Schema:**
```python
KnowledgeEntry {
    id: str,
    type: str,  # workflow_pattern, ui_pattern, etc.
    data: Dict[str, Any],
    embedding: np.ndarray,
    timestamp: datetime,
    source_agent: str,
    confidence: float,
    usage_count: int,
    relationships: List[str]  # IDs of related knowledge
}
```

**Acceptance Criteria:**
- Can store 10,000+ knowledge entries
- Semantic queries return results in <50ms
- Knowledge persists across system restarts
- Agents can discover related knowledge via graph relationships

---

### FR-3: Multi-Agent Orchestrator
**Priority:** P0 (Critical)

**Description:** A central coordinator for decomposing tasks, selecting agents, and managing multi-agent workflows.

**Requirements:**
- FR-3.1: Accept high-level tasks from UAE/SAI
- FR-3.2: Decompose tasks into subtasks based on agent capabilities
- FR-3.3: Select optimal agents for each subtask (via registry)
- FR-3.4: Coordinate parallel agent execution
- FR-3.5: Handle task failures with retry logic and fallback agents
- FR-3.6: Aggregate results from multiple agents
- FR-3.7: Track task execution state: `PENDING`, `RUNNING`, `COMPLETED`, `FAILED`
- FR-3.8: Provide async API: `submit_task()`, `get_task_status()`, `cancel_task()`

**Orchestration Flow:**
```
1. Receive task from UAE/SAI
2. Query registry for capable agents
3. Decompose task into subtasks
4. Assign subtasks to agents (via Communication Bus)
5. Monitor progress and handle failures
6. Aggregate results
7. Return to requesting agent
```

**Acceptance Criteria:**
- Can coordinate 5+ agents in parallel
- Task failures trigger automatic retries (max 3 attempts)
- If primary agent fails, fallback agent is selected automatically
- Task execution time is tracked and optimized

---

### FR-4: Agent Registry
**Priority:** P0 (Critical)

**Description:** A system for dynamic agent registration, health monitoring, and capability discovery.

**Requirements:**
- FR-4.1: Agents register on startup with name, type, capabilities, backend (local/cloud)
- FR-4.2: Track agent state: `INITIALIZING`, `ACTIVE`, `BUSY`, `IDLE`, `ERROR`, `OFFLINE`
- FR-4.3: Monitor agent health via periodic heartbeats (every 30 seconds)
- FR-4.4: Mark agents as `OFFLINE` if heartbeat missed for 60 seconds
- FR-4.5: Support agent capability queries: "Find agents with capability X"
- FR-4.6: Track agent load (0.0 = idle, 1.0 = fully loaded)
- FR-4.7: Provide async API: `register()`, `unregister()`, `update_status()`, `find_agents()`, `get_agent_info()`

**Agent Metadata:**
```python
AgentInfo {
    name: str,
    type: str,  # vision, voice, system, ml, etc.
    capabilities: Set[str],
    backend: str,  # "local" or "cloud"
    state: AgentState,
    load: float,  # 0.0 to 1.0
    last_heartbeat: datetime,
    metadata: Dict[str, Any]
}
```

**Acceptance Criteria:**
- Registry tracks all 60+ JARVIS agents
- Offline agents are detected within 60 seconds
- Capability queries return agents sorted by load (least loaded first)
- Registry persists to disk and restores on restart

---

### FR-5: BaseAgent Standard
**Priority:** P0 (Critical)

**Description:** A base class that all JARVIS agents inherit from, providing standardized lifecycle and communication.

**Requirements:**
- FR-5.1: All agents inherit from `BaseAgent` abstract class
- FR-5.2: Provide lifecycle methods: `initialize()`, `start()`, `stop()`
- FR-5.3: Provide communication methods: `publish()`, `subscribe()`
- FR-5.4: Provide knowledge methods: `query_knowledge()`, `add_knowledge()`
- FR-5.5: Handle heartbeat transmission automatically
- FR-5.6: Abstract method `execute_task()` for task handling
- FR-5.7: Abstract methods `on_initialize()`, `on_start()`, `on_stop()` for custom logic
- FR-5.8: Support both local and cloud backends

**Class Structure:**
```python
class BaseAgent(ABC):
    def __init__(self, agent_name: str, agent_type: str, 
                 capabilities: Set[str], backend: str = "local")
    
    # Lifecycle
    async def initialize(self)
    async def start(self)
    async def stop(self)
    
    # Communication
    async def publish(self, to_agent: str, message_type: MessageType, 
                     payload: Dict[str, Any], priority: MessagePriority)
    async def subscribe(self, message_type: MessageType, handler: Callable)
    
    # Knowledge
    async def query_knowledge(self, query: str, 
                             knowledge_types: List[str], limit: int)
    async def add_knowledge(self, knowledge_type: str, data: Dict[str, Any])
    
    # Abstract methods to implement
    @abstractmethod
    async def execute_task(self, task_payload: Dict[str, Any]) -> Any
    @abstractmethod
    async def on_initialize(self)
    @abstractmethod
    async def on_start(self)
    @abstractmethod
    async def on_stop(self)
```

**Acceptance Criteria:**
- All existing agents migrated to inherit from BaseAgent
- Agents can communicate via publish/subscribe without direct coupling
- Agents can query and add knowledge without direct database access
- Agent lifecycle is managed consistently across the system

---

### FR-6: Goal Inference System
**Priority:** P1 (High)

**Description:** Predict user intent and next actions based on context, history, and patterns.

**Requirements:**
- FR-6.1: Analyze current context (screen, voice, recent actions) via SAI/UAE
- FR-6.2: Query knowledge graph for similar past workflows
- FR-6.3: Use Transformer model for intent classification
- FR-6.4: Predict next 1-3 likely user actions with confidence scores
- FR-6.5: Publish predictions to Predictive Precomputation Engine
- FR-6.6: Learn from user corrections (when prediction is wrong)
- FR-6.7: Support confidence threshold configuration (default: 0.7)

**Inference Pipeline:**
```
1. Receive context from SAI (screen state, voice command, etc.)
2. Query knowledge graph for similar workflows
3. Use Transformer model to classify intent
4. Predict next actions with confidence scores
5. If confidence > threshold, publish to Predictive Precomputation
6. If confidence > high_threshold (0.85), publish to Autonomous Decision
7. Store prediction and outcome for learning
```

**Acceptance Criteria:**
- Predicts user intent with >70% accuracy
- Responds within 200ms
- Learns from corrections and improves over time
- Stores prediction history in knowledge graph

---

### FR-7: Activity Recognition Engine
**Priority:** P1 (High)

**Description:** Detect and classify user activities and workflows in real-time.

**Requirements:**
- FR-7.1: Monitor screen changes, keyboard/mouse events, application switches
- FR-7.2: Classify activities: `coding`, `debugging`, `browsing`, `writing`, `meeting`, `idle`
- FR-7.3: Detect workflow patterns (sequences of activities)
- FR-7.4: Store recognized workflows in knowledge graph
- FR-7.5: Trigger Workflow Pattern Engine when new pattern detected
- FR-7.6: Support custom activity definitions via config

**Activity Recognition:**
```
1. Subscribe to VSMS for screen changes
2. Subscribe to system events for keyboard/mouse/app switches
3. Extract features: active app, window title, focused element, typing speed, etc.
4. Use ML model to classify current activity
5. Track activity sequences to detect workflows
6. Store workflows in knowledge graph
```

**Acceptance Criteria:**
- Identifies user activities with >80% accuracy
- Detects workflow patterns after 2-3 repetitions
- Stores workflow patterns for future automation
- Minimal performance impact (<5% CPU)

---

### FR-8: Workflow Pattern Engine
**Priority:** P1 (High)

**Description:** Learn and automate repetitive user workflows.

**Requirements:**
- FR-8.1: Receive workflow patterns from Activity Recognition Engine
- FR-8.2: Identify repetitive workflows (occurred 3+ times)
- FR-8.3: Create automation rules for repetitive workflows
- FR-8.4: Store automation rules in knowledge graph
- FR-8.5: Publish automation suggestions to UAE (for user confirmation)
- FR-8.6: Execute automated workflows when triggered
- FR-8.7: Support workflow parameterization (e.g., "open file X" where X varies)

**Automation Flow:**
```
1. Receive workflow pattern from Activity Recognition
2. Check if pattern is repetitive (occurred 3+ times)
3. Create automation rule with trigger conditions
4. Ask user for confirmation via UAE
5. If confirmed, store rule and monitor for trigger conditions
6. When triggered, execute workflow via Multi-Agent Orchestrator
7. Track success/failure and adjust rule
```

**Acceptance Criteria:**
- Detects repetitive workflows after 3 occurrences
- Creates automation rules with >90% accuracy
- Executes automated workflows successfully >85% of the time
- Allows user to approve/reject automation suggestions

---

### FR-9: Predictive Precomputation Engine
**Priority:** P2 (Medium)

**Description:** Pre-compute likely next actions for performance optimization.

**Requirements:**
- FR-9.1: Receive predictions from Goal Inference System
- FR-9.2: Pre-load resources (files, data, models) for predicted actions
- FR-9.3: Warm up agents likely to be needed
- FR-9.4: Cache computation results for predicted queries
- FR-9.5: Track hit rate (how often predictions are correct)
- FR-9.6: Discard pre-computed results after timeout (default: 5 minutes)

**Precomputation Strategy:**
```
1. Receive prediction from Goal Inference (e.g., "User likely to open file X")
2. Pre-load file X into memory
3. Warm up relevant agents (e.g., code analysis agent)
4. When user actually opens file X, serve from cache (instant)
5. Track prediction accuracy
```

**Acceptance Criteria:**
- Pre-computation hit rate >60%
- Reduces action latency by 50%+ for predicted actions
- Memory usage stays under 500MB for pre-computed data
- Automatically adjusts strategy based on hit rate

---

### FR-10: Autonomous Decision Engine
**Priority:** P2 (Medium)

**Description:** Make autonomous decisions and execute actions without user input.

**Requirements:**
- FR-10.1: Receive high-confidence predictions from Goal Inference (confidence >0.85)
- FR-10.2: Evaluate if action is safe to execute autonomously
- FR-10.3: Execute action via Multi-Agent Orchestrator
- FR-10.4: Log all autonomous actions for audit trail
- FR-10.5: Support undo mechanism for autonomous actions
- FR-10.6: Respect user-defined autonomy level: `OFF`, `LOW`, `MEDIUM`, `HIGH`
- FR-10.7: Never execute destructive actions (delete, overwrite) without confirmation

**Safety Rules:**
```python
SAFE_AUTONOMOUS_ACTIONS = {
    "LOW": ["open_file", "switch_app", "scroll", "search"],
    "MEDIUM": ["open_file", "switch_app", "scroll", "search", "navigate", "run_test"],
    "HIGH": ["*"],  # All except destructive
}

NEVER_AUTONOMOUS = ["delete_file", "overwrite_file", "commit", "push", "deploy"]
```

**Acceptance Criteria:**
- Only executes actions when confidence >85%
- Respects user-defined autonomy level
- Never executes destructive actions autonomously
- Provides undo for last 10 autonomous actions
- All actions are logged with timestamp, trigger, result

---

### FR-11: Autonomous Behaviors Manager
**Priority:** P2 (Medium)

**Description:** Manage and coordinate autonomous behavior patterns.

**Requirements:**
- FR-11.1: Define behavior patterns: `proactive_assistance`, `error_prevention`, `performance_optimization`
- FR-11.2: Monitor system state and trigger behaviors when conditions met
- FR-11.3: Coordinate multiple behaviors to avoid conflicts
- FR-11.4: Learn behavior effectiveness and adjust trigger conditions
- FR-11.5: Support user-defined custom behaviors via config

**Example Behaviors:**
```yaml
behaviors:
  - name: proactive_error_detection
    trigger: "Vision detects error message on screen"
    actions:
      - Query knowledge graph for solution
      - If solution found, suggest to user
      - If not found, search online and store solution
  
  - name: performance_optimization
    trigger: "System detects slow response time"
    actions:
      - Profile current agents
      - Offload heavy tasks to cloud
      - Cache frequently accessed data
  
  - name: workflow_suggestion
    trigger: "Repetitive pattern detected"
    actions:
      - Create automation rule
      - Ask user for confirmation
```

**Acceptance Criteria:**
- Supports 5+ behavior patterns out of the box
- Behaviors trigger correctly based on conditions
- No behavior conflicts (managed via priority system)
- Users can enable/disable behaviors individually
- Behavior effectiveness is tracked and displayed

---

### FR-12: High-Level Action Agents
**Priority:** P0 (Critical)

**Description:** Agents that translate high-level intents into sequences of low-level actions. Bridges the gap between "understanding" and "execution."

**Context:** JARVIS has low-level tools (Yabai, AppleScript, Core Graphics) but lacks agents that can orchestrate complex multi-step actions like "write an essay" or "fix a code error."

**Requirements:**

#### FR-12.1: Content Generation & Text Agents
- **Essay Writer Agent**: Generate long-form content via Claude API, coordinate with Text Editor Agent
- **Text Editor Agent**: Control TextEdit/Notes/Word, open files, position windows
- **Typing Agent**: Stream text character-by-character via Core Graphics with natural typing speed
- **Document Formatter Agent**: Apply formatting (bold, italic, headers, bullet points)

**Example Flow:**
```python
# User: "Write an essay on AGI"
Essay_Writer_Agent:
  1. Generate content via Claude API (500 words)
  2. Request Window_Management_Agent to open TextEdit
  3. Send content to Typing_Agent with target="TextEdit"
  4. Request Document_Formatter_Agent to apply title formatting
  5. Save document via AppleScript (Cmd+S)
```

#### FR-12.2: Code & Development Agents
- **Code Analysis Agent**: Read error messages from Vision, diagnose bugs, suggest fixes
- **Code Solution Agent**: Generate code fixes using Claude Code or local reasoning
- **IDE Controller Agent**: Navigate VS Code/PyCharm (go to file, go to line, select text)
- **Code Editor Agent**: Apply code changes (delete, insert, replace lines)
- **Test Runner Agent**: Execute tests, parse results, report failures
- **Git Agent**: Commit changes, push, create PRs via `gh` CLI

**Example Flow:**
```python
# User: "Fix the error in VS Code"
Code_Fixer_Agent:
  1. Query Vision for error message: "line 42: undefined variable 'foo'"
  2. Query Code_Analysis_Agent for diagnosis
  3. Generate fix via Code_Solution_Agent: "foo = get_foo()"
  4. Request IDE_Controller_Agent.goto_line(42)
  5. Request Code_Editor_Agent.replace_line(42, "foo = get_foo()")
  6. Request Test_Runner_Agent.run_tests()
  7. Report result to user
```

#### FR-12.3: UI & Window Agents
- **Window Management Agent**: Orchestrate Yabai commands (create space, move window, focus)
- **Multi-Window Coordinator Agent**: Manage parallel windows for multi-task workflows
- **App Launcher Agent**: Open/close/switch applications via AppleScript
- **UI Navigation Agent**: Navigate UI elements using Vision + Core Graphics (click buttons, fill forms)

**Example Flow:**
```python
# User: "Open essay in one window and VS Code in another"
Multi_Window_Coordinator_Agent:
  1. Request Window_Management_Agent.create_space()  # Space 4
  2. Request App_Launcher_Agent.open("TextEdit", space=4)
  3. Request Window_Management_Agent.focus_space(3)
  4. Request App_Launcher_Agent.activate("Visual Studio Code")
```

#### FR-12.4: File & System Agents
- **File Manager Agent**: Create, read, move, delete files safely
- **Browser Control Agent**: Control Chrome/Safari (open URL, navigate, click)
- **System Monitor Agent**: Monitor CPU/memory, trigger optimizations
- **Screenshot Agent**: Capture screenshots for documentation/debugging

**Agent Implementation Template:**
```python
from backend.core.base_agent import BaseAgent, MessageType, MessagePriority

class EssayWriterAgent(BaseAgent):
    def __init__(self):
        super().__init__(
            agent_name="Essay_Writer",
            agent_type="content",
            capabilities={"content_generation", "essay_writing", "long_form_text"},
            backend="local"
        )
        self.claude_api = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    
    async def on_initialize(self):
        # Subscribe to essay writing tasks
        await self.subscribe(MessageType.TASK_ASSIGNED, self._handle_task)
    
    async def execute_task(self, task_payload: Dict[str, Any]) -> Any:
        topic = task_payload.get("topic")
        length = task_payload.get("length", 500)
        
        # 1. Generate content
        essay = await self._generate_essay(topic, length)
        
        # 2. Request window setup
        await self.publish(
            to_agent="Window_Management_Agent",
            message_type=MessageType.TASK_ASSIGNED,
            payload={
                "action": "open_text_editor",
                "app": "TextEdit",
                "space": "new"
            },
            priority=MessagePriority.HIGH
        )
        
        # Wait for window ready
        ready_msg = await self.wait_for_message("Window_Management_Agent", "TASK_COMPLETED")
        
        # 3. Type essay
        await self.publish(
            to_agent="Typing_Agent",
            message_type=MessageType.TASK_ASSIGNED,
            payload={
                "text": essay,
                "target_app": "TextEdit",
                "speed": "fast"
            }
        )
        
        # 4. Save document
        await self.publish(
            to_agent="Action_Executor",
            message_type=MessageType.TASK_ASSIGNED,
            payload={
                "action": "applescript",
                "command": 'tell application "System Events" to keystroke "s" using command down'
            }
        )
        
        # 5. Store knowledge
        await self.add_knowledge(
            knowledge_type="completed_task",
            data={
                "task": "essay_writing",
                "topic": topic,
                "length": len(essay),
                "timestamp": datetime.now()
            }
        )
        
        return {"success": True, "essay_length": len(essay)}
    
    async def _generate_essay(self, topic: str, length: int) -> str:
        response = await asyncio.to_thread(
            self.claude_api.messages.create,
            model="claude-3-5-sonnet-20241022",
            max_tokens=length * 3,  # ~3 tokens per word
            messages=[{
                "role": "user",
                "content": f"Write a {length}-word essay on {topic}. Be concise and informative."
            }]
        )
        return response.content[0].text
```

**Acceptance Criteria:**
- 10+ high-level action agents implemented
- Each agent inherits from BaseAgent
- Agents coordinate via Communication Bus (no direct calls)
- Complex tasks (essay writing, code fixing) work end-to-end
- Agents query Knowledge Graph for learned patterns
- All actions are logged for audit trail

---

### FR-13: Configuration System
**Priority:** P1 (High)

**Description:** Centralized configuration for autonomous features.

**Requirements:**
- FR-12.1: Configuration file: `config/autonomous_settings.yaml`
- FR-12.2: Support hierarchical config (global, agent-specific)
- FR-12.3: Hot-reload on config changes (no restart needed)
- FR-12.4: Validate config on load (fail fast on errors)
- FR-12.5: Provide defaults for all settings

**Configuration Schema:**
```yaml
autonomous:
  enabled: true
  level: "MEDIUM"  # OFF, LOW, MEDIUM, HIGH
  
  goal_inference:
    enabled: true
    confidence_threshold: 0.7
    high_confidence_threshold: 0.85
    model: "facebook/bart-large-mnli"
  
  activity_recognition:
    enabled: true
    activities: ["coding", "debugging", "browsing", "writing", "meeting", "idle"]
    min_pattern_repetitions: 3
  
  workflow_automation:
    enabled: true
    require_user_confirmation: true
    max_automated_workflows: 20
  
  predictive_precomputation:
    enabled: true
    cache_timeout_seconds: 300
    max_cache_size_mb: 500
  
  autonomous_decision:
    enabled: false  # Disabled by default for safety
    allowed_actions: ["open_file", "switch_app", "scroll", "search"]
  
  behaviors:
    proactive_error_detection: true
    performance_optimization: true
    workflow_suggestion: true

communication_bus:
  backend: "local"  # "local" or "redis"
  redis_url: "redis://localhost:6379"
  message_history_size: 1000
  
knowledge_graph:
  backend: "local"  # "local" or "cloud"
  embedding_model: "sentence-transformers/all-MiniLM-L6-v2"
  vector_db_path: "backend/data/chroma"
  graph_db_path: "backend/data/knowledge_graph.db"
  max_entries: 100000
```

**Acceptance Criteria:**
- Configuration loads on system startup
- Config changes are detected and applied within 5 seconds
- Invalid config triggers clear error message
- All settings have sensible defaults

---

## Non-Functional Requirements

### NFR-1: Performance
- Communication Bus: <10ms message latency, 1000+ msg/sec throughput
- Knowledge Graph: <50ms query time for semantic search
- Agent heartbeats: <5ms processing time
- Goal Inference: <200ms prediction time
- Activity Recognition: <5% CPU usage
- Total system overhead: <10% CPU, <1GB RAM

### NFR-2: Reliability
- Communication Bus: At-least-once delivery for CRITICAL/HIGH priority messages
- Agent failures: Automatic recovery and fallback
- Knowledge Graph: Persistent storage with backup/restore
- Uptime: 99.9% availability for core infrastructure

### NFR-3: Scalability
- Support 60+ agents (current) to 100+ agents (future)
- Knowledge Graph: 100,000+ entries
- Communication Bus: 10,000+ messages/hour
- Orchestrator: Coordinate 10+ agents in parallel

### NFR-4: Security
- No destructive actions executed autonomously
- All autonomous actions logged for audit
- User can disable autonomous features at any time
- Sensitive data (passwords, API keys) never logged

### NFR-5: Maintainability
- Code follows Python PEP 8 style guide
- All public methods have docstrings
- Type hints on all function signatures
- Unit tests for core infrastructure (80%+ coverage)
- Integration tests for multi-agent workflows

### NFR-6: Observability
- Structured logging (JSON format)
- Metrics: message count, knowledge queries, agent load, task execution time
- Dashboard for monitoring system health
- Alerts for agent failures, high latency, low accuracy

---

## Technical Architecture

### Component Diagram
```
┌─────────────────────────────────────────────────────────────┐
│                    JARVIS Neural Mesh                        │
├─────────────────────────────────────────────────────────────┤
│                                                               │
│  ┌──────────────────────────────────────────────────────┐  │
│  │  Tier 1: Master Intelligence                          │  │
│  │  - UAE (Unified Awareness Engine)                     │  │
│  │  - SAI (Situational Awareness Intelligence)          │  │
│  └──────────────────────────────────────────────────────┘  │
│                          │                                   │
│                          ▼                                   │
│  ┌──────────────────────────────────────────────────────┐  │
│  │  Core Infrastructure (NEW)                            │  │
│  │  ┌──────────────────┐  ┌───────────────────┐         │  │
│  │  │ Communication    │  │ Knowledge Graph   │         │  │
│  │  │ Bus              │  │ - Vectors (Chroma)│         │  │
│  │  │ - Pub/Sub        │  │ - Graph (NetworkX)│         │  │
│  │  │ - Message Queue  │  │ - Semantic Search │         │  │
│  │  └──────────────────┘  └───────────────────┘         │  │
│  │  ┌──────────────────┐  ┌───────────────────┐         │  │
│  │  │ Multi-Agent      │  │ Agent Registry    │         │  │
│  │  │ Orchestrator     │  │ - Discovery       │         │  │
│  │  │ - Task Decomp    │  │ - Health Monitor  │         │  │
│  │  │ - Coordination   │  │ - Load Balancing  │         │  │
│  │  └──────────────────┘  └───────────────────┘         │  │
│  └──────────────────────────────────────────────────────┘  │
│                          │                                   │
│                          ▼                                   │
│  ┌──────────────────────────────────────────────────────┐  │
│  │  Tier 2: Intelligent Agents (ACTIVATE)               │  │
│  │  - Goal Inference System                              │  │
│  │  - Activity Recognition Engine                        │  │
│  │  - Workflow Pattern Engine                            │  │
│  │  - Predictive Precomputation Engine                   │  │
│  │  - Autonomous Decision Engine                         │  │
│  │  - Autonomous Behaviors Manager                       │  │
│  └──────────────────────────────────────────────────────┘  │
│                          │                                   │
│                          ▼                                   │
│  ┌──────────────────────────────────────────────────────┐  │
│  │  Tier 3: Domain Agents (MIGRATE)                     │  │
│  │  - VSMS Core, Claude Vision, Voice Pipeline          │  │
│  │  - 60+ specialized agents...                          │  │
│  └──────────────────────────────────────────────────────┘  │
│                                                               │
└─────────────────────────────────────────────────────────────┘
```

### File Structure
```
backend/
├── core/
│   ├── base_agent.py                    # NEW: BaseAgent abstract class
│   ├── agent_communication_bus.py       # NEW: Pub/sub messaging system
│   ├── shared_knowledge_graph.py        # NEW: Vector + graph knowledge store
│   ├── multi_agent_orchestrator.py      # NEW: Task coordinator
│   ├── agent_registry.py                # NEW: Agent discovery & health
│   ├── hybrid_router.py                 # EXISTING: Enhance for neural mesh
│   └── cloud_agent_launcher.py          # EXISTING: For cloud agents
│
├── intelligence/                         # NEW: Intelligent agents
│   ├── goal_inference_system.py         # NEW: Predict user intent
│   ├── activity_recognition_engine.py   # NEW: Detect user activities
│   ├── workflow_pattern_engine.py       # NEW: Learn & automate workflows
│   ├── predictive_precomputation.py     # NEW: Pre-compute next actions
│   ├── autonomous_decision_engine.py    # NEW: Make autonomous decisions
│   └── autonomous_behaviors_manager.py  # NEW: Manage behavior patterns
│
├── agents/                               # NEW: High-level action agents
│   ├── content/                          # Content generation agents
│   │   ├── essay_writer_agent.py        # NEW: Generate essays/articles
│   │   ├── text_editor_agent.py         # NEW: Control text editors
│   │   ├── typing_agent.py              # NEW: Type text via Core Graphics
│   │   └── document_formatter_agent.py  # NEW: Apply text formatting
│   │
│   ├── code/                             # Code & development agents
│   │   ├── code_analysis_agent.py       # NEW: Analyze errors
│   │   ├── code_solution_agent.py       # NEW: Generate fixes
│   │   ├── ide_controller_agent.py      # NEW: Navigate IDE
│   │   ├── code_editor_agent.py         # NEW: Apply code changes
│   │   ├── test_runner_agent.py         # NEW: Run tests
│   │   └── git_agent.py                 # NEW: Git operations
│   │
│   ├── ui/                               # UI & window agents
│   │   ├── window_management_agent.py   # NEW: Orchestrate Yabai
│   │   ├── multi_window_coordinator.py  # NEW: Manage parallel windows
│   │   ├── app_launcher_agent.py        # NEW: Open/close apps
│   │   └── ui_navigation_agent.py       # NEW: Navigate UI elements
│   │
│   └── system/                           # File & system agents
│       ├── file_manager_agent.py        # NEW: File operations
│       ├── browser_control_agent.py     # NEW: Control browser
│       ├── system_monitor_agent.py      # NEW: Monitor resources
│       └── screenshot_agent.py          # NEW: Capture screenshots
│
├── ml/
│   └── transformer_manager.py           # NEW: Manage Transformer models
│
├── vision/
│   └── visual_state_management_system.py # EXISTING: Migrate to BaseAgent
│
├── voice/
│   └── voice_pipeline.py                # EXISTING: Migrate to BaseAgent
│
└── data/                                 # NEW: Persistent storage
    ├── chroma/                           # ChromaDB vector store
    └── knowledge_graph.db                # NetworkX graph store

config/
└── autonomous_settings.yaml              # NEW: Configuration file

tests/
├── test_communication_bus.py             # NEW: Unit tests
├── test_knowledge_graph.py               # NEW: Unit tests
├── test_orchestrator.py                  # NEW: Unit tests
├── test_registry.py                      # NEW: Unit tests
├── test_goal_inference.py                # NEW: Unit tests
├── test_high_level_agents.py             # NEW: Test action agents
└── test_integration_multi_agent.py       # NEW: Integration tests
```

### Data Flow: Autonomous Action
```
1. User Activity
   ↓
2. VSMS detects screen change → Publishes to Communication Bus
   ↓
3. SAI receives event → Queries Knowledge Graph for context
   ↓
4. SAI publishes context to Goal Inference System
   ↓
5. Goal Inference:
   - Queries Knowledge Graph for similar past workflows
   - Uses Transformer model to predict intent
   - Predicts next 3 actions with confidence scores
   ↓
6. If confidence > 0.7:
   - Publish to Predictive Precomputation Engine
   - Pre-load resources for predicted actions
   ↓
7. If confidence > 0.85 AND autonomy_level >= MEDIUM:
   - Publish to Autonomous Decision Engine
   - Evaluate if action is safe
   - If safe, execute via Multi-Agent Orchestrator
   ↓
8. Activity Recognition observes action outcome
   - Stores pattern in Knowledge Graph
   - Updates Goal Inference accuracy
```

---

## Implementation Phases

### Phase 1: Core Infrastructure (Weeks 1-4)
**Goal:** Build the Neural Mesh foundation

**Deliverables:**
- [ ] `backend/core/base_agent.py` - BaseAgent abstract class
- [ ] `backend/core/agent_communication_bus.py` - Pub/sub messaging
- [ ] `backend/core/shared_knowledge_graph.py` - Vector + graph knowledge store
- [ ] `backend/core/agent_registry.py` - Agent discovery & health
- [ ] `backend/core/multi_agent_orchestrator.py` - Task coordinator
- [ ] `backend/ml/transformer_manager.py` - Transformer model management
- [ ] `config/autonomous_settings.yaml` - Configuration system
- [ ] Unit tests for all core components (80%+ coverage)

**Success Criteria:**
- All core infrastructure components operational
- Communication Bus handles 1000+ msg/sec with <10ms latency
- Knowledge Graph stores/queries entries in <50ms
- Registry tracks all agents with heartbeat monitoring
- Orchestrator coordinates multi-agent tasks

---

### Phase 2: Agent Migration (Weeks 5-8)
**Goal:** Migrate existing agents to Neural Mesh

**Deliverables:**
- [ ] Migrate VSMS Core to BaseAgent
- [ ] Migrate Claude Vision Analyzer to BaseAgent
- [ ] Migrate Voice Pipeline to BaseAgent
- [ ] Migrate UAE to use Orchestrator for task coordination
- [ ] Migrate SAI to publish events to Communication Bus
- [ ] Migrate all 60+ agents to BaseAgent standard
- [ ] Integration tests for migrated agents

**Success Criteria:**
- All agents inherit from BaseAgent
- Agents communicate via Communication Bus (no direct coupling)
- Agents use Knowledge Graph for shared memory
- Zero regression in existing functionality

---

### Phase 3: Intelligent Agents (Weeks 9-12)
**Goal:** Activate dormant brain agents

**Deliverables:**
- [ ] `backend/intelligence/goal_inference_system.py`
- [ ] `backend/intelligence/activity_recognition_engine.py`
- [ ] `backend/intelligence/workflow_pattern_engine.py`
- [ ] Train/fine-tune Transformer models for intent classification
- [ ] Integration with UAE/SAI for context input
- [ ] Unit and integration tests

**Success Criteria:**
- Goal Inference predicts user intent with >70% accuracy
- Activity Recognition identifies workflows with >80% accuracy
- Workflow patterns are learned after 3 repetitions
- All intelligent agents operational and connected

---

### Phase 4: High-Level Action Agents (Weeks 13-16)
**Goal:** Implement agents that execute complex actions

**Deliverables:**
- [ ] `backend/agents/content/essay_writer_agent.py`
- [ ] `backend/agents/content/text_editor_agent.py`
- [ ] `backend/agents/content/typing_agent.py`
- [ ] `backend/agents/code/code_analysis_agent.py`
- [ ] `backend/agents/code/code_solution_agent.py`
- [ ] `backend/agents/code/ide_controller_agent.py`
- [ ] `backend/agents/code/code_editor_agent.py`
- [ ] `backend/agents/ui/window_management_agent.py`
- [ ] `backend/agents/ui/multi_window_coordinator.py`
- [ ] `backend/agents/ui/app_launcher_agent.py`
- [ ] End-to-end tests for complex actions

**Success Criteria:**
- Can write essay and save to file autonomously
- Can fix code errors in VS Code autonomously
- Can manage multi-window workflows via Yabai
- All agents coordinate via Communication Bus
- Complex tasks complete successfully >85% of the time

---

### Phase 5: Autonomous Operation (Weeks 17-20)
**Goal:** Enable proactive, autonomous behaviors

**Deliverables:**
- [ ] `backend/intelligence/predictive_precomputation.py`
- [ ] `backend/intelligence/autonomous_decision_engine.py`
- [ ] `backend/intelligence/autonomous_behaviors_manager.py`
- [ ] Safety rules and guardrails for autonomous actions
- [ ] Undo mechanism for autonomous actions
- [ ] Dashboard for monitoring autonomous behaviors
- [ ] End-to-end integration tests

**Success Criteria:**
- Predictive precomputation hit rate >60%
- Autonomous decisions only made when confidence >85%
- No destructive actions executed autonomously
- All autonomous actions logged and auditable
- System operates autonomously for 4+ hours without intervention

---

### Phase 6: Optimization & Polish (Weeks 21-24)
**Goal:** Optimize performance and user experience

**Deliverables:**
- [ ] Performance profiling and optimization
- [ ] Reduce Communication Bus latency to <5ms
- [ ] Increase Goal Inference accuracy to >80%
- [ ] Dashboard for monitoring system health
- [ ] Documentation and user guide
- [ ] Tutorial videos for autonomous features

**Success Criteria:**
- All performance targets met (NFR-1)
- User satisfaction with autonomous features >85%
- System runs stably for 7+ days without intervention
- Complete documentation and tutorials

---

## Dependencies

### Python Packages (Required)
```
# Core Infrastructure
chromadb>=0.4.0           # Vector database for knowledge graph
networkx>=3.0             # Graph database for knowledge relationships
sentence-transformers>=2.2.0  # Embedding generation for semantic search

# Machine Learning
transformers>=4.30.0      # Hugging Face Transformers for intent classification
torch>=2.0.0             # PyTorch for ML models
scikit-learn>=1.3.0      # ML utilities

# Optional (for distributed mode)
redis>=4.5.0             # Redis backend for Communication Bus
celery>=5.3.0            # Distributed task queue
```

### System Requirements
- Python 3.9+
- 8GB+ RAM (16GB recommended for local ML models)
- 10GB+ disk space (for models and knowledge graph)
- macOS (for Yabai, Core Graphics integration)
- Optional: GPU for faster ML inference

### External Services
- GCP account (project: jarvis-473803) for cloud agents
- Claude API key for vision analysis
- Whisper model for voice recognition

---

## Out of Scope

The following are explicitly **NOT** included in this PRD:

❌ **Claude Computer Use API Integration** - Not required for initial autonomy. Can be added later for GUI automation.

❌ **LangChain / LangGraph / LangFuse** - JARVIS has custom agent architecture. These generic frameworks are unnecessary.

❌ **Web UI / Dashboard** - Phase 1-4 focus on backend. UI is Phase 5+ enhancement.

❌ **Mobile App** - Future consideration, not in current scope.

❌ **Multi-User Support** - JARVIS is currently single-user. Multi-user is future enhancement.

❌ **Cloud-Only Deployment** - Hybrid local/cloud is the architecture. Pure cloud is not planned.

---

## Risks & Mitigations

| Risk | Impact | Probability | Mitigation |
|------|--------|-------------|------------|
| Agent migration breaks existing functionality | HIGH | MEDIUM | Comprehensive integration tests, gradual rollout, feature flags |
| Knowledge Graph grows too large | MEDIUM | HIGH | Implement automatic cleanup of old entries, cap at 100k entries |
| ML models too slow on local machine | MEDIUM | MEDIUM | Support cloud offloading, use smaller models, GPU acceleration |
| Autonomous actions cause user frustration | HIGH | MEDIUM | Conservative autonomy level (default: MEDIUM), undo mechanism, user approval required |
| Inter-agent communication overhead | MEDIUM | LOW | Use local backend (in-process), optimize message serialization |
| Goal Inference accuracy too low | MEDIUM | MEDIUM | Continuous learning from corrections, fine-tune models, increase confidence threshold |

---

## Success Metrics (KPIs)

### Technical Metrics
- ✅ Communication Bus latency: <10ms (target: <5ms)
- ✅ Knowledge Graph query time: <50ms
- ✅ Agent migration: 60/60 agents migrated
- ✅ Test coverage: >80%
- ✅ System uptime: >99.9%

### Intelligence Metrics
- ✅ Goal Inference accuracy: >70% (target: >80%)
- ✅ Activity Recognition accuracy: >80%
- ✅ Workflow pattern detection: 3 repetitions
- ✅ Predictive precomputation hit rate: >60%
- ✅ Autonomous decision confidence: >85%

### User Experience Metrics
- ✅ Autonomous operation time: >4 hours without intervention (target: >8 hours)
- ✅ User satisfaction: >85%
- ✅ False positive rate (wrong predictions): <10%
- ✅ Action latency reduction: >50% for predicted actions

---

## Acceptance Criteria (Overall)

This project is considered **COMPLETE** when:

1. ✅ All Phase 1-4 deliverables are implemented and tested
2. ✅ All 60+ agents migrated to BaseAgent standard
3. ✅ Communication Bus operational with <10ms latency
4. ✅ Knowledge Graph stores 1000+ entries from actual usage
5. ✅ Goal Inference System predicts intent with >70% accuracy
6. ✅ Activity Recognition Engine identifies workflows with >80% accuracy
7. ✅ Autonomous Decision Engine operates safely (no destructive actions)
8. ✅ System runs autonomously for 4+ hours without user intervention
9. ✅ All core functionality regression tests pass
10. ✅ Documentation complete (README, config guide, API docs)

---

## Appendix A: Example Agent Migration

**Before (Isolated Agent):**
```python
# backend/vision/visual_state_management_system.py
class VisualStateManagementSystem:
    def __init__(self):
        self.current_ui_state = {}
    
    def detect_ui_change(self):
        # Detect change
        # No way to communicate with other agents
        pass
```

**After (Neural Mesh Connected):**
```python
# backend/vision/visual_state_management_system.py
from backend.core.base_agent import BaseAgent, MessageType, MessagePriority

class VisualStateManagementAgent(BaseAgent):
    def __init__(self):
        super().__init__(
            agent_name="VSMS_Core",
            agent_type="vision",
            capabilities={"ui_state_tracking", "element_detection", "state_validation"},
            backend="local"
        )
        self.current_ui_state = {}
    
    async def on_initialize(self):
        # Subscribe to UI change events
        await self.subscribe(MessageType.CUSTOM, self._handle_ui_change)
        
        # Query knowledge graph for UI patterns
        patterns = await self.query_knowledge(
            query="ui state patterns",
            knowledge_types=["ui_pattern"]
        )
    
    async def execute_task(self, task_payload: Dict[str, Any]) -> Any:
        if task_payload["action"] == "detect_ui_change":
            change = self._detect_change()
            
            # Publish to other agents
            await self.publish(
                to_agent="SAI",
                message_type=MessageType.EVENT,
                payload={"event": "ui_changed", "change": change},
                priority=MessagePriority.HIGH
            )
            
            # Store pattern in knowledge graph
            await self.add_knowledge(
                knowledge_type="ui_pattern",
                data={"pattern": change, "timestamp": datetime.now()}
            )
            
            return change
```

---

## Appendix B: Configuration Examples

**Minimal Configuration (Conservative):**
```yaml
autonomous:
  enabled: true
  level: "LOW"  # Only safe, non-intrusive actions
  
  goal_inference:
    enabled: true
    confidence_threshold: 0.8  # Higher threshold for safety
  
  autonomous_decision:
    enabled: false  # Disabled, only suggestions
```

**Moderate Configuration (Recommended):**
```yaml
autonomous:
  enabled: true
  level: "MEDIUM"
  
  goal_inference:
    enabled: true
    confidence_threshold: 0.7
  
  activity_recognition:
    enabled: true
  
  workflow_automation:
    enabled: true
    require_user_confirmation: true
  
  autonomous_decision:
    enabled: true
    allowed_actions: ["open_file", "switch_app", "scroll", "search", "navigate"]
```

**Aggressive Configuration (Power Users):**
```yaml
autonomous:
  enabled: true
  level: "HIGH"
  
  goal_inference:
    confidence_threshold: 0.6  # Lower threshold, more predictions
  
  autonomous_decision:
    enabled: true
    allowed_actions: ["*"]  # All except destructive
  
  predictive_precomputation:
    max_cache_size_mb: 1000  # More aggressive caching
```

---

## Questions & Clarifications

If you have questions during implementation, refer to:

1. **Architecture Questions:** See `JARVIS_MULTI_AGENT_SYSTEM_DOCUMENTATION.md`
2. **Roadmap & Timeline:** See `JARVIS_IMPLEMENTATION_ROADMAP.md`
3. **Vision Integration:** See `VISION_INTELLIGENCE_ROADMAP.md`
4. **Voice Integration:** See `IMPLEMENTATION_SUMMARY.md`

For technical decisions not covered in this PRD, follow these principles:
- **Safety First:** Never execute destructive actions autonomously
- **User Control:** User can always disable or override autonomous features
- **Transparency:** All autonomous actions logged and auditable
- **Performance:** Minimize latency and resource usage
- **Simplicity:** Prefer simple, maintainable solutions over complex ones

---

**END OF PRD**

---

## Change Log

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| 1.0 | 2025-11-26 | JARVIS Team | Initial PRD for Neural Mesh implementation |
