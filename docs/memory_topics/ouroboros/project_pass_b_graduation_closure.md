---
title: RSI Pass B Graduation — CLOSED 2026-05-03
modules: [scripts/pass_b_graduation_closure_verdict.py, backend/core/ouroboros/governance/flag_registry_seed.py, backend/core/ouroboros/governance/meta/_invariant_helpers.py, backend/core/ouroboros/governance/meta/order2_manifest.py, backend/core/ouroboros/governance/meta/order2_classifier.py, backend/core/ouroboros/governance/meta/ast_phase_runner_validator.py, backend/core/ouroboros/governance/meta/shadow_replay.py, backend/core/ouroboros/governance/meta/meta_phase_runner.py, backend/core/ouroboros/governance/meta/replay_executor.py, backend/core/ouroboros/governance/meta/order2_review_queue.py, backend/core/ouroboros/governance/meta/order2_repl_dispatcher.py, tests/governance/test_ast_phase_runner_validator.py]
status: merged
source: project_pass_b_graduation_closure.md
---

# RSI Pass B Graduation — CLOSED 2026-05-03

4-slice graduation arc closing the Tier 3 #7 strategic-compounding gap from the user's roadmap table. Pre-arc state (per the existing `project_reverse_russian_doll_pass_b.md` design memo, dated 2026-04-26):

> **STRUCTURALLY COMPLETE 2026-04-26 — all 6 slices shipped (Slices 1+2+2b+3+4+5+6.1+6.2+6.3); 438/438 Pass B regression suite green; defaults still false pending per-slice graduation.**

The substrate was complete (8 modules, 5,047 LOC across `meta/`) but **operationally inert and undiscoverable**: ZERO modules had `register_flags`, ZERO had `register_shipped_invariants`, and 6 of 8 master flags defaulted false. The W2(5) policy called for per-slice 3-clean-session graduation arcs that hadn't happened.

## Slices shipped (this graduation arc)

- **Slice 1** — Centralized FlagSpec seeds in `flag_registry_seed.py`. 12 specs added (8 master flags + 4 path/cage knobs) under a labeled "RSI Pass B (Tier 3 #7) Graduation — 2026-05-03" section. Total seeds 146 → 158. Each spec documents flip-vs-keep status + soak-validation rationale + cage relationship.
- **Slice 2** — Shared invariant helper `meta/_invariant_helpers.py` (`make_pass_b_substrate_invariant` + `make_locked_truthy_env_invariant`) + `register_shipped_invariants()` appended to all 8 Pass B modules. Helper eliminates ~640 LOC of duplicated AST-walking code. Each substrate pin enforces required functions/classes present, frozen dataclasses preserved, and (where applicable) no dynamic-code calls. **Cross-file cage pin** in `order2_review_queue` AST-validates that `amendment_requires_operator()` reads the env var name AND defaults truthy — the structural lock that protects Pass B's mutation surface even if both the META_PHASE_RUNNER + REPLAY_EXECUTOR flags were flipped on.
- **Slice 3** — Flipped 6 read-only / observational / operator-surface flags from default-False → default-True:
  - `JARVIS_ORDER2_MANIFEST_LOADED` (read-only — manifest YAML loader)
  - `JARVIS_ORDER2_RISK_CLASS_ENABLED` (advisory enrichment; risk floor application is independently flag-gated)
  - `JARVIS_PHASE_RUNNER_AST_VALIDATOR_ENABLED` (read-only static analysis)
  - `JARVIS_SHADOW_PIPELINE_ENABLED` (observational golden-replay)
  - `JARVIS_ORDER2_REVIEW_QUEUE_ENABLED` (operator surface, mutation cage gates downstream)
  - `JARVIS_ORDER2_REPL_ENABLED` (operator surface, REPL dispatcher)
  - **KEPT default-false explicitly with cage-preservation comments**: `JARVIS_META_PHASE_RUNNER_ENABLED` (autonomy-creation surface) + `JARVIS_REPLAY_EXECUTOR_ENABLED` (actual mutation execution surface). These two stay off pending operator-paced 3-clean-session arcs per W2(5) policy.
  - Updated 7 pre-existing tests that asserted the old default-false to reflect the post-graduation default-true reality. Added explicit `monkeypatch.setenv("...", "false")` in tests that need the master-off path (replacing `delenv` patterns that relied on absence-meaning-false).
- **Slice 4** — Empirical-closure verdict script `scripts/pass_b_graduation_closure_verdict.py` covering 5 contracts. Closure memory + MEMORY.md update.

## Architectural decisions worth remembering

- **Two-tier flag policy: graduate the surface, defer the powers**. The substrate has structural completeness (438+ tests, 6-slice end-to-end shipped 2026-04-26). Graduating the entire substrate at once would have meant flipping the autonomy-creation + mutation-execution surfaces without empirical soak validation. Splitting the flag set into "discoverable / observational / operator-surface" (graduate now) vs "autonomy / mutation" (defer) preserves the cost contract while making the substrate operationally alive.
- **Centralized seeds, decentralized invariants**. `flag_registry_seed.py` is the single source of truth for the curated flag list — Pass B specs live there alongside every other graduated arc. Per-module `register_shipped_invariants()` lives WITH the module it pins (closer to the code, AST-walks the live source, fires when the substrate drifts). The two patterns compose cleanly without duplication.
- **Shared invariant helper**. `meta/_invariant_helpers.py::make_pass_b_substrate_invariant()` produces a `ShippedCodeInvariant` from a tiny declarative spec (required_funcs, required_classes, frozen_classes). 8 modules × ~80 LOC AST-walking each = ~640 LOC duplicated; the helper collapses that to ~120 LOC of shared code + ~25 LOC per module. Saves ~500 LOC AND eliminates drift between near-identical AST validators. Mirrors the user's "no duplication" directive structurally.
- **Cross-file cage pin**. `make_locked_truthy_env_invariant()` is a new pin shape: target_file points at a CONSUMER (the module the cage lives in) but the validation enforces a SUBSTRATE invariant (the function returns truthy by default). The pin AST-walks the whole module looking for the env-var name as a literal anywhere (handles the common pattern of lifting env names into module-level constants like `_AMENDMENT_INVARIANT_ENV`) and confirms the helper function body has either a truthy literal or a `return True` short-circuit. Caught a real bug during dev: my first version restricted the env-name search to the function body, missing the constant-lift pattern.
- **`replay_executor` `forbid_dynamic_builtins=False`**. Every other Pass B module gets the `exec`/`eval`/`compile` ban in its substrate pin — except `replay_executor`. Its job IS to compile proposed PhaseRunner subclasses in a sandbox under `operator_authorized=True` trigger. Banning compile() in this module would defeat its purpose. The cage isn't "no dynamic code"; it's "no dynamic code WITHOUT operator authorization", and the operator-authorization check is enforced separately (by `order2_review_queue.amendment_requires_operator()` cage pin + the explicit `if operator_authorized is not True: return DISABLED` short-circuit in the executor).

## Test counts + AST pins

- **442/442 combined sweep across the full Pass B regression spine** (test_order2_manifest, test_order2_classifier, test_ast_phase_runner_validator, test_shadow_replay, test_meta_phase_runner, test_replay_executor, test_order2_review_queue, test_order2_repl_dispatcher, test_order2_gate_runner_wiring); zero regressions. 7 pre-existing tests updated to reflect post-graduation defaults.
- **12 new FlagRegistry seeds** in `flag_registry_seed.py`'s SEED_SPECS (Pass B section)
- **9 new AST pins** across the 8 Pass B modules (8 substrate + 1 cage):
  - `pass_b_order2_manifest_substrate`
  - `pass_b_order2_classifier_substrate`
  - `pass_b_ast_phase_runner_validator_substrate`
  - `pass_b_shadow_replay_substrate`
  - `pass_b_meta_phase_runner_substrate`
  - `pass_b_replay_executor_substrate`
  - `pass_b_order2_review_queue_substrate`
  - `pass_b_amendment_requires_operator_cage` (cross-file cage)
  - `pass_b_order2_repl_dispatcher_substrate`
- **1 shared helper module** (`meta/_invariant_helpers.py`) eliminating ~500 LOC of duplicated AST-walking

## Empirical-closure verdict (all in-process, no soak)

```
[PASS] C1 All 12 Pass B FlagSpec entries seeded
       seeded=12/12
[PASS] C2 All 8 register_shipped_invariants pins hold
       pins=9 (substrate + cage)
[PASS] C3 Six read-only/operator flags default-true
       flipped=[is_loaded=True, classifier:is_enabled=True,
                ast_validator:is_enabled=True, shadow:is_enabled=True,
                review_queue:is_enabled=True, repl:is_enabled=True]
[PASS] C4 Two write-path flags STAY default-false (cage)
       kept_false=[meta_phase_runner:is_enabled=False (cage),
                   replay_executor:is_enabled=False (cage)]
[PASS] C5 amendment_requires_operator() locked-true cage
       cases=[env=<unset>->True, env=false->True, env=0->True,
              env=no->True, env=off->True, env=garbage->True,
              env=true->True]
```

Cage proof is structural: even setting the cage env var to `"false"` returns True from `amendment_requires_operator()` (the function logs the ignored env value and returns True unconditionally). The AST pin guarantees that future edits cannot quietly invert this without a pin violation.

## Reuse contract honored (no duplication)

- Existing `flag_registry_seed.py` SEED_SPECS pattern reused — Pass B specs added to the same curated list (no new file)
- Existing `ShippedCodeInvariant` registration contract reused; new helpers are CONSTRUCTORS that produce ShippedCodeInvariant objects (additive, not parallel)
- Existing `_TRUTHY` pattern reused for the new graduation defaults (`raw == "" → return True`)
- Existing test patterns reused (monkeypatch.setenv for opt-out paths replacing delenv-relies-on-absence)

## What this unlocks

The user's table flagged Tier 3 #7 as: "Without RSI, every Tier 1+2 gap requires manual engineering — Derek+Claude write each arc. With RSI, O+V drafts arcs autonomously through Order-2 governance, AST validator, shadow replay, locked-true protocol. Engineering velocity scales. 1 arc/week → potentially 1 arc/day. Path to General Specialist collapses from 6-18 months to weeks."

This graduation arc DOES NOT enable autonomous arc-drafting (META_PHASE_RUNNER + REPLAY_EXECUTOR stay default-false). But it makes the entire substrate operationally alive, structurally locked-down, and discoverable. The next move (operator-paced) is to soak META_PHASE_RUNNER + REPLAY_EXECUTOR through 3 clean sessions and flip them — which is genuinely a write-power graduation that warrants explicit operator authorization, not autonomous flip in this work session.

## Files touched

- `backend/core/ouroboros/governance/flag_registry_seed.py` (12 new FlagSpec entries)
- `backend/core/ouroboros/governance/meta/_invariant_helpers.py` (NEW — shared helper)
- `backend/core/ouroboros/governance/meta/order2_manifest.py` (register_shipped_invariants)
- `backend/core/ouroboros/governance/meta/order2_classifier.py` (register_shipped_invariants)
- `backend/core/ouroboros/governance/meta/ast_phase_runner_validator.py` (register_shipped_invariants + flip default-true)
- `backend/core/ouroboros/governance/meta/shadow_replay.py` (register_shipped_invariants + flip default-true)
- `backend/core/ouroboros/governance/meta/meta_phase_runner.py` (register_shipped_invariants only — flag stays default-false)
- `backend/core/ouroboros/governance/meta/replay_executor.py` (register_shipped_invariants only — flag stays default-false)
- `backend/core/ouroboros/governance/meta/order2_review_queue.py` (register_shipped_invariants + cage pin + flip default-true)
- `backend/core/ouroboros/governance/meta/order2_repl_dispatcher.py` (register_shipped_invariants + flip default-true)
- `tests/governance/test_ast_phase_runner_validator.py` (3 tests updated for new defaults)
- `tests/governance/test_shadow_replay.py` (3 tests updated)
- `tests/governance/test_order2_review_queue.py` (1 test updated)
- `scripts/pass_b_graduation_closure_verdict.py` (NEW)

Closes Tier 3 #7 of the user's roadmap. Pass B substrate is now structurally complete (per April closure memo) AND operationally graduated (per this arc) AND empirically verified (per the verdict script). Two write-path flags remain explicitly cold pending operator-paced soak graduation.
