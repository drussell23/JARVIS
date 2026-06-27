---
title: Project Followup Harness Idle Counter Parallel Streams
modules: [backend/core/ouroboros/battle_test/harness.py]
status: historical
source: project_followup_harness_idle_counter_parallel_streams.md
---

## Why this exists

F1 Slice 4 cadence S3 (`bt-2026-04-25-080530`, MERGE_HEAD `1491afc671`, post both fixes #19706 + #19800):

- Cost-cap fix proven live: `[CostGovernor] op=op-019dc3ac- cap bumped for parallel fan-out: $0.4500 → $1.4850`
- 3-stream PLAN-EXPLOIT launched successfully (3 fallback sem acquires)
- Stream 1 + Stream 2 visibly active via tool_round_complete events at 01:09:10 (5 read_file) and 01:11:10 (3 tools incl. glob_files)
- Session shutdown at 01:16:29 — `idle_timeout` fired despite tool activity 5min 19sec earlier
- 5min 19sec is WITHIN the `--idle-timeout 600` (10min) threshold, suggesting the harness's idle counter is NOT consulting tool_round_complete events

**Hypothesis**: harness's idle-counter activity definition is currently restricted to top-level orchestrator events (INTENT, HEARTBEAT, DECISION, POSTMORTEM, APPLY-class events). PLAN-EXPLOIT's child streams emit tool_round_complete from inside their own task contexts, never bubbling to the orchestrator's event surface that the harness watches.

## Operator binding 2026-04-25

> "Follow-up: open/track Option B (idle counter should treat parallel tool-loop / stream progress as activity). Don't implement B in the S4 PR unless I explicitly say so."

S4 launched with `--idle-timeout 1800` (30m) as the immediate workaround. This buys 3× the wall-clock so multi-stream ops can complete even with the current idle-counter blind spot. Long-term fix is this follow-up.

## Scope

Touch the harness's idle-counter "what is activity" predicate. Specifically:

- `backend/core/ouroboros/battle_test/harness.py` — find the idle_timeout watcher loop
- Identify the event source it consults (probably orchestrator's CommProtocol, GovernedLoop heartbeat, or session log writes)
- Add tool_executor's `tool_round_complete` events as activity-class
- Optionally also: PLAN-EXPLOIT's per-stream events, candidate_generator's fallback sem release events

## Non-goals

- Don't change `--idle-timeout` default (still 600s — most ops complete inside that)
- Don't change op-level budgets (operator binding: harness ≠ op budget)
- Don't change session termination policy beyond what's needed to count parallel-stream activity

## Slices (when authorized)

### Slice 1 — find the idle counter + characterize what counts as activity
- Read harness.py source; identify the watcher loop
- Document the current activity predicate (INTENT/HEARTBEAT/etc.)
- Draft fix shape (hook tool_round_complete events as activity)
- No code changes

### Slice 2 — primitive: tool_round_complete → activity hook
- Add a thin pub-sub or direct hook from tool_executor → harness idle counter
- Tests: simulate a session where the only activity is tool_round_complete, verify idle_timeout doesn't fire
- Default-off env knob initially (operator opt-in)

### Slice 3 — graduation: flip default + 3-session cadence under standard `--idle-timeout 600`
- Run F1 cadence with default-600s timeout + multi-stream seed
- Verify seeds complete without timing out

## Authority invariants

- Doesn't change op/provider budgets (separate concern)
- Doesn't loosen the harness's max_wall_seconds ceiling (the absolute kill)
- Master-off → byte-for-byte pre-fix harness behavior

## Status

- **Identified**: 2026-04-25, F1 Slice 4 S3 forensics
- **Tracked**: this doc
- **Workaround active**: S4 launched with `--idle-timeout 1800`
- **Implementation**: NOT authorized; awaiting explicit operator green light
