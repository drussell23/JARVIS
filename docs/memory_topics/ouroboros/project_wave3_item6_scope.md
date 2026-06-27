---
title: Wave 3 (6) — Parallel L3 fan-out — SCOPE DOC
modules: [backend/core/ouroboros/governance/autonomy/subagent_scheduler.py, backend/core/ouroboros/governance/worktree_manager.py, backend/core/ouroboros/governance/memory_pressure_gate.py, backend/core/ouroboros/governance/posture_store.py, backend/core/ouroboros/governance/saga/merge_coordinator.py, backend/core/ouroboros/governance/phase_dispatcher.py, backend/core/ouroboros/governance/parallel_dispatch.py, backend/core/ouroboros/governance/phase_runners/generate_runner.py, backend/core/ouroboros/governance/phase_runners/slice4b_runner.py, tests/governance/test_parallel_dispatch_eligibility.py, tests/governance/test_parallel_dispatch_graph_build.py, tests/governance/test_parallel_dispatch_integration.py]
status: merged
source: project_wave3_item6_scope.md
---

# Wave 3 (6) — Parallel L3 fan-out — SCOPE DOC

**Status:** PLANNING ONLY. **No implementation** until operator sends `authorize Wave 3 (6) implementation`. This doc locks the contract before code.

**Operator binding 2026-04-23 (on which this scope stands):**
- Wave 2 (5) CLOSED — all 8 PhaseRunner flags default-true; `phase_dispatcher.dispatch_pipeline` is the production path.
- Wave 3 (6) authorized as the NEXT single arc.
- Wave 3 (7) mid-phase `/cancel` sequenced AFTER (6) FINAL — no parallel execution.
- Wave 2 (4) curiosity / capped `ask_human` remains Wave 2.5 deferred; not interleaved with (6).
- Reject "both in parallel."

---

## 1. What this arc ships

**Capability unlock:** multi-file coordinated ops (`files: [...]` shape from Slice 5 multi-file gen) currently walk phases sequentially — GENERATE produces one candidate list, then APPLY iterates files. Under (6), eligible ops build an `ExecutionGraph`, submit it to the existing `autonomy/subagent_scheduler.py`, and run units in parallel across L3 worktrees governed by `MemoryPressureGate` + `StrategicPosture` fan-out caps.

**Wall-clock delta:** 10-file coordinated refactor today ≈ 30 min serial (per-file tool loops). Under (6) with 3-way fan-out under healthy memory pressure ≈ 10 min. 3–6× on the op shapes where fan-out is admissible.

**What does NOT change:**
- §6 Iron Gate semantics preserved — every fanned unit walks GATE + SemanticGuardian + Exploration Ledger exactly as a single-op would. No bypass, no shortcut.
- §1 execution authority unchanged — deterministic code still holds absolute authority over every unit.
- §3 lifecycle physics preserved — idle / budget / wall-clock ceilings (Ticket A1) apply at the outer op level.
- Multi-file APPLY atomicity preserved — if any unit fails, the batch rolls back as a unit (existing `ChangeEngine.execute` snapshot + restore, now applied across fan-out units).

## 2. Pre-existing infrastructure this arc composes on

Not starting from scratch. These are already in production and graduated:

- `backend/core/ouroboros/governance/autonomy/subagent_scheduler.py` (984 lines) — L3 scheduler with `submit(ExecutionGraph)` + `_run_graph` + MemoryPressureGate consultation (Wave 1 Slice 5 Arc B, commit reference in `project_slice5_arc_b.md`).
- `backend/core/ouroboros/governance/worktree_manager.py` (366 lines) — L3 worktree create/register/prune/reap_orphans lifecycle (graduated 2026-04-19 in `project_worktree_isolation_hardened.md`).
- `backend/core/ouroboros/governance/memory_pressure_gate.py` (651 lines) — 4-level pressure probe (OK / WARN / HIGH / CRITICAL) with `can_fanout(n)` decision (Wave 1 #3, graduated 2026-04-21).
- `backend/core/ouroboros/governance/posture_store.py` + `posture.py` — posture read for weighting fan-out cap (Wave 1 #1, graduated 2026-04-21).
- `backend/core/ouroboros/governance/saga/merge_coordinator.py` — per-unit result merge into a single commit.
- `backend/core/ouroboros/governance/phase_dispatcher.py` (597 lines) — Wave 2 (5) #8 graduated 2026-04-23. Dispatch pipeline walks phases via registry; this arc extends its GENERATE + APPLY hooks.

## 3. Files touched

### New

- `backend/core/ouroboros/governance/parallel_dispatch.py` — arc's main module. Three responsibilities:
  1. `is_fanout_eligible(ctx) -> bool` — pure function, decides whether an op's shape admits fan-out (multi-file gen/apply, posture not HARDEN-below-threshold, memory not CRITICAL). Zero LLM calls. §5 Tier 0 math.
  2. `build_execution_graph(ctx, candidates) -> ExecutionGraph` — converts a multi-file candidate list into an `ExecutionGraph` (one `WorkUnitSpec` per file, dependency edges from import analysis where applicable).
  3. `dispatch_fanout(orch, serpent, ctx, graph) -> PhaseResult` — the parallel entry point. Submits graph to `SubagentScheduler`, awaits completion, merges unit results into a single PhaseResult with combined artifacts for downstream phases (VALIDATE, GATE, SLICE4B) to consume transparently.
- `memory/project_wave3_item6_graduation_matrix.md` — empty template for the 3-session graduation cadence, mirroring Wave 2 (5) matrix.

### Modified (surgical, single-hook each)

- `backend/core/ouroboros/governance/phase_dispatcher.py` — one new branch in `dispatch_pipeline` (or its GENERATE/APPLY hooks): when `parallel_dispatch_enabled()` AND `is_fanout_eligible(ctx)`, route through `parallel_dispatch.dispatch_fanout` instead of the serial per-file path. Zero change to the serial path; zero change to any terminal handling. Flag-gated short-circuit, same pattern as the `#8` dispatcher flip.
- `backend/core/ouroboros/governance/phase_runners/generate_runner.py` — candidate-list shape passes through to the new dispatch hook (no semantic change; may require threading `ctx.execution_graph` forward if we build the graph in GENERATE rather than a post-GENERATE seam).
- `backend/core/ouroboros/governance/phase_runners/slice4b_runner.py` — APPLY hook consumes per-unit results instead of iterating files serially when the fan-out path ran. Existing batch-rollback snapshot mechanism reused verbatim; no new rollback path.

### Tests (new)

- `tests/governance/test_parallel_dispatch_eligibility.py` — unit tests for `is_fanout_eligible` across posture / memory / op-shape matrix. ~25 cases.
- `tests/governance/test_parallel_dispatch_graph_build.py` — `build_execution_graph` deterministic conversion of candidate lists; dependency-edge correctness on import analysis. ~15 cases.
- `tests/governance/test_parallel_dispatch_integration.py` — end-to-end: serial-path vs fan-out-path produce equivalent final PhaseResult + commit shape under deterministic mock scheduler. ~20 cases (includes failure-propagation: 1-of-N unit fails → full-batch rollback matches serial semantics).
- `tests/governance/test_parallel_dispatch_invariants.py` — MemoryPressureGate clamp enforcement; posture weighting; Iron Gate pass-through (SemanticGuard fires per unit); worktree-orphan cleanup on exception propagation; concurrent-unit log isolation. ~20 cases.

## 4. Invariants (hard-pinned, grep-enforced in tests)

1. **MemoryPressureGate sovereignty.** Before any fan-out commits worktrees, `gate.can_fanout(n_requested)` is consulted. If `n_allowed < n_requested`, fan-out clamps to `n_allowed` (may be 1 — serial-equivalent). CRITICAL pressure → clamp to 1 regardless of eligibility. Test: mock `memory_pressure_gate.probe_pressure` to return CRITICAL, assert fan-out clamps to 1 or serial-path falls through.
2. **Posture weighting.** Base fan-out cap is multiplied by posture weight: HARDEN → 0.5× (tighten), MAINTAIN → 1.0×, CONSOLIDATE → 1.0×, EXPLORE → 1.5× (loosen). Multiplier floors at 1 unit (can't go below serial-equivalent). Test: parameterize posture, assert multiplied cap matches contract.
3. **Iron Gate per-unit preservation.** Each fanned unit runs through the full GATE phase independently. `SemanticGuardian` scans each unit's candidate; `ExplorationLedger` accumulates per unit; `MutationGate` decides per unit. No cross-unit short-circuits. Test: fan out 3 units, assert 3 SemanticGuard telemetry lines with distinct op-id sub-tags.
4. **Batch atomicity.** If unit k of N fails VERIFY, units 1..k-1 roll back to pre-apply snapshots AND unit k+1..N are cancelled before APPLY. Test: inject verify_regression on unit 2 of 3, assert units 1 + 3 rolled back / not applied.
5. **Worktree hygiene.** Every fanned unit's worktree is registered via `WorktreeManager.create()`. Exception propagation still runs `finally` cleanup + `reap_orphans` on next boot. Test: raise mid-execution, assert worktree dir removed OR reapable on next probe.
6. **Zero-authority on scheduler path.** `parallel_dispatch.py` imports NONE of: `orchestrator`, `policy`, `iron_gate`, `risk_tier`, `change_engine`, `candidate_generator`, `gate`. Grep-enforced in CI (pattern established across Wave 1 arcs). The scheduler composes existing primitives; it does not re-derive authority.
7. **Observability.** Each dispatched unit emits a distinct `[ParallelDispatch]` structured log line with `op_id / unit_id / file / worktree_branch / gate / memory_allowed / posture_weight`. Per-unit Iron Gate telemetry survives without collision via the op-id sub-tag pattern used by Slice 5 multi-file gen.
8. **§3 lifecycle physics.** The outer op's `--max-wall-seconds` cap, `--idle-timeout`, `--cost-cap` all still fire. A parallel arc cannot escape Ticket A1 termination physics. Test: arm a 30s wall-clock cap, start a fan-out that would take 60s serial, assert session terminates at ~30s with stop_reason=wall_clock_cap regardless of fan-out state.

## 5. Rollback env

- **Master flag:** `JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED` (default `false` throughout the entire arc's test + shadow + enforce phases, until graduation's 3 clean sessions meet the bar).
- **Sub-flags** (for staged rollout within the arc):
  - `JARVIS_WAVE3_PARALLEL_DISPATCH_SHADOW=true` — shadow mode: builds the graph + records telemetry but does NOT submit to scheduler (serial path still runs). Used to prove `is_fanout_eligible` decisions match expectations on live ops before any enforcement.
  - `JARVIS_WAVE3_PARALLEL_DISPATCH_ENFORCE=true` — enforce mode: eligible ops actually run through fan-out. Gated by master.
  - `JARVIS_WAVE3_PARALLEL_MAX_UNITS=N` (default 3, env-tunable) — hard ceiling on fan-out degree regardless of memory/posture caps.
- **Kill-switch contract:** `JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED=false` at any point reverts to the graduated #8 dispatcher serial path in a single env flip — no restart required, no state migration. Same rollback contract as every Wave 2 (5) flag.

## 6. Test matrix

| Category | Test file | Count | Notes |
|---|---|---|---|
| Eligibility decision | `test_parallel_dispatch_eligibility.py` | ~25 | Posture × memory × op-shape matrix; all pure Python, no I/O |
| Graph build | `test_parallel_dispatch_graph_build.py` | ~15 | Deterministic `WorkUnitSpec` construction, dependency-edge correctness |
| End-to-end parity | `test_parallel_dispatch_integration.py` | ~20 | Parallel vs serial path, commit shape equivalence, failure propagation |
| Invariant pins | `test_parallel_dispatch_invariants.py` | ~20 | MemoryPressureGate clamp, posture weight, Iron Gate pass-through, worktree hygiene, authority-import ban |
| Harness parity (`_run_both_paths`) | extends existing `test_phase_runner_parity.py` | ~10 | Assert fan-out off (default) = identical to Wave 2 (5) #8 baseline |
| **Total new tests** | | **~90** | |

**No mock of MemoryPressureGate beyond pressure-level injection** — the gate itself is Wave 1 #3 graduated code and runs for real in tests.

**No mock of WorktreeManager.create** — tests run against real tmp-path worktrees (same pattern as `test_worktree_isolation.py`).

## 7. Graduation cadence (same shape as Wave 2 (5))

Three live-fire battle-test sessions under the canonical recipe (`--headless --cost-cap 1.00 --idle-timeout 600 --max-wall-seconds 2400`) with the master flag ON + ENFORCE mode:

1. **S1** — baseline fan-out eligibility session. Seed a 3-file multi-file op via BacklogSensor. Verify 1× `[ParallelDispatch]` dispatch log + 3× unit telemetry + 3× per-unit SemanticGuard + merged commit with 3-file diff. Hard bar: zero `parallel_dispatch.py` frames in tracebacks, zero worktree orphans on next reap.
2. **S2** — posture-weighted throttle session. Operator sets posture to HARDEN before launch; verify fan-out cap clamps per §4 invariant #2.
3. **S3** — failure-propagation session. Inject a deliberate verify_regression on unit 2 of 3 via a test-injection env flag. Verify batch rollback of units 1+3 + POSTMORTEM on unit 2 + session still idle-timeout clean.

**Shadow phase before S1:** min 2 shadow-mode sessions proving `is_fanout_eligible` decisions align with expectations on real-ops workloads — no enforcement yet, just telemetry.

**Post-flip confirmation:** one session after the default flip, same discipline as Wave 2 (5) #8 (API probe → launch → verify dispatch markers + per-unit traces under natural default-true).

**Clean-bar criteria** (revised infra_waiver rules from Wave 2 (5) apply verbatim):
- `stop_reason ∈ {idle_timeout, budget_exhausted, wall_clock_cap}` — all acceptable harness-class stops.
- `session_outcome=complete` (Ticket B v1.1b field).
- 0 runner-attributed frames (`parallel_dispatch.py`, `phase_dispatcher.py`, `phase_runners/`, `worktree_manager.py` in any traceback = blocker).
- 0 worktree orphans after session + boot-time `reap_orphans` sweep.
- 0 JARVIS shutdown race.
- ≥1 `[ParallelDispatch]` dispatch log line in at least one of the 3 sessions (reachability).
- MemoryPressureGate consultation visible in logs for every fan-out attempt.

**Reachability profile:** eligibility requires multi-file ops. Natural reachability via RuntimeHealthSensor (single-file ops on requirements.txt per Wave 2 (5) cadence) will NOT trigger fan-out — need the forced-reachability harness (seed a 3-file trivial backlog op) for S1–S3. This is a known-pattern from #6 SLICE4B + #7 GENERATE cadences; budget accordingly.

## 8. Non-goals (explicit)

- **NOT expanding execution authority** — fan-out runs under the same §1 Zero-Trust contract as serial. Every unit passes through GATE.
- **NOT changing Iron Gate semantics** — SemanticGuardian, MutationGate, ExplorationLedger unchanged; each fires per unit.
- **NOT re-implementing worktree isolation** — reuses Wave 1 #3 graduated primitives.
- **NOT widening CHANGE authority** — `ChangeEngine` is still the only path to disk writes; each unit's APPLY calls it unchanged.
- **NOT adding new agentic reflex** — no new LLM calls beyond what the serial path already does. Fan-out is a scheduling layer, not a cognitive one.
- **NOT touching `--cost-cap` semantics** — total session cost cap still bounds total spend; fan-out does not multiply the budget.
- **NOT auto-starting Wave 3 (7) or Wave 2 (4)** — both remain separate authorizations per binding.

## 9. Slice structure (proposed; operator may reshape)

- **Slice 1** — `parallel_dispatch.py` module + `is_fanout_eligible` + unit tests (eligibility matrix). Default-off. No phase-dispatcher integration yet. Pure primitive ship.
- **Slice 2** — `build_execution_graph` + graph-build tests. Still default-off. Still no integration.
- **Slice 3** — shadow-mode wiring: `phase_dispatcher` calls `is_fanout_eligible` + emits telemetry when shadow flag is ON, but DOES NOT submit graph. Live-ops shadow for decision-correctness observation.
- **Slice 4** — enforce-mode wiring: `dispatch_fanout` actually submits. Master flag still OFF default; operator can flip locally for testing. Parity tests (`_run_both_paths`) pinned.
- **Slice 5** — 3-session graduation cadence + post-flip + default flip commit. Wave 3 (6) FINAL.

Each slice: own commit, own parity tests, no behavior change downstream of the flag default. Same discipline that closed Wave 2 (5).

## 10. Risk register

| Risk | Mitigation |
|---|---|
| Unit failure semantics leak between isolated worktrees | Worktree isolation tested in Wave 1 #3; tests §4 invariant #5 repeat the check under fan-out |
| Per-unit Iron Gate bypass via clever graph construction | §4 invariant #3: test 3× SemanticGuard lines for 3-unit fan-out; grep-enforced |
| Fan-out under CRITICAL memory DoSes host | §4 invariant #1: MemoryPressureGate clamp to 1 or fall through to serial; test against mocked CRITICAL pressure |
| Posture-override bypass on HARDEN | §4 invariant #2: posture weight mandatory per spec; test HARDEN → cap × 0.5 + floor at 1 |
| Wall-clock escape during fan-out | §4 invariant #8: Ticket A1 cap wraps the outer session; test with 30s cap + deliberately-slow fan-out |
| Parity test flakiness on async scheduler ordering | Scheduler is already deterministic (topological order by `ExecutionGraph.edges`); tests assert result equivalence, not ordering equivalence |
| Arc bleeds into `orchestrator.py` edits | §4 invariant #6: authority-import ban on `parallel_dispatch.py` grep-pinned; phase_dispatcher.py edit is single hook, not structural |

## 11. What operator is deciding with this doc

**Approval of THIS scope doc** = operator accepts:
- The 5-slice arc shape above.
- The file list (new + modified).
- The invariant set + test matrix bounds.
- The rollback flag contract (master-off + sub-flags for shadow/enforce).
- The graduation cadence shape (shadow → 3 enforce sessions → post-flip).

**Approval does NOT** = authorization to implement. That is a separate signal: `authorize Wave 3 (6) implementation` (or equivalent), which will be followed by Slice 1 as a standalone commit.

**Rejection or revision** is the operator's choice; this doc iterates until the contract is locked.

## Known debt (post-Slice-4, pre-Slice-5a)

- **SSE bridges deferred for Slices 3 + 4.** Per operator §8 "log + bus" parity directive, enforce + shadow events (`enforce_submit_start`, `enforce_completed`, `enforce_failed`, `enforce_submit_denied`, `enforce_timeout`, `shadow_graph_built`, `shadow_skipped`) currently emit only to the module logger, not to `StreamEventBroker`. Prior Wave 1 arcs (SensorGovernor, MemoryPressureGate) use `publish_<event>_event()` + `bridge_<module>_to_broker()` helpers that `GovernedLoopService.start` wires at boot. Wave 3 (6) should add the same pattern before FINAL — either in Slice 5a as part of observability parity, or as a named follow-up ticket after FINAL if graduation doesn't gate on it. Current slice work: log-only is acceptable for cadence observation (operators tail debug.log for `[ParallelDispatch]` markers), but the public IDE observability SSE stream will not surface fanout events until this lands.

## 12. Open questions for operator

1. **Graph-build placement:** build the `ExecutionGraph` inside `generate_runner` (seeing candidates) vs. post-GENERATE seam in `phase_dispatcher` (cleaner separation, one more threading step). Default proposal: post-GENERATE seam.
2. **Max-units default:** `JARVIS_WAVE3_PARALLEL_MAX_UNITS` — propose 3 (conservative, matches BackgroundAgentPool current worker count). Operator may prefer 2 (safer) or 4 (more aggressive).
3. **Posture weight table:** HARDEN 0.5× / MAINTAIN 1.0× / CONSOLIDATE 1.0× / EXPLORE 1.5× — tunable per your preference. Emergency-brake (posture confidence <0.3) → force serial regardless.
4. **Shadow-mode session count before S1 enforce:** minimum 2 proposed; operator may require more or less.
5. **Forced-reachability seed design:** do we seed the 3-file backlog op ourselves (agent-managed) or operator-authored test fixture? Wave 2 (5) precedent was agent-managed under explicit authorization per cadence.

---

**Standing:** awaiting operator `authorize Wave 3 (6) implementation` (or revision directive) before any code. Wave 3 (7) + Wave 2 (4) remain not-started per prior binding.
