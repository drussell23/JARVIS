"""Push-to-talk router — spacebar intercept for the cockpit input row.

The terminal constraint that shapes this design
-----------------------------------------------
A standard TTY delivers **keypresses only — there are no key-release events**.
``prompt_toolkit`` therefore cannot observe "the operator let go of space", so a
literal hold-to-talk (`press → mic_active`, `release → mic_flush`) is not
implementable on the default input stack. Terminals implementing the *kitty
keyboard protocol* (``CSI > 1 u``: kitty, WezTerm, foot, recent iTerm2) do emit
release events, but prompt_toolkit does not request or parse that mode.

Rather than ship a `mic_flush` that silently never fires, this router models PTT
as an explicit **latch** with two independent closing edges:

1. **Toggle** — a second spacebar on an empty buffer closes the latch. Always
   available, zero terminal requirements.
2. **Silence auto-flush** — the latch closes itself after a bounded quiet
   interval, driven by the same normalized level stream that feeds the scope.
   This is what makes it *feel* like release without pretending to observe one.

``on_release_supported()`` reports whether a real release edge is available, so
a future release-capable input layer can drive :meth:`PTTLatch.close` directly
without changing anything else here.

Spacebar safety
---------------
The intercept fires **only when the buffer is empty**. The instant there is any
text — even one character — space types a space, because silently swallowing
word separators mid-sentence would be intolerable. That condition is a
``prompt_toolkit`` ``Condition`` filter, so the binding is inert rather than
conditionally-branching, and the ``" "`` binding never shadows normal typing.
"""

from __future__ import annotations

import enum
import logging
import os
import time
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


class MicState(str, enum.Enum):
    CLOSED = "closed"
    OPEN = "open"


def ptt_enabled() -> bool:
    """Master gate. Default ON, but wholly inert until a latch is wired to an
    audio backend — an armed binding with no consumer is just a no-op."""
    return os.environ.get(
        "JARVIS_PTT_ENABLED", "true",
    ).strip().lower() in ("1", "true", "yes", "on")


def _silence_flush_s() -> float:
    """Quiet interval that auto-closes the latch. Env-tunable; 1.2s is long
    enough to survive an inter-word pause and short enough not to feel stuck."""
    try:
        return max(0.1, float(os.environ.get("JARVIS_PTT_SILENCE_FLUSH_S", "1.2")))
    except (TypeError, ValueError):
        return 1.2


def _silence_level() -> float:
    """Normalized level at or below which audio counts as silence."""
    try:
        return max(0.0, float(os.environ.get("JARVIS_PTT_SILENCE_LEVEL", "0.06")))
    except (TypeError, ValueError):
        return 0.06


class PTTMode(str, enum.Enum):
    """How the latch closes — chosen by capability probe, never hardcoded."""

    HOLD = "hold"        # KEY_DOWN opens, KEY_UP flushes (kitty protocol)
    TOGGLE = "toggle"    # tap opens, tap closes, silence auto-flushes

    @property
    def hint(self) -> str:
        """Operator-facing hint. The UI must state the ACTIVE paradigm — a
        cockpit that says 'hold' on a terminal that cannot see release would
        leave the mic latched with no obvious way out."""
        return "Hold Space to Talk" if self is PTTMode.HOLD else "Space ⇄ Mic"


def resolve_ptt_mode(*, probe: Optional[Any] = None) -> "tuple":
    """``(PTTMode, verdict, telemetry)`` from a live terminal probe.

    ``probe`` is injectable so tests exercise both paradigms with zero terminal
    I/O. Fails CLOSED to TOGGLE on every non-supporting verdict, including
    TIMEOUT and ERROR: degrading is harmless, whereas wrongly assuming release
    support strands the mic open."""
    try:
        if probe is None:
            from backend.core.ouroboros.terminal_capability import (
                probe_key_release_support,
            )
            probe = probe_key_release_support
        verdict, telemetry = probe()
        mode = PTTMode.HOLD if verdict.has_release else PTTMode.TOGGLE
        return (mode, verdict, dict(telemetry or {}))
    except Exception as exc:  # noqa: BLE001 — probe faults degrade, never raise
        return (PTTMode.TOGGLE, None, {"reason": type(exc).__name__})


def on_release_supported() -> bool:
    """Whether the input stack can deliver a true key-RELEASE edge.

    Now answered by a live handshake with the terminal (kitty keyboard protocol
    ``CSI ? u``) rather than assumed. Fail-closed: anything short of a
    conforming reply is False."""
    try:
        from backend.core.ouroboros.terminal_capability import (
            probe_key_release_support,
        )
        verdict, _ = probe_key_release_support()
        return bool(verdict.has_release)
    except Exception:  # noqa: BLE001
        return False


class PTTLatch:
    """The mic latch. Transport-agnostic: it emits intents, owns no audio.

    Deliberately NOT an asyncio object — key handlers are sync callbacks, and
    forcing a loop hop here would risk dropping an edge if no loop is running.
    Emission is via injected callables so the latch is testable with zero I/O."""

    def __init__(
        self,
        *,
        on_open: Optional[Callable[[], None]] = None,
        on_close: Optional[Callable[[str], None]] = None,
        clock: Optional[Callable[[], float]] = None,
        mode: Optional["PTTMode"] = None,
    ) -> None:
        self._state = MicState.CLOSED
        self._on_open = on_open
        self._on_close = on_close
        self._clock = clock or time.monotonic
        self._opened_at = 0.0
        self._last_voice_at = 0.0
        self._mode = mode if mode is not None else PTTMode.TOGGLE
        self._open_count = 0
        self._close_reasons: list = []

    # -- state ----------------------------------------------------------

    @property
    def state(self) -> MicState:
        return self._state

    @property
    def is_open(self) -> bool:
        return self._state is MicState.OPEN

    @property
    def open_count(self) -> int:
        return self._open_count

    @property
    def close_reasons(self) -> tuple:
        return tuple(self._close_reasons)

    # -- edges ----------------------------------------------------------

    def open(self) -> bool:
        """Arm the mic (``mic_active``). Idempotent — returns False if already
        open, so a key-repeat storm cannot emit a burst of open intents."""
        if self._state is MicState.OPEN:
            return False
        self._state = MicState.OPEN
        now = self._clock()
        self._opened_at = now
        self._last_voice_at = now
        self._open_count += 1
        if self._on_open is not None:
            try:
                self._on_open()
            except Exception:  # noqa: BLE001 — a consumer fault never wedges the latch
                logger.debug("[PTT] on_open raised", exc_info=True)
        return True

    def close(self, reason: str = "toggle") -> bool:
        """Disarm and flush (``mic_flush``). Idempotent."""
        if self._state is MicState.CLOSED:
            return False
        self._state = MicState.CLOSED
        self._close_reasons.append(reason)
        if self._on_close is not None:
            try:
                self._on_close(reason)
            except Exception:  # noqa: BLE001
                logger.debug("[PTT] on_close raised", exc_info=True)
        return True

    def toggle(self) -> MicState:
        """The spacebar edge: open if closed, close if open."""
        if self._state is MicState.OPEN:
            self.close("toggle")
        else:
            self.open()
        return self._state

    # -- level-driven auto-flush ----------------------------------------

    def note_level(self, level: float) -> bool:
        """Feed one normalized level (0..1). Returns True iff this observation
        auto-closed the latch.

        This is the substitute for an unobservable key-release: sustained
        silence closes the latch on its own. Only meaningful while open, so a
        quiet idle cockpit never emits spurious flushes."""
        if self._state is not MicState.OPEN:
            return False
        # HOLD mode has a REAL closing edge (key release), so silence must not
        # flush: the operator may pause mid-thought while still holding the key,
        # and cutting them off there would be worse than no PTT at all. VAD is
        # strictly the substitute for an unobservable release.
        if self._mode is PTTMode.HOLD:
            return False
        try:
            lvl = float(level)
        except (TypeError, ValueError):
            return False
        now = self._clock()
        if lvl > _silence_level():
            self._last_voice_at = now
            return False
        if (now - self._last_voice_at) >= _silence_flush_s():
            self.close("silence")
            return True
        return False

    def held_s(self) -> float:
        if self._state is not MicState.OPEN:
            return 0.0
        return max(0.0, self._clock() - self._opened_at)


def build_ptt_key_bindings(
    latch: PTTLatch,
    *,
    buffer_getter: Optional[Callable[[], str]] = None,
    invalidate: Optional[Callable[[], None]] = None,
    detector: Optional[Any] = None,
    watchdog_factory: Optional[Callable[..., Any]] = None,
) -> Any:
    """A ``KeyBindings`` carrying ONLY the space intercept.

    Returned for the layout's existing ``extra_key_bindings`` merge seam — this
    adds a binding set, it does not rewrite the app's bindings (DRY).

    ``buffer_getter`` reports the current input text; when absent the binding
    falls back to the event's own buffer. The ``Condition`` filter means the
    binding is INERT unless the buffer is empty, so prompt_toolkit routes space
    to normal insertion whenever there is text — the binding cannot shadow
    typing."""
    from prompt_toolkit.filters import Condition
    from prompt_toolkit.key_binding import KeyBindings

    kb = KeyBindings()

    def _buffer_is_empty() -> bool:
        try:
            if buffer_getter is not None:
                return not (buffer_getter() or "").strip()
        except Exception:  # noqa: BLE001
            return False
        return True

    @Condition
    def _empty_and_enabled() -> bool:
        return ptt_enabled() and _buffer_is_empty()

    @kb.add(" ", filter=_empty_and_enabled, eager=True)
    def _(event) -> None:  # noqa: ANN001
        # Guard on the REAL buffer when the caller gave us no getter: the filter
        # may have been evaluated a beat earlier, and typing a space into a
        # freshly non-empty buffer must always win over the intercept.
        try:
            buf = getattr(event, "current_buffer", None)
            if buffer_getter is None and buf is not None:
                if (getattr(buf, "text", "") or "").strip():
                    buf.insert_text(" ")
                    return
        except Exception:  # noqa: BLE001
            pass
        # HOLD-TO-TALK, layered under the toggle rather than replacing it.
        #
        # A TTY sends no key-release, but a HELD key is not silent: the OS
        # repeats it. So a hold is observable as a rate and a release as that
        # rate stopping — see `hold_to_talk`.
        #
        # The first press opens the microphone IMMEDIATELY, exactly as the
        # toggle always did. Only what CLOSES it is decided later: repeats
        # arriving mean this was a hold, so releasing closes it; silence means
        # it was a tap, so it stays open until the next tap. Deciding the
        # close rather than the open is what keeps tap latency at zero and
        # removes any need to type a space and retroactively delete it — the
        # binding is already gated to an empty buffer, where space is a
        # control key rather than text, so there is nothing to take back.
        if detector is not None:
            try:
                from backend.core.ouroboros.ui.hold_to_talk import HoldAction

                action = detector.on_key()
                if action is HoldAction.WARMUP:
                    # A repeat that has not yet proved a hold. The mic is
                    # already open from the first press; toggling again here
                    # would shut it the instant the operator held the key.
                    _arm_hold_watchdog(latch, detector, watchdog_factory,
                                       invalidate)
                    return
                if action is HoldAction.SWALLOW:
                    # A repeat of a key already known to be held. Dropping it
                    # is the whole point: without this the buffer fills with
                    # spaces for as long as the operator speaks.
                    return
                if action is HoldAction.CONFIRM:
                    # Already open from the first press; nothing to do but
                    # let the watchdog close it on release.
                    if invalidate is not None:
                        try:
                            invalidate()
                        except Exception:  # noqa: BLE001
                            pass
                    return
                _arm_hold_watchdog(latch, detector, watchdog_factory,
                                   invalidate)
            except Exception:  # noqa: BLE001
                logger.debug("[PTT] hold detect degraded", exc_info=True)

        try:
            latch.toggle()
        except Exception:  # noqa: BLE001
            logger.debug("[PTT] toggle raised", exc_info=True)
        if invalidate is not None:
            try:
                invalidate()
            except Exception:  # noqa: BLE001
                pass

    return kb


def _arm_hold_watchdog(
    latch: "PTTLatch",
    detector: Any,
    watchdog_factory: Optional[Callable[..., Any]],
    invalidate: Optional[Callable[[], None]],
) -> None:
    """Start (or reuse) the watcher that decides when the key was let go.

    The RELEASE event closes the latch; TAP deliberately does nothing — the
    microphone stays open, which is the toggle behaviour that shipped before
    hold detection existed and must survive it unchanged.
    """
    from backend.core.ouroboros.ui.hold_to_talk import HoldEvent, HoldWatchdog

    existing = getattr(detector, "_ov_watchdog", None)
    if existing is not None and getattr(existing, "running", False):
        return

    def _on_event(event: Any) -> None:
        try:
            if event is HoldEvent.RELEASE:
                latch.close("release")
                if invalidate is not None:
                    invalidate()
        except Exception:  # noqa: BLE001
            logger.debug("[PTT] release close degraded", exc_info=True)

    factory = watchdog_factory or HoldWatchdog
    watchdog = factory(detector, _on_event)
    try:
        detector._ov_watchdog = watchdog  # noqa: SLF001 — one owner, one task
    except Exception:  # noqa: BLE001
        pass
    watchdog.kick()


__all__ = [
    "MicState",
    "PTTLatch",
    "build_ptt_key_bindings",
    "on_release_supported",
    "ptt_enabled",
]
