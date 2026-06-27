---
title: Autonomous Convergence Watchdog (2026-06-22, MERGED PR #69660)
modules: []
status: merged
source: project_convergence_watchdog.md
---

# Autonomous Convergence Watchdog (2026-06-22, MERGED PR #69660)

**Why:** with AST-slicing + the egress interceptor live, the risk shifted from network-spam to **local infinite recursion** — an irreducible-but-overweight AST block re-decomposing forever. Self-healing, no manual babysitting, no hardcoded retry caps for stall *detection*.

**What shipped** (all default-ON, fail-soft, OFF byte-identical):
- **`convergence_watchdog.py`:** reduction-trajectory tracker — `ratio = max(child_chars)/parent_chars`; stall = ratio ≥ `JARVIS_WATCHDOG_STALL_RATIO` (0.95) for `JARVIS_WATCHDOG_STALL_PASSES` (2) consecutive passes (env-tuned, no magic numbers). Bounded LRU per-lineage (reuses recursion_dedup FIFO pattern). `emit_sovereign_yield` → `[SOVEREIGN YIELD]` stdout WARNING + best-effort SSE (`publish_task_event`).
- **`epistemic_shedder.py`:** tiered **pure-AST** weight shed (NEVER exec/eval) — Tier1 strip docstrings (comments drop via unparse) → Tier2 hollow heaviest FunctionDef/AsyncFunctionDef bodies to signatures (`[SOVEREIGN YIELD: Implementation Omitted]`) → Tier3 prefix-truncate. Re-measures each tier; parse-error → Tier3.
- **Sovereign Ledger-Watchdog Composition (the keystone, `orchestrator._watchdog_self_heal` + `_decompose_block_or_legacy`):** (1) **invariant lineage** `subgoal_hash(target_files, ())` — survives async re-injection (was `ctx.op_id` = per-op → never stalled, the dormant-on-live-path bug a review caught); (2) **funnel inversion** — on the egress re-chunk path (`compression_target is not None and watchdog_enabled()`), a DUPLICATE goal goes to `_watchdog_self_heal` FIRST (de-dup yields) for a shed-and-CONTINUE before hard-failing; de-dup ledger retained as the FINAL backstop; (3) **deep-payload shed** (`goal_decomposition_planner.shed_block_goal_to_fit`) — reads full target-file SOURCE (the ruler is dominated by symbol segments, not description), sheds, inlines + CLEARS `scoped_symbols` so `estimate_subgoal_payload_chars ≤ compression_target` → next egress check passes → loop breaks.

**Termination (TWO Opus reviews):** provably bounded. **Review #1 caught a degenerate bound:** the claimed "≤1 hop" was wrong — `multi_step_orchestrator._make_envelope` re-injects `description = f"{title}\n\n{shed}"`, so the title prefix shifted the Tier3 truncation window each hop → fixpoint not hit until ~`compression_target/82` ≈ **7,300 re-injection hops** at the 600k default ceiling (a multi-minute storm, NOT infinite). **Fixed (#599a726b):** explicit per-lineage self-heal cap `JARVIS_WATCHDOG_MAX_SELF_HEAL_HOPS` (default **3**, env-tunable) — small constant bound independent of shed/envelope dynamics + `title=""` stabilizes the prefix. A stalled-and-unsheddable lineage still terminates `advisor_blocked` via de-dup. 61 arc tests + static stall→yield proof.

**LESSON (3rd time on this program): "wired-but-inert-on-the-live-path."** The watchdog was wired but INERT live because lineage=op_id never accumulated + the de-dup ledger pre-empted the shed. The final review (not per-task tests) caught it. **Always ask: does the seam ENGAGE on the live path, or is it a tested-but-dormant decoration that a legacy fallback pre-empts?** The funnel inversion (de-dup yields to the self-heal) is the fix for "a lazy legacy hard-fail shadows the dynamic self-heal."

**Status:** MERGED. C2 convergence soak IGNITED 2026-06-22 ~21:58Z (node `jarvis-ouroboros-soak-20260622-145847`, e2-custom-8-16384 SPOT, us-central1-a, gpt-oss-120b pin, $10 cap, 1h battle wall). Composes with [[project_sovereign_egress_interceptor]] (the egress guard it backstops) + [[project_sovereign_resilience_chunking]] (the chunking matrices) + [[project_a1_intake_dispatch]] (A1 dispatch). Convergence proof chain to watch: `[A1Trace] accept` → `Advisor BLOCKED` → `BLOCK decomposed` → (if a sub-goal still heavy) `[SOVEREIGN YIELD]` self-heal → generation → `ouroboros/review` orange PR. **DW-citizen rule: delete the node promptly if it stalls/doesn't converge (pure DW noise).**
