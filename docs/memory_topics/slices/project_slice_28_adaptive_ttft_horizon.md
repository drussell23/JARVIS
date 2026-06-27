---
title: Phase 2 — Adaptive Primary Budget
modules: []
status: historical
source: project_slice_28_adaptive_ttft_horizon.md
---

PR #59084 squash-merged 2026-05-26 at `a79774ffdf`. Branch `ouroboros/slice-28-adaptive-ttft-horizon`. Closes v21 (`bt-2026-05-27-025855`) 30s primary cap wedge.

# Phase 2 — Adaptive Primary Budget

`_compute_primary_budget(total_s, *, model_id="")` now applies 2.5× scalar when `_is_heavy_model(model_id)` (Qwen-397B, Kimi-K2.6). Cap 240s. Env: `JARVIS_PRIMARY_HEAVY_TTFT_SCALAR` (default 2.5) + `JARVIS_PRIMARY_HEAVY_TTFT_CAP_S` (default 240). Legacy callers preserved byte-identical.

`_call_primary` reads dispatcher's per-attempt model_id via `topology_sentinel.get_dw_model_override()` (Slice 23 ContextVar) and threads to `_compute_primary_budget`. Defensive — missing/unstamped override → legacy 30s path.

# Phase 3 — Inline Fault Discriminator

On TimeoutError from `_call_primary.wait_for`, fires 2-token probe via `self._primary.prompt_only` (Slice 27 Phase 2 Aegis-stabilized) with 5s wall. Classifies `context_lag` (probe fast → endpoint alive) vs `infrastructure_outage` (probe failed → endpoint dead). Pure observability — never raises, never changes return. Env-gated: `JARVIS_TTFT_FAULT_DISCRIMINATOR_ENABLED` (default false; v22 enabled it).

# Verification

12 tests (3 AST + 9 spine). 285/285 regression (exceeds operator's 273 target).

# v22 forensic — Slice 25B fail-fast PROVED in production

v22 launched 2026-05-27T03:46:42Z with PID 44131 + caffeinate 44418. Slice 26 power assertion live. Slice 25B preflight probed 3 trusted models:
- Qwen-35B: DEGRADED_5XX (transport_error: done_before_content)
- Qwen-397B: DEGRADED_5XX (transport_error: done_before_content)
- Kimi-K2.6: DEGRADED_TIMEOUT (latency=10000ms)

ALL THREE FAILED. Slice 25B `PreflightAllFailedError` fired EXACTLY as designed:
```
[Preflight] FAIL-FAST — every probed model failed; halting initialization.
            report=active=0 demoted_entitlement=0 degraded_5xx=2 degraded_timeout=1
            total=3 duration_s=10.02
[GLS] Slice 25B preflight FAIL-FAST — halting boot. per-model verdicts:
       Qwen-35B=degraded_5xx(status=0), Qwen-397B=degraded_5xx(status=0),
       Kimi=degraded_timeout(status=0)
```

Boot was correctly halted. ZERO dispatch attempts. ZERO cost. Process auto-terminated structurally (manual SIGKILL also tried; process had already exited via the FAILED state transition).

# What v22 proved + what it didn't

**Architecturally proven**:
- ✅ Slice 26 power assertion (caffeinate bound at boot)
- ✅ Slice 25B preflight (10.02s probe + correct fail-fast halt)
- ✅ Slice 25B persistence (3 models probed because Qwen-4B remains demoted from v19)
- ✅ Slice 28 substrate (constants + helpers + AST pins)

**Architecturally untested** (because preflight halted before any dispatch):
- ❌ Slice 28 Phase 2 adaptive primary budget — never fired (no dispatch occurred)
- ❌ Slice 28 Phase 3 discriminator — never fired
- ❌ Slice 27 Phase 3 adaptive Tier 0 — never fired
- ❌ Slice 20B/20C/20D — never fired
- ❌ The DW fleet's actual capability against SWE-Bench-Pro — UNKNOWN

# The honest read

DW endpoint capacity FOR THIS ACCOUNT has DEGRADED between v21 (1/4 ACTIVE) and v22 (0/3 ACTIVE). The fail-fast working perfectly is architectural validation. The capability question (can pure-DW carry SWE-Bench-Pro to RESOLVED?) remains unanswered — and now we have empirical evidence that on some boots, ZERO DW models will respond, making the question unanswerable until upstream recovers.

Related: [[project_slice_27_unified_auth_adaptive_timeout]] (sibling: adaptive Tier 0 + Aegis auth), [[project_slice_25b_preflight_boot_wiring]] (the fail-fast that fired in v22), [[feedback_no_preresult_euphoria]] (v22 = methodology validation, NOT capability win; DW unreachable for this account today).
