"""JSONL Append-Only Trace Store v1.0

Three-stream persistence for causal traceability:
- lifecycle: boot/phase/shutdown events (never dropped)
- decisions: routing/termination decisions (date-partitioned)
- spans: individual operation spans (date-partitioned, backpressure)

Thread-safe. Uses O_APPEND + fcntl for cross-process safety.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import threading
import time
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# JSONLWriter -- atomic append with checksum
# ---------------------------------------------------------------------------

class JSONLWriter:
    """Append-only JSONL writer with per-line checksums.

    Thread-safe via fcntl file lock (works cross-process).
    Uses O_APPEND for atomic append semantics.
    """

    __slots__ = ("_path",)

    def __init__(self, file_path: Path) -> None:
        self._path = Path(file_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, record: Dict[str, Any]) -> None:
        """Append a record as a single JSONL line with checksum."""
        # Compute checksum of the payload (before adding checksum)
        payload_bytes = json.dumps(
            record, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        checksum = zlib.crc32(payload_bytes) & 0xFFFFFFFF
        record_with_checksum = dict(record)
        record_with_checksum["_checksum"] = checksum

        line = json.dumps(record_with_checksum, separators=(",", ":")) + "\n"
        line_bytes = line.encode("utf-8")

        # Open with O_APPEND for atomic append, use fcntl for cross-process lock
        fd = os.open(
            str(self._path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644
        )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            os.write(fd, line_bytes)
            os.fsync(fd)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    @property
    def path(self) -> Path:
        return self._path


# ---------------------------------------------------------------------------
# SpanBuffer -- in-memory buffer with backpressure
# ---------------------------------------------------------------------------

class SpanBuffer:
    """In-memory buffer for span records with backpressure policy.

    - At >80% capacity: sample success spans at 50%
    - At >95% capacity: keep only errors/timeouts
    - Never drops records with idempotency_key

    Thread-safe via threading.Lock.
    """

    __slots__ = ("_buffer", "_lock", "_max_size", "_drop_count")

    def __init__(self, max_size: int = 256) -> None:
        self._buffer: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self._max_size = max(1, max_size)
        self._drop_count: int = 0

    def add(self, record: Dict[str, Any]) -> bool:
        """Add a record to the buffer. Returns True if kept, False if dropped."""
        with self._lock:
            fill_ratio = len(self._buffer) / self._max_size

            # Never drop records with idempotency_key
            has_idem_key = bool(record.get("idempotency_key"))
            status = record.get("status", "")
            is_error = status in ("error", "timeout", "failure")

            if fill_ratio >= 0.95 and not has_idem_key and not is_error:
                self._drop_count += 1
                return False

            if fill_ratio >= 0.80 and not has_idem_key and not is_error:
                # Sample at 50% -- use hash of event_id for determinism
                eid = record.get("event_id", "")
                if hash(eid) % 2 == 0:
                    self._drop_count += 1
                    return False

            self._buffer.append(record)
            return True

    def drain(self) -> List[Dict[str, Any]]:
        """Drain and return all buffered records."""
        with self._lock:
            records = self._buffer
            self._buffer = []
            return records

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._buffer)

    @property
    def drop_count(self) -> int:
        return self._drop_count


# ---------------------------------------------------------------------------
# TraceStreamManager -- three-stream persistence
# ---------------------------------------------------------------------------

class TraceStreamManager:
    """Manages three JSONL streams: lifecycle, decisions, spans.

    - lifecycle: epoch-partitioned (one file per runtime epoch)
    - decisions: date-partitioned (one file per day)
    - spans: date-partitioned, buffered with backpressure
    """

    def __init__(
        self,
        base_dir: Path,
        runtime_epoch_id: str,
        span_buffer_size: int = 256,
    ) -> None:
        self._base_dir = Path(base_dir)
        self._epoch_id = runtime_epoch_id
        self._span_buffer = SpanBuffer(max_size=span_buffer_size)

        # Create stream directories
        for subdir in ("lifecycle", "decisions", "spans"):
            (self._base_dir / subdir).mkdir(parents=True, exist_ok=True)

        # Lifecycle writer is epoch-scoped (one file per epoch)
        self._lifecycle_writer = JSONLWriter(
            self._base_dir
            / "lifecycle"
            / f"{time.strftime('%Y%m%d')}_epoch_{self._epoch_id}.jsonl"
        )

    def write_lifecycle(self, record: Dict[str, Any]) -> None:
        """Write a lifecycle event. Never dropped."""
        self._lifecycle_writer.append(record)

    def write_decision(self, record: Dict[str, Any]) -> None:
        """Write a decision event. Date-partitioned."""
        writer = JSONLWriter(
            self._base_dir / "decisions" / f"{time.strftime('%Y%m%d')}.jsonl"
        )
        writer.append(record)

    def write_span(self, record: Dict[str, Any]) -> None:
        """Buffer a span record (subject to backpressure)."""
        self._span_buffer.add(record)

    def flush_spans(self) -> int:
        """Flush buffered spans to disk. Returns count of flushed records."""
        records = self._span_buffer.drain()
        if not records:
            return 0
        writer = JSONLWriter(
            self._base_dir / "spans" / f"{time.strftime('%Y%m%d')}.jsonl"
        )
        for record in records:
            writer.append(record)
        return len(records)

    @property
    def span_buffer(self) -> SpanBuffer:
        """Access span buffer for inspection."""
        return self._span_buffer


# ---------------------------------------------------------------------------
# DiskGuard -- stub for Task 12
# ---------------------------------------------------------------------------

class DiskGuard:
    """Stub for disk usage monitoring. Full implementation in Task 12."""

    def check_disk_usage(self) -> float:
        """Return disk usage ratio (0.0 - 1.0). Stub returns 0.0."""
        return 0.0

    def should_rotate(self) -> bool:
        """Return True if rotation is needed. Stub returns False."""
        return False
