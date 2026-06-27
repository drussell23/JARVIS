---
title: Decision matrix (closed, first-match-wins) in `_slice23_should_activate_sentinel(provider_route)`
modules: [backend/core/ouroboros/governance/candidate_generator.py]
status: historical
source: project_slice_23_sentinel_activation.md
---

PR #59078 squash-merged 2026-05-26 at `a4c39772b8`. Branch `ouroboros/slice-23-autonomous-sentinel-activation`. Closes structural bottleneck surfaced by v16+v17: locking dispatch to single DW model when a 4-model trusted fleet sits in PromotionLedger.

# Decision matrix (closed, first-match-wins) in `_slice23_should_activate_sentinel(provider_route)`

| # | Condition | Verdict | Reason |
|---|---|---|---|
| 1 | `JARVIS_TOPOLOGY_SENTINEL_ENABLED=true` | ACTIVATE | `env_explicit_on` (legacy) |
| 2 | `JARVIS_TOPOLOGY_SENTINEL_ENABLED=false` | DO NOT | `env_explicit_off` (rollback wins) |
| 3 | `JARVIS_PROVIDER_CLAUDE_DISABLED=true` | ACTIVATE | `claude_disabled` (Slice 19a composition) |
| 4 | PromotionLedger ≥2 promoted for route | ACTIVATE | `multi_model_fleet` (autonomous) |
| 5 | Default | DO NOT | `default_off_phase10_contract` |

# What changed at the call site

`candidate_generator.py:1882-1885` env-only check → `_slice23_should_activate_sentinel(_provider_route)` helper call. Logs activation reason at INFO. AST pin bans legacy `os.environ.get("JARVIS_TOPOLOGY_SENTINEL_ENABLED")` pattern inside `_generate_dispatch` body.

# Composition discipline

- **No new env knobs** — uses existing `JARVIS_TOPOLOGY_SENTINEL_ENABLED` (Phase 10) + `JARVIS_PROVIDER_CLAUDE_DISABLED` (Slice 19a)
- **No new state** — composes `_trusted_seed_dw_models_for_route` (Slice 10B-ii)
- **No yaml changes**
- **Phase 10 contract preserved**: env-var DEFAULT still false; Slice 23 adds structural OVERRIDES on top. Test `test_master_flag_pin_blocks_premature_flip` + `test_master_flag_falsy[*]` all pass

# What this unlocks

Sentinel walker (`candidate_generator.py:2540`) iterates ranked DW fleet per route:
- STANDARD/COMPLEX: `[Qwen-397B, Qwen-35B, Kimi-K2.6]`
- BACKGROUND/SPECULATIVE: `[Qwen-397B, Qwen-35B, Kimi-K2.6, Qwen-4B]`

Composes with: Slice 22 IMMEDIATE→STANDARD demotion + Slice 20C drift rotation (rotation lives INSIDE the walker) + Slice 20B JSON heal + Slice 20D parser drift + Slice 21 supervisor containment.

**Healing matrix is structurally REACHABLE for the first time.**

# v17 forensic that motivated this

PromotionLedger had 4 promoted trusted models, dispatch only tried 397B (3 never-called). 397B server-errored, no fall-over, exhaustion. With Slice 23, the walker now tries 397B → falls to 35B (5× faster TTFT) → Kimi.

# Verification

12 tests (3 AST pins + 9 spine). 5 spine cover each decision branch. 4 defensive cover probe failure / threshold / precedence ordering (with spy probes confirming short-circuit). 80/80 regression across Slices 18c → 23.

# v18 status

Launched 2026-05-26T23:30:07Z, session `bt-2026-05-26-233010`, PID 91694. Awaiting first dispatches to confirm `Slice 23 sentinel activation: route=standard reason=claude_disabled — walking ranked DW fleet` log signature.

Related: [[project_slice_22_tier_decay]] (predecessor — demotes to STANDARD), [[project_slice_20bc_healing_rotation]] (the matrix Slice 23 unlocks), [[project_slice_20a_self_fallback]] (Claude isolation substrate), [[feedback_no_preresult_euphoria]] (v18 graduation bar: APPLY→VERIFY→RESOLVED, not "fleet walked").
