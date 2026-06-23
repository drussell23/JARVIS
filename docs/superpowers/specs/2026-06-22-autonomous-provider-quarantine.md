# Autonomous Provider Quarantine Matrix — Design Spec

> **Arc:** the live soak proved the full resilience stack fires, then localized the wall to DW throughput collapse — and exposed a pathological immortal-retry loop (`dilation hops=77`, hammering a degraded upstream). When a GLOBAL provider outage is deduced from the FAILURE-RATE GRADIENT (NOT a hardcoded retry count), the op must terminally quarantine into cold storage instead of spinning forever — a good DW citizen, and resumable when DW recovers.
> **Date:** 2026-06-22. Branch `worktree-provider-quarantine`. Built into the existing immortal backstop seam.

## 1. Diagnosis (reuse-first)
- **Immortal re-queue seam:** `candidate_generator.py:~4750` — `if immortal_should_retry(...)` then recursive `_dispatch_via_sentinel(_immortal_attempt+1)` (the loop that spun to `hops=77`). The quarantine intercepts HERE, before the re-queue.
- **Full-fleet-sweep signal:** `candidate_generator.py:~4563` ("exhausted all N DW models", `fallback_tolerance=="queue"`) = a single dispatch where every ranked model failed (success rate 0.0 across the fleet).
- **Lane collapse:** `convergence_watchdog.LaneDilationTracker` (the `[SOVEREIGN YIELD: LANE COLLAPSE]` site) = both batch+realtime lanes TIMEOUT.
- **Why a NEW rate signal is needed:** `topology_sentinel` weights `FSM_EXHAUSTED`/`GENERATION_TIMEOUT` at **0.0** (our-side faults) so DW timeouts never trip its breaker → models never go OPEN on this failure mode → its state cannot deduce this outage. So the gradient must track the DW-timeout-sweep outcomes directly.
- **Cryo-DLQ:** `intake_dlq.append_dlq(envelope, reason=)` + `replay_dlq(path, ingest_fn)` (resume on recovery). Envelope serialized via `to_dict()`/dict.
- **Yield:** `convergence_watchdog.emit_sovereign_yield(op_id, *, reason="UPSTREAM_QUARANTINE", ...)` (the `reason` kwarg already exists).

## 2. Goals / Non-Goals
**Goals.** (G1) Deduce a GLOBAL DW outage from the **failure-rate gradient** — NO hardcoded retry-count N. (G2) On a deduced outage, at the immortal re-queue seam, do NOT re-queue: emit terminal `[SOVEREIGN YIELD: UPSTREAM QUARANTINE]` + seal the op in the Cryo-DLQ with the DW timeout/latency telemetry. (G3) Resumable: the Cryo-DLQ replays flawlessly when DW recovers. (G4) Reuse the immortal seam, intake_dlq, emit_sovereign_yield — no parallel queue. (G5) Default-on, fail-soft, OFF byte-identical (legacy immortal loop).

**Non-Goals.** No change to the per-op generation deadline / the lane escalation / the temporal breaker (all working). No new provider rotation. Not a hardcoded `hops > N` cap (explicitly rejected).

## 3. Components
### 3.1 `provider_quarantine.py` (new pure leaf — the degradation gradient)
- `class ProviderHealthGradient`: a per-route **rolling success-rate window** (bounded deque of recent full-fleet-sweep outcomes, env `JARVIS_QUARANTINE_WINDOW` default 5; reuse the ReductionTracker/LaneDilationTracker bounded-deque+singleton pattern — justified: the existing trackers ignore weight-0.0 timeouts). `record_sweep(route, *, success: bool) -> None`; `success_rate(route) -> float`; `is_global_outage(route) -> bool` = window is FULL **and** `success_rate == 0.0` (absolute 0.0 across a full programmatic sweep — the user's threshold; the window is the velocity/gradient, NOT a retry count). Fail-soft.
- `quarantine_enabled() -> bool` (`JARVIS_PROVIDER_QUARANTINE_ENABLED`, default **true**).
- `get_provider_health_gradient()` singleton.
- The gradient also RECOVERS: a single successful sweep pushes `success_rate > 0.0` → outage clears → new ops dispatch normally (autonomous un-quarantine; no manual reset).

### 3.2 Quarantine action (`provider_quarantine.quarantine_op` or inline)
`quarantine_op(ctx, *, route, telemetry: dict) -> None`: (a) `emit_sovereign_yield(op_id, reason="UPSTREAM_QUARANTINE", ...)` → logs `[SOVEREIGN YIELD: UPSTREAM QUARANTINE]` + SSE; (b) `append_dlq(ctx, reason="upstream_quarantine:dw_global_outage")` with the DW telemetry (fleet swept, lanes collapsed, last timeout mode, dilation hops) attached to the envelope for flawless resume. Fail-soft (any error → fall back to the legacy immortal path; the op is NEVER lost).

### 3.3 Wiring into the immortal seam (`candidate_generator` ~4750)
At the immortal re-queue decision: `record_sweep(route, success=False)` on a full-fleet exhaustion; then `if quarantine_enabled() and get_provider_health_gradient().is_global_outage(route): quarantine_op(ctx, route, telemetry); raise <terminal quarantine error>` (do NOT recurse into the immortal re-queue). Else → the existing immortal retry (unchanged). On a successful dispatch elsewhere, `record_sweep(route, success=True)` so the gradient recovers. Gated; OFF → exact legacy immortal loop.

### 3.4 Resume on recovery
The Cryo-DLQ is the existing `intake_dlq` (reason `upstream_quarantine`). `replay_dlq` re-ingests quarantined ops; trigger it on the next boot (existing DLQ-replay-on-boot) AND/OR when the gradient recovers (`success_rate > 0.0`). Reuse — no new store.

## 4. Cross-cutting
- **No hardcoded retry-N:** the trigger is `success_rate == 0.0 over a rolling window` (a rate/gradient), window size env-tunable; NOT a `hops > N` count. The dilation cap stays as-is (orthogonal).
- **Fail-soft + OFF byte-identical:** any quarantine error → legacy immortal path; op never lost (the I1 guarantee). Default-on; OFF → unchanged.
- **Good-citizen:** a quarantined op stops hammering DW (no immortal re-queue on a deduced outage) and is cold-stored for resume — exactly the DW-citizenship principle.
- **Reuse-first:** immortal seam, intake_dlq, emit_sovereign_yield, the bounded-deque pattern. No parallel queue/loop.

## 5. Tests
- `ProviderHealthGradient`: window fills; `success_rate==0.0` over a full window → `is_global_outage` True; one success → rate>0 → outage clears (recovery); fail-soft. No hardcoded N (window env-tunable).
- `quarantine_op`: emits `[SOVEREIGN YIELD: UPSTREAM QUARANTINE]` + appends to DLQ with telemetry; fail-soft.
- Intercept: a deduced outage at the immortal seam → quarantine + terminal (NOT immortal re-queue); a transient (window not all-failed) → normal immortal retry; OFF byte-identical. Cryo-DLQ envelope is replay-shaped.
- Static: a full-outage op terminally quarantines (no infinite immortal loop) + is resumable; OFF unchanged.

## 6. Phasing
1. `provider_quarantine.py` (gradient + quarantine action) + tests. 2. Wire into the immortal seam + the recovery record_sweep + tests. 3. Integration + final review (confirm: no immortal loop on outage; legacy byte-identical when off; the intercept is on the LIVE immortal path). Then (operator) a future soak when DW has recovered.
