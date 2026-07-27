"""Degradation for terminals that cannot be drawn on.

A terminal that does not answer ``ESC[6n`` cannot be addressed: nothing can
know where the cursor is, so absolute positioning, overlays and repainting
regions all become guesses. That is the normal state of a CI job, a cron
entry, `ov | tee log.txt`, or a stripped SSH session — and in exactly those
places the output is usually being READ LATER AS TEXT, where escape sequences
are not merely useless but actively destroy the log.

So this module is two things:

``cpr_unsupported_signal``
    A hook onto prompt_toolkit's OWN cursor-position-report timeout. Nothing
    here re-probes the terminal.

``AppendOnlyWriter``
    A linear, strictly append-only stdout writer that emits plain text.

Not reimplementing the CPR probe is deliberate
----------------------------------------------
prompt_toolkit already asks for the cursor position, already waits with a
timeout (``Renderer.wait_for_cpr_responses``), already marks the terminal
``CPR_Support.NOT_SUPPORTED``, and already calls back when that happens. It
does not hang. The familiar

    WARNING: your terminal doesn't support cursor position requests (CPR).

is that machinery working correctly.

A second probe layered on top would race the first for the same response
bytes: whichever reader wins, the other times out, and a terminal that
answered perfectly well gets classified as dumb. The correct move is to
consume the signal that already exists and to tune its timeout through the
parameter that already exists.
"""
from __future__ import annotations

import logging
import os
import re
import sys
from typing import Any, Callable, Optional, TextIO

logger = logging.getLogger("Ouroboros.AppendOnly")

__all__ = [
    "AppendOnlyWriter",
    "cpr_timeout_s",
    "install_cpr_degradation",
    "plain_text",
    "strip_ansi",
]

#: CSI / OSC / charset-select escape sequences. Covers colour (SGR), cursor
#: movement, erase, mode set/reset, and OSC strings terminated by BEL or ST.
_ANSI_RE = re.compile(
    r"""
    \x1b \[ [0-9;?]* [ -/]* [@-~]      # CSI ... final byte
  | \x1b \] .*? (?: \x07 | \x1b\\ )    # OSC ... BEL or ST
  | \x1b [@-Z\\-_]                     # single-character escapes
  | \x1b [()][A-Za-z0-9]               # charset selection
    """,
    re.VERBOSE | re.DOTALL,
)


def cpr_timeout_s() -> float:
    """How long to wait for the terminal to report its cursor position.

    prompt_toolkit's own default is 1 second, which is a long time to sit
    still at boot when the answer is nearly always immediate or never. A real
    terminal replies within a round-trip; anything slower is indistinguishable
    from a pipe for our purposes.

    ``JARVIS_CPR_TIMEOUT_S`` tunes it — a heavily loaded remote session over a
    slow link is the one case where waiting longer is right.
    """
    try:
        value = float(os.environ.get("JARVIS_CPR_TIMEOUT_S", "0.2"))
        # Never zero: a zero timeout classifies EVERY terminal as dumb before
        # a reply could physically arrive.
        return max(0.05, min(5.0, value))
    except (TypeError, ValueError):
        return 0.2


def strip_ansi(text: Any) -> str:
    """Remove every escape sequence, leaving the characters a human reads."""
    try:
        return _ANSI_RE.sub("", str(text))
    except Exception:  # noqa: BLE001 — logging must never be the thing that fails
        return str(text)


def plain_text(payload: Any) -> str:
    """Render *payload* as plain text, whatever form it arrives in.

    Three shapes reach the client and each hides styling differently:

      * Rich renderables and ``Text`` objects — ``.plain`` is Rich's own
        answer, so markup semantics stay Rich's problem rather than becoming
        a regex here;
      * Rich MARKUP strings (``[bold red]x[/]``) — parsed by Rich for the
        same reason, since hand-rolling the bracket grammar means owning its
        edge cases (escaped brackets, unclosed tags) forever;
      * already-rendered ANSI — regex, because at that point the styling is
        no longer markup and Rich cannot un-render it.

    Applied in that order, so a payload carrying both markup and raw ANSI
    (the mirrored-router frames do) comes out clean either way.
    """
    try:
        if payload is None:
            return ""
        # 1. A Rich object that can flatten itself.
        plain = getattr(payload, "plain", None)
        if isinstance(plain, str):
            return strip_ansi(plain)

        text = str(payload)
        # 2. Rich markup — but ONLY when a CLOSING tag is present.
        #
        #    `[` and `]` are not evidence of markup. Log lines are full of
        #    `[daemon]`, `[worker]`, `[Advisor]`, `tokens=[15k]`, and Rich
        #    parses every one of those as an unknown style tag and DELETES it.
        #    A filter whose job is to preserve logs as readable text must not
        #    silently eat their prefixes; that is a worse corruption than the
        #    escape codes it was added to remove, because it is invisible.
        #
        #    A closing `[/` is the cheap discriminator: emitted markup carries
        #    one, incidental brackets do not.
        if "[/" in text:
            try:
                from rich.text import Text
                text = Text.from_markup(text).plain
            except Exception:  # noqa: BLE001 — not markup after all
                pass
        # 3. Whatever escape sequences survived.
        return strip_ansi(text)
    except Exception:  # noqa: BLE001
        try:
            return strip_ansi(str(payload))
        except Exception:  # noqa: BLE001
            return ""


class AppendOnlyWriter:
    """A strictly linear stdout sink for terminals that cannot be addressed.

    Every write is a whole line appended at the bottom. There is no cursor
    movement, no erase, no region, no repaint — because on a stream that
    cannot report its cursor position, all of those are writes to an unknown
    location, and the visible result is a scrambled log.

    Idempotent line endings and a flush per line: the common consumer is
    ``tee``, a CI log collector, or a file being tailed while the process
    runs, and a buffered final chunk is the difference between a useful log
    and an empty one when the process is killed.
    """

    def __init__(self, stream: Optional[TextIO] = None) -> None:
        self._stream = stream if stream is not None else sys.stdout
        self.lines_written = 0

    def write(self, payload: Any) -> None:
        """Append *payload* as plain text. NEVER raises."""
        try:
            text = plain_text(payload)
            if not text:
                return
            for line in text.splitlines() or [""]:
                self._stream.write(line + "\n")
                self.lines_written += 1
            try:
                self._stream.flush()
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001 — output must not kill the client
            logger.debug("[AppendOnly] write degraded", exc_info=True)

    # Enough of a file-like surface to stand in for a console sink.
    def print(self, *args: Any, **_kwargs: Any) -> None:
        self.write(" ".join(str(a) for a in args))

    def flush(self) -> None:
        try:
            self._stream.flush()
        except Exception:  # noqa: BLE001
            pass

    def isatty(self) -> bool:
        return False        # by construction: this exists because it is not one


def install_cpr_degradation(
    app: Any, on_degrade: Callable[[], None],
) -> bool:
    """Call *on_degrade* if the terminal never reports its cursor position.

    Hooks prompt_toolkit's ``cpr_not_supported_callback`` rather than probing:
    see the module docstring for why a second probe would misclassify healthy
    terminals by racing the first for the same bytes.

    Fires at most once — the renderer latches ``NOT_SUPPORTED`` and would
    otherwise call back on every subsequent render. Returns True if the hook
    was installed. NEVER raises.
    """
    try:
        fired = []

        def _once() -> None:
            if fired:
                return
            fired.append(1)
            logger.info(
                "[AppendOnly] terminal did not report cursor position — "
                "degrading to append-only output",
            )
            try:
                on_degrade()
            except Exception:  # noqa: BLE001
                logger.debug("[AppendOnly] degrade hook failed", exc_info=True)

        app.renderer.cpr_not_supported_callback = _once
        return True
    except Exception:  # noqa: BLE001
        logger.debug("[AppendOnly] could not install CPR hook", exc_info=True)
        return False
