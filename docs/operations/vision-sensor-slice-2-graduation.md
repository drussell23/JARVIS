# VisionSensor — Slice 2 Graduation Checklist

**Task 16 of the VisionSensor + Visual VERIFY arc.**

**Status (as of this doc's creation)**: pre-flight PASSED (Tier 2
integration smoke test green). 3-session real-world arc: **not yet
started**. Prerequisite — Slice 1 graduated with
`JARVIS_VISION_SENSOR_ENABLED` already flipped to `true` in source.

**Spec**: `docs/superpowers/specs/2026-04-18-vision-sensor-verify-design.md`
§Graduation Criteria → Slice 2.
**Plan**: `docs/superpowers/plans/2026-04-18-vision-sensor-verify.md`
Task 16.

---

## Scope

Slice 2 = **Tier 2 VLM classifier enabled** on top of the Slice 1
deterministic sensor. Per-app scope enforcement, FP budget tracker
live, cost caps enforced. **Chain cap still `1`** — the operator
gets one governed chain per session; the bump to `3` is part of
this task's graduation (Step 3).

**Visual VERIFY remains OFF** in this slice (that's Slice 3 / Task 17).

---

## Pre-flight — autonomous, runs in ~2 seconds

```bash
python3 -m pytest tests/governance/intake/sensors/test_vision_sensor_slice2_preflight.py -v
```

**Pass = 7/7**. If this is red, do not proceed to real sessions.

Exercises (via mock VLM callable driving the real Task 15 machinery):

1. Tier 1 quiet + VLM returns `bug_visible` hi-conf → signal, urgency=low.
2. Tier 1 matched + VLM enabled → VLM skipped.
3. VLM `ok` → no signal emitted (clean drop).
4. VLM confidence 0.5 → severity downgraded to `info`.
5. Cost cascade: 80% → VLM skipped (downshift), 95% → sensor pauses
   with `cost_cap_exhausted`.
6. UTC rollover clears the cost-cap pause.
7. T2ab app denylist + T2c credential-shape drop both fire **before**
   VLM runs (no wasted VLM spend on denied/leaky frames).
8. VLM reasoning with injection phrase → `[sanitized:prompt_injection_detected]`.
9. VLM exception → swallowed, counter bumps.
10. Cost ledger persists across restart.
11. Master-switch defaults in source still `false`/`1` (flip is Step 3).

---

## Slice 2 runtime configuration

Build on the Slice-1-graduated baseline (`JARVIS_VISION_SENSOR_ENABLED`
already `true`), then enable Tier 2 for this arc:

```bash
# Slice 1 stays on — graduated default
unset JARVIS_VISION_SENSOR_ENABLED         # default is now true after Slice 1 Step 5

# Slice 2 additions
export JARVIS_VISION_SENSOR_TIER2_ENABLED=true
export JARVIS_VISION_CHAIN_MAX=1            # still 1 — bump is the Slice 2 flip
export JARVIS_VISION_TIER2_COST_USD=0.005   # Qwen3-VL-235B pricing
export JARVIS_VISION_DAILY_COST_CAP_USD=1.00
export JARVIS_VISION_SENSOR_MIN_CONFIDENCE=0.70

# Required — Task 15 didn't wire this in yet; the operator has to
# provide a VLM callable. For Slice 2 the lean_loop path is the
# target; the default sensor has ``vlm_fn=None`` so no VLM fires
# until wiring lands. If wiring isn't in place yet, Slice 2
# graduation is blocked — see "Prerequisite wiring" below.
```

**Do NOT flip** the `JARVIS_VISION_SENSOR_TIER2_ENABLED` or
`JARVIS_VISION_CHAIN_MAX` defaults in the source until Step 3.

---

## Prerequisite wiring (if not already done)

Task 15 made Tier 2 available via an injectable `vlm_fn` kwarg, but
the production intake-layer wiring (`IntakeLayerService` in Task 13)
doesn't yet construct a VLM callable. Before Session 1:

1. Decide the VLM adapter: thin wrapper around `backend/vision/lean_loop.py`'s
   `see_and_respond()` or equivalent, returning the dict shape
   `{"verdict": ..., "confidence": ..., "model": ..., "reasoning": ...}`.
2. Inject it at `VisionSensor(...)` construction in `IntakeLayerService`:
   ```python
   _vision_sensor = VisionSensor(
       router=self._router,
       repo="jarvis",
       vlm_fn=_make_lean_loop_vlm_adapter(),   # NEW
   )
   ```
3. Re-run Slice 1 pre-flight (`test_vision_sensor_slice1_preflight.py`)
   to confirm no regression.

This wiring is small and self-contained; I flagged it as a
prerequisite rather than baking it into Task 15 because the real
adapter shape depends on the lean_loop API surface and is best
decided at wiring time.

---

## Session runbook

### Boot

```bash
python3 scripts/ouroboros_battle_test.py --cost-cap 0.50 --idle-timeout 600 -v
```

Within 60s, confirm the boot log contains:

```
[IntakeLayer] VisionSensor registered enabled=true tier2=true chain_max=1 session_id=vis-sess-...
```

The `tier2=true` token is new in Slice 2 — if you see `tier2=false`,
your env wasn't picked up.

### What to drive in each session

Slice 2 sessions look like Slice 1 sessions, but with Tier 2
actively firing on screens that *look* buggy without a literal
regex-matchable error string (e.g. red underlines without visible
text, modal dialogs in foreign languages, visually-stuck progress
bars).

| Session | Purpose | What the operator does |
|---------|---------|------------------------|
| **1** | VLM fires ≥5 times | Drive scenarios where Tier 1 stays quiet but something visually wrong is on screen: non-English error modals, red icon-only buttons, rendering glitches. |
| **2** | FP rate discipline | Normal working day, ignore the sensor. Goal: ≥70% of fired signals are real issues the human would have wanted to see. |
| **3** | Cost envelope | Genuinely use the app all day. Cost must stay ≤ $0.50/session. If it climbs, the VLM is firing too eagerly and the `JARVIS_VISION_TIER2_DHASH_DISTANCE` / cooldown config needs tightening before graduation. |

### Observability during the session

Watch for these tokens in `.ouroboros/sessions/<session-id>/debug.log`:

**Tier 2 firing**:
```
[VisionSensor] signal_emitted op_id=... source=vision_sensor classifier=qwen3-vl-235b verdict=bug_visible
```
(vs Slice 1's `classifier=deterministic`).

**Tier 2 gated**:
```
tier2_skipped_disabled tier2_skipped_tier1_matched tier2_skipped_cost_downshift tier2_skipped_dhash_dedup
```

**Cost cascade**:
```
[VisionSensor] paused reason=cost_cap_exhausted
```
If this fires, the sensor hit 95% of the daily cap. The remainder
of the session has the sensor paused; `/vision resume` is available
(REPL, Task 21) or wait for UTC midnight.

### At end of each session

```bash
# Cost ledger snapshot
cat .jarvis/vision_cost_ledger.json | jq .

# Signal-source breakdown (Tier 1 vs Tier 2)
grep 'signal_emitted' .ouroboros/sessions/<session-id>/debug.log | \
    awk -F'classifier=' '{print $2}' | awk '{print $1}' | sort | uniq -c

# Cost-cap pause events
grep 'cost_cap_exhausted' .ouroboros/sessions/<session-id>/debug.log | wc -l
```

---

## Graduation criteria (spec §Slice 2)

Each of the **3 consecutive** sessions must satisfy **every** criterion.
A single failure resets the arc.

- [ ] **Tier 2 fires ≥5 times per session.** Grep `classifier=qwen3-vl-235b`
      lines in the session debug.log.
- [ ] **FP rate < 30% across rolling 20-op window.** The sensor's own
      FP budget tracker (`.jarvis/vision_sensor_fp_ledger.json`) surfaces
      this; any `fp_budget_exhausted` pause event fails the criterion.
- [ ] **Zero T5 sensor-loop attempts (chain cap of 1 proved restrictive
      enough — no session hit the cap).** No `chain_cap_exhausted`
      pause events in any session. If even one session pauses on chain
      cap, Slice 2 fails — the sensor is emitting too many
      sessions-originated ops, and Slice 3 (bump to `chain_max=3`) is
      premature.
- [ ] **No FORBIDDEN_APP / denylist violation.** Grep session log for
      denied-app frame bytes or credential patterns — must be empty.
- [ ] **Total vision-originated op cost ≤ $0.50 / session.** Check
      `.jarvis/vision_cost_ledger.json` at session end.
- [ ] **Visual VERIFY still off.** Slice 3's scaffolding must not have
      been enabled prematurely.

Fill in at session end:

| Session | Session ID | Date | Tier 2 fires | FP rate | Chain-cap hits | Cost | Denylist violations | Verdict |
|---------|-----------|------|-------------:|---------|----------------|------|---------------------|---------|
| 1       |           |      |              |    %    |                | $    |                     |   /     |
| 2       |           |      |              |    %    |                | $    |                     |   /     |
| 3       |           |      |              |    %    |                | $    |                     |   /     |

---

## Step 3 — The dual default-flip (only after 3 consecutive clean sessions)

Both flips land together per spec:

1. Edit `backend/core/ouroboros/governance/intake/sensors/vision_sensor.py`:

   Replace
   ```python
   _DEFAULT_TIER2_ENABLED = os.environ.get(
       "JARVIS_VISION_SENSOR_TIER2_ENABLED", "false",
   ).strip().lower() in ("1", "true", "yes", "on")
   ```
   with
   ```python
   _DEFAULT_TIER2_ENABLED = os.environ.get(
       "JARVIS_VISION_SENSOR_TIER2_ENABLED", "true",
   ).strip().lower() in ("1", "true", "yes", "on")
   ```

   And replace
   ```python
   _DEFAULT_CHAIN_MAX = int(os.environ.get("JARVIS_VISION_CHAIN_MAX", "1"))
   ```
   with
   ```python
   _DEFAULT_CHAIN_MAX = int(os.environ.get("JARVIS_VISION_CHAIN_MAX", "3"))
   ```

2. Update the test guards in
   `tests/governance/intake/sensors/test_vision_sensor_slice2_preflight.py::test_slice2_master_switches_default_off_in_source`
   and
   `test_slice2_chain_max_default_still_one`:
   flip both to assert the graduated defaults (`"true"` / `3`).

3. Commit with message:
   ```
   feat(vision-sensor): Slice 2 graduated — flip TIER2_ENABLED + CHAIN_MAX defaults

   3-session arc:
     Session 1: <session-id> — <N> Tier 2 fires, <M>% FP rate, $<cost>
     Session 2: <session-id> — <N> Tier 2 fires, <M>% FP rate, $<cost>
     Session 3: <session-id> — <N> Tier 2 fires, <M>% FP rate, $<cost>

   All spec §Graduation Criteria → Slice 2 bullets met.

   New defaults:
     - JARVIS_VISION_SENSOR_TIER2_ENABLED: false → true
     - JARVIS_VISION_CHAIN_MAX: 1 → 3 (post-trust ceiling per D3)
   ```

---

## Step 4 — Document in OUROBOROS.md

Append a Slice 2 entry to `docs/architecture/OUROBOROS.md#battle-test-breakthrough-log`
alongside Slice 1's. Include:

- The 3 session IDs.
- Per-session Tier 2 fire count + FP rate + cost.
- One "what's proven" paragraph (Tier 2 VLM economics viable at scale,
  chain cap of 3 sufficient, FP discipline holds under VLM noise).
- One "what's NOT yet proven" paragraph (Visual VERIFY still off,
  auto-apply still forbidden, no cross-operator validation).

---

## Failure modes + recovery

| Symptom | Diagnosis | Fix |
|---------|-----------|-----|
| Boot log says `tier2=false` despite `TIER2_ENABLED=true` export | Env not in the harness's shell, or `_DEFAULT_TIER2_ENABLED` evaluated at import before export | Set env system-wide (shell profile) OR pass `--env` flag to the harness wrapper |
| `tier2_calls=0` across a whole session | `vlm_fn` not wired in `IntakeLayerService` | See "Prerequisite wiring" above |
| Cost > $0.50 in any session | VLM firing too often — `_last_tier2_dhash` dedup not catching similar frames | Tighten `hash_cooldown_s` or add a dhash-distance threshold (future enhancement); drop the session and retry |
| `chain_cap_exhausted` pause in any session | Sensor emitting too many actionable ops | **Fails Slice 2.** Return to Slice 1 discipline; investigate what the sensor is seeing in the test op-chain. |
| `fp_budget_exhausted` pause | FP rate > 30% | **Fails Slice 2.** Too many VLM false positives — raise `JARVIS_VISION_SENSOR_MIN_CONFIDENCE` before retrying. |
| `.jarvis/vision_cost_ledger.json` shows spend accumulated across days | UTC rollover logic silently failing | Inspect `_load_cost_ledger` + `_maybe_rollover_cost_ledger` in debug.log |

---

## Why not automated?

Same rationale as Slice 1: "≥5 Tier 2 fires" and "FP rate < 30%" are
human-judgement bounds. "What counts as a bug worth firing on" is
culture-dependent and requires the operator. The pre-flight (above)
exercises every integration surface the real VLM would hit, short of
the non-deterministic VLM output itself.
