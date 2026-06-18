# Repair Context Bridge — Architecture Design Document

> **Status:** DRAFT for operator review (brainstorm output; not yet a plan).
> **Author:** O+V architecture pass, 2026-06-18.
> **Goal:** Eliminate the *structural blindspot* in the active L2 self-repair loop. Today L2 generates
> fixes **blind to graph structure**; the Oracle's blast-radius/call-chain (now 16 GB-safe via the
> lazy GraphBackend) is never consulted during repair. The Bridge feeds graph topology *into* repair
> generation (steer) and adds a deterministic **structural pre-flight gate** (enforce) — a
> zero-regression shield that makes the existing immune system architecture-aware.

---

## 0. Scope discipline — what this is NOT

This does **not** rebuild the orchestrator, the 11-phase FSM, L2 `repair_engine`, `SemanticGuardian`,
Iron Gate, post-apply VERIFY, or `AutoCommitter` — all of which exist and are production-grade. The
audit (PRD §3.1/§9) confirmed the self-healing pipeline already remediates test failures end-to-end.
The **only** gap is that the repair loop is graph-blind. This ADD fills exactly that gap by
*composing existing primitives*; every new line is glue + one new deterministic gate. Anything that
would duplicate the engine is explicitly out of scope.

### Existing assets we build on (no duplication)
| Need | Existing asset | How used |
|---|---|---|
| Fault signal | `intent/test_watcher.py::TestFailure` + `intent/signals.py::IntentSignal` | **Enriched** (Phase 1) |
| Repair loop + per-iteration feedback | `governance/repair_engine.py` (`run`, `_generate_repair_candidate`, `repair_context` slot, `prev_failure_class`) | **Populated + extended** (Phase 2/3) |
| Graph topology, 16 GB-safe | `oracle_graph_backend.py` (`compute_blast_radius`/`find_call_chain`/`find_circular_dependencies`/`stream_edges`) + `SqliteLazyGraphBackend` + `AdaptiveNodeCache` | **Consulted** (Phase 2/3) |
| AST → nodes/edges parser | `governance/ast_compile_helper.py::analyze_python_source_for_oracle` | **Reused** to parse the candidate (Phase 3) |
| Pre-apply gate pattern | `governance/semantic_guardian.py` (post-VALIDATE/pre-GATE, soft/hard verdicts) | **Mirrored** by the new structural gate |
| Divergence signatures | `governance/failure_classifier.py::patch_signature_hash` | Reused for the structural-divergence signature |

---

## 1. Phase 1 — Deep Semantic Failure Ingestion

**Problem:** `TestFailure(test_id, file_path, error_text)` carries a one-line error, not the failing
call stack — so there are no precise node coordinates to seed blast-radius.

**Design:** enrich the sensor to capture the **full traceback, frame-by-frame, AST-mapped to Oracle
node keys**.
- `test_watcher.py` already reads pytest output; extend the parser to capture the traceback block and
  split it into frames `(abs_file, lineno, func_name)` (pytest prints these deterministically).
- For each frame, **AST-map line → node**: resolve the Oracle node whose span contains `lineno` —
  `nodes_in_file(rel_path)` then pick the node where `line_number ≤ lineno < line_number + line_count`
  (the Oracle already stores `line_number` + `line_count`). The mapped node keys are the
  **functional mutation coordinates** of the failure (the deepest in-repo frame = the prime suspect).
- Extend the signal schema additively (back-compat): `IntentSignal.evidence` gains
  `traceback_frames: list[{file, line, func, node_key|None}]` + `fault_node_keys: list[str]`
  (deepest-first, repo-internal frames only — stdlib/site-packages frames are recorded but never
  used as repair targets).

**Honest bound:** line→node mapping is span-approximate (a node's `line_count` is its symbol span);
overlaps resolve to the innermost span. Frames with no resolvable node (e.g. generated code) carry
`node_key=None` and are skipped as targets. Fail-soft: if traceback parsing fails, the signal
degrades to today's `error_text` (the loop still works, just graph-blind for that op).

---

## 2. Phase 2 — Graph-Informed Cognitive Context (the steer)

**Problem:** L2's GENERATE has a `repair_context` slot (`_generate_repair_candidate(repair_context=…)`,
provider `generate(…, repair_context=None)`) but nothing populates it with topology.

**Design:** a new **`RepairContextBridge`** (`governance/repair_context_bridge.py`) that, before each
L2 GENERATE, builds the **dependency cone** for the fault node(s) and populates `repair_context`:
- Concurrent lazy lookups on the failing coordinates (sub-10 MB via the lazy backend + adaptive
  cache): `compute_blast_radius(fault_key)` (downstream dependents — what this fix could break),
  `get_dependencies(fault_key)` (upstream — what it relies on), and `find_call_chain` between the
  test's entry node and the fault node (the causal path).
- Render a **structured cone** (NOT an embedding vector — "vectorized" here = a deterministic,
  ordered node/edge set; flagged so we don't over-engineer): `{files[], symbols[], call_chain[],
  dependents[], dependencies[]}`, capped (top-K by proximity) to fit the prompt window.
- Inject into `repair_context` as an **explicit boundary clause**: *"Your fix MUST stay within this
  dependency cone — files: […]; do not modify or break: dependents […]."*

**Honest framing — steer vs enforce:** a prompt clause is **advisory** — the model *can* still wander
outside the cone. Phase 2 makes staying-in-cone the path of least resistance; **Phase 3 is what
actually enforces it.** Stating this plainly because "immutable semantic boundary constraint" in a
prompt is not immutable — only the gate is.

**Memory:** the bridge holds no resident graph; it queries the `SqliteLazyGraphBackend` (the
already-proven 7 MB-vs-134 MB footprint) and reuses the `AdaptiveNodeCache` that contracts under
`MemoryPressureGate` pressure.

---

## 3. Phase 3 — Pre-Flight Structural Validation Gate (the Zero-Regression Shield)

**Problem:** even a test-passing fix can introduce a hidden structural regression (a new import
cycle, orphaned dead code, or a severed call-chain other code relies on) that the test for *this*
failure won't catch. `SemanticGuardian` checks AST/regex *patterns*; nothing checks *graph deltas*.

**Design:** a new **`StructuralValidationGate`** (`governance/structural_validation_gate.py`) — a
deterministic, ~10 ms, zero-LLM gate that simulates the candidate's structural delta **before** it's
flushed to disk or signed by `AutoCommitter`:

1. **Parse the candidate** with the *existing* `analyze_python_source_for_oracle` → candidate
   nodes/edges for the changed file(s). (No new parser; same one the Oracle uses, so the extracted
   structure is identical in shape.)
2. **Simulate the cone-scoped delta** (no disk write): take the current edges within the
   blast-radius cone, remove the changed file's old edges, add the candidate's new edges → a
   *what-if* subgraph bounded to the cone (cheap; the cone is small, streamed via `stream_edges`).
3. **Three structural checks on the delta:**
   - **New cycle:** `find_circular_dependencies` on the delta cone returns a cycle absent from the
     pre-fix cone → reject (introduced circular dependency).
   - **New dead code:** a node that had callers/importers pre-fix has *none* post-delta → reject
     (the fix orphaned a referenced symbol).
   - **Severed call-chain:** a call-chain present pre-fix between two cone nodes no longer resolves
     post-delta → reject (structural break elsewhere in the blast radius).
4. **Verdict:** ACCEPT → candidate proceeds to the existing VERIFY/SemanticGuardian/AutoCommit path
   unchanged. REJECT → emit a **structural-divergence signature** (kind + offending node/edge, hashed
   via `failure_classifier.patch_signature_hash`) and **append it to the L2 failure context**, so the
   next iteration's GENERATE is told exactly what structure it broke — the existing L2
   iterate-with-feedback loop drives **autonomous self-correction until structural convergence** (or
   the existing `max_iterations`/`timebox`/no-progress budget stops it; we add no new unbounded loop).

**Composition, not duplication:** this gate is *complementary* to `SemanticGuardian` (pattern checks)
and runs at the same post-GENERATE/pre-APPLY seam (mirroring its integration). It enforces what
Phase 2 only steers. It reuses the parser, the GraphBackend primitives, the lazy backend, the L2
feedback loop, and the failure-classifier — net new code is the simulator + the three delta checks.

**Honest bounds:**
- The check is **cone-scoped** (the blast radius) — by construction the relevant scope, but a
  regression *outside* the cone is out of scope (and would be unrelated to this fix by definition).
- It validates **structure**, not behavior — VERIFY (the scoped test run) remains the behavioral
  authority. This gate is friction *before* the test run, catching structural breaks tests miss.

---

## 4. End-to-end flow (all glue + one new gate)

```
TestFailure (pytest)
  └─ Phase 1: sensor captures traceback frames → AST-maps to fault_node_keys ──► enriched IntentSignal
       └─ UnifiedIntakeRouter (priority 1) ──► Orchestrator FSM ──► VALIDATE fails ──► L2 RepairEngine.run()
            iteration loop (existing budget: max_iters / timebox / no-progress):
              ├─ Phase 2: RepairContextBridge builds cone (lazy blast-radius/call-chain) ──► repair_context
              ├─ GENERATE (existing) with cone clause in repair_context
              ├─ Phase 3: StructuralValidationGate simulates cone-scoped delta
              │     REJECT → append divergence signature to failure context ──► next iteration
              │     ACCEPT ▼
              ├─ VERIFY (existing scoped test run)
              ├─ SemanticGuardian (existing pattern gate) + Iron Gate
              └─ AutoCommitter (existing O+V-signed commit)
```

## 5. Gating & safety
- `JARVIS_REPAIR_CONTEXT_BRIDGE_ENABLED` (Phase 1/2) + `JARVIS_REPAIR_STRUCTURAL_GATE_ENABLED`
  (Phase 3), **default OFF** → L2 behaves exactly as today (byte-identical); graduate after a soak,
  per the arc's discipline.
- Fail-soft everywhere: any bridge/gate error logs + degrades to today's graph-blind repair — the
  immune system must never be *weakened* by the thing meant to strengthen it.
- The structural gate is **advisory-to-hard**: default rejects only on `new_cycle` (hard, unambiguous);
  `new_dead_code`/`severed_call_chain` start as **soft** (logged + fed back, not blocking) until the
  soak shows low false-positive rate, then graduate to hard. (Same earn-trust pattern as
  SemanticGuardian's soft→hard.)

## 6. Testing
- Phase 1: traceback→frames→node-key mapping (incl. span-overlap, stdlib-frame skip, parse-failure
  fallback).
- Phase 2: cone construction parity (bridge cone == direct Oracle blast-radius), prompt-clause shape,
  bounded memory (reuses the lazy soak harness).
- Phase 3: **the core proof** — a candidate that introduces a cycle / orphans a node / severs a
  call-chain is REJECTED with the right signature; a clean fix is ACCEPTED; the divergence signature
  threads into the next L2 iteration (self-correction). All against a REAL aiosqlite graph via the
  existing dual-backend parity harness (zero divergence between in-mem + lazy during the gate).
- Regression: L2 with both flags OFF is byte-identical to today.

## 7. Decomposition (each = spec → plan → build)
1. **Signal enrichment** (Phase 1) — traceback capture + AST line→node mapping; additive schema.
   Independently shippable; smallest.
2. **RepairContextBridge** (Phase 2) — cone builder + `repair_context` population + prompt clause.
3. **StructuralValidationGate** (Phase 3) — the delta simulator + 3 checks + L2 feedback wiring.
   Largest; the zero-regression shield; soft→hard graduation.

## 8. Open questions for the operator
1. **Cone size cap** — top-K nodes/files injected into the prompt (token budget). Starting guess: the
   direct blast-radius + 1-hop dependencies, capped ~50 symbols; tune after the soak.
2. **Severed-call-chain strictness** — should removing *any* pre-existing call-chain in the cone
   reject, or only chains the failing test transitively exercised? (Lean: the latter — fewer false
   positives.)
3. **"Vectorized" cone** — confirm the structured node/edge-set interpretation (recommended) vs an
   actual embedding vector (out of scope; the semantic index already embeds separately).
