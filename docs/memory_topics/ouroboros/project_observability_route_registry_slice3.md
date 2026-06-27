---
title: Project Observability Route Registry Slice3
modules: [backend/core/ouroboros/governance/observability_route_registry.py, tests/governance/test_observability_route_registry.py, backend/core/ouroboros/governance/event_channel.py, backend/core/ouroboros/battle_test/repl_dispatch_registry.py, backend/core/ouroboros/battle_test/serpent_flow.py]
status: historical
source: project_observability_route_registry_slice3.md
---

**Status (2026-05-04)**: Slice 3 CLOSED. 24 new tests + 6 new pins green; 390/390 across full sweep.

## What landed

`backend/core/ouroboros/governance/observability_route_registry.py` (~395 LOC, pure substrate composing Slice 2 module_discovery primitive):

- `discover_and_mount_observability_routes(app, *, rate_limit_check, cors_headers, packages, excluded_modules) -> MountReport` — single boot call mounts every module-level `register_routes(app, **kwargs)` across curated provider packages
- Frozen `MountReport` (mounted_count + already_mounted + signature_rejected + handler_failed + mounted tuple + skipped_reasons + elapsed_s + master_flag_on + schema_version) with `as_dict()` projection
- Frozen `MountedRoute(module_full_name, mounted_at_unix)` for telemetry
- Master flag `JARVIS_OBSERVABILITY_AUTODISCOVERY_ENABLED` default-true with asymmetric env semantics
- Default provider packages: governance + governance.m10 + governance.verification + governance.observability
- Substrate exclusions list (recursion guard + class-based routers like IDEObservabilityRouter that require constructor dependencies)

## 5 dormant surfaces auto-mounted

Single boot call now wires:
1. `backend.core.ouroboros.governance.decisions_observability` → `/observability/decisions[/session/{session_id}]`
2. `backend.core.ouroboros.governance.curiosity_observability` → `/observability/curiosity[/region/{cluster_id}]`
3. `backend.core.ouroboros.governance.epistemic_budget_observability` → `/observability/budget[/{op_id}]`
4. `backend.core.ouroboros.governance.m10.observability` → `/observability/m10[/proposal/{proposal_id}]`
5. `backend.core.ouroboros.governance.action_outcome_memory_observability` → `/observability/action-outcomes[/cluster/{id}]`

The last one was renamed `register_action_outcome_routes` → `register_routes` for naming-convention uniformity; alias retained for backward-compat with existing event_channel.py call site (which still works during transition).

## event_channel.py wiring

Added single block after the legacy explicit register_routes blocks. Master-flag-gated. Idempotency at module-name granularity ensures no double-mount; legacy explicit blocks short-circuit on already_mounted (rolls into the registry naturally). Logs structured count: `auto-mounted N (skipped: M already-mounted, K signature-rejected, J handler-failed)`.

## 6 new AST pins (cleanup_invariants now 13 pins)

Slice 3 added:
1-5. `observability_module_exposes_register_routes_{decisions,curiosity,epistemic_budget,action_outcome,m10}` — per-module pins enforcing the canonical naming convention
6. `observability_route_registry_uses_primitive` — registry MUST compose Slice 2 (no parallel walker; forbid `pkgutil.iter_modules`)

## Architectural significance

The Slice 5b debt class is now closed by **construction**:

- Past pattern: each Slice 5 arc shipped substrate + observability module, then Slice 5b deferred wiring → 5 surfaces accumulated as dormant
- Slice 3 pattern: ship a `*_observability.py` file with `register_routes(app, **kwargs)` and it auto-mounts at next boot. Zero edits to `event_channel.py` per arc
- Future Phase 10 / Phase 9 / etc. surfaces inherit this — auto-mount is the default; manual wiring is the exception (only for class-based routers with constructor deps)

## Test spine

`tests/governance/test_observability_route_registry.py` — 24 tests covering: master flag asymmetric semantics / 5 dormant surfaces auto-mount / actual aiohttp routes appear / idempotency / list_mounted_modules / signature rejection (synthetic package) / handler failure isolation (synthetic package) / substrate exclusions / canonical name + alias for action_outcome / registry-composes-primitive (inspect-source) / MountReport schema + projection / authority asymmetry / public API exports.

## What's next

- **Slice 4** — `repl_dispatch_registry.py` for SerpentREPL command auto-discovery (~5h, ~25 tests). Replaces if/elif ladder in serpent_flow.py with auto-discovered registry; unlocks 5 new REPL verbs (m10, decisions, curiosity, budget, action_outcome).
- **Slice 5** — Graduation + ~125 total regression tests + PRD v2.20 → v2.21
