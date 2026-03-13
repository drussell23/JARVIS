# L3–L5 Umbrella Architecture: Autonomous Ouroboros System-of-Systems

**Date:** 2026-03-12
**Status:** Approved
**Scope:** System-of-systems contract locking global invariants, shared data contracts, cross-repo ownership, telemetry schema, and promotion ladder for L3 (Parallel Subagent Execution), L4 (Strategic Memory + Intent Model), and L5 (Governed Self-Improvement).
**Per-phase execution specs:** See `2026-03-12-l3-parallel-subagent-spec.md`, and forthcoming L4/L5 specs.

---

## 1. Purpose

This document is the authoritative anchor for the L3–L5 autonomy ladder. Per-phase specs MUST conform to every contract, invariant, and schema defined here. Any deviation requires an explicit amendment to this umbrella doc — not unilateral change in a per-phase spec.

**Do not start implementation of any phase until:**
1. This umbrella doc is committed and reviewed.
2. The per-phase spec for that phase is committed and reviewed.
3. The previous phase's hard gates pass.

---

## 2. Maturity Ladder Summary

| Level | Name | Core Capability | Repo Owner |
|---|---|---|---|
| L3 | Parallel Subagent Execution | DAG-decomposed parallel patch bundles, merge coordinator | JARVIS + J-Prime + Reactor |
| L4 | Strategic Memory + Intent Model | Long-horizon memory graph, persistent intent injection | JARVIS + J-Prime + Reactor |
| L5 | Governed Self-Improvement | Self-proposed system improvements, shadow→canary→promote | JARVIS + J-Prime + Reactor |

Each level is **additive**: L3 does not break L1/L2; L4 does not break L3; L5 does not break L4. Schema dispatch and feature flags enforce this.

---

## 3. Global Invariants

These invariants apply to every phase unconditionally. Any L3/L4/L5 component that violates them is non-conformant.

| # | Invariant | Enforcement |
|---|---|---|
| G1 | **Deterministic lifecycle** — same inputs produce same decisions. No random tie-breaking, no wall-clock-dependent logic in decision paths. | Stable sort keys; monotonic ns timestamps only. |
| G2 | **Bounded concurrency** — all parallelism is gated by named semaphores with explicit caps. No unbounded fan-out. | Global + per-repo semaphores (L3); memory write locks (L4); eval slot caps (L5). |
| G3 | **Replayable decisions** — every autonomous decision is replayable from ledger + artifacts alone. No ephemeral in-memory state is load-bearing. | `TaskDAG.digest()` + ledger event chain = complete replay key. |
| G4 | **No hidden mutable shared state** — shared state exists only through explicit contracts and the durable ledger. Never via module-level globals or in-process caches that outlive a request. | Frozen dataclasses; explicit dependency injection. |
| G5 | **Cancellation propagates completely** — any external cancel kills all in-flight subprocesses (SIGTERM→wait→SIGKILL) and cleans all ephemeral resources. `asyncio.shield()` guards cleanup tasks. | WorkerSandbox.cancel(); MemoryWriter.cancel() (L4); EvalRunner.cancel() (L5). |
| G6 | **No real-tree writes before governance gate** — all work executes in isolated sandboxes; promotion to real working tree requires passing canonical VALIDATE + GATE. | WorkerSandbox invariant; L4 memory commits gated; L5 self-patches gated. |
| G7 | **Audit completeness** — every state transition emits a structured event with causal IDs, phase, and decision reason to the durable ledger. No silent transitions. | Canonical telemetry schema (Section 6). |
| G8 | **Backpressure and budgets** — every async loop has a timebox, retry cap, and budget counter. No retry storms; no unbounded queues. | Per-node/op timeboxes; regen caps; oscillation kill conditions. |

---

## 4. Unified Stop Conditions

All phases share these stop condition codes. When any condition triggers, the phase transitions to its ABORTED terminal state and emits the reason code to the ledger.

| Code | Trigger |
|---|---|
| `BUDGET_EXHAUSTED` | Wall-clock op deadline exceeded |
| `OSCILLATION` | Same failure signature detected N consecutive times (configurable per phase) |
| `POLICY_DENY` | Governance gate explicitly rejects |
| `CONFIDENCE_COLLAPSE` | L4: intent/memory confidence falls below phase threshold |
| `EVALUATOR_CAPTURE` | L5: single-metric gaming detected (multi-metric guard triggers) |
| `EXTERNAL_CANCEL` | Upstream `asyncio.CancelledError` received |
| `FATAL_INFRA` | Unrecoverable subprocess or storage error |
| `PREFLIGHT_REJECTED` | Phase-specific input validation failed |

---

## 5. Shared Data Contracts

These types are the cross-phase lingua franca. Per-phase specs extend but never replace them.

### 5.1 TaskDAG (L3)

Fully specified in `2026-03-12-l3-parallel-subagent-spec.md`. Summarized here for umbrella reference:

```python
@dataclass(frozen=True)
class DAGNode:
    node_id: str           # sha256(op_id + repo + sorted_bundle_files)[:16]
    repo: str
    bundle_files: tuple[str, ...]
    intent_summary: str
    estimated_risk: RiskLevel
    depends_on: frozenset[str]
    apply_mode: Literal["atomic"]
    conflict_key: str      # sha256(repo + "\x00" + "\x00".join(bundle_files))
    priority: int = 0

@dataclass(frozen=True)
class TaskDAG:
    dag_id: str            # sha256(op_id + sorted(node_ids))[:16]
    op_id: str
    schema_version: Literal["2d.1"]
    nodes: tuple[DAGNode, ...]
    conflict_groups: tuple[ConflictGroup, ...]
    allow_cross_repo_edges: bool   # default False
    generation_metadata: DAGGenerationMetadata
    # + precomputed _failure_closure, helpers: ready_nodes(), blocked_by_failure(),
    #   topological_layers(), conflicts_for(), digest()
```

**Cross-phase usage:** L4 may annotate DAG nodes with memory-derived context at injection time. L5 may propose modifications to DAG planner prompts. Neither may mutate a frozen `TaskDAG` after construction.

---

### 5.2 MemoryFact / IntentNode (L4)

Interface-level definition. Full contract in forthcoming L4 spec.

```python
@dataclass(frozen=True)
class MemoryFact:
    fact_id: str                        # sha256(content + provenance)[:16]
    content: str                        # the stored fact
    provenance: str                     # source: op_id | ledger_entry_id | "user"
    confidence: float                   # 0.0–1.0; subject to decay
    created_at_ns: int
    expires_at_ns: Optional[int]        # None = no expiry
    tags: frozenset[str]
    schema_version: Literal["fact.v1"]

@dataclass(frozen=True)
class IntentNode:
    intent_id: str                      # sha256(description + parent_intent_id)[:16]
    description: str                    # what the user is building
    supporting_facts: tuple[str, ...]   # fact_ids
    confidence: float                   # aggregate; decays over time
    parent_intent_id: Optional[str]     # hierarchical graph
    created_at_ns: int
    last_confirmed_at_ns: int
    decay_rate_per_day: float           # confidence -= decay_rate * elapsed_days
    schema_version: Literal["intent.v1"]
```

**Provenance requirement:** Every `MemoryFact` must have a traceable `provenance` field linking it to a ledger entry or explicit user statement. Facts with `confidence < 0.3` must not be injected into generation prompts without explicit flagging.

**Poisoning protection:** `MemoryFact` content may not be written from model output alone. At minimum one of: ledger-verified op result, user confirmation, or Reactor quality score ≥ 0.7.

---

### 5.3 SelfProposal / RolloutPlan / EvaluationResult (L5)

Interface-level definition. Full contract in forthcoming L5 spec.

```python
@dataclass(frozen=True)
class SelfProposal:
    proposal_id: str                    # sha256(target_component + rationale)[:16]
    description: str
    target_component: str               # e.g. "tool_executor", "brain_selector"
    proposed_patch: PatchBundle         # concrete change
    rationale: str
    risk_level: RiskLevel
    requires_governance_approval: bool = True   # default True; never False for L4 trust policy
    schema_version: Literal["proposal.v1"]

@dataclass(frozen=True)
class RolloutPlan:
    plan_id: str
    proposal_id: str
    stages: tuple[Literal["shadow", "canary", "promote"], ...]
    canary_fraction: float              # 0.0–1.0
    rollback_triggers: Mapping[str, float]  # metric_name → regression threshold
    schema_version: Literal["rollout.v1"]

@dataclass(frozen=True)
class EvaluationResult:
    eval_id: str
    plan_id: str
    stage: Literal["shadow", "canary", "promote"]
    metrics: Mapping[str, float]        # multi-metric; no single-KPI gating
    passed: bool
    rollback_triggered: bool
    rollback_reason: Optional[str]
    schema_version: Literal["eval.v1"]
```

**Multi-metric gate requirement (evaluator-gaming guard):** No `EvaluationResult.passed = True` based on a single metric. Minimum three independent metrics required (e.g., pass_rate + regression_rate + wall_clock_delta). L5 spec must enumerate the required metric set.

---

## 6. Canonical Telemetry + Event Schema

Every event emitted by any L3/L4/L5 component MUST include these base fields. Phase-specific fields are additive.

```python
@dataclass(frozen=True)
class BaseEvent:
    # Identity
    event_id: str                   # sha256(kind + op_id + timestamp_ns)[:16]
    kind: str                       # e.g. "sched.node.dispatched.v1"
    schema_version: str             # event schema version; semver

    # Causal chain
    op_id: str                      # parent operation
    causal_id: str                  # links this event to its cause in the chain
    parent_event_id: Optional[str]  # direct parent event; None for root events

    # Phase attribution
    phase: Literal["L3", "L4", "L5"]
    component: str                  # e.g. "subagent_scheduler", "memory_store"

    # Timing
    timestamp_ns: int               # monotonic; never wall-clock for decisions
    duration_ms: Optional[float]

    # Decision audit (required on any event representing a decision)
    decision_reason: Optional[str]  # reason code for any gate/kill/approve decision
```

**Phase-specific additive fields:**

| Phase | Required additional fields |
|---|---|
| L3 | `dag_id`, `dag_digest`, `node_id`, `conflict_key` |
| L4 | `intent_id`, `fact_id`, `confidence`, `provenance` |
| L5 | `proposal_id`, `plan_id`, `stage`, `eval_id` |

**Event naming convention:** `<component>.<noun>.<verb>.<schema_version>` — e.g., `sched.node.dispatched.v1`, `memory.fact.written.v1`, `rollout.canary.promoted.v1`.

---

## 7. Cross-Repo Ownership Matrix

| Capability | JARVIS | J-Prime | Reactor |
|---|---|---|---|
| **L3** | | | |
| DAG Planner (emits `2d.1`) | | ✓ | |
| Subagent Scheduler FSM | ✓ | | |
| Worker Sandbox | ✓ | | |
| Merge Coordinator | ✓ | | |
| Conflict analytics + attribution | | | ✓ |
| **L4** | | | |
| Memory store + retrieval policy | ✓ | | |
| Intent graph + provenance enforcement | ✓ | | |
| Planning conditioned on memory | | ✓ | |
| Memory quality scoring + drift detection | | | ✓ |
| **L5** | | | |
| Policy governor + promotion authority | ✓ | | |
| Self-improvement proposal engine | | ✓ | |
| Objective eval harness + rollback triggers | | | ✓ |
| **Shared** | | | |
| Durable ledger (all phases write) | ✓ | | |
| Brain selector | ✓ | | |
| Canonical telemetry sink | ✓ | | |

**Principle:** JARVIS owns governance, scheduling, and policy. J-Prime owns intelligence (planning, generation, proposals). Reactor owns measurement, attribution, and quality signals. No repo crosses these lanes without an explicit cross-repo contract.

---

## 8. Promotion Ladder

Used by L5. Defined here at umbrella level so L3/L4 components are designed to be observable by L5's eval harness.

```
SHADOW
  All traffic processed; proposed component runs in parallel (no effect on output).
  Metrics collected. Must pass shadow SLOs for promotion_shadow_duration_s.
        ↓
CANARY
  canary_fraction of ops routed through proposed component.
  Multi-metric eval at end of canary window.
  Auto-rollback if any rollback_trigger metric regresses beyond threshold.
        ↓
PROMOTE
  100% of ops routed through proposed component.
  Prior component retained for rollback_window_s.
        ↓
AUTO-ROLLBACK (from any stage)
  Any rollback_trigger fires → immediate revert to prior component version.
  Emits rollout.rollback.triggered.v1 with metric deltas.
  Locks proposal for human review before re-promotion attempt.
```

**Rollback is automatic and non-negotiable.** No L5 component may disable or defer rollback triggers without explicit governance approval.

---

## 9. Cross-Phase Guardrails

These rules constrain how phases interact. Violations are treated as governance failures.

| Rule | Scope |
|---|---|
| **L5 must not mutate L4 trust policy** without explicit governance approval. "Trust policy" = memory provenance rules, confidence decay parameters, fact write authorization rules. | L5 ↔ L4 |
| **L4 must not persist facts derived solely from L5 proposals** without Reactor quality score ≥ 0.7 or user confirmation. Prevents circular self-justification. | L5 → L4 |
| **L3 conflict groups are immutable after DAG construction.** Neither L4 nor L5 may inject additional conflicts or remove existing ones at runtime. | L4/L5 → L3 |
| **L5 may not propose changes to the governance gate itself** (orchestrator GATE phase logic) without human review and a signed approval ledger entry. | L5 → GATE |
| **Memory injection (L4) must be additive context only.** L4 may append context to generation prompts but may not suppress or replace existing prompt sections. | L4 → prompts |

---

## 10. Reproducibility Requirement

Every autonomous decision made by any L3/L4/L5 component must be fully reproducible from:
1. The durable ledger event chain (all events up to the decision point)
2. The artifacts referenced in those events (DAG digest, fact_ids, proposal_id, etc.)
3. The config snapshot at operation start (all env vars frozen to `OperationConfig` at intake)

**Nothing outside these three inputs may influence a decision.** Wall-clock time may be recorded but must not gate decisions. Random number generators are forbidden in decision paths.

---

## 11. Schema Version Registry

| Schema | Owner | Meaning |
|---|---|---|
| `2b.1` | J-Prime | Linear patch, full content (≤14B models) |
| `2b.1-diff` | J-Prime | Linear patch, unified diff (32B+) |
| `2b.1-noop` | J-Prime | Change already present |
| `2b.2-tool` | J-Prime | Tool-use response (L1) |
| `2c.1` | J-Prime | Multi-repo patch bundles |
| `2d.1` | J-Prime | DAG of parallel patch bundles (L3) |
| `2d.1-regen` | J-Prime | Conflict-stale regen response (L3 sub-variant) |
| `fact.v1` | JARVIS | MemoryFact (L4) |
| `intent.v1` | JARVIS | IntentNode (L4) |
| `proposal.v1` | JARVIS | SelfProposal (L5) |
| `rollout.v1` | JARVIS | RolloutPlan (L5) |
| `eval.v1` | Reactor | EvaluationResult (L5) |
| `dag.v1` | JARVIS | TaskDAG object (L3 runtime) |

Any new schema version requires an entry here before use in code.

---

## 12. Dependency Order

L4 may be designed and implemented before L3 hard gates pass, but **must not be activated** until L3 SLO gates pass. L5 must not be activated until L4 SLO gates pass.

```
L3 hard gates pass → L4 activation allowed
L4 hard gates pass → L5 activation allowed
```

Each phase's feature flag defaults to `false` and is enabled explicitly after gate passage.
