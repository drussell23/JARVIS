"""PlanApproval REPL dispatcher — Slice 3 of problem #7.

Provides a pure, importable dispatcher for ``/plan`` subcommands
over :class:`PlanApprovalController`. Designed to plug into
SerpentFlow's slash-command path as one callable:

    from backend.core.ouroboros.governance.plan_approval_repl import (
        dispatch_plan_command,
    )
    result = dispatch_plan_command(line)
    if result is not None:
        console.print(result.text)

...but equally usable from any REPL or smoke test because the
dispatcher is stateless apart from the controller singleton.

Commands
--------

  /plan mode                 → show current mode state
  /plan mode on              → enable plan approval mode
  /plan mode off             → disable plan approval mode
  /plan pending              → list pending plans (one line per op)
  /plan show <op-id>         → render one plan's full detail
  /plan approve <op-id>      → approve the plan
  /plan reject <op-id> <why> → reject with reason (required)
  /plan history [N]          → last N terminal decisions (default 10)
  /plan help                 → print this help

All commands return a :class:`PlanDispatchResult` — text plus an
``ok`` flag for scripting. Rich markup is embedded in the text so
SerpentFlow's console can render colors; plain-text consumers
strip it trivially.

## Authority posture

- Read + mutate restricted to the operator's session. No network
  side-effects. No orchestrator hooks.
- The *approve/reject* commands are the ONLY way human action
  moves from pending to terminal state via the REPL; this mirrors
  the IDE's authority design (read-only over HTTP, approval
  explicitly via REPL or a future authenticated endpoint).
"""
from __future__ import annotations

import json
import os
import shlex
import textwrap
import time
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from backend.core.ouroboros.governance.plan_approval import (
    PlanApprovalController,
    PlanApprovalStateError,
    STATE_APPROVED,
    STATE_EXPIRED,
    STATE_PENDING,
    STATE_REJECTED,
    get_default_controller,
)


# --- Result dataclass ------------------------------------------------------


@dataclass
class PlanDispatchResult:
    """Return from every dispatch call.

    Attributes
    ----------
    ok : bool
        True on success, False on user-error (bad args, unknown
        op_id, etc.) or controller-error (state mismatch).
    text : str
        Human-readable message. May contain Rich markup.
    matched : bool
        True if the line was recognized as a ``/plan`` command
        (even if it later failed). False means the caller should
        fall through to the next handler.
    """

    ok: bool
    text: str
    matched: bool = True


# --- Entry point -----------------------------------------------------------


def dispatch_plan_command(
    line: str,
    controller: Optional[PlanApprovalController] = None,
    *,
    reviewer: str = "repl",
) -> PlanDispatchResult:
    """Parse + dispatch one REPL input line.

    Recognizes ``/plan ...`` prefixes. Returns ``matched=False``
    when the line isn't a plan command — callers should continue
    down the normal dispatch chain.
    """
    if not isinstance(line, str):
        return PlanDispatchResult(ok=False, text="(not a string)", matched=False)
    stripped = line.strip()
    if not stripped.startswith("/plan"):
        return PlanDispatchResult(ok=False, text="", matched=False)

    try:
        parts = shlex.split(stripped)
    except ValueError as exc:
        return PlanDispatchResult(
            ok=False, text="[red]Malformed /plan line: %s[/red]" % exc,
        )
    assert parts[0] == "/plan"
    args = parts[1:]
    ctrl = controller or get_default_controller()

    if not args or args[0] == "help":
        return PlanDispatchResult(ok=True, text=_render_help())

    sub = args[0]
    rest = args[1:]
    handler = _HANDLERS.get(sub)
    if handler is None:
        return PlanDispatchResult(
            ok=False,
            text=(
                "[red]Unknown /plan subcommand: %s[/red]  "
                "([dim]try [bold]/plan help[/bold][/dim])"
            ) % sub,
        )
    return handler(ctrl, rest, reviewer)


# --- Subcommand handlers ---------------------------------------------------


def _cmd_mode(
    controller: PlanApprovalController,
    args: Sequence[str],
    reviewer: str,
) -> PlanDispatchResult:
    """/plan mode [on|off]."""
    env = os.environ.get("JARVIS_PLAN_APPROVAL_MODE", "false")
    current = env.strip().lower() == "true"
    if not args:
        state = "[green]ON[/green]" if current else "[dim]OFF[/dim]"
        pending = controller.pending_count
        return PlanDispatchResult(
            ok=True,
            text=(
                "[bold]Plan approval mode:[/bold] %s\n"
                "Pending plans: %d\n"
                "[dim]Set JARVIS_PLAN_APPROVAL_MODE=true to halt every op "
                "for review.[/dim]"
            ) % (state, pending),
        )
    toggle = args[0].lower()
    if toggle == "on":
        os.environ["JARVIS_PLAN_APPROVAL_MODE"] = "true"
        return PlanDispatchResult(
            ok=True,
            text="[green]Plan approval mode ENABLED[/green]  "
            "[dim](next op with a plan will halt for review)[/dim]",
        )
    if toggle == "off":
        os.environ["JARVIS_PLAN_APPROVAL_MODE"] = "false"
        return PlanDispatchResult(
            ok=True,
            text="[yellow]Plan approval mode DISABLED[/yellow]  "
            "[dim](complex ops still gated via the complexity "
            "heuristic)[/dim]",
        )
    return PlanDispatchResult(
        ok=False,
        text="[red]/plan mode takes 'on' or 'off'[/red]",
    )


def _cmd_pending(
    controller: PlanApprovalController,
    args: Sequence[str],
    reviewer: str,
) -> PlanDispatchResult:
    """/plan pending — list pending op_ids."""
    pending_snapshots = [
        s for s in controller.snapshot_all()
        if s["state"] == STATE_PENDING
    ]
    if not pending_snapshots:
        return PlanDispatchResult(
            ok=True,
            text="[dim]No pending plans.[/dim]",
        )
    lines = [
        "[bold]%d pending plan%s[/bold]" % (
            len(pending_snapshots),
            "" if len(pending_snapshots) == 1 else "s",
        ),
    ]
    for s in pending_snapshots:
        remaining = max(0.0, s["expires_ts"] - time.monotonic())
        approach = s["plan"].get("approach") or s["plan"].get(
            "description", ""
        )
        preview = (approach or "(no approach)")[:60]
        lines.append(
            "  [cyan]%s[/cyan]  [dim]expires in %ds[/dim]  %s" % (
                s["op_id"], int(remaining), preview,
            )
        )
    lines.append(
        "\n[dim]Commands: /plan show <op-id> · "
        "/plan approve <op-id> · /plan reject <op-id> <reason>[/dim]"
    )
    return PlanDispatchResult(ok=True, text="\n".join(lines))


def _cmd_show(
    controller: PlanApprovalController,
    args: Sequence[str],
    reviewer: str,
) -> PlanDispatchResult:
    """/plan show <op-id>."""
    if not args:
        return PlanDispatchResult(
            ok=False,
            text="[red]/plan show requires an op-id[/red]",
        )
    op_id = args[0]
    snap = controller.snapshot(op_id)
    if snap is None:
        return PlanDispatchResult(
            ok=False,
            text="[red]No plan registered for op=%s[/red]" % op_id,
        )
    return PlanDispatchResult(ok=True, text=render_plan_detail(snap))


def _cmd_approve(
    controller: PlanApprovalController,
    args: Sequence[str],
    reviewer: str,
) -> PlanDispatchResult:
    """/plan approve <op-id>."""
    if not args:
        return PlanDispatchResult(
            ok=False,
            text="[red]/plan approve requires an op-id[/red]",
        )
    op_id = args[0]
    try:
        outcome = controller.approve(op_id, reviewer=reviewer)
    except PlanApprovalStateError as exc:
        return PlanDispatchResult(
            ok=False,
            text="[red]Cannot approve %s: %s[/red]" % (op_id, exc),
        )
    return PlanDispatchResult(
        ok=True,
        text=(
            "[green]✓ Plan APPROVED for %s[/green]  "
            "[dim](reviewer=%s, elapsed=%.1fs)[/dim]"
        ) % (op_id, outcome.reviewer, outcome.elapsed_s),
    )


def _cmd_reject(
    controller: PlanApprovalController,
    args: Sequence[str],
    reviewer: str,
) -> PlanDispatchResult:
    """/plan reject <op-id> <reason...>."""
    if len(args) < 2:
        return PlanDispatchResult(
            ok=False,
            text=(
                "[red]/plan reject requires <op-id> and a reason[/red]  "
                "[dim](rejection reason is mandatory — it feeds back "
                "into future PLAN attempts)[/dim]"
            ),
        )
    op_id = args[0]
    reason = " ".join(args[1:])
    try:
        outcome = controller.reject(op_id, reason=reason, reviewer=reviewer)
    except PlanApprovalStateError as exc:
        return PlanDispatchResult(
            ok=False,
            text="[red]Cannot reject %s: %s[/red]" % (op_id, exc),
        )
    return PlanDispatchResult(
        ok=True,
        text=(
            "[yellow]✗ Plan REJECTED for %s[/yellow]  "
            "[dim](reviewer=%s, reason=%s)[/dim]"
        ) % (op_id, outcome.reviewer, outcome.reason),
    )


def _cmd_history(
    controller: PlanApprovalController,
    args: Sequence[str],
    reviewer: str,
) -> PlanDispatchResult:
    """/plan history [N]. Shows last N resolved plans."""
    try:
        limit = int(args[0]) if args else 10
    except ValueError:
        return PlanDispatchResult(
            ok=False,
            text="[red]/plan history takes an integer limit[/red]",
        )
    limit = max(1, min(limit, 500))
    history = controller.history()
    recent = history[-limit:]
    if not recent:
        return PlanDispatchResult(
            ok=True, text="[dim]No resolved plans yet.[/dim]",
        )
    lines = [
        "[bold]Last %d resolved plan%s[/bold]" % (
            len(recent), "" if len(recent) == 1 else "s",
        ),
    ]
    for row in recent:
        state = row["state"]
        color = {
            STATE_APPROVED: "green",
            STATE_REJECTED: "yellow",
            STATE_EXPIRED: "red",
        }.get(state, "dim")
        reason_suffix = (
            "  [dim](%s)[/dim]" % row["reason"]
            if row.get("reason") else ""
        )
        lines.append(
            "  [%s]%-9s[/%s]  %s  [dim]%.1fs  by %s[/dim]%s" % (
                color, state, color,
                row["op_id"], row["elapsed_s"],
                row.get("reviewer", "?"), reason_suffix,
            )
        )
    return PlanDispatchResult(ok=True, text="\n".join(lines))


_HANDLERS = {
    "mode": _cmd_mode,
    "pending": _cmd_pending,
    "show": _cmd_show,
    "approve": _cmd_approve,
    "reject": _cmd_reject,
    "history": _cmd_history,
}


# --- Rendering -------------------------------------------------------------


def _render_help() -> str:
    return (
        "[bold]/plan[/bold] commands:\n"
        "  [cyan]/plan mode[/cyan]               show current plan-mode state\n"
        "  [cyan]/plan mode on|off[/cyan]        toggle session-wide plan mode\n"
        "  [cyan]/plan pending[/cyan]            list pending plans\n"
        "  [cyan]/plan show <op-id>[/cyan]       render full plan detail\n"
        "  [cyan]/plan approve <op-id>[/cyan]    approve a pending plan\n"
        "  [cyan]/plan reject <op-id> <reason>[/cyan] reject with reason\n"
        "  [cyan]/plan history [N][/cyan]        last N resolved plans (default 10)\n"
        "  [cyan]/plan help[/cyan]               this message"
    )


def render_plan_detail(snap: dict) -> str:
    """Render one pending/resolved plan as a multi-line Rich string.

    Exported so the IDE observability surface (Slice 4) can reuse
    the same rendering for text previews.
    """
    plan = snap.get("plan") or {}
    approach = plan.get("approach") or plan.get("markdown") or ""
    complexity = plan.get("complexity", "")
    ordered = plan.get("ordered_changes") or []
    risks = plan.get("risk_factors") or []
    tests = plan.get("test_strategy", "")
    notes = plan.get("architectural_notes", "")

    state_color = {
        STATE_PENDING: "yellow",
        STATE_APPROVED: "green",
        STATE_REJECTED: "red",
        STATE_EXPIRED: "red",
    }.get(snap.get("state", ""), "dim")

    remaining = 0.0
    if snap.get("state") == STATE_PENDING:
        remaining = max(0.0, snap["expires_ts"] - time.monotonic())

    parts: List[str] = [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "[bold]Plan: %s[/bold]  "
        "[%s]%s[/%s]" % (
            snap["op_id"], state_color,
            snap.get("state", "?").upper(), state_color,
        ),
    ]
    if remaining > 0:
        parts.append("[dim]Expires in %ds[/dim]" % int(remaining))
    if complexity:
        parts.append("[dim]Complexity: %s[/dim]" % complexity)
    parts.append("")

    if approach:
        parts.append("[bold]Approach[/bold]")
        parts.extend(
            "  " + line for line in textwrap.wrap(approach, width=76) or [""]
        )
        parts.append("")

    if ordered:
        parts.append("[bold]Ordered changes[/bold] ([dim]%d[/dim])" % len(ordered))
        for i, ch in enumerate(ordered, 1):
            if isinstance(ch, dict):
                fp = ch.get("file_path", "") or ch.get("file", "")
                action = ch.get("action", "modify")
                reason = ch.get("reason", "") or ch.get("rationale", "")
                head = "  [cyan]%d.[/cyan] %s  [dim]%s[/dim]" % (i, fp, action)
                parts.append(head)
                if reason:
                    for line in textwrap.wrap(reason, width=70):
                        parts.append("     [dim]%s[/dim]" % line)
            else:
                parts.append("  [cyan]%d.[/cyan] %s" % (i, ch))
        parts.append("")

    if risks:
        parts.append("[bold]Risks[/bold]")
        for r in risks:
            parts.append("  [yellow]▸[/yellow] %s" % r)
        parts.append("")

    if tests:
        parts.append("[bold]Test strategy[/bold]")
        for line in textwrap.wrap(tests, width=76):
            parts.append("  %s" % line)
        parts.append("")

    if notes:
        parts.append("[bold]Architectural notes[/bold]")
        for line in textwrap.wrap(notes, width=76):
            parts.append("  %s" % line)
        parts.append("")

    if snap.get("state") == STATE_PENDING:
        parts.append(
            "[dim]Commands: /plan approve %s · "
            "/plan reject %s <reason>[/dim]" % (
                snap["op_id"], snap["op_id"],
            )
        )
    else:
        reviewer = snap.get("reviewer") or "?"
        reason = snap.get("reason") or ""
        if reason:
            parts.append(
                "[dim]Resolved by %s · %s[/dim]" % (reviewer, reason)
            )
        else:
            parts.append("[dim]Resolved by %s[/dim]" % reviewer)
    return "\n".join(parts)
