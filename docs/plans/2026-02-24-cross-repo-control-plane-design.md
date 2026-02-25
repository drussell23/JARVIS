# Cross-Repo Control Plane Design

**Date**: 2026-02-24
**Status**: Approved
**Phase**: 1 (Foundation)
**Approach**: C — SQLite Journal + UDS Event Fabric + Optional Redis Mirror

## Problem Statement

The JARVIS ecosystem (JARVIS, JARVIS Prime, Reactor Core) operates as a system-of-systems with five root diseases in cross-repo coordination:

1. **Two disconnected startup systems** — internal `StartupStateMachine` and external `CrossRepoStartupOrchestrator` share no state, no health semantics, no failure propagation.
2. **Environment-variable state model** — cross-process truth is fragmented (`os.environ` is process-local, non-atomic, non-durable, non-observable).
3. **No real-time cross-repo events** — coordination relies on polling and stale assumptions.
4. **No graceful shutdown ordering** — causes dirty exits, orphaned work, bad restart baselines.
5. **Advisory-only contracts** — drift becomes visible only after something already failed.

### Advanced Gaps (identified during design review)

- No single-writer control-plane lease (concurrent orchestrators possible)
- No cross-repo causal identity (no shared transaction_id + epoch + sequence)
- No idempotent lifecycle commands (not replay-safe across crashes)
- No compatibility window policy (no enforced N/N-1 at boot + runtime)
- No hard backpressure contract across repos
- No crash-consistent orchestration journal (recovery from heuristics, not intent log)
- No failure-domain isolation (one repo's degraded mode poisons global readiness)
- No deterministic voice authority boundary (static/crackle is symptom of control-plane split)

## Design Principles

- **Correctness without external dependency**: SQLite + UDS on critical path; Redis optional.
- **Deterministic authority**: single-writer lease with epoch fencing on every mutation.
- **Journal as truth**: append-only log; current state is a projection, never independently mutated.
- **Idempotent operations**: all lifecycle commands replay-safe via semantic keys.
- **Bidirectional handshake**: readiness only after mutual ACK + compatibility decision.
- **Lease-backed heartbeats**: missing heartbeat past TTL = deterministic state transition.
- **Reverse-DAG shutdown**: explicit DRAINING → STOPPING → STOPPED with bounded drain.

---

## Section 1: SQLite Orchestration Journal (Truth)

### Database Location

```
~/.jarvis/control/orchestration.db
```

### SQLite Configuration

```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;  -- Configurable: JARVIS_SQLITE_SYNC_MODE=FULL for production
PRAGMA wal_autocheckpoint = 1000;
PRAGMA busy_timeout = 5000;
PRAGMA foreign_keys = ON;
```

`synchronous = NORMAL` in WAL mode guarantees durability on every transaction commit. `FULL` adds an extra fsync on checkpoint — configurable via `JARVIS_SQLITE_SYNC_MODE` env var.

### Schema

**Table 1: `journal` (append-only, source of truth)**

```sql
CREATE TABLE journal (
    seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    epoch           INTEGER NOT NULL,
    timestamp       REAL NOT NULL,          -- time.time() (wall clock, persisted)
    wall_clock      TEXT NOT NULL,           -- ISO-8601 for human readability
    actor           TEXT NOT NULL,           -- 'supervisor', 'prime', 'reactor'
    action          TEXT NOT NULL,           -- 'start', 'stop', 'handshake', 'heartbeat', 'recover', 'drain'
    target          TEXT NOT NULL,           -- Component name
    idempotency_key TEXT,                    -- Replay-safe dedup key
    payload         TEXT,                    -- JSON: action-specific data
    result          TEXT,                    -- 'pending', 'committed', 'failed', 'superseded'
    fence_token     INTEGER NOT NULL         -- Must match current epoch to be valid
);

CREATE INDEX idx_journal_epoch ON journal(epoch);
CREATE INDEX idx_journal_target ON journal(target, seq);
CREATE INDEX idx_journal_idemp ON journal(idempotency_key) WHERE idempotency_key IS NOT NULL;
```

**Table 2: `component_state` (derived projection, rebuilt from journal on recovery)**

```sql
CREATE TABLE component_state (
    component       TEXT PRIMARY KEY,
    status          TEXT NOT NULL,
    epoch           INTEGER NOT NULL,
    last_seq        INTEGER NOT NULL,
    pid             INTEGER,
    endpoint        TEXT,
    api_version     TEXT,
    capabilities    TEXT,                    -- JSON
    last_heartbeat  REAL,                    -- time.time()
    heartbeat_ttl   REAL NOT NULL DEFAULT 30.0,
    drain_deadline  REAL,
    instance_id     TEXT,
    FOREIGN KEY (last_seq) REFERENCES journal(seq)
);
```

**Table 3: `lease` (single-writer authority)**

```sql
CREATE TABLE lease (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    holder          TEXT NOT NULL,
    epoch           INTEGER NOT NULL,
    acquired_at     REAL NOT NULL,           -- time.time()
    ttl             REAL NOT NULL DEFAULT 15.0,
    last_renewed    REAL NOT NULL            -- time.time()
);
```

**Table 4: `contracts` (registered interface contracts)**

```sql
CREATE TABLE contracts (
    component       TEXT NOT NULL,
    contract_type   TEXT NOT NULL,
    contract_key    TEXT NOT NULL,
    schema_hash     TEXT NOT NULL,
    min_version     TEXT,
    max_version     TEXT,
    registered_at   REAL NOT NULL,
    epoch           INTEGER NOT NULL,
    PRIMARY KEY (component, contract_type, contract_key)
);
```

**Table 5: `schema_version` (migration tracking)**

```sql
CREATE TABLE schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL,
    description TEXT
);
```

### Timestamp Discipline

- **Persisted columns** (`timestamp`, `acquired_at`, `last_renewed`, `last_heartbeat`, `registered_at`): `time.time()` (wall clock). Survives restart.
- **In-process TTL checks**: `time.monotonic()` in `LeaseSession` dataclass (never persisted). Immune to NTP jumps within a session.
- **Wall clock regression safety**: If `time.time()` < persisted `last_renewed`, treat lease as NOT expired. Wait full TTL from current time before claiming.

### Idempotency Semantics

Keys are epoch-independent (stable across leader failover):

```
# Reactive (triggered by journal event): deterministic
idempotency_key = f"{action}:{target}:triggered_by:{trigger_seq}"

# Proactive (supervisor-initiated): UUID-based
idempotency_key = f"{action}:{target}:{uuid4().hex}"
```

Before writing:
1. `SELECT seq FROM journal WHERE idempotency_key = ? AND result != 'failed'`
2. If exists: return existing seq (replay-safe, no side effects)
3. If not: write `result = 'pending'`, execute, update to `committed` or `failed`

### Journal Compaction

- Retain all entries from current epoch
- Retain last 1000 entries from prior epochs
- Compact on startup and every 24 hours
- Compacted entries archived to `orchestration_archive.db`

### Recovery Protocol

On supervisor startup:
1. Open DB (create if missing), check `schema_version`, migrate if needed
2. Read `lease` — if exists and not expired, wait TTL + 1s, then attempt CAS acquisition
3. Acquire lease with epoch = `old_epoch + 1`
4. Replay journal from `max(seq) - 1000` to rebuild `component_state`
5. For each component with `status != 'STOPPED'`: probe health to determine actual state
6. Reconcile projected state with probed reality, write corrective journal entries

---

## Section 2: Lease Algorithm & Epoch Fencing (Authority)

### Constants

```python
LEASE_TTL_S = 15.0              # Time before lease expires
LEASE_RENEW_INTERVAL_S = 5.0    # 3x safety margin (renew 3 times within TTL)
LEASE_ACQUIRE_TIMEOUT_S = 20.0  # Wait for stale lease to expire
LEASE_ACQUIRE_RETRY_MS = 250    # Poll interval during acquisition
```

### Acquisition: CAS Semantics

Two contenders can both observe expired lease. Unconditional `UPDATE` would race. CAS prevents this:

**First boot (no lease exists):**

```python
conn.execute(
    "INSERT OR IGNORE INTO lease (id, holder, epoch, acquired_at, ttl, last_renewed) "
    "VALUES (1, ?, ?, ?, ?, ?)",
    (holder_id, new_epoch, now, LEASE_TTL_S, now)
)
if conn.execute("SELECT changes()").fetchone()[0] == 1:
    # Won insert race
else:
    # Someone else inserted first — loop back and read
```

**Expired lease:**

```python
result = conn.execute(
    "UPDATE lease SET holder=?, epoch=?, acquired_at=?, last_renewed=?, ttl=? "
    "WHERE id=1 AND epoch=? AND holder=? AND last_renewed=?",
    (holder_id, new_epoch, now, now, LEASE_TTL_S,
     old_epoch, old_holder, old_last_renewed)
)
if result.rowcount == 1:
    # CAS succeeded — won the race
else:
    # CAS failed — another process claimed between our read and write
```

### Renewal

Background task with jitter (+-20%) to avoid synchronized spikes.

CAS on renewal ensures fencing detection:

```python
result = conn.execute(
    "UPDATE lease SET last_renewed = ? WHERE id = 1 AND holder = ? AND epoch = ?",
    (time.time(), holder_id, epoch)
)
if result.rowcount != 1:
    # Fenced — another leader took over
    on_lease_lost("fenced_by_new_epoch")
```

After 3 consecutive renewal failures: voluntary abdication. Better to surrender authority than hold it while unable to journal.

### Fence Token Enforcement

Every write to the journal validates epoch:

```python
row = conn.execute("SELECT epoch, holder FROM lease WHERE id = 1").fetchone()
if row is None or row[0] != self._epoch or row[1] != self._holder_id:
    raise StaleEpochError(...)
```

`StaleEpochError` is NOT retryable. The correct response is to stop issuing commands, enter read-only mode, and terminate. The new leader takes over.

### External Side Effect Fencing

Process spawn, process kill, and RPCs are fenced before AND after execution:

1. Journal intent (`result = 'pending'`) — validates epoch
2. Execute side effect
3. Validate epoch AGAIN — if stale, side effect is orphaned
4. Journal result (`committed` or `failed`)

Orphaned side effects (executed but epoch lost before commit) are reconciled by the new leader's recovery protocol (probe actual state, adopt or kill).

### Holder ID Format

```
supervisor:{pid}:{uuid4().hex[:12]}
```

PID alone recycles. UUID suffix guarantees uniqueness across incarnations.

### Edge Cases

| Edge Case | Behavior |
|-----------|----------|
| Two supervisors start simultaneously | SQLite WAL write lock serializes. First writer wins CAS. |
| Supervisor crashes without releasing | Lease expires after 15s. New supervisor claims at epoch+1. |
| GC pause > 5s but < 15s | Misses 1-2 renewals, lease survives. No leadership change. |
| GC pause > 15s | Lease expires. Resumed supervisor's renewal fails (epoch mismatch) → abdication. |
| Filesystem I/O stall | 3 consecutive renewal failures → voluntary abdication. |
| PID recycled after crash | UUID suffix prevents old PID from matching. |
| Clock jump (wall clock) | Lease expiry uses wall clock with regression safety. In-process uses monotonic. |

---

## Section 3: Component State Machine & Unified Lifecycle DAG

### State Machine

Every component — in-process, subprocess, remote — follows this state machine:

```
REGISTERED → STARTING → HANDSHAKING → READY ⇄ DEGRADED → DRAINING → STOPPING → STOPPED
                ↓            ↓                      ↓          ↓
              FAILED       FAILED                 FAILED     FAILED

From ANY state except STOPPED:
  heartbeat_ttl_expired() → LOST
  crash_detected()        → FAILED
```

**Valid transitions:**

| From | To |
|------|-----|
| REGISTERED | STARTING |
| STARTING | HANDSHAKING, FAILED |
| HANDSHAKING | READY, FAILED |
| READY | DEGRADED, DRAINING, FAILED, LOST |
| DEGRADED | READY, DRAINING, FAILED, LOST |
| DRAINING | STOPPING, FAILED, LOST |
| STOPPING | STOPPED, FAILED |
| FAILED | STARTING (recovery retry) |
| LOST | STARTING, STOPPED (reconciliation) |

Every transition is journaled before execution.

### Component Locality

```python
class ComponentLocality(Enum):
    IN_PROCESS = "in_process"     # Python module in supervisor process
    SUBPROCESS = "subprocess"     # Spawned child process
    REMOTE     = "remote"         # Separate host (GCP VM)
```

### Component Declarations

```python
@dataclass(frozen=True)
class ComponentDeclaration:
    name: str
    locality: ComponentLocality
    dependencies: Tuple[str, ...] = ()          # Hard: must be READY before start
    soft_dependencies: Tuple[str, ...] = ()     # Soft: degrades if unavailable
    is_critical: bool = False                    # Failure prevents FULLY_READY
    start_timeout_s: float = 60.0
    handshake_timeout_s: float = 10.0
    drain_timeout_s: float = 30.0
    heartbeat_ttl_s: float = 30.0
    spawn_command: Optional[Tuple[str, ...]] = None
    endpoint: Optional[str] = None
    health_path: str = "/health"
    init_fn: Optional[str] = None
```

### Unified DAG

Single declaration list covering all repos:

- **Wave 0**: Infrastructure (event_bus, cloud_sql) — no dependencies
- **Wave 1**: Core (backend_api) — depends on event_bus
- **Wave 2**: Cross-repo (jarvis_prime, reactor_core) + in-process intelligence (ecapa_backend, model_serving) — depends on backend_api
- **Wave 3**: Remote (gcp_vm) + enterprise (semantic_cache, audio_bus) — depends on backend_api
- **Wave 4**: Features (voice_unlock, frontend) — depends on Wave 2/3 components

Soft dependencies do NOT affect wave ordering. A component with unmet soft dependencies enters DEGRADED, not blocked.

### Wave Computation

Kahn's algorithm topological sort on hard dependencies. Same algorithm as existing `StartupStateMachine`, extended to cross-repo components.

### Failure Propagation Rules

| Dependency Type | Dependency Failed | Dependency Lost |
|----------------|-------------------|-----------------|
| Hard (in `dependencies`) | Dependent SKIPped if not started; DRAINed if running | Dependent DRAINed |
| Soft (in `soft_dependencies`) | Dependent DEGRADEd | Dependent DEGRADEd |

### Shutdown: Reverse DAG with Drain Contracts

1. Compute reverse topological waves (leaves first, roots last)
2. Per wave (parallel within wave):
   a. Transition to DRAINING — send drain signal
   b. Wait for `drain_timeout_s`
   c. Transition to STOPPING — SIGTERM / cancel
   d. Wait for confirmation or force-kill
   e. Transition to STOPPED
3. Journal shutdown completion

---

## Section 4: Cross-Repo Handshake Protocol & Heartbeat Contract

### Handshake Protocol

Two-phase exchange after health gate passes:

**Phase A**: Supervisor polls health endpoint until HTTP 200 (bounded by `start_timeout_s`).

**Phase B**: Supervisor sends `HandshakeProposal`, component returns `HandshakeResponse`.

```python
@dataclass(frozen=True)
class HandshakeProposal:
    supervisor_epoch: int
    supervisor_instance_id: str
    expected_api_version_min: str       # Semver minimum
    expected_api_version_max: str       # Semver maximum
    required_capabilities: Tuple[str, ...]
    health_schema_hash: str             # SHA-256 of expected health schema
    heartbeat_interval_s: float
    heartbeat_ttl_s: float
    protocol_version: str               # "1.0.0"

@dataclass(frozen=True)
class HandshakeResponse:
    accepted: bool
    component_instance_id: str
    api_version: str
    capabilities: Tuple[str, ...]
    health_schema_hash: str
    rejection_reason: Optional[str]
    metadata: Optional[Dict[str, Any]]
```

### Compatibility Decision

1. `accepted` must be True
2. `api_version` must be within `[min, max]` range
3. Major version must match (breaking change boundary)
4. All `required_capabilities` must be present
5. Health schema hash mismatch: warning, not blocking (gradual migration)

### Legacy Fallback

If component returns 404 for `/lifecycle/handshake`: synthesize legacy response with `api_version="0.0.0"` and empty capabilities. Logged as degraded contract confidence. System works but with no contract guarantees.

### Heartbeat Contract

**Pull-based**: supervisor polls each component's health endpoint at `heartbeat_ttl / 3` interval (3x safety margin with jitter).

**Heartbeat validates:**

| Check | On Failure |
|-------|-----------|
| HTTP response received | Increment miss counter |
| 3 consecutive misses (= 1 TTL) | Transition → LOST |
| `instance_id` changed | Transition → LOST (process resurrected outside lifecycle) |
| `api_version` changed | Transition → DEGRADED (contract drift) |
| `status: degraded` reported | Transition → DEGRADED |
| `status: healthy` after DEGRADED | Transition → READY |

### Required Endpoints (External Repos)

```
POST /lifecycle/handshake    — respond to handshake proposal
POST /lifecycle/drain        — begin graceful drain
GET  /health                 — existing health endpoint (unchanged)
```

Implementation: ~50 lines per repo using `HandshakeResponder` from `control_plane_client.py`.

---

## Section 5: UDS Event Fabric (Distribution)

### Architecture

Supervisor runs UDS server at `~/.jarvis/control.sock`. Subscribers (JARVIS Prime, Reactor Core, optional Redis mirror) connect and receive sequenced events with replay capability.

**Critical ordering**: journal write first, UDS emission second. If UDS fails, no data lost — subscribers replay from journal.

### Wire Protocol

Length-prefixed JSON (4-byte big-endian length header + UTF-8 JSON payload). Simple, debuggable, sufficient for local IPC at tens-per-second event rates.

### Message Types

- `subscribe` — subscriber → server (with `last_seen_seq` for replay)
- `subscribe_ack` — server → subscriber (confirms replay range)
- `event` — server → subscriber (journal entry notification)
- `ping` / `pong` — keepalive (10s interval, 30s timeout)

### Backpressure

Bounded queue per subscriber (500 events). On full:
- Drop oldest event
- Subscriber detects sequence gap
- Subscriber requests replay from journal via reconnect

### Replay Semantics

On subscriber connect/reconnect:
1. Subscriber sends `last_seen_seq`
2. Server queries journal for entries after that seq (capped at 1000)
3. Server sends replayed events before live stream begins

### Socket Lifecycle

- Created on lease acquisition (epoch-safe stale socket cleanup)
- `chmod 0o600` — only owning user can connect
- Cleaned up on graceful shutdown or lease loss
- Stale socket from crashed supervisor: safe to unlink because we hold the lease

### Redis Mirror (Optional)

Fire-and-forget publish to `jarvis:control_plane:events` channel. Never blocks critical path. `_available = False` on any Redis error — zero impact on correctness.

### Client Library

`ControlPlaneSubscriber` (~60 lines): connect, subscribe with filter, async iterator over events, auto-reconnect with replay. Usable by external repos without importing the full JARVIS codebase.

---

## Section 6: Integration Plan

### New Module Layout

```
backend/core/
├── orchestration_journal.py    # Sections 1+2: SQLite, journal, lease, fencing
├── lifecycle_engine.py         # Section 3: State machine, DAG, waves, shutdown
├── handshake_protocol.py       # Section 4: Handshake, compatibility, heartbeat
├── uds_event_fabric.py         # Section 5: UDS server, subscribers, replay
└── control_plane_client.py     # Client: subscriber + handshake responder
```

### Module Dependency Graph

```
orchestration_journal.py          (stdlib only — no external deps)
        │
        ▼
lifecycle_engine.py               (depends on: orchestration_journal)
        │
        ├──▶ handshake_protocol.py    (depends on: orchestration_journal)
        │
        └──▶ uds_event_fabric.py      (depends on: orchestration_journal)

control_plane_client.py           (stdlib + asyncio only — standalone)
```

No circular dependencies.

### Locality Driver Pattern

Existing code knows HOW to spawn JARVIS Prime, manage GCP VMs, and initialize modules. The lifecycle engine decides WHEN and tracks STATE. Locality drivers bridge the two:

```python
class LocalityDriver(Protocol):
    async def start(comp) -> int          # Returns PID or 0
    async def stop(comp) -> None
    async def health_check(comp) -> dict
    async def send_drain(comp, timeout_s) -> None
```

| Driver | Wraps |
|--------|-------|
| `InProcessDriver` | Existing supervisor init methods |
| `SubprocessDriver` | `CrossRepoStartupOrchestrator` process spawning |
| `RemoteDriver` | `GCPVMManager` VM operations |

### Module Retirement Plan

| Existing Module | Fate |
|----------------|------|
| `startup_state_machine.py` | Retired (import alias kept 1 release) |
| `cross_repo_startup_orchestrator.py` | Narrowed to `SubprocessDriver` |
| `startup_contracts.py` | Retired (replaced by `contracts` table + handshake) |
| `startup_transaction.py` | Retired (replaced by journal) |
| `state_authority.py` | Retired (replaced by `component_state` table) |
| `health_contracts.py` | Kept (HealthStatus/HealthReport still useful) |
| `protocol_version_gate.py` | Kept (GCP hot-swap validation) |

### Migration Strategy (Incremental, Rollback-Safe)

**Step 1**: Build foundation modules with full test suites. Existing startup unchanged.

**Step 2**: Bootstrap control plane alongside existing startup (observation phase). Journal records events but doesn't drive decisions. Verify state projection matches reality.

**Step 3**: Migrate subprocess components (jarvis_prime, reactor_core) to lifecycle engine. CrossRepoStartupOrchestrator becomes SubprocessDriver.

**Step 4**: Migrate in-process components. Wave execution replaces sequential phases.

**Step 5**: Retire old modules. Remove import aliases.

**Step 6**: Deploy lifecycle endpoints to JARVIS Prime and Reactor Core. Handshake upgrades from legacy fallback to full protocol.

### Rollback Safety

```python
CONTROL_PLANE_ENABLED = os.environ.get("JARVIS_CONTROL_PLANE", "false") == "true"
```

Gate removed in Step 5. Until then, unset or `false` runs existing code path.

### External Repo Changes

**JARVIS Prime**: ~50 lines — `HandshakeResponder.register_routes(app)` adding `/lifecycle/handshake` and `/lifecycle/drain`.

**Reactor Core**: ~50 lines — same pattern.

**Optional (Phase 2)**: `ControlPlaneSubscriber` for real-time event subscription.

### Testing Strategy

```
tests/unit/core/
├── test_orchestration_journal.py     # SQLite, lease CAS, fencing, replay
├── test_lifecycle_engine.py          # State machine, DAG waves, failure propagation
├── test_handshake_protocol.py        # Compatibility, version windows, legacy fallback
├── test_uds_event_fabric.py          # Wire protocol, subscribers, backpressure

tests/integration/
├── test_control_plane_e2e.py         # Full journal → engine → handshake → UDS flow
├── test_lease_contention.py          # Two supervisors competing for lease
├── test_crash_recovery.py            # Kill supervisor, restart, verify journal replay
├── test_cross_repo_lifecycle.py      # Subprocess start → handshake → heartbeat → drain → stop
```

---

## Future Phases (Not In Scope for Phase 1)

**Phase 2**: Cross-repo backpressure protocol, saturation signaling, adaptive capacity management.

**Phase 3**: Distributed deployment support (network-distributable control plane for multi-host).

**Phase 4**: Monolith containment — extract bounded modules from `unified_supervisor.py` with strict interfaces, guided by the ownership boundaries established in Phase 1.
