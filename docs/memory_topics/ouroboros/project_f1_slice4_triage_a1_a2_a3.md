---
title: Rule 2: Security surface is unconditionally off-limits
modules: [backend/core/ouroboros/governance/tool_executor.py, backend/core/ouroboros/governance/orchestrator.py, backend/core/ouroboros/governance/risk_engine.py, backend/core/ouroboros/cancellation_token.py]
status: historical
source: project_f1_slice4_triage_a1_a2_a3.md
---

## Context

F1 Slice 4 S2 (`bt-2026-04-24-091016`) finished with all graduation markers at 0:
- `[ParallelDispatch]`: 0
- `enforce_submit_start`: 0
- `APPLY`: 0
- 3 target files: unchanged

**Session reclassification (operator-accepted 2026-04-24)**: this session's graduation miss is primarily **fixture / harness hygiene** (reachable path set didn't preflight against `_build_profile` + `risk_engine.classify`), **not scheduler regression**. F1 and F2 remain proven on live traffic for this failure mode. See A3 below for full RCA.

**Advisor classification for this session**:
- **Advisor BLOCKED count**: 1
- **Blocked op**: `op-019dbedd-de30-7f6e-b49f-2cb107a99bcd-cau`
- **Block reason (Advisor fields)**: `risk=0.73, blast=50, coverage=0%, entropy=50%, read_only=False`, reasons=4
- **Target file of blocked op**: `backend/core/ouroboros/governance/tool_executor.py` (4631 lines)
- **Block message**: *"High blast radius: 50 files import these targets; Low test coverage: 0% of targets have tests; Large files (>500 lines): backend/core/ouroboros/governance/tool_executor.py(4631L); BLOCKED: Zero test coverage + extreme blast radius"*

**Key finding: the block was NOT for the seed.** It was a DocStaleness op targeting `tool_executor.py`. The forced-reach seed (3 low-blast-radius utility modules) did NOT trip the Advisor — the Advisor is not the gate that ate the seed this session.

**Seed's actual fate** (S2):
- 02:10:29 — `BacklogSensor: enqueued task_id=wave3-item6-forced-reach-multifile-seed`
- 02:10:30 — `[IntakePriority] primary dequeue urgency=critical source=backlog waited_s=0.10 mode=priority depth=0` — F1 worked (0.10s wait)
- 02:10:30 onward — **seed vanishes. Zero `Route:` decisions for `source=backlog` in the entire session.** All 14 Route decisions were doc_staleness (6+3), todo_scanner (3), runtime_health (1), exploration (1). None for the seed.

**Classification**: this is a NEW failure mode, distinct from S1 (`blocked_by_operation_advisor`). Candidate name: **`blocked_between_dequeue_and_route`** — F1 dequeued the seed correctly, something downstream consumed it before CLASSIFY. Primary hypothesis (not confirmed): coalesce buffer holding `urgency=critical` envelope (critical doesn't bypass coalesce, only `high` does per the `_dispatch_loop` code). But could also be worker-pool / file-conflict / dedup / pending-ack path. **Not investigated further pending operator direction.**

---

## A1 — Wall cap vs idle_timeout precedence

**Status**: OPEN. Owner: unassigned.

### Evidence

- Session launched 02:10:23 with `--max-wall-seconds 2400` (40 min)
- `[WallClockWatchdog]` log at 02:10:29 claimed: *"armed: max_wall_seconds=2400s — session will terminate with stop_reason=wall_clock_cap if not already stopped."*
- Expected wall-cap firing time: **02:50:23**
- Actual termination: **03:59:32** via `stop_reason=idle_timeout` (1h 49m past start, 1h 9m PAST the wall cap)
- `wall_clock_cap` never fired

### Question

Did `WallClockWatchdog` actually arm correctly? Did `idle_timeout` beat the wall-cap to the shutdown path, or did wall-cap silently never fire? What is the documented precedence when both conditions become true?

### Expected precedence (if not already specified)

Wall-cap should be **hard absolute** — it's an upper bound regardless of activity signal. `idle_timeout` is a soft lower bound (no activity for N seconds → stop). If both become true, wall-cap should win (or at least co-fire with the earliest deadline). Session running 1h 49m > 40min cap means the cap was NEVER enforced.

### Repro candidate

Ticket A1 should include: a battle-test session with `--max-wall-seconds=60 --idle-timeout=3600`. If the session runs >60s it's a reproducer.

### Next steps (not authorized — for operator decision)

- Audit `WallClockWatchdog` implementation in `harness.py` vs the asyncio shutdown path
- Verify the watchdog task actually runs and can preempt `idle_timeout` handler
- Document the precedence in CLAUDE.md under Battle Test

---

## A2 — Post-summary shutdown delay (~3h 55m)

**Status**: OPEN. Owner: unassigned. **Strong candidate for harness epic item #3.**

### Evidence

- `Session ... stopping: idle_timeout` at 03:59:32
- `Summary written to ...summary.json` at **07:55:04**
- Delta: **3h 55m 32s** between idle-stop and summary-write

Prior Py_FinalizeEx zombies (10 documented in `project_followup_battle_test_post_summary_hang.md`) sat at 0% CPU for 20min-1d+. This session is unusual because summary.json **did** get written — just very slowly. The process was wedged at 0% CPU for most of those 4 hours per the standard signature pattern (sampled and confirmed `Py_FinalizeEx + PyThread_acquire_lock_timed` on the child — 11th identical wedge, now killed 2026-04-24).

### Hypothesis (reinforces harness epic)

Same `threading._shutdown()` deadlock pattern as zombies 1-10, but **eventually** completed the shutdown chain. Something in the teardown sequence (DurableJSONL executor, HibernationProber, SensorGovernor task, or a long-running asyncgen) was slowly finishing its own cleanup while the main thread waited on it.

### Evidence to capture (already captured)

- Python child PID 65961 — sampled at 13:20 wall-clock (5h 22m after session end), signature match confirmed
- Sample file: `.jarvis/forensics/pid65961_sample_<ts>.txt`
- Stack: `Py_FinalizeEx → PyObject_VectorcallMethod → _PyEval_EvalFrameDefault → _threadmodule.c offset +0x1b34c4 → PyThread_acquire_lock_timed → _pthread_cond_wait → __psynch_cvwait` (identical to prior 10)

### Next steps (not authorized — for operator decision)

- Harness epic item #3 (bounded-shutdown + `os._exit` fallback) becomes higher priority — 11 reproductions now, including 1 that DID complete (proving the wedge is sometimes transient, sometimes fatal)
- Audit: DurableJSONL executor, HibernationProber, SensorGovernor periodic task, PostureObserver task for non-daemon threads
- Repro: launch session, idle-stop it, capture `sample` at +30s, +5min, +30min to map the unblock sequence

---

## A3 — Throughput: 14 ops / 0 graduation markers over 1h 49m active — **RCA CLOSED**

**Status**: **CLOSED** 2026-04-24 — RCA proven via log trace (operator-accepted). Owner: n/a (complete). **Session reclassified as fixture/harness hygiene failure, not scheduler regression.**

### Evidence — complete seed trace from log

```
02:10:23  session start
02:10:29  BacklogSensor: enqueued task_id=wave3-item6-forced-reach-multifile-seed
02:10:30  [IntakePriority] primary dequeue urgency=critical source=backlog waited_s=0.10
          ─── F1 WORKED (0.10s wait) ✓
02:10:30→02:11:00  30s gap — seed in _coalesce_buffer (critical doesn't bypass, per code)
02:11:00  Submitted bgop-e4ee40865274 (goal='op-019dbec1-4315...',
          route=standard, priority=3, queue_depth=1/16)
          ─── F2 envelope_routing_override landed on route=standard ✓
02:11:00  Worker 0 picked up bgop-e4ee40865274
02:11:01  PhaseDispatcher iter=0 dispatching CLASSIFY → CLASSIFYRunner
02:29:33  [Advisor] caution (risk=0.57, blast=12, coverage=0%) reasons=3
          ─── ADVISOR PASSED (caution, not block) ✓
02:29:33  Complexity: architectural, Persistence: ephemeral
          + 4 goals + user prefs injected (88s of CLASSIFY work)
02:29:33  runner returned next_phase=None (status=fail
          reason='touches_security_surface') — terminal
          ─── RISK ENGINE TERMINAL BLOCK
02:29:33  Worker 0 completed bgop-e4ee40865274 in 88.07s
```

### Root cause: naive substring match in `_build_profile`

**Location**: `backend/core/ouroboros/governance/orchestrator.py:8131-8134`

```python
touches_security = any(
    any(kw in str(p).lower() for kw in ("auth", "secret", "cred", "token", "encrypt"))
    for p in target_paths
)
```

Enforced by `backend/core/ouroboros/governance/risk_engine.py:326-330`:

```python
# Rule 2: Security surface is unconditionally off-limits
if profile.touches_security_surface:
    return RiskClassification(
        tier=RiskTier.BLOCKED,
        reason_code="touches_security_surface",
    )
```

**Unconditional BLOCK** — no severity scaling, no content inspection, just path-substring.

### False-positive mechanism

The reframed fixture's third target, `backend/core/ouroboros/cancellation_token.py`, contains the substring `"token"` in its filename. The file is actually a cooperative asyncio cancellation primitive (the "token" here is the standard concurrency-primitive name, like `CancellationToken` in .NET). Zero security relevance. But the path-regex treats any path-substring match as a security surface, so the BLOCK fires unconditionally.

**The same class of false positive would fire on** (for illustration, not a denylist):
- `authoring_helpers.py` (auth)
- `credentials_format.py` (cred)
- `secret_santa_sensor.py` (secret)
- `token_bucket.py` / `cancellation_token.py` (token)
- `encryption_stub.py` (encrypt)

None of these are security-relevant by content — they're all false positives waiting to happen.

### Coalesce theory — FALSIFIED

The original A3 hypothesis was "critical doesn't bypass coalesce → seed sits in buffer indefinitely." The log trace disproves this:

- Seed entered coalesce buffer at 02:10:30
- Coalesce window is `JARVIS_COALESCE_WINDOW_S=30` (default)
- Seed flushed at 02:11:00 exactly — **the 30s default window, no stall**
- Post-flush: seed reached BackgroundAgentPool → Worker → CLASSIFY cleanly

**Coalesce adds 30s latency to critical envelopes but does NOT indefinitely stall them.** The "critical should bypass coalesce" question is a legitimate latency-invariant discussion but is orthogonal to the graduation blocker. It is not shipped on the F1 branch and not part of this pass's scope.

### F1 and F2 status

**Both proven on live traffic for this session, free of failure-mode contribution:**

- F1 worked — priority queue dequeued the seed in 0.10s (instant pop, no starvation)
- F2 worked — envelope `routing_override=standard` landed as `route=standard priority=3` on BackgroundAgentPool.submit
- F2 UrgencyRouter priority-0.5 worked — the seed was submitted with route=standard via the envelope_routing_override path
- Neither has any failure mode that would let the seed stall post-dequeue

### Complete stall-path enumeration (code-reviewed)

Every gate between `primary dequeue` and downstream phases, with seed's actual fate:

| # | Gate | Location | Can stall indefinitely? | Seed's fate this session |
|---|---|---|---|---|
| 1 | Coalesce buffer (30s window, critical doesn't bypass) | `unified_intake_router._dispatch_loop` lines ~895–920 | No — flushes on timeout | 30s delay, then flushed ✓ |
| 2 | File-conflict (`_find_file_conflict` + `_queued_behind`) | `ingest()` lines 488–505 | Yes, until blocking op completes | No hit — no file conflict ✓ |
| 3 | Dedup (`_is_duplicate`) | `ingest()` lines 480–485 | Yes, silently drops | No hit — unique dedup_key ✓ |
| 4 | Pending-ack (`_pending_ack.park`) | `ingest()` lines 483–486 | Yes, until acknowledge | No hit — `requires_human_ack=False` ✓ |
| 5 | Router-level backpressure | `ingest()` lines 510–515 | No — refuses at ingest | No hit — queue not full ✓ |
| 6 | SensorGovernor shadow/enforce deny | `ingest()` lines 520–545 | Refuses (enforce) or passes (shadow) | Passed (shadow mode) ✓ |
| 7 | BackgroundAgentPool worker saturation | `GovernedLoop.submit` | Yes, queues behind workers | No hit — Worker 0 picked up in <1s ✓ |
| 8 | CLASSIFY complexity classifier → "architectural" | `orchestrator._classify_op` | No — classifies and continues | Passed (architectural) ✓ |
| 9 | **Risk engine `touches_security_surface` → unconditional BLOCK** | `risk_engine.py:326` | **Terminal fail** | **BLOCKED HERE** ✗ |
| 10 | Risk engine other rules (touches_supervisor, crosses_repo, DELETE) | `risk_engine.py` | Various | Not reached ✓ |
| 11 | Iron Gate gates (post-GENERATE) | `orchestrator._iron_gate_*` | Returns retry/fail | Not reached — CLASSIFY terminated first ✓ |

Only gate #9 fired for the seed.

### Remediation track — fixture + preflight (authorized by operator 2026-04-24)

**Authorized for Slice 4 S3 prep** (fixture-only, no code changes on F1 branch):

1. **Swap `cancellation_token.py` target** for a path whose filename contains NONE of `{auth, secret, cred, token, encrypt}` substrings.
2. **Preflight script** (manual walk-through before next live run): feed each candidate path through `_build_profile` + `risk_engine.classify` and log `touches_*` booleans + `RiskTier`. Prevents burning another session on a path that a pre-ingest regex would block.
3. Also continue to satisfy the original Advisor criteria (blast_radius < 50, coverage > 0%).

**Not authorized for this pass, tracked as separate F-tickets:**

- Tightening `touches_security_surface` match (path-segment vs substring, denylist vs allowlist for benign `*token*` names, content-based detection). Scope: Manifesto §6 Iron Gate change.
- Critical envelope bypass of coalesce (latency invariant). Scope: scheduler semantics — per operator: "explicitly not F1 Slice 4 graduation, so we don't mix live reachability with scheduler semantics in one narrative."
- A1 / A2 harness items (wall-cap precedence, post-summary shutdown wedge) — on harness epic.

### Fixture-design lesson (captured for future reframings)

Candidate target paths should be pre-flighted against **at least three gates**:

1. `OperationAdvisor` blast_radius × coverage composite (tripped S1)
2. `_build_profile` substring regex for security/supervisor/core-orchestration (tripped S2)
3. `risk_engine.classify` — additionally catches `touches_supervisor`, `crosses_repo_boundary`, `DELETE` change types

A minimal preflight:
```python
from backend.core.ouroboros.governance.risk_engine import risk_classifier
from backend.core.ouroboros.governance.orchestrator import Orchestrator
# (plus an OperationContext stub; see _build_profile signature)
orch._build_profile(ctx)  # → check touches_security_surface, touches_supervisor
risk_classifier.classify(profile)  # → check RiskTier
```

If either surfaces BLOCKED or a non-SAFE_AUTO tier, the fixture is un-graduateable.

---

## Shared binding

Per operator 2026-04-24:
- **No default flip.** F1 Slice 4 live_reachability still blocked. Zero clean sessions.
- **No Slice 4 S3 run** until A3 classification lands (blocker is not Advisor → something else).
- **No new knobs invented**, no heroics, no config thrash.
- **Work is split**: A1, A2, A3 are orthogonal and should be triaged independently.

## Cross-links

- `project_followup_f1_intake_governor_enforcement.md` — parent F1 arc, Slice 4 status
- `project_followup_battle_test_post_summary_hang.md` — harness epic (A2 is 11th reproduction)
- `project_wave3_item6_graduation_matrix.md` — graduation ledger, S2 row needs adding
- `project_known_preexisting_test_flakes.md` — separate ticket, unrelated
