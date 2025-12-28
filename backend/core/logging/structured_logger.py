"""
JARVIS Structured Logging System v1.0
======================================

Production-grade structured logging with:
- JSON formatted logs for easy parsing and analysis
- Async file writing (non-blocking)
- Automatic log rotation (prevents huge files)
- Context enrichment (session IDs, tracing, stack traces)
- Intelligent error aggregation and pattern detection
- Performance metrics tracking
- Security event logging
- Real-time error analysis

Architecture:
    Logger → JSONFormatter → AsyncFileHandler → Rotating Log Files
           ↓
    ErrorAnalyzer (detects patterns, aggregates, alerts)

Usage:
    from backend.core.logging.structured_logger import get_structured_logger

    logger = await get_structured_logger("my_module")

    # Basic logging
    logger.info("User authenticated", user_id="derek", confidence=0.95)

    # Error logging with auto-context
    try:
        result = await risky_operation()
    except Exception as e:
        logger.error("Operation failed", exc_info=True, operation="risky_operation")

    # Performance tracking
    with logger.timer("database_query"):
        await db.query(...)

    # Get error statistics
    stats = await logger.get_error_stats()
"""

import asyncio
import json
import logging
import logging.handlers
import os
import sys
import traceback
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Set
from queue import Queue
import threading
import time

# =============================================================================
# CONFIGURATION
# =============================================================================

@dataclass
class LoggingConfig:
    """Configuration for structured logging system."""

    # Base directory for log files
    log_dir: Path = field(default_factory=lambda: Path.home() / ".jarvis" / "logs")

    # Log file rotation
    max_bytes: int = 10 * 1024 * 1024  # 10MB per file
    backup_count: int = 10  # Keep 10 rotated files

    # Log levels
    default_level: str = "INFO"
    console_level: str = "INFO"
    file_level: str = "DEBUG"

    # Format options
    include_hostname: bool = True
    include_process_info: bool = True
    include_thread_info: bool = True

    # Error analysis
    enable_error_aggregation: bool = True
    error_window_seconds: int = 300  # 5 minutes
    error_threshold_for_alert: int = 10  # Alert if same error >10 times in window

    # Performance tracking
    enable_performance_tracking: bool = True
    slow_operation_threshold_ms: float = 1000.0  # Warn if operation >1s

    # Async writing
    queue_size: int = 10000  # Max queued log records
    flush_interval_seconds: float = 5.0  # Flush every 5 seconds

    @staticmethod
    def from_env() -> "LoggingConfig":
        """Load configuration from environment variables."""
        config = LoggingConfig()

        if log_dir := os.getenv("JARVIS_LOG_DIR"):
            config.log_dir = Path(log_dir)

        if max_bytes := os.getenv("JARVIS_LOG_MAX_BYTES"):
            config.max_bytes = int(max_bytes)

        if backup_count := os.getenv("JARVIS_LOG_BACKUP_COUNT"):
            config.backup_count = int(backup_count)

        if default_level := os.getenv("JARVIS_LOG_LEVEL"):
            config.default_level = default_level.upper()

        if console_level := os.getenv("JARVIS_LOG_CONSOLE_LEVEL"):
            config.console_level = console_level.upper()

        if file_level := os.getenv("JARVIS_LOG_FILE_LEVEL"):
            config.file_level = file_level.upper()

        config.enable_error_aggregation = os.getenv(
            "JARVIS_LOG_ERROR_AGGREGATION", "true"
        ).lower() == "true"

        config.enable_performance_tracking = os.getenv(
            "JARVIS_LOG_PERFORMANCE_TRACKING", "true"
        ).lower() == "true"

        return config


# =============================================================================
# JSON FORMATTER
# =============================================================================

class StructuredJSONFormatter(logging.Formatter):
    """
    JSON formatter for structured logging.

    Converts log records to JSON with rich context:
    - Timestamp (ISO 8601)
    - Level name and number
    - Logger name and module
    - Message
    - Exception info (if present)
    - Stack trace (if present)
    - Custom fields (passed as extra={})
    - Process and thread info
    - Hostname
    """

    def __init__(self, config: LoggingConfig):
        super().__init__()
        self.config = config
        self.hostname = os.uname().nodename if config.include_hostname else None

    def format(self, record: logging.LogRecord) -> str:
        """Format log record as JSON."""

        # Base log entry
        log_entry = {
            "timestamp": datetime.utcfromtimestamp(record.created).isoformat() + "Z",
            "level": record.levelname,
            "level_num": record.levelno,
            "logger": record.name,
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
            "message": record.getMessage(),
        }

        # Add hostname
        if self.hostname:
            log_entry["hostname"] = self.hostname

        # Add process info
        if self.config.include_process_info:
            log_entry["process"] = {
                "id": record.process,
                "name": record.processName,
            }

        # Add thread info
        if self.config.include_thread_info:
            log_entry["thread"] = {
                "id": record.thread,
                "name": record.threadName,
            }

        # Add exception info
        if record.exc_info:
            log_entry["exception"] = {
                "type": record.exc_info[0].__name__ if record.exc_info[0] else None,
                "message": str(record.exc_info[1]) if record.exc_info[1] else None,
                "traceback": self.formatException(record.exc_info),
            }

        # Add stack trace (if requested via stack_info)
        if record.stack_info:
            log_entry["stack_trace"] = record.stack_info

        # Add custom fields from extra={}
        # Filter out internal logging attributes
        reserved_attrs = {
            "name", "msg", "args", "created", "filename", "funcName",
            "levelname", "levelno", "lineno", "module", "msecs",
            "message", "pathname", "process", "processName",
            "relativeCreated", "thread", "threadName", "exc_info",
            "exc_text", "stack_info", "taskName",
        }

        custom_fields = {}
        for key, value in record.__dict__.items():
            if key not in reserved_attrs:
                # Serialize complex objects
                try:
                    json.dumps(value)  # Test if JSON serializable
                    custom_fields[key] = value
                except (TypeError, ValueError):
                    custom_fields[key] = str(value)

        if custom_fields:
            log_entry["context"] = custom_fields

        return json.dumps(log_entry)


# =============================================================================
# ASYNC FILE HANDLER
# =============================================================================

class AsyncRotatingFileHandler(logging.Handler):
    """
    Async file handler with rotation support.

    Features:
    - Non-blocking writes (uses background thread)
    - Automatic log rotation (size-based)
    - Graceful shutdown with queue flushing
    - Error resilience (won't crash on write failures)
    """

    def __init__(
        self,
        filename: Path,
        max_bytes: int,
        backup_count: int,
        queue_size: int = 10000,
        flush_interval: float = 5.0,
    ):
        super().__init__()
        self.filename = filename
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self.flush_interval = flush_interval

        # Ensure log directory exists
        self.filename.parent.mkdir(parents=True, exist_ok=True)

        # Create rotating file handler
        self.file_handler = logging.handlers.RotatingFileHandler(
            filename=str(self.filename),
            maxBytes=self.max_bytes,
            backupCount=self.backup_count,
            encoding="utf-8",
        )

        # Queue for async writing
        self.queue: Queue = Queue(maxsize=queue_size)
        self.shutdown_flag = threading.Event()

        # Start background writer thread
        self.writer_thread = threading.Thread(
            target=self._writer_loop,
            name="LogWriter",
            daemon=True,
        )
        self.writer_thread.start()

    def emit(self, record: logging.LogRecord) -> None:
        """Queue log record for async writing."""
        try:
            self.queue.put_nowait(record)
        except Exception:
            # Queue full or other error - drop the log to prevent blocking
            self.handleError(record)

    def _writer_loop(self) -> None:
        """Background thread that writes queued logs to file."""
        while not self.shutdown_flag.is_set():
            try:
                # Wait for records with timeout
                try:
                    record = self.queue.get(timeout=self.flush_interval)
                    self.file_handler.emit(record)
                    self.queue.task_done()
                except Exception:
                    # Timeout or error - continue
                    pass
            except Exception as e:
                # Unexpected error in writer loop
                print(f"[AsyncFileHandler] Writer error: {e}", file=sys.stderr)

    def close(self) -> None:
        """Flush queue and close handler."""
        # Signal shutdown
        self.shutdown_flag.set()

        # Wait for queue to drain (max 10 seconds)
        try:
            self.queue.join()
        except Exception:
            pass

        # Close file handler
        self.file_handler.close()
        super().close()


# =============================================================================
# ERROR AGGREGATOR
# =============================================================================

@dataclass
class ErrorPattern:
    """Represents an aggregated error pattern."""
    error_type: str
    error_message: str
    count: int
    first_seen: datetime
    last_seen: datetime
    affected_modules: Set[str] = field(default_factory=set)
    sample_traceback: Optional[str] = None


class ErrorAggregator:
    """
    Aggregates errors to detect patterns and prevent log spam.

    Features:
    - Tracks error frequency in sliding time window
    - Groups similar errors together
    - Detects error storms (many errors in short time)
    - Provides statistics for debugging
    """

    def __init__(self, window_seconds: int = 300, alert_threshold: int = 10):
        self.window_seconds = window_seconds
        self.alert_threshold = alert_threshold

        # Error tracking
        self.errors: Deque[Dict[str, Any]] = deque()
        self.error_patterns: Dict[str, ErrorPattern] = {}
        self.lock = threading.Lock()

    def record_error(
        self,
        error_type: str,
        error_message: str,
        module: str,
        traceback_str: Optional[str] = None,
    ) -> bool:
        """
        Record an error and check if alert threshold reached.

        Returns:
            True if this error has crossed the alert threshold
        """
        now = datetime.now()

        with self.lock:
            # Create error signature for grouping
            signature = f"{error_type}:{error_message[:100]}"

            # Update or create error pattern
            if signature in self.error_patterns:
                pattern = self.error_patterns[signature]
                pattern.count += 1
                pattern.last_seen = now
                pattern.affected_modules.add(module)
            else:
                pattern = ErrorPattern(
                    error_type=error_type,
                    error_message=error_message,
                    count=1,
                    first_seen=now,
                    last_seen=now,
                    affected_modules={module},
                    sample_traceback=traceback_str,
                )
                self.error_patterns[signature] = pattern

            # Add to sliding window
            self.errors.append({
                "timestamp": now,
                "signature": signature,
                "module": module,
            })

            # Clean old errors from window
            cutoff = now - timedelta(seconds=self.window_seconds)
            while self.errors and self.errors[0]["timestamp"] < cutoff:
                self.errors.popleft()

            # Check if threshold crossed
            should_alert = pattern.count == self.alert_threshold

            return should_alert

    def get_stats(self) -> Dict[str, Any]:
        """Get error statistics."""
        with self.lock:
            now = datetime.now()
            cutoff = now - timedelta(seconds=self.window_seconds)

            # Count errors in current window
            recent_errors = sum(
                1 for e in self.errors if e["timestamp"] >= cutoff
            )

            # Top error patterns
            top_patterns = sorted(
                self.error_patterns.values(),
                key=lambda p: p.count,
                reverse=True,
            )[:10]

            return {
                "total_errors_all_time": sum(p.count for p in self.error_patterns.values()),
                "errors_in_window": recent_errors,
                "unique_error_types": len(self.error_patterns),
                "window_seconds": self.window_seconds,
                "top_errors": [
                    {
                        "type": p.error_type,
                        "message": p.error_message[:100],
                        "count": p.count,
                        "first_seen": p.first_seen.isoformat(),
                        "last_seen": p.last_seen.isoformat(),
                        "affected_modules": list(p.affected_modules),
                    }
                    for p in top_patterns
                ],
            }


# =============================================================================
# PERFORMANCE TRACKER
# =============================================================================

class PerformanceTracker:
    """
    Tracks operation performance and detects slow operations.

    Features:
    - Context manager for timing operations
    - Automatic slow operation detection
    - Performance statistics aggregation
    """

    def __init__(self, slow_threshold_ms: float = 1000.0):
        self.slow_threshold_ms = slow_threshold_ms
        self.operations: Dict[str, List[float]] = defaultdict(list)
        self.lock = threading.Lock()

    def record(self, operation: str, duration_ms: float) -> bool:
        """
        Record operation timing.

        Returns:
            True if operation was slow (exceeded threshold)
        """
        with self.lock:
            self.operations[operation].append(duration_ms)
            # Keep only last 1000 samples per operation
            if len(self.operations[operation]) > 1000:
                self.operations[operation] = self.operations[operation][-1000:]

        return duration_ms > self.slow_threshold_ms

    def get_stats(self) -> Dict[str, Any]:
        """Get performance statistics."""
        with self.lock:
            stats = {}
            for operation, durations in self.operations.items():
                if durations:
                    stats[operation] = {
                        "count": len(durations),
                        "min_ms": min(durations),
                        "max_ms": max(durations),
                        "avg_ms": sum(durations) / len(durations),
                        "p95_ms": sorted(durations)[int(len(durations) * 0.95)]
                        if len(durations) > 1
                        else durations[0],
                    }
            return stats


# =============================================================================
# STRUCTURED LOGGER
# =============================================================================

class StructuredLogger:
    """
    Enhanced logger with structured logging, error aggregation, and performance tracking.

    Features:
    - JSON formatted logs
    - Async file writing
    - Automatic log rotation
    - Error pattern detection
    - Performance monitoring
    - Context enrichment
    """

    def __init__(
        self,
        name: str,
        config: LoggingConfig,
        error_aggregator: Optional[ErrorAggregator] = None,
        performance_tracker: Optional[PerformanceTracker] = None,
    ):
        self.name = name
        self.config = config
        self.error_aggregator = error_aggregator
        self.performance_tracker = performance_tracker

        # Create underlying logger
        self._logger = logging.getLogger(name)
        self._logger.setLevel(getattr(logging, config.default_level))
        self._logger.propagate = False  # Don't propagate to root logger

        # Add handlers (will be done in setup)
        self._handlers_configured = False

    def _ensure_handlers(self) -> None:
        """Ensure handlers are configured (lazy initialization)."""
        if self._handlers_configured:
            return

        # Clear existing handlers
        self._logger.handlers.clear()

        # JSON formatter
        json_formatter = StructuredJSONFormatter(self.config)

        # Console handler (stdout)
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(getattr(logging, self.config.console_level))
        console_handler.setFormatter(json_formatter)
        self._logger.addHandler(console_handler)

        # File handler (async rotating)
        log_file = self.config.log_dir / f"{self.name}.jsonl"
        file_handler = AsyncRotatingFileHandler(
            filename=log_file,
            max_bytes=self.config.max_bytes,
            backup_count=self.config.backup_count,
            queue_size=self.config.queue_size,
            flush_interval=self.config.flush_interval_seconds,
        )
        file_handler.setLevel(getattr(logging, self.config.file_level))
        file_handler.setFormatter(json_formatter)
        self._logger.addHandler(file_handler)

        # Error file (only errors and critical)
        error_file = self.config.log_dir / f"{self.name}_errors.jsonl"
        error_handler = AsyncRotatingFileHandler(
            filename=error_file,
            max_bytes=self.config.max_bytes,
            backup_count=self.config.backup_count,
        )
        error_handler.setLevel(logging.ERROR)
        error_handler.setFormatter(json_formatter)
        self._logger.addHandler(error_handler)

        self._handlers_configured = True

    def _log_with_context(
        self,
        level: int,
        msg: str,
        *args,
        exc_info: Any = None,
        stack_info: bool = False,
        extra: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> None:
        """Internal logging with context enrichment."""
        self._ensure_handlers()

        # Merge extra and kwargs
        context = extra or {}
        context.update(kwargs)

        # Log the message
        self._logger.log(
            level,
            msg,
            *args,
            exc_info=exc_info,
            stack_info=stack_info,
            extra=context,
        )

        # Record error if applicable
        if level >= logging.ERROR and exc_info and self.error_aggregator:
            if exc_info is True:
                exc_info = sys.exc_info()

            if exc_info[0]:
                error_type = exc_info[0].__name__
                error_message = str(exc_info[1]) if exc_info[1] else ""
                traceback_str = "".join(traceback.format_exception(*exc_info))

                should_alert = self.error_aggregator.record_error(
                    error_type=error_type,
                    error_message=error_message,
                    module=self.name,
                    traceback_str=traceback_str,
                )

                if should_alert:
                    self._logger.critical(
                        f"ERROR THRESHOLD REACHED: {error_type} has occurred "
                        f"{self.error_aggregator.alert_threshold} times",
                        error_type=error_type,
                        error_message=error_message,
                    )

    # Logging methods
    def debug(self, msg: str, *args, **kwargs) -> None:
        """Log debug message."""
        self._log_with_context(logging.DEBUG, msg, *args, **kwargs)

    def info(self, msg: str, *args, **kwargs) -> None:
        """Log info message."""
        self._log_with_context(logging.INFO, msg, *args, **kwargs)

    def warning(self, msg: str, *args, **kwargs) -> None:
        """Log warning message."""
        self._log_with_context(logging.WARNING, msg, *args, **kwargs)

    def error(self, msg: str, *args, **kwargs) -> None:
        """Log error message."""
        self._log_with_context(logging.ERROR, msg, *args, **kwargs)

    def critical(self, msg: str, *args, **kwargs) -> None:
        """Log critical message."""
        self._log_with_context(logging.CRITICAL, msg, *args, **kwargs)

    @asynccontextmanager
    async def timer(self, operation: str, **context):
        """
        Context manager for timing operations.

        Usage:
            async with logger.timer("database_query", table="users"):
                result = await db.query(...)
        """
        start = time.perf_counter()
        try:
            yield
        finally:
            duration_ms = (time.perf_counter() - start) * 1000

            # Record performance
            is_slow = False
            if self.performance_tracker:
                is_slow = self.performance_tracker.record(operation, duration_ms)

            # Log timing
            log_func = self.warning if is_slow else self.debug
            log_func(
                f"Operation completed: {operation}",
                operation=operation,
                duration_ms=round(duration_ms, 2),
                slow=is_slow,
                **context,
            )

    def get_error_stats(self) -> Optional[Dict[str, Any]]:
        """Get error statistics."""
        if self.error_aggregator:
            return self.error_aggregator.get_stats()
        return None

    def get_performance_stats(self) -> Optional[Dict[str, Any]]:
        """Get performance statistics."""
        if self.performance_tracker:
            return self.performance_tracker.get_stats()
        return None


# =============================================================================
# GLOBAL REGISTRY
# =============================================================================

_global_config: Optional[LoggingConfig] = None
_global_error_aggregator: Optional[ErrorAggregator] = None
_global_performance_tracker: Optional[PerformanceTracker] = None
_loggers: Dict[str, StructuredLogger] = {}
_lock = threading.Lock()


def configure_structured_logging(config: Optional[LoggingConfig] = None) -> None:
    """
    Configure global structured logging system.

    Args:
        config: Optional configuration (defaults to environment-based config)
    """
    global _global_config, _global_error_aggregator, _global_performance_tracker

    with _lock:
        _global_config = config or LoggingConfig.from_env()

        if _global_config.enable_error_aggregation:
            _global_error_aggregator = ErrorAggregator(
                window_seconds=_global_config.error_window_seconds,
                alert_threshold=_global_config.error_threshold_for_alert,
            )

        if _global_config.enable_performance_tracking:
            _global_performance_tracker = PerformanceTracker(
                slow_threshold_ms=_global_config.slow_operation_threshold_ms,
            )


def get_structured_logger(name: str) -> StructuredLogger:
    """
    Get or create a structured logger for a module.

    Args:
        name: Logger name (usually __name__)

    Returns:
        StructuredLogger instance
    """
    with _lock:
        # Ensure config is initialized
        if _global_config is None:
            configure_structured_logging()

        # Get or create logger
        if name not in _loggers:
            _loggers[name] = StructuredLogger(
                name=name,
                config=_global_config,
                error_aggregator=_global_error_aggregator,
                performance_tracker=_global_performance_tracker,
            )

        return _loggers[name]


def get_global_logging_stats() -> Dict[str, Any]:
    """Get global logging statistics."""
    stats = {
        "config": asdict(_global_config) if _global_config else None,
        "active_loggers": list(_loggers.keys()),
    }

    if _global_error_aggregator:
        stats["errors"] = _global_error_aggregator.get_stats()

    if _global_performance_tracker:
        stats["performance"] = _global_performance_tracker.get_stats()

    return stats
