---
title: Project Section 35 4 Artifact Contract
modules: []
status: historical
source: project_section_35_4_artifact_contract.md
---

May 9 2026: §35 row 🟡 #4 + §3.6.3 priority #4 ✅ SHIPPED. **Last structural
engineering arc** before autonomy roadmap transitions to operator-paced
Phase 9 cadence.

**Root problem** (fragility vector #8): Wave 2 PhaseRunner extraction
threads 10 cross-phase artifacts (generation / episodic_memory /
generate_retries_remaining / advisory / best_candidate /
best_validation / t_apply / risk_tier / consciousness_bridge /
cancel_token) via untyped `ctx.artifacts` Mapping. Verbatim extraction
sidestepped the issue. Any future refactor beyond verbatim — one
rename, one type drift, one phase-ownership confusion — silently
crashes the FSM mid-pipeline with no recovery path. The §35 row called
this out as the last engineering item before Phase 9 cadence becomes
the path forward.

**Closure**: single new substrate `governance/artifact_contract.py`
(~530 LOC pure-stdlib + op_context.OperationPhase only) plus a SINGLE
wiring site at `phase_dispatcher.py:828` (BEFORE
`pctx.merge_artifacts`).

**Substrate shape**:
- Closed 10-value `ArtifactKind` taxonomy mirroring `PhaseContext`
  slots 1:1 (AST-pinned coverage — adding a slot without a kind is
  a structural drift caught by `test_registry_covers_every_phase_context_slot`)
- Closed 6-value `ValidationOutcome` taxonomy (OK / UNKNOWN_KEY /
  TYPE_MISMATCH / WRONG_PRODUCER / SCHEMA_VERSION_SKEW /
  PASSED_DISABLED)
- Frozen `ArtifactSpec` dataclass with `kind` + `key` (matches
  PhaseContext slot name) + `producer_phases` (FrozenSet) +
  `consumer_phases` (FrozenSet) + `validate_value` (duck-typed
  callable — does NOT import producer types, avoids circular imports
  + reduces coupling) + `schema_version` (per-artifact, e.g.,
  `"generation.1"`)
- Bytes-pinned `_ARTIFACT_REGISTRY` tuple of 10 entries
- Pure-function `validate_artifact_value(key, value, producer_phase)`
  returning frozen `ArtifactValidation` — NEVER raises (defensive
  try/except wraps even custom validators that themselves raise →
  treated as TYPE_MISMATCH)
- `validate_artifacts_bundle` for dispatcher's iteration
- `first_failure` helper for fail-fast strict mode

**Dispatcher wiring** (single choke point):
- `phase_dispatcher.py:828` composes canonical validator BEFORE
  `pctx.merge_artifacts(dict(result.artifacts))` so unknown keys
  are rejected before they reach `extras` (where they'd be silently
  lost forever — the load-bearing positional invariant)
- AST pin `test_dispatcher_composes_validate_artifacts_bundle_at_merge_choke`
  asserts validator call site precedes merge_artifacts call site
- Master flag `JARVIS_ARTIFACT_CONTRACT_ENABLED` (default-FALSE per
  §33.1 graduation contract)
- `JARVIS_ARTIFACT_CONTRACT_STRICTNESS` selects `advisory` (log only)
  or `strict` (raise `PhaseContextError` on first failure)

**4 violation classes caught**:
1. **UNKNOWN_KEY** — rename / typo / new artifact lacking spec entry.
   The #1 most common refactor crash class. Without the contract, a
   producer renaming `generation` to `gen` silently lands in `extras`
   forever and the consumer phase reads None.
2. **TYPE_MISMATCH** — value fails per-artifact `validate_value`
   predicate. Catches e.g., `t_apply` receiving a string instead of
   float. Includes bool-rejected-as-int defense (Python bool is
   subclass of int but for numeric artifacts we want real numbers).
3. **WRONG_PRODUCER** — phase emits an artifact it doesn't own.
   Catches APPLY emitting `advisory` (CLASSIFY-only) or any phase
   emitting `cancel_token` (infrastructure-set with empty
   producer_phases — the "no phase may produce this" sentinel).
4. **SCHEMA_VERSION_SKEW** — reserved for future per-artifact schema
   bumps. `generation.2` vs `generation.1` is a single registry edit,
   not a pipeline refactor.

**32 regression tests**:
- 2 closed-taxonomy frozen pins (10 ArtifactKind values + 6
  ValidationOutcome values)
- 3 registry-coverage pins (covers every PhaseContext slot via AST
  sweep + key-equals-kind.value redundancy detection + size-pin
  forces reviewer attention on additions)
- 11 validator behavior pins (master-off PASSED_DISABLED + UNKNOWN_KEY
  rename + UNKNOWN_KEY empty + TYPE_MISMATCH on
  generate_retries_remaining/t_apply-string/t_apply-bool +
  WRONG_PRODUCER advisory-from-APPLY + correct CLASSIFY-advisory pass
  + cancel_token-from-any-phase rejected +
  custom-validator-raises-treated-as-mismatch)
- 5 bundle pins (master-off empty tuple + master-on validates each +
  non-Mapping TYPE_MISMATCH + first_failure returns first invalid +
  first_failure returns None on all-OK)
- 2 substrate authority pins (no forbidden imports + never-raises spot
  check)
- 3 dispatcher integration AST pins (composes validate_artifacts_bundle
  BEFORE merge_artifacts — load-bearing positional pin / composes
  first_failure + PhaseContextError for strict mode / cites §35 row #4)
- 2 functional integration tests (strict mode raises on UNKNOWN_KEY +
  advisory mode does not raise)
- 1 master-off byte-equivalent test (pathological bundle that would
  FAIL master-on validation produces empty tuple master-off — safe-
  revert contract)
- 3 spec round-trip + introspection
- 1 substrate provenance pin (cites §35 row #4 + fragility vector #8
  in source)

**Test results**: 32/32 §35 #4 + **1147/1147 cumulative regression
green** across §35 #4 + §37 Tier 1 #1+#2+#3 + Phase 8 + P9.5 +
Vector #5 + Wave 3 + adversarial cage + scheduler + posture (52+15) +
sensor_governor (303) + graduation_ledger + 7 v2.82 consumer files.

**Architecture preserved** (operator binding satisfied verbatim):
- ZERO parallel state schema — composes existing
  `phase_dispatcher.PhaseContext` slot declarations
- ZERO parallel transport — composes existing `PhaseResult.artifacts`
  channel
- ZERO parallel locking / validation paths — single choke point at
  `merge_artifacts` boundary
- NO hardcoding — every validator is a callable on the spec; every
  threshold env-tunable; per-artifact schema_version enables future
  shape evolution without breaking the contract
- Substrate is rails; graduation flip via 3-clean-soak ladder (Phase 9
  cadence operator-paced)

**§35 row #4 IS THE LAST STRUCTURAL ENGINEERING ITEM**. Autonomy arc
remaining work transitions to:
- **Operator-paced cadence** — Phase 9 graduation soaks (~6-9 weeks
  wall-clock validating ~24 substrate flags via 3-clean-session ladders)
- **Trigger-gated arrival** — §39 Tier 6 multi-organism cross-Trinity
  telemetry awaits J-Prime + Reactor-Core repos coming online

The architecture roadmap is structurally complete. Remaining work is
wall-clock validation + external dependencies, not engineering.

**Five §35/§37 closures landed in same day** (2026-05-09):
- v2.81 Vector #5 Part B — Phase 8 producer wiring
- v2.82 §37 Tier 1 #3 — Cross-process flock on ledgers
- v2.83 §37 Tier 1 #1 — Confidence-drop SSE payload enriched
- v2.84 §37 Tier 1 #2 — PostureObserver task-death detection
- v2.85 §35 row #4 — Cross-runner artifact contract

**NEXT**: Phase 9 graduation cadence (operator-paced). No further
structural engineering arcs in the §35/§3.6.3/§37 tables.
