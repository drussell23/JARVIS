---
title: Swarm reachability — the Golden Rule was never reached
modules: [backend/core/ouroboros/governance/autonomy/swarm_invoker.py, backend/core/ouroboros/governance/autonomy/worker_synthesizer.py, backend/core/ouroboros/governance/autonomy/subagent_scheduler.py, backend/core/ouroboros/governance/autonomy/subagent_factory.py]
status: active
source: session 2026-07-31, liveness audit
---

# Swarm reachability

## The audit

`worker_synthesizer` is O+V's most radical piece: a worker's persona, tool
allowlist, mutation budget and context budget are **synthesized from AST +
semantic inspection of the sub-goal** — "NO static enum, NO dictionary of
Agent Roles". Fail-closed to read-only.

It never ran in production.

## The chain

```
GovernedLoopService  l3_enabled=true (DEFAULT ON)     ✅ live
  → SubagentScheduler._execute_unit_guarded            ✅ unconditional, every unit
    → _get_swarm_invoker()                             ✅ wired (line 1050)
      → SwarmUnitInvoker.execute()
        → _should_route_swarm()                        ❌ ALWAYS FALSE
        → synthesize_worker_spec()                     ⛔ never reached
```

`_should_route_swarm` ended in `return bool(unit.is_swarm_worker)`, and
`is_swarm_worker` is True only when the unit ALREADY carries
`system_prompt_template` / `allowed_tools` / `worker_role`. **All five
production builders leave them None** (`parallel_dispatch`,
`iteration_planner`, `providers`, `meta_goal_aggregator`, `graph_coalescer`);
`subagent_types.execution_graph_from_dict` only round-trips them. The only
writer is `SwarmOrchestrator.define_worker`, which has **zero production
callers** — in the invoker it is an injectable test seam defaulting to None.

So the invoker would only shape a unit that was already shaped. Circular.
`swarm_invoker`'s own docstring says it closed "nothing CALLS the swarm"; it
left "nothing is ever ELIGIBLE for it" — the same defect one layer in.

**The suite asserted the bug was correct.** `test_swarm_on_legacy_unit_in_multi_graph_uses_legacy`
pinned "a non-swarm unit → legacy even in a multi DAG", which under the old
predicate means *every real unit, always*. That is why it stayed green.

## The fix

`swarm_eligible(unit)` asks what the synthesizer NEEDS, not what a marking
says: a unit with `target_files` (dataclass-guaranteed non-empty) and a
non-blank `goal` is synthesizable. Pre-shaped units stay eligible so the
`define_worker` path is unchanged. Fail-CLOSED: blank goal → legacy.

Rollback: `JARVIS_SWARM_REQUIRE_PRESHAPED_UNITS=1` restores the old predicate
without touching the master gate.

## Proven

A unit with NO swarm fields (exactly `parallel_dispatch`'s shape) now routes
SWARM and derives: `role='python-source mutator'`,
`tools=(read_file, search_code, list_dir, get_callers, edit_file)`,
`mutation_budget=3`, `ctx=18180`. A read-only goal gets **no** mutation tool.
Cage failure → `FAILED/swarm_cage`, never uncaged.

## Two things called "swarm"

- `JARVIS_SWARM_ROUTING_ENABLED` → `candidate_generator._maybe_swarm_short_circuit`
  → `full_content_interceptor` → `agentic_super_agent`. Genuinely wired on the
  live GENERATE path. **Does not touch the synthesizer.**
- `JARVIS_SWARM_ORCHESTRATOR_ENABLED` → scheduler → invoker → synthesizer.
  This one.

Both still default-OFF. ⚠️ Not soaked.

## Slice 2 — the cage learns (`autonomy/cage_calibration.py`)

Making the synthesizer reachable exposed that it is **open-loop**: a derived
PRIOR with no posterior. A worker granted `mutation_budget=3` that never used
more than one taught it nothing; the same shape was re-derived, identically
wrong, forever. Exactly the defect `memory_utility` fixed for topics, one
layer down.

`ScopedToolBackend` was recording the evidence all along — `mutations_count`
vs `max_mutations`, `call_records` stamped `authorized`/`type_denied`/
`count_denied`. Nobody consumed it for learning.

**Tighten autonomously; NEVER widen.** The asymmetry is the whole design.
Observed under-use is safe to act on — dropping to what evidence shows grants
strictly less than the prior already did. Observed DENIAL is not: widening a
cage because a worker kept asking builds a privilege-escalation ramp out of
persistence, reachable by any worker including a prompt-injected one. So
denials become a FINDING naming `worker_synthesizer`'s RULE as the thing to
fix — the root cause. A learned per-class exception would erode the cage one
class at a time.

Note this is the OPPOSITE direction from `UserMemory.matches_path`, where the
safe move was to widen a guard. The invariant is not "always widen" or
"always tighten" — it is *move only in the direction that cannot grant
something new*. Deny-list → widen. Allow-list → tighten.

`_is_tightening` asserts the post-condition rather than trusting it: tools
must be a SUBSET and budget `<=`, or the prior is returned. A calibrator is
not permitted to be the thing that grants privilege.

**Learning key = the synthesizer's own clustering.** `role` + `read_only`,
both already derived. No second taxonomy to drift; a new role invented by
inspection tomorrow becomes a new learning class with zero code change.

**Reuse:** two `memory_utility.UtilityStore` instances (outcome, headroom) —
already an opaque-key store of decayed, confidence-weighted observations with
a corpus-mean baseline. A second implementation would be a second definition
of "how fast does evidence age".

Refusals: cold start → prior unchanged; thin evidence → prior; FAILED units
never teach the cage to shrink (a crash's low usage is not "needs less");
unattributable shapes are not recorded at all.

Wired at the ONE seam in `_execute_swarm` — calibrate between synthesis and
cage, observe after execution. Both fail-open.

Default **OFF** (`JARVIS_CAGE_CALIBRATION_ENABLED`) — it narrows a live
security boundary from data. 18 tests; 761 green across autonomy.
