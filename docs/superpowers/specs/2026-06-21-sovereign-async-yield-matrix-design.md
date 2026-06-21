# Sovereign Asynchronous Yield Matrix (Dual-Layer Execution Boundary) — Design Spec

> **Arc codename:** Sovereign Asynchronous Yield Matrix
> **Date:** 2026-06-21
> **Branch:** `fleet/sovereign-async-yield-matrix`
> **Trigger:** The autonomous O+V loop wrote generative file mutations directly into the **primary host checkout** (`candidate_generator.py`, 13:36) because `JARVIS_FILE_ISOLATION_ENABLED` is default-OFF and nothing turned it on — a split-brain collision with live operator work. Fix = make the **existing** Sovereign Execution Boundary (PRs #69533/#69534) **self-enforcing** (Layer 1) and add a **graceful operator-yield** so the loop steps aside when an operator is active (Layer 2).

---

## 1. Problem & Principle

A static env default (`JARVIS_FILE_ISOLATION_ENABLED=false`) is a **safety vulnerability**: protection that depends on someone remembering to set a flag is not protection. Two layers, with a strict separation of concerns:

- **Layer 1 (Hard / deterministic):** the loop must *physically* be unable to mutate the primary checkout when running there — regardless of env config, even if the flag is explicitly `false`. This is a **deterministic** gate (`git rev-parse`), never probabilistic.
- **Layer 2 (Soft / advisory):** when an operator is actively working, the loop should **gracefully yield the execution thread** (park its in-flight op, free the worker, go dormant) and **resume** when the operator goes idle — not a blocking `sleep` spin.

**Invariant (from CLAUDE.md Watchdog Isolation):** a safety gate must not depend on fuzzy/advisory state. → Layer 1 is deterministic; Layer 2 (advisory yield) sits *on top of* Layer 1 and can never weaken it.

### Two honest corrections to the original framing
- O+V's background pool is **asyncio.Task-based, not `ProcessPoolExecutor`** (`background_agent_pool.py`). Parking frees the asyncio worker slot via the existing path; there is no process pool to free.
- **CAI** (`context_awareness_intelligence.predict_intent`) is **text-intent only** — no operator-presence. Presence is derived **deterministically** (session-liveness / last-input timestamp). CAI/`cai_router` may *enrich* the signal but is not the source of truth for it.

---

## 2. Goals / Non-Goals

### Goals
- **G1.** When O+V boots inside the primary host checkout (not an isolated worktree, not a cloud container), it **forces isolation on** and routes all mutations to an isolated worktree — overriding `JARVIS_FILE_ISOLATION_ENABLED=false`.
- **G2.** Autonomous **raw file writes** (`edit_file`/`write_file`) to the primary checkout are denied (close the incident vector — today only *commits* are denied).
- **G3.** When an operator is active, the loop **parks** its in-flight op (frees the worker, dormant), and **resumes** on operator-idle — reusing the existing park/resume machinery, no blocking sleep.
- **G4.** A verification script **mathematically proves** that an explicit `JARVIS_FILE_ISOLATION_ENABLED=false` is overridden by the deterministic lock and the primary checkout stays pristine.
- **G5.** Zero duplication — extend `autonomous_workspace`, `execution_context`, `operator_commit_authority`, `op_park_store`, `trinity_event_bus`, `sensor_governor`; build no parallel systems.

### Non-Goals
- **NG1.** No new worktree manager / event bus / park system (all exist).
- **NG2.** No mutex (isolation removes the shared tree → a mutex is subsumed).
- **NG3.** No change to risk tiers, OCA merge authority, or the Epistemic Context Matrix.
- **NG4.** Layer 2 is advisory; it must never be the thing that prevents a primary-checkout write (that's Layer 1's deterministic job).

---

## 3. Reuse Inventory (audit-confirmed)

| Capability | Existing asset | Anchor | Verdict |
|---|---|---|---|
| project_root → worktree routing (single seam) | `autonomous_workspace.resolve_loop_project_root()` | `autonomous_workspace.py:63-106` | EXISTS |
| isolation flag | `file_isolation_enabled()` (default FALSE) | `autonomous_workspace.py:48-52` | EXISTS |
| **primary-checkout detection (git)** | `is_primary_checkout()` (git-dir vs git-common-dir) | `execution_context.py:53-74` | **EXISTS — reuse** |
| autonomy (HMAC operator presence) | `is_autonomous()` | `execution_context.py:77-99` | EXISTS |
| Stage A commit denial in primary | `_execution_boundary_verdict()` | `operator_commit_authority.py:1305-1353` | EXISTS |
| **raw write denial (edit/write to primary)** | — tool_executor validates vs repo_root only | `tool_executor.py` write path | **GAP — build** |
| container/cloud detection | — | — | GAP (small) |
| verify harness | `scripts/verify_file_isolation.py` (I1–I4) | — | EXISTS — extend (I5) |
| park admission / decision / resume | `op_park_store.park()/should_park_for_route()`, `generate_park_wrapper.maybe_park_or_resume()`, `background_agent_pool` | `op_park_store.py:158-425`, `generate_park_wrapper.py:81-204` | EXISTS — reuse |
| async pub/sub (Neural Mesh) | `TrinityEventBus.publish()/subscribe()` (MQTT wildcards) | `trinity_event_bus.py:948-1091` | EXISTS — add topics |
| op-emission throttle/gate | `SensorGovernor.request_budget()` + injectable signal fns | `sensor_governor.py:477-771` | EXISTS — inject signal |
| operator-presence source | `register_session_liveness_probe()` / last-input ts | `harness.py:1149-1156` | EXISTS — reuse |
| CAI (optional enrichment only) | `predict_intent`, `cai_router.cai_tier_advisory` | `context_awareness_intelligence.py:123`, `cai_router.py` | EXISTS — advisory |

**Net-new:** a deterministic-override block in `resolve_loop_project_root`, a container check, a raw-write guard, an `operator_presence` detector + 2 event topics + a governor signal + a park trigger wire, and `verify_file_isolation.py` I5. ~120–160 lines + tests.

---

## 4. Architecture

```
                 ┌──────────────────── LAYER 1 (deterministic, hard) ────────────────────┐
O+V boot ─────►  resolve_loop_project_root(repo_root, session_id)
                   │  effective = is_primary_checkout(root) AND not _is_cloud_container()
                   │  if effective AND is_autonomous(root):
                   │      FORCE isolation → route to worktree  (override env flag, even =false)
                   │      log [DeterministicLock] forced isolation despite env=<x>
                   │  else: existing behavior (flag-driven)
                   ▼
            all mutation delegates inherit the (now-isolated) project_root
                   │
   tool-loop raw write (edit_file/write_file):
            if is_primary_checkout(target_root) AND autonomous → DENY  (raw-write guard, G2)
                 └──────────────────────────────────────────────────────────────────────┘

                 ┌──────────────────── LAYER 2 (advisory, soft yield) ───────────────────┐
   operator_presence detector (deterministic: session-liveness / last-input ts)
        │  emits TrinityEvent  operator.active / operator.idle   (Neural Mesh)
        ▼
   SensorGovernor.operator_active_fn  +  park trigger
        │  on operator.active:  governor hard-zero caps (no NEW ops) + park IN-FLIGHT op
        │                        (op_park_store via should_park_for_route(operator_suspended=True))
        │                        → worker slot freed, op dormant, descriptor stored
        │  on operator.idle:    clear suspend → resume parked op via existing continuation path
        └──────────────────────────────────────────────────────────────────────────────┘
```

---

## 5. Component Specs

### 5.1 Layer 1a — Deterministic self-enforcing isolation (`autonomous_workspace.py` + `execution_context.py`)
- Add `_is_cloud_container() -> bool` to `execution_context.py`: deterministic check for a designated isolated runtime (env markers `OUROBOROS_CLOUD_NODE`/`KUBERNETES_SERVICE_HOST`/`/.dockerenv` presence + GCE metadata best-effort). Default: not-container. Never raises.
- In `resolve_loop_project_root()` (before the `file_isolation_enabled()` early-return at line 73): compute `force = is_primary_checkout(root) and not _is_cloud_container() and is_autonomous(root)`. If `force` → **dual-arm BOTH flags in-process** (`os.environ["JARVIS_FILE_ISOLATION_ENABLED"]="true"` AND `os.environ["JARVIS_EXECUTION_BOUNDARY_ENABLED"]="true"` — LR-A, so the downstream Stage A commit gate also reads armed), proceed to worktree routing **even when the flags were False**, and emit `[DeterministicLock] forced isolation+boundary despite env (primary checkout, autonomous)`. The two flags are set as a pair, before routing — never one without the other.
- Gating: `JARVIS_DETERMINISTIC_ISOLATION_LOCK_ENABLED` (default **TRUE** — this is the safety fix; OFF reverts to pure flag-driven legacy). The override is **failure-path-only**: when already isolated (worktree) or in a container or non-autonomous (operator present), it's a no-op.

### 5.2 Layer 1b — Raw-write denial (`tool_executor.py` + `operator_commit_authority`)
- The tool-loop write path (`edit_file`/`write_file`) gains a guard: if the resolved target lives in a **primary checkout** AND the op is autonomous AND the deterministic lock is enabled → **deny the write** (`POLICY_DENIED reason=primary_checkout_raw_write`), routed like other policy denials. Reuses `is_primary_checkout()` + the autonomy check. This is defense-in-depth: even if routing were somehow bypassed, raw writes to the host tree are refused.
- Fail-soft: any error in the guard → fall through to existing path (never crash the loop), but log.

### 5.3 Layer 2a — Deterministic operator-presence detector (NEW small module `operator_presence.py`)
- `operator_present() -> bool`: True iff a human is active — derived from the **most recent** of: a live interactive session liveness probe, and a last-human-input monotonic timestamp (threshold `JARVIS_OPERATOR_IDLE_S`, default 45s). Pure/deterministic, no LLM. CAI `predict_intent` may be consulted *only* to enrich confidence, never as the sole source.
- An async `OperatorPresenceWatcher` polls/debounces and **publishes `operator.active` / `operator.idle`** `TrinityEvent`s on transitions (edge-triggered, not level-spam). Bounded, fail-soft.

### 5.4 Layer 2b — Governor suspend + park trigger (`sensor_governor.py` + `op_park_store.py` + `generate_park_wrapper.py`)
- `SensorGovernor` gains injectable `operator_active_fn: Optional[Callable[[], bool]]` (mirrors `_posture_fn`). When it returns True, the weighted cap is **hard-zeroed** (no NEW ops admitted) — distinct from the soft 0.2× emergency brake.
- A subscriber to `operator.active` triggers the **existing** park path for the in-flight op: `should_park_for_route(..., operator_suspended=True)` (new param) → `op_park_store.park()` → worker freed, descriptor stored. On `operator.idle`, `operator_suspended=False` and the existing resume continuation (`background_agent_pool.submit_for_resume` → `maybe_park_or_resume` RESUME path) rehydrates the op into the FSM.
- **Atomic Yield Integrity (LR-B):** the park trigger does NOT call `park()` immediately. It first `await`s a per-op **mutation critical-section guard** to drain — a lightweight `asyncio` counter/section entered around the apply/commit path (`ChangeEngine.execute`, the tool-loop `write_file`/`edit_file`, the AutoCommitter commit). If a critical mutation is executing, the yield waits for *that* operation to complete (bounded by `JARVIS_OPERATOR_YIELD_DRAIN_MAX_S`, default 30s) and parks only at the next safe checkpoint. If the section never drains within the cap (wedged mutation), the yield is **abandoned** (logged) and the op runs to its own terminal — we never park a half-applied mutation. The guard is reused by both the park trigger and any future safe-checkpoint consumer; it is a no-op when `JARVIS_OPERATOR_YIELD_ENABLED=false`.
- Gating: `JARVIS_OPERATOR_YIELD_ENABLED` (default **FALSE** initially — advisory behavior change; graduate after soak). When off, byte-identical.

### 5.5 Layer 3 — Verification (`scripts/verify_file_isolation.py` extension + a focused test)
- Add **I5: deterministic-lock override** — boot with `JARVIS_FILE_ISOLATION_ENABLED=false` explicitly set, assert the debug log contains `[DeterministicLock] forced isolation` AND the primary checkout is `git status --porcelain`-clean of loop-authored changes (reusing I3's pristine check). This is the "prove the lock overrides me" mechanism (G4).
- Add focused unit tests: `is_primary_checkout` override path, raw-write guard denial, operator-presence edge detection, governor hard-zero on operator-active, park-on-active / resume-on-idle.

---

## 6. Cross-Cutting

### Gating (all fail-soft)
| Flag | Default | Effect |
|---|---|---|
| `JARVIS_DETERMINISTIC_ISOLATION_LOCK_ENABLED` | `true` | Layer 1a override (safety fix; OFF = legacy flag-driven) |
| `JARVIS_OPERATOR_IDLE_S` | `45` | Idle threshold for presence |
| `JARVIS_OPERATOR_YIELD_ENABLED` | `false` | Layer 2 yield (advisory; graduate after soak) |
| `JARVIS_OPERATOR_YIELD_DRAIN_MAX_S` | `30` | LR-B: max wait for an in-flight critical mutation to drain before parking; exceeded → abandon yield, op runs to terminal (never park mid-mutation) |
| `JARVIS_FILE_ISOLATION_ENABLED` | (existing, false) | still honored; the lock can only force it *on*, never off |
| `JARVIS_EXECUTION_BOUNDARY_ENABLED` | (existing, false) | Stage A commit denial (forced on by the lock alongside isolation) |

### Invariants
- **I-A.** The deterministic lock can only ever *increase* isolation (force on), never disable it. There is no path where the lock reduces protection.
- **I-B.** Layer 2 (advisory yield) can never cause a primary-checkout write — that is solely Layer 1's deterministic guarantee.
- **I-C.** OFF (`JARVIS_OPERATOR_YIELD_ENABLED=false` + lock disabled) = byte-identical legacy.
- **I-D.** All new code fail-soft; presence/yield errors degrade to "no yield" (loop continues, still isolated by Layer 1).

### Telemetry (Manifesto §7)
- `[DeterministicLock] forced isolation despite env=<x> (primary, autonomous)`
- `[RawWriteGuard] denied edit_file/write_file to primary checkout op=<id>`
- `[OperatorYield] operator.active → parked op=<id> worker_freed`
- `[OperatorYield] operator.idle → resumed op=<id>`

---

## 7. Phasing
- **Layer 1 first** (the actual incident fix — deterministic override + raw-write guard + verify I5). Highest value, default-on.
- **Layer 2 second** (graceful yield — presence detector + events + governor/park wire). Advisory, default-off, graduate after soak.

## 8. Locked Resolutions (operator-ratified 2026-06-21 — absolute, not open)

- **LR-A — Absolute Dual-Arming.** When the deterministic lock fires (primary checkout, autonomous, non-container) it **forces BOTH** `JARVIS_FILE_ISOLATION_ENABLED=true` **AND** `JARVIS_EXECUTION_BOUNDARY_ENABLED=true` — atomically, as a pair. Forcing file isolation without commit denial leaves a catastrophic gap; the lockdown is absolute. Implemented in §5.1 (the override sets both before routing) and asserted in §5.5 (I5 verifies both are armed when the lock fires). The lock can only ever set these *on*, never off (Invariant I-A).

- **LR-B — Atomic Yield Integrity (the Corruption Guard).** The operator-yield park sequence MUST be atomic with respect to in-flight critical mutations. When `operator.active` arrives, if the generative loop is mid critical state mutation (an executing `write_file`/`edit_file`/`ChangeEngine.execute`/git commit), the yield path MUST **wait for that specific atomic operation to resolve** before invoking `op_park_store.park()`. We never park a half-written file or an in-progress commit — the FSM must only ever rehydrate from a consistent checkpoint. Mechanism: a per-op **mutation critical-section guard** (an `asyncio` re-entrant section / counter around the apply/commit path); the park trigger `await`s the section to drain (bounded by `JARVIS_OPERATOR_YIELD_DRAIN_MAX_S`, default 30s — if a mutation wedges past the cap, the yield is **abandoned** and the op continues to its own terminal rather than risk a corrupt park). Implemented in §5.4.
