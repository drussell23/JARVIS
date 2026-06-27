---
title: Production Oracle Observer Defect #2 — CLOSED 2026-05-03
modules: [scripts/production_oracle_boot_defect2_verdict.py, backend/core/ouroboros/battle_test/harness.py]
status: historical
source: project_production_oracle_observer_defect2_closure.md
---

# Production Oracle Observer Defect #2 — CLOSED 2026-05-03

2-slice arc fixing the boot wire-up gap that left the entire Production Oracle observer dormant across all soaks. Second of the four systemic defects from the soak v5 findings memo.

## Root cause

The `production_oracle_observer.run_periodic()` substrate exists and is well-tested. The `get_default_observer()` factory is callable. But **nothing in the harness boot path scheduled `run_periodic()` as an asyncio task**. Soak v5: `production_oracle_observer_tick: 0` across 62 minutes.

Without the periodic loop:
- Observer's bounded ring buffer never populates
- `GET /observability/production-oracle` returns `current=null, history=[]`
- `auto_action_router._read_recent_oracle_observation()` reads `current()=None`
- Rule 1.5 (oracle veto) falls through to existing rules
- Tier 2 #6 substrate empirically dead

## Slices shipped

- **Slice A — Harness boot wire-up + adaptive posture provider**. Inserted boot block alongside existing `_activity_monitor_task` / `_cost_monitor_task` / `_wall_clock_monitor_task` / `_restart_monitor_task` spawns. Master flag `JARVIS_PRODUCTION_ORACLE_ENABLED` gates entire path. Posture provider closure reads from `posture_observer.get_default_store().load_current()` with defensive `"EXPLORE"` fallback under any failure (broken store / None reading / missing module → most-conservative cadence). Shutdown cancellation in cleanup section 0d2.
- **Slice B — AST pin extension + verdict**. Extended `harness.py::register_shipped_invariants()` with 3 new required string literals (`_production_oracle_monitor_task`, `production_oracle_observer`, `run_periodic`). Verdict `scripts/production_oracle_boot_defect2_verdict.py` covers 5 contracts.

## Empirical-closure verdict (5/5 PRIMARY PASS)

```
[PASS] C1 AST pin extended with Defect #2 boot-markers
       invariant_violations=() new_markers_in_source=3/3
[PASS] C2 Boot wire-up references master flag + factory + posture
       references_found=4/4
[PASS] C3 Shutdown cancellation path present
       task_referenced_count=7
[PASS] C4 Synthetic boot tick populates observer current()
       adapters_queried=4 adapters_failed=0 signals=3
       verdict=failed current_is_populated=True
[PASS] C5 Posture provider robust + Posture enum stable
       valid_postures=['CONSOLIDATE', 'EXPLORE', 'HARDEN', 'MAINTAIN']
```

C4 headline: synthetic boot-style schedule produces fully-populated observation with all 4 adapters queried (stdlib_self_health + http_healthcheck + sentry + datadog), 0 failures, 3 signals, verdict=failed (StdlibSelfHealthOracle correctly identifies harness completion-rate failure mode).

## Architectural decisions worth remembering

- **Adaptive posture provider, not static cadence**. Cadence adapts: HARDEN→60s, MAINTAIN→300s, EXPLORE/CONSOLIDATE→180s. Satisfies user's "advanced/dynamic/adaptive" directive without hardcoding.
- **Defensive provider closure (NEVER raises)**. Posture-provider lambda has try/except wrapping entire chain. Any failure → `"EXPLORE"`. Observer's tick is more important than cadence accuracy.
- **Shutdown cancellation parallel to existing monitor tasks**. Mirrors `_restart_monitor_task` pattern in section 0d. New task gets section 0d2.
- **Master-flag gated boot**. Operators flipping `JARVIS_PRODUCTION_ORACLE_ENABLED=false` bypass entire boot path.
- **AST pin extension, not new pin**. Reused existing `wall_clock_watchdog_substrate` invariant — both protect harness boot wire-ups for monitor tasks. Keeps AST-pin surface lean.

## Reuse contract honored (no duplication)

- Existing `asyncio.ensure_future` monitor-task spawn pattern reused
- Existing `posture_observer.get_default_store()` + `Posture` enum reused
- Existing shutdown cleanup section pattern reused (cancel + await + swallow)
- Existing `wall_clock_watchdog_substrate` AST pin extended (no parallel pin)
- Master flag `JARVIS_PRODUCTION_ORACLE_ENABLED` reused (no new flag)

## Soak v5 false-positive correction (also landed this arc)

While diagnosing Defect #2, discovered my initial soak v5 extraction reported `auto_action_proposal_sse: 3` — regex false positive. Real matches were:
- 1 `stale_lock_detected` warning on `auto_action_proposals.jsonl.lock`
- 2 DEBUG messages about an unrelated publisher's unknown event-type `auto_action_proposal_emitted`

Actual count: ZERO Arc 1 hook firings, ZERO VERIFY phase completions, ZERO AdvisoryAction events. Honest empirical state corrected in `project_soak_v5_findings.md`. Arc 1 hook is gated on VERIFY phase; ZERO ops reached VERIFY in soak v5 — a deeper defect tracked separately.

## What this unlocks

Production Oracle observer now operationally alive in the harness. Next soak populates the bounded ring buffer + GET endpoint + provides non-None observations to `_read_recent_oracle_observation()`. When ops reach VERIFY phase (deeper defect), Rule 1.5 oracle veto will have real observations to read. Sentry/Datadog adapters benefit immediately when operators stage real tokens.

## Files touched

- `backend/core/ouroboros/battle_test/harness.py` (boot wire-up + shutdown cancellation + AST pin extension)
- `scripts/production_oracle_boot_defect2_verdict.py` (NEW)

Defects #3 (PersistentIntelligence readonly-DB) and #4 (CandidateGenerator EXHAUSTION + unhandled task exceptions) remain queued.
