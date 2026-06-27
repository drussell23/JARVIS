---
title: Project A1 Completion Writeback
modules: [tests/governance/test_subgoal_completion_writeback.py]
status: historical
source: project_a1_completion_writeback.md
---

**§51.11.34-ROADMAP A1 — why GOAL-001::file-00 was a frozen hostage (`emitted=1 done=0` forever).** Committed `c9c554bffd` on branch `ouroboros/battle-test/20260607-072411`.

**The trace (verify-first overturned every earlier hypothesis).** file-00 was NOT a dispatch problem: the emit returns `emitted=True, idempotency_key='enqueued'` (S217 router wire works), and the soak showed 440 dispatches over 2h. The real blocker is **non-economic** — the cost/noise stack (Slices 218–224) was elite but orthogonal.

**ROOT CAUSE — severed completion feedback loop.** `multi_step_orchestrator` computes `done_count` by reading a SEPARATE completion ledger (`.jarvis/goal_decomposition_ledger.jsonl`, via `goal_decomposition_planner.mark_sub_goal_status`), counting rows with status `completed`. The ONLY writers were `_mark_emitted_via_goal_decomposition` (writes `PROPOSED` at EMIT) and the planner's decomposition path (also PROPOSED). **NOTHING ever wrote `COMPLETED`/`FAILED` back when a dispatched op reached terminal.** So `done_count` was structurally pinned at 0 — a sub-goal could dispatch and succeed any number of times and the roadmap would never advance. Grep proof: zero non-test callers of `mark_sub_goal_status` with `CompletionStatus.COMPLETED`.

**THE FIX (no workaround, leverages existing architecture).** Added `_slice_a1_subgoal_completion_writeback(ctx, state)` in `orchestrator.py`, called from the terminal hook `_slice12q_record_terminal` — the SAME fail-soft, recorder-independent seam the Slice-134 episodic synapse fires from. Reads roadmap sub-goal provenance from `ctx.intake_evidence_json` (`sub_goal_id`+`parent_goal_id`, stamped by the multi_step emit path lines 642-677), maps terminal state (`applied`→COMPLETED, any other→FAILED), and appends via the existing `mark_sub_goal_status`. Production path verified byte-equal: planner `ledger_path()` == multi_step `completion_ledger_path()` == `.jarvis/goal_decomposition_ledger.jsonl`. Also added module-level `import json` to orchestrator.py (was only imported locally inside one function at line ~10640 — my helper's `json.loads` was silently NameError-ing through the fail-soft except until I added it).

**Gate:** `JARVIS_SUBGOAL_COMPLETION_WRITEBACK_ENABLED` default-TRUE (closes a structural gap; OFF = byte-identical legacy severed loop). Pinned explicit in `docker-compose.dw-cortex-soak.yml`.

**Verified:** 5 new TDD tests (`tests/governance/test_subgoal_completion_writeback.py`, RED→GREEN) + 286 regression green (episodic synapse, multi_step, goal_decomposition, orchestrator wiring, roadmap integration) + e2e loop-closure (terminal applied → COMPLETED row → `multi_step._load_completion_status` reads `completed`).

**Live validation:** rebuild+relaunch keeps the existing ledgers (fresh container = empty in-memory dedup → file-00 re-emits → its op runs → writeback fires). Watch `done_count` move off 0. NOTE: live outcome may be `done` OR `failed` depending on whether file-00's patch passes the gates — either way the frozen `done=0 failed=0` hostage state finally moves, which is the proof. If it reaches `applied`→done, the roadmap advances to file-01.

**LIVE-PROVEN 2026-06-12.** Rebuilt+relaunched (lifecycle kernel ATTESTED `c9c554bffd3a`, stamp==pin). Archived the pre-fix wedged ledgers (file-00 was stuck in EMITTED — `compute_run_state` maps `proposed`→EMITTED and `compute_ready_set` never re-emits an already-emitted sub-goal, which is WHY the live ledger stayed `emitted=1` across every mesh poll; the existing hostage can't self-heal, needs the stale PROPOSED row cleared so it re-decomposes fresh). At t+390s the mesh re-decomposed → file-00 re-emitted → op `op-019ebdaa-...-cau` dispatched → reached terminal → **THE WRITEBACK FIRED**: goal_decomposition_ledger now shows `proposed` then `failed` (note=`terminal:failed via orchestrator op op-...` = my exact code signature). `multi_step._load_completion_status` now reads `{'GOAL-001::file-00':'failed'}` → terminal/DONE, no longer wedged. The severed loop is CLOSED in production.

**NEXT LAYER (distinct from A1):** the op reached `failed` (not `applied`) because GENERATION failed — terminal reason `generation_failed`/`empty_plan` (the GIL-starvation patch for file-00 isn't being produced; connects to the S215/S216 no_plan thread). A1 fixed the structural feedback loop; making file-00 actually SUCCEED (reach `applied`) is the next item. Also surfaced: an EMITTED-recovery watchdog (re-emit sub-goals stuck in EMITTED past a TTL with no terminal record) is the natural robustness follow-up — without it, any op lost before terminal wedges its sub-goal forever even with the writeback.

See [[project_slice131_cost_sovereign]].
