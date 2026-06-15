# Sovereign Evidence Rail & L3 Memory Governor

**Date:** 2026-06-14
**Author:** Derek J. Russell (O+V Trinity Architect)
**Status:** DESIGN — awaiting Sovereign Authorization before implementation
**Spec ID:** sovereign-evidence-rail

---

## 1. Problem Statement

The O+V multi-agent fleet runs four graduated subagents. Two of them —
**REVIEW** and **PLAN** — operate in *shadow mode*: they execute on every
qualifying op, emit telemetry, and their output is **discarded**. The legacy
deterministic paths (SemanticGuardian/GATE for review; the flat-list
`PlanGenerator` for plan) remain authoritative.

We want to graduate REVIEW and PLAN to **authoritative** status — but only on
**verified evidence**, never on a hardcoded flag flip. A diagnostic audit of the
current telemetry established three load-bearing facts:

1. **The shadow telemetry does not compare against the authoritative path.**
   `_run_review_shadow` (`orchestrator.py:1572`) emits only the subagent's own
   verdict counts; `_run_plan_shadow` (`orchestrator.py:1730`) emits only the
   DAG's own metrics. No agreement/disagreement is computed anywhere.
2. **The telemetry is ephemeral.** Both are `logger.info(...)` strings under the
   `Ouroboros.Orchestrator` logger. No structured corpus is persisted to
   `.jarvis/` or the ledger. Once a session ends, the data is gone.
3. **"100% semantic alignment" is a category error for PLAN as literally
   stated.** The PLAN subagent emits a *DAG*; the legacy path emits a *flat task
   list*. They are different data structures by construction and will never be
   byte-identical. The meaningful relation is *refinement*: does the DAG cover
   100% of the legacy tasks, acyclically, with disjoint owned paths?

Therefore we cannot "query existing telemetry." We must **build the evidence
rail first**, accumulate organic production history through a soak, and let an
**event-driven graduation gate** promote each agent autonomously once the
evidence threshold is crossed. The retained legacy paths are repurposed as an
**active graceful-degradation circuit breaker**, not dead weight.

This is the structurally-correct, zero-shortcut path mandated by the governing
philosophy: *structural repair, not bypasses.*

---

## 2. Three Architectural Corrections (premise fixes)

These corrections are baked into the design below and are called out here so the
deviation from the original verbal directive is explicit and auditable.

| # | Naive directive | Correction | Rationale |
|---|-----------------|------------|-----------|
| 1 | "Flip `JARVIS_REVIEW_SUBAGENT_SHADOW` / `JARVIS_PLAN_SUBAGENT_SHADOW` to `false` to graduate." | `_SHADOW=false` currently *disables the observer entirely* and leaves legacy 100% in control. Graduation requires **(a)** new authoritative wiring **and (b)** a new per-agent flag `JARVIS_{REVIEW,PLAN}_SUBAGENT_AUTHORITATIVE` (default `false`) that the gate flips. `_SHADOW` is set `false` only as a redundant follow-on once authoritative. | The shadow flag is not a promotion switch; it is an existence switch. |
| 2 | "Inject a new RAM probe into `subagent_scheduler.py`." | **Reuse `MemoryPressureGate`** (`memory_pressure_gate.py`), which already provides the `psutil → /proc/meminfo → vm_stat` cascade and per-level fanout caps. The scheduler *already consults it* (Slice 5 Arc B, `_consult_memory_gate`, lines 684–756). Unit D **adds a worktree-RAM-budget dimension on top**, it does not duplicate the probe. | Zero-duplication mandate. |
| 3 | "Run a background cron-style evaluator." | The graduation check is **event-driven**: it runs at each op boundary when a new comparison row lands in the store. No separate periodic process. | Consistent with the Gap #4 event-primary campaign; one fewer process to wedge. |

---

## 3. Architecture Overview

Four units. Build order **D → A → B → C** (D is independent and ships first; A→C
is the staged rail).

```
                         ┌─────────────────────────────────────────────┐
                         │            ORCHESTRATOR FSM (11-phase)        │
                         │                                              │
   post-PLAN  ───────────┤  _run_plan_shadow(ctx)                       │
   (legacy flat plan +   │     │  legacy_flat + shadow_dag              │
    shadow DAG present)  │     ▼                                        │
                         │  ┌──────────────┐   ┌──────────────────┐     │
                         │  │  Unit B      │──▶│  Unit A           │     │
   post-VALIDATE ────────┤  │  Evaluator   │   │  Telemetry Store  │     │
   (shadow verdict;      │  │ (pure, det.) │   │  SQLite + async   │     │
    legacy outcome       │  └──────────────┘   │  writer + FIFO    │     │
    arrives at GATE)     │     ▲                │  prune (1k/agent) │     │
   GATE/terminal ────────┤     │ legacy_outcome └────────┬─────────┘     │
   (legacy review        │     └─────────────────────────┘ row landed    │
    decision)            │                                ▼              │
                         │                       ┌──────────────────┐    │
                         │                       │  Unit C          │    │
                         │                       │  Graduation Gate │    │
                         │                       │  (event-driven,  │    │
                         │                       │   50-soak, .env  │    │
                         │                       │   persist)       │    │
                         │                       └────────┬─────────┘    │
                         │                                │ promote       │
                         │                                ▼              │
                         │                 JARVIS_{REVIEW,PLAN}_SUBAGENT  │
                         │                        _AUTHORITATIVE = true   │
                         └─────────────────────────────────────────────┘
                                                  │
   ┌──────────────────────────────────────────────┴───────────────────┐
   │  Unit D — L3 Memory Governor (subagent_scheduler.py)              │
   │   composes MemoryPressureGate; worktree RAM budget;               │
   │   CRITICAL pressure ⇒ pre-emptive circuit-breaker trip ⇒ legacy   │
   └───────────────────────────────────────────────────────────────────┘
```

**Master invariant:** every new flag defaults to current behavior. With all new
flags off/shadow, the system is **byte-identical** to today. The observer
contract is preserved: nothing in Units A/B may raise into or block the FSM.

---

## 4. Unit A — Async Telemetry Store

**New module:** `backend/core/ouroboros/governance/shadow_telemetry_store.py`
**DB path:** `.jarvis/shadow_telemetry.db` (gitignored; host-local)
**Master flag:** `JARVIS_SHADOW_TELEMETRY_STORE_ENABLED` (default `true`; off ⇒
no-op, no file created)

### 4.1 Async-safety model

Python's `sqlite3` is blocking; the codebase forbids blocking the event loop.
Design:

- A single **writer task** drains a **bounded `asyncio.Queue`** (capacity env
  `JARVIS_SHADOW_TELEMETRY_QUEUE_MAX`, default 256). All `sqlite3` calls run
  inside `asyncio.to_thread(...)` so the loop never blocks.
- Producers (`_run_*_shadow` hooks) call **fire-and-forget** `record_*_nowait()`
  — enqueue and return immediately. Queue full ⇒ drop-oldest + increment a
  `dropped` counter (bounded memory; telemetry is advisory, never load-bearing).
- **Fail-soft everywhere.** Any sqlite/IO exception is caught, logged once at
  WARN, and swallowed. The store can never break the FSM (observer contract).
- Pattern precedent: the episodic-core `note_*_nowait` fire-and-forget synapse
  (Slice 134–136) and `state_persistence_daemon.py` async fail-soft writer.

### 4.2 Schema

One logical row per `(op_id, agent)`, written in up to two phases (see §4.4):

```sql
CREATE TABLE IF NOT EXISTS shadow_comparison (
    op_id           TEXT NOT NULL,
    agent           TEXT NOT NULL,          -- 'review' | 'plan'
    ts              REAL NOT NULL,          -- time.time() passed in by caller
    seq             INTEGER NOT NULL,       -- monotonic per-agent insert ordinal
    legacy_outcome  TEXT,                   -- json; NULL until legacy phase lands
    shadow_outcome  TEXT,                   -- json; NULL until shadow phase lands
    aligned         INTEGER,                -- 0/1/NULL(=incomplete)
    divergence_reason TEXT,                 -- NULL when aligned or incomplete
    PRIMARY KEY (op_id, agent)
);
CREATE INDEX IF NOT EXISTS idx_agent_seq ON shadow_comparison(agent, seq);
```

`seq` is a per-agent monotonic counter (table `agent_seq(agent TEXT PRIMARY KEY,
next INTEGER)`) — the basis for both the FIFO cap and the "last 50 consecutive"
graduation query. It does **not** use `Date.now()`/wall-clock for ordering
(determinism); `ts` is informational only and is supplied by the caller.

### 4.3 Anti-bloat: rolling FIFO cap (Sovereign enhancement)

After each insert, the writer task runs an **async self-prune** inside the same
`to_thread` call:

```sql
DELETE FROM shadow_comparison
 WHERE agent = ?
   AND seq <= (SELECT MAX(seq) FROM shadow_comparison WHERE agent = ?)
              - :cap;
```

`cap` = env `JARVIS_SHADOW_TELEMETRY_MAX_ROWS_PER_AGENT` (default **1000**).
Guarantees a microscopic, bounded SSD footprint (≤ ~2000 rows total across both
agents). A `VACUUM` is run opportunistically every `cap` inserts to reclaim
pages. Pruning is part of the writer's normal cycle — never a separate job.

### 4.4 Public API

```python
class ShadowTelemetryStore:
    def __init__(self, *, db_path: pathlib.Path | None = None,
                 cap_per_agent: int = 1000) -> None: ...
    async def start(self) -> None: ...          # spawn writer task (idempotent)
    async def aclose(self) -> None: ...         # drain + close, fail-soft

    # fire-and-forget producers (never block, never raise)
    def record_shadow_nowait(self, *, op_id: str, agent: str, ts: float,
                             shadow_outcome: dict) -> None: ...
    def record_legacy_nowait(self, *, op_id: str, agent: str, ts: float,
                             legacy_outcome: dict) -> None: ...

    # read side (used by Unit C; runs in to_thread)
    async def recent_aligned_streak(self, agent: str) -> int: ...
    async def last_n(self, agent: str, n: int) -> list[dict]: ...
```

**Two-phase upsert (load-bearing for REVIEW):** the REVIEW shadow verdict is
known at post-VALIDATE, but the legacy authoritative decision is not known until
GATE/terminal. So `record_shadow_nowait` and `record_legacy_nowait` each upsert
their half keyed by `(op_id, agent)`; when **both** halves are present the writer
invokes Unit B to compute `aligned` + `divergence_reason` and patches the row.
PLAN is single-phase (both halves available at the post-PLAN hook) and writes
once with both fields populated.

---

## 5. Unit B — Semantic Evaluator

**New module:** `backend/core/ouroboros/governance/shadow_evaluator.py`
Pure functions, **zero LLM, zero IO, never raises** (returns a structured
`Alignment(aligned: bool, reason: str)` even on malformed input — malformed ⇒
`aligned=False, reason="malformed:<detail>"`, which is the safe/conservative
default that *blocks* graduation).

### 5.1 REVIEW evaluator

```python
def evaluate_review(legacy: dict, shadow: dict) -> Alignment
```

Binary-verdict agreement on the **block-vs-allow** decision:

- `shadow_binary`: subagent aggregate verdict, `reject → BLOCK`;
  `approve` / `approve_with_reservations → ALLOW`. (Reservations map to allow —
  they are advisory, not blocking, matching shadow semantics today.)
- `legacy_binary`: derived from the authoritative outcome captured at
  GATE/terminal — `BLOCK` if the op's resolved risk tier ∈
  {`APPROVAL_REQUIRED`, `BLOCKED`} **or** SemanticGuardian raised a *hard*
  finding; else `ALLOW`.
- `aligned = (shadow_binary == legacy_binary)`. Divergence reason records both
  sides, e.g. `"shadow=BLOCK legacy=ALLOW"`.

### 5.2 PLAN evaluator

```python
def evaluate_plan(legacy_flat: list, shadow_dag: dict) -> Alignment
```

Refinement check — **NOT** structural equality. The DAG is *aligned* iff all
three hold:

1. **Coverage:** the set of files/tasks touched by the DAG's flattened node set
   ⊇ the set of tasks in the legacy flat list (100% coverage; the DAG may add
   structure but may not *drop* a legacy task). Missing tasks ⇒
   `reason="dropped_tasks:<names>"`.
2. **Acyclicity:** the DAG is a true DAG (Kahn's algorithm / topological sort
   succeeds). A cycle ⇒ `reason="cyclical_dag"`.
3. **Disjoint ownership:** parallel-eligible units have disjoint `owned_paths`
   (no two concurrent units claim the same file). Overlap ⇒
   `reason="owned_path_overlap:<path>"`.

Extra structure beyond the legacy tasks does **not** count as misalignment
(refinement is allowed). Only *dropping*, *cycling*, or *overlapping* fails.

---

## 6. Unit C — Auto-Graduation Gate + Graceful Degradation Circuit Breaker

**New module:** `backend/core/ouroboros/governance/shadow_graduation_gate.py`
**Master flag:** `JARVIS_SHADOW_GRADUATION_GATE_ENABLED` (default `true`)

### 6.1 Event-driven graduation (50-soak)

Invoked at each op boundary **after** a comparison row is finalized (both halves
present + `aligned` computed). For the agent whose row just landed:

```
streak = store.recent_aligned_streak(agent)   # consecutive aligned, newest-first
if streak >= JARVIS_SHADOW_GRADUATION_THRESHOLD (default 50):
    promote(agent)
```

`recent_aligned_streak` counts consecutive `aligned=1` rows from the highest
`seq` downward, stopping at the first `aligned=0` (a single divergence **resets
the streak to 0** — "50 *consecutive*"). Incomplete rows (one half missing) are
skipped, not counted as breaks, until they finalize.

### 6.2 Promotion (autonomous + persistent)

`promote(agent)`:

1. Set process env `JARVIS_{REVIEW|PLAN}_SUBAGENT_AUTHORITATIVE=true`.
2. Persist durably via **existing** `graduation_orchestrator.persist_flag_to_env`
   (`graduation_orchestrator.py:106`) — the bounded, credential-safe `.env`
   writer that refuses credential-shaped keys and never raises. Our flags
   (`..._AUTHORITATIVE`) contain no credential marker substring, so they pass.
3. Persist `JARVIS_{REVIEW|PLAN}_SUBAGENT_SHADOW=false` as a redundant follow-on
   (shadow is subsumed by authoritative).
4. Emit a `flag_registered`-style audit event + a one-line structured log
   `[GRADUATION] agent=plan streak=50 -> authoritative`.
5. **Idempotent:** if already authoritative, no-op.

Operator `=0`/explicit-off precedence is honored exactly as Slice 136: if the
operator has explicitly set the flag, the gate does not override it.

### 6.3 Authoritative wiring

Once `..._AUTHORITATIVE=true`:

- **PLAN:** `ctx.execution_graph` produced by the subagent becomes the
  authoritative input consumed by `_materialize_execution_graph_candidate`
  (`orchestrator.py:10400`) and submitted to `SubagentScheduler` — instead of
  being stashed-and-ignored. The legacy flat plan is computed but held as the
  fallback baseline.
- **REVIEW:** the subagent verdict gains the authority to **raise** the risk tier
  (REJECT ⇒ force `APPROVAL_REQUIRED`) before GATE. It composes with — never
  weakens — SemanticGuardian/Iron Gate (strictest-wins, same discipline as the
  risk-tier floor). REVIEW can only *add* friction, never remove it.

### 6.4 Graceful Degradation Circuit Breaker

Once authoritative, the subagent call is wrapped in a breaker. **Trip
conditions:**

| Condition | Detection | Action |
|-----------|-----------|--------|
| Cyclical DAG | Unit B acyclicity check fails on the authoritative DAG | trip → legacy flat plan |
| Unparsable / empty DAG | DAG materialization raises or yields 0 units | trip → legacy flat plan |
| Subagent LLM timeout | dispatch exceeds `JARVIS_PLAN_SUBAGENT_TIMEOUT_S` | trip → legacy flat plan |
| **CRITICAL memory pressure** (Sovereign enhancement) | `MemoryPressureGate.pressure() == CRITICAL` **before** dispatch | **pre-emptive trip → legacy flat plan; do NOT spin up the subagent or worktree fan-out at all** | 

On any trip:

1. Emit `AGENT_DEGRADATION` to `StreamEventBroker.publish(...)`
   (`ide_observability_stream.py:1894`). New constant
   `EVENT_TYPE_AGENT_DEGRADATION = "agent_degradation"` added beside the existing
   `EVENT_TYPE_MEMORY_PRESSURE_CHANGED`. Payload: `{agent, op_id, trip_reason,
   pressure_level}`. `publish` never raises/blocks.
2. Seamlessly route the operation to the retained legacy generator. **Execution
   continuity is mathematically guaranteed** — the legacy path is always present
   and is the same code that runs today.
3. Record the trip to the telemetry store as a divergence signal (a trip is
   evidence the agent is not yet trustworthy; it counts against future streaks
   for observability, but post-graduation it does not auto-demote in v1 —
   demotion is out of scope, see §10).

The CRITICAL trip is the **Unit C/D coupling**: under CRITICAL pressure on the
16GB unified-memory footprint, the authoritative PLAN path does not even attempt
to serialize worktrees to 1 — it routes to the low-memory legacy flat-plan
generator outright, absolutely guaranteeing zero swap-thrash.

---

## 7. Unit D — L3 Memory Governor

**Edit:** `backend/core/ouroboros/governance/autonomy/subagent_scheduler.py`
**Master flag:** `JARVIS_L3_MEMORY_GOVERNOR_ENABLED` (default `true`; inert until
L3 actually runs a graph)

### 7.1 What already exists

`SubagentScheduler` already consults `MemoryPressureGate` (Slice 5 Arc B,
`_consult_memory_gate`, lines 684–756; fan-out clamp at lines 493–507). Today it
clamps `n_requested → n_allowed` using the gate's **per-level fanout caps**
(WARN 8 / HIGH 3 / CRITICAL 1) and defers overflow with zero work loss.

### 7.2 What Unit D adds (worktree-RAM-budget dimension)

The per-level caps are free-percentage based; they do not model the *absolute
RAM cost of a worktree*. On a 16GB box, even "HIGH ⇒ 3" can over-commit if each
worktree's process set is heavy. Unit D adds an absolute budget clamp that
composes (strictest-wins) with the existing level clamp:

```python
avail_mb     = MemoryPressureGate probe → available RAM (MB)
budget_mb    = JARVIS_L3_WORKTREE_RAM_BUDGET_MB        # default 1500
ram_cap      = max(1, floor(avail_mb / budget_mb))
level_cap    = existing FanoutDecision.n_allowed
max_worktrees = min(ram_cap, level_cap, configured_concurrency_limit)
```

`max_worktrees` is recomputed **before each scheduling wave** (it is not a
boot-time constant), so it tracks live pressure. The existing
defer-overflow-with-zero-work-loss mechanism is reused for any clamp.

### 7.3 CRITICAL ⇒ pre-emptive legacy (the Unit C/D bridge)

When `pressure() == CRITICAL`, Unit D does **not** serialize the L3 scheduler to
1 for the PLAN-authoritative path. Instead it signals the Unit C breaker to trip
**before** any worktree is created, routing generation to the legacy flat-plan
generator (§6.4). Serialize-to-1 remains the behavior only for already-admitted
non-PLAN L3 work at HIGH pressure. This is the explicit Sovereign enhancement:
*the safest response to CRITICAL pressure is to not fan out at all.*

New env knobs:

- `JARVIS_L3_WORKTREE_RAM_BUDGET_MB` (default `1500`) — assumed RAM per worktree.
- `JARVIS_L3_MEMORY_GOVERNOR_ENABLED` (default `true`).

---

## 8. Event-Driven Topology (no polling)

| Trigger | Producer | Consumer | Cadence |
|---------|----------|----------|---------|
| Shadow verdict computed | `_run_review_shadow` / `_run_plan_shadow` | `store.record_shadow_nowait` | per op, post-phase |
| Legacy outcome resolved | GATE/terminal hook (REVIEW); post-PLAN (PLAN) | `store.record_legacy_nowait` | per op |
| Comparison row finalized | writer task (both halves present) | Unit B → Unit C gate check | per op, in writer |
| Streak ≥ threshold | Unit C | `persist_flag_to_env` + promote | once per agent (idempotent) |
| Breaker trip | Unit C breaker | `StreamEventBroker.publish(AGENT_DEGRADATION)` | per trip |
| Memory clamp | Unit D | existing SSE governor telemetry | per scheduling wave |

No `Date.now()`-driven loop, no cron. The graduation decision is a pure function
of accumulated rows, checked exactly when a new row could change the answer.

---

## 9. New Environment Flags (complete list)

| Flag | Default | Unit | Meaning |
|------|---------|------|---------|
| `JARVIS_SHADOW_TELEMETRY_STORE_ENABLED` | `true` | A | Enable the SQLite store (off ⇒ no file, no-op) |
| `JARVIS_SHADOW_TELEMETRY_QUEUE_MAX` | `256` | A | Bounded write-queue capacity |
| `JARVIS_SHADOW_TELEMETRY_MAX_ROWS_PER_AGENT` | `1000` | A | Rolling FIFO cap |
| `JARVIS_SHADOW_GRADUATION_GATE_ENABLED` | `true` | C | Enable event-driven graduation |
| `JARVIS_SHADOW_GRADUATION_THRESHOLD` | `50` | C | Consecutive-aligned ops to promote |
| `JARVIS_REVIEW_SUBAGENT_AUTHORITATIVE` | `false` | C | REVIEW verdict gates (set by gate) |
| `JARVIS_PLAN_SUBAGENT_AUTHORITATIVE` | `false` | C | PLAN DAG authoritative (set by gate) |
| `JARVIS_L3_MEMORY_GOVERNOR_ENABLED` | `true` | D | Worktree-RAM-budget clamp |
| `JARVIS_L3_WORKTREE_RAM_BUDGET_MB` | `1500` | D | Assumed RAM per worktree |

`JARVIS_{REVIEW,PLAN}_SUBAGENT_SHADOW` (existing, default `true`) are flipped to
`false` by the gate post-promotion. All defaults preserve today's behavior: the
two `_AUTHORITATIVE` flags are `false`, so the rail observes and records but
**changes no decision** until evidence promotes it.

---

## 10. Out of Scope (YAGNI)

- **Auto-demotion** after post-graduation regression. v1 trips the breaker to
  legacy per-op but does not flip `_AUTHORITATIVE` back to `false` automatically.
  (Visual VERIFY's auto-demotion is the precedent to copy later; deferred.)
- **LLM enrichment of the PLAN DAG** (Step-2 import-graph analysis). The
  evaluator only checks the deterministic Step-1 DAG.
- **A web dashboard for the corpus.** SSE `AGENT_DEGRADATION` + structured logs
  are sufficient for v1; the existing IDE observability surfaces consume them.
- **Cross-agent graduation coupling.** REVIEW and PLAN graduate independently.

---

## 11. Testing Strategy

- **Unit A:** in-memory `sqlite3` (`:memory:`) tests — two-phase upsert,
  alignment patch, FIFO prune at cap, drop-oldest under queue saturation,
  fail-soft on a poisoned write (no raise into caller). Writer-task lifecycle
  (start/aclose idempotent).
- **Unit B:** table-driven pure-function tests — REVIEW binary mapping incl.
  reservations→allow; PLAN coverage/acyclicity/disjoint with a cyclical DAG, a
  dropped-task DAG, an owned-path-overlap DAG, and a valid refinement. Malformed
  input ⇒ `aligned=False` (conservative).
- **Unit C:** streak counting (49 aligned ⇒ no promote; 50 ⇒ promote; one
  divergence resets); `persist_flag_to_env` called with correct args (mocked);
  idempotent re-promotion; operator-off precedence; breaker trip table for all
  four conditions incl. CRITICAL-pre-emptive; `AGENT_DEGRADATION` published.
- **Unit D:** `max_worktrees` math across OK/WARN/HIGH/CRITICAL with injected
  `MemoryPressureGate` probe; CRITICAL ⇒ breaker-trip signal not serialize-to-1;
  governor-disabled ⇒ pass-through (byte-identical to Slice 5 Arc B today).
- **Regression spine:** with all new flags at default and `_AUTHORITATIVE=false`,
  prove the FSM is byte-identical (no decision changed) — the OFF-is-inert
  guarantee.

---

## 12. Build Order & Plan Decomposition

This spec will likely become **two implementation plans**:

1. **Plan 1 — L3 Memory Governor (Unit D).** Independent, lowest-risk, highest
   immediate value. Ships first.
2. **Plan 2 — The Evidence Rail (Units A → B → C).** Staged: store, then
   evaluator, then gate+breaker. C depends on A+B and on the Unit D breaker
   signal for the CRITICAL trip.

---

## 13. Authority & Mandate Alignment

- **§5 Intelligence-driven routing:** graduation is evidence-driven, not a
  hardcoded table.
- **§6 Threshold-triggered neuroplasticity:** the 50-soak gate is the literal
  "detect → verify → graduate" loop.
- **§7 Absolute observability:** every promotion and every breaker trip emits a
  structured, durable, SSE-visible signal.
- **Zero-shortcut mandate:** we build the evidence rail rather than flipping a
  flag on faith; we reuse `MemoryPressureGate` rather than duplicating a probe;
  we retain legacy as an active circuit breaker rather than deleting the only
  rollback baseline.

---

## 14. Naming Disambiguation — two unrelated "shadows" (operator hygiene)

A reconnaissance pass (2026-06-14) confirmed that the autonomous Ouroboros loop
independently shipped a *resilience* feature also named "Shadow Mode" (Slices
252/253 on `main`). **It is a different system in a different domain.** This
section exists so an operator never conflates the two when reading flags or
events. They collide only on the word "shadow"; there is **zero functional
overlap** (verified: the loop never touches `orchestrator.py`'s
`_run_{plan,review}_shadow` hooks nor `governed_loop_service.py`).

| Axis | **Resilience "Shadow Mode"** (loop, Slice 252/253) | **Subagent "Shadow Rail"** (this spec) |
|------|---------------------------------------------------|----------------------------------------|
| Flag | `JARVIS_RESILIENCE_SHADOW_MODE` | `JARVIS_{PLAN,REVIEW}_SUBAGENT_SHADOW` |
| Meaning of "shadow" | Trap a dangerous resilience **action** (process kill / load-shed / restart) and log what it *would* have done instead of executing it | Run the new REVIEW/PLAN **subagent** silently alongside the legacy path and record the verdict comparison |
| Instruments | `cybernetic_reanimation.py` + `unified_supervisor.py` self-healing organs | `orchestrator._run_{plan,review}_shadow` hooks |
| Persistence | Ephemeral SSE (`EVENT_TYPE_SHADOW_ACTION_TRAPPED`) | Durable SQLite ledger (`.jarvis/shadow_telemetry.db`) |
| "Endorse"/promote | `/endorse <action_id>` runs **one trapped action once**; promotes nothing, never reads/writes `_AUTHORITATIVE` | 50-soak gate flips `_AUTHORITATIVE`→true **permanently** |
| Our SSE event | (theirs: `shadow_action_trapped`) | `agent_degradation` (breaker trip) |

**Merge note:** when this branch later rebases onto `main`, expect *mechanical*
adjacent-line conflicts in `ide_observability_stream.py` (both add `EVENT_TYPE_*`
constants + frozenset entries) and `serpent_flow.py` — resolve by **keeping both
sets** of additions. There is no semantic interaction to reconcile.
