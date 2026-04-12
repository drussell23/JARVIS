"""DurableJSONLTransport — P2-2 persistent idempotent event log.

CommProtocol transport that appends every governance message to a
sequenced JSONL file on disk.

Design
------
* One JSONL file per calendar day: ``<log_dir>/governance-events-YYYY-MM-DD.jsonl``
* Each line is a JSON object with all CommMessage fields plus write metadata.
* The ``idempotency_key`` field is used to skip duplicate writes during
  replay (e.g. after crash-restart).
* Rotation happens transparently: a new file is opened when the date changes.
* File writes use ``asyncio.to_thread`` to avoid blocking the event loop.
* No locks needed: a single background writer is serialised through the
  BoundedFanout queue; the JSONL append is the only I/O.

Replay
------
To replay all events since a given timestamp::

    log = DurableJSONLTransport(Path("/var/jarvis/events"))
    async for entry in log.iter_entries(since=datetime(...)):
        process(entry)
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

from backend.core.ouroboros.governance.comm_protocol import CommMessage

__all__ = [
    "DurableJSONLTransport",
    "EventLogEntry",
]

logger = logging.getLogger(__name__)

# Maximum in-flight messages buffered before shedding.
_QUEUE_MAXSIZE = 2000
# Reserved: rotate when file exceeds this size (not yet implemented).
# _MAX_FILE_BYTES = 100 * 1024 * 1024


# ---------------------------------------------------------------------------
# Log entry format
# ---------------------------------------------------------------------------


@dataclass
class EventLogEntry:
    """One line in the JSONL event log."""

    idempotency_key: str
    op_id: str
    msg_type: str
    seq: int
    global_seq: int
    causal_parent_seq: Optional[int]
    correlation_id: str
    payload: Dict[str, Any]
    timestamp_wall: float          # from CommMessage
    write_monotonic: float         # time.monotonic() at write time
    write_wall: str                # ISO-8601 UTC at write time

    def to_json_line(self) -> str:
        d = {
            "idempotency_key": self.idempotency_key,
            "op_id": self.op_id,
            "msg_type": self.msg_type,
            "seq": self.seq,
            "global_seq": self.global_seq,
            "causal_parent_seq": self.causal_parent_seq,
            "correlation_id": self.correlation_id,
            "payload": self.payload,
            "timestamp_wall": self.timestamp_wall,
            "write_monotonic": self.write_monotonic,
            "write_wall": self.write_wall,
        }
        return json.dumps(d, separators=(",", ":"))

    @classmethod
    def from_comm_message(cls, msg: CommMessage) -> "EventLogEntry":
        now_mono = time.monotonic()
        now_wall = datetime.now(tz=timezone.utc).isoformat()
        return cls(
            idempotency_key=msg.idempotency_key,
            op_id=msg.op_id,
            msg_type=msg.msg_type.value,
            seq=msg.seq,
            global_seq=getattr(msg, "global_seq", 0),
            causal_parent_seq=msg.causal_parent_seq,
            correlation_id=getattr(msg, "correlation_id", ""),
            payload=dict(msg.payload),
            timestamp_wall=msg.timestamp,
            write_monotonic=now_mono,
            write_wall=now_wall,
        )

    @classmethod
    def from_json_line(cls, line: str) -> "EventLogEntry":
        d = json.loads(line)
        return cls(**d)


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


class DurableJSONLTransport:
    """CommProtocol transport that persists every message to a JSONL log.

    Parameters
    ----------
    log_dir:
        Directory where JSONL files are stored.  Created if absent.
    queue_maxsize:
        Number of messages buffered before the oldest are shed (prevents
        log-writer from consuming unbounded memory under burst load).
    """

    def __init__(
        self,
        log_dir: Path,
        queue_maxsize: int = _QUEUE_MAXSIZE,
    ) -> None:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        from backend.core.ouroboros.governance.sandbox_paths import sandbox_fallback
        self._log_dir = sandbox_fallback(log_dir)
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._queue: "asyncio.Queue[EventLogEntry]" = asyncio.Queue(
            maxsize=queue_maxsize
        )
        self._worker_task: Optional["asyncio.Task[None]"] = None
        # Dedup guard: holds idempotency_keys seen in the CURRENT process run.
        # On restart this is empty, but the JSONL is the durable record.
        self._seen_keys: set = set()
        self._shed_count: int = 0
        self._written_count: int = 0
        self._write_error_logged: bool = False

    # ------------------------------------------------------------------
    # CommProtocol transport interface
    # ------------------------------------------------------------------

    async def send(self, msg: CommMessage) -> None:
        """Serialize *msg* and enqueue for async writing.

        Returns immediately (non-blocking).  If the write queue is full,
        the oldest entry is shed (DROP_OLDEST policy).
        """
        self._ensure_worker_started()

        # In-process dedup via idempotency_key
        key = msg.idempotency_key
        if key and key in self._seen_keys:
            logger.debug("[DurableJSONL] dedup skip key=%s", key)
            return

        entry = EventLogEntry.from_comm_message(msg)

        if self._queue.full():
            # DROP_OLDEST shedding
            try:
                self._queue.get_nowait()
                self._shed_count += 1
                logger.debug(
                    "[DurableJSONL] queue full — shed oldest (total_shed=%d)",
                    self._shed_count,
                )
            except asyncio.QueueEmpty:
                pass

        try:
            self._queue.put_nowait(entry)
        except asyncio.QueueFull:
            self._shed_count += 1
            return

        if key:
            self._seen_keys.add(key)
            # Prevent unbounded in-memory growth: evict if > 50k keys
            if len(self._seen_keys) > 50_000:
                self._seen_keys.pop()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _ensure_worker_started(self) -> None:
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.ensure_future(self._write_loop())

    async def stop(self) -> None:
        """Flush remaining entries and stop the writer."""
        if self._worker_task is not None:
            # Drain remaining
            while not self._queue.empty():
                await asyncio.sleep(0.01)
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass

    # ------------------------------------------------------------------
    # Replay / iteration
    # ------------------------------------------------------------------

    def iter_entries(
        self,
        since: Optional[datetime] = None,
    ) -> Iterator[EventLogEntry]:
        """Synchronously iterate over all log entries in date order.

        Parameters
        ----------
        since:
            Only yield entries with ``write_wall`` >= *since*.
        """
        since_iso = since.isoformat() if since else None
        for log_file in sorted(self._log_dir.glob("governance-events-*.jsonl")):
            with open(log_file, "r", encoding="utf-8") as fh:
                for raw_line in fh:
                    raw_line = raw_line.strip()
                    if not raw_line:
                        continue
                    try:
                        entry = EventLogEntry.from_json_line(raw_line)
                        if since_iso and entry.write_wall < since_iso:
                            continue
                        yield entry
                    except Exception as exc:
                        logger.warning(
                            "[DurableJSONL] corrupted line in %s: %s",
                            log_file.name, exc,
                        )

    # ------------------------------------------------------------------
    # Internal write loop
    # ------------------------------------------------------------------

    async def _write_loop(self) -> None:
        """Drain the queue, writing entries to the current day's JSONL file."""
        current_date: str = ""
        current_fh = None
        current_file: Optional[Path] = None

        try:
            while True:
                entry = await self._queue.get()
                try:
                    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
                    if today != current_date or current_fh is None:
                        if current_fh is not None:
                            current_fh.close()
                        current_date = today
                        current_file = self._log_dir / f"governance-events-{today}.jsonl"
                        current_fh = await asyncio.to_thread(
                            open, current_file, "a", encoding="utf-8"
                        )
                    line = entry.to_json_line() + "\n"
                    await asyncio.to_thread(current_fh.write, line)
                    await asyncio.to_thread(current_fh.flush)
                    self._written_count += 1
                except Exception as exc:
                    if not self._write_error_logged:
                        logger.warning("[DurableJSONL] write error (suppressing further): %s", exc)
                        self._write_error_logged = True
                finally:
                    self._queue.task_done()
        except asyncio.CancelledError:
            if current_fh is not None:
                try:
                    current_fh.close()
                except Exception:
                    pass

    def stats(self) -> dict:
        return {
            "written": self._written_count,
            "shed": self._shed_count,
            "queue_size": self._queue.qsize(),
        }
