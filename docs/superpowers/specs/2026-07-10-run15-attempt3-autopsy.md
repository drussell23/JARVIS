# A1 Run #15 Attempt 3 Autopsy — session bt-iso-1783742671 (2026-07-10 21:05→22:28)

**Verdict: FAILED `fsm:only_noop_read_only_completions_no_mutation`, proven=false — and the run was
STRUCTURALLY UNPASSABLE: both the SENSE→intake edge and the patch producer were dead by harness
construction, independent of organism quality.** Full 5000s wall, clean stop, no memory kill.
Criteria pass: a1trace_5_hops, lineage, intervention_lock. Fail: fsm_classify_to_applied,
autonomous_pr, twelve_flag_audit.

## Causal chain (minimal)

1. **SENSE edge dead — checksum-baseline race.** The production FS watcher is CONTENT-CHECKSUM based
   (`file_watch_guard.py:648-660`, md5 vs baseline; `fs_event_bridge.py:63 verify_checksum=True`).
   Chaos mutated `leaf_predicates.py` at ~21:05:35, INSIDE the watcher's ~2-minute startup baseline
   scan (first published event 21:07:31, debug.log:3545) — the red content was folded into the
   baseline. The driver's mitigation re-`touch`es the file "to fire fs.changed.modified" — but touch
   changes only mtime; md5 identical → `old_checksum == checksum` → NO event. Zero
   `fs.changed.*leaf_predicates*` events across the whole 41k-line log. The plugin PRIMARY lane then
   served a PRE-mutation `.jarvis/test_results.json` snapshot: "plugin results consumed (0 failures)"
   (debug.log:3543, file 99s stale). Poll demoted to 600s (event-primary). **The red never became an
   intake op.** (Run #14's resolver wedge is FIXED — sensor scoped+spawned pytest cleanly at 21:08:17.)
2. **ACT edge dead — the adversary DW stub cannot emit a valid patch.** In iso mode DW = local
   adversary stub returning a fixed ~1.3KB truncated `leaf_predicates.py` payload for every op.
   Validation correctly rejected all candidates: 8× `full_content too short (1306 vs 15183 bytes)`
   (debug.log:4343), 4× `all_candidates_syntax_error`, 2× `file_scope_mismatch` (the roadmap op's
   candidates targeted the wrong file, debug.log:1800/1887). The ONLY mutation-class op (signed
   roadmap docstring seed `op-019f4f5a-8bda`, read_only=False) therefore ended `state=failed`.
   GENERATE "25 ok" = completed-with-no-applicable-patch; validation pipeline working correctly by
   discarding garbage.
3. **Everything else read-only BY DESIGN.** 75 `read_only_complete` lines = 25 distinct ops, dominated
   by Advisor `read_only=True` exploration/self-audit ops (32 ops so marked), short-circuited at
   `orchestrator.py:8573-8595` / `validate_runner.py:1148-1173`, or provider `is_noop`. These are the
   ops that make a1trace pass while the loop looks "healthy but inert" (the historical Grade-B class).

## Slice 4 quiet-claims — corrected

- LoopDeadman: 0 fires ✓ (the 22:30:05 os._exit(75) = post-wall ShutdownWatchdog teardown backstop;
  wall stop itself was clean/DISARMED).
- WAL flock_append_line LoopSink: **4** (was 214 in Run #14) ✓.
- Budget: $2.00, zero spurious quarantine ✓ (the 22:04 dw_global_outage was ADVERSARY-INJECTED chaos —
  legitimate; 14 DLQ CRITICAL orphans incl. l2_unresolvable_awaiting_human worth eyes).
- **ControlPlaneStarvation: 1,041 warnings — NOT quiet** (earlier summary overstated). LoopSink 284
  total: `file_has_test_coverage` 225 (still the residual), ingest 38, posture 17.

## Traps flagged

- `LEDGER_TERMINAL state=applied written=True` is stamped for read-only ops EVEN THOUGH APPLY was
  skipped (op-019f4f5a-8bed, 21:06:12 vs debug.log:944) — ledger bookkeeping, not filesystem mutation;
  false-positive for any "did we mutate?" check.
- Roadmap goal declared `blast_radius=3` but Advisor computed `blast=50` → caution (21:06:32).

## Ranked fixes for the next slice

HARNESS (unblocks the gate — without these no Run #16 can pass):
1. Injected-red signal path: (1a) post-inject re-fire must CHANGE CONTENT (nonce comment), not mtime
   `touch`; (1b) gate `STEP inject` on the FSEventBridge "chain is live" first-event marker
   (debug.log:3545 class), not `TestWatcher READY` (2-min blind window); (1c) seed/delete
   `.jarvis/test_results.json` at inject so the plugin lane can't serve stale "0 failures".
2. Adversary DW stub must emit at least one VALID full-file repair candidate keyed to the op targeting
   `leaf_predicates.py` (well-formed, full-length, correct scope) — otherwise ACT is untestable.

CODE (durable):
3. FileWatchGuard startup-baseline race: reconcile first-scan checksums against a persisted
   prior-session baseline (emit MODIFIED on diff), or post-arm reconciliation window for changes
   landing during the initial md5 walk.
4. Roadmap blast_radius vs Advisor blast reconciliation for the seed's target set.

OBSERVABILITY (autopsy hygiene):
5. Distinct terminal label for read-only completions (not `state=applied`); starvation-rate rollup;
   `file_has_test_coverage` 225-sink residual re-diagnosis (per backlog, needs cpu_ms read).

Artifacts: `.ouroboros/sessions/bt-iso-1783742671/debug.log`,
`logs/a1_runs/20260711T040418Z/iso-a1-20260710-210431/a1_verdict.json`, autopsy dir
`iso-a1-20260710-210431_20260710-223512`, console `logs/a1_runs/ignite_run15_console.log`.
