# Phase 2C.2/2C.3 — IntakeLayerService + Loop Activation Design

**Date:** 2026-03-08
**Status:** Approved
**Phase:** 2C.2 (Supervisor boot wiring) + 2C.3 (Voice narration)

---

## Problem Statement

Phase 2C.1 delivered a complete intake layer (WAL-backed `UnifiedIntakeRouter`, 4 sensors,
`IntentEnvelope` contract). However, nothing in the supervisor starts it. The intake layer
is idle: sensors never scan, no envelopes reach `GovernedLoopService.submit()`. Separately,
`VoiceNarrator` (CommProtocol B-layer) silently fails at boot because
`from backend.audio import safe_say` imports a non-existent module.

**Goal:** Make JARVIS autonomously ingest intents and narrate them by fixing both gaps.

---

## Architecture

### Plane Model (unchanged)

```
Supervisor (Zone 6.8)  GovernedLoopService   ← execution truth
Supervisor (Zone 6.9)  IntakeLayerService    ← intake lifecycle owner
                            │
                    UnifiedIntakeRouter
                    ├── BacklogSensor
                    ├── TestFailureSensor
                    ├── VoiceCommandSensor
                    └── OpportunityMinerSensor
                            │
                    A-Narrator (preflight awareness)
                    B-Narrator (VoiceNarrator via CommProtocol)  ← fixed
```

### Narration Architecture (A + B hybrid)

| Layer | When | Language | Authority |
|-------|------|----------|-----------|
| A (intake) | High-salience envelope ingested | "detected / queued" | Non-authoritative |
| B (CommProtocol) | GLS runs INTENT/DECISION/POSTMORTEM | "analyzing / applying / complete" | Source of truth |

**A-narrator salience policy:**
- `source == "voice_human"` → always speak (critical human command)
- `source == "test_failure"` AND count ≥ 2 → speak ("N failures detected")
- `source == "backlog"` or `"ai_miner"` → silent (B layer only)
- Disposition narration required: if A spoke but op is dead-lettered/dropped,
  A must follow up ("signal not admitted: policy/rate limit")

**Dedup/correlation:**
- A uses `envelope.causal_id` as correlation key
- B CommProtocol carries same `causal_id` → user hears one coherent story per op
- QoS precedence: B terminal messages preempt any pending A utterance for same `causal_id`

**Voice injection (Dependency Injection):**
- `say_fn` is injected from supervisor at boot — never hard-imported inside governance layer
- Supervisor passes `safe_say` from `backend.core.supervisor.unified_voice_orchestrator`
- `governance/integration.py` import fixed: `from backend.core.supervisor.unified_voice_orchestrator import safe_say`

---

## Components

### 1. `IntakeLayerService` (new file)
**Path:** `backend/core/ouroboros/governance/intake/intake_layer_service.py`

Mirrors `GovernedLoopService` pattern:
- `__init__(gls, config, say_fn)` — no side effects in constructor
- `async start()` — builds router + sensors + A-narrator, starts all
- `async stop()` — stops sensors first, then router (drain order)
- `health()` — returns dict: queue_depth, oldest_pending_age_s, dead_letter_count, per_source_rate
- `state` property — `ServiceState` enum (INACTIVE/STARTING/ACTIVE/DEGRADED/STOPPING/FAILED)

### 2. `IntakeNarrator` (new class, same file)
Salience-gated A-narrator:
- `async on_envelope(envelope: IntentEnvelope)` — called by router post-ingest
- Filters by source + count policy
- Carries `causal_id` on all utterances
- Tracks pending utterances by `causal_id`; suppresses superseded on terminal B event
- Calls `say_fn(text, source="intake_narrator")`

### 3. Supervisor Zone 6.9 (modify `unified_supervisor.py`)
```
# ---- Zone 6.9: Intake Layer Service ----
if self._governed_loop and self._governed_loop.state in (ACTIVE, DEGRADED):
    try:
        from backend.core.ouroboros.governance.intake.intake_layer_service import (
            IntakeLayerService, IntakeLayerConfig,
        )
        from backend.core.supervisor.unified_voice_orchestrator import safe_say
        _intake_config = IntakeLayerConfig.from_env(config=_loop_config)
        self._intake_layer = IntakeLayerService(
            gls=self._governed_loop,
            config=_intake_config,
            say_fn=safe_say,
        )
        await asyncio.wait_for(
            asyncio.shield(self._intake_layer.start()),
            timeout=30.0,
        )
    except BaseException as exc:
        self._intake_layer = None
        logger.warning("[Kernel] Zone 6.9 intake layer failed: %s -- skipped", exc)
```

**Stop order (reverse):** Zone 6.9 stop → Zone 6.8 stop (drain intake before draining GLS).

### 4. VoiceNarrator B-layer fix (modify `governance/integration.py`)
```python
# Before (broken):
from backend.audio import safe_say

# After (correct):
from backend.core.supervisor.unified_voice_orchestrator import safe_say
```

---

## Delivery Semantics

- **Intake**: at-least-once (WAL guarantees replay on restart)
- **Execution**: idempotent (dedup_key prevents double-submit to GLS)
- **NOT** exactly-once end-to-end (acceptable: GLS idempotency is the guard)

---

## Health Model

`IntakeLayerService.health()` returns:
```python
{
    "state": "active",
    "queue_depth": 3,
    "oldest_pending_age_s": 12.4,
    "dead_letter_count": 0,
    "per_source_rate": {
        "backlog": 0.2,      # envelopes/min
        "test_failure": 0.0,
        "voice_human": 0.0,
        "ai_miner": 0.0,
    },
    "wal_entries_pending": 1,
}
```

---

## Edge Cases

| Case | Mitigation |
|------|-----------|
| A spoke but op never admitted | A-narrator mandatory disposition follow-up ("signal not admitted: …") |
| In-flight file conflict (voice vs miner) | Router deterministic arbitration: voice_human wins; miner dead-lettered |
| Restart replay re-enqueues old envelopes | Dedup via durable `causal_id + dedup_key` in WAL-replayed router |
| Narration lag after terminal phase | A-narrator cancels pending utterance when B POSTMORTEM fires for same `causal_id` |
| Dead-letter storm on one file | Per-file circuit-breaker: 3 dead-letters in 60s → quarantine TTL + log warning |
| Multiple supervisors | Single active lease via existing DLM (`distributed_lock_manager`) before starting Zone 6.9 |

---

## Stop / Shutdown Order

```
supervisor.shutdown()
  → self._intake_layer.stop()   # Zone 6.9 first: drain sensors → drain router queue
  → self._governed_loop.stop()  # Zone 6.8 second: drain in-flight ops
  → self._governance_stack.stop()
```

---

## Files Touched

| Action | File |
|--------|------|
| Create | `backend/core/ouroboros/governance/intake/intake_layer_service.py` |
| Modify | `backend/core/ouroboros/governance/intake/__init__.py` |
| Modify | `backend/core/ouroboros/governance/__init__.py` |
| Modify | `unified_supervisor.py` (Zone 6.9 + shutdown order) |
| Fix | `backend/core/ouroboros/governance/integration.py` (VoiceNarrator import) |
| Test | `tests/governance/intake/test_intake_layer_service.py` |
| Test | `tests/governance/integration/test_phase2c2_acceptance.py` |

---

## Success Criteria

1. `IntakeLayerService.start()` reaches ACTIVE/DEGRADED within 30s at supervisor boot
2. A voice command via `VoiceCommandSensor` reaches `GovernedLoopService.submit()` within 1s of being ingested
3. `VoiceNarrator` (B) narrates INTENT/DECISION/POSTMORTEM — no silent-fail at boot
4. A-narrator speaks "voice command queued" for `source=voice_human` and is silent for backlog/miner
5. Disposition narration fires when a voice_human envelope is dead-lettered
6. 439+ tests pass with 0 regressions
7. `IntakeLayerService.health()` returns accurate queue/dead-letter metrics
