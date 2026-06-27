---
title: Sovereign GitOps Identity Matrix (PR #69635 MERGED, main fff687091a, 2026-06-21)
modules: [backend/core/ouroboros/governance/graduation/graduation_workspace.py]
status: merged
source: project_gitops_identity_matrix.md
---

# Sovereign GitOps Identity Matrix (PR #69635 MERGED, main fff687091a, 2026-06-21)

**Why:** The [SOVEREIGN GRADUATION] PR #69632 was opened only after a MANUAL `git clone` + `git config` inside the soak container — because the prod image WORKDIR `/app` is a BAKED copy with no `.git` and the container had no git identity → proposer hit `rev-parse: not a git repository` / `unable to auto-detect email`. Operator: an organism that needs a human to hand it a pen isn't autonomous.

**Fix (2 layers, no hardcoding, composes existing GH_TOKEN + JARVIS_AUTO_COMMIT_WORKSPACE + OrangePRReviewer):**
1. **Autonomous git identity (env, no `git config`)**: docker-compose.crucible.yml sets `GIT_AUTHOR_NAME/EMAIL` + `GIT_COMMITTER_NAME/EMAIL` (inherit-from-host-else-default via `${VAR:-...}`) + `safe.directory=*` via git ENV-config (`GIT_CONFIG_COUNT=1/GIT_CONFIG_KEY_0/GIT_CONFIG_VALUE_0`). `OrangePRReviewer._run_git_sync` uses `subprocess.run` (cwd=repo_root, inherits os.environ) → identity flows, zero config-file mutation.
2. **Self-provisioning workspace (`graduation_workspace.py` NEW)**: `ensure_clean_workspace(repo_root)` idempotently makes repo_root a CLEAN, AUTHORIZED checkout of the integration branch — clone-if-missing (shallow, token-embedded `origin` from `JARVIS_GRADUATION_GIT_REMOTE`+`GH_TOKEN`) else `fetch + reset --hard origin/<branch> + clean -fd`. Wired into `propose_graduation_pr` (await asyncio.to_thread, after math-veto gate, before locate/flip) — **ONLY when `reviewer is None`** (production path; injected reviewer = caller owns repo, keeps existing tests green). Also fixes the STALE-FLIP footgun (a prior failed propose left the literal already flipped→"true"→literal_site_unresolved; reset wipes it). Master `JARVIS_GRADUATION_WORKSPACE_ENABLED` default-true; OFF=legacy; fail-soft → `ProposalResult(workspace_unready:<detail>)`, never raises.

compose adds: `JARVIS_AUTO_COMMIT_WORKSPACE=/graduation_workspace`, `JARVIS_GRADUATION_GIT_REMOTE=${...:-github.com/drussell23/JARVIS-AI-Agent.git}`. 32 tests (12 workspace + 20 proposer regression). GH_TOKEN already flows from Secret Manager (startup script) — no startup change needed.

**Net:** the cadence's `_propose()` opens the [SOVEREIGN GRADUATION] PR end-to-end with ZERO manual git steps. **OPS NOTE: squash-merge + local-branch-chain divergence caused a merge conflict on the first PR (#69634) — rebuilt clean on origin/main as #69635 (bring new files via `git checkout <oldbranch> -- <newfiles>` + re-run idempotent patch scripts on main's tracked files). See [[project_cognitive_graduation_crucible]], [[project_dw_reasoning_capability_profiler]].
