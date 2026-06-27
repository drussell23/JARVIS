---
title: Project Vector 5 Cross Session Harness
modules: [backend/core/ouroboros/governance/cross_session_harness.py, tests/governance/test_vector_5_cross_session_coherence.py]
status: historical
source: project_vector_5_cross_session_harness.md
---

May 9 2026: §35 row 🟡 #3 Part A + §3.6.3 priority #3 Part A ✅ Shipped. Vector #5 Part B (Phase 8 producer wiring) remains open as separate architectural concern.

**Substrate** `cross_session_harness.py` (~970 LOC pure-stdlib):
- Closed 4-value `CoherenceAxis` (USER_PREFS / ADAPTATIONS / SEMANTIC_CENTROID / SESSION_HISTORY) — one axis per canonical cross-session memory substrate
- Closed 4-value `DriftLevel` (STABLE / DRIFTING / DIVERGED / CORRUPTED) — asymmetric drift semantics: additive growth → STABLE; deletion → DRIFTING; same-count-different-hash → DIVERGED; load failure → CORRUPTED
- Frozen §33.5 artifacts: `AxisDigest` + `CrossSessionDigest` + `AxisDrift` + `CoherenceReport` with `digest_for_axis()` + `to_dict()` projections

**Per-axis digesters compose canonical sources only** (ZERO parallel state):
- `_digest_user_prefs` → `UserPreferenceStore.list_all` + sorted (id, type, content_hash) tuples
- `_digest_adaptations` → `AdaptationLedger.history(limit=500)` with volatile iso timestamps stripped from fingerprint
- `_digest_semantic_centroid` → `SemanticIndex.snapshot_global_centroid()` rounded to 6 decimals (legitimate-empty distinguished from CORRUPTED — first-boot state is STABLE not corruption)
- `_digest_session_history` → `LastSessionSummary.load(n_sessions=10)` with immutable session_id + stop_reason + stats fingerprinted

**Bytes-pinned `_AXIS_DIGESTERS` dispatch tuple** ensures every CoherenceAxis enum value has a registered digester (AST regression enforces).

**Pure-function `compute_drift(before, after)`** with first-match-wins decision tree.

**`simulate_session_boundary(project_root)`** resets canonical in-process default singletons (`UserPreferenceStore.reset_default_store` + `LastSessionSummary.reset_default_summary`) so the next digest reads from disk rather than stale cache — proves persistence boundaries actually work.

**`report_coherence((d1, d2, ..., dN))`** walks N-session arc producing per-boundary drift records + `overall_stable: bool` (every axis at every boundary lands STABLE — strictest bar; deletions or hash-rewrites fail).

**New SSE event** `EVENT_TYPE_COHERENCE_REPORTED` registered in canonical `_VALID_EVENT_TYPES` frozenset.

**6 AST pins**:
1. master_default_false (§33.1)
2. axis_taxonomy_4_values (CoherenceAxis frozen)
3. level_taxonomy_4_values (DriftLevel frozen)
4. **composes_all_4_substrates** (bytes-pin requires user_preference_memory + adaptation.ledger + semantic_index + last_session_summary substrings)
5. **digesters_cover_all_axes** (bytes-pin every CoherenceAxis.<NAME> reference in dispatch source)
6. authority_asymmetry

**40 regression tests** in `test_vector_5_cross_session_coherence.py`:
- Master flag default-FALSE + 5 truthy
- 4-value taxonomy (axis + level)
- Frozen artifacts to_dict + axis lookup
- aggregate master-off / master-on / **deterministic on same project_root**
- **7 compute_drift parametrized scenarios** (both empty / identical / additive / deletion / same-count-different-hash / diagnostic-corrupted / axis-mismatch)
- **3-session arc with user-pref growth lands STABLE on both boundaries** (full end-to-end harness validation)
- **deletion produces DRIFTING level + overall_stable=False** (asymmetric semantics validated)
- Zero-session trivially stable + simulate_session_boundary resets singletons
- 6 AST pin canonical-source pass + 6 synthetic regressions
- 2 canonical-source smokes (`EVENT_TYPE_COHERENCE_REPORTED` registered + all 4 substrates importable)

**Test results**: 40/40 Vector #5 + **1124/1124 cumulative** across §38.11 (A-F) + §39 Tier-1+2+3+4+5+7 + Wave 3 hygiene + scheduler + Vector #5 + canonical sources.

**End-to-end smoke against 4-session synthetic arc**: empty → plant user_pref #1 (STABLE +1) → plant #2 (STABLE +1) → delete #1 (DRIFTING -1); semantic_centroid correctly STABLE both_empty across all boundaries (legitimate first-boot state); determinism verified (2 aggregate calls on same empty root → identical hashes per axis).

**§38.11.5a.5 single-canonical-name discipline honored**: harness composes all 4 canonical surfaces — ZERO parallel memory ledgers; ZERO parallel digest schemes; the 6th AST pin (`digesters_cover_all_axes`) bytes-pins the dispatch tuple to forbid axis additions without registered digesters.

**Architectural framing**: the harness is the **validation infrastructure** — it does not produce empirical proof of long-horizon coherence by itself. Operator-paced 50+ session runs will USE this harness to produce that proof; Part A ships the rails.

**§35 row 🟡 #3 + §3.6.3 #3** both flipped to `🟡 Part A SHIPPED 2026-05-09; Part B still open`.

**NEXT** (autonomy arc remaining):
- **M10 ArchitectureProposer** (~7-10d substrate move closing weak-form ontogeny gap)
- **Vector #5 Part B** (Phase 8 producer wiring — orchestrator ROUTE phase + classifiers + phase-timing → call existing decision_trace_ledger / latent_confidence_ring / latency_slo_detector record APIs) — ~1 week
- **Phase 9 empirical graduation cadence** (~6-9 weeks operator-paced soaks)
