---
title: Wave 2 (5) Slice 6a — Result
modules: [backend/core/ouroboros/governance/phase_dispatcher.py, orchestrator.py, tests/governance/phase_runner/test_phase_dispatcher.py, backend/core/ouroboros/governance/phase_runners/generate_runner.py]
status: historical
source: project_wave2_phaserunner_slice6a.md
---

# Wave 2 (5) Slice 6a — Result

**Status:** Infrastructure landed, dispatcher path parity-equivalent to inline on happy + cancel terminals. Flag stays **default off**. 6b (deep per-phase terminal matrix) pending.

## Why 6a + 6b + post-graduation deletion

Operator directive: *"Do not attempt 6-in-one-pass if it would thin dispatcher parity across phases—that repeats the failure mode we already rejected for 4a and 5."*

- **6a** (this commit): dispatcher infrastructure + flag + spine parity (happy + one high-signal terminal).
- **6b** (next): deep per-phase terminal parity matrix across all 9 phases, artifact propagation edge cases.
- **Post-graduation** (separate maintenance PRs): delete inline `else:` blocks once individual phase flags graduate. Dispatcher becomes canonical.

## What landed

| Artifact | Path | Notes |
|---|---|---|
| Dispatcher module | `backend/core/ouroboros/governance/phase_dispatcher.py` | `PhaseContext` dataclass, `PhaseRunnerRegistry`, `dispatch_pipeline` async function, `build_default_registry`, `dispatcher_enabled` helper |
| Flag hook | `orchestrator.py` ~line 1373 | `_run_pipeline` short-circuits to `dispatch_pipeline` when `JARVIS_PHASE_RUNNER_DISPATCHER_ENABLED=true`; else falls through to legacy inline path |
| Parity tests | `test_phase_dispatcher.py` | 25 tests: context slot semantics, registry register/get/miss, loud-fail contract (unknown phase / malformed runner / malformed result / factory exception wrap / iteration cap), dispatch_phase-follows-next_phase invariant, dispatcher-on ≡ dispatcher-off for happy + cancel terminal, authority invariant |

## Design: factory-per-phase pattern

Two patterns were considered for handling heterogeneous runner constructor signatures:

1. **Typed slots**: central `PhaseContext` with every possible field; runners read/write slots directly. Downside: hides which phase produced which field; runners must know the full cross-phase schema.
2. **Factory-per-phase** (chosen): registry stores `phase → factory(orch, serpent, pctx, ctx) → PhaseRunner`. Each factory plucks its deps from `pctx` (cross-phase context) and `ctx` (current OperationContext). Per-phase dep declarations stay explicit (greppable).

The dispatcher passes BOTH `pctx` and `ctx` to every factory because some cross-phase state lives on `pctx.<slot>` (produced by a prior runner's `artifacts`) and some lives on `ctx.<attr>` (stamped via `ctx.advance(..., kwarg=value)`). Notably `risk_tier` lives on `ctx.risk_tier` initially (CLASSIFY's advance-to-ROUTE) but gets mutated by GATE and threaded back via `pctx.risk_tier` artifact.

Factory convention: prefer `pctx.<slot>` if set (post-runner value), else fall back to `ctx.<attr>` (inline-path initial value).

## Key invariant discovered + pinned: dispatch_phase ≠ ctx.phase

Some runners (GENERATE especially) don't advance `ctx.phase` internally — their inline-path equivalents relied on the NEXT phase's body to advance with cross-phase kwargs (e.g. VALIDATE's inline advances `ctx.advance(VALIDATE, generation=generation)`). The dispatcher must track `dispatch_phase` separately from `ctx.phase` and use the runner's returned `next_phase` for the next factory lookup — NOT `ctx.phase`. Otherwise GENERATE's runner (which returns `next_phase=VALIDATE` but leaves ctx in GENERATE) would cause an infinite self-loop.

Pinned with `test_dispatcher_follows_next_phase_not_ctx_phase`.

## Terminal phases handling

COMPLETE is REGISTERED (COMPLETERunner does canary + oracle update + serpent stop + ledger). Only UNregistered terminal phases (CANCELLED / EXPIRED / POSTMORTEM — landed there via an early runner return) short-circuit without invoking a runner. This is pinned implicitly by the happy-path parity test (which exercises the COMPLETE-runner path end-to-end) and by `test_default_registry_covers_all_nine_extracted_phases`.

## Loud-fail contract (Manifesto §6 + §8)

Every infrastructure failure mode raises with descriptive messaging, caught by dedicated tests:

| Failure mode | Exception | Test |
|---|---|---|
| Registry miss | `PhaseRunnerRegistryError` | `test_dispatcher_registry_miss_raises` |
| Malformed runner (non-PhaseRunner return) | `PhaseDispatchError` | `test_dispatcher_malformed_runner_raises` |
| Malformed result (non-PhaseResult return) | `PhaseDispatchError` | `test_dispatcher_malformed_result_raises` |
| Factory unexpected exception | wrapped as `PhaseContextError` | `test_dispatcher_factory_exception_wraps_as_context_error` |
| Factory raises `PhaseContextError` | re-raised as-is | `test_dispatcher_factory_phase_context_error_passes_through` |
| Iteration cap exceeded (cycle) | `PhaseDispatchError` | `test_dispatcher_iteration_cap_raises` |
| Registry register invalid args | `PhaseRunnerRegistryError` | `test_registry_register_rejects_non_phase`, `..._non_callable` |
| Artifacts merge with non-Mapping | `PhaseContextError` | `test_phase_context_merge_artifacts_rejects_non_mapping` |

None of these leak silently. Any dispatcher exception bubbles out of `_run_pipeline` identical to the orchestrator's own unhandled exceptions.

## Parity proofs for 6a

Two dedicated parity tests drive a REAL `GovernedOrchestrator` through both paths:

- `test_dispatcher_parity_happy_path` — mock stack + mock generator + configured orchestrator; run through dispatcher-off baseline, capture final `ctx.phase` + `terminal_reason_code`; run dispatcher-on with same setup; assert identical. Pins the whole-pipeline parity invariant.
- `test_dispatcher_parity_cancel_terminal` — same setup but `is_cancel_requested=True` forces pre-APPLY cancel. This stresses cross-phase context threading because CLASSIFY has to stamp `advisory` + `consciousness_bridge` + `risk_tier` into `pctx` before Slice4b reads them for the cancel check. Both paths must land identical terminal state.

6b's test matrix will extend this to each of the 9 phases' distinct terminal paths.

## Bug discovered and fixed in-flight

`generate_runner.py` was missing two imports that the inline GENERATE code had:
- `LedgerEntry` from `ledger`
- `_dc_asdict` from `dataclasses`

These were referenced in the tool-execution-audit logging path. Inline parity tests hadn't exercised this path because the mock ledger's `append` was awaited at the top of the try, hitting a MagicMock-not-awaitable error before the `LedgerEntry` construction ran. Dispatcher parity test triggered the real code path (because the parity test orchestrator has a slightly different stack shape), exposing the missing imports.

Fixed: added `from dataclasses import asdict as _dc_asdict` and `from backend.core.ouroboros.governance.ledger import LedgerEntry, OperationState` to `generate_runner.py`.

## Authority invariant (grep-pinned)

`phase_dispatcher.py` imports only `op_context` + `phase_runner`. Factories import per-phase runners from the `phase_runners/` package. No `candidate_generator` / `iron_gate` / `change_engine` / `gate` / `policy` / `risk_tier` at module level. Pinned by `test_phase_dispatcher_bans_execution_authority_imports`.

## Graduation criteria

- ✅ Dispatcher infrastructure + 25 parity tests + regression green on both paths (228/228)
- ⬜ **6b**: deep per-phase terminal parity matrix lands
- ⬜ 3 clean battle-test sessions with `JARVIS_PHASE_RUNNER_DISPATCHER_ENABLED=true`
- ⬜ Flip `JARVIS_PHASE_RUNNER_DISPATCHER_ENABLED` default false→true
- ⬜ Per-phase flags graduate (Slices 1-5)
- ⬜ **Post-graduation maintenance** (separate PRs): delete inline `else:` blocks phase-by-phase as each phase flag graduates. Dispatcher becomes canonical; orchestrator `_run_pipeline` shrinks to the thin scheduler scope-doc envisioned.

## Wave 2 (5) arc status so far

| Slice | Scope | Lines | Tests |
|---|---|---|---|
| 1 | COMPLETE pilot | 60 | 22 |
| 2 | CLASSIFY | 762 | 22 |
| 3 | ROUTE + CTX + PLAN | 965 | 29 |
| 4a.1 | VALIDATE | 762 | 13 |
| 4a.2 | GATE | 600 | 21 |
| 4b | APPROVE + APPLY + VERIFY | 1,150 | 15 |
| 5a | GENERATE spine | 1,611 | 12 |
| 5b | Iron Gate depth | 0 | 24 |
| **6a** | **Dispatcher infrastructure** | **0 (new module)** | **25** |
| **TOTAL so far** | | **5,910** | **183** |

Wave 3 (concurrency items 6 + 7) remains blocked per prior directive until all Wave 2 (5) slices graduate.

## Next — Slice 6b

Same dispatcher module. Add per-phase terminal parity tests covering:
- CLASSIFY: emergency block, advisor block, risk BLOCKED
- ROUTE: (no terminals in route itself — simple pass-through)
- CONTEXT_EXPANSION: (expansion failure is swallowed — no terminal)
- PLAN: plan_required_unavailable, plan_review_unavailable, plan_rejected, plan_approval_expired, user_cancelled
- GENERATE: op_cost_cap_exceeded, no_forward_progress, stalled_productivity, is_noop → COMPLETE, Iron Gate failures
- VALIDATE: 7 terminals (budget, infra, L2 cancel/fatal, no_candidate, drift, read_only)
- GATE: 4 terminals (gate_blocked, security_review, user_rejected_5a, user_rejected_5b)
- APPROVE: 6 terminals (orange PR, no provider, expired, rejected, cancel, dry_run)
- APPLY/VERIFY: 5 terminals (change_engine_error/failed, infra_failed, human_active, verify_regression)

Target: 20+ new tests — one per terminal at minimum, with artifact-propagation edge cases where state threading is non-trivial. Match or exceed prior-slice discipline.
