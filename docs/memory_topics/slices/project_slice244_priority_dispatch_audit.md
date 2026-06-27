---
title: Project Slice244 Priority Dispatch Audit
modules: [tests/governance/test_slice244_priority_dispatch.py]
status: merged
source: project_slice244_priority_dispatch_audit.md
---

**Slice 244 — Asynchronous Priority Queue audit + proof. MERGED PR #69486, main `f82cdc63`.** Authorized as a "restructure the WAL from single-lane FIFO into a priority scheduler" feature; verify-first audit found the premise FALSE at every layer → shipped a PROOF that locks the existing invariant instead of duplicating it. Part of the DW-sovereignty arc ([[project_dw_sovereignty_arc_intent]]), follows [[project_slice242_recovery_prior]] (242 prior + 243 stability gate live in that file).

**DURABLE ARCHITECTURAL TRUTH (so future slices don't re-investigate): intake dispatch is a TWO-TIER integer-priority system, NOT FIFO.**
- **Intake layer:** `intake/unified_intake_router.py` enqueues onto an `asyncio.PriorityQueue` (`:561`) via `_compute_priority(envelope, dependency_credit=0) -> (int, alignment)` (`:311`; LOWER int = HIGHER primacy). `:1202` "IntakePriorityQueue is the source of truth for dequeue order." Also a parallel `IntakePriorityQueue` (F1 Slice 2, `JARVIS_INTAKE_PRIORITY_SCHEDULER_ENABLED`). Priority factors: base source tier + urgency boost + cost penalty (file count) + confidence + dependency credit (cap 3) + goal_alignment boost.
- **`_PRIORITY_MAP`** (primary tiers): voice_human=0, test_failure=1, backlog=2, swe_bench_pro=2, ai_miner=3, architecture=3, exploration=4, roadmap=4, capability_gap=5, cu_execution=5, runtime_health=6. **`_PRIORITY_MAP_DEFERRED`** (lowest, base 99→~100): test_coverage (Slice 239), todo_scanner, doc_staleness, web_intelligence, vision_sensor, github_issue, performance_regression, security_advisory, intent_discovery, cross_repo_drift, auto_proposed, cadence_synthetic, meta_dormancy_alarm. Unmapped → 99.
- **Background pool layer:** `background_agent_pool.py` `_queue` is ALSO an `asyncio.PriorityQueue` (`:293`), keyed on `provider_route` (`submit()` `:553`): immediate=1, standard/complex=3, background=5, speculative=7, tie-break by submission_order. Decoupled pool (`JARVIS_BG_POOL_SIZE` default 3, `JARVIS_BG_QUEUE_SIZE` default 16). HIBERNATION = pause the pool (`:818` block-while-paused), NOT WAL re-ingest. WAL `_replay_wal` is a BOOT path only (cross-restart at-least-once).
- **So a primary GOAL (priority 0-6 / route immediate-standard 1-3) ALWAYS dequeues ahead of a test_coverage shard (~100 / route background 5) at BOTH layers.** Slice 240 `should_decouple_test_gen` already budget/velocity-gates test-gen decoupling.

**Why no rebuild:** a float-weight matrix (1.0/0.5) + new scheduler + new bg lane would DUPLICATE all of the above. The WAL (`wal.py` WALEntry: lease_id/envelope_dict/status/ts) is a DURABILITY log (pending/acked/dead_letter), NOT the live dispatch queue — it has no priority field by design (priority is computed at enqueue into the PriorityQueue).

**Shipped:** `tests/governance/test_slice244_priority_dispatch.py` (7 tests) proves Phase 4 against real machinery (inject test,test,GOAL out of order → GOAL first, shards last, at both lanes) + Phase 3 Iron-Gate-deferral safety (per-op GENERATE→VALIDATE gate vs cross-op intake ordering — reordering distinct intents never touches one op's gate sequence; test_coverage is a Slice-239 distinct decoupled intent, never a precondition). Fixed stale "FIFO ordering" docstring at `background_agent_pool.py:35` (the wording that seeded the false premise; in-code comment already said PriorityQueue). 2 pre-existing `test_bg_readonly_cascade` failures (providers.py drift + topology-skip) proven unrelated via stash-compare.

**Process:** branched off FRESH `origin/main` (lesson from the 243 rebase conflict — don't cut slice branches from a prior un-squash-merged feature branch).
