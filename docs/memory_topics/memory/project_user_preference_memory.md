---
title: Project User Preference Memory
modules: [backend/core/ouroboros/governance/user_preference_memory.py, tests/test_ouroboros_governance/test_user_preference_memory.py, orchestrator.py, backend/core/ouroboros/governance/tool_executor.py]
status: historical
source: project_user_preference_memory.md
---

Ouroboros+Venom now has a persistent typed memory store across sessions, modeled on Claude Code auto-memory. Backed by `.jarvis/user_preferences/` (one `.md` per memory + `MEMORY.md` index).

**Why:** O+V needed cross-session learning so the user's explicit preferences, forbidden paths, and rejection reasons survive process restarts. Previously the `_session_lessons` buffer cleared on restart and NegativeConstraintStore stored domain-keyed constraints not human-readable rules. UserPreferenceMemory closes the gap with typed, human-editable files (Manifesto §4 synthetic soul, §7 observability).

**How to apply:** When working on O+V features that interact with user guidance, forbidden paths, or post-rejection learning: reach for `user_preference_memory.get_default_store()` as the persistent layer. Six memory types: USER / FEEDBACK / PROJECT / REFERENCE / FORBIDDEN_PATH / STYLE. Three integration points already wired:

1. **Prompt injection** — `orchestrator.py` CONTEXT_EXPANSION calls `store.format_for_prompt(target_files, description, risk_tier)` and appends the result to `strategic_memory_prompt`. Relevance scoring: path overlap (10) > tag match (4) > type bonus (STYLE=3, USER=2, PROJECT=1); FORBIDDEN_PATH boosted 2× on matching target; freshest-wins tiebreak.

2. **ToolExecutor hard block** — `tool_executor._is_protected_path` consults a module-level `register_protected_path_provider` hook. Every FORBIDDEN_PATH memory becomes a substring match against `edit_file`/`write_file`/`delete_file` paths. Same layer as the hardcoded `.git/`, `.env`, `credentials` list.

3. **Postmortem auto-extraction** — When a human REJECTs an APPROVAL_REQUIRED op, `orchestrator.py` calls `store.record_approval_rejection(...)` which creates a FEEDBACK memory dedupe'd by description slug. Upserts rather than piling up repeat rejections. Runs in parallel to existing NegativeConstraintStore hook.

**Files shipped:**
- `backend/core/ouroboros/governance/user_preference_memory.py` (~820 LOC)
- `tests/test_ouroboros_governance/test_user_preference_memory.py` (81 tests, all pass)
- Integration edits in `tool_executor.py`, `orchestrator.py`, `CLAUDE.md`

Process-wide singleton access via `get_default_store(project_root)`; `reset_default_store()` for tests.
