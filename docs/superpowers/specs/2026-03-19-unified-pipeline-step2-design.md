# Unified Thinking Pipeline — Step 2: LangGraph Reasoning on J-Prime

> **Status**: Approved
> **Date**: 2026-03-19
> **Scope**: J-Prime (Mind) + JARVIS (Body)
> **Parent spec**: `docs/superpowers/specs/2026-03-19-unified-thinking-pipeline-design.md`
> **Depends on**: Step 0+1 complete (protocol schemas, brain selector, MindClient, health endpoints)

---

## 1. Goal

Ship `POST /v1/reason` — the canonical Mind API. JARVIS sends a command, J-Prime reasons through it (classify, decompose, validate, assemble plan), and returns a full `ReasonResponse` that JARVIS executes with LangChain tools.

This is the first time J-Prime **thinks**, not just selects brains.

---

## 2. Graph Structure

```
POST /v1/reason receives ReasonRequest
       |
       v
  AnalysisNode (always runs)
    - UnifiedBrainSelector picks brain (phi3 or qwen_7b)
    - Calls GPU model for intent + complexity classification
    - On model failure: switch to LEVEL_1_DEGRADED, cap confidence at 0.6
    - Output: intent, complexity, confidence, inferred_goals
       |
       | route_by_depth(complexity)
       |
  trivial/light ---------> skip to ExecutionPlanner
       |
  heavy/complex
       |
       v
  PlanningNode
    - UnifiedBrainSelector picks brain (qwen_14b or qwen_32b)
    - Calls GPU model for sub-goal decomposition
    - On model failure: switch to LEVEL_1_DEGRADED, use keyword expansion
    - Output: sub_goals with dependencies, execution_strategy
       |
       v
  ValidationNode (runs whenever PlanningNode runs)
    - Rule-based, deterministic, NO model call
    - Cost gate, resource gate, approval gate
    - FAIL-CLOSED: any gate error -> status=needs_approval
    - Output: approval_required, approval_reason_codes, risk_level
       |
       v
  ExecutionPlanner (always runs)
    - Per sub-goal: assign brain + tool via UnifiedBrainSelector
    - Build Plan with plan_id + plan_hash (canonical JSON SHA-256)
    - Assemble ReasonResponse
    - Output: full ReasonResponse for JARVIS Body
```

---

## 3. Explicit Model Failure Policy

**No silent fallbacks.** When a GPU model call fails inside any node:

1. Set `served_mode = "LEVEL_1_DEGRADED"` on the response
2. Set `degraded_reason_code` to the specific failure (e.g., `"ANALYSIS_MODEL_UNAVAILABLE"`, `"PLANNING_MODEL_TIMEOUT"`)
3. Cap confidence at 0.6 (degraded results are never high-confidence)
4. Use pattern-matching/keyword fallback for the failed node only
5. Subsequent nodes see the degraded flag and adjust behavior
6. The response is honest about what happened — JARVIS knows it got a degraded result

```python
# Inside AnalysisNode._call_model():
try:
    result = await self._gpu_inference(prompt, brain_selection)
    return result, "LEVEL_0_PRIMARY"
except Exception as exc:
    logger.warning("[AnalysisNode] Model call failed: %s — using pattern fallback", exc)
    result = self._pattern_fallback(state)
    result["confidence"] = min(result.get("confidence", 0.5), 0.6)
    return result, "LEVEL_1_DEGRADED"
```

---

## 4. Fail-Closed Validation

ValidationNode is **authoritative and fail-closed**. Three gates, all deterministic:

### 4.1 Cost Gate

```
estimated_plan_cost = sum(brain_cost_estimate(step.brain_assigned) for step in sub_goals)
if daily_spend + estimated_plan_cost > hard_cost_cap_usd:
    -> approval_required=True, reason="COST_EXCEEDED"
if daily_spend + estimated_plan_cost > soft_cost_target_usd:
    -> approval_required=False, but routing_trace.cost_gate_passed=False (advisory)
```

### 4.2 Resource Gate

```
for each sub_goal:
    if sub_goal.tool_required not in KNOWN_BODY_CAPABILITIES:
        -> approval_required=True, reason="TOOL_UNAVAILABLE"
    if sub_goal.brain_assigned not in loaded_brains:
        -> try fallback_chain; if all exhausted:
           -> approval_required=True, reason="BRAIN_UNAVAILABLE"
```

`KNOWN_BODY_CAPABILITIES` is a static set matching JARVIS LangChain tools:
`{app_control, visual_browser, screen_capture, computer_use, voice_speak, voice_listen, file_ops, workspace_query, vision_observe}`

### 4.3 Approval Gate

High-risk action classes that ALWAYS require approval:

| Action class | task_type pattern | Why |
|-------------|------------------|-----|
| Destructive file ops | `file_delete`, `file_overwrite` | Data loss |
| Financial | `payment`, `purchase`, `subscribe` | Money |
| Communication | `email_compose`, `message_send` | Sends on behalf of user |
| Security | `unlock`, `auth`, `permission_change` | Access control |
| System | `system_shutdown`, `process_kill` | Disruption |

In degraded mode (`served_mode=LEVEL_1_DEGRADED`), approval requirements are **stricter, never looser**:
- All `heavy` tasks require approval (not just `complex`)
- Confidence threshold for auto-execute raised from 0.90 to 0.95

### 4.4 Fail-Closed Behavior

If any gate encounters an internal error (exception, missing data, corrupt state):

```python
# NEVER return plan_ready when validation fails
return ValidationResult(
    status="needs_approval",
    approval_required=True,
    approval_reason_codes=["VALIDATION_UNAVAILABLE"],
    risk_level="high",
    validation_error=str(exc),
)
```

This is enforced in code via try/except around each gate. The outer handler catches any unhandled exception and returns `VALIDATION_UNAVAILABLE`.

---

## 5. Plan Hash — Canonical Hashing

`plan_hash` is SHA-256 of a canonical JSON representation of the plan:

```python
import hashlib
import json

def compute_plan_hash(plan: Plan) -> str:
    """Canonical JSON hash for plan integrity verification.

    Rules:
    - Keys sorted alphabetically at every level
    - No whitespace (separators=(',', ':'))
    - Floats normalized to 6 decimal places
    - UTF-8 encoding
    - Includes: sub_goals, execution_strategy, approval_required
    - Excludes: plan_id (circular), timestamps (non-deterministic)
    """
    hashable = {
        "sub_goals": [
            {
                "step_id": sg.step_id,
                "action_id": sg.action_id,
                "goal": sg.goal,
                "task_type": sg.task_type,
                "brain_assigned": sg.brain_assigned,
                "tool_required": sg.tool_required,
                "depends_on": sorted(sg.depends_on),
            }
            for sg in plan.sub_goals
        ],
        "execution_strategy": plan.execution_strategy,
        "approval_required": plan.approval_required,
    }
    canonical = json.dumps(hashable, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
```

JARVIS Body echoes `plan_id` + `plan_hash` in `ReasonFeedback`. J-Prime verifies the hash matches before processing feedback. Mismatch → reject with `PLAN_HASH_MISMATCH` error.

---

## 6. Protocol Negotiation (Hard-Gated)

`POST /v1/reason` rejects incompatible protocol versions before any processing:

```python
async def handle_reason(req: ReasonRequest) -> dict:
    # Hard gate: reject incompatible protocol
    if not _is_compatible(req.protocol_version):
        return ReasonResponse(
            request_id=req.request_id,
            session_id=req.session_id,
            status="error",
            served_mode="LEVEL_0_PRIMARY",
            error=ErrorDetail(
                code="PROTOCOL_MISMATCH",
                error_class="permanent",
                message=f"Protocol {req.protocol_version} not in [{MIN_SUPPORTED}, {MAX_SUPPORTED}]",
                recovery_strategy="BLOCK",
            ),
        ).model_dump(mode="json", by_alias=True)

    # ... proceed with reasoning
```

---

## 7. Idempotency Persistence

`request_id` dedupe must survive process restarts.

**Implementation**: SQLite file at `~/.jarvis-prime/reasoning/idempotency.db`

```sql
CREATE TABLE IF NOT EXISTS idempotency (
    request_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    response_json TEXT NOT NULL
);
CREATE INDEX idx_created ON idempotency(created_at);
```

- Dedupe window: 24h (configurable via `REASON_IDEMPOTENCY_WINDOW_H`)
- On startup: prune entries older than window
- On request: check `request_id` exists → return cached response
- On completion: store response
- Max entries: 10,000 (evict oldest on overflow)

---

## 8. Shadow Mode Cutover Thresholds

Shadow mode (`JARVIS_REASONING_SHADOW=true`) runs both local and remote reasoning, compares results.

### 8.1 Metrics Collected

| Metric | What it measures |
|--------|-----------------|
| `classification_divergence` | intent or complexity disagree |
| `plan_structure_divergence` | sub-goal count, task_types, or dependency graph differ |
| `approval_divergence` | approval_required disagrees (CRITICAL) |
| `brain_assignment_divergence` | different brain assigned to same step |

### 8.2 Severity Levels

| Level | Definition | Example |
|-------|-----------|---------|
| INFO | Confidence differs by <10% | Local 0.87 vs remote 0.92 |
| WARN | Brain or complexity differs | Local qwen_7b vs remote qwen_14b |
| CRITICAL | Approval decision differs, or sub-goal count differs by >1 | Local says safe, remote says needs_approval |

### 8.3 Cutover Gates (hard thresholds)

```
CRITICAL divergence rate:  <= 1% over 1000 requests (or 72h, whichever first)
WARN divergence rate:      <= 5% over same window
Latency regression:        p99 <= 2x current p99 for same complexity tier
Error rate:                <= 0.5% of requests return error status
```

All four gates must pass simultaneously. If any regresses after cutover, automatic rollback to shadow mode.

---

## 9. Replay Semantics

For tasks queued during Level 2 (reflex-only mode):

### 9.1 Queue Ordering

Tasks replayed in order of `original_enqueued_at` within each `session_id`. Cross-session ordering by `original_enqueued_at`.

### 9.2 Dedupe

Before replay, check `request_id` against idempotency store. If already processed, skip.

### 9.3 Max Replay Attempts

Each deferred task has `max_replay_attempts=3` (configurable). Counter incremented on each attempt. After max reached, task moves to poison queue (`~/.jarvis/mind/poison_tasks.json`) and emits CRITICAL telemetry.

### 9.4 Poison Task Handling

Poison tasks are:
- Logged with full context (command, original error, all attempt results)
- Never automatically retried
- Surfaced to user via voice notification: "I wasn't able to complete [task] after several attempts. Want me to try a different approach?"
- Available for manual inspection at the poison queue path

---

## 10. Approval Policy Boundaries

### 10.1 Normal Mode (Level 0)

| Confidence | Action |
|-----------|--------|
| >= 0.90 | Auto-execute (unless action class is always-approve) |
| 0.70 - 0.90 | Execute with advisory flag |
| 0.50 - 0.70 | Request approval via voice |
| < 0.50 | Suggest only (no execution without explicit approval) |

### 10.2 Degraded Mode (Level 1)

Stricter, never looser:

| Confidence | Action |
|-----------|--------|
| >= 0.95 | Auto-execute trivial/light only (heavy/complex always approve) |
| 0.70 - 0.95 | Request approval via voice |
| < 0.70 | Queue for Level 0 recovery (don't attempt in degraded mode) |

### 10.3 Invariants

- Degraded mode NEVER has looser approval than normal mode
- High-risk action classes ALWAYS require approval regardless of confidence or mode
- `approval_scope` is per-plan (not per-step) unless individual steps are flagged high-risk

---

## 11. Internal Graph State

`ReasoningGraphState` is the Pydantic model that flows between LangGraph nodes (J-Prime internal, not on the wire):

```python
class ReasoningGraphState(BaseModel):
    # Identity (from ReasonRequest)
    request_id: str
    session_id: str
    trace_id: str
    command: str
    context: Dict[str, Any] = {}

    # Phase tracking
    phase: str = "initializing"  # analyzing | planning | validating | assembling | completed
    served_mode: str = "LEVEL_0_PRIMARY"
    degraded_reason_code: Optional[str] = None

    # Analysis output
    intent: str = ""
    complexity: str = "light"
    confidence: float = 0.0
    inferred_goals: List[str] = []
    analysis_brain_used: str = ""

    # Planning output
    sub_goals: List[Dict[str, Any]] = []
    action_graph: Dict[str, List[str]] = {}  # step_id -> [depends_on]
    execution_strategy: str = "sequential"
    planning_brain_used: str = ""

    # Validation output
    approval_required: bool = False
    approval_reason_codes: List[str] = []
    risk_level: str = "low"
    cost_gate_passed: bool = True
    resource_gate_passed: bool = True

    # Control
    graph_depth: str = "fast"  # fast | standard | full
    error_count: int = 0

    # Trace
    reasoning_trace: List[Dict[str, Any]] = []
```

## 12. GPU Inference Wiring

Each graph node calls the GPU model via J-Prime's existing inference path:

```python
# Inside AnalysisNode._gpu_inference():
# J-Prime's run_server.py loads models via llama-cpp-python.
# Nodes call the model through the same internal API that
# /v1/chat/completions uses — the Llama instance in _startup_state.

from jarvis_prime.server import _startup_state  # or equivalent accessor

async def _gpu_inference(self, prompt: str, brain: UnifiedBrainSelection) -> dict:
    """Call the loaded GPU model for inference.

    Uses the same Llama instance that serves /v1/chat/completions.
    The brain_selection determines which model should be loaded.
    """
    model = _startup_state.get_model()  # llama_cpp.Llama instance
    if model is None:
        raise RuntimeError("No model loaded")

    response = model.create_chat_completion(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=brain.max_output_tokens or 1024,
        temperature=0.2,
    )
    return {"content": response["choices"][0]["message"]["content"]}
```

If the model currently loaded doesn't match `brain.model_name`, the node uses pattern fallback and marks the response as degraded (not hot-swapping models mid-request).

## 13. Cost Estimation

`brain_cost_estimate()` is a simple lookup — estimated USD per call based on model size and typical token usage:

```python
_BRAIN_COST_ESTIMATES: Dict[str, float] = {
    "phi3_lightweight": 0.0001,    # 1B model, trivial
    "qwen_coder": 0.0005,         # 7B model, light
    "qwen_coder_14b": 0.001,      # 14B model, medium
    "qwen_coder_32b": 0.003,      # 32B model, heavy
    "deepseek_r1": 0.001,         # 7B reasoning, medium
    "mistral_7b_fallback": 0.0005, # 7B fallback
}

def brain_cost_estimate(brain_id: str) -> float:
    return _BRAIN_COST_ESTIMATES.get(brain_id, 0.001)
```

GCP compute cost (GPU time) is included in these estimates. Claude API fallback costs are tracked separately via `daily_spend_claude`.

## 14. Replay Scope Boundary

Section 9 (Replay Semantics) covers the **basic queue + replay mechanism** needed for Level 2 recovery in Step 2. Step 3 adds on top:
- Voice notification for poison tasks ("I wasn't able to complete...")
- Cross-session replay orchestration
- Reactor Core training signals from replay outcomes
- Replay success rate monitoring in observability dashboard

## 15. File Structure

### J-Prime (new files)

```
jarvis_prime/reasoning/
  graph_nodes/
    __init__.py
    analysis_node.py        # Intent classification via GPU model
    planning_node.py        # Sub-goal decomposition via GPU model
    validation_node.py      # Rule-based cost/resource/approval gates
    execution_planner.py    # Per-step brain+tool assignment, plan assembly
  reasoning_graph.py        # LangGraph StateGraph + depth routing
  idempotency_store.py      # SQLite-backed request_id dedupe
```

### J-Prime (modified files)

```
jarvis_prime/reasoning/
  endpoints.py              # Add handle_reason() for POST /v1/reason
  protocol.py               # Add ReasoningGraphState (internal state model)
jarvis_prime/
  server.py                 # Register POST /v1/reason route
```

### JARVIS (modified files)

```
backend/core/
  mind_client.py            # Add send_command() method
backend/api/
  unified_command_processor.py  # Wire send_command() behind feature flag
```

---

## 16. JARVIS-Side Wiring

### 12.1 MindClient.send_command()

```python
async def send_command(
    self,
    command: str,
    context: Optional[Dict[str, Any]] = None,
    deadline_ms: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Send command to J-Prime for full reasoning.

    Returns ReasonResponse dict with plan, or None on failure.
    Level 2: returns None (queue + reflex only).
    Level 1: returns degraded result from emergency planner.
    """
```

### 12.2 Command Processor Wiring

Behind `JARVIS_USE_REMOTE_REASONING=true`:

```
1. Reflex check (unchanged)
2. If no reflex: mind_client.send_command(command, context)
3. If response.status == "plan_ready":
   a. Execute plan sub-goals with LangChain tools
   b. mind_client.send_feedback(results)
4. If response.status == "needs_approval":
   a. VoiceApprovalManager asks Derek
   b. If approved: execute plan
   c. If denied: discard plan, inform user
5. If response is None (Mind unavailable):
   a. Fall back to existing J-Prime classify -> local execution
```

---

## 17. Acceptance Criteria

- [ ] Trivial command ("open Safari") -> Analysis -> ExecutionPlanner, plan returned in <500ms
- [ ] Complex command ("research competitors, build spreadsheet") -> full pipeline, multi-step plan
- [ ] Over-budget request -> ValidationNode returns `needs_approval` + `COST_EXCEEDED`
- [ ] Missing tool capability -> ValidationNode returns `needs_approval` + `TOOL_UNAVAILABLE`
- [ ] High-risk action -> `approval_required=true` with reason codes
- [ ] Validation internal error -> `needs_approval` + `VALIDATION_UNAVAILABLE` (fail-closed)
- [ ] Model failure in AnalysisNode -> `served_mode=LEVEL_1_DEGRADED`, confidence capped at 0.6
- [ ] Model failure in PlanningNode -> `served_mode=LEVEL_1_DEGRADED`, keyword fallback used
- [ ] Plan has `plan_id` + `plan_hash` (canonical JSON SHA-256, stable across runs for same plan)
- [ ] Each sub-goal has `action_id` for step-level idempotency
- [ ] `request_id` dedupe persists across J-Prime restarts (SQLite)
- [ ] Protocol mismatch -> `PROTOCOL_MISMATCH` error, request rejected
- [ ] Degraded mode approval is stricter than normal mode
- [ ] Shadow mode logs divergence metrics at INFO/WARN/CRITICAL levels
- [ ] All graph node tests pass on J-Prime
- [ ] MindClient.send_command() roundtrip works with mock J-Prime
- [ ] Feature flag `JARVIS_USE_REMOTE_REASONING=false` preserves existing behavior exactly

---

## 18. Non-Goals (Step 3)

- ExecutionNode migration (JARVIS Body executes plans locally)
- ReflectionNode / LearningNode migration (feedback loop comes in Step 3)
- Reactor Core training signal hardening (Step 3)
- Agent migration (GoalInference, PredictivePlanner, Coordinator) — Step 3
- Full replay pipeline with poison queue voice notification — Step 3
