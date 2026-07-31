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
           "is_contentless", "implementation_vocabulary"]

#: Openers that describe what the FUNCTION does rather than what the operator
#: gets. Stripped with their trailing connective so the remainder still reads
#: as a sentence. Ordered longest-first so "Parse and dispatch" does not lose
#: only its first word.
_IMPL_OPENERS = (
    "parse and dispatch", "parse and handle", "handle and dispatch",
    "parse", "handle", "dispatch", "implement", "process", "route",
    "execute", "perform", "run the", "entry point for", "entrypoint for",
    "helper for", "wrapper for", "callback for", "hook for",
    # Naming-cage scaffolding. "canonical entry point — auto-discovered"
    # describes how the REGISTRY finds the verb, which is a fact about the
    # dispatch convention and tells an operator nothing about what typing it
    # does. Left in, it surfaced as the description "Auto-discovered".
    "canonical entry point", "auto-discovered", "auto discovered",
)

#: Connectives left dangling after an opener is removed.
_DANGLING = re.compile(r"^(?:and\s+|then\s+|to\s+|the\s+|a\s+|an\s+)+", re.I)


#: A leading provenance citation — "§37 Slice 1 — ", "Upgrade 3 Slice 5 — ",
#: "M11 Slice 5 — ", "Treefinement Phase 4 — ". These modules DO describe
#: themselves; the description just sits behind a reference to the spec that
#: commissioned it. Thirty verbs read as undocumented for that reason alone,
#: and writing thirty fresh docstrings would have duplicated prose that was
#: already there and already accurate.
#:
#: Only stripped when what PRECEDES the dash looks like a citation — short, and
#: carrying a §/slice/phase/move/wave/upgrade marker or a bare identifier. A
#: blind "cut at the first dash" would decapitate "Posture — the organism's
#: current stance", which is a real sentence that happens to use one.
_CITATION = re.compile(
    r"^(?P<head>[^—-]{0,64}?)\s*[—–]\s*(?P<rest>.+)$", re.S,
)
_CITATION_MARKER = re.compile(
    r"§|\bslice\b|\bphase\s*\d|\bmove\s*\d|\bwave\s*\d|\bupgrade\s*\d"
    r"|\btier\s*\d|\bprd\b|\bstep\s*\d|^[A-Z]\d+\b|\bv\d+\b",
    re.I,
)

#: Openers describing the FILE's role rather than the verb's job.
_SURFACE_OPENERS = (
    "repl dispatcher for", "repl dispatcher", "repl surface composing",
    "repl surface for", "repl surface", "repl verb for", "repl verb",
    "operator-facing cli surface for", "operator-facing cli surface",
    "operator-facing surface for", "operator-facing surface",
    "operator surface for", "operator surface", "read-only inspection of",
    "dashboard for", "cli surface for", "cli surface",
)


#: Sphinx cross-reference roles — ``:mod:`pkg.mod.Class```. Unwrapping to the
#: raw target produced "Mod:component_health.ComponentHealthTracker", which is
#: not a description, it is a symbol path with a capital letter.
_RST_ROLE = re.compile(r":(?:mod|class|func|meth|attr|data|obj|ref):`([^`]+)`")

#: A TRAILING provenance parenthetical — "(PRD §38 Slice 3, 2026-05-07)".
#: The leading-citation stripper only looks at the head; these sit at the tail
#: and ate the description's budget with a reference the operator cannot use.
_TRAILING_CITATION = re.compile(
    r"\s*\((?=[^)]*(?:§|PRD|Slice|Phase|Wave|Tier|Pattern|\d{4}-\d{2}-\d{2}))"
    r"[^)]*\)\s*$", re.I,
)


def _humanise_symbol(match: "re.Match") -> str:
    """`:mod:`a.b.ComponentHealthTracker`` -> "component health tracker".

    The LAST segment is the meaningful one; the package path is address, not
    meaning. CamelCase and snake_case both become words so the result reads as
    prose rather than as an identifier that happens to be in a sentence.
    """
    target = str(match.group(1) or "").strip("~`")
    leaf = target.rsplit(".", 1)[-1]
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", leaf).replace("_", " ")
    return spaced.strip().lower()


def _strip_citation(text: str) -> str:
    """Drop a leading spec reference, keeping the sentence it introduces."""
    match = _CITATION.match(text.strip())
    if not match:
        return text
    head, rest = match.group("head"), match.group("rest")
    if head and not _CITATION_MARKER.search(head):
        return text            # a real sentence that merely contains a dash
    return rest.strip() or text


def _strip_surface_opener(text: str) -> str:
    """Drop 'REPL dispatcher' / 'operator-facing surface' scaffolding."""
    lowered = text.lower()
    for opener in _SURFACE_OPENERS:
        if lowered.startswith(opener):
            return text[len(opener):].strip(" .:,—-")
    return text


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
        if not stripped or len(stripped) < 12:
            return True
        if _CONTENTLESS.match(stripped):
            return True
        # The length floor is necessary but nowhere near sufficient: "Line and
        # dispatch" is 17 characters of pure machinery. A description has to
        # name something in the operator's world, not the function's.
        return not _has_domain_content(stripped)
    except Exception:  # noqa: BLE001
        return True



def implementation_vocabulary() -> frozenset:
    """Words that describe MACHINERY rather than the operator's world.

    Env-overridable, because what counts as machinery is codebase-specific and
    a fork should be able to say so without editing this file.
    """
    raw = os.environ.get("JARVIS_PALETTE_IMPL_WORDS", "").strip()
    if raw:
        return frozenset(w.strip().lower() for w in raw.split(",") if w.strip())
    return frozenset({
        # implementation verbs + their objects
        "parse", "parses", "dispatch", "dispatches", "dispatcher", "handle",
        "handles", "handler", "route", "routes", "router", "process",
        "processes", "execute", "executes", "run", "runs", "call", "calls",
        "invoke", "invokes", "return", "returns", "raise", "raises", "never",
        "line", "lines", "command", "commands", "subcommand", "subcommands",
        "verb", "verbs", "arg", "args", "argument", "arguments", "input",
        "result", "results", "entry", "point", "wrapper", "helper", "callback",
        "hook", "shim", "facade", "impl", "implementation", "function",
        # grammar
        "a", "an", "the", "and", "or", "to", "for", "of", "on", "in", "with",
        "from", "into", "this", "that", "it", "its", "is", "are", "be",
    })


def _has_domain_content(text: str) -> bool:
    """Does anything survive that is about the OPERATOR'S world?

    The load-bearing test, and it is structural rather than a blocklist.
    ``"Parse a ``/canvas`` line and dispatch."`` normalises to ``"Line and
    dispatch"`` — 17 characters, so it cleared the length floor and shipped as
    a description for months. Every word in it is machinery.

    Enumerating bad PHRASES would be whack-a-mole: the next template produces
    ``"Handle the command and return"`` and the palette lies again. Asking
    whether ANY word names something the operator cares about generalises to
    templates nobody has written yet.
    """
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9_]*", str(text or "").lower())
    if not words:
        return False
    vocabulary = implementation_vocabulary()
    return any(w not in vocabulary for w in words)

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
    # IDEMPOTENT: matches with OR without the leading slash.
    #
    # This is called twice — once before the dangling-article sweep and once
    # after — and the first pass rewrites "/attach a file" to "attach a
    # file". With `^/` required, the second pass no longer recognised the
    # acting form, fell through to the catch-all below, and deleted the word
    # the first pass had just protected. The result was "File or image to the
    # next generation": a fragment about files where an instruction to attach
    # one used to be, produced by the very rule written to prevent it.
    #
    # A transform applied more than once has to be a fixed point, or the
    # number of times it runs becomes part of its contract.
    acting = re.compile(
        rf"^/?{re.escape(name)}\b(?=\s+(?:a|an|the|to|for|with|from|into|on)\b)",
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
        raw = _RST_ROLE.sub(_humanise_symbol, raw)
        raw = re.sub(r"``([^`]*)``", r"\1", raw)
        raw = re.sub(r"`([^`]*)`", r"\1", raw)
        raw = " ".join(raw.split())
        # Citation first, THEN the verb name, THEN surface scaffolding. Order
        # matters: the verb name usually sits inside the clause the citation
        # introduces, so stripping the citation last leaves nothing to match.
        raw = _strip_citation(raw)
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
        head = _strip_surface_opener(head.strip())
        head = _strip_verb_name(head.strip(), verb)
        head = _DANGLING.sub("", head.strip())
        head = _TRAILING_CITATION.sub("", head).strip()
        head = head.strip(" .:;,—-")
        # A result that is ENTIRELY a parenthetical is a citation wearing
        # brackets — "(PRD §32.7 Pattern B)" told an operator nothing.
        if head.startswith("(") and head.endswith(")"):
            inner = head[1:-1].strip()
            head = inner if _has_domain_content(inner) and len(inner) >= 12 else ""

        if len(head) < 3:
            # Subtraction emptied it. That is a RESULT, not a failure: what
            # was removed was scaffolding, so there was no description here.
            #
            # This used to resurrect the whole docstring, which put
            # "/m10 REPL dispatcher. Operator-facing CLI surface" back on the
            # palette — the exact scaffolding the stripping had just correctly
            # identified. Returning empty lets the cascade fall through to the
            # module docstring and then to mined subcommands, both of which
            # say more than a restated function signature.
            return ""

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
