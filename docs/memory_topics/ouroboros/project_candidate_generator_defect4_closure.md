---
title: CandidateGenerator Defect #4 — CLOSED 2026-05-03
modules: [backend/core/ouroboros/governance/candidate_generator.py, backend/core/ouroboros/governance/flag_registry_seed.py, scripts/candidate_generator_defect4_verdict.py]
status: historical
source: project_candidate_generator_defect4_closure.md
---

# CandidateGenerator Defect #4 — CLOSED 2026-05-03

3-slice arc fixing the fourth and final systemic defect from soak v5: 3 EXHAUSTION events with `remaining_s=0.0` + 4 `Task exception was never retrieved` asyncio errors + 1 STREAM RUPTURE. Two distinct sub-defects identified, both fixed structurally.

## Root cause (two layers)

1. **Task leak via `asyncio.shield`**: `candidate_generator.py` lines 1911 + 2951 spawn `_tier0.generate()` as background tasks via `ensure_future`. The `asyncio.shield(...)` wrapper prevents cancellation when the outer `wait_for` times out — the shielded task continues running. If it later raises (e.g., `RuntimeError('all_providers_exhausted')`), nobody retrieves the exception → asyncio's default handler logs "Task exception was never retrieved".
2. **Retry-without-budget**: `_call_fallback` was entered with `remaining_s=0.0`, attempted the call anyway, got `CancelledError` mid-flight, raised relabeled as `fallback_failed`. Wasted CPU + provider call attempt + log noise + the unhandled-exception cascade.

## Slices shipped

- **Slice A — Task-leak prevention done_callback helper**. New `_swallow_task_exception(task)` module-level function. Attached as `add_done_callback` to all 4 spawn sites (lines 1911, 2071, 2951, 2966). Classifies retrieved exceptions: expected (CancelledError / TimeoutError / one of 5 EXPECTED_BACKGROUND_EXC_PATTERNS) → DEBUG; unexpected → WARNING with `(consumed by _swallow_task_exception to prevent asyncio leak)` marker. NEVER raises — even a misbehaving exception accessor must not propagate from the callback.
- **Slice B — Pre-fallback budget short-circuit**. In `_call_fallback`, immediately after env-disable check, if `_pre_sem_remaining <= JARVIS_FALLBACK_MIN_VIABLE_BUDGET_S` (default 5.0s, env-tunable, floor 1s, ceiling 60s), invoke `self._raise_exhausted("deadline_exhausted_pre_fallback", ...)` with structured payload (pre_sem_remaining_s, min_viable_s, phase, route).
- **Slice C — AST pin + flag seed + verdict**. `register_shipped_invariants()` enforces helper presence + 3 required string literals + every `ensure_future`/`create_task` of `.generate()` or background-poll has paired `add_done_callback(_swallow_task_exception)` within 10 source lines (skipping helper's own body to avoid docstring false positives). 1 new FlagRegistry seed. 6-contract verdict.

## Architectural decisions worth remembering

- **Defense-in-depth: callback + short-circuit**. The callback (Slice A) consumes ALL straggler exceptions from shielded background tasks — the structural fix at the asyncio layer. The short-circuit (Slice B) prevents ONE common cause class from happening in the first place. Both layers ship together: A handles unforeseen raises, B prevents the deterministically-doomed path.
- **5 expected-pattern classification list**. The `_EXPECTED_BACKGROUND_EXC_PATTERNS` tuple is a closed enumerated set. Operators see WARNING for any unrecognized pattern, immediately surfacing new failure modes.
- **AST pin enforces source-level pairing**. The pin scans every line for `asyncio.ensure_future` / `asyncio.create_task` containing `.generate(` or `_background_poll_tier0` — and asserts `add_done_callback(_swallow_task_exception)` appears in the next 10 lines. Skips lines inside the helper's own body (the docstring legitimately mentions the spawn primitives). Caught one false-positive during dev (helper's docstring at line 429); fixed by computing the helper's `lineno`/`end_lineno` and excluding that range.
- **`_raise_exhausted` reused for clean cause**. The pre-fallback short-circuit raises through the existing `_raise_exhausted` helper with a NEW cause string `deadline_exhausted_pre_fallback`. The orchestrator's main retry loop already catches `RuntimeError('all_providers_exhausted:*')` and classifies the suffix — so the new cause flows through the existing handlers without orchestrator changes.

## Empirical-closure verdict (6/6 PRIMARY PASS)

```
[PASS] C1 _swallow_task_exception helper + expected patterns
       helper_present=True expected_pattern_count=5/5
[PASS] C2 Helper classifies + consumes all exception classes
       all paths consumed cleanly
       (live evidence: WARNING fired for ValueError test case BEFORE
        verdict header — helper consumed it cleanly)
[PASS] C3 AST pin enforces ensure_future/add_done_callback pairing
       all spawn sites paired
[PASS] C4 Pre-fallback budget short-circuit present
       4/4 markers found in source
[PASS] C5 JARVIS_FALLBACK_MIN_VIABLE_BUDGET_S seeded
       type=float default=5.0 category=timing
[PASS] C6 Substrate AST pin holds
       violations=()
```

C2 produced **live empirical evidence** during the verdict run itself — the WARNING `[CandidateGenerator] background task unhandled exception (consumed by _swallow_task_exception to prevent asyncio leak): ValueError(unexpected unrelated error)` fired BEFORE the verdict header, proving the helper correctly captures + classifies + consumes unexpected exceptions in real time.

## Reuse contract honored (no duplication)

- Existing `_raise_exhausted` helper reused for the new cause — no parallel exception-raise path
- Existing `RuntimeError('all_providers_exhausted:*')` shape preserved — orchestrator handlers unchanged
- Existing `add_done_callback` asyncio idiom — no new exception-consumption substrate
- Existing `register_shipped_invariants` registration contract reused
- Existing FlagSpec pattern reused (162 → 163 total seeds)
- Existing `_EXPECTED_BACKGROUND_EXC_PATTERNS` closed-tuple pattern mirrors prior arcs' closed-5 enums

## What this unlocks

- **Soak v5's cascading-failure pattern is structurally fixed**: the 4 unhandled task exceptions class is eliminated entirely (every spawn site has a callback); the 3 EXHAUSTION-with-zero-budget pattern is short-circuited cleanly.
- **Operational visibility improved**: when an unexpected exception class DOES leak from a spawn site (regression), it logs at WARNING with the `(consumed by _swallow_task_exception)` marker — instantly searchable + forms basis for future expected-pattern additions.
- **Cost contract preserved**: short-circuit fires BEFORE provider call attempt → no wasted Claude/DW spend on doomed calls.
- **Combined with Defect #1+#2+#3 fixes**: the next soak should produce (a) clean termination at wall cap (Defect #1), (b) populated Production Oracle history (Defect #2), (c) writable persistent intelligence with circuit-breaker visibility (Defect #3), AND (d) zero unhandled-task-exceptions + clean-cause exhaustion logs (Defect #4). **All 4 systemic defects from soak v5 findings now structurally addressed.**

## Files touched

- `backend/core/ouroboros/governance/candidate_generator.py`:
  - `_swallow_task_exception` helper + `_EXPECTED_BACKGROUND_EXC_PATTERNS` (Slice A)
  - 4 spawn sites get `add_done_callback` (Slice A)
  - `_call_fallback` pre-budget short-circuit (Slice B)
  - `register_shipped_invariants` NEW (Slice C)
- `backend/core/ouroboros/governance/flag_registry_seed.py`:
  - 1 new FlagSpec entry (`JARVIS_FALLBACK_MIN_VIABLE_BUDGET_S`)
- `scripts/candidate_generator_defect4_verdict.py` (NEW)

Closes Defect #4 from soak v5 findings. **All 4 systemic defects from soak v5 are now closed**: WallClockWatchdog fire delay (#1), Production Oracle observer boot wire-up (#2), PersistentIntelligence readonly-DB silent degradation (#3), CandidateGenerator task leak + retry-without-budget (#4).
