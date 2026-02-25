# Causal Traceability & Adversarial Testing Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add mandatory cross-repo causal traceability with persistent trace stores and adversarial test infrastructure that proves startup determinism, recovery integrity, and ordering invariants.

**Architecture:** Contract-first TraceEnvelope (immutable, Lamport-ordered, dual-clock) propagated at every boundary. Three append-only JSONL streams (lifecycle/decisions/spans) with rebuildable SQLite indexes. Adversarial test harness with fault injection and deterministic replay engine.

**Tech Stack:** Python 3.11+, dataclasses (frozen), contextvars, threading, asyncio, SQLite3, fcntl, pytest, pytest-asyncio

---

### Task 1: TraceEnvelope Core Schema

**Files:**
- Create: `backend/core/trace_envelope.py`
- Test: `tests/unit/backend/core/test_trace_envelope.py`

**Step 1: Write the failing tests**

```python
"""Tests for TraceEnvelope v1 schema, LamportClock, and TraceEnvelopeFactory."""
import json
import os
import time
import threading
import pytest
from unittest.mock import patch


class TestLamportClock:
    def test_tick_monotonically_increasing(self):
        from backend.core.trace_envelope import LamportClock
        clock = LamportClock()
        values = [clock.tick() for _ in range(100)]
        assert values == list(range(1, 101))

    def test_receive_advances_past_incoming(self):
        from backend.core.trace_envelope import LamportClock
        clock = LamportClock()
        clock.tick()  # local = 1
        result = clock.receive(50)  # max(1, 50) + 1 = 51
        assert result == 51
        # Next tick should be 52
        assert clock.tick() == 52

    def test_receive_when_local_ahead(self):
        from backend.core.trace_envelope import LamportClock
        clock = LamportClock()
        for _ in range(100):
            clock.tick()
        # local = 100, incoming = 5 -> max(100, 5) + 1 = 101
        result = clock.receive(5)
        assert result == 101

    def test_thread_safety(self):
        from backend.core.trace_envelope import LamportClock
        clock = LamportClock()
        results = []
        def worker():
            for _ in range(1000):
                results.append(clock.tick())
        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # All values unique, no gaps when sorted
        assert len(set(results)) == 4000
        assert max(results) == 4000


class TestBoundaryType:
    def test_all_types_are_strings(self):
        from backend.core.trace_envelope import BoundaryType
        for bt in BoundaryType:
            assert isinstance(bt.value, str)

    def test_expected_types_exist(self):
        from backend.core.trace_envelope import BoundaryType
        expected = {"http", "ipc", "file_rpc", "event_bus", "subprocess", "internal"}
        actual = {bt.value for bt in BoundaryType}
        assert expected == actual


class TestTraceEnvelope:
    def test_frozen_immutable(self):
        from backend.core.trace_envelope import TraceEnvelopeFactory
        factory = TraceEnvelopeFactory(repo="jarvis", boot_id="test-boot", runtime_epoch_id="test-epoch", node_id="test-node", producer_version="v1.0")
        env = factory.create_root(component="test", operation="test_op")
        with pytest.raises(AttributeError):
            env.trace_id = "tampered"

    def test_serialization_round_trip(self):
        from backend.core.trace_envelope import TraceEnvelopeFactory, TraceEnvelope
        factory = TraceEnvelopeFactory(repo="jarvis", boot_id="test-boot", runtime_epoch_id="test-epoch", node_id="test-node", producer_version="v1.0")
        original = factory.create_root(component="test", operation="test_op")
        as_dict = original.to_dict()
        restored = TraceEnvelope.from_dict(as_dict)
        assert restored.trace_id == original.trace_id
        assert restored.span_id == original.span_id
        assert restored.event_id == original.event_id
        assert restored.sequence == original.sequence
        assert restored.schema_version == original.schema_version

    def test_json_round_trip(self):
        from backend.core.trace_envelope import TraceEnvelopeFactory, TraceEnvelope
        factory = TraceEnvelopeFactory(repo="jarvis", boot_id="test-boot", runtime_epoch_id="test-epoch", node_id="test-node", producer_version="v1.0")
        original = factory.create_root(component="test", operation="test_op")
        json_str = original.to_json()
        restored = TraceEnvelope.from_json(json_str)
        assert restored.trace_id == original.trace_id

    def test_header_round_trip(self):
        from backend.core.trace_envelope import TraceEnvelopeFactory, TraceEnvelope
        factory = TraceEnvelopeFactory(repo="jarvis", boot_id="test-boot", runtime_epoch_id="test-epoch", node_id="test-node", producer_version="v1.0")
        original = factory.create_root(component="test", operation="test_op")
        headers = original.to_headers()
        restored = TraceEnvelope.from_headers(headers)
        assert restored is not None
        assert restored.trace_id == original.trace_id
        assert restored.span_id == original.span_id
        assert restored.boot_id == original.boot_id
        assert restored.runtime_epoch_id == original.runtime_epoch_id

    def test_child_inherits_trace_id(self):
        from backend.core.trace_envelope import TraceEnvelopeFactory
        factory = TraceEnvelopeFactory(repo="jarvis", boot_id="test-boot", runtime_epoch_id="test-epoch", node_id="test-node", producer_version="v1.0")
        parent = factory.create_root(component="parent", operation="parent_op")
        child = factory.create_child(parent=parent, component="child", operation="child_op")
        assert child.trace_id == parent.trace_id
        assert child.parent_span_id == parent.span_id
        assert child.span_id != parent.span_id
        assert child.event_id != parent.event_id
        assert child.sequence > parent.sequence

    def test_causality_link(self):
        from backend.core.trace_envelope import TraceEnvelopeFactory
        factory = TraceEnvelopeFactory(repo="jarvis", boot_id="test-boot", runtime_epoch_id="test-epoch", node_id="test-node", producer_version="v1.0")
        cause = factory.create_root(component="health", operation="check_failed")
        effect = factory.create_child(parent=cause, component="recovery", operation="restart_vm", caused_by_event_id=cause.event_id, idempotency_key="restart_vm:vm-001:nonce-abc")
        assert effect.caused_by_event_id == cause.event_id
        assert effect.idempotency_key == "restart_vm:vm-001:nonce-abc"

    def test_extra_fields_preserved(self):
        from backend.core.trace_envelope import TraceEnvelopeFactory, TraceEnvelope
        factory = TraceEnvelopeFactory(repo="jarvis", boot_id="test-boot", runtime_epoch_id="test-epoch", node_id="test-node", producer_version="v1.0")
        original = factory.create_root(component="test", operation="test_op")
        as_dict = original.to_dict()
        as_dict["extra"]["future_field"] = "future_value"
        restored = TraceEnvelope.from_dict(as_dict)
        assert restored.extra["future_field"] == "future_value"

    def test_env_var_round_trip(self):
        from backend.core.trace_envelope import TraceEnvelopeFactory, TraceEnvelope
        factory = TraceEnvelopeFactory(repo="jarvis", boot_id="test-boot", runtime_epoch_id="test-epoch", node_id="test-node", producer_version="v1.0")
        original = factory.create_root(component="test", operation="test_op")
        json_str = original.to_json()
        assert len(json_str) < 4096, "Envelope must fit in env var (4KB limit)"
        restored = TraceEnvelope.from_json(json_str)
        assert restored.trace_id == original.trace_id


class TestTraceEnvelopeValidation:
    def test_rejects_empty_trace_id(self):
        from backend.core.trace_envelope import TraceEnvelope, validate_envelope
        env_dict = _make_valid_envelope_dict()
        env_dict["trace_id"] = ""
        env = TraceEnvelope.from_dict(env_dict)
        errors = validate_envelope(env)
        assert any("trace_id" in e for e in errors)

    def test_rejects_negative_sequence(self):
        from backend.core.trace_envelope import TraceEnvelope, validate_envelope
        env_dict = _make_valid_envelope_dict()
        env_dict["sequence"] = -1
        env = TraceEnvelope.from_dict(env_dict)
        errors = validate_envelope(env)
        assert any("sequence" in e for e in errors)

    def test_rejects_unknown_repo(self):
        from backend.core.trace_envelope import TraceEnvelope, validate_envelope
        env_dict = _make_valid_envelope_dict()
        env_dict["repo"] = "unknown_repo"
        env = TraceEnvelope.from_dict(env_dict)
        errors = validate_envelope(env)
        assert any("repo" in e for e in errors)

    def test_detects_gross_clock_skew(self):
        from backend.core.trace_envelope import TraceEnvelope, validate_envelope
        env_dict = _make_valid_envelope_dict()
        env_dict["ts_wall_utc"] = time.time() + 600  # 10 min in future
        env = TraceEnvelope.from_dict(env_dict)
        errors = validate_envelope(env)
        assert any("clock" in e.lower() or "skew" in e.lower() for e in errors)

    def test_valid_envelope_passes(self):
        from backend.core.trace_envelope import TraceEnvelopeFactory, validate_envelope
        factory = TraceEnvelopeFactory(repo="jarvis", boot_id="test-boot", runtime_epoch_id="test-epoch", node_id="test-node", producer_version="v1.0")
        env = factory.create_root(component="test", operation="test_op")
        errors = validate_envelope(env)
        assert errors == []


class TestSchemaCompatibility:
    def test_min_version_reject(self):
        from backend.core.trace_envelope import check_schema_compatibility
        result = check_schema_compatibility(schema_version=0, boundary_critical=True)
        assert result.accepted is False

    def test_max_version_critical_reject(self):
        from backend.core.trace_envelope import check_schema_compatibility
        result = check_schema_compatibility(schema_version=999, boundary_critical=True)
        assert result.accepted is False

    def test_max_version_non_critical_warn(self):
        from backend.core.trace_envelope import check_schema_compatibility
        result = check_schema_compatibility(schema_version=999, boundary_critical=False)
        assert result.accepted is True
        assert result.warning is not None

    def test_current_version_accepted(self):
        from backend.core.trace_envelope import check_schema_compatibility, TRACE_SCHEMA_VERSION
        result = check_schema_compatibility(schema_version=TRACE_SCHEMA_VERSION, boundary_critical=True)
        assert result.accepted is True
        assert result.warning is None


def _make_valid_envelope_dict() -> dict:
    """Helper to create a valid envelope dict for mutation testing."""
    import uuid
    return {
        "trace_id": uuid.uuid4().hex[:16],
        "span_id": uuid.uuid4().hex[:16],
        "event_id": uuid.uuid4().hex[:16],
        "parent_span_id": None,
        "sequence": 1,
        "boot_id": str(uuid.uuid4()),
        "runtime_epoch_id": str(uuid.uuid4()),
        "process_id": os.getpid(),
        "node_id": "test-node",
        "ts_wall_utc": time.time(),
        "ts_mono_local": time.monotonic(),
        "repo": "jarvis",
        "component": "test",
        "operation": "test_op",
        "boundary_type": "internal",
        "caused_by_event_id": None,
        "idempotency_key": None,
        "producer_version": "v1.0-test",
        "schema_version": 1,
        "extra": {},
    }
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/backend/core/test_trace_envelope.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'backend.core.trace_envelope'`

**Step 3: Implement `backend/core/trace_envelope.py`**

Create the file with:
- `BoundaryType` enum (6 values: http, ipc, file_rpc, event_bus, subprocess, internal)
- `LamportClock` class (locked integer, `tick()` increments, `receive(incoming)` does `max(local, incoming) + 1`)
- `TraceEnvelope` frozen dataclass (all fields from design doc Section 1)
  - `to_dict()` → dict, `from_dict(d)` → TraceEnvelope
  - `to_json()` → str, `from_json(s)` → TraceEnvelope
  - `to_headers()` → Dict[str, str], `from_headers(h)` → Optional[TraceEnvelope]
- `TraceEnvelopeFactory` class (holds repo, boot_id, runtime_epoch_id, node_id, producer_version, internal LamportClock)
  - `create_root(component, operation, ...)` → TraceEnvelope (new trace_id, no parent)
  - `create_child(parent, component, operation, ...)` → TraceEnvelope (inherits trace_id, sets parent_span_id)
  - `create_event(span, ...)` → TraceEnvelope (same span_id, new event_id)
- `validate_envelope(env)` → List[str] (returns list of error strings, empty if valid)
- `check_schema_compatibility(schema_version, boundary_critical)` → `CompatibilityResult(accepted, warning)`
- Constants: `TRACE_SCHEMA_VERSION = 1`, `TRACE_SCHEMA_MIN_SUPPORTED = 1`, `TRACE_SCHEMA_MAX_SUPPORTED = 1`
- Constants: `KNOWN_REPOS = {"jarvis", "jarvis-prime", "reactor-core"}`
- Validation: `CLOCK_SKEW_TOLERANCE_S = 300.0` (configurable via `JARVIS_TRACE_CLOCK_SKEW_TOLERANCE`)

Key implementation notes:
- `from_dict()` must preserve unknown fields into `extra`
- `to_headers()` prefix: `X-Trace-*` (e.g., `X-Trace-ID`, `X-Trace-Span-ID`, `X-Trace-Sequence`)
- `from_headers()` returns `None` if `X-Trace-ID` header missing
- All string IDs generated via `uuid.uuid4().hex[:16]` (compact, unique enough)
- `boundary_type` stored as string in dict/headers, converted to enum on deserialize

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/backend/core/test_trace_envelope.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add backend/core/trace_envelope.py tests/unit/backend/core/test_trace_envelope.py
git commit -m "feat: add TraceEnvelope v1 schema, LamportClock, and factory"
```

---

### Task 2: Integrate TraceEnvelope into CorrelationContext

**Files:**
- Modify: `backend/core/resilience/correlation_context.py` (lines 78-166, 200-220, 223-245, 469-506)
- Test: `tests/unit/backend/core/test_correlation_trace_integration.py`

**Step 1: Write the failing tests**

```python
"""Tests that CorrelationContext uses TraceEnvelope as backing store."""
import pytest


class TestCorrelationContextEnvelopeIntegration:
    def test_create_attaches_envelope(self):
        from backend.core.resilience.correlation_context import CorrelationContext
        ctx = CorrelationContext.create(operation="test_op", source_component="test")
        assert ctx.envelope is not None
        assert ctx.envelope.operation == "test_op"
        assert ctx.envelope.component == "test"

    def test_child_context_inherits_trace_id(self):
        from backend.core.resilience.correlation_context import CorrelationContext
        parent = CorrelationContext.create(operation="parent_op")
        child = CorrelationContext.create(operation="child_op", parent=parent)
        assert child.envelope.trace_id == parent.envelope.trace_id
        assert child.envelope.parent_span_id == parent.envelope.span_id

    def test_to_headers_includes_envelope(self):
        from backend.core.resilience.correlation_context import CorrelationContext
        ctx = CorrelationContext.create(operation="test")
        headers = ctx.to_headers()
        assert "X-Trace-ID" in headers
        assert "X-Trace-Span-ID" in headers
        assert "X-Trace-Sequence" in headers
        # Backward compat: old correlation headers still present
        assert "X-Correlation-ID" in headers

    def test_from_headers_restores_envelope(self):
        from backend.core.resilience.correlation_context import CorrelationContext
        original = CorrelationContext.create(operation="test")
        headers = original.to_headers()
        restored = CorrelationContext.from_headers(headers)
        assert restored is not None
        assert restored.envelope.trace_id == original.envelope.trace_id

    def test_inject_extract_round_trip(self):
        from backend.core.resilience.correlation_context import (
            CorrelationContext, set_current_context, inject_correlation, extract_correlation,
        )
        ctx = CorrelationContext.create(operation="test")
        set_current_context(ctx)
        data = {}
        data = inject_correlation(data)
        assert "_trace_envelope" in data
        extracted = extract_correlation(data)
        assert extracted is not None
        assert extracted.envelope.trace_id == ctx.envelope.trace_id

    def test_backward_compat_without_envelope_headers(self):
        """Existing callers that only send X-Correlation-ID still work."""
        from backend.core.resilience.correlation_context import CorrelationContext
        old_headers = {"X-Correlation-ID": "old-style-123", "X-Source-Repo": "jarvis"}
        ctx = CorrelationContext.from_headers(old_headers)
        assert ctx is not None
        assert ctx.correlation_id == "old-style-123"
        # Envelope should be auto-generated for backward compat
        assert ctx.envelope is not None
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/backend/core/test_correlation_trace_integration.py -v`
Expected: FAIL — `AttributeError: 'CorrelationContext' object has no attribute 'envelope'`

**Step 3: Modify `correlation_context.py`**

Changes to make (preserve ALL existing API — this is additive):
1. Add `from backend.core.trace_envelope import TraceEnvelope, TraceEnvelopeFactory, LamportClock` import (with ImportError guard)
2. Add module-level `_envelope_factory: Optional[TraceEnvelopeFactory] = None` and `_lamport_clock: Optional[LamportClock] = None`
3. Add `init_trace_envelope_factory(repo, boot_id, runtime_epoch_id, node_id, producer_version)` function
4. Add `envelope: Optional[TraceEnvelope] = None` field to `CorrelationContext` (line 78)
5. In `create()` (line 114): if factory available, create envelope (root or child) and attach
6. In `to_headers()` (line 200): if envelope exists, add `X-Trace-*` headers alongside existing `X-Correlation-ID`
7. In `from_headers()` (line 223): try to restore envelope from `X-Trace-*` headers; if missing, fall back to old behavior and auto-generate envelope
8. In `inject_correlation()` (line 469): if envelope exists, add `_trace_envelope` key alongside existing `_correlation`
9. In `extract_correlation()` (line 481): if `_trace_envelope` in data, restore envelope; advance Lamport clock on receive

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/backend/core/test_correlation_trace_integration.py tests/unit/backend/core/test_trace_envelope.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add backend/core/resilience/correlation_context.py tests/unit/backend/core/test_correlation_trace_integration.py
git commit -m "feat: integrate TraceEnvelope into CorrelationContext as backing store"
```

---

### Task 3: JSONL Append Writer (Trace Store Foundation)

**Files:**
- Create: `backend/core/trace_store.py`
- Test: `tests/unit/backend/core/test_trace_store.py`

**Step 1: Write the failing tests**

```python
"""Tests for JSONL append-only trace store."""
import json
import os
import tempfile
import threading
import time
import pytest
from pathlib import Path


class TestJSONLWriter:
    def test_append_single_event(self, tmp_path):
        from backend.core.trace_store import JSONLWriter
        writer = JSONLWriter(tmp_path / "test.jsonl")
        writer.append({"event_id": "e1", "data": "hello"})
        lines = (tmp_path / "test.jsonl").read_text().strip().split("\n")
        assert len(lines) == 1
        assert json.loads(lines[0])["event_id"] == "e1"

    def test_append_multiple_events(self, tmp_path):
        from backend.core.trace_store import JSONLWriter
        writer = JSONLWriter(tmp_path / "test.jsonl")
        for i in range(100):
            writer.append({"event_id": f"e{i}"})
        lines = (tmp_path / "test.jsonl").read_text().strip().split("\n")
        assert len(lines) == 100

    def test_line_checksum_present(self, tmp_path):
        from backend.core.trace_store import JSONLWriter
        writer = JSONLWriter(tmp_path / "test.jsonl")
        writer.append({"event_id": "e1"})
        line = (tmp_path / "test.jsonl").read_text().strip()
        parsed = json.loads(line)
        assert "_checksum" in parsed

    def test_concurrent_appends_no_interleaving(self, tmp_path):
        from backend.core.trace_store import JSONLWriter
        writer = JSONLWriter(tmp_path / "test.jsonl")
        def worker(prefix):
            for i in range(50):
                writer.append({"event_id": f"{prefix}_{i}", "data": "x" * 200})
        threads = [threading.Thread(target=worker, args=(f"t{t}",)) for t in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        lines = (tmp_path / "test.jsonl").read_text().strip().split("\n")
        assert len(lines) == 200
        for line in lines:
            json.loads(line)  # Each line must be valid JSON (no torn writes)

    def test_creates_parent_directories(self, tmp_path):
        from backend.core.trace_store import JSONLWriter
        deep_path = tmp_path / "a" / "b" / "c" / "test.jsonl"
        writer = JSONLWriter(deep_path)
        writer.append({"event_id": "e1"})
        assert deep_path.exists()


class TestTraceStreamManager:
    def test_lifecycle_stream_creates_epoch_file(self, tmp_path):
        from backend.core.trace_store import TraceStreamManager
        mgr = TraceStreamManager(base_dir=tmp_path, runtime_epoch_id="epoch-001")
        mgr.write_lifecycle({"event_type": "boot_start", "envelope": {}})
        files = list((tmp_path / "lifecycle").glob("*.jsonl"))
        assert len(files) == 1
        assert "epoch_epoch-001" in files[0].name

    def test_decisions_stream_date_partitioned(self, tmp_path):
        from backend.core.trace_store import TraceStreamManager
        mgr = TraceStreamManager(base_dir=tmp_path, runtime_epoch_id="epoch-001")
        mgr.write_decision({"decision_type": "vm_termination", "envelope": {}})
        files = list((tmp_path / "decisions").glob("*.jsonl"))
        assert len(files) == 1
        today = time.strftime("%Y%m%d")
        assert today in files[0].name

    def test_spans_stream_date_partitioned(self, tmp_path):
        from backend.core.trace_store import TraceStreamManager
        mgr = TraceStreamManager(base_dir=tmp_path, runtime_epoch_id="epoch-001")
        mgr.write_span({"operation": "health_check", "envelope": {}})
        files = list((tmp_path / "spans").glob("*.jsonl"))
        assert len(files) == 1

    def test_backpressure_drops_spans_not_lifecycle(self, tmp_path):
        from backend.core.trace_store import SpanBuffer
        buffer = SpanBuffer(max_size=10)
        # Fill buffer past 95% (10 items, >9.5 = all 10)
        for i in range(10):
            buffer.add({"status": "success", "event_id": f"s{i}"})
        # Add an 11th — should trigger drop policy
        buffer.add({"status": "success", "event_id": "overflow"})
        kept = buffer.drain()
        # Only errors kept at >95%, successes sampled/dropped
        assert len(kept) <= 11  # Some may be dropped
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/backend/core/test_trace_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'backend.core.trace_store'`

**Step 3: Implement `backend/core/trace_store.py`**

Create the file with:
- `JSONLWriter` class:
  - `__init__(self, file_path: Path)` — creates parent dirs
  - `append(self, record: Dict)` — adds `_checksum` (CRC32 of JSON bytes), opens with `O_APPEND | O_WRONLY | O_CREAT`, `fcntl.LOCK_EX`, writes line + `\n`, `os.fsync()`, unlocks
  - Thread-safe via `fcntl` file lock (not Python threading lock — works cross-process)
- `SpanBuffer` class:
  - `__init__(self, max_size=256)` — in-memory buffer with backpressure
  - `add(self, record)` — adds to buffer; at >80% capacity samples successes 50%; at >95% keeps only errors/timeouts; never drops records with `idempotency_key`
  - `drain()` → List[Dict] — returns and clears buffer
  - Thread-safe via `threading.Lock`
- `TraceStreamManager` class:
  - `__init__(self, base_dir, runtime_epoch_id)` — creates `lifecycle/`, `decisions/`, `spans/` subdirs
  - `write_lifecycle(self, record)` — writes to `lifecycle/{date}_epoch_{epoch_id}.jsonl`
  - `write_decision(self, record)` — writes to `decisions/{date}.jsonl`
  - `write_span(self, record)` — buffers into SpanBuffer, flushes on drain
  - `flush_spans(self)` — drains SpanBuffer, writes to `spans/{date}.jsonl`
- `DiskGuard` class (stub for now — full implementation in Task 12):
  - `check_disk_usage()` → float (0.0-1.0)
  - `should_rotate()` → bool

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/backend/core/test_trace_store.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add backend/core/trace_store.py tests/unit/backend/core/test_trace_store.py
git commit -m "feat: add JSONL append-only trace store with three streams"
```

---

### Task 4: Lifecycle Event Emitter (Supervisor Integration)

**Files:**
- Create: `backend/core/lifecycle_emitter.py`
- Modify: `unified_supervisor.py` (lines 64235-64249, 65570-65594, and other phase call sites)
- Test: `tests/unit/backend/core/test_lifecycle_emitter.py`

**Step 1: Write the failing tests**

```python
"""Tests for lifecycle event emission."""
import time
import pytest
from unittest.mock import MagicMock, patch


class TestLifecycleEmitter:
    def test_emit_phase_enter(self, tmp_path):
        from backend.core.lifecycle_emitter import LifecycleEmitter
        from backend.core.trace_envelope import TraceEnvelopeFactory
        factory = TraceEnvelopeFactory(repo="jarvis", boot_id="b1", runtime_epoch_id="e1", node_id="n1", producer_version="v1")
        emitter = LifecycleEmitter(trace_dir=tmp_path, envelope_factory=factory)
        emitter.phase_enter("preflight")
        events = emitter.get_recent(10)
        assert len(events) == 1
        assert events[0]["event_type"] == "phase_enter"
        assert events[0]["phase"] == "preflight"

    def test_emit_phase_exit(self, tmp_path):
        from backend.core.lifecycle_emitter import LifecycleEmitter
        from backend.core.trace_envelope import TraceEnvelopeFactory
        factory = TraceEnvelopeFactory(repo="jarvis", boot_id="b1", runtime_epoch_id="e1", node_id="n1", producer_version="v1")
        emitter = LifecycleEmitter(trace_dir=tmp_path, envelope_factory=factory)
        emitter.phase_enter("preflight")
        emitter.phase_exit("preflight", success=True)
        events = emitter.get_recent(10)
        assert len(events) == 2
        assert events[1]["event_type"] == "phase_exit"
        assert events[1]["to_state"] == "success"

    def test_emit_phase_fail(self, tmp_path):
        from backend.core.lifecycle_emitter import LifecycleEmitter
        from backend.core.trace_envelope import TraceEnvelopeFactory
        factory = TraceEnvelopeFactory(repo="jarvis", boot_id="b1", runtime_epoch_id="e1", node_id="n1", producer_version="v1")
        emitter = LifecycleEmitter(trace_dir=tmp_path, envelope_factory=factory)
        emitter.phase_enter("backend")
        emitter.phase_fail("backend", error="TimeoutError", evidence={"elapsed_s": 300})
        events = emitter.get_recent(10)
        assert events[1]["event_type"] == "phase_fail"
        assert events[1]["evidence"]["elapsed_s"] == 300

    def test_emit_boot_start(self, tmp_path):
        from backend.core.lifecycle_emitter import LifecycleEmitter
        from backend.core.trace_envelope import TraceEnvelopeFactory
        factory = TraceEnvelopeFactory(repo="jarvis", boot_id="b1", runtime_epoch_id="e1", node_id="n1", producer_version="v1")
        emitter = LifecycleEmitter(trace_dir=tmp_path, envelope_factory=factory)
        emitter.boot_start()
        events = emitter.get_recent(10)
        assert events[0]["event_type"] == "boot_start"

    def test_events_persisted_to_jsonl(self, tmp_path):
        from backend.core.lifecycle_emitter import LifecycleEmitter
        from backend.core.trace_envelope import TraceEnvelopeFactory
        import json
        factory = TraceEnvelopeFactory(repo="jarvis", boot_id="b1", runtime_epoch_id="e1", node_id="n1", producer_version="v1")
        emitter = LifecycleEmitter(trace_dir=tmp_path, envelope_factory=factory)
        emitter.boot_start()
        emitter.phase_enter("clean_slate")
        emitter.flush()
        files = list((tmp_path / "lifecycle").glob("*.jsonl"))
        assert len(files) == 1
        lines = files[0].read_text().strip().split("\n")
        assert len(lines) == 2
        for line in lines:
            record = json.loads(line)
            assert "envelope" in record
            assert record["envelope"]["repo"] == "jarvis"

    def test_causality_chain_maintained(self, tmp_path):
        from backend.core.lifecycle_emitter import LifecycleEmitter
        from backend.core.trace_envelope import TraceEnvelopeFactory
        factory = TraceEnvelopeFactory(repo="jarvis", boot_id="b1", runtime_epoch_id="e1", node_id="n1", producer_version="v1")
        emitter = LifecycleEmitter(trace_dir=tmp_path, envelope_factory=factory)
        emitter.boot_start()
        emitter.phase_enter("clean_slate")
        events = emitter.get_recent(10)
        boot_event = events[0]
        phase_event = events[1]
        # Phase enter should be caused by boot start
        assert phase_event["envelope"]["caused_by_event_id"] == boot_event["envelope"]["event_id"]
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/backend/core/test_lifecycle_emitter.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'backend.core.lifecycle_emitter'`

**Step 3: Implement `backend/core/lifecycle_emitter.py`**

Create the file with:
- `LifecycleEmitter` class:
  - `__init__(self, trace_dir, envelope_factory)` — creates `TraceStreamManager`, holds factory
  - `boot_start()` — emits boot_start event, stores root envelope as `_boot_envelope`
  - `boot_complete()` — emits boot_complete event
  - `shutdown_start()` — emits shutdown_start event
  - `phase_enter(phase)` — emits phase_enter, links caused_by to previous phase_exit or boot_start
  - `phase_exit(phase, success)` — emits phase_exit
  - `phase_fail(phase, error, evidence)` — emits phase_fail
  - `recovery_start(component, reason, caused_by_event_id)` — emits recovery_start
  - `recovery_complete(component, outcome)` — emits recovery_complete
  - `recovery_fail(component, error)` — emits recovery_fail
  - `get_recent(n)` → List[Dict] — returns last n events from in-memory buffer
  - `flush()` — writes buffered events to JSONL via TraceStreamManager
  - Internal: maintains `_last_event_id` for causality chaining
  - Internal: maintains in-memory buffer (64 max) + auto-flush on phase transitions and every 2s

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/backend/core/test_lifecycle_emitter.py -v`
Expected: ALL PASS

**Step 5: Integrate into `unified_supervisor.py`**

Modify `_startup_impl()` at line 63914:
- After startup transaction coordinator init (~line 63943), initialize `LifecycleEmitter` and `TraceEnvelopeFactory`
- Call `lifecycle_emitter.boot_start()`
- Before each `_phase_*` call (lines 64239, 65577, 65669, 65751, etc.): call `lifecycle_emitter.phase_enter(phase_name)`
- After each phase completes or fails: call `lifecycle_emitter.phase_exit()` or `lifecycle_emitter.phase_fail()`
- Integration pattern uses existing `_emit_event()` (line 79626) — add lifecycle_emitter call alongside

**Step 6: Run full test suite**

Run: `python3 -m pytest tests/unit/backend/core/test_lifecycle_emitter.py tests/unit/backend/core/test_trace_envelope.py -v`
Expected: ALL PASS

**Step 7: Commit**

```bash
git add backend/core/lifecycle_emitter.py tests/unit/backend/core/test_lifecycle_emitter.py unified_supervisor.py
git commit -m "feat: add lifecycle event emitter with supervisor integration"
```

---

### Task 5: Decision Log JSONL Flusher + Envelope Field

**Files:**
- Modify: `backend/core/decision_log.py` (lines 60-69, 118-147)
- Test: `tests/unit/backend/core/test_decision_log_persistence.py`

**Step 1: Write the failing tests**

```python
"""Tests for DecisionLog persistence and envelope integration."""
import json
import time
import pytest
from pathlib import Path


class TestDecisionRecordEnvelope:
    def test_record_accepts_envelope(self):
        from backend.core.decision_log import DecisionLog
        from backend.core.trace_envelope import TraceEnvelopeFactory
        factory = TraceEnvelopeFactory(repo="jarvis", boot_id="b1", runtime_epoch_id="e1", node_id="n1", producer_version="v1")
        env = factory.create_root(component="gcp_vm_manager", operation="terminate_vm")
        log = DecisionLog(max_entries=100)
        rec = log.record(
            decision_type="vm_termination",
            reason="cost exceeded",
            inputs={"cost": 5.0},
            outcome="terminated",
            component="gcp_vm_manager",
            envelope=env,
        )
        assert rec.envelope is not None
        assert rec.envelope.trace_id == env.trace_id

    def test_record_works_without_envelope(self):
        """Backward compat: envelope is optional."""
        from backend.core.decision_log import DecisionLog
        log = DecisionLog(max_entries=100)
        rec = log.record(
            decision_type="vm_termination",
            reason="cost exceeded",
            inputs={"cost": 5.0},
            outcome="terminated",
        )
        assert rec.envelope is None

    def test_to_dict_includes_envelope(self):
        from backend.core.decision_log import DecisionLog
        from backend.core.trace_envelope import TraceEnvelopeFactory
        factory = TraceEnvelopeFactory(repo="jarvis", boot_id="b1", runtime_epoch_id="e1", node_id="n1", producer_version="v1")
        env = factory.create_root(component="test", operation="test")
        log = DecisionLog(max_entries=100)
        rec = log.record(
            decision_type="test",
            reason="test",
            inputs={},
            outcome="test",
            envelope=env,
        )
        d = rec.to_dict()
        assert "envelope" in d
        assert d["envelope"]["trace_id"] == env.trace_id


class TestDecisionLogFlusher:
    def test_flush_writes_jsonl(self, tmp_path):
        from backend.core.decision_log import DecisionLog
        log = DecisionLog(max_entries=100)
        log.record(decision_type="test", reason="r", inputs={}, outcome="o")
        log.record(decision_type="test", reason="r2", inputs={}, outcome="o2")
        flushed = log.flush_to_jsonl(tmp_path / "decisions")
        assert flushed == 2
        files = list((tmp_path / "decisions").glob("*.jsonl"))
        assert len(files) == 1
        lines = files[0].read_text().strip().split("\n")
        assert len(lines) == 2

    def test_flush_is_incremental(self, tmp_path):
        from backend.core.decision_log import DecisionLog
        log = DecisionLog(max_entries=100)
        log.record(decision_type="test", reason="r1", inputs={}, outcome="o1")
        log.flush_to_jsonl(tmp_path / "decisions")
        log.record(decision_type="test", reason="r2", inputs={}, outcome="o2")
        log.flush_to_jsonl(tmp_path / "decisions")
        files = list((tmp_path / "decisions").glob("*.jsonl"))
        assert len(files) == 1
        lines = files[0].read_text().strip().split("\n")
        assert len(lines) == 2  # Both records, not duplicated

    def test_flush_does_not_lose_records_on_error(self, tmp_path):
        from backend.core.decision_log import DecisionLog
        log = DecisionLog(max_entries=100)
        log.record(decision_type="test", reason="r1", inputs={}, outcome="o1")
        # Flush to non-writable path
        bad_path = tmp_path / "nonexistent" / "deep" / "path"
        # Should not raise, should return 0
        flushed = log.flush_to_jsonl(bad_path)
        # Records still in memory
        assert log.size == 1
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/backend/core/test_decision_log_persistence.py -v`
Expected: FAIL — various AttributeErrors

**Step 3: Modify `backend/core/decision_log.py`**

Changes:
1. Add `envelope: Optional[TraceEnvelope] = None` field to `DecisionRecord` (line 60)
2. Update `record()` (line 118) to accept optional `envelope` parameter
3. Update `to_dict()` to include `envelope.to_dict()` if present
4. Add `flush_to_jsonl(self, decisions_dir: Path) -> int` method:
   - Tracks `_last_flushed_index` (int) to know what's already been written
   - Drains new records since last flush
   - Writes to date-partitioned JSONL via `JSONLWriter` from `trace_store.py`
   - Returns count of flushed records
   - On error: logs warning, returns 0, records stay in memory (not lost)
5. Add `_last_flushed_index: int = 0` instance variable
6. Update module-level `record_decision()` to accept optional `envelope` kwarg

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/backend/core/test_decision_log_persistence.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add backend/core/decision_log.py tests/unit/backend/core/test_decision_log_persistence.py
git commit -m "feat: add envelope field and JSONL flusher to DecisionLog"
```

---

### Task 6: Span Recorder (Wraps Circuit Breakers and Health Checks)

**Files:**
- Create: `backend/core/span_recorder.py`
- Test: `tests/unit/backend/core/test_span_recorder.py`

**Step 1: Write the failing tests**

```python
"""Tests for span recording around existing circuit breakers and operations."""
import asyncio
import time
import pytest


class TestSpanRecorder:
    @pytest.mark.asyncio
    async def test_record_success_span(self, tmp_path):
        from backend.core.span_recorder import SpanRecorder
        from backend.core.trace_envelope import TraceEnvelopeFactory
        factory = TraceEnvelopeFactory(repo="jarvis", boot_id="b1", runtime_epoch_id="e1", node_id="n1", producer_version="v1")
        recorder = SpanRecorder(trace_dir=tmp_path, envelope_factory=factory)
        async with recorder.span("health_check", component="prime_client") as span:
            await asyncio.sleep(0.01)
        assert span["status"] == "success"
        assert span["duration_ms"] > 0

    @pytest.mark.asyncio
    async def test_record_error_span(self, tmp_path):
        from backend.core.span_recorder import SpanRecorder
        from backend.core.trace_envelope import TraceEnvelopeFactory
        factory = TraceEnvelopeFactory(repo="jarvis", boot_id="b1", runtime_epoch_id="e1", node_id="n1", producer_version="v1")
        recorder = SpanRecorder(trace_dir=tmp_path, envelope_factory=factory)
        with pytest.raises(ValueError):
            async with recorder.span("inference", component="model_serving") as span:
                raise ValueError("model not loaded")
        # Span still recorded despite error
        recent = recorder.get_recent(10)
        assert len(recent) == 1
        assert recent[0]["status"] == "error"
        assert recent[0]["error_class"] == "ValueError"

    @pytest.mark.asyncio
    async def test_flush_writes_to_spans_stream(self, tmp_path):
        from backend.core.span_recorder import SpanRecorder
        from backend.core.trace_envelope import TraceEnvelopeFactory
        import json
        factory = TraceEnvelopeFactory(repo="jarvis", boot_id="b1", runtime_epoch_id="e1", node_id="n1", producer_version="v1")
        recorder = SpanRecorder(trace_dir=tmp_path, envelope_factory=factory)
        async with recorder.span("test_op", component="test"):
            pass
        recorder.flush()
        files = list((tmp_path / "spans").glob("*.jsonl"))
        assert len(files) == 1

    @pytest.mark.asyncio
    async def test_idempotency_key_spans_never_dropped(self, tmp_path):
        from backend.core.span_recorder import SpanRecorder
        from backend.core.trace_envelope import TraceEnvelopeFactory
        factory = TraceEnvelopeFactory(repo="jarvis", boot_id="b1", runtime_epoch_id="e1", node_id="n1", producer_version="v1")
        recorder = SpanRecorder(trace_dir=tmp_path, envelope_factory=factory, buffer_max=5)
        # Fill buffer with normal spans
        for i in range(10):
            async with recorder.span("normal", component="test"):
                pass
        # Add one with idempotency key
        async with recorder.span("vm_create", component="gcp", idempotency_key="create:vm-1:n1"):
            pass
        # Flush — idempotency span must survive
        recorder.flush()
        files = list((tmp_path / "spans").glob("*.jsonl"))
        content = files[0].read_text()
        assert "create:vm-1:n1" in content
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/backend/core/test_span_recorder.py -v`
Expected: FAIL — `ModuleNotFoundError`

**Step 3: Implement `backend/core/span_recorder.py`**

Create the file with:
- `SpanRecorder` class:
  - `__init__(self, trace_dir, envelope_factory, buffer_max=256)`
  - `span(self, operation, component, idempotency_key=None, caused_by_event_id=None)` — async context manager that:
    - Creates envelope via factory
    - Records start time
    - On `__aexit__`: records end time, status (success/error/cancelled), error details
    - Adds to SpanBuffer
    - Yields span dict for caller inspection
  - `get_recent(n)` → List[Dict]
  - `flush()` — drains buffer to JSONL via TraceStreamManager
  - Uses `SpanBuffer` from `trace_store.py` for backpressure

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/backend/core/test_span_recorder.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add backend/core/span_recorder.py tests/unit/backend/core/test_span_recorder.py
git commit -m "feat: add SpanRecorder with backpressure-aware buffering"
```

---

### Task 7: Boundary Enforcement Middleware

**Files:**
- Create: `backend/core/trace_enforcement.py`
- Test: `tests/unit/backend/core/test_trace_enforcement.py`

**Step 1: Write the failing tests**

```python
"""Tests for boundary enforcement middleware."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


class TestEnforcementDecorator:
    @pytest.mark.asyncio
    async def test_strict_rejects_missing_envelope(self):
        from backend.core.trace_enforcement import enforce_trace, EnforcementMode, set_enforcement_mode
        set_enforcement_mode(EnforcementMode.STRICT)

        @enforce_trace(boundary_type="internal", classification="critical")
        async def my_phase():
            return "ok"

        # No envelope in context → should raise
        with pytest.raises(Exception, match="[Tt]race"):
            await my_phase()

    @pytest.mark.asyncio
    async def test_strict_allows_valid_envelope(self):
        from backend.core.trace_enforcement import enforce_trace, EnforcementMode, set_enforcement_mode
        from backend.core.trace_envelope import TraceEnvelopeFactory
        from backend.core.resilience.correlation_context import CorrelationContext, set_current_context
        set_enforcement_mode(EnforcementMode.STRICT)

        factory = TraceEnvelopeFactory(repo="jarvis", boot_id="b1", runtime_epoch_id="e1", node_id="n1", producer_version="v1")
        ctx = CorrelationContext.create(operation="test")
        set_current_context(ctx)

        @enforce_trace(boundary_type="internal", classification="critical")
        async def my_phase():
            return "ok"

        result = await my_phase()
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_permissive_allows_missing_with_warning(self):
        from backend.core.trace_enforcement import enforce_trace, EnforcementMode, set_enforcement_mode, get_violation_count
        set_enforcement_mode(EnforcementMode.PERMISSIVE)
        initial_count = get_violation_count()

        @enforce_trace(boundary_type="internal", classification="standard")
        async def my_handler():
            return "ok"

        result = await my_handler()
        assert result == "ok"
        assert get_violation_count() > initial_count

    @pytest.mark.asyncio
    async def test_canary_allows_but_alerts(self):
        from backend.core.trace_enforcement import enforce_trace, EnforcementMode, set_enforcement_mode, get_violation_count
        set_enforcement_mode(EnforcementMode.CANARY)
        initial_count = get_violation_count()

        @enforce_trace(boundary_type="http", classification="standard")
        async def my_request():
            return "ok"

        result = await my_request()
        assert result == "ok"
        assert get_violation_count() > initial_count


class TestComplianceScore:
    def test_score_calculation(self):
        from backend.core.trace_enforcement import ComplianceTracker
        tracker = ComplianceTracker()
        tracker.register_boundary("startup_phase", classification="critical")
        tracker.register_boundary("health_check", classification="standard")
        tracker.mark_instrumented("startup_phase")
        score = tracker.get_score()
        assert score["critical_instrumented"] == 1
        assert score["critical_total"] == 1
        assert score["score_critical"] == 100.0
        assert score["total_boundaries"] == 2
        assert score["instrumented"] == 1
        assert score["score_overall"] == 50.0


class TestHTTPEnvelopeInjection:
    def test_inject_headers(self):
        from backend.core.trace_enforcement import inject_trace_headers
        from backend.core.trace_envelope import TraceEnvelopeFactory
        factory = TraceEnvelopeFactory(repo="jarvis", boot_id="b1", runtime_epoch_id="e1", node_id="n1", producer_version="v1")
        env = factory.create_root(component="test", operation="test")
        headers = {}
        inject_trace_headers(headers, env)
        assert "X-Trace-ID" in headers
        assert headers["X-Trace-ID"] == env.trace_id

    def test_extract_headers(self):
        from backend.core.trace_enforcement import extract_trace_from_headers
        from backend.core.trace_envelope import TraceEnvelopeFactory
        factory = TraceEnvelopeFactory(repo="jarvis", boot_id="b1", runtime_epoch_id="e1", node_id="n1", producer_version="v1")
        original = factory.create_root(component="test", operation="test")
        headers = {}
        from backend.core.trace_enforcement import inject_trace_headers
        inject_trace_headers(headers, original)
        restored = extract_trace_from_headers(headers)
        assert restored is not None
        assert restored.trace_id == original.trace_id
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/backend/core/test_trace_enforcement.py -v`
Expected: FAIL — `ModuleNotFoundError`

**Step 3: Implement `backend/core/trace_enforcement.py`**

Create the file with:
- `EnforcementMode` enum (STRICT, CANARY, PERMISSIVE)
- Module-level `_enforcement_mode` (default from env `JARVIS_TRACE_ENFORCEMENT` or PERMISSIVE)
- `set_enforcement_mode(mode)` / `get_enforcement_mode()` — for tests and staged rollout
- `_violation_count: int = 0` — atomic counter for violations
- `get_violation_count()` → int
- `enforce_trace(boundary_type, classification)` — async decorator:
  - Checks `get_current_context()` from correlation_context for envelope
  - STRICT + missing/invalid: raise `TraceEnforcementError`
  - CANARY + missing/invalid: log warning, increment violation counter, proceed
  - PERMISSIVE + missing: log debug, increment counter, proceed
- `inject_trace_headers(headers_dict, envelope)` — adds `X-Trace-*` headers
- `extract_trace_from_headers(headers_dict)` → Optional[TraceEnvelope]
- `inject_trace_env_var(env_dict, envelope)` — adds `JARVIS_TRACE_ENVELOPE` (JSON, <4KB)
- `extract_trace_env_var()` → Optional[TraceEnvelope] (reads from os.environ)
- `ComplianceTracker` class — tracks registered vs instrumented boundaries, computes score

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/backend/core/test_trace_enforcement.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add backend/core/trace_enforcement.py tests/unit/backend/core/test_trace_enforcement.py
git commit -m "feat: add boundary enforcement middleware with compliance tracking"
```

---

### Task 8: SQLite Trace Index

**Files:**
- Modify: `backend/core/trace_store.py`
- Test: `tests/unit/backend/core/test_trace_index.py`

**Step 1: Write the failing tests**

```python
"""Tests for SQLite trace index (rebuildable cache)."""
import json
import time
import pytest
from pathlib import Path


class TestTraceIndex:
    def test_index_event(self, tmp_path):
        from backend.core.trace_store import TraceIndex
        idx = TraceIndex(tmp_path / "index" / "trace_index.sqlite")
        idx.index_event(
            trace_id="t1", event_id="e1", stream="lifecycle",
            file_path="lifecycle/20260224.jsonl", byte_offset=0,
            ts_wall_utc=time.time(), operation="phase_enter", status="success"
        )
        results = idx.query_by_trace("t1")
        assert len(results) == 1
        assert results[0]["event_id"] == "e1"

    def test_query_by_time_range(self, tmp_path):
        from backend.core.trace_store import TraceIndex
        idx = TraceIndex(tmp_path / "index" / "trace_index.sqlite")
        now = time.time()
        idx.index_event("t1", "e1", "lifecycle", "f.jsonl", 0, now - 100, "op1", "success")
        idx.index_event("t1", "e2", "lifecycle", "f.jsonl", 50, now - 50, "op2", "success")
        idx.index_event("t1", "e3", "lifecycle", "f.jsonl", 100, now, "op3", "success")
        results = idx.query_by_time(since=now - 75, until=now - 25)
        assert len(results) == 1
        assert results[0]["event_id"] == "e2"


class TestCausalityIndex:
    def test_add_and_query_edge(self, tmp_path):
        from backend.core.trace_store import CausalityIndex
        idx = CausalityIndex(tmp_path / "index" / "causality_edges.sqlite")
        idx.add_edge(event_id="e2", caused_by_event_id="e1", parent_span_id="s1",
                     trace_id="t1", operation="recovery", ts_wall_utc=time.time())
        children = idx.get_children("e1")
        assert len(children) == 1
        assert children[0]["event_id"] == "e2"

    def test_detect_cycle(self, tmp_path):
        from backend.core.trace_store import CausalityIndex
        idx = CausalityIndex(tmp_path / "index" / "causality_edges.sqlite")
        idx.add_edge("e1", None, None, "t1", "boot", time.time())
        idx.add_edge("e2", "e1", "s1", "t1", "phase", time.time())
        idx.add_edge("e3", "e2", "s2", "t1", "recovery", time.time())
        cycles = idx.detect_cycles()
        assert len(cycles) == 0

    def test_detect_self_cycle(self, tmp_path):
        from backend.core.trace_store import CausalityIndex
        idx = CausalityIndex(tmp_path / "index" / "causality_edges.sqlite")
        idx.add_edge("e1", "e1", None, "t1", "broken", time.time())  # self-reference
        cycles = idx.detect_cycles()
        assert len(cycles) > 0

    def test_rebuild_from_jsonl(self, tmp_path):
        from backend.core.trace_store import TraceIndex, JSONLWriter
        # Write some JSONL records
        writer = JSONLWriter(tmp_path / "lifecycle" / "test.jsonl")
        writer.append({"envelope": {"trace_id": "t1", "event_id": "e1", "ts_wall_utc": time.time()}, "event_type": "boot_start"})
        writer.append({"envelope": {"trace_id": "t1", "event_id": "e2", "ts_wall_utc": time.time()}, "event_type": "phase_enter"})
        idx = TraceIndex(tmp_path / "index" / "trace_index.sqlite")
        rebuilt = idx.rebuild_from_directory(tmp_path / "lifecycle", stream="lifecycle")
        assert rebuilt == 2
        results = idx.query_by_trace("t1")
        assert len(results) == 2
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/backend/core/test_trace_index.py -v`
Expected: FAIL

**Step 3: Add to `backend/core/trace_store.py`**

Add classes:
- `TraceIndex` — SQLite-backed trace lookup
  - `__init__(self, db_path)` — creates db + tables if not exist, with generation marker
  - `index_event(trace_id, event_id, stream, file_path, byte_offset, ts_wall_utc, operation, status)` — INSERT OR REPLACE
  - `query_by_trace(trace_id)` → List[Dict]
  - `query_by_time(since, until)` → List[Dict]
  - `rebuild_from_directory(dir_path, stream)` → int (count of indexed records)
- `CausalityIndex` — SQLite-backed causality DAG
  - `__init__(self, db_path)` — creates db + tables if not exist
  - `add_edge(event_id, caused_by_event_id, parent_span_id, trace_id, operation, ts_wall_utc)`
  - `get_children(event_id)` → List[Dict]
  - `get_parent(event_id)` → Optional[Dict]
  - `detect_cycles()` → List[List[str]] (returns list of cycles found via DFS)

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/backend/core/test_trace_index.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add backend/core/trace_store.py tests/unit/backend/core/test_trace_index.py
git commit -m "feat: add SQLite trace index and causality DAG with cycle detection"
```

---

### Task 9: Cross-Repo Contract Fixture

**Files:**
- Create: `tests/fixtures/trace_envelope_v1.json`
- Test: `tests/unit/backend/core/test_trace_contract.py`

**Step 1: Create the shared fixture**

```json
{
  "description": "Canonical TraceEnvelope v1 test fixture — shared across JARVIS, Prime, Reactor-Core",
  "schema_version": 1,
  "min_supported": 1,
  "max_supported": 1,
  "test_cases": [
    {
      "name": "root_span_boot",
      "envelope": {
        "trace_id": "test-trace-001-abcdef",
        "span_id": "test-span-001-abcdef",
        "event_id": "test-event-001-abcdef",
        "parent_span_id": null,
        "sequence": 1,
        "boot_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
        "runtime_epoch_id": "epoch-2026-02-24-001",
        "process_id": 12345,
        "node_id": "dereks-macbook",
        "ts_wall_utc": 1740422400.0,
        "ts_mono_local": 1000.0,
        "repo": "jarvis",
        "component": "unified_supervisor",
        "operation": "boot_start",
        "boundary_type": "internal",
        "caused_by_event_id": null,
        "idempotency_key": null,
        "producer_version": "v270.1-abc1234",
        "schema_version": 1,
        "extra": {}
      },
      "expect_valid": true,
      "expect_errors": []
    },
    {
      "name": "cross_repo_child_with_causality",
      "envelope": {
        "trace_id": "test-trace-001-abcdef",
        "span_id": "test-span-002-ghijkl",
        "event_id": "test-event-002-ghijkl",
        "parent_span_id": "test-span-001-abcdef",
        "sequence": 5,
        "boot_id": "b2c3d4e5-f6a7-8901-bcde-f12345678901",
        "runtime_epoch_id": "epoch-2026-02-24-001",
        "process_id": 67890,
        "node_id": "gcp-vm-jarvis-prime",
        "ts_wall_utc": 1740422401.5,
        "ts_mono_local": 50.3,
        "repo": "jarvis-prime",
        "component": "inference_server",
        "operation": "model_load",
        "boundary_type": "http",
        "caused_by_event_id": "test-event-001-abcdef",
        "idempotency_key": "load_model:gguf-q8:nonce-abc",
        "producer_version": "v3.2.1-def5678",
        "schema_version": 1,
        "extra": {}
      },
      "expect_valid": true,
      "expect_errors": []
    },
    {
      "name": "invalid_empty_trace_id",
      "envelope": {
        "trace_id": "",
        "span_id": "s1",
        "event_id": "e1",
        "parent_span_id": null,
        "sequence": 1,
        "boot_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
        "runtime_epoch_id": "epoch-001",
        "process_id": 1,
        "node_id": "test",
        "ts_wall_utc": 1740422400.0,
        "ts_mono_local": 1.0,
        "repo": "jarvis",
        "component": "test",
        "operation": "test",
        "boundary_type": "internal",
        "caused_by_event_id": null,
        "idempotency_key": null,
        "producer_version": "v1",
        "schema_version": 1,
        "extra": {}
      },
      "expect_valid": false,
      "expect_errors": ["trace_id"]
    },
    {
      "name": "future_schema_version",
      "envelope": {
        "trace_id": "t1",
        "span_id": "s1",
        "event_id": "e1",
        "parent_span_id": null,
        "sequence": 1,
        "boot_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
        "runtime_epoch_id": "epoch-001",
        "process_id": 1,
        "node_id": "test",
        "ts_wall_utc": 1740422400.0,
        "ts_mono_local": 1.0,
        "repo": "jarvis",
        "component": "test",
        "operation": "test",
        "boundary_type": "internal",
        "caused_by_event_id": null,
        "idempotency_key": null,
        "producer_version": "v99.0",
        "schema_version": 99,
        "extra": {"future_field": "future_value"}
      },
      "expect_valid": false,
      "expect_errors": ["schema_version"]
    },
    {
      "name": "unknown_extra_fields_preserved",
      "envelope": {
        "trace_id": "t1",
        "span_id": "s1",
        "event_id": "e1",
        "parent_span_id": null,
        "sequence": 1,
        "boot_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
        "runtime_epoch_id": "epoch-001",
        "process_id": 1,
        "node_id": "test",
        "ts_wall_utc": 1740422400.0,
        "ts_mono_local": 1.0,
        "repo": "jarvis",
        "component": "test",
        "operation": "test",
        "boundary_type": "internal",
        "caused_by_event_id": null,
        "idempotency_key": null,
        "producer_version": "v1",
        "schema_version": 1,
        "extra": {"custom_metadata": "preserved", "another_field": 42}
      },
      "expect_valid": true,
      "expect_errors": []
    }
  ]
}
```

**Step 2: Write contract tests**

```python
"""Cross-repo contract tests for TraceEnvelope v1.
These tests consume the shared fixture and verify serialization/validation.
The same fixture should be consumed by JARVIS-Prime and Reactor-Core CI."""
import json
import pytest
from pathlib import Path


FIXTURE_PATH = Path(__file__).parent.parent.parent / "fixtures" / "trace_envelope_v1.json"


@pytest.fixture
def fixture_data():
    assert FIXTURE_PATH.exists(), f"Contract fixture not found: {FIXTURE_PATH}"
    return json.loads(FIXTURE_PATH.read_text())


class TestTraceEnvelopeContract:
    def test_fixture_schema_version_matches(self, fixture_data):
        from backend.core.trace_envelope import TRACE_SCHEMA_VERSION
        assert fixture_data["schema_version"] == TRACE_SCHEMA_VERSION

    @pytest.mark.parametrize("case_idx", range(5))
    def test_deserialize_fixture_case(self, fixture_data, case_idx):
        from backend.core.trace_envelope import TraceEnvelope
        case = fixture_data["test_cases"][case_idx]
        env = TraceEnvelope.from_dict(case["envelope"])
        assert env.trace_id == case["envelope"]["trace_id"]
        assert env.schema_version == case["envelope"]["schema_version"]

    @pytest.mark.parametrize("case_idx", range(5))
    def test_validate_fixture_case(self, fixture_data, case_idx):
        from backend.core.trace_envelope import TraceEnvelope, validate_envelope
        case = fixture_data["test_cases"][case_idx]
        env = TraceEnvelope.from_dict(case["envelope"])
        errors = validate_envelope(env)
        if case["expect_valid"]:
            assert errors == [], f"Expected valid but got errors: {errors}"
        else:
            assert len(errors) > 0, "Expected invalid but got no errors"
            for expected_field in case["expect_errors"]:
                assert any(expected_field in e for e in errors), \
                    f"Expected error about '{expected_field}' in {errors}"

    @pytest.mark.parametrize("case_idx", range(5))
    def test_round_trip_preserves_all_fields(self, fixture_data, case_idx):
        from backend.core.trace_envelope import TraceEnvelope
        case = fixture_data["test_cases"][case_idx]
        env = TraceEnvelope.from_dict(case["envelope"])
        round_tripped = TraceEnvelope.from_dict(env.to_dict())
        assert round_tripped.trace_id == env.trace_id
        assert round_tripped.extra == env.extra

    def test_extra_fields_preserved(self, fixture_data):
        from backend.core.trace_envelope import TraceEnvelope
        case = fixture_data["test_cases"][4]  # unknown_extra_fields_preserved
        env = TraceEnvelope.from_dict(case["envelope"])
        assert env.extra.get("custom_metadata") == "preserved"
        assert env.extra.get("another_field") == 42
```

**Step 3: Run tests to verify they pass** (depends on Task 1 being complete)

Run: `python3 -m pytest tests/unit/backend/core/test_trace_contract.py -v`
Expected: ALL PASS

**Step 4: Commit**

```bash
git add tests/fixtures/trace_envelope_v1.json tests/unit/backend/core/test_trace_contract.py
git commit -m "feat: add cross-repo TraceEnvelope v1 contract fixture and tests"
```

---

### Task 10: Fault Injection Framework

**Files:**
- Create: `tests/adversarial/conftest.py`
- Create: `tests/adversarial/fault_injector.py`
- Test: `tests/adversarial/test_fault_injector.py`

**Step 1: Write the failing tests**

```python
"""Tests for the fault injection framework itself."""
import asyncio
import pytest


class TestFaultInjector:
    def test_register_and_trigger_fault(self):
        from tests.adversarial.fault_injector import FaultInjector, FaultType
        injector = FaultInjector()
        injector.register(boundary="prime_client.request", fault_type=FaultType.NETWORK_PARTITION)
        fault = injector.check("prime_client.request")
        assert fault is not None
        assert fault.fault_type == FaultType.NETWORK_PARTITION
        # Second check: fault consumed (one-shot by default)
        assert injector.check("prime_client.request") is None

    def test_probabilistic_fault(self):
        from tests.adversarial.fault_injector import FaultInjector, FaultType
        injector = FaultInjector(seed=42)  # Deterministic for testing
        injector.register_probabilistic("health_check.*", FaultType.TIMEOUT_AFTER_SUCCESS, probability=1.0)
        fault = injector.check("health_check.prime")
        assert fault is not None

    def test_no_fault_when_unregistered(self):
        from tests.adversarial.fault_injector import FaultInjector
        injector = FaultInjector()
        assert injector.check("unknown_boundary") is None

    @pytest.mark.asyncio
    async def test_inject_timeout_after_success(self):
        from tests.adversarial.fault_injector import FaultInjector, FaultType, apply_fault
        injector = FaultInjector()
        injector.register("my_op", FaultType.TIMEOUT_AFTER_SUCCESS, params={"delay_s": 0.01})

        call_count = 0
        async def my_operation():
            nonlocal call_count
            call_count += 1
            return "success"

        fault = injector.check("my_op")
        with pytest.raises(asyncio.TimeoutError):
            await apply_fault(fault, my_operation(), timeout=0.005)
        # Operation DID execute (success-after-timeout)
        assert call_count == 1

    def test_clock_jump_fault(self):
        from tests.adversarial.fault_injector import FaultInjector, FaultType, MockClock
        injector = FaultInjector()
        clock = MockClock()
        injector.register("timer_check", FaultType.CLOCK_JUMP_FORWARD, params={"jump_s": 60})
        fault = injector.check("timer_check")
        assert fault is not None
        clock.apply_fault(fault)
        assert clock.wall_offset == 60
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/adversarial/test_fault_injector.py -v`
Expected: FAIL — `ModuleNotFoundError`

**Step 3: Implement**

Create `tests/adversarial/__init__.py` (empty).

Create `tests/adversarial/conftest.py`:
- Shared fixtures: `fault_injector`, `mock_clock`, `tmp_trace_dir`, `envelope_factory`

Create `tests/adversarial/fault_injector.py`:
- `FaultType` enum: NETWORK_PARTITION, PARTIAL_PARTITION, TIMEOUT_AFTER_SUCCESS, DELAYED_DUPLICATE, CLOCK_JUMP_FORWARD, CLOCK_JUMP_BACKWARD, CRASH_MID_COMMIT, SUSPEND_RESUME
- `FaultSpec` dataclass: fault_type, params, one_shot
- `FaultInjector` class:
  - `register(boundary, fault_type, params=None, one_shot=True)`
  - `register_probabilistic(pattern, fault_type, probability, params=None)`
  - `check(boundary)` → Optional[FaultSpec] (consumes one-shot faults)
  - `clear()`
  - Deterministic via optional `seed` parameter
- `apply_fault(fault_spec, coro, timeout)` — applies fault semantics to an async operation
- `MockClock` class:
  - `wall_offset: float = 0` — added to `time.time()` results
  - `mono_offset: float = 0` — added to `time.monotonic()` results
  - `apply_fault(fault_spec)` — applies clock jump
  - Can be used as context manager to patch `time.time` and `time.monotonic`

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/adversarial/test_fault_injector.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add tests/adversarial/
git commit -m "feat: add fault injection framework for adversarial testing"
```

---

### Task 11: Replay Engine + 10 Invariant Tests

**Files:**
- Create: `tests/adversarial/replay_engine.py`
- Create: `tests/adversarial/invariant_checks.py`
- Create: `tests/adversarial/test_startup_determinism.py`
- Create: `tests/adversarial/test_recovery_integrity.py`
- Create: `tests/adversarial/test_boundary_propagation.py`
- Create: `tests/adversarial/test_ordering_guarantees.py`

**Step 1: Write the replay engine**

Create `tests/adversarial/replay_engine.py`:
- `ReplayEngine` class:
  - `load_streams(lifecycle_dir, decisions_dir)` — reads JSONL files, parses into events
  - `sort_events()` — sorts by: causal DAG (primary), Lamport sequence (secondary), ts_wall_utc (tertiary)
  - `replay(checker: InvariantChecker)` → ReplayResult
  - `replay_with_faults(faults, checker)` → ReplayResult
- `ReplayResult` dataclass: passed (bool), violations (List[str]), events_processed (int)

Create `tests/adversarial/invariant_checks.py`:
- `InvariantChecker` class with methods for each invariant
- Each method returns List[str] (violations found, empty if pass)

**Step 2: Write the 10 invariant tests**

Create `tests/adversarial/test_startup_determinism.py`:
```python
"""Startup determinism invariants."""
import json
import pytest
from pathlib import Path


class TestNoOrphanLifecyclePhases:
    """Every phase_enter has a matching phase_exit or phase_fail."""
    def test_with_clean_startup(self, tmp_path):
        from tests.adversarial.invariant_checks import check_no_orphan_phases
        events = _make_clean_startup_events()
        _write_events(tmp_path, events)
        violations = check_no_orphan_phases(tmp_path / "lifecycle")
        assert violations == []

    def test_detects_orphan(self, tmp_path):
        from tests.adversarial.invariant_checks import check_no_orphan_phases
        events = _make_clean_startup_events()
        # Remove the phase_exit for preflight
        events = [e for e in events if not (e.get("event_type") == "phase_exit" and e.get("phase") == "preflight")]
        _write_events(tmp_path, events)
        violations = check_no_orphan_phases(tmp_path / "lifecycle")
        assert len(violations) > 0
        assert "preflight" in violations[0]


class TestStartupPhaseDAGConsistency:
    """Phase transitions respect declared dependency DAG."""
    def test_correct_order(self, tmp_path):
        from tests.adversarial.invariant_checks import check_phase_dag_consistency
        events = _make_clean_startup_events()
        _write_events(tmp_path, events)
        violations = check_phase_dag_consistency(tmp_path / "lifecycle")
        assert violations == []


class TestDeterministicReplay:
    """Same event stream produces identical final state twice."""
    def test_replay_determinism(self, tmp_path):
        from tests.adversarial.replay_engine import ReplayEngine
        events = _make_clean_startup_events()
        _write_events(tmp_path, events)
        engine = ReplayEngine()
        engine.load_streams(tmp_path / "lifecycle", tmp_path / "decisions")
        result1 = engine.replay()
        result2 = engine.replay()
        assert result1.final_state == result2.final_state


# Helper functions
def _make_clean_startup_events():
    """Create a valid startup event sequence for testing."""
    from backend.core.trace_envelope import TraceEnvelopeFactory
    factory = TraceEnvelopeFactory(repo="jarvis", boot_id="b1", runtime_epoch_id="e1", node_id="n1", producer_version="v1")
    boot = factory.create_root(component="supervisor", operation="boot_start")
    phases = ["clean_slate", "preflight", "resources", "backend", "intelligence", "trinity", "enterprise"]
    events = [{"event_type": "boot_start", "envelope": boot.to_dict()}]
    prev_event_id = boot.event_id
    for phase in phases:
        enter_env = factory.create_child(parent=boot, component="supervisor", operation=f"phase_{phase}", caused_by_event_id=prev_event_id)
        events.append({"event_type": "phase_enter", "phase": phase, "envelope": enter_env.to_dict()})
        exit_env = factory.create_event_from(enter_env)
        events.append({"event_type": "phase_exit", "phase": phase, "to_state": "success", "envelope": exit_env.to_dict()})
        prev_event_id = exit_env.event_id
    events.append({"event_type": "boot_complete", "envelope": factory.create_child(parent=boot, component="supervisor", operation="boot_complete").to_dict()})
    return events

def _write_events(tmp_path, events):
    import json
    lifecycle_dir = tmp_path / "lifecycle"
    lifecycle_dir.mkdir(parents=True, exist_ok=True)
    decisions_dir = tmp_path / "decisions"
    decisions_dir.mkdir(parents=True, exist_ok=True)
    with open(lifecycle_dir / "test_epoch.jsonl", "w") as f:
        for event in events:
            f.write(json.dumps(event) + "\n")
```

Create similar test files for:
- `test_recovery_integrity.py`: tests 4-6 (no duplicate side-effect, causal chain, timeout-after-success)
- `test_boundary_propagation.py`: tests 7-8 (critical boundaries carry envelope, cross-repo round-trip)
- `test_ordering_guarantees.py`: tests 9-10 (Lamport monotonic, causality DAG acyclic)

Each test file follows the same pattern: create events, write to JSONL, run invariant check, assert violations.

**Step 3: Run tests to verify they pass**

Run: `python3 -m pytest tests/adversarial/ -v`
Expected: ALL PASS

**Step 4: Commit**

```bash
git add tests/adversarial/
git commit -m "feat: add replay engine and 10 CI-gating invariant tests"
```

---

### Task 12: Compliance Score CI Gate + Disk Guard + Compaction

**Files:**
- Modify: `backend/core/trace_store.py` (add DiskGuard full implementation, compaction)
- Modify: `backend/core/trace_enforcement.py` (add CI score output)
- Test: `tests/unit/backend/core/test_disk_guard.py`
- Test: `tests/adversarial/test_compliance_gate.py`

**Step 1: Write the failing tests**

```python
# test_disk_guard.py
"""Tests for DiskGuard and compaction."""
import gzip
import json
import time
import pytest
from pathlib import Path


class TestDiskGuard:
    def test_reports_usage(self, tmp_path):
        from backend.core.trace_store import DiskGuard
        guard = DiskGuard(base_dir=tmp_path)
        usage = guard.check_disk_usage()
        assert 0.0 <= usage <= 1.0

    def test_rotation_priority(self, tmp_path):
        from backend.core.trace_store import DiskGuard
        guard = DiskGuard(base_dir=tmp_path, critical_threshold=0.0)  # Force rotation
        # Create old files in each stream
        for stream in ["spans", "decisions", "lifecycle"]:
            d = tmp_path / stream
            d.mkdir(parents=True, exist_ok=True)
            (d / "old_file.jsonl").write_text('{"old": true}\n')
        rotated = guard.rotate_if_needed(current_epoch="current-epoch")
        # Spans should be rotated first
        assert (tmp_path / "spans" / "old_file.jsonl").exists() is False


class TestCompaction:
    def test_compress_old_files(self, tmp_path):
        from backend.core.trace_store import compact_old_files
        spans_dir = tmp_path / "spans"
        spans_dir.mkdir()
        (spans_dir / "old.jsonl").write_text('{"data": "test"}\n')
        compact_old_files(spans_dir, max_age_days=0)  # Compress everything
        assert (spans_dir / "old.jsonl.gz").exists()
        assert not (spans_dir / "old.jsonl").exists()
```

```python
# test_compliance_gate.py
"""Tests for CI compliance gate."""
import pytest


class TestComplianceGate:
    def test_fails_when_critical_below_100(self):
        from backend.core.trace_enforcement import ComplianceTracker
        tracker = ComplianceTracker()
        tracker.register_boundary("phase_transition", "critical")
        tracker.register_boundary("health_check", "standard")
        # Don't instrument critical boundary
        tracker.mark_instrumented("health_check")
        score = tracker.get_score()
        assert score["score_critical"] < 100.0
        assert tracker.ci_gate_passes() is False

    def test_passes_when_all_critical_instrumented(self):
        from backend.core.trace_enforcement import ComplianceTracker
        tracker = ComplianceTracker()
        tracker.register_boundary("phase_transition", "critical")
        tracker.register_boundary("health_check", "standard")
        tracker.mark_instrumented("phase_transition")
        tracker.mark_instrumented("health_check")
        assert tracker.ci_gate_passes() is True
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/backend/core/test_disk_guard.py tests/adversarial/test_compliance_gate.py -v`
Expected: FAIL

**Step 3: Implement**

Add to `trace_store.py`:
- `DiskGuard` full implementation:
  - `check_disk_usage()` → float (uses `shutil.disk_usage()`)
  - `rotate_if_needed(current_epoch)` → List[str] (rotated files)
  - Rotation priority: spans → old decisions → old lifecycle (never current epoch)
  - Configurable thresholds via env vars
- `compact_old_files(dir_path, max_age_days)` — gzip old JSONL files
- Per-stream retention via env vars (from design doc)

Add to `trace_enforcement.py`:
- `ComplianceTracker.ci_gate_passes()` → bool (critical=100%, overall>=80%)
- `ComplianceTracker.to_json()` → str (for CI output)

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/backend/core/test_disk_guard.py tests/adversarial/test_compliance_gate.py -v`
Expected: ALL PASS

**Step 5: Run full test suite**

Run: `python3 -m pytest tests/unit/backend/core/test_trace_envelope.py tests/unit/backend/core/test_correlation_trace_integration.py tests/unit/backend/core/test_trace_store.py tests/unit/backend/core/test_lifecycle_emitter.py tests/unit/backend/core/test_decision_log_persistence.py tests/unit/backend/core/test_span_recorder.py tests/unit/backend/core/test_trace_enforcement.py tests/unit/backend/core/test_trace_index.py tests/unit/backend/core/test_trace_contract.py tests/adversarial/ -v`
Expected: ALL PASS

**Step 6: Commit**

```bash
git add backend/core/trace_store.py backend/core/trace_enforcement.py tests/unit/backend/core/test_disk_guard.py tests/adversarial/test_compliance_gate.py
git commit -m "feat: add disk guard, compaction, and CI compliance gate"
```

---

## Summary

| Task | Files Created | Files Modified | Tests |
|------|--------------|----------------|-------|
| 1 | `trace_envelope.py` | — | 18 tests |
| 2 | `test_correlation_trace_integration.py` | `correlation_context.py` | 7 tests |
| 3 | `trace_store.py` | — | 8 tests |
| 4 | `lifecycle_emitter.py` | `unified_supervisor.py` | 6 tests |
| 5 | `test_decision_log_persistence.py` | `decision_log.py` | 6 tests |
| 6 | `span_recorder.py` | — | 4 tests |
| 7 | `trace_enforcement.py` | — | 7 tests |
| 8 | `test_trace_index.py` | `trace_store.py` | 6 tests |
| 9 | `trace_envelope_v1.json`, `test_trace_contract.py` | — | 12 tests |
| 10 | `fault_injector.py`, `conftest.py` | — | 5 tests |
| 11 | `replay_engine.py`, `invariant_checks.py`, 4 test files | — | 10+ tests |
| 12 | `test_disk_guard.py`, `test_compliance_gate.py` | `trace_store.py`, `trace_enforcement.py` | 5 tests |

**Total: ~12 new files, ~6 modified files, ~94 tests**
