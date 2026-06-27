---
title: Project Slice251 Durable Steering
modules: [tests/governance/test_slice251_durable_steering.py, backend/core/ouroboros/governance/user_preference_memory.py, backend/core/ouroboros/governance/steering.py, backend/core/ouroboros/governance/tool_executor.py, tests/battle_test/test_plan_and_memory_cmds.py]
status: merged
source: project_slice251_durable_steering.md
---

**Slice 251 — Durable Agentic Memory & Semantic Intent Classifier. MERGED PR #69495, main `79c907a4`.** RENUMBERED from 250 per operator — **Slice 250 is reserved for the operator's "Sovereign Distillation / Hardware Verification / adaptive quantization" architecture** (their parallel spec work on branches `sovereign/distillation-phase-ab` + `topology/slice-250-unlock-verification`). Builds on S249 (ephemeral live steering); cures multi-agent session-amnesia.

**VERIFY-FIRST (whole-repo memory audit — KEY REFERENCE for future O+V memory work):**
- The "global memory all future agents boot with" ALREADY EXISTS = **`UserPreferenceMemory`** (`user_preference_memory.py`): persists typed memories to `.jarvis/user_preferences/*.md`; `StrategicDirection.format_for_prompt()` injects them into EVERY future generation prompt; already has `record_approval_rejection`/`record_rollback`/`record_critique_failure` auto-extraction. MemoryType enum: USER/FEEDBACK/PROJECT/REFERENCE/FORBIDDEN_PATH/FORBIDDEN_APP/STYLE. `get_default_store(project_root)` singleton; `add(memory_type,name,description,...)`; `find_relevant(...)`; `format_for_prompt(...)`; `list_all()`.
- **ChromaDB is NOT wired to O+V** — voice-biometric/legacy/demo only (lazy_imports, voice_authentication_layer, test_trinity_knowledge_indexer). Active O+V vector store = `SemanticIndex` (fastembed/.npz). So O+V memory = UserPreferenceMemory (prompt) + SemanticIndex (vector) + episodic_core + domain_map/action_outcome/failure_mode/long_horizon memories. DON'T use ChromaDB for O+V.
- Classifier idiom = DETERMINISTIC (`urgency_router` §5 Tier 0 <1ms). No LOCAL/GLOBAL classifier existed.

**Built:**
- `steering.py`: `classify_steering_intent(text)->INTENT_LOCAL|INTENT_GLOBAL` (deterministic regex/phrase: always/never/from now on/for all/standardize/all/every→GLOBAL; ambiguous→LOCAL conservative). `steering_global_propagation_enabled()` gate (`JARVIS_STEERING_GLOBAL_PROPAGATION_ENABLED` default-TRUE). `propagate_directive(op_id,text,store=)` async (classify→if GLOBAL+gate persist via store; NEVER raises).
- `user_preference_memory.py`: `UserPreferenceStore.record_live_steering_directive(op_id, directive)` (mirrors record_approval_rejection → STYLE memory → injected to all future agents).
- `tool_executor.py` (S249 absorption seam): after folding guidance into current_prompt SYNC, `asyncio.create_task(propagate_directive(...))` fire-and-forget (strong-ref set, no GC) — out-of-band, never blocks round loop.
- NOT "Tiny Prime LLM" (latency/cost on hot path; deterministic is right + truly non-blocking). NO ChromaDB, no new graph.

**Tests:** `tests/governance/test_slice251_durable_steering.py` 21 incl. Phase 4 (inject GLOBAL→absorb local→classify GLOBAL→persist→FRESH UserPreferenceStore over same root boots with directive in format_for_prompt = amnesia cured). 35 green w/ S249. Pre-existing unrelated `MemoryType.FORBIDDEN_APP` missing emoji/border in battle_test renderer (`test_plan_and_memory_cmds.py`) — enum-coverage gap, proven via stash, NOT mine (candidate tiny follow-up fix).

**⚠️ GIT-INCIDENT LESSON (important):** the operator was committing rapidly in the SAME working directory during my session → the working-dir HEAD kept moving onto their branches (`sovereign/distillation-phase-ab`, `topology/slice-250-unlock-verification`); my `checkout -b`/`push` aborted/collided repeatedly (a new uncommitted file appeared mid-checkout; my commit got stacked on their spec commits). RESOLUTION: my commit was a safe reachable object (`b79c71bd`, clean 5-file diff); I built the final branch in an **ISOLATED `git worktree`** off the exact clean main sha (`git worktree add -b <branch> $TMPDIR/... <main-sha>` → `git cherry-pick <my-commit>` → push → `git worktree remove`). **LESSON: when the shared working tree is being concurrently mutated, do NOT fight it with checkout/stash in the main dir — use an isolated git worktree (separate files, immune to main-dir churn) to build+push the branch.**
