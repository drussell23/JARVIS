---
title: Sentry + Datadog Stub-Server Integration Tests — CLOSED 2026-05-03
modules: [scripts/stub_oracle_servers.py]
status: historical
source: project_vendor_oracle_stub_integration_closure.md
---

# Sentry + Datadog Stub-Server Integration Tests — CLOSED 2026-05-03

Single-slice arc shipping `scripts/stub_oracle_servers.py` — local stdlib `http.server` instances that mimic the Sentry Issues API + Datadog Monitor API endpoints with controlled responses. Solves the "we have vendor adapters but no real tokens to test against" problem by staging the vendor side locally; exercises the real `urllib.request` code path without ever reaching real vendor endpoints.

## What ships

- **`SentryStubServer`** context manager: spins up a stdlib HTTP server on a dynamically-allocated localhost port mimicking `/api/0/projects/.../issues/` + `/api/0/organizations/.../issues/`. Validates `Authorization: Bearer <token>` when an expected token is configured (returns 401 on mismatch).
- **`DatadogStubServer`** context manager: same shape; mimics `/api/v1/monitor`. Validates dual-key auth (`DD-API-KEY` + `DD-APPLICATION-KEY`) — returns 401 on api-key mismatch, 403 on app-key mismatch (matching real Datadog API behavior).
- **Integration test harness** (the script's `main()`): 10 end-to-end tests exercising `SentryOracle` + `DatadogOracle` against the stubs:
  - Sentry: 0 issues → HEALTHY, 2 → DEGRADED (sev 0.34), 15 → FAILED (sev 0.85), 60 → FAILED (sev 0.95)
  - Sentry: wrong token → FAILED (HTTP 401, sev 0.90)
  - Datadog: 2 OK → HEALTHY, 1 Warn + 2 OK → DEGRADED, 1 Alert + 1 OK → FAILED, 1 No Data + 1 OK → DEGRADED
  - Datadog: wrong api key → FAILED (HTTP 401, sev 0.90)

## Architectural decisions worth remembering

- **Sandbox-disable required for `socket.bind()`**. The Claude Code sandbox blocks listen-socket binding by default; this script needs loopback bind to spawn the stubs. Documented as TEST INFRASTRUCTURE in the file docstring; never imported by production code; never reaches real vendor endpoints. Operators can manage this via `/sandbox`.
- **Stub validates the wire protocol, not just the parsing**. The harness goes through the full `urllib.request.Request → urlopen → response.read → json.loads` chain. Catches:
  - Header construction bugs (Bearer prefix, dual-key naming)
  - URL-builder bugs (project vs org path routing, query string encoding)
  - HTTP status classification bugs (401/403/500 mapping)
  - Empty-response edge cases (Sentry with no issues; Datadog with no monitors)
- **Per-test env hot-swap**. Each test sets `SENTRY_AUTH_TOKEN` / `JARVIS_SENTRY_API_BASE` / `DD_API_KEY` / `DD_APP_KEY` / `JARVIS_DATADOG_API_BASE` to point the adapter at the stub, then restores prior env on exit. No leak between tests.
- **Pure stdlib stubs** — no Flask, no FastAPI, no aiohttp. `http.server.HTTPServer` + threading. Matches the "no new deps" discipline of the rest of the Production Oracle family.

## Test counts

- **10/10 integration tests PASS** (4 Sentry happy paths + Sentry auth failure + 4 Datadog happy paths + Datadog auth failure)
- **No new AST pins** — script is test infrastructure
- **No new FlagRegistry seeds** — uses existing vendor adapter env knobs

## What this unlocks

- Sentry + Datadog adapters are now empirically validated end-to-end against the real urllib code path without requiring vendor tokens. CI can run this script (with sandbox-disable for the bind) as part of the pre-merge gate to catch URL/auth/parsing regressions.
- Future vendor adapters (Prometheus, GitHub Checks, PagerDuty, OpsGenie) can be added to the same harness — each gets a 4-line stub class + 4-test happy/failure path block.
- Operators staging real vendor tokens get a known-good control: if real-token tests fail and stub-token tests pass, the bug is in the vendor account config, not the adapter.

## Files touched

- `scripts/stub_oracle_servers.py` (NEW — 16.6 KB; 2 stub server classes + 10-test harness)

Closes the empirical-validation gap on Sentry + Datadog adapters that Arc 2 deferred ("real tokens not available in this env").
