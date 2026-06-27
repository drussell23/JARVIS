---
title: Sentry + Datadog Vendor Oracle Adapters — CLOSED 2026-05-03
modules: [scripts/vendor_oracle_adapters_closure_verdict.py, backend/core/ouroboros/governance/sentry_oracle.py, backend/core/ouroboros/governance/datadog_oracle.py, backend/core/ouroboros/governance/production_oracle_observer.py]
status: historical
source: project_vendor_oracle_adapters_closure.md
---

# Sentry + Datadog Vendor Oracle Adapters — CLOSED 2026-05-03

4-slice follow-up arc to the Production Oracle substrate (Tier 2 #6) shipping vendor-specific adapters that prove the architecture supports real external services. Both adapters use the same urllib.request pattern as the substrate's `HTTPHealthCheckOracle` + `boot_handshake._fetch_urllib`; vendor differences live in URL building, auth headers, and response parsing only.

## Slices shipped

- **Slice A** — `sentry_oracle.py`. `SentryOracle` class implementing `ProductionOracleProtocol` against the Sentry Issues API. Reads `SENTRY_AUTH_TOKEN` + `JARVIS_SENTRY_ORG` (required) + `JARVIS_SENTRY_PROJECT` (optional, scopes to project; org-wide otherwise) + `JARVIS_SENTRY_API_BASE` (defaults `https://sentry.io`; self-host operators set their own) + `JARVIS_SENTRY_STATS_PERIOD` (default `1h`) + `JARVIS_SENTRY_TIMEOUT_S` (default 10s, floor 1, ceiling 60). `_classify_count` produces ERROR-kind signals: 0 issues → HEALTHY/0.1; 1-9 → DEGRADED/0.3-0.6 (linear scale); 10-49 → FAILED/0.85; 50+ → FAILED/0.95 (burst). Auth failures (401/403) → FAILED/0.9 with detailed payload. Network errors → FAILED/0.85 with structured error. AST pin enforces all required functions/classes present.

- **Slice B** — `datadog_oracle.py`. `DatadogOracle` class implementing the same Protocol against the Datadog Monitor API. Reads `DD_API_KEY` + `DD_APP_KEY` (both required — Datadog uses dual-key auth) + `JARVIS_DATADOG_MONITOR_QUERY` (optional tag query; empty = all monitors) + `JARVIS_DATADOG_API_BASE` (defaults `api.datadoghq.com`; EU operators set `api.datadoghq.eu`) + `JARVIS_DATADOG_TIMEOUT_S` (default 10s). `_classify_states` produces METRIC-kind signals via decision precedence: empty → INSUFFICIENT_DATA; any "Alert" → FAILED/0.85; any "Warn" → DEGRADED/0.55; any "No Data" → DEGRADED/0.4; otherwise HEALTHY/0.1. Auth + network failure paths same shape as Sentry.

- **Slice C** — `production_oracle_observer.get_default_observer()` updated to register 4 adapters in the default bundle: StdlibSelfHealthOracle + HTTPHealthCheckOracle + SentryOracle + DatadogOracle. Safe to register all unconditionally — each adapter reports `enabled=False` when its env config is absent, so the observer's `tick_once` skips them via the `if not getattr(adapter, "enabled", True): continue` guard. Aggregator naturally handles a mix of enabled+disabled adapters (substrate already filters DISABLED-verdict signals from informative set).

- **Slice D** — `scripts/vendor_oracle_adapters_closure_verdict.py` covering 5 primary contracts; closure memory + MEMORY.md update.

## Architectural decisions worth remembering

- **No vendor SDKs / requests / aiohttp** — both adapters use stdlib `urllib.request` with `run_in_executor`, mirroring the established `boot_handshake._fetch_urllib` pattern. This keeps the dependency surface zero for vendor coverage.
- **Constructor args take precedence over env** for both adapters. Operators can construct `SentryOracle(token="...", org="...")` for tests/manual ops without polluting the environment. Default behavior reads env at every call so hot-swap of credentials works without reconstructing the adapter.
- **Sentry: project-scoped or org-wide via single env knob** — `JARVIS_SENTRY_PROJECT` empty = org-wide query; non-empty = project-scoped. Avoids two parallel adapter classes; one knob switches scope.
- **Datadog: dual-key auth required by API** — both `DD_API_KEY` AND `DD_APP_KEY` must be set. The adapter reports DISABLED specifically saying which key is missing (separate evidence in C2 verdict). Mirrors the actual Datadog API contract.
- **Severity scaling per vendor reflects domain semantics** — Sentry's 1-9 unresolved-issues range is "noisy but not crisis"; the linear severity scale (0.3 → 0.6) lets the aggregator differentiate "1 nuisance error" from "9 errors building up". Datadog states are categorical (Alert/Warn/OK/No Data) so the verdict map is discrete; severity is fixed per state. Different shapes for different vendor APIs.
- **Both adapters survive in the default bundle without secrets** — the verdict script explicitly proves the no-env path. Operators who never configure Sentry/Datadog see no oracle noise; operators who configure one or both immediately get coverage without code changes.

## Test counts + AST pins

- **Empirical verdict 5/5 PRIMARY PASS** (verdict script is the regression spine):
  - C1 SentryOracle DISABLED when env unset (no token / token-without-org both → DISABLED)
  - C2 DatadogOracle DISABLED when env unset (no keys / api-key-without-app-key both → DISABLED)
  - C3 Both adapters pass `isinstance(_, ProductionOracleProtocol)` (runtime_checkable)
  - C4 Default bundle registers 4 adapters; aggregator handles mix correctly
  - C5 All AST pins hold (6 total: substrate + 5 adapters)
- **2 new AST pins**: `sentry_oracle_substrate`, `datadog_oracle_substrate`
- **6 total AST pins** in the Production Oracle family (substrate + stdlib_self_health + http_healthcheck + observer + sentry + datadog)
- **No new FlagRegistry seeds in this arc** — the env knobs are listed in module bodies + closure memo; full seed roll-up is a follow-up arc (similar to how Pass B's per-module register_flags came after individual slice landings)

## Empirical-closure verdict (against live source)

```
[PASS] C1 SentryOracle DISABLED when env unset
       enabled_when_no_env=False no_token_verdict=disabled
       no_org_verdict=disabled
[PASS] C2 DatadogOracle DISABLED when env unset
       enabled_when_no_env=False no_keys_verdict=disabled
       no_app_key_verdict=disabled
[PASS] C3 Both adapters structurally implement Protocol
       SentryOracle_isinstance=True DatadogOracle_isinstance=True
[PASS] C4 Default bundle registers 4 adapters
       adapter_count=4 names=[datadog, http_healthcheck, sentry, stdlib_self_health]
       verdict=failed adapters_failed=0
[PASS] C5 All AST pins hold across substrate + adapters
       pins=6 (substrate + 5 adapters)
```

## Reuse contract honored (no duplication)

- Existing `urllib.request` + `run_in_executor` pattern reused from `boot_handshake._fetch_urllib` (no new HTTP client dep)
- Existing `OracleSignal` + `OracleVerdict` + `OracleKind` substrate reused (no new signal shapes)
- Existing `_disabled_signal` helper pattern from `http_healthcheck_oracle` mirrored
- Existing `_do_blocking_get` shape mirrored across both adapters (same return tuple convention; same error classification)
- Existing `register_shipped_invariants` AST pin pattern reused
- Existing `production_oracle_observer.get_default_observer()` factory updated additively (no new factory; just registers more adapters)

## What this unlocks

The Production Oracle Arc 1 (VERIFY wiring) closed the loop oracle → auto_action_router. This Arc 2 makes the loop ACTUALLY USEFUL for real production environments:

1. **Operators configure SENTRY_AUTH_TOKEN once** → every VERIFY phase gets advisory proposals for risk-tier-demote when production error rates spike.
2. **Operators configure DD_API_KEY once** → every VERIFY phase gets advisory proposals when Datadog monitors transition to Warn/Alert.
3. **Self-hosted Sentry / EU Datadog supported** via `JARVIS_SENTRY_API_BASE` / `JARVIS_DATADOG_API_BASE` env overrides — no hardcoded vendor URLs.
4. **Future vendor adapters drop in cleanly** — Prometheus, GitHub Checks, PagerDuty, OpsGenie are all the same Protocol implementation pattern. The substrate doesn't change; the default observer bundle adds the new adapter; existing rule 1.5 in auto_action_router immediately consumes them.

## Files touched

- `backend/core/ouroboros/governance/sentry_oracle.py` (NEW)
- `backend/core/ouroboros/governance/datadog_oracle.py` (NEW)
- `backend/core/ouroboros/governance/production_oracle_observer.py` (default bundle += 2 adapters)
- `scripts/vendor_oracle_adapters_closure_verdict.py` (NEW)

Closes Tier 2 #6 follow-up Arc 2. MetaPhaseRunner soak preparation lands in Arc 3.
