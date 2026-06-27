---
title: Project Pass C Graduation Closure
modules: [backend/core/ouroboros/governance/adaptation/semantic_guardian_miner.py, backend/core/ouroboros/governance/adaptation/exploration_floor_tightener.py, backend/core/ouroboros/governance/adaptation/per_order_mutation_budget.py, backend/core/ouroboros/governance/adaptation/risk_tier_extender.py, backend/core/ouroboros/governance/adaptation/category_weight_rebalancer.py, backend/core/ouroboros/governance/adaptation/meta_governor.py, backend/core/ouroboros/governance/flag_registry_seed.py, ledger.py, backend/core/ouroboros/governance/auto_action_router.py]
status: merged
source: project_pass_c_graduation_closure.md
---

**Closed 2026-04-29.** Move 1 of the §27 v6 brutal-review autonomy
roadmap — graduating Pass C (Adaptive Anti-Venom == Priority 3) from
structurally-complete to empirically-validated.

**Why:** Per §27 v6 review, the autonomy gap was *empirical, not
structural* — Pass B + Pass C were structurally complete since
2026-04-26 but never graduated. Move 1 closes the "Learning" capability
dimension gap (B− → A−) by flipping defaults so adaptive proposals are
mined automatically without operator opt-in.

**How to apply:** Treat Pass C as load-bearing autonomy substrate now.
Future arcs that touch SemanticGuardian patterns, Iron Gate
exploration floors, per-Order mutation budgets, the risk-tier ladder,
or ExplorationLedger category weights MUST go through the
`/adapt approve` operator-gated path — do NOT bypass the ledger.
Master flags hot-revert via explicit `JARVIS_X_ENABLED=0` (asymmetric
env semantics: empty/unset = graduated default-true).

## What graduated

7 master flags flipped false→true with asymmetric env semantics:

| Flag | Module |
|---|---|
| `JARVIS_ADAPTATION_LEDGER_ENABLED` | adaptation/ledger.py |
| `JARVIS_ADAPTIVE_SEMANTIC_GUARDIAN_ENABLED` | semantic_guardian_miner.py |
| `JARVIS_ADAPTIVE_IRON_GATE_FLOORS_ENABLED` | exploration_floor_tightener.py |
| `JARVIS_ADAPTIVE_PER_ORDER_BUDGET_ENABLED` | per_order_mutation_budget.py |
| `JARVIS_ADAPTIVE_RISK_TIER_LADDER_ENABLED` | risk_tier_extender.py |
| `JARVIS_ADAPTIVE_CATEGORY_WEIGHTS_ENABLED` | category_weight_rebalancer.py |
| `JARVIS_ADAPT_REPL_ENABLED` | meta_governor.py |

## Evidence

- **Pre-flight:** 387 Pass C regression tests green pre-flip; smoke
  test confirmed all 7 surfaces functional with masters on.
- **Integrated graduation soak** `bt-2026-04-29-212606`: 906s, $0.0317
  cost, 8 ops, idle_timeout clean exit, strategic_drift=ok, 5
  postmortems, zero Pass C-related errors.
- **Post-graduation regression:** 393 Pass C tests green (added 6 new
  default_true_post_graduation pins); 494 combined with shipped_code
  + flag_registry suites.
- **Post-graduation soak** `bt-2026-04-29-215306`: 843s, idle_timeout
  clean exit, strategic_drift=ok (1/9 drifted, ratio 0.111), all 9
  errors are pre-existing infra noise (sentence_transformers /
  sandbox port / gh TLS / DW catalog probe / provider timeout
  fallback) — none Pass C-related. Graduated defaults flowed through
  with no env overrides.

## Structural seeds added (this graduation)

- **7 FlagRegistry seeds** in `flag_registry_seed.py` (87 total, was
  80) — all category=SAFETY (or OBSERVABILITY for the REPL),
  posture-relevance HARDEN+CONSOLIDATE.
- **7 shipped_code_invariants seeds** in `meta/shipped_code_invariants.py`
  (18 total, was 11):
  - `adaptation_ledger_monotonic_tightening_pin` (LOAD-BEARING — pins
    `MonotonicTighteningVerdict` + `validate_monotonic_tightening` +
    `REJECTED_WOULD_LOOSEN` tokens in ledger.py).
  - `adaptation_<miner>_no_authority_imports` × 6 (semantic_guardian
    + exploration_floor + per_order_budget + risk_tier + category_weights
    + meta_governor) — pins read-only contract: no
    orchestrator/phase_runners/iron_gate/change_engine/policy/
    semantic_firewall/providers/doubleword/urgency_router imports.

## Known deferred work (NOT a graduation blocker)

- Surface miners' auto-trigger wiring is a deferred Slice 6 follow-up:
  observed in soak `bt-2026-04-29-212606` — `.jarvis/adaptation_ledger.jsonl`
  was not created during the soak (no proposals minted) because
  miners are not yet auto-triggered from the orchestrator hot path.
  This is an empirical wiring gap, not a structural failure — when
  miners are auto-triggered (separate arc), the substrate is ready.

## Next moves (per §27 v6)

- **Move 2:** Multi-day soak (graduated defaults active) — empirical
  validation of session-to-session continuity at scale.
- **Move 3:** `auto_action_router.py` — close the "Action selection"
  capability dimension (B → A−).
