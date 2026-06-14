# Spec 2 — Cybernetic Reanimation (Phase C)

**Date:** 2026-06-14
**Author:** Derek J. Russell (O+V architect) / Claude
**Target:** `unified_supervisor.py` @ `origin/main` (post-Distillation, 98,877 lines) + a new focused module.
**Depends on:** Spec 1 (Sovereign Distillation, merged via #69497 / #69498).
**Execution:** Manual, on an isolated OCA-owned worktree. Spec is committed together with the implementation in the final PR (NOT committed early — the autonomous loop ingests committed specs).

---

## 1. Problem Statement

The event-driven resilience infrastructure already exists in `unified_supervisor.py` but is **dead config**:
- `SystemServiceRegistry` supports `ActivationContract.trigger_events` + an `event_driven` activation mode, **but nothing dispatches `SupervisorEventBus` events to `activate_service()`** — so contracts never fire.
- Seven resilience organs survive (each defined once on `origin/main`): `SelfHealingOrchestrator`, `ProcessHealthPredictor`, `AutoScalingController`, `AnomalyDetector`, `GracefulDegradationManager`, `LoadSheddingController`, `AdvancedCircuitBreaker` — none are wired to fire on system pressure.
- There are no typed "pressure" signals on the bus to drive them.

Phase C builds the missing bridge + the signals + the registration, turning the dead infrastructure into a live, **edge-triggered, asynchronous reactive nervous system** — no hardcoded polling loops, all cadences/thresholds env-tunable, fail-soft, default-OFF until soak-validated.

## 2. Architecture Refinement (vs. the inline design)

The reactive logic is implemented in a **new focused module** `backend/core/ouroboros/governance/resilience_reanimation.py`, NOT inline in `unified_supervisor.py`. Rationale:
- **Testability:** `import unified_supervisor` raises `RuntimeError: split-brain-guard` in sandbox/CI-without-lockdirs. A standalone module is importable and unit-testable with a mock bus/registry — which is the only way to satisfy this spec's headline integration proof in the available environment.
- **Modularity:** the kernel file is already 99K lines; new subsystems belong in focused modules (matches the `governance/` layout).
- The kernel's only change is a **minimal master-flag-guarded hook** that constructs and starts the reanimation layer, passing it the kernel's existing `SupervisorEventBus` + `SystemServiceRegistry`. When the master flag is OFF the hook is not constructed → kernel behavior is **byte-identical**.

## 3. Guiding Constraints

- **Root-cause, no shortcuts.** Build the genuinely missing dispatcher; reuse `ActivationContract`/`SystemServiceRegistry`/`SupervisorEventBus` as-is — do not invent a parallel mechanism.
- **No hardcoding.** Every interval/threshold/cap reads from a `JARVIS_*` env var with a sensible default, registered in `FlagRegistry`.
- **Edge-triggered.** Emitters publish only on threshold *transitions*, never every tick — prevents event storms.
- **Fail-soft.** Every handler/sample is wrapped; a failure logs + continues, never crashes the kernel.
- **Default-OFF + graduation.** Master `JARVIS_RESILIENCE_REANIMATION_ENABLED` defaults `false`; OFF path byte-identical; graduate after soak.
- **Absolute observability.** Dispatcher + emitter emit structured telemetry (`[Reanimation] ...`).

## 4. Components

### 4.1 `EventActivationDispatcher` (the bridge — root fix)
- Constructed with `(event_bus, service_registry)`. On `start()`, calls `event_bus.subscribe(self._on_event)`.
- `async _on_event(event: SupervisorEvent)`: looks up registry service descriptors whose `ActivationContract.trigger_events` contains `event.event_type.value`; for each match, `await service_registry.activate_service(name)`. The registry already enforces `dependency_gate` / `budget_gate` / `backoff_gate` / `max_activations_per_hour` / `deactivate_after_idle_s` — the dispatcher adds **no** new policy, it only connects.
- Fault-isolated per match (one failing activation never blocks others); structured telemetry per dispatch.
- Requires a registry accessor for "descriptors with contracts" — if `SystemServiceRegistry` lacks a public iterator, add a minimal read-only `iter_event_driven()` accessor (pure-add, no behavior change).

### 4.2 `SupervisorEventType` extension + `PressureSignalEmitter`
- Add three additive enum values: `RESOURCE_PRESSURE = "resource_pressure"`, `ANOMALY_DETECTED = "anomaly_detected"`, `COMPONENT_DEGRADED = "component_degraded"`.
- `PressureSignalEmitter` runs as a kernel background task (registered via `KernelBackgroundTaskRegistry`, reusing the async spine — no bespoke loop). Each tick (interval `JARVIS_PRESSURE_SAMPLE_INTERVAL_S`, default 15) it samples:
  - memory pressure via existing `DynamicRAMMonitor`,
  - CPU/load via `IntelligentResourceOrchestrator`/`psutil`,
  - component health via the existing `_health_monitor_loop` state.
- **Edge-triggered:** keeps the last emitted level per signal; emits a typed `SupervisorEvent` only when a level *transition* crosses an env threshold (`JARVIS_PRESSURE_MEM_THRESHOLD`, `JARVIS_PRESSURE_CPU_THRESHOLD`, …). Payload carries level/metric/component in `metadata`.
- Fail-soft sampling (a probe error logs + skips that signal for the tick).

### 4.3 Muscle Registration (7 organs, event-driven contracts)
Register each surviving organ as a `ServiceDescriptor` with an `ActivationContract` (mode `event_driven` unless noted). Reuse each organ's existing API:

| Organ | `trigger_events` | Action |
|---|---|---|
| `GracefulDegradationManager` | `resource_pressure` | disable low-priority features for the level (event-driven, replacing its internal 10s poll) |
| `LoadSheddingController` | `resource_pressure` | `record_load()` from payload; shed by priority |
| `AutoScalingController` | `resource_pressure` | `evaluate()` → scale callbacks |
| `AnomalyDetector` | `component_degraded`, `metric` | `record_observation()`; on anomaly → **emit `anomaly_detected`** |
| `ProcessHealthPredictor` + `SelfHealingOrchestrator` | fed by existing `_health_monitor_loop` + `component_degraded` | predictor scores health → `SelfHealingOrchestrator.check_and_remediate()` |
| `AdvancedCircuitBreaker` | wrapped on Trinity/provider calls | on OPEN → **emit `component_degraded`** |

**Feedback loops:** `AnomalyDetector` emits `anomaly_detected` and breakers emit `component_degraded` — the dispatcher re-dispatches these, closing the reactive matrix through the one bus.

### 4.4 Kernel hook (minimal, guarded)
In `JarvisSystemKernel` boot, behind `if reanimation_enabled():` construct `ReanimationLayer(event_bus, service_registry, kernel_signals)` and register its emitter task + dispatcher start. OFF → not constructed → byte-identical.

## 5. Flags (no hardcoding; all in `FlagRegistry`)
- Master: `JARVIS_RESILIENCE_REANIMATION_ENABLED` (default `false`).
- Per-organ: `JARVIS_REANIMATE_<ORGAN>_ENABLED` (default `true` when master on).
- `JARVIS_PRESSURE_SAMPLE_INTERVAL_S` (15), `JARVIS_PRESSURE_MEM_THRESHOLD`, `JARVIS_PRESSURE_CPU_THRESHOLD`, `JARVIS_PRESSURE_DEGRADED_THRESHOLD` — sensible defaults, env-overridable.

## 6. Testing & Verification (the proof, runnable in-sandbox via the extracted module)
- **Unit (module imported in isolation, mock bus/registry):**
  - Dispatcher: emit event whose type ∈ a service's `trigger_events` → that service's `activate_service` awaited; non-matching event → not activated; one failing activation doesn't block others.
  - Emitter: rising metric across threshold → exactly one typed event; staying above → no repeat (edge-trigger); dropping below then rising → new event; probe exception → fail-soft skip.
  - Each organ adapter: event payload → correct organ method called.
- **Integration (module-level, the headline proof):** construct `ReanimationLayer` with a real `SupervisorEventBus` + a fake registry holding the 7 contracts; emit a synthetic `RESOURCE_PRESSURE` → assert `GracefulDegradationManager`/`LoadSheddingController`/`AutoScalingController` activate.
- **OFF byte-identical:** master OFF → `ReanimationLayer` not constructed; prove the kernel hook is the only change and it's flag-guarded (code inspection + a test that the layer factory returns a no-op sentinel when disabled).
- **No-regression:** `unified_supervisor.py` still `ast.parse`s; shadow-guard test green; governance tests still collect.

## 7. Slice Plan (each independently green)
- **C.1** New module skeleton + `EventActivationDispatcher` + registry `iter_event_driven()` accessor + unit tests (mock bus/registry). Kernel hook (guarded, default-off).
- **C.2** `SupervisorEventType` += 3 values + `PressureSignalEmitter` (edge-triggered, fail-soft) + unit tests.
- **C.3** Register the 7 organs with `ActivationContract`s + per-organ adapter unit tests.
- **C.4** Feedback emitters (`AnomalyDetector`, `AdvancedCircuitBreaker`) + the synthetic `RESOURCE_PRESSURE` integration proof + flag registration + final verification.

## 8. Risks & Mitigations
| Risk | Mitigation |
|---|---|
| Event storms | edge-triggered emit + bus's bounded queue/drop-oldest |
| Activation thrash | existing `max_activations_per_hour` / `backoff_gate` / `deactivate_after_idle_s` |
| Kernel import unrunnable in sandbox | logic in standalone importable module; unit/integration tests there |
| Behavior change | master default-OFF; OFF byte-identical; soak before graduation |
| Double-driving (organ's own loop + events) | `GracefulDegradationManager` switched to event-driven; its internal poll disabled under reanimation |

## 9. Definition of Done
- New module with dispatcher + emitter + organ adapters; all unit + integration tests green (runnable in sandbox).
- `SupervisorEventType` has the 3 new values; kernel hook guarded by master flag; OFF byte-identical.
- 7 organs registered with contracts; synthetic `RESOURCE_PRESSURE` activates the pressure-tier organs in test.
- All flags in `FlagRegistry`; `ast.parse` + shadow-guard + governance collection still green.
- Spec + implementation committed together in one PR (master default-OFF).
