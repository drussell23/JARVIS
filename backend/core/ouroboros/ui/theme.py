"""backend/core/ouroboros/ui/theme.py -- the unified theme engine.

Single source of truth for O+V's CLI styling. Every component references a
*semantic token* (``accent``, ``muted``, ``danger`` ...) -- never a literal
color -- and the theme resolves that token to a concrete Rich style based on
the terminal's detected capability tier.

Design (spec 2026-07-06 §3):

* **One accent color** (restrained cyan-teal ``#3AAFA9``) degraded across
  four capability tiers: TRUECOLOR -> 256 -> 16 -> stripped.
* **One Console factory** (:func:`build_console`) -- the only place a themed
  :class:`rich.console.Console` is constructed. ``force_tier`` overrides
  detection for tests + the ``JARVIS_UI_THEME_FORCE_TIER`` debug override.
* **Glyph degradation** (:func:`mark`) -- unicode marks fall back to ASCII.
* **Zero escape leakage** at the NONE tier (bulletproof mandate #4): the
  console's ``color_system`` is ``None`` so Rich emits no ANSI at all.

Dependency rule: stdlib + Rich only. No ``governance``/``battle_test`` imports.
"""
from __future__ import annotations

import enum
import logging
import os
from typing import Mapping, Optional

from rich.console import Console
from rich.theme import Theme

logger = logging.getLogger("Ouroboros.UI.Theme")

#: Debug override -- force a specific tier regardless of terminal detection.
FORCE_TIER_ENV_VAR: str = "JARVIS_UI_THEME_FORCE_TIER"

#: The one brand accent, as a truecolor hex. Single point of retune.
ACCENT_HEX: str = "#3AAFA9"


# ===========================================================================
# Capability tiers
# ===========================================================================


class ColorTier(enum.IntEnum):
    """Terminal color capability, ordered least -> most capable.

    Ordered (IntEnum) so callers can compare, e.g. ``tier >= ColorTier.C256``.
    """

    NONE = 0       # NO_COLOR / pipe / dumb term -> styles stripped
    STANDARD = 1   # 8/16 color
    C256 = 2       # 256 color
    TRUECOLOR = 3  # 24-bit


_COLOR_SYSTEM_TO_TIER: Mapping[Optional[str], ColorTier] = {
    None: ColorTier.NONE,
    "truecolor": ColorTier.TRUECOLOR,
    "256": ColorTier.C256,
    "standard": ColorTier.STANDARD,
    "windows": ColorTier.STANDARD,
}

_TIER_TO_COLOR_SYSTEM = {
    ColorTier.NONE: None,
    ColorTier.STANDARD: "standard",
    ColorTier.C256: "256",
    ColorTier.TRUECOLOR: "truecolor",
}


def detect_tier(console: Console) -> ColorTier:
    """Map a console's ``color_system`` to a :class:`ColorTier`.

    Defensive: any unexpected value degrades to ``STANDARD`` (colored but
    conservative); a ``None`` color system is the NONE tier. NEVER raises.
    """
    try:
        cs = getattr(console, "color_system", None)
    except Exception:  # noqa: BLE001 -- never raise into a render path
        cs = None
    if cs in _COLOR_SYSTEM_TO_TIER:
        return _COLOR_SYSTEM_TO_TIER[cs]
    return ColorTier.STANDARD


def _forced_tier_from_env() -> Optional[ColorTier]:
    """Resolve ``JARVIS_UI_THEME_FORCE_TIER`` to a tier, or ``None``.

    Accepts the tier name (case-insensitive) or its integer value. Invalid
    values are ignored (auto-detect). NEVER raises.
    """
    raw = os.environ.get(FORCE_TIER_ENV_VAR)
    if not raw:
        return None
    key = raw.strip().upper()
    try:
        if key in ColorTier.__members__:
            return ColorTier[key]
        return ColorTier(int(key))
    except (ValueError, KeyError):
        return None


def supports_unicode(env: Optional[Mapping[str, str]] = None) -> bool:
    """True when the locale advertises a UTF-8 codec.

    Purely locale-env based so it is deterministic and testable: pass an
    explicit ``env`` mapping in tests. In production (``env=None``) it reads
    ``os.environ``. An env with no UTF-8 locale hint yields ``False`` -- the
    conservative choice that degrades glyphs to ASCII.
    """
    e = os.environ if env is None else env
    for key in ("LC_ALL", "LC_CTYPE", "LANG"):
        val = e.get(key, "") or ""
        if "utf" in val.lower():
            return True
    return False


# ===========================================================================
# Semantic tokens + per-tier resolution table
# ===========================================================================


class Token(str, enum.Enum):
    """Semantic style names. Components reference these, never colors."""

    ACCENT = "accent"     # interactive verbs, prompt glyph, active op-id
    HEADING = "heading"   # titles -- weight, not color
    BODY = "body"         # primary text (default fg)
    MUTED = "muted"       # subtitles, context line, separators, hints
    SUCCESS = "success"   # OUTCOMES ONLY (apply/verify OK) -- reserved
    WARNING = "warning"   # soft warnings
    DANGER = "danger"     # errors, rollback
    RULE = "rule"         # hairline separators


# Per-tier resolved Rich style strings. NONE maps everything to "" so the
# style is a no-op (and the console's color_system=None strips ANSI anyway).
_TOKEN_TABLE: Mapping[ColorTier, Mapping[Token, str]] = {
    ColorTier.TRUECOLOR: {
        Token.ACCENT: ACCENT_HEX,
        Token.HEADING: "bold",
        Token.BODY: "default",
        Token.MUTED: "grey50",
        Token.SUCCESS: "green",
        Token.WARNING: "yellow",
        Token.DANGER: "red",
        Token.RULE: "grey50",
    },
    ColorTier.C256: {
        Token.ACCENT: "color(73)",   # nearest xterm-256 to #3AAFA9
        Token.HEADING: "bold",
        Token.BODY: "default",
        Token.MUTED: "grey50",
        Token.SUCCESS: "green",
        Token.WARNING: "yellow",
        Token.DANGER: "red",
        Token.RULE: "grey50",
    },
    ColorTier.STANDARD: {
        Token.ACCENT: "cyan",
        Token.HEADING: "bold",
        Token.BODY: "default",
        Token.MUTED: "dim",
        Token.SUCCESS: "green",
        Token.WARNING: "yellow",
        Token.DANGER: "red",
        Token.RULE: "dim",
    },
    ColorTier.NONE: {t: "" for t in Token},
}


def style_for(token: Token, tier: ColorTier) -> str:
    """Resolve a semantic token to a concrete Rich style string for a tier.

    Returns ``""`` at the NONE tier (or for any unmapped combination). NEVER
    raises -- an unknown token degrades to empty rather than erroring a render.
    """
    return _TOKEN_TABLE.get(tier, {}).get(token, "")


def _theme_for(tier: ColorTier) -> Theme:
    """Build a Rich :class:`Theme` mapping every token name to its resolved
    style for the given tier. Empty styles become ``"none"`` (a valid null
    Rich style) so ``Theme`` construction never rejects them.
    """
    styles = {t.value: (style_for(t, tier) or "none") for t in Token}
    return Theme(styles)


# ===========================================================================
# Glyph marks (unicode -> ASCII degradation)
# ===========================================================================


#: (unicode, ascii) pairs. ASCII fallback preserves meaning without codepoints.
_GLYPHS: Mapping[str, tuple] = {
    "dot": ("·", "-"),      # middot separator
    "check": ("✓", "OK"),   # done
    "cross": ("✗", "X"),    # failed
    "arrow": ("›", ">"),    # prompt / pointer
    "rule": ("─", "-"),     # hairline
}


def mark(name: str, *, unicode: Optional[bool] = None) -> str:
    """Return a glyph by semantic name, degraded to ASCII when needed.

    ``unicode=None`` (default) consults :func:`supports_unicode`; pass an
    explicit bool in tests. An unknown mark name returns ``""``.
    """
    pair = _GLYPHS.get(name)
    if pair is None:
        return ""
    use_unicode = supports_unicode() if unicode is None else unicode
    return pair[0] if use_unicode else pair[1]


# ===========================================================================
# Console factory + primitives
# ===========================================================================


def build_console(
    *,
    force_tier: Optional[ColorTier] = None,
    **console_kwargs: object,
) -> Console:
    """Construct the one themed :class:`rich.console.Console`.

    This is the single chokepoint for console construction across the CLI --
    every surface should obtain its console here so styling stays uniform
    (DRY mandate #3).

    ``force_tier`` (explicit arg or the ``JARVIS_UI_THEME_FORCE_TIER`` env
    override) pins the tier and the console's ``color_system``; otherwise the
    tier is auto-detected from the constructed console and the matching theme
    is pushed. NEVER raises -- on any failure it returns a plain Console.
    """
    tier = force_tier if force_tier is not None else _forced_tier_from_env()
    try:
        if tier is not None:
            console = Console(
                color_system=_TIER_TO_COLOR_SYSTEM[tier],
                theme=_theme_for(tier),
                **console_kwargs,  # type: ignore[arg-type]
            )
        else:
            console = Console(**console_kwargs)  # type: ignore[arg-type]
            console.push_theme(_theme_for(detect_tier(console)))
        _mark_themed(console)
        return console
    except Exception:  # noqa: BLE001 -- construction must never crash the CLI
        logger.debug("[theme] build_console failed; plain fallback", exc_info=True)
        return Console(**console_kwargs)  # type: ignore[arg-type]


def _mark_themed(console: object) -> None:
    """Tag a console as already carrying the O+V theme (idempotency marker)."""
    try:
        setattr(console, "_ov_themed", True)
    except Exception:  # noqa: BLE001
        pass


def ensure_theme(console: Console) -> Console:
    """Push the tier-appropriate theme onto a console if it lacks one.

    Idempotent -- consoles built via :func:`build_console` are already tagged
    and skipped. Lets any consumer safely use ``[accent]`` markup regardless of
    how its console was constructed (DRY -- the render helpers call this).
    NEVER raises.
    """
    try:
        if getattr(console, "_ov_themed", False):
            return console
        push = getattr(console, "push_theme", None)
        if callable(push):
            push(_theme_for(detect_tier(console)))
            _mark_themed(console)
    except Exception:  # noqa: BLE001
        logger.debug("[theme] ensure_theme failed", exc_info=True)
    return console


def box_for(tier: ColorTier):
    """Return the box style for a tier: rounded when unicode is available,
    ASCII otherwise (bulletproof mandate #4). ``tier`` reserved for future
    per-tier box tuning. NEVER raises."""
    from rich import box
    try:
        return box.ROUNDED if supports_unicode() else box.ASCII
    except Exception:  # noqa: BLE001
        return box.ASCII


def render_panel(
    console: Console,
    body: object,
    *,
    token: Token = Token.MUTED,
    title: Optional[str] = None,
) -> None:
    """Draw a bordered panel styled by a semantic token.

    The single panel-drawing primitive -- consumers must not hand-roll their
    own Panel/box logic (DRY mandate #3). Box degrades to ASCII without
    unicode; border color degrades with the tier. NEVER raises.
    """
    from rich.panel import Panel
    ensure_theme(console)
    try:
        console.print(Panel(
            body,
            border_style=token.value,
            box=box_for(detect_tier(console)),
            title=title,
            expand=False,
            padding=(0, 2),
        ))
    except Exception:  # noqa: BLE001
        logger.debug("[theme] render_panel failed", exc_info=True)
        try:
            console.print(body)
        except Exception:  # noqa: BLE001
            pass


def render_rule(console: Console, label: Optional[str] = None) -> None:
    """Draw a width-measuring hairline separator via the ``rule`` token.

    Replaces every hardcoded ``"-" * N`` in the codebase: Rich measures the
    live console width. Degrades the rule glyph to ASCII when the locale lacks
    UTF-8. NEVER raises.
    """
    char = mark("rule") or "-"
    try:
        console.rule(label or "", characters=char, style="rule")
    except Exception:  # noqa: BLE001
        logger.debug("[theme] render_rule failed", exc_info=True)
        try:
            width = min(int(getattr(console, "width", 80) or 80), 80)
            console.print(char * width)
        except Exception:  # noqa: BLE001
            pass


__all__ = [
    "ACCENT_HEX",
    "FORCE_TIER_ENV_VAR",
    "ColorTier",
    "Token",
    "box_for",
    "build_console",
    "detect_tier",
    "ensure_theme",
    "mark",
    "render_panel",
    "render_rule",
    "style_for",
    "supports_unicode",
]
