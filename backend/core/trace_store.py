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
import sqlite3
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
        # Strip any pre-existing _checksum so it doesn't affect the hash
        clean_record = {k: v for k, v in record.items() if k != "_checksum"}
        payload_bytes = json.dumps(
            clean_record, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        checksum = zlib.crc32(payload_bytes) & 0xFFFFFFFF
        record_with_checksum = dict(clean_record)
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

    @staticmethod
    def verify_checksum(record: Dict[str, Any]) -> bool:
        """Verify the CRC32 checksum of a record read back from JSONL."""
        stored = record.get("_checksum")
        if stored is None:
            return False
        clean = {k: v for k, v in record.items() if k != "_checksum"}
        payload_bytes = json.dumps(
            clean, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return (zlib.crc32(payload_bytes) & 0xFFFFFFFF) == stored

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
    - Hard cap at 2x max_size to prevent unbounded growth from
      error/idempotency records that bypass backpressure
    - Never drops records with idempotency_key (unless hard cap hit)

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
            # Hard cap: prevent unbounded growth even for priority records
            if len(self._buffer) >= self._max_size * 2:
                self._drop_count += 1
                return False

            fill_ratio = len(self._buffer) / self._max_size

            # Never drop records with idempotency_key (below hard cap)
            has_idem_key = bool(record.get("idempotency_key"))
            status = record.get("status", "")
            is_error = status in ("error", "timeout", "failure")

            if fill_ratio >= 0.95 and not has_idem_key and not is_error:
                self._drop_count += 1
                return False

            if fill_ratio >= 0.80 and not has_idem_key and not is_error:
                # Sample at 50% -- use crc32 for cross-process determinism
                eid = record.get("event_id", "")
                if zlib.crc32(eid.encode("utf-8")) % 2 == 0:
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
        with self._lock:
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
        self._decision_writers: Dict[str, JSONLWriter] = {}

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

    def _get_decision_writer(self) -> JSONLWriter:
        """Get or create a cached decision writer for today's date."""
        date_key = time.strftime("%Y%m%d")
        writer = self._decision_writers.get(date_key)
        if writer is None:
            writer = JSONLWriter(
                self._base_dir / "decisions" / f"{date_key}.jsonl"
            )
            self._decision_writers[date_key] = writer
        return writer

    def write_decision(self, record: Dict[str, Any]) -> None:
        """Write a decision event. Date-partitioned."""
        self._get_decision_writer().append(record)

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
# TraceIndex -- SQLite-backed trace lookup index
# ---------------------------------------------------------------------------


class TraceIndex:
    """SQLite-backed trace lookup index. Rebuildable from JSONL files.

    Not thread-safe — callers must serialize access or use one instance per thread.
    Supports context manager protocol for safe cleanup.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._create_tables()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _create_tables(self) -> None:
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS trace_events (
                event_id TEXT PRIMARY KEY,
                trace_id TEXT NOT NULL,
                stream TEXT NOT NULL,
                file_path TEXT NOT NULL,
                byte_offset INTEGER NOT NULL,
                ts_wall_utc REAL NOT NULL,
                operation TEXT,
                status TEXT
            )
        """)
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_trace_id ON trace_events(trace_id)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ts ON trace_events(ts_wall_utc)"
        )
        self._conn.commit()

    def index_event(
        self,
        trace_id: str,
        event_id: str,
        stream: str,
        file_path: str,
        byte_offset: int,
        ts_wall_utc: float,
        operation: str,
        status: str,
    ) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO trace_events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_id,
                trace_id,
                stream,
                str(file_path),
                byte_offset,
                ts_wall_utc,
                operation,
                status,
            ),
        )
        self._conn.commit()

    def query_by_trace(self, trace_id: str) -> List[Dict[str, Any]]:
        cursor = self._conn.execute(
            "SELECT * FROM trace_events WHERE trace_id = ? ORDER BY ts_wall_utc",
            (trace_id,),
        )
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]

    def query_by_time(
        self,
        since: Optional[float] = None,
        until: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        conditions: List[str] = []
        params: List[float] = []
        if since is not None:
            conditions.append("ts_wall_utc >= ?")
            params.append(since)
        if until is not None:
            conditions.append("ts_wall_utc <= ?")
            params.append(until)
        where = " AND ".join(conditions) if conditions else "1=1"
        cursor = self._conn.execute(
            f"SELECT * FROM trace_events WHERE {where} ORDER BY ts_wall_utc",
            params,
        )
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]

    def rebuild_from_directory(self, dir_path: Path, stream: str) -> int:
        """Rebuild index from JSONL files in a directory. Returns count of indexed events.

        Uses a single transaction for performance (avoids per-record fsync).
        """
        import json as _json

        dir_path = Path(dir_path)
        count = 0
        self._conn.execute("BEGIN")
        try:
            for jsonl_file in sorted(dir_path.glob("*.jsonl")):
                with open(jsonl_file, "rb") as f:
                    for raw_line in f:
                        offset = f.tell() - len(raw_line)
                        line = raw_line.decode("utf-8", errors="replace").strip()
                        if not line:
                            continue
                        try:
                            record = _json.loads(line)
                            envelope = record.get("envelope", {})
                            trace_id = envelope.get("trace_id", "")
                            event_id = envelope.get("event_id", "")
                            ts = envelope.get("ts_wall_utc", 0.0)
                            operation = record.get("event_type", "")
                            if trace_id and event_id:
                                self._conn.execute(
                                    "INSERT OR REPLACE INTO trace_events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                    (event_id, trace_id, stream, str(jsonl_file),
                                     offset, ts, operation, record.get("status", "")),
                                )
                                count += 1
                        except Exception:
                            logger.debug(
                                "Failed to parse JSONL line for indexing",
                                exc_info=True,
                            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return count

    def close(self) -> None:
        self._conn.close()


# ---------------------------------------------------------------------------
# CausalityIndex -- SQLite-backed causality DAG with cycle detection
# ---------------------------------------------------------------------------


class CausalityIndex:
    """SQLite-backed causality DAG with cycle detection.

    Not thread-safe — callers must serialize access or use one instance per thread.
    Supports context manager protocol for safe cleanup.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._create_tables()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _create_tables(self) -> None:
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS causality_edges (
                event_id TEXT PRIMARY KEY,
                caused_by_event_id TEXT,
                parent_span_id TEXT,
                trace_id TEXT NOT NULL,
                operation TEXT,
                ts_wall_utc REAL
            )
        """)
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_caused_by ON causality_edges(caused_by_event_id)"
        )
        self._conn.commit()

    def add_edge(
        self,
        event_id: str,
        caused_by_event_id: Optional[str],
        parent_span_id: Optional[str],
        trace_id: str,
        operation: str,
        ts_wall_utc: float,
    ) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO causality_edges VALUES (?, ?, ?, ?, ?, ?)",
            (
                event_id,
                caused_by_event_id,
                parent_span_id,
                trace_id,
                operation,
                ts_wall_utc,
            ),
        )
        self._conn.commit()

    def get_children(self, event_id: str) -> List[Dict[str, Any]]:
        cursor = self._conn.execute(
            "SELECT * FROM causality_edges WHERE caused_by_event_id = ? ORDER BY ts_wall_utc",
            (event_id,),
        )
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]

    def get_parent(self, event_id: str) -> Optional[Dict[str, Any]]:
        cursor = self._conn.execute(
            "SELECT caused_by_event_id FROM causality_edges WHERE event_id = ?",
            (event_id,),
        )
        row = cursor.fetchone()
        if row and row[0]:
            return {"caused_by_event_id": row[0]}
        return None

    def detect_cycles(self) -> List[List[str]]:
        """Iterative DFS-based cycle detection. Returns list of cycles found.

        Uses explicit stack to avoid recursion depth limits on deep DAGs.
        """
        cursor = self._conn.execute(
            "SELECT event_id, caused_by_event_id FROM causality_edges"
        )
        edges: Dict[str, Optional[str]] = {}
        for row in cursor.fetchall():
            event_id, caused_by = row
            edges[event_id] = caused_by

        # Build adjacency: caused_by -> children
        children_map: Dict[str, List[str]] = {}
        for event_id, caused_by in edges.items():
            if caused_by is not None:
                children_map.setdefault(caused_by, []).append(event_id)

        cycles: List[List[str]] = []
        visited: set = set()
        in_stack: set = set()

        # Check self-references first
        for event_id, caused_by in edges.items():
            if caused_by == event_id:
                cycles.append([event_id])
                visited.add(event_id)

        # Iterative DFS with explicit stack
        for start in edges:
            if start in visited:
                continue
            # Stack entries: (node, child_index, path)
            stack: List[tuple] = [(start, 0, [start])]
            visited.add(start)
            in_stack.add(start)

            while stack:
                node, ci, path = stack[-1]
                node_children = children_map.get(node, [])

                if ci < len(node_children):
                    stack[-1] = (node, ci + 1, path)
                    child = node_children[ci]
                    if child in in_stack:
                        cycle_start = path.index(child) if child in path else 0
                        cycles.append(list(path[cycle_start:]))
                    elif child not in visited:
                        visited.add(child)
                        in_stack.add(child)
                        stack.append((child, 0, path + [child]))
                else:
                    in_stack.discard(node)
                    stack.pop()

        return cycles

    def close(self) -> None:
        self._conn.close()


# ---------------------------------------------------------------------------
# DiskGuard -- disk usage monitoring and rotation
# ---------------------------------------------------------------------------

_DISK_WARNING_THRESHOLD = float(os.environ.get("JARVIS_TRACE_DISK_WARNING", "0.85"))
_DISK_CRITICAL_THRESHOLD = float(os.environ.get("JARVIS_TRACE_DISK_CRITICAL", "0.95"))

# Rotation priority: spans first (highest volume, lowest value), then old
# decisions, then old lifecycle (never current epoch).
_ROTATION_PRIORITY = ["spans", "decisions", "lifecycle"]


class DiskGuard:
    """Disk usage monitoring and rotation for trace data."""

    def __init__(
        self,
        base_dir: Path,
        warning_threshold: float = _DISK_WARNING_THRESHOLD,
        critical_threshold: float = _DISK_CRITICAL_THRESHOLD,
    ) -> None:
        self._base_dir = Path(base_dir)
        self._warning_threshold = warning_threshold
        self._critical_threshold = critical_threshold

    def check_disk_usage(self) -> float:
        """Return disk usage ratio (0.0 - 1.0)."""
        import shutil
        try:
            usage = shutil.disk_usage(self._base_dir)
            return usage.used / usage.total if usage.total > 0 else 0.0
        except OSError:
            return 0.0

    def should_rotate(self) -> bool:
        """Return True if disk usage exceeds critical threshold."""
        return self.check_disk_usage() >= self._critical_threshold

    def rotate_if_needed(self, current_epoch: str = "") -> List[str]:
        """Rotate old files if disk usage exceeds critical threshold.

        Rotation priority: spans → decisions → lifecycle.
        Never deletes files from the current epoch.
        Returns list of deleted file paths.
        """
        if not self.should_rotate():
            return []

        rotated: List[str] = []
        for stream in _ROTATION_PRIORITY:
            stream_dir = self._base_dir / stream
            if not stream_dir.exists():
                continue
            for jsonl_file in sorted(stream_dir.glob("*.jsonl")):
                # Never delete current epoch files
                if current_epoch and current_epoch in jsonl_file.name:
                    continue
                try:
                    jsonl_file.unlink()
                    rotated.append(str(jsonl_file))
                except OSError:
                    logger.debug("Failed to delete %s", jsonl_file, exc_info=True)

            # Re-check after each stream
            if not self.should_rotate():
                break

        return rotated


# ---------------------------------------------------------------------------
# Compaction -- gzip old JSONL files
# ---------------------------------------------------------------------------

def compact_old_files(dir_path: Path, max_age_days: int = 7) -> List[str]:
    """Compress JSONL files older than max_age_days to .jsonl.gz.

    Returns list of compressed file paths.
    """
    import gzip

    dir_path = Path(dir_path)
    if not dir_path.exists():
        return []

    now = time.time()
    cutoff = now - (max_age_days * 86400)
    compressed: List[str] = []

    for jsonl_file in sorted(dir_path.glob("*.jsonl")):
        try:
            if jsonl_file.stat().st_mtime <= cutoff:
                gz_path = jsonl_file.with_suffix(".jsonl.gz")
                with open(jsonl_file, "rb") as f_in:
                    with gzip.open(gz_path, "wb") as f_out:
                        f_out.write(f_in.read())
                jsonl_file.unlink()
                compressed.append(str(gz_path))
        except OSError:
            logger.debug("Failed to compact %s", jsonl_file, exc_info=True)

    return compressed
