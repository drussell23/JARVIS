# Vertical Integration Design — Self-Development Pipeline Phase 1

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Wire all 4 governance layers (Intent, Multi-Repo, Comms, Autonomy) into a live end-to-end self-development pipeline triggered by CLI, generating fixes via J-Prime, approved via CLI, applied with test verification, emitting READY_TO_COMMIT.

**Architecture:** Extend GovernedLoopService (Approach A) — maximum reuse of existing pipeline, minimal new surface area. Three new modules (CLI, TestRunner, ApprovalStore), two modified modules (GovernedLoopService, ChangeEngine op_id passthrough).

**Tech Stack:** Python 3.11+, asyncio, pytest subprocess, fcntl file locking, JSON persistence, CommProtocol notifications.

---

## Phase 1 Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Trigger | Manual CLI (`jarvis self-modify "..."`) | Deterministic, scriptable, maximum control |
| Approval | CLI-only (`jarvis approve/reject <op_id>`) | No TUI dependency, clean audit trail |
| Apply scope | Write files + run tests, no auto-commit | One human-controlled boundary after verification |
| Notifications | TUI + voice (read-only) | Visibility without authority |

---

## 1. Architecture Overview

```
CLI Command                    GovernedLoopService              External
-----------                    -------------------              --------
jarvis self-modify "desc"  --> submit(OperationContext)
                               |
                               +- RiskEngine.classify()
                               +- AutonomyGate.should_proceed()
                               +- CandidateGenerator (PrimeProvider -> ClaudeProvider failback)
                               +- TestRunner.run() [sandbox]      --> pytest subprocess
                               +- ChangeEngine.execute() [PLAN->SANDBOX->VALIDATE->GATE->APPLY]
                               +- ApprovalStore (pause for CLI)    --> waits for CLI input
                               +- TestRunner.run() [post-apply]    --> pytest subprocess
                               +- READY_TO_COMMIT result
                               +- CommProtocol notifications       --> TUI panel + voice

jarvis approve <op_id>     --> ApprovalStore.decide()
jarvis reject <op_id>      --> ApprovalStore.decide()
jarvis self-dev-status     --> Ledger query
```

**New modules (3):**
- `backend/core/ouroboros/governance/self_dev_cli.py` — CLI entry points (thin, no side effects)
- `backend/core/ouroboros/governance/test_runner.py` — pytest subprocess wrapper
- `backend/core/ouroboros/governance/approval_store.py` — Durable approval persistence (atomic JSON)

**Modified modules (2):**
- `governed_loop_service.py` — Add SANDBOX_TEST, APPROVE, VERIFY phases + READY_TO_COMMIT result
- `change_engine.py` — Accept external `op_id` parameter (no internal generation)

**No changes to:** RiskEngine, CommProtocol, TUI rendering, supervisor core.

---

## 2. Pipeline Flow

```
Phase 1: INTAKE
  CLI parses args -> builds OperationContext(trigger_source="cli_manual", description, target_files)
  -> RiskEngine.classify() -> RiskClassification
  -> AutonomyGate.should_proceed(config, cai, uae, sai)
  -> Ledger: INTAKE recorded

Phase 2: GENERATE
  CandidateGenerator selects provider (PrimeProvider -> ClaudeProvider failback)
  Provider locked for entire op (no mid-op switch)
  -> GenerationResult(candidate_code, provider_id, model_id, routing_reason)
  -> Ledger: GENERATE recorded with provider provenance

Phase 3: SANDBOX_TEST
  TestRunner.run_sandbox(candidate, affected_tests)
  Runs in temp copy -- no disk writes to working tree yet
  If sandbox tests fail -> REJECTED, no APPLY, rollback not needed
  -> Ledger: SANDBOX_TEST pass/fail

Phase 4: GATE
  ChangeEngine phases: PLAN -> SANDBOX -> VALIDATE -> GATE
  RiskEngine re-check on actual diff
  If BLOCKED -> stop, emit denial
  -> Ledger: GATE pass/block

Phase 5: APPROVE
  If risk tier != SAFE_AUTO or autonomy tier < AUTONOMOUS:
    -> Write pending approval to ApprovalStore (atomic JSON + fsync + flock)
    -> Emit notifications (CommProtocol -> TUI panel + voice)
    -> Poll for CLI decision: approve / reject / timeout
    -> First valid decision wins; duplicates -> "superseded"
    -> Timeout (configurable, default 30min) -> "expired" -> safe path (no apply)
  Phase 1: always requires approval (SAFE_AUTO skip deferred to Phase 2)
  -> Ledger: APPROVE granted/rejected/expired with actor, channel, reason

Phase 6: APPLY
  ChangeEngine phases: APPLY -> LEDGER -> PUBLISH
  Files written to working tree
  -> Ledger: APPLY with changed file list

Phase 7: VERIFY
  TestRunner.run_affected(changed_files)
  Runs against real working tree post-apply
  If tests fail -> ChangeEngine ROLLBACK -> files restored -> FAILED result
  -> Ledger: VERIFY pass/fail

Phase 8: READY_TO_COMMIT (terminal)
  Emit result payload:
    op_id, changed_files, provider_id, model_id, routing_reason,
    verification_summary (sandbox + post-apply results),
    rollback_status ("clean" | "rolled_back" | "rollback_failed"),
    suggested_commit_message: "fix(governed): <desc> [op:<op_id>]"
  -> Ledger: READY_TO_COMMIT
  -> CommProtocol: final notification (TUI + voice)
```

**Affected test scoping (deterministic):**
1. Parse changed files -> map to test files via naming convention (`foo.py` -> `test_foo.py`)
2. If mapping found: run matched tests only
3. If no mapping or confidence low: run full test suite for the target package
4. Fallback: run entire `tests/` directory (bounded by timeout)

---

## 3. Critical Design Fixes (from review)

### 3.1 Single op_id end-to-end
ChangeEngine.execute() accepts an `op_id` parameter instead of generating one internally. The orchestrator's `ctx.op_id` flows through every phase, ledger entry, approval record, and result payload.

### 3.2 Durable approval store
`approval_store.py` uses atomic write (tempfile + fsync + rename) + `fcntl.flock()` for cross-process safety. CAS-style state transition: `PENDING -> APPROVED|REJECTED|EXPIRED|SUPERSEDED`. First valid write wins.

### 3.3 Behavioral verify as default
TestRunner becomes the default verify_fn passed to ChangeEngine. AST-only parse is demoted to a pre-check inside SANDBOX, not the VERIFY gate.

### 3.4 Crash reconciliation
On boot, scan ledger for ops in APPLIED state without VERIFIED or ROLLED_BACK. For each: check RollbackArtifact -> attempt rollback -> mark FAILED(reason="crash_recovery").

### 3.5 Lifecycle drain
`stop()` uses `asyncio.wait(active_tasks, timeout=30)` with per-op cancellation and ledger reconciliation.

### 3.6 Ledger dedup key
Key becomes `(op_id, phase, sequence_number)` where sequence auto-increments per op. Multiple entries for same phase are distinct records.

---

## 4. Module Contracts

### 4.1 self_dev_cli.py (new, thin)

```python
# Commands:
#   jarvis self-modify "fix test_foo failing on import"
#   jarvis self-modify "fix test_foo" --target tests/test_foo.py
#   jarvis approve <op_id> [--reason "looks good"]
#   jarvis reject <op_id> --reason "wrong approach"
#   jarvis self-dev-status [<op_id>]

async def handle_self_modify(description, target_files, service) -> str:
    """Build OperationContext, call service.submit(). Returns op_id.
    No side effects -- only parse, build ctx, submit."""

async def handle_approve(op_id, reason, store) -> ApprovalResult:
    """CAS transition: PENDING -> APPROVED. Returns result."""

async def handle_reject(op_id, reason, store) -> ApprovalResult:
    """CAS transition: PENDING -> REJECTED. Returns result."""

async def handle_status(op_id, service) -> str:
    """Query ledger for op state. Returns formatted summary."""
```

### 4.2 test_runner.py (new)

```python
@dataclass(frozen=True)
class TestResult:
    passed: bool
    total: int
    failed: int
    failed_tests: Tuple[str, ...]
    duration_seconds: float
    stdout: str
    flake_suspected: bool  # True if passed on retry after initial failure

class TestRunner:
    def __init__(self, repo_root: Path, timeout: float = 120.0): ...

    async def resolve_affected_tests(self, changed_files) -> Tuple[Path, ...]:
        """Deterministic: name convention -> package fallback -> full suite."""

    async def run(self, test_files, sandbox_dir=None) -> TestResult:
        """Run pytest subprocess. Retry once on failure for flake detection."""
```

### 4.3 approval_store.py (new)

```python
class ApprovalState(enum.Enum):
    PENDING / APPROVED / REJECTED / EXPIRED / SUPERSEDED

@dataclass(frozen=True)
class ApprovalRecord:
    op_id, state, actor, channel, reason, policy_version,
    created_at, decided_at

class ApprovalStore:
    """File-backed, atomic, cross-process safe."""

    def create(self, op_id, policy_version) -> ApprovalRecord:
        """Write PENDING. Atomic: flock + tempfile + fsync + rename."""

    def decide(self, op_id, decision, reason) -> ApprovalRecord:
        """CAS: PENDING -> decision. First valid wins. Late -> SUPERSEDED."""

    def get(self, op_id) -> Optional[ApprovalRecord]

    def expire_stale(self, timeout_seconds=1800.0) -> List[str]:
        """Expire PENDING records older than timeout."""
```

### 4.4 GovernedLoopService extensions (modified)

```python
@dataclass(frozen=True)
class ReadyToCommitPayload:
    op_id: str
    changed_files: Tuple[str, ...]
    provider_id: str
    model_id: str
    routing_reason: str
    verification_summary: str
    rollback_status: str  # "clean" | "rolled_back" | "rollback_failed"
    suggested_commit_message: str

# Modified submit() adds phases: SANDBOX_TEST, APPROVE, VERIFY, READY_TO_COMMIT
```

---

## 5. Error Handling & Rollback

### 5.1 Phase failure matrix

| Phase | Failure | Action | Ledger State | Rollback? |
|-------|---------|--------|-------------|-----------|
| INTAKE | BLOCKED | Stop, emit denial | BLOCKED | No |
| INTAKE | AutonomyGate defer | Stop, emit reason | DEFERRED | No |
| GENERATE | Provider timeout | Failback. All fail -> FAILED | GENERATION_FAILED | No |
| GENERATE | Empty response | FAILED | GENERATION_FAILED | No |
| SANDBOX_TEST | Tests fail | REJECTED, no apply | SANDBOX_FAILED | No |
| SANDBOX_TEST | Pytest timeout | FAILED | SANDBOX_TIMEOUT | No |
| GATE | Gate rejects | Stop | GATE_REJECTED | No |
| APPROVE | Timeout (30min) | EXPIRED, safe path | APPROVAL_EXPIRED | No |
| APPROVE | User rejects | Stop | APPROVAL_REJECTED | No |
| APPLY | Write fails | Rollback via artifact | APPLY_FAILED | Yes |
| VERIFY | Tests fail | Rollback via artifact | VERIFY_FAILED | Yes |
| VERIFY | Pytest timeout | Rollback | VERIFY_TIMEOUT | Yes |
| READY_TO_COMMIT | Notification fails | Log warning, emit result | N/A | No |

### 5.2 Rollback contract
Rollback only needed after APPLY (files on disk changed). Pre-APPLY failures are clean.
Uses ChangeEngine RollbackArtifact. If rollback fails: ROLLBACK_FAILED + ALERT + human intervention.

### 5.3 Crash recovery (boot-time)
1. Scan ledger for ops in APPLIED without VERIFIED/ROLLED_BACK
2. Attempt rollback from artifact -> ROLLED_BACK(reason="crash_recovery")
3. No artifact -> FAILED(reason="crash_recovery_no_artifact") + ALERT
4. Expire stale PENDING approvals

### 5.4 Provider failure isolation
Provider locked at GENERATE. No mid-op switch. Failback logged with routing_reason.
All providers fail -> GENERATION_FAILED, no retry within same op.

### 5.5 Rate limiting
Max 1 concurrent op per repo. Second submit rejected with active op_id message.

---

## 6. Notification Layer

| Phase | TUI Panel | Voice |
|-------|-----------|-------|
| INTAKE accepted | Status: RUNNING | "Starting self-modification: {desc}" |
| GENERATE complete | Show provider, model | Silent |
| SANDBOX_TEST pass | Test summary | Silent |
| SANDBOX_TEST fail | Status: FAILED | "Sandbox tests failed. Candidate rejected." |
| GATE pass | Phase indicator | Silent |
| APPROVE pending | Status: AWAITING_APPROVAL | "Approval needed. Run jarvis approve {op_id}" |
| APPROVE granted | Status: APPROVED | "Approved. Applying changes." |
| APPROVE rejected | Status: REJECTED | "Rejected. No changes applied." |
| APPROVE expired | Status: EXPIRED | "Approval timed out. No changes applied." |
| VERIFY pass | Test summary | Silent |
| VERIFY fail | Status: ROLLED_BACK | "Post-apply tests failed. Changes rolled back." |
| READY_TO_COMMIT | Full payload | "Fix ready to commit. {n} files changed." |

**Hard rules:**
1. Notification failure never blocks pipeline
2. Voice unavailable = skip (safe_say returns False -> continue)
3. TUI not running = skip (drop message, no retry)

**Approval reminder:** Every 5 minutes while PENDING, re-emit notification + voice.

---

## 7. Testing Strategy

```
tests/governance/self_dev/
  test_cli.py            (~8 tests)  CLI entry points
  test_test_runner.py    (~10 tests) TestRunner unit tests
  test_approval_store.py (~12 tests) Durability + concurrency
  test_pipeline_flow.py  (~10 tests) GovernedLoopService extensions
  test_notifications.py  (~6 tests)  Emission + fault isolation
  test_error_handling.py (~8 tests)  Failure matrix coverage
  test_crash_recovery.py (~5 tests)  Boot reconciliation
  test_e2e.py            (~3 tests)  Full vertical slice
```

**Total: ~62 tests.** All provider calls mocked. TestRunner tests use real pytest subprocess against fixture test files.

---

## 8. Architectural Gaps — Phase 1 Scope

| Gap | Phase 1 Approach |
|-----|-----------------|
| Dual-validation drift | Single TestRunner contract shared by SANDBOX_TEST and VERIFY |
| Test flake containment | Retry once + log flake suspicion. No classifier yet |
| Provider stickiness budget | Persist provider+model in ledger. Cost counters deferred |
| Path safety | Resolve + reject ../ and symlinks before APPLY |
| Schema migration | Version field in approval store + ledger. N/N-1 read compat |
| Supervisor bootstrap | Governance never owns its own restart |

---

## 9. Phase Progression

- **Phase 1:** Manual CLI trigger, CLI-only approval, write + verify, no auto-commit
- **Phase 1.5:** Optional auto-commit for SAFE_AUTO in tests/ slice with strict gates
- **Phase 2:** Broader auto-commit by slice/risk tier, TUI approval authority, periodic polling trigger
- **Phase 3:** File watcher, CI webhook triggers, full autonomous tier support

**Hard gates before auto-commit (Phase 1.5):**
- Zero unsafe write incidents across canary window
- Rollback correctness verified repeatedly
- Idempotent decision handling proven under retries/restarts
- Approval timeout and superseded-decision behavior proven
- Provider failover/failback stable under induced faults
