"""What a colour MEANS, resolved once, for every surface.

`ui/theme.py` owns colour: a hex `PALETTE`, a `_SEMANTIC_STD` map, and a
`ColorTier` ladder (NONE / STANDARD / C256 / TRUECOLOR) that resolves a
meaning to the best form a given terminal can actually show.

`serpent_flow._C` was a second, independent palette — a flat dict of
standard-ANSI names. So the two did not merely risk drifting apart: on a
truecolor terminal the theme-aware surfaces rendered hex while every
`_C`-coloured line rendered plain ANSI, at visibly different fidelity, in
the same session.

And 256 call sites bypassed both, writing bracketed colour names directly. The
question "what colour is a failure" was answered independently 256 times,
with nothing able to detect divergence.

The vocabulary was never the problem
====================================
`_C`'s roles are good, and map almost exactly onto Claude Code's scheme —
which is *semantic, not decorative*: the action verb is bold and
uncoloured, paths are cyan, green appears only for additions and
outcomes, yellow means "needs you", dim means metadata. The load-bearing
rule is SCARCITY: when green is also chrome, a successful outcome stops
being visible.

So this module does not invent a vocabulary. It gives the existing one a
single owner:

    role  ("death", "life", "file", …)   what the cockpit calls it
      ↓
    semantic ("crit", "ok", "info", …)   what the theme calls it
      ↓
    tier-resolved style                  what THIS terminal can show

`_C` becomes a projection of this, so no call site changes and nothing
breaks — but there is exactly one answer to every colour question, and it
is tier-aware everywhere instead of in half the codebase.

NEVER raises. A surface that cannot resolve a colour must still render
text; losing the colour is a cosmetic failure, losing the line is not.
"""
from __future__ import annotations

import logging
from typing import Dict, Optional

logger = logging.getLogger("Ouroboros.SemanticTokens")

SEMANTIC_TOKENS_SCHEMA_VERSION = "semantic_tokens.v1"

#: The structural floor. NOT "" — an empty style is not "unstyled": Rich
#: raises MarkupError on `[]text[/]` ("closing tag has nothing to close"),
#: so an unresolvable role rendered through an f-string would take down
#: the line it was decorating, and with it whatever coroutine was writing.
#: "none" is the only value that is simultaneously a valid Rich style and
#: visually absent. A styling typo must never be able to crash a render.
SAFE_FALLBACK_STYLE = "none"

#: Cockpit ROLE → theme SEMANTIC. The only place the two vocabularies are
#: connected. Roles are what the operator-facing code already says; the
#: semantics are what `theme` already resolves. Neither side is rewritten
#: to suit the other — a translation table between two existing, correct
#: vocabularies is smaller and safer than a migration of either.
_ROLE_TO_SEMANTIC: Dict[str, str] = {
    # outcomes — the scarce ones. Green means "succeeded" or "added" and
    # nothing else, which is what makes it readable at a glance.
    "life": "ok_bold",      # the organism evolved — emphatic
    "success": "ok",        # a step succeeded — plain
    "code_add": "ok",
    # failure
    "death": "crit",
    "code_del": "crit",
    # needs-you
    "heal": "warn",
    # thinking / structure
    "neural": "cyan",
    # Structural metadata RECEDES. A `@@ -410,7 +410,22 @@` header tells you where
    # a hunk sits; it is not the change and must not compete with it for attention.
    # It was `cyan` here and Pygments paints it magenta in the overlay — two loud
    # colours for a coordinate. `structural_dim` is the role that says "this is
    # scaffolding", so the code changes stay the focal point.
    # Both point at the SEMANTIC, not at each other: `_ROLE_TO_SEMANTIC` is
    # role -> semantic, and chaining `code_hunk -> structural_dim` made `style_for`
    # resolve a role name it does not know and return "none" — an invisible header.
    "code_hunk": "verbose",
    "structural_dim": "verbose",
    # external brains
    "provider": "venom_purple",
    # goal lifecycle. Previously written as bare `blue` / `magenta` and
    # deferred from the mechanical pass precisely because no existing role
    # fit: mapping a goal's TAGS to `provider` would have said the tag came
    # from an external brain.
    # elevated severity — the missing concept behind 21 `bright_*`
    # literals no existing role could absorb.
    "alert": "alert",           # emphatic warning, wants the eye NOW
    "highlight": "highlight",   # picked out of surrounding text
    "milestone": "info",        # a goal reached completion
    "annotation": "venom_purple",   # labels attached to a goal
    # metadata — ids, costs, timings. The most common role, deliberately
    # the quietest.
    "dim": "muted",
    "border": "faint",
    "verbose": "verbose",
    # paths get their own treatment below (underline is not a colour).
    "file": "info",
}
#: Background tints for diff bands. Separate roles because a colour that reads
#: correctly as FOREGROUND does not read correctly as BACKGROUND: `code_add`
#: resolves to a foreground green, and `on green` is a saturated slab that makes
#: every syntax colour drawn over it illegible. These are deliberately dark, so a
#: keyword, a string and a comment all keep their own hue on top of the band —
#: which is the whole point of highlighting a hunk rather than tinting it.
#:
#: Hex rather than an ANSI name: the 16-colour names have no dark variants, and a
#: band is one of the few places the exact luminance matters more than the
#: terminal's palette preference. `style_for` passes hex through unchanged.
_ROLE_BACKGROUNDS: Dict[str, str] = {
    "code_add_bg": "#16261a",
    "code_del_bg": "#2b1618",
}

#: Roles whose rendering carries a non-colour attribute. Kept separate
#: because a tier that cannot do colour can still do underline, and
#: conflating them loses the affordance on exactly the terminals that
#: need it most.
_ROLE_ATTRS: Dict[str, str] = {
    "file": "underline",
}

def semantic_for(role: str) -> str:
    """The theme semantic a cockpit role maps to. Pure. NEVER raises."""
    try:
        return _ROLE_TO_SEMANTIC.get(str(role or "").strip().lower(), "ink")
    except Exception:  # noqa: BLE001
        return "ink"


def style_for(role: str, *, tier: Optional[object] = None) -> str:
    """The resolved style string for ``role`` on this terminal.

    Asks `theme` rather than restating it, so a palette change or a tier
    downgrade reaches every surface at once. NEVER raises, and returns ""
    when it cannot resolve — the caller's retained seed is the ONE place
    the historical literals live, so this module never holds a second
    copy of them. A missing colour is cosmetic; a missing line is not.
    """
    key = str(role or "").strip().lower()
    try:
        from backend.core.ouroboros.ui import theme as _theme
        semantic = semantic_for(key)
        resolved = ""
        # `theme` is the authority. Its accessor has changed shape before,
        # so ask in order of specificity rather than binding to one name.
        for attr in ("semantic_style", "style_for_semantic", "resolve_style"):
            fn = getattr(_theme, attr, None)
            if callable(fn):
                try:
                    resolved = str(fn(semantic) or "")
                except Exception:  # noqa: BLE001
                    resolved = ""
                if resolved:
                    break
        if not resolved:
            table = getattr(_theme, "_SEMANTIC_STD", None)
            if isinstance(table, dict):
                resolved = str(table.get(semantic) or "")
        if not resolved:
            resolved = SAFE_FALLBACK_STYLE
        attr_extra = _ROLE_ATTRS.get(key, "")
        if attr_extra and attr_extra not in resolved:
            resolved = f"{resolved} {attr_extra}".strip()
        return resolved
    except Exception:  # noqa: BLE001
        logger.debug("[SemanticTokens] resolve degraded for %r", key,
                     exc_info=True)
        return SAFE_FALLBACK_STYLE


def sem(role: str) -> str:
    """Short alias — the call-site spelling.

    Named for the QUESTION ("what does this mean") rather than the answer
    ("red"), which is the whole point: a call site that says `sem("death")`
    keeps meaning the right thing when the palette changes, and one that
    says a literal colour does not.
    """
    return style_for(role)


def role_palette() -> Dict[str, str]:
    """Every role → its resolved style. The projection `_C` becomes.

    Built fresh rather than cached: `ColorTier` is a property of the
    terminal, and a cached palette computed during import would outlive a
    resize, a `--no-color` flip, or a client attaching from a different
    terminal than the daemon booted on. The cost is a dict comprehension
    over a dozen keys.
    """
    try:
        resolved = {role: style_for(role) for role in _ROLE_TO_SEMANTIC}
        # Backgrounds bypass `style_for`: they are literal tints, not
        # semantics to be resolved against a ColorTier.
        resolved.update(_ROLE_BACKGROUNDS)
        return resolved
    except Exception:  # noqa: BLE001
        return {}


__all__ = [
    "SAFE_FALLBACK_STYLE",
    "SEMANTIC_TOKENS_SCHEMA_VERSION",
    "role_palette",
    "sem",
    "semantic_for",
    "style_for",
]
