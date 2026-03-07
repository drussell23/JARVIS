# Phase A: Formal Contracts for Autonomous Pipeline

**Date**: 2026-03-06
**Status**: Approved
**Prerequisite**: Phase C2 (throughput hardening) — complete
**Enables**: Phase B (decision-action bridge wiring)

## Context

Phase C proved the runtime chain works end-to-end. Phase C2 hardened throughput
(concurrent extraction, adaptive admission, latency tracking). But the pipeline
has a critical gap: **no formal contract bridges reasoning output to action
commitment**. Scoring results flow directly to policy, which flows directly to
label+notify — no typed envelope, no commit ledger, no behavioral safety valve.

Phase A introduces 4 typed contracts that Phase B will wire into the triage
pipeline to create a fully auditable, idempotent, anomaly-guarded autonomous
decision-action chain.

## Scope: 4 Contracts

| Contract | Purpose | Location |
|---|---|---|
| DecisionEnvelope | Typed wrapper for reasoning outputs with traceability | `backend/core/contracts/decision_envelope.py` |
| PolicyGate | Async gate that allows/denies/defers proposed actions | `backend/core/contracts/policy_gate.py` |
| ActionCommitLedger | Durable append-only record of committed actions | `backend/core/contracts/action_commit_ledger.py` |
| BehavioralHealthMonitor | Anomaly detection on autonomous behavior patterns | `backend/autonomy/contracts/behavioral_health.py` |

**Not building** (already covered or deferred):
- ReasoningProvider: Thin adapter added as P1 (minimal interface over PrimeRouter)
- LifecycleGate: warm_up() + StartupContracts already handle this
- ActionExecutor: Thin adapter added as P1 (minimal interface for apply_label/deliver)

## File Layout

```
backend/core/contracts/
    __init__.py
    decision_envelope.py      # DecisionEnvelope + enums + IdempotencyKey builder
    policy_gate.py             # PolicyGate protocol + PolicyVerdict
    action_commit_ledger.py    # ActionCommitLedger + CommitRecord + state machine

backend/autonomy/contracts/
    __init__.py
    behavioral_health.py       # BehavioralHealthMonitor + recommendations
    triage_policy_gate.py      # TriagePolicyGate (wraps NotificationPolicy)
    reasoning_provider.py      # ReasoningProvider protocol (thin adapter for PrimeRouter)
    action_executor.py         # ActionExecutor protocol (thin adapter for label/notify)
```

## Contract 1: DecisionEnvelope

### Enums (strict, not free strings)

```python
class DecisionType(str, Enum):
    EXTRACTION = "extraction"
    SCORING = "scoring"
    POLICY = "policy"
    ACTION = "action"

class DecisionSource(str, Enum):
    JPRIME_V1 = "jprime_v1"
    JPRIME_DEGRADED = "jprime_degraded_fallback"
    HEURISTIC = "heuristic"
    CLOUD_CLAUDE = "cloud_claude"
    LOCAL_PRIME = "local_prime"
    ADAPTIVE = "adaptive"       # adapted weights from Reactor-Core

class OriginComponent(str, Enum):
    EMAIL_TRIAGE_RUNNER = "email_triage.runner"
    EMAIL_TRIAGE_EXTRACTION = "email_triage.extraction"
    EMAIL_TRIAGE_SCORING = "email_triage.scoring"
    EMAIL_TRIAGE_POLICY = "email_triage.policy"
    EMAIL_TRIAGE_LABELER = "email_triage.labels"
    EMAIL_TRIAGE_NOTIFIER = "email_triage.notifications"
```

### Envelope

```python
@dataclass(frozen=True)
class DecisionEnvelope:
    envelope_id: str                  # uuid4
    trace_id: str                     # correlates all decisions in one cycle
    parent_envelope_id: Optional[str] # previous envelope in the chain
    decision_type: DecisionType       # enum, not string
    source: DecisionSource            # enum, not string
    origin_component: OriginComponent # which module produced this

    # Payload
    payload: Dict[str, Any]           # serialized decision data
    confidence: float                 # 0.0-1.0

    # Dual timestamps
    created_at_epoch: float           # time.time() — wall clock for humans
    created_at_monotonic: float       # time.monotonic() — for execution semantics

    # Causal ordering
    causal_seq: int                   # LamportClock tick

    # Provenance
    config_version: str               # scoring_version, policy_version, etc.
    schema_version: int = 1           # envelope schema version
    producer_version: str = "1.0.0"   # code version that produced this
    compat_min_version: int = 1       # minimum reader version

    # Extensible
    metadata: Dict[str, Any] = field(default_factory=dict)
```

### IdempotencyKey Builder

```python
@dataclass(frozen=True)
class IdempotencyKey:
    """Canonical deterministic idempotency key.

    Built from: decision_type + target_id + action + config_version.
    Same inputs always produce the same key. Shared contract across repos.
    """
    key: str  # sha256 hex digest, truncated to 32 chars

    @classmethod
    def build(cls, decision_type: DecisionType, target_id: str,
              action: str, config_version: str) -> IdempotencyKey:
        ...
```

## Contract 2: PolicyGate

### Protocol (async)

```python
@runtime_checkable
class PolicyGate(Protocol):
    async def evaluate(
        self, envelope: DecisionEnvelope, context: Dict[str, Any]
    ) -> PolicyVerdict: ...

class VerdictAction(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    DEFER = "defer"

@dataclass(frozen=True)
class PolicyVerdict:
    allowed: bool
    action: VerdictAction             # enum, not string
    reason: str                       # human-readable
    conditions: Tuple[str, ...]       # invariants that must hold for execution
    envelope_id: str                  # which envelope this verdict applies to
    gate_name: str                    # which gate issued this verdict
    created_at_epoch: float
    created_at_monotonic: float
```

### TriagePolicyGate (implementation)

Wraps existing `NotificationPolicy.decide_action()` behind the `PolicyGate`
protocol. No behavior change — just typed interface.

## Contract 3: ActionCommitLedger

### State Machine

```
RESERVED ──commit()──> COMMITTED
    │
    ├──abort()───> ABORTED
    │
    └──(expiry)──> EXPIRED
```

No implicit states. Transitions are atomic (SQLite transaction).

### CommitRecord

```python
class CommitState(str, Enum):
    RESERVED = "reserved"
    COMMITTED = "committed"
    ABORTED = "aborted"
    EXPIRED = "expired"

@dataclass(frozen=True)
class CommitRecord:
    commit_id: str                    # uuid4
    idempotency_key: str              # from IdempotencyKey.build()
    envelope_id: str                  # which DecisionEnvelope
    trace_id: str                     # cycle correlation
    decision_type: DecisionType
    action: str                       # "apply_label", "deliver_immediate", etc.
    target_id: str                    # message_id being acted on

    # Lease provenance
    fencing_token: int
    lock_owner: str                   # runner instance id
    session_id: str                   # cycle session id
    expires_at_monotonic: float       # lease expiry

    # State
    state: CommitState
    reserved_at_epoch: float
    committed_at_epoch: Optional[float]
    outcome: Optional[str]            # "success", "partial", "failed"
    abort_reason: Optional[str]
    metadata: Dict[str, Any]
```

### Ledger Interface

```python
class ActionCommitLedger:
    async def reserve(
        self, envelope: DecisionEnvelope, action: str, target_id: str,
        fencing_token: int, lock_owner: str, session_id: str,
        idempotency_key: IdempotencyKey, lease_duration_s: float
    ) -> str:  # returns commit_id

    async def commit(self, commit_id: str, outcome: str,
                     metadata: Dict[str, Any] = None) -> None

    async def abort(self, commit_id: str, reason: str) -> None

    async def expire_stale(self) -> int  # returns count expired

    async def is_duplicate(self, idempotency_key: IdempotencyKey) -> bool

    async def query(self, *, since_epoch: float = 0,
                    decision_type: DecisionType = None,
                    state: CommitState = None) -> List[CommitRecord]
```

### Pre-Exec Invariant Check

Between `reserve()` and execution, the runner must verify:
1. Still the lease owner (fencing token matches)
2. Reservation not expired (`time.monotonic() < expires_at_monotonic`)
3. Not a duplicate (idempotency key not already committed)

```python
async def check_pre_exec_invariants(
    self, commit_id: str, current_fencing_token: int
) -> Tuple[bool, Optional[str]]:
    """Returns (ok, reason_if_not_ok)."""
```

### Storage

SQLite WAL backend (matches TriageStateStore, DedupLedger pattern).
Single `action_commits` table with indexes on `idempotency_key`, `state`,
`trace_id`, `expires_at_monotonic`.

### Retry Semantics

Retries reuse the same `idempotency_key` and same `commit_id`. A retry after
abort creates a new `CommitRecord` with the same idempotency key but a new
commit_id, linked via `metadata["retry_of"]`.

## Contract 4: BehavioralHealthMonitor

### Recommendations, Not Mutations

The monitor returns typed recommendations. The supervisor/runtime decides
whether to apply them. No hidden side effects.

```python
class ThrottleRecommendation(str, Enum):
    NONE = "none"
    REDUCE_BATCH = "reduce_batch"
    PAUSE_CYCLE = "pause_cycle"
    CIRCUIT_BREAK = "circuit_break"

@dataclass(frozen=True)
class BehavioralHealthReport:
    healthy: bool
    anomalies: Tuple[str, ...]              # detected anomaly descriptions
    recommendation: ThrottleRecommendation
    recommended_max_emails: Optional[int]   # suggested batch size, or None
    confidence: float                       # 0.0-1.0 in the recommendation
    window_cycles: int                      # how many cycles in the window
    metrics: Dict[str, float]               # raw metrics for observability
```

### Monitor Interface

```python
class BehavioralHealthMonitor:
    def record_cycle(
        self, report: TriageCycleReport,
        envelopes: List[DecisionEnvelope]
    ) -> None

    def check_health(self) -> BehavioralHealthReport

    def should_throttle(self) -> Tuple[ThrottleRecommendation, Optional[str]]
```

### Anomaly Detection (sliding window)

| Anomaly | Detection | Threshold |
|---|---|---|
| Rate spike | Actions per cycle > 3x rolling mean | env-configurable |
| Tier distribution shift | KL-divergence of tier counts > threshold | env-configurable |
| Error rate spike | Error ratio > 2x rolling mean | env-configurable |
| Confidence degradation | Mean confidence trending down 3+ cycles | slope < -0.05/cycle |
| Extraction timeout rate | >50% of extractions hitting timeout | env-configurable |

All thresholds env-var configurable (matches TriageConfig pattern).

## P1: Thin Adapter Interfaces

### ReasoningProvider (minimal)

```python
@runtime_checkable
class ReasoningProvider(Protocol):
    async def reason(
        self, prompt: str, context: Dict[str, Any],
        deadline: Optional[float] = None
    ) -> DecisionEnvelope: ...

    @property
    def provider_name(self) -> DecisionSource: ...
```

Implemented by `PrimeRouterAdapter` — wraps `router.generate()` call,
returns result inside a DecisionEnvelope.

### ActionExecutor (minimal)

```python
@runtime_checkable
class ActionExecutor(Protocol):
    async def execute(
        self, envelope: DecisionEnvelope, verdict: PolicyVerdict,
        commit_id: str
    ) -> ActionOutcome: ...

class ActionOutcome(str, Enum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    SKIPPED = "skipped"
```

Implemented by `TriageActionExecutor` — calls `apply_label()` +
`deliver_immediate()` / `deliver_summary()`.

## Data Flow After Phase A+B

```
fetch_emails()
    |
extract_features() --> DecisionEnvelope(type=EXTRACTION)
    |
score_email() --> DecisionEnvelope(type=SCORING, parent=extraction.id)
    |
PolicyGate.evaluate() --> PolicyVerdict(ALLOW/DENY/DEFER)
    |  (if ALLOW)
ActionCommitLedger.reserve(envelope, fencing_token, idempotency_key, lease)
    |
Pre-exec invariant check (still lease owner? not expired? not duplicate?)
    |  (if ok)
ActionExecutor.execute(envelope, verdict, commit_id)
    |
ActionCommitLedger.commit(commit_id, outcome) OR abort(commit_id, reason)
    |
BehavioralHealthMonitor.record_cycle(report, envelopes)
    |
Runner checks should_throttle() before next cycle
```

## Done Criteria

1. All 4 contracts typed and versioned (schema_version + compat_min_version)
2. Ledger state machine with atomic SQLite transitions (RESERVED->COMMITTED|ABORTED|EXPIRED)
3. Async PolicyGate protocol + TriagePolicyGate implementation
4. DecisionEnvelope with dual clocks + provenance + typed enums
5. Canonical IdempotencyKey builder (deterministic, shared)
6. BehavioralHealthMonitor returns recommendations only (no direct mutation)
7. Unit tests for:
   - Envelope creation and causal chaining
   - Idempotency key determinism
   - Ledger reserve/commit/abort state transitions
   - Ledger duplicate detection
   - Ledger stale lease expiry
   - Ledger pre-exec invariant check (fencing, expiry, duplicate)
   - PolicyGate allow/deny/defer paths
   - BehavioralHealth anomaly detection (rate spike, tier shift, error spike)
   - BehavioralHealth recommendation types (NONE, REDUCE_BATCH, PAUSE_CYCLE, CIRCUIT_BREAK)

## Mathematical Foundation: Dynamic Concurrency

Phase C2 uses static concurrency (`extraction_concurrency=3`). The system
should evolve toward dynamic concurrency based on Little's Law:

    C >= (N * W_p95) / (T_budget - T_overhead)

Subject to memory constraint:

    M_active <= M_limit (13GB on 16GB M1, leaving 3GB for macOS)

This is deferred to Phase B wiring, where BehavioralHealthMonitor's
`recommended_max_emails` output feeds back into adaptive admission. The
mathematical throttle uses real-time memory pressure + observed p95 latency
to compute optimal batch size each cycle.
