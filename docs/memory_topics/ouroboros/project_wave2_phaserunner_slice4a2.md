---
title: Wave 2 (5) Slice 4a.2 — Result
modules: [orchestrator.py, tests/governance/phase_runner/test_gate_runner_parity.py]
status: historical
source: project_wave2_phaserunner_slice4a2.md
---

# Wave 2 (5) Slice 4a.2 — Result

**Status:** implementation complete, parity + regression green, flag stays **default off**.

## What landed

| Artifact | Path | Notes |
|---|---|---|
| GATERunner | `phase_runners/gate_runner.py` | Verbatim 5496-6098; constructor takes `(orch, serpent, best_candidate, risk_tier)` |
| Flag helper | `orchestrator.py` `_phase_runner_gate_extracted()` | `JARVIS_PHASE_RUNNER_GATE_EXTRACTED` default false |
| Delegation hook | `orchestrator.py` ~line 5513 | 600-line inline block wrapped in `else:` branch |
| Parity tests | `test_gate_runner_parity.py` | 21 tests covering 4 terminal paths + 11 sub-gates + branch ordering + artifact threading |

## Parity test outcomes

**Both paths, both suites — 152/152 green:**

- flag=false → 152 passed
- all-extraction-flags=true → 152 passed

## Explicit sub-gate coverage (per operator directive)

Every sub-gate the operator named has at least one dedicated test:

| Sub-gate | Test(s) |
|---|---|
| `can_write` policy check | `test_can_write_denied_terminates` |
| SecurityReviewer | `test_security_reviewer_exception_swallowed` (observability preserves) |
| SimilarityGate | `test_similarity_gate_escalates_to_approval` |
| SemanticGuardian | `test_semantic_guardian_clean_still_emits_log` + `test_branch_ordering_semantic_guardian_after_risk_ceiling` |
| MutationGate | `test_mutation_gate_enforce_mode_upgrades` |
| MIN_RISK_TIER floor | `test_min_risk_tier_floor_escalates` + `test_paranoia_mode_escalates` |
| frozen_autonomy_tier | `test_frozen_tier_observe_escalates_to_approval` |
| JARVIS_RISK_CEILING | `test_risk_ceiling_escalates_below_floor` + `test_risk_ceiling_does_not_downgrade` |
| Phase 5a green preview | `test_5a_green_preview_cancel_terminates` + `test_5a_green_preview_no_cancel_continues` |
| Phase 5b NOTIFY_APPLY | `test_5b_notify_apply_cancel_terminates` + `test_5b_notify_apply_no_cancel_continues` + `test_5b_emits_notify_apply_decision` |

## Branch ordering parity

One test explicitly pins the inline-block branch ordering:
`test_branch_ordering_semantic_guardian_after_risk_ceiling` — with
`JARVIS_RISK_CEILING=NOTIFY_APPLY` set AND SemanticGuardian returning a
hard finding, the final risk tier must be APPROVAL_REQUIRED (strictest
wins), proving the ordering `RISK_CEILING → SemanticGuardian` is
preserved. If someone reorders those in future, this test fails loudly.

## Four terminal exit paths, all covered

1. `gate_blocked:<reason>` — can_write denied (BLOCKED ledger state)
2. `security_review_blocked` — SecurityReviewer BLOCK verdict
3. `user_rejected_safe_auto_preview` — 5a green preview cancel
4. `user_rejected_notify_apply` — 5b yellow preview cancel

## Cross-phase artifact — `risk_tier` (mutation site inventory)

GATE mutates `risk_tier` at **6 sites**, all preserved verbatim:

1. SimilarityGate escalation (`risk_tier = RiskTier.APPROVAL_REQUIRED`)
2. frozen_tier=observe (`risk_tier = RiskTier.APPROVAL_REQUIRED`)
3. JARVIS_RISK_CEILING env floor (`risk_tier = _floor`)
4. SemanticGuardian tier upgrade (`risk_tier = _upgrade` — NOTIFY_APPLY or APPROVAL_REQUIRED)
5. MutationGate enforce-mode decision (`risk_tier = RiskTier.BLOCKED` or `APPROVAL_REQUIRED`)
6. MIN_RISK_TIER floor (`risk_tier = _tgt` — paranoia / quiet hours composed)

All threaded back via `PhaseResult.artifacts["risk_tier"]`. Orchestrator
hook rebinds the local before APPROVE inline code reads it.

## §8 observability parity

- `[SemanticGuard]` structured log fires on every op (hit OR clean).
  Test `test_semantic_guardian_clean_still_emits_log` pins this contract.
- `[MutationGate]` structured log fires when gate runs. Preserved.
- `emit_decision(outcome="notify_apply", ...)` fires on 5b entry. Test
  `test_5b_emits_notify_apply_decision` pins this.
- `emit_decision(outcome="escalated", reason_code="similarity_escalation", ...)`
  fires on SimilarityGate escalation. Preserved.

## Operator-visible affordance — `_human_is_watching`

Resolved through the orchestrator module namespace so
`JARVIS_DIFF_PREVIEW_ALL` env override still works identically. Test
`test_5a_green_preview_cancel_terminates` uses this env var to force
the human-is-watching code path in headless pytest.

## Authority invariant (grep-pinned)

Runner imports: `ledger.OperationState`, `op_context.*`, `phase_runner.*`,
`risk_engine.RiskTier`, plus function-local imports matching inline
(`security_reviewer`, `similarity_gate`, `semantic_guardian`,
`mutation_gate`, `risk_tier_floor`, `diff_preview`). Test
`test_gate_runner_bans_execution_authority_imports` grep-pins the ban.

## Graduation criteria

- ✅ Runner + 21 parity tests + regression green on both paths
- ⬜ 3 clean battle-test sessions with flag=true
- ⬜ Flip `JARVIS_PHASE_RUNNER_GATE_EXTRACTED` default false→true
- ⬜ Post-slice-6: delete inline GATE block

## Combined Slice 4a (4a.1 + 4a.2) totals

**1,362 lines extracted across two sub-slices. 34 parity tests. 152/152 green.**

## Next

**4b**: APPROVE + APPLY + VERIFY (~1135 lines). Mutation-adjacent
phases. Same contract: verbatim + parity oracle + regression + §8
audit signal preservation + rollback story intact.
