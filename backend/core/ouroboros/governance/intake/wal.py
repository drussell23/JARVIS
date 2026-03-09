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
_TERMINAL_STATUSES = frozenset({"acked", "dead_letter"})


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
        if status not in _TERMINAL_STATUSES:
            raise ValueError(
                f"status must be one of {sorted(_TERMINAL_STATUSES)}, got {status!r}"
            )
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
        """Remove non-pending entries older than ``max_age_days``.

        Pending entries are NEVER removed regardless of age.
        Returns the number of removed lines.
        """
        if not self._path.exists():
            return 0

        max_age_s = self._max_age_days * 86400.0
        now_monotonic = time.monotonic()
        now_utc = datetime.now(timezone.utc)

        # First pass: resolve effective status of every lease
        effective_statuses: Dict[str, str] = {}
        raw_lines: List[str] = []
        with self._path.open("r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                raw_lines.append(stripped)
                try:
                    record = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                lid = record.get("lease_id", "")
                if not lid:
                    continue
                if record.get("_type") == "status_update":
                    effective_statuses[lid] = record.get("status", "pending")
                elif "envelope" in record:
                    effective_statuses.setdefault(lid, record.get("status", "pending"))

        # Second pass: keep pending entries always; age-out others
        kept: List[str] = []
        removed = 0
        for stripped in raw_lines:
            try:
                record = json.loads(stripped)
            except json.JSONDecodeError:
                removed += 1
                continue
            lid = record.get("lease_id", "")
            effective = effective_statuses.get(lid, "pending")
            if effective == "pending":
                kept.append(stripped)
                continue
            # Use ts_utc for wall-clock age (stable across process restarts)
            ts_utc_str = record.get("ts_utc", "")
            try:
                entry_dt = datetime.fromisoformat(ts_utc_str.replace("Z", "+00:00"))
                age_s = (now_utc - entry_dt).total_seconds()
            except (ValueError, TypeError):
                # Fallback to ts_monotonic if ts_utc is missing/malformed
                ts_mono = record.get("ts_monotonic", now_monotonic)
                age_s = now_monotonic - ts_mono
            if age_s < max_age_s:
                kept.append(stripped)
            else:
                removed += 1

        tmp = self._path.with_suffix(".wal.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for line in kept:
                f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._path)

        return removed

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _write_line(self, record: Dict[str, Any]) -> None:
        with self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
            f.flush()
            os.fsync(f.fileno())
