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
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Iterable, List, Optional, Tuple

logger = logging.getLogger("Ouroboros.VerbDescription")

__all__ = ["to_operator_voice", "describe_width", "prose_first_enabled",
           "is_contentless", "implementation_vocabulary",
           "Shape", "Assessment", "Candidate", "assess", "best_candidate",
           "shape_ceiling",
           "runtime_vocabulary"]

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

#: The openers above, anchored to a WORD boundary.
#:
#: ``str.startswith`` matched them as character prefixes, so "dispatch" fired
#: on the word "Dispatcher" and cut it mid-morpheme. ``/multi_prior``'s
#: docstring — "Dispatcher for ``/multi_prior`` REPL verb" — became the palette
#: row **"Er for /multi_prior REPL verb"**, which is not a description, a
#: usage, or even a word.
#:
#: The class of bug matters more than the instance: a subtractive rule that
#: operates below the token level can produce output no author wrote, which is
#: precisely the failure mode "SUBTRACTIVE only" was chosen to prevent. Every
#: remaining opener is at risk of the same thing — "process" inside
#: "processor", "route" inside "router", "run the" inside nothing but "parse"
#: inside "parser" — so this is fixed at the matcher, not per opener.
_IMPL_OPENER_RE = tuple(
    re.compile(rf"^{re.escape(o)}(?![\w-])", re.IGNORECASE)
    for o in sorted(_IMPL_OPENERS, key=len, reverse=True)
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
#:
#: ``^[A-Z]\d+$`` — the WHOLE head, not merely its first token. As ``^[A-Z]\d+\b``
#: this fired on "L3 execution graph", read the tier prefix as a citation and
#: threw the phrase away: /graph's row became "Units, edges and stats", a
#: sentence with no subject. A bare identifier is weak evidence of a citation
#: and only conclusive when it is ALL there is; every real citation in this
#: tree carries a second marker ("M11 Slice 5", "Upgrade 3 Slice 5") and still
#: matches on that.
_CITATION_MARKER = re.compile(
    r"§|\bslice\b|\bphase\s*\d|\bmove\s*\d|\bwave\s*\d|\bupgrade\s*\d"
    r"|\btier\s*\d|\bprd\b|\bstep\s*\d|^[A-Z]\d+$|\bv\d+\b",
    re.I,
)

#: Openers describing the FILE's role rather than the verb's job.
_SURFACE_OPENERS = (
    "repl dispatcher for", "repl dispatcher", "repl surface composing",
    "repl surface for", "repl surface", "repl verb for", "repl verb",
    "operator-facing cli surface for", "operator-facing cli surface",
    "operator-facing surface for", "operator-facing surface",
    # Bare, as well as compounded. /provider's module docstring opens
    # "operator-facing DoubleWord resilience dashboard" — the adjective is
    # true of every verb in this palette, so it distinguishes nothing and
    # costs 16 of the description's ~58 columns.
    "operator-facing", "operator facing",
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


def _strip_citation(text: str, verb: str = "") -> str:
    """Drop a leading spec reference or self-address, keeping what follows.

    Two ledes are empty for different reasons and both were being kept:
    one CITES the spec that commissioned the verb, the other RE-STATES the
    verb. ``_CITATION_MARKER`` only ever saw the first.
    """
    match = _CITATION.match(text.strip())
    if not match:
        return text
    head, rest = match.group("head"), match.group("rest")
    if head and not (_CITATION_MARKER.search(head)
                     or _is_vacuous_head(head, verb)):
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
    # ``(?![\w-])`` rather than ``\b``: a word boundary sits INSIDE a
    # hyphenated compound, so "embodied" matched the first half of
    # "Embodied-state" and ``[\s:,—-]*`` then ate the hyphen. The palette row
    # for /embodied read "State views: arch, aura, attention, portrait" — a
    # sentence about state, produced by decapitating a sentence about embodied
    # state. Same class as the opener bug above: a token-level rule applied
    # below the token level invents text nobody wrote.
    return re.compile(
        rf"^/?{re.escape(name)}(?![\w-])[\s:,—-]*", re.IGNORECASE,
    ).sub("", text, count=1)


#: Nouns that name the SURFACE rather than its subject. A lede built only from
#: these plus the verb's own name carries no information — it is an address.
_STRUCTURAL_NOUNS = frozenset({
    "repl", "cli", "tui", "verb", "verbs", "command", "commands", "dispatcher",
    "dispatch", "surface", "handler", "operator", "facing", "operator-facing",
    "entry", "point", "module", "view", "panel", "screen",
})

#: Grammatical glue. Present so a lede made ONLY of scaffolding is recognised
#: even when the scaffolding is joined into a phrase: "Dispatcher for
#: /multi_prior REPL verb" is five words of pure address, and without "for" in
#: this set it read as content and shipped as that verb's description.
_GLUE_WORDS = frozenset({
    "a", "an", "the", "and", "or", "for", "of", "to", "in", "on", "with",
    "from", "into", "this", "that", "its", "it", "is", "are", "be",
})

#: An argument placeholder in a usage lede — ``<target_path>``, ``[flag]``.
_PLACEHOLDER = re.compile(r"[<\[][^>\]]*[>\]]")


def _is_vacuous_head(head: str, verb: str) -> bool:
    """True when a dash-led lede is pure address, not content.

    ``_CITATION_MARKER`` recognises a lede that CITES a spec ("§38 Slice 4 —").
    It does not recognise one that merely re-states the surface, and those are
    just as empty::

        "``/enqueue_soak <target_path>`` — stage a crash-immortal Swarm soak"
        "``/cost`` REPL dispatcher — Slice 4 of the Per-Phase Cost arc"

    Both ledes reduce to the verb's own name plus scaffolding. Left in place
    they burned the description's whole width on an address the operator had
    just typed; ``/enqueue_soak`` showed "Async — walks the target off the
    event loop", having fallen through to the function docstring instead.

    Checked STRUCTURALLY — what survives after removing the verb name,
    placeholders and surface nouns — rather than by listing lede shapes. A
    blocklist of ledes is a blocklist of the ones already written.
    """
    try:
        text = _PLACEHOLDER.sub(" ", str(head or ""))
        name = str(verb or "").lstrip("/").strip().lower()
        words = [w for w in re.findall(r"[\w-]+", text.lower()) if w]
        if not words:
            return True
        for word in words:
            if word == name or word.replace("_", "") == name.replace("_", ""):
                continue
            if word in _STRUCTURAL_NOUNS or word in _GLUE_WORDS:
                continue
            return False        # something real survives — keep the lede
        return True
    except Exception:  # noqa: BLE001
        return False


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
        raw = _strip_citation(raw, verb)
        original = raw

        # First sentence only. A palette row is one line, and everything
        # after the first full stop is detail the operator did not ask for
        # while scanning.
        head = re.split(r"(?<=[.!?])\s+", raw, maxsplit=1)[0]

        for pattern in _IMPL_OPENER_RE:
            match = pattern.match(head)
            if match:
                head = head[match.end():]
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


# ===========================================================================
# Judgement — the organ the cascade never had
# ===========================================================================
#
# Every rung of the description cascade asked one question: "did this source
# return a non-empty string?" That is a test of PRESENCE, not of quality, and
# the two come apart constantly:
#
#   * "Er for /multi_prior REPL verb" is non-empty and is not a description;
#   * "Op fan-out tree" is a perfect description and was DISCARDED by a
#     four-word floor, so the palette showed "help · show · depth · status";
#   * "operator-facing DoubleWord resilience dashboard" sits in /provider's
#     module docstring, which the cascade banned wholesale — so the row read
#     "[undocumented]" while the sentence sat one scope up.
#
# Boolean acceptance also makes rank ABSOLUTE: residue from a high rung beats
# prose from a low one, and no better candidate can ever supersede a worse one
# that happened to arrive first. Every one of the defects above is that single
# design fact wearing different clothes.
#
# So sources stop deciding and start NOMINATING. Each produces a candidate,
# every candidate is classified by SHAPE and scored, and the best one wins —
# with source rank demoted from decision to prior. A description is now
# something the pipeline can recognise rather than something it assumes.


class Shape(str, Enum):
    """What a candidate string IS, independent of where it came from."""

    EMPTY = "empty"
    #: Text that survived subtraction as a fragment — a dangling connective, a
    #: decapitated word, a bare citation. Reads as help; answers nothing.
    RESIDUE = "residue"
    #: True prose about the FUNCTION rather than the verb. "Async — walks the
    #: target off the event loop" is accurate and useless to an operator.
    IMPLEMENTATION = "implementation"
    USAGE = "usage"
    #: A mined vocabulary, "help · show · depth · status". Honest, and strictly
    #: worse than a sentence — which is why it must be able to LOSE to one.
    SUBCOMMAND_LIST = "subcommand_list"
    #: A short contentful label: "Op fan-out tree", "Capability flag star-map".
    NOUN_PHRASE = "noun_phrase"
    PROSE = "prose"


#: Floor score per shape, and the span density may add on top. Overlapping
#: spans are deliberate: a dense noun phrase SHOULD beat thin prose, because
#: "Op fan-out tree" tells an operator more than "Show the thing for the op".
_SHAPE_FLOOR = {
    Shape.EMPTY: 0.00,
    Shape.RESIDUE: 0.05,
    Shape.IMPLEMENTATION: 0.20,
    Shape.USAGE: 0.30,
    Shape.SUBCOMMAND_LIST: 0.42,
    Shape.NOUN_PHRASE: 0.62,
    Shape.PROSE: 0.70,
}
_DENSITY_SPAN = 0.25

#: Below this, nothing is worth showing and the caller says so plainly.
ACCEPT_FLOOR = 0.30

#: A first word that cannot begin a description. Two families, both fragments:
#: connectives left by a stripped clause, and morphological debris left by a
#: stripped word-prefix ("Dispatcher" − "dispatch" = "er").
_FRAGMENT_HEADS = frozenset({
    "and", "or", "but", "then", "with", "to", "of", "for", "in", "on", "from",
    "by", "that", "which", "is", "are", "be", "was", "were", "as", "at", "if",
    "when", "its", "it", "returns", "return", "lets", "short-circuit", "also",
    "er", "ers", "ed", "ing", "es", "ion", "ment", "ly",
})

#: A last word that means the sentence was cut mid-thought.
_DANGLING_TAILS = frozenset({
    "by", "for", "with", "and", "or", "the", "a", "an", "to", "of", "in", "on",
    "from", "when", "that", "is", "are", "as", "at", "into", "über",
})

#: Machinery an operator never touches. Distinct from
#: `implementation_vocabulary`, which is about GRAMMAR and plumbing nouns:
#: these words can only appear in a sentence describing a RUNTIME mechanism,
#: so two of them together are near-proof the docstring is addressing a
#: maintainer. One alone is not — "L1 event emitter throughput" is a real
#: description that happens to contain "event".
_RUNTIME_WORDS = frozenset({
    "async", "await", "asyncio", "coroutine", "thread", "threading", "mutex",
    "singleton", "kwargs", "stdout", "stderr", "traceback", "monkeypatch",
    "subprocess", "idempotent", "memoised", "memoized", "lru", "gc",
})
_RUNTIME_PHRASES = ("event loop", "under the hood", "in place", "off the loop",
                    "fire and forget", "fire-and-forget", "no-op")


def runtime_vocabulary() -> frozenset:
    """Words that can only describe a RUNTIME mechanism.

    Env-overridable via ``JARVIS_PALETTE_RUNTIME_WORDS`` for the same reason
    `implementation_vocabulary` is: what counts as machinery is a property of
    the codebase, and a fork must be able to say so without editing this file.
    """
    raw = os.environ.get("JARVIS_PALETTE_RUNTIME_WORDS", "").strip()
    if raw:
        return frozenset(w.strip().lower() for w in raw.split(",") if w.strip())
    return _RUNTIME_WORDS


#: Subjects that name an AUDIENCE rather than an effect. "Tests can inject an
#: explicit governor and/or session_browser without touching the module
#: singletons" is fluent, specific, dense in domain words — and addressed to
#: whoever writes the tests. It scored as clean PROSE and became /cost's
#: palette row.
#:
#: Vocabulary cannot catch this one: every word is legitimate. The tell is
#: grammatical. A description says what the OPERATOR gets, so its subject is
#: never the reader, the caller, or the test suite — and a sentence that opens
#: by naming one of those is answering a different question than the palette
#: asked.
_MAINTAINER_SUBJECTS = frozenset({
    "tests", "test", "callers", "caller", "subclasses", "subclass",
    "implementors", "implementers", "maintainers", "consumers", "we", "you",
})

#: A verb-echo: the name plus scaffolding and nothing else. "/causal REPL",
#: "/continuity REPL dispatcher" — beside the verb in the left column this is
#: worse than a blank, because it LOOKS like help.
_ECHO_RE = re.compile(
    r"^[\w\-]+(\s+(repl|verb|dispatcher|surface|command|cli|handler))+$", re.I)

#: A candidate that is only a citation.
_BARE_CITATION_RE = re.compile(
    r"^\s*(§|slice\s+\d|phase\s+\d|prd\b|wave\s+\d|move\s+\d|path\s+[a-z]\.\d"
    r"|v\d+\.\d+|\d{4}-\d{2}-\d{2})", re.I)

#: Separators a mined vocabulary is joined with.
_LIST_SPLIT = re.compile(r"\s*[·|]\s*")


@dataclass(frozen=True)
class Assessment:
    """What a candidate is, how good it is, and WHY — the third field matters.

    Without ``reasons`` a rejected description is indistinguishable from an
    absent one, and the hygiene question ("which verbs still need a sentence,
    and what is wrong with what they have?") becomes unanswerable.
    """

    shape: Shape
    score: float
    reasons: Tuple[str, ...] = ()

    @property
    def acceptable(self) -> bool:
        return self.score >= ACCEPT_FLOOR


def _words(text: str) -> List[str]:
    return re.findall(r"[A-Za-z][A-Za-z0-9_'\-]*", str(text or "").lower())


def _density(words: Iterable[str]) -> float:
    """Fraction of words naming something in the OPERATOR's world."""
    items = list(words)
    if not items:
        return 0.0
    vocabulary = implementation_vocabulary()
    return sum(1 for w in items if w not in vocabulary) / float(len(items))


def _looks_like_list(text: str) -> bool:
    """A mined vocabulary rather than a sentence.

    Every segment short, no segment a clause. Checked on SHAPE so a list
    joined with a different separator tomorrow is still recognised, and so a
    real sentence containing a middot is not mistaken for one.
    """
    parts = [p.strip() for p in _LIST_SPLIT.split(text) if p.strip()]
    if len(parts) < 2:
        return False
    return all(len(p.split()) <= 2 and len(p) <= 18 for p in parts)


def assess(text: Optional[str], verb: str = "") -> Assessment:
    """Classify and score one candidate description. NEVER raises.

    Order is significant: the disqualifying shapes are tested first, because a
    fragment that happens to be dense in domain words ("Er for /multi_prior
    REPL verb" scores 3/5 on density) must not be rescued by its density.
    """
    try:
        raw = " ".join(str(text or "").split())
        if not raw or len(raw) < 8:
            return Assessment(Shape.EMPTY, 0.0, ("blank-or-too-short",))

        words = _words(raw)
        if len(words) < 2:
            return Assessment(Shape.EMPTY, 0.0, ("single-word",))

        reasons: List[str] = []

        # --- disqualifying shapes -----------------------------------------
        if not raw[0].isalnum() and raw[0] not in "\"'“‘(":
            return Assessment(Shape.RESIDUE, _SHAPE_FLOOR[Shape.RESIDUE],
                              ("leading-punctuation",))
        if words[0] in _FRAGMENT_HEADS:
            return Assessment(Shape.RESIDUE, _SHAPE_FLOOR[Shape.RESIDUE],
                              (f"fragment-head:{words[0]}",))
        if words[-1] in _DANGLING_TAILS:
            return Assessment(Shape.RESIDUE, _SHAPE_FLOOR[Shape.RESIDUE],
                              (f"dangling-tail:{words[-1]}",))
        if _ECHO_RE.match(raw.lstrip("/")):
            return Assessment(Shape.RESIDUE, _SHAPE_FLOOR[Shape.RESIDUE],
                              ("verb-echo",))
        if _BARE_CITATION_RE.match(raw):
            return Assessment(Shape.RESIDUE, _SHAPE_FLOOR[Shape.RESIDUE],
                              ("bare-citation",))
        if _is_vacuous_head(raw, verb):
            # The WHOLE candidate reduces to the verb's own name plus
            # scaffolding — the same emptiness `_strip_citation` removes from a
            # lede, here occupying the entire row.
            return Assessment(Shape.RESIDUE, _SHAPE_FLOOR[Shape.RESIDUE],
                              ("self-address",))

        density = _density(words)

        # --- informative but not a description ----------------------------
        if raw.lower().startswith("usage:"):
            return Assessment(Shape.USAGE,
                              _SHAPE_FLOOR[Shape.USAGE] + _DENSITY_SPAN * density,
                              ("usage-line",))
        if _looks_like_list(raw):
            return Assessment(Shape.SUBCOMMAND_LIST,
                              _SHAPE_FLOOR[Shape.SUBCOMMAND_LIST],
                              ("mined-vocabulary",))

        lowered = raw.lower()
        if words[0] in _MAINTAINER_SUBJECTS:
            # IMPLEMENTATION, not RESIDUE: it is well-formed prose, merely
            # addressed to the wrong reader, so it should still beat a bare
            # usage line if nothing better exists.
            return Assessment(Shape.IMPLEMENTATION,
                              _SHAPE_FLOOR[Shape.IMPLEMENTATION],
                              (f"maintainer-subject:{words[0]}",))
        runtime_hits = sum(1 for w in words if w in runtime_vocabulary())
        runtime_hits += sum(1 for p in _RUNTIME_PHRASES if p in lowered)
        if runtime_hits >= 2:
            return Assessment(Shape.IMPLEMENTATION,
                              _SHAPE_FLOOR[Shape.IMPLEMENTATION]
                              + _DENSITY_SPAN * density,
                              (f"runtime-vocabulary:{runtime_hits}",))
        if density < 0.34:
            # Almost every word is plumbing. "Line and dispatch" cleared the
            # old 12-character floor and shipped for months.
            return Assessment(Shape.IMPLEMENTATION,
                              _SHAPE_FLOOR[Shape.IMPLEMENTATION]
                              + _DENSITY_SPAN * density,
                              (f"low-density:{density:.2f}",))

        # --- a real description -------------------------------------------
        #
        # The split is LENGTH only, and deliberately so. Deciding "is this a
        # sentence or a label" needs a part-of-speech tagger; guessing at it
        # with a verb blocklist would reject "Show the activity radar" on one
        # list and accept "Op fan-out tree" on another for no principled
        # reason. Both are good rows; longer ones tend to carry more, so they
        # get a slightly higher floor and the tie resolves itself.
        shape = Shape.PROSE if len(words) >= 4 else Shape.NOUN_PHRASE
        if runtime_hits:
            reasons.append(f"runtime-vocabulary:{runtime_hits}")
        return Assessment(shape,
                          _SHAPE_FLOOR[shape] + _DENSITY_SPAN * density,
                          tuple(reasons) or (f"density:{density:.2f}",))
    except Exception:  # noqa: BLE001 — a palette must never throw
        logger.debug("[VerbDescription] assessment degraded", exc_info=True)
        return Assessment(Shape.EMPTY, 0.0, ("assessment-failed",))


@dataclass(frozen=True)
class Candidate:
    """One nomination, from one source.

    ``prior`` is how much the SOURCE is trusted, not how good the text is —
    an ``Operator:`` line was written for the person typing the verb, so it
    starts ahead of a docstring mined for the same information. It is a
    thumb on the scale, never a veto: a mangled authored line still loses to
    a clean derived one, which is the whole reason rank stopped deciding.
    """

    text: str
    source: str
    prior: float = 0.0
    assessment: Optional[Assessment] = field(default=None, compare=False)
    #: Deferred producer, for sources that cost real work to evaluate.
    supplier: Optional[Callable[[], str]] = field(default=None, compare=False)
    #: The HIGHEST weight this source could possibly reach. Only meaningful
    #: alongside ``supplier``; see :func:`best_candidate`.
    ceiling: float = 1.0

    def weight(self) -> float:
        return (self.assessment.score if self.assessment else 0.0) + self.prior


def shape_ceiling(shape: Shape) -> float:
    """The greatest score *shape* can reach. Exact, not an estimate.

    ``SUBCOMMAND_LIST`` returns its floor flat — a mined vocabulary carries no
    density bonus because its density is an artefact of which words the verb
    happens to accept. Everything else may add the full span.
    """
    if shape is Shape.SUBCOMMAND_LIST:
        return _SHAPE_FLOOR[shape]
    return _SHAPE_FLOOR[shape] + _DENSITY_SPAN


def best_candidate(candidates: Iterable[Candidate],
                   verb: str = "") -> Optional[Candidate]:
    """The best nomination, or ``None`` when none clears the floor.

    ``None`` is a real answer and the caller must be able to act on it: an
    honest "[undocumented]" is worth more than a confident fragment, because
    the operator acts on what the palette says.

    A candidate carrying a ``supplier`` is evaluated only if its ``ceiling``
    could still beat the best score so far. This is an EXACT bound, not a
    heuristic: a mined subcommand list can never exceed 0.42 and a usage line
    never 0.55, so once a real sentence is in hand the answer is provably
    unchanged by looking at either. The result is identical to evaluating
    everything — which matters, because the cheap version of this idea is
    "stop at the first source that answers", and that is the boolean
    acceptance the arbiter exists to replace. Skipping work whose outcome is
    determined is not the same as letting order decide.

    Worth the care: the arbiter nominates eagerly by default, and
    `mine_subcommands` costs an ``inspect.getsource`` plus an ``ast.parse``
    per verb — 340ms across the table, on a surface that renders between
    keystrokes.
    """
    try:
        scored: List[Candidate] = []
        best_weight = 0.0
        for cand in candidates:
            if cand is None:
                continue
            text = str(cand.text or "")
            if not text.strip() and cand.supplier is not None:
                if cand.ceiling + cand.prior <= best_weight:
                    continue        # outcome already determined
                try:
                    text = str(cand.supplier() or "")
                except Exception:  # noqa: BLE001
                    text = ""
            if not text.strip():
                continue
            evaluated = Candidate(
                text=text, source=cand.source, prior=cand.prior,
                assessment=assess(text, verb),
            )
            scored.append(evaluated)
            best_weight = max(best_weight, evaluated.weight())
        if not scored:
            return None
        # Ties break on the source's own order, which is why this is `max` over
        # a stable list rather than a sort: equal weights keep nomination
        # order, and callers nominate most-authored first.
        winner = max(scored, key=lambda c: c.weight())
        if winner.assessment is None or not winner.assessment.acceptable:
            return None
        return winner
    except Exception:  # noqa: BLE001
        logger.debug("[VerbDescription] arbitration degraded", exc_info=True)
        return None
