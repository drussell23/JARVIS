"""Lifecycle Event Emitter v1.0

Emits structured lifecycle events (boot, phase, recovery, shutdown) with
causal chaining via TraceEnvelopes.  Events are buffered in-memory and
persisted to JSONL via TraceStreamManager.

Thread-safe.  Auto-flushes every 2 seconds and on phase transitions.
"""

from __future__ import annotations

import collections
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.core.trace_envelope import TraceEnvelope, TraceEnvelopeFactory, BoundaryType
from backend.core.trace_store import TraceStreamManager

logger = logging.getLogger(__name__)


class LifecycleEmitter:
    """Emits and persists lifecycle events with causal chaining.

    Each event gets a TraceEnvelope, with caused_by_event_id linking
    to the previous event (boot_start -> phase_enter -> phase_exit -> ...).

    Auto-flushes buffered events to JSONL every 2 seconds and on phase
    transitions.  Call close() to cancel the timer and do a final flush.
    """

    def __init__(
        self,
        trace_dir: Path,
        envelope_factory: TraceEnvelopeFactory,
        buffer_max: int = 64,
        auto_flush_interval: float = 2.0,
    ) -> None:
        self._factory = envelope_factory
        self._stream_mgr = TraceStreamManager(
            base_dir=trace_dir,
            runtime_epoch_id=envelope_factory.runtime_epoch_id,
        )
        self._buffer: collections.deque = collections.deque(maxlen=buffer_max)
        self._flush_pending: List[Dict[str, Any]] = []
        self._lock = threading.Lock()

        # Causality tracking
        self._last_event_id: Optional[str] = None
        self._boot_envelope: Optional[TraceEnvelope] = None

        # Auto-flush timer
        self._auto_flush_interval = auto_flush_interval
        self._flush_timer: Optional[threading.Timer] = None
        self._closed = False
        if auto_flush_interval > 0:
            self._start_auto_flush()

    def _start_auto_flush(self) -> None:
        """Schedule the next auto-flush tick."""
        if self._closed:
            return
        self._flush_timer = threading.Timer(self._auto_flush_interval, self._auto_flush_tick)
        self._flush_timer.daemon = True
        self._flush_timer.start()

    def _auto_flush_tick(self) -> None:
        """Auto-flush callback.  Reschedules itself."""
        try:
            self.flush()
        except Exception:
            logger.debug("Auto-flush failed", exc_info=True)
        finally:
            self._start_auto_flush()

    def close(self) -> None:
        """Cancel auto-flush timer and do a final flush."""
        self._closed = True
        if self._flush_timer is not None:
            self._flush_timer.cancel()
        try:
            self.flush()
        except Exception:
            logger.debug("Final flush on close failed", exc_info=True)

    def _emit(
        self,
        event_type: str,
        caused_by_override: Optional[str] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Core emit method. Creates envelope, builds event dict, buffers it.

        Thread-safe: holds lock for entire envelope creation + buffer append
        to prevent causality chain forks.
        """
        component = kwargs.pop("component", "supervisor")
        operation = f"lifecycle.{event_type}"

        with self._lock:
            caused_by = caused_by_override if caused_by_override is not None else self._last_event_id

            if self._boot_envelope is not None:
                envelope = self._factory.create_child(
                    parent=self._boot_envelope,
                    component=component,
                    operation=operation,
                    caused_by_event_id=caused_by,
                )
            else:
                envelope = self._factory.create_root(
                    component=component,
                    operation=operation,
                )

            event = {
                "event_type": event_type,
                "ts": time.time(),
                "component": component,
                "envelope": envelope.to_dict(),
                **kwargs,
            }

            self._buffer.append(event)
            self._flush_pending.append(event)
            self._last_event_id = envelope.event_id

        return event

    # -- Boot lifecycle -------------------------------------------------------

    def boot_start(self, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Emit boot_start event. Sets the root envelope for all subsequent events."""
        root = self._factory.create_root(
            component="supervisor",
            operation="lifecycle.boot_start",
        )

        event = {
            "event_type": "boot_start",
            "ts": time.time(),
            "component": "supervisor",
            "envelope": root.to_dict(),
            **(metadata or {}),
        }

        with self._lock:
            self._boot_envelope = root
            self._buffer.append(event)
            self._flush_pending.append(event)
            self._last_event_id = root.event_id

        return event

    def boot_complete(self, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Emit boot_complete event."""
        return self._emit("boot_complete", **(metadata or {}))

    def shutdown_start(self, reason: str = "") -> Dict[str, Any]:
        """Emit shutdown_start event."""
        return self._emit("shutdown_start", reason=reason)

    # -- Phase lifecycle ------------------------------------------------------

    def phase_enter(self, phase: str, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Emit phase_enter event. Auto-flushes pending events."""
        event = self._emit("phase_enter", phase=phase, **(metadata or {}))
        self.flush()
        return event

    def phase_exit(self, phase: str, success: bool = True, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Emit phase_exit event. Auto-flushes pending events."""
        to_state = "success" if success else "failure"
        event = self._emit("phase_exit", phase=phase, to_state=to_state, **(metadata or {}))
        self.flush()
        return event

    def phase_fail(
        self, phase: str, error: str = "", evidence: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Emit phase_fail event. Auto-flushes pending events."""
        event = self._emit(
            "phase_fail", phase=phase, error=error,
            evidence=evidence or {}, **(metadata or {}),
        )
        self.flush()
        return event

    # -- Recovery lifecycle ---------------------------------------------------

    def recovery_start(
        self, component: str, reason: str, caused_by_event_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Emit recovery_start event. Optionally links to the failure that triggered it."""
        return self._emit(
            "recovery_start",
            caused_by_override=caused_by_event_id,
            component=component,
            reason=reason,
        )

    def recovery_complete(self, component: str, outcome: str) -> Dict[str, Any]:
        """Emit recovery_complete event."""
        return self._emit("recovery_complete", component=component, outcome=outcome)

    def recovery_fail(self, component: str, error: str) -> Dict[str, Any]:
        """Emit recovery_fail event."""
        return self._emit("recovery_fail", component=component, error=error)

    # -- Query ----------------------------------------------------------------

    def get_recent(self, n: int = 10) -> List[Dict[str, Any]]:
        """Return the last N events from the in-memory buffer."""
        with self._lock:
            items = list(self._buffer)
        return items[-n:]

    # -- Persistence ----------------------------------------------------------

    def flush(self) -> int:
        """Flush pending events to JSONL. Returns count of flushed events."""
        with self._lock:
            events = list(self._flush_pending)
            self._flush_pending.clear()

        if not events:
            return 0

        for event in events:
            try:
                self._stream_mgr.write_lifecycle(event)
            except Exception:
                logger.debug("Failed to persist lifecycle event", exc_info=True)

        return len(events)
