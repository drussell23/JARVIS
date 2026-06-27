---
title: Project Slice 55 Dynamic Effort
modules: []
status: historical
source: project_slice_55_dynamic_effort.md
---

Slice 55 Phases 1+2 MERGED 2026-06-01 (PR #65641, squash 63d57118d4). Phases 3+4 deliberately NOT done (P3 misdiagnosed, P4 gated). main synced. Builds on Slice 54 reasoning unlock [[project_slice_54_reasoning_unlock]].

**P1 — HeavyProber convergence (dw_heavy_probe.py):** boot/surface-health streaming probe built its own minimal request WITHOUT reasoning_effort → Qwen3.5 burned probe budget on CoT, emitted no content → FALSE done_before_content verdict → forced needless batch routing (seen live v47). Probe body now sends `reasoning_effort="none"` (+harmless enable_thinking) → straight to content. Always "none" (liveness check).

**P2 — complexity-derived reasoning_effort (doubleword_provider.py):** new `_COMPLEXITY_REASONING_EFFORT` map + `_reasoning_effort_for(complexity)` (trivial/simple→none, moderate→low, complex→medium, heavy_code/architectural→high). `_reasoning_request_params(complexity=)` wired at batch (ctx.task_complexity) + streaming (_complexity) GENERATE sites; prompt_only stays bare none. **KEPT JARVIS_DW_REASONING_EFFORT as explicit override/kill-switch — runbook said "eliminate" it, DECLINED (operator override + disable path are load-bearing safety).** Caught a real bug pre-merge: batch method param is `ctx` not `context` (pyright flagged my first edit's NameError). 6 tests + updated S54 pin, 102 regression green (1 pre-existing slice36 AST-pin fail verified on main).

**P3 — AutoCommitter "No changes detected": RUNBOOK MISDIAGNOSED → DEFERRED (not shipped).** Runbook assumed FS-sync/flush race. v47 falsifies: APPLY@15:15:45, AutoCommitter no-changes@15:15:53 = 8s gap, file ALREADY on disk (we reverted it post-session). An async flush fixes nothing. REAL cause: `_stage_files` runs `git status --porcelain` in `_effective_repo_root()` (=`JARVIS_AUTO_COMMIT_WORKSPACE` env else `self._repo_root`) — checked a DIFFERENT working tree than where ChangeEngine APPLY wrote (ties to L3 worktree isolation). Fix = align the trees; needs a focused debug slice, NOT a flush. **This is THE gating blocker for "first committed RESOLVED."**

**P4 — full $15/90min hybrid soak (Claude-enabled): DEFERRED/RECOMMENDED-AGAINST until P3 fixed.** Goal "first committed RESOLVED" is UNREACHABLE while commit checks the wrong tree — APPLY works (proven v47) but commit won't land. Running it would spend $15/90min and still not commit. Correct sequence: fix P3 (cheap, targeted) → short soak to confirm commit lands → THEN full hybrid sweep. Also note: the runbook's "SWE-bench-Pro sweep" command is actually the battle-test (autonomous self-dev on the JARVIS repo), not the literal swe_bench_pro harness.

**Status of DW corridor:** GENERATE works (Slice 54), health probe no longer false-flags (P1), effort scales with complexity (P2). Last remaining blocker to an autonomous committed RESOLVED = the AutoCommitter working-tree mismatch (P3). See [[project_slice_54_reasoning_unlock]]
