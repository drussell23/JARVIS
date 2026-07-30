"""How the organism knows what it just told you.

The transcript renders every line with equal authority. A test that actually
ran, an AST analysis, a model's assertion, a cap the code invented when a
scan timed out, and a value nobody ever established — identical glyph,
identical colour, identical confidence.

The organism is not ignorant of the difference. It computes it, carefully,
in at least two places:

    ReasonProvenance          stated / unstated / synthetic
    Advisory.blast_provenance measured / localized_lower_bound /
                              synthetic_cap / unknown

`ReasonProvenance`'s own docstring names the shared abstraction and the
consequence of dropping it:

    "the question is never only 'what is the value' but 'is this measured,
     or did we make it up'. A consumer that cannot tell a typed sentence
     from a code constant will eventually present one as the other."

Neither value reaches a render path. Both die one frame short of the eye
they were computed for — which is how a FATAL overlay came to show
`origin: ?` in the same green as everything it had actually measured.

Why this is not a parameter on `_op_line`
=========================================
The obvious fix — thread a `provenance=` argument through the render call —
puts the burden on every call site, of which there are hundreds, each
needing an author to decide. That is `/narrate`'s hardcoded flag list in a
new costume: correct on the day it is written and wrong the moment a
producer is added, with nothing to detect it.

So provenance is AMBIENT, exactly like the op-DAG causality context: a
producer declares its epistemic footing ONCE, at the boundary where it
knows it, and every line rendered inside inherits.

    with claiming(Provenance.MODELED):
        ...anything rendered in here is marked as a model's word,
           including across `await` — contextvars follow the task.

The render chokepoint reads the ambient value. No call site changes, and a
producer that says nothing is not silently promoted to "measured".

Scarcity is the whole design
============================
Marking every line would make the mark meaningless and the transcript
unreadable — the same rule that keeps green scarce in `semantic_tokens`,
where "when green is also chrome, a successful outcome stops being
visible."

So the mark is an EXCEPTION surface. What the organism observed or derived
renders clean, because that is what a transcript is presumed to be. The
mark appears only when a line is WEAKER than it looks: a model said it, the
code invented it, or nobody established it.

UNKNOWN is not UNSET
====================
The distinction that makes the default honest:

  * ``UNSET``   — nobody asked. Renders clean, and claims nothing.
  * ``UNKNOWN`` — a producer TRIED to establish provenance and failed.
                  Renders marked, and loudly.

Defaulting the unexamined to UNKNOWN would badge the whole transcript into
noise; defaulting it to OBSERVED would be the fabrication this module
exists to end. Not-asked and asked-and-failed are different facts, and only
one of them is a warning. This mirrors `advisor_locality`, where a `blast`
of provenance `unknown` goes NEUTRAL — never cached, never a BLOCK — rather
than borrowing the authority of a measurement it does not have.

NEVER raises. Losing a mark is cosmetic; losing the line is not.
"""
from __future__ import annotations

import contextlib
import contextvars
import enum
import logging
from dataclasses import dataclass
from typing import Dict, Iterator, Optional, Tuple

logger = logging.getLogger("Ouroboros.Provenance")

PROVENANCE_SCHEMA_VERSION = "provenance.v1"


class Provenance(enum.IntEnum):
    """How a claim came to be, ordered by epistemic strength.

    ``IntEnum`` so strength composes: when two provenances meet on one
    line — a derived summary of a model's prose — the WEAKER wins, because
    a chain is exactly as trustworthy as its softest link. An unordered
    enum would push that judgement into every producer.
    """

    #: The system watched it happen: a test ran, a file was read, a scan
    #: completed. The strongest thing a transcript can say.
    OBSERVED = 5
    #: Deterministic computation over observations — an AST walk, a diff,
    #: a count. No model, no guess, but one step removed from the event.
    DERIVED = 4
    #: A human said it. The highest authority for INTENT and no authority
    #: at all for fact — an operator's reason is testimony, not a
    #: measurement, and the two must not be rendered as the same kind of
    #: thing.
    STATED = 3
    #: A model said it. Plausible prose with no observation behind it.
    MODELED = 2
    #: The code invented it: a default, a cap, a fallback string. Real and
    #: recordable, and never evidence.
    SYNTHETIC = 1
    #: Asked, and could not be answered. Distinct from UNSET — see module
    #: docstring. This is the loudest mark there is.
    UNKNOWN = 0

    @property
    def label(self) -> str:
        return self.name.lower()


#: Sentinel for "nobody asked". Deliberately NOT a member of the ladder:
#: making it `UNKNOWN = 0` would collapse "unexamined" into "examined and
#: unresolved", which is the one distinction this module is built on.
UNSET: Optional[Provenance] = None

#: The provenances that render CLEAN. Everything else earns a mark. This is
#: the scarcity rule as data: a transcript is presumed to be what the
#: organism saw, so seeing needs no annotation and everything softer does.
_CLEAN: Tuple[Provenance, ...] = (Provenance.OBSERVED, Provenance.DERIVED)

#: Glyph + semantic ROLE per marked provenance. Roles, never colours — the
#: tier-aware resolution belongs to `semantic_tokens`, and a second palette
#: here would drift from it exactly as `serpent_flow._C` drifted from
#: `theme`.
_MARK: Dict[Provenance, Tuple[str, str]] = {
    Provenance.STATED: ("‹stated›", "neural"),
    Provenance.MODELED: ("‹model›", "provider"),
    Provenance.SYNTHETIC: ("‹synthetic›", "dim"),
    Provenance.UNKNOWN: ("‹unverified›", "heal"),
}


# ---------------------------------------------------------------------------
# Projection — the existing vocabularies, not replaced
# ---------------------------------------------------------------------------

#: Existing domain vocabularies → the shared ladder. A PROJECTION, in the
#: shape `semantic_tokens` already uses for cockpit-role → theme-semantic:
#: both source vocabularies are correct in their own domain and neither is
#: rewritten to suit this one. A translation table between two right
#: answers is smaller and safer than a migration of either.
#:
#: `unstated` is the subtle one. The DECISION was a human's; the reason
#: STRING is a fallback the code supplied. Provenance marks the claim, and
#: the claim here is the text — so it projects to SYNTHETIC, not STATED.
#: Presenting it as the operator's word is precisely the fabrication that
#: `_reject_args` was rewritten to stop.
_PROJECTION: Dict[str, Provenance] = {
    # ReasonProvenance (inline_approval)
    "stated": Provenance.STATED,
    "unstated": Provenance.SYNTHETIC,
    "synthetic": Provenance.SYNTHETIC,
    # Advisory.blast_provenance (operation_advisor / advisor_locality)
    "measured": Provenance.OBSERVED,
    "localized_lower_bound": Provenance.DERIVED,
    "synthetic_cap": Provenance.SYNTHETIC,
    "unknown": Provenance.UNKNOWN,
}


def project(raw: object) -> Optional[Provenance]:
    """Map a domain provenance onto the ladder. NEVER raises.

    Returns ``UNSET`` for anything unrecognised rather than guessing. A
    vocabulary this module has not been taught is not evidence of anything,
    and inventing a rung for it would be the fabrication in miniature.
    """
    try:
        if isinstance(raw, Provenance):
            return raw
        # str-enums (both existing vocabularies are `str, enum.Enum`)
        text = str(getattr(raw, "value", raw) or "").strip().lower()
        found = _PROJECTION.get(text)
        if found is not None:
            return found
        # This ladder's OWN labels must round-trip. Without it `project`
        # accepted every foreign vocabulary and rejected its own: only three
        # of the six rungs happen to share a spelling with a projected
        # domain value, so `"modeled"` resolved to UNSET and rendered
        # clean — a model's word presented as an observation, which is the
        # single failure this module exists to prevent.
        for member in Provenance:
            if text == member.label:
                return member
        return None
    except Exception:  # noqa: BLE001
        return None


def of(obj: object) -> Optional[Provenance]:
    """Read provenance off an object that already carries it. NEVER raises.

    Complements the ambient context: `Advisory` and `ApprovalResult` have
    computed the answer and stored it on themselves, so a renderer holding
    one should not need a `with` block to learn what it is already looking
    at. Field names are the ones those types actually use.
    """
    try:
        for attr in ("provenance", "blast_provenance", "reason_provenance"):
            if hasattr(obj, attr):
                found = project(getattr(obj, attr))
                if found is not None:
                    return found
        if isinstance(obj, dict):
            for attr in ("provenance", "blast_provenance",
                         "reason_provenance"):
                if attr in obj:
                    found = project(obj[attr])
                    if found is not None:
                        return found
        return None
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Ambient context
# ---------------------------------------------------------------------------

_ACTIVE: contextvars.ContextVar[Optional[Provenance]] = contextvars.ContextVar(
    "ouroboros_active_provenance", default=None,
)


def active() -> Optional[Provenance]:
    """The ambient provenance, or UNSET. NEVER raises."""
    try:
        return _ACTIVE.get()
    except Exception:  # noqa: BLE001
        return None


@contextlib.contextmanager
def claiming(prov: object) -> Iterator[Optional[Provenance]]:
    """Declare the epistemic footing of everything rendered inside.

    Restores via the contextvars TOKEN rather than by writing back the
    previous value: under concurrency the "previous value" read at entry
    may not be the one in effect at exit, and restoring it would leak one
    task's footing into another. The token is the only correct undo.

    Nested contexts take the STRICTER of the two. A model's prose quoted
    inside a measured block does not become measured by being nested, and
    a producer that wraps a weaker one should not have to know it did.

    NEVER raises — a failure to set context must not fail the work.
    """
    token = None
    try:
        resolved = project(prov)
        current = active()
        if resolved is not None and current is not None:
            resolved = min(resolved, current)      # weakest link wins
        elif resolved is None:
            resolved = current
        token = _ACTIVE.set(resolved)
        yield resolved
    except Exception:  # noqa: BLE001
        logger.debug("[Provenance] claiming degraded", exc_info=True)
        yield None
    finally:
        try:
            if token is not None:
                _ACTIVE.reset(token)
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Mark:
    """What a line's provenance adds to it, if anything."""

    provenance: Optional[Provenance]
    markup: str = ""

    @property
    def marked(self) -> bool:
        return bool(self.markup)


def mark_for(prov: object) -> Mark:
    """The markup suffix for ``prov``, or an empty Mark. Pure. NEVER raises.

    Empty for OBSERVED, DERIVED and UNSET — the scarcity rule. A transcript
    is presumed to be what the organism saw; annotating that presumption on
    every line would spend the reader's attention on the ordinary and leave
    nothing to spend on the exceptional.
    """
    try:
        resolved = project(prov)
        if resolved is None or resolved in _CLEAN:
            return Mark(resolved)
        glyph, role = _MARK.get(resolved, ("", ""))
        if not glyph:
            return Mark(resolved)
        try:
            from backend.core.ouroboros.ui.semantic_tokens import sem
            style = sem(role)
        except Exception:  # noqa: BLE001
            style = ""
        if not style:
            return Mark(resolved, f" {glyph}")
        return Mark(resolved, f" [{style}]{glyph}[/{style}]")
    except Exception:  # noqa: BLE001
        return Mark(None)


def annotate(text: str, prov: object = None) -> str:
    """Append the provenance mark to one rendered line. NEVER raises.

    Falls back to the ambient context when ``prov`` is not given, which is
    the common path: the producer declared once, at its boundary, and the
    render seam asks here without any call site having changed.

    Idempotent — a line that already carries a mark is returned untouched,
    so a seam reached twice (local console AND the cockpit mirror) cannot
    double-stamp it.
    """
    try:
        line = str(text if text is not None else "")
        resolved = project(prov) if prov is not None else active()
        m = mark_for(resolved)
        if not m.marked:
            return line
        for glyph, _role in _MARK.values():
            if glyph in line:
                return line
        return f"{line}{m.markup}"
    except Exception:  # noqa: BLE001
        return str(text if text is not None else "")


def legend() -> Tuple[Tuple[str, str, str], ...]:
    """``(label, glyph, meaning)`` per marked rung — for `/provenance`.

    Only the MARKED rungs: a legend listing entries the operator will never
    see on a line would describe the vocabulary rather than the surface.
    """
    meanings = {
        Provenance.STATED: "a human said it — intent, not measurement",
        Provenance.MODELED: "a model said it — no observation behind it",
        Provenance.SYNTHETIC: "the code invented it — a default or cap",
        Provenance.UNKNOWN: "asked, and could not be answered",
    }
    return tuple(
        (p.label, _MARK[p][0], meanings[p])
        for p in sorted(_MARK, reverse=True)
    )


__all__ = [
    "Mark",
    "PROVENANCE_SCHEMA_VERSION",
    "Provenance",
    "UNSET",
    "active",
    "annotate",
    "claiming",
    "legend",
    "mark_for",
    "of",
    "project",
]
