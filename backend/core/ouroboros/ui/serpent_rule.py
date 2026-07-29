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
CELLS_PER_FRAME_ENV_VAR = "JARVIS_SERPENT_RULE_CELLS_PER_FRAME"
FRAME_INTERVAL_ENV_VAR = "JARVIS_SERPENT_RULE_FRAME_S"
CHASE_ENV_VAR = "JARVIS_SERPENT_RULE_CHASE_S"
BODY_ENV_VAR = "JARVIS_SERPENT_RULE_BODY"
LEAD_ENV_VAR = "JARVIS_SERPENT_RULE_LEAD"

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


def frame_interval_s() -> float:
    """The surface's repaint period — the animation's time quantum.

    Named and tunable because the smoothness of this animation is a function
    of it, not of any speed chosen independently. Defaults to the cockpit
    Application's `refresh_interval`; callers that repaint at a different
    rate pass their own to :func:`rule_fragments`.
    """
    return _env_float(FRAME_INTERVAL_ENV_VAR, 0.1, 0.01, 2.0)


def cells_per_frame() -> float:
    """Cells the head advances per repaint. FRACTIONAL, and that is the point.

    An integer step removed the beat but not the steppiness: one whole
    character every 100 ms is a teleport of a character's width, ten times a
    second. A cell is the atom of POSITION in a terminal, so the remaining
    smoothness had to come from somewhere other than position.

    It comes from INTENSITY — see :func:`_coverage`. Once a mark can sit
    between two cells, fractional steps are not merely allowed, they are
    required: an exactly-integer step lands on a cell boundary every frame
    and the sub-cell machinery never engages.

    Default 0.6 — a step small enough that the glide is continuous and large
    enough that the creature is visibly travelling.
    """
    return _env_float(CELLS_PER_FRAME_ENV_VAR, 0.6, 0.05, 8.0)


def speed_cells_s(interval: Optional[float] = None) -> float:
    """Derived, never chosen: cells per frame over the frame period."""
    try:
        return cells_per_frame() / max(0.001, float(
            interval if interval is not None else frame_interval_s()))
    except Exception:  # noqa: BLE001
        return 6.0


def chase_period_s() -> float:
    """Seconds from a fresh prey to the bite.

    The gap closes across this window, so the whole arc — pursuit, closing,
    catch — plays out at a pace a glance can follow.
    """
    return _env_float(CHASE_ENV_VAR, 6.0, 0.5, 120.0)


def lead_cells() -> int:
    """How far ahead the prey starts. Cells, so it means the same thing on
    every terminal."""
    try:
        return max(2, min(80, int(os.environ.get(LEAD_ENV_VAR, "") or 14)))
    except (TypeError, ValueError):
        return 14


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

    head: float
    body: Tuple[float, ...]
    prey: float
    biting: bool
    width: int

    @property
    def length(self) -> int:
        return path_length(self.width)


def frame(
    t: float, width: int, *, interval: Optional[float] = None,
) -> Optional[ChaseFrame]:
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

        # Quantise time to FRAMES first, then advance whole cells. Stepping
        # `t * speed` and truncating samples a continuous position at
        # irregular instants — the same beat that made this stutter.
        # Time quantised to FRAMES — the position is only ever sampled at
        # instants the surface will actually draw, so a frame the terminal
        # skips cannot leave the creature somewhere it was never rendered.
        # The position itself stays continuous.
        step = int(float(t) / max(0.001, float(
            interval if interval is not None else frame_interval_s())))
        head = (step * cells_per_frame()) % length
        trail = min(body_length(), max(1, length // 4))
        body = tuple(float((head - n) % length)
                     for n in range(1, trail + 1))

        period = chase_period_s()
        progress = (float(t) % period) / period
        # The lead is PERCEPTUAL, not a fraction of the circuit.
        #
        # A third of the circuit is 58 cells on an 88-column terminal — more
        # than half a rule, so the two were rarely on the same line and the
        # chase read as two unrelated marks. What makes a pursuit legible is
        # holding both in one glance: far enough apart to be clearly separate,
        # near enough that the closing is the thing you notice.
        #
        # Still bounded by the circuit, so a narrow terminal cannot be handed
        # a lead longer than the path it runs on.
        max_gap = max(3, min(length // 4, lead_cells()))
        gap = max_gap * (1.0 - progress)
        return ChaseFrame(
            head=float(head),
            body=body,
            prey=float((head + gap) % length),
            biting=gap < 0.5,
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
    interval: Optional[float] = None,
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
        f = frame(t, w, interval=interval)
        if f is None:
            return plain

        # SUB-CELL COVERAGE — the smoothness the cell grid cannot give.
        #
        # A terminal's atom of position is one character, so a mark stepping
        # whole cells teleports a character's width per frame however evenly
        # it does it. The crest already solved this on its own raster:
        # boundary cells are DIMMED, and "dimmed edges read as native
        # terminal anti-aliasing" (`crest._edge_feather`). Same law here — a
        # mark at 12.4 paints cell 12 at 60% and cell 13 at 40%, and the eye
        # integrates the pair as one mark sitting four tenths of the way
        # along. Position becomes continuous without a single new glyph.
        #
        # Coverage BLENDS toward the rule rather than toward black, because
        # the rule is still there underneath: a partially-covered cell is a
        # hairline with some serpent on it, not a hole.
        marks: dict = {}

        def _paint(pos: float, glyph: str, rgb, weight: float = 1.0) -> None:
            for cell, cover in _coverage(pos, w):
                r, x = cell_at(cell, w)
                if r != row:
                    continue
                amount = cover * max(0.0, min(1.0, weight))
                prior = marks.get(x)
                # The head wins a cell it shares with its own tail: brightest
                # coverage owns the glyph, so a body segment cannot erase the
                # head on the frame they overlap.
                if prior is None or amount > prior[2]:
                    marks[x] = (glyph, rgb, amount)

        for n, pos in enumerate(reversed(f.body)):
            fade = 0.30 + 0.55 * (n / max(1, len(f.body)))
            _paint(pos, g_body, _rgb(serpent_c), fade)
        _paint(f.head, g_head, _rgb(serpent_c), 1.0)
        # The bite flashes the prey to its core colour — the one moment the
        # crest's story resolves, and the only place this line is allowed to
        # be the brightest thing on screen.
        _paint(f.prey, g_prey, prey_core if f.biting else prey_edge, 1.0)

        if not marks:
            return plain
        rule_rgb = _rgb(rule_c)
        out: List[Tuple[str, str]] = []
        run: List[str] = []
        for x in range(w):
            mark = marks.get(x)
            # Below this, a mark is fainter than the rule it sits on and
            # painting it only smudges the hairline.
            if mark is None or mark[2] < 0.08:
                run.append(g_rule)
                continue
            if run:
                out.append((f"fg:{rule_c}", "".join(run)))
                run = []
            glyph, rgb, amount = mark
            style = f"fg:{_hex(_blend(rule_rgb, rgb, amount))}"
            if amount > 0.85:
                style += " bold"
            out.append((style, glyph))
        if run:
            out.append((f"fg:{rule_c}", "".join(run)))
        return out
    except Exception:  # noqa: BLE001
        logger.debug("[SerpentRule] render degraded", exc_info=True)
        # `cells` is whatever was successfully parsed before the failure —
        # never re-derived from the argument that caused it.
        return [(f"fg:{rule_c}", g_rule * cells)] if cells else []


def _subcell_available() -> bool:
    """Can this terminal express the in-between colours? NEVER raises.

    Sub-cell coverage IS colour interpolation, so it needs a palette that can
    hold the interpolants. Truecolor and 256 can; 16 cannot, and NONE has no
    colour at all.
    """
    try:
        from backend.core.ouroboros.ui.theme import ColorTier, active_tier
        return active_tier() >= ColorTier.C256
    except Exception:  # noqa: BLE001
        return False


def _coverage(pos: float, width: int) -> Tuple[Tuple[int, float], ...]:
    """``((cell, coverage), …)`` for a mark at fractional ``pos``.

    Linear split across the two cells the mark straddles — the simplest
    filter that is correct, and the one the eye reads as a single mark at an
    in-between position. A mark exactly on a boundary returns one cell at
    full coverage, so an integer step still renders crisply.
    """
    try:
        length = path_length(width)
        if length <= 0:
            return ()
        base = int(pos // 1) % length
        frac = float(pos) - float(int(pos // 1))
        if frac <= 1e-6 or not _subcell_available():
            # A terminal that cannot express the blend gets the NEAREST cell,
            # crisply. Sixteen colours quantise an interpolated green-purple
            # to whichever of the two it is closer to, so the "smooth" render
            # would flicker between rule and serpent — worse than a clean
            # step, and worse in a way only that terminal would ever show.
            return ((base if frac < 0.5 else (base + 1) % length, 1.0),)
        return ((base, 1.0 - frac), ((base + 1) % length, frac))
    except Exception:  # noqa: BLE001
        return ()


def _rgb(hex_colour: str) -> Tuple[int, int, int]:
    """``#rrggbb`` → ``(r, g, b)``. NEVER raises."""
    try:
        h = str(hex_colour).lstrip("#")
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    except Exception:  # noqa: BLE001
        return (163, 113, 247)


def _blend(under, over, amount: float):
    """``under`` toward ``over`` by ``amount`` — the coverage law.

    Toward the RULE, not toward black: a partially covered cell is a
    hairline with some serpent on it, and fading to black would punch a hole
    in the line this animation exists to decorate.
    """
    try:
        a = max(0.0, min(1.0, float(amount)))
        return tuple(int(round(u + (o - u) * a)) for u, o in zip(under, over))
    except Exception:  # noqa: BLE001
        return over


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
    "CELLS_PER_FRAME_ENV_VAR",
    "FRAME_INTERVAL_ENV_VAR",
    "LEAD_ENV_VAR",
    "TOP",
    "body_length",
    "cell_at",
    "chase_period_s",
    "frame",
    "path_length",
    "rule_fragments",
    "serpent_enabled",
    "cells_per_frame",
    "frame_interval_s",
    "lead_cells",
    "speed_cells_s",
]
