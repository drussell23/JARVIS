---
title: Pass B Soak Preparation — CLOSED 2026-05-03
modules: [scripts/pass_b_soak_assertion.py, scripts/pass_b_soak_prep_closure_verdict.py]
status: historical
source: project_pass_b_soak_prep_closure.md
---

# Pass B Soak Preparation — CLOSED 2026-05-03

3-slice arc shipping the **infrastructure** for the W2(5) 3-clean-session graduation arc. This arc does NOT flip META_PHASE_RUNNER + REPLAY_EXECUTOR defaults — that requires running the operator-paced 3-soak procedure documented in the playbook. What this arc ships is everything operators need to run that procedure successfully.

## Slices shipped

- **Slice A** — `scripts/pass_b_soak_assertion.py`. Reads any battle-test session's `summary.json` + `debug.log` (+ the production `.jarvis/order2_review_queue.jsonl`) and asserts 5 clean-bar criteria. Designed to be run against any session — the operator runs it three times, once per soak. Each criterion has a clear failure shape + root cause + fix in the script's docstring + operator playbook.
- **Slice B** — `memory/project_pass_b_soak_playbook.md`. Operator-facing playbook documenting: pre-flight checklist, 3-step soak procedure (env config → harness invocation → assertion), interpretation guide for each failed criterion (root causes + fixes), and the post-3-CLEAN flip procedure. Reads like a runbook, not a closure memo.
- **Slice C** — `scripts/pass_b_soak_prep_closure_verdict.py`. Empirical-closure verdict that exercises the assertion script against synthetic clean + abnormal session fixtures + verifies the playbook structure. 4/4 PASS.

## Five clean-bar criteria (the load-bearing contract)

1. **CB1** — ZERO unhandled exceptions in `[MetaPhaseRunner]` / `[ReplayExecutor]` / `[Order2ReviewQueue]` log lines. Pass B substrate is supposed to fail-soft via typed status returns; exceptions in WARNING/ERROR for these modules indicate substrate bugs.
2. **CB2** — Every `execute_replay_under_operator_trigger()` invocation logged with `operator_authorized=True` nearby. Empirical complement to the structural cage — catches the case of a new caller invoking the executor without the cage firing.
3. **CB3** — Order-2 manifest amendments only via `/order2 amend` REPL (markers in `order2_review_queue.jsonl` carry `approved_via_repl=True`). Verifies the operator surface is THE only mutation path empirically.
4. **CB4** — Cost burn within env-tunable baseline (default $0.50/session, ceiling 3x = $1.50). Catches runaway-cost failure mode.
5. **CB5** — `session_outcome=complete` AND `stop_reason` head in the clean set (idle_timeout / wall_clock_cap / cost_cap / shutdown_event / operator_quit). Abnormal terminations (SIGKILL / SIGTERM / sighup / sigint) fail.

## Architectural decisions worth remembering

- **Soak prep is NOT graduation**. The user's directive demands "no shortcuts" — and the W2(5) policy explicitly requires operator-paced soak validation before flipping write-path autonomy flags. Auto-flipping META_PHASE_RUNNER + REPLAY_EXECUTOR in a single work session would VIOLATE that policy. Building the assertion infrastructure + playbook respects the policy AND eliminates the operator's manual work of judging clean-vs-not-clean per soak.
- **Sessions-dir env override** added during this arc — `JARVIS_OUROBOROS_SESSIONS_DIR` env (or CWD-relative `.ouroboros/sessions/` lookup). Caught by the verdict script: synthetic-fixture testing required the assertion script to read sessions from a tmpdir, not the real repo. Module-level static SESSIONS_DIR was a bug; runtime resolution is correct.
- **Synthetic-fixture verdict pattern**. Arc 3's verdict is the first that exercises a script via subprocess + synthetic fixtures rather than direct in-process function calls. Mirrors how integration tests work in CI — the assertion script's exit code is the contract, not its internal state. C2/C3 prove that contract holds for clean + abnormal session shapes.
- **Operator playbook lives in memory directory (not repo)**. The playbook is operator-facing instructions, not part of the codebase. Memory directory is the correct home for procedural knowledge that future sessions / operators consult.

## Test counts + AST pins

- **Empirical verdict 4/4 PRIMARY PASS** (verdict script is the regression spine):
  - C1 pass_b_soak_assertion.py present + 5 criteria visible (12,972 bytes, all CB1-CB5 markers found)
  - C2 Clean session correctly identified (exit 0 against synthetic clean fixture)
  - C3 NOT-CLEAN session correctly identified (exit 1 against synthetic abnormal fixture)
  - C4 Operator-facing soak playbook present (9,907 bytes, 9/9 expected markers found)
- **No new AST pins or FlagRegistry seeds in this arc** — the script ships standalone without modifying the substrate; the playbook is documentation.
- **1 new env knob added to assertion script**: `JARVIS_OUROBOROS_SESSIONS_DIR` (tooling-only; not a Pass B substrate flag)

## Empirical-closure verdict

```
[PASS] C1 pass_b_soak_assertion.py present + 5 criteria visible
       size_bytes=12972 criteria_found=5/5
[PASS] C2 Clean session correctly identified (exit 0)
       exit_code=0 stdout_tail='VERDICT: CLEAN ...'
[PASS] C3 NOT-CLEAN session correctly identified (exit 1)
       exit_code=1 stdout_contains_NOT_CLEAN=True
[PASS] C4 Operator-facing soak playbook present
       playbook_size=9907 markers_found=9/9
```

## Reuse contract honored (no duplication)

- Existing `summary.json` + `debug.log` artifacts (already produced by the harness on every reachable exit path) are the data source — no new persistence layer
- Existing `order2_review_queue.jsonl` (already produced by Slice 6.2 review queue) is the source for CB3 — no new audit log
- Existing baseline cost env knob pattern (`JARVIS_PASS_B_SOAK_COST_BASELINE_USD` mirrors `JARVIS_STDLIB_SELF_HEALTH_BASELINE_COST_USD`)
- Existing session-name regex pattern (`bt-YYYY-MM-DD-HHMMSS`) reused for filtering non-session directories
- Existing verdict-script structure (mirror of cluster_intelligence / mission_inferrer / multi_repo / pass_b_graduation / production_oracle / oracle_to_auto_action verdicts) — same ContractVerdict dataclass + main() shape

## What this unlocks

When the operator wants to graduate the two write-path Pass B flags:
1. They follow the playbook step-by-step
2. They run `python3 scripts/pass_b_soak_assertion.py` after each soak
3. They get a clear PASS/FAIL with named criteria + interpretation hints
4. After 3 consecutive PASS results, they flip the flags

The judgment of "is this soak clean enough?" is no longer a manual log-reading exercise — it's a deterministic 5-criterion assertion. This:
- Eliminates operator burden + judgment variance
- Makes the W2(5) policy procedurally executable
- Provides a concrete artifact (assertion exit code) for future audit ("did the operator actually pass 3 soaks before flipping?")

## Files touched

- `scripts/pass_b_soak_assertion.py` (NEW — 12.9 KB; 5 criteria + env-aware sessions-dir)
- `memory/project_pass_b_soak_playbook.md` (NEW — operator-facing runbook)
- `scripts/pass_b_soak_prep_closure_verdict.py` (NEW — synthetic-fixture verdict)

Closes Tier 3 #7 follow-up Arc 3. Pass B graduation infrastructure is now end-to-end ready; the actual flag flip remains operator-paced per W2(5) policy.
