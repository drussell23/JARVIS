"""P3 Slice 3 — Inline approval CLI renderer + 30s prompt + ``$EDITOR``.

Owns the I/O surface for the inline approval UX (Slice 1 primitive +
Slice 2 provider). PRD §9 Phase 3 P3 spec:

  > Show full diff in terminal with hunks
  > Prompt: ``[y]es / [n]o / [s]how stack / [e]dit / [w]ait`` with
  > 30s default timeout
  > On ``y``: apply
  > On ``e``: open in ``$EDITOR``, then re-prompt

This module is the **pure-renderer + prompt loop** layer. It DOES NOT
touch the orchestrator or the FSM — its only authority is talking to
the operator and shelling out to ``$EDITOR`` for file edits. Slice 4
adds the SerpentFlow wiring + master-flag flip.

Authority invariants (PRD §12.2 — Slice 3 widens the I/O surface
relative to Slices 1+2 because rendering needs git + ``$EDITOR``):
  * Allowed I/O: ``subprocess.run`` for ``git diff`` and ``$EDITOR``;
    ``select.select`` on the stdin file descriptor for the 30s timeout.
  * Banned: orchestrator / policy / iron_gate / change_engine /
    candidate_generator / gate / semantic_guardian / risk_tier imports.
  * Banned: shelled-out subprocess (only argv-form).
  * Banned: ``os.environ[`` writes; reads are fine (``$EDITOR``,
    ``$VISUAL``).
  * Best-effort: every prompt failure / EOF / timeout returns the
    safety-first ``WAIT`` choice (mirrors Slice 1's
    ``parse_decision_input`` contract). The loop never auto-approves
    on a degraded prompt.
  * The renderer is **dormant** while
    ``JARVIS_APPROVAL_UX_INLINE_ENABLED`` is false (Slice 4 flips it).
    Nothing in this module reads the flag — gating happens at the
    caller (Slice 4 SerpentFlow wiring) so renderer functions stay
    pure + testable.
"""
from __future__ import annotations

import logging
import os
import select
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import IO, Iterable, Optional, Sequence

from backend.core.ouroboros.governance.approval_provider import (
    ApprovalResult,
    ApprovalStatus,
)
from backend.core.ouroboros.governance.inline_approval import (
    InlineApprovalChoice,
    InlineApprovalRequest,
    decision_timeout_s,
    parse_decision_input,
)

logger = logging.getLogger(__name__)


# Default git-diff timeout. Cap at 5s so a hung git call never blocks
# the operator prompt.
DIFF_SUBPROCESS_TIMEOUT_S: float = 5.0

# Default editor timeout. ``$EDITOR`` is interactive — operators may
# spend minutes inspecting; cap is generous to avoid surprise kills.
EDITOR_SUBPROCESS_TIMEOUT_S: float = 1800.0

# Maximum diff bytes shown in the prompt. Anything larger gets
# truncated with a ``... <N more lines truncated ...>`` footer so the
# terminal doesn't choke on a 50MB generated change.
MAX_DIFF_BYTES: int = 64 * 1024  # 64 KiB

# Prompt label per PRD spec (kept as a module constant so tests can
# assert verbatim shape).
PROMPT_LABEL: str = "[y]es / [n]o / [s]how stack / [e]dit / [w]ait"


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_request_block(
    request: InlineApprovalRequest,
    diff_text: str = "",
) -> str:
    """Return the full prompt block as ASCII text.

    Layout (matches PRD §9 P3 spec)::

        ============================================================
        [INLINE APPROVAL] op_id=<...> tier=<...>
        Files (<N>):
          - path/one.py
          - path/two.py
        Summary: <description>
        ----------------------------------------------------------
        <diff hunks ... truncated to MAX_DIFF_BYTES>
        ----------------------------------------------------------
        Choose: [y]es / [n]o / [s]how stack / [e]dit / [w]ait
        (auto-WAIT in <Xs>):
    """
    files = request.target_files or ()
    bullet_files = "\n".join(f"  - {p}" for p in files) or "  (none)"
    secs_left = max(0, int(request.seconds_remaining()))
    diff_block = _truncate_diff(diff_text)

    parts = [
        "=" * 60,
        f"[INLINE APPROVAL] op_id={request.op_id} tier={request.risk_tier}",
        f"Files ({len(files)}):",
        bullet_files,
        f"Summary: {request.diff_summary or '(no summary)'}",
        "-" * 58,
        diff_block if diff_block else "(no diff captured)",
        "-" * 58,
        f"Choose: {PROMPT_LABEL}",
        f"(auto-WAIT in {secs_left}s):",
    ]
    return "\n".join(parts)


def _truncate_diff(diff_text: str) -> str:
    """Trim oversize diffs so the prompt stays terminal-friendly."""
    if not diff_text:
        return ""
    encoded = diff_text.encode("utf-8", errors="replace")
    if len(encoded) <= MAX_DIFF_BYTES:
        return diff_text
    head = encoded[:MAX_DIFF_BYTES].decode("utf-8", errors="replace")
    extra_lines = diff_text.count("\n") - head.count("\n")
    return (
        f"{head}\n"
        f"... <{extra_lines} more lines truncated ...>"
    )


def compute_diff_text(
    target_files: Sequence[str],
    repo_root: Optional[Path] = None,
    timeout_s: float = DIFF_SUBPROCESS_TIMEOUT_S,
) -> str:
    """Best-effort ``git diff`` capture for the target file set.

    Returns ``""`` on any subprocess error; never raises. ``argv`` form
    only — never ``shell=True``."""
    if not target_files:
        return ""
    cwd = str(repo_root) if repo_root is not None else None
    argv = ["git", "diff", "--no-color", "--", *target_files]
    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("[InlineApprovalRenderer] git diff failed: %s", exc)
        return ""
    if proc.returncode not in (0, 1):
        # 0 = no diff, 1 = diff present; anything else = error.
        logger.warning(
            "[InlineApprovalRenderer] git diff rc=%d stderr=%r",
            proc.returncode, proc.stderr[:200],
        )
        return ""
    return proc.stdout or ""


# ---------------------------------------------------------------------------
# Prompt loop
# ---------------------------------------------------------------------------


def prompt_decision(
    stream_in: Optional[IO[str]] = None,
    timeout_s: Optional[float] = None,
    now_fn=time.monotonic,
) -> InlineApprovalChoice:
    """Read one decision line from ``stream_in`` with a hard timeout.

    Returns:
      * ``InlineApprovalChoice`` for parsed input.
      * ``InlineApprovalChoice.TIMEOUT_DEFERRED`` when the timeout
        expires without input (caller surfaces this to the audit
        ledger via ``mark_timeout``).
      * ``InlineApprovalChoice.WAIT`` on EOF, garbage, or any I/O
        failure — safety-first default mirrors
        :func:`parse_decision_input`.

    The 30s timeout is wall-clock; uses ``select.select`` on the
    stream's file descriptor (POSIX). When ``stream_in`` lacks a real
    fd (StringIO in tests / non-tty), falls back to a simple readline
    so unit tests stay deterministic without monkey-patching ``select``.
    """
    if stream_in is None:
        stream_in = sys.stdin
    if timeout_s is None:
        timeout_s = decision_timeout_s()

    fileno = _safe_fileno(stream_in)
    if fileno is None:
        # No real file descriptor (e.g. StringIO) — fall back to a
        # blocking readline. Tests inject pre-loaded buffers so this
        # path is deterministic.
        return _read_one_line_or_safe(stream_in)

    deadline = now_fn() + timeout_s
    while True:
        remaining = deadline - now_fn()
        if remaining <= 0:
            return InlineApprovalChoice.TIMEOUT_DEFERRED
        try:
            ready, _, _ = select.select([fileno], [], [], remaining)
        except (OSError, ValueError) as exc:
            logger.warning(
                "[InlineApprovalRenderer] select failed: %s", exc,
            )
            return InlineApprovalChoice.WAIT
        if not ready:
            return InlineApprovalChoice.TIMEOUT_DEFERRED
        return _read_one_line_or_safe(stream_in)


def _safe_fileno(stream: IO[str]) -> Optional[int]:
    try:
        fno = stream.fileno()
        # Only accept real OS-level descriptors. StringIO raises here.
        return fno if isinstance(fno, int) and fno >= 0 else None
    except (AttributeError, OSError, ValueError):
        return None


def _read_one_line_or_safe(stream: IO[str]) -> InlineApprovalChoice:
    """Read exactly one line; map empty/EOF/garbage → WAIT."""
    try:
        line = stream.readline()
    except (OSError, ValueError) as exc:
        logger.warning("[InlineApprovalRenderer] readline failed: %s", exc)
        return InlineApprovalChoice.WAIT
    if not line:
        # EOF — mirrors Slice 1 contract: no input ≠ approval.
        return InlineApprovalChoice.WAIT
    return parse_decision_input(line)


# ---------------------------------------------------------------------------
# $EDITOR shell-out
# ---------------------------------------------------------------------------


def resolve_editor() -> Optional[Sequence[str]]:
    """Return the editor argv (list form) or ``None`` when unset.

    Reads ``$EDITOR`` first, then ``$VISUAL``. Splits the value with
    :func:`shlex.split` so ``EDITOR='code -w'`` works without shell
    invocation. Returns ``None`` when both are unset/empty."""
    raw = os.environ.get("EDITOR") or os.environ.get("VISUAL")
    if not raw or not raw.strip():
        return None
    try:
        argv = shlex.split(raw)
    except ValueError as exc:
        logger.warning(
            "[InlineApprovalRenderer] EDITOR not parseable: %s", exc,
        )
        return None
    return argv if argv else None


def open_editor(
    file_path: str,
    timeout_s: float = EDITOR_SUBPROCESS_TIMEOUT_S,
) -> bool:
    """Open ``file_path`` in ``$EDITOR``. Returns True on rc 0.

    Never uses ``shell=True``. ``$EDITOR=''`` returns False without
    side effect. The path is passed argv-form so embedded spaces /
    quotes are handled by the OS, not a shell."""
    argv = resolve_editor()
    if argv is None:
        logger.warning(
            "[InlineApprovalRenderer] no $EDITOR / $VISUAL set; skipping edit",
        )
        return False
    full_argv = [*argv, file_path]
    try:
        proc = subprocess.run(
            full_argv,
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning(
            "[InlineApprovalRenderer] $EDITOR invocation failed: %s", exc,
        )
        return False
    return proc.returncode == 0


# ---------------------------------------------------------------------------
# Stack rendering (for SHOW_STACK)
# ---------------------------------------------------------------------------


def render_pending_stack(
    pending: Iterable[InlineApprovalRequest],
    now_unix: Optional[float] = None,
) -> str:
    """Render the queue stack one-per-line for the [s]how-stack command.

    Format::

        Pending (N):
          1. <op_id> tier=<...> files=<N> in <Xs>
          2. <op_id> ...
    """
    items = list(pending)
    if not items:
        return "Pending (0): (queue empty)"
    lines = [f"Pending ({len(items)}):"]
    for i, req in enumerate(items, 1):
        secs = max(0, int(req.seconds_remaining(now_unix=now_unix)))
        files_n = len(req.target_files or ())
        lines.append(
            f"  {i}. {req.op_id} tier={req.risk_tier} "
            f"files={files_n} in {secs}s",
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Loop orchestration
# ---------------------------------------------------------------------------


def run_inline_approval_loop(
    provider,
    request: InlineApprovalRequest,
    *,
    diff_text: str = "",
    pending_stack: Iterable[InlineApprovalRequest] = (),
    stream_in: Optional[IO[str]] = None,
    stream_out: Optional[IO[str]] = None,
    timeout_s: Optional[float] = None,
    operator: str = "operator",
    max_iterations: int = 6,
    edit_target: Optional[str] = None,
    editor_invoker=open_editor,
) -> ApprovalResult:
    """Render the prompt and process operator decisions until terminal.

    Behaviour per PRD §9 P3:
      * Show diff + prompt.
      * ``y`` → ``provider.approve(request_id, operator)`` → return.
      * ``n`` → ``provider.reject(request_id, operator, reason)`` →
        return. Reason is the literal string ``"inline reject"`` since
        Slice 3 doesn't ask for free-text follow-up (Slice 4 may add
        a one-line reason capture).
      * ``s`` → print pending stack, re-prompt.
      * ``e`` → ``$EDITOR`` ``edit_target`` (defaults to first target
        file); on any outcome, re-prompt.
      * ``w`` / ``TIMEOUT_DEFERRED`` → ``provider.await_decision(...,
        0.0)`` → returns EXPIRED so the FSM falls back to the existing
        Orange-PR async path.

    ``max_iterations`` bounds re-prompts (SHOW_STACK/EDIT cycles) so
    a stuck operator can't pin a worker thread. Excess iterations
    return EXPIRED (same as TIMEOUT_DEFERRED — defers to async path).

    All provider calls go through the standard ``ApprovalProvider``
    Protocol — the loop never reaches into provider internals.
    """
    if stream_out is None:
        stream_out = sys.stdout

    for _ in range(max(1, int(max_iterations))):
        block = render_request_block(request, diff_text=diff_text)
        _safe_print(stream_out, block)

        choice = prompt_decision(
            stream_in=stream_in,
            timeout_s=timeout_s,
        )

        if choice is InlineApprovalChoice.APPROVE:
            return _await_now(
                provider.approve(request.request_id, operator),
            )
        if choice is InlineApprovalChoice.REJECT:
            return _await_now(
                provider.reject(
                    request.request_id, operator, "inline reject",
                ),
            )
        if choice is InlineApprovalChoice.SHOW_STACK:
            _safe_print(
                stream_out,
                render_pending_stack(pending_stack),
            )
            continue
        if choice is InlineApprovalChoice.EDIT:
            target = edit_target or (
                request.target_files[0] if request.target_files else ""
            )
            if target:
                editor_invoker(target)
            else:
                _safe_print(stream_out, "(no file to edit)")
            continue
        # WAIT / TIMEOUT_DEFERRED → defer to async path.
        return _await_now(
            provider.await_decision(request.request_id, 0.0),
        )

    # Exhausted re-prompts — defer to async path.
    _safe_print(
        stream_out,
        f"(max {max_iterations} re-prompts reached; deferring)",
    )
    return _await_now(
        provider.await_decision(request.request_id, 0.0),
    )


def _safe_print(stream_out: IO[str], text: str) -> None:
    """Best-effort write; swallows errors so a broken pipe never raises."""
    try:
        stream_out.write(text + "\n")
        flush = getattr(stream_out, "flush", None)
        if callable(flush):
            flush()
    except (OSError, ValueError):
        pass


def _await_now(coro_or_result):
    """Helper: bridge sync loop ↔ async provider.

    The provider methods are async; we await them via
    :func:`asyncio.get_event_loop` ``run_until_complete`` when a coroutine
    is returned. When already running inside an event loop, callers
    should use the async variant (Slice 4 wiring lands an async
    sibling). For Slice 3 the sync entry point is enough — every
    test path injects an awaitable-or-direct fake provider."""
    import asyncio
    import inspect

    if inspect.iscoroutine(coro_or_result):
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        if loop.is_running():
            # Cannot run_until_complete on a running loop. Caller must
            # use an async wrapper. Surface as EXPIRED so the FSM falls
            # back to the Orange-PR path.
            logger.warning(
                "[InlineApprovalRenderer] running inside event loop; "
                "deferring to async path",
            )
            return ApprovalResult(
                status=ApprovalStatus.EXPIRED,
                approver=None,
                reason="loop_already_running",
                decided_at=None,
                request_id="",
            )
        return loop.run_until_complete(coro_or_result)
    return coro_or_result


__all__ = [
    "DIFF_SUBPROCESS_TIMEOUT_S",
    "EDITOR_SUBPROCESS_TIMEOUT_S",
    "MAX_DIFF_BYTES",
    "PROMPT_LABEL",
    "compute_diff_text",
    "open_editor",
    "prompt_decision",
    "render_pending_stack",
    "render_request_block",
    "resolve_editor",
    "run_inline_approval_loop",
]
