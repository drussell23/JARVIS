# Sub-project A: The Severed Nerve

**Date:** 2026-04-02
**Parent:** [Ouroboros HUD Pipeline Program](2026-04-02-ouroboros-hud-pipeline-program.md)
**Status:** Approved

## Problem

`CUExecutionSensor` is a singleton that tracks CU failure patterns and graduates them into `IntentEnvelope`s for Ouroboros self-improvement. The sensor works correctly — it counts failures, detects patterns, and builds envelopes. But `_emit_envelope()` drops every envelope because `self._router is None`:

```
[CUExecutionSensor] No router wired — cannot emit envelope for 'messaging_failure:...'
```

The router is never wired because `IntakeLayerService._build_components()` constructs and registers 10+ sensors (Backlog, TestFailure, OpportunityMiner, Voice, Scheduled, CapabilityGap, RuntimeHealth, WebIntelligence, PerformanceRegression, DocStaleness) but never touches CUExecutionSensor.

The envelope source `"cu_execution"` is already registered in `_VALID_SOURCES` (intent_envelope.py:20), so the intake schema accepts it. But `"cu_execution"` is not in `_PRIORITY_MAP` (unified_intake_router.py:33-43), so any envelope that did get through would receive fallback priority 99 (dead last).

## Changes

### 1. Wire CUExecutionSensor in IntakeLayerService

**File:** `backend/core/ouroboros/governance/intake/intake_layer_service.py`
**Location:** In `_build_components()`, after the `self._sensors = backlog_sensors + test_failure_sensors + miner_sensors` line (around line 433), in the same try/except/append style as ScheduledTriggerSensor, CapabilityGapSensor, etc.

```python
# ---- CUExecutionSensor (Pillar 6: Vision Neuroplasticity) ----
# Event-driven sensor — records fed by ActionDispatcher after CU execution.
# Singleton re-wiring: CUExecutionSensor.__init__ accepts router= on
# re-init (if already constructed by get_cu_execution_sensor() elsewhere).
try:
    from backend.core.ouroboros.governance.intake.sensors.cu_execution_sensor import (
        CUExecutionSensor,
    )
    _cu_sensor = CUExecutionSensor(router=self._router, repo="jarvis")
    self._sensors.append(_cu_sensor)
    logger.info("[IntakeLayer] CUExecutionSensor wired (vision neuroplasticity active)")
except Exception as exc:
    logger.debug("[IntakeLayer] CUExecutionSensor skipped: %s", exc)
```

**Why this works:**
- The singleton pattern in CUExecutionSensor.__init__ (lines 130-139) already handles re-wiring: if `_initialized is True` and `router is not None`, it sets `self._router = router`.
- `backend/main.py` and `brainstem/action_dispatcher.py` call `get_cu_execution_sensor()` which returns the same singleton — so after IntakeLayerService wires the router, all callers benefit.
- `async start()` is a no-op (event-driven), `stop()` clears state. Both match the sensor lifecycle protocol.

### 2. Add cu_execution to Priority Map

**File:** `backend/core/ouroboros/governance/intake/unified_intake_router.py`
**Location:** `_PRIORITY_MAP` dict (lines 33-43)

Add `"cu_execution": 5` — same tier as `"capability_gap"`. Both are neuroplasticity-class pain signals: recurring failures that trigger self-improvement. Not as urgent as test failures (1) or backlog items (2), but more urgent than runtime health checks (6).

```python
_PRIORITY_MAP: Dict[str, int] = {
    "voice_human": 0,
    "test_failure": 1,
    "backlog": 2,
    "ai_miner": 3,
    "architecture": 3,
    "exploration": 4,
    "roadmap": 4,
    "capability_gap": 5,
    "cu_execution": 5,      # <-- NEW
    "runtime_health": 6,
}
```

## Integration Test

### Test 1: Spine Test (fast, no LLM)

**File:** `tests/integration/test_cu_pipeline_spine.py`

Proves the envelope flows from sensor → router → governed loop. No real LLM calls.

**Setup:**
1. Construct `UnifiedIntakeRouter` with a mock GLS (or minimal GovernedLoopService with a mock orchestrator)
2. Construct `CUExecutionSensor(router=router, repo="jarvis")`
3. Spy on `router.ingest` (or mock it to capture the envelope)

**Steps:**
1. Feed 3 `CUExecutionRecord` failures with the same signature pattern
2. Assert `sensor._total_envelopes_emitted >= 1`
3. Assert `router.ingest` was called with an `IntentEnvelope` where `source == "cu_execution"`
4. Assert no `"No router wired"` warning in captured logs

**Why spine-first:** This test is fast (<1s), has no filesystem side effects, and proves the deterministic handoff. If this passes, the spinal cord is connected.

### Test 2: Full Pipeline Test (slow, optional mark)

**File:** `tests/integration/test_cu_pipeline_full.py` (or same file, `@pytest.mark.slow`)

Proves the envelope flows through the full orchestrator and produces an APPLIED ledger entry.

**Setup:**
1. Create a temp directory with a copy of `cu_task_planner.py`
2. Construct a real `GovernanceStack` with:
   - Mock LLM provider that returns a canned `2b.1` schema response (adds a comment to the file)
   - Real `ChangeEngine` pointing at the temp directory
   - Real `OperationLedger` (in-memory or temp file)
3. Construct `GovernedLoopService` with the stack
4. Wire `CUExecutionSensor` to the intake router (as in production)

**Steps:**
1. Start the GovernedLoopService
2. Feed 3 CU failure records (crossing graduation threshold)
3. Wait for the orchestrator to process (with timeout)
4. Assert ledger entry status is APPLIED or COMPLETED
5. Assert the temp file was modified (ChangeEngine executed)
6. Assert orchestrator phase callbacks fired for: CLASSIFY, ROUTE, GENERATE, VALIDATE, GATE, APPLY, VERIFY, COMPLETE

**Mock LLM response:** Return a valid 2b.1 schema with a trivial change:
```json
{
  "schema_version": "2b.1",
  "full_content": "<original file with one added comment line>",
  "reasoning": "Added anti-pattern guard (test fixture)"
}
```

**Assertions use stable hooks:** Phase callbacks, ledger API, and sensor counters — not log substring matching.

## Acceptance Criteria

1. `CUExecutionSensor` receives router from `IntakeLayerService` during startup
2. After 3 CU failures with the same signature, an envelope enters the router
3. The envelope is queued with priority 5 (not fallback 99)
4. No `"No router wired"` warning in logs during normal operation
5. Spine test passes in <1s with no external dependencies
6. Full pipeline test (if implemented) passes with mock LLM in <10s

## Files Modified

| File | Change |
|------|--------|
| `backend/core/ouroboros/governance/intake/intake_layer_service.py` | Wire CUExecutionSensor singleton |
| `backend/core/ouroboros/governance/intake/unified_intake_router.py` | Add `"cu_execution": 5` to `_PRIORITY_MAP` |
| `tests/integration/test_cu_pipeline_spine.py` | New: spine integration test |
| `tests/integration/test_cu_pipeline_full.py` | New: full pipeline integration test (optional) |

## Out of Scope

- Context injection quality (Sub-project B)
- Duplication guard in VALIDATE (Sub-project C)
- DaemonNarrator wiring in HUD (Sub-project D)
- Multi-repo CU sensor fan-out (future, if CU operates across repos)
