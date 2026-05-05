# Archive — Legacy Modules

This directory preserves O+V modules that have been superseded by newer
substrate but whose **design** remains canonical reference for the
arcs that absorbed them. Code is moved here, not deleted, so that:

1. The architectural lineage of newer arcs (e.g. M10's inheritance of
   `graduation_orchestrator`'s 15-phase FSM + Bayesian
   `AdaptiveThreshold` + H1-H6 hard-won lessons) remains traceable.
2. Future audits can confirm what was lifted, what was rejected, and
   why.
3. Git blame stays unbroken — `git mv` was used, not `rm` + `add`.

Files here MUST NOT be imported from production code. The
`graduation_orchestrator_archived_only` AST pin in
`backend/core/ouroboros/governance/meta/shipped_code_invariants.py`
asserts this structurally; CI fails any production module that
re-introduces an `import` of an archived module.

---

## Inventory

### `graduation_orchestrator_2026_04_06.py`

**Original location**:
`backend/core/ouroboros/governance/graduation_orchestrator.py`

**Salvaged date**: 2026-05-04 (PRD §32.5 cleanup arc, Slice 1)

**What it was**: 1,137-LOC monolithic graduation engine for the
"ephemeral code synthesis graduation" lifecycle. Contained a
15-phase FSM (`GraduationPhase`), a Bayesian
`AdaptiveThreshold` (Beta(1+s, 1+f) posterior × diversity-adjusted),
H1-H6 hardening lessons, and a 5-layer validation pipeline.

**Why archived**:

- The runtime ephemeral-synthesis path that gated the orchestrator's
  `evaluate_graduation()` call (`runtime_task_orchestrator.py:1431-1450`)
  was structurally unreachable: `_graduation_tracker` was never
  assigned anywhere in the codebase, so the gate never opened.
- `harness.boot_graduation()` instantiated `GraduationOrchestrator()`
  at battle-test boot, but the instance was never invoked by any
  caller.
- `governed_loop_service.py:2517-2529` had an op-completion hook that
  read `_graduation_tracker` (always `None`) and was therefore also
  unreachable.

**What was salvaged into M10** (PRD §32.4):

- 15-phase FSM → `m10.primitives.M10ProposalPhase` (16 values; M10
  added one terminal phase)
- Bayesian `AdaptiveThreshold` → `m10.primitives.M10AdaptiveThreshold`
  + `compute_threshold()` (lifted verbatim from
  `compute_adaptive_threshold` lines 63-85)
- H1-H6 hard-won lessons → encoded as architectural locks in
  `m10/lifecycle.py` (e.g. H3 push-fail preserves branch)
- 5-layer validation pipeline → `m10.lifecycle.ValidationLayer` enum
  + `ProposalLifecycleOrchestrator.advance()` method (Layers 3+4
  parallelized via `asyncio.gather`)

**What was NOT salvaged**:

- Direct LLM call substrate (M10 routes through
  `candidate_generator` STANDARD route + `GenerativeQuorum` K=3
  consensus instead — composes existing cage)
- The `EphemeralUsageTracker` companion (was always-None gated; no
  M10 analog needed)
- The 5 layer implementations themselves (M10 caller-injects via
  Bridge Protocols — `WorktreeManager` / `AutoCommitter` /
  `OrangePRReviewer` / `SemanticGuardian`)

### `graduation_tracker_2026_04_06.py`

**Original location**:
`backend/core/ouroboros/governance/graduation_tracker.py`

**Salvaged date**: 2026-05-04

**What it was**: Companion module — tracked progress through autonomy
graduation gates with persistent JSON state under
`~/.jarvis/ouroboros/`.

**Why archived**: Zero importers anywhere in the codebase. Pure
orphan. The `_graduation_tracker` attribute that this class would
have populated was never assigned, so the module had no runtime
consumer.

### `test_graduation_orchestrator_2026_04_06.py`

**Original location**:
`tests/governance/test_graduation_orchestrator.py`

**Salvaged date**: 2026-05-04

**What it was**: 301-LOC test suite for `GraduationOrchestrator` and
`EphemeralUsageTracker`.

**Why archived**: Tests track archived code; they're preserved for
historical reference but excluded from the production test
collection. M10's regression spine
(`tests/governance/test_m10_*.py` — 173 tests) is the canonical
successor.

---

## Architectural Note

The pattern of "preserve design, archive code" is a deliberate
discipline (see PRD §32.4 — Path C verdict). When a newer arc
inherits the *design* from a legacy module, the legacy code is not
deleted because:

1. The design ancestor's docstrings, comments, and inline reasoning
   document why the inherited choices were made.
2. Future operators auditing M10's threshold formula can compare
   against the reference implementation here verbatim.
3. The cleanup AST pin enforces archive-only — production cannot
   accidentally regress to importing the legacy substrate.

**This is the Reverse Russian Doll discipline applied to code
hygiene**: the inner doll (M10) carves an exponentially larger,
smarter shell around the original core, but the original core's
design is preserved as a reference for verifying the carving was
faithful.
