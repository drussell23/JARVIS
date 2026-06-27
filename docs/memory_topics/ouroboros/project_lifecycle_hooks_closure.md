---
title: Project Lifecycle Hooks Closure
modules: [backend/core/ouroboros/governance/lifecycle_hook.py, backend/core/ouroboros/governance/lifecycle_hook_registry.py, backend/core/ouroboros/governance/lifecycle_hook_executor.py, backend/core/ouroboros/governance/lifecycle_hook_orchestrator_bridge.py, orchestrator.py, backend/core/ouroboros/governance/flag_registry_seed.py]
status: historical
source: project_lifecycle_hooks_closure.md
---

May 2, 2026: Lifecycle Hook Registry 5-slice arc closed same-day. Closes the last remaining ❌ row in the CC parity table — operator-defined Python-callable hooks now fire on the 5 named orchestrator phase boundaries (PRE_GENERATE / PRE_APPLY / POST_APPLY / POST_VERIFY / ON_OPERATOR_ACTION). Substrate is live; operators register hooks via the Priority #6 module-owned-registration contract.

**Five slices shipped:**

1. **Slice 1 — Pure-stdlib decision primitive** (`lifecycle_hook.py`, commit `2dae6a44a2`): 5-value `LifecycleEvent` closed taxonomy + 5-value `HookOutcome` closed taxonomy + frozen `HookContext`/`HookResult`/`AggregateHookDecision` dataclasses + total `compute_hook_decision` BLOCK-wins aggregator + `make_hook_result` convenience constructor with auto-stamped Phase C tightening (BLOCK → "passed"; others → empty) + master flag default-FALSE + env-knob clamping. 73 tests.

2. **Slice 2 — Sync registry** (`lifecycle_hook_registry.py`, commit `4c501b461e`): `LifecycleHookRegistry` thread-safe via RLock + capacity-limited per-event + insertion-sorted by priority for O(N) lookup + `HookRegistration` frozen dataclass + `LifecycleHookCallable` Protocol + 4 typed exception classes (DuplicateHookName / HookCapacityExceeded / InvalidHook / base) + listener pattern + singleton + `discover_module_provided_hooks()` walking `_HOOK_PROVIDER_PACKAGES`. Mirrors `InlinePromptController` operational discipline (NEVER raises on read paths; raises EXPLICITLY at register-time). 50 tests including thread-safety smoke (50 concurrent registers).

3. **Slice 3 — Async executor** (`lifecycle_hook_executor.py`, commit `cb3bfc0288`): `fire_hooks()` async coordinator + `_run_one_hook()` per-hook defensive wrapper + 3 sentinel detail-format prefixes (timeout/raise/bad-return). Wraps sync hooks in `asyncio.to_thread` + `asyncio.wait_for` + parallel via `asyncio.gather(return_exceptions=True)` for fail-isolation. 22 tests including parallel proof (3×300ms hooks complete in <700ms wall-clock) + per-hook timeout doesn't leak.

4. **Slice 4 — Orchestrator bridge + PRE_APPLY wire-up** (`lifecycle_hook_orchestrator_bridge.py`, commit `82b388b093`): 5 typed gate helpers (one per LifecycleEvent) + `LifecycleHookGate` frozen result mirroring `DeployGate.preflight` shape + internal `_gate_event()` dispatcher + `_compute_gate_from_aggregate()` translator + `_FAIL_OPEN_DETAIL_PREFIX` sentinel. Wired into `orchestrator.py` line 6991 immediately after the existing `DeployGate.preflight` block — single ~40-line block; BLOCK routes to `CANCELLED` via established `ctx.advance(CANCELLED, terminal_reason_code=lifecycle_hook_blocked:<names>)` pattern. 23 tests including orchestrator wire-up smoke (parses cleanly + correct integration shape).

5. **Slice 5 — Graduation** (commit `3ccab0f225`): Master flag flipped default false→true. 3 lifecycle hook flags + 8 AST-pin invariants registered via dynamic discovery (158 total flags / 76 total invariants post-Slice-5). All 8 validate clean. 22 graduation tests + `discover_and_register_default()` convenience boot helper.

**Architectural reuse spine — no duplication:**
- Module-owned `register_flags(registry)` + `register_shipped_invariants()` contract from Priority #6 closure: all 4 modules expose them; discovered automatically. No edits to `flag_registry_seed.py` or `meta/shipped_code_invariants.py` required.
- `InlinePromptController` operational discipline mirrored: NEVER-raises-on-read-paths + raises-explicitly-on-register-misconfig + listener pattern + singleton + reset-for-tests.
- `DeployGate.preflight` result shape mirrored: caller does `if not gate.passed: ...` exactly like the existing deploy gate. The two gates live in the same orchestrator block at lines 6973-7050.
- Registration-contract exemption from Priority #6: AST pins on hot-path-purity recognize the boot-time meta nature of register_* functions and exempt their imports.

**Two layers of fail-open by construction:**
1. Slice 3 executor `FAILED-is-non-blocking` aggregator semantics — buggy hooks (timeout / raise / bad-return) cannot stop the orchestrator. Only properly-returning BLOCK outcomes block.
2. Slice 4 bridge `passed=True on bridge crash` semantics — fire_hooks raising despite its NEVER-raise contract still produces `gate.passed=True` with `_FAIL_OPEN_DETAIL_PREFIX` sentinel. Broken hook substrate cannot block autonomous loop.

Both layers AST-pinned by Slice 5 invariants: drift on `return_exceptions=True` or `_FAIL_OPEN_DETAIL_PREFIX` would silently break safety; pins catch it before merge.

**Sweep results:** 216/216 combined sweep across full Lifecycle Hook stack (Slices 1-5) + canonical "all 76 invariants validate clean against main" pin.

**Where O+V stands post Lifecycle Hook closure:** A across the board structurally, ALL ❌ rows in the original CC parity table eliminated:
- ✅ Multi-turn tool loop (Venom)
- ✅ MCP external tools (Gap #7)
- ✅ Interleaved thinking (SBT-Probe Escalation)
- ✅ Hooks (this arc)
- ✅ Diff preview before applying (InlinePromptGate)
- ✅ Inline prompt for confirm (InlinePromptGate)

Remaining ⚠️ rows are all "ecosystem/UI/incremental" rather than "structural primitive missing": Plan replan-on-falsify, Subagent free-form delegation, Skills/slash commands ecosystem, /compact /clear UX, Time-travel debugging UI. None require fundamental architectural work; all have substrate ready and need surface wire-up.

**Why deferred Slice 5b for orchestrator wire-up of remaining 4 events:** Slice 4 wired the LOAD-BEARING site (PRE_APPLY = the operator's veto gate). Bridge helpers for PRE_GENERATE / POST_APPLY / POST_VERIFY / ON_OPERATOR_ACTION exist; orchestrator inserts can land incrementally as operator demand surfaces. Avoids a long-tail PR with 5 simultaneous mods to a 9725-line file.

**How to apply (operator-facing):** Operators register hooks by exposing `register_lifecycle_hooks(registry)` in any module under `_HOOK_PROVIDER_PACKAGES`. The discovery loop walks at boot via `discover_and_register_default()`. Each hook receives a `HookContext` (event + op_id + phase + payload) and returns a `HookResult` (typically via `make_hook_result(name, outcome, detail=...)`). BLOCK outcomes route the op to CANCELLED; WARN outcomes log + proceed; CONTINUE proceeds normally; FAILED/DISABLED outcomes are non-blocking by construction. Master flag `JARVIS_LIFECYCLE_HOOKS_ENABLED=false` for hot-revert.

**Commits:** `2dae6a44a2` (Slice 1) → `4c501b461e` (Slice 2) → `cb3bfc0288` (Slice 3) → `82b388b093` (Slice 4) → `3ccab0f225` (Slice 5).
