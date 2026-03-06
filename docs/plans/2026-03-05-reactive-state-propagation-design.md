# Disease 8 Cure: Reactive State Propagation Design

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement the implementation plan derived from this design.

**Goal:** Replace 23+ environment variables used for cross-repo state communication with a reactive, observable, versioned state propagation system — curing the root cause of non-atomic, non-observable, process-scoped, unversioned state sharing.

**Architecture:** A dedicated `ReactiveStateStore` (data plane) with CAS semantics, epoch fencing, ownership domains, and typed schemas. `StateAuthority` (policy plane) provides cross-key invariant validation. UMF (transport plane) propagates state changes across repos. An `EnvBridge` provides zero-downtime migration via `legacy → shadow → active` mode progression.

**Tech Stack:** Python 3.9+, SQLite WAL (journal), stdlib threading, existing UMF infrastructure, existing `state_authority.py` and `startup_contracts.py`.

---

## 1. Problem Statement

JARVIS uses 23+ environment variables as a cross-component state bus:

- `JARVIS_CAN_SPAWN_HEAVY`, `JARVIS_GCP_OFFLOAD_ACTIVE`, `JARVIS_HOLLOW_CLIENT_ACTIVE`, `JARVIS_STARTUP_COMPLETE`, `JARVIS_INVINCIBLE_NODE_IP`, `JARVIS_MEASURED_MEMORY_TIER`, etc.

**Why this is a disease, not a feature:**

| Limitation | Consequence |
|-----------|-------------|
| Non-atomic | Related state (e.g., GCP IP + offload flag) updates one-at-a-time; readers see intermediate states |
| Non-observable | Components must poll `os.environ` — no change notification, no watcher API |
| Process-scoped | Child processes inherit env at spawn time; no propagation of later changes |
| Unversioned | No CAS, no conflict detection, no audit trail of who changed what when |
| Inheritance fragile | `os.environ.copy()` + manual mutation pattern scattered across codebase |
| Not typed | All values are strings — coercion is ad-hoc and inconsistent |

## 2. Non-Negotiable Architecture Rules

- **Authority split:** Store = data plane, `state_authority.py` = policy plane, UMF = transport plane.
- **Commit ordering invariant:** validate → CAS/epoch/ownership → durable journal append → publish UMF event.
- **Replay invariant:** reconstructed state from journal at `global_revision = R` must equal live state snapshot at `R`.
- **Ownership invariant:** one writer domain per keyspace prefix; no wildcard overlaps.
- **Schema invariant:** key schema evolution is N/N-1 and migration metadata is attached to each schema change.
- **Backpressure invariant:** watcher overload cannot block writes indefinitely (explicit policy + metrics).
- **Observability invariant:** every rejection (`VERSION_CONFLICT`, `EPOCH_STALE`, etc.) is counted and emitted with key, writer, epoch, revision.

## 3. ReactiveStateStore Core

**Module:** `backend/core/reactive_state/store.py`

The store is an in-process, single-writer-per-key, versioned key-value store with CAS semantics.

```
ReactiveStateStore
├── _entries: Dict[str, StateEntry]     # key → current value + metadata
├── _journal: AppendOnlyJournal         # immutable change log
├── _ownership: Dict[str, str]          # key → writer_domain (e.g. "supervisor", "gcp_controller")
├── _schemas: Dict[str, KeySchema]      # key → type + validation + N/N-1 compat
├── _epoch: int                         # global epoch (incremented on restart/rejoin)
├── _watchers: Dict[str, List[Watcher]] # key-pattern → subscriber callbacks
```

**StateEntry** (frozen dataclass):
```python
key: str
value: Any                    # typed per KeySchema
version: int                  # monotonic per key, starts at 1
epoch: int                    # global epoch at write time
writer: str                   # writer_domain identity
origin: str                   # "explicit" | "default" | "derived"
updated_at_mono: float        # time.monotonic() for staleness
updated_at_unix_ms: int       # wall clock for cross-repo correlation
```

**Write semantics:**
- `write(key, value, expected_version, writer) → WriteResult`
- CAS: fails if `expected_version != current_version` (returns `VERSION_CONFLICT`)
- Ownership: fails if `writer` doesn't own the key's domain (returns `OWNERSHIP_REJECTED`)
- Schema: fails if value doesn't match `KeySchema` (returns `SCHEMA_INVALID`)
- Epoch fencing: fails if write carries a stale epoch (returns `EPOCH_STALE`)
- On success: increments version, appends to journal, notifies watchers, emits UMF `state.changed` event

**Read semantics:**
- `read(key) → Optional[StateEntry]` — returns full entry with staleness metadata
- `read_many(keys) → Dict[str, StateEntry]` — batch read, latest-per-key (non-atomic by default)
- `read_many_at_revision(keys, revision) → Dict[str, Optional[StateEntry]]` — atomic-at-revision, returns `None` for keys that did not exist at that revision. Audit/debug only, not hot-path.
- `watch(key_pattern, callback) → WatchId` — subscribe to changes (bounded queue + backpressure)

## 4. Ownership, Schema, and Authority Integration

### Ownership Model

**Module:** `backend/core/reactive_state/ownership.py`

Each key belongs to exactly one **writer domain** — a logical grouping tied to a component or subsystem. No wildcard overlaps allowed.

```python
@dataclass(frozen=True)
class OwnershipRule:
    key_prefix: str          # e.g. "lifecycle.", "gcp.", "memory."
    writer_domain: str       # e.g. "supervisor", "gcp_controller", "memory_assessor"
    description: str         # human-readable purpose
```

**Ownership table** (declarative config loaded from versioned manifest, frozen at startup):

| Prefix | Writer Domain | Examples |
|--------|--------------|---------
| `lifecycle.` | `supervisor` | `lifecycle.startup_complete`, `lifecycle.effective_mode` |
| `memory.` | `memory_assessor` | `memory.can_spawn_heavy`, `memory.available_gb`, `memory.tier` |
| `gcp.` | `gcp_controller` | `gcp.offload_active`, `gcp.node_ip`, `gcp.node_booting` |
| `hollow.` | `gcp_controller` | `hollow.client_active` |
| `prime.` | `supervisor` | `prime.early_pid`, `prime.early_port` |
| `service.` | `supervisor` | `service.tier_N_enabled` |
| `port.` | `supervisor` | `port.backend`, `port.frontend` |

**Validation on write:**
1. Find the longest matching prefix in the ownership table
2. If `writer` doesn't match `writer_domain` → `OWNERSHIP_REJECTED`
3. No prefix match → reject (all keys must be declared)

**Prefix collision validation at boot:** A startup validator rejects ambiguous overlaps (e.g., `gcp.` vs `gcp.node.` without explicit resolution).

**Cross-repo trust boundary:** Remote writes (via UMF) carry `writer` identity. The store validates against the ownership table — never trusts the source repo name alone. The writer identity is verified via UMF's HMAC signature + key ID / trust domain mapping, not self-asserted. A valid HMAC from `key_id=prime-001` only authorizes writes to keys whose ownership domain maps to that trust domain.

### Schema Model

**Module:** `backend/core/reactive_state/schemas.py`

```python
@dataclass(frozen=True)
class KeySchema:
    key: str                      # exact key name
    value_type: str               # "bool" | "str" | "int" | "float" | "enum"
    enum_values: Optional[Tuple[str, ...]]  # if value_type == "enum"
    nullable: bool                # whether None is a valid value
    pattern: Optional[str]        # regex for constrained strings
    min_value: Optional[float]    # for int/float
    max_value: Optional[float]    # for int/float
    default: Any                  # default value when not explicitly set
    origin_default: str           # "default" — origin tag for default value
    schema_version: int           # monotonic, starts at 1
    previous_version: Optional[int]  # N-1 for migration compatibility
    unknown_enum_policy: str      # "reject" | "map_to:<value>" | "default_with_violation"
    description: str
```

**Schema registry** maps the 23+ env vars to typed keys:

| Key | Type | Default | Env Var It Replaces |
|-----|------|---------|-------------------|
| `lifecycle.effective_mode` | enum(`local_full`, `local_optimized`, `sequential`, `cloud_first`, `cloud_only`, `minimal`) | `"local_full"` | `JARVIS_STARTUP_EFFECTIVE_MODE` |
| `lifecycle.startup_complete` | bool | `false` | `JARVIS_STARTUP_COMPLETE` |
| `memory.can_spawn_heavy` | bool | `false` | `JARVIS_CAN_SPAWN_HEAVY` |
| `memory.available_gb` | float | `0.0` | `JARVIS_HEAVY_ADMISSION_AVAILABLE_GB` |
| `memory.admission_reason` | str | `""` | `JARVIS_HEAVY_ADMISSION_REASON` |
| `memory.tier` | enum(`abundant`, `optimal`, `elevated`, `constrained`, `critical`, `emergency`, `unknown`) | `"unknown"` | `JARVIS_MEASURED_MEMORY_TIER` |
| `gcp.offload_active` | bool | `false` | `JARVIS_GCP_OFFLOAD_ACTIVE` |
| `gcp.node_ip` | str | `""` | `JARVIS_INVINCIBLE_NODE_IP` |
| `gcp.node_port` | int | `8000` | `JARVIS_INVINCIBLE_NODE_PORT` |
| `gcp.node_booting` | bool | `false` | `JARVIS_INVINCIBLE_NODE_BOOTING` |
| `hollow.client_active` | bool | `false` | `JARVIS_HOLLOW_CLIENT_ACTIVE` |
| *(full table in manifest.py)* | | | |

**Schema evolution:** N/N-1 compatibility. When a key's schema changes (e.g., adding a new enum value), `schema_version` increments. Unknown enum values are processed per `unknown_enum_policy`: `reject` returns error, `map_to:<value>` substitutes, `default_with_violation` applies default and logs a violation record to a separate `schema_violations` table.

### Multi-Key Atomicity

**MVP: single-key CAS only.** Multi-key transactions are explicitly not supported. If related state needs updating (e.g., `gcp.offload_active` + `hollow.client_active` + `gcp.node_ip`), the writer issues sequential writes. Consumers must tolerate intermediate states where only some keys are updated. The journal preserves ordering, so replaying will always see the same intermediate states in the same order.

**Consistency Groups** (metadata only in MVP):

```python
@dataclass(frozen=True)
class ConsistencyGroup:
    name: str                     # e.g. "gcp_readiness"
    keys: Tuple[str, ...]         # ("gcp.offload_active", "gcp.node_ip", "hollow.client_active")
    description: str
```

Consumers can use the `consistency_group` field in journal entries to batch-process related changes or detect partial updates. Future: upgrade to multi-key CAS using group as transaction boundary.

### Read Consistency

- `read(key)` → latest committed value (single-key, always consistent).
- `read_many(keys)` → latest-per-key, non-atomic. Each key independently returns its latest version.
- `read_many_at_revision(keys, revision)` → atomic-at-revision. All keys return their values as of the specified global_revision. Keys not yet existing at that revision return `None`. Uses journal replay if needed. Audit/debugging only.

### StateAuthority Integration

`state_authority.py` becomes the **policy enforcement hook** called during the write pipeline:

```
write(key, value, expected_version, writer)
  │
  ├─ 1. Schema validation (type, enum range, pattern, nullable)
  ├─ 2. Ownership check (writer_domain match)
  ├─ 3. Epoch fencing (reject stale epoch)
  ├─ 4. CAS check (expected_version == current)
  ├─ 5. StateAuthority.validate_write(key, value, context)  ← policy hook
  │     └─ Side-effect-free and deterministic (no network, no I/O, no clock branching)
  │        Uses read-only snapshot, never store.read() directly
  │        e.g., "gcp.offload_active=true requires gcp.node_ip != ''"
  ├─ 6. Journal append (durable, checksummed)
  └─ 7. Notify watchers + emit UMF state.changed event
```

### Observability

Every rejection is counted and emitted:

```python
@dataclass(frozen=True)
class WriteRejection:
    key: str
    writer: str
    writer_session_id: str
    reason: str          # VERSION_CONFLICT | OWNERSHIP_REJECTED | SCHEMA_INVALID | EPOCH_STALE | POLICY_REJECTED
    epoch: int
    attempted_version: int
    current_version: int
    global_revision_at_reject: int
    timestamp_mono: float
```

Counter: `rejection_total{key, reason}`. Emitted as UMF telemetry event for cross-repo visibility.

## 5. Append-Only Journal

**Module:** `backend/core/reactive_state/journal.py`

The journal is the durable source of truth. State can always be reconstructed by replaying the journal from epoch start.

```python
@dataclass(frozen=True)
class JournalEntry:
    global_revision: int          # monotonic across all keys, starts at 1 per epoch
    key: str
    value: Any                    # serialized value
    previous_value: Any           # for rollback/audit
    version: int                  # key-specific version after this write
    epoch: int
    writer: str                   # writer_domain
    writer_session_id: str        # distinguishes restarts of same writer
    origin: str                   # "explicit" | "default" | "derived"
    consistency_group: Optional[str]  # e.g. "gcp_readiness" — metadata only in MVP
    timestamp_unix_ms: int
    checksum: str                 # SHA-256 of (global_revision, key, value, previous_value,
                                  #             version, epoch, writer_session_id, consistency_group)
```

**Storage:** SQLite WAL (same pattern as UMF dedup ledger). Single table, append-only inserts.

```sql
CREATE TABLE state_journal (
    global_revision INTEGER PRIMARY KEY,
    key TEXT NOT NULL,
    value TEXT NOT NULL,          -- JSON-encoded
    previous_value TEXT NOT NULL,
    version INTEGER NOT NULL,
    epoch INTEGER NOT NULL,
    writer TEXT NOT NULL,
    writer_session_id TEXT NOT NULL,
    origin TEXT NOT NULL,
    consistency_group TEXT,
    timestamp_unix_ms INTEGER NOT NULL,
    checksum TEXT NOT NULL
);
CREATE INDEX idx_journal_key ON state_journal(key, version);
CREATE INDEX idx_journal_epoch ON state_journal(epoch);
```

**Durability:** Journal fsync policy is `PRAGMA synchronous = NORMAL` (WAL mode default — durable on commit, not on every write). Expected RPO: last committed transaction. RTO: checkpoint load + replay time (sub-second for typical state sizes).

**Replay invariant:** Reconstructed state from journal entries `1..R` must equal the live state snapshot at global_revision `R`. Verified by checkpoint comparison.

**Compaction:**
- Uses copy-on-write checkpointing (no write pause required).
- Checkpoint record = full state snapshot + `last_applied_revision` + SHA-256 of snapshot.
- Checkpoint verified before archive: reconstruct from checkpoint + remaining entries == live state.
- Old entries moved to archive table (not deleted) for audit trail.
- Archived rows carry archive-batch hash + manifest for audit chain.

**Journal gap detection:** Startup validator rejects if revision sequence has holes, unless an explicit recovery workflow repairs the gap.

**Hot-restart bootstrap:**
1. Increment `epoch` (derived from `session_id` + monotonic counter, never reuses previous epoch).
2. Load latest checkpoint if available.
3. Replay journal entries after checkpoint's `last_applied_revision`.
4. All writes from previous epoch are readable but not writable — epoch fencing rejects writes carrying old epoch.
5. Previous process instance's pending watchers are dead — new subscribers must re-register.
6. Mandatory `post_replay_invariant_audit()` gate — blocks READY if critical invariants fail.

**Schema rollback (N → N-1 downgrade):**
- Journal entries written under schema version N remain in the journal with their original values.
- On replay under N-1 schema, entries with unknown enum values are processed per `unknown_enum_policy`: `reject` halts replay with error, `map_to:<value>` substitutes, `default_with_violation` applies default and logs a violation record.
- The violation record is appended to a separate `schema_violations` table for audit.

**Clock anomaly resilience:** All cross-process ordering uses `global_revision`, never wall time. `updated_at_unix_ms` is for audit logs only.

## 6. UMF Event Integration

**Module:** `backend/core/reactive_state/event_emitter.py`

After a successful journal append, the store publishes a UMF message on the `event` stream:

```python
UmfMessage(
    stream=Stream.event,
    kind=Kind.event,
    source=MessageSource(
        repo="jarvis",
        component="reactive_state_store",
        instance_id=instance_id,
        session_id=session_id,
    ),
    target=MessageTarget(repo="broadcast", component="*"),
    payload={
        "event_type": "state.changed",
        "event_schema_version": 1,    # independent of key schema
        "key": "gcp.offload_active",
        "value": True,
        "previous_value": False,
        "version": 7,
        "epoch": 3,
        "global_revision": 142,
        "writer": "gcp_controller",
        "writer_session_id": "sess-abc-123",
        "origin": "explicit",
        "consistency_group": "gcp_readiness",
        "schema_version": 1,
    },
)
```

**Guarantees:**
- Event published **only after** durable journal append (never before).
- Event contains the full change record — subscribers don't need to call back to the store.
- Idempotency key = `f"state.{epoch}.{global_revision}"` — dedup prevents duplicate processing on retry.
- If UMF publish fails, the write still succeeded (journal is authoritative). A background reconciler re-publishes un-acked events.

**Reconciler:**
- Uses `last_published_revision` cursor persisted durably in journal DB (same SQLite).
- On crash recovery, replays from cursor position.
- Handles: partial publish ACK (re-publish from last confirmed cursor), duplicate replay (idempotency key dedup), out-of-order cursor commits (cursor only advances monotonically).

**Cross-repo consumption:**
- jarvis-prime and reactor-core subscribe to `state.changed` events via their UMF clients.
- They maintain a local read-only projection of relevant keys (like `HeartbeatProjection` pattern).
- They do NOT write back via UMF directly — all writes go through the authoritative store in the supervisor process.
- Remote write requests are submitted as UMF commands (`state.write_request`) with explicit `requested_writer_domain` validated against trust-domain mapping.

**Projection staleness SLO:** Maximum lag from `global_revision` commit to subscriber projection apply is monitored. Breaches are logged as warnings.

## 7. Watcher / Subscription System

**Module:** `backend/core/reactive_state/watchers.py`

In-process observers that receive change notifications synchronously after journal commit.

```python
@dataclass
class WatchSpec:
    watch_id: str                 # UUID
    key_pattern: str              # exact key or glob (e.g. "gcp.*")
    callback: Callable[[StateEntry, StateEntry], None]  # (old, new) → None
    queue: collections.deque      # bounded buffer
    max_queue_size: int           # default 100
    overflow_policy: str          # "drop_oldest" | "drop_newest" | "block_bounded"
```

**Registration:**
```python
watch_id = store.watch("gcp.*", on_gcp_change, max_queue_size=50, overflow_policy="drop_oldest")
# Later:
store.unwatch(watch_id)
```

**Backpressure invariant:** Watcher overload cannot block writes indefinitely.
- `drop_oldest`: oldest pending notification is discarded, counter incremented.
- `drop_newest`: new notification is discarded if queue full.
- `block_bounded`: write blocks for up to `watcher_timeout_ms` (default 100ms), then drops and logs. **Disallowed on lifecycle-critical write paths** (enforced by path annotation).
- Metrics: `watcher_drops_total` counter per `(watch_id, overflow_policy)`.
- A watcher that drops >N notifications in M seconds triggers a WARNING log.

**Callback contract:**
- Callbacks receive `(old_entry: Optional[StateEntry], new_entry: StateEntry)`.
- Callbacks MUST be non-blocking and side-effect-light (no I/O, no network). Heavy work should be dispatched to a task queue.
- Callbacks are invoked in journal-append order (preserving causal ordering).
- If a callback raises, the exception is logged and the watcher continues (no poisoning).

**Policy deadlock prevention:**
- `StateAuthority.validate_write()` is side-effect-free and deterministic.
- Cross-key invariant checks read current state via a read-only snapshot passed as argument, never via `store.read()`.
- Watcher callbacks are invoked AFTER the write is fully committed — they cannot recursively trigger validation of the same write.
- If a watcher callback triggers a new `store.write()`, that write enters the normal pipeline — no re-entrancy on the same lock.

**Partial journal replay with policy evolution:**
- During replay, entries are validated against the schema version recorded in each entry, not the current schema.
- Policy validation (`StateAuthority.validate_write`) is NOT re-run during replay — replay trusts the journal.
- A `post_replay_invariant_audit()` runs after replay and flags violations without rejecting replayed state.

## 8. Environment Variable Compatibility Bridge

**Module:** `backend/core/reactive_state/env_bridge.py`

The bridge ensures zero-downtime migration from env vars to the reactive store. It operates in three modes, controlled by `JARVIS_STATE_BRIDGE_MODE` env var.

### Bridge Modes

| Mode | Authoritative Source | Behavior |
|------|---------------------|----------|
| `legacy` (default) | `os.environ` | Store mirrors env reads. No store writes. Baseline. |
| `shadow` | `os.environ` | Store receives writes in parallel. Shadow comparisons logged. Env remains authoritative for all reads. |
| `active` | `ReactiveStateStore` | Store is authoritative. Env vars are written as compatibility mirrors for child processes. |

**Mode transition rules:** `legacy → shadow → active` only. No skipping. No reverse transitions in production (rollback requires restart with lower mode).

**Bootstrap resolution:** `JARVIS_STATE_BRIDGE_MODE` is read from `os.environ` *before* store initialization. If missing, defaults to `legacy`. If invalid value, logs error and defaults to `legacy`. This is the one env var that is never migrated to the store.

**Mode change authority:** Only the supervisor process can trigger mode transitions, via explicit API call. No automatic promotion.

### Key Mapping

```python
@dataclass(frozen=True)
class EnvKeyMapping:
    env_var: str                  # "JARVIS_GCP_OFFLOAD_ACTIVE"
    state_key: str                # "gcp.offload_active"
    coerce_to_env: Callable       # bool → "true"/"false"
    coerce_from_env: Callable     # "true" → True
    sensitive: bool               # if True, values are redacted in logs/metrics
```

### Shadow Mode Behavior

1. When code calls `store.write(key, value, ...)`, the value is committed to the store.
2. The bridge reads the current env var and compares using **canonical comparison**: values are coerced to their schema type before comparison (`"true"` == `"1"` == `True` for bools, absent == `None` == default for missing keys).
3. Mismatches are logged with full context: `key`, `store_value`, `env_value`, `global_revision`. Sensitive keys have values redacted.
4. A parity counter tracks match/mismatch rates (reuses `ShadowParityLogger` from UMF).
5. Promotion to `active` mode is blocked if parity < 99.9% over configurable soak window.

**Read/write precedence in shadow mode:**
- All consumer reads go through `os.environ` (env is authoritative).
- Store writes happen in parallel for comparison purposes only.
- The store does NOT feed back into env reads — no dual-authority ambiguity.

### Active Mode Behavior

1. Store is authoritative for all mapped keys.
2. After every successful write, the bridge writes the env var as a compatibility mirror:
   ```python
   os.environ[mapping.env_var] = mapping.coerce_to_env(new_value)
   ```
3. **Env mutation serialization:** A threading lock serializes env mirror writes and `get_subprocess_env()` calls to prevent race conditions on the process-global `os.environ`.
4. **Loop prevention:** Mirror writes carry a suppression token (version guard). If the store detects a write that originated from its own mirror, it skips re-mirroring.

**Subprocess environment:**

```python
def get_subprocess_env(store: ReactiveStateStore, revision: Optional[int] = None) -> Dict[str, str]:
    """Build env dict from store snapshot for child process spawning.

    If revision is specified, pins to that global_revision for coherent snapshot.
    Otherwise uses latest-per-key.
    """
    env = os.environ.copy()
    entries = store.read_many_at_revision(
        [m.state_key for m in ENV_KEY_MAPPINGS], revision
    ) if revision else {m.state_key: store.read(m.state_key) for m in ENV_KEY_MAPPINGS}
    for mapping in ENV_KEY_MAPPINGS:
        entry = entries.get(mapping.state_key)
        if entry is not None:
            env[mapping.env_var] = mapping.coerce_to_env(entry.value)
    return env
```

### Unmapped Keys

Env vars not in the mapping table are **pass-through**: they continue to work via `os.environ` in all modes. They are not tracked, versioned, or observable by the store. This is explicitly documented — no silent drift zone.

### Per-Domain Kill Switches

Each ownership domain can be independently bridged to `active` mode:
```python
JARVIS_STATE_BRIDGE_ACTIVE_DOMAINS=gcp,memory  # only these domains use store as authority
```
Keys in non-active domains remain in `shadow` or `legacy` behavior. This reduces blast radius during rollout.

## 9. Rollout Strategy

**Wave 0: Foundation (store + journal + schemas)**
- Create `ReactiveStateStore`, `AppendOnlyJournal`, `KeySchema`, `OwnershipRule`.
- Ownership manifest as versioned declarative config (`reactive_state_manifest.py`).
- Prefix collision validator at boot.
- Tests: CAS semantics, epoch fencing, schema validation, ownership rejection.

**Wave 1: Authority integration + watchers**
- Wire `StateAuthority.validate_write()` as policy hook (side-effect-free, snapshot-based).
- Watcher system with bounded queues and overflow policies.
- `block_bounded` disallowed on lifecycle-critical paths (enforced by path annotation).
- Post-replay invariant audit gate.
- Tests: policy rejection, watcher delivery ordering, backpressure, replay correctness.

**Wave 2: UMF event integration**
- `state.changed` event emission after journal commit.
- `last_published_revision` cursor persisted in journal DB.
- Background reconciler republishes from cursor on crash recovery.
- State event payload carries its own `event_schema_version`.
- Tests: event emission, idempotency, reconciler replay, cross-repo subscription.

**Wave 3: Env bridge — shadow mode**
- `EnvKeyMapping` table for all 23+ env vars.
- Shadow comparisons with canonical coercion and `ShadowParityLogger`.
- `JARVIS_STATE_BRIDGE_MODE=shadow` opt-in.
- Soak: run shadow for configurable window, measure parity.
- Tests: shadow comparison logging, parity calculation, mode switching, coercion equivalence.

**Wave 4: Env bridge — active mode + subprocess helper**
- `get_subprocess_env()` replaces manual `os.environ.copy()` + mutation.
- Env mirror writes after store commits with serialization lock.
- Per-domain kill switches for blast radius control.
- Migrate supervisor env-setting callsites to `store.write()`.
- Tests: env mirror correctness, subprocess env snapshot, revision-pinned coherence, end-to-end write-through.

**Wave 5: Consumer migration + legacy removal**
- Migrate hot-path env readers to `store.read()`.
- Add deprecation warnings on direct `os.environ` reads for mapped keys.
- Remove env var as authoritative source once all consumers migrated.
- Final: `JARVIS_STATE_BRIDGE_MODE=active` becomes default.

**Wave 6: Hardening**
- Copy-on-write checkpointing (no write pause).
- Journal gap detection at startup.
- Archive integrity (batch hash + manifest).
- Projection staleness SLO metrics.
- Cross-repo read-only projections in jarvis-prime and reactor-core.
- Chaos testing: crash during write, epoch fencing after restart, stale writer rejection.

## 10. Modular File Decomposition

```
backend/core/reactive_state/
├── __init__.py
├── store.py                    # ReactiveStateStore (core CAS + read + watch)
├── journal.py                  # AppendOnlyJournal (SQLite WAL)
├── schemas.py                  # KeySchema + schema registry + N/N-1 compat
├── ownership.py                # OwnershipRule + registry + prefix validator
├── manifest.py                 # Versioned ownership + schema manifest (declarative)
├── watchers.py                 # WatchSpec + bounded queue + overflow policy
├── event_emitter.py            # UMF state.changed integration + reconciler
├── env_bridge.py               # EnvKeyMapping + shadow/active bridge + get_subprocess_env()
├── types.py                    # StateEntry, WriteResult, WriteRejection, JournalEntry
└── audit.py                    # Post-replay invariant audit + schema violation tracking
```

## 11. Go/No-Go Gate

All must be true before legacy env var authority is removed:

- Shadow parity >= 99.9% over 4h soak.
- Replay invariant verified: journal reconstruction == live snapshot at every checkpoint.
- Zero `EPOCH_STALE` rejections from current-epoch writers.
- All critical consumers migrated to `store.read()`.
- `get_subprocess_env()` used at all subprocess spawn sites.
- Post-replay invariant audit passes on restart.
- Watcher drop rate < 0.1% under normal load.
- All mapped keys have deterministic coercion + parity canonicalization tests.
- No unknown mapped-key direct env reads in critical paths (enforced lint + CI failure).
- Replay + publish reconciliation proof under crash matrix (not just nominal replay equality).
- Revision-coherent subprocess env verified at all spawn sites.

## 12. Test Blueprint

| Category | Must-Pass Criteria |
|----------|-------------------|
| CAS semantics | Concurrent writes: exactly one wins, others get VERSION_CONFLICT |
| Epoch fencing | Stale-epoch write rejected; new-epoch write succeeds |
| Ownership | Wrong writer_domain rejected; correct domain accepted |
| Schema validation | Type mismatch rejected; enum out-of-range rejected; nullable handled |
| Journal replay | Reconstructed state == live snapshot at every 100th revision |
| Watcher delivery | Notifications in journal order; overflow policy respected |
| Policy hook | Cross-key invariant enforced; recursive write doesn't deadlock |
| UMF events | Event published only after journal commit; reconciler fills gaps |
| Shadow parity | Mismatches detected and logged; parity ratio calculated correctly |
| Env bridge active | Store write mirrors to env; subprocess env snapshot correct |
| Hot restart | Epoch increments; old-epoch writes fenced; checkpoint loads correctly |
| Compaction | Copy-on-write checkpoint verified; archived entries have batch hash |
| Property/fuzz | Coercion and parity equivalence classes |
| Fault injection | Crash between journal commit and env mirror write; recovery contract |
| Concurrency stress | High-frequency writes + subprocess spawns + watcher overflow |
| SQLite contention | Long-running read txn vs write bursts vs checkpoint |
| Migration safety | Hidden manual `os.environ` mutations detected in callsites |
| Security | Sensitive keys redacted in WriteRejection, parity logs, and UMF payloads |

---

## Appendix A: Safety & Invariants

### A.1 Bridge Mode Safety Invariants

- **Transition rules:** `legacy → shadow → active` only. No skipping, no reverse transitions without process restart.
- **Rollback semantics:** To roll back from `active` to `shadow`, restart the process with `JARVIS_STATE_BRIDGE_MODE=shadow`. The store still functions (journal preserved), but env becomes authoritative for reads again.
- **Mode change authority:** Only the supervisor process triggers mode transitions via explicit API call. No automatic promotion.

### A.2 Bootstrap Chicken-Egg Resolution

- `JARVIS_STATE_BRIDGE_MODE` is resolved from `os.environ` before store initialization.
- If missing: defaults to `legacy`. If invalid: logs error and defaults to `legacy`.
- This env var is never migrated to the store — it is the one bootstrap-critical env var that stays as-is.

### A.3 Mapped vs Unmapped Keys

- Env vars in the mapping table: fully managed by the bridge in the current mode.
- Env vars NOT in the mapping table: pass-through via `os.environ`, no tracking, no versioning.
- Documented explicitly so there is no silent drift zone.

### A.4 Env Mutation Race Semantics

- `os.environ` is process-global and not thread-safe by default.
- A dedicated `threading.Lock` serializes: (a) env mirror writes from store, (b) `get_subprocess_env()` calls, (c) any direct env mutation in bridged code paths.
- All env mutations for mapped keys go through the bridge — direct `os.environ` mutation for mapped keys is a lint error.

### A.5 Parity Correctness Rules

Canonical comparison normalizes values before comparison:
- Booleans: `"true"`, `"1"`, `"yes"` → `True`; `"false"`, `"0"`, `"no"`, `""` → `False`
- Absent key: treated as schema default value, not as `None` or empty string
- Numeric strings: parsed to int/float before comparison
- Enums: case-sensitive exact match after stripping whitespace

### A.6 Shadow Write Direction

In `shadow` mode:
- **Reads:** always from `os.environ` (authoritative)
- **Writes:** committed to store for comparison only
- The store never feeds back into env reads in shadow mode — no dual-authority window

### A.7 Two-Way Loop Prevention

- In `active` mode, store-to-env mirror writes carry a version guard.
- If the bridge detects a write that originated from its own env mirror (same version), it skips re-mirroring.
- No env-to-store sync exists in `active` mode — store is sole authority.

### A.8 Durability Guarantees

- SQLite WAL with `PRAGMA synchronous = NORMAL`.
- RPO: last committed WAL frame (typically sub-millisecond).
- RTO: checkpoint load + replay (sub-second for typical state sizes < 10K entries).
- Power-loss during WAL write: SQLite recovers to last consistent state on next open.

### A.9 Publisher Recovery Edge Cases

The reconciler handles:
- **Partial publish ACK:** re-publishes from last confirmed cursor position.
- **Duplicate replay:** idempotency key (`state.{epoch}.{global_revision}`) prevents double-processing.
- **Out-of-order cursor commits:** cursor only advances monotonically; never moves backward.

### A.10 Security Boundary

- `EnvKeyMapping.sensitive` flag marks keys whose values must be redacted.
- Redaction applies to: shadow parity logs, `WriteRejection` emissions, UMF payloads, debug/audit output.
- Sensitive keys: any that contain IPs, tokens, credentials, or API keys.

### A.11 Ownership Transfer Protocol

For controlled handoff when a key domain's owner changes:
1. New owner declares intent in the ownership manifest (new version with explicit transfer token and deadline).
2. During migration window, both old and new owner are accepted (dual-writer mode with version bump required).
3. After deadline, old owner is revoked automatically.
4. Journal entries record which owner wrote them — audit trail preserved.
5. If deadline passes without clean handoff, the store logs an error and reverts to old owner.

### A.12 Subprocess Snapshot Atomicity

- `get_subprocess_env(revision=R)` pins all mapped keys to global_revision `R`.
- Guarantees child process receives one coherent state revision, not a mix of versions.
- If `revision` is `None`, latest-per-key is used (non-atomic, for backward compat).

### A.13 Operational Blast Radius Controls

- Per-domain kill switches: `JARVIS_STATE_BRIDGE_ACTIVE_DOMAINS=gcp,memory`.
- Only listed domains use store as authority; others remain in `shadow` or `legacy`.
- Emergency override: `JARVIS_STATE_BRIDGE_MODE=legacy` on restart disables all store authority.
- Metrics: per-domain write rate, rejection rate, parity score — enable granular rollout decisions.
