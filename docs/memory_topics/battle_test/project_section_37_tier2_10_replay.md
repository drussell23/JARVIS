---
title: Project Section 37 Tier2 10 Replay
modules: [backend/core/ouroboros/governance/verification/causality_dag.py, backend/core/ouroboros/governance/replay_repl.py, scripts/ouroboros_battle_test.py, tests/governance/test_section_37_tier2_10_replay_repl.py]
status: historical
source: project_section_37_tier2_10_replay.md
---

## §37 Tier 2 #10 — closure log

PRD §36.4 Priority #2 (Temporal Observability spine) closed. Three composing changes shipped same-day after Tier 1 Dashboard Arc closure:

### (a) CausalityDAG read-API extension

`backend/core/ouroboros/governance/verification/causality_dag.py` — three new public methods on the canonical DAG class:

| Method | Behavior | Defensive |
|---|---|---|
| `nodes_for_phase(phase)` | O(n) scan returning all DecisionRecords with matching phase | blank/None → `()`, NEVER raises |
| `first_record_in_phase(phase)` | First-by-insertion-order match (canonical fork-point) | blank/None → `None`, NEVER raises |
| `distinct_phases()` | Insertion-order tuple with seen-set tracking, blanks skipped | NEVER raises |

138/138 existing DAG construction + navigation + replay-from-record regression spine still green. Zero parallel walker — composes `self._nodes` directly.

### (b) `replay_repl.py` operator browser

`backend/core/ouroboros/governance/replay_repl.py` (~580 LOC pure substrate). Composes:

- `verification.causality_dag.build_dag(session_id)` (canonical DAG construction)
- New phase-filter helpers from (a)
- `.ouroboros/sessions/<id>/decisions.jsonl` discovery (no path duplication; reads canonical layout)

Subcommands: `bare/sessions/phases <session>/show <session>[:<phase>]/help`. Auto-discovered ZERO-EDIT via §32.11 Slice 4 naming-cage — file ends `_repl.py` → verb `/replay` → dispatcher `dispatch_replay_command(line)` derived structurally; `repl_dispatch_registry.try_dispatch('/replay help')` routes correctly without ANY registry edit.

Color discipline: cyan default, yellow VALIDATE/GATE/APPROVE, red ERROR/FAILED/ROLLBACK, dim boundary phases. **NO `bright_green`** — preserves §37.9 invariant #3 + Slice 4 AST lint pin.

### (c) Harness CLI extension

`scripts/ouroboros_battle_test.py` `--rerun-from` extended to accept `<session-id>:<phase>` form alongside the existing `<record-id>` form. Resolution via `build_dag(args.rerun).first_record_in_phase(phase)` rewrites `args.rerun_from` in-place BEFORE the existing `prepare_replay_from_record` codepath. Session-vs-`--rerun` mismatch guard exits 2. Honest empty-state for missing phase + missing DAG.

### AST pins (3 module + 3 source-text)

| Pin | Forbids |
|---|---|
| `replay_repl_composes_canonical_dag` | direct `CausalityDAG()` construction (must compose `build_dag`) |
| `replay_repl_authority_read_only` | `apply_replay_from_record_env` / `prepare_replay_from_record` / `setup_replay_from_cli` calls (read-only browser) |
| `replay_repl_authority_asymmetry` | orchestrator+iron_gate+policy+providers+candidate_generator+urgency_router+change_engine+semantic_guardian imports |

Plus 3 harness-source regression pins (argparse SESSION:PHASE form documented / phase-form resolution branch composes both `build_dag` + `first_record_in_phase` / session-mismatch exit-2 guard present).

### Test count + cost

- 28 new tests in `tests/governance/test_section_37_tier2_10_replay_repl.py`, all green
- 306/307 across full §37 Tier 1 + Tier 2 #10 spine
- 138/138 across canonical DAG core
- ~2h elapsed vs audit-estimated ~3d — **90%+ savings via composition** with existing substrate (validates the audit's "thin-wrapper single-slice viable" call)

### Operator workflow

```
/replay sessions               # find session
/replay phases <session>       # pick fork phase
/replay show <session>:<phase> # copy displayed record_id
python3 scripts/ouroboros_battle_test.py \
    --rerun <session> \
    --rerun-from <record_id>
```

OR pass `--rerun-from <session>:<phase>` directly to harness (resolves via DAG before existing record_id codepath). Both paths converge.

### Pattern crystallization

**Singleton + Read-API Extension Pattern applied 9th time** (was applied 8× across Tier 1; this 9th invocation strengthens crystallization candidacy per Tier 1 closure memo's `project_section_37_tier_1_complete.md` ranking). If a 3rd reusable architectural primitive surfaces, the pattern qualifies for §33 catalog elevation alongside the existing 5 meta-patterns.

### What's open in §37 Tier 2

| # | Arc | Effort | Status |
|---|---|---|---|
| 11 | Session search via SQLite index | ~4-5h | pending |
| 12 | Op dependency graph / parallel fan-out canvas | ~5h | pending |
| 13 | Per-tool confidence indicator | ~4h | pending |
| 14 | Operation modes (`/plan` `/analyze` `/apply` `/auto`) | ~1 slice | pending |
| 15 | Per-tool permissions (Venom V2) | ~2 slices | pending |
| 16 | Per-component tool scope (Pattern C) | ~2 slices | pending |
