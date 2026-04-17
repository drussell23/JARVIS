"""``/undo N`` command — revert the last N AutoCommitter-produced commits.

Closes the Priority 2 trust-building gap: O+V auto-commits code without
blocking the operator, but a one-keystroke "undo that last autonomous
commit" has been missing — meaning operators had to hand-roll ``git
revert`` against commits they might not have identified as O+V yet.
This module provides the user-facing command SerpentFlow's REPL hooks
into.

Two-phase design (plan → execute):
  • :class:`UndoPlanner` — read-only. Walks ``HEAD..HEAD~N``, classifies
    each commit by the canonical O+V trailer
    (``Co-Authored-By: Ouroboros+Venom <ouroboros@jarvis.trinity>``),
    runs the safety gate, and returns an :class:`UndoPlan` describing
    what *would* happen. Used by ``/undo preview N`` and also always
    run first inside ``/undo N`` so the operator sees the safety verdict
    before any git mutation.
  • :class:`UndoExecutor` — mutation. Given a passing plan, runs
    ``git revert --no-commit <oldest>..HEAD`` + one O+V-signed revert
    commit (default) or ``git reset --hard HEAD~N`` (opt-in ``--hard``,
    unpushed only). Transactional: on any failure during revert, fires
    ``git revert --abort`` so the working tree returns to its pre-undo
    state.

Safety gate (all must pass before execute):
  1. ``JARVIS_UNDO_ENABLED`` env is truthy
  2. Working tree clean (``git status --porcelain`` empty)
  3. No in-flight ops (``gls._active_ops`` empty)
  4. All N commits have the O+V trailer (refuse if ANY manual commit
     sits in the range — partial-undo would clobber human work)
  5. ``N ≤ JARVIS_UNDO_MAX_BATCH`` (default 10)
  6. ``--hard`` mode requires branch is NOT pushed upstream

Authority invariant: this module runs ``git`` subprocess calls only.
It never touches governance surfaces (Iron Gate, UrgencyRouter, risk
tier, policy engine, FORBIDDEN_PATH, ToolExecutor protected-path).
Operator-triggered only — no autonomous code path invokes
``UndoExecutor.execute()``.
"""
from __future__ import annotations

import logging
import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Tuple

logger = logging.getLogger("Ouroboros.Undo")

_ENV_ENABLED = "JARVIS_UNDO_ENABLED"
_ENV_MAX_BATCH = "JARVIS_UNDO_MAX_BATCH"

_TRUTHY = frozenset({"1", "true", "yes", "on"})

# Canonical O+V trailer — must match auto_committer.py::_OV_COAUTHOR.
# Any future change to that string must update this one; the AST canary
# in tests/battle_test/test_undo_command.py locks the invariant.
_OV_TRAILER = "Co-Authored-By: Ouroboros+Venom <ouroboros@jarvis.trinity>"

_DEFAULT_MAX_BATCH = 10


def undo_enabled() -> bool:
    """Env master switch. Default: ON."""
    return os.environ.get(_ENV_ENABLED, "1").strip().lower() in _TRUTHY


def max_batch() -> int:
    """Largest N allowed per ``/undo N``. Default: 10."""
    try:
        val = int(os.environ.get(_ENV_MAX_BATCH, str(_DEFAULT_MAX_BATCH)))
    except (TypeError, ValueError):
        val = _DEFAULT_MAX_BATCH
    return max(1, min(100, val))


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UndoTarget:
    """One commit that a ``/undo`` would or would not revert."""

    sha: str                        # full SHA (40 chars)
    short_sha: str                  # first 10 chars, for display
    subject: str                    # first line of commit message
    is_ov: bool                     # has the O+V trailer?
    author_email: str = ""
    files_changed: Tuple[str, ...] = ()
    insertions: int = 0
    deletions: int = 0

    @property
    def display_label(self) -> str:
        tag = "[O+V]" if self.is_ov else "[manual]"
        return f"{self.short_sha} {tag} {self.subject}"


@dataclass
class UndoPlan:
    """Output of :meth:`UndoPlanner.plan` — read-only description of
    what ``/undo N`` would do.

    ``safety_errors`` is non-empty → executor MUST refuse. Each error
    is a human-readable sentence the operator should see verbatim.
    ``safety_warnings`` is advisory (e.g. "branch is pushed — using
    revert commits instead of reset").
    """

    mode: str = "revert"            # "revert" | "hard" | "preview"
    requested_n: int = 1
    targets: Tuple[UndoTarget, ...] = ()
    safety_errors: Tuple[str, ...] = ()
    safety_warnings: Tuple[str, ...] = ()
    branch_is_pushed: bool = False
    upstream_name: str = ""         # "origin/main", "", etc.

    @property
    def is_safe(self) -> bool:
        return not self.safety_errors

    @property
    def total_insertions(self) -> int:
        return sum(t.insertions for t in self.targets)

    @property
    def total_deletions(self) -> int:
        return sum(t.deletions for t in self.targets)

    @property
    def unique_files(self) -> Tuple[str, ...]:
        seen: dict = {}
        for t in self.targets:
            for f in t.files_changed:
                seen[f] = True
        return tuple(seen.keys())


@dataclass
class UndoResult:
    """Output of :meth:`UndoExecutor.execute`.

    ``executed=True`` iff the git mutation completed successfully and
    (for revert mode) produced a single revert commit. ``committed_sha``
    is the new revert-commit SHA when applicable.
    """

    executed: bool = False
    mode: str = ""
    n_reverted: int = 0
    files_affected: Tuple[str, ...] = ()
    committed_sha: str = ""         # new revert commit (revert mode)
    error: str = ""


# ---------------------------------------------------------------------------
# Git subprocess helper — synchronous, quote-safe
# ---------------------------------------------------------------------------


def _git(repo_root: Path, args: List[str], check: bool = False) -> subprocess.CompletedProcess:
    """Run ``git <args>`` inside ``repo_root`` and return the completed process.

    Synchronous by design — the slash-command handler is sync (see
    harness ``_handle_repl_command``) and asyncio is not needed for
    ~100ms git operations. Uses argument-array form, no shell.
    """
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=check,
        )
    except FileNotFoundError:
        # git not installed — bubble as a structured CompletedProcess.
        proc = subprocess.CompletedProcess(
            args=["git", *args], returncode=127,
            stdout="", stderr="git: command not found",
        )
        return proc


def _trim(s: str) -> str:
    return s.strip() if s else ""


# ---------------------------------------------------------------------------
# Planner — read-only plan construction
# ---------------------------------------------------------------------------


class UndoPlanner:
    """Builds an :class:`UndoPlan` for a requested ``/undo N``.

    The planner never mutates git state; it only reads commit metadata
    and inspects the working tree. All safety checks are pure-read so
    ``/undo preview N`` reuses the same code path.
    """

    def __init__(
        self,
        repo_root: Path,
        *,
        governed_loop_service: Any = None,
    ) -> None:
        self._repo_root = Path(repo_root).resolve()
        self._gls = governed_loop_service

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def plan(self, n: int, *, mode: str = "revert") -> UndoPlan:
        """Produce an :class:`UndoPlan` for reverting the last N commits.

        ``mode`` ∈ {"revert", "hard", "preview"}. "preview" behaves
        identically to "revert" for safety checking; ``UndoExecutor``
        short-circuits preview mode before mutation.
        """
        errors: List[str] = []
        warnings: List[str] = []

        if mode not in {"revert", "hard", "preview"}:
            errors.append(
                f"Unknown undo mode '{mode}' (expected revert|hard|preview)"
            )

        if not undo_enabled():
            errors.append(
                f"Undo disabled by env — set {_ENV_ENABLED}=1 to enable"
            )

        cap = max_batch()
        if n < 1:
            errors.append(f"N must be ≥ 1 (got {n})")
        elif n > cap:
            errors.append(
                f"N={n} exceeds safety cap (max={cap}; set "
                f"{_ENV_MAX_BATCH}=<higher> to raise)"
            )

        # Working-tree cleanliness — even preview warns here so the
        # operator doesn't confuse a dirty-tree dry run with a safe one.
        if not self._working_tree_clean():
            errors.append(
                "Working tree is not clean — commit or stash changes "
                "before running /undo"
            )

        # No in-flight ops. Preview doesn't need this, but we still
        # report it so the operator knows ops are racing the /undo.
        inflight = self._active_op_ids()
        if inflight:
            errors.append(
                f"{len(inflight)} op(s) in flight — /cancel them first: "
                f"{', '.join(inflight[:4])}"
                + (" …" if len(inflight) > 4 else "")
            )

        # Walk the last N commits and classify each.
        targets = self._load_last_n_commits(n)
        if len(targets) < n:
            errors.append(
                f"Only {len(targets)} commit(s) available, cannot undo "
                f"N={n}"
            )

        # Trailer gate — any non-O+V commit in the range is a hard fail.
        non_ov = [t for t in targets if not t.is_ov]
        if non_ov:
            first = non_ov[0]
            errors.append(
                f"Refusing to undo — commit {first.short_sha} "
                f"('{first.subject}') is not O+V (author={first.author_email}). "
                f"Use /undo N-M to stop above it."
            )

        # Upstream / pushed detection.
        pushed, upstream = self._branch_pushed_state()

        # --hard on pushed branch is a hard refusal. Revert on pushed
        # is allowed but gets a warning so operator knows a push follows.
        if mode == "hard" and pushed:
            errors.append(
                f"Refusing --hard: branch is pushed to {upstream}. "
                f"Use /undo N (revert commits preserve history)."
            )
        if mode == "revert" and pushed:
            warnings.append(
                f"Branch is pushed to {upstream} — revert commits will "
                f"need an explicit `git push` to propagate."
            )

        return UndoPlan(
            mode=mode,
            requested_n=n,
            targets=tuple(targets),
            safety_errors=tuple(errors),
            safety_warnings=tuple(warnings),
            branch_is_pushed=pushed,
            upstream_name=upstream,
        )

    # ------------------------------------------------------------------
    # Internals — each is best-effort + explicit about git failure shape
    # ------------------------------------------------------------------

    def _working_tree_clean(self) -> bool:
        proc = _git(self._repo_root, ["status", "--porcelain"])
        if proc.returncode != 0:
            # Can't tell — treat as dirty to fail closed.
            return False
        return _trim(proc.stdout) == ""

    def _active_op_ids(self) -> List[str]:
        gls = self._gls
        if gls is None:
            return []
        try:
            active = getattr(gls, "_active_ops", None) or set()
            return sorted(str(op) for op in active)
        except Exception:  # noqa: BLE001
            return []

    def _load_last_n_commits(self, n: int) -> List[UndoTarget]:
        """Fetch N most recent commits with full body + stat for classification."""
        if n < 1:
            return []
        # ``%H`` full SHA, ``%s`` subject, ``%ae`` author email,
        # ``%b`` body — separated by \x1f and commits by \x1e so we
        # can parse deterministically.
        proc = _git(
            self._repo_root,
            [
                "log",
                f"-{n}",
                "--no-merges",
                "--format=%H%x1f%ae%x1f%s%x1f%b%x1e",
            ],
        )
        if proc.returncode != 0:
            logger.debug(
                "[Undo] git log failed: %s", proc.stderr.strip(),
            )
            return []

        commits: List[UndoTarget] = []
        for raw in proc.stdout.split("\x1e"):
            # Only strip newlines, NOT the \x1f unit separator. Python's
            # str.strip() treats \x1f as whitespace and would truncate
            # trailing empty fields (e.g. empty commit bodies), shrinking
            # the split result from 4 to 3 and silently dropping those
            # commits. Using rstrip/lstrip on "\r\n" exclusively keeps
            # every record intact regardless of body presence.
            raw = raw.strip("\r\n")
            if not raw:
                continue
            parts = raw.split("\x1f", 3)
            # Pad in case body is missing entirely (defensive; the split
            # maxsplit=3 already guarantees ≤4 parts, but the record
            # could be malformed enough to yield fewer separators).
            while len(parts) < 4:
                parts.append("")
            sha, author_email, subject, body = parts[0], parts[1], parts[2], parts[3]
            if not sha:
                continue
            is_ov = _OV_TRAILER in body
            # Pull per-commit stats (files + insertions/deletions).
            files, ins, dele = self._commit_stats(sha)
            commits.append(UndoTarget(
                sha=sha,
                short_sha=sha[:10],
                subject=subject,
                is_ov=is_ov,
                author_email=author_email,
                files_changed=tuple(files),
                insertions=ins,
                deletions=dele,
            ))
        return commits

    def _commit_stats(self, sha: str) -> Tuple[List[str], int, int]:
        """Return (files_changed, insertions, deletions) for a commit."""
        proc = _git(
            self._repo_root,
            ["show", "--stat", "--format=", sha],
        )
        if proc.returncode != 0:
            return ([], 0, 0)
        files: List[str] = []
        insertions = 0
        deletions = 0
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            if " files changed" in line or " file changed" in line:
                # Summary: "3 files changed, 45 insertions(+), 12 deletions(-)"
                parts = line.split(",")
                for p in parts:
                    p = p.strip()
                    if p.endswith("insertions(+)") or p.endswith("insertion(+)"):
                        try:
                            insertions = int(p.split()[0])
                        except (ValueError, IndexError):
                            pass
                    elif p.endswith("deletions(-)") or p.endswith("deletion(-)"):
                        try:
                            deletions = int(p.split()[0])
                        except (ValueError, IndexError):
                            pass
            elif " | " in line:
                # Per-file: "path/to/file.py | 10 +-"
                path = line.split(" | ", 1)[0].strip()
                if path:
                    files.append(path)
        return (files, insertions, deletions)

    def _branch_pushed_state(self) -> Tuple[bool, str]:
        """Detect whether the current branch has an upstream + is pushed.

        Returns (pushed, upstream_name). ``pushed`` is True iff HEAD is
        reachable from the upstream ref (i.e. remote already has the
        commits we'd be undoing). ``upstream_name`` is "" when there's
        no upstream configured.
        """
        # Upstream ref, e.g. "origin/main".
        up = _git(
            self._repo_root,
            ["rev-parse", "--abbrev-ref", "@{u}"],
        )
        if up.returncode != 0:
            return (False, "")
        upstream = _trim(up.stdout)
        if not upstream:
            return (False, "")

        # HEAD reachable from upstream iff `git merge-base --is-ancestor
        # HEAD @{u}` returns 0 — i.e. the remote contains every local commit.
        ancestor = _git(
            self._repo_root,
            ["merge-base", "--is-ancestor", "HEAD", "@{u}"],
        )
        pushed = ancestor.returncode == 0
        return (pushed, upstream)


# ---------------------------------------------------------------------------
# Executor — performs the git mutation
# ---------------------------------------------------------------------------


class UndoExecutor:
    """Applies an :class:`UndoPlan` to the repository.

    Called by the slash-command handler only when
    ``plan.is_safe == True``. Never mutates in preview mode.
    """

    def __init__(
        self,
        repo_root: Path,
        *,
        comm: Any = None,
    ) -> None:
        self._repo_root = Path(repo_root).resolve()
        self._comm = comm

    async def execute(self, plan: UndoPlan) -> UndoResult:
        """Execute ``plan`` and return an :class:`UndoResult`.

        ``async`` so the caller can ``await`` us from the REPL loop
        (which may want to schedule ``emit_decision`` on the comm bus).
        The git subprocess calls themselves are synchronous — they
        complete in ~100ms each.
        """
        if plan.mode == "preview":
            return UndoResult(
                executed=False, mode="preview",
                n_reverted=0, files_affected=plan.unique_files,
            )
        if not plan.is_safe:
            return UndoResult(
                executed=False, mode=plan.mode,
                error="plan has unresolved safety errors",
            )
        if not plan.targets:
            return UndoResult(
                executed=False, mode=plan.mode,
                error="no targets to undo",
            )

        if plan.mode == "hard":
            return await self._execute_hard(plan)
        return await self._execute_revert(plan)

    # ------------------------------------------------------------------
    # Revert mode (default) — preserves history with new revert commits
    # ------------------------------------------------------------------

    async def _execute_revert(self, plan: UndoPlan) -> UndoResult:
        n = len(plan.targets)
        # Targets come newest→oldest from git log; revert consumes them
        # in that same order (latest first) so each revert's context is
        # the preceding commit, matching how `git revert HEAD..HEAD~N`
        # works for a batch.
        shas = [t.sha for t in plan.targets]

        # --no-commit + batch: we stage every revert's diff, then craft
        # one O+V-signed revert commit ourselves.
        for sha in shas:
            proc = _git(
                self._repo_root,
                ["revert", "--no-commit", sha],
            )
            if proc.returncode != 0:
                # Abort the whole batch — leave working tree as it was.
                _abort = _git(self._repo_root, ["revert", "--abort"])
                return UndoResult(
                    executed=False, mode="revert",
                    error=(
                        f"git revert failed on {sha[:10]}: "
                        f"{proc.stderr.strip() or proc.stdout.strip()} "
                        f"(abort: {_abort.returncode})"
                    ),
                )

        # Compose the single revert commit body.
        subject = f"Revert: undo last {n} autonomous commit{'s' if n != 1 else ''}"
        body_lines: List[str] = [
            "",
            f"Operator-triggered /undo {n} — reverts the following O+V commits:",
            "",
        ]
        for t in plan.targets:
            body_lines.append(f"  * {t.short_sha} {t.subject}")
        body_lines += [
            "",
            "Ouroboros+Venom [O+V] — Autonomous Self-Development Engine",
            _OV_TRAILER,
        ]
        msg = subject + "\n" + "\n".join(body_lines)

        commit_proc = _git(
            self._repo_root,
            ["commit", "-m", msg],
        )
        if commit_proc.returncode != 0:
            # Rare — revert staged but commit failed. Try to clean up.
            _abort = _git(self._repo_root, ["revert", "--abort"])
            return UndoResult(
                executed=False, mode="revert",
                error=(
                    f"git commit failed: "
                    f"{commit_proc.stderr.strip() or commit_proc.stdout.strip()} "
                    f"(abort: {_abort.returncode})"
                ),
            )

        # Resolve the new revert commit's SHA.
        sha_proc = _git(self._repo_root, ["rev-parse", "HEAD"])
        new_sha = _trim(sha_proc.stdout) if sha_proc.returncode == 0 else ""

        # Emit session event (best-effort).
        await self._emit_decision(
            n=n, mode="revert", new_sha=new_sha,
            files=plan.unique_files,
        )

        logger.info(
            "[Undo] reverted=%d files=%d method=revert pushed=%s new_sha=%s",
            n, len(plan.unique_files), plan.branch_is_pushed, new_sha[:10],
        )

        return UndoResult(
            executed=True, mode="revert",
            n_reverted=n, files_affected=plan.unique_files,
            committed_sha=new_sha,
        )

    # ------------------------------------------------------------------
    # Hard mode — reset --hard (history rewrite, unpushed only)
    # ------------------------------------------------------------------

    async def _execute_hard(self, plan: UndoPlan) -> UndoResult:
        n = len(plan.targets)
        proc = _git(
            self._repo_root,
            ["reset", "--hard", f"HEAD~{n}"],
        )
        if proc.returncode != 0:
            return UndoResult(
                executed=False, mode="hard",
                error=(
                    f"git reset failed: "
                    f"{proc.stderr.strip() or proc.stdout.strip()}"
                ),
            )

        await self._emit_decision(
            n=n, mode="hard", new_sha="",
            files=plan.unique_files,
        )

        logger.info(
            "[Undo] reverted=%d files=%d method=hard pushed=%s",
            n, len(plan.unique_files), plan.branch_is_pushed,
        )

        return UndoResult(
            executed=True, mode="hard",
            n_reverted=n, files_affected=plan.unique_files,
        )

    # ------------------------------------------------------------------
    # Event emission — CommProtocol DECISION with outcome="undo"
    # ------------------------------------------------------------------

    async def _emit_decision(
        self,
        *,
        n: int,
        mode: str,
        new_sha: str,
        files: Tuple[str, ...],
    ) -> None:
        if self._comm is None:
            return
        try:
            emit = getattr(self._comm, "emit_decision", None)
            if emit is None:
                return
            await emit(
                op_id=f"undo-{new_sha[:10] if new_sha else 'hard'}-n{n}",
                outcome="undo",
                reason_code=f"user_undo_n={n}_mode={mode}",
                target_files=list(files),
            )
        except Exception:  # noqa: BLE001
            logger.debug(
                "[Undo] emit_decision failed; continuing", exc_info=True,
            )


# ---------------------------------------------------------------------------
# Plan rendering — Rich output for SerpentFlow
# ---------------------------------------------------------------------------


def render_plan(plan: UndoPlan) -> Any:
    """Return a Rich renderable summarising ``plan`` for the operator.

    Used by both ``/undo`` (pre-execute confirmation banner) and
    ``/undo preview N`` (dry-run report). Falls back to a plain string
    when Rich isn't importable — the REPL's ``console.print`` handles
    both shapes.
    """
    try:
        from rich.console import Group
        from rich.panel import Panel
        from rich.table import Table
        from rich.text import Text
    except Exception:  # noqa: BLE001
        return _render_plan_plain(plan)

    mode_color = {
        "revert": "yellow", "hard": "red", "preview": "cyan",
    }.get(plan.mode, "white")
    title = Text()
    title.append("/undo", style="bold")
    title.append(f"  mode={plan.mode}", style=mode_color)
    title.append(f"  n={plan.requested_n}")
    if plan.branch_is_pushed:
        title.append(
            f"  (pushed to {plan.upstream_name})", style="dim",
        )

    lines: List[Any] = [title]

    if plan.targets:
        table = Table(
            show_header=True,
            header_style="bold cyan",
            padding=(0, 1),
        )
        table.add_column("#", justify="right")
        table.add_column("sha", no_wrap=True)
        table.add_column("kind")
        table.add_column("subject")
        table.add_column("files", justify="right")
        table.add_column("+/-", justify="right")
        for idx, t in enumerate(plan.targets, 1):
            kind_style = "green" if t.is_ov else "red"
            kind_txt = "O+V" if t.is_ov else "manual"
            table.add_row(
                str(idx),
                t.short_sha,
                Text(kind_txt, style=kind_style),
                t.subject[:70],
                str(len(t.files_changed)),
                Text(
                    f"+{t.insertions}/-{t.deletions}",
                    style="dim",
                ),
            )
        lines.append(table)

    if plan.safety_warnings:
        warn_txt = Text()
        for w in plan.safety_warnings:
            warn_txt.append(f"⚠ {w}\n", style="yellow")
        lines.append(warn_txt)

    if plan.safety_errors:
        err_txt = Text()
        for e in plan.safety_errors:
            err_txt.append(f"✖ {e}\n", style="bold red")
        lines.append(err_txt)
    else:
        ready = Text()
        if plan.mode == "preview":
            ready.append("(preview — no changes will be made)", style="cyan")
        else:
            ready.append("✓ ready to execute", style="bold green")
        lines.append(ready)

    return Panel(
        Group(*lines),
        title=f"[bold]{'Preview ' if plan.mode == 'preview' else ''}Undo Plan[/bold]",
        border_style=mode_color,
        padding=(1, 2),
    )


def _render_plan_plain(plan: UndoPlan) -> str:
    """Rich-free fallback — always returns a string."""
    lines: List[str] = []
    lines.append(
        f"/undo  mode={plan.mode}  n={plan.requested_n}  "
        f"pushed={plan.branch_is_pushed}  upstream={plan.upstream_name or 'none'}"
    )
    for idx, t in enumerate(plan.targets, 1):
        tag = "O+V" if t.is_ov else "manual"
        lines.append(
            f"  {idx:>2}. {t.short_sha} [{tag}] {t.subject}  "
            f"(+{t.insertions}/-{t.deletions}, files={len(t.files_changed)})"
        )
    for w in plan.safety_warnings:
        lines.append(f"  warn: {w}")
    for e in plan.safety_errors:
        lines.append(f"  ERROR: {e}")
    if plan.is_safe:
        lines.append(
            "  (preview — no changes)"
            if plan.mode == "preview"
            else "  ready to execute"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Command-line parse helper — ``/undo [preview|--hard] [N]``
# ---------------------------------------------------------------------------


def parse_undo_args(raw: str) -> Tuple[int, str, Optional[str]]:
    """Parse ``/undo`` argument tail into ``(n, mode, error)``.

    Examples
    --------
    ``/undo``              → ``(1, "revert", None)``
    ``/undo 3``            → ``(3, "revert", None)``
    ``/undo preview``      → ``(1, "preview", None)``
    ``/undo preview 5``    → ``(5, "preview", None)``
    ``/undo --hard 2``     → ``(2, "hard", None)``
    ``/undo garbage``      → ``(0, "revert", "could not parse N")``
    """
    tokens = shlex.split((raw or "").strip())
    # Drop the leading "/undo" or "undo" token if present.
    if tokens and tokens[0].lstrip("/") == "undo":
        tokens = tokens[1:]

    mode = "revert"
    n_str: Optional[str] = None
    for tok in tokens:
        if tok in ("preview", "--preview", "-p"):
            mode = "preview"
        elif tok in ("--hard", "-H"):
            mode = "hard"
        elif tok.lstrip("-").isdigit():
            n_str = tok
        else:
            return (0, mode, f"unknown token '{tok}'")

    if n_str is None:
        return (1, mode, None)
    try:
        n = int(n_str)
    except ValueError:
        return (0, mode, f"N must be an integer (got '{n_str}')")
    if n < 1:
        return (0, mode, f"N must be ≥ 1 (got {n})")
    return (n, mode, None)
