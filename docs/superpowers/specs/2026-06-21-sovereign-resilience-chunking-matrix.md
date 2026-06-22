# Sovereign Resilience & Chunking Matrix — Design Spec

> **Arc:** the two downstream FSM blockers that stop a dispatched strategic GOAL (A1 proven 2026-06-22 in the wild) from converging to a merged autonomous PR.
> **Date:** 2026-06-21. **Branch:** `worktree-sovereign-resilience-chunking`.
> **Mandate:** make the AI *smarter*, never weaken the safety gate. Root-cause, not band-aids. No hardcoding (env-driven, dynamic, adaptive). Reuse-first — extend the existing FSM/sentinel/decomposition machinery, never a parallel system.

---

## 1. Diagnosis (live, from the GCP Spot soak node — already traced, not assumed)

Two independent blockers, both observed on `jarvis-ouroboros-soak-20260621-194305`:

### Blocker A — Transport-lane failure (NOT model failure, NOT payload size, NOT host)
Live evidence:
- **Host healthy:** load `0.24/0.36/0.34` on 8 vCPU — not the bottleneck.
- **DW endpoint healthy:** `GET /v1/models` → 200 in 0.33s; `POST /v1/chat/completions` (realtime, gpt-oss-120b) → **200 in 1.7s**. The **realtime/SSE lane is fast and healthy**.
- **Batch lane is the failure:** `DoublewordInfraError: Batch retrieval failed`, `fsm_failure_mode=TIMEOUT`, dispatch telemetry `batch_lane_healthy=False`. Generation routes to the **batch transport**, whose *retrieval* times out.
- **The "all 18 models exhausted" line is a symptom:** every model on the route fails because the *lane* is broken, not the models. `[Immortal] DW exhausted + NO fallback → QUEUE_ONLY … backoff 4.0s then re-attempt #2 … op NEVER lost` — the op is preserved but **re-loops onto the same dead batch lane**.

**Root cause:** the dynamic transport router *computes* `batch_lane_healthy=False` (Slice183 telemetry) but **never acts on it** — ops keep routing to batch. There is no transport-level circuit breaker that rotates a dead lane's traffic to the healthy lane. The fix axis is **transport lane (batch↔realtime)**, not model-to-model retry and not prompt compression (no 413 anywhere; the only 413 in the code is synthetic file-upload).

### Blocker B — Whole-file blast radius vetoes the strategic GOAL (correctly)
- GOAL-001 targets `semantic_index.py` (3247L) + `goal_inference.py` (1355L). The OperationAdvisor BLOCKs: *"High blast radius: 50 files import these targets; Low test coverage: 0%; … BLOCKED: Zero test coverage + extreme blast radius"* (`operation_advisor.py:1639-1653` BLOCK condition; `orchestrator.py:2396-2407` terminates `CANCELLED reason=advisor_blocked`).
- **The safety gate is correct** — an autonomous rewrite of a 3247-line untested widely-imported file is genuinely dangerous. We do NOT relax it.
- **The gap:** on BLOCK the op *terminates*. It never tries to make itself safe. And naive per-*file* decomposition keeps the whole-file blast radius (one huge file imported by 50). The GOAL needs **symbol-scoped** sub-goals + a **test-first** sub-goal to clear *both* halves of the veto.

---

## 2. Goals / Non-Goals

**Goals.** (G1) A self-healing transport circuit breaker: batch-retrieval failures trip the batch lane OPEN → traffic rotates to the healthy realtime lane → a jittered timer drives HALF-OPEN async re-probe → CLOSED on success — zero human intervention. (G2) On Advisor BLOCK, autonomously decompose the GOAL into AST-symbol-scoped, independently-testable sub-goals (with a mandatory test-first prerequisite) and re-inject them — so the system makes itself safe instead of terminating. (G3) Bound the recursion *adaptively* (no hardcoded depth/fan-out) from live load, with semantic de-dup, so it can never flood the queue or recurse infinitely. (G4) Reuse the existing FailbackFSM, topology sentinel, Slice183 lane telemetry, `goal_decomposition_planner`, `multi_step_orchestrator`, `operation_advisor`, `plan_generator`, `oracle`, `router.ingest`.

**Non-Goals.** No weakening of the OperationAdvisor heuristics (explicitly rejected). No manual transport pin / fast-unblock band-aid (explicitly rejected — self-healing breaker only). No context-window compression for this failure mode (diagnosed irrelevant). No new dispatch queue or FSM — extend the existing ones. No change to the cage / risk-tiers / boundary gate.

---

## 3. Reuse Inventory

| Need | Existing asset | Anchor |
|---|---|---|
| Failback FSM states (PRIMARY_READY/FALLBACK_ACTIVE/PRIMARY_DEGRADED/QUEUE_ONLY) | `candidate_generator.py` FailbackFSM | `:1635-1638`, `:1784` |
| **Full-jitter backoff primitive** (the ONE jitter algorithm) | `circuit_breaker.full_jitter_delay(attempt, *, base_s, cap_s, rng)` | `circuit_breaker.py:396` |
| **Dynamic recovery-window timing** (episode tracker + Slice-242 adaptive prior; composes `full_jitter_delay`) | `dw_transport_recovery.DWTransportRecovery` — `note_degraded/note_recovered/dynamic_recovery_window_s` | `dw_transport_recovery.py:76-150` |
| **Total-outage breaker** (both lanes dead → terminal pause; we COMPOSE, never duplicate) | `dual_lane_breaker.DualLaneOutageBreaker` — `record_total_outage/record_success/is_tripped` | `dual_lane_breaker.py:72-140` |
| Lane-health signal (computed, unused) | Slice183 `batch_lane_healthy` dispatch telemetry | `candidate_generator.py` (Slice183 log site) |
| Failure taxonomy + breaker primitives | `topology_sentinel.py` FailureSource, breaker states | `:429-465` |
| Transport selection (batch vs realtime/SSE) | dynamic transport router (`JARVIS_DW_DYNAMIC_TRANSPORT_ENABLED`) + `doubleword_provider` | `doubleword_provider.py` SSE `:3307-3369`, batch `:4457-4715` |
| Op-never-lost re-attempt | `[Immortal]` QUEUE_ONLY deadline-detached path | `candidate_generator.py` |
| Advisor BLOCK seam | `AdvisoryDecision.BLOCK` → terminate | `orchestrator.py:2396-2407` |
| Goal decomposition + SubGoal (narrowed target_files) | `goal_decomposition_planner.py` `SubGoal`, `heuristic_decompose` | `:330-359`, `:562-665` |
| Sub-goal envelope factory (evidence stamps parent/dep IDs) | `_make_envelope_for_sub_goal` | `goal_decomposition_planner.py:729-793` |
| Topo-ordered sub-goal emission into router | `emit_sub_goal_envelopes` | `multi_step_orchestrator.py:886-952` |
| Blast-radius computation | `_compute_blast_radius(target_files)` (+ Oracle short-circuit) | `operation_advisor.py:1729-1920`, `:1767-1788` |
| Structured plan DAG (ordered_changes) | `PlanResult` schema `plan.1` | `plan_generator.py:363-426` |
| Re-injection API | `UnifiedIntakeRouter.ingest` | `unified_intake_router.py:934-949` |
| Adaptive-governor patterns (load → budget) | `SensorGovernor` + `MemoryPressureGate` | (graduated) |
| Event-loop latency signal | `loop_sink` telemetry (`[LoopSink] … blocked_ms`) | `telemetry/loop_sink.py` |
| Semantic hashing for de-dup | `oracle` neighborhood + `state_drift` sha256 | `oracle.py`, epistemic modules |

---

## 4. Component Specs

### Matrix A — Sovereign Transport Circuit Breaker

**A1. `TransportCircuitBreaker` (new leaf module `transport_circuit_breaker.py`, pure + testable).**
A classic three-state breaker keyed **per transport lane** (`batch`, `realtime`), process-global, fail-soft:
- **CLOSED** (normal): traffic uses the lane the dynamic router selects. Each batch-retrieval failure with `mode∈{TIMEOUT, SERVER_ERROR, STREAM_STALL}` increments a rolling failure score for that lane.
- **Trip → OPEN** when the lane's failure score crosses an **adaptive** threshold (derived from the rolling window, not a hardcoded N): the lane is unhealthy. While OPEN, the breaker's `select_lane()` **rotates** all traffic to the sibling healthy lane (batch OPEN → realtime).
- **Recovery timer (jittered exponential):** OPEN carries a recovery deadline = `min(base × 2^consecutive_open, max) ± jitter`, reusing `_RECOVERY_PARAMS` magnitudes (env-tunable, never literal). No permanent pin.
- **HALF-OPEN** when the timer expires: the breaker emits **one lightweight async probe** to the OPEN lane (a tiny generation/ping through that exact transport). Probe success → **CLOSED** (resume optimal batch). Probe failure → back to **OPEN** with the next (longer, jittered) timer.
- **Self-healing invariant:** no state transition needs human input; the probe is fire-and-forget and bounded; a probe that hangs counts as failure via its own timeout.
- **REUSE (no duplicate timer/jitter):** the recovery-window timing is **delegated to one `dw_transport_recovery.DWTransportRecovery` instance per lane** (`note_degraded` on trip/probe-fail, `dynamic_recovery_window_s` for the deadline, `note_recovered` on probe-success) — which already composes `circuit_breaker.full_jitter_delay` + the Slice-242 adaptive prior. This module reimplements **no** jitter or exponential-backoff math. The genuinely-new layer is only the per-lane state machine + the adaptive failure-rate trip + `select_lane` rotation + the async probe.
- **COMPOSE with `dual_lane_breaker` (no overlap of authority):** `select_lane` rotates a single dead lane to its healthy sibling; when BOTH lanes are OPEN (total outage) it stops rotating and returns the preferred lane, leaving the terminal session-pause to `dual_lane_breaker` (which owns total-outage). The two breakers handle disjoint cases (partial vs total).

**A2. Wiring into the dispatch path.** At the transport-selection site, consult `breaker.select_lane(router_choice)` so the *computed-but-ignored* `batch_lane_healthy` finally has teeth: an OPEN batch lane forces realtime regardless of the router's first choice. Each generation attempt reports its lane + outcome to `breaker.record(lane, outcome)`. The HALF-OPEN probe is scheduled by an idle async tick (reuse an existing daemon cadence; no new always-on loop). Reuses the FailbackFSM classification (`mode=TIMEOUT` etc.) — the breaker consumes the *same* failure-mode signal, it does not re-classify.

**A3. Observability.** Structured `[TransportBreaker] lane=batch state=OPEN→HALF_OPEN probe=… ` WARNING lines; counters surfaced on the existing observability GET. Master `JARVIS_TRANSPORT_BREAKER_ENABLED` (default decided at graduation; OFF = today's behavior byte-identical).

### Matrix B — Adaptive Recursion Matrix

**B1. `AstSymbolScoper` (new leaf module, pure `ast` only — NEVER exec/eval).** Given a GOAL's `target_files` + description, parse each file's AST and isolate the specific `ClassDef`/`FunctionDef` symbols the GOAL needs (matched from the description + `plan_generator.ordered_changes` when available). Emit a symbol-scoped target descriptor (`file::Symbol`) so a sub-goal's effective change set — and thus `_compute_blast_radius` over *that* scope — collapses from whole-file to symbol-local. Fail-soft: on parse failure, fall back to file-level (degrade, never crash).

**B1a. Syntactic-integrity gate (mandatory, before any re-injection).** AST slicing must never inject uncompilable garbage into the queue. Each isolated symbol slice MUST pass a lightweight `ast.parse` round-trip (re-parse the extracted source segment / `ast.get_source_segment`) — proving the slice is a structurally valid, self-contained unit (no severed decorator, unmatched bracket, dangling `else`, or orphaned nested def). A slice that fails the round-trip is **discarded for that chunk** and the decomposer degrades to the next-coarser valid scope (enclosing symbol → whole file), logging the rejection. Never re-inject an invalid slice; never crash. This gate runs inside `AstSymbolScoper` and is a hard precondition of B5 re-injection.

**B2. Test-first prerequisite injection.** The decomposer detects the **zero-coverage** half of the BLOCK (reuse the Advisor's own coverage signal) and **prepends** a sub-goal `kind=sequential`: *"Generate a PyTest suite for `<symbols>`"*, with the mutation sub-goal carrying `depends_on=[test_subgoal_id]`. `multi_step_orchestrator` already enforces "emit only when deps COMPLETED" — so the mutation cannot run until the test exists. We satisfy the safety gate by **making the AI build its own safety net** before it mutates.

**B3. `AdaptiveRecursionGovernor` (new leaf, reuses SensorGovernor/MemoryPressureGate patterns).** Computes max decomposition **depth** and **fan-out** *dynamically at decision time* from: current `intake_priority_queue` length + event-loop latency (`loop_sink` blocked_ms) + memory pressure. Under load → budget shrinks toward 1; idle → expands. No `MAX_DEPTH=` literal anywhere — every bound is a runtime function of live signals (env knobs tune the *curve*, not the cap).

**B4. Semantic DAG de-dup.** Before re-injecting a sub-goal, compute a **semantic hash** of `(scoped_targets, normalized_description)` (reuse `oracle`/`state_drift` sha256 utilities). Cross-check against the active plan DAG + a bounded "already-attempted" ledger. Duplicate / already-attempted → **discard** (logged) → no infinite cycle, no redundant work.

**B5. BLOCK → decompose → re-inject seam (`orchestrator.py:2407`).** Replace the unconditional `CANCELLED` with: if recursion is enabled AND the governor grants budget AND this op isn't itself a de-dup repeat → run B1→B4, emit the surviving sub-goals via `multi_step_orchestrator.emit_sub_goal_envelopes` (topo order, `router.ingest`), and terminate the parent as `terminal_reason_code="decomposed"` (a *success-ish* terminal, not a failure). Otherwise (recursion off / no budget / dup) → the legacy `CANCELLED advisor_blocked` exactly as today. **Starvation safety:** sub-goal envelope urgency is mapped so re-injected children never crowd out fresh sensor signals (reuse the existing `SubGoalKind→urgency` map + the priority queue's fairness); the governor's fan-out cap is the flood guard.

---

## 5. Cross-cutting

- **Gating (fail-soft, reuse-first):** `JARVIS_TRANSPORT_BREAKER_ENABLED`, `JARVIS_RECURSIVE_CHUNKING_ENABLED` (+ curve-tuning knobs). Default OFF or byte-identical-when-off; every new call site wrapped fail-soft so an error degrades to today's path.
- **Invariants.** (I1) The OperationAdvisor BLOCK heuristics are never weakened — chunking *satisfies* them, it does not bypass them; a sub-goal that still trips the gate is itself re-evaluated, not force-passed. (I2) No op is ever lost (the `[Immortal]` guarantee is preserved; decompose is an alternative terminal, not a drop). (I3) Recursion is provably bounded — adaptive governor + semantic de-dup + already-attempted ledger ⇒ no infinite cycle, no queue flood. (I4) The transport breaker self-heals — OPEN is never permanent; HALF-OPEN probe restores batch with zero human action. (I5) No change to the cage / FSM authority / boundary gate. (I6) Pure AST only (no exec/eval) in the scoper.
- **Honest scope note (for the PR).** This turns a *dispatched* GOAL into a *convergeable* one. It does not itself merge a PR — the proof is a future soak where GOAL-001 decomposes → test-first sub-goal → symbol-scoped mutation clears the Advisor → orange PR. The current Spot node (≈6h Spot) is the diagnosis source + A1 proof, not the validation venue for this code (TDD validates the code; the soak validates the outcome).

## 6. Test strategy
- **Unit:** breaker state machine (CLOSED→OPEN on adaptive threshold; OPEN rotates lane; jittered timer → HALF-OPEN; probe success→CLOSED, fail→OPEN; fail-soft on bad input). AST scoper (symbol isolation; multi-symbol; parse-failure→file-level fallback; never exec). Test-first injection (prepend + `depends_on` wiring). Adaptive governor (load→budget monotonicity; idle→expand; no literal cap). Semantic de-dup (dup discarded; novel passes; already-attempted ledger).
- **Interaction:** BLOCK→decompose→re-inject with fakes (parent terminal=`decomposed`; sub-goals reach `router.ingest` in topo order; mutation blocks on test sub-goal). Breaker rotation under simulated batch-TIMEOUT then realtime success. Starvation: fresh sensor signal still dispatched while children queued.
- **OFF byte-identical** (both masters off → legacy BLOCK-terminates + no breaker). Reused-subsystem regression (candidate_generator, topology_sentinel, orchestrator, decomposition, intake, multi_step).
- **Live proof (future operator soak):** GOAL-001 → `[A1Trace]` dispatch → Advisor BLOCK → decompose → test-first + symbol-scoped sub-goals → Advisor passes → generation via realtime lane (breaker rotated) → orange PR.

## 7. Phasing
1. **Matrix A** (transport breaker — unblocks generation; the immediate convergence enabler).
2. **Matrix B** (recursion matrix — unblocks the safety veto). A and B are independent; either order works, but A first lets a future soak observe generation succeed even before chunking lands.
3. Cross-cutting coherence review (mandatory — caught dormant-wiring twice this program).
