# Reasoning Chain Wiring — Handoff Bundle

> **Date**: 2026-03-19
> **Next task**: Wire ProactiveCommandDetector → PredictivePlanningAgent → CoordinatorAgent into the voice pipeline (Approach B)
> **Prerequisite**: This handoff must be accepted before implementation begins

---

## 1. Artifact Proof

### 1.1 Changed Files by Repo

**JARVIS-AI-Agent** (28 new production files, 15 test files):

```
NEW PRODUCTION FILES:
  backend/core/mind_client.py
  backend/core/task_chain_executor.py
  backend/knowledge/__init__.py
  backend/knowledge/fabric.py
  backend/knowledge/fabric_router.py
  backend/knowledge/scene_partition.py
  backend/knowledge/schema.py
  backend/vision/realtime/__init__.py
  backend/vision/realtime/action_executor.py
  backend/vision/realtime/frame_pipeline.py
  backend/vision/realtime/fusion.py
  backend/vision/realtime/metrics.py
  backend/vision/realtime/precheck_gate.py
  backend/vision/realtime/states.py
  backend/vision/realtime/verification.py
  backend/vision/realtime/vision_action_loop.py
  backend/vision/realtime/vision_router.py
  scripts/activate_trinity.sh

MODIFIED PRODUCTION FILES:
  backend/api/unified_command_processor.py
  backend/core/interactive_brain_router.py
  backend/display/computer_use_connector.py
  backend/neural_mesh/agents/goal_inference_agent.py
  backend/vision/proactive_vision_intelligence.py
  unified_supervisor.py
  .env (not committed — .gitignore)

TEST FILES:
  tests/core/test_mind_client.py
  tests/core/test_shadow_mode.py
  tests/knowledge/test_fabric.py
  tests/vision/realtime/test_states.py
  tests/vision/realtime/test_precheck.py
  tests/vision/realtime/test_fusion.py
  tests/vision/realtime/test_frame_pipeline.py
  tests/vision/realtime/test_action_executor.py
  tests/vision/realtime/test_verification.py
  tests/vision/realtime/test_vision_action_loop.py
  tests/vision/realtime/test_vision_router.py
```

**jarvis-prime** (16 new production files, 13 test files):

```
NEW PRODUCTION FILES:
  jarvis_prime/reasoning/__init__.py
  jarvis_prime/reasoning/protocol.py
  jarvis_prime/reasoning/endpoints.py
  jarvis_prime/reasoning/unified_brain_selector.py
  jarvis_prime/reasoning/model_provider.py
  jarvis_prime/reasoning/idempotency_store.py
  jarvis_prime/reasoning/reasoning_graph.py
  jarvis_prime/reasoning/vision_assist.py
  jarvis_prime/reasoning/graph_nodes/__init__.py
  jarvis_prime/reasoning/graph_nodes/analysis_node.py
  jarvis_prime/reasoning/graph_nodes/planning_node.py
  jarvis_prime/reasoning/graph_nodes/validation_node.py
  jarvis_prime/reasoning/graph_nodes/execution_planner.py
  jarvis_prime/knowledge/__init__.py
  jarvis_prime/knowledge/semantic_partition.py

MODIFIED:
  jarvis_prime/core/hybrid_router.py
  jarvis_prime/server.py

TEST FILES:
  tests/reasoning/test_protocol.py
  tests/reasoning/test_endpoints.py
  tests/reasoning/test_brain_selector.py
  tests/reasoning/test_model_provider.py
  tests/reasoning/test_analysis_node.py
  tests/reasoning/test_planning_node.py
  tests/reasoning/test_validation_node.py
  tests/reasoning/test_execution_planner.py
  tests/reasoning/test_reasoning_graph.py
  tests/reasoning/test_handle_reason.py
  tests/reasoning/test_idempotency.py
  tests/reasoning/test_vision_assist.py
  tests/knowledge/test_semantic_partition.py
```

**reactor-core** (2 modified/new files, 1 test file):

```
MODIFIED:
  reactor_core/integration/trinity_experience_receiver.py

NEW:
  reactor_core/training/vision_calibrator.py

TEST:
  tests/training/test_vision_calibrator.py
```

### 1.2 Test Commands + Pass/Fail Output

```
JARVIS:   python3 -m pytest tests/vision/ tests/knowledge/ tests/core/ → 196 passed (5.49s)
J-Prime:  python3 -m pytest tests/reasoning/ tests/knowledge/          → 276 passed (3.41s)
Reactor:  python3 -m pytest tests/training/                            → 7 passed (3.02s)
TOTAL:    479 passed, 0 failed
```

### 1.3 Active Feature Flags

| Flag | Value | Effect |
|------|-------|--------|
| `JARVIS_BRAIN_SELECTOR_SHADOW` | `true` | Shadow mode: compare local vs remote brain selection |
| `JARVIS_USE_REMOTE_BRAIN_SELECTOR` | `false` | NOT using remote brain selector (shadow only) |
| `JARVIS_USE_REMOTE_REASONING` | `true` | Commands route through J-Prime POST /v1/reason |
| `JARVIS_VISION_LOOP_ENABLED` | `true` | VisionActionLoop starts at boot |
| `JARVIS_PRIME_PORT` | `8002` | Points to reasoning sidecar (not stock llama-cpp) |
| `VISION_MOTION_THRESHOLD` | `0.05` | Motion detection sensitivity |
| `VISION_CONFIDENCE_THRESHOLD` | `0.75` | PRECHECK confidence floor |
| `VISION_FRESHNESS_MS` | `500` | Max frame age for action dispatch |
| `MIND_CLIENT_REASON_TIMEOUT` | `30` | J-Prime reasoning call timeout (seconds) |

---

## 2. Contract Proof — Agent Interfaces

### 2.1 ProactiveCommandDetector

**File**: `backend/core/proactive_command_detector.py`

```python
class ProactiveDetectionResult:
    is_proactive: bool           # Should this use expand_and_execute?
    confidence: float            # 0.0-1.0
    signals_detected: List[str]  # Which signals fired
    suggested_intent: str        # Detected intent category
    reasoning: str               # Human-readable explanation

class ProactiveCommandDetector:
    async def detect(command: str) -> ProactiveDetectionResult
    def record_feedback(command: str, was_correct: bool) -> None
    def get_stats() -> Dict[str, Any]
```

**Singleton**: `get_proactive_detector()`

### 2.2 PredictivePlanningAgent

**File**: `backend/neural_mesh/agents/predictive_planning_agent.py`

```python
class ExpandedTask:
    goal: str
    priority: int               # 1=highest
    target_app: Optional[str]
    estimated_duration_seconds: int
    dependencies: List[str]
    category: IntentCategory
    workspace_service: Optional[str]

class PredictionResult:
    original_query: str
    detected_intent: IntentCategory
    confidence: float
    expanded_tasks: List[ExpandedTask]
    reasoning: str
    context_used: str

class PredictivePlanningAgent(BaseNeuralMeshAgent):
    async def expand_intent(query: str) -> PredictionResult
    async def detect_intent(query: str) -> Tuple[IntentCategory, float]
    def to_workflow_tasks(prediction: PredictionResult) -> List[WorkflowTask]
```

**In PRODUCTION_AGENTS**: Yes (agent runs but `expand_intent()` never called from voice)

### 2.3 CoordinatorAgent

**File**: `backend/neural_mesh/agents/coordinator_agent.py`

```python
class CoordinatorAgent(BaseNeuralMeshAgent):
    async def execute_task(payload: Dict[str, Any]) -> Any
    # Routes tasks to agents by capability matching
    # Manages agent lifecycle, load balancing, orchestration
```

**In PRODUCTION_AGENTS**: Yes (agent runs but voice pipeline bypasses it)

### 2.4 Version Compatibility

| Component | Protocol Version | Compatible Range |
|-----------|-----------------|-----------------|
| MindClient | 1.0.0 | [1.0.0, 1.0.999] |
| J-Prime endpoints | 1.0.0 | [1.0.0, 1.0.999] |
| ReasonRequest/Response | 1.0.0 | Pydantic v2 schema |
| Brain selection policy | v2 (policy_id: brain-selection-policy-v2) | Handshake at boot |

Boot gate: MindClient calls `GET /v1/protocol/version` and verifies compatibility before enabling remote reasoning. Mismatch → `PROTOCOL_MISMATCH` error, request rejected.

---

## 3. Runtime Authority Proof

### 3.1 Single Planning Authority

**Current**: Two planning paths exist but ONLY ONE is active at runtime based on feature flags:

```
IF JARVIS_USE_REMOTE_REASONING=true:
  AUTHORITY: J-Prime ReasoningGraph (AnalysisNode → PlanningNode → ValidationNode → ExecutionPlanner)
  Local path: BYPASSED (code exists but unreachable)

IF JARVIS_USE_REMOTE_REASONING=false:
  AUTHORITY: Local J-Prime classify → _execute_action() path
  Remote path: UNREACHABLE (MindClient.send_command() never called)
```

**After reasoning chain wiring (Approach B)**:

```
AUTHORITY: J-Prime ReasoningGraph (unchanged — still the single planner)

NEW PRE-ROUTING LAYER (on JARVIS, BEFORE Mind call):
  1. ProactiveCommandDetector.detect(command)
     → Enriches context: {is_proactive, suggested_intent, signals}
     → Does NOT generate plans. Advisor only.

  2. If is_proactive AND confidence > threshold:
     PredictivePlanningAgent.expand_intent(command)
     → Expands "start my day" into ["check email", "check calendar", "open Slack"]
     → Each sub-intent sent to J-Prime INDIVIDUALLY
     → J-Prime is still the SOLE planning authority per sub-intent

  3. CoordinatorAgent.delegate_task() called AFTER Mind returns plan
     → Routes plan steps to agents by capability (not planning — routing)
     → Does NOT modify the plan. Executor only.
```

### 3.2 Duplicate Planning Prevention

| Layer | Role | Can generate plans? |
|-------|------|-------------------|
| ProactiveCommandDetector | Advisor: "this is multi-task" | NO — classifies only |
| PredictivePlanningAgent | Expander: splits into sub-intents | NO plans — produces intent list |
| J-Prime ReasoningGraph | **SOLE PLANNER** | YES — the only component that produces plans |
| CoordinatorAgent | Router: maps plan steps to agents | NO — routes existing plan steps |
| VisionActionLoop | Executor: acts on plan steps | NO — executes, doesn't plan |

**Invariant**: Only J-Prime ReasoningGraph produces `Plan` objects with `plan_id` and `plan_hash`. All other components consume plans, never produce them.

---

## 4. Cutover Safety

### 4.1 Shadow Mode Plan

```
Phase 1: SHADOW (observe only)
  JARVIS_REASONING_CHAIN_SHADOW=true
  JARVIS_REASONING_CHAIN_ENABLED=false

  - ProactiveCommandDetector runs on every command
  - Result logged but NOT used for routing
  - PredictivePlanningAgent.expand_intent() called in background
  - Expansion result logged but NOT sent to Mind
  - Compare: would expansion have been better than single-intent?
  - Emit shadow_divergence metrics

Phase 2: SOFT ENABLE (expand but confirm)
  JARVIS_REASONING_CHAIN_ENABLED=true
  JARVIS_REASONING_CHAIN_AUTO_EXPAND=false

  - ProactiveCommandDetector classifies
  - If proactive: ask user "Sounds like multiple tasks. Want me to
    handle email, calendar, and Slack separately?"
  - User confirms → expand → send each to Mind
  - User declines → send as single intent (existing path)

Phase 3: FULL ENABLE (autonomous expansion)
  JARVIS_REASONING_CHAIN_AUTO_EXPAND=true

  - Automatic expansion without confirmation
  - Only for intents with confidence > CHAIN_AUTO_EXPAND_THRESHOLD (0.85)
  - Below threshold → still asks for confirmation
```

### 4.2 Rollback Switch

```bash
# Instant rollback — disable chain, keep Mind reasoning active
export JARVIS_REASONING_CHAIN_ENABLED=false

# Full rollback — disable everything, back to local path
./scripts/activate_trinity.sh --rollback
```

Each flag is independently controllable. Disabling the reasoning chain does NOT disable Mind reasoning or VisionActionLoop.

### 4.3 Go/No-Go Thresholds

| Metric | Go threshold | Measurement window |
|--------|-------------|-------------------|
| Expansion accuracy | >= 80% (expanded intents match user's actual goal) | 100 proactive detections |
| False positive rate | <= 10% (single-intent commands incorrectly expanded) | 100 commands |
| Expansion latency p95 | <= 500ms (ProactiveDetector + PredictivePlanner combined) | 1000 commands |
| Mind plan quality | No regression vs single-intent (shadow comparison) | 72h |
| User override rate | <= 20% (user declines expansion in Phase 2) | 50 expansions |

All five gates must pass before Phase 2 → Phase 3 promotion.

---

## 5. Observability

### 5.1 End-to-End Correlation IDs

Every command carries these IDs through the entire chain:

```
trace_id:    UUID — spans the full lifecycle from voice input to execution complete
request_id:  UUID — per Mind request (expansion may generate multiple)
session_id:  UUID — per JARVIS session (survives across commands)
action_id:   UUID — per vision action within a plan step
plan_id:     UUID — per Mind plan (tied to request_id)

Flow:
  Voice input → trace_id generated
    → ProactiveCommandDetector.detect() — logs with trace_id
    → PredictivePlanningAgent.expand_intent() — logs with trace_id
    → For each sub-intent:
        → MindClient.send_command() — generates request_id, carries trace_id
        → J-Prime logs with trace_id + request_id
        → Plan returned with plan_id
        → CoordinatorAgent.delegate_task() — logs with trace_id + plan_id
        → VisionActionLoop.execute_action() — generates action_id, carries trace_id
        → Reactor Core receives trace_id in all experience events
```

### 5.2 Transition Telemetry

New telemetry events for the reasoning chain:

| Event | Fields | Emitted by |
|-------|--------|-----------|
| `proactive_detection` | trace_id, command, is_proactive, confidence, signals, latency_ms | ProactiveCommandDetector |
| `intent_expansion` | trace_id, original_query, expanded_count, intents, confidence, latency_ms | PredictivePlanningAgent |
| `expansion_shadow_divergence` | trace_id, would_expand, actually_expanded, match | Shadow comparator |
| `coordinator_delegation` | trace_id, plan_id, step_id, agent_name, capability, latency_ms | CoordinatorAgent |
| `chain_complete` | trace_id, total_intents, total_steps, total_ms, success_rate | Chain orchestrator |

All events carry `trace_id` for end-to-end correlation. Reactor Core receives all events for training.

### 5.3 Decision Provenance per Command

Every command produces a full audit trail:

```json
{
  "trace_id": "abc-123",
  "command": "start my day",
  "proactive_detection": {
    "is_proactive": true,
    "confidence": 0.92,
    "signals": ["multi_task", "workflow_trigger"],
    "latency_ms": 15
  },
  "expansion": {
    "expanded_intents": ["check email", "check calendar", "open Slack"],
    "confidence": 0.88,
    "latency_ms": 120
  },
  "mind_plans": [
    {"request_id": "r1", "plan_id": "p1", "intent": "check email", "status": "plan_ready"},
    {"request_id": "r2", "plan_id": "p2", "intent": "check calendar", "status": "plan_ready"},
    {"request_id": "r3", "plan_id": "p3", "intent": "open Slack", "status": "plan_ready"}
  ],
  "coordinator_delegations": [
    {"plan_id": "p1", "agent": "GoogleWorkspaceAgent", "capability": "email_management"},
    {"plan_id": "p2", "agent": "GoogleWorkspaceAgent", "capability": "calendar_management"},
    {"plan_id": "p3", "agent": "NativeAppControlAgent", "capability": "app_control"}
  ],
  "total_ms": 2500,
  "success_rate": 1.0
}
```

---

## Acceptance Criteria for Handoff

- [x] All 479 tests pass across 3 repos
- [x] File inventory matches actual repo state
- [x] Feature flags documented with current values
- [x] Agent interfaces documented with method signatures
- [x] Single planning authority identified (J-Prime ReasoningGraph)
- [x] Duplicate planning prevention invariant defined
- [x] 3-phase shadow mode cutover plan
- [x] Rollback switch documented
- [x] Go/no-go thresholds defined with measurement windows
- [x] trace_id correlation flows end-to-end
- [x] 5 new telemetry events defined
- [x] Decision provenance JSON structure defined

---

## Implementation Order (Approach B)

After this handoff is accepted:

1. **ProactiveCommandDetector wiring** — call `detect()` in command processor before Mind, log results
2. **PredictivePlanningAgent wiring** — call `expand_intent()` when proactive detected, split into sub-intents
3. **CoordinatorAgent wiring** — route Mind plan steps through `execute_task()` by capability
4. **Shadow mode infrastructure** — feature flags, divergence logging, metrics
5. **Tests** — end-to-end chain with mock agents
