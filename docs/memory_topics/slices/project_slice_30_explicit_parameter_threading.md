---
title: Root cause
modules: []
status: historical
source: project_slice_30_explicit_parameter_threading.md
---

PR #59095 squash-merged 2026-05-27 at `37215680a8`. Branch `ouroboros/slice-30-explicit-parameter-threading`. Closes v23 (`bt-2026-05-27-045049`) production wiring gap: 12 EXHAUSTION events at elapsed=30.00s ALL with primary_budget=30.0s (static cap), NOT Slice 28's adaptive 75s heavy-model budget.

# Root cause

Slice 28 Phase 2 read model_id from `topology_sentinel.get_dw_model_override()` ContextVar inside `_call_primary`. The Slice 23 sentinel walker SET it, but the value was empty when READ across the async/semaphore boundary — classic ContextVar propagation gap.

# Refactor

3 signatures gain `*, model_id: str = ""`:
- `_call_primary(ctx, deadline, *, model_id="")`
- `_try_primary_then_fallback(ctx, deadline, *, model_id="")`
- `_compute_primary_budget(total_s, *, model_id="")` (Slice 28 substrate already had this)

Sentinel walker passes `model_id=model_id` (loop variable) → `_try_primary_then_fallback` forwards → `_call_primary` uses directly → `_compute_primary_budget` engages heavy scalar.

# What stays ContextVar (legitimate)

`topology_sentinel`'s `set_dw_model_override`/`get_dw_model_override`/`reset_dw_model_override` + `DW_MODEL_OVERRIDE_VAR` REMAIN for `DoublewordProvider._resolve_effective_model` internal routing. Two-layer separation: TRANSPORT PARAMETER (timeout, now explicit) vs PROVIDER ROUTING (which model to call, still ContextVar).

# Verification

9 tests (3 AST + 6 spine). 306/306 regression. AST pins prevent reversion: `_call_primary` body BANS `get_dw_model_override` / `_attempted_model_id` / `_slice28_get_model_override` references.

# v24 expected behavior

For the first time, Slice 28 Phase 2 adaptive budget engages in production:
- Heavy 397B/Kimi → 75s primary budget (vs v23's 30s)
- Within 75s, streaming layer's 120s TTFT can fully fire
- If model serves first token → JSON parse → APPLY → potentially RESOLVED

The v23 12-EXHAUSTION-at-30s pattern should NOT recur. If 75s still isn't enough, DW endpoint is structurally slower than 75s.

# v24 status

Launched 2026-05-27T18:36:58Z PID 13002. Slice 26 caffeinate + Slice 25B preflight (3 models, Qwen-4B demoted from v19 persisted).

Related: [[project_slice_29_preflight_recovery_daemon]] (sibling boot resilience), [[project_slice_28_adaptive_ttft_horizon]] (substrate Slice 30 finally engages), [[feedback_no_preresult_euphoria]] (Slice 30 = methodology; v24 RESOLVED is capability bar).
