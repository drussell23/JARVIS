# Milestone A1 — Sovereign Event-Driven Intake & DLQ Matrix — Design Spec

> **Arc:** A1 (the PRD's #1 autonomy gate — "trace `file-00` enqueued→dispatched … the milestone (first autonomous PR) is gated on it, nothing else").
> **Branch:** `fleet/a1-event-driven-intake-dlq`. **Date:** 2026-06-21.
> **PRD:** OUROBOROS_VENOM_NORTH_STAR §51.11.34-ROADMAP A1.

---

## 1. Diagnosis (root cause — already traced, not assumed)

The dispatch *pipe* works; the failure is at the *source* and a silent *sink*:
- **The dispatch chain is wired + runs unconditionally.** `IntakeLayerService` boots, `UnifiedIntakeRouter.start()` spawns `_dispatch_loop`, `GLS.submit` is reachable. The PRD's "zero `IntakeLayerService booted` lines in stdout" was a **red herring** — `silent_boot` redirects INFO→`debug.log`; only WARNING+ reaches stdout.
- **The strategic-GOAL source is default-OFF.** `_roadmap_ignition_daemon` (the thing that reads the roadmap and emits GOAL envelopes into intake) only spawns when `JARVIS_ROADMAP_ORCHESTRATOR_ENABLED` is set (gov_loop_service.py:1910; §33.1 default-FALSE). Most soak configs never emit a GOAL into the (working) pipe. (`docker-compose.dw-cortex-soak.yml:251` does set it.)
- **A latent silent-drop race.** When the daemon emits while the router is unattached (`router=None`), `roadmap_orchestrator._TeeRouter` (line 656) **captures the envelope into `.captured` and never forwards it** — "NEVER raises," no warning. The GOAL vanishes with zero signal.

So A1 is NOT a dispatcher rewrite. It is: **eliminate the emit-before-ready race, make any drop loud + recoverable, route heavy GOALs through the Epistemic Matrix, and instrument the path so a soak proves it.** The Sovereign Intake Priority Mesh's backpressure Health Gate is explicitly **deferred** until the pipe is proven flowing (admission control on a not-yet-flowing pipe would mask the fix).

---

## 2. Goals / Non-Goals
**Goals:** (G1) the roadmap daemon never emits before the router is attached + ready (event-driven, no poll/sleep-retry). (G2) no strategic GOAL is ever silently lost — any orphaned envelope is loud + persisted to a DLQ + replayed. (G3) heavy multi-file GOALs are tagged at intake to route through the Epistemic Context Matrix (avoid the Venom timeout). (G4) `[A1Trace]` breadcrumbs at every hop so the next soak proves the exact path. (G5) reuse-first — TrinityEventBus, `is_heavy_goal`, the existing dispatch chain.

**Non-Goals:** backpressure Health Gate (deferred — Part 1 of the Mesh, after the pipe is proven). No new dispatch queue / FSM. No change to the cage/risk-tiers. End-to-end `file-00 → autonomous PR` proof is an **operator-run soak on a real host** (C2), not part of this code arc.

---

## 3. Reuse Inventory
| Need | Existing asset | Anchor |
|---|---|---|
| Async pub/sub | `TrinityEventBus.publish/subscribe`, `get_event_bus_if_exists` | `trinity_event_bus.py:948-1091`, `:1367` |
| Router attach point | `IntakeLayerService` sets `gls._intake_router = self._router` | `intake_layer_service.py:464` |
| Dispatch loop | `UnifiedIntakeRouter.start()` → `_dispatch_loop` | `unified_intake_router.py:750-757`, `:1260` |
| Roadmap daemon | `_roadmap_ignition_daemon` + `_TeeRouter` | `governed_loop_service.py:1910`, `roadmap_orchestrator.py:656` |
| GLS submit | `GovernedLoopService.submit` | gov_loop_service.py |
| Heavy-GOAL predicate | `epistemic_prefetch.is_heavy_goal(target_files, blast_radius)` | (merged) |
| Atomic JSONL append | `op_context`/`state_persistence` `_atomic_write` patterns | (existing) |

---

## 4. Component Specs

### 4.1 Event-driven valve (G1) — `intake.router.ready`
- **IntakeLayerService**: after it attaches `gls._intake_router = self._router` AND `router.start()` has spawned `_dispatch_loop`, publish a `TrinityEvent(topic="intake.router.ready")` (constant `EVENT_ROUTER_READY`). Idempotent (publish once per boot). Also expose a sync `router_is_ready()` probe (an `asyncio.Event` / flag) for the daemon to check on subscribe (avoids a missed-event race: subscribe THEN check the flag).
- **`_roadmap_ignition_daemon`**: before emitting ANY envelope, `await` the router-ready signal — subscribe to `intake.router.ready` **and** check `router_is_ready()` first (subscribe-then-check, so a ready-before-subscribe can't deadlock). Bounded wait (`JARVIS_A1_ROUTER_READY_TIMEOUT_S`, default 60s); on timeout → log CRITICAL + route the GOAL to the DLQ (don't emit into a void). No sleep-poll loop — pure event + flag.

### 4.2 Sovereign DLQ (G2) — `.jarvis/intake_dlq.jsonl`
- **`_TeeRouter` (and any ingest with unattached upstream):** if `upstream is None` at ingest time → **CRITICAL log** (`[IntakeDLQ] orphaned GOAL — no attached router`) + append the full envelope (serialized, atomic temp+rename) to `.jarvis/intake_dlq.jsonl` with `{ts, reason, envelope, schema_version}`. Never silently capture-and-forget.
- **DLQ replay:** on boot, AFTER `intake.router.ready` fires, drain `.jarvis/intake_dlq.jsonl` → re-ingest each entry through the now-attached router → on success, remove/mark it (atomic rewrite). Bounded, fail-soft, idempotent (dedup by envelope id). Master `JARVIS_INTAKE_DLQ_ENABLED` default-TRUE (the no-silent-drop guarantee).
- A small `intake_dlq.py` module owns the append + replay (pure, testable). `_TeeRouter` calls it.

### 4.3 DAG-weight pre-flight routing (G3)
- At the dispatch/intake pre-flight (where the envelope is dequeued before `GLS.submit`, OR at envelope construction), compute `is_heavy_goal(envelope.target_files, blast_radius)` and stamp a tag (`envelope.metadata["dag_weight"]="heavy"` / a ctx flag) so the orchestrator's existing Epistemic prefetch trigger treats it as heavy → routes through the prefetch DAG (avoiding the Venom timeout on big GOALs). Reuse `is_heavy_goal`; do NOT duplicate the prefetch (it already triggers on heavy at GENERATE). This tag just makes the intake-origin heaviness explicit + observable. Gated by the existing prefetch flag; no-op when off.

### 4.4 `[A1Trace]` breadcrumbs (G4)
- One structured WARNING-level (so it survives `silent_boot` to stdout) `[A1Trace]` line at each hop, keyed by a stable `goal_id`:
  - `[A1Trace] emit goal=<id> source=roadmap` (daemon, post router-ready)
  - `[A1Trace] ingest goal=<id> router=attached` (router.ingest)
  - `[A1Trace] dequeue goal=<id>` (`_dispatch_loop`)
  - `[A1Trace] submit goal=<id> → GLS` (the submit call)
  - `[A1Trace] accept goal=<id> phase=CLASSIFY` (orchestrator entry)
- Gated `JARVIS_A1_TRACE_ENABLED` (default-TRUE; it's the proof instrument for the soak). The chain of 5 lines in a soak's stdout *is* the A1 milestone proof.

---

## 5. Cross-cutting
- **Gating (fail-soft):** `JARVIS_INTAKE_DLQ_ENABLED` (true), `JARVIS_A1_TRACE_ENABLED` (true), `JARVIS_A1_ROUTER_READY_TIMEOUT_S` (60). The roadmap-orchestrator master (`JARVIS_ROADMAP_ORCHESTRATOR_ENABLED`) stays as-is (operator/overlay enables it for the A1 soak — `dw-cortex-soak` already does).
- **Invariants:** (I1) no strategic GOAL is ever silently lost — every drop is loud + DLQ'd + replayable. (I2) the daemon never emits before the router is ready. (I3) all additions fail-soft; with DLQ/trace off, behavior is the legacy path (minus the silent drop, which we never want back). (I4) no change to the cage / FSM authority.
- **Honest composite note (for the PR):** this is the **gate-unlock engineering** for the autonomous-PR track record (the #1 composite-gating factor). It does NOT itself move ~35% — the number moves when `file-00 → autonomous PR` runs *for real on a host* and accumulates under shadow. This arc makes the pipe **correct, recoverable, heavy-GOAL-safe, and observable**; the soak generates the evidence.

## 6. Test strategy
- Unit: event-driven valve (subscribe-then-check, ready-before-subscribe no deadlock, timeout→DLQ); DLQ append (atomic, schema) + replay (drain, dedup, fail-soft); `is_heavy_goal` tag stamping; breadcrumb emission at each hop (fakes).
- Interaction: daemon waits for `intake.router.ready` then emits → router ingests → dispatch → submit (with fakes); orphaned-envelope (None router) → DLQ not silent-capture.
- OFF byte-identical (minus the silent drop). Reused-subsystem regression (intake, roadmap_orchestrator, trinity_event_bus, governed_loop).
- **Live proof (operator, real host):** `--production-soak` with the roadmap orchestrator enabled → the 5 `[A1Trace]` lines appear in order in stdout → first autonomous PR.

## 7. Phasing
1. DLQ + event-valve (the correctness core — kills the silent drop + the race).
2. DAG-weight tag + `[A1Trace]` breadcrumbs (routing + proof).
3. (Deferred) Backpressure Health Gate — after a soak proves the pipe flows.
