---
title: Project Module Discovery Slice2
modules: [backend/core/ouroboros/governance/meta/module_discovery.py, tests/governance/test_module_discovery.py, backend/core/ouroboros/governance/observability_route_registry.py, backend/core/ouroboros/governance/event_channel.py, backend/core/ouroboros/battle_test/repl_dispatch_registry.py, backend/core/ouroboros/battle_test/serpent_flow.py]
status: historical
source: project_module_discovery_slice2.md
---

**Status (2026-05-04)**: Slice 2 CLOSED. 32 new tests green; 366/366 across module_discovery + 9 dependent suites.

## What landed

`backend/core/ouroboros/governance/meta/module_discovery.py` (~370 LOC, pure stdlib + importlib + pkgutil only):

- `discover_module_provided_callable(*, packages, attr_name, handler, excluded_modules, log_prefix) -> DiscoveryReport`
- Frozen `DiscoveryReport` (8 fields: discovered_count, modules_scanned, submodules_seen, packages_unavailable, modules_skipped, elapsed_s, master_flag_on, schema_version) + `as_dict()` projection
- Frozen `SkippedModule(full_name, reason)` for telemetry
- `make_registry_handler(*, registry)` — for `fn(registry) -> int` consumers
- `make_factory_handler(*, register_one, iterable_validator=None)` — for `fn() -> Iterable[X]` consumers
- Master flag `JARVIS_MODULE_DISCOVERY_ENABLED` default-true; off → fast no-op zero-count report

## Architectural invariants (AST-pinned via 3 consumer pins)

1. Pure substrate — stdlib + importlib + pkgutil only; NEVER raises; per-module + per-package exception isolation structural; idempotent at substrate
2. Authority asymmetry — no orchestrator/iron_gate/policy/providers/candidate_generator/urgency_router/change_engine/semantic_guardian imports
3. No dynamic-code calls (exec/eval/compile)
4. **Three consumers MUST delegate** — `module_discovery_consumer_flag_registry_seed` / `module_discovery_consumer_shipped_code_invariants` / `module_discovery_consumer_help_dispatcher` pins forbid `pkgutil.iter_modules` outside imports + require `discover_module_provided_callable` import

## Three consumers refactored

| Consumer | Pattern | Handler |
|---|---|---|
| `flag_registry_seed._discover_module_provided_flags` | `fn(registry) -> int` | `make_registry_handler(registry=...)` |
| `help_dispatcher._discover_module_provided_verbs` | `fn(registry) -> int` | `make_registry_handler(registry=...)` |
| `shipped_code_invariants._discover_module_provided_invariants` | `fn() -> Iterable[ShippedCodeInvariant]` | `make_factory_handler(register_one=register_shipped_code_invariant)` |

~120 LOC duplication eliminated. Public API of each consumer unchanged (return type identical).

## Bug fix bonus

`reset_registry_for_tests()` in `meta/shipped_code_invariants.py` previously only re-seeded the static set, dropping module-owned pins (M10's 8 + cleanup's 7) across test isolation. Slice 2 fix: reset rebuilds the FULL registry (seed + discovered) so post-reset state matches boot-time state.

## Test spine

- `tests/governance/test_module_discovery.py` — 32 tests covering: master flag asymmetric semantics / happy-path real-codebase walk / per-package + per-module exception isolation / recursion guard / handler return-coercion / DiscoveryReport schema + projection / 2 convenience handlers / 3-consumer-uses-primitive enforcement / authority asymmetry / no-dynamic-code / reset_registry_for_tests fix / synthetic broken-module isolation / public API exports
- 366/366 across module_discovery + cleanup_arc_slice1 + shipped_code_invariants + flag_registry + help_dispatcher + full M10 spine

## What's next

- **Slice 3** — `observability_route_registry.py` auto-mount substrate (~5h, ~25 tests). Wires 5 dormant observability surfaces (m10/decisions/curiosity/budget/action_outcome) via single boot call from `event_channel.py`.
- **Slice 4** — `repl_dispatch_registry.py` (~5h, ~25 tests). Replaces if/elif ladder in `serpent_flow.py` with auto-discovered registry; unlocks 5 new REPL verbs.
- **Slice 5** — Graduation + observability bridge AST pins + ~125 total regression tests + PRD v2.19 → v2.20

## Architectural significance

This is the **shared primitive** that all future Slice 5 arcs (3, 4 of this consolidation arc + Phase 10 + Phase 9) compose with. Future arcs ship surfaces that auto-mount through this primitive structurally — Slice 5b debt class closes by construction, not by manual wiring per arc.
