# Ouroboros Activation Wiring — Design Doc

**Goal:** Resolve the 4 remaining activation blockers so JARVIS can autonomously self-develop across jarvis/prime/reactor-core repos with J-Prime generation and real-time voice narration.

**Scope:** 4 P0 items. No architectural changes — wiring/config only.

**Prerequisite:** B+ saga hardening (8 tasks, completed 2026-03-11).

---

## P0-1: Activate `JARVIS_SAGA_BRANCH_ISOLATION` in `.env`

**What:** Add `JARVIS_SAGA_BRANCH_ISOLATION=true` to `.env`.

**Why:** This is the only thing gating the B+ branch-isolated saga path (ephemeral branches, two-tier locks, ff-only promote gates, rollback-via-branch-delete). Without it, cross-repo applies use the legacy direct-to-HEAD path.

**How:** One line in `.env`. Requires process restart — the flag is read at import time via `os.getenv()` in `saga_apply_strategy.py:54`.

**Rollback:** Set to `false` → instant revert to legacy behavior.

**Pass criteria:**
1. `SagaApplyStrategy(...)._branch_isolation == True` during live saga execution after supervisor restart
2. Startup logs confirm `mode=governed` in governance health output
3. `JARVIS_GOVERNANCE_MODE=governed` explicitly present in `.env` (default is `sandbox`)

---

## P0-2: Wire SagaMessageBus as Passive Observer

**What:** Emit saga lifecycle events to `SagaMessageBus` for observability. No execution authority changes.

**Architecture:**

```
orchestrator._execute_saga_apply()
  │
  ├─► strategy.execute(ctx, patch_map)     ← UNCHANGED execution path
  │     ├─► bus.emit(SAGA_CREATED)          ← NEW: passive emit
  │     ├─► bus.emit(SAGA_ADVANCED)         ← per-repo apply step
  │     └─► bus.emit(SAGA_ROLLED_BACK)      ← on failure
  │
  ├─► strategy.promote_all()               ← UNCHANGED execution path
  │     ├─► bus.emit(SAGA_COMPLETED)        ← on full success
  │     └─► bus.emit(SAGA_PARTIAL_PROMOTE)  ← on partial failure
  │
  └─► orchestrator failure paths
        └─► bus.emit(SAGA_FAILED)           ← verify/postmortem failures
```

### Policy (6 rules)

1. **No execution authority shift:** `orchestrator → SagaApplyStrategy` remains the sole execution route.
2. **Emit saga lifecycle events:** SAGA_CREATED, SAGA_ADVANCED, SAGA_COMPLETED, SAGA_FAILED, SAGA_ROLLED_BACK, SAGA_PARTIAL_PROMOTE, TARGET_MOVED, ANCESTRY_VIOLATION.
3. **Side-effect-free handlers only:** No apply/verify/rollback decisions from bus handlers.
4. **Optional/fault-isolated:** If bus fails, execution continues with warning log.
5. **Rich event payload:** Each message carries `correlation_id`, `op_id`, `saga_id`, `repo`, `base_sha`, `promoted_sha`, `reason_code`, `schema_version`.
6. **TTL/message-history for debugging only.**

### Constraints (4 guardrails)

1. **No sync handler backpressure on hot path:** `_bus_emit()` is fire-and-forget. If a handler is slow, it doesn't block the apply path. Use non-blocking wrapper.
2. **Bounded message retention:** Strict TTL (default 300s) + max message count (default 500) + max correlation count. Long-running daemons don't grow unbounded memory.
3. **Schema pin for event payloads:** Add `schema_version: "1.0"` to metadata. Validate required keys (`op_id`, `saga_id`, `reason_code`) at emit time.
4. **Emit from orchestrator boundaries too:** Emit SAGA_FAILED from orchestrator post-verify and postmortem paths, not just strategy internals.

### Observer-only invariant

Bus is **never consulted for execution decisions**. Dropping all bus handlers does not alter saga terminal state.

### Wiring point

- `SagaApplyStrategy.__init__` gets optional `message_bus` parameter (default `None`).
- GLS creates bus at startup, passes to orchestrator config, which passes to strategy.
- Bus lifetime = GLS lifetime (survives across operations for message history).

### Fault isolation wrapper

```python
def _bus_emit(self, msg_type: str, **kwargs) -> None:
    if self._bus is None:
        return
    try:
        self._bus.send(SagaMessage(
            message_type=msg_type,
            metadata={"schema_version": "1.0", **kwargs},
        ))
    except Exception:
        logger.debug("[Saga] bus emit failed for %s (non-fatal)", msg_type)
```

### Pass criteria

1. After one cross-repo saga, bus contains ordered lifecycle events (`prepare`, `apply_repo`, `promote_repo` or failure equivalent).
2. Dropping bus handlers does not alter saga terminal state.
3. Bus not consulted for any execution decision (observer-only invariant).
4. Bus failure = warning log, execution continues.

---

## P0-3: Wire TestFailureSensor with Real TestWatcher

**What:** IntakeLayerService currently creates `TestFailureSensor` without a `TestWatcher`, so the sensor's `start()` no-ops and no polling happens. Wire a real `TestWatcher` per repo.

**Current state (broken):**

```python
# intake_layer_service.py:353-356
test_failure_sensors = [
    TestFailureSensor(repo=rc.name, router=self._router)  # No test_watcher!
    for rc in enabled_repos
]
```

**Fix:** Construct `TestWatcher` per repo and pass to `TestFailureSensor`:

```python
from backend.core.ouroboros.governance.intent.test_watcher import TestWatcher

test_failure_sensors = [
    TestFailureSensor(
        repo=rc.name,
        router=self._router,
        test_watcher=TestWatcher(
            repo=rc.name,
            repo_path=str(rc.local_path),
            poll_interval_s=float(os.environ.get("JARVIS_INTENT_TEST_INTERVAL_S", "300")),
        ),
    )
    for rc in enabled_repos
]
```

**Poll interval:** Defaults to 300s via `JARVIS_INTENT_TEST_INTERVAL_S`. To get 30s polling, set the env var explicitly. The design doc does not mandate a specific interval — that's operational config.

**Pass criteria:**
1. Each enabled repo gets a `TestWatcher(repo=<repo>, repo_path=<repo_root>, poll_interval_s=<from_env>)`.
2. `TestFailureSensor._watcher is not None` for each sensor.
3. `_poll_loop` task created on `start()`.
4. Behavioral: induced failing test emits stable `intent:test_failure` envelope after 2 consecutive polls (streak >= 2 required by TestWatcher).

---

## P0-4: Config Propagation Fixes

### P0-4a: `keep_failed_saga_branches` not propagated

**Current:** `SagaApplyStrategy` accepts `keep_failed_saga_branches` in constructor, but orchestrator constructs it without passing the value.

**Fix:** Add `JARVIS_SAGA_KEEP_FORENSICS_BRANCHES` env var (default `true`). Orchestrator reads it and passes to `SagaApplyStrategy(keep_failed_saga_branches=...)`.

**Pass criteria:** Configurable via env var; default `true`; crashed saga branches retained for postmortem debug.

### P0-4b: Orphan detection repo path mismatch

**Current:** `_detect_orphan_branches()` in GLS uses `self._config.repo_registry`, but GLS stores the registry on `self._repo_registry` (set during `start()`).

**Fix:** Use `self._repo_registry` (the live instance) with fallback to `self._config.repo_registry` then `self._config.project_root`.

**Pass criteria:** `health().orphan_saga_branches` includes branch hits from all 3 enabled repos, not jarvis-only.

---

## Explicitly Out of Scope (Defer)

| Item | Reason |
|------|--------|
| Brain handshake fully fail-closed | Current degraded path (gate disabled) is operationally safe |
| Supervisor crash mid-saga re-entry | Orphan detection + forensics branches provide recovery; deterministic re-entry is larger design |
| Sensor task lifecycle tracking | Sensors start once at Zone 6.9; restart tears down entire kernel |
| Backpressure/starvation patterns | Single-writer invariant + file_touch_cache cooldown already rate-limit |
| Human gate replay idempotency | Dedup via envelope signature (60s window) in UnifiedIntakeRouter |

---

## Activation Sequence (Post-Implementation)

1. `.env` updated with `JARVIS_SAGA_BRANCH_ISOLATION=true` + confirm `JARVIS_GOVERNANCE_MODE=governed`
2. `python3 unified_supervisor.py --force`
3. Zone 6.8: capability contract pings J-Prime at env-resolved endpoint (hard-fail if unreachable)
4. Zone 6.8: brain inventory handshake (hard-fail if required brain missing; gate disabled on other errors)
5. Zone 6.8: GLS starts, creates SagaMessageBus, wires to orchestrator
6. Zone 6.9: IntakeLayerService fans out sensors per enabled repo:
   - `TestFailureSensor` with `TestWatcher` — polls at `JARVIS_INTENT_TEST_INTERVAL_S` interval (default 300s)
   - `OpportunityMinerSensor` — scans at `JARVIS_INTAKE_MINER_SCAN_INTERVAL_S` interval (default 300s)
7. On detection → orchestrator pipeline: CLASSIFY → ROUTE → CONTEXT_EXPANSION → GENERATE (J-Prime) → VALIDATE → GATE → APPROVE (human blocks) → APPLY (B+ saga) → VERIFY → PROMOTE → COMPLETE
8. SagaMessageBus passively records lifecycle events (observer-only)
9. VoiceNarrator announces significant phases via `safe_say()`

---

## GO/NO-GO Criteria

| # | Criterion | Gate |
|---|-----------|------|
| 1 | `SagaApplyStrategy._branch_isolation == True` after restart | P0-1 |
| 2 | Startup logs `mode=governed` | P0-1 |
| 3 | Bus contains ordered lifecycle events after saga run | P0-2 |
| 4 | Dropping bus handlers doesn't change terminal state | P0-2 |
| 5 | `TestFailureSensor._watcher is not None` per repo | P0-3 |
| 6 | Induced failing test → stable envelope after 2 polls | P0-3 |
| 7 | `health().orphan_saga_branches` covers all enabled repos | P0-4 |
| 8 | `keep_failed_saga_branches` configurable via env | P0-4 |
