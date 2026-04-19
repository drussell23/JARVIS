# Visual VERIFY — Slice 4 Graduation Checklist

**Task 20 of the VisionSensor + Visual VERIFY arc.**

**Status (as of this doc's creation)**: pre-flight PASSED (16/16 advisory
integration + auto-demotion green; 47/47 module spine green). 3-session
real-world arc: **not yet started**.

**Spec**: `docs/superpowers/specs/2026-04-18-vision-sensor-verify-design.md`
§Graduation Criteria → Slice 4.
**Plan**: `docs/superpowers/plans/2026-04-18-vision-sensor-verify.md`
Task 20.

---

## Scope

Slice 4 = **model-assisted advisory verdict** on top of the Slice 3
deterministic battery. The advisory layer calls Qwen3-VL-235B with
pre/post frames + the op's stated intent and emits one of
`aligned` / `regressed` / `unclear`. A `regressed` verdict above
the confidence threshold (default 0.80) routes to L2 Repair.

**I4 asymmetry preserved**: the advisory alone cannot rescue a
deterministic `fail`; it can only add an L2 dispatch when
deterministic verdict is `pass` and the VLM sees regression.

Prerequisites:
- Slices 1 + 2 + 3 already graduated.
- Orchestrator wiring for `run_advisory` + `check_and_apply_auto_demotion`.
- SerpentFlow wiring for `/verify-confirm` + `/verify-undemote`.

---

## Pre-flight — autonomous, ~2 seconds

```bash
python3 -m pytest tests/governance/test_visual_verify_slice4_preflight.py -v
python3 -m pytest tests/governance/test_visual_verify_advisory.py -v
```

**Pass = 16/16 preflight + 47/47 module**. If either is red, do not proceed.

Exercises (via mock `advisory_fn`):

1. Deterministic pass + advisory `aligned` → no L2.
2. Deterministic pass + advisory `regressed` above threshold → L2 triggered.
3. At-threshold confidence → no L2 (strict greater-than).
4. Below-threshold → no L2.
5. `unclear` → no L2.
6. **I4 asymmetry**: deterministic fail + advisory aligned hi-conf →
   deterministic verdict preserved.
7. Advisory reasoning with injection → sanitized placeholder (T1).
8. VLM exception → graceful skip, no crash.
9. Ledger record + `/verify-confirm` round-trip across restart.
10. Auto-demotion fires at >50% FP rate.
11. Auto-demotion idempotent; `/verify-undemote` clears.
12. `model_assisted_active()` requires env AND no demotion flag.
13. Master-switch + confidence-threshold + FP-threshold pinned in source.

---

## Slice 4 runtime configuration

Build on the Slice-1/2/3-graduated baseline, then enable the advisory:

```bash
# Slice 1-3 graduated defaults — don't override
unset JARVIS_VISION_SENSOR_ENABLED
unset JARVIS_VISION_SENSOR_TIER2_ENABLED
unset JARVIS_VISION_CHAIN_MAX
unset JARVIS_VISION_VERIFY_ENABLED

# Slice 4 additions
export JARVIS_VISION_VERIFY_MODEL_ASSISTED_ENABLED=true
# Optional tunables
export JARVIS_VISION_VERIFY_REGRESS_CONFIDENCE=0.80
```

**Do NOT flip** the `JARVIS_VISION_VERIFY_MODEL_ASSISTED_ENABLED`
default in source until Step 3.

---

## Prerequisite wiring (if not already done)

Task 19 shipped `visual_verify.py` with the advisory layer + ledger +
auto-demotion + REPL handlers as self-contained functions. The
remaining wiring lives in orchestrator + SerpentFlow:

1. **Orchestrator call site** — after `run_if_triggered` returns
   `pass`, dispatch advisory:

   ```python
   from backend.core.ouroboros.governance.visual_verify import (
       run_advisory, model_assisted_active, AdvisoryLedger,
   )
   if det_result.verdict == VERDICT_PASS and model_assisted_active():
       adv_outcome = run_advisory(
           attachments=ctx.attachments,
           op_description=ctx.description,
           advisory_fn=_make_vlm_adapter(),   # lean_loop-backed
       )
       if adv_outcome.advisory is not None:
           _advisory_ledger.record_advisory(
               op_id=ctx.op_id,
               advisory=adv_outcome.advisory,
               l2_triggered=adv_outcome.l2_triggered,
           )
       if adv_outcome.l2_triggered:
           # Dispatch to L2 same path TestRunner red uses
           await _l2_engine.repair(ctx, reason=adv_outcome.advisory.reasoning)
   ```

2. **Session-end hook** — call `check_and_apply_auto_demotion`
   before shutdown (in the harness `atexit` or signal handler,
   mirroring Task 9's retention purge pattern):

   ```python
   from backend.core.ouroboros.governance.visual_verify import (
       check_and_apply_auto_demotion,
   )
   did_demote, rate = check_and_apply_auto_demotion(_advisory_ledger)
   if did_demote:
       logger.warning(
           "[VisualVerify] auto-demoted: FP rate %.2f >= 0.50 — "
           "model-assisted off next session",
           rate,
       )
   ```

3. **SerpentFlow REPL commands** — register two slash handlers:

   ```python
   from backend.core.ouroboros.governance.visual_verify import (
       handle_verify_confirm_command,
       handle_verify_undemote_command,
   )
   # /verify-confirm <op-id> {agree|disagree}
   # /verify-undemote
   ```

4. Re-run Slice 3 pre-flight + all advisory tests to confirm no
   regression.

---

## Session runbook

### Boot

```bash
python3 scripts/ouroboros_battle_test.py --cost-cap 0.50 --idle-timeout 600 -v
```

Within 60s, confirm the boot log contains:

```
[IntakeLayer] VisionSensor registered enabled=true tier2=true chain_max=3 session_id=...
[Orchestrator] Visual VERIFY phase active (deterministic)
[Orchestrator] Visual VERIFY model-assisted active (advisory → L2 above 0.80 confidence)
```

If the third line is missing and no demotion flag is present, env
didn't propagate. If a demotion flag is present from a previous
session, boot log should say:

```
[Orchestrator] Visual VERIFY model-assisted DEMOTED — run /verify-undemote to re-arm
```

### What to drive in each session

Slice 4 sessions focus on **advisory verdict quality** — does the
VLM correctly identify when a UI change diverges from the op's
intent?

| Session | Purpose | What the operator does |
|---------|---------|------------------------|
| **1** | Advisory fires ≥3 times | Drive UI ops where something interesting happens: color/layout changes that deviate from the described intent. Goal: VLM emits at least 3 advisory verdicts (any mix of aligned/regressed/unclear). |
| **2** | Human agreement discipline | For each `regressed` verdict, run `/verify-confirm <op-id> agree` if the VLM was right, `/verify-confirm <op-id> disagree` if it was a false positive. Target: ≥60% agree rate. |
| **3** | Cost + I4 preservation | Normal UI work. Advisory fires as appropriate; verify no op is auto-applied on a `pass` deterministic + `regressed` advisory-above-threshold without L2 running. |

### Observability

```
[VisualVerify] advisory verdict=aligned confidence=0.88
[VisualVerify] advisory verdict=regressed confidence=0.85 → L2 triggered
[VisualVerify] advisory verdict=regressed confidence=0.70 → below threshold
[VisualVerify] advisory skipped reason=advisory_fn raised
[VisualVerify] auto-demoted FP rate=0.57 >= 0.50
```

### End-of-session rollups

```bash
# Advisory fire breakdown
grep 'advisory verdict=' .ouroboros/sessions/<session-id>/debug.log | \
    awk -F'verdict=' '{print $2}' | awk '{print $1}' | sort | uniq -c

# Human confirmation ledger snapshot
cat .jarvis/vision_verify_advisory_ledger.json | \
    jq '.entries[] | select(.verdict == "regressed") | .human_confirmation' | \
    sort | uniq -c

# Auto-demotion flag status
ls -la .jarvis/vision_verify_model_assisted_demoted.flag 2>&1
```

---

## Graduation criteria (spec §Slice 4)

Each of the **3 consecutive** sessions must satisfy **every** criterion.

- [ ] **Model-assisted runs on ≥3 UI-affected ops per session.** Count
      `[VisualVerify] advisory verdict=` lines.
- [ ] **Human agreement with `regressed` verdicts ≥60%.** Compute
      from `.jarvis/vision_verify_advisory_ledger.json` — agrees /
      (agrees + disagrees) on `verdict == regressed` confirmed entries.
- [ ] **L2 convergence on advisory-routed ops ≥50%.** Of the ops
      where `l2_triggered=True`, at least half should converge in L2
      (i.e. produce a subsequent successful APPLY). Low convergence
      suggests the VLM is hallucinating regressions L2 can't fix.
- [ ] **Zero I4 asymmetry violations.** No op where deterministic was
      `pass` got auto-applied on advisory `regressed` without
      hitting L2. Preflight Scenario 6 guards this mechanically, but
      check the live session debug.log for any skipped-L2 path.
- [ ] **Daily cost ≤ $1.00** including advisory VLM calls.
- [ ] **Zero T1 (prompt injection) or T6 (UX-state confusion)
      incidents traced to model-assisted output.** Grep for any
      unsanitized injection artifacts surviving into ledger /
      debug.log.

Fill in at session end:

| Session | Session ID | Date | Advisory fires | Regressed agrees / disagrees | L2 convergence | Cost | T1/T6 incidents | Verdict |
|---------|-----------|------|---------------:|------------------------------|---------------:|------|-----------------|---------|
| 1       |           |      |                |   /   = %                    | %              | $    |                 |   /     |
| 2       |           |      |                |   /   = %                    | %              | $    |                 |   /     |
| 3       |           |      |                |   /   = %                    | %              | $    |                 |   /     |

---

## Step 3 — The default-flip (only after 3 consecutive clean sessions)

Single flip — model-assisted from opt-in to default-on.

1. Edit `backend/core/ouroboros/governance/visual_verify.py`:

   Replace
   ```python
   return _env_truthy(
       os.environ.get("JARVIS_VISION_VERIFY_MODEL_ASSISTED_ENABLED", "false"),
   )
   ```
   with
   ```python
   return _env_truthy(
       os.environ.get("JARVIS_VISION_VERIFY_MODEL_ASSISTED_ENABLED", "true"),
   )
   ```

2. Update the test guard in
   `tests/governance/test_visual_verify_slice4_preflight.py::test_slice4_master_switch_default_off_in_source`
   — flip assertion to `"true"` and rename to
   `test_slice4_master_switch_graduated_to_true`.

3. Commit with message:
   ```
   feat(visual-verify): Slice 4 graduated — flip MODEL_ASSISTED_ENABLED default to true

   3-session arc:
     Session 1: <id> — <N> fires, <A>/<D> agree/disagree (<P>%), $<cost>
     Session 2: <id> — <N> fires, <A>/<D> agree/disagree (<P>%), $<cost>
     Session 3: <id> — <N> fires, <A>/<D> agree/disagree (<P>%), $<cost>

   All spec §Graduation Criteria → Slice 4 bullets met.

   Auto-demotion guardrail stays armed:
     - FP rate >= 50% on any post-graduation session auto-reverts to false
     - Operator clears via /verify-undemote
   ```

---

## Step 4 — Verify auto-demotion guardrail active (MANDATORY)

**Do not skip this step.** After the default-flip, intentionally
drive one more session with bad advisory performance and confirm the
auto-demotion guardrail fires. This is the Slice-4-unique test —
every other graduation has been "flip then trust"; Slice 4 adds
"flip then stress-test the safety net."

1. Force a high-FP session: run `/verify-confirm <op-id> disagree`
   on enough `regressed` verdicts to push the ledger's FP rate ≥50%.
2. End the session.
3. Verify the demotion flag appeared:
   ```bash
   ls -la .jarvis/vision_verify_model_assisted_demoted.flag
   cat .jarvis/vision_verify_model_assisted_demoted.flag | jq .
   ```
4. Next session boot should show:
   ```
   [Orchestrator] Visual VERIFY model-assisted DEMOTED — run /verify-undemote to re-arm
   ```
5. Run `/verify-undemote` — flag clears, next boot restores the
   graduated default.

If any of steps 3–5 fail, the graduation is **revoked** — revert the
default-flip commit and file a bug against the auto-demotion path.

---

## Step 5 — Document in OUROBOROS.md

Append a Slice 4 entry to `docs/architecture/OUROBOROS.md#battle-test-breakthrough-log`.
Include:

- The 3 session IDs.
- Advisory fire count + agree-rate per session.
- L2 convergence rate — proof the VLM's regressed calls were actually actionable.
- Demotion stress-test outcome (Step 4) — proof the safety net works.
- Honest caveats: single operator, macOS-only, specific screen-content distribution.

---

## Failure modes + recovery

| Symptom | Diagnosis | Fix |
|---------|-----------|-----|
| Boot log lacks "model-assisted active" line | `MODEL_ASSISTED_ENABLED` env not propagated OR demotion flag present | Check env in the harness shell; `ls .jarvis/vision_verify_model_assisted_demoted.flag` |
| Advisory fires but never records to ledger | Orchestrator not calling `led.record_advisory` | Prerequisite wiring Step 1 |
| `/verify-confirm` returns "no advisory entry" | Wrong op_id, or advisory was skipped (check `adv.reason` in debug.log) | Use the exact op_id from the `signal_emitted` log line |
| Auto-demotion doesn't fire at end-of-session | `check_and_apply_auto_demotion` not wired at session end | Prerequisite wiring Step 2 |
| Advisory triggers L2 but op never converges | VLM hallucinating regressions L2 can't fix — FP rate rising | Run more `disagree` confirmations; auto-demotion will fire at 50% |
| Ledger file contains raw reasoning text | Sanitization regressed | Run `test_ledger_reasoning_hash_replaces_reasoning_text` from the module spine |

---

## Why not automated?

Same rationale as previous slices: the ≥60% agreement criterion is a
human judgement call — whether the VLM's `regressed` verdict was
"actually a regression" depends on the operator's mental model of
what the op was supposed to do. The pre-flight covers every
mechanical code path; the 3-session arc is the dataset for tuning
the confidence threshold and verifying the auto-demotion guardrail
isn't over- or under-sensitive on the operator's actual usage pattern.
