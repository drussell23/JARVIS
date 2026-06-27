---
title: Project Slice197 Autonomous Graduation
modules: []
status: historical
source: project_slice197_autonomous_graduation.md
---

**Slice 197 — Autonomous Graduation Contract + Adaptive Synthesis Governor (MERGED #69436, main `9fe63875d9`, 2026-06-10).**

**Why:** M10 ArchitectureProposer deadlocked behind §30.5.2 static default-false (audit needs proposals ↔ proposals need flag). §41.6 evidence rows frozen at 0 while soak accumulated uptime-only evidence.

**How to apply:** Design = OPERATOR-DELEGATED conditional authorization, NOT self-authorization (operator pushback honored: kill-switch supreme + boundary gate untouched). (1) `m10/primitives.m10_arch_proposer_enabled` three-state: explicit on → on; explicit off/garbage → OFF (supreme, S136 precedent); unset → `m10_autonomous_graduation.is_autonomously_unlocked()` (fail-soft False). (2) Registry charter counters added: `provider_exhaustions` (wired at candidate_generator exhaustion-funnel helper ~line 2146) + `control_plane_starvation_events` (wired at control_plane_watchdog lag site ~line 498) — criteria are pure .bin reads. (3) `m10_autonomous_graduation.py`: criteria env-tunable (`JARVIS_M10_GRAD_MIN_DISPATCHES`=5 evidence floor / `_MAX_EXHAUSTIONS`=0 / `_MAX_ABANDONED_RATIO`=0.25 / `_MAX_STARVATION_EVENTS`=50); STICKY unlock persisted `.jarvis/m10_graduation_state.json` (stamped metrics audit artifact, `JARVIS_M10_GRADUATION_STATE_PATH`); 30s lazy-eval TTL; master `JARVIS_M10_AUTONOMOUS_GRADUATION_ENABLED` default TRUE (merged PR = operator act superseding §30.5.2). (4) Pacing: `effective_cadence_n(base_n, dispatch_delta, cost_burn_ratio)` — busy(≥10)→2×, idle(0)→0.5×, cost_burn≥0.8→conserve; wired into `cadence_runner.should_fire_at` via `_recent_dispatch_delta()` registry deltas. **Pins:** boundary gate has ZERO coupling to this module (grep-pinned `test_boundary_gate_not_weakened`); evidence floor blocks empty registries (also prevents test-env stray unlocks). 26 tests; 297 regression. **NOTE: M10 graduates AUTONOMOUSLY on the soak container once registry shows ≥5 dispatches + clean health — watch for `[M10Graduation] AUTONOMOUS GRADUATION` WARNING + state file on host .jarvis. Revoke = `JARVIS_M10_ARCH_PROPOSER_ENABLED=0`.** See [[project-slice193-observability-registry]], [[project-slice194-race-triage]]
