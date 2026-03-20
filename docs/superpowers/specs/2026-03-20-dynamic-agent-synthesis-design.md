# Dynamic Agent Synthesis (DAS) — Design Spec

> **Status:** Approved for implementation (2026-03-20)
> **Repos:** JARVIS · J-Prime · Reactor-Core
> **Skill invoked:** `superpowers:brainstorming`

---

## 1. Problem Statement

JARVIS routes tasks to agents registered in `AgentCapabilityIndex`. When no capable agent exists for a
task type, `resolve_capability()` returns `("computer_use", None)` — the universal silent fallback
(hardcoded at lines 1560 and 1594 of `agent_registry.py`). This is a silent mismatch: the task is
served by a generic computer-use agent that may not have the right domain tools, resulting in degraded
execution or silent failure.

The actual signature is:
```python
def resolve_capability(
    self,
    goal: str,
    target_app: Optional[str],
    task_type: Optional[str] = None,
) -> Tuple[str, Optional[str]]:  # (primary_tool, fallback_tool | None)
```

**Root cause:** The capability registry is static. New task types not foreseen at build time have no path
to resolution beyond the `"computer_use"` universal fallback.

**Solution:** Dynamic Agent Synthesis — when a capability gap is detected, JARVIS triggers Ouroboros to
synthesize a new agent at runtime, loads it under a canary, and graduates it via domain-trust gates.
No hardcoded agents, no manual registrations, fully async and policy-driven.

---

## 2. Architecture Overview

```
Command Path (never blocked)
────────────────────────────────────────────────────────
User Command → UnifiedCommandProcessor / ReasoningChainOrchestrator
    ↓
AgentCapabilityIndex.resolve_capability(goal, target_app, task_type)
    ├─ specific tool returned  → normal execution
    └─ "computer_use" fallback → fire GapSignalBus event (put_nowait, 0ms overhead)
                    ↓
           GapResolutionProtocol (async, out of band)
                    ↓
           CapabilityGapSensor (Ouroboros intake sensor)
                    ↓
           Ouroboros pipeline: CLASSIFY→ROUTE→GENERATE→VALIDATE→GATE→APPROVE→APPLY
                    ↓
           AgentSynthesisLoader (artifact safety + canary load)
                    ↓
           DomainTrustLedger (graduation gates)
                    ↓
           AgentRegistry (live re-route)
```

The command path is **never blocked**. All synthesis work is fully async and out-of-band.
The original command is handled via tri-mode routing (A/B/C) while synthesis proceeds.

---

## 3. Key Definitions

| Term | Definition |
|---|---|
| **Capability Gap** | `resolve_capability()` returns `("computer_use", None)` — no domain-specific agent handles `(goal, target_app, task_type)` |
| **dedupe_key** | `sha256(task_type + target_app + capability_signature)` — stable semantic identity of the gap |
| **attempt_key** | `sha256(dedupe_key + policy_version + manifest_snapshot_hash)` — per synthesis attempt identity |
| **domain_id** | `normalize(task_type) + ":" + normalize(target_app or "any")` — never includes `risk_class` |
| **das_canary_key** | `sha256(session_id + normalized_command)` — per-session-per-command stable key used **only** for DAS canary routing. Separate from the existing UUID `command_id`. Stable across retries in the same session. |
| **Canary** | Synthesized agent serving ~10% of domain traffic before graduation |
| **Domain Trust Ledger** | Per-domain append-only journal of synthesis outcomes; drives approval tier |

`normalized_command` = lowercased, stripped, whitespace-collapsed command text.
`session_id` = generated once at session start (UUID) and stored on the session object.
`das_canary_key` does **not** replace the existing UUID `command_id` used for request tracking.

---

## 4. Dual-Source Gap Detection

Gap events originate from two sources. Both feed the same `GapSignalBus`.

### 4.1 Primary — AgentCapabilityIndex Fallback

Modify `resolve_capability()` to detect the universal-fallback case. The existing inline resolution
logic is extracted into a new private method `_resolve_internal()` (refactored from the existing body
at lines 1541–1594), and the detection wrapper is added around it:

```python
# backend/neural_mesh/registry/agent_registry.py
def resolve_capability(
    self,
    goal: str,
    target_app: Optional[str],
    task_type: Optional[str] = None,
) -> Tuple[str, Optional[str]]:
    primary, fallback = self._resolve_internal(goal, target_app, task_type)
    if primary == "computer_use" and fallback is None:
        # Universal fallback — capability gap detected
        get_gap_signal_bus().emit_nowait(CapabilityGapEvent(
            goal=goal,
            task_type=task_type or "",
            target_app=target_app or "",
            source="primary_fallback",
        ))
    return primary, fallback

def _resolve_internal(
    self,
    goal: str,
    target_app: Optional[str],
    task_type: Optional[str] = None,
) -> Tuple[str, Optional[str]]:
    # Contains the current body of resolve_capability() verbatim (lines 1547–1594).
    # No logic change — only extracted to enable gap detection wrapper.
    ...
```

`emit_nowait` uses `asyncio.Queue.put_nowait()` — never blocks the command path.

### 4.2 Secondary — J-Prime Advisory Hint

`ExecutionPlanner` may include `"capability_gap_hint"` in its plan response when it cannot resolve a
tool from the live `capability_index`. This is **advisory only** — it informs the dedup lock but never
substitutes for the primary detection path.

```python
# jarvis_prime/reasoning/graph_nodes/execution_planner.py
# Added to plan_dict when _resolve_tool_from_index returns "":
plan_dict["capability_gap_hint"] = {
    "task_type": task_type,
    "target_app": target_app,
    "reason": "no_index_match",
}
```

When `GapResolutionProtocol` receives a J-Prime hint with no corresponding primary event, it logs it
but takes no action (advisory-only contract).

---

## 5. Single-Flight Dedup Lock

To prevent synthesis storms when many commands hit the same gap simultaneously, a single-flight lock
prevents duplicate concurrent synthesis for the same `dedupe_key`:

```python
# backend/neural_mesh/synthesis/gap_resolution_protocol.py
_in_flight: Dict[str, asyncio.Event] = {}  # dedupe_key → completion event

async def handle_gap_event(self, event: CapabilityGapEvent) -> None:
    dedupe_key = _compute_dedupe_key(event.task_type, event.target_app)
    if dedupe_key in self._in_flight:
        # Synthesis already in progress — await completion, then return.
        await asyncio.wait_for(self._in_flight[dedupe_key].wait(), timeout=120.0)
        return
    evt = asyncio.Event()
    self._in_flight[dedupe_key] = evt
    try:
        await self._synthesize(event, dedupe_key)
    finally:
        evt.set()
        self._in_flight.pop(dedupe_key, None)
```

Any burst of identical gap events (same `dedupe_key`) collapses to a single synthesis operation.
Subsequent arrivals wait and return once synthesis completes.

---

## 6. Policy-Routed Tri-Mode Gap Resolution Protocol

Every gap is classified into exactly one resolution mode **before** synthesis starts.

### Decision Matrix

| Mode | Trigger Conditions | Immediate Response | Synthesis Action |
|---|---|---|---|
| **A — Fail Fast** | `risk_class=high` OR `idempotent=false` | Return structured error + user notification | Synthesize but do not auto-route |
| **B — Pending Queue** | `idempotent=true` AND `user_critical=true` | Enqueue command in `SynthesisCommandQueue` | Synthesize; replay queue on graduation |
| **C — Parallel Fallback** | `read_only=true` OR `assistive=true` | Execute via best-available fallback | Synthesize in parallel; switch on graduation |

### Mode A Details

- Returns `CapabilityGapError(mode="A", task_type=..., retry_after_synthesis=True)` to caller.
- JARVIS narrates to user: *"I don't have an agent capable of [X] yet. Building one now — you'll be
  able to try again in a few minutes."*
- Synthesized agent undergoes full canary + trust graduation before any routing.

### Mode B Details

- Commands are enqueued with their full `das_canary_key`, payload snapshot, and expiry TTL (default 30 min).
- On agent graduation: `SynthesisCommandQueue.replay(domain_id)` re-routes enqueued commands in
  arrival order.
- **"Semantically superseded"**: an entry is superseded when a newer entry with the identical
  `dedupe_key` arrives after the first, indicating the user re-issued the same command. The older
  entry transitions to `REPLAY_STALE`. The newest entry is the one replayed.
- Entries past TTL also transition to `REPLAY_STALE`.

### Mode C Details

- **Hard read-only enforcement at the adapter layer** — not at the call site. The execution adapter
  checks `CapabilityScope.read_only` before any tool call. If a Mode-C agent attempts a write,
  the adapter raises `ReadOnlyViolationError` and rolls back.
- Fallback quality is tracked in telemetry for comparison against synthesized agent post-graduation.

### Mode Classification Policy

```python
# backend/neural_mesh/synthesis/gap_resolution_protocol.py
def _classify_mode(self, event: CapabilityGapEvent, policy: GapResolutionPolicy) -> ResolutionMode:
    if event.source == "dream_advisory":
        return ResolutionMode.C  # Dream-sourced events always Mode C
    if policy.risk_class == "high" or not policy.idempotent:
        return ResolutionMode.A
    if policy.user_critical and policy.idempotent:
        return ResolutionMode.B
    return ResolutionMode.C
```

### gap_resolution_policy.yaml

Location: `backend/neural_mesh/synthesis/gap_resolution_policy.yaml`

Schema and example:

```yaml
version: "1.0"

# Default policy applied when no domain-specific override matches
defaults:
  risk_class: "medium"       # "low" | "medium" | "high" | "critical"
  idempotent: true
  user_critical: false
  read_only: false
  assistive: false
  slo_p99_ms: 5000           # default canary graduation latency SLO

# Domain-specific overrides. domain_id = "task_type:target_app"
# "any" matches any target_app for that task_type.
domain_overrides:
  "file_edit:any":
    risk_class: "high"
    idempotent: false
  "calendar_query:any":
    idempotent: true
    user_critical: true
    slo_p99_ms: 3000
  "screen_observation:any":
    read_only: true
    assistive: true
    slo_p99_ms: 2000
```

`GapResolutionPolicy` is loaded once at startup and reloaded on SIGHUP. Per-domain overrides merge
with defaults (override keys win; unspecified keys inherit from `defaults`).

### das_canary_key Generation

`das_canary_key` is generated in `unified_command_processor.py` at the point where a command is
accepted from the user. It is used **only** for DAS canary routing — the existing UUID-based
`command_id` at lines 3017 and 3543 is **not modified**.

```python
# backend/api/unified_command_processor.py
# New: generated alongside the existing command_id, not replacing it.
# session_id: UUID stored on the session object, generated once at session start.
das_canary_key = hashlib.sha256(
    f"{session_id}:{_normalize_command(command_text)}".encode()
).hexdigest()
```

`session_id` is a UUID generated once when the session handler is first created and stored on the
session state object. It is stable for the lifetime of the session, making `das_canary_key` stable
across retries for the same command within the same session.

---

## 7. Ouroboros Integration — CapabilityGapSensor

`CapabilityGapSensor` follows the exact same pattern as `OpportunityMinerSensor` — a standalone class
with an async `_poll_loop()` method, no base class.

Location: `backend/core/ouroboros/governance/intake/sensors/capability_gap_sensor.py`

```python
# backend/core/ouroboros/governance/intake/sensors/capability_gap_sensor.py
from backend.core.ouroboros.governance.intake.intent_envelope import make_envelope

class CapabilityGapSensor:
    def __init__(self, intake_router: UnifiedIntakeRouter, repo: str) -> None:
        self._router = intake_router
        self._repo = repo
        self._gap_bus: GapSignalBus = get_gap_signal_bus()

    def start(self) -> None:
        asyncio.create_task(self._poll_loop(), name="capability_gap_sensor_poll")

    async def _poll_loop(self) -> None:
        async for event in self._gap_bus.consume():
            try:
                envelope = make_envelope(
                    source="capability_gap",
                    description=f"Synthesize agent for {event.task_type}:{event.target_app}",
                    target_files=(
                        f"backend/neural_mesh/synthesis/agents/{event.domain_id}.py",
                    ),
                    repo=self._repo,
                    confidence=0.9,
                    urgency=event.resolution_mode.urgency_value(),
                    evidence={
                        "task_type": event.task_type,
                        "target_app": event.target_app,
                        "dedupe_key": event.dedupe_key,
                        "attempt_key": event.attempt_key,
                        "resolution_mode": event.resolution_mode.value,
                        "domain_id": event.domain_id,
                    },
                    requires_human_ack=(event.resolution_mode == ResolutionMode.A),
                )
                await self._router.submit(envelope)
            except Exception:
                logger.exception("CapabilityGapSensor: failed to submit envelope")
```

`make_envelope` is imported from `backend.core.ouroboros.governance.intake.intent_envelope` (the
same import used by all other sensors). It auto-generates `signal_id`, `causal_id`,
`idempotency_key`, `lease_id`, `submitted_at`, and `dedup_key`.

`target_files` is set to the expected output path of the synthesized module
(`backend/neural_mesh/synthesis/agents/{domain_id}.py`). This satisfies the `non-empty` invariant.

`IntentEnvelope._VALID_SOURCES` (the `frozenset` literal at line 20 of `intent_envelope.py`) gains
`"capability_gap"` as a valid source value.

---

## 8. Three-Stage Artifact Safety

Every synthesized artifact must pass all three stages before loading.

### Stage 1 — AST Scan (static analysis)

Parse the artifact with Python's `ast` module. Reject the artifact if any of the following patterns
appear at module scope:

- Dynamic code execution via dangerous builtin names: `"eval"`, `"exec"`, `"__import__"` as
  `ast.Call` nodes with an `ast.Name` id matching these strings
- Shell invocation via `os` attribute access: `ast.Attribute` nodes with `attr="system"` on an `os`
  name, or any `os.popen`, `os.execvp`, `os.execve` family
- Process spawning: any `import subprocess` or `from subprocess import ...`
- `import ctypes`
- `import socket`

This check is purely structural — no code is executed during the scan.

### Stage 2 — Sandboxed Import

Load the module via `importlib` with a restricted `__builtins__` namespace. The sandbox import
allowlist is defined in `backend/neural_mesh/synthesis/sandbox_allowlist.yaml`:

```yaml
# Permitted standard library modules for synthesized agents
allowed_imports:
  - asyncio
  - collections
  - dataclasses
  - datetime
  - enum
  - functools
  - hashlib
  - inspect
  - json
  - logging
  - math
  - pathlib
  - re
  - time
  - typing
  - uuid
  # JARVIS internal (explicit allowlist)
  - backend.neural_mesh.base.base_neural_mesh_agent
  - backend.neural_mesh.data_models
  - backend.neural_mesh.registry.agent_registry
```

Any import not on the allowlist raises `SandboxImportError` and quarantines the artifact.

### Stage 3 — Cross-Repo Contract Gate

The synthesized agent must declare at module scope:
- `AGENT_MANIFEST: CapabilityManifest` — capability declaration using the existing `CapabilityManifest`
  dataclass from `backend.neural_mesh.data_models` (line 718). Must pass `AgentCapabilityIndex`
  schema validation.
- `side_effect_policy: SideEffectPolicy` — what the agent writes/modifies
- `compensation_strategy: CompensationStrategy` — how to undo its effects on rollback

If any are missing or fail schema validation, the artifact is quarantined with state
`QUARANTINED_PENDING_REVIEW`. A quarantine re-attempt generates a new `attempt_key` with
`retry_of_attempt_key` link for the audit chain.

---

## 9. Domain Trust Ledger

Per-domain trust authority. Drives approval mode and canary graduation thresholds.

### Schema

```python
@dataclass
class DomainTrustRecord:
    domain_id: str            # "normalize(task_type):normalize(target_app or 'any')"
    tier: int                 # 0=always-human, 1=human-first, 2=graduating, 3=auto+guardrails
    trust_score: float        # computed (see formula below)
    total_attempts: int
    successful_runs: int
    rollback_count: int
    incident_count: int
    audit_pass_count: int
    last_updated_ms: int
    journal: List[TrustJournalEntry]  # append-only
```

### Trust Score Formula

```
trust_score = (
    0.40 * (successful_runs / max(total_attempts, 1))
  - 0.30 * (rollback_count  / max(total_attempts, 1))
  - 0.20 * (incident_count  / max(total_attempts, 1))
  + 0.10 * (audit_pass_count / max(total_attempts, 1))
)
```

This formula is Goodhart-resistant: gaming `successful_runs` is offset by the rollback and incident
penalties. Audit pass rate provides an independent signal. All four denominators are guarded with
`max(..., 1)` to prevent division-by-zero.

### Tier Graduation Gates

| Tier | Entry Condition | Approval Mode |
|---|---|---|
| tier_0 | `risk_class=critical` OR `compensation_strategy.strategy_type="manual"` | Always human, never graduates |
| tier_1 | New domain (default) | Human approves each synthesis |
| tier_2 | `trust_score >= 0.70` AND `total_attempts >= 5` | Human approves first, then canary auto |
| tier_3 | `trust_score >= 0.90` AND `total_attempts >= 20` AND `incident_count == 0` | Auto with guardrails |

Graduation is **never retroactive** — a domain can only move tier by tier, and any incident resets
to tier_1 immediately.

---

## 10. Canary Activation and Graduation

### Sticky Deterministic Routing

```python
def _route_to_canary(self, domain_id: str, das_canary_key: str) -> bool:
    bucket = int(hashlib.sha256(f"{domain_id}:{das_canary_key}".encode()).hexdigest(), 16) % 10
    return bucket == 0  # stable 10% cohort
```

The same `(domain_id, das_canary_key)` pair always routes to the same cohort. Because
`das_canary_key` uses only `session_id` + `normalized_command` (no random component), retrying the
same command in the same session always hits the same routing bucket. This prevents flapping and
enables reproducible debugging.

### Hybrid Graduation Gate

A canary graduates when **all** of the following are true:

```
(requests >= 10 OR (elapsed_s >= 300 AND distinct_sessions >= 3))
AND error_rate < 0.01
AND p99_latency_ms <= domain_slo_p99_ms
AND no_incidents_in_window
```

`domain_slo_p99_ms` is loaded from `gap_resolution_policy.yaml` (the `slo_p99_ms` field under the
matching domain override, or `defaults.slo_p99_ms = 5000` if no override exists).

### Versioned Registry Rollback

Rollback does NOT pop `sys.modules` — module references leak into already-executing coroutines.
Instead, `AgentRegistry._version` is incremented and all new routing decisions use the new version.
In-flight executions using the old version complete normally; no new executions are dispatched to
the rolled-back version.

```python
# backend/neural_mesh/registry/agent_registry.py
async def rollback_agent(self, domain_id: str, reason: str) -> None:
    async with self._lock:
        self._version += 1
        self._rollback_log.append(RollbackEntry(
            domain_id=domain_id,
            version=self._version,
            reason=reason,
            timestamp_ms=int(time.time() * 1000),
        ))
        self._active_routes[domain_id] = self._stable_routes[domain_id]
```

---

## 11. 19-State Machine (Verbatim)

```
State                       Valid Next States
──────────────────────────────────────────────────────────────────────────────
GAP_DETECTED                → GAP_COALESCING
GAP_COALESCING              → GAP_COALESCED
GAP_COALESCED               → ROUTE_DECIDED_A | ROUTE_DECIDED_B | ROUTE_DECIDED_C
ROUTE_DECIDED_A             → SYNTH_PENDING
ROUTE_DECIDED_B             → SYNTH_PENDING
ROUTE_DECIDED_C             → SYNTH_PENDING
SYNTH_PENDING               → SYNTH_TIMEOUT | SYNTH_REJECTED | ARTIFACT_WRITTEN
SYNTH_TIMEOUT               → CLOSED_UNRESOLVED
SYNTH_REJECTED              → CLOSED_UNRESOLVED
ARTIFACT_WRITTEN            → ARTIFACT_VERIFIED | QUARANTINED_PENDING_REVIEW
QUARANTINED_PENDING_REVIEW  → SYNTH_PENDING (retry, new attempt_key) |
                              CLOSED_UNRESOLVED (max retries exhausted)
ARTIFACT_VERIFIED           → CANARY_ACTIVE
CANARY_ACTIVE               → CANARY_ROLLED_BACK | AGENT_GRADUATED
CANARY_ROLLED_BACK          → CLOSED_UNRESOLVED
AGENT_GRADUATED             → REPLAY_AUTHORIZED | CLOSED_RESOLVED
REPLAY_AUTHORIZED           → REPLAY_STALE (TTL expired or superseded) |
                              CLOSED_RESOLVED (replay succeeded)
REPLAY_STALE                → CLOSED_UNRESOLVED
CLOSED_RESOLVED             → (terminal)
CLOSED_UNRESOLVED           → (terminal)
──────────────────────────────────────────────────────────────────────────────
Notes:
- QUARANTINED_PENDING_REVIEW → CLOSED_UNRESOLVED fires when retry_count >= DAS_QUARANTINE_MAX_RETRIES
- REPLAY_AUTHORIZED → REPLAY_STALE fires when Mode B queue entry TTL expired or superseded by newer entry
- All state transitions are recorded in the Domain Trust Ledger journal
```

---

## 12. Failure Class Taxonomy

| Class | Example | Detection | DAS Behavior |
|---|---|---|---|
| **Transient** | LLM rate limit, timeout | `SynthTimeoutError` or HTTP 429/503 | Retry with exponential backoff (max 3 attempts) |
| **Structural** | AST safety scan fail, missing manifest field | Stage 1/2/3 raises | Quarantine; surface to human |
| **Semantic** | Synthesized agent solves wrong task | Canary error_rate spike | Canary rollback; log semantic drift signal |
| **Conflict** | Two gaps synthesize overlapping agents | `domain_id` collision in AgentRegistry | `AGENT_SYNTHESIS_CONFLICT` event; arbitration via trust score (higher score wins) |
| **Oscillation** | Repeated route flip between agents | `DAS_OSCILLATION_FLIP_THRESHOLD` flips in `DAS_OSCILLATION_WINDOW_S` | `ROUTING_OSCILLATION_DETECTED` event; domain routing frozen for `DAS_OSCILLATION_FREEZE_S` seconds |

---

## 13. Synthesized Agent Contract

Every synthesized agent module must export at module scope:

```python
AGENT_MANIFEST: CapabilityManifest    # from backend.neural_mesh.data_models
side_effect_policy: SideEffectPolicy
compensation_strategy: CompensationStrategy

class SynthesizedAgent(BaseNeuralMeshAgent):
    ...
```

### CompensationStrategy Interface

```python
# backend/neural_mesh/synthesis/agent_synthesis_loader.py
@dataclass
class CompensationStrategy:
    strategy_type: Literal["rollback_file", "reverse_api_call", "noop", "manual"]
    # rollback_file: restore files from a snapshot taken before execution
    # reverse_api_call: call a defined undo endpoint
    # noop: side effects are fully idempotent; no compensation needed
    # manual: human intervention required; auto-rollback is not possible

    snapshot_paths: Tuple[str, ...]  # populated for rollback_file
    undo_endpoint: Optional[str]     # populated for reverse_api_call
    manual_instructions: str         # populated for manual (surfaced in UI)
```

`strategy_type="manual"` forces `tier_0` classification — the domain can never auto-graduate.
`CapabilityManifest` is the existing dataclass from `backend.neural_mesh.data_models:718`.
`SideEffectPolicy` is checked by the Mode-C read-only adapter before any tool dispatch.

### SideEffectPolicy Interface

```python
@dataclass
class SideEffectPolicy:
    writes_files: bool
    calls_external_apis: bool
    modifies_system_state: bool
    read_only: bool  # True only when all three write flags are False
```

---

## 14. Trinity Consciousness Integration

Trinity is partially implemented. DAS integrates conservatively as an **observer-only**.

| Engine | DAS Signal | Direction | Status |
|---|---|---|---|
| **HealthCortex** | Synthesis latency, canary error rate, quarantine rate | DAS → Trinity (emit) | Active — wrapped in try/except |
| **MemoryEngine** | Per-domain synthesis outcomes, trust score history | DAS → Trinity (emit) | Active — wrapped in try/except |
| **DreamEngine** | Pre-synthesis of predicted future gaps during idle cycles | Trinity → DAS (advisory) | **Deferred to follow-up spec** |
| **ProphecyEngine** | Elevate strategic capability gaps to Mode B urgency | Trinity → DAS (advisory) | **Deferred to follow-up spec** |

DreamEngine and ProphecyEngine integration requires DreamEngine to be fully operational.
That integration will be specified separately once Trinity reaches full implementation.
The `TRINITY_DREAM_DAS_ENABLED` env var is defined but defaults to `false`; setting it to `true`
is a no-op in this implementation iteration.

DAS proceeds correctly whether Trinity is fully operational, partially operational, or absent.
All Trinity emit calls are wrapped in `try/except Exception` with fallback to no-op.

---

## 15. Cross-Repo Integration

### JARVIS (this repo)

| File | Change |
|---|---|
| `backend/neural_mesh/synthesis/__init__.py` | **New** — package init |
| `backend/neural_mesh/synthesis/gap_signal_bus.py` | **New** — asyncio.Queue broadcaster |
| `backend/neural_mesh/synthesis/gap_resolution_protocol.py` | **New** — dedup lock + tri-mode routing |
| `backend/neural_mesh/synthesis/domain_trust_ledger.py` | **New** — per-domain trust journal |
| `backend/neural_mesh/synthesis/agent_synthesis_loader.py` | **New** — artifact safety + canary load; defines `CompensationStrategy`, `SideEffectPolicy` |
| `backend/neural_mesh/synthesis/synthesis_command_queue.py` | **New** — Mode B pending queue |
| `backend/neural_mesh/synthesis/sandbox_allowlist.yaml` | **New** — Stage 2 import allowlist |
| `backend/neural_mesh/synthesis/gap_resolution_policy.yaml` | **New** — per-domain resolution policy |
| `backend/core/ouroboros/governance/intake/sensors/capability_gap_sensor.py` | **New** — Ouroboros intake sensor |
| `backend/neural_mesh/registry/agent_registry.py` | **Modify** — extract `_resolve_internal()`, add gap event on fallback, add `rollback_agent()` with versioned routing |
| `backend/neural_mesh/agents/agent_initializer.py` | **Modify** — register `CapabilityGapSensor` at startup |
| `backend/core/ouroboros/governance/intake/intent_envelope.py` | **Modify** — add `"capability_gap"` to `_VALID_SOURCES` frozenset (line 20) |
| `backend/core/ouroboros/governance/intake/unified_intake_router.py` | **Modify** — route `capability_gap` source to synthesis handler |
| `backend/api/unified_command_processor.py` | **Modify** — add `session_id` to session state, generate `das_canary_key` alongside existing `command_id`, handle Mode A/B/C responses |
| `backend/core/ouroboros/cross_repo.py` | **Modify** — add 7 new `EventType` enum values (line 97, after `SYNC_REQUEST`) |

### J-Prime

| File | Change |
|---|---|
| `jarvis_prime/reasoning/graph_nodes/execution_planner.py` | **Modify** — emit `capability_gap_hint` when `_resolve_tool_from_index` returns `""` |

### Reactor-Core

| File | Change |
|---|---|
| `reactor_core/integration/event_bridge.py` | **Modify** — add the same 7 new `EventType` values to Reactor-Core's EventType enum |

#### New EventType values (added to both `backend/core/ouroboros/cross_repo.py` and Reactor-Core)

```python
AGENT_SYNTHESIS_REQUESTED       = "agent_synthesis_requested"
AGENT_SYNTHESIS_CANARY_ACTIVE   = "agent_synthesis_canary_active"
AGENT_SYNTHESIS_COMPLETED       = "agent_synthesis_completed"
AGENT_SYNTHESIS_FAILED          = "agent_synthesis_failed"
CAPABILITY_GAP_UNRESOLVED       = "capability_gap_unresolved"
AGENT_SYNTHESIS_CONFLICT        = "agent_synthesis_conflict"
ROUTING_OSCILLATION_DETECTED    = "routing_oscillation_detected"
```

Both repos must be updated together so the event bridge mapping remains consistent.

---

## 16. Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `DAS_ENABLED` | `true` | Master switch for Dynamic Agent Synthesis |
| `DAS_CANARY_TRAFFIC_PCT` | `10` | Percentage of domain traffic to canary |
| `DAS_CANARY_MIN_REQUESTS` | `10` | Min requests before hybrid gate evaluation |
| `DAS_CANARY_MIN_ELAPSED_S` | `300` | Min elapsed seconds before gate evaluation |
| `DAS_CANARY_MIN_SESSIONS` | `3` | Min distinct sessions for time-based path |
| `DAS_CANARY_MAX_ERROR_RATE` | `0.01` | Max error rate for canary graduation |
| `DAS_MODE_B_TTL_S` | `1800` | Mode B queue entry TTL (30 min default) |
| `DAS_SYNTH_TIMEOUT_S` | `120` | Max seconds to wait for Ouroboros synthesis |
| `DAS_QUARANTINE_MAX_RETRIES` | `3` | Max quarantine re-attempts before CLOSED_UNRESOLVED |
| `DAS_OSCILLATION_FLIP_THRESHOLD` | `3` | Route flips in `DAS_OSCILLATION_WINDOW_S` before freeze |
| `DAS_OSCILLATION_WINDOW_S` | `60` | Rolling window for oscillation detection (seconds) |
| `DAS_OSCILLATION_FREEZE_S` | `300` | Domain routing freeze duration after oscillation detected |
| `TRINITY_DREAM_DAS_ENABLED` | `false` | Reserved; no-op in this implementation iteration |

---

## Appendix A: Files Summary

```
NEW (7 source files + 2 config files = 9 new files):
  backend/neural_mesh/synthesis/__init__.py
  backend/neural_mesh/synthesis/gap_signal_bus.py
  backend/neural_mesh/synthesis/gap_resolution_protocol.py
  backend/neural_mesh/synthesis/domain_trust_ledger.py
  backend/neural_mesh/synthesis/agent_synthesis_loader.py
  backend/neural_mesh/synthesis/synthesis_command_queue.py
  backend/neural_mesh/synthesis/sandbox_allowlist.yaml
  backend/neural_mesh/synthesis/gap_resolution_policy.yaml
  backend/core/ouroboros/governance/intake/sensors/capability_gap_sensor.py

MODIFIED (6 files in JARVIS + 1 J-Prime + 1 Reactor-Core = 8 modified):
  JARVIS (6):
    backend/neural_mesh/registry/agent_registry.py
    backend/neural_mesh/agents/agent_initializer.py
    backend/core/ouroboros/governance/intake/intent_envelope.py
    backend/core/ouroboros/governance/intake/unified_intake_router.py
    backend/api/unified_command_processor.py
    backend/core/ouroboros/cross_repo.py
  J-Prime (1):
    jarvis_prime/reasoning/graph_nodes/execution_planner.py
  Reactor-Core (1):
    reactor_core/integration/event_bridge.py
```

---

## Appendix B: Pre-Implementation Checklist (10 Go/No-Go Checks)

Before implementation starts, verify all 10:

1. **GapSignalBus never blocks the command path** — `put_nowait()` confirmed; never `await put()`
2. **`domain_id` excludes mutable metadata** — `risk_class`, `trust_score` never in `domain_id`; only `task_type` + `target_app`
3. **`das_canary_key` is per-session-stable, not per-request** — `sha256(session_id + normalized_command)` with no random component; does not replace existing UUID `command_id`
4. **Mode C read-only enforcement is at the adapter layer** — `CapabilityScope.read_only` checked in adapter, not at call site; dream-advisory events default to Mode C via `_classify_mode` source check
5. **Versioned rollback does not pop `sys.modules`** — `AgentRegistry._version` increment + route cutover only
6. **Domain Trust Ledger is append-only** — no in-place mutation of journal entries; all denominators use `max(..., 1)`
7. **Quarantine retry generates new `attempt_key`** — `retry_of_attempt_key` chain preserved for audit; max-retry exit to `CLOSED_UNRESOLVED` enforced at `DAS_QUARANTINE_MAX_RETRIES`
8. **`REPLAY_STALE` is reachable** — entered from `REPLAY_AUTHORIZED` when TTL expired or entry superseded
9. **All Trinity emit calls have no-op fallback** — wrapped in `try/except Exception`; DreamEngine and ProphecyEngine advisory integration deferred to follow-up spec
10. **EventType enum updated in both repos** — `backend/core/ouroboros/cross_repo.py` AND `reactor_core/integration/event_bridge.py` both receive the 7 new values; additive-only (no removal or rename of existing values)
