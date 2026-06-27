---
title: Project Slice252 Shadow Telemetry
modules: [tests/governance/test_slice252_shadow_telemetry.py]
status: merged
source: project_slice252_shadow_telemetry.md
---

**Slice 252 — Shadow-Telemetry & Real-Time Auditing. MERGED PR #69503, main `a00c3860`.** Closes the Shadow Mode observability deficit from Spec 2 ([[project_spec2_cybernetic_reanimation]]): trapped resilience actions now emit structured telemetry to the live broker, not just text logs.

**Built (reuses Slice-249 StreamEventBroker — no parallel path):**
- `ide_observability_stream.py`: `EVENT_TYPE_SHADOW_ACTION_TRAPPED="shadow_action_trapped"` + `_VALID_EVENT_TYPES` entry + `publish_shadow_action_trapped(organ_name, intended_action, triggering_signal, op_id)` (non-blocking, NEVER raises, None when stream disabled). Payload keys: organ_name/intended_action/triggering_signal/op_id.
- `cybernetic_reanimation.py`: `emit_shadow_trap(organ, action, signal=None)` (lazy broker import → keeps module decoupled/in-sandbox-testable; fail-soft); `shadow_guard`/`shadow_guard_async` gain `organ=` kwarg + emit on trap; **`_current_signal_var` ContextVar** — `EventActivationDispatcher.dispatch` sets the triggering signal around each handler (set/reset per organ) so the DEEP guard call-site attributes a trap to its signal WITHOUT threading signatures (async-safe). signal repr = `f"{type.value}:{source}:{edge.value}"`.
- `unified_supervisor.py`: `SelfHealingOrchestrator._execute_remediation` shadow_guard_async call passes `organ="SelfHealingOrchestrator"`; `LoadSheddingController.with_shedding` shadow branch calls `emit_shadow_trap("LoadSheddingController", f"shed (reject) request: {action}")`.

**Tests:** `tests/governance/test_slice252_shadow_telemetry.py` 11 decoupled (in-sandbox): event registered, publish/emit register structured payload in broker.recent_history(), shadow_guard emits-on-trap (not when shadow-off), dispatch ContextVar attributes signal end-to-end. + 1 kernel-chain proof added to `test_reanimation_kernel_wiring.py` (sandbox-off): ANOMALY→real SelfHealing→trap→broker registers SHADOW_ACTION_TRAPPED w/ organ_name + triggering_signal="anomaly_detected:proc-victim:rising" + "execute remediation". 37 green incl Slice 249 + reanimation foundation, zero regression.

**Patterns reused this session (now well-worn):** OCA-owned worktree (`ledger_sovereignty.mark_owned`) off clean origin/main; kernel-import tests run `dangerouslyDisableSandbox` (split_brain_guard lock-dir); broker readback via `recent_history()`/`event_types()` + `reset_default_broker()`; StreamEvent has `.event_type`/`.payload`.
