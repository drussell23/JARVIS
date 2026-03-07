# Governed Self-Programming Loop — Design Doc

**Date:** 2026-03-07
**Status:** Approved
**Scope:** Wire sandbox_loop.py through governance can_write() gate, add approval pause/resume, route candidate generation to GCP J-Prime with failback, define shadow harness for one domain slice.
**Explicit exclusions:** Curiosity engine, red team arbiter — deferred to future phases.

---

## 1. Module Layout + OperationContext

### New Files

| File | Purpose | Lines (est.) |
|------|---------|-------------|
| `backend/core/ouroboros/governance/op_context.py` | Frozen OperationContext dataclass + typed sub-objects | ~120 |
| `backend/core/ouroboros/governance/orchestrator.py` | Thin pipeline coordinator — phase transitions only | ~200 |
| `backend/core/ouroboros/governance/candidate_generator.py` | Failback state machine + provider abstraction | ~250 |
| `backend/core/ouroboros/governance/approval_provider.py` | ApprovalProvider protocol + CLI implementation | ~180 |
| `backend/core/ouroboros/governance/shadow_harness.py` | Side-effect firewall + output comparator | ~220 |

### Modified Files

| File | Change |
|------|--------|
| `backend/core/ouroboros/governance/sandbox_loop.py` | Replace direct execution with orchestrator call |
| `backend/core/ouroboros/governance/integration.py` | Wire orchestrator into GovernanceStack |

### OperationContext

```python
@dataclass(frozen=True)
class OperationContext:
    op_id: str                          # UUID, immutable
    created_at: datetime                 # UTC
    phase: OperationPhase               # Current phase (enum)
    phase_entered_at: datetime           # When current phase started
    context_hash: str                    # SHA-256 of serialized state
    previous_hash: Optional[str]        # Hash chain — previous phase's hash

    # Classification
    target_files: Tuple[str, ...]       # Files this op targets
    risk_tier: Optional[RiskTier]       # From risk engine classification
    description: str                    # Human-readable op description

    # Phase-specific typed sub-objects (None until populated)
    routing: Optional[RoutingDecision] = None
    approval: Optional[ApprovalDecision] = None
    shadow: Optional[ShadowResult] = None
    generation: Optional[GenerationResult] = None
    validation: Optional[ValidationResult] = None

    # Governance
    policy_version: str = ""
    side_effects_blocked: bool = True   # Default: side effects blocked

    def advance(self, new_phase: OperationPhase, **updates) -> "OperationContext":
        """Return new context with updated phase + hash chain."""
        # Validates transition legality
        # Computes new context_hash, sets previous_hash = self.context_hash
        ...
```

Typed sub-objects (`RoutingDecision`, `ApprovalDecision`, `ShadowResult`, `GenerationResult`, `ValidationResult`) are each frozen dataclasses with their own fields. All transitions go through `advance()` which enforces the state machine and maintains the hash chain.

---

## 2. Operation Lifecycle State Machine

### Phases (Enum)

```
CLASSIFY -> ROUTE -> GENERATE -> VALIDATE -> GATE -> [APPROVE] -> APPLY -> VERIFY -> COMPLETE
                        |            |                                          |
                   GENERATE_RETRY  VALIDATE_RETRY                          POSTMORTEM
                     (max 1)        (max 2)

Terminal states: COMPLETE, CANCELLED, EXPIRED, POSTMORTEM
```

### Phase Definitions

| Phase | Entry Condition | Action | Exit |
|-------|----------------|--------|------|
| CLASSIFY | Op created | Risk engine classifies target files | -> ROUTE |
| ROUTE | Classification done | Failback FSM selects provider (GCP or local) | -> GENERATE |
| GENERATE | Provider selected | Candidate code generation | -> VALIDATE (or GENERATE_RETRY max 1) |
| VALIDATE | Candidates exist | AST parse + sandbox test suite | -> GATE (or VALIDATE_RETRY max 2) |
| GATE | Validation passed | `can_write()` check | -> APPROVE or APPLY (based on risk tier) |
| APPROVE | Gate passed + risk requires approval | Human decision via ApprovalProvider | -> APPLY or CANCELLED/EXPIRED |
| APPLY | Approved or auto-approved | change_engine executes write with pre-tested rollback | -> VERIFY |
| VERIFY | Apply succeeded | Post-apply test suite | -> COMPLETE or POSTMORTEM (auto-rollback) |
| COMPLETE | Verify passed | Ledger finalized, metrics emitted | Terminal |
| POSTMORTEM | Apply/Verify failed | Rollback confirmed, failure context recorded | Terminal |
| CANCELLED | Any pre-APPLY failure | Reason recorded in context | Terminal |
| EXPIRED | Approval timeout | No auto-approve, human must re-trigger | Terminal |

### Retry States

- **GENERATE_RETRY**: Max 1. Triggered by generation timeout. Failback FSM switches provider.
- **VALIDATE_RETRY**: Max 2. Triggered by AST/test failure. Feedback sent to generator for refinement.

### Transition Rules

- Every transition records: `(from_phase, to_phase, actor, timestamp, context_hash)`
- `actor` is one of: `orchestrator`, `human`, `timeout`, `error`
- Only `advance()` can create transitions — no direct field mutation
- Hash chain: each phase boundary computes `SHA-256(serialized_context)`, embeds previous hash
- Approval timeout codified: `approval_timeout_s` in GovernanceConfig (default 600s). After expiry -> EXPIRED, not CANCELLED (different semantics: EXPIRED = no decision made, CANCELLED = explicit rejection or system failure).

---

## 3. Candidate Generator + Failback State Machine

### Provider Abstraction

```python
class CandidateProvider(Protocol):
    async def generate(
        self, context: OperationContext, deadline: datetime
    ) -> GenerationResult: ...
    async def health_probe(self) -> bool: ...
    @property
    def provider_name(self) -> str: ...
```

Two implementations:
- `GCPPrimeProvider`: Routes to J-Prime on GCP VM via PrimeClient. Propagates deadline (not fixed timeout).
- `LocalProvider`: Uses local model serving (unified_model_serving.py 3-tier). Same interface, same deadline propagation.

### Failback State Machine

```
                    3+ probes pass
                    over 45s dwell
PRIMARY_READY  <─────────────────  PRIMARY_DEGRADED
     |                                    ^
     | timeout/error                      |
     v                                    |
FALLBACK_ACTIVE ──────────────────────────┘
     |                    probe starts
     | local also fails
     v
QUEUE_ONLY  (terminal for this op — CANCELLED)
```

| State | Behavior | Transition Out |
|-------|----------|----------------|
| PRIMARY_READY | Route to GCP J-Prime | timeout/error -> FALLBACK_ACTIVE |
| PRIMARY_DEGRADED | Route to local, probe GCP in background | 3+ probes pass over 45s -> PRIMARY_READY |
| FALLBACK_ACTIVE | Route to local provider | GCP probe succeeds -> PRIMARY_DEGRADED (begins dwell) |
| QUEUE_ONLY | No provider available | Op -> CANCELLED |

**Asymmetric timing**: Failover is immediate (one failure). Failback requires 3 consecutive successful probes spread over at least 45 seconds. This prevents flapping.

**Per-provider concurrency quotas**: GCP gets higher quota (heavier model, slower). Local gets lower quota. Excess requests queue, never drop.

**Deadline propagation**: Generator passes `deadline = now + generation_timeout_s` to provider. Provider must respect it — no fixed internal timeouts that ignore the caller's deadline.

### Cross-Repo Awareness

Cross-repo event bus stays OUTSIDE candidate_generator. The orchestrator is responsible for feeding cross-repo context into `OperationContext` before the GENERATE phase. The generator is pure: context in, candidates out.

---

## 4. Approval Provider

### Protocol

```python
class ApprovalProvider(Protocol):
    async def request(self, context: OperationContext) -> str:
        """Submit op for approval. Returns request_id. Idempotent on same op_id."""
        ...

    async def approve(self, request_id: str, approver: str) -> ApprovalDecision:
        """Approve. Idempotent — second call on approved op is no-op."""
        ...

    async def reject(
        self, request_id: str, approver: str, reason: str
    ) -> ApprovalDecision:
        """Reject. Idempotent."""
        ...

    async def await_decision(
        self, request_id: str, timeout_s: float
    ) -> ApprovalDecision:
        """Block until decision or timeout. Returns EXPIRED on timeout."""
        ...
```

### ApprovalDecision (Frozen Dataclass)

```python
@dataclass(frozen=True)
class ApprovalDecision:
    status: ApprovalStatus  # PENDING, APPROVED, REJECTED, EXPIRED, SUPERSEDED
    approver: Optional[str]
    reason: Optional[str]
    decided_at: Optional[datetime]
    request_id: str
```

### CLI Implementation (Phase 1)

- `jarvis --approve <op_id>` — approve pending operation
- `jarvis --reject <op_id> --reason "..."` — reject with reason
- `jarvis --list-pending` — show all pending approvals with context

Stored in-memory dict keyed by `request_id`. `asyncio.Event` per request for `await_decision()` blocking.

### Behavioral Guarantees

- **Idempotent**: Approving an already-approved op returns the existing decision
- **Timeout -> EXPIRED**: Never auto-approve. Human must explicitly re-trigger.
- **Late decision -> SUPERSEDED**: If decision arrives after EXPIRED, logged as SUPERSEDED (forensic trail), no effect on op
- **Future adapters**: TUI, voice, webhook — all implement same protocol. CLI is first, not only.

---

## 5. Shadow Harness

### Purpose

Run candidate code in a side-effect-free environment parallel to production. Compare outputs. Build confidence before enabling real writes for a domain slice.

### Side-Effect Firewall (Hard Enforcement)

```python
class ShadowModeViolation(Exception):
    """Raised when shadow code attempts a forbidden side effect."""

class SideEffectFirewall:
    """Patches dangerous builtins/modules during shadow execution.

    BLOCKED operations (raises ShadowModeViolation):
    - File writes (open in write/append mode)
    - Process spawning (subprocess module)
    - OS-level mutations (os.remove, os.unlink, shutil.rmtree)
    - Network calls (socket.connect, urllib)

    ALLOWED operations:
    - File reads (open in read mode)
    - Pure computation (ast.parse, json.loads, math)
    """

    def __enter__(self):
        """Monkey-patch all blocked functions to raise ShadowModeViolation."""
        ...

    def __exit__(self, *args):
        """Restore all originals."""
        ...
```

This is **hard enforcement**, not convention. Shadow code physically cannot write files, spawn processes, or make network calls. Violation = immediate exception + shadow run aborted.

### Output Comparator

```python
class OutputComparator:
    def compare(self, expected: Any, actual: Any, mode: CompareMode) -> float:
        """Return confidence score 0.0-1.0."""
        ...
```

Three comparison modes:
- **EXACT**: Byte-identical output -> 1.0, else 0.0
- **AST**: Parse both as Python AST, compare structure (ignores whitespace/comments) -> 0.0-1.0
- **SEMANTIC**: Normalize identifiers, compare logical structure -> 0.0-1.0

### ShadowResult (Frozen Dataclass)

```python
@dataclass(frozen=True)
class ShadowResult:
    confidence: float           # 0.0-1.0
    comparison_mode: CompareMode
    violations: Tuple[str, ...]  # Any firewall violations caught
    shadow_duration_s: float
    production_match: bool      # confidence >= threshold
    disqualified: bool          # True if 3 consecutive below threshold
```

### Automatic Disqualification

If a shadow slice produces `confidence < 0.7` on 3 consecutive runs:
1. Slice is auto-disqualified
2. Alert emitted (log + TUI notification)
3. Shadow continues to run (for data collection) but slice cannot promote
4. Human must investigate and manually re-enable

### Integration

Shadow harness runs in parallel with the main pipeline during VALIDATE phase. Shadow failure never blocks or affects the production path. Results are attached to `OperationContext.shadow` for observability.

---

## 6. Failure Matrix

Every component failure maps to a deterministic recovery action. No ambiguity.

| Failure | Phase | Detection | Recovery | Op State |
|---------|-------|-----------|----------|----------|
| Candidate generation timeout | GENERATE | `asyncio.TimeoutError` | Retry once with fallback provider. Both fail -> CANCELLED | GENERATE_RETRY -> CANCELLED |
| Candidate generation crash | GENERATE | Unhandled exception | Log traceback + op_id. No retry. -> CANCELLED | CANCELLED |
| Failback provider also fails | GENERATE | Second provider timeout/error | QUEUE_ONLY mode. -> CANCELLED `reason=all_providers_exhausted` | CANCELLED |
| Validation: AST parse fails | VALIDATE | `SyntaxError` | VALIDATE_RETRY (max 2). Third failure -> CANCELLED | VALIDATE_RETRY -> CANCELLED |
| Validation: test suite fails | VALIDATE | exit code != 0 | VALIDATE_RETRY with feedback. Cap at 2 -> CANCELLED | VALIDATE_RETRY -> CANCELLED |
| Validation: sandbox timeout | VALIDATE | `asyncio.TimeoutError` | Kill subprocess. -> CANCELLED (untrusted code) | CANCELLED |
| Write gate returns False | GATE | `(False, reason_code)` | Logged with reason. -> CANCELLED | CANCELLED |
| Approval timeout | APPROVE | `approval_timeout_s` expires | -> EXPIRED. No auto-approve. | EXPIRED |
| Approval rejected | APPROVE | `REJECTED` status | -> CANCELLED `reason=human_rejected` | CANCELLED |
| Apply: file write fails | APPLY | `IOError`/`PermissionError` | Rollback via change_engine. -> POSTMORTEM | POSTMORTEM |
| Apply: lock acquisition fails | APPLY | Lock timeout | -> CANCELLED `reason=lock_contention`. No partial write. | CANCELLED |
| Verify: post-apply tests fail | VERIFY | exit code != 0 | Auto-rollback. -> POSTMORTEM `rollback=true` | POSTMORTEM |
| Shadow: side-effect violation | SHADOW | `ShadowModeViolation` | Shadow aborted. Production unaffected. | N/A |
| Shadow: low confidence x3 | SHADOW | 3 consecutive < 0.7 | Slice auto-disqualified. Production unaffected. | N/A |
| Orchestrator crash | Any | Unhandled exception | Top-level handler logs full context. -> POSTMORTEM | POSTMORTEM |
| Governance not started | Pre-CLASSIFY | `can_write()` -> False | Op never enters pipeline. Logged as rejected. | Never created |

**Invariants:**
- Every failure path ends in a terminal state (CANCELLED, EXPIRED, POSTMORTEM, or COMPLETE). No op can be abandoned in a non-terminal state.
- POSTMORTEM always includes: `{op_id, phase_at_failure, context_hash, rollback_performed, error_class, error_message}`.

---

## 7. Minimal Test Plan

### 7.1 OperationContext (`tests/test_ouroboros_governance/test_op_context.py`)

| Test | Pass Gate |
|------|-----------|
| Frozen after construction | `dataclasses.replace()` works, direct assignment raises `FrozenInstanceError` |
| Phase transition via `advance()` | Returns new context with updated phase + timestamp |
| Hash chain integrity | `context_hash` changes on every `advance()`, previous hash embedded |
| Invalid phase transition rejected | `COMPLETE -> GENERATE` raises `ValueError` |
| Typed sub-objects round-trip | `RoutingDecision`, `ApprovalDecision`, `ShadowResult` serialize/deserialize |

### 7.2 Orchestrator (`tests/test_ouroboros_governance/test_orchestrator.py`)

| Test | Pass Gate |
|------|-----------|
| Happy path end-to-end (mock all providers) | Op reaches COMPLETE, ledger has full trace |
| `can_write()` returns False -> CANCELLED | Op never reaches APPLY phase |
| Generator timeout -> retry -> fallback | Failback FSM transitions correctly |
| Validation failure -> VALIDATE_RETRY (max 2) | Third failure -> CANCELLED |
| Apply failure -> auto-rollback -> POSTMORTEM | Rollback confirmed, postmortem has error context |
| Approval required -> pause -> resume on approve | Op sits in APPROVE until decision |
| Approval timeout -> EXPIRED | Op terminal after `approval_timeout_s` |
| Crash in any phase -> POSTMORTEM | Top-level handler catches, logs, records |

### 7.3 Candidate Generator + Failback (`tests/test_ouroboros_governance/test_candidate_generator.py`)

| Test | Pass Gate |
|------|-----------|
| Primary success | Returns candidates, state stays PRIMARY_READY |
| Primary timeout -> failback | State -> FALLBACK_ACTIVE, local provider used |
| Primary recovery (3 probes, 45s dwell) | State -> PRIMARY_READY only after all 3 pass |
| Fast failover, slow failback (asymmetric) | Failover < 5s, failback requires 45s+ |
| Both providers fail -> QUEUE_ONLY | Op cancelled, no candidates returned |
| Concurrency quota enforced | Excess requests queued, not dropped |

### 7.4 Approval Provider (`tests/test_ouroboros_governance/test_approval_provider.py`)

| Test | Pass Gate |
|------|-----------|
| CLI approve flow | `request()` -> pending, `approve()` -> APPROVED |
| CLI reject flow | `request()` -> pending, `reject()` -> REJECTED |
| Timeout -> EXPIRED | No decision within TTL -> EXPIRED |
| Idempotent approve on already-approved | Second `approve()` is no-op |
| Late decision after timeout -> SUPERSEDED | Logged as SUPERSEDED, no effect |
| `await_decision()` blocks until resolution | Resolves on approve/reject/timeout |

### 7.5 Shadow Harness (`tests/test_ouroboros_governance/test_shadow_harness.py`)

| Test | Pass Gate |
|------|-----------|
| Firewall blocks file writes | `ShadowModeViolation` raised |
| Firewall blocks process spawning | `ShadowModeViolation` raised |
| Firewall blocks network calls | `ShadowModeViolation` raised |
| Read-only ops pass through | `open('r')`, `ast.parse()`, `json.loads()` succeed |
| Output comparator: exact match -> 1.0 | Identical outputs score perfect |
| Output comparator: AST-equivalent -> >= 0.9 | Whitespace/comment differences tolerated |
| Confidence < 0.7 x3 -> disqualify | Slice auto-disqualified, alert emitted |
| Shadow crash doesn't affect production | Exception in shadow -> logged, pipeline proceeds |

**Overall pass gate**: All tests green + zero POSTMORTEM ops in a 50-op canary run.

---

## 8. Rollout Plan for First Canary Slice

### Target Slice: `backend/core/ouroboros/governance/`

Self-referential but low-blast-radius. Failures only affect governance internals, not user-facing features.

### Phase 1: Shadow-Only (Week 1-2)

| Step | Action | Gate |
|------|--------|------|
| 1 | Deploy orchestrator with shadow harness enabled, writes disabled | All governance tests pass |
| 2 | Run 20+ shadow operations against governance slice | Zero `ShadowModeViolation` |
| 3 | Review shadow output comparator scores | Mean confidence >= 0.8 |
| 4 | Verify no production file was touched | `git status` clean after every shadow run |

### Phase 2: Gated Writes with Approval (Week 3-4)

| Step | Action | Gate |
|------|--------|------|
| 5 | Enable `can_write()` gate in SANDBOX mode | Write gate returns `(True, "ok")` for canary slice |
| 6 | Run 30+ operations with APPROVAL_REQUIRED on all | Derek approves/rejects each via CLI |
| 7 | Track approval latency, rejection reasons | <5% rejection rate, p95 approval < 10min |
| 8 | Verify rollback works on rejected ops | Every rejection -> clean rollback confirmed |

### Phase 3: Canary Promotion (Week 5-6)

| Metric | Threshold | Measured Over |
|--------|-----------|---------------|
| Total ops | >= 50 | Cumulative |
| Rollback rate | < 5% | Last 50 ops |
| p95 latency | < 120s | Last 50 ops |
| Stability window | 72h | No POSTMORTEM in window |
| Shadow confidence | Mean >= 0.85 | Last 20 shadow runs |

**Promotion**: When all thresholds met -> canary state PENDING -> ACTIVE. Derek signs off. Governance mode eligible for GOVERNED.

### Phase 4: Second Slice (Week 7+)

Pick next slice based on canary evidence. Candidates:
- `backend/core/ouroboros/` (broader ouroboros, still low blast radius)
- A non-critical utility module identified by shadow harness confidence scores

Each new slice starts at Phase 1 (shadow-only). No shortcuts.

---

## 9. What This Enables

After implementation:
1. `sandbox_loop.py` routes all writes through `can_write()` — single gate, no bypass
2. Heavy candidate generation offloads to GCP J-Prime with deterministic local fallback
3. Every operation above LOW risk requires explicit human approval via CLI
4. Shadow harness proves candidate quality before real writes are enabled for a slice
5. Full operation lifecycle is traceable via hash-chained OperationContext + ledger
6. First canary slice (`backend/core/ouroboros/governance/`) begins shadow evaluation immediately
