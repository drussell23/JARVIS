"""
session_log - every kernel run leaves its full output in a file.

Run through scripts/windows/jarvis.ps1, the kernel's output was redirected to
a log; run directly (``python3.11 unified_supervisor.py``) it went to the
console only, so whatever went wrong scrolled away with nothing to read
afterwards. When this process writes to an interactive console, its stdout
and stderr are teed into ``<log_dir>/<name>-<timestamp>.log`` -- the same
directory and naming the launcher uses, so ``jarvis.ps1 logs`` finds either.
When output is already going somewhere else (a file, a pipe), the caller
chose where it goes and nothing is added.

The console keeps everything it had: ``fileno``, ``isatty``, ``buffer`` and
``encoding`` are the console's, so child processes, faulthandler and rich
rendering behave exactly as before. The file gets the same text with
terminal escape sequences removed.
"""
from __future__ import annotations

import io
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional, TextIO

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\r(?!\n)")


class _Tee(io.TextIOBase):
    """A console stream that also appends what it shows to a log file."""

    def __init__(self, console: TextIO, log: TextIO) -> None:
        self._console = console
        self._log = log

    def write(self, text: str) -> int:
        n = self._console.write(text)
        self.record(text)
        return n if isinstance(n, int) else len(text)

    def record(self, text: str) -> None:
        """Append *text* to the log only (the console already has it)."""
        try:
            self._log.write(_ANSI.sub("", text))
        except (OSError, ValueError):  # log closed or disk full: the console still works
            pass

    @property
    def console(self) -> TextIO:
        """The console stream underneath the tee."""
        return self._console

    def flush(self) -> None:
        self._console.flush()
        try:
            self._log.flush()
        except (OSError, ValueError):
            pass

    def isatty(self) -> bool:
        return self._console.isatty()

    def fileno(self) -> int:
        return self._console.fileno()

    def writable(self) -> bool:
        return True

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return getattr(self._console, "encoding", "utf-8")

    @property
    def errors(self) -> Optional[str]:  # type: ignore[override]
        return getattr(self._console, "errors", None)

    @property
    def buffer(self):  # noqa: D401 -- binary writes go to the console only
        return self._console.buffer


def _keep() -> int:
    try:
        return max(1, int(os.environ.get("JARVIS_SESSION_LOG_KEEP", "20")))
    except ValueError:
        return 20


def _prune(directory: Path, name: str) -> None:
    logs = sorted(directory.glob(f"{name}-*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in logs[_keep():]:
        try:
            old.unlink()
        except OSError:
            pass


def console_of(stream: TextIO) -> TextIO:
    """The terminal under *stream*: a session-log tee's console, else itself.

    A writer that draws on the terminal itself -- a live region redrawn in
    place -- must not have every frame copied into the log.
    """
    return stream.console if isinstance(stream, _Tee) else stream


def record(stream: TextIO, text: str) -> None:
    """Append *text* to the session log behind *stream*, if it has one.

    For output that reaches the console another way (through a live region's
    console) but belongs in the log like any other line.
    """
    if isinstance(stream, _Tee):
        stream.record(text)


class SessionLogHandler(logging.Handler):
    """Every log record into the session log, whatever the terminal shows.

    The terminal is a VIEW (backend/core/console_view): it may show only
    warnings and the narrative while the log keeps every record. Records
    therefore reach the log through this handler and the terminal through
    its own handler on the bare console -- not through the tee, which would
    make the log only as complete as the terminal.
    """

    def __init__(self, tee: "_Tee", level: int = logging.NOTSET) -> None:
        super().__init__(level)
        self._tee = tee

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(record, "console_only", False):
            return  # a view action: an expansion re-shows what the log already has
        try:
            self._tee.record(self.format(record) + "\n")
        except Exception:  # noqa: BLE001 -- logging never raises into its caller
            self.handleError(record)


def session_log_handler(stream: TextIO, level: int = logging.NOTSET) -> Optional[logging.Handler]:
    """A handler writing records to the session log behind *stream*, or None
    when *stream* has no session log (output already goes where the caller
    sent it)."""
    return SessionLogHandler(stream, level) if isinstance(stream, _Tee) else None


_active: Optional[_Tee] = None


def log_only(text: str) -> None:
    """Append *text* to this run's session log without showing it -- a line
    the terminal's view leaves out. Independent of what ``sys.stdout`` is at
    the moment (a live region routes it through a proxy); a no-op when the
    run has no session log."""
    if _active is not None:
        _active.record(text if text.endswith("\n") else text + "\n")


def start_session_log(name: str) -> Optional[Path]:
    """Tee this process's console output to a new session log; return its path.

    Returns None when output is not an interactive console (it already goes
    where the caller sent it) or when no log can be opened -- a run is never
    refused for want of a log file.
    """
    if isinstance(sys.stdout, _Tee) or not (sys.stdout and sys.stdout.isatty()):
        return None
    from backend.core.host_platform import log_dir

    directory = log_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{name}-{time.strftime('%Y%m%d-%H%M%S')}.log"
        log = open(path, "a", encoding="utf-8", errors="replace", buffering=1)  # noqa: SIM115
    except OSError as exc:
        print(f"[KERNEL] session log unavailable ({exc}); console only", file=sys.stderr)
        return None
    global _active
    sys.stdout = _active = _Tee(sys.stdout, log)
    sys.stderr = _Tee(sys.stderr, log)
    _prune(directory, name)
    print(f"[KERNEL] Session log: {path}")
    return path


__all__ = ["SessionLogHandler", "console_of", "log_only", "record", "session_log_handler",
           "start_session_log"]
