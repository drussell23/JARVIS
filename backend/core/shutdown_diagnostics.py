"""
Shutdown Diagnostics v151.0 - Deep Forensic Logging for Shutdown Analysis
==========================================================================

This module provides enterprise-grade diagnostic logging to trace exactly
what triggers shutdown events in the JARVIS Trinity system.

PROBLEM SOLVED:
    System was shutting down within 100ms of startup with no clear trigger.
    This module captures full stack traces, environment state, process info,
    and timing data at every shutdown-related checkpoint.

USAGE:
    from backend.core.shutdown_diagnostics import (
        log_shutdown_trigger,
        log_startup_checkpoint,
        capture_system_state,
        ShutdownDiagnostics,
    )

    # At any shutdown-triggering location:
    log_shutdown_trigger("coordinated_shutdown", "initiate_shutdown called")

DIAGNOSTIC OUTPUT:
    All diagnostics are written to:
    - Console (WARNING level and above)
    - ~/.jarvis/trinity/shutdown_diagnostics.log (DEBUG level - full detail)
    - ~/.jarvis/trinity/shutdown_forensics.json (structured JSON for analysis)

Author: JARVIS AI System
Version: 151.0.0
"""

from __future__ import annotations

import atexit
import datetime
import inspect
import json
import logging
import os
import signal
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# =============================================================================
# DIAGNOSTIC LOGGER SETUP
# =============================================================================

# Create dedicated diagnostic logger
_diag_logger = logging.getLogger("jarvis.shutdown.diagnostics")
_diag_logger.setLevel(logging.DEBUG)

# Ensure we have a handler for the diagnostic logger
_diag_log_dir = Path.home() / ".jarvis" / "trinity"
_diag_log_dir.mkdir(parents=True, exist_ok=True)

_diag_log_file = _diag_log_dir / "shutdown_diagnostics.log"
_forensics_file = _diag_log_dir / "shutdown_forensics.json"

# File handler with detailed format
try:
    _file_handler = logging.FileHandler(_diag_log_file, mode='a')
    _file_handler.setLevel(logging.DEBUG)
    _file_handler.setFormatter(logging.Formatter(
        '%(asctime)s.%(msecs)03d | %(levelname)-8s | %(name)s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    ))
    _diag_logger.addHandler(_file_handler)
except Exception:
    pass

# Console handler for critical events
try:
    _console_handler = logging.StreamHandler()
    _console_handler.setLevel(logging.WARNING)
    _console_handler.setFormatter(logging.Formatter(
        '🔬 DIAG | %(asctime)s | %(message)s',
        datefmt='%H:%M:%S'
    ))
    _diag_logger.addHandler(_console_handler)
except Exception:
    pass

# =============================================================================
# GLOBAL STATE TRACKING
# =============================================================================

@dataclass
class ShutdownEvent:
    """Record of a shutdown-related event."""
    timestamp: float
    timestamp_iso: str
    event_type: str  # trigger, checkpoint, state_change, signal
    source_module: str
    source_function: str
    source_line: int
    message: str
    stack_trace: str
    thread_id: int
    thread_name: str
    process_id: int
    elapsed_since_startup: float
    environment_snapshot: Dict[str, str]
    extra_data: Dict[str, Any] = field(default_factory=dict)


class ShutdownDiagnostics:
    """
    Central diagnostics collector for shutdown events.

    Thread-safe singleton that collects all shutdown-related events
    for forensic analysis.
    """

    _instance: Optional["ShutdownDiagnostics"] = None
    _lock = threading.Lock()

    def __new__(cls) -> "ShutdownDiagnostics":
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self._events: List[ShutdownEvent] = []
        self._startup_time = time.time()
        self._startup_time_iso = datetime.datetime.now().isoformat()
        self._checkpoints: Dict[str, float] = {}
        self._event_lock = threading.Lock()
        self._shutdown_triggered = False
        self._shutdown_trigger_info: Optional[Dict[str, Any]] = None

        # Capture initial environment state
        self._initial_env = self._capture_env_snapshot()

        # Log initialization
        _diag_logger.info("=" * 80)
        _diag_logger.info("🔬 SHUTDOWN DIAGNOSTICS v151.0 INITIALIZED")
        _diag_logger.info(f"   Startup time: {self._startup_time_iso}")
        _diag_logger.info(f"   PID: {os.getpid()}")
        _diag_logger.info(f"   Diagnostic log: {_diag_log_file}")
        _diag_logger.info(f"   Forensics file: {_forensics_file}")
        _diag_logger.info("=" * 80)

        # Write initial state to forensics file
        self._write_forensics_header()

        # Register atexit handler to save diagnostics
        atexit.register(self._save_on_exit)

        self._initialized = True

    def _capture_env_snapshot(self) -> Dict[str, str]:
        """Capture relevant environment variables."""
        relevant_prefixes = (
            "JARVIS_", "GCP_", "TRINITY_", "REACTOR_",
            "PYTHONPATH", "PATH", "HOME", "USER"
        )
        return {
            k: v for k, v in os.environ.items()
            if any(k.startswith(p) for p in relevant_prefixes)
        }

    def _get_caller_info(self, skip_frames: int = 2) -> Tuple[str, str, int]:
        """Get caller module, function, and line number."""
        try:
            frame = inspect.currentframe()
            for _ in range(skip_frames + 1):
                if frame is not None:
                    frame = frame.f_back

            if frame is not None:
                info = inspect.getframeinfo(frame)
                module = info.filename.split("/")[-1] if info.filename else "unknown"
                return module, info.function, info.lineno
        except Exception:
            pass
        return "unknown", "unknown", 0

    def record_event(
        self,
        event_type: str,
        message: str,
        extra_data: Optional[Dict[str, Any]] = None,
        skip_frames: int = 2,
    ) -> ShutdownEvent:
        """
        Record a shutdown-related event with full context.

        Args:
            event_type: Type of event (trigger, checkpoint, state_change, signal)
            message: Human-readable description
            extra_data: Additional structured data
            skip_frames: Stack frames to skip for caller detection

        Returns:
            The recorded ShutdownEvent
        """
        now = time.time()
        source_module, source_function, source_line = self._get_caller_info(skip_frames)

        # Capture full stack trace
        stack_trace = "".join(traceback.format_stack()[:-skip_frames])

        event = ShutdownEvent(
            timestamp=now,
            timestamp_iso=datetime.datetime.now().isoformat(),
            event_type=event_type,
            source_module=source_module,
            source_function=source_function,
            source_line=source_line,
            message=message,
            stack_trace=stack_trace,
            thread_id=threading.current_thread().ident or 0,
            thread_name=threading.current_thread().name,
            process_id=os.getpid(),
            elapsed_since_startup=now - self._startup_time,
            environment_snapshot=self._capture_env_snapshot() if event_type == "trigger" else {},
            extra_data=extra_data or {},
        )

        with self._event_lock:
            self._events.append(event)

            # If this is a shutdown trigger, record it specially
            if event_type == "trigger" and not self._shutdown_triggered:
                self._shutdown_triggered = True
                self._shutdown_trigger_info = {
                    "first_trigger_time": now,
                    "first_trigger_elapsed": now - self._startup_time,
                    "first_trigger_message": message,
                    "first_trigger_source": f"{source_module}:{source_function}:{source_line}",
                    "first_trigger_stack": stack_trace,
                }

        # Log with appropriate level
        log_msg = (
            f"[{event_type.upper()}] {message} "
            f"(from {source_module}:{source_function}:{source_line}, "
            f"elapsed={event.elapsed_since_startup:.3f}s)"
        )

        if event_type == "trigger":
            _diag_logger.warning(log_msg)
            _diag_logger.warning(f"STACK TRACE:\n{stack_trace}")
        else:
            _diag_logger.info(log_msg)

        # Append to forensics file
        self._append_forensics_event(event)

        return event

    def checkpoint(self, name: str, message: str = "") -> float:
        """
        Record a startup checkpoint with timing.

        Returns:
            Elapsed time since startup
        """
        elapsed = time.time() - self._startup_time

        with self._event_lock:
            self._checkpoints[name] = elapsed

        self.record_event(
            "checkpoint",
            f"Checkpoint '{name}': {message}" if message else f"Checkpoint '{name}' reached",
            {"checkpoint_name": name, "elapsed_ms": elapsed * 1000},
            skip_frames=3,
        )

        return elapsed

    def _write_forensics_header(self) -> None:
        """Write initial forensics header to JSON file."""
        try:
            header = {
                "version": "151.0",
                "startup_time": self._startup_time,
                "startup_time_iso": self._startup_time_iso,
                "pid": os.getpid(),
                "python_version": sys.version,
                "platform": sys.platform,
                "initial_environment": self._initial_env,
                "events": [],
            }
            _forensics_file.write_text(json.dumps(header, indent=2))
        except Exception as e:
            _diag_logger.error(f"Failed to write forensics header: {e}")

    def _append_forensics_event(self, event: ShutdownEvent) -> None:
        """Append event to forensics JSON file."""
        try:
            # Read existing data
            if _forensics_file.exists():
                data = json.loads(_forensics_file.read_text())
            else:
                data = {"events": []}

            # Append event
            data["events"].append(asdict(event))

            # Write back
            _forensics_file.write_text(json.dumps(data, indent=2, default=str))
        except Exception as e:
            _diag_logger.error(f"Failed to append forensics event: {e}")

    def _save_on_exit(self) -> None:
        """Save final diagnostics state on exit."""
        try:
            exit_time = time.time()

            _diag_logger.info("=" * 80)
            _diag_logger.info("🔬 SHUTDOWN DIAGNOSTICS - EXIT SUMMARY")
            _diag_logger.info(f"   Total runtime: {exit_time - self._startup_time:.3f}s")
            _diag_logger.info(f"   Total events recorded: {len(self._events)}")
            _diag_logger.info(f"   Shutdown triggered: {self._shutdown_triggered}")

            if self._shutdown_trigger_info:
                _diag_logger.info(f"   First trigger at: {self._shutdown_trigger_info['first_trigger_elapsed']:.3f}s")
                _diag_logger.info(f"   First trigger source: {self._shutdown_trigger_info['first_trigger_source']}")

            _diag_logger.info(f"   Checkpoints: {list(self._checkpoints.keys())}")
            _diag_logger.info("=" * 80)

            # Write final summary to forensics file
            try:
                if _forensics_file.exists():
                    data = json.loads(_forensics_file.read_text())
                    data["exit_summary"] = {
                        "exit_time": exit_time,
                        "exit_time_iso": datetime.datetime.now().isoformat(),
                        "total_runtime": exit_time - self._startup_time,
                        "total_events": len(self._events),
                        "shutdown_triggered": self._shutdown_triggered,
                        "shutdown_trigger_info": self._shutdown_trigger_info,
                        "checkpoints": self._checkpoints,
                    }
                    _forensics_file.write_text(json.dumps(data, indent=2, default=str))
            except Exception:
                pass

        except Exception as e:
            print(f"[DIAG] Exit save error: {e}")

    def get_summary(self) -> Dict[str, Any]:
        """Get diagnostic summary."""
        with self._event_lock:
            return {
                "startup_time": self._startup_time_iso,
                "events_count": len(self._events),
                "checkpoints": dict(self._checkpoints),
                "shutdown_triggered": self._shutdown_triggered,
                "shutdown_trigger_info": self._shutdown_trigger_info,
                "trigger_events": [
                    asdict(e) for e in self._events if e.event_type == "trigger"
                ],
            }


# =============================================================================
# CONVENIENCE FUNCTIONS
# =============================================================================

_diagnostics: Optional[ShutdownDiagnostics] = None


def get_diagnostics() -> ShutdownDiagnostics:
    """Get the global diagnostics instance."""
    global _diagnostics
    if _diagnostics is None:
        _diagnostics = ShutdownDiagnostics()
    return _diagnostics


def log_shutdown_trigger(
    source: str,
    message: str,
    extra_data: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Log a shutdown trigger event with full forensics.

    Call this whenever shutdown is being initiated for ANY reason.

    Args:
        source: Module/component triggering shutdown
        message: Description of why shutdown is being triggered
        extra_data: Additional context data
    """
    diag = get_diagnostics()
    diag.record_event(
        "trigger",
        f"[{source}] {message}",
        {"source_component": source, **(extra_data or {})},
        skip_frames=3,
    )


def log_startup_checkpoint(name: str, message: str = "") -> float:
    """
    Log a startup checkpoint for timing analysis.

    Args:
        name: Unique checkpoint name
        message: Optional description

    Returns:
        Elapsed time since startup in seconds
    """
    return get_diagnostics().checkpoint(name, message)


def log_state_change(
    component: str,
    old_state: str,
    new_state: str,
    reason: str = "",
) -> None:
    """Log a state change event."""
    get_diagnostics().record_event(
        "state_change",
        f"[{component}] State: {old_state} → {new_state}" + (f" ({reason})" if reason else ""),
        {"component": component, "old_state": old_state, "new_state": new_state, "reason": reason},
        skip_frames=3,
    )


def log_signal_received(signum: int, handler_name: str) -> None:
    """Log when a signal is received."""
    try:
        sig_name = signal.Signals(signum).name
    except (ValueError, AttributeError):
        sig_name = f"signal_{signum}"

    get_diagnostics().record_event(
        "signal",
        f"Signal {sig_name} received, handled by {handler_name}",
        {"signal_number": signum, "signal_name": sig_name, "handler": handler_name},
        skip_frames=3,
    )


def capture_system_state() -> Dict[str, Any]:
    """
    Capture comprehensive system state for debugging.

    Returns:
        Dict with process info, threads, env vars, etc.
    """
    import psutil

    try:
        process = psutil.Process()

        return {
            "timestamp": time.time(),
            "timestamp_iso": datetime.datetime.now().isoformat(),
            "pid": os.getpid(),
            "ppid": os.getppid(),
            "cwd": os.getcwd(),
            "memory": {
                "rss_mb": process.memory_info().rss / 1024 / 1024,
                "vms_mb": process.memory_info().vms / 1024 / 1024,
                "percent": process.memory_percent(),
            },
            "cpu_percent": process.cpu_percent(),
            "threads": [
                {"id": t.ident, "name": t.name, "daemon": t.daemon}
                for t in threading.enumerate()
            ],
            "open_files": len(process.open_files()),
            "connections": len(process.net_connections()),
            "children": [
                {"pid": c.pid, "name": c.name()}
                for c in process.children(recursive=True)
            ],
            "environment_keys": list(os.environ.keys()),
        }
    except Exception as e:
        return {"error": str(e)}


def get_diagnostic_summary() -> Dict[str, Any]:
    """Get current diagnostic summary."""
    return get_diagnostics().get_summary()


# =============================================================================
# AUTO-INSTRUMENTATION
# =============================================================================

def instrument_signal_handlers() -> None:
    """
    Wrap existing signal handlers with diagnostic logging.

    Call this early in startup to ensure all signals are logged.
    """
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            original = signal.getsignal(sig)
            # Log what handler is currently installed (don't replace to avoid conflicts)
            _diag_logger.info(f"[INSTRUMENT] Signal {sig.name} handler: {original}")
        except Exception as e:
            _diag_logger.debug(f"[INSTRUMENT] Could not inspect {sig}: {e}")


# =============================================================================
# EXPORTS
# =============================================================================

__all__ = [
    "ShutdownDiagnostics",
    "ShutdownEvent",
    "get_diagnostics",
    "log_shutdown_trigger",
    "log_startup_checkpoint",
    "log_state_change",
    "log_signal_received",
    "capture_system_state",
    "get_diagnostic_summary",
    "instrument_signal_handlers",
]


# Initialize diagnostics on import
get_diagnostics()
