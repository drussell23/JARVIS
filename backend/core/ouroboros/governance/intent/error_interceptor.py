"""ErrorInterceptor — capture ERROR/CRITICAL log records as IntentSignals.

Installs a custom :class:`logging.Handler` on any logger to intercept
ERROR and CRITICAL records.  Each qualifying record is converted into an
observe-only :class:`IntentSignal` (``stable=False``) and dispatched via
the ``on_signal`` callback.

This is Phase 1.5 of the Intent Engine — narrate only, no auto-submit.
Signals carry evidence including the logger name, log level, source file,
line number, first 200 characters of the message (as a dedup signature),
and the formatted traceback when an exception is attached.

Usage::

    interceptor = ErrorInterceptor(repo="jarvis")
    interceptor.on_signal = my_callback  # (IntentSignal) -> None
    interceptor.install(logging.getLogger("my.module"))

    # later…
    interceptor.uninstall(logging.getLogger("my.module"))
"""
from __future__ import annotations

import logging
import traceback as tb_module
from typing import Callable, Optional

from .signals import IntentSignal


# ---------------------------------------------------------------------------
# ErrorInterceptor (defined first so _InterceptHandler can reference it)
# ---------------------------------------------------------------------------


class ErrorInterceptor:
    """Intercepts ERROR/CRITICAL log records and emits observe-only IntentSignals.

    Parameters
    ----------
    repo:
        Repository origin string embedded in emitted signals.
        Defaults to ``"jarvis"``.
    """

    def __init__(self, repo: str = "jarvis") -> None:
        self._repo = repo
        self.on_signal: Optional[Callable[[IntentSignal], None]] = None
        self._handlers: dict[int, _InterceptHandler] = {}

    # ------------------------------------------------------------------
    # Install / Uninstall
    # ------------------------------------------------------------------

    def install(self, logger: logging.Logger) -> None:
        """Add an :class:`_InterceptHandler` to *logger*.

        The handler only fires for ``ERROR`` and above.  Multiple calls
        with the same logger are idempotent — only one handler is added.
        """
        logger_id = id(logger)
        if logger_id in self._handlers:
            return  # Already installed on this logger
        handler = _InterceptHandler(self)
        self._handlers[logger_id] = handler
        logger.addHandler(handler)

    def uninstall(self, logger: logging.Logger) -> None:
        """Remove the previously installed handler from *logger*.

        No-op if this interceptor was never installed on the logger.
        """
        logger_id = id(logger)
        handler = self._handlers.pop(logger_id, None)
        if handler is not None:
            logger.removeHandler(handler)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _handle_record(self, record: logging.LogRecord) -> None:
        """Convert a log record into an :class:`IntentSignal` and dispatch it.

        Called by :class:`_InterceptHandler.emit`.  If ``on_signal`` is
        ``None``, the record is silently ignored (interceptor is inactive).
        """
        if self.on_signal is None:
            return

        source_file: str = record.pathname
        line_no: int = record.lineno
        signature: str = record.getMessage()[:200]

        evidence: dict = {
            "logger_name": record.name,
            "level": record.levelname,
            "source_file": source_file,
            "line_no": line_no,
            "signature": signature,
        }

        # Attach formatted traceback when an exception is present
        if record.exc_info and record.exc_info[0] is not None:
            evidence["traceback"] = "".join(
                tb_module.format_exception(*record.exc_info)
            )

        confidence: float = 0.85 if record.levelno >= logging.CRITICAL else 0.7

        signal = IntentSignal(
            source="intent:stack_trace",
            target_files=(source_file,),
            repo=self._repo,
            description=f"{record.levelname} in {record.name}: {signature}",
            evidence=evidence,
            confidence=confidence,
            stable=False,
        )

        self.on_signal(signal)


# ---------------------------------------------------------------------------
# _InterceptHandler (defined after ErrorInterceptor to avoid forward ref)
# ---------------------------------------------------------------------------


class _InterceptHandler(logging.Handler):
    """Internal logging handler that forwards ERROR+ records to an :class:`ErrorInterceptor`."""

    def __init__(self, interceptor: ErrorInterceptor) -> None:
        super().__init__(level=logging.ERROR)
        self._interceptor = interceptor

    def emit(self, record: logging.LogRecord) -> None:
        """Forward the record to the interceptor for signal emission."""
        self._interceptor._handle_record(record)
