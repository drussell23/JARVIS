---
title: Project Loop Shadow Mode Vs Evidence Rail
modules: [backend/core/cybernetic_reanimation.py, unified_supervisor.py, orchestrator.py, backend/core/ouroboros/governance/governed_loop_service.py, backend/core/ouroboros/governance/ide_observability_stream.py, backend/core/ouroboros/battle_test/serpent_flow.py]
status: historical
source: project_loop_shadow_mode_vs_evidence_rail.md
---

**Naming collision, zero functional overlap** (recon 2026-06-14). The autonomous Ouroboros loop independently shipped a RESILIENCE feature also called "Shadow Mode" (Slices 252/253 on main, PRs #69503/#69505/#69508). It is unrelated to the [[project_sovereign_evidence_rail]].
- **Loop "Shadow Mode"** = `JARVIS_RESILIENCE_SHADOW_MODE`: traps dangerous resilience ACTIONS (process kill / load-shed / restart) from `cybernetic_reanimation.py` + `unified_supervisor.py` self-healing organs, logs what it WOULD have done. Slice 252 = emit `EVENT_TYPE_SHADOW_ACTION_TRAPPED` SSE (ephemeral, no persistence). Slice 253 = `/endorse <action_id>` HITL one-shot to run ONE trapped kill once (`PendingShadowActionRegistry`); promotes nothing, NEVER reads/writes `_AUTHORITATIVE` flags.
- **Our "Shadow Rail"** = `JARVIS_{PLAN,REVIEW}_SUBAGENT_SHADOW`: records subagent verdict-vs-legacy to durable SQLite, auto-graduates after 50-soak.
- **Verified non-overlap:** the loop NEVER touches `orchestrator.py` `_run_{plan,review}_shadow` hooks nor `governed_loop_service.py` — those are ours, no conflict. Only collisions are MECHANICAL adjacent-line `EVENT_TYPE_*` additions in `ide_observability_stream.py` + `serpent_flow.py` → resolve KEEP-BOTH on rebase. Documented in spec §14.

**OPS LESSON — agent-worktree drift recurred mid-session.** During subagent-driven execution, two implementer subagents (Tasks for the SQLite store) committed into a stray `.claude/worktrees/agent-<hash>/` worktree branch `worktree-agent-*` (based on newer main w/ loop slices), NOT our `sovereign/distillation-phase-ab` checkout. The store files never reached our branch. This is the [[project_autonomous_loop_implements_committed_specs]] race in a new form (agent worktrees, not just branch-flipping).
- **Detect:** subagent reports a commit but `git log` on our branch doesn't show it; `git worktree list` shows an `agent-*` worktree; `git branch --contains <sha>` names a worktree branch.
- **Rescue (do NOT merge the divergent branch — it deletes our work + pulls loop slices):** `git checkout <worktree-branch> -- <specific files>` to pull ONLY the wanted files onto our branch, re-run tests, commit on our branch. Then `git worktree remove .claude/worktrees/agent-* --force` + `git branch -D worktree-agent-*` (the `.git/config Operation not permitted` sandbox warning is harmless — removal still succeeds).
- **Prevent:** give EVERY implementer subagent an explicit branch-guard: BEFORE committing, verify `git rev-parse --show-toplevel`==main checkout path AND `git branch --show-current`==target branch; if not, STOP/BLOCKED without committing. This worked — all subsequent subagents committed to the right branch.
