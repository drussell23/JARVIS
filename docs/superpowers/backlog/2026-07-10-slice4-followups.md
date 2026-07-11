# Slice 4 follow-up backlog (non-gating; from whole-branch reviews 2026-07-10)

Evidence: Run #14 autopsy (docs/superpowers/specs/2026-07-10-run14-autopsy.md), Slice 4 whole-branch
reviews, `.superpowers/sdd/slice4-evidence-pack.md`.

1. **`_flush_coalesced` sync WAL ack on-loop** — `unified_intake_router.py:1709` acks through the sync
   `update_status` from a plain `def` (correctly excluded from T4's Mandate-2 scope). PRE-EXISTING at
   base; only bites under coalesce storms. Fix shape: async-ify `_flush_coalesced` (its callers permit)
   or offload the ack; then convert the last WAL call site to the async twin.
2. **SPECULATIVE lane `_background_polls` cap bypass (LongCat Phase 2 GRADUATION BLOCKER)** —
   `candidate_generator.py` `_generate_speculative` else-branch (~:6646) stores `_lane_task` without the
   `_max_background_polls` cap/eviction the DW poll path enforces (:4107-4111). Unreachable while the
   hosted resilience lane is dark; MUST route through the same prune/cap block before the lane arms.
3. **`_consecutive_lt` streak not reset on budget-refusal `continue`** — sentinel loop's refusal branch
   (~:5256-5290) skips the LIVE_TRANSPORT streak reset that other non-transport failures perform.
   Near-unreachable (refusals are homogeneous), 1-line correctness fix when next in the file.
4. **First BG/SPEC op does a sync `load_lane_configs` YAML read on-loop** — hosted_resilience_lane;
   cached after first call, fail-soft. Offload or pre-warm at boot when the lane graduates.
5. **T4 AST-pin coverage gap** — `test_slice4_wal_async.py` source-pins only uir.py:1267 + :2173 shapes;
   the :1954/:2208/:2230 conversions are revertible without pin failure. Extend pins or add a
   straggler-grep test (zero `self._wal.append(`/`update_status(` inside async defs).
6. **Slice 4 deferred list (unchanged from plan):** shutdown-teardown interruptibility;
   mutation_gate/multi_prior/auto_action_router flock conversions; file_has_test_coverage residual
   re-diagnosis (needs cpu_ms data from a quiet run); LoopSink async-window attribution refinement.
7. **ignite_a1_brain non-failover starve mode** — T1b Important #2 adjudicated KEEP-ABORT: its
   documented cost_cap=0.0 starve without --enable-failover is a zero-provider bug class; either arm
   failover in that mode or retire the doc snippet.
