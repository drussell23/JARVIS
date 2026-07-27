"""The cockpit's view mode — FLOW / SELECT / FOCUS:<lane>.

Ported from ``layout_controller`` rather than reached across to it. That FSM
lives in the daemon and drives the LOCAL SerpentFlow console; the selection
UX being built here is client-side, in the ``ov attach`` process. Driving a
daemon-side FSM over the bridge would put a socket round-trip between an
arrow key and a repaint, and would mean two attached cockpits shared one view
mode — pressing Down in terminal A would move terminal B's cursor.

So: same vocabulary, same strictness, client-side ownership. Pure logic, no
prompt_toolkit import, so the transitions are testable without a terminal.

Modes
-----
``flow``          ambient interleaving — the default, and what every cockpit
                  did before selection existed.
``select``        the deck is a cursor list; Up/Down move, Enter focuses.
``focus:<lane>``  one lane's isolated output, hydrated from its ring.

Esc pops one level from anywhere, and pops to ``flow`` rather than unwinding
a stack: an operator hitting Escape wants out, not one step back.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence

COCKPIT_FSM_SCHEMA_VERSION = "cockpit_fsm.1"

#: Before the first byte of upstream telemetry. A cockpit attached to a
#: cold-booting daemon renders faster than the daemon hydrates, and the
#: honest state during that gap is "waiting", not "nothing is happening" —
#: which is what a blank deck says.
MODE_IGNITION = "ignition"
MODE_FLOW = "flow"
MODE_SELECT = "select"
MODE_FOCUS_PREFIX = "focus:"

_TRUTHY = ("1", "true", "yes", "on")

#: A lane id is produced by us (``swarm/<sym>``, ``unit/<id>``), never by a
#: model, but it crosses IPC and indexes a dict — so it is validated rather
#: than trusted.
_LANE_RX = re.compile(r"^[A-Za-z0-9_./:@-]{1,120}$")


def selection_enabled() -> bool:
    """``JARVIS_COCKPIT_SELECTION`` (default ON). OFF pins the cockpit to
    FLOW, which is the pre-D3 behaviour."""
    return os.environ.get(
        "JARVIS_COCKPIT_SELECTION", "1",
    ).strip().lower() in _TRUTHY


def is_focus(mode: str) -> bool:
    return isinstance(mode, str) and mode.startswith(MODE_FOCUS_PREFIX)


def focus_lane(mode: str) -> Optional[str]:
    """The lane id inside a focus mode, or None. Validated, not just split."""
    if not is_focus(mode):
        return None
    lane = mode[len(MODE_FOCUS_PREFIX):]
    return lane if _LANE_RX.match(lane) else None


def is_valid_mode(mode: str) -> bool:
    if not isinstance(mode, str):
        return False
    if mode in (MODE_FLOW, MODE_SELECT):
        return True
    return focus_lane(mode) is not None


@dataclass(frozen=True)
class ModeTransition:
    old_mode: str
    new_mode: str
    reason: str = ""


class CockpitFSM:
    """View mode + deck cursor for one attached cockpit.

    ``lanes_provider`` returns the current selectable rows (the daemon's
    ``LaneRegistry.summary()``, delivered over the bridge). The FSM never
    caches that list: a cursor held against a stale snapshot is exactly the
    drift that makes selection unreliable, so position is resolved against
    whatever the deck holds at the moment a key is pressed.
    """

    def __init__(
        self,
        *,
        lanes_provider: Callable[[], Sequence[dict]] = lambda: (),
        on_change: Optional[Callable[[ModeTransition], None]] = None,
    ) -> None:
        self._mode = MODE_FLOW
        self._cursor = 0
        self._lanes = lanes_provider
        self._on_change = on_change

    # -- state ------------------------------------------------------------

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def cursor(self) -> int:
        return self._cursor

    @property
    def focused_lane(self) -> Optional[str]:
        return focus_lane(self._mode)

    def rows(self) -> List[dict]:
        try:
            return list(self._lanes() or [])
        except Exception:  # noqa: BLE001 — a broken provider is an empty deck
            return []

    def _set(self, mode: str, reason: str) -> bool:
        if not is_valid_mode(mode) or mode == self._mode:
            return False
        old, self._mode = self._mode, mode
        if self._on_change is not None:
            try:
                self._on_change(ModeTransition(old, mode, reason))
            except Exception:  # noqa: BLE001
                pass
        return True

    # -- transitions ------------------------------------------------------

    def enter_select(self) -> bool:
        """FLOW -> SELECT. No-op when the deck is empty: a cursor list with
        nothing in it is a mode the operator cannot leave by selecting."""
        if not selection_enabled() or self._mode != MODE_FLOW:
            return False
        if not self.rows():
            return False
        self._cursor = 0
        return self._set(MODE_SELECT, "operator_select")

    def move(self, delta: int) -> bool:
        """Move the cursor. CLAMPS rather than wraps.

        Wrapping in a list whose length changes underneath you means Down at
        the bottom can silently land on row 0 of a deck that just grew — the
        operator's mental model breaks without any error."""
        if self._mode != MODE_SELECT:
            return False
        rows = self.rows()
        if not rows:
            return self.escape()
        self._cursor = max(0, min(len(rows) - 1, self._cursor + int(delta)))
        return True

    def focus_selected(self) -> bool:
        """SELECT -> FOCUS. Resolves the cursor against the CURRENT deck."""
        if self._mode != MODE_SELECT:
            return False
        rows = self.rows()
        if not rows:
            return self.escape()
        idx = max(0, min(len(rows) - 1, self._cursor))
        lane = str(rows[idx].get("lane", ""))
        return self.focus(lane)

    def focus(self, lane: str) -> bool:
        """Focus a lane by id.

        Does NOT verify the lane is live. A tombstoned lane is a legitimate
        target — that is the entire point of retaining it — and a lane that
        has aged out entirely simply fails validation here and leaves the
        operator where they were."""
        lane = str(lane or "")
        if not selection_enabled() or not _LANE_RX.match(lane):
            return False
        return self._set(f"{MODE_FOCUS_PREFIX}{lane}", "operator_focus")

    def escape(self) -> bool:
        """Back to FLOW from anywhere. One press, not one level."""
        self._cursor = 0
        return self._set(MODE_FLOW, "operator_escape")

    # -- render helpers ---------------------------------------------------

    def selected_lane(self) -> Optional[str]:
        if self._mode != MODE_SELECT:
            return None
        rows = self.rows()
        if not rows:
            return None
        idx = max(0, min(len(rows) - 1, self._cursor))
        return str(rows[idx].get("lane", "")) or None


__all__ = [
    "COCKPIT_FSM_SCHEMA_VERSION",
    "MODE_FLOW",
    "MODE_FOCUS_PREFIX",
    "MODE_SELECT",
    "CockpitFSM",
    "ModeTransition",
    "focus_lane",
    "is_focus",
    "is_valid_mode",
    "selection_enabled",
]
