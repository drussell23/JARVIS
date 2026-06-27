---
title: Project V46 Preflight Soak
modules: []
status: historical
source: project_v46_preflight_soak.md
---

v46 Pre-Flight Transport Probe (session `bt-2026-06-01-053522`, 2026-06-01, 300s wall / $0.50 / 120s idle, Claude-disabled + DW_SURFACE_HEALTH, `--headless`, sandbox-off). Run to gate a **DW-only SWE-bench-Pro** sweep.

**GATE VERDICT: HOLD — SWE-bench-Pro is a NO-GO.** Both DW lanes still fail `live_transport RuntimeError`. The blocker is now DEFINITIVELY proven to be DW upstream capacity, NOT our budget/plumbing.

**Slice 50 — BOTH phases PROVEN in production (within hours of merge):**
- **Phase 2 (force-batch deadline floor) WORKS + delivers the definitive diagnosis.** Floor fired `gen_timeout 220→330s` for standard ops; op-019e81b1-c95b got `remaining=330 primary_budget=300 fallback_reserve=30` and ran `_call_primary` for a FULL **hold=324.3s** before TimeoutError (in v45 the same op-class was severed at 220s). So with the batch's full ~330s lease, **DW still returned NOTHING in 324s** → batch lane genuinely not delivering; the fix is correct, the remaining wall is 100% upstream. All 3 models (Qwen-35B/397B/Kimi) cascade `failed:live_transport:RuntimeError`.
- **Phase 1 (bounded observer teardown) WORKS.** `[FileWatchGuard] observer teardown exceeded 10.0s deadline — abandoning daemon thread (... total_abandoned=1). Exit path cleared.` The v45 ~57s `emitter.join()` wedge is GONE — bounded at 10s exactly as designed, leak flagged.

**Other validations:** Slice 46 zero-skew wall cap (monotonic=wall=effective=300); Slice 49 scanner pruning clean (miner 2563 files, no storm); ExternalWatchdog armed (budget=390s). Boot was FAST (~2 min vs v45's 14 min — Oracle cache warm, DW discovery quick).

**Streaming flicker (do not over-read):** the Slice 40 fresh surface sweep probed `stream=healthy` ONCE at 22:36:53 (vs preflight `done_before_content` 6s earlier) — a transient blip, NOT recovery. Every actual generation still threw `live_transport RuntimeError`. Also: standard/complex ops force-batch regardless of streaming health (Slice 36 static opt-in ON under Claude-disabled), so a recovered streaming wire wouldn't even be exercised for generation without flipping the opt-in.

**NEW co-binding concern — severe loop starvation (§48).** ControlPlaneWatchdog fired 21 events, lag 0.6–1.6s sustained with a **20.0s peak** (lag_ms=19987) under Oracle-background-index + 3 concurrent batch polls + sensors on the 16GB M1. Not a LoopDeadman kill (monotonic heartbeat held), but it delayed ops reaching GENERATE and is now arguably a second blocker independent of DW. Ties to [[project_stage_1_6_park_spike]].

**Teardown note:** ShutdownWatchdog still FIRED os._exit(75) at elapsed=51s (>30s deadline) — Slice 50 P1 bounded FileWatchGuard (10s) but Oracle._save_cache (abandoned 5s) + the in-flight 324s batch-op unwind pushed total teardown to ~56s. Process DID exit via in-process os._exit(75) this time (~56s) — external watchdog not needed (better than v45). Candidate follow-up: the 324s in-flight op cancellation isn't bounded on shutdown.

**Path to SWE-bench-Pro:** gated entirely on DW serving generations — either (a) `live_transport` RuntimeError clears on the batch lane (a batch actually returns a candidate), or (b) streaming genuinely recovers AND the force-batch static opt-in is flipped so generation uses the RT wire. Neither holds. Infra is ready (harness wired, Slice 49/50 stable); provider is not. Re-probe before any future greenlight; do not spend the big run on an unproven corridor. See [[project_slice_50_teardown_batch_floor]] [[project_v45_preflight_soak]]
