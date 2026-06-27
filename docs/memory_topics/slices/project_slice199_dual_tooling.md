---
title: Project Slice199 Dual Tooling
modules: [backend/core/ouroboros/governance/m10_autonomous_graduation.py, backend/core/ouroboros/governance/auto_committer.py, tests/governance/test_auto_committer_ignore_guard_graduation.py]
status: historical
source: project_slice199_dual_tooling.md
---

**Slice 199 — Sovereign Tooling & Dual-Identity Matrix, HTTPS-token variant (MERGED #69438, main `a27b8af38e`, 2026-06-10).**

**Why:** [[project-slice198-sovereign-ignition]] proved the gitless/gh-less container can't ship (orange-PR fail-closed). 198 LIVE VERDICT confirmed the design: `proposer:True | cadence:True | taste:True | orange:False` (orange correctly dark in gitless container).

**SECURITY DECISION (load-bearing, operator-plan-divergence):** The user's plan asked to bind-mount host `~/.ssh` + `~/.config/gh` + `~/.gitconfig` into the container. **REFUSED that mechanism, delivered the goal a safer way.** Mounting the operator's private SSH keys into an autonomous self-modifying LLM agent (bash+network) works against the Semantic Firewall. Instead: container ships PRs over HTTPS using `GH_TOKEN` already in its env (.env), bootstraps its OWN isolated git repo bound to origin — MORE isolated than sharing host identity. Grep-pinned: compose does NOT mount `.ssh`/`.config/gh`. If SSH ever needed → dedicated push-scoped DEPLOY KEY, never personal `~/.ssh`.

**How to apply:** `docker/Dockerfile.soak` + openssh-client+gnupg+official gh CLI (apt-repo, clean layers; git already present). `docker/soak_git_entrypoint.sh` (NEW): hardening (GIT_TERMINAL_PROMPT=0/GIT_ASKPASS/GCM_INTERACTIVE=never) + O+V git identity + `gh auth setup-git` + isolated git-repo bootstrap to origin/main over HTTPS (history-only via update-ref+symbolic-ref+reset --mixed, working tree untouched) + FAIL-SOFT (never blocks boot) + `exec python3 "$@"`. compose entrypoint `["python3"]`→`["bash","docker/soak_git_entrypoint.sh"]` + hardening/identity env. `m10_autonomous_graduation.py`: `hardened_git_env(base=None)`, `gh_auth_status_ok(_probe=None)` (non-interactive `gh auth status` timeout 15), `orange_pr_armed` now = unlocked AND assertion AND gh_auth. `auto_committer.py` push subprocess `env=hardened_git_env()`. Origin slug = `drussell23/JARVIS` (NOT JARVIS-AI-Agent — that's the CLAUDE.md display name; actual remote is /JARVIS). 18 tests; 264 green touched suites. KNOWN pre-existing main failures: `test_auto_committer_ignore_guard_graduation.py` 5/16 fail on BOTH clean main + worktree (sovereignty-marker test-env, NOT this slice). See [[project-slice193-observability-registry]].
