"""
portable_fcntl - the one place JARVIS asks the OS for an advisory file lock.

Drop-in for the slice of ``fcntl`` the codebase uses (``flock`` + ``LOCK_*``,
and ``fcntl.fcntl`` for ``F_GETFD``/``F_SETFD``/``FD_CLOEXEC``)::

    from backend.core import portable_fcntl as fcntl

POSIX (macOS, Linux, WSL): the real ``fcntl`` module's objects are re-exported
unchanged, so behaviour there is byte-identical to ``import fcntl``.

Windows: ``flock`` is built on ``msvcrt.locking``:

* The lock covers one byte at ``JARVIS_PORTABLE_FLOCK_OFFSET`` (default 2**30),
  far past any real content. Windows byte-range locks are MANDATORY, so a lock
  on byte 0 would stop other processes reading the metadata several lock files
  carry; a range past EOF is legal and blocks nothing but other lockers.
* Contention on a ``LOCK_NB`` request raises ``BlockingIOError`` (errno
  EAGAIN), which is what every ``except (BlockingIOError, OSError)`` around a
  POSIX ``flock`` already expects.
* A blocking request polls ``LK_NBLCK`` (``JARVIS_PORTABLE_FLOCK_POLL_S``)
  rather than using ``LK_LOCK``, which gives up after ~10 s and would turn a
  wait into a spurious failure.
* ``LOCK_SH`` is taken exclusively: ``msvcrt`` has no shared mode. Readers
  serialise; nothing that was safe becomes unsafe.
* As with ``flock``, the kernel drops the lock when the handle closes or the
  process dies, so a crash cannot strand it.

``fcntl.fcntl`` on Windows answers the close-on-exec questions through
``os.get_inheritable`` / ``os.set_inheritable`` and raises for anything else,
rather than pretending.
"""
from __future__ import annotations

import os
import sys

if sys.platform != "win32":
    from fcntl import (  # noqa: F401  (re-exported)
        FD_CLOEXEC,
        F_GETFD,
        F_GETFL,
        F_SETFD,
        F_SETFL,
        LOCK_EX,
        LOCK_NB,
        LOCK_SH,
        LOCK_UN,
        fcntl,
        flock,
        lockf,
    )

    IS_EMULATED = False

else:
    import errno as _errno
    import msvcrt as _msvcrt
    import time as _time
    from typing import Any as _Any

    IS_EMULATED = True

    # Values match Linux so code that logs or compares them reads the same.
    LOCK_SH = 1
    LOCK_EX = 2
    LOCK_NB = 4
    LOCK_UN = 8

    F_GETFD = 1
    F_SETFD = 2
    F_GETFL = 3
    F_SETFL = 4
    FD_CLOEXEC = 1

    def _env_int(name: str, default: int) -> int:
        try:
            return int(os.environ.get(name, "") or default)
        except ValueError:
            return default

    def _env_float(name: str, default: float) -> float:
        try:
            return float(os.environ.get(name, "") or default)
        except ValueError:
            return default

    _LOCK_OFFSET = _env_int("JARVIS_PORTABLE_FLOCK_OFFSET", 1 << 30)
    _POLL_S = _env_float("JARVIS_PORTABLE_FLOCK_POLL_S", 0.05)

    def _fileno(fd: _Any) -> int:
        return fd if isinstance(fd, int) else fd.fileno()

    def _locking(fd: int, mode: int) -> None:
        """Apply ``mode`` to the lock byte, leaving the file position as found."""
        pos = os.lseek(fd, 0, os.SEEK_CUR)
        try:
            os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
            _msvcrt.locking(fd, mode, 1)
        finally:
            os.lseek(fd, pos, os.SEEK_SET)

    def flock(fd: _Any, operation: int) -> None:
        fno = _fileno(fd)
        if operation & LOCK_UN:
            try:
                _locking(fno, _msvcrt.LK_UNLCK)
            except OSError:
                pass  # POSIX unlock of an unheld lock is a no-op too
            return
        if not operation & (LOCK_EX | LOCK_SH):
            raise ValueError(f"unsupported flock operation: {operation!r}")
        while True:
            try:
                _locking(fno, _msvcrt.LK_NBLCK)
                return
            except OSError as exc:
                if exc.errno not in (_errno.EACCES, _errno.EDEADLK, _errno.EAGAIN):
                    raise
                if operation & LOCK_NB:
                    raise BlockingIOError(
                        _errno.EAGAIN, "Resource temporarily unavailable"
                    ) from exc
            _time.sleep(_POLL_S)

    def lockf(fd: _Any, cmd: int, length: int = 0, start: int = 0, whence: int = 0) -> None:
        flock(fd, cmd)

    def fcntl(fd: _Any, cmd: int, arg: int = 0) -> int:
        fno = _fileno(fd)
        if cmd == F_GETFD:
            return 0 if os.get_inheritable(fno) else FD_CLOEXEC
        if cmd == F_SETFD:
            os.set_inheritable(fno, not arg & FD_CLOEXEC)
            return 0
        if cmd == F_GETFL:
            return 0
        raise OSError(_errno.EINVAL, f"fcntl cmd {cmd!r} is not available on Windows")


__all__ = [
    "FD_CLOEXEC", "F_GETFD", "F_GETFL", "F_SETFD", "F_SETFL",
    "IS_EMULATED", "LOCK_EX", "LOCK_NB", "LOCK_SH", "LOCK_UN",
    "fcntl", "flock", "lockf",
]
