---
title: Project Spec2 Cybernetic Reanimation
modules: [tests/governance/test_reanimation_kernel_wiring.py, scripts/_distillation_surgeon.py, backend/core/cybernetic_reanimation.py, tests/governance/test_cybernetic_reanimation.py, unified_supervisor.py]
status: historical
source: project_spec2_cybernetic_reanimation.md
---

**Spec 2 — Cybernetic Reanimation. COMPLETE — foundation (PR #69500, main `a5a7e547`) + live-kernel wiring + Shadow Mode chokepoints (PR #69502, main `30e0e1ad`).**

**LIVE WIRING DONE (PR #69502):** `unified_supervisor.py` (repo ROOT) `build_resilience_dispatcher(organs)` registers all 7 organs (operator-confirmed: SelfHealingOrchestrator, LoadSheddingController, GracefulDegradationManager, AutoScalingController, AnomalyDetector, ProcessHealthPredictor, AdvancedCircuitBreaker) keyed to PressureSignalTypes. Shadow chokepoint #1: `SelfHealingOrchestrator._execute_remediation` wraps `await handler(component)` in `shadow_guard_async` → trapped+logged in shadow mode. #2: `LoadSheddingController.with_shedding` gates the `raise RuntimeError("Request rejected")` → logs + lets request through in shadow mode. 5 sandbox-off tests (`tests/governance/test_reanimation_kernel_wiring.py`) prove ANOMALY→SelfHealing→calculate→shadow-trap-kill + shadow-off-executes + LoadShedding-trap + all-7-register. BUG CAUGHT by kernel-import test: these classes have NO module-level `logger` → used shadow_guard's default logger + explicit logging.getLogger. NotificationChannelEnum-style: operator chose direct execution (no spec doc) to avoid the autonomous loop racing the live-kernel wiring.

**[Original foundation-pending note below is now superseded — wiring is DONE.]**
**Spec 2 — Cybernetic Reanimation. FOUNDATION COMPLETE (PR #69500, main `a5a7e547`); live-kernel muscle-wiring PENDING.** Follows Slice 250 Sovereign Distillation ([[project_slice250_sovereign_distillation]]) — reanimates the resilience organs that survived the purge. NO formal spec doc exists (only the operator's prose plan; Spec 1 deferred this as "a separate spec 2026-06-14-cybernetic-reanimation-design.md" which was never written).

**Phase 1 (DONE, PR #69499 main aca2573b):** deleted `scripts/_distillation_surgeon.py` (campaign tooling).

**Foundation (DONE, PR #69500 main a5a7e547) — `backend/core/cybernetic_reanimation.py` (DECOUPLED, duck-typed, no kernel import → unit-testable in-sandbox):**
- `PressureSignalEmitter.observe(type, source, active, ...)` — EDGE-TRIGGERED typed signals (`PressureSignalType`: RESOURCE_PRESSURE/ANOMALY_DETECTED/COMPONENT_DEGRADED). Emits one RISING edge on become-active, one FALLING on clear; sustained → no re-emit. NEVER raises.
- `EventActivationDispatcher` — `register_organ(name, async_handler, [signal_types])` + `dispatch(signal)` (async, FAIL-SOFT per organ) + `attach_to_bus(bus, extract=)` (bridges SupervisorEventBus via create_task, non-blocking).
- Shadow Mode: `resilience_shadow_mode_enabled()` (env `JARVIS_RESILIENCE_SHADOW_MODE` default-TRUE, fail-safe-on-error) + `shadow_guard(action_desc, execute_fn)` / `shadow_guard_async` → in shadow mode logs `[SHADOW MODE] Would have <action>` + returns `SHADOW_TRAPPED` WITHOUT executing; off → executes. The single chokepoint for all kill/shed/restart.
- 12 tests (`tests/governance/test_cybernetic_reanimation.py`) incl Phase 4 (signal→organ wakes→shadow traps the command).

**PENDING — live-kernel muscle-wiring (needs operator confirmation of the organ list):**
- **The "7 surviving resilience organs" are NOT enumerated anywhere.** Candidates in unified_supervisor.py: `GracefulDegradationManager`(SystemService @27195), `AutoScalingController`(SystemService @33882), `LoadSheddingController`(SystemService @40650), `SelfHealingOrchestrator`(@29269), `TrinityCircuitBreaker`(@18253), `TrinityHealthMonitor`(@26951), `AdvancedCircuitBreaker`(@30489), `SmartWatchdog`(@11259). (LegacyDegradationManager is a KEPT governed organ from Spec 1, not a reanimation target.) NEED operator to confirm WHICH 7.
- **Shadow-mode interception points (found, ready to wire):** `SelfHealingOrchestrator._execute_remediation` (@29408) + `LoadSheddingController` shed path (`_update_shedding_state` @40716 / `with_shedding` @40764). Wrap their dangerous actions in `shadow_guard`.
- Event infra to reuse: `SupervisorEventBus` (@8881, subscribe/emit, singleton @9036), `SystemServiceRegistry`.
- This wiring edits the LIVE 102K kernel + needs `import unified_supervisor` (sandbox-blocked by split_brain_guard → use dangerouslyDisableSandbox for kernel-import tests, like Slice 250 Phase B/C governance tests).

**Process:** built in OCA-owned worktree off clean origin/main (`ledger_sovereignty.mark_owned`). Whenever the live wiring proceeds: confirm the 7 organs, wrap the 2 interception sites in shadow_guard, register organs on the dispatcher, prove with kernel-import integration tests (sandbox-off).
