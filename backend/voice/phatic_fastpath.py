"""Zero-cost greetings — don't pay an LLM to say "I'm here".

The expensive hello
-------------------
"Hey Karen" carries no question. Routing it to a 30B model over the network
costs tokens, ~0.9s of TTFT, and the operator's patience — for an utterance
whose only informational content is *I am addressing you*. That is a routing
defect, not a prompt-tuning opportunity.

Why this is not a greeting list
-------------------------------
The temptation is ``if text in ("hi", "hello", "hey karen")``. That is the
same mistake as matching weather by substring, which in this codebase opened
the Weather app for "the brain is thinking" and "close the window". A list
fails open on everything it has not seen and fails closed on everything
phrased slightly differently.

The actual property is INFORMATIONAL. A phatic utterance is one that carries
no residual semantic payload once you remove:

  * the agent's own name (addressing, not content),
  * phatic markers — a real linguistic category, closed and small: the tokens
    English uses purely to open and maintain contact,
  * function words, which carry structure rather than meaning.

If nothing remains, the operator said "I am talking to you" and nothing else.
If ANYTHING remains — one content word — it is a real turn and goes to the
model. That generalises to phrasings never enumerated here ("yo karen you
around", "karen?", "are you there jarvis") and, more importantly, refuses to
swallow "karen what's my disk usage", which contains content and must not be
answered from a cache.

Bias is deliberately asymmetric: a missed phatic costs one unnecessary LLM
call; a FALSE phatic answers a real question with "I'm here", which is a lie.
So the classifier only fires when the residue is empty.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

_TRUTHY = ("1", "true", "yes", "on")


def fastpath_enabled() -> bool:
    """Master gate. OFF sends every turn to the model, which is the only
    honest way to compare cost and latency against this path."""
    return os.getenv("JARVIS_PHATIC_FASTPATH", "true").strip().lower() in _TRUTHY


#: Tokens English uses purely to establish or maintain contact. A closed
#: linguistic class, not a list of remembered greetings — which is why it
#: generalises to phrasings nobody enumerated.
_PHATIC_MARKERS = frozenset("""
hi hello hey heya yo hiya howdy greetings morning afternoon evening night
good goodmorning ok okay alright allright sup wassup whatsup
there here around awake alive up listening
please thanks thank cheers mate
test testing check checking hear me
""".split())

#: Function words: structure, not meaning.
_FUNCTION_WORDS = frozenset("""
a an the and or but so it its it's this that these those to of in on at for
with my your our their i me we they he she do does did done can could will
would shall should may might must have has had be been being was were
just now still again very really quite bit
are is am r u ya you
# ^ copulas and pronouns are STRUCTURE, not contact. Listing "is" as a
# phatic marker let the pure-fragment guard pass "is it the" — an utterance
# with no greeting in it at all — which would have answered a transcription
# artifact with "I'm here" instead of sending it on. The construction
# "are you there" is still phatic because "there" is the marker; the copula
# never was.
""".split())

_WORD = re.compile(r"[a-z0-9']+")


@dataclass(frozen=True)
class PhaticVerdict:
    """Why the classifier decided what it did — so a wrong call is legible."""

    is_phatic: bool
    residue: Tuple[str, ...]
    reason: str

    def __bool__(self) -> bool:
        return self.is_phatic


def _max_words() -> int:
    """Length ceiling. Above this the operator is saying something, whatever
    the individual tokens look like — a long utterance made entirely of
    function words is far more likely to be a transcription artifact than a
    greeting, and artifacts must reach the model rather than be answered."""
    try:
        return max(2, int(os.getenv("JARVIS_PHATIC_MAX_WORDS", "8")))
    except (TypeError, ValueError):
        return 8


def classify(text: str, *, agent_names: Sequence[str] = ()) -> PhaticVerdict:
    """Is this pure contact-establishment? NEVER raises.

    *agent_names* are stripped as ADDRESSING rather than content: saying an
    assistant's name tells it who you mean, not what you want."""
    try:
        raw = str(text or "").strip().lower()
        if not raw:
            return PhaticVerdict(False, (), "empty")

        tokens = _WORD.findall(raw)
        if not tokens:
            return PhaticVerdict(False, (), "no_tokens")
        if len(tokens) > _max_words():
            return PhaticVerdict(False, tuple(tokens), "too_long")

        # A question mark is a request even when every word is phatic:
        # "you there?" is contact, but "how are you doing today?" is a turn.
        names = {n.lower() for n in agent_names}
        name_tokens = {t for n in names for t in _WORD.findall(n)}

        residue = [
            t for t in tokens
            if t not in name_tokens
            and t not in _PHATIC_MARKERS
            and t not in _FUNCTION_WORDS
        ]
        if residue:
            return PhaticVerdict(False, tuple(residue), "has_content")

        # Guard against the degenerate case: an utterance made ONLY of
        # function words ("is it the") is not a greeting, it is a fragment,
        # and a fragment is usually a transcription artifact worth sending on.
        if not (set(tokens) & _PHATIC_MARKERS) and not (set(tokens) & name_tokens):
            return PhaticVerdict(False, (), "no_phatic_marker")

        return PhaticVerdict(True, (), "phatic")
    except (TypeError, re.error):
        return PhaticVerdict(False, (), "error")


# ---------------------------------------------------------------------------
# Response pool
# ---------------------------------------------------------------------------

#: Acknowledgements only. Every one of these is true regardless of what was
#: asked, because the fast path must never answer a QUESTION — it exists to
#: answer being addressed. Nothing here claims knowledge or takes an action.
_ACKS: Tuple[str, ...] = (
    "I'm here.",
    "Listening.",
    "Go ahead.",
    "Right here.",
    "Yep, listening.",
    "Here. What do you need?",
)


def _pool() -> Tuple[str, ...]:
    """Operator-supplied acknowledgements, else the defaults."""
    raw = os.getenv("JARVIS_PHATIC_RESPONSES", "").strip()
    if not raw:
        return _ACKS
    parts = tuple(p.strip() for p in raw.split("|") if p.strip())
    return parts or _ACKS


def acknowledge(text: str, *, operator: str = "", salt: str = "") -> str:
    """One acknowledgement, varied but DETERMINISTIC for a given utterance.

    Chosen by hashing the input rather than at random: an assistant that
    answers the same greeting differently on every repetition feels
    capricious, while one that never varies feels mechanical. Hashing gives
    variety across different greetings and stability for the same one, and it
    keeps tests exact without stubbing a random source.

    NEVER raises."""
    try:
        pool = _pool()
        key = f"{text or ''}|{salt}".encode("utf-8", "replace")
        idx = int(hashlib.sha256(key).hexdigest()[:8], 16) % len(pool)
        reply = pool[idx]
        who = (operator or "").strip()
        if who and reply.endswith("."):
            # Address them back when we know their name — resolved upstream,
            # never invented here.
            return f"{reply[:-1]}, {who}."
        return reply
    except (TypeError, ValueError, ZeroDivisionError):
        return _ACKS[0]


__all__ = [
    "PhaticVerdict",
    "acknowledge",
    "classify",
    "fastpath_enabled",
]
