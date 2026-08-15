"""``/why <ref>`` — the causal account, rendered for a person.

The engine (:mod:`why_engine`) produces the join. This renders it, and the
rendering is a real design constraint rather than a formatting detail: an
explanation an operator skips is worse than no explanation, because it was
offered and declined.

THE SHAPE IS FIXED
------------------
Four bands, same order, every time::

    ⏺ why o-12  op-019ff316…
      ⎿ TRIGGER   what woke it
      ⎿ CONTEXT   what it knew
      ⎿ LOGIC     how it reasoned
      ⎿ ACTION    what it did

Fixed order because an operator learns where to look once, and a layout that
reorders by what happens to be present makes them re-read it every time.
Always all four, because a band that vanished when empty would render "it
had no context" and "we did not record the context" identically — and those
are different answers.

The glyphs are the deck's own (``⏺``/``⎿``): this is one more op-scoped
block in a transcript full of them, not a new visual language.

CERTAINTY IS RENDERED, NOT DROPPED
-----------------------------------
``observed`` prints its items. ``tombstone`` says the records existed and
were evicted — a fact about a retention policy the operator can change.
``unknown`` says nothing was ever recorded. Three states, three renderings,
because the action each calls for is different.

Auto-discovered through the naming cage, like ``trust_repl`` and
``reach_repl``.

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, List, Optional

logger = logging.getLogger("Ouroboros.WhyRepl")

WHY_REPL_SCHEMA_VERSION: str = "why_repl.1"

__verb_help__ = {
    "why": "the causal account for a ref — trigger, context, logic, action",
}

_HELP = (
    "/why — why did the organism do that? (PRD §27.5)\n"
    "\n"
    "  /why <ref>          the causal account for one ref or op-id\n"
    "  /why <ref> --full   walk the whole ancestry, not one hop\n"
    "  /why help           this text\n"
    "\n"
    "Refs are the deck's own: o-12 (op block), d-7 (diff), t-3 (tool),\n"
    "n-4 (narrative), m-2 (milestone) — or a bare op-id.\n"
    "\n"
    "Deterministic: read from the transcript, never generated. It answers\n"
    "when every provider lane is dry, and it cannot invent a rationale.\n"
)

#: One line each, so the band names carry their meaning without a legend.
_BAND_GLOSS = {
    "trigger": "what woke it",
    "context": "what it knew",
    "logic": "how it reasoned",
    "action": "what it did",
}


@dataclass(frozen=True)
class WhyReplDispatchResult:
    ok: bool
    text: str
    matched: bool = True
    schema_version: str = WHY_REPL_SCHEMA_VERSION


def _render_band(band: Any) -> List[str]:
    from backend.core.ouroboros.governance.why_engine import Certainty

    gloss = _BAND_GLOSS.get(band.name, "")
    head = f"  ⎿ {band.name.upper():<9} {gloss}"
    if band.certainty is Certainty.OBSERVED:
        rows = [head]
        rows.extend(f"      {item}" for item in band.items)
        return rows
    if band.certainty is Certainty.TOMBSTONE:
        # Named as a structural node rather than as an absence: the system
        # knew this and discarded it on a policy the operator controls.
        return [head, f"      [tombstone — {band.detail}]"]
    return [head, f"      (unknown — {band.detail})"]


def render(explanation: Any) -> str:
    """The whole account as one block. Pure; no I/O."""
    lines: List[str] = []
    flight = "  · IN FLIGHT" if explanation.in_flight else ""
    lines.append(f"⏺ why {explanation.ref}  {explanation.op_id[:24]}{flight}")
    for band in explanation.bands:
        lines.extend(_render_band(band))

    if explanation.lineage:
        nearest = explanation.lineage[0]
        tail = ("  (…deeper ancestry — /why <that ref> --full)"
                if explanation.lineage_truncated else "")
        lines.append(f"  ⎿ CAUSED BY  {nearest}{tail}")
    if explanation.partial:
        # Louder than a footnote: a partial account can be read as a
        # complete one, and an operator acting on "it did nothing else" when
        # records were evicted is the failure this line prevents.
        lines.append("  ⎿ PARTIAL    the spine has evicted records for this "
                     "op — absence here is not evidence of absence")
    if explanation.loss_point:
        lines.append(f"  ⎿ DATA LOSS  {explanation.loss_point}")
    return "\n".join(lines)


def dispatch_why_command(line: str) -> WhyReplDispatchResult:
    """Explain one ref.

    Operator: ask the organism why it did something, and get the ledger's
    answer rather than a model's.
    """
    try:
        raw = (line or "").strip().lstrip("/")
        tokens = raw.split()
        args = [t for t in tokens[1:] if t]
        if not args or args[0] in ("help", "-h", "--help"):
            return WhyReplDispatchResult(ok=True, text=_HELP)

        full = any(a in ("--full", "-f") for a in args)
        positional = [a for a in args if not a.startswith("-")]
        if not positional:
            return WhyReplDispatchResult(
                ok=False,
                text="/why needs a ref — try `/why o-12` or `/why help`")

        from backend.core.ouroboros.governance import why_engine as we

        try:
            explanation = we.explain(
                positional[0], depth=(64 if full else None))
        except we.RefNotFound as exc:
            # A ref that resolves nowhere is REFUSED with the reason,
            # never answered with an empty account — an empty account
            # reads as "it did nothing", which is a different claim.
            return WhyReplDispatchResult(ok=False, text=str(exc))

        return WhyReplDispatchResult(ok=True, text=render(explanation))
    except Exception as exc:  # noqa: BLE001
        logger.debug("[WhyRepl] dispatch degraded", exc_info=True)
        return WhyReplDispatchResult(
            ok=False,
            text=(f"could not build the account ({type(exc).__name__}) — "
                  f"the transcript may be mid-write; try again"))


__all__ = [
    "WHY_REPL_SCHEMA_VERSION",
    "WhyReplDispatchResult",
    "dispatch_why_command",
    "render",
]
