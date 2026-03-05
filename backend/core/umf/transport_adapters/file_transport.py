"""File-based transport adapter for UMF messages.

Provides durable, file-system-backed message delivery with atomic writes
(write-to-tmp then ``os.rename``).  Each stream maps to a subdirectory
under ``base_dir``; messages are stored as individual JSON files whose
names sort lexicographically by observed timestamp.

Filename format::

    {observed_at_unix_ms:015d}_{message_id}.json

Design rules
------------
* **No** third-party or JARVIS imports -- stdlib + ``backend.core.umf.types`` only.
* Fully async interface (``asyncio``).
* Atomic writes via ``os.rename`` (POSIX guarantees rename is atomic on the
  same filesystem).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import AsyncIterator, Set

from backend.core.umf.types import UmfMessage

logger = logging.getLogger(__name__)


class FileTransport:
    """File-system transport that stores each UMF message as a JSON file.

    Parameters
    ----------
    base_dir:
        Root directory.  Each stream gets its own subdirectory.
    cleanup_age_s:
        Age in seconds after which files are eligible for cleanup.
        Defaults to 86 400 s (24 hours).
    """

    def __init__(self, base_dir: Path, cleanup_age_s: float = 86400.0) -> None:
        self._base_dir = Path(base_dir)
        self._cleanup_age_s = cleanup_age_s
        self._running: bool = False
        self._processed: Set[str] = set()

    # ── lifecycle ────────────────────────────────────────────────────

    async def start(self) -> None:
        """Create the base directory (parents included) and mark as running."""
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._running = True
        logger.info("FileTransport started: %s", self._base_dir)

    async def stop(self) -> None:
        """Mark the transport as stopped."""
        self._running = False
        logger.info("FileTransport stopped: %s", self._base_dir)

    def is_connected(self) -> bool:
        """Return ``True`` when the transport is running."""
        return self._running

    # ── send ─────────────────────────────────────────────────────────

    async def send(self, msg: UmfMessage) -> bool:
        """Atomically write a message to the stream subdirectory.

        1. Ensure ``base_dir/<stream>/`` exists.
        2. Write JSON to a ``.tmp`` file.
        3. ``os.rename`` the temp file to the final ``.json`` path.

        Returns ``True`` on success, ``False`` on any error.
        """
        try:
            stream_name: str = msg.stream.value
            stream_dir = self._base_dir / stream_name
            stream_dir.mkdir(parents=True, exist_ok=True)

            filename = f"{msg.observed_at_unix_ms:015d}_{msg.message_id}.json"
            final_path = stream_dir / filename
            tmp_path = stream_dir / f"{filename}.tmp"

            data = msg.to_json()

            # Write to temp file, then atomic rename
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._write_atomic, tmp_path, final_path, data)

            return True
        except Exception:
            logger.exception("FileTransport.send failed")
            return False

    @staticmethod
    def _write_atomic(tmp_path: Path, final_path: Path, data: str) -> None:
        """Blocking helper: write data to *tmp_path*, then rename to *final_path*."""
        tmp_path.write_text(data, encoding="utf-8")
        os.rename(str(tmp_path), str(final_path))

    # ── receive ──────────────────────────────────────────────────────

    async def receive(
        self,
        stream: str,
        timeout_s: float = 0.0,
        poll_interval_s: float = 0.1,
    ) -> AsyncIterator[UmfMessage]:
        """Yield messages from a stream directory, sorted by filename.

        Already-processed filenames are skipped.  If ``timeout_s > 0``,
        the iterator polls (with ``poll_interval_s`` sleeps) until the
        deadline, yielding new messages as they appear.
        """
        stream_dir = self._base_dir / stream

        if timeout_s <= 0:
            # Single-pass scan
            async for msg in self._scan_dir(stream_dir):
                yield msg
            return

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            found_any = False
            async for msg in self._scan_dir(stream_dir):
                found_any = True
                yield msg
            if not found_any:
                await asyncio.sleep(poll_interval_s)

    async def _scan_dir(self, stream_dir: Path) -> AsyncIterator[UmfMessage]:
        """Scan *stream_dir* for new ``.json`` files and yield parsed messages."""
        if not stream_dir.is_dir():
            return

        files = sorted(stream_dir.glob("*.json"))
        for fpath in files:
            fname = fpath.name
            if fname in self._processed:
                continue
            try:
                raw = fpath.read_text(encoding="utf-8")
                d = json.loads(raw)
                msg = UmfMessage.from_dict(d)
                self._processed.add(fname)
                yield msg
            except Exception:
                logger.exception("FileTransport: failed to parse %s", fpath)

    # ── cleanup ──────────────────────────────────────────────────────

    async def cleanup(self) -> int:
        """Remove files older than ``cleanup_age_s``.  Returns count of removed files."""
        removed = 0
        now = time.time()
        cutoff = now - self._cleanup_age_s

        if not self._base_dir.is_dir():
            return 0

        for stream_dir in self._base_dir.iterdir():
            if not stream_dir.is_dir():
                continue
            for fpath in stream_dir.iterdir():
                if not fpath.is_file():
                    continue
                try:
                    mtime = fpath.stat().st_mtime
                    if mtime < cutoff:
                        fpath.unlink()
                        removed += 1
                except Exception:
                    logger.exception("FileTransport.cleanup: failed to remove %s", fpath)

        return removed
