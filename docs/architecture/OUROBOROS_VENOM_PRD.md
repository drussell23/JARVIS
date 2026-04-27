# Ouroboros + Venom (O+V) — Product Requirements Document & Roadmap

**Status**: Living document
**Version**: 2.0 (2026-04-25)
**Author**: Derek J. Russell (vision) · Claude Opus 4.7 (PRD synthesis)
**Audience**: Operator (decision authority), JARVIS engineers, future-self (resuming after context loss)
**Prerequisite reading**: `CLAUDE.md` (architecture), `docs/architecture/OUROBOROS.md` (battle-test breakthrough log), `docs/architecture/RSI_CONVERGENCE_FRAMEWORK.md` (Wang RSI mathematical foundation)

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Vision Statement](#2-vision-statement)
3. [Current State Assessment](#3-current-state-assessment)
4. [The Cognitive Scaffolding Gap (Deep Dive)](#4-the-cognitive-scaffolding-gap-deep-dive)
5. [RSI Convergence Framework — Where We Are on the Wang Curve](#5-rsi-convergence-framework--where-we-are-on-the-wang-curve)
6. [Target State (A-Level Execution from A-Level Vision)](#6-target-state-a-level-execution-from-a-level-vision)
7. [Strategic Pillars](#7-strategic-pillars)
8. [Governing Philosophy Alignment (Manifesto + 7 Principles)](#8-governing-philosophy-alignment-manifesto--7-principles)
9. [Roadmap (Phased, Impact-Ranked)](#9-roadmap-phased-impact-ranked)
   - [Phase 1 — Self-Reading](#phase-1--self-reading-target-46-weeks)
   - [Phase 2 — Self-Direction](#phase-2--self-direction-target-610-weeks)
   - [Phase 3 — Operator Symbiosis](#phase-3--operator-symbiosis-target-46-weeks-parallel-to-phase-2)
   - [Phase 4 — Cognitive Metrics](#phase-4--cognitive-metrics-target-24-weeks-can-ship-in-parallel)
   - [Phase 5 — Adversarial Depth](#phase-5--adversarial-depth-target-610-weeks-can-run-after-phase-12)
   - [Phase 6 — Self-Modeling](#phase-6--self-modeling-target-36-months-long-horizon)
10. [Per-Phase Requirements: Telemetry & Observability](#10-per-phase-requirements-telemetry--observability)
11. [Per-Phase Requirements: Testing Strategy](#11-per-phase-requirements-testing-strategy)
12. [Edge Cases & Nuances (cross-cutting)](#12-edge-cases--nuances-cross-cutting)
13. [Success Metrics (PRD-level)](#13-success-metrics-prd-level)
14. [Risks & Mitigations](#14-risks--mitigations)
15. [Out of Scope (deferred / future)](#15-out-of-scope-deferred--future)
16. [Open Questions for Operator Decision](#16-open-questions-for-operator-decision)
17. [Implementation Discipline](#17-implementation-discipline)
18. [Stakeholder Map](#18-stakeholder-map)
19. [PRD Migration & Versioning Strategy](#19-prd-migration--versioning-strategy)
20. [Roadmap Summary (one-page chronological)](#20-roadmap-summary-one-page-chronological)
21. [Why this Roadmap, in this Order](#21-why-this-roadmap-in-this-order)
22. [The Larger Frame — Trinity AI Ecosystem](#22-the-larger-frame--trinity-ai-ecosystem)
23. [The Reverse Russian Doll — Orders of Self-Reference (Architectural Framing)](#23-the-reverse-russian-doll--orders-of-self-reference-architectural-framing)
    - [23.1 The vocabulary contribution](#231-the-vocabulary-contribution)
    - [23.2 Orthogonality — the Order axis runs perpendicular](#232-orthogonality--the-order-axis-runs-perpendicular)
    - [23.3 Order 0 — The Exoskeleton Baseline](#233-order-0--the-exoskeleton-baseline)
    - [23.4 Order 1 — The Body (current shipping state)](#234-order-1--the-body-current-shipping-state)
    - [23.5 Order 2 — The Cognitive Substrate (horizon)](#235-order-2--the-cognitive-substrate-horizon)
    - [23.6 Anti-Venom — the Adaptive Immune System Thesis](#236-anti-venom--the-adaptive-immune-system-thesis)
    - [23.7 Trinity-Wide Order-2 Manifest Architecture](#237-trinity-wide-order-2-manifest-architecture)
    - [23.8 Composition with the Phase 1–6 Roadmap](#238-composition-with-the-phase-16-roadmap)
    - [23.9 Composition with Wang RSI Convergence (§5)](#239-composition-with-wang-rsi-convergence-5)
    - [23.10 Pass A → Pass B → Pass C — the Three-Pass Sequence](#2310-pass-a--pass-b--pass-c--the-three-pass-sequence)
    - [23.11 Operator Decisions Ratified 2026-04-26](#2311-operator-decisions-ratified-2026-04-26)
    - [23.12 Implementation Discipline + Cross-References](#2312-implementation-discipline--cross-references)
- [Appendix A — Glossary](#appendix-a--glossary)
- [Appendix B — Reference Documents Map](#appendix-b--reference-documents-map)
- [Appendix C — Phase Gate Criteria (entry/exit conditions)](#appendix-c--phase-gate-criteria-entryexit-conditions)
- [Appendix D — Document History](#appendix-d--document-history)

---

## 1. Executive Summary

Ouroboros + Venom (O+V) is the autonomous self-development governance engine of JARVIS. It is the **proactive autonomous opposite of Claude Code (CC)** — where CC requires a human to ask, O+V should observe, hypothesize, propose, validate, and ship without prompting (with human-in-loop escalation only when context warrants it).

### Where we stand (2026-04-25)

- **Architecture**: B+ — sophisticated, composable, observability-rich, financial-circuit-breaker-protected. The 11-phase FSM + 16 sensors + cost-governor + Iron Gate + risk-tier ladder all work and compose correctly.
- **Cognitive depth**: C+ — sensors fire on hardcoded conditions; POSTMORTEM is observational not corrective; no closed feedback loops.
- **Production track record**: 1 verified end-to-end multi-file APPLY (Sessions Q-S, 2026-04-15); Wave 3 architecturally complete (W3(7) graduated, W3(6) gated only on external Anthropic API stability).
- **RSI scaffolding**: 6 Wang-paper improvements designed (`docs/architecture/RSI_CONVERGENCE_FRAMEWORK.md`); composite_score / convergence_tracker / oracle_prescorer / transition_tracker / vindication_reflector / adaptive-graduation-threshold pending implementation status verification.

### Where we're going

A-level reliable execution from A-level vision — measurable by:
- Sustained 90%+ session completion rate (currently variable)
- Cross-session learning evidence (currently zero)
- Self-directed goal formation (currently zero — sensors only)
- Conversational mode parity with CC (currently intent-driven only)
- Convergence metric trending in the Wang sense (currently INSUFFICIENT_DATA)

This PRD lays out a phased roadmap to close the gap. **The gap is internal to JARVIS, not external.** External provider quality is sufficient; what's missing is the orchestration layer that converts that intelligence into self-directing, self-improving behavior.

### Roadmap Execution Status (live)

Per-slice status. `[x]` = landed on main; `[~]` = in-flight on a branch / open PR; `[ ]` = not started. Master-flag flips after a graduation cadence are tracked separately (see §17 Implementation Discipline).

**Phase 0 — RSI implementation status audit** (gate for Phase 1)
- [x] 6/6 Wang RSI modules verified to exist (composite_score, convergence_tracker, oracle_prescorer, transition_tracker, vindication_reflector, adaptive graduation threshold)
- [x] 4/6 wired into the live FSM; 2 stranded (oracle_prescorer, vindication_reflector — tracked for Phase 4)
- [x] 131/131 RSI module tests green
- [x] Audit memo committed (`memory/project_phase_0_rsi_audit_2026_04_25.md`)

**Phase 1 — Self-Reading**
- P0 — POSTMORTEM → next-op recall (`PostmortemRecallService`, PRD §9.P0)
  - [x] Module + orchestrator wiring + 41 unit tests landed (PR #20968 merged → main `ef32006663`)
  - [x] Live-fire smoke (`scripts/livefire_p0_postmortem_recall.py`, 16/16 PASS)
  - [x] Graduation pin tests (`tests/governance/test_postmortem_recall_graduation_pins.py`, 17/17 PASS)
  - [x] Helper extraction + orchestrator-level reachability supplement (W3(6) precedent — `tests/governance/test_postmortem_recall_orchestrator_smoke.py`, 9/9 PASS). Total layered evidence: **67 deterministic tests + 16 in-process smoke**. Live-cadence soak attempts (3/3) hit known BG-starvation pattern (memory `project_wave3_item6_graduation_matrix.md`) — supplement substitutes per Layer 3 precedent.
  - [x] Observability follow-on (PR #21451): helper emits CONTEXT_EXPANSION DEBUG breadcrumbs uniformly on master-off + matched=0 paths (mirrors LSS pattern; closes audit gap discovered during post-#21355 live verification).
  - [x] **Master flag `JARVIS_POSTMORTEM_RECALL_ENABLED` flipped `false`→`true`** (2026-04-26, this PR). Hot-revert: `export JARVIS_POSTMORTEM_RECALL_ENABLED=false`. **Phase 1 P0 COMPLETE — first cognitive feedback loop now live by default.**
- P0.5 — Cross-session direction memory (DirectionInferrer + LSS + 100-commit git momentum)
  - [x] Slice 1 — `git_momentum` extraction (PR #21545 → main `9250f62538`, 22 tests, byte-identical refactor)
  - [x] Slice 2 — `arc_context` consumer + bounded-nudge math (PR #21624 → main `996569646b`, 20 tests, observation-only by default)
  - [x] Slice 3 — `/posture explain` arc-context section + graduation flip + comprehensive pin suite + in-process live-fire + posture-observer reachability supplement (this PR). **`JARVIS_DIRECTION_INFERRER_ARC_CONTEXT_ENABLED` default flipped `false`→`true`.** Hot-revert: single env knob. **Phase 1 P0.5 COMPLETE — second cognitive feedback loop now live by default.** Layered evidence: 282 deterministic tests + 31 in-process smoke + bounded-nudge safety pinned (≤0.10/posture, cannot override clear winner).

**Phase 2 — Self-Direction** (per PRD §9):
- P1 — Curiosity Engine v2 (model writes backlog entries; consumes POSTMORTEM clusters)
  - [x] Slice 1 — `postmortem_clusterer.py` (PR #21663 → main `f32e64aca1`, 28 tests, deterministic + signature-hash-stable)
  - [x] Slice 2 — `self_goal_formation.py` engine (PR #21702 → main `eb290e4eff`, 40 tests, 9-gate decision tree all pinned, JSONL audit ledger)
  - [x] Slice 3 — `BacklogSensor` consumer (PR #21739 → main `d063cbd924`, 26 tests, source="auto_proposed", bounded ≤5/scan, requires_human_ack=True)
  - [x] Slice 4 — `/backlog auto-proposed` REPL operator-review surface (PR #21751 → main `da9f55c707`, 35 tests, idempotent approve/reject, decisions sidecar ledger)
  - [x] Slice 5 — **DUAL master flag flip**: `JARVIS_SELF_GOAL_FORMATION_ENABLED` + `JARVIS_BACKLOG_AUTO_PROPOSED_ENABLED` both default `false`→`true` (this PR). 29 graduation pin tests + 18 in-process live-fire checks + end-to-end integration test (cluster → engine → ledger → sensor → envelope → REPL approve → backlog.json all in one). Hot-revert: each flag has its own env knob. **Phase 2 P1 COMPLETE — first self-formed-goal feedback loop now live by default.** Layered evidence: 158 deterministic tests + 18 in-process smoke. Bounded-by-construction safety pinned (per-session cap=1, cost cap=$0.10, posture veto, blocklist dedup, operator-review tier).
- P1.5 — Hypothesis ledger (every self-formed goal paired with a falsifiable hypothesis)
  - [x] Slice 1 — `hypothesis_ledger.py` primitive + `/hypothesis ledger` REPL (PR #21794 → main `d8ae6e988a`, 46 tests, append-only with last-write-wins per ID, 7 REPL subcommands)
  - [x] Slice 2 — engine integration (`SelfGoalFormationEngine` emits paired Hypothesis behind `JARVIS_HYPOTHESIS_PAIRING_ENABLED`) + `hypothesis_validator.py` (token-overlap math) + comprehensive graduation pin suite (28 pins) + in-process live-fire smoke (15/15 PASS) + dual env knob hot-revert (this PR). **`JARVIS_HYPOTHESIS_PAIRING_ENABLED` default `false`→`true`.** Hot-revert: single env knob. **Phase 2 P1.5 COMPLETE — every self-formed goal now paired with a falsifiable hypothesis + auto-validator.** Layered evidence: 74 deterministic tests + 15 in-process smoke + end-to-end integration (engine emit → validator decide → ledger updated → stats reflected).

**Phase 3 — Operator Symbiosis**: ✅ ALL THREE ITEMS GRADUATED (P2 + P3 + P3.5)
  - [x] P3.5 — Realtime progress visibility (per-stream HEARTBEAT + coalesced status line; PR #21896 → main `c39eb05197`). Always-on per PRD spec; bounded in-memory tracker, FIFO eviction, ASCII-safe render.
  - [x] P3 Slice 1 — `inline_approval.py` primitive: parser + bounded FIFO queue with IMMEDIATE/BLOCKED priority + frozen request/decision dataclasses + default-singleton (PR #21910 → main `f6dbba93d0`, 82 tests).
  - [x] P3 Slice 2 — `inline_approval_provider.py` conforms to `ApprovalProvider` Protocol + JSONL audit ledger at `.jarvis/inline_approval_audit.jsonl` for §8 observability (PR #21926 → main `37fd122b0c`, 35 tests).
  - [x] P3 Slice 3 — `inline_approval_renderer.py` owns the I/O surface: render block + 30s `select`-based prompt + `$EDITOR` shell-out (argv only, never `shell=True`) + `run_inline_approval_loop` orchestrator (PR #21944 → main `54b93f12a8`, 48 tests).
  - [x] P3 Slice 4 — graduation: `build_approval_provider()` factory wired into `GovernedLoopService`; **`JARVIS_APPROVAL_UX_INLINE_ENABLED` default flipped `false`→`true`** (this PR). Layered evidence: 165 deterministic Slice 1-3 tests + 36 graduation pins (master flag default-true + source-grep `"1"` literal + factory branch coverage + GovernedLoopService source-grep + cross-slice authority survival + reachability supplement) + 15 in-process live-fire smoke checks (factory-built provider end-to-end through queue + renderer + audit ledger). Hot-revert: single env knob — set `JARVIS_APPROVAL_UX_INLINE_ENABLED=false` and the factory returns the legacy `CLIApprovalProvider` on the next construction. **Phase 3 P3 COMPLETE — inline approval UX live by default, EOF / garbage / timeout all defer-not-approve (safety-first contract preserved).**
  - [x] P2 Slice 1 — `intent_classifier.py` primitive (4-category enum + deterministic regex + code-paste heuristic + bounded message length; PR #22036 → main `e89ba70fa6`, 81 tests).
  - [x] P2 Slice 2 — `conversation_orchestrator.py` + `ChatTurn` + `ChatSession` (bounded ring buffer + routing dispatch + ConversationBridge feed; PR #22059 → main `67a6136fe6`, 38 tests).
  - [x] P2 Slice 3 — `chat_repl_dispatcher.py` + `/chat` REPL + ASCII renderer + `ChatActionExecutor` Protocol (PR #22070 → main `b44d70a85e`, 52 tests). Subcommand parsing has shape gating so natural-language `/chat why is X happening?` doesn't misroute.
  - [x] P2 Slice 4 — graduation: `build_chat_repl_dispatcher()` factory + `LoggingChatActionExecutor` safe-default + flag flip. **`JARVIS_CONVERSATIONAL_MODE_ENABLED` default flipped `false`→`true`** (this PR). Layered evidence: 171 deterministic Slice 1-3 tests + 45 graduation pins (master flag default-true on BOTH env-knob owners + source-grep `"1"` literal pin × 2 + factory branch coverage + LoggingExecutor contract pin + cross-slice authority survival × 4 modules + reachability supplement) + 15 in-process live-fire smoke checks (factory→classifier→orchestrator→dispatcher→executor end-to-end across all 4 ChatActionExecutor branches; bounded-ring under load; hot-revert proven). Hot-revert: single env knob — `JARVIS_CONVERSATIONAL_MODE_ENABLED=false` and the factory returns `None` so SerpentFlow can skip surfacing `/chat` entirely; orchestrator + bridge state remain inspectable for prior-decision recall. **Phase 3 P2 COMPLETE — operator natural-language understood + classified + audit-trailed by default.**
  - [x] **P2 Slice 4 follow-up — concrete ChatActionExecutors** ✅ MINI-ARC CLOSED 2026-04-26 (all 3 PRs landed; safe-default `LoggingChatActionExecutor` is now superseded by Claude(Subagent(Backlog(Logging))) when all three flags are on; each executor is independently default-off until graduation):
    - [x] **PR 1 — `BacklogChatActionExecutor` landed 2026-04-26.** Concrete `dispatch_backlog` writes to `.jarvis/backlog.json` via the existing `_append_to_backlog_json` helper (single-source the write contract with `/backlog auto-proposed`). Entry shape includes `source="chat_repl"`, `session_id`, `turn_id`, `submitted_timestamp_unix` provenance markers + `task_id="chat:{turn_id}"` for BacklogSensor dedup. Other 3 Protocol methods (spawn_subagent / query_claude / attach_context) delegate to a fallback executor (defaults to `LoggingChatActionExecutor`) — **per-method composition pattern** so PRs 2 + 3 can swap each fallback slot without touching the dispatcher. Default-off behind `JARVIS_CHAT_EXECUTOR_BACKLOG_ENABLED` (legacy fallback to LoggingChatActionExecutor when off — zero behavior change). New factory `build_chat_repl_dispatcher_with_backlog()` honors both the per-executor flag AND the existing `JARVIS_CONVERSATIONAL_MODE_ENABLED` master (master-off → returns None regardless). Bounded message length (`MAX_BACKLOG_DESCRIPTION_CHARS=1024`); empty message → error token + no file write (no schema pollution). Audit list `.calls` populated with task_id-or-error-token. Layered evidence: **27 regression pins** (`tests/governance/test_chat_repl_backlog_executor.py`) covering module constants + master flag truthy/falsy variants + write-real-entry + append-to-existing + empty-message-no-write + whitespace-only-no-write + truncation-at-max + timestamp-bounded + audit-on-success + audit-on-error + 4 fallback-delegation pins + 5 factory wiring pins (master-off / master-on / chat-master-off / default-cwd / fallback-passthrough) + 1 end-to-end smoke + 3 authority invariant pins (no banned imports / no subprocess+network tokens / write-only-via-helper) + Protocol conformance. Combined regression spine: **239/239 tests green** across new executor + chat_repl_dispatcher + conversation_orchestrator + intent_classifier + backlog_auto_proposed_repl. Hot-revert: single env knob (`JARVIS_CHAT_EXECUTOR_BACKLOG_ENABLED=false`) → factory returns the legacy LoggingChatActionExecutor-wired dispatcher.
    - [x] **PR 2 — `SubagentChatActionExecutor` landed 2026-04-26.** Concrete `spawn_subagent` enqueues a JSONL ticket to `.jarvis/chat_subagent_queue.jsonl` (enqueue-and-return-ticket pattern; the actual `AgenticExploreSubagent` dispatch is a future `ChatSubagentSweeper` PR — keeps the `/chat` REPL responsive instead of blocking on a multi-second exploration). Ticket shape: `ticket_id="subagent:{turn_id}"` for sweeper dedup + `subagent_type="explore"` (only read-only type allowed via this surface — `plan/review/general` require orchestrator-driven invocation per Phase 1 contract) + provenance markers (`source="chat_repl"`, `session_id`, `turn_id`, `submitted_timestamp_unix`) + `schema_version=1`. Bounded goal length (`MAX_SUBAGENT_GOAL_CHARS=512`, tighter than backlog's 1024 because goals feed `SubagentRequest.goal`). Default-off behind `JARVIS_CHAT_EXECUTOR_SUBAGENT_ENABLED`. New factory `build_chat_repl_dispatcher_with_subagent()` chains through PR 1's backlog factory: subagent OFF → falls through to backlog factory; subagent ON + backlog OFF → `Subagent(fallback=Logging)`; both ON → `Subagent(fallback=Backlog(fallback=Logging))` so each method routes to the right concrete executor. AST-pinned that the executor does NOT import `AgenticExploreSubagent` / `SubagentScheduler` / `ExplorationSubagent` (would defeat the enqueue-pattern by allowing synchronous dispatch). Layered evidence: **32 regression pins** (`tests/governance/test_chat_repl_subagent_executor.py`) covering module constants + master flag truthy/falsy variants + write-real-ticket + append-to-existing + empty/whitespace-no-write + truncation-at-max + timestamp + audit-on-success/error + 5 fallback-delegation pins (incl. composition smoke `Subagent(Backlog(Logging))` end-to-end across 3 methods → 3 different files) + 7 factory wiring pins (4-flag-matrix coverage + master-off + default-cwd + explicit-fallback-bypass) + end-to-end smoke + 4 authority invariant pins (no banned imports / no subprocess+network / write-only-via-helper / no-sync-subagent-import) + Protocol conformance. Combined regression spine: **236/236 tests green** across PR 1+2 executors + chat_repl_dispatcher + conversation_orchestrator + intent_classifier. Hot-revert: single env knob (`JARVIS_CHAT_EXECUTOR_SUBAGENT_ENABLED=false`) → factory falls through to PR 1's backlog factory.
    - [x] **PR 3 — `ClaudeChatActionExecutor` landed 2026-04-26 — CLOSES the mini-arc + the third (final) deferred follow-up.** Concrete `query_claude` calls an injectable `ClaudeQueryProvider` (production wires `AnthropicClaudeQueryProvider` externally; tests inject fakes; default is `_NullClaudeQueryProvider` returning a sentinel — no API call, no cost — so misconfigured factory CANNOT accidentally hit the API). Cage: per-call cost cap (`DEFAULT_COST_CAP_PER_CALL_USD=0.05` matches AdversarialReviewer's per-op budget) + cumulative per-instance session budget (`DEFAULT_SESSION_BUDGET_USD=1.00`) + bounded prompt (`MAX_QUERY_CHARS=1024`) + bounded context (`MAX_RECENT_TURNS_INCLUDED=5`, per-fragment `MAX_RECENT_TURN_FRAGMENT_CHARS=240`) + bounded response (`MAX_RESPONSE_CHARS=4096`) + no auto-retry (one-shot) + persistent audit ledger at `.jarvis/chat_claude_audit.jsonl` capturing every outcome (ok / empty_message / session_budget_exhausted / call_would_exceed_budget / provider_error / provider_non_string). Conservative spend accounting (assumes per-call cap was hit; `cumulative_cost_usd` property exposed). AST-pinned that the executor does NOT import `providers.py` (would couple chat to codegen + drag the entire Anthropic stack into tests) NOR import `anthropic` directly (provider is injected). New factory `build_chat_repl_dispatcher_with_claude()` chains through PR 2's subagent factory producing the **full 8-flag composition matrix** (claude × subagent × backlog × master): all-on yields `Claude(fallback=Subagent(fallback=Backlog(fallback=Logging)))` — every Protocol method routes to its concrete implementation. Default-off behind `JARVIS_CHAT_EXECUTOR_CLAUDE_ENABLED`. Layered evidence: **51 regression pins** (`tests/governance/test_chat_repl_claude_executor.py`) covering 7 module constants + 3 master flag pins + NullProvider safety (returns sentinel + spend accounting documented) + happy-path response delivery + recent-turns context inclusion + 4 truncation pins (response / message / recent count / per-fragment) + 7 cage error paths (empty / whitespace / provider raise / non-string / session-budget-exhausted / pre-call-overshoot / already-exhausted-state) + 4 audit row pins (ok / empty / provider_error / session_budget_exhausted) + 4 fallback-delegation pins + cage check (query_claude does NOT delegate) + full-composition smoke (Claude→Subagent→Backlog→Logging across 4 methods → 4 different files) + 8 factory wiring pins (8-flag-matrix coverage + NullProvider when no provider supplied + custom budget kwargs propagate + master-off + explicit fallback bypass) + 4 authority invariant pins (no banned imports / no providers.py import / no subprocess+network / no anthropic import) + Protocol conformance + 3 audit-list pins. Combined regression spine: **287/287 tests green** across PR 1+2+3 executors + chat_repl_dispatcher + conversation_orchestrator + intent_classifier. Hot-revert: single env knob (`JARVIS_CHAT_EXECUTOR_CLAUDE_ENABLED=false`) → factory falls through to PR 2.

**🎯 Phase 3 — Operator Symbiosis FULLY GRADUATED 2026-04-26.** All three items closed (P3.5 + P3 + P2). Next open phase items: Phase 5 (Adversarial Depth) and Phase 6 (Self-Modeling) — both await operator direction.
**Phase 4 — Cognitive Metrics**: ✅ FULLY GRADUATED (P3 + P4)
  - [x] P3 Slice 1 — `cognitive_metrics.py` wrapper + `/cognitive` REPL + JSONL ledger (PR #21838 → main `24ec252519`, 43 tests). Un-strands the previously-isolated `OraclePreScorer` + `VindicationReflector` modules under a single `CognitiveMetricsService`.
  - [x] P3 Slice 2 — orchestrator boot-time singleton wiring + CONTEXT_EXPANSION pre-score call site + 19 graduation pin tests + 15 in-process live-fire checks + dual env knob hot-revert. **`JARVIS_COGNITIVE_METRICS_ENABLED` default `false`→`true`.** **Phase 4 P3 COMPLETE — both stranded RSI modules now wired into the live FSM.**
  - [x] P4 Slice 1 — `metrics_engine.py` primitive: 7-metric un-stranding wrapper around existing `composite_score` + `convergence_tracker` (305+354 LOC, both verified by Phase 0 audit but never user-surfaced) + 5 net-new operator calculators (session_completion_rate, self_formation_ratio, postmortem_recall_rate, cost_per_successful_apply, posture_stability_seconds); frozen `MetricsSnapshot` with `METRICS_SNAPSHOT_SCHEMA_VERSION=1` (PR #22145 → main `f98572b102`, 62 tests).
  - [x] P4 Slice 2 — `metrics_history.py` JSONL ledger at `.jarvis/metrics_history.jsonl` (env-overridable) with bounded reader (`MAX_LINES_READ=8192` clamps caller limit), 7d/30d time-window aggregator, `ConvergenceTracker`-backed window trend, oversize line dropped at write, malformed-line tolerance on read, 8-thread concurrent-append stress (PR #22162 → main `8d9b743b77`, 39 tests).
  - [x] P4 Slice 3 — `metrics_repl_dispatcher.py` `/metrics` REPL: 7 subcommands (`current`/`7d`/`30d`/`composite`/`trend`/`why <id>`/`help`) with **ASCII sparkline rendering** (`SPARKLINE_CHARS = "_.-=*#"`), shape-gated subcommand parsing (every shape mismatch → UNKNOWN_SUBCOMMAND with help), provider→ledger fallback resilience (PR #22180 → main `304a9a3a06`, 62 tests).
  - [x] P4 Slice 4 — `metrics_observability.py` (~720 LOC): `MetricsSessionObserver` post-VERIFY hook (compute → ledger append → atomic `summary.json` merge → SSE `metrics_updated` publish, all best-effort) + `register_metrics_routes(app)` (4 GET endpoints: `/observability/metrics{,/window?days=N,/composite,/sessions/{id}}`) + `EVENT_TYPE_METRICS_UPDATED` added to `_VALID_EVENT_TYPES` (PR #22193 → main `505444f465`, 41 tests).
  - [x] P4 Slice 5 — graduation: **`JARVIS_METRICS_SUITE_ENABLED` default flipped `false`→`true`** in all three owner modules (engine + repl_dispatcher + observability); `register_metrics_routes` wired into `EventChannelServer.start` (loopback-asserted, gated on master flag, shares the IDE router's rate-limit + CORS via dedicated helper instance) (this PR). Layered evidence: 204 deterministic Slice 1-4 tests + 38 graduation pins (master flag default-true × 3 owner modules + source-grep `"1"` literal × 3 + pre-graduation pin renames × 3 owner suites + EventChannelServer source-grep × 3 + cross-slice authority survival × 4 modules + reachability supplement) + 15 in-process live-fire smoke checks (observer end-to-end, all 4 GET endpoints reachable + return correct shape, all 3 REPL commands render, master-off revert proven). Hot-revert: single env knob — `JARVIS_METRICS_SUITE_ENABLED=false` and the observer short-circuits, the GET endpoints 403, SSE drops silently. **Phase 4 P4 COMPLETE — Wang's composite score + 5 net-new operator metrics now surfaced via summary.json + /metrics REPL + IDE GET + SSE event by default. The `INSUFFICIENT_DATA` problem statement that motivated this phase is resolved — operators can now answer "is O+V getting smarter?" with concrete data.**
  - [x] **P4 Slice 5 follow-up — harness MetricsSessionObserver wiring landed 2026-04-26.** Wires `MetricsSessionObserver.record_session_end` into `battle_test/harness.py` `_generate_report` between the recorder's `save_summary` call and the SessionReplayBuilder block (so the observer can MERGE its `metrics` block into the existing summary.json via read-modify-write, and replay.html sees the merged content). Reads `self._session_recorder._operations` for ops list, `self._cost_tracker.total_spent` for total cost, `branch_stats.get("commits", 0)` for commits; uses singleton `get_default_observer()` to share warned-once dedup state across multiple session-ends. Best-effort try/except (ImportError + bare Exception both swallowed) so an observer crash NEVER breaks `_generate_report`. Telemetry log surfaces ledger_appended + summary_merged + sse_published flags + notes. **Closes the deferred follow-up** noted in P4 Slice 5 graduation; every session-end now produces a metrics snapshot, appends to JSONL ledger, merges summary.json, and publishes SSE `metrics_updated`. Layered evidence: **17 wiring pins** (`tests/battle_test/test_harness_metrics_observer_wiring.py`) covering observer import + 5 expected kwargs (session_id / session_dir / ops / total_cost_usd / commits) + recorder._operations getattr + branch_stats.commits read + cost_tracker.total_spent read + ordering after save_summary / before SessionReplayBuilder + try/except shape + structured telemetry log + singleton-not-fresh-construction pin + 4 observer-contract integration smokes (signature surface + SessionObservation 5 fields + master-off short-circuit + minimal-inputs-no-raise) + master flag default-true preservation + SessionRecorder._operations field-shape pin. Combined regression spine: **221/221 tests green** across wiring + harness suites + metrics Slices 1-3. Hot-revert: same single env knob — `JARVIS_METRICS_SUITE_ENABLED=false` → observer short-circuits with `notes=("master_off",)` → wiring no-ops → summary.json unchanged.

**🎯 Phase 4 — Cognitive Metrics FULLY GRADUATED 2026-04-26.** Both items closed (P3 + P4). Phases 1-4 + Phase 0 all complete.
**Phase 5 — Adversarial Depth**: ✅ FULLY GRADUATED (P5)
  - [x] P5 Slice 1 — `adversarial_reviewer.py` primitive: 4-class system (`AdversarialFinding` + `AdversarialReview` + `build_review_prompt` + `parse_review_response` + `filter_findings` + `format_findings_for_generate_prompt`) with hallucination filter (drops empty/ungrounded/traversal references unconditionally) (PR #22233 → main `33b0ba6db1`, 60 tests).
  - [x] P5 Slice 2 — `adversarial_reviewer_service.py`: `AdversarialReviewerService` with **6 skip paths** (master_off / safe_auto / empty_plan / no_provider / provider_error / budget_exhausted), `ReviewProvider` Protocol + frozen `ReviewProviderResult`, `_AdversarialAuditLedger` JSONL writer at `.jarvis/adversarial_review_audit.jsonl`, cost budget at $0.05/op default per PRD spec (env-overridable), §8 telemetry log line (PR #22251 → main `7e7c255b8c`, 40 tests).
  - [x] P5 Slice 3 — `adversarial_reviewer_hook.py`: `review_plan_for_generate_injection` (full pipeline → `GenerateInjection`) + `inject_into_generate_prompt` (pure helper, two-blank-line delimiter) + `feed_review_to_bridge` (best-effort summary turn into ConversationBridge as `postmortem`-source for cross-op CONTEXT_EXPANSION recall; file list capped at 5 with `+N more`). PLAN authority structurally preserved — hook returns text only, never gates (PR #22260 → main `387466adbc`, 27 tests).
  - [x] P5 Slice 4 — `adversarial_observability.py`: `/adversarial` REPL (5 subcommands: current/history/why/stats/help with shape gating + 6-value status enum) + `register_adversarial_routes(app)` (4 GET endpoints: `/observability/adversarial{,/history?limit=N,/stats,/{op_id}}` mirroring P4 metrics shape) + `publish_adversarial_findings_emitted` SSE bridge + `EVENT_TYPE_ADVERSARIAL_FINDINGS_EMITTED` added to broker `_VALID_EVENT_TYPES` + `compute_stats` aggregator with skip_reason histogram. Read-only over Slice 2's JSONL ledger; pinned by `, "a"` / `, "w"` write-mode string absence (PR #22262 → main `5859a96cc0`, 58 tests).
  - [x] P5 Slice 5 — graduation: **`JARVIS_ADVERSARIAL_REVIEWER_ENABLED` default flipped `false`→`true`** in the single owner module (`adversarial_reviewer.py`); `register_adversarial_routes` wired into `EventChannelServer.start` (loopback-asserted, gated on master flag, dedicated `IDEObservabilityRouter` helper for shared rate-limit + CORS) (this PR). Layered evidence: 185 deterministic Slice 1-4 tests + 33 graduation pins (master flag default-true + source-grep `"1"` literal + pre-graduation pin rename + EventChannelServer source-grep × 3 + cross-slice authority survival × 4 modules + `EVENT_TYPE_ADVERSARIAL_FINDINGS_EMITTED` allow-list pin + reachability supplement) + **15 in-process live-fire smoke checks** (service skip-paths under default-on, audit row written, hook produces injection, all 5 REPL subcommands render, all 4 GET endpoints reach 200, master-off revert proven for service + REPL + endpoints). Hot-revert: single env knob (`JARVIS_ADVERSARIAL_REVIEWER_ENABLED=false`) → service returns `skip_reason="master_off"`, REPL renders DISABLED, GET endpoints 403, SSE drops silently, hook returns empty injection. **Phase 5 P5 COMPLETE — Iron Gate enforces hygiene + SemanticGuardian matches patterns + AdversarialReviewer thinks adversarially.**
  - [x] **P5 Slice 5 follow-up — orchestrator GENERATE wiring landed 2026-04-26.** Wires the Slice 3 hook (`review_plan_for_generate_injection`) into `phase_runners/plan_runner.py` at the post-PLAN/pre-GENERATE site (after `ctx.advance(OperationPhase.GENERATE)`, between Tier 5 Cross-Domain Intelligence and Tier 6 Personality voice — same try/except pattern as the sibling Adaptive Learning + Tier 5 + TestCoverageEnforcer injectors). Reads `ctx.implementation_plan` as `plan_text`, normalizes `ctx.risk_tier.name` (or None), passes `target_files` from ctx; defaults to the singleton service + bridge. Injection lands via `ctx.with_strategic_memory_context()` (invariant-safe setter, NOT `dataclasses.replace`) so PLAN authority is preserved by construction — the hook returns text only, never gates / advances / raises. Best-effort try/except (ImportError + bare Exception both swallowed). Telemetry log line surfaces findings count + bridge_fed flag. **Closes the deferred follow-up** noted in P5 Slice 5 graduation; AdversarialReviewer is now auto-invoked by the FSM during every non-SAFE_AUTO op. Layered evidence: **16 wiring pins** (`tests/governance/test_plan_runner_adversarial_wiring.py`) covering hook import + 4 expected kwargs + `implementation_plan` read + `.name` risk-tier conversion + `with_strategic_memory_context` (not replace) + ordering after GENERATE-advance + try/except shape + no-advance-no-PhaseResult-no-raise authority pin + telemetry log + section ordering after Tier 5 / before Tier 6 + master flag default-true preservation + 4 hook-contract integration smokes. Combined regression spine: **581/581 tests green** across wiring + adversarial Slices 1-4 + full Pass B suite. Hot-revert: same single env knob — `JARVIS_ADVERSARIAL_REVIEWER_ENABLED=false` → hook returns empty injection → wiring no-ops. Operator binding `feedback_*` policies preserved.

**🎯 Phase 5 — Adversarial Depth FULLY GRADUATED 2026-04-26.** P5 closed.

**Reverse Russian Doll Pass B — Order-2 governance cage**: ✅ STRUCTURALLY COMPLETE 2026-04-26 (defaults still false pending per-slice graduation cadence)
  - [x] Slice 1 — `Order2Manifest` schema + loader + 9 Body-only initial entries at `.jarvis/order2_manifest.yaml` + grep-pinned authority invariants (PR #22298 → main, 69 tests). `JARVIS_ORDER2_MANIFEST_LOADED` default false.
  - [x] Slice 2 — `ORDER_2_GOVERNANCE` risk class (5th value, strictly above BLOCKED via `auto()`) + `classify_order2_match()` pure function + `apply_order2_floor()` gated risk-floor application (PR #22320 → main, 47 tests). `JARVIS_ORDER2_RISK_CLASS_ENABLED` default false.
  - [x] Slice 2b — `apply_order2_floor` wired into `phase_runners/gate_runner.py` between MIN_RISK_TIER floor (step 9) and SAFE_AUTO preview (now step 11) (PR #22329 → main, 13 tests). DUAL-flag protected (manifest_loaded × risk_class_enabled).
  - [x] Slice 3 — `meta/ast_phase_runner_validator.py` 6-rule pure `ast.parse` walk (ABC conformance / phase attr / async run signature / no ctx mutation / top-level try/except / no banned imports) + `MAX_CANDIDATE_BYTES=256 KiB` (PR #22347 → main, 56 tests). `JARVIS_PHASE_RUNNER_AST_VALIDATOR_ENABLED` default false.
  - [x] Slice 4 — `meta/shadow_replay.py` ReplaySnapshot + ReplayCorpus + structural-equality diff via `DEFAULT_CTX_WHITELIST = {op_id, risk_tier, phase, target_files, candidate_files}` + initial seed corpus at `.jarvis/order2_replay_corpus/` (PR #22375 → main, 61 tests). `JARVIS_SHADOW_PIPELINE_ENABLED` default false.
  - [x] Slice 5 — `meta/meta_phase_runner.py` MetaPhaseRunner primitive composing Slices 1+3+4 evidence into one `MetaEvaluation` bundle; 7 status outcomes; **deliberately deferred candidate exec to Slice 6.1** (cage's whole point: arbitrary candidate Python is NOT compiled or evaluated without operator authorization) (PR #22396 → main, 33 tests). `JARVIS_META_PHASE_RUNNER_ENABLED` default false.
  - [x] Slice 6.1 — `meta/replay_executor.py` sandboxed candidate exec **resolves Slice 5 deferred problem**. Five preconditions (master flag + literal `operator_authorized=True` + size cap + parse/compile success + exactly-one PhaseRunner subclass with phase match). 35-name `__builtins__` allowlist; `asyncio.wait_for` timeout (5s default, 60s max); mock OperationContext with `__getattr__` + `advance(**kwargs)`; output diff via Slice 4's `compare_phase_result_to_expected` (PR #22475 → main, 47 tests). `JARVIS_REPLAY_EXECUTOR_ENABLED` default false.
  - [x] Slice 6.2 — `meta/order2_review_queue.py` append-only JSONL queue with sha256-tamper-detection per record + **locked-true cage invariant** `amendment_requires_operator()` returns True regardless of any env knob value (`JARVIS_ORDER2_MANIFEST_AMENDMENT_REQUIRES_OPERATOR=false` still returns True; logs warning for audit visibility). AST-pinned: function body must end with `return True` constant. Lifecycle: `PENDING_REVIEW → AMENDED | REJECTED | EXPIRED`. Cage rule on amend: at least one PASSED replay required (`NO_PASSING_REPLAY` rejection if zero) (PR #22517 → main, 59 tests). `JARVIS_ORDER2_REVIEW_QUEUE_ENABLED` default false.
  - [x] Slice 6.3 — `meta/order2_repl_dispatcher.py` `/order2 {pending,show,amend,reject,history,help}` REPL — **THE only caller in O+V that passes `operator_authorized=True`** to the replay executor (source-grep-pinned). Amend ceremony: queue lookup → defensive `amendment_requires_operator()` re-check → corpus availability → resolve applicable_snapshots from live corpus → run sandboxed replay for EVERY snapshot → require ≥1 PASSED → operator types reason → `queue.amend()` with full replay-results bundle as evidence. Help subcommand bypasses master flag (discoverability). 14-status enum (PR #22535 → main, 51 tests). `JARVIS_ORDER2_REPL_ENABLED` default false.

**Combined Pass B regression spine: 438/438 tests green across all 6 slices.** Defaults all still `false` pending per-slice 3-clean-session graduation cadence (W1 + W2(5) soak discipline). Pass B closure unblocked Pass C structurally.

**Reverse Russian Doll Pass C — Adaptive Anti-Venom**: 🚀 EXECUTION STARTED 2026-04-26 (Slice 1 shipped; defaults still false pending per-slice graduation cadence)
  - [x] Slice 4 — **combined** per-Order mutation budget + risk-tier ladder extender (this PR; per §8 design two sub-surfaces in one slice). **Slice 4a** = `adaptation/per_order_mutation_budget.py`: pure stdlib analyzer of `MutationUsageLite` events. Proposes lowering the per-Order mutation budget when ops consistently used fewer mutations than budgeted. Conservative: uses **max observed** as the proposed new budget (any op that needed N mutations in the window will still get N under the new budget). Order-2 hard floor `MIN_ORDER2_BUDGET=1` so Pass C never proposes a non-functional budget. Surface validator: kind=`lower_budget` + sha256-hash + threshold + summary-`→`-indicator. Master flag `JARVIS_ADAPTIVE_PER_ORDER_BUDGET_ENABLED` (default false). **Slice 4b** = `adaptation/risk_tier_extender.py`: pure stdlib analyzer of `PostmortemEventLite` events with `blast_radius` field. Identifies novel `failure_class` values (not in `DEFAULT_KNOWN_FAILURE_CLASSES = {infra, test, code, approval_denied, blocked}`) accumulating ≥ `JARVIS_ADAPTATION_TIER_THRESHOLD` (default 5) occurrences. Classifies blast_radius into 4 bands: 0.0-0.25 → SAFE_AUTO/NOTIFY_APPLY/HARDENED, 0.25-0.5 → NOTIFY_APPLY/APPROVAL_REQUIRED/HARDENED, 0.5-0.75 → APPROVAL_REQUIRED/BLOCKED/HARDENED, 0.75+ → APPROVAL_REQUIRED/BLOCKED/CRITICAL. Synthesizes deterministic tier name `<insert_after>_<SUFFIX>_<FAILURE_CLASS>` (uppercase, sanitized special chars, truncated at `MAX_TIER_NAME_CHARS=64`). Proposes insertion between two existing tiers — **strictly tightening** per §8.3: ladder grows; nothing on it is removed. Surface validator: kind=`add_tier` + sha256-hash + threshold + summary-contains-`insert`-or-`between`. Master flag `JARVIS_ADAPTIVE_RISK_TIER_LADDER_ENABLED` (default false). Both sub-surfaces auto-register their validators at module-import + are independently default-off. 63 regression pins (Slice 4a: 22, Slice 4b: 24, cross-surface: 5, plus 12 shared substrate-integration pins) covering all module constants + master-flag pins + dataclass-frozen + env-overrides + mine pipeline (empty / threshold / window-filter / per-Order independence / max-observed-as-proposal / Order-2-floor-pin / multi-class / known-class-skipped / idempotent proposal_id) + ledger integration (master-off / master-on / DUPLICATE on re-mine) + surface validators (registered-at-import / 4 reject paths each + valid pass each) + blast-radius classifier (4 bands) + tier-name synthesis (basic / sanitized / truncated / uppercase) + cross-surface authority invariants (no banned governance imports / no subprocess+network / distinct validator registration). Combined regression spine: **232/232 tests green** across Slice 1+2+3+4. Per §8.6: this slice graduates when both sub-surfaces have 5 clean sessions each (cumulative ladder).
  - [x] Slice 3 — `adaptation/exploration_floor_tightener.py` IronGate exploration-floor auto-tightener (this PR): pure stdlib analyzer of (exploration-score, verify-outcome) tuples per op. **Bypass-failure detector** (`floor_satisfied=True AND verify_outcome IN {regression, failed}`) — the structural signal that the exploration gate was bypassed and the cage was not strict enough. **Weakest-category identification** via per-op argmin (lowest-scoring category in each bypass-failure op) + group-count winner across the window (alpha tie-break for determinism). **Bounded 10% raise per cycle** via `compute_proposed_floor(current, pct=10)`: `current + ceil(current * pct/100)`, floor-shaped to MIN_NOMINAL_RAISE=1; defends against the math stalling on small floors. Per-cycle pct hard-capped at MAX_FLOOR_RAISE_PCT=100 to prevent operator-typo runaway (env override to 500% gets clamped). Auto-registers a per-surface validator at module-import enforcing: kind == "raise_floor" + proposed_state_hash sha256-prefixed + observation_count >= JARVIS_ADAPTATION_FLOOR_THRESHOLD (default 5, slightly higher than Slice 2's 3 because a floor-raise has broader impact than one detector pattern) + summary-contains-→-indicator (defense-in-depth against doctored proposals). Idempotent proposal_id (sha256 of category + current + proposed floor) so re-mining the same window's events yields DUPLICATE_PROPOSAL_ID at substrate. Master flag `JARVIS_ADAPTIVE_IRON_GATE_FLOORS_ENABLED` (default false). Per §7.1 design: "the candidate for floor tightening" (singular per cycle) — keeps the operator-review surface trim. 55 regression pins covering 6 module constants + 3 master-flag pins + 9 env-override pins (incl. invalid-falls-back + zero-falls-back + clamp-to-max for raise_pct) + 6 compute_proposed_floor math pins (basic 10% / min_nominal kicks in / higher pct / zero / negative / env-derived) + 2 bypass-filter pins (regression+failed kept; pass+l2_recovered excluded) + 4 weakest-category pins (per-op argmin + alpha tie-break + empty-input + skip-no-scores ops) + 9 mine_floor_raises_from_events end-to-end pins (empty / below-threshold / weakest-cat-below-threshold / qualifies / skip-non-bypass / window-filter / proposal_id-stable / proposal_id-differs) + 4 propose_floor_raises_from_events ledger pins (master-off / master-on / idempotent / observation_count matches bypass_count) + 6 surface validator pins (registered-at-import + 4 reject paths + 1 pass + idempotent install) + 4 authority invariants (no banned imports / substrate+stdlib-only / no subprocess+network / no LLM tokens) + 1 substrate integration pin. Combined regression spine: **169/169 tests green** across Slice 1+2+3.
  - [x] Slice 2 — `adaptation/semantic_guardian_miner.py` POSTMORTEM-mined patterns (this PR): pure stdlib-only longest-common-substring detector synthesizer + group-by-(root_cause, failure_class) + window filter + existing-pattern duplicate check + idempotent proposal_id (hash of group+pattern). `PostmortemEventLite` frozen dataclass = caller-supplied input shape (the miner does NOT read postmortem files itself — Slice 6 MetaGovernor will wire the source at window cadence per §4.3). End-to-end `propose_patterns_from_events()` flows through Slice 1's `AdaptationLedger.propose()`. Auto-registers a per-surface validator with the substrate at module-import enforcing: kind == "add_pattern" + proposed_state_hash starts with "sha256:" + observation_count >= JARVIS_ADAPTATION_PATTERN_THRESHOLD (default 3). Master flag `JARVIS_ADAPTIVE_SEMANTIC_GUARDIAN_ENABLED` (default false until Slice 2 graduation) gates `propose_patterns_from_events()` (returns empty list when off — substrate gate kicks in independently). Bounded synthesis: MAX_EXCERPTS_PER_GROUP=32 + MAX_SYNTHESIZED_PATTERN_CHARS=256 + MIN_LCS_LENGTH=8 + MIN_SYNTHESIZED_PATTERN_CHARS=8 (sub-3-char patterns would match anything; LCS bounded at 256 chars defends against multi-KB regex blob). Window filter: events older than `now - window_days*86400` dropped; epoch=0 back-compat retained for boot-time tests. 54 regression pins covering 6 module constants + 3 master-flag pins + 5 env-override pins + 6 LCS algorithm pins + 5 existing-pattern duplicate pins + 8 mine_patterns_from_events end-to-end pins + 5 propose_patterns_from_events pins (master-off / master-on / idempotency / evidence summary / existing-skip) + 5 surface validator pins (registered-at-import / 4 reject paths + 1 pass + idempotent install) + 4 authority invariants (no banned governance imports / substrate+stdlib-only / no subprocess+network / no LLM-call tokens) + 3 integration pins (substrate accepts what miner produces / proposal_id stable across calls / proposal_id differs for different patterns). Combined regression spine: **114/114 tests green** across Slice 1+2. Per Pass C §6.2: deterministic-only synthesizer per zero-LLM-in-cage invariant; LCS is the v1 algorithm; if it proves too narrow over time, operator can extend the synthesizer module via a normal Pass B Order-2 amendment (it IS governance code).
  - [x] Slice 1 — `adaptation/ledger.py` substrate: append-only JSONL audit log at `.jarvis/adaptation_ledger.jsonl` + 5-value `AdaptationSurface` enum (one per Pass C §3 thesis bullet: SemanticGuardian patterns / IronGate exploration floors / ScopedToolBackend mutation budget / risk-tier ladder / ExplorationLedger category weights) + 3-value `OperatorDecisionStatus` lifecycle + 2-value `MonotonicTighteningVerdict` (passed | rejected:would_loosen) + frozen `AdaptationProposal`/`AdaptationEvidence` dataclasses (sha256 tamper-detect per record) + pluggable per-surface validator registry (Slices 2-5 will register their semantic checks at module-import) + universal `validate_monotonic_tightening()` that **refuses to persist loosening proposals** (the cage's load-bearing rule per §4.1: Pass C cannot loosen via any path; loosening goes through Pass B `/order2 amend`). State transitions write NEW lines (append-only, never rewritten). Latest-record-per-proposal-id wins for current state. `approve()` is the ONLY transition that flips `applied_at` non-null. Stdlib-only import surface (AST-pinned). 60 regression tests covering module constants + 5 enums + dataclass-frozen pins + master-flag default-false + 7 propose paths (OK / DISABLED / 4 INVALID sub-cases / DUPLICATE / WOULD_LOOSEN with NOT-PERSISTED pin / surface-validator pass + reject + raise) + 6 decision paths + read queries (latest-wins / pending-excludes-terminals / history filter-by-surface) + persistence (append-only state-transitions / sha256 round-trip / tampered-record-skipped / malformed-json-skipped) + surface-validator routing pin + singleton + path-env-override + round-trip serialization + rollback_via field pin + 4 authority invariants (no banned imports / stdlib-only / no subprocess+network / loosening-NOT-persisted). `JARVIS_ADAPTATION_LEDGER_ENABLED` default false. Slices 2-6 pending.

Next open phase items per the Forward-Looking Priority Roadmap: **Reverse Russian Doll Pass C** (Adaptive Anti-Venom, structurally unblocked but design held pending operator authorization) → **Phase 6 P6** (Self-narrative, long-horizon).
**Phase 6 — Self-Modeling**: [ ] not started — long-horizon (3-6 months per PRD).

Update discipline: each closing slice updates this section in the same PR. Status is the source of truth for "what's next" — when in doubt, the lowest-numbered `[ ]` row in the lowest-numbered active phase is the next slice.

### Forward-Looking Priority Roadmap (chronological, impact-weighted)

This section is the **canonical "what's next" ordering** for everything still on the board after Phase 3's full graduation (2026-04-26). Each item is scored by:
- **Impact**: how much the operator-visible system improves once the item lands.
- **Dependency depth**: how many downstream items it unblocks (a high-leverage prerequisite outranks a high-impact terminal).
- **Cost-of-delay**: how stale / load-bearing the item becomes if deferred (Order-2 governance designed against a moving Order-1 target ages badly).
- **Risk surface**: novel cognitive layers (P5/P6/Pass C) ship safer when the measurement substrate (P4) is in place to detect regressions.

The list below resolves the four-way ordering for: **P4** (Convergence metrics), **P5** (Adversarial reviewer), **P6** (Self-narrative), **Reverse Russian Doll Pass B** (Order-2 governance), **Reverse Russian Doll Pass C** (Adaptive Anti-Venom). Anything not in this list is either complete or out of scope.

---

#### Priority 1 — Phase 4 P4: Convergence metrics suite ⭐ **NEXT**

**Why first**: foundational — everything below this line needs measurable convergence to claim it worked.

- **Impact (high)**: replaces `convergence_state: "INSUFFICIENT_DATA"` (the most-cited "unprovable" gap in the RSI claim) with 7 concrete metrics + Wang's composite score. Operator gets `/metrics 7d`, IDE GET `/observability/metrics`, SSE `metrics_updated` event. Every `summary.json` gets a `metrics: {…}` block.
- **Dependency leverage (highest)**: unblocks P5 (need metrics to validate that AdversarialReviewer findings actually move the score) and P6 (self-narrative needs metric history to narrate). Also un-strands the existing `composite_score.py` + `convergence_tracker.py` primitives (305 + 354 LOC already on disk, currently unsurfaced).
- **Cost of delay (high)**: every Phase 5/6 PR shipped without P4 gets a "did this actually help?" question we can't answer. Ad-hoc metrics added later won't be cross-comparable to the framework spec.
- **Risk (low)**: pure observability layer. No authority crossings. Hot-revert single env knob. Mirrors Phase 4 P3 cognitive_metrics graduation pattern (proven 2026-04-26).
- **Scope**: 5 slices (~1,230 LOC + ~145 tests + graduation pins + live-fire). See §9 P4.
- **Status**: 📋 plan briefed; Slice 1 (`metrics_engine.py` primitive) starting.

#### Priority 2 — Phase 5 P5: AdversarialReviewer subagent

**Why second**: highest-impact NEW cognitive layer the system can grow once measurable.

- **Impact (high)**: closes the "Iron Gate enforces hygiene; SemanticGuardian matches patterns; *neither thinks adversarially*" gap. AdversarialReviewer activates post-PLAN/pre-GENERATE, prompted as "find at least 3 failure modes," structured findings injected into GENERATE prompt as `Reviewer raised:`. Catches the class of bug that passes static analysis + tests but fails on a thoughtful read.
- **Dependency leverage (medium)**: P4 metrics let us prove AdversarialReviewer findings → composite score moves up. Without P4 baseline this is unprovable folklore.
- **Cost of delay (medium)**: the system is currently shipping plans that escape adversarial review entirely; deferring keeps a known cognitive gap open.
- **Risk (medium-high)**: novel side-stream Claude call (cost-budgeted at $0.05/op default). Reviewer hallucinations are a real failure mode (must reference specific files; ungrounded findings filtered). Telemetry-heavy.
- **Scope**: ~1,000 LOC + 40 tests per PRD §9; will need 4-5 sub-slices: (1) `adversarial_reviewer.py` primitive (Tier -1 sanitized findings JSON), (2) Claude side-stream caller + cost budget enforcement, (3) GENERATE-prompt injection wiring, (4) telemetry + REPL surface, (5) graduation.
- **Status**: ❌ not started (PRD §9 P5 spec landed; no code yet).

#### Priority 3 — Reverse Russian Doll Pass B: Order-2 governance (Order-1 freeze) ✅ STRUCTURALLY COMPLETE

**Status (2026-04-26)**: ✅ **ALL 6 SLICES SHIPPED.** 438/438 tests green. Defaults still `false` pending per-slice graduation cadence (W1 + W2(5) soak discipline).

- **Impact (high, delivered)**: the **Order-2** vocabulary is now in the live system: `MetaPhaseRunner` + Order-2 manifest + `ORDER_2_GOVERNANCE` risk class + AST validator + shadow replay + locked-true amendment protocol + sandboxed replay executor + `/order2` REPL. Distinguishes "the FSM ran a phase" (Order-1) from "the rules-of-the-FSM changed" (Order-2). The architectural prerequisite for Pass C is now in place.
- **Dependency leverage**: Pass C is now structurally unblocked.
- **Cost of delay**: NONE — closure landed 2026-04-26 in 9 PRs (#22298 / #22320 / #22329 / #22347 / #22375 / #22396 / #22475 / #22517 / #22535).
- **Risk discipline that held throughout**: every slice shipped behind a master flag default `false`; AST-pinned authority invariants on every module (no banned governance imports); locked-true `amendment_requires_operator()` cage invariant; `operator_authorized=True` only ever passed by `/order2 amend` (source-grep-pinned).
- **Cage's whole point** (Slice 5 deferred → Slice 6.1 resolved): arbitrary candidate Python is NOT compiled or evaluated without operator authorization. The replay executor is the ONLY place in O+V that compiles a candidate `PhaseRunner` subclass, and only when 5 preconditions (master flag + literal `operator_authorized=True` + size cap + parse/compile success + exactly-one PhaseRunner subclass with phase match) are all met.
- **Scope shipped**: 6 slices (Slice 6 split into 6.1/6.2/6.3 mid-arc); 438 regression tests; full Pass B regression spine green.
- **Next**: per-slice graduation cadence (3-clean-session arc per slice). All 9 master flags can flip independently.

#### Priority 4 — Reverse Russian Doll Pass C: Adaptive Anti-Venom (NOW UNBLOCKED)

**Why fourth**: this is **the** novel architectural contribution per `memory/project_reverse_russian_doll_pass_a.md` ("Anti-Venom adaptive thesis is genuinely novel"). The Pass B prerequisites have all landed; only operator authorization remains.

- **Impact (highest long-term, lowest short-term)**: the system **adapts its own gates** within bounds — `AdaptationLedger` + 5 surfaces (SemanticGuardian patterns, IronGate floors, per-Order budgets, risk-tier ladder, ExplorationLedger weights) + `MetaAdaptationGovernor`. This is the closest existing item to genuine RSI: not just *measuring* convergence (P4) but *responding to it by tightening rules*. Per `memory/project_reverse_russian_doll_pass_c.md`: "monotonic-tightening invariant; operator-approval required; zero-LLM in cage." The "zero-LLM in cage" constraint is what makes it provably safe.
- **Dependency leverage (none — terminal)**: nothing downstream depends on Pass C in the current PRD. It's the **endgame** of the cognitive-substrate work.
- **Cost of delay (low)**: every other item in this list ships value before Pass C. Pass C is the multiplicative finisher, not a prerequisite for anything else.
- **Risk (highest)**: the system actively rewrites its own governance gates. The full safety stack (monotonic-tightening invariant + operator-approval per amendment + zero-LLM-in-cage + AdaptationLedger immutable audit) is what makes this defensible — but every safety pin must hold under live operation.
- **Scope**: 6 slices per `memory/project_reverse_russian_doll_pass_c.md` draft.
- **Status**: 📋 DESIGN COMPLETE; **structurally unblocked 2026-04-26 (Pass B Slice 1 + 6 prerequisites all landed)**; execution held pending operator authorization.

#### Priority 5 — Phase 6 P6: Behavior summarizer + self-narrative

**Why last (not lowest impact — longest horizon)**: PRD §9 P6 explicitly tags this as **"target: 3–6 months, long-horizon"**. The reason is depth, not lack of value: self-narrative requires sustained metric history (P4) + adversarial-finding history (P5) + Order-2 amendment history (Pass B/C) to have anything substantial to narrate.

- **Impact (medium-high but slow-burning)**: system gets a model of its own behavior over time. Operator + audit get a "who is this AI becoming?" view that's grounded in actual data, not anthropomorphism.
- **Dependency leverage (low)**: nothing else depends on P6.
- **Cost of delay (low)**: per the PRD's own 3-6 month horizon, this is intentionally back-loaded.
- **Risk (medium)**: surface is small but the failure mode (self-narrative drifting from actual behavior, becoming hallucinated) requires careful pinning. Easier once P4 metrics provide ground truth.
- **Status**: ❌ not started; do not start until P4 + P5 are graduated.

---

#### Cross-priority sequencing rules (binding)

1. ✅ **Pass C unblocked from Pass B's primitives** (2026-04-26 — all 6 Pass B slices shipped). Original rule "Never ship Pass C before Pass B Slice 1" is now structurally satisfied.
2. ✅ **Pass B prerequisites met** (W2(5) Slice 5b dependency was deferred-then-resolved during Pass B execution; all Pass B slices landed without further W2(5) blocks).
3. **P4 first, always.** Every novel cognitive layer (P5, Pass B, Pass C, P6) needs the metric substrate to claim it worked. ✅ Satisfied — Phase 4 P4 graduated 2026-04-26.
4. ✅ **P5 shipped before Pass B/C.** Adversarial findings + metrics are natural inputs to `MetaAdaptationGovernor`'s "should we tighten?" decision (P5 graduated 2026-04-26, before Pass B closed).
5. **P6 after the adaptive substrate exists.** Narrating a system that doesn't adapt is less interesting than narrating one that does. Still binding — P6 deferred until Pass C ships.

The "lowest-numbered `[ ]` row" heuristic (above) still applies *within* a phase. This priority list is the **between-phase** ordering when multiple phases are simultaneously eligible.

---

## 2. Vision Statement

> *"O+V is proactive and not reactive. Its job is to explore the codebase like CC does and develop the JARVIS repo on its own without any human intervention (only if necessary, based on context and severity). It should also understand the direction I'm going and the goal I'm trying to achieve on its own. I want O+V to have the most advanced intelligent capabilities possible — and to be the proactive autonomous version of CC."*
>
> — Derek J. Russell, operator binding

### Operationalized as success criteria

The vision delivers when:

1. **Self-initiating** — O+V begins useful work without human prompting (✅ delivered: 16 sensors)
2. **Codebase exploration parity with CC** — same depth of read/search/reason as CC's tool loop (⚠️ partial: Iron Gate enforces hygiene, not curiosity)
3. **Repo development without intervention** — multi-file changes ship end-to-end autonomously (⚠️ proven once, Sessions Q-S)
4. **Human-in-loop only when severity demands** — risk-tier ladder + curiosity ask_human (✅ delivered)
5. **Understands operator direction + goal** — without being told (❌ shallow: DirectionInferrer reads env signals, not intent)
6. **A-level execution** — sustained quality + reliability + learning (❌ not yet)

---

## 3. Current State Assessment

### 3.1 What O+V uniquely does (the cognitive delta from CC)

| Capability | Implementation | Maturity |
|---|---|---|
| Self-initiating work loop | 16 sensors → UnifiedIntakeRouter → 11-phase FSM | ✅ production |
| Multi-tier provider routing with deterministic budget math | UrgencyRouter (5 routes, sub-ms) | ✅ production |
| Cost as first-class governance dimension | cost_governor with route × complexity × headroom × parallel_factor | ✅ production |
| Posture-aware behavior | DirectionInferrer + StrategicPosture (4 values: EXPLORE/CONSOLIDATE/HARDEN/MAINTAIN) | ✅ production |
| Multi-file coordinated generation with batch rollback | `files: [...]` schema + ChangeEngine | ✅ production |
| L3 worktree isolation for parallel fan-out | git worktree per unit, `reap_orphans` on boot | ✅ production |
| Auto-commit with O+V signature | AutoCommitter + protected-branch guards | ✅ production |
| Cross-session memory (3 surfaces) | UserPreferenceMemory + SemanticIndex + LastSessionSummary | ✅ production |
| Per-op POSTMORTEM with root-cause classification | CommProtocol 5-phase + PostmortemEngine | ✅ production (but unused — see §4) |
| Deterministic financial circuit-breaker | cost_governor + Class E watchdog cancel | ✅ production |
| L3 mode self-protection + auto-recovery | SafetyNet + #20147 resilience pack | ✅ production |
| Mid-op cancellation infrastructure | W3(7) cancel-token (REPL + watchdog + signal) | ✅ production |
| Parallel L3 fan-out with cost-aware cap | parallel_dispatch + #19800 cost-cap parallel-stream bump | ✅ production |

### 3.2 What CC genuinely beats O+V on (and we should port)

| Capability | CC | O+V | Priority |
|---|---|---|---|
| Conversational mode — natural dialog | ✅ first-class | ✅ `/chat <message>` + bare-text + 4-intent classifier (PRD §9 P2, graduated 2026-04-26) | — |
| Real-time token streaming with model thinking visible | ✅ always | ⚠️ phases only in headless | P3 |
| Lightweight approval UX | ✅ inline `[y/N]` | ✅ inline `[y]/[n]/[s]/[e]/[w]` (PRD §9 P3, graduated 2026-04-26) | — |
| Easy mid-flight redirect | ✅ "wait, do this instead" | ❌ requires `/cancel` | P5 |
| Status line with current activity | ✅ always | ⚠️ requires opt-in dashboard | P4 |
| Conversational context across turns | ✅ default | ⚠️ ConversationBridge default-off | **P2** |
| MCP tool ecosystem visibility | ✅ first-class | ⚠️ integrated, thin discovery | P5 |
| Skills/workflows surface (saved playbooks) | ✅ rich | ❌ none | P5 |
| Background tasks with notify | ✅ run_in_background | ⚠️ internal only | P4 |
| `/help` discoverability of slash commands | ✅ rich | ⚠️ FlagRegistry exists, /help thin | P3 |

### 3.3 Production track record

- **Verified end-to-end multi-file APPLY**: 1 (Sessions Q-S, 2026-04-15, 4 test modules generated → applied → committed)
- **Single-file APPLYs**: handful (most recent: Session O, 2026-04-15)
- **NO_OP terminations**: common (model decides no change needed)
- **EXHAUSTION terminations**: common (provider transport)
- **Sessions completed cleanly with at least 1 commit**: small fraction of total session-hours

The infrastructure is exceptional. The cognitive workload it serves is sparse. **That gap is the work to do.**

### 3.4 Wave 1 + Wave 2 + Wave 3 — what's already on main

For context on what's available as substrate for Phases 1-6:

| Wave | What | Status |
|---|---|---|
| W1 #1 | DirectionInferrer + StrategicPosture | graduated 2026-04-21 |
| W1 #2 | FlagRegistry + /help dispatcher | graduated 2026-04-21 |
| W1 #3 | SensorGovernor + MemoryPressureGate | graduated 2026-04-21 |
| W2 (4) | Curiosity Engine (ask_human widening) | graduated 2026-04-25 |
| W2 (5) | PhaseRunner extraction (8 phases) | graduated 2026-04-23 |
| W3 (6) | Parallel L3 fan-out | architecturally complete; FINAL gated on external API stability |
| W3 (7) | Mid-op cancellation | graduated 2026-04-25 |
| Resilience pack | #19706 outer-retry + #19800 cost-cap parallel + #20147 L3 auto-recovery | merged 2026-04-25 |

Phases 1-6 build on this substrate. Nothing in the roadmap requires re-architecting these primitives.

---

## 4. The Cognitive Scaffolding Gap (Deep Dive)

This section exists because the term "cognitive gap" is ambiguous. It does **NOT** mean the LLM provider is insufficient. Claude (and DW when healthy) is plenty smart — when the seed reaches GENERATE under stable API conditions, the model reads multiple files, reasons about multi-file dependencies, produces coherent multi-file patches with rationale, and self-corrects on validate failures via L2 repair.

The cognitive gap is **internal to JARVIS** — the orchestration layer that converts provider intelligence into self-directing, self-improving behavior is shallow.

### 4.1 The lab analogy

Claude is a brilliant scientist. JARVIS is the lab around the scientist.

- The lab is **exceptional** — instruments (16 sensors), safety interlocks (Iron Gate, risk-tier-floor, cost-governor), observability (41 SSE events + 10+ JSONL ledgers + replay.html), multi-tenancy (L3 worktree isolation), financial circuit-breakers (cost-governor with parallel-stream bump), audit trails (CommProtocol 5-phase), autonomous experiment runners (16 sensors).
- The lab does **not** have a research agenda generator. It runs whichever experiments the operator (or sensors triggered by hardcoded conditions) writes down.
- The scientist is fully capable of forming new hypotheses; **the lab just doesn't ask them to.**

### 4.2 The six concrete cognitive shallows

Each is a closeable gap. The primitives exist. They aren't yet wired into self-referential loops.

#### Shallow 1: No goal formation

**Symptom**: Sensors react to operator-written backlog entries or hardcoded triggers (test failures, doc staleness). The system doesn't form hypotheses like "I notice VALIDATE fails 40% of multi-file ops in `governance/intake/` — let me investigate."

**Primitive that's missing**: a service that observes patterns in POSTMORTEM clusters and SemanticIndex centroid drift, then proposes its own backlog entries.

**Closed by**: Phase 2 → P1 (Curiosity Engine v2 — model writes backlog entries).

#### Shallow 2: No closed feedback loop

**Symptom**: POSTMORTEM produces detailed records. **Nothing reads those records at the next op's CLASSIFY/PLAN time.** The system has perfect memory and zero recall.

**Concrete example**: Op X fails with `validation_failed: missing test coverage`. The postmortem says exactly that. The next time a similar op runs, **nothing reads that postmortem**. The system makes the same mistake, writes the same postmortem, learns nothing.

**Primitive that's missing**: a query layer over POSTMORTEM history, surfaced at decision time.

**Closed by**: Phase 1 → P0 (POSTMORTEM → next-op recall via SemanticIndex similarity).

#### Shallow 3: No self-modeling

**Symptom**: SemanticIndex builds a centroid of work but uses it for prompt injection only. The system doesn't model its own behavior over time ("I tend to fail at X; let me preempt").

**Concrete example**: When SemanticIndex sees 80% of recent work is in `governance/intake/`, it injects that into the next prompt as context. **It doesn't say "the operator's clearly working on intake — should I propose backlog entries that would advance that work?"**

**Primitive that's missing**: a behavior summarizer that consumes POSTMORTEM + commits + metrics + posture history into a periodic "who am I becoming" document.

**Closed by**: Phase 6 → P6 (Behavior summarizer + self-narrative).

#### Shallow 4: No reflective improvement on architecture

**Symptom**: O+V can edit any code in the repo but cannot meaningfully reason about *its own architecture*. Iron Gate, risk-tier-floor, cost-governor are operator-modified only.

**Why this matters for RSI**: real RSI requires the system to be able to modify its own scoring functions, gates, and policies — with structural proofs of safety preservation. Wang's framework allows this *in theory*; we don't yet allow it *in practice*.

**Primitive that's missing**: a meta-modification path that lets the system propose changes to its own governance layer, gated by extra-strict adversarial review.

**Closed by**: Phase 5 (Adversarial Depth) + Phase 6 (Self-Modeling) compositionally. Not a single phase.

#### Shallow 5: No internal adversary

**Symptom**: Iron Gate is hygiene checks ("did you read 2 files?"). SemanticGuardian is pattern matching ("does this code remove an import that's still referenced?"). Neither is a model trying to break the plan adversarially.

**Concrete example**: When PLAN proposes a 3-file refactor, no part of the system asks "what's the most likely way this fails? what edge case is the model glossing over?" We rely on the original model + tests + Iron Gate. Wang's RSI framework explicitly requires multi-perspective scoring; we have one perspective.

**Primitive that's missing**: an adversarial reviewer subagent that's prompted "find at least 3 ways this plan will fail."

**Closed by**: Phase 5 → P5 (Adversarial reviewer subagent).

#### Shallow 6: No convergence metric that means anything

**Symptom**: `convergence_state: "INSUFFICIENT_DATA"` tells the truth — there's no measure that the system is getting smarter. Wang's paper specifies a composite score that should be *non-decreasing* over RSI iterations; we don't compute one.

**Primitive that's missing**: a unified score function that composes test-pass-rate, coverage, complexity-delta, lint, semantic-drift, and other quality signals into a single number per op. Already designed in `RSI_CONVERGENCE_FRAMEWORK.md` (Improvement 1).

**Closed by**: Phase 4 → P4 (Cognitive metrics suite, includes the composite score) + Phase 1 → P0 (gives it data to consume).

### 4.3 Why provider quality is *not* the bottleneck

If you swapped Claude for GPT-5 or Gemini 3 tomorrow, the *throughput* might improve and the *quality of single-op output* might shift slightly. But the cognitive depth of O+V wouldn't change because **the loops that would use that intelligence don't exist yet**.

Conversely, if you wired the closed feedback loop (P0), even today's Claude would produce dramatically smarter behavior because it'd be *learning across ops* instead of starting fresh each time.

The gap to A-level cognition is entirely within our control. It's not waiting on Anthropic to ship a better Claude. It's waiting on us to wire the existing primitives into closed loops.

---

## 5. RSI Convergence Framework — Where We Are on the Wang Curve

JARVIS already has a comprehensive RSI architecture document at `docs/architecture/RSI_CONVERGENCE_FRAMEWORK.md` (mapping Wenyi Wang's *"A Formulation of RSI & Its Possible Efficiency"* (UBC, arXiv:1805.06610)) onto Ouroboros. This PRD section gives a status read on that framework + maps each PRD phase to the Wang improvements.

### 5.1 Wang's 6 improvements (per RSI_CONVERGENCE_FRAMEWORK.md)

| # | Improvement | Purpose | What it produces |
|---|---|---|---|
| 1 | **Composite Score Function** | Unify pytest, coverage, complexity, lint, semantic-drift into one number per op | `composite_score.py` |
| 2 | **Convergence Monitoring** | Detect logarithmic improvement (healthy), plateau, or oscillation | `convergence_tracker.py` |
| 3 | **Adaptive Graduation Threshold** | Replace fixed "3 successful uses" with probabilistic quality gate | modifies `graduation_orchestrator.py` |
| 4 | **Oracle Pre-Scoring** | Fast approximate quality check before full validation | `oracle_prescorer.py` |
| 5 | **Transition Probability Tracking** | Empirical data on which of the 9 self-evolution techniques work | `transition_tracker.py` |
| 6 | **Vindication Reflection** | After validation: "will this make future patches better?" not just "does it pass tests?" | `vindication_reflector.py` |

### 5.2 Current RSI implementation status (verify before citing)

Per memory (`project_rsi_convergence.md`, 2026-04-06): "6 improvements planned and documented, pending implementation." The architecture doc exists; **implementation status of the 6 modules requires code verification before any phase work begins**.

This PRD's Phase 1 (P0 — POSTMORTEM recall) is **partially overlapping with Improvements 2 (Convergence Monitoring) and 6 (Vindication Reflection)**. When Phase 1 starts, the first task is auditing what exists vs what the RSI doc plans — to avoid duplicate work.

### 5.3 PRD phases mapped to Wang improvements

| PRD Phase | Wang Improvement(s) | Relationship |
|---|---|---|
| P0 (POSTMORTEM recall) | #6 Vindication Reflection | Both wire postmortem outputs into next-op decisions |
| P0.5 (arc-aware DirectionInferrer) | — | Adjacent — DirectionInferrer is JARVIS-specific, Wang doesn't address strategic posture |
| P1 (Curiosity v2 — self-formation) | partially #5 Transition Tracking | Self-formed goals can use empirical technique-success data |
| P1.5 (HypothesisLedger) | #2 Convergence Monitoring | Hypothesis validation rate IS a convergence signal |
| P2 (Conversational mode) | — | UX layer; orthogonal to Wang |
| P3 (Lightweight approval) | — | UX layer; orthogonal |
| P4 (Cognitive Metrics) | #1 Composite Score + #2 Convergence Monitoring | The metrics suite IS Wang's score function + monitoring |
| P5 (Adversarial reviewer) | partially #4 Oracle Pre-Scoring | Both add a fast-approximate quality check before full pipeline |
| P6 (Self-Modeling) | #3 Adaptive Graduation + #5 Transition Tracking | Self-narrative consumes graduation + transition data |

### 5.4 Minimum Viable RSI definition

Per Wang's theorems, a system has RSI when its composite score is non-decreasing over iterations AND the rate of improvement is at least logarithmic (vs polynomial decay).

For O+V, this translates to:
- **Composite score per op**: implemented (P4)
- **Score persisted across sessions**: implemented (cross-session metrics history)
- **Rolling 30-day score trend computable**: implemented (P4)
- **Trend NOT decreasing OR oscillating wildly**: empirically observed across ≥ 2 weeks of operation
- **Self-formed goals contribute positive score on validation**: empirically observed (P1 + P1.5 needed)

When ALL 5 hold, we can claim "MVP RSI" with mathematical grounding (not just architectural claim).

### 5.5 RSI gap analysis

| Wang requirement | O+V status | Closed by phase |
|---|---|---|
| Single composite score function | designed, status TBD | P4 |
| Score-driven graduation | static "3 successful uses" | P4 (via Wang improvement 3) |
| Convergence monitoring | none | P4 |
| Pre-scoring (cheap quality gate) | none | P5 (adversarial reviewer fills similar role) |
| Transition probability tracking | none | P1 + future Wang improvement 5 implementation |
| Self-reflection on improvement trajectory | none | P6 |

### 5.6 The convergence threshold

Wang's paper proves RSI systems converge in *O(log n)* steps under specific assumptions. For O+V to credibly claim convergence:
- Need ≥ 100 ops with composite score recorded
- Need 30-day rolling score trend with ≥ 2σ above null
- Need ≥ 3 self-formed goals that improved score (proves the loop closes)

We're currently at 0 / 0 / 0. Phase 4 + Phase 2 close the gap.

---

## 6. Target State (A-Level Execution from A-Level Vision)

### Definition of A-level

| Dimension | A-level signal |
|---|---|
| Autonomous initiation | ≥ 3 self-formed goals per session that wouldn't have been written by a human operator |
| Cross-session learning | POSTMORTEM-driven prompt changes visible in ≥ 30% of subsequent ops |
| Reliability | ≥ 90% session completion rate (clean stop_reason, no infra waivers) |
| Throughput | Sustained ≥ 1 commit per 30 min of session wall-clock |
| Operator UX | < 30s from "I want X" → "X is being worked on" via conversational mode |
| Cognitive depth | Self-modeling layer producing a behavior summary the operator can read |
| RSI convergence | Composite score trend non-decreasing over rolling 30 days |

None of these are met today. All of them are implementation-feasible.

### Anti-goals (what A-level is NOT)

- **NOT** "model is smarter" — provider quality is fine
- **NOT** "more sensors" — we have enough; they need to be smarter
- **NOT** "more env knobs" — we have 481+; we need fewer with better defaults
- **NOT** "bigger context windows" — we already use 1M; the question is what we put in them
- **NOT** "more LLM calls" — cost discipline matters
- **NOT** "ship faster" — quality compounds; mistakes don't

---

## 7. Strategic Pillars

The roadmap organizes around 5 pillars. Each priority maps to one or more pillars.

### Pillar 1: **Self-Reading** (the loop reads its own outputs)

The system already produces structured POSTMORTEM, SemanticIndex centroids, ConversationBridge buffers, StrategicPosture history, and 41 SSE event types. **None of these flow back into decision-making at the right moments.** The first pillar is wiring those outputs back into inputs.

### Pillar 2: **Self-Direction** (the system forms its own goals)

Today sensors trigger ops. The system should also form goals from postmortem patterns, semantic clusters, and direction inference. Curiosity engine v2 = the model writes backlog entries.

### Pillar 3: **Operator Symbiosis** (CC-class UX in autonomous mode)

The vision is "proactive autonomous CC." We've built proactive autonomy. We need to recover the CC-class operator experience that was traded away — conversational mode, lightweight approvals, real-time visibility, redirect mid-flight.

### Pillar 4: **Cognitive Metrics** (we measure what matters)

Replace `INSUFFICIENT_DATA` with concrete signals: completion rate, learning evidence, semantic drift, self-formation ratio, composite score. Dashboard them. Optimize against them. **This pillar makes Wang's RSI convergence claim measurable.**

### Pillar 5: **Adversarial Depth** (an internal opponent)

Iron Gate is hygiene. SemanticGuardian is pattern matching. Add a model adversary that tries to break each plan before it executes. Catches subtle errors hygiene gates miss.

---

## 8. Governing Philosophy Alignment (Manifesto + 7 Principles)

Per `CLAUDE.md`, JARVIS is bound by 7 governing principles. Each PRD pillar maps to (and must preserve) those principles:

| # | Principle (from CLAUDE.md) | What it means for new cognitive layers |
|---|---|---|
| 1 | **Unified organism** — tri-partite microkernel, single entry point | New services compose into existing FSM; no parallel pipelines |
| 2 | **Progressive awakening** — adaptive lifecycle, no blocking boot chains | New services are best-effort at boot; failure must not block GLS.start |
| 3 | **Asynchronous tendrils** — structured concurrency, no event loop starvation | New services use existing pool / scheduler; no blocking calls on event loop |
| 4 | **Synthetic soul** — episodic awareness, cross-session learning | Phase 1 + Phase 6 directly serve this principle |
| 5 | **Intelligence-driven routing** — semantic, not regex; DAGs, not scripts | UrgencyRouter / DirectionInferrer / SemanticIndex are the substrate |
| 6 | **Threshold-triggered neuroplasticity** — Ouroboros: detect gaps, synthesize, graduate | Phase 2 (self-formation) is the most direct expression |
| 7 | **Absolute observability** — every autonomous decision is visible | Per-phase telemetry requirements (§10) make this enforceable |

**Zero-shortcut mandate** (also from CLAUDE.md): *"No brute-force retries without diagnosis. No hardcoded routing tables. Structural repair, not bypasses."* — this PRD's roadmap respects this; every phase has a diagnostic component (telemetry + tests) before behavioral change.

---

## 9. Roadmap (Phased, Impact-Ranked)

### Phase 1 — Self-Reading (target: 4–6 weeks)

**Goal**: System consults its own past outputs at decision time.

**Pre-Phase audit required**: verify which Wang improvements (per `RSI_CONVERGENCE_FRAMEWORK.md`) are already in code. If `vindication_reflector.py` exists and works, P0 reduces to wiring it into CLASSIFY/PLAN. If not, build the recall service from scratch using SemanticIndex.

#### P0 — POSTMORTEM → next-op strategy

**Problem**: Postmortems describe failures in detail and then sit in `.ouroboros/sessions/<id>/`. Nothing reads them.

**Solution**: at CLASSIFY/PLAN phase entry, query "what postmortems exist for ops similar to current op X?" via SemanticIndex similarity. Inject up to 3 relevant lessons into the GENERATE prompt. Same channel as ConversationBridge.

**Acceptance criteria**:
- New `PostmortemRecallService` queries SemanticIndex by op signature
- GENERATE prompt includes a "Lessons from prior similar ops" section when matches exist
- Telemetry: `[PostmortemRecall] op=X matched N postmortems (similarity ≥ threshold), injected K`
- Off-master flag `JARVIS_POSTMORTEM_RECALL_ENABLED` (default false → graduated true after 2 weeks of proof)

**Edge cases**:
- No matches → no injection, no log noise (silent)
- Stale postmortems (>30 days) → time-decay weight in similarity
- Recall could mislead when codebase has changed substantially since the postmortem; mitigate via commit-window filter
- Privacy: postmortems may reference operator preferences; ensure UserPreferenceMemory's redaction rules apply

**Why P0**: this single change converts O+V from "executes intent" to "learns from itself." Without it, the RSI claim is aspirational.

**Effort**: ~600 LOC + 30 tests. Builds on existing SemanticIndex + ConversationBridge primitives.

#### P0.5 — Cross-session direction memory

**Problem**: DirectionInferrer reads current-session signals only. Operator's actual long-arc direction (from git log + LSS) isn't fed back.

**Solution**: extend DirectionInferrer to consult LastSessionSummary + 100-commit git momentum. Posture decisions become arc-aware, not point-in-time.

**Acceptance criteria**:
- DirectionInferrer reads LSS + recent commit history at evaluation time
- Posture decisions logged with both immediate signals AND arc context
- `/posture explain` REPL command shows the arc reasoning

**Effort**: ~200 LOC + 12 tests.

### Phase 2 — Self-Direction (target: 6–10 weeks)

**Goal**: System forms its own goals, not just operator/sensor-written ones.

#### P1 — Curiosity Engine v2 (model writes backlog entries)

**Problem**: W2(4) Curiosity Engine (graduated 2026-04-25) lets the model ask the operator a question. The next slice should let the model **ask itself a question** by autonomously enqueuing a backlog entry.

**Solution**: new mechanism — when POSTMORTEM clusters reveal a recurring pattern (e.g., 3+ similar failures) OR SemanticIndex centroid drift exceeds threshold, the model proposes a self-formed backlog entry. Subject to:
- Posture must be EXPLORE or CONSOLIDATE
- Per-session cap: 1 self-formed entry (compared to 3 ask_human in W2(4))
- Cost cap: $0.10/entry (the model thinks → writes proposal)
- Operator-review-required tier — not auto-applied; lands in backlog with `auto_proposed=true` flag
- Hot-revert: `JARVIS_SELF_GOAL_FORMATION_ENABLED=false`

**Acceptance criteria**:
- `SelfGoalFormationEngine` analyzes postmortem clusters + semantic drift
- Proposes backlog entries with rationale + linked evidence (postmortem IDs, commits)
- BacklogSensor reads `auto_proposed=true` and routes through standard pipeline
- Telemetry: `[SelfGoalFormation] op=X analyzed N postmortems → proposed entry "Y" (cost=$Z)`
- Operator can approve/reject in `/backlog auto-proposed`

**Edge cases**:
- Runaway proposal generation — strict per-session cap (1) + cost cap ($0.10)
- Proposed goals that conflict with operator's actual direction — DirectionInferrer veto
- Proposed goals that would create infinite postmortem loops (op fails → propose investigation → fails again) — blocklist signature dedup

**Why P1**: this is the line between automation (does what you wrote) and autonomy (forms its own intent). Without it, "self-development engine" is overstating the cognition.

**Effort**: ~1200 LOC + 50 tests + new memory tracker. Builds on W2(4) + SemanticIndex + POSTMORTEM.

#### P1.5 — Hypothesis ledger

**Problem**: Self-formed goals need to be testable. Without an explicit hypothesis structure, the system can't measure whether its self-direction is yielding insight.

**Solution**: every self-formed goal is paired with a hypothesis ("I think X causes Y; if I do Z, I expect W"). After the goal completes, automated check: did W happen?

**Acceptance criteria**:
- `HypothesisLedger` JSONL artifact in session dir
- Each entry: `{hypothesis_id, op_id, claim, expected_outcome, actual_outcome, validated: bool}`
- Validated/invalidated count surfaced in summary.json
- `/hypothesis ledger` REPL command

**Effort**: ~400 LOC + 20 tests.

### Phase 3 — Operator Symbiosis (target: 4–6 weeks, parallel to Phase 2)

**Goal**: CC-class operator UX in autonomous mode.

#### P2 — Conversational mode (true CC parity)

**Problem**: O+V is intent-driven. To make a request, you write a backlog entry. There's no "let me clarify" loop with the operator beyond curiosity ask_human (which fires only during model-side exploration, not operator-initiated).

**Solution**: SerpentFlow gets a real REPL conversational mode. Operator types natural language → routed through a new ConversationOrchestrator that:
1. Classifies intent (do-this-now vs explore-this vs explain-that)
2. For do-this-now: synthesizes a backlog entry on the fly + dispatches
3. For explore-this: spawns a read-only subagent
4. For explain-that: directly queries Claude with relevant context
5. All conversational turns feed ConversationBridge buffer (already-built primitive)

**Acceptance criteria**:
- New REPL command: `/chat <message>` (or just bare text in interactive mode)
- ConversationOrchestrator routes appropriately, returns response + any spawned ops
- Multi-turn context preserved across messages within session
- Cross-session: ConversationBridge already exists; default-on for chat mode

**Edge cases**:
- Operator request that conflicts with current op-in-flight → ask before cancelling
- Very long conversations → ConversationBridge has K-cap and TTL; surface gracefully
- Operator pastes code/error messages → don't re-classify as a new goal; treat as context for current

**Effort**: ~1500 LOC + 60 tests. Largest single Phase 3 item.

**Status**: ✅ COMPLETE 2026-04-26. 4-slice arc landed (`intent_classifier` → `conversation_orchestrator` → `chat_repl_dispatcher` → graduation). 216 tests across the four-slice surface. `build_chat_repl_dispatcher()` factory in `chat_repl_dispatcher.py` is the single SerpentFlow integration point; safe-default `LoggingChatActionExecutor` ships every decision through the audit trail without touching the live FSM. Concrete backlog/subagent/Claude executors tracked as follow-up slices.

#### P3 — Lightweight approval UX

**Problem**: Yellow/Orange-tier approval today = create a PR + review. That's heavy for fast iterations.

**Solution**: SerpentFlow inline approval mode for development:
- Show full diff in terminal with hunks
- Prompt: `[y]es / [n]o / [s]how stack / [e]dit / [w]ait` with 30s default timeout
- On `y`: apply (same path as auto-apply for SAFE_AUTO)
- On `e`: open in $EDITOR, then re-prompt
- Keep existing PR path for production work (operator setting decides)

**Acceptance criteria**:
- New env: `JARVIS_APPROVAL_UX_INLINE_ENABLED` (~~default false → opt-in~~ **graduated 2026-04-26 — default true; hot-revert to `false`**)
- When enabled, NOTIFY_APPLY/APPROVAL_REQUIRED show inline prompt instead of (or in addition to) PR
- 30s timeout → auto-defer (not auto-approve — safety) ✅ pinned by `prompt_decision` returning `TIMEOUT_DEFERRED` + safety-first `WAIT` on EOF / garbage
- Audit trail unchanged (every decision still in cancel ledger) ✅ delivered via JSONL ledger at `.jarvis/inline_approval_audit.jsonl` (env-overridable; `AUDIT_LEDGER_SCHEMA_VERSION=1`)

**Status**: ✅ COMPLETE 2026-04-26. 4-slice arc landed (primitive → provider → renderer → graduation). 248 tests across the four-slice surface; `build_approval_provider()` factory in `inline_approval_provider.py` is the single selection point for `GovernedLoopService`.

**Edge cases**:
- Operator AFK → timeout to deferred queue, not auto-approved
- Multiple concurrent ops needing approval → single queue, FIFO with priority for IMMEDIATE
- Operator declines → POSTMORTEM tags as `operator_rejected_inline`, lessons feed back into PostmortemRecall

**Effort**: ~800 LOC + 30 tests.

#### P3.5 — Real-time progress visibility for long ops

**Problem**: PLAN-EXPLOIT 3-stream takes 2-5 min with no progress UI. Operator sees silence.

**Solution**: periodic HEARTBEAT events from each stream surface as a single coalesced status line:
```
[op-019dc42c-38d7] PLAN-EXPLOIT 3-stream: stream-1 reading file 4/8, stream-2 patching foo.py, stream-3 building tests (78s elapsed, ~120s ETA)
```

**Acceptance criteria**:
- Each `_generate_unit` emits HEARTBEAT every 5s with current activity summary
- SerpentFlow renders coalesced view
- LiveDashboard already supports this surface; SerpentFlow needs a slot

**Effort**: ~400 LOC + 15 tests.

### Phase 4 — Cognitive Metrics (target: 2–4 weeks, can ship in parallel)

**Goal**: Replace `INSUFFICIENT_DATA` with metrics that move when O+V gets smarter. **This phase is the implementation home for Wang's Improvements 1, 2, and 3.**

#### P4 — Convergence metrics suite

**Problem**: `convergence_state: "INSUFFICIENT_DATA"` is honest but useless. We can't optimize what we don't measure. RSI claim is unprovable without a composite score function.

**Solution**: replace with 5 concrete metrics + Wang's composite score:

| Metric | Definition | Target | Wang mapping |
|---|---|---|---|
| **Composite score per op** | weighted sum: pytest (40%) + coverage (20%) + complexity (15%) + lint (10%) + semantic-drift (15%) | non-decreasing 30d trend | Improvement 1 |
| **Convergence state** | classifier: `IMPROVING` / `PLATEAU` / `OSCILLATING` / `DEGRADING` from rolling score window | `IMPROVING` or `PLATEAU` | Improvement 2 |
| **Session completion rate** | % sessions with stop_reason ∈ {idle, budget, wall} AND ≥ 1 commit OR ≥ 1 ack'd no-op | 90%+ at A-level | — |
| **Self-formation ratio** | self-formed backlog entries / total ops per session | 10%+ at A-level | — |
| **POSTMORTEM recall rate** | % subsequent ops that consulted ≥ 1 prior postmortem | 30%+ at A-level | partial Improvement 6 |
| **Cost per successful APPLY** | total session cost / commits | trending DOWN over rolling 30d | — |
| **Strategic posture stability** | mean dwell time per posture (secondary signal of operator-arc tracking) | trending UP | — |

Surface in `summary.json` + `/metrics` REPL + IDE observability stream.

**Acceptance criteria**:
- All 7 metrics computed at session end ✅ delivered via `MetricsEngine.compute_for_session`
- Persisted to `.jarvis/metrics_history.jsonl` (cross-session) ✅ delivered via `MetricsHistoryLedger.append` (env-overridable path; oversize-line dropped; concurrent-writer truncation tolerance)
- `/metrics 7d` REPL shows trends ✅ delivered via `MetricsReplDispatcher` with ASCII sparkline rendering
- IDE GET `/observability/metrics` ✅ delivered via `register_metrics_routes` (4 GET endpoints; loopback + rate-limit + CORS + schema_version + Cache-Control: no-store) + SSE `metrics_updated` event added to broker allow-list
- `composite_score.py` exists (per RSI_CONVERGENCE_FRAMEWORK.md Improvement 1) — verify before reimplementing ✅ verified by Phase 0 audit; un-stranded by Slice 1 wrapper rather than reimplemented

**Status**: ✅ COMPLETE 2026-04-26. 5-slice arc landed (`metrics_engine` → `metrics_history` → `metrics_repl_dispatcher` → `metrics_observability` → graduation). 242 tests across the five-slice surface. `JARVIS_METRICS_SUITE_ENABLED` graduated default `false`→`true` in three owner modules (engine + repl_dispatcher + observability). `register_metrics_routes` wired into `EventChannelServer.start` (loopback-asserted, gated on master flag). The `INSUFFICIENT_DATA` problem statement that motivated this phase is resolved.

**Effort**: ~800 LOC + 35 tests (larger than original PRD estimate due to composite score depth).  *Actuals: ~2,800 LOC + 242 tests across 5 slices — significantly beefier than estimate due to Slice 4's IDE/SSE surfaces + Slice 5's cross-cutting graduation pin suite.*

### Phase 5 — Adversarial Depth (target: 6–10 weeks, can run after Phase 1+2)

**Goal**: Add an internal opponent that tries to break each plan.

#### P5 — Adversarial reviewer subagent

**Problem**: Iron Gate enforces hygiene rules. SemanticGuardian matches patterns. Neither *thinks adversarially* about whether a plan is correct.

**Solution**: new subagent role — `AdversarialReviewer`. Activates post-PLAN, pre-GENERATE. Given the plan, the model is prompted as: "You are a senior engineer reviewing this plan for the most likely way it will fail. Find at least 3 failure modes." Output is structured findings injected into GENERATE prompt as "Reviewer raised:" section.

**Acceptance criteria**:
- New `AdversarialReviewerService` calls a Claude side-stream
- Findings in JSON: `[{severity, category, description, mitigation_hint}]`
- Cost-budgeted (default $0.05/op)
- Skipped for trivial / SAFE_AUTO ops
- Telemetry: `[AdversarialReviewer] op=X raised N findings (severity high=A, med=B, low=C)`

**Edge cases**:
- Reviewer hallucinations — findings must reference specific files / patterns; ungrounded findings filtered ✅ delivered via `filter_findings` (drops empty `file_reference`, ungrounded references, traversal references unconditionally)
- Reviewer disagreement with PLAN — use as warning, not gate (PLAN still authoritative; findings inform GENERATE) ✅ delivered structurally — `adversarial_reviewer_hook.py` returns text only, no return path that gates anything
- Cost budget exceeded — reviewer skipped silently with INFO log ✅ delivered as post-check; provider over budget → findings discarded, `skip_reason=budget_exhausted`, cost preserved on review for audit

**Status**: ✅ COMPLETE 2026-04-26. 5-slice arc landed (`adversarial_reviewer` primitive → `adversarial_reviewer_service` + cost budget + JSONL ledger → `adversarial_reviewer_hook` + GENERATE injection + bridge feed → `adversarial_observability` REPL + IDE GETs + SSE → graduation). 218 tests across the five-slice surface. `JARVIS_ADVERSARIAL_REVIEWER_ENABLED` graduated default `false`→`true`. `register_adversarial_routes` wired into `EventChannelServer.start`. **The "neither thinks adversarially" gap from the problem statement is structurally closed** — though the orchestrator GENERATE-prompt wiring (calling the Slice 3 hook from the post-PLAN/pre-GENERATE site in `orchestrator.py`) is **deferred to follow-up**, mirroring P4 Slice 5's deferral of the harness session-end wiring. Until that follow-up lands, the AdversarialReviewer is callable + audit-trailed + observable but not yet automatically invoked by the FSM.

**Effort**: ~1000 LOC + 40 tests.  *Actuals: ~1,950 LOC + 218 tests across 5 slices — beefier than estimate due to Slice 4's REPL + 4 GET endpoints + SSE event + Slice 5's cross-cutting graduation pin suite.*

### Phase 6 — Self-Modeling (target: 3–6 months, long-horizon)

**Goal**: System has a model of its own behavior over time.

#### P6 — Behavior summarizer + self-narrative

**Problem**: System has perfect data about what it did but no narrative about who it is becoming.

**Solution**: weekly cron-like job consumes the prior week's POSTMORTEM ledger + commits + metrics. Produces a 1-page "self-narrative" doc: what I worked on, what I learned, what I'm getting better at, what I'm stuck on. Operator-readable; also fed into next-week's StrategicPosture default.

**Acceptance criteria**:
- `SelfNarrativeService` runs weekly
- Output: `docs/operations/o-v-weekly/<week>.md`
- Includes: top 5 themes, top 5 failure modes, learning trajectory (which postmortems inspired which subsequent improvements)
- Auto-PR'd for operator review

**Effort**: ~1500 LOC + 50 tests + new doc convention.

---

## 10. Per-Phase Requirements: Telemetry & Observability

Each phase MUST add structured telemetry compatible with the existing 41-event SSE vocabulary + JSONL ledger pattern. Per CLAUDE.md governing principle 7 (Absolute Observability), no autonomous decision is allowed to be invisible.

| Phase | New SSE event types | New JSONL ledger | New IDE GET routes |
|---|---|---|---|
| P0 (POSTMORTEM recall) | `postmortem_recalled` | `postmortem_recall_history.jsonl` (one entry per recall, with similarity score + injected lesson IDs) | `/observability/recall` (recent recalls, filterable by op_id) |
| P0.5 (arc-aware DirectionInferrer) | `posture_arc_updated` (extends existing posture_changed) | extends existing posture_history.jsonl | extends existing `/observability/posture` |
| P1 (Curiosity v2 — self-formation) | `goal_self_formed`, `goal_self_form_rejected_by_operator` | `self_formed_goal_ledger.jsonl` | `/observability/self-formed-goals` |
| P1.5 (HypothesisLedger) | `hypothesis_validated`, `hypothesis_invalidated` | `hypothesis_ledger.jsonl` | `/observability/hypotheses` |
| P2 (Conversational mode) | `conversation_turn_received`, `conversation_intent_classified` | `conversation_history.jsonl` (extends ConversationBridge buffer) | `/observability/conversation` |
| P3 (Lightweight approval) | `inline_approval_requested`, `inline_approval_decided` | extends existing approval ledger | extends existing `/observability/plans` |
| P3.5 (Progress visibility) | `op_stream_progress` (5s cadence per stream) | (memory-only ring buffer; no persistent ledger needed) | extends existing `/observability/tasks` |
| P4 (Metrics suite) | `metric_snapshot_recorded`, `convergence_state_changed` | `metrics_history.jsonl` | `/observability/metrics` |
| P5 (Adversarial reviewer) | `adversarial_finding_raised` | `adversarial_review_ledger.jsonl` | `/observability/adversarial-findings` |
| P6 (Self-Modeling) | `self_narrative_generated` | `self_narrative_index.jsonl` | `/observability/narratives` |

**Vocabulary discipline**: per W2(4) Slice 4 + W3(7) Slice 7 graduation pin pattern, the SSE event vocabulary is **additive only**. Removing an event is a wire-format break. New event types require updating `_VALID_EVENT_TYPES` in `ide_observability_stream.py` + corresponding count pin.

**Total new SSE events across all phases**: ~16 (vocabulary grows from 41 → ~57). Each phase's PR adds its events to the count pin.

---

## 11. Per-Phase Requirements: Testing Strategy

Every phase ships with the same 4-layer test discipline established by W2/W3 graduations:

### Layer 1: Unit tests
- ≥ 80% line coverage on new code
- Authority invariant grep pin (no banned imports per Manifesto §1)
- Source-grep pin for the wiring point (the place the new service is invoked)

### Layer 2: Integration tests
- Cross-component: new service ↔ existing service hooks tested end-to-end
- Per `feedback_orchestrator_wiring_invariant_checklist.md` pattern

### Layer 3: Live-fire smoke (no API dependency)
- Standalone script `scripts/livefire_<phase>.py` that exercises the new primitive in-process
- Must not require Anthropic API stability — uses stubs / fakes for provider calls
- Outputs a journal: N/N checks passed/failed
- Mirrors W2(4) `livefire_w2_4_curiosity.py` + W3(6) `livefire_w3_6_parallel_dispatch.py` pattern

### Layer 4: Graduation cadence
- 3 clean live battle-test sessions under master flag on (matches W2(5) PhaseRunner extraction protocol)
- Per-session evidence captured in graduation matrix doc
- Operator-authorized default flip after 3/3 clean
- 1 post-flip confirmation soak

### Test count targets per phase (rough)

| Phase | Unit | Integration | Live-fire checks | Graduation pins |
|---|---|---|---|---|
| P0 | 25 | 5 | 15 | 12 |
| P0.5 | 10 | 2 | 8 | 6 |
| P1 | 35 | 10 | 20 | 15 |
| P1.5 | 15 | 5 | 10 | 8 |
| P2 | 40 | 15 | 25 | 18 |
| P3 | 20 | 5 | 12 | 10 |
| P3.5 | 10 | 3 | 8 | 5 |
| P4 | 25 | 8 | 15 | 12 |
| P5 | 30 | 8 | 18 | 12 |
| P6 | 35 | 10 | 22 | 15 |

**Total**: ~245 unit + ~71 integration + ~153 live-fire + ~113 graduation pins = **~580 new tests across all phases**.

---

## 12. Edge Cases & Nuances (cross-cutting)

### 12.1 Cost runaway prevention

Every new cognitive layer adds LLM calls. Protections:
- All new services budgeted via cost_governor (per-op caps + parallel-stream multiplier already in place from #19800)
- Self-formation strictly capped at 1 entry/session (P1)
- Adversarial reviewer skipped for trivial ops (P5)
- New global env: `JARVIS_COGNITIVE_LAYER_BUDGET_USD_PER_SESSION` (default $1.00, hard ceiling for all cognitive layers combined)

### 12.2 Authority preservation invariants

NEW cognitive layers must NOT:
- Soften Iron Gate (exploration-first, ASCII strict, multi-file coverage)
- Bypass risk-tier-floor
- Modify SemanticGuardian's hard findings
- Write to `.git/` config
- Add new mutation tools to Venom's capability set

Each new service has a grep-pinned authority test (same pattern as Phase B subagent cage).

### 12.3 Failure mode containment

Each new service is independently hot-revertable via env flag. A misbehaving cognitive layer must not poison other layers:
- PostmortemRecall failure → fall back to no injection (silent)
- SelfGoalFormation failure → no entry proposed (silent)
- ConversationOrchestrator failure → fall back to legacy backlog flow
- AdversarialReviewer failure → GENERATE proceeds without findings injection
- SelfNarrative failure → no PR generated; logged for next-week retry

### 12.4 The "model knows it's being measured" risk

Once the system is rewarded for "self-formation ratio," it may game it (proposing trivial entries to inflate the metric). Mitigations:
- Operator-review gate on auto-proposed entries (P1)
- HypothesisLedger validation (P1.5) — proposals that don't deliver lose weight
- Quality metric paired with quantity (cost per successful APPLY)
- Composite score (P4) ensures gaming requires *actual* improvement

### 12.5 Cross-cutting observability

Every new layer adds events to the IDE stream. Vocabulary must stay additive (current invariant from W2(4) Slice 4 + W3(7) Slice 7). See §10 for the per-phase event list.

### 12.6 Operator-in-the-loop boundary

Self-formed goals are NEVER auto-applied at risk-tier > SAFE_AUTO. Even SAFE_AUTO self-formed goals require an explicit operator opt-in (separate env from auto-apply for sensor-driven SAFE_AUTO). Reason: the operator authored sensor logic; they didn't author the model's self-formation policy.

### 12.7 Cross-session state contamination

Phase 1 (POSTMORTEM recall) reads from accumulated postmortem history. Cross-session contamination class observed in Wave 3 (intake WAL signature dedup carryover). Mitigations:
- Time-decay weighting in similarity (older postmortems weight less)
- Commit-window filter (postmortems before HEAD~N skipped)
- Postmortem lifecycle policy: archive after 90 days (separate cleanup follow-up)

### 12.8 Conflict between phases

If phase outputs disagree (e.g., DirectionInferrer says HARDEN, but SelfGoalFormation proposes an EXPLORE-class goal), the conflict resolution:
- Posture is authoritative (HARDEN posture vetoes self-formation)
- Operator override always wins
- Conflict events logged for postmortem analysis

---

## 13. Success Metrics (PRD-level)

### Per-phase exit criteria

| Phase | Exits when |
|---|---|
| Phase 1 (Self-Reading) | PostmortemRecall produces ≥ 1 injection per 3 ops on average + DirectionInferrer arc-aware in 3 consecutive battle-test sessions |
| Phase 2 (Self-Direction) | ≥ 5 self-formed goals shipped end-to-end across 1 week + HypothesisLedger validation rate ≥ 40% |
| Phase 3 (Operator Symbiosis) | Conversational mode used for ≥ 50% of operator-initiated work + Inline approval used for ≥ 30% of Yellow ops |
| Phase 4 (Cognitive Metrics) | All 7 metrics dashboarded + 30-day rolling trends visible + composite score computed for ≥ 100 ops |
| Phase 5 (Adversarial Depth) | Adversarial findings caught ≥ 1 prevented bug in production cadence |
| Phase 6 (Self-Modeling) | Weekly self-narratives auto-PR'd for ≥ 4 consecutive weeks |

### Overall A-level signal

When all 7 of the §6.1 dimensions land simultaneously, O+V is A-level.

### MVP RSI signal (per Wang)

When all 5 of the §5.4 conditions land simultaneously, O+V is MVP-RSI.

---

## 14. Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Cognitive layers add cost without proportional value | Medium | High | Cost-budgeted; metrics-tracked; revert per-layer |
| Self-formation produces noise / spam | Medium | Medium | Strict per-session cap (1); operator-review gate; HypothesisLedger feedback |
| Conversational mode fragments the operator-experience | Low | Medium | Default-off; opt-in via env; extensive UX testing |
| External provider regression makes cognitive layers fail invisibly | Medium | Medium | Already-built resilience pack (#20147) handles; cognitive layers gracefully degrade |
| Postmortem recall pollutes prompts with stale context | Medium | Medium | Time-decay weights + commit-window filters |
| Adversarial reviewer becomes overcautious / blocks work | Low | High | Findings inform, don't gate; operator can disable per-op |
| 102K-line supervisor.py grows further | High | Low | All new services in their own modules; PhaseRunner extraction precedent |
| Wang's RSI guarantees don't hold for non-stationary code distribution | Medium | High | Convergence monitoring (P4) detects oscillation/degradation; operator can pause autonomy |
| Composite score weights become operator-tunable knobs proliferating | Medium | Low | Initial weights frozen in code; only score values are env-tunable |
| Self-narrative becomes hallucinatory ("I'm getting smarter when I'm not") | Medium | Medium | Self-narrative consumes objective metrics (composite score, completion rate); operator review on each weekly PR |

---

## 15. Out of Scope (deferred / future)

- **Multi-modal autonomous use** — vision/audio sensors are excluded from this PRD; deferred to a separate roadmap (VisionSensor exists, integration with cognitive layers is future)
- **Inter-repo direction inference** — DirectionInferrer is single-repo for now; cross-repo posture is a future surface
- **Distributed multi-instance O+V** — federation across multiple JARVIS deployments is excluded
- **Real-time voice REPL** — Karen / voice surfaces exist but their integration with cognitive layers is excluded from this PRD
- **Provider hedging / multi-region Anthropic fallback** — separate scope (resilience pack v2 candidate)
- **Trinity (Mind / Soul) integration** — assumes JARVIS-side O+V matures first; J-Prime + Reactor Core integration is a separate document
- **Wang Improvements 4 (Oracle Pre-Scoring) and 5 (Transition Tracking) standalone implementations** — partially addressed by Phase 5 + Phase 1 respectively; standalone build deferred unless gaps emerge

---

## 16. Open Questions for Operator Decision

Before Phase 1 implementation begins:

1. **Postmortem time-decay window**: 30 days, 60 days, or session-count-based? *(Recommend: 30 days, env-tunable)*
2. **Self-formation cost cap per session**: $0.10 (proposed) or higher? *(Recommend: $0.10 to start; widen after HypothesisLedger validation rate ≥ 40%)*
3. **Conversational mode default**: opt-in env (proposed) or default-on for interactive sessions? *(Recommend: opt-in for graduation cadence; default-on after operator UX testing)*
4. **Adversarial reviewer model**: same as primary (Claude) or distinct (cheaper Sonnet, distinct provider)? *(Recommend: cheaper Sonnet — adversarial role doesn't need top-tier reasoning)*
5. **Self-narrative cadence**: weekly (proposed), bi-weekly, or per-N-commits? *(Recommend: weekly fixed cadence + on-demand operator trigger)*
6. **Phase 4 metrics destination**: SQLite, JSONL, or Parquet? *(Recommend: JSONL — matches existing observability pattern; SQLite for query layer in P6 if needed)*
7. **Wang RSI implementation status verification**: do we audit `RSI_CONVERGENCE_FRAMEWORK.md`'s 6 modules before Phase 1 starts? *(Recommend: yes, 1-day audit + verify-vs-code as Phase 0)*
8. **Phase 4 composite score weights**: pytest 40% / coverage 20% / complexity 15% / lint 10% / semantic-drift 15% as proposed? *(Recommend: yes, env-locked initially; revisit if convergence_state shows OSCILLATING)*

Each question has a recommended default. Operator can override.

---

## 17. Implementation Discipline

Per established O+V conventions (per CLAUDE.md):

- **Per-slice operator authorization** — no slice begins without explicit operator green light
- **Default-off env flags** — every new service is opt-in until graduation
- **3-clean-session graduation cadence** — same as W2(5) PhaseRunner extraction pattern
- **Source-grep pins** — every new service has invariant grep tests
- **Authority invariants** — every new service has a "does NOT import gate/policy modules" test
- **Hot-revert documented** — every service has a single env knob that returns byte-for-byte pre-fix behavior
- **Live-fire smoke** — every service has a local smoke script that doesn't depend on Anthropic API stability
- **PRs scoped to single slice** — no cross-pillar work in one PR
- **Memory ledger updates** — every closure updates the relevant memory file (`memory/project_*.md`) + MEMORY.md index
- **Operator-runbook for every graduated knob** — `docs/operations/<feature>-graduation.md` with hot-revert recipe + env table

---

## 18. Stakeholder Map

Different consumers of this PRD have different reading paths:

| Stakeholder | Primary read | Secondary read | What they need |
|---|---|---|---|
| **Operator (you)** | §1, §6, §7, §16, §20 | §22, §13 | Vision alignment, decisions to make, schedule |
| **Engineers (per-phase implementers)** | §9 (full), §10, §11 | §12, §17, App C | Acceptance criteria, telemetry, testing pattern |
| **Architects** | §4, §5, §7, §8 | §22 | Cognitive scaffolding, RSI alignment, principle preservation |
| **Reviewers (PR review)** | §17, §10, §11 | §12 | Discipline checklist, telemetry compliance, edge cases |
| **Future-self (resuming context)** | §3, §9 (current phase) | App A, App B | What state we're in, what's next |
| **Battle-test harness consumers** | §11 layer 4 | App C | Cadence requirements, exit criteria |
| **IDE extension consumers** | §10 | — | New SSE events to subscribe + GET routes |

---

## 19. PRD Migration & Versioning Strategy

This PRD is a **living document**. It will be amended as phases land and as reality deviates from plan. Versioning discipline:

### Version bumps

- **Patch (vX.Y.Z+1)**: typo fixes, clarifications, link fixes — no PR required, direct commit acceptable
- **Minor (vX.Y+1.0)**: per-phase status updates, new edge cases discovered, clarifications to acceptance criteria — PR required
- **Major (vX+1.0.0)**: phase reordering, new pillar added, target state changed — PR + operator authorization required

### Amendment process

When reality deviates from plan (e.g., a phase produces unexpected results that change downstream phases):
1. Open a PR amending the PRD
2. Add a row to Appendix D (Document History) with `change`, `reason`, `impact_on_subsequent_phases`
3. Operator reviews + merges

### What CAN be amended without operator authorization

- Acceptance criteria refinements (e.g., "≥ 30%" → "≥ 25%" if data shows initial estimate was wrong)
- Effort estimates (LOC + test counts are approximate)
- Edge case additions
- Stakeholder map additions
- Reference doc additions

### What CANNOT be amended without operator authorization

- Vision statement (§2)
- Anti-goals (§6)
- Phase reordering
- Pillar changes
- Manifesto principle alignment (§8)
- Scope expansion (anything in §15 moving in)

### Phase boundary discipline

When a phase exits (per its exit criteria in §13):
1. Phase status updated in §9 from `pending` → `complete`
2. Memory file `memory/project_phase_<N>_closure.md` written
3. Lessons learned amended to PRD if applicable
4. Next phase's Pre-Phase audit triggered

---

## 20. Roadmap Summary (one-page chronological)

| Phase | Item | Effort | Pillar | When |
|---|---|---|---|---|
| 0 | RSI implementation status audit | 1d | Pre-Phase | Day 1 |
| 1 | P0 — POSTMORTEM → next-op recall | 600 LOC + 30 tests | Self-Reading | Weeks 1-3 |
| 1 | P0.5 — Cross-session direction memory | 200 LOC + 12 tests | Self-Reading | Weeks 3-4 |
| 4 | P4 — Convergence metrics suite (incl. Wang composite score) | 800 LOC + 35 tests | Cognitive Metrics | Weeks 1-3 (parallel) |
| 3 | P2 — Conversational mode | 1500 LOC + 60 tests | Operator Symbiosis | Weeks 4-8 |
| 3 | P3 — Lightweight approval UX | 800 LOC + 30 tests | Operator Symbiosis | Weeks 6-8 (parallel) |
| 3 | P3.5 — Real-time progress visibility | 400 LOC + 15 tests | Operator Symbiosis | Weeks 7-8 (parallel) |
| 2 | P1 — Curiosity Engine v2 (self-formation) | 1200 LOC + 50 tests | Self-Direction | Weeks 8-12 |
| 2 | P1.5 — Hypothesis ledger | 400 LOC + 20 tests | Self-Direction | Weeks 11-12 |
| 5 | P5 — Adversarial reviewer | 1000 LOC + 40 tests | Adversarial Depth | Weeks 12-18 |
| 6 | P6 — Behavior summarizer | 1500 LOC + 50 tests | Self-Modeling | Weeks 18-30 |

**Total**: ~8400 LOC + ~342 tests across ~7 months. Comparable in scope to Wave 2 (5) PhaseRunner extraction. Larger in cognitive impact than the entire Wave 1+2+3 sequence combined.

---

## 21. Why this Roadmap, in this Order

The ordering is **not** by complexity. It's by **dependency + compounding impact**:

- **P0 (Self-Reading) first** because every subsequent layer benefits from POSTMORTEM recall. Curiosity v2 needs to consult prior postmortems. Conversational mode needs to remember prior turns. Metrics need historical baselines.
- **P4 (Metrics) parallel** because we can't measure improvement of P1/P2/P3 without baseline metrics in place. Also: P4 owns Wang's composite score, which is the spine of the RSI claim.
- **P2/P3 (Operator Symbiosis) before P1 (Self-Direction)** because conversational mode lets the operator more easily review self-formed goals when they start landing. Putting P1 before P2 would create operator-feedback friction.
- **P5 (Adversarial) after P1** because adversarial reasoning is most valuable on self-formed goals (which the model wrote and didn't critique itself).
- **P6 (Self-Modeling) last** because it consumes outputs from all other phases.

The roadmap is **architecturally inevitable** given the pillar structure. There aren't many other valid orderings.

---

## 22. The Larger Frame — Trinity AI Ecosystem

This PRD treats O+V as *the* product. But the operator's broader vision (per `CLAUDE.md`) is the **JARVIS Trinity AI Ecosystem** — Body (JARVIS) + Mind (J-Prime) + Soul (Reactor Core). O+V is the autonomous self-development engine within Body.

### 22.1 Body / Mind / Soul roles

| Component | Role | Current state | Relationship to this PRD |
|---|---|---|---|
| **Body (JARVIS)** | macOS integration, screen capture, voice, keyboard automation, autonomous self-development (O+V) | mature; this PRD scopes O+V layer | This PRD is Body's roadmap |
| **Mind (J-Prime)** | GCP-hosted reasoning, plan synthesis, deep thinking | exists; integration partial | Phase 6 (Self-Modeling) outputs may feed J-Prime as long-arc memory |
| **Soul (Reactor Core)** | Sandboxed safety / governance kernel | exists | Constraints on what O+V can autonomously do; cognitive layers MUST respect |

### 22.2 Why Body matters first

The cognitive layers added in Phases 1-6 here are the foundation for J-Prime ↔ Reactor Core integration later. A self-reading, self-directing, self-modeling Body is the precondition for genuine Trinity convergence. **Without these phases, Mind and Soul have a dumb Body to drive — not an autonomous one.**

### 22.3 What success means at Trinity scale

This PRD's success is not measured by O+V alone reaching A-level. It's measured by **Body becoming the kind of substrate Mind and Soul can compose into a true RSI organism.**

Specifically:
- POSTMORTEM ledger (Phase 1 output) becomes Mind's long-arc memory source
- HypothesisLedger (Phase 2) becomes Soul's "what's the system claiming?" audit surface
- Composite score (Phase 4) becomes Trinity's unified quality signal
- Self-narrative (Phase 6) becomes operator-readable Trinity status

### 22.4 Sequencing

Body's cognitive maturation MUST precede Mind/Soul integration. Reasons:
1. Mind without a self-reading Body has no signals to reason from
2. Soul without a self-directing Body has no decisions to govern
3. Trinity convergence requires Body's hypotheses to validate against Mind's plans + Soul's guardrails

This PRD's 7-month timeline is the precondition for Trinity work.

---

## 23. The Reverse Russian Doll — Orders of Self-Reference (Architectural Framing)

> *"In a standard Russian doll, the layers compress inward, getting smaller and simpler. We are doing the exact opposite. We have established the solid core, and we are building the mechanisms for the core to autonomously carve an exponentially larger, smarter shell around itself."*
>
> — Derek J. Russell, operator binding (2026-04-26)

This section introduces an **architectural lens** for understanding the system's self-improvement that is *orthogonal* to the Phase 1–6 roadmap (§9) and complementary to the Wang convergence framework (§5). Where Phases describe **behavioral milestones** ("the system reads its own output," "the system forms its own goals"), and Wang describes the **mathematical guarantee** that score-monotonic optimization converges, the Reverse Russian Doll axis describes **what O+V acts upon** — the layer of self-reference at which a given improvement operates.

The framework was articulated by the operator in the 2026-04-26 architectural review and reconciled against the four canonical docs (`OUROBOROS.md`, this PRD, `RSI_CONVERGENCE_FRAMEWORK.md`, `JARVIS_LEVEL_OUROBOROS.md`) in a Pass A document — `memory/project_reverse_russian_doll_pass_a.md`. The Pass A finding was that **the Order axis was not present in any canonical doc**, even though every Order-1 subsystem it describes was already shipping. This section closes that vocabulary gap.

### 23.1 The vocabulary contribution

Pre-existing taxonomies in this PRD and adjacent docs:

| Taxonomy | What it captures | Where it lives |
|---|---|---|
| **Phases 1–6** (this PRD §9) | Behavioral milestones — Self-Reading → Self-Direction → Operator Symbiosis → Cognitive Metrics → Adversarial Depth → Self-Modeling | §9 of this PRD |
| **Tiers 1–7** (`JARVIS_LEVEL_OUROBOROS.md`) | Behavioral enhancements — Judgment → Emergency → Prediction → Resilience → Reasoning → Personality → Autonomous Judgment | All Pre-Implementation per source doc |
| **Wang RSI loop** (§5) | Single score-monotonic optimization with O(log n) expected convergence | §5 of this PRD |
| **11-phase FSM** (`OUROBOROS.md`) | Operational stages of one operation — CLASSIFY → COMPLETE | `orchestrator.py` |

None of these capture **what O+V is acting upon**: is the patch modifying application code (the body), or modifying the cognitive substrate that produces patches? The Reverse Russian Doll axis fills that gap.

### 23.2 Orthogonality — the Order axis runs perpendicular

Phase, Tier, Wang, and FSM-stage all describe **dynamics within a fixed substrate**. Order describes **which substrate is in play**. They compose freely:

| | Order 0 | Order 1 | Order 2 |
|---|---|---|---|
| **Phase 1 (Self-Reading)** | n/a | shipping (POSTMORTEM ledger reads, SemanticIndex centroids) | future: cognitive substrate reads its own commit history of governance changes |
| **Phase 2 (Self-Direction)** | n/a | partial (DirectionInferrer on env signals) | future: O+V proposes new PhaseRunner subclasses to address postmortem patterns |
| **Phase 6 (Self-Modeling)** | n/a | future scope: Body's behavior summary | future scope: cognitive substrate produces a summary of its own evolution |
| **Wang composite score** (Imp #1) | n/a | already mapped to Order-1 ops | future: separate score for Order-2 ops with different weights — see §23.6 |

The Order axis tells the reader *"this work is about the Body"* (Phase 1 today) vs. *"this work is about the cognitive cage"* (Phase 1 in the Order-2 horizon). Without this distinction, "Self-Reading" is ambiguous — the loop reads what?

### 23.3 Order 0 — The Exoskeleton Baseline

**Industry default. Ouroboros rejects it by design.**

Order 0 is the operating mode of mainstream developer-AI tools: the AI is a compiled exoskeleton — the human types, the AI suggests; the human stops, the AI freezes. The system has no continuous existence between turns, no sensory layer, no autonomous initiation, no memory across sessions in any architecturally load-bearing way.

This is documented as the contrast in `TRINITY_ECOSYSTEM_TECHNICAL_DOCUMENT.md:534`:

> *"Claude Code / OpenClaw / ClawdBot — Developer CLI agent — Session-scoped, no continuous operation, no sensory layer, single model, cannot self-modify."*

The reason Order 0 is included in this taxonomy is to make the rejection explicit. **Every Order-1 capability listed below is a deliberate departure from the Order-0 default.** Where the industry treats AI as a frozen-when-idle exoskeleton, Ouroboros treats it as a continuously-running autonomic nervous system that initiates, perceives, and acts on its own.

### 23.4 Order 1 — The Body (current shipping state)

**O+V as autonomic nervous system. This is what currently runs.**

Order 1 is the layer at which the cognitive engine acts on **the body** — the JARVIS application code, sensors, tooling, tests, runbooks, documentation, and configuration. The cognitive engine itself is the actor; the body is the object. Every battle-test breakthrough logged in `OUROBOROS.md` is an Order-1 success.

#### 23.4.1 Order-1 substrate, by subsystem

| Capability | Subsystem | Location |
|---|---|---|
| Continuous environmental scan | 16 autonomous sensors | `backend/core/ouroboros/intake/sensors/` |
| Priority queue + WAL persistence | UnifiedIntakeRouter | `backend/core/ouroboros/intake/` |
| 11-phase governed loop | Orchestrator FSM | `backend/core/ouroboros/governance/orchestrator.py` |
| 3-tier provider cascade | DW 397B → Claude → J-Prime | `candidate_generator.py`, `providers.py`, `doubleword_provider.py` |
| Multi-turn agentic tool loop | Venom (16 built-in + MCP) | `tool_executor.py` |
| Multi-file coordinated APPLY | `files: [...]` schema + ChangeEngine batch rollback | `orchestrator.py::_apply_multi_file_candidate` |
| Posture-aware self-regulation | DirectionInferrer + StrategicPosture (4 values) | `direction_inferrer.py`, `posture*.py` |
| Global op-emission cap | SensorGovernor | `sensor_governor.py` |
| Memory-pressure throttle | MemoryPressureGate | `memory_pressure_gate.py` |
| Post-VERIFY structured commit | AutoCommitter with O+V signature | `auto_committer.py` |
| Cross-session memory | UserPreferenceMemory + SemanticIndex + LastSessionSummary | `user_preference_memory.py`, `semantic_index.py`, `last_session_summary.py` |
| L3 worktree isolation | Per-unit COW worktrees + `reap_orphans` | `subagent_scheduler.py`, `worktree_manager.py` |
| Mid-op cooperative cancel | W3(7) cancel-token (REPL + watchdog + signal) | per W3(7) graduation 2026-04-25 |
| Parallel L3 fan-out | parallel_dispatch + cost-cap parallel-stream | per W3(6) architectural completion 2026-04-25 |

#### 23.4.2 What "Order 1 ships" means concretely

End-to-end autonomous APPLY-to-disk under full complex-route enforcement is **proven and graduated**. Battle-test landmarks documented in `docs/architecture/OUROBOROS.md` battle-test breakthrough log:

- **2026-04-15 Session O** — first sustained single-file APPLY (`test_test_failure_sensor_dedup.py`, ChangeEngine + L2 CONVERGED + POSTMORTEM root_cause=none)
- **2026-04-15 Sessions U–W** — first end-to-end multi-file APPLY (4 test modules, AutoCommitter commit `0890a7b6f0`, 20/20 post-hoc pytest pass)
- **Wave 1 graduations 2026-04-21** — DirectionInferrer + FlagRegistry + SensorGovernor: the system reads its own posture and self-throttles
- **W2(4) graduation 2026-04-25** — Curiosity engine widening `ask_human` on EXPLORE/CONSOLIDATE Green ops
- **W3(7) graduation 2026-04-25** — mid-op cancellation infrastructure

The Order-1 thesis is no longer conjecture; it is the operating regime.

#### 23.4.3 What Order 1 still has to grow into

The Phase 1–6 roadmap (§9) is largely Order-1 work. Phase 1 (Self-Reading) wires existing structured outputs (POSTMORTEM, SemanticIndex, ConversationBridge) back into Order-1 decisions. Phase 2 (Self-Direction) lets Order-1 ops form their own backlog entries. Phase 5 (Adversarial Depth) adds an internal opponent for Order-1 plans. None of those Phases require Order-2 capabilities to land.

**This is important**: Phases 1–6 do not require self-modification of the cognitive substrate. They require deeper self-reference *within* the Order-1 layer. Order 2 is a separate horizon (§23.5), not a Phase 1–6 prerequisite.

### 23.5 Order 2 — The Cognitive Substrate (horizon)

**O+V turns inward and proposes modifications to its own cognitive architecture.**

Order 2 is the layer at which the cognitive engine acts on **itself** — the orchestrator FSM, the immune system gates (Iron Gate sequence, `semantic_firewall.py`, `semantic_guardian.py`, `scoped_tool_backend.py`), the change engine, the risk-tier ladder, the PhaseRunner classes that implement each phase.

#### 23.5.1 Why Order 2 is not Phase B subagent invocation

A common misreading after the Phase B subagent graduation (2026-04-20, `memory/project_phase_b_subagent_roadmap.md`) is that Phase B subagents constitute "partial Order 2." They do not. **Phase B subagents are cognitive *delegation*, not cognitive *self-modification*.** O+V invokes EXPLORE/REVIEW/PLAN/GENERAL; it does not generate them, modify their scope contracts, or design new subagent kinds. The subagent shells (`agentic_*_subagent.py`) and their contracts (`subagent_contracts.py`) are hand-written governance code — they would themselves be Order-2 modification targets if O+V proposed to change them.

The Phase C Slice 1b GENERAL LLM driver (2026-04-20) is the closest existing thing to a *cognitive interior on a subagent shell*, but the shell itself is still hand-written. Real Order 2 would be O+V proposing new subagent kinds with their own scope/budget/allowlist contracts. This is explicitly out of Pass B scope (§23.10) and reserved for future architectural work.

#### 23.5.2 The Order-2 rung map

Five gaps separate "Order 1 graduated" from "Order 2 real," in structural-prerequisite order:

| Rung | Gap | Status | Reference |
|---|---|---|---|
| **2.1** | Pipeline FSM not yet pluggable | In motion — W2(5) PhaseRunner extraction shipped per `phase_runner.py`/`phase_runners/`; Slice 5b (final dispatcher cutover + Iron Gate depth) gated on operator authorization | `memory/project_wave2_phaserunner_slice5a.md` |
| **2.2** | No amendment protocol for governance code (orchestrator, gates, firewall) | **Hard ceiling, not in any roadmap doc** | Pass B closes this |
| **2.3** | Anti-Venom is static, not adaptive | **Framework's load-bearing novel claim, not in any roadmap doc** | Pass C scope (§23.6) |
| **2.4** | Cognitive primitives are fixed, not composable (subagent kinds hand-written) | Future scope; no Pass yet | n/a |
| **2.5** | Cross-Trinity scope deferred | Operator decision 2026-04-26: Trinity-wide eventually; Body-only execution focus for now | §15 of this PRD; §23.7 |

Pass B (`memory/project_reverse_russian_doll_pass_b.md`) closes Rungs 2.1 + 2.2 *jointly*. Pass C will close Rung 2.3. Rungs 2.4 + 2.5 are reserved for future work.

#### 23.5.3 Why Order 2 must be additively gated, not replaced

The Anti-Venom thesis (§23.6) is that as the cognitive engine reaches inward, the cage must scale proportionally. **No existing Iron Gate / SemanticGuardian / SemanticFirewall path may be loosened or replaced by Order-2 capability.** Order-2 changes traverse all existing Order-1 gates *plus* additional structural cages defined in Pass B:

1. The Order-2 manifest classifier (does this patch touch governance code?)
2. The `ORDER_2_GOVERNANCE` risk class (strictly above `BLOCKED` — no auto-apply at any nominal tier)
3. The AST-shape validator (for new `PhaseRunner` subclasses against the W2(5) Slice 1 frozen ABC)
4. The shadow-pipeline regression-replay (against a curated 20-op golden corpus from the battle-test breakthrough log)
5. The manifest-amendment protocol (operator-only mutation path for the manifest itself; locked-true invariant)

Composition rule: **Order-2 cages are additive to Order-1 gates, never substitutive.**

### 23.6 Anti-Venom — the Adaptive Immune System Thesis

**As the outward shell expands, the inward cage must scale proportionally.**

This is the framework's load-bearing novel claim. None of the four canonical docs articulate adaptive gate calibration; the existing immune system is uniformly *static*:

| Anti-Venom layer | Subsystem | Adaptive today? |
|---|---|---|
| AST validation | `change_engine` AST parser + multi-file coverage gate (Iron Gate 5) | **Static** — patterns fixed at code-time |
| Exploration-first | Iron Gate 1 (`JARVIS_EXPLORATION_GATE`) + ExplorationLedger 5-category diversity scoring | **Static** — category floors are env-tuned, not learned |
| ASCII strictness | Iron Gate 2 (`JARVIS_ASCII_GATE`) | **Static** — codepoint set fixed |
| Pre-APPLY pattern detection | SemanticGuardian (10 AST/regex patterns, ~10ms, zero LLM) | **Static** — patterns hand-written |
| Mutation cage | `ScopedToolBackend._mutations_count` structural COUNT gate | **Static cap** — budget set per-invocation |
| Injection cage | SemanticFirewall (11 detectors, 5 credential shapes, recursion ban) | **Static** — detector set hand-written |
| Risk-tier floor | 4-tier ladder + 3 composing knobs | **Static** — strictest-wins composition; thresholds operator-set |
| Isolation | L3 worktree with hard-fail (no silent shared-tree fallback) | **Static** — isolation policy is binary |
| Atomic rollback | Multi-file batch snapshots | **Static** — semantics fixed |

**The Anti-Venom thesis**: every entry above must, eventually, learn. Concretely:

- SemanticGuardian patterns mined from the POSTMORTEM corpus (failure → new detector)
- Iron Gate exploration-floors auto-tightening when a category-bypass is detected in graduated ops
- Mutation budgets calibrated **per Order**: Order-1 ops get the current budget; Order-2 ops (touching governance code) get a much smaller one (e.g. 1)
- Risk-tier ladder learning new tiers as new attack surfaces emerge from Order-1 outward expansion
- ExplorationLedger weighting adjusting when categories show diminishing-marginal-value across ops

Pass C (forward pointer §23.10) designs how each of these grows adaptive. Pass C's existence depends on Pass B — you cannot grow an adaptive immune system if amending the immune system itself isn't a defined protocol.

### 23.7 Trinity-Wide Order-2 Manifest Architecture

Per §22 the larger frame is Body + Mind + Soul. Per the operator decision recorded 2026-04-26:

> Order 2 = **Trinity-wide eventually**; **JARVIS-Body cognitive code only for now** (phased buildout via O+V & Anti-Venom).

The Pass B design surface honors both halves of this decision via the `(repo, path-glob)` manifest schema documented in `memory/project_reverse_russian_doll_pass_b.md` §3:

```python
@dataclass(frozen=True)
class Order2ManifestEntry:
    repo: str           # RepoRegistry key: "jarvis" | "jarvis-prime" | "jarvis-reactor"
    path_glob: str      # POSIX glob relative to repo root
    rationale: str      # Why this path is governance code (audit-readable)
    added: str          # ISO date the entry landed
    added_by: str       # "operator" | "<commit-sha>"
```

The schema is **Trinity-ready from day one**. Initial deployment registers only `repo: jarvis` entries (Body-only execution focus). Adding J-Prime or Reactor Core later is one entry per file — no schema change, no logic change, no orchestrator refactor. The `RepoRegistry` (`backend/core/ouroboros/governance/multi_repo/registry.py`) already resolves `jarvis-prime` and `jarvis-reactor` to filesystem paths via `JARVIS_PRIME_REPO_PATH` and `JARVIS_REACTOR_REPO_PATH`.

**This means**: the framework supports Trinity-wide self-modification authority even though the *initial enforcement* is Body-only. When the deferred cross-Trinity integration document referenced in §15 lands, activating Mind/Soul Order-2 enforcement is a manifest amendment, not an architecture change.

### 23.8 Composition with the Phase 1–6 Roadmap

The Order axis is orthogonal to the Phase axis. They compose without conflict:

| Phase × Order | Order 1 (Body) | Order 2 (Cognitive substrate) |
|---|---|---|
| **Phase 1 — Self-Reading** | PostmortemRecallService + cross-session direction memory (this PRD §9.P0–P0.5) | future: cognitive substrate reads its own evolution log; postmortems of Order-2 ops feed back into Order-2 caging |
| **Phase 2 — Self-Direction** | SelfGoalFormation forming Order-1 backlog entries (§9.P0) | future: O+V proposes new PhaseRunner subclasses to address recurring postmortem patterns |
| **Phase 3 — Operator Symbiosis** | ConversationOrchestrator (§9.P2), inline approval UX | conversational approval surface for Order-2 manifest amendments (extends `/order2 amend`) |
| **Phase 4 — Cognitive Metrics** | composite score per Order-1 op (Wang Imp #1) | separate composite score for Order-2 ops with different weights (Pass B §12 open question) |
| **Phase 5 — Adversarial Depth** | AdversarialReviewer subagent on Order-1 plans (§9.P5) | future: AdversarialReviewer evaluates proposed PhaseRunner subclasses against the shadow-replay corpus |
| **Phase 6 — Self-Modeling** | weekly behavior summary of Order-1 ops (§9) | future: weekly summary of Order-2 evolution (governance code change history, manifest amendments, gate calibration trajectory) |

**No Phase requires Order 2 to land.** Phases 1–6 are Order-1 work. Order 2 is a separate horizon that opens after Pass B graduates, and each Phase has a natural Order-2 extension.

### 23.9 Composition with Wang RSI Convergence (§5)

Wang's framework (§5) proves that score-monotonic optimization converges in expected O(log n) steps under the Markov assumption. The Order axis adds a constraint: **Wang's score must be score-of-Order-1 ops, not score-of-all-ops.**

Reasoning: Order-2 ops are by definition rare, structurally caged, and cannot auto-apply. Folding Order-2 ops into the same composite-score window as Order-1 ops would produce noise that overwhelms the Order-1 signal. The Pass C design (§23.10) will likely introduce:

- **Composite Score (Order-1)**: existing 5-component formula (test_delta + coverage_delta + complexity_delta + lint_delta + blast_radius), Wang-grounded
- **Composite Score (Order-2)**: separate metric with different weights — blast_radius dominates (e.g. 0.6+), test_delta de-weighted (Order-2 patches often add new structural cages without changing existing tests)

The Wang convergence claim then lives at Order-1 only. Order-2 ops are not convergence-monotonic; they're authority-gated discrete events. This is consistent with §5.6 ("The convergence threshold") — the threshold metrics there are Order-1 metrics by construction.

### 23.10 Pass A → Pass B → Pass C — the Three-Pass Sequence

The framework's operationalization proceeds in three Passes, each producing a memory-file deliverable:

#### Pass A — Reconciliation (complete, 2026-04-26)

`memory/project_reverse_russian_doll_pass_a.md`

- Verified the Order axis is genuinely new vocabulary (zero hits across the four canonical docs for "Order 0/1/2" or "Reverse Russian Doll").
- Mapped Order 1 to existing subsystems with file:line citations (the table in §23.4.1 derives from this).
- Mapped Anti-Venom layers to existing gates and confirmed all are static (the table in §23.6 derives from this).
- Identified the five Rungs (Gaps 2.1–2.5) separating "Order 1 ships" from "Order 2 real."
- Surfaced three operator decisions; all three ratified 2026-04-26 (§23.11).

#### Pass B — Joint Design for Rungs 2.1 + 2.2 (drafted 2026-04-26; execution gated on W2(5) Slice 5b)

`memory/project_reverse_russian_doll_pass_b.md`

Six-slice plan, mirroring the W2(5) PhaseRunner extraction discipline (per-slice flag, parity tests, authority invariants, full-revert matrix, defaults all `false` until graduation):

| Slice | Deliverable | Env flag |
|---|---|---|
| 1 | `Order2Manifest` schema + loader + `.jarvis/order2_manifest.yaml` initial Body-only entries | `JARVIS_ORDER2_MANIFEST_LOADED` (default false) |
| 2 | `ORDER_2_GOVERNANCE` risk class + `risk_tier_floor.py` integration + GATE classifier hook | `JARVIS_ORDER2_RISK_CLASS_ENABLED` (default false) |
| 3 | AST-shape validator for new PhaseRunner subclasses (6-rule check against W2(5) Slice 1 frozen ABC) | `JARVIS_PHASE_RUNNER_AST_VALIDATOR_ENABLED` (default false) |
| 4 | Shadow-pipeline replay corpus (20 ops curated from battle-test breakthrough log) + structural-equality diff harness | `JARVIS_SHADOW_PIPELINE_ENABLED` (default false) |
| 5 | `MetaPhaseRunner` primitive composing Slices 1–4 | `JARVIS_META_PHASE_RUNNER_ENABLED` (default false) |
| 6 | Manifest-amendment protocol: `/order2 {pending,show,amend,reject,history}` REPL + `order2_review` queue + AutoCommitter trailer | `JARVIS_ORDER2_MANIFEST_AMENDMENT_REQUIRES_OPERATOR` (**locked true**, never flipped) |

Pass B is **design only — no code, no flag flips** until W2(5) Slice 5b authorizes execution.

#### Pass C — Adaptive Anti-Venom (deferred; depends on Pass B existing)

Per §23.6, Pass C designs how each static gate grows adaptive. Pass C's existence depends on Pass B because you cannot grow an adaptive immune system if amending the immune system itself isn't a defined protocol. Pass C scope draft will follow Pass B Slice 1 graduation.

### 23.11 Operator Decisions Ratified 2026-04-26

The Pass A reconciliation surfaced three open operator decisions. All three were ratified in the architectural review:

1. **Order-2 scope**: **Trinity-wide eventually; JARVIS-Body cognitive code only for now.** Phased buildout via O+V & Anti-Venom. The `(repo, path-glob)` manifest schema (§23.7) honors both halves.
2. **Pass B sequencing**: **Draft now; flip-gate execution on W2(5) Slice 5b authorization.** Pass B drafted 2026-04-26; per-slice graduation cadence determined post-W2(5) Slice 5b.
3. **Vocabulary landing**: **Add the Reverse Russian Doll Order axis to this PRD as a new section orthogonal to Phase 1–6.** This §23 is the deliverable for that decision.

### 23.12 Implementation Discipline + Cross-References

**Authority invariants** (Pass B §3.4 + §8):

- The Order-2 manifest is **read** by the Order-2 risk classifier (GATE phase) and the `MetaPhaseRunner` AST validator (§23.5.3). It is **written** only by the §23.10 manifest-amendment protocol — never by APPLY, never by AutoCommitter, never by O+V autonomous proposal.
- AST-grep CI invariant: any future import of `Order2Manifest` outside `risk_tier_floor.py`, `meta_phase_runner.py`, and the manifest amendment path is a CI failure.
- `JARVIS_ORDER2_MANIFEST_AMENDMENT_REQUIRES_OPERATOR=true` is **not a graduation candidate** — it ships locked-true. Flipping it false is itself an Order-2 governance change. The flag exists only so the property is greppable and asserted in tests.
- No Order-1 gate (Iron Gate, SemanticGuardian, SemanticFirewall, exploration ledger, mutation cage, risk-tier floor) may be loosened by an Order-2 capability. Order-2 cages are **additive** to Order-1 gates, never substitutive.

**Hot-revert path**: every Pass B slice ships behind a per-slice env flag defaulting `false` (or locked-true for §23.10 Slice 6). Reverting Pass B is `unset` of all five non-locked flags + revert of `.jarvis/order2_manifest.yaml`. Mirrors the W2(5) and W3(7) revert discipline (`docs/operations/wave3-parallel-dispatch-graduation.md`).

**Cross-references** (canonical):

| Document | Relationship to §23 |
|---|---|
| `memory/project_reverse_russian_doll_pass_a.md` | Pass A — reconciliation source for §23.4.1, §23.6, §23.10 |
| `memory/project_reverse_russian_doll_pass_b.md` | Pass B — design source for §23.5.3, §23.7, §23.10, §23.12 |
| §5 (this PRD) — RSI Convergence Framework | Wang's framework; complementary mathematical lens (§23.9) |
| §9 (this PRD) — Roadmap | Phase 1–6 work; orthogonal to Order axis (§23.8) |
| §22 (this PRD) — The Larger Frame | Trinity Body/Mind/Soul scope; §23.7 honors §22's Body-first sequencing |
| `CLAUDE.md` Battle Test Milestones | Order-1 graduation evidence (§23.4.2) |
| `docs/architecture/OUROBOROS.md` battle-test breakthrough log | Source corpus for Pass B Slice 4 shadow-replay (§23.10) |
| `docs/architecture/RSI_CONVERGENCE_FRAMEWORK.md` | Wang's mathematical foundation; complemented by Order axis |

**Status**: §23 lands as doctrine. It does not gate or block any in-flight work — Pass B execution is gated separately on W2(5) Slice 5b authorization (§23.10), and Phases 1–6 (§9) do not depend on Order 2 (§23.8).

---

## Appendix A — Glossary

### Core terms

- **O+V**: Ouroboros (governance) + Venom (tool execution) — the autonomous self-development engine
- **CC**: Claude Code (Anthropic's interactive CLI) — the comparator
- **RSI**: Recursive Self-Improvement — system that improves itself; Wang's mathematical formulation grounds the claim
- **Wang's framework**: per `arXiv:1805.06610`, the Markov chain + Dijkstra-like score-construction proof that RSI converges in O(log n)
- **Trinity**: Body (JARVIS) + Mind (J-Prime) + Soul (Reactor Core)

### O+V infrastructure terms

- **POSTMORTEM**: structured failure record produced after each op
- **Iron Gate**: deterministic post-GENERATE gates (exploration ledger, ASCII strict, multi-file coverage)
- **SemanticGuardian**: pre-APPLY pattern detector
- **DirectionInferrer**: signal → 4-value posture (EXPLORE/CONSOLIDATE/HARDEN/MAINTAIN)
- **SemanticIndex**: recency-weighted centroid over commits/goals/conversation
- **ConversationBridge**: sanitized signal channel from dialogue → CONTEXT_EXPANSION
- **PLAN-EXPLOIT**: parallel multi-stream GENERATE for multi-file ops
- **Cost Governor**: per-op financial circuit-breaker
- **Cancel Token**: W3(7) cooperative-cancel infrastructure
- **F1 / F2 / F3**: intake priority queue / routing-hint priority-0.5 / urgency override (the Wave 3 fix-cascade)

### PRD-introduced terms

- **PostmortemRecall**: P0 service that consults prior postmortems at decision time
- **SelfGoalFormation**: P1 mechanism for the model to write its own backlog entries
- **HypothesisLedger**: P1.5 structured record of self-formed-goal predictions + outcomes
- **ConversationOrchestrator**: P2 router for natural-language operator turns
- **AdversarialReviewer**: P5 subagent that finds 3+ failure modes per plan
- **SelfNarrative**: P6 weekly behavior summary
- **Composite Score**: Wang Improvement 1 — unified quality metric per op
- **Convergence State**: classifier `IMPROVING/PLATEAU/OSCILLATING/DEGRADING` from rolling score window

### Reverse Russian Doll vocabulary (§23)

- **Reverse Russian Doll**: architectural lens for self-improvement; the core ("cognitive engine") carves an exponentially larger shell around itself rather than compressing inward (operator framing 2026-04-26)
- **Order**: layer of self-reference at which an O+V improvement operates; orthogonal to Phase (§9) and Tier (`JARVIS_LEVEL_OUROBOROS.md`)
- **Order 0**: industry default — AI as exoskeleton, frozen between turns; Ouroboros rejects this baseline by design
- **Order 1**: O+V acting on the body (JARVIS application code, sensors, tooling, tests, docs, config); current shipping state
- **Order 2**: O+V acting on its own cognitive substrate (orchestrator FSM, immune system gates, change engine, risk-tier ladder, PhaseRunner classes); horizon, additively gated, no auto-apply ever
- **Anti-Venom**: thesis that the immune system (Iron Gate, SemanticGuardian, SemanticFirewall, mutation cage, risk-tier floor) must scale proportionally as O+V's outward reach grows; today static, Pass C scope to grow adaptive
- **Order-2 manifest**: `(repo, path-glob)` registry of governance-code paths; Trinity-extensible from day one; written only via the operator-only manifest-amendment protocol
- **`ORDER_2_GOVERNANCE`**: risk class strictly above `BLOCKED`; no auto-apply at any nominal tier; cannot be cleared by REPL `approve <op-id>`; only by `/order2 amend <op-id>`
- **MetaPhaseRunner**: Pass B Slice 5 primitive composing the Order-2 manifest classifier + AST-shape validator + shadow-pipeline replay; the cage through which O+V proposes new `PhaseRunner` subclasses
- **Shadow-pipeline replay**: structural-equality diff against a curated 20-op golden corpus from the battle-test breakthrough log; pre-APPLY regression cage for Order-2 PhaseRunner candidates
- **Pass A / Pass B / Pass C**: the three-pass operationalization sequence — A = reconciliation (complete), B = joint design for Rungs 2.1+2.2 (drafted, gated on W2(5) Slice 5b), C = adaptive Anti-Venom (deferred, depends on Pass B)

---

## Appendix B — Reference Documents Map

### Architecture documents (canonical)

| Document | Purpose | Relationship to this PRD |
|---|---|---|
| `CLAUDE.md` | Current architecture + governing principles | Source of truth for "what exists today" |
| `docs/architecture/OUROBOROS.md` | Battle-test breakthrough log + 24-section pipeline reference | Background reading; complements §3 |
| `docs/architecture/RSI_CONVERGENCE_FRAMEWORK.md` | Wang's RSI mathematical foundation + 6 improvements | Sourced for §5; Phase 0 audit verifies status |
| `docs/architecture/JARVIS_LEVEL_OUROBOROS.md` | Higher-level JARVIS context | Background |
| `docs/architecture/BRAIN_ROUTING.md` | Provider routing architecture | Background for UrgencyRouter |
| `docs/architecture/SUBAGENT_PHASE1_ARCHITECTURE.md` | Phase 1 subagent design | Background for Phase 5 (Adversarial Reviewer) |
| `docs/architecture/CLAUDE_MYTHOS_OV_INTEGRATION.md` | OV integration with Claude/CC mythology | Background for §22 Trinity context |
| `docs/architecture/OV_RESEARCH_PAPER_2026-04-16.md` | Research paper format of O+V | Background; some overlap with this PRD |

### Reverse Russian Doll architecture (§23)

| Document | Purpose | Relationship to this PRD |
|---|---|---|
| `memory/project_reverse_russian_doll_pass_a.md` | Pass A reconciliation — verifies Order axis is genuinely new vocabulary; maps Order 1 to existing subsystems with file:line citations; identifies 5 Rungs (Gaps 2.1–2.5) | Source for §23.1, §23.4.1, §23.6, §23.10 |
| `memory/project_reverse_russian_doll_pass_b.md` | Pass B joint design for Rungs 2.1+2.2 — Order-2 manifest schema, `ORDER_2_GOVERNANCE` risk class, AST-shape validator, shadow-pipeline replay, `MetaPhaseRunner`, manifest-amendment protocol | Source for §23.5.3, §23.7, §23.10, §23.12; execution gated on W2(5) Slice 5b |

### Operations runbooks

| Document | Purpose |
|---|---|
| `docs/operations/curiosity-graduation.md` | W2(4) hot-revert + env reference |
| `docs/operations/wave3-parallel-dispatch-graduation.md` | W3(6) hot-revert + cadence protocol |
| `docs/operations/battle_test_runbook.md` | Battle-test harness operator reference |
| `docs/operations/vision-sensor-slice-{1,2,3,4}-graduation.md` | VisionSensor graduation series (background) |

### Memory documents (operator's session memory, NOT in repo)

| Document | Purpose |
|---|---|
| `memory/project_rsi_convergence.md` | RSI framework status (per memory: 6 improvements documented, implementation TBD) |
| `memory/project_wave3_item6_graduation_matrix.md` | W3(6) cadence ledger (closed) |
| `memory/project_w2_4_curiosity_closure.md` | W2(4) closure record |
| `memory/project_phase_1_subagent_graduation.md` | Phase 1 subagent precedent |
| `memory/project_ouroboros_direction.md` | Strategic direction for O+V |
| `memory/feedback_orchestrator_wiring_invariant_checklist.md` | Wiring invariant test pattern (consumed by §11 Layer 2) |

### Code references (current state — verify before citing)

- `backend/core/ouroboros/governance/orchestrator.py` (102K-line monolithic supervisor)
- `backend/core/ouroboros/governance/phase_dispatcher.py` (Wave 2 (5) extracted dispatcher)
- `backend/core/ouroboros/governance/candidate_generator.py` (provider routing + outer-retry from #19706)
- `backend/core/ouroboros/governance/cost_governor.py` (financial circuit-breaker + #19800 parallel-stream bump)
- `backend/core/ouroboros/governance/parallel_dispatch.py` (W3(6) parallel L3 fan-out)
- `backend/core/ouroboros/governance/cancel_token.py` (W3(7) mid-op cancel)
- `backend/core/ouroboros/governance/curiosity_engine.py` (W2(4) curiosity)
- `backend/core/ouroboros/governance/autonomy/safety_net.py` (#20147 L3 auto-recovery)
- `backend/core/ouroboros/governance/intake/intake_priority_queue.py` (F1)
- `backend/core/ouroboros/governance/intake/sensors/backlog_sensor.py` (F2 routing_hint)
- `backend/core/ouroboros/governance/posture_observer.py` (DirectionInferrer)
- `backend/core/ouroboros/governance/semantic_index.py` (SemanticIndex)
- `backend/core/ouroboros/governance/conversation_bridge.py` (ConversationBridge)
- `backend/core/ouroboros/governance/last_session_summary.py` (LastSessionSummary)
- `backend/core/ouroboros/governance/comm_protocol.py` (5-phase observability)

### External references

- Wang, W. *"A Formulation of RSI & Its Possible Efficiency"* — UBC, arXiv:1805.06610
- Anthropic API documentation (rate limits, error taxonomy)
- aiohttp / anyio / httpx documentation (transport-layer for resilience pack)

---

## Appendix C — Phase Gate Criteria (entry/exit conditions)

### Phase 0 — Pre-Phase audit (1 day)

**Entry**: Operator authorizes PRD execution begin.

**Tasks**:
- Audit `docs/architecture/RSI_CONVERGENCE_FRAMEWORK.md` against current code state
- Verify which of the 6 Wang improvement modules exist (`composite_score.py`, `convergence_tracker.py`, `oracle_prescorer.py`, `transition_tracker.py`, `vindication_reflector.py`, adaptive-graduation-threshold modifications)
- Update §5.2 of this PRD with verified status
- Update §9 Phase 4 acceptance criteria if Wang modules are partially implemented

**Exit**: PRD §5.2 reflects actual code state. Phase 1 + Phase 4 implementation can begin without duplicate-work risk.

### Phase 1 — Self-Reading

**Entry conditions**:
- Phase 0 complete
- Operator green-lights P0 implementation
- SemanticIndex healthy (cache file exists, embedding model installed)

**Per-slice exit** (graduation cadence per W2/W3 pattern):
- 3 clean live battle-test sessions with PostmortemRecall firing on ≥ 1 op per session
- 0 traceback frames in `postmortem_recall_service.py` across the cadence
- Operator-authorized default flip
- 1 post-flip confirmation soak

**Phase exit (both P0 + P0.5 graduated)**:
- See §13 Phase 1 row

### Phase 2 — Self-Direction

**Entry conditions**:
- Phase 1 graduated
- Operator green-lights P1 implementation
- POSTMORTEM history ≥ 50 entries (gives clusters something to find)

**Per-slice exit**: same 3-clean cadence pattern.

**Phase exit**:
- See §13 Phase 2 row
- Plus: ≥ 1 self-formed goal led to a successful APPLY (proves the loop closes)

### Phase 3 — Operator Symbiosis

**Entry conditions**:
- Phase 1 graduated (so postmortem recall is available to inform conversational responses)
- Operator green-lights P2

**Per-slice exit**: same 3-clean cadence + UX testing (operator uses it for ≥ 1 week).

**Phase exit**:
- See §13 Phase 3 row

### Phase 4 — Cognitive Metrics

**Entry conditions**:
- Phase 0 complete (Wang implementation status known)
- Operator green-lights P4
- Can run in parallel to Phase 1 if Wang composite score module already exists

**Per-slice exit**:
- All 7 metrics computed at session end
- 30-day rolling history visible
- Composite score computed for ≥ 100 ops (may take weeks of operation to accumulate)

**Phase exit**:
- See §13 Phase 4 row

### Phase 5 — Adversarial Depth

**Entry conditions**:
- Phase 1 + Phase 4 graduated (adversarial reviewer needs metrics + history)
- Phase 2 graduated OR explicitly waived (most value on self-formed goals)
- Operator green-lights P5

**Per-slice exit**: same 3-clean cadence + ≥ 1 adversarial finding caught a real bug.

**Phase exit**:
- See §13 Phase 5 row

### Phase 6 — Self-Modeling

**Entry conditions**:
- Phases 1-5 graduated
- Operator green-lights P6
- ≥ 4 weeks of accumulated metrics + postmortems + commits

**Per-slice exit**:
- Weekly self-narrative auto-PR'd for 4 consecutive weeks
- Operator finds ≥ 50% of narratives "useful enough to read fully"

**Phase exit**:
- See §13 Phase 6 row

### A-Level exit

When all 6 phases exit + §6 A-level signals all met → O+V is A-level.

### MVP RSI exit

When §5.4 MVP RSI conditions all met → claim Wang-grounded RSI.

---

## Appendix D — Document History

| Date | Version | Change | Author |
|---|---|---|---|
| 2026-04-25 | 1.0 | Initial draft | Claude Opus 4.7 (synthesis from 7-day operator collaboration) |
| 2026-04-25 | 2.0 | Added: TOC, §4 Cognitive Scaffolding deep dive, §5 RSI Convergence Framework, §8 Manifesto alignment, §10 Per-phase telemetry, §11 Per-phase testing, §18 Stakeholder map, §19 Migration & versioning. Expanded: §22 Trinity context, App A glossary, App B reference docs map, App C phase gate criteria. | Claude Opus 4.7 (per operator request: "more depth, RSI section, more references") |
| 2026-04-25 | 2.1 | Added §1 "Roadmap Execution Status (live)" subsection — per-slice [x]/[~]/[ ] tracking. Records: Phase 0 audit complete; Phase 1 P0 build (PR #20968) + live-fire smoke + graduation pins landed; P0 master-flag flip pending 3-clean-session cadence. Update discipline noted: each closing slice updates this section in same PR. | Claude Opus 4.7 (P0 follow-on PR) |
| 2026-04-26 | 2.2 | P0 reachability supplement: 3/3 live-cadence soak attempts hit the known BG-starvation pattern (W3(6) memory). Pivoted to W3(6) Layer 3 reachability supplement precedent — extracted CONTEXT_EXPANSION wiring to `_inject_postmortem_recall_impl` (mirrors LSS), added orchestrator-level smoke (9 tests covering integration / concat contract / authority invariants / AST regression). Layered evidence now totals 67 deterministic tests + 16 in-process smoke. Master-flag flip gates on operator review of layered evidence (no further live cadence required). | Claude Opus 4.7 (option-2 deliverable) |
| 2026-04-26 | 2.3 | **Phase 1 P0 GRADUATED.** `JARVIS_POSTMORTEM_RECALL_ENABLED` default flipped `false`→`true`. Pre-graduation pin renamed to `test_master_flag_default_true_post_graduation` per its embedded instructions. Source-grep pin updated to assert `_env_bool(..., True)` literal. PRD §1 status row marked `[x]`. Hot-revert: single env knob (`JARVIS_POSTMORTEM_RECALL_ENABLED=false`). First cognitive feedback loop closed end-to-end. | Claude Opus 4.7 (graduation flip PR) |
| 2026-04-26 | 2.4 | Doc-only fix to §1 Roadmap Execution Status: corrected three mislabeled rows to match PRD §9 truth. P0.5 was "POSTMORTEM root-cause taxonomy expansion" → now "Cross-session direction memory (DirectionInferrer + LSS + 100-commit git momentum)". P1 was "Cross-session pattern detector" → now "Curiosity Engine v2 (model writes backlog entries)". P1.5 was "Self-RAG over own commit history" → now "Hypothesis ledger". P1/P1.5 also relocated from a fictional "Phase 1" sub-list into the "Phase 2 — Self-Direction" group where §9 places them. Zero behavior change. | Claude Opus 4.7 (post-graduation cleanup) |
| 2026-04-26 | 2.5 | **Phase 1 P0.5 GRADUATED.** 3-slice arc landed (Slice 1 git_momentum extraction → Slice 2 arc_context consumer → Slice 3 REPL surfacing + graduation). `JARVIS_DIRECTION_INFERRER_ARC_CONTEXT_ENABLED` default flipped `false`→`true`. Pre-graduation pin renamed per its embedded instructions. Layered evidence: 282 deterministic tests + 31 in-process smoke + comprehensive graduation pin suite (17 pins) + posture-observer reachability supplement (7 tests). Bounded-nudge safety: ≤0.10/posture cap, provably cannot override clear winner. Hot-revert: single env knob. Second cognitive feedback loop closed end-to-end. | Claude Opus 4.7 (P0.5 Slice 3 graduation PR) |
| 2026-04-26 | 2.6 | **Phase 2 P1 GRADUATED.** 5-slice arc landed (Slice 1 clusterer → Slice 2 engine → Slice 3 sensor consumer → Slice 4 REPL → Slice 5 graduation). DUAL master flags: `JARVIS_SELF_GOAL_FORMATION_ENABLED` + `JARVIS_BACKLOG_AUTO_PROPOSED_ENABLED` both default flipped `false`→`true`. Layered evidence: 158 deterministic tests + 18 in-process live-fire + end-to-end integration test. Bounded-by-construction safety stack (per-session cap=1, cost cap=$0.10, posture veto, blocklist dedup, operator-review tier requires_human_ack=True). Hot-revert: independent env knob per flag. **The line between automation and autonomy** — first self-formed-goal feedback loop now live by default. | Claude Opus 4.7 (P1 Slice 5 graduation PR) |
| 2026-04-26 | 2.7 | **Phase 2 P1.5 GRADUATED.** 2-slice arc landed (Slice 1 hypothesis_ledger primitive + REPL → Slice 2 engine integration + validator + graduation). `JARVIS_HYPOTHESIS_PAIRING_ENABLED` default flipped `false`→`true`. Engine extends model prompt to emit paired Hypothesis (claim + expected_outcome); auto-validator does token-overlap matching (overlap≥0.5 → True; ≤0.1 → False; middle band → None) + records back to ledger. Layered evidence: 74 deterministic tests + 15 in-process live-fire + end-to-end integration (engine emit → validator decide → ledger updated → stats reflected). Bounded-by-construction safety stack from P1 unchanged. Hot-revert: single env knob. **Phase 2 entirely closed** — every self-formed goal now testable by construction. | Claude Opus 4.7 (P1.5 Slice 2 graduation PR) |
| 2026-04-26 | 2.8 | **Phase 4 P3 GRADUATED — both stranded RSI modules un-stranded.** 2-slice arc landed (Slice 1 cognitive_metrics wrapper + `/cognitive` REPL → Slice 2 orchestrator integration + graduation). `JARVIS_COGNITIVE_METRICS_ENABLED` default flipped `false`→`true`. Orchestrator boot wires `CognitiveMetricsService` singleton with the live Oracle; CONTEXT_EXPANSION calls `_score_cognitive_metrics_pre_apply_impl` after PostmortemRecall (advisory-only, never blocks FSM). Both `OraclePreScorer` + `VindicationReflector` accessible via REPL even when wrapper short-circuits. Layered evidence: 63 deterministic tests (43 wrapper + 19 graduation pins + 1 sequence) + 15 in-process live-fire. Vindication call site at post-APPLY tracked as future work — wrapper itself is graduated. Hot-revert: single env knob. | Claude Opus 4.7 (P3 Slice 2 graduation PR) |
| 2026-04-26 | 2.9 | Doc-only fix to §23.8 Phase × Order composition table — Phase 1 / Order 1 cell now matches §9 truth: drops "SelfRAG over commit history" (never adopted into canonical roadmap; was speculative content from PRD v2.1) and changes the §9 reference from "P0–P1.5" to "P0–P0.5" (Phase 1 in §9 contains only P0 + P0.5; the canonical P1.5 is the Hypothesis ledger under Phase 2). Zero behavior change. Operator-binding: do not reintroduce a "Phase 1 P1.5" label anywhere. | Claude Opus 4.7 (post-P3 cleanup) |
| 2026-04-26 | 2.23 | **Reverse Russian Doll Pass C Slice 4 — combined per-Order mutation budget + risk-tier ladder extender shipped.** Two sub-surfaces in one slice per §8 design ("combined slice graduates when both sub-surfaces have 5 clean each"). **Slice 4a** (`per_order_mutation_budget.py`): proposes lowering per-Order budget when ops consistently used fewer mutations than budgeted; uses max-observed as the conservative new budget; Order-2 hard floor MIN_ORDER2_BUDGET=1; surface validator pins kind=lower_budget + sha256-hash + threshold + summary-→. **Slice 4b** (`risk_tier_extender.py`): identifies novel failure_class values not in DEFAULT_KNOWN_FAILURE_CLASSES; classifies blast_radius into 4 bands (HARDENED at 3 levels + CRITICAL at top); synthesizes deterministic tier name + proposes insertion between two existing tiers (strictly tightening — ladder only grows); surface validator pins kind=add_tier + sha256-hash + threshold + summary-contains-insert/between. Both sub-surfaces auto-register their validators at module-import; both independently default-off (`JARVIS_ADAPTIVE_PER_ORDER_BUDGET_ENABLED` + `JARVIS_ADAPTIVE_RISK_TIER_LADDER_ENABLED`). **63 regression pins** (4a: 22, 4b: 24, cross-surface: 5, shared: 12) + **232/232 combined Pass C tests green** (Slice 1+2+3+4). Per §8.3: tier extension is strictly additive (insertion grows the ladder; existing tier behavior preserved for ops not matching the new class). | Claude Opus 4.7 (Pass C Slice 4 PR) |
| 2026-04-26 | 2.22 | **Reverse Russian Doll Pass C Slice 3 — IronGate exploration-floor auto-tightener shipped.** Second adaptive surface on the Slice 1 substrate. New module `backend/core/ouroboros/governance/adaptation/exploration_floor_tightener.py`: pure stdlib analyzer of (exploration-score, verify-outcome) tuples per op. **Bypass-failure detector** (floor_satisfied=True AND verify_outcome IN {regression, failed}) identifies ops where the exploration gate was bypassed. **Weakest-category identification** via per-op argmin + group-count winner across the window (alpha tie-break for determinism). **Bounded 10% raise per cycle** via `compute_proposed_floor()` with min_nominal_raise=1 floor. Per-cycle pct hard-capped at 100% to prevent operator-typo runaway. Auto-registers per-surface validator: kind=raise_floor + sha256-prefix hash + observation_count-above-threshold + summary-contains-→-indicator (defense against doctored proposals). Idempotent proposal_id (sha256 of category + current + proposed floor). Master flag `JARVIS_ADAPTIVE_IRON_GATE_FLOORS_ENABLED` default false. Threshold default 5 (higher than Slice 2's 3 — floor-raise has broader impact than one detector pattern). Per §7.1 design: "one weakest candidate per cycle" keeps the operator-review surface trim. **55 regression pins** + **169/169 combined Pass C tests green** (Slice 1+2+3). | Claude Opus 4.7 (Pass C Slice 3 PR) |
| 2026-04-26 | 2.21 | **Reverse Russian Doll Pass C Slice 2 — SemanticGuardian POSTMORTEM-mined patterns shipped.** First adaptive surface on the Slice 1 substrate. New module `backend/core/ouroboros/governance/adaptation/semantic_guardian_miner.py`: pure stdlib-only longest-common-substring detector synthesizer + group-by-(root_cause, failure_class) + window filter + existing-pattern duplicate check + idempotent proposal_id (hash of group+pattern) so re-mining the same events yields DUPLICATE_PROPOSAL_ID at the substrate layer. End-to-end `propose_patterns_from_events()` flows through Slice 1's `AdaptationLedger.propose()`. Auto-registers a per-surface validator at module-import enforcing: kind == "add_pattern" + proposed_state_hash sha256-prefixed + observation_count >= threshold floor. Master flag `JARVIS_ADAPTIVE_SEMANTIC_GUARDIAN_ENABLED` (default false). Bounded synthesis (MAX_EXCERPTS_PER_GROUP=32, MAX_SYNTHESIZED_PATTERN_CHARS=256, MIN_LCS_LENGTH=8) defends against multi-KB regex blobs and sub-3-char "matches-anything" patterns. Window filter retains epoch=0 events for back-compat. **54 regression pins** + **114/114 combined Pass C tests green** (Slice 1+2). Per §6.2: deterministic-only per zero-LLM-in-cage invariant; LCS is v1 — if too narrow, operator can extend the synthesizer via Pass B Order-2 amendment (it IS governance code). Slices 3-6 pending. | Claude Opus 4.7 (Pass C Slice 2 PR) |
| 2026-04-26 | 2.20 | **Reverse Russian Doll Pass C EXECUTION STARTED — Slice 1 (AdaptationLedger substrate) shipped.** Pass B Slice 1+2 prerequisites met; operator-authorized to begin Pass C (the genuine RSI architectural contribution per `memory/project_reverse_russian_doll_pass_a.md` "Anti-Venom adaptive thesis is genuinely novel"). New module `backend/core/ouroboros/governance/adaptation/ledger.py`: append-only JSONL audit log at `.jarvis/adaptation_ledger.jsonl` + 5-value `AdaptationSurface` enum (one per Pass C §3 thesis bullet) + 3-value `OperatorDecisionStatus` + 2-value `MonotonicTighteningVerdict` + frozen `AdaptationProposal`/`AdaptationEvidence` dataclasses (sha256 tamper-detect per record) + pluggable per-surface validator registry + universal `validate_monotonic_tightening()` that **refuses to persist loosening proposals** (load-bearing cage rule per §4.1: Pass C is one-way tighten-only; loosening goes through Pass B `/order2 amend`). Append-only invariant: state transitions write NEW lines, never rewrite. Latest-record-per-proposal-id wins for current state. `approve()` is the ONLY transition that flips `applied_at` non-null + makes the adaptation live. Stdlib-only import surface (AST-pinned to keep substrate acyclic — Slices 2-5 will import the substrate; substrate imports nothing of theirs). 60 regression pins covering module constants + 5 enums + dataclass-frozen + master flag + 7 propose paths (OK / DISABLED / 4 INVALID sub-cases / DUPLICATE / WOULD_LOOSEN with critical NOT-PERSISTED pin / surface-validator pass+reject+raise) + 6 decision paths + read queries + persistence (append-only / sha256 / tampered-skipped / malformed-skipped) + surface-validator routing + singleton + round-trip serialization + rollback_via field pin + 4 authority invariants. `JARVIS_ADAPTATION_LEDGER_ENABLED` default false. Slices 2-6 pending: 2 (SemanticGuardian POSTMORTEM-mined patterns), 3 (IronGate exploration-floor tightening), 4 (per-Order mutation budgets + risk-tier ladder extension), 5 (ExplorationLedger category-weight rebalance), 6 (MetaAdaptationGovernor + `/adapt` REPL + observability). | Claude Opus 4.7 (Pass C Slice 1 PR) |
| 2026-04-26 | 2.19 | **P3 P2 Slice 4 deferred follow-up PR 3 — `ClaudeChatActionExecutor` landed. CLOSES the 3-PR mini-arc + the third (final) deferred follow-up.** Wires `query_claude` against an injectable `ClaudeQueryProvider` (production wires `AnthropicClaudeQueryProvider` externally; tests inject fakes; default is `_NullClaudeQueryProvider` returning a sentinel — no API call, no cost — so misconfigured factory CANNOT accidentally hit the API). Cage: per-call cost cap ($0.05 mirrors AdversarialReviewer per-op budget) + cumulative session budget ($1.00) + bounded prompt (1024 chars) + bounded context (5 turns × 240 chars/fragment) + bounded response (4096 chars) + no auto-retry + persistent audit at `.jarvis/chat_claude_audit.jsonl` (6 outcomes captured: ok / empty_message / session_budget_exhausted / call_would_exceed_budget / provider_error / provider_non_string). AST-pinned that the executor does NOT import `providers.py` NOR `anthropic` directly (provider is injected — keeps chat decoupled from codegen + tests fast). New factory `build_chat_repl_dispatcher_with_claude()` chains through PR 2's subagent factory producing the **full 8-flag composition matrix**: all-on yields `Claude(Subagent(Backlog(Logging)))` — every Protocol method routes to its concrete implementation. Default-off behind `JARVIS_CHAT_EXECUTOR_CLAUDE_ENABLED`. 51 regression pins covering 7 module constants + 3 master flag pins + NullProvider safety + happy-path + recent-turns context + 4 truncation pins + 7 cage error paths + 4 audit row pins + 4 fallback-delegation pins + cage check + full-composition smoke (4 methods → 4 different files) + 8 factory wiring pins + 4 authority invariant pins + Protocol conformance + 3 audit-list pins. Combined: 287/287 tests green across PR 1+2+3 + chat_repl_dispatcher + conversation_orchestrator + intent_classifier. **Mini-arc total: 110 net-new regression pins across 3 PRs (27 + 32 + 51); the safe-default LoggingChatActionExecutor is now superseded by Claude(Subagent(Backlog(Logging))) when all three independent env flags are on.** Hot-revert: single env knob per executor (each independently default-off until graduation). All three deferred follow-ups from earlier graduated phases (P5 adversarial wiring + P4 metrics observer wiring + P2 chat executors) now closed. | Claude Opus 4.7 (P2 Slice 4 follow-up PR 3 — closes mini-arc) |
| 2026-04-26 | 2.18 | **P3 P2 Slice 4 deferred follow-up PR 2 — `SubagentChatActionExecutor` landed.** Second of three concrete chat executors. Wires `spawn_subagent` against `.jarvis/chat_subagent_queue.jsonl` via enqueue-and-return-ticket pattern (avoids blocking the `/chat` REPL on multi-second subagent runs; future `ChatSubagentSweeper` PR will dispatch the actual `AgenticExploreSubagent` from the queue). Ticket shape: `ticket_id="subagent:{turn_id}"`, `subagent_type="explore"` (only read-only type via this surface), provenance markers, `schema_version=1`. Per-method composition pattern preserved: other 3 Protocol methods delegate to fallback (defaults to LoggingChatActionExecutor; auto-composes Backlog(Logging) when PR 1's backlog flag also on). Default-off behind `JARVIS_CHAT_EXECUTOR_SUBAGENT_ENABLED`. New factory `build_chat_repl_dispatcher_with_subagent()` 4-flag composition matrix: subagent off → falls through to PR 1's backlog factory; subagent on + backlog off → Subagent(Logging); both on → Subagent(Backlog(Logging)); chat master off → None. AST-pinned no `AgenticExploreSubagent` / `SubagentScheduler` / `ExplorationSubagent` imports (cage). 32 regression pins covering module constants + master flag truthy/falsy + write-real-ticket + append + empty/whitespace-no-write + truncation + timestamp + audit + 5 fallback-delegation pins (incl. 3-method-3-file composition smoke) + 7 factory wiring pins + end-to-end smoke + 4 authority invariant pins + Protocol conformance. Combined: 236/236 tests green across PR 1+2 executors + chat_repl_dispatcher + conversation_orchestrator + intent_classifier. Hot-revert: single env knob → factory falls through to PR 1. PR 3 (ClaudeChatActionExecutor) pending. | Claude Opus 4.7 (P2 Slice 4 follow-up PR 2) |
| 2026-04-26 | 2.17 | **P3 P2 Slice 4 deferred follow-up PR 1 — `BacklogChatActionExecutor` landed.** First of three concrete chat executors per the operator's 3-PR mini-arc. Wires `dispatch_backlog` against `.jarvis/backlog.json` via the existing `_append_to_backlog_json` helper (single-source the write contract with `/backlog auto-proposed`). Entry shape: `task_id="chat:{turn_id}"` for BacklogSensor dedup + provenance markers (`source="chat_repl"`, `session_id`, `turn_id`, `submitted_timestamp_unix`). **Per-method composition pattern**: other 3 Protocol methods (spawn_subagent / query_claude / attach_context) delegate to a fallback executor (defaults to `LoggingChatActionExecutor`) so PRs 2 + 3 can swap each fallback slot without touching the dispatcher. Default-off behind `JARVIS_CHAT_EXECUTOR_BACKLOG_ENABLED` (legacy fallback when off — zero behavior change). New factory `build_chat_repl_dispatcher_with_backlog()` honors both the per-executor flag AND the existing `JARVIS_CONVERSATIONAL_MODE_ENABLED` master. Bounded message length (`MAX_BACKLOG_DESCRIPTION_CHARS=1024`); empty message → error token + no file write. 27 regression pins covering module constants + master flag truthy/falsy variants + write-real-entry + append-to-existing + empty/whitespace-no-write + truncation + timestamp + audit-on-success/error + 4 fallback-delegation pins + 5 factory wiring pins + end-to-end smoke + 3 authority invariant pins (no banned imports / no subprocess+network tokens / write-only-via-helper) + Protocol conformance. Combined: 239/239 tests green across new executor + chat_repl_dispatcher + conversation_orchestrator + intent_classifier + backlog_auto_proposed_repl. PR 2 (SubagentChatActionExecutor) + PR 3 (ClaudeChatActionExecutor) pending. | Claude Opus 4.7 (P2 Slice 4 follow-up PR 1) |
| 2026-04-26 | 2.16 | **P4 Slice 5 deferred follow-up — harness MetricsSessionObserver wiring landed.** Wires `MetricsSessionObserver.record_session_end` into `battle_test/harness.py` `_generate_report` between the recorder's `save_summary` call and the SessionReplayBuilder block. Reads `self._session_recorder._operations` for ops list, `self._cost_tracker.total_spent` for total cost, `branch_stats.get("commits", 0)` for commits; uses singleton `get_default_observer()` to share warned-once dedup state. Best-effort try/except (ImportError + bare Exception swallowed) — observer crash NEVER breaks `_generate_report`. Telemetry log surfaces ledger_appended + summary_merged + sse_published flags + notes. Every session-end now produces a metrics snapshot, appends to JSONL ledger, merges summary.json, and publishes SSE `metrics_updated`. 17 wiring pins covering observer import + 5 expected kwargs (session_id / session_dir / ops / total_cost_usd / commits) + recorder._operations getattr + branch_stats.commits + cost_tracker.total_spent + ordering after save_summary / before SessionReplayBuilder + try/except shape + structured telemetry + singleton-not-fresh-construction + 4 observer-contract integration smokes + master flag default-true preservation + SessionRecorder._operations field-shape pin. Combined: 221/221 tests green across wiring + harness + metrics Slices 1-3. Hot-revert: same single env knob (`JARVIS_METRICS_SUITE_ENABLED=false`) → observer short-circuits → wiring no-ops → summary.json unchanged. Closes the deferred follow-up from PRD v2.12. One deferred follow-up remains: concrete ChatActionExecutors (P3 P2 Slice 4). | Claude Opus 4.7 (P4 follow-up wiring PR) |
| 2026-04-26 | 2.15 | **P5 Slice 5 deferred follow-up — orchestrator GENERATE wiring landed.** AdversarialReviewer is now auto-invoked by the FSM during every non-SAFE_AUTO op. Wires `review_plan_for_generate_injection` into `phase_runners/plan_runner.py` at the post-PLAN/pre-GENERATE site (after `ctx.advance(OperationPhase.GENERATE)`, between Tier 5 Cross-Domain Intelligence and Tier 6 Personality voice). Reads `ctx.implementation_plan` as `plan_text`, normalizes `ctx.risk_tier.name`, passes `target_files`; injection lands via `ctx.with_strategic_memory_context()` (invariant-safe setter, NOT `dataclasses.replace`) so PLAN authority is preserved by construction — hook returns text only, never gates / advances / raises. Best-effort try/except (ImportError + bare Exception both swallowed). 16 wiring pins covering hook import + 4 expected kwargs + `implementation_plan` read + `.name` risk-tier conversion + `with_strategic_memory_context` use + ordering after GENERATE-advance + try/except shape + no-advance-no-PhaseResult-no-raise authority pin + telemetry log + section ordering after Tier 5 / before Tier 6 + master flag default-true preservation + 4 hook-contract integration smokes. Combined: 581/581 tests green across wiring + adversarial Slices 1-4 + full Pass B suite. Hot-revert: same single env knob (`JARVIS_ADVERSARIAL_REVIEWER_ENABLED=false`) → hook returns empty injection → wiring no-ops. Closes the deferred follow-up from PRD v2.13. Two deferred follow-ups remain: MetricsSessionObserver harness session-end wiring (P4 Slice 5) + concrete ChatActionExecutors (P3 P2 Slice 4). | Claude Opus 4.7 (P5 follow-up wiring PR) |
| 2026-04-26 | 2.14 | **Reverse Russian Doll Pass B STRUCTURALLY COMPLETE — Order-2 governance cage shipped end-to-end.** All 6 slices landed in 9 PRs (Slice 6 split into 6.1/6.2/6.3 mid-arc): #22298 (manifest + 9 Body-only entries) → #22320 (`ORDER_2_GOVERNANCE` risk class + classifier + `apply_order2_floor`) → #22329 (gate_runner.py wiring) → #22347 (570 LOC AST validator + 6 rules + 56 tests) → #22375 (544 LOC shadow-replay primitive + 61 tests) → #22396 (411 LOC MetaPhaseRunner; deferred candidate exec to Slice 6.1) → #22475 (sandboxed replay executor — RESOLVES the deferred exec; 47 tests; 35-name `__builtins__` allowlist + `asyncio.wait_for` timeout + 5 preconditions including literal `operator_authorized=True`) → #22517 (review queue + **locked-true** `amendment_requires_operator()` cage invariant pinned by AST-walk: function body must end with `return True` constant; `JARVIS_ORDER2_MANIFEST_AMENDMENT_REQUIRES_OPERATOR=false` still returns True; 59 tests) → #22535 (`/order2 {pending,show,amend,reject,history,help}` REPL — THE only caller in O+V that passes `operator_authorized=True` to the replay executor, source-grep-pinned; 51 tests). Combined regression spine: **438 deterministic tests green** across all 6 slices. Defaults all still `false` pending per-slice 3-clean-session graduation cadence (W1 + W2(5) soak discipline). Cage's whole point preserved: arbitrary candidate Python is NOT compiled or evaluated without operator authorization (5 preconditions + AST-pinned authority invariants on every module + locked-true cage invariant + `/order2 amend`-only authorization path). Pass C (`memory/project_reverse_russian_doll_pass_c.md`) is now structurally unblocked; draft remains held pending operator authorization. | Claude Opus 4.7 (Pass B Slice 6.3 closure PR) |
| 2026-04-26 | 2.13 | **Phase 5 P5 GRADUATED — AdversarialReviewer subagent live by default. Phase 5 ENTIRELY CLOSED.** 5-slice arc landed (Slice 1 primitive: 4-class system with hallucination filter → Slice 2 service: 6 skip paths + cost budget at $0.05/op + Provider Protocol + JSONL ledger → Slice 3 hook: GENERATE-injection helper + ConversationBridge feed; PLAN-still-authoritative invariant structurally preserved → Slice 4 observability: `/adversarial` REPL + 4 IDE GETs + SSE event → Slice 5 graduation). `JARVIS_ADVERSARIAL_REVIEWER_ENABLED` default flipped `false`→`true` in the single owner module (`adversarial_reviewer.py`). `register_adversarial_routes` wired into `EventChannelServer.start` (loopback-asserted, gated on master flag, dedicated `IDEObservabilityRouter` helper instance for shared rate-limit + CORS — mirrors P4 Slice 5 wiring pattern). Pre-graduation pin renamed in the owner test suite per its embedded discipline. Layered evidence: 185 deterministic Slice 1-4 tests + 33 graduation pins (master flag default-true + source-grep `"1"` literal + pre-graduation pin rename + EventChannelServer source-grep × 3 (`register_adversarial_routes` import + `_adversarial_enabled()` gate + `_assert_loopback_adversarial`) + cross-slice authority survival × 4 modules + post-graduation re-pins of pure-data primitive / ledger-only service / IO-free hook / read-only observability + `EVENT_TYPE_ADVERSARIAL_FINDINGS_EMITTED` allow-list pin + reachability supplement) + 15 in-process live-fire smoke checks (service skip-paths under default-on, audit row written, hook produces injection, all 5 REPL subcommands render, all 4 GET endpoints reach 200, master-off revert proven for service + REPL + endpoints). Authority invariants survived through all 5 slices: pure-data primitive (S1, no LLM call) + only-audit-ledger I/O (S2) + IO-free wiring (S3) + read-only-observability + write-mode-string-absence-pin (S4) + EventChannel-block-only addition (S5). Reviewer is structurally advisory: produces text only, no return path that gates anything. PLAN-still-authoritative invariant preserved by construction — orchestrator (when wired) is free to ignore the injection text entirely. Cost-budgeted at $0.05/op default per PRD spec; budget enforced as post-check. Hot-revert: single env knob (`JARVIS_ADVERSARIAL_REVIEWER_ENABLED=false`) → service short-circuits with `skip_reason="master_off"`, REPL renders DISABLED, GET endpoints 403, SSE drops silently, hook returns empty injection. **Orchestrator GENERATE wiring deferred to follow-up** — calling the Slice 3 hook from the post-PLAN/pre-GENERATE site in `orchestrator.py` mirrors P4 Slice 5's deferral of `MetricsSessionObserver` → harness session-end wiring. Until that follow-up lands, the AdversarialReviewer is callable + audit-trailed + observable but not yet automatically invoked by the FSM. **Phase 5 — Adversarial Depth FULLY GRADUATED 2026-04-26.** P5 closed. Next per Forward-Looking Priority Roadmap: **Reverse Russian Doll Pass B** (Order-2 governance, blocked on W2(5) Slice 5b graduation) → **Pass C** (Adaptive Anti-Venom, blocked on Pass B Slice 1) → **Phase 6 P6** (Self-narrative, long-horizon). | Claude Opus 4.7 (P5 Slice 5 graduation PR) |
| 2026-04-26 | 2.12 | **Phase 4 P4 GRADUATED — Convergence Metrics Suite live by default. Phase 4 ENTIRELY CLOSED.** 5-slice arc landed (Slice 1 `MetricsEngine` 7-metric un-stranding wrapper → Slice 2 `MetricsHistoryLedger` JSONL persistence + 7d/30d aggregator → Slice 3 `/metrics` REPL with ASCII sparkline → Slice 4 `MetricsSessionObserver` + 4 IDE GET endpoints + SSE `metrics_updated` event → Slice 5 graduation). `JARVIS_METRICS_SUITE_ENABLED` default flipped `false`→`true` in **three owner modules** (`metrics_engine.py` + `metrics_repl_dispatcher.py` + `metrics_observability.py`). `register_metrics_routes` wired into `EventChannelServer.start` (loopback-asserted, gated on master flag, per-instance rate-limit + shared CORS allowlist via dedicated `IDEObservabilityRouter` helper). Pre-graduation pins renamed in all three owner suites per their embedded discipline. Layered evidence: 204 deterministic Slice 1-4 tests + 38 graduation pins (master flag default-true × 3 owner modules + source-grep `"1"` literal × 3 + pre-graduation pin renames × 3 owner suites + EventChannelServer source-grep × 3 (`register_metrics_routes` import + `_metrics_enabled()` gate + `_assert_loopback_metrics`) + cross-slice authority survival × 4 modules + reachability supplement) + 15 in-process live-fire smoke checks (observer end-to-end with master-on default, all 4 GET endpoints reachable + return correct shape, all 3 REPL commands render, master-off revert proven). Authority invariants survived through all 5 slices: pure-data engine (S1) + ledger-only I/O (S2) + delegating-only REPL (S3) + summary.json + delegated-ledger I/O (S4) + EventChannel-block-only addition (S5). The `INSUFFICIENT_DATA` problem statement that motivated this phase is resolved — operators can now answer "is O+V getting smarter?" with concrete data via `/metrics 7d` REPL or `GET /observability/metrics/window?days=7`. Hot-revert: single env knob (`JARVIS_METRICS_SUITE_ENABLED=false`) → observer short-circuits, GET endpoints return 403, SSE drops silently; ledger remains readable for prior-session recall. **Phase 4 — Cognitive Metrics FULLY GRADUATED 2026-04-26.** Both items closed (P3 + P4). Phases 0-4 all complete. Next per Forward-Looking Priority Roadmap: Phase 5 P5 (AdversarialReviewer subagent). | Claude Opus 4.7 (P4 Slice 5 graduation PR) |
| 2026-04-26 | 2.11 | **Phase 3 P2 GRADUATED — Conversational mode live by default. Phase 3 ENTIRELY CLOSED.** 4-slice arc landed (Slice 1 IntentClassifier primitive → Slice 2 ConversationOrchestrator + ChatSession → Slice 3 /chat REPL dispatcher + ChatActionExecutor Protocol → Slice 4 graduation). `JARVIS_CONVERSATIONAL_MODE_ENABLED` default flipped `false`→`true`. `build_chat_repl_dispatcher()` factory in `chat_repl_dispatcher.py` is the single SerpentFlow integration point: returns a wired dispatcher (with safe-default `LoggingChatActionExecutor`) when on, `None` when reverted so SerpentFlow can skip surfacing `/chat` entirely. Pre-graduation pins renamed in BOTH env-knob owner suites (intent_classifier + chat_repl_dispatcher) per their embedded discipline. Layered evidence: 171 deterministic Slice 1-3 tests + 45 graduation pins (master flag default-true × 2 owner modules + source-grep `"1"` literal × 2 + factory branch coverage + LoggingExecutor contract pin + cross-slice authority survival × 4 modules + reachability supplement) + 15 in-process live-fire smoke checks (factory→classifier→orchestrator→dispatcher→executor end-to-end across all 4 ChatActionExecutor branches; bounded-ring under load; hot-revert proven). Authority invariants survived through all 4 slices: pure-data classifier (Slice 1) + IO-free orchestrator (Slice 2) + IO-free dispatcher (Slice 3) + LoggingExecutor never raises (Slice 4). Safety-first contract pinned: noop input never invokes executor; CONTEXT_PASTE without prior turn falls back to query_claude (degraded — never attaches to non-existent target). Concrete executors against backlog ingestion / subagent_scheduler / Claude provider tracked as follow-up slices — wiring those crosses authority boundaries that need their own pin suites. Hot-revert: single env knob (`JARVIS_CONVERSATIONAL_MODE_ENABLED=false`) → factory returns None → `/chat` invisible to operators; orchestrator + bridge state remain inspectable for prior-decision recall. **Phase 3 — Operator Symbiosis FULLY GRADUATED 2026-04-26.** All three items closed (P3.5 + P3 + P2). | Claude Opus 4.7 (P2 Slice 4 graduation PR) |
| 2026-04-26 | 2.10 | **Phase 3 P3 GRADUATED — inline approval UX live by default.** 4-slice arc landed (Slice 1 primitive → Slice 2 provider + audit ledger → Slice 3 renderer + 30s prompt + `$EDITOR` → Slice 4 graduation). `JARVIS_APPROVAL_UX_INLINE_ENABLED` default flipped `false`→`true`. `build_approval_provider()` factory in `inline_approval_provider.py` is the single source of truth for `GovernedLoopService`'s approval-provider selection (returns `InlineApprovalProvider` when on, legacy `CLIApprovalProvider` when off). Pre-graduation pin renamed to `test_master_flag_default_true_post_graduation` per its embedded discipline. Layered evidence: 165 deterministic Slice 1-3 tests + 36 graduation pins (master flag + source-grep `"1"` literal + factory branch coverage + GovernedLoopService source-grep + cross-slice authority survival + reachability supplement) + 15 in-process live-fire smoke checks (factory-built provider end-to-end through queue + renderer + audit ledger). Authority invariants survived through all 4 slices: pure-data primitive (Slice 1) + only-audit-ledger I/O (Slice 2) + argv-only subprocess (Slice 3, no `shell=True`). Safety-first contract pinned: EOF / garbage / 30s timeout all `defer-not-approve`. Hot-revert: single env knob (`JARVIS_APPROVAL_UX_INLINE_ENABLED=false`) — factory returns `CLIApprovalProvider` on the next construction. Phase 3 P3 + P3.5 both COMPLETE; Phase 3 P2 (Conversational mode) remains the only open Phase 3 item. | Claude Opus 4.7 (P3 Slice 4 graduation PR) |
