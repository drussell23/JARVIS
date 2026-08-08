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
    # The dedup receipt grammar. Public because this module OWNS what a
    # receipt means, and the submitter (harness) and the executor both have
    # to speak it — importing from here is what stops a second, drifting copy.
    "make_dedup_receipt",
    "is_dedup_receipt",
    "with_filed",
    "dedup_lines",
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


def _logging_prefix() -> str:
    """The marker the safe-default executor stamps on a receipt for work it
    did NOT do. Read from the executor rather than restated here, so the two
    cannot drift into disagreeing about what "logged" looks like."""
    try:
        from backend.core.ouroboros.governance.chat_repl_dispatcher import (
            LoggingChatActionExecutor,
        )
        return str(getattr(LoggingChatActionExecutor, "LABEL_PREFIX", "logged-"))
    except Exception:  # noqa: BLE001 — dispatcher unavailable
        return "logged-"


def _was_discarded(receipt: str) -> bool:
    """Did the executor decline to do anything at all?

    ``LoggingChatActionExecutor`` — the safe default wired whenever
    ``JARVIS_CHAT_EXECUTOR_BACKLOG_ENABLED`` is off — logs the message and
    returns a synthetic token it deliberately prefixes ``logged-``. That
    prefix is the executor saying, in the only channel it has, "I did not do
    this."
    """
    return str(receipt or "").strip().startswith(_logging_prefix())


# ---------------------------------------------------------------------------
# The deduplication receipt
# ---------------------------------------------------------------------------
#
# `router.ingest` answers `deduplicated` when a goal's dedup_key was accepted
# inside the window. Before this, that verdict was collapsed into None by the
# submitter, which is the same value it returns when intake is UNREACHABLE —
# so the renderer could not tell "you already asked for this" from "nothing is
# listening", and said "queued … the Backlog sensor will pick it up" about
# both. True, and useless: it explains nothing an operator can act on.
#
# The fix is a state chain, not a string. The router exposes the facts it
# already holds (`describe_dedup_collision` → key, age, window — no second
# tracker), the submitter encodes them, the executor stamps whether the goal
# was ALSO filed, and this module — which already owns receipt interpretation
# — is the only layer that decides how any of it reads.
#
# Grammar is key=value rather than positional so a missing, extra or reordered
# field degrades one fact instead of misaligning all of them:
#
#     dedup:h=<sha256[:12]>;a=<age_s>;w=<window_s>;f=<0|1>
#
# Both producers import these helpers rather than restating the format, for
# the same reason `_logging_prefix()` reads its marker off the executor: two
# copies of a grammar are two grammars.

_DEDUP_PREFIX = "dedup:"


def make_dedup_receipt(
    short_hash: str, age_s: float, window_s: float, filed: bool = False,
) -> str:
    """Encode a deduplication collision as a receipt token. NEVER raises."""
    try:
        return (
            f"{_DEDUP_PREFIX}h={str(short_hash)[:12]};"
            f"a={float(age_s):.1f};w={float(window_s):.1f};"
            f"f={1 if filed else 0}"
        )
    except Exception:  # noqa: BLE001
        return f"{_DEDUP_PREFIX}h=;a=;w=;f={1 if filed else 0}"


def is_dedup_receipt(receipt: str) -> bool:
    """True iff this receipt reports a dedup collision rather than a dispatch
    or a filing. The executor asks this to decide whether it must STILL write
    the backlog safety net."""
    return str(receipt or "").strip().startswith(_DEDUP_PREFIX)


def with_filed(receipt: str, filed: bool) -> str:
    """Re-stamp the ``f`` flag. The submitter mints the collision but cannot
    know whether the backlog write later succeeded; the executor can, and
    only it may answer that. Non-dedup receipts pass through untouched."""
    if not is_dedup_receipt(receipt):
        return receipt
    fields = _parse_dedup(receipt)
    return make_dedup_receipt(
        fields.get("h", ""),
        _as_float(fields.get("a")), _as_float(fields.get("w")),
        filed=filed,
    )


def _as_float(raw: Any) -> float:
    try:
        return float(str(raw))
    except (TypeError, ValueError):
        return -1.0


def _parse_dedup(receipt: str) -> dict:
    """Tolerant parse. An unreadable field is absent, never fatal."""
    out: dict = {}
    try:
        body = str(receipt or "").strip()[len(_DEDUP_PREFIX):]
        for part in body.split(";"):
            if "=" in part:
                k, _, v = part.partition("=")
                out[k.strip()] = v.strip()
    except Exception:  # noqa: BLE001
        pass
    return out


def _humanize(seconds: float) -> str:
    """A duration said the way a person says it. Negative/unknown → ''."""
    try:
        s = float(seconds)
    except (TypeError, ValueError):
        return ""
    if s < 0:
        return ""
    if s < 2:
        return "moments"
    if s < 60:
        return f"{int(s)}s"
    if s < 3600:
        m, rem = divmod(int(s), 60)
        return f"{m}m{rem:02d}s" if rem else f"{m}m"
    h, rem = divmod(int(s), 3600)
    m = rem // 60
    return f"{h}h{m:02d}m" if m else f"{h}h"


def dedup_lines(receipt: str) -> List[str]:
    """Render a dedup collision as op chrome. Adaptive: every clause appears
    only when the fact behind it is actually known.

    Says "accepted", never "in flight". The registry stores ONE thing — when
    a key was last accepted — so a goal that has since completed or failed
    still collides. Claiming the earlier op is still running would be a state
    nothing measures, which is the defect class this whole arc exists to kill.
    """
    f = _parse_dedup(receipt)
    age, window = _as_float(f.get("a")), _as_float(f.get("w"))
    h = str(f.get("h", "") or "")

    bits: List[str] = ["Status: Deduplicated"]
    ago = _humanize(age)
    bits.append(
        f"an identical goal was accepted {ago} ago" if ago
        else "an identical goal was already accepted"
    )
    retry = _humanize(window - age) if (window >= 0 and age >= 0) else ""
    if retry:
        bits.append(f"re-runnable in {retry}")
    line = " · ".join(bits)
    if h:
        line += f" [hash: {h}]"

    out = [f"⎿ {line}"]
    # The safety net, stated only when it actually exists.
    if str(f.get("f", "")) == "1":
        out.append(
            "⎿ filed to the backlog as well — if that first run failed, the "
            "Backlog sensor still collects this one"
        )
    else:
        out.append(
            "⎿ NOT filed — this goal exists only as the earlier run; if that "
            "one failed, ask again after the window"
        )
    return out


def _queue_note(action: str, receipt: str = "") -> Optional[str]:
    """Say plainly what happened to the work. NEVER guess.

    ``backlog_dispatch`` has THREE outcomes and this used to render two:

      * an ``op-`` receipt — intake accepted it, a worker has it
      * a ``chat:`` receipt — filed in backlog.json for the sensor to collect
      * a ``logged-`` receipt — the executor is the safe-default logger and
        NOTHING happened

    The third fell through to the second, so an operator who typed a real
    request was told "queued — the Backlog sensor will pick it up on its next
    sweep" about an entry that was never written. Verified against the live
    tree: ``.jarvis/backlog.json`` was last modified in April and contained
    no chat entries at all.

    Three claims in one line, all false, about the operator's own work. The
    receipt already carried the truth; the renderer had two branches for
    three realities, which is the same defect as every other one in this
    arc — a surface reporting a state it did not measure — with the sharpest
    possible consequence, because this one eats the request and says thank
    you.
    """
    _phrase, deferred = _voice(action)
    if not deferred or _dispatched_now(receipt):
        return None
    if is_dedup_receipt(receipt):
        # A FOURTH reality, and the one this note would lie about hardest:
        # the goal was dropped as a duplicate, not filed-and-waiting. Handled
        # by `dedup_lines` in compose_reply; saying "queued" here would be the
        # same defect as the `logged-` case above.
        return None
    if _was_discarded(receipt):
        # Name the flag. An operator who has just watched their request
        # vanish should not have to grep for why.
        return ("⎿ NOT queued — the chat executor is the safe-default "
                "logger, so nothing was written. Set "
                "JARVIS_CHAT_EXECUTOR_BACKLOG_ENABLED=1 to make this real.")
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
            elif is_dedup_receipt(receipt):
                # Neither started nor merely filed. The head names what the
                # operator actually did — asked twice — because "adding that
                # to the backlog" would describe the safety net as if it were
                # the outcome.
                lines.append("⏺ you have already asked for this")
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
            if is_dedup_receipt(receipt):
                # The collision IS the op chrome for this turn — status,
                # measured age, when it becomes re-runnable, and whether the
                # backlog safety net exists. Rendered from the receipt's own
                # fields, so a fact absent from the receipt is absent here
                # rather than guessed.
                lines.extend(f"  {ln}" for ln in dedup_lines(receipt))
            elif note:
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
