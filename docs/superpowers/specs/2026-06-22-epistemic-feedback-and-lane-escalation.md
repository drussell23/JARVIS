# Adaptive Epistemic Feedback Matrix + Dynamic Lane Escalation — Design Spec

> **Arc:** the final-leg robustness of the generation/review pipeline. Two related matrices, both signal-driven (no hardcoded retry caps), both reuse-first.
> **Date:** 2026-06-22. Branch `worktree-epistemic-lane-matrix`. Feeds the live C2 convergence soak.
> **Live context:** the State-Propagation Bridge ([[project_sovereign_state_propagation_bridge]]) fixed the decompose severance; sub-goals now reach GENERATE. The live blocker is now **DW batch-lane TIMEOUT exhaustion** (Part 2) upstream of the **review-rewrite deadlock** (Part 1).

---

## PART 1 — Adaptive Epistemic Feedback Matrix (review-rewrite deadlock)

### 1.1 Diagnosis (reuse-first)
The L2 `repair_engine` ALREADY does: multi-iteration FSM, `failure_signature_hash` (SHA-256 of sorted failing-test-ids + class), oscillation detection (`seen_pairs`), classification, adaptive per-iteration budgets, structural pre-flight, multi-file repair. **Genuine gaps:** (a) the repair prompt gets only a 300-char truncated `failure_summary` — no diff, no full trace; (b) temperature is a global constant `_DW_TEMPERATURE=0.2`, never lowered per attempt (the `generate(temperature=X)` override is plumbed but unused); (c) on exhaustion the op terminates `cancel` — no decompose-further pivot. Anchors: `repair_engine.py:1207-1220` (RepairContext build), `repair_sandbox.SandboxValidationResult.stderr` (full stderr captured), `doubleword_provider.py:5101-5202` (`generate(temperature=)`), `orchestrator.py:~7690` (`_l2_hook` soft-stop seam), `goal_decomposition_planner.decompose_for_block`.

### 1.2 Components
**`epistemic_feedback.py` (new pure leaf):**
- `build_failure_context(*, prior_src, failed_src, stderr, failing_tests, sub_goal_label) -> str` — the **Hybrid Epistemic Diff**:
  1. **Safe AST probe (fail-soft):** `try: ast.parse(failed_src)`. On `SyntaxError as e`: prepend a `[SOVEREIGN SYNTAX FATAL] line={e.lineno} msg={e.msg}` header (the model's #1 hint). Probe error other than SyntaxError → skip header. NEVER raises.
  2. **Labeled unified diff:** `difflib.unified_diff(prior_src, failed_src, "Previous Stable Sub-Goal", "Current Failing Iteration")` (stdlib, zero parse-risk), bounded (`JARVIS_EPISTEMIC_DIFF_MAX_CHARS` default 4000).
  3. **Stderr trace tail:** the last `JARVIS_EPISTEMIC_TRACE_MAX_CHARS` (default 2500) of `stderr` (the real stack trace + assertion) — replacing the 300-char truncation. + the failing test ids.
  Returns the assembled block (header? + labels + diff + trace). Fail-soft → returns whatever it has (or "").
- `temperature_for_attempt(base_temp, repeated_signature_count) -> float` — **Parametric Degeneration:** decay `base_temp * (JARVIS_EPISTEMIC_TEMP_DECAY default 0.5) ** repeated_signature_count`, floored at `JARVIS_EPISTEMIC_TEMP_FLOOR` (default 0.0). Trigger is the **repeated `failure_signature_hash`** (the model making the SAME logical error), NOT a blind count.
- `pivot_verdict(repeated_signature_count, temp_at_floor) -> bool` — **Unresolvable-path detection:** True when the same signature persists AND temperature already hit the floor (deterministic + identical error ⇒ retry cannot help). Signal-based.

### 1.3 Threading (reuse, no parallel loop)
- `RepairContext` (frozen) gains `prior_iteration_diff: str = ""` + `failure_trace: str = ""`. `repair_engine._run_inner` (~1207) tracks the prior candidate src + computes `build_failure_context(...)` into these fields; the repair prompt builder injects them.
- `repair_engine` tracks `repeated_signature_count` per `failure_signature_hash` (it already computes the hash + `seen_pairs`); passes `temperature_for_attempt(_budget.base_temp, count)` into `_generate_repair_candidate` → `generate(temperature=…)`.
- **Semantic Pivot:** when `pivot_verdict` true, repair_engine returns a new directive (e.g. `("l2_pivot", ctx, failure_signature_hash, stderr_tail)`). The orchestrator `_l2_hook` consumer: emit `[SOVEREIGN YIELD: UNRESOLVABLE PATH]` → `decompose_for_block(_BlockGoal(...), failure_hint={signature_hash, stderr_tail})` so the chunker splits **at the failure locus** (pass the hint; the decomposer scopes the failing symbol/region first) → re-inject sub-goals. If `decompose_for_block` yields nothing (already atomic) → `intake_dlq.append_dlq(ctx, reason="l2_unresolvable_awaiting_human")` (HITL flag). **The rest of the DAG keeps running** (only this sub-goal pivots). Gated `JARVIS_EPISTEMIC_FEEDBACK_ENABLED` (default true; off → exact legacy L2).

## PART 2 — Dynamic Lane Escalation (DW batch-lane TIMEOUT wall)

### 2.1 Diagnosis (live, reuse-first)
The candidate_generator already rotates all 13 DW models + cascades to Claude. Live failure = ALL models `fsm_exhausted:TIMEOUT` — a **batch-lane latency wall** (aegis shows batches POSTed `201` + polled `200`/594B but never completing in deadline). The transport breaker (`transport_circuit_breaker.py`, batch→realtime rotation) IS armed but **blind to these** because batch timeouts surface as the generic `fsm_exhausted` which the breaker's record-filter excludes (the I2 filter from the resilience arc). So batch never trips → never rotates to realtime.

### 2.2 Components
1. **Breaker Vision Fix:** at the batch dispatch/classification site (candidate_generator), when a failure is a **batch-lane TIMEOUT** (origin = batch poll/retrieval timeout — distinguishable from a generic fsm_exhausted by the batch lane + TIMEOUT failure_mode), record it to the transport breaker as a trippable batch-lane failure (NOT filtered). The batch lane trips OPEN → `select_lane` rotates the sub-goal to the **realtime** endpoint without losing op state. Gated `JARVIS_LANE_ESCALATION_ENABLED` (default true); off → exact legacy (breaker stays blind, byte-identical).
2. **Stateful Escalation + Deadline Dilation:** if realtime ALSO times out for the same op → emit `[SOVEREIGN YIELD: LANE COLLAPSE]`. The orchestrator catches it and **dilates the generation deadline** for that op's next tick by `JARVIS_LANE_DILATION_FACTOR` (default 1.5×, capped at `JARVIS_LANE_DILATION_MAX_S`) — assuming DW is under heavy global load — instead of looping at the same deadline. Bounded: at most `JARVIS_LANE_DILATION_MAX_HOPS` (default 2) dilations per op, then fall to the existing immortal-queue/DLQ (no infinite loop). Reuses the deadline plumbing in candidate_generator (`_PRIMARY_MAX_TIMEOUT_S` / per-op deadline).

### 2.3 Reuse anchors
`transport_circuit_breaker.select_lane`/record; `candidate_generator` batch dispatch + `FailureSource`/TIMEOUT classification + the per-op deadline; `convergence_watchdog.emit_sovereign_yield` (extend the YIELD kinds); `doubleword_provider` batch vs realtime (`_generate_realtime` vs `_generate_via_batch`).

## Cross-cutting invariants
- **No hardcoded retry caps:** Part 1 triggers on repeated `failure_signature_hash`; Part 2 bounds dilation by env hops + the existing immortal/DLQ backstop. Curves are env-tunable.
- **Fail-soft + DAG-preserving:** every new path degrades to the existing behavior; a pivoting/escalating sub-goal NEVER severs the rest of the DAG; off-flags are byte-identical.
- **Reuse-first:** no parallel repair loop, no parallel breaker, no parallel provider rotation. Extend `RepairContext`, the breaker record, `decompose_for_block`, `emit_sovereign_yield`, the deadline plumbing.
- **Pure where possible:** `epistemic_feedback.py` is pure stdlib (ast/difflib), no model calls, no I/O.

## Test strategy
- Part 1 unit: hybrid diff (clean parse → labeled diff; SyntaxError → `[SOVEREIGN SYNTAX FATAL]` header + still a diff; fail-soft on garbage); `temperature_for_attempt` decay+floor; `pivot_verdict` (persists-after-floor only). repair_engine: prior-diff+trace threaded into RepairContext; temperature lowered on repeated signature; pivot directive on unresolvable. Pivot seam: decompose-further with the failure hint; atomic → DLQ HITL; DAG not severed.
- Part 2 unit: a batch-lane TIMEOUT records→trips the breaker (where a generic fsm_exhausted does NOT); rotation to realtime; realtime-timeout → LANE COLLAPSE → deadline dilated next tick, bounded by max-hops then immortal/DLQ. OFF byte-identical for both.
- Static: both default-on but degrade to legacy; no infinite loop provable (signal triggers + bounded dilation + DLQ backstop).

## Phasing
1. `epistemic_feedback.py` + tests (pure). 2. repair_engine threading (diff+trace+temperature) + tests. 3. Part-1 pivot seam (`_l2_hook` → decompose/DLQ) + tests. 4. Part-2 breaker vision fix (batch-timeout trips) + tests. 5. Part-2 lane collapse + deadline dilation + tests. 6. Integration + final cross-cutting review. Then the operator soak.
