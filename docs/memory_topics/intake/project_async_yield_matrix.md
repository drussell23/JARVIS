---
title: Sovereign Asynchronous Yield Matrix (2026-06-21, MERGED to main — PR #69638 Layer 1 + #69639 Layer 2; main 06e587fc5c)
modules: [docs/superpowers/specs/2026-06-21-sovereign-async-yield-matrix-design.md, docs/superpowers/plans/2026-06-21-sovereign-async-yield-matrix-plan.md]
status: historical
source: project_async_yield_matrix.md
---

# Sovereign Asynchronous Yield Matrix (2026-06-21, MERGED to main — PR #69638 Layer 1 + #69639 Layer 2; main 06e587fc5c)

**Trigger:** The autonomous O+V loop wrote generative file mutations directly into the **primary host checkout** (`candidate_generator.py`, 13:36) — a split-brain collision with live operator work — because `JARVIS_FILE_ISOLATION_ENABLED` is default-OFF and nothing turned it on. (Diagnosis confirmed the loop independently re-implemented my deadlock-guard fix in em-dash style on a stale base; that redundant work was stashed→dropped; the deadlock fix was already on main via PR #69637.) **Root cause = a static env default is a safety vulnerability.** Fix = make the EXISTING Sovereign Execution Boundary (PRs #69533/#69534) self-enforcing + add graceful operator-yield. Reuse-first: the concurrency matrix already ~90% existed; built the activation + the missing raw-write guard + the yield, NOT a parallel system.

**Spec/plan:** `docs/superpowers/specs/2026-06-21-sovereign-async-yield-matrix-design.md` + `docs/superpowers/plans/2026-06-21-sovereign-async-yield-matrix-plan.md`. Built via subagent-driven-development, 10 tasks (YM-T1..T10), 2 PRs.

## Layer 1 — Deterministic Hard Lock (PR #69638, default-ON)
- `execution_context._is_cloud_container()` — env markers (`OUROBOROS_CLOUD_NODE`/`KUBERNETES_SERVICE_HOST`) + `/.dockerenv`. Reuses existing `is_primary_checkout()` (git-dir vs git-common-dir, EXISTED) + `is_autonomous()` (HMAC presence).
- **Deterministic lock (LR-A)** in `autonomous_workspace.resolve_loop_project_root()`: when primary-checkout + autonomous + non-container + lock-enabled → `_arm_boundary_flags()` force-arms **BOTH** `JARVIS_FILE_ISOLATION_ENABLED` + `JARVIS_EXECUTION_BOUNDARY_ENABLED` (as a pair) and routes to a worktree EVEN when flags were explicitly false. Byte-identical on non-forced paths. Gate `JARVIS_DETERMINISTIC_ISOLATION_LOCK_ENABLED` default-true.
- **Raw-write guard** (`tool_executor` ToolUsePolicy Rule 0e, mirrors the read-only Rule 0d): denies autonomous `edit_file`/`write_file` to a primary checkout (`POLICY_DENIED reason=tool.denied.primary_checkout_raw_write`). **This is the actual incident-vector fix** — previously only commits were denied (operator_commit_authority Stage A), not raw file writes.
- **verify_file_isolation.py I5** + `--prove-override`: proves an explicit `JARVIS_FILE_ISOLATION_ENABLED=false` is overridden by the lock and the primary stays pristine (G4). 19 Layer-1 tests.

## Layer 2 — Graceful Operator-Yield (PR #69639, default-OFF, activated)
- **`mutation_critical_section.py` (LR-B corruption guard):** `mutation_section(op_id)` async re-entrant CM wrapping the 3 real apply/commit sites (multi-file apply, `change_engine.execute`, AutoCommitter commit, via `maybe_mutation_section` gated no-op); `drain(op_id, timeout)` → True=safe-to-park / False=wedged-abandon. **Never park a half-written file/commit.**
- **`operator_presence.py`:** DETERMINISTIC presence (last-input ts `JARVIS_OPERATOR_IDLE_S=45` + liveness probe — NOT CAI/probabilistic; CAI is text-intent-only, wrong layer for a gate per Watchdog Isolation Invariant). `OperatorPresenceWatcher` publishes edge-triggered `operator.active`/`operator.idle` on `TrinityEventBus` (uses `get_event_bus_if_exists` + `persist=False`).
- **SensorGovernor `operator_active_fn`:** hard-zeros new-op admission when operator active (distinct from soft 0.2x emergency brake).
- **`operator_yield_bridge.py`:** `operator.active` → suspend flag → cooperative park at next checkpoint (the park is COOPERATIVE — `should_park_for_route(operator_suspended=)` consulted at `generate_park_wrapper`; NO preemptive mid-op parking) → drain-gated (abandon if wedged) → worker freed; `operator.idle` → resume parked ops via existing `submit_for_resume`.
- **YM-T10 production activation (Daemon Injection):** GLS boot spawns the watcher as a **non-blocking daemon** (try/except — can never crash the loop) + calls `attach()`; default `SensorGovernor` DI'd with `operator_active_fn=operator_present`; SerpentREPL `_loop` stamps `note_human_input()` at the input boundary. ~180 Layer-2 tests.

**LR-B locked:** drain before park, bounded `JARVIS_OPERATOR_YIELD_DRAIN_MAX_S=30s`; wedged → abandon yield, op runs to terminal. Gates: `JARVIS_OPERATOR_YIELD_ENABLED` default-FALSE (advisory, graduate after soak).

## Process lessons (cross-cutting reviews EARN their cost)
- The **wired-vs-dormant bug class** recurred: Layer 2's per-task tests all passed but the final cross-cutting coherence review found 4 unwired production-activation seams (watcher never spawned, attach never called, note_human_input never called, governor never DI'd) → Layer 2 would have merged as dead code. YM-T10 fixed it. **Per-task green ≠ production-wired; always run an end-to-end coherence review before merge** (same lesson as the Epistemic Matrix's deadlock-swallow + GLS-reconcile-dead bugs).
- **Worktree quirk:** `.claude/worktrees/discoroute` had an Edit-flush anomaly mid-session (Edit reported success but writes didn't always land) — subagents worked around via on-disk python/sed + `git show` verification. Trust git over Read for these files.
- **Diagnose before building:** the operator wanted a brand-new "Concurrency Matrix" + a mutex; the audit showed the Sovereign Execution Boundary already existed (just default-off) and that **isolation subsumes a mutex** (removes the shared tree entirely). Avoided duplicating a merged subsystem.
- Merge path: main is PR-protected (local pre-push hook + remote) → direct push refused; merged via `gh pr create` + `gh pr merge --merge` (gh needs `dangerouslyDisableSandbox` for the api.github.com TLS path). Local main FF'd from main checkout (not the worktree — main is checked out there).

**NOT validated:** the actual operator-yield behavior (typing parks the loop, idle resumes) needs an operator-run soak with `JARVIS_OPERATOR_YIELD_ENABLED=true`. Unit/integration tests prove the machinery + OFF byte-identical, not the live yield. One follow-up refinement: the parked op's out-of-pool continuation still progresses (worker IS freed — the goal); fully holding it dormant-until-idle is a future enhancement. See [[project_epistemic_context_matrix]] (the split-brain originated during that arc's build), [[project_sovereign_execution_boundary]] (the boundary this activates).
