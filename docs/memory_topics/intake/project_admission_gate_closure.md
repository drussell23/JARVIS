---
title: Project Admission Gate Closure
modules: [backend/core/ouroboros/governance/admission_gate.py, backend/core/ouroboros/governance/admission_estimator.py, backend/core/ouroboros/governance/ide_observability_stream.py, backend/core/ouroboros/governance/ide_observability.py]
status: historical
source: project_admission_gate_closure.md
---

May 2: 3-slice arc closed same-day; addresses 90-min wide-pool soak failure mode where multiple sensors natively classify IMMEDIATE simultaneously (RuntimeHealth python_eol/torch_outdated + GitHub critical) saturating the per-route semaphore queue → ops time out on sem.acquire BEFORE API call (sem_wait_total_s=146 > pre_sem_remaining_s=120).

Slice 1 — Pure-stdlib substrate at `backend/core/ouroboros/governance/admission_gate.py` (~580 LOC):
  - `AdmissionDecision` 5-value closed enum (ADMIT / SHED_BUDGET_INSUFFICIENT / SHED_QUEUE_DEEP / DISABLED / FAILED) with `_PROCEED_OUTCOMES` (fail-open) + `_SHED_OUTCOMES`
  - Frozen `AdmissionContext` (route, remaining_s, queue_depth, projected_wait_s, op_id) + `AdmissionRecord` (proceeds/is_shed/to_dict/from_dict)
  - Total `compute_admission_decision()` NEVER raises — exception path → AdmissionDecision.FAILED → fail-open
  - Master flag default-False at Slice 1, env knobs: min_viable_call_s (25.0), budget_safety_factor (1.2), queue_depth_hard_cap (16), estimator_alpha (0.3) — all clamped, no hardcoding
  - 58/58 tests including pure-stdlib substrate AST pin + no-exec/eval/compile
  
Slice 2 — `WaitTimeEstimator` at `backend/core/ouroboros/governance/admission_estimator.py`:
  - Per-route rolling EWMA (alpha-weighted), threading.Lock-guarded, NEVER raises
  - Memory-bounded by route enum (5-6 entries); cold-start sane (first observation initializes EWMA at observed value)
  - Wired into `candidate_generator._call_fallback`: pre-acquire admission check (compute_admission_decision with projected_wait_s from estimator) + post-acquire estimator.update_observed
  - Default-off behind master flag; `decision.proceeds()` fail-open path preserves legacy behavior
  - 34/34 estimator tests + 10/10 integration tests
  
Slice 3 — Graduation:
  - Master flag flipped False→True with empty/whitespace-as-unset asymmetric env semantics
  - `register_shipped_invariants()` returns 4 AST pins:
    - `admission_decision_vocabulary` — 5-value enum frozen
    - `candidate_generator_admission_check_present` — BUG-FIX REGRESSION PIN, validates _call_fallback contains compute_admission_decision + is_shed + pre_admission_shed branch (refactor cannot silently delete the fix)
    - `compute_admission_decision_total` — no raise statements in body
    - `admission_gate_no_caller_imports` — substrate stays caller-agnostic (allows only flag_registry + meta.shipped_code_invariants registration-contract imports)
  - `register_flags(registry)` returns 5 FlagSpecs (master TRUE, min_viable 25.0, safety 1.2, queue_cap 16, alpha 0.3) all in Category.SAFETY since="AdmissionGate Slice 3 (May 2 2026)"
  - `RecentDecisionsRing` class added to admission_estimator.py: thread-safe deque(maxlen) bounded ring with capacity clamp [4, 4096] env-tunable via JARVIS_ADMISSION_HISTORY_RING_SIZE (default 64); silently drops non-dict input
  - Module-level singletons `get_default_estimator()` / `get_default_history()` / `reset_singletons_for_tests()` at module scope (avoid global pollution; testable)
  - `EVENT_TYPE_ADMISSION_DECISION_EMITTED = "admission_decision_emitted"` SSE event added to ide_observability_stream.py + `_VALID_EVENT_TYPES` frozenset
  - History-record + SSE-publish wired into `_call_fallback` after pre-admission decision (both ADMIT and SHED branches)
  - GET route `/observability/admission-gate` registered in ide_observability.py: bounded JSON projection (config + estimator stats + history snapshot), `?limit=N` clamped [1, 200] default 50, returns 403 admission_gate_disabled when master off, 403 disabled when umbrella off, 400 malformed_limit on bad query
  - 55/55 graduation tests + 156/156 combined Slice 1+2+3 sweep green

Solves root problem (pre-admission viability check → load-shed instead of timeout-after-acquire) without hardcoding, brute force, or workaround. Cost contract preserved by construction (substrate never invokes provider; SHED returns IMMEDIATE→DISABLED → fail-open caller proceeds normally).

Pre-existing flag_registry test_ensure_seeded_installs_specs failure (203 vs 146 dynamic-discovery vs SEED_SPECS gap) NOT caused by Slice 3 — verified previously via git stash.

Deferred: /admission REPL verb + /observability/admission-gate IDE consumer panel + production admission_estimator update from PostmortemRecall route projection.
