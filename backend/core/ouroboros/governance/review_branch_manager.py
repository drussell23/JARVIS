"""ReviewBranchManager — local-only git preview branches for IDE-native review.
==============================================================================

Slice 2 of the **Gap #4 closure arc**.

Root problem
------------

For Yellow-tier (NOTIFY_APPLY) operations, today's flow renders a 5s
overlay then auto-applies. The operator has no opportunity to inspect
the change in their IDE's native diff viewer. For Orange-tier
(APPROVAL_REQUIRED), the existing :class:`OrangePRReviewer` creates an
``ouroboros/review/{op-id}`` branch but immediately ``git push``-es it
and opens a GitHub PR — requires network round-trip and external review
surface.

This module supplies the **local, non-destructive** branch primitive:
both Yellow and Orange tiers can create an ``ouroboros/preview/{op-id}``
branch carrying the candidate diff that VS Code's native source control
can compare against ``HEAD`` *without* requiring the operator to open a
PR or even leave the editor.

Non-destructive plumbing
-------------------------

The standard "checkout -b + write files + commit + checkout back" path
(see :class:`OrangePRReviewer`) is destructive: it briefly moves the
working tree to the review branch. If the operator's editor is open on
HEAD when the orchestrator does this, files appear to flicker.

Instead, this module uses git's plumbing layer:

  1. Hash candidate file contents into the object store
     (``git hash-object -w --stdin``)
  2. Read ``HEAD``'s tree into a **temporary index**
     (``GIT_INDEX_FILE=<tmp> git read-tree HEAD``)
  3. Stage the new blobs in the temp index
     (``git update-index --add --cacheinfo``)
  4. Write the temp index as a tree (``git write-tree``)
  5. Create a commit object on top of HEAD (``git commit-tree``)
  6. Point a fresh branch ref at the commit (``git branch <name> <sha>``)

Result: working tree, HEAD, index, and operator's editor state are all
untouched. The branch exists; VS Code's source control auto-detects it
and the extension's Slice 5 ``vscode.diff`` command can render the
diff natively.

Authority boundary
------------------

* §1 deterministic — pure git plumbing; no LLM, no model decisions
* §6 Iron Gate — refuses to operate when the working tree has uncommitted
  changes (``BLOCKED`` outcome with diagnostic) or when ``HEAD`` is
  detached. We never silently overwrite operator state.
* §7 fail-closed — every subprocess invocation has a documented timeout
  + capture; on any failure the manager returns a structured
  ``ReviewBranch(state=FAILED, ...)`` rather than raising
* §8 observable — frozen records suitable for SSE serialization (Slice
  4) carrying full enough metadata for ``/review`` REPL listings

What this module does NOT do
----------------------------

* Push branches anywhere — local-only by design
* Open PRs — that's :class:`OrangePRReviewer`'s job (Slice 3 will route
  Orange tier through *both* this manager AND the PR reviewer)
* Manage worktrees — :class:`WorktreeManager` is the L3-isolation tool
  for parallel execution; preview branches are not worktrees
"""
from __future__ import annotations

import asyncio
import enum
import logging
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("Ouroboros.ReviewBranchManager")


# ===========================================================================
# Schema + env vocabulary
# ===========================================================================


REVIEW_BRANCH_SCHEMA_VERSION: str = "review_branch.v1"


# Branch namespace — matches OrangePRReviewer's ``ouroboros/review/`` style
# but distinct so the two flows can coexist without ambiguity.
BRANCH_PREFIX: str = "ouroboros/preview/"


# Subprocess timeout — bounded so a hung git call can't deadlock the
# orchestrator. 15s matches OrangePRReviewer's default.
_GIT_TIMEOUT_S: float = float(
    os.environ.get("JARVIS_REVIEW_BRANCH_GIT_TIMEOUT_S", "15"),
)


# Reserved-character regex for branch slug — matches OrangePRReviewer
_VALID_SLUG_CHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_",
)


# ===========================================================================
# Closed taxonomy — review branch lifecycle
# ===========================================================================


class ReviewState(str, enum.Enum):
    """Closed 5-value review-branch lifecycle.

    ``PENDING`` is the only non-terminal state. The other four are all
    end-states for the branch (the operator made a decision, or the
    system gave up trying to act on it).
    """

    PENDING = "pending"           # branch created, operator has not decided
    ACCEPTED = "accepted"         # operator accepted; fast-forward merged into base
    REJECTED = "rejected"         # operator rejected; branch deleted
    SUPERSEDED = "superseded"     # newer candidate replaced this one mid-review
    EXPIRED = "expired"           # timeout window elapsed without operator action

    @classmethod
    def coerce(cls, raw: object) -> "ReviewState":
        if isinstance(raw, cls):
            return raw
        if isinstance(raw, str):
            s = raw.strip().lower()
            for member in cls:
                if member.value == s:
                    return member
        return cls.PENDING

    @property
    def is_terminal(self) -> bool:
        return self is not ReviewState.PENDING


class CreateOutcome(str, enum.Enum):
    """Closed 4-value outcome for :meth:`ReviewBranchManager.create`.

    Distinct from :class:`ReviewState` because creation can fail in
    structurally different ways: a successful creation enters
    ``PENDING`` state; a failure is a *non-state* (no branch exists).
    """

    CREATED = "created"
    BLOCKED = "blocked"          # dirty tree / detached HEAD / etc.
    COLLISION = "collision"      # branch name already exists
    FAILED = "failed"            # git plumbing error


class AcceptOutcome(str, enum.Enum):
    """Closed 4-value outcome for :meth:`ReviewBranchManager.accept`."""

    ACCEPTED = "accepted"
    BLOCKED = "blocked"          # dirty tree at accept time
    NOT_FAST_FORWARD = "not_fast_forward"   # base diverged from preview
    FAILED = "failed"


class RejectOutcome(str, enum.Enum):
    """Closed 3-value outcome for :meth:`ReviewBranchManager.reject`."""

    REJECTED = "rejected"
    NOT_FOUND = "not_found"      # branch already deleted / never existed
    FAILED = "failed"


# ===========================================================================
# Frozen records
# ===========================================================================


@dataclass(frozen=True)
class ReviewBranch:
    """One review branch (created or attempted-but-failed).

    Frozen + hashable. State transitions produce new records via
    :func:`dataclasses.replace`; the manager's in-memory index stores
    the latest record per ``op_id``.

    Fields
    ------
    * ``branch_name`` — full branch ref (e.g.
      ``"ouroboros/preview/op-019d8…"``).
    * ``op_id`` — orchestrator op id.
    * ``base_branch`` — branch HEAD was on at creation time
      (e.g. ``"main"``).
    * ``base_sha`` — SHA the branch was created on top of.
    * ``tip_sha`` — SHA the branch points at (the candidate commit).
      Empty string when ``state`` is BLOCKED / COLLISION / FAILED.
    * ``file_paths`` — repo-relative paths the diff touches.
    * ``risk_tier`` — ``"notify_apply"`` / ``"approval_required"`` /
      etc. Stored as string for forward-compat.
    * ``diff_archive_ref`` — Slice 1 ref (``"d-12"``); links the
      branch to its archived diff entry.
    * ``state`` — :class:`ReviewState` (PENDING at creation success).
    * ``created_at`` — ``time.monotonic()`` timestamp.
    * ``terminal_at`` — ``time.monotonic()`` when state first became
      terminal; ``0.0`` while pending.
    * ``error`` — short reason when state is REJECTED / EXPIRED with
      diagnostic context. Empty string for normal lifecycle.
    """

    branch_name: str
    op_id: str
    base_branch: str
    base_sha: str
    tip_sha: str
    file_paths: Tuple[str, ...]
    risk_tier: str
    diff_archive_ref: str
    state: ReviewState
    created_at: float
    terminal_at: float
    error: str
    schema_version: str = REVIEW_BRANCH_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, object]:
        return {
            "branch_name": self.branch_name,
            "op_id": self.op_id,
            "base_branch": self.base_branch,
            "base_sha": self.base_sha,
            "tip_sha": self.tip_sha,
            "file_paths": list(self.file_paths),
            "risk_tier": self.risk_tier,
            "diff_archive_ref": self.diff_archive_ref,
            "state": self.state.value,
            "created_at": self.created_at,
            "terminal_at": self.terminal_at,
            "error": self.error,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class CreateResult:
    """Outcome of :meth:`ReviewBranchManager.create`."""

    outcome: CreateOutcome
    branch: Optional[ReviewBranch]
    error: str = ""


@dataclass(frozen=True)
class AcceptResult:
    """Outcome of :meth:`ReviewBranchManager.accept`."""

    outcome: AcceptOutcome
    branch: Optional[ReviewBranch]
    merged_sha: str = ""
    error: str = ""


@dataclass(frozen=True)
class RejectResult:
    """Outcome of :meth:`ReviewBranchManager.reject`."""

    outcome: RejectOutcome
    branch: Optional[ReviewBranch]
    error: str = ""


# ===========================================================================
# Helpers
# ===========================================================================


def safe_branch_slug(op_id: str) -> str:
    """Convert an op_id into a git-branch-safe slug.

    Mirrors :func:`OrangePRReviewer._safe_branch_slug` so the namespace
    rules are identical between the two flows.
    """
    return "".join(
        c if c in _VALID_SLUG_CHARS else "-" for c in (op_id or "")
    )[:40]


def build_branch_name(op_id: str) -> str:
    """``"ouroboros/preview/{slug}"`` — module-pinned namespace."""
    return f"{BRANCH_PREFIX}{safe_branch_slug(op_id)}"


def _safe_str(raw: object) -> str:
    if raw is None:
        return ""
    try:
        return str(raw)
    except Exception:  # noqa: BLE001
        return ""


def _safe_path_tuple(raw: object) -> Tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        return (raw,)
    if not hasattr(raw, "__iter__"):
        return ()
    try:
        return tuple(_safe_str(x) for x in raw if _safe_str(x))  # type: ignore[union-attr]
    except TypeError:
        return ()


# ===========================================================================
# ReviewBranchManager
# ===========================================================================


class ReviewBranchManager:
    """Local-only ``ouroboros/preview/{op-id}`` branch creator + acceptor.

    Thread-safety: methods are not internally synchronized — the
    expected caller is the orchestrator which serializes per-op
    pipeline phases. The in-memory index is appended atomically per
    method via the dict's GIL semantics.

    Subprocess shims (``_run_git_sync`` / ``_run_git``) mirror
    :class:`OrangePRReviewer`'s pattern so tests can override either.
    """

    def __init__(
        self,
        project_root: Path,
        *,
        git_timeout_s: float = _GIT_TIMEOUT_S,
    ) -> None:
        self._root = Path(project_root).resolve()
        self._git_timeout_s = git_timeout_s
        # In-memory index of latest record per op_id. Bounded only by
        # the orchestrator's session lifetime; the DiffArchive (Slice
        # 1) carries the size-bounded view for /review listings.
        self._records: Dict[str, ReviewBranch] = {}

    # ---- subprocess shims ---------------------------------------------

    def _run_git_sync(
        self,
        args: List[str],
        *,
        env_overrides: Optional[Dict[str, str]] = None,
        stdin_bytes: Optional[bytes] = None,
    ) -> Tuple[int, str, str]:
        """Run ``git <args>`` in the project root with optional env + stdin.

        Returns ``(rc, stdout_stripped, stderr_stripped)``. NEVER raises:
        on subprocess failure returns ``(1, "", err_msg)``.
        """
        run_env = os.environ.copy()
        if env_overrides:
            run_env.update(env_overrides)
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=str(self._root),
                env=run_env,
                input=stdin_bytes,
                capture_output=True,
                timeout=self._git_timeout_s,
                check=False,
            )
            return (
                proc.returncode,
                proc.stdout.decode("utf-8", errors="replace").strip(),
                proc.stderr.decode("utf-8", errors="replace").strip(),
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as err:
            return 1, "", f"git subprocess failed: {err}"

    async def _run_git(
        self, *args: str,
        env_overrides: Optional[Dict[str, str]] = None,
        stdin_bytes: Optional[bytes] = None,
    ) -> Tuple[int, str, str]:
        return await asyncio.to_thread(
            self._run_git_sync, list(args),
            env_overrides=env_overrides,
            stdin_bytes=stdin_bytes,
        )

    # ---- introspection ------------------------------------------------

    def lookup(self, op_id: object) -> Optional[ReviewBranch]:
        """Return the latest record for ``op_id``, or ``None``."""
        if not isinstance(op_id, str) or not op_id:
            return None
        return self._records.get(op_id)

    def list_pending(self) -> Tuple[ReviewBranch, ...]:
        """All records currently in :data:`ReviewState.PENDING`."""
        return tuple(
            r for r in self._records.values()
            if r.state is ReviewState.PENDING
        )

    def list_all(self) -> Tuple[ReviewBranch, ...]:
        """All records in this session (bounded by orchestrator
        lifetime, not by capacity)."""
        return tuple(self._records.values())

    # ---- preconditions ------------------------------------------------

    async def _check_preconditions(
        self,
    ) -> Tuple[Optional[Tuple[str, str]], str]:
        """Return ``((base_branch, base_sha), "")`` on success, or
        ``(None, error_msg)`` on a precondition failure."""
        rc, base_branch, err = await self._run_git(
            "rev-parse", "--abbrev-ref", "HEAD",
        )
        if rc != 0 or not base_branch:
            return None, f"rev-parse HEAD failed: {err}"
        if base_branch == "HEAD":
            return None, "detached HEAD has no review base"

        rc, base_sha, err = await self._run_git("rev-parse", "HEAD")
        if rc != 0 or not base_sha:
            return None, f"rev-parse SHA failed: {err}"

        # Dirty-tree check. ``--porcelain`` returns one line per
        # changed-or-untracked path. We refuse to operate on a dirty
        # tree because accept() will need to fast-forward merge
        # without conflicts.
        rc, status, _ = await self._run_git(
            "status", "--porcelain", "--untracked-files=no",
        )
        if rc != 0:
            return None, f"git status failed: {status}"
        if status:
            return None, f"working tree dirty: {status.splitlines()[0][:80]}"

        return (base_branch, base_sha), ""

    async def _branch_exists(self, branch_name: str) -> bool:
        rc, _, _ = await self._run_git(
            "show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}",
        )
        return rc == 0

    # ---- create -------------------------------------------------------

    async def create(
        self,
        op_id: str,
        files: Sequence[Tuple[str, str]],
        *,
        risk_tier: str = "",
        diff_archive_ref: str = "",
    ) -> CreateResult:
        """Create the preview branch with ``files`` committed on top of
        HEAD, **without touching the working tree, index, or HEAD**.

        ``files`` is a sequence of ``(repo_relative_path, content)``
        pairs. Empty content is allowed (file becomes empty); ``None``
        content (use the legacy delete signal) is NOT supported here —
        deletes are out of scope for the preview-branch flow until a
        future arc surfaces them.
        """
        op_id_safe = _safe_str(op_id)
        if not op_id_safe:
            return CreateResult(
                outcome=CreateOutcome.FAILED,
                branch=None,
                error="empty op_id",
            )
        if not files:
            return CreateResult(
                outcome=CreateOutcome.FAILED,
                branch=None,
                error="empty file list",
            )

        ok, err = await self._check_preconditions()
        if ok is None:
            return CreateResult(
                outcome=CreateOutcome.BLOCKED,
                branch=None,
                error=err,
            )
        base_branch, base_sha = ok

        branch_name = build_branch_name(op_id_safe)
        if await self._branch_exists(branch_name):
            return CreateResult(
                outcome=CreateOutcome.COLLISION,
                branch=None,
                error=f"branch {branch_name} already exists",
            )

        path_tuple = _safe_path_tuple(p for p, _ in files)

        # Use a temp index — keeps the operator's real index untouched.
        tmp_index_fd, tmp_index_path = tempfile.mkstemp(
            prefix="ov-preview-index-",
        )
        os.close(tmp_index_fd)
        try:
            os.unlink(tmp_index_path)  # git wants the path absent
        except OSError:
            pass

        env_overrides = {"GIT_INDEX_FILE": tmp_index_path}

        try:
            # Step 1: load HEAD's tree into the temp index.
            rc, _, err = await self._run_git(
                "read-tree", base_sha,
                env_overrides=env_overrides,
            )
            if rc != 0:
                return CreateResult(
                    outcome=CreateOutcome.FAILED,
                    branch=None,
                    error=f"read-tree failed: {err}",
                )

            # Step 2-3: hash-object + update-index for each candidate file.
            for rel_path, content in files:
                content_bytes = (content or "").encode("utf-8")
                rc, blob_sha, err = await self._run_git(
                    "hash-object", "-w", "--stdin",
                    stdin_bytes=content_bytes,
                )
                if rc != 0 or not blob_sha:
                    return CreateResult(
                        outcome=CreateOutcome.FAILED,
                        branch=None,
                        error=f"hash-object {rel_path} failed: {err}",
                    )
                rc, _, err = await self._run_git(
                    "update-index", "--add",
                    "--cacheinfo", f"100644,{blob_sha},{rel_path}",
                    env_overrides=env_overrides,
                )
                if rc != 0:
                    return CreateResult(
                        outcome=CreateOutcome.FAILED,
                        branch=None,
                        error=f"update-index {rel_path} failed: {err}",
                    )

            # Step 4: snapshot the temp index as a tree.
            rc, tree_sha, err = await self._run_git(
                "write-tree",
                env_overrides=env_overrides,
            )
            if rc != 0 or not tree_sha:
                return CreateResult(
                    outcome=CreateOutcome.FAILED,
                    branch=None,
                    error=f"write-tree failed: {err}",
                )

            # Step 5: commit the tree on top of HEAD.
            commit_msg = (
                f"O+V review: {op_id_safe}\n\n"
                f"Risk tier: {risk_tier or 'unknown'}\n"
                f"Files: {len(files)}\n"
                f"DiffArchive ref: {diff_archive_ref or '(unset)'}\n"
            )
            rc, commit_sha, err = await self._run_git(
                "commit-tree", tree_sha, "-p", base_sha, "-m", commit_msg,
            )
            if rc != 0 or not commit_sha:
                return CreateResult(
                    outcome=CreateOutcome.FAILED,
                    branch=None,
                    error=f"commit-tree failed: {err}",
                )

            # Step 6: point a fresh branch ref at the commit.
            rc, _, err = await self._run_git(
                "branch", branch_name, commit_sha,
            )
            if rc != 0:
                return CreateResult(
                    outcome=CreateOutcome.FAILED,
                    branch=None,
                    error=f"branch creation failed: {err}",
                )

            record = ReviewBranch(
                branch_name=branch_name,
                op_id=op_id_safe,
                base_branch=base_branch,
                base_sha=base_sha,
                tip_sha=commit_sha,
                file_paths=path_tuple,
                risk_tier=_safe_str(risk_tier).lower() or "unknown",
                diff_archive_ref=_safe_str(diff_archive_ref),
                state=ReviewState.PENDING,
                created_at=time.monotonic(),
                terminal_at=0.0,
                error="",
            )
            self._records[op_id_safe] = record
            logger.info(
                "[ReviewBranch] created %s @ %s for op=%s",
                branch_name, commit_sha[:8], op_id_safe,
            )
            return CreateResult(
                outcome=CreateOutcome.CREATED, branch=record,
            )
        finally:
            try:
                if os.path.exists(tmp_index_path):
                    os.unlink(tmp_index_path)
            except OSError:
                logger.debug(
                    "[ReviewBranch] tmp-index cleanup failed: %s",
                    tmp_index_path,
                )

    # ---- accept -------------------------------------------------------

    async def accept(self, op_id: str) -> AcceptResult:
        """Fast-forward merge the preview branch into ``base_branch``,
        then delete the preview branch.

        Refuses on dirty tree (BLOCKED) or non-fast-forward (the base
        moved since branch creation — mid-review SUPERSEDED races).
        NEVER raises.
        """
        op_id_safe = _safe_str(op_id)
        record = self._records.get(op_id_safe)
        if record is None:
            return AcceptResult(
                outcome=AcceptOutcome.FAILED,
                branch=None,
                error=f"unknown op_id: {op_id_safe!r}",
            )
        if record.state is not ReviewState.PENDING:
            return AcceptResult(
                outcome=AcceptOutcome.FAILED,
                branch=record,
                error=f"already terminal: {record.state.value}",
            )

        # Re-check working tree at accept time (operator may have edited
        # between create and accept).
        rc, status, _ = await self._run_git(
            "status", "--porcelain", "--untracked-files=no",
        )
        if rc != 0 or status:
            return AcceptResult(
                outcome=AcceptOutcome.BLOCKED,
                branch=record,
                error=f"working tree dirty at accept: "
                      f"{status.splitlines()[0][:80] if status else 'unknown'}",
            )

        # Verify fast-forward — HEAD must be an ancestor of the preview tip.
        rc, _, _ = await self._run_git(
            "merge-base", "--is-ancestor", "HEAD", record.branch_name,
        )
        if rc != 0:
            updated = self._mark_terminal(
                record, ReviewState.SUPERSEDED,
                error="base moved; not fast-forward",
            )
            return AcceptResult(
                outcome=AcceptOutcome.NOT_FAST_FORWARD,
                branch=updated,
                error="HEAD is no longer ancestor of preview branch",
            )

        # Fast-forward HEAD to the preview tip.
        rc, _, err = await self._run_git(
            "merge", "--ff-only", record.branch_name,
        )
        if rc != 0:
            return AcceptResult(
                outcome=AcceptOutcome.FAILED,
                branch=record,
                error=f"merge --ff-only failed: {err}",
            )

        # Delete the now-merged branch.
        rc, _, err = await self._run_git(
            "branch", "-d", record.branch_name,
        )
        if rc != 0:
            # Soft-failure: HEAD has the merged commit; the branch ref
            # is stale but harmless. Log and proceed.
            logger.debug(
                "[ReviewBranch] post-merge branch delete failed: %s", err,
            )

        updated = self._mark_terminal(record, ReviewState.ACCEPTED)
        return AcceptResult(
            outcome=AcceptOutcome.ACCEPTED,
            branch=updated,
            merged_sha=record.tip_sha,
        )

    # ---- reject -------------------------------------------------------

    async def reject(
        self, op_id: str, *, reason: str = "",
    ) -> RejectResult:
        """Delete the preview branch (force, since it's not on HEAD).

        Reason is recorded in the in-memory record for audit. NEVER raises.
        """
        op_id_safe = _safe_str(op_id)
        record = self._records.get(op_id_safe)
        if record is None:
            return RejectResult(
                outcome=RejectOutcome.NOT_FOUND,
                branch=None,
                error=f"unknown op_id: {op_id_safe!r}",
            )
        if record.state is not ReviewState.PENDING:
            return RejectResult(
                outcome=RejectOutcome.FAILED,
                branch=record,
                error=f"already terminal: {record.state.value}",
            )

        # ``-D`` to force; the branch isn't merged into HEAD.
        rc, _, err = await self._run_git(
            "branch", "-D", record.branch_name,
        )
        if rc != 0:
            # Branch may have already been deleted (e.g. by a stray
            # operator command). That's OK — we still mark rejected.
            if "not found" not in err.lower():
                return RejectResult(
                    outcome=RejectOutcome.FAILED,
                    branch=record,
                    error=f"branch -D failed: {err}",
                )

        updated = self._mark_terminal(
            record, ReviewState.REJECTED, error=_safe_str(reason),
        )
        return RejectResult(outcome=RejectOutcome.REJECTED, branch=updated)

    # ---- expire (timeout-driven, called by orchestrator) ---------------

    async def expire(self, op_id: str) -> RejectResult:
        """Same effect as :meth:`reject` but stamps EXPIRED state.

        Called by the orchestrator's timeout watchdog when
        ``JARVIS_REVIEW_TIMEOUT_S`` elapses without operator action.
        """
        op_id_safe = _safe_str(op_id)
        record = self._records.get(op_id_safe)
        if record is None:
            return RejectResult(
                outcome=RejectOutcome.NOT_FOUND,
                branch=None,
                error=f"unknown op_id: {op_id_safe!r}",
            )
        if record.state is not ReviewState.PENDING:
            return RejectResult(
                outcome=RejectOutcome.FAILED,
                branch=record,
                error=f"already terminal: {record.state.value}",
            )

        rc, _, err = await self._run_git(
            "branch", "-D", record.branch_name,
        )
        if rc != 0 and "not found" not in err.lower():
            return RejectResult(
                outcome=RejectOutcome.FAILED,
                branch=record,
                error=f"branch -D failed during expire: {err}",
            )

        updated = self._mark_terminal(
            record, ReviewState.EXPIRED,
            error="review timeout elapsed without operator action",
        )
        return RejectResult(outcome=RejectOutcome.REJECTED, branch=updated)

    # ---- internal helpers ---------------------------------------------

    def _mark_terminal(
        self, record: ReviewBranch, new_state: ReviewState,
        *, error: str = "",
    ) -> ReviewBranch:
        from dataclasses import replace
        updated = replace(
            record,
            state=new_state,
            terminal_at=time.monotonic(),
            error=error,
        )
        self._records[record.op_id] = updated
        return updated


__all__ = [
    "AcceptOutcome",
    "AcceptResult",
    "BRANCH_PREFIX",
    "CreateOutcome",
    "CreateResult",
    "REVIEW_BRANCH_SCHEMA_VERSION",
    "RejectOutcome",
    "RejectResult",
    "ReviewBranch",
    "ReviewBranchManager",
    "ReviewState",
    "build_branch_name",
    "safe_branch_slug",
]
