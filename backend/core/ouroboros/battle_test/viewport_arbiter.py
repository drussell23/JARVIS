"""What fits, and what gives way when it does not.

`LayoutController` already answers "which mode is the operator in" — flow,
split, or focus on one region. It is toolkit-agnostic and correct, and this
does not replace it. What it cannot answer is the question a terminal keeps
asking: **does the requested layout actually fit the window right now?**

Nothing asked that before. A layout engine that hands three regions to an
80-column terminal produces one of two outcomes, and both are bad: the
container maths fails and the application dies mid-session, or every region
is squeezed to a width where the content is technically present and
practically unreadable — a diff wrapped at 24 columns is not a diff.

So the arbiter sits between the operator's INTENT and the geometry, and
answers a narrower question than the layout engine does: given this many
columns, which of the regions they asked for can be shown honestly?

Demotion, not refusal
---------------------
A narrow terminal must never turn a keystroke into an error. The operator
asked for the transcript; telling them "no" trains them to stop asking. So a
region that cannot fit SIDE BY SIDE is demoted rather than dropped:

    SPLIT   → shown as a column, its own width
    FLOAT   → drawn over the deck as an overlay (the FloatContainer the
              palette already uses — one Z-index architecture, not two)
    HIDDEN  → not drawn, but still requested; it returns the moment the
              window grows

HIDDEN is the last resort and it is remembered. Resizing a terminal back to
width must restore what the operator had, not what survived the squeeze —
otherwise every accidental drag silently deletes part of their layout.

Priority is explicit, not incidental
------------------------------------
When something must give way, WHICH region gives way is a decision, and
leaving it to iteration order means it changes when someone reorders a
dict. The deck outranks lanes, lanes outrank the transcript: the deck is
where the organism speaks and is never demoted below FLOAT, because a cockpit
that hides its own output has stopped being a cockpit.

Hysteresis
----------
Terminal resize events arrive continuously while a window is dragged. A
region that re-promotes the instant it fits will flicker between FLOAT and
SPLIT across a single drag, so promotion requires a margin beyond the bare
minimum. Demotion has no such margin: becoming unreadable is urgent, becoming
readable can wait a few columns.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("Ouroboros.ViewportArbiter")

__all__ = [
    "Placement", "ViewportArbiter", "arbiter_enabled",
    "REGION_PRIORITY", "min_region_cols",
]

#: Placements, widest to narrowest.
SPLIT = "split"
FLOAT = "float"
HIDDEN = "hidden"

#: Who gives way first. The deck is the organism's voice and outranks
#: everything; the transcript is a reference surface and yields first.
#: Explicit because "whichever the loop reached last" is not a decision.
REGION_PRIORITY: Tuple[str, ...] = ("deck", "lanes", "transcript")

#: Below this a column is not a region, it is a rumour. Sized from content:
#: a lane row is an id plus a state, a transcript line is a sentence.
_MIN_COLS: Dict[str, int] = {"deck": 48, "lanes": 28, "transcript": 40}

#: Extra columns required to PROMOTE, so a drag does not flicker.
_PROMOTE_MARGIN = 8


def arbiter_enabled() -> bool:
    """Default ON. Off, every requested region is shown and the terminal is
    left to cope — the behaviour before this existed."""
    return os.environ.get(
        "JARVIS_VIEWPORT_ARBITER_ENABLED", "1",
    ).strip().lower() not in ("0", "false", "no", "off")


def min_region_cols(region: str) -> int:
    """Narrowest honest width for *region*, env-tunable.

    A knob because "readable" depends on font and content: an operator on a
    dense terminal reads a 34-column lane list fine, and one at 20pt does not.
    """
    key = f"JARVIS_MIN_COLS_{str(region).upper()}"
    try:
        raw = os.environ.get(key, "").strip()
        if raw:
            return max(8, int(raw))
    except (TypeError, ValueError):
        pass
    return _MIN_COLS.get(str(region), 32)


class Placement:
    """Where one region ended up, and whether that was the operator's ask."""

    __slots__ = ("region", "placement", "cols", "requested")

    def __init__(self, region: str, placement: str, cols: int = 0,
                 requested: bool = True) -> None:
        self.region = str(region)
        self.placement = str(placement)
        self.cols = int(cols)
        #: True when the operator asked for it — a HIDDEN region that was
        #: requested is pending, not declined, and returns when there is room.
        self.requested = bool(requested)

    @property
    def visible(self) -> bool:
        return self.placement in (SPLIT, FLOAT)

    def __repr__(self) -> str:  # pragma: no cover — diagnostics only
        return f"<Placement {self.region}={self.placement} cols={self.cols}>"


class ViewportArbiter:
    """Decides what fits. Holds intent; owns no widgets.

    Deliberately free of prompt_toolkit: the whole value is being provable at
    every width without a terminal, and a geometry rule that can only be
    tested by resizing a window is a rule nobody tests.
    """

    def __init__(self, controller: Optional[Any] = None) -> None:
        #: The EXISTING mode FSM. Reused rather than reimplemented — it
        #: already knows flow/split/focus and is toolkit-agnostic.
        self._controller = controller
        #: What the operator asked for, independent of what fits. Restoring
        #: on resize reads from THIS, so a squeeze never edits their intent.
        self._requested: Dict[str, bool] = {"deck": True}
        self._last: Dict[str, str] = {}
        self.demotions = 0

    # -- intent ------------------------------------------------------------

    def request(self, region: str, on: Optional[bool] = None) -> bool:
        """Ask for a region (or toggle it). Returns the new intent.

        The deck cannot be dismissed: a cockpit that hides the organism's own
        output has stopped being a cockpit.
        """
        try:
            key = str(region)
            if key == "deck":
                return True
            current = self._requested.get(key, False)
            self._requested[key] = (not current) if on is None else bool(on)
            return self._requested[key]
        except Exception:  # noqa: BLE001
            return False

    def requested(self, region: str) -> bool:
        return bool(self._requested.get(str(region), False))

    # -- geometry ----------------------------------------------------------

    def arbitrate(self, cols: int, rows: int = 0) -> List[Placement]:  # noqa: ARG002
        """Place every requested region for a terminal this size. NEVER raises.

        Returns placements in priority order. A region the operator asked for
        that could not fit comes back HIDDEN with ``requested=True`` — pending,
        not declined.
        """
        try:
            width = max(0, int(cols))
            wanted = [r for r in REGION_PRIORITY
                      if r == "deck" or self._requested.get(r)]

            if not arbiter_enabled():
                return [Placement(r, SPLIT, width, True) for r in wanted]

            focus = self._focused_region()
            if focus and focus in wanted:
                # Focus mode is an explicit "only this one" — the arbiter has
                # nothing to negotiate, and second-guessing it here would
                # override the operator with a heuristic.
                return [
                    Placement(r, SPLIT if r == focus else HIDDEN,
                              width if r == focus else 0,
                              self._requested.get(r, r == "deck"))
                    for r in wanted
                ]

            placements: List[Placement] = []
            remaining = list(wanted)
            # Give way from the LOWEST priority up, until the survivors fit.
            while remaining and not self._fits(remaining, width):
                victim = remaining[-1]
                if victim == "deck":
                    break               # never demoted below FLOAT; see below
                remaining.pop()
                placements.append(
                    Placement(victim, self._demote(victim, width), 0, True),
                )
                self.demotions += 1

            share = self._share(remaining, width)
            survivors = [Placement(r, SPLIT, share.get(r, 0),
                                   self._requested.get(r, r == "deck"))
                         for r in remaining]
            out = survivors + placements
            out.sort(key=lambda p: REGION_PRIORITY.index(p.region)
                     if p.region in REGION_PRIORITY else 99)
            self._last = {p.region: p.placement for p in out}
            return out
        except Exception:  # noqa: BLE001 — a resize must never kill the app
            logger.debug("[ViewportArbiter] arbitrate degraded", exc_info=True)
            # The fallback must not repeat whatever just failed. `int(cols)`
            # is exactly what raises on a junk dimension, so calling it again
            # here turned a contained fault into an uncaught one — the
            # degraded path became the crash it was written to prevent.
            return [Placement(r, SPLIT, 0, True)
                    for r in (self._requested or {"deck": True})]

    # -- internals ---------------------------------------------------------

    def _focused_region(self) -> Optional[str]:
        try:
            return getattr(self._controller, "focused_region", None)
        except Exception:  # noqa: BLE001
            return None

    def _fits(self, regions: List[str], width: int) -> bool:
        """Do these regions all clear their minimum, side by side?

        Promotion needs a margin so a window drag does not flicker a region
        between FLOAT and SPLIT; demotion does not, because becoming
        unreadable is urgent and becoming readable can wait a few columns.
        """
        need = sum(min_region_cols(r) for r in regions)
        for region in regions:
            if self._last.get(region) in (FLOAT, HIDDEN):
                need += _PROMOTE_MARGIN
                break
        return width >= need

    def _demote(self, region: str, width: int) -> str:
        """FLOAT if it can still be drawn over the deck, else HIDDEN.

        Floating reuses the FloatContainer Z-index the palette established —
        one overlay architecture, so a second one cannot disagree with it
        about what draws on top.
        """
        return FLOAT if width >= min_region_cols(region) else HIDDEN

    def _share(self, regions: List[str], width: int) -> Dict[str, int]:
        """Split the width, minimum first and the remainder to the deck.

        The deck takes the slack because it holds unbounded content; lanes
        and the transcript are lists whose extra columns are whitespace.
        """
        if not regions:
            return {}
        out = {r: min_region_cols(r) for r in regions}
        slack = width - sum(out.values())
        if slack > 0:
            head = "deck" if "deck" in out else regions[0]
            out[head] += slack
            return out
        # NEGATIVE slack: the survivors' own minimums exceed the window. This
        # is reachable at the floor — the deck alone is never demoted, so a
        # terminal narrower than its minimum still has to render it. Report
        # the width that EXISTS rather than the width it wants: a region that
        # claims more columns than the terminal has is the geometry panic
        # this class exists to prevent, handed to the layout engine as data.
        scale = width / max(1, sum(out.values()))
        clipped = {r: max(1, int(cols * scale)) for r, cols in out.items()}
        overflow = sum(clipped.values()) - width
        if overflow > 0:
            head = "deck" if "deck" in clipped else regions[0]
            clipped[head] = max(1, clipped[head] - overflow)
        return clipped
