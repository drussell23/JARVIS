---
title: Wave 2 (5) Slice 5a — Result
modules: []
status: historical
source: project_wave2_phaserunner_slice5a.md
---

# Wave 2 (5) Slice 5a — Result

**Status:** spine extraction complete, flag stays **default off**, 5b (Iron Gate depth) pending.

## Why 5a + 5b split

Operator authorized splitting Slice 5 after realizing one-pass delivery would thin Iron Gate / Exploration Ledger parity. Per directive: *"Do not ship Slice 5 in one pass if the cost is weaker Iron Gate / Exploration Ledger parity—that directly conflicts with §6 and the standing rule: prefer smaller merges over thinner immune-system coverage."*

- **5a** (this commit): spine extraction + FSM-edge parity (12 tests).
- **5b** (next commit): Iron Gate suite parity depth (20-25 tests covering exploration ledger category-aware path, ASCII strict, dep integrity, multi-file coverage, retry feedback composition). Same runner module + flag.

## What landed

| Artifact | Path | Notes |
|---|---|---|
| GENERATERunner | `phase_runners/generate_runner.py` | Verbatim 3156-4767 (1611 lines). Constructor `(orch, serpent, consciousness_bridge)` |
| Flag helper | `orchestrator.py` `_phase_runner_generate_extracted()` | `JARVIS_PHASE_RUNNER_GENERATE_EXTRACTED` default false |
| Delegation hook | `orchestrator.py` ~line 3156 | 1611-line inline block wrapped in `else:` branch |
| Build script | `/tmp/claude/build_generate_runner.py` | Scripted verbatim extraction — self.→orch. + `return ctx` → `PhaseResult` transforms |
| Spine parity | `test_generate_runner_parity.py` | 12 FSM-edge tests |

## Scripted verbatim extraction (new technique)

Given GENERATE's size (1,611 lines) and complexity (7 terminal `return ctx` sites + nested try/except + deep retry loop), manual transcription risked both budget exhaustion and transcription errors. Used a Python script to:

1. Extract the exact inline block text from `orchestrator.py:3138-4748`
2. Strip 8-space indent
3. `self.` → `orch.` via regex
4. `return ctx\n` → `return PhaseResult(next_ctx=ctx, next_phase=None, status="fail", reason=ctx.terminal_reason_code, artifacts={"generation":..., "episodic_memory":...})` at each terminal site
5. Append normal-exit `return PhaseResult(next_phase=VALIDATE, ...)` after the retry loop
6. Re-indent back to 8 spaces (run method body depth)

This keeps parity byte-exact with the inline block; any future divergence is visible in `git diff` of `generate_runner.py` vs the re-extraction.

## Leak audit — cross-phase artifacts threaded

- `generation` — consumed by VALIDATE via `ctx.generation` AND inline `generation` local. Both paths handled — the orchestrator hook rebinds the local from `artifacts["generation"]` when the runner path runs.
- `_episodic_memory` — consumed by VALIDATE for failure recording + entropy critique aggregation. Threaded via `artifacts["episodic_memory"]`.
- `generate_retries_remaining` — **discovered mid-testing** when VALIDATE's entropy calc at orchestrator.py ~5402 read it. Threaded via `artifacts["generate_retries_remaining"]` with default fallback to `max_generate_retries` in the orchestrator hook.
- `_PreloadedExplorationRecord` — orchestrator module-level class referenced inside GENERATE (Iron Gate exploration ledger preloaded-credit path). Late-imported inside `run()` to avoid circular import.

## 12 spine parity tests

| Test | Coverage |
|---|---|
| `test_generate_runner_is_phase_runner` | Class wiring (PhaseRunner subclass, phase=GENERATE) |
| `test_happy_path_advances_to_validate` | Candidate returned → advance to VALIDATE + artifact threading |
| `test_cost_cap_pre_attempt_terminates` | `op_cost_cap_exceeded` terminal (per-op cost governor pre-attempt check) |
| `test_forward_progress_trip_terminates` | `no_forward_progress` terminal (EC8 detector) |
| `test_productivity_trip_terminates` | `stalled_productivity` terminal (EC9 detector) |
| `test_is_noop_terminates_with_noop_reason` | `is_noop` → advance COMPLETE with `terminal_reason_code=noop` |
| `test_no_candidates_raises_and_retries` | No-candidates exception triggers retry path |
| `test_generator_exception_triggers_retry_path` | Generator exception counted against retries (generator called 2x with max_retries=1) |
| `test_happy_path_threads_retries_remaining` | `generate_retries_remaining` artifact correctly propagated |
| `test_cost_governor_charged_per_attempt` | `CostGovernor.charge(..., phase="GENERATE")` fires per attempt for per-phase cost drill-down |
| `test_serpent_update_phase_generate` | `_serpent.update_phase("GENERATE")` fires at phase start |
| `test_generate_runner_bans_execution_authority_imports` | Authority invariant (no `iron_gate` / `change_engine` at module import) |

## Deliberately deferred to 5b

The following Iron Gate suite behaviors are inside the runner body (verbatim) but dedicated parity tests land in **5b**:

- Exploration ledger category-aware diversity scoring (`ExplorationLedger.from_records`, `_PreloadedExplorationRecord` synthetic records, `render_retry_feedback(verdict, floors)`)
- ASCII strict gate (`AsciiStrictGate.check`, `build_retry_feedback`)
- Dependency-file integrity (hallucinated rename catcher)
- Multi-file coverage gate (`_multi_file_coverage_gate`)
- Retry feedback composition (dynamic re-plan, episodic injection, file-specific failure summaries)

5b adds 20-25 tests covering these gates at §6 depth. Same runner module + flag.

## Authority invariant (grep-pinned)

Runner imports at module level: `ascii_strict_gate`, `forward_progress`, `productivity_detector`, `ledger`, `op_context`, `phase_runner`. Late imports inside `run()`: `orchestrator._PreloadedExplorationRecord` (circular-import-avoiding pattern). No `iron_gate` or `change_engine` module imports at all — `change_engine` is accessed via `orch._stack.change_engine` identical to inline.

## Graduation criteria

- ✅ Runner + 12 spine parity tests + regression green on both paths (179/179)
- ⬜ **5b**: Iron Gate suite parity depth tests land (target 20-25 tests)
- ⬜ 3 clean battle-test sessions with flag=true (AFTER 5b merges)
- ⬜ Flip `JARVIS_PHASE_RUNNER_GENERATE_EXTRACTED` default false→true
- ⬜ Post-slice-6: delete inline GENERATE block

## Next — Slice 5b

Same runner file. Add 20-25 tests focused on Iron Gate suite category-by-category:
- Exploration ledger: category-aware diversity scoring, preloaded-file credit, verdict-based retry feedback
- ASCII strict: violation detection, retry feedback format
- Dependency-file integrity: hallucinated rename detection, retry feedback
- Multi-file coverage: files: [...] validation, rejection on stub
- Retry feedback composition: dynamic re-plan, episodic injection, file-specific failure aggregation

No behavioral drift between 5a merge and 5b merge; flag gating stays consistent until graduation.
