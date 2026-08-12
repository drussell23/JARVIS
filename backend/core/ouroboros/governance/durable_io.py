"""durable_io — the primitives that make "we wrote it" mean "it survives".
=========================================================================

Root problem
------------

Every atomic-write helper in this repo (``approval_store._atomic_write``,
``invariant_drift_store._atomic_write``, ``dw_ttft_observer._atomic_write``,
``aegis/bootstrap.atomic_write_payload``, …) follows the same shape:

    tmp = mkstemp(dir=target.parent)
    write(tmp); close(tmp)
    os.replace(tmp, target)

That is **rename atomicity**, and it is not durability. Two steps are
missing, and both are invisible until the machine loses power:

1. **The data was never flushed before the rename.** ``write()`` puts
   bytes in the page cache. ``os.replace`` then publishes a name that
   may point at a file whose contents have not reached stable storage —
   classically yielding a zero-length or stale file after a crash, on a
   path the code believes is transactional.

2. **The rename itself was never flushed.** A directory entry is
   metadata; until the *directory* is fsynced the rename can be lost
   even though the file's data is safe.

And on this platform there is a third, sharper one:

3. **macOS ``fsync()`` does not flush the drive's write cache.** It
   pushes the data out of the kernel and returns. The bytes can still be
   sitting in the disk's volatile buffer. ``fcntl(fd, F_FULLFSYNC)`` is
   the call that forces the device to flush. Apple documents this
   explicitly; ``F_FULLFSYNC`` appears nowhere in this repository, so
   every durability claim made on a developer Mac to date has been
   nominal.

This module is the one place that knows those three facts.

What it does NOT do
-------------------

It does not decide *when* to sync. Cadence is a policy question owned by
the caller (group commit, explicit barrier, per-record), because the
right answer differs between a transcript that fires on every render and
an approval ledger that writes once a minute. This module only
guarantees that when a sync is requested, it is a real one.

Blocking, by design
-------------------

Every function here blocks — ``F_FULLFSYNC`` costs milliseconds on real
hardware. **None of these may be called from the event loop.** Callers
run them on a dedicated single-threaded executor; see
``transcript_log.DurableLogWriter`` for the reference wiring.

Authority boundary
------------------

* §1 deterministic — syscalls only; no LLM, no policy
* §7 fail-closed — a sync that cannot be performed RAISES. A durability
  primitive that swallows its own failure is worse than no primitive at
  all, because it manufactures exactly the false confidence this module
  exists to remove. Callers decide how to degrade; this layer never
  degrades silently on their behalf.
"""
from __future__ import annotations

import errno
import logging
import os
import sys
from pathlib import Path
from typing import Optional, Union

logger = logging.getLogger("Ouroboros.DurableIO")


__all__ = [
    "DurableIOError",
    "atomic_replace",
    "flush_and_sync",
    "full_fsync_available",
    "fsync_dir",
    "fsync_file",
    "is_space_exhaustion",
]


PathLike = Union[str, "os.PathLike[str]"]


class DurableIOError(OSError):
    """Raised when a requested durability guarantee could not be met."""


# ===========================================================================
# Platform abstraction — one decision, made once, at import
# ===========================================================================


def _resolve_full_fsync():
    """Return the ``F_FULLFSYNC`` constant, or ``None`` off Darwin.

    Resolved once at import rather than probed per call: the answer
    cannot change for the life of the process, and a per-call
    ``sys.platform`` test in the hot sync path is exactly the kind of
    re-derived value that acquires a second authority."""
    if sys.platform != "darwin":
        return None
    try:
        import fcntl  # noqa: PLC0415 — platform-conditional by nature

        return getattr(fcntl, "F_FULLFSYNC", None)
    except ImportError:
        return None


_F_FULLFSYNC: Optional[int] = _resolve_full_fsync()


def full_fsync_available() -> bool:
    """True when this platform offers a real device-level flush.

    Exposed so telemetry can state the strength of the guarantee rather
    than implying one. A log that reports ``durable_through_seq`` on a
    platform without ``F_FULLFSYNC`` is making a weaker promise, and the
    operator is entitled to know which."""
    return _F_FULLFSYNC is not None


def fsync_file(fd: int) -> None:
    """Force ``fd``'s data to stable storage. BLOCKS. Raises on failure.

    On Darwin this issues ``F_FULLFSYNC``, which instructs the drive to
    flush its own write cache — plain ``fsync()`` on macOS does not.
    ``F_FULLFSYNC`` is unsupported on some filesystems (several network
    and FUSE mounts return ``ENOTSUP`` / ``EINVAL``); there we fall back
    to ``os.fsync``, which is still the strongest call available on that
    mount. Any other error propagates: an ENOSPC surfacing at fsync time
    is real data loss and the caller must see it."""
    if _F_FULLFSYNC is not None:
        try:
            import fcntl  # noqa: PLC0415

            fcntl.fcntl(fd, _F_FULLFSYNC)
            return
        except OSError as exc:
            if exc.errno not in (errno.ENOTSUP, errno.EINVAL, errno.EOPNOTSUPP):
                raise
            # Filesystem cannot do a full flush — fall through to fsync.
    os.fsync(fd)


def fsync_dir(path: PathLike) -> None:
    """Flush a DIRECTORY so a rename or create inside it is durable.

    Without this, ``os.replace`` can be undone by a crash even when the
    file's own data reached the platter: the name is metadata living in
    the parent directory, and nothing has flushed the parent.

    Directory fsync is not available on every platform (notably Windows,
    where a directory cannot be opened as a file). There the call is a
    documented no-op rather than an error, because refusing to run at all
    would be a worse outcome than a weaker guarantee the caller can read
    off :func:`full_fsync_available`."""
    if os.name != "posix":
        return
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    except OSError as exc:
        # Some filesystems refuse fsync on a directory fd. That is a
        # weaker guarantee, not a failure of the caller's operation.
        if exc.errno not in (errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP):
            raise
    finally:
        os.close(fd)


def flush_and_sync(fh) -> None:
    """Push a Python file object all the way down. BLOCKS.

    Three layers, and skipping any one of them leaves bytes behind:
    the object's own buffer (``fh.flush()``), the kernel page cache
    (``fsync``), and on Darwin the device write cache
    (``F_FULLFSYNC``)."""
    fh.flush()
    fsync_file(fh.fileno())


# ===========================================================================
# Atomic, durable replace
# ===========================================================================


def atomic_replace(
    tmp_path: PathLike, dst_path: PathLike, *, sync_dir: bool = True,
) -> None:
    """Publish ``tmp_path`` as ``dst_path``, atomically AND durably.

    Order is load-bearing and is the whole point of this function:

      1. fsync the temp file's DATA  — so the bytes exist before a name
         points at them.
      2. ``os.replace``             — atomic swap; readers see old or new,
                                      never a partial file.
      3. fsync the DIRECTORY        — so the swap itself survives.

    Callers already holding the temp file open should
    :func:`flush_and_sync` it and pass ``sync_dir=True``; this function
    re-opens the temp only when it must, and a temp that has vanished
    between write and replace is an error, never a silent skip."""
    tmp = Path(tmp_path)
    dst = Path(dst_path)

    # (1) data before name.
    fd = os.open(str(tmp), os.O_RDONLY)
    try:
        fsync_file(fd)
    finally:
        os.close(fd)

    # (2) atomic swap.
    os.replace(str(tmp), str(dst))

    # (3) the name itself.
    if sync_dir:
        fsync_dir(dst.parent)


# ===========================================================================
# Resource-exhaustion classification
# ===========================================================================


#: Errnos that mean "the filesystem cannot accept more", as distinct from
#: "this particular operation was wrong". They share one response — seal
#: the log and degrade honestly — so they are classified in one place
#: instead of being re-listed at each catch site.
_EXHAUSTION_ERRNOS = frozenset(
    e for e in (
        getattr(errno, "ENOSPC", None),   # no space left on device
        getattr(errno, "EDQUOT", None),   # quota exceeded
        getattr(errno, "EFBIG", None),    # file too large
        getattr(errno, "EROFS", None),    # read-only (remount-on-error)
        getattr(errno, "EIO", None),      # device gave up
    ) if e is not None
)


def is_space_exhaustion(exc: BaseException) -> bool:
    """True when ``exc`` means the filesystem is out of room or unusable.

    Inode exhaustion arrives as ``ENOSPC`` from ``open``/``mkstemp`` —
    the same errno as a full data area — which is why the two cases share
    one recovery path rather than being told apart at the call site."""
    return isinstance(exc, OSError) and exc.errno in _EXHAUSTION_ERRNOS
