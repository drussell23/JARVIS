---
title: Project Slice 52 Posture Cache
modules: []
status: historical
source: project_slice_52_posture_cache.md
---

Slice 52 Phases 1+2 MERGED 2026-06-01 (PR #65638, squash 681b266f5a). Phase 3 deferred on a design conflict. main synced.

**Phase 1 — DW vendor repro artifact** (`diagnostics/vendor_doubleword_empty_stream_repro.md`, committed). Canonical framework-free proof DW returns empty token streams under HTTP 200: vanilla aiohttp → api.doubleword.ai, 18/8 SSE chunks, content_chars=0, finish_reason=length on Qwen3.5-35B/397B. Rules out our client/Aegis/headers/transport → server-side serving fault. Includes minimal repro script + raw outputs for DW engineering. This is the artifact that unblocks the real bottleneck (DW must serve non-empty content).

**Phase 2 — reactive posture commit-ratio cache** (`posture_observer.py`). v46 forensics: `commit_ratios` re-ran `git log` over last N=100 commits EVERY 300s cycle (LoopSink up to 9.8s, ~10x next callsite — dominant recurring cost behind the 20s starvation). `SignalCollector.commit_ratios_async` now caches ratios keyed by (HEAD hash, window); new `_git_head_async()` = bounded `git rev-parse HEAD` gate → skips the 100-commit log when HEAD unchanged (common case at 300s cadence); recomputes on HEAD advance; NEVER caches when HEAD unresolvable (no stale pin). Ratio math extracted to `_compute_commit_ratios` (async path only; legacy sync `commit_ratios()` untouched = min-diff). Real-repo smoke cold 56ms→cached 10.8ms. **Honest scope: zeroes the REPEATED traversal not the single cold scan, REDUCES (doesn't alone eliminate) the multi-contributor loop lag** — v46 20s spike also had GIL contention, pools already minimal (1+2 workers), RSS 636MB (never core/mem oversubscription, per [[project_slice_51_disambiguation]]). 4 new tests, 177 posture-adjacent green; 14 pre-existing test-stub failures (build_bundle_async missing / awaitable TypeError) verified on clean main — unrelated drift, candidate cleanup.

**Phase 3 — empty-stream allocation-pause breaker: DEFERRED (operator decision).** Detection ALREADY exists: done_before_content → UPSTREAM_DEGRADED in dw_surface_health.json w/ consecutive_failures (saw =12 in v45/v46). The runbook's real ask = escalate to PAUSE allocations instead of exhausting retries. **This DIRECTLY CONFLICTS with Slice 41's deliberate ACTIVE_BATCH_ONLY design** (intentionally keeps models eligible during done_before_content so the loop doesn't halt). Reversing it is a real policy change needing operator sign-off — especially since v46 showed BOTH lanes empty (pause justified now, but the architectural reversal is deliberate). NOT blindly implemented. SurfaceVerdict enum: HEALTHY/TRANSPORT_DEGRADED/UPSTREAM_DEGRADED/AUTH_FAILED/ERROR_OTHER (no separate provider_degraded — UPSTREAM_DEGRADED IS the empty-stream state). ProviderExhaustionWatcher threshold=3 already exists.

**Pattern note:** Phases 1+2 shipped clean; Phase 3 surfaced as a decision rather than a blind build — same verify-first discipline that's now corrected runbook premises across Slices 42/45/47/50/51. See [[project_slice_51_disambiguation]] [[project_slice_41_batch_aware_fleet]]
