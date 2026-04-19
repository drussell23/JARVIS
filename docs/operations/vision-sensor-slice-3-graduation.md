# Visual VERIFY — Slice 3 Graduation Checklist

**Task 18 of the VisionSensor + Visual VERIFY arc.**

**Status (as of this doc's creation)**: pre-flight PASSED (Visual
VERIFY deterministic end-to-end green, 26/26). 3-session real-world
arc: **not yet started**.

**Spec**: `docs/superpowers/specs/2026-04-18-vision-sensor-verify-design.md`
§Graduation Criteria → Slice 3.
**Plan**: `docs/superpowers/plans/2026-04-18-vision-sensor-verify.md`
Task 18.

---

## Scope

Slice 3 = **Visual VERIFY deterministic-only** on UI-affected ops.
The deterministic battery (app-liveness + pixel-variance + dhash-
distance sanity) runs between existing VERIFY and COMPLETE for any
op that matches the Trigger rules (§VERIFY Extension D2). **Model-
assisted advisory is OFF** throughout this slice — that's Slice 4.

Prerequisites:
- Slices 1 + 2 already graduated.
- `visual_verify.py` module shipped (Task 17).
- Orchestrator FSM wiring landed (see "Prerequisite wiring" below).

---

## Pre-flight — autonomous, ~2 seconds

```bash
python3 -m pytest tests/governance/test_vision_sensor_slice3_preflight.py -v
python3 -m pytest tests/governance/test_visual_verify.py -v
```

**Pass = 26/26 preflight + 68/68 module**. If either is red, do not
proceed.

Exercises (via injectable probes — no real Quartz / PIL calls):

1. UI op + healthy pre/post frames → `deterministic_pass`.
2. Blank post frame → fail `blank_screen`.
3. Identical pre/post → fail `hash_unchanged`.
4. Crashed app probe → fail `app_crashed`.
5. TestRunner red + Visual VERIFY pass → **clamped to fail** (I4
   asymmetry enforcement).
6. Backend file → skipped, reason `not_ui_affected`.
7. Tertiary trigger (unclassifiable + zero tests + approval_required).
8. Missing pre/post → skipped gracefully.
9. Master switch env default is `false` in source.

---

## Slice 3 runtime configuration

Build on the Slice-1-and-2-graduated baseline, then enable Visual
VERIFY for this arc:

```bash
# Slice 1+2 already-graduated defaults
unset JARVIS_VISION_SENSOR_ENABLED
unset JARVIS_VISION_SENSOR_TIER2_ENABLED
unset JARVIS_VISION_CHAIN_MAX

# Slice 3 additions
export JARVIS_VISION_VERIFY_ENABLED=true
# Model-assisted stays OFF — that's Slice 4.
unset JARVIS_VISION_VERIFY_MODEL_ASSISTED_ENABLED

# Optional tunables
export JARVIS_VISION_VERIFY_MIN_VARIANCE=0.01
export JARVIS_VISION_VERIFY_HASH_DIST_MIN=0.0
export JARVIS_VISION_VERIFY_HASH_DIST_MAX=0.9
export JARVIS_VISION_VERIFY_RENDER_DELAY_S=2.0
```

**Do NOT flip** the `JARVIS_VISION_VERIFY_ENABLED` default in the
source until Step 3.

---

## Prerequisite wiring (if not already done)

Task 17 shipped `visual_verify.py` as a self-contained module with a
clean `run_if_triggered(...)` API. The orchestrator FSM integration
is a separate small change (deliberately deferred from Task 17 to
keep its blast radius small). Before Session 1:

1. **Add `VISUAL_VERIFY` to `OperationPhase`** in
   `backend/core/ouroboros/governance/op_context.py`:

   ```python
   class OperationPhase(Enum):
       ...
       VERIFY = auto()
       VISUAL_VERIFY = auto()   # NEW
       COMPLETE = auto()
       ...
   ```

2. **Wire transitions** in the same file's `PHASE_TRANSITIONS`:

   ```python
   PHASE_TRANSITIONS[OperationPhase.VERIFY].add(OperationPhase.VISUAL_VERIFY)
   PHASE_TRANSITIONS[OperationPhase.VISUAL_VERIFY] = {
       OperationPhase.COMPLETE,
       OperationPhase.VALIDATE_RETRY,   # L2 routes fails
   }
   ```

3. **Hook `run_if_triggered` into the orchestrator's VERIFY
   handler** (`backend/core/ouroboros/governance/orchestrator.py` —
   search for the existing VERIFY phase handler):

   ```python
   from backend.core.ouroboros.governance.visual_verify import (
       run_if_triggered, visual_verify_enabled,
   )
   # After TestRunner returns a ValidationResult:
   if visual_verify_enabled():
       vv_result = run_if_triggered(
           target_files=ctx.target_files,
           attachments=ctx.attachments,
           plan_ui_affected=_extract_ui_affected(ctx),
           test_targets_resolved=validation.adapter_names_run,
           risk_tier=ctx.risk_tier.name.lower() if ctx.risk_tier else "",
           test_runner_result=(
               "failed" if validation and not validation.passed else "passed"
           ),
       )
       # Failures route to L2 same as TestRunner failures.
   ```

4. **Pre/post frame capture** at GENERATE-start and APPLY-success +
   `render_delay_s`. The `Attachment` substrate is already in place
   (Task 1); orchestrator adds via `ctx.add_attachment(...)`.

5. Re-run the full `tests/governance/test_visual_verify.py` +
   `test_vision_sensor_slice3_preflight.py` (should stay green —
   wiring changes shouldn't affect pure-function behavior).

---

## Session runbook

### Boot

```bash
python3 scripts/ouroboros_battle_test.py --cost-cap 0.50 --idle-timeout 600 -v
```

Within 60s, confirm the boot log contains:

```
[IntakeLayer] VisionSensor registered enabled=true tier2=true chain_max=3 session_id=...
[Orchestrator] Visual VERIFY phase active (JARVIS_VISION_VERIFY_ENABLED=true)
```

(The second line assumes the orchestrator wiring logs a boot banner —
add this during prerequisite wiring if absent.)

### What to drive in each session

Slice 3 sessions focus on **UI-affected ops reaching Visual VERIFY**.
The real-world value shows up when the TestRunner is silent (no
tests for the changed files) but something visually broke.

| Session | Purpose | What the operator does |
|---------|---------|------------------------|
| **1** | Prove the phase fires | Make deliberate frontend edits (e.g. `src/Button.tsx`). Ensure TestRunner says green. Verify Visual VERIFY runs on each op. Goal: ≥3 UI-affected ops reach the phase. |
| **2** | Prove zero false rejections | Normal UI coding work. Visual VERIFY must not fail a working op — any `fail` verdict requires operator review. |
| **3** | Prove a regression caught | Deliberately ship a broken UI change that passes tests but visibly breaks rendering (e.g. wrong CSS var that leaves a blank panel). Visual VERIFY's `blank_screen` / `hash_unchanged` check must fire. |

### Observability during the session

Watch for these tokens in `.ouroboros/sessions/<session-id>/debug.log`:

**Visual VERIFY firing**:
```
[VisualVerify] trigger=ui_files
[VisualVerify] deterministic_pass pre_hash=... post_hash=... hash_distance=0.42
```

**Visual VERIFY failing**:
```
[VisualVerify] deterministic_fail check=blank_screen post_variance=0.003 < min_variance=0.01
[VisualVerify] deterministic_fail check=hash_unchanged distance=0.0000
[VisualVerify] deterministic_fail check=app_crashed app_id=com.apple.Safari
```

**Visual VERIFY skipped (backend op)**:
```
[VisualVerify] skipped trigger=not_ui_affected
```

**I4 asymmetry fire**:
```
[VisualVerify] clamped_pass_to_fail reason=I4_asymmetry testrunner=failed
```

### At end of each session

```bash
# Count Visual VERIFY fires
grep -E '\[VisualVerify\]' .ouroboros/sessions/<session-id>/debug.log | \
    awk '{print $2}' | sort | uniq -c

# Count distinct check outcomes
grep 'deterministic_fail check=' .ouroboros/sessions/<session-id>/debug.log | \
    awk -F'check=' '{print $2}' | awk '{print $1}' | sort | uniq -c

# Verify no false rejection — for each fail, check whether the op
# was genuinely broken. Record verdict in
# .jarvis/vision_verify_review_log.md (manual ledger, one line per fail).
```

---

## Graduation criteria (spec §Slice 3)

Each of the **3 consecutive** sessions must satisfy **every** criterion.

- [ ] **At least 3 UI-affected ops reach Visual VERIFY per session.**
      Count `[VisualVerify] trigger=ui_files` (or `plan_ui_affected` /
      `zero_test_coverage`) lines per session.
- [ ] **Zero false rejections.** Any `deterministic_fail` must
      correspond to a genuinely-broken op. Record the review verdict
      in the manual ledger.
- [ ] **≥1 session catches a regression TestRunner missed.** The
      deliberate "broken UI, tests pass" scenario must trigger a
      `blank_screen` / `hash_unchanged` / `app_crashed` fail.
- [ ] **Daily cost ≤ $1.00** — deterministic-only should be near
      zero (no VLM calls from Visual VERIFY in this slice). Any
      cost came from Tier 2 sensor emissions (Slice 2 cost cap).
- [ ] **No regression in the non-UI-affected skip path.** Backend
      ops must still reach COMPLETE normally.

Fill in at session end:

| Session | Session ID | Date | UI ops reached VERIFY | False rejections | Regression caught | Daily cost | Verdict |
|---------|-----------|------|----------------------:|-----------------:|-------------------|-----------:|---------|
| 1       |           |      |                       |                  |                   | $          |   /     |
| 2       |           |      |                       |                  |                   | $          |   /     |
| 3       |           |      |                       |                  |                   | $          |   /     |

---

## Step 3 — The default-flip (only after 3 consecutive clean sessions)

Single flip this time — model-assisted stays OFF (Slice 4).

1. Edit `backend/core/ouroboros/governance/visual_verify.py`:

   Replace
   ```python
   return _env_truthy(os.environ.get("JARVIS_VISION_VERIFY_ENABLED", "false"))
   ```
   with
   ```python
   return _env_truthy(os.environ.get("JARVIS_VISION_VERIFY_ENABLED", "true"))
   ```

2. Update the test guard in
   `tests/governance/test_vision_sensor_slice3_preflight.py::test_slice3_master_switch_default_off_in_source`
   — flip the assertion to `"true"` and rename to
   `test_slice3_master_switch_graduated_to_true`.

3. Commit with message:
   ```
   feat(visual-verify): Slice 3 graduated — flip JARVIS_VISION_VERIFY_ENABLED default to true

   3-session arc:
     Session 1: <session-id> — <N> UI ops reached VERIFY, 0 false rejects
     Session 2: <session-id> — <N> UI ops reached VERIFY, 0 false rejects
     Session 3: <session-id> — <N> UI ops reached VERIFY, caught <specific regression>

   All spec §Graduation Criteria → Slice 3 bullets met.
   Model-assisted advisory stays off (Slice 4 scope).
   ```

---

## Step 4 — Document in OUROBOROS.md

Append a Slice 3 entry to `docs/architecture/OUROBOROS.md#battle-test-breakthrough-log`.
Highlight the regression caught in Session 3 — that's the proof this
slice paid for itself.

---

## Failure modes + recovery

| Symptom | Diagnosis | Fix |
|---------|-----------|-----|
| Boot log lacks Visual VERIFY banner | Wiring not in place | See "Prerequisite wiring" — Steps 1–3 above. |
| `ctx.attachments` empty on UI ops | Pre/post frame capture not wired | Orchestrator needs to call `ctx.add_attachment(...)` at GENERATE-start + post-APPLY+delay. |
| False rejection on a working op | Variance threshold too tight OR render_delay_s too short | Raise `JARVIS_VISION_VERIFY_MIN_VARIANCE` or `RENDER_DELAY_S` for next session. |
| `CHECK_HASH_SCRAMBLED` fires on every op | Default `hash_distance_fn` returns 1.0 for any diff — real dhash needed | Inject a proper dhash distance fn at orchestrator call site (Hamming distance / 64). |
| App-liveness probe always returns True | Default `app_alive_fn` is permissive stub | Wire a Quartz `CGWindowListCopyWindowInfo` probe in the orchestrator call site. |
| I4 clamp fires on every op | TestRunner returning "failed" verbatim when it means something else | Check `validation.passed` boolean path, not string — I4 clamp only fires on string red values. |

---

## Why not automated?

Slice 3's graduation pivots on "did Visual VERIFY catch a regression
TestRunner missed?" — a *semantic* pass/fail judgement that depends
on what the operator considers a regression. The pre-flight and the
68-test module suite cover every mechanical path through the code;
the remaining unknown is whether the deterministic thresholds
(variance + hash distance) tune correctly against the operator's
real screens. Three sessions of tuning + observation is the minimum
dataset.
