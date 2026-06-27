---
title: Project Swebench Task 21 22 Teardown Coherence
modules: [tests/governance/test_swebench_eval_timeout_coherence.py, backend/core/ouroboros/battle_test/harness.py, backend/core/ouroboros/governance/swe_bench_pro/evaluator.py, test_swe_bench_pro_wiring_validation.py]
status: historical
source: project_swebench_task_21_22_teardown_coherence.md
---

**Task #21+#22 (SWE-Bench-Pro evaluator timeout/teardown coherence) — ARC CLOSED, merged to `main` 2026-05-19.**

PR **#40617** squash-merged → mergeCommit `c563965bb5a8` (2026-05-19T04:22:59Z), base `main d08a48718a`. Local+remote `main` synced at `c563965bb5`.

**What landed:** `_apply_wall_coherence(configured) = min(configured, wall_remaining − drain_buffer)` floored at `_MIN_EVAL_FLOOR_S` (10s) so the inner SWE eval `wait_for` always raises TERMINAL_TIMEOUT *before* the outer bounded-shutdown — removing human config-error (inner ≥ outer wall ⇒ no verdict ever) from the threat model. **Task #22 refinement:** `_eval_drain_buffer_s` = explicit-override OR `shutdown_deadline + autoscore_grace + margin` (30+30+15=75s default), replacing the undersized 2×grace (60s) heuristic that caused the deep run's verdict to never flush despite #21's clamp proving (86400→5337.6). Harness publishes `OUROBOROS_BATTLE_WALL_DEADLINE_MONOTONIC` at WallClockWatchdog arm; wall_clock_cap arm extends `_arm_deadline` by grace+margin when `autoscore_work_in_flight()`. Env-var seam only — evaluator never imports `battle_test` (AST-pinned, ×2 post-#22). Spine: `tests/governance/test_swebench_eval_timeout_coherence.py` (17 tests incl. 4 #22 AST pins) + `register_flags` seeds 3 (EVAL_TIMEOUT + EVAL_DRAIN_BUFFER_S + EVAL_DRAIN_MARGIN_S).

**CI merge classification (same discipline as #39314):** authoritative gates GREEN — CodeQL `Analyze (python)` pass (30m20s), Run Tests & Quality Checks 3.10/3.11 ×4 pass, Validate PR Title pass. Waived infra reds (operator pre-authorized): Code Quality Analysis = whole-repo Black, **verified pre-existing** (`harness.py`+`evaluator.py` already Black-dirty on base `main d08a48718a`; authoritative `test.yml` runs no Black); Vercel = account paused. Infra-waiver comment posted to PR for audit.

**Commit-subject discrepancy (honest record):** branch had one commit (`cd24100e01`) with a stale reused subject (`feat(ouroboros): ProcessMemoryWatchdog + …`); GitHub squash on a single-commit PR keeps that subject, so `main` commit `c563965bb5` is mis-titled. Code/diff on main is correct (512 ins, full Task #22 A–E + spine). Truthful record = PR #40617 title (`feat(swe-bench): Task #22 verdict-flush teardown coherence`) + body + waiver comment. NOT amended — rewriting protected `main` history is off-limits; operator pre-stated this exact fallback.

**Status:** #21 completed. #22 completed+merged. **#19-RIG CLOSED** (rig/timeout-coherence proven). **#19-CAPABILITY still OPEN** — closes only when a real verdict flushes on merged `main` via the authorized element-web deep capability re-run (`bt-*-capability-v22`, EVAL_TIMEOUT_S=86400, --max-wall-seconds 5400, --cost-cap 5.00). PASS slice-1 = autoscore verdict line OR results.jsonl row w/ eval_outcome set + log shows drain_buffer ~75s (not 60s). terminal_timeout still PASS for slice-1. See [[project-no-preresult-euphoria]] — report empirically, no graduation/multi-problem until rubric floor met.

Out of scope (unchanged): `requirements.txt` drift; deleted Ouroboros-authored `test_swe_bench_pro_wiring_validation.py` soak debris.

---

**Deep capability re-run on merged main — session `bt-2026-05-19-052150` (capability-v22), 2026-05-19.** element-web only, headless, --max-wall-seconds 5400, --cost-cap 5.00, EVAL_TIMEOUT_S=86400.

**#21+#22 PROVEN IN PRODUCTION (decisive line):** `[SWEBenchPro] eval timeout clamped 86400.0s -> 5321.1s (wall_remaining=5396.1s drain_buffer=75.0s) — Dynamic Timeout Coherence`. drain_buffer=**75.0s** = shutdown 30 + grace 30 + margin 15 — NOT the old 60s 2×grace. `ShutdownWatchdog ARMED reason='wall_clock_cap' deadline_s=75.0`. Verdict artifact flushed: `.jarvis/swe_bench_pro/results.jsonl` 1 row, `evaluation.outcome="unresolved"` populated → **slice-1 #19-CAPABILITY PASS gate MET** (verdict artifact exists; NOT a #22 regression).

**On-main regression CLEAN:** oracle cache-HIT 12.4s (not 52GB loop); peak RSS 2477MB ≪ cap 12288MB; no process_memory_cap; stop_reason=wall_clock_cap+atexit_fallback; session_outcome=complete; cost_total=$0.4229 (≪$5).

**Capability = INCONCLUSIVE, NOT measured (honest read).** element-web `unresolved` root cause = **provider EXHAUSTION** ×2 during GENERATE/GENERATE_RETRY: both DoubleWord (Tier-0) and Claude (Tier-1 fallback) returned `TimeoutError`/`fallback_failure_mode=TIMEOUT` (early `[ClaudeProvider] APITimeoutError ConnectTimeout deadline exceeded`). Model never received a completed generation → no candidate patch (`diff_outcome=no_changes`, `captured_patch=null`) → scoring `skipped`. Op went STALE (656s no transition) → cooperative cancel at wall-cap. This run exercised the rig + #21/#22 verdict-flush, NOT O+V's element-web reasoning. Per [[feedback-no-preresult-euphoria]]: rig/teardown = PROVEN; capability = provider-outage-masked, unmeasured.

**Data-provenance caveats (Zero-Trust, flagged not glossed):** results.jsonl row `op_id=op-019e2fff` + `recorded_at_iso=2026-05-16` + `elapsed_s=852` diverge from the live solve op `op-019e3eb1` cancelled-after `1941.2s` in debug.log. eval_outcome IS populated (gate met) but per-record op_id/timestamp provenance is suspect — candidate Task #22-adjacent follow-up (record-only, NO auto re-spend). summary.json `attempted=0`/`operations=[]` = known counter bug; debug.log authoritative.

**Status:** #19-RIG CLOSED. **#19-CAPABILITY slice-1 PASS by operator's written gate** (verdict artifact exists) — but element-web capability itself UNMEASURED (provider outage). A true capability datapoint needs an operator-authorized re-run when the provider chain is healthy. NO auto re-spend. Master-flag graduation NOT met (rubric floor: ≥1 RESOLVED known-good + ≥1 UNRESOLVED known-hard — neither obtained).
