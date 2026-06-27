---
title: Sovereign State-Propagation Bridge (2026-06-22, MERGED PR #69662)
modules: [backend/core/ouroboros/governance/semantic_index.py, orchestrator.py]
status: merged
source: project_sovereign_state_propagation_bridge.md
---

# Sovereign State-Propagation Bridge (2026-06-22, MERGED PR #69662)

**THE convergence blocker** that stopped a dispatched strategic GOAL from reaching the autonomous orange PR (downstream of A1 dispatch + chunking + egress + watchdog — all of which were already working).

**Symptom (live C2 soak, observed via the autopsy sentinel):** every roadmap GOAL → `[A1Trace] accept (router=attached)` → `Advisor BLOCKED` (blast 50 + 0% cov on semantic_index.py 3247L etc.) → `CRITICAL [Chunking] decompose emitted 0 sub-goals -- falling back to advisor_blocked` → `[IntakeDLQ] orphaned GOAL reason=decompose_emitted_zero`, looping. Diagnostic: `verdict=progressing; total=2 blocked=1 ready=1 emitted=0; this_tick_emitted=1`.

**ROOT CAUSE (traced to the line — I PUSHED BACK on the operator's "object-state translation / re-bind the object" hypothesis, which was wrong):** the DAG was never severed and binding never broke (`router.ingest` SUCCEEDS — `this_tick_emitted=1`). `multi_step_orchestrator.advance_orchestration` loads `completion_status` ONCE *before* the emit loop (line ~885). `emitted_count` is aggregated by `_aggregate_counts`→`compute_run_state(sub, completion_status)`, which marks a record `EMITTED` only if its OWN status ∈ {proposed,in_progress} *in that pre-emit ledger*. `emit_outcomes` only stamps `emitted_at_unix`, never flips `run_state`. So a just-`ingest`ed READY sub-goal stays `READY` in the aggregate → **`emitted_count` STRUCTURALLY reads 0 on every fresh decompose** even though a sub-goal was dispatched. The orchestrator I1 gate `if report.emitted_count >= 1` (orchestrator.py ~2471) thus false-negatived EVERY first emit → DLQ + `advisor_blocked`. `emitted_this_tick` (the ground truth, a local var) wasn't even exposed on the report.

**FIX (root-cause; no object-rebinding bridge — that would fix a non-problem; no duplicated ledger):**
- `OrchestrationReport` exposes `emitted_this_tick: int = 0` (default keeps early-return sites byte-identical) + `@property made_forward_progress = emitted_count>=1 OR emitted_this_tick>=1`.
- Orchestrator I1 gate reads `report.made_forward_progress` → terminate `decomposed` when a sub-goal actually dispatched this tick. `ledger.mark(h)` (dedup backstop) now correctly reached on real dispatch.
- **Zero-Drop policy:** a REAL drop = `to_emit` non-empty (ready sub-goals selected) but `emitted_this_tick==0` (all `router.ingest`/envelope failed) → loud `logger.critical("[SovereignPropagation] REAL DROP ...")`. Never a silent ready-drop. (`to_emit` = compute_ready_set→READY only, so legitimately-empty ticks don't false-alarm.)
- SSE `_publish_event` parity (emitted_this_tick + made_forward_progress).

**Opus review APPROVE (no Critical/Important):** diagnosis confirmed exactly; genuine structural repair NOT a symptom relabel; master-off/no-plan/dedup paths byte-identical; parent terminating `decomposed` while the sub-goal is an independent in-flight intake op is pre-existing correct hand-off. **CAVEAT (load-bearing):** this unblocks the false-DLQ, but full convergence STILL requires `router.ingest` to actually succeed end-to-end on the node so the dispatched sub-goal flows to `state=applied` → orange PR — that's the soak's job. The new telemetry disambiguates: `BLOCK decomposed ... dispatched_this_tick=N` (progress) vs `[SovereignPropagation] REAL DROP` (genuine dispatch failure). 6 bridge tests (incl. the exact `emitted_count=0 ∧ emitted_this_tick=1` trap reproduction) + 101 governance regression green.

**Status:** MERGED + **LIVE-PROVEN on the C2 soak (node …162951, 2026-06-22).** The autonomous sentinel captured the exact fix firing: `+ DECOMPOSE | [Orchestrator] BLOCK decomposed into 2 sub-goals (test_first=True, emitted=0 dispatched_this_tick=1)` — the lagging aggregate still reads `emitted=0` but the gate now reads `made_forward_progress` (dispatched_this_tick=1) → terminates `decomposed` (NOT the old `decompose emitted 0 → DLQ → advisor_blocked` severance). The DAG baton is no longer dropped; sub-goals re-inject as new ops. **The convergence severance is FIXED.** Remaining to observe: the dispatched test+mutation sub-goals flow to generation → orange PR (the node runs ~1h; chain flowing, 68+ transitions, zero false-kills). **LESSON: trace the exact counter before building the operator's prescribed "bridge" — the binding worked; the success gate read the wrong (lagging) field.** See [[project_sovereign_sentinel_autopsy]], [[project_sovereign_resilience_chunking]], [[project_a1_intake_dispatch]].
