"""A reply in O+V's voice, not a dump of how the reply was decided.

What an operator types a goal and currently gets back::

    [chat] · turn: chat-1dc4650228e7 · session: repl
      intent: ACTION_REQUEST (conf=0.67)
      action: backlog_dispatch
      reasons: action_verb
      message: read project_ov_unified_ipc_transceiver.md and build it
      reason: action verb match (conf=0.67)
    [chat] => logged-backlog-chat-1dc4650228e7

Six lines, and not one answers the question the operator asked. It is the
CLASSIFIER'S internal state — its confidence, its matched heuristic, its
routing choice — presented as the response. The operator's own words are
echoed back at them under a field name. And the only line describing what
actually happened, ``=> logged-backlog-…``, uses a word ("logged") that hides
the most important fact: nothing is running yet.

This is the same defect the palette had when docstrings leaked into it as
help — maintainer-facing text rendered in an operator-facing surface. The fix
is the same: keep the information, change WHO it is addressed to and WHERE it
appears.

Three rules, and they map onto grammar that already exists
-----------------------------------------------------------
``docs/OV_STYLE_GUIDE.md`` §04 defines the glyphs and §05 the severity ladder
that ``/breadcrumbs`` already filters by. The chat renderer simply never used
either. So:

1. **The acknowledgement is a sentence** (``⏺``) — what O+V understood, in
   words, with no field names.
2. **The work is op chrome** (``⎿``) — what it is doing about it, one line
   per real step, the same shape a tool call renders in.
3. **The classifier's reasoning is TRACE** (``·``, VERBOSE) — it does not
   vanish, it drops below the breadcrumb floor and returns with ``/expand``.
   Nobody debugging routing loses anything; everybody else stops reading it.

And a fourth, which is honesty rather than style
------------------------------------------------
``backlog_dispatch`` files the goal for a sensor to collect on a later poll.
Saying "logged" and stopping reads as *done*. A queued goal must say it is
queued, and say what will collect it — otherwise the operator watches a still
screen and concludes the organism ignored them. That was the actual complaint
behind "it doesn't start doing the process".

Pure formatting. This module decides nothing, dispatches nothing, and holds no
state — it turns a decision that has already been made into lines. That is
what keeps it testable without a daemon, and what stops presentation from
quietly acquiring authority.
"""
from __future__ import annotations

import logging
from typing import Any, List, Optional, Sequence

logger = logging.getLogger("Ouroboros.ChatStyle")

__all__ = [
    "ACTION_VOICE",
    "compose_reply",
    "acknowledge",
    "trace_lines",
]

#: What each routing action MEANS, said the way a colleague would say it.
#:
#: Keyed by the action the classifier already produces, so adding a route
#: means adding a line here — not teaching a renderer about a new concept.
#: The value is (verb-phrase, is_deferred). ``is_deferred`` is the honesty
#: flag: True when the work will be collected later rather than started now,
#: which is the difference between "on it" and "queued".
ACTION_VOICE = {
    # backlog_dispatch is now TWO outcomes wearing one action name: the goal
    # either entered intake and is running, or it was filed for a sensor
    # sweep. The receipt shape tells them apart (`op-…` vs `chat:…`), and
    # saying "queued" about live work — or "on it" about filed work — is
    # exactly the dishonesty the queue note exists to prevent. Resolved at
    # render time by `_dispatched_now`, not by a second action token, so the
    # classifier is not asked to predict what the executor will manage.
    "backlog_dispatch": ("adding that to the backlog", True),
    "subagent_explore": ("sending a subagent to explore that", False),
    "claude_query": ("thinking about that", False),
    "context_attach": ("attaching that to the earlier turn", False),
    "social_ack": ("", False),          # the reply IS the response
    "noop": ("nothing to do here", False),
}


def _short_ref(ref: str) -> str:
    """The distinguishing TAIL of an op id — UUIDv7 shares its prefix."""
    parts = [p for p in str(ref).split("-") if p]
    return "-".join(parts[-2:]) if len(parts) >= 3 else str(ref)


def _voice(action: str) -> tuple:
    return ACTION_VOICE.get(action, (f"routing that to {action}", False))


def acknowledge(action: str, message: str = "") -> str:
    """The one line that says O+V heard you.

    Deliberately does NOT echo the operator's message back. They just typed
    it; repeating it under a field label is what made the old output read like
    a form submission receipt rather than a reply.
    """
    phrase, _deferred = _voice(action)
    if not phrase:
        return ""
    return f"⏺ {phrase}"


def _dispatched_now(receipt: str) -> bool:
    """Did this goal actually START, or was it filed?

    An op-id means intake accepted it and a worker has it. A `chat:` task-id
    means it landed in backlog.json for the Backlog sensor to collect. Read
    off the receipt the executor returns, because the executor is the only
    layer that knows which path it took.
    """
    ref = str(receipt or "").strip()
    return bool(ref) and (ref.startswith("op-") or ref.startswith("op_"))


def _queue_note(action: str, receipt: str = "") -> Optional[str]:
    """Say plainly when work is queued rather than started.

    ``backlog_dispatch`` hands the goal to a sensor that polls. "logged" hides
    that; silence hides it worse. An operator staring at an unchanged screen
    needs to know whether to wait or to worry.
    """
    _phrase, deferred = _voice(action)
    if not deferred or _dispatched_now(receipt):
        return None
    ref = f" ({receipt})" if receipt else ""
    return (f"⎿ queued{ref} — the Backlog sensor will pick it up on its "
            f"next sweep")


def trace_lines(
    turn_id: str = "",
    session_id: str = "",
    intent: str = "",
    confidence: Optional[float] = None,
    reasons: Sequence[str] = (),
    reason: str = "",
    indent: str = "  ",
) -> List[str]:
    """The classifier's reasoning, as VERBOSE trace.

    Not deleted — routing bugs are diagnosed from exactly these fields. Marked
    ``·`` so the severity ladder in §05 filters it the way it filters every
    other telemetry line, and ``/breadcrumbs verbose`` brings it back.

    One line, not six. Six lines of internals outweigh the answer they
    accompany no matter how they are styled.
    """
    bits: List[str] = []
    if intent:
        conf = f" {confidence:.2f}" if isinstance(confidence, (int, float)) else ""
        bits.append(f"{intent.lower()}{conf}")
    if reasons:
        bits.append("/".join(str(r) for r in reasons))
    elif reason:
        bits.append(str(reason))
    if turn_id:
        # The TAIL of the id: chat turn ids share a prefix, so the leading
        # characters distinguish nothing (same lesson as the op-id digest).
        bits.append(turn_id.rsplit("-", 1)[-1])
    if not bits:
        return []
    return [f"{indent}· {' · '.join(bits)}"]


def compose_reply(
    action: str,
    *,
    message: str = "",
    receipt: str = "",
    social_reply: str = "",
    steps: Sequence[str] = (),
    turn_id: str = "",
    session_id: str = "",
    intent: str = "",
    confidence: Optional[float] = None,
    reasons: Sequence[str] = (),
    reason: str = "",
    verbose: bool = False,
) -> str:
    """The whole reply: acknowledgement, work, then trace if asked for.

    ``verbose`` is the ``/breadcrumbs``-style floor. Default False, because
    the operator asking a question is the common case and the operator
    debugging the router is the rare one — and the old output had that
    backwards.

    NEVER raises: a formatting fault must not swallow a real response, so any
    failure degrades to the plain receipt.
    """
    try:
        lines: List[str] = []

        # A social turn's reply IS the response. Wrapping it in chrome would
        # make "Hey." look like a system event.
        if action == "social_ack" and social_reply:
            lines.append(f"⏺ {social_reply}")
        else:
            if _dispatched_now(receipt):
                # It is RUNNING. Say so, and hand the operator the ref the op
                # chrome will narrate under — so the ⏺/⎿ lines that follow
                # are recognisably the answer to what they just typed.
                lines.append("⏺ on it")
                lines.append(f"  ⎿ dispatched {_short_ref(receipt)} · "
                             f"immediate · watching")
            else:
                head = acknowledge(action, message)
                if head:
                    lines.append(head)

            for step in steps:
                text = str(step).strip()
                if text:
                    lines.append(f"  ⎿ {text}")

            note = _queue_note(action, receipt)
            if _dispatched_now(receipt):
                note = None                      # already said above
            if note:
                lines.append(f"  {note.lstrip()}")
            elif receipt and action != "noop" and not _dispatched_now(receipt):
                # Not repeated when dispatched: the ref is already on the
                # "dispatched …" line, and printing the full id underneath it
                # is the UUID-noise the digest work removed everywhere else.
                lines.append(f"  ⎿ {receipt}")

        if verbose:
            lines.extend(trace_lines(
                turn_id=turn_id, session_id=session_id, intent=intent,
                confidence=confidence, reasons=reasons, reason=reason,
            ))
        return "\n".join(l for l in lines if l.strip())
    except Exception:  # noqa: BLE001 — never lose a response to formatting
        logger.debug("[ChatStyle] compose degraded", exc_info=True)
        return str(receipt or social_reply or "")
