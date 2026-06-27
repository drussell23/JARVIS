---
title: Project Slice201 Bandit Router
modules: []
status: historical
source: project_slice201_bandit_router.md
---

**Slice 201 — Contextual Bandit Routing Advisor (MERGED #69441, main `963cf671d7`, 2026-06-10).** Phase 3 of the authorized "Strategic Horizon" plan; Phases 2+4 (roadmap/ledger) deferred — operator-blocked (see below).

**How to apply:** `bandit_router.py` — Thompson Sampling over per-arm Beta(α,β) posteriors. `advise(ranked_models)` samples each arm → returns list REORDERED best-first; `record_outcome(model_id, success, cost_usd, latency_s)` folds `compute_reward` ([0,1]) into posterior (α+=r, β+=1-r). Reward=(Success·Ws−Cost·Wc−Latency·Wl) mapped [0,1], env-tunable `JARVIS_BANDIT_W_{SUCCESS,COST,LATENCY}` + `_{COST,LATENCY}_SCALE`; unknown cost/latency=0 penalty. Durable `.jarvis/bandit_router_state.json`. **STRUCTURAL fail-closed: advisor's input domain IS `ranked_models` (= brain_selection_policy active set via `topology.dw_models_for_route`), so it can ONLY reorder policy-permitted arms, NEVER select out-of-policy.** Wiring guards `set(order)==set(ranked_models)`. `_GatedBanditRouter` singleton: advise() hard no-op unless `JARVIS_BANDIT_ROUTER_ENABLED` (default-FALSE). Wired in candidate_generator: advise() before sentinel walk (~line 3199); record_outcome(True) at report_success (~3466), record_outcome(False) at per-model failure (~3653). compose enables it. 17 tests; 266 regression. KNOWN pre-existing main failures (NOT this slice): `test_topology_sentinel_{dispatch,preflight}` 2 stale `_topology.dw_allowed_for_route` source-pins fail on clean main.

**ROADMAP BLOCKER (the real strategic-vision next step, operator-gated):** RoadmapReader reads `.jarvis/roadmap.yaml` (HMAC-SHA256 via `JARVIS_ROADMAP_READER_HMAC_SECRET`, with `goals:` list) — FILE DOES NOT EXIST. The `.jarvis/roadmap.signed.yaml` present is the DIFFERENT Layer-4 Ed25519 scope-auth roadmap (authorized_scopes/budget/expires, currently "UNSIGNED DRAFT"), NOT executable goals. To light RoadmapReader + goal_decomposition_planner (both default-FALSE): operator must author goals + set HMAC secret + sign. Cannot be done unilaterally — only the user can write the strategic vision. goal_decomposition_planner is rule-based default (splits by target_files), pluggable model-backed decomposer. See [[project-slice200-genesis-proposal]].
