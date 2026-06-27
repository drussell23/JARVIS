---
title: Project V57 Hybrid Soak
modules: [backend/core/ouroboros/governance/tool_executor.py]
status: historical
source: project_v57_hybrid_soak.md
---

v57 production hybrid soak (bt-2026-06-01-231338, Claude ENABLED, $15/600s-idle/5400s-wall). Killed manually at ~12min (operator authorized) — 0 commits, burning Claude $ on noops/fails. Branch slice-58 opened then deleted; main pristine (Slices 50,52,53,54,55,56 merged).

**Telemetry @12min:** DW RT produced candidates FAST/CHEAP in hybrid (21.8s/$0.0014, 49.8s/$0.0015, 107s/$0.0007) — reasoning unlock (S54) + RT path (Claude-enabled → no force-batch) working. **0 APPLY, 0 commits.** Bonus: **Slice 56 zero-leak HELD under 12min concurrent hybrid load — main tree clean.**

**Why 0 commits (NOT a regression — op selection + legit outcomes):**
- torch op → `2b.1-noop` "torch already pinned to 2.12.0, prior Ouroboros op applied it, no change needed" (provider=claude-api) = LEGITIMATE no-change. NOT a diff-engine bug.
- proactive-exploration op (vague goal on `backend/core/`, 10912-byte file) → DW candidates `full_content too short` (97/208/269/134 bytes vs 10912) → correctly skipped. TRUNCATED output, likely vague-goal + possibly reasoning_effort=none quality tradeoff.
- immediate/Claude ops → `tool_loop_starved_below_min_ttft_floor` budget bails (3x) under loop lag.
- The committing op-class (v48's test_todo_scanner, real change) just didn't recur this session.

**Slice 58 BOTH phases REJECTED (verify-first):**
- **Phase 3 (audit noop diff engine): MISDIAGNOSED** — noops were LEGITIMATE (model correctly said no change). Real DW-candidate issue = `full_content too short` (truncation), a generation-completeness problem, not a noop-detection bug.
- **Phase 2 (elastic min-TTFT, discount LoopSink lag + proceed): RISKY/misdirected** — `tool_loop_starved_below_min_ttft_floor` (tool_executor.py:5467) is a PROTECTIVE bail (its comment: starting a round with too little budget makes outer wait_for "murder a healthy stream mid-token-flow", bt-2026-05-25-012206). Discounting lag to PROCEED would re-expose the murdered-mid-stream bug the bail prevents. Runbook also targeted wrong file (sensors/control_plane_watchdog.py doesn't exist; tool_loop_starved isn't a watchdog check). Real fix = reduce loop lag (§48, murky — Slice 51 found no clean CPU-governor fix) or widen immediate budget (tuning) — NOT discount-and-proceed.

**CONSOLIDATED TRUE STATE (end of the v44→v57 arc):** The platform WORKS. First autonomous zero-leak commit proven (v48 abbabc70, S56). DW corridor unlocked (S54 reasoning), fast/cheap RT (~$0.0015), honest health probe (S55), complexity-tuned effort (S55), zero-leak worktree commits (S56), dual-lane breaker (S53). The v44→v53 "DW broken" saga was entirely client-side (reasoning parsing + commit-tree mismatch). Recent runbook slices (57, 58) chase non-problems — verify-first rejected them. **Genuinely-open levers (data-driven, low urgency, none block the proven capability):** (1) DW full_content truncation on vague/complex ops (reasoning_effort-for-complex tuning, or generation-completeness); (2) loop lag §48 (murky, posture-git/GIL — see [[project_slice_51_disambiguation]]); (3) op-selection (sensors emit many noop/vague ops vs change-producing ones). Recommendation given operator: STOP the slice treadmill; platform is solid; pursue the real levers only with data + clear ROI. See [[project_slice_56_worktree_commit]] [[project_slice_54_reasoning_unlock]]
