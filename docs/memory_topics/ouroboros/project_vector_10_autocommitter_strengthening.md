---
title: Project Vector 10 Autocommitter Strengthening
modules: []
status: historical
source: project_vector_10_autocommitter_strengthening.md
---

May 9 2026: §35 row 🟡 #7 + §3.6.2 vector #10 row + §3.6.3 priority #6 + line-819 prose all synced. Closure was already real but PRD doc state was stale.

**Audit findings**:
- PRD v2.26 banner (line 56) + line 237 changelog + line 5435 + line 5467 ALL stated Vector #10 closed 2026-05-05 (Wave 3 hygiene Item 5)
- Substrate verified intact: `cross_process_jsonl.async_flock_critical_section` (~80 LOC) + `AutoCommitter._commit_critical_section` TOCTOU body + `_intent_lock_path()` helper + `<repo>/.jarvis/auto_commit_locks/<token[:32]>.lock` convention
- 5 existing Item 5 regression tests all green
- BUT 4 PRD rows still said "🔴 Not started" — pure doc drift

**3 strengthening tests added** to close missing depth axes:

1. **`test_async_flock_serializes_across_processes`** — true cross-process race coverage. Existing tests were within-process async only; the WHOLE POINT of `flock` (vs `asyncio.Lock`) is OS-level cross-process serialization. New test:
   - Spawn child via `multiprocessing.get_context("fork").Process`
   - Child holds `flock_critical_section(target)` for 1.2s
   - Parent waits 0.2s for child to acquire, then attempts `async_flock_critical_section(target, timeout_s=0.3)` → MUST fail
   - After child release, parent retries with longer timeout → MUST succeed
   - Proves both OS-level lock semantics AND release-on-exit

2. **`test_auto_committer_returns_commit_lock_contended_on_timeout`** — exercises the `commit_lock_contended` skipped_reason code path:
   - Monkey-patches `cross_process_jsonl.async_flock_critical_section` to yield `False` (simulated contention beyond timeout)
   - Calls `AutoCommitter.commit()` with realistic args
   - Asserts `CommitResult(committed=False, skipped_reason="commit_lock_contended", intent_token=<populated>)`
   - Proves contention-handling code is wired AND `intent_token` audit field is populated for operator correlation with sibling process

3. **`test_auto_committer_substrate_unavailable_falls_through`** — exercises the substrate-unavailable fallback decision:
   - Sets `sys.modules["backend.core.ouroboros.governance.cross_process_jsonl"] = None` to simulate fcntl-unavailable platforms (Windows)
   - Stubs `_commit_critical_section` to record invocation
   - Asserts the legacy critical section was invoked (proves "better to commit with residual TOCTOU than fail closed" semantics)

**Test results**: 8/8 total Vector #10 tests (5 existing Item 5 + 3 new strengthening) + **1084/1084 cumulative** across §38.11 (A-F) + §39 (Tier-1+2+3+4+5+7) + Wave 3 hygiene + scheduler + canonical sources.

**4 PRD rows synced** ✅:
- §35 row 🟡 #7 (line 469): "🔴 Not started" → "✅ SHIPPED 2026-05-05 + STRENGTHENED 2026-05-09"
- §3.6.2 vector #10 row (line 717): "🟡 Empirically observed → 🟡 Open" → "✅ CLOSED + strengthened"
- §3.6.3 priority #6 (line 736): "🔴 Not started" → "✅ Shipped + strengthened"
- Line 819 prose: removed "AutoCommitter race" from "two empirical landmines I'd patch first" with strikethrough + closure note

**Architectural discipline**: re-used canonical Python stdlib (`multiprocessing` + `asyncio` + `sys.modules`) — zero new deps; composes existing `async_flock_critical_section` + `AutoCommitter` substrate — zero parallel locking machinery; pins existing TOCTOU contract — zero refactor (depth-test additions only).

**§35 status update**: Wave 3 hygiene arc was actually fully closed 2026-05-05 per v2.27 banner; this version (v2.79) just syncs the doc rows that drifted + adds depth tests.

**NEXT** (autonomy arc remaining):
- Vector #5 cross-session coherence harness (~1-2 wks empirical validation arc)
- M10 ArchitectureProposer (~7-10d substrate move closing weak-form ontogeny gap)
