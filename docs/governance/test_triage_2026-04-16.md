# Governance test suite — triage (2026-04-16)

**Status**: 46 pre-existing failures across `tests/governance/` — unrelated
to the `SemanticGuardian` / `risk_tier_floor` / orchestrator wiring shipped
this session. Documenting them here so drift doesn't become normal
(Manifesto §8 — auditability).

**Do not ship a blanket `xfail` / `skip` pass**. Each module needs its own
triage PR with a diagnosis in the commit message: "was this always broken?",
"did a recent refactor regress it?", "is the assertion wrong or the code
wrong?". Batch-skipping erases the signal.

## Mainline green path — what we're *actually* testing on every feature ship

```bash
python3 -m pytest \
    tests/battle_test/ \
    tests/governance/test_last_session_summary.py \
    tests/governance/test_last_session_summary_v1_1a.py \
    tests/governance/test_last_session_summary_composition.py \
    tests/governance/test_semantic_guardian.py \
    tests/governance/test_risk_tier_floor.py
```

As of 2026-04-16: 357+ tests green on this path. Every new feature must
keep this green AND add its own test file to the list.

## Failing modules — full list (2026-04-16 run)

| Count | Module |
|---|---|
| 6× | `tests/governance/integration/test_phase2b_acceptance.py` |
| 6× | `tests/governance/integration/test_validate_pipeline_acceptance.py` |
| 4× | `tests/governance/integration/test_phase2c_acceptance.py` |
| 3× | `tests/governance/self_dev/test_candidate_ledger.py` |
| 3× | `tests/governance/self_dev/test_e2e.py` |
| 2× | `tests/governance/intake/test_crash_recovery.py` |
| 2× | `tests/governance/intake/test_out_of_order_events.py` |
| 2× | `tests/governance/intake/test_unified_intake_router.py` |
| 2× | `tests/governance/integration/test_governed_loop_startup.py` |
| 2× | `tests/governance/self_dev/test_prompt_enrichment.py` |
| 2× | `tests/governance/self_dev/test_source_drift.py` |
| 2× | `tests/governance/self_dev/test_validate_phase.py` |
| 2× | `tests/governance/test_codegen_context.py` |
| 2× | `tests/governance/test_graduation_orchestrator.py` |
| 1× | `tests/governance/intake/sensors/test_backlog_sensor.py` |
| 1× | `tests/governance/integration/test_phase2c2_acceptance.py` |
| 1× | `tests/governance/multi_repo/test_registry.py` |
| 1× | `tests/governance/saga/test_saga_types.py` |
| 1× | `tests/governance/self_dev/test_candidate_parser.py` |
| 1× | `tests/governance/self_dev/test_pipeline_deadline.py` |

**Total: 46 failures across 20 distinct files.**

## Why these are NOT resolved by `SemanticGuardian`

The guardian ships new behavior (pre-APPLY pattern detection) + its own
82-test regression spine. It does not touch:

- Integration phases `2b`, `2c`, `2c2` acceptance contracts
- Self-dev e2e / candidate-ledger / prompt-enrichment paths
- Intake crash recovery / out-of-order event handling
- Graduation-orchestrator ephemeral-usage tracker
- Codegen-context truncation heuristics
- Multi-repo registry / saga type contracts

These failures existed *before* the guardian work and will continue to
exist *after*. Confirmed via spot-check:
`test_codegen_context.py::test_prompt_no_truncation_32kb_file` fails
with `AssertionError: 32KB file should not be truncated with 64KB budget`
— a prompt-length heuristic in `codegen_context.py`, no shared surface
with the guardian.

## Proposed handling (per-module, separate PRs)

For each failing module:

1. **Reproduce in isolation** —
   `python3 -m pytest <module> -v --tb=long` — capture the exact failure.
2. **Classify** —
   - **Environment-dependent**: missing optional dependency, OS-specific
     path, test fixture assuming a specific working directory →
     `@pytest.mark.skipif(...)` with a `reason` citing this doc.
   - **Genuine regression**: mainline code changed, assertion is right →
     fix the code.
   - **Stale assertion**: requirements changed, test wasn't updated →
     fix the test.
   - **Flaky / timing-dependent**: retry logic, timeouts, order-sensitive →
     quarantine with `@pytest.mark.flaky(reruns=3)` or `@pytest.mark.xfail(strict=True)`
     and file a ticket.
3. **Commit with diagnosis** — one module per PR, commit message explains
   which of the above + a link back to this doc.

Do **not**:
- Bulk-xfail the entire list (erases signal).
- Silently delete tests (erases intent).
- Leave this doc unchanged after a PR lands — remove the row when fixed.

## Reframing the original audit question (for `CLAUDE.md` / `OUROBOROS.md`)

`SemanticGuardian` + tier floor answers:
> *"Would a syntactically-valid semantic atrocity auto-apply?"*
>
> For the 10 pattern classes, usually no: hard detections force
> APPROVAL_REQUIRED, soft detections force NOTIFY_APPLY.

It does **not** answer:
> *"Is the patch logically correct?"*
>
> That remains the job of VALIDATE + Iron Gate + exploration discipline
> + the user's own test suite. Manifesto §1 (Boundary Principle):
> deterministic pattern checks raise friction, they don't replace proof.

## Track A follow-up (V2 — not this commit)

When a bad change slips past the guardian in production:

1. Capture the (old, new) pair as a fixture in
   `tests/governance/test_semantic_guardian.py`.
2. Add a new pattern detector (if the class is generalisable) OR extend
   an existing detector (if it's a missed edge case).
3. Assert the fixture fires on the new/extended pattern — the regression
   test name itself becomes the postmortem reference.

This closed-loop "real miss → fixture + pattern" is the only honest path
to expanding guardian coverage. Don't stack heuristics upfront; ship,
observe a week of `[SemanticGuard]` telemetry lines, then expand from
evidence.
