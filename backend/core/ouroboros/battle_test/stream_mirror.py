"""Model output, streamed to an attached cockpit.

`StreamRenderer` shows tokens arriving as the model thinks — the difference
between watching work happen and reading a report of it. It renders through
Rich `Live`, an animated in-place widget, which means two things:

  * it needs a real interactive TTY, so a `--headless` daemon falls through to
    the plain path;
  * it cannot be mirrored. `Live` repaints a region by moving the cursor, and
    a remote surface has no cursor to move — replaying those escapes would
    corrupt the cockpit rather than animate it.

So the local widget is left exactly as it is, and this ADDS a second consumer
of the same token feed that emits **committed text frames** instead. The
cockpit gets prose; the local terminal keeps its animation. Neither is a
degraded version of the other — they are different renderings of one stream.

Why frames and not tokens
--------------------------
One bridge write per token would put thousands of frames on the socket for a
single reply, and an attached client would spend its time parsing JSON rather
than drawing. Tokens accumulate and flush on a boundary — either enough text
to be worth sending, or a natural break in the prose.

`find_commit_boundary` from `stream_renderer` decides where prose can be cut
safely. Reusing it means the cockpit breaks text at the same places the local
renderer commits it, so the two surfaces never disagree about what has been
"said" — and markdown that spans a boundary (a fenced block, a list) is not
severed mid-structure.

Backpressure
------------
The flush is bounded and drop-oldest, for the reason the console spooler is:
an attached cockpit that stops reading must never become a memory leak in the
organism, and when it falls behind the RECENT text is what the operator is
waiting for.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable, List, Optional

logger = logging.getLogger("Ouroboros.StreamMirror")

__all__ = ["StreamMirror", "stream_mirror_enabled", "fan_out_tokens"]

#: Flush when this much text has accumulated, even without a clean boundary.
#: A long unbroken paragraph must not sit unsent while the operator waits.
_FLUSH_CHARS = 160

#: …or when this long has passed since the last flush. Bounds latency for a
#: slow stream, where char count alone would hold text indefinitely.
_FLUSH_INTERVAL_S = 0.35

#: Never buffer more than this. A cockpit that stops reading is a bug in the
#: cockpit, not a reason for the daemon to grow.
_MAX_BUFFER_CHARS = 8192


def stream_mirror_enabled() -> bool:
    """Default ON: this closes the last structural gap in the transcript."""
    return os.environ.get(
        "JARVIS_STREAM_MIRROR_ENABLED", "1",
    ).strip().lower() not in ("0", "false", "no", "off")


class StreamMirror:
    """Accumulates tokens and emits committed text frames to the cockpit."""

    def __init__(
        self,
        emit: Optional[Callable[[str], None]] = None,
        *,
        flush_chars: int = _FLUSH_CHARS,
        flush_interval_s: float = _FLUSH_INTERVAL_S,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        # INJECTED sink and clock: this must be testable with no bridge, no
        # daemon and no wall-clock dependence.
        self._emit = emit
        self._flush_chars = max(1, int(flush_chars))
        self._interval = max(0.0, float(flush_interval_s))
        self._clock = clock or time.monotonic
        self._buf: List[str] = []
        self._size = 0
        self._last_flush = self._clock()
        self.frames_emitted = 0
        self.chars_dropped = 0

    # -- token side --------------------------------------------------------

    def on_token(self, text: Any) -> None:
        """Accept one token. NEVER raises, never blocks."""
        try:
            chunk = str(text or "")
            if not chunk:
                return
            self._buf.append(chunk)
            self._size += len(chunk)
            if self._size > _MAX_BUFFER_CHARS:
                # Drop from the FRONT: when a cockpit falls behind, the recent
                # text is what the operator is waiting for.
                dropped = self._size - _MAX_BUFFER_CHARS
                joined = "".join(self._buf)[dropped:]
                self._buf = [joined]
                self._size = len(joined)
                self.chars_dropped += dropped
            if self._size >= self._flush_chars:
                self.flush()
            elif (self._clock() - self._last_flush) >= self._interval:
                # A TIME-triggered flush must actually emit. Requiring a
                # boundary here defeats the timer: a slow model producing
                # short, unpunctuated text would hold it forever and the
                # operator would watch a still screen. The deadline is the
                # whole point, so it overrides the preference for a clean cut.
                self.flush(force=True)
        except Exception:  # noqa: BLE001 — a mirror must never break a stream
            logger.debug("[StreamMirror] token degraded", exc_info=True)

    def _should_flush(self) -> bool:
        if self._size >= self._flush_chars:
            return True
        return (self._clock() - self._last_flush) >= self._interval

    def flush(self, force: bool = False) -> None:
        """Emit what has accumulated, cut at a safe boundary. NEVER raises.

        ``force`` emits even without a boundary — used by the latency timer,
        where waiting for a clean cut would defeat the deadline it exists to
        enforce.
        """
        try:
            if not self._buf:
                return
            text = "".join(self._buf)
            head, tail = self._split_at_boundary(text, force=force)
            if not head:
                # No safe cut yet and not over the hard cap — keep waiting
                # rather than severing a fenced block or a half-written list.
                return
            self._buf = [tail] if tail else []
            self._size = len(tail)
            self._last_flush = self._clock()
            sink = self._emit
            if sink is not None and head.strip():
                sink(head)
                self.frames_emitted += 1
        except Exception:  # noqa: BLE001
            logger.debug("[StreamMirror] flush degraded", exc_info=True)

    def _split_at_boundary(self, text: str, force: bool = False) -> tuple:
        """(committed, remainder) using the renderer's own boundary rule.

        Falls back to sending everything when the rule cannot find a cut and
        the buffer is already large — holding text hostage to a boundary that
        never arrives is worse than one awkward break.
        """
        try:
            from backend.core.ouroboros.battle_test.stream_renderer import (
                find_commit_boundary,
            )
            idx = find_commit_boundary(text)
            if idx and 0 < idx <= len(text):
                return text[:idx], text[idx:]
        except Exception:  # noqa: BLE001
            pass
        if force or len(text) >= self._flush_chars:
            return text, ""
        return "", text

    # -- lifecycle ---------------------------------------------------------

    def end(self) -> None:
        """Stream finished — emit whatever is left, boundary or not."""
        try:
            if self._buf:
                text = "".join(self._buf)
                self._buf, self._size = [], 0
                self._last_flush = self._clock()
                sink = self._emit
                if sink is not None and text.strip():
                    sink(text)
                    self.frames_emitted += 1
        except Exception:  # noqa: BLE001
            logger.debug("[StreamMirror] end degraded", exc_info=True)

    @property
    def pending_chars(self) -> int:
        return self._size


def fan_out_tokens(renderer: Any, mirror: StreamMirror) -> Any:
    """Wrap a StreamRenderer so tokens reach BOTH it and the mirror.

    A decorator rather than a replacement, for the same reason the subagent
    narrator wraps its comm sink: the local `Live` widget is correct where it
    runs, and the registry holds ONE renderer. Providers keep calling
    `get_stream_renderer().on_token` and are never told there are now two
    consumers.

    Delegates to the real renderer FIRST — the local terminal is the surface
    that has always worked, and a mirror fault must not degrade it.
    """
    if renderer is None:
        return renderer

    class _FannedOut:
        def on_token(self, text: Any) -> None:
            try:
                renderer.on_token(text)
            except Exception:  # noqa: BLE001
                logger.debug("[StreamMirror] inner on_token failed",
                             exc_info=True)
            mirror.on_token(text)

        def end(self) -> None:
            try:
                renderer.end()
            except Exception:  # noqa: BLE001
                logger.debug("[StreamMirror] inner end failed", exc_info=True)
            mirror.end()

        def __getattr__(self, name: str) -> Any:
            # Everything else — start(), notify(), flush(), the counters —
            # belongs to the real renderer. Reimplementing its surface would
            # mean silently dropping whatever it grows next.
            return getattr(renderer, name)

    return _FannedOut()
