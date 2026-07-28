"""The terminal's other buffer, entered before anything is drawn.

A terminal keeps two screen buffers. The normal one accumulates scrollback —
everything you have ever run is up there. The **alternate screen** does not:
it is a fixed viewport that a full-screen program borrows, and when the
program leaves, the normal buffer comes back untouched, with its scrollback
intact. `vim`, `less` and every TUI you cannot scroll out of live there.

    ESC[?1049h   enter (termcap `smcup`)
    ESC[?1049l   leave (termcap `rmcup`)

The cockpit already asked for it — prompt_toolkit's ``full_screen=True``
issues those sequences. What was wrong was the ORDER. The crest animation,
the wake logs and the attach summary were all printed first, to the NORMAL
buffer, and only then did the cockpit switch. So the logo was sitting in the
scrollback behind the cockpit, and scrolling up found it.

Nothing about that is fixed by drawing differently. The logo has to be
painted somewhere it can never be scrolled back to, which means the switch
has to happen before the first byte of it — hence a context manager wrapping
the whole boot rather than a flag on the widget at the end of it.

Why not just erase the scrollback
---------------------------------
``ESC[3J`` (Erase Saved Lines) would also hide the logo, and it is the wrong
tool: it destroys the operator's ENTIRE terminal history, including
everything they did before running `ov`. Borrowing a buffer and giving it
back is reversible; deleting someone's scrollback is not. Reach for the
alternate screen, never for ED-3.

Leaving is not optional
-----------------------
A process that enters the alternate screen and dies without leaving hands
back a terminal stuck in a fixed viewport with no scrollback and, usually, a
hidden cursor. That is a wrecked shell, and it is the only way this module
can do real harm — so restoration is defended three times over: the context
manager's ``finally``, an ``atexit`` backstop, and a signal handler chain
that restores before deferring to whatever was installed before it.

``SIGKILL`` remains unrecoverable by design. Nothing in-process can catch it.

Nesting
-------
prompt_toolkit issues its own smcup/rmcup when the cockpit mounts inside
this. That is safe and deliberate: 1049h while already on the alternate
screen is idempotent, and pt's rmcup on exit simply returns to the normal
buffer slightly before this context manager would have. The operator sees no
flash back to the shell mid-boot, which is the whole point. Re-entry here is
depth-counted so only the outermost exit actually switches back.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
from contextlib import contextmanager
from typing import Any, Iterator, Optional

logger = logging.getLogger("Ouroboros.AltScreen")

__all__ = ["alt_screen_enabled", "alternate_screen", "in_alternate_screen"]

#: Enter the alternate screen, then home the cursor. The cursor move matters:
#: the alternate buffer retains whatever a previous occupant left in it on
#: some terminals, so a boot that starts drawing at the old cursor position
#: renders halfway down the screen.
_ENTER = "\x1b[?1049h\x1b[H"

#: Leave. No clear first — the point is to hand the normal buffer back
#: exactly as it was found.
_LEAVE = "\x1b[?1049l"

_LOCK = threading.RLock()
_DEPTH = 0
_ATEXIT_TOKEN: Any = None


def alt_screen_enabled() -> bool:
    """Should the boot claim the alternate screen?

    Defers to the cockpit's OWN decision (`fullscreen_enabled`) rather than
    introducing a second switch: a boot that hides the crest and then mounts
    an inline cockpit would leave the operator staring at a blank shell, and
    two flags that must agree eventually will not. One question, asked once.

    ``JARVIS_ALT_SCREEN_BOOT=0`` opts the BOOT out on its own, for anyone who
    wants the crest left in their scrollback.
    """
    raw = os.environ.get("JARVIS_ALT_SCREEN_BOOT", "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    try:
        from backend.core.ouroboros.battle_test.bipartite_layout import (
            fullscreen_enabled,
        )
        return bool(fullscreen_enabled())
    except Exception:  # noqa: BLE001 — no cockpit, no takeover
        return False


def in_alternate_screen() -> bool:
    with _LOCK:
        return _DEPTH > 0


def _out() -> Any:
    """The REAL stdout.

    ``sys.stdout`` may be a `patch_stdout` proxy or a Rich capture by the
    time a restore runs — and a restore that writes into a proxy instead of
    the terminal leaves the operator's shell wrecked. `sys.__stdout__` is
    Python's untouched reference, the same one `real_stdout_isatty` reads.
    """
    return sys.__stdout__ or sys.stdout


def _write(seq: str) -> bool:
    """Emit a control sequence and flush immediately. NEVER raises.

    Flushed rather than buffered because the next thing that happens may be
    a crash, and a restore sitting in a buffer is a restore that did not
    happen.
    """
    try:
        stream = _out()
        if stream is None or stream.closed:
            return False
        stream.write(seq)
        stream.flush()
        return True
    except Exception:  # noqa: BLE001
        return False


def _restore_now() -> None:
    """Unconditional return to the normal buffer. Safe to call repeatedly."""
    global _DEPTH
    with _LOCK:
        if _DEPTH <= 0:
            return
        _DEPTH = 0
    _write(_LEAVE)


def _install_backstops() -> None:
    """atexit + signal restoration, installed only while we are borrowing.

    Signals are CHAINED, never replaced: the harness installs its own
    SIGTERM/SIGINT handlers to write a partial summary, and swallowing those
    would trade a wrecked terminal for a lost session record. This restores
    the screen first — it is the cheap, non-blocking part — and then calls
    whatever was there before.
    """
    global _ATEXIT_TOKEN
    if _ATEXIT_TOKEN is None:
        try:
            from backend.core.ouroboros.governance.exit_guard import (
                guarded_atexit_register,
            )
            # GUARDED, never raw `atexit.register`. A Ctrl+C landing inside a
            # bare exit handler prints a traceback across the goodbye — the
            # exact defect `exit_guard` exists to prevent, and there is a
            # grep-enforced invariant test that will fail on a raw one.
            #
            # No fallback if that import fails: registering rawly to be
            # "safe" would trade a restored terminal for a tracebacked one,
            # and the `finally` plus the signal chain below already cover
            # every path atexit would have.
            _ATEXIT_TOKEN = guarded_atexit_register(_restore_now)
        except Exception:  # noqa: BLE001
            logger.debug("[AltScreen] atexit backstop unavailable",
                         exc_info=True)

    try:
        import signal

        if threading.current_thread() is not threading.main_thread():
            # signal.signal only works on the main thread; the atexit
            # backstop still covers this case.
            return

        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            try:
                previous = signal.getsignal(sig)
                if getattr(previous, "_ov_alt_screen_chained", False):
                    continue

                def _handler(signum: int, frame: Any,
                             _prev: Any = previous) -> None:
                    _restore_now()
                    if callable(_prev):
                        _prev(signum, frame)
                    elif _prev == signal.SIG_DFL:
                        # Re-raise with the default disposition so the exit
                        # status still reports the signal honestly.
                        signal.signal(signum, signal.SIG_DFL)
                        os.kill(os.getpid(), signum)

                _handler._ov_alt_screen_chained = True  # type: ignore[attr-defined]
                signal.signal(sig, _handler)
            except (OSError, ValueError, RuntimeError):
                continue
    except Exception:  # noqa: BLE001
        pass


@contextmanager
def alternate_screen(enabled: Optional[bool] = None) -> Iterator[bool]:
    """Borrow the terminal's alternate screen for this block.

    Yields True when the switch actually happened, so a caller can adapt
    (there is no scrollback to leave a summary in, for instance).

    Enters at most once however deeply nested — only the outermost exit hands
    the buffer back, so an inner block cannot drop the operator to their
    shell halfway through a boot.

    NEVER raises on entry: failing to borrow the screen means the boot looks
    like it used to, which is a far better outcome than not booting.
    """
    global _DEPTH
    want = alt_screen_enabled() if enabled is None else bool(enabled)
    entered = False
    if want:
        with _LOCK:
            if _DEPTH == 0:
                entered = _write(_ENTER)
                if entered:
                    _DEPTH = 1
            else:
                _DEPTH += 1
                entered = True
        if entered and _DEPTH == 1:
            _install_backstops()
    try:
        yield bool(want and entered)
    finally:
        if want and entered:
            with _LOCK:
                _DEPTH = max(0, _DEPTH - 1)
                leaving = _DEPTH == 0
            if leaving:
                _write(_LEAVE)
