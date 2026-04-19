# VisionSensor — Slice 1 Graduation Checklist

**Task 14 of the VisionSensor + Visual VERIFY arc.**

**Status (as of this doc's creation)**: pre-flight PASSED (synthetic
integration smoke test green). 3-session real-world arc: **not yet
started**.

**Spec**: `docs/superpowers/specs/2026-04-18-vision-sensor-verify-design.md`
§Graduation Criteria → Slice 1.
**Plan**: `docs/superpowers/plans/2026-04-18-vision-sensor-verify.md`
Task 14.

---

## Scope

Slice 1 = **deterministic-only sensor** (Tier 0 dhash dedup + Tier 1
OCR regex). Tier 2 VLM is Slice 2 (Task 15); Visual VERIFY is Slice 3
(Task 17). **Vision-originated ops always require human approval in
this slice** — we are not yet trusting the sensor to auto-apply.

---

## Pre-flight — autonomous, runs in seconds

A synthetic integration smoke test drives the full Task 1–13 stack
through scripted Ferrari frames:

```bash
python3 -m pytest tests/governance/intake/sensors/test_vision_sensor_slice1_preflight.py -v
```

**Pass = 6/6**. If this is red, do not proceed to real sessions —
fix the code path first.

The pre-flight exercises:

1. Clean screen → no signal.
2. Traceback → signal emitted, schema v1 evidence shape correct.
3. Repeat dhash → Tier 0 dedup.
4. Same verdict+app, different dhash → Task 11 finding cooldown.
5. Different verdict (linter_red) → second signal.
6. Credential in OCR (`sk-*`) → whole frame dropped (T2c).
7. Prompt injection in OCR → signal emitted with `[sanitized:prompt_injection_detected]` snippet (T1).
8. Denylisted app (`com.1password.mac`) → dropped before OCR (T2ab).

Plus retention dir lifecycle + ledger persistence + Slice 1 env
config honoured by the constructor.

---

## Slice 1 runtime configuration

Set these exact env vars before each of the 3 sessions:

```bash
export JARVIS_VISION_SENSOR_ENABLED=true
export JARVIS_VISION_SENSOR_TIER2_ENABLED=false
export JARVIS_VISION_CHAIN_MAX=1
# Optional: tighten retention for the graduation arc
export JARVIS_VISION_FRAME_TTL_S=600        # 10 minutes
export JARVIS_VISION_SENSOR_FP_BUDGET=0.3
export JARVIS_VISION_SENSOR_FINDING_COOLDOWN_S=120
```

**Do NOT flip** the `JARVIS_VISION_SENSOR_ENABLED` default in the
source until Step 5 of this checklist. The env export is per-session.

---

## Session runbook

Each of the 3 graduation sessions follows the same shape.

### Boot

```bash
python3 scripts/ouroboros_battle_test.py --cost-cap 0.50 --idle-timeout 600 -v
```

Within 60s of boot, confirm the boot log contains:

```
[IntakeLayer] VisionSensor registered enabled=true tier2=false chain_max=1 session_id=vis-sess-...
```

If you don't see this line, the master switch didn't propagate — fix
env before continuing.

### What to drive in each session

| Session | Purpose | What the operator does |
|---------|---------|------------------------|
| **1** | Force failures — verify emission rate | Open deliberately-broken screens: pytest red, compile errors in editor, modal "Error" dialogs. Keep one terminal visible with a failing test loop. |
| **2** | Normal work — verify FP rate | Run a typical coding session. Ignore the sensor; do whatever the day's actual work is. |
| **3** | Privacy + credential-leak verification | Reproduce "had a credential visible" scenarios: terminal with `AWS_SECRET_ACCESS_KEY=AKIA...`, 1Password briefly visible. Confirm sensor drops, never forwards. |

### Observability during the session

Watch the session's `.ouroboros/sessions/<session-id>/debug.log` for:

**Session 1 must-see tokens**:
- `[VisionSensor] signal_emitted op_id=...` — at least 5 distinct entries.
- Each entry has `source=vision_sensor` and one of `classifier=deterministic verdict=error_visible` or `verdict=bug_visible`.

**Session 2 must-see tokens**:
- Same signal_emitted lines, but fewer (normal work doesn't generate many errors).
- `dropped_finding_cooldown` counter grows (same errors reappearing).
- `dropped_hash_dedup` counter grows (static screens).

**Session 3 must-see tokens**:
- `dropped_credential_shape` > 0 for any credential-visible period.
- `dropped_app_denied` > 0 when 1Password is focused.
- **Zero** raw credential strings (e.g. `sk-`, `AKIA`, `ghp_`) in `debug.log`.
  Grep: `grep -E 'sk-[A-Za-z0-9_\-]{20,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20,}' debug.log` — **must be empty**.

### At end of each session

```bash
# Session retention dir was purged on shutdown (atexit + SIGTERM):
ls -la .jarvis/vision_frames/<session-id>/ 2>&1
# Expected: "No such file or directory"

# FP ledger state (outcomes + cooldowns + streak):
cat .jarvis/vision_sensor_fp_ledger.json | jq .

# Quick rollup of sensor behaviour from debug.log:
grep -E '\[VisionSensor\]' .ouroboros/sessions/<session-id>/debug.log | \
    awk -F'[ =]' '/signal_emitted|dropped_/ {print $1, $2, $3}' | \
    sort | uniq -c | sort -rn
```

---

## Graduation criteria (from spec §Graduation Criteria → Slice 1)

Each of the **3 consecutive** sessions must satisfy **every** criterion
below. A single failure in any session resets the arc to zero — the
next attempt is Session 1 again.

- [ ] **Sensor emits ≥5 signals per session.**
- [ ] **Human accepts (clicks approve on Orange PR) ≥70% of emitted
      signals.** Track in `.jarvis/vision_sensor_approval_log.md`
      (manual ledger — one line per op).
- [ ] **Zero credential-shape leaks** — grep above returns empty.
      `grep "[REDACTED]"` allowed (that means the firewall did its
      job); raw credential strings forbidden.
- [ ] **Zero stale-signal wins** — pre-APPLY re-capture caught any
      stale signals. Check for `reason_code=vision_signal_stale`
      cancellations in the session log; any such cancellation counts
      as the mitigation working, not a failure. **Zero ops applied
      on a stale frame.**
- [ ] **Daily cost ≤ $0.05** (deterministic-only should be near zero).
      Check the ops cost ledger at session end.
- [ ] **FP budget ledger never exhausted** — sensor never auto-paused
      with `reason=fp_budget_exhausted`.

Fill in the table below at session end:

| Session | Session ID | Date | Signals emitted | Human accepts | Credential leaks | Stale wins | Cost | FP pauses | Verdict |
|---------|-----------|------|----------------:|---------------|------------------|------------|------|-----------|---------|
| 1       |           |      |                 |  / =          |                  |            | $    |           |   /     |
| 2       |           |      |                 |  / =          |                  |            | $    |           |   /     |
| 3       |           |      |                 |  / =          |                  |            | $    |           |   /     |

---

## Step 5 — The default-flip (only after 3 consecutive clean sessions)

Once all three sessions pass every criterion, perform the flip:

1. Edit `backend/core/ouroboros/governance/intake/intake_layer_service.py`:

   Replace
   ```python
   if _env_truthy(os.environ.get("JARVIS_VISION_SENSOR_ENABLED", "false")):
   ```
   with
   ```python
   if _env_truthy(os.environ.get("JARVIS_VISION_SENSOR_ENABLED", "true")):
   ```

2. Update the test guard in `tests/governance/intake/sensors/test_vision_sensor_slice1_preflight.py::test_slice1_master_switch_default_off`
   — it currently asserts the default is still `"false"`. Flip to
   `"true"` and rename to `test_slice1_master_switch_graduated_to_true`.

3. Commit with message
   ```
   feat(vision-sensor): Slice 1 graduated — flip JARVIS_VISION_SENSOR_ENABLED default to true

   3-session arc:
     Session 1: <session-id> — <N> signals, <M>% human-accept, $<cost>
     Session 2: <session-id> — <N> signals, <M>% human-accept, $<cost>
     Session 3: <session-id> — <N> signals, <M>% human-accept, $<cost>

   All spec §Graduation Criteria → Slice 1 bullets met.
   ```

---

## Step 6 — Document in OUROBOROS.md

Append an entry to `docs/architecture/OUROBOROS.md#battle-test-breakthrough-log`
using the template of the prior Session W / Slice entries. Include:

- The 3 session IDs.
- The exact operator-visible INFO log line(s) on boot.
- Headline numbers: signals emitted, human accepts, cost.
- One "what's proven" paragraph and one "what's NOT yet proven" paragraph
  (honest caveats — e.g. "Tier 2 VLM is still off", "Visual VERIFY
  not yet integrated", "single operator on a single machine").

---

## Failure modes + recovery

| Symptom | Diagnosis | Fix |
|---------|-----------|-----|
| Boot log says `enabled=false` | Env not exported into harness shell | `export` in the same shell that runs the harness, or use `env VAR=… python3 scripts/…` |
| `[IntakeLayer] VisionSensor skipped (construction error)` | One of Tasks 1–12 regressed | Re-run `tests/governance/test_vision_threat_model.py` + `tests/governance/intake/sensors/test_vision_sensor*.py` locally |
| `dropped_ferrari_absent=N, N polls` | Ferrari not running | Start VisionCortex first (see `backend/vision/realtime/vision_cortex.py`) — sensor is a read-only consumer (I8) |
| FP ledger auto-pauses on Session 1 | Finding-cooldown state inherited from a pre-flight test | `rm .jarvis/vision_sensor_fp_ledger.json` before session start |
| Credential string in `debug.log` | T2c regression | **STOP**. Do not continue graduation. Raise at `sensor.stats.dropped_credential_shape == 0` and open a bug. |

---

## Why not automated?

Slice 1 specifically requires **human judgement** at one criterion:
"human accepts ≥70% of emitted signals". An op is only correctly
classified as a true positive if the human agrees the signal was
worth acting on. Until an automated judge exists for "was this
screen *really* an actionable bug", graduation has to sit on a real
operator's desk.

The pre-flight (`test_vision_sensor_slice1_preflight.py`) exercises
everything the arc can exercise without that judgement call. Run it
before every session boot — it's cheap and catches any integration
regression from code landed between Tasks 1–13 and the current
attempt.
