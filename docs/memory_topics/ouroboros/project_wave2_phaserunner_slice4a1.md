---
title: Wave 2 (5) Slice 4a.1 — Result
modules: [orchestrator.py, tests/governance/phase_runner/test_validate_runner_parity.py, test_orchestrator.py]
status: historical
source: project_wave2_phaserunner_slice4a1.md
---

# Wave 2 (5) Slice 4a.1 — Result

**Status:** implementation complete, parity tests + orchestrator regression green, flag stays **default off** pre-graduation.

## Why 4a.1 = VALIDATE as its own sub-slice

Operator authorized a further split of Slice 4 into 4a.1 (VALIDATE) / 4a.2 (GATE) / 4b (APPROVE+APPLY+VERIFY) because the combined Slice 4 reading (~2,500 lines across 5 phases) risked thinning parity coverage on nested-retry FSM / shadow harness paths / 5a/5b previews / SemanticGuardian / MutationGate / risk floor logic. Per directive: *"Do not compress VALIDATE+GATE into one change set if that would dilute retry-loop parity. Prefer smaller merges over weaker §6 coverage."*

## What landed

| Artifact | Path | Notes |
|---|---|---|
| VALIDATERunner | `phase_runners/validate_runner.py` | Verbatim 4693-5440; constructor takes `(orch, serpent, generation, generate_retries_remaining, episodic_memory)` |
| Flag helper | `orchestrator.py` `_phase_runner_validate_extracted()` | `JARVIS_PHASE_RUNNER_VALIDATE_EXTRACTED` default false |
| Delegation hook | `orchestrator.py` ~line 4711 | 762-line inline block wrapped in `else:` branch |
| Parity tests | `test_validate_runner_parity.py` | 13 tests covering all 7 terminal paths + happy path + L2 converged deep path |

## Parity test outcomes

**Both paths, both suites — 131/131 green:**

- flag=false: `pytest test_orchestrator.py + phase_runner/` → 131 passed
- all-extraction-flags=true: → 131 passed

### The 7 terminal exit paths (all covered)

1. `validation_budget_exhausted` — pre-iteration budget check (remaining_s ≤ 0)
2. `validation_budget_exhausted` — failure_class=budget, non-retryable mid-loop
3. `validation_infra_failure` — failure_class=infra, non-retryable
4. L2 `cancel` / `fatal` directive (repair_engine returns terminal)
5. `no_candidate_valid` — retries exhausted, no L2 recovery available
6. `source_drift_detected` — candidate hash differs from on-disk hash
7. `read_only_complete` — `ctx.is_read_only` → full COMPLETE (not GATE)

### Deep-path coverage (per operator directive)

- **L2 converged "break"**: test `test_nested_retry_l2_converges_advances_to_gate` verifies the retry loop exhausts → L2 returns `("break", fresh_cand, fresh_val)` → runner proceeds past source-drift → advance to GATE with the fresh candidate in `artifacts["best_candidate"]`.
- **L2 cancel**: test `test_nested_retry_loop_exhausts_to_l2_cancel` with `max_validate_retries=1` (2 iterations) verifies the inner retry loop runs the full iteration count before L2 fires.
- **No L2 available**: `test_retry_exhausted_no_l2_cancels` verifies `repair_engine=None` yields `no_candidate_valid` without spurious L2 invocation.

## Cross-phase artifact — `best_candidate`

Inline audit found `best_candidate` referenced 37 times downstream of VALIDATE (primarily inside GATE's SecurityReviewer / SimilarityGate / SemanticGuardian / MutationGate / 5a-green preview / 5b-yellow notify blocks). Runner threads it via `PhaseResult.artifacts["best_candidate"]`. Orchestrator hook rebinds `best_candidate = _validate_result.artifacts.get("best_candidate")` before GATE inline code runs. Same pattern as Slice 2 `_advisory` and Slice 3 PLAN advisory threading.

`best_validation` rides on `ctx.validation` (stamped by the advance call) so no separate artifact threading needed — orchestrator inline code at and past line 5440 reads it as `ctx.validation`.

## FSM telemetry preserved verbatim

Every `_fsm_log(state, extra)` call from the inline block is preserved. Counts: 17 distinct transition states (`iter_start`, `budget_exhausted_pre`, `budget_exhausted_return`, `infra_early_return_set`, `budget_early_return_set`, `early_return`, `candidate_passed_break`, `no_tests_short_circuit`, `l2_dispatch_pre`, `l2_dispatch_post`, `l2_converged_break`, `l2_escape_return`, `l2_skipped`, `no_candidate_valid_return`, `micro_fix_pre/returned/succeeded_break/skipped_new_file/skipped_no_target/cancelled/exception_swallowed`, `retry_advance_pre/post`, `loop_exit_normal`). Test `test_fsm_emits_iter_start_log` pins that telemetry still fires on the runner path.

## Authority invariant (grep-pinned)

Runner imports: `ledger.OperationState`, `op_context.*`, `phase_runner.*`, plus function-local imports matching the inline block (`lsp_checker`, `shadow_harness`, `entropy_calculator`, `interactive_repair`, `structured_critique`, `gap_signal_bus`). Test `test_validate_runner_bans_execution_authority_imports` grep-pins the ban on `candidate_generator` / `iron_gate` / `change_engine`.

## What's NOT changed

- Zero behavior change at default env (flag off → inline verbatim runs)
- No §6 Iron Gate semantics touched
- No §1 execution authority widened
- Shadow harness advisory-only contract preserved
- L2 deadline reconciliation (Session V fix) preserved
- All 7 terminal paths preserve their inline ledger + telemetry

## Graduation criteria

- ✅ Runner + parity tests + regression green on both paths
- ⬜ 3 clean battle-test sessions with flag=true
- ⬜ Flip `JARVIS_PHASE_RUNNER_VALIDATE_EXTRACTED` default false→true
- ⬜ Post-slice-6: delete inline VALIDATE block

## Next sub-slices

- **4a.2**: GATE body (~600 lines including 5a/5b previews + SemanticGuardian + MutationGate + risk floor + SecurityReviewer). Pending operator authorization.
- **4b**: APPROVE + APPLY + VERIFY (~1135 lines). After 4a.2.

Each sub-slice gets its own 3-clean-session graduation arc. Operator directive: *"Do not 'thin' tests to fit context; prefer smaller merges over weaker §6 coverage."*
