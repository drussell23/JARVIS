# Cross-Repository Contract

This document defines the contract between JARVIS, JARVIS-Prime, and Reactor-Core.

## Environment Variables

All three repositories MUST respect these environment variables:

| Variable | Purpose | Default |
|----------|---------|---------|
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
|----------|---------|---------|
| `JARVIS_VERIFICATION_TIMEOUT` | 60.0 | Seconds to wait for service verification |
| `JARVIS_UNHEALTHY_THRESHOLD_FAILURES` | 3 | Consecutive failures before unhealthy |
| `JARVIS_UNHEALTHY_THRESHOLD_SECONDS` | 30.0 | Seconds unhealthy before revocation |
| `JARVIS_REVOCATION_COOLDOWN_SECONDS` | 5.0 | Seconds between revocation events |

## Related Files

- `backend/core/readiness_config.py` - Unified configuration
- `backend/core/readiness_predicate.py` - Readiness evaluation logic
- `backend/core/trinity_integrator.py` - `IntelligentRepoDiscovery` class
- `unified_supervisor.py` - Main supervisor with readiness management
