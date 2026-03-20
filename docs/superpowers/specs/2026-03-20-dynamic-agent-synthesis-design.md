# Dynamic Agent Synthesis (DAS) — Design Spec

> **Status:** Approved for implementation (2026-03-20)
> **Repos:** JARVIS · J-Prime · Reactor-Core
> **Skill invoked:** `superpowers:brainstorming`

---

## 1. Problem Statement

JARVIS routes tasks to agents registered in `AgentCapabilityIndex`. When no capable agent exists for a
task type, `resolve_capability()` silently falls back to `_DEFAULT_TOOL = "app_control"`. This is a
silent mismatch: the task is served by a generic agent that may not have the right tools, resulting in
degraded execution or silent failure.

**Root cause:** The capability registry is static. New task types not foreseen at build time have no path
to resolution beyond a generic fallback.

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
AgentCapabilityIndex.resolve_capability()
    ├─ HIT  → normal execution
    └─ MISS → fire GapSignalBus event (fire-and-forget, 0ms overhead)
                    ↓
           GapResolutionProtocol (async, out of band)
                    ↓
           CapabilityGapSensor (Ouroboros intake)
                    ↓
           Ouroboros pipeline: CLASSIFY→ROUTE→GENERATE→VALIDATE→GATE→APPROVE→APPLY
                    ↓
           AgentSynthesisLoader (load + canary)
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
| **Capability Gap** | `resolve_capability()` falls back to `_DEFAULT_TOOL`; no registered agent handles `(task_type, target_app)` |
| **dedupe_key** | `sha256(task_type + target_app + capability_signature)` — stable semantic identity of the gap |
| **attempt_key** | `sha256(dedupe_key + policy_version + manifest_snapshot_hash)` — per synthesis attempt identity |
| **domain_id** | `normalize(task_type) + ":" + normalize(target_app or "any")` — never includes `risk_class` |
| **command_id** | `sha256(session_id + normalized_command + client_nonce)` — stable request identity |
| **Canary** | Synthesized agent serving 10% of domain traffic before graduation |
| **Domain Trust Ledger** | Per-domain append-only journal of synthesis outcomes; drives approval tier |

---

## 4. Dual-Source Gap Detection

Gap events originate from two sources. Both feed the same `GapSignalBus`.

### 4.1 Primary — AgentCapabilityIndex Fallback

In `AgentCapabilityIndex.resolve_capability()`, when the result would be `_DEFAULT_TOOL`:

```python
# backend/neural_mesh/registry/agent_registry.py
async def resolve_capability(self, task_type: str, target_app: Optional[str] = None) -> str:
    tool = self._resolve_internal(task_type, target_app)
    if tool == _DEFAULT_TOOL:
        await get_gap_signal_bus().emit(CapabilityGapEvent(
            task_type=task_type,
            target_app=target_app,
            source="primary_fallback",
        ))
    return tool
```

### 4.2 Secondary — J-Prime Advisory Hint

`ExecutionPlanner` may include `"capability_gap_hint"` in its plan response when it cannot resolve a
tool from the live `capability_index`. This is **advisory only** — it informs the coalescing window
but never substitutes for the primary detection path.

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

## 5. Gap Coalescing and Single-Flight Lock

To prevent synthesis storms when many commands hit the same gap simultaneously:

```python
# backend/neural_mesh/synthesis/gap_resolution_protocol.py
_coalescing_window_s: float = 10.0
_in_flight: Dict[str, asyncio.Event] = {}  # dedupe_key → completion event

async def handle_gap_event(self, event: CapabilityGapEvent) -> None:
    dedupe_key = _compute_dedupe_key(event.task_type, event.target_app)
    if dedupe_key in self._in_flight:
        # Another synthesis is already running for this gap — await its completion
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

The 10s coalescing window absorbs burst arrivals of the same gap before committing to synthesis.

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

- Commands are enqueued with their full `command_id`, payload snapshot, and expiry TTL (default 30 min).
- On agent graduation: `SynthesisCommandQueue.replay(domain_id)` re-routes enqueued commands in
  arrival order.
- Stale entries (past TTL or semantically superseded) are discarded with `REPLAY_STALE` state
  transition. The caller is notified.

### Mode C Details

- **Hard read-only enforcement at the adapter layer** — not at the call site. The execution adapter
  checks `CapabilityScope.read_only` before any tool call. If a Mode-C agent attempts a write,
  the adapter raises `ReadOnlyViolationError` and rolls back.
- Fallback quality is tracked in telemetry for comparison against synthesized agent post-graduation.

### Mode Classification Policy

```python
# backend/neural_mesh/synthesis/gap_resolution_protocol.py
def _classify_mode(self, event: CapabilityGapEvent, policy: GapResolutionPolicy) -> ResolutionMode:
    if policy.risk_class == "high" or not policy.idempotent:
        return ResolutionMode.A
    if policy.user_critical and policy.idempotent:
        return ResolutionMode.B
    return ResolutionMode.C
```

Policy is loaded from `gap_resolution_policy.yaml` (no hardcoded values). Per-domain overrides
are supported.

---

## 7. Ouroboros Integration — CapabilityGapSensor

`CapabilityGapSensor` is a new Ouroboros intake sensor, parallel to `OpportunityMinerSensor`.

```python
# backend/core/ouroboros/governance/intake/capability_gap_sensor.py
class CapabilityGapSensor(BaseSensor):
    source: str = "capability_gap"

    async def _poll(self) -> Optional[IntentEnvelope]:
        event = await self._gap_bus.next_unhandled()
        if event is None:
            return None
        return IntentEnvelope(
            source="capability_gap",
            intent="synthesize_agent",
            payload={
                "task_type": event.task_type,
                "target_app": event.target_app,
                "dedupe_key": event.dedupe_key,
                "attempt_key": event.attempt_key,
                "resolution_mode": event.resolution_mode.value,
                "domain_id": event.domain_id,
            },
            repo=self._repo,
        )
```

`IntentEnvelope._VALID_SOURCES` gains `"capability_gap"` as a valid source value.

The Ouroboros GENERATE step produces a synthesized agent module. VALIDATE runs the three-stage
artifact safety check (Section 8). GATE checks domain trust tier. APPROVE routes to human or auto
per trust tier. APPLY writes the artifact to disk.

---

## 8. Three-Stage Artifact Safety

Every synthesized artifact must pass all three stages before loading.

### Stage 1 — AST Scan (static analysis)

Parse the artifact with Python's `ast` module. Reject the artifact if any of the following patterns
appear at module scope: dynamic code execution builtins (the `"eval"` name, the `"exec"` name,
the `"__import__"` name), shell invocation via the `os` module's `system` function, process spawning
via any `subprocess` attribute access, `ctypes` imports, or raw `socket` imports. This check is purely
structural — no code is executed during the scan.

### Stage 2 — Sandboxed Import

Load the module via `importlib` with a restricted `__builtins__` namespace. The sandbox permits only
pure-Python standard library imports from an explicit allowlist. Any import not on the allowlist raises
`SandboxImportError` and quarantines the artifact.

### Stage 3 — Cross-Repo Contract Gate

The synthesized agent must declare:
- `AGENT_MANIFEST: AgentManifest` — capability declaration matching `AgentCapabilityIndex` schema
- `side_effect_policy: SideEffectPolicy` — what the agent writes/modifies
- `compensation_strategy: CompensationStrategy` — how to undo its effects on rollback

If any of these are missing or fail schema validation, the artifact is quarantined with state
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
penalties. Audit pass rate provides an independent signal.

### Tier Graduation Gates

| Tier | Entry Condition | Approval Mode |
|---|---|---|
| tier_0 | `risk_class=critical` OR `compensation_strategy=none` | Always human, never graduates |
| tier_1 | New domain (default) | Human approves each synthesis |
| tier_2 | `trust_score >= 0.70` AND `total_attempts >= 5` | Human approves first, then canary auto |
| tier_3 | `trust_score >= 0.90` AND `total_attempts >= 20` AND `incident_count == 0` | Auto with guardrails |

Graduation is **never retroactive** — a domain can only move tier by tier, and any incident resets
to tier_1 immediately.

---

## 10. Canary Activation and Graduation

### Sticky Deterministic Routing

```python
def _route_to_canary(self, domain_id: str, command_id: str) -> bool:
    bucket = int(hashlib.sha256(f"{domain_id}:{command_id}".encode()).hexdigest(), 16) % 10
    return bucket == 0  # stable 10% cohort
```

The same `(domain_id, command_id)` pair always routes to the same cohort. This prevents
flapping and enables reproducible debugging.

### Hybrid Graduation Gate

A canary graduates when **all** of the following are true:

```
(requests >= 10 OR (elapsed_s >= 300 AND distinct_sessions >= 3))
AND error_rate < 0.01
AND p99_latency_ms <= domain_slo_p99_ms
AND no_incidents_in_window
```

### Versioned Registry Rollback

Rollback does NOT pop `sys.modules` — module references leak into already-executing coroutines.
Instead, `AgentRegistry._version` is incremented and all new routing decisions use the new version.
In-flight executions using the old version complete normally against the old agent; no new executions
are dispatched to the rolled-back version.

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
        # Route new requests to previous stable agent
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
QUARANTINED_PENDING_REVIEW  → SYNTH_PENDING          ← new attempt_key, retry_of link
ARTIFACT_VERIFIED           → CANARY_ACTIVE
CANARY_ACTIVE               → CANARY_ROLLED_BACK | AGENT_GRADUATED
CANARY_ROLLED_BACK          → CLOSED_UNRESOLVED
AGENT_GRADUATED             → REPLAY_AUTHORIZED | CLOSED_RESOLVED
REPLAY_AUTHORIZED           → CLOSED_RESOLVED        ← Mode B only
REPLAY_STALE                → CLOSED_UNRESOLVED       ← Mode B only
CLOSED_RESOLVED             → (terminal)
CLOSED_UNRESOLVED           → (terminal)
──────────────────────────────────────────────────────────────────────────────
```

All state transitions are recorded in the Domain Trust Ledger journal.

---

## 12. Failure Class Taxonomy

| Class | Example | DAS Behavior |
|---|---|---|
| **Transient** | LLM rate limit, timeout | Retry with exponential backoff (max 3 attempts) |
| **Structural** | AST safety scan fail, missing manifest field | Quarantine; surface to human |
| **Semantic** | Synthesized agent solves wrong task | Canary rollback; log semantic drift signal |
| **Conflict** | Two gaps synthesize overlapping agents | `AGENT_SYNTHESIS_CONFLICT` event; arbitration via trust score |
| **Oscillation** | Repeated route flip between agents | `ROUTING_OSCILLATION_DETECTED` event; freeze routing for domain |

---

## 13. Synthesized Agent Contract

Every synthesized agent module must export at module scope:

```python
AGENT_MANIFEST: AgentManifest          # capability declaration
side_effect_policy: SideEffectPolicy   # what the agent writes/modifies
compensation_strategy: CompensationStrategy  # how to undo on rollback

class SynthesizedAgent(BaseNeuralMeshAgent):
    ...
```

`AgentManifest` must be compatible with `AgentCapabilityIndex` schema.
`SideEffectPolicy` is checked by the Mode-C read-only adapter before any tool dispatch.
`CompensationStrategy` is invoked by the canary rollback path.

---

## 14. Trinity Consciousness Integration

Trinity is partially implemented. DAS integrates conservatively as an **observer**, never a controller.

| Engine | DAS Signal | Direction |
|---|---|---|
| **HealthCortex** | Synthesis latency, canary error rate, quarantine rate | DAS → Trinity (emit) |
| **MemoryEngine** | Per-domain synthesis outcomes, trust score history | DAS → Trinity (emit) |
| **DreamEngine** | Pre-synthesis of predicted future gaps during idle cycles | Trinity → DAS (advisory request, gated by `TRINITY_DREAM_DAS_ENABLED`) |
| **ProphecyEngine** | Elevate strategic capability gaps to Mode B urgency | Trinity → DAS (advisory, not binding) |

DAS proceeds correctly whether Trinity is fully operational, partially operational, or absent.
All Trinity hooks are wrapped in `try/except` with fallback to no-op.

---

## 15. Cross-Repo Integration

### JARVIS (this repo)

| File | Change |
|---|---|
| `backend/neural_mesh/synthesis/__init__.py` | **New** — package init |
| `backend/neural_mesh/synthesis/gap_signal_bus.py` | **New** — asyncio.Queue broadcaster |
| `backend/neural_mesh/synthesis/capability_gap_sensor.py` | **New** — Ouroboros intake sensor |
| `backend/neural_mesh/synthesis/gap_resolution_protocol.py` | **New** — tri-mode routing + coalescing |
| `backend/neural_mesh/synthesis/domain_trust_ledger.py` | **New** — per-domain trust journal |
| `backend/neural_mesh/synthesis/agent_synthesis_loader.py` | **New** — artifact safety + canary load |
| `backend/neural_mesh/synthesis/synthesis_command_queue.py` | **New** — Mode B pending queue |
| `backend/neural_mesh/registry/agent_registry.py` | **Modify** — emit gap events, versioned rollback |
| `backend/neural_mesh/agents/agent_initializer.py` | **Modify** — register CapabilityGapSensor |
| `backend/core/ouroboros/governance/intake/intent_envelope.py` | **Modify** — add `"capability_gap"` to `_VALID_SOURCES` |
| `backend/core/ouroboros/governance/intake/unified_intake_router.py` | **Modify** — route `capability_gap` source |
| `backend/api/unified_command_processor.py` | **Modify** — Mode A/B/C response handling |

### J-Prime

| File | Change |
|---|---|
| `jarvis_prime/reasoning/graph_nodes/execution_planner.py` | **Modify** — emit `capability_gap_hint` when tool unresolvable |

### Reactor-Core

| File | Change |
|---|---|
| `reactor_core/integration/event_bridge.py` | **Modify** — add 7 new `EventType` values |

#### New Reactor-Core EventType values

```python
AGENT_SYNTHESIS_REQUESTED       = "agent_synthesis_requested"
AGENT_SYNTHESIS_CANARY_ACTIVE   = "agent_synthesis_canary_active"
AGENT_SYNTHESIS_COMPLETED       = "agent_synthesis_completed"
AGENT_SYNTHESIS_FAILED          = "agent_synthesis_failed"
CAPABILITY_GAP_UNRESOLVED       = "capability_gap_unresolved"
AGENT_SYNTHESIS_CONFLICT        = "agent_synthesis_conflict"
ROUTING_OSCILLATION_DETECTED    = "routing_oscillation_detected"
```

---

## 16. Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `DAS_ENABLED` | `true` | Master switch for Dynamic Agent Synthesis |
| `DAS_COALESCING_WINDOW_S` | `10` | Gap coalescing dedup window |
| `DAS_CANARY_TRAFFIC_PCT` | `10` | Percentage of domain traffic to canary |
| `DAS_CANARY_MIN_REQUESTS` | `10` | Min requests before hybrid gate evaluation |
| `DAS_CANARY_MIN_ELAPSED_S` | `300` | Min elapsed seconds before gate evaluation |
| `DAS_CANARY_MIN_SESSIONS` | `3` | Min distinct sessions for time-based path |
| `DAS_CANARY_MAX_ERROR_RATE` | `0.01` | Max error rate for canary graduation |
| `DAS_MODE_B_TTL_S` | `1800` | Mode B queue entry TTL (30 min default) |
| `DAS_SYNTH_TIMEOUT_S` | `120` | Max seconds to wait for Ouroboros synthesis |
| `DAS_QUARANTINE_MAX_RETRIES` | `3` | Max quarantine re-attempts before CLOSED_UNRESOLVED |
| `TRINITY_DREAM_DAS_ENABLED` | `false` | Allow DreamEngine to request proactive pre-synthesis |

---

## Appendix A: Files Summary

```
NEW (7 files):
  backend/neural_mesh/synthesis/__init__.py
  backend/neural_mesh/synthesis/gap_signal_bus.py
  backend/neural_mesh/synthesis/capability_gap_sensor.py
  backend/neural_mesh/synthesis/gap_resolution_protocol.py
  backend/neural_mesh/synthesis/domain_trust_ledger.py
  backend/neural_mesh/synthesis/agent_synthesis_loader.py
  backend/neural_mesh/synthesis/synthesis_command_queue.py

MODIFIED (12 files across 3 repos):
  JARVIS:
    backend/neural_mesh/registry/agent_registry.py
    backend/neural_mesh/agents/agent_initializer.py
    backend/core/ouroboros/governance/intake/intent_envelope.py
    backend/core/ouroboros/governance/intake/unified_intake_router.py
    backend/api/unified_command_processor.py
  J-Prime:
    jarvis_prime/reasoning/graph_nodes/execution_planner.py
  Reactor-Core:
    reactor_core/integration/event_bridge.py
```

---

## Appendix B: Pre-Implementation Checklist (10 Go/No-Go Checks)

Before implementation starts, verify all 10:

1. **GapSignalBus never blocks the command path** — fire-and-forget confirmed; `asyncio.Queue.put_nowait()` only, never `await put()`
2. **`domain_id` excludes mutable metadata** — `risk_class`, `trust_score` never in `domain_id`; only `task_type` + `target_app`
3. **`command_id` is stable and collision-resistant** — `sha256(session_id + normalized_command + client_nonce)` with documented nonce generation
4. **Mode C read-only enforcement is at the adapter layer** — `CapabilityScope.read_only` checked in adapter, not at call site
5. **Versioned rollback does not pop `sys.modules`** — `AgentRegistry._version` increment + route cutover only
6. **Domain Trust Ledger is append-only** — no in-place mutation of journal entries; materialized view rebuilt on read
7. **Trust formula denominators are guarded** — `max(total_attempts, 1)` in all four ratio computations
8. **Quarantine retry generates new `attempt_key`** — `retry_of_attempt_key` chain preserved for audit
9. **All Trinity hooks have no-op fallback** — each Trinity integration point wrapped in `try/except Exception`
10. **`AGENT_SYNTHESIS_*` Reactor-Core events do not break existing consumers** — additive-only enum extension; no removal or rename of existing values
