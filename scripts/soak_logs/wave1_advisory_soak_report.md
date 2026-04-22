# Wave 1 Advisory Soak Report (pre-enforcement wiring)

**Soak conductor: agent**
**Date:** 2026-04-21
**Repo HEAD at soak start:** `472a02459c` (Track A seed-sync landed)
**Probe commit (part b evidence):** `3d3212d5a4`
**Wave 1 graduation commits:** `d0dd4f2346` (DirectionInferrer), `64494d0a5b` (FlagRegistry), `75d3a9f048` (SensorGovernor + MemoryPressureGate)

## Soak mode

Per operator delegation (`feedback_agent_conducted_soak_delegation.md`): agent ran mechanical headless battle-test sessions. $0.50 hard cost cap per session. `idle-timeout=180` seconds to keep run time bounded. 2 sessions run; session 3 skipped by data-driven decision (signal replicated at N=2).

## Session 1

- Session ID: `bt-2026-04-22-024636`
- Commit at session start: `472a02459c`
- Any flag forced off? **none**
- Duration / cost: **204.3s / $0.078** (well under $0.50 cap)
- Stop reason: `idle_timeout`
- Ops observed: 3 generated (1 cost-bearing at $0.030 in GENERATE phase); `strategic_drift.total_ops=3`
- Posture timeline: **not observable** — PostureObserver did not start during session; `.jarvis/posture_current.json` not written
- `/help flags --posture <X>` — not exercised in session (requires REPL/TTY, operator-present)
- `/governor status` spot-checks — N/A; no `sensor_governor` log lines
- `/governor memory` — N/A; no `memory_pressure` log lines
- Surprises: **StrategicDirection loaded normally** (7 principles, 3 git themes, 2660 char digest). No Wave 1 primitive fired during session.

## Session 2

- Session ID: `bt-2026-04-22-025106`
- Commit at session start: `472a02459c`
- Any flag forced off? **none**
- Duration / cost: **200.2s / $0.2118** (cap untouched)
- Stop reason: `idle_timeout`
- Ops observed: 2 generated (both $0.00 charged — no claude-api calls); `strategic_drift.total_ops=2`
- Posture timeline: **not observable** (same as session 1)
- `/help flags --posture <X>` — not exercised
- `/governor` spot-checks — N/A (no log lines)
- Surprises: 22 unique sensor names fired into intake over the window (OpportunityMiner, DocStaleness, PerformanceRegression, IntentDiscovery, CapabilityGap, Backlog, TodoScanner, CrossRepoDrift, RuntimeHealth, VisionSensor, GitHubIssue, WebIntelligence, CUExecution, Scheduled, TestFailure + assorted aliases). Zero of those 22 consulted `SensorGovernor.request_budget()` before emitting.

## Session 3

**Skipped by agent decision.** Operator authorized skip if session 2 added no marginal signal. Sessions 1 and 2 produced identical structural observations: no posture persistence, no governor consultation, no memory-pressure advisory invocation. A third session would replicate. Cost savings: ~$0.50.

## Part (b) — primitives return sane decisions when probed

Evidence-bound. Covered by `scripts/wave1_advisory_probe.py` (commit `3d3212d5a4`), PASS log at `scripts/wave1_advisory_probe_PASS.log`. Summary:

- Posture inferred from live git log: **EXPLORE @ 0.984 confidence** (88% `feat:` ratio) — matches intuition for the current repo state (Wave 1 just landed)
- 16×4 sensor budget matrix produces expected posture-weighted caps: TestFailure 16/20/**36**/20, OpportunityMiner **22**/7/4/15, DocStaleness 4/**7**/3/6 across EXPLORE/CONSOLIDATE/HARDEN/MAINTAIN
- Urgency sweep on TestFailureSensor correct: IMMEDIATE 32 / STANDARD 16 / COMPLEX 12 / BACKGROUND 8 / SPECULATIVE 4
- Memory probe on real system: **psutil, 24.6% free → WARN → `can_fanout(16)` → 8** (correctly capped)
- Emergency brake math exact: baseline 16 → braked 3 = ratio 0.19 (within expected 0.15–0.25 band); both `cost_burn=0.95` and `postmortem=0.75` trigger paths fire
- `/help flags --posture EXPLORE` surface returns all 4 master kill switches + SemanticGuard + SemanticInference (6 flags); post Track A (`472a02459c`) the defaults shown are correct

**Part (b): GREEN.** Primitives mathematically sound, posture-direction intended, emergency brake activates, memory probe honest.

## Part (a) — posture "feels right", flag usefulness, §8 audit trail usability

**Status: `HUMAN_REVIEW_WAIVED`.**

**Residual risk statement:** operator did not validate TTY-only affordances. Specifically un-validated:
- `/posture status` / `/posture explain` rich-table rendering (falls through to flat in headless)
- `/help flags --posture <X>` ergonomic "does this feel like the right subset"
- `NOTIFY_APPLY` 5s diff-preview window — not exercised (no yellow ops in sessions)
- `GENERATE` token-stream visualization — headless plain path
- Operator intuition check "was the posture transition I observed expected given what I was doing"

**Evidence-bound partial judgment on what WAS observable:**
- The `StrategicDirection` digest loaded correctly into CONTEXT_EXPANSION prompts (confirmed in debug.log lines 2026-04-21T19:46:47 session 1); posture section injection would compose cleanly but wasn't exercised because observer never ran
- Session `summary.json` schema stable at v2 across both sessions; `cost_by_phase` + `cost_by_op_phase` populated normally
- Known `attempted=0` bug in summary.json counter confirmed still present (CLAUDE.md: "summary.json has a known attempted counter bug") — use `strategic_drift.total_ops` for real count

## Finding — Wave 1 integration gap (NOT a defect in graduated primitives)

Two sessions produced zero references to `posture`, `direction_inferrer`, `sensor_governor`, or `memory_pressure` in debug.log. `.jarvis/posture_current.json` never written. This is consistent with and confirms what was always known and explicitly scoped: **Wave 1 graduated primitives + surfaces, not hot-path integration.** Slice 5 is the wiring.

**Implication for Slice 5 scope (important):** Slice 5 as specified wires only (A) `unified_intake_router.py` → `SensorGovernor.request_budget()` and (B) `subagent_scheduler.py` → `MemoryPressureGate.can_fanout()`. Neither arc starts `PostureObserver`. Without the observer running in-harness, `SensorGovernor._default_posture_fn()` reads `posture_current.json` → `None` → `spec.weight_for_posture(None)` = 1.0 → **governor weighted caps degenerate to `base_cap × urgency_multiplier`, the posture weighting never applies in practice.** This is not a defect in the primitive; it's a wiring prerequisite that should be bundled into or paired with Arc A. Flag raised as follow-up.

## Issues

| # | Issue | Triage / follow-up |
|---|---|---|
| 1 | PostureObserver not auto-started in battle-test harness | **New follow-up** — spin up observer in `battle_test/harness.py` init sequence OR bundle into Slice 5 Arc A as a prerequisite. Without this, governor posture-weighting is inert. |
| 2 | Seed defaults lied post-graduation (surfaced by probe) | **Fixed** in commit `472a02459c` + regression in `test_flag_registry_seed_truth.py` (6/6 green). No further action. |
| 3 | `summary.json attempted=0` known bug | **Pre-existing** (documented in CLAUDE.md). Out of scope for this soak. |

**Issues that block Slice 5: 0.** Issue #1 is a scope clarification for Slice 5, not a block.

## Slice 5 authorization recommendation

**AUTHORIZE Arc A and Arc B.** Bundle the PostureObserver harness wiring into Arc A so governor actually sees live posture when it's consulted.

See final paragraph below for the single-paragraph binding rationale.
