# Zero-Waste Substrate & Predictive Routing Arc — PRD

Status: **DRAFT for operator review. No implementation until "implement S1".**
NEW arc — NOT an OCA slice. OCA / git-index / sovereignty / cursor-agent-ban
are CLOSED and untouched.

---

## 1. Problem statement

Phase-1 SWE-Bench-Pro wiring soak was **INCONCLUSIVE** and burned the full
**$2.00** cap (estimate was ~cents). Three root causes, none a swe_bench_pro
code defect:

1. **Compute waste.** Redundant prompts were paid for; no response reuse.
2. **Budget cannibalization.** Autonomous sensors (OpportunityMiner /
   DocStaleness) spent the entire budget on JARVIS's own code before the
   injected fixture was ever reached. No predictive preemption.
3. **Environmental fragility.** Host sleep -> wall/monotonic clock skew ->
   `WallClockWatchdog` hard-killed (`stop_reason=wall_clock_cap`) before the
   fixture ran.

The fix is architectural, not a bespoke isolation script (symptom) or a hard
sensor throttle (workaround). The system must reuse compute, predict cost,
and dynamically route resources — composing existing substrates, not
duplicating them.

## 2. Compose diagram (extend, never parallel)

```
            EXISTING (compose)                  ARC ADDS (extend only)
  prompt_cache.PromptCache  --------------->  S1 ProviderResponseCache
   (OrderedDict LRU + TTL, get/put,             (response trajectory value,
    get_prompt_cache singleton)                  byte-budget LRU, repo-state key)
  semantic_index (embed+cosine) ----------->     `- semantic-similar tier
  cross_process_jsonl.flock_append_line ---->     `- cross-session persistence
                                                  v
  providers.ClaudeProvider.generate  <----- pre-call gate (hit => $0, skip API)
  doubleword_provider.generate       <----- pre-call gate

  admission_gate / admission_estimator ---->  S2 Forecasted_Cost dimension
   (budget-vs-projected-wait, EWMA)             (spend + forecast vs budget)
  sensor_governor (weighted caps,    <-----     `- ACTUATION: drive existing
   emergency brake, quarantine)                    quarantine; NO new router

  battle WallClockWatchdog (wall-cap) ------>  S3 monotonic-authoritative
   (time.time ages, wall-authoritative)          budget deadline + wall backstop
                                                 + sleep-vs-runaway skew classify
```

## 3. Audit — substrate -> extend -> forbidden duplication

| Substrate (file:line) | S? extends | MUST NOT duplicate |
|---|---|---|
| `prompt_cache.py:71 PromptCache` (`get`:106 `put`:127 `_evict_expired`:200 `_make_key`:210 `get_prompt_cache`:297; env `JARVIS_PROMPT_CACHE_MAX_ENTRIES`:41) | S1 | a second OrderedDict-LRU / TTL evictor / key-hash; reuse the eviction discipline |
| `providers.py` `ClaudeProvider.generate` + `GenerationResult` (import :34; parsers return GenerationResult :3782/:3855); `doubleword_provider.py:874 async def generate` | S1 (pre-call gate seam) | no fork of the provider classes; gate wraps, returns the same `GenerationResult` |
| `semantic_index.py` (fastembed->stdlib-tfidf embed + cosine) | S1 (semantic tier) | no new embedder / no hardcoded cosine constant |
| `cross_process_jsonl.flock_append_line` | S1 (persistence) | no new flock/JSONL primitive |
| `admission_gate.py:120 admission_gate_enabled` (+ gate fn ~:468); `admission_estimator.py:94 WaitTimeEstimator` (:363 `get_default_estimator`, EWMA) | S2 | no parallel admission/forecast engine |
| `sensor_governor.py:98 is_enabled` (emergency brake :148/:156/:540 `_emergency_brake_active`, weighted caps) | S2 (actuation) | **no new router**; drive the existing quarantine/brake |
| battle `WallClockWatchdog` (`scripts/ouroboros_battle_test.py:1305` arms `max_wall_seconds_s`; kill path emits `stop_reason=wall_clock_cap` + skew log; `time.time()` ages :242/:423) — exact watchdog module pinned at S3 kickoff (read-only locate, deferred to keep this review tight) | S3 | not `s/time.time/monotonic/`; dual-clock + skew classify |

## 4. S1 — Zero-Waste ProviderResponseCache (load-bearing constraints)

A design that violates ANY of these is rejected:

- Cache the **RESPONSE trajectory** (final text + tool-rounds metadata),
  not prompt-only.
- Key = `SHA-256(prompt + model + repo-state-digest + route)`. The repo
  digest **MUST** incorporate `git HEAD` + a staged/working dirty-hash so
  **any** staged/HEAD change invalidates the entry (no stale-fix
  application — correctness over savings).
- Eviction: **LRU by serialized BYTE budget** (`JARVIS_PROVIDER_CACHE_MAX_BYTES`,
  conservative default, 16GB-M1-safe) — entry count is insufficient
  (trajectories are large).
- **Exact hit -> skip the API, $0.00.** Optional semantic-similar tier
  composes `semantic_index` (env-tunable cosine threshold, NEVER a
  hardcoded constant; default conservative; advisory).
- Persistence: `cross_process_jsonl` or a bounded file under `.jarvis/`
  (compose the existing pattern; survives restart -> "persistent local").
- **fail-open**: any miss / IO error / HMAC fault -> normal `generate`
  path. The cache NEVER blocks an op and NEVER raises into the provider.
- **Authority asymmetry**: substrate imports stdlib +
  `prompt_cache`/`semantic_index`/`cross_process_jsonl` ONLY — never
  `orchestrator`/`iron_gate`/`candidate_generator`.
- Master `JARVIS_PROVIDER_RESPONSE_CACHE_ENABLED` default **FALSE**.
- AST pins: composes-not-duplicates `prompt_cache`; NEVER-raises; byte-budget
  LRU present; no hardcoded cosine; authority-asymmetric.
- ~25 spine tests: exact-hit->$0 / miss->passthrough / TTL / byte-evict
  under M1 bound / repo-state-change invalidates / semantic tier /
  fail-open (IO+HMAC) / persistence roundtrip / master-off no-op.

### S1 acceptance
Exact-repeat op returns the cached trajectory with **zero provider tokens
billed**; any staged/HEAD change forces a real call; cache memory stays
within `JARVIS_PROVIDER_CACHE_MAX_BYTES`; master-off is byte-identical to
today; 25 spine green; AST pins green.

## 5. S2 — Predictive Budget Preemption (design only; no impl until "implement S2")

Extend `admission_gate` with a `Forecasted_Cost` term: compose
`admission_estimator.WaitTimeEstimator` EWMA + the predictive-resilience
TTFT forecaster + a token->USD model. Before dispatch, if
`current_spend + forecasted_cost` approaches `session_budget` **or** a
higher-priority op is queued (compose UnifiedIntakeRouter priority queue +
`urgency_router`), **drive the existing `sensor_governor` quarantine** to
dynamically starve low-priority sensors. No new router; no hard throttle.
Master default FALSE. Acceptance: under tight budget, low-prio sensors
quarantine while a high-urgency op still admits; over-budget never;
governor composed (AST-pinned), not duplicated.

## 6. S3 — Monotonic-resilient WallClockWatchdog (design only)

NOT `s/time.time/monotonic/` (Darwin `CLOCK_MONOTONIC` pauses during
suspend — that IS the desired soak behavior). Make the **budget/liveness
deadline monotonic-authoritative** (host sleep cleanly *pauses* the
budget) with a **separate absolute wall ceiling** as a runaway backstop;
classify skew: monotonic-stalled-while-wall-jumps = benign host-sleep
(resume) vs monotonic-advancing-past-budget = real runaway (kill).
Behavior-flag default preserves today's safety; graduation flips
monotonic-authoritative. Independent of S1/S2 (may land in parallel).

## 7. Graduation contract (Phase-9 cadence, PRD evidence row)

Each slice graduates default-FALSE -> default-TRUE only with a recorded
evidence row (operator-referenced "§41.6" Phase-9 cadence; cross-linked to
the main PRD graduation register at graduation time):

| Slice | Graduation evidence required |
|---|---|
| S1 | A real op repeated with identical repo-state returns cached trajectory at **$0.00** (provider-call count delta = 0) across a soak; cache RSS within byte budget; zero stale-fix incidents (repo-change-invalidation proven) |
| S2 | A soak where budget pressure quarantines low-prio sensors while every high-urgency op still admits; spend stays under cap without a hard kill |
| S3 | A soak with a simulated host-sleep produces NO premature kill; a true runaway IS killed; forward-NTP-jump still safe |

Soak never auto-graduates a flag — flipping default-TRUE is a separate
operator-authorized PR.

## 8. NON-goals (explicit; reject scope creep)

- No new cache / router / budget / forecast substrate (extend only).
- No bespoke Phase-1 isolation script; no hard sensor throttle.
- No OCA / git-index / sovereignty / cursor-agent-ban changes (CLOSED).
- No Phase-1 re-run or any provider spend without explicit operator
  approval. No capability/euphoria claims.
- S2/S3 are design-only here; no S2 code until S1 graduates or operator
  says "implement S2".

## 9. Open questions (operator decision)

1. **Semantic tier in S1 v1, or exact-hash only first?** Exact-only is
   strictly correct and simplest; the `semantic_index` similar-tier adds
   reuse but needs a conservative env threshold + a correctness story.
2. **Byte budget default** for `JARVIS_PROVIDER_CACHE_MAX_BYTES` on a
   16GB M1 — propose 256MB (in-mem) + a larger on-disk persistence tier?
3. **Repo-state digest scope**: HEAD + tracked dirty only, or also
   untracked? (untracked rarely affects a fix but widens invalidation).
4. **Persistence medium**: `cross_process_jsonl` append-log (replayed to
   an in-mem ring on boot) vs a single bounded JSON file — both compose
   existing patterns; which do you want as v1?
5. **S3 sequencing**: land S3 in parallel with S1 (independent), or strict
   S1 -> S2 -> S3?
