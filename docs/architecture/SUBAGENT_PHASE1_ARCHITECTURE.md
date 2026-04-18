# Subagent Phase 1 — `dispatch_subagent` Venom Tool + ExploreAgent Integration

**Status:** design draft
**Author:** Derek J. Russell + Claude Opus 4.7
**Date:** 2026-04-17
**Scope:** Phase 1 of the three-phase rollout (A → B → C). This phase delivers Integration Point A (the Venom tool) using the ExploreAgent. Phases B (pipeline phases) and C (PLAN-phase upgrade) are out of scope for this document.

---

## 1. Executive Summary

Derek's Phase 1 ask is:
- A new `dispatch_subagent` tool exposed to the main generation model via Venom.
- An ExploreAgent subagent with deeply isolated, parallel context.
- `asyncio.TaskGroup` / `asyncio.gather` for concurrency (Manifesto §3).
- Highly structured JSON `SubagentResult`; subagent forbidden from mutation (Manifesto §6).

**After code review: roughly 90% of the foundation already exists.** The existing code is:

| Component | File | Lines | What it does today |
|---|---|---|---|
| `ExplorationSubagent` | `backend/core/ouroboros/governance/exploration_subagent.py` | 399 | Read-only AST/regex exploration. Produces typed `ExplorationReport` with categorized `ExplorationFinding`s. Has `request_yield()` cooperative cancellation. Already forbidden from mutation. |
| `ExplorationFleet` | `backend/core/ouroboros/governance/exploration_fleet.py` | ~350 | Parallel dispatch of `ExplorationSubagent` across per-repo scopes via `asyncio.Task`. Configurable `_MAX_AGENTS` (default 8), `_FLEET_TIMEOUT_S` (default 120s). Produces merged `FleetReport`. |
| `SubagentScheduler` + `GenerationSubagentExecutor` | `backend/core/ouroboros/governance/autonomy/subagent_scheduler.py` | 876 | APPLY-phase parallel work-unit execution under concurrency limits, barriers, dependency ordering. Not used for Phase 1 but establishes the pattern. |
| `WorkUnitSpec`, `ExecutionGraph`, `WorkUnitResult` | `backend/core/ouroboros/governance/autonomy/subagent_types.py` | 436 | L3 typed contracts (APPLY-phase). Reference for Phase 1 type design. |

**The delta Phase 1 must deliver:**

1. A **new Venom tool** (`dispatch_subagent`) in `tool_executor.py` that the main generation model can invoke mid-generation.
2. A **thin agentic wrapper** (`AgenticExploreSubagent`) that gives the existing deterministic `ExplorationSubagent` its own LLM-driven tool loop, with its own context, budget, and structured output.
3. A **SubagentResult JSON contract** for model consumption.
4. A **GoverningToolPolicy rule** auto-approving `dispatch_subagent` with read-only intent (Iron Gate).
5. **CommProtocol observability** (`SUBAGENT_SPAWN` / `SUBAGENT_RESULT` events) and **SerpentFlow rendering** (`⏺ Subagent(explore)` block).

Estimated engineering footprint: **~500–800 lines across 4–5 files**, plus tests. Much smaller than a greenfield implementation because the execution backbone already exists.

---

## 2. What Phase 1 Does NOT Touch

To minimize blast radius:

| Out-of-scope | Why |
|---|---|
| The 11-phase pipeline | No new phases. Subagents dispatch from inside GENERATE's tool loop only. |
| PLAN-phase integration | Reserved for Phase C. |
| `plan_generator.py` / `PlanAgent` | Reserved for Phase C. |
| `CodeReviewerAgent` (parallel to VALIDATE) | Reserved for Phase B. |
| `SubagentScheduler` / `GenerationSubagentExecutor` | APPLY-phase parallelism; not used here. |
| Cross-repo exploration | Phase 1 uses single-repo `ExplorationSubagent`. `ExplorationFleet`'s multi-repo capability is available but not exposed by default. |
| Dynamic subagent-type registration | Phase 1 ships with one subagent type (`explore`). Registration for `plan`, `review`, `research`, `refactor` is Phase B/C work. |

---

## 3. Design Principles (Non-Negotiable)

Per Derek's Phase 1 mandate, these are invariants — not choices.

### 3.1 Read-Only Mandate (Manifesto §6)

- The subagent's tool manifest is a **strict subset** of Venom's: `read_file`, `search_code`, `list_symbols`, `get_callers`, `glob_files`, `list_dir`, `git_log`, `git_diff`, `git_blame`.
- **No** `edit_file`, `write_file`, `delete_file`, `bash`, `run_tests`, or `ask_human` in the subagent's manifest.
- The `dispatch_subagent` tool itself is an Iron Gate-auto-allowed tool (read-only side-effect envelope).
- The subagent's backend executor raises `PermissionError` if the model attempts to call a non-manifest tool.

### 3.2 Deep Context Isolation

- The subagent runs in its own `SubagentContext` dataclass — **not** the parent `OperationContext`.
- The subagent has its own `ToolLoopCoordinator` instance, its own prompt, its own budget, its own `tool_execution_records`.
- Mid-subagent failures do **not** propagate to the parent's FSM — they are caught, classified, and returned as a structured failure result.
- The subagent's `ledger_entry` is written to the parent's ledger under the parent's `op_id` with a `::sub-<seq>` sub-op identifier (mirroring the `_apply_multi_file_candidate` sub-op convention).

### 3.3 `asyncio.TaskGroup` / `asyncio.gather` Concurrency

- When the model dispatches N subagents in parallel (`parallel_scopes` >= 2), the orchestrator uses `asyncio.TaskGroup` (Python 3.11+) or `asyncio.gather(..., return_exceptions=True)` (3.9 fallback).
- Per-subagent deadline is strictly bounded by `_FLEET_TIMEOUT_S` (default 120s). A hung subagent does not block the others.
- If any subagent raises, other subagents are allowed to complete (not cancelled) — their results are returned with `status=completed` alongside the failure.
- The parent generation's deadline is respected: if the parent's `ctx.pipeline_deadline` is closer than the subagent's timeout, the subagent deadline is clamped downward.

### 3.4 Structured JSON Result (Contract)

The `SubagentResult` schema — version `subagent.1` — is the contract between subagent and parent. See §6.

### 3.5 Cost Attribution

- Every LLM call made by the subagent is billed to the **parent op's cost ledger** with a `subagent` role tag.
- The parent's `CostGovernor` per-op cap applies to subagent+parent combined. A subagent cannot exceed the parent's remaining budget.
- If the subagent is mid-call when the parent's cap is hit, the subagent is cancelled cooperatively.

### 3.6 Observability (Manifesto §7)

Every subagent dispatch emits:

- `CommProtocol.emit_subagent_spawn(op_id, subagent_id, subagent_type, goal)` — at start.
- `CommProtocol.emit_subagent_result(op_id, subagent_id, result)` — at completion.
- A ledger entry under the parent op_id with sub-op identifier.
- A `⏺ Subagent(explore)` SerpentFlow block with per-subagent status.
- Thought log entry under the parent's `ouroboros_thoughts.jsonl`.

---

## 4. File Layout

### 4.1 New Files

| File | Purpose | Est. lines |
|---|---|---|
| `backend/core/ouroboros/governance/agentic_subagent.py` | `AgenticExploreSubagent` class — wraps `ExplorationSubagent` with an LLM-driven tool loop; owns the subagent's own provider call and tool policy. | ~250 |
| `backend/core/ouroboros/governance/subagent_contracts.py` | `SubagentType` enum, `SubagentRequest`, `SubagentResult`, `SubagentContext`, schema version constants. | ~150 |
| `backend/core/ouroboros/governance/subagent_orchestrator.py` | `SubagentOrchestrator` — parallel dispatch via `TaskGroup`, cost attribution, cancellation, result aggregation. | ~200 |
| `tests/test_ouroboros_governance/test_dispatch_subagent_tool.py` | Policy tests, tool-loop tests, parallel-dispatch tests, failure-mode tests. | ~300 |
| `tests/test_ouroboros_governance/test_agentic_explore_subagent.py` | Read-only enforcement tests, result-shape tests, cancellation tests. | ~200 |

### 4.2 Modified Files

| File | Change |
|---|---|
| `backend/core/ouroboros/governance/tool_executor.py` | Add `dispatch_subagent` to `_L1_MANIFESTS`. Add handler `_dispatch_subagent()` in `AsyncProcessToolBackend`. |
| `backend/core/ouroboros/governance/governing_tool_policy.py` | Add rule 0c: `dispatch_subagent` auto-allowed when `intent=explore` and `subagent_type=explore`. Other types / intents deferred to Phase B/C. |
| `backend/core/ouroboros/governance/comm_protocol.py` | Add `emit_subagent_spawn`, `emit_subagent_result` methods. |
| `backend/core/ouroboros/governance/providers.py` | `_build_tool_section` — add `dispatch_subagent` to the published manifest so the main generation model sees it. |
| `backend/core/ouroboros/battle_test/serpent_flow.py` | Add `⏺ Subagent(explore)` rendering for `SUBAGENT_SPAWN` / `SUBAGENT_RESULT` events. |
| `backend/core/ouroboros/governance/goal_memory_bridge.py` | Add `SUBAGENT` phase logging for thought-log integration. |
| `docs/architecture/OUROBOROS.md` | New section §"Subagents (Phase 1)" referencing this design doc. |

### 4.3 Unchanged Files (No Touch)

All of these remain untouched in Phase 1:

- `exploration_subagent.py` — re-used as-is. No modifications.
- `exploration_fleet.py` — re-used by orchestrator when `parallel_scopes` > 1. No modifications.
- `subagent_scheduler.py`, `subagent_types.py` — APPLY-phase infrastructure, not touched.
- `orchestrator.py` — the pipeline orchestrator is unchanged; subagents dispatch from within GENERATE's tool loop.

---

## 5. Core Types

### 5.1 `SubagentType` Enum

```python
class SubagentType(str, Enum):
    EXPLORE = "explore"  # Phase 1 — only type shipped
    # Reserved for Phase B/C:
    # PLAN = "plan"
    # REVIEW = "review"
    # RESEARCH = "research"
    # REFACTOR = "refactor"
```

### 5.2 `SubagentRequest` (Model → Subagent)

```python
@dataclass(frozen=True)
class SubagentRequest:
    subagent_type: SubagentType
    goal: str                              # 1–2 sentence description
    target_files: Tuple[str, ...] = ()     # optional entry files
    scope_paths: Tuple[str, ...] = ()      # optional subtree scopes for parallel fan-out
    max_files: int = 20                    # per-subagent cap
    max_depth: int = 3                     # BFS depth cap
    timeout_s: float = 120.0               # wall-clock cap
    parallel_scopes: int = 1               # 1 = single subagent; >1 = fleet dispatch
```

### 5.3 `SubagentResult` (Subagent → Model)

```python
@dataclass(frozen=True)
class SubagentResult:
    schema_version: str = "subagent.1"
    subagent_id: str = ""
    subagent_type: SubagentType = SubagentType.EXPLORE
    status: str = "completed"               # completed | failed | cancelled | partial
    goal: str = ""
    started_at_ns: int = 0
    finished_at_ns: int = 0
    findings: Tuple[SubagentFinding, ...] = ()   # typed findings
    files_read: Tuple[str, ...] = ()
    search_queries: Tuple[str, ...] = ()
    summary: str = ""                        # model-synthesized summary for prompt injection
    cost_usd: float = 0.0                    # LLM cost billed to parent
    tool_calls: int = 0                      # how many tool rounds the subagent ran
    error_class: str = ""                    # when status != completed
    error_detail: str = ""
```

### 5.4 `SubagentFinding` (structured evidence)

```python
@dataclass(frozen=True)
class SubagentFinding:
    category: str                            # import_chain | call_graph | complexity | pattern | structure | api_surface
    description: str
    file_path: str = ""
    line: int = 0
    evidence: str = ""
    relevance: float = 0.0                   # 0.0–1.0
```

The `category` values align with `ExplorationFinding`'s existing taxonomy for backward-compatibility with the deterministic exploration pipeline.

### 5.5 `SubagentContext` (runtime state)

```python
@dataclass
class SubagentContext:
    parent_op_id: str
    parent_ctx: OperationContext             # read-only reference for cost + deadline
    subagent_id: str                          # op-<parent-uuid>::sub-<seq>
    subagent_type: SubagentType
    request: SubagentRequest
    tool_loop: ToolLoopCoordinator           # isolated from parent's tool loop
    deadline: datetime                        # clamped to min(parent_deadline, request.timeout_s)
    yield_requested: bool = False             # cooperative cancellation
    cost_remaining_usd: float = 0.0
```

---

## 6. The `dispatch_subagent` Tool

### 6.1 Manifest Entry

Added to `_L1_MANIFESTS` in `tool_executor.py`:

```python
"dispatch_subagent": ToolManifest(
    name="dispatch_subagent",
    description=(
        "Spawn a read-only subagent to explore the codebase in its own context. "
        "Use this when you need to understand a large area before making changes — "
        "the subagent reads files, searches code, and returns structured findings "
        "without polluting your context. Can fan out in parallel across multiple scopes."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "subagent_type": {"type": "string", "enum": ["explore"]},
            "goal": {"type": "string", "description": "1-2 sentences on what to find"},
            "target_files": {"type": "array", "items": {"type": "string"}},
            "scope_paths": {"type": "array", "items": {"type": "string"}},
            "max_files": {"type": "integer", "default": 20},
            "max_depth": {"type": "integer", "default": 3},
            "timeout_s": {"type": "number", "default": 120.0},
            "parallel_scopes": {"type": "integer", "default": 1},
        },
        "required": ["subagent_type", "goal"],
    },
),
```

### 6.2 Policy Rule (Iron Gate)

Added to `GoverningToolPolicy` as **Rule 0c**:

```python
# Rule 0c — dispatch_subagent: auto-allowed when subagent_type is read-only class.
# Phase 1 ships with only "explore" type, which is structurally read-only.
if tool_call.name == "dispatch_subagent":
    st = tool_call.arguments.get("subagent_type", "")
    if st == "explore":
        return PolicyVerdict.allowed("tool.allowed.subagent_explore")
    return PolicyVerdict.denied(
        f"subagent_type={st!r} not yet supported (Phase 1 ships only 'explore')"
    )
```

### 6.3 Backend Execution

`AsyncProcessToolBackend._dispatch_subagent()` in `tool_executor.py`:

```python
async def _dispatch_subagent(self, ctx, tool_call) -> ToolResult:
    request = SubagentRequest.from_args(tool_call.arguments)
    orchestrator = self._subagent_orchestrator  # injected at construction
    try:
        result = await orchestrator.dispatch(parent_ctx=ctx, request=request)
    except SubagentError as e:
        return ToolResult.error(str(e), error_class=e.__class__.__name__)
    return ToolResult.success(json.dumps(result.to_dict(), indent=2))
```

### 6.4 What the Model Sees

In the generation prompt's tool section (via `_build_tool_section`):

```
### dispatch_subagent

Spawn a read-only subagent to explore the codebase in its own context. Use this when
you need to understand a large area before making changes — the subagent reads files,
searches code, and returns structured findings without polluting your context. Can fan
out in parallel across multiple scopes.

Example single dispatch:
{
  "subagent_type": "explore",
  "goal": "understand how FileWatchGuard debounces duplicate events",
  "target_files": ["backend/core/ouroboros/governance/intake/file_watch_guard.py"],
  "max_files": 15,
  "max_depth": 3
}

Example parallel fan-out:
{
  "subagent_type": "explore",
  "goal": "trace the full governance pipeline from CLASSIFY to APPLY",
  "scope_paths": [
    "backend/core/ouroboros/governance/",
    "backend/core/ouroboros/consciousness/",
    "tests/test_ouroboros_governance/"
  ],
  "parallel_scopes": 3,
  "timeout_s": 90
}
```

---

## 7. The `AgenticExploreSubagent`

### 7.1 Purpose

`AgenticExploreSubagent` is a thin agentic layer over `ExplorationSubagent`. Where `ExplorationSubagent` does AST walking and regex grep (deterministic), `AgenticExploreSubagent` gives a model its own tool loop with the existing read-only toolset — letting it reason *semantically* about what to explore next, while reusing `ExplorationSubagent`'s proven parsing infrastructure under the hood.

### 7.2 Class Sketch

```python
class AgenticExploreSubagent:
    def __init__(
        self,
        project_root: Path,
        provider: CandidateProvider,         # inherits parent's provider choice
        deterministic_fallback: ExplorationSubagent,
    ) -> None:
        self._root = project_root
        self._provider = provider
        self._deterministic = deterministic_fallback

    async def explore(
        self,
        ctx: SubagentContext,
    ) -> SubagentResult:
        """Run an LLM-driven exploration loop.

        The model is given the read-only tool manifest and a goal. It explores
        via multi-turn tool calls; each tool call is policy-gated. When the
        model emits a final answer, it is parsed into a SubagentResult.

        If the model's budget is exhausted or the provider fails, falls through
        to the deterministic ExplorationSubagent as a best-effort backup.
        """
        # 1. Build read-only tool loop with restricted manifest
        # 2. Build goal-oriented system prompt (no mutation intent; explore-only)
        # 3. Run provider.generate(ctx, deadline, tool_loop=loop)
        # 4. Parse result into SubagentResult.findings
        # 5. On failure → fall back to self._deterministic.explore(goal, entry_files)
```

### 7.3 Read-Only Tool Manifest

The `AgenticExploreSubagent` constructs its own `ToolLoopCoordinator` with a **restricted tool manifest**:

```python
_EXPLORE_READONLY_MANIFEST = frozenset({
    "read_file",
    "search_code",
    "list_symbols",
    "get_callers",
    "glob_files",
    "list_dir",
    "git_log",
    "git_diff",
    "git_blame",
})
```

Any tool call outside this set is rejected by the subagent's own policy gate (before reaching the global `GoverningToolPolicy`).

### 7.4 Deterministic Fallback

If the provider call fails (timeout, schema-invalid, budget exhausted), the subagent falls through to the existing `ExplorationSubagent.explore()` with the original goal and entry files. The fallback's `ExplorationReport` is converted into a `SubagentResult` with `status=partial`. The parent model still receives structured findings.

---

## 8. The `SubagentOrchestrator`

### 8.1 Purpose

Coordinates subagent dispatch: parallel fan-out, cost attribution, cancellation, result aggregation.

### 8.2 Class Sketch

```python
class SubagentOrchestrator:
    def __init__(
        self,
        explore_factory: Callable[[], AgenticExploreSubagent],
        ledger: OperationLedger,
        comm: CommProtocol,
    ) -> None:
        self._explore_factory = explore_factory
        self._ledger = ledger
        self._comm = comm
        self._sub_seq = 0  # monotonic subagent sequence per parent op

    async def dispatch(
        self,
        parent_ctx: OperationContext,
        request: SubagentRequest,
    ) -> SubagentResult:
        if request.parallel_scopes <= 1:
            return await self._dispatch_single(parent_ctx, request)
        return await self._dispatch_parallel(parent_ctx, request)

    async def _dispatch_single(self, parent_ctx, request) -> SubagentResult:
        sub_ctx = self._build_sub_context(parent_ctx, request, scope=None)
        self._comm.emit_subagent_spawn(parent_ctx.op_id, sub_ctx.subagent_id, request.subagent_type, request.goal)
        try:
            agent = self._explore_factory()
            result = await agent.explore(sub_ctx)
        except Exception as e:
            result = SubagentResult(status="failed", error_class=e.__class__.__name__, error_detail=str(e))
        self._comm.emit_subagent_result(parent_ctx.op_id, sub_ctx.subagent_id, result)
        self._ledger.append_subagent_record(parent_ctx.op_id, sub_ctx.subagent_id, result)
        return result

    async def _dispatch_parallel(self, parent_ctx, request) -> SubagentResult:
        # One subagent per scope_path, up to parallel_scopes.
        scopes = list(request.scope_paths)[: request.parallel_scopes]
        sub_ctxs = [self._build_sub_context(parent_ctx, request, scope=s) for s in scopes]
        for sc in sub_ctxs:
            self._comm.emit_subagent_spawn(parent_ctx.op_id, sc.subagent_id, request.subagent_type, request.goal)

        async def _run_one(sc):
            agent = self._explore_factory()
            try:
                return await agent.explore(sc)
            except Exception as e:
                return SubagentResult(status="failed", error_class=e.__class__.__name__, error_detail=str(e))

        # asyncio.TaskGroup (3.11+) or gather fallback (3.9+).
        if hasattr(asyncio, "TaskGroup"):
            async with asyncio.TaskGroup() as tg:
                tasks = [tg.create_task(_run_one(sc)) for sc in sub_ctxs]
            results = [t.result() for t in tasks]
        else:
            results = await asyncio.gather(*(_run_one(sc) for sc in sub_ctxs), return_exceptions=False)

        for sc, r in zip(sub_ctxs, results):
            self._comm.emit_subagent_result(parent_ctx.op_id, sc.subagent_id, r)
            self._ledger.append_subagent_record(parent_ctx.op_id, sc.subagent_id, r)

        # Merge into single SubagentResult with aggregated findings.
        return self._merge_results(request, results)
```

### 8.3 Cost Attribution

The orchestrator consults `parent_ctx.cost_remaining_usd` before spawning. If the parent's budget is already over, the dispatch fails fast with `status=failed, error_class=ParentBudgetExhausted`. Each subagent's LLM call reduces the parent's remaining budget atomically.

### 8.4 Cancellation

If the parent's `ctx.pipeline_deadline` is reached while subagents are running, the orchestrator calls `request_yield()` on each subagent's deterministic fallback (cooperative) and cancels the active tool-loop tasks (asyncio cancellation). Partial results are returned with `status=cancelled`.

---

## 9. Integration with Venom

The integration point is minimal — one addition to `_L1_MANIFESTS`, one handler in `AsyncProcessToolBackend`, one new policy rule.

### 9.1 `ToolLoopCoordinator` Construction

The existing `ToolLoopCoordinator` is constructed with an `AsyncProcessToolBackend`. In Phase 1, the backend receives a `SubagentOrchestrator` at construction time:

```python
# In governed_loop_service.py (boot):
subagent_orch = SubagentOrchestrator(
    explore_factory=lambda: AgenticExploreSubagent(
        project_root=self._project_root,
        provider=self._default_provider,
        deterministic_fallback=ExplorationSubagent(self._project_root),
    ),
    ledger=self._ledger,
    comm=self._comm,
)
backend = AsyncProcessToolBackend(
    project_root=self._project_root,
    subagent_orchestrator=subagent_orch,  # NEW
    # ... existing kwargs
)
```

### 9.2 Prompt Injection

`providers.py:_build_tool_section` gets `dispatch_subagent` added to the standard tool manifest. The model sees it alongside the existing 15 built-in tools and any MCP tools.

### 9.3 No Phase Changes

The pipeline's 11 phases are unchanged. Subagents dispatch from within GENERATE's tool loop. The orchestrator's FSM has no new transitions. This is the minimal-blast-radius design choice.

---

## 10. Observability

### 10.1 CommProtocol Events

Two new events:

```python
@dataclass(frozen=True)
class SubagentSpawnEvent:
    op_id: str
    subagent_id: str
    subagent_type: str
    goal: str
    dispatched_at_ns: int

@dataclass(frozen=True)
class SubagentResultEvent:
    op_id: str
    subagent_id: str
    result: SubagentResult
    completed_at_ns: int
```

These flow through the existing heartbeat infrastructure and are consumed by SerpentFlow + the voice narrator (optional).

### 10.2 Ledger Entry

Under the parent's `op_id`:

```
{
  "op_id": "op-019e...-cau",
  "state": "SUBAGENT_COMPLETED",
  "data": {
    "subagent_id": "op-019e...-cau::sub-01",
    "subagent_type": "explore",
    "status": "completed",
    "cost_usd": 0.012,
    "findings_count": 17,
    "files_read": 8,
    "duration_s": 34.2
  },
  "timestamp": 1776199368.349634,
  "wall_time": 1776199368.349634
}
```

### 10.3 SerpentFlow Rendering

```
┌ op-019e... CLASSIFY complex multi_file
├── 🗺️  planning
├── 🧠 generating (claude-api, 15.3s)
│   ⏺ Subagent(explore) op-019e...::sub-01 [parallel_scopes=3]
│     ├── scope=backend/core/ouroboros/governance/
│     ├── scope=backend/core/ouroboros/consciousness/
│     └── scope=tests/test_ouroboros_governance/
│     🧬 all 3 completed in 28.4s, 47 findings, $0.031
│   ⏺ Update(backend/...py)  +42 -7
└── 🛡️  verify pass · ✅ complete
```

### 10.4 Thought Log

A new phase `SUBAGENT` added to `goal_memory_bridge.py`:

```jsonl
{"ts":"...", "op_id":"...", "phase":"SUBAGENT", "subagent_id":"...::sub-01", "action":"spawn", "type":"explore", "goal":"..."}
{"ts":"...", "op_id":"...", "phase":"SUBAGENT", "subagent_id":"...::sub-01", "action":"result", "status":"completed", "findings":17, "cost":0.012}
```

---

## 11. Test Strategy

### 11.1 Unit Tests

`tests/test_ouroboros_governance/test_agentic_explore_subagent.py`:

- **Read-only enforcement**: simulate the subagent model trying to call `edit_file`, `bash`, `write_file`. Verify each is denied by the subagent's internal policy.
- **Result shape**: run against a fixture project; verify `SubagentResult.schema_version == "subagent.1"`, `findings` are all `SubagentFinding` instances, `status` is correct.
- **Deterministic fallback**: mock the provider to raise; verify the deterministic `ExplorationSubagent` fallback path produces a `status=partial` result.
- **Cooperative cancellation**: call `ctx.request_yield()` mid-loop; verify the subagent exits cleanly within one tool-round boundary.

### 11.2 Integration Tests

`tests/test_ouroboros_governance/test_dispatch_subagent_tool.py`:

- **Tool policy**: `dispatch_subagent` with `subagent_type=explore` → allowed. With `subagent_type=plan` → denied with specific reason.
- **Single dispatch**: model calls `dispatch_subagent` mid-generation; verify subagent runs, returns a structured result, and the result is injected as the tool result back to the model.
- **Parallel dispatch**: `parallel_scopes=3` → verify 3 subagents run concurrently (observed via start-timestamp spread), results are merged, total duration is bounded by max per-subagent duration (not sum).
- **Cost attribution**: verify parent op's cost ledger includes subagent cost. Verify parent budget cap triggers early termination.
- **Observability**: verify `SUBAGENT_SPAWN` and `SUBAGENT_RESULT` CommProtocol events are emitted. Verify ledger entries exist. Verify SerpentFlow renders (via capture).

### 11.3 Contract Tests

- **`SubagentResult` JSON round-trip**: serialize to JSON, parse back, verify equality.
- **`SubagentFinding` category enum**: verify each category value renders correctly in the prompt-injection format.

### 11.4 Battle-Test Validation

Add one battle-test scenario:

- Sensor detects a multi-module refactor task.
- Model dispatches 3 parallel explore subagents across `backend/core/`, `backend/vision/`, `tests/`.
- Verify the generation completes with richer context, observed in the SerpentFlow trace.

This is the Manifesto §6 *threshold-triggered neuroplasticity* test. Three consecutive successful runs → Phase 1 capability graduates from "data point" to "capability."

---

## 12. Exit Criteria (When Phase 1 Is Done)

- [ ] `dispatch_subagent` tool appears in the generation prompt's tool manifest.
- [ ] Model can call `dispatch_subagent` and receive a `SubagentResult` JSON.
- [ ] Parallel dispatch works under `asyncio.TaskGroup` (3.11+) with `gather` fallback (3.9+).
- [ ] Read-only enforcement: every non-manifest tool call is denied structurally.
- [ ] Cost attribution to parent op is visible in `cost_tracker.json`.
- [ ] CommProtocol `SUBAGENT_SPAWN` / `SUBAGENT_RESULT` events flow through SerpentFlow.
- [ ] Thought log records `SUBAGENT` phase entries.
- [ ] All unit tests green. All integration tests green. All contract tests green.
- [ ] One battle-test session shows end-to-end subagent dispatch.
- [ ] `docs/architecture/OUROBOROS.md` has a new `§"Subagents (Phase 1)"` section referencing this doc.
- [ ] Commit message conventional-style: `feat(subagents): Phase 1 — dispatch_subagent Venom tool + AgenticExploreSubagent`.

---

## 13. Phased Rollout Safety (Manifesto §6 Iron Gate)

Per Derek's mandate, Phase 1 must not break `semantic_guardian_demo.py` at any step. The rollout order within Phase 1 is:

1. **Land types + orchestrator scaffolding** (`subagent_contracts.py`, `subagent_orchestrator.py`) behind a master switch `JARVIS_SUBAGENT_DISPATCH_ENABLED=false`. No behavior change.
2. **Land the `AgenticExploreSubagent` class** (`agentic_subagent.py`). Still gated off.
3. **Land the Venom tool registration** (`tool_executor.py`, `providers.py`) gated by the master switch. Flip the switch only in a test env to validate end-to-end.
4. **Land the policy rule + observability** (`governing_tool_policy.py`, `comm_protocol.py`, `serpent_flow.py`, `goal_memory_bridge.py`).
5. **Battle-test with master switch on.** Three consecutive clean runs → flip the default to `true` in a final commit.
6. **Update `OUROBOROS.md`** with Phase 1 documentation.

Each of steps 1–5 is a separate commit. Step 6 is the doc update commit. Six commits total for Phase 1.

Master switch: `JARVIS_SUBAGENT_DISPATCH_ENABLED` (default `false` until battle-test graduation).

---

## 14. Open Design Questions (Flag to Derek Before Coding)

1. **Provider choice for the subagent.** The subagent inherits the parent's provider by default (Claude if the parent is on Claude, DW if on DW). Alternative: always use Claude for subagents (better reasoning). **Recommendation: inherit parent; revisit in Phase B.**

2. **Subagent prompt template.** Needs drafting. Claude Code's Explore subagent uses a specific persona — we should write ours. **Recommendation: draft in the next commit along with the `AgenticExploreSubagent` class.**

3. **`parallel_scopes` hard cap.** `_FLEET_MAX_AGENTS` is 8 today. The `dispatch_subagent` tool caps `parallel_scopes` at the same value (8). **Recommendation: enforce in the tool schema validator.**

4. **Per-subagent max tool rounds.** Inherit from `JARVIS_GOVERNED_TOOL_MAX_ROUNDS` (default 5)? Or allow longer for exploration? **Recommendation: new env `JARVIS_SUBAGENT_MAX_ROUNDS` (default 8).**

5. **Result size cap.** A subagent could return 500 findings, blowing up the parent's prompt. Cap `findings` at 50 (top-relevance) in the `SubagentResult.to_dict()` serializer? **Recommendation: yes, cap at 50 findings; store full report in ledger for audit.**

6. **Subagent-within-subagent?** For Phase 1, explicitly **disallow** nested dispatch. The `dispatch_subagent` tool is NOT in the subagent's manifest. Revisit in Phase B/C.

---

## 15. What This Doc Is NOT

- Not a code implementation. No line of production code is written until Derek green-lights this design.
- Not a promise of scope beyond Phase 1. Phases B and C have their own design docs (TBD).
- Not a change to the pipeline's FSM. The 11 phases are untouched.

---

**Ready for Derek's review.** On approval, I'll proceed with step 1 of the phased rollout (land types + orchestrator scaffolding behind master switch, no behavior change).
