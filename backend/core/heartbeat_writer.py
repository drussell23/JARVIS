"""
v310.0: File-based heartbeat writer for supervisor liveness detection.

The supervisor event loop writes a heartbeat JSON file every ~10s.
An external watcher (launchd, cron, or sister process) can read the file
and detect staleness — proving the event loop is actually ticking,
not just that the PID is alive.

Atomic write protocol: write to .tmp -> fsync -> os.replace (rename).
This guarantees readers always see a complete, valid JSON payload.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional


class HeartbeatWriter:
    """Writes an atomic JSON heartbeat file each time ``write()`` is called."""

    def __init__(self, path: Optional[Path] = None) -> None:
        if path is None:
            path = Path.home() / ".jarvis" / "heartbeat.json"
        self.path: Path = Path(path)
        self.boot_id: str = str(uuid.uuid4())
        self._start_mono: float = time.monotonic()

    # ------------------------------------------------------------------
    def write(self, phase: str, loop_iteration: int) -> None:
        """Write heartbeat payload atomically via tmp+fsync+replace."""
        now_mono = time.monotonic()
        payload: Dict[str, Any] = {
            "boot_id": self.boot_id,
            "pid": os.getpid(),
            "ts_mono": now_mono,
            "monotonic_age_ms": int((now_mono - self._start_mono) * 1000),
            "phase": phase,
            "loop_iteration": loop_iteration,
            "written_at_wall": datetime.now().isoformat(timespec="seconds"),
        }

        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")

        # Ensure parent directory exists
        self.path.parent.mkdir(parents=True, exist_ok=True)

        tmp = self.path.with_suffix(".tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(str(tmp), str(self.path))


# ======================================================================
# Validation helper — used by external watchers / tests
# ======================================================================

def validate_heartbeat(
    payload: Dict[str, Any],
    *,
    expected_boot_id: Optional[str] = None,
    expected_pid: Optional[int] = None,
    max_age_s: Optional[float] = None,
) -> Dict[str, Any]:
    """Validate a heartbeat payload for liveness.

    Returns ``{"valid": True/False, "reason": "<explanation>"}``
    """
    # --- boot_id check ---
    if expected_boot_id is not None:
        if payload.get("boot_id") != expected_boot_id:
            return {
                "valid": False,
                "reason": (
                    f"boot_id mismatch: expected {expected_boot_id!r}, "
                    f"got {payload.get('boot_id')!r}"
                ),
            }

    # --- pid check ---
    pid = payload.get("pid")
    if expected_pid is not None:
        if pid != expected_pid:
            return {
                "valid": False,
                "reason": (
                    f"pid mismatch: expected {expected_pid}, got {pid}"
                ),
            }
    elif pid is not None:
        # Even without an expected pid, verify the process is alive
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return {
                "valid": False,
                "reason": f"pid {pid} is not running",
            }
        except PermissionError:
            pass  # Process exists but we lack permission — that's fine

    # --- staleness check (monotonic age) ---
    if max_age_s is not None:
        age_ms = payload.get("monotonic_age_ms")
        ts_mono = payload.get("ts_mono")
        if ts_mono is not None:
            # Compare against current monotonic clock
            elapsed_since_write = time.monotonic() - ts_mono
            if elapsed_since_write > max_age_s:
                return {
                    "valid": False,
                    "reason": (
                        f"heartbeat is stale: {elapsed_since_write:.1f}s "
                        f"since last write (max {max_age_s}s)"
                    ),
                }

    return {"valid": True, "reason": "all checks passed"}
