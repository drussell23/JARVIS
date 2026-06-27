---
title: Project Slice194 Race Triage
modules: []
status: historical
source: project_slice194_race_triage.md
---

**Slice 194 — Race-Triage & Cross-Model Rotation Matrix (MERGED #69433, main `06aa64ece8`, 2026-06-09).**

**Why:** [[project-slice193-observability-registry]] exposed it live: `dispatches − victories = 2` races died with no winner (op-019eae7b: RT RuntimeError + structural batch rejection) while the op re-walked the same dead model every ~2 min.

**How to apply:** (1) `hedge_races_abandoned` added to registry charter + `record_hedge_abandoned()`. (2) `hedged_race(on_abandoned=(fast_exc, stable_exc))` fires ONLY when both arms fail (per-arm capture; sink errors never change the raise). (3) `race_triage.py`: `classify_arm` → VENDOR/INTERNAL_FAULT/CANCELLED/ABSENT; `triage_dual_failure` hard blockage requires BOTH arms vendor-lane — Slice 185 carve-outs: internal fault (NameError/TypeError/bare-ValueError-without-status_code) NEVER blames the model; cancelled/absent arm = no evidence. Blacklist rides `schema_drift_tracker` bounded storage (new `DriftType.DUAL_ARM_FAILURE`, /drift audit free) with OWN predicate `is_blacklisted_for_op` gated `JARVIS_RACE_TRIAGE_ENABLED` default-TRUE (failure-path-only, Slice-170 precedent) — INDEPENDENT of default-FALSE `JARVIS_SCHEMA_DRIFT_ROTATION_ENABLED` (key trap: `has_drifted` is master-gated, `events_for` isn't). (4) Provider `_s194_on_abandoned` counts→triages→blacklists (model via `_resolve_effective_model`). (5) Sentinel walker skips blacklisted (`skipped_dual_arm`) → next iteration IS next-ranked catalog candidate, same cycle. 21 tests (`test_slice194_race_triage.py`) incl. synthetic dual-arm acceptance; 428 regression green. KNOWN pre-existing main failure (NOT this slice): `test_slice12af_universal_route_taxonomy.py::TestSite5GenerateRunnerDefensiveRaise` generate_runner source pin, fails on clean 9eff300d0f. Soak container runs 193 — rebuild needed to pick up 194.
