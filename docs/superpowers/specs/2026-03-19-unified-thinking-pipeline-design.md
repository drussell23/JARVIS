# Unified Thinking Pipeline — Trinity Architecture Design

> **Status**: Approved
> **Date**: 2026-03-19
> **Scope**: JARVIS (Body) + J-Prime (Mind) + Reactor Core (Soul)
> **Approach**: Protocol-first incremental migration (Approach B) with targeted shadow mode

---

## 1. Vision

JARVIS is a synthetic intelligence organism split across three repos:

- **J-Prime** = the **Mind** — LangGraph reasoning, brain selection, model routing, planning, goal inference, reflection
- **JARVIS** = the **Body** — LangChain tools, screen interaction, voice, device control, execution
- **Reactor Core** = the **Soul** — learns from every reasoning step, execution outcome, and approval pattern

The Mind thinks. The Body acts. The Soul evolves.

---

## 2. Architecture

### 2.1 The ONE Thinking Pipeline

All commands flow through a single LangGraph pipeline on J-Prime. The complexity tier determines the graph depth — not which system handles it.

```
voice/text command (JARVIS)
       |
       | ReasonRequest (one network call)
       v
J-PRIME: LangGraph Pipeline
       |
       +-- AnalysisNode ---------> always runs
       |     brain: phi3 (1B) or qwen_coder (7B)
       |     calls: GoalInferenceAgent, CAI intent classifier
       |     output: intent, complexity tier, confidence
       |
       +-- PlanningNode ---------> heavy/complex only
       |     brain: qwen_14b or qwen_32b
       |     calls: PredictivePlanningAgent, ProactiveCommandDetector
       |     output: sub-goals with dependencies
       |
       +-- ValidationNode -------> heavy/complex only
       |     no inference (rule-based)
       |     checks: cost gate, resource gate, approval requirements
       |
       +-- ExecutionPlanner -----> always runs
       |     per sub-goal: UnifiedBrainSelector picks brain + tool
       |     calls: CoordinatorAgent (capability mapping)
       |     output: ReasonResponse.plan (sent to JARVIS)
       |
       +-- ReflectionNode -------> complex only (on feedback)
       |     brain: qwen_coder (7B) or deepseek_r1
       |     input: ReasonFeedback from JARVIS
       |     output: needs_replan flag or goal_validated
       |
       +-- LearningNode ---------> complex only
             no inference (data emission)
             output: training signals -> Reactor Core
```

Graph depth by complexity:

| Complexity | Nodes traversed | Latency |
|-----------|----------------|---------|
| trivial | Analysis -> ExecutionPlanner | ~300ms |
| light | Analysis -> ExecutionPlanner | ~500ms |
| heavy | Analysis -> Planning -> Validation -> ExecutionPlanner | ~5s |
| complex | All nodes, with reflection loop on feedback | ~30-60s |

### 2.2 Split-Brain Execution Model

The Mind produces plans. The Body executes them. Results flow back for reflection.

```
JARVIS (Mac): sends command + context
       |
       v
J-PRIME (GCP): LangGraph reasons, produces plan
       |
       v
JARVIS (Mac): executes plan with LangChain tools
       |
       v
J-PRIME (GCP): reflects on results, learns
       |
       v
JARVIS (Mac): speaks final result
       |
       v
REACTOR CORE: receives training signals from both
```

### 2.3 Unified Brain Selector

Three existing selectors merge into one on J-Prime:

| Current | Merges into |
|---------|------------|
| BrainSelector (Ouroboros, code gen) | UnifiedBrainSelector Layer 1-4 |
| InteractiveBrainRouter (voice) | UnifiedBrainSelector task_type lookup |
| RouteDecisionService (CAI+SAI+UAE) | UnifiedBrainSelector intelligence overlay |

The unified selector runs a 4-layer gate at every LangGraph node:

1. **Intent Gate**: CAI intent -> (complexity, brain_id)
2. **Task Gate**: task_type -> complexity tier (from policy YAML)
3. **Resource Gate**: GPU memory/load -> downgrade if under pressure
4. **Cost Gate**: daily budget -> queue if exceeded

Intelligence overlay:
- SAI health -> auto-downgrade (32B -> 14B -> 7B) under pressure
- UAE context -> tiebreaker for borderline confidence

Policy source: `brain_selection_policy.yaml` (hot-reload, single source of truth)

---

## 3. Mind-Body Protocol (v1.0.0)

### 3.1 Transport

HTTP/JSON over existing J-Prime server (136.113.252.164:8000). New endpoints alongside existing `/v1/chat/completions`.

### 3.2 Endpoints

```
POST /v1/reason           -- Send command, get reasoning result
POST /v1/reason/feedback  -- Send execution results for reflection
GET  /v1/reason/health    -- Mind health + loaded brains + graph readiness
POST /v1/reason/replay    -- Replay queued tasks after recovery
GET  /v1/protocol/version -- Version negotiation + feature flags
```

### 3.3 ReasonRequest Schema

```json
{
  "protocol_version": "1.0.0",
  "request_id": "uuid (unique within idempotency_scope window)",
  "idempotency_scope": "session | global",
  "session_id": "uuid",
  "trace_id": "uuid (end-to-end correlation)",
  "parent_request_id": "uuid | null (distinguishes retries from new intent)",

  "auth": {
    "token_id": "string (identifies the signing key)",
    "signature": "hmac-sha256 of: request_id + command + nonce + issued_at",
    "nonce": "uuid (unique per request, prevents replay)",
    "issued_at": "iso8601 (rejected if > 60s stale)"
  },
  "_auth_note": "Implementation basis: backend/core/umf/signing.py. Shared secret stored in JARVIS_MIND_AUTH_KEY env var on both JARVIS and J-Prime. Nonce replay window: 5 minutes (LRU cache on J-Prime). Auth enforcement deferred to migration Step 4; Steps 0-3 accept unsigned requests with auth.token_id='unsigned'.",

  "command": "research competitors and build a spreadsheet",
  "context": {
    "speaker": "Derek",
    "active_app": "Safari",
    "source": "voice",
    "has_audio": true,
    "recent_history": [],
    "sensory_context": {}
  },
  "_sensory_note": "sensory_context schema matches output of unified_command_processor._gather_sensory_context(): {active_app, screen_state, proactive_predictions, sai_context}.",

  "constraints": {
    "deadline_ms": 30000,
    "deadline_at_ms": 1710876543210,
    "hard_cost_cap_usd": 0.10,
    "soft_cost_target_usd": 0.03,
    "session_budget_remaining_usd": 0.45,
    "allowed_task_classes": ["tier0", "tier1", "tier2", "tier3"]
  },

  "fallback_policy": {
    "allow_tier1": true,
    "allow_tier2": true,
    "queue_on_fail": true,
    "require_approval_in_degraded": true
  }
}
```

### 3.4 ReasonResponse Schema

```json
{
  "protocol_version": "1.0.0",
  "request_id": "uuid",
  "session_id": "uuid",
  "trace_id": "uuid",

  "status": "plan_ready | needs_approval | queued",
  "served_mode": "LEVEL_0_PRIMARY | LEVEL_1_DEGRADED | LEVEL_2_REFLEX",
  "requested_mode": "LEVEL_0_PRIMARY",
  "degraded_reason_code": "null | MIND_UNREACHABLE | BRAIN_NOT_LOADED | ...",

  "classification": {
    "intent": "research_and_create",
    "complexity": "complex",
    "confidence": 0.87,
    "brain_used": "qwen_coder_14b",
    "graph_depth": "full"
  },

  "plan": {
    "plan_id": "uuid",
    "plan_hash": "sha256 (Body must echo in feedback)",
    "sub_goals": [
      {
        "step_id": "s1",
        "action_id": "stable-key-for-idempotency",
        "goal": "open Safari and navigate to competitor websites",
        "task_type": "browser_navigation",
        "brain_assigned": "qwen_coder",
        "tool_required": "visual_browser",
        "depends_on": [],
        "estimated_ms": 5000
      }
    ],
    "execution_strategy": "sequential | parallel | dag",
    "total_estimated_ms": 25000,
    "risk_level": "low | medium | high | critical",
    "approval_required": false,
    "approval_reason_codes": [],
    "approval_scope": "step | plan | global"
  },

  "routing_trace": {
    "analysis_brain": "phi3_lightweight",
    "planning_brain": "qwen_coder_14b",
    "cost_gate_passed": true,
    "resource_gate_passed": true,
    "sai_health": 0.92,
    "cai_confidence": 0.87
  },

  "error": {
    "code": "NONE | MIND_TIMEOUT | BRAIN_NOT_LOADED | COST_EXCEEDED | GRAPH_ERROR | PROTOCOL_MISMATCH",
    "class": "transient | permanent | policy",
    "message": "human-readable",
    "retry_after_ms": null,
    "recovery_strategy": "RETRY_SHORT | QUEUE | FALLBACK_TIER1 | BLOCK"
  }
}
```

### 3.5 ReasonFeedback Schema

```json
{
  "request_id": "uuid",
  "session_id": "uuid",
  "trace_id": "uuid",
  "plan_id": "uuid (must match)",
  "plan_hash": "sha256 (must match)",

  "step_results": [
    {
      "step_id": "s1",
      "action_id": "stable-key",
      "success": true,
      "output": "navigated to competitor.com",
      "latency_ms": 4200,
      "tool_used": "visual_browser",
      "artifact_refs": ["artifacts/screenshots/s1_result.png"],
      "side_effects_committed": true
    }
  ],

  "final_outcome": "success | partial_success | failure",
  "replay_token": "null (set only for replayed tasks)",
  "original_enqueued_at": "null (set only for replayed tasks)",
  "replay_attempt": 0,
  "max_replay_attempts": 3
}
```

### 3.6 Protocol Version Negotiation

`GET /v1/protocol/version` response:

```json
{
  "current_version": "1.0.0",
  "min_supported_version": "1.0.0",
  "max_supported_version": "1.0.999",
  "features": ["brain_selection", "langgraph_reasoning", "reflection", "shadow_mode"],
  "brain_policy_hash": "sha256 of brain_selection_policy.yaml"
}
```

### 3.7 Failure Taxonomy

| Code | Class | Recovery | Description |
|------|-------|----------|-------------|
| MIND_UNREACHABLE | transient | FALLBACK_TIER1 | GCP network down |
| MIND_TIMEOUT | transient | FALLBACK_TIER1 or QUEUE | Deadline exceeded |
| BRAIN_NOT_LOADED | transient | fallback_chain | Requested brain not in GPU memory |
| COST_EXCEEDED | policy | QUEUE | Daily budget exhausted |
| GRAPH_ERROR | transient | FALLBACK_TIER1 | LangGraph node failure |
| PROTOCOL_MISMATCH | permanent | BLOCK | Incompatible protocol versions |
| AUTH_FAILED | permanent | BLOCK | Invalid auth envelope |

### 3.8 Artifact Transfer

Binary data (screenshots, audio) transfers out-of-band. Protocol carries only `artifact_refs` (object-store keys). Transfer mechanism: shared filesystem (`~/.jarvis/artifacts/`) or HTTP upload to J-Prime `/v1/artifacts`.

---

## 4. J-Prime Reasoning Service

### 4.1 Module Structure

> **Cross-repo note**: J-Prime repo is at `JARVIS_PRIME_REPO_PATH` (default: `~/Documents/repos/jarvis-prime`). Module root: `{repo}/jarvis_prime/`. Reactor Core repo: `~/Documents/repos/reactor-core`, module root: `{repo}/reactor_core/`.

```
jarvis_prime/
  reasoning/                        # NEW - the Mind
    __init__.py
    protocol.py                     # v1.0.0 Pydantic schemas
    endpoints.py                    # aiohttp route handlers
    unified_brain_selector.py       # merged 3 selectors
    reasoning_graph.py              # LangGraph StateGraph
    graph_nodes/
      analysis_node.py              # intent + complexity classification
      planning_node.py              # sub-goal decomposition
      validation_node.py            # safety + approval + cost gates
      execution_planner.py          # build plan for Body
      reflection_node.py            # analyze feedback, detect failures
      learning_node.py              # emit training signals
    agents/
      goal_inference.py             # migrated from JARVIS neural_mesh
      predictive_planner.py         # migrated from JARVIS neural_mesh
      coordinator.py                # migrated from JARVIS neural_mesh
      proactive_detector.py         # migrated from JARVIS core
    fallback/
      tier_manager.py               # Tier 0/1/2 state machine
    telemetry/
      reactor_emitter.py            # training signal emission
```

### 4.2 Unified Brain Selector

Merges BrainSelector + InteractiveBrainRouter + RouteDecisionService.

**Complexity normalization**: The Ouroboros `TaskComplexity` enum uses `HEAVY_CODE`. The InteractiveBrainRouter uses `heavy`. The unified selector normalizes all values to lowercase without suffixes: `trivial`, `light`, `heavy`, `complex`. A mapping layer converts `HEAVY_CODE` -> `heavy` at the boundary.

Called at every LangGraph node that needs inference:

| Node | Brain selection | Typical result |
|------|----------------|---------------|
| AnalysisNode | task_type="classification" | phi3_lightweight (1B) |
| PlanningNode | task_type="multi_step_planning" | qwen_coder_14b or _32b |
| ValidationNode | no inference | none |
| ExecutionPlanner | per sub-goal task_type | varies |
| ReflectionNode | task_type="complex_reasoning" | qwen_coder or deepseek_r1 |
| LearningNode | no inference | none |

### 4.3 Policy YAML Evolution

`brain_selection_policy.yaml` gains new sections:

```yaml
# Existing sections stay (brains, routing, fallback_chain, authority_boundaries)

# NEW: Interactive task complexity (moved from InteractiveBrainRouter Python dicts)
interactive_task_complexity:
  workspace_fastpath: "trivial"
  system_command: "trivial"
  reflex_match: "trivial"
  classification: "light"
  step_decomposition: "light"
  goal_chain_step: "light"
  vision_action: "heavy"
  screen_observation: "heavy"
  browser_navigation: "heavy"
  multi_step_planning: "complex"
  complex_reasoning: "complex"

# NEW: LangGraph node brain preferences
node_brain_preferences:
  analysis:
    default: "phi3_lightweight"
    ambiguous: "qwen_coder"
  planning:
    default: "qwen_coder_14b"
    architecture: "qwen_coder_32b"
  execution:
    per_step: true
  reflection:
    default: "qwen_coder"
    deep: "deepseek_r1"
  learning:
    inference: false
```

### 4.4 Agent Migration

These agents move from JARVIS to J-Prime (code relocated, not rewritten):

| Agent | From (JARVIS) | To (J-Prime) | Called by |
|-------|--------------|-------------|----------|
| GoalInferenceAgent | neural_mesh/agents/goal_inference_agent.py | reasoning/agents/goal_inference.py | AnalysisNode |
| PredictivePlanningAgent | neural_mesh/agents/predictive_planning_agent.py | reasoning/agents/predictive_planner.py | PlanningNode |
| CoordinatorAgent | neural_mesh/agents/coordinator_agent.py | reasoning/agents/coordinator.py | ExecutionPlanner |
| ProactiveCommandDetector | core/proactive_command_detector.py | reasoning/agents/proactive_detector.py | ExecutionPlanner |

Agents become functions called within graph nodes, not standalone services.

---

## 5. JARVIS Body Executor

### 5.1 New Module: MindClient

`backend/core/mind_client.py` replaces direct PrimeClient usage for reasoning:

```python
class MindClient:
    async send_command(command, context) -> ReasonResponse
    async send_feedback(request_id, results) -> ReflectResult
    async check_health() -> MindHealth
    async replay_queue() -> List[ReasonResponse]

    # Tier management
    _current_tier: Tier  # TIER_0 | TIER_1 | TIER_2
    _tier_transitions: HysteresisStateMachine
    _task_queue: PersistentDeque  # ~/.jarvis/mind/deferred_tasks.json
```

### 5.2 Command Processor Changes

`unified_command_processor._execute_command_pipeline()` simplifies to:

```
1. Reflex manifest check (sub-ms, stays local, unchanged)
2. If no reflex match:
   a. mind_client.send_command(command, context)
   b. If needs_approval: VoiceApprovalManager asks Derek
   c. Execute each sub-goal with LangChain tools
   d. mind_client.send_feedback(results)
   e. Speak final result
```

Steps 1.5 (workspace fast-path), 1.7 (biometric unlock), 1.8 (screen observation), 1.9 (proactive mode) remain as local fast-paths on JARVIS. They skip J-Prime for latency-critical device interactions.

### 5.3 LangChain Tools (Stay on JARVIS)

| Tool | Capability | Hardware |
|------|-----------|---------|
| screen_capture | screencapture -> base64 | Mac display |
| app_control | AppleScript / yabai | macOS APIs |
| browser_navigate | VisualBrowserAgent | Screen + keyboard |
| computer_use | ClaudeComputerUseConnector | Screen + mouse |
| voice_speak | safe_say TTS | Mac audio |
| voice_listen | Microphone capture | Mac mic |
| file_ops | Local filesystem | Mac disk |
| workspace_query | Google Workspace API | OAuth tokens |
| vision_observe | ScreenCaptureKit + LLaVA | Mac display + J-Prime GPU |

---

## 6. Reactor Core Integration

### 6.1 Training Signals from Mind (J-Prime)

| Experience type | Source | What it captures |
|----------------|--------|-----------------|
| brain_routing_decision | UnifiedBrainSelector | which brain selected, why, confidence |
| reasoning_trace | LangGraph pipeline | full node execution trace |
| planning_quality | PlanningNode | sub-goal decomposition details |
| reflection_assessment | ReflectionNode | self-evaluation accuracy |
| goal_inference_result | GoalInferenceAgent | intent prediction accuracy |
| prediction_accuracy | PredictivePlanningAgent | anticipation correctness |

### 6.2 Training Signals from Body (JARVIS)

| Experience type | Source | What it captures |
|----------------|--------|-----------------|
| execution_outcome | Body executor | per-step success/failure |
| tool_performance | LangChain tools | tool latency, errors |
| approval_pattern | VoiceApprovalManager | Derek's yes/no decisions |
| mode_transition | MindClient tier manager | tier changes, recovery events |
| queue_replay_result | Replay mechanism | deferred task outcomes |

### 6.3 Feedback Loop

Reactor Core trains on signals and produces:
- Adjusted brain selection weights -> `brain_selection_policy.yaml`
- Approval threshold updates -> VoiceApprovalManager config
- Complexity calibration -> `interactive_task_complexity` section
- Cost optimization -> budget allocation per brain

---

## 7. Fallback Levels (Option C: Tiered Hybrid)

> **Naming**: "Level 0/1/2" = operational fallback modes (Mind availability).
> "tier0/tier1/tier2/tier3" = brain task classes (model capability). Different concepts.

### 7.1 Level State Machine

```
LEVEL_0 --health_fails--> LEVEL_1 --claude_fails--> LEVEL_2
  ^                                                    |
  +--------health_passes(3x consecutive)--------------+
```

Hysteresis: 3 consecutive healthy checks required before upgrading level.

### 7.2 Level Definitions

**Level 0 (Normal)**: J-Prime full pipeline
- All complexities served
- Full graph depth
- All brains available

**Level 1 (Degraded Thinking)**: JARVIS emergency planner via Claude API
- Reduced graph: AnalysisNode -> ExecutionPlanner only
- Only trivial/light/heavy intents served
- Complex tasks queued
- All outputs tagged DEGRADED_MODE
- Hard cost cap: $0.50/session, tracked per `session_id` in `MindClient._degraded_cost_tracker`
- Cap enforcement: checked before each Claude API call; if exceeded, transition to Level 2
- High-risk actions require voice approval regardless of confidence
- Explicitly versioned reduced graph profile
- Same intent/plan schema as J-Prime (no drift)

**Level 2 (Reflex + Queue)**: No thinking
- Only reflex manifest matches execute
- All else queued to ~/.jarvis/mind/deferred_tasks.json
- Idempotency keys prevent duplicate replay
- On recovery: POST /v1/reason/replay with dedupe + ordering

### 7.3 Queue + Replay

**DeferredTask schema** (persisted to `~/.jarvis/mind/deferred_tasks.json`):
```json
{
  "request_id": "uuid",
  "idempotency_key": "sha256(session_id + command)",
  "replay_token": "uuid",
  "original_enqueued_at": "iso8601",
  "command": "original command text",
  "context": {},
  "attempt_count": 0,
  "max_attempts": 3,
  "enqueue_reason": "MIND_UNREACHABLE | COST_EXCEEDED | ..."
}
```

- Tasks persist with `replay_token`, `original_enqueued_at`, `idempotency_key`
- Max replay attempts: 3 (configurable)
- Replay respects original ordering within session
- Cross-session replay ordered by `original_enqueued_at`
- Dedupe: `request_id` uniqueness window (24h default)

### 7.4 Recovery Semantics

- Active degraded tasks finish cleanly before tier upgrade
- New tasks route to recovered primary immediately
- Health hysteresis prevents flapping (3 consecutive checks)
- Mode transitions emit telemetry to Reactor Core

---

## 8. Boot + Runtime Contract Hardening

### 8.1 Boot Gate

On JARVIS startup:
1. `GET /v1/protocol/version` from J-Prime
2. Verify `protocol_version` within `[min_supported, max_supported]` range
3. Verify `brain_policy_hash` matches local copy
4. If mismatch: log CRITICAL, start in TIER_1 degraded mode
5. Verify `features` includes required capabilities

### 8.2 Runtime Drift Detection

- Periodic health check (30s interval): `GET /v1/reason/health`
- Monitor brain inventory changes (model loads/unloads)
- Monitor policy YAML hash changes
- On drift detected: quarantine affected capability, emit alert

---

## 9. Migration Steps (Approach B)

| Step | What moves | Feature flag | Shadow mode | Rollback |
|------|-----------|-------------|-------------|----------|
| 0 | Protocol schemas + /v1/reason/health on J-Prime | N/A (additive) | No | N/A |
| 1 | UnifiedBrainSelector to J-Prime | JARVIS_USE_REMOTE_BRAIN_SELECTOR | Yes: compare local vs remote | Flip flag |
| 2 | AnalysisNode + PlanningNode | JARVIS_USE_REMOTE_REASONING | Yes: compare plans | Flip flag |
| 3 | Agent migration (GoalInference, Predictive, Coordinator) | Per-agent flag | No (internal to nodes) | Flip flag |
| 4 | Full pipeline (Validation + Reflection + Learning) | JARVIS_MIND_MODE=local/remote/shadow | Yes: routing + approval | Flip flag |
| 5 | Remove duplicated code from JARVIS | N/A | No | Git revert |

### 9.1 Evidence Required Per Step

- Unit + integration tests passing
- Shadow divergence rate < 5% for critical outputs
- Replay/dedupe proof (inject duplicate, verify single execution)
- Failover test (kill J-Prime, verify Tier 1 activates < 5s)
- Recovery test (restore J-Prime, verify Tier 0 resumes with hysteresis)
- No split-brain authority (verify JARVIS never makes brain selection when J-Prime is primary)

---

## 10. What LangChain Does (The Hands)

LangChain's role is the tool interface layer on JARVIS. It wraps device capabilities as standardized `BaseTool` objects that J-Prime's plan references by `tool_required` field.

Existing LangChain integration points (stay on JARVIS):
- `autonomy/langchain_tools.py` — dynamic tool registry
- `voice_unlock/orchestration/voice_auth_tools.py` — voice auth as StructuredTool
- `voice_unlock/orchestration/voice_auth_orchestrator.py` — multi-factor auth chain

The Body executor maps `tool_required` from ReasonResponse to the appropriate LangChain tool and invokes it locally.

---

## 11. Observability

Every component emits structured telemetry:

| Event | Source | Fields |
|-------|--------|--------|
| mode_transition | MindClient | from_tier, to_tier, reason_code, timestamp |
| fallback_activated | MindClient | reason, degraded_intent_count |
| shadow_divergence | Shadow comparator | old_decision, new_decision, field, severity |
| queue_depth | Task queue | depth, oldest_task_age_s |
| replay_outcome | Replay handler | replay_token, success, attempt_number |
| degraded_task_outcome | Tier 1 executor | request_id, was_degraded, outcome |
| protocol_mismatch | Boot gate | local_version, remote_version, action_taken |
| brain_drift | Runtime monitor | expected_hash, actual_hash, quarantined |

---

## 12. Testing Strategy

### 12.1 Unit Tests (per module)

| Module | Test file | Scope |
|--------|----------|-------|
| protocol.py | tests/reasoning/test_protocol.py | Schema validation, serialization roundtrip |
| unified_brain_selector.py | tests/reasoning/test_brain_selector.py | 4-layer gate logic, downgrade, cost tracking |
| reasoning_graph.py | tests/reasoning/test_graph.py | Graph traversal per complexity tier |
| Each graph_node | tests/reasoning/test_nodes.py | Node input/output contracts |
| mind_client.py | tests/core/test_mind_client.py | Tier transitions, hysteresis, queue persistence |

### 12.2 Integration Tests

- **Mock J-Prime server**: aiohttp test server implementing `/v1/reason` + `/v1/reason/health` with canned responses. Used for all JARVIS-side integration tests.
- **Live J-Prime tests**: run against actual GCP instance, gated behind `JARVIS_LIVE_JPRIME_TESTS=true` env var. Used for migration step validation only.
- **Reactor Core tests**: verify experience emission format matches `EXPERIENCE_EVENT_TYPES` set.

### 12.3 Component Acceptance Criteria

| Component | Pass criteria |
|-----------|-------------|
| UnifiedBrainSelector | Given task_type X + resource pressure Y, selects brain Z within 10ms. Fallback chain activates when primary brain unavailable. Cost gate blocks when daily budget exceeded. |
| LangGraph pipeline | Trivial command completes graph in <100ms (excluding network). Complex command traverses all nodes. Reflection loop detects failure and sets needs_replan. |
| MindClient | Tier 1 activates within 5s of J-Prime health failure. Tier 0 resumes after 3 consecutive healthy checks. Queue persists across process restart. |
| PersistentDeque | Atomic writes (no corruption on crash). Deduplicates by request_id. Respects max_replay_attempts. |

### 12.4 Shadow Mode Validation

**Fields compared**: `brain_assigned`, `complexity`, `confidence`, `plan.sub_goals` (count and task_types), `approval_required`.

**Severity levels**:
- INFO: confidence difference < 10%
- WARN: brain selection differs, or sub-goal count differs
- CRITICAL: missing/extra sub-goals, or approval_required disagrees

**Promotion gate**: 72h with < 5% WARN-or-above divergence rate.

### 12.5 Latency Regression

- Trivial commands: p99 < 500ms (end-to-end including network)
- Light commands: p99 < 2s
- Heavy commands: p99 < 15s
- Complex commands: p99 < 120s

## 13. Concurrency Model

### 13.1 MindClient on JARVIS

- `MindClient` runs as an asyncio singleton on the main event loop (same as `unified_supervisor.py`).
- Health checks run in a background `asyncio.Task` (30s interval), created at MindClient init, cancelled on shutdown.
- Tier transition state machine uses `asyncio.Lock` (not threading.Lock) — all access is from the event loop.
- The `/v1/reason` call uses `asyncio.wait_for()` with the request's `deadline_ms` as timeout.

### 13.2 PersistentDeque

- Enqueue (from executor) and dequeue (from replay) are serialized via `asyncio.Lock`.
- Writes use atomic temp-file + rename pattern (`write to .tmp`, `os.replace()` to target).
- Maximum queue size: 500 tasks (configurable via `JARVIS_MIND_QUEUE_MAX`). Oldest tasks evicted on overflow.
- On corrupt queue file: quarantine to `deferred_tasks.json.corrupt`, start fresh, emit CRITICAL telemetry.

### 13.3 Cancellation Safety

- `asyncio.wait_for()` on MindClient calls uses `asyncio.shield()` for any task that must complete (e.g., feedback emission).
- Background health check task catches `CancelledError` (BaseException in Python 3.9+) and exits cleanly.

## 14. Circuit Breaker (Mind-Body Link)

`MindClient` includes a circuit breaker for the `/v1/reason` endpoint:

**States**: CLOSED (normal) -> OPEN (blocked) -> HALF_OPEN (testing recovery)

**Transitions**:
- CLOSED -> OPEN: 3 consecutive failures (timeout, connection error, 5xx)
- OPEN -> HALF_OPEN: after 30s cooldown
- HALF_OPEN -> CLOSED: 1 successful request
- HALF_OPEN -> OPEN: 1 failure

**Relationship to tiers**:
- Circuit OPEN triggers Tier 1 activation (if not already degraded)
- Circuit CLOSED + health passing triggers Tier 0 recovery (with hysteresis)

Follows the same `CircuitBreaker` pattern used in `backend/core/prime_client.py`.

## 15. Non-Goals (Explicit Exclusions)

- AGI OS coordinator integration (separate future project)
- Autonomy layer full migration (too large, separate project)
- Voice biometric enhancement (separate spec exists in CLAUDE.md)
- Ouroboros governance pipeline changes (code gen stays on existing path)
- Frontend/UI changes (no UI in this spec)
