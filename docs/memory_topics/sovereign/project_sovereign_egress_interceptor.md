---
title: Sovereign Egress Interceptor Mesh (2026-06-22, MERGED PR #69659) — DW API-citizenship guard
modules: []
status: merged
source: project_sovereign_egress_interceptor.md
---

# Sovereign Egress Interceptor Mesh (2026-06-22, MERGED PR #69659) — DW API-citizenship guard

**Why:** Meryem @ DW (co-founder) reported `reasoning_effort=none` errors on gpt-oss-120b (2026-06-21, pre-fix). Operator mandate: be an impeccable API citizen — make it *structurally impossible* to send DW a malformed/oversized request, validated LOCALLY before any egress. (DW reasoning fix was already live — verified zero reasoning errors / zero 4xx on the C2 node; Meryem's report = pre-fix soaks. DW support: support@doubleword.ai.)

**What shipped** (`dw_egress_interceptor.py`, pure leaf, **default-ON** `JARVIS_DW_EGRESS_INTERCEPTOR_ENABLED`):
- **Schema sanitizer** `sanitize_egress_body(body, model)`: extensible env-driven registry (`JARVIS_DW_EGRESS_SANITIZE_RULES` + built-in `gpt-oss:floor-reasoning`), no hardcoded model if/elif; reasoning rule DELEGATES to the existing `_dw_model_min_effort`/`_clamp_up_to_min` floor (lazy import → cycle-safe; NOT reimplemented).
- **Payload-weight governor** `assert_egress_weight(body, model)`: char weight vs `min(JARVIS_DW_EGRESS_MAX_CHARS default 600k, ModelCard.context_window×chars_per_token)`; over → raise `LocalEgressOverweightError(attempted_size, max_allowed_size, required_compression_ratio, model)` + BLOCK (no `session.post`).
- **Wired at ALL 3 DW generation egress chokepoints** (`doubleword_provider.py`): realtime SSE+non-stream body @~3171, batch JSONL @~1746, AND `complete_sync` heavy lane @~5210 (the 3rd was a review-caught gap). Guard runs only when enabled; fail-soft.
- **Context-aware re-chunk:** `LocalEgressOverweightError` → `FailureSource.LOCAL_EGRESS_OVERWEIGHT` (weight 0.0, mirrors FSM_EXHAUSTED everywhere — never trips the vendor breaker/sentinel/surface-health) → the **LIVE extracted `generate_runner.py`** catch (NOT the dead orchestrator inline block — applied the classify_runner lesson) routes to `decompose_for_block(compression_target=max_allowed)` → AST slices sub-goals ≤ target (irreducible logged, never silently exceeded). Re-chunk gated on `chunking_enabled()`/`JARVIS_RECURSIVE_CHUNKING_ENABLED` (same flag the gcp overlay arms — verified, no dormant-config mismatch).
- **Sovereign Telemetry Boot-Guard:** GLS.start() emits exact `[SOVEREIGN WARNING] API Citizenship Guard Disabled: Egress Interceptor is OFF. Node is vulnerable to overweight payload dispatch.` if disabled (default-ON → fires only on explicit override). Reuses `dw_fault_taxonomy.is_local_egress_overweight` (legit classifier, not a parallel taxonomy).

**Invariants (Opus final review APPROVE / no Critical):** I1 zero-egress (traced — no session.post on overweight); I2 fail-soft ASYMMETRY (sanitize/estimate bug NEVER wrongly blocks; confirmed overweight ALWAYS blocks); I3 weight-0.0 cannot sever DW lane; compression_target genuinely bounds (no C1-discard bug); reuse-first; live-path reachability confirmed (exception survives candidate_generator unwrapped to the runner). 84 egress tests + static zero-egress proof. Built via brainstorm→spec→plan→SDD (5 tasks, T3 keystone on opus). Default-ON per the File-Isolation lesson (a safety boundary defaulting OFF is a vulnerability).

**Status:** MERGED. Static-validation gate (oversized blocked locally, zero egress) SATISFIED. The C2 convergence soak remains **operator-gated** (Option 2: hold until certain egress is safe — now it is). See [[project_sovereign_resilience_chunking]] (the chunking matrices it feeds) + [[project_a1_intake_dispatch]] (A1 dispatch) + [[project_dw_reasoning_capability_profiler]] (the reasoning floor it reuses). **DW citizenship rule: never leave a stuck/non-converging soak running; delete promptly (it's pure DW noise).**
