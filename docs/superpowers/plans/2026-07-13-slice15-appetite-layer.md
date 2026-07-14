# Slice 15 — The Appetite Layer: Semantic-Weight Value Scoring + Adaptive Routing Implementation Plan

> **For agentic workers:** superpowers:subagent-driven-development or executing-plans, task-by-task.

**Goal (operator verdict, Run #24 postmortem):** the execution pipeline is proven; the proactive layer is starved by its own cage. The only spontaneous ops are annotation-grade. Slice 15 makes semantic weight — a mathematical property of the work itself — the currency that buys routing tier, budget, and execution window, so real logic defects structurally outrank cosmetics and high-value work stops dying silently at approval walls. Sets up Run #25 (the Real-Work Trial: unstaged red tests in the real codebase).

## Operator-locked mandates (2026-07-13, verbatim intent)

1. **Root-Cause Only** — no keyword matching, no hardcoded priority lists. Intake scoring roots in AST impact + the verifiable presence of a failing test / executable-adjacent TODO. Logic defects mathematically outrank annotations by default.
2. **Architectural Purity** — adaptive allocation: budget, model tier, and window scale as FUNCTIONS of triage-computed semantic weight. No new hardcoded absolutes; coefficients env-tunable per house convention, every limit weight-derived.
3. **DRY** — extend the Slice-13 gate engine to EMIT weight (one engine: verdict = weight==0); wire into the existing dispatcher/router — zero duplicated triage or routing logic.
4. **Bulletproof** — high-value COMPLEX ops shift APPROVAL_REQUIRED → NOTIFY_APPLY **only under the red-test oracle** (attribution-resolved failing-test evidence + candidate-tree green: the test IS the objective judge — never a blind demotion); AST failure / indeterminate weight fails safe to BACKGROUND.

## Tasks

- [ ] **T1 — Dispatcher value-gate wiring (the Run-24 known-inert seam).** Hoist the Slice-13 seam body into `Orchestrator._maybe_complete_cosmetic_candidate(ctx, generation) -> Optional[ctx]`; legacy inline seam delegates; `dispatch_pipeline` invokes it at the GENERATE→VALIDATE transition (reading `PhaseContext.generation`). **RUNTIME-reachability test**: drive `dispatch_pipeline` itself with a stub registry (fake CLASSIFY/GENERATE runners emitting a cosmetic candidate) and assert the `no_op_cosmetic` terminal — never again a source-slice pin standing in for reachability (the T5 lesson's final form).
- [ ] **T2 — Weight engine.** `candidate_value_gate.file_semantic_weight(root, rel, new) -> Optional[int]`: Python = count of executable AST statements added/removed/changed (SequenceMatcher over per-statement docstring-stripped `ast.dump` sequences — deterministic, stdlib); line-grammar formats = changed residue lines; indeterminate = None. `classify_file_change` becomes a thin wrapper (weight==0 → COSMETIC) — one engine, verdicts unchanged (existing 24 pins must stay green). `candidate_semantic_weight(root, files)` aggregates (None-poisons-to-indeterminate, else sum).
- [ ] **T3 — Intake value scorer.** New `signal_value.py` reusing the T2 engine: score = f(verifiable failing-test evidence [attribution-resolved TestFailure = top band], executable-AST density of the target region [TODO adjacent to real statements ≫ comment-only files], annotation-class signals = floor). Wired into `UnifiedIntakeRouter` priority + `UrgencyRouter`: top band → COMPLEX/STANDARD escalation; floor/indeterminate → BACKGROUND (mandate-4 fail-safe). No keyword lists — the failing test and the AST are the only inputs.
- [ ] **T4 — Adaptive allocation + oracle-backed supervision shift.** Generation budget/timeout multipliers = weight-derived functions over existing route baselines (env-tunable coefficients, no new absolutes). GATE-side (BOTH gate paths — gate_runner is live): COMPLEX op + attribution-resolved failing-test oracle + candidate-tree green → APPROVAL_REQUIRED demotes to NOTIFY_APPLY with weight-scaled diff-preview delay; every other Orange stays Orange (the immutable guard is untouched for non-oracle ops).
- [ ] **T5 — Regression gate + ledger + Run #25 prep** (real red-test inventory as the trial worklist; attended session posture).

## Evidence anchors
- Run-24: zero `[ValueGate]` lines — `dispatch_pipeline` bypasses ALL legacy inline seams (phase_dispatcher.py:3298 comment: "legacy inline blocks below are never reached"); noise op escaped gating, died only by VERIFY-timeout luck.
- `PhaseContext.generation` slot (phase_dispatcher.py:266; VALIDATE factory consumes :421).
- Spontaneous-op inventory Run-21→24: requirements annotations, test-file nits, read-only — zero self-initiated logic repairs (the operator's core complaint).
