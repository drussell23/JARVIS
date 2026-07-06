"""Asynchronous headless telemetry — non-blocking rotating log pipeline.

Root cause fixed: the observation apparatus. Session bt-iso-1782987919
died to SIGTERM after Terminal scrollback ballooned to 67 GB; separately,
the legacy logging.FileHandler does disk I/O on the emitting thread —
i.e., on the asyncio event loop. This module provides:

  * NonBlockingQueueHandler — QueueHandler -> daemon QueueListener ->
    RotatingFileHandler. Emitters enqueue (lock-free SimpleQueue.put)
    and return; disk I/O happens on the listener thread. Size-based
    rotation guards disk exhaustion.
  * HeartbeatConsoleHandler — the ONLY terminal surface: a rate-limited
    single updating line (carriage-return rewrite, no scrollback).

Consumed by silent_boot._configure_locked (the existing console-muting
organ). Same defensive contract: NEVER raises; boot is not blocked by
logging glue. Authority-free.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import queue
import sys
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_FLAG_MASTER = "JARVIS_HEADLESS_TELEMETRY_ENABLED"
_FLAG_MAX_BYTES = "JARVIS_TELEMETRY_LOG_MAX_BYTES"
_FLAG_BACKUPS = "JARVIS_TELEMETRY_LOG_BACKUPS"
_FLAG_LOG_DIR = "JARVIS_TELEMETRY_LOG_DIR"
_FLAG_HB_ENABLED = "JARVIS_CONSOLE_HEARTBEAT_ENABLED"
_FLAG_HB_INTERVAL = "JARVIS_CONSOLE_HEARTBEAT_INTERVAL_S"
_FLAG_HB_WIDTH = "JARVIS_CONSOLE_HEARTBEAT_WIDTH"

_DEFAULT_MAX_BYTES = 100 * 1024 * 1024  # 100 MB chunks (expanded for A1 diagnostic completeness)
_DEFAULT_BACKUPS = 20  # 20 backups × 100 MB = 2 GB total capacity
_DEFAULT_LOG_DIR = os.path.join(".ouroboros", "logs")
_DEFAULT_HB_INTERVAL_S = 30.0
_DEFAULT_HB_WIDTH = 120


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default


def is_enabled() -> bool:
    return _env_bool(_FLAG_MASTER, True)


def resolve_telemetry_path(session_dir: Optional[Any], filename: str) -> Path:
    """Session dir wins (auditor contract: debug.log colocates with the
    session artifacts); otherwise the global env-driven telemetry dir."""
    if session_dir is not None:
        return Path(session_dir) / filename
    return Path(os.getenv(_FLAG_LOG_DIR, "") or _DEFAULT_LOG_DIR) / filename


class NonBlockingQueueHandler(logging.handlers.QueueHandler):
    """QueueHandler bound to its own QueueListener + RotatingFileHandler.

    Exposes .baseFilename (harness contract) and .close() that tears the
    whole pipeline down (stop listener -> drain -> close file handler).
    """

    def __init__(
        self,
        record_queue: "queue.SimpleQueue",
        listener: logging.handlers.QueueListener,
        file_handler: logging.handlers.RotatingFileHandler,
    ) -> None:
        super().__init__(record_queue)
        self.listener = listener
        self._file_handler = file_handler
        self.baseFilename = file_handler.baseFilename

    def close(self) -> None:  # noqa: D102 — logging.Handler override
        try:
            self.listener.stop()  # joins the daemon thread, drains queue
        except Exception:  # noqa: BLE001 — teardown is best-effort
            pass
        try:
            self._file_handler.close()
        except Exception:  # noqa: BLE001
            pass
        super().close()


def build_nonblocking_handler(
    log_path: Any, formatter: logging.Formatter, level: int,
) -> NonBlockingQueueHandler:
    """Assemble queue -> listener -> rotating sink. Raises on failure —
    the CALLER (silent_boot) owns the fail-soft fallback decision."""
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        str(path),
        maxBytes=_env_int(_FLAG_MAX_BYTES, _DEFAULT_MAX_BYTES),
        backupCount=_env_int(_FLAG_BACKUPS, _DEFAULT_BACKUPS),
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    record_queue: "queue.SimpleQueue" = queue.SimpleQueue()
    listener = logging.handlers.QueueListener(
        record_queue, file_handler, respect_handler_level=True,
    )
    listener.start()  # daemon thread — never blocks interpreter exit
    handler = NonBlockingQueueHandler(record_queue, listener, file_handler)
    handler.setLevel(level)
    return handler


class HeartbeatConsoleHandler(logging.Handler):
    """Single updating line on the real TTY, at most once per interval.

    Eliminates scrollback bloat structurally: carriage-return rewrite,
    never a newline. No TTY -> silent no-op. Never raises.
    """

    def __init__(self, stream: Optional[Any] = None) -> None:
        super().__init__(level=logging.NOTSET)
        self._stream = stream if stream is not None else sys.__stdout__
        self._last_emit = 0.0
        self._count = 0

    @staticmethod
    def _now() -> float:
        return time.monotonic()

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        try:
            self._count += 1
            if not _env_bool(_FLAG_HB_ENABLED, True):
                return
            stream = self._stream
            if stream is None or not getattr(stream, "isatty", lambda: False)():
                return
            now = self._now()
            interval = _env_float(_FLAG_HB_INTERVAL, _DEFAULT_HB_INTERVAL_S)
            if self._last_emit and (now - self._last_emit) < interval:
                return
            self._last_emit = now
            width = _env_int(_FLAG_HB_WIDTH, _DEFAULT_HB_WIDTH)
            line = "\r⏳ %s · %d records · last: %s %s" % (
                time.strftime("%H:%M:%S"),
                self._count,
                record.levelname,
                record.name,
            )
            stream.write(line[:width].ljust(width))
            stream.flush()
        except Exception:  # noqa: BLE001 — a heartbeat must never wound
            pass


def register_flags(registry: Any) -> int:
    """FlagRegistry seed hook (auto-discovered convention)."""
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            Category, FlagSpec, FlagType,
        )
    except Exception:  # noqa: BLE001
        return 0
    src = "backend/core/ouroboros/governance/headless_telemetry.py"
    seeds = [
        FlagSpec(name=_FLAG_MASTER, type=FlagType.BOOL, default=True,
                 category=Category.OBSERVABILITY, source_file=src,
                 description="Non-blocking rotating telemetry pipeline "
                             "(QueueListener + RotatingFileHandler). "
                             "=false restores legacy blocking FileHandler.",
                 example="JARVIS_HEADLESS_TELEMETRY_ENABLED=false"),
        FlagSpec(name=_FLAG_MAX_BYTES, type=FlagType.INT,
                 default=_DEFAULT_MAX_BYTES,
                 category=Category.OBSERVABILITY, source_file=src,
                 description="Rotation chunk size for the telemetry sink.",
                 example="JARVIS_TELEMETRY_LOG_MAX_BYTES=52428800"),
        FlagSpec(name=_FLAG_BACKUPS, type=FlagType.INT, default=_DEFAULT_BACKUPS,
                 category=Category.OBSERVABILITY, source_file=src,
                 description="Rotated chunk retention count (disk-exhaustion guard).",
                 example="JARVIS_TELEMETRY_LOG_BACKUPS=10"),
        FlagSpec(name=_FLAG_LOG_DIR, type=FlagType.STR, default=_DEFAULT_LOG_DIR,
                 category=Category.OBSERVABILITY, source_file=src,
                 description="Global telemetry dir when no session dir exists.",
                 example="JARVIS_TELEMETRY_LOG_DIR=.ouroboros/logs"),
        FlagSpec(name=_FLAG_HB_ENABLED, type=FlagType.BOOL, default=True,
                 category=Category.OBSERVABILITY, source_file=src,
                 description="Single-line console heartbeat (TTY only).",
                 example="JARVIS_CONSOLE_HEARTBEAT_ENABLED=false"),
        FlagSpec(name=_FLAG_HB_INTERVAL, type=FlagType.FLOAT,
                 default=_DEFAULT_HB_INTERVAL_S,
                 category=Category.OBSERVABILITY, source_file=src,
                 description="Minimum seconds between heartbeat line rewrites.",
                 example="JARVIS_CONSOLE_HEARTBEAT_INTERVAL_S=30"),
        FlagSpec(name=_FLAG_HB_WIDTH, type=FlagType.INT, default=_DEFAULT_HB_WIDTH,
                 category=Category.OBSERVABILITY, source_file=src,
                 description="Heartbeat line clamp width (columns).",
                 example="JARVIS_CONSOLE_HEARTBEAT_WIDTH=120"),
    ]
    registry.bulk_register(seeds)
    return len(seeds)
