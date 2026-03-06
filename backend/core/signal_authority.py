"""
Signal Authority (Disease 5+6 MVP)
====================================
Single owner of all OS signal registrations.

Uses loop.add_signal_handler() on POSIX when available.
Falls back to signal.signal() + call_soon_threadsafe() otherwise.
Modules subscribe to lifecycle events, never to OS signals directly.
"""
import json
import logging
import os
import signal
import time
from pathlib import Path
from typing import Dict

from backend.core.kernel_lifecycle_engine import LifecycleEngine, LifecycleEvent
from backend.core.lifecycle_exceptions import TransitionRejected

_logger = logging.getLogger(__name__)


class SignalAuthority:
    """Single owner of all OS signal registrations.

    One instance per process. Bridges OS signals into the
    lifecycle engine's transition table.
    """

    def __init__(self, engine: LifecycleEngine, loop):
        self._engine = engine
        self._loop = loop
        self._signal_count: Dict[int, int] = {}
        self._installed = False

    def install(self) -> None:
        """Register handlers for SIGTERM, SIGINT. Call once at boot."""
        if self._installed:
            return
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                self._loop.add_signal_handler(sig, self._handle_signal, sig.value)
            except (NotImplementedError, AttributeError):
                # Fallback for non-POSIX or mock loops
                signal.signal(sig, self._handle_signal_compat)
        self._installed = True
        _logger.info("[SignalAuthority] Installed handlers for SIGTERM, SIGINT")

    def _handle_signal(self, signum: int) -> None:
        """POSIX path: runs in event loop context (add_signal_handler)."""
        self._signal_count[signum] = self._signal_count.get(signum, 0) + 1
        count = self._signal_count[signum]

        if count > 3:
            self._emergency_exit(signum)
            return

        try:
            sig_name = signal.Signals(signum).name
        except (ValueError, AttributeError):
            sig_name = str(signum)

        _logger.warning(
            "[SignalAuthority] Received %s (count=%d)", sig_name, count,
        )

        try:
            self._engine.transition(
                LifecycleEvent.SHUTDOWN,
                actor=f"signal:{sig_name}",
                reason=f"OS signal received (count={count})",
            )
        except TransitionRejected:
            _logger.info("[SignalAuthority] Shutdown already in progress, signal deduplicated")
        except Exception as e:
            _logger.error("[SignalAuthority] Transition failed: %s", e)

    def _handle_signal_compat(self, signum: int, frame) -> None:
        """Fallback: runs in signal thread. Bridges to event loop."""
        self._signal_count[signum] = self._signal_count.get(signum, 0) + 1
        if self._signal_count[signum] > 3:
            self._emergency_exit(signum)
            return
        try:
            self._loop.call_soon_threadsafe(self._handle_signal, signum)
        except RuntimeError:
            # Loop already closed — handle synchronously
            self._handle_signal(signum)

    def _emergency_exit(self, signum: int) -> None:
        """Hard exit after repeated signals. Best-effort snapshot first."""
        _logger.critical(
            "[SignalAuthority] Emergency exit: signal %d received >3 times", signum,
        )
        try:
            snapshot = {
                "exit_reason": f"repeated_signal:{signum}",
                "signal_counts": dict(self._signal_count),
                "engine_state": self._engine.state.value,
                "engine_epoch": self._engine.epoch,
                "at_monotonic": time.monotonic(),
            }
            Path("/tmp/jarvis_emergency_snapshot.json").write_text(
                json.dumps(snapshot), encoding="utf-8",
            )
        except Exception:
            pass  # best effort, bounded
        os._exit(128 + signum)
