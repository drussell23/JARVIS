# L3–L5 Umbrella Architecture: Autonomous Ouroboros System-of-Systems

**Date:** 2026-03-12
**Status:** Approved — v2 (post spec review)
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
| G1 | **Deterministic lifecycle** — same inputs produce same decisions. No random tie-breaking, no wall-clock-dependent logic in decision paths. | Stable sort keys; monotonic ns timestamps for ordering only; count-based budgets for gates. |
| G2 | **Bounded concurrency** — all parallelism is gated by named semaphores with explicit caps. No unbounded fan-out. | Global + per-repo semaphores (L3); memory write locks (L4); eval slot caps (L5). |
| G3 | **Replayable decisions** — every autonomous decision is replayable from ledger + artifacts alone. No ephemeral in-memory state is load-bearing. | `TaskDAG.digest()` + ledger event chain = complete replay key. |
| G4 | **No hidden mutable shared state** — shared state exists only through explicit contracts and the durable ledger. Never via module-level globals or in-process caches that outlive a request. | Frozen dataclasses; explicit dependency injection. |
| G5 | **Cancellation propagates completely** — any external cancel kills all in-flight subprocesses (SIGTERM→wait→SIGKILL) and cleans all ephemeral resources. `asyncio.shield()` guards cleanup tasks. | `WorkerSandbox.cancel()`; `MemoryWriter.cancel()` (L4); `EvalRunner.cancel()` (L5) — all share this protocol. |
| G6 | **No real-tree writes before governance gate** — all work executes in isolated sandboxes; promotion to real working tree requires passing canonical VALIDATE + GATE. | WorkerSandbox invariant; L4 memory commits gated; L5 self-patches gated. |
| G7 | **Audit completeness** — every state transition emits a structured event with causal IDs, phase, and decision reason to the durable ledger. No silent transitions. | Canonical telemetry schema (Section 6). |
| G8 | **Backpressure and budgets** — every async loop has a timebox, retry cap, and budget counter. No retry storms; no unbounded queues. | Per-node/op timeboxes; regen caps; oscillation kill conditions. |

---

## 4. Unified Stop Conditions

All phases share these stop condition codes. When any condition triggers, the phase transitions to its ABORTED terminal state and emits the reason code to the ledger.

Per-phase prefixes are used when the code is phase-specific (e.g., `SCHED_CONFIDENCE_COLLAPSE` for L3). The base codes below are the canonical names; per-phase specs may add a component prefix for disambiguation.

| Code | Trigger | Phase applicability |
|---|---|---|
| `BUDGET_EXHAUSTED` | Wall-clock op deadline exceeded | All |
| `OSCILLATION` | Same failure signature detected N consecutive times (configurable per phase) | All |
| `POLICY_DENY` | Governance gate explicitly rejects | All |
| `CONFIDENCE_COLLAPSE` | Confidence signal falls below phase threshold. L3: fraction of failed nodes ≥ collapse_threshold. L4: intent/memory confidence score < minimum. L5: eval confidence < required floor. | All (phase-specific threshold) |
| `EVALUATOR_CAPTURE` | L5: single-metric gaming detected (multi-metric guard triggers) | L5 |
| `EXTERNAL_CANCEL` | Upstream `asyncio.CancelledError` received | All |
| `FATAL_INFRA` | Unrecoverable subprocess or storage error | All |
| `PREFLIGHT_REJECTED` | Phase-specific input validation failed | All |

---

## 5. Shared Data Contracts

These types are the cross-phase lingua franca. Per-phase specs extend but never replace them.

### 5.0 Carried-Over Primitives

**`PatchBundle`** is the canonical type for a set of file-level changes targeting a single repository. It is defined and owned by JARVIS and used across L1/L2/L3/L5.

```python
@dataclass(frozen=True)
class FilePatch:
    file_path: str                      # normalized, relative to repo root
    op: Literal["modify", "create", "delete"]
    full_content: Optional[str]         # present for modify/create (schema 2b.1)
    unified_diff: Optional[str]         # present for diff-based schemas (2b.1-diff)

@dataclass(frozen=True)
class PatchBundle:
    repo: str                           # target repo name (must be in RepoRegistry)
    patches: tuple[FilePatch, ...]      # one entry per file in bundle
    schema_version: str                 # inherits from the generating schema (2b.1 / 2b.1-diff)
    candidate_id: str                   # stable identifier for this candidate set
```

`PatchBundle` maps directly to the per-repo patch shape in J-Prime schema `2c.1` / `2d.1`. The `patch` field in a `2d.1` DAG node wraps a `PatchBundle` for that node's `repo`.

Canonical definition location: `backend/core/ouroboros/governance/patch_bundle.py` (to be created in L3 implementation). L3/L5 specs reference this type by name.

---

### 5.1 TaskDAG (L3)

Fully specified in `2026-03-12-l3-parallel-subagent-spec.md`. Summarized here for umbrella reference:

```python
@dataclass(frozen=True)
class DAGNode:
    node_id: str           # sha256(op_id + "\x00" + repo + "\x00" + "\x00".join(sorted_bundle_files))[:16]
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
    dag_id: str            # sha256(op_id + "\x00" + "\x00".join(sorted(node_ids)))[:16]
    op_id: str
    schema_version: Literal["2d.1"]
    nodes: tuple[DAGNode, ...]
    conflict_groups: tuple[ConflictGroup, ...]
    allow_cross_repo_edges: bool   # default False
    generation_metadata: DAGGenerationMetadata
    # + precomputed _failure_closure, helpers: ready_nodes(), blocked_by_failure(),
    #   topological_layers(), conflicts_for(), digest()
```

**Cross-phase usage:** L4 may inject memory-derived context into the generation prompt that produces the `2d.1` response. However, **once a `TaskDAG` is constructed from a given `op_id`, it is authoritative for the lifetime of that operation**. A new `2d.1` response (e.g., from L4 memory-conditioned re-generation) produces a new `TaskDAG` with a new `dag_id` — it does not mutate the prior DAG. L5 may propose modifications to DAG planner prompts; neither may mutate a frozen `TaskDAG` after construction.

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

**Poisoning protection:** `MemoryFact` content may not be written from model output alone. At minimum one of: ledger-verified op result, user confirmation, or Reactor `MemoryQualityScore ≥ 0.7`. `MemoryQualityScore` is produced by Reactor's memory quality scorer (L4 cross-repo contract; full definition in L4 spec). JARVIS owns the write authorization gate; Reactor produces the score signal.

**Required cancel/cleanup interface (G5):**
```python
class MemoryWriter:
    async def cancel(self) -> None: ...   # cancel in-flight write; shield(cleanup)
    async def cleanup(self) -> None: ...  # remove uncommitted memory state
```

---

### 5.3 SelfProposal / RolloutPlan / EvaluationResult (L5)

Interface-level definition. Full contract in forthcoming L5 spec.

```python
@dataclass(frozen=True)
class SelfProposal:
    proposal_id: str                    # sha256(target_component + rationale)[:16]
    description: str
    target_component: str               # e.g. "tool_executor", "brain_selector"
    proposed_patch: PatchBundle         # concrete change (see Section 5.0)
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
    rollback_triggers: Mapping[str, float]
    # Semantics: each entry is metric_name → absolute_floor.
    # Rollback fires when metric_value < floor for any entry.
    # Example: {"pass_rate": 0.90} means rollback if pass_rate drops below 0.90.
    # Relative thresholds (e.g., % of baseline) must be pre-computed into absolutes at plan creation.
    requires_human_approval: bool = True
    # True: human sign-off required before CANARY → PROMOTE transition.
    # Must be True for any component touching governance, trust policy, or memory provenance.
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

**Required cancel/cleanup interface (G5):**
```python
class EvalRunner:
    async def cancel(self) -> None: ...   # cancel in-flight eval; shield(cleanup)
    async def cleanup(self) -> None: ...  # remove eval sandbox state
```

---

## 6. Canonical Telemetry + Event Schema

Every event emitted by any L3/L4/L5 component MUST include these base fields. Phase-specific fields are additive.

```python
@dataclass(frozen=True)
class BaseEvent:
    # Identity
    event_id: str                   # UUID4 assigned at emission time by canonical telemetry sink.
                                    # All events are emitted through the parent process's telemetry
                                    # sink (never from worker subprocesses directly). This ensures
                                    # single-authority assignment and avoids cross-process collisions.
    kind: str                       # e.g. "sched.node.dispatched.v1"
    schema_version: str             # event schema version; semver

    # Causal chain
    op_id: str                      # parent operation
    causal_id: str                  # links this event to its cause in the chain
    parent_event_id: Optional[str]  # direct parent event; None ONLY for root operation intake event

    # Phase attribution
    phase: Literal["L3", "L4", "L5"]
    component: str                  # e.g. "subagent_scheduler", "memory_store"

    # Timing
    timestamp_ns: int               # monotonic; used for ordering and duration only, never for decisions
    duration_ms: Optional[float]

    # Decision audit (required on any event representing a decision)
    decision_reason: Optional[str]  # reason code for any gate/kill/approve decision
```

**Single-authority emission rule:** Worker subprocess results are reported to the parent scheduler/coordinator, which creates ledger events on their behalf. Subprocess components do NOT write to the ledger directly. This ensures `event_id` uniqueness without cross-process coordination.

**Causal chain completeness (G7):** Any event emitted within an active operation context (where `op_id` is set and the event is not the root intake event) MUST have a non-null `parent_event_id` tracing back to a prior event in the same `op_id` chain. Events with `parent_event_id=None` are only valid for root intake events.

**Phase-specific additive fields:**

| Phase | Required additional fields |
|---|---|
| L3 | `dag_id`, `dag_digest`, `node_id` (where applicable), `conflict_key` (where applicable) |
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
| Memory quality scoring (`MemoryQualityScore`) + drift detection | | | ✓ |
| **L5** | | | |
| Policy governor + promotion authority | ✓ | | |
| Self-improvement proposal engine | | ✓ | |
| Objective eval harness + rollback triggers | | | ✓ |
| **Shared** | | | |
| Durable ledger (all phases write via JARVIS sink) | ✓ | | |
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
  Auto-rollback if any rollback_trigger metric_value < floor threshold.
        ↓
[HUMAN GATE — required when RolloutPlan.requires_human_approval=True]
  Human reviews canary eval metrics and signs approval ledger entry.
  Blocks PROMOTE until signed. Non-negotiable for governance-adjacent components.
        ↓
PROMOTE
  100% of ops routed through proposed component.
  Prior component retained for rollback_window_s.
        ↓
AUTO-ROLLBACK (from any stage)
  Any rollback_trigger fires (metric_value < floor) → immediate revert to prior component version.
  Emits rollout.rollback.triggered.v1 with metric deltas.
  Locks proposal for human review before re-promotion attempt.
```

**Rollback is automatic and non-negotiable.** No L5 component may disable or defer rollback triggers without explicit governance approval.

**`requires_human_approval` defaults to `True`.** It MUST be `True` for: governance gate components, trust policy rules, memory provenance rules, brain selector logic, and any component in the GATE/APPLY/VERIFY orchestrator phases.

---

## 9. Cross-Phase Guardrails

These rules constrain how phases interact. Violations are treated as governance failures.

| Rule | Scope |
|---|---|
| **L5 must not mutate L4 trust policy** without explicit governance approval. "Trust policy" = memory provenance rules, confidence decay parameters, fact write authorization rules. | L5 ↔ L4 |
| **L4 must not persist facts derived solely from L5 proposals** without Reactor `MemoryQualityScore ≥ 0.7` or user confirmation. Prevents circular self-justification. | L5 → L4 |
| **L3 conflict groups are immutable after DAG construction.** Neither L4 nor L5 may modify conflict groups in a live `TaskDAG`. If L4 memory context causes J-Prime to emit a different `2d.1` response, a new `TaskDAG` is built with a new `dag_id` — the prior DAG is not mutated. | L4/L5 → L3 |
| **L5 may not propose changes to the governance gate itself** (orchestrator GATE phase logic) without human review and a signed approval ledger entry. | L5 → GATE |
| **Memory injection (L4) must be additive context only.** L4 may append context to generation prompts but may not suppress or replace existing prompt sections. | L4 → prompts |

---

## 10. Reproducibility Requirement

Every autonomous decision made by any L3/L4/L5 component must be fully reproducible from:
1. The durable ledger event chain (all events up to the decision point)
2. The artifacts referenced in those events (DAG digest, fact_ids, proposal_id, etc.)
3. The config snapshot at operation start — all env vars frozen to `OrchestratorConfig` (existing type in `backend/core/ouroboros/governance/orchestrator.py`) at intake. L3/L4/L5 phase configs are sub-fields of this config.

**Nothing outside these three inputs may influence a decision.** Wall-clock time may be recorded (`timestamp_ns`) but must not gate decisions. Count-based budgets and fraction-based thresholds are used for all kill conditions. Random number generators are forbidden in decision paths.

---

## 11. Schema Version Registry

All schema version strings in use by J-Prime, JARVIS, or Reactor must be registered here before use in code.

| Schema | Owner | Meaning |
|---|---|---|
| `2b.1` | J-Prime | Linear patch, full content (≤14B models) |
| `2b.1-diff` | J-Prime | Linear patch, unified diff (32B+) |
| `2b.1-noop` | J-Prime | Change already present |
| `2b.2-tool` | J-Prime | Tool-use response (L1) |
| `2c.1` | J-Prime | Multi-repo patch bundles |
| `2d.1` | J-Prime | DAG of parallel patch bundles (L3); also the `schema_version` field on `TaskDAG` objects |
| `2d.1-regen` | J-Prime | Conflict-stale regen response (L3 sub-variant; same response shape as 2b.1/2b.1-diff) |
| `fact.v1` | JARVIS | MemoryFact (L4) |
| `intent.v1` | JARVIS | IntentNode (L4) |
| `proposal.v1` | JARVIS | SelfProposal (L5) |
| `rollout.v1` | JARVIS | RolloutPlan (L5) |
| `eval.v1` | Reactor | EvaluationResult (L5) |

**Note:** `2d.1` serves as both the J-Prime wire format version and the `schema_version` field on the in-process `TaskDAG` object. There is no separate `dag.v1` identifier — the wire schema and runtime object schema are the same.

---

## 12. Dependency Order + SLO Placeholder Gates

L4 may be designed and implemented before L3 hard gates pass, but **must not be activated** until L3 SLO gates pass. L5 must not be activated until L4 SLO gates pass.

```
L3 hard gates pass → L4 activation allowed
L4 hard gates pass → L5 activation allowed
```

Each phase's feature flag defaults to `false` and is enabled explicitly after gate passage.

**L3 hard gates:** See `2026-03-12-l3-parallel-subagent-spec.md` Section 10 (fully specified).

**L4 placeholder gates** (to be detailed in L4 spec):
- Memory fact retention rate ≥ X% across Y ops
- Intent drift ≤ Z% per session (measured by Reactor)
- No memory poisoning incidents (confidence + provenance guard: 0 bypasses)
- Causal trace completeness: zero facts written without traceable provenance

**L5 placeholder gates** (to be detailed in L5 spec):
- ≥ 3 independent metrics gated per `EvaluationResult` (evaluator-gaming guard)
- Auto-rollback latency ≤ 1 op after trigger (rollback fires before next op completes)
- Zero governance gate bypasses (GATE phase modifications require human approval: 100%)
- Shadow SLO pass rate ≥ 95% before canary promotion allowed
