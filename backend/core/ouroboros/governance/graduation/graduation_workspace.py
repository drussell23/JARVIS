"""Sovereign GitOps Identity Matrix — autonomous graduation workspace (2026-06-21).

The autonomous [SOVEREIGN GRADUATION] PR proposer needs a real, authorized,
branch-capable git checkout to flip a source-default literal and push a PR. The
production soak container's WORKDIR (``/app``) is a BAKED image copy with no
``.git`` — so the proposer previously failed (``pr_create_returned_none`` /
``rev-parse: not a git repository``) and required a human to ``git clone`` +
``git config`` inside the container by hand. That manual assist is exactly what a
Sovereign architecture must NOT need.

This module provisions the workspace autonomously, with zero manual steps:

  * ``ensure_clean_workspace(repo_root)`` — idempotently makes ``repo_root`` a CLEAN,
    AUTHORIZED checkout of the integration branch: clone-if-missing, else
    fetch + ``reset --hard origin/<branch>`` + ``clean -fd``. Each call hands the
    proposer a pristine tree (this also fixes the stale-flip footgun — a prior
    failed propose leaves the literal already flipped; the reset wipes it).
  * Authorization is token-embedded in the ``origin`` URL
    (``https://x-access-token:<GH_TOKEN>@<host>/<path>``) so ``git push`` is
    authorized without interactive credentials.
  * Git IDENTITY is supplied via the standard ``GIT_AUTHOR_*`` / ``GIT_COMMITTER_*``
    environment variables (set in the deploy overlay), which ``OrangePRReviewer``'s
    ``subprocess.run`` git calls inherit — NO ``git config`` mutation. ``safe.directory``
    is likewise injected via git's env-config (``GIT_CONFIG_COUNT`` ...), set in the
    overlay. This module only VERIFIES identity presence (advisory) and never writes
    global git config.

Design discipline:
  * **No hardcoding** — remote + branch + token come from env (deploy config), never
    baked model/repo literals in the algorithm.
  * **Fail-soft** — every public function returns a structured ``(ok, detail)`` and
    NEVER raises into the dispatch/graduation path.
  * **Gated** — master ``JARVIS_GRADUATION_WORKSPACE_ENABLED`` (default true); OFF →
    ``ensure_clean_workspace`` no-ops (legacy: proposer uses repo_root as-is).
  * **Reuse-first** — composes the existing ``GH_TOKEN`` (Secret Manager / metadata),
    the existing ``JARVIS_AUTO_COMMIT_WORKSPACE`` path the engine already passes as
    ``repo_root``, and ``OrangePRReviewer`` for the branch/commit/push/PR mechanics.
"""
from __future__ import annotations

import logging
import os
import subprocess
from typing import Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_ENV_ENABLED = "JARVIS_GRADUATION_WORKSPACE_ENABLED"
_ENV_REMOTE = "JARVIS_GRADUATION_GIT_REMOTE"
_ENV_BRANCH = "JARVIS_GRADUATION_GIT_BRANCH"
_ENV_TOKEN = "GH_TOKEN"
_ENV_CLONE_DEPTH = "JARVIS_GRADUATION_CLONE_DEPTH"

_GIT_TIMEOUT_S = 120.0


def workspace_enabled() -> bool:
    """Master gate. Default TRUE — only acts when the proposer needs a git
    workspace. =0 reverts to legacy (proposer uses repo_root as-is). NEVER raises."""
    return (os.environ.get(_ENV_ENABLED, "true") or "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _branch() -> str:
    return (os.environ.get(_ENV_BRANCH, "") or "main").strip() or "main"


def _clone_depth() -> int:
    raw = (os.environ.get(_ENV_CLONE_DEPTH, "") or "").strip()
    try:
        return max(1, int(raw)) if raw else 50
    except (TypeError, ValueError):
        return 50


def _mask(url: str) -> str:
    """Redact an embedded token from a remote URL for safe logging."""
    try:
        if "x-access-token:" in url:
            pre, _, post = url.partition("x-access-token:")
            _, _, tail = post.partition("@")
            return f"{pre}x-access-token:***@{tail}"
        return url
    except Exception:  # noqa: BLE001
        return "<remote>"


def authed_remote_url() -> str:
    """Build the token-authorized https remote from ``JARVIS_GRADUATION_GIT_REMOTE``
    + ``GH_TOKEN``. Accepts either a full ``https://host/owner/repo.git`` or a bare
    ``host/owner/repo.git``. Empty string if remote or token unset. NEVER raises."""
    try:
        remote = (os.environ.get(_ENV_REMOTE, "") or "").strip()
        token = (os.environ.get(_ENV_TOKEN, "") or "").strip()
        if not remote or not token:
            return ""
        # Normalize to host/path (strip any scheme + existing creds).
        if "://" in remote:
            parsed = urlparse(remote)
            host = parsed.hostname or ""
            path = parsed.path or ""
            hostpath = f"{host}{path}"
        else:
            # bare "github.com/owner/repo.git" (strip any leading creds@)
            hostpath = remote.split("@")[-1]
        hostpath = hostpath.lstrip("/")
        if not hostpath:
            return ""
        return f"https://x-access-token:{token}@{hostpath}"
    except Exception:  # noqa: BLE001
        return ""


def git_identity_ready() -> bool:
    """Advisory: True iff the env-based git identity is present so commits won't
    fail with 'unable to auto-detect email'. NEVER raises."""
    try:
        name = os.environ.get("GIT_AUTHOR_NAME") or os.environ.get("GIT_COMMITTER_NAME")
        email = os.environ.get("GIT_AUTHOR_EMAIL") or os.environ.get("GIT_COMMITTER_EMAIL")
        return bool((name or "").strip()) and bool((email or "").strip())
    except Exception:  # noqa: BLE001
        return False


def _run(args, cwd=None) -> Tuple[int, str]:
    """Run a git command, return (rc, combined_output). NEVER raises."""
    try:
        proc = subprocess.run(
            args, cwd=cwd, capture_output=True, text=True,
            timeout=_GIT_TIMEOUT_S, check=False,
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as err:
        return 1, f"git subprocess failed: {err}"


def ensure_clean_workspace(repo_root: str) -> Tuple[bool, str]:
    """Idempotently ensure ``repo_root`` is a CLEAN, AUTHORIZED checkout of the
    integration branch, ready for the graduation_pr_proposer.

    Clone-if-missing, else fetch + reset --hard origin/<branch> + clean -fd. Returns
    ``(ok, detail)``. Gated + fail-soft — NEVER raises. When the gate is off, returns
    (True, "disabled") so the proposer proceeds with repo_root as-is (legacy)."""
    if not workspace_enabled():
        return True, "disabled"
    if not repo_root:
        return False, "no_repo_root"
    if not git_identity_ready():
        # Advisory only — git env identity should be set by the deploy overlay.
        logger.warning(
            "[GraduationWorkspace] git identity env not set (GIT_AUTHOR_*/"
            "GIT_COMMITTER_*) — commits may fail; set them in the deploy overlay",
        )
    url = authed_remote_url()
    if not url:
        return False, "remote_or_token_unset"
    branch = _branch()
    git_dir = os.path.join(repo_root, ".git")
    try:
        if not os.path.isdir(git_dir):
            # Fresh clone (shallow — a single-commit PR diff needs no history).
            parent = os.path.dirname(repo_root.rstrip("/")) or "."
            os.makedirs(parent, exist_ok=True)
            rc, out = _run([
                "git", "clone", "--quiet", "--depth", str(_clone_depth()),
                "--branch", branch, url, repo_root,
            ])
            if rc != 0:
                return False, f"clone_failed:{_mask(out)[:200]}"
            logger.info(
                "[GraduationWorkspace] cloned %s @ %s → %s",
                _mask(url), branch, repo_root,
            )
            return True, "cloned"
        # Existing checkout — refresh to a pristine origin/<branch>.
        # (Re-point origin to the authed URL in case the token rotated.)
        _run(["git", "remote", "set-url", "origin", url], cwd=repo_root)
        rc, out = _run(
            ["git", "fetch", "--quiet", "--depth", str(_clone_depth()), "origin", branch],
            cwd=repo_root,
        )
        if rc != 0:
            return False, f"fetch_failed:{_mask(out)[:200]}"
        rc, out = _run(["git", "reset", "--hard", f"origin/{branch}"], cwd=repo_root)
        if rc != 0:
            return False, f"reset_failed:{_mask(out)[:200]}"
        _run(["git", "clean", "-fd"], cwd=repo_root)
        logger.info(
            "[GraduationWorkspace] refreshed %s to clean origin/%s", repo_root, branch,
        )
        return True, "refreshed"
    except Exception as exc:  # noqa: BLE001 — never raise into the graduation path
        return False, f"workspace_error:{type(exc).__name__}"


__all__ = [
    "ensure_clean_workspace",
    "authed_remote_url",
    "git_identity_ready",
    "workspace_enabled",
]
