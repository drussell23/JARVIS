---
title: Project Slice 51 Disambiguation
modules: [scripts/ouroboros_battle_test.py]
status: historical
source: project_slice_51_disambiguation.md
---

Slice 51 (rescoped) investigation 2026-06-01. Authorized as "Resource Governance + Disambiguation." **Outcome: one decisive diagnostic win + TWO runbook premises falsified by verify-first → NO code shipped (correctly).** Branch opened then deleted; main pristine.

**PHASE 1 — DW client-vs-server bisection: DEFINITIVE = SERVER-SIDE.** Wrote a vanilla-aiohttp out-of-band probe (NO HeavyProber/Aegis/provider/pooled-sockets — total framework bypass), key loaded from project-root `.env` (the app's own source; battle_test force-overrides DOUBLEWORD_API_KEY from .env, scripts/ouroboros_battle_test.py:175,186). Direct POST to `https://api.doubleword.ai/v1/chat/completions` stream=True:
- Qwen3.5-35B: **HTTP 200**, content-type text/event-stream, **18 SSE chunks, content_chars=0, finish_reason=length**, total 1249ms.
- Qwen3.5-397B: HTTP 200, 8 chunks, content_chars=0, finish=length.
**Verdict: 100% upstream.** DW accepts the connection and streams valid SSE framing but delivers ZERO content tokens, closing with `finish_reason=length`. NOT our client, NOT Aegis, NOT headers, NOT a transport outage. `done_before_content` is literally accurate (stream "done" before content). DW's API is UP; its **model serving returns empty completions** — a DoubleWord-side defect. Report to DW with this vanilla repro. Confirms Slice 39's `classify_surface_failure: UPSTREAM`. **This is the definitive answer to "is it our client or DW": DW's server.** SWE-bench-Pro stays blocked until DW serves non-empty completions; nothing on our side fixes it.

**PHASE 2 — Resource Governor: NOT BUILT (premise falsified twice).** Runbook said the 20s loop starvation = CPU core oversubscription; build a core-reservation governor across oracle/miner/advisor/posture pools. Verify-first killed it:
- AstCompileHelper process pool (shared chokepoint for BOTH oracle._index_file AND opportunity_miner_sensor.scan_once) is already `_DEFAULT_POOL_MAX_WORKERS=1` → serialized, NOT oversubscribing.
- advisor-blast ThreadPoolExecutor = max_workers=2 ("CPU-light").
- ProcessMemoryWatchdog rss=**636MB** right before the 20s spike (warn=10445/cap=12288) → NO memory pressure / swap. (2.5M-node Oracle graph is not resident in this process.)
- SidecarProfiler stuck-frame at the spike showed MainThread running its OWN ControlPlaneWatchdog snapshot callback via normal `_run_once` — loop was TICKING, not frozen in a sync call.
- LoopSink ledger dominated by `posture_observer.run_one_cycle` (up to 10.3s) + `posture.signal.commit_ratios` (9.8s) — PostureObserver computing momentum over 100 commits of git history every cycle (logged `kind=async`, awaited via run_in_executor, so loop nominally free during them).
**Conclusion: the 20s lag is NOT core oversubscription (pools are 1+2, mem 636MB) — a core-reservation governor would be a THIRD misdiagnosis.** The dominant recurring cost is the PostureObserver git-history cycle + GIL contention from many light threads/git-subprocesses during op bursts. It's non-fatal (self-recovered, never tripped LoopDeadman). If it ever needs fixing: lighten/throttle/fully-offload the posture cycle (cadence + git-log depth), NOT a CPU governor. Deferred — fleet runs are blocked by DW regardless, so starvation isn't on the critical path.

**Discipline note:** this is the 3rd–5th consecutive runbook premise corrected by grounding (cf. Slices 42/45/47/50). Pattern holds: verify the named target before refactoring. The pools being already-minimal + 636MB RSS were the decisive falsifiers. See [[project_v46_preflight_soak]] [[project_slice_50_teardown_batch_floor]]
