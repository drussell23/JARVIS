"""Hold-to-talk on a terminal that cannot report a key release.

`ptt_router` states the constraint correctly: a TTY delivers keypresses only.
There is no `KEY_UP`, so "the operator let go of space" is not an event any
terminal sends, and a literal press-to-open / release-to-close microphone is
not implementable by listening for a release that never arrives.

The conclusion drawn from that — hold-to-talk is impossible — is wrong, and
the counterexample ships: Claude Code does it. The trick is that a *held* key
is not silent. The operating system's own key-repeat turns it into a stream:

    press ──[initial delay 250-500ms]──► repeat ──[25-50ms]──► repeat ──► …

A held key is therefore observable as a RATE, and a release is observable as
that rate stopping. Neither needs a release event, a global hook, or anything
outside the prompt_toolkit event loop — only the arrival times of keys the
application already receives.

Two windows, not one
--------------------
The tempting design uses a single ~150ms watchdog. It cannot work, because
the two things being measured are an order of magnitude apart:

* **Arming** must outwait the OS *initial delay* (250-500ms, user-configurable
  and different on every platform). A 150ms watchdog expires during that gap
  and classifies every hold as a tap.
* **Release** must be short once repeats are streaming, or letting go feels
  laggy — but longer than one repeat interval, or a single dropped keystroke
  under load reads as a release and cuts the operator off mid-sentence.

So arming waits long and releasing waits short, and the release window is
LEARNED: the observed repeat interval is measured and multiplied, because
25ms and 50ms machines want different answers and neither should be hardcoded.

Deciding the CLOSE, not the open
--------------------------------
The obvious wiring makes the microphone wait for confirmation: pass the first
space into the buffer, and once repeats prove a hold, delete those characters
and start recording. It works, and it costs the operator a visible flicker
plus ~0.75s of latency on every tap.

`ov` does not need it. The space binding is already gated to an EMPTY buffer
(`ptt_router`), where space is a control key rather than text — so nothing is
being typed that could be damaged, and nothing has to be taken back.

That inverts the problem. The first press opens the microphone immediately,
exactly as the toggle always did, and what gets DECIDED later is only what
closes it:

* repeats arrive  → it was a hold → releasing closes it
* silence         → it was a tap  → it stays open until the next tap

Tap latency is therefore unchanged at zero, there is no flicker to explain,
and hold-to-talk and toggle stop being two modes the operator has to choose
between: the same key does whichever one their hand did.

The passthrough/undo path is still implemented (`undo_chars`) because a
binding WITHOUT the empty-buffer gate — a printable key that must also insert
text — genuinely needs it. `ov`'s does not.

Confirmation needs TWO repeats, not one. The first repeat arrives after the
initial delay, which is squarely in the range of a deliberate human
double-tap; the second arrives at the fast repeat rate, which a human hand
cannot reproduce. Waiting for it is the difference between "held" and "typed
two spaces quickly".

What this is not
----------------
Not a keylogger. Nothing is captured outside the focused application, no OS
hooks are installed, and the only input is the arrival time of a key
prompt_toolkit already delivered. On terminals implementing the kitty
keyboard protocol, real release events exist and `ptt_router.on_release_supported`
already detects them — those should be preferred, and this heuristic is what
every other terminal gets instead of nothing.
"""
from __future__ import annotations

import enum
import logging
import os
import time
from typing import Any, Callable, List, Optional

logger = logging.getLogger("Ouroboros.HoldToTalk")

__all__ = [
    "HoldAction", "HoldEvent", "HoldPhase", "HoldDetector",
    "hold_to_talk_enabled", "arm_window_s", "release_window_s",
    "confirm_repeats", "HoldWatchdog",
]


class HoldPhase(str, enum.Enum):
    """Where the detector is between "a key arrived" and "they let go"."""

    IDLE = "idle"
    #: A key arrived and passed through; waiting to see whether it repeats.
    PENDING = "pending"
    #: Repeats confirmed a held key. The microphone is open.
    HOLDING = "holding"


class HoldAction(str, enum.Enum):
    """What the key binding should do with the keystroke it just received."""

    #: The FIRST press of a sequence. Insert it if printable, and take
    #: whatever action a press normally takes — for `ov`, opening the mic.
    PASSTHROUGH = "passthrough"
    #: A repeat that has not yet confirmed a hold. Insert it if printable
    #: (it counts toward `undo_chars`), but do NOT repeat the press action.
    #:
    #: Distinct from PASSTHROUGH because collapsing the two toggles the
    #: microphone shut on the first repeat: the operator holds the key, the
    #: OS sends a second event, and the binding reads it as a second tap.
    #: A held key must look like one press, not many.
    WARMUP = "warmup"
    #: A hold just became certain: delete the warmup characters and open the
    #: microphone. Carries `undo_chars`.
    CONFIRM = "confirm"
    #: A repeat of a key already known to be held. Drop it.
    SWALLOW = "swallow"


class HoldEvent(str, enum.Enum):
    """What the watchdog concluded from silence."""

    #: Nothing repeated. It was an ordinary keystroke; leave the buffer alone.
    TAP = "tap"
    #: The repeat stream stopped. They let go.
    RELEASE = "release"


def hold_to_talk_enabled() -> bool:
    """Default ON. Off leaves the existing latch/toggle behaviour untouched."""
    return os.environ.get(
        "JARVIS_HOLD_TO_TALK_ENABLED", "1",
    ).strip().lower() not in ("0", "false", "no", "off")


def _env_float(name: str, default: float, lo: float, hi: float) -> float:
    try:
        return min(hi, max(lo, float(os.environ.get(name, "") or default)))
    except (TypeError, ValueError):
        return default


def arm_window_s() -> float:
    """How long to wait for the FIRST repeat before calling it a tap.

    Must exceed the OS key-repeat initial delay, which is user-configurable
    and platform-specific: macOS ranges roughly 200-2000ms with a ~500ms
    default, X11 defaults to 660ms, Windows to ~250-1000ms. 0.75s covers the
    common configurations without making a genuine tap feel like it hangs —
    and nothing is blocked while waiting, because the character was already
    inserted.
    """
    return _env_float("JARVIS_PTT_ARM_WINDOW_S", 0.75, 0.20, 3.0)


def release_window_s(observed_interval: float = 0.0) -> float:
    """How long a gap in the repeat stream means "they let go".

    LEARNED, not fixed. Given a measured repeat interval, the window is a
    multiple of it — enough to absorb a dropped keystroke under load without
    cutting the operator off, but short enough that releasing feels immediate.
    A 25ms machine and a 50ms machine want different answers, and an operator
    who slowed their repeat rate for accessibility wants a third.

    Falls back to a fixed default before any interval has been observed.
    """
    floor = _env_float("JARVIS_PTT_RELEASE_MIN_S", 0.18, 0.05, 2.0)
    ceiling = _env_float("JARVIS_PTT_RELEASE_MAX_S", 0.60, 0.10, 5.0)
    if observed_interval <= 0:
        return floor
    multiple = _env_float("JARVIS_PTT_RELEASE_MULTIPLE", 5.0, 1.5, 20.0)
    return min(ceiling, max(floor, observed_interval * multiple))


def _fast_repeat_ceiling_s() -> float:
    """Longest gap still counted as part of the repeat STREAM.

    Above this a gap is the initial delay or a stall, not a repeat. OS repeat
    rates top out around 30/s (33ms) and bottom out near 2/s for accessibility
    settings, so 150ms admits the slowest real stream while excluding a
    250ms+ initial delay.
    """
    return _env_float("JARVIS_PTT_FAST_REPEAT_MAX_S", 0.15, 0.05, 1.0)


def confirm_repeats() -> int:
    """Repeats required before a hold is certain.

    Two, because one is ambiguous: the first repeat arrives after the OS
    initial delay, which overlaps the timing of a deliberate double-tap. The
    second arrives at the fast repeat rate, which a human hand cannot
    reproduce.
    """
    try:
        return max(1, int(os.environ.get("JARVIS_PTT_CONFIRM_REPEATS", "2")))
    except (TypeError, ValueError):
        return 2


class HoldDetector:
    """Classifies a stream of keypresses as tapping or holding.

    Pure timing logic: no prompt_toolkit, no microphone, no I/O. The clock is
    injected so the whole state machine is provable without sleeping, which
    is the only way to test something whose entire behaviour is "what happened
    between two moments".
    """

    def __init__(
        self,
        clock: Optional[Callable[[], float]] = None,
        *,
        printable: bool = True,
    ) -> None:
        self._clock = clock or time.monotonic
        #: A printable trigger (Space) must pass through so typing stays
        #: instant. A modifier combo (meta+k) inserts nothing, so there is
        #: nothing to undo and a hold can arm on the very first press.
        self._printable = bool(printable)
        self._phase = HoldPhase.IDLE
        self._last = 0.0
        self._first = 0.0
        self._repeats = 0
        self._emitted = 0
        self._intervals: List[float] = []
        self.holds_confirmed = 0
        self.taps_settled = 0

    # -- state -------------------------------------------------------------

    @property
    def phase(self) -> HoldPhase:
        return self._phase

    @property
    def is_recording(self) -> bool:
        return self._phase is HoldPhase.HOLDING

    @property
    def pending_chars(self) -> int:
        """Warmup characters currently sitting in the buffer."""
        return self._emitted

    @property
    def observed_interval(self) -> float:
        """Mean of the fast repeat intervals seen while holding."""
        fast = [i for i in self._intervals if i > 0]
        return sum(fast) / len(fast) if fast else 0.0

    # -- input -------------------------------------------------------------

    def on_key(self) -> HoldAction:
        """One trigger keypress arrived. Says what to do with it.

        NEVER raises: a detector fault must not eat the operator's keystroke,
        so every failure resolves to PASSTHROUGH — the behaviour this module
        replaces.
        """
        try:
            now = self._clock()
            if self._phase is HoldPhase.HOLDING:
                self._note_interval(now - self._last)
                self._last = now
                return HoldAction.SWALLOW

            if self._phase is HoldPhase.PENDING:
                gap = now - self._last
                self._last = now
                if gap > arm_window_s():
                    # Too slow to be a repeat — a fresh keystroke. Restart the
                    # observation rather than counting it toward a hold, or
                    # slow deliberate tapping would eventually look held.
                    self._begin(now)
                    return HoldAction.PASSTHROUGH
                self._repeats += 1
                self._note_interval(gap)
                if self._repeats >= confirm_repeats():
                    self._phase = HoldPhase.HOLDING
                    self.holds_confirmed += 1
                    return HoldAction.CONFIRM
                if self._printable:
                    self._emitted += 1
                return HoldAction.WARMUP

            # IDLE — the first press.
            self._begin(now)
            if not self._printable:
                # Nothing was inserted, so there is nothing to take back and
                # no reason to make the operator wait for a repeat.
                self._phase = HoldPhase.HOLDING
                self.holds_confirmed += 1
                return HoldAction.CONFIRM
            return HoldAction.PASSTHROUGH
        except Exception:  # noqa: BLE001
            logger.debug("[HoldToTalk] on_key degraded", exc_info=True)
            return HoldAction.PASSTHROUGH

    def undo_chars(self) -> int:
        """Warmup characters to remove now that a hold is confirmed.

        Read once at CONFIRM and then cleared, so a caller that deletes them
        cannot be told to delete them twice.
        """
        count, self._emitted = self._emitted, 0
        return max(0, count)

    # -- the watchdog ------------------------------------------------------

    def poll(self) -> Optional[HoldEvent]:
        """Called by the watchdog. Returns an event when silence is decisive.

        None means "still waiting" — the common case, and it must stay cheap
        because this runs on a timer for as long as a key is down.
        """
        try:
            if self._phase is HoldPhase.IDLE:
                return None
            silence = self._clock() - self._last
            if self._phase is HoldPhase.PENDING:
                if silence > arm_window_s():
                    self._reset()
                    self.taps_settled += 1
                    return HoldEvent.TAP
                return None
            if silence > release_window_s(self.observed_interval):
                self._reset()
                return HoldEvent.RELEASE
            return None
        except Exception:  # noqa: BLE001
            logger.debug("[HoldToTalk] poll degraded", exc_info=True)
            return None

    def next_deadline_s(self) -> float:
        """How long the watchdog may sleep before it must look again.

        Lets the watchdog wake on the boundary that actually matters instead
        of spinning at a fixed rate: a long doze during the warmup, a short
        one while repeats stream.
        """
        try:
            if self._phase is HoldPhase.PENDING:
                window = arm_window_s()
            elif self._phase is HoldPhase.HOLDING:
                window = release_window_s(self.observed_interval)
            else:
                return arm_window_s()
            remaining = window - (self._clock() - self._last)
            # A floor, so a burst of repeats cannot turn the watchdog into a
            # busy loop competing with the input reader for the event loop.
            return max(0.02, min(window, remaining))
        except Exception:  # noqa: BLE001
            return 0.1

    def abort(self) -> None:
        """Cancelled — Esc, focus loss, or the app going away."""
        self._reset()

    # -- internals ---------------------------------------------------------

    def _begin(self, now: float) -> None:
        self._phase = HoldPhase.PENDING
        self._first = now
        self._last = now
        self._repeats = 0
        self._emitted = 1 if self._printable else 0
        self._intervals = []

    def _note_interval(self, gap: float) -> None:
        # ONLY the fast repeat stream trains the release window. The gap
        # before the first repeat is the OS *initial delay* — an order of
        # magnitude longer and not a repeat interval at all. Averaging it in
        # inflated a measured 30ms stream to 49ms, which would have stretched
        # the release window by the same proportion and made letting go feel
        # sticky on exactly the machines that repeat fastest.
        if 0 < gap < _fast_repeat_ceiling_s():
            self._intervals.append(gap)
            # Bounded: a key held for a minute must not accumulate a list.
            if len(self._intervals) > 32:
                self._intervals = self._intervals[-32:]

    def _reset(self) -> None:
        self._phase = HoldPhase.IDLE
        self._repeats = 0
        self._emitted = 0
        self._intervals = []


class HoldWatchdog:
    """Drives `HoldDetector.poll` from the event loop.

    A separate task rather than a `refresh_interval` tick, because the
    question "have the repeats stopped" has to be answered on a deadline that
    changes with what is happening — a long doze during the warmup, a short
    one while repeats stream (`next_deadline_s`). Polling at a fixed rate
    would either burn the loop or answer late.

    Single-task by construction and cancelled on stop, because a watchdog
    that outlives its detector is exactly the leak that makes a microphone
    close a second after the operator started speaking again.
    """

    def __init__(
        self,
        detector: "HoldDetector",
        on_event: Callable[["HoldEvent"], None],
    ) -> None:
        self._detector = detector
        self._on_event = on_event
        self._task: Any = None

    @property
    def running(self) -> bool:
        task = self._task
        return task is not None and not task.done()

    def kick(self) -> None:
        """A key arrived — make sure something is watching. NEVER raises."""
        try:
            import asyncio

            if self.running:
                # Already watching; the detector's own timestamps carry the
                # reset, so there is nothing to restart.
                return
            loop = asyncio.get_running_loop()
            self._task = loop.create_task(self._run())
        except RuntimeError:
            # No running loop (unit tests, non-async callers). `poll` can
            # still be driven by hand.
            self._task = None
        except Exception:  # noqa: BLE001
            logger.debug("[HoldToTalk] watchdog kick degraded", exc_info=True)

    async def _run(self) -> None:
        import asyncio

        try:
            while True:
                await asyncio.sleep(self._detector.next_deadline_s())
                event = self._detector.poll()
                if event is not None:
                    try:
                        self._on_event(event)
                    except Exception:  # noqa: BLE001
                        logger.debug("[HoldToTalk] sink raised",
                                     exc_info=True)
                    return
                if self._detector.phase is HoldPhase.IDLE:
                    return
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.debug("[HoldToTalk] watchdog degraded", exc_info=True)

    def stop(self) -> None:
        """Cancel the watcher. Always safe; never raises."""
        task, self._task = self._task, None
        try:
            if task is not None and not task.done():
                task.cancel()
        except Exception:  # noqa: BLE001
            pass
