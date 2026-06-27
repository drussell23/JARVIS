---
title: Project M10 Architecture Proposer
modules: []
status: merged
source: project_m10_architecture_proposer.md
---

**Status (2026-05-04)**: Slices 1+2+3+4+5 CLOSED — full M10 spine **173/173 tests green**. Master flag remains default-FALSE per §30.5.2 — Slice 5 graduates *surfaces* (REPL/HTTP/SSE/pins/seeds), NOT the production default.

## Architectural locks (operator mandate, all enforced)

1. **Zero duplication** — composes `cross_process_jsonl.flock_*` / Move 6 `compute_ast_signature` / `_scoring_primitives` / coherence_window_store / OrangePRReviewer / AutoCommitter / WorktreeManager / SemanticGuardian — every primitive existing-substrate. Per AST pin: NO direct imports of orchestrator/iron_gate/policy/providers/candidate_generator/urgency_router/change_engine/semantic_guardian/graduation_orchestrator
2. **Closed-enum `M10ProposalPhase`** — 16-value FSM lifted verbatim from graduation_orchestrator's `GraduationPhase`
3. **Mandatory AST self-pin** — every M10 proposal MUST include an AST invariant for itself; rejected at synthesizer if missing (NO_SELF_PIN verdict)
4. **Cost contract preserved by composition** — STANDARD route × Quorum K=3 = ~$0.015/proposal; hard-capped at `JARVIS_M10_MAX_DAILY` (default 5/day) → ≤$0.075/day max
5. **Master flag default-FALSE** until 30+ proposal-acceptance audit (per §30.5.2; AST-pinned guard against silent default-true)
6. **Forced APPROVAL_REQUIRED** — module-level `M10_FORCED_RISK_TIER = "approval_required"` constant + bytes-pin enforces never-auto-apply
7. **Operator-fatigue auto-pause** — `proposal_acceptance_rate < 30%` over last 20 proposals → miner auto-paused for one posture cycle
8. **Authority asymmetry** — AST-pinned at 4 modules: primitives / synthesizer / lifecycle / unhandled_pattern_miner

## Slices 1-4 summary

`m10/primitives.py` (~530 LOC, 52 tests) — Slice 1: closed enums + Bayesian threshold + 24-field record + 6 env knobs.

`m10/unhandled_pattern_miner.py` (~750 LOC, 23 tests) — Slice 2: pattern clustering with PatternSourceProtocol + adaptive threshold gate + storm-guard + daily cap.

`m10/proposal_synthesizer.py` (~620 LOC, 28 tests) — Slice 3: K=3 parallel via `asyncio.gather` + Move 6 `compute_ast_signature` consensus + mandatory ast_pin gate + `M10_FORCED_RISK_TIER = "approval_required"`.

`m10/lifecycle.py` (~750 LOC, 29 tests) — Slice 4: 5-layer validation (Layers 3+4 parallel) + caller-injected Bridge Protocols (Worktree/Commit/OrangePR) + branch namespace `ouroboros/m10/{proposal_id}` + H3 push-fail preserves branch.

## Slice 5 (DONE 2026-05-04) — Graduation surfaces

41/41 graduation regression tests green. Substrate-only graduation per §30.5.2.

### Modules added

- `m10/proposal_store.py` (~230 LOC) — frozen `StoredProposal` (15 fields) + `append_proposal` / `read_all_proposals` / `find_proposal_by_id` / `aggregate_phase_histogram` / `list_pending_proposals` via `cross_process_jsonl.flock_*`. JSONL ledger at `.jarvis/m10/proposals.jsonl` (override via `JARVIS_M10_PROPOSALS_PATH`). NEVER raises.
- `m10/observability.py` (~210 LOC) — `_M10RoutesHandler` + `register_routes(app)` mounting `GET /observability/m10` (overview: recent + phase histogram + pending) + `GET /observability/m10/proposal/{proposal_id}` (detail). 503 on master-off; 429 on rate-limit; loopback-only via caller-supplied check.
- `m10/repl.py` (~330 LOC) — `dispatch_m10_command` 5-subcommand REPL (`pending`/`show`/`history`/`stats`/`help`) + `register_verbs` /help auto-discovery hook. `help` bypasses master gate; everything else 503-equivalent on disabled master.

### Cross-cutting wiring

- `ide_observability_stream.py` — added `EVENT_TYPE_M10_PROPOSAL_EMITTED = "m10_proposal_emitted"` constant + `publish_m10_proposal_event()` best-effort publisher (chatter-suppressed: fires only at terminal+awaiting phases).
- `m10/__init__.py::register_shipped_invariants()` — auto-discovered by `shipped_code_invariants._discover_module_provided_invariants` (one level deep — `m10/` is a sub-package of `governance/`, picked up via `iter_modules`).
- `flag_registry_seed.py` — 5 FlagSpec entries appended to `SEED_SPECS`.

### 8 AST pins (all PASS validate_all)

1. `m10_synthesizer_uses_quorum` — `compute_ast_signature` import + `asyncio.gather` present in proposal_synthesizer.py
2. `m10_lifecycle_uses_orange_pr` — `OrangePRBridgeProtocol` class present in lifecycle.py + NO direct `orange_pr_reviewer`/`auto_committer`/`worktree_manager` imports
3. `m10_forced_risk_tier_constant` — module-level constant + bytes-pin literal `"approval_required"`
4. `m10_master_flag_stays_default_false` — `m10_arch_proposer_enabled` returns False on unset env + NO `return True  # graduated default` marker
5-8. `m10_*_authority_asymmetry` — 4 modules (primitives/synthesizer/lifecycle/unhandled_pattern_miner) cannot import orchestrator/iron_gate/policy/providers/doubleword_provider/candidate_generator/urgency_router/change_engine/semantic_guardian/graduation_orchestrator

### 5 FlagRegistry seeds

- `JARVIS_M10_ARCH_PROPOSER_ENABLED` (BOOL, default **False** — operator-pinned per §30.5.2)
- `JARVIS_M10_ADAPTIVE_MIN_THRESHOLD` (INT, default 2)
- `JARVIS_M10_ADAPTIVE_CONFIDENCE` (FLOAT, default 2.0)
- `JARVIS_M10_MAX_DAILY` (INT, default 5)
- `JARVIS_M10_APPROVAL_TIMEOUT_S` (FLOAT, default 86400)

### Slice 5b deferred (production wire-up)

- `event_channel.py` — call `m10.observability.register_routes(app)` at boot
- SerpentREPL `command_loop` — call `dispatch_m10_command` in `/m10` branch
- UnhandledPatternMiner async observer — schedule periodic `mine()` with posture-aware cadence
- ProposalLifecycleOrchestrator → `proposal_store.append_proposal` write path on every phase transition

## §32.8 v3 sequencing context (next item)

After M10 closure: **graduation_orchestrator.py cleanup arc** (§32.5, ~1d) → §32.8 v4 supplement items (Phase 9 CRITICAL BLOCKER, Phase 10 Slices 2-6, Phase 6 SelfNarrativeService).
