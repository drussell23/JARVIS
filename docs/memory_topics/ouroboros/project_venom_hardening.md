---
title: Project Venom Hardening
modules: [backend/core/ouroboros/governance/tool_executor.py, tests/test_ouroboros_governance/test_venom_edit_tools.py, backend/core/ouroboros/governance/live_work_sensor.py, orchestrator.py, tests/test_ouroboros_governance/test_live_work_sensor.py]
status: historical
source: project_venom_hardening.md
---

**Venom tool hardening (Task #193, shipped)** — `backend/core/ouroboros/governance/tool_executor.py`

First-class `edit_file` / `write_file` / `delete_file` tools with 7-layer safety chain per call:
path safety → protected-path → existence → must-have-read → uniqueness (edit) → Iron Gate ASCII → dependency-integrity → Python AST → post-write sha256 verify with auto-rollback. Must-have-read invariant: `ToolExecutor._files_read` tracks every `read_file` call and mutations to unread paths are rejected. Protected paths enforced at both the policy layer AND handler (defense in depth). Env-extensible via `JARVIS_VENOM_PROTECTED_PATHS`. 50 tests in `tests/test_ouroboros_governance/test_venom_edit_tools.py`.

**LiveWorkSensor (shipped follow-up)** — `backend/core/ouroboros/governance/live_work_sensor.py`

Pre-APPLY gate in orchestrator.py (right after stale-exploration guard, ~line 2988). Three signals: `git status --porcelain` (cached 2s), recent mtime within `JARVIS_LIVE_WORK_ACTIVE_WINDOW_S` (180s default), IDE lock files (vim .swp/.swo/.swn, emacs .#file, backup ~). On any hit, Green/Yellow ops abort with reason `human_active_on_target`; Orange (APPROVAL_REQUIRED) proceeds since the human already approved. 19 tests in `test_live_work_sensor.py`.

**Why:** bt-2026-04-10-184157 regressions showed the model could bypass Iron Gate by avoiding the post-GENERATE path (via tool-based patches), and that autonomous edits could stomp on live human work. These two ships close both gaps — Venom mutations now go through the same safety fabric as schema-emitted candidates, and APPLY can no longer blow past a file the human is touching.

**How to apply:** When future work needs to mutate files from within Venom, trust the handlers — they enforce the invariants. When touching the APPLY phase of the orchestrator, keep the LiveWorkSensor check intact; if you need to bypass it for a specific code path, pass an explicit reason via ledger metadata rather than removing the guard.
