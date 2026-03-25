"""
Telemetry Contract v1
=====================

Unified envelope schema for all JARVIS telemetry events.

Dual versioning:
  envelope_version -- transport/routing contract (stable)
  event_schema     -- domain payload contract (name@semver, evolves per-type)

Delivery: at-least-once, idempotent consumers via idempotency_key.
Ordering: per-partition_key monotonic sequence; no global ordering.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Coroutine, Deque, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

ENVELOPE_VERSION = "1.0.0"

V1_EVENT_SCHEMAS: List[str] = [
    "lifecycle.transition@1.0.0",
    "lifecycle.health@1.0.0",
    "lifecycle.hardware@1.0.0",
    "reasoning.activation@1.0.0",
    "reasoning.decision@1.0.0",
    "reasoning.proactive_drive@1.0.0",
    "scheduler.graph_state@1.0.0",
    "scheduler.unit_state@1.0.0",
    "recovery.attempt@1.0.0",
    "fault.raised@1.0.0",
    "fault.resolved@1.0.0",
    "host.environment_change@1.0.0",
    "exploration.tendril@1.0.0",
    "vision.perception@1.0.0",
    "ouroboros.cognitive_inefficiency@1.0.0",
    "ouroboros.reflex_graduation@1.0.0",
]


class SequenceCounter:
    """Per-partition monotonic counter.

    Each partition_key gets an independent counter starting at 1.
    Thread-safe within a single process (GIL-protected dict ops).
    """

    def __init__(self) -> None:
        self._counters: Dict[str, int] = defaultdict(int)

    def next(self, partition_key: str) -> int:
        self._counters[partition_key] += 1
        return self._counters[partition_key]


# Module-level singleton used by TelemetryEnvelope.create().
_sequence_counter = SequenceCounter()


@dataclass(frozen=True)
class TelemetryEnvelope:
    """Immutable telemetry event envelope (v1.0.0).

    Fields:
        envelope_version -- transport contract version (semver)
        event_id         -- globally unique event identifier (UUID4)
        event_schema     -- domain payload contract (name@semver)
        emitted_at       -- epoch seconds (float, monotonic-ish via time.time())
        sequence         -- per-partition monotonic counter value
        trace_id         -- distributed trace identifier
        span_id          -- span within the trace
        causal_parent_id -- optional parent span for causal chaining
        op_id            -- optional Ouroboros operation identifier
        idempotency_key  -- deterministic dedup key (schema:trace:seq)
        partition_key    -- routing/ordering partition
        source           -- emitting component name
        severity         -- log-level-like severity (info, warn, error, critical)
        payload          -- domain-specific data dict
    """

    envelope_version: str
    event_id: str
    event_schema: str
    emitted_at: float
    sequence: int
    trace_id: str
    span_id: str
    causal_parent_id: Optional[str]
    op_id: Optional[str]
    idempotency_key: str
    partition_key: str
    source: str
    severity: str
    payload: Dict[str, Any]

    @classmethod
    def create(
        cls,
        event_schema: str,
        source: str,
        trace_id: str,
        span_id: str,
        partition_key: str,
        payload: Dict[str, Any],
        severity: str = "info",
        causal_parent_id: Optional[str] = None,
        op_id: Optional[str] = None,
    ) -> TelemetryEnvelope:
        """Factory that stamps event_id, emitted_at, sequence, and idempotency_key."""
        seq = _sequence_counter.next(partition_key)
        return cls(
            envelope_version=ENVELOPE_VERSION,
            event_id=str(uuid.uuid4()),
            event_schema=event_schema,
            emitted_at=time.time(),
            sequence=seq,
            trace_id=trace_id,
            span_id=span_id,
            causal_parent_id=causal_parent_id,
            op_id=op_id,
            idempotency_key=f"{event_schema}:{trace_id}:{seq}",
            partition_key=partition_key,
            source=source,
            severity=severity,
            payload=payload,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a plain dict suitable for JSON encoding."""
        return asdict(self)


class EventRegistry:
    """Validates event schemas against registered types.

    Supports semver-aware compatibility: consumers registered at major
    version N accept any event at major version N (minor/patch may differ).
    """

    def __init__(self) -> None:
        self._schemas: Dict[str, str] = {}  # name -> version

    def register(self, event_schema: str) -> None:
        """Register an event schema (name@semver)."""
        name, version = self.parse_schema(event_schema)
        self._schemas[name] = version

    def is_registered(self, event_schema: str) -> bool:
        """Check if the exact event name (ignoring version) is registered."""
        try:
            name, _ = self.parse_schema(event_schema)
            return name in self._schemas
        except ValueError:
            return False

    def is_compatible(self, event_schema: str) -> bool:
        """Check major-version compatibility with the registered version."""
        try:
            name, version = self.parse_schema(event_schema)
            registered = self._schemas.get(name)
            if registered is None:
                return False
            return registered.split(".")[0] == version.split(".")[0]
        except (ValueError, IndexError):
            return False

    @staticmethod
    def parse_schema(event_schema: str) -> Tuple[str, str]:
        """Parse 'name@version' into (name, version). Raises ValueError if malformed."""
        if "@" not in event_schema:
            raise ValueError(
                f"Invalid event_schema '{event_schema}': missing @version"
            )
        name, version = event_schema.rsplit("@", 1)
        return name, version

    @classmethod
    def with_v1_defaults(cls) -> EventRegistry:
        """Create a registry pre-loaded with the v1 event schemas."""
        registry = cls()
        for schema in V1_EVENT_SCHEMAS:
            registry.register(schema)
        return registry


# ---------------------------------------------------------------------------
# TelemetryBus — bounded queue, dedup, backpressure, dead-letter
# ---------------------------------------------------------------------------

CRITICAL_EVENT_SCHEMAS: Set[str] = {"fault.raised", "lifecycle.transition"}

TelemetryHandler = Callable[[TelemetryEnvelope], Coroutine[Any, Any, None]]


class TelemetryBus:
    """Async event bus for telemetry envelopes.

    Features:
    - Bounded asyncio.Queue with configurable max size.
    - Pattern-based subscribe (``lifecycle.*`` matches ``lifecycle.transition@1.0.0``).
    - Non-blocking ``emit()`` with dedup (idempotency_key window) and
      backpressure (drops non-critical events when queue is full).
    - Async consumer loop dispatches to matching subscribers.
    - Dead-letter deque for consumer errors.
    - ``get_metrics()`` for observability.
    """

    def __init__(
        self,
        max_queue: int = 1000,
        dedup_window_s: float = 300.0,
        dead_letter_max: int = 100,
    ) -> None:
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=max_queue)
        self._dedup_window_s = dedup_window_s
        self._subscribers: List[tuple] = []
        self._consumer_task: Optional[asyncio.Task] = None
        self._running = False
        self._seen_keys: Dict[str, float] = {}
        self.dead_letter: Deque[Dict[str, Any]] = deque(maxlen=dead_letter_max)
        self.dropped_count: int = 0
        self.emitted_count: int = 0
        self.delivered_count: int = 0
        self.deduped_count: int = 0
        self._registry = EventRegistry.with_v1_defaults()

    # ------------------------------------------------------------------
    # subscribe
    # ------------------------------------------------------------------

    def subscribe(self, pattern: str, handler: TelemetryHandler) -> None:
        """Register *handler* for envelopes whose ``event_schema`` matches *pattern*.

        Pattern language:
        - ``"*"`` — matches every event.
        - ``"lifecycle.*"`` — matches any event whose schema name starts with ``lifecycle``.
        """
        self._subscribers.append((pattern, handler))

    # ------------------------------------------------------------------
    # emit (non-blocking)
    # ------------------------------------------------------------------

    def emit(self, envelope: TelemetryEnvelope) -> None:
        """Enqueue *envelope* for delivery.  Non-blocking (``put_nowait``).

        Dedup: envelopes with an ``idempotency_key`` already seen within the
        dedup window are silently dropped.

        Backpressure: when the queue is full, non-critical events are dropped.
        Critical events (schemas in ``CRITICAL_EVENT_SCHEMAS``) are logged but
        **not** silently discarded — the caller is still notified via the
        warning log so they can decide to retry.
        """
        now = time.time()

        # --- dedup ---
        if envelope.idempotency_key in self._seen_keys:
            if now - self._seen_keys[envelope.idempotency_key] < self._dedup_window_s:
                self.deduped_count += 1
                return

        self._seen_keys[envelope.idempotency_key] = now

        # Prune stale keys to bound memory.
        if len(self._seen_keys) > 5000:
            cutoff = now - self._dedup_window_s
            self._seen_keys = {k: v for k, v in self._seen_keys.items() if v > cutoff}

        # --- schema registration warning ---
        if not self._registry.is_registered(envelope.event_schema):
            logger.warning(
                "[TelemetryBus] Unknown event_schema: %s", envelope.event_schema
            )

        # --- enqueue with backpressure ---
        try:
            self._queue.put_nowait(envelope)
            self.emitted_count += 1
        except asyncio.QueueFull:
            schema_name = (
                envelope.event_schema.split("@")[0]
                if "@" in envelope.event_schema
                else envelope.event_schema
            )
            if schema_name in CRITICAL_EVENT_SCHEMAS:
                logger.warning(
                    "[TelemetryBus] Queue full, critical event %s may be delayed",
                    envelope.event_schema,
                )
            else:
                self.dropped_count += 1

    # ------------------------------------------------------------------
    # pattern matching
    # ------------------------------------------------------------------

    def _matches_pattern(self, pattern: str, event_schema: str) -> bool:
        """Return ``True`` if *event_schema* matches the subscriber *pattern*."""
        if pattern == "*":
            return True
        prefix = pattern.rstrip("*").rstrip(".")
        schema_name = (
            event_schema.split("@")[0] if "@" in event_schema else event_schema
        )
        return schema_name.startswith(prefix)

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the background consumer loop."""
        if self._running:
            return
        self._running = True
        self._consumer_task = asyncio.create_task(
            self._consumer_loop(), name="telemetry_bus_consumer"
        )

    async def _consumer_loop(self) -> None:
        """Drain the queue and dispatch to matching subscribers."""
        while self._running:
            try:
                envelope = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            for pattern, handler in self._subscribers:
                if self._matches_pattern(pattern, envelope.event_schema):
                    try:
                        await handler(envelope)
                        self.delivered_count += 1
                    except Exception as exc:
                        self.dead_letter.append(
                            {
                                "envelope": envelope.to_dict(),
                                "error": str(exc),
                                "timestamp": time.time(),
                                "handler": getattr(handler, "__name__", str(handler)),
                            }
                        )

    async def stop(self) -> None:
        """Stop the consumer loop and cancel its task."""
        self._running = False
        if self._consumer_task is not None:
            self._consumer_task.cancel()
            try:
                await self._consumer_task
            except asyncio.CancelledError:
                pass
            self._consumer_task = None

    # ------------------------------------------------------------------
    # metrics
    # ------------------------------------------------------------------

    def get_metrics(self) -> Dict[str, Any]:
        """Return a snapshot of bus metrics."""
        return {
            "emitted": self.emitted_count,
            "delivered": self.delivered_count,
            "dropped": self.dropped_count,
            "deduped": self.deduped_count,
            "dead_letter": len(self.dead_letter),
            "queue_size": self._queue.qsize(),
        }


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_bus_instance: Optional[TelemetryBus] = None


def get_telemetry_bus() -> TelemetryBus:
    """Return the module-level ``TelemetryBus`` singleton (lazy-created)."""
    global _bus_instance
    if _bus_instance is None:
        _bus_instance = TelemetryBus()
    return _bus_instance
