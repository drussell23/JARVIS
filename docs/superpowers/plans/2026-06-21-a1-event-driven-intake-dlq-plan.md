# Milestone A1 — Sovereign Event-Driven Intake & DLQ — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use `- [ ]`.

**Goal:** Make the strategic-GOAL intake path correct, recoverable, heavy-GOAL-safe, and observable so `file-00` can dispatch end-to-end (the PRD's A1 gate). Reuse-first: TrinityEventBus, `is_heavy_goal`, the existing dispatch chain.

**Spec (binding):** `docs/superpowers/specs/2026-06-21-a1-event-driven-intake-dlq-design.md`. **Diagnosis already done** — pipe works; root cause = source default-off + `_TeeRouter` silent-drop race + silent_boot log red herring.

**Branch:** `fleet/a1-event-driven-intake-dlq`. **Worktree quirk:** verify writes via `git show`/`grep` (Edit-flush anomaly seen here).

**Honest framing for the PR:** gate-unlock engineering for the autonomous-PR track record; composite moves on the soak evidence, not this merge. Backpressure Health Gate is DEFERRED.

---

## File Structure
| File | Responsibility | New/Modify |
|---|---|---|
| `backend/core/ouroboros/governance/intake_dlq.py` | DLQ append + replay (pure, testable) | **Create** |
| `backend/core/ouroboros/governance/roadmap_orchestrator.py` | `_TeeRouter` None-upstream → loud + DLQ (no silent drop) | Modify |
| `backend/core/ouroboros/governance/intake/intake_layer_service.py` | publish `intake.router.ready` + `router_is_ready()` after attach+dispatch-loop-spawn | Modify |
| `backend/core/ouroboros/governance/governed_loop_service.py` | roadmap daemon awaits router-ready (subscribe-then-check, bounded, timeout→DLQ) before emit; DLQ replay post-ready | Modify |
| `backend/core/ouroboros/governance/intake/unified_intake_router.py` | DAG-weight tag at dequeue + `[A1Trace]` ingest/dequeue/submit breadcrumbs | Modify |
| `tests/governance/test_*` | per-task tests | **Create** |

**Order:** T1 (DLQ correctness core) → T2 (event valve) → T3 (DAG-weight tag) → T4 (breadcrumbs) → T5 (integration).

---

## Task A1-T1: Sovereign DLQ (no silent drops)
**Files:** Create `intake_dlq.py`; Modify `roadmap_orchestrator.py` (`_TeeRouter`); Test `tests/governance/test_intake_dlq.py`.

- [ ] **Read first:** `roadmap_orchestrator.py:656` `_TeeRouter` (`__init__(upstream=None)`, `ingest()` capture-then-forward). Confirm where `upstream is None` leads to silent capture.
- [ ] **Failing tests:**
```python
# tests/governance/test_intake_dlq.py
from __future__ import annotations
from backend.core.ouroboros.governance import intake_dlq as dlq

def test_append_and_read(tmp_path):
    p = tmp_path / "intake_dlq.jsonl"
    dlq.append_dlq({"goal_id": "g1", "x": 1}, reason="no_router", path=str(p))
    rows = dlq.read_dlq(str(p))
    assert len(rows) == 1 and rows[0]["envelope"]["goal_id"] == "g1"
    assert rows[0]["reason"] == "no_router"

def test_append_failsoft_bad_path():
    # unwritable path -> never raises
    dlq.append_dlq({"goal_id": "g"}, reason="x", path="/nonexistent_dir/dlq.jsonl")

def test_replay_dedups_by_goal_id(tmp_path):
    p = tmp_path / "dlq.jsonl"
    dlq.append_dlq({"goal_id": "g1"}, reason="r", path=str(p))
    dlq.append_dlq({"goal_id": "g1"}, reason="r", path=str(p))  # dup
    dlq.append_dlq({"goal_id": "g2"}, reason="r", path=str(p))
    seen = []
    async def fake_ingest(env): seen.append(env["goal_id"]); return "ok"
    import asyncio
    drained = asyncio.run(dlq.replay_dlq(str(p), fake_ingest))
    assert set(seen) == {"g1", "g2"}   # dedup -> each goal once
    assert drained == 2

def test_replay_keeps_failed_entries(tmp_path):
    p = tmp_path / "dlq.jsonl"
    dlq.append_dlq({"goal_id": "g1"}, reason="r", path=str(p))
    async def boom(env): raise RuntimeError("down")
    import asyncio
    drained = asyncio.run(dlq.replay_dlq(str(p), boom))
    assert drained == 0
    assert len(dlq.read_dlq(str(p))) == 1   # failed re-ingest stays in DLQ
```
- [ ] **Implement `intake_dlq.py`:** `append_dlq(envelope, *, reason, path=None)` (default `.jarvis/intake_dlq.jsonl`; atomic append; record `{ts, reason, schema_version, envelope}`; `goal_id` extracted from envelope; fail-soft never raises; CRITICAL log). `read_dlq(path)` (parse, fail-open []). `async replay_dlq(path, ingest_fn)` (dedup by goal_id, call `ingest_fn(envelope)`; on success drop, on failure keep; atomic rewrite of survivors; returns count drained; fail-soft). Master `JARVIS_INTAKE_DLQ_ENABLED` default-true (gate `append_dlq`/`replay_dlq` to no-op when off).
- [ ] **Wire `_TeeRouter`:** when `upstream is None` at `ingest()` → CRITICAL log `[IntakeDLQ] orphaned GOAL — no attached router` + `intake_dlq.append_dlq(envelope, reason="no_router")` INSTEAD of silent `.captured`-only. (Keep `.captured` for the existing report path, but ALSO DLQ it.)
- [ ] Run tests + `-k "roadmap_orchestrator or tee or dlq"` regression. Commit: `feat(a1): Sovereign DLQ — no silent strategic-GOAL drops + replay (kills _TeeRouter sinkhole)`.

## Task A1-T2: Event-driven valve (`intake.router.ready`, no poll)
**Files:** Modify `intake_layer_service.py` + `governed_loop_service.py`; Test `tests/governance/test_a1_router_ready_valve.py`.
- [ ] **Read first:** `intake_layer_service.py:464` (router attach) + `router.start()` (dispatch-loop spawn); the roadmap daemon emit site in `governed_loop_service.py:~1910-1960`; TrinityEventBus publish/subscribe + `get_event_bus_if_exists`.
- [ ] **Implement:** add module constant `EVENT_ROUTER_READY = "intake.router.ready"`. In IntakeLayerService, AFTER attach + `router.start()`: set a ready flag (`self._router_ready = True` + a module/singleton `router_is_ready()` probe) and publish `TrinityEvent(topic=EVENT_ROUTER_READY)` once (idempotent). In the roadmap daemon: before the FIRST emit, `await` readiness via **subscribe-then-check** (subscribe to the topic, THEN check `router_is_ready()` — if already ready, proceed; else await the event), bounded by `JARVIS_A1_ROUTER_READY_TIMEOUT_S` (60); on timeout → `intake_dlq.append_dlq(envelope, reason="router_ready_timeout")` + CRITICAL, do NOT emit into a void. NO sleep-poll.
- [ ] **Tests:** ready-before-subscribe → no deadlock (proceeds); ready-after-subscribe → event wakes it; timeout → DLQ + no emit. Use a fake bus + fake clock.
- [ ] Run + regression (`-k "intake_layer or governed_loop or router_ready"`). Commit: `feat(a1): event-driven router-ready valve — daemon never emits before router attached (subscribe-then-check, no poll)`.

## Task A1-T3: DAG-weight pre-flight tag
**Files:** Modify `unified_intake_router.py` (dequeue/pre-submit); Test `tests/governance/test_a1_dag_weight_tag.py`.
- [ ] **Read first:** the `_dispatch_loop` dequeue → `GLS.submit` site; the envelope shape (`target_files`, `metadata`); how the orchestrator's Epistemic prefetch reads heaviness (`is_heavy_goal`).
- [ ] **Implement:** at dequeue/pre-submit, compute `is_heavy_goal(envelope.target_files, getattr(envelope,"blast_radius",0))` and stamp `envelope.metadata["dag_weight"]="heavy"` (or a ctx flag the orchestrator already reads). Reuse `is_heavy_goal`; do NOT duplicate the prefetch (it already triggers at GENERATE). Fail-soft. No-op when not heavy.
- [ ] **Tests:** heavy envelope → tagged; light → untagged; fail-soft on bad envelope. Commit: `feat(a1): DAG-weight pre-flight tag — heavy intake GOALs routed to Epistemic Matrix`.

## Task A1-T4: `[A1Trace]` breadcrumbs
**Files:** Modify the 5 hop sites (daemon emit, router ingest, dispatch dequeue, GLS.submit, orchestrator entry); Test `tests/governance/test_a1_trace_breadcrumbs.py`.
- [ ] **Implement:** a tiny `_a1trace(hop, goal_id, **kw)` helper (in `intake_dlq.py` or a new `a1_trace.py`) emitting WARNING-level `[A1Trace] <hop> goal=<id> ...` (WARNING so it survives `silent_boot` to stdout), gated `JARVIS_A1_TRACE_ENABLED` default-true. Call it at the 5 hops with a stable `goal_id`. Read each hop site first; keep fail-soft.
- [ ] **Tests:** helper emits the expected string per hop; gated off → silent. (Structural assertion that each hop site calls `_a1trace` is acceptable for the deep sites.) Commit: `feat(a1): [A1Trace] breadcrumbs at all 5 intake->FSM hops (soak proof instrument)`.

## Task A1-T5: Integration + OFF byte-identical
**Files:** Test `tests/governance/test_a1_intake_integration.py`.
- [ ] End-to-end with fakes: daemon awaits ready → emits → router ingests (traced) → dequeue tags heavy → submit; orphaned (None router) → DLQ not silent; DLQ replay after ready re-ingests. OFF byte-identical (DLQ/trace off → legacy path minus silent drop). Reused-subsystem regression sweep (intake, roadmap_orchestrator, event-bus, governed_loop). Commit.

## Done criteria
- No strategic GOAL can be silently lost (DLQ + replay); daemon never emits before router-ready; heavy GOALs tagged for the Epistemic Matrix; 5 `[A1Trace]` hops instrumented. Zero real regressions.
- **Live proof (operator, real host):** `--production-soak` w/ `JARVIS_ROADMAP_ORCHESTRATOR_ENABLED=1` → 5 ordered `[A1Trace]` lines in stdout → first autonomous PR. (This code arc makes it correct+observable; the soak generates the evidence.)

## Self-review
- Spec coverage: §4.2 (T1), §4.1 (T2), §4.3 (T3), §4.4 (T4), §6 (T5). Backpressure deferred per §2/§7.
- Reuse: TrinityEventBus, is_heavy_goal, existing dispatch chain, atomic-jsonl pattern. No new queue/FSM.
- Large-file tasks (T2/T3/T4) ship read-first edit instructions — confirm exact hop sites against live code.
