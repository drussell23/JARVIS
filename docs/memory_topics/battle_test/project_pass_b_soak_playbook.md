---
title: Pass B Graduation — Soak Playbook (Operator-Facing)
modules: [scripts/pass_b_soak_assertion.py, scripts/pass_b_graduation_closure_verdict.py, scripts/ouroboros_battle_test.py, backend/core/ouroboros/governance/meta/meta_phase_runner.py, backend/core/ouroboros/governance/meta/replay_executor.py, backend/core/ouroboros/governance/meta/order2_review_queue.py, backend/core/ouroboros/governance/flag_registry_seed.py]
status: merged
source: project_pass_b_soak_playbook.md
---

# Pass B Graduation — Soak Playbook (Operator-Facing)

This is the **operator-paced** graduation procedure for the two write-path Pass B flags that the structural graduation arc deliberately kept default-false:

- `JARVIS_META_PHASE_RUNNER_ENABLED` (Pass B Slice 5 — autonomy-creation surface)
- `JARVIS_REPLAY_EXECUTOR_ENABLED` (Pass B Slice 6.1 — actual mutation execution surface)

The W2(5) policy requires a **3-clean-session arc** before flipping these defaults. Each session must satisfy the 5 clean-bar criteria asserted by `scripts/pass_b_soak_assertion.py`. This playbook walks through the procedure end-to-end.

## Pre-flight checklist

Before starting the 3-soak arc, verify:

1. **Pass B substrate is structurally graduated** (already done 2026-05-03 — see `project_pass_b_graduation_closure.md`). FlagRegistry seeds present, AST pins live, the 6 read-only flags default-true. Run `python3 scripts/pass_b_graduation_closure_verdict.py` to confirm 5/5 PASS.
2. **The cost-contract cage is locked-true** (also already done — `pass_b_amendment_requires_operator_cage` AST pin in `order2_review_queue.register_shipped_invariants()`).
3. **The Production Oracle observer is live** (`JARVIS_PRODUCTION_ORACLE_ENABLED=true` default). The auto_action_router VERIFY hook will surface advisory proposals during soaks if the substrate misbehaves.
4. **The pass_b_soak_assertion.py script is reachable** at `scripts/pass_b_soak_assertion.py`. Run it against the most recent existing session as a smoke test:
   ```bash
   python3 scripts/pass_b_soak_assertion.py
   ```
   You should see 5 named criteria evaluated. The verdict result doesn't matter for the smoke test — what matters is that the script exits 0/1 (not 2 = artifacts missing).

## Soak procedure (run 3 times)

### Step 1: Configure the env

```bash
# Master flags for the substrate under test
export JARVIS_META_PHASE_RUNNER_ENABLED=true
export JARVIS_REPLAY_EXECUTOR_ENABLED=true

# Cage stays locked-true (it's structurally enforced regardless,
# but operators set it explicitly for paranoia visibility)
export JARVIS_ORDER2_MANIFEST_AMENDMENT_REQUIRES_OPERATOR=true

# Sane soak bounds — pick values appropriate for the local env.
export OUROBOROS_BATTLE_HEADLESS=true
export OUROBOROS_BATTLE_MAX_WALL_SECONDS=2400  # 40 min hard cap
```

### Step 2: Run the soak

```bash
python3 scripts/ouroboros_battle_test.py \
    --headless \
    --cost-cap 0.50 \
    --idle-timeout 600 \
    --max-wall-seconds 2400 \
    -v 2>&1 | tee /tmp/pass_b_soak_${USER}_$(date +%s).log
```

The harness writes `summary.json` + `debug.log` to `.ouroboros/sessions/<session-id>/` on every reachable exit path (per the partial-shutdown insurance documented in CLAUDE.md). SIGKILL is the only unrecoverable case.

### Step 3: Assert the clean-bar

```bash
python3 scripts/pass_b_soak_assertion.py
```

The script reads the most-recent session's artifacts and prints a 5-criterion verdict. Three possible outcomes:

- **Exit 0 = CLEAN** → session counts toward the 3-clean arc.
- **Exit 1 = NOT CLEAN** → session does NOT count. Investigate the failed criteria; do NOT continue to the next soak until the root cause is understood + fixed.
- **Exit 2 = artifacts missing** → harness died before summary.json was written (likely SIGKILL). Investigate; this also doesn't count.

### Step 4: Repeat steps 1-3 two more times

Three sessions total. The assertion script is intentionally session-scoped (one verdict per call) — you can run it against any past session by passing the session_id:

```bash
python3 scripts/pass_b_soak_assertion.py bt-2026-05-04-091523
```

## Interpretation guide for failed criteria

### CB1 — Pass B substrate exceptions

**Failure shape**: `pass_b_exception_lines=N` where N>0.

**Root causes (most-likely first)**:
1. MetaPhaseRunner generated an invalid PhaseRunner subclass that the AST validator rejected — but the validator's exception leaked instead of returning `ValidationStatus.PARSE_ERROR` cleanly. Check `[ASTPhaseRunnerValidator]` log lines.
2. replay_executor sandbox setup failed (e.g., compile error in proposed source) — but the executor's exception leaked instead of returning `ReplayExecutionStatus.FAILED`. Check `[ReplayExecutor]` lines.
3. Order2ReviewQueue flock contention or disk error — check `[Order2ReviewQueue]` lines.

**Fix**: Add the missing try/except to whichever module leaked. Pass B substrate's discipline is "every code path returns a typed status; exceptions are bugs". Update `register_shipped_invariants` to pin the new exception-handling block.

### CB2 — replay_executor invocation without operator_authorized

**Failure shape**: `invocations=N authorized_log_lines=M` where M < N.

**Root causes**:
1. Some new caller is invoking `execute_replay_under_operator_trigger()` without passing `operator_authorized=True`. Find the caller; either fix it OR add a structural cage pin preventing it.
2. Logger formatting changed and `operator_authorized=True` no longer appears verbatim near invocations. Check `[ReplayExecutor]` log messages and update the regex in `pass_b_soak_assertion.py::_RE_OPERATOR_AUTHORIZED` if the format genuinely changed.

**Fix**: This is a CRITICAL failure. Do NOT continue the soak arc until the unauthorized caller is removed. The cage's structural enforcement (`amendment_requires_operator()` always returns True) will block actual mutation — but the empirical signal that someone is even attempting it indicates a substrate threat-model violation.

### CB3 — Order-2 amendment outside /order2 amend

**Failure shape**: `violations=[entry_id=X approved without /order2 amend marker]`.

**Root causes**:
1. Some code path is writing to the review queue JSONL directly without going through the dispatcher. The dispatcher is the only call site that sets `approved_via_repl=True`.
2. A test fixture left behind a malformed entry. Clean the fixture or rotate the queue.

**Fix**: Identify the bypass caller. The /order2 amend REPL is the structurally-required mutation path; any other path is a bug.

### CB4 — Cost burn over baseline

**Failure shape**: `cost_usd=X baseline=$0.50 ratio=Yx` where Y > 3.

**Root causes**:
1. Provider cascade fell back to Claude for ops that should have stayed on DoubleWord. Check provider-route distribution in `summary.json::cost_by_op_phase_provider`.
2. The harness ran a long-tail op (e.g., L2 repair iteration) that dragged cost up disproportionately. Inspect `summary.json::operations` for outliers.
3. The baseline is mis-configured for this env. Update `JARVIS_PASS_B_SOAK_COST_BASELINE_USD` to match local soak realistic cost.

**Fix**: Either fix the cost-pathway or recalibrate the baseline. Until the baseline is reliable, CB4 is noise.

### CB5 — session_outcome=incomplete_kill / abnormal stop_reason

**Failure shape**: `outcome='incomplete_kill'` or `abnormal=True`.

**Root causes**:
1. Wall-clock cap fired and the harness died before clean shutdown — most common. Increase `--max-wall-seconds` if soaks are legitimately long, OR investigate why the soak isn't reaching idle_timeout.
2. SIGTERM from external process (e.g., terminal closed). Re-run from a stable terminal.
3. SIGKILL from OOM killer. Investigate memory pressure; this is a real bug.

**Fix**: Most CB5 failures are infra (terminal management, OOM, wall-clock budgeting). Fix the infra and re-soak.

## After 3 CLEAN sessions

When you have 3 sessions in a row that all pass `pass_b_soak_assertion.py` with exit 0, flip the defaults:

1. Edit `backend/core/ouroboros/governance/meta/meta_phase_runner.py` `is_enabled()` to default-true via the standard `if raw == "": return True` pattern (mirror the Pass B Slice 3 graduation pattern).
2. Edit `backend/core/ouroboros/governance/meta/replay_executor.py` `is_enabled()` similarly.
3. Update the FlagSpec defaults in `flag_registry_seed.py` (Pass B section) for both flags from `default=False` to `default=True`.
4. Update `pass_b_graduation_closure_verdict.py::_KEEP_FALSE_TARGETS` — both should now move into `_FLIP_TARGETS`.
5. Run the verdict to confirm: 6 flags should now flip to True (was 4), and the keep-false set should be empty.
6. Save a `project_pass_b_full_graduation_closure.md` memory entry referencing the 3 clean session IDs.

The cage (`amendment_requires_operator()` LOCKED-TRUE via AST pin) remains structurally enforced after the flip — the flags control whether the substrate is loaded; the cage controls whether mutations actually happen.

## Why the operator-paced gate

The substrate is structurally complete (438 tests across the 6 Pass B slices, plus the graduation arc's 442 sweep). Structural correctness has been proven AT REST. What soak validates is that:

1. The substrate behaves correctly UNDER LOAD (real intake, real ops, real provider cascades).
2. No new caller has introduced an unauthorized replay_executor invocation since the last soak.
3. Cost behavior is bounded.
4. The harness reaches clean termination.

These are EMPIRICAL properties that AST pins cannot enforce — only soak observation can. Hence the operator-paced gate.

## Files referenced

- `scripts/pass_b_soak_assertion.py` (this arc)
- `scripts/pass_b_graduation_closure_verdict.py` (Pass B graduation arc 2026-05-03)
- `scripts/ouroboros_battle_test.py` (harness)
- `backend/core/ouroboros/governance/meta/meta_phase_runner.py`
- `backend/core/ouroboros/governance/meta/replay_executor.py`
- `backend/core/ouroboros/governance/meta/order2_review_queue.py`
- `backend/core/ouroboros/governance/flag_registry_seed.py`
