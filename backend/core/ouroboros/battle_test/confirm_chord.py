"""Destructive keystrokes that ask, in the keystroke itself.

Claude Code binds two actions this way and the shape is the same both times:
``Ctrl+X Ctrl+K`` stops every running background subagent — "press twice
within 3 seconds to confirm" — and in fullscreen ``Ctrl+L`` redraws once and
runs ``/clear`` if pressed again inside two seconds.

Why a latch and not a dialog
-----------------------------
The alternative to this is a modal "are you sure? [y/N]", and for a key an
operator reaches for while something is going wrong that is the wrong trade:
it steals focus, it has to be dismissed, and it turns a reflex into a
conversation. A repeat of the same chord costs nothing to learn — the
operator's fingers are already there — and it cannot be triggered by a single
mis-hit, which is the only failure this guard exists to prevent.

Why the window is short
-----------------------
The arm has to expire, or the second half of the confirmation can arrive
minutes later attached to a completely different intention. Three seconds is
long enough to be a deliberate repeat and short enough that no unrelated
keystroke lands inside it.

Why the clock is injected
-------------------------
So the expiry is TESTABLE without sleeping. A guard whose only test is
"press twice fast and hope" is a guard nobody re-verifies after they change
it, and this one stands between a mis-hit and every running agent.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Callable, Optional

__all__ = ["ConfirmLatch", "confirm_window_s"]


#: Claude Code's own window for `Ctrl+X Ctrl+K`.
_DEFAULT_WINDOW_S = 3.0


def confirm_window_s(env_var: str = "JARVIS_CONFIRM_CHORD_WINDOW_S") -> float:
    """Seconds a first press stays armed. NEVER raises.

    Clamped rather than trusted: zero would make the chord fire on the first
    press (the guard removed by configuration, silently), and a very long
    window would let an unrelated keystroke complete a confirmation the
    operator has long forgotten starting.
    """
    try:
        return max(0.5, min(30.0, float(
            os.environ.get(env_var, "") or _DEFAULT_WINDOW_S,
        )))
    except (TypeError, ValueError):
        return _DEFAULT_WINDOW_S


class ConfirmLatch:
    """One destructive action's arm/confirm state.

    ``press()`` returns True only on a repeat inside the window — that return
    value IS the decision, so a caller cannot accidentally act on the arming
    press by reading the wrong attribute.
    """

    def __init__(
        self,
        window_s: Optional[float] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._window = float(window_s) if window_s is not None else None
        self._clock = clock
        self._armed_at: Optional[float] = None
        self._lock = threading.Lock()

    @property
    def window_s(self) -> float:
        """Re-read per call when not pinned, so an env change lands without a
        restart — the same discipline the roster's row budget follows."""
        return self._window if self._window is not None else confirm_window_s()

    def press(self) -> bool:
        """Record a press; True means CONFIRMED. NEVER raises.

        A confirmation consumes the arm, so a third press starts over rather
        than firing again. Holding the chord down must not repeat a
        destructive action once per key-repeat interval.
        """
        try:
            now = float(self._clock())
        except Exception:  # noqa: BLE001
            now = 0.0
        with self._lock:
            armed_at = self._armed_at
            if armed_at is not None and (now - armed_at) <= self.window_s:
                self._armed_at = None
                return True
            self._armed_at = now
            return False

    def armed(self) -> bool:
        """Whether a press is waiting for its repeat, expiry included.

        Checked rather than stored: an arm that expired while nothing was
        pressed must READ as disarmed, or a toolbar hint would go on
        advertising a confirmation the next press will not give.
        """
        try:
            now = float(self._clock())
        except Exception:  # noqa: BLE001
            return False
        with self._lock:
            armed_at = self._armed_at
            if armed_at is None:
                return False
            if (now - armed_at) > self.window_s:
                self._armed_at = None
                return False
            return True

    def disarm(self) -> None:
        """Drop a pending arm — for when the operator does something else
        that plainly means they moved on. NEVER raises."""
        with self._lock:
            self._armed_at = None
