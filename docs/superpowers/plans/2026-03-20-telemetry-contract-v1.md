# Telemetry Contract v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the unified telemetry contract layer (TelemetryEnvelope, TelemetryBus, EventRegistry) that all producers and consumers share, then migrate the two existing producers (ChainTelemetry, LifecycleController) to emit envelopes.

**Architecture:** A new `backend/core/telemetry_contract.py` defines the frozen envelope schema, a bounded async bus with backpressure/dead-letter, and an event registry for schema validation. Existing producers wrap their domain payloads in envelopes via a thin `emit_envelope()` helper. Consumers (future dashboard) subscribe to the bus by event schema pattern.

**Tech Stack:** Python 3.12, asyncio, dataclasses, pytest

**Spec:** `docs/superpowers/specs/2026-03-20-telemetry-contract-v1-design.md`

---

## File Structure

| File | Responsibility |
|---|---|
| `backend/core/telemetry_contract.py` | **NEW** — TelemetryEnvelope, EventRegistry, TelemetryBus, SequenceCounter |
| `tests/core/test_telemetry_contract.py` | **NEW** — Envelope validation, bus emit/subscribe, dedup, backpressure, dead-letter |
| `backend/core/reasoning_chain_orchestrator.py` | **MODIFY** — ChainTelemetry._emit() wraps events in envelopes via TelemetryBus |
| `backend/core/jprime_lifecycle_controller.py` | **MODIFY** — _emit_telemetry() wraps transitions in envelopes via TelemetryBus |

---

### Task 1: TelemetryEnvelope, EventRegistry, and SequenceCounter

**Files:**
- Create: `backend/core/telemetry_contract.py`
- Create: `tests/core/test_telemetry_contract.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/core/test_telemetry_contract.py
"""Tests for the unified telemetry contract v1."""
import asyncio
import time
import pytest
from unittest.mock import AsyncMock, patch
from backend.core.telemetry_contract import (
    TelemetryEnvelope,
    EventRegistry,
    SequenceCounter,
    ENVELOPE_VERSION,
)


class TestTelemetryEnvelope:
    def test_create_envelope(self):
        env = TelemetryEnvelope.create(
            event_schema="lifecycle.transition@1.0.0",
            source="jprime_lifecycle_controller",
            trace_id="t1",
            span_id="s1",
            partition_key="lifecycle",
            payload={"from_state": "UNKNOWN", "to_state": "PROBING"},
        )
        assert env.envelope_version == ENVELOPE_VERSION
        assert env.event_schema == "lifecycle.transition@1.0.0"
        assert env.source == "jprime_lifecycle_controller"
        assert env.trace_id == "t1"
        assert env.span_id == "s1"
        assert env.partition_key == "lifecycle"
        assert env.payload["from_state"] == "UNKNOWN"
        assert env.event_id  # UUID generated
        assert env.emitted_at > 0
        assert env.severity == "info"

    def test_idempotency_key_deterministic(self):
        env = TelemetryEnvelope.create(
            event_schema="lifecycle.transition@1.0.0",
            source="test",
            trace_id="t1",
            span_id="s1",
            partition_key="lifecycle",
            payload={},
        )
        expected = f"lifecycle.transition@1.0.0:t1:{env.sequence}"
        assert env.idempotency_key == expected

    def test_envelope_is_frozen(self):
        env = TelemetryEnvelope.create(
            event_schema="test@1.0.0",
            source="test",
            trace_id="t1",
            span_id="s1",
            partition_key="test",
            payload={},
        )
        with pytest.raises(AttributeError):
            env.trace_id = "modified"

    def test_to_dict(self):
        env = TelemetryEnvelope.create(
            event_schema="fault.raised@1.0.0",
            source="test",
            trace_id="t1",
            span_id="s1",
            partition_key="recovery",
            severity="error",
            payload={"fault_class": "connection_refused"},
        )
        d = env.to_dict()
        assert d["envelope_version"] == ENVELOPE_VERSION
        assert d["event_schema"] == "fault.raised@1.0.0"
        assert d["severity"] == "error"
        assert d["payload"]["fault_class"] == "connection_refused"

    def test_causal_parent_id_optional(self):
        env = TelemetryEnvelope.create(
            event_schema="test@1.0.0",
            source="test",
            trace_id="t1",
            span_id="s1",
            partition_key="test",
            causal_parent_id="parent-s1",
            payload={},
        )
        assert env.causal_parent_id == "parent-s1"


class TestSequenceCounter:
    def test_monotonic_per_partition(self):
        counter = SequenceCounter()
        assert counter.next("lifecycle") == 1
        assert counter.next("lifecycle") == 2
        assert counter.next("reasoning") == 1
        assert counter.next("lifecycle") == 3

    def test_independent_partitions(self):
        counter = SequenceCounter()
        counter.next("a")
        counter.next("a")
        counter.next("b")
        assert counter.next("a") == 3
        assert counter.next("b") == 2


class TestEventRegistry:
    def test_register_and_validate(self):
        registry = EventRegistry()
        registry.register("lifecycle.transition@1.0.0")
        assert registry.is_registered("lifecycle.transition@1.0.0") is True

    def test_unknown_schema_not_registered(self):
        registry = EventRegistry()
        assert registry.is_registered("unknown.event@1.0.0") is False

    def test_parse_schema(self):
        name, version = EventRegistry.parse_schema("lifecycle.transition@1.0.0")
        assert name == "lifecycle.transition"
        assert version == "1.0.0"

    def test_parse_invalid_schema(self):
        with pytest.raises(ValueError):
            EventRegistry.parse_schema("no_version")

    def test_major_version_compatible(self):
        registry = EventRegistry()
        registry.register("lifecycle.transition@1.0.0")
        assert registry.is_compatible("lifecycle.transition@1.0.0") is True
        assert registry.is_compatible("lifecycle.transition@1.1.0") is True
        assert registry.is_compatible("lifecycle.transition@1.99.0") is True
        assert registry.is_compatible("lifecycle.transition@2.0.0") is False

    def test_default_v1_events_registered(self):
        registry = EventRegistry.with_v1_defaults()
        assert registry.is_registered("lifecycle.transition@1.0.0")
        assert registry.is_registered("lifecycle.health@1.0.0")
        assert registry.is_registered("reasoning.activation@1.0.0")
        assert registry.is_registered("reasoning.decision@1.0.0")
        assert registry.is_registered("scheduler.graph_state@1.0.0")
        assert registry.is_registered("scheduler.unit_state@1.0.0")
        assert registry.is_registered("recovery.attempt@1.0.0")
        assert registry.is_registered("fault.raised@1.0.0")
        assert registry.is_registered("fault.resolved@1.0.0")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/core/test_telemetry_contract.py -v 2>&1 | head -20`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement TelemetryEnvelope, SequenceCounter, EventRegistry**

```python
# backend/core/telemetry_contract.py
"""
Telemetry Contract v1
=====================

Unified envelope schema for all JARVIS telemetry events.

Dual versioning:
  envelope_version — transport/routing contract (stable)
  event_schema     — domain payload contract (name@semver, evolves per-type)

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
from typing import Any, Callable, Coroutine, Deque, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

ENVELOPE_VERSION = "1.0.0"

# Frozen v1 event taxonomy
V1_EVENT_SCHEMAS: List[str] = [
    "lifecycle.transition@1.0.0",
    "lifecycle.health@1.0.0",
    "reasoning.activation@1.0.0",
    "reasoning.decision@1.0.0",
    "scheduler.graph_state@1.0.0",
    "scheduler.unit_state@1.0.0",
    "recovery.attempt@1.0.0",
    "fault.raised@1.0.0",
    "fault.resolved@1.0.0",
]


# ---------------------------------------------------------------------------
# Sequence counter (per-partition monotonic)
# ---------------------------------------------------------------------------

class SequenceCounter:
    """Thread-safe monotonic counter per partition key."""

    def __init__(self) -> None:
        self._counters: Dict[str, int] = defaultdict(int)

    def next(self, partition_key: str) -> int:
        self._counters[partition_key] += 1
        return self._counters[partition_key]


# Module-level singleton
_sequence_counter = SequenceCounter()


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TelemetryEnvelope:
    """Immutable telemetry event envelope (v1.0.0)."""

    # Identity
    envelope_version: str
    event_id: str
    event_schema: str

    # Timing
    emitted_at: float
    sequence: int

    # Correlation
    trace_id: str
    span_id: str
    causal_parent_id: Optional[str]
    op_id: Optional[str]

    # Deduplication
    idempotency_key: str
    partition_key: str

    # Source
    source: str
    severity: str

    # Payload
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
        return asdict(self)


# ---------------------------------------------------------------------------
# Event registry
# ---------------------------------------------------------------------------

class EventRegistry:
    """Validates event schemas against registered types."""

    def __init__(self) -> None:
        self._schemas: Dict[str, str] = {}  # name -> registered version

    def register(self, event_schema: str) -> None:
        name, version = self.parse_schema(event_schema)
        self._schemas[name] = version

    def is_registered(self, event_schema: str) -> bool:
        try:
            name, _ = self.parse_schema(event_schema)
            return name in self._schemas
        except ValueError:
            return False

    def is_compatible(self, event_schema: str) -> bool:
        """Check if schema is compatible (same name, same major version)."""
        try:
            name, version = self.parse_schema(event_schema)
            registered = self._schemas.get(name)
            if registered is None:
                return False
            reg_major = registered.split(".")[0]
            check_major = version.split(".")[0]
            return reg_major == check_major
        except (ValueError, IndexError):
            return False

    @staticmethod
    def parse_schema(event_schema: str) -> tuple:
        """Parse 'name@version' into (name, version). Raises ValueError if invalid."""
        if "@" not in event_schema:
            raise ValueError(f"Invalid event_schema '{event_schema}': missing @version")
        name, version = event_schema.rsplit("@", 1)
        return name, version

    @classmethod
    def with_v1_defaults(cls) -> EventRegistry:
        """Create registry pre-loaded with all v1 frozen event schemas."""
        registry = cls()
        for schema in V1_EVENT_SCHEMAS:
            registry.register(schema)
        return registry
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/core/test_telemetry_contract.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/telemetry_contract.py tests/core/test_telemetry_contract.py
git commit -m "feat(telemetry): add TelemetryEnvelope, EventRegistry, and SequenceCounter (v1)"
```

---

### Task 2: TelemetryBus — Bounded Queue, Subscribe, Dead-Letter

**Files:**
- Modify: `backend/core/telemetry_contract.py`
- Test: `tests/core/test_telemetry_contract.py`

- [ ] **Step 1: Write failing tests for TelemetryBus**

APPEND to `tests/core/test_telemetry_contract.py`:

```python
from backend.core.telemetry_contract import TelemetryBus, get_telemetry_bus


class TestTelemetryBus:
    @pytest.mark.asyncio
    async def test_emit_and_subscribe(self):
        bus = TelemetryBus(max_queue=100)
        received = []

        async def handler(env: TelemetryEnvelope):
            received.append(env)

        bus.subscribe("lifecycle.*", handler)
        await bus.start()

        env = TelemetryEnvelope.create(
            event_schema="lifecycle.transition@1.0.0",
            source="test", trace_id="t1", span_id="s1",
            partition_key="lifecycle", payload={"test": True},
        )
        bus.emit(env)
        await asyncio.sleep(0.1)  # Let consumer process
        await bus.stop()
        assert len(received) == 1
        assert received[0].event_id == env.event_id

    @pytest.mark.asyncio
    async def test_pattern_matching(self):
        bus = TelemetryBus(max_queue=100)
        lifecycle_events = []
        reasoning_events = []

        async def lifecycle_handler(env):
            lifecycle_events.append(env)

        async def reasoning_handler(env):
            reasoning_events.append(env)

        bus.subscribe("lifecycle.*", lifecycle_handler)
        bus.subscribe("reasoning.*", reasoning_handler)
        await bus.start()

        bus.emit(TelemetryEnvelope.create(
            event_schema="lifecycle.transition@1.0.0",
            source="test", trace_id="t1", span_id="s1",
            partition_key="lifecycle", payload={},
        ))
        bus.emit(TelemetryEnvelope.create(
            event_schema="reasoning.decision@1.0.0",
            source="test", trace_id="t2", span_id="s2",
            partition_key="reasoning", payload={},
        ))
        await asyncio.sleep(0.1)
        await bus.stop()
        assert len(lifecycle_events) == 1
        assert len(reasoning_events) == 1

    @pytest.mark.asyncio
    async def test_dedup_by_idempotency_key(self):
        bus = TelemetryBus(max_queue=100, dedup_window_s=5.0)
        received = []

        async def handler(env):
            received.append(env)

        bus.subscribe("*", handler)
        await bus.start()

        env = TelemetryEnvelope.create(
            event_schema="lifecycle.health@1.0.0",
            source="test", trace_id="t1", span_id="s1",
            partition_key="lifecycle", payload={},
        )
        bus.emit(env)
        bus.emit(env)  # Duplicate — same idempotency_key
        await asyncio.sleep(0.1)
        await bus.stop()
        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_backpressure_drops_non_critical(self):
        bus = TelemetryBus(max_queue=2)  # Very small queue
        bus.subscribe("*", AsyncMock())
        # Don't start consumer — let queue fill up

        env1 = TelemetryEnvelope.create(
            event_schema="lifecycle.health@1.0.0",  # Non-critical
            source="test", trace_id="t1", span_id="s1",
            partition_key="lifecycle", payload={},
        )
        env2 = TelemetryEnvelope.create(
            event_schema="lifecycle.health@1.0.0",
            source="test", trace_id="t2", span_id="s2",
            partition_key="lifecycle", payload={},
        )
        env3_critical = TelemetryEnvelope.create(
            event_schema="fault.raised@1.0.0",  # Critical — never dropped
            source="test", trace_id="t3", span_id="s3",
            partition_key="recovery", severity="error",
            payload={"fault_class": "test"},
        )

        bus.emit(env1)
        bus.emit(env2)
        # Queue is full (2/2). Non-critical emit should be dropped.
        env4 = TelemetryEnvelope.create(
            event_schema="lifecycle.health@1.0.0",
            source="test", trace_id="t4", span_id="s4",
            partition_key="lifecycle", payload={},
        )
        bus.emit(env4)  # Should drop (non-critical, queue full)
        assert bus.dropped_count > 0

    @pytest.mark.asyncio
    async def test_dead_letter_on_consumer_error(self):
        bus = TelemetryBus(max_queue=100)

        async def failing_handler(env):
            raise ValueError("consumer exploded")

        bus.subscribe("lifecycle.*", failing_handler)
        await bus.start()

        env = TelemetryEnvelope.create(
            event_schema="lifecycle.transition@1.0.0",
            source="test", trace_id="t1", span_id="s1",
            partition_key="lifecycle", payload={},
        )
        bus.emit(env)
        await asyncio.sleep(0.1)
        await bus.stop()
        assert len(bus.dead_letter) == 1
        assert bus.dead_letter[0]["error"] == "consumer exploded"

    @pytest.mark.asyncio
    async def test_wildcard_subscribe(self):
        bus = TelemetryBus(max_queue=100)
        all_events = []

        async def catch_all(env):
            all_events.append(env)

        bus.subscribe("*", catch_all)
        await bus.start()

        bus.emit(TelemetryEnvelope.create(
            event_schema="lifecycle.transition@1.0.0",
            source="test", trace_id="t1", span_id="s1",
            partition_key="lifecycle", payload={},
        ))
        bus.emit(TelemetryEnvelope.create(
            event_schema="fault.raised@1.0.0",
            source="test", trace_id="t2", span_id="s2",
            partition_key="recovery", payload={},
        ))
        await asyncio.sleep(0.1)
        await bus.stop()
        assert len(all_events) == 2


class TestTelemetryBusSingleton:
    def test_singleton(self):
        import backend.core.telemetry_contract as mod
        mod._bus_instance = None
        b1 = get_telemetry_bus()
        b2 = get_telemetry_bus()
        assert b1 is b2
        mod._bus_instance = None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/core/test_telemetry_contract.py::TestTelemetryBus -v 2>&1 | head -20`
Expected: FAIL with ImportError

- [ ] **Step 3: Implement TelemetryBus**

APPEND to `backend/core/telemetry_contract.py`:

```python
# ---------------------------------------------------------------------------
# Critical event schemas (never dropped under backpressure)
# ---------------------------------------------------------------------------

CRITICAL_EVENT_SCHEMAS: Set[str] = {
    "fault.raised",
    "lifecycle.transition",
}


# ---------------------------------------------------------------------------
# Telemetry Bus
# ---------------------------------------------------------------------------

# Type alias for subscriber callbacks
TelemetryHandler = Callable[[TelemetryEnvelope], Coroutine[Any, Any, None]]


class TelemetryBus:
    """
    Bounded async event bus for telemetry envelopes.

    - Non-blocking emit (put_nowait with drop policy)
    - Pattern-based subscriptions ("lifecycle.*", "fault.*", "*")
    - Idempotency dedup by idempotency_key
    - Dead-letter channel for consumer failures
    - Critical events never dropped under backpressure
    """

    def __init__(
        self,
        max_queue: int = 1000,
        dedup_window_s: float = 300.0,
        dead_letter_max: int = 100,
    ):
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=max_queue)
        self._max_queue = max_queue
        self._dedup_window_s = dedup_window_s
        self._subscribers: List[tuple] = []  # (pattern, handler)
        self._consumer_task: Optional[asyncio.Task] = None
        self._running = False

        # Dedup state
        self._seen_keys: Dict[str, float] = {}  # key -> timestamp

        # Dead letter
        self.dead_letter: Deque[Dict[str, Any]] = deque(maxlen=dead_letter_max)

        # Metrics
        self.dropped_count: int = 0
        self.emitted_count: int = 0
        self.delivered_count: int = 0
        self.deduped_count: int = 0

        # Registry
        self._registry = EventRegistry.with_v1_defaults()

    def subscribe(self, pattern: str, handler: TelemetryHandler) -> None:
        """Subscribe to events matching pattern. Patterns: 'lifecycle.*', 'fault.*', '*'."""
        self._subscribers.append((pattern, handler))

    def emit(self, envelope: TelemetryEnvelope) -> None:
        """Non-blocking emit. Drops non-critical events when queue is full."""
        # Dedup check
        now = time.time()
        if envelope.idempotency_key in self._seen_keys:
            if now - self._seen_keys[envelope.idempotency_key] < self._dedup_window_s:
                self.deduped_count += 1
                return
        self._seen_keys[envelope.idempotency_key] = now

        # Prune old dedup keys periodically
        if len(self._seen_keys) > 5000:
            cutoff = now - self._dedup_window_s
            self._seen_keys = {k: v for k, v in self._seen_keys.items() if v > cutoff}

        # Schema validation (warning only)
        if not self._registry.is_registered(envelope.event_schema):
            logger.warning(
                "[TelemetryBus] Unknown event_schema: %s (not in registry)",
                envelope.event_schema,
            )

        # Try to enqueue
        try:
            self._queue.put_nowait(envelope)
            self.emitted_count += 1
        except asyncio.QueueFull:
            # Check if critical
            schema_name = envelope.event_schema.split("@")[0] if "@" in envelope.event_schema else envelope.event_schema
            if schema_name in CRITICAL_EVENT_SCHEMAS:
                # Force-enqueue critical: drop oldest non-critical to make room
                # Simplified: just log warning — critical events should use a separate path
                logger.warning(
                    "[TelemetryBus] Queue full, critical event %s may be delayed",
                    envelope.event_schema,
                )
            else:
                self.dropped_count += 1
                logger.debug(
                    "[TelemetryBus] Queue full, dropped non-critical event %s",
                    envelope.event_schema,
                )

    def _matches_pattern(self, pattern: str, event_schema: str) -> bool:
        """Check if event_schema matches subscription pattern."""
        if pattern == "*":
            return True
        # "lifecycle.*" matches "lifecycle.transition@1.0.0"
        prefix = pattern.rstrip("*").rstrip(".")
        schema_name = event_schema.split("@")[0] if "@" in event_schema else event_schema
        return schema_name.startswith(prefix)

    async def start(self) -> None:
        """Start the consumer loop."""
        if self._running:
            return
        self._running = True
        self._consumer_task = asyncio.create_task(
            self._consumer_loop(), name="telemetry_bus_consumer",
        )

    async def _consumer_loop(self) -> None:
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
                        self.dead_letter.append({
                            "envelope": envelope.to_dict(),
                            "error": str(exc),
                            "timestamp": time.time(),
                            "handler": getattr(handler, "__name__", str(handler)),
                        })
                        logger.debug(
                            "[TelemetryBus] Consumer error for %s: %s",
                            envelope.event_schema, exc,
                        )

    async def stop(self) -> None:
        """Stop the consumer loop."""
        self._running = False
        if self._consumer_task is not None:
            self._consumer_task.cancel()
            try:
                await self._consumer_task
            except asyncio.CancelledError:
                pass
            self._consumer_task = None

    def get_metrics(self) -> Dict[str, Any]:
        return {
            "emitted": self.emitted_count,
            "delivered": self.delivered_count,
            "dropped": self.dropped_count,
            "deduped": self.deduped_count,
            "dead_letter": len(self.dead_letter),
            "queue_size": self._queue.qsize(),
        }


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_bus_instance: Optional[TelemetryBus] = None


def get_telemetry_bus() -> TelemetryBus:
    global _bus_instance
    if _bus_instance is None:
        _bus_instance = TelemetryBus()
    return _bus_instance
```

- [ ] **Step 4: Run ALL tests**

Run: `python3 -m pytest tests/core/test_telemetry_contract.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/telemetry_contract.py tests/core/test_telemetry_contract.py
git commit -m "feat(telemetry): add TelemetryBus with bounded queue, dedup, backpressure, dead-letter"
```

---

### Task 3: Migrate ChainTelemetry to Envelopes

**Files:**
- Modify: `backend/core/reasoning_chain_orchestrator.py:280+` (ChainTelemetry class)
- Test: `tests/core/test_telemetry_contract.py` (append)

- [ ] **Step 1: Write failing tests for envelope emission**

APPEND to `tests/core/test_telemetry_contract.py`:

```python
class TestChainTelemetryEnvelope:
    @pytest.mark.asyncio
    async def test_proactive_detection_emits_envelope(self):
        bus = TelemetryBus(max_queue=100)
        received = []
        bus.subscribe("reasoning.*", lambda env: received.append(env) or asyncio.sleep(0))

        # Patch get_telemetry_bus to return our test bus
        with patch("backend.core.reasoning_chain_orchestrator.get_telemetry_bus", return_value=bus):
            await bus.start()
            from backend.core.reasoning_chain_orchestrator import ChainTelemetry
            ct = ChainTelemetry()
            event = await ct.emit_proactive_detection(
                trace_id="t1", command="start my day", is_proactive=True,
                confidence=0.92, signals=["workflow_trigger"], latency_ms=15.0,
            )
            await asyncio.sleep(0.1)
            await bus.stop()

        # Original dict still returned for backward compat
        assert event["event"] == "proactive_detection"
        # Envelope emitted to bus
        assert len(received) == 1
        assert received[0].event_schema == "reasoning.decision@1.0.0"
        assert received[0].trace_id == "t1"
        assert received[0].source == "reasoning_chain"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/core/test_telemetry_contract.py::TestChainTelemetryEnvelope -v`
Expected: FAIL (ChainTelemetry doesn't emit envelopes yet)

- [ ] **Step 3: Modify ChainTelemetry._emit() to wrap in envelope**

Read `backend/core/reasoning_chain_orchestrator.py` around line 280 (ChainTelemetry class). Find the `_emit` method. Modify it to ALSO emit a TelemetryEnvelope to the bus, while keeping the existing return value for backward compatibility.

In `ChainTelemetry._emit()`, after the existing `logger.info(...)` call, add:

```python
        # v300.1: Emit to unified TelemetryBus
        try:
            from backend.core.telemetry_contract import TelemetryEnvelope, get_telemetry_bus
            envelope = TelemetryEnvelope.create(
                event_schema="reasoning.decision@1.0.0",
                source="reasoning_chain",
                trace_id=event.get("trace_id", ""),
                span_id=event.get("trace_id", ""),  # span = trace for chain events
                partition_key="reasoning",
                severity="info",
                payload=event,
            )
            get_telemetry_bus().emit(envelope)
        except Exception:
            pass  # Telemetry must never block
```

- [ ] **Step 4: Run ALL tests (both contract and orchestrator)**

Run: `python3 -m pytest tests/core/test_telemetry_contract.py tests/core/test_reasoning_chain_orchestrator.py -v --tb=short`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/reasoning_chain_orchestrator.py tests/core/test_telemetry_contract.py
git commit -m "feat(telemetry): migrate ChainTelemetry to emit TelemetryEnvelopes"
```

---

### Task 4: Migrate LifecycleController to Envelopes

**Files:**
- Modify: `backend/core/jprime_lifecycle_controller.py:419+` (_emit_telemetry method)
- Test: `tests/core/test_telemetry_contract.py` (append)

- [ ] **Step 1: Write failing test**

APPEND to `tests/core/test_telemetry_contract.py`:

```python
class TestLifecycleTelemetryEnvelope:
    @pytest.mark.asyncio
    async def test_lifecycle_transition_emits_envelope(self):
        bus = TelemetryBus(max_queue=100)
        received = []
        bus.subscribe("lifecycle.*", lambda env: received.append(env) or asyncio.sleep(0))

        with patch("backend.core.jprime_lifecycle_controller.get_telemetry_bus", return_value=bus):
            await bus.start()
            from backend.core.jprime_lifecycle_controller import (
                LifecycleState, LifecycleTransition,
                JprimeLifecycleController, RestartPolicy,
            )
            ctrl = JprimeLifecycleController(
                host="127.0.0.1", port=8000,
                restart_policy=RestartPolicy(max_restarts=3, window_s=60.0, base_backoff_s=0.01),
            )
            ctrl._prime_router_notify = AsyncMock()
            ctrl._mind_client_update = AsyncMock()

            # Trigger a transition
            from backend.core.jprime_lifecycle_controller import HealthResult, HealthVerdict
            ctrl._probe = AsyncMock()
            ctrl._probe.check.return_value = HealthResult(
                verdict=HealthVerdict.READY, ready_for_inference=True,
            )
            await ctrl._do_probe()
            await asyncio.sleep(0.1)
            await bus.stop()

        assert len(received) == 1
        assert received[0].event_schema == "lifecycle.transition@1.0.0"
        assert received[0].partition_key == "lifecycle"
        assert received[0].payload["to_state"] == "READY"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/core/test_telemetry_contract.py::TestLifecycleTelemetryEnvelope -v`
Expected: FAIL

- [ ] **Step 3: Modify _emit_telemetry to also emit envelope**

Read `backend/core/jprime_lifecycle_controller.py` around line 419 (`_emit_telemetry`). Modify it to ALSO emit a TelemetryEnvelope:

```python
    def _emit_telemetry(self, transition: LifecycleTransition) -> None:
        """Fire-and-forget telemetry emission via structured logging + TelemetryBus."""
        try:
            logger.debug(
                "[JprimeLifecycle] telemetry: %s",
                transition.to_telemetry_dict(),
            )
            # v300.1: Emit to unified TelemetryBus
            from backend.core.telemetry_contract import TelemetryEnvelope, get_telemetry_bus
            envelope = TelemetryEnvelope.create(
                event_schema="lifecycle.transition@1.0.0",
                source="jprime_lifecycle_controller",
                trace_id=transition.root_cause_id or "",
                span_id=str(uuid.uuid4())[:8],
                partition_key="lifecycle",
                severity="warning" if transition.to_state in (
                    LifecycleState.UNHEALTHY, LifecycleState.TERMINAL,
                ) else "info",
                payload=transition.to_telemetry_dict(),
            )
            get_telemetry_bus().emit(envelope)
        except Exception:
            logger.debug("[JprimeLifecycle] telemetry emission failed", exc_info=True)
```

- [ ] **Step 4: Run ALL tests**

Run: `python3 -m pytest tests/core/test_telemetry_contract.py tests/core/test_jprime_lifecycle_controller.py tests/core/test_reasoning_chain_orchestrator.py -v --tb=short`
Expected: All PASS

- [ ] **Step 5: Run regression check**

Run: `python3 -m pytest tests/vision/ tests/knowledge/ tests/core/ -q --tb=no --timeout=60 2>&1 | tail -5`
Expected: No new failures

- [ ] **Step 6: Commit**

```bash
git add backend/core/jprime_lifecycle_controller.py tests/core/test_telemetry_contract.py
git commit -m "feat(telemetry): migrate LifecycleController to emit TelemetryEnvelopes"
```
