---
title: Test→Source Attribution Bridge — Slice 6
modules: [backend/core/ouroboros/governance/intent/test_source_attribution.py, backend/core/ouroboros/governance/reverse_dep_resolver.py, backend/core/ouroboros/governance/intent/test_watcher.py, backend/core/ouroboros/governance/intent/signals.py, backend/core/ouroboros/governance/orchestrator.py, backend/core/ouroboros/governance/phase_runners/gate_runner.py]
status: closed
source: project_slice6_test_source_attribution.md
---

2026-07-11: 7-task slice closing the Run #16 blind-mutation class — a
`TestFailure` signal's `target_files` was definitionally the failing TEST
file, never the source module under test, so autonomous repair could never
reach the actual bug.

## The Run #16 evidence chain

- **The pin** — `backend/core/ouroboros/governance/intent/test_watcher.py:444`:
  `target_files=(f.file_path,)` where `file_path = test_id.split("::")[0]`
  (the test file, definitionally — never the source it exercises).
- **The double-bind** — `doubleword_provider.py:2490-2512`'s
  `file_scope_mismatch` guard rejects any candidate whose paths don't
  intersect `ctx.target_files`. Net effect: a CORRECT source-file patch is
  REJECTED as an out-of-scope mutation, while a test-file edit (which
  cannot fix the bug) passes the scope check cleanly. Two independently
  correct guards compose into a structural deadlock.
- **The kill** — `verify_gate.py:50-54`: VERIFY dies deterministically at
  `pass_rate=0.75 < 1.00` because the source bug survives every
  test-file-scoped APPLY. The loop cannot converge; it can only retry the
  same wrong scope.
- **Why traceback-only attribution doesn't close the gap**: for the
  Run-16 class (assertion failures), the deepest in-repo stack frame IS
  the test line itself — the traceback never points at the source module.
  Traceback frames are therefore a ranking TIE-BREAKER only (used to order
  already-resolved candidates), never the resolution mechanism. Imports
  are primary.

## The mandate set (user-locked, 2026-07-11, verbatim intent)

1. **Root-Cause Only** — no naive string-matching heuristics, no regex
   path mapping (e.g. `tests/test_foo.py` → `src/foo.py`), no hardcoded
   directory assumptions. Must construct a deterministic dependency
   bridge, not a path convention guess.
2. **Architectural Purity** — dynamically resolve the target source by
   parsing the AST of the failing test module and tracing its actual
   `import` statements back to the specific source file(s) it exercises.
3. **DRY** — no new Python parser. A substrate audit was run FIRST:
   `opportunity_miner_sensor.py`'s `_import_fan_out` only counts top-level
   import segments (discards dotted names, mandate-1-forbidden string
   heuristic in `_get_module_name`); `doc_staleness_sensor.py` never
   touches `ast.Import` at all. The repo's sanctioned import-graph layer
   is `reverse_dep_resolver.py` — stdlib-only, full `import`/`from`/`as`/
   relative handling, already reused by `target_stratification.py`,
   `autonomous_pr_pipeline.py`, `phase_runners/slice4b_runner.py`. Slice 6
   extends it (`extract_module_imports` factored out of
   `_build_forward_import_graph`'s inlined walk; `build_module_to_path`
   added as the inverse mapper the existing rglob loop already walks but
   discarded) rather than duplicating a parser.
4. **Bulletproof** — account for indirect imports, aliased imports
   (`import x as y`), and framework-level abstractions (conftest
   fixtures, `mock.patch`/`monkeypatch.setattr` string targets) that
   might obscure the direct source link. If the source cannot be
   deterministically resolved, fail-fast with a typed attribution error
   rather than blindly mutating the test file.

## Substrate audit rationale (why `reverse_dep_resolver.py`)

Candidates considered and rejected before building on `reverse_dep_resolver`:
- `opportunity_miner_sensor.py._import_fan_out` — counts import statements
  for opportunity scoring only; discards dotted qualification, and its
  module-name resolution is a string heuristic (violates mandate 1).
- `doc_staleness_sensor.py` — never parses `ast.Import` nodes at all; it
  diffs doc mtimes against code mtimes, no import graph involved.

`reverse_dep_resolver.py` was already the load-bearing, stdlib-only,
alias/relative-aware AST import layer three other subsystems depend on.
Slice 6 factored its inlined per-module walk into public
`extract_module_imports(tree, module, is_init) -> Set[str]` (proven
byte-identical to the pre-refactor forward-graph output via a same-input
equality test) and added `build_module_to_path(root) -> Dict[str, str]`
as the missing inverse (`{dotted_module: repo_relative_path}`,
deterministic sorted walk, first-wins on collision).

## The attributor (`test_source_attribution.py`)

`attribute_test_to_sources(test_file, *, repo_root, traceback_frames=())`
parses the failing test module's AST, extracts import targets via
`extract_module_imports` plus `mock.patch`/`monkeypatch.setattr` string
literals (receiver-identity checked — a REST client's `client.patch(path)`
or a bare `setattr(obj, "x.y", val)` must NOT match), resolves each dotted
name to a repo path via longest-prefix lookup against the cached
module→path map (TTL-bounded, one `rglob` per repo per TTL window, not per
failing test), filters out anything under the configured test tree
(`JARVIS_TEST_DIR_NAMES`, reused from TestRunner — config-driven, not
hardcoded), and ranks the survivors (traceback-implicated first, direct
imports before patch targets, then lexical) bounded to
`JARVIS_ATTRIBUTION_MAX_SOURCE_FILES`.

Returns a frozen `Attribution(test_locus, source_loci, method,
evidence_kinds)`. `source_loci` is NEVER empty — emptiness raises the
typed `AttributionUnresolved(reason, detail)` instead, with `reason` one
of `test_outside_root | test_file_missing | parse_error |
no_first_party_source_imports`. `method` is derived honestly from the
evidence kinds actually present — valid values are `"direct_import"`,
`"patch_target"`, or `"direct_import+patch_target"` (never inferred from
one kind's presence when the other is absent).

## Wiring

- **Evidence schema** (`signals.py::build_attribution_evidence`,
  schema-versioned, mirrors the `VisionSignalEvidence` discipline):
  `{schema_version, status, test_locus, source_loci, method, reason}`
  where `status` is `resolved | unresolved | disabled`.
- **`test_watcher.py::process_failures`**: when attribution is disabled,
  stamps `status=disabled` and keeps legacy test-locus scope. When
  enabled, calls `attribute_test_to_sources`; on success composes
  `target_files=(*source_loci, test_locus)` (both loci, so
  `file_scope_mismatch` passes a correct source patch AND the test file
  stays in scope); on `AttributionUnresolved`, scope stays test-locus and
  evidence carries `status=unresolved` + the typed reason. Any
  UNEXPECTED attributor fault (not `AttributionUnresolved`) degrades to
  legacy scope — fail-soft, never eat a real failure signal.
- **The gate — `_attribution_scope_risk_floor`** (Task 5, wired on BOTH
  GATE paths, a Task-5 review finding closed the gap): the predicate
  lives in `orchestrator.py` (inline path) and is imported into
  `phase_runners/gate_runner.py` (the extracted path — the shipping
  default under `JARVIS_PHASE_RUNNER_GATE_EXTRACTED`). Post-VALIDATE,
  when an op's attribution is `unresolved` AND the candidate mutates ONLY
  test loci, escalates risk tier to `APPROVAL_REQUIRED` — a HUMAN GATE,
  not a reject, mirroring the SemanticGuardian hard-finding escalation at
  the same site (stricter-wins, `risk_tier.value < APPROVAL_REQUIRED.value`,
  never a downgrade). Fail-soft: any exception (import error, malformed
  evidence, predicate raise) yields no escalation.
- **Task-5 lesson (caught in review)**: the gate was first wired only
  into the inline orchestrator path. `JARVIS_PHASE_RUNNER_GATE_EXTRACTED`
  is default-true, meaning production traffic runs the EXTRACTED
  `GATERunner`, not the inline path — so the first pass shipped a gate
  that was dead on the actual default route. Fixed by wiring both paths
  with behavioral tests exercised through the real `GATERunner`, not just
  the inline orchestrator function.

## Feasibility distribution (measured over 2,972 test files)

- **~73%** trivially resolvable via direct `backend.` imports.
- **~17%** recoverable only via `mock.patch(...)`/`monkeypatch.setattr(...)`
  target strings (no direct import of the module under test).
- **~6%** multi-source (test exercises 2+ backend subtrees) — all carried,
  bounded by `JARVIS_ATTRIBUTION_MAX_SOURCE_FILES`.
- **~5%** dynamic/no-backend-import → fail-fast unresolved (typed error,
  scope stays test-locus, gate escalates on blind mutation attempts).

This distribution is the basis for the Deferred/YAGNI list: transitive
import closure (test → helper → source) and dynamic
`importlib.import_module("literal")` string extraction are both deferred
until `attribution.method` telemetry from live runs shows a real miss —
the measured 73%+17% direct coverage was judged sufficient to ship without
them. conftest fixture *tracing* was also deferred: fixtures are test
infrastructure (env isolation, sys.path), not source-module wrappers, and
the config-driven test-tree exclusion already classifies them correctly.

## Env knobs

`JARVIS_TEST_SOURCE_ATTRIBUTION_ENABLED` (bool, default true) — master;
`JARVIS_ATTRIBUTION_SCOPE_GATE_ENABLED` (bool, default true) — gate
sub-switch; `JARVIS_ATTRIBUTION_MAX_SOURCE_FILES` (int, default 8);
`JARVIS_ATTRIBUTION_MODULE_MAP_TTL_S` (float, default 300).

## Acceptance bar (not part of the doc/flag task — user-conducted)

Run #16 re-fired via `scripts/ignite_a1_soak.py --max-wall-seconds 5000`
(Docker up, no chaos pre-arm — driver injects post-boot). Success chain:
chaos inject red → TestFailure signal whose debug.log line shows
`[Attribution] tests/.../test_leaf_predicates.py ->
['backend/.../leaf_predicates.py']` → op `target_files` contains the
SOURCE file → adversary's manifest full-file repair passes the scope gate
→ APPLY targets the source → VERIFY `pass_rate=1.0` → AutoCommit. Per
`feedback_agent_conducted_soak_delegation`, this ignition is the user's
call, not an implementation-task step.

## Slice 7 — subset coverage semantics (2026-07-11)

Run #17 fired the live proof of the section above: attribution resolved
correctly (`[Attribution] test_leaf → leaf_predicates (direct_import)`,
scope = `[source, test]`), one layer deeper than Run #16 — but the op
still FAILED with `multi_file_coverage_insufficient covers 1/2`. The
`MultiFileCoverageGate` was reading attributed scope as an EXHAUSTIVE
change-set (both loci must be touched) when Slice 6 made it deliberately
PERMISSIVE (either locus is a valid fix target — a source-only repair is
correct and complete on its own). The gate outlived the assumption it was
built under.

**Fix — subset semantics, evidence-scoped.** `attribution_status()`
(`test_source_attribution.py`) is a new single fail-soft parser over the
Slice-6 evidence block — `unattributed_test_scope_violation` was refactored
onto it so there's one reader, not two. `MultiFileCoverageGate.check_candidate`
gained `*, intake_evidence_json=""`: when `attribution.status == "resolved"`
and `JARVIS_ATTRIBUTION_SUBSET_COVERAGE_ENABLED` is not falsy, a candidate
covering **>=1** of the attributed target files passes instead of being
rejected; zero-coverage candidates are still rejected outright; ops without
resolved attribution (or with the flag off) keep the pre-Slice-7 strict
full-coverage demand. Any fault parsing evidence (malformed JSON, missing
keys, unexpected exception) falls CLOSED to strict superset — the subset
relaxation never fires on ambiguous input. **Correction (final review,
2026-07-12):** the pre-existing `file_scope_mismatch` guard
(`doubleword_provider.py:2496-2515`) does NOT enforce ⊆ containment — it
only rejects a candidate whose paths have EMPTY INTERSECTION with
`ctx.target_files` (a candidate touching `[target, /anywhere/else.py]`
passes), reads only the top-level `file_path` (not `files: [...]`
entries), and runs on the DoubleWord provider path only — TestFailure ops
route IMMEDIATE straight to Claude, where it never runs at all. So
subset-coverage semantics widens what counts as SUFFICIENT coverage while
real containment of candidate paths against the attributed scope is not
enforced anywhere today; a genuine containment check at the waiver site
for resolved-attribution candidates is a named, ledgered follow-up, not
yet built.

**Wiring — the Slice-6 T5 lesson, applied structurally.** Intake evidence is
forwarded at BOTH GENERATE call sites — inline `orchestrator.py` and the
extracted `phase_runners/generate_runner.py` (the shipping default under
`JARVIS_PHASE_RUNNER_GATE_EXTRACTED`) — and pinned by a single AST wiring
test parametrized over both files, so a future refactor that adds a third
call path or silently drops the argument on one path fails CI instead of
shipping wired-but-inert like the Task-5 gate did.

**Fast-follow — lock-free prewarm probe.** Slice-6 final review flagged
`prewarm_module_map`'s freshness probe as blocking the event loop: an
in-flight executor build holds `_MAP_CACHE_LOCK` for ~7s (one repo-wide
`rglob`), and probing staleness under that same lock serialized every
concurrent caller behind it. Fixed to read cache freshness lock-free.

**Proof.** `tests/governance/intent/test_attribution_e2e_leaf_predicates.py`
pins the exact Run #17 scenario end-to-end: real `TestWatcher`-produced
evidence flowing through the real gate, asserting the source-only
candidate now passes where it previously rejected at `covers 1/2`.

Env: `JARVIS_ATTRIBUTION_SUBSET_COVERAGE_ENABLED` (bool, default true) —
`backend/core/ouroboros/governance/multi_file_coverage_gate.py`.
