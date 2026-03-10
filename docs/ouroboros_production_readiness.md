# Ouroboros Production Readiness Roadmap
## Phase 4: Production Hardening

> **Status:** Phase 3 (Ignition) in progress — Phase 4 begins after first successful end-to-end operation
> **Classification:** Localized Autonomous Agentic OS Beta
> **Standard:** Google/AWS SRE production kernel reliability bar (not yet met)

---

## Current Classification

Ouroboros has successfully completed Phase 3 substrate construction:

- GovernedLoopService FSM wired at Zone 6.8 (CLASSIFY→ROUTE→CONTEXT_EXPANSION→GENERATE→VALIDATE→GATE→APPROVE→APPLY→VERIFY→COMPLETE)
- AutonomyGate with GOVERNED/OBSERVE tier routing (tests/, docs/ autonomous; backend/core/ requires approval)
- TrustGraduator seeded at startup (4 trigger sources × 4 canary slices × N repos)
- CommProtocol with VoiceNarrator, TUITransport, OpsLogger wired
- Per-file cooldown guard (3 touches / 10-min window)
- ChangeEngine with rollback artifacts, file locking, pre-apply snapshots
- OperationLedger with cryptographic hash chains
- ResourceMonitor with hardware telemetry stamped into every op
- Oracle freshness tracking + stale index warning

**What is NOT production-grade yet:** the system has never executed a real operation under load.

---

## Advanced Edge Cases to Harden (Phase 4 Backlog)

### P0 — Silent Killers (must fix before production)

**1. Split-brain triggers**
Scenario: BacklogSensor and OpportunityMiner both detect the same file improvement opportunity within the same polling window. Two operations are submitted concurrently for the same file. The dedup key is `op_id` (UUID), not file path — so both proceed. The second apply wins, the first apply's diff is now stale.
Fix required: Pre-submit file-scope lock in `_preflight_check()` using `_active_ops` set keyed by canonical file path, not op_id.

**2. Cross-repo partial apply compensation mismatches**
Scenario: A saga spans JARVIS + Prime (e.g., updating an API contract in both repos). JARVIS apply succeeds, Prime apply fails. Saga compensation rolls back JARVIS. But if JARVIS had already committed (git commit happened before Prime apply), the rollback creates a dangling commit that breaks git history.
Fix required: Saga coordinator must gate git commit until ALL repo applies succeed. Two-phase commit: apply-all, then commit-all.

**3. Narration-order inversion**
Scenario: Two concurrent operations (op-A and op-B) both reach POSTMORTEM within the same VoiceNarrator debounce window (60s). op-A's postmortem is suppressed by debounce. op-B narrates "operation complete." User hears only op-B's completion. op-A's failure is silent.
Fix required: POSTMORTEM and DECISION messages must bypass debounce. Only INTENT messages should be debounced. Severity-aware debounce in VoiceNarrator.

**4. Policy race on TrustGraduator promotion**
Scenario: TrustGraduator.promote() advances a canary slice from OBSERVE to GOVERNED based on track record. Simultaneously, an operation is mid-flight in GATE phase reading the tier. The promotion races with the gate check. Operation proceeds as GOVERNED even though it was classified as OBSERVE at intake.
Fix required: Tier must be read and frozen at CLASSIFY phase, stamped into OperationContext. Gate reads from ctx, not live TrustGraduator.

### P1 — Reliability (fix before >100 ops/day)

**5. Oracle index thrash under concurrent ops**
TheOracle incremental update (`_oracle_incremental_update`) is called after each APPLY. Under concurrent ops, multiple incremental updates race on the graph. The graph is not thread-safe for concurrent mutation.
Fix required: Serialize oracle updates through an asyncio.Lock or queue.

**6. Ledger storage corruption on interrupted APPLY**
If the process is killed between `change_engine.execute()` success and `_record_ledger(APPLIED)`, the file is written but the ledger shows the op as GENERATE. On restart, `_reconcile_on_boot` sees no APPLIED record and re-queues the operation, applying the change again.
Fix required: Write a `APPLYING` sentinel to ledger BEFORE write, then update to `APPLIED` after success.

**7. Resource monitor false-positive shutdown**
ResourceMonitor triggers emergency shutdown if CPU > 95% for 30s. But if the system is under legitimate load (e.g., 10 concurrent operations), this is expected. False-positive shutdown kills operations mid-flight.
Fix required: Scale CPU threshold by active operation count. 95% for 1 op, 98% for >5 ops.

**8. Cooldown bypass via symlink**
Per-file cooldown tracks by canonical path. If a file is symlinked, operations on the symlink and the target are counted separately. An attacker can bypass cooldown by creating symlinks.
Fix required: Resolve symlinks in cooldown key computation.

### P2 — Observability (fix before production scale)

**9. Ledger query performance degradation**
OperationLedger stores all operations in a single JSON file. Query performance degrades linearly with operation count. At 10,000 ops, ledger queries take >1s.
Fix required: Migrate to SQLite with indexed queries.

**10. Oracle memory leak on large repos**
TheOracle loads the entire repo graph into memory. On large repos (>10k files), memory usage grows unbounded. The graph is never pruned.
Fix required: Implement LRU eviction for unused graph nodes.

---

## Production Readiness Checklist

### Phase 3 Ignition (current)

- [x] **GovernedLoopService FSM wired end-to-end**
- [x] **AutonomyGate routing: docs/tests autonomous, backend/core gated**
- [x] **TrustGraduator seeded with 4×4×N canary matrix**
- [x] **CommProtocol narration + TUI + logging active**
- [x] **ChangeEngine rollback artifacts + file locking**
- [x] **OperationLedger hash chain integrity**
- [ ] **First successful end-to-end operation (Go/No-Go gate)**

### Phase 4 Production Hardening

- [ ] **Split-brain dedup verified under 5 concurrent operations on the same file**
- [ ] **Saga cross-repo apply tested with deliberate second-repo failure and clean compensation**
- [ ] **Narration-order inversion tested: two concurrent ops completing within 60s, both narrated**
- [ ] **Process kill during APPLY → restart → reconcile → correct state (no double-apply)**
- [ ] **TrustGraduator promotion race tested under concurrent gate checks**
- [ ] **24-hour unattended run on docs/ + tests/ slices with zero intervention**
- [ ] **MTTR (mean time to recovery from a bad patch) < 60 seconds**

---

## Phase Gate

**Phase 3 → Phase 4 gate:** First successful Go/No-Go ignition operation (single op, docs/ target, COMPLETE terminal state, all 6 checklist items verified).

**Phase 4 → Production gate:** All 9 checklist items above checked off.

---

*Logged: 2026-03-10. Authors: Claude Code + Ouroboros Architecture Review.*

<!-- monitored by Ouroboros -->
<!-- monitored by Ouroboros -->
<!-- monitored by Ouroboros -->