---
title: Project Repo Git Pr Ops
modules: []
status: historical
source: project_repo_git_pr_ops.md
---

**Operational constraints for doing manual git/feature work in JARVIS-AI-Agent** (learned shipping Slice 86, 2026-06-03; the repo runs an autonomous O+V loop that commits/PRs concurrently).

- **`main` is protected / PR-only.** A pre-push hook refuses direct pushes (`LOCAL PROTECTION: direct push to 'main' refused ... Use a feature branch + PR`); GitHub branch protection enforces it too. NEVER direct-push or force-push `main`. The autonomous loop integrates the same way (PRs #69251+). Land work via `git push origin <branch>` + `gh pr create` + squash-merge.
- **Use an isolated `git worktree` for manual feature work.** The autonomous loop concurrently switches branches and commits in the shared checkout (it switched the working tree out from under an in-progress branch mid-session, and scattered a doc commit onto one of its `ouroboros/battle-test/*` branches). A dedicated worktree (e.g. under `$TMPDIR`) keeps manual work isolated from the loop's churn. Committed work on its own branch is safe regardless; uncommitted/working-tree state is what's at risk.
- **Always `git fetch` + verify LIVE `origin/main` before branch/PR work** — the loop advances `origin/main` every few minutes when active (it burns slice numbers fast: 82→86 in one session). Rebase onto the freshly-fetched `origin/main`, not a cached ref.
- **`gh` may need `dangerouslyDisableSandbox: true`** for TLS/auth API calls (`gh pr create`/`merge` hit `x509: OSStatus -26276` under the sandbox's network interception). Plain `git push` over https usually works sandboxed; only the GitHub-API operations need sandbox-off.
- **Do NOT blindly `kill -STOP` the autonomous loop.** Process enumeration is sandbox-blocked (`ps`/`pgrep` denied: "Cannot get process list"), so the PID can't be confirmed, and freezing a process mid-git/network op risks corruption or a stuck lock. Only consider it if the PID AND its current git-operation state are proven safe. Prefer the no-freeze path: if `origin/main` is static, rebase + PR cleanly; the protected-main + PR flow is inherently race-safe (a non-fast-forward push is rejected harmlessly, never clobbers).
- **If the loop merges while manual work is in progress, just rebase cleanly and proceed through a PR** — our files and the loop's (DW routing, swe_bench) don't overlap, so rebases replay conflict-free. See [[project_swe_bench_closed_loop_gap]].
