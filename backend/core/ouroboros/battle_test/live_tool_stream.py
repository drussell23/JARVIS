"""What a running command is doing, while it is still doing it.

A tool call was a black box between invocation and result. `_bash` runs
through a Docker sandbox whose runner returned ``(rc, out, err)`` — a
completion-only contract — so the operator saw the command, then silence,
then a wall of output. For a 40-second `pytest` that is 40 seconds of
nothing, which is indistinguishable from a hang.

The runner now accepts an optional observer. This module is the sink that
turns those bytes into something a cockpit can draw, and it exists as its
own module rather than a lambda inside ``_bash`` because the interesting
part is not the plumbing — it is the five ways naive forwarding goes wrong.

WHAT IT IS NOT
--------------
Not a transcript. A command's live tail is STATE: the last frame wins, and
a dropped frame costs a tick of smoothness rather than a line of history.
The completed output still arrives through the normal tool-result path and
is still the authoritative record. Nothing here is the record.

THE FIVE HAZARDS
----------------
1. **Volume.** `pytest -q` emits thousands of lines. Publishing each one
   would flood the bridge with frames nobody can read. Frames are
   COALESCED on an interval — the tail is a picture of now, not a log.

2. **Secrets.** Shell output is arbitrary. A command that echoes an env
   var would spray a credential across every attached cockpit and into any
   terminal scrollback. Redacted through the firewall's own pattern set,
   which is documented as the authoritative credential-shape redactor —
   a second pattern list here would rot the moment the real one gained a
   sixth shape.

3. **Progress bars.** pytest, pip and npm repaint with carriage returns.
   Forwarded literally, a single logical line arrives as hundreds of
   frames, each one a redraw of the same row. `\\r` segments collapse to
   their last state, which is what the writer meant them to display.

4. **Escape sequences.** Colour codes and cursor moves in a byte stream
   would repaint or corrupt a cockpit that is not a terminal emulator.
   Stripped, not rendered.

5. **Thread affinity.** `_bash` runs in a thread-pool executor, so this
   sink is called off the event loop. `publish_telemetry` is explicitly
   cross-loop marshalled, which is why it is the transport and a direct
   client write is not.

NEVER raises, in the strong sense. The observer contract says a sink that
throws is swallowed by the runner, but relying on that would mean every
failure costs a container read. A progress renderer must not be able to
slow, break, or alter the command it is watching.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from typing import Callable, List, Optional

logger = logging.getLogger("Ouroboros.LiveToolStream")

LIVE_TOOL_STREAM_SCHEMA_VERSION = "tool_stream.v1"

MASTER_FLAG_ENV_VAR = "JARVIS_LIVE_TOOL_STREAM_ENABLED"

#: Seconds between published frames. The tail is a picture of NOW, so the
#: rate is bounded by what an eye can follow, never by what a command can
#: emit — a build that prints 50k lines publishes the same ~10 frames a
#: second as one that prints 50.
_DEFAULT_INTERVAL_S = 0.12

#: Characters of tail carried per frame. Bounded here as well as at the
#: renderer because an unbounded string crosses the bridge every frame.
_MAX_TAIL_CHARS = 600

#: Lines of tail retained. A command's live view is the last few lines;
#: the full output is still delivered as the tool result.
_MAX_TAIL_LINES = 6

#: ANSI CSI / OSC sequences. Stripped rather than rendered — the cockpit
#: canvas is not a terminal emulator, and a stray cursor-move would
#: repaint rows that belong to other producers.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")

#: Remaining control characters, tab and newline excepted.
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def live_tool_stream_enabled() -> bool:
    """Default ON. Off, a command is a black box again — which is the
    state this module exists to end, so it is off only by choice."""
    try:
        return os.environ.get(
            MASTER_FLAG_ENV_VAR, "1",
        ).strip().lower() not in ("0", "false", "no", "off")
    except Exception:  # noqa: BLE001
        return True


def _interval_s() -> float:
    try:
        return max(0.02, min(2.0, float(os.environ.get(
            "JARVIS_LIVE_TOOL_STREAM_INTERVAL_S", _DEFAULT_INTERVAL_S))))
    except Exception:  # noqa: BLE001
        return _DEFAULT_INTERVAL_S


def _collapse_carriage_returns(text: str) -> str:
    """Resolve ``\\r`` repaints to what the writer meant to display.

    A progress bar is ONE logical line rewritten in place. Splitting on
    ``\\n`` alone turns it into a single enormous line containing every
    intermediate state; forwarding each ``\\r`` segment turns it into
    hundreds of frames of the same row. Keeping the last segment of each
    line is what a terminal would have shown.
    """
    out: List[str] = []
    for line in text.split("\n"):
        out.append(line.rsplit("\r", 1)[-1] if "\r" in line else line)
    return "\n".join(out)


def sanitize_stream_text(text: object) -> str:
    """Make arbitrary command output safe to display. NEVER raises.

    Order matters: collapse repaints first (so a redacted secret cannot be
    reassembled from two halves of a repainted line), then strip escapes,
    then redact. Redaction LAST means it sees the final visible text
    rather than a form that could still be rewritten.
    """
    try:
        raw = str(text or "")
        if not raw:
            return ""
        flat = _collapse_carriage_returns(raw)
        flat = _ANSI_RE.sub("", flat)
        flat = _CTRL_RE.sub("", flat)
        return _redact(flat)
    except Exception:  # noqa: BLE001
        # A sanitizer that fails must yield NOTHING, never the raw text.
        # The one output worse than no progress display is a credential in
        # everyone's scrollback.
        logger.debug("[LiveToolStream] sanitize degraded", exc_info=True)
        return ""


def _redact(text: str) -> str:
    """Mask credential shapes using the firewall's OWN pattern set.

    Asked rather than restated. `semantic_firewall` documents its set as
    the authoritative credential-shape redactor, and a private copy here
    would silently stop covering a sixth shape the day one is added there.
    """
    try:
        from backend.core.ouroboros.governance.semantic_firewall import (
            _CREDENTIAL_SHAPE_PATTERNS,
        )
        for pattern in _CREDENTIAL_SHAPE_PATTERNS:
            text = pattern.sub("[redacted]", text)
        return text
    except Exception:  # noqa: BLE001
        # The pattern set could not be consulted, so nothing can be
        # asserted about this text. Fail CLOSED: no display beats an
        # unscanned one.
        logger.debug("[LiveToolStream] redactor unavailable", exc_info=True)
        return ""


class LiveToolStream:
    """A coalescing sink for one running command's output.

    Thread-safe: created on the event loop, called from the executor
    thread that runs the tool, and published through the bridge's
    cross-loop marshalling.
    """

    def __init__(
        self, *, tool: str, op_id: str = "", label: str = "",
        publish: Optional[Callable[[dict], None]] = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self._tool = str(tool or "tool")
        self._op_id = str(op_id or "")
        self._label = str(label or "")
        self._clock = clock or time.monotonic
        self._publish = publish
        self._lock = threading.Lock()
        self._tail: List[str] = []
        #: Per-stream carry for a line split across two read blocks.
        self._partial: dict = {}
        self._last_emit = 0.0
        self._pending = False
        self._started = self._clock()
        self._frames = 0

    # -- the observer the runner calls ---------------------------------

    def __call__(self, stream: str, text: str) -> None:
        """``(stream, text)`` — the runner's observer contract.

        Accumulates and emits at most once per interval. NEVER raises and
        never blocks: this runs inside the container read loop, and a slow
        sink would throttle the command it is watching.
        """
        try:
            clean = sanitize_stream_text(text)
            if not clean:
                return
            with self._lock:
                # The runner reads fixed-size BLOCKS, so a chunk boundary
                # lands mid-line as often as not. Splitting each chunk
                # independently would emit one line as two entries, and
                # append the empty fragment after every trailing newline —
                # which is where the blank rows came from. The remainder
                # after the last newline is a PARTIAL line and is carried
                # until the rest of it arrives.
                buf = self._partial.get(stream, "") + clean
                parts = buf.split("\n")
                self._partial[stream] = parts.pop()
                for line in parts:
                    # stderr is TAGGED rather than silently interleaved.
                    # The two pipes are drained by separate tasks, so their
                    # relative order here is an artefact of scheduling, not
                    # of what the command did — presenting it as faithful
                    # interleaving would be a small fiction, and an
                    # operator reading a failure needs to know which stream
                    # a line came from.
                    self._tail.append(
                        line if stream != "stderr" else f"stderr │ {line}")
                # Bounded in the ACCUMULATOR, not just at render: a command
                # emitting 50k lines must not grow this list to 50k.
                if len(self._tail) > _MAX_TAIL_LINES * 4:
                    del self._tail[:-_MAX_TAIL_LINES]
                self._pending = True
                now = self._clock()
                if now - self._last_emit < _interval_s():
                    return
                self._last_emit = now
            self._emit(done=False)
        except Exception:  # noqa: BLE001
            logger.debug("[LiveToolStream] observe degraded", exc_info=True)

    # -- lifecycle -----------------------------------------------------

    def finish(self, *, summary: str = "") -> None:
        """The command ended. Retire the strip. NEVER raises.

        Always emits, ignoring the interval: the LAST frame is the one
        that clears the display, and dropping it on a rate limit would
        leave a finished command looking like it is still running.
        """
        try:
            self._emit(done=True, summary=summary)
        except Exception:  # noqa: BLE001
            logger.debug("[LiveToolStream] finish degraded", exc_info=True)

    def _emit(self, *, done: bool, summary: str = "") -> None:
        with self._lock:
            if not self._pending and not done:
                return
            self._pending = False
            if done:
                # Flush any trailing partial line: a command whose final
                # line lacks a newline would otherwise never be shown.
                for stream, rest in list(self._partial.items()):
                    if rest.strip():
                        self._tail.append(
                            rest if stream != "stderr" else f"stderr │ {rest}")
                self._partial.clear()
            tail = "\n".join(self._tail[-_MAX_TAIL_LINES:])[-_MAX_TAIL_CHARS:]
            elapsed = max(0.0, self._clock() - self._started)
            self._frames += 1
            frames = self._frames
        payload = {
            "kind": "tool_stream",
            "schema_version": LIVE_TOOL_STREAM_SCHEMA_VERSION,
            "tool": self._tool,
            "op_id": self._op_id,
            "label": self._label,
            # ELAPSED, never a start timestamp. `time.monotonic()` is a
            # per-process origin, so a reader subtracting its own clock
            # would show a duration wrong by however long the two
            # processes have been alive — and plausibly wrong, which is
            # worse than obviously wrong.
            "elapsed_s": round(elapsed, 2),
            "text": "" if done else tail,
            "summary": str(summary or "")[:200],
            "done": bool(done),
            "frames": frames,
        }
        self._send(payload)

    def _send(self, payload: dict) -> None:
        # THE seam, and deliberately here rather than at the construction
        # site. `cockpit_mount` recorded the correct fix as "a
        # `set_active_stream` registry ... which needs a producer-side audit
        # of every stream construction site" — but construction sites can
        # multiply, and a producer that forgets to register is silently dark.
        # Every frame a stream ever emits passes through this one method
        # regardless of how the stream was made, so recording here cannot be
        # forgotten by a future caller.
        #
        # Before the `self._publish` branch, so an injected publisher (the
        # demo, the tests) records identically to the real bridge.
        try:
            from backend.core.ouroboros.battle_test.inflight_registry import (
                note_inflight_frame,
            )
            note_inflight_frame(payload)
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._publish is not None:
                self._publish(payload)
                return
            from backend.core.ouroboros.battle_test.cockpit_attach import (
                publish_telemetry_global,
            )
            publish_telemetry_global(payload)
        except Exception:  # noqa: BLE001
            logger.debug("[LiveToolStream] publish degraded", exc_info=True)


def make_tool_observer(
    *, tool: str, op_id: str = "", label: str = "",
    publish: Optional[Callable[[dict], None]] = None,
) -> Optional[LiveToolStream]:
    """A sink for this command, or None when nobody is watching.

    ``None`` is load-bearing: the runner's streaming path exists only when
    an observer is supplied, and with no cockpit attached the proven
    `communicate()` path should run untouched. Paying the cost of manual
    pipe draining to feed a display nobody can see would be strictly
    worse than the black box it replaces.
    """
    try:
        if not live_tool_stream_enabled():
            return None
        if publish is None:
            from backend.core.ouroboros.battle_test.cockpit_attach import (
                attached_cockpits,
            )
            if attached_cockpits() <= 0:
                return None
        return LiveToolStream(
            tool=tool, op_id=op_id, label=label, publish=publish)
    except Exception:  # noqa: BLE001
        logger.debug("[LiveToolStream] observer unavailable", exc_info=True)
        return None


__all__ = [
    "LIVE_TOOL_STREAM_SCHEMA_VERSION",
    "MASTER_FLAG_ENV_VAR",
    "LiveToolStream",
    "live_tool_stream_enabled",
    "make_tool_observer",
    "sanitize_stream_text",
]
