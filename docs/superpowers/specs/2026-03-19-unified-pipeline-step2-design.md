# Unified Thinking Pipeline — Step 2: LangGraph Reasoning on J-Prime

> **Status**: Approved (revised)
> **Date**: 2026-03-19
> **Revision**: 2 — addresses 7 required fixes from review
> **Scope**: J-Prime (Mind) + JARVIS (Body)
> **Parent spec**: `docs/superpowers/specs/2026-03-19-unified-thinking-pipeline-design.md`
> **Depends on**: Step 0+1 complete (protocol schemas, brain selector, MindClient, health endpoints)

---

## 1. Goal

Ship `POST /v1/reason` — the canonical Mind API. JARVIS sends a command, J-Prime reasons through it (classify, decompose, validate, assemble plan), and returns a full `ReasonResponse` that JARVIS executes with LangChain tools.

This is the first time J-Prime **thinks**, not just selects brains.

---

## 2. Graph Structure

**Fix 1**: Validation runs on EVERY executable plan path — including trivial/light. Trivial/light commands skip full PlanningNode but still pass through LightValidation before plan assembly.

```
POST /v1/reason receives ReasonRequest
       |
       v
  [Protocol Gate] — reject PROTOCOL_MISMATCH before any work
       |
       v
  [Idempotency Check] — return cached response if request_id seen
       |
       v
  AnalysisNode (always runs)
    - UnifiedBrainSelector picks brain (phi3 or qwen_7b)
    - Calls GPU model via ModelProvider (off-thread, with timeout)
    - On model failure: switch to LEVEL_1_DEGRADED, cap confidence at 0.6
    - Output: intent, complexity, confidence, inferred_goals
       |
       | route_by_depth(complexity)
       |
  trivial/light ----+                    heavy/complex
       |            |                         |
       |            |                         v
       |            |                    PlanningNode
       |            |                      - Calls GPU model for sub-goal decomposition
       |            |                      - On failure: LEVEL_1_DEGRADED + keyword expansion
       |            |                      - Output: sub_goals, dependencies, strategy
       |            |                         |
       |            |                         v
       |            |                    FullValidationNode
       |            |                      - Cost gate + resource gate + approval gate
       |            |                      - FAIL-CLOSED on any error
       |            |                         |
       v            v                         v
  LightValidationNode                         |
    - Same 3 gates as Full but on a           |
      single-step plan (the trivial action)   |
    - FAIL-CLOSED on any error                |
       |                                      |
       +------------------+-------------------+
                          |
                          v
                   ExecutionPlanner (always runs)
                     - Per sub-goal: assign brain + tool
                     - Build Plan with plan_id + plan_hash
                     - Assemble full ReasonResponse
```

**LightValidationNode** applies the same three gates (cost, resource, approval) to single-step plans. It is a thin call to the same validation logic — not a separate implementation. The invariant is: **no plan reaches ExecutionPlanner without passing validation**.

---

## 3. Explicit Model Failure Policy

**No silent fallbacks.** When a GPU model call fails inside any node:

1. Set `served_mode = "LEVEL_1_DEGRADED"` on the graph state
2. Set `degraded_reason_code` to the specific failure (`ANALYSIS_MODEL_UNAVAILABLE`, `ANALYSIS_MODEL_TIMEOUT`, `PLANNING_MODEL_UNAVAILABLE`, `PLANNING_MODEL_TIMEOUT`)
3. Cap confidence at 0.6 (degraded results are never high-confidence)
4. Use pattern-matching/keyword fallback for the failed node only
5. Subsequent nodes see the degraded flag and adjust behavior (stricter approval)
6. The response is honest — JARVIS knows it got a degraded result

```python
# Inside AnalysisNode
async def _call_model_or_fallback(self, state: ReasoningGraphState) -> Tuple[dict, str]:
    try:
        result = await self._model_provider.infer(prompt, brain_selection, timeout_s=10.0)
        return result, "LEVEL_0_PRIMARY"
    except Exception as exc:
        logger.warning("[AnalysisNode] Model call failed: %s — using pattern fallback", exc)
        result = self._pattern_fallback(state)
        result["confidence"] = min(result.get("confidence", 0.5), 0.6)
        return result, "LEVEL_1_DEGRADED"
```

---

## 4. Fail-Closed Validation

ValidationNode is **authoritative and fail-closed**. Three gates, all deterministic. The same logic serves both `FullValidationNode` and `LightValidationNode` — the difference is input (multi-step plan vs single-step plan), not logic.

### 4.1 Cost Gate

```
estimated_plan_cost = sum(brain_cost_estimate(step.brain_assigned) for step in sub_goals)
if daily_spend + estimated_plan_cost > hard_cost_cap_usd:
    -> approval_required=True, reason="COST_EXCEEDED"
if daily_spend + estimated_plan_cost > soft_cost_target_usd:
    -> approval_required=False, routing_trace.cost_gate_passed=False (advisory)
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

Enforced via try/except around each gate. The outer handler catches any unhandled exception and returns `VALIDATION_UNAVAILABLE`.

---

## 5. Plan Hash — Fully Canonical

`plan_hash` is **full SHA-256** (64 hex characters) of a canonical JSON representation.

**Why full hash, not truncated**: A 16-char (64-bit) truncation has birthday collision probability of ~1 in 2^32 (~4 billion). With high-throughput plan generation, this is insufficient for integrity verification. Full SHA-256 (256-bit) has collision probability negligible for any realistic workload.

**Canonicalization rules** (all enforced in code):

```python
import hashlib
import json
import math

def _normalize_value(v):
    """Normalize a value for canonical hashing."""
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return str(v)  # "nan", "inf", "-inf"
        # Normalize to 6 decimal places, strip trailing zeros
        return f"{v:.6f}".rstrip('0').rstrip('.')
    if isinstance(v, dict):
        return {k: _normalize_value(val) for k, val in sorted(v.items())}
    if isinstance(v, (list, tuple)):
        return [_normalize_value(item) for item in v]
    return v

def compute_plan_hash(plan: Plan) -> str:
    """Full SHA-256 of canonical JSON plan representation.

    Canonicalization:
    - Keys sorted alphabetically at every nesting level
    - No whitespace (separators=(',', ':'))
    - Floats normalized to 6 decimal places, trailing zeros stripped
    - NaN/Inf serialized as strings
    - Lists preserve order (sub_goals are ordered)
    - depends_on lists sorted alphabetically
    - UTF-8 encoding
    - Includes: sub_goals, execution_strategy, approval_required
    - Excludes: plan_id (circular), timestamps (non-deterministic)
    """
    hashable = _normalize_value({
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
    })
    canonical = json.dumps(hashable, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()  # full 64-char hex
```

JARVIS Body echoes `plan_id` + `plan_hash` in `ReasonFeedback`. J-Prime verifies the hash matches before processing feedback. Mismatch → reject with `PLAN_HASH_MISMATCH` error.

---

## 6. Protocol Negotiation (Hard-Gated)

`POST /v1/reason` rejects incompatible protocol versions before any processing:

```python
async def handle_reason(req: ReasonRequest) -> dict:
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
```

---

## 7. Idempotency Persistence

`request_id` dedupe must survive process restarts.

**Implementation**: SQLite at `~/.jarvis-prime/reasoning/idempotency.db`

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
- On request: check `request_id` → return cached response
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
| CRITICAL | Approval decision or sub-goal count differs by >1 | Local says safe, remote says needs_approval |

### 8.3 Cutover Gates (hard thresholds)

```
CRITICAL divergence rate:  <= 1% over 1000 requests (or 72h, whichever first)
WARN divergence rate:      <= 5% over same window
Latency regression:        p99 <= 2x current p99 for same complexity tier
Error rate:                <= 0.5% of requests return error status
```

All four gates must pass simultaneously. If any regresses after cutover, automatic rollback to shadow mode.

---

## 9. Replay Semantics (Step 2 Scope)

For tasks queued during Level 2 (reflex-only mode). **Step 2 covers queue + replay mechanics only. Step 3 adds voice notification and observability dashboard.**

### 9.1 Queue Ordering

Tasks replayed in order of `original_enqueued_at` within each `session_id`. Cross-session ordering by `original_enqueued_at`.

### 9.2 Dedupe

Before replay, check `request_id` against idempotency store. If already processed, skip.

### 9.3 Max Replay Attempts

Each deferred task has `max_replay_attempts=3` (configurable). Counter incremented on each attempt.

### 9.4 Poison Task Handling (Step 2)

When a task exceeds max replay attempts:
- Move to poison queue file (`~/.jarvis/mind/poison_tasks.json`)
- Log with full context (command, original error, all attempt results)
- Emit CRITICAL telemetry event
- Never automatically retried

**NOT in Step 2** (deferred to Step 3):
- Voice notification ("I wasn't able to complete [task]...")
- Cross-session replay orchestration
- Replay success rate monitoring in observability dashboard

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

---

## 12. Model Provider Interface (Dependency Injection)

**Fix 6**: Nodes do NOT import from `jarvis_prime.server`. Instead, they accept a `ModelProvider` protocol via constructor injection. This enables testing with mock providers and decouples reasoning from the server lifecycle.

```python
from typing import Protocol, runtime_checkable

@runtime_checkable
class ModelProvider(Protocol):
    """Interface for GPU model inference. Injected into graph nodes."""

    async def infer(
        self,
        messages: list,
        max_tokens: int = 1024,
        temperature: float = 0.2,
        timeout_s: float = 10.0,
    ) -> dict:
        """Run inference and return {"content": "..."}. Raises on failure."""
        ...

    def is_model_loaded(self) -> bool:
        """Check if a model is currently loaded and ready."""
        ...

    def loaded_model_name(self) -> str:
        """Return the name of the currently loaded model, or "" if none."""
        ...
```

**Production implementation** (`LlamaCppModelProvider`):

```python
class LlamaCppModelProvider:
    """Wraps the llama-cpp-python Llama instance for graph node inference."""

    def __init__(self, get_model_fn: Callable[[], Optional[Any]]):
        """
        Args:
            get_model_fn: callable returning the loaded Llama instance (or None).
                          In production: lambda: _startup_state.get_model()
        """
        self._get_model = get_model_fn

    async def infer(self, messages, max_tokens=1024, temperature=0.2, timeout_s=10.0):
        model = self._get_model()
        if model is None:
            raise RuntimeError("No model loaded")

        # Run synchronous llama-cpp inference OFF the event loop
        loop = asyncio.get_running_loop()
        response = await asyncio.wait_for(
            loop.run_in_executor(
                None,  # default ThreadPoolExecutor
                functools.partial(
                    model.create_chat_completion,
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                ),
            ),
            timeout=timeout_s,
        )
        return {"content": response["choices"][0]["message"]["content"]}

    def is_model_loaded(self) -> bool:
        return self._get_model() is not None

    def loaded_model_name(self) -> str:
        model = self._get_model()
        if model is None:
            return ""
        return getattr(model, "model_path", "unknown")
```

**Key properties**:
- **Off-thread**: `run_in_executor(None, ...)` moves the synchronous `create_chat_completion` to the default `ThreadPoolExecutor`, preventing event loop blocking.
- **Timeout**: `asyncio.wait_for(..., timeout=timeout_s)` cancels the executor future if inference hangs. Default 10s per node call.
- **Cancellation**: On `asyncio.TimeoutError`, the node catches it and triggers model failure policy (Section 3). The executor thread may continue running but the graph does not wait for it.
- **Testability**: Tests inject a `MockModelProvider` that returns canned responses without GPU.

**Test mock**:

```python
class MockModelProvider:
    def __init__(self, response: str = "mock response", should_fail: bool = False):
        self._response = response
        self._should_fail = should_fail

    async def infer(self, messages, max_tokens=1024, temperature=0.2, timeout_s=10.0):
        if self._should_fail:
            raise RuntimeError("Mock model failure")
        return {"content": self._response}

    def is_model_loaded(self) -> bool:
        return not self._should_fail

    def loaded_model_name(self) -> str:
        return "mock-model"
```

**Wiring** (in `endpoints.py`):

```python
# Production: inject real model provider at endpoint level
from jarvis_prime.reasoning.model_provider import LlamaCppModelProvider

def _get_model_provider() -> ModelProvider:
    from jarvis_prime.server import _startup_state
    return LlamaCppModelProvider(get_model_fn=lambda: _startup_state.get_model())
```

Only `endpoints.py` touches the server module. Graph nodes never import from server.

---

## 13. Cost Estimation

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

GCP compute cost (GPU time) included in estimates. Claude API fallback costs tracked separately via `daily_spend_claude`.

---

## 14. JARVIS-Side Wiring

### 14.1 MindClient.send_command()

```python
async def send_command(
    self,
    command: str,
    context: Optional[Dict[str, Any]] = None,
    deadline_ms: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Send command to J-Prime for full reasoning.

    Returns ReasonResponse dict with plan, or None on failure.
    Level 2: returns None (queue task + reflex only).
    Level 1: returns None (triggers JARVIS-local Claude emergency planner).
    Level 0: full J-Prime reasoning.
    """
```

### 14.2 Command Processor Wiring

**Fix 3**: The fallback path does NOT depend on J-Prime. When `send_command()` returns None:

```
1. Reflex check (unchanged, always local)
2. If no reflex: mind_client.send_command(command, context)
3. If response is not None and response.status == "plan_ready":
   a. Execute plan sub-goals with LangChain tools
   b. mind_client.send_feedback(results)
4. If response is not None and response.status == "needs_approval":
   a. VoiceApprovalManager asks Derek
   b. If approved: execute plan
   c. If denied: discard plan, inform user
5. If response is None (Mind unavailable):
   a. Level 1: JARVIS-local emergency planner via Claude API
      - Reduced analysis (Claude classifies intent)
      - Single-step plan only (no multi-step decomposition)
      - LightValidation gates still apply
      - Response tagged LEVEL_1_DEGRADED
      - Capped at $0.50/session Claude spend
   b. Level 2: reflex commands only, queue rest for replay
   c. NEVER falls back to "existing J-Prime classify" — that path
      requires J-Prime, which is the thing that's unavailable
```

This matches the parent spec's tiered fallback model (Section 7).

---

## 15. File Structure

### J-Prime (new files)

```
jarvis_prime/reasoning/
  model_provider.py         # ModelProvider protocol + LlamaCppModelProvider
  graph_nodes/
    __init__.py
    analysis_node.py        # Intent classification via ModelProvider
    planning_node.py        # Sub-goal decomposition via ModelProvider
    validation_node.py      # Rule-based cost/resource/approval gates (shared logic)
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

## 16. Acceptance Criteria

- [ ] Trivial command ("open Safari") → Analysis → **LightValidation** → ExecutionPlanner, plan in <500ms
- [ ] Complex command ("research competitors, build spreadsheet") → full pipeline, multi-step plan
- [ ] Over-budget request → ValidationNode returns `needs_approval` + `COST_EXCEEDED`
- [ ] Missing tool capability → `needs_approval` + `TOOL_UNAVAILABLE`
- [ ] High-risk action → `approval_required=true` with reason codes
- [ ] Validation internal error → `needs_approval` + `VALIDATION_UNAVAILABLE` (fail-closed)
- [ ] **Trivial command with high-risk action** → LightValidation catches it, `needs_approval`
- [ ] Model failure in AnalysisNode → `LEVEL_1_DEGRADED`, confidence capped at 0.6
- [ ] Model failure in PlanningNode → `LEVEL_1_DEGRADED`, keyword fallback used
- [ ] GPU inference runs off event loop (ThreadPoolExecutor), with 10s timeout
- [ ] Model timeout → `asyncio.TimeoutError` caught, triggers degraded path
- [ ] Plan has `plan_id` + `plan_hash` (full SHA-256, canonical JSON, deterministic)
- [ ] Same plan content produces identical hash across runs (canonicalization test)
- [ ] Each sub-goal has `action_id` for step-level idempotency
- [ ] `request_id` dedupe persists across J-Prime restarts (SQLite)
- [ ] Protocol mismatch → `PROTOCOL_MISMATCH` error, request rejected
- [ ] Degraded mode approval is stricter than normal mode
- [ ] Shadow mode logs divergence at INFO/WARN/CRITICAL levels
- [ ] Graph nodes accept MockModelProvider in tests (no server import)
- [ ] MindClient.send_command() returns None when J-Prime unavailable
- [ ] **JARVIS fallback uses Claude API (not J-Prime)** when Mind unavailable
- [ ] Feature flag `JARVIS_USE_REMOTE_REASONING=false` preserves existing behavior exactly

---

## 17. Non-Goals (Step 3)

- ExecutionNode migration (JARVIS Body executes plans locally)
- ReflectionNode / LearningNode migration (feedback loop)
- Reactor Core training signal hardening
- Agent migration (GoalInference, PredictivePlanner, Coordinator)
- Poison task voice notification ("I wasn't able to complete...")
- Cross-session replay orchestration
- Replay success rate monitoring in observability dashboard
