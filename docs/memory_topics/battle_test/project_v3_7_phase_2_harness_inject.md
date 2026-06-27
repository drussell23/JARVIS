---
title: Project V3 7 Phase 2 Harness Inject
modules: [backend/core/ouroboros/governance/swe_bench_pro/harness_inject.py, backend/core/ouroboros/battle_test/harness.py, backend/core/ouroboros/governance/swe_bench_pro/__init__.py, tests/governance/test_swe_bench_pro_harness_inject.py, backend/core/ouroboros/governance/repair_engine.py]
status: historical
source: project_v3_7_phase_2_harness_inject.md
---

May 12 2026 — SWE-Bench-Pro harness boot hook shipped on branch `ouroboros/swe-bench-pro/harness-inject`. **Stop condition met for first live run.**

## What this PR closes

Before this PR, the SWE-Bench-Pro arc had substrate but no operator-facing path to actually run O+V live on it:

| Prerequisite | Pre-PR | Post-PR |
|---|---|---|
| Phases A → F substrate | ✅ (339 tests) | ✅ |
| Phase A loader (cache + JSONL + HF) | ✅ | ✅ |
| **Harness driver / boot hook** | ❌ | ✅ |
| **≥1 on-disk problem fixture** | ❌ | ✅ |
| **Cost/wall caps documented** | ❌ | ✅ |

The operator's stop condition (2026-05-12) was explicit:
> No "full benchmark" until: harness hook merged, ≥1 on-disk problem via checked-in or scripted reproducible seed, and cost/wall caps documented for the operator run.

All three are now met. No live run yet — that's the operator's call.

## Architectural decisions

**Root problem solved at source — no shortcut**:

The shortcut would have been to write a one-off `/tmp/swe_bench_pro_driver.py` script. The operator explicitly rejected that path:
> A one-off /tmp driver + random public repo is fine for a personal 30-minute spike to debug, but it does not strengthen the repo (no regression lock, no harness parity). Anything worth keeping belongs in Option 2's fixture + hook.

The structural fix: mirror the L2 exercise corpus precedent exactly. That arc already proved the boot-hook shape works end-to-end through the same `IntakeLayerService.ingest_envelope` surface. Mirroring it gives:
- Shape-parity with `maybe_inject_exercise_at_boot` — same verdict taxonomy, same fail-open contract, same lazy-import-inside-try harness wiring
- Regression-lockable behavior — CI runs the same entrypoint operators run
- Reusable observability — IDE extensions already subscribe to `operation_terminal` SSE per B.2.0.5

**Composition discipline (AST-pinned, 4 pins)**:

1. Canonical Phase A `load_problem` import (no parallel loader)
2. Canonical Phase B.1 `prepare_problem` import (no parallel worktree manager)
3. Canonical Phase B.2.1 `build_evaluation_envelope` import (no parallel envelope shape)
4. No `WorktreeManager` or `UnifiedIntakeRouter` AST name references — composition strictly through `prepare_problem` + `intake_service.ingest_envelope`

The AST pin walks `ast.Name` + `ast.ImportFrom` nodes (NOT docstrings) so prose explanations of WHY we don't bypass the canonical surfaces don't trip the pin.

**Orthogonal master flags**:

`JARVIS_SWE_BENCH_PRO_HARNESS_INJECT_ENABLED` is intentionally orthogonal to `JARVIS_SWE_BENCH_PRO_ENABLED` (Phase A). This lets operators:
- Have the loader enabled (Phase A on) without auto-injecting at every harness boot
- Run unit tests / offline scoring against the loader without triggering the boot hook
- Flip the boot hook on for a single soak session without flipping the whole arc

Both flags default-FALSE per §33.1; the arc ships dormant.

**Two-tier instance-id selection**:

- CSV override (`JARVIS_SWE_BENCH_PRO_INJECT_INSTANCE_IDS`) takes priority — explicit operator-chosen problem set
- Count first-N (`JARVIS_SWE_BENCH_PRO_INJECT_COUNT`, default 1) — first N from `list_cached_problems()` when CSV unset

This matches operator workflows: reproducing a specific failing problem uses CSV; first-time soaks use count.

**Minimal fixture**:

`tests/fixtures/swe_bench_pro/problems.jsonl` ships ONE record against `octocat/Hello-World` (GitHub's canonical stable public repo). The `test_patch` adds a trivially-passing test so even a no-op model scores PASS — this fixture validates the harness wiring, NOT the model's solving ability. Real benchmark problems come from HF in option 3 (post wiring-validation).

The fixture is deliberately minimal: 1 record, trivial test_patch, well-known repo. Operators wanting real benchmarks configure `JARVIS_SWE_BENCH_PRO_HF_DATASET` (documented in the fixture's README + PRD §40.7.10-arc runbook).

## Composition discipline — what was deliberately NOT done

- No one-off `/tmp` driver script (operator rejected as canonical deliverable)
- No parallel worktree management — composes Phase B.1 `prepare_problem` (AST-pinned)
- No direct `UnifiedIntakeRouter` access — composes `intake_service.ingest_envelope` (AST-pinned)
- No new envelope shape — composes Phase B.2.1's `build_evaluation_envelope` verbatim
- No live run in this PR per operator stop condition — runbook published, operator decides when to flip masters
- No edits to `repair_engine.py` — `_run_inner` sha256 stays `9e881fdde25ec5b1`

## Files

- `backend/core/ouroboros/governance/swe_bench_pro/harness_inject.py` — substrate (NEW)
- `backend/core/ouroboros/battle_test/harness.py` — wiring (block AFTER L2 exercise hook)
- `backend/core/ouroboros/governance/swe_bench_pro/__init__.py` — exports
- `tests/fixtures/swe_bench_pro/problems.jsonl` — minimal fixture (NEW)
- `tests/fixtures/swe_bench_pro/README.md` — fixture usage docs (NEW)
- `tests/governance/test_swe_bench_pro_harness_inject.py` — 19-test spine + 4 AST pins (NEW)
- `docs/architecture/OUROBOROS_VENOM_PRD.md` — §40.7.10-soak closure + §40.7.10-arc operator runbook

## Master flags (FlagRegistry auto-seeded — 3 specs)

- `JARVIS_SWE_BENCH_PRO_HARNESS_INJECT_ENABLED` (BOOL/SAFETY, default FALSE)
- `JARVIS_SWE_BENCH_PRO_INJECT_COUNT` (INT/CAPACITY, default 1)
- `JARVIS_SWE_BENCH_PRO_INJECT_INSTANCE_IDS` (STR/INTEGRATION, default empty)

## What's next — first live run

Stop condition met. Operator-bound budget: **$2.00 max for 5–10 problems**. Two paths:

1. **Wiring-validation live run** (~$0.01–0.10) — flip the masters with the checked-in fixture; verify O+V dispatches against `octocat/Hello-World`; observe `operation_terminal` SSE; check JSONL audit + report card render. Lowest-cost first contact with the live pipeline.

2. **Real benchmark cherry-pick** (~$0.05–2.00) — configure HF dataset, cherry-pick 1–3 known problems, run with `--cost-cap 2.00`. Highest fidelity to the published benchmark.

Both runbooks documented in PRD §40.7.10-arc.

Graduation criterion (preliminary): SWE-Bench-Pro masters stay default-FALSE until a soak demonstrates ≥1 RESOLVED outcome on a known-good problem AND ≥1 UNRESOLVED outcome on a known-hard problem (rubric sanity floor — distinguishes real fixes from non-fixes).
