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

## 8b. S1 wiring plan (PR `ouroboros/zero-waste-s1-wire`) — review-only

**Status:** plan only. Awaiting operator "wire S1 approved" before any
edit to `providers.py` / `doubleword_provider.py`. The S1 substrate
(`provider_response_cache.py` + `cached_or_generate`) is the ONE seam.
**No inline cache logic in providers** — they import + call the gate.

### 8b.1 ClaudeProvider.generate (`providers.py` ~6392)

**Audit (load-bearing facts):**
- Top of method runs the **PRD §26.6.2 Layer-2 cost-contract gate**
  (`assert_provider_route_compatible(...)`, raises
  `CostContractViolation`) and `self._daily_spend >= self._daily_budget`
  raises `claude_budget_exhausted` — these are correctness gates that
  MUST fire whether or not we serve from cache (a cached hit on a
  contract-violated route would silently bypass the cost contract).
- Prompt is finalized later via `_build_lean_codegen_prompt` /
  `_build_codegen_prompt` → `prompt_text` local.
- `generate` is a multi-iteration tool loop with **8+ `return
  GenerationResult(...)` sites** (incl. 6930/6933/6935/6938/7239/...).

**Insertion point:** **AFTER** the cost-contract gate + daily-budget
check + `prompt_text` assembly + model/route/repo_root resolved, and
**BEFORE** the first `await self._client.messages.create(...)`.
Justification: (a) correctness — cost-contract MUST run; (b)
key-faithfulness — keying on the actually-assembled prompt is the
spec; (c) avoids re-wrapping the entire method when the tool loop is
active (see policy below).

**Canonical key serialization:** `compute_cache_key(prompt_text,
self._model, route=getattr(context, "provider_route", ""),
repo_root=repo_root)`. **Excluded** from the key (deliberately): `op_id`
(volatile), `deadline` (per-op), `repair_context` (would force misses
on every repair iteration — even when the underlying repair attempt is
identical), MCP tools list (env-dependent; repo-digest already invalidates
on code change). The repo-digest is the correctness anchor for the rest.

**Tool-loop policy for v1 (recommend + argue):** **SKIP CACHE WHEN
TOOLS WILL ENGAGE.** v1 gate-enables only when `self._tools_enabled is
False`. Argument: a tool-loop trajectory includes side-effecting tool
results (`run_tests`/`bash` outputs whose determinism we cannot prove
even with stable repo state); re-serving a cached tool trajectory could
silently elide real tool calls. Repo-digest covers code-state but not
e.g. test-runner flakiness or `bash` non-determinism. v1 captures the
substantial no-tools case ($/op savings on direct-patch paths); a future
S1.x can add tool-loop-aware caching with deterministic-trajectory
verification. This is the operator-recommended policy.

**produce() thunk shape (D2 — final, supersedes earlier "promote
`_generate_raw`" wording):** Audit showed `_generate_raw` is a
**~1,036-line nested closure** (L6506–L7542) wrapping streaming /
multi-retry / prefill-fallback state with `nonlocal total_cost`
mutation and ~10 closure captures. Promoting it to a method is NOT
mechanical and there is no integration-test coverage for the
streaming/retry/prefill paths to catch a regression.

**D2 keeps `_generate_raw` exactly in place as a nested closure.** The
gate's `produce()` is itself a **nested closure** inside
`generate()` — `_no_tools_inner` — that closes over the existing
`_generate_raw` + the post-prompt locals (`start`, `total_cost`,
`tool_rounds`, `_token_usage`, `_first_token_ms`,
`_thinking_reason_out`, `_preloaded_files`) and calls
`_finalize_codegen_result(...)` (extracted method, ~45 lines).
Only two extracts as methods, both low-risk:

  * `async def _assemble_codegen_prompt(self, *, context, repo_root,
    repair_context) -> (prompt_text, mcp_tools, preloaded_files)` —
    contains MCP discovery + lean/full prompt-build (~30 lines).
  * `def _finalize_codegen_result(self, *, raw, context, repo_root,
    start, preloaded_files, token_usage, total_cost, tool_rounds,
    first_token_ms, thinking_reason, tool_records, venom_edits) ->
    GenerationResult` — the post-raw result parsing + token/cost
    finalize (~45 lines).

**Gate insertion (D2):** immediately before the tool-dispatch block
(before the `tool_records: tuple = ()` line), after `_generate_raw`
is defined. When eligible (cache enabled AND `not self._tools_enabled`
AND `self._tool_loop is None`): build `_no_tools_inner` closure,
`gr, _ = await cached_or_generate(prompt=prompt_text, model=...,
route=..., repo_root=..., produce=_no_tools_inner)`, **return `gr`
early** — skipping the tool-dispatch block and all provider API/tool
work. **Important precision (operator correction): cache HIT avoids
provider/API/tool-loop work — it does NOT avoid the Python
nested-function setup above the gate (the prompt-build, the
`_generate_raw` `def` evaluation, etc.) — those are cheap CPU; the
savings are in the network/provider/tool dispatch.**

When NOT eligible (tools_enabled OR cache disabled): the gate is
skipped; the existing tool-dispatch block runs unchanged.

**Hit cost/observability:** `cost_usd=0.0` is already set by
`reconstruct_generation_result` in the substrate; the cached
`provider_name` carries the `+cache` suffix → existing telemetry
(`session_archive`, `cost_governor`, the `cost_tracker.json` writer)
naturally records $0.00 for cache-served ops. **No new ledger** — the
GenerationResult IS the existing telemetry record.

### 8b.2 DoubleWordProvider.generate (`doubleword_provider.py` ~874)

**Audit:** `generate(...)` dispatches: `if not is_available: noop
GenerationResult`; `if self._realtime_enabled: return await
self._generate_realtime(...)`; else batch (`submit_batch +
poll_and_retrieve`). DW has its own tool loop via `self._tool_loop`;
`_generate_realtime` computes `_will_skip_tools = _complexity in
("trivial", "simple")` and `_tools_available = self._tool_loop is not
None and not _will_skip_tools`. DW exposes a clean **`prompt_override`**
parameter on all paths (RT + batch + submit_batch) — composable.

**Insertion point:** at the **top of `generate`, AFTER the
`is_available` check**, BEFORE the RT/batch dispatch. Compose the
canonical builder once at this level (mirror the DW RT logic:
`_should_use_lean_prompt` -> `_build_lean_codegen_prompt` /
`_build_codegen_prompt`) and pass the result as `prompt_override` to
the dispatchers. This avoids prompt-build duplication (the inner
methods honor `prompt_override`) and gives the gate the actual prompt
at one seam. Justification: DW already has the `prompt_override` plumbing
— this composes it, doesn't introduce a new path.

**Canonical key:** `compute_cache_key(prompt, model=self._effective_model_id(context),
route=getattr(context,"provider_route",""), repo_root=self._repo_root)`.
Excludes the same volatile fields as Claude.

**Tool-loop policy for v1:** same as Claude — **skip cache when DW
tool-loop will engage.** Predicate at the top of `generate`: enable the
gate only when `self._tool_loop is None` OR `_complexity in
("trivial","simple")` (the same `_will_skip_tools` predicate the RT
path uses; we compute it once at gate time using the existing helper).
Same correctness argument.

**produce() thunk shape:** the thunk wraps the existing dispatch:
```
async def _dw_inner():
    if self._realtime_enabled:
        return await self._generate_realtime(
            context, deadline, prompt_override=prompt)
    pending = await self.submit_batch(context, prompt_override=prompt)
    ...
    return result
```
Then `gr, _ = await cached_or_generate(prompt=prompt, model=...,
route=..., repo_root=self._repo_root, produce=_dw_inner); return gr`.
The RT/batch internals are **unchanged**.

**Hit cost/observability:** same as Claude — `cost_usd=0.0` +
`provider_name="doubleword+cache"`; existing DW cost telemetry records
the $0 naturally.

### 8b.3 Correctness invariants (MUST hold; spine-pinned)

1. **master OFF → byte-identical**: when
   `JARVIS_PROVIDER_RESPONSE_CACHE_ENABLED` is unset/false,
   `cached_or_generate` returns `(produce(), DISABLED)` — provider path
   is the original code path verbatim. Tested by mocking the gate
   substrate untouched + asserting `produce` runs every call.
2. **repo digest change → miss (no stale patch)**: key includes
   `HEAD+SHA(git diff HEAD)`; any staged/HEAD mutation re-keys → MISS or
   `INVALIDATED_REPO_CHANGE`; provider runs. Already covered by S1
   substrate tests; the wiring test reasserts at the provider seam.
3. **fail-open on cache fault → normal generate**: any cache exception
   (compute_key fault / IO / persistence fault) returns
   `FAULT_FAIL_OPEN` → `produce()` runs. Spine-pinned.
4. **is_noop results not stored**: already in substrate; wiring test
   reasserts by mocking a noop result and confirming the next call still
   misses.
5. **Cost contract still fires**: the contract gate at the top of
   `ClaudeProvider.generate` MUST run on every call, including cache
   hits — the cache insertion is AFTER it (invariant by construction).
6. **Tool-loop never replayed in v1**: when the tool-engage predicate is
   true, the gate is skipped (no lookup, no store).
7. **AST pin (wiring PR)**: each provider imports `cached_or_generate`
   from `provider_response_cache` only; no inline cache class/store; no
   `OrderedDict` LRU in `providers.py`/`doubleword_provider.py`.

### 8b.4 Test plan (~8–12 wiring-PR integration tests)

`tests/governance/test_provider_response_cache_wiring.py` (NEW). Uses
the substrate's `cached_or_generate`; mocks `ClaudeProvider._client` /
`DoubleWordProvider._generate_realtime` so **no real provider call**.
With `JARVIS_PROVIDER_RESPONSE_CACHE_ENABLED=true` in-test only:

1. master-OFF default → both providers' generate behavior byte-identical
   (mock-produce called every time, no lookup).
2. master-ON, no-tools, identical repeat call → **2nd call: mock-produce
   NOT called**; result `cost_usd==0.0`; `provider_name` ends `+cache`.
3. master-ON, tools_enabled (Claude) → cache SKIPPED (mock-produce called
   both times) — tool-loop exclusion proven.
4. master-ON, DW `_tool_loop is not None` and complexity!=trivial →
   cache SKIPPED.
5. master-ON, repo-digest changed (monkeypatch `repo_state_digest`) →
   MISS, produce called.
6. master-ON, cache fault (monkeypatch `compute_cache_key` to raise) →
   `FAULT_FAIL_OPEN`, produce runs, no exception escapes.
7. master-ON, is_noop result → next identical call still misses.
8. Cost-contract violation on Claude (mock `assert_provider_route_compatible`
   to raise) → contract raises BEFORE any cache lookup (gate placement
   correct).
9. AST pin: `providers.py` + `doubleword_provider.py` import
   `cached_or_generate` only; no `class .*Cache\b` definitions; no
   `OrderedDict`-LRU literals.
10. (DW) `prompt_override` propagation: when the cache misses,
    `_generate_realtime` / `submit_batch` receive the canonical prompt
    built at the gate level.
11. (Claude) `_no_tools_inner` closes over the right locals — a happy-
    path no-tools call returns a sane GenerationResult identical to the
    pre-wiring baseline (master OFF) for the same inputs.

### 8b.5 Graduation path (no auto-flip)

1. Wiring PR merges with **master still FALSE** (dormant; unchanged
   behavior).
2. Operator runs a controlled soak with master TRUE + `--cost-cap` +
   the SWE-Bench-Pro Phase-1 fixture (or equivalent low-spend rep-rate
   workload), comparing `$/op` and `provider_call_count` vs the dormant
   baseline.
3. Evidence row appended to the PRD graduation section + the operator-
   referenced §41.6 cadence (cross-linked in the main PRD doc at flip
   time). **No capability claims**; methodology only.
4. Default-TRUE flip is a **separate operator-authorized PR** —
   reviewing the evidence row, not auto-graduated by the soak.

### 8b.6 Explicit NON-goals (this wiring PR)

- No semantic-similar tier (SEMANTIC_HIT stays reserved; S1.x).
- No tool-loop caching (v1 skips cache when tool loop will engage).
- No S2 (predictive budget) / S3 (monotonic watchdog) wiring.
- No master default-TRUE; no auto-graduation.
- No edit to OCA / git-index / sovereignty / cursor-agent-ban (CLOSED).
- No SWE-Bench-Pro Phase-1 re-run or any provider spend within this PR.

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
