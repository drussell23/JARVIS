"""PresentationRouter — the design-language gateway for the UI plane.

OV_DESIGN_LANGUAGE.md §6, operator-authorized 2026-07-18. Root cause of
aesthetic rot: disparate modules formatting their own output, each arc
minting private glyph vocabularies and leaking ``key=value`` telemetry
onto operator surfaces. The structural fix is a CHOKEPOINT: every
CLI-facing line pipes through :meth:`PresentationRouter.route_line`,
which

  1. **Scrubs glyphs** against the closed six-mark semantic ration
     (``theme._GLYPHS``: action/detail/voice/human/warn/audio) —
     legacy marks are ALIASED to their canonical meaning (``⛲``→warn),
     decorative emoji are stripped;
  2. **Recasts telemetry dumps** — a line carrying ≥2 raw ``key=value``
     pairs is re-voiced into the detail grammar (``key: value ·
     key: value``), so serialization-boundary leaks render as designed
     output instead of debug spew;
  3. **Normalizes density** — blank-line runs collapse to one; trailing
     whitespace dies;
  4. **Stays tier-adaptive** — glyph geometry comes from
     :func:`theme.mark`, so ASCII terminals get the same shape
     (``*``/``-``/``!``) the truecolor tier gets.

DRY: this module owns NO rendering — it composes ``theme.mark`` /
``supports_unicode`` and hands styled text back to whatever SerpentFlow
console the caller already writes to. Master
``JARVIS_PRESENTATION_ROUTER_ENABLED`` (default on); off = byte-identical
passthrough. Enforcement against bypass lives in
``tests/battle_test/test_presentation_ast_parity.py`` (the sentinel).
NEVER raises anywhere on the UI plane.
"""
from __future__ import annotations

import enum
import logging
import os
import re
import unicodedata
from typing import Optional

logger = logging.getLogger("Ouroboros.PresentationRouter")

_TRUTHY = ("1", "true", "yes", "on")


def router_enabled() -> bool:
    """Master gate — default ON. Off = byte-identical passthrough."""
    return os.environ.get(
        "JARVIS_PRESENTATION_ROUTER_ENABLED", "1",
    ).strip().lower() in _TRUTHY


# ---------------------------------------------------------------------------
# The semantic ration — closed enum over theme._GLYPHS names
# ---------------------------------------------------------------------------


class Glyph(str, enum.Enum):
    """The SIX operator-plane semantic marks (OV_DESIGN_LANGUAGE §2)."""

    ACTION = "action"
    DETAIL = "detail"
    VOICE = "voice"
    HUMAN = "human"
    WARN = "warn"
    AUDIO = "audio"


def _mark(g: "Glyph") -> str:
    try:
        from backend.core.ouroboros.ui.theme import mark
        return mark(g.value)
    except Exception:  # noqa: BLE001 — theme unavailable → bare text
        return ""


def canonical_glyph_chars() -> frozenset:
    """The unicode characters of the six rationed marks."""
    return frozenset("⏺⎿💭🗣⚠🎙")


#: Structure/typography set — allowed everywhere, not rationed (§2).
# ``▸`` is the Karen speaker mark (``💭 Karen ▸ …``) — registered
# typography since 2026-07-18 (it renders on the chat bridge, the
# attach persona-host line, and the answer engine; the sentinel was
# right to flag it unregistered).
TYPOGRAPHIC_CHARS = frozenset("·✓✗›─▸")

#: Legacy semantic marks → their canonical meaning. Aliased, not merely
#: stripped — the SIGNAL survives the sweep even where old emitters
#: haven't been re-voiced yet.
LEGACY_GLYPH_ALIASES = {
    "⛲": "⚠",
}


def _is_decorative_symbol(ch: str) -> bool:
    """True for emoji / pictographs outside the ration. Pure; NEVER
    raises. Uses codepoint ranges + unicode category — no hardcoded
    denylist of individual emoji (the set is open-ended by nature)."""
    try:
        cp = ord(ch)
        if ch in canonical_glyph_chars() or ch in TYPOGRAPHIC_CHARS:
            return False
        if ch in LEGACY_GLYPH_ALIASES:
            return False              # aliased separately, never dropped
        if 0x1F000 <= cp <= 0x1FAFF:  # emoji / symbols-extended planes
            return True
        if 0x2190 <= cp <= 0x2BFF and ch not in TYPOGRAPHIC_CHARS:
            # arrows / misc symbols / dingbats blocks — decorative on
            # the UI plane unless in the typographic set.
            return unicodedata.category(ch) in ("So", "Sk")
        if cp in (0xFE0F, 0x200D):    # variation selector / ZWJ debris
            return True
        return False
    except Exception:  # noqa: BLE001
        return False


def scrub_glyphs(text: str) -> str:
    """Alias legacy marks to canonical; strip decorative emoji; drop
    orphaned variation selectors. Pure; NEVER raises."""
    try:
        scrubbed_lines = []
        for line in str(text).split("\n"):
            stripped_lead = line.lstrip()
            lead_was_symbol = bool(stripped_lead) and _is_decorative_symbol(
                stripped_lead[0],
            )
            out = []
            for ch in line:
                if ch in LEGACY_GLYPH_ALIASES:
                    out.append(LEGACY_GLYPH_ALIASES[ch])
                elif _is_decorative_symbol(ch):
                    continue
                else:
                    out.append(ch)
            cleaned = "".join(out)
            # Collapse doubled interior spaces a strip left behind;
            # trailing residue always dies; LEADING space dies only when
            # the line's first glyph was the thing we stripped —
            # intentional indentation survives.
            cleaned = re.sub(r"(?<=\S)  +(?=\S)", " ", cleaned).rstrip()
            if lead_was_symbol:
                cleaned = cleaned.lstrip()
            scrubbed_lines.append(cleaned)
        return "\n".join(scrubbed_lines)
    except Exception:  # noqa: BLE001
        return str(text)


# ---------------------------------------------------------------------------
# Telemetry recast — key=value leaks become detail-voice
# ---------------------------------------------------------------------------


_KV_PAIR = re.compile(r"\b([A-Za-z_][\w.-]*)=(\S+)")


def looks_like_telemetry(line: str) -> bool:
    """≥2 raw ``key=value`` pairs on one line = a serialization leak.
    Pure; NEVER raises."""
    try:
        return len(_KV_PAIR.findall(str(line))) >= 2
    except Exception:  # noqa: BLE001
        return False


def recast_telemetry(line: str) -> str:
    """Re-voice ``key=value`` pairs into the detail grammar
    (``key: value`` joined by the middot). Words around the pairs are
    preserved. Pure; NEVER raises (degrades to the input)."""
    try:
        if not looks_like_telemetry(line):
            return line
        recast = _KV_PAIR.sub(lambda m: f"{m.group(1)}: {m.group(2)}", line)
        # Successive recast pairs separated by bare spaces read better
        # middot-joined: "a: 1 b: 2" -> "a: 1 · b: 2".
        recast = re.sub(
            r"(?<=\S) (?=[A-Za-z_][\w.-]*: )", " · ", recast,
        )
        return recast
    except Exception:  # noqa: BLE001
        return line


# ---------------------------------------------------------------------------
# The router
# ---------------------------------------------------------------------------


class PresentationRouter:
    """Chokepoint gateway — see module docstring. Stateless; cheap
    enough for every line of every verb."""

    def route_line(
        self,
        text: str,
        *,
        kind: Optional[Glyph] = None,
    ) -> str:
        """Return *text* conformed to the design language. ``kind``
        (optional) prefixes the semantic mark when the line doesn't
        already lead with one. Master-off = byte-identical passthrough.
        NEVER raises."""
        try:
            if not router_enabled():
                return text
            line = str(text)
            line = scrub_glyphs(line)
            line = recast_telemetry(line)
            line = line.rstrip()
            if kind is not None and line.strip():
                lead = line.lstrip()
                if not any(
                    lead.startswith(g) for g in canonical_glyph_chars()
                ):
                    m = _mark(kind)
                    if m:
                        line = f"{m} {line}"
            return line
        except Exception:  # noqa: BLE001 — UI plane never breaks
            return str(text)

    def route_block(self, text: str, *, kind: Optional[Glyph] = None) -> str:
        """Multi-line variant: per-line conformance + density rule §5
        (blank-line runs collapse to one). NEVER raises."""
        try:
            if not router_enabled():
                return text
            lines = [
                self.route_line(ln, kind=None) for ln in str(text).split("\n")
            ]
            out = []
            prev_blank = False
            for ln in lines:
                blank = not ln.strip()
                if blank and prev_blank:
                    continue
                out.append(ln)
                prev_blank = blank
            joined = "\n".join(out)
            if kind is not None:
                joined = self.route_line(joined.split("\n", 1)[0], kind=kind) + (
                    ("\n" + joined.split("\n", 1)[1]) if "\n" in joined else ""
                )
            return joined
        except Exception:  # noqa: BLE001
            return str(text)


_DEFAULT_ROUTER: Optional[PresentationRouter] = None


def get_default_router() -> PresentationRouter:
    """Process-global router (stateless — the singleton is a
    convenience, not shared state)."""
    global _DEFAULT_ROUTER
    if _DEFAULT_ROUTER is None:
        _DEFAULT_ROUTER = PresentationRouter()
    return _DEFAULT_ROUTER


__all__ = [
    "Glyph",
    "LEGACY_GLYPH_ALIASES",
    "PresentationRouter",
    "TYPOGRAPHIC_CHARS",
    "canonical_glyph_chars",
    "get_default_router",
    "looks_like_telemetry",
    "recast_telemetry",
    "router_enabled",
    "scrub_glyphs",
]
