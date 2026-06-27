---
title: Project Vector 5 Part B Phase8 Wiring
modules: [backend/core/ouroboros/governance/observability/phase8_producers.py, tests/governance/test_vector_5_part_b_phase8_wiring.py]
status: merged
source: project_vector_5_part_b_phase8_wiring.md
---

May 9 2026: §35 row 🟡 #3 Part B + §3.6.3 priority #3 Part B ✅ Shipped. Vector #5 fully closed (Part A + B).

**Three minimal-touch wiring sites** all compose the canonical
`backend/core/ouroboros/governance/observability/phase8_producers.py`
wrapper (NEVER raises; SSE publish piggy-backs on the same call;
substrate's master flag gates the underlying ledger):

1. `phase_runners/route_runner.py:212-237` — after
   `UrgencyRouter.classify` returns and `provider_route` is stamped on
   the ctx, calls `record_decision(op_id, phase="ROUTE", decision=
   route.value, factors={signal_urgency, signal_source,
   task_complexity}, rationale=route_reason)`. Best-effort try/except;
   routing must always succeed.

2. `semantic_triage.py:382-407` — after the LLM-driven triage decision
   is logged, calls `record_confidence(classifier_name=
   "semantic_triage", confidence=result.confidence, threshold=0.5,
   outcome=result.decision.name, op_id, extra={model, files})`.

3. `phase_dispatcher.py:792-818` — wraps `await runner.run(ctx)` with
   `time.monotonic()` deltas (Vector #11 NTP-immune discipline) and
   composes BOTH `record_phase_latency(phase=dispatch_phase.name,
   latency_s)` and `check_breach_and_publish(phase)` at the SAME call
   site so operators see live SLO violations without a separate
   observer loop. Single choke point covers all 9 extracted
   PhaseRunners with one wiring.

**Why phase8_producers wrapper, not direct substrate calls**: the
canonical wrapper handles substrate.record() + Slice 2 SSE bridge
publish in one call; future producer-side optimizations (batching,
sampling) live behind the wrapper without touching the 3 production
call sites.

**3 stale Phase 8 substrate tests synced** (DecisionTraceLedger
graduated default-TRUE 2026-05-05 via Phase 9 cadence; prior tests
still asserted default-FALSE):
- `test_phase_8_temporal_observability::test_default_false` →
  `test_default_true_post_graduation` + new
  `test_explicit_false_disables`
- `test_phase_8_temporal_observability::test_constants` →
  `SCHEMA_VERSION == "2"` (was "1" — predecessor_ids/decision_tier/
  decision_hash_digest fields added)
- `test_phase_8_temporal_observability::test_master_off_skips` → uses
  explicit `=false` rollback
- `test_p9_5_coherence_and_producer_wiring::test_substrate_flag_snapshot_default_all_false`
  → renamed `_default_post_graduation`; expects
  `decision_trace_ledger: True` only
- `test_p9_5_coherence_and_producer_wiring::test_record_decision_master_off_returns_false`
  → uses explicit `=false` rollback

**19 regression tests** in
`tests/governance/test_vector_5_part_b_phase8_wiring.py`:
- 3 AST pin pass tests (positive: production source is wired)
- 7 pin synthetic regressions (negative: pin fires when wiring removed
  — phase8_producers stripped / phase label renamed / classifier
  renamed / monotonic anchor replaced with wall-clock /
  check_breach_and_publish removed / etc.)
- 3 functional integration (master-on each producer, assert record
  succeeds end-to-end)
- 2 master-off rollback (graduated DTL flag escape-hatch + still-
  default-FALSE confidence ring path)
- 3 import-cleanly (route_runner / semantic_triage / phase_dispatcher
  load with the new producer imports)
- 1 producer-count pin (exactly 3 production files compose
  phase8_producers — adding a 4th forces reviewer attention).

**Test results**: 19/19 Vector #5 Part B + **165/165 cumulative**
across Phase 8 + P9.5 + Vector #5 Part A + Part B + cross-session
coherence sweep.

**Architecture preserved**:
- Authority asymmetry: producer wrapper is the substrate boundary;
  call sites compose it lazily; orchestrator hot path NEVER imports
  ledgers directly.
- §38.11.5a.5 single-canonical-name discipline: ZERO parallel producer
  paths; `phase8_producers` is the only surface and it owns substrate
  + SSE composition.
- Vector #11 monotonic-clock discipline: dispatcher's phase timing
  uses `time.monotonic()` (NTP-immune) for both anchor and delta.
- §33.4 NEVER-raises contract: every producer call wrapped in
  try/except logging at debug; pipeline never blocks on observability
  failure.

**§35 row 🟡 #3 Part B + §3.6.3 priority #3 Part B both flipped to
✅ Shipped 2026-05-09**. Vector #5 fully closed.

**NEXT** (autonomy arc remaining):
- **M10 ArchitectureProposer** (~7-10d substrate move closing weak-
  form ontogeny gap)
- **Phase 9 empirical graduation cadence** (~6-9 weeks operator-
  paced soaks; flips remaining 11 substrate flags from default-FALSE
  → default-TRUE with 3-clean-session evidence ladders)
