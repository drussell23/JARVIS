---
title: DirectionInferrer Arc — CLOSED
modules: [backend/core/ouroboros/governance/direction_inferrer.py, backend/core/ouroboros/governance/posture.py, backend/core/ouroboros/governance/strategic_direction.py, backend/core/ouroboros/governance/ide_observability.py, backend/core/ouroboros/governance/posture_observer.py]
status: merged
source: project_direction_inferrer_graduation.md
---

# DirectionInferrer Arc — CLOSED

**Graduated 2026-04-21.** Wave 1 #1 of the A-level-execution roadmap is live.

## What graduated

`JARVIS_DIRECTION_INFERRER_ENABLED` default flipped **`false` → `true`** in `direction_inferrer.py::is_enabled()`. Four surfaces now active out-of-the-box with zero env setup:

1. **Prompt injection** — `StrategicDirection.format_for_prompt()` appends `## Current Strategic Posture` block (≤600 chars, top-3 signals, posture-specific advisory).
2. **/posture REPL** — 7 subcommands (status/explain/history/signals/override/clear-override/help). Override is the ONLY write-surface; does NOT bypass Iron Gate / SemanticGuardian / risk-tier.
3. **IDE GET** — `/observability/posture` + `/observability/posture/history` (loopback + rate-limit + CORS + schema v1.0 + `Cache-Control: no-store`, double-gated by ide_observability + master flag).
4. **SSE bridge** — `posture_changed` event type, `publish_posture_event()` helper, `bridge_posture_to_broker()` chains observer.on_change → broker.

Explicit `JARVIS_DIRECTION_INFERRER_ENABLED=false` reverts all four in lockstep (proven in live-fire revert matrix).

## Commits

```
61e7abd488 Slice 1 — primitive + posture.py (55 tests, livefire 12/12)
ec8fccac22 Slice 2 — observer + store + prompt injection (61 tests, livefire 17/17)
00b1f0f22e Slice 3 — /posture REPL + GET + SSE (49 tests, livefire 27/27)
<pending>   Slice 4 — graduation + 43 pins (livefire 33/33)
```

## Final numbers

| Dimension | Count |
|---|---|
| Python test files | 4 (direction_inferrer / observer / repl / graduation) |
| Tests green | **208/208** combined |
| Live-fire scripts | 4, all PASS |
| Live-fire checks | 12 + 17 + 27 + 33 = **89 total** |
| Graduation pins | 43 (7 authority + 13 behavioral + 9 graduation-specific + 4 schema + 3 docstring + 3 integration + 2 revert + 2 CLAUDE.md guard) |
| LoC new | ~2700 Python (primitives + observer + REPL + store + prompt) |
| LoC integration | ~30 (strategic_direction.py + ~170 ide_observability + ~170 ide_observability_stream) |
| Authority files grep-pinned | 6 arc files + 2 GET handler methods in ide_observability.py |
| Commits | 4 |

## Posture vocabulary (final, frozen at graduation)

| Posture | Driver signals | Advisory |
|---|---|---|
| `EXPLORE` | high feat:, low postmortem, recent graduations, fresh momentum | Ship new capabilities, accept measured risk, favor breadth |
| `CONSOLIDATE` | refactor:, stale WIP, deferred items, no recent graduation | Finish in-flight threads, prefer graduation over new arcs |
| `HARDEN` | fix:, postmortem spike, Iron Gate rejects, L2 repair rate, session lessons infra% | Stabilize before adding features, tighten gates, test-first |
| `MAINTAIN` | baseline / low-confidence fallback (<0.35 default) | No strong signal — standard diligence |

Severity encoded in `confidence ∈ [0,1]`, not a 5th posture value. Acute failure → Tier 3 Nervous System Reflex / Iron Gate, not a passive posture.

## Full-revert matrix (Slice 4 live-fire centerpiece)

Setting `JARVIS_DIRECTION_INFERRER_ENABLED=false` at runtime reverts:

- Surface 1 (prompt): `format_for_prompt()` omits posture section ✓
- Surface 2 (REPL): `/posture status` returns error citing the flag name ✓
- Surface 3 (GET): `/observability/posture` returns **403** (port scanners see no signal) ✓
- Surface 4 (SSE): bridge stops firing because observer stops cycling; direct `publish_posture_event()` still allowed (stream has its own flag — cross-silence not the contract)

`/posture help` **still works master-off** — intentional exception so operators can discover the flag without reading CLAUDE.md.

Re-defaulting the env var (unset) restores all surfaces within one call — proven in live-fire Phase 2 bidirectional check.

## Signal collectors — real sources at graduation

| Signal | Source | Fallback |
|---|---|---|
| feat_ratio / fix_ratio / refactor_ratio / test_docs_ratio | `git log -50 --pretty=format:%s` + Conventional Commit regex | all zeros (no-git repo) |
| postmortem_failure_rate | `.ouroboros/sessions/*/summary.json` ops_digest (48h window) | 0.0 (no sessions dir) |
| iron_gate_reject_rate | summary.json event_counts.iron_gate_reject (24h) | 0.0 |
| l2_repair_rate | summary.json event_counts.l2_invoked (24h) | 0.0 |
| session_lessons_infra_ratio | summary.json session_lessons array, tag=infra | 0.0 |
| time_since_last_graduation_inv | `git log` subjects matching `graduate.*JARVIS_` or `GRADUATED` | 0.0 |
| open_ops_normalized | injected `open_ops_provider` callable (optional) | 0.0 |
| cost_burn_normalized | `.jarvis/cost_state.json` daily_spent / daily_cap | 0.0 |
| worktree_orphan_count | `JARVIS_WORKTREE_BASE` env var dir `unit-*` count | 0 |

## Consumer API ready for Wave 1 #2 + #3

`posture_observer.py` exposes these for downstream consumers:

- `get_current_posture()` — `Posture | None`
- `get_default_observer()` / `get_default_store()` — singletons
- Observer's `on_change` hook is chain-compatible (bridge preserves prior hooks via `_chained` wrapper)
- `bridge_posture_to_broker()` returns an unsubscribe callable
- `publish_posture_event(trigger, reading, previous, extra)` — best-effort, never-raise

**NOT yet shipped (deferred to Slice 5 follow-up):** `get_posture_weighted_budget()` + `is_flag_relevant_to_posture()` + `subscribe(callback)` pub/sub primitive. These land when Wave 1 #2 FlagRegistry or Wave 1 #3 SensorGovernor ship and actually need them.

## What was proven by the per-slice E2E mandate

Per Derek's durable guidance in `feedback_per_slice_e2e_livefire.md`, every slice got a real-repo live-fire. Bugs caught by live-fire that unit tests missed:

1. **Slice 2** — malformed f-string (`:.3f if reading else 0`) — would have crashed first production observer cycle
2. **Slice 3** — blocking socket call on async event loop starved the server itself — would have deadlocked every GET request in a real REPL session
3. **Slice 4** — same f-string typo (refactor-paste regression); caught instantly by Slice 4 live-fire before graduation commit

Each fix landed in-slice; zero bugs deferred forward. This is what the mandate is for.

## What's next

**Wave 1 #2** — FlagRegistry + /help auto-generation (481 unregistered env flags → typed registry; subsequent arcs get posture-tagged discovery for free).

**Wave 1 #3** — SensorGovernor — global op-emission cap across all 16 sensors, weighted by current posture (TestFailure ×1.8 HARDEN, OpportunityMiner ×1.5 EXPLORE, etc.) + MemoryPressureGate that refuses worktree fan-out under threshold.

Both consume `get_current_posture()` and (when Slice 5 ships) `get_posture_weighted_budget()`. Neither changes anything in the arc just graduated — integration is pull-mode, not push.

## Operator kill switches (canonical reference)

| Flag | Default | Effect when false |
|---|---|---|
| `JARVIS_DIRECTION_INFERRER_ENABLED` | `true` | All 4 surfaces revert (master kill switch) |
| `JARVIS_POSTURE_PROMPT_INJECTION_ENABLED` | `true` (when master on) | Prompt section empty, other surfaces active |
| `JARVIS_POSTURE_OBSERVER_INTERVAL_S` | `300` | — (tuning knob, not a kill) |
| `JARVIS_POSTURE_HYSTERESIS_WINDOW_S` | `900` | — (tuning) |
| `JARVIS_POSTURE_CONFIDENCE_FLOOR` | `0.35` | — (tuning) |
| `JARVIS_POSTURE_HIGH_CONFIDENCE_BYPASS` | `0.75` | — (tuning) |
| `JARVIS_POSTURE_OVERRIDE_MAX_H` | `24` | — (clamp) |
| `JARVIS_POSTURE_HISTORY_SIZE` | `256` | — (ring buffer cap) |
| `JARVIS_POSTURE_WEIGHTS_OVERRIDE` | unset | — (JSON hot-swap for A/B) |
