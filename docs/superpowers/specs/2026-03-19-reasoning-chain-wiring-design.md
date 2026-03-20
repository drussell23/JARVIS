# Reasoning Chain Wiring — Design Spec (Approach B)

> **Date**: 2026-03-19
> **Handoff**: `docs/superpowers/handoff/2026-03-19-reasoning-chain-handoff.md`
> **Status**: Approved, ready for implementation

---

## Problem

Three production-registered components exist but are never called from the voice pipeline:

| Component | File | Status |
|---|---|---|
| `ProactiveCommandDetector` | `backend/core/proactive_command_detector.py` | Built, zero callers |
| `PredictivePlanningAgent` | `backend/neural_mesh/agents/predictive_planning_agent.py` | Registered, `expand_intent()` never called from voice |
| `CoordinatorAgent` | `backend/neural_mesh/agents/coordinator_agent.py` | Registered, voice pipeline bypasses it |

The voice pipeline sends every command as a single intent to J-Prime. Multi-task commands like "start my day" get a single plan instead of being expanded into parallel sub-intents (check email, check calendar, open Slack).

## Solution: Extracted ReasoningChainOrchestrator

### Why extracted (not inline)

`unified_command_processor.py` is 9700+ lines. Adding chain logic, shadow metrics, phase transitions, and 5 telemetry events inline would make it worse. An extracted orchestrator is:
- Independently testable
- Single-responsibility (chain lifecycle)
- Disableable via one feature flag
- Clean wiring point (~5 lines in the processor)

### New file: `backend/core/reasoning_chain_orchestrator.py`

```
ReasoningChainOrchestrator
    ├── ChainPhase enum: SHADOW | SOFT_ENABLE | FULL_ENABLE
    ├── ChainConfig dataclass: thresholds, timeouts, phase
    ├── ChainResult dataclass: results, audit_trail, metrics
    ├── ShadowMetrics: tracks divergence for go/no-go gates
    └── process(command, context, trace_id) → Optional[ChainResult]
```

### Internal flow of `process()`

```
1. ProactiveCommandDetector.detect(command)
   → ProactiveDetectionResult {is_proactive, confidence, signals, suggested_intent}
   → Emit: proactive_detection telemetry event

2. IF NOT proactive OR confidence < threshold:
   → Return None (caller falls through to existing single-intent path)

3. PredictivePlanningAgent.expand_intent(command)
   → PredictionResult {expanded_tasks[], confidence, reasoning}
   → Emit: intent_expansion telemetry event

4. Phase-dependent behavior:
   SHADOW:
     → Log would-expand vs actually-expanded
     → Emit: expansion_shadow_divergence
     → Return None (no behavioral change)

   SOFT_ENABLE:
     → Return ChainResult with needs_confirmation=True
     → Caller asks user: "Multiple tasks detected. Handle separately?"
     → If user confirms → proceed to step 5
     → If user declines → Return None (single-intent path)

   FULL_ENABLE (confidence > auto_expand_threshold):
     → Proceed to step 5 automatically

5. For each expanded sub-intent:
   → MindClient.send_command(sub_intent, context={trace_id, parent_command})
   → Collect plan results

6. For each Mind plan:
   → CoordinatorAgent.execute_task({
       action: "delegate_task",
       capability: mapped_from_PredictivePlanningAgent._map_goal_to_capability(),
       task_payload: {plan_steps, trace_id}
     })
   → Emit: coordinator_delegation telemetry event

7. Aggregate results
   → Emit: chain_complete telemetry event
   → Return ChainResult with all sub-results, audit trail
```

### Wiring point in unified_command_processor.py

At line ~2283 (before `_mind.send_command()`):

```python
# v300.0: Reasoning chain pre-routing
_chain_result = await self._try_reasoning_chain(
    command_text, _jprime_ctx, deadline,
)
if _chain_result is not None:
    return _chain_result
```

The `_try_reasoning_chain` method:
1. Checks `JARVIS_REASONING_CHAIN_ENABLED` or `JARVIS_REASONING_CHAIN_SHADOW`
2. Gets the orchestrator singleton
3. Calls `orchestrator.process()`
4. If SHADOW: logs and returns None (fall through)
5. If SOFT_ENABLE + needs_confirmation: returns confirmation prompt
6. If result has expanded plans: calls `_execute_mind_plan()` for each

### Feature flags

| Flag | Default | Effect |
|---|---|---|
| `JARVIS_REASONING_CHAIN_SHADOW` | `false` | Shadow mode: run chain, log, don't act |
| `JARVIS_REASONING_CHAIN_ENABLED` | `false` | Enable chain for real routing |
| `JARVIS_REASONING_CHAIN_AUTO_EXPAND` | `false` | Skip user confirmation (Phase 3) |
| `CHAIN_PROACTIVE_THRESHOLD` | `0.6` | Min confidence for proactive detection |
| `CHAIN_AUTO_EXPAND_THRESHOLD` | `0.85` | Min confidence for auto-expand (Phase 3) |
| `CHAIN_EXPANSION_TIMEOUT` | `2.0` | Max seconds for detect+expand combined |

### Telemetry events (all carry trace_id)

1. `proactive_detection` — emitted after ProactiveCommandDetector.detect()
2. `intent_expansion` — emitted after PredictivePlanningAgent.expand_intent()
3. `expansion_shadow_divergence` — emitted in shadow mode (would vs actual)
4. `coordinator_delegation` — emitted per plan step routed through CoordinatorAgent
5. `chain_complete` — emitted at end with totals

### Go/no-go gates (Phase 2 → Phase 3)

| Metric | Threshold | Window |
|---|---|---|
| Expansion accuracy | >= 80% | 100 proactive detections |
| False positive rate | <= 10% | 100 commands |
| Expansion latency p95 | <= 500ms | 1000 commands |
| Mind plan quality | No regression | 72h |
| User override rate | <= 20% | 50 expansions |

### Test file: `tests/core/test_reasoning_chain_orchestrator.py`

Test categories:
1. **Phase behavior**: shadow logs but doesn't act, soft asks confirmation, full auto-expands
2. **Threshold gating**: below-threshold commands fall through unchanged
3. **Expansion**: multi-task commands expand into correct sub-intents
4. **CoordinatorAgent routing**: plan steps map to correct agent capabilities
5. **Telemetry**: all 5 events emitted with correct trace_id
6. **Rollback**: disabling flags returns to single-intent path
7. **Timeout**: expansion respects CHAIN_EXPANSION_TIMEOUT
8. **Error handling**: Mind unavailable → graceful fallback to single-intent
9. **Go/no-go**: metrics accumulate correctly

### Invariants

1. **J-Prime is the sole planner.** The orchestrator never generates plans. It classifies (detector), expands intents (planner), and routes plans (coordinator). Only J-Prime produces Plan objects.
2. **Single command, single trace_id.** All telemetry for one voice command correlates via trace_id.
3. **Graceful degradation.** Any component failure (detector, planner, Mind, coordinator) → fall through to existing single-intent path. Never worse than today.
4. **No latency regression in shadow.** Shadow detection runs with timeout; if it exceeds budget, it's cancelled. The command proceeds normally.

### Files changed

| File | Change |
|---|---|
| `backend/core/reasoning_chain_orchestrator.py` | **NEW** — orchestrator class |
| `backend/api/unified_command_processor.py` | ~15 lines: `_try_reasoning_chain()` method + call site |
| `tests/core/test_reasoning_chain_orchestrator.py` | **NEW** — comprehensive tests |
| `.env` | New feature flag defaults (not committed) |

### Out of scope

- Changes to ProactiveCommandDetector, PredictivePlanningAgent, or CoordinatorAgent internals (already built)
- Changes to J-Prime endpoints (already built)
- Changes to MindClient (already built)
- UI for go/no-go dashboard (future)
