# Phase 2C: Loop Activation — Autonomous Trigger System Design

> **Status:** Approved for implementation
> **Date:** 2026-03-08
> **Supersedes:** Phase 2B design (code generation with file context)

---

## 1. Goal

Wire four autonomous trigger sensors (task backlog, test failure, voice command, AI opportunity miner) into a single **Unified Intake Router** that feeds `GovernedLoopService.submit()`. Add real-time phase-transition voice narration so the user hears JARVIS developing itself as it happens — a "Claude Code, spoken live" experience.

**Phase 2B left us with:** a fully functional `CLASSIFY → GENERATE → VALIDATE → GATE → APPLY → VERIFY → COMPLETE` pipeline reachable via `GovernedLoopService.submit()`.
**Phase 2C adds:** the autonomous front-end that decides *when* and *what* to submit.

---

## 2. Three-Plane Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  INTAKE PLANE                                               │
│                                                             │
│  SensorA(backlog) ──┐                                       │
│  SensorB(tests)  ──┤──► UnifiedIntakeRouter ──► WAL Queue  │
│  SensorC(voice)  ──┤       (normalize → dedup → priority   │
│  SensorD(miner)  ──┘        → rate gate → enqueue)         │
└──────────────────────────────┬──────────────────────────────┘
                               │ IntentEnvelope (at-least-once)
┌──────────────────────────────▼──────────────────────────────┐
│  EXECUTION PLANE                                            │
│                                                             │
│  GovernedLoopService.submit()                               │
│    └─► GovernedOrchestrator (full governed FSM)             │
│          CLASSIFY → GENERATE[_RETRY] → VALIDATE[_RETRY]     │
│          → GATE → APPLY → VERIFY → COMPLETE                 │
│          → CANCELLED | EXPIRED | POSTMORTEM                 │
└──────────────────────────────┬──────────────────────────────┘
                               │ PhaseEvent (every FSM transition)
┌──────────────────────────────▼──────────────────────────────┐
│  COMMS PLANE                                                │
│                                                             │
│  PhaseEventEmitter → CommProtocol.HEARTBEAT enrichment      │
│    └─► VoiceNarrator (QoS: critical | phase | info)         │
│          └─► safe_say() → afplay (macOS HAL mixer)          │
└─────────────────────────────────────────────────────────────┘
```

**Hard constraint:** The execution plane is unchanged. Sensors and router are purely additive front-end; they must not embed orchestration logic.

---

## 3. Canonical Contract: IntentEnvelope

```python
@dataclass(frozen=True)
class IntentEnvelope:
    schema_version: str                  # always "2c.1"
    source: Literal["backlog", "test_failure", "voice_human", "ai_miner"]
    description: str
    target_files: Tuple[str, ...]
    repo: str
    confidence: float                    # 0.0–1.0
    urgency: Literal["critical", "high", "normal", "low"]
    dedup_key: str                       # deterministic; router uses for global dedup window
    causal_id: str                       # flows: sensor → intake → op_id → ledger → voice
    signal_id: str                       # unique per sensor firing
    idempotency_key: str                 # op-level; re-delivery of same key = no-op
    lease_id: str                        # WAL admission lease; router sets before enqueue
    evidence: Dict[str, Any]             # sensor-specific payload (e.g. failing test names)
    requires_human_ack: bool             # if True, router parks in PENDING_ACK; never auto-submits
    submitted_at: float                  # monotonic timestamp (time.monotonic()); UTC epoch in audit log
```

`schema_version = "2c.1"` is validated at router ingress. Unknown versions → dead-letter with `reason="schema_version_unsupported"`.

---

## 4. Sensor Adapters

### 4A — BacklogSensor
- Source: task backlog store (filesystem JSON / SQLite, configurable)
- Fires when: unclaimed task exists with `priority >= threshold` and no active op for its `target_files`
- Produces `urgency = "normal"` or `"high"` based on task age + priority
- `dedup_key = sha256(repo + sorted(target_files) + task_id)`
- Polling interval: configurable via `JARVIS_BACKLOG_POLL_S` (default 60s)
- No external API; reads local store only

### 4B — TestFailureSensor
- Source: pytest result files (`.pytest_cache/v/cache/lastfailed`, JUnit XML) or subprocess watch
- Fires when: new test failures detected vs previous scan snapshot
- Produces `urgency = "high"` for regressions (was passing → now failing)
- `dedup_key = sha256(repo + sorted(failed_test_ids))`
- Evidence: `{"failed_tests": [...], "first_seen_monotonic": ...}`
- Flap guard: same test set must fail in ≥2 consecutive scans before firing

### 4C — VoiceCommandSensor
- Source: existing voice intent pipeline (hooks into `unified_voice_orchestrator.py` intent dispatch)
- Fires when: recognized intent matches self-dev patterns (e.g., "fix the failing tests", "improve coverage for X")
- Produces `urgency = "critical"` (human said so)
- `requires_human_ack = False` (voice is already a human confirmation)
- STT misrecognition hardening:
  - Confidence gate: STT confidence < 0.82 → ask for confirmation before enqueuing
  - Keyword blocklist: reject envelopes whose `description` contains risky keywords without explicit confirm phrase
  - Rate guard: max 3 voice-triggered ops per hour (configurable via `JARVIS_VOICE_OP_RATE_LIMIT`)
  - `dedup_key = sha256(source + description + target_files_hash)` with 5-minute dedup window

### 4D — OpportunityMinerSensor (observe-only at launch)
- Source: static analysis tools (AST complexity, coverage gaps, TODO density, dead code scanners)
- Two-stage triage:
  1. Static tools scan → candidate files with `static_evidence_score ∈ [0,1]`
  2. LLM triage (Claude Haiku) → `llm_quality_score ∈ [0,1]`, blast radius, novelty penalty
- Confidence formula:
  ```
  confidence = (static_evidence_score × 0.5) + (llm_quality_score × 0.4)
               − risk_penalty − novelty_penalty
  ```
  Where `risk_penalty ∈ [0, 0.3]` (file importance, test coverage of file) and `novelty_penalty ∈ [0, 0.1]` (penalizes same file touched recently).
- Gate for auto-submit (Phase 2C.4): `confidence ≥ 0.65 AND blast_radius ≤ 3 AND within_daily_cap`
- Phase 2C.1–2C.3: sensor fires but router marks all D envelopes `requires_human_ack = True` → goes to `PENDING_ACK` queue; voice narration asks user to confirm
- Daily budget cap: `JARVIS_MINER_DAILY_BUDGET` (default 5 ops/day); tracked in ledger

---

## 5. Unified Intake Router

### 5.1 Pipeline Stages (sequential, in order)

```
ingress(envelope)
  → schema_validation()        # schema_version check; fail → dead-letter
  → normalize()                # canonical form, monotonic deadline stamp
  → global_dedup()             # dedup_key × source-specific window (voice: 5m, others: 10m)
  → priority_arbitration()     # token-bucket fairness; voice > test > backlog > miner
  → rate_and_budget_gate()     # per-source leaky bucket; per-day caps; backpressure signal
  → conflict_detection()       # same target_files in active/queued op → park or merge
  → human_ack_gate()           # requires_human_ack=True → PENDING_ACK; rest continue
  → wal_enqueue()              # fsync before ack; assign lease_id
  → dispatch_to_executor()     # GovernedLoopService.submit(ctx)
```

### 5.2 Priority + Fairness

Priority ordering: `voice_human > test_failure > backlog > ai_miner`

Token-bucket fairness prevents backlog/miner starvation:
- Each source gets a replenishment rate even when a higher-priority source is active
- `voice_human` always preempts via priority lane; never token-bucketed
- Configurable rates: `JARVIS_INTAKE_RATE_*` per source

### 5.3 WAL Admission Semantics

- Append-only log, one entry per envelope
- `fsync()` policy: always before acknowledging to sensor
- Replay order: FIFO within same priority level; monotonic `submitted_at` as tiebreak
- Compaction: on clean shutdown + configurable age (default: keep last 7 days)
- At-least-once delivery guarantee: router replays WAL on startup for any `lease_id` with no corresponding `op_id` in the ledger
- Idempotent execution: if `idempotency_key` already appears in ledger with terminal status → skip, emit `DUPLICATE_DISCARDED` event

### 5.4 Conflict Resolution

Same `target_files` conflict strategies (configurable per source combination):
- `COALESCE`: merge evidence, extend deadline, keep higher-urgency envelope (default for backlog+miner)
- `QUEUE_AFTER`: wait for active op to reach terminal state before dequeuing next (default for test_failure)
- `PREEMPT`: cancel active op, enqueue new one — only allowed for `voice_human` urgency=critical
- `REJECT_DUPLICATE`: drop newcomer, log `conflict_reason` (default for miner vs any)

### 5.5 Poison Intent Policy

- `MAX_RETRIES_PER_IDEMPOTENCY_KEY = 3` (configurable via `JARVIS_INTAKE_MAX_RETRIES`)
- After 3 terminal failures for same key → move to dead-letter queue, emit alert via VoiceNarrator (critical tier)
- Dead-letter entries: age out after 24h; queryable via admin API

### 5.6 Backpressure Contract

- Router exposes `intake_queue_depth()` metric
- When queue depth > `JARVIS_INTAKE_BACKPRESSURE_THRESHOLD` (default 10):
  - `backlog` and `ai_miner` sources receive `BackpressureSignal(retry_after_s=60)` → sensors back off
  - `test_failure` and `voice_human` sources: unaffected (bypass backpressure)

### 5.7 Leader Election / Split-Brain Guard

- Single-process deployment (local macOS): router holds a file-based advisory lock (`intake_router.lock`) via `fcntl.flock(LOCK_EX|LOCK_NB)`
- On startup: acquire lock or fail fast with `RouterAlreadyRunningError`
- On crash recovery: lock released by OS; next startup reacquires and replays WAL

---

## 6. Causal Chain

Every entity in the system carries a consistent keyset:

| Entity | Keys |
|--------|------|
| `IntentEnvelope` | `causal_id`, `signal_id`, `idempotency_key`, `lease_id` |
| `OperationContext` | `op_id` (= `causal_id`), `idempotency_key`, `lease_id` |
| Ledger entries | `op_id`, `causal_id`, `signal_id`, `idempotency_key`, `lease_id` |
| `PhaseEvent` | `op_id`, `causal_id`, `signal_id` |
| Voice narration | includes `op_id` in log; user hears summary |

`causal_id` is set by the sensor at signal origination and flows unchanged to the ledger and voice. This enables full trace reconstruction: voice narration → ledger entry → intake WAL → sensor signal.

---

## 7. Phase Event Emitter + Comms Plane

### 7.1 PhaseEvent Schema

```python
@dataclass(frozen=True)
class PhaseEvent:
    schema_version: str         # "2c.1"
    op_id: str
    causal_id: str
    signal_id: str
    phase: str                  # current FSM phase name
    prior_phase: str
    timestamp_monotonic: float  # time.monotonic()
    timestamp_utc: str          # ISO-8601 UTC; for audit/logging only
    metadata: Dict[str, Any]    # phase-specific data (e.g. failure_class, candidate_count)
```

`schema_version` is validated by the CommProtocol consumer. Unknown versions → log and skip.

### 7.2 FSM Transitions That Emit Events

Every transition emits a `PhaseEvent`. The full governed FSM:

```
QUEUED → CLASSIFY → GENERATE → [GENERATE_RETRY] → VALIDATE → [VALIDATE_RETRY]
       → GATE → APPLY → VERIFY → COMPLETE
                                → CANCELLED (test_failure, budget, security, source_drift, conflict)
                                → EXPIRED (monotonic deadline exceeded)
                                → POSTMORTEM (infra failure, unrecoverable error)
```

Stuck-op detector: if an op remains in any non-terminal phase for `> max_phase_duration_s` (configurable per phase), it is forcibly terminated → EXPIRED with `reason="stuck_op_detector"`, and a critical-tier voice alert fires.

### 7.3 VoiceNarrator QoS Tiers

| Tier | Trigger | Debounce | Behavior |
|------|---------|----------|----------|
| `critical` | Security block, poison intent, stuck-op, GATE approval needed, voice_human op starts | 0s | Bypass all coalescing; fire immediately |
| `phase_transition` | GENERATE start/end, VALIDATE start/end, APPLY start/end, COMPLETE, POSTMORTEM | 2s coalesce window | Merge rapid transitions into single utterance |
| `informational` | CLASSIFY, GENERATE_RETRY, VALIDATE_RETRY, QUEUED | 60s debounce | Only fire if no higher-tier event in window |

VoiceNarrator → `safe_say()` → `say -o tempfile` → `afplay tempfile` (native macOS, no GIL, no device contention).

### 7.4 Narration Examples

- **GENERATE start (phase_transition):** "Starting code generation for backend slash core slash foo dot py"
- **VALIDATE pass (phase_transition):** "Tests passing. Waiting for gate approval."
- **COMPLETE (phase_transition):** "Done. Applied fix to foo dot py. All tests green."
- **Security block (critical):** "Operation blocked. Restricted path detected. No changes made."
- **Poison intent (critical):** "Caution: three consecutive failures on the same target. Operation moved to dead letter. Please review."
- **Human ack needed (critical):** "Opportunity miner found a candidate in router dot py. Say 'yes' to proceed or 'skip' to dismiss."

---

## 8. Cross-Repo Semantics (Saga Pattern)

When `IntentEnvelope.repo` references a secondary repository:

- Use **Saga pattern** (compensating actions), not distributed atomic commit
- Each repo gets its own orchestrator run; a coordinator tracks saga state
- On partial failure: coordinator issues compensating transactions (e.g., revert applied patch in repo A if repo B validation fails)
- Saga log persisted to WAL alongside intake entries
- Phase 2C.1 implements single-repo only; saga coordinator is Phase 2C future work (flagged in code with `TODO(saga)`)

---

## 9. Deadline Model

- **Enforcement:** monotonic timestamps (`time.monotonic()`) — immune to clock adjustments, NTP slew, DST
- **Audit/logging:** UTC wall-clock (`datetime.now(timezone.utc)`) — human-readable, stored in ledger and PhaseEvent
- `OperationContext.pipeline_deadline` is a `datetime` (UTC) for interop with existing code; internally, all budget arithmetic uses `time.monotonic()` deltas
- Stuck-op detector uses monotonic timestamps only

---

## 10. SLO Definitions

| SLO | Target | Measurement |
|-----|--------|-------------|
| Intake latency (sensor fire → WAL enqueue) | p99 < 500ms | monotonic delta |
| Queue age (enqueue → submit()) | p95 < 30s | monotonic delta |
| Submit success rate | ≥ 95% (non-voice-human sources) | ledger outcomes / enqueue count |
| Rollback rate | ≤ 5% of APPLY completions | ledger `reason_code=rollback` / APPLY events |
| Narration lag (PhaseEvent emit → afplay start) | p95 < 2s | monotonic delta |
| Miner false-positive rate | ≤ 20% of auto-submits → CANCELLED/POSTMORTEM | ledger outcomes for source=ai_miner |

SLOs are logged to the ledger as `event="slo_sample"` entries for future dashboard integration.

---

## 11. Testing Strategy

### Unit tests (per component)
- `IntentEnvelope` construction, schema validation, dedup_key stability
- Each sensor adapter: correct envelope generation, flap guard, STT confidence gate (4C)
- Router pipeline stages: each in isolation (normalize, dedup, priority, conflict, ack gate, WAL)
- Poison intent policy: 3-strike threshold, dead-letter promotion
- `PhaseEvent` construction, schema_version validation
- VoiceNarrator QoS: debounce timing, coalescing window, critical bypass

### Integration tests
- A+B+C sensors → router → `submit()` mock (verifies causal chain keys flow end-to-end)
- D observe-only: fires but never reaches `submit()` without human ack
- Conflict detection: concurrent voice + backlog on same target_files → correct strategy applied
- Backpressure: queue depth > threshold → sensor back-off signal

### Crash-recovery tests (required)
- WAL replay after simulated crash mid-enqueue: duplicate delivery handled by idempotency_key check
- Router restart with in-flight op: no double-submit (idempotency_key already in ledger)
- Partial WAL write (truncated entry): corrupt entry skipped, alert emitted, subsequent entries replayed

### Out-of-order / duplicate event tests
- PhaseEvent delivered out of order to VoiceNarrator: no crash, debounce still correct
- Duplicate PhaseEvent (same op_id + phase): idempotent narration (fire once)
- Duplicate IntentEnvelope (same idempotency_key, op already terminal): `DUPLICATE_DISCARDED`

### Split-brain simulation
- Two router instances attempt lock acquisition: second fails with `RouterAlreadyRunningError`
- Stale lock from crashed process: new process acquires on restart

### Soak tests (Phase 2C.5+)
- 24-hour run: miner fires at full rate, voice ops interleaved; verify no memory leak, SLO drift, or stuck ops
- Monotonic counter exhaustion: not applicable (float precision > 100 years of ns resolution)

---

## 12. File Layout

```
backend/core/ouroboros/
  governance/
    intake/
      __init__.py
      intent_envelope.py          # IntentEnvelope dataclass + schema validation
      unified_intake_router.py    # router pipeline, WAL, priority, conflict, dedup
      wal.py                      # WAL append/replay/compaction
      sensors/
        __init__.py
        backlog_sensor.py         # 4A
        test_failure_sensor.py    # 4B
        voice_command_sensor.py   # 4C
        opportunity_miner_sensor.py # 4D
    comms/
      phase_event.py              # PhaseEvent dataclass
      phase_event_emitter.py      # emitter hooked into orchestrator FSM
      voice_narrator.py           # QoS tiers, debounce, coalesce, safe_say()

tests/governance/
  intake/
    test_intent_envelope.py
    test_unified_intake_router.py
    test_wal.py
    sensors/
      test_backlog_sensor.py
      test_test_failure_sensor.py
      test_voice_command_sensor.py
      test_opportunity_miner_sensor.py
  comms/
    test_phase_event.py
    test_voice_narrator.py
  integration/
    test_phase2c_acceptance.py
    test_crash_recovery.py
    test_out_of_order_events.py
    test_split_brain.py
```

---

## 13. Implementation Phases

### Phase 2C.1 — Core Intake (this plan)
- `IntentEnvelope` + schema validation
- `UnifiedIntakeRouter` (all pipeline stages)
- `WAL` (append, replay, compaction)
- Sensors A (backlog), B (test_failure), C (voice command)
- Sensor D: observe-only (fires but `requires_human_ack = True`)
- All unit + integration tests for above

### Phase 2C.2 — PhaseEvent Emitter
- `PhaseEvent` dataclass
- Emitter hooks in `GovernedOrchestrator` at every FSM transition
- `CommProtocol` HEARTBEAT enrichment with PhaseEvent payload

### Phase 2C.3 — VoiceNarrator
- `VoiceNarrator` with 3 QoS tiers
- `safe_say()` integration
- Narration scripts for all FSM transitions
- Comms integration tests

### Phase 2C.4 — D Auto-Submit
- Confidence formula + gate logic
- Daily budget cap + ledger tracking
- Remove `requires_human_ack = True` guard for high-confidence low-risk D envelopes
- Soak test baseline

### Phase 2C.5 — Tuning & Graduation
- Arbitration tuning from real data
- SLO dashboard entries
- Soak test pass at 24h
- Stuck-op detector refinement
