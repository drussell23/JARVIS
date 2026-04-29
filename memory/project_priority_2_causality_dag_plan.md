---
name: Priority 2 Causality DAG + Deterministic Replay — A-Level RSI Critical Path
description: 6-slice scope doc for §26.5.2 Causality DAG + Deterministic Replay; second concrete arc on the post-Phase-12 critical path; promotes phase_capture from per-phase Merkle nodes to a session-spanning navigable graph; fixes L3 fan-out ordinal determinism; no hardcoding, leverages existing decision_runtime + phase_capture + EventChannelServer
type: project
---

Priority 2 Causality DAG + Deterministic Replay — scope doc for the second concrete arc on the post-Phase-12 A-Level RSI Critical Path (PRD §26.5.2).

**Why second:** Priority 1 (Confidence-Aware Execution) gave the system internal awareness of its own epistemic state. Priority 2 makes that awareness *historically navigable* — every decision becomes a node with explicit causal predecessors, every confidence-drop a graph edge to the GENERATE round it aborted, every HypothesisProbe verdict a counterfactual fork. Without this substrate, drift detection is heuristic, time-travel debugging is impossible, and Pass C's Adaptive Anti-Venom (Priority 3) has nowhere to mine evidence-graph diffs from.

**How to apply:** treat as the load-bearing arc gating Priority 3 (Adaptive Anti-Venom). Each slice gets its own master flag default false; per-slice 3-clean-session graduation cadence; AST-pinned authority invariants; full-revert matrix. No hardcoding — every threshold (max_depth, max_records, drift_threshold, ordinal_namespace) lives in FlagRegistry, posture-relevant where applicable.

**Cost contract preservation (load-bearing throughout all 6 slices):** Causality DAG is read-only over the existing `decisions.jsonl` ledger. No DAG node ever carries an escalation directive; only diagnostic data. Replay-from-record respects the same §26.6 four-layer cost contract — replaying a BG op cannot escalate to Claude, the structural guard fires regardless of replay state.

**Composition with Priority 1 (just shipped):**
  * Confidence-drop SSE events become DAG nodes; `parent_record_ids` point to the GENERATE record they aborted
  * HypothesisProbe verdicts (RETRY/ESCALATE/INCONCLUSIVE) become counterfactual fork branches via `counterfactual_of=<original_id>`
  * Sustained-low-confidence trends span multiple ops → DAG cluster detection (Slice 4)

---

## Root problem statement

Today the system stores decisions as a flat append-only JSONL at `.jarvis/determinism/<session>/decisions.jsonl`. Every record carries `record_id`, `op_id`, `phase`, `kind`, `ordinal`, `inputs_hash`, `monotonic_ts`, `wall_ts`. **Records have no explicit causal links.**

The orchestrator `--rerun <session-id>` (Slice 1.4) replays a session linearly from start; it cannot:
  1. Replay forward from an arbitrary record (mid-session fork)
  2. Render the upstream causal tree of any decision
  3. Detect counterfactual branches (HypothesisProbe outputs that didn't materialize)
  4. Compare two sessions' decision graphs for drift

**Three concrete bugs/gaps the existing substrate has:**

1. **Per-process ordinal counter** (`decision_runtime.py:_ordinals: Dict[Tuple, int]`) is bound to a single runtime instance. Under L3 worktree fan-out (W3(6)) multiple worker processes hold independent counters — ordinals collide on `(op_id, phase, kind)` matches across workers. Recorded as known debt in PRD §26.4 + memory `project_phase_b_step2_deferred.md`.

2. **No parent-pointer in record schema** — `inputs_hash` collapses inputs to a content hash, but doesn't preserve which prior records contributed to those inputs. A GENERATE round's inputs include the PLAN output, but the captured record doesn't say "my parent is record-X (the PLAN capture)."

3. **No counterfactual differentiation** — HypothesisProbe outputs are recorded the same way as live-path outputs, with no marker that says "this was a 'what if?' branch, not the actual chosen path." Replay treats them as authoritative.

This scope doc closes all three structurally + ships the navigation surface that makes the DAG operator-visible.

---

## Slice 1 — Schema extension (parent_record_ids + counterfactual_of)

**Goal:** extend `DecisionRecord` schema with optional causal-graph fields; backward-compatible read path; new helper accepts explicit parent IDs at capture time.

**Files extended:**
- `backend/core/ouroboros/governance/determinism/decision_runtime.py` — extend `DecisionRecord` dataclass with optional `parent_record_ids: Tuple[str, ...] = ()` and `counterfactual_of: Optional[str] = None` fields. Update `to_dict()` / `from_dict()`. Old records without the fields parse cleanly with empty defaults.
- `backend/core/ouroboros/governance/determinism/phase_capture.py` — extend `capture_phase_decision(...)` with optional `parent_record_ids`/`counterfactual_of` kwargs; threaded through to the underlying `decide(...)`. When unset, behavior is byte-for-byte identical to pre-Slice-1.

**New module:** none. Pure extension of existing graduated primitives.

**Master flag:** `JARVIS_CAUSALITY_DAG_SCHEMA_ENABLED` default false. When off, the new fields are *ignored at write time* (always written as empty/None) — read path is always tolerant.

**Authority invariants (AST-pinned):**
- Schema extension is purely additive on the JSON shape; old records still parse via `DecisionRecord.from_dict`.
- No new I/O paths; same JSONL append.
- Schema version stays the same — additive backward-compat changes don't bump (consistent with `confidence_capture.SCHEMA_VERSION` policy).

**Tests:** ~25-30 deterministic tests covering: schema round-trip with/without new fields, backward-compat read of pre-Slice-1 records, helper accepts ID lists / coerces malformed input safely, master-off byte-for-byte preservation, AST authority invariants, defensive on None / non-string parent IDs.

**Graduation criterion:** 3 clean soaks where new records optionally carry parent IDs without breaking replay determinism.

**Hot-revert:** single env knob → schema extension ignored at write time → read path stays tolerant.

---

## Slice 2 — Per-worker sub-ordinals (L3 fan-out determinism fix)

**Goal:** namespace ordinals as `(worker_id, sub_ordinal)` so multi-worker concurrent writes to the shared session ledger produce a stable, replayable total order.

**Files extended:**
- `backend/core/ouroboros/governance/determinism/decision_runtime.py` — extend `_ordinals` dict key from `(op_id, phase, kind)` to `(worker_id, op_id, phase, kind)`. Worker-id derived from `os.getpid()` XOR a worktree-path hash so it's deterministic per worker but unique across workers. Total order across the session is the lexicographic compare `(wall_ts, worker_id, sub_ordinal)`.
- `backend/core/ouroboros/governance/worktree_manager.py` (existing W3(6) primitive) — expose a `worker_id_for_path(path)` helper that decision_runtime imports. NO orchestrator/policy imports added.

**New module:** none.

**Master flag:** `JARVIS_DAG_PER_WORKER_ORDINALS_ENABLED` default false. Shadow mode when on but enforce sub-flag off: dual-write old + new ordinal columns. Slice 6 graduation flips both.

**Sub-flag:** `JARVIS_DAG_PER_WORKER_ORDINALS_ENFORCE` default false. When on, the new ordinal scheme is authoritative for replay. Mirrors the Slice 5 Arc B + Priority 1 Slice 2 shadow→enforce pattern.

**Authority invariants (AST-pinned):**
- worker_id derivation is pure (no I/O during ordinal assignment)
- worktree_path hash is one-way; doesn't leak path content
- Total-order invariant: under shadow mode, old + new ordinal both monotonically increase per their respective namespaces

**Tests:** ~30-35 covering: 4-worker concurrent write stress (1000 records each), total-order stability across worker boundaries, replay determinism preserved across L3 fan-out, worker_id derivation (pure / deterministic), ordinal_key collision prevention (synthesized clash test), AST authority pins.

**Graduation criterion:** 3 clean soaks under L3 fan-out (`JARVIS_GOVERNED_L3_ENABLED=true` + worktree subagents active) producing a session ledger that replays deterministically with `--rerun <session-id>`.

**Hot-revert:** two independent env knobs (master + enforce) → fall back to per-process ordinals.

---

## Slice 3 — DAG construction primitive (read-time graph builder)

**Goal:** pure-data graph builder reads the JSONL ledger and produces a navigable DAG. Bounded by record count + max_depth so traversal never explodes.

**Files extended:** none modified. New module ships standalone.

**New module:** `backend/core/ouroboros/governance/verification/causality_dag.py`
- `CausalityDAG` class — frozen + hashable; holds `nodes: Dict[record_id, DecisionRecord]` + `edges: Dict[record_id, Tuple[str, ...]]` (parent links) + reverse-edge index for child queries.
- `build_dag(session_id: str, max_records: Optional[int] = None) -> CausalityDAG` — reads the ledger, builds the DAG. Bounded by `JARVIS_DAG_MAX_RECORDS` (default 100_000).
- `node(record_id) -> Optional[DecisionRecord]` — O(1) lookup.
- `parents(record_id) -> Tuple[str, ...]` — O(1) lookup.
- `children(record_id) -> Tuple[str, ...]` — O(1) lookup via the reverse-edge index.
- `subgraph(record_id, max_depth=8) -> CausalityDAG` — bounded BFS upstream + downstream; max_depth env-tunable.
- `counterfactual_branches(record_id) -> Tuple[str, ...]` — list child records where `counterfactual_of==record_id`.
- `topological_order() -> Tuple[str, ...]` — Kahn's algorithm; raises on cycle (DAG invariant; cycles indicate ledger corruption).
- `cluster_kind(records: Sequence[DecisionRecord]) -> str` — heuristic clustering (e.g., "confidence_collapse_cluster" when ≥3 confidence_drop nodes share a recent ancestor).

**Master flag:** `JARVIS_CAUSALITY_DAG_QUERY_ENABLED` default false. When off, `build_dag()` returns an empty `CausalityDAG` immediately — no I/O, no parsing.

**Knobs (FlagRegistry-typed):**
- `JARVIS_DAG_MAX_RECORDS` default 100_000
- `JARVIS_DAG_MAX_DEPTH` default 8 (subgraph traversal cap)
- `JARVIS_DAG_DRIFT_NODE_DELTA_THRESHOLD` default 0.30 (Slice 4 drift detection)

**Authority invariants (AST-pinned):**
- No imports of orchestrator / phase_runners / candidate_generator / iron_gate / change_engine / policy / semantic_guardian / providers / urgency_router (cost-contract isolation).
- Pure stdlib + `verification.*` + `determinism.*`. Reads JSONL via stdlib `pathlib` + `json`.
- NEVER raises; cycle detection returns empty DAG with diagnostic.
- Read-only over the ledger; never modifies records.
- Bounded by max_records + max_depth — no unbounded traversal.

**Tests:** ~35-40 deterministic + a synthetic-DAG fixture suite covering: graph correctness (parents/children/subgraph), bounded traversal (max_depth respect), counterfactual branch detection, topological order on a chain + diamond + tree, cycle detection (returns empty + logs), empty/missing ledger handling, master-off short-circuits, AST authority invariants, posture-relevant cluster detection.

**Graduation criterion:** 3 clean soaks where `build_dag()` runs on production session ledgers (sizes 100-10_000 records) without exceeding 200ms wall time.

**Hot-revert:** single env knob → `build_dag()` returns empty DAG → all queries safely return None / empty tuple.

---

## Slice 4 — Navigation surface (REPL + IDE GET + SSE)

**Goal:** make the DAG operator-visible via three composing surfaces — `/dag` REPL, IDE GET endpoints, SSE event class for fork detection.

**Files extended:**
- `backend/core/ouroboros/governance/postmortem_observability.py` — new `dag` family of subcommands integrated into the existing `/postmortems` REPL dispatcher (or split into `/dag` REPL — operator decides; default plan: extend `/postmortems` for discoverability).
- `backend/core/ouroboros/governance/ide_observability.py` (existing GET surface) — register `GET /observability/dag/{session_id}` (full DAG summary) + `GET /observability/dag/record/{record_id}` (subgraph). Same loopback-only + rate-limited contract.
- `backend/core/ouroboros/governance/ide_observability_stream.py` — new `EVENT_TYPE_DAG_FORK_DETECTED` for counterfactual fork emission events.

**New module:** `backend/core/ouroboros/governance/verification/dag_navigation.py`
- `render_dag_for_record(dag, record_id, depth=4) -> str` — ASCII tree renderer; bounded.
- `render_dag_drift(dag_a, dag_b) -> str` — node-set delta + structural-distance metric.
- `publish_dag_fork_event(record_id, counterfactual_id, ...) -> Optional[str]` — SSE publisher; advisory only.

**Master flag:** `JARVIS_DAG_NAVIGATION_ENABLED` default false. Three independent sub-flags for the three surfaces (REPL / GET / SSE) so operator can selectively enable.

**REPL subcommands:**
- `/postmortems dag for-record <record_id>` — render upstream + downstream tree (max_depth=4)
- `/postmortems dag fork-counterfactuals <record_id>` — list counterfactual branches
- `/postmortems dag drift <session-a> <session-b>` — pairwise graph diff
- `/postmortems dag stats` — DAG aggregates (node count, edge count, fork count)

**Authority invariants (AST-pinned):**
- REPL extension is read-only (matches existing `/postmortems` contract)
- GET endpoints honor the existing IDEStreamRouter contract: loopback-asserted, rate-limited, CORS allowlist
- SSE publisher: master-flag-gated, never raises, payload size bounded

**Cost contract preservation:**
- DAG navigation is read-only over the ledger
- DAG nodes never carry route changes; SSE events are advisory only
- AST-pinned `dag_navigation.py` cannot import provider/router modules

**Tests:** ~40-45 covering: REPL dispatch for all 4 dag subcommands, GET endpoint shape (200/403/429), SSE event fires on counterfactual detection, render_dag_drift correctness, render_dag_for_record bounded depth, master-off short-circuits, three sub-flag independence, AST authority invariants.

**Graduation criterion:** 3 clean soaks with operator successfully navigating the DAG via each of the 3 surfaces; counterfactual fork SSE fires at expected frequency.

**Hot-revert:** four env knobs (master + 3 sub-flags) → independent revert paths.

---

## Slice 5 — Replay-from-record (`--rerun-from`)

**Goal:** extend `scripts/ouroboros_battle_test.py` with `--rerun-from <record-id>` — load session ledger up to record, restore entropy/clock state from that record, dispatch forward.

**Files extended:**
- `scripts/ouroboros_battle_test.py` — add `--rerun-from` argparse arg; when set, load the session ledger via `causality_dag.build_dag(session_id)`, find the target record, restore entropy/clock to its captured state, and dispatch the orchestrator from that point forward.
- `backend/core/ouroboros/governance/determinism/phase_capture.py` — extend `capture_phase_decision(...)` to support a "fork-from" mode where the captured record's `counterfactual_of` field is automatically set to the original record_id when running under `--rerun-from`.

**New module:** none.

**Master flag:** `JARVIS_DAG_REPLAY_FROM_RECORD_ENABLED` default false.

**Cost contract preservation:**
- Replay-from-record respects the same §26.6 four-layer cost contract — a replayed BG op cannot escalate to Claude regardless of replay state.
- The replay path goes through the same orchestrator + candidate_generator + provider stack — all four layers fire identically.
- AST-pinned: the replay extension cannot bypass `assert_provider_route_compatible` or the advisor's structural guard.

**Authority invariants (AST-pinned):**
- The replay extension imports nothing new beyond what `--rerun` already imports
- The orchestrator is dispatched through the same entry point — no shortcut paths
- Counterfactual records are written with `counterfactual_of` set, so the original session's records are NOT corrupted

**Tests:** ~30-35 covering: round-trip replay (replay → produce identical state), counterfactual fork persistence, missing/invalid record_id handling (fail loud), entropy/clock state restoration correctness, multi-fork from same record (idempotent), AST authority invariants, cost-contract preservation under replay (synthetic test).

**Graduation criterion:** 3 clean soaks where operator successfully forks 5+ counterfactual branches from a real session, validates the original session's ledger is unchanged.

**Hot-revert:** single env knob → `--rerun-from` argparse arg returns "feature disabled" → fall back to `--rerun`.

---

## Slice 6 — Graduation flip + AST authority invariants

**Goal:** flip 7 master flags (5 slice masters + 2 enforce sub-flags from Slice 2) default false→true; add 4 new shipped_code_invariants seeds; register 7 flags in FlagRegistry; pre-graduation pin renames.

**Master flags flipped (7 total):**
- `JARVIS_CAUSALITY_DAG_SCHEMA_ENABLED` (Slice 1) → true
- `JARVIS_DAG_PER_WORKER_ORDINALS_ENABLED` (Slice 2 master) → true
- `JARVIS_DAG_PER_WORKER_ORDINALS_ENFORCE` (Slice 2 enforce) → true
- `JARVIS_CAUSALITY_DAG_QUERY_ENABLED` (Slice 3) → true
- `JARVIS_DAG_NAVIGATION_ENABLED` (Slice 4) → true
- `JARVIS_DAG_REPLAY_FROM_RECORD_ENABLED` (Slice 5) → true

**Files extended:**
- `meta/shipped_code_invariants.py` — add 4 new structural invariants:
  1. `causality_dag_no_authority_imports` — Slice 3 read-only AST pin (no orchestrator/policy/iron_gate/providers imports).
  2. `causality_dag_bounded_traversal` — `subgraph()` body MUST contain a `max_depth` parameter check + early termination (AST pinned).
  3. `dag_navigation_no_ctx_mutation` — `dag_navigation.py` MUST NOT call any `ctx.advance(...)` / `ctx.with_*` / mutation method (read-only contract).
  4. `dag_replay_cost_contract_preserved` — `scripts/ouroboros_battle_test.py` `--rerun-from` path MUST go through the orchestrator entry point (no shortcut bypass of the §26.6 four-layer defense).
- `flag_registry.py` (via `flag_registry_seed.py`) — register all 7 DAG flags with category=OBSERVABILITY (or appropriate), posture-relevance RELEVANT, examples + descriptions.
- Pre-graduation pin renames in 5 owner suites per the embedded discipline.

**Layered evidence target:** ~150-180 deterministic tests + 4 in-process live-fire smoke checks + ~30 graduation pins + 3 clean soak sessions.

**Hot-revert:** single env knob per slice → independent revert paths.

---

## Sequencing summary

| Slice | Depends on | Ship after | Ships in parallel with |
|---|---|---|---|
| 1 — Schema extension | none | — | — |
| 2 — Per-worker ordinals | none (independent of Slice 1; both extend `decision_runtime.py`) | — | Slice 1 (independent files within the same module) |
| 3 — DAG construction primitive | Slice 1 (consumes parent_record_ids) | Slice 1 graduates | Slice 2 (no dependency, but Slice 2 fix improves DAG quality under L3 fan-out) |
| 4 — Navigation surface | Slice 3 | Slice 3 graduates | — |
| 5 — Replay-from-record | Slice 1 + Slice 3 | both graduate | Slice 4 |
| 6 — Graduation flip | Slices 1–5 graduated | Slice 5 graduates | — |

**Estimated wall-clock:** ~1.5 weeks with focused execution + soak windows.

---

## Anti-pattern checklist (reject if any present)

- [ ] Hardcoded max_depth (must live in FlagRegistry)
- [ ] Hardcoded max_records (must be FlagRegistry-tunable)
- [ ] Hardcoded drift threshold (must be posture-relevant)
- [ ] Synchronous blocking on DAG build (must read JSONL incrementally where possible)
- [ ] Mutation of DecisionRecord on read (DAG construction MUST be read-only)
- [ ] BG/SPEC → STANDARD/COMPLEX/IMMEDIATE escalation path in replay-from-record (cost contract violation; AST-pinned reject)
- [ ] Provider-module import in causality_dag.py / dag_navigation.py (cost-contract isolation broken)
- [ ] Duplicating the `--rerun` infrastructure (must extend existing primitive)
- [ ] New module that could live as extension of `decision_runtime.py` / `phase_capture.py` / `postmortem_observability.py`
- [ ] Test that asserts on internals not contract (Iron Gate § discipline)

---

## Reverse Russian Doll alignment

- **Outer shell expansion:** the system can now navigate its own past + fork counterfactuals from any decision. The shell genuinely expands — replay-from-record makes time-travel debugging real, and counterfactual forks let the system reason about what-could-have-been without committing to it.
- **Anti-Venom proportional scaling:** every DAG node is read-only by construction (Slice 3 AST-pinned), every replay path goes through the orchestrator's existing four-layer cost-contract defense (Slice 5 AST-pinned), every counterfactual fork is marked as such (Slice 1 schema field) so it cannot pollute the live session's ledger. Defense-in-depth scales proportionally.
- **Order-2 readiness:** the DAG schema (parent_record_ids + counterfactual_of) is itself an Order-2 governance object — extending the schema requires Pass B Slice 6.2 amend-queue authorization. Pass C's MetaAdaptationGovernor (Priority 3, gated) will mine drift across DAG sessions for adaptation evidence.
- **No hardcoding:** every threshold, max_depth, and namespace knob lives in FlagRegistry; posture-relevance assigned where applicable; AdaptationLedger adjusts within Pass C's monotonic-tightening invariant when unblocked.

---

## Composition with shipped infrastructure

| Existing primitive | How Priority 2 leverages it |
|---|---|
| `decision_runtime.DecisionRecord` (Phase 1 Slice 1.2) | Schema extended with two optional fields (Slice 1) |
| `decision_runtime._ordinals` dict (Phase 1 Slice 1.2) | Key namespace expanded to include worker_id (Slice 2) |
| `phase_capture.capture_phase_decision()` (Phase 1 Slice 1.3) | Threaded `parent_record_ids` + `counterfactual_of` kwargs (Slice 1) |
| `decisions.jsonl` ledger | Read source for DAG construction (Slice 3) — never modified |
| `--rerun <session-id>` (Phase 1 Slice 1.4) | Extended with `--rerun-from <record-id>` (Slice 5) |
| `EventChannelServer` IDE GET surface | New endpoint pair `/observability/dag/{session_id}` + `/dag/record/{id}` (Slice 4) |
| `IDEStreamRouter` SSE broker | New `EVENT_TYPE_DAG_FORK_DETECTED` event (Slice 4) |
| `/postmortems` REPL dispatcher (§25 Priority D) | New `dag` family of subcommands (Slice 4) |
| `worktree_manager.py` (W3(6)) | `worker_id_for_path` helper imported by Slice 2 |
| `confidence_observability` (Priority 1 Slice 4) | Confidence-drop SSE events become DAG nodes; HypothesisProbe verdicts become counterfactual forks |
| `cost_contract_assertion.py` (§26.6.2) | Replay-from-record path goes through the same dispatch boundary; no shortcut |
| `shipped_code_invariants.py` (Priority 1 Slice 5 + §26.6.1) | 4 new invariant seeds added in Slice 6 graduation |

Zero duplication — every slice extends a graduated primitive or composes with one.

---

## Pass C unlock dependency

Priority 3 (Adaptive Anti-Venom, PRD §26.5.3) is gated on Pass B Slice 1 (Order-2 manifest cage). Once Pass B Slice 1 ships, Pass C's MetaAdaptationGovernor consumes:
  * **Per-trajectory drift** — Slice 4's `dag drift <session-a> <session-b>` graph diff is the substrate. Drift-driven adaptation needs the DAG to identify where two sessions' decision trees diverged.
  * **Counterfactual evidence** — Slice 5's replay-from-record produces counterfactual branches. Pass C's adaptation rules can mine these for "what would have happened if?" evidence to tighten constraints preemptively.

Priority 2 + Priority 3 together turn drift detection from heuristic ("strategic_drift_ratio > 0.1") into structural ("DAG isomorphism distance under HARDEN posture exceeds adaptation-allowed threshold"). This is the substrate that makes Order-2 self-rewriting safe by construction.

---

## What this scope doc explicitly does NOT prescribe

- **A new persistence backend** — the existing JSONL ledger is sufficient. Don't migrate to SQLite / sled / RocksDB without explicit operator authorization.
- **Real-time DAG mutation** — DAG construction is read-time; the ledger is append-only. No "live DAG" daemon.
- **Cross-session DAG aggregation by default** — Slice 4 `dag drift` is pairwise on operator request; no automatic multi-session aggregation (memory cost would be unbounded).
- **DAG-driven mutation of ctx.route** — DAG events are advisory only (Slice 4). Cost contract preservation is non-negotiable.
- **Replay-from-record as an autonomous capability** — Slice 5 ships the operator-visible CLI flag only. Autonomous counterfactual exploration is Pass C's responsibility (when unblocked).
