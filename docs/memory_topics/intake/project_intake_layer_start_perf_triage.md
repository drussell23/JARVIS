---
title: IntakeLayerService.start() performance triage memo
modules: []
status: historical
source: project_intake_layer_start_perf_triage.md
---

# IntakeLayerService.start() performance triage memo

**Filed**: 2026-05-12  
**Observed in**: stage-1 wiring-validation soak `bt-2026-05-13-025330`  
**Scope**: triage only — do NOT bundle into SWE-Bench-Pro arc work (operator binding 2026-05-12).

## Observation

The `IntakeLayerService.start()` call in `boot_intake()` blocked the harness boot path for **5 min 30 sec** (19:53:30 → 19:59:02). Specifically:

| Time | Event |
|---|---|
| 19:53:30 | Battle test launched |
| 19:53:32–42 | Boot phases 1–3 (governance / governed-loop-service / jarvis-tiers) |
| 19:53:42 | `GovernedLoopService booted` |
| 19:54:18 | `Trinity Consciousness failed to boot` (non-fatal WARNING) |
| 19:54:47 | `Accumulation branch created` (`create_branch` done) |
| 19:54:47 | First `[IntakeLayer] *Sensor added` lines — `start()` enters `_build_components` |
| ... | **3.5-min gap with no harness logs** |
| 19:55:08 | FileWatchGuard + FSEventBridge starting |
| 19:55:09–10 | Sensors actually starting + WAL replay + SemanticIndex cluster |
| 19:59:02 | `[IntakeLayer] Started: state=ACTIVE` + `EventChannelServer activated` |
| 19:59:02 | **`IntakeLayerService booted`** ← harness control finally returns |
| 19:59:02 | L2 exercise + SWE-Bench-Pro hooks fire (the actual boot hooks we wanted to test) |

So boot ran for **5min 32sec total**, with **~5min spent inside `IntakeLayerService.start()`** alone. The harness boot hooks (L2 + SWE-Bench-Pro) cannot fire until that returns.

## Why this matters

Short-iteration validation runs become impractical:
- An operator running a quick fixture-validation smoke (`--max-wall-seconds 720`) loses 80% of the wall budget to boot before any work happens
- The "stage 1 wiring validation" pattern I had to pivot away from in the SWE-Bench-Pro arc (`bt-2026-05-13-030901` died at boot+15s with hooks unreached) is partly caused by this — Bash tool `run_in_background` lifecycle interacts badly with a 5+ min cold start
- Real benchmark soaks (`--max-wall-seconds 2400` per CLAUDE.md) are less affected proportionally but still pay the 5-min tax

Operators have already worked around this with `--max-wall-seconds 2400` (2× safety margin) for graduation soaks. The wiring-validation pattern needs either:
1. A different lifecycle (e.g., a long-running `daemon` mode where boot happens once and many soaks reuse it)
2. A focused-validator pattern (what the SWE-Bench-Pro stage-1 ended up using — direct hook invocation against a stub intake, ~10ms)
3. A faster start path

## Suspected root causes (pre-diagnosis)

Investigation surface — none of these confirmed; just candidates:

1. **`SemanticIndex` k-means cluster on warm-cache**. Per CLAUDE.md: "Recency-weighted centroid over commits + goals + conversation (3d halflife conversation, 14d commits/goals)". Cluster computation across ~50 recent commits + auto-K silhouette could be the dominant cost — this fires once per boot.
2. **ChromaDB connection for Goal Memory Bridge**. First connection often pays init overhead (DB warm-up, schema check).
3. **`WebIntelligenceSensor` CVE DB sync** or **`CrossRepoDriftSensor` baseline file load**. CLAUDE.md says default polling intervals are 4–6 hours, so these should NOT probe at init — but maybe they DO, which would be the bug.
4. **`fastembed` / `bge-small-en-v1.5` model download** if not already cached. SemanticIndex uses local fastembed; first download is ~50MB. If cache is cold (e.g., fresh checkout, fresh container), this dominates.
5. **Cross-process flock acquisition** on `intake_router.lock` / `intake_wal.jsonl.lock` — the WAL replay shows multiple sources being replayed, each potentially blocking on flock acquisition.

The 3.5-min gap between `Sensor added` (19:54:47) and `FileWatchGuard scheduled` (19:55:08) is suspicious — but only 21 sec — most of the time is *between* `WAL replay` finishing (19:55:09) and `[IntakeLayer] Started: state=ACTIVE` (19:59:02). That 3:53 window is where the dominant cost lives. Hypothesis: cross-process lock waits or background-task-startup-and-wait inside the components built during `_build_components`.

## Mitigation candidates (NOT IMPLEMENTED in this PR)

1. **Lazy sensor init**. Defer expensive sensor construction to first poll/event rather than eager init at `_build_components` time. Sensors that don't fire in the first N minutes pay zero startup cost.
2. **Surface `IntakeLayerService.start_timing`**. Add a per-component timing log inside `_build_components` so the next person to triage this can immediately see which sensor / component is slow without needing a debugger.
3. **Make boot hooks fire BEFORE `start()` returns**. The boot hooks (L2 exercise + SWE-Bench-Pro) only need the IntakeLayerService to have an `ingest_envelope` method, not the full sensor stack to be active. Could expose an `await intake_service.wait_for_ingest_ready()` event that fires earlier in `_build_components`.
4. **Daemon-mode harness**. A persistent process that boots once and accepts SWE-Bench-Pro / cadence-synthetic injections from a CLI client — eliminates per-soak boot cost for iteration.

## Action

**File-and-defer.** This memo is the action; no code change in this PR. Performance work is orthogonal to SWE-Bench-Pro enumeration correctness (which is the active arc).

When this gets prioritized: start by adding the per-component timing log (option 2) — it's the cheapest diagnostic and unblocks every subsequent decision.
