---
title: PersistentIntelligence Defect #3 — CLOSED 2026-05-03
modules: [backend/core/persistent_intelligence_manager.py, backend/core/ouroboros/governance/persistent_intelligence_health_oracle.py, backend/core/ouroboros/governance/production_oracle_observer.py, scripts/persistent_intelligence_defect3_verdict.py]
status: historical
source: project_persistent_intelligence_defect3_closure.md
---

# PersistentIntelligence Defect #3 — CLOSED 2026-05-03

3-slice arc fixing the third systemic defect from soak v5 findings: 12 PersistentIntelligence "attempt to write a readonly database" errors across the 62-min run (~1 every 5 min). Silent degradation — errors at ERROR level but no SSE event, no GET surface, no health flag. Exactly the pattern the brutal review called out as the load-bearing operational gap.

## Root cause (multi-layer)

1. **macOS file-protection blocks writes to `~/.jarvis/state/`** in the test env (`touch` fails "Operation not permitted"). The DB file existed (last written Jan 11) but new writes blocked.
2. **`create_checkpoint()` invokes `INSERT INTO checkpoints + commit()`** which triggers SQLite WAL/journal write attempts that fail.
3. **`_checkpoint_loop` retries every 300s with no backoff** — permanent log noise once DB goes readonly.
4. **No observability surface** — error logs fire but operators have no flag/event/endpoint to learn the substrate is degraded.

## Slices shipped

- **Slice A — Writable-path fallback chain in `_init_local_db`**. New `_resolve_writable_db_path` method probes write capability via `touch` test on parent directory (catches WAL/journal write failures even when the .db file appears writable). Falls through 4-tier sequence: `JARVIS_STATE_DB` env → `JARVIS_STATE_DIR` env → `<cwd>/.jarvis/state/...` → `tempfile.gettempdir()/jarvis_state/...`. Logs WARNING with chosen path on every fallback transition. Stores result on `self._effective_db_path`.
- **Slice B — Closed-5 health enum + checkpoint circuit breaker**. New `PersistentIntelligenceHealth` enum with exactly 5 values (HEALTHY / DEGRADED_READONLY / DEGRADED_DISK_FULL / DEGRADED_OTHER / DISABLED). `_init_local_db` flips DISABLED→HEALTHY on success. `_checkpoint_loop` rewrite: tracks consecutive failures; classifies error message into health enum; exponential backoff 1x→2x→4x interval; suspends checkpointing after `JARVIS_PERSISTENT_INTELLIGENCE_FAIL_THRESHOLD` (default 3) consecutive failures. Successful checkpoint resets failure count + restores HEALTHY. New properties: `health`, `effective_db_path`, `checkpoint_suspended`.
- **Slice C — `PersistentIntelligenceHealthOracle` adapter implementing `ProductionOracleProtocol`**. Reads manager singleton's `health` property; maps to `OracleSignal` via `_classify_health` pure function with closed-5 coverage + defensive unknown-value fallback. Mapping: HEALTHY→HEALTHY/0.1, DEGRADED_READONLY→DEGRADED/0.55, DEGRADED_DISK_FULL→FAILED/0.85, DEGRADED_OTHER→DEGRADED/0.5, DISABLED→DISABLED/0.0. Auto-registered in `production_oracle_observer.get_default_observer()` default bundle (now 5 adapters). When manager singleton is None (pre-boot), reports DISABLED gracefully — does NOT trigger side-effect creation.

## Architectural decisions worth remembering

- **Leverage existing observability surface, no parallel SSE/GET**. Instead of building parallel `EVENT_TYPE_PERSISTENT_INTELLIGENCE_HEALTH_CHANGED` SSE event + `GET /observability/persistent-intelligence-health` endpoint, the adapter plugs into the Production Oracle observer (Defect #2 just made it tick). One more adapter = automatic SSE via `production_oracle_signal_observed` + automatic GET via `/observability/production-oracle`. Zero new wiring; full operator visibility. This is the "leverage existing architecture, no duplication" directive structurally enforced.
- **Read-only adapter — never mutates manager**. The adapter has READ-ONLY access to manager properties. NEVER constructs the manager (would side-effect-create the singleton from a watcher's tick context). Pre-boot path returns DISABLED gracefully.
- **Pure function `_classify_health`** for closed-5 mapping. Testable in isolation. AST pin enforces all 5 health values appear as string literals in source — catches silent enum drift.
- **Circuit breaker downstream feeds the auto_action_router naturally**. When `health=DEGRADED_DISK_FULL` (FAILED + sev 0.85), the existing Rule 1.5 oracle veto in auto_action_router proposes `ROUTE_TO_NOTIFY_APPLY` (or `DEMOTE_RISK_TIER` for SAFE_AUTO ops). Operator gets advisory action AT THE NEXT VERIFY PHASE without writing any new wiring. The whole cascade is structurally cohesive.
- **Manager construction's `asyncio.Lock()` quirk**. Same Python 3.9 issue I caught in Defect #2 with `production_oracle_observer.py`. The verdict's C6 uses class-level introspection (`hasattr(PersistentIntelligenceManager, "health")`) instead of instantiating to avoid no-event-loop failure. Worth remembering: any module using `asyncio.Lock()` in `__init__` triggers this on Python 3.9 outside `asyncio.run()`.

## Empirical-closure verdict (6/6 PRIMARY PASS)

```
[PASS] C1 Writable-path fallback chain selects writable path
       configured=~/.jarvis/state/persistent_intelligence.db (sandbox-blocked)
       chosen=<cwd>/.jarvis/state/persistent_intelligence.db (project-local)
       fellthrough=True chosen_writable=True
[PASS] C2 Closed-5 PersistentIntelligenceHealth enum
       all 5 values present + correct order
[PASS] C3 _classify_health closed-5 + defensive on unknown
       cases_passed=5/5 + unknown→DEGRADED/0.5
[PASS] C4 PersistentIntelligenceHealthOracle implements Protocol
       isinstance=True
[PASS] C5 Default bundle = 5 adapters; new reports DISABLED pre-init
       adapter_count=5 names=[datadog, http_healthcheck,
       persistent_intelligence_health, sentry, stdlib_self_health]
       pre_init_verdict=disabled
[PASS] C6 AST pin + manager properties accessible
       invariant_violations=() class_props=all True
```

C1 fired the WARNING log BEFORE the verdict header (proving the fallback is producing real operator visibility on the verdict's own run). C5 confirms the bundle expansion landed cleanly + the new adapter handles pre-boot state gracefully.

## Reuse contract honored (no duplication)

- Existing `production_oracle_observer.get_default_observer()` factory extended with one adapter — no new factory
- Existing `OracleSignal` + `OracleVerdict` + `OracleKind` substrate reused (no parallel signal type)
- Existing `register_shipped_invariants` registration contract reused
- Existing `JARVIS_STATE_DB` / `JARVIS_STATE_DIR` env knobs reused for fallback chain inputs (no new override knobs)
- Existing `Posture`-style closed-5 enum pattern + AST pin coverage check pattern reused
- Existing `_checkpoint_loop` rewritten in-place (no new background-task substrate)
- One new env knob: `JARVIS_PERSISTENT_INTELLIGENCE_FAIL_THRESHOLD` (default 3) — minimal addition

## What this unlocks

Three operational improvements compound:

1. **Writable-path fallback eliminates the 12-error/hour log spam** from sandbox-blocked default paths. Operators in macOS-protected envs / sandboxed CI / restricted Docker setups get clean operation by default.
2. **Health state surfaces through existing observability**: next soak's GET `/observability/production-oracle` payload will include the persistent_intelligence health alongside the other 4 adapters. SSE `production_oracle_signal_observed` events carry the health verdict.
3. **Auto_action_router now sees substrate-degradation signals**: when persistent intelligence's checkpoint loop trips the circuit breaker, the FAILED/DEGRADED OracleSignal flows into Rule 1.5 oracle veto. At the next VERIFY phase, operator gets a proposed `ROUTE_TO_NOTIFY_APPLY` or `RAISE_EXPLORATION_FLOOR` AdvisoryAction — substrate degradation becomes ACTIONABLE not just OBSERVABLE.

The brutal-review pattern "silent degradation needs to become loud" is structurally addressed for THIS substrate. The same pattern (oracle adapter for a substrate's health state) can be applied to any other silently-degrading substrate.

## Files touched

- `backend/core/persistent_intelligence_manager.py`:
  - New `PersistentIntelligenceHealth` closed-5 enum
  - `_resolve_writable_db_path` method NEW (Slice A)
  - `_init_local_db` updated to use fallback chain + flip health
  - `_checkpoint_loop` rewritten with circuit breaker + classification (Slice B)
  - 3 new properties: `health`, `effective_db_path`, `checkpoint_suspended`
- `backend/core/ouroboros/governance/persistent_intelligence_health_oracle.py` (NEW — Slice C adapter)
- `backend/core/ouroboros/governance/production_oracle_observer.py` (default bundle += new adapter)
- `scripts/persistent_intelligence_defect3_verdict.py` (NEW — 6 contracts)

Closes Defect #3 from the soak v5 findings. Defect #4 (CandidateGenerator EXHAUSTION + unhandled task exceptions) remains queued.
