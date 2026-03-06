"""UMF Dead Letter Queue -- centralized store for failed messages.

Messages that fail delivery after retry budget exhaustion are sent here.
No oscillation: poison messages are stored once (deduped by message_id).
Bounded with TTL compaction via ``cleanup()``.

Design rules
------------
* Stdlib only -- no third-party or JARVIS imports.
* File-based storage (JSON per entry) for durability.
* No oscillation: same message_id stored only once.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


class DeadLetterQueue:
    """Centralized DLQ for messages that failed delivery.

    Parameters
    ----------
    storage_dir:
        Directory where DLQ entries are stored as JSON files.
    max_age_s:
        Maximum age in seconds before entries are eligible for cleanup.
    """

    def __init__(
        self,
        storage_dir: Path,
        max_age_s: float = 86400.0,  # 24 hours default
    ) -> None:
        self._storage_dir = storage_dir
        self._max_age_s = max_age_s
        self._known_ids: set = set()

    def start(self) -> None:
        """Create storage directory and load existing entry IDs."""
        self._storage_dir.mkdir(parents=True, exist_ok=True)
        # Load existing message IDs to prevent oscillation
        for entry_file in self._storage_dir.glob("*.json"):
            try:
                data = json.loads(entry_file.read_text())
                self._known_ids.add(data.get("message_id", ""))
            except (json.JSONDecodeError, OSError):
                pass

    async def add(
        self,
        message_id: str,
        reason: str,
        payload: Dict[str, Any],
    ) -> bool:
        """Add a message to the DLQ. Returns False if already present (no oscillation)."""
        if message_id in self._known_ids:
            return False

        self._known_ids.add(message_id)
        entry = {
            "message_id": message_id,
            "reason": reason,
            "payload": payload,
            "added_at": time.time(),
        }
        entry_path = self._storage_dir / f"{message_id}.json"
        entry_path.write_text(json.dumps(entry, sort_keys=True))
        logger.warning(
            "[DLQ] Added message_id=%s reason=%s", message_id, reason,
        )
        return True

    def list_entries(self) -> List[Dict[str, Any]]:
        """Return all DLQ entries."""
        entries = []
        for entry_file in sorted(self._storage_dir.glob("*.json")):
            try:
                entries.append(json.loads(entry_file.read_text()))
            except (json.JSONDecodeError, OSError):
                pass
        return entries

    def cleanup(self) -> int:
        """Remove entries older than max_age_s. Returns count of removed entries."""
        now = time.time()
        removed = 0
        for entry_file in list(self._storage_dir.glob("*.json")):
            try:
                data = json.loads(entry_file.read_text())
                added_at = data.get("added_at", 0)
                if (now - added_at) > self._max_age_s:
                    entry_file.unlink()
                    self._known_ids.discard(data.get("message_id", ""))
                    removed += 1
            except (json.JSONDecodeError, OSError):
                pass
        return removed
