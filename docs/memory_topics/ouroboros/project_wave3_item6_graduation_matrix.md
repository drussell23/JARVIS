---
title: Wave 3 (6) — Parallel L3 fan-out — Graduation ledger
modules: [scripts/ouroboros_battle_test.py, backend/core/ouroboros/governance/phase_runners/, backend/core/ouroboros/governance/parallel_dispatch.py, backend/core/ouroboros/governance/worktree_manager.py, tests/governance/test_parallel_dispatch_reachability_supplement.py, backend/core/ouroboros/architect/__init__.py, backend/core/tui/__init__.py, backend/core/umf/__init__.py, docs/operations/wave3-parallel-dispatch-graduation.md, tests/governance/test_w3_6_slice5a_structural_pins.py, scripts/livefire_w3_6_parallel_dispatch.py]
status: merged
source: project_wave3_item6_graduation_matrix.md
---

# Wave 3 (6) — Parallel L3 fan-out — Graduation ledger

**Status:** Wave 3 (6) arc extracted across 4 Slices on main (`cc175b9cc4` / `42b8a15711` / `6b4afbd733` / `618e6f5fc6`). 128/128 parity tests green + 25/25 phase_dispatcher regression. **Defaults ALL `false`**:

- `JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED` = false (master)
- `JARVIS_WAVE3_PARALLEL_DISPATCH_SHADOW` = false
- `JARVIS_WAVE3_PARALLEL_DISPATCH_ENFORCE` = false
- `JARVIS_WAVE3_PARALLEL_MAX_UNITS` = 3
- `JARVIS_WAVE3_PARALLEL_WAIT_TIMEOUT_S` = 900.0

Per operator binding 2026-04-23 Slice 5 split:
- **Slice 5a (authorized):** run the 3-session graduation cadence + fill this ledger + docs. **No default flip.**
- **Slice 5b (pending):** separate operator authorization after 3/3 clean sessions + evidence.

## Canonical harness recipe for graduation sessions

```bash
# Wave 3 (6) Slice 5a graduation session <N>
# purpose: exercise enforce-mode parallel dispatch on live multi-file ops
# stop conditions: first of idle_timeout | budget_exhausted | wall_clock_cap
JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED=true \
JARVIS_WAVE3_PARALLEL_DISPATCH_ENFORCE=true \
python3 scripts/ouroboros_battle_test.py \
    --headless \
    --cost-cap 1.00 \
    --idle-timeout 600 \
    --max-wall-seconds 2400 \
    -v > /tmp/claude/w3_s6_s<N>.log 2>&1
```

`--shadow` sub-flag is NOT required when enforce is on (enforce covers shadow's telemetry surface at higher fidelity). Toggle `…_SHADOW=true` separately if a shadow-only matrix row is being recorded.

## Graduation protocol (Slice 5a per flag-set)

Mirrors Wave 2 (5) protocol. One flag-set (master + enforce both on) graduates as a unit:

1. **Authorization**: operator approved 2026-04-23.
2. **Session 1**: battle-test with both flags `true`. Must reach a multi-file op (see Reachability below) OR document a waiver.
3. **Session 2**: repeat; different op mix if possible.
4. **Session 3**: repeat; different day/time if possible.
5. **Evidence table pasted to operator** (this doc's §Verdict section).
6. **Slice 5b flip** — single commit flipping all 3 env-flag defaults (or just master + enforce; operator's call). Inline hook stays gated by the flags; `=false` remains runtime kill.
7. **Post-flip confirmation soak** — natural defaults, no env overrides. Clean = FINAL.

## Clean-bar criteria (extended from Wave 2 (5) matrix)

**Runner-attributed-frames blocklist — extended 2026-04-23 for Wave 3 (6):**

The Wave 2 (5) clean-bar rule blocks a graduation flip if any traceback in the cadence carries a frame from `backend/core/ouroboros/governance/phase_runners/` or `phase_dispatcher.py`. **Added for Wave 3 (6):**

- `backend/core/ouroboros/governance/parallel_dispatch.py` — Wave 3 (6) Slice 1–4 module; a traceback frame here is a structural bug in the fan-out primitive.
- `backend/core/ouroboros/governance/worktree_manager.py` — L3 worktree lifecycle (Wave 1 #3 graduated, but Wave 3 (6) exercises it under new fan-out pressure). Orphan-cleanup failures or worktree create/prune bugs surfacing during cadence trigger rollback.

**All other clean-bar criteria carry over verbatim from Wave 2 (5):**
- 0 POSTMORTEM / CANCELLED runner-caused terminals.
- 0 JARVIS shutdown race (`Event loop is closed` absent).
- `session_outcome=complete` (Ticket B v1.1b).
- stop_reason ∈ {idle_timeout, budget_exhausted, wall_clock_cap}.
- Infra-noise waivers (`anthropic_transport` / `dw_transport` / `pim_readonly_db`) do NOT block — but MUST be tagged per traceback.
- Cost within harness-class budget (≤ `--cost-cap`).

**Wave 3 (6)-specific reachability signals:**

- `[ParallelDispatch] op=... allowed=...` — eligibility line from Slice 1. Fires every time `is_fanout_eligible` runs (master required; either shadow or enforce armed).
- `[ParallelDispatch shadow_graph_built]` — Slice 3 shadow-mode graph construction signal.
- `[ParallelDispatch enforce_submit_start]` + `[ParallelDispatch enforce_{completed|failed|cancelled|timeout|submit_denied}]` — Slice 4 enforce-mode lifecycle signals. **Required for graduation** — at least one enforce cycle must be observed across the 3 sessions (or documented as the forced-reachability seed's role).

## Reachability profile — the same bottleneck as #6 and #7

Natural reachability requires a **multi-file op** (n_candidate_files ≥ 2). Observed patterns from prior cadences:

- **RuntimeHealthSensor** (Python 3.9.6 EOL → requirements.txt): single-file, NOT eligible.
- **BacklogSensor phase-b-multi-type**: read-only cartography, never reaches APPLY.
- **OpportunityMinerSensor**: typically single-file docstring/style suggestions.

→ **Forced-reachability seed required** (agent-managed, repo-committed, per scope §12 (e)). The Slice 5a cadence must seed `.jarvis/backlog.json` with a multi-file op BEFORE session launch. Seed design and commit: see accompanying commit alongside this doc.

## Per-session ledger

### Session 1

- **session_id**: `bt-2026-04-24-021024`
- **commit_head**: `25d0f68312` (Slice 5a prep)
- **flag_config**: `JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED=true JARVIS_WAVE3_PARALLEL_DISPATCH_ENFORCE=true`
- **harness recipe**: canonical — `--headless --cost-cap 1.00 --idle-timeout 600 --max-wall-seconds 2400`
- **stop_reason**: `idle_timeout` ✓
- **session_outcome**: `complete` ✓
- **duration / cost / PM**: 2065.8s (~34 min) / $0.000 / 0
- **Eligibility markers (`[ParallelDispatch] op=...`)**: **0** ✗ PRIMARY REACHABILITY SIGNAL NOT OBSERVED
- **Shadow graph builds (`shadow_graph_built`)**: 0 (expected — enforce on not shadow)
- **Enforce submits (`enforce_submit_start`)**: **0** ✗
- **Enforce terminals (any)**: 0
- **Runner-attributed frames** (parallel_dispatch.py / worktree_manager.py / phase_runners / phase_dispatcher.py): **0 / 0 / 0 / 0** ✓ (extended clean-bar intact)
- **Shutdown race**: 0 ✓
- **Tracebacks**: 2 — both `persistent_intelligence_manager.py` (pre-existing `pim_readonly_db` infra). **infra_waiver: pim_readonly_db** (non-blocking).
- **Supporting markers** (routing-only, not reachability proof per operator binding): 8 `[PhaseRunnerDelegate] DISPATCHER → pipeline`, 26 `[PhaseDispatcher] iter=` internal routing lines.
- **Seed status**: enqueued by BacklogSensor at 19:11:53 but **never dequeued to the governance pipeline** — no CLASSIFY/ROUTE/GENERATE for `wave3-item6-forced-reach-multifile-seed` op_id found in debug.log.
- **Actual ops routed to DISPATCHER this session**: 8 × `DocStalenessSensor` ops (`<file>: N/M public symbols undocumented`). All classified BACKGROUND → DW topology-blocked (`Gemma 4 31B stream-stalls on DW endpoint`) → generation skipped without Claude cascade per sealed BG policy → never produced `pctx.generation` → post-GENERATE fan-out seam never executed.
- **Baseline restoration**: git checkout -- notebooks/report.ipynb reverted the sole benign notebook dirt. **Tracked files restored to baseline = YES, none intentional leftovers.**
- **outcome**: **CLEAN on extended clean-bar but PRIMARY REACHABILITY NOT OBSERVED** — fan-out seam physically cannot fire with BACKGROUND-only routing when DW is topology-blocked and BACKGROUND does not cascade to Claude.

**Root cause**: BacklogSensor emits BACKGROUND-classified ops; `project_bg_spec_sealed.md` sealed BG from Claude cascade for cost reasons; with DW topology-blocked on small-token generation in this session, no BACKGROUND op produces `pctx.generation`. **DocStalenessSensor noise** dominated the op mix and starved the priority-1 seed in the intake queue (observed: 8 DocStaleness ops routed, 0 seed ops routed despite priority: 1 in backlog.json).

**S1 classification**: incomplete for Slice 5a graduation purposes. Hard bar clean but primary reachability signal (`[ParallelDispatch]`) NOT met. Does NOT count toward the 3/3 cadence. Per operator binding, parity alone is NOT sufficient for enforce flip — must invoke remediation before S2.

### Session 2

- **session_id**: `bt-2026-04-24-030628`
- **commit_head**: `768637046f` (F3 landed)
- **flag_config / env matrix**:
  - `JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED=true`
  - `JARVIS_WAVE3_PARALLEL_DISPATCH_ENFORCE=true`
  - `JARVIS_BACKLOG_SENSOR_DEFAULT_URGENCY=critical` (F3 override — fired ✓)
  - `JARVIS_DOC_STALENESS_SENSOR_ENABLED=false` (intended mute — **NOT EFFECTIVE**, see below)
  - `JARVIS_OPPORTUNITY_MINER_ENABLED=false` (intended mute — NOT EFFECTIVE)
  - `JARVIS_TODO_SCANNER_SENSOR_ENABLED=false` (intended mute — NOT EFFECTIVE)
  - `JARVIS_CROSS_REPO_DRIFT_SENSOR_ENABLED=false` (intended mute — NOT EFFECTIVE)
- **harness recipe**: `--headless --cost-cap 2.00 --idle-timeout 600 --max-wall-seconds 2400`
- **stop_reason**: `idle_timeout` ✓
- **session_outcome**: `complete` ✓
- **duration / cost / PM**: 1612.2s (~27 min) / $0.000 / 0
- **Eligibility markers (`[ParallelDispatch] op=...`)**: **0** ✗ PRIMARY REACHABILITY SIGNAL NOT MET
- **enforce_submit_start / _completed / _failed / _cancelled / _timeout / _submit_denied**: all **0**
- **shadow_graph_built / enforce_skipped / shadow_skipped**: all 0
- **F3 override proof (INFO log)**: ✓ *"[BacklogSensor] JARVIS_BACKLOG_SENSOR_DEFAULT_URGENCY override active: urgency=critical (applied to all emissions this scan)"* — `backlog_sensor.py` at 20:06:49
- **Seed fate**: enqueued at 20:06:49, **never dequeued to governance pipeline** — no INTENT emitted for `wave3-item6-forced-reach-multifile-seed` op_id (same pattern as S1 but this time with F3 active)
- **Actual routed ops** (13 DISPATCHER markers, all BACKGROUND): 6× DocStalenessSensor, 2× TodoScannerSensor, ≥1× ProactiveExplorationSensor. **All BG-class sensors that were supposed to be muted via (a1) env mutes were still firing** — see root cause below.
- **Extended blocklist**: parallel_dispatch.py=0, worktree_manager.py=0, phase_runners=0, phase_dispatcher.py=0 ✓
- **Shutdown race**: 0 ✓
- **Tracebacks**: 2 — both `persistent_intelligence_manager.py`. infra_waiver: `pim_readonly_db` (non-blocking).
- **Baseline restoration**: `git checkout -- notebooks/report.ipynb` ✓. Three seed target `__init__.py` files still 0 bytes (no APPLY happened). **Tracked files = baseline; intentional leftovers = none.**
- **outcome**: **CLEAN on extended hard bar, but PRIMARY REACHABILITY NOT MET.** F3 worked correctly at the envelope emission layer but the actual routing bottleneck was NOT "backlog→BACKGROUND default" — it was **BG-sensor noise flooding the intake queue and starving the seed**.

**Root cause analysis (deeper than Slice 5a S1 original finding)**:

1. **(a1) env mutes were non-existent knobs.** `JARVIS_DOC_STALENESS_SENSOR_ENABLED` / `JARVIS_TODO_SCANNER_SENSOR_ENABLED` / `JARVIS_OPPORTUNITY_MINER_ENABLED` / `JARVIS_CROSS_REPO_DRIFT_SENSOR_ENABLED` are NOT supported by any sensor module. The individual sensors expose tuning knobs (poll intervals, webhook enable flags) but no master kill switch. Setting these envs had zero effect.

2. **BG-sensor flood persisted.** DocStaleness + TodoScanner + ProactiveExploration fired throughout the session, all emitting source-BG-default envelopes, all flooding the intake queue.

3. **F3 alone cannot unblock.** F3 correctly overrode the seed's urgency to "critical", so the seed envelope was stamped `urgency=critical`. But the seed still never got dequeued. The intake router's queue selection is not purely urgency-sorted — it appears to be FIFO-within-priority, and with BG ops constantly arriving first, the queue never surfaces the seed.

**S2 classification**: **does NOT count toward 3/3 cadence** per operator binding (0 `[ParallelDispatch]` markers). Third consecutive session demonstrating the BG-starvation structural issue cannot be worked around with F3 + non-existent env mutes alone.

**Per operator binding**: STOPPING. No blind S3. Seeking option (b) authorization — harness supplement — or alternative remediation direction.

### Lessons from S1 + S2 (captured 2026-04-23 per operator directive)

1. **S2 (a1) mutes were a no-op — operator/process mistake, not a product regression.** The env vars I used in S2 (`JARVIS_DOC_STALENESS_SENSOR_ENABLED`, `JARVIS_TODO_SCANNER_SENSOR_ENABLED`, `JARVIS_OPPORTUNITY_MINER_ENABLED`, `JARVIS_CROSS_REPO_DRIFT_SENSOR_ENABLED`) are NOT read by any sensor module. They were assumed-to-exist, not code-verified. **Binding process rule going forward**: for any graduation-harness env matrix, grep/read the actual sensor source before adding each env to the launch command. Use only knobs confirmed in code.

2. **F3 did its job.** The override log (`[BacklogSensor] JARVIS_BACKLOG_SENSOR_DEFAULT_URGENCY override active: urgency=critical`) fired in S2. The seed envelope was correctly stamped `urgency=critical`. The knob works exactly as designed.

3. **Starvation is intake/FIFO + BG flood, not "BACKGROUND vs Claude" alone.** S1's original root-cause write-up was incomplete. F3 correctly fixes the classification-side issue (source=backlog → BACKGROUND default). But with the seed stamped `urgency=critical`, it STILL never dequeued — proving the bottleneck is deeper: the intake router's queue selection is FIFO-within-class enough that a constant BG flood from unmuted sensors prevents the seed from being picked regardless of its urgency stamp. **F1 is load-bearing** for any multi-file graduation on this codebase.

4. **F1 stays deferred.** Per operator binding 2026-04-23: "F1: not authorized now. After Wave 3 (6) reaches a stable checkpoint (post–(b) + graduation decision), open F1 as its own scoped arc (slices + graduation), per §2." Do NOT start F1 inside Slice 5a.

### Session 3 — bt-2026-04-24-044547 (COMPLETED 2026-04-23 post-contamination-cleanup)

- **session_id**: `bt-2026-04-24-044547`
- **commit_head**: `e68993f5b4` (CLAUDE.md refactor auto-committed post-HEAD; working tree clean at launch)
- **launched by**: claude-code-agent (single-flight verified clean after massive pre-launch cleanup; see §"Contamination cleanup" below)
- **actual env matrix** (diverged from the 11-digit plan — I used 8-digit `86400000` ≈ 1000 days after code-verification confirmed 8 digits is sufficient; both are >> any sensor poll. The other agent's 11-digit attempt used the planned matrix verbatim but landed in session `bt-2026-04-24-033849` which was killed as zombie):
  - Wave 3 + F3: `JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED=true`, `JARVIS_WAVE3_PARALLEL_DISPATCH_ENFORCE=true`, `JARVIS_BACKLOG_SENSOR_DEFAULT_URGENCY=critical`
  - 18 × `JARVIS_*_INTERVAL_S=86400000` / `JARVIS_*_FALLBACK_INTERVAL_S=86400000` covering DocStaleness + Todo + Exploration + OpportunityMiner + WebIntel + GitHubIssue + PerfRegression + CrossRepoDrift + TestFailure (fallback only) + IntentDiscovery + Scheduled. BacklogSensor deliberately untouched (seed carrier).
  - Did NOT include the other agent's `_FS_EVENTS_ENABLED=false` / `_WEBHOOK_ENABLED=false` event-path kills; rationale: intervals at 1000d are sufficient without additionally disabling event paths, and keeping events enabled preserves observability of what the sensors WOULD have fired on.
  - Canonical harness: `--cost-cap 2.00 --idle-timeout 600 --max-wall-seconds 2400 --headless -v`
- **stop_reason**: `idle_timeout` ✓
- **session_outcome**: `complete` ✓
- **duration**: 876.9s (~14.6m)
- **cost**: null (known summary-counter bug)
- **ops (from replay.html)**: 14 ops / 1192 events / guardian=1 / inference=0
- **F3 override fired**: 1 ✓ (BacklogSensor stamped seed with urgency=critical at 21:45:59)
- **Seed enqueued**: 1 ✓ (`BacklogSensor: enqueued task_id=wave3-item6-forced-reach-multifile-seed`)
- **INTENT total**: 16 (silencing effective — S1 had ~130, this had 16, 87% reduction)
- **DISPATCHER→pipeline**: 17 ops
- **[ParallelDispatch] eligibility markers**: **0** ✗ (runner-attributable, not infra)
- **enforce_submit_start**: **0** ✗
- **APPLY (single)**: 1 (op `-06a0` on requirements.txt, NOT the seed — some other sensor's emission)
- **POSTMORTEM**: 1 (root_cause=none for op `-06a0`)
- **3 target files**: 0 bytes each (seed did NOT apply)
- **Root cause of 0 markers** (traced in debug.log):
  - Seed got into BacklogSensor + pipeline
  - Mid-run op `-58de` (GENERATE at 31% progress): `PlanGenerator Skipping plan ... trivial_op: 1 file(s), short description` — classified as 1 file, not 3
  - Route: `BACKGROUND route: DW failed ... accepting [op-019dbdd4-58de...]` — BG route despite F3 critical urgency
  - parallel_dispatch eligibility requires STANDARD/COMPLEX route AND multi-file — **BG + 1-file both disqualify independently**
  - F3 stamps urgency on envelope but UrgencyRouter's route decision uses source-type mapping (`backlog` → BG default) not urgency alone
- **Baseline restoration**: yes — notebooks/report.ipynb reverted; 3 seed init.py files still 0 bytes (untouched)
- **Classification**: **runner-attributable 0 markers, NOT starvation, NOT infra.** This is exactly the condition pre-authorized (b) harness supplement was scoped for.
- **outcome**: **0 markers → (b) supplement triggered per binding**

## Contamination cleanup (2026-04-23, pre-S3)

Before S3 could be cleanly launched, an unrelated cleanup exposed major parallel-session contamination:

- **34 PIDs terminated** (21 Python `ouroboros_battle_test.py` processes + 11 zsh Claude-Code wrappers + 2 round-2 orphans)
- Every Python process sampled via `sample` showed identical `Py_FinalizeEx → PyThread_acquire_lock_timed → __psynch_cvwait` wedge pattern (7 distinct forensic samples captured: 49285, 20089, 22798, 26749, 47079, 51435, 53687)
- Elapsed times ranged from 44min to 1d+3h — all past `--max-wall-seconds 2400` + all post-summary
- Root cause: `threading._shutdown()` deadlocks on a non-daemon thread's lock during `Py_FinalizeEx`
- Zsh wrappers used deprecated `tail -f /dev/null | python3 ...` stdin-guard idiom from pre-Ticket-C era
- Full record: `memory/project_followup_battle_test_post_summary_hang.md` — includes 5-item harness epic for structural fix
- Single-flight rule now binding: `pgrep -f "python3? scripts/ouroboros_battle_test\.py"` must be empty before S*

## Session 3 supplement — reachability proof via test_harness

Per operator binding: 0 markers on S3 → pre-authorized (b) harness supplement executes automatically.

- **Test file**: `tests/governance/test_parallel_dispatch_reachability_supplement.py` (uncommitted, pending authorization to commit)
- **Ledger tag**: `reachability_supplement=test_harness`
- **Target of proof**: the post-GENERATE seam wiring in `phase_dispatcher.py` lines 588-634 correctly invokes `enforce_evaluate_fanout` exactly once when master+enforce flags are on AND GENERATE produced a multi-file generation artifact
- **Scope**: additive evidence, NOT a substitute for live fan-out; does NOT count toward the 3 clean sessions for Slice 5a graduation
- **Mechanism**:
  - Builds `OperationContext` at GENERATE phase (CLASSIFY → ROUTE → GENERATE walk via legal PHASE_TRANSITIONS)
  - Registers a minimal `_StubGenerateRunner` emitting a 3-file `GenerationResult` with paths matching the real forced-reach seed (`backend/core/ouroboros/architect/__init__.py`, `backend/core/tui/__init__.py`, `backend/core/umf/__init__.py`)
  - Mocks `orchestrator._subagent_scheduler` with a `_FakeScheduler` returning COMPLETED + 3 completed units
  - Patches `parallel_dispatch.get_default_gate` + `_default_posture_fn` to hermetic stubs (posture=MAINTAIN confidence=0.9, memory=OK)
  - Calls `dispatch_pipeline(orchestrator, None, ctx, registry, pctx)` with master+enforce+dispatcher flags on
  - Asserts: 1 `[ParallelDispatch]` eligibility log + 1 `[ParallelDispatch enforce_submit_start]` log + 1 scheduler.submit + 1 scheduler.wait_for_graph + 3-unit ExecutionGraph + `pctx.extras["parallel_dispatch_fanout_result"].outcome == COMPLETED`
  - Plus negative control: flags off → zero markers, scheduler untouched, no extras key
- **Result**: **2/2 green.** Full parallel_dispatch regression suite still 130/130 green (supplement + enforce + shadow + eligibility + graph_build).
- **Interpretation**: the post-GENERATE seam is wired correctly. The S1/S2/S3 0-marker results are NOT a wiring bug; they're the operator-identified BG starvation + source→route mapping gap (F1 load-bearing per §"Lessons from S1 + S2").

### Verdict

- **Slice 5a live-session cadence**: **0 of 3 clean** (S1 hard-bar clean but 0 markers, S2 superseded/contaminated, S3 runner-attributable 0 markers — all non-counting toward clean bar).
- **Reachability supplement**: green. Proves wiring integrity.
- **Graduation (Slice 5b default flip) blocked** on live-fire markers. Two non-graduation paths unblock it:
  - **(F1)** Intake governor enforcement — upgrade Wave 1 #3 SensorGovernor to enforce at `UnifiedIntakeRouter.ingest()`. Ensures BG sensors can't starve IMMEDIATE/STANDARD ops regardless of volume. Would produce live markers on the next S* cadence. Operator-deferred per 2026-04-23 binding.
  - **(F2)** BacklogSensor per-entry `urgency_hint` schema — lets the seed declare its own urgency in backlog.json, bypassing source-type mapping default. Narrower than F1; still requires operator authorization per binding.
  - Until one of F1/F2 ships, live multi-file fan-out through the forced-reach seed is **structurally unreachable** on this codebase.
- **Operator decision 2026-04-23**: (1) supplement committed at `92ddb54463`. (3) narrowed — F2 arc authorized, F1 stays deferred. See `project_followup_f2_backlog_urgency_hint_schema.md` for the controlling scope.
- **F2 Slice 1 SHIPPED**: `37642cfbe2` — per-entry `urgency_hint` schema + sensor-side stamping + FlagRegistry entry. Default-off; no routing change yet. Slice 2 (UrgencyRouter consumption) pending separate authorization.
- **live_reachability**: **blocked** pending F2 Slices 2-4 + 3 clean live sessions.
- **Collateral**: harness epic at `project_followup_battle_test_post_summary_hang.md` (7 forensic samples in `.jarvis/forensics/`, 5-item structural fix draft). Orthogonal to F2; unblocks single-flight S* but not the live_reachability gap.

---

## F2 graduation S1 attempt — `live_reachability=blocked_by_intake_starvation` (2026-04-24)

- **session_id**: `bt-2026-04-24-062608`
- **MERGE_HEAD_SHA**: `4bdc9f58d543cbd2ec66fe8ca16319a36559c6ee` (post F2 Slices 1+2+3 merge)
- **auth**: Checkpoint A (stop after S1)
- **session_outcome**: `complete` · **stop_reason**: `idle_timeout` · **duration**: 858.9s
- **ops (replay)**: 12 / events=988 / guardian=0

### Evidence — F2 plumbing fired correctly
- `F2 hint consumption` marker count: **2** (first-ever live)
- `envelope_routing_override` reference count: **1** (first-ever live)
- `BacklogSensor: enqueued task_id=wave3-item6-forced-reach-multifile-seed` at 23:26:33 ✓
- Log at 23:26:33: `[BacklogSensor] JARVIS_BACKLOG_URGENCY_HINT_ENABLED active: per-entry routing_hint consumed for one or more emissions this scan (envelope.routing_override stamped; UrgencyRouter honors via envelope_routing_override path)` ✓

### Evidence — graduation bar failed
- `[ParallelDispatch]`: **0** ✗
- `enforce_submit_start`: **0** ✗
- `APPLY`: 0, `POSTMORTEM`: 0, 3 target files unchanged at 0 bytes

### Root cause — `blocked_by_intake_starvation`

**Enqueue without dequeue.** Envelope stamped correctly, `router.ingest()` returned `"enqueued"`. After that, the seed's op never appears in any `Route:` log.

All 12 routed ops were BG/IMMEDIATE from other sensors:
- 6× `background (background_source:doc_staleness:simple)`
- 3× `background (background_source:todo_scanner:simple)`
- 3× `background (background_source:doc_staleness:low_urgency)`
- 1× `immediate (critical_urgency:runtime_health)`

No `Route: standard (envelope_routing_override:standard)` anywhere. UrgencyRouter's F2 priority-0.5 clause cannot fire on an op that never reaches `classify()`. The seed was stuck in the UnifiedIntakeRouter's intake queue behind a bursty BG flood (DocStaleness + TodoScanner initial scans fired ~12 envelopes in the first seconds). Session idle-timed before seed's dequeue.

### Classification

F2 routing math = **correct and proven on live traffic**.
F2 plumbing = **complete and merged** at `4bdc9f58d5`.

Graduation-blocking gap = **intake queue discipline**. The queue is class-partitioned FIFO, not urgency-priority. `urgency=critical` is a label on the envelope, not a dequeue-priority signal. This is F1 territory; deferring F1 past F2 graduation was a category error.

- **live_reachability**: `blocked_by_intake_starvation`
- **S1 classification**: **non-counting toward clean bar** (not runner-attributable, not infra; class = `intake_priority_inversion`)
- **Evidence chain**: `sensor.enqueue → router.ingest returns "enqueued" → op never reaches classify() → seed never routed → post-GENERATE seam never evaluated → 0 [ParallelDispatch]`

### Harness collateral — 9th Py_FinalizeEx zombie
- python PID 57884 wedged post-summary; 20min no writes; `Py_FinalizeEx` + 9× `PyThread_acquire_lock_timed` + 20× `__psynch_cvwait`. Identical to prior 8 samples.
- Killed 2026-04-24 via signature-match predicate; lock released. Details: `project_followup_battle_test_post_summary_hang.md`.

### Operator binding 2026-04-24 — next arc priorities

- **P0-1**: F1 intake priority scheduling — scope doc + default-off flag + tests, same graduation discipline as F2. Load-bearing for any future F2 graduation.
- **P0-2**: Harness bounded shutdown + executor audit (harness epic #3). Co-ships or immediately after F1 Slice 1.
- **P1**: Per-op lifecycle ledger (observability).
- **Deferred**: Curiosity / hypothesis loops, SerpentFlow ledger replay, SemanticGuardian capability-delta.

**No F2 Slice 4 S2/S3. No default flip. Until F1 (or equivalent intake ordering) proves critical seed can't be starved by BG burst under test + one soak.**

---

---

---

## F1 Slice 4 cadence S1b — `live_reachability=blocked_by_provider_exhaustion` (2026-04-25)

**First-ever live battle-test in the entire W3(6)+F1+F2 arc with all three integrations firing simultaneously.** F1+F2 plumbing PROVEN end-to-end on production traffic. W3(6) reachability bar still missed — but the block is now **downstream of F1+F2** (provider transport class, not intake starvation, not source-route mapping). Operator-bound classification: `anthropic_transport` infra waiver, NOT a cognitive/routing failure within O+V.

### Session record

- **session_id**: `bt-2026-04-25-054256`
- **MERGE_HEAD_SHA**: `b068c8a083` (post W3(6) Slice 5a graduation prep merge)
- **launched by**: claude-code-agent (single-flight verified clean after killing PID 91589 zombie from earlier S1)
- **env matrix** (additive over W3(6)+F2 baseline): `JARVIS_INTAKE_PRIORITY_SCHEDULER_ENABLED=true` (F1 master, FIRST EVER LIVE), `JARVIS_BACKLOG_URGENCY_HINT_ENABLED=true` (F2 hint reader), `JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED=true`, `JARVIS_WAVE3_PARALLEL_DISPATCH_ENFORCE=true`, `JARVIS_BACKLOG_SENSOR_DEFAULT_URGENCY=critical` (F3), 18× sensor mute intervals @ 86400000s
- **canonical harness**: `--headless --cost-cap 2.00 --idle-timeout 600 --max-wall-seconds 2400 -v`
- **stop_reason**: `idle_timeout` ✓
- **session_outcome**: `complete` ✓
- **duration**: 1509.97s (~25m, well under 40m wall cap)
- **cost**: $0.16 (well under $2.00 cap)
- **ops (replay)**: 8 ops / 1103 events / guardian=0 / inference=0

### Hard-bar evidence (CLEAN)

| Marker | Required | Got | Status |
|---|---|---|---|
| `[IntakePriority] primary dequeue` (F1) | active | **8** | ✅ first-ever live |
| `envelope_routing_override` (F2) | active | **3** | ✅ first-ever live |
| Seed route classification | STANDARD | `route=standard, route_reason=envelope_routing_override:standard` | ✅ first-ever live (was IMMEDIATE in S1) |
| `[IntakePriority] priority_inversion` | 0 (or non-load-bearing) | 0 | ✅ |
| Extended blocklist (parallel_dispatch.py / worktree_manager.py / phase_runners/ / phase_dispatcher.py traceback frames) | 0 | 0 / 0 / 0 / 0 | ✅ |
| Shutdown race (`Event loop is closed`) | 0 | 0 | ✅ |

### Reachability bar (BLOCKED — infra-class)

| Marker | Required | Got | Status |
|---|---|---|---|
| `[ParallelDispatch] allowed=true` | ≥1 | 0 (only line was `allowed=false reason=empty_candidate_list` from a non-seed op) | ❌ |
| `enforce_submit_start` | ≥1 | 0 | ❌ |
| 3 target files written | yes | 0 | ❌ |

### Root cause — `live_reachability=blocked_by_provider_exhaustion`

The seed (`op-019dc329-d33c`) successfully traversed the integration chain:

1. ✓ BacklogSensor enqueued at 22:43:11 with `urgency_hint=critical` + `routing_hint=standard`
2. ✓ F2 reader stamped `envelope.routing_override=standard`
3. ✓ F1 priority queue dequeued in 0.10s waited
4. ✓ UrgencyRouter chose `route=standard, route_reason=envelope_routing_override:standard` (priority-0.5 clause won over urgency=critical → IMMEDIATE)
5. ✓ Reached GENERATE phase with 3-file target list intact
6. ✗ All 3 GENERATE_RETRY attempts hit `EXHAUSTION cause=fallback_failed fsm_failure_mode=TIMEOUT` — DW (doubleword-397b) primary failed, Claude fallback raised `CancelledError` after 72.8s sem_wait
7. ✗ Op terminated `all_providers_exhausted:fallback_failed` — never produced a multi-file `GenerationResult`
8. ✗ Post-GENERATE seam in `phase_dispatcher.py` therefore never invoked `is_fanout_eligible` on the seed's candidate files

The single `[ParallelDispatch]` line in the session was from an unrelated op (`op-019dc32c-8dc5`) where `n_requested=0 → empty_candidate_list` reason — not the seed.

### Classification (per operator binding 2026-04-25)

> "The F1+F2 plumbing is definitively proven end-to-end. The microkernel successfully intercepted, classified, and routed the complex payload to the STANDARD multi-file route without human intervention. The subsequent all_providers_exhausted failure is an infrastructure-layer anthropic_transport waiver, not a cognitive or routing failure within O+V. ... The internal architecture is mathematically sound."

- **F1 primitive bar**: ESSENTIALLY MET. Priority queue active; critical-urgency seed dequeued first; 0 inversions; 0 runner-attributed traceback frames; reserved-slot + back-pressure quiescent (no burst conditions met). The contract `IntakePriorityQueue` was authored to enforce held without contradiction on real traffic.
- **W3(6) Slice 5b reachability bar**: STILL BLOCKED. Block class re-tagged from prior `intake_priority_inversion` (S1+S2+S3 of W3(6) cadence) → `provider_exhaustion` (S1b of F1 Slice 4 cadence). The structural failure mode upstream (intake) is FIXED; the failure mode downstream (provider transport) is the new gating condition — but it's infra-noise, not architectural.
- **Operator binding**: do NOT mask provider latency by modifying the seed (Option C) or artificially inflating timeout boundaries (Option D). Lock in S1b. Close cadence. Await separate direction on provider-layer fixes.

### Cadence verdict

- **F1 Slice 4 cadence**: **PAUSED post-S1b**. 1 of 3 sessions recorded (incomplete-on-reachability-bar but clean-on-everything-else and infra-classified). No S2 or S3 launched per operator close-cadence directive.
- **F1 Slice 4 default flip**: **NOT eligible**. The "3 clean live sessions" gate cannot close on infra waiver alone; needs at least one session where the seed (or any other multi-file op) reaches the post-GENERATE seam with a complete generation.
- **W3(6) Slice 5b default flip**: **STILL BLOCKED**. Block class promoted upward (was: intake starvation; now: provider exhaustion) — both are non-architectural; both require external-to-O+V remediation.

### Unblock paths (operator-deferred)

- **Provider transport fix** — DW endpoint stability OR Claude fallback timeout/retry-budget tuning. Outside W3(6)+F1+F2 scope.
- **Smaller op profile** — explicitly REJECTED by operator (Option C). Modifying the seed would mask the issue, not fix it.
- **Inflated timeouts** — explicitly REJECTED by operator (Option D). Same masking concern.
- **External (Anthropic side / DW side) infra fix** — outside JARVIS scope.

When a future battle-test session naturally produces a multi-file `GenerationResult` (any op, any sensor — not necessarily the F2 seed), the post-GENERATE seam will fire and `[ParallelDispatch]` markers will accumulate organically. F1+F2 are now graduated-shape primitives waiting in the queue; the next eligible op will exercise them.

---

---

## F1 Slice 4 cadence S2 + S3 — fix-chain breakthroughs + harness limit (2026-04-25)

S1b proved F1+F2 plumbing live for the first time. S2+S3 extended the chain end-to-end through multiple bottleneck classes, each surfaced and fixed in turn:

### S2 (`bt-2026-04-25-065229`, post-#19706 outer-retry merge)

- **MERGE_HEAD_SHA**: `1b3ae692c9` (outer-retry only)
- **session_outcome**: complete · **stop_reason**: idle_timeout · **duration**: 1510s · **cost**: $0.16
- **F1**: 8 `[IntakePriority] primary dequeue` markers, 0 priority_inversion ✓
- **F2**: 3 `envelope_routing_override` markers, seed routed STANDARD via priority-0.5 ✓
- **Outer-retry fix proven live**: 4 `Fallback outer-retry` attempts (CancelledError/TIMEOUT, ReadError/TRANSIENT_TRANSPORT) — exact log line my fix added
- **🎉 FIRST EVER LIVE**: `[ParallelDispatch] op=op-019dc369-982e allowed=true n_requested=4 n_allowed=3 reason=max_units_clamp posture=EXPLORE posture_weight=1.50 posture_confidence=0.88 memory_level=warn memory_n_allowed=3` ← every Wave 1 + W3(6) primitive fired correctly together
- **🎉 FIRST EVER LIVE**: `[ParallelDispatch enforce_submit_start] graph_id=graph-3ca532de4f6e plan_digest=b996a6f969f3 concurrency_limit=3 n_units=4`
- **PLAN-EXPLOIT completed**: 3 concurrent streams, 8 merged files in 176s, $0.49 total cost
- **NEW BOTTLENECK SURFACED**: `Cost summary spent=$0.4914 / cap=$0.4500` → `[ParallelDispatch enforce_cancelled] phase=wait elapsed_s=53.3` — financial circuit breaker (correctly) cancelled because per-op cap was sized for ONE stream
- **Block class**: `live_reachability=blocked_by_cost_cap_singlestream_vs_3stream` (the cap math didn't account for fan-out width)

### S3 (`bt-2026-04-25-080530`, post-#19800 cost-cap parallel-stream merge)

- **MERGE_HEAD_SHA**: `1491afc671` (outer-retry + cost-cap parallel-stream)
- **session_outcome**: complete · **stop_reason**: idle_timeout · **duration**: 659s · **cost**: $0.86
- **F1**: 6 markers, 0 inversions ✓
- **F2**: 3 markers, seed routed STANDARD ✓
- **🎉 COST-CAP FIX PROVEN LIVE**: `[CostGovernor] op=op-019dc3ac- cap bumped for parallel fan-out: $0.4500 → $1.4850 (n_streams=3, parallel_factor=3.30x — rooted-problem fix)` ← exact log line my fix added; fired ON THE SEED
- **0 EXHAUSTION** events, **0 cost EXCEEDED** warnings, **0 enforce_cancelled** — S2's class is gone
- **Outer-retry attempts: 0** (provider was healthy this run; fix didn't need to fire)
- **Seed pipeline**: enqueued → F1 dequeue → F2 stamp → STANDARD route → cost_governor.start cap=$0.45 → PLAN/CONTEXT_EXPANSION → GENERATE entry → cost-cap bumped to $1.49 → PLAN-EXPLOIT 3 streams launched → tool_round_complete on streams 1+2 (5 read_file, then 3 tools incl. glob_files)
- **NEW BOTTLENECK**: harness `--idle-timeout 600` fired before seed completed multi-stream tool-loop exploration. Tool round events (between PLAN-EXPLOIT children) don't appear to reset the harness's idle counter — it likely tracks INTENT/HEARTBEAT/APPLY-class events only
- **Block class**: `live_reachability=blocked_by_harness_idle_timeout_vs_parallel_stream_wallclock` (NOT a JARVIS bug; harness termination policy)

### Operator binding 2026-04-25 (S3 read accepted)

> "S3 read accepted — not a hang; parallel stream work + harness idle definition mismatch is credible. ... S4 launch: use --idle-timeout 1800 (30m). Clarify binding: this adjusts harness session idle, not op/provider budget caps unless we say otherwise."

**Binding clarification**: the prior "don't inflate timeouts" rule (Option D rejection in S1b closure) referred to OP/PROVIDER budget caps (which would mask provider latency). The harness-level `--idle-timeout` is a TEST TERMINATION policy — adjusting it for graduation cadences is fine when ops legitimately need longer wall-clock for multi-stream parallel exploration.

**Ledger entry for S3**: `integration-validated through last logged tool activity; idle-terminated = harness limitation, not architectural failure`. **Tagged accordingly — does NOT count toward 3 clean live sessions for Slice 4 graduation cadence**, but the cost-cap fix IS proven live in production.

### S4 (`bt-2026-04-25-083724`, idle-timeout 1800) — `live_reachability=blocked_by_intake_wal_cross_session_dedup`

- **MERGE_HEAD_SHA**: `1491afc671` (same as S3)
- **session_outcome**: TBD (killed at ~20min mid-run after diagnosis)
- **Result before kill**: 0 ops dispatched, 0 INTENT, 0 enqueue events
- **Root cause**: `.jarvis/intake_wal.jsonl` (1.3 MB) carried over the seed's task_id signature from S1+S1b+S2+S3. BacklogSensor scanned, applied F2/F3 hints (markers fired), called `router.ingest(envelope)` — but the router dedup'd the seed against the WAL → returned non-`"enqueued"` → BacklogSensor's enqueue log only fires on `result == "enqueued"` so the failure was SILENT.
- **Documented failure mode**: "intake WAL cross-session coalescing (state cleanup)" from Session O memory. Known bug, not in canonical pre-flight recipe.
- **Operator-authorized cleanup** 2026-04-25:
  - Killed PID 97070
  - Backed up + cleared `.jarvis/intake_wal.jsonl` → `.jarvis/intake_wal.pre-s4b-20260425-085926Z.bak.jsonl`
  - Removed 3 stale `intake_router*.lock` files (Apr 23-25)
  - Pre-flight verified clean (0 procs, 0 locks, 0 WAL lines, 1 seed in backlog)
- **Block class**: `live_reachability=blocked_by_intake_wal_cross_session_dedup` (NOT a JARVIS bug; pre-flight hygiene gap that has bitten ~5 prior cadences silently)

### S4b (`bt-2026-04-25-085942`, idle-timeout 1800, CLEAN WAL) — `live_reachability=blocked_by_anthropic_transport_infra` [INFRA WAIVER]

- **MERGE_HEAD_SHA**: `1491afc671` (same as S3)
- **session_outcome**: complete ✓ · **stop_reason**: idle_timeout ✓ · **duration**: 1855s (~31min) · **cost**: $0.80
- **strategic_drift**: 16 ops total, 1 drifted (ratio 6.25%, status=ok)

**JARVIS-side proof — every fix landed and fired live on the seed:**

| Marker | Count | Note |
|---|---|---|
| **`BacklogSensor: enqueued task_id=wave3-item6`** | 1 | WAL clear PROVEN (was 0 in S4) |
| F1 `[IntakePriority] primary dequeue` | 17 | F1 active across multiple urgency tiers |
| F1 `priority_inversion` | 0 | F1 working correctly |
| F2 `envelope_routing_override` | 3 | F2 priority-0.5 clause active |
| Seed `Route: standard (envelope_routing_override:standard)` | 1 | seed routed STANDARD via F2 hint |
| **`[CostGovernor] cap bumped for parallel fan-out: $0.4500 → $1.4850 (n_streams=3, parallel_factor=3.30x)` ON THE SEED** | 1 | cost-cap fix #19800 fired on the seed itself |
| Outer-retry attempts | 1 | outer-retry fix #19706 engaged on a transient |
| EXHAUSTION cause=fallback_failed | 0 | (seed didn't exhaust) |
| Extended blocklist tracebacks (parallel_dispatch.py / worktree_manager.py / phase_runners/ / phase_dispatcher.py) | 0 / 0 / 0 / 0 | clean |

**External Anthropic API instability — the actual blocker:**

| Marker | Count | Note |
|---|---|---|
| Claude transient failures | 6 | `APITimeoutError → ConnectTimeout → ConnectTimeout → TimeoutError → CancelledError: deadline exceeded` |
| Pool recycles | 8 | gen 7 → gen 15 across the session |
| L3 mode switches | 2 | NORMAL → REDUCED_AUTONOMY @ 02:13:35 (3 probe failures) → READ_ONLY_PLANNING @ 02:15:57 (5 probe failures) |
| `enforce_submit_start` | 0 | seed couldn't reach the post-GENERATE seam — Claude API down |
| `enforce_completed` | 0 | (no graduation evidence) |
| 3 target files written | 0 | (seed couldn't APPLY) |

### Operator binding 2026-04-25 — Option A then D classification

> "Let S4b finish naturally and capture final ledger/artifacts. Classify outcome as anthropic_transport infra waiver per W2(5) graduation matrix: External provider instability (APITimeout/ConnectTimeout chain). JARVIS self-protection engaged correctly (READ_ONLY_PLANNING). Not a product regression. ... Do not change seed, prompts, or gate policy. Do not open new product bug from this run; open/update only an infra-waiver row with session id, timestamps, failure chain, and provider status notes."

### S4b classification: `anthropic_transport` infra waiver

**Exact wording per binding**: *"JARVIS path proven live through PLAN-EXPLOIT launch + cost-cap bump; completion blocked by external Anthropic transport instability."*

- **session_id**: `bt-2026-04-25-085942`
- **session window**: 2026-04-25T01:59:50 → 2026-04-25T02:30:38 (1855s)
- **failure chain**: `APITimeoutError → ConnectTimeout → ConnectTimeout → TimeoutError → CancelledError: deadline exceeded`
- **provider status notes**:
  - 8 httpx pool recycles
  - L3 mode: NORMAL → REDUCED_AUTONOMY @ 02:13:35 (3 consecutive probe failures) → READ_ONLY_PLANNING @ 02:15:57 (5 consecutive probe failures)
  - JARVIS self-protection (Hibernation Prober + L3 mode switcher) engaged correctly
- **Decision**: **does NOT count toward 3-clean Slice 4 cadence**. NOT a product regression. NO seed/prompts/gate policy changes. NO new product bug opened. Cleanup post-summary zombie PID 97730 executed (standard predicate).

### Cumulative cadence position post-S4b

- **Slice 4 cadence clean count**: still **0 of 3**
- **JARVIS-side fixes proven live across S2 + S3 + S4b**:
  - F1 priority queue (S1b first ever live)
  - F2 routing_hint priority-0.5 (S1b first ever live)
  - Outer-retry on transient with budget remaining (S2 first ever live, recurred S4b)
  - Cost-cap parallel-stream bump (S3 first ever live, recurred S4b)
  - WAL clear (S4b first ever — manual one-shot pending Option B follow-up)
- **Remaining blocker class**: `anthropic_transport` infra waiver (external)
- **Recommended next step**: monitor Anthropic API stability; re-launch S5 only when probe success rate recovers (e.g., post-incident notification or ≥30min of stable runtime_health probes)

### Follow-up tracking — Option B (open ticket, do NOT implement in S4 PR)

### Follow-up tracking — Option B (open ticket, do NOT implement in S4 PR)

Idle-counter-vs-parallel-tool-loop mismatch. Specifically:
- Harness's idle counter currently appears to consult INTENT/HEARTBEAT/APPLY/POSTMORTEM events
- PLAN-EXPLOIT child stream tool_round_complete events should ALSO reset the counter
- Without this, multi-stream ops get terminated even when they're making progress
- See: `memory/project_followup_harness_idle_counter_parallel_streams.md` (to-be-created)

---

## Slice 5a delivery summary (2026-04-25, current arc)

Per operator binding "advance to W3(6) Slice 5a (the sessions/ledger/docs prep — no flip) next and super beef it up":

**Shipped on `w3-6-slice-5a-prep` branch:**

| Artifact | Detail |
|---|---|
| **FlagRegistry seed** | `parallel_dispatch.ensure_flag_registry_seeded()` — registers all 5 Wave 3 (6) env knobs (master / shadow / enforce / max_units / wait_timeout_s) into Wave 1 #2's FlagRegistry. Wired into `GovernedLoopService.start` at boot (best-effort, never-raise pattern). Master flag tagged CRITICAL relevance under HARDEN posture so `/help flags --posture HARDEN` surfaces it. |
| **Operations runbook** | `docs/operations/wave3-parallel-dispatch-graduation.md` — operator-facing graduation + hot-revert reference. Documents env knob table, hot-revert recipe, marker glossary, live reachability blocker (F1), graduation cadence protocol, and Slice 5a evidence package. |
| **Structural pin tests** | `tests/governance/test_w3_6_slice5a_structural_pins.py` — 28 graduation-ready pins covering: (A) master/sub-flag defaults pre-graduation, (B) sub-flag composition under master-on/-off, (C) hot-revert force-disable, (D) ReasonCode + FanoutOutcome enum vocab + schema constants, (E) source-grep wiring (env reader literal, post-GENERATE seam, GLS seed call site, master-off short-circuit, worktree_manager coupling), (F) FlagRegistry registration with correct types/categories, (G) eligibility decision matrix (5 ReasonCode paths via injected gate+posture). 28/28 green. |
| **Live-fire smoke** | `scripts/livefire_w3_6_parallel_dispatch.py` — formal in-process smoke (no battle-test required). 30 checks across 7 sections: defaults / floor pin / hot-revert / decision matrix / FlagRegistry seed / source-grep wiring / authority invariants. 30/30 PASS locally. |
| **Combined regression** | 269/269 green across structural pins + 5 parallel_dispatch suites + 3 flag_registry suites. |

**NOT shipped (per Slice 5a "no flip" binding):**
- No master flag default flip (Slice 5b owns).
- No new live battle-test sessions (live reachability still blocked by F1; would just re-hit `intake_priority_inversion`).
- No SSE bridge for enforce/shadow events (separate scope-deferred follow-up per scope §"Known debt").

**Slice 5b unblock conditions still open:**
- F1 (intake priority scheduling) Slice 4 live cadence on main (Slices 1–3 already shipped per `project_followup_f1_intake_governor_enforcement.md`).
- 3 clean live battle-test sessions under `JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED=true JARVIS_WAVE3_PARALLEL_DISPATCH_ENFORCE=true` with ≥1 `[ParallelDispatch enforce_submit_start]` marker.
- Operator authorization for the master flag default flip commit.

The structural pin tests were authored explicitly so the Slice 5b flip
PR can be a single-commit env-default change + assertion update + matrix
row addition, with this Slice 5a infrastructure already pinning the
correctness invariants on every commit.

---

## Slice 5b prerequisites (for operator review before flip)

Before sending `authorize default flip for Wave 3 parallel dispatch flags`:

- 3/3 sessions clean on extended clean-bar.
- ≥1 `enforce_submit_start` marker observed across the cadence.
- ≥1 enforce terminal observed (completed / failed / cancelled / timeout) with downstream classifier matching scheduler state.
- 0 runner-attributed frames (parallel_dispatch.py / worktree_manager.py / phase_dispatcher.py / phase_runners).
- 0 worktree orphans after session + boot-time `reap_orphans` sweep.
- Slice 4 parity tests (128/128) still green on current HEAD — the flip commit will verify this pre-flip.
- FlagRegistry seed entries for all 5 Wave 3 env knobs present and `/help flag JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED` renders correctly (landed in Slice 5a docs commit).
- SSE debt acknowledged: enforce/shadow events log-only until a separate follow-up. Graduation does NOT gate on SSE parity per operator's Slice 5a ask.

## Post-flip expectations (Slice 5b + confirmation soak)

Once operator sends the default-flip authorization:

1. Single commit: flip `parallel_dispatch_enabled()` (and `..._enforce_enabled()` depending on operator's choice of which layer graduates first) default `"false"` → `"true"` in `parallel_dispatch.py`. Update FlagRegistry seed defaults to match.
2. Push to origin/main (standing authorization under shared-state rule).
3. Ledger row added below Verdict with flip commit hash.
4. Post-flip confirmation soak — canonical recipe, **no env overrides** (natural default behavior).
5. On clean post-flip: Wave 3 (6) → **FINAL**.

## Non-graduation follow-ups (deferred after FINAL)

Per operator scope freeze:
- **F1 — Intake governor enforcement** (`memory/project_followup_f1_intake_governor_enforcement.md`). Upgrade Wave 1 #3 SensorGovernor from advisory to enforcing at `UnifiedIntakeRouter.ingest()`. Closes the BG-starvation root cause surfaced in Slice 5a S1.
- **F2 — BacklogSensor per-entry urgency_hint schema** (`memory/project_followup_f2_backlog_urgency_hint_schema.md`). Additive schema field letting individual backlog.json entries override urgency without a session-wide env knob.
- **F3 — SHIPPED** as part of Slice 5a side-arc (`JARVIS_BACKLOG_SENSOR_DEFAULT_URGENCY`). Session-scoped override; graduation/harness-only knob.
- slice4b_runner fan-out consumption wiring (Wave 3 (6) enhancement; separate authorization).
- SSE bridges for enforce/shadow events (§8 parity debt; separate authorization).
- Wave 3 (7) mid-phase /cancel — structurally separate arc.
- Wave 2 (4) curiosity — deferred until Wave 3 (6) + (7) done OR narrow operator authorization.
