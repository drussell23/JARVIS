# Telemetry Contract v1 — Design Spec

> **Date**: 2026-03-20
> **Phase**: A (shared contract layer, prerequisite for Sub-projects 2 and 3)
> **Status**: Approved, ready for implementation

---

## Problem

The codebase has 5+ independent telemetry emission patterns:
- `ChainTelemetry` (reasoning chain): ad-hoc dicts with `event`, `trace_id`, `timestamp`
- `LifecycleTransition.to_telemetry_dict()`: structured but lifecycle-specific
- `CommProtocol` (Ouroboros): INTENT/HEARTBEAT/DECISION/POSTMORTEM message types
- `forward_experience()`: `experience_type` + `input_data` + `output_data`
- `StartupEventBus`: `phase_gate`, `budget_acquire`, `lease_acquired`

These use different field names, different ID conventions, no versioning, no ordering guarantees, and no compatibility policy. Building a dashboard on this will create "observability theater" — visual noise coupled to unstable internals.

## Solution: Unified Envelope + Versioned Payloads

### Dual Versioning

```
envelope_version: "1.0.0"          — transport/routing contract (rarely changes)
event_schema: "lifecycle.transition@1.0.0"  — domain payload contract (evolves per-type)
```

Envelope is stable. Payloads evolve independently per event type.

---

## Envelope Schema (v1.0.0)

Every event, regardless of domain, carries this mandatory envelope:

```python
@dataclass(frozen=True)
class TelemetryEnvelope:
    # Identity
    envelope_version: str          # "1.0.0" — major.minor.patch
    event_id: str                  # UUID — globally unique per emission
    event_schema: str              # "lifecycle.transition@1.0.0" — name@semver

    # Timing
    emitted_at: float              # time.time() wall clock (for human display)
    sequence: int                  # Monotonic counter per partition_key (for ordering)

    # Correlation
    trace_id: str                  # Spans full command lifecycle (voice input -> execution)
    span_id: str                   # This specific operation within the trace
    causal_parent_id: Optional[str]  # span_id of the event that caused this one
    op_id: Optional[str]           # Ouroboros operation ID (governance pipeline)

    # Deduplication
    idempotency_key: str           # Deterministic: f"{event_schema}:{trace_id}:{sequence}"
    partition_key: str             # Ordering scope: "lifecycle", "reasoning", "agent", etc.

    # Source
    source: str                    # "jprime_lifecycle_controller", "reasoning_chain", etc.
    severity: str                  # "info", "warning", "error", "critical"

    # Payload
    payload: Dict[str, Any]        # Domain-specific, schema-validated
```

### Compatibility Rules

| Envelope field change | Policy |
|---|---|
| New major envelope version | Consumers MUST reject (unknown transport) |
| New minor envelope version | Consumers MUST accept (backward-compatible addition) |
| Unknown envelope field | Consumers MUST ignore (forward-compatible) |

| Payload schema change | Policy |
|---|---|
| New major payload version | Consumers MUST reject or fall back to raw display |
| New minor payload version | Consumers MUST accept (new optional fields) |
| New patch payload version | Consumers MUST accept (documentation/description only) |

### Delivery Semantics

- **At-least-once** delivery — consumers must be idempotent
- **Deduplication** via `idempotency_key` with configurable window (default 5 min)
- **Per-partition ordering** via monotonic `sequence` per `partition_key`
- **No global ordering guarantee** — never trust timestamp ordering alone

### Clock and Ordering

- `emitted_at` (wall clock) for human display and approximate time queries
- `sequence` (monotonic counter) for strict ordering within a partition
- Dashboard MUST sort by `(partition_key, sequence)`, not by `emitted_at`

---

## Event Taxonomy (v1 frozen set)

### Lifecycle Domain (`partition_key: "lifecycle"`)

| event_schema | Emitted by | When |
|---|---|---|
| `lifecycle.transition@1.0.0` | JprimeLifecycleController | Any state change (UNKNOWN->PROBING, READY->DEGRADED, etc.) |
| `lifecycle.health@1.0.0` | JprimeLifecycleController | Each health probe result (periodic, not just transitions) |

### Reasoning Domain (`partition_key: "reasoning"`)

| event_schema | Emitted by | When |
|---|---|---|
| `reasoning.activation@1.0.0` | ReasoningChainOrchestrator | Chain phase changes (shadow/soft/full) or feature flag toggle |
| `reasoning.decision@1.0.0` | ReasoningChainOrchestrator | Each command classification (proactive/expand/passthrough) |

### Scheduler Domain (`partition_key: "scheduler"`)

| event_schema | Emitted by | When |
|---|---|---|
| `scheduler.graph_state@1.0.0` | Neural Mesh Coordinator | Agent graph topology changes (agent init/stop/crash) |
| `scheduler.unit_state@1.0.0` | Individual agents | Agent-level state changes (idle/busy/error) |

### Recovery Domain (`partition_key: "recovery"`)

| event_schema | Emitted by | When |
|---|---|---|
| `recovery.attempt@1.0.0` | JprimeLifecycleController, Neural Mesh | Restart/retry initiated |
| `fault.raised@1.0.0` | Any component | Typed failure with class + recovery policy |
| `fault.resolved@1.0.0` | Any component | Fault cleared (matches a prior fault.raised) |

---

## Payload Schemas (v1.0.0)

### lifecycle.transition@1.0.0

```python
{
    "from_state": str,              # LifecycleState value
    "to_state": str,
    "trigger": str,                 # What caused this transition
    "reason_code": str,             # Machine-readable reason
    "root_cause_id": Optional[str], # Groups related transitions
    "attempt": int,                 # Restart attempt number
    "backoff_ms": Optional[int],
    "restarts_in_window": int,
    "apars_progress": Optional[float],
    "vm_zone": Optional[str],
    "elapsed_in_prev_state_ms": float,
}
```

### lifecycle.health@1.0.0

```python
{
    "verdict": str,                 # HealthVerdict value
    "ready_for_inference": bool,
    "response_time_ms": float,
    "apars_progress": Optional[float],
    "error": Optional[str],
}
```

### reasoning.decision@1.0.0

```python
{
    "command": str,
    "is_proactive": bool,
    "confidence": float,
    "signals": List[str],
    "phase": str,                   # ChainPhase value
    "expanded_intents": List[str],  # Empty if not expanded
    "mind_requests": int,
    "delegations": int,
    "total_ms": float,
    "success_rate": float,
}
```

### fault.raised@1.0.0

```python
{
    "fault_class": str,             # "connection_refused", "budget_exhausted", "startup_stall"
    "component": str,               # "jprime_lifecycle", "reasoning_chain", "neural_mesh"
    "message": str,
    "recovery_policy": str,         # "auto_restart", "exponential_backoff", "manual", "none"
    "terminal": bool,               # Is this a terminal fault?
    "related_fault_id": Optional[str],  # Links to prior fault if escalation
}
```

### fault.resolved@1.0.0

```python
{
    "fault_id": str,                # event_id of the original fault.raised
    "resolution": str,              # "auto_recovered", "manual_reset", "timeout_expired"
    "duration_ms": float,           # Time from raised to resolved
}
```

---

## Backpressure and Safety

### Bounded Queue

```python
TELEMETRY_QUEUE_MAX = 1000          # Max queued events
TELEMETRY_DROP_POLICY = "drop_oldest"  # When full: drop oldest non-critical
TELEMETRY_CRITICAL_EVENTS = {"fault.raised", "lifecycle.transition"}  # Never dropped
```

Critical events (`fault.raised`, `lifecycle.transition`) are never dropped. Non-critical events (`lifecycle.health`, `scheduler.unit_state`) are dropped under pressure with a metric counter.

### Non-Blocking Emission

```python
# Producers MUST use fire-and-forget:
asyncio.create_task(telemetry_bus.emit(envelope))
# NEVER: await telemetry_bus.emit(envelope)  # blocks runtime path
```

Telemetry emission MUST NOT block the execution path. The bus uses a bounded asyncio.Queue; `emit()` is non-blocking (`put_nowait` with drop policy).

### Dead Letter

Events that fail consumer processing go to a dead-letter deque (maxlen=100) with:
- Original envelope
- Error message
- Failure timestamp
- Retry count

### PII Policy

- `command` field in reasoning events: truncated to 100 chars, no PII extraction
- `speaker` field: never included in telemetry (available in audit logs only)
- No raw audio data in any event

---

## Cross-Repo Trace Continuity

```
JARVIS (trace_id=T1, span_id=S1)
  -> MindClient.send_command() propagates trace_id=T1 in context
    -> J-Prime receives trace_id=T1, creates span_id=S2, causal_parent_id=S1
      -> J-Prime emits events with trace_id=T1, span_id=S2
  -> Experience forwarded to Reactor with trace_id=T1
    -> Reactor creates span_id=S3, causal_parent_id=S1
```

`trace_id` is immutable across the entire command lifecycle. Each component creates its own `span_id` and links to the parent via `causal_parent_id`.

---

## Implementation: TelemetryBus

### New file: `backend/core/telemetry_contract.py`

```
TelemetryEnvelope  — frozen dataclass
TelemetryBus       — singleton, bounded queue, emit/subscribe/dead-letter
EventRegistry      — registered schemas with version validation
emit()             — non-blocking fire-and-forget
subscribe(schema_pattern, callback)  — consumers register by event type
```

### Migration Path

Existing producers (ChainTelemetry, LifecycleTransition, CommProtocol) wrap their current dicts in a TelemetryEnvelope. No behavioral change — just structured wrapping.

New producers (Sub-project 2: agent boot, reasoning activation) emit envelopes natively.

Sub-project 3 (dashboard) consumes envelopes exclusively — never reads internal state directly.

---

## Acceptance Criteria (Phase A Go/No-Go)

1. All existing producers (ChainTelemetry, LifecycleController) emit valid envelopes
2. EventRegistry validates `event_schema` on emission (unknown schema = warning, not crash)
3. Consumer replay test passes with duplicate/out-of-order injection
4. `idempotency_key` dedup window works (5 min default)
5. Cross-repo `trace_id` propagation verified (JARVIS -> J-Prime context)
6. Bounded queue drops non-critical events under pressure without blocking producers
7. Dead-letter channel captures failed events with reason codes
8. Load test: 1000 events/sec sustained without degrading command processing latency

---

## Files Changed

| File | Change |
|---|---|
| `backend/core/telemetry_contract.py` | **NEW** — TelemetryEnvelope, TelemetryBus, EventRegistry |
| `backend/core/reasoning_chain_orchestrator.py` | **MODIFY** — ChainTelemetry wraps events in envelopes |
| `backend/core/jprime_lifecycle_controller.py` | **MODIFY** — _emit_telemetry wraps transitions in envelopes |
| `tests/core/test_telemetry_contract.py` | **NEW** — envelope validation, dedup, ordering, backpressure |

## Out of Scope

- Dashboard UI (Sub-project 3)
- Agent boot wiring (Sub-project 2, Phase B)
- Schema registry CI enforcement (future)
- Protobuf/Avro serialization (JSON-only for v1)
