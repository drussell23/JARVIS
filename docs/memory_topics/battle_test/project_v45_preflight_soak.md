---
title: Project V45 Preflight Soak
modules: [scripts/ouroboros_battle_test.py]
status: historical
source: project_v45_preflight_soak.md
---

v45 Pre-Flight Transport Probe (session `bt-2026-06-01-034745`, 2026-05-31, 300s wall / $0.50 / 120s idle, `JARVIS_PROVIDER_CLAUDE_DISABLED=true` + `JARVIS_DW_SURFACE_HEALTH_ENABLED=true`, `--headless`).

**Entrypoint correction (recurring):** runbook's `./ouroboros_run.py` does NOT exist — real entrypoint is `python3 scripts/ouroboros_battle_test.py`. Same as Slice 47 finding. Aegis daemon needs a real socket bind → **must run sandbox-disabled** (first sandboxed attempt died at `sock.bind() PermissionError [Errno 1]`).

**VERDICT against operator's escalation boundary: HOLD FIRE — do NOT authorize the 40-min graduation soak.** DW streaming path STILL degraded (`done_before_content` → `source=live_transport, exc=RuntimeError`). 0 APPLY, 0 commits. Cost ~$0 (no completed GENERATE billed).

**Slice 49 invariants — all 4 exercised/confirmed:**
1. **External Sentinel ✅ ARMED + PROVEN as backstop**: `[ExternalWatchdog] armed: budget=390s stale=120s pid=5215 (out-of-process, GIL-immune backstop)`. heartbeat.tick lives at SESSION dir (`.ouroboros/sessions/<id>/heartbeat.tick`), NOT `.jarvis/`. Process reaped at its 390s budget boundary (~21:08:06), exit 75.
2. **Scanner pruning ✅**: FileWatchGuard `excluded_top_level=[...'.jarvis','.ouroboros','.worktrees'...]` + Slice 12I SWE pattern-descent preserved. OpportunityMiner cycle clean (scanned 2563 files, skipped 540, ingested 7, NO storm — the `_iter_python_files_pruned` os.walk path working). Oracle loaded from CACHE (2.53M nodes, 87.6s) so its FS-walk pruning wasn't stressed this run.
3. **Intake stratification ✅**: Advisor blast-radius gate correctly BLOCKED every heavy-core op (hypothesis_envelope_factory.py, python39_compat.py+unified_supervisor.py 102K, 0% coverage, blast 26-50). Slice 48 soft-penalty `background_tier_skip` engaged. SensorGovernor SHADOW deny working (doc_staleness cap exhausted).
4. **DW streaming ❌ still degraded**: Slice 39/40 surface sweep `batch=healthy stream=upstream_degraded auth=healthy`; preflight `active=2 active_batch_only=2` (both Qwen kept eligible via Slice 41 FORCE_BATCH).

**DEEPEST HEALTHY REACH of the arc — batch GENERATE actually dispatched for 3 SAFE_AUTO ops** (GitHub #65630 "Unlock Test Suite", torch 2.5.1→2.12.0 requirements.txt, todo_scanner test target). Each: streaming yields `tokens=0 first_token_ms=-1` (done_before_content) → Slice 36 selector → BATCH path → batches submitted async (`113253d3`, `942fe2be`, `927176eb`). **A DW RT tool loop (op=dw-rt-178028) DID execute exploration tools** — round 0 read_file, round 1 read_file+search_code — before wall cap cut it.

**BINDING BLOCKER: batch lane doesn't RETURN within budget.** `op-019e8158-e944` `_call_primary` TimeoutError at **220s** (batch submitted but result never retrieved). Claude disabled → `fallback=None` → EXHAUSTION `fallback_skipped:no_fallback_configured`; sentinel walker tries next Qwen → also `live_transport RuntimeError`. Same v44 class: streaming RuntimeError persists AND batch retrieval > 220s budget. Slice 43 set `JARVIS_DW_BATCH_TIMEOUT_S=300` but the primary hold here was 220s — possible budget/wait misalignment to revisit, but root cause is upstream DW not completing batches fast enough.

**Watchdog stack validated:**
- WallClockWatchdog fired CLEANLY at exactly 300s (`monotonic=300s wall=300s effective=300s`, ZERO skew → Slice 46 confirmed again, no host suspension). Contrast v44 where cap DIDN'T fire under FS starvation — here loop was healthy so in-process cap worked.
- LoopDeadman never fired os._exit (periodic `stack_dump` samples showed `exploration_sensor_jarvis` thread — benign diagnostic).

**NEW BLOCKER (next slice candidate): teardown wedge on FileWatchGuard PollingObserver.** Teardown hung ~57s in `watchdog/observers/api.py _clear_emitters() → emitter.join() → _wait_for_tstate_lock() → lock.acquire()`. Tied to **runaway-watching guard**: 151 candidate roots → 42 scheduled (cap 30, coalesced 7) from nested venvs; macOS PollingObserver fallback can't join its emitter threads on shutdown. In-process `ShutdownWatchdog` logged `os._exit(75)` at +57.5s (past 30s deadline) but process survived ~30s more under the wedge — **vindicates Slice 49**: external watchdog was the actual guarantee of death (budget boundary). Sibling to v43's exit-75 but DIFFERENT culprit (PollingObserver threads, not cross_repo_cleanup/Oracle._save_cache). Also: a `TestWatcher.run_pytest` subprocess (pid 5953) timed out at 30s → SIGTERM→SIGKILL escalation worked.

**Orphan note:** parent death left Aegis daemon (manually reaped) + caffeinate (self-expires); harness would reap on next boot anyway.

**Path forward:** still gated on DW transport recovery (operator's call — hold fire). Independent of that, two stack hardenings are now warranted: (1) bound/fix FileWatchGuard PollingObserver teardown join (the new wedge); (2) reconcile the 220s primary hold vs Slice 43's 300s batch timeout so a healthy batch isn't severed before it returns. See [[project_slice_49_external_sentinel]] [[project_slice_48_v44_soak]] [[project_slice_46_monotonic_deadman]]
