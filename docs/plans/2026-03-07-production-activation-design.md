# Production Activation of the Governed Self-Programming Loop

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Wire the governed self-programming pipeline into the running JARVIS system so that `jarvis self-modify` triggers a full CLASSIFY -> GENERATE -> VALIDATE -> GATE -> [APPROVE] -> APPLY -> VERIFY -> COMPLETE cycle against GCP J-Prime.

**Architecture:** Approach B — a `GovernedLoopService` lifecycle manager owns provider wiring, orchestrator construction, and health probes. The supervisor instantiates it in Zone 6.8, fail-open. CLI commands are the sole authoritative trigger in Phase 1.

**Tech Stack:** Python 3.9+, asyncio, existing PrimeClient/PrimeRouter, existing GovernanceStack, existing CandidateGenerator + FailbackStateMachine.

---

## 1. Module Layout

```
backend/core/ouroboros/governance/
  providers.py               # NEW — PrimeProvider + ClaudeProvider adapters
  governed_loop_service.py   # NEW — GovernedLoopService lifecycle manager
  integration.py             # MODIFY — wire service field into GovernanceStack

unified_supervisor.py        # MODIFY — add Zone 6.8 startup block
<CLI entrypoint>             # MODIFY — add self-modify, approve, reject commands
```

3 touchpoints in governance (2 new, 1 modify), 2 external modifications. No changes to existing governance modules (orchestrator, candidate_generator, approval_provider stay as-is).

---

## 2. Providers (`providers.py`)

Both implement the existing `CandidateProvider` protocol:

```python
@runtime_checkable
class CandidateProvider(Protocol):
    @property
    def provider_name(self) -> str: ...
    async def generate(self, context: OperationContext, deadline: datetime) -> GenerationResult: ...
    async def health_probe(self) -> bool: ...
```

### PrimeProvider

- **Constructor**: takes a `PrimeClient` instance. No side effects.
- **`generate()`**:
  1. Builds a structured prompt from `OperationContext` (target files, description, constraints).
  2. Appends schema enforcement suffix requiring JSON: `{candidates: [{file, content}], model_id, reasoning_summary}`.
  3. Calls `PrimeClient.generate(prompt, system_prompt, max_tokens, temperature=0.2)`.
  4. Parses response via shared `_parse_generation_response()`.
  5. On parse failure: raises `RuntimeError("prime_schema_invalid")` with raw response logged.
  6. On success: returns `GenerationResult` with provenance metadata (`model_id`, `tokens_used`, `latency_ms`, `routing_reason`).
- **`health_probe()`**: delegates to `PrimeClient._check_health()`, returns `True` only if `PrimeStatus.AVAILABLE`.

### ClaudeProvider

- **Constructor**: takes API key, model config, cost limits. No client creation until `generate()`.
- **`generate()`**: same prompt construction, same schema enforcement, calls Claude API via `anthropic.AsyncAnthropic`.
- **`health_probe()`**: lightweight API ping with 1-token max.
- **Cost gate**: constructor takes `max_cost_per_op` and `daily_budget`. Before each call, checks accumulated spend. If over budget: raises `RuntimeError("claude_budget_exhausted")` triggering FSM to `QUEUE_ONLY`.

### Shared Schema Validation

`_parse_generation_response(raw: str, provider_name: str) -> GenerationResult`:
- Expects JSON with `candidates` array (each has `file` and `content` keys).
- Validates all candidates have non-empty `file` and `content`.
- Rejects if any candidate `content` fails `ast.parse()` (Python files only).
- Returns structured `GenerationResult` or raises `RuntimeError` with deterministic reason code.

Deterministic failure reason codes: `prime_schema_invalid`, `prime_timeout`, `prime_unhealthy`, `claude_budget_exhausted`, `claude_schema_invalid`, `claude_timeout`.

---

## 3. GovernedLoopService (`governed_loop_service.py`)

Thin lifecycle manager. No domain logic, just wiring and coordination.

### Constructor

```python
class GovernedLoopService:
    def __init__(
        self,
        stack: GovernanceStack,
        prime_client: Optional[PrimeClient],
        config: GovernedLoopConfig,
    ) -> None:
        # Store refs only. No side effects. No async. No connections.
```

### GovernedLoopConfig

```python
@dataclass(frozen=True)
class GovernedLoopConfig:
    project_root: Path
    claude_api_key: Optional[str] = None
    claude_model: str = "claude-sonnet-4-20250514"
    claude_max_cost_per_op: float = 0.50
    claude_daily_budget: float = 10.00
    generation_timeout_s: float = 120.0
    approval_timeout_s: float = 600.0
    health_probe_interval_s: float = 30.0
    max_concurrent_ops: int = 2
    initial_canary_slices: Tuple[str, ...] = ("tests/",)
```

`from_env(args)` classmethod reads env vars with safe defaults. No new argparse flags in Phase 1.

### Service States

```python
class ServiceState(Enum):
    INACTIVE = auto()    # Constructed, not started
    STARTING = auto()    # start() in progress
    ACTIVE = auto()      # Accepting submit() calls
    DEGRADED = auto()    # Active but primary provider down
    STOPPING = auto()    # stop() in progress, draining ops
    FAILED = auto()      # start() failed, structured reason available
```

### Lifecycle Methods

**`start()`**:
1. Build PrimeProvider (if prime_client available and healthy).
2. Build ClaudeProvider (if API key configured).
3. Build CandidateGenerator with FailbackStateMachine (primary + fallback).
4. Build CLIApprovalProvider.
5. Build GovernedOrchestrator.
6. Register initial canary slices (idempotent, validated — nonexistent path is startup warning).
7. Start background health probe loop (isolated from generation budget/tokens).
8. Attach all to GovernanceStack fields.
9. On ANY failure: deterministic teardown of partial components, set FAILED with reason code + startup trace id.

**`stop()`**:
1. Cancel health probe loop.
2. Drain in-flight operations (30s timeout).
3. In-flight APPROVE waits cancelled as EXPIRED with audit event.
4. Detach from GovernanceStack.

**`submit(ctx: OperationContext) -> OperationResult`**:
1. Validate service is ACTIVE or DEGRADED (reject if INACTIVE/STOPPING/FAILED).
2. Concurrency check: if active ops >= `max_concurrent_ops`, return BUSY result.
3. Dedupe check: `(op_id, policy_version, trigger_source)` idempotency.
4. Snapshot `policy_version` into ctx.
5. Delegate to `orchestrator.run(ctx)`.
6. Return `OperationResult` (stable result contract; OperationContext stays internal/ledgered).

**`health() -> Dict[str, Any]`**: returns state, provider FSM state, active ops count, canary slices, uptime.

### OperationResult

```python
@dataclass(frozen=True)
class OperationResult:
    op_id: str
    terminal_phase: OperationPhase
    provider_used: Optional[str]
    generation_duration_s: Optional[float]
    total_duration_s: float
    reason_code: str
    trigger_source: str
```

### Key Invariants

- `submit()` rejects if state is not ACTIVE or DEGRADED.
- `start()` is idempotent (no-op if already ACTIVE).
- If both providers fail construction, service goes ACTIVE with QUEUE_ONLY FSM state.
- Health probe loop errors are tracked separately from generation errors.
- Probes never consume generation budget/tokens.

---

## 4. Supervisor Integration (Zone 6.8)

Added after existing governance startup at ~line 85876 of `unified_supervisor.py`:

```python
# ---- Zone 6.8: Governed Self-Programming Loop ----
if self._governance_stack and self._governance_stack._started:
    try:
        from backend.core.ouroboros.governance.governed_loop_service import (
            GovernedLoopService, GovernedLoopConfig,
        )
        _loop_config = GovernedLoopConfig.from_env(self._args)
        self._governed_loop = GovernedLoopService(
            stack=self._governance_stack,
            prime_client=getattr(self, "_prime_client", None),
            config=_loop_config,
        )
        await asyncio.wait_for(
            self._governed_loop.start(),
            timeout=30.0,
        )
        self.logger.info(
            "[Kernel] Zone 6.8 governed loop: %s",
            self._governed_loop.health(),
        )
    except Exception as exc:
        self._governed_loop = None
        self.logger.warning(
            "[Kernel] Zone 6.8 governed loop failed: %s -- skipped", exc,
        )
```

### Key Behaviors

- **Fail-open**: governed loop failure does not block supervisor startup. Core JARVIS continues in read-only planning mode.
- **Conditional**: only attempts if GovernanceStack already started.
- **PrimeClient optional**: passes None if unavailable; service handles gracefully.
- **Shutdown**: `_governed_loop.stop()` called during supervisor shutdown, before governance stack stops.

New supervisor field:
```python
self._governed_loop: Optional["GovernedLoopService"] = None
```

---

## 5. CLI Trigger

### Commands

```
jarvis self-modify --target <file_or_dir> --goal "<description>" [--op-id <id>] [--dry-run]
jarvis approve <op_id>
jarvis reject <op_id> --reason "<reason>"
```

CLI lives outside governance package (command layer). It imports `GovernedLoopService` lazily at invocation time.

### `self-modify` Behavior

1. Validate target path exists and falls within a registered canary slice.
2. Build `OperationContext.create(target_files=(...), description=goal, trigger_source="cli")`.
3. Call `service.submit(ctx)`.
4. Print `OperationResult`: terminal phase, provider used, duration, op_id.
5. `--dry-run`: runs CLASSIFY + ROUTE only, prints risk tier and routing decision without generating or applying.

### `approve` / `reject` Behavior

Calls `CLIApprovalProvider.approve()` / `.reject()` on the service's approval provider. The orchestrator's `await_decision()` unblocks immediately. Audit fields persisted: actor, channel=`cli`, timestamp, reason, policy_version.

Race handling: if decision arrives after timeout, return `expired`/`superseded` with explicit reason code.

### Error Cases

| Scenario | Output |
|----------|--------|
| Governed loop not started | "Governed loop is not active. Start JARVIS with governance enabled." |
| Target outside canary slice | "Target `X` not in registered canary slice. Registered: `tests/`" |
| Service BUSY | "Pipeline at capacity (N active ops). Try again shortly." |
| Service QUEUE_ONLY | "All providers unavailable. Operation queued for provider recovery." |

---

## 6. TUI & Voice Notification (Notify-Only)

No new files. Hooks into existing infrastructure.

### TUI Notifications

When the orchestrator records a ledger entry at APPROVE phase with `waiting_approval: True`, the existing CommProtocol/TUITransport receives it.

New message payload fields:
- `message_type`: `APPROVAL_PENDING`
- `op_id`, `description`, `target_files`, `risk_tier`
- `approval_deadline_ns` (absolute, not relative — deterministic timeout rendering)
- `notification_id`: `hash(op_id + phase_seq + channel)` for dedupe

TUI renders pending approvals in existing notification area. No approve/reject buttons in Phase 1. Shows exact CLI command: `jarvis approve <op_id>` and `jarvis reject <op_id> --reason "..."`.

### Voice Notifications

On `APPROVAL_PENDING`, fire via existing `safe_say()`:

> "Derek, I'd like to modify test_utils.py to add edge case coverage. This is approval-required. Use the CLI to approve or reject op test zero zero one."

Constraints:
- Notify-only, no voice-based approval in Phase 1.
- Respects quiet hours (existing speech gate).
- One announcement per op (idempotent notification_id).
- Hard debounce: max 1 approval announcement per 60s globally.
- Suppressed if CLI already responded before voice fires.

---

## 7. Failure Matrix

| Failure | Detection | Response | Terminal State |
|---------|-----------|----------|----------------|
| PrimeClient unavailable at startup | `health_probe()` False | Service DEGRADED, Claude fallback | ACTIVE (degraded) |
| Both providers unavailable at startup | Both probes fail | ACTIVE with QUEUE_ONLY FSM | ACTIVE (queue-only) |
| `GovernedLoopService.start()` throws | Zone 6.8 try/except | `_governed_loop = None`, supervisor continues | Service FAILED |
| Prime schema parse failure | `_parse_generation_response` raises | `prime_schema_invalid`, FSM records, retry or fallback | Per FSM |
| Claude budget exhausted | Cost gate in ClaudeProvider | `claude_budget_exhausted`, FSM QUEUE_ONLY | QUEUE_ONLY until reset |
| Target outside canary slice | `submit()` pre-check | Reject: `target_not_in_canary` | Never enters pipeline |
| Approval timeout | `await_decision()` EXPIRED | Op EXPIRED, ledger records | EXPIRED |
| Change engine failure | Raises or `success=False` | Rollback, op POSTMORTEM | POSTMORTEM |
| Service at capacity | Active ops >= max | `submit()` returns BUSY | Never enters pipeline |
| Shutdown with in-flight APPROVE | `stop()` drain timeout | Cancel as EXPIRED, audit event | EXPIRED |
| Health probe loop crash | Task exception handler | Log, restart with backoff | Probe continues |

---

## 8. Rollout Plan

### Step 0: Pre-activation (done)
All governance modules built and tested (517 tests passing). GovernanceStack has optional fields ready.

### Step 1: Providers + Service (this implementation)
- Build `providers.py` with PrimeProvider + ClaudeProvider.
- Build `governed_loop_service.py`.
- Wire Zone 6.8 in supervisor.
- Add CLI commands (self-modify, approve, reject).
- Register `tests/` as first canary slice.
- TUI + voice notify-only hooks.

### Step 2: Soak Test (1-2 weeks)
- Run 10-20 manual `self-modify` operations against `tests/`.
- Validate: ledger entries, provider routing, approval flow, rollback.
- Tune: schema prompt, generation temperature, timeouts.
- Measure: parse failure rate, latency distribution, cost per op.

### Step 3: Expand Canary (after soak)
- Register `backend/core/utils/` slice.
- Lower approval threshold for `tests/` (SAFE_AUTO for test-only ops).
- Add utility module canary with APPROVAL_REQUIRED.

### Step 4: Toward Autonomy (Phase 2)
- Intent engine (what triggers self-modification).
- Multi-repo awareness (JARVIS + prime + reactor-core).
- Reactive triggers (stack traces, git hooks) in sandbox mode.
- TUI authoritative approval adapter.
- Voice notify with speaker verification.

---

## 9. Post-Activation Roadmap

After production activation is stable, the path to "JARVIS developing itself across repos and communicating in real-time" requires these layers in order:

1. **Intent Engine** — identifies what to improve (test gaps, error patterns, performance bottlenecks). Stack-trace interceptor is the best first reactive trigger.
2. **Multi-Repo Coordinator** — repo registry, cross-repo blast radius, coordinated commits across JARVIS/prime/reactor-core.
3. **Real-Time Communication** — persistent conversational interface where JARVIS narrates operations and asks questions mid-pipeline.
4. **Selective Autonomy** — policy-gated reactive triggers in governed mode, per-slice trust levels, graduated human oversight reduction.

Each layer builds on the previous. The governed loop service provides the foundation for all of them.
