#!/usr/bin/env bash
# =============================================================================
# soak_git_entrypoint.sh — Slice 199: Sovereign Tooling & Dual-Identity Matrix
#
# Condition the isolated soak container for AUTONOMOUS, NON-INTERACTIVE PR
# shipping WITHOUT mounting any host SSH keys or gh identity. The container
# bootstraps its OWN git repo bound to origin over HTTPS, authenticated by the
# GH_TOKEN already present in the runtime env. This is MORE isolated than
# sharing host identity files — a scoped token + the container's own repo,
# never the operator's private keys.
#
# FAIL-SOFT throughout: any conditioning step that fails leaves the soak
# running (read-only, ship disabled) — it NEVER blocks boot. The final exec
# always runs the battle test with the args passed by compose.
# =============================================================================
set -uo pipefail

# --- Non-interactive hardening: never hang on a hidden credential prompt -----
export GIT_TERMINAL_PROMPT=0        # git never prompts on the terminal
export GIT_ASKPASS=/bin/true        # no interactive askpass helper
export GCM_INTERACTIVE=never        # git-credential-manager stays non-interactive

REPO_SLUG="${JARVIS_GIT_ORIGIN_SLUG:-drussell23/JARVIS}"
GIT_NAME="${JARVIS_GIT_AUTHOR_NAME:-Ouroboros+Venom}"
GIT_EMAIL="${JARVIS_GIT_AUTHOR_EMAIL:-ov-soak@users.noreply.github.com}"
TOKEN="${GH_TOKEN:-${GITHUB_TOKEN:-}}"

log() { printf '[soak-git] %s\n' "$*"; }

# --- Git identity (O+V signature) --------------------------------------------
git config --global user.name  "$GIT_NAME"  2>/dev/null || true
git config --global user.email "$GIT_EMAIL" 2>/dev/null || true
git config --global --add safe.directory /app 2>/dev/null || true

# --- gh credential helper over HTTPS (uses GH_TOKEN; no token in any URL) -----
if command -v gh >/dev/null 2>&1 && [ -n "$TOKEN" ]; then
    if gh auth setup-git >/dev/null 2>&1; then
        log "gh HTTPS credential helper wired (token-scoped)"
    else
        log "gh auth setup-git failed — ship disabled, soak continues"
    fi
else
    log "gh missing or no GH_TOKEN — ship disabled, soak continues"
fi

# --- Bootstrap /app as an isolated git repo bound to origin over HTTPS --------
# The image was built from this commit, so the working tree already matches
# origin/main; we attach history metadata only (no file download) and leave
# the working tree untouched so soak-applied edits surface for AutoCommitter
# and the orange-PR reviewer.
if [ ! -d /app/.git ]; then
    if git init -q /app 2>/dev/null \
       && git -C /app remote add origin "https://github.com/${REPO_SLUG}.git" 2>/dev/null \
       && git -C /app fetch -q --depth 50 origin main 2>/dev/null; then
        git -C /app update-ref refs/heads/main FETCH_HEAD 2>/dev/null || true
        git -C /app symbolic-ref HEAD refs/heads/main 2>/dev/null || true
        git -C /app reset -q --mixed 2>/dev/null || true
        log "isolated git repo bootstrapped → origin ${REPO_SLUG} (HTTPS)"
    else
        log "git bootstrap failed — ship disabled, soak continues read-only"
    fi
fi

# --- Hand off to the battle test (args supplied by compose's command:) -------
exec python3 "$@"
