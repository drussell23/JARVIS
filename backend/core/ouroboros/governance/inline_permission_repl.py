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
from typing import Any, Callable, List, Optional, Sequence, Tuple

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
    "/permissions",
})


def _matches(line: str) -> bool:
    if not line:
        return False
    first = line.split(None, 1)[0]
    return first in _COMMANDS


def _looks_like_ordinal(line: str) -> bool:
    """Is the first token a bare number? Pure and cheap. NEVER raises."""
    try:
        first = str(line or "").strip().split(None, 1)[0]
        return first.strip(".)").isdigit()
    except Exception:  # noqa: BLE001
        return False


def _handle_ordinal(
    controller: InlinePromptController, line: str, reviewer: str,
) -> InlineDispatchResult:
    """Resolve a numbered answer against the choices THIS prompt offered.

    The choice set is recomposed from the controller's own snapshot — the
    same ``tool`` / ``target_path`` / ``arg_preview`` the renderer used —
    so the number the operator is reading and the number resolved here
    cannot disagree. Recomposition rather than storage keeps a single
    source; storing the rendered set would let it go stale against a
    prompt whose state moved on.

    Out of range answers NOTHING. An operator who typed 9 at a 4-choice
    prompt has not chosen, and picking the nearest — or the first — is how
    a fat finger becomes a persisted grant.
    """
    try:
        from backend.core.ouroboros.governance.gate_choices import (
            compose_gate_choices, describe_grant_scope, resolve_answer,
        )
        pending = controller.pending_ids()
        if not pending:
            return InlineDispatchResult(ok=False, text="", matched=False)
        pid = pending[0]
        snap = controller.snapshot(pid) or {}
        choices = compose_gate_choices(
            grant_scope=describe_grant_scope(
                tool=str(snap.get("tool") or ""),
                target_path=str(snap.get("target_path") or ""),
                arg_preview=str(snap.get("arg_preview") or ""),
            ),
        )
        answer = resolve_answer(line, choices)
        if answer.choice is None:
            return InlineDispatchResult(
                ok=False,
                text=(f"  (no option {line.strip().split()[0]} — this prompt "
                      f"offers 1-{len(choices.choices)}; "
                      f"/prompts to see it again)"),
            )
        # Delegate to the EXISTING handlers. The number is a new way to
        # name a verb, never a second implementation of one.
        args = [pid] + ([answer.reason] if answer.reason else [])
        verb = answer.verb
        if verb == "/allow":
            return _handle_allow(controller, args, reviewer, remember=False)
        if verb == "/always":
            return _handle_allow(controller, args, reviewer, remember=True)
        if verb == "/deny":
            return _handle_deny(controller, args, reviewer)
        if verb == "/pause":
            return _handle_pause(controller, args, reviewer)
        return InlineDispatchResult(ok=False, text="", matched=False)
    except Exception as exc:  # noqa: BLE001
        # Fail CLOSED to "nothing happened", never to allow. A degraded
        # resolver must not decide a permission prompt.
        logger.debug("[InlinePrompt] ordinal dispatch degraded", exc_info=True)
        return InlineDispatchResult(
            ok=False, text=f"  (could not read that answer: {exc})")


def _split_id_and_reason(
    controller: InlinePromptController,
    args: Sequence[str],
) -> Tuple[Optional[str], str]:
    """Disambiguate ``args[0]`` between a prompt-id and the first reason word.

    Rule: if ``args[0]`` is a currently-known prompt id (pending OR in the
    recent history), treat it as the id and the remainder as the reason.
    Otherwise treat *all* args as the reason and fall back to the oldest
    pending prompt.

    This avoids the landmine where ``/pause let me check`` would be read as
    pause-prompt-id=``let`` with reason=``me check``.
    """
    if not args:
        pending = controller.pending_ids()
        return (pending[0] if pending else None), ""

    head = args[0]
    known_ids = set(controller.pending_ids())
    for h in controller.history():
        pid = h.get("prompt_id")
        if pid:
            known_ids.add(pid)

    if head in known_ids:
        return head, " ".join(args[1:]).strip()

    pending = controller.pending_ids()
    return (pending[0] if pending else None), " ".join(args).strip()


def dispatch_inline_command(
    line: str,
    *,
    controller: Optional[InlinePromptController] = None,
    store: Optional[Any] = None,
    reviewer: str = "repl",
) -> InlineDispatchResult:
    """Route one REPL line to an inline-permission action.

    The dispatcher is stateless (apart from the singleton controller).
    All commands return an :class:`InlineDispatchResult`; callers print
    ``.text`` unconditionally and branch on ``.ok`` for scripting.
    Unmatched lines return ``matched=False`` so the REPL can fall
    through to the next handler.

    ``store`` — optional :class:`RememberedAllowStore` for
    ``/permissions`` subcommands. When None, the dispatcher looks up
    the per-repo singleton lazily so SerpentFlow can pass a single
    ``dispatch_inline_command(line)`` call through the branch.
    """
    if not _matches(line) and not _looks_like_ordinal(line):
        return InlineDispatchResult(ok=False, text="", matched=False)

    controller = controller or get_default_controller()

    if not _matches(line):
        # A bare number. It is an ANSWER only while something is asking:
        # with nothing pending, "2" is ordinary REPL input and must fall
        # through untouched. The singleton is only consulted after the
        # cheap syntactic check, so an ordinary line never instantiates it.
        if not controller.pending_ids():
            return InlineDispatchResult(ok=False, text="", matched=False)
        return _handle_ordinal(controller, line, reviewer)

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
    if cmd == "/permissions":
        return _handle_permissions(store, args)

    return InlineDispatchResult(ok=False, text="", matched=False)


# ---------------------------------------------------------------------------
# /permissions dispatcher (Slice 3)
# ---------------------------------------------------------------------------


_PERM_HELP = textwrap.dedent(
    """
    Inline permission store commands (Slice 3)
    ------------------------------------------
      /permissions                    — list active grants
      /permissions list               — same as above
      /permissions show <grant-id>    — full detail
      /permissions revoke <grant-id>  — remove a grant (tombstone)
      /permissions clear              — revoke every grant (bounded by repo)
      /permissions prune              — force-expire + purge stale grants
      /permissions help               — this text
    """
).strip()


def _resolve_store(store: Optional[Any]) -> Any:
    """Lazy-import the store only when /permissions is actually invoked.

    Avoids making the REPL module hard-depend on the memory module.
    """
    if store is not None:
        return store
    try:
        from pathlib import Path as _P
        from backend.core.ouroboros.governance.inline_permission_memory import (
            get_store_for_repo,
        )
    except Exception as exc:  # noqa: BLE001
        return (
            None,
            f"  /permissions: memory module unavailable: {exc}",
        )
    # Default: current working directory as repo root. SerpentFlow owns
    # the real repo_root; callers that need that should pass store=... .
    return get_store_for_repo(_P.cwd())


def _handle_permissions(
    store: Optional[Any], args: Sequence[str],
) -> InlineDispatchResult:
    resolved = _resolve_store(store)
    if isinstance(resolved, tuple):
        _, err = resolved
        return InlineDispatchResult(ok=False, text=err)
    s = resolved

    if not args:
        return _perm_list(s)
    head = args[0]
    if head == "list":
        return _perm_list(s)
    if head == "help":
        return InlineDispatchResult(ok=True, text=_PERM_HELP)
    if head == "show":
        if len(args) < 2:
            return InlineDispatchResult(
                ok=False, text="  /permissions show <grant-id>",
            )
        return _perm_show(s, args[1])
    if head == "revoke":
        if len(args) < 2:
            return InlineDispatchResult(
                ok=False, text="  /permissions revoke <grant-id>",
            )
        return _perm_revoke(s, args[1])
    if head == "clear":
        n = s.revoke_all()
        return InlineDispatchResult(
            ok=True, text=f"  /permissions cleared {n} grant(s)",
        )
    if head == "prune":
        n = s.prune_expired()
        return InlineDispatchResult(
            ok=True, text=f"  /permissions pruned {n} expired grant(s)",
        )
    # `/permissions <grant-id>` short-form for show
    return _perm_show(s, head)


def _perm_list(store: Any) -> InlineDispatchResult:
    grants = store.list_active()
    if not grants:
        return InlineDispatchResult(
            ok=True, text="  (no active grants)",
        )
    lines: List[str] = [f"  Active grants ({len(grants)}):"]
    for g in grants:
        lines.append(
            f"  - {g.grant_id}  {g.tool:<12} {g.match_mode:<12} "
            f"pattern={g.sanitized_pattern[:60] or g.pattern[:60]}  "
            f"expires={g.expires_at_iso}"
        )
    return InlineDispatchResult(ok=True, text="\n".join(lines))


def _perm_show(store: Any, grant_id: str) -> InlineDispatchResult:
    g = store.get(grant_id)
    if g is None:
        return InlineDispatchResult(
            ok=False, text=f"  /permissions: unknown grant: {grant_id}",
        )
    lines = [
        f"  Grant {g.grant_id}",
        f"    tool       : {g.tool}",
        f"    match_mode : {g.match_mode}",
        f"    pattern    : {g.sanitized_pattern or g.pattern}",
        f"    repo_root  : {g.repo_root}",
        f"    granted_at : {g.granted_at_iso}",
        f"    expires_at : {g.expires_at_iso}",
    ]
    if g.granted_from_prompt_id:
        lines.append(f"    from_prompt: {g.granted_from_prompt_id}")
    if g.operator_note:
        lines.append(f"    note       : {g.operator_note}")
    return InlineDispatchResult(ok=True, text="\n".join(lines))


def _perm_revoke(store: Any, grant_id: str) -> InlineDispatchResult:
    if store.revoke(grant_id):
        return InlineDispatchResult(
            ok=True, text=f"  /permissions revoked {grant_id}",
        )
    return InlineDispatchResult(
        ok=False, text=f"  /permissions: unknown grant: {grant_id}",
    )


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
    pid, reason = _split_id_and_reason(controller, args)
    if pid is None:
        return InlineDispatchResult(
            ok=False, text="  (no pending prompt to allow)",
        )
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
    pid, reason = _split_id_and_reason(controller, args)
    if pid is None:
        return InlineDispatchResult(
            ok=False, text="  (no pending prompt to deny)",
        )
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
    pid, reason = _split_id_and_reason(controller, args)
    if pid is None:
        return InlineDispatchResult(
            ok=False, text="  (no pending prompt to pause)",
        )
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
        # DERIVED. The sibling renderer's hand-written twin of this line
        # had already lost `/always`; two hands writing one vocabulary is
        # the defect, so neither writes it now.
        lines.extend(ConsoleInlineRenderer._choice_lines(request))
        lines.append(ConsoleInlineRenderer._actions_line())
        lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _choices(request: InlinePromptRequest):
        """The choice set for THIS prompt. NEVER raises.

        Unlike the phase-boundary surface, this one knows the tool and the
        argument, so `/always` can state exactly what it would grant —
        that tool, that exact path or command, this repo, and the store's
        own TTL. Where the scope cannot be named, `describe_grant_scope`
        returns None and the option drops out rather than promising a
        boundary the operator cannot see.
        """
        from backend.core.ouroboros.governance.gate_choices import (
            compose_gate_choices, describe_grant_scope,
        )
        return compose_gate_choices(
            question=f"Do you want to allow {request.tool}?",
            grant_scope=describe_grant_scope(
                tool=request.tool,
                target_path=request.target_path,
                arg_preview=request.arg_preview,
            ),
        )

    @staticmethod
    def _choice_lines(request: InlinePromptRequest) -> list:
        try:
            from backend.core.ouroboros.governance.gate_choices import (
                render_choices,
            )
            rows = render_choices(ConsoleInlineRenderer._choices(request))
            return [""] + rows if rows else []
        except Exception:  # noqa: BLE001
            return []

    @staticmethod
    def _actions_line(request: InlinePromptRequest = None) -> str:
        try:
            from backend.core.ouroboros.governance.gate_choices import (
                compose_gate_choices, describe_grant_scope, render_verbs,
            )
            scope = (describe_grant_scope(
                tool=request.tool, target_path=request.target_path,
                arg_preview=request.arg_preview) if request is not None
                else "remember it")
            return render_verbs(compose_gate_choices(grant_scope=scope)) or (
                "    actions  : /allow   /deny <reason>   /always   /pause")
        except Exception:  # noqa: BLE001
            return "    actions  : /allow   /deny <reason>   /always   /pause"


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
