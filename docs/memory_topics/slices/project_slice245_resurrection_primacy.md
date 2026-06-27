---
title: Project Slice245 Resurrection Primacy
modules: [tests/governance/test_slice245_resurrection_primacy.py, backend/core/ouroboros/governance/op_context.py, backend/core/ouroboros/governance/background_agent_pool.py, backend/core/ouroboros/governance/governed_loop_service.py]
status: merged
source: project_slice245_resurrection_primacy.md
---

**Slice 245 — Absolute-Primacy Re-Ingest Matrix. MERGED PR #69487, main `5af740a3`.** DW-sovereignty arc ([[project_dw_sovereignty_arc_intent]]); the genuine gap surfaced in [[project_slice244_priority_dispatch_audit]] (resurrection got normal source-tier priority, not absolute primacy). Follows 242 prior + 243 stability gate ([[project_slice242_recovery_prior]]).

**Problem:** an op that fails `all_providers_exhausted` during a dark window is terminal today (`background_agent_pool` marks it `"failed"` + drops it at the worker-loop except). On wake it should NOT re-enter behind the work that piled up while dark — it earned Absolute Primacy.

**HONEST SCOPE — two corrections to the brief (pattern the user repeatedly praises):**
1. Did NOT "eradicate pool-resume" — healthy QUEUED ops are preserved across pause + resume in place correctly (eradicating loses them). Only the exhaustion-FAILED survivor needs re-ingest; this ADDS that.
2. Phase 3 "carry exact partial state" = re-submit the EXACT frozen `OperationContext` (phase / `generation.candidates` / plan preserved → no completed work re-computed), NOT a reconstructed envelope (which restarts at CLASSIFY, LOSING state). Live LLM stream cannot cross a stateless boundary + doesn't need to (completed phases live in the context). Same stateless-resume honesty as the byte-offset-healer / mid-DAG fictions previously declined.

**Built:**
- `op_context.py`: `resurrected_from_hibernation: bool = False` field + `with_resurrection()` (dataclasses.replace + hash-chain recompute, same mechanics as `with_expanded_files`).
- `intake/unified_intake_router.py`: `_compute_priority(..., resurrected=False)` — when True, short-circuits to `_resurrection_intake_priority() = min(_PRIORITY_MAP.values()) - _resurrection_primacy_margin()`. DYNAMIC (derived from live tiers), NOT hardcoded 0; beats even voice_human=0.
- `background_agent_pool.py`: `_ROUTE_PRIORITY` module const (immediate1/standard3/complex3/background5/speculative7) + `_resurrection_pool_priority() = min(_ROUTE_PRIORITY.values()) - margin`; `submit()` honors `resurrected_from_hibernation`; `resubmit_resurrected(ctx)` (marks via with_resurrection + reuses canonical `submit`); `drain_exhaustion_failures()` (scans `_ops` for status=="failed" matching `_RESURRECTABLE_ERROR_MARKERS` = all_providers_exhausted/providers_exhausted/deadline_exhausted/live_transport, returns contexts + CLEARS them so a 2nd wake can't double-resurrect).
- `governed_loop_service.py` `_wake_bridge` made ASYNC: after `resume()`, drains + `resubmit_resurrected` each survivor. Gated `JARVIS_RESURRECTION_REINGEST_ENABLED` default-TRUE, fail-soft. Async-compatible via the controller's `_call_maybe_async` (proven by test_hibernation_observability staying green).
- Shared knob `JARVIS_RESURRECTION_PRIMACY_MARGIN` default 100 (both layers).

**Tests:** `tests/governance/test_slice245_resurrection_primacy.py` 10 tests incl. Phase 4 (blackout→3 dark-window standard tasks→recovery→survivor re-ingested→dequeues before all 3). 199 green across hibernation/pool/intake/242-245, zero regression. TDD RED→GREEN. Test-harness gotcha learned: `BackgroundOp.goal` falls back to `ctx.op_id` (OperationContext has `description`, not `goal`) → assert on `op.context.description`.

**Process:** branched off FRESH `origin/main` (243 rebase-conflict lesson) — clean merge.
