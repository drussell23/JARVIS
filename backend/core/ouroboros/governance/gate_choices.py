"""One permission grammar, composed from what a prompt can actually do.

The controller behind every operator prompt accepts four verbs — ``/allow``,
``/always`` (allow + remember), ``/deny <reason>``, ``/pause``. What an
operator SEES is a hand-written string, and there were three of them:

    inline_prompt_gate_renderer.PROMPT_ACTIONS_HINT
        "/allow   /deny <reason>   /pause"          <- no /always
    inline_permission_repl ConsoleInlineRenderer
        "/allow   /deny <reason>   /always   /pause"
    serpent_flow Iron Gate
        "Apply this change? [Y/n]"                  <- a different grammar

Three surfaces, one authority, and they had already drifted: at a
phase-boundary prompt the operator is never told ``/always`` exists, even
though the dispatcher accepts it and the store persists it. A capability
built, tested, persisted, live — and unmentioned by the surface. That is
not a rendering nit; it is the reason nobody uses it.

Hand-written action strings cannot stay in sync, because nothing forces
them to. So this module removes the strings. A prompt declares what it can
DO, and the choice list is composed from that declaration; every renderer
renders the composed list. A fourth surface cannot drift, because there is
nothing left to write down.

Numbering is positional, and resolved against the set that was RENDERED
------------------------------------------------------------------------
A global ``{"2": ALWAYS}`` map would be wrong the moment a prompt cannot
offer ``/always`` — and it silently would be wrong in the dangerous
direction, turning "2" into a persisted grant on a prompt that never
offered one. So ordinals are assigned at compose time and resolved against
that instance. An out-of-range number matches NOTHING; it never falls
through to the convenient reading.

Labels state the TRUE scope
---------------------------
``/always`` is called always and is not: it stores a grant keyed by tool +
exact path (or exact bash command) + repo root, expiring in 30 days by
default. A label reading "always allow" would overstate the grant the
operator is agreeing to — the same class of defect as quoting a code
constant as the operator's stated reason. So the scope is derived from the
store's own match mode and TTL, and when it CANNOT be named truthfully the
option is omitted rather than guessed.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from backend.core.ouroboros.governance.inline_approval import (
    MAX_REASON_CHARS,
    ReasonProvenance,
    normalize_reason,
)

logger = logging.getLogger("Ouroboros.GateChoices")

GATE_CHOICES_SCHEMA_VERSION = "gate_choices.v1"

MASTER_FLAG_ENV_VAR = "JARVIS_NUMBERED_GATE_CHOICES_ENABLED"

#: Widest label rendered before the detail clause is dropped. A choice the
#: operator cannot read in one glance is not a choice.
_MIN_DETAIL_WIDTH = 46


def numbered_choices_enabled() -> bool:
    """Default ON. Off, renderers fall back to their verb-only hint.

    NEVER raises — a prompt that cannot decide how to render itself must
    still render.
    """
    try:
        return os.environ.get(
            MASTER_FLAG_ENV_VAR, "1",
        ).strip().lower() not in ("0", "false", "no", "off")
    except Exception:  # noqa: BLE001
        return True


# ---------------------------------------------------------------------------
# The choice model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateChoice:
    """One thing the operator may do about this prompt.

    ``verb`` is canonical and always typeable, so an operator who has
    learned ``/always`` never has to count rows, and every existing script,
    keybinding and test keeps working. The number is an ADDITION to the
    vocabulary, never a replacement for it.
    """

    verb: str
    label: str
    detail: str = ""
    aliases: Tuple[str, ...] = ()
    #: Does choosing this invite an explanation? Only rejection does — an
    #: approval needs no defence, and prompting for one trains operators to
    #: type nothing, which is how a reason capture dies.
    wants_reason: bool = False
    #: A real key binding, when one exists. Never invented: a hint for a
    #: key that is not bound is worse than no hint.
    hint: str = ""

    def matches(self, token: str) -> bool:
        """Does this token name this choice? NEVER raises."""
        try:
            tok = str(token or "").strip().lower().lstrip("/")
            if not tok:
                return False
            if tok == self.verb.lstrip("/").lower():
                return True
            return any(tok == a.lstrip("/").lower() for a in self.aliases)
        except Exception:  # noqa: BLE001
            return False


@dataclass(frozen=True)
class GateAnswer:
    """What the operator chose, and what they said about it.

    Carries :class:`ReasonProvenance` rather than a bare string for the
    reason the whole approval path now does: a reason separated from where
    it came from drifts, and a consumer holding one without the other has
    no way to ask before storing it as the human's stated wish.
    """

    choice: Optional[GateChoice]
    reason: str = ""
    provenance: ReasonProvenance = ReasonProvenance.UNSTATED
    #: How the operator expressed it — for §7 audit, and because "they
    #: typed 2" and "they typed /always" are worth telling apart when
    #: diagnosing a misclick.
    matched_by: str = ""

    @property
    def verb(self) -> str:
        return self.choice.verb if self.choice is not None else ""

    @property
    def is_stated(self) -> bool:
        return (self.provenance is ReasonProvenance.STATED
                and bool(self.reason))


@dataclass(frozen=True)
class GateChoiceSet:
    """The choices offered for ONE prompt, in the order rendered.

    Immutable, because the ordinal an operator is reading must still mean
    the same thing when their answer arrives. A set that recomposed between
    render and resolve would silently redefine "2" underneath them.
    """

    choices: Tuple[GateChoice, ...] = ()
    question: str = ""

    def __bool__(self) -> bool:
        return bool(self.choices)

    def ordinal(self, choice: GateChoice) -> int:
        """1-based position, or 0 when absent. NEVER raises."""
        try:
            return self.choices.index(choice) + 1
        except Exception:  # noqa: BLE001
            return 0


# ---------------------------------------------------------------------------
# Truthful scope description
# ---------------------------------------------------------------------------


def remembered_grant_ttl_days() -> float:
    """The store's OWN ttl, read from the store. NEVER raises.

    Asked rather than restated: a duplicated default drifts the first time
    someone tunes the real one, and the label would then promise a window
    the grant does not have.
    """
    try:
        from backend.core.ouroboros.governance import inline_permission_memory
        return float(inline_permission_memory._default_ttl_days())
    except Exception:  # noqa: BLE001
        try:
            return max(0.0, float(os.environ.get(
                "JARVIS_REMEMBERED_ALLOW_TTL_DAYS", "30")))
        except Exception:  # noqa: BLE001
            return 30.0


def describe_grant_scope(
    *, tool: str = "", target_path: str = "", arg_preview: str = "",
) -> Optional[str]:
    """What ``/always`` would ACTUALLY grant, or None if it cannot be said.

    ``None`` is the important return. A phase-boundary prompt's projection
    carries no ``tool``, so the scope genuinely is not knowable there — and
    an option offering to "not ask again" about something the operator
    cannot see the boundaries of is a worse failure than not offering it.
    The caller omits the choice rather than inventing a scope, exactly as
    an unmeasurable blast radius is reported unknown rather than capped.

    NEVER raises.
    """
    try:
        tool_name = " ".join(str(tool or "").split())
        if not tool_name:
            return None
        subject = " ".join(str(target_path or "").split())
        if not subject:
            subject = " ".join(str(arg_preview or "").split())
        if not subject:
            return None
        # The store matches on an EXACT path or an EXACT bash command, so
        # the label says so. "this file" would read as a directory grant.
        shape = "command" if tool_name.strip().lower() == "bash" else "path"
        shown = subject if len(subject) <= 48 else "…" + subject[-47:]
        days = remembered_grant_ttl_days()
        window = (f"{days:.0f}d" if days >= 1
                  else f"{max(1, int(days * 24))}h")
        return (f"don't ask again for {tool_name} on this exact "
                f"{shape} ({shown}) in this repo · {window}")
    except Exception:  # noqa: BLE001
        logger.debug("[GateChoices] scope description degraded", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Composition — the option list is DERIVED, never written down
# ---------------------------------------------------------------------------


def compose_gate_choices(
    *,
    question: str = "",
    grant_scope: Optional[str] = None,
    can_pause: bool = True,
    allow_label: str = "Yes",
    deny_label: str = "No",
    extra: Sequence[GateChoice] = (),
) -> GateChoiceSet:
    """Build the choices this prompt can honestly offer. NEVER raises.

    ``grant_scope`` is the OUTPUT of :func:`describe_grant_scope`, not a
    flag: passing the description itself makes it impossible to offer the
    remember-option while being unable to describe what it remembers.

    Allow and deny are always present — a prompt that cannot be answered
    either way is not a prompt. Everything else is conditional, so the
    number an operator types genuinely varies by what this prompt supports,
    which is exactly why resolution is positional against the instance.
    """
    try:
        out: List[GateChoice] = [
            GateChoice(verb="/allow", label=allow_label,
                       aliases=("y", "yes", "a", "approve")),
        ]
        if grant_scope:
            out.append(GateChoice(
                verb="/always", label=f"{allow_label}, and {grant_scope}",
                aliases=("always",),
            ))
        out.append(GateChoice(
            verb="/deny", label=deny_label,
            detail="and tell O+V what to do differently",
            aliases=("n", "no", "d", "reject"),
            # The ONE place a reason is invited. #70247 made a stated
            # reason storable; this is the affordance that produces one.
            wants_reason=True,
        ))
        if can_pause:
            out.append(GateChoice(
                verb="/pause", label="Decide later",
                detail="hold this and keep working",
                aliases=("p", "wait", "w", "defer"),
            ))
        out.extend(c for c in extra if isinstance(c, GateChoice))
        return GateChoiceSet(choices=tuple(out), question=str(question or ""))
    except Exception:  # noqa: BLE001
        logger.debug("[GateChoices] composition degraded", exc_info=True)
        return GateChoiceSet()


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_choices(
    choice_set: GateChoiceSet, *, width: Optional[int] = None,
    marker: str = "❯", indent: str = "    ",
) -> List[str]:
    """CC's numbered block. Pure. NEVER raises.

    The first choice carries the marker because it is the one an operator
    lands on; it is a POSITION, not a default — nothing is selected until
    something is typed, and :func:`resolve_answer` refuses to invent one.
    """
    try:
        if not choice_set or not numbered_choices_enabled():
            return []
        import textwrap
        cols = int(width) if width and int(width) > 0 else 80
        rows: List[str] = []
        if choice_set.question:
            rows.append(f"{indent}{choice_set.question}")
        for i, choice in enumerate(choice_set.choices, start=1):
            lead = f"{indent}{marker} " if i == 1 else f"{indent}  "
            body = choice.label
            if choice.detail:
                candidate = f"{body}, {choice.detail}"
                # The detail is the first thing to go on a narrow terminal:
                # it qualifies the choice, the label IS the choice.
                if len(lead) + len(f"{i}. {candidate}") <= max(
                        _MIN_DETAIL_WIDTH, cols - 2):
                    body = candidate
            if choice.hint:
                body = f"{body} {choice.hint}"
            # WRAPPED, never truncated. A `/always` label ends in the
            # qualifiers that bound the grant — "in this repo · 30d" — so
            # cutting the tail removes exactly the words that keep the
            # label honest and leaves one that reads BROADER than the
            # grant it describes. Same failure as an overstated blast
            # radius: the shortened form is the confident, wrong one.
            head = f"{lead}{i}. "
            hang = " " * len(head)
            wrapped = textwrap.wrap(
                body, width=max(24, cols - len(head)),
            ) or [body]
            rows.append(f"{head}{wrapped[0]}")
            rows.extend(f"{hang}{w}" for w in wrapped[1:])
        return rows
    except Exception:  # noqa: BLE001
        logger.debug("[GateChoices] render degraded", exc_info=True)
        return []


def render_verbs(choice_set: GateChoiceSet) -> str:
    """The one-line verb hint, derived from the SAME set.

    This replaces the hand-written ``PROMPT_ACTIONS_HINT`` strings. Their
    only defect was being written by hand — one of them had already lost
    ``/always`` — so the fix is not to correct them but to stop having
    them. NEVER raises.
    """
    try:
        if not choice_set:
            return ""
        parts = []
        for c in choice_set.choices:
            parts.append(f"{c.verb} <reason>" if c.wants_reason else c.verb)
        return "    actions  : " + "   ".join(parts)
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def resolve_answer(
    text: object, choice_set: GateChoiceSet, *,
    empty_means: Optional[GateChoice] = None,
) -> GateAnswer:
    """Interpret an answer against the choices ACTUALLY offered.

    Accepts, in order of specificity:

      * a positional number — resolved against THIS set, never a global map
      * a canonical verb (``/always``) or alias (``n``, ``yes``)
      * everything after the token, as the reason

    ``empty_means`` lets the prompt declare what a bare Enter does, for the
    reason the approval parser learned it: ``[Y/n]`` promises approve and
    the inline prompt promises wait, and a parser that picks for them
    silently breaks whichever surface it did not have in mind. Passing
    ``None`` — the default — means Enter chooses NOTHING, which is the
    safe reading when nobody has made a promise.

    An unmatched answer returns ``choice=None``. It never falls through to
    the first option, and never to the convenient one.

    NEVER raises.
    """
    try:
        raw = str(text or "")
        first_line = raw.splitlines()[0] if raw.strip() else ""
        parts = first_line.strip().split(None, 1)
        if not parts:
            return GateAnswer(choice=empty_means, matched_by="empty")
        token, rest = parts[0], (parts[1] if len(parts) > 1 else "")

        matched: Optional[GateChoice] = None
        how = ""
        # Numbers first: "2" is unambiguous where an alias could collide
        # with a future verb.
        stripped = token.strip().strip(".)").lstrip("/")
        if stripped.isdigit():
            idx = int(stripped)
            if 1 <= idx <= len(choice_set.choices):
                matched = choice_set.choices[idx - 1]
                how = f"ordinal:{idx}"
            else:
                # Out of range. NOT the nearest option, NOT the first —
                # an operator who typed 5 at a 4-choice prompt has not
                # chosen anything, and guessing which they meant is how a
                # misclick becomes a persisted grant.
                return GateAnswer(choice=None, matched_by=f"ordinal:{idx}?")
        if matched is None:
            for choice in choice_set.choices:
                if choice.matches(token):
                    matched = choice
                    how = f"verb:{choice.verb}"
                    break
        if matched is None:
            # Unrecognised. The words are attached to nothing — binding
            # them to a choice the operator did not make is how a typo
            # becomes a stored preference.
            return GateAnswer(choice=None, matched_by="unmatched")

        reason = normalize_reason(rest)
        if not reason:
            return GateAnswer(choice=matched, matched_by=how)
        return GateAnswer(
            choice=matched, reason=reason[:MAX_REASON_CHARS],
            provenance=ReasonProvenance.STATED, matched_by=how,
        )
    except Exception:  # noqa: BLE001
        logger.debug("[GateChoices] resolve degraded", exc_info=True)
        return GateAnswer(choice=None, matched_by="error")


__all__ = [
    "GATE_CHOICES_SCHEMA_VERSION",
    "MASTER_FLAG_ENV_VAR",
    "GateAnswer",
    "GateChoice",
    "GateChoiceSet",
    "compose_gate_choices",
    "describe_grant_scope",
    "numbered_choices_enabled",
    "remembered_grant_ttl_days",
    "render_choices",
    "render_verbs",
    "resolve_answer",
]
