# Disease 3 Cure: Unified Message Fabric (UMF) Design

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement the implementation plan derived from this design.

**Goal:** Eliminate dual communication split-brain by replacing parallel cross-repo messaging implementations (Trinity Event Bus + Reactor Bridge) with one canonical logical communication system (UMF) governed by the Unified Supervisor.

**Architecture:** Single logical MessageFabric with pluggable transport adapters (file, Redis, WS). All three repos use the same canonical envelope, dedup ledger, and heartbeat model. The Unified Supervisor is the sole authority for lifecycle health truth. Disease 2 types (`SubsystemState`, `ContractGate`, `ProcessIdentity`) are reused directly.

**Tech Stack:** Python 3.9+, asyncio structured concurrency, HMAC-SHA256 signing, SQLite WAL (dedup ledger), existing transport channels.

---

## 1. Problem Statement

Two overlapping cross-repo communication mechanisms exist:

- **Trinity Event Bus** (`jarvis-prime/jarvis_prime/core/trinity_event_bus.py`): pub/sub event routing with content-hash dedup
- **Reactor Bridge** (`backend/system/reactor_bridge.py`): command/heartbeat channel with independent retry loops

These create split-brain state: independent dedup, independent heartbeats, independent retry policies, and transport fanout divergence.

## 2. Non-Negotiable Architecture Rule

- One **logical** communication system (`UnifiedMessageFabric`)
- Many **physical** transports (file, Redis, WS) behind it
- No repo may bypass UMF with its own routing/dedup/heartbeat policy

## 3. Canonical Envelope Schema

```json
{
  "schema_version": "umf.v1",
  "message_id": "uuid-v7",
  "idempotency_key": "string",
  "stream": "lifecycle|command|event|heartbeat|telemetry",
  "kind": "command|event|heartbeat|ack|nack",
  "source": {
    "repo": "jarvis|jarvis-prime|reactor-core",
    "component": "string",
    "instance_id": "string",
    "session_id": "string"
  },
  "target": {
    "repo": "jarvis|jarvis-prime|reactor-core|broadcast",
    "component": "string|*"
  },
  "routing": {
    "partition_key": "string",
    "priority": "critical|high|normal|low",
    "ttl_ms": 30000,
    "deadline_unix_ms": 0
  },
  "causality": {
    "trace_id": "string",
    "span_id": "string",
    "parent_message_id": "string|null",
    "sequence": 0
  },
  "contract": {
    "capability_hash": "sha256",
    "schema_hash": "sha256",
    "compat_window": "N|N-1"
  },
  "payload": {},
  "observed_at_unix_ms": 0,
  "signature": {
    "alg": "HMAC-SHA256",
    "key_id": "string",
    "value": "hex"
  }
}
```

## 4. Compatibility Gate

Accept only if:
- `schema_version` in {N, N-1}
- `capability_hash` matches negotiated boot contract (or approved compatibility map)
- Signature valid
- TTL not expired

Else route to `REJECTED` with reason code (no silent drop).

## 5. Heartbeat Model (Single Source)

Heartbeats are `kind=heartbeat`, `stream=lifecycle`. Required payload fields:
- `liveness` (bool)
- `readiness` (bool)
- `subsystem_role`
- `state` (`SubsystemState.value` — reused from Disease 2)
- `last_error_code`
- `queue_depth`
- `resource_pressure`

Authority: Supervisor consumes, validates, and derives global truth. Subsystems report; they do not independently adjudicate global health.

## 6. Dedup Ledger API

```
reserve(idempotency_key, message_id, ttl_ms) -> RESERVED|DUPLICATE|CONFLICT
commit(message_id, effect_hash)
abort(message_id, reason)
get(message_id)
```

Rules:
- Dedup key priority: `idempotency_key`, fallback `message_id`
- Never dedup by payload hash (too collision-prone semantically)
- Ledger must be durable + bounded (WAL + TTL compaction)

## 7. Delivery Semantics

- Control-plane/lifecycle/command streams: **effectively-once**
- Telemetry: **at-least-once**
- Ordering: per `partition_key` strict order; cross-partition no global order guarantee

## 8. Retry/Failure Policy (Single Policy)

- Retry budget per stream + per target
- Backoff: exponential + jitter + max cap
- Circuit states: `CLOSED|OPEN|HALF_OPEN`
- Dead-letter queue centralized in UMF, not per-repo ad hoc
- No component-local retry storms

## 9. Protocol Interfaces

```python
class MessageFabric(Protocol):
    async def publish(self, msg: UmfMessage) -> PublishResult: ...
    async def subscribe(self, stream: str, handler: Handler) -> SubscriptionId: ...
    async def ack(self, message_id: str, result: AckResult) -> None: ...
    async def health(self) -> FabricHealth: ...

class TransportAdapter(Protocol):
    async def send(self, framed: bytes, route: Route) -> SendResult: ...
    async def receive(self) -> AsyncIterator[bytes]: ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...

class DedupLedger(Protocol):
    async def reserve(self, key: str, message_id: str, ttl_ms: int) -> ReserveResult: ...
    async def commit(self, message_id: str, effect_hash: str) -> None: ...
    async def abort(self, message_id: str, reason: str) -> None: ...
```

## 10. Repo Ownership After Unification

| Repo | Role |
|------|------|
| JARVIS-AI-Agent | Control plane authority + UMF runtime integration |
| jarvis-prime | UMF client consumer only (removes independent event bus policy) |
| reactor-core | UMF client producer/consumer only (removes independent bridge policy) |

No repo owns a separate communication policy anymore.

## 11. Disease 2 Type Reuse

- `SubsystemState.value` is canonical heartbeat lifecycle state
- `ContractGate.is_schema_compatible()` enforces N/N-1 compatibility
- `RootAuthorityWatcher` remains lifecycle policy authority
- `is_root_managed()` remains anti-self-promotion guard
- `ProcessIdentity` semantics retained for safety-sensitive operations

## 12. Implementation Waves

### Wave 0: Freeze + Baseline
- Feature freeze on cross-repo comm internals
- Capture baseline metrics on current bus/bridge divergence
- Define ownership matrix for state domains

### Wave 1: Canonical Contract + Shared SDK
- Publish UMF schema module + strict validators
- Implement shared UMF client package for all three repos
- Wire ContractGate compatibility checks into UMF handshake

### Wave 2: Shadow Mode Parity
- Run UMF in shadow against legacy paths
- Parity diff telemetry with deterministic reason taxonomy
- Block promotion unless parity threshold is met

### Wave 3: Heartbeat/Lifecycle Cutover
- Move lifecycle heartbeats to UMF-only path
- Supervisor derives single global health truth from UMF stream
- Disable legacy heartbeat publications

### Wave 4: Command/Event Authority Cutover
- UMF becomes authoritative for command/event routing
- Legacy paths set to read-only mirror mode temporarily
- Enable strict dedup ledger reserve/commit/abort semantics

### Wave 5: Legacy Path Removal
- Delete independent retry/dedup/routing/heartbeat logic in old systems
- Retain only transport adapters as physical channels under UMF
- Enforce no-bypass guardrails

### Wave 6: Hardening + Operationalization
- Run chaos and long-haul stress suites
- Finalize SLOs, alerts, and runbooks
- Mark migration complete after Go/No-Go pass

## 13. Reason Taxonomy (Deterministic)

All rejects use fixed enums:
- `schema_mismatch`
- `sig_invalid`
- `capability_mismatch`
- `ttl_expired`
- `deadline_expired`
- `dedup_duplicate`
- `route_unavailable`
- `backpressure_drop`
- `circuit_open`
- `handler_timeout`

## 14. Advanced Gaps to Guard

1. Split TTL semantics across old/new paths during migration
2. Duplicate side effects when dual-write + retries overlap
3. Stale capability hash cache causing false reject loops
4. Clock skew breaking deadline/expiry checks
5. Per-transport reorder windows under reconnect
6. Poison messages bouncing between DLQs
7. Partition-key hot spotting starving low-priority partitions
8. Half-open circuit flapping during intermittent network
9. Re-entrant restart requests from mirrored health transitions
10. Supervisor overload collapse if UMF central queue is unbounded
11. Schema downgrade traps (N-1 accepted but payload fields required by policy)
12. Trace continuity loss when adapters omit parent_message_id

## 15. Additional Design Invariants

- **Correlation continuity:** `trace_id` and `parent_message_id` must survive every adapter hop
- **Deadline semantics:** enforce both `ttl_ms` and absolute deadline to avoid zombie messages after clock drift
- **Replay watermarking:** persist per-partition replay watermark to prevent old-file reconsumption
- **Source-of-truth table:** explicit ownership matrix for lifecycle state, dedup state, contract state, and replay state
- **No hidden fallback rule:** if transport fallback occurs, it must be visible and policy-approved, not implicit

## 16. Modular Internal Decomposition

To prevent control-plane obesity (another monolith), UMF internals are modular:
- `backend/core/umf/contract_gate.py`
- `backend/core/umf/dedup_ledger.py`
- `backend/core/umf/delivery_engine.py`
- `backend/core/umf/transport_adapters/`
- `backend/core/umf/heartbeat_projection.py`

## 17. Test Blueprint

### Contract & Schema Gates
- Valid `umf.v1` envelope passes across all 3 repos
- Unknown schema version rejected with explicit reason code
- N and N-1 compatibility accepted, N-2 rejected
- Capability hash mismatch fails before handler execution
- HMAC signature invalid fails closed
- Pass criteria: 100% deterministic, zero silent drops

### Dedup Ledger Correctness
- Duplicate publish with same idempotency_key yields one effect
- Crash after reserve before commit recovers without double-effect
- Abort path allows safe replay
- Ledger TTL expiration deterministic and documented
- Concurrent reserve races produce one winner
- Pass criteria: exactly-once for command/control under fault injection

### Ordering & Delivery
- Per-partition strict ordering preserved under reconnect
- Cross-partition out-of-order tolerated and non-corrupting
- Transport failover does not violate per-partition order
- Replay after restart preserves monotonic sequence
- Pass criteria: no sequence inversion in same partition at p99 under stress

### Heartbeat Truth Unification
- Only UMF lifecycle stream feeds Supervisor truth projection
- Payload state maps directly to SubsystemState.value
- Contradictory subsystem reports resolve deterministically
- Stale heartbeat transitions readiness per policy
- Pass criteria: one authoritative health state, zero split-brain

### Retry/Backpressure/Circuit
- Retry budget caps prevent storms
- Circuit opens under repeated failure; half-open recovers cleanly
- Backpressure throttles publishers before queue collapse
- Poison message routes to DLQ once, no oscillation
- Pass criteria: bounded depth and retries under prolonged failure

### Shadow Parity
- Legacy path vs UMF shadow parity on same input stream
- Decision parity for routing, dedup, heartbeat, health transitions
- Parity diff emits actionable diagnostics
- Pass criteria: >= 99.99% parity over soak window

### Cross-Repo Integration
- JARVIS -> Prime command path via UMF only
- Prime -> Reactor model/training events via UMF only
- Reactor -> Prime model-ready lifecycle with contract checks
- Supervisor boot contract negotiation across all repos
- Pass criteria: zero direct legacy bridge usage in active mode

### Upgrade Compatibility
- Rolling upgrade where one repo is N-1, others N
- Feature-flagged field introduction with backward compatibility
- Downgrade rollback preserves envelope validity
- Pass criteria: no downtime, no schema split-brain

### Supervisor Failure & Recovery
- Supervisor restart while messages in-flight; replay safe and bounded
- Bootstrap rehydrates ledger and sequence cursors correctly
- Subordinate repos do not self-promote during outage
- Pass criteria: authority singular, recovery deterministic

### Chaos & Long-Run Stability
- Random transport partitions, process kills, disk latency spikes
- 24h soak with sustained mixed workload
- Memory growth bounded (no orphan-task or queue leak)
- Deadlock/livelock watchdog on async tasks
- Pass criteria: no unbounded growth, stable throughput/latency

## 18. Go/No-Go Gate

All must be true:
- Contract tests 100% pass
- Dedup exactly-once tests pass under failure injection
- Shadow parity threshold met
- Heartbeat split-brain incidents == 0
- Chaos + soak pass without instability
- Legacy dual-path disabled via enforceable guard

## 19. Open Questions (Resolve in Plan)

1. Which durable store backs dedup ledger initially (SQLite WAL vs Redis vs hybrid)?
2. What partition_key strategy avoids hot-spotting for high-volume streams?
3. How are signing keys rotated without breaking N/N-1 compatibility?
4. What replay retention window is acceptable per stream class?
5. What is the exact parity soak duration threshold for promotion?
