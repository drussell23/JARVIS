"""The serpent runs the prompt's hairlines, chasing the prey.

The boot crest already tells this story: a snake travelling a closed path
after a `+`, catching it, and going round again. `crest_animator` owns the
laws — `_prey_plus_hit` for the prey's shape, `_prey_rgb` for its pale-core
-to-venom-purple gradient — and renders them into a raster.

This is the same story on a different geometry. The cockpit frames its input
with two hairlines rather than a box, and two hairlines are a closed path:
left→right along the top, right→left along the bottom, and round. A cycle of
``2 × width`` cells, which is all a chase needs.

Clock-stateless, like `ouroboros_frame`
----------------------------------------
Every frame is a pure function of ``(t, width)``. No tick task, no state, no
coordination: any number of readers at any instant derive the same frame, and
a dropped repaint costs one frame rather than desynchronising an animation
from its own clock. That property is why the spinner could be consumed by
three surfaces without any of them owning it, and it is worth more here —
this one is redrawn on every keystroke.

Resize is free for the same reason. The path length is derived from the width
handed in at render time, so a SIGWINCH mid-chase simply produces a frame for
the new geometry; there is no cached path to invalidate.

Decoration that knows it is decoration
---------------------------------------
It animates only while the organism is WORKING. A border that moves forever
is a distraction that teaches the operator to stop seeing the border, and the
one thing this line has to do is frame the place they type. Idle renders the
plain hairline, byte-identical to what shipped before this existed.

NEVER raises. A chase that cannot be computed renders as the hairline it
decorates.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

logger = logging.getLogger("Ouroboros.SerpentRule")

SERPENT_RULE_SCHEMA_VERSION = "serpent_rule.v1"

MASTER_FLAG_ENV_VAR = "JARVIS_SERPENT_RULE_ENABLED"
SPEED_ENV_VAR = "JARVIS_SERPENT_RULE_SPEED"
CHASE_ENV_VAR = "JARVIS_SERPENT_RULE_CHASE_S"
BODY_ENV_VAR = "JARVIS_SERPENT_RULE_BODY"

#: Rows of the path. 0 = the rule above the caret, 1 = the rule below it.
TOP, BOTTOM = 0, 1

#: Narrower than this and the path is too short to read as motion — the
#: serpent would appear to teleport rather than travel.
_MIN_WIDTH = 12


def serpent_enabled() -> bool:
    """Default ON. Off renders the plain hairline. NEVER raises."""
    return os.environ.get(
        MASTER_FLAG_ENV_VAR, "1",
    ).strip().lower() not in ("0", "false", "no", "off")


def _env_float(name: str, default: float, lo: float, hi: float) -> float:
    try:
        return max(lo, min(hi, float(os.environ.get(name, "") or default)))
    except (TypeError, ValueError):
        return default


def speed_cells_s() -> float:
    """Cells per second.

    Constant SPEED rather than constant laps: a lap is a different distance
    on an 80-column terminal and a 200-column one, and an operator reads
    motion at a rate, not as a fraction of a screen they are not measuring.
    """
    return _env_float(SPEED_ENV_VAR, 18.0, 1.0, 120.0)


def chase_period_s() -> float:
    """Seconds from a fresh prey to the bite.

    The gap closes across this window, so the whole arc — pursuit, closing,
    catch — plays out at a pace a glance can follow.
    """
    return _env_float(CHASE_ENV_VAR, 6.0, 0.5, 120.0)


def body_length() -> int:
    """Cells of serpent behind the head, fading."""
    try:
        return max(1, min(24, int(os.environ.get(BODY_ENV_VAR, "") or 5)))
    except (TypeError, ValueError):
        return 5


# ---------------------------------------------------------------------------
# The path
# ---------------------------------------------------------------------------


def path_length(width: int) -> int:
    """Cells in one full circuit of the two hairlines."""
    try:
        return max(0, 2 * int(width))
    except (TypeError, ValueError):
        return 0


def cell_at(index: int, width: int) -> Tuple[int, int]:
    """``(row, x)`` for a position along the circuit.

    The top runs left→right and the bottom runs right→left, so the path is a
    closed loop rather than two strips scanned the same way — a serpent that
    reappeared at the left edge each time would read as two animations, not
    one creature going round.
    """
    try:
        w = max(1, int(width))
        i = int(index) % (2 * w)
        if i < w:
            return TOP, i
        return BOTTOM, (2 * w - 1 - i)
    except (TypeError, ValueError):
        return TOP, 0


@dataclass(frozen=True)
class ChaseFrame:
    """One instant of the chase, as positions along the circuit."""

    head: int
    body: Tuple[int, ...]
    prey: int
    biting: bool
    width: int

    @property
    def length(self) -> int:
        return path_length(self.width)


def frame(t: float, width: int) -> Optional[ChaseFrame]:
    """The chase at time ``t``, or None when it cannot be drawn.

    None rather than a degenerate frame: a terminal too narrow to show
    travel should render its hairline plainly, not a serpent that jumps a
    third of the screen per repaint.

    The gap closes linearly across :func:`chase_period_s` and reaches zero at
    the bite, then the prey is ahead again — the ouroboros arc the crest
    tells, in one dimension.
    """
    try:
        w = int(width)
        if w < _MIN_WIDTH or not serpent_enabled():
            return None
        length = path_length(w)
        if length <= 0:
            return None

        head = int(float(t) * speed_cells_s()) % length
        trail = min(body_length(), max(1, length // 4))
        body = tuple((head - n) % length for n in range(1, trail + 1))

        period = chase_period_s()
        progress = (float(t) % period) / period
        # The prey starts a comfortable lead ahead and is reeled in. Capped
        # at a third of the circuit so it stays on screen with its pursuer on
        # a narrow terminal, where a full-lap lead would put them adjacent.
        max_gap = max(2, min(length // 3, int(length * 0.4)))
        gap = int(round(max_gap * (1.0 - progress)))
        return ChaseFrame(
            head=head,
            body=body,
            prey=(head + gap) % length,
            biting=gap <= 0,
            width=w,
        )
    except Exception:  # noqa: BLE001
        logger.debug("[SerpentRule] frame degraded", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Rendering — prompt_toolkit fragments for one row
# ---------------------------------------------------------------------------


def _hex(rgb: Any) -> str:
    """``(r, g, b)`` → ``#rrggbb``. Accepts any 3-sequence. NEVER raises."""
    try:
        r, g, b = (max(0, min(255, int(c))) for c in tuple(rgb)[:3])
        return f"#{r:02x}{g:02x}{b:02x}"
    except Exception:  # noqa: BLE001
        return "#a371f7"


def _palette() -> Tuple[str, str, Tuple[int, int, int], Tuple[int, int, int]]:
    """``(rule, serpent, prey_core, prey_edge)`` from the ONE brand source.

    The prey's colours are the crest's own — pale eye-colour core fading to
    the V's venom purple — so the creature on the hairline and the creature
    on the boot screen are visibly the same animal. Falls back to the Style
    Guide hexes if the crest cannot be imported, which keeps this module
    usable in a headless test without dragging in the rasteriser.
    """
    try:
        from backend.core.ouroboros.ui.theme import PALETTE
        rule = PALETTE.get("venom_purple", "#a371f7")
        serpent = PALETTE.get("venom_green", "#5EE06A")
    except Exception:  # noqa: BLE001
        rule, serpent = "#a371f7", "#5EE06A"
    try:
        from backend.core.ouroboros.ui.crest import _EYE_RGB, _V_TOP_RGB
        return rule, serpent, _EYE_RGB, _V_TOP_RGB
    except Exception:  # noqa: BLE001
        return rule, serpent, (234, 255, 208), (192, 132, 252)


def _glyphs(unicode_ok: Optional[bool] = None) -> Tuple[str, str, str, str]:
    """``(rule, head, body, prey)``.

    Every mark is ONE cell. The identity emoji is deliberately absent: `🐍`
    is double-width on most terminals and single on some, so a hairline
    carrying it would be the wrong length on half the machines that draw it —
    and the length of this line is the frame around the operator's caret.
    """
    try:
        from backend.core.ouroboros.ui.theme import supports_unicode
        ok = supports_unicode() if unicode_ok is None else bool(unicode_ok)
    except Exception:  # noqa: BLE001
        ok = bool(unicode_ok)
    if ok:
        return "─", "◉", "●", "+"
    return "-", "O", "o", "+"


def rule_fragments(
    row: int,
    width: int,
    t: float,
    *,
    active: bool = True,
    unicode_ok: Optional[bool] = None,
) -> List[Tuple[str, str]]:
    """prompt_toolkit fragments for ONE hairline. NEVER raises.

    Idle (``active=False``) returns the plain rule — byte-identical to the
    line that shipped before this module existed, so the feature costs
    nothing when the organism is doing nothing.
    """
    # Resolved INSIDE the guard, and the width parsed exactly once.
    #
    # Both were outside it, and both were "NEVER raises" violations the
    # tests caught: a theme that could not be imported propagated out of a
    # repaint, and the except clause re-ran `int(width)` on the very value
    # that had just failed to parse — so a bad width raised FROM the
    # handler meant to absorb it.
    rule_c, g_rule, cells = "#a371f7", "─", 0
    try:
        rule_c, serpent_c, prey_core, prey_edge = _palette()
        g_rule, g_head, g_body, g_prey = _glyphs(unicode_ok)
        w = max(0, int(width))
        cells = w
        if w <= 0:
            return []
        plain = [(f"fg:{rule_c}", g_rule * w)]
        if not active:
            return plain
        f = frame(t, w)
        if f is None:
            return plain

        # cell → (glyph, style). Built for THIS row only; the other row's
        # cells are simply absent, so one pass over the body serves both.
        marks = {}
        for n, pos in enumerate(reversed(f.body)):
            r, x = cell_at(pos, w)
            if r == row:
                # Older segments dim toward the rule they are crossing.
                fade = 0.35 + 0.5 * (n / max(1, len(f.body)))
                marks[x] = (g_body, f"fg:{_scale(serpent_c, fade)}")
        r, x = cell_at(f.head, w)
        if r == row:
            marks[x] = (g_head, f"fg:{serpent_c} bold")
        r, x = cell_at(f.prey, w)
        if r == row:
            # The bite flashes the prey to its core colour — the one moment
            # the crest's story resolves, and the only place this line is
            # allowed to be the brightest thing on screen.
            rgb = prey_core if f.biting else prey_edge
            marks[x] = (g_prey, f"fg:{_hex(rgb)}" + (" bold" if f.biting else ""))

        if not marks:
            return plain
        out: List[Tuple[str, str]] = []
        run: List[str] = []
        for x in range(w):
            if x in marks:
                if run:
                    out.append((f"fg:{rule_c}", "".join(run)))
                    run = []
                glyph, style = marks[x]
                out.append((style, glyph))
            else:
                run.append(g_rule)
        if run:
            out.append((f"fg:{rule_c}", "".join(run)))
        return out
    except Exception:  # noqa: BLE001
        logger.debug("[SerpentRule] render degraded", exc_info=True)
        # `cells` is whatever was successfully parsed before the failure —
        # never re-derived from the argument that caused it.
        return [(f"fg:{rule_c}", g_rule * cells)] if cells else []


def _scale(hex_colour: str, factor: float) -> str:
    """Dim a hex colour toward black. NEVER raises."""
    try:
        h = hex_colour.lstrip("#")
        rgb = tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
        return _hex(tuple(int(c * max(0.0, min(1.0, factor))) for c in rgb))
    except Exception:  # noqa: BLE001
        return hex_colour


__all__ = [
    "BODY_ENV_VAR",
    "BOTTOM",
    "CHASE_ENV_VAR",
    "ChaseFrame",
    "MASTER_FLAG_ENV_VAR",
    "SERPENT_RULE_SCHEMA_VERSION",
    "SPEED_ENV_VAR",
    "TOP",
    "body_length",
    "cell_at",
    "chase_period_s",
    "frame",
    "path_length",
    "rule_fragments",
    "serpent_enabled",
    "speed_cells_s",
]
