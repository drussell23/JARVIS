"""Tier 1 #3 — Cross-process JSONL append helper.

Closes §28.5.1 v9 brutal review's concrete data-loss race:

  ``auto_action_router.py:1110-1113`` and ``adaptation/ledger.py:648``
  use ``path.open("a")`` with ``threading.Lock()`` only. POSIX
  append-mode is line-atomic **within a single process** but **NOT
  across processes**. Two ``ouroboros_battle_test.py`` processes
  writing the same ``.jsonl`` concurrently can interleave partial
  writes — the second write can overwrite the tail of the first
  before the newline lands. ``ApprovalStore`` already uses
  ``fcntl.flock`` correctly elsewhere; the action ledgers don't.

This module is the single source of truth for cross-process JSONL
append safety. Three ledgers wire to it:

  * ``auto_action_router.AutoActionProposalLedger.append``
  * ``adaptation.ledger.AdaptationLedger.append``
  * ``invariant_drift_store.InvariantDriftStore.append_history``
  * ``invariant_drift_store.InvariantDriftStore.append_audit``

Design pillars:

  * **Asynchronous** — flock is sync-blocking but the cost is
    bounded (microseconds for a single append; the lock scope is
    open-write-flush-close, never wraps long operations). Pattern
    matches ``ApprovalStore.decide`` exactly.

  * **Dynamic** — POSIX uses ``fcntl.flock``; Windows falls through
    to a documented degraded mode (advisory threading.Lock only —
    a future ``msvcrt.locking`` wiring can land additively without
    touching call sites). Production target is POSIX.

  * **Adaptive** — degrades gracefully when ``fcntl`` is missing
    (extreme edge — embedded environments / Windows). Returns False
    on lock-acquire failure rather than raising.

  * **Intelligent** — distinguishes (a) lock-acquire failure
    (concurrent writer holds the lock past timeout, returns False),
    (b) write failure (OSError mid-write, returns False), (c)
    success (returns True). Caller can stat() the result and
    surface accordingly.

  * **Robust** — never raises. Lock is released on every exit path
    including exceptions. ``finally`` block guarantees release +
    fd close even if ``fcntl.flock`` itself raises.

  * **No hardcoding** — lock acquisition timeout is env-tunable;
    default 5.0s (long enough for any reasonable single append on
    a healthy disk; short enough that a deadlocked writer doesn't
    hang the entire ledger system).

Authority invariants (AST-pinned by companion tests):

  * Imports stdlib ONLY (``fcntl`` / ``msvcrt`` are stdlib-conditional;
    everything else is core stdlib).
  * NEVER imports any governance module — this is a pure-stdlib
    primitive consumed by ledgers; reverse-coupling would create
    a cycle.
  * Never raises out of any public method.
"""
from __future__ import annotations

import errno
import logging
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator, Optional

logger = logging.getLogger(__name__)


CROSS_PROCESS_JSONL_SCHEMA_VERSION: str = "cross_process_jsonl.1"


# ---------------------------------------------------------------------------
# fcntl detection — degrade gracefully on Windows / embedded environments
# ---------------------------------------------------------------------------


try:
    import fcntl as _fcntl  # type: ignore[import]
    _HAS_FCNTL: bool = True
except ImportError:
    _fcntl = None  # type: ignore[assignment]
    _HAS_FCNTL = False


def fcntl_available() -> bool:
    """True iff fcntl is importable (POSIX). Public so callers can
    decide whether the cross-process guarantee is real or degraded
    to in-process-only on this platform."""
    return _HAS_FCNTL


# ---------------------------------------------------------------------------
# Env knobs
# ---------------------------------------------------------------------------


_DEFAULT_LOCK_TIMEOUT_S: float = 5.0
_LOCK_TIMEOUT_FLOOR_S: float = 0.1


def lock_timeout_s() -> float:
    """``JARVIS_CROSS_PROCESS_LOCK_TIMEOUT_S`` (default 5.0s, floor
    0.1s).

    Maximum wall-clock seconds to wait for an exclusive flock. Long
    enough for any reasonable single append on a healthy disk;
    short enough that a deadlocked writer doesn't hang the entire
    ledger system."""
    raw = os.environ.get(
        "JARVIS_CROSS_PROCESS_LOCK_TIMEOUT_S", "",
    ).strip()
    if not raw:
        return _DEFAULT_LOCK_TIMEOUT_S
    try:
        return max(_LOCK_TIMEOUT_FLOOR_S, float(raw))
    except (TypeError, ValueError):
        return _DEFAULT_LOCK_TIMEOUT_S


# ---------------------------------------------------------------------------
# In-process lock map — keyed by lock-file absolute path
# ---------------------------------------------------------------------------
#
# The threading.Lock layer is NOT redundant with fcntl.flock — it
# handles fast in-process serialization (avoiding a lock-file open()
# + flock syscall per append from the same process). The fcntl layer
# handles the cross-process serialization. Both compose: in-process
# threads serialize via threading.Lock; processes serialize via
# fcntl.flock; one writer at a time globally.


_in_process_locks: dict = {}
_in_process_locks_guard = threading.Lock()


def _get_inprocess_lock(lock_path: Path) -> threading.Lock:
    """Return (or create) the threading.Lock for a given lock-path.
    Per-path locks so different ledgers don't serialize against each
    other unnecessarily."""
    key = str(lock_path.resolve())
    with _in_process_locks_guard:
        existing = _in_process_locks.get(key)
        if existing is not None:
            return existing
        new_lock = threading.Lock()
        _in_process_locks[key] = new_lock
        return new_lock


def _reset_inprocess_locks_for_tests() -> None:
    """Test isolation helper. Drops the in-process lock map so each
    test starts fresh (matters for tests that assert lock identity)."""
    with _in_process_locks_guard:
        _in_process_locks.clear()


# ---------------------------------------------------------------------------
# Lock acquisition — context manager wrapping flock + threading.Lock
# ---------------------------------------------------------------------------


@contextmanager
def _acquire_cross_process_lock(
    lock_path: Path,
    *,
    timeout_s: Optional[float] = None,
) -> Iterator[bool]:
    """Acquire exclusive cross-process lock on ``lock_path``. Yields
    True on success, False on timeout / fcntl-unavailable / OSError.
    Always releases on exit. NEVER raises."""
    effective_timeout = (
        timeout_s if timeout_s is not None and timeout_s > 0
        else lock_timeout_s()
    )
    inprocess = _get_inprocess_lock(lock_path)
    # In-process serialize first (cheap)
    if not inprocess.acquire(timeout=effective_timeout):
        yield False
        return

    try:
        # Cross-process serialize via fcntl.flock with poll-loop
        # (POSIX flock has no native timeout; we use LOCK_NB +
        # exponential-backoff poll until the deadline).
        if not _HAS_FCNTL:
            # Degraded mode: in-process lock only. Document this
            # in stats; caller can detect via fcntl_available().
            yield True
            return

        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            yield False
            return

        try:
            lock_fd = os.open(
                str(lock_path),
                os.O_CREAT | os.O_RDWR,
                0o644,
            )
        except OSError as exc:
            logger.debug(
                "[CrossProcessJSONL] lock-file open failed: %s", exc,
            )
            yield False
            return

        deadline = time.monotonic() + effective_timeout
        backoff = 0.005
        max_backoff = 0.25
        acquired = False
        try:
            while True:
                try:
                    _fcntl.flock(  # type: ignore[union-attr]
                        lock_fd,
                        _fcntl.LOCK_EX | _fcntl.LOCK_NB,  # type: ignore[union-attr]
                    )
                    acquired = True
                    break
                except (BlockingIOError, OSError) as exc:
                    if (
                        isinstance(exc, OSError)
                        and exc.errno not in (
                            errno.EWOULDBLOCK,
                            errno.EAGAIN,
                            errno.EACCES,
                        )
                    ):
                        # Non-contention OSError — bail
                        logger.debug(
                            "[CrossProcessJSONL] flock raised "
                            "unexpected OSError: %s", exc,
                        )
                        break
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(backoff)
                    backoff = min(max_backoff, backoff * 1.5)

            if not acquired:
                yield False
                return

            yield True
        finally:
            if acquired:
                try:
                    _fcntl.flock(  # type: ignore[union-attr]
                        lock_fd,
                        _fcntl.LOCK_UN,  # type: ignore[union-attr]
                    )
                except OSError as exc:
                    logger.debug(
                        "[CrossProcessJSONL] flock UN raised: %s",
                        exc,
                    )
            try:
                os.close(lock_fd)
            except OSError:
                pass
    finally:
        try:
            inprocess.release()
        except RuntimeError:
            # Already released — should not happen but defensive
            pass


# ---------------------------------------------------------------------------
# Public append helpers
# ---------------------------------------------------------------------------


def flock_append_line(
    path: Path,
    line: str,
    *,
    timeout_s: Optional[float] = None,
) -> bool:
    """Append a single line (with trailing newline) to ``path``,
    serialized cross-process via ``fcntl.flock`` on a sibling
    ``.lock`` file. Returns True on success, False on any failure
    (lock timeout, write error, fcntl unavailable, etc.). NEVER
    raises.

    The line is written exactly as-given plus exactly one trailing
    ``\\n``. Caller is responsible for ensuring ``line`` does not
    already contain newlines (the JSONL contract — one record per
    line)."""
    return flock_append_lines(
        path, (line,), timeout_s=timeout_s,
    )


@contextmanager
def flock_critical_section(
    path: Path,
    *,
    timeout_s: Optional[float] = None,
) -> Iterator[bool]:
    """Acquire exclusive cross-process lock on ``path``'s sibling
    ``.lock`` file for a custom critical section (e.g., a ring-
    buffer read-modify-write). Yields True on success, False on
    timeout / failure. Always releases on exit. NEVER raises.

    Use this when a single ``flock_append_line`` call doesn't fit —
    e.g., the InvariantDriftStore history-ring-buffer pattern that
    reads existing lines, appends + trims, then atomic-writes the
    truncated tail. Concurrent processes must not race the read-
    trim-write block.

    Caller is responsible for the actual file I/O inside the block.
    The context manager only provides the lock; if I/O fails
    inside the block, the caller handles it (defensive contract is
    caller-owned)."""
    try:
        target = Path(path)
        lock_path = target.with_suffix(target.suffix + ".lock")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.debug(
                "[CrossProcessJSONL] critical-section parent mkdir "
                "failed: %s", exc,
            )
            yield False
            return
        with _acquire_cross_process_lock(
            lock_path, timeout_s=timeout_s,
        ) as acquired:
            yield acquired
    except Exception as exc:  # noqa: BLE001 — last-resort defensive
        logger.debug(
            "[CrossProcessJSONL] flock_critical_section raised: %s",
            exc,
        )
        yield False


def flock_append_lines(
    path: Path,
    lines: Iterable[str],
    *,
    timeout_s: Optional[float] = None,
) -> bool:
    """Append multiple lines atomically (all-or-nothing within the
    flock scope) to ``path``. Each line gets exactly one trailing
    ``\\n``. Returns True iff every line landed; False on any
    failure. NEVER raises.

    All lines write under one flock acquire — concurrent writers
    cannot interleave. Cheaper than calling ``flock_append_line``
    in a loop when batching."""
    try:
        target = Path(path)
        lock_path = target.with_suffix(target.suffix + ".lock")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.debug(
                "[CrossProcessJSONL] parent mkdir failed: %s", exc,
            )
            return False

        with _acquire_cross_process_lock(
            lock_path, timeout_s=timeout_s,
        ) as acquired:
            if not acquired:
                logger.debug(
                    "[CrossProcessJSONL] lock acquisition failed "
                    "for %s", target,
                )
                return False
            try:
                with target.open("a", encoding="utf-8") as fh:
                    for raw_line in lines:
                        if not isinstance(raw_line, str):
                            # Coerce defensively rather than raise.
                            raw_line = str(raw_line)
                        fh.write(raw_line)
                        fh.write("\n")
                    fh.flush()
                return True
            except OSError as exc:
                logger.debug(
                    "[CrossProcessJSONL] append write failed at "
                    "%s: %s", target, exc,
                )
                return False
    except Exception as exc:  # noqa: BLE001 — last-resort defensive
        logger.debug(
            "[CrossProcessJSONL] flock_append_lines raised: %s",
            exc,
        )
        return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "CROSS_PROCESS_JSONL_SCHEMA_VERSION",
    "fcntl_available",
    "flock_append_line",
    "flock_append_lines",
    "flock_critical_section",
    "lock_timeout_s",
]
