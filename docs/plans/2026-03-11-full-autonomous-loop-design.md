# Full Autonomous Loop — C+ Layered Architecture Design

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Close all feedback loops in the Ouroboros governance pipeline so the system operates autonomously with deterministic safety guarantees.

**Architecture:** Approach C+ — layered by authority boundaries. Layer 1 (GLS + Intake) is the ONLY execution authority. Layers 2/3/4 are advisory/request-driven only. No layer may bypass the GLS state machine.

**Tech Stack:** Python 3.9+, asyncio, existing governance FSM, CommProtocol EventBridge, JSON file IPC for reactor.

---

## Deliverable 1: Interface Contract

### Authority Boundaries

```
Layer 1 — Execution Authority (GLS + Intake)
  SOLE writer of: operation state, file system, ledger, trust tiers
  Owns: submit(), apply(), rollback(), promote(), demote()

Layer 2 — Decision Intelligence (AutonomyFeedbackEngine)
  ADVISORY ONLY — emits recommendations via typed commands
  Owns: curriculum consumption, model attribution scoring, reactor feedback, brain routing hints

Layer 3 — Safety & Reliability (ProductionSafetyNet)
  ADVISORY ONLY — emits alerts and mode-switch requests
  Owns: health escalation, rollback analysis, incident detection, human presence signals

Layer 4 — Advanced Coordination (AdvancedAutonomyService)
  ADVISORY ONLY — emits saga orchestration requests and consensus results
  Owns: cross-repo saga state, consensus voting, dynamic tier recommendations
```

### Ownership Matrix

| Responsibility | Owner Layer | Execution via | Advisory from |
|---|---|---|---|
| Operation state transitions | L1 (GLS) | `submit()` | L2, L3 |
| File system mutations | L1 (GLS) | `change_engine.apply()` | — |
| Ledger writes | L1 (GLS) | `ledger.record()` | — |
| Trust tier changes | L1 (GLS) | `graduator.promote/demote()` | L2, L3, L4 |
| Degradation mode switch | L1 (GLS) | `degradation.evaluate()` | L3 |
| Backlog work generation | L1 (Intake) | `backlog_sensor.ingest()` | L2 |
| Model attribution scores | L2 | `attribution_recorder.score()` | — |
| Curriculum analysis | L2 | `curriculum_consumer.analyze()` | — |
| Brain routing hints | L2 | command → L1 brain_selector | — |
| Health alerts | L3 | command → L1 degradation | — |
| Rollback root cause | L3 | command → L2 attribution | — |
| Incident detection | L3 | command → L1 degradation | — |
| Human presence signal | L3 | command → L1 submit gate | — |
| Saga coordination | L4 | command → L1 submit | — |
| Consensus voting | L4 | command → L1 candidate_generator | — |
| Dynamic tier override | L4 | command → L1 graduator | — |

### Command Schemas (directed, versioned, idempotent)

All commands carry:
```python
@dataclass(frozen=True)
class CommandEnvelope:
    command_id: str          # UUIDv7, idempotency key
    schema_version: str      # "1.0.0"
    source_layer: int        # 2, 3, or 4
    target_layer: int        # always 1
    command_type: str        # enum below
    payload: Dict[str, Any]
    issued_at_ns: int        # monotonic nanoseconds
    ttl_s: float             # max age before stale (default 300s)
    idempotency_key: str     # deterministic hash of (command_type + canonical payload)
```

Command types:

| Command | Source | Target | Payload | Idempotency |
|---|---|---|---|---|
| `generate_backlog_entry` | L2 | L1 Intake | `{description, target_files, priority, repo, source_curriculum_id}` | hash(curriculum_id + file_set) |
| `adjust_brain_hint` | L2 | L1 BrainSelector | `{brain_id, canary_slice, weight_delta, evidence_window_ops}` | hash(brain_id + slice + window) |
| `request_mode_switch` | L3 | L1 Degradation | `{target_mode, reason, evidence_count, probe_failure_streak}` | hash(target_mode + reason) |
| `report_rollback_cause` | L3 | L2 Attribution | `{op_id, root_cause_class, affected_files, model_used}` | hash(op_id) |
| `signal_human_presence` | L3 | L1 Submit Gate | `{is_active, activity_type, defer_until_ns}` | hash(activity_type + defer_until) |
| `request_saga_submit` | L4 | L1 GLS | `{saga_id, repo_patches: Dict[repo, patch], idempotency_key}` | saga_id |
| `report_consensus` | L4 | L1 CandidateGen | `{op_id, candidates: List, votes: Dict[brain_id, verdict], majority}` | hash(op_id) |
| `recommend_tier_change` | L4 | L1 Graduator | `{trigger_source, repo, canary_slice, recommended_tier, evidence}` | hash(trigger + repo + slice) |

### Event Schemas (append-only facts, no side effects)

All events carry:
```python
@dataclass(frozen=True)
class EventEnvelope:
    event_id: str           # UUIDv7
    schema_version: str     # "1.0.0"
    source_layer: int       # 1, 2, 3, or 4
    event_type: str         # enum below
    payload: Dict[str, Any]
    emitted_at_ns: int      # monotonic nanoseconds
    op_id: Optional[str]    # operation context if applicable
```

Event types:

| Event | Source | Consumers | Payload |
|---|---|---|---|
| `op_completed` | L1 | L2, L3 | `{op_id, brain_id, model_name, terminal_phase, provider, duration_s, rollback: bool}` |
| `op_rolled_back` | L1 | L2, L3 | `{op_id, brain_id, rollback_reason, affected_files, phase_at_failure}` |
| `trust_tier_changed` | L1 | L2, L4 | `{trigger_source, repo, canary_slice, old_tier, new_tier}` |
| `degradation_mode_changed` | L1 | L3 | `{old_mode, new_mode, trigger_reason}` |
| `health_probe_result` | L1 | L3 | `{provider, success: bool, latency_ms, consecutive_failures}` |
| `curriculum_published` | L2 | L1 Intake | `{curriculum_id, top_k_task_types, publish_ts}` |
| `attribution_scored` | L2 | L3, L4 | `{brain_id, canary_slice, success_rate, sample_size, window_hours}` |
| `rollback_analyzed` | L3 | L2 | `{op_id, root_cause_class, pattern_match: bool, similar_op_ids}` |
| `incident_detected` | L3 | L1 | `{severity, reason, recommended_mode, evidence}` |
| `saga_state_changed` | L4 | L1 | `{saga_id, phase, repos_applied, repos_pending, repos_failed}` |

### Failure Precedence (deterministic)

```
Priority 0 (highest): Safety fault from L3 (incident_detected, rollback cascade)
Priority 1:           Execution timeout from L1 (pipeline_timeout_s exceeded)
Priority 2:           Optimization request from L2 (brain hint, backlog entry)
Priority 3 (lowest):  Learning hint from L2/L4 (attribution score, tier recommendation)
```

When commands conflict at the same priority level, the command with the earlier `issued_at_ns` wins. Stale commands (age > ttl_s) are silently dropped and logged.

### Schema Validation at Layer Boundaries

Every command/event crossing a layer boundary MUST:
1. Pass `contract_gate.validate(envelope.schema_version)` for N/N-1 compatibility
2. Have a non-expired TTL
3. Have a valid UUIDv7 command_id/event_id
4. Be deduplicated by idempotency_key (L1 maintains a bounded LRU of seen keys, default 10,000)

---

## Deliverable 2: Phased Implementation Plan

### Reuse Matrix

| C+ Responsibility | Existing Module(s) | Action | Rationale |
|---|---|---|---|
| **L2: Curriculum → Work Gen** | `curriculum_publisher.py` (ACTIVE), `backlog_sensor.py` (ACTIVE) | **ADAPT** curriculum_publisher + new consumer adapter | Publisher exists, need consumer that writes to backlog.json |
| **L2: Model Attribution Loop** | `model_attribution_recorder.py` (ACTIVE), `learning_bridge.py` (REUSABLE) | **WIRE** learning_bridge into GLS postmortem, add periodic scoring loop | Both modules exist, just need connection |
| **L2: Reactor → Backlog** | GLS `_reactor_event_loop` (ACTIVE), `reactor_core_integration.py` (ACTIVE) | **ADAPT** reactor event handler to write backlog entries | Event loop exists, needs output adapter |
| **L2: Canary → Brain Feedback** | `canary_controller.py` (ACTIVE), `model_attribution_recorder.py` (ACTIVE) | **WIRE** canary outcomes into attribution → brain_selector reload | Both exist, need event path |
| **L3: Health Escalation** | GLS `_health_probe_loop` (ACTIVE), `comm_protocol.py` (ACTIVE), `degradation.py` (ACTIVE) | **ADAPT** health_probe_loop with escalation callback | Loop exists, need escalation logic |
| **L3: Rollback Root Cause** | `change_engine.py` (ACTIVE), `learning_bridge.py` (REUSABLE), `error_recovery.py` (REUSABLE) | **ADAPT** error_recovery classification + new analysis adapter | Reuse error_recovery enums, add pattern matching |
| **L3: Incident Auto-Trigger** | `degradation.py` (ACTIVE), `break_glass.py` (ACTIVE) | **ADAPT** degradation.evaluate() with programmatic trigger | FSM exists, need trigger input |
| **L3: Human Presence** | `intervention_decision_engine.py` (REUSABLE), `autonomy/tiers.py` CognitiveLoad (ACTIVE) | **ADAPT** intervention_decision_engine with calendar integration | Concepts exist, needs rewiring for new import paths |
| **L4: Cross-Repo Saga** | `saga/` (ACTIVE: saga_types, saga_apply_strategy, cross_repo_verifier) | **ENHANCE** with idempotency keys + saga state persistence | Full saga module exists, needs persistence |
| **L4: Consensus Validation** | `shadow_harness.py` (REUSABLE), `candidate_generator.py` (ACTIVE) | **ADAPT** shadow_harness for parallel brain execution + voting | Shadow harness has dry-run + comparator, needs voting |
| **L4: Dynamic Tier Override** | `autonomy/graduator.py` (ACTIVE), `autonomy/gate.py` (ACTIVE) | **WIRE** check_graduation() call in orchestrator VALIDATE phase | Both exist, need in-flight check |

### New Modules Required

Only 3 new files needed (everything else reuses/adapts existing):

| New File | Why Existing Cannot Satisfy | Responsibility |
|---|---|---|
| `backend/core/ouroboros/governance/feedback_engine.py` | No module currently consumes curriculum output or runs periodic attribution scoring. curriculum_publisher only publishes; model_attribution_recorder only records transitions on explicit call. Need an async service that hosts the consumption + scoring loops. | L2 service host |
| `backend/core/ouroboros/governance/safety_net.py` | No module currently aggregates health probe failures into escalation decisions, performs rollback pattern analysis, or detects human presence. degradation.py is a pure state machine with no sensing. error_recovery.py has classification but no governance integration. | L3 service host |
| `backend/core/ouroboros/governance/advanced_coordination.py` | No module currently hosts saga state persistence, consensus voting, or dynamic tier recommendation. saga/ has apply strategy but no durable state or orchestration loop. shadow_harness has comparison but no multi-brain voting protocol. | L4 service host |

### Phase P0 — Feedback Loops (items 1-4)

**Files to modify:**
- `governed_loop_service.py` — wire L2 commands into submit gate, add event emission
- `learning_bridge.py` — wire into GLS postmortem flow
- `curriculum_publisher.py` — add consumer adapter method
- `model_attribution_recorder.py` — add periodic scoring loop
- `brain_selector.py` — accept brain hints from L2

**Files to create:**
- `feedback_engine.py` — L2 AutonomyFeedbackEngine service

**Acceptance gates:**
- [ ] Curriculum signal creates backlog entry within 60s of publication
- [ ] Model attribution scores update every `attribution_interval_s` (default 1800s)
- [ ] Reactor model_promoted event creates backlog entry
- [ ] Brain hint from L2 adjusts next brain selection
- [ ] All commands idempotent (duplicate command_id is no-op)
- [ ] L2 crash + restart replays from last checkpoint without duplicate work
- [ ] 100% test coverage on feedback_engine.py

### Phase P1 — Safety & Reliability (items 5-8)

**Files to modify:**
- `governed_loop_service.py` — wire L3 commands into health probe, submit gate
- `degradation.py` — accept programmatic mode switch commands
- `change_engine.py` — emit rollback event with cause metadata

**Files to adapt:**
- `error_recovery.py` — extract ErrorCategory/ErrorSeverity for rollback classification
- `intervention_decision_engine.py` — rewire imports, integrate with GLS

**Files to create:**
- `safety_net.py` — L3 ProductionSafetyNet service

**Acceptance gates:**
- [ ] 3 consecutive health probe failures → L3 emits `request_mode_switch(REDUCED_AUTONOMY)`
- [ ] 5 consecutive → `request_mode_switch(READ_ONLY_PLANNING)`
- [ ] Rollback triggers root cause analysis within 5s
- [ ] Same root cause class 2x in 1 hour → auto-block similar ops
- [ ] Human presence signal defers non-critical ops
- [ ] L3 crash does not affect L1 operation (fail-open for safety advisory)
- [ ] All safety commands have highest priority (preempt L2/L4)

### Phase P2 — Advanced Coordination (items 9-11)

**Files to modify:**
- `saga/saga_apply_strategy.py` — add idempotency key + state persistence
- `saga/saga_types.py` — add SagaState persistence schema
- `orchestrator.py` — add in-flight graduation check at VALIDATE phase
- `candidate_generator.py` — accept consensus results

**Files to adapt:**
- `shadow_harness.py` — wire for parallel brain execution + voting protocol

**Files to create:**
- `advanced_coordination.py` — L4 AdvancedAutonomyService

**Acceptance gates:**
- [ ] Cross-repo saga persists state; partial failure → deterministic replay
- [ ] Saga idempotency key prevents duplicate apply across restart
- [ ] Consensus voting: 2-of-3 brains agree → apply; otherwise → BLOCKED
- [ ] Dynamic tier: in-flight promotion logged in ledger with evidence
- [ ] Consensus deadlock (no majority) → escalate to human, don't hang
- [ ] L4 crash does not affect L1/L2/L3 (full isolation)

### Restart/Crash Semantics

| Layer | Crash behavior | Recovery |
|---|---|---|
| L1 (GLS) | Active op rolled back via change_engine snapshot | WAL replay for intake, ledger recovery for ops |
| L2 (FeedbackEngine) | Last-processed curriculum_id persisted to JSON | Skip already-processed curriculum signals on restart |
| L3 (SafetyNet) | Stateless advisory; health probe state reconstructed from last N probe results | No state to recover; probes resume immediately |
| L4 (AdvancedCoord) | Saga state persisted to JSON with version + checksum | Replay from last committed saga phase; idempotency keys prevent re-apply |

### Replay/Idempotency Behavior

- **Commands**: Deduplicated by `idempotency_key` in bounded LRU (10,000 entries). Replay of same key → logged + dropped.
- **Events**: Append-only. Consumers maintain `last_processed_event_id` cursor. On restart, replay from cursor.
- **Saga operations**: Each repo-apply carries a saga-scoped idempotency key. `change_engine.apply()` checks if file already matches target content before writing.

### Per-Layer Timeout Budgets

| Layer | Operation | Timeout | Backpressure |
|---|---|---|---|
| L1 | Full pipeline (submit → complete) | `pipeline_timeout_s` (default 300s) | max_concurrent_ops (default 3) |
| L1 | Single provider call | `provider_timeout_s` (default 240s) | — |
| L2 | Curriculum consumption | 30s per entry | Bounded queue (100 entries) |
| L2 | Attribution scoring | 60s per brain | Sequential, no queue |
| L3 | Rollback analysis | 10s per rollback | Bounded queue (50 entries) |
| L3 | Human presence check | 5s | Single-shot, no queue |
| L4 | Saga orchestration | `saga_timeout_s` (default 600s) | max_concurrent_sagas (default 1) |
| L4 | Consensus voting | 120s per brain × num_brains | Sequential voting |

### Test Matrix

| Test Scenario | Layer(s) | What to verify |
|---|---|---|
| Re-entrant escalation | L3 → L1 | Two concurrent incident_detected commands → only one mode switch |
| Advisory loop cycle | L2 → L1 → L2 | curriculum → backlog → op_completed → attribution → curriculum (no infinite loop) |
| Consensus deadlock | L4 | 3 brains, all disagree → timeout → escalate, no hang |
| Partial saga replay | L4 → L1 | Saga applied to repo A, crash, restart → repo A skipped, repo B applied |
| Stale clock | All | Command with TTL=10s arrives after 15s → silently dropped |
| Event duplication | All | Same event_id delivered twice → consumer processes once |
| Out-of-order delivery | L2 | curriculum_id=5 arrives before curriculum_id=4 → both processed, no skip |
| L2 crash during scoring | L2 | Attribution loop interrupted → restart resumes from last scored brain |
| L3 crash during analysis | L3 | Rollback analysis interrupted → L1 continues unaffected (fail-open) |
| L4 crash mid-saga | L4 | Saga state persisted → restart replays from last phase |
| Single-writer invariant | L1 | Verify no L2/L3/L4 code path directly mutates op_context, ledger, or filesystem |
| Command flooding | All | 1000 commands/s from L2 → bounded queue, backpressure, no OOM |
| Safety preemption | L3 over L2 | L2 brain_hint + L3 incident_detected arrive simultaneously → L3 processed first |

---

## Deliverable 3: Autonomy File Audit Report

### High Confidence Unused (legacy, zero production imports)

| File | Description | Static Imports | Dynamic Refs | Tests | Verdict |
|---|---|---|---|---|---|
| `backend/core/ouroboros/governance/sandbox_loop.py` | Pre-GLS sandbox execution loop | Only `_deprecated_run_supervisor.py` | None | `test_sandbox_loop.py`, `test_phase0_integration.py` | **LEGACY** — superseded by GovernedLoopService |
| `backend/core/ouroboros/advanced_orchestrator.py` | Pre-governance advanced orchestrator | Only `_deprecated_run_supervisor.py`, `bin/jarvis-improve` | None | None | **LEGACY** — superseded by governance/orchestrator.py |
| `backend/core/ouroboros/engine.py` | Original self-improvement engine | Only `_deprecated_run_supervisor.py`, `bin/jarvis-improve` | None | None | **LEGACY** — superseded by governance pipeline |
| `backend/core/ouroboros/brain_orchestrator.py` | Pre-governance brain management | Only `_deprecated_run_supervisor.py`, `scripts/verify_trinity_life.py` | None | None | **LEGACY** — superseded by brain_selector.py |
| `backend/core/ouroboros/neural_mesh.py` | Neural mesh orchestration | Only `_deprecated_run_supervisor.py`, `scripts/verify_trinity_life.py` | None | None | **LEGACY** |
| `backend/core/ouroboros/genetic.py` | Genetic algorithm engine | No production imports found | None | None | **LEGACY** |
| `backend/core/ouroboros/simulator.py` | Self-improvement simulator | No production imports found | None | None | **LEGACY** |
| `backend/core/ouroboros/scalability.py` | Scalability utilities | No production imports found | None | None | **LEGACY** |
| `backend/core/ouroboros/protector.py` | Safety protector | No production imports found | None | None | **LEGACY** |
| `backend/core/ouroboros/test_dummy.py` | Test placeholder | No production imports found | None | None | **LEGACY** |
| `backend/autonomy/error_recovery.py` | Generic error classification | No imports from outside itself | None | None | **REUSABLE** for L3 (adapt for rollback analysis) |
| `backend/autonomy/error_recovery_orchestrator.py` | Adaptive retry orchestrator | No imports from outside itself | None | None | **REUSABLE** for L3 |
| `backend/autonomy/monitoring_metrics.py` | Generic metrics collection | No imports from outside itself | None | None | **REUSABLE** for L3 (supplements resource_monitor) |
| `backend/autonomy/system_states.py` | Generic state machine | No imports from outside itself | None | None | **LEGACY** — superseded by preemption_fsm.py |
| `backend/autonomy/predictive_intelligence.py` | sklearn-based predictions | No imports from outside itself | None | None | **NOT REUSABLE** — heavy deps, wrong domain |

### Medium Confidence Unused (imported but underutilized)

| File | Description | Production Imports | Notes |
|---|---|---|---|
| `governance/shadow_harness.py` | Shadow execution + comparator | `__init__.py` re-export only; GovernanceStack has `shadow_harness: Optional = None` slot, never populated | **REUSABLE** for L4 consensus |
| `governance/telemetry_contextualizer.py` | Host-binding for remote routes | Referenced in docstrings only, not imported | **REUSABLE** for L1 split-brain safety |
| `governance/event_bridge.py` | Governance → cross-repo event mapping | `integration.py` wires into GovernanceStack | **REUSABLE** for L2 event fanout |
| `governance/learning_bridge.py` | Operation feedback to learning memory | `integration.py` + `orchestrator.py` (OperationOutcome type) | **REUSABLE** for L2 attribution |
| `autonomy/intervention_decision_engine.py` | User state evaluation | `agent_runtime.py` only (within autonomy/) | **REUSABLE** for L3 human presence |

### Low Confidence Unused (probably active, needs verification)

| File | Description | Notes |
|---|---|---|
| `governance/approval_store.py` | Persisted approval decisions | Imported by orchestrator.py but unclear if consumed in production flow |
| `backend/core/ouroboros/cross_repo.py` | Cross-repo event types | Imported by tests + 2 intelligence modules; provides EventType used by event_bridge |
| `backend/core/ouroboros/trinity_integration.py` | Trinity bridge | Only `_deprecated_run_supervisor.py` + `scripts/verify_trinity_life.py` — may still be needed for Trinity |
| `backend/core/ouroboros/ui_integration.py` | UI bridge | Only `_deprecated_run_supervisor.py` — may still be needed for TUI |

### Quarantine Plan

**Phase 1 — Deprecation markers (this PR):**
Add `# DEPRECATED: superseded by {replacement}. Quarantine date: 2026-03-11` comment to top of each High Confidence Unused file. No code changes.

**Phase 2 — Runtime monitoring (next 2 weeks):**
Add import hook that logs if any deprecated module is loaded at runtime. Monitor logs for false positives.

**Phase 3 — Removal (separate PR, after monitoring window):**
Remove files with zero runtime loads during monitoring window. Each removal in its own commit for easy revert.

---

## Deliverable 4: NO-GO Conditions

### Phase P0 NO-GO (blocks feedback loop rollout)

| Condition | Verification |
|---|---|
| Single-writer invariant violated | grep/AST audit: no L2/L3/L4 code path calls `ledger.record()`, `change_engine.apply()`, `graduator.promote()` directly |
| Command idempotency broken | Test: send same command_id twice → second is no-op (logged, not executed) |
| Curriculum consumer creates duplicate backlog entries | Test: publish same curriculum signal twice → one backlog entry |
| Attribution scoring blocks GLS event loop | Verify: scoring runs in separate asyncio task with timeout, GLS submit() latency unchanged |
| Schema version mismatch undetected | Test: send command with version "0.9.0" → rejected by contract_gate |
| L2 crash cascades to L1 | Test: kill feedback_engine during scoring → GLS continues accepting operations |
| Feedback loop cycles infinitely | Test: curriculum → backlog → op → curriculum → verify max_depth or rate limit breaks cycle |

### Phase P1 NO-GO (blocks safety rollout)

| Condition | Verification |
|---|---|
| Safety command not highest priority | Test: L2 brain_hint + L3 incident_detected arrive simultaneously → L3 processed first |
| Health escalation fires on transient failure | Test: 1 probe failure → no escalation; 3 consecutive → escalation |
| Rollback analysis blocks GLS | Verify: analysis runs in separate task with 10s timeout |
| Human presence signal blocks critical ops | Test: presence=active + voice_human urgency=critical → op proceeds |
| Degradation mode switch not idempotent | Test: two `request_mode_switch(READ_ONLY)` → one transition |
| L3 crash blocks L1 safety | Verify: L3 is fail-open; L1 degradation FSM has its own rollback-count trigger |

### Phase P2 NO-GO (blocks advanced coordination rollout)

| Condition | Verification |
|---|---|
| Saga partial apply leaves inconsistent state | Test: apply repo A, crash, restart → repo A unchanged (idempotent), repo B applied |
| Consensus deadlock hangs pipeline | Test: 3 brains disagree, 120s timeout → escalate to human |
| Dynamic tier promotion without evidence | Test: recommend_tier_change without sufficient `evidence` field → rejected |
| L4 saga bypasses GLS submit | grep audit: saga always calls `GLS.submit()`, never `change_engine.apply()` directly |
| Consensus voting exceeds timeout budget | Test: voting with 120s/brain × 3 brains = 360s total; pipeline_timeout=300s → voted results cached, reused |
| Saga state file corruption | Test: corrupt JSON mid-saga → detected by checksum, saga rolled back to last clean phase |

### Global NO-GO (blocks any phase)

| Condition | Verification |
|---|---|
| Test coverage < 90% on new code | `pytest --cov` on all new/modified files |
| Any existing test regresses | Full suite: `pytest tests/` passes (excluding known 9 pre-existing failures) |
| Command queue unbounded | Verify: all queues have `maxsize` set, backpressure tested under load |
| Event consumers skip entries on restart | Test: stop consumer mid-batch, restart → processes from cursor, no skips |
| Timeout budget exceeded | Verify: sum of per-layer timeouts < pipeline_timeout for any single request path |
