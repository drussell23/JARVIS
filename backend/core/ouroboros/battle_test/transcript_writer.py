"""transcript_writer — the append path that seals rather than limps.

Step 5 of the transcript durability arc. Built to satisfy the oracles in
:mod:`transcript_kill_harness`, never the other way round: every rule
below exists because one of them fails without it.

Seal, don't limp
----------------

A write failure — ENOSPC, EDQUOT, EIO, a quota that moved under us —
leaves a **partial frame** on disk. There are two possible responses and
only one of them is safe:

*Limp*: note the error and keep appending. The file becomes
``[valid…][torn][valid…]``. A forward scan cannot pass a torn frame
without inventing an order that never happened, so everything after the
tear is **orphaned** — silently, and permanently. The log looks healthy,
is the right size, and has lost its tail.

*Seal*: ``ftruncate`` back to the last verified good offset — which
needs no free space, and is therefore still possible in exactly the
situation that caused the failure — then refuse every subsequent append.
What remains on disk is a valid prefix. Nothing is lost that was ever
acknowledged, and nothing after the failure pretends to be there.

This module seals. ``tests/battle_test/test_transcript_log_oracles.py::
test_oracle_a_appending_past_a_torn_tail_orphans_and_is_detected`` pins
the shape that limping produces, so the rule cannot be quietly relaxed.

Three health states, and they are telemetry, not decoration
-----------------------------------------------------------

``DURABLE``   appends land and barriers succeed.
``DEGRADED``  appends still land, but the durability promise is weaker
              than advertised — a sync failed, or free space fell under
              the reserve. ``durable_through_seq`` stops advancing and
              the gap is visible.
``SEALED``    terminal. No further append will be attempted by this
              writer, for the life of this writer.

The distinction that matters: DEGRADED is *not* a quiet fallback. It is
the state in which the writer keeps working while publicly no longer
claiming durability. A silent downgrade to memory-only is the failure
mode this whole arc exists to remove.

Ordering: sequence comes from the caller
----------------------------------------

``seq`` is assigned by :class:`TranscriptSpine` under its own lock, and
:meth:`DurableLogWriter.submit` must be called from inside that lock.
That is what makes queue order equal sequence order. Were the writer to
mint its own ``seq``, there would be two authorities for the same value
and the downstream one — the file — would disagree with the spine under
concurrency, which ``recover_log`` correctly reports as
``NON_MONOTONIC_SEQ`` and treats as the end of the trustworthy prefix.

Threading
---------

Every syscall happens on ONE dedicated worker thread. ``submit`` is a
queue put — O(1), non-blocking, safe to call from the event loop or any
other thread. Nothing in the async path ever waits on a disk.
"""
from __future__ import annotations

import enum
import errno
import logging
import os
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional

from backend.core.ouroboros.battle_test.transcript_log import (
    RecoveryResult,
    encode_record,
    recover_log,
)
from backend.core.ouroboros.governance.durable_io import (
    atomic_replace,
    fsync_dir,
    fsync_file,
    full_fsync_available,
    is_space_exhaustion,
)

logger = logging.getLogger("Ouroboros.TranscriptWriter")


__all__ = [
    "DurableLogWriter",
    "LogHealth",
    "SEAL_REASON_ENV_SNAPSHOT",
    "read_reserve_bytes",
    "read_sync_every_n",
    "read_sync_interval_s",
]


# ===========================================================================
# Env vocabulary
# ===========================================================================


SYNC_EVERY_N_ENV_VAR: str = "JARVIS_TRANSCRIPT_SYNC_EVERY_N"
SYNC_INTERVAL_ENV_VAR: str = "JARVIS_TRANSCRIPT_SYNC_INTERVAL_S"
RESERVE_BYTES_ENV_VAR: str = "JARVIS_TRANSCRIPT_RESERVE_BYTES"
MAX_QUEUE_ENV_VAR: str = "JARVIS_TRANSCRIPT_MAX_QUEUE"

#: Frames between forced barriers. A transcript fires on every tool
#: render, and F_FULLFSYNC costs milliseconds on real hardware, so a
#: barrier per record would make the cockpit unusable. Group commit
#: trades a bounded, MEASURED window of records for that cost.
_DEFAULT_SYNC_EVERY_N: int = 32

#: Wall-clock ceiling on that window, so a quiet transcript still
#: reaches disk. Driven by the caller's event loop, never by a timer
#: thread — see :meth:`DurableLogWriter.run_flusher`.
_DEFAULT_SYNC_INTERVAL_S: float = 5.0

#: Stop appending while free space is under this. The point is to make
#: ENOSPC the RARE path: hitting it is survivable (we seal) but costs the
#: rest of the session's transcript, and stopping early costs only the
#: records that would not have fit anyway.
_DEFAULT_RESERVE_BYTES: int = 8 << 20  # 8 MiB

#: Queue depth before submissions are dropped and counted. Unbounded
#: queueing turns a slow or stuck disk into an OOM; a counted drop is a
#: fact the operator can read.
_DEFAULT_MAX_QUEUE: int = 4096

#: Checked every N appends rather than every append — statvfs is a
#: syscall, and the reserve is a soft threshold, not a fence.
_SPACE_CHECK_EVERY: int = 64

SEAL_REASON_ENV_SNAPSHOT: str = "seal_reason"


def _read_int(env_var: str, default: int) -> int:
    raw = os.environ.get(env_var, "").strip()
    if not raw:
        return default
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def read_sync_every_n() -> int:
    return _read_int(SYNC_EVERY_N_ENV_VAR, _DEFAULT_SYNC_EVERY_N)


def read_reserve_bytes() -> int:
    return _read_int(RESERVE_BYTES_ENV_VAR, _DEFAULT_RESERVE_BYTES)


def read_max_queue() -> int:
    return _read_int(MAX_QUEUE_ENV_VAR, _DEFAULT_MAX_QUEUE)


def read_sync_interval_s() -> float:
    raw = os.environ.get(SYNC_INTERVAL_ENV_VAR, "").strip()
    if not raw:
        return _DEFAULT_SYNC_INTERVAL_S
    try:
        parsed = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_SYNC_INTERVAL_S
    return parsed if parsed > 0 else _DEFAULT_SYNC_INTERVAL_S


# ===========================================================================
# Closed taxonomy — writer health
# ===========================================================================


class LogHealth(str, enum.Enum):
    """Closed 3-value state. Reported, never inferred by the caller."""

    DURABLE = "durable"
    DEGRADED = "degraded"
    SEALED = "sealed"

    @property
    def accepts_appends(self) -> bool:
        return self is not LogHealth.SEALED


@dataclass(frozen=True)
class WriterStats:
    """One atomic view of the writer, for ``snapshot_stats``."""

    health: str
    path: str
    head_seq: int
    durable_through_seq: int
    good_offset: int
    appended: int
    dropped: int
    rejected: int
    syncs: int
    sync_failures: int
    degraded_events: int
    seal_reason: str
    seal_errno: int
    full_fsync: bool
    free_bytes: int
    queue_depth: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "health": self.health,
            "path": self.path,
            "head_seq": self.head_seq,
            "durable_through_seq": self.durable_through_seq,
            #: head_seq - durable_through_seq is the honest answer to
            #: "how much would a power cut cost right now".
            "undurable_records": max(
                0, self.head_seq - self.durable_through_seq,
            ),
            "good_offset": self.good_offset,
            "appended": self.appended,
            "dropped": self.dropped,
            "rejected": self.rejected,
            "syncs": self.syncs,
            "sync_failures": self.sync_failures,
            "degraded_events": self.degraded_events,
            "seal_reason": self.seal_reason,
            "seal_errno": self.seal_errno,
            #: False means fsync() cannot reach the device write cache on
            #: this platform, so "durable" is a weaker claim. Stated
            #: rather than implied.
            "full_fsync": self.full_fsync,
            "free_bytes": self.free_bytes,
            "queue_depth": self.queue_depth,
        }


# ===========================================================================
# DurableLogWriter
# ===========================================================================


class DurableLogWriter:
    """Append-only, group-committed, seal-on-failure transcript log."""

    def __init__(
        self,
        path: "os.PathLike[str] | str",
        *,
        sync_every_n: Optional[int] = None,
        sync_interval_s: Optional[float] = None,
        reserve_bytes: Optional[int] = None,
        max_queue: Optional[int] = None,
    ) -> None:
        self._path = Path(path)
        self._tmp_path = self._path.with_suffix(self._path.suffix + ".compact")
        self._sync_every_n = int(sync_every_n or read_sync_every_n())
        self._sync_interval_s = float(sync_interval_s or read_sync_interval_s())
        self._reserve_bytes = int(reserve_bytes or read_reserve_bytes())
        self._max_queue = int(max_queue or read_max_queue())

        # Mutated ONLY on the io thread, except where noted.
        self._fd: int = -1
        self._good_offset: int = 0
        self._head_seq: int = 0
        self._durable_through: int = 0
        self._pending: int = 0
        self._last_sync_at: float = 0.0
        self._since_space_check: int = 0
        self._free_bytes: int = -1

        self._health: LogHealth = LogHealth.DURABLE
        self._seal_reason: str = ""
        self._seal_errno: int = 0
        self._counters: Dict[str, int] = {
            "appended": 0, "dropped": 0, "rejected": 0,
            "syncs": 0, "sync_failures": 0, "degraded_events": 0,
        }

        self._state_lock = threading.Lock()   # guards reads from other threads
        self._queue_depth = 0                 # guarded by _state_lock
        self._executor: Optional[ThreadPoolExecutor] = None
        self._started = False
        self._closed = False

    # ---- lifecycle ----------------------------------------------------

    def start(self) -> RecoveryResult:
        """Recover, truncate any torn tail, and open for append. BLOCKS.

        Called once, off the event loop. Truncating to ``durable_bytes``
        before the first append is what makes a crash LOOP idempotent
        rather than cumulative: without it, each crash would leave its
        own tear and the second one would be appended after the first,
        orphaning everything between them.
        """
        if self._started:
            raise RuntimeError("writer already started")
        self._started = True

        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._reap_stale_temp()

        result = recover_log(self._path)
        self._good_offset = result.durable_bytes
        self._head_seq = result.head_seq
        self._durable_through = result.head_seq

        try:
            self._fd = os.open(
                str(self._path),
                os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                0o600,
            )
            if result.trailing_bytes:
                # A torn tail from a previous life. Shrinking needs no
                # free space, which is why this still works when the
                # cause of the tear was a full disk.
                os.ftruncate(self._fd, self._good_offset)
                fsync_file(self._fd)
                logger.warning(
                    "[TranscriptLog] recovered %s: dropped %d trailing byte(s) "
                    "after seq=%d (reason=%s)",
                    self._path, result.trailing_bytes, result.head_seq,
                    result.stop_reason.value,
                )
            fsync_dir(self._path.parent)
        except OSError as exc:
            self._seal(f"open_failed:{exc.strerror}", exc)
            return result

        self._last_sync_at = time.monotonic()
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="transcript-io",
        )
        return result

    def close(self, *, sync: bool = True) -> None:
        """Drain, sync, and release. Idempotent. NEVER raises."""
        if self._closed:
            return
        self._closed = True
        ex, self._executor = self._executor, None
        if ex is not None:
            try:
                if sync:
                    ex.submit(self._sync_blocking).result(timeout=30)
            except Exception:  # noqa: BLE001
                logger.debug("[TranscriptLog] close sync degraded", exc_info=True)
            ex.shutdown(wait=True)
        if self._fd >= 0:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = -1

    # ---- submit (hot path — called under the spine's lock) -------------

    def submit(self, record: Mapping[str, Any]) -> bool:
        """Queue one record. NON-BLOCKING, thread-safe, NEVER raises.

        Returns ``True`` when accepted for writing — which is **not** a
        durability claim. ``durable_through_seq`` is the only honest
        answer to "did it survive", and it advances at a barrier.

        Encoding happens here, in the caller's thread, deliberately: a
        record that cannot be represented must be rejected while it is
        still a value, never carried to the io thread where its failure
        would arrive as a half-written frame.
        """
        ex = self._executor
        if ex is None or self._closed:
            return False
        with self._state_lock:
            if self._health is LogHealth.SEALED:
                return False
            if self._queue_depth >= self._max_queue:
                self._counters["dropped"] += 1
                return False
            self._queue_depth += 1

        try:
            frame = encode_record(record)
            seq = int(record["seq"])
        except Exception:  # noqa: BLE001 — unrepresentable record
            with self._state_lock:
                self._queue_depth -= 1
                self._counters["rejected"] += 1
            return False

        try:
            ex.submit(self._append_blocking, frame, seq)
        except RuntimeError:
            # Executor shut down between the check and the submit.
            with self._state_lock:
                self._queue_depth -= 1
                self._counters["dropped"] += 1
            return False
        return True

    def barrier(self, *, timeout: Optional[float] = 30.0) -> bool:
        """Force a sync and WAIT for it. Blocks the calling thread.

        For the event loop use :meth:`barrier_async`."""
        ex = self._executor
        if ex is None:
            return False
        try:
            fut: Future = ex.submit(self._sync_blocking)
            return bool(fut.result(timeout=timeout))
        except Exception:  # noqa: BLE001
            return False

    async def barrier_async(self, *, timeout: Optional[float] = 30.0) -> bool:
        """Force a sync from the event loop without blocking it."""
        import asyncio

        ex = self._executor
        if ex is None:
            return False
        loop = asyncio.get_running_loop()
        try:
            return bool(
                await asyncio.wait_for(
                    loop.run_in_executor(ex, self._sync_blocking),
                    timeout=timeout,
                ),
            )
        except Exception:  # noqa: BLE001
            return False

    async def run_flusher(self, interval_s: Optional[float] = None) -> None:
        """Time-based half of group commit. Owned by the caller's loop.

        A quiet transcript must still reach disk, but the interval cannot
        live on the io thread — sleeping there would block the very
        worker that is supposed to be writing, and a second timer thread
        would be a second place that knows the cadence. Reusing the
        event loop's timer keeps ONE authority for "when", while the work
        itself still happens on the executor.
        """
        import asyncio

        period = float(interval_s or self._sync_interval_s)
        try:
            while not self._closed and self._health.accepts_appends:
                await asyncio.sleep(period)
                await self.barrier_async()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.debug("[TranscriptLog] flusher degraded", exc_info=True)

    # ---- io thread ------------------------------------------------------

    def _append_blocking(self, frame: bytes, seq: int) -> None:
        """The ONLY writer of bytes. Runs on the io thread. NEVER raises."""
        try:
            with self._state_lock:
                self._queue_depth -= 1
            if self._health is LogHealth.SEALED or self._fd < 0:
                return

            if not self._space_available(len(frame)):
                # Refusing early is not sealing: nothing is torn, and a
                # later barrier can still heal us if space returns.
                self._degrade("reserve_exhausted")
                return

            try:
                _write_all(self._fd, frame)
            except OSError as exc:
                # SEAL, DON'T LIMP. Truncation first: it needs no free
                # space, so it is still possible in exactly the
                # circumstance that caused this.
                self._truncate_to_good_offset()
                self._seal(f"write_failed:{exc.strerror or exc.errno}", exc)
                return

            self._good_offset += len(frame)
            self._head_seq = max(self._head_seq, seq)
            self._pending += 1
            with self._state_lock:
                self._counters["appended"] += 1

            if self._sync_due():
                self._sync_blocking()
        except Exception:  # noqa: BLE001
            logger.debug("[TranscriptLog] append degraded", exc_info=True)

    def _sync_due(self) -> bool:
        if self._pending <= 0:
            return False
        if self._pending >= self._sync_every_n:
            return True
        return (time.monotonic() - self._last_sync_at) >= self._sync_interval_s

    def _sync_blocking(self) -> bool:
        """The durability barrier. Runs on the io thread. NEVER raises."""
        if self._fd < 0 or self._health is LogHealth.SEALED:
            return False
        if self._pending <= 0:
            return True
        try:
            fsync_file(self._fd)
        except OSError as exc:
            with self._state_lock:
                self._counters["sync_failures"] += 1
            if is_space_exhaustion(exc):
                # An ENOSPC surfacing at fsync means writeback failed and
                # the bytes are gone. What is on disk is still a valid
                # prefix; what is above durable_through is not, and never
                # will be, so continuing to append would be a lie.
                self._truncate_to_good_offset()
                self._seal(f"sync_failed:{exc.strerror or exc.errno}", exc)
            else:
                self._degrade(f"sync_failed:{exc.strerror or exc.errno}")
            return False

        self._durable_through = self._head_seq
        self._pending = 0
        self._last_sync_at = time.monotonic()
        with self._state_lock:
            self._counters["syncs"] += 1
            if self._health is LogHealth.DEGRADED:
                # Heal, but keep the history: degraded_events is never
                # decremented, so "this log was once not durable" stays
                # readable after recovery.
                self._health = LogHealth.DURABLE
        return True

    def _truncate_to_good_offset(self) -> None:
        """Remove any partial frame. Best-effort; failure keeps us sealed
        and the reader still stops at the tear, so the prefix is correct
        either way — this only spares the next boot the work."""
        try:
            if self._fd >= 0:
                os.ftruncate(self._fd, self._good_offset)
                fsync_file(self._fd)
        except OSError:
            logger.debug("[TranscriptLog] truncate degraded", exc_info=True)

    def _space_available(self, need: int) -> bool:
        """Soft reserve check, sampled rather than per-append."""
        self._since_space_check += 1
        if self._free_bytes >= 0 and self._since_space_check < _SPACE_CHECK_EVERY:
            return self._free_bytes > (self._reserve_bytes + need)
        self._since_space_check = 0
        try:
            st = os.statvfs(str(self._path.parent))
            self._free_bytes = st.f_bavail * st.f_frsize
        except OSError:
            self._free_bytes = -1
            return True           # unknown must not mean "refuse"
        return self._free_bytes > (self._reserve_bytes + need)

    # ---- state transitions ---------------------------------------------

    def _degrade(self, reason: str) -> None:
        """DURABLE -> DEGRADED. Appends continue; the promise does not."""
        with self._state_lock:
            if self._health is not LogHealth.DURABLE:
                return
            self._health = LogHealth.DEGRADED
            self._counters["degraded_events"] += 1
        logger.warning(
            "[TranscriptLog] %s DEGRADED (%s) — durable_through=%d head=%d",
            self._path.name, reason, self._durable_through, self._head_seq,
        )

    def _seal(self, reason: str, exc: Optional[BaseException] = None) -> None:
        """-> SEALED. Terminal for this writer, by design.

        Reopening is a deliberate act by the owner, not something the
        writer decides for itself: a writer that resurrects on its own
        would append after a tear the moment the fault cleared, which is
        precisely the limp this class exists to refuse."""
        with self._state_lock:
            if self._health is LogHealth.SEALED:
                return
            self._health = LogHealth.SEALED
            self._seal_reason = reason
            self._seal_errno = getattr(exc, "errno", 0) or 0
        logger.error(
            "[TranscriptLog] %s SEALED (%s) — valid prefix ends at byte %d, "
            "seq %d. No further records will be written by this writer.",
            self._path.name, reason, self._good_offset, self._head_seq,
        )

    # ---- compaction ------------------------------------------------------

    def compact(self, records: Iterable[Mapping[str, Any]]) -> bool:
        """Rewrite the log from ``records``. BLOCKS; io thread only.

        Preconditions matter more than the mechanism: compaction
        temporarily DOUBLES usage, so running it when space is tight is
        the one action most likely to cause the failure it is meant to
        prevent. It refuses rather than risking that, and the live log is
        never unlinked until the replacement is fsynced, renamed, and the
        directory fsynced.
        """
        if self._health is LogHealth.SEALED:
            return False
        frames = [encode_record(r) for r in records]
        needed = sum(len(f) for f in frames)
        if not self._space_available(needed * 2):
            logger.warning(
                "[TranscriptLog] compaction skipped — %d byte(s) free, "
                "need %d plus reserve", self._free_bytes, needed * 2,
            )
            return False

        try:
            # FIXED temp name, never mkstemp: a crash loop with random
            # names sprays inodes until the filesystem runs out of them,
            # and inode exhaustion arrives as the same ENOSPC we are
            # trying to survive. One name is reused, so a crash loop
            # costs one inode forever.
            fd = os.open(
                str(self._tmp_path),
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                0o600,
            )
            try:
                for frame in frames:
                    _write_all(fd, frame)
                fsync_file(fd)
            finally:
                os.close(fd)

            atomic_replace(self._tmp_path, self._path)
        except OSError as exc:
            self._reap_stale_temp()
            if is_space_exhaustion(exc):
                logger.warning(
                    "[TranscriptLog] compaction ran out of space — live log "
                    "untouched (%s)", exc,
                )
                self._degrade("compaction_enospc")
                return False
            self._seal(f"compaction_failed:{exc.strerror or exc.errno}", exc)
            return False

        # Re-open onto the replacement inode: the old fd still points at
        # the unlinked original, so appending through it would write to a
        # file no name refers to.
        try:
            if self._fd >= 0:
                os.close(self._fd)
            self._fd = os.open(
                str(self._path), os.O_WRONLY | os.O_APPEND, 0o600,
            )
        except OSError as exc:
            self._seal(f"reopen_failed:{exc.strerror or exc.errno}", exc)
            return False

        self._good_offset = needed
        self._pending = 0
        self._durable_through = self._head_seq
        return True

    def _reap_stale_temp(self) -> None:
        """Remove a compaction temp left by a crash. Safe because there
        is exactly one writer and the name is fixed — nobody else can be
        mid-write on it."""
        try:
            if self._tmp_path.exists():
                self._tmp_path.unlink()
        except OSError:
            logger.debug("[TranscriptLog] temp reap degraded", exc_info=True)

    # ---- observability ---------------------------------------------------

    @property
    def health(self) -> LogHealth:
        with self._state_lock:
            return self._health

    @property
    def durable_through_seq(self) -> int:
        return self._durable_through

    def snapshot_stats(self) -> Dict[str, Any]:
        """One honest view. NEVER raises."""
        try:
            with self._state_lock:
                stats = WriterStats(
                    health=self._health.value,
                    path=str(self._path),
                    head_seq=self._head_seq,
                    durable_through_seq=self._durable_through,
                    good_offset=self._good_offset,
                    appended=self._counters["appended"],
                    dropped=self._counters["dropped"],
                    rejected=self._counters["rejected"],
                    syncs=self._counters["syncs"],
                    sync_failures=self._counters["sync_failures"],
                    degraded_events=self._counters["degraded_events"],
                    seal_reason=self._seal_reason,
                    seal_errno=self._seal_errno,
                    full_fsync=full_fsync_available(),
                    free_bytes=self._free_bytes,
                    queue_depth=self._queue_depth,
                )
            return stats.to_dict()
        except Exception:  # noqa: BLE001
            return {"health": "unknown"}


# ===========================================================================
# Step 6 — spine wiring
# ===========================================================================


DURABLE_ENABLED_ENV_VAR: str = "JARVIS_TRANSCRIPT_DURABLE_ENABLED"
MAX_PAYLOAD_ENV_VAR: str = "JARVIS_TRANSCRIPT_MAX_PAYLOAD_BYTES"

#: A diff payload can be megabytes. Persisting it verbatim would let one
#: record consume the space budget of a thousand, so an oversize payload
#: is replaced by a marker that states its size — the transcript keeps
#: the ORDER and admits what it dropped, rather than silently storing a
#: truncated body that reads as complete.
_DEFAULT_MAX_PAYLOAD_BYTES: int = 8 << 10


def durable_enabled() -> bool:
    """Read :data:`DURABLE_ENABLED_ENV_VAR`. **Default false** for this
    slice: persistence has not been live-proven, and a durability layer
    that arms itself before its first soak is the failure mode this arc
    exists to remove. Flip to ``true`` to attach."""
    raw = os.environ.get(DURABLE_ENABLED_ENV_VAR, "false")
    return raw.strip().lower() in ("1", "true", "yes", "on")


def read_max_payload_bytes() -> int:
    return _read_int(MAX_PAYLOAD_ENV_VAR, _DEFAULT_MAX_PAYLOAD_BYTES)


def build_durable_sink(writer: "DurableLogWriter",
                       *, max_payload_bytes: Optional[int] = None):
    """Adapt a :class:`SpineRecord` to a log record. O(1), non-blocking.

    Uses the record's OWN ``to_dict(include_payload=True)``, which in
    turn reuses each payload's own ``to_dict`` — so a record persists
    exactly as its store already serialises it, and there is no second
    definition of "what a diff looks like" to drift from the first.
    """
    import json

    cap = int(max_payload_bytes or read_max_payload_bytes())

    def _sink(rec: Any) -> None:
        doc = rec.to_dict(include_payload=True)
        payload = doc.get("payload")
        if payload is not None:
            try:
                blob = json.dumps(
                    payload, separators=(",", ":"), ensure_ascii=True,
                )
            except (TypeError, ValueError):
                doc["payload"] = {"__unrenderable__": True}
            else:
                if len(blob) > cap:
                    doc["payload"] = {
                        "__truncated__": True, "bytes": len(blob),
                    }
        writer.submit(doc)

    return _sink


def install_durable_transcript(
    path: "os.PathLike[str] | str",
    *,
    spine: Optional[Any] = None,
    force: bool = False,
) -> Optional["DurableLogWriter"]:
    """Attach durability to the process spine. BLOCKS (recovery + open).

    Returns the writer, or ``None`` when the flag is off or the log
    cannot be opened — never a half-attached state. Call once, off the
    event loop, then own :meth:`DurableLogWriter.run_flusher` as a task.
    """
    if not (force or durable_enabled()):
        return None
    from backend.core.ouroboros.battle_test.transcript_spine import (
        get_default_spine,
    )

    target = spine if spine is not None else get_default_spine()
    writer = DurableLogWriter(path)
    try:
        result = writer.start()
    except Exception:  # noqa: BLE001
        logger.warning("[TranscriptLog] durability unavailable", exc_info=True)
        return None
    if writer.health is LogHealth.SEALED:
        logger.error(
            "[TranscriptLog] sealed at open — running memory-only, "
            "and saying so rather than implying durability",
        )
        return None

    target.attach_sink(build_durable_sink(writer))
    logger.info(
        "[TranscriptLog] durable transcript attached at %s "
        "(recovered %d record(s), full_fsync=%s)",
        path, len(result.records), full_fsync_available(),
    )
    return writer


def _write_all(fd: int, data: bytes) -> None:
    """Complete a short write. Safe because this process runs exactly one
    io thread: with a second appender, resuming a short write would
    splice another writer's frame into the middle of this one."""
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:                       # pragma: no cover
            raise OSError(errno.EIO, "write returned 0")
        view = view[written:]
