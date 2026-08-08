"""A real terminal running a real process, driven byte by byte.

Extracted from ``test_cockpit_pty_proof.CockpitSession`` when a second suite
needed the identical driver. Two copies of "spawn under a pty and drain it"
would drift in exactly the details that make a terminal test trustworthy — the
drain-before-exec ordering, the mark/delta discipline, the wait-never-sleep
rule — and the copy that drifted would be the one reporting green.

WHAT ASSERTING ON THE OUTPUT HONESTLY MEANS
-------------------------------------------
A pty carries a STREAM, not a screen: cursor moves, partial repaints and
styling interleave, so the buffer is the terminal's INPUT, not its picture.
Reconstructing the picture needs an emulator, and asserting against one would
be asserting against the emulator's fidelity as much as the program's.

So callers assert on what is decidable from a stream: that a sequence was
negotiated (exact bytes), that a keystroke CHANGED something (a delta measured
from a mark), or that specific text appeared after an input that should produce
it. Weaker than a screenshot, far stronger than nothing, and it catches the
entire class of "the handler is right but the binding never fires".

TIMING IS A WAIT, NEVER A SLEEP
-------------------------------
``wait_for`` polls against a deadline. A fixed sleep tuned on one machine is a
flake on a loaded one, and a flaky terminal test gets deleted rather than fixed.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import threading
import time
from typing import List, Optional, Sequence

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pty_input import key, set_winsize  # noqa: E402

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

#: CSI/OSC escape sequences. Stripped only for READING; the raw stream is kept
#: intact so tests that assert on negotiation bytes still can.
_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
                   r"|\x1b[=>NOP\\]|\x1b\([AB0]")


def clean(text: str) -> str:
    """The printable text a terminal would have drawn, styling removed."""
    return _ANSI.sub("", text or "")


class PtyProcess:
    """A subprocess whose stdio is a genuine kernel pty."""

    #: Wide enough that a deck does not fold, tall enough that a viewport has
    #: rows to scroll. Both matter: several cockpit features are no-ops on a
    #: screen too small to show what they changed.
    DEFAULT_COLS = 110
    DEFAULT_ROWS = 34

    def __init__(self, argv: Sequence[str], *, env: Optional[dict] = None,
                 cols: Optional[int] = None,
                 rows: Optional[int] = None) -> None:
        import pty

        cols = self.DEFAULT_COLS if cols is None else cols
        rows = self.DEFAULT_ROWS if rows is None else rows

        self._master, slave = pty.openpty()
        set_winsize(self._master, cols, rows)

        base = dict(os.environ)
        base.update({
            "TERM": "xterm-256color",
            "COLUMNS": str(cols),
            "LINES": str(rows),
            "PYTHONUNBUFFERED": "1",
        })
        base.update(env or {})

        self.proc = subprocess.Popen(
            list(argv), stdin=slave, stdout=slave, stderr=slave,
            env=base, cwd=REPO, start_new_session=True,
        )
        os.close(slave)

        self._chunks: List[str] = []
        self._lock = threading.Lock()
        # Drained from the instant the child starts. A pty has a finite kernel
        # buffer: a child that outruns an undrained reader blocks in write(),
        # and the test then measures a stall it caused itself.
        self._drain = threading.Thread(target=self._pump, daemon=True)
        self._drain.start()

    def _pump(self) -> None:
        while True:
            try:
                data = os.read(self._master, 65536)
            except OSError:
                return
            if not data:
                return
            with self._lock:
                self._chunks.append(data.decode("utf-8", "replace"))

    # -- reading ------------------------------------------------------------
    @property
    def output(self) -> str:
        with self._lock:
            return "".join(self._chunks)

    def mark(self) -> int:
        """A cursor into the stream. Everything after it is attributable."""
        return len(self.output)

    def since(self, at: int) -> str:
        return self.output[at:]

    def wait_for(self, needle: str, *, timeout: float = 25.0,
                 after: int = 0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if needle in clean(self.since(after)):
                return True
            if self.proc.poll() is not None:
                # One last look: the text may have arrived in the same breath
                # the process exited.
                return needle in clean(self.since(after))
            time.sleep(0.05)
        return False

    def wait_for_change(self, at: int, *, minimum: int = 32,
                        timeout: float = 8.0) -> str:
        """Wait for at least ``minimum`` new bytes; return whatever arrived.

        A repaint is many bytes. The floor rejects the stray single-byte cursor
        report that would otherwise make any keystroke look handled.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            delta = self.since(at)
            if len(delta) >= minimum:
                return delta
            time.sleep(0.05)
        return self.since(at)

    # -- writing ------------------------------------------------------------
    def send(self, data: "bytes | str") -> None:
        if isinstance(data, str):
            data = data.encode()
        os.write(self._master, data)

    def press(self, *names: str, settle: float = 0.06) -> None:
        for n in names:
            self.send(key(n))
            time.sleep(settle)   # let the event loop turn between keys

    def signal(self, sig: int) -> None:
        os.killpg(os.getpgid(self.proc.pid), sig)

    # -- lifecycle ----------------------------------------------------------
    def close(self, *, timeout: float = 5.0) -> None:
        try:
            if self.proc.poll() is None:
                self.signal(signal.SIGTERM)
                try:
                    self.proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    self.signal(signal.SIGKILL)
                    self.proc.wait(timeout=timeout)
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                os.close(self._master)
            except OSError:
                pass

    def __enter__(self) -> "PtyProcess":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
