# Phase 2A: Wire TestRunner into VALIDATE Phase — Design

**Date:** 2026-03-08
**Status:** Approved
**Scope:** Safety-first — no multi-repo, no approval IPC.

---

## Goal

Wire deterministic test verification into the governed pipeline's VALIDATE phase so that no generated candidate can reach APPLY without passing the language-appropriate test suite.

---

## Architecture Overview

**Four files touched (minimal diff):**

| File | Change |
|------|--------|
| `op_context.py` | (1) Extend `ValidationResult` with compact provenance fields; (2) Add `VALIDATE → POSTMORTEM` to `PHASE_TRANSITIONS` |
| `orchestrator.py` | (1) Add `validation_runner: Any` parameter to constructor; (2) Replace sync `_validate_candidates()` with async `_run_validation()`; (3) Thread `pipeline_deadline` through VALIDATE budget |
| `governed_loop_service.py` | (1) Stamp `pipeline_deadline` once at `submit()`; (2) Wire `LanguageRouter` instance into `GovernedOrchestrator` via `_build_components()` |
| `test_runner.py` | No changes — `LanguageRouter`, `PythonAdapter`, `CppAdapter` already exist from Phase 1.5 |

**Zero new modules.** All existing abstractions are extended in place.

---

## Component: ValidationRunner Protocol

To keep `GovernedOrchestrator` testable, inject the router via constructor — not imported globally:

```python
# orchestrator.py — constructor addition
def __init__(
    self,
    stack: Any,
    generator: Any,
    approval_provider: Any,
    config: OrchestratorConfig,
    validation_runner: Any = None,  # LanguageRouter | duck-typed
) -> None:
    ...
    self._validation_runner = validation_runner
```

In tests, a mock with `.run(changed_files, sandbox_dir, timeout_budget_s, op_id)` signature is injected. In production, a real `LanguageRouter` is wired in `_build_components()`.

---

## Component: Single Deadline Owner

`pipeline_deadline` is stamped **once** in `GovernedLoopService.submit()` before calling `orchestrator.run(ctx)`:

```python
# governed_loop_service.py — submit()
pipeline_deadline = datetime.now(tz=timezone.utc) + timedelta(
    seconds=self._config.pipeline_timeout_s   # existing or new config field
)
ctx = ctx.advance(OperationPhase.CLASSIFY, pipeline_deadline=pipeline_deadline)
```

`GovernedOrchestratorConfig` gets **no** `validate_timeout_s`. The VALIDATE phase computes:

```python
remaining_s = (ctx.pipeline_deadline - datetime.now(tz=timezone.utc)).total_seconds()
if remaining_s <= 0:
    # → CANCELLED, no subprocess spawn
```

`pipeline_deadline` is stored as a new `Optional[datetime]` field on `OperationContext` (frozen dataclass, included in hash).

---

## Component: _run_validation() — the core replacement

Replaces `_validate_candidates()` (sync, AST-only):

```
_run_validation(ctx, candidate, remaining_s) -> ValidationResult:

  1. AST preflight (fast, free):
     ast.parse(candidate["content"])
     → SyntaxError → ValidationResult(passed=False, failure_class="test",
                                       short_summary="SyntaxError: …")
       (no subprocess spawned)

  2. Budget check:
     remaining_s <= 0 → raise BudgetExhausted

  3. Write candidate content to temp sandbox dir (mkdtemp, auto-cleanup)

  4. Build changed_files = (Path(candidate["file"]),)

  5. result = await self._validation_runner.run(
         changed_files=changed_files,
         sandbox_dir=sandbox_path,
         timeout_budget_s=remaining_s,
         op_id=ctx.op_id,
     )

  6. Map MultiAdapterResult → ValidationResult:
     - result.passed → ValidationResult.passed
     - result.failure_class → ValidationResult.failure_class
     - result.total_duration_s → ValidationResult.validation_duration_s
     - Compact summary (≤300 chars per adapter) → ValidationResult.short_summary
     - Artifact ref → ledger data (NOT in ValidationResult)
```

---

## Component: Lean ValidationResult (op_context.py)

Only compact fields live in context (hashed on every advance):

```python
@dataclass(frozen=True)
class ValidationResult:
    passed: bool
    best_candidate: Optional[Dict[str, Any]]   # kept for GATE/APPLY
    validation_duration_s: float
    error: Optional[str]                       # existing, kept

    # New compact fields:
    failure_class: Optional[str] = None        # "test" | "build" | "infra" | None
    short_summary: str = ""                    # ≤300 chars, human-readable
    adapter_names_run: Tuple[str, ...] = ()    # e.g. ("python",) or ("python", "cpp")
```

**Full adapter output (stdout/stderr, per-adapter TestResult)** goes into the ledger `data` dict — never into `ValidationResult` or `OperationContext`.

---

## Component: PHASE_TRANSITIONS addition (op_context.py)

```python
OperationPhase.VALIDATE: {
    OperationPhase.GATE,
    OperationPhase.VALIDATE_RETRY,
    OperationPhase.CANCELLED,
    OperationPhase.POSTMORTEM,   # ← NEW: infra failures during validation
},
```

---

## Failure Mapping (Deterministic 3-Way)

| Condition | OperationPhase | OperationState | Ledger reason |
|-----------|---------------|----------------|---------------|
| test/build failure (VALIDATE) | CANCELLED | FAILED | `validation_test_failure` / `validation_build_failure` |
| infra failure (VALIDATE) | POSTMORTEM | FAILED | `validation_infra_failure` |
| budget exhausted (VALIDATE) | CANCELLED | FAILED | `validation_budget_exhausted` |
| test failure (VERIFY, post-APPLY) | POSTMORTEM | ROLLED_BACK | `verify_test_failure` (ChangeEngine rolls back) |
| infra failure (VERIFY) | POSTMORTEM | FAILED | `verify_infra_failure` |

**Note:** `ROLLED_BACK` OperationState during VERIFY reflects an actual filesystem rollback by `ChangeEngine`. During VALIDATE (pre-APPLY), no file has been touched — failure terminates as CANCELLED+FAILED.

---

## Ledger Provenance Format

On validation outcome, the ledger entry `data` dict carries full detail:

```python
{
    "validation_passed": bool,
    "failure_class": "test" | "build" | "infra" | None,
    "adapter_names_run": ["python"] | ["python", "cpp"],
    "validation_duration_s": float,
    "short_summary": "≤300 char human summary",
    # Per-adapter detail (full stdout truncated to 2000 chars):
    "adapter_results": [
        {
            "adapter": "python",
            "passed": bool,
            "failure_class": str | None,
            "duration_s": float,
            "stdout_tail": "last 2000 chars of stdout",
            "failed_tests": ["test_foo::test_bar"],
        },
        ...
    ],
}
```

---

## op_id Continuity

Already satisfied (`_build_change_request()` line 502 already passes `op_id=ctx.op_id`). The new `_run_validation()` passes `op_id=ctx.op_id` to `LanguageRouter.run()` explicitly.

---

## Wiring in GovernedLoopService._build_components()

```python
# After building self._generator, self._approval_provider:
from backend.core.ouroboros.governance.test_runner import (
    LanguageRouter, PythonAdapter, CppAdapter,
)
python_adapter = PythonAdapter(repo_root=self._config.project_root)
cpp_adapter = CppAdapter()
validation_runner = LanguageRouter(
    repo_root=self._config.project_root,
    adapters={"python": python_adapter, "cpp": cpp_adapter},
)

self._orchestrator = GovernedOrchestrator(
    stack=self._stack,
    generator=self._generator,
    approval_provider=self._approval_provider,
    config=orchestrator_config,
    validation_runner=validation_runner,   # ← injected
)
```

---

## Adapter Routing (as specified)

Inherited from `_ADAPTER_RULES` in `test_runner.py` (Phase 1.5):

| Path pattern | Adapters required |
|---|---|
| `mlforge/**`, `bindings/**` | `python` + `cpp` (both must pass) |
| `reactor_core/**`, `tests/**`, `jarvis/**`, `prime/**` | `python` only |
| Everything else (catch-all) | `python` only |

---

## Acceptance Criteria Mapping

| Criterion | How satisfied |
|-----------|--------------|
| VALIDATE always calls TestRunner for non-trivial ops | `_run_validation()` called in VALIDATE loop; AST preflight is fast-fail only |
| APPLY is unreachable when VALIDATE fails/times out | All VALIDATE failure paths → CANCELLED or POSTMORTEM; neither has APPLY in transitions |
| op_id identical in all ledger entries | `op_id=ctx.op_id` passed to all: `_record_ledger`, `LanguageRouter.run`, `CommProtocol` |
| Rollback executes on test/build failure (VERIFY) | Existing ChangeEngine rollback + ROLLED_BACK state; VALIDATE failures never reach APPLY |
| Infra failures end in POSTMORTEM | `failure_class="infra"` → `ctx.advance(POSTMORTEM)` deterministically |
| Deterministic adapter routing for mlforge/bindings | `_ADAPTER_RULES` pattern `^(mlforge\|bindings)/` → `("python","cpp")` |

---

## Hard Constraints Checklist

- [x] No global LanguageRouter in orchestrator — injected via constructor
- [x] No `validate_timeout_s` — single `pipeline_deadline` from submit()
- [x] VALIDATE → POSTMORTEM added to PHASE_TRANSITIONS
- [x] ValidationResult stores compact fields only; full output in ledger
- [x] ChangeRequest.op_id already populated; no new generation
- [x] AST preflight kept as cheap first gate
- [x] No new modules — LanguageRouter from test_runner.py (Phase 1.5)
- [x] No supervisor bloat — wiring is in _build_components(), dispatch is thin
