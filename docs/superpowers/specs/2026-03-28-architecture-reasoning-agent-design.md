# Architecture Reasoning Agent: Multi-File Design Engine for Ouroboros

**Date:** 2026-03-28
**Status:** Design approved, pending implementation
**Depends on:** Ouroboros Daemon (Zone 7.0) + Cognitive Extensions (Roadmap Sensor + Feature Synthesis Engine)
**Scope:** Third and final Ouroboros extension — completes the autonomous developer stack

---

## Preamble

The Ouroboros Daemon finds and fixes bugs (REM Sleep). The Cognitive Extensions know WHERE the system is going and WHAT's missing (Roadmap Sensor + Feature Synthesis). But neither can **design new features** — they produce patches, not architecture.

The Architecture Reasoning Agent closes this gap. It takes FeatureHypotheses (capability gaps) and produces ArchitecturalPlans — structured, validated, multi-file design documents with dependency DAGs, interface contracts, and acceptance checks. These plans decompose into coordinated IntentEnvelopes (sagas) that execute through the existing governance pipeline.

This is the final piece that transforms Ouroboros from a maintenance daemon into an autonomous developer.

### Governing Philosophy

**The Symbiotic AI-Native Manifesto v2 — Boundary Mandate:**

- **One expensive "think"** (design phase): the model reasons about architecture, interfaces, dependencies. This is where intelligence creates maximum leverage.
- **Many governed "acts"** (execution phase): each step is a governed envelope through the existing pipeline. The immune system is reused, not reinvented.
- **Plan as frozen contract**: the ArchitecturalPlan is an immutable, auditable artifact. Envelopes are obligations derived from the contract. GATE enforces the contract.

---

## Architecture: Two-Phase + Saga Orchestration

```
FeatureHypothesis (from Synthesis Engine)
  |
  v
DESIGN PHASE (expensive, infrequent)
  |  Model: Doubleword 397B batch (primary) or Claude API (fallback)
  |  Input: hypothesis + Oracle graph neighborhoods + snapshot P0 fragments
  |  Output: ArchitecturalPlan (versioned, immutable, auditable)
  |
  v
PlanValidator (deterministic)
  |  Validates: DAG is acyclic, no orphan steps, paths repo-relative,
  |  no ".." escape, allowlist complete, acceptance checks well-formed
  |  FAIL -> reject plan, optionally one repair pass
  |
  v
PlanStore (immutable, keyed by plan_hash)
  |  ~/.jarvis/ouroboros/plans/{plan_hash}.json
  |
  v
Plan GATE (RiskEngine)
  |  source="architecture"
  |  BLOCK: kernel, secrets, auth
  |  APPROVAL_REQUIRED: ouroboros/**, cross-repo, high blast radius
  |  SAFE_AUTO: single-repo, scoped to allowlist
  |
  v
SagaOrchestrator.execute(saga_id)
  |  Fetches plan from PlanStore
  |  Calls PlanDecomposer.decompose(plan) -> List[IntentEnvelope]
  |  Iterates steps by topological tier (sequential in v1)
  |  Each step: IntakeRouter.ingest(envelope) -> governance pipeline
  |  WAL-backed state machine: PENDING -> RUNNING -> COMPLETE | ABORTED
  |  Step failure -> ABORTED, label earlier PRs blocked, no further work
  |  All steps complete -> run acceptance checks in sandbox
  |  All acceptance pass -> SAGA_COMPLETE
```

### Boundary Principle Applied

- Design phase is **agentic**: 397B/Claude reasons about structure, interfaces, dependencies.
- Plan schema, DAG validation, allowlist enforcement, saga state machine are **deterministic skeleton**.
- Execution reuses **existing governance pipeline** per envelope — no second immune system.
- Plan is **first-class auditable artifact**, not implicit state in prompts or logs.

---

## Schemas

### PlanStep

```python
class StepIntentKind(enum.Enum):
    CREATE_FILE = "create_file"
    MODIFY_FILE = "modify_file"
    DELETE_FILE = "delete_file"

@dataclass(frozen=True)
class PlanStep:
    """Single step in the architectural plan's dependency DAG."""
    step_index: int                         # 0-based, unique within plan
    description: str                        # "Create WhatsAppAgent class"
    intent_kind: StepIntentKind
    target_paths: Tuple[str, ...]           # repo-relative, no ".." escape
    ancillary_paths: Tuple[str, ...] = ()   # registry, __init__.py, config wiring
    repo: str                               # "jarvis"
    interface_contracts: Tuple[str, ...] = ()  # ("class WhatsAppAgent(BaseNeuralMeshAgent)",)
    tests_required: Tuple[str, ...] = ()    # ("tests/neural_mesh/test_whatsapp_agent.py",)
    risk_tier_hint: str = "safe_auto"       # "safe_auto", "approval_required"
    depends_on: Tuple[int, ...] = ()        # step indices that must complete first
```

### AcceptanceCheck

```python
class CheckKind(enum.Enum):
    EXIT_CODE = "exit_code"           # command must exit 0
    REGEX_STDOUT = "regex_stdout"     # stdout must match pattern
    IMPORT_CHECK = "import_check"     # python import must succeed

@dataclass(frozen=True)
class AcceptanceCheck:
    """Deterministic check the orchestrator runs post-saga in sandbox."""
    check_id: str
    check_kind: CheckKind
    command: str                        # "python3 -m pytest tests/neural_mesh/ -v"
    expected: str                       # "" for exit_code, regex for regex_stdout, module path for import
    cwd: str = "."                     # repo-relative working directory
    timeout_s: float = 120.0
    run_after_step: Optional[int] = None  # None = after all steps; int = after specific step
    sandbox_required: bool = True       # must run in Reactor Core sandbox, not host
```

### ArchitecturalPlan

```python
@dataclass(frozen=True)
class ArchitecturalPlan:
    """Immutable design contract produced by the Architecture Reasoning Agent.

    Once plan_hash is computed, the plan is frozen. Material changes require
    a new plan_id and new plan_hash. Sagas bind to plan_hash.
    """
    plan_id: str                            # UUID
    plan_hash: str                          # SHA256 of structure+scope (not provenance)
    parent_hypothesis_id: str               # FeatureHypothesis that triggered this
    parent_hypothesis_fingerprint: str

    # Scope
    title: str                              # "WhatsApp Agent Integration"
    description: str                        # Design rationale
    repos_affected: Tuple[str, ...]         # ("jarvis",) or ("jarvis", "jarvis-prime")
    non_goals: Tuple[str, ...]              # Explicit scope boundaries

    # DAG
    steps: Tuple[PlanStep, ...]             # Ordered by step_index
    file_allowlist: FrozenSet[str]          # Union of all target_paths + tests + ancillary

    # Acceptance
    acceptance_checks: Tuple[AcceptanceCheck, ...]

    # Provenance (NOT included in plan_hash — same design = same hash)
    model_used: str                         # "doubleword-397b" or "claude-api"
    created_at: float                       # UTC epoch seconds
    snapshot_hash: str                      # RoadmapSnapshot hash at design time
```

**plan_hash computation:** SHA256 of canonical JSON containing: title, description, repos_affected, non_goals, steps (sorted by step_index, each with all structural fields), file_allowlist (sorted), acceptance_checks. Excludes: plan_id, model_used, created_at, snapshot_hash (provenance).

**file_allowlist invariant:** `file_allowlist == union(step.target_paths + step.tests_required + step.ancillary_paths for step in steps)`. PlanValidator enforces this.

### SagaRecord

```python
class SagaPhase(enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    ABORTED = "aborted"

class StepPhase(enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    BLOCKED = "blocked"             # dependency failed

@dataclass
class StepState:
    step_index: int
    phase: StepPhase
    envelope_id: Optional[str] = None
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    error: Optional[str] = None

@dataclass
class SagaRecord:
    """WAL-backed saga execution state."""
    saga_id: str
    plan_id: str
    plan_hash: str                      # Frozen at saga creation (immutability invariant)
    phase: SagaPhase
    step_states: Dict[int, StepState]
    created_at: float
    completed_at: Optional[float] = None
    abort_reason: Optional[str] = None

    # Persistence: ~/.jarvis/ouroboros/sagas/{saga_id}.json
```

**Saga rules:**
- Step N cannot start until all `depends_on` steps have phase `COMPLETE`
- v1: steps execute sequentially by topological tier (parallel independent steps deferred)
- Any step `FAILED` -> saga transitions to `ABORTED`
- `ABORTED`: no new work on this saga. Earlier PRs labeled as blocked. New saga required.
- `COMPLETE` only when: all steps `COMPLETE` AND all acceptance_checks pass (run in sandbox)

### Per-Envelope Binding

Each IntentEnvelope produced by decomposition carries:
```python
evidence={
    "saga_id": saga.saga_id,
    "plan_hash": saga.plan_hash,        # GATE loads allowlist from PlanStore
    "step_index": step.step_index,
    "analysis_complete": True,           # bypasses ANALYZING
}
```

Per-envelope GATE: loads plan from PlanStore by `plan_hash`, validates `envelope.target_files ⊆ plan.file_allowlist` (paths normalized to repo-relative). Violation -> BLOCKED.

---

## Components

### ArchitectureReasoningAgent (Design Phase)

```python
class ArchitectureReasoningAgent:
    """Produces ArchitecturalPlans from FeatureHypotheses.

    One expensive model call per hypothesis. The model reasons about:
    - What files need to exist (or be modified)
    - What interfaces they expose
    - What the dependency order is
    - What tests verify correctness
    - What is explicitly out of scope
    """

    async def design(
        self,
        hypothesis: FeatureHypothesis,
        snapshot: RoadmapSnapshot,
        oracle: Any,
    ) -> Optional[ArchitecturalPlan]:
        """Design an architectural plan for a capability gap.

        Returns None if the hypothesis doesn't warrant structural design
        (e.g., confidence too low, gap_type is patch-level).
        """
```

**Model stack:**
- Primary: Doubleword 397B batch (highest reasoning quality, async)
- Fallback: Claude API (on 397B failure/timeout/parse error)
- Prompt: structured JSON output schema with examples

**Threshold:** Only `missing_capability` and `manifesto_violation` hypotheses trigger design. `incomplete_wiring` and `stale_implementation` are patches.

### PlanValidator (Deterministic)

```python
class PlanValidator:
    """Deterministic validation of ArchitecturalPlan structure.

    Zero model calls. All checks are structural.
    """

    def validate(self, plan: ArchitecturalPlan) -> ValidationResult:
        """Validate plan structure. Returns pass/fail with reasons."""
```

**Checks:**
1. DAG is acyclic (topological sort succeeds)
2. Step indices are 0..N-1 with no gaps or duplicates
3. All depends_on references are valid step indices
4. Every step has at least one target_path
5. All paths are repo-relative, no ".." escape
6. file_allowlist == union of all step paths
7. Acceptance checks have valid check_kind
8. repos_affected matches union of step repos
9. No empty plan (at least one step)
10. step count <= OUROBOROS_ARCHITECT_MAX_STEPS

### PlanStore (Immutable)

```python
class PlanStore:
    """Immutable plan storage keyed by plan_hash.

    Once stored, a plan cannot be modified. GATE and SagaOrchestrator
    load plans by hash — single source of truth for allowlists.
    """

    def store(self, plan: ArchitecturalPlan) -> None
    def load(self, plan_hash: str) -> Optional[ArchitecturalPlan]
    def exists(self, plan_hash: str) -> bool

    # Persistence: ~/.jarvis/ouroboros/plans/{plan_hash}.json
```

### PlanDecomposer (Deterministic)

```python
class PlanDecomposer:
    """Decompose an ArchitecturalPlan into coordinated IntentEnvelopes.

    One envelope per PlanStep. Envelopes carry saga_id + plan_hash + step_index.
    Deterministic: no model calls.
    """

    def decompose(
        self,
        plan: ArchitecturalPlan,
        saga_id: str,
    ) -> List[IntentEnvelope]
```

### SagaOrchestrator (WAL-Backed State Machine)

```python
class SagaOrchestrator:
    """Orchestrate multi-step architectural sagas.

    WAL-backed, idempotent, deterministic state transitions.
    Sequential execution by topological tier (v1).
    """

    def create_saga(self, plan: ArchitecturalPlan) -> SagaRecord
    async def execute(self, saga_id: str) -> SagaRecord
    def get_saga(self, saga_id: str) -> Optional[SagaRecord]
    def list_sagas(self) -> List[SagaRecord]

    # Persistence: ~/.jarvis/ouroboros/sagas/{saga_id}.json
```

**Execution flow inside `execute(saga_id)`:**
1. Load plan from PlanStore by saga.plan_hash
2. Call PlanDecomposer.decompose(plan, saga_id) -> envelopes
3. Compute topological tiers from DAG
4. For each tier (sequential in v1):
   - For each step in tier:
     - Submit envelope to IntakeRouter.ingest()
     - Wait for governance pipeline to complete (poll or callback)
     - Update StepState in WAL
     - If FAILED -> abort saga, mark remaining steps as BLOCKED
5. After all steps complete: run acceptance checks via AcceptanceRunner
6. All pass -> SAGA_COMPLETE. Any fail -> SAGA_ABORTED.

### AcceptanceRunner (Sandbox-Bound)

```python
class AcceptanceRunner:
    """Execute acceptance checks in Reactor Core sandbox.

    NEVER runs on the host Body (JARVIS). All commands execute in
    an isolated sandbox environment for safety.
    """

    async def run_checks(
        self,
        checks: Tuple[AcceptanceCheck, ...],
        saga_id: str,
    ) -> List[AcceptanceResult]
```

**Safety:** All acceptance commands run in Reactor Core sandbox, not the host OS. The orchestrator must not blindly execute model-generated commands on the live system.

---

## REM Epoch Integration

### Routing Split on gap_type

The Architecture Reasoning Agent fires during REM epochs for hypotheses that require structural design:

```python
# In RemEpoch, during PATCHING phase:
for finding in ranked_findings:
    if (finding.source_check.startswith("roadmap:")
        and finding.category in ("missing_capability", "manifesto_violation")
        and self._architect is not None):
        # Structural design needed — route to architect
        plan = await self._architect.design(finding_to_hypothesis(finding), snapshot, oracle)
        if plan:
            validated = self._plan_validator.validate(plan)
            if validated.passed:
                self._plan_store.store(plan)
                saga = self._saga_orchestrator.create_saga(plan)
                await self._saga_orchestrator.execute(saga.saga_id)
    else:
        # Normal patch flow (existing pipeline)
        envelope = finding_to_envelope(finding)
        await intake_router.ingest(envelope)
```

**Gap type routing:**
| gap_type | Route | Why |
|----------|-------|-----|
| `missing_capability` | Architecture Agent | New feature = new files + interfaces + wiring |
| `manifesto_violation` | Architecture Agent | Structural misalignment = design-level fix |
| `incomplete_wiring` | Direct to pipeline | Module exists, just needs connection |
| `stale_implementation` | Direct to pipeline | Code exists, needs update |

### Max Sagas Per Epoch

`OUROBOROS_ARCHITECT_MAX_SAGAS_PER_EPOCH` (default 2) prevents runaway design during a single REM epoch. After the limit, remaining architectural hypotheses are deferred to the next epoch.

---

## Risk Engine Rules

### New Source: "architecture"

```python
_VALID_SOURCES: add "architecture"
_PRIORITY_MAP: "architecture": 3  # higher than exploration/roadmap (coordinated changes)
```

### Tiered Rules (Order: BLOCK -> APPROVAL_REQUIRED -> SAFE_AUTO -> else BLOCK)

```python
if source == "architecture":
    # 1. BLOCK: kernel, secrets, auth (hard invariant)
    if touches_supervisor or touches_security_surface:
        -> BLOCKED

    # 2. APPROVAL_REQUIRED: ouroboros code (can evolve, but reviewed)
    if touches_ouroboros_code:
        -> APPROVAL_REQUIRED, reason="architecture_self_modification"

    # 3. APPROVAL_REQUIRED: cross-repo or multi-PR
    if crosses_repo_boundary:
        -> APPROVAL_REQUIRED, reason="architecture_cross_repo"

    # 4. SAFE_AUTO: single-repo, scoped to plan allowlist
    if within_plan_allowlist:
        -> SAFE_AUTO

    # 5. else: unknown surface -> BLOCK (prevent accidental auto)
    -> BLOCKED, reason="architecture_unknown_surface"
```

---

## File Structure

### New Files

```
backend/core/ouroboros/architect/
  __init__.py
  plan.py                      # ArchitecturalPlan, PlanStep, AcceptanceCheck, enums
  plan_validator.py             # Deterministic DAG/allowlist/structure validation
  plan_store.py                 # Immutable plan storage keyed by plan_hash
  plan_decomposer.py            # Plan -> coordinated IntentEnvelopes
  reasoning_agent.py            # ArchitectureReasoningAgent (model call)
  saga.py                       # SagaRecord, SagaPhase, StepPhase, StepState
  saga_orchestrator.py          # WAL-backed saga state machine
  acceptance_runner.py          # Sandbox-bound acceptance check executor

tests/core/ouroboros/architect/
  __init__.py
  test_plan.py
  test_plan_validator.py
  test_plan_store.py
  test_plan_decomposer.py
  test_reasoning_agent.py
  test_saga.py
  test_saga_orchestrator.py
  test_acceptance_runner.py
  test_integration.py
```

### Modified Files

| File | Change |
|------|--------|
| `intent_envelope.py` | Add `"architecture"` to `_VALID_SOURCES` |
| `unified_intake_router.py` | Add `"architecture": 3` to `_PRIORITY_MAP` |
| `risk_engine.py` | Add architecture-source tiered rules |
| `rem_epoch.py` | Route missing_capability/manifesto_violation to architect |
| `daemon_config.py` | Add architect/saga env vars |
| `daemon.py` | Wire ArchitectureReasoningAgent + SagaOrchestrator |
| `rem_sleep.py` | Pass architect reference to RemEpoch |

---

## Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `OUROBOROS_ARCHITECT_ENABLED` | `true` | Master toggle |
| `OUROBOROS_ARCHITECT_MODEL` | `doubleword-397b` | Primary model for design phase |
| `OUROBOROS_ARCHITECT_FALLBACK_MODEL` | `claude-api` | Fallback on primary failure |
| `OUROBOROS_ARCHITECT_MAX_STEPS` | `10` | Max steps per plan |
| `OUROBOROS_ARCHITECT_MAX_SAGAS_PER_EPOCH` | `2` | Max sagas per REM epoch |
| `OUROBOROS_SAGA_STEP_TIMEOUT_S` | `300` | Per-step execution timeout |
| `OUROBOROS_SAGA_TOTAL_TIMEOUT_S` | `3600` | Full saga timeout |
| `OUROBOROS_ACCEPTANCE_TIMEOUT_S` | `120` | Per acceptance check timeout |
| `OUROBOROS_PLAN_STORE_DIR` | `~/.jarvis/ouroboros/plans` | Immutable plan storage |
| `OUROBOROS_SAGA_STORE_DIR` | `~/.jarvis/ouroboros/sagas` | Saga WAL storage |

---

## Testing Strategy

- **Plan schemas:** Unit tests for PlanStep, AcceptanceCheck, ArchitecturalPlan creation and plan_hash determinism
- **PlanValidator:** Unit tests for each of the 10 validation rules (acyclic DAG, path normalization, allowlist completeness, etc.)
- **PlanStore:** Unit tests for store/load immutability, duplicate detection, missing plan handling
- **PlanDecomposer:** Unit tests for plan -> envelope decomposition, saga_id/plan_hash binding, step ordering
- **SagaOrchestrator:** Unit tests for state machine transitions, WAL persistence, dependency ordering, abort handling, acceptance gate
- **AcceptanceRunner:** Unit tests with mock sandbox, check_kind handling, timeout behavior
- **ReasoningAgent:** Mock model, verify structured output parsing, threshold filtering
- **REM integration:** Mock architect + saga, verify routing split, max sagas limit
- **End-to-end:** Full hypothesis -> design -> validate -> decompose -> saga -> accept with mocked providers

---

## Day 1 Capabilities (Complete Autonomous Developer Stack)

With all three extensions implemented, Ouroboros can:

| Layer | What | How |
|-------|------|-----|
| **Maintenance** (Daemon) | Fix bugs, wire dormant code, resolve TODOs | Oracle + Fleet -> patches -> governance |
| **Awareness** (Roadmap Sensor) | Know where the system is going | Deterministic snapshot from specs/plans/backlog/git |
| **Synthesis** (Feature Engine) | Know what's missing | Tier 0 hints + 397B reasoning -> hypotheses |
| **Architecture** (Reasoning Agent) | Design new features | 397B/Claude design -> plan -> saga -> governed execution |

The organism wakes up, surveys its body, understands its roadmap, identifies capability gaps, designs solutions, validates them, and ships PRs — autonomously, across all three Trinity repos, 24/7.
