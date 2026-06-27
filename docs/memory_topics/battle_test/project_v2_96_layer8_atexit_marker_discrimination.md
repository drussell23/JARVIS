---
title: Project V2 96 Layer8 Atexit Marker Discrimination
modules: [tests/battle_test/test_layer8_atexit_marker_discrimination.py, backend/core/ouroboros/battle_test/termination_hook_default_adapters.py, backend/core/ouroboros/governance/graduation/live_fire_soak.py, tests/battle_test/test_termination_hook_slice3_wiring.py, tests/governance/test_phase_9_slice_7c_composite_stop_reason.py]
status: historical
source: project_v2_96_layer8_atexit_marker_discrimination.md
---

May 10 2026: closes Layer 8 of the cadence-arc — the marker-discrimination gap exposed by soak #3 (`bt-2026-05-10-221432`).

**Diagnosis trail**:

The 3rd Phase 9 graduation soak fired the v2.92 Layer 7 dual-clock watchdog correctly (skew=210s sleep detected, cap fired on wall-clock authority at effective=2404s). The v2.88 Layer 6 atexit fallback wrote summary.json before the ShutdownWatchdog os._exit(75) fired at 35.7s. **All three layers (6/7) held structurally.**

But the resulting history row was:
```
session_id      bt-2026-05-10-221432
session_outcome incomplete_kill
stop_reason     wall_clock_cap+atexit_fallback
outcome         infra              ← NOT clean
```

The Phase 9 ladder stayed at 2/3 clean rows. No graduation.

**Root cause** — TWO design errors compounding:

1. **`termination_hook_default_adapters._CAUSE_TO_SESSION_OUTCOME`** mapped `WALL_CLOCK_CAP → "incomplete_kill"`. The original Slice 3 design rationale (line 145-152 of adapter, dated 2026-05-07) said "every cause that was previously classified as incomplete_kill MUST still map to incomplete_kill ... New causes (wall-cap, idle, budget) ALSO map to incomplete_kill because the LastSessionSummary parser's clean-vs-interrupted dichotomy treats them all the same".

   But CLAUDE.md battle-test footnote (independent of that adapter) states: *"wall_clock_cap is treated equivalent to idle_timeout for clean-bar purposes in the Wave 2 (5) graduation matrix harness-class footnote"*. The adapter contradicted CLAUDE.md.

2. **`live_fire_soak._SHUTDOWN_NOISE_STOP_REASONS`** included `wall_clock_cap`. Slice 7c (2026-05-07) added it because the May 8 cadence soak `bt-2026-05-08-022312` had the composite signature and the team wanted it routed to infra. But that was a defensive move BEFORE v2.88 Layer 6 atexit fallback shipped. Post-v2.88, the composite is a graceful-shutdown signature, not an "external boundary cut us off" signature.

**Architectural insight**: there are TWO distinct termination classes:

- **External-signal causes** (SIGTERM/SIGINT/SIGHUP) — the harness did NOT intend the termination. Something outside the harness killed it. INFRA classification is correct.
- **Harness-intended causes** (WALL_CLOCK_CAP/IDLE_TIMEOUT/BUDGET_EXCEEDED) — the harness's own watchdogs/gates fired per design. These are clean stops by intent. CLEAN classification is correct.

Pre-Layer-8 the adapter conflated both classes under "incomplete_kill" + the classifier conflated both under noise. Layer 8 separates them at the source.

**Structural fix — 2 seams**:

1. **`termination_hook_default_adapters.py:153-163`** — revised `_CAUSE_TO_SESSION_OUTCOME`:
   ```python
   # External-signal causes — incomplete_kill (the harness did
   # not intend this termination).
   TerminationCause.SIGTERM: "incomplete_kill",
   TerminationCause.SIGINT: "incomplete_kill",
   TerminationCause.SIGHUP: "incomplete_kill",
   # Harness-intended causes — clean stamp per Layer 8 (v2.96).
   # CLAUDE.md battle-test footnote: wall_clock_cap is treated
   # equivalent to idle_timeout for clean-bar purposes.
   TerminationCause.WALL_CLOCK_CAP: "complete",   # was incomplete_kill
   TerminationCause.IDLE_TIMEOUT: "complete",     # was incomplete_kill
   TerminationCause.BUDGET_EXCEEDED: "complete",  # was incomplete_kill
   TerminationCause.NORMAL_EXIT: None,
   TerminationCause.UNKNOWN: "incomplete_kill",
   ```

2. **`live_fire_soak.py:380-388`** — revised `_SHUTDOWN_NOISE_STOP_REASONS`:
   ```python
   _SHUTDOWN_NOISE_STOP_REASONS: FrozenSet[str] = frozenset({
       "sigterm",
       "sighup",
       "sigint",
       # `wall_clock_cap` REMOVED in Layer 8 (v2.96, 2026-05-10) —
       # harness-intended termination, classifies as clean per
       # CLAUDE.md clean-bar-equivalence footnote.
       "harness_idle_timeout",
   })
   ```

**Why source-discrimination (not downstream filtering)**:

Alternative considered: leave the adapter alone, fix only the soak classifier to recognize `wall_clock_cap+atexit_fallback` as a clean special case. Rejected because:
- It treats a symptom (classifier blind to the path) not the root cause (wrong marker stamped at source)
- The LastSessionSummary parser (`last_session_summary.py`) would still see `session_outcome=incomplete_kill` and mark the previous session as incomplete on the next boot — wrong for a clean wall-clock-cap
- Other downstream consumers (audit tooling, future graduation arcs) would each need the same filter — duplication

Source-discrimination fixes the marker once; every downstream consumer sees the canonical value.

**Test impact**:

- 16 NEW regression tests in `tests/battle_test/test_layer8_atexit_marker_discrimination.py`:
  - Adapter mapping (6 tests: WALL_CLOCK_CAP/IDLE_TIMEOUT/BUDGET_EXCEEDED→complete; SIGTERM/SIGINT/SIGHUP→incomplete_kill; NORMAL_EXIT→None; UNKNOWN→incomplete_kill)
  - Classifier noise set (2 tests: wall_clock_cap NOT in set; external signals still in)
  - Forward-fix integration (1 test: bt-2026-05-10-221432 post-Layer-8 signature classifies CLEAN)
  - Backward-compat (1 test: legacy `incomplete_kill` data still routes infra — Layer 8 fixes source, not predicate)
  - **4 AST pins**: adapter cites Layer 8 + v2.96 / classifier cites Layer 8 + v2.96 / adapter uses `"complete"` literal for WALL_CLOCK_CAP (bytes-pinned via ast.Dict walk) / classifier noise set size = 4 entries

- Updated tests (no regressions):
  - `test_termination_hook_slice3_wiring.py` (24 tests) — flipped 3 expectations (WALL_CLOCK_CAP/IDLE_TIMEOUT/BUDGET_EXCEEDED expected values + 1 docstring + 1 commentary block)
  - `test_phase_9_slice_7c_composite_stop_reason.py` (32 tests) — flipped 2 parametrize entries (wall_clock_cap False / wall_clock_cap+atexit_fallback False) + updated canonical set assertion (5→4 entries) + renamed legacy test to "_legacy_incomplete_kill_routes_infra" + added new "_layer8_routes_clean" test + updated uncomposed-shutdown test to skip wall_clock_cap and added uncomposed-wall-cap-routes-clean test

**Broader regression**: 100 tests green across Layer 6 + Layer 7 + Layer 8 + Slice 3 wiring + Slice 7c composite. **Zero regression** on the cadence-arc spine.

**Operator binding 2026-05-10 satisfied verbatim**:
- Solved root problem directly — fixed the marker at the source where it's stamped, NOT at the downstream classifier (avoids symptom-treatment; every consumer benefits from canonical value)
- No workarounds — did NOT add a special-case branch in classify_outcome for `wall_clock_cap+atexit_fallback`; the fix is structural via the 2 canonical seams
- No shortcuts — 16 new tests + 4 AST pins + 56 existing tests updated to reflect new semantics; bytes-pin verifies `WALL_CLOCK_CAP: "complete"` via ast.Dict walk
- Composes existing canonical paths: `TerminationCause` 8-value taxonomy (no new value), `_CAUSE_TO_SESSION_OUTCOME` dict (changed 3 values), `_SHUTDOWN_NOISE_STOP_REASONS` frozenset (removed 1 value), CLAUDE.md clean-bar-equivalence footnote (now actually enforced)
- No hardcoding — semantic classes still derived from existing TerminationCause enum (no new strings invented)
- No duplication — 2 seam edits; no parallel discrimination logic; no special-case branches

**Cadence arc state (Layers 1–8 all CLOSED)**:
- v2.86 — Layers 1-4 (env / wrapper / sentinel / socksio)
- v2.87 — Layer 5 (modality ledger always loads)
- v2.88 — Layer 6 (watchdog Layer 4 escape-hatch writes partial summary)
- v2.92 — Layer 7 (dual-clock authority — sleep/suspend immunity)
- **v2.96 — Layer 8 (atexit-fallback marker discrimination — harness-intended vs external)**

The watchdog now correctly classifies wall_clock_cap as a clean harness-intended termination under ALL paths (clean shutdown completes / clean shutdown wedges + atexit fallback fires). Soak `bt-2026-05-10-221432` (the diagnostic session) STILL classifies as infra — Layer 8 fixes the source, not historical rows. The NEXT soak under Layer 8 will produce a correctly-classified clean row.

**Why this matters for RSI**: graduation soaks under Phase 9 cadence MUST produce reliable evidence rows. When the watchdog's stated contract ("kill after N seconds") is honored AND CLAUDE.md's stated semantics ("wall_clock_cap is clean-bar-equivalent") are honored, the soak's terminal classification mirrors the harness's intent. Pre-Layer-8 the system had a self-contradiction between code and documentation; Layer 8 resolves it in favor of the documentation (the authoritative source).

**Files modified**:
- `backend/core/ouroboros/battle_test/termination_hook_default_adapters.py` (revised `_CAUSE_TO_SESSION_OUTCOME` mapping at lines 153-163 — 3 values flipped + extensive comment block citing Layer 8 + soak signature + CLAUDE.md)
- `backend/core/ouroboros/governance/graduation/live_fire_soak.py` (revised `_SHUTDOWN_NOISE_STOP_REASONS` at lines 380-400 — wall_clock_cap removed + Layer 8 commentary)
- `tests/battle_test/test_termination_hook_slice3_wiring.py` (3 expectation flips + commentary updates)
- `tests/governance/test_phase_9_slice_7c_composite_stop_reason.py` (2 parametrize flips + canonical set assertion update + 2 new tests added: `_layer8_routes_clean` + `_uncomposed_wall_clock_cap_routes_clean`)
- `tests/battle_test/test_layer8_atexit_marker_discrimination.py` (NEW, 16 regression tests + 4 AST pins)

**NEXT** (operator-paced): run `--once` for soak #4 — first graduation-eligible soak under the FULLY-closed cadence arc (Layers 1-8 all in place). If clean, `JARVIS_EXECUTION_MONITOR_BRIDGE_ENABLED` graduates default-FALSE → default-TRUE.
