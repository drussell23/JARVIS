"""``/btw`` — ask O+V something without taking the floor.

The operator surface for :mod:`side_channel`. Auto-discovered by
:mod:`repl_dispatch_registry` through the §33.3 naming cage: basename
``btw_repl.py`` → verb ``btw`` → ``/btw`` routes with zero edits to any
dispatch ladder.

    /btw <question>       ask; returns an s-N ticket IMMEDIATELY
    /btw                  the ledger — what was asked, and where it got to
    /btw status           the lane itself: depth, spend, admission signals
    /btw cancel <s-N>     withdraw one; cancels its provider call if running
    /btw <s-N>            re-read one (same rendering as `/expand s-N`)
    /btw help             this text (always available, master-flag or not)

Why the grammar refuses to guess
--------------------------------
A side question is a SENTENCE and a subcommand is a WORD. That is the
whole disambiguation rule, and it is the same one
:func:`operator_prompt_bridge.is_bare_verdict` arrived at after the
looser version approved an op because the operator's goal happened to
start with "go".

Applied here, the loose version costs a question rather than a
repository: ``/btw cancel the doc storm and tell me why`` is a question
about cancelling, and a prefix match would have silently tried to
withdraw a ticket named "the". So a control form must be EXACTLY a bare
word, or a verb plus one ref-shaped token — anything else is what the
operator typed, and gets asked.

Authority
---------
None. This module submits text and renders ledgers. It imports stdlib +
:mod:`side_channel` (lazily, at the call site) ONLY; NEVER orchestrator
/ policy / iron_gate / candidate_generator / tool_executor /
urgency_router / change_engine / semantic_guardian / providers. It
mutates exactly one thing — the aside ledger — and only through that
substrate's own ``submit`` / ``cancel``.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("Ouroboros.BtwRepl")


BTW_REPL_SCHEMA_VERSION: str = "btw_repl.v1"


#: A ticket handle, and nothing that merely looks like one. Anchored so
#: ``s-1 and also why`` is a question, not a lookup.
_REF_RE = re.compile(r"^s-\d+$", re.IGNORECASE)


_HELP = (
    "[bold]/btw[/bold] [dim]— ask a side question without interrupting "
    "the work[/dim]\n"
    "\n"
    "  [bold]/btw <question>[/bold]      ask; you get an [bold]s-N[/bold] "
    "ticket back immediately and\n"
    "                             keep your prompt. The answer arrives "
    "when the\n"
    "                             organism has room — it never preempts "
    "an op.\n"
    "  [bold]/btw[/bold]                 the ledger: every aside and "
    "where it got to\n"
    "  [bold]/btw status[/bold]          the lane: depth, spend, why "
    "anything is waiting\n"
    "  [bold]/btw cancel <s-N>[/bold]    withdraw one (kills its provider "
    "call if running)\n"
    "  [bold]/btw <s-N>[/bold]           re-read one\n"
    "  [bold]/btw help[/bold]            this text\n"
    "\n"
    "[dim]Answers are grounded in what O+V is doing right now — the "
    "status line, the\n"
    "streams in flight, the recent narrative — so \"why is that slow\" "
    "has a referent.\n"
    "Each answer is parked as a q-N artifact: `/expand q-N` re-reads "
    "it.\n"
    "The lane can only READ. To make something happen, use `/goal`.[/dim]"
)


@dataclass(frozen=True)
class BtwReplDispatchResult:
    """Result of a ``/btw`` dispatch. Frozen for safe propagation.

    ``matched=False`` signals the line wasn't a ``/btw`` invocation and
    the caller routes elsewhere. §33.5 symmetric ``to_dict``."""

    ok: bool
    text: str
    matched: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "text": self.text, "matched": self.matched}


_NO_MATCH = BtwReplDispatchResult(ok=False, text="", matched=False)


def _matches(line: object) -> bool:
    """NEVER raises — callers may pass non-str garbage."""
    try:
        s = str(line or "").strip()
    except Exception:  # noqa: BLE001
        return False
    if not s:
        return False
    return s in ("/btw", "btw") or s.startswith(("/btw ", "btw "))


def _remainder(line: object) -> str:
    """Everything after the verb, whitespace-normalised at the edges but
    NOT internally — a question keeps its own spacing."""
    try:
        s = str(line or "").strip()
        for prefix in ("/btw", "btw"):
            if s == prefix:
                return ""
            if s.startswith(prefix + " "):
                return s[len(prefix) + 1:].strip()
        return ""
    except Exception:  # noqa: BLE001
        return ""


def _control_form(rest: str) -> Optional[Tuple[str, str]]:
    """``(verb, argument)`` when *rest* is unambiguously a subcommand.

    Returns None for anything a human would recognise as a question,
    which is the safe direction: mistaking a subcommand for a question
    costs one cheap provider call and an answer that says "use /btw
    cancel"; mistaking a question for a subcommand silently swallows it.
    """
    try:
        tokens = rest.split()
        if not tokens:
            return None
        if len(tokens) == 1:
            head = tokens[0].lower()
            if head in ("help", "?", "status", "stats"):
                return (head, "")
            if _REF_RE.match(tokens[0]):
                return ("show", tokens[0].lower())
            return None
        if len(tokens) == 2 and _REF_RE.match(tokens[1]):
            head = tokens[0].lower()
            if head in ("cancel", "drop", "withdraw"):
                return ("cancel", tokens[1].lower())
            if head in ("show", "expand", "read"):
                return ("show", tokens[1].lower())
        return None
    except Exception:  # noqa: BLE001
        return None


def _current_session() -> str:
    """Which cockpit is typing, or "" for the daemon's own terminal.

    Read HERE, on the dispatch task, because that is the only task the
    ContextVar is set on — the worker that answers later is a different
    task and would read None. NEVER raises.
    """
    try:
        from backend.core.ouroboros.battle_test.attach_session import (
            current_session,
        )
        return str(current_session() or "")
    except Exception:  # noqa: BLE001
        return ""


def dispatch_btw_command(line: str) -> BtwReplDispatchResult:
    """Route one ``/btw`` line. NEVER raises.

    Synchronous by contract and by necessity: this runs on the operator
    input queue's single consumer, and the entire point of the lane is
    that nothing on that path awaits a provider. Everything expensive
    happens on :class:`side_channel.SideChannel`'s worker.
    """
    if not _matches(line):
        return _NO_MATCH

    rest = _remainder(line)
    control = _control_form(rest)

    # `help` answers before any substrate import, so a broken or absent
    # side_channel still tells the operator what the verb is for.
    if control is not None and control[0] in ("help", "?"):
        return BtwReplDispatchResult(ok=True, text=_HELP)

    try:
        from backend.core.ouroboros.governance import side_channel as sc
    except Exception as exc:  # noqa: BLE001
        return BtwReplDispatchResult(
            ok=False,
            text=(
                f"  [dim]/btw: side-channel substrate unavailable: "
                f"{exc!r}[/dim]"
            ),
        )

    try:
        if control is None and rest:
            return _submit(sc, rest)
        if control is None:
            return _render_ledger(sc)
        verb, arg = control
        if verb in ("status", "stats"):
            return _render_status(sc)
        if verb == "cancel":
            return _cancel(sc, arg)
        if verb == "show":
            return BtwReplDispatchResult(ok=True, text=sc.render_ref(arg))
        return _render_ledger(sc)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[Btw] dispatch degraded", exc_info=True)
        return BtwReplDispatchResult(
            ok=False, text=f"  [dim]/btw: {exc!r}[/dim]",
        )


def _submit(sc: Any, question: str) -> BtwReplDispatchResult:
    """Take the aside and hand back a receipt in the same keystroke."""
    outcome = sc.ask_aside(question, _current_session())
    if not outcome.accepted or outcome.ticket is None:
        # Refused, never dropped. The reason names the flag or the
        # bound, so the operator can act on it rather than guess.
        return BtwReplDispatchResult(
            ok=False,
            text=f"  [dim]⚠ /btw: {outcome.reason}[/dim]",
        )
    if outcome.coalesced:
        return BtwReplDispatchResult(
            ok=True,
            text=(
                f"  [dim]… already asked — folded into "
                f"[/dim][bold]{outcome.ticket.ref}[/bold]"
                f"[dim] ({outcome.ticket.state.value})[/dim]"
            ),
        )
    return BtwReplDispatchResult(
        ok=True,
        text=sc.render_ack(outcome.ticket, depth=outcome.depth),
    )


def _cancel(sc: Any, ref: str) -> BtwReplDispatchResult:
    ticket = sc.get_default_side_channel().cancel(ref)
    if ticket is None:
        # Deliberately does not distinguish "unknown" from "already
        # finished": both mean "there is nothing to withdraw", and an
        # operator acting on a ref they read a moment ago should not
        # have to care which race they lost.
        return BtwReplDispatchResult(
            ok=False,
            text=(
                f"  [dim]/btw: {ref} is not a waiting side question "
                f"(unknown, or already resolved)[/dim]"
            ),
        )
    return BtwReplDispatchResult(
        ok=True,
        text=f"  [dim]× withdrew [/dim][bold]{ticket.ref}[/bold]",
    )


def _render_ledger(sc: Any) -> BtwReplDispatchResult:
    channel = sc.get_default_side_channel()
    tickets = channel.list_recent(limit=12)
    body = sc.render_ledger(tickets)
    live = len(channel.live_tickets())
    head = "[bold]💭 Side questions[/bold]"
    if live:
        head += f" [dim]· {live} waiting[/dim]"
    if not sc.side_channel_enabled():
        head += (
            f" [dim]· lane disabled ({sc.ENV_MASTER}=false)[/dim]"
        )
    return BtwReplDispatchResult(ok=True, text=f"{head}\n{body}")


def _render_status(sc: Any) -> BtwReplDispatchResult:
    """The lane's own vitals — including WHY anything is waiting.

    The admission signals are recomputed live rather than read off the
    ledger: an operator asking "why is my question still waiting" wants
    the condition as it is NOW, not the one that held it thirty seconds
    ago.
    """
    channel = sc.get_default_side_channel()
    snap = channel.snapshot(tickets=0)
    rows: List[str] = [
        "[bold]💭 Side-question lane[/bold]",
        (
            f"  [dim]lane[/dim] "
            f"{'on' if sc.side_channel_enabled() else 'off'}"
            f"  [dim]worker[/dim] "
            f"{'running' if snap.worker_running else 'idle'}"
            f"  [dim]concurrency[/dim] {sc.concurrency()}"
        ),
        (
            f"  [dim]waiting[/dim] {snap.live}/{sc.queue_depth()}"
            f"  [dim]answered[/dim] {snap.answered}"
            f"  [dim]failed[/dim] {snap.failed}"
            f"  [dim]refused[/dim] {snap.refused}"
            f"  [dim]spent[/dim] ${snap.cost_usd:.5f}"
        ),
    ]

    admission = sc.assess_admission_now()
    signals = "  ".join(
        f"[dim]{name}[/dim] {value}" for name, value in admission.signals
    ) or "[dim]no load signals available[/dim]"
    rows.append(
        f"  [dim]admission[/dim] {admission.state.value}"
        f"  [dim]—[/dim] {admission.reason or 'clear'}"
    )
    rows.append(f"  {signals}")

    readers = ", ".join(sc.situation_reader_names()) or "none"
    rows.append(
        f"  [dim]grounding readers[/dim] {readers}"
        f"  [dim]sink[/dim] "
        f"{'bound' if sc.answer_sink_bound() else 'fallback'}"
    )

    # The paid gate lives in fast_path_qa and stays there. Reporting it
    # here rather than shadowing it keeps ONE authority over whether an
    # aside can be answered, and still answers the operator's actual
    # question ("why did that come back 'disabled'?").
    try:
        from backend.core.ouroboros.governance.fast_path_qa import (
            cost_today_usd,
            daily_budget_usd,
            master_enabled,
        )
        rows.append(
            f"  [dim]answering substrate[/dim] "
            f"{'enabled' if master_enabled() else 'DISABLED'}"
            f"  [dim]budget today[/dim] "
            f"${cost_today_usd():.4f}/${daily_budget_usd():.2f}"
        )
    except Exception:  # noqa: BLE001
        rows.append("  [dim]answering substrate[/dim] unavailable")

    return BtwReplDispatchResult(ok=True, text="\n".join(rows))


def register_verbs(registry: Any) -> int:
    """Declare `/btw` to the canonical help registry. NEVER raises.

    Auto-discovered by ``help_dispatcher._discover_module_provided_verbs``
    — no edit there. Without this the verb DISPATCHES correctly and is
    findable nowhere, which is the same shape as the sixteen verbs
    `/help` lost when a hand-written list said what existed and the code
    said something else.

    ``help_text_fn`` rather than ``help_text``: the help block is the
    module's ``_HELP`` and passing it by reference means `/help btw` and
    `/btw help` can never render two different explanations of one verb.
    """
    try:
        from backend.core.ouroboros.governance.help_dispatcher import (
            VerbSpec,
        )
    except Exception:  # noqa: BLE001 — defensive
        return 0
    try:
        registry.register(VerbSpec(
            name="/btw",
            one_line=(
                "Ask a side question without interrupting the work: "
                "returns an s-N ticket immediately; the answer arrives "
                "out-of-band when the organism has room."
            ),
            category="introspection",
            help_text_fn=lambda: _HELP,
        ))
        return 1
    except Exception:  # noqa: BLE001 — defensive
        logger.debug("[btw_repl] register_verbs swallowed", exc_info=True)
        return 0


__all__ = [
    "BTW_REPL_SCHEMA_VERSION",
    "BtwReplDispatchResult",
    "dispatch_btw_command",
    "register_verbs",
]
