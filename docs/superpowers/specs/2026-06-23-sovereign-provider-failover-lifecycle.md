# Sovereign Provider Failover Lifecycle + Recovery Forecaster — Design Spec

> **Arc.** The live soak proved the full resilience stack fires and localized the wall to DW throughput collapse. The Provider Quarantine Matrix (#69671) now seals ops into a Cryo-DLQ instead of hammering a dead upstream. This arc adds the **other half**: when DW collapses (and Claude is depleted), **GCP J-Prime — the self-hosted Mind — awakens from its golden-image snapshot, carries generation through the outage, and tears itself back down the instant DW is sustainably healthy again.** Cost is bounded to the outage window. A *recovery forecaster* (EWMA + live velocity gradient — NOT heavy ML on sparse data) makes the awaken/probe/handback timing intelligent.
>
> **Date:** 2026-06-23. Branch `worktree-sovereign-failover-lifecycle`.
> **Trinity context.** Body (JARVIS / O+V) = this repo. Mind (J-Prime, `jarvis_prime`) = 11 self-hosted GGUF specialists on an `e2-highmem-4` CPU node; **code → `Qwen2.5-Coder-7B` (70.4% HumanEval)**. Nerves (Reactor-Core) = training / experience-collection / model-deployment (later phase).

---

## 1. Goals / Non-Goals

**Goals.**
- **G1 — Sovereign failover.** When the Quarantine Matrix deduces a global DW outage (and Claude is unavailable), awaken J-Prime as the Tier-2 generation provider and route generation to it — so O+V is *never* hard-blocked by vendor collapse.
- **G2 — Cost-bounded to the outage.** J-Prime dormant cost ≈ **$0.50/mo** (golden-image snapshot only; VM + disk deleted). Active cost = outage-hours × cheap CPU rate. Hard ceiling = the existing `IntelligentGCPOptimizer` **$5/day** budget.
- **G3 — Intelligent, observed-gated handback.** Detect DW recovery via a cheap probe; hand back to DW (primary) and tear J-Prime down on **sustained** recovery (hysteresis, anti-thrash). The forecast *paces* this; an **observed** probe *decides* it.
- **G4 — Recovery forecaster.** EWMA-MTTR + percentile bands + a live within-outage recovery-velocity gradient, robust on sparse outage data. Drives the three advanced layers below.
- **G5 — Experience capture (RSI substrate).** Every outage→recovery is recorded locally AND exported asynchronously to Reactor-Core via TrinityEventBus — the dataset the make-the-model-better flywheel later trains on.
- **G6 — Reuse-first, fail-soft, OFF byte-identical, gated.** Compose the existing quarantine gradient, transport-breaker HALF-OPEN probe, `dw_surface_health`, `gcp_vm_manager` idle-stop/golden-image, TrinityEventBus, `telemetry_ingestor`. No parallel machinery.

**Non-Goals.**
- Not Reactor-Core *training* (that's the later, budgeted flywheel phase — this spec only *feeds* it).
- Not the per-sub-goal cognitive-complexity bounding / `LOCAL COGNITIVE OVERLOAD` decompose (a companion concern; a 7B coder is far above the 3B ceiling that motivated it — referenced in §9, not core here).
- Not changing DW primacy: J-Prime is **last-resort**, never primary. DW recovers → DW resumes.
- No heavy ML (LSTM/transformer) for forecasting — sparse outage data makes it overfit; explicitly rejected.

---

## 2. Architecture — the Lifecycle FSM + the Forecaster

The controller is a small FSM over the *provider-fleet health state*, driven by the quarantine gradient + the forecaster:

```
        ┌────────────────────────────────────────────────────────────────┐
        │ DORMANT  (DW healthy primary; J-Prime = golden-image snapshot)   │
        └──────────────┬─────────────────────────────────────────────────┘
   quarantine.is_global_outage(DW)==True  AND  Claude unavailable
                       │   (Cryo-Trigger §5: forecast says R_remaining > cold-start cost)
                       ▼
        ┌──────────────────────────────────────────────────────────────┐
        │ AWAKENING  (create VM from snapshot/golden image, ~boot+load)  │
        └──────────────┬───────────────────────────────────────────────┘
              ensure_static_vm_ready() healthy
                       ▼
        ┌──────────────────────────────────────────────────────────────┐
        │ SERVING  (route generation → J-Prime Tier-2; DW recovery probe │
        │           loop paced by Adaptive Polling §3)                   │
        └──────────────┬───────────────────────────────────────────────┘
        DW sustainably healthy (gradient recovered ≥ window + hysteresis + cooldown)
                       ▼
        ┌──────────────────────────────────────────────────────────────┐
        │ HANDBACK  ([SOVEREIGN YIELD: UPSTREAM RECOVERED] → route DW →   │
        │            delete-to-snapshot J-Prime)                         │
        └──────────────┬───────────────────────────────────────────────┘
                       ▼ back to DORMANT
```

**The reactive floor (correctness baseline, forecast-INDEPENDENT).** With zero forecasting the FSM is still correct: AWAKEN after a fixed confirm window once outage is deduced; PROBE at a fixed interval; HANDBACK on sustained observed recovery. The forecaster is a *pure optimization layer* on top — it can be wrong and the system stays correct (it only mis-paces). This is the load-bearing invariant: **observed probes are authoritative; the forecast is advisory.**

---

## 3. Layer 1 — Dynamic Polling Backoff (The Throttle)

The DW-recovery probe interval is a function of the forecast, never static.

**Inputs.** `t` = current outage elapsed (monotonic); `p50`, `p90` = forecaster recovery-time bands (§6); `I_min`, `I_max` = probe interval clamps (env).

**The throttle function** `probe_interval(t, p50, p90)`:
- **Far below p50** (`t < p50`, large `Δ = p50 − t`): probe *sparsely* — recovery is statistically unlikely yet. Interval scales with `Δ` toward `I_max` (don't waste probes or load a recovering DW early).
- **Approaching p50** (`Δ → 0`): probe interval **accelerates toward `I_min`** — recovery is statistically imminent; we want to catch it fast (minimize handback latency = minimize J-Prime cost).
- **Overshoot past p90** (`t > p90`): the forecast was wrong / the outage is anomalous → **exponential backoff** of the interval (toward `I_max`) to preserve local compute + network and avoid pestering a deeply-degraded DW. Reuse the existing `circuit_breaker.full_jitter_delay` exponential+jitter primitive (do NOT reimplement backoff).

```
probe_interval(t) =
    t < p50 :  clamp(I_min, I_max, I_min + k_pre * (p50 - t))      # decelerate-to-sparse far out
    p50≤t≤p90: I_min                                               # dense probing in the likely window
    t > p90 :  clamp(I_min, I_max, full_jitter_delay(attempt=n_overshoot))  # exponential back-off
```

**Authority.** The interval is *pacing only*. A probe firing does not hand back — a probe **succeeding (sustained, §4)** does. A wrong forecast changes only *when we look*, never *what we conclude*. Cheap probe = a tiny health ping / minimal completion, **never** a full generation (good-citizen: no hammering a recovering DW).

**Component.** `recovery_throttle.py` (pure): `probe_interval(t, p50, p90, *, cfg) -> float`. Gated `JARVIS_RECOVERY_THROTTLE_ENABLED` (default true); OFF → fixed `I_default`.

---

## 4. The Recovery Confirmation + Anti-Thrash (the HANDBACK gate)

Reuse the quarantine `ProviderHealthGradient` (the rolling success-rate window) as the recovery signal — it already *recovers* when DW sweeps succeed. Add a symmetric **recovered** predicate:
- `is_recovered(route)` = window FULL **and** `success_rate(route) >= JARVIS_RECOVERY_THRESHOLD` (default e.g. 0.6 — sustained, not a single flicker).
- **Hysteresis:** require `is_recovered` to hold across `N` consecutive probe cycles (`JARVIS_RECOVERY_HYSTERESIS_CYCLES`, default 2) **+** a `JARVIS_JPRIME_MIN_UPTIME_S` floor (don't tear J-Prime down seconds after awakening) **+** a `JARVIS_HANDBACK_COOLDOWN_S` before a *re-awaken* is allowed (kills the awaken↔handback churn cycle).

Reuse `dw_surface_health` (HEALTHY = "last probe completed without error") for the per-probe verdict feeding the gradient. Recovery probes use the transport-breaker **HALF-OPEN async probe** machinery already built (`transport_circuit_breaker` self-heals via HALF-OPEN; `dw_transport_recovery` owns the jitter window) — we consume its verdict rather than reimplement probing.

On confirmed recovery → emit `[SOVEREIGN YIELD: UPSTREAM RECOVERED]` (reuse `emit_sovereign_yield(reason=...)`) → route generation back to DW → trigger HANDBACK teardown (§5).

---

## 5. Layer 3 — Cryo-Trigger Integration (forecast → awaken-from-snapshot threshold)

Synthesizes the forecaster with the **Delete-to-Snapshot** dormancy strategy. Two decision points, both forecast-shaped, both cost-safe:

**5a. AWAKEN threshold (when to pay the spin-up).** Spinning up J-Prime costs a cold-start `C` (boot + model load; ~87s from a stopped VM, ~2–5 min from a deleted-to-snapshot recreate) + the active compute. The math:

```
Let R = forecast remaining recovery time (p50, or a chosen percentile).
Let C = J-Prime cold-start cost (env JARVIS_JPRIME_COLDSTART_S, measured).
AWAKEN  iff  quarantine.is_global_outage(DW)  AND  Claude_unavailable
             AND  R > C * JARVIS_CRYO_AWAKEN_MARGIN   (default 1.5)
```

- If `R > C·margin`: by the time J-Prime is ready, DW will *still* be down and J-Prime will serve a meaningful window → the spin-up pays off. Awaken.
- If `R < C` (DW likely back before J-Prime even finishes booting — a blip): **do NOT awaken.** The quarantine Cryo-DLQ holds the op briefly; DW recovers; no money spent waking J-Prime for a flicker. *This is the core cost-saver of the cryo-trigger.*
- **Cold-start confidence gate:** if the forecaster lacks history (cold-start, <`JARVIS_FORECAST_MIN_SAMPLES`), `R` is unknown → fall back to the reactive floor (awaken after a fixed `JARVIS_OUTAGE_CONFIRM_S` window). Never block awakening on an absent forecast.

**5b. SLEEP (delete-to-snapshot on handback).** On confirmed recovery (§4): route to DW, then **delete-to-snapshot** — tear down the J-Prime VM + disk, preserving only the golden-image snapshot (~$0.50/mo dormant). Reuse `gcp_vm_manager`'s golden-image create path (`source_image`, `ensure_static_vm_ready`) for awaken and its delete/stop lifecycle for sleep.

**Authority.** The cryo-trigger decides *when to pay* the spin-up (a cost optimization); routing to J-Prime only happens once it's *observed* ready AND DW is *observed* still down; handback is *observed*-gated (§4). A wrong forecast = a slightly early/late spin-up (a bounded cost wobble) — never a correctness break or a lost op.

**Component.** `failover_lifecycle.py` — the FSM controller. Consumes: quarantine gradient, forecaster, throttle, `gcp_vm_manager`. Gated `JARVIS_FAILOVER_LIFECYCLE_ENABLED` (default **false** until the golden image exists + a soak validates — flip after Phase 3). OFF → today's behavior (quarantine → Cryo-DLQ, no J-Prime).

---

## 6. The Recovery Forecaster (EWMA + velocity gradient — robust on sparse data)

`recovery_forecaster.py` (pure, stdlib + math). Reads the OutageLedger (§7).

- **v1 — EWMA-MTTR + percentile bands.** Maintain an EWMA of historical outage durations + a simple percentile estimate (p50/p90) from the bounded recent-outage window. Works with 3–5 samples; interpretable; cannot overfit. `forecast(now) -> RecoveryForecast{p50_s, p90_s, samples, confidence}`.
- **v1.5 — within-outage recovery-velocity gradient.** During the *live* outage, track the probe trajectory (latency trend + first intermittent successes). A turning trajectory (latencies dropping / sporadic 200s) is the strongest *leading* indicator — bias `R` downward when the gradient turns positive. This is "this outage," not history — the highest-signal input.
- **v2 (optional, later) — conditioning** on time-of-day / failure-mode (a 5xx storm ≠ a batch backlog). Still a simple conditioned estimator.
- `confidence` low (cold-start) → consumers fall back to reactive-floor constants. Fail-soft → returns a conservative default forecast, never raises.

Gated `JARVIS_RECOVERY_FORECAST_ENABLED` (default true; OFF → consumers use fixed constants = the reactive floor).

---

## 7. Layer 2 — Trinity Telemetry Bridge + the Outage Ledger (async, non-blocking)

**Two stores, deliberately separate:**

**7a. Local OutageLedger** (`outage_ledger.py`, in-process, append-only, bounded). Records each outage lifecycle: `{outage_id, started_ts, ended_ts, duration_s, failure_mode, error_codes, lane, model_ids, dilation_hops, served_by_jprime, jprime_uptime_s, recovery_probe_trajectory}`. **The forecaster reads THIS** — in-process, zero cross-repo dependency. Durable at `.jarvis/outage_ledger.jsonl` (small, bounded ring). The forecast must work even if Reactor-Core is entirely offline.

**7b. Trinity export (async, fire-and-forget).** On outage-detected and outage-recovered, publish a TrinityEventBus event carrying the DW-collapse metadata (timestamps, failure modes, error codes, lane, model_ids). **Strictly non-blocking** — it rides the existing async publish pattern (the v244 command-lifecycle / unlock-telemetry path):
- `asyncio.create_task(...)` fire-and-forget with a strong task-ref (no GC), wrapped fail-soft — a dead bus / offline Reactor-Core **never** blocks or breaks the O+V operational DAG.
- Add event types `DW_OUTAGE_DETECTED` / `DW_OUTAGE_RECOVERED` to the Body→Nerves telemetry contract (mirroring the `VOICE_UNLOCK_*` precedent).
- Reactor-Core's `reactor_core/ingestion/telemetry_ingestor.py` maps them into the training dataset (the RSI substrate) — *that* side is a Reactor-Core change, scoped to Phase 4, but the **Body emission contract is defined here** so the bridge is ready.

**Invariant.** The Body's live forecast/lifecycle depend ONLY on the local ledger (7a). The Trinity bridge (7b) is additive export for training — its failure is invisible to O+V. Gated `JARVIS_TRINITY_OUTAGE_EXPORT_ENABLED` (default true; fail-soft).

---

## 8. Cost Model (the whole point)

| State | Billing | ~Cost |
|---|---|---|
| DORMANT (≈99% of time) | golden-image snapshot only (VM+disk deleted) | **~$0.50/mo** |
| AWAKENING/SERVING | `e2-highmem-2` (16GB, code-only) Spot ~$0.015/hr or on-demand ~$0.067/hr × outage hours | **cents–$ per outage** |
| Cryo-trigger blip-skip | `R < C` → never awaken for a flicker | **$0** |
| Hard ceiling | `IntelligentGCPOptimizer` daily budget | **$5/day cap** |

Realistic all-in: **~$1–3/month** vs ~$98–110 always-on. Spot-first / on-demand-fallback for the active burst is a minor (~$0.50/mo) bonus; the dominant levers are delete-to-snapshot dormancy + the cryo-trigger blip-skip.

---

## 9. Reuse Map (no parallel machinery)

| Need | Reuse |
|---|---|
| Outage detection | `ProviderHealthGradient.is_global_outage` (quarantine arc) |
| Recovery probe | `transport_circuit_breaker` HALF-OPEN async probe + `dw_transport_recovery` jitter window |
| Per-probe verdict | `dw_surface_health` (HEALTHY/degraded) |
| Recovery confirm | `ProviderHealthGradient.success_rate` + new `is_recovered` |
| Backoff/jitter primitive | `circuit_breaker.full_jitter_delay` |
| Yield surface | `emit_sovereign_yield(reason="UPSTREAM RECOVERED")` |
| VM awaken/sleep | `gcp_vm_manager` golden-image `source_image` create + `ensure_static_vm_ready` + delete/snapshot + `jprime_idle_stop` lifecycle |
| Budget cap | `IntelligentGCPOptimizer` `CostBudget` ($5/day) |
| Trinity export | `TrinityEventBus` + `telemetry/events.py` + `reactor_core/ingestion/telemetry_ingestor.py` |
| Tier-2 generation | `jarvis_prime_client` / the `PrimeProvider` seat (OpenAI-compatible `/v1/chat/completions`) |

*Companion (out of scope here, referenced):* per-sub-goal cognitive-complexity bounding (`LOCAL COGNITIVE OVERLOAD` → `decompose_for_block`) — useful if a smaller local model is ever used; a 7B coder largely obviates it. Tracked separately.

---

## 10. Error Handling / Invariants

- **Fail-soft absolute.** Any forecaster/throttle/bridge/VM error → fall back to the reactive floor (fixed intervals, conservative awaken) → the op is never lost (quarantine Cryo-DLQ remains the backstop).
- **OFF byte-identical.** `JARVIS_FAILOVER_LIFECYCLE_ENABLED=false` → today's behavior exactly (quarantine → Cryo-DLQ; no J-Prime).
- **Observed-gated authority** (load-bearing): the forecast only paces/optimizes; awaken-readiness and handback are observed-gated. A wrong forecast is a bounded cost wobble, never a correctness or op-loss event.
- **Good-citizen probing:** recovery probes are tiny health pings, never full generations; throttle backs off past p90 so a deeply-degraded DW is not pestered.
- **No cross-repo hard dependency:** the live forecast depends only on the local ledger; Reactor-Core may be entirely offline.

---

## 11. Phasing (precisely sequenced)

**Phase 1 — Golden Image Bake + The Telemetry Ledger (execute first, per operator).**
1. Bake a **code-only** golden image: provision a node, `download_recommended_models.py` for `Qwen2.5-Coder-7B` only, install `jarvis_prime`, snapshot → a small (~10–15GB) GCP image. One-time, a few dollars.
2. `outage_ledger.py` (7a) + the Trinity export contract (7b) wired to the quarantine outage-detect/recover points (fire-and-forget). The ledger starts accumulating immediately (even before J-Prime is wired) so the forecaster has data.

**Phase 2 — Forecaster + Adaptive Polling.** `recovery_forecaster.py` (§6) + `recovery_throttle.py` (§3), consuming the ledger. Pure, testable, no infra.

**Phase 3 — Cryo-Trigger + Lifecycle FSM + Tier-2 wiring.** `failover_lifecycle.py` (§5) + `is_recovered`/hysteresis (§4) + `PrimeProvider` Tier-2 routing + the awaken/handback VM lifecycle. Flip `JARVIS_FAILOVER_LIFECYCLE_ENABLED` after a soak validates an awaken→serve→handback→teardown cycle.

**Phase 4 — Reactor-Core training flywheel (later, budgeted).** `telemetry_ingestor` ingests the exported outage + generation-outcome dataset; DPO/curriculum fine-tunes the coder model; redeploy to J-Prime. Heavier GPU cost → budgeted bursts, only once experience justifies it.

---

## 12. Tests (per phase)

- **OutageLedger:** append/bounded-ring/durable round-trip; failure-mode + trajectory captured; fail-soft on corrupt file.
- **Trinity export:** fire-and-forget non-blocking (the publish never awaits / never raises into the DAG); dead-bus/offline-Reactor → no-op, O+V unaffected; event schema matches the contract.
- **Forecaster:** EWMA/percentile correct on 3–5 samples; velocity-gradient biases R down on a turning trajectory; cold-start → low confidence → conservative default; never raises.
- **Throttle:** `probe_interval` decelerates far below p50, hits `I_min` in the window, exponentially backs off past p90; OFF → fixed interval.
- **Recovery confirm:** `is_recovered` requires full window + threshold; hysteresis blocks single-flicker handback; cooldown blocks re-awaken churn.
- **Cryo-trigger:** `R > C·margin` → awaken; `R < C` (blip) → no awaken; cold-start → reactive-floor awaken; never awakens without `is_global_outage`.
- **Lifecycle FSM:** dormant→awaken→serve→handback→dormant happy path; OFF byte-identical; fail-soft at each transition → reactive floor → op never lost.
- **Static/integration:** observed-gated authority holds (a forced-wrong forecast never causes premature handback or op loss); cost-path (delete-to-snapshot) reachable.

---

## 13. Open Decisions (for operator review)

1. **Node size / model set for the bake:** code-only `Qwen2.5-Coder-7B` on `e2-highmem-2` (16GB, recommended cheapest) vs the full 11-model fleet on `e2-highmem-4` (32GB). *Spec assumes code-only.*
2. **Dormancy:** delete-to-snapshot (~$0.50/mo, ~2–5 min recreate) vs stop-when-idle (~$3–14/mo disk, ~87s start). *Spec assumes delete-to-snapshot* for max savings; the cryo-trigger's `C` accounts for the slower recreate.
3. **Active provisioning:** Spot-first/on-demand-fallback vs plain on-demand. *Spec assumes Spot-first/on-demand-fallback* (minor saving, reuses the `use_spot` paths).
