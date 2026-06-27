---
title: Project Slice215 Cascade Matrix
modules: []
status: historical
source: project_slice215_cascade_matrix.md
---

**Slice 215 — FrugalGPT Per-Round Cascade (MERGED #69457, main `2ead1e42ae`, 2026-06-10).** THE FLIP: `JARVIS_PROVIDER_CLAUDE_DISABLED=false` — DW-only era ends (its cortex-validation purpose long served; soak purpose is now autonomy). Pure-config slice (every knob already env-driven).

**L2 PROBE VERDICT (the decisive evidence, via `docker exec -i` + capture-router):** GOAL-001 flows reader(`roadmap_verdict=valid`) → decomposition(`valid`, **2 sub-goals identified**) → **IntentEnvelope(source='roadmap') CAPTURED into the router**. Remaining break: `orchestration_verdict='no_plan'`, sub_goals_emitted=0/2 — the planning step produces nothing = needs reliable LLM capacity = the provider problem (39 exhaustions/49 dispatches DW-only). NOTE: `docker exec` heredoc probes REQUIRE `-i` (without it stdin closes → python3 - exits silently with NO output — burned two probe runs).

**Per-round cascade (blindspot-#1 correction to my own chain-math):** chains do NOT go Claude-primary — each Venom round runs DW-primary, Claude rescues THAT round on failure → per-round survival ≈1−(0.3×0.01)≈99.7%, 10-round chain ~97%, DW still serves cheap rounds. Charter via `provider_topology` DEFAULTS (already encoded): IMMEDIATE/COMPLEX `dw_allowed=false`→`cascade_to_claude`; BACKGROUND/SPECULATIVE `skip_and_queue` (NEVER Claude — cost shield).

**Knobs set:** `JARVIS_FALLBACK_MAX_TIMEOUT_S` 120→180, `OUROBOROS_PLAN_FALLBACK_MAX_TIMEOUT_S` 60→120 (old caps truncated healthy mid-stream rescues). **COST (operator's #1 concern):** session cost-cap 25→**10** (bounds the restart:always per-session-reset loophole), `JARVIS_TELEMETRY_SENTINEL_ENABLED=1` (Discord webhook in .env → COST_CAP_90 alert BEFORE the cap), `JARVIS_THINKING_BUDGET_IMMEDIATE=0` pinned, S131 response cache dedupes. Expected steady ~$1-2/day. Bandit state archived host-side at relaunch (`bandit_router_state.pre215.*.bak`) → fresh priors for the Claude arm (noisy first hours accepted). **Kernel take-3 milestone same day: first `real_kernel_exit=0` LAUNCH ATTESTED (8cc2fb4d4ded stamp==pin, marker present).** Watch items first hours: 3-router interplay, brake never exercised w/ Claude live, hedge/cascade layering. See [[project-slice213-lifecycle-kernel]], [[project-slice209-autonomy-ignition]].
