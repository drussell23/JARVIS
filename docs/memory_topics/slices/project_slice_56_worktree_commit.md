---
title: Project Slice 56 Worktree Commit
modules: []
status: historical
source: project_slice_56_worktree_commit.md
---

Slice 56 MERGED 2026-06-01 (PR #65642, squash 15513f027b). **Closes the last blocker + a sovereignty leak. Validation soak landed the FIRST fully autonomous, zero-leak commit.** main synced.

**Confirmed divergence (v47, verify-first via code-read not debug-print):** ChangeEngine APPLY wrote to MAIN repo (`self._project_root`); AutoCommitter checks/commits in the owned worktree (`JARVIS_AUTO_COMMIT_WORKSPACE`, stamped by harness_sovereignty_pin so commits never touch operator's main). → patch in main, committer sees clean worktree → "No changes detected" → no commit (the v47 8s-gap was THIS, NOT an FS-flush race → runbook flush hypothesis declined). → autonomous patches were LEAKING into operator's real main tree (manual revert each soak).

**Fix (Option A, operator-authorized):** redirect ChangeEngine writes into the owned worktree. New `ChangeEngine._effective_write_root()` (env `JARVIS_AUTO_COMMIT_WORKSPACE` else project_root — MIRRORS `AutoCommitter._effective_repo_root` → same tree by construction) + `_redirect_target(target)` (rebase absolute via relative_to(project_root); relative joined; outside-root unchanged; never raises; no-op when env unset = byte-identical). Wired at the single APPLY seam (execute, line ~572) so rollback snapshot + file lock + signature all follow the redirected target (Phase 2 coherence free). 7 tests incl coherence-with-AutoCommitter pin + execute wiring pin. 101 regression green; 3 pre-existing test_ledger_sovereignty_wiring "master_off" fails verified present with change stashed (test-isolation, unrelated).

**VALIDATION SOAK bt-2026-06-01-224826 = THE MILESTONE: first fully autonomous zero-leak commit.** `DW produced 1 candidate 65.4s $0.0024` → APPLY mode=single (into owned worktree) → `[AutoCommitter] Committed abbabc70 (1 files)` → `Auto-committed abbabc706f`. Committed @15:58:33 BEFORE wall cap (420s window vs v47's 300s that cut it). VERIFIED: (1) commit `abbabc706f` lives ONLY on `ouroboros/auto/bt-2026-06-01-224826` (NOT main); (2) operator main tree CLEAN — zero leak (no manual revert needed for first time ever); (3) proper conventional commit `test(...)` w/ signal/urgency/why metadata. Worktree+aegis reaped, branch preserved.

**THE FULL AUTONOMOUS LOOP NOW WORKS END-TO-END, ZERO LEAK:** DW GENERATE (reasoning unlock S54) → force-batch full lease (S50) → APPLY into owned worktree (S56) → AutoCommitter commit on isolated branch (S56) → operator main untouched. The v44→v53 "DW broken" saga was client-side (reasoning parsing S54 + commit-tree mismatch S56); DW itself works + ~700x cheaper than Claude.

**Path to SWE-bench-Pro now genuinely clear:** run the full $15/90min hybrid soak (Claude-enabled fallback) — DW carries cheap volume, Claude catches misses, commits land on isolated branches, main protected. Remaining nuance: some concurrent standard-route ops still EXHAUSTED in this soak (transport hiccup on those — not the committed bg op); worth watching at scale. See [[project_slice_55_dynamic_effort]] [[project_slice_54_reasoning_unlock]]
