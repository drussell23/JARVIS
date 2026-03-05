# Thrash Loop Elimination Design

**Date:** 2026-03-04
**Status:** Approved
**Phase:** 1 of 2 (Phase 2: Resource Orchestrator)

## Problem

A self-reinforcing feedback loop causes recurring memory thrashing and email triage timeouts:

```
_maybe_run_email_triage()           [NO memory gate]
  → runner.run_cycle()
    → extract_features() per email   [N sequential model calls]
      → router.generate()
        → PrimeLocalClient.generate() [NO _model_swapping guard]
          → mmap pulls GGUF pages → pageins spike 13,896/s
            → MemoryQuantizer → "emergency"
              → _handle_thrash_state_change → model downgrade
                → concurrent generate() stalls during swap
                  → 30s triage timeout fires (CancelledError)
                    → model stays loaded, 60s later: loop repeats
```

## Root Causes

| # | Root Cause | Component |
|---|---|---|
| 1 | No memory-pressure gate before triage launches | `agent_runtime.py:_maybe_run_email_triage` |
| 2 | Every email hits the model (`extraction_enabled=True`) | `email_triage/runner.py` + `extraction.py` |
| 3 | `PrimeLocalClient.generate()` has no `_model_swapping` guard | `unified_model_serving.py` |
| 4 | No deadline propagation — existing v280.6 budget logic is dead code for triage | `runner.py` → `extraction.py` |
| 5 | EMA smoothing (alpha=0.35) keeps emergency state elevated, triggering repeated callbacks | `memory_quantizer.py` |

## Design

### Change 1: Memory Pressure Gate in `_maybe_run_email_triage()`

**File:** `backend/autonomy/agent_runtime.py` (~line 2780)

Insert after the cooldown check, before any runner/lock work:

```python
# Gate: refuse launch under memory pressure
try:
    from core.memory_quantizer import get_memory_quantizer_instance
    _mq = get_memory_quantizer_instance()
    if _mq:
        _thrash = getattr(_mq, 'thrash_state', 'healthy')
        if _thrash in ('thrashing', 'emergency'):
            self._triage_pressure_skip_count = getattr(
                self, '_triage_pressure_skip_count', 0
            ) + 1
            # Exponential backoff with jitter: 60s, 120s, 240s... capped at 600s
            import random
            backoff = min(600.0, interval * (2 ** min(self._triage_pressure_skip_count - 1, 4)))
            backoff *= (0.8 + 0.4 * random.random())  # ±20% jitter
            self._last_email_triage_run = now - interval + backoff
            logger.info(
                "[AgentRuntime] Email triage deferred: memory_state=%s, "
                "consecutive_skips=%d, next_attempt_in=%.0fs",
                _thrash, self._triage_pressure_skip_count, backoff,
            )
            # Drift guard: auto-disable extraction after 5 consecutive pressure blocks
            if self._triage_pressure_skip_count >= 5:
                import os
                os.environ.setdefault('EMAIL_TRIAGE_EXTRACTION_ENABLED', 'false')
                logger.warning(
                    "[AgentRuntime] Drift guard: extraction auto-disabled after %d "
                    "consecutive memory pressure blocks",
                    self._triage_pressure_skip_count,
                )
            return
        else:
            # Reset on healthy
            self._triage_pressure_skip_count = 0
except Exception:
    pass  # Gate failure = proceed (fail-open on gate itself)
```

**Why this works:**
- Hard gate: triage never launches into thrashing/emergency state
- Backoff with jitter: prevents synchronized retry bursts
- Drift guard: after 5 consecutive blocks, disables extraction (the expensive part) while keeping lightweight triage running
- Reason-coded telemetry: every skip is logged with state and backoff duration

### Change 2: `_model_swapping` Guard in `PrimeLocalClient.generate()`

**File:** `backend/intelligence/unified_model_serving.py` (~line 1138)

Insert before the `not self._loaded` check:

```python
# Fast-fail during model swap — don't queue behind the swap operation
if getattr(self, '_model_swapping', False):
    response.success = False
    response.error = "model_swap_in_progress"
    response.latency_ms = (time.time() - start_time) * 1000
    return response
```

**Why this works:**
- Callers (email triage extraction) catch the failure and fall through to heuristic extraction
- No queue contention with the swap operation
- Fast-fail, not a block — immediate return with reason code

### Change 3: Deadline Propagation in Email Triage

**File:** `backend/autonomy/email_triage/runner.py`

In `run_cycle()`, compute and propagate a deadline:

```python
# At the start of run_cycle():
_cycle_deadline = time.monotonic() + self._config.cycle_timeout  # or accept as parameter

# When calling extract_features():
features = await extract_features(
    email_data,
    router=self._router,
    deadline=_cycle_deadline,  # propagate budget
)
```

**File:** `backend/autonomy/email_triage/extraction.py`

Already accepts `deadline` parameter and passes it to `router.generate(deadline=deadline)`. The issue is `runner.py` passes `None`. Fix: pass the cycle deadline.

**File:** `backend/autonomy/agent_runtime.py`

Pass deadline to runner:

```python
deadline = time.monotonic() + timeout
report = await asyncio.wait_for(runner.run_cycle(deadline=deadline), timeout=timeout)
```

**Why this works:**
- Activates the existing v280.6 budget-aware timeout in `PrimeLocalClient.generate()` (line 1164-1167)
- Each `generate()` call automatically computes remaining budget and sets its own timeout
- No more unbounded inference blocking the full 30s triage window

### Change 4: Public `thrash_state` Property + Hysteresis Exit Thresholds

**File:** `backend/core/memory_quantizer.py`

Add public property:

```python
@property
def thrash_state(self) -> str:
    """Current thrash state: 'healthy', 'thrashing', or 'emergency'."""
    return self._thrash_state
```

Add hysteresis exit thresholds in `_check_thrash_state()` (~line 1382):

Currently, recovery from emergency requires rate dropping below `THRASH_PAGEIN_HEALTHY` (100) for `THRASH_RECOVERY_SUSTAINED_SECONDS` (20s). This is already good hysteresis for the healthy exit. But the transition from emergency → thrashing needs a separate exit threshold.

In the `rate > THRASH_PAGEIN_HEALTHY` deadband section (~line 1370):

```python
# Current: emergency -> thrashing when rate drops below WARNING
# Enhanced: use explicit exit threshold (70% of entry) to prevent flapping
THRASH_EXIT_RATIO = float(os.environ.get("THRASH_EXIT_RATIO", "0.7"))
emergency_exit = THRASH_PAGEIN_EMERGENCY * THRASH_EXIT_RATIO  # 2000 * 0.7 = 1400
if old_state == "emergency" and rate >= emergency_exit:
    return  # Hold emergency until rate drops below 1400
```

## Files Modified (4 files, 0 new files)

| File | Change Summary |
|---|---|
| `backend/autonomy/agent_runtime.py` | Memory gate + backoff + drift guard in `_maybe_run_email_triage` |
| `backend/intelligence/unified_model_serving.py` | `_model_swapping` fast-fail in `PrimeLocalClient.generate()` |
| `backend/autonomy/email_triage/runner.py` | Accept + propagate deadline to `extract_features` |
| `backend/core/memory_quantizer.py` | Public `thrash_state` property + hysteresis exit threshold |

## Acceptance Criteria

1. No repeated `email triage timeout -> emergency -> retry` loop for 30-60 min soak
2. Under pressure, triage is skipped/deferred deterministically (not partially executed)
3. No in-flight inference survives model swap teardown
4. Emergency callbacks do not trigger repeated offload churn
5. Memory pressure returns below threshold and stays stable with hysteresis
6. Every gate decision logs machine-parseable reason code (`memory_state=`, `consecutive_skips=`, `model_swap_in_progress`)

## Phase 2 (Future): Resource Orchestrator

Once the loop is broken, graduate into a centralized resource budget system:
- Global "heavy workload token" that ALL subsystems (triage, voice, agents) must acquire
- Shared pressure governance with admission control
- Backpressure-aware scheduling across all inference consumers
