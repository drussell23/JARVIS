# Control Plane Phase 2A: Hardening Design

**Date**: 2026-02-24
**Status**: Approved
**Phase**: 2A (Hardening — mandatory gate before Phase 2B capabilities)
**Prerequisite**: Phase 1 foundation complete (137 tests, 9 commits)

## Problem Statement

Phase 1 delivered the control plane foundation: SQLite journal, lifecycle engine, handshake protocol, UDS event fabric, locality drivers. But four correctness gaps remain that can cause the control plane to misrepresent system state:

1. **No recovery probe** — After crash restart, the supervisor trusts journal state blindly. A component the journal says is READY might be dead.
2. **No UDS keepalive** — Dead subscribers are invisible until the next emit() hits a broken pipe. Silent connection accumulation.
3. **No reconnect+replay proof** — The core UDS value proposition (disconnect, reconnect, catch up on missed events) is unproven end-to-end.
4. **No journal compaction** — Append-only journal grows unbounded, slowing replay and consuming disk.

Without these fixes, Phase 2B capabilities (backpressure, saturation signaling, adaptive capacity) would run on a substrate that can lie under fault.

## Execution Order and Gate Rules

```
Recovery Protocol (item 3)
    → UDS Keepalive (item 1)
        → [HARD GATE: Reconnect+Replay tests must pass]
            → Journal Compaction (item 2)
                → [HARD GATE: Full fault-injection suite]
                    → Phase 2B unlocked
```

No forward progress past a gate until all tests at that gate pass.

## Design Principles

- **Recovery** owns "what changed while supervisor lacked authority" (startup + lease takeover)
- **Heartbeat/keepalive** owns "what is changing now" (runtime liveness)
- **Sparse audit** is a low-frequency integrity check, not a health loop
- All corrective writes are idempotent transition commands with rich fingerprinted keys
- All probes are fenced — if lease changes mid-probe, discard results

---

## Section 1: Recovery Protocol (Probe + Reconcile)

### When It Runs

- On lease acquisition (both first boot and takeover after crash)
- New leader holds lease before probing; all corrective writes fenced under new epoch

### Algorithm

```
1. Rebuild projected state
   - Read component_state table (persisted projection)
   - Cross-reference with replay_from(0) for consistency

2. Classify components by projected status:
   - STOPPED, REGISTERED → skip (nothing to probe)
   - All others → mark UNVERIFIED, add to probe queue

3. Probe with bounded retry:
   - Max 2 attempts per component, jittered backoff between attempts
   - Before each probe: verify lease still held (epoch check)
   - If lease lost mid-reconcile: abort entirely, discard all pending writes
   - On timeout/unreachable after retries: finalize as LOST
   - On reachable: capture full ProbeResult with fence context

4. Reconcile projected vs actual:
   - Projected READY/DEGRADED, actual unreachable → reconcile_mark_lost
   - Projected READY/DEGRADED, actual reachable+healthy → no-op
   - Projected READY, actual reachable+degraded → reconcile_mark_degraded
   - Projected FAILED/LOST, actual reachable+healthy → reconcile_recover
     (requires full handshake revalidation, not just /health success)
   - Projected STARTING/HANDSHAKING, actual unreachable → reconcile_mark_failed
   - Projected DRAINING/STOPPING, actual unreachable → reconcile_mark_stopped

5. All corrective writes are idempotent transition commands:
   - Actions: reconcile_mark_lost, reconcile_mark_failed,
     reconcile_mark_stopped, reconcile_mark_degraded, reconcile_recover
   - Idempotency key includes contradiction fingerprint:
     f"reconcile:{component}:{epoch}:{projected}->{observed_category}:{instance_id}:{api_version}"
   - Prevents suppression of distinct corrections within same epoch
```

### Recovery Transition Guard

When recovering a FAILED/LOST component to READY:
- Route through: FAILED/LOST → STARTING → HANDSHAKING → READY
- This revalidates version compatibility, capability set, and schema hash
- Bounded handshake window: `handshake_timeout_s` per component declaration
- Terminal fallback on timeout: → DEGRADED (if soft dependency) or → FAILED (if hard)
- Cannot stall reconciliation — bounded window enforced

### Probe Result

```python
class HealthCategory(Enum):
    HEALTHY = "healthy"
    CONTRACT_MISMATCH = "contract_mismatch"
    DEPENDENCY_DEGRADED = "dependency_degraded"
    SERVICE_DEGRADED = "service_degraded"
    UNREACHABLE = "unreachable"

@dataclass
class ProbeResult:
    reachable: bool
    category: HealthCategory
    instance_id: str = ""
    api_version: str = ""
    error: str = ""
    probe_epoch: int = 0         # epoch when probe was issued
    probe_seq: int = 0           # journal seq at probe time (monotonic fence)
```

### Probe Strategies by Locality

- **SUBPROCESS / REMOTE:** HTTP GET `/health` with timeout (5s subprocess, 10s remote). Parse response for status, instance_id, api_version. Classify into HealthCategory.
- **IN_PROCESS:** Must provide runtime evidence, not just "module loaded." Required checks: (a) background task alive and not cancelled, (b) last heartbeat tick within TTL, (c) no fatal error latch set, (d) critical dependencies reachable. Exposed via `RuntimeHealthProbe` callable registered per component.

### Sparse Audit

- Leader-only, 15-60min interval (jittered ±25%)
- Same probe logic as startup recovery
- Read-only by default — only writes on detected contradiction
- Action: `audit_reconcile` with idempotency key: `f"audit:{component}:{epoch}:{audit_round}:{projected}->{observed}"`
- Skipped if supervisor is not lease holder

---

## Section 2: UDS Keepalive (Ping/Pong)

### Wire Protocol Additions

Two new message types (same length-prefixed JSON framing):

```python
# Server → subscriber
{"type": "ping", "ping_id": "<nonce>", "ts": <monotonic_timestamp>}

# Subscriber → server
{"type": "pong", "ping_id": "<echo_nonce>", "ts": <echo_ts>}
```

`ping_id` is a unique nonce per ping. Pong must echo it back to prevent ambiguity with delayed/out-of-order frames.

### Server-Side (EventFabric)

- Background task per subscriber: send ping every `KEEPALIVE_INTERVAL_S`
- Track `last_pong_received` AND `last_seen_any` (monotonic clock) per subscriber
- `last_seen_any` updates on ANY valid frame (event ack, pong, subscribe)
- Dead detection: absolute deadline `max(last_pong, last_seen_any) + KEEPALIVE_TIMEOUT_S`
- Compare monotonic now against deadline — no implicit "missed ping" counting
- On dead subscriber:
  - Close writer
  - Remove from `_subscribers` dict
  - Emit structured disconnect with reason: `timeout`, `write_error`, `eof`, `protocol_error`
  - Log subscriber_id, last_pong time, disconnect reason
- Ping task cancels on `fabric.stop()` and subscriber disconnect

### Client-Side (ControlPlaneSubscriber)

- In `_receive_loop`: on frame with `type: "ping"`, send pong immediately
- Pong write is timeout-bounded (2s) — stalled write does not freeze receive processing
- On pong write timeout: log warning, skip (server will detect via timeout)
- No separate task needed — pong is inline

### Constants

```python
KEEPALIVE_INTERVAL_S = 10.0
KEEPALIVE_TIMEOUT_S = 30.0
PONG_WRITE_TIMEOUT_S = 2.0
```

Configurable via `JARVIS_UDS_KEEPALIVE_INTERVAL`, `JARVIS_UDS_KEEPALIVE_TIMEOUT`.

### Edge Cases

| Scenario | Behavior |
|----------|----------|
| Subscriber slow (pong delayed) | `last_seen_any` covers active event flow. 30s deadline is generous. |
| Graceful disconnect | Writer close detected on next ping write. Immediate cleanup, reason=`eof`. |
| Ping overlaps with event | Both serialized through subscriber queue → sender task. |
| Server restart | Old connections die. Subscribers detect EOF, trigger reconnect. |

---

## Section 3: Reconnect + Replay (Gate)

### Client Auto-Reconnect (ControlPlaneSubscriber)

On connection loss (EOF, IncompleteReadError, write failure):

```
Connection lost
  → close writer
  → wait reconnect_delay (0.5s start, exponential backoff, max 30s, ±25% jitter)
  → attempt connect()
  → on success: subscribe with last_seen_seq → receive subscribe_ack
    → check earliest_available_seq in ack for gap detection
    → resume receive loop
  → on failure: increment attempts, backoff, retry
  → after max_reconnect_attempts (20): invoke on_disconnect callback, give up
```

### Key Behaviors

- `last_seen_seq` tracks highest seq dispatched to callbacks
- Reconnect is transparent to consumers — events continue with no gap (if replay covers)
- If `earliest_available_seq > last_seen_seq + 1`: invoke `on_gap` callback for consumers to handle (e.g., full state resync)

### Server-Side: `subscribe_ack` Enhancement

```python
{
    "type": "subscribe_ack",
    "subscriber_id": "...",
    "status": "ok",
    "earliest_available_seq": <MIN(seq) from journal>,  # NEW
}
```

Client receives `earliest_available_seq` at subscribe time. Gap detection is immediate and deterministic — no guesswork from sequence discontinuities.

### Gate Tests (all must pass before Section 4)

```
test_subscriber_reconnect_and_replay:
  1. Start journal + fabric
  2. Connect subscriber, verify subscribe_ack
  3. Emit events seq 1, 2, 3 → subscriber receives all three
  4. Kill subscriber connection (close writer)
  5. Emit events seq 4, 5 while subscriber disconnected
  6. Subscriber reconnects with last_seen_seq=3
  7. Verify subscriber receives replayed events 4, 5
  8. Emit event seq 6 → subscriber receives it (live stream)
  9. Verify no gaps in received sequence

test_subscriber_detects_keepalive_timeout:
  1. Start fabric with short keepalive (interval=1s, timeout=3s)
  2. Connect subscriber, verify subscribe_ack
  3. Subscriber stops responding to pings
  4. Wait 4s
  5. Verify server removed subscriber
  6. Verify disconnect reason is "timeout"

test_subscriber_reconnect_after_keepalive_death:
  1. Subscriber stops ponging → server kills it
  2. Subscriber detects EOF → auto-reconnects with last_seen_seq
  3. Events emitted during dead window are replayed
  4. Live stream resumes with no gap
```

---

## Section 4: Journal Compaction

### Compaction Algorithm

```
1. Determine retention boundary:
   - Current epoch entries: always retained
   - Prior epoch entries: retain most recent COMPACTION_RETAIN_PRIOR_EPOCHS (1000) by seq
   - Everything else: eligible for archival

2. Pre-compaction FK safety:
   - For each component_state row where last_seq references a to-be-compacted entry:
     update last_seq to the nearest retained seq (preserves FK integrity)

3. Archive eligible entries (same-file archive table):
   - INSERT INTO journal_archive SELECT * FROM journal WHERE seq <= boundary AND epoch < current
   - DELETE FROM journal WHERE seq <= boundary AND epoch < current
   - Single transaction, single WAL — truly atomic (no cross-DB crash risk)

4. Post-compaction:
   - PRAGMA wal_checkpoint(TRUNCATE) to reclaim WAL space
   - Log: entries archived, entries remaining, duration
```

### Archive Storage

Same-file `journal_archive` table within `orchestration.db` (not a separate database).

```sql
CREATE TABLE IF NOT EXISTS journal_archive (
    seq             INTEGER PRIMARY KEY,
    epoch           INTEGER NOT NULL,
    timestamp       REAL NOT NULL,
    wall_clock      TEXT NOT NULL,
    actor           TEXT NOT NULL,
    action          TEXT NOT NULL,
    target          TEXT NOT NULL,
    idempotency_key TEXT,
    payload         TEXT,
    result          TEXT,
    fence_token     INTEGER NOT NULL,
    archived_at     REAL NOT NULL       -- time.time() when archived
);
```

Eliminates cross-DB atomicity problem. Archive rows are never queried during normal operation — forensics only.

Optional: `JARVIS_JOURNAL_ARCHIVE_ENABLED=false` skips archival and just deletes.

### When Compaction Runs

- On lease acquisition, after recovery protocol completes
- Background task every 24h (jittered ±1h), leader-only
- Epoch-fenced — verify lease before and after compaction transaction

### Batched Compaction for Large Journals

If journal has 100K+ entries, compact in batches of 10K to avoid holding the write lock too long. Each batch is its own transaction.

### Constants

```python
COMPACTION_RETAIN_PRIOR_EPOCHS = 1000
COMPACTION_INTERVAL_S = 86400          # 24h
COMPACTION_BATCH_SIZE = 10000
```

Overridable via `JARVIS_JOURNAL_RETAIN_PRIOR`, `JARVIS_JOURNAL_COMPACTION_INTERVAL`.

### Edge Cases

| Scenario | Behavior |
|----------|----------|
| First boot, no prior entries | Compaction is a no-op |
| Lease lost during compaction | Transaction rolls back. No partial deletes. |
| Subscriber's last_seen_seq references compacted entry | Server sends earliest_available_seq in subscribe_ack. Client detects gap, invokes on_gap. |
| Very large journal | Batched compaction (10K per transaction) |
| Archive disabled | Entries deleted without archival |

### Tests

```
test_compaction_retains_current_epoch:
  - Write entries across epoch 1 and epoch 2
  - Run compaction as epoch 2 leader
  - Verify all epoch 2 entries retained
  - Verify epoch 1 trimmed to most recent 1000

test_compaction_archives_to_same_db:
  - Write 1500 entries in epoch 1, acquire epoch 2
  - Run compaction
  - Verify archived entries in journal_archive table
  - Verify main journal has ≤1000 epoch 1 entries + all epoch 2

test_compaction_preserves_fk_integrity:
  - Set component_state.last_seq referencing a compactable row
  - Run compaction
  - Verify last_seq updated to nearest retained seq
  - Verify no FK violation

test_compaction_is_atomic:
  - Simulate failure mid-compaction (mock transaction error)
  - Verify no entries lost (journal unchanged)

test_compaction_noop_on_small_journal:
  - Write 50 entries, run compaction
  - Verify nothing archived

test_replay_after_compaction_with_gap:
  - Compact away entries 1-500
  - Subscriber requests replay_from(0)
  - Verify earliest_available_seq in subscribe_ack
  - Verify client detects gap

test_compaction_crash_between_copy_and_delete:
  - Simulate crash after INSERT INTO archive but before DELETE
  - On recovery: verify no data loss, re-run compaction is idempotent

test_compaction_duration_under_threshold:
  - Write 50K entries, run compaction
  - Verify completes within 5s (metric gate, not just correctness)
```

---

## Full Phase 2A Test Matrix

| Section | Test Count | Gate? |
|---------|-----------|-------|
| Recovery protocol | ~8 tests | Must pass before keepalive |
| UDS keepalive | ~4 tests | Must pass before reconnect gate |
| Reconnect + replay | 3 tests | **HARD GATE** — blocks compaction |
| Journal compaction | 8 tests | Must pass before Phase 2B gate |
| **Fault-injection suite** | ~4 tests | **HARD GATE** — blocks Phase 2B |

**Fault-injection suite (final gate):**
```
test_kill_leader_recovery: kill supervisor mid-operation, restart, verify state reconciled
test_drop_subscriber_replay: subscriber dies, events emitted, reconnect replays correctly
test_stalled_component_detection: component stops responding, keepalive detects, marks LOST
test_journal_replay_after_crash: write entries, crash, restart, verify full replay consistency
```

**Estimated total: ~27 new tests across Phase 2A**

---

## Future: Phase 2B (Capabilities)

Not in scope for this design. Unlocked only after all Phase 2A gates pass:
- Cross-repo backpressure protocol
- Saturation signaling
- Adaptive capacity management

Phase 2B design will be a separate document.
