# Neural Mesh Agent Boot + Reasoning Chain Activation — Design Spec (Phase B)

> **Date**: 2026-03-20
> **Phase**: B (main implementation, follows Phase A telemetry contract)
> **Sub-project**: 2 of 3
> **Status**: Approved, ready for implementation

---

## Problem

1. **15 Neural Mesh agents are registered but never initialized** — `initialize_all_agents()` exists but is never called from `unified_supervisor.py`. Agents like CoordinatorAgent, PredictivePlanningAgent, GoogleWorkspaceAgent remain inert.

2. **Reasoning chain has no activation gate** — the chain is wired (Sub-project 1) and feature flags exist in `.env`, but there's no formal readiness check. The chain could activate before its critical dependencies (J-Prime, CoordinatorAgent, PredictivePlanningAgent) are healthy, causing hidden partial-readiness bugs.

3. **No capability-scoped gating** — all agents are treated equally. Agents that don't need J-Prime (MemoryAgent, SpatialAwarenessAgent) are unnecessarily coupled to agents that do.

## Solution: C+ (Capability-Scoped Gating)

### Authority Model

- **Agent init**: Independent of J-Prime. All 15 agents initialize at boot.
- **Reasoning chain activation**: Gated on J-Prime AND only the agents it depends on.
- **Non-critical agents**: Run immediately, serve non-Mind features.

### Critical Dependency Set

```python
CRITICAL_FOR_REASONING = {
    "jprime_lifecycle",       # JprimeLifecycleController.state in (READY, DEGRADED)
    "coordinator_agent",      # CoordinatorAgent initialized and responding
    "predictive_planner",     # PredictivePlanningAgent initialized and responding
    "proactive_detector",     # ProactiveCommandDetector singleton created
}
```

---

## ReasoningActivationGate — State Machine

### States

| State | Accepts commands? | Description |
|---|---|---|
| `DISABLED` | no | Feature flags off. Gate closed. |
| `WAITING_DEPS` | no | Flags on, >=1 critical dep unavailable. |
| `READY` | no (arming) | All deps healthy. Dwell timer running. |
| `ACTIVE` | yes | Chain processing commands. All deps healthy. |
| `DEGRADED` | yes (reduced) | J-Prime DEGRADED or 1 agent slow. Higher thresholds. |
| `BLOCKED` | no | Critical dep lost. In-flight preempted. |
| `TERMINAL` | no | Sustained failure. Manual reset or cooldown. |

### Transition Table

| From | To | Trigger | Guard | Action | Failure Class |
|---|---|---|---|---|---|
| `DISABLED` | `WAITING_DEPS` | Flag enabled | `_ENABLED=true` OR `_SHADOW=true` | Start dep polling | — |
| `WAITING_DEPS` | `READY` | All deps healthy | All 4 report healthy within staleness | Start dwell timer (5s) | — |
| `WAITING_DEPS` | `WAITING_DEPS` | Poll cycle | >=1 dep unavailable | Log missing deps | `CRITICAL_AGENT_UNAVAILABLE` |
| `WAITING_DEPS` | `DISABLED` | Flag disabled | — | Stop polling | — |
| `READY` | `ACTIVE` | Dwell expires | All deps still healthy | Enable chain, emit activation | — |
| `READY` | `WAITING_DEPS` | Dep lost during dwell | Any dep unavailable | Cancel dwell | `DEP_LOST_DURING_ARM` |
| `READY` | `DISABLED` | Flag disabled | — | Cancel dwell | — |
| `ACTIVE` | `DEGRADED` | Dep slow/degraded | `consecutive_slow >= 3` AND dwell met | Raise thresholds, emit degradation | `JPRIME_DEGRADED` / `AGENT_SLOW` |
| `ACTIVE` | `BLOCKED` | Dep unavailable | `consecutive_failures >= 3` AND dwell met | Preempt in-flight, emit block | `JPRIME_UNHEALTHY` / `AGENT_CRASHED` |
| `ACTIVE` | `DISABLED` | Flag disabled | — | Preempt, deactivate | — |
| `DEGRADED` | `ACTIVE` | All deps recovered | `consecutive_healthy >= 3` AND dwell met | Restore thresholds, emit recovery | — |
| `DEGRADED` | `BLOCKED` | Further degradation | 2nd dep fails OR J-Prime UNHEALTHY | Preempt in-flight | `MULTI_DEP_FAILURE` |
| `DEGRADED` | `DISABLED` | Flag disabled | — | Preempt, deactivate | — |
| `BLOCKED` | `WAITING_DEPS` | Recovery attempt | `block_duration < 5 min` | Re-check deps | — |
| `BLOCKED` | `TERMINAL` | Sustained block | `block_duration >= 5 min` | Emit terminal | `SUSTAINED_BLOCK` |
| `BLOCKED` | `DISABLED` | Flag disabled | — | — | — |
| `TERMINAL` | `WAITING_DEPS` | Cooldown expired | `terminal_duration >= 15 min` | Auto-reset | — |
| `TERMINAL` | `WAITING_DEPS` | Manual reset | Operator command | Clear counters | — |
| `TERMINAL` | `DISABLED` | Flag disabled | — | — | — |

### Debounce and Dwell Rules

| Rule | Default | Env Override | Purpose |
|---|---|---|---|
| `ACTIVATION_DWELL_S` | 5s | `REASONING_ACTIVATION_DWELL_S` | Arming delay before ACTIVE |
| `MIN_STATE_DWELL_S` | 3s | `REASONING_MIN_DWELL_S` | Minimum in any state (flap suppression) |
| `DEGRADE_THRESHOLD` | 3 | `REASONING_DEGRADE_THRESHOLD` | Consecutive slow before DEGRADED |
| `BLOCK_THRESHOLD` | 3 | `REASONING_BLOCK_THRESHOLD` | Consecutive failures before BLOCKED |
| `RECOVERY_THRESHOLD` | 3 | `REASONING_RECOVERY_THRESHOLD` | Consecutive healthy before recovery |
| `MAX_BLOCK_DURATION_S` | 300s | `REASONING_MAX_BLOCK_S` | BLOCKED to TERMINAL if exceeded |
| `TERMINAL_COOLDOWN_S` | 900s | `REASONING_TERMINAL_COOLDOWN_S` | Auto-reset from TERMINAL |
| `DEP_POLL_INTERVAL_S` | 10s | `REASONING_DEP_POLL_S` | Health check frequency |

**Flap invariant:** No state entered, exited, and re-entered within `MIN_STATE_DWELL_S`. Violations suppressed with `FLAP_SUPPRESSED` warning.

### Preemption Policy

| Transition | In-Flight | Cleanup |
|---|---|---|
| `ACTIVE -> DEGRADED` | Allow completion | Apply degraded thresholds to next command |
| `ACTIVE -> BLOCKED` | Cancel immediately | `orchestrator.process()` returns None |
| `DEGRADED -> BLOCKED` | Cancel immediately | Same |
| `DEGRADED -> ACTIVE` | No disruption | Restore thresholds for next command |
| `* -> DISABLED` | Cancel all | Reset orchestrator |

**Orphan prevention:** In-flight operations carry `gate_sequence`. If gate transitions, stale operations discard results (`my_sequence < gate.current_sequence`).

### Degraded Mode Overrides

| Parameter | ACTIVE | DEGRADED |
|---|---|---|
| `proactive_threshold` | 0.6 | 0.7 (+0.1) |
| `auto_expand_threshold` | 0.85 | 1.0 (never auto-expand) |
| `expansion_timeout` | 2.0s | 1.0s (halved) |
| `mind_request_timeout` | 30s | 15s (halved) |

### Dependency Health Checks

| Dependency | Check Method | Healthy | Degraded | Unavailable |
|---|---|---|---|---|
| `jprime_lifecycle` | Read `.state` (in-process) | READY | DEGRADED | UNHEALTHY/TERMINAL/others |
| `coordinator_agent` | `execute_task({"action":"get_stats"})` 2s timeout | Responds <1s | Responds 1-2s | Timeout/error |
| `predictive_planner` | `get_stats()` 2s timeout | Responds <1s | Responds 1-2s | Timeout/error |
| `proactive_detector` | `get_stats()` | Always healthy once created | — | Not created |

### Split-Authority Reconciliation

Gate computes **intersection** of readiness. Never overrides source authority:
- Lifecycle READY + agent UNAVAILABLE -> gate `WAITING_DEPS`
- Lifecycle DEGRADED + all agents HEALTHY -> gate `DEGRADED`
- Dashboard reads gate state from telemetry bus, never polls deps directly

### Telemetry

Every gate transition emits `reasoning.activation@1.0.0`:

```python
{
    "from_state": str,
    "to_state": str,
    "trigger": str,
    "cause_code": str,
    "critical_deps": {
        "jprime_lifecycle": "HEALTHY|DEGRADED|UNAVAILABLE",
        "coordinator_agent": "HEALTHY|DEGRADED|UNAVAILABLE",
        "predictive_planner": "HEALTHY|DEGRADED|UNAVAILABLE",
        "proactive_detector": "HEALTHY|DEGRADED|UNAVAILABLE",
    },
    "gate_sequence": int,
    "dwell_ms": float,
    "in_flight_preempted": int,
    "degraded_overrides": {},
}
```

---

## Neural Mesh Agent Boot

### Where to Wire

Add `initialize_all_agents()` call in `unified_supervisor.py` after Zone 5.7 (Trinity/J-Prime boot) and before Zone 7 (Agent Runtime). This is Zone 6.5 — agents don't wait for J-Prime READY.

```python
# Zone 6.5: Neural Mesh Agent Initialization
from backend.neural_mesh.agents.agent_initializer import initialize_all_agents
agent_statuses = await initialize_all_agents()
# Non-fatal: failures logged, system continues
```

### Boot Telemetry

Agent init emits `scheduler.graph_state@1.0.0`:

```python
{
    "total_agents": 15,
    "initialized": 13,
    "failed": 2,
    "failed_agents": ["agent_name_1", "agent_name_2"],
    "critical_ready": {
        "coordinator_agent": true,
        "predictive_planner": true,
        "proactive_detector": true,
    },
}
```

### Activation Gate Wiring

After agent init, start the gate:

```python
# Zone 6.6: Reasoning Activation Gate
gate = get_reasoning_activation_gate()
await gate.start()  # Begins dep polling, evaluates flags
# Gate transitions asynchronously — does not block boot
```

---

## Integration with ReasoningChainOrchestrator

Gate check at top of `process()`:

```python
async def process(self, command, context, trace_id, deadline=None):
    gate = get_reasoning_activation_gate()
    if not gate.is_active():
        return None  # Fall through to single-intent

    # Apply degraded overrides if needed
    if gate.state == GateState.DEGRADED:
        self._apply_degraded_overrides(gate.degraded_config)

    # Existing logic continues...
```

---

## Files Changed

| File | Change |
|---|---|
| `backend/core/reasoning_activation_gate.py` | **NEW** — 7-state gate, dep polling, preemption, telemetry |
| `backend/core/reasoning_chain_orchestrator.py` | **MODIFY** — Gate check at top of `process()`, degraded override support |
| `unified_supervisor.py` | **MODIFY** — Zone 6.5: `initialize_all_agents()`. Zone 6.6: start gate. |
| `tests/core/test_reasoning_activation_gate.py` | **NEW** — Gate transitions, dep health, preemption, flap suppression |

## Acceptance Criteria

1. All 15 Neural Mesh agents initialized at boot (logged, non-fatal failures)
2. ReasoningActivationGate enforces 7-state FSM with debounce
3. Reasoning chain only processes when gate is ACTIVE or DEGRADED
4. DEGRADED mode raises thresholds and shortens timeouts
5. In-flight preemption on BLOCKED transition (no orphaned tasks)
6. Gate sequence prevents stale results from completed operations
7. Flap suppression: no state re-entry within MIN_STATE_DWELL_S
8. All transitions emit `reasoning.activation@1.0.0` with dep status
9. `scheduler.graph_state@1.0.0` emitted after agent init
10. Dashboard can render gate state from telemetry (no direct dep polling)
11. Deterministic failure-injection tests:
    - J-Prime UNHEALTHY mid-command -> BLOCKED, in-flight cancelled
    - CoordinatorAgent crashes -> BLOCKED after 3 consecutive failures
    - Flapping J-Prime oscillation -> suppressed
    - All deps healthy -> ACTIVE within ACTIVATION_DWELL_S
    - Flag disabled while ACTIVE -> immediate DISABLED, in-flight cancelled

## Out of Scope

- Dashboard UI (Sub-project 3, Phase C)
- Changes to J-Prime server or Reactor Core
- Changes to individual agent internals
- CoordinatorAgent -> TaskChainExecutor wiring (future, after gate proves stable)
