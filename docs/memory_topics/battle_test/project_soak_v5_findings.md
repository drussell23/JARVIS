---
title: Soak v5 Findings — 2026-05-03
modules: [scripts/pass_b_soak_assertion.py, scripts/production_oracle_closure_verdict.py, scripts/auto_action_oracle_closure_verdict.py]
status: historical
source: project_soak_v5_findings.md
---

# Soak v5 Findings — 2026-05-03

Single-session validation soak following the 14-arc session. Booted with current defaults (META_PHASE_RUNNER + REPLAY_EXECUTOR stay false per playbook; everything else default-true). Headless, cost-cap $0.50, idle-timeout 600s, max-wall-seconds 2400s. Watched live via Monitor against the session debug.log.

Session: `bt-2026-05-03-060330`. Duration: 3696s (61.6 min — overran the 40-min wall cap by 22 min, see defect #1 below). Cost: $0.0357 (Claude only — DW failed to produce successful ops). Outcome: `incomplete_kill / wall_clock_cap+atexit_fallback`. Convergence: `INSUFFICIENT_DATA`.

## Verdict results

| Verdict script | Result | Notes |
|---|---|---|
| `pass_b_soak_assertion.py` | **4/5 (CB5 FAIL)** | CB1+CB2+CB3+CB4 PASS; CB5 fail because outcome=incomplete_kill (the predicted pattern from brutal review) |
| `production_oracle_closure_verdict.py` | **6/6 PASS** | Substrate stable through soak |
| `auto_action_oracle_closure_verdict.py` | **5/5 PASS** | Arc 1 VERIFY wiring stable through soak |

## Empirical activation telemetry — partial firing only (CORRECTED 2026-05-03 evening)

**Correction note**: my initial extraction reported `auto_action_proposal_sse: 3` — that was a regex false positive. The 3 matches were a stale-lock warning + 2 DEBUG messages about an unrelated publisher's unknown event-type. Verified by direct grep `[Orchestrator] auto_action proposal`: ZERO matches.

| Substrate | Soak v5 firing count | Pre-arc baseline |
|---|---|---|
| `auto_action_proposal_sse` (Arc 1 hook) | **0 (CORRECTED)** | 0 (substrate was dead code) |
| `[Orchestrator] auto_action proposal` log | 0 | 0 |
| Successful VERIFY phase completions | **0** | varies |
| `AdvisoryAction` events from substrate | 0 | 0 |
| `cluster_coverage_emit` (CIS arc + fastembed addendum) | **4** | 0 across 4 prior soaks |
| `goal_inference_inject` (MissionInferrer arc) | **17** | 0 (default-off pre-graduation) |
| `goal_inference_built` (cache miss path) | 1 | n/a (per-`refresh_s` cadence; correct) |
| `plan_shadow_completed` (Phase B subagents) | 2 | n/a |
| `bg_op_completed` (BackgroundAgentPool) | 19 | varies |

**Honest empirical state**: cluster_coverage + MissionInferrer + Phase B subagents fired (CONTEXT_EXPANSION-phase substrates). But ZERO ops reached VERIFY phase, so Arc 1 hook never had a chance to fire. The 19 BackgroundAgentPool completions take a different code path that doesn't traverse the full 11-phase FSM.

**`cluster_coverage_emit: 4` still validates the fastembed addendum** — earlier soaks (`bt-2026-05-03-021235`, etc.) had 0 cluster_coverage emissions. The adaptive-embedder substrate (stdlib hashing fallback) is empirically the difference at the CONTEXT_EXPANSION layer.

**New deeper defect surfaced**: the harness produces background ops without traversing the full pipeline. Until ops reach VERIFY phase, the Arc 1 oracle→auto_action wiring stays dormant. This is upstream of Defect #2 (`run_periodic` boot wire-up) — fixing #2 makes the observer tick, but the hook still won't fire without VERIFY-phase ops.

## Substrate that did NOT fire — real wiring gap

**`production_oracle_observer_tick: 0`**. The periodic observer's `run_periodic()` loop never executed during the 62-min soak. The 3 auto_action_proposals therefore came from:
1. The orchestrator VERIFY hook's `_read_recent_oracle_observation()` returning either None (which would trigger NO_ACTION fall-through, not the 3 firings observed) OR
2. Some other code path triggered the observer's `tick_once` once or twice (need to confirm)

**Action item**: the observer's `run_periodic` needs to be explicitly STARTED by the harness boot. Currently the `get_default_observer()` factory creates the observer but nothing schedules `run_periodic()` as an asyncio task. This is the boot wire-up that ships separately from the substrate.

## Systemic defects ranked

### Defect #1 — WallClockWatchdog 22-minute fire delay (HEADLINE)
- Armed `23:04:23` with max_wall_seconds=2400
- Fired `00:05:07` with wall time 3696s — **21.6 min after cap was hit**

This is a real bug, NOT just slow tick. The watchdog check loop is either:
- On too-long a sleep interval (e.g., 60s tick, but should be 5-10s for a 2400s cap)
- Being asyncio-starved by other coroutines
- Getting blocked by a synchronous call (the BoundedShutdownWatchdog's design uses `os._exit(75)` to bypass blocked asyncio — but the WallClockWatchdog is the layer that DETECTS the cap)

**Pattern repeats across recent soaks**: every soak ending in `wall_clock_cap+atexit_fallback` is this bug. Until fixed, the W2(5) 3-clean-session arc for graduating META_PHASE_RUNNER + REPLAY_EXECUTOR will repeatedly fail CB5.

### Defect #2 — PersistentIntelligence readonly-DB recurrent error
- **12 occurrences** in 62 min = ~1 every 5 min
- `[PERSISTENT-INTELLIGENCE] Checkpoint error: attempt to write a readonly database`
- The substrate continues; whatever the manager checkpoints is being lost
- No SSE event, no GET surface, no verdict failure — exactly the silent-degradation pattern from the brutal review

### Defect #3 — CandidateGenerator EXHAUSTION + unhandled task exceptions
- **3 EXHAUSTION events**, all with `remaining_s=0.0` (deadline exhausted before fallback attempt)
- **4 `Task exception was never retrieved`** asyncio errors — orchestrator's task await isn't catching `RuntimeError('all_providers_exhausted')`
- 1 `[ClaudeProvider] STREAM RUPTURE (phase=ttft): no chunk for 120s`

The pattern: ops with `remaining_s=0.0` re-attempt the same broken provider chain without backoff or budget reset, then the resulting RuntimeError bubbles unhandled. The Move 2 24h Burn-In arc shipped Stream Rupture / Transport Resilience layers — they fired 1 stream rupture cleanly, but the EXHAUSTION cascade is upstream of those layers.

### Defect #4 — `attempted=0` summary counter bug (documented in CLAUDE.md)
- summary.json says `stats: {attempted=0, completed=0, failed=0}`
- log shows 43 routed ops (20 standard + 17 background + 6 immediate) and 19 bg-worker completions
- CLAUDE.md notes: "the canonical source of truth for any 'did the loop work' question is the session `debug.log`, not `summary.json` (which has a known `attempted` counter bug)"
- Until the counter is fixed, automated soak-success metrics that rely on summary.stats are unreliable

## Recurring pathologies (continuing patterns)
- `doc_staleness: 40` emissions in 62 min — the historical fixation pattern continues even with cluster_coverage now firing
- Provider routing skew: standard:20, background:17, immediate:6 — the IMMEDIATE-route ops are the ones hitting EXHAUSTION

## What this confirms about O+V's current state

Cross-referencing the brutal review (B− grade):
- **Empirical activation: D+ → C+ for the recent arcs** — auto_action_router, MissionInferrer, ClusterIntelligence (with fastembed addendum) all empirically firing. Production Oracle observer is the lone dead-tick.
- **Operational reliability unattended: still C** — wall-cap overrun + 12 silent-DB errors + 3 EXHAUSTION + 4 unhandled task exceptions. Clean termination remains the exception, not the norm.
- **Long-horizon convergence: still F** — `INSUFFICIENT_DATA`. Until ops actually flow through GENERATE → APPLY successfully (rather than getting routed to background completion), convergence machinery has no signal.

## Top 3 follow-up arcs ordered by leverage

1. **WallClockWatchdog fire-delay fix** — single highest-leverage defect. Until clean termination is the norm, soak validation cannot graduate META_PHASE_RUNNER + REPLAY_EXECUTOR per the playbook. Probably 1-2 hour arc.
2. **`production_oracle_observer.run_periodic` boot wire-up** — currently the observer is constructed but never run as an asyncio task. Without this, the observer's history ring buffer never populates and the GET `/observability/production-oracle` endpoint returns empty. Single-slice arc.
3. **PersistentIntelligence readonly-DB root cause + SSE event** — silent degradation pattern is exactly what the brutal review called out. Either fix the DB permission OR convert the silent error into an SSE-published `persistent_intelligence_degraded` event so operators see it in real time.

The orchestrator unhandled-task-exception pattern (defect #3) is a #4 priority — it's a real bug but the EXHAUSTION root cause is upstream and possibly env-specific.

## Files referenced

- `scripts/pass_b_soak_assertion.py` (Arc 3 from prior session work)
- `scripts/production_oracle_closure_verdict.py`
- `scripts/auto_action_oracle_closure_verdict.py`
- `.ouroboros/sessions/bt-2026-05-03-060330/{summary.json,debug.log}`

This session's running tally now: 4 verdicts produced (Pass B soak / Production Oracle / Auto-Action / Soak v5 findings); 14+ arcs closed; 3 systemic defects empirically characterized for the next session's queue.
