"""OrangePRReviewer - async review path for APPROVAL_REQUIRED (Orange) ops.

When the Ouroboros risk engine classifies a change as ``APPROVAL_REQUIRED``,
the default flow is to block the autonomous loop on a synchronous CLI
approval. That's safe, but it also freezes the entire organism on a single
human's inbox - a real problem once the loop is running 24/7.

This module provides an alternative handoff: instead of blocking, the
orchestrator creates a git branch, commits the candidate files, pushes, and
opens a GitHub PR so the human can review asynchronously. The autonomous
loop continues immediately with the next op; the human reviews the PR on
their own time. Manifesto §7 (absolute observability) - the PR itself is
the auditable artifact (diff + evidence + rationale in one place).

Boundary Principle (Manifesto §6):
  Deterministic: git branch/commit/push, ``gh pr create`` invocation,
  commit message / PR body templating.
  Agentic: the candidate content being reviewed (the model wrote it).

Opt-in. Master switch: ``JARVIS_ORANGE_PR_ENABLED`` (default ``false``).

All subprocess calls use ``subprocess.run`` with an argument *list* (no
shell interpolation, no command injection). The async entry point wraps
the sync calls with ``asyncio.to_thread`` so the event loop stays free.
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_GIT_TIMEOUT_S = float(os.environ.get("JARVIS_ORANGE_PR_GIT_TIMEOUT_S", "15"))
_GH_TIMEOUT_S = float(os.environ.get("JARVIS_ORANGE_PR_GH_TIMEOUT_S", "30"))


@dataclass
class PRReviewResult:
    """Outcome of an async-review PR creation."""

    url: str
    branch: str
    base_branch: str


def is_orange_pr_enabled() -> bool:
    """True when the Orange-tier async PR path is active.

    Slice 198 — three-state: an explicit ``JARVIS_ORANGE_PR_ENABLED`` value
    wins (``=0`` is the supreme kill switch); when UNSET the gate ARMS itself
    once the organism has autonomously graduated AND a live gh+git preflight
    passes (``orange_pr_armed`` — no push, no blocking CLI prompt). A
    headless/gitless container correctly does NOT arm. Fail-soft: arming
    module unavailable → legacy default-FALSE."""
    raw = os.environ.get("JARVIS_ORANGE_PR_ENABLED", "").strip().lower()
    if raw == "":
        try:
            from backend.core.ouroboros.governance.m10_autonomous_graduation import (  # noqa: E501
                orange_pr_armed,
            )
            return bool(orange_pr_armed())
        except Exception:  # noqa: BLE001
            return False
    return raw in ("1", "true", "yes", "on")


def _token_enforcer_enabled() -> bool:
    """True when the Iron Triad token enforcer gates ``create_review_pr``.

    Default-OFF (``JARVIS_A1_TOKEN_ENFORCER_ENABLED`` unset/false) is
    byte-identical legacy: the enforcer block is fully skipped and no
    token-machinery imports are evaluated.
    """
    return os.environ.get("JARVIS_A1_TOKEN_ENFORCER_ENABLED", "false").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def build_commit_message(
    op_id: str,
    description: str,
    files: List[Tuple[str, str]],
    risk_tier_name: str = "APPROVAL_REQUIRED",
) -> str:
    """Deterministic commit message for an Orange review PR.

    The subject follows Conventional Commits (``chore(ouroboros-review): ...``)
    so the repo's commit tooling keeps working. The body lists the files
    and carries an explicit "DO NOT AUTO-MERGE" marker.
    """
    subject = f"chore(ouroboros-review): {description[:60].strip()}"
    file_lines = "\n".join(f"  - {fp}" for fp, _ in files[:20])
    if len(files) > 20:
        file_lines += f"\n  - ... and {len(files) - 20} more"
    body = (
        f"Ouroboros op {op_id} classified as {risk_tier_name}.\n\n"
        f"Files in this change:\n{file_lines}\n\n"
        f"DO NOT AUTO-MERGE. Human review required per Manifesto §6 "
        f"(the deterministic perimeter refuses to apply this change without "
        f"explicit human consent).\n"
    )
    return f"{subject}\n\n{body}"


def build_pr_body(
    op_id: str,
    description: str,
    files: List[Tuple[str, str]],
    evidence: Optional[Dict[str, Any]] = None,
    risk_tier_name: str = "APPROVAL_REQUIRED",
) -> str:
    """Markdown PR body with the full review context for a human reviewer."""
    lines: List[str] = [
        "## Ouroboros Review Request",
        "",
        f"**Op ID:** `{op_id}`",
        f"**Risk tier:** `{risk_tier_name}` (Orange - human approval required)",
        f"**Description:** {description}",
        "",
        "### Files changed",
        "",
    ]
    for fp, _ in files[:30]:
        lines.append(f"- `{fp}`")
    if len(files) > 30:
        lines.append(f"- *... and {len(files) - 30} more*")

    if evidence:
        lines.extend(["", "### Risk evidence", "", "```json"])
        import json
        try:
            lines.append(json.dumps(evidence, indent=2, default=str)[:2000])
        except Exception:
            lines.append("(evidence serialization failed)")
        lines.append("```")

    lines.extend([
        "",
        "### Review checklist",
        "",
        "- [ ] The diff does what the description claims",
        "- [ ] Tests cover the new behavior (or are added in this PR)",
        "- [ ] No new hardcoded model names, credentials, or URLs",
        "- [ ] Nothing bypasses Manifesto §6 (Iron Gate) boundaries",
        "",
        "---",
        "*Filed automatically by the Ouroboros organism on behalf of an "
        "APPROVAL_REQUIRED change. The autonomous loop did NOT apply this "
        "change - it handed it off to you for review.*",
    ])
    return "\n".join(lines)


def _safe_branch_slug(op_id: str) -> str:
    """Convert an op_id into a git-branch-safe slug."""
    return "".join(c if (c.isalnum() or c in "-_") else "-" for c in op_id)[:40]


class OrangePRReviewer:
    """Creates a review PR for an APPROVAL_REQUIRED candidate.

    The default ``create_review_pr`` path uses ``subprocess.run`` with
    argument lists (no shell interpolation) for both ``git`` and ``gh``
    invocations. Tests may override ``_run_git_sync`` and ``_run_gh_sync``
    with fakes.

    Parameters
    ----------
    project_root:
        Repository root. Every git invocation runs here.
    git_timeout_s:
        Per-call timeout for ``git`` calls.
    gh_timeout_s:
        Per-call timeout for ``gh`` calls.
    """

    def __init__(
        self,
        project_root: Path,
        git_timeout_s: float = _GIT_TIMEOUT_S,
        gh_timeout_s: float = _GH_TIMEOUT_S,
    ) -> None:
        self._root = Path(project_root)
        self._git_timeout_s = git_timeout_s
        self._gh_timeout_s = gh_timeout_s

    # ------------------------------------------------------------------
    # Subprocess shims - override in tests
    # ------------------------------------------------------------------

    def _run_git_sync(self, args: List[str]) -> Tuple[int, str, str]:
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=str(self._root),
                capture_output=True,
                text=True,
                timeout=self._git_timeout_s,
                check=False,
            )
            return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as err:
            return 1, "", f"git subprocess failed: {err}"

    def _run_gh_sync(self, args: List[str]) -> Tuple[int, str, str]:
        try:
            proc = subprocess.run(
                ["gh", *args],
                cwd=str(self._root),
                capture_output=True,
                text=True,
                timeout=self._gh_timeout_s,
                check=False,
            )
            return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as err:
            return 1, "", f"gh subprocess failed: {err}"

    async def _run_git(self, *args: str) -> Tuple[int, str, str]:
        return await asyncio.to_thread(self._run_git_sync, list(args))

    async def _run_gh(self, *args: str) -> Tuple[int, str, str]:
        return await asyncio.to_thread(self._run_gh_sync, list(args))

    def _enforcer_enabled(self) -> bool:
        """Instance accessor for the Iron Triad token enforcer flag."""
        return _token_enforcer_enabled()

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def create_review_pr(
        self,
        op_id: str,
        description: str,
        files: List[Tuple[str, str]],
        evidence: Optional[Dict[str, Any]] = None,
        risk_tier_name: str = "APPROVAL_REQUIRED",
        body_override: Optional[str] = None,
        title_override: Optional[str] = None,
        *,
        chain: Any = None,
        sandbox_token: Any = None,
        blast_token: Any = None,
        lint_token: Any = None,
    ) -> Optional[PRReviewResult]:
        """Open an async-review PR for the given candidate.

        Returns a populated ``PRReviewResult`` on success, or ``None`` on
        any failure (caller falls back to synchronous CLI approval).

        The method is best-effort: it always attempts to return the
        working tree to the original branch via ``git checkout <base>``,
        even on error. Partial-failure state (e.g. committed but not
        pushed) is logged but not automatically rolled back - leaving the
        local branch makes it trivial to retry manually.
        """
        if not files:
            logger.warning("[OrangePR] no files in candidate for op=%s", op_id)
            return None

        # ---- Iron Triad enforcer: no verified token chain -> no PR ----
        # Default-OFF (JARVIS_A1_TOKEN_ENFORCER_ENABLED unset/false): this whole
        # block is skipped and the method behaves byte-identically to legacy.
        # All token-machinery imports live INSIDE this block so the OFF path
        # never evaluates them. Placed BEFORE any git op so an invalid/missing
        # chain refuses cheaply, without touching the working tree.
        if _token_enforcer_enabled():
            from .dag_capability_token import (
                SandboxExecutionToken,
                BlastRadiusClearedToken,
                LintClearedToken,
            )
            from .pr_self_linter import (
                acquire_lint_cleared_token,
                linter_enabled,
                LintRejected,
            )

            # Gate 3: mint the lint token here if not already supplied.
            if (
                linter_enabled()
                and lint_token is None
                and blast_token is not None
                and chain is not None
            ):
                _diff = "\n".join(f"--- {p}\n{c}" for p, c in files)
                try:
                    lint_token = await acquire_lint_cleared_token(
                        op_id=op_id,
                        diff=_diff,
                        chain=chain,
                        prev_token=blast_token,
                    )
                except LintRejected as _lr:
                    logger.warning("[Gate3] op=%s LINT_REJECTED: %s", op_id, _lr)
                    return None

            # Mandatory typed token objects + verified hash chain, or refuse.
            if not (
                isinstance(sandbox_token, SandboxExecutionToken)
                and isinstance(blast_token, BlastRadiusClearedToken)
                and isinstance(lint_token, LintClearedToken)
                and chain is not None
                and chain.verify_chain(
                    [sandbox_token, blast_token, lint_token], op_id=op_id
                )
            ):
                logger.warning(
                    "[Enforcer] op=%s missing/invalid token chain -> refuse PR",
                    op_id,
                )
                return None

        rc, base_branch, err = await self._run_git("rev-parse", "--abbrev-ref", "HEAD")
        if rc != 0 or not base_branch:
            logger.warning(
                "[OrangePR] rev-parse failed for op=%s: %s", op_id, err
            )
            return None

        # Refuse to operate on detached HEAD - no safe base for the PR.
        if base_branch == "HEAD":
            logger.warning(
                "[OrangePR] op=%s refused: detached HEAD has no base branch",
                op_id,
            )
            return None

        branch = f"ouroboros/review/{_safe_branch_slug(op_id)}"
        rc, _, err = await self._run_git("checkout", "-b", branch)
        if rc != 0:
            logger.warning(
                "[OrangePR] checkout -b %s failed for op=%s: %s",
                branch, op_id, err,
            )
            return None

        try:
            # Write candidate files and stage them.
            for fp, fc in files:
                rel = Path(fp)
                abs_path = rel if rel.is_absolute() else (self._root / rel)
                try:
                    abs_path.parent.mkdir(parents=True, exist_ok=True)
                    abs_path.write_text(fc, encoding="utf-8")
                except OSError as write_err:
                    logger.warning(
                        "[OrangePR] failed to write %s for op=%s: %s",
                        fp, op_id, write_err,
                    )
                    return None
                rc, _, err = await self._run_git("add", str(abs_path))
                if rc != 0:
                    logger.warning(
                        "[OrangePR] git add %s failed for op=%s: %s",
                        fp, op_id, err,
                    )
                    return None

            commit_msg = build_commit_message(
                op_id, description, files, risk_tier_name
            )
            rc, _, err = await self._run_git("commit", "-m", commit_msg)
            if rc != 0:
                logger.warning(
                    "[OrangePR] commit failed for op=%s: %s", op_id, err
                )
                return None

            rc, _, err = await self._run_git("push", "-u", "origin", branch)
            if rc != 0:
                logger.warning(
                    "[OrangePR] push failed for op=%s: %s", op_id, err
                )
                return None

            # body_override / title_override let an autonomous caller (e.g. the
            # Sovereign Cognitive Crucible) supply a self-authored PR body — the
            # Telemetry Manifest — and an exact title. Falls back to the
            # templated review body when not supplied (byte-identical legacy).
            pr_title = (
                title_override.strip() if title_override
                else f"[Ouroboros Review] {description[:72].strip()}"
            )
            pr_body = (
                body_override if body_override
                else build_pr_body(
                    op_id, description, files, evidence, risk_tier_name
                )
            )
            rc, out, err = await self._run_gh(
                "pr", "create",
                "--title", pr_title,
                "--body", pr_body,
                "--base", base_branch,
                "--head", branch,
            )
            if rc != 0:
                logger.warning(
                    "[OrangePR] gh pr create failed for op=%s: %s",
                    op_id, err,
                )
                return None
            if not out.startswith("http"):
                logger.warning(
                    "[OrangePR] gh returned non-URL for op=%s: %r", op_id, out
                )
                return None

            logger.info("[OrangePR] PR created for op=%s: %s", op_id, out)
            return PRReviewResult(url=out, branch=branch, base_branch=base_branch)
        finally:
            # Best-effort: return to the original branch so the next op
            # doesn't start life on the review branch.
            await self._run_git("checkout", base_branch)
