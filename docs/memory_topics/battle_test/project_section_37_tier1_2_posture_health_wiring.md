---
title: Project Section 37 Tier1 2 Posture Health Wiring
modules: [tests/governance/test_section_37_tier1_2_posture_health_wiring.py]
status: historical
source: project_section_37_tier1_2_posture_health_wiring.md
---

May 9 2026: §37 Tier 1 row #2 ✅ Shipped. Pre-audit substrate verification:

**Already shipped pre-audit**:
- `posture_health.py` (~571 LOC) — 4-state closed taxonomy (HEALTHY /
  DEGRADED_HUNG / DEGRADED_FAILING / TASK_DEAD), pure-function
  `evaluate_observer_health` classifier, `safe_load_posture` /
  `safe_load_posture_value` consumer wrappers, debounced
  `_maybe_publish_degraded_event` SSE publisher, env-tunable thresholds,
  AST-pinned authority invariants
- `posture_observer.py:553-589` — `task_health_snapshot()` heartbeat
  field exposure with explicit "consumers should NOT classify health
  themselves; the classifier owns the policy" contract
- `invariant_drift_observer.py:438-468` — consumer already wired to
  `safe_load_posture_value`
- 52 regression tests passing in `test_posture_health.py`

**4 genuine gaps closed in this slice**:

1. **SensorGovernor consumer wiring** — THE silent-degradation cascade the
   §37 row names. `_default_posture_fn:318-331` + `_default_signal_bundle_fn:334-351`
   were calling `get_default_store().load_current()` directly, silently
   returning stale `_store` state when the observer task is dead-but-still-
   listed. v2.84 wires both functions to compose canonical
   `posture_health.safe_load_posture_value` / `safe_load_posture` so a dead
   PostureObserver degrades the governor to unweighted (1.0×) caps —
   equivalent to MAINTAIN safe-default — instead of applying weights against
   frozen state. **Substrate-unavailable rollback path preserved**: catches
   `ImportError`, falls through to legacy direct-store read so a missing
   `posture_health` module never breaks the governor.

2. **Canonical SSE event registration** — local constant
   `EVENT_TYPE_POSTURE_OBSERVER_DEGRADED = "posture_observer_degraded"`
   existed in `posture_health.py:502-504` but was NOT in
   `ide_observability_stream._VALID_EVENT_TYPES` frozenset → broker was
   silently rejecting publishes from `_maybe_publish_degraded_event`.
   v2.84 registers the canonical constant. Local definition retained for
   substrate independence (per leaf-authority discipline — `posture_health`
   is below `ide_observability_stream` in the import graph and must not
   couple at module load); AST pin asserts the two definitions agree on
   string value (drift here would silently drop SSE events).

3. **`/posture health` REPL subcommand** — added to
   `posture_repl.py::dispatch_posture_command` + new `_health()` helper that
   composes the canonical classifier. `_HELP` block updated. When detection
   master flag is dormant, returns a "detection dormant" notice rather than
   fabricating HEALTHY response (operator binding: no fake-healthy). When
   observer is None, returns `status=TASK_DEAD` (load-bearing distinction —
   operator sees DEAD rather than 5xx).

4. **`GET /observability/posture/health` IDE route** — new handler
   `_handle_posture_health` in `ide_observability.py` registered alongside
   existing `/observability/posture` + `/observability/posture/history`
   routes. Composes `evaluate_observer_health(observer.task_health_snapshot())`
   and returns the verdict via `verdict.to_dict()`. Preserves loopback-only /
   rate-limited / master-flag-gated contract. Outcome map: 403 when
   `JARVIS_IDE_OBSERVABILITY_ENABLED=false`; 200+detection_off notice when
   classifier dormant; 200+TASK_DEAD when observer instance unavailable;
   200+verdict when classifier evaluates.

**15 regression tests** in
`tests/governance/test_section_37_tier1_2_posture_health_wiring.py`:
- 2 SensorGovernor AST pins (composes safe wrappers + imports substrate at
  primary path; rollback path preserved at except branch)
- 2 SSE event pins (registered in `_VALID_EVENT_TYPES` + canonical/local
  constants string-equal — drift would silently drop publishes)
- 2 REPL pins (`_health()` helper composes canonical classifier + `_HELP`
  block documents subcommand)
- 2 IDE route pins (route registered + handler exists at runtime —
  belt-and-braces source AST + runtime hasattr check)
- 2 functional tests for SensorGovernor (observer=None → returns None for
  MAINTAIN safe-default; detection-off → byte-equivalent legacy behavior
  pass-through to `store.load_current`)
- 2 REPL functional tests (master-off returns dormant notice not
  fake-HEALTHY; observer=None returns TASK_DEAD)
- 2 provenance pins (§37 Tier 1 #2 cited in sensor_governor +
  ide_observability)
- **1 no-parallel-logic pin** that asserts `sensor_governor` source NEVER
  mentions `TASK_DEAD` / `DEGRADED_HUNG` / `DEGRADED_FAILING` strings (the
  classifier owns the policy per `posture_health.py:558-561`; consumers
  compose it)

**1131/1131 cumulative regression green** across §37 Tier 1 #1 + #2 + #3 +
Phase 8 + P9.5 + Vector #5 + Wave 3 + adversarial cage + scheduler +
posture (52 + 15 new) + sensor_governor (303 with new wiring) +
graduation_ledger + 7 v2.82 consumer files.

**Master flag `JARVIS_POSTURE_HEALTH_DETECTION_ENABLED` stays default-FALSE**
per Phase 9 cadence operator binding.

**Architecture preserved** (operator binding satisfied verbatim):
- ZERO parallel classifier — sensor_governor never mentions
  TASK_DEAD/DEGRADED_* strings (AST-pinned)
- Composes existing `posture_health` substrate (zero parallel logic)
- Composes existing `_maybe_publish_degraded_event` debounced publisher
  (zero parallel SSE publish)
- Composes existing `task_health_snapshot()` heartbeat fields (zero
  parallel state)
- Substrate-unavailable rollback paths preserve byte-equivalent legacy
  behavior at every site
- No hardcoding — all thresholds env-tunable via posture_health knobs

**Three §37 Tier 1 closures landed in same day** (v2.82 #3 ledger flock +
v2.83 #1 confidence drop payload + v2.84 #2 task-death wiring). §37 Tier 1
arc fully closed.

**NEXT** (autonomy arc remaining):
- **§35 row 🟡 #4 / §3.6.3 priority #4** — Cross-runner artifact contract
  (schema-versioned) (~3-5d, pre-empts a class of Wave 2 PhaseRunner
  refactor crashes — last engineering item before Phase 9 cadence becomes
  the path forward)
- **Phase 9 graduation cadence** ~6-9 weeks operator-paced soaks
- **§39 Tier 6** trigger-gated on J-Prime + Reactor-Core repos
