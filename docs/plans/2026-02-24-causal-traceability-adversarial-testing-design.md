# Causal Traceability & Adversarial Testing — Design Document

**Date:** 2026-02-24
**Status:** Approved
**Scope:** JARVIS + JARVIS-Prime + Reactor-Core (cross-repo)

## Problem Statement

The JARVIS ecosystem lacks **causal traceability** and **adversarial test coverage**:

1. **Observability without causal traceability**: Metrics and logs exist across 358+ backend files (12,455 logging statements), but without cross-repo trace IDs and lifecycle correlation, root-cause diagnosis remains slow and ambiguous. Every major root cause documented in MEMORY.md (9-min stall, ECAPA segfault, APARS blackout, version mismatch recycle loop) was discovered through painful manual session-by-session diagnosis. Causal tracing would have surfaced each in minutes.

2. **Test strategy underweights adversarial scenarios**: Unit tests are necessary but insufficient. The system lacks fault-injection, chaos testing, replay, and deterministic simulation for race/ordering/recovery invariants. Production bugs come from concurrent edge cases (timeout-after-success, asymmetric partitions, PID reuse, clock skew) that sequential unit tests cannot catch.

## Root Cause

The disease is **not** missing tools — it's **missing mandatory contract**. Four observability primitives exist as islands:

| Primitive | Location | Disease |
|---|---|---|
| `correlation_context.py` (548 lines) | `backend/core/resilience/` | Excellent design. Only adopted in ~30/358 files (~8% boundary coverage). |
| `decision_log.py` (233 lines) | `backend/core/` | In-memory ring buffer. Lost on restart. Post-mortem impossible. |
| `telemetry_emitter.py` (913 lines) | `backend/core/` | Disk-backed, persistent. Training-focused, no trace correlation. |
| `idempotency_registry.py` (388 lines) | `backend/core/` | In-memory only. No trace linkage to decisions that trigger operations. |

Without a required envelope at every boundary, traces stop at repo edges, decisions have no causal chain, and lifecycle events cannot be replayed.

## Approach: Contract-First Trace Envelope (Approach C — Hybrid)

Define a **single canonical TraceEnvelope** shared across all three repos. Every boundary crossing must carry this immutable envelope. Build persistent trace stores and adversarial test infrastructure on top of the contract.

### Non-Negotiable Rules

1. No boundary crossing without `trace_id` + `span_id` + `parent_span_id`.
2. No autonomous recovery action without a persisted decision record (reason + inputs + chosen action).
3. No startup transition without emitted lifecycle event in a persistent store.
4. No retries without idempotency key for side-effecting operations.
5. No release unless chaos/fault suite passes determinism invariants.

---

## Section 1: TraceEnvelope v1 Schema

### Core Schema

```python
from __future__ import annotations
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class BoundaryType(str, Enum):
    HTTP = "http"
    IPC = "ipc"
    FILE_RPC = "file_rpc"
    EVENT_BUS = "event_bus"
    SUBPROCESS = "subprocess"
    INTERNAL = "internal"


TRACE_SCHEMA_VERSION = 1
TRACE_SCHEMA_MIN_SUPPORTED = 1
TRACE_SCHEMA_MAX_SUPPORTED = 1


@dataclass(frozen=True)
class TraceEnvelope:
    """Immutable trace envelope carried at every boundary crossing.

    Primary replay truth: causal DAG via parent_span_id + caused_by_event_id.
    Secondary ordering: Lamport sequence per (boot_id, process_id).
    Tertiary correlation: ts_wall_utc for cross-process human-readable stitching.
    """

    # === Identity ===
    trace_id: str                              # Root trace (persists across entire causal chain)
    span_id: str                               # This operation's span
    event_id: str                              # Unique per emission (one span -> many events)
    parent_span_id: Optional[str]              # None for root spans

    # === Ordering (Lamport) ===
    sequence: int                              # Lamport counter: on receive, max(local, incoming) + 1
    boot_id: str                               # Unique per process boot (uuid4)
    runtime_epoch_id: str                      # Supervisor-level epoch, propagated to all repos
    process_id: int                            # os.getpid()
    node_id: str                               # hostname or container ID

    # === Timing (dual clock) ===
    ts_wall_utc: float                         # time.time() -- cross-process correlation
    ts_mono_local: float                       # time.monotonic() -- local ordering, immune to NTP/suspend

    # === Origin ===
    repo: str                                  # "jarvis" | "jarvis-prime" | "reactor-core"
    component: str                             # "gcp_vm_manager" | "prime_router" etc.
    operation: str                             # "create_vm" | "promote_endpoint" etc.
    boundary_type: BoundaryType                # How this envelope crossed into current context

    # === Causality ===
    caused_by_event_id: Optional[str]          # Explicit causal link -- None if root cause
    idempotency_key: Optional[str]             # Links to IdempotencyRegistry -- None if not side-effecting

    # === Provenance ===
    producer_version: str                      # Repo version/commit hash (git short SHA or semver)
    schema_version: int                        # Starting at 1

    # === Forward compatibility ===
    extra: Dict[str, Any] = field(default_factory=dict)  # Unknown fields preserved, never dropped
```

### Lamport Clock

```python
class LamportClock:
    """Per-process Lamport clock. Thread-safe via locked integer."""

    def __init__(self) -> None:
        self._value: int = 0
        self._lock = threading.Lock()

    def tick(self) -> int:
        """Local event: increment and return."""
        with self._lock:
            self._value += 1
            return self._value

    def receive(self, incoming_seq: int) -> int:
        """On receiving envelope: max(local, incoming) + 1."""
        with self._lock:
            self._value = max(self._value, incoming_seq) + 1
            return self._value

    @property
    def current(self) -> int:
        with self._lock:
            return self._value
```

### Schema Compatibility Policy

| Boundary Classification | `version < min_supported` | `version > max_supported` |
|---|---|---|
| Critical control-plane (startup, recovery, promote/demote) | Reject + error | Reject + error |
| Standard operational (health, progress, inference) | Reject + error | Warn + accept |
| Observability-only (log flush, metrics) | Warn + accept | Warn + accept |

### Validation Rules

- `trace_id`, `span_id`, `event_id`: non-empty, max 64 chars
- `sequence`: > 0, monotonically increasing per `(boot_id, process_id)`
- `boot_id`, `runtime_epoch_id`: valid uuid4 format
- `ts_wall_utc`: within +/-300s of receiver's wall clock (detects gross clock skew)
- `ts_mono_local`: > 0 (sanity only -- not comparable across processes)
- `repo`: must be in `{"jarvis", "jarvis-prime", "reactor-core"}`
- `schema_version`: checked per boundary classification table above
- Unknown fields in `extra`: preserved, counted in schema drift metric per `producer_version`

---

## Section 2: Persistent Trace Store — Three Streams, One Index

### Stream Architecture

```
~/.jarvis/traces/
├── lifecycle/                    # Stream 1: Startup, shutdown, phase transitions
│   ├── 20260224_epoch_a1b2c3.jsonl
│   └── 20260224_epoch_d4e5f6.jsonl
├── decisions/                    # Stream 2: Recovery actions, routing changes, VM ops
│   ├── 20260224.jsonl
│   └── 20260225.jsonl
├── spans/                        # Stream 3: Operational spans (health, inference, auth)
│   ├── 20260224.jsonl
│   └── 20260225.jsonl
└── index/                        # Rebuildable cache (not source of truth)
    ├── trace_index.sqlite        # trace_id -> file:offset
    └── causality_edges.sqlite    # event_id -> caused_by_event_id DAG
```

### Stream 1: Lifecycle Events (Never Dropped)

```python
@dataclass(frozen=True)
class LifecycleEvent:
    envelope: TraceEnvelope
    event_type: str          # "phase_enter" | "phase_exit" | "phase_fail" |
                             # "boot_start" | "boot_complete" | "shutdown_start" |
                             # "recovery_start" | "recovery_complete" | "recovery_fail"
    phase: Optional[str]     # "clean_slate" | "preflight" | "backend" | "trinity" etc.
    from_state: Optional[str]
    to_state: Optional[str]
    evidence: Dict[str, Any] # Measurements/context that caused the transition
```

**Backpressure policy:**
- Write buffer: 64 events max in-memory, flush on every phase transition or every 2s
- Write failure: retry 3x with 100ms backoff, then write to stderr as last resort
- **Never dropped.** Disk-full: rotate oldest non-current-epoch files (spans first, then old decisions, lifecycle last, never current epoch)

### Stream 2: Decision Events (Extends decision_log.py)

```python
@dataclass
class DecisionRecord:
    envelope: TraceEnvelope       # NEW: causal link to trace
    decision_type: str
    reason: str
    inputs: Dict[str, Any]
    outcome: str
    timestamp: float = field(default_factory=time.time)
    component: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
```

**Flusher design:**
- Background asyncio task, 5s interval
- Drains in-memory buffer snapshot -> date-partitioned JSONL
- Atomic append: `open(..., O_APPEND)` + `fsync` + `fcntl.LOCK_EX`
- Line-level checksums for torn write detection
- On flush failure: events stay in memory, retry next cycle

### Stream 3: Operational Spans (Droppable Under Pressure)

```python
@dataclass(frozen=True)
class SpanRecord:
    envelope: TraceEnvelope
    duration_ms: Optional[float]
    status: str                    # "success" | "error" | "timeout" | "cancelled"
    error_class: Optional[str]
    error_message: Optional[str]
    metadata: Dict[str, Any]
```

**Backpressure policy:**
- Write buffer: 256 events max, flush every 5s
- Buffer > 80%: sample at 50% (keep all errors, sample successes)
- Buffer > 95%: keep only errors and timeouts
- **Never drop spans with `idempotency_key` set** (side-effecting ops always persisted)

### Disk Guard

```python
class DiskGuard:
    """Monitors trace store disk usage, triggers rotation before emergency."""

    WARNING_THRESHOLD = 0.85   # 85% disk usage -> warning
    CRITICAL_THRESHOLD = 0.95  # 95% -> emergency rotation

    # Rotation priority (drop order):
    # 1. Compress old spans (> 7d)
    # 2. Delete compressed spans (> 30d)
    # 3. Compress old decisions (> 30d)
    # 4. Delete compressed decisions (> 60d)
    # 5. Compress old lifecycle (> 60d)
    # 6. Delete compressed lifecycle (> 90d)
    # 7. NEVER touch current-epoch lifecycle files
```

### Index Layer (Rebuildable SQLite Cache)

```sql
-- trace_index.sqlite
CREATE TABLE trace_lookup (
    trace_id TEXT NOT NULL,
    event_id TEXT PRIMARY KEY,
    stream TEXT NOT NULL,           -- 'lifecycle' | 'decisions' | 'spans'
    file_path TEXT NOT NULL,
    byte_offset INTEGER NOT NULL,
    ts_wall_utc REAL NOT NULL,
    operation TEXT,
    status TEXT
);
CREATE INDEX idx_trace ON trace_lookup(trace_id);
CREATE INDEX idx_time ON trace_lookup(ts_wall_utc);

-- causality_edges.sqlite
CREATE TABLE causality (
    event_id TEXT PRIMARY KEY,
    caused_by_event_id TEXT,
    parent_span_id TEXT,
    trace_id TEXT NOT NULL,
    operation TEXT,
    ts_wall_utc REAL NOT NULL
);
CREATE INDEX idx_caused_by ON causality(caused_by_event_id);
CREATE INDEX idx_parent ON causality(parent_span_id);
```

**Index maintenance:**
- Background rebuild every 60s from recent JSONL entries
- Index lag SLO: <= 60s
- Generation marker: `(index_epoch, last_offset)` for safe partial rebuild detection
- On stale/corrupt index: fall back to JSONL scan (always works)
- On startup: if index missing or stale, rebuild from current epoch's JSONL

**Causality DAG integrity checks (periodic validator):**
- No self-cycles
- Parent exists or explicitly marked external
- No orphan critical lifecycle events

### Retention Policy (Per-Stream, Configurable)

| Stream | Compress After | Delete After | Env Var |
|---|---|---|---|
| lifecycle | 60 days | 90 days | `JARVIS_TRACE_LIFECYCLE_RETENTION_DAYS` |
| decisions | 30 days | 60 days | `JARVIS_TRACE_DECISIONS_RETENTION_DAYS` |
| spans | 7 days | 30 days | `JARVIS_TRACE_SPANS_RETENTION_DAYS` |

---

## Section 3: Boundary Enforcement Layer

### Enforcement Modes

```python
class EnforcementMode(str, Enum):
    STRICT = "strict"       # Reject missing/invalid envelope
    CANARY = "canary"       # Accept but emit hard alert + increment violation counter
    PERMISSIVE = "permissive"  # Accept with warning log
```

### Boundary Classification

| Boundary | Classification | Target Mode |
|---|---|---|
| Startup phase transitions | Critical control-plane | STRICT |
| Recovery/rollback actions | Critical control-plane | STRICT |
| Endpoint promote/demote | Critical control-plane | STRICT |
| VM create/delete/recycle | Critical control-plane | STRICT |
| Health check poll/response | Standard operational | CANARY -> STRICT |
| IPC command dispatch | Standard operational | CANARY -> STRICT |
| Prime inference routing | Standard operational | CANARY -> STRICT |
| Telemetry emission | Observability | PERMISSIVE -> CANARY |
| Log flush | Observability | PERMISSIVE |

### Enforcement Primitives

1. **Decorator** for internal async boundaries
2. **HTTP middleware** (outbound: inject headers; inbound: extract + advance Lamport)
3. **File-RPC injection/extraction** (extends existing `inject_correlation`/`extract_correlation`)
4. **Subprocess env propagation** (`JARVIS_TRACE_ENVELOPE` env var, JSON, max 4KB)

### CI Compliance Gate

- `score_critical < 100%` -> build fails (non-negotiable)
- `score_overall < 80%` -> build fails (ratchet up over time)
- Schema drift counter > 0 for unknown fields -> warning (tracked per `producer_version`)

---

## Section 4: Adversarial Test Harness

### Architecture

```
backend/tests/adversarial/
├── conftest.py                    # Shared fixtures
├── fault_injector.py              # Boundary fault injection framework
├── replay_engine.py               # Deterministic replay from JSONL streams
├── invariant_checks.py            # All invariant assertions
├── test_startup_determinism.py
├── test_recovery_integrity.py
├── test_boundary_propagation.py
├── test_ordering_guarantees.py
└── fixtures/
    └── trace_envelope_v1.json     # Shared across all 3 repos
```

### Fault Types

- `network_partition` — target unreachable
- `partial_partition` — A sees B, B doesn't see A
- `timeout_after_success` — action completed but caller timed out
- `delayed_duplicate` — same event arrives twice, second delayed
- `clock_jump_forward` — wall clock jumps +60s
- `clock_jump_backward` — wall clock jumps -30s (NTP correction)
- `crash_mid_commit` — process dies between action and record
- `suspend_resume` — macOS sleep/wake, monotonic gap

### 10 CI-Gating Invariant Tests

1. **`test_no_orphan_lifecycle_phases`** — every `phase_enter` has matching `phase_exit` or `phase_fail`
2. **`test_startup_phase_ordering_is_dag_consistent`** — phases respect dependency DAG
3. **`test_deterministic_state_from_same_event_stream`** — replay twice, assert state equivalence
4. **`test_no_duplicate_side_effect_without_idempotency_key`** — side-effecting events carry idempotency_key
5. **`test_recovery_action_has_causal_chain`** — recovery events have non-null caused_by_event_id
6. **`test_timeout_after_success_does_not_duplicate`** — inject timeout-after-success, verify exactly-once
7. **`test_critical_boundaries_carry_valid_envelope`** — zero violations on critical paths
8. **`test_cross_repo_envelope_round_trip`** — serialize/deserialize across repos preserves all fields
9. **`test_lamport_monotonic_per_process`** — sequence strictly monotonic per (boot_id, process_id)
10. **`test_causality_dag_is_acyclic`** — no cycles in caused_by_event_id graph

### Exactly-Once Proof Hooks

For side-effecting operations, require tuple: `(idempotency_key, event_id, outcome_hash)`.
This provides forensic proof of duplicate suppression.

### Replay Determinism Mode

Deterministic replay runner consumes lifecycle+decision streams, asserts final state equivalence.
Sorts events by causal DAG (primary) then Lamport sequence (secondary) then ts_wall_utc (tertiary).

---

## Section 5: Implementation Order (Ranked Top 12)

| # | What | Touches | Enables | Effort |
|---|---|---|---|---|
| 1 | `TraceEnvelope` dataclass + `LamportClock` + `TraceEnvelopeFactory` | New: `backend/core/trace_envelope.py` | Everything else | S |
| 2 | Extend `correlation_context.py` to use `TraceEnvelope` as backing store | `backend/core/resilience/correlation_context.py` | Existing `@correlate` decorators propagate envelopes | S |
| 3 | JSONL append writer with `O_APPEND` + `fsync` + `fcntl` + line checksum | New: `backend/core/trace_store.py` | Persistent streams | S |
| 4 | Lifecycle event emitter — hooks into supervisor phase transitions | `unified_supervisor.py` phase entry/exit points | Stream 1 populated | M |
| 5 | Decision log JSONL flusher + envelope field | `backend/core/decision_log.py` | Stream 2 populated, decisions survive restart | S |
| 6 | Span recorder — wraps circuit breakers, health checks, inference | `backend/core/trace_store.py` + emit points | Stream 3 populated | M |
| 7 | Boundary enforcement middleware — HTTP, file-RPC, subprocess env | New: `backend/core/trace_enforcement.py` + integration | Envelopes cross repo boundaries | L |
| 8 | SQLite index builder + query interface | `backend/core/trace_store.py` extend | Fast post-mortem queries | M |
| 9 | Cross-repo contract fixture + deserialization tests | `fixtures/trace_envelope_v1.json` + test files per repo | Schema drift detected in CI | S |
| 10 | Fault injection framework | New: `backend/tests/adversarial/fault_injector.py` | Adversarial testing enabled | M |
| 11 | Replay engine + 10 invariant tests | New: `backend/tests/adversarial/replay_engine.py` + tests | CI-gated invariant proofs | L |
| 12 | Compliance score CI gate + disk guard + compaction | `backend/core/trace_store.py` extend + CI config | Full operational maturity | M |

**Effort key:** S = half day, M = 1-2 days, L = 2-3 days
**Total estimated:** ~12-16 days of focused work

---

## Files Modified (Summary)

### New Files
- `backend/core/trace_envelope.py` — TraceEnvelope, LamportClock, TraceEnvelopeFactory, BoundaryType
- `backend/core/trace_store.py` — JSONL writer, stream managers, DiskGuard, SQLite index, compaction
- `backend/core/trace_enforcement.py` — enforcement middleware, decorators, compliance scoring
- `backend/tests/adversarial/conftest.py` — shared fixtures
- `backend/tests/adversarial/fault_injector.py` — fault injection framework
- `backend/tests/adversarial/replay_engine.py` — deterministic replay
- `backend/tests/adversarial/invariant_checks.py` — invariant assertions
- `backend/tests/adversarial/test_startup_determinism.py`
- `backend/tests/adversarial/test_recovery_integrity.py`
- `backend/tests/adversarial/test_boundary_propagation.py`
- `backend/tests/adversarial/test_ordering_guarantees.py`
- `backend/tests/adversarial/fixtures/trace_envelope_v1.json`

### Modified Files
- `backend/core/resilience/correlation_context.py` — TraceEnvelope as backing store
- `backend/core/decision_log.py` — envelope field + JSONL flusher
- `unified_supervisor.py` — lifecycle event emission at phase transitions
- `backend/core/prime_client.py` — HTTP envelope injection
- `backend/core/trinity_ipc.py` — file-RPC envelope injection
- `backend/clients/reactor_core_client.py` — HTTP envelope injection

### Cross-Repo (JARVIS-Prime, Reactor-Core)
- HTTP inbound middleware for envelope extraction
- `fixtures/trace_envelope_v1.json` consumed in CI tests
- Lamport clock receive on inbound requests
