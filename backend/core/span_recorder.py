"""Span Recorder v1.0

Async context manager for recording operation spans with TraceEnvelopes.
Spans are buffered via SpanBuffer (backpressure-aware) and flushed to
date-partitioned JSONL via TraceStreamManager.

Thread-safe.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional

from backend.core.trace_envelope import TraceEnvelopeFactory
from backend.core.trace_store import TraceStreamManager

logger = logging.getLogger(__name__)


class SpanRecorder:
    """Records operation spans with TraceEnvelopes and backpressure.

    Usage::

        async with recorder.span("health_check", component="prime") as span:
            result = await do_health_check()
        # span["status"] is "success" or "error"
        # span["duration_ms"] has elapsed time
    """

    def __init__(
        self,
        trace_dir: Path,
        envelope_factory: TraceEnvelopeFactory,
        buffer_max: int = 256,
    ) -> None:
        self._factory = envelope_factory
        self._stream_mgr = TraceStreamManager(
            base_dir=trace_dir,
            runtime_epoch_id=envelope_factory.runtime_epoch_id,
            span_buffer_size=buffer_max,
        )
        self._recent: List[Dict[str, Any]] = []
        self._recent_max = 64

    @asynccontextmanager
    async def span(
        self,
        operation: str,
        component: str = "unknown",
        idempotency_key: Optional[str] = None,
        caused_by_event_id: Optional[str] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        """Async context manager that records a span.

        Yields a mutable dict that is updated with status/duration on exit.
        The span is added to the buffer on exit (even on error).
        """
        envelope = self._factory.create_root(
            component=component,
            operation=operation,
            idempotency_key=idempotency_key,
        )

        span_dict: Dict[str, Any] = {
            "operation": operation,
            "component": component,
            "envelope": envelope.to_dict(),
            "start_ts": time.time(),
            "start_mono": time.monotonic(),
            "status": "running",
            "duration_ms": 0,
            "error_class": None,
            "error_message": None,
        }
        if idempotency_key:
            span_dict["idempotency_key"] = idempotency_key
        if caused_by_event_id:
            span_dict["caused_by_event_id"] = caused_by_event_id

        try:
            yield span_dict
            span_dict["status"] = "success"
        except asyncio.CancelledError:
            span_dict["status"] = "cancelled"
            raise
        except Exception as exc:
            span_dict["status"] = "error"
            span_dict["error_class"] = type(exc).__name__
            span_dict["error_message"] = str(exc)[:500]
            raise
        finally:
            elapsed = time.monotonic() - span_dict["start_mono"]
            span_dict["duration_ms"] = round(elapsed * 1000, 2)
            span_dict["end_ts"] = time.time()
            # Remove monotonic clock (internal only)
            span_dict.pop("start_mono", None)

            self._stream_mgr.write_span(span_dict)

            if len(self._recent) >= self._recent_max:
                self._recent = self._recent[-(self._recent_max // 2):]
            self._recent.append(span_dict)

    def get_recent(self, n: int = 10) -> List[Dict[str, Any]]:
        """Return the last N recorded spans."""
        return self._recent[-n:]

    def flush(self) -> int:
        """Flush buffered spans to JSONL. Returns count flushed."""
        return self._stream_mgr.flush_spans()
