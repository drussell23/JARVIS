# [Ouroboros] Modified by Ouroboros (op=op-019d8535-) at 2026-04-13 05:01 UTC
# Reason: IntentEnvelope schema version 2c.1 not found in cross-repo contract doc. Contract may be outdated.

# Cross-Repository Contract

This document defines the contract between JARVIS, JARVIS-Prime, and Reactor-Core.

## Environment Variables

All three repositories MUST respect these environment variables:

| Variable | Purpose | Default |
|----------|---------|----------|
| `JARVIS_PRIME_PATH` | Path to JARVIS-Prime repository | Auto-discovered |
| `REACTOR_CORE_PATH` | Path to Reactor-Core repository | Auto-discovered |
| `JARVIS_PRIME_PORT` | Port for JARVIS-Prime | 8000 |
| `REACTOR_CORE_PORT` | Port for Reactor-Core | 8090 |

## Path Discovery

JARVIS uses `IntelligentRepoDiscovery` to find repositories:

1. **Environment variable** (highest priority): `JARVIS_PRIME_PATH`, `REACTOR_CORE_PATH`
2. **Sibling directory**: `../jarvis-prime`, `../reactor-core`
3. **Standard locations**: `~/Documents/repos/`, `~/repos/`
4. **Git-based search**: Find by .git presence

## Health Contract

Each repository MUST expose:

- `GET /health` - Returns 200 when healthy
- Response includes: `{"status": "healthy", "version": "...", "uptime": ...}`

## Heartbeat Contract

Each repository SHOULD write heartbeat files to:

- `~/.jarvis/trinity/components/{component_name}.json`
- Updated every 10-30 seconds
- Contains: timestamp, status, version, pid

## IntentEnvelope Schema (version 2c.1)

All cross-repo intent messages MUST conform to this envelope schema.

### Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `schema_version` | string | Yes | Must be `"2c.1"` |
| `intent_id` | string (UUID) | Yes | Unique identifier for this intent |
| `op_id` | string | Yes | Operation ID linking related intents |
| `source` | string | Yes | Originating component (e.g. `"jarvis"`, `"jarvis-prime"`, `"reactor-core"`) |
| `target` | string | Yes | Destination component |
| `intent_type` | string | Yes | Semantic action label (e.g. `"execute_task"`, `"status_query"`) |
| `payload` | object | Yes | Intent-specific data (schema varies by `intent_type`) |
| `timestamp` | string (ISO-8601) | Yes | UTC creation time |
| `ttl_seconds` | number | No | Seconds until this intent expires; omit for no expiry |
| `reply_to` | string | No | Component that should receive the response |
| `trace_context` | object | No | Distributed tracing metadata (e.g. W3C traceparent) |

### Example

```json
{
  "schema_version": "2c.1",
  "intent_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "op_id": "op-019d8535-fb8e-7315-ab15-71820119e5c9",
  "source": "jarvis",
  "target": "reactor-core",
  "intent_type": "execute_task",
  "payload": {
    "task": "run_preflight",
    "args": {}
  },
  "timestamp": "2025-01-01T00:00:00Z",
  "ttl_seconds": 30
}
```

### Validation Rules

- `schema_version` MUST be exactly `"2c.1"`; receivers MUST reject unknown versions.
- `intent_id` MUST be a valid UUID v4.
- `timestamp` MUST be UTC ISO-8601 (e.g. `2025-01-01T00:00:00Z`).
- Receivers MUST discard intents whose `ttl_seconds` has elapsed relative to `timestamp`.
- Unknown top-level fields MUST be ignored (forward-compatibility).

## Status Semantics

| Status | Meaning |
|--------|---------|
| `healthy` | Running and passing health checks |
| `starting` | Process started, waiting for health |
| `degraded` | Running but some checks failing |
| `stopped` | Was running, intentionally stopped |
| `skipped` | Never started (not configured) |
| `unavailable` | Not available on this system |
| `error` | Fatal error occurred |

## Component Criticality

Components are classified as:

### Critical (must be healthy for FULLY_READY)
- `backend` - JARVIS backend API server
- `loading_server` - Static file server for loading page
- `preflight` - Startup preflight checks

### Optional (can be skipped/unavailable)
- `jarvis_prime` - JARVIS-Prime AI components
- `reactor_core` - Reactor-Core event processing
- `enterprise` - Enterprise features
- `agi_os` - AGI OS integration
- `gcp_vm` - GCP VM integration

## Readiness Tiers

| Tier | Meaning |
|------|---------|
| `INITIALIZING` | Kernel starting up |
| `HTTP_HEALTHY` | HTTP server accepting requests |
| `INTERACTIVE` | Can handle basic commands (degraded) |
| `FULLY_READY` | All critical components healthy |

## Display Codes

4-character status codes for CLI display:

| Status | Display |
|--------|---------|
| `pending` | `PEND` |
| `starting` | `STAR` |
| `healthy` | `HEAL` |
| `degraded` | `DEGR` |
| `error` | `EROR` |
| `stopped` | `STOP` |
| `skipped` | `SKIP` |
| `unavailable` | `UNAV` |

**CRITICAL**: `skipped` displays as `SKIP`, NOT `STOP`.

## Configuration

Readiness behavior can be configured via environment variables:

| Variable | Default | Purpose |
|----------|---------|----------|
| `JARVIS_VERIFICATION_TIMEOUT` | 60.0 | Seconds to wait for service verification |
| `JARVIS_UNHEALTHY_THRESHOLD_FAILURES` | 3 | Consecutive failures before unhealthy |
| `JARVIS_UNHEALTHY_THRESHOLD_SECONDS` | 30.0 | Seconds unhealthy before revocation |
| `JARVIS_REVOCATION_COOLDOWN_SECONDS` | 5.0 | Seconds between revocation events |

## Related Files

- `backend/core/readiness_config.py` - Unified configuration
- `backend/core/readiness_predicate.py` - Readiness evaluation logic
- `backend/core/trinity_integrator.py` - `IntelligentRepoDiscovery` class
- `unified_supervisor.py` - Main supervisor with readiness management
