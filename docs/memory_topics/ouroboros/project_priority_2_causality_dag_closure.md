---
title: Project Priority 2 Causality Dag Closure
modules: [tests/governance/test_causality_dag_replay_from_record.py, tests/governance/test_causality_dag_graduation.py, backend/core/ouroboros/governance/worktree_manager.py, backend/core/ouroboros/governance/cost_contract_assertion.py]
status: merged
source: project_priority_2_causality_dag_closure.md
---

Priority 2 Causality DAG + Deterministic Replay arc CLOSED 2026-04-29 (post-finishing-pass `2c0f642735`).

**Why:** Post-Phase-12 the system had passive verification (Priority 1 ships confidence as a routing/circuit-breaker signal), but no historical navigability. Decisions wrote to a flat `decisions.jsonl` with no causal links. Three concrete gaps: (1) per-process ordinal counter collided under L3 worktree fan-out (W3(6) known debt); (2) no parent_record_ids in record schema — `inputs_hash` collapses content but doesn't preserve causal links; (3) no counterfactual differentiation — HypothesisProbe outputs recorded identically to live-path. Six slices structurally close all three.

**How to apply:** Treat as the canonical closure record for any "DAG navigation" / "replay-from-record" / "L3 fan-out determinism" question. The replay path is annotation-only (env-var overlay) and goes through the orchestrator's existing `--rerun` dispatch — §26.6 four-layer cost contract holds by construction. Don't re-litigate the L3 fan-out bug; the per-worker sub-ordinal namespace structurally fixes it.

Slice trail (single-day arc):

| Slice | Commits | Tests | Status |
|---|---|---|---|
| 1 — Schema extension | `ba91de582e` + `bddca75ab6` | 38 | graduated default-true |
| 2 — Per-worker sub-ordinals | `91190886e2` + `6b49270647` | 49 | graduated default-true (master + enforce) |
| 3 — DAG construction primitive | `c5ff33ca58` | 55 | graduated default-true |
| 4 — Navigation surface | `18aa41474f` | 45 | graduated default-true |
| 5 — Replay-from-record | `1c826aa790` | 35 | graduated default-true |
| 6 — Graduation flip | `ae52cb4352` (Antigravity, partial) | 14 | partial |
| 6.5 finishing-pass | `cb892ec66e` (auto) + `2c0f642735` | +18 graduation + 3 cost-contract | CLOSED |

Audit caught Antigravity's Slice 6 was materially incomplete vs scope doc:
  * 0 of 4 shipped_code_invariants seeds → 4 added
  * Master flags still default false (despite Slice 6 being graduation slice) → 6 flipped
  * 0 cost-contract preservation tests under replay → 3 added
  * 12 of 25-30 graduation pins → 30
  * 6 of 7 FlagRegistry seeds → 9 (added schema + per-worker × 2)

Final tokens:
  * 6 master flags graduated default-true (independent hot-revert paths each)
  * 4 new shipped_code_invariants seeds (causality_dag_no_authority_imports / causality_dag_bounded_traversal / dag_navigation_no_ctx_mutation / dag_replay_cost_contract_preserved) → total 11 invariants, all hold against main
  * 9 FlagRegistry seeds for the Causality DAG family
  * Combined regression: 655/655 green (Priority 2 all slices + Priority 1 graduation + cost contract + dependencies)
  * §26.6 four-layer cost-contract defense verified holding under DAG / replay state via 3 dedicated tests in `test_causality_dag_replay_from_record.py` + 3 in `test_causality_dag_graduation.py`

Components closed by this arc:
  * **DecisionRecord schema extension** (Slice 1) — `parent_record_ids: Tuple[str,...]` + `counterfactual_of: Optional[str]` fields; SCHEMA_VERSION unchanged (additive backward-compat); pre-Slice-1 records parse cleanly
  * **Per-worker ordinal namespace** (Slice 2) — `(worker_id, op_id, phase, kind)` composite key fixes L3 fan-out determinism; `worker_id_for_path()` in `worktree_manager.py` is pure (AST-pinned no I/O); shadow→enforce two-flag pattern
  * **CausalityDAG primitive** (Slice 3) — `verification/causality_dag.py`; bounded BFS, cycle detection, topo sort, counterfactual branch detection; `JARVIS_DAG_MAX_RECORDS` (100K) + `JARVIS_DAG_MAX_DEPTH` (8) bounds
  * **Navigation surfaces** (Slice 4) — `/postmortems dag` REPL family + `/observability/dag/{session,record}` GETs + `EVENT_TYPE_DAG_FORK_DETECTED` SSE; three independent sub-flags (REPL/GET/SSE)
  * **Replay-from-record** (Slice 5) — `--rerun-from <record-id>` CLI arg; pure env-overlay (`JARVIS_CAUSALITY_FORK_FROM_RECORD_ID` + `JARVIS_CAUSALITY_FORK_COUNTERFACTUAL_OF`); requires `--rerun` for session identity; cost contract preservation pinned by `dag_replay_cost_contract_preserved` invariant

Don't re-litigate:
  * Replay-from-record going through orchestrator: it's PURELY env-overlay; the orchestrator's existing `--rerun` path is what dispatches. The shipped-code-invariant pins this structurally
  * Schema bumps: SCHEMA_VERSION stays `decision_record.1` because all extensions (Slices 1+2) are additive — bumping would invalidate every pre-existing ledger
  * Slice 6's graduation-flip discipline: the Antigravity-shipped Slice 6 deferred flag flips to operator (defensible) but the finishing-pass flipped them per scope-doc instructions; both are valid; the finishing-pass closes the gap so the contract is observably enforced rather than soak-deferred

Composition with shipped infrastructure (zero duplication):
  * Confidence-drop SSE events (Priority 1 Slice 4) → DAG nodes via `parent_record_ids`
  * HypothesisProbe verdicts (Priority 1 Slice 3) → counterfactual fork branches via `counterfactual_of`
  * Sustained-low-confidence trends → DAG cluster detection via Slice 3's `cluster_kind` heuristic
  * `--rerun` (Phase 1 Slice 1.4) → extended with `--rerun-from <record-id>` (Slice 5)
  * `EventChannelServer` GET surface → 2 new endpoints (Slice 4)
  * `IDEStreamRouter` SSE broker → 1 new event class (Slice 4)
  * `cost_contract_assertion.py` (§26.6) → invariant pin verifies replay path doesn't bypass

Pass C unlock dependency: Slice 4's `dag drift <session-a> <session-b>` is now the per-trajectory drift substrate Pass C MetaAdaptationGovernor needs. Slice 5's counterfactual replay produces evidence Pass C will mine for adaptation rules. Both are gated on Pass B Slice 1 (held on W2(5) Slice 5b). Together Priority 2 + Priority 3 turn drift detection from heuristic to structural.

Critical-path next step: **W2(5) Slice 5b** (Iron Gate parity for GENERATE phase runner). When 5b ships → Pass B Slice 1 unblocks → Pass C Slice 1 unblocks → Priority 3 (Adaptive Anti-Venom) becomes the next concrete arc per PRD §26.5.3.
