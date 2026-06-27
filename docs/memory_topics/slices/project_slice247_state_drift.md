---
title: Project Slice247 State Drift
modules: [tests/governance/test_slice247_state_drift.py, backend/core/ouroboros/governance/state_drift.py, orchestrator.py, target_file.py]
status: merged
source: project_slice247_state_drift.md
---

**Slice 247 — State-Drift Reconciliation & Dynamic Context Alignment. MERGED PR #69491, main `ed3d0782`.** Closes the concurrency-collision vuln from [[project_slice246_preemption_primacy]]: a resurrected GOAL awakening to a human-drifted target file would blind-patch → AST/line corruption. DW-sovereignty arc.

**VERIFY-FIRST — most already existed:**
- Phase 1 (capture hashes): ALREADY exists — `generate_runner` snapshots sha256 of `ctx.target_files` into `ctx.generate_file_hashes` at GENERATE entry (~line 449); rides preserved context through preemption.
- Phase 2 (zero-LLM compare): ALREADY existed at APPLY (`orchestrator` ~8549) but was LOG-ONLY + blind-applied the stale candidate = the real latent bug.
- Phase 3 (autonomous re-align): GENUINELY NEW.

**Built — `state_drift.py` (pure, zero-LLM, NEVER raises):**
- `detect_drift(prior_hashes, project_root) -> List[str]` — drifted rel-paths; skips empty-hash (new file) + now-missing (deleted).
- `build_realignment_feedback(files) -> str` — ASCII `## STATE=CONTEXT_DRIFTED` instruction forcing `read_file` on each drifted file before patching.
- `state_drift_reconcile_enabled()` — gate `JARVIS_STATE_DRIFT_RECONCILE_ENABLED` default-TRUE.
- `STATE_CONTEXT_DRIFTED` telemetry constant.

**Wiring (reuse, no duplication):**
- `phase_runners/generate_runner.py` GENERATE-entry: for a resumed/resurrected op carrying PRIOR `generate_file_hashes`, compare vs disk BEFORE the re-snapshot erases the baseline; on drift inject realignment feedback via the EXISTING `strategic_memory_prompt` append channel (same pattern as the consciousness-memory injection at ~424-430).
- `orchestrator.py` APPLY-seam (~8549) refactored to REUSE `detect_drift` (single source of truth; was inline hashlib loop).
- **KEY INSIGHT:** preemption (246) fires in the tool loop = GENERATE phase, so resurrected ops RE-RUN GENERATE → the drift validator at GENERATE entry is the correct, SAFE seam (avoided risky APPLY→GENERATE_RETRY control-flow surgery in the giant `_run_pipeline`).

**Tests:** `tests/governance/test_slice247_state_drift.py` 13 incl. Phase 4 (snapshot baseline → mutate target_file.py → detect_drift catches → feedback names file + forces read_file → re-snapshot of new state shows aligned). 260 green across phase-runner/park/iron-gate/exploration/244-247; zero regression. 1 pre-existing stale source-pin (`test_determinism_generate_wiring` pins removed `generate(ctx, deadline)` string) proven via stash-compare.

**Slice 248 — APPLY-time verification pass (hardens 247). MERGED PR #69493, main `1e1bf058`.** The legacy APPLY drift check (orchestrator ~8549) was LOG-ONLY → blind-applied stale candidates (corruption: full-content overwrite=data loss, diff=line drift). 248 converts it to a deterministic zero-LLM VERIFICATION GATE that BLOCKS. `state_drift.py`: `state_drift_verify_enabled()` (env `JARVIS_STATE_DRIFT_VERIFY_ENABLED` default-TRUE; OFF=legacy log-and-apply) + `should_block_apply(prior_hashes, root) -> (block, drifted)` pure decision (block iff drift AND gate on; NEVER raises) + `STATE_DRIFT_UNRECONCILED='state_drift_unreconciled'` token. Orchestrator APPLY-seam on block → record FAILED + advance POSTMORTEM(terminal_reason_code) + publish_outcome + return (fail-safe, NO corruption) — REUSES the LiveWorkSensor abort pattern (orchestrator 8590-8606). Op re-runs fresh next sensor trigger → regenerates against current disk = eventual re-alignment. Routing APPLY→GENERATE_RETRY in-line is UNSAFE (8549 is past the GENERATE `for attempt` retry walk that the 5526 except wraps), so fail-safe is correct. Verification is CRYPTOGRAPHIC (hash, no model-trust). Defense-in-depth: 247 re-aligns at GENERATE, 248 blocks at APPLY if drift persists. 8 tests + updated 1 S247 pin (orchestrator now uses should_block_apply composing detect_drift). 208+13 green. NOTE: exploration ledger (`exploration_engine.ExplorationCall`) only stores `arguments_hash` not file paths → can't cleanly verify "model read_file THIS file"; hash-compare verification chosen instead (deterministic, no model-trust).

**COMPANION GIT HYGIENE — TWO PRs (the full fix needed BOTH):**
- PR #69490 (main `aa45d627`): **355 `.pyc` were TRACKED in HEAD** (committed historically; `.gitignore` is inert for already-tracked files). `git rm --cached` untracked all (files stay on disk) + added durable `forbid-committed-pyc` hook to TRACKED `.pre-commit-config.yaml` (`language: fail`, dependency-free) to block recurrence even via `git add -f`. **CAUTION — my "verified empirically" claim here was WRONG:** I grepped a filename while `git status --short` COLLAPSES untracked dirs, so I missed that 433 freshly-generated pyc still showed as `??`. Lesson: when verifying gitignore, count INDIVIDUAL files with `git status --short --untracked-files=all | grep -c '\.pyc$'`, not collapsed dir display.
- PR #69492 (main `6df5169c`): the REAL gitignore fix. `git check-ignore -v` showed the broad `!tests/core/**/*` (line 223) + `!tests/unit/core/**/*` (line 227) negations — there to TRACK the `.py` source the line-204 core-dump rule excludes — ALSO re-include `__pycache__/*.pyc` under those trees, and being LATER than the global `*.pyc` (line 5) they WIN (gitignore=last-match-wins). `backend/core` was hand-patched against this (.gitignore ~236-242) but the test/doc trees weren't. Fix: a single COMPILED-ARTIFACT INVARIANT appended as the LAST lines of `.gitignore` (`**/__pycache__/`, `**/*.pyc`, `**/*.pyo`, `**/.pytest_cache/`) → beats EVERY negation present+future; the `**/__pycache__/` dir-exclude blocks `**/*` re-entry. VERIFIED: 433→0 untracked pyc; `.py` source under tests/unit/core stays tracked. NOTE: `.jarvis/roadmap.draft.yaml` is tracked-despite-ignored (pre-existing M, leave alone).
