---
title: Project Repl Dispatch Registry Slice4
modules: [backend/core/ouroboros/battle_test/repl_dispatch_registry.py, tests/governance/test_repl_dispatch_registry.py]
status: historical
source: project_repl_dispatch_registry_slice4.md
---

**Status (2026-05-04)**: Slice 4 CLOSED. 36 new tests + 1 new pin green; 325/325 across full sweep.

## What landed

`backend/core/ouroboros/battle_test/repl_dispatch_registry.py` (~330 LOC, pure substrate):

- `try_dispatch(line: str) -> DispatchOutcome` — single entry point routes verb-shaped lines through auto-discovered map
- `prime_registry(*, packages, excluded_verbs, excluded_modules, force) -> RegistryReport` — idempotent priming (cached after first call)
- `list_verbs() -> Tuple[str, ...]` — telemetry snapshot
- `reset_registry_for_tests()` — test isolation
- Frozen `DispatchOutcome(matched, ok, text, verb)` + frozen `RegistryReport(verb_count, verbs, excluded, elapsed_s, master_flag_on)` with `as_dict()` projection
- Master flag `JARVIS_REPL_DISPATCH_AUTODISCOVERY_ENABLED` default-true with asymmetric env semantics

## Slice 2 primitive extended

`module_discovery.discover_module_provided_callable` gained an additive `attr_name=None` "module-scan mode" — handler is invoked once per imported module with the module object itself, used by REPL registry where the dispatcher attribute name (`dispatch_<verb>_command`) varies per module by filename convention.

## Verb-name extraction (filename convention)

- `X_repl.py` → verb `X` (e.g. `decisions_repl.py` → `decisions`)
- `<sub>/repl.py` → verb `<sub>` (e.g. `m10/repl.py` → `m10`)
- Anything else → skip

## 17+ verbs auto-discovered

**5 legacy (previously hardcoded)**: probe, coherence, quorum, failures, outcomes
**12 newly-unlocked**: m10, decisions, curiosity, governor, posture, cost, hypothesis, replay, recovery, render, compact, backlog_auto_proposed

## 7 verbs excluded (custom handlers retained)

`budget`, `risk`, `goal`, `cancel`, `plan`, `postmortems`, `inline` — bespoke operator semantics that diverge from pure `dispatch_<verb>_command(line)` contract:
- `/budget 1.00` sets cost cap (numeric, not subcommand)
- `/cancel <op-id>` schedules cooperative cancellation
- `/postmortems` takes argv-style invocation
- `/risk`, `/goal`, `/plan`, `/inline` — runtime config

## serpent_flow.py refactor

- 5-branch if/elif ladder for probe/coherence/quorum/failures/outcomes → REMOVED
- `_print_observability_verb` helper (~60 LOC) → REMOVED
- Single `try_dispatch(line)` call replaces both
- Custom handlers (`_handle_budget`, `_handle_risk`, `_handle_goal`, `_handle_cancel`, `_print_postmortems`) retained verbatim
- Master-flag-gated for instant rollback

## 1 new AST pin

`repl_dispatch_registry_uses_primitive` — registry MUST delegate to module_discovery (no parallel walker; forbids `pkgutil.iter_modules`).

`cleanup_invariants.py` now registers 14 pins total:
- 4 archive-only (Slice 1)
- 3 consumer-uses-primitive for flag_registry_seed / shipped_code_invariants / help_dispatcher (Slice 2)
- 5 observability-module-exposes-register_routes per dormant module (Slice 3)
- 1 observability_route_registry_uses_primitive (Slice 3)
- 1 repl_dispatch_registry_uses_primitive (Slice 4)

## Test spine

`tests/governance/test_repl_dispatch_registry.py` — 36 tests covering: master flag asymmetric semantics / 5 legacy + 12 newly-unlocked verbs / 7-verb exclusion list / idempotent priming / try_dispatch routing (matched/unmatched/excluded/empty/bare-form) / verb-name extraction (4 shapes via parametrize) / signature rejection (synthetic package) / dispatcher exception isolation (synthetic package) / registry-composes-primitive (inspect-source) / SerpentREPL hookup verification / legacy helper removal verification / authority asymmetry / public API exports / DispatchOutcome+RegistryReport schema.

`test_slice_5b_e_repls.py` updated: `test_serpent_dispatches_probe` rewritten to `test_serpent_uses_repl_dispatch_registry` asserting on the NEW pattern; `test_repl_registry_resolves_legacy_dispatchers` proves byte-equivalence for the 5 legacy verbs.

## Architectural significance

Slice 5b debt class now CLOSED for BOTH HTTP routes (Slice 3) AND REPL commands (Slice 4) by construction:
- Past pattern: each Slice 5 arc shipped `*_observability.py` + `*_repl.py`, then needed manual edits to event_channel.py + serpent_flow.py to wire — 2 surfaces × N arcs = N×2 manual edits
- New pattern: ship the files with canonical names; both surfaces auto-route zero-edit

Future Phase 9 / Phase 10 arcs inherit this for free. The Reverse Russian Doll Antivenom (the immune system) scales by the same pattern: structural pins ensure every new arc adheres to the naming conventions, so the cage never lags behind the spawning core.

## What's next

Slice 5 — final graduation arc:
- Comprehensive end-to-end smoke test (all 5 observability surfaces reachable + all 17+ REPL verbs route)
- Architectural locks recap pin (umbrella invariant asserting all 4 slices' invariants are present)
- PRD v2.21 → v2.22 closure

After Slice 5 closes, this entire 4-slice consolidation arc closes, and the next item in §32.8 v4 sequencing is **Phase 10 Slices 2-6** (THE PURGE / TopologySentinel finishing — ~2-3 weeks).
