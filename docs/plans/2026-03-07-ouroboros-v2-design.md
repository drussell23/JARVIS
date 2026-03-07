# Ouroboros v2.0 — Self-Programming System with Tiered Governance

**Date:** 2026-03-07
**Status:** Approved
**Approach:** Parallel tracks (Track 1 plumbing gates Track 2 loop writes)

---

## 1. Architecture Overview

### Core Principles

1. **Supervisor owns everything** — Ouroboros start/stop/pause/resume is exclusively controlled by `unified_supervisor.py`. No alternate entry points.
2. **Cloud proposes, local disposes** — GCP/J-Prime (golden image, multi-model) generates candidates and plans. Local supervisor applies the risk engine and decides.
3. **Deterministic policy, not model trust** — Risk classification is rule-based. The LLM generates code; a rule engine decides if it's safe to apply.
4. **Communication is mandatory** — Every operation follows: Intent -> Plan -> Heartbeat -> Decision -> Postmortem.
5. **Sandbox until gates pass** — Track 2 (the loop) runs in read-only/sandbox mode until Track 1 (plumbing) gates are green.

### Intelligence Hierarchy

1. **PRIME_API (GCP golden image)** — Primary for heavy tasks (long-context codegen, cross-repo analysis, candidate generation). Multi-model on the VM.
2. **PRIME_LOCAL** — Fallback for lighter tasks or when GCP is unavailable.
3. **CLAUDE API** — Ultimate fallback, and preferred for tool-use tasks.

Task-type routing is deterministic (risk engine decides where code gets generated based on task complexity and resource availability).

### Risk Tiers

| Tier | Examples | Gate |
|------|----------|------|
| `SAFE_AUTO` | Single-file bug fix, test fix, lint cleanup, localized perf | Auto-apply after validation + hard invariants pass |
| `APPROVAL_REQUIRED` | Multi-file changes, API changes, dependency upgrades, architecture | Show diff + plan + rollback preview, wait for Derek's approval |
| `BLOCKED` | Security-impacting, cross-repo schema changes, supervisor self-modification | Blocked unless break-glass token issued |

### Degradation Modes

```
FULL_AUTONOMY       All tiers active, GCP available, all gates green
REDUCED_AUTONOMY    GCP unavailable -> safe_auto local only, heavy tasks queued
READ_ONLY_PLANNING  Gates failed or incident mode -> analyze + plan only, no writes
EMERGENCY_STOP      Manual kill or 3+ rollbacks in 1 hour -> all autonomy halted
```

---

## 2. Component Design

### 2.1 Operation Identity System

Every autonomous action gets a globally unique OperationID using UUIDv7 (monotonic-sortable, 128-bit, ms precision + random):

```
Format: op-<uuidv7>-<repo_origin>
Example: op-01905e6c-8a3b-7f2a-9d1e-4b7c3a2f1e0d-jarvis
```

Each operation persists:
- `op_id` — globally unique, propagated across all repos
- `policy_version` — which policy classified it
- `decision_inputs_hash` — for deterministic replay verification
- `model_metadata_hash` — which model/version generated candidates

Idempotency: if a consumer sees the same `op_id` twice, it skips.

### 2.2 Risk Engine (Deterministic Policy Classifier)

```
RiskEngine.classify(operation) -> RiskTier

Inputs (all deterministic, no LLM):
  - files_affected: List[Path]
  - change_type: create | modify | delete | rename
  - blast_radius: int (number of dependents from Oracle)
  - crosses_repo_boundary: bool
  - touches_security_surface: bool
  - touches_supervisor: bool
  - test_scope_confidence: float (0-1)
  - policy_version: str

Rules (first match wins, initial strict thresholds for first 30 days):
  1. touches_supervisor -> BLOCKED
  2. touches_security_surface -> BLOCKED
  3. crosses_repo_boundary -> APPROVAL_REQUIRED
  4. change_type == delete -> APPROVAL_REQUIRED
  5. dependency change -> APPROVAL_REQUIRED + lockfile diff
  6. create/delete/rename in core paths -> APPROVAL_REQUIRED
  7. blast_radius > 5 -> APPROVAL_REQUIRED
  8. files_affected > 2 -> APPROVAL_REQUIRED
  9. test_scope_confidence < 0.75 -> APPROVAL_REQUIRED
  10. Otherwise -> SAFE_AUTO

Hard invariants (must ALL pass regardless of tier, checked BEFORE gate):
  - contract_regression_delta == 0
  - security_risk_delta <= 0
  - behavioral_equivalence_score >= threshold (for refactors)
  - operator_load_delta <= 0 (no new noise/alerts)

Output: tier, reason_code, policy_version, auto_rollback_plan
```

**Break-glass flow (Phase 1A implementation, Phase 0A policy stub):**
1. Derek issues time-limited token: `jarvis break-glass --scope <op_id> --ttl 300`
2. Token stored with audit trail (who, when, why, scope)
3. Operation proceeds under APPROVAL_REQUIRED rules (not unguarded)
4. Token auto-expires after TTL
5. Postmortem auto-generated for any break-glass usage

### 2.3 Lock Hierarchy (Read/Write Lease Model)

```
Level 0: FILE_LOCK        (per-file, shared-read / exclusive-write)
Level 1: REPO_LOCK        (per-repo exclusive write)
Level 2: CROSS_REPO_TX    (multi-repo transaction envelope)
Level 3: POLICY_LOCK      (short-lived, around classification + gating)
Level 4: LEDGER_APPEND     (fencing token for exactly-once state transitions)
Level 5: BUILD_LOCK        (build gate)
Level 6: STAGING_LOCK      (staging apply)
Level 7: PROD_LOCK         (production apply)
```

Rules:
- Always acquire in ascending order (0 -> 7), never hold higher while acquiring lower
- Shared-read vs exclusive-write semantics per level
- All locks have TTL (FILE=60s, REPO=120s, CROSS_REPO=300s, DEPLOY=600s)
- Fencing tokens: monotonic, checked on every write operation
- Lock metadata: `fencing_token`, `owner_epoch`, `lease_id`, `renewal_count`
- Heartbeat every TTL/3 or lease forcibly released
- Monotonic timestamps only (not wall-clock)
- Fairness: no lock waiter exceeds max wait threshold under sustained contention

### 2.4 Transactional Change Engine

```
Pipeline (per operation):

1. PLAN        Generate change plan (files, diffs, test strategy)
               Stored in ledger as "planned" state

2. SANDBOX     Apply changes to git worktree (not working tree)
               Run validation suite against sandbox copy
               No production files touched

3. VALIDATE    AST parse (syntax valid?)
               Tests pass in sandbox?
               Blast radius within tier limits?
               Risk engine re-check with actual diff
               All hard invariants pass?

4. GATE        If safe_auto: proceed
               If approval_required: notify Derek, pause, wait
               If blocked: stop (unless break-glass active)

5. APPLY       Atomic write to production files
               Git commit with op_id in message
               Ledger updated to "applied" state

6. LEDGER      Commit to local append-only operation log FIRST
               Then publish event to cross-repo bus
               (outbox pattern: ledger is source of truth)

7. PUBLISH     Event published to Prime/Reactor if relevant
               Consumer deduplicates via op_id
               Inbox pattern: consumer acks after processing

8. VERIFY      Post-apply test run on production code
               If fails: automatic rollback + postmortem
```

Rollback is a pre-tested artifact (generated and validated alongside the plan, not "git revert and pray"). Rollback hash must match pre-change snapshot hash exactly.

### 2.5 Supervisor Survivability

```
Bootstrap Watchdog (launchd plist on macOS):
  - Separate lightweight process, ONLY responsibility: detect death, restart
  - No orchestration logic, no Ouroboros awareness
  - PID file monitoring
  - Max 3 restarts within 5 minutes, then safe-mode boot
  - Exponential cool-off between restarts
  - Safe-mode: no autonomy, interactive paths healthy, writes blocked 100%
```

### 2.6 Communication Protocol

Every operation emits 5 message types via pluggable transport:

```
INTENT:     op_id, seq, goal, target_files, risk_tier, blast_radius,
            estimated_scope, policy_version

PLAN:       op_id, seq, causal_parent_seq, steps[], validation_suite,
            rollback_strategy, rollback_preview (auto-generated undo diff)

HEARTBEAT:  op_id, seq, causal_parent_seq, phase, progress_pct,
            elapsed_time, current_action, blockers[]
            (emitted within 5s of phase transition, 99th percentile)

DECISION:   op_id, seq, outcome (applied|blocked|escalated|rolled_back),
            reason_code, diff_summary, test_results

POSTMORTEM: op_id, seq, root_cause, failed_phase, error_details,
            next_safe_action, learning_stored
```

Transport priority: TUI dashboard > log file > Slack webhook
Fault isolation: transport failure queues message, never blocks pipeline.

### 2.7 Hybrid Routing Policy

Deterministic routing matrix (multi-signal):

```
Task Type          | Normal        | CPU>80% 30s   | RAM>85%       | GCP Down
-------------------|---------------|---------------|---------------|----------
Single-file fix    | LOCAL         | LOCAL         | LOCAL(minimal)| LOCAL
Multi-file analysis| LOCAL         | GCP_PRIME     | GCP_PRIME     | QUEUE
Cross-repo planning| GCP_PRIME     | GCP_PRIME     | GCP_PRIME     | QUEUE
Candidate gen (3+) | GCP_PRIME     | GCP_PRIME     | GCP_PRIME     | QUEUE
Test execution     | LOCAL         | LOCAL         | LOCAL         | LOCAL
Blast radius calc  | LOCAL         | LOCAL         | LOCAL         | LOCAL
```

Additional pressure signals:
- Event loop latency > 40ms p95 -> shed background autonomy
- Disk IO saturated or test queue > 5 -> pause candidate generation
- GCP unavailable -> read-only planning mode for heavy tasks

Concurrency budgets (16GB Mac, hard caps):
- Interactive (voice/UI): 4GB reserved, HIGH priority, never preempted
- Ouroboros safe_auto: 2GB cap, LOW priority
- Ouroboros approval_req: 4GB cap, MEDIUM priority

Cost guardrails: daily/hourly GCP budget caps, alert on approach, fallback to local.

### 2.8 Goal Drift Guard

Ouroboros optimizes ONLY explicit objective functions:

```
1. test_pass_delta          Must be >= 0
2. coverage_delta           Must be >= 0
3. complexity_delta         Should decrease
4. latency_impact           Must not regress
5. error_rate_delta         Must decrease
6. contract_regression_delta Must be 0
7. security_risk_delta      Must be <= 0
8. behavioral_equivalence   Must meet threshold for refactors
9. operator_load_delta      Must be <= 0

"Improve generally" is NOT a valid goal.
Every request must map to at least one measurable objective.
No apply unless all hard invariants pass, regardless of weighted score.
```

### 2.9 Canary Mode

Before enabling full autonomy:
1. Enable for ONE domain slice (e.g., `backend/core/ouroboros/` only)
2. Meet all promotion criteria (see Phase 3 Go/No-Go)
3. Expand to next domain slice
4. Repeat until full codebase coverage

---

## 3. Phase Map — Parallel Tracks

```
TRACK 1 (Plumbing)                    TRACK 2 (Loop)
  RELEASE GATE                          SANDBOX UNTIL
                                        TRACK 1 PASSES

Phase 0A: Supervisor                  Phase 0B: Minimal
  authority + risk                      improvement loop
  engine + contracts                    in sandbox mode
        |                                     |
Phase 1A: Lock manager                Phase 1B: Communication
  + transactional engine                protocol + heartbeats
  + ledger + break-glass                + TUI integration
        |                                     |
Phase 2A: Hybrid routing              Phase 2B: Multi-file +
  + pressure-aware                      cross-repo (sandbox)
  + degradation modes                   + learning loop
        |                                     |
        +----------------+-------------------+
                         |
                   GATE CHECK
                   (all pass/fail
                    criteria green)
                         |
                   Phase 3: Governed
                     Autonomy (canary
                     -> full rollout)
```

---

## 4. Go/No-Go Acceptance Criteria

### Phase 0A — Supervisor Authority + Core Gates

**Scope:** Supervisor lifecycle ownership, OpID system, minimal risk engine (no break-glass yet), contract gate.

```
GO criteria (ALL must pass):
[x] Supervisor is sole Ouroboros lifecycle owner (no alternate entry paths)
[x] Watchdog restarts supervisor within 10s of death
[x] 3 crashes in 5min -> safe-mode boot (no autonomy, interactive works)
[x] Safe-mode write attempts blocked 100%, interactive paths healthy
[x] UUIDv7 op_ids: 10K concurrent -> zero collisions, chronological sort
[x] Replayed op_id -> idempotently skipped
[x] Risk engine classifies deterministically (same input 1000x -> same result)
[x] Decision reproducible from persisted inputs + policy_version across restarts
[x] Supervisor-touching change -> BLOCKED 100%
[x] All 4 hard invariants checked BEFORE tier gate
[x] Contract gate: incompatible schema at boot -> autonomy disabled, interactive works
[x] Flapping dependency (30s up/down oscillation) -> no unsafe writes
[x] No classification uses LLM output as input

NO-GO (any one blocks promotion):
[x] Any code path starts Ouroboros outside supervisor
[x] Any SAFE_AUTO classification violates a hard invariant
[x] Deterministic replay fails on ANY input
[x] Safe-mode allows any write operation
```

### Phase 0B — Sandbox Loop

**Scope:** End-to-end improvement loop in git worktree only. J-Prime (GCP golden image) as primary intelligence.

```
GO criteria (ALL must pass):
[x] Request -> analyze -> generate -> validate -> result (in worktree)
[x] Production files unchanged after 100 loop executions
[x] Worktree cleaned up after every operation (pass or fail)
[x] J-Prime on GCP used when available, graceful fallback to local/Claude
[x] LearningMemory consulted before generation (known solution -> skip)
[x] Failed approach stored -> not retried on same file+goal
[x] Learning memory persists across process restarts
[x] Genetic algorithm produces >= 2 distinct candidates per run

NO-GO:
[x] Any production file modified
[x] Learning memory lost on restart
[x] GCP fallback path hangs or crashes
```

### Phase 1A — Locks + Transactional Engine + Ledger

**Scope:** Read/write lease lock manager, transactional change engine, append-only ledger, break-glass implementation, outbox/inbox eventing.

```
GO criteria (ALL must pass):
[x] Lock acquisition out of order -> immediate error (not deadlock)
[x] Stale holder (2 missed heartbeats) -> lease forcibly released
[x] Write with expired fencing token -> rejected
[x] Concurrent shared-read same file -> both succeed
[x] Concurrent exclusive-write same file -> one waits, one proceeds
[x] No deadlock under 50 concurrent operations (sustained 60s)
[x] No lock waiter exceeds max wait threshold (fairness)
[x] Ledger entry exists for every state transition
[x] Event published ONLY after ledger commit succeeds
[x] Duplicate event burst (100x same op_id) -> exactly-once final state
[x] Out-of-order + delayed ack + consumer restart -> correct final state
[x] Rollback hash matches pre-change snapshot hash exactly
[x] Post-apply test failure -> automatic rollback within 30s
[x] Break-glass: token expires after TTL, audit trail complete
[x] Break-glass: operation proceeds under APPROVAL_REQUIRED (not unguarded)

NO-GO:
[x] Deadlock reachable under any tested access pattern
[x] Event published without ledger entry
[x] Rollback produces different hash than pre-change state
[x] Writer starvation observed under fairness test
[x] Replay storm produces duplicate state mutations
```

### Phase 1B — Communication Protocol

**Scope:** Structured messages, TUI integration, fault-isolated transport.

```
GO criteria (ALL must pass):
[x] Every operation emits all 5 message types in correct order
[x] Sequence numbers monotonic per op_id, causal parent links valid
[x] Heartbeat emitted within 5s of phase transition (99th percentile)
[x] TUI crash -> messages queue -> delivered when TUI recovers
[x] Transport failure -> pipeline continues unblocked
[x] Reason codes human-readable and machine-parseable
[x] POSTMORTEM includes root cause + next safe action

NO-GO:
[x] Pipeline blocked waiting for transport ack
[x] Missing message type in any completed operation
[x] Heartbeat delay > 30s (indicates pipeline stall)
```

### Phase 2A — Hybrid Routing + Degradation

**Scope:** Multi-signal routing, concurrency budgets, 4 degradation modes, cost guardrails.

```
GO criteria (ALL must pass):
[x] Voice command during heavy codegen -> < 200ms latency (unaffected)
[x] CPU spike -> heavy task routed to GCP within 5s
[x] Event loop latency > 40ms p95 -> background autonomy shed
[x] GCP unavailable -> heavy tasks queued, safe_auto continues local
[x] All 4 degradation modes reachable via test triggers
[x] FULL -> REDUCED -> READ_ONLY -> EMERGENCY_STOP transitions tested
[x] Recovery from EMERGENCY_STOP requires explicit re-enable
[x] GCP routing stays under configured daily budget cap
[x] Autonomy never causes interactive latency regression > 5%

NO-GO:
[x] Interactive path starved by autonomy workload
[x] Cost guardrail exceeded without alerting
[x] Degradation mode unreachable or stuck
```

### Phase 2B — Multi-File + Cross-Repo

**Scope:** Atomic multi-file ops, cross-repo event bus, blast radius integration, Reactor Core learning feedback.

```
GO criteria (ALL must pass):
[x] Multi-file change: all files updated atomically or all rolled back
[x] Cross-repo event: outbox commit -> inbox delivery -> consumer ack
[x] N/N-1 schema compatibility enforced at runtime (not just boot)
[x] Partial service tested: Prime up/Reactor down, vice versa, both flapping
[x] Blast radius from Oracle integrated into risk engine classification
[x] Learning feedback published to Reactor Core with op_id correlation

NO-GO:
[x] Partial multi-file apply (some files changed, others not)
[x] Cross-repo event lost or duplicated in final state
[x] Schema mismatch allows autonomous write
```

### Phase 3 — Governed Autonomy Rollout

**Scope:** Canary per domain slice, then full rollout.

```
CANARY PROMOTION CRITERIA (per domain slice, ALL must pass):
[x] >= 50 successful operations in slice
[x] 0 unrecoverable rollbacks (auto-rollback is acceptable)
[x] rollback_rate < 5% over trailing 50 operations
[x] p95 operation latency < 120s
[x] false_positive_approval_rate < 10%
[x] interactive path latency within 5% of baseline
[x] operator_load_delta <= 0 (no new alerts/noise)
[x] At least 1 induced fault scenario passed (GCP down, test flake, queue spike)
[x] Event replay test passed during canary window
[x] Budget adherence confirmed (within daily cap)
[x] No critical severity incidents for 72 hours
[x] Operator paging/noise below threshold

FULL ROLLOUT:
[x] All domain slices passing canary criteria
[x] Cross-slice operations tested
[x] 72 hours stable across all slices
[x] Derek signs off on autonomy scope
```

---

## 5. Advanced Failure Modes Explicitly Modeled

- **Supervisor death spiral:** watchdog with exponential cool-off + safe-mode boot
- **Re-entrant lifecycle collisions:** supervisor serializes start/stop/pause via state machine
- **Event disorder:** inbox deduplication via op_id, outbox ordering via monotonic seq
- **Cross-repo split-brain:** N/N-1 contract enforcement at boot + runtime
- **Deadlock hierarchy:** strict ascending acquisition order, runtime assertion
- **Backpressure collapse:** hard concurrency budgets, interactive path reservation
- **State authority drift:** single ledger per repo, cross-repo coordination via events only
- **Long-uptime degradation:** periodic health self-check, metrics on async task count
- **Policy bypass paths:** all component calls routed through supervisor risk engine
- **Model drift hazards:** policy is rule-based, never trust-based; model metadata logged
- **Prompt/goal injection:** input sanitization on improvement goals, sandboxed execution
- **Resource contention:** QoS partitioning (voice/UI 4GB reserved, autonomy capped)
- **Replay storms after restart:** idempotency keys on all events, consumer dedup

---

## 6. Key Files (Current Codebase)

| Component | Primary File(s) |
|-----------|----------------|
| Supervisor (control plane) | `unified_supervisor.py` |
| Ouroboros Engine | `backend/core/ouroboros/engine.py` |
| Ouroboros Integration | `backend/core/ouroboros/integration.py` |
| Native Self-Improvement | `backend/core/ouroboros/native_integration.py` |
| Trinity Integration | `backend/core/ouroboros/trinity_integration.py` |
| Cross-Repo Coordination | `backend/core/ouroboros/cross_repo.py` |
| Advanced Orchestrator | `backend/core/ouroboros/advanced_orchestrator.py` |
| Code Analysis (AST) | `backend/core/ouroboros/analyzer.py` |
| Rollback Protection | `backend/core/ouroboros/protector.py` |
| Validation | `backend/core/ouroboros/validator.py` |
| Genetic Algorithm | `backend/core/ouroboros/genetic.py` |
| Oracle (GraphRAG) | `backend/core/ouroboros/oracle.py` |
| Brain Orchestrator | `backend/core/ouroboros/brain_orchestrator.py` |
| UI Integration | `backend/core/ouroboros/ui_integration.py` |
| Prime Router | `backend/core/prime_router.py` |
| Prime Client | `backend/core/prime_client.py` |
| GCP VM Manager | `backend/core/gcp_vm_manager.py` |
| Model Serving | `backend/intelligence/unified_model_serving.py` |
| Coding Council | `backend/core/coding_council/` |
| DLM | `backend/core/distributed_lock_manager.py` |
