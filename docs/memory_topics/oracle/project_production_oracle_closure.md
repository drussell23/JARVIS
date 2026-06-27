---
title: Production Oracle Integration — CLOSED 2026-05-03
modules: [backend/core/ouroboros/governance/production_oracle.py, backend/core/ouroboros/governance/stdlib_self_health_oracle.py, backend/core/ouroboros/governance/http_healthcheck_oracle.py, backend/core/ouroboros/governance/production_oracle_observer.py, backend/core/ouroboros/governance/ide_observability_stream.py, backend/core/ouroboros/governance/ide_observability.py, scripts/production_oracle_closure_verdict.py]
status: merged
source: project_production_oracle_closure.md
---

# Production Oracle Integration — CLOSED 2026-05-03

4-slice arc closing the Tier 2 #6 strategic gap from the user's roadmap table. Pre-arc state: zero Sentry/Datadog/Prometheus/healthcheck-style external signal feeds in the governance substrate. The only truth signal feeding VERIFY was pytest. Production reality (real-world health, error rates, latency drift, dependency status) was **invisible**.

## Slices shipped

- **Slice A** — `production_oracle.py` substrate primitive. `OracleSignal` frozen dataclass + closed-5 `OracleVerdict` (HEALTHY/DEGRADED/FAILED/INSUFFICIENT_DATA/DISABLED) + closed-5 `OracleKind` (HEALTHCHECK/ERROR/METRIC/DEPLOY_EVENT/PERFORMANCE) + `ProductionOracleProtocol` (`@runtime_checkable`, async query_signals + sync name/enabled properties) + `compute_aggregate_verdict()` pure function (env-tunable severity thresholds) + `project_signal_for_observability()` lightweight projection. AST pin enforces closed-5 taxonomy (catches silent enum drift across versions), frozen OracleSignal (consumers depend on hash-stable identity), and no exec/eval/compile.
- **Slice B** — `stdlib_self_health_oracle.py` offline empirical anchor. `StdlibSelfHealthOracle` reads recent N (default 10) battle-test session summaries from `.ouroboros/sessions/<id>/summary.json`; emits 3 grounded signals per query: HEALTHCHECK (completion ratio with healthy/degraded/failed thresholds), PERFORMANCE (mean cost per session relative to env-tunable baseline), METRIC (stop_reason distribution — clean-terminations vs SIGKILL/SIGTERM/sighup/sigint). Pure stdlib (filesystem reads only); zero network. NEVER raises -- contract violations return DISABLED signals so the aggregator handles them cleanly.
- **Slice C** — `http_healthcheck_oracle.py` generic network adapter. `HTTPHealthCheckOracle` proves the Protocol supports external services. Mirrors the existing `boot_handshake._fetch_urllib` pattern (urllib.request + run_in_executor; `urllib.error.URLError` + `OSError` -> structured error payload). 5-tier status code classification (2xx-expected → HEALTHY; 2xx-unexpected/3xx → DEGRADED low-sev; 4xx → FAILED high-sev; 5xx → FAILED higher-sev; network/timeout → FAILED highest-sev). Env-configurable URL + timeout + expected-status-set; reports DISABLED when no URL configured (safe to register unconditionally).
- **Slice D** — `production_oracle_observer.py` async observer + IDE observability bridge. Periodic loop with posture-aware cadence (HARDEN: 60s, MAINTAIN: 300s, EXPLORE/CONSOLIDATE: 180s; all env-tunable per-posture). Composes registered adapters (default bundle: StdlibSelfHealthOracle + HTTPHealthCheckOracle) into a single tick that aggregates signals → OracleVerdict, stores in bounded ring buffer (default 64 entries; env-tunable), publishes SSE event `production_oracle_signal_observed`, surfaces via `GET /observability/production-oracle`. Adapter exceptions are fault-isolated -- one broken adapter never breaks the tick. Master flag `JARVIS_PRODUCTION_ORACLE_ENABLED` graduated default-true; 4 FlagRegistry seeds (master + history size + fail/degrade thresholds); empirical-closure verdict script.

## Architectural decisions worth remembering

- **Pure-stdlib substrate, network adapters in siblings**. The substrate (`production_oracle.py`) is offline-validatable -- only stdlib + dataclasses + enum. Network adapters live in their own files (`http_healthcheck_oracle.py`); future Sentry/Datadog/Prometheus adapters are siblings with vendor-specific HTTP/auth/parsing. Avoids the "do-everything HTTP oracle becomes a god object" anti-pattern.
- **Empirical-anchor adapter is mandatory for substrate validation in sandbox**. Without `StdlibSelfHealthOracle`, the substrate would only be testable via mocks (no real Sentry tokens in this env). The offline anchor reads existing battle-test session summaries -- artifacts the system already produces for free -- and turns them into grounded OracleSignals. Future sandboxed envs can validate the substrate the same way.
- **Sentry/Datadog/Prometheus adapters DEFERRED, not skipped**. The Protocol shape is proven via two concrete implementers (offline + network-failure paths). Vendor-specific adapters drop in as additional Protocol implementers with no substrate changes -- empirically validated when real tokens are available.
- **Posture-aware cadence reuses StrategicPosture enum**. Mirrors `posture_observer` pattern: HARDEN (active soak / regression hunting) → fast 60s; MAINTAIN (steady state) → slow 300s; EXPLORE/CONSOLIDATE → 180s middle. All knobs env-tunable per-posture so operators can tune without code changes.
- **Lazy lock construction**. Python 3.9 `asyncio.Lock()` at module load time fails with "no current event loop in thread" when imported before any `asyncio.run()`. Fixed via `_ensure_lock()` lazy initializer inside async methods. Caught by the verdict script -- the smoke test had previously masked this because it called `asyncio.run()` first.
- **Adapter exceptions fault-isolated at tick level**. The observer wraps each adapter call in try/except; one broken adapter increments `failure_count` but doesn't break the tick. Verified by the verdict's C4 contract which registers a synthetic `_BrokenAdapter` and confirms `adapters_failed=1` while other adapters' signals still flow through.
- **HTTPError specifically caught BEFORE URLError**. `urllib.error.HTTPError` IS a `Response` -- it carries a status code we can use even when the response is non-2xx. Bare `except URLError` would mask this. The classification logic uses the status code from HTTPError to produce a meaningful FAILED signal instead of a generic network-error one.

## Test counts + AST pins

- **Empirical verdict 6/6 PRIMARY PASS** (no test suite ships in this initial arc — verdict script is the regression spine; tests are a follow-up arc):
  - C1 Substrate primitive correct (closed-5 enums + aggregator deterministic)
  - C2 StdlibSelfHealthOracle reads real sessions (3 signals across 3 kinds against `.ouroboros/sessions/`)
  - C3 HTTPHealthCheckOracle handles disabled + network-fail (graceful DISABLED + FAILED with structured error)
  - C4 Observer composes adapters + isolates failures (synthetic broken adapter increments failure_count without breaking tick)
  - C5 register_flags + 4 AST pins land cleanly (4 flags + 4 pins all hold against live source)
  - C6 Master default-true + SSE + publisher importable (graduation surface live)
- **4 new AST pins**: `production_oracle_substrate`, `stdlib_self_health_oracle_substrate`, `http_healthcheck_oracle_substrate`, `production_oracle_observer_substrate`
- **4 new FlagRegistry seeds** (registered via observer's `register_flags`): JARVIS_PRODUCTION_ORACLE_{ENABLED,HISTORY_SIZE,FAIL_THRESHOLD,DEGRADE_THRESHOLD}
- **1 new SSE event**: `production_oracle_signal_observed` + `publish_production_oracle_signal()` helper
- **1 new GET route**: `/observability/production-oracle` returning current observation + history ring + config

## Empirical-closure verdict (against live repo)

```
[PASS] C1 Substrate primitive correct
       verdicts=5 kinds=5 empty->insufficient_data
       healthy->healthy failed->failed
[PASS] C2 StdlibSelfHealthOracle reads real sessions
       signals=3 kinds=[healthcheck,metric,performance]
       first_summary='completion ratio 37.50% (3/8 sessions clean-terminated)'
[PASS] C3 HTTPHealthCheckOracle handles disabled + network-fail
       no_url->verdict=disabled unroutable->verdict=failed sev=0.85
[PASS] C4 Observer composes adapters + isolates failures
       adapters_queried=3 adapters_failed=1 signals=3 verdict=failed history_size=1
[PASS] C5 register_flags + 4 AST pins land cleanly
       flags=4 pins=4
[PASS] C6 Master default-true + SSE + publisher importable
       master_default=True sse_event='production_oracle_signal_observed'
```

The C2 evidence is particularly meaningful: the StdlibSelfHealthOracle correctly identified that the harness has a real completion-rate problem (3/8 = 37.5% clean-terminated → FAILED healthcheck verdict). This is exactly the kind of advisory signal the substrate is meant to surface to operators -- and it's grounded in real existing artifacts, not synthetic test fixtures.

## Reuse contract honored (no duplication)

- Existing `urllib.request` + `run_in_executor` pattern from `boot_handshake._fetch_urllib` reused by HTTPHealthCheckOracle (no new HTTP client dependency)
- Existing `ShippedCodeInvariant` registration contract reused; 4 new pins added across modules
- Existing `FlagSpec` + `Category` + `FlagType` reused for the 4 new flags
- Existing SSE publish helper pattern from `publish_domain_map_update` / `publish_goal_inference_built` mirrored for `publish_production_oracle_signal`
- Existing GET handler pattern from `_handle_codebase_character` / `_handle_goal_inference` mirrored for `_handle_production_oracle`
- Existing posture vocabulary (HARDEN / MAINTAIN / EXPLORE / CONSOLIDATE) reused for cadence map
- `StdlibSelfHealthOracle` reuses existing `.ouroboros/sessions/<id>/summary.json` artifacts -- no new persistence layer

## Reverse Russian Doll posture preserved

Outer doll (the substrate) gains an entire new sense organ for production reality. Antivenom scaled proportionally: 4 AST pins lock the substrate against silent drift (closed-5 taxonomy + frozen dataclass + required functions/classes/constants); 4 FlagRegistry seeds make every env knob `/help flags` discoverable + typo-detectable; authority-free by construction (advisory verdicts only — never directly mutate Iron Gate/risk/route/approval); fault-isolated at adapter level (one broken adapter doesn't break the tick).

## What this unlocks

The user's table flagged this gap as: "Without this, pytest passing is the only truth signal. Production reality is invisible." Pre-arc, the system had no way to ingest external signals about whether changes were actually working in the real world. Post-arc:

1. **Substrate exists** -- the Protocol + aggregator + observer compose any number of oracle adapters into a single advisory verdict stream.
2. **Two adapters ship** -- offline self-health + generic HTTP healthcheck. The architecture is proven for both file-based and network-based oracles.
3. **Observability live** -- SSE events + GET route + ring buffer give operators real-time visibility into the verdict stream.
4. **Future adapters drop in** -- Sentry, Datadog, Prometheus, GitHub Checks, etc. are sibling implementations of `ProductionOracleProtocol` with no substrate changes required.
5. **VERIFY consumer is downstream** -- the next arc (deferred) is wiring OracleVerdict into the existing `auto_action_router` advisory framework so verdict=FAILED/DEGRADED can propose AdvisoryActionType (DEFER_OP_FAMILY / DEMOTE_RISK_TIER / NOTIFY_APPLY) without touching the cost-contract substrate.

## Files touched

- `backend/core/ouroboros/governance/production_oracle.py` (NEW substrate)
- `backend/core/ouroboros/governance/stdlib_self_health_oracle.py` (NEW offline anchor)
- `backend/core/ouroboros/governance/http_healthcheck_oracle.py` (NEW network adapter)
- `backend/core/ouroboros/governance/production_oracle_observer.py` (NEW observer + IDE bridge)
- `backend/core/ouroboros/governance/ide_observability_stream.py` (EVENT_TYPE_PRODUCTION_ORACLE_SIGNAL + publish_production_oracle_signal)
- `backend/core/ouroboros/governance/ide_observability.py` (route registration + _handle_production_oracle)
- `scripts/production_oracle_closure_verdict.py` (NEW)

Closes Tier 2 #6 of the user's roadmap with the structural-then-empirical pattern proven on the prior arcs (cluster_intelligence, mission_inferrer, multi_repo, pass_b_graduation). Sentry/Datadog/Prometheus adapters and the auto_action_router VERIFY-consumer wire-up are deferred to follow-up arcs; the substrate proves the architecture supports them.
