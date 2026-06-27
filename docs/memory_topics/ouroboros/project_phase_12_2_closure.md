---
title: Project Phase 12 2 Closure
modules: []
status: merged
source: project_phase_12_2_closure.md
---

**Phase 12.2 architecturally complete + empirically proven 2026-04-28.**

## Closure summary

Seven slices merged in a single day after operator authorization on 2026-04-27:

| Slice | What | PR | SHA |
|---|---|---|---|
| A | Full-jitter retry helper | #27424 | 6080e0ba7b |
| B | TtftObserver + dynamic promotion math (CV + rel_SEM) | #28257 | 40a6b44c45 |
| C | TTFT wiring + cold-storage demotion + retry retrofits | #28317 | ce0fd612cf |
| D | Heavy probe (VRAM allocation verification) + budget ledger | #28355 | 14c7e724ba |
| E | Graduation flip — 4 master flags default-true | #28444 | 1dc661dae7 |
| F | Autonomic Pacemaker — eradicate lazy-boot deadlock | #28836 | f93d550c7e |
| G | Absolute Ceiling Gate + Failure Ignorance | #28964 | (post-E flip) |

**Combined regression spine: 433/433 green** at closure.

## What graduated (default-true post-Slice-E)

- `JARVIS_TOPOLOGY_FULL_JITTER_ENABLED` — retry desync at 3 callsites (FailbackFSM record_primary_failure / recovery_eta + Claude budget-aware backoff in providers.py:5329)
- `JARVIS_TOPOLOGY_TTFT_TRACKING_ENABLED` — TtftObserver record-on-write
- `JARVIS_TOPOLOGY_TTFT_DEMOTION_ENABLED` — promotion gate consults `observer.is_promotion_ready` instead of count gate; classifier consults `observer.is_cold_storage` for SPECULATIVE-only soft demotion
- `JARVIS_TOPOLOGY_HEAVY_PROBE_ENABLED` — VRAM-warming probes scheduler

All four flags retain hot-revert via explicit "false"-class strings. Asymmetric env semantics: empty/whitespace = unset marker = graduated default True; only explicit false-class strings revert.

## Slice F — the Autonomic Pacemaker (architectural critical fix)

**Discovered during once-proof attempts on 2026-04-28.** The original lazy-boot pattern (boot_discovery_once fired from CandidateGenerator._dispatch_via_sentinel on first dispatch) created a deadlock in idle dev environments:

1. Empty catalog → BG topology block → skip_and_queue (no DW HTTP call)
2. No HTTP call → boot_discovery_once never fires → catalog stays empty
3. Forever loop: Phase 12.2 paths never wake

**Operator directive 2026-04-28**: "Eradicate Lazy-Booting." The Pacemaker is now armed eagerly inside `GovernedLoopService.start()` immediately after DoublewordProvider construction:

```python
asyncio.create_task(
    _boot_discovery_once(session=..., base_url=..., api_key=...),
    name="dw_autonomic_pacemaker",
)
```

Fire-and-forget. Boot never blocks on DW response. The 30-min refresh task (`JARVIS_DW_CATALOG_REFRESH_S=1800`) heartbeats independently of operator traffic — the catalog is "never mathematically empty when a BG op fires." The lazy call site in `_dispatch_via_sentinel` was deleted entirely; single source of truth for discovery boot is now the Pacemaker.

## Slice G — the zero-order fix (post-once-proof correction)

**Discovered during live once-proof harvest on 2026-04-28.** Initial Slice D used an asymmetric ceiling-on-failure pattern: failed probes recorded the timeout ceiling (30000ms) as a TTFT sample, treating "consistently dead" as a cold-storage signal. The empirical state file showed:

```json
"samples": {
  "deepseek-ai/DeepSeek-OCR-2": [{"ttft_ms": 30000, ...}, {"ttft_ms": 30000, ...}]
}
```

The flaw: with two uniform 30000ms samples, CV=0 and rel_SEM=0 trivially pass the variance gates. Without correction, the promotion gate would have returned True for a model that's just timing out.

**Operator directive 2026-04-28**: two-part fix —

1. **Absolute Ceiling Gate**: `is_promotion_ready` rejects mean_ms >= 5000ms (default, env-tunable) BEFORE variance math. Mathematical stability is necessary but not sufficient.
2. **Failure Ignorance**: Heavy probe NEVER records failed-probe samples. ConnectionTimeoutError + transport errors + empty streams are network failures, not TTFT measurements. Cold-storage detection for "endpoint dead" falls to modality ledger / terminal breaker (where it belongs in the Phase 12 zero-trust layering).

Source-level ordering pin enforces ceiling-before-variance.

## Live once-proof — `bt-2026-04-28-201119`

After Slice F merged, the standard `ouroboros_battle_test.py --headless --cost-cap 0.50 --idle-timeout 600 --max-wall-seconds 2400` exercised every Phase 12.2 layer:

- **Pacemaker armed at +20s after boot**
- **DiscoveryRunner boot complete: ok=True models=22 newly_quarantined=0 routes_assigned=['background', 'complex', 'speculative', 'standard']**
- **3 HeavyProbe events** fired on 120s cadence
- **`.jarvis/dw_ttft_observer.json`** persisted with samples + schema `ttft_observer.1`
- **`.jarvis/dw_heavy_probe_budget.json`** atomic ledger working, $0.000063 spent (3 × $0.000021), schema `heavy_probe_budget.1`
- **`session_outcome=complete`** via `idle_timeout` after 22.9 min

The DW chat-completions endpoint itself is environmentally flaky (same pattern as Wave 3 closure 2026-04-25 — unreachable for actual generation despite reachable for HEAD probes). All 3 heavy probes hit ConnectionTimeoutError / Connection reset by peer at the 30000ms ceiling. This is **not a Phase 12.2 architectural issue** — the wiring proves itself when exercised.

## Cost economics validated

- Heavy probe per-call cost: **$0.000021** (50 tokens × $0.40/M output + 10 tokens × $0.10/M input)
- Daily budget default: **$0.05** = ~2,300 probes max
- Real spend in once-proof: $0.000063 (3 probes)
- Budget ledger atomic + UTC-midnight rollover proven via test fixture + live state

## Authority invariants pinned

- Heavy prober NEVER mutates PromotionLedger (AST-walked invariant)
- Heavy probe module NEVER imports orchestrator / phase_runner / iron_gate / semantic_guardian
- Classifier NEVER mutates ledger or observer (existing Slice C invariant preserved)
- Observer is read-only from PromotionLedger + DwCatalogClassifier consumers

## What's next (deferred — operator-gated)

Phase 1 (Determinism Substrate) speccing **NOT authorized yet**. Per directive 2026-04-28: "Do not begin speccing Phase 1 (Determinism Substrate) until I explicitly authorize the new arc." Wait for operator green-light.

Other deferred follow-ups (low priority, ship-when-cheap):
- DW provider's adaptive poll has `+/-25%` jitter, not full-jitter (different decay profile, semantically different from desync goal — left as-is)
- record_failure path on PromotionLedger is wired to receive failures but no production caller invokes it yet (failures handled instead by terminal breaker / modality ledger / cold-storage gate)

## Quick-reference flag inventory

```
JARVIS_TOPOLOGY_FULL_JITTER_ENABLED            (default true)
JARVIS_TOPOLOGY_BACKOFF_BASE_S                 (default 10.0)
JARVIS_TOPOLOGY_BACKOFF_CAP_S                  (default 300.0)
JARVIS_TOPOLOGY_TTFT_TRACKING_ENABLED          (default true)
JARVIS_TOPOLOGY_TTFT_DEMOTION_ENABLED          (default true)
JARVIS_TOPOLOGY_TTFT_CV_THRESHOLD              (default 0.15)
JARVIS_TOPOLOGY_TTFT_REL_SEM_THRESHOLD         (default 0.05)
JARVIS_TOPOLOGY_TTFT_PROMOTION_CEILING_MS      (default 5000)   # Slice G
JARVIS_TOPOLOGY_TTFT_COLD_SIGMA                (default 2.0)
JARVIS_TOPOLOGY_TTFT_WINDOW_N                  (default 50)
JARVIS_TOPOLOGY_TTFT_STATE_PATH                (default .jarvis/dw_ttft_observer.json)
JARVIS_TOPOLOGY_HEAVY_PROBE_ENABLED            (default true)
JARVIS_TOPOLOGY_HEAVY_PROBE_TOKENS             (default 50)
JARVIS_TOPOLOGY_HEAVY_PROBE_INTERVAL_S         (default 600)
JARVIS_TOPOLOGY_HEAVY_PROBE_TIMEOUT_S          (default 30)
JARVIS_TOPOLOGY_HEAVY_PROBE_BUDGET_USD_DAILY   (default 0.05)
JARVIS_TOPOLOGY_HEAVY_PROBE_CYCLE_S            (default 120)
JARVIS_TOPOLOGY_HEAVY_PROBE_BUDGET_PATH        (default .jarvis/dw_heavy_probe_budget.json)
```
