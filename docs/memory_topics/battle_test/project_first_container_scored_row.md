---
title: Project First Container Scored Row
modules: []
status: historical
source: project_first_container_scored_row.md
---

**🎉 FIRST LEGITIMATE CONTAINER-SCORED RESOLVED ROW — 2026-06-03, session bt-2026-06-03-063919.**

The full autonomous SWE-bench-Pro loop closed end-to-end for the first time. Claude solved the qutebrowser GUIProcess bug and the Docker container scorer confirmed it PASSES:
```
[ContainerEngine] scoring instance=instance_qutebrowser__…-v2ef375ac…b3c171 image=jefzda/sweap-images:qutebrowser.qutebrowser-…b3c platform=linux/amd64 tests=21
[HarnessInject] autoscore verdict: eval_outcome=resolved score_outcome=pass diagnostic=''
```
Op `op-019e8c3d` ($0.24, Claude): explored 3 rounds (no bails) → targeted the CORRECT file `qutebrowser/misc/guiprocess.py` → patched `_on_error` to the exact required format (`"{Process} '{cmd}' failed to start: {msg}"` + non-Windows `"(Hint: Make sure '{}' exists and is executable)"`) → APPLY → COMPLETE → captured → container ran 21 held-out tests → **RESOLVED/pass**.

**The corridor that made it work (all merged to remote main via PR #69167, branch deleted):**
- **Slice 69** (daec214575) — test-patch diff isolation in `capture_produced_patch` (strip test footprint).
- **Slice 71** (5bf21d0969) — fallback synthesis inherits the op wall envelope (`providers._synthesis_envelope_deadline`); continuation rounds no longer collapse to 1.0s.
- **Slice 72** (613f985b44) — target-existence guard (`target_existence_guard.py`, routes host-namespace paths to GENERATE_RETRY) + benchmark prompt insulation. THIS got the model onto guiprocess.py instead of the JARVIS `process_manager.py`.
- **Slice 73** (c8f400df83) — structural transport fast-cascade (sever DW lane on `LIVE_TRANSPORT`, `should_sever_dw_lane`) + adaptive turn gate (`is_next_round_viable` assesses the immediate turn, not fair-share/rounds_left). THIS enabled proper multi-round exploration (no `tool_loop_starved` truncation → tiny responses).
All flag-gated default-on; each was a verify-first re-scope of a runbook whose premise the code contradicted (chroot/allowlist/TTFT-predictor all already existed; the real bugs were elsewhere). DW was 100% `live_transport:RuntimeError` (transport down) the whole arc — Claude-only carried it.

**REMAINING WORK (the real "Slice 74", NOT a blocker — scoring WORKS):**
1. **Slow eval wake (~25 min):** op completed 23:53, container scored 00:18. The autoscore closed-loop (Slice 61) waited on the `operation_terminal` SSE; with the SSE not firing promptly it fell back to its long timeout→ledger path. Only scored because the wall cap was 40 min. Root fix = make the op-lifecycle SSE publish on COMPLETE so the eval wakes in seconds (leverage existing Slice 61 SSE/ledger wiring; no new machinery, no hardcoding).
2. **Durable persistence gap:** the verdict landed in the log + autoscore verdict line, but `.jarvis/swe_bench_pro/results.jsonl` rows still show None (the resolved row wasn't fully persisted — `JARVIS_SWE_BENCH_PRO_RESULT_PERSISTENCE_ENABLED` / record() schema). Fix so the durable ledger carries the real RESOLVED row.

**Infra notes:** runaway CI bot resolved — `.github/workflows/failed-ci-auto-pr.yml` ("Advanced CI/CD Auto-PR", `workflows: ["*"]` + no loop guard) created ~230 "Fix CI/CD" PRs (#69006-#69236); DISABLED + backlog cancelled. ~400 stale bot PRs (all `app/github-actions`) still need bulk-closing. Remote is `github.com/drussell23/JARVIS.git` (NOT JARVIS-AI-Agent per CLAUDE.md). `.claude/scheduled_tasks.lock` keeps getting committed (gitignore it). See [[project_slice_73_structural_cascade]] [[project_slice_72_target_guard]] [[project_slice_71_synthesis_envelope]] [[project_slice_69_manifest_isolation]].
