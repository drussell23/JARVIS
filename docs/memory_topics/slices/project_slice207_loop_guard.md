---
title: Project Slice207 Loop Guard
modules: [backend/core/ouroboros/governance/semantic_index.py]
status: historical
source: project_slice207_loop_guard.md
---

**Slice 207 — Class-Level Loop Guard on SemanticIndex.build (MERGED #69448, main `294d3549c9`, 2026-06-10).** The follow-on to S206's HONEST finding that the 25s loop freeze persisted (a non-singleton SemanticIndex calling sync build() on the loop).

**CORRECTED plan's incoherent mechanism:** plan wanted build() to offload via `run_coroutine_threadsafe().result()` — IMPOSSIBLE: a sync method returning bool can't offload-and-wait (.result() blocks the loop thread / deadlock; can't drive the loop you're blocking). COHERENT pivot (valid because semantic index is ADVISORY/eventually-consistent): on sync-on-loop call → redirect to existing thread-offloaded `build_async()` (single-flight daemon thread) + return last-known-good `self._built_at > 0` immediately. Loop never blocks; index refreshes a cycle later (score/boost already tolerate currently-loaded/empty centroid).

**How to apply:** `semantic_index.py` `loop_guard_enabled()` (`JARVIS_LOOP_GUARD_ENABLED` default-FALSE=byte-identical). `build()` does: try `asyncio.get_running_loop()` → success=on-loop → warn + `build_async()` + return built-state; RuntimeError=off-loop → real `_build_impl`. RECURSION-SAFE: build_async's daemon thread has NO running loop → get_running_loop raises → real rebuild runs there (guard inert off-loop) — verified by test. Immunizes ANY caller current/future (class-level, not localized patch). compose enabled. 7 tests; 189 regression. **HONESTLY SCOPED OUT: `strategic_direction._extract_git_themes` (1.5s, 16x smaller) is a @staticmethod w/ no instance cache — clean guard needs instance-cache refactor; naive on-loop return-[] would REGRESS git-momentum feature. Deferred not rushed.** PENDING LIVE VERIFY: does LoopSink show `semantic_index.build kind=sync` GONE after rebuild? (the actual proof the 25s freeze is eliminated). See [[project-slice206-warmup-lifecycle]].
