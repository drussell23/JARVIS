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

#: Last-resort literals, used when terminfo cannot answer.
#:
#: ANSI `smcup`/`rmcup` as xterm and every terminal that copied it define
#: them. Kept as a FALLBACK rather than the definition — see `_capability`,
#: which asks the terminal's own description first. The cursor-home after
#: enter matters: the alternate buffer retains whatever a previous occupant
#: left in it on some terminals, so a boot that starts drawing at the old
#: cursor position renders halfway down the screen.
_ENTER_FALLBACK = "\x1b[?1049h"
_LEAVE_FALLBACK = "\x1b[?1049l"
_HOME = "\x1b[H"

_LOCK = threading.RLock()
_DEPTH = 0
_ATEXIT_TOKEN: Any = None

# ---------------------------------------------------------------------------
# The async-signal-safe half
# ---------------------------------------------------------------------------
#
# Everything below this comment may run inside a signal handler, and a signal
# handler in CPython is delivered at an ARBITRARY bytecode boundary — inside a
# weakref callback, a ``__del__``, an ``atexit`` handler, a ``finally``. Three
# things are therefore forbidden on this path, each of them measured rather
# than assumed:
#
#   * **Taking a lock.** `_restore_now` acquired `_LOCK`, and a handler that
#     blocks on a lock another thread holds hangs the main thread inside the
#     handler — 2001 ms in the reproduction, and unbounded if that thread is
#     itself waiting on the main one. The restore is idempotent, so it needs
#     no mutual exclusion at all: the worst a race can do is emit `rmcup`
#     twice, and `rmcup` twice is `rmcup`.
#
#   * **Writing through a Python stream.** `TextIOWrapper.write` takes the
#     stream's own internal lock. A signal delivered while the main thread was
#     mid-`print` therefore deadlocks the handler against the very stream it
#     is trying to restore. `os.write` on a file descriptor takes no such lock
#     and is one syscall.
#
#   * **Allocating what can be prepared in advance.** The bytes are encoded at
#     resolution time and the descriptor is captured on entry, so the handler
#     performs one `os.write` and nothing else.
#
#: Cell rather than a plain global: rebinding a module attribute and reading
#: it are each a single bytecode under the GIL, which is all the atomicity an
#: idempotent restore needs — and it lets the signal path avoid `global`,
#: which is how the locked path and this one stay visibly separate.
_ARMED = [False]
_SIGNAL_FD = [-1]
_LEAVE_BYTES = [b"\x1b[?1049l"]
_IN_HANDLER = [False]
_SUSPENDED = [False]
_ENTER_BYTES = [b"\x1b[?1049h\x1b[H"]


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


def _capability(name: str, fallback: str) -> str:
    """One terminal capability, from the terminal's OWN description.

    `smcup`/`rmcup` are not universal facts — they are entries in a terminfo
    record, and a terminal that does not define them does not HAVE an
    alternate screen. Writing xterm's literal at such a terminal prints
    garbage into the operator's session, and hardcoding the literal is what
    made that indistinguishable from success.

    Resolution order is deliberate: terminfo, then the ANSI literal. The
    fallback stays because terminfo is frequently unavailable in exactly the
    environments that DO support the sequence — a stripped container with no
    `TERM`, a CI runner, a pty with `TERM=dumb` set by a wrapper — and
    refusing the alternate screen there would be a regression dressed as
    correctness.

    NEVER raises: `curses` is optional, `setupterm` fails on an unknown TERM,
    and neither is worth a boot.
    """
    try:
        import curses
        try:
            curses.setupterm()
        except Exception:  # noqa: BLE001 — unknown/absent TERM
            return fallback
        value = curses.tigetstr(name)
        if value:
            return value.decode("ascii", "ignore")
        # DEFINED-AS-ABSENT is a real answer, distinct from "could not ask".
        # `tigetstr` returning None/empty for a terminal that HAS a
        # description means this terminal genuinely lacks the capability.
        return ""
    except Exception:  # noqa: BLE001
        return fallback


def _resolve_sequences() -> bool:
    """Fill the pre-encoded enter/leave buffers. True if the terminal has an
    alternate screen at all. NEVER raises.

    Encoded ONCE, here, because the leave sequence is written from a signal
    handler and a handler must not be encoding strings.
    """
    try:
        enter = _capability("smcup", _ENTER_FALLBACK)
        leave = _capability("rmcup", _LEAVE_FALLBACK)
        if not enter or not leave:
            return False
        _ENTER_BYTES[0] = (enter + _HOME).encode("ascii", "ignore")
        _LEAVE_BYTES[0] = leave.encode("ascii", "ignore")
        return True
    except Exception:  # noqa: BLE001
        return True         # keep the literals already in the cells


def _write(seq: str) -> bool:
    """Emit a control sequence and flush immediately. NEVER raises.

    The BLOCKING path — the context manager and the atexit backstop, where
    taking a stream lock is fine. Signal handlers use `_emit_signal_safe`.

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


def _emit_signal_safe(payload: bytes) -> bool:
    """One `os.write` to the captured descriptor. NEVER raises.

    No lock, no encoding, no Python stream — see the contract above this
    module's `_ARMED` cell for why each of those is forbidden here. Partial
    writes are looped rather than ignored: a control sequence delivered
    half-way is worse than one not delivered at all, because the terminal
    then interprets the remainder as text.
    """
    fd = _SIGNAL_FD[0]
    if fd < 0:
        return False
    try:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                return False
            view = view[written:]
        return True
    except (OSError, ValueError):
        return False


def _capture_fd() -> None:
    """Remember the real stdout's descriptor while it is safe to ask.

    Captured at ENTRY, not at restore time: `sys.__stdout__` may be closed or
    replaced by the time a handler runs, and `fileno()` on a closed stream
    raises — inside a signal handler, during shutdown, on the one path whose
    entire job is to leave the terminal usable.
    """
    try:
        stream = _out()
        _SIGNAL_FD[0] = int(stream.fileno()) if stream is not None else -1
    except Exception:  # noqa: BLE001
        _SIGNAL_FD[0] = -1


def _restore_signal_safe() -> bool:
    """Leave the alternate screen from a signal handler. NEVER raises.

    Idempotent by construction, which is what buys the lock-free flag: two
    concurrent callers both emit `rmcup`, and `rmcup` twice is `rmcup`.
    """
    if not _ARMED[0]:
        return False
    _ARMED[0] = False
    return _emit_signal_safe(_LEAVE_BYTES[0])


def _restore_now() -> None:
    """Unconditional return to the normal buffer. Safe to call repeatedly.

    The blocking path. Clears the signal-path flag too so the two cannot
    disagree about whether the buffer is still borrowed.
    """
    global _DEPTH
    with _LOCK:
        if _DEPTH <= 0:
            _ARMED[0] = False
            return
        _DEPTH = 0
    _ARMED[0] = False
    _write(_LEAVE_BYTES[0].decode("ascii", "ignore"))


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

    # The unraisable guard, armed for exactly as long as we are borrowing the
    # screen. THE fix for the traceback an operator sees on Ctrl+C:
    #
    #   Exception ignored in: <function WeakValueDictionary...remove>
    #     File ".../alt_screen.py", line ..., in _handler
    #       _prev(signum, frame)
    #   KeyboardInterrupt:
    #
    # A signal is delivered at an arbitrary bytecode boundary, so `_prev` —
    # `signal.default_int_handler`, whose whole contract is to RAISE — can be
    # called from inside a weakref callback or a `__del__`, where there is no
    # caller to catch anything. The interpreter routes that to
    # `sys.unraisablehook`, which is where it has to be answered; catching it
    # here would swallow a KeyboardInterrupt the operator is entitled to.
    try:
        from backend.core.ouroboros.governance.exit_guard import (
            install_unraisable_guard,
        )
        install_unraisable_guard()
    except Exception:  # noqa: BLE001
        logger.debug("[AltScreen] unraisable guard unavailable", exc_info=True)

    try:
        import signal

        if threading.current_thread() is not threading.main_thread():
            # signal.signal only works on the main thread; the atexit
            # backstop still covers this case.
            return

        def _chain(sig: Any, kind: str) -> None:
            previous = signal.getsignal(sig)
            if getattr(previous, "_ov_alt_screen_chained", False):
                return

            def _handler(signum: int, frame: Any, _prev: Any = previous,
                         _kind: str = kind) -> None:
                # PHASE 1 — the async-signal-safe part, always, first, and
                # unconditionally. One `os.write`. Whatever else goes wrong
                # from here on, the operator gets their terminal back.
                if _kind == "cont":
                    # Resuming from a stop we handed the buffer back for.
                    if _SUSPENDED[0]:
                        _SUSPENDED[0] = False
                        _ARMED[0] = True
                        _emit_signal_safe(_ENTER_BYTES[0])
                else:
                    if _kind == "stop":
                        _SUSPENDED[0] = _ARMED[0]
                    _restore_signal_safe()

                # PHASE 2 — chaining, which is NOT signal-safe and is
                # therefore guarded on re-entry. A second Ctrl+C arriving
                # while a slow predecessor runs (the harness writes a partial
                # summary here) previously re-entered this handler and ran it
                # again; an impatient operator must get the process to leave,
                # not a deeper stack.
                if _IN_HANDLER[0]:
                    try:
                        signal.signal(signum, signal.SIG_DFL)
                        os.kill(os.getpid(), signum)
                    except Exception:  # noqa: BLE001
                        os._exit(128 + int(signum))
                    return
                _IN_HANDLER[0] = True
                try:
                    if callable(_prev):
                        _prev(signum, frame)
                    elif _prev == signal.SIG_DFL:
                        # Re-raise with the default disposition so the exit
                        # status still reports the signal honestly — and so a
                        # stop signal actually stops the process.
                        signal.signal(signum, signal.SIG_DFL)
                        os.kill(os.getpid(), signum)
                    # SIG_IGN falls through: the predecessor asked for this
                    # signal to do nothing, and restoring the screen is not a
                    # reason to overrule them.
                finally:
                    _IN_HANDLER[0] = False

            _handler._ov_alt_screen_chained = True  # type: ignore[attr-defined]
            signal.signal(sig, _handler)

        for name, kind in (
            ("SIGINT", "exit"), ("SIGTERM", "exit"), ("SIGHUP", "exit"),
            # SIGTSTP/SIGCONT are NOT speculative here. prompt_toolkit wraps
            # its own Ctrl+Z in `run_in_terminal`, so it hands the buffer back
            # while IT owns the screen — but this module claims the buffer for
            # the whole BOOT, before the cockpit mounts and after it exits, and
            # nothing owns the screen in that window. A `kill -TSTP` there, or
            # a Ctrl+Z during the crest, left the shell drawing its prompt on
            # the alternate buffer.
            ("SIGTSTP", "stop"), ("SIGCONT", "cont"),
        ):
            sig = getattr(signal, name, None)
            if sig is None:
                continue        # Windows has neither TSTP nor CONT
            try:
                _chain(sig, kind)
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
                # Resolve, capture and arm BEFORE the first byte goes out.
                # Ordering is the whole safety argument: if the process dies
                # between the write and the arm, nothing knows to restore.
                # Arming first costs at most one redundant `rmcup` at a
                # terminal we never entered, which is a no-op.
                if not _resolve_sequences():
                    # The terminal has a description and it says there is no
                    # alternate screen. Writing xterm's literal anyway would
                    # print it as text into the operator's session.
                    logger.debug("[AltScreen] terminal has no smcup/rmcup")
                    want = False
                else:
                    _capture_fd()
                    _ARMED[0] = True
                    entered = _write(
                        _ENTER_BYTES[0].decode("ascii", "ignore"),
                    )
                    if entered:
                        _DEPTH = 1
                    else:
                        _ARMED[0] = False
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
                _ARMED[0] = False
                _write(_LEAVE_BYTES[0].decode("ascii", "ignore"))
