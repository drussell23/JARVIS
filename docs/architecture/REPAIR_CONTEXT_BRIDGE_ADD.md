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

**Implemented (Slice 1):** pure mapper `governance/intent/repair_traceback.py`
(`parse_pytest_tracebacks` → `_frames_for_test` correlation → `map_frames_to_nodes` innermost-span
AST map → `build_traceback_map` deepest-first `fault_node_keys`); injectable `NodeResolver`
(production = the Oracle's `_graph._backend`, lazily + fail-soft). Wired into
`intent/test_watcher.py`: `TestFailure.traceback_evidence` (additive field) populated by the async
`_enrich_failures` (mapping offloaded via `asyncio.to_thread` — zero block on the sensor poll loop),
merged into `IntentSignal.evidence` in `process_failures`. Gated `JARVIS_REPAIR_CONTEXT_BRIDGE_ENABLED`
(default OFF → signal evidence byte-identical to pre-bridge). 32 tests (mapper + watcher wiring).

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

**Implemented (Slice 2):** `governance/repair_context_bridge.py` — `RepairCone` (structured,
proximity-ordered node/edge set; *not* an embedding, per §8 Q3) + `RepairContextBridge`.
**Adaptive fault-key resolution** (no hardcoding): Slice 1 `fault_node_keys` (from
`ctx.intake_evidence_json`) → file being repaired (`find_nodes_in_file`) → failing-test functions
(`find_nodes_by_name`) — sharper with Slice 1 on, still works standalone. Composes the shipped Oracle
primitives `compute_blast_radius` (downstream dependents, proximity-ordered: direct then transitive),
`get_dependencies` (upstream), `find_call_chain` (test→fault causal path) — **no new traversal**.
`async build()` self-gates on the master flag and offloads the synchronous lazy-graph query path via
`asyncio.to_thread` (zero block on the L2 loop). Caps env-tunable: `JARVIS_REPAIR_CONE_MAX_SYMBOLS`
(default 50), `JARVIS_REPAIR_CONE_BLAST_DEPTH` (default 2); top-K-by-proximity truncation flagged in
the clause. Injection: additive `RepairContext.dependency_cone` field (default `None` → prompt
byte-identical); populated in `repair_engine._run_inner` via `_build_dependency_cone` (lazy
bridge, fail-soft); rendered in `providers._build_codegen_prompt`'s REPAIR MODE block only when
present. Gated `JARVIS_REPAIR_CONTEXT_BRIDGE_ENABLED` (shared master). 15 bridge tests + 112
repair-engine regression green.

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
   - **Severed call-chain (reachability-gated — see §3.1):** a call-chain present pre-fix between two
     cone nodes no longer resolves post-delta. This is **not** a binary rejection: it is a structural
     break *only if* severing it disrupts reachability from a **system entry point** (an active test
     suite, a `__main__`/loop entry, or any live external caller) to a still-**active** downstream
     component. If the only paths it severs lead to **dead/orphaned subgraphs**, the severance is
     **authorized as valid structural pruning** (the fix legitimately removed reachable-only-from-dead
     code) → ACCEPT + emit non-blocking cleanup telemetry. The Dynamic Reachability Matrix (§3.1)
     decides which case applies.
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

## 3.1 The Dynamic Graph Reachability Matrix (severed-chain adjudication)

**The principle (operator-ratified behavioral rule):** *a severed call-chain is a structural
regression only when it disrupts reachability from a live system entry point to a still-active
downstream component. A chain severed only within a dead or orphaned subgraph is valid structural
pruning, not a break.* This replaces naïve "any removed edge = reject" with intent-aware
adjudication, and is the canonical answer to §8 Q2.

**Entry points (the reachability roots) — derived, never hardcoded:**
- **Active test suites** — the test nodes the Oracle already indexes (files under the configured test
  dirs); for the repairing op specifically, the *failing test node* is always a root.
- **Runtime entries** — `__main__` guards, registered loop/daemon entrypoints, and any node with a
  live external in-edge the Oracle records (e.g. CLI/handler registration).
- These are read from the graph, not a static list — new entrypoints become roots automatically (§5
  intelligence-driven, no hardcoded routing).

**The matrix (computed on the *post-delta* cone, deterministic, zero-LLM):**
For each call-chain `C` that existed pre-fix but no longer resolves post-delta, classify its severed
endpoint(s) by reachability from the entry-point root set `R` over the post-delta graph:

| Downstream endpoint reachable from `R` post-delta? | Endpoint still has *other* live callers? | Verdict |
|---|---|---|
| **Yes** (a live root still reaches it another way) | — | **ACCEPT** — chain rerouted, not broken; reachability preserved |
| **No** | **Yes** (reachable via a different live chain) | **ACCEPT** — local re-wiring; component still active |
| **No** | **No**, but endpoint *was* reachable from `R` **pre-delta** | **REJECT** — *this fix* orphaned a live component → divergence signature → L2 feedback |
| **No** | **No**, and endpoint was **already** dead pre-delta | **ACCEPT (prune)** — severing dead-only code; emit `structural_prune` cleanup telemetry (non-blocking) |

The decisive comparison is **pre-delta vs post-delta reachability of the endpoint from `R`**: REJECT
iff the fix transitions an endpoint from *reachable-from-a-live-root* to *unreachable*. Everything
else (already-dead, rerouted, or still-reachable) is authorized.

**Implementation (reuses shipped primitives, no new traversal engine):**
- Reachability = the GraphBackend's existing `descendants(root)` / `find_call_chain` over the
  cone-scoped *what-if* subgraph (§3 step 2) — run once for the root set pre-delta and once
  post-delta; the endpoint's membership flip in `reachable(R)` is the whole decision. Streamed,
  cone-bounded, ~10 ms, lazy-backend memory profile (the proven 7 MB path).
- `structural_prune` events are fire-and-forget telemetry on the existing observability/SSE surface —
  they record *what dead code the fix legitimately removed* (so cleanup is auditable per §7
  observability), and **never block** the candidate.

**Honest bound:** "dead" here means *graph-unreachable from the live root set* — a component reached
only via reflection / dynamic dispatch / external RPC the Oracle can't see could be misclassified as
dead. Mitigations: (a) the matrix only *prunes* (ACCEPT) on dead-only severance, so a
misclassification is a missed-friction event, never a false REJECT that blocks a good fix; (b) VERIFY
(the behavioral authority) still runs; (c) `structural_prune` telemetry makes every authorized
pruning visible for human audit. Like the other two checks, severed-chain starts **soft** (logged +
fed back, non-blocking) and graduates to hard-REJECT-on-live-orphan only after the soak shows a low
false-positive rate.

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
2. **Severed-call-chain strictness** — **RESOLVED (see §3.1, operator-ratified).** Not binary: the
   Dynamic Graph Reachability Matrix rejects *only* when a fix flips a downstream component from
   reachable-from-a-live-entry-point to unreachable; a chain severed only within a dead/orphaned
   subgraph is authorized as valid structural pruning (ACCEPT + non-blocking `structural_prune`
   cleanup telemetry). Entry-point roots (active test suites incl. the failing test, `__main__`/loop
   entries, live external callers) are derived from the graph, never hardcoded.
3. **"Vectorized" cone** — confirm the structured node/edge-set interpretation (recommended) vs an
   actual embedding vector (out of scope; the semantic index already embeds separately).
