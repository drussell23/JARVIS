---
title: Wave 2 (5) Slice 6b — Result
modules: [tests/governance/phase_runner/test_phase_dispatcher_terminals.py, backend/core/ouroboros/governance/phase_dispatcher.py]
status: historical
source: project_wave2_phaserunner_slice6b.md
---

# Wave 2 (5) Slice 6b — Result

**Status:** dispatcher terminal matrix complete. **Slice 6 CLOSED.** **Wave 2 (5) CLOSED.** Runner + dispatcher code unchanged since 6a — this slice is parity tests only. 248/248 green on both paths.

## What landed

| Artifact | Path | Notes |
|---|---|---|
| Terminal matrix | `tests/governance/phase_runner/test_phase_dispatcher_terminals.py` | 20 tests, per-phase terminals + artifact propagation edges |
| Dispatcher (unchanged) | `phase_dispatcher.py` | No code diff since 6a |

## 20 tests across phase-terminal + artifact-threading categories

Every test follows the `_run_both_paths(...)` harness: build the same setup twice (dispatcher-off baseline + dispatcher-on), capture both final `ctx.phase` + `terminal_reason_code`, assert parity via `_assert_terminal_parity`.

### CLASSIFY terminals (1)
- `test_classify_risk_blocked_terminal` — RiskEngine returns BLOCKED → CANCELLED

### GATE terminals (1)
- `test_gate_can_write_denied_terminal` — `can_write` denies → `gate_blocked:<reason>` CANCELLED

### APPROVE terminals (3)
- `test_approve_no_provider_when_required` — APPROVAL_REQUIRED + provider=None → `approval_required_but_no_provider`
- `test_approve_rejected_terminal` — APPROVAL_REQUIRED + REJECTED decision → `approval_rejected` CANCELLED
- `test_approve_expired_terminal` — APPROVAL_REQUIRED + EXPIRED decision → `approval_expired` EXPIRED

### APPLY / VERIFY terminals (3)
- `test_apply_dry_run_terminal` — `JARVIS_DRY_RUN=1` → `dry_run_session` CANCELLED
- `test_apply_change_engine_failed_terminal` — ChangeEngine success=False → POSTMORTEM
- `test_apply_change_engine_exception_terminal` — ChangeEngine raises → `change_engine_error` POSTMORTEM

### GENERATE / VALIDATE terminals (2)
- `test_generate_no_candidates_returned` — empty candidates across retries → terminal
- `test_generate_is_noop_completes` — `is_noop=True` → advance COMPLETE with `terminal_reason=noop`

### Cross-phase artifact propagation edges (3)
- `test_artifact_risk_tier_mutation_gate_to_approve` — `JARVIS_MIN_RISK_TIER=approval_required` forces SAFE_AUTO→APPROVAL_REQUIRED at GATE; APPROVE must see the mutated value to hit `approval_required_but_no_provider`. Pins `pctx.risk_tier` threading GATE→Slice4b.
- `test_artifact_generation_threads_classify_to_validate` — happy path reaching COMPLETE/POSTMORTEM confirms `generation` threaded through GENERATE→VALIDATE factory without raising PhaseContextError.
- `test_artifact_t_apply_threads_apply_to_complete` — happy path to COMPLETE confirms `t_apply` reached COMPLETERunner for canary latency.

### Cancellation paths (1)
- `test_pre_apply_user_cancel_terminal` — `is_cancel_requested=True` pre-APPLY → `user_cancelled` CANCELLED (re-pin of 6a's spine coverage in the matrix for completeness).

### Multi-phase chained terminals (1)
- `test_generator_exception_retry_exhaustion` — generator raises every call → retries exhaust → terminal.

### Risk-tier escalation chain (2)
- `test_notify_apply_path_parity` — NOTIFY_APPLY tier + 5b preview + no cancel → proceeds through. Pins dispatcher routing through GATE's 5b block.
- `test_notify_apply_cancel_during_preview` — NOTIFY_APPLY + cancel during preview → `user_rejected_notify_apply`.

### Risk-engine knob parity (1)
- `test_policy_engine_override_to_blocked` — `JARVIS_RISK_CEILING=APPROVAL_REQUIRED` escalation + no provider. Verifies the env knob produces identical terminal state through dispatcher.

### Dispatcher consistency (1)
- `test_dispatcher_on_is_deterministic` — two back-to-back runs of the same setup through dispatcher-on produce same final `ctx.phase`. Pins that `PhaseContext` state doesn't leak across ops.

### Authority invariant (1)
- `test_terminal_matrix_bans_execution_authority_imports` — test module grep-pinned: no `candidate_generator` / `iron_gate` / `change_engine` imports.

## Parity harness design

Central `_run_both_paths(monkeypatch, build_stack, build_generator, ...)` helper returns `(off_result, on_result)`. Each test constructs the orchestrator twice (once with dispatcher flag unset, once with flag=true) using the same builder closures so state is identical. Then:

```python
_assert_terminal_parity(off_result, on_result)
# asserts on.phase is off.phase and on.terminal_reason_code == off.terminal_reason_code
```

Any dispatcher-driven divergence (wrong phase, wrong terminal reason) trips the assert with a descriptive message. This is the strongest form of parity proof short of diffing the full ctx hash chain.

## What this slice proves

1. **Every terminal exit path Slices 1-5 extracted produces identical observable state under the dispatcher.**
2. **Cross-phase context threading (advisory, consciousness_bridge, risk_tier, best_candidate, generation, episodic_memory, generate_retries_remaining, t_apply) survives the factory-per-phase handoff pattern at every phase boundary where it matters.**
3. **Dispatcher state is per-op** — two back-to-back runs don't cross-pollinate `PhaseContext` because each `dispatch_pipeline` call creates a fresh `PhaseContext`.

## Slice 6 arc closure

| Sub-slice | Scope | Tests |
|---|---|---|
| 6a | Registry + Context + dispatcher infra + spine parity | 25 |
| 6b | Per-phase terminal matrix + artifact propagation | 20 |
| **Slice 6 TOTAL** | | **45** |

## Wave 2 (5) FULL ARC CLOSED

| Slice | Scope | Lines | Parity tests |
|---|---|---|---|
| 1 | COMPLETE pilot | 60 | 22 |
| 2 | CLASSIFY | 762 | 22 |
| 3 | ROUTE + CTX + PLAN | 965 | 29 |
| 4a.1 | VALIDATE | 762 | 13 |
| 4a.2 | GATE | 600 | 21 |
| 4b | APPROVE + APPLY + VERIFY | 1,150 | 15 |
| 5a | GENERATE spine | 1,611 | 12 |
| 5b | GENERATE Iron Gate depth | 0 | 24 |
| 6a | Dispatcher infrastructure | 0 | 25 |
| 6b | Dispatcher terminal matrix | 0 | 20 |
| **TOTAL** | | **5,910** | **203** |

**248/248 green on both paths** (45 orchestrator + 203 phase_runner).

All flags default `false`. Graduation per-slice requires 3 clean battle-test sessions before flipping the flag. Once all graduate:
- Per-phase flag deletions (separate maintenance PRs): delete inline `else:` blocks. Orchestrator `_run_pipeline` shrinks to the thin scheduler scope-doc envisioned.
- Dispatcher becomes canonical.

**Wave 3 (items 6+7 concurrency) now unblocked** per scope-doc sequencing — pending operator authorization and per-slice graduation of Wave 2 (5) flags.

## Authority invariants (grep-pinned across all 10 slice files)

All runners + dispatcher verified: no imports of `candidate_generator` / `iron_gate` / `change_engine` / `gate` / `policy` / `risk_tier` at module level. `change_engine` accessed via `orch._stack.change_engine` identical to inline path.

## Next — scope-doc sequencing options

Per operator directive, Wave 3 was blocked until Wave 2 (5) closure. With Slice 6b merged:

1. **Per-slice graduation** — 3 clean battle-test sessions per flag, starting with lowest-risk (COMPLETE → CLASSIFY → ROUTE/CTX/PLAN → VALIDATE → GATE → APPROVE+APPLY+VERIFY → GENERATE → DISPATCHER). Each graduation flip is a one-line env default change + graduation session commit.
2. **Inline-block deletion PRs** — one per phase once its flag graduates.
3. **Wave 3 items 6+7 (concurrency)** — now eligible for authorization.

Operator chooses the next arc. Recommend starting a graduation battle-test cadence before opening Wave 3 work.
