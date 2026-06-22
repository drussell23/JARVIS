# Sovereign Call-Graph Risk Matrix — Design Spec (C1 root-cause)

> **Arc:** resolves final-review finding C1 — the AST symbol-scope is computed then discarded because the OperationAdvisor's blast radius is over the **import graph** (who imports the file), not the **call graph** (who calls the symbol). A symbol-scoped sub-goal therefore inherits the whole file's blast radius and can never clear the veto.
> **Date:** 2026-06-21. **Track B** of the resilience-chunking fix wave (parallel with Track A infra fixes).
> **Mandate:** solve the epistemological mismatch at the root — when the FSM mutates AST symbols, evaluate risk on the **execution path (callers)**, not file size. Reuse the EXISTING oracle call graph; no parallel analyzer.

---

## 1. Diagnosis
`operation_advisor._compute_blast_radius(target_files)` counts files that import the target *module* (import graph), capped at 50. For `semantic_index.py` (imported by 50 files) every sub-goal that touches it — even one scoped to a single 8-line method — measures blast radius 50 and is BLOCKED. The symbol scope produced by `ast_symbol_scoper.isolate_symbols` (Track B1) is real but invisible to the Advisor. **Root cause:** the risk model is file-granular; the mutation is symbol-granular.

## 2. Goals / Non-Goals
**Goals.** (G1) When a sub-goal carries an AST symbol scope (`file::Symbol`), the Advisor computes blast radius over the **transitive callers of those symbols** via the existing oracle call graph — bypassing the file-level import heuristic. (G2) Reuse `oracle.get_callers` + `NodeID.callers` (the call graph is already built — `oracle.py:456,792,1124`); add NO parallel graph. (G3) The symbol scope must PROPAGATE: `SubGoal` carries `scoped_symbols`; it rides the envelope evidence to `ctx`; the Advisor reads it. (G4) Fail-soft + OFF byte-identical: no scope, or oracle unavailable, or master off → the existing file-level blast radius EXACTLY.

**Non-Goals.** No new call-graph construction (oracle owns it). No change to the BLOCK *thresholds* (only the blast-radius *input* gets sharper). No weakening of the gate — a symbol whose callers are themselves numerous still measures high and stays BLOCKED (correctly).

## 3. Reuse Inventory
| Need | Existing asset | Anchor |
|---|---|---|
| Reverse call edges (who calls a symbol) | `oracle.get_callers(node_id) -> List[NodeID]` | `oracle.py:1124` |
| Per-node caller list | `NodeID.callers: List[str]` | `oracle.py:456` |
| Existing oracle-blast integration in the Advisor | `_advisor_oracle_blast_enabled()` + `get_blast_radius()` consult | `operation_advisor.py:114-132`, `:1767-1788` |
| Symbol isolation (produces `file::Symbol`) | `ast_symbol_scoper.isolate_symbols` / `ScopedTarget` | (Track B1, merged) |
| Sub-goal carrier | `goal_decomposition_planner.SubGoal` | `:331-360` |
| Active oracle handle | `operation_advisor._active_oracle` | `:221` |

## 4. Component Specs

### 4.1 Symbol-scope propagation (the wire C1 says is severed)
- `goal_decomposition_planner.SubGoal` gains `scoped_symbols: Tuple[str, ...] = ()` (each `"file::Symbol"`). `decompose_for_block` populates it from `isolate_symbols` (STOP discarding the `ScopedTarget`s in `_scoped_files_for`). Additive, default `()` → pre-change byte-identical.
- The sub-goal envelope stamps `evidence["scoped_symbols"]` (in `_make_envelope_for_sub_goal`) so it rides intake → `ctx.intake_evidence_json` → the Advisor (the same side-channel A1-T3 used for `dag_weight`).

### 4.2 `symbol_blast_radius` (new pure helper, reuses oracle)
- `def symbol_blast_radius(scoped_symbols, *, oracle) -> Optional[int]`: for each `file::Symbol`, resolve the oracle `NodeID`, BFS the transitive `get_callers` closure (bounded depth + bounded fan, env-tunable curve, dedup by node id), return the count of distinct caller symbols (capped to match the legacy 50 ceiling for comparability). Returns `None` when the oracle can't resolve a symbol (→ caller falls back to file-level). Pure, fail-soft, NEVER raises. Lives in `operation_advisor.py` (or a small `call_graph_blast.py` leaf it imports) — reuses `oracle.get_callers`, builds no graph.

### 4.3 Advisor integration (sharpen the input, not the thresholds)
- In `_compute_blast_radius` (or its caller), BEFORE the file-level scan: if the op carries `scoped_symbols` (read from `ctx`/evidence) AND `_advisor_oracle_blast_enabled()` AND `_active_oracle` is available → use `symbol_blast_radius(...)`; if it returns an int, that IS the blast radius (call-graph truth). Else fall through to the existing file-level computation EXACTLY. Gated additionally by `JARVIS_ADVISOR_CALLGRAPH_BLAST_ENABLED` (default decided at graduation; OFF → file-level, byte-identical).

## 5. Cross-cutting
- **Invariants.** (I1) Gate never weakened: call-graph blast is the *same or sharper* signal; a widely-called symbol still BLOCKs. (I2) OFF byte-identical (no scope / master off / oracle absent → file-level). (I3) Fail-soft: any oracle/resolve error → file-level fallback, never crash, never force-pass. (I4) No parallel call graph — `oracle.get_callers` only.
- **Bounded:** the caller BFS is depth- + fan-bounded (env curve) so a pathological hub symbol can't blow up the Advisor's <60s budget.
- **Track boundary:** Track B touches ONLY `operation_advisor.py`, `goal_decomposition_planner.py` (+ a `call_graph_blast.py` leaf if extracted) + their tests. It MUST NOT touch `candidate_generator.py` / `orchestrator.py` / `transport_circuit_breaker.py` (Track A's files).

## 6. Test strategy
- Unit: `symbol_blast_radius` over a fake oracle (symbol with few callers → low; hub symbol → high/capped; unresolved → None). Propagation: `decompose_for_block` populates `scoped_symbols`; envelope stamps evidence. Advisor: symbol-scoped op with few callers → blast below threshold → not BLOCKED; same file without scope → file-level high → BLOCKED (proves the sharpening). OFF byte-identical (master off → file-level). Fail-soft (oracle raises → file-level).
- Interaction: a BLOCKed whole-file GOAL → decompose → symbol-scoped mutation sub-goal now measures call-graph blast → clears the veto (the C1 end-to-end the review demanded).

## 7. Phasing
1. SubGoal.scoped_symbols + propagation (stop discarding). 2. `symbol_blast_radius` (oracle callers BFS). 3. Advisor integration + gate. 4. Tests. Converges with Track A under the final Opus cross-cutting review.
