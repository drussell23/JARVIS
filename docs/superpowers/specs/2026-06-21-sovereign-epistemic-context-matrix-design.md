# Sovereign Epistemological Context Matrix — Design Specification

> **Arc codename:** Sovereign Epistemological Context Matrix (the Sovereign Memory Engine)
> **Date:** 2026-06-21
> **Author:** Derek J. Russell + O+V architect
> **Branch:** `fleet/sovereign-epistemic-context-matrix`
> **PRD anchor:** OUROBOROS_VENOM_PRD — Execution Frontier (heavy multi-file GOAL execution + long-term memory). M11 ActionOutcomeMemory arc, fused with the Venom tool-loop seventh-layer fix.

---

## 1. Problem Statement

O+V's autonomous loop reliably reaches `state=applied` on light/single-file ops, but a clean **heavy multi-file GOAL** `state=applied` has never been achieved end-to-end. The blocking failure is the "seventh layer": **`tool_loop_deadline_exceeded`** — Venom (the multi-turn tool loop) burns its generation deadline re-discovering the codebase from scratch (`read_file`/`search_code`/`get_callers` rounds) *every op*, then cascades to a dead Claude fallback and dies `deadline_exhausted`.

This is **partly compute** (now mitigated: GCP Spot `e2-custom-8-16384` replaces the starved 2-CPU M1) and **partly architectural** (unbounded, memory-less exploration). GCP is necessary, not sufficient. The architectural fix is to **collapse exploration time-complexity from O(N) brute-force search to directed semantic retrieval**, backed by durable long-term memory, while never weakening the governance cage.

### The core insight
The long-term-memory arc and the heavy-GOAL-execution arc are **the same arc**. Memory that lets Venom recall codebase structure and prior action outcomes — instead of re-exploring — *is* the mechanism that makes a heavy GOAL fit inside the deadline. We attack the deadline first (feed Venom), then enrich generation.

---

## 2. Goals / Non-Goals

### Goals
- **G1.** Heavy multi-file GOALs reach `state=applied` without `tool_loop_deadline_exceeded` by reducing exploration cost via directed pre-fetch.
- **G2.** Replace unbounded exploration with an **Information-Gain Governor**: terminate exploration on semantic information-gain decay, not just wall-clock — with a controlled, native handoff to GENERATE.
- **G3.** Never weaken the Iron Gate. Memory *seeds* exploration; it never *bypasses* mandatory verification reads of live file state.
- **G4.** Wire the dormant `ActionOutcomeMemory` primitive into the GENERATE prompt (Phase 2).
- **G5.** Zero duplication: compose `oracle.py`, `state_drift.py`, the existing Venom budget machinery, and `EmbeddingService`. Build only the genuinely-new seams.

### Non-Goals
- **NG1.** M9 (CuriosityGradient) / M10 (ArchitectureProposer) — sequenced *after* this arc proves heavy-GOAL execution.
- **NG2.** New embedding backends or vector stores — reuse oracle's `EmbeddingService` / ChromaDB.
- **NG3.** Replacing the deadline. The wall-clock cap remains as a hard backstop; the Governor's job is to converge *before* it fires.
- **NG4.** Any change to risk-tier gating, OCA boundary, or merge authority. Cage-preserving throughout.

---

## 3. Reuse Inventory (zero-duplication baseline)

| Capability | Existing asset | Anchor | Use |
|---|---|---|---|
| DAG pre-fetch (structural + semantic fusion) | `oracle.get_fused_neighborhood(file_paths, query, k_semantic)` | `oracle.py:4163` | Pre-fetch candidate DAG |
| Semantic file ranking | `oracle.get_relevant_files_for_query(query, limit)` / `query_relevant_nodes` | `oracle.py:4281`, `3903` | Candidate ranking |
| Dependency DAG | `get_dependencies`/`get_dependents`/`get_blast_radius`/`_file_index` | `oracle.py:3758-3783`, `869` | DAG edges |
| Oracle readiness gate | `is_semantic_ready()` / `wait_until_ready(scope)` | `oracle.py:2240-2266` | Graceful degrade |
| Embedder (no new dep) | `EmbeddingService` singleton | `oracle.py:1719`, `backend/core/embedding_service.py` | Δ approximation + async deep embed |
| **Truth Guard precedent** | `state_drift.detect_drift()` / `should_block_apply()` | `state_drift.py:57-109` | sha256 live-vs-stored compare |
| Hash snapshot shape | `op_context.generate_file_hashes: Tuple[(relpath, sha256hex)]` | `op_context.py:1057` | Manifest shape to mirror |
| Venom main loop + deadline | `ToolLoopCoordinator.run()`; deadline raise | `tool_executor.py:5728`, `5646` | Governor seat |
| Per-round observer hook | `per_round_observer(round_index)` | `tool_executor.py:6554-6569` | Governor invocation point |
| Existing exploration budgets | `_cumulative_explore_calls`, `_convergence_call_threshold` (14), `scale_convergence_threshold` (Slice 237), `_explore_only_rounds` | `tool_executor.py:5699-5710`, `6387-6403` | Elastic budget substrate |
| Graceful "stop exploring" nudge | final-write nudge path | `tool_executor.py:5940-6011` | Controlled handoff |
| Live context compaction | `_compact_prompt()` (Gap #8) | `tool_executor.py:6430` | Seed-budget guard |
| Iron Gate floors (score-based) | `_DEFAULT_FLOORS` per complexity | `exploration_engine.py:374-431` | Deadlock-breaker target |
| Category→tool map | `_TOOL_CATEGORY` | `exploration_engine.py:59-78` | Deadlock-breaker directive synthesis |
| ActionOutcomeMemory primitive (dormant) | M11 Slice 1 primitive | (defined, not wired) | Phase 2 retrieval |
| GENERATE prompt builder (volatile tail) | `_build_lean_codegen_prompt()`; episodic inject at `~2624` | `providers.py:2525-2850` | Phase 2 injection site |
| Heavy-GOAL signal | `len(ctx.target_files) > 1` OR `blast_radius > OUROBOROS_BLAST_RADIUS_THRESHOLD` (default 5) | `unified_intake_router.py:399`, `risk_engine.py:104-145` | Pre-fetch trigger |

**Net new code:** two new modules (`epistemic_prefetch.py`, `context_governor.py`), one new `OperationContext` field (`prefetch_manifest`), one new `tool_executor.run()` parameter (`prefetched_candidates`), and the Phase-2 ActionOutcomeMemory retrieval wire. Everything else is composition.

---

## 4. Architecture

```
Heavy GOAL ingested
        │
        ▼
 CONTEXT_EXPANSION ──(ctx.target_files + expanded_files finalized)
        │
        ▼
 ┌─────────────────────────────────────────────────────────┐
 │ epistemic_prefetch.py  — THE DAG ROUTER (async)          │
 │  • gate: heavy GOAL + oracle.is_semantic_ready()         │
 │  • oracle.get_fused_neighborhood(target_files, goal)     │
 │  • rank → bounded top-K candidate DAG                    │
 │  • Truth Guard: sha256 each candidate (atomic read)      │
 │  • write ctx.prefetch_manifest (immutable tuple)         │
 └─────────────────────────────────────────────────────────┘
        │  prefetched_candidates (validated, bounded)
        ▼
 GENERATE → Venom ToolLoopCoordinator.run(prefetched_candidates=…)
        │
        │  (each round)
        ▼
 ┌─────────────────────────────────────────────────────────┐
 │ context_governor.py — INFORMATION-GAIN GOVERNOR          │
 │  (wired into per_round_observer)                         │
 │  • lightweight Δ (TF-IDF / cached-cosine; deep embed     │
 │    async, never blocks the observer)                     │
 │  • elastic budget: warm cache → compress; cold → expand  │
 │    only while Δ stays high                               │
 │  • on Δ-decay:                                           │
 │      ├─ Iron Gate floor MET → fire graceful nudge        │
 │      └─ floor NOT met → ACTIVE DEADLOCK BREAKER:         │
 │           parse missing categories, inject zero-shot     │
 │           directive to fetch exactly those, THEN nudge   │
 │  • NEVER raises tool_loop_deadline_exceeded              │
 └─────────────────────────────────────────────────────────┘
        │  controlled handoff
        ▼
 patch emitted → VALIDATE → GATE → APPLY (state_drift verify) → VERIFY → COMPLETE
```

Phase 2 (after Phase 1 cures the deadline): dormant `ActionOutcomeMemory` retrieval injected into `_build_lean_codegen_prompt` volatile tail.

---

## 5. Component Specifications

### 5.1 `epistemic_prefetch.py` (NEW) — The DAG Router

**Responsibility:** On a heavy GOAL, asynchronously build a bounded, validated candidate-file DAG from oracle so Venom starts *directed*, not blind.

**Public interface:**
```python
async def build_prefetch_manifest(
    ctx: OperationContext,
    oracle,                      # injected; the booted TheOracle (or None)
    *,
    goal_text: str,
    top_k: int,                  # bounded seed size (env JARVIS_EPISTEMIC_PREFETCH_TOPK, default 8)
) -> tuple[PrefetchEntry, ...]:  # immutable; () when disabled / oracle cold / not heavy
    ...

@dataclass(frozen=True)
class PrefetchEntry:
    rel_path: str
    sha256: str                  # "" if unreadable at snapshot time
    relevance: float             # oracle fused score
    category_hint: str           # which Iron Gate category this read would satisfy
    content_excerpt: str         # bounded; for seeding (empty if seed-budget exhausted)
```

**Algorithm:**
1. **Gate:** return `()` unless `JARVIS_EPISTEMIC_PREFETCH_ENABLED` (default true) AND heavy GOAL (`len(target_files) > 1` OR `blast_radius > threshold`) AND `oracle is not None and oracle.is_semantic_ready()`.
2. `neighborhood = await oracle.get_fused_neighborhood(ctx.target_files, goal_text, k_semantic=top_k)`.
3. Rank by fused score; truncate to `top_k`.
4. For each candidate: **atomic snapshot** (§6.3) → `(rel_path, sha256, excerpt)`.
5. Stamp `category_hint` from `_TOOL_CATEGORY` so prefetch *credits* Iron Gate categories.
6. Return immutable tuple. **Fail-soft:** any exception → log + return `()`.

**Wiring:** invoked after CONTEXT_EXPANSION advance (`orchestrator.py:~3294`) via `asyncio.create_task` so it overlaps PLAN; awaited (with a short bounded timeout) before GENERATE dispatch. Result stored on `ctx` via `dataclasses.replace` (ctx is frozen) into the new field `prefetch_manifest: Tuple[PrefetchEntry, ...] = ()` on `OperationContext`.

**Seed budget:** total excerpt bytes bounded by `JARVIS_EPISTEMIC_PREFETCH_SEED_BYTES` (default 24_000) so seeding never trips Venom compaction at `tool_executor.py:6430`. Over-budget candidates carry `content_excerpt=""` (still ranked + hashed for the Truth Guard, just not seeded).

---

### 5.2 `context_governor.py` (NEW) — The Information-Gain Governor

**Responsibility:** Decide, after each Venom round, whether continued exploration yields enough new information to justify the budget — and force a *mathematically safe* handoff when it does not.

**Public interface:**
```python
class InformationGainGovernor:
    def __init__(self, *, embed_service, iron_gate_floors, prefetch_manifest, enabled: bool): ...

    def observe_round(
        self,
        round_index: int,
        round_tool_results: list,        # this round's tool outputs
        exploration_ledger,              # current category coverage + score
    ) -> GovernorVerdict:
        """Pure, fast, synchronous. NEVER blocks on a deep embed."""

@dataclass(frozen=True)
class GovernorVerdict:
    action: str          # "continue" | "converge" | "deadlock_break"
    info_gain: float     # lightweight Δ, [0,1]
    budget_scale: float  # multiplier applied to convergence threshold
    missing_categories: tuple[str, ...]   # populated only for "deadlock_break"
    directive: str       # zero-shot fetch directive for deadlock_break; "" otherwise
```

**Wiring:** constructed per-op in the orchestrator, passed into `tool_executor.run()`, invoked from the existing `per_round_observer` (`tool_executor.py:6554`). The observer is **pure observer** today (return discarded); we extend the coordinator to honor a returned verdict by (a) appending the existing final-write nudge, or (b) appending the deadlock-break directive. The coordinator still owns the loop; the governor only advises + supplies directive text.

#### 5.2.1 Lightweight Semantic Δ (Constraint 2 — Zero-Latency Governor)
Deep embedding every round is forbidden on the synchronous path.
- **Primary (sync, in-observer):** TF-IDF / hashed-token cosine over the round's new tool-result text vs the accumulated exploration corpus. Pure stdlib + numpy, sub-millisecond, no model call. **The corpus is initialized to the prefetch-DAG excerpts as the round-0 baseline** (LR1) — Round-1 Δ is therefore measured against what memory already supplied, never against an empty cache, so the Governor cannot artificially inflate Information Gain on the first real read.
- **Refinement (async, off-path):** an `asyncio.create_task` deep-embed via `EmbeddingService` *batched* across rounds; its result, when ready, recalibrates the lightweight estimator's threshold for *subsequent* rounds. The observer **never awaits** it.
- `info_gain` ∈ [0,1]. Decay = `info_gain < JARVIS_GOVERNOR_MIN_GAIN` (default 0.15) for `JARVIS_GOVERNOR_DECAY_ROUNDS` (default 2) consecutive rounds.

#### 5.2.2 Cache-Linked Elastic Budget
Wired to the **existing** `_convergence_call_threshold` (14) via a multiplier (we do not replace the threshold):
- **Warm + Truth-Guard-validated prefetch** (manifest non-empty, hashes valid): `budget_scale < 1.0` → compress; converge fast.
- **Cold cache:** `budget_scale > 1.0` → expand, **but only while `info_gain` stays high**. The moment Δ decays, elasticity stops regardless of remaining budget — preventing unbounded wandering on a cold cache.
- Composes with `scale_convergence_threshold` (Slice 237, heavy-op down-scaling): governor multiplier applies *on top of* the heavy-op scale; the effective threshold is `floor(base * heavy_scale * governor_scale)`.

#### 5.2.3 Active Iron Gate Routing (Constraint 1 — The Deadlock Breaker)
On Δ-decay the governor consults the live `exploration_ledger`:
- **Floor satisfied** (`score ≥ min_score` AND `categories ≥ min_categories` AND required-categories covered): verdict `converge` → coordinator fires the existing graceful final-write nudge.
- **Floor NOT satisfied:** verdict `deadlock_break`. The governor:
  1. Computes `missing = required_floor_categories − covered_categories` and the score gap.
  2. Maps each missing category → its canonical tool via `_TOOL_CATEGORY` (CALL_GRAPH→`get_callers`, HISTORY→`git_blame`/`git_log`, STRUCTURE→`list_symbols`, DISCOVERY→`search_code`, COMPREHENSION→`read_file`), targeting the highest-relevance prefetch-manifest paths.
  3. Emits a **strict zero-shot directive** (`directive`) instructing Venom to call exactly those tools on exactly those targets — nothing else — to satisfy the floor immediately.
  4. The coordinator appends the directive for **exactly ONE dedicated bounded round** (its own counter, distinct from the enforcement-round cap — LR3). After that single round the ledger is re-evaluated:
     - **Floor now satisfied** → graceful handoff (the intended path).
     - **Floor STILL unsatisfied** (Venom hallucinated, refused, or failed to fetch the missing categories) → the Governor **MUST NOT loop and MUST NOT fall through to GENERATE_RETRY**. It forcefully fails the operation with terminal `deadlock_override_failed` (a `fatal_governance_error`). This is a hard circuit breaker: we do not tolerate infinite looping at the safety gate.

This *actively bridges* the floor gap rather than passively avoiding it — the loop terminates **mathematically safe** (Iron Gate satisfied) and **never** on `tool_loop_deadline_exceeded`. The only non-success exit is the explicit one-shot circuit-breaker fatal.

#### 5.2.4 Handoff invariant
The governor never raises. The only terminal outcomes it produces are `converge` and `deadlock_break→converge`. The pre-existing wall-clock deadline remains an independent backstop owned by the coordinator (untouched), but in the happy path the governor converges before it can fire.

---

### 5.3 Cryptographic Truth Guard (extends `state_drift.py`)

**Responsibility:** Guarantee no seeded/recalled memory is ever stale relative to live disk.

- **Reuse:** `state_drift`'s sha256 hasher and the `(rel_path, sha256hex)` shape. We do **not** fork a second hasher.
- **At prefetch:** snapshot each candidate's hash into `PrefetchEntry.sha256`.
- **At seed-consumption (Venom loop start) and at any memory-recall use:** re-hash the live file; if it mismatches the stored hash, the entry is **STALE** → discard the seed/recall for that file, force a fresh live `read_file`, and asynchronously refresh the memory node. Memory thus *accelerates which files to read*, never *replaces reading them* — preserving the Iron Gate.

#### 5.3.1 Atomic Hash Verification (Constraint 3 — Race Condition Guard)
Heavy GOALs may run under `ProcessPoolExecutor` (parallel dispatch / L3 worktrees), so a file can be mutated by a sibling unit mid-verify.
- **Atomic read-then-hash:** read the file's bytes **once** and hash *those exact bytes* (no read→stat→re-read window). The hash describes a single atomic snapshot; a concurrent writer either lands before or after, never tears the hash.
- **Cross-process quarantine ledger:** on a detected mismatch, the node is quarantined in a **process-shared invalidation set** so no sibling worker ingests the stale node. Mechanism (honest tradeoff): a small on-disk quarantine ledger at `.jarvis/epistemic_quarantine.jsonl` (append-only, atomic temp+rename, mirrors the `op_context`/`state_drift` `.jarvis` convention) that all workers consult before consuming a memory node. An in-memory `set` is insufficient — it would not cross process boundaries; the on-disk ledger (or a `multiprocessing.Manager` dict when a shared manager is already in scope) is the load-bearing cross-process barrier. Reads are best-effort + fail-open-to-fresh-read (a quarantine-ledger error must never block a legitimate live read).
- **Session-bound TTL + terminal reconciliation (LR2):** every quarantine record is stamped with the current `session_id`; consult-time filtering ignores any record whose `session_id != current` — so **stale memory from a previous soak can never permanently cripple the oracle** (no infinite TTL). On session termination the FSM **flushes/reconciles** the session's quarantine back to the primary oracle store: each quarantined node is re-hashed against live disk and either (a) re-validated + the oracle node refreshed to ground-truth, or (b) dropped. Reconciliation is fail-soft (a flush error leaves the append-only ledger intact for the next boot to filter out by session_id) and reuses the existing `.jarvis` GCS-vault flush path so the reconciliation survives Spot preemption.
- **Invariant:** quarantine only ever *removes trust* from memory; it can never cause a patch to apply against stale content (that path is still guarded independently by `state_drift.should_block_apply()` at APPLY).

---

### 5.4 Phase 2 — ActionOutcomeMemory → GENERATE

**Responsibility:** Inject "what broke the tests last time I touched this file" into the GENERATE prompt.

- Wire the **dormant** M11 primitive's retrieval: `render_action_outcomes_for_region(ctx.target_files)`.
- **Injection site:** `providers.py:_build_lean_codegen_prompt()`, volatile tail, immediately after the episodic block (`~2624`), before the oracle dependency summary (`~2629`). **Volatile tail only** — never `stable_prefix_out` (P2a-safe; outcomes change per op).
- **Gating:** `JARVIS_ACTION_OUTCOME_MEMORY_ENABLED` (default false initially; graduates via the standard cadence).
- **Truth Guard applies:** recalled outcomes for a file whose hash has drifted are suppressed (stale).

---

## 6. Cross-Cutting Concerns

### 6.1 Gating & flags (all fail-soft, default byte-identical when off)
| Flag | Default | Effect |
|---|---|---|
| `JARVIS_EPISTEMIC_PREFETCH_ENABLED` | `true` | DAG Router on (no-op unless heavy GOAL + oracle ready) |
| `JARVIS_EPISTEMIC_PREFETCH_TOPK` | `8` | Candidate DAG size |
| `JARVIS_EPISTEMIC_PREFETCH_SEED_BYTES` | `24000` | Seed budget (compaction guard) |
| `JARVIS_CONTEXT_GOVERNOR_ENABLED` | `true` | Info-Gain Governor on (advisory until proven) |
| `JARVIS_GOVERNOR_MIN_GAIN` | `0.15` | Δ-decay threshold |
| `JARVIS_GOVERNOR_DECAY_ROUNDS` | `2` | Consecutive low-Δ rounds → converge |
| `JARVIS_GOVERNOR_DEADLOCK_BREAKER_ENABLED` | `true` | Active Iron Gate routing on |
| `JARVIS_ACTION_OUTCOME_MEMORY_ENABLED` | `false` | Phase 2 GENERATE injection |

Every component returns control unchanged when its flag is off — the loop behaves exactly as today (verified by byte-identical OFF tests).

### 6.2 Error handling
- **Oracle cold / absent:** prefetch returns `()`; Venom runs exactly as today. Never block GENERATE on oracle.
- **Governor exception:** swallowed in the observer (already exception-safe per `tool_executor.py:6554-6569`); loop falls back to existing budget behavior.
- **Quarantine-ledger I/O error:** fail-open to a fresh live read.
- **Deadlock-breaker can't satisfy floor in its one dedicated round:** terminal `deadlock_override_failed` (`fatal_governance_error`). Hard circuit breaker — no loop, no GENERATE_RETRY fallthrough (LR3). The op fails cleanly and auditably rather than spinning at the safety gate.

### 6.3 Performance / latency
- Per-round governor cost: sub-ms (TF-IDF/cosine on cached vectors). No synchronous model calls — Constraint 2.
- Prefetch overlaps PLAN (async task), bounded-timeout awaited before GENERATE.
- Deep embeds: batched, off the critical path.

### 6.4 Cage / safety invariants (must hold)
- **I1.** Memory seeds exploration; the Iron Gate floor is always satisfied before handoff (actively, via the deadlock breaker).
- **I2.** No patch applies against stale content (Truth Guard at seed-time + `state_drift` at APPLY — defense in depth).
- **I3.** Governor never raises `tool_loop_deadline_exceeded`; wall-clock backstop untouched.
- **I4.** No change to risk tiers, OCA boundary, or merge authority.
- **I5.** All flags fail-soft; OFF = byte-identical legacy behavior.

---

## 7. Phasing

- **Phase 1 (the deadline cure):** `epistemic_prefetch.py` + `context_governor.py` + Truth Guard extension + Venom seed wire + deadlock breaker. Target: heavy GOAL `state=applied`, zero `tool_loop_deadline_exceeded`.
- **Phase 2 (generative enrichment):** ActionOutcomeMemory → GENERATE prompt.
- **After this arc:** M9 / M10 (only once heavy-GOAL execution is reliable).

---

## 8. Test Strategy

- **Unit (pure, fast):** prefetch ranking + bounding; governor Δ math + elastic-budget scaling + decay detection; deadlock-breaker category→tool mapping + directive synthesis; atomic read-then-hash; quarantine ledger append/consult.
- **Interaction:** governor + Iron Gate floor (deadlock breaker satisfies floor → converge, never deadlock GENERATE_RETRY); seed budget vs compaction threshold; warm-vs-cold elastic budget.
- **OFF byte-identical:** each flag off → loop output identical to pre-arc (golden comparison on a recorded heavy-op trace).
- **Fail-soft:** oracle cold, governor raises, quarantine I/O error — all degrade to legacy.
- **Regression spine:** existing exploration-gate, state_drift (Slice 247/248), and tool-loop budget suites stay green.
- **Live validation:** heavy multi-file GOAL soak on GCP Spot (8-CPU) — assert `state=applied`, `tool_loop_deadline_exceeded`=0, governor convergence telemetry present.

---

## 9. Telemetry (Manifesto §7 — absolute observability)
- `[EpistemicPrefetch] op=… heavy=… oracle_ready=… candidates=N seeded=M bytes=B`
- `[ContextGovernor] op=… round=R gain=… scale=… action=continue|converge|deadlock_break missing=[…]`
- `[TruthGuard] op=… file=… verdict=valid|stale quarantined=bool`
- SSE event types added additively (no breaking change to the stream schema).

---

## 10. Locked Resolutions (operator-ratified 2026-06-21 — absolute, not open)

- **LR1 — Prefetch is the Δ round-0 baseline.** The TF-IDF/cosine corpus is seeded with the prefetch-DAG excerpts before Round 1. The Governor measures Round-1 Information Gain against what memory already supplied, never against an empty cache. Rationale: an empty baseline artificially inflates Δ on the first real read and defeats the Governor. Implemented in §5.2.1.
- **LR2 — Quarantine TTL is strictly session-bound, with terminal reconciliation.** Every `.jarvis/epistemic_quarantine.jsonl` record carries the current `session_id`; consult-time filtering ignores foreign-session records (no infinite TTL — stale memory from a prior soak can never permanently cripple the oracle). On session termination the FSM flushes/reconciles the session's quarantine back into the primary oracle store (re-hash → re-validate-and-refresh or drop), fail-soft, over the existing `.jarvis` GCS-vault flush path. Implemented in §5.3.1.
- **LR3 — Deadlock-breaker is a one-shot circuit breaker.** The Active Iron Gate routing gets EXACTLY ONE dedicated bounded round (its own counter, never the enforcement-round cap). If the floor is still unmet after that round (Venom hallucinated / refused / failed), the Governor forcefully terminates the op with `deadlock_override_failed` (`fatal_governance_error`) — it MUST NOT loop and MUST NOT fall through to GENERATE_RETRY. We do not tolerate infinite loops at the safety gate. Implemented in §5.2.3 and §6.2.

These resolutions are binding inputs to the implementation plan; sub-agents implement them verbatim, not by interpretation.
```
