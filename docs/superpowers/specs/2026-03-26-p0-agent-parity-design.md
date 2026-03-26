# P0 Agent Parity Design — Closing 4 Claude Code Gaps

**Date:** 2026-03-26
**Status:** Approved
**Scope:** Ouroboros governance pipeline agent infrastructure

## Problem

Ouroboros has 4 built-but-disconnected modules that, when wired, close the critical
gaps between Ouroboros agents and Claude Code agents:

1. **Agent Teams** — `agent_team.py` exists but isn't called from the pipeline
2. **Background Agents** — operations block the caller; no async submission
3. **LSP Integration** — `lsp_checker.py` exists but isn't in the VALIDATE phase
4. **Edit→Fix Tight Loop** — `interactive_repair.py` exists but isn't in the retry path

## Design

### Gap 1: Hybrid Teammate Executor

**Boundary Principle:** Deterministic risk classification → agentic execution.

**Architecture:**

```
HybridTeammateExecutor
├── evaluate_risk(work_unit) → WorkUnitRiskProfile   [DETERMINISTIC]
│   ├── Signals: phase, role, target_files, operation_type
│   └── Decision: requires_execution OR mutates_state → subprocess
│
├── CoroutineRunner (Cognitive Default)              [LIGHTWEIGHT]
│   ├── asyncio.Task in same event loop
│   ├── Shares SharedFindingsBus + read-only tools
│   ├── Roles: RESEARCHER, REVIEWER, context trackers
│   └── No filesystem mutation, no subprocess execution
│
└── SubprocessRunner (Immune Isolation)              [CRASH-SAFE]
    ├── asyncio.create_subprocess_exec("python3", "-m", "...worker")
    ├── IPC: JSON lines over stdin/stdout pipes
    ├── Timeout: JARVIS_SUBPROCESS_AGENT_TIMEOUT_S (default 120s)
    ├── Crash containment: parent survives child crash
    └── Memory guard: MemoryBudgetGuard.can_spawn() before launch
```

**Risk Evaluation Rules (deterministic, no model inference):**

| Signal | Value | Classification |
|--------|-------|---------------|
| TeammateRole | RESEARCHER, REVIEWER | Always coroutine |
| TeammateRole | WORKER + phase=APPLY | Always subprocess |
| operation_type | code_generation, test_execution | requires_execution=True |
| target includes | .py, .rs, .go, .js files + GENERATE/APPLY | mutates_state=True |
| Any | requires_execution OR mutates_state | → subprocess |
| Default | everything else | → coroutine |

**IPC Protocol (subprocess):**

```json
// Parent → Child (stdin, one JSON line)
{"op_id": "...", "goal": "...", "target_files": [...], "work_unit": {...}}

// Child → Parent (stdout, JSON lines as they arrive)
{"type": "finding", "data": {...}}
{"type": "progress", "percent": 45, "message": "running tests"}
{"type": "result", "success": true, "patches": [...], "validation": {...}}

// Child exits with code 0 (success) or 1 (failure)
```

**New files:**
- `backend/core/ouroboros/governance/hybrid_teammate_executor.py` — executor + risk evaluator
- `backend/core/ouroboros/governance/isolated_agent_worker.py` — subprocess entry point

**Wire into:** `subagent_scheduler.py` `_run_selected_units()` method

### Gap 2: Background Agent Pool

**Architecture:**

```
BackgroundAgentPool
├── submit(op_context, background=True) → op_id    [NON-BLOCKING]
├── get_result(op_id) → Optional[OperationContext]  [POLL]
├── cancel(op_id) → bool                           [KILL]
├── list_active() → List[BackgroundOp]              [STATUS]
│
├── _op_queue: asyncio.Queue (bounded, max 10)
├── _workers: asyncio.Task pool (size = JARVIS_BG_POOL_SIZE, default 2)
├── _results: Dict[str, OperationResult]
├── _callbacks: Dict[str, Callable]  (optional completion callback)
└── _worker_loop(): dequeue → run orchestrator → store result → fire callback
```

**New file:** `backend/core/ouroboros/governance/background_agent_pool.py`

**Wire into:** `governed_loop_service.py` — add `submit_background()` method that enqueues
instead of awaiting. Sensors can submit with `background=True` for non-blocking processing.

### Gap 3: LSP Integration in VALIDATE

**Enhanced VALIDATE phase:**

```
Current:  AST preflight → Test execution → Pass/Fail
Enhanced: AST preflight → LSP type check → Test execution → Pass/Fail
                           │
                           └→ Errors injected into ctx for InteractiveRepair
```

**Enhancements to `lsp_checker.py`:**
- `check_incremental(changed_files)` — only checks modified files
- Error results formatted for micro-prompt injection
- Timeout resilience: failures degrade gracefully (skip, don't block)

**Wire into:** `orchestrator.py` VALIDATE phase, after AST preflight, before test execution.
LSP errors become first-class validation failures that feed into the repair loop.

### Gap 4: Edit→Observe→Fix Tight Loop

**Enhanced retry path:**

```
Current:  VALIDATE fails → full GENERATE_RETRY (expensive: re-prompt full model)

Enhanced: VALIDATE fails
          ├── LSP errors? → InteractiveRepairLoop.repair(micro-prompt) [FAST]
          │   ├── Success after ≤3 micro-iterations → Continue to GATE
          │   └── Fail → Fall through to GENERATE_RETRY
          │
          └── Test failures? → InteractiveRepairLoop.repair(test-error) [FAST]
              ├── Success → Continue to GATE
              └── Fail → GENERATE_RETRY (full regeneration)

Cost: Micro-fix ≈ 500 tokens. Full regeneration ≈ 5,000-50,000 tokens.
      10-100x cost savings per successful micro-fix.
```

**Parallel candidate validation:**
Replace sequential `for candidate in candidates` with `asyncio.gather(*[validate(c) for c in candidates])`.
First passing candidate wins; cancel remaining.

**Wire into:** `orchestrator.py` between VALIDATE failure and GENERATE_RETRY advance.

## Files Modified

| File | Change |
|------|--------|
| `hybrid_teammate_executor.py` | **NEW** — risk evaluator + hybrid runner |
| `isolated_agent_worker.py` | **NEW** — subprocess entry point |
| `background_agent_pool.py` | **NEW** — async operation pool |
| `lsp_checker.py` | **ENHANCE** — incremental mode |
| `orchestrator.py` | **WIRE** — LSP in VALIDATE, repair in retry, parallel validation |
| `governed_loop_service.py` | **WIRE** — background pool, hybrid executor |
| `subagent_scheduler.py` | **WIRE** — hybrid executor in _run_selected_units() |

## Success Criteria

1. `HybridTeammateExecutor.evaluate_risk()` correctly routes: RESEARCHER→coroutine, WORKER+APPLY→subprocess
2. Subprocess agent crash does NOT crash unified_supervisor
3. `submit_background()` returns immediately; result available via `get_result()` later
4. LSP type errors caught in VALIDATE before test execution
5. Micro-fix resolves simple type errors without full regeneration
6. Parallel candidate validation runs N candidates concurrently

## Boundary Principle Compliance

- **Deterministic:** Risk evaluation, IPC protocol, LSP subprocess invocation, parallel gather
- **Agentic:** Code generation within micro-fix loop, exploration within coroutine runners
