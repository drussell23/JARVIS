"""
Write-Ahead Log (WAL) for the Unified Intake Router.

Append-only JSONL file.  Each line is a WAL record.
Supports append (with fsync), status updates, crash-recovery replay,
and compaction (remove entries older than max_age_days).

At-least-once guarantee: router replays all ``status="pending"``
entries on startup and checks idempotency_key against the ledger to
skip already-terminal ops.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)
_WAL_VERSION = 1


@dataclass
class WALEntry:
    lease_id: str
    envelope_dict: Dict[str, Any]
    status: str  # "pending" | "acked" | "dead_letter"
    ts_monotonic: float
    ts_utc: str


class WAL:
    """Append-only write-ahead log for intake envelopes.

    Parameters
    ----------
    path:
        Path to the JSONL WAL file (created on first append).
    max_age_days:
        Entries older than this are pruned during :meth:`compact`.
    """

    def __init__(self, path: Path, max_age_days: int = 7) -> None:
        self._path = path
        self._max_age_days = max_age_days
        path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, entry: WALEntry) -> None:
        """Append an entry and fsync."""
        record = {
            "v": _WAL_VERSION,
            "lease_id": entry.lease_id,
            "envelope": entry.envelope_dict,
            "status": entry.status,
            "ts_monotonic": entry.ts_monotonic,
            "ts_utc": entry.ts_utc,
        }
        self._write_line(record)

    def update_status(self, lease_id: str, status: str) -> None:
        """Append a status-update tombstone for the given lease_id."""
        record = {
            "v": _WAL_VERSION,
            "lease_id": lease_id,
            "status": status,
            "ts_monotonic": time.monotonic(),
            "ts_utc": datetime.now(timezone.utc).isoformat(),
            "_type": "status_update",
        }
        self._write_line(record)

    def pending_entries(self) -> List[WALEntry]:
        """Return all entries whose effective status is ``'pending'``.

        Reads the entire WAL, applies status-update tombstones, and
        returns only those entries that remain pending.  Used for
        crash-recovery replay on startup.
        """
        entries: Dict[str, WALEntry] = {}
        status_overrides: Dict[str, str] = {}

        if not self._path.exists():
            return []

        with self._path.open("r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("WAL: corrupt entry at line %d, skipping", line_no)
                    continue

                lease_id: str = record.get("lease_id", "")
                if not lease_id:
                    continue

                if record.get("_type") == "status_update":
                    status_overrides[lease_id] = record.get("status", "pending")
                elif "envelope" in record:
                    entries[lease_id] = WALEntry(
                        lease_id=lease_id,
                        envelope_dict=record["envelope"],
                        status=record.get("status", "pending"),
                        ts_monotonic=record.get("ts_monotonic", 0.0),
                        ts_utc=record.get("ts_utc", ""),
                    )

        # Apply tombstones
        for lid, status in status_overrides.items():
            if lid in entries:
                e = entries[lid]
                entries[lid] = WALEntry(
                    lease_id=e.lease_id,
                    envelope_dict=e.envelope_dict,
                    status=status,
                    ts_monotonic=e.ts_monotonic,
                    ts_utc=e.ts_utc,
                )

        return [e for e in entries.values() if e.status == "pending"]

    def compact(self) -> int:
        """Remove entries older than ``max_age_days``.

        Returns the number of removed lines.
        """
        if not self._path.exists():
            return 0

        max_age_s = self._max_age_days * 86400.0
        now = time.monotonic()
        kept: List[str] = []
        removed = 0

        with self._path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    removed += 1
                    continue
                ts = record.get("ts_monotonic", now)
                if (now - ts) < max_age_s:
                    kept.append(line)
                else:
                    removed += 1

        with self._path.open("w", encoding="utf-8") as f:
            for line in kept:
                f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())

        return removed

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _write_line(self, record: Dict[str, Any]) -> None:
        with self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
            f.flush()
            os.fsync(f.fileno())
