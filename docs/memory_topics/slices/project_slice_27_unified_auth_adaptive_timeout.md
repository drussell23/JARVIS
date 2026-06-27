---
title: Phase 2 — Aegis-unified auth bridge
modules: []
status: historical
source: project_slice_27_unified_auth_adaptive_timeout.md
---

PR #59083 squash-merged 2026-05-26 at `78620e4adf`. Branch `ouroboros/slice-27-unified-auth-bridge`. Closes v20 (`bt-2026-05-27-011121`) two final upstream coordination roadblocks.

# Phase 2 — Aegis-unified auth bridge

`DoublewordProvider.prompt_only()` previously raised `ValueError` when `self._api_key=""`. Post-Aegis env_scrub, the api_key IS empty by design (Aegis daemon injects the real key server-side). Fix: `if not self._api_key AND not aegis_enabled() → raise`. Affected callers restored: SemanticTriage, IntentDiscovery, Slice 20B json_healer.

# Phase 3 — Adaptive Tier 0 timeout

Formula (operator-attested):
```
timeout = (base + step_bonus × floor(prompt_chars / step_chars)) × (heavy_scalar if heavy_model else 1.0)
timeout = min(timeout, cap)
```

Defaults (env-tunable, no hardcoding):
- base=60s (`JARVIS_ADAPTIVE_TIER0_BASE_S`)
- step_chars=5000 (`JARVIS_ADAPTIVE_TIER0_STEP_CHARS`)
- step_bonus=15s (`JARVIS_ADAPTIVE_TIER0_STEP_BONUS_S`)
- heavy_scalar=1.5 (`JARVIS_ADAPTIVE_TIER0_HEAVY_SCALAR`)
- cap=240s (`JARVIS_ADAPTIVE_TIER0_CAP_S`)
- heavy markers=("397B", "Kimi") CSV via `JARVIS_ADAPTIVE_HEAVY_MODEL_MARKERS`

Example: 10KB SWE prompt on Qwen-397B → (60+30)×1.5 = **135s** (vs 90s in v18c). 50KB on 397B → cap at 240s.

# Backwards compatibility

`_tier0_rt_cap_for_route(route)` without kwargs returns static 90s (Slice 18c byte-identical) — legacy call site at line 5087 unchanged. Adaptive engages only when `model_id` or `prompt_chars` kwargs passed.

# Verification

18 tests (3 AST + 15 spine). 273/273 regression (exceeds operator's 245 target).

# v21 launch

v21 (bt-2026-05-27-025855, PID 29675) launched 2026-05-27T02:58:47Z. Slice 26 caffeinate confirmed, Slice 25B preflight starting (3 models — Qwen-4B persistently demoted from v19).

# v21 expected behavior

- prompt_only callers (SemanticTriage / IntentDiscovery / json_healer) all work post-scrub
- Qwen-397B GENERATE on ~10KB SWE prompt gets 135s budget (vs v20's 90s)
- If 397B can complete in 135s → JSON parse → APPLY → VERIFY → potentially RESOLVED
- If still TIMEOUT at 135s → DW endpoint is genuinely slow, need to either bump cap or accept this is a DW capability limit

Related: [[project_slice_26_power_assertion]] (sibling boot hook), [[project_slice_25b_preflight_boot_wiring]] (auto-activated via Slice 19a), [[feedback_no_preresult_euphoria]] (Slice 27 ships infrastructure; v21 RESOLVED is the capability bar).
