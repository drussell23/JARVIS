---
title: Project Worktree Isolation Hardened
modules: [backend/core/ouroboros/governance/autonomy/subagent_scheduler.py, tests/governance/test_worktree_isolation.py, tests/governance/autonomy/test_subagent_executor_worktree.py]
status: historical
source: project_worktree_isolation_hardened.md
---

CC-vs-O+V gap #3 ("Real subagent isolation with worktree + cleanup") closed
2026-04-19. The framing that O+V's L3 worktree was "heavy" was partially
wrong — it was already COW via `git worktree add -b`, no copy or venv
warmup. The real gaps were contract drift on failure and SIGKILL-orphan
accumulation.

**Why:** Manifesto §1 (Boundary) + §6 (Iron Gate) demand deterministic
execution authority. The prior silent `_worktree_path = None` fallback let
parallel units collide in the shared tree while the contract still
promised isolation. §2 (Progressive Awakening) demands clean boot
invariants — the `finally`-block cleanup couldn't survive SIGKILL/OOM.

**How to apply:** Two mechanical guarantees now hold — don't spend time
re-deriving them in future sessions.

1. `GenerationSubagentExecutor.execute` (subagent_scheduler.py): if
   `worktree_manager.create()` raises, returns
   `WorkUnitResult(FAILED, failure_class="infra",
   error="worktree_create_failed:<ExcType>:<msg>")` before
   `generator.generate()` is ever called. Zero silent shared-tree
   fallback path remains.
2. `WorktreeManager.reap_orphans()` sweeps at
   `_build_components` boot under
   `JARVIS_WORKTREE_REAP_ORPHANS=true` (default true). Four sources:
   registered `unit-*` worktrees (via `git worktree list
   --porcelain`), unregistered on-disk `unit-*` dirs under
   `worktree_base`, dangling `unit-*` branches (prevents "branch
   already exists" on next submit), then `git worktree prune`.
   Idempotent on clean boot.

**Proof stack:**
- 17 unit tests green (10 reaper in `test_worktree_isolation.py`, 2
  executor in `test_subagent_executor_worktree.py`, 5/5 existing
  scheduler regression).
- Live smoke `/tmp/claude/wt_smoke.py` against real git binary + real
  repo — reaper A1 reaped 2 paths and deleted the dangling branch;
  hard-fail A2 converted a real `rc=255: branch already exists` into
  `FAILED/infra/worktree_create_failed:RuntimeError:…` with zero
  `generator.calls`.
- Battle test `bt-2026-04-20-033546` (6m 3s, $0.00, 0 ops, idle-timeout)
  confirmed `[GovernedLoop] WorktreeManager wired` + L3
  SubagentScheduler wired, zero WARN/ERROR on boot, clean worktree
  base post-session. L3 create/cleanup roundtrip was not exercised
  (dormant session) — accepted, since the gap closure is structural,
  not behavioral.

**Deliberately NOT shipped** (P3, cosmetic CC-parity optics): zero-delta
fast-close — would add one git subprocess call per cleanup just to log
`delta=empty` vs `delta=dirty`. Not worth the noise; the existing
cleanup is already ref-based and cheap.
