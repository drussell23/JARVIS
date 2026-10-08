# Continuous Loop Supervisor — design draft

Status: **draft, not built** (2026-10-08). Depends on the held-out benchmark
(`observability/heldout_benchmark.py`) having a baseline run, and on one
attested unattended cycle (`origin.unattended: true`) having been observed.

## What exists, and what is missing

Every stage of one pass already runs unattended:

| Stage | Owner | Mechanism |
|---|---|---|
| Soak (autonomous work) | O+V | `start_detached_soak.ps1` → Task Scheduler `OV-Soak` |
| Session-end decision | harness → `training_trigger.maybe_train_after_soak` | ledger row REQUESTED / DEFERRED |
| Training cycle | `training_handoff run` | Task Scheduler `OV-TrainingHandoff`, J-Prime lease, verdict COMMITTED / ROLLED_BACK |
| Adapter reject / restore | J-Prime | `POST /v1/adapters/{model}/reject` (deletes the version, restores the last good) |
| Productivity | `adapter_productivity.measure` | PRODUCTIVITY row at every session end |

Missing: **nothing starts the next soak.** A pass ends at its verdict (or its
deferral) and waits for a person. The supervisor is that one missing step,
plus the decisions only a longitudinal view can make.

## Shape

One long-lived process, `python -m ...observability.loop_supervisor`, run as
Task Scheduler task `OV-LoopSupervisor` through the existing
`start_detached_wsl.ps1` (same lifetime guarantees as soaks and cycles).
It never trains, serves or generates; it only **observes and starts things
through the mechanisms above.** Single-flight (a kernel lock in
`~/.jarvis/loop_supervisor/`), state in `state.json` + append-only
`history.jsonl` (the handoff's ledger pattern), every decision recorded with
its inputs.

```
            ┌────────────── health gate (every transition) ──────────────┐
            ▼                                                            │
  IDLE ─► SOAKING ─► (session end) ─► CYCLING? ─► BENCHMARKING ─► EVALUATING ─► IDLE
                       DEFERRED ──────────────────────────────────────────┘
```

* **IDLE → SOAKING**: when no organism holds its lock, no cycle holds the lane
  (`training_handoff.occupancy()`), the health gate passes, and the operator's
  window allows it (`JARVIS_LOOP_WINDOW`, an hours range; empty = always).
  Starts `OV-Soak` exactly as an operator would (`start_detached_soak.ps1`,
  wall from `JARVIS_LOOP_SOAK_WALL_S`, default the envelope's soak wall).
* **SOAKING → …**: waits on the organism's pidfd (the handoff's
  `_await_exit`), then reads the session's ledger row. REQUESTED → CYCLING;
  DEFERRED → IDLE (the landings carry over — verified by the yield tests).
* **CYCLING**: waits for the cycle's terminal state. The cycle's own gate
  decides COMMITTED / ROLLED_BACK; the supervisor does not second-guess it.
* **BENCHMARKING** (after COMMITTED only): `training_handoff exclusive --
  heldout_benchmark run <set>` for the new adapter. The incumbent's run on
  the same fingerprint is reused when one exists (greedy decoding on the same
  weights reproduces it), else run first.
* **EVALUATING**: the decision table below.

## The two measurements, and why each has the role it has

* **Held-out benchmark** — controlled: fixed tasks, hidden tests, greedy,
  paired per task. Available minutes after training. Cannot see everything
  the organism does.
* **Productivity tracker** — landed commits per organism hour, matched
  exposure, exact Poisson test. Sees the real job, but is confounded by how
  much landable work each session found; needs ≥ `JARVIS_ADAPTER_PRODUCTIVITY_MIN_HOURS`
  (default 6) of service before it judges.

Neither is allowed to act alone on a SERVED adapter.

## Decision table (deterministic)

`B` = benchmark verdict of the new adapter vs its incumbent (better /
no_detectable_difference / worse; `incomparable` / `unmeasured` count as
no_detectable_difference and are recorded). `P` = productivity verdict
(better / no_detectable_difference / worse / insufficient_evidence).

**Before serving** (right after COMMITTED, before the next soak):

| B | Action |
|---|---|
| worse | **reject now** (`/v1/adapters/{model}/reject`). A held-out regression is direct evidence; serving it for 6 h to collect productivity data would cost real organism hours on a model already shown worse. This is a pre-deployment gate, not a rollback of a proven adapter. |
| otherwise | serve; P starts accruing |

**While serving** (re-evaluated at every session end, from the PRODUCTIVITY row):

| B | P | Action |
|---|---|---|
| any | insufficient_evidence | keep; wait |
| any | better / no_detectable_difference | keep |
| no_detectable_difference | worse | **strike 1**; keep. Rollback on the 2nd consecutive window that is also `worse` |
| better | worse | **disagreement**: keep, record it, notify the operator. The controlled measurement outranks the confounded one; P alone never rolls back an adapter B rated better |

So a served adapter is rolled back **only** when productivity is worse in two
consecutive windows **and** the benchmark did not rate it better — both
measurements must point the same way. Two consecutive windows, not one,
because one window's landing count is exposed to that session's work supply
(the 2026-10-04 soak's bottleneck was supply, not the model).

Rollback = the existing reject endpoint; the evidence row (B, P, both windows)
is attached as its `reason`.

## Health gate — halts, not retries

Checked before every transition. A halt stops the loop, records why, and
notifies; it never retries on its own (a self-improving loop's dangerous
failure is not crashing but spinning).

| Check | Signal (all from existing surfaces) | Halts when |
|---|---|---|
| J-Prime alive & serving | `/health`, `/v1/lease` state | not `serving` with the expected model after a restore |
| J-Prime memory leak | resident VRAM of the same model+adapter set after each restore (`/api/ps size_vram`), J-Prime process RSS | grows monotonically across `JARVIS_LOOP_LEAK_WINDOW` consecutive restores beyond the spread of its own first samples (median ± k·MAD, k from env) |
| Systemic collapse | ledger: ROLLED_BACK + pre-serve rejects | `JARVIS_LOOP_MAX_CONSECUTIVE_REJECTS` (default 3) in a row |
| Broken cycles | ledger: FAILED / ABORTED | same bound, consecutive |
| Starved loop | ledger: DEFERRED with 0 new landings | `JARVIS_LOOP_MAX_EMPTY_SESSIONS` consecutive (work supply, not the model — needs a person) |
| Host | existing admission (`memory_guard.check_admission`: Windows commit, guest memory) | below the floors it already enforces |
| Disk | free space under `~/grpo-runs` | below `JARVIS_LOOP_MIN_FREE_GIB`; old run dirs (adapters live in J-Prime's registry) are pruned oldest-first before halting |
| Operator | the organism lock held by an interactive `ov` | never preempted: the supervisor waits |

Plus a cockpit verb (`/loop status|pause|resume|stop`) and a status-line
segment naming the phase — the operator can always see and stop it.

## What it deliberately does not do

* Push to GitHub — stays a human step.
* Approve Orange-tier changes — unchanged.
* Change the cycle's own gate — the cycle stays self-contained; the
  supervisor only adds the longitudinal decisions above.

## Build order

1. Held-out benchmark: build the task set, take the incumbent's baseline run.
2. Supervisor skeleton: lock, ledger, IDLE↔SOAKING with the health gate (no
   training decisions yet) — proves continuous soaking.
3. BENCHMARKING + the pre-serve reject.
4. The while-serving table (needs PRODUCTIVITY rows across ≥ 2 windows).
