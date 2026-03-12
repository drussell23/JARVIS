# Full Autonomous Loop — C+ Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Close all feedback loops in the Ouroboros governance pipeline so the system operates autonomously with deterministic safety guarantees, using C+ layered architecture (L1=execution, L2/L3/L4=advisory).

**Architecture:** Reuse-first — 11 existing modules adapted/wired, 3 new service hosts created. All advisory layers communicate with L1 via typed CommandEnvelope/EventEnvelope with idempotency, TTL, schema validation, and failure precedence.

**Tech Stack:** Python 3.9+, asyncio, frozen dataclasses, JSON file IPC, bounded asyncio.Queue, existing governance FSM

**Design Doc:** `docs/plans/2026-03-11-full-autonomous-loop-design.md`

---

## Task 1: Shared Infrastructure — Command & Event Envelopes

**Files:**
- Create: `backend/core/ouroboros/governance/autonomy_types.py`
- Test: `tests/governance/autonomy/test_autonomy_types.py`

**Step 1: Write the failing test**

```python
# tests/governance/autonomy/test_autonomy_types.py
"""Tests for C+ command/event envelope types and contract gate."""
import time
import pytest

from backend.core.ouroboros.governance.autonomy_types import (
    CommandEnvelope,
    EventEnvelope,
    CommandType,
    EventType,
    ContractGate,
    IdempotencyLRU,
)


def _cmd(cmd_type: str = "generate_backlog_entry", **overrides) -> CommandEnvelope:
    defaults = dict(
        source_layer=2,
        target_layer=1,
        command_type=cmd_type,
        payload={"description": "fix auth", "target_files": ["auth.py"]},
        ttl_s=300.0,
    )
    defaults.update(overrides)
    return CommandEnvelope(**defaults)


def _evt(evt_type: str = "op_completed", **overrides) -> EventEnvelope:
    defaults = dict(
        source_layer=1,
        event_type=evt_type,
        payload={"op_id": "abc123", "brain_id": "qwen_coder"},
    )
    defaults.update(overrides)
    return EventEnvelope(**defaults)


class TestCommandEnvelope:
    def test_auto_generates_command_id(self):
        cmd = _cmd()
        assert cmd.command_id  # non-empty UUID
        assert len(cmd.command_id) > 10

    def test_auto_generates_idempotency_key(self):
        cmd = _cmd()
        assert cmd.idempotency_key  # deterministic hash

    def test_same_payload_same_idempotency_key(self):
        a = _cmd(payload={"x": 1})
        b = _cmd(payload={"x": 1})
        assert a.idempotency_key == b.idempotency_key

    def test_different_payload_different_key(self):
        a = _cmd(payload={"x": 1})
        b = _cmd(payload={"x": 2})
        assert a.idempotency_key != b.idempotency_key

    def test_is_expired_false_when_fresh(self):
        cmd = _cmd(ttl_s=300.0)
        assert not cmd.is_expired()

    def test_is_expired_true_when_stale(self):
        cmd = _cmd(ttl_s=0.0)  # already expired
        assert cmd.is_expired()

    def test_frozen(self):
        cmd = _cmd()
        with pytest.raises(AttributeError):
            cmd.command_type = "other"  # type: ignore[misc]


class TestEventEnvelope:
    def test_auto_generates_event_id(self):
        evt = _evt()
        assert evt.event_id
        assert len(evt.event_id) > 10

    def test_frozen(self):
        evt = _evt()
        with pytest.raises(AttributeError):
            evt.event_type = "other"  # type: ignore[misc]


class TestContractGate:
    def test_validate_accepts_current_version(self):
        gate = ContractGate(supported_versions={"1.0.0"})
        assert gate.validate("1.0.0") is True

    def test_validate_rejects_unknown_version(self):
        gate = ContractGate(supported_versions={"1.0.0"})
        assert gate.validate("0.9.0") is False


class TestIdempotencyLRU:
    def test_first_seen_returns_false(self):
        lru = IdempotencyLRU(maxsize=10)
        assert lru.seen("key1") is False

    def test_second_seen_returns_true(self):
        lru = IdempotencyLRU(maxsize=10)
        lru.seen("key1")
        assert lru.seen("key1") is True

    def test_eviction_at_capacity(self):
        lru = IdempotencyLRU(maxsize=2)
        lru.seen("a")
        lru.seen("b")
        lru.seen("c")  # evicts "a"
        assert lru.seen("a") is False  # evicted
        assert lru.seen("b") is True
        assert lru.seen("c") is True
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/governance/autonomy/test_autonomy_types.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.core.ouroboros.governance.autonomy_types'`

**Step 3: Write minimal implementation**

```python
# backend/core/ouroboros/governance/autonomy_types.py
"""C+ Layered Architecture — Command & Event Envelopes.

All inter-layer communication uses typed, frozen envelopes with:
- Idempotency keys (deterministic hash of command_type + canonical payload)
- TTL expiry (stale commands silently dropped)
- Schema version validation at layer boundaries
- Failure precedence (safety > execution > optimization > learning)
"""
from __future__ import annotations

import enum
import hashlib
import json
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Set


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class CommandType(str, enum.Enum):
    GENERATE_BACKLOG_ENTRY = "generate_backlog_entry"
    ADJUST_BRAIN_HINT = "adjust_brain_hint"
    REQUEST_MODE_SWITCH = "request_mode_switch"
    REPORT_ROLLBACK_CAUSE = "report_rollback_cause"
    SIGNAL_HUMAN_PRESENCE = "signal_human_presence"
    REQUEST_SAGA_SUBMIT = "request_saga_submit"
    REPORT_CONSENSUS = "report_consensus"
    RECOMMEND_TIER_CHANGE = "recommend_tier_change"


class EventType(str, enum.Enum):
    OP_COMPLETED = "op_completed"
    OP_ROLLED_BACK = "op_rolled_back"
    TRUST_TIER_CHANGED = "trust_tier_changed"
    DEGRADATION_MODE_CHANGED = "degradation_mode_changed"
    HEALTH_PROBE_RESULT = "health_probe_result"
    CURRICULUM_PUBLISHED = "curriculum_published"
    ATTRIBUTION_SCORED = "attribution_scored"
    ROLLBACK_ANALYZED = "rollback_analyzed"
    INCIDENT_DETECTED = "incident_detected"
    SAGA_STATE_CHANGED = "saga_state_changed"


# Failure precedence: lower = higher priority
FAILURE_PRECEDENCE: Dict[str, int] = {
    # Priority 0 — Safety faults from L3
    CommandType.REQUEST_MODE_SWITCH: 0,
    # Priority 1 — L2 optimization
    CommandType.GENERATE_BACKLOG_ENTRY: 2,
    CommandType.ADJUST_BRAIN_HINT: 2,
    CommandType.REPORT_ROLLBACK_CAUSE: 1,
    CommandType.SIGNAL_HUMAN_PRESENCE: 0,
    # Priority 3 — Learning hints
    CommandType.RECOMMEND_TIER_CHANGE: 3,
    CommandType.REPORT_CONSENSUS: 3,
    CommandType.REQUEST_SAGA_SUBMIT: 2,
}


def _canonical_hash(command_type: str, payload: Dict[str, Any]) -> str:
    """Deterministic hash of command_type + sorted JSON payload."""
    raw = json.dumps({"t": command_type, "p": payload}, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


# ---------------------------------------------------------------------------
# Envelopes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CommandEnvelope:
    source_layer: int
    target_layer: int
    command_type: str
    payload: Dict[str, Any]
    ttl_s: float = 300.0
    schema_version: str = "1.0.0"
    command_id: str = field(default="")
    idempotency_key: str = field(default="")
    issued_at_ns: int = field(default=0)

    def __post_init__(self) -> None:
        if not self.command_id:
            object.__setattr__(self, "command_id", str(uuid.uuid4()))
        if not self.issued_at_ns:
            object.__setattr__(self, "issued_at_ns", time.monotonic_ns())
        if not self.idempotency_key:
            object.__setattr__(
                self, "idempotency_key",
                _canonical_hash(self.command_type, self.payload),
            )

    def is_expired(self) -> bool:
        age_s = (time.monotonic_ns() - self.issued_at_ns) / 1e9
        return age_s > self.ttl_s

    @property
    def priority(self) -> int:
        return FAILURE_PRECEDENCE.get(self.command_type, 99)


@dataclass(frozen=True)
class EventEnvelope:
    source_layer: int
    event_type: str
    payload: Dict[str, Any]
    schema_version: str = "1.0.0"
    event_id: str = field(default="")
    emitted_at_ns: int = field(default=0)
    op_id: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.event_id:
            object.__setattr__(self, "event_id", str(uuid.uuid4()))
        if not self.emitted_at_ns:
            object.__setattr__(self, "emitted_at_ns", time.monotonic_ns())


# ---------------------------------------------------------------------------
# Contract Gate
# ---------------------------------------------------------------------------

class ContractGate:
    """Validates schema version at layer boundary crossings."""

    def __init__(self, supported_versions: Set[str] = frozenset({"1.0.0"})) -> None:
        self._supported = set(supported_versions)

    def validate(self, version: str) -> bool:
        return version in self._supported


# ---------------------------------------------------------------------------
# Idempotency LRU
# ---------------------------------------------------------------------------

class IdempotencyLRU:
    """Bounded LRU of seen idempotency keys. Thread-safe for single-thread asyncio."""

    def __init__(self, maxsize: int = 10_000) -> None:
        self._maxsize = maxsize
        self._cache: OrderedDict[str, bool] = OrderedDict()

    def seen(self, key: str) -> bool:
        """Return True if key was already seen (duplicate). Registers key if new."""
        if key in self._cache:
            self._cache.move_to_end(key)
            return True
        self._cache[key] = True
        while len(self._cache) > self._maxsize:
            self._cache.popitem(last=False)
        return False
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/governance/autonomy/test_autonomy_types.py -v`
Expected: PASS (all tests green)

**Step 5: Commit**

```bash
mkdir -p tests/governance/autonomy
touch tests/governance/autonomy/__init__.py
git add backend/core/ouroboros/governance/autonomy_types.py tests/governance/autonomy/test_autonomy_types.py tests/governance/autonomy/__init__.py
git commit -m "feat(autonomy): add C+ command/event envelope types, contract gate, idempotency LRU"
```

---

## Task 2: Command Bus — Priority Queue with Dedup

**Files:**
- Create: `backend/core/ouroboros/governance/command_bus.py`
- Test: `tests/governance/autonomy/test_command_bus.py`

**Step 1: Write the failing test**

```python
# tests/governance/autonomy/test_command_bus.py
"""Tests for the L1 command bus — priority queue with dedup and TTL."""
import asyncio
import pytest

from backend.core.ouroboros.governance.autonomy_types import CommandEnvelope
from backend.core.ouroboros.governance.command_bus import CommandBus


def _cmd(cmd_type: str = "generate_backlog_entry", ttl_s: float = 300.0, **payload_extra) -> CommandEnvelope:
    payload = {"description": "test", **payload_extra}
    return CommandEnvelope(
        source_layer=2, target_layer=1, command_type=cmd_type,
        payload=payload, ttl_s=ttl_s,
    )


@pytest.mark.asyncio
async def test_put_and_get_single_command():
    bus = CommandBus(maxsize=100)
    cmd = _cmd()
    await bus.put(cmd)
    got = await asyncio.wait_for(bus.get(), timeout=1.0)
    assert got.command_id == cmd.command_id


@pytest.mark.asyncio
async def test_priority_ordering_safety_before_optimization():
    bus = CommandBus(maxsize=100)
    opt_cmd = _cmd("generate_backlog_entry")  # priority 2
    safety_cmd = _cmd("request_mode_switch", target_mode="REDUCED_AUTONOMY")  # priority 0
    await bus.put(opt_cmd)
    await bus.put(safety_cmd)
    first = await asyncio.wait_for(bus.get(), timeout=1.0)
    assert first.command_type == "request_mode_switch"


@pytest.mark.asyncio
async def test_duplicate_idempotency_key_dropped():
    bus = CommandBus(maxsize=100)
    cmd1 = _cmd(payload_extra={"x": 1})
    cmd2 = _cmd(payload_extra={"x": 1})  # same idempotency_key
    await bus.put(cmd1)
    await bus.put(cmd2)  # should be silently dropped
    got = await asyncio.wait_for(bus.get(), timeout=1.0)
    assert got.command_id == cmd1.command_id
    assert bus.qsize() == 0


@pytest.mark.asyncio
async def test_expired_command_dropped_on_get():
    bus = CommandBus(maxsize=100)
    cmd = _cmd(ttl_s=0.0)  # already expired
    await bus.put(cmd)
    # get should skip expired and raise TimeoutError (empty queue)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(bus.get(), timeout=0.1)


@pytest.mark.asyncio
async def test_backpressure_when_full():
    bus = CommandBus(maxsize=2)
    await bus.put(_cmd(payload_extra={"i": 1}))
    await bus.put(_cmd(payload_extra={"i": 2}))
    # Third should not block indefinitely — try_put returns False
    assert bus.try_put(_cmd(payload_extra={"i": 3})) is False


@pytest.mark.asyncio
async def test_qsize_reflects_pending():
    bus = CommandBus(maxsize=100)
    assert bus.qsize() == 0
    await bus.put(_cmd())
    assert bus.qsize() == 1
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/governance/autonomy/test_command_bus.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.core.ouroboros.governance.command_bus'`

**Step 3: Write minimal implementation**

```python
# backend/core/ouroboros/governance/command_bus.py
"""Command Bus — Priority Queue with Idempotency Dedup and TTL.

L1 ingests commands from L2/L3/L4 through this bus. Commands are:
- Deduplicated by idempotency_key (bounded LRU)
- Expired by TTL on dequeue
- Ordered by failure precedence (safety > optimization > learning)
"""
from __future__ import annotations

import asyncio
import heapq
import logging
from typing import List

from backend.core.ouroboros.governance.autonomy_types import (
    CommandEnvelope,
    IdempotencyLRU,
)

logger = logging.getLogger("Ouroboros.CommandBus")


class CommandBus:
    """Priority queue for inter-layer commands."""

    def __init__(self, maxsize: int = 1000) -> None:
        self._maxsize = maxsize
        self._heap: List[tuple] = []  # (priority, seq, cmd)
        self._seq = 0
        self._dedup = IdempotencyLRU(maxsize=maxsize * 2)
        self._event = asyncio.Event()

    async def put(self, cmd: CommandEnvelope) -> bool:
        """Enqueue a command. Returns False if duplicate or full."""
        return self.try_put(cmd)

    def try_put(self, cmd: CommandEnvelope) -> bool:
        if len(self._heap) >= self._maxsize:
            logger.warning("[CommandBus] Backpressure: queue full (%d)", self._maxsize)
            return False
        if self._dedup.seen(cmd.idempotency_key):
            logger.debug("[CommandBus] Duplicate dropped: %s", cmd.idempotency_key[:12])
            return False
        self._seq += 1
        heapq.heappush(self._heap, (cmd.priority, self._seq, cmd))
        self._event.set()
        return True

    async def get(self) -> CommandEnvelope:
        """Dequeue highest-priority non-expired command. Blocks if empty."""
        while True:
            while self._heap:
                _, _, cmd = heapq.heappop(self._heap)
                if not self._heap:
                    self._event.clear()
                if cmd.is_expired():
                    logger.debug("[CommandBus] Expired: %s", cmd.command_id[:12])
                    continue
                return cmd
            self._event.clear()
            await self._event.wait()

    def qsize(self) -> int:
        return len(self._heap)
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/governance/autonomy/test_command_bus.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/command_bus.py tests/governance/autonomy/test_command_bus.py
git commit -m "feat(autonomy): add CommandBus — priority queue with dedup, TTL, backpressure"
```

---

## Task 3: Event Emitter — L1 Outcome Events

**Files:**
- Create: `backend/core/ouroboros/governance/event_emitter.py`
- Test: `tests/governance/autonomy/test_event_emitter.py`

**Step 1: Write the failing test**

```python
# tests/governance/autonomy/test_event_emitter.py
"""Tests for L1 event emission to advisory layers."""
import asyncio
import pytest

from backend.core.ouroboros.governance.autonomy_types import EventEnvelope, EventType
from backend.core.ouroboros.governance.event_emitter import EventEmitter


@pytest.mark.asyncio
async def test_subscribe_and_receive():
    emitter = EventEmitter()
    received = []
    emitter.subscribe(EventType.OP_COMPLETED, received.append)
    evt = EventEnvelope(
        source_layer=1, event_type=EventType.OP_COMPLETED,
        payload={"op_id": "op1", "brain_id": "qwen_coder"},
    )
    await emitter.emit(evt)
    assert len(received) == 1
    assert received[0].payload["op_id"] == "op1"


@pytest.mark.asyncio
async def test_multiple_subscribers():
    emitter = EventEmitter()
    a, b = [], []
    emitter.subscribe(EventType.OP_COMPLETED, a.append)
    emitter.subscribe(EventType.OP_COMPLETED, b.append)
    await emitter.emit(EventEnvelope(
        source_layer=1, event_type=EventType.OP_COMPLETED, payload={"op_id": "x"},
    ))
    assert len(a) == 1
    assert len(b) == 1


@pytest.mark.asyncio
async def test_unrelated_event_not_delivered():
    emitter = EventEmitter()
    received = []
    emitter.subscribe(EventType.OP_COMPLETED, received.append)
    await emitter.emit(EventEnvelope(
        source_layer=1, event_type=EventType.OP_ROLLED_BACK, payload={},
    ))
    assert len(received) == 0


@pytest.mark.asyncio
async def test_subscriber_error_isolated():
    emitter = EventEmitter()
    def bad_handler(evt):
        raise RuntimeError("boom")
    good = []
    emitter.subscribe(EventType.OP_COMPLETED, bad_handler)
    emitter.subscribe(EventType.OP_COMPLETED, good.append)
    await emitter.emit(EventEnvelope(
        source_layer=1, event_type=EventType.OP_COMPLETED, payload={},
    ))
    assert len(good) == 1  # bad handler didn't block good handler


@pytest.mark.asyncio
async def test_cursor_tracking():
    emitter = EventEmitter()
    evt1 = EventEnvelope(source_layer=1, event_type=EventType.OP_COMPLETED, payload={"i": 1})
    evt2 = EventEnvelope(source_layer=1, event_type=EventType.OP_COMPLETED, payload={"i": 2})
    await emitter.emit(evt1)
    await emitter.emit(evt2)
    assert emitter.last_event_id == evt2.event_id
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/governance/autonomy/test_event_emitter.py -v`
Expected: FAIL with `ModuleNotFoundError`

**Step 3: Write minimal implementation**

```python
# backend/core/ouroboros/governance/event_emitter.py
"""Event Emitter — L1 publishes append-only facts to advisory layers.

Events are fire-and-forget. Subscriber errors are fault-isolated.
Each subscriber maintains a cursor for replay-on-restart.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from backend.core.ouroboros.governance.autonomy_types import EventEnvelope

logger = logging.getLogger("Ouroboros.EventEmitter")


class EventEmitter:
    """Pub-sub event emitter for governance events."""

    def __init__(self) -> None:
        self._subscribers: Dict[str, List[Callable]] = {}
        self._last_event_id: Optional[str] = None

    def subscribe(self, event_type: str, handler: Callable[[EventEnvelope], Any]) -> None:
        self._subscribers.setdefault(event_type, []).append(handler)

    async def emit(self, event: EventEnvelope) -> None:
        self._last_event_id = event.event_id
        handlers = self._subscribers.get(event.event_type, [])
        for handler in handlers:
            try:
                handler(event)
            except Exception as exc:
                logger.warning(
                    "[EventEmitter] Handler error for %s: %s",
                    event.event_type, exc,
                )

    @property
    def last_event_id(self) -> Optional[str]:
        return self._last_event_id
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/governance/autonomy/test_event_emitter.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/event_emitter.py tests/governance/autonomy/test_event_emitter.py
git commit -m "feat(autonomy): add EventEmitter — pub-sub for L1 outcome events"
```

---

## Task 4: P0 Item 1 — Curriculum → Work Generation (L2)

Reuses: `curriculum_publisher.py` (ACTIVE), `backlog_sensor.py` (ACTIVE).
New: consumer adapter in `feedback_engine.py` that reads curriculum JSON → emits `generate_backlog_entry` commands.

**Files:**
- Create: `backend/core/ouroboros/governance/feedback_engine.py`
- Test: `tests/governance/autonomy/test_feedback_engine_curriculum.py`

**Step 1: Write the failing test**

```python
# tests/governance/autonomy/test_feedback_engine_curriculum.py
"""Tests for L2 curriculum consumption → backlog generation commands."""
import asyncio
import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock

from backend.core.ouroboros.governance.autonomy_types import CommandType
from backend.core.ouroboros.governance.command_bus import CommandBus
from backend.core.ouroboros.governance.feedback_engine import (
    AutonomyFeedbackEngine,
    FeedbackEngineConfig,
)


@pytest.fixture
def event_dir(tmp_path):
    d = tmp_path / "events"
    d.mkdir()
    return d


@pytest.fixture
def state_dir(tmp_path):
    d = tmp_path / "state"
    d.mkdir()
    return d


def _write_curriculum(event_dir: Path, curriculum_id: str = "curr_001") -> Path:
    payload = {
        "schema_version": "curriculum.1",
        "event_type": "curriculum_signal",
        "generated_at": "2026-03-11T10:00:00+00:00",
        "top_k": [
            {"task_type": "bug_fix", "priority": 0.6, "failure_rate": 0.4,
             "sample_size": 10, "confidence": 0.8},
            {"task_type": "testing", "priority": 0.4, "failure_rate": 0.3,
             "sample_size": 8, "confidence": 0.7},
        ],
    }
    path = event_dir / f"curriculum_{curriculum_id}.json"
    path.write_text(json.dumps(payload))
    return path


@pytest.mark.asyncio
async def test_consume_curriculum_generates_backlog_commands(event_dir, state_dir):
    bus = CommandBus(maxsize=100)
    config = FeedbackEngineConfig(event_dir=event_dir, state_dir=state_dir)
    engine = AutonomyFeedbackEngine(command_bus=bus, config=config)

    _write_curriculum(event_dir, "1710000000000")
    await engine.consume_curriculum_once()

    assert bus.qsize() >= 1
    cmd = await asyncio.wait_for(bus.get(), timeout=1.0)
    assert cmd.command_type == CommandType.GENERATE_BACKLOG_ENTRY
    assert "task_type" in cmd.payload


@pytest.mark.asyncio
async def test_duplicate_curriculum_not_reprocessed(event_dir, state_dir):
    bus = CommandBus(maxsize=100)
    config = FeedbackEngineConfig(event_dir=event_dir, state_dir=state_dir)
    engine = AutonomyFeedbackEngine(command_bus=bus, config=config)

    _write_curriculum(event_dir, "1710000000000")
    await engine.consume_curriculum_once()
    count1 = bus.qsize()

    # Process again — should skip already-seen file
    await engine.consume_curriculum_once()
    # Bus should not have additional items beyond what's already there
    assert bus.qsize() == count1


@pytest.mark.asyncio
async def test_cursor_persisted_across_restart(event_dir, state_dir):
    bus = CommandBus(maxsize=100)
    config = FeedbackEngineConfig(event_dir=event_dir, state_dir=state_dir)

    # First engine processes file
    engine1 = AutonomyFeedbackEngine(command_bus=bus, config=config)
    _write_curriculum(event_dir, "1710000000000")
    await engine1.consume_curriculum_once()

    # Drain bus
    while bus.qsize() > 0:
        await asyncio.wait_for(bus.get(), timeout=0.1)

    # Second engine (simulating restart) should not reprocess
    engine2 = AutonomyFeedbackEngine(command_bus=bus, config=config)
    await engine2.consume_curriculum_once()
    assert bus.qsize() == 0
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/governance/autonomy/test_feedback_engine_curriculum.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.core.ouroboros.governance.feedback_engine'`

**Step 3: Write minimal implementation**

```python
# backend/core/ouroboros/governance/feedback_engine.py
"""Autonomy Feedback Engine — L2 Decision Intelligence Service.

Consumes curriculum signals, runs attribution scoring, handles reactor
feedback, and emits brain routing hints. All outputs are advisory
CommandEnvelopes routed to L1 via the CommandBus.

Single-writer invariant: this module NEVER mutates op_context, ledger,
filesystem, or trust tiers directly.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Set

from backend.core.ouroboros.governance.autonomy_types import (
    CommandEnvelope,
    CommandType,
)
from backend.core.ouroboros.governance.command_bus import CommandBus

logger = logging.getLogger("Ouroboros.FeedbackEngine")

_CURSOR_FILE = "feedback_engine_cursor.json"


@dataclass
class FeedbackEngineConfig:
    event_dir: Path = field(default_factory=lambda: Path.home() / ".jarvis" / "reactor_events")
    state_dir: Path = field(default_factory=lambda: Path.home() / ".jarvis" / "ouroboros" / "state")
    max_backlog_entries_per_curriculum: int = 5
    attribution_interval_s: float = 1800.0


class AutonomyFeedbackEngine:
    """L2 — Decision Intelligence. Advisory only."""

    def __init__(
        self,
        command_bus: CommandBus,
        config: Optional[FeedbackEngineConfig] = None,
    ) -> None:
        self._bus = command_bus
        self._config = config or FeedbackEngineConfig()
        self._config.state_dir.mkdir(parents=True, exist_ok=True)
        self._seen_files: Set[str] = set()
        self._load_cursor()

    def _cursor_path(self) -> Path:
        return self._config.state_dir / _CURSOR_FILE

    def _load_cursor(self) -> None:
        path = self._cursor_path()
        if path.exists():
            try:
                data = json.loads(path.read_text())
                self._seen_files = set(data.get("seen_files", []))
            except Exception:
                self._seen_files = set()

    def _save_cursor(self) -> None:
        path = self._cursor_path()
        path.write_text(json.dumps({"seen_files": sorted(self._seen_files)}))

    async def consume_curriculum_once(self) -> int:
        """Scan event_dir for new curriculum files, emit backlog commands. Returns count."""
        count = 0
        if not self._config.event_dir.exists():
            return 0

        for path in sorted(self._config.event_dir.glob("curriculum_*.json")):
            if path.name in self._seen_files:
                continue
            try:
                data = json.loads(path.read_text())
                if data.get("event_type") != "curriculum_signal":
                    continue
                for entry in data.get("top_k", [])[:self._config.max_backlog_entries_per_curriculum]:
                    cmd = CommandEnvelope(
                        source_layer=2,
                        target_layer=1,
                        command_type=CommandType.GENERATE_BACKLOG_ENTRY,
                        payload={
                            "task_type": entry["task_type"],
                            "priority": entry["priority"],
                            "failure_rate": entry.get("failure_rate", 0.0),
                            "source_curriculum_id": path.stem,
                            "description": f"Address {entry['task_type']} failures (rate={entry.get('failure_rate', 0):.0%})",
                            "target_files": [],
                            "repo": "jarvis",
                        },
                    )
                    self._bus.try_put(cmd)
                    count += 1
                self._seen_files.add(path.name)
            except Exception as exc:
                logger.warning("[FeedbackEngine] Failed to parse %s: %s", path.name, exc)

        if count > 0:
            self._save_cursor()
        return count
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/governance/autonomy/test_feedback_engine_curriculum.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/feedback_engine.py tests/governance/autonomy/test_feedback_engine_curriculum.py
git commit -m "feat(autonomy): P0.1 curriculum → backlog generation via FeedbackEngine"
```

---

## Task 5: P0 Item 2 — Model Attribution Scoring Loop (L2)

Reuses: `model_attribution_recorder.py` (ACTIVE), `learning_bridge.py` (REUSABLE).
Adds: periodic scoring method to FeedbackEngine that emits `attribution_scored` events.

**Files:**
- Modify: `backend/core/ouroboros/governance/feedback_engine.py`
- Test: `tests/governance/autonomy/test_feedback_engine_attribution.py`

**Step 1: Write the failing test**

```python
# tests/governance/autonomy/test_feedback_engine_attribution.py
"""Tests for L2 model attribution scoring loop."""
import asyncio
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from dataclasses import dataclass, field
from datetime import datetime, timezone

from backend.core.ouroboros.governance.autonomy_types import EventType
from backend.core.ouroboros.governance.command_bus import CommandBus
from backend.core.ouroboros.governance.event_emitter import EventEmitter
from backend.core.ouroboros.governance.feedback_engine import (
    AutonomyFeedbackEngine,
    FeedbackEngineConfig,
)


@dataclass
class FakeRecord:
    success: bool = True
    latency_ms: float = 100.0
    code_quality_score: float = 0.8
    timestamp: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))


@pytest.fixture
def state_dir(tmp_path):
    d = tmp_path / "state"
    d.mkdir()
    return d


@pytest.fixture
def event_dir(tmp_path):
    d = tmp_path / "events"
    d.mkdir()
    return d


@pytest.mark.asyncio
async def test_score_attribution_emits_event(event_dir, state_dir):
    bus = CommandBus(maxsize=100)
    emitter = EventEmitter()
    config = FeedbackEngineConfig(event_dir=event_dir, state_dir=state_dir)
    engine = AutonomyFeedbackEngine(command_bus=bus, config=config, event_emitter=emitter)

    # Mock persistence
    persistence = AsyncMock()
    persistence.get_records_by_model_and_task = AsyncMock(
        return_value=[FakeRecord(), FakeRecord(), FakeRecord()]
    )
    persistence.get_active_brain_ids = AsyncMock(return_value=["qwen_coder", "qwen_coder_32b"])

    scored_events = []
    emitter.subscribe(EventType.ATTRIBUTION_SCORED, scored_events.append)

    await engine.score_attribution_once(persistence)

    assert len(scored_events) >= 1
    assert scored_events[0].event_type == EventType.ATTRIBUTION_SCORED


@pytest.mark.asyncio
async def test_attribution_fault_isolated(event_dir, state_dir):
    bus = CommandBus(maxsize=100)
    emitter = EventEmitter()
    config = FeedbackEngineConfig(event_dir=event_dir, state_dir=state_dir)
    engine = AutonomyFeedbackEngine(command_bus=bus, config=config, event_emitter=emitter)

    persistence = AsyncMock()
    persistence.get_active_brain_ids = AsyncMock(side_effect=RuntimeError("db down"))

    # Should not raise
    await engine.score_attribution_once(persistence)
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/governance/autonomy/test_feedback_engine_attribution.py -v`
Expected: FAIL (TypeError — `event_emitter` not accepted by `__init__`)

**Step 3: Add attribution scoring to FeedbackEngine**

Modify `feedback_engine.py` — add `event_emitter` param to `__init__` and `score_attribution_once()` method.

In `backend/core/ouroboros/governance/feedback_engine.py`, update `__init__`:

```python
    def __init__(
        self,
        command_bus: CommandBus,
        config: Optional[FeedbackEngineConfig] = None,
        event_emitter: Optional[Any] = None,
    ) -> None:
        self._bus = command_bus
        self._config = config or FeedbackEngineConfig()
        self._config.state_dir.mkdir(parents=True, exist_ok=True)
        self._emitter = event_emitter
        self._seen_files: Set[str] = set()
        self._load_cursor()
```

Add import at top: `from typing import Any, Optional, Set`

Add method after `consume_curriculum_once`:

```python
    async def score_attribution_once(self, persistence: Any) -> None:
        """Score model attribution across active brains. Fault-isolated."""
        if self._emitter is None:
            return
        try:
            brain_ids = await persistence.get_active_brain_ids()
            for brain_id in brain_ids:
                records = await persistence.get_records_by_model_and_task(
                    brain_id, "code_improvement", limit=20,
                )
                if len(records) < 3:
                    continue
                success_rate = sum(float(r.success) for r in records) / len(records)
                from backend.core.ouroboros.governance.autonomy_types import EventEnvelope, EventType
                evt = EventEnvelope(
                    source_layer=2,
                    event_type=EventType.ATTRIBUTION_SCORED,
                    payload={
                        "brain_id": brain_id,
                        "success_rate": success_rate,
                        "sample_size": len(records),
                        "window_hours": 24,
                    },
                )
                await self._emitter.emit(evt)
        except Exception as exc:
            logger.warning("[FeedbackEngine] score_attribution_once failed: %s", exc)
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/governance/autonomy/test_feedback_engine_attribution.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/feedback_engine.py tests/governance/autonomy/test_feedback_engine_attribution.py
git commit -m "feat(autonomy): P0.2 model attribution scoring loop in FeedbackEngine"
```

---

## Task 6: P0 Item 3 — Reactor → Backlog (L2)

Reuses: GLS `_reactor_event_loop` (lines 1852-1862), `_handle_model_promoted` (lines 1930-1944).
Adds: `consume_reactor_events()` in FeedbackEngine that reads reactor JSON and emits backlog commands.

**Files:**
- Modify: `backend/core/ouroboros/governance/feedback_engine.py`
- Test: `tests/governance/autonomy/test_feedback_engine_reactor.py`

**Step 1: Write the failing test**

```python
# tests/governance/autonomy/test_feedback_engine_reactor.py
"""Tests for L2 reactor event consumption → backlog commands."""
import json
import pytest
from pathlib import Path

from backend.core.ouroboros.governance.autonomy_types import CommandType
from backend.core.ouroboros.governance.command_bus import CommandBus
from backend.core.ouroboros.governance.feedback_engine import (
    AutonomyFeedbackEngine,
    FeedbackEngineConfig,
)


@pytest.fixture
def event_dir(tmp_path):
    d = tmp_path / "events"
    d.mkdir()
    return d


@pytest.fixture
def state_dir(tmp_path):
    d = tmp_path / "state"
    d.mkdir()
    return d


def _write_reactor_event(event_dir: Path, event_type: str = "model_promoted", **extra):
    payload = {
        "event_type": event_type,
        "model_id": "qwen-2.5-coder-14b",
        "previous_model_id": "qwen-2.5-coder-7b",
        "training_batch_size": 50,
        **extra,
    }
    path = event_dir / f"reactor_{event_type}_001.json"
    path.write_text(json.dumps(payload))
    return path


@pytest.mark.asyncio
async def test_model_promoted_generates_backlog_command(event_dir, state_dir):
    bus = CommandBus(maxsize=100)
    config = FeedbackEngineConfig(event_dir=event_dir, state_dir=state_dir)
    engine = AutonomyFeedbackEngine(command_bus=bus, config=config)

    _write_reactor_event(event_dir)
    await engine.consume_reactor_events_once()

    assert bus.qsize() >= 1
    cmd = await bus.get()
    assert cmd.command_type == CommandType.GENERATE_BACKLOG_ENTRY
    assert "model_promoted" in cmd.payload.get("source_event", "")


@pytest.mark.asyncio
async def test_unknown_reactor_event_ignored(event_dir, state_dir):
    bus = CommandBus(maxsize=100)
    config = FeedbackEngineConfig(event_dir=event_dir, state_dir=state_dir)
    engine = AutonomyFeedbackEngine(command_bus=bus, config=config)

    _write_reactor_event(event_dir, event_type="unknown_type")
    await engine.consume_reactor_events_once()

    assert bus.qsize() == 0
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/governance/autonomy/test_feedback_engine_reactor.py -v`
Expected: FAIL (`AttributeError: 'AutonomyFeedbackEngine' object has no attribute 'consume_reactor_events_once'`)

**Step 3: Add reactor consumption to FeedbackEngine**

Add to `feedback_engine.py` after `score_attribution_once`:

```python
    async def consume_reactor_events_once(self) -> int:
        """Scan event_dir for reactor events, emit backlog commands. Returns count."""
        count = 0
        if not self._config.event_dir.exists():
            return 0

        for path in sorted(self._config.event_dir.glob("reactor_*.json")):
            if path.name in self._seen_files:
                continue
            try:
                data = json.loads(path.read_text())
                event_type = data.get("event_type", "")

                if event_type == "model_promoted":
                    cmd = CommandEnvelope(
                        source_layer=2,
                        target_layer=1,
                        command_type=CommandType.GENERATE_BACKLOG_ENTRY,
                        payload={
                            "description": f"Validate model promotion: {data.get('model_id', 'unknown')}",
                            "task_type": "code_improvement",
                            "source_event": "model_promoted",
                            "model_id": data.get("model_id"),
                            "previous_model_id": data.get("previous_model_id"),
                            "target_files": [],
                            "repo": "jarvis",
                        },
                    )
                    self._bus.try_put(cmd)
                    count += 1
                else:
                    logger.debug("[FeedbackEngine] Ignoring reactor event: %s", event_type)

                self._seen_files.add(path.name)
            except Exception as exc:
                logger.warning("[FeedbackEngine] Failed to parse %s: %s", path.name, exc)

        if count > 0:
            self._save_cursor()
        return count
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/governance/autonomy/test_feedback_engine_reactor.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/feedback_engine.py tests/governance/autonomy/test_feedback_engine_reactor.py
git commit -m "feat(autonomy): P0.3 reactor → backlog generation via FeedbackEngine"
```

---

## Task 7: P0 Item 4 — Canary → Brain Feedback (L2)

Reuses: `canary_controller.py` (ACTIVE), `model_attribution_recorder.py` (ACTIVE).
Adds: method in FeedbackEngine that listens to `op_completed` events and emits `adjust_brain_hint` commands when canary data shows a brain should be weighted differently.

**Files:**
- Modify: `backend/core/ouroboros/governance/feedback_engine.py`
- Test: `tests/governance/autonomy/test_feedback_engine_brain_hint.py`

**Step 1: Write the failing test**

```python
# tests/governance/autonomy/test_feedback_engine_brain_hint.py
"""Tests for L2 canary outcomes → brain routing hints."""
import pytest

from backend.core.ouroboros.governance.autonomy_types import (
    CommandType,
    EventEnvelope,
    EventType,
)
from backend.core.ouroboros.governance.command_bus import CommandBus
from backend.core.ouroboros.governance.event_emitter import EventEmitter
from backend.core.ouroboros.governance.feedback_engine import (
    AutonomyFeedbackEngine,
    FeedbackEngineConfig,
)


@pytest.fixture
def state_dir(tmp_path):
    d = tmp_path / "state"
    d.mkdir()
    return d


@pytest.fixture
def event_dir(tmp_path):
    d = tmp_path / "events"
    d.mkdir()
    return d


def test_brain_hint_after_enough_canary_failures(event_dir, state_dir):
    bus = CommandBus(maxsize=100)
    emitter = EventEmitter()
    config = FeedbackEngineConfig(event_dir=event_dir, state_dir=state_dir)
    engine = AutonomyFeedbackEngine(command_bus=bus, config=config, event_emitter=emitter)

    # Wire handler
    engine.register_event_handlers(emitter)

    # Simulate 5 failed ops for same brain (threshold = 3)
    for i in range(5):
        evt = EventEnvelope(
            source_layer=1,
            event_type=EventType.OP_ROLLED_BACK,
            payload={
                "op_id": f"op_{i}",
                "brain_id": "qwen_coder_32b",
                "rollback_reason": "validation_failed",
                "phase_at_failure": "VALIDATE",
            },
        )
        emitter._subscribers.get(EventType.OP_ROLLED_BACK, [None])[0](evt)

    # Should have emitted adjust_brain_hint
    assert bus.qsize() >= 1
    cmd = bus._heap[0][2]  # peek at highest-priority
    assert cmd.command_type == CommandType.ADJUST_BRAIN_HINT
    assert cmd.payload["brain_id"] == "qwen_coder_32b"
    assert cmd.payload["weight_delta"] < 0  # downweight


def test_no_hint_below_threshold(event_dir, state_dir):
    bus = CommandBus(maxsize=100)
    emitter = EventEmitter()
    config = FeedbackEngineConfig(event_dir=event_dir, state_dir=state_dir)
    engine = AutonomyFeedbackEngine(command_bus=bus, config=config, event_emitter=emitter)
    engine.register_event_handlers(emitter)

    # Only 1 failure — below threshold
    evt = EventEnvelope(
        source_layer=1,
        event_type=EventType.OP_ROLLED_BACK,
        payload={
            "op_id": "op_0",
            "brain_id": "qwen_coder",
            "rollback_reason": "validation_failed",
            "phase_at_failure": "VALIDATE",
        },
    )
    emitter._subscribers[EventType.OP_ROLLED_BACK][0](evt)

    assert bus.qsize() == 0
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/governance/autonomy/test_feedback_engine_brain_hint.py -v`
Expected: FAIL (`AttributeError: 'AutonomyFeedbackEngine' object has no attribute 'register_event_handlers'`)

**Step 3: Add brain hint logic to FeedbackEngine**

Add to `feedback_engine.py`:

1. Add `from collections import defaultdict` to imports
2. In `__init__`, add: `self._rollback_counts: Dict[str, int] = defaultdict(int)` and `self._brain_hint_threshold = 3`
3. Add method:

```python
    def register_event_handlers(self, emitter: Any) -> None:
        """Subscribe to L1 events for canary feedback."""
        from backend.core.ouroboros.governance.autonomy_types import EventType
        emitter.subscribe(EventType.OP_ROLLED_BACK, self._on_op_rolled_back)
        emitter.subscribe(EventType.OP_COMPLETED, self._on_op_completed)

    def _on_op_rolled_back(self, event: Any) -> None:
        brain_id = event.payload.get("brain_id", "")
        if not brain_id:
            return
        self._rollback_counts[brain_id] += 1
        if self._rollback_counts[brain_id] >= self._brain_hint_threshold:
            cmd = CommandEnvelope(
                source_layer=2,
                target_layer=1,
                command_type=CommandType.ADJUST_BRAIN_HINT,
                payload={
                    "brain_id": brain_id,
                    "weight_delta": -0.1,
                    "evidence_window_ops": self._rollback_counts[brain_id],
                    "canary_slice": "tests/",
                    "reason": f"{self._rollback_counts[brain_id]} rollbacks in window",
                },
            )
            self._bus.try_put(cmd)

    def _on_op_completed(self, event: Any) -> None:
        brain_id = event.payload.get("brain_id", "")
        if brain_id and brain_id in self._rollback_counts:
            # Decay rollback count on success
            self._rollback_counts[brain_id] = max(0, self._rollback_counts[brain_id] - 1)
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/governance/autonomy/test_feedback_engine_brain_hint.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/feedback_engine.py tests/governance/autonomy/test_feedback_engine_brain_hint.py
git commit -m "feat(autonomy): P0.4 canary → brain hint feedback loop in FeedbackEngine"
```

---

## Task 8: P0 Integration — Wire FeedbackEngine into GLS

**Files:**
- Modify: `backend/core/ouroboros/governance/governed_loop_service.py` (lines 560-617, 740-776, 1145-1177)
- Test: `tests/governance/autonomy/test_p0_integration.py`

**Step 1: Write the failing test**

```python
# tests/governance/autonomy/test_p0_integration.py
"""Integration test: GLS emits events → FeedbackEngine → commands → GLS ingests."""
import asyncio
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from backend.core.ouroboros.governance.autonomy_types import EventType
from backend.core.ouroboros.governance.command_bus import CommandBus
from backend.core.ouroboros.governance.event_emitter import EventEmitter
from backend.core.ouroboros.governance.feedback_engine import (
    AutonomyFeedbackEngine,
    FeedbackEngineConfig,
)


@pytest.mark.asyncio
async def test_op_completed_event_flows_to_feedback_engine(tmp_path):
    """Verify: op completes → event emitted → FeedbackEngine receives it."""
    bus = CommandBus(maxsize=100)
    emitter = EventEmitter()
    config = FeedbackEngineConfig(
        event_dir=tmp_path / "events",
        state_dir=tmp_path / "state",
    )
    (tmp_path / "events").mkdir()
    (tmp_path / "state").mkdir()
    engine = AutonomyFeedbackEngine(command_bus=bus, config=config, event_emitter=emitter)
    engine.register_event_handlers(emitter)

    received = []
    emitter.subscribe(EventType.OP_COMPLETED, received.append)

    from backend.core.ouroboros.governance.autonomy_types import EventEnvelope
    evt = EventEnvelope(
        source_layer=1,
        event_type=EventType.OP_COMPLETED,
        payload={"op_id": "test_op", "brain_id": "qwen_coder", "terminal_phase": "COMPLETE"},
    )
    await emitter.emit(evt)

    assert len(received) == 1


@pytest.mark.asyncio
async def test_command_bus_drains_cleanly(tmp_path):
    """Verify: commands put by FeedbackEngine can be consumed."""
    bus = CommandBus(maxsize=100)
    config = FeedbackEngineConfig(
        event_dir=tmp_path / "events",
        state_dir=tmp_path / "state",
    )
    (tmp_path / "events").mkdir()
    (tmp_path / "state").mkdir()
    engine = AutonomyFeedbackEngine(command_bus=bus, config=config)

    # Write curriculum file
    import json
    (tmp_path / "events" / "curriculum_001.json").write_text(json.dumps({
        "event_type": "curriculum_signal",
        "schema_version": "curriculum.1",
        "generated_at": "2026-03-11T00:00:00Z",
        "top_k": [{"task_type": "bug_fix", "priority": 1.0, "failure_rate": 0.5,
                    "sample_size": 10, "confidence": 0.9}],
    }))

    await engine.consume_curriculum_once()
    assert bus.qsize() == 1
    cmd = await asyncio.wait_for(bus.get(), timeout=1.0)
    assert cmd.payload["task_type"] == "bug_fix"
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/governance/autonomy/test_p0_integration.py -v`
Expected: PASS (this is an integration test using already-built components)

**Step 3: Wire into GLS**

Modify `governed_loop_service.py`:

1. **Add imports** after line 66:
```python
from backend.core.ouroboros.governance.command_bus import CommandBus
from backend.core.ouroboros.governance.event_emitter import EventEmitter
from backend.core.ouroboros.governance.feedback_engine import (
    AutonomyFeedbackEngine,
    FeedbackEngineConfig,
)
```

2. **Add attributes** in `__init__` after line 607 (after `self._oracle`):
```python
        # C+ autonomy infrastructure
        self._command_bus: Optional[CommandBus] = None
        self._event_emitter: Optional[EventEmitter] = None
        self._feedback_engine: Optional[AutonomyFeedbackEngine] = None
        self._command_consumer_task: Optional[asyncio.Task] = None
        self._feedback_loop_task: Optional[asyncio.Task] = None
```

3. **Initialize in `start()`** after line 776 (after health_probe_task creation):
```python
            # C+ L2: FeedbackEngine + CommandBus
            self._command_bus = CommandBus(maxsize=1000)
            self._event_emitter = EventEmitter()
            fe_config = FeedbackEngineConfig(
                event_dir=self._event_dir or Path.home() / ".jarvis" / "reactor_events",
                state_dir=Path(os.environ.get(
                    "JARVIS_AUTONOMY_STATE_DIR",
                    str(Path.home() / ".jarvis" / "ouroboros" / "state"),
                )),
            )
            self._feedback_engine = AutonomyFeedbackEngine(
                command_bus=self._command_bus,
                config=fe_config,
                event_emitter=self._event_emitter,
            )
            self._feedback_engine.register_event_handlers(self._event_emitter)
            self._feedback_loop_task = asyncio.create_task(
                self._feedback_loop(), name="feedback_loop"
            )
            self._command_consumer_task = asyncio.create_task(
                self._command_consumer_loop(), name="command_consumer_loop"
            )
```

4. **Add emit after op completes** — after line 1177 (after `return result`), insert before the `finally`:
```python
            # C+ L1: Emit op_completed event to advisory layers
            if self._event_emitter is not None:
                try:
                    from backend.core.ouroboros.governance.autonomy_types import EventEnvelope, EventType as AutonomyEventType
                    await self._event_emitter.emit(EventEnvelope(
                        source_layer=1,
                        event_type=AutonomyEventType.OP_COMPLETED,
                        payload={
                            "op_id": ctx.op_id,
                            "brain_id": brain.brain_id,
                            "model_name": brain.model_name,
                            "terminal_phase": result.terminal_phase.name,
                            "provider": result.provider_used or "",
                            "duration_s": result.total_duration_s or 0.0,
                            "rollback": False,
                        },
                    ))
                except Exception:
                    pass
```

5. **Add background loop methods** after `_handle_model_promoted` (after line 1945):
```python
    async def _feedback_loop(self) -> None:
        """Periodically run FeedbackEngine consumption loops."""
        while True:
            try:
                await asyncio.sleep(60.0)  # 1 minute poll
                if self._feedback_engine:
                    await self._feedback_engine.consume_curriculum_once()
                    await self._feedback_engine.consume_reactor_events_once()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning("[GovernedLoop] feedback_loop error: %s", exc)

    async def _command_consumer_loop(self) -> None:
        """Consume commands from advisory layers and route to L1 handlers."""
        while True:
            try:
                if self._command_bus is None:
                    await asyncio.sleep(5.0)
                    continue
                cmd = await asyncio.wait_for(self._command_bus.get(), timeout=5.0)
                await self._handle_command(cmd)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning("[GovernedLoop] command_consumer error: %s", exc)

    async def _handle_command(self, cmd: Any) -> None:
        """Route a command envelope to the appropriate L1 handler."""
        from backend.core.ouroboros.governance.autonomy_types import CommandType
        ct = cmd.command_type
        if ct == CommandType.GENERATE_BACKLOG_ENTRY:
            logger.info("[GovernedLoop] L2 backlog command: %s", cmd.payload.get("description", "")[:80])
            # Future: write to backlog.json for IntakeLayerService
        elif ct == CommandType.ADJUST_BRAIN_HINT:
            logger.info("[GovernedLoop] L2 brain hint: %s weight_delta=%s",
                        cmd.payload.get("brain_id"), cmd.payload.get("weight_delta"))
            # Future: pass to brain_selector.adjust_weights()
        else:
            logger.debug("[GovernedLoop] Unhandled command: %s", ct)
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/governance/autonomy/test_p0_integration.py -v`
Expected: PASS

**Step 5: Run full existing test suite to check for regressions**

Run: `python3 -m pytest tests/governance/ -x --timeout=30 -q`
Expected: All previously passing tests still pass (9 known pre-existing failures excluded)

**Step 6: Commit**

```bash
git add backend/core/ouroboros/governance/governed_loop_service.py tests/governance/autonomy/test_p0_integration.py
git commit -m "feat(autonomy): wire P0 FeedbackEngine + CommandBus + EventEmitter into GLS"
```

---

## Task 9: P1 Item 5 — Health Escalation (L3)

Reuses: GLS `_health_probe_loop` (lines 1805-1836), `degradation.py` (ACTIVE).
New: `safety_net.py` with `ProductionSafetyNet` service that tracks consecutive probe failures and emits `request_mode_switch` commands.

**Files:**
- Create: `backend/core/ouroboros/governance/safety_net.py`
- Test: `tests/governance/autonomy/test_safety_net_health.py`

**Step 1: Write the failing test**

```python
# tests/governance/autonomy/test_safety_net_health.py
"""Tests for L3 health probe escalation."""
import pytest

from backend.core.ouroboros.governance.autonomy_types import CommandType, EventEnvelope, EventType
from backend.core.ouroboros.governance.command_bus import CommandBus
from backend.core.ouroboros.governance.event_emitter import EventEmitter
from backend.core.ouroboros.governance.safety_net import (
    ProductionSafetyNet,
    SafetyNetConfig,
)


@pytest.fixture
def bus():
    return CommandBus(maxsize=100)


@pytest.fixture
def emitter():
    return EventEmitter()


def _probe_event(success: bool, consecutive_failures: int = 0) -> EventEnvelope:
    return EventEnvelope(
        source_layer=1,
        event_type=EventType.HEALTH_PROBE_RESULT,
        payload={
            "provider": "gcp-jprime",
            "success": success,
            "latency_ms": 50.0,
            "consecutive_failures": consecutive_failures,
        },
    )


class TestHealthEscalation:
    def test_no_escalation_on_single_failure(self, bus, emitter):
        config = SafetyNetConfig(probe_failure_escalation_threshold=3)
        net = ProductionSafetyNet(command_bus=bus, config=config)
        net.register_event_handlers(emitter)

        net._on_health_probe(_probe_event(success=False, consecutive_failures=1))
        assert bus.qsize() == 0

    def test_escalation_at_threshold(self, bus, emitter):
        config = SafetyNetConfig(probe_failure_escalation_threshold=3)
        net = ProductionSafetyNet(command_bus=bus, config=config)
        net.register_event_handlers(emitter)

        for i in range(3):
            net._on_health_probe(_probe_event(success=False, consecutive_failures=i + 1))

        assert bus.qsize() >= 1
        cmd = bus._heap[0][2]
        assert cmd.command_type == CommandType.REQUEST_MODE_SWITCH
        assert cmd.payload["target_mode"] == "REDUCED_AUTONOMY"

    def test_severe_escalation_at_5_failures(self, bus, emitter):
        config = SafetyNetConfig(
            probe_failure_escalation_threshold=3,
            probe_failure_severe_threshold=5,
        )
        net = ProductionSafetyNet(command_bus=bus, config=config)
        net.register_event_handlers(emitter)

        for i in range(5):
            net._on_health_probe(_probe_event(success=False, consecutive_failures=i + 1))

        # Should have both REDUCED and READ_ONLY commands
        cmds = []
        while bus.qsize() > 0:
            import asyncio
            cmds.append(asyncio.get_event_loop().run_until_complete(bus.get()))
        modes = [c.payload["target_mode"] for c in cmds]
        assert "READ_ONLY_PLANNING" in modes

    def test_success_resets_failure_count(self, bus, emitter):
        config = SafetyNetConfig(probe_failure_escalation_threshold=3)
        net = ProductionSafetyNet(command_bus=bus, config=config)
        net.register_event_handlers(emitter)

        net._on_health_probe(_probe_event(success=False, consecutive_failures=1))
        net._on_health_probe(_probe_event(success=False, consecutive_failures=2))
        net._on_health_probe(_probe_event(success=True, consecutive_failures=0))
        net._on_health_probe(_probe_event(success=False, consecutive_failures=1))
        # Reset after success, so only 1 consecutive failure — no escalation
        assert bus.qsize() == 0
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/governance/autonomy/test_safety_net_health.py -v`
Expected: FAIL with `ModuleNotFoundError`

**Step 3: Write minimal implementation**

```python
# backend/core/ouroboros/governance/safety_net.py
"""Production Safety Net — L3 Safety & Reliability Service.

Monitors health probes, analyzes rollback patterns, detects incidents,
and signals human presence. All outputs are advisory CommandEnvelopes
routed to L1 via the CommandBus.

Single-writer invariant: this module NEVER mutates op_context, ledger,
filesystem, or trust tiers directly.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from backend.core.ouroboros.governance.autonomy_types import (
    CommandEnvelope,
    CommandType,
    EventType,
)
from backend.core.ouroboros.governance.command_bus import CommandBus

logger = logging.getLogger("Ouroboros.SafetyNet")


@dataclass
class SafetyNetConfig:
    probe_failure_escalation_threshold: int = 3
    probe_failure_severe_threshold: int = 5
    rollback_pattern_threshold: int = 2
    rollback_pattern_window_s: float = 3600.0
    human_presence_defer_s: float = 300.0


class ProductionSafetyNet:
    """L3 — Safety & Reliability. Advisory only."""

    def __init__(
        self,
        command_bus: CommandBus,
        config: Optional[SafetyNetConfig] = None,
    ) -> None:
        self._bus = command_bus
        self._config = config or SafetyNetConfig()
        self._consecutive_failures = 0
        self._escalated_reduced = False
        self._escalated_readonly = False
        self._rollback_history: List[Dict[str, Any]] = []

    def register_event_handlers(self, emitter: Any) -> None:
        emitter.subscribe(EventType.HEALTH_PROBE_RESULT, self._on_health_probe)
        emitter.subscribe(EventType.OP_ROLLED_BACK, self._on_rollback)

    def _on_health_probe(self, event: Any) -> None:
        payload = event.payload
        if payload.get("success"):
            self._consecutive_failures = 0
            self._escalated_reduced = False
            self._escalated_readonly = False
            return

        self._consecutive_failures += 1

        if (self._consecutive_failures >= self._config.probe_failure_severe_threshold
                and not self._escalated_readonly):
            self._escalated_readonly = True
            cmd = CommandEnvelope(
                source_layer=3,
                target_layer=1,
                command_type=CommandType.REQUEST_MODE_SWITCH,
                payload={
                    "target_mode": "READ_ONLY_PLANNING",
                    "reason": f"{self._consecutive_failures} consecutive probe failures",
                    "evidence_count": self._consecutive_failures,
                    "probe_failure_streak": self._consecutive_failures,
                },
            )
            self._bus.try_put(cmd)

        elif (self._consecutive_failures >= self._config.probe_failure_escalation_threshold
              and not self._escalated_reduced):
            self._escalated_reduced = True
            cmd = CommandEnvelope(
                source_layer=3,
                target_layer=1,
                command_type=CommandType.REQUEST_MODE_SWITCH,
                payload={
                    "target_mode": "REDUCED_AUTONOMY",
                    "reason": f"{self._consecutive_failures} consecutive probe failures",
                    "evidence_count": self._consecutive_failures,
                    "probe_failure_streak": self._consecutive_failures,
                },
            )
            self._bus.try_put(cmd)

    def _on_rollback(self, event: Any) -> None:
        """Track rollback patterns for root cause analysis."""
        import time
        self._rollback_history.append({
            "op_id": event.payload.get("op_id"),
            "brain_id": event.payload.get("brain_id"),
            "reason": event.payload.get("rollback_reason"),
            "ts": time.monotonic(),
        })
        # Prune old entries
        now = time.monotonic()
        self._rollback_history = [
            r for r in self._rollback_history
            if now - r["ts"] < self._config.rollback_pattern_window_s
        ]
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/governance/autonomy/test_safety_net_health.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/safety_net.py tests/governance/autonomy/test_safety_net_health.py
git commit -m "feat(autonomy): P1.5 health probe escalation in ProductionSafetyNet"
```

---

## Task 10: P1 Item 6 — Rollback Root Cause Analysis (L3)

Reuses: `error_recovery.py` ErrorCategory/ErrorSeverity enums, `learning_bridge.py`.
Adds: rollback pattern detection in SafetyNet that emits `report_rollback_cause` commands.

**Files:**
- Modify: `backend/core/ouroboros/governance/safety_net.py`
- Test: `tests/governance/autonomy/test_safety_net_rollback.py`

**Step 1: Write the failing test**

```python
# tests/governance/autonomy/test_safety_net_rollback.py
"""Tests for L3 rollback root cause analysis."""
import time
import pytest

from backend.core.ouroboros.governance.autonomy_types import (
    CommandType,
    EventEnvelope,
    EventType,
)
from backend.core.ouroboros.governance.command_bus import CommandBus
from backend.core.ouroboros.governance.event_emitter import EventEmitter
from backend.core.ouroboros.governance.safety_net import ProductionSafetyNet, SafetyNetConfig


def _rollback_event(op_id: str, brain_id: str = "qwen_coder", reason: str = "validation_failed") -> EventEnvelope:
    return EventEnvelope(
        source_layer=1,
        event_type=EventType.OP_ROLLED_BACK,
        payload={
            "op_id": op_id,
            "brain_id": brain_id,
            "rollback_reason": reason,
            "affected_files": ["auth.py"],
            "phase_at_failure": "VALIDATE",
        },
    )


class TestRollbackRootCause:
    def test_single_rollback_analyzed(self):
        bus = CommandBus(maxsize=100)
        emitter = EventEmitter()
        config = SafetyNetConfig(rollback_pattern_threshold=2)
        net = ProductionSafetyNet(command_bus=bus, config=config)
        net.register_event_handlers(emitter)

        net._on_rollback(_rollback_event("op1"))

        assert bus.qsize() >= 1
        cmd = bus._heap[0][2]
        assert cmd.command_type == CommandType.REPORT_ROLLBACK_CAUSE
        assert cmd.payload["op_id"] == "op1"

    def test_pattern_detected_same_reason(self):
        bus = CommandBus(maxsize=100)
        emitter = EventEmitter()
        config = SafetyNetConfig(rollback_pattern_threshold=2)
        net = ProductionSafetyNet(command_bus=bus, config=config)
        net.register_event_handlers(emitter)

        net._on_rollback(_rollback_event("op1", reason="validation_failed"))
        net._on_rollback(_rollback_event("op2", reason="validation_failed"))

        # Second rollback should trigger pattern match + incident
        cmds = []
        while bus._heap:
            _, _, cmd = bus._heap.pop(0)
            cmds.append(cmd)
        cause_cmds = [c for c in cmds if c.command_type == CommandType.REPORT_ROLLBACK_CAUSE]
        assert len(cause_cmds) >= 2
        # At least one should have pattern_match=True
        assert any(c.payload.get("pattern_match") for c in cause_cmds)
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/governance/autonomy/test_safety_net_rollback.py -v`
Expected: FAIL (rollback handler doesn't emit commands yet)

**Step 3: Enhance `_on_rollback` in safety_net.py**

Replace the `_on_rollback` method:

```python
    def _on_rollback(self, event: Any) -> None:
        """Analyze rollback, emit root cause report, detect patterns."""
        import time
        payload = event.payload
        op_id = payload.get("op_id", "unknown")
        reason = payload.get("rollback_reason", "unknown")
        brain_id = payload.get("brain_id", "unknown")

        now = time.monotonic()
        self._rollback_history.append({
            "op_id": op_id,
            "brain_id": brain_id,
            "reason": reason,
            "ts": now,
        })

        # Prune old entries
        self._rollback_history = [
            r for r in self._rollback_history
            if now - r["ts"] < self._config.rollback_pattern_window_s
        ]

        # Check for pattern: same reason repeated
        same_reason = [r for r in self._rollback_history if r["reason"] == reason]
        pattern_match = len(same_reason) >= self._config.rollback_pattern_threshold

        # Classify root cause
        root_cause_class = self._classify_root_cause(reason)

        cmd = CommandEnvelope(
            source_layer=3,
            target_layer=2,  # L3 → L2 attribution
            command_type=CommandType.REPORT_ROLLBACK_CAUSE,
            payload={
                "op_id": op_id,
                "root_cause_class": root_cause_class,
                "affected_files": payload.get("affected_files", []),
                "model_used": brain_id,
                "pattern_match": pattern_match,
                "similar_op_ids": [r["op_id"] for r in same_reason if r["op_id"] != op_id],
            },
        )
        self._bus.try_put(cmd)

    @staticmethod
    def _classify_root_cause(reason: str) -> str:
        """Classify rollback reason into root cause category."""
        reason_lower = reason.lower()
        if "validation" in reason_lower or "validate" in reason_lower:
            return "validation_failure"
        if "timeout" in reason_lower:
            return "timeout"
        if "syntax" in reason_lower or "parse" in reason_lower:
            return "syntax_error"
        if "test" in reason_lower:
            return "test_failure"
        if "permission" in reason_lower or "access" in reason_lower:
            return "permission_error"
        return "unknown"
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/governance/autonomy/test_safety_net_rollback.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/safety_net.py tests/governance/autonomy/test_safety_net_rollback.py
git commit -m "feat(autonomy): P1.6 rollback root cause analysis in ProductionSafetyNet"
```

---

## Task 11: P1 Item 7 — Incident Auto-Trigger (L3)

Reuses: `degradation.py` (ACTIVE), `break_glass.py` (ACTIVE).
Adds: incident detection that emits `request_mode_switch` when rollback patterns exceed threshold.

**Files:**
- Modify: `backend/core/ouroboros/governance/safety_net.py`
- Test: `tests/governance/autonomy/test_safety_net_incident.py`

**Step 1: Write the failing test**

```python
# tests/governance/autonomy/test_safety_net_incident.py
"""Tests for L3 incident auto-detection → mode switch."""
import pytest

from backend.core.ouroboros.governance.autonomy_types import (
    CommandType,
    EventEnvelope,
    EventType,
)
from backend.core.ouroboros.governance.command_bus import CommandBus
from backend.core.ouroboros.governance.event_emitter import EventEmitter
from backend.core.ouroboros.governance.safety_net import ProductionSafetyNet, SafetyNetConfig


def _rollback(op_id: str, reason: str = "validation_failed") -> EventEnvelope:
    return EventEnvelope(
        source_layer=1,
        event_type=EventType.OP_ROLLED_BACK,
        payload={
            "op_id": op_id,
            "brain_id": "qwen_coder",
            "rollback_reason": reason,
            "affected_files": ["auth.py"],
            "phase_at_failure": "VALIDATE",
        },
    )


class TestIncidentAutoTrigger:
    def test_three_rollbacks_triggers_incident(self):
        bus = CommandBus(maxsize=100)
        config = SafetyNetConfig(
            rollback_pattern_threshold=2,
            incident_rollback_threshold=3,
        )
        net = ProductionSafetyNet(command_bus=bus, config=config)
        emitter = EventEmitter()
        net.register_event_handlers(emitter)

        for i in range(3):
            net._on_rollback(_rollback(f"op_{i}"))

        # Should have incident-triggered mode switch
        cmds = []
        while bus._heap:
            _, _, cmd = bus._heap.pop(0)
            cmds.append(cmd)
        mode_cmds = [c for c in cmds if c.command_type == CommandType.REQUEST_MODE_SWITCH]
        assert len(mode_cmds) >= 1
        assert any("incident" in c.payload.get("reason", "").lower() for c in mode_cmds)

    def test_incident_not_triggered_below_threshold(self):
        bus = CommandBus(maxsize=100)
        config = SafetyNetConfig(incident_rollback_threshold=5)
        net = ProductionSafetyNet(command_bus=bus, config=config)
        emitter = EventEmitter()
        net.register_event_handlers(emitter)

        for i in range(2):
            net._on_rollback(_rollback(f"op_{i}"))

        cmds = []
        while bus._heap:
            _, _, cmd = bus._heap.pop(0)
            cmds.append(cmd)
        mode_cmds = [c for c in cmds if c.command_type == CommandType.REQUEST_MODE_SWITCH]
        assert len(mode_cmds) == 0
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/governance/autonomy/test_safety_net_incident.py -v`
Expected: FAIL (`SafetyNetConfig` has no `incident_rollback_threshold`)

**Step 3: Add incident detection to SafetyNet**

1. Add to `SafetyNetConfig`:
```python
    incident_rollback_threshold: int = 3
```

2. Add to `__init__`:
```python
        self._incident_triggered = False
```

3. Add to end of `_on_rollback` method (after the REPORT_ROLLBACK_CAUSE command):
```python
        # Incident detection: too many rollbacks in window
        if (len(self._rollback_history) >= self._config.incident_rollback_threshold
                and not self._incident_triggered):
            self._incident_triggered = True
            incident_cmd = CommandEnvelope(
                source_layer=3,
                target_layer=1,
                command_type=CommandType.REQUEST_MODE_SWITCH,
                payload={
                    "target_mode": "READ_ONLY_PLANNING",
                    "reason": f"Incident: {len(self._rollback_history)} rollbacks in {self._config.rollback_pattern_window_s}s",
                    "evidence_count": len(self._rollback_history),
                    "probe_failure_streak": 0,
                },
            )
            self._bus.try_put(incident_cmd)
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/governance/autonomy/test_safety_net_incident.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/safety_net.py tests/governance/autonomy/test_safety_net_incident.py
git commit -m "feat(autonomy): P1.7 incident auto-trigger in ProductionSafetyNet"
```

---

## Task 12: P1 Item 8 — Human Presence Signal (L3)

Reuses: `intervention_decision_engine.py` (REUSABLE) concepts.
Adds: human presence handler that emits `signal_human_presence` commands.

**Files:**
- Modify: `backend/core/ouroboros/governance/safety_net.py`
- Test: `tests/governance/autonomy/test_safety_net_human.py`

**Step 1: Write the failing test**

```python
# tests/governance/autonomy/test_safety_net_human.py
"""Tests for L3 human presence signal."""
import pytest

from backend.core.ouroboros.governance.autonomy_types import CommandType
from backend.core.ouroboros.governance.command_bus import CommandBus
from backend.core.ouroboros.governance.safety_net import ProductionSafetyNet, SafetyNetConfig


class TestHumanPresence:
    def test_signal_active_emits_command(self):
        bus = CommandBus(maxsize=100)
        net = ProductionSafetyNet(command_bus=bus)
        net.signal_human_presence(is_active=True, activity_type="keyboard")

        assert bus.qsize() == 1
        cmd = bus._heap[0][2]
        assert cmd.command_type == CommandType.SIGNAL_HUMAN_PRESENCE
        assert cmd.payload["is_active"] is True
        assert cmd.payload["activity_type"] == "keyboard"

    def test_signal_inactive_emits_command(self):
        bus = CommandBus(maxsize=100)
        net = ProductionSafetyNet(command_bus=bus)
        net.signal_human_presence(is_active=False, activity_type="idle")

        assert bus.qsize() == 1
        cmd = bus._heap[0][2]
        assert cmd.payload["is_active"] is False

    def test_idempotent_same_state(self):
        bus = CommandBus(maxsize=100)
        net = ProductionSafetyNet(command_bus=bus)
        net.signal_human_presence(is_active=True, activity_type="keyboard")
        net.signal_human_presence(is_active=True, activity_type="keyboard")

        # Idempotency should dedup
        assert bus.qsize() == 1
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/governance/autonomy/test_safety_net_human.py -v`
Expected: FAIL (`ProductionSafetyNet has no attribute 'signal_human_presence'`)

**Step 3: Add human presence to SafetyNet**

Add to `safety_net.py` class:

```python
    def signal_human_presence(self, is_active: bool, activity_type: str = "unknown") -> None:
        """Signal human presence to L1 submit gate."""
        import time
        defer_until = time.monotonic_ns() + int(self._config.human_presence_defer_s * 1e9) if is_active else 0
        cmd = CommandEnvelope(
            source_layer=3,
            target_layer=1,
            command_type=CommandType.SIGNAL_HUMAN_PRESENCE,
            payload={
                "is_active": is_active,
                "activity_type": activity_type,
                "defer_until_ns": defer_until,
            },
        )
        self._bus.try_put(cmd)
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/governance/autonomy/test_safety_net_human.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/safety_net.py tests/governance/autonomy/test_safety_net_human.py
git commit -m "feat(autonomy): P1.8 human presence signal in ProductionSafetyNet"
```

---

## Task 13: P1 Integration — Wire SafetyNet into GLS

**Files:**
- Modify: `backend/core/ouroboros/governance/governed_loop_service.py`
- Test: `tests/governance/autonomy/test_p1_integration.py`

**Step 1: Write the failing test**

```python
# tests/governance/autonomy/test_p1_integration.py
"""Integration: health probe failure → SafetyNet → mode switch command → GLS handles."""
import pytest

from backend.core.ouroboros.governance.autonomy_types import (
    CommandType,
    EventEnvelope,
    EventType,
)
from backend.core.ouroboros.governance.command_bus import CommandBus
from backend.core.ouroboros.governance.event_emitter import EventEmitter
from backend.core.ouroboros.governance.safety_net import ProductionSafetyNet, SafetyNetConfig


@pytest.mark.asyncio
async def test_health_failure_escalation_flow():
    bus = CommandBus(maxsize=100)
    emitter = EventEmitter()
    config = SafetyNetConfig(probe_failure_escalation_threshold=2)
    net = ProductionSafetyNet(command_bus=bus, config=config)
    net.register_event_handlers(emitter)

    # Simulate L1 emitting health probe failures
    for i in range(2):
        await emitter.emit(EventEnvelope(
            source_layer=1,
            event_type=EventType.HEALTH_PROBE_RESULT,
            payload={"provider": "gcp-jprime", "success": False,
                     "latency_ms": 0, "consecutive_failures": i + 1},
        ))

    # SafetyNet should have enqueued a mode switch command
    assert bus.qsize() >= 1
    cmd = await bus.get()
    assert cmd.command_type == CommandType.REQUEST_MODE_SWITCH
    assert cmd.payload["target_mode"] == "REDUCED_AUTONOMY"
```

**Step 2: Run test to verify it passes**

Run: `python3 -m pytest tests/governance/autonomy/test_p1_integration.py -v`
Expected: PASS (uses already-built components)

**Step 3: Wire SafetyNet into GLS `start()` and health probe**

In `governed_loop_service.py`:

1. Add import after the feedback_engine imports:
```python
from backend.core.ouroboros.governance.safety_net import (
    ProductionSafetyNet,
    SafetyNetConfig,
)
```

2. Add attribute in `__init__` after feedback_engine attributes:
```python
        self._safety_net: Optional[ProductionSafetyNet] = None
```

3. In `start()` after FeedbackEngine initialization, add:
```python
            # C+ L3: ProductionSafetyNet
            self._safety_net = ProductionSafetyNet(
                command_bus=self._command_bus,
                config=SafetyNetConfig(),
            )
            self._safety_net.register_event_handlers(self._event_emitter)
```

4. In `_health_probe_loop` after each probe result (after line 1821 for success, after line 1826/1830 for failure), emit health probe event:
```python
                            # C+ L1: Emit probe result to L3
                            if self._event_emitter is not None:
                                from backend.core.ouroboros.governance.autonomy_types import EventEnvelope, EventType as AET
                                await self._event_emitter.emit(EventEnvelope(
                                    source_layer=1,
                                    event_type=AET.HEALTH_PROBE_RESULT,
                                    payload={
                                        "provider": "gcp-jprime",
                                        "success": ok,
                                        "latency_ms": 0,
                                        "consecutive_failures": 0 if ok else getattr(self, '_probe_fail_count', 0),
                                    },
                                ))
```

5. In `_handle_command`, add handler for `REQUEST_MODE_SWITCH`:
```python
        elif ct == CommandType.REQUEST_MODE_SWITCH:
            target_mode = cmd.payload.get("target_mode", "")
            logger.warning("[GovernedLoop] L3 mode switch request: %s (reason: %s)",
                           target_mode, cmd.payload.get("reason", ""))
            if self._stack and hasattr(self._stack, 'degradation'):
                # Advisory: pass to degradation controller for evaluation
                pass
```

**Step 4: Run integration test + regression check**

Run: `python3 -m pytest tests/governance/autonomy/test_p1_integration.py -v`
Run: `python3 -m pytest tests/governance/ -x --timeout=30 -q`
Expected: PASS

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/governed_loop_service.py tests/governance/autonomy/test_p1_integration.py
git commit -m "feat(autonomy): wire P1 SafetyNet into GLS — health escalation + rollback analysis"
```

---

## Task 14: P2 Item 9 — Cross-Repo Saga Persistence (L4)

Reuses: `saga/saga_types.py` (ACTIVE), `saga/saga_apply_strategy.py` (ACTIVE).
New: `advanced_coordination.py` with saga state persistence and idempotency.

**Files:**
- Create: `backend/core/ouroboros/governance/advanced_coordination.py`
- Test: `tests/governance/autonomy/test_advanced_coordination_saga.py`

**Step 1: Write the failing test**

```python
# tests/governance/autonomy/test_advanced_coordination_saga.py
"""Tests for L4 cross-repo saga persistence and idempotency."""
import json
import pytest
from pathlib import Path

from backend.core.ouroboros.governance.autonomy_types import CommandType
from backend.core.ouroboros.governance.command_bus import CommandBus
from backend.core.ouroboros.governance.advanced_coordination import (
    AdvancedAutonomyService,
    AdvancedCoordinationConfig,
    SagaState,
)


@pytest.fixture
def state_dir(tmp_path):
    d = tmp_path / "saga_state"
    d.mkdir()
    return d


class TestSagaPersistence:
    def test_create_saga_persists_state(self, state_dir):
        bus = CommandBus(maxsize=100)
        config = AdvancedCoordinationConfig(state_dir=state_dir)
        svc = AdvancedAutonomyService(command_bus=bus, config=config)

        saga_id = svc.create_saga(
            repos=["jarvis", "jarvis-prime"],
            patches={"jarvis": "patch1", "jarvis-prime": "patch2"},
        )

        state_file = state_dir / f"saga_{saga_id}.json"
        assert state_file.exists()
        data = json.loads(state_file.read_text())
        assert data["saga_id"] == saga_id
        assert data["phase"] == "CREATED"

    def test_advance_saga_updates_state(self, state_dir):
        bus = CommandBus(maxsize=100)
        config = AdvancedCoordinationConfig(state_dir=state_dir)
        svc = AdvancedAutonomyService(command_bus=bus, config=config)

        saga_id = svc.create_saga(repos=["jarvis"], patches={"jarvis": "p"})
        svc.advance_saga(saga_id, repo="jarvis", success=True)

        data = json.loads((state_dir / f"saga_{saga_id}.json").read_text())
        assert "jarvis" in data["repos_applied"]

    def test_idempotent_advance(self, state_dir):
        bus = CommandBus(maxsize=100)
        config = AdvancedCoordinationConfig(state_dir=state_dir)
        svc = AdvancedAutonomyService(command_bus=bus, config=config)

        saga_id = svc.create_saga(repos=["jarvis"], patches={"jarvis": "p"})
        svc.advance_saga(saga_id, repo="jarvis", success=True)
        svc.advance_saga(saga_id, repo="jarvis", success=True)  # duplicate

        data = json.loads((state_dir / f"saga_{saga_id}.json").read_text())
        assert data["repos_applied"].count("jarvis") == 1

    def test_saga_emits_submit_command(self, state_dir):
        bus = CommandBus(maxsize=100)
        config = AdvancedCoordinationConfig(state_dir=state_dir)
        svc = AdvancedAutonomyService(command_bus=bus, config=config)

        saga_id = svc.create_saga(
            repos=["jarvis"],
            patches={"jarvis": "patch_data"},
        )
        svc.request_saga_submit(saga_id)

        assert bus.qsize() == 1
        cmd = bus._heap[0][2]
        assert cmd.command_type == CommandType.REQUEST_SAGA_SUBMIT
        assert cmd.payload["saga_id"] == saga_id

    def test_restart_recovers_state(self, state_dir):
        bus = CommandBus(maxsize=100)
        config = AdvancedCoordinationConfig(state_dir=state_dir)

        # First instance creates saga
        svc1 = AdvancedAutonomyService(command_bus=bus, config=config)
        saga_id = svc1.create_saga(repos=["jarvis"], patches={"jarvis": "p"})
        svc1.advance_saga(saga_id, repo="jarvis", success=True)

        # Second instance (simulating restart) recovers
        svc2 = AdvancedAutonomyService(command_bus=bus, config=config)
        state = svc2.get_saga_state(saga_id)
        assert state is not None
        assert "jarvis" in state.repos_applied
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/governance/autonomy/test_advanced_coordination_saga.py -v`
Expected: FAIL with `ModuleNotFoundError`

**Step 3: Write minimal implementation**

```python
# backend/core/ouroboros/governance/advanced_coordination.py
"""Advanced Autonomy Service — L4 Advanced Coordination.

Hosts cross-repo saga persistence, consensus voting, and dynamic tier
recommendations. All outputs are advisory CommandEnvelopes routed to L1
via the CommandBus.

Single-writer invariant: this module NEVER mutates op_context, ledger,
filesystem, or trust tiers directly. Saga state is internal to L4.
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from backend.core.ouroboros.governance.autonomy_types import (
    CommandEnvelope,
    CommandType,
)
from backend.core.ouroboros.governance.command_bus import CommandBus

logger = logging.getLogger("Ouroboros.AdvancedCoordination")


@dataclass
class AdvancedCoordinationConfig:
    state_dir: Path = field(default_factory=lambda: Path.home() / ".jarvis" / "ouroboros" / "saga_state")
    saga_timeout_s: float = 600.0
    max_concurrent_sagas: int = 1
    consensus_timeout_per_brain_s: float = 120.0


@dataclass
class SagaState:
    saga_id: str
    repos: List[str]
    patches: Dict[str, str]
    phase: str = "CREATED"  # CREATED | IN_PROGRESS | COMPLETED | FAILED
    repos_applied: List[str] = field(default_factory=list)
    repos_failed: List[str] = field(default_factory=list)
    idempotency_key: str = ""
    checksum: str = ""

    def __post_init__(self) -> None:
        if not self.idempotency_key:
            self.idempotency_key = self.saga_id
        self._update_checksum()

    def _update_checksum(self) -> None:
        raw = json.dumps({
            "saga_id": self.saga_id,
            "phase": self.phase,
            "repos_applied": sorted(self.repos_applied),
            "repos_failed": sorted(self.repos_failed),
        }, sort_keys=True)
        self.checksum = hashlib.sha256(raw.encode()).hexdigest()[:16]


class AdvancedAutonomyService:
    """L4 — Advanced Coordination. Advisory only."""

    def __init__(
        self,
        command_bus: CommandBus,
        config: Optional[AdvancedCoordinationConfig] = None,
    ) -> None:
        self._bus = command_bus
        self._config = config or AdvancedCoordinationConfig()
        self._config.state_dir.mkdir(parents=True, exist_ok=True)
        self._sagas: Dict[str, SagaState] = {}
        self._load_persisted_sagas()

    def _saga_path(self, saga_id: str) -> Path:
        return self._config.state_dir / f"saga_{saga_id}.json"

    def _persist_saga(self, state: SagaState) -> None:
        state._update_checksum()
        data = {
            "saga_id": state.saga_id,
            "repos": state.repos,
            "patches": state.patches,
            "phase": state.phase,
            "repos_applied": state.repos_applied,
            "repos_failed": state.repos_failed,
            "idempotency_key": state.idempotency_key,
            "checksum": state.checksum,
        }
        self._saga_path(state.saga_id).write_text(json.dumps(data, indent=2))

    def _load_persisted_sagas(self) -> None:
        for path in self._config.state_dir.glob("saga_*.json"):
            try:
                data = json.loads(path.read_text())
                state = SagaState(
                    saga_id=data["saga_id"],
                    repos=data["repos"],
                    patches=data.get("patches", {}),
                    phase=data.get("phase", "CREATED"),
                    repos_applied=data.get("repos_applied", []),
                    repos_failed=data.get("repos_failed", []),
                    idempotency_key=data.get("idempotency_key", data["saga_id"]),
                )
                # Verify checksum
                expected = data.get("checksum", "")
                if expected and state.checksum != expected:
                    logger.warning("[AdvancedCoord] Saga %s checksum mismatch — state may be corrupt", state.saga_id)
                self._sagas[state.saga_id] = state
            except Exception as exc:
                logger.warning("[AdvancedCoord] Failed to load %s: %s", path.name, exc)

    def create_saga(self, repos: List[str], patches: Dict[str, str]) -> str:
        saga_id = str(uuid.uuid4())[:12]
        state = SagaState(saga_id=saga_id, repos=repos, patches=patches)
        self._sagas[saga_id] = state
        self._persist_saga(state)
        return saga_id

    def advance_saga(self, saga_id: str, repo: str, success: bool) -> None:
        state = self._sagas.get(saga_id)
        if state is None:
            logger.warning("[AdvancedCoord] Unknown saga: %s", saga_id)
            return
        if success:
            if repo not in state.repos_applied:
                state.repos_applied.append(repo)
        else:
            if repo not in state.repos_failed:
                state.repos_failed.append(repo)
        # Update phase
        if set(state.repos_applied) >= set(state.repos):
            state.phase = "COMPLETED"
        elif state.repos_failed:
            state.phase = "FAILED"
        else:
            state.phase = "IN_PROGRESS"
        self._persist_saga(state)

    def get_saga_state(self, saga_id: str) -> Optional[SagaState]:
        return self._sagas.get(saga_id)

    def request_saga_submit(self, saga_id: str) -> None:
        state = self._sagas.get(saga_id)
        if state is None:
            return
        cmd = CommandEnvelope(
            source_layer=4,
            target_layer=1,
            command_type=CommandType.REQUEST_SAGA_SUBMIT,
            payload={
                "saga_id": saga_id,
                "repo_patches": state.patches,
                "idempotency_key": state.idempotency_key,
            },
        )
        self._bus.try_put(cmd)
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/governance/autonomy/test_advanced_coordination_saga.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/advanced_coordination.py tests/governance/autonomy/test_advanced_coordination_saga.py
git commit -m "feat(autonomy): P2.9 cross-repo saga persistence in AdvancedAutonomyService"
```

---

## Task 15: P2 Item 10 — Consensus Validation (L4)

Reuses: `shadow_harness.py` (REUSABLE).
Adds: multi-brain voting in AdvancedAutonomyService.

**Files:**
- Modify: `backend/core/ouroboros/governance/advanced_coordination.py`
- Test: `tests/governance/autonomy/test_advanced_coordination_consensus.py`

**Step 1: Write the failing test**

```python
# tests/governance/autonomy/test_advanced_coordination_consensus.py
"""Tests for L4 consensus voting — multi-brain validation."""
import pytest

from backend.core.ouroboros.governance.autonomy_types import CommandType
from backend.core.ouroboros.governance.command_bus import CommandBus
from backend.core.ouroboros.governance.advanced_coordination import (
    AdvancedAutonomyService,
    AdvancedCoordinationConfig,
)


@pytest.fixture
def state_dir(tmp_path):
    d = tmp_path / "saga_state"
    d.mkdir()
    return d


class TestConsensusVoting:
    def test_majority_agree_emits_consensus(self, state_dir):
        bus = CommandBus(maxsize=100)
        config = AdvancedCoordinationConfig(state_dir=state_dir)
        svc = AdvancedAutonomyService(command_bus=bus, config=config)

        result = svc.record_vote(
            op_id="op_1",
            candidates=["candidate_A"],
            votes={"qwen_coder": "approve", "qwen_coder_32b": "approve", "phi3_lightweight": "reject"},
        )

        assert result.majority is True
        assert bus.qsize() == 1
        cmd = bus._heap[0][2]
        assert cmd.command_type == CommandType.REPORT_CONSENSUS
        assert cmd.payload["majority"] is True

    def test_no_majority_escalates(self, state_dir):
        bus = CommandBus(maxsize=100)
        config = AdvancedCoordinationConfig(state_dir=state_dir)
        svc = AdvancedAutonomyService(command_bus=bus, config=config)

        result = svc.record_vote(
            op_id="op_2",
            candidates=["candidate_A"],
            votes={"qwen_coder": "approve", "qwen_coder_32b": "reject", "phi3_lightweight": "reject"},
        )

        assert result.majority is False
        # Should still emit consensus result (with majority=False)
        assert bus.qsize() == 1
        cmd = bus._heap[0][2]
        assert cmd.payload["majority"] is False

    def test_unanimous_agreement(self, state_dir):
        bus = CommandBus(maxsize=100)
        config = AdvancedCoordinationConfig(state_dir=state_dir)
        svc = AdvancedAutonomyService(command_bus=bus, config=config)

        result = svc.record_vote(
            op_id="op_3",
            candidates=["candidate_A"],
            votes={"a": "approve", "b": "approve"},
        )

        assert result.majority is True
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/governance/autonomy/test_advanced_coordination_consensus.py -v`
Expected: FAIL (`AdvancedAutonomyService has no attribute 'record_vote'`)

**Step 3: Add consensus voting to AdvancedAutonomyService**

Add dataclass and method to `advanced_coordination.py`:

```python
@dataclass
class ConsensusResult:
    op_id: str
    votes: Dict[str, str]
    majority: bool
    approved_count: int
    total_count: int
```

Add method to `AdvancedAutonomyService`:

```python
    def record_vote(
        self,
        op_id: str,
        candidates: List[str],
        votes: Dict[str, str],
    ) -> ConsensusResult:
        """Record multi-brain votes and emit consensus result."""
        approved = sum(1 for v in votes.values() if v == "approve")
        total = len(votes)
        majority = approved > total / 2

        result = ConsensusResult(
            op_id=op_id,
            votes=votes,
            majority=majority,
            approved_count=approved,
            total_count=total,
        )

        cmd = CommandEnvelope(
            source_layer=4,
            target_layer=1,
            command_type=CommandType.REPORT_CONSENSUS,
            payload={
                "op_id": op_id,
                "candidates": candidates,
                "votes": votes,
                "majority": majority,
            },
        )
        self._bus.try_put(cmd)

        return result
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/governance/autonomy/test_advanced_coordination_consensus.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/advanced_coordination.py tests/governance/autonomy/test_advanced_coordination_consensus.py
git commit -m "feat(autonomy): P2.10 consensus multi-brain voting in AdvancedAutonomyService"
```

---

## Task 16: P2 Item 11 — Dynamic Tier Override (L4)

Adds: `recommend_tier_change` command emission from L4.

**Files:**
- Modify: `backend/core/ouroboros/governance/advanced_coordination.py`
- Test: `tests/governance/autonomy/test_advanced_coordination_tier.py`

**Step 1: Write the failing test**

```python
# tests/governance/autonomy/test_advanced_coordination_tier.py
"""Tests for L4 dynamic tier override recommendations."""
import pytest

from backend.core.ouroboros.governance.autonomy_types import CommandType
from backend.core.ouroboros.governance.command_bus import CommandBus
from backend.core.ouroboros.governance.advanced_coordination import (
    AdvancedAutonomyService,
    AdvancedCoordinationConfig,
)


@pytest.fixture
def state_dir(tmp_path):
    d = tmp_path / "saga_state"
    d.mkdir()
    return d


class TestDynamicTierOverride:
    def test_recommend_promotion(self, state_dir):
        bus = CommandBus(maxsize=100)
        config = AdvancedCoordinationConfig(state_dir=state_dir)
        svc = AdvancedAutonomyService(command_bus=bus, config=config)

        svc.recommend_tier_change(
            repo="jarvis",
            canary_slice="tests/",
            recommended_tier="GOVERNED",
            evidence={"success_rate": 0.95, "sample_size": 20},
        )

        assert bus.qsize() == 1
        cmd = bus._heap[0][2]
        assert cmd.command_type == CommandType.RECOMMEND_TIER_CHANGE
        assert cmd.payload["recommended_tier"] == "GOVERNED"
        assert cmd.payload["evidence"]["success_rate"] == 0.95

    def test_reject_without_evidence(self, state_dir):
        bus = CommandBus(maxsize=100)
        config = AdvancedCoordinationConfig(state_dir=state_dir)
        svc = AdvancedAutonomyService(command_bus=bus, config=config)

        svc.recommend_tier_change(
            repo="jarvis",
            canary_slice="tests/",
            recommended_tier="AUTONOMOUS",
            evidence={},  # empty evidence
        )

        # Should not emit without evidence
        assert bus.qsize() == 0
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/governance/autonomy/test_advanced_coordination_tier.py -v`
Expected: FAIL

**Step 3: Add tier recommendation method**

Add to `AdvancedAutonomyService`:

```python
    def recommend_tier_change(
        self,
        repo: str,
        canary_slice: str,
        recommended_tier: str,
        evidence: Dict[str, Any],
    ) -> bool:
        """Recommend a trust tier change. Requires non-empty evidence."""
        if not evidence:
            logger.warning("[AdvancedCoord] Tier recommendation rejected: empty evidence")
            return False

        cmd = CommandEnvelope(
            source_layer=4,
            target_layer=1,
            command_type=CommandType.RECOMMEND_TIER_CHANGE,
            payload={
                "trigger_source": "l4_dynamic_override",
                "repo": repo,
                "canary_slice": canary_slice,
                "recommended_tier": recommended_tier,
                "evidence": evidence,
            },
        )
        return self._bus.try_put(cmd)
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/governance/autonomy/test_advanced_coordination_tier.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/advanced_coordination.py tests/governance/autonomy/test_advanced_coordination_tier.py
git commit -m "feat(autonomy): P2.11 dynamic tier override recommendations in L4"
```

---

## Task 17: Full Suite Regression + NO-GO Verification

**Files:**
- Test: all tests in `tests/governance/autonomy/`

**Step 1: Run full autonomy test suite**

Run: `python3 -m pytest tests/governance/autonomy/ -v --tb=short`
Expected: ALL PASS

**Step 2: Run full governance test suite for regressions**

Run: `python3 -m pytest tests/governance/ -x --timeout=60 -q`
Expected: No new failures (9 pre-existing excluded)

**Step 3: Verify NO-GO conditions**

Run: `python3 -m pytest tests/governance/autonomy/ -v --tb=short -k "idempoten or duplicate or expired or priority or fault_isolated"`
Expected: All idempotency, priority, and fault isolation tests pass.

**Step 4: Commit all remaining files**

```bash
git add -A tests/governance/autonomy/
git commit -m "test(autonomy): full C+ autonomous loop test suite — P0/P1/P2 complete"
```

---

## Task 18: Deprecation Markers on Legacy Files

Per design doc Deliverable 3, add deprecation markers to high-confidence unused files.

**Files to modify (comment only, no code changes):**
- `backend/core/ouroboros/governance/sandbox_loop.py` — line 1
- `backend/core/ouroboros/advanced_orchestrator.py` — line 1
- `backend/core/ouroboros/engine.py` — line 1
- `backend/core/ouroboros/brain_orchestrator.py` — line 1
- `backend/core/ouroboros/neural_mesh.py` — line 1
- `backend/core/ouroboros/genetic.py` — line 1
- `backend/core/ouroboros/simulator.py` — line 1
- `backend/core/ouroboros/scalability.py` — line 1
- `backend/core/ouroboros/protector.py` — line 1
- `backend/core/ouroboros/test_dummy.py` — line 1
- `backend/autonomy/system_states.py` — line 1

**Step 1: Add deprecation comment to each file**

Add as first line of each file:
```python
# DEPRECATED: superseded by governance/governed_loop_service.py. Quarantine date: 2026-03-11
```

For `backend/autonomy/system_states.py`:
```python
# DEPRECATED: superseded by governance/preemption_fsm.py. Quarantine date: 2026-03-11
```

**Step 2: Verify no tests break**

Run: `python3 -m pytest tests/ -x --timeout=60 -q`
Expected: No new failures

**Step 3: Commit**

```bash
git add backend/core/ouroboros/governance/sandbox_loop.py backend/core/ouroboros/advanced_orchestrator.py backend/core/ouroboros/engine.py backend/core/ouroboros/brain_orchestrator.py backend/core/ouroboros/neural_mesh.py backend/core/ouroboros/genetic.py backend/core/ouroboros/simulator.py backend/core/ouroboros/scalability.py backend/core/ouroboros/protector.py backend/core/ouroboros/test_dummy.py backend/autonomy/system_states.py
git commit -m "chore(quarantine): add deprecation markers to 11 legacy modules per audit"
```

---

## Summary

| Phase | Tasks | New Files | Modified Files |
|-------|-------|-----------|----------------|
| Shared | 1-3 | `autonomy_types.py`, `command_bus.py`, `event_emitter.py` | — |
| P0 | 4-8 | `feedback_engine.py` | `governed_loop_service.py` |
| P1 | 9-13 | `safety_net.py` | `governed_loop_service.py` |
| P2 | 14-16 | `advanced_coordination.py` | — |
| Verify | 17 | — | — |
| Cleanup | 18 | — | 11 legacy files (comment only) |

**Total: 6 new files, 1 production file modified, 11 legacy files marked deprecated.**
**Test files: 12 new test files across `tests/governance/autonomy/`.**
