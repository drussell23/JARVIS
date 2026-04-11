"""backend/core/ouroboros/governance/comms/ops_logger.py

Append-only ops log writer. Writes human-readable pipeline narratives
to daily log files at ~/.jarvis/ops/YYYY-MM-DD-ops.log.

Design ref: docs/plans/2026-03-07-autonomous-layers-design.md §4
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from backend.core.ouroboros.governance.comm_protocol import CommMessage
from backend.core.ouroboros.governance.sandbox_paths import sandbox_fallback

logger = logging.getLogger(__name__)

_DEFAULT_LOG_DIR = Path.home() / ".jarvis" / "ops"


class OpsLogger:
    """CommProtocol transport that writes human-readable ops logs."""

    def __init__(
        self,
        log_dir: Optional[Path] = None,
        retention_days: int = 30,
    ) -> None:
        _primary = Path(
            log_dir
            or os.environ.get("JARVIS_OPS_LOG_DIR", str(_DEFAULT_LOG_DIR))
        )
        # Iron Gate compliance: route around PermissionError, don't lower shields.
        self._log_dir = sandbox_fallback(_primary)
        self._retention_days = int(
            os.environ.get("JARVIS_OPS_LOG_RETENTION_DAYS", str(retention_days))
        )

    async def send(self, msg: CommMessage) -> None:
        """CommProtocol transport interface. Appends entry to daily log."""
        try:
            self._log_dir.mkdir(parents=True, exist_ok=True)
            log_path = self._log_dir / self._daily_filename()
            entry = self._format_entry(msg)
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(entry)
        except Exception:
            logger.debug("OpsLogger: failed to write for op %s", msg.op_id, exc_info=True)

    async def cleanup_old_logs(self) -> None:
        """Remove log files older than retention_days."""
        if not self._log_dir.exists():
            return
        cutoff = time.time() - (self._retention_days * 86400)
        for log_file in self._log_dir.glob("*-ops.log"):
            try:
                if log_file.stat().st_mtime < cutoff:
                    log_file.unlink()
            except Exception:
                logger.debug("OpsLogger: failed to remove %s", log_file)

    @staticmethod
    def _daily_filename() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d-ops.log")

    @staticmethod
    def _format_entry(msg: CommMessage) -> str:
        _ts = getattr(msg, "timestamp", None) or time.time()
        ts = datetime.fromtimestamp(_ts, tz=timezone.utc)
        ts_str = ts.strftime("%Y-%m-%d %H:%M:%S")
        _msg_type = getattr(msg, "msg_type", None)
        msg_type = getattr(_msg_type, "name", getattr(_msg_type, "value", "UNKNOWN"))

        lines = [f"[{ts_str}] {msg_type}  {msg.op_id}"]

        payload = msg.payload
        # Add key payload fields on indented lines
        for key in ("goal", "target_files", "outcome", "reason_code",
                     "root_cause", "failed_phase", "next_safe_action",
                     "risk_tier", "blast_radius", "steps",
                     "phase", "progress_pct", "diff_summary"):
            if key in payload:
                val = payload[key]
                if isinstance(val, (list, tuple)):
                    val = ", ".join(str(v) for v in val)
                lines.append(f"    {key}: {val}")

        return "\n".join(lines) + "\n\n"
