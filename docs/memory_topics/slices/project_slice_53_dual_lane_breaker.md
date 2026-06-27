---
title: Project Slice 53 Dual Lane Breaker
modules: [backend/core/ouroboros/governance/dual_lane_breaker.py]
status: historical
source: project_slice_53_dual_lane_breaker.md
---

Slice 53 MERGED 2026-06-01 (PR #65639, squash e438ac8073). Implements the gated Option A recommended when deferring Slice 52 Phase 3. main synced.

**Gap closed:** DW-only verified total blackout (empty streams HTTP 200 on BOTH streaming preflight AND batch generation, per [[project_slice_51_disambiguation]]) → every GENERATE op exhausts all DW models + burns retries forever. ProviderExhaustionWatcher doesn't catch it (DW-only exhaustions raise `fallback_skipped:no_fallback_configured`, deliberately NOT counted toward its threshold).

**Design — gated dual-lane isolation (preserves Slice 41 by construction):** a single op only fails to yield a candidate from EITHER lane when BOTH are empty → breaker trips only on verified TOTAL blackout, never single-lane streaming degradation (working batch lane yields candidates → resets counter). NEW `dual_lane_breaker.py` pure thread-safe `DualLaneOutageBreaker` singleton: `record_total_outage(diag)` (returns True once on the tripping call), `record_success()` (resets counter, does NOT un-trip a verified blackout), trips at `JARVIS_TOTAL_OUTAGE_THRESHOLD` consecutive (default 3), master `JARVIS_DUAL_LANE_BREAKER_ENABLED` default-on (=false → records nothing, byte-identical).

**Wiring (verify-first hooks):** candidate_generator `_note_dw_total_outage` at the THREE DW-exhaustion raises in `_dispatch_via_sentinel` (sentinel_dispatch_no_fallback / background_dw_blocked_by_topology / speculative_deferred — the "all DW models failed, no Claude fallback" terminals); `_note_dw_candidate_success` on the sentinel success return (line 2257, `_result is not None`). Both best-effort (lazy import, never raise — they sit on the generation error path). governed_loop_service `submit()` (the documented single dispatch chokepoint) gets a NEW first gate: tripped → refuse op with `reason_code=dual_lane_outage_paused` → pauses allocations → loop idles → existing idle_timeout → clean exit-0. Gate is `is_tripped()`-guarded → normal path byte-identical when untripped.

**Deliberate scope decision:** NOT wired to a forced os._exit. Verified (not assumed) that `UserSignalBus.request_stop()` is a PER-OP cancel (voice/CLI "stop this op"), NOT a session stop — wrong mechanism. So instead of blind-wiring the harness session-shutdown path, the breaker pauses allocations at submit() and lets the existing idle_timeout produce exit-0. Lower risk, achieves the core ask ("pause active task allocations").

11 tests (8 state-machine incl single-lane-preserved + terminal-trip; 3 wiring pins per Slice 45 dead-code lesson). 80 transport/candidate regression green. Also Slice 53 P2 (vendor script standalone flag) + the runbook's full-regression-then-merge folded in.

**This makes O+V graceful under a DW blackout but does NOT unblock SWE-bench-Pro — that still requires DW to serve non-empty content (server-side, their fix; see vendor repro `diagnostics/vendor_doubleword_empty_stream_repro.md`).** The breaker means a blackout now pauses cleanly instead of burning the session. See [[project_slice_52_posture_cache]] [[project_slice_41_batch_aware_fleet]]
