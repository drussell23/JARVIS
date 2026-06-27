---
title: Project Cleanup Arc Slice1
modules: [backend/core/ouroboros/governance/graduation_orchestrator.py, backend/core/ouroboros/governance/graduation_tracker.py, tests/governance/test_graduation_orchestrator.py, backend/core/ouroboros/governance/cleanup_invariants.py, tests/governance/test_cleanup_arc_slice1.py]
status: historical
source: project_cleanup_arc_slice1.md
---

**Status (2026-05-04)**: §32.5 Slice 1 CLOSED — full archive landed same-day. Slices 2-5 (Slice 5b auto-discovery consolidation) PENDING — operator-approved 3-day combined arc.

## Investigation findings (deeper than original PRD plan)

Original §32.5.1 plan: "move 1 file + 1 test." Investigation revealed broader cleanup needed:

- `graduation_orchestrator` was instantiated at boot (`harness.boot_graduation()` line 628) but **structurally unreachable in production** — `_graduation_tracker` gate at `runtime_task_orchestrator.py:1447` was never assigned anywhere in the codebase, so the chained `evaluate_graduation()` call could never fire
- Companion `graduation_tracker.py` had **zero importers anywhere** — pure orphan
- 3 production files contained dead wiring: harness.py (3 sites) + runtime_task_orchestrator.py (1 block) + governed_loop_service.py (1 hook)

Per operator mandate "no workarounds": cleanup excised the dead wiring alongside archiving the modules — the proper root-cause fix.

## Files moved (via `git mv` — history preserved)

- `backend/core/ouroboros/governance/graduation_orchestrator.py` → `archive/legacy/graduation_orchestrator_2026_04_06.py`
- `backend/core/ouroboros/governance/graduation_tracker.py` → `archive/legacy/graduation_tracker_2026_04_06.py`
- `tests/governance/test_graduation_orchestrator.py` → `archive/legacy/test_graduation_orchestrator_2026_04_06.py`

## Dead wiring removed

- `harness.py:311` — `self._graduation_orchestrator: Any = None` declaration
- `harness.py:628` — `await self.boot_graduation()` boot call
- `harness.py:1671-1681` — entire `boot_graduation()` method
- `runtime_task_orchestrator.py:1431-1450` — structurally-unreachable graduation gate block
- `governed_loop_service.py:2517-2529` — always-None `_graduation_tracker` op-completion hook

## New module: `cleanup_invariants.py`

`backend/core/ouroboros/governance/cleanup_invariants.py` (~280 LOC) — auto-discovered via `register_shipped_invariants()`. Owns archive-only enforcement with 4 pins:

1. `graduation_orchestrator_archived_only_harness` — harness.py forbidden imports + forbidden method (`boot_graduation`) + forbidden attribute (`self._graduation_orchestrator`)
2. `graduation_orchestrator_archived_only_runtime_task` — runtime_task_orchestrator.py forbidden imports + forbidden symbols (`_graduation_tracker` / `_graduation_orchestrator` / `evaluate_graduation`)
3. `graduation_orchestrator_archived_only_governed_loop` — governed_loop_service.py forbidden imports + forbidden symbol (`_graduation_tracker`)
4. `graduation_orchestrator_module_archived` — sentinel pin asserting (a) 3 archived files exist at expected `archive/legacy/` paths, (b) 3 forbidden production paths absent, (c) provenance README present

Authority asymmetry pinned: stdlib + ShippedCodeInvariant import ONLY. No orchestrator/iron_gate/policy/providers imports.

## Archive provenance

`archive/legacy/README.md` documents the salvage discipline: what M10 inherited (15-phase FSM + Bayesian AdaptiveThreshold + H1-H6 + 5-layer validation), what was rejected (direct LLM substrate, EphemeralUsageTracker, layer implementations themselves), and the architectural Reverse-Russian-Doll lineage framing.

## Tests

- `tests/governance/test_cleanup_arc_slice1.py` — 16/16 green
- Combined regression sweep: 226/226 across M10 (173) + cleanup (16) + shipped_code_invariants (37)

## Effort actual

1 day (vs ~2-day estimate). Faster because (a) `jarvis_intelligence.py:447` was already closed pre-§32.5 (audit revealed pre-existing FlagRegistry-based replacement), (b) the orchestrator + tracker were structurally unreachable so no migration was needed, just dead-code excision.

## What's next (Slices 2-5)

Slice 5b auto-discovery consolidation arc:

- **Slice 2** — `meta/module_discovery.py` substrate (~6h, ~30 tests). Extracted shared abstraction so `shipped_code_invariants` + `help_dispatcher` reuse the primitive instead of parallel walkers
- **Slice 3** — `observability_route_registry.py` (~5h, ~25 tests). Auto-mounts the 5 dormant observability surfaces (m10/decisions/curiosity/budget/action_outcome) via single boot call
- **Slice 4** — `repl_dispatch_registry.py` (~5h, ~25 tests). Replaces if/elif ladder in serpent_flow.py with auto-discovered registry; unlocks 5 new REPL verbs
- **Slice 5** — Graduation + 6 AST pins + ~125 total regression tests + PRD v2.18 → v2.19

The architectural pattern: future Slice 5 arcs ship surfaces that auto-mount; the Slice 5b debt class closes structurally, not just for the 5 currently-dormant arcs.
