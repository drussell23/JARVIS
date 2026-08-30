# Wave 3 (6) Parallel L3 Fan-Out — Graduation + Hot-Revert Runbook

**Status (Slice 5a delivery)**: 4 structural slices CLOSED; defaults
all `false`; awaiting Slice 5b operator-authorized default flip after
F1 (intake priority scheduling) unblocks live multi-file reachability.

**Source**: `backend/core/ouroboros/governance/parallel_dispatch.py`
**Wired into**: `backend/core/ouroboros/governance/phase_dispatcher.py`
(post-GENERATE seam, lines 588–634)
**Pinned by**: `tests/governance/test_w3_6_slice5a_structural_pins.py`
**Live-fire smoke**: `scripts/livefire_w3_6_parallel_dispatch.py`

---

## What this enables (post-Slice-5b graduation)

Pre-Wave-3-(6): `phase_dispatcher.dispatch_pipeline` walked phases
serially per op. Multi-file ops (`n_candidate_files >= 2`) executed
file-by-file across the same git worktree.

Post-Slice-5b graduation: when eligibility allows, the post-GENERATE
seam fans out the candidate files across **L3 worktrees in parallel**
via the existing `subagent_scheduler` (Wave 1 #3 graduated primitive)
and the `worktree_manager` lifecycle (Wave 1 #3 graduated). Throughput
gain on multi-file generation; no authority change.

Eligibility decision (`is_fanout_eligible`) gates on **all four**:

1. Master flag on (`JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED=true`).
2. Either shadow OR enforce sub-flag armed (telemetry-only vs actual submit).
3. `n_candidate_files >= 2` (single-file ops are not eligible).
4. Posture confidence ≥ 0.3 floor AND memory pressure < CRITICAL
   (HARDEN posture clamps fan-out via `posture_weight_for()`).

Each decision is recorded as a `FanoutEligibility` (8 reason codes:
`allowed / master_off / empty_candidate_list / single_file_op /
posture_low_confidence / memory_critical / memory_clamp / posture_clamp /
max_units_clamp`).

---

## Hot-revert recipe (single env knob)

```bash
export JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED=false
```

That single flip force-disables every sub-flag (mirrors W2(4) and
W3(7) cancel master-off composition):

| Sub-flag                                      | Master-off behavior                  |
|-----------------------------------------------|---------------------------------------|
| `JARVIS_WAVE3_PARALLEL_DISPATCH_SHADOW`       | composition no-op (master gates first) |
| `JARVIS_WAVE3_PARALLEL_DISPATCH_ENFORCE`      | composition no-op (master gates first) |
| `JARVIS_WAVE3_PARALLEL_MAX_UNITS`             | unused (no fan-out happens)           |
| `JARVIS_WAVE3_PARALLEL_WAIT_TIMEOUT_S`        | unused (no fan-out happens)           |

`is_fanout_eligible` returns `FanoutEligibility(allowed=False,
reason_code=MASTER_OFF)` immediately. The post-GENERATE seam in
`phase_dispatcher.py` (`dispatch_pipeline` lines 588–634) reverts to
serial per-phase walks (the graduated #8 dispatcher path). No code
revert, no service restart beyond reloading env.

---

## Env knob reference

| Knob | Default | Purpose |
|---|---|---|
| `JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED` | `false` | **Master**. Single hot-revert env knob. |
| `JARVIS_WAVE3_PARALLEL_DISPATCH_SHADOW` | `false` | Telemetry-only fan-out evaluation (no actual submit). |
| `JARVIS_WAVE3_PARALLEL_DISPATCH_ENFORCE` | `false` | Real fan-out via subagent_scheduler. |
| `JARVIS_WAVE3_PARALLEL_MAX_UNITS` | `3` | Hard ceiling on fan-out degree. |
| `JARVIS_WAVE3_PARALLEL_WAIT_TIMEOUT_S` | `900.0` | Max wall-clock for `wait_for_graph` before TIMEOUT. |

All 5 knobs registered in the FlagRegistry (Wave 1 #2) at
`GovernedLoopService.start` via `parallel_dispatch.ensure_flag_registry_seeded()`.
Discoverable via:

```
/help flag JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED
/help flags --search parallel_dispatch
/help flags --posture HARDEN   # surfaces the master + enforce flags
```

---

## Authority preservation

- **§1 additive only** — fan-out is a *throughput* optimization; no
  rule softened. Each parallel unit still passes through the same
  Iron Gate / SemanticGuardian / risk-tier-floor / change_engine
  pipeline serially within its own worktree.
- **§2 Progressive Awakening** — `worktree_manager.reap_orphans()`
  sweeps stale `unit-*` worktrees on boot (already graduated
  Wave 1 #3); fan-out doesn't add new orphan classes.
- **§5 Tier 0** — eligibility decision is pure deterministic code
  (`is_fanout_eligible`), no LLM call. Posture + memory-pressure read
  via injected functions for testability.
- **§6 Iron Gate** unchanged — every fan-out unit passes the same
  exploration ledger, ASCII strict, dependency integrity, multi-file
  coverage gates that serial ops do.
- **§7 Approval surface** untouched.
- **§8 Observability** — log-only currently:
  - `[ParallelDispatch] op=... allowed=...` (every eligibility decision)
  - `[ParallelDispatch shadow_graph_built ...]` (shadow path)
  - `[ParallelDispatch enforce_submit_start ...]` + `enforce_{completed|failed|cancelled|timeout|submit_denied}` (enforce path)

  SSE bridges for these events are scope-deferred to a separate
  follow-up (see `project_wave3_item6_scope.md` §"Known debt").

---

## Graduation cadence (Slice 5b — pending operator authorization)

Per the W2(5) protocol carried forward into W3(6):

1. **Authorization**: operator approves Slice 5b default flip.
2. **3 clean battle-test sessions** under the canonical recipe:
   ```bash
   JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED=true \
   JARVIS_WAVE3_PARALLEL_DISPATCH_ENFORCE=true \
   python3 scripts/ouroboros_battle_test.py \
       --headless --cost-cap 1.00 \
       --idle-timeout 600 --max-wall-seconds 2400 -v
   ```
3. **Reachability bar**: ≥1 `[ParallelDispatch enforce_submit_start]`
   marker observed across the 3 sessions; ≥1 enforce terminal
   (completed / failed / cancelled / timeout) with downstream classifier
   matching scheduler state.
4. **Extended clean-bar blocklist**: 0 traceback frames in
   `parallel_dispatch.py` / `worktree_manager.py` / `phase_runners/` /
   `phase_dispatcher.py`. Carries over Wave 2 (5) bar.
5. **Single flip commit** — `parallel_dispatch_enabled()` default
   `false → true` (or master + enforce together, operator's choice).
6. **Post-flip confirmation soak** — natural defaults, no env
   overrides.
7. **On clean post-flip**: Wave 3 (6) → **FINAL**.

### Live reachability blocker (current state — promoted 2026-04-25)

**Block class history**:
- W3(6) S1+S2+S3 cadence (2026-04-23/24): `live_reachability=blocked_by_intake_starvation` — BG sensor flood starved the forced-reachability seed in `UnifiedIntakeRouter`'s class-partitioned FIFO queue.
- F1 Slice 4 cadence S1b (2026-04-25): `live_reachability=blocked_by_provider_exhaustion` — **intake starvation FIXED**, but seed exhausts at GENERATE via DW primary failure → Claude fallback `CancelledError` after 72.8s sem_wait × 3 retries (`anthropic_transport`-class infra waiver, not runner-attributed).

**S1b proved F1+F2 plumbing end-to-end on production traffic** — first
ever live battle-test where `[IntakePriority] primary dequeue` markers
fired (8 in session) AND `envelope_routing_override` markers fired (3
in session) AND the F2-stamped seed routed STANDARD via the priority-0.5
clause (was IMMEDIATE in S1a without F2). Extended blocklist clean (0
traceback frames in `parallel_dispatch.py`/`worktree_manager.py`/
`phase_runners/`/`phase_dispatcher.py`).

**The structural intake failure mode is resolved.** What remains is a
downstream provider transport reliability issue, classified as
infra-noise per the W2(5) graduation matrix's `anthropic_transport`
waiver convention.

**Unblock paths** (operator-deferred 2026-04-25):
- Provider transport reliability fix (DW endpoint stability OR Claude
  fallback timeout/retry-budget tuning) — outside W3(6)+F1+F2 scope.
- Organic accumulation: under steady-state operation, any sensor
  producing a multi-file generation will exercise the now-graduated-
  shape F1+F2 path and the post-GENERATE seam will fire.

**Explicitly REJECTED unblock paths** (per operator binding):
- Modifying the seed to a smaller workload to dodge the provider
  timeout — would mask the issue, not fix it.
- Inflating timeout boundaries via env — same masking concern.

**Cadence record** (full evidence in
`memory/project_wave3_item6_graduation_matrix.md` §"F1 Slice 4
cadence S1b — `live_reachability=blocked_by_provider_exhaustion`"):
session `bt-2026-04-25-054256`, MERGE_HEAD_SHA `b068c8a083`,
1/3 cadence sessions recorded, PAUSED — Slice 5b default flip remains
blocked, Slice 4 default flip remains blocked.

---

## Operator-facing audit

Tail the battle-test log to watch fan-out decisions in real time:

```bash
tail -f /tmp/claude/w3_s6_s<N>.log | grep -E '\[ParallelDispatch'
```

Marker glossary:

| Marker | Meaning |
|---|---|
| `[ParallelDispatch] op=... allowed=true` | Eligibility passed; n_allowed units approved. |
| `[ParallelDispatch] op=... allowed=false reason=master_off` | Master flag off (hot-revert active). |
| `[ParallelDispatch] op=... allowed=false reason=single_file_op` | Op has only 1 candidate file; fan-out N/A. |
| `[ParallelDispatch] op=... allowed=false reason=posture_low_confidence` | Posture confidence < 0.3 floor. |
| `[ParallelDispatch] op=... allowed=false reason=memory_critical` | MemoryPressureGate at CRITICAL. |
| `[ParallelDispatch] op=... allowed=true reason=posture_clamp n_allowed=2` | HARDEN-posture weighted down from 3 to 2 units. |
| `[ParallelDispatch shadow_graph_built ...]` | Shadow-mode graph constructed (no submit). |
| `[ParallelDispatch enforce_submit_start ...]` | Enforce-mode graph submitted to scheduler. |
| `[ParallelDispatch enforce_completed ...]` | All units COMPLETED in scheduler. |
| `[ParallelDispatch enforce_failed ...]` | Graph reached FAILED phase. |
| `[ParallelDispatch enforce_cancelled ...]` | Graph reached CANCELLED phase. |
| `[ParallelDispatch enforce_timeout ...]` | wait_for_graph hit `JARVIS_WAVE3_PARALLEL_WAIT_TIMEOUT_S` ceiling. |

---

## Live-fire smoke (developer-friendly, no battle-test required)

```bash
python3 scripts/livefire_w3_6_parallel_dispatch.py
```

Runs ~25 in-process structural checks covering:
- All 5 env knob defaults (master off, sub-flags off, max_units=3, timeout=900s)
- Hot-revert composition (master=false force-disables sub-flag effect)
- Eligibility decision matrix (8 ReasonCode paths)
- Shadow-mode graph construction (telemetry only)
- Enforce-mode FanoutOutcome paths (5 outcomes via stub scheduler)
- Iron Gate composition (parallel units don't bypass gates)
- FlagRegistry seed (all 5 knobs registered with correct types)

Exit 0 on PASS; non-zero with failed-check summary on FAIL.

---

## Slice 5a evidence package

| Artifact | Status |
|---|---|
| Reachability test-harness supplement | ✓ green (committed in `92ddb54463`, expanded in this slice to 8 scenarios) |
| FlagRegistry seed | ✓ landed in this slice (`parallel_dispatch.ensure_flag_registry_seeded`) |
| Operations runbook | ✓ this document |
| Structural pin tests | ✓ `tests/governance/test_w3_6_slice5a_structural_pins.py` |
| Live-fire smoke | ✓ `scripts/livefire_w3_6_parallel_dispatch.py` |
| F1+F2 plumbing live-PROVEN | ✓ S1b session `bt-2026-04-25-054256` (8 F1 markers, 3 F2 markers, seed routed STANDARD) |
| 3 clean live sessions | ⚠ 1/3 recorded; cadence PAUSED on `live_reachability=blocked_by_provider_exhaustion` (`anthropic_transport` infra waiver) |
| Slice 5b default flip | ⏸ pending provider-transport unblock OR organic multi-file generation accumulation |

---

## Graduation pin contract (Slice 5b prerequisite)

`tests/governance/test_w3_6_slice5a_structural_pins.py` enforces
on every commit going forward:

- **(A) Master default false (pre-graduation)** + master-off composition: every sub-flag reader gates on `if not parallel_dispatch_enabled()` first (structural enforcement).
- **(B) Sub-flag composition** under master-on / master-off — explicit setenv tests.
- **(C) Hot-revert path** — master=false + every sub-flag=true → all eligibility paths return MASTER_OFF.
- **(D) Authority invariants** — ReasonCode enum stable (8 values), FanoutOutcome enum stable (7 values), schema constants frozen (PLANNER_ID, GRAPH_SCHEMA_VERSION).
- **(E) Source-grep pins** — `phase_dispatcher.py` post-GENERATE seam, `parallel_dispatch.py` env reader literals, GLS FlagRegistry seed call site.
- **(F) FlagRegistry registration** — all 5 knobs present in registry with correct FlagSpec types (BOOL/INT/FLOAT) and category assignments.

When Slice 5b flips the master default `false → true`:
1. Update pin (A) to assert default true (rename `test_master_default_false_pre_graduation` → `test_master_default_true_post_graduation`).
2. Update the env-reader source-grep pin in (E).
3. Update this runbook's "Default" column above.
4. Add a graduation evidence row to `project_wave3_item6_graduation_matrix.md`.

If any other graduation pin breaks: either the change is a regression
(fix), or the contract is intentionally being expanded (update the
pin AND the corresponding runbook section). The master-off invariant
is non-negotiable.
