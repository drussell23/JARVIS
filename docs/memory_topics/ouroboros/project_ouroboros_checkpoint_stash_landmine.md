---
title: Project Ouroboros Checkpoint Stash Landmine
modules: []
status: historical
source: project_ouroboros_checkpoint_stash_landmine.md
---

**Symptom (recurring):** `git commit` fails with `error: Committing
is not possible because you have unmerged files` / `UU
requirements.txt`, conflict markers labelled `Updated upstream` /
`Stashed changes` (a *stash-pop* conflict signature, not merge/rebase).

**Root cause:** The Ouroboros battle-test engine, during soaks,
(1) appends `# [Ouroboros] Modified by...` bookkeeping comments to
`requirements.txt` in the **operator's working tree**, and (2) creates
`git stash` checkpoints named `ouroboros-checkpoint:<op>:pre-apply`
before each APPLY. When a soak is **interrupted** (Phase D watchdog
`wall_clock_cap` → `os._exit(75)`, SIGKILL, OOM) these checkpoint
stashes are left **dangling**. A later `git stash pop`/rebase collides
on the stale requirements.txt comment divergence → `UU` → silently
blocks ALL commits until resolved.

**Correct resolution (NOT a workaround):** the conflicting content is
provably 100% non-functional `#`-comment cruft; `HEAD:requirements.txt`
is the clean authoritative superset. Verify real specs identical
(`grep -vE '^\s*#|^\s*$' | sort -u` both sides → equal), then
`git checkout HEAD -- requirements.txt && git add requirements.txt`.

**Clearing dangling stashes:** match-and-drop only
`ouroboros-checkpoint:<op>:pre-apply` (highest index first or
self-correcting loop); NEVER blanket `git stash clear` (a manual
stash like "stale ouroboros requirements.txt comments (unblock
battle test LiveWorkSensor)" must be preserved).

**Architectural gap + ratified fix:** the harness already reaps
zombie procs (`JARVIS_BATTLE_REAP_ZOMBIES`) and orphan `unit-*`
worktrees (`WorktreeManager.reap_orphans`, §2 Progressive
Awakening) but NOT dangling checkpoint stashes — the missing 3rd
leftover-class reaper. Operator ratified 2026-05-17 a boot-time
`ouroboros-checkpoint:*` stash reaper composing the existing
reap-on-boot pattern (own arc/PR, after the SWE-bench cognition fix
merges). See task tracker + [[project-iron-gate-commit-and-gh-workflow]].
