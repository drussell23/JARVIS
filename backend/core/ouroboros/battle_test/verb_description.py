"""What a verb is FOR, in one line, in the operator's voice.

The palette was answering the wrong question. Scanning it produced::

    /anticipate              help | panel | banners | prefetch | status
    /autobiography           help | status | refresh | commit | escapes
    /breadcrumbs             Parse /breadcrumbs and set/show the feed verbosity

Two different faults produced those three rows, and only one of them is a
bug.

`/anticipate` shows a subcommand list because it has NO PROSE AT ALL. The
resolution ladder in `repl_completion` already ranks prose above subcommands
— an operator section, then the docstring, THEN mined subcommands — so that
row is the honest bottom of the ladder, not a mis-ranking. The fix for it is
someone writing a sentence, and no amount of formatting substitutes.

`/breadcrumbs` is the actual defect. It HAS prose and the prose is in the
wrong voice: "Parse /breadcrumbs and set/show the feed verbosity" opens with
the verb the FUNCTION performs rather than the one the operator does, and
then repeats the verb name that is already the left-hand column — a third of
the line spent restating what they just read.

Normalising is deliberately SUBTRACTIVE. Implementation openers are stripped,
the verb's own name is stripped, the result is sentence-cased and unpunctuated.
Nothing is invented: no word appears that the author did not write. A palette
that paraphrases is a palette that can be confidently wrong, and the operator
acts on it.

    "Parse /breadcrumbs and set/show the feed verbosity"
        → "Set or show the feed verbosity"

When subtraction leaves nothing meaningful, the original survives untouched.
A slightly awkward true line beats a tidy invented one.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Optional

logger = logging.getLogger("Ouroboros.VerbDescription")

__all__ = ["to_operator_voice", "describe_width", "prose_first_enabled",
           "is_contentless"]

#: Openers that describe what the FUNCTION does rather than what the operator
#: gets. Stripped with their trailing connective so the remainder still reads
#: as a sentence. Ordered longest-first so "Parse and dispatch" does not lose
#: only its first word.
_IMPL_OPENERS = (
    "parse and dispatch", "parse and handle", "handle and dispatch",
    "parse", "handle", "dispatch", "implement", "process", "route",
    "execute", "perform", "run the", "entry point for", "entrypoint for",
    "helper for", "wrapper for", "callback for", "hook for",
)

#: Connectives left dangling after an opener is removed.
_DANGLING = re.compile(r"^(?:and\s+|then\s+|to\s+|the\s+|a\s+|an\s+)+", re.I)


def prose_first_enabled() -> bool:
    """Default ON. Off restores subcommands-before-prose ranking."""
    return os.environ.get(
        "JARVIS_PALETTE_PROSE_FIRST", "1",
    ).strip().lower() not in ("0", "false", "no", "off")


def describe_width() -> int:
    """Columns the description may occupy before it is clipped.

    A knob rather than a constant: the right width depends on terminal size
    and on how long the longest verb name is, and both vary per install.
    """
    try:
        return max(24, int(os.environ.get("JARVIS_PALETTE_DESC_WIDTH", "58")))
    except (TypeError, ValueError):
        return 58


#: What is left when a docstring described the FUNCTION and not the verb.
#: "Parse ``/anticipate`` line. NEVER raises." normalises to "Line" — which is
#: not a short description, it is the absence of one wearing a capital letter.
_CONTENTLESS = re.compile(
    r"^(line|a line|the line|dispatch|handler|command|subcommands?|"
    r"entry point|canonical entry point|result|handler result)$"
    r"|^/[\w -]+$"
    r"|^§|^slice \d|^prd |^path [a-z]\.\d", re.I,
)


def is_contentless(text: str) -> bool:
    """True when normalisation left no DESCRIPTION behind.

    A docstring existing is not the same as a description existing, and
    conflating them is how "78/78 verbs documented" was reported while the
    palette still showed subcommand lists. What survives has to say something
    about what the verb DOES; "Line" and "§32.11 Slice 4" do not.

    Returning True makes the resolver fall through to the next rung — a
    subcommand list is a poorer answer than a real sentence but a far better
    one than a confident fragment.
    """
    try:
        stripped = str(text or "").strip(" .:;,—-")
        return not stripped or len(stripped) < 12 or bool(
            _CONTENTLESS.match(stripped),
        )
    except Exception:  # noqa: BLE001
        return True


def _strip_verb_name(text: str, verb: str) -> str:
    """Deal with the verb's own name in the opening.

    Two cases, and conflating them destroys sentences:

    * ``"the /posture verb — status, override"`` — the name is being NAMED.
      It is already the left-hand column, so the whole phrase goes.
    * ``"/attach a file or image"`` — the name IS the sentence's verb. Only
      the slash goes. Deleting it here produced "File or image to the next
      generation", which reads as a fragment about files rather than an
      instruction to attach one.

    The discriminator is what FOLLOWS the name: an article or a preposition
    means it was doing grammatical work and must stay.
    """
    name = str(verb or "").lstrip("/").strip()
    if not name:
        return text
    named = re.compile(
        rf"^(?:the\s+)?/?{re.escape(name)}\s+verb\b[\s:,—-]*", re.IGNORECASE,
    )
    if named.search(text):
        return named.sub("", text, count=1)
    acting = re.compile(
        rf"^/{re.escape(name)}\b(?=\s+(?:a|an|the|to|for|with|from|into|on)\b)",
        re.IGNORECASE,
    )
    if acting.search(text):
        return acting.sub(name, text, count=1)
    return re.compile(
        rf"^/?{re.escape(name)}\b[\s:,—-]*", re.IGNORECASE,
    ).sub("", text, count=1)


def to_operator_voice(text: Optional[str], verb: str = "",
                      width: Optional[int] = None) -> str:
    """One line describing what *verb* is for. NEVER raises.

    SUBTRACTIVE only — no word appears that the author did not write. A
    palette that paraphrases can be confidently wrong, and the operator acts
    on it. When subtraction leaves nothing meaningful the original survives:
    a slightly awkward true line beats a tidy invented one.
    """
    try:
        raw = " ".join(str(text or "").split())
        if not raw:
            return ""
        # RST/markdown literal markup, unwrapped before anything else. Every
        # rule below matches on WORDS, and ``/anticipate`` is not a word —
        # the verb-name check silently failed on it, leaving "``/anticipate``
        # line" as the description. Docstrings in this codebase mark up verb
        # names by convention, so this is the common case, not an edge one.
        raw = re.sub(r"``([^`]*)``", r"\1", raw)
        raw = re.sub(r"`([^`]*)`", r"\1", raw)
        raw = " ".join(raw.split())
        original = raw

        # First sentence only. A palette row is one line, and everything
        # after the first full stop is detail the operator did not ask for
        # while scanning.
        head = re.split(r"(?<=[.!?])\s+", raw, maxsplit=1)[0]

        lowered = head.lower()
        for opener in _IMPL_OPENERS:
            if lowered.startswith(opener):
                head = head[len(opener):]
                break

        # Dangling articles are cleared BEFORE the name check as well as
        # after: "Handle the /posture verb" leaves "the /posture verb", and
        # the named-form pattern has to see it to recognise it.
        head = _DANGLING.sub("", head.strip())
        head = _strip_verb_name(head.strip(), verb)
        head = _DANGLING.sub("", head.strip())
        head = head.strip(" .:;,—-")

        if len(head) < 3:
            # Subtraction ate the sentence. Fall back to the author's words
            # rather than emit a fragment.
            head = original.strip(" .")

        # Sentence case: uppercase the first letter ONLY. Title-casing would
        # mangle identifiers, and lowering the rest would mangle `DW`, `L2`,
        # `APPROVAL_REQUIRED` and every other term that carries meaning in
        # its capitals.
        if head and head[0].islower():
            head = head[0].upper() + head[1:]

        if is_contentless(head):
            # The docstring described the function, not the verb. Say nothing
            # rather than something empty — the caller falls through to a
            # rung that at least tells the operator what the verb accepts.
            return ""
        limit = describe_width() if width is None else max(8, int(width))
        if len(head) > limit:
            head = head[: limit - 1].rstrip(" .,;:—-") + "…"
        return head
    except Exception:  # noqa: BLE001
        return " ".join(str(text or "").split())[: describe_width()]
