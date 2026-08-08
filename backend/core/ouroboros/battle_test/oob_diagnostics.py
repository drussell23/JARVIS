"""Get a stack trace out of a process that is too wedged to give you one.

A UI deadlock is the one bug you cannot investigate after the fact. Ctrl+C
does nothing, `kill -9` destroys the stack frames, and what reaches the log is
the absence of anything. The `/` freeze this was built for was never
reproduced precisely because there was no way to ask a frozen `ov` what it was
doing.

    kill -USR1 <ov pid>

writes every thread's stack to `.jarvis/logs/ov-crash.log` and the process
keeps running.

Why faulthandler and not a signal handler
-----------------------------------------
Two obvious approaches both fail in exactly the situation that matters:

* ``signal.signal(SIGUSR1, dump)`` — a Python-level handler runs only BETWEEN
  bytecode instructions. A main thread blocked inside a C call (acquiring a
  lock, in ``read()``, waiting on a futex) executes no bytecodes, so the
  handler is deferred until the thread moves. It never fires under the
  deadlock it was installed to diagnose.

* ``loop.add_signal_handler(...)`` — worse. It schedules a callback on the
  event loop, so it requires the loop to be processing callbacks. A wedged
  loop is the thing being investigated.

``faulthandler.register`` installs a handler that writes from the C signal
handler itself, walking interpreter state directly rather than executing
Python. It fires while the GIL is held by a blocked thread, which is the only
property that makes it useful here.

The cost of that property is the constraint below: the destination must be a
real file descriptor, because the C handler cannot call back into Python to
resolve a file object.
"""
from __future__ import annotations

import logging
import os
import signal
import sys
from pathlib import Path
from typing import Any, Optional, TextIO

logger = logging.getLogger("Ouroboros.OOB")

__all__ = [
    "OOB_SIGNAL",
    "dump_all_threads",
    "install_oob_stack_dump",
    "oob_hint",
]

#: SIGUSR1 — not caught by anything else here, ignored by default, and safe to
#: send to a healthy process. SIGQUIT would also dump but kills the process,
#: which defeats the purpose: the point is to inspect a live wedge, repeatedly,
#: and watch whether the stacks are actually moving.
OOB_SIGNAL = getattr(signal, "SIGUSR1", None)

#: Held open for the process lifetime. faulthandler writes from a C signal
#: handler, so it cannot open a file, resolve a path, or take a lock at dump
#: time — the descriptor must already exist and stay valid.
_LOG_HANDLE: Optional[TextIO] = None


def _log_path() -> Path:
    """The same crash log the mount breaker writes to (DRY).

    One file for "the cockpit could not start" and "the cockpit stopped
    responding" — an operator debugging a wedge should not have to know which
    of two files to look in, and the two failures are often the same story.
    """
    try:
        from backend.core.ouroboros.battle_test.mount_breaker import (
            crash_log_path,
        )
        return crash_log_path()
    except Exception:  # noqa: BLE001
        return Path(".jarvis/logs/ov-crash.log")


def dump_all_threads(file: Any = None) -> bool:
    """Dump every thread's stack right now. Returns True if anything wrote.

    The in-band path — used by tests and by any code that wants a snapshot
    without a signal. The signal path below shares the same formatter, so what
    an operator reads is identical either way.
    """
    try:
        import faulthandler
        target = file if file is not None else sys.stderr
        faulthandler.dump_traceback(file=target, all_threads=True)
        try:
            target.flush()
        except Exception:  # noqa: BLE001
            pass
        return True
    except Exception:  # noqa: BLE001
        logger.debug("[OOB] dump_traceback failed", exc_info=True)
        return False


def crash_log_handle(path: Optional[Path] = None) -> Any:
    """The append handle every stack dump in this process writes to.

    ONE descriptor, opened once and held for the process lifetime, shared by
    every producer: the SIGUSR1 trap, and the loop watchdog's C-level timer.
    That sharing is not tidiness — ``faulthandler`` writes from contexts that
    cannot open a file, resolve a path, or take a lock (a C signal handler, and
    a watchdog thread firing while the GIL is held elsewhere), so the
    descriptor MUST already exist and stay valid. A second producer opening its
    own would interleave two independent buffers into one file.

    Returns None if the log cannot be opened; callers degrade to stderr.
    """
    global _LOG_HANDLE
    if _LOG_HANDLE is not None:
        return _LOG_HANDLE
    try:
        target = path if path is not None else _log_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        # Line-buffered append. A wedged process is usually killed eventually,
        # and a buffered dump dies with it.
        _LOG_HANDLE = target.open("a", buffering=1, encoding="utf-8")
        _LOG_HANDLE.write(
            f"\n{'=' * 72}\n"
            f"[oob] stack-dump log opened — pid {os.getpid()}\n"
            f"{'=' * 72}\n"
        )
        _LOG_HANDLE.flush()
        return _LOG_HANDLE
    except Exception:  # noqa: BLE001
        logger.debug("[OOB] could not open the crash log", exc_info=True)
        return None


def install_oob_stack_dump(path: Optional[Path] = None) -> bool:
    """Arm ``kill -USR1``. Returns True if the trap is live. NEVER raises.

    Idempotent, and a no-op on platforms without SIGUSR1 (Windows), where the
    absence of the signal is not an error — just an unavailable facility.
    """
    if OOB_SIGNAL is None:
        return False
    try:
        import faulthandler

        target = path if path is not None else _log_path()
        handle = crash_log_handle(path)
        if handle is None:
            return False

        # chain=False, and this is load-bearing.
        #
        # `chain=True` calls the PREVIOUS handler after dumping — and the
        # previous handler for SIGUSR1 is SIG_DFL, whose default disposition
        # is to TERMINATE. The first version of this dumped a perfect trace
        # and then killed the process it was diagnosing (exit 158 = 128+30),
        # which is indistinguishable from the `kill -9` this exists to avoid.
        #
        # The generic argument for chaining — do not displace someone else's
        # handler — does not apply: nothing else handles SIGUSR1 here, and
        # what would be preserved is a fatal default.
        faulthandler.register(
            OOB_SIGNAL, file=handle, all_threads=True, chain=False,
        )
        logger.info(
            "[OOB] stack-dump trap armed on SIGUSR1 (pid=%d) -> %s",
            os.getpid(), target,
        )
        return True
    except Exception:  # noqa: BLE001 — a missing debugger must not stop a boot
        logger.debug("[OOB] could not arm the stack-dump trap", exc_info=True)
        return False


def oob_hint() -> str:
    """The line to show an operator so they can use this when it matters.

    Printed at boot rather than discovered in a docstring: the moment someone
    needs this, the UI is frozen and they cannot read anything the UI would
    have told them later.
    """
    if OOB_SIGNAL is None:
        return ""
    return f"frozen? kill -USR1 {os.getpid()} → stacks land in {_log_path()}"
