---
title: Env block
modules: [scripts/ouroboros_battle_test.py]
status: historical
source: project_v3_5_treefinement_first_validation_soak.md
---

May 12 2026 — first operator-paced Phase 9 validation soak under the v3.4 production wiring shipped 2026-05-12 (Phases A-F). Goal: verify end-to-end that the strategy gate + lazy boot wiring + production factory don't crash anything under real soak load.

## Soak invocation

```bash
# Env block
export JARVIS_L2_TREEFINEMENT_ENABLED=true
export JARVIS_L2_BRANCHING_STRATEGY=bfs
export JARVIS_L2_TREE_ARCHIVE_ENABLED=true
export JARVIS_L2_TREE_PERSISTENCE_ENABLED=true
export JARVIS_GRADUATION_LEDGER_ENABLED=true
export JARVIS_IDE_OBSERVABILITY_ENABLED=true
export JARVIS_IDE_STREAM_ENABLED=true
export OUROBOROS_BATTLE_HEADLESS=true
export OUROBOROS_BATTLE_SEED_INTENTS=3
# .env loaded for DOUBLEWORD_API_KEY + ANTHROPIC_API_KEY

# Invocation
python3 scripts/ouroboros_battle_test.py \
    --cost-cap 0.30 \
    --idle-timeout 180 \
    --max-wall-seconds 480 \
    --headless -v
```

## Session metadata

- **Session ID**: `bt-2026-05-12-171129`
- **Duration**: 828.4s (~13.8 min wall-clock; 480s cap + 30s grace + ~318s atexit-write sequence)
- **Stop reason**: `wall_clock_cap+atexit_fallback`
- **Session outcome**: `complete` (per v2.96 Layer 8 discipline — `wall_clock_cap` is harness-intended termination, NOT external interruption)
- **Cost**: $0.00 (no API calls reached billing path)
- **Op stats**: 12 background ops submitted, 6 completed (6 still queued when wall-cap fired)
- **Branch stats**: 0 commits, 0 files_changed (no Yellow/Green tier work)

## What the soak VALIDATED (wiring safety — end-to-end)

1. **Boot path with Treefinement env block is safe** — no crashes, no tracebacks, no production-wiring-attributed errors. The pre-existing FlagRegistry.register(name=...) DEBUG warnings (autonomy_command_bus_bridge / component_tool_scope) are unrelated to our work — they exist in the codebase already.

2. **Six-layer stack boots cleanly under tree-mode config**:
   - `GovernedLoopService` — Started: state=ACTIVE
   - `IntakeLayer` — all 16 sensors active
   - `DreamEngine` — booted with DW + Claude both active (`dw=active, claude=active, jprime=none`)
   - `StrategicDirection` — momentum digest computed (100 commits)
   - `CommProtocol` — HEARTBEAT events firing every ~5s
   - `Harness` — StatusLineBuilder registered

3. **`L2 RepairEngine wired: max_iterations=5, timebox=120.0s`** logged at boot — L2 path is wired and ready for tree-mode dispatch (just never had an op reach it).

4. **`DW Discovery: 23 models, routes_assigned=['complex','speculative','standard']`** — provider topology healthy.

5. **`SemanticIndex` picked up `feat(governance): implement Treefinement Phase 5...` as a cluster nearest-source** — the substrate's own commit history is being indexed correctly (composition signal — git history walker working).

6. **WallClockWatchdog fires correctly** (v2.92 Layer 7 dual-clock authority):
   - Armed at 10:16:28 with `cap=480s grace=30s sigterm=60s exit=60s`
   - Fired at 10:25:19 with `monotonic=309s wall=531s effective=531s >= 480s` (wall-clock authority preferred, consistent with v2.92 sleep/suspend immunity)
   - Graceful shutdown sequence: WallClockWatchdog → atexit fallback writes partial `summary.json` → ShutdownWatchdog ARMED → 30s deadline → `os._exit(75)` (v2.88 Layer 6 escape hatch fires correctly)

7. **`session_outcome=complete`** stamped per v2.96 Layer 8 discipline — `wall_clock_cap` is a clean harness-intended termination, NOT external SIGTERM/SIGINT/SIGHUP. Soak classifier correctly distinguishes intent.

8. **6 background ops completed normally** during steady-state — pipeline executes ops under tree-mode config without perturbation. Triggers observed: `backlog` / `runtime_health` / `cadence_synthetic` / `doc_staleness`.

## What the soak DID NOT EXERCISE (honest functionality gap)

The strategy gate at `RepairEngine.run()` engages ONLY when an op reaches L2 repair. L2 fires ONLY when VALIDATE exhausts retries on a candidate. None of the 12 background ops dispatched during the soak generated candidates that failed VALIDATE — they all passed validation on first try (or weren't write-class ops in the first place).

Concrete consequences:

- **`_maybe_run_treefinement` was never called** — grep on `debug.log` returns zero matches for `maybe_run_treefinement` / `tree mode requested` / `treefinement gate`
- **`register_production_factory_at_boot()` never fired** — grep returns zero matches for `production factory registered lazily`
- **`production_tree_runner_factory` was never invoked** — no factory call site activity
- **Zero branches created** — `WorktreeManager.create(...)` for `ouroboros/repair-tree/...` branches was never called
- **`.jarvis/ouroboros/repair_tree.jsonl` does not exist** — no tree results to persist
- **No SSE `repair_branch_*` events fired** — broker received zero treefinement-scope events

**This is the byte-identical rollback contract working AS DESIGNED.** The gate stays inert until L2 fires. Master-flag-TRUE + strategy=BFS is structurally REACHABLE; functionally DORMANT until L2 path engages.

## Root cause of the exercise gap

`OUROBOROS_BATTLE_SEED_INTENTS=3` injects 3 synthetic intents to defeat the "headless zero-ops blocker" (Phase 9 Slice 3). But these synthetic intents are simple probes that pass validation easily — they don't trigger validation failures. Similarly, the background sensors (`doc_staleness` / `runtime_health` / `opportunity_miner` / `cadence_synthetic` / `backlog`) emit ops that either complete without write-class work or pass validation on first try.

**There is no current mechanism in the battle test harness to deliberately inject VALIDATE failures.** That's the engineering gap if we want soak-driven Treefinement validation.

## Three paths forward (operator decision)

**Option A — Wait for real-world L2 triggers**
- Pros: zero engineering cost; validates under real load
- Cons: operator-paced (arrives naturally over weeks/months); no controlled measurement
- Recommendation: passive baseline; let it happen

**Option B — Engineer `JARVIS_L2_FORCE_REPAIR_FAILURE_RATE` synthetic-failure-injection knob**
- Pros: controlled tree-mode exercise; A/B-able vs LINEAR baseline
- Cons: ~1 session of engineering; instruments test infrastructure with a deliberate-failure path (audit surface)
- Recommendation: if Option C blocks for some reason

**Option C — SWE-Bench-Pro evaluation arc** (recommended)
- Each SWE-Bench-Pro problem ships with failing tests by design — every problem naturally triggers L2 repair → tree mode → full production wiring exercised
- SWE-Bench-Pro is BOTH external benchmark AND natural tree-mode exercise harness
- Pros: realistic load + external benchmark provenance (PRD §40.7.5) + per-problem isolation (composes existing WorktreeManager); single arc serves both Treefinement validation AND §40.7-citation closure (§40.7.5 — SWE-Bench-Pro is named in the canonical citation list)
- Cons: ~2-3 sessions for harness binding + real compute cost (1,865 problems × ~5-10 min each = 150-300 hours; parallel fan-out helps)
- Recommendation: **this is the next arc**

## Cumulative status (post-v3.5)

| Aspect | Status |
|---|---|
| Treefinement substrate (v3.3) | ✅ Shipped — 243 tests / 12 AST pins / 19 flags / 4 SSE events / 3 IDE GET routes |
| Production wiring (v3.4) | ✅ Shipped — 1700 LOC / 107 tests / 8 AST pins / 2 flags |
| Strategy gate safety under load | ✅ Validated by v3.5 soak |
| Strategy gate functionality (tree mode actually running) | ⏳ Awaits L2-triggering ops (Option A) OR exercise harness (Option B) OR SWE-Bench-Pro (Option C) |
| Phase 9 graduation criterion (≥10% lift OR ≥20% wall-clock reduction) | ⏳ Requires Option B or C for controlled A/B measurement |

## Soak artifact paths

- Session dir: `.ouroboros/sessions/bt-2026-05-12-171129/`
- Debug log: `.ouroboros/sessions/bt-2026-05-12-171129/debug.log` (161 KB, 689+ lines)
- Summary: `.ouroboros/sessions/bt-2026-05-12-171129/summary.json` (partial — atexit fallback shape)
- Graduation ledger: `.jarvis/graduation_ledger.jsonl` (no new row — battle_test direct invocation, not via graduation runner)

## Lessons for future Treefinement soaks

1. **Tree mode requires L2-triggering ops** — synthetic seed intents don't fire L2; background sensor ops mostly don't fire L2 either
2. **The byte-identical rollback works** — Treefinement env block has zero impact on non-L2 paths (confirmed by clean 828s run)
3. **`session_outcome=complete` for `wall_clock_cap`** is the canonical clean termination — v2.96 Layer 8 + v2.92 Layer 7 discipline holds end-to-end under tree-mode config
4. **Atexit fallback's "0 attempted" stats** are a known artifact — runtime op counts live in debug.log, not the partial summary.json (the watchdog os._exit bypass means normal counters aren't flushed)
5. **For Phase 9 graduation evidence, A/B measurement requires controlled tree-vs-LINEAR comparison** — neither this soak nor any "natural traffic" soak provides that; SWE-Bench-Pro is the natural fit (per-problem isolation enables per-problem A/B)

## Recommendation

Move to **SWE-Bench-Pro arc (Option C)** as the next engineering work. The arc serves three goals simultaneously:

1. **Exercise the v3.4 production wiring under realistic load** — each problem naturally fires L2 → tree mode → full pipeline
2. **External benchmark provenance** — per PRD §40.7.5; comparison vs Claude Sonnet 4.5's 43.6% resolve rate baseline
3. **Closure of a second §40.7-citation** — Treefinement (§40.7.2 AlphaVerus) + SWE-Bench-Pro (§40.7.5) makes 2 of 6 citations now operationally realized

The arc plan from v3.4's §40.7.7-op already sketches 6 phases (~2-3 sessions). Ready for operator approval.
