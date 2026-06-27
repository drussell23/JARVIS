---
title: Project Slice 48 V44 Soak
modules: []
status: historical
source: project_slice_48_v44_soak.md
---

**Slice 48 (Semantic Target Stratification) MERGED to main 2026-05-30 (PR #65633, beabd152ec).** Three components, TDD, 184 tests green, 0 regressions:
1. **Soft-penalty stratification** (`target_stratification.py`): `file_has_test_coverage` (canonical `tests/test_{stem}.py` signal — OperationAdvisor now DELEGATES to it) + `stratification_penalty_multiplier` (`1-alpha*min(1,lines/max)*(1-cov)`, env JARVIS_STRATIFICATION_PENALTY_ALPHA=0.75 / MAX_LINES=2000, suppress=test-gen escape). OpportunityMiner `_FileAnalysis` gains `has_test_coverage`+`stratification_penalty`+`stratified_score`; penalty applied to BOTH exploit sort key AND explore weights. **NO filename denylist** (rejected runbook's hardcoded list per Manifesto §5).
2. **Bounded emergency teardown** (`cross_repo_cleanup._sync_emergency_cleanup`): daemon-thread + join(timeout=JARVIS_CROSS_REPO_EMERGENCY_CLEANUP_BUDGET_S=5s). Fixes v43 exit-75 (atexit registry open()/stat blew 30s ShutdownWatchdog budget). **Redirected off Oracle._save_cache** (runbook's target — already async+bounded, NOT the culprit; verified via v43 tombstone). atexit has no event loop so asyncio.to_thread N/A → daemon-thread bound.
3. **Stale-lock reaper** (`_reap_stale_jarvis_locks` in ouroboros_battle_test.py): purges `.jarvis/**/*.lock` >24h at boot (JARVIS_STALE_LOCK_REAP_AGE_S), skips intake_router.lock (PID-aware handler owns it).
Pre-existing unrelated test-drift (merkle_consult_enabled / _check_auto_ack_lane / resolve_envelope_repo_root AST pins) verified BYTE-IDENTICAL on clean main — NOT introduced.

**Also Slice 47 doc MERGED (#65632, 4c11607a87)**: Watchdog Isolation Invariant in CLAUDE.md (waiver rejected — see [[project_slice_47_v43_soak]]).

**v44 soak (bt-2026-05-31-002950, ~73min, KILLED manually) = 0 APPLY, THREE independent blockers, none fixable by tuning:**
- ✅ Slice 48 Phase 3 PROVEN in prod: `reaped 19 stale .jarvis lock file(s) (>24h old)` at boot. Aegis cleared.
- ❌ **(1) DW TRANSPORT DOWN**: both Qwen models failed `live_transport RuntimeError` → `Sentinel dispatch exhausted all 2 DW models` → `fallback_skipped:no_fallback_configured` (Claude disabled) → CandidateGenerator EXHAUSTION → GovernedLoop degraded L3 to REDUCED_AUTONOMY then READ_ONLY_PLANNING (3/5 consecutive probe failures). NO GENERATE possible. (Recurring upstream-transport theme, cf v33 era.)
- ❌ **(2) NEW WEDGE CLASS — FS-scan loop starvation**: python at 107% CPU; macOS `sample` dominated by `os.scandir`/`os.stat`/`ScandirIterator_iternext`. FileWatchGuard PollingObserver ("151 candidate roots → 42 scheduled; walks every scheduled root") + scanning traversed `.jarvis/swe_bench_pro/` (**437MB / 26,839 files**, 14,827 in worktrees) + `.worktrees/` (12,717 files). ControlPlaneStarvation lag warnings. **duration 4395s (73min) >> 2400s (40min) wall cap — wall_clock_cap did NOT fire (asyncio monitor + resource-zero hard-kill both starved). VINDICATES rejecting Slice 47 waiver: the cap not firing is a starvation BUG TO FIX, not something to make extendable.** Sibling to Slice 44 (which excluded .worktrees from Oracle but NOT from FileWatchGuard, and swe_bench_pro is uncovered everywhere). Required manual SIGKILL (GIL-starved watchdog).
- ❌ **(3) Stratification caveat REALIZED (as flagged pre-launch)**: Advisor still BLOCKED op-019e7bb1-a0d1 targeting unified_supervisor.py(102487L)+start_system.py(22882L). These `-cau`-suffixed ops come from a NON-OpportunityMiner sensor (causal/capability), so Slice 48's miner-only bias did NOT redirect them.

**NEXT SLICE (49 candidate)**: (a) exclude `.jarvis/swe_bench_pro` + `.worktrees` from FileWatchGuard watch roots AND Oracle scan (extend Slice 44 exclusions to the FS-watch consumer + swe_bench dir) — do NOT delete swe_bench_pro (437MB benchmark data, likely intentional); (b) extend stratification to the shared target path used by `-cau` sensors, OR apply the Advisor blast-radius signal pre-emission there; (c) investigate why wall_clock_cap didn't fire under FS-scan GIL contention (scandir/stat should release GIL — why was the watchdog thread starved 33min past cap?). See [[project_slice_47_v43_soak]] [[project_slice_44_worktree_reaper_oracle]]
