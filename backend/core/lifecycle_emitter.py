"""Lifecycle Event Emitter v1.0

Emits structured lifecycle events (boot, phase, recovery, shutdown) with
causal chaining via TraceEnvelopes.  Events are buffered in-memory and
persisted to JSONL via TraceStreamManager.

Thread-safe.  Singleton via get_instance().
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
    """

    def __init__(
        self,
        trace_dir: Path,
        envelope_factory: TraceEnvelopeFactory,
        buffer_max: int = 64,
    ) -> None:
        self._factory = envelope_factory
        self._stream_mgr = TraceStreamManager(
            base_dir=trace_dir,
            runtime_epoch_id=envelope_factory._runtime_epoch_id,
        )
        self._buffer: collections.deque = collections.deque(maxlen=buffer_max)
        self._lock = threading.Lock()

        # Causality tracking
        self._last_event_id: Optional[str] = None
        self._boot_envelope: Optional[TraceEnvelope] = None

    def _emit(self, event_type: str, **kwargs: Any) -> Dict[str, Any]:
        """Core emit method. Creates envelope, builds event dict, buffers it."""
        # Create envelope with causality link
        caused_by = self._last_event_id

        component = kwargs.pop("component", "supervisor")
        operation = f"lifecycle.{event_type}"

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
            "envelope": envelope.to_dict(),
            **kwargs,
        }

        with self._lock:
            self._buffer.append(event)
            self._last_event_id = envelope.event_id

        return event

    # -- Boot lifecycle -------------------------------------------------------

    def boot_start(self, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Emit boot_start event. Sets the root envelope for all subsequent events."""
        root = self._factory.create_root(
            component="supervisor",
            operation="lifecycle.boot_start",
        )
        self._boot_envelope = root

        event = {
            "event_type": "boot_start",
            "ts": time.time(),
            "envelope": root.to_dict(),
            **(metadata or {}),
        }

        with self._lock:
            self._buffer.append(event)
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
        """Emit phase_enter event."""
        return self._emit("phase_enter", phase=phase, **(metadata or {}))

    def phase_exit(self, phase: str, success: bool = True, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Emit phase_exit event."""
        to_state = "success" if success else "failure"
        return self._emit("phase_exit", phase=phase, to_state=to_state, **(metadata or {}))

    def phase_fail(
        self, phase: str, error: str = "", evidence: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Emit phase_fail event."""
        return self._emit(
            "phase_fail", phase=phase, error=error,
            evidence=evidence or {}, **(metadata or {}),
        )

    # -- Recovery lifecycle ---------------------------------------------------

    def recovery_start(
        self, component: str, reason: str, caused_by_event_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Emit recovery_start event."""
        return self._emit(
            "recovery_start", component=component, reason=reason,
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
        """Flush all buffered events to JSONL. Returns count of flushed events."""
        with self._lock:
            events = list(self._buffer)

        for event in events:
            try:
                self._stream_mgr.write_lifecycle(event)
            except Exception:
                logger.debug("Failed to persist lifecycle event", exc_info=True)

        return len(events)
