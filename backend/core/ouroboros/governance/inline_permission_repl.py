"""Inline-permission REPL dispatcher + renderer adapter — Slice 2.

Provides two entry points for operator surfaces:

1. :func:`dispatch_inline_command` — one-call slash-command handler for
   ``/allow``, ``/deny``, ``/always``, ``/pause``, ``/prompts``.
   Mirrors the shape of :mod:`plan_approval_repl` (Problem #7) so both
   dispatchers coexist in the same REPL branch without collision.

2. :class:`ConsoleInlineRenderer` — a tiny adapter that implements the
   :class:`InlinePromptRenderer` protocol by writing to a print-callback.
   SerpentFlow injects its Rich console's ``print`` method; tests inject
   a list-append.

Slice 2 does not touch ``serpent_flow.py`` directly — the dispatcher
and renderer are importable from one place and can be wired in a
three-line edit (the same pattern Problem #7 used).
"""
from __future__ import annotations

import logging
import os
import shlex
import textwrap
from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Sequence

from backend.core.ouroboros.governance.inline_permission_prompt import (
    InlinePromptController,
    InlinePromptOutcome,
    InlinePromptRequest,
    InlinePromptStateError,
    STATE_ALLOWED,
    STATE_DENIED,
    STATE_EXPIRED,
    STATE_PAUSED,
    STATE_PENDING,
    get_default_controller,
)

logger = logging.getLogger("Ouroboros.InlinePrompt.REPL")


# ---------------------------------------------------------------------------
# Result dataclass (mirrors PlanDispatchResult shape)
# ---------------------------------------------------------------------------


@dataclass
class InlineDispatchResult:
    """Return value from :func:`dispatch_inline_command`."""

    ok: bool
    text: str
    matched: bool = True


# ---------------------------------------------------------------------------
# Slash-command dispatcher
# ---------------------------------------------------------------------------


_HELP_TEXT = textwrap.dedent(
    """
    Inline permission commands (Slice 2)
    ------------------------------------
      /prompts                    — list pending prompts
      /prompts show <prompt-id>   — full detail for one prompt
      /prompts history [N]        — last N resolved prompts (default 10)
      /allow   [<prompt-id>]      — allow once; defaults to oldest pending
      /always  [<prompt-id>]      — allow + remember (Slice 3 persists)
      /deny    [<prompt-id>] [reason]
      /pause   [<prompt-id>] [reason]   — halt the owning op
      /prompts help
    """
).strip()


_COMMANDS = frozenset({
    "/prompts", "/allow", "/always", "/deny", "/pause",
})


def _matches(line: str) -> bool:
    if not line:
        return False
    first = line.split(None, 1)[0]
    return first in _COMMANDS


def _resolve_prompt_id(
    controller: InlinePromptController,
    explicit: Optional[str],
) -> Optional[str]:
    if explicit:
        return explicit
    pending = controller.pending_ids()
    return pending[0] if pending else None


def dispatch_inline_command(
    line: str,
    *,
    controller: Optional[InlinePromptController] = None,
    reviewer: str = "repl",
) -> InlineDispatchResult:
    """Route one REPL line to an inline-permission action.

    The dispatcher is stateless (apart from the singleton controller).
    All commands return an :class:`InlineDispatchResult`; callers print
    ``.text`` unconditionally and branch on ``.ok`` for scripting.
    Unmatched lines return ``matched=False`` so the REPL can fall
    through to the next handler.
    """
    if not _matches(line):
        return InlineDispatchResult(ok=False, text="", matched=False)

    controller = controller or get_default_controller()

    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return InlineDispatchResult(
            ok=False, text=f"  /prompts: shlex parse error: {exc}",
        )

    if not tokens:
        return InlineDispatchResult(ok=False, text="", matched=False)

    cmd = tokens[0]
    args = tokens[1:]

    if cmd == "/prompts":
        return _handle_prompts(controller, args)
    if cmd == "/allow":
        return _handle_allow(controller, args, reviewer, remember=False)
    if cmd == "/always":
        return _handle_allow(controller, args, reviewer, remember=True)
    if cmd == "/deny":
        return _handle_deny(controller, args, reviewer)
    if cmd == "/pause":
        return _handle_pause(controller, args, reviewer)

    return InlineDispatchResult(ok=False, text="", matched=False)


def _handle_prompts(
    controller: InlinePromptController, args: Sequence[str],
) -> InlineDispatchResult:
    if not args:
        return _list_pending(controller)
    head = args[0]
    if head == "help":
        return InlineDispatchResult(ok=True, text=_HELP_TEXT)
    if head == "show":
        if len(args) < 2:
            return InlineDispatchResult(
                ok=False, text="  /prompts show <prompt-id>",
            )
        return _show_one(controller, args[1])
    if head == "history":
        n = 10
        if len(args) >= 2:
            try:
                n = max(1, int(args[1]))
            except ValueError:
                return InlineDispatchResult(
                    ok=False,
                    text=f"  /prompts history: not an integer: {args[1]}",
                )
        return _show_history(controller, n)
    # `/prompts <prompt-id>` → show
    return _show_one(controller, head)


def _list_pending(
    controller: InlinePromptController,
) -> InlineDispatchResult:
    snapshots = [
        s for s in controller.snapshot_all() if s["state"] == STATE_PENDING
    ]
    if not snapshots:
        return InlineDispatchResult(
            ok=True, text="  (no inline prompts pending)",
        )
    lines: List[str] = [f"  Pending inline prompts ({len(snapshots)}):"]
    for s in snapshots:
        lines.append(
            f"  - {s['prompt_id'][:40]:<40} {s['tool']:<12} "
            f"rule={s['verdict_rule_id']} target={s['target_path'][:40]}"
        )
    return InlineDispatchResult(ok=True, text="\n".join(lines))


def _show_one(
    controller: InlinePromptController, prompt_id: str,
) -> InlineDispatchResult:
    s = controller.snapshot(prompt_id)
    if s is None:
        return InlineDispatchResult(
            ok=False, text=f"  unknown prompt_id: {prompt_id}",
        )
    lines = [
        f"  Prompt {s['prompt_id']}",
        f"    op         : {s['op_id']}",
        f"    call       : {s['call_id']}",
        f"    tool       : {s['tool']}",
        f"    target     : {s['target_path'] or '-'}",
        f"    args       : {s['arg_preview']}",
        f"    verdict    : {s['verdict_decision']} ({s['verdict_rule_id']})",
        f"    state      : {s['state']}",
    ]
    if s.get("response"):
        lines.append(f"    response   : {s['response']}")
    if s.get("reviewer"):
        lines.append(f"    reviewer   : {s['reviewer']}")
    if s.get("operator_reason"):
        lines.append(f"    reason     : {s['operator_reason']}")
    return InlineDispatchResult(ok=True, text="\n".join(lines))


def _show_history(
    controller: InlinePromptController, n: int,
) -> InlineDispatchResult:
    recent = controller.history()[-n:]
    if not recent:
        return InlineDispatchResult(
            ok=True, text="  (no prompt history)",
        )
    lines = [f"  Recent inline prompts ({len(recent)}):"]
    for h in recent:
        lines.append(
            f"  - {h['prompt_id'][:40]:<40} {h['state']:<8} "
            f"reviewer={h['reviewer']} elapsed={h['elapsed_s']:.1f}s "
            f"reason={(h.get('operator_reason') or '')[:60]}"
        )
    return InlineDispatchResult(ok=True, text="\n".join(lines))


def _handle_allow(
    controller: InlinePromptController,
    args: Sequence[str],
    reviewer: str,
    *,
    remember: bool,
) -> InlineDispatchResult:
    pid = _resolve_prompt_id(
        controller,
        args[0] if args and not args[0].startswith("-") else None,
    )
    if pid is None:
        return InlineDispatchResult(
            ok=False, text="  (no pending prompt to allow)",
        )
    reason = " ".join(args[1:]) if args and args[0] == pid else " ".join(args)
    reason = reason.strip()
    try:
        if remember:
            out = controller.allow_always(pid, reviewer=reviewer, reason=reason)
        else:
            out = controller.allow_once(pid, reviewer=reviewer, reason=reason)
    except InlinePromptStateError as exc:
        return InlineDispatchResult(ok=False, text=f"  /allow: {exc}")
    verb = "allow-always" if remember else "allow-once"
    return InlineDispatchResult(
        ok=True,
        text=f"  {verb}: {pid[:40]} (elapsed={out.elapsed_s:.1f}s)",
    )


def _handle_deny(
    controller: InlinePromptController,
    args: Sequence[str],
    reviewer: str,
) -> InlineDispatchResult:
    pid = _resolve_prompt_id(
        controller,
        args[0] if args and not args[0].startswith("-") else None,
    )
    if pid is None:
        return InlineDispatchResult(
            ok=False, text="  (no pending prompt to deny)",
        )
    reason = " ".join(args[1:]) if args and args[0] == pid else " ".join(args)
    reason = reason.strip()
    try:
        out = controller.deny(pid, reviewer=reviewer, reason=reason)
    except InlinePromptStateError as exc:
        return InlineDispatchResult(ok=False, text=f"  /deny: {exc}")
    return InlineDispatchResult(
        ok=True,
        text=f"  denied: {pid[:40]} (elapsed={out.elapsed_s:.1f}s)",
    )


def _handle_pause(
    controller: InlinePromptController,
    args: Sequence[str],
    reviewer: str,
) -> InlineDispatchResult:
    pid = _resolve_prompt_id(
        controller,
        args[0] if args and not args[0].startswith("-") else None,
    )
    if pid is None:
        return InlineDispatchResult(
            ok=False, text="  (no pending prompt to pause)",
        )
    reason = " ".join(args[1:]) if args and args[0] == pid else " ".join(args)
    reason = reason.strip()
    try:
        out = controller.pause_op(pid, reviewer=reviewer, reason=reason)
    except InlinePromptStateError as exc:
        return InlineDispatchResult(ok=False, text=f"  /pause: {exc}")
    return InlineDispatchResult(
        ok=True,
        text=f"  paused: {pid[:40]} (elapsed={out.elapsed_s:.1f}s)",
    )


# ---------------------------------------------------------------------------
# ConsoleInlineRenderer — adapter for any write-line callback
# ---------------------------------------------------------------------------


PrintCallback = Callable[[str], None]


class ConsoleInlineRenderer:
    """A minimal :class:`InlinePromptRenderer` that formats a CC-style
    block and hands it to a print callback.

    ``print_cb`` is usually ``console.print`` when hosted inside
    SerpentFlow. Tests inject ``lines.append`` and inspect the captured
    strings.

    The renderer is intentionally dumb: no Rich markup, no color codes,
    no prompt-id redaction. The caller adds formatting in a SerpentFlow
    wrapper if wanted (Rich tags can be interpolated safely because we
    do not include untrusted user content in the structural scaffolding).
    """

    def __init__(self, print_cb: PrintCallback) -> None:
        self._print = print_cb

    def render(self, request: InlinePromptRequest) -> None:
        block = self.format_block(request)
        self._print(block)

    def dismiss(
        self, prompt_id: str, outcome: InlinePromptOutcome,
    ) -> None:
        verb = {
            STATE_ALLOWED: "allowed",
            STATE_DENIED: "denied",
            STATE_EXPIRED: "expired",
            STATE_PAUSED: "paused",
        }.get(outcome.state, outcome.state)
        line = (
            f"  [InlinePrompt] {verb}: {prompt_id[:40]} "
            f"(elapsed={outcome.elapsed_s:.1f}s reviewer={outcome.reviewer})"
        )
        if outcome.operator_reason:
            line += f" reason={outcome.operator_reason[:80]}"
        self._print(line)

    # --- pure formatter --------------------------------------------------

    @staticmethod
    def format_block(request: InlinePromptRequest) -> str:
        """Return the multi-line prompt block as a string.

        Pure function; no I/O. Useful for golden tests.
        """
        v = request.verdict
        lines = [
            "",
            f"  [InlinePrompt] {request.tool}({request.arg_preview})",
            f"    rule     : {v.decision.value} / {v.rule_id}",
            f"    reason   : {v.reason}",
            f"    target   : {request.target_path or '(n/a)'}",
            f"    op       : {request.op_id}  call: {request.call_id}",
        ]
        if request.rationale:
            lines.append(f"    model    : {request.rationale[:200]}")
        lines.append(
            f"    prompt_id: {request.prompt_id}"
        )
        lines.append(
            "    actions  : /allow   /deny <reason>   /always   /pause"
        )
        lines.append("")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Env-guarded accessor (Slice 5 graduation controls default)
# ---------------------------------------------------------------------------


def inline_repl_enabled() -> bool:
    """Convenience wrapper over the master switch. Mirrors the env
    knob used by :mod:`inline_permission_prompt`; the REPL dispatcher
    itself is authority-free and may still be imported and called
    when the master switch is off."""
    return os.environ.get(
        "JARVIS_INLINE_PERMISSION_ENABLED", "false",
    ).strip().lower() == "true"


# Late, module-level re-export so consumers don't need to import from
# two places when wiring up REPL + renderer.
__all__ = [
    "ConsoleInlineRenderer",
    "InlineDispatchResult",
    "dispatch_inline_command",
    "inline_repl_enabled",
]

# Intentionally unused import guard — silences the linter if the symbol
# becomes used by a future slice without churning imports.
_ = (Any,)
