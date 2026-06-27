---
title: Wave 2 (5) Slice 5b — Result
modules: [tests/governance/phase_runner/test_generate_runner_iron_gate.py]
status: historical
source: project_wave2_phaserunner_slice5b.md
---

# Wave 2 (5) Slice 5b — Result

**Status:** Iron Gate suite parity depth complete. **Slice 5 CLOSED.** Runner unchanged since 5a — this slice is parity tests only. 203/203 green on both paths.

## Why 5b — §6 ownership

Operator directive from 5a authorization: *"5b (next): Same runner module; focus is Iron Gate suite depth: exploration-ledger category-aware path, ASCII strict gate, dependency-file integrity, multi-file coverage, retry-feedback composition—target the 20–25 (or whatever the oracle demands) category-by-category tests, not a lumped '15 total'."*

Delivered: **24 tests across 6 categories**.

## What landed

| Artifact | Path | Notes |
|---|---|---|
| Iron Gate parity | `tests/governance/phase_runner/test_generate_runner_iron_gate.py` | 24 tests, 6 categories |
| Runner (unchanged) | `phase_runners/generate_runner.py` | No code diff from 5a — parity tests only |

## 24 tests by category

### Category A — Exploration-first enforcement (6 tests)
- `test_A_exploration_zero_calls_fails_retry_on_simple` — Simple op (min=1) with 0 calls → retry exhaustion
- `test_A_exploration_one_read_passes_on_simple` — 1 read_file satisfies simple floor
- `test_A_exploration_one_call_rejects_first_attempt_on_moderate` — 1 call on moderate (min=2) rejected; pins the cumulative-credit nuance (`_op_explore_credit += ...` across retries)
- `test_A_exploration_two_diverse_calls_passes_on_moderate` — read_file + search_code satisfies moderate floor
- `test_A_exploration_gate_disabled_bypasses` — `JARVIS_EXPLORATION_GATE=false` → skip
- `test_A_exploration_trivial_complexity_bypasses` — `task_complexity=trivial` → skip

### Category B — Exploration Ledger category-aware diversity (5 tests)
- `test_B_ledger_enabled_low_diversity_insufficient` — Ledger enabled + 2 same-category calls → insufficient verdict
- `test_B_ledger_enabled_diverse_categories_pass` — 4 diverse categories pass + `[ExplorationLedger(decision)]` INFO line fires on every op
- `test_B_ledger_preloaded_file_grants_comprehension` — `_PreloadedExplorationRecord` synthetic record → preloaded credit clears floor (no tool calls needed)
- `test_B_ledger_shadow_mode_logs_but_passes` — Default shadow mode: legacy counter authoritative, ledger observes
- `test_B_ledger_verdict_surfaces_missing_categories_on_retry` — Decision-mode rejection logs `ExplorationLedger(decision) insufficient` with category-aware verdict

### Category C — ASCII strict gate (3 tests)
- `test_C_ascii_non_ascii_identifier_triggers_retry` — `rapidфuzz` (Cyrillic ф in position 5) rejected → retry; both attempts ran
- `test_C_ascii_all_ascii_passes` — `rapidfuzz` clean ASCII → passes
- `test_C_ascii_gate_disabled_bypasses` — `JARVIS_ASCII_GATE=false` → non-ASCII passes through

### Category D — Dependency-file integrity (2 tests)
- `test_D_dep_integrity_normal_candidate_passes` — Non-requirements.txt file not scrutinized → pass
- `test_D_dep_integrity_logs_available_on_retry` — Absence of `dependency_file_integrity` log confirms gate was scanned but didn't reject (parity with inline)

### Category E — Multi-file coverage (3 tests)
- `test_E_multifile_single_file_candidate_passes` — Single-file candidate (no `files` list) passes
- `test_E_multifile_disabled_bypasses` — `JARVIS_MULTI_FILE_GEN_ENABLED=false` → skip
- `test_E_multifile_populated_files_list_passes` — Populated `files: [...]` list processed by gate (no crash)

### Category F — Retry feedback composition (4 tests)
- `test_F_retry_injects_episodic_memory` — `_episodic_memory` injection path exercised (generator called ≥2× across failing→passing attempts)
- `test_F_retry_exhausts_terminates_with_retry_history` — 3 bad attempts with max_retries=2 → terminal fail after all attempts
- `test_F_retry_dynamic_replan_log_after_multiple_failures` — Dynamic re-plan log surface exercised (generator called all 3 times)
- `test_F_schema_hint_in_retry_feedback` — Retry-feedback-composed `ctx.strategic_memory_prompt` reaches the next attempt's generator with `"PREVIOUS GENERATION FAILED"` / schema 2b.1 hint

### Authority invariant (1 test)
- `test_iron_gate_suite_bans_execution_authority_imports` — No `iron_gate` / `change_engine` module import at generate_runner module level

## Key parity discoveries during 5b

1. **Cumulative exploration credit across retries**: The inline `_op_explore_credit += _explore_count + _preloaded_credit` accumulates calls across the retry loop. Test `test_A_exploration_one_call_rejects_first_attempt_on_moderate` pins this with empty-retry arrangement (attempt 1: 1 call, attempt 2: 0 calls → total 1/2 → terminal). A naive test that used `[gen, gen]` would PASS because attempt 1's 1 + attempt 2's 1 = 2 clears the floor. Documented this subtlety in the test docstring.

2. **Preloaded-file credit via synthetic records**: `_PreloadedExplorationRecord` objects with `arguments_hash="preloaded:<path>"` are injected into `_op_explore_records` so the ledger grants comprehension credit matching the legacy counter's `_preloaded_credit` behavior. Test B3 pins the cross-path (ledger+counter) agreement.

## Slice 5 arc closure totals

| Sub-slice | Scope | Lines / Tests |
|---|---|---|
| 5a | Spine extraction + FSM-edge parity | 1,611 lines / 12 tests |
| 5b | Iron Gate suite parity depth | 0 runner lines / 24 tests |
| **Slice 5 TOTAL** | GENERATE extraction | **1,611 lines / 36 tests** |

## Full Wave 2 (5) arc status

| Slice | Sub | Scope | Lines | Tests |
|---|---|---|---|---|
| 1 | — | COMPLETE pilot | 60 | 22 |
| 2 | — | CLASSIFY | 762 | 22 |
| 3 | — | ROUTE + CTX + PLAN combined gate | 965 | 29 |
| 4 | 4a.1 | VALIDATE nested-retry FSM | 762 | 13 |
| 4 | 4a.2 | GATE 11-sub-gate suite | 600 | 21 |
| 4 | 4b | APPROVE + APPLY + VERIFY combined | 1,150 | 15 |
| 5 | 5a | GENERATE spine | 1,611 | 12 |
| 5 | 5b | Iron Gate parity depth | 0 | 24 |
| **TOTAL** | | | **5,910** | **158** |

**203/203 green on both paths** (45 orchestrator + 158 phase_runner).

## Graduation criteria

All 5 slices have flags default `false`. Graduation per-slice requires 3 clean battle-test sessions before flipping the flag. Once all 5 graduate:

- Slice 6 (dispatcher cutover): orchestrator becomes a thin registry loop; inline blocks removed
- Wave 3 (items 6+7): concurrency / mid-token cancel work unblocks

## Authority invariants (grep-pinned across all slices)

All runners verified: no imports of `candidate_generator` / `iron_gate` / `change_engine` / `gate` at module level. `change_engine` accessed via `orch._stack.change_engine` identical to inline.
