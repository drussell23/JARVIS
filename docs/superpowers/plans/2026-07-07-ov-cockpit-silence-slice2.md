# ov Cockpit Silence + Ceremony at t0 — Slice 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement task-by-task. Tasks are investigation-fix shaped: each names its anchors, binding invariants, and test contract; the implementer performs its own file-level recon at the anchors.

**Goal:** Make the `ov` cockpit genuinely quiet (console = ceremony + ERROR+ only; everything else to the session log) and start the awakening at t0 so the crest animates *while* boot hydrates — plus the functional fixes surfaced by the first live run (DW entitlement fallback, triage auth, lock/branch hygiene, posture-observer loop block).

**Context:** Slice 1 (merged, PR #69892) gated the 3 banner sites + set cockpit root to WARNING. Live run bt-2026-07-08-013911 proved insufficient: WARNING≈chatter in this codebase, a dozen ungated print sites, the Aegis subprocess's own handler, the headless ticker, and the ceremony arriving after ~2.5min of boot. Root-cause map in `.superpowers/sdd/progress.md` (Slice 2 section) + session debug.log.

## Global Constraints (every task)

- **Mandate 1:** silence via handler/level configuration at the root logging infra (`governance/silent_boot.py`) and the Aegis setup sequence, plus PresentationMode conditionals at print emission sources. NO stdout interception, NO print overrides, NO regex filters. ERROR/CRITICAL always reach the console in every mode.
- **Mandate 2:** the conductor becomes an independent asyncio task dispatched at t0 (top of `harness.run()`), rendering reactively off `BootTimer` while boot phases execute; awaited to completion at the banner block (still strictly before `SerpentFlow.start()` output and the REPL's `patch_stdout`).
- **Mandate 3 (DRY):** extend `silent_boot.configure_silent_boot`, `_reap_stale_jarvis_locks` (script:390 area), the existing orphan-reap machinery, `dw_catalog_client`, and `cooperative_fs_io.offload`. No parallel mechanisms.
- **Mandate 4:** fatal boot error while the crest traces ⇒ ceremony aborts (skip → bounded await), Live closes, termios restored, the exception propagates and its stack trace prints to the console (which passes ERROR+). Prove by test.
- SOAK mode byte-identical throughout. Absolute observability preserved: nothing deleted, everything ≥DEBUG lands in the session debug.log.
- Env-var driven; no hardcoded model names (F1 fallback is policy ∩ live catalog).
- Commit per task, named files only, trailer `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`. OCA may contend for the branch: verify `git branch --show-current` == feat/ov-cockpit-silence before and after work; if the tree switches under you, STOP and report.

---

### Task 1: Cockpit console silence — main process

**Anchors:** `backend/core/ouroboros/governance/silent_boot.py` (`configure_silent_boot`); `scripts/ouroboros_battle_test.py` — `[Slice12X.BootExorcism]` print (script top), `[BattleTestDefaults]` (~1586, keep the stderr WARNING variant at 1591 unconditional — it is a warning), `[LedgerHygiene]` (~1617/1626), `_resolve_boot_log_level` (Task-1 Slice-1 helper); `backend/core/ouroboros/battle_test/harness.py` `[DiscordBridge] boot:` print; `backend/core/ouroboros/governance/headless_telemetry.py` ⏳ tty-heartbeat emit (+ its starters in `ui_integration.py`/`native_integration.py` if they arm it for battle-test sessions).

**Do:** (a) extend `configure_silent_boot` with presentation awareness: in COCKPIT the console/stderr handler threshold is `logging.ERROR`; SOAK unchanged (WARNING). The session-file handler keeps DEBUG. (b) `_resolve_boot_log_level`: cockpit root level must NOT rise above what the file handler needs — set root to DEBUG-pass-through with per-handler levels doing the filtering (investigate how silent_boot layers with basicConfig; the invariant is: console shows ERROR+, file gets DEBUG+, no double-emission). (c) gate the named print sites on PresentationMode (ceremony content available via /preflight verb where it fits). (d) headless ⏳ heartbeat: never renders in COCKPIT (it is a headless surface); gate at its emit/arm source.

**Test contract (extend `tests/battle_test/test_presentation_gate.py` + new `tests/governance/test_silent_boot_cockpit.py`):** cockpit console handler level == ERROR while file handler == DEBUG; injected `logger.warning` reaches the file handler but NOT the console handler; injected `logger.error` reaches BOTH; SOAK thresholds unchanged (golden); each gated print site skips in COCKPIT + emits in SOAK (spy pattern from Slice 1); heartbeat emit no-ops under COCKPIT.

### Task 2: Aegis subprocess silence

**Anchors:** `backend/core/ouroboros/aegis/preflight.py` (env scrub + spawn + `[Aegis] daemon ready` print), `backend/core/ouroboros/aegis/daemon.py:669` (`logging.basicConfig`) + `AegisPassthrough`/`AegisForward`/env-load INFO logs.

**Do:** presentation mode must survive the preflight env scrub (allowlist `JARVIS_OV_PRESENTATION` or carry it in the bootstrap payload — investigate which is structurally cleaner; the daemon reads it at ITS setup). In COCKPIT: daemon console handler ERROR+; its INFO/access logs go to a daemon log file under the session dir (or stay stdout in SOAK — byte-identical). Gate the `[Aegis] daemon ready` parent-side print. Credential env-load lines (INFO) follow the same policy automatically.

**Test contract (`tests/aegis/test_daemon_presentation.py` or alongside existing aegis tests):** scrub preserves the mode var; daemon logging setup resolves ERROR console under cockpit env + legacy under soak; parent print gated. (Do not spawn a real daemon in tests — test the setup functions.)

### Task 3: Ceremony at t0 + fatal-abort (Mandates 2+4)

**Anchors:** `backend/core/ouroboros/battle_test/harness.py` — top of `run()` (immediately after the D1 silent-boot block ~925-960), the current awakening block (~1351-1375), `SerpentFlow` construction (~2318, inside a boot phase), fatal/except paths of `run()`.

**Do:** (a) construct the conductor at t0: right after silent-boot config in `run()`, in COCKPIT build a themed console via `theme.build_console()` (SerpentFlow doesn't exist yet — the conductor only needs a console; SerpentFlow's own console prints AFTER the ceremony completes, preserving serialization), attach `get_default_timer()`, wire on_ignition/context_provider exactly as `build_awakening_for_cockpit` does today (move/adapt that builder — keep it the single construction point), and `asyncio.create_task(conductor.run())`. (b) at the banner block: COCKPIT awaits the task (replacing the inline `await ...run()`); SOAK path untouched. (c) fatal-abort: wrap the boot-phase region so any exception first calls `conductor.request_skip()` and awaits the ceremony task with a short bound (e.g. 3s) before re-raising — Live closes, terminal restored, the traceback prints normally (console passes ERROR+). NEVER swallow the boot exception. (d) the REPL `typed_prefix` handoff keeps working (conductor reference retained).

**Test contract (extend `tests/battle_test/test_awakening_hook.py` + `tests/ui/test_awakening.py` if needed):** conductor task starts before the first `_BootPhase` completes (observable via injected timer/fake phases in a unit-level harness driver, or by asserting the construction helper is invoked at t0 with a running-task handle); ceremony still fully awaited before REPL construction; fatal-abort test — start conductor with a hanging wake model, raise a synthetic boot error, assert: skip requested, ceremony task completes within the bound, the original exception propagates unchanged.

### Task 4: F1 DW entitlement fallback + F2 triage auth

**Anchors:** `backend/core/ouroboros/governance/dw_catalog_client.py` (the /v1/models catalog), `doubleword_provider.py` (403 'has not been configured' sites — file upload + dispatch), `dw_surface_probes.py` (slice39 model choice), `governance/brain_selection_policy.yaml` (policy context), `semantic_triage.py` (/v1/models 401 probe — find why credential injection misses it; Aegis passthrough of the same endpoint returns 200).

**Do:** (a) on a 403 entitlement error, resolve a fallback = highest-preference model from `brain_selection_policy.yaml` that IS present in the live catalog (`dw_catalog_client`), cache the entitled set per session, retry once with the fallback, emit structured telemetry (`[DWEntitlement] model=X blocked -> fallback=Y`); no hardcoded model names anywhere — pure policy ∩ catalog. If the intersection is empty, degrade exactly as today (TERMINAL_OPEN routing). (b) fix SemanticTriage's /v1/models probe to use the same credentialed client path the rest of the DW stack uses (root cause: whichever header/base-url divergence produces 401 while Aegis's own GET returns 200).

**Test contract (`tests/governance/test_dw_entitlement_fallback.py`):** 403-entitlement error + fake catalog ⇒ fallback chosen by policy order, retry issued, telemetry emitted; empty intersection ⇒ legacy degrade; no retry storm (single fallback attempt per op). Triage probe: unit test that its request carries the credential header / routes via the same injected client as `DoublewordProvider`.

### Task 5: F3+F4 hygiene — stale JSONL locks + dangling auto/* branches

**Anchors:** `scripts/ouroboros_battle_test.py` `_reap_stale_jarvis_locks` (~390) + `_cleanup_stale_router_lock` (~326); the runtime warner `CrossProcessJSONL stale_lock_detected` (find its module) for the lock-file naming convention; `ledger_sovereignty` worktree creation error path + `worktree_manager.reap_orphans` (unit-* sweep) for the auto/* analog.

**Do:** (a) extend the existing boot-time lock reaper to sweep `.jarvis/*.jsonl.lock` files whose owning PID is dead OR age > the CrossProcessJSONL threshold (reuse its threshold env, do not invent a new one). (b) extend orphan reaping to dangling `ouroboros/auto/<session>-*` branches AND their `.worktrees/ouroboros__auto__*` dirs whose session is not the live one and whose owning process is dead — same dead-session detection the zombie reaper already uses. Never touch the CURRENT session's branch or any `unit-*` (existing behavior preserved).

**Test contract (`tests/battle_test/test_hygiene_reapers.py`):** stale coherence-style lock (dead PID / old mtime) reaped, fresh lock kept; dangling auto branch+worktree from a dead session reaped, live session's kept; unit-* untouched.

### Task 6: F8 posture observer off-loop

**Anchors:** `backend/core/ouroboros/governance/posture_observer.py::run_one_cycle` (~1225) — find the synchronous call(s) that produced `LoopSink blocked_ms=5341` (likely git-history / file reads inside the cycle); `cooperative_fs_io.offload` (the A1-campaign substrate — read its call pattern from an existing consumer).

**Do:** offload the blocking work via `cooperative_fs_io.offload` exactly like existing consumers; cycle semantics unchanged.

**Test contract (extend posture observer tests):** the offloaded call goes through the substrate (spy/injection), cycle result unchanged; if a LoopSink-style timing assertion exists in the repo's test idiom, add the cheap variant.

---

**Out of scope (recorded):** F5 HMAC checkpoint rejects (self-healing by design); F6 croniter (installed in the operator env 2026-07-07); F7 gh 401 for JARVIS-Reactor (operator-side `gh auth` scope); boot *duration* optimization (~2.5min of real hydration — separate slice if desired).
