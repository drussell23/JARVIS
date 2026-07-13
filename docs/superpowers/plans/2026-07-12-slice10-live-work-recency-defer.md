# Slice 10 — LiveWorkSensor recency-composed dirty signal + genuine APPLY defer

**Root cause (Run #20, session bt-iso-1783924404):** the repair op for the chaos leaf pair
passed GATE and candidate-tree VALIDATE (Slice 9 proven live), then died at APPLY 80%:
`LiveWorkSensor: human is active on leaf_predicates.py (git status: has uncommitted
changes) — deferring APPLY` → `LEDGER_TERMINAL state=failed`. Two defects compose:

1. **Timeless dirty signal** (`live_work_sensor.py:126-132`): signal 1 treats *any*
   uncommitted change as "human is active" with no recency bound — only signal 2 (mtime)
   has the `JARVIS_LIVE_WORK_ACTIVE_WINDOW_S` (180s) window. A file dirty for three days
   is not being edited; a chaos-injected file is dirty *by construction* and stays dirty
   until the repair lands — which the signal itself forbids. Deadlock class.
2. **The "defer" is a lie** (`orchestrator.py` APPLY gate ~10210-10248): the code logs
   "deferring APPLY" but terminal-fails the op (`human_active_on_target`). No wait, no
   requeue. The reason code IS distinct (§7 satisfied) — the defect is purely that a
   recoverable condition is treated as terminal.

## Fix 1 — sensor primitive: dirty composes with recency

`is_human_active`: the git-dirty signal now *qualifies* rather than decides — dirty AND
`mtime age <= active_window_s` → active (reason: `git status: X has uncommitted changes
(modified Ns ago)`). Dirty + stale mtime falls through to signals 2/3 (which will also be
quiet) → file is idle. Signals 2 (bare recent mtime) and 3 (IDE locks) unchanged — an
actively-editing human is still detected by either. Data-loss analysis: applying over a
stale-dirty file is safe — the candidate was generated FROM the dirty content
(working-tree-faithful, Slice 9) and the existing stale-exploration hash guard at APPLY
catches mid-flight content changes; ChangeEngine 2PC snapshots pre-images.

New method `seconds_until_quiet(rel_path) -> float`: 0.0 when idle now; `window - age`
when recency holds; `float("inf")` when an IDE lock is present (locks have no expiry the
sensor can predict). Pure derivation from sensor state — gives the orchestrator an exact,
non-polled wait horizon.

Kill switch: `JARVIS_LIVE_WORK_DIRTY_REQUIRES_RECENCY` (default `true`; `false` restores
the legacy timeless-dirty signal byte-for-byte).

## Fix 2 — orchestrator: defer means wait, bounded by the op's own budget

At the APPLY LiveWork gate: on an active hit, ask the sensor `seconds_until_quiet`. If
that horizon fits inside the op's remaining pipeline budget (re-clocked from
`ctx.pipeline_deadline` — same re-clock idiom as the VALIDATE retry loop), `await
asyncio.sleep(horizon)` ONCE (event-precise: the wait is derived from the sensor's own
window math, not a poll interval), invalidate the sensor's git cache, and re-run the full
scan. Loop while budget remains and horizons stay finite; a human re-editing mid-wait
refreshes mtime → new horizon or budget exhaustion. Infinite horizon (IDE lock) or
insufficient budget → the existing terminal path with `human_active_on_target`,
log made honest (states waited Ns / wait-infeasible, not "deferring").
Orange tier (APPROVAL_REQUIRED) continues to bypass the gate entirely (human approved).

Master: `JARVIS_APPLY_LIVE_WORK_WAIT_ENABLED` (default `true`; `false` = legacy
immediate-terminal).

## Composition on Run #21

Injection dirties the leaf at T0 (mtime fresh). Repair op reaches APPLY at ~T0+50s →
sensor horizon ≈ 130s, well inside the op budget → op waits once, file crosses the
window, scan comes back quiet → APPLY proceeds → VERIFY → AutoCommit. Zero wasted
GENERATE cycles; no harness-specific carve-outs; production semantics strengthened
(a real mid-edit human still defers, now with an honest bounded wait).

## Tasks

1. Sensor: recency-composed dirty + `seconds_until_quiet` + kill switch (TDD,
   `tests/governance/test_live_work_sensor_recency.py`).
2. Orchestrator: bounded defer-wait at the APPLY gate (TDD, extend the defer-site tests;
   verify `ctx.pipeline_deadline` is the live budget source at this seam).
3. Flag seeds ×2 + CLAUDE.md bullet + ledger.

Battery: new tests + test_validate_candidate_tree.py + repair sweep. Soak proof: Run #21.
