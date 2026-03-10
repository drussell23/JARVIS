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
Fix required: Write a `APPLYING` sentinel to ledger BEFORE writing the file. On reconcile, treat APPLYING as "verify current file state."

**7. Cooldown bypass via op_id rotation**
A buggy sensor that generates new op_ids for the same file on every poll cycle bypasses the per-file cooldown (which checks `_file_touch_cache` by file path). The cooldown guard correctly uses file path as key — but only within the same GLS process lifetime. On restart, the cache resets.
Fix required: Persist cooldown cache to ledger storage so restarts don't reset the guard.

**8. ChangeEngine sandbox path collision**
Under high concurrency, two ops targeting the same file write to `sandbox/{op_id}/file.py`. The op_ids are unique, so no collision. But if the sandbox root is on a tmpfs that fills up, both silently fail with OSError. The error is caught and treated as change_engine_failed (correct), but no disk-space alert is emitted.
Fix required: Add disk space pre-check in ChangeEngine.execute() before sandbox write.

### P2 — Observability (fix before production handoff)

**9. No structured telemetry export**
All telemetry is currently logged to file via OpsLogger. There is no export to Prometheus/Datadog/Cloud Monitoring. Operating the system blind in production.
Fix required: OpsLogger structured export to Cloud Monitoring or Prometheus push gateway.

**10. No canary slice graduation visibility**
TrustGraduator.promote() silently advances tiers. There is no dashboard or voice notification when a slice graduates from OBSERVE to GOVERNED.
Fix required: Emit CommProtocol DECISION message on tier promotion. Log to ledger.

---

## Practical Definition of "Production Ready"

Ouroboros is production-ready when ALL of the following are true:

- [ ] 100 operations executed across at least 2 repos with zero silent failures
- [ ] 3 consecutive failed operations correctly roll back and narrate without human intervention
- [ ] Split-brain dedup verified under 5 concurrent operations on the same file
- [ ] Saga cross-repo apply tested with deliberate second-repo failure and clean compensation
- [ ] Narration-order inversion tested: two concurrent ops completing within 60s, both narrated
- [ ] Process kill during APPLY → restart → reconcile → correct state (no double-apply)
- [ ] TrustGraduator promotion race tested under concurrent gate checks
- [ ] 24-hour unattended run on docs/ + tests/ slices with zero intervention
- [ ] MTTR (mean time to recovery from a bad patch) < 60 seconds

---

## Phase Gate

**Phase 3 → Phase 4 gate:** First successful Go/No-Go ignition operation (single op, docs/ target, COMPLETE terminal state, all 6 checklist items verified).

**Phase 4 → Production gate:** All 9 checklist items above checked off.

---

*Logged: 2026-03-10. Authors: Claude Code + Ouroboros Architecture Review.*
