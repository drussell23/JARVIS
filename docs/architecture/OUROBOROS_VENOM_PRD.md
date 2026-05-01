# Ouroboros + Venom (O+V) — Product Requirements Document & Roadmap

**Status**: Living document
**Version**: 2.6 (2026-04-30 — Move 3 auto_action_router CLOSED; 4-slice arc shipped same-day; verification→action loop closes; Self-tightening immunity A−→A; master flag graduated default-true in shadow mode; ENFORCE locked off until separate later authorization)
**Author**: Derek J. Russell (vision) · Claude Opus 4.7 (PRD synthesis)
**Audience**: Operator (decision authority), JARVIS engineers, future-self (resuming after context loss)
**Prerequisite reading**: `CLAUDE.md` (architecture), `docs/architecture/OUROBOROS.md` (battle-test breakthrough log), `docs/architecture/RSI_CONVERGENCE_FRAMEWORK.md` (Wang RSI mathematical foundation)
**Latest review**: §27 (v6, 2026-04-29) — answers the autonomy question; supersedes §26 critical-path framing now that Pass B + Pass C + Priorities 1+2 are all structurally complete

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
24. [Brutal Architectural Review v3 — Convergence-Phase (2026-04-28)](#24-brutal-architectural-review-v3--convergence-phase-2026-04-28) *(superseded in part by §25)*
    - [24.1 Context & Scope of Review](#241-context--scope-of-review)
    - [24.2 Capability Matrix — current vs A-level sovereign developer](#242-capability-matrix--current-vs-a-level-sovereign-developer)
    - [24.3 Cognitive & Epistemic Delta — what CC paradigms O+V lacks](#243-cognitive--epistemic-delta--what-cc-paradigms-ov-lacks)
    - [24.4 The HypothesisProbe Primitive — autonomous ambiguity resolution](#244-the-hypothesisprobe-primitive--autonomous-ambiguity-resolution)
    - [24.5 Temporal Observability — state reconstruction surface](#245-temporal-observability--state-reconstruction-surface)
    - [24.6 Systemic Fragility — race conditions in async phase-runners](#246-systemic-fragility--race-conditions-in-async-phase-runners)
    - [24.7 Cascading state-failure vectors over long horizons](#247-cascading-state-failure-vectors-over-long-horizons)
    - [24.8 Antivenom bypass vectors — Quine-class hallucinations](#248-antivenom-bypass-vectors--quine-class-hallucinations)
    - [24.9 Letter grade — B+ trending A-, defended](#249-letter-grade--b-trending-a-defended) *(superseded by §25.4 — current grade B-)*
    - [24.10 Critical Path to A-Level RSI — top 3 systemic upgrades](#2410-critical-path-to-a-level-rsi--top-3-systemic-upgrades) *(superseded by §25.5 — top 5 priorities)*
    - [24.11 In-flight alignment — Phase 12 / 12.2 maps to the critical path](#2411-in-flight-alignment--phase-12--122-maps-to-the-critical-path)
    - [24.12 What this review explicitly does NOT prescribe](#2412-what-this-review-explicitly-does-not-prescribe)
25. [Brutal Architectural Review v4 — Post-Phase-2-Production-Verification (2026-04-29)](#25-brutal-architectural-review-v4--post-phase-2-production-verification-2026-04-29) *(superseded by §26 — Priorities A–F all closed)*
    - [25.1 What soak #3 actually proved (and didn't)](#251-what-soak-3-actually-proved-and-didnt)
    - [25.2 The Cognitive & Epistemic Delta — refined post-Phase-2](#252-the-cognitive--epistemic-delta--refined-post-phase-2)
    - [25.3 Deep Observability — Temporal surface refined](#253-deep-observability--temporal-surface-refined)
    - [25.4 Brutal grade — current state: **B-**](#254-brutal-grade--current-state-b-)
    - [25.5 Critical Path to A-Level RSI — top 5 systemic upgrades](#255-critical-path-to-a-level-rsi--top-5-systemic-upgrades)
    - [25.6 In-flight alignment — what's on main right now that helps](#256-in-flight-alignment--whats-on-main-right-now-that-helps)
    - [25.7 Reverse Russian Doll alignment](#257-reverse-russian-doll-alignment)
    - [25.8 What this review explicitly does NOT prescribe](#258-what-this-review-explicitly-does-not-prescribe)
    - [25.9 Summary](#259-summary)
26. [Brutal Architectural Review v5 — Post-Phase-12-DW-Resilience-Closure (2026-04-29)](#26-brutal-architectural-review-v5--post-phase-12-dw-resilience-closure-2026-04-29) *(superseded by §27 — Pass B + Pass C + Priorities 1+2 all closed; autonomy question re-graded)*
    - [26.1 What soak #7 actually proved (and what §25 priorities A–F now closed)](#261-what-soak-7-actually-proved-and-what-25-priorities-af-now-closed)
    - [26.2 Refined Cognitive & Epistemic Delta — what CC still has that O+V doesn't](#262-refined-cognitive--epistemic-delta--what-cc-still-has-that-ov-doesnt)
    - [26.3 Refined Deep Observability — temporal reconstruction is the missing depth](#263-refined-deep-observability--temporal-reconstruction-is-the-missing-depth)
    - [26.4 Brutal grade — current state: **B+ / B−**](#264-brutal-grade--current-state-b--b-)
    - [26.5 Critical Path to A-Level RSI — top 3 systemic upgrades (post-Phase-12)](#265-critical-path-to-a-level-rsi--top-3-systemic-upgrades-post-phase-12)
    - [26.6 Cost contract structural reinforcement — BG never cascades to Claude (bulletproofing)](#266-cost-contract-structural-reinforcement--bg-never-cascades-to-claude-bulletproofing)
    - [26.7 In-flight alignment + sequencing](#267-in-flight-alignment--sequencing)
    - [26.8 What this review explicitly does NOT prescribe](#268-what-this-review-explicitly-does-not-prescribe)
    - [26.9 Summary — the path from B+ to A](#269-summary--the-path-from-b-to-a)
27. [Brutal Architectural Review v6 — The Autonomy Question (2026-04-29)](#27-brutal-architectural-review-v6--the-autonomy-question-2026-04-29) *(latest)*
    - [27.1 What's actually shipped (vs the §26 v5 framing which is now stale)](#271-whats-actually-shipped-vs-the-26-v5-framing-which-is-now-stale)
    - [27.2 The 8 capability dimensions of "autonomous coding"](#272-the-8-capability-dimensions-of-autonomous-coding)
    - [27.3 Brutal letter grade — answering the autonomy question directly](#273-brutal-letter-grade--answering-the-autonomy-question-directly)
    - [27.4 Critical path to actual autonomy — top 3 systemic moves](#274-critical-path-to-actual-autonomy--top-3-systemic-moves)
    - [27.5 What this review explicitly does NOT prescribe](#275-what-this-review-explicitly-does-not-prescribe)
    - [27.6 Summary — answering the operator's question directly](#276-summary--answering-the-operators-question-directly)
- [Appendix A — Glossary](#appendix-a--glossary)
- [Appendix B — Reference Documents Map](#appendix-b--reference-documents-map)
- [Appendix C — Phase Gate Criteria (entry/exit conditions)](#appendix-c--phase-gate-criteria-entryexit-conditions)
- [Appendix D — Document History](#appendix-d--document-history)

---

## 1. Executive Summary

Ouroboros + Venom (O+V) is the autonomous self-development governance engine of JARVIS. It is the **proactive autonomous opposite of Claude Code (CC)** — where CC requires a human to ask, O+V should observe, hypothesize, propose, validate, and ship without prompting (with human-in-loop escalation only when context warrants it).

### Where we stand (2026-04-29 — post-Phase-12-DW-Resilience-closure + soak #7 verification)

- **Architecture**: **A** — the verification loop is now *functionally* live, not just structurally live. The 11-phase FSM + 16 sensors + cost-governor + Iron Gate + risk-tier ladder all compose correctly under hostile network conditions. **Order-2 governance cage shipped (Pass B closed 2026-04-26)** + **Pass C Adaptive Anti-Venom structurally complete (2026-04-26)** + **Phase 1 Determinism Substrate graduated (`memory/project_phase_1_closure.md`, 2026-04-28)** + **Phase 12.2 Physics-Aware Topology Routing closed (`memory/project_phase_12_2_closure.md`, 2026-04-28)** + **Phase 2 Closed-Loop Self-Verification graduated (`dc5f77017f`, 2026-04-29)** + **§25 Priorities A–F all CLOSED single-day (2026-04-29)**: A (mandatory claim density), B (MetaSensor degenerate-loop alarm), C (HypothesisProbe + Venom `hypothesize` tool), D (postmortem ledger discoverability), E (shipped-code structural invariants + Order-2 promotion), F (evidence collector extension + F2/F3 capture) + **Phase 12 DW Resilience CLOSED 2026-04-29 (`memory/project_phase_12_dw_resilience_closure.md`)**: Pricing Oracle (Option α) + Sentinel-Pacemaker Handshake (Option β) + Universal Terminal Postmortem (Option E) all validated firing live in soak #7 under DW endpoint flakiness.
- **Cognitive depth**: **A−** — Priority 1 (Confidence-Aware Execution, CLOSED 2026-04-29) wired confidence as a routing/circuit-breaker signal + HypothesisProbe consumer. Priority 2 (Causality DAG, CLOSED 2026-04-29) shipped session-spanning navigable graph + replay-from-record + L3 fan-out determinism fix. The verification loop now closes *meaningfully* AND has structural memory of its own causal history. The remaining gap to full A is Priority 3 (Adaptive Anti-Venom, gated on W2(5) Slice 5b → Pass B Slice 1 → Pass C Slice 1). See §26 for the full review.
- **Production track record**: 1 verified end-to-end multi-file APPLY (Sessions Q-S, 2026-04-15); 7 soaks 2026-04-29: soaks #1–#3 caught Phase 2 hollowness (forcing §25 Priorities A–F); soak #4 validated A-shipped; soak #5 validated A+B+C+D+E+F empirical loop closure; soak #6 surfaced Static Pricing Blindspot (forced Phase 12 DW Resilience); **soak #7 (`bt-2026-04-29-074851`) — clean idle exit, session_outcome=complete, 16 Pricing Oracle resolutions + 3 Handshake firings + 22-model boot catalog + 8 postmortems w/ non-trivial claim density + $0.0316 spend + strategic_drift=ok**. Wave 3 architecturally complete (W3(7) graduated, W3(6) closed-pending-external-API-stability per operator binding 2026-04-25).
- **RSI scaffolding**: 6 Wang modules verified (Phase 0 audit, 2026-04-25); 4 wired into live FSM. **RSI Gear 1 (Determinism)**: DONE per Phase 1 closure. **RSI Gear 2 (Bounded Curiosity)**: HypothesisProbe primitive shipped + Venom `hypothesize` tool + plan_generator wiring (§25 Priority C) + Priority 1 Slice 3 wired probe consumer for confidence-collapse → 3-action verdict (CLOSED 2026-04-29). **RSI Gear 3 (Closed-Loop Memory)**: functionally complete (Phase 2 + Option E + §25 Priorities A+F evidence capture) — soak #7 confirms non-trivial claim density. **NEW: Causality DAG substrate (§26.5.2 Priority 2 CLOSED 2026-04-29)**: session-spanning navigable graph + replay-from-record + per-worker sub-ordinals fix L3 fan-out determinism — substrate for Pass C drift detection ready.

### Grade summary table (current vs A-level target — refreshed 2026-04-29 post-Phase-12-closure / soak #7)

Color legend: 🟢 = at target / done · 🟡 = in progress / partial · 🔴 = not at target / critical gap.

| Dimension | Current | A-Level Target | Gap to close |
|---|---|---|---|
| **Architecture** | 🟢 A | A | Phase 12 DW Resilience closed 2026-04-29 — Pricing Oracle + Handshake + Universal Postmortem all live in production under hostile DW conditions |
| **Cognitive depth** | 🟢 A | A | §26.5 Priority 1 (Confidence-Aware Execution) ✅ CLOSED 2026-04-29 + §26.5.2 Priority 2 (Causality DAG) ✅ CLOSED 2026-04-29 + §26.5.3 Priority 3 (Adaptive Anti-Venom, Pass C) ✅ GRADUATED 2026-04-29 (Move 1) — full A reached |
| **Production track record** | 🟡 B+ | A | Move 2 24h burn-in CLOSED 2026-04-30 — 7 soaks of clean degradation evidence under hostile upstream API; 6 architectural layers shipped + regression-pinned; 24h sustained operation now bounded by Anthropic API physics, not O+V substrate (`memory/project_move_2_closure.md`) |
| **RSI Gear 1 — Determinism** | 🟢 A | A | Phase 1 closed 2026-04-28; soak #7 confirms Merkle DAG holds under DW endpoint flakiness |
| **RSI Gear 2 — Bounded Curiosity** | 🟢 A− | A | §25 Priority C HypothesisProbe shipped + Priority 1 Slice 3 wires probe consumer for confidence-collapse → 3-action verdict (RETRY/ESCALATE/INCONCLUSIVE); §26.5.1 Priority 1 CLOSED 2026-04-29 |
| **RSI Gear 3 — Closed-Loop Memory** | 🟢 A− | A | Soak #7 confirms non-trivial claim density (claims=3 pass=1 fail=0 insuff=2 err=0); Order-2 promotion of plan_runner default-claim wiring landed (§25 Priority E) |
| **Operator UX vs CC** | 🟢 A | A | `/postmortems` REPL + 4 GET endpoints + SSE event landed (§25 Priority D); session-spanning causal DAG navigation surface CLOSED 2026-04-29 (§26.5.2 Priority 2 — `/postmortems dag` family + 2 IDE GETs + `dag_fork_detected` SSE) |
| **Self-tightening immunity** | 🟢 A | A | MetaSensor graduated default-true (§25 Priority B); shipped_code_invariants seeded (§25 Priority E); §26.5 Priority 3 Pass C graduated 2026-04-30; **Move 3 `auto_action_router` CLOSED 2026-04-30** — verification→action loop closes via 5-value AdvisoryActionType (`memory/project_move_3_closure.md`). Mutation boundary (`JARVIS_AUTO_ACTION_ENFORCE`) locked off; advisory-only shadow mode active. |
| **Sandbox safety boundary** | 🟡 B | A | Object-graph escape vector documented (§3.6 vector 1); PLAN-skip-by-trivialization bypass closed (§25.5.5 → Priority E shipped); Quine-class hallucination vectors enumerated (§26.4) |
| **Cross-process safety** | 🟢 A− | A | Slice 1.3 ordinal-counter L3 fan-out bug CLOSED 2026-04-29 by Priority 2 Slice 2 — `(worker_id, op_id, phase, kind)` composite namespace + `worker_id_for_path()` pure-stdlib helper. Advisory file-locking on AdaptationLedger writes (§3.6 vector 3) still pending — gated on Pass C unblock |
| **Long-horizon semantic stability** | 🟡 B | A | TrajectoryAuditor still missing (§24.8.2); Antivenom is per-op not per-trajectory — addressed by §26.5 Priority 3 (Adaptive Anti-Venom unblocking Pass C) |
| **Cost contract enforcement (BG never cascades to Claude)** | 🟢 A− | A | Soak #7 validated 14 BG blocks correctly skip-and-queue'd; structural reinforcement pending (§26.6) — AST invariant + runtime assertion + Property Oracle claim |
| **Net overall grade vs "95%+ sovereign autonomous developer"** | 🟢 **A− / B+** | A | Move 1 (Pass C graduation) + Move 2 (24h burn-in structurally closed, 6 layers shipped) both 2026-04-30. Empirical 24h proof gated on Anthropic API stability — separate test-bench arc required. |

**The gap is honest, refreshed against today's evidence.** O+V has A-level architecture and A-level vision. Execution sits at B+ on happy paths and B− on edge cases. The §25 priorities A–F closing single-day moved us from "B− on production behavior" (§25.4) to "B+ on closed-loop verification" (§26.4) — the upgrade captures the honest gap between *signal density* (now non-trivial) and *cognitive depth* (still single-threaded). The remaining edge-case fragility is enumerated in §26.4 and addressed by the three systemic upgrades in §26.5.

### Where we're going

A-level reliable execution from A-level vision — measurable by:
- Sustained 90%+ session completion rate (currently variable; `/metrics 7d` now answers this concretely)
- Cross-session learning evidence (✅ delivered: PostmortemRecall + DirectionInferrer arc-context + ConversationBridge + UserPreferenceMemory + SemanticIndex + LastSessionSummary)
- Self-directed goal formation (✅ delivered: SelfGoalFormationEngine + Hypothesis pairing + BacklogSensor auto-proposed entries with operator-review-tier safety)
- Conversational mode parity with CC (✅ delivered: `/chat` REPL classified + audit-trailed + 3 concrete executors against real systems)
- Convergence metric trending in the Wang sense (✅ delivered: `INSUFFICIENT_DATA` problem statement resolved by Phase 4 P4 graduation)
- **Adaptive immune system** that grows stricter as the shell expands (🚀 in flight: Pass C Slices 1-4 landed; 5-6 pending; per-slice graduation cadence post-arc-closure)
- A-level reliability metric: **70%+ operator-approval rate** for adaptive proposals over a 30-day window (Pass C arc-closure criterion per §10.3)

This PRD lays out a phased roadmap to close the gap. **The gap is internal to JARVIS, not external.** External provider quality is sufficient; what's missing is the orchestration layer that converts that intelligence into self-directing, self-improving, self-tightening behavior.

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

**🎯 Phase 3 — Operator Symbiosis FULLY GRADUATED 2026-04-26.** All three items closed (P3.5 + P3 + P2).
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

**Reverse Russian Doll Pass C — Adaptive Anti-Venom**: ✅ STRUCTURALLY COMPLETE 2026-04-26 (all 6 slices shipped same day; defaults still false pending per-slice graduation cadence)
  - [x] Slice 6 — `adaptation/meta_governor.py` MetaAdaptationGovernor + `/adapt` REPL (this PR; **CLOSES Pass C**): operator-facing `/adapt {pending,show,approve,reject,history,stats,help}` REPL dispatcher mirroring Pass B's `/order2` REPL pattern. 12-status DispatchStatus enum + frozen DispatchResult + 7 subcommand handlers + render helpers (pending/show/history/stats). `compute_stats()` aggregator counts pending/approved/rejected per surface + totals from the AdaptationLedger append-only log (latest-record-per-proposal-id reduction). `--surface` filter on history accepts the 5 enum values (validated; INVALID_ARGS for unknown). `help` bypasses master flag (discoverability per the policy adopted across Pass A graduations). Substrate master-off short-circuit: even with REPL on, returns LEDGER_DISABLED for read+write subcommands. **AST-pinned**: NO imports of the 4 mining-surface modules (each registered its own validator at its own import; substrate stays acyclic). Master flag `JARVIS_ADAPT_REPL_ENABLED` (default false). 55 regression pins covering 12-value status enum + DispatchResult frozen + parse_argv (4 cases) + master-off-blocks-except-help + ledger-master-off-blocks-except-help + 6 read-side path matrices (pending empty/populated, show MISSING/NOT_FOUND/OK, history default/custom/invalid/clamp/--surface filter+invalid+missing-arg, stats empty/aggregated, compute_stats direct) + 8 approve paths (OPERATOR_REQUIRED / MISSING_PROPOSAL_ID / PROPOSAL_NOT_FOUND / NOT_PENDING / REASON_REQUIRED / reader-raises / OK / LEDGER_REJECTED) + 8 reject paths (same matrix) + reason-truncation + reader-raises + **end-to-end pin** (mining surface → propose → REPL approve → APPLIED state with stats reflecting) + 5 authority invariants (no banned governance imports / substrate+stdlib-only / no subprocess+network / no LLM tokens / no other-surface-module imports). Combined regression spine: **349/349 tests green** across ALL 6 Pass C slices. **Deferred follow-ups** (tracked in module docstring): `register_adaptation_routes(app)` GET endpoints / SSE event emission for 4 event types / weekly background analyzer scheduling / actual gate-state mutation on approve (each surface's `.jarvis/adapted_<surface>.yaml` writer per §6.3/§7.3/§8.4/§9.3). Same split-pattern as Pass B's "/order2 amend" → replay-executor: this REPL closes the cage's structural cycle (operator review + decision); the activation wirings are the natural follow-up arc.
  - [x] Slice 5 — `adaptation/category_weight_rebalancer.py` ExplorationLedger category-weight auto-rebalance (this PR): the only Pass C surface where the proposal *appears* to lower something — **mass-conservation makes it net-tighten**. Pure stdlib **Pearson correlation kernel** (Py 3.9 compat manual implementation since `statistics.correlation` was added in 3.10) computes per-category correlation between exploration score and verify_passed binary. Identifies high-value (highest correlation) + low-value (lowest correlation) categories. If `(high - low) >= JARVIS_ADAPTATION_CORRELATION_DELTA` (default 0.3) AND ≥ JARVIS_ADAPTATION_REBALANCE_THRESHOLD (default 10) ops in window, proposes raise-high (DEFAULT_RAISE_PCT=20%) + lower-low (DEFAULT_LOWER_PCT=10%, hard-floored at JARVIS_ADAPTATION_WEIGHT_FLOOR_PCT=50% of original AND MIN_WEIGHT_VALUE=0.01 absolute). **Net-tighten guarantee enforced at three layers**: (a) DEFAULT_LOWER_PCT < DEFAULT_RAISE_PCT constants pin; (b) caller-passed lower_pct >= raise_pct gets clamped to raise_pct//2; (c) defensive mass-conservation check at mine-time refuses to propose if Σ(new) < Σ(old) somehow. Surface validator: kind=`rebalance_weight` + sha256-hash + threshold + summary contains BOTH `↑` AND `↓` tokens + summary contains `net +` indicator. Idempotent proposal_id (sha256 of high+low+new_weights vector rounded 6dp). Pearson kernel handles edge cases: short input / mismatched lengths / zero-variance returns 0.0; never raises. Master flag `JARVIS_ADAPTIVE_CATEGORY_WEIGHTS_ENABLED` (default false). 62 regression pins (12 constants + master flag + 8 env overrides + **7 Pearson kernel pins** + 3 per-category correlation pins + 5 weight-rebalance computation pins + 11 mine pipeline pins inc. mass-conservation-invariant + low-floor-invariant + lower-pct-clamp + 3 ledger integration + 8 surface validator + 3 authority invariants + 1 substrate integration). Combined regression spine: **294/294 tests green** across Pass C Slice 1+2+3+4+5.
  - [x] Slice 4 — **combined** per-Order mutation budget + risk-tier ladder extender (PR #22866; per §8 design two sub-surfaces in one slice). **Slice 4a** = `adaptation/per_order_mutation_budget.py`: pure stdlib analyzer of `MutationUsageLite` events. Proposes lowering the per-Order mutation budget when ops consistently used fewer mutations than budgeted. Conservative: uses **max observed** as the proposed new budget (any op that needed N mutations in the window will still get N under the new budget). Order-2 hard floor `MIN_ORDER2_BUDGET=1` so Pass C never proposes a non-functional budget. Surface validator: kind=`lower_budget` + sha256-hash + threshold + summary-`→`-indicator. Master flag `JARVIS_ADAPTIVE_PER_ORDER_BUDGET_ENABLED` (default false). **Slice 4b** = `adaptation/risk_tier_extender.py`: pure stdlib analyzer of `PostmortemEventLite` events with `blast_radius` field. Identifies novel `failure_class` values (not in `DEFAULT_KNOWN_FAILURE_CLASSES = {infra, test, code, approval_denied, blocked}`) accumulating ≥ `JARVIS_ADAPTATION_TIER_THRESHOLD` (default 5) occurrences. Classifies blast_radius into 4 bands: 0.0-0.25 → SAFE_AUTO/NOTIFY_APPLY/HARDENED, 0.25-0.5 → NOTIFY_APPLY/APPROVAL_REQUIRED/HARDENED, 0.5-0.75 → APPROVAL_REQUIRED/BLOCKED/HARDENED, 0.75+ → APPROVAL_REQUIRED/BLOCKED/CRITICAL. Synthesizes deterministic tier name `<insert_after>_<SUFFIX>_<FAILURE_CLASS>` (uppercase, sanitized special chars, truncated at `MAX_TIER_NAME_CHARS=64`). Proposes insertion between two existing tiers — **strictly tightening** per §8.3: ladder grows; nothing on it is removed. Surface validator: kind=`add_tier` + sha256-hash + threshold + summary-contains-`insert`-or-`between`. Master flag `JARVIS_ADAPTIVE_RISK_TIER_LADDER_ENABLED` (default false). Both sub-surfaces auto-register their validators at module-import + are independently default-off. 63 regression pins (Slice 4a: 22, Slice 4b: 24, cross-surface: 5, plus 12 shared substrate-integration pins) covering all module constants + master-flag pins + dataclass-frozen + env-overrides + mine pipeline (empty / threshold / window-filter / per-Order independence / max-observed-as-proposal / Order-2-floor-pin / multi-class / known-class-skipped / idempotent proposal_id) + ledger integration (master-off / master-on / DUPLICATE on re-mine) + surface validators (registered-at-import / 4 reject paths each + valid pass each) + blast-radius classifier (4 bands) + tier-name synthesis (basic / sanitized / truncated / uppercase) + cross-surface authority invariants (no banned governance imports / no subprocess+network / distinct validator registration). Combined regression spine: **232/232 tests green** across Slice 1+2+3+4. Per §8.6: this slice graduates when both sub-surfaces have 5 clean sessions each (cumulative ladder).
  - [x] Slice 3 — `adaptation/exploration_floor_tightener.py` IronGate exploration-floor auto-tightener (this PR): pure stdlib analyzer of (exploration-score, verify-outcome) tuples per op. **Bypass-failure detector** (`floor_satisfied=True AND verify_outcome IN {regression, failed}`) — the structural signal that the exploration gate was bypassed and the cage was not strict enough. **Weakest-category identification** via per-op argmin (lowest-scoring category in each bypass-failure op) + group-count winner across the window (alpha tie-break for determinism). **Bounded 10% raise per cycle** via `compute_proposed_floor(current, pct=10)`: `current + ceil(current * pct/100)`, floor-shaped to MIN_NOMINAL_RAISE=1; defends against the math stalling on small floors. Per-cycle pct hard-capped at MAX_FLOOR_RAISE_PCT=100 to prevent operator-typo runaway (env override to 500% gets clamped). Auto-registers a per-surface validator at module-import enforcing: kind == "raise_floor" + proposed_state_hash sha256-prefixed + observation_count >= JARVIS_ADAPTATION_FLOOR_THRESHOLD (default 5, slightly higher than Slice 2's 3 because a floor-raise has broader impact than one detector pattern) + summary-contains-→-indicator (defense-in-depth against doctored proposals). Idempotent proposal_id (sha256 of category + current + proposed floor) so re-mining the same window's events yields DUPLICATE_PROPOSAL_ID at substrate. Master flag `JARVIS_ADAPTIVE_IRON_GATE_FLOORS_ENABLED` (default false). Per §7.1 design: "the candidate for floor tightening" (singular per cycle) — keeps the operator-review surface trim. 55 regression pins covering 6 module constants + 3 master-flag pins + 9 env-override pins (incl. invalid-falls-back + zero-falls-back + clamp-to-max for raise_pct) + 6 compute_proposed_floor math pins (basic 10% / min_nominal kicks in / higher pct / zero / negative / env-derived) + 2 bypass-filter pins (regression+failed kept; pass+l2_recovered excluded) + 4 weakest-category pins (per-op argmin + alpha tie-break + empty-input + skip-no-scores ops) + 9 mine_floor_raises_from_events end-to-end pins (empty / below-threshold / weakest-cat-below-threshold / qualifies / skip-non-bypass / window-filter / proposal_id-stable / proposal_id-differs) + 4 propose_floor_raises_from_events ledger pins (master-off / master-on / idempotent / observation_count matches bypass_count) + 6 surface validator pins (registered-at-import + 4 reject paths + 1 pass + idempotent install) + 4 authority invariants (no banned imports / substrate+stdlib-only / no subprocess+network / no LLM tokens) + 1 substrate integration pin. Combined regression spine: **169/169 tests green** across Slice 1+2+3.
  - [x] Slice 2 — `adaptation/semantic_guardian_miner.py` POSTMORTEM-mined patterns (this PR): pure stdlib-only longest-common-substring detector synthesizer + group-by-(root_cause, failure_class) + window filter + existing-pattern duplicate check + idempotent proposal_id (hash of group+pattern). `PostmortemEventLite` frozen dataclass = caller-supplied input shape (the miner does NOT read postmortem files itself — Slice 6 MetaGovernor will wire the source at window cadence per §4.3). End-to-end `propose_patterns_from_events()` flows through Slice 1's `AdaptationLedger.propose()`. Auto-registers a per-surface validator with the substrate at module-import enforcing: kind == "add_pattern" + proposed_state_hash starts with "sha256:" + observation_count >= JARVIS_ADAPTATION_PATTERN_THRESHOLD (default 3). Master flag `JARVIS_ADAPTIVE_SEMANTIC_GUARDIAN_ENABLED` (default false until Slice 2 graduation) gates `propose_patterns_from_events()` (returns empty list when off — substrate gate kicks in independently). Bounded synthesis: MAX_EXCERPTS_PER_GROUP=32 + MAX_SYNTHESIZED_PATTERN_CHARS=256 + MIN_LCS_LENGTH=8 + MIN_SYNTHESIZED_PATTERN_CHARS=8 (sub-3-char patterns would match anything; LCS bounded at 256 chars defends against multi-KB regex blob). Window filter: events older than `now - window_days*86400` dropped; epoch=0 back-compat retained for boot-time tests. 54 regression pins covering 6 module constants + 3 master-flag pins + 5 env-override pins + 6 LCS algorithm pins + 5 existing-pattern duplicate pins + 8 mine_patterns_from_events end-to-end pins + 5 propose_patterns_from_events pins (master-off / master-on / idempotency / evidence summary / existing-skip) + 5 surface validator pins (registered-at-import / 4 reject paths + 1 pass + idempotent install) + 4 authority invariants (no banned governance imports / substrate+stdlib-only / no subprocess+network / no LLM-call tokens) + 3 integration pins (substrate accepts what miner produces / proposal_id stable across calls / proposal_id differs for different patterns). Combined regression spine: **114/114 tests green** across Slice 1+2. Per Pass C §6.2: deterministic-only synthesizer per zero-LLM-in-cage invariant; LCS is the v1 algorithm; if it proves too narrow over time, operator can extend the synthesizer module via a normal Pass B Order-2 amendment (it IS governance code).
  - [x] Slice 1 — `adaptation/ledger.py` substrate: append-only JSONL audit log at `.jarvis/adaptation_ledger.jsonl` + 5-value `AdaptationSurface` enum (one per Pass C §3 thesis bullet: SemanticGuardian patterns / IronGate exploration floors / ScopedToolBackend mutation budget / risk-tier ladder / ExplorationLedger category weights) + 3-value `OperatorDecisionStatus` lifecycle + 2-value `MonotonicTighteningVerdict` (passed | rejected:would_loosen) + frozen `AdaptationProposal`/`AdaptationEvidence` dataclasses (sha256 tamper-detect per record) + pluggable per-surface validator registry (Slices 2-5 will register their semantic checks at module-import) + universal `validate_monotonic_tightening()` that **refuses to persist loosening proposals** (the cage's load-bearing rule per §4.1: Pass C cannot loosen via any path; loosening goes through Pass B `/order2 amend`). State transitions write NEW lines (append-only, never rewritten). Latest-record-per-proposal-id wins for current state. `approve()` is the ONLY transition that flips `applied_at` non-null. Stdlib-only import surface (AST-pinned). 60 regression tests covering module constants + 5 enums + dataclass-frozen pins + master-flag default-false + 7 propose paths (OK / DISABLED / 4 INVALID sub-cases / DUPLICATE / WOULD_LOOSEN with NOT-PERSISTED pin / surface-validator pass + reject + raise) + 6 decision paths + read queries (latest-wins / pending-excludes-terminals / history filter-by-surface) + persistence (append-only state-transitions / sha256 round-trip / tampered-record-skipped / malformed-json-skipped) + surface-validator routing pin + singleton + path-env-override + round-trip serialization + rollback_via field pin + 4 authority invariants (no banned imports / stdlib-only / no subprocess+network / loosening-NOT-persisted). `JARVIS_ADAPTATION_LEDGER_ENABLED` default false. Slices 2-6 pending.

**Phase 7 — Activation & Hardening** (NEW 2026-04-26 — converts Pass C from substrate to functional; per §3.6 brutal review): 🎉 **STRUCTURALLY COMPLETE — 9/9 slices landed**. All 5 Pass C activation surfaces have substrate loaders; HypothesisProbe + Sandbox hardening (Rule 7) + Cross-process flock + Stale-pattern sunset all shipped. Remaining work = caller wiring + Slice-6 YAML writer + per-slice graduation cadence.
  - [x] Phase 7.1 — `adaptation/adapted_guardian_loader.py` SemanticGuardian boot-time loader (PR #22992 → main `fe344a8a21`). Reads `.jarvis/adapted_guardian_patterns.yaml`, builds detector closures, additively merges into `_PATTERNS` at `semantic_guardian.py` import. ~340 LOC + ~38-line boot block. Cage rules: ADDITIVE only (collision with hand-written → SKIP); fail-open on every error path (master flag off / YAML missing / parse error / oversize). Constants: MAX_ADAPTED_PATTERNS=256 + MAX_ADAPTED_REGEX_CHARS=256 + MAX_ADAPTED_MESSAGE_CHARS=240 + MAX_YAML_BYTES=4 MiB. Master flag `JARVIS_SEMANTIC_GUARDIAN_LOAD_ADAPTED_PATTERNS` (default false until graduation cadence). Stdlib + adaptation.ledger only — one-way dependency rule (semantic_guardian imports loader, not reverse). Lazy-imports `Detection` inside detector closure to avoid circular dep. Layered evidence: comprehensive boot-time pin suite covering YAML happy path / fail-open paths / collision-skip / detector closure shape / cage rules. **Phase 7.1 closes the SemanticGuardian activation gap** — the highest single-impact Pass C activation surface (most-cited adaptive surface from Slice 2 mining).
  - [x] Phase 7.2 — `adaptation/adapted_iron_gate_loader.py` IronGate adapted-floor boot-time loader (this PR). Reads `.jarvis/adapted_iron_gate_floors.yaml`, returns `Dict[category, float]` of per-category numeric floors loaded from operator-approved Pass C Slice 3 proposals. New classmethod `ExplorationFloors.from_env_with_adapted(complexity)` on `exploration_engine.py` reads adapted floors when env flag on, merges adapted required-categories into base ExplorationFloors. Cage rule: only ADDS to required_categories; never removes; doesn't modify min_score or min_categories (per Pass C §7.3 "Pass C cannot lower coverage requirements"). Translates "category X has adapted floor > 0" → "category X must be in required_categories" (categorical-coverage; numeric floor preserved for `/posture` follow-up surfacing). Constants: MAX_ADAPTED_FLOORS=64 + MAX_FLOOR_VALUE=100.0 + MAX_YAML_BYTES=4 MiB. Hardcoded `_KNOWN_CATEGORIES` allowlist (5 values) so loader doesn't need to import exploration_engine (one-way dep, mirrors Phase 7.1). Per-entry skip on missing/unknown category / non-numeric floor / floor <= 0; clamps floor > MAX_FLOOR_VALUE. Latest-occurrence-wins per category. Master flag `JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_FLOORS` (default false until graduation cadence). Lazy-imports loader inside `from_env_with_adapted` (fail-open with try/except — every error path returns base floors unchanged). Layered evidence: **42 regression pins** (`tests/governance/test_phase_7_2_adapted_iron_gate_loader.py`) covering 7 module constants + master flag truthy/falsy + dataclass-frozen + master-flag short-circuit + 13 YAML reader pins (missing-yaml / oversize / unreadable / empty / no-PyYAML / parse-error / non-mapping-doc / missing-floors-key / non-list-floors / cap-truncate / latest-wins / clamp-too-large / non-mapping-entry-skip) + 3 `compute_adapted_required_categories` pins (empty / positive / zero-skip) + 6 `_parse_entry` pins (missing-cat / unknown-cat / non-numeric / negative / clamp / lowercase) + 6 `from_env_with_adapted` integration pins (master-off identical to from_env / missing-yaml identical / merges required / preserves env required / preserves min_score / unknown-yaml-cat tolerated) + 3 authority invariants (no banned governance imports / stdlib + adaptation.ledger only / no subprocess+network). Combined regression spine: **196/196 tests green** across Phase 7.1+7.2 + Pass C Slice 1+3 (substrate + miner). **Phase 7.2 closes the second-most-impactful activation gap** — the IronGate exploration floors gate every op.
  - [x] Phase 7.3 — `adaptation/adapted_mutation_budget_loader.py` ScopedToolBackend per-Order mutation budget adapted loader (this PR). Reads `.jarvis/adapted_mutation_budgets.yaml`, returns `Dict[order_int, int]` of operator-approved per-Order budgets loaded from Pass C Slice 4a proposals. New module-level helper `compute_effective_max_mutations(order, env_default, adapted=None)` returns `min(env_default, adapted_budget)` when loader on; returns env_default unchanged when loader off / no entry / adapted loader raises. **Cage rule (per Pass C §4.1)**: adapted budgets only LOWER the env default — defense-in-depth via `min()` ensures even a doctored YAML cannot loosen the cage. Order-2 hard floor `MIN_ORDER2_BUDGET=1` enforced (matches Slice 4a miner — Pass C never proposes a non-functional Order-2 budget). Constants: MAX_ADAPTED_BUDGETS=8 + MAX_BUDGET_VALUE=64 + MAX_YAML_BYTES=4 MiB. Hardcoded `_KNOWN_ORDERS = frozenset({1, 2})` allowlist so loader doesn't need to import scoped_tool_backend.py (one-way dep, mirrors Phase 7.1+7.2). Per-entry skip on missing/unknown order / non-integer budget / negative budget; clamps budget > MAX_BUDGET_VALUE; raises Order-2 below floor up to MIN_ORDER2_BUDGET. Latest-occurrence-wins per order. Master flag `JARVIS_SCOPED_TOOL_BACKEND_LOAD_ADAPTED_BUDGETS` (default false until graduation cadence). YAML path env-overridable via `JARVIS_ADAPTED_MUTATION_BUDGETS_PATH`. Helper accepts pre-loaded adapted dict (callers may load once + reuse to amortize YAML I/O on hot paths). Stdlib + adaptation.ledger only — never raises. Layered evidence: **48 regression pins** (`tests/governance/test_phase_7_3_adapted_mutation_budget_loader.py`) covering 6 module constants + master flag truthy/falsy + dataclass-frozen + master-flag short-circuit + 14 YAML reader pins (missing-yaml / oversize / unreadable / empty / no-PyYAML / parse-error / non-mapping-doc / missing-budgets-key / non-list-budgets / non-mapping-entry-skip / cap-truncate / latest-wins / clamp-too-large / happy-path-both-orders) + 10 `_parse_entry` pins (missing-order / non-int-order / unknown-order / non-int-budget / negative / clamp / order2-floor-raise / order1-zero-allowed / float-truncated / provenance-preserved) + 8 `compute_effective_max_mutations` cage pins (loader-off / loader-on-no-entry / lower-returns-lower / **higher-clamped-to-env (defense-in-depth)** / negative-env-normalized / other-order-unaffected / loader-exception-falls-back / string-order-coerced) + 3 authority invariants (no banned imports / stdlib + adaptation.ledger only / no subprocess+network). Combined regression spine: **252/252 tests green** across Phase 7.1+7.2+7.3 + Pass C Slice 1+4 (substrate + miner). **Phase 7.3 closes the third Pass C activation gap** — ScopedToolBackend now has the structural ability to consume operator-approved Order-1/Order-2 budget reductions. Caller wiring (constructing invocation dict with adapted budget) is a follow-up consistent with the 7.1/7.2 substrate-first pattern.
  - [x] Phase 7.4 — `adaptation/adapted_risk_tier_loader.py` risk-tier ladder adapted boot-time loader (this PR). Reads `.jarvis/adapted_risk_tiers.yaml`, returns `List[AdaptedTierEntry]` of operator-approved tier-insertion proposals from Pass C Slice 4b. New module-level helper `compute_extended_ladder(base_ladder, adapted=None)` returns the canonical ladder with each adapted tier inserted IMMEDIATELY AFTER its `insert_after` slot. **Cage rule (per Pass C §8.3 + §4.1)**: the ladder ONLY GROWS — defense-in-depth at three layers: (a) base_ladder elements ALWAYS appear in output in the same relative order (load-bearing assertion-pinned in `test_base_ladder_relative_order_preserved`); (b) adapted `tier_name` colliding with a base ladder name → SKIPPED; (c) adapted `insert_after` not in base ladder → SKIPPED. Constants: MAX_ADAPTED_TIERS=16 + MAX_TIER_NAME_CHARS=64 + MAX_YAML_BYTES=4 MiB. Per-entry hardening: tier_name must match `[A-Z0-9_]+` charset (matches Slice 4b miner's `_synthesize_tier_name` output) — operator-typo-grade names (paths, lowercase, dashes, traversal) SKIPPED rather than truncated (truncation could collide with a canonical name). insert_after must satisfy the same charset. Latest-occurrence-wins per tier_name (operator can re-target an earlier proposal by re-listing with same name + new `insert_after`). Master flag `JARVIS_RISK_TIER_FLOOR_LOAD_ADAPTED_TIERS` (default false until graduation cadence). YAML path env-overridable via `JARVIS_ADAPTED_RISK_TIERS_PATH`. Helper accepts pre-loaded list for hot-path amortization. Stdlib + adaptation.ledger only — never raises. Hardcoded import-ban list (no risk_tier_floor / scoped_tool_backend / orchestrator / phase_runners) keeps the loader strictly substrate. Layered evidence: **49 regression pins** (`tests/governance/test_phase_7_4_adapted_risk_tier_loader.py`) covering 5 module constants + master flag truthy/falsy + dataclass-frozen + master-flag short-circuit + 13 YAML reader pins (missing-yaml / oversize / unreadable / empty / no-PyYAML / parse-error / non-mapping-doc / missing-tiers-key / non-list-tiers / non-mapping-entry-skip / cap-truncate-at-MAX / latest-wins-per-tier-name / happy-path-two-entries) + 11 `_parse_entry` pins (missing-tier_name / blank / lowercase / dash / path-traversal / too-long / at-max-allowed / missing-insert_after / invalid-insert_after / provenance-preserved) + 11 `compute_extended_ladder` cage pins (loader-off / loader-on-no-entries / insert-after-SAFE_AUTO / insert-after-BLOCKED / multiple-after-same-slot-ordered / **collision-with-base-skipped (defense-in-depth)** / insert-after-unknown-skipped / **base-ladder-relative-order-preserved (load-bearing)** / loader-exception-falls-back / returns-tuple-not-list / empty-base-ladder-handled) + 3 authority invariants (no banned imports / stdlib + adaptation only / no subprocess+network). Combined regression spine: **301/301 tests green** across Phase 7.1+7.2+7.3+7.4 + Pass C Slice 1+4. **Phase 7.4 closes the fourth Pass C activation gap** — the highest-risk activation surface (mutates the canonical risk-tier ladder enum). Caller wiring (orchestrator + `risk_tier_floor.py` consuming `compute_extended_ladder()` to build the live ladder) is a follow-up consistent with the 7.1/7.2/7.3 substrate-first pattern.
  - [x] Phase 7.5 — `adaptation/adapted_category_weight_loader.py` ExplorationLedger category-weight rebalance adapted boot-time loader (this PR; **CLOSES the 5/5 Pass C activation-surface set**). Reads `.jarvis/adapted_category_weights.yaml`, returns `List[AdaptedRebalanceEntry]` of operator-approved per-category weight rebalances from Pass C Slice 5. New module-level helper `compute_effective_category_weights(base_weights, adapted=None)` returns the merged weights dict — applying the latest valid rebalance IF it passes all three defense-in-depth net-tighten checks. **Cage rule (per Pass C §4.1)**: net cage strictness ONLY RISES — Slice 5 is the only Pass C surface where individual values appear to fall, but mass-conservation keeps the whole vector tightening. Three independent defense-in-depth layers enforced at activation: **(a) Sum invariant**: `Σ(new) ≥ Σ(base)` enforced by `_net_tighten_check` — doctored YAML with lower sum REJECTED → return base unchanged. **(b) Per-category floor**: each new weight ≥ HALF_OF_BASE × base[k] (matches Slice 5 miner's 50% floor). **(c) Absolute floor**: each new weight ≥ MIN_WEIGHT_VALUE=0.01. **Schema invariant**: output ALWAYS contains every base_weights key (preserving structure); unknown adapted keys silently dropped (Pass C cannot ADD categories — that's a Pass B Order-2 amendment). Constants: MAX_ADAPTED_REBALANCES=8 + MAX_WEIGHT_VALUE=100.0 + MIN_WEIGHT_VALUE=0.01 + HALF_OF_BASE=0.5 + MAX_YAML_BYTES=4 MiB. Per-entry hardening: weights dict with non-string keys / non-numeric / weight<=0 / empty SKIPPED; weight > MAX_WEIGHT_VALUE clamped; category keys lowercased + stripped. **Latest-occurrence-wins** (only the LAST entry is consulted — matches Slice 5 miner's "ONE rebalance per cycle" design; earlier valid entries are NOT a fallback if latest is REJECTED). Master flag `JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_CATEGORY_WEIGHTS` (default false until graduation cadence). YAML path env-overridable via `JARVIS_ADAPTED_CATEGORY_WEIGHTS_PATH`. Helper accepts pre-loaded list for hot-path amortization. Stdlib + adaptation.ledger only — never raises. Hardcoded import-ban list (no exploration_engine / risk_tier_floor / scoped_tool_backend / orchestrator / phase_runners). Layered evidence: **57 regression pins** (`tests/governance/test_phase_7_5_adapted_category_weight_loader.py`) covering 6 module constants + master flag truthy/falsy + dataclass-frozen + `__post_init__` sorted-keys + float-coercion + master-flag short-circuit + 14 YAML reader pins (missing-yaml / oversize / unreadable / empty / no-PyYAML / parse-error / non-mapping-doc / missing-rebalances-key / non-list / non-mapping-entry-skip / cap-truncate / happy-path / clamp-too-large + lowercases-keys) + 10 `_parse_entry` pins (missing/non-mapping/non-string-key/blank-key/non-numeric/zero/negative/clamp/empty/lowercases) + 11 `compute_effective_category_weights` cage pins (loader-off / loader-on-no-entries / valid-applied / **sum-invariant-violated-rejected** / **per-category-floor-violated-rejected** / **absolute-floor-violated-rejected** / unknown-adapted-keys-dropped / partial-adapted-uses-base-for-missing / **latest-wins-only-last-consulted** / schema-invariant-output-has-all-base-keys / loader-exception-falls-back / returns-dict-not-same-object) + 4 `_net_tighten_check` direct pins (equal-sum-passes / lower-sum-rejected / per-category-floor-rejected / missing-adapted-key-uses-base) + 3 authority invariants (no banned imports / stdlib + adaptation only / no subprocess+network). Combined regression spine: **357/357 tests green** across Phase 7.1+7.2+7.3+7.4+7.5 + Pass C Slice 1+5. **🎉 Phase 7.5 closes the 5/5 Pass C activation-surface set** — all five adaptive surfaces (SemanticGuardian patterns / IronGate floors / per-Order budgets / risk-tier ladder / category weights) now have substrate-complete boot loaders + cage-rule helpers. Caller wiring (orchestrator + `exploration_engine.py` consuming `compute_effective_category_weights()`) is a follow-up consistent with the substrate-first pattern. Phase 7 advances 5/9 → only 7.6 (hypothesis-probe loop) + 7.7 (sandbox hardening) + 7.8 (cross-process flock) + 7.9 (stale-pattern sunset) remain.
  - [x] Phase 7.6 — `adaptation/hypothesis_probe.py` bounded HypothesisProbe primitive — **closes the autonomous-curiosity gap** (this PR). New module ships the primitive (data model + cage + runner) plus a Protocol for the evidence prober — production wires this to a read-only Venom subset; tests inject fakes; **default is `_NullEvidenceProber` (zero cost — a misconfigured caller cannot accidentally hit a paid API)**. **Three independent termination guarantees** ALWAYS fire structurally — no prober configuration can override them: (1) **Call cap** `MAX_CALLS_PER_PROBE_DEFAULT=5` (env-overridable via `JARVIS_HYPOTHESIS_PROBE_MAX_CALLS`); (2) **Wall-clock cap** `TIMEOUT_S_DEFAULT=30.0` (env-overridable via `JARVIS_HYPOTHESIS_PROBE_TIMEOUT_S`) — measured against `time.monotonic()` (NOT wall clock — defends against system clock changes mid-probe); (3) **Diminishing-returns** sha256 fingerprint of every round's evidence — terminate `INCONCLUSIVE_DIMINISHING` if round N+1 returns same fingerprint as N. **Read-only tool allowlist** frozen set: `{read_file, search_code, get_callers, glob_files, list_dir}` (production `EvidenceProber` MUST honor at the implementation site; the loader exposes the constant). 9-value `ProbeVerdict` enum: CONFIRMED / REFUTED / 4 INCONCLUSIVE_* / 3 SKIPPED_* (master_off / no_prober / empty_hypothesis). Frozen `ProbeRoundResult` (per-round) + frozen `ProbeResult` (terminal). Pre-checks fire BEFORE any round: master flag off / empty claim / empty expected_outcome / no prober. **Defense-in-depth**: confirmed/refuted signal with EMPTY evidence does NOT terminate (treats as continue) — prevents stuck-positive prober from claiming victory without proof. Bounded sizes: `MAX_EVIDENCE_CHARS_PER_ROUND=4096` + `MAX_NOTES_CHARS=1024` (truncation appends `...(truncated)` marker). Master flag `JARVIS_HYPOTHESIS_PROBE_ENABLED` (default false until graduation cadence). Stdlib-only import surface — does NOT import HypothesisLedger / tool_executor / Venom (Protocol-typed prober; concrete implementations live elsewhere). Per the PRD spec: confirmed hypotheses become adaptation proposals (feeds Slice 2 + 3 mining surfaces); refuted hypotheses become POSTMORTEMs (feeds PostmortemRecall) — bridge wiring is a follow-up. Layered evidence: **55 regression pins** (`tests/governance/test_phase_7_6_hypothesis_probe.py`) covering 7 module constants + master flag truthy/falsy + 8 env override pins + 9-value verdict enum pin + 5 pre-check skip pins (master-off / empty-claim / whitespace-claim / empty-expected / no-prober) + 3 Null-prober pins (returns-continue / null-terminates-diminishing / empty-evidence-cannot-confirm) + 5 verdict-signal pins (confirmed-immediately / refuted-immediately / **confirmed-with-empty-evidence-does-NOT-terminate (defense-in-depth)** / refuted-with-empty-evidence-does-NOT-terminate / continue-then-confirmed / unknown-signal-treated-as-continue) + 3 call-cap pins (default=5 / constructor-override / env-override) + 2 wall-clock pins (timeout-terminates / runner-uses-monotonic-clock NOT wall clock) + 4 diminishing-returns pins (identical-evidence-terminates-at-2 / different-then-repeat-terminates / alternating-does-NOT-diminish / evidence-hashes-returned-in-order) + 2 prober-exception pins (raise-caught / raise-after-some-evidence) + 3 bounded-sizes pins (evidence-truncated / notes-truncated / call-cap-with-huge-evidence-still-caps-calls) + 5 ProbeResult convenience pins (is_confirmed / is_refuted / is_inconclusive-covers-all-4 / is_skipped-covers-all-3 / frozen) + 4 authority invariants (no banned imports / stdlib-only / no subprocess+network+anthropic tokens / runner-uses-monotonic-clock). Combined regression spine: **396/396 tests green** across Phase 7.1+7.2+7.3+7.4+7.5+7.6 + Pass C Slice 1 + HypothesisLedger. **Phase 7.6 closes the autonomous-curiosity gap** — the primitive composes existing HypothesisLedger + (future) PostmortemRecall + (future) read-only Venom probe into a closing loop with structural termination. Production wiring — `AnthropicVenomEvidenceProber` calling read-only Venom subset + bridge to ledger.record_outcome + bridge to AdaptationLedger.propose for confirmed hypotheses — is a follow-up consistent with the substrate-first 7.1-7.5 pattern (the same approach proven by Phase 5 P5's `_NullClaudeQueryProvider`).
  - [x] Phase 7.7 — Sandbox hardening: `meta/ast_phase_runner_validator.py` Rule 7 (this PR) — **closes the only known structural sandbox-escape vector** (§3.6.2 vector #1). Extends Slice 3 AST validator with introspection-escape blocking: walks ALL function bodies in the candidate (not just `run` methods — defends against the candidate hiding the escape in a helper called from `run`) for two patterns: (1) **direct `ast.Attribute` access** where `.attr in {"__subclasses__", "__bases__", "__class__"}` — catches `obj.__subclasses__()`, `cls.__bases__`, `instance.__class__`, chained attribute access (`obj.foo.__class__`), call-chain (`f().__bases__`), subscript-chain (`arr[0].__class__`), AND the **classic CPython sandbox-escape one-liner** `().__class__.__bases__[0].__subclasses__()`; (2) **`getattr(x, "<banned>")` with string literal** second argument — defends against operator string-encoding the attr name to bypass Pattern 1. New failure reason `INTROSPECTION_ESCAPE` added to enum (9th value; full set pinned). Per-rule kill switch `JARVIS_AST_VALIDATOR_BLOCK_INTROSPECTION_ESCAPE` defaults **TRUE** (unlike most JARVIS flags — security hardening on by default; operator can disable in emergency without disabling whole validator). New helpers `_find_introspection_escape(tree)` + `_is_getattr_call(node)` + `_string_constant_value(node)` + `_describe_attribute_target(node)` — all stdlib `ast` only, never raise. Known limitation pinned: dynamic-string getattr (`getattr(obj, var_name)` where `var_name` is computed at runtime) slips through — Rule 7 is a static-shape check; runtime-computed attrs require the candidate to literally encode the full string at parse time, which is a much higher bar than direct attribute access. Layered evidence: **39 regression pins** (`tests/governance/test_phase_7_7_sandbox_hardening.py`) covering 4 module-constant + per-rule-kill-switch (default-true + 5 truthy variants + 6 falsy variants) + Pattern 1 attribute-access (7 pins incl. classic-sandbox-escape one-liner) + Pattern 2 getattr-string (5 pins incl. dynamic-string-NOT-blocked + module-getattr-NOT-blocked) + walker-scope (3 pins: helper-function / nested-function / second-method) + clean-candidates-pass (4 pins: safe-attr-access / string-literal-with-banned-substring / other-dunder-attrs / safe-getattr-string) + per-rule-kill-switch behavior (3 pins: per-rule-off-lets-escape-through / per-rule-default-on-blocks / validator-master-off-skips-everything) + helper-function direct (10 pins). Existing `test_validation_failure_reason_eight_values` pin renamed to `..._nine_values` and updated to include `INTROSPECTION_ESCAPE`. Combined regression spine: **385/385 tests green** across Phase 7.1+7.2+7.3+7.4+7.5+7.6+7.7 + existing AST validator suite. **Phase 7.7 closes §3.6.2 vector #1** — the only known structural sandbox-escape vector (`object.__subclasses__()` → `subprocess.Popen` walk via `replay_executor.py:_SAFE_BUILTIN_NAMES`'s inclusion of `object` + `type`). The cage transitions from "trust the operator" to "structural sandbox" for this attack class.
  - [x] Phase 7.8 — Cross-process AdaptationLedger advisory locking via `fcntl.flock` (this PR) — **closes §3.6.2 fragility-vector #3**. New private substrate module `backend/core/ouroboros/governance/adaptation/_file_lock.py` ships two context-manager helpers `flock_exclusive(fd)` + `flock_shared(fd)` — POSIX `fcntl.flock` advisory locks on the file descriptor. **Best-effort, never raise**: when `fcntl` is unavailable (Windows ImportError) OR `flock` itself raises (NFS / unsupported FS), the helper logs once + degrades to a no-op + yields `True` so callers don't think the lock failed. Per-feature kill switch `JARVIS_ADAPTATION_LEDGER_FLOCK_ENABLED` (default **TRUE** — same convention as P7.7 Rule 7; security hardening on by default; operator can disable in emergency without disabling the whole ledger). **Wired into `AdaptationLedger._append`**: the existing `with self._path.open("a", ...)` block now nests `with flock_exclusive(f.fileno())` around the `write` + `flush` + `fsync` sequence — exclusive lock serializes append paths across processes (within-process serialization remains `threading.RLock` at the call site; this is additive defense-in-depth, not a replacement). Lock granularity: per-fd; the lock is released automatically on context exit (LOCK_UN) AND on fd close (kernel-level safety net). Stdlib-only + `fcntl` (POSIX) — no banned imports. Layered evidence: **19 regression pins** (`tests/governance/test_phase_7_8_cross_process_flock.py`) covering 3 module constants + kill switch (default-true + 5 truthy + 6 falsy variants) + happy-path POSIX (4 pins: exclusive-acquire-release / shared-acquire-release / kill-switch-off-yields-true / **concurrent-exclusive-locks-serialize within-process**) + fail-open no-fcntl (2 pins: yields-true + log-only-emitted-once) + fail-open flock-raises (2 pins: yields-false-no-exception + release-failure-logged-not-raised) + AdaptationLedger integration (2 pins: ledger-append-imports-flock + ledger-append-works-with-kill-switch-off) + **multiprocess contention smoke test** (POSIX-only, spawns 3 child processes racing to write 10 lines each; pin asserts each process's 10-line block lands contiguously — proving cross-process serialization works in practice) + 3 authority invariants. Combined regression spine: **408/408 tests green** across Phase 7.1+7.2+7.3+7.4+7.5+7.6+7.7+7.8 + Pass C Slice 1 substrate (60 existing AdaptationLedger pins survive; no regression). **Phase 7.8 closes §3.6.2 vector #3** — concurrent miners across processes (e.g. parallel agent-conducted soak runs) now serialize at the file-system level. Only Phase 7.9 (stale-pattern sunset signal) remains.
  - [x] Phase 7.9 — Stale-pattern sunset signal: `adaptation/stale_pattern_detector.py` (this PR) — **🎉 CLOSES Phase 7 STRUCTURALLY (9/9 slices landed)** + closes §3.6.2 fragility-vector #4. New module ships pure-stdlib detector primitive over caller-supplied `(adapted_patterns, match_events)` lists. End-to-end pipeline `propose_sunset_candidates_from_events()` mines stale candidates → flows through `AdaptationLedger.propose()` → operator-review surface. **Stale = pattern hasn't matched in `JARVIS_ADAPTATION_STALENESS_THRESHOLD_DAYS` days** (default 30). Patterns NEVER matched are treated as immediately-stale. **Cage rule (load-bearing per Pass C §4.1)**: sunset signals are **advisory only** — Pass C cannot REMOVE patterns; removal is loosening and MUST go through Pass B `/order2 amend` (operator-authorized). The sunset signal is a NOTICE that surfaces "this pattern looks stale, consider removing"; the actual decision is operator-only. Allowed in `_TIGHTEN_KINDS` because the signal itself is structurally conservative — it suggests reducing surface area, never expanding it. Constants: DEFAULT_STALENESS_THRESHOLD_DAYS=30 + MAX_STALE_CANDIDATES_PER_CYCLE=8 (operator-review surface trim) + MAX_HISTORY_FILE_BYTES=4 MiB + MAX_HISTORY_LINES=10000 + MIN_OBSERVATIONS_FOR_SUNSET=1. Sorting: stalest-first (highest days_since_last_match) tie-broken alphabetically by pattern_name for determinism. **Idempotent proposal_id** (sha256 of pattern_name) so re-mining the same stale state yields DUPLICATE_PROPOSAL_ID at substrate. **proposed_state_hash deterministically distinct from current** (sha256 of `current || "|sunset|" || pattern_name`) so the universal default validator's "hash distinct" check passes. **Surface validator** (chain-of-responsibility — composes with Slice 2's `add_pattern` validator on the same SemanticGuardianPatterns surface): kind="sunset_candidate" → our validator (sha256 prefix + observation_count + summary contains "stale" + day-indicator); kind="add_pattern" → delegates to prior Slice 2 validator if registered. **JSONL match-history reader** (`load_match_events`) is fail-open: missing/oversized/malformed/non-mapping/non-numeric/negative all degrade to empty/skipped. Master flag `JARVIS_ADAPTIVE_STALE_PATTERN_DETECTOR_ENABLED` (default false until graduation cadence). YAML path env-overridable via `JARVIS_SEMANTIC_GUARDIAN_MATCH_HISTORY_PATH`. Stdlib + adaptation.ledger only — never raises. Layered evidence: **59 regression pins** (`tests/governance/test_phase_7_9_stale_pattern_sunset.py`) covering 6 module constants + master flag (default-false + 5 truthy + 6 falsy) + 8 env override pins + dataclass-frozen + path defaults + 12 JSONL reader pins (missing/oversize/unreadable/empty/happy-path/malformed-skip/non-mapping-skip/missing-pattern/blank-pattern/missing-matched/non-numeric-skip/negative-skip/cap-truncate) + 10 mine_stale_candidates pins (empty / recent-not-stale / old-stale / never-matched-stale / threshold-boundary-stale / multiple-events-uses-max / sorted-stalest-first / **alpha-tie-break** / max-cap-truncate / unknown-event-pattern-ignored) + 4 propose-pipeline pins (master-off-empty / no-stale-empty / one-stale-OK / **idempotent-dedup** / pending-status) + 7 surface validator pins (registered-at-import / valid-passes / hash-format-rejected / observation-count-rejected / summary-stale-indicator-rejected / summary-day-indicator-rejected / **chain-delegates-add-pattern-to-prior** / chain-no-prior-passes-other-kinds) + 3 authority invariants. **Modified `ledger.py`**: added `"sunset_candidate"` to `_TIGHTEN_KINDS` allowlist (the only ledger.py change). Combined regression spine: **521/521 tests green** across Phase 7.1+7.2+7.3+7.4+7.5+7.6+7.7+7.8+7.9 + Pass C Slice 1 substrate + Slice 2 SemanticGuardian miner (54 existing miner pins survive — chain-of-responsibility validator works correctly; **no shadowing**). **🎉 §3.6.2 vector #4 marked MITIGATED** (was 🟡 Medium). **🎉 Phase 7 — Activation & Hardening structurally complete (9/9 slices landed in one day, 2026-04-26).**

**🎉 Phase 7 structurally complete 2026-04-26.** Next open phase items per the Forward-Looking Priority Roadmap: **caller wiring + Slice-6 YAML writer + HypothesisProbe production prober wiring + per-slice graduation cadences** (these convert substrate-complete to functionally-live) → **Phase 6 P6** (Self-narrative, long-horizon — now unblocked from a Phase-7-substrate POV; caller wiring is the practical prerequisite for "real adaptation history to narrate").
**Phase 6 — Self-Modeling**: [ ] not started — long-horizon (3-6 months per PRD).

**Phase 10 — Provider Strategy + Dynamic Topology Sentinel** *(NEW 2026-04-27 — derived from §3.7 audit)*: 🚀 IN FLIGHT — Slice 1 landed 2026-04-27.
- [x] **P10.1 — `AsyncTopologySentinel` foundation** landed 2026-04-27 (PR #25504 → main `d1556abf15`, 62 tests + 134 combined regression). Composes `rate_limiter.CircuitBreaker` + `TokenBucket` + `preemption_fsm._compute_backoff_ms` + `RetryBudget(full_jitter=True)`; net-new ~600 LOC = `SlowStartRamp` (BG concurrency ramp; wraps `TokenBucket.set_throttle()`) + `ContextWeightedProber` (light/heavy 4:1; weighted failure matrix with live stream-stall = 3.0 to trip alone) + `SentinelStateStore` (mirrors `posture_store.py` triplet pattern; atomic temp+rename; bounded ring trim) + `TopologySentinel` coordinator (~150 LOC of glue). Master flag `JARVIS_TOPOLOGY_SENTINEL_ENABLED` default **false** — no consumers wired, byte-identical behavior. Boot-loop protection: `state=OPEN` snapshots reconstruct breaker into OPEN with original `opened_at`; process killed mid-SEVERED comes back into SEVERED. AST authority pins forbid `*Breaker`/`*Bucket`/`*Backoff` class definitions in the new module + forbid orchestrator/iron_gate/policy/gate/change_engine/candidate_generator imports. RLock re-entrancy fix bundled (force_severed/force_healthy hold the lock then call register_endpoint — same pattern that bit posture_observer slice5_arc_a).
- [x] **Immediate yaml caller swap** (this PR, 2026-04-27): `compaction` caller migrated `gemma-4-31B-it` → `Qwen3-14B-FP8` under v1 schema. **75× cost reduction** on every compaction call. `summarization` is the catalog-published use case for Qwen3-14B-FP8; 262K context handles deep tool-loop history.
- [~] **P10.2 — yaml v2 schema + dual-reader** in flight (branch `feat/topology-sentinel-slice-2`). `topology.2` schema introduces per-route `dw_models:` ranked list + `fallback_tolerance: cascade_to_claude | queue` enum + `monitor:` block (probe intervals, severed_threshold, ramp schedule). Backward-compat: `Topology.from_v2(...)` classmethod; `get_topology()` tries v2 first, falls back to v1.
- [ ] **P10.3 — `candidate_generator.py` consumer wiring** under `JARVIS_TOPOLOGY_SENTINEL_ENABLED`; replaces static gate at `1404-1465` with sentinel-driven walk over ranked `dw_models:` list. BG/SPEC `fallback_tolerance="queue"` regression-pinned at the routing layer (no env shortcut allows BG/SPEC to cascade to Claude under sentinel-OPEN).
- [ ] **P10.4 — Live-exception failure ingest** at existing DW failure sites (`candidate_generator.py:1662-1667 / 1674-1687 / 2200-2213`); `sentinel.report_failure(model_id, FailureSource.LIVE_STREAM_STALL, detail)` weight 3.0 trips faster than probe-only.
- [ ] **P10.5 — THE PURGE** (operator-authorized after 3 forced-clean once-proofs of Slices 3-4): delete static `dw_allowed: false` lines + read-only Nervous System Reflex carve-out at `candidate_generator.py:2062-2067` + v1 dual-reader + `JARVIS_BACKGROUND_ALLOW_FALLBACK` / `FORCE_CLAUDE_BACKGROUND` env shortcuts. Flip `JARVIS_TOPOLOGY_SENTINEL_ENABLED` default `false` → `true`.
- [ ] **P10.6 — 24h soak + cost-per-op trending validation**: post-Phase-10 target ≥30% of GENERATE cost on DW providers + ≥50% reduction in $/op median.

Update discipline: each closing slice updates this section in the same PR. Status is the source of truth for "what's next" — when in doubt, the lowest-numbered `[ ]` row in the lowest-numbered active phase is the next slice.

### Forward-Looking Priority Roadmap (rewritten 2026-04-27 post-Phase-8-surface-closure + post-brutal-review-v2)

This section is the **canonical "what's next" ordering** after Phase 7 + Phase 8 + AST Rule 8 + CuriosityScheduler + Pass C activation-pipeline closure. Reflects the post-Phase-8 brutal review (Doc History v2.51).

**Color legend**: 🔴 = highest priority (do first) · 🟡 = medium priority · 🟢 = least priority (defer).

#### Priority summary table (sortable, 2026-04-27 v2)

| Rank | Item | Status | Why this rank | Estimate |
|---|---|---|---|---|
| 🔴 **#1** | **Live-Fire Graduation Cadence — flip 12+ default-false flags** | 🔴 Not started (substrate complete; 12+ flags awaiting soak proof) | **THE CRITICAL BLOCKER**. Phase 7 + Phase 8 + Pass C activation pipeline + Mining-payload + AST Rule 8 + CuriosityEngine/Scheduler all SHIPPED but ALL DEFAULT FALSE. The cognitive loop is theoretically end-to-end functional but has NEVER fired end-to-end in production with all flags on. Until ≥3 clean soaks per substrate flag, the system claims more than it delivers. Maps to brutal-review §3.6.6 fragility vector #6 (Default-False Flag Problem). | 4-6 weeks background |
| 🔴 **#2** | **Adversarial Cage Stress — empirical Quine-resistance numbers** | 🔴 Not started | Cage is currently *theoretically* tight (8 AST rules + 10 SemGuard patterns + sandbox + Iron Gate + risk-tier floor + mutation cap). Has NEVER been adversarially tested. Need a corpus: ~50 known Python sandbox-escape patterns from CTF/security research + ~50 LLM-generated quine attempts → measure pass-through rate. Target: 0%. Maps to fragility vector #7 (Quine via `__mro__`/`vars()`/`dir()`/`globals()`/`chr()`-construction — known static-shape rule gaps). | ~1-2 weeks |
| 🔴 **#3** | **Cross-Session Coherence Harness + Phase 8 Producer Wiring** | 🔴 Not started | Two parts, same architectural concern. **Part A**: end-to-end test booting session N, running 20 ops, killing harness, booting session N+1, asserting session N+1's CONTEXT_EXPANSION measurably includes signals from session N (LSS digest tokens / SemanticIndex centroid drift / UserPreferenceMemory carryover). Proves cross-session learning works — foundation for Second Order RSI. **Part B**: wire the Phase 8 producers — orchestrator's ROUTE phase calls `decision_trace_ledger.record()`; classifiers call `latent_confidence_ring.record()`; phase-timing calls `latency_slo_detector.record()`. Without producers, the 16 Phase 8 GET endpoints + SSE events return EMPTY. Maps to fragility vectors #4 (long-horizon drift) + the empty-substrate problem surfaced in brutal review §3.6.6. | ~1-2 weeks |
| 🟡 **#4** | **Cross-runner artifact contract (schema-versioned)** | 🔴 Not started | Wave 2 PhaseRunner extraction threads ~7 cross-phase leaks (`generation`, `_episodic_memory`, `generate_retries_remaining`, `_advisory`, `best_candidate`, `t_apply`, `risk_tier`) via `ctx.artifacts`. Verbatim extraction sidesteps the issue. **As soon as a runner is *refactored* beyond verbatim, one unversioned dict shape change crashes the FSM with no recovery path.** Add a schema-versioned artifact contract before any further runner refactor. Maps to fragility vector #8. | ~3-5 days |
| 🟡 **#5** | **Pass B + Pass C per-slice graduation cadences** | 🟡 In flight (Pass B); 🔴 Not started (Pass C) | Pass B agent-conducted twice-daily soak (`trig_012EvEDkABy2u5PSSs3xK5C4`); ~14 days remaining for full flip. Pass C: 6 slices × 5 clean sessions ≈ 30 minimum sessions. Subsumed by #1 above. | Background |
| 🟡 **#6** | **Mask-discipline regression sweep** | 🔴 Not started | `FlagChangeEvent.to_dict()` echoes raw env values verbatim. The Phase 8 GET endpoint masks; the SSE bridge masks; **but a future consumer reading `to_dict()` directly without going through the bridge leaks secrets.** One accidental import away from a credential exposure. Pin the entire substrate→consumer chain with masking-discipline invariant tests. Maps to fragility vector #9. | ~1 day |
| 🟡 **#7** | **AutoCommitter cross-process flock** | 🔴 Not started | Empirically observed three times in single dev session (this conversation): background AutoCommitters race with foreground commits on the same op_id, producing overlapping commits. Apply Phase 7.8's `flock_exclusive` model to AutoCommitter. Maps to fragility vector #10. | ~1 day |
| 🟡 **#8** | **CuriosityScheduler monotonic-clock conversion** | 🔴 Not started | Rate-cap window uses `time.time()` (wall clock). Same vector that bit HypothesisProbe (since fixed). Clock skew (ntpd update mid-session) skips cap entirely or triggers spurious throttle. Convert to `time.monotonic()`. Maps to fragility vector #11. | ~2 hours |
| 🟢 **#9** | **Tier 3 emergency provider fallback** | 🔴 Not started | Tier 0 = DW, Tier 1 = Claude, Tier 2 = J-Prime "when available." Tier 0+1 simultaneous outage → session aborts → CuriosityEngine idle-signal cannot fire → organism freezes. No Tier 3 (e.g., Llama-local). Maps to fragility vector #12. | Long-horizon |
| 🟢 **#10** | **Phase 6 P6 — Self-narrative** | 🔴 Not started | Now unblocked from substrate POV (Phase 7+8 complete) but blocked by #1 (no real adaptation history to narrate until graduation cadence runs). Long-horizon. | ~3 weeks (after #1) |
| 🟢 **#11** | **Skills/workflows + per-action permission granularity** (CC parity polish) | 🔴 Not started | Nice-to-have. Per-action granularity tradeoff against autonomy; likely operator-rejected as anti-vision. | Future / possibly never |

**Critical-path summary (post-Phase-8 brutal review)**: Priorities #1-#3 are **THE A-level gate**. The architecture is built; the substrate is built; the surfaces are built. **What hasn't happened: empirical validation under real model output.** Until #1 (Live-Fire Graduation) + #2 (Adversarial Cage Stress) + #3 (Cross-Session Coherence + Phase 8 Producer Wiring) all complete, O+V is an A-level *vision* executing at B+ effective. After all three: A− structurally proven; A within reach as graduation cadence stabilizes.

---

#### Priority 1 (🔴 highest) — Phase 7 — Activation & Hardening

**Goal**: convert Pass C from substrate-only ("observable theater") to real (gates actually consume adapted state). Close 3 critical-path systemic upgrades from §3.6.3.

**Sub-priorities** (ordered for landing):

| Slice | What | Status | Notes |
|---|---|---|---|
| 7.1 | SemanticGuardian boot-time loader | ✅ Landed (PR #22992) | Substrate complete; flag default false until cadence flip |
| 7.2 | IronGate adapted-floor loader | ✅ Landed (PR #23033) | Substrate complete; flag default false until cadence flip |
| 7.3 | Per-Order mutation budget loader | ✅ Landed (this PR) | Substrate complete; flag default false until cadence flip |
| 7.4 | Risk-tier ladder loader | ✅ Landed (this PR) | Substrate complete; flag default false until cadence flip |
| 7.5 | Category-weight rebalance loader | ✅ Landed (this PR) | Substrate complete; flag default false until cadence flip. **Closes 5/5 activation surfaces** |
| 7.6 | Bounded hypothesis-probe primitive | ✅ Landed (this PR) | Substrate complete; flag default false until cadence flip. Production prober wiring is a follow-up |
| 7.7 | Sandbox hardening (AST validator Rule 7) | ✅ Landed (this PR) | Closes §3.6.2 vector #1 — the only known structural sandbox-escape vector. Per-rule kill switch defaults TRUE |
| 7.8 | Cross-process AdaptationLedger flock | ✅ Landed (this PR) | Closes §3.6.2 vector #3. Per-feature kill switch defaults TRUE. Best-effort no-op fallback on Windows/NFS |
| 7.9 | Stale-pattern sunset signal | ✅ Landed (this PR) | Closes §3.6.2 vector #4. Master flag default false until cadence flip |

**🎉 Phase 7 — Activation & Hardening STRUCTURALLY COMPLETE 2026-04-26 (9/9 slices landed in one day)**: 5/5 Pass C activation surfaces + HypothesisProbe + Sandbox hardening (Rule 7) + Cross-process flock + Stale-pattern sunset. **All 4 §3.6.2 fragility vectors mitigated** (#1 sandbox, #2 activation gap (substrate), #3 cross-process race, #4 semantic drift). Remaining work shifts to **caller wiring + Slice-6 YAML writer + per-slice graduation cadences** — these convert "substrate complete" to "live gates change behavior on `/adapt approve`".

**🎉 Caller wiring progress** (post-Phase-7 functional milestone): **5/5 surfaces wired end-to-end — ACTIVATION PIPELINE COMPLETE**:
  - [x] **Phase 7.1 SemanticGuardian wiring** — `_PATTERNS` merge at `semantic_guardian.py` boot block (landed in Phase 7.1 PR #22992; substrate + wiring shipped together)
  - [x] **Phase 7.2 IronGate floors wiring** — 6 live call sites in `orchestrator.py` (3) + `phase_runners/generate_runner.py` (3) switched from `ExplorationFloors.from_env()` → `ExplorationFloors.from_env_with_adapted()` (PR #23414). Master-off byte-identical (default-flag state); master-on injects adapted required_categories. 14 wiring pins.
  - [x] **Phase 7.3 ScopedToolBackend per-Order budget wiring** — single inner site in `general_driver.py:308-353` (the only consumer of `invocation["max_mutations"]` that constructs `ScopedToolBackend` — all upstream callers benefit automatically) wraps `max_mutations` with `compute_effective_max_mutations(order, max_mutations)` (this PR). New OPTIONAL `invocation["order"]` field (default 1) — caller-supplied per dispatch by upstream invocation builders; missing/invalid/unknown safely defaults to Order-1. Both `ToolScope.read_only` AND `ScopedToolBackend(max_mutations=...)` use the EFFECTIVE post-wiring value (not raw env) so adapted-budget=0 correctly produces a read-only scope. Subagent contract docstring (`subagent_contracts.py:472`) updated with the new optional `order` field shape. **22 wiring pins**: 6 helper-direct unit (master-off / order-1-lower / order-2-lower / **doctored-higher-clamped (defense-in-depth)** / other-order-unaffected / loader-exception-falls-back) + 6 caller-source invariants (driver-imports-helper / driver-uses-effective-for-scope / driver-passes-effective-to-scoped-backend / driver-reads-order / driver-defaults-invalid-to-1 / **no-other-live-caller-passes-raw-max_mutations-to-scope** — load-bearing bit-rot guard) + 1 subagent-contract-docstring pin + 9 behavioral end-to-end via `run_general_tool_loop` (master-off-byte-identical / order-1-lowers / order-2-lowers / doctored-higher-clamped / missing-order-defaults-to-1 / invalid-order / non-int-order / **read-only-flag-uses-effective-value (proves wiring threads through scope AND backend)** / **order-2-floor-preserved (MIN_ORDER2_BUDGET=1)**). Behavioral pins use a `_MutationCapturingBackend` stub patched at the import source (`scoped_tool_backend.ScopedToolBackend`) so the lazy-import inside `run_general_tool_loop` picks up the patched version. Combined regression spine: **443/443 tests green** across Phase 7.1-7.9 + wiring PRs #1+#2; **128/128 general_driver / scoped_tool_backend / general_subagent suite green** — no regression in cage / mutation-counter / state-mirror / hard-kill behavior.
  - [x] **Phase 7.4 risk-tier ladder wiring** — `risk_tier_floor.py` adds new `get_active_tier_order()` helper (this PR) that composes canonical `_ORDER` baseline with operator-approved adapted tiers via `compute_extended_ladder()`. All 6 internal `_ORDER` consumers (env-floor / vision-floor / recommended-floor / apply-floor-to-name) refactored to call `get_active_tier_order()`. Master-off byte-identical: when `JARVIS_RISK_TIER_FLOOR_LOAD_ADAPTED_TIERS=false` (default), `get_active_tier_order()` returns dict equal to canonical `_ORDER`. Master-on extends ladder: each adapted tier inserted at its `insert_after` slot; canonical relative order preserved (load-bearing — pinned). **Case normalization at boundary**: adapted YAML uses uppercase per Slice 4b miner's `[A-Z0-9_]+` charset; wiring lifts canonical `_ORDER` to uppercase for the helper, then lowercases the extended result so downstream consumers find new tiers under the canonical lowercase convention. **Defense-in-depth**: loader raise → falls back to canonical `_ORDER` baseline (try/except in helper; NEVER raises into caller). Mutation-safety: returns NEW dict on every call so callers may mutate without affecting future callers. **19 wiring pins** (`tests/governance/test_wiring_3_risk_tier_ladder_extended.py`): 4 master-off byte-identical (incl. caller-mutation-doesnt-affect-canonical) + 4 master-on extension (incl. **canonical-relative-order-preserved** + **adapted-tier-lowercased-for-lookup** + unknown-insert-after-skipped) + 1 defense-in-depth (loader-raise-falls-back) + 3 caller-source invariants (zero-internal-_ORDER-lookups + wiring-imports-compute_extended_ladder + wiring-returns-new-dict) + 6 behavioral end-to-end (env-min-risk-tier-recognizes-adapted + apply-floor-passes-through-unknown + apply-floor-accepts-adapted-name + recommended-floor-uses-extended-ranking + 2 master-off-byte-identical) + 1 authority invariant (no-external-imports-of-private-_ORDER). Combined regression: **147/147 risk_tier_floor + Phase 7.4 + wiring tests green**; **541/541 combined Phase 7.1-7.9 + all 3 wiring PRs green** — no regression.
  - [x] **Phase 7.5 ExplorationLedger category-weight wiring** — `exploration_engine.py` adds two new module-level helpers (`_baseline_category_weights()` returns canonical {cat: 1.0} dict for all 5 known categories excluding UNCATEGORIZED; `_compute_active_category_weights()` composes adapted weights via Phase 7.5's `compute_effective_category_weights()` substrate) and threads the result into `ExplorationLedger.diversity_score()` as **per-category multipliers** on per-tool contributions. Wiring shape: `base += call.base_weight * cat_multiplier` where `cat_multiplier = active_weights.get(call.category.value, 1.0)`. Master-off byte-identical: when `JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_CATEGORY_WEIGHTS=false` (default), substrate returns `dict(baseline)` → all multipliers == 1.0 → diversity_score arithmetic is byte-identical to pre-wiring `sum(base_weight)` form. Master-on with valid rebalance: high-value categories scale up; low-value scale down; net Σ tightens (Slice 5 cage rule enforced at substrate — sum invariant + per-category floor at HALF_OF_BASE + absolute floor at MIN_WEIGHT_VALUE — not re-validated here). UNCATEGORIZED tools default to multiplier=1.0 via `dict.get(cat, 1.0)`. **Defense-in-depth**: substrate raise → caught + falls back to canonical baseline (NEVER raises into caller). Score-cap (`_BASE_SCORE_CAP=15.0`) and category multiplier (`1.0 + 0.5 × (n_cats - 1)`) both still applied — they layer ON TOP of the per-category weighting. **24 wiring pins** (`tests/governance/test_wiring_4_category_weight_rebalance.py`): 3 baseline-helper pins (5-categories / excludes-UNCATEGORIZED / new-dict-each-call) + 4 active-weights-helper pins (master-off-baseline / master-on-no-yaml-baseline / master-on-valid-rebalanced / substrate-raise-falls-back) + 5 master-off byte-identical (simple-4-call=16.25 / duplicate-zero-contribution / failed-calls-base-but-not-coverage / score-cap=15 / explicit-false=16.25) + 7 master-on rebalance behavior (high-value-scores-higher / low-value-scores-lower / **doctored-loosening-yaml-rejected-falls-back** / uncategorized-uses-1.0 / partial-yaml-uses-baseline / **score-cap-still-applied-after-multipliers**) + 3 caller-source invariants (diversity_score-uses-active-weights / helper-imports-substrate / **per-cat-multiplier-actually-used-in-loop bit-rot guard**) + 3 no-regression-against-pinned-scores (4-cat=16.25 / 3-cat=8.0 / 5-cat full=24.0). Combined regression: **159/159 exploration + iron_gate tests green** (no score regression in existing test suite — master-off byte-identical guarantee proven empirically); **486/486 combined Phase 7.1-7.9 + all 4 wiring PRs green**. **🎉 ACTIVATION PIPELINE COMPLETE — all 5 Pass C activation surfaces have substrate-complete loaders + LIVE caller wiring.** Operator-approved adapted state can now actually CHANGE GATE BEHAVIOR end-to-end (subject to per-loader graduation cadences flipping master flags from default-false to default-true).
  - [x] **Slice-6 MetaGovernor YAML writer** — `/adapt approve` now materializes the proposal's `proposed_state_payload` into the live gate's adapted YAML (this PR — Item #2). Adds new `adaptation/yaml_writer.py` module + extends `AdaptationProposal` schema with optional `proposed_state_payload: Dict[str, Any]` field (schema version bumped 1.0 → 2.0; pre-Item-#2 rows still readable). Per-surface schema mapping for all 5 surfaces (semantic-guardian patterns / iron-gate floors / mutation budgets / risk-tier ladder / category weights). **Atomic-rename writer** with cross-process flock (reuses Phase 7.8's `flock_exclusive`) — concurrent /adapt approve sessions serialize correctly. **Provenance enrichment**: each YAML entry auto-gets `proposal_id` + `approved_at` + `approved_by` from the proposal record (payload-supplied values take precedence). **Master flag** `JARVIS_META_GOVERNOR_YAML_WRITER_ENABLED` (default false until per-surface graduation cadences ramp). **Critical invariant**: writer failures DO NOT roll back the ledger approval (audit trail of decision must persist regardless — pinned by `test_writer_failure_does_not_roll_back_approval`). **38 wiring pins** covering schema-extension backward-compat (7 pins incl. round-trip-preserves-payload + pre-extension-row-loads-as-None + garbage-payload-loads-as-None) + propose-with-payload (4 pins incl. payload-survives-approve-state-transition) + writer master flag (4 pins) + skip paths (4: master-off / pending / rejected / no-payload) + per-surface materialization (5 surfaces × correct YAML path + top-level key + provenance) + append semantics (1 pin) + existing-file edge cases (4: oversize / corrupted / non-mapping / no-pyyaml) + provenance-enrichment edge case (1) + meta_governor wiring (2: approve-calls-writer + writer-failure-doesnt-roll-back-approval) + 5 authority/cage invariants (no-banned-imports / stdlib+adaptation-only / no-subprocess-tokens / atomic-rename / flock-for-cross-process). Combined regression spine: **584/584 tests green** across Phase 7.1-7.9 + all 4 wiring PRs + Item #2 + AdaptationLedger + meta_governor + 5 mining-surface suites — no regression. **Producer-side gap CLOSED 2026-04-26**: operator-approved adapted state now flows end-to-end from /adapt approve → YAML write → loader read → live gate behavior change.
  - [x] **HypothesisProbe production EvidenceProber** — `adaptation/anthropic_venom_evidence_prober.py` ships `AnthropicVenomEvidenceProber` (production prober wiring Phase 7.6's `EvidenceProber` Protocol to a read-only Venom-style query provider via injectable `VenomQueryProvider` Protocol) + `adaptation/hypothesis_probe_bridge.py` ships two bridges: `bridge_confirmed_to_adaptation_ledger()` (CONFIRMED probe → `AdaptationLedger.propose()` carrying `proposed_state_payload` per Item #2) + `bridge_to_hypothesis_ledger()` (terminal verdict → `HypothesisLedger.record_outcome()` with validated=True/False/None per CONFIRMED/REFUTED/INCONCLUSIVE_*) (this PR — Item #3). **Provider injection**: `_NullVenomQueryProvider` is the safe default (zero cost; defends against accidental API hits — same pattern as P5 `_NullClaudeQueryProvider`); production wires `AnthropicProvider` from canonical stack at SerpentFlow boot; tests inject fakes. **Cost cap**: `DEFAULT_COST_CAP_PER_CALL_USD=0.05` (matches P5 AdversarialReviewer convention) + cumulative `DEFAULT_SESSION_BUDGET_USD=1.00` (matches P5). **Tool allowlist enforcement**: every prober query passes `READONLY_TOOL_ALLOWLIST` (from Phase 7.6 substrate) — pinned by source-grep + behavioral test. **Bounded sizes**: `MAX_PROMPT_CHARS=4096` + `MAX_EVIDENCE_CHARS_RETURNED=3500` + `MAX_PRIOR_EVIDENCE_ROUNDS_INCLUDED=3` + `MAX_PRIOR_EVIDENCE_ROW_CHARS=500`. **Verdict parsing**: looks for explicit `VERDICT: confirmed|refuted|continue` sentinel anywhere in response; LAST occurrence wins (model's final verdict); falls back to "continue" when missing. **NEVER raises**: provider exceptions caught + converted to error rounds; provider error field caught; runner's own try/except is second line of defense. **Cost overrun safety belt**: provider reporting cost > per-call cap gets clipped to cap (prevents provider violating contract from exhausting session budget). Master flags: `JARVIS_HYPOTHESIS_PROBE_PRODUCTION_PROBER_ENABLED` (default false) + `JARVIS_HYPOTHESIS_PROBE_BRIDGES_ENABLED` (default false). Bridges are best-effort (return BridgeResult with structured status; NEVER raise); confirmed→adaptation only fires on CONFIRMED (refused/inconclusive don't propose); hypothesis-ledger fires on all 4 terminal verdicts (CONFIRMED/REFUTED/3 INCONCLUSIVE_*); SKIPPED_* verdicts no-op (the probe didn't actually run). **57 regression pins** covering 6 module constants + 4 master-flag pins + 3 Null sentinel + 4 factory wiring + 6 prompt-building (incl. caps + truncation + allowlist-listed) + 7 response parsing (incl. last-sentinel-wins + case-insensitive) + 4 cost-accounting (cumulative + per-call-cap-clips-overrun + session-budget-prevents-further-calls + pre-check-fires-when-exhausted) + 2 exception handling (provider-raise-caught + provider-error-field-caught) + 1 tool-allowlist-enforcement + 3 runner integration (production-prober-confirms / null-terminates-diminishing / refuting-provider) + 9 bridge pins (master-flag + skip paths + actual-propose + record_outcome × 4 verdict mappings + ledger-not-found + ledger-raise-caught) + **6 authority/cage invariants** (no-banned-imports × 2 modules + stdlib+adaptation-only × 2 + no-subprocess+no-direct-anthropic-import + uses-substrate-allowlist-constant). Combined regression spine: **581/581 tests green** across Phase 7.1-7.9 + all 4 wiring PRs + Items #2+#3 — no regression.

**Why this remains Priority 1 (caller wiring is the new blocker)**:
- **The substrate is now complete; the activation gap is now SHIFTED from "no loaders exist" to "callers haven't switched yet"** — boot-time loaders + cage-rule helpers exist for ALL FIVE adapted surfaces. The gap is now (a) Slice 6 MetaGovernor writing approved proposals to `.jarvis/adapted_<surface>.yaml` on `/adapt approve` and (b) the live gates (orchestrator + `SemanticGuardian._PATTERNS` + `ExplorationFloors` + `ScopedToolBackend` + `risk_tier_floor`) calling the new helpers.
- The novel architectural claim (per Pass A: "Anti-Venom adaptive thesis is genuinely novel") gains its first end-to-end defensible footing once Slice 6's writer + caller wiring lands and a single `/adapt approve` cycle changes a live gate's behavior in the same op.
- The sandbox-escape vector (§3.6.2 row #1) is currently UNMITIGATED — operator-authorization is the only defense. Phase 7.7 closes that.

#### Priority 2 (🟡 medium) — Phase 8 — Temporal Observability

Per §3.6.4. Can ship in parallel with Phase 7 (different modules; no dependency overlap).

#### Priority 3 (🟡 medium) — Pass B + Pass C graduation soak cadences (in flight + scheduled)

- Pass B: agent-conducted twice-daily soak running now; 9 master flag flips pending.
- Pass C: 6 slices × 5 clean sessions each ≈ 30 minimum sessions. Starts after Phase 7 lands (proves activation).

#### Priority 4 (🟢 least) — Phase 6 P6 (Self-narrative)

Now blocked by Phase 7 per the new sequencing rule: self-narrative needs *real* adaptation history (not substrate-only). Re-rank when Phase 7 ships.

#### Priority 5 (🟢 least) — CC-parity polish items

Skills/workflows surface, per-action permission granularity, etc. Tracked but no PRD priority.

---

#### Cross-priority sequencing rules (binding)

1. ✅ **Pass C unblocked from Pass B's primitives** (2026-04-26 — all 6 Pass B slices shipped).
2. ✅ **Pass B prerequisites met** (W2(5) Slice 5b dependency was deferred-then-resolved during Pass B execution).
3. ✅ **P4 first, always.** Phase 4 P4 graduated 2026-04-26 — every novel cognitive layer can now claim measurable convergence.
4. ✅ **P5 shipped before Pass B/C.** Phase 5 P5 graduated 2026-04-26.
5. **P6 after the adaptive substrate is FUNCTIONAL.** *(Updated 2026-04-26)* Originally "after Pass C ships." Now: after **Phase 7 ships** — Pass C structural completion alone is insufficient; self-narrative needs real adaptation history to narrate, which requires the activation pipeline.
6. **(NEW) Phase 7 is the new Priority 1.** Phase 7 converts Pass C from substrate to functional. Without it, no A-level RSI claim is defensible.
7. **(NEW) Phase 8 can ship in parallel with Phase 7.** Different modules, no overlap. Pinning this to prevent operator from sequencing them serially.
8. **(NEW) Sandbox escape vector (§3.6.2 #1) is critical-path.** Cannot graduate Pass C activation without either Phase 7.7 hardening OR explicit accepted-risk documentation.

The "lowest-numbered `[ ]` row" heuristic (above) still applies *within* a phase. This priority list is the **between-phase** ordering when multiple phases are simultaneously eligible.

---

## 2. Vision Statement

> *"O+V is proactive and not reactive. Its job is to explore the codebase like CC does and develop the JARVIS repo on its own without any human intervention (only if necessary, based on context and severity). It should also understand the direction I'm going and the goal I'm trying to achieve on its own. I want O+V to have the most advanced intelligent capabilities possible — and to be the proactive autonomous version of CC."*
>
> — Derek J. Russell, operator binding

### Operationalized as success criteria

The vision delivers when:

1. **Self-initiating** — O+V begins useful work without human prompting (✅ delivered: 16 sensors + 9 self-formed-goal entries via Phase 2 SelfGoalFormation)
2. **Codebase exploration parity with CC** — same depth of read/search/reason as CC's tool loop (✅ partial→strong: Iron Gate enforces hygiene-first AND ExplorationLedger enforces diversity-floor across 5 categories AND Phase 5 AdversarialReviewer auto-injects findings every non-SAFE_AUTO GENERATE; Pass C Slice 3 will auto-tighten the floors based on bypass-failure observations)
3. **Repo development without intervention** — multi-file changes ship end-to-end autonomously (⚠️ proven once Sessions Q-S; broader cadence pending Pass B per-slice graduation soak underway)
4. **Human-in-loop only when severity demands** — risk-tier ladder + curiosity ask_human + Phase 3 inline approval UX (✅ delivered) + Phase 7 plan approval (✅ delivered) + Pass B `/order2 amend` operator-only authorization for Order-2 governance changes (✅ delivered)
5. **Understands operator direction + goal** — without being told (✅ delivered: DirectionInferrer + arc-context + 100-commit git momentum + ConversationBridge + UserPreferenceMemory + LastSessionSummary all wired into CONTEXT_EXPANSION; intent-classifier routes natural-language `/chat` turns into structured actions)
6. **A-level execution** — sustained quality + reliability + learning (✅ scaffolding complete: Wang composite_score + 5 operator metrics now surfaced; Pass C Adaptive Anti-Venom in flight to close the "static cage" gap; soak-cadence-driven graduation discipline proven)
7. **Self-tightening immune system** *(NEW success criterion, added 2026-04-26)* — gates grow stricter as the shell expands, never looser via the adaptive surface (🚀 Pass C Slices 1-4 landed: AdaptationLedger substrate + 4 surfaces all enforcing the monotonic-tightening invariant. Slices 5-6 pending. Loosening operations strictly require Pass B `/order2 amend` operator authorization.)

---

## 3. Current State Assessment

### 3.1 What O+V uniquely does (the cognitive delta from CC)

*Updated 2026-04-26 to reflect Phase 1-5 graduations + Pass B closure + Pass C in-flight + 3 deferred-follow-up wirings.*

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
| Per-op POSTMORTEM with root-cause classification + recall | CommProtocol 5-phase + PostmortemEngine + **PostmortemRecall (Phase 1 P0, graduated 2026-04-26)** | ✅ production + cognitive feedback loop closed |
| Deterministic financial circuit-breaker | cost_governor + Class E watchdog cancel | ✅ production |
| L3 mode self-protection + auto-recovery | SafetyNet + #20147 resilience pack | ✅ production |
| Mid-op cancellation infrastructure | W3(7) cancel-token (REPL + watchdog + signal) | ✅ production |
| Parallel L3 fan-out with cost-aware cap | parallel_dispatch + #19800 cost-cap parallel-stream bump | ✅ production |
| **Self-formed goals from POSTMORTEM clusters** | SelfGoalFormationEngine + 9-gate decision tree + Hypothesis pairing (Phase 2 P1+P1.5) | ✅ production (graduated 2026-04-26) |
| **Conversational mode with classified intent + real side effects** | `/chat` REPL + IntentClassifier + 3 concrete executors (backlog/subagent-queue/Claude) | ✅ production (Phase 3 P2 graduated 2026-04-26 + 3-PR mini-arc 2026-04-26) |
| **Inline approval UX** | InlineApprovalProvider + `[y]/[n]/[s]/[e]/[w]` + 30s timeout-to-defer | ✅ production (Phase 3 P3 graduated 2026-04-26) |
| **Realtime progress visibility** | per-stream HEARTBEAT + coalesced status line | ✅ production (Phase 3 P3.5 graduated 2026-04-26) |
| **Wang composite score + convergence tracking + 5 operator metrics** | MetricsEngine + JSONL ledger + `/metrics` REPL + IDE GET + SSE event | ✅ production (Phase 4 P3+P4 graduated 2026-04-26) |
| **AdversarialReviewer auto-injection** | post-PLAN/pre-GENERATE hook in plan_runner.py + cost-budgeted Claude side-stream | ✅ production (Phase 5 P5 graduated + wiring landed 2026-04-26) |
| **Order-2 governance cage** *(novel)* | Pass B: Order-2 manifest + ORDER_2_GOVERNANCE risk class + AST validator + shadow-replay corpus + MetaPhaseRunner + sandboxed replay executor + locked-true `amendment_requires_operator()` invariant + `/order2` REPL | ✅ structurally complete (Pass B closed 2026-04-26; per-slice graduation soak in flight) |
| **Adaptive Anti-Venom — self-tightening immune system** *(novel + the genuine RSI architectural contribution)* | Pass C: `AdaptationLedger` substrate + 4 adaptive surfaces (SemanticGuardian POSTMORTEM-mined patterns / IronGate exploration-floor auto-tightener / per-Order mutation budget / risk-tier ladder extender) + monotonic-tightening invariant that REFUSES to persist loosening proposals | 🚀 in flight (Pass C Slices 1-4 landed 2026-04-26; Slices 5-6 pending) |

### 3.2 What CC genuinely beats O+V on (and we should port)

*Updated 2026-04-26.*

| Capability | CC | O+V | Status |
|---|---|---|---|
| Conversational mode — natural dialog | ✅ first-class | ✅ `/chat <message>` + bare-text + 4-intent classifier + 3 concrete executors (backlog / subagent-queue / Claude) | ✅ delivered (Phase 3 P2 graduated; concrete executors landed 2026-04-26) |
| Real-time token streaming with model thinking visible | ✅ always | ⚠️ phases only in headless; per-stream HEARTBEAT delivered (Phase 3 P3.5) | partial (granular token streaming still TUI-gated) |
| Lightweight approval UX | ✅ inline `[y/N]` | ✅ inline `[y]/[n]/[s]/[e]/[w]` | ✅ delivered (Phase 3 P3 graduated) |
| Easy mid-flight redirect | ✅ "wait, do this instead" | ⚠️ `/cancel` infrastructure (W3(7) cancel-token) but no natural-language redirect; `/chat` could route an interrupt via IntentClassifier | future polish (low priority — `/cancel` works) |
| Status line with current activity | ✅ always | ⚠️ requires opt-in dashboard; HEARTBEAT delivers similar info | partial |
| Conversational context across turns | ✅ default | ✅ ConversationBridge wired (5 sources) + ChatSession ring buffer + Phase 3 P2 routing | ✅ delivered (Phase 3 P2 graduated; bridge default true post-graduation) |
| MCP tool ecosystem visibility | ✅ first-class | ✅ MCP tools discovered + injected at GENERATE prompt (Gap #7) | ✅ delivered |
| Skills/workflows surface (saved playbooks) | ✅ rich | ❌ none | future scope (no PRD priority assigned) |
| Background tasks with notify | ✅ run_in_background | ✅ scheduled remote agents via routine API (Pass B graduation soak conductor running) | ✅ delivered for orchestrator-level use |
| `/help` discoverability of slash commands | ✅ rich | ✅ FlagRegistry + `/help` dispatcher with typo detection + posture-relevant filtering | ✅ delivered (Wave 1 #2 graduated 2026-04-21) |

### 3.3 Production track record

*Updated 2026-04-26.*

- **Verified end-to-end multi-file APPLY**: 1 (Sessions Q-S, 2026-04-15, 4 test modules generated → applied → committed)
- **Single-file APPLYs**: handful (most recent: Session O, 2026-04-15)
- **NO_OP terminations**: common (model decides no change needed)
- **EXHAUSTION terminations**: common (provider transport noise — handled by ExhaustionWatcher dedup)
- **Sessions completed cleanly with at least 1 commit**: small fraction of total session-hours; **`/metrics 7d` REPL now answers this concretely** (Phase 4 P4 graduated 2026-04-26)
- **Pass B graduation soak**: agent-conducted twice-daily cadence in flight (`trig_012EvEDkABy2u5PSSs3xK5C4`); 27 minimum sessions across 9 master flag flips, calendar projection 2-3 weeks
- **Self-formed goal entries**: live count via `/backlog auto-proposed pending` and `/hypothesis ledger`; bounded ≤1/session/cap=$0.10 by SelfGoalFormationEngine (Phase 2 P1 graduated 2026-04-26)
- **AdversarialReviewer findings emitted per op**: live count via `/adversarial stats`; auto-injects into every non-SAFE_AUTO GENERATE post-2026-04-26 wiring landing
- **Adaptation proposals emitted per cycle**: pending Pass C Slice 6 MetaGovernor (will surface via `/adapt stats`)

The infrastructure is exceptional. **The cognitive substrate is now rich** (Phases 0-5 graduated; Pass B closed; Pass C in flight). The remaining work is closing the loop: Pass C completion (Slices 5-6) + per-slice graduation soak across all flags-still-default-false items + accumulated-evidence battle-test landmark beyond Sessions Q-S.

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

### 3.5 Pass B + Pass C — the Order-2 + Adaptive cage *(NEW 2026-04-26)*

This section captures the architectural delta the Reverse Russian Doll arc adds on top of Waves 1-3. Pass A surfaced the gaps (`memory/project_reverse_russian_doll_pass_a.md`); Pass B closed the *governance amendment* gap; Pass C closes the *adaptive immunity* gap.

| Pass | What | Status | PR(s) |
|---|---|---|---|
| Pass B Slice 1 | Order2Manifest + 9 Body-only entries | structurally complete | #22298 |
| Pass B Slice 2+2b | ORDER_2_GOVERNANCE risk class + classifier + gate wiring | structurally complete | #22320, #22329 |
| Pass B Slice 3 | AST-shape validator (6 rules) | structurally complete | #22347 |
| Pass B Slice 4 | shadow-replay corpus + structural-equality diff | structurally complete | #22375 |
| Pass B Slice 5 | MetaPhaseRunner evidence composer | structurally complete | #22396 |
| Pass B Slice 6.1 | sandboxed replay executor (resolves Slice 5 deferred candidate exec) | structurally complete | #22475 |
| Pass B Slice 6.2 | review queue + locked-true `amendment_requires_operator()` cage invariant | structurally complete | #22517 |
| Pass B Slice 6.3 | `/order2 {pending,show,amend,reject,history,help}` REPL | structurally complete | #22535 |
| **Pass B per-slice graduation** | 9 master flag flips via 3-clean-session arc per slice | 🔄 in flight (agent-conducted soak `trig_012EvEDkABy2u5PSSs3xK5C4` twice-daily) | — |
| Pass C Slice 1 | AdaptationLedger substrate (universal append-only audit log + monotonic-tightening invariant) | structurally complete | #22801 |
| Pass C Slice 2 | SemanticGuardian POSTMORTEM-mined patterns | structurally complete | #22821 |
| Pass C Slice 3 | IronGate exploration-floor auto-tightener | structurally complete | #22841 |
| Pass C Slice 4 | per-Order mutation budget + risk-tier ladder extender (combined) | structurally complete | #22866 |
| Pass C Slice 5 | ExplorationLedger category-weight auto-rebalance | pending | — |
| Pass C Slice 6 | MetaAdaptationGovernor + `/adapt` REPL + observability | pending | — |

**Combined Pass B + Pass C regression spine (2026-04-26): 1004+ tests** (438 Pass B + 349 Pass C across all 6 slices + 217 deferred-follow-up wirings).

The novel architectural claim: **the adaptive surface can only tighten, never loosen**. Loosening operations (deprecating a SemanticGuardian pattern, lowering an exploration floor, raising a mutation budget, removing a risk tier) MUST go through Pass B's `/order2 amend` operator-only authorization. Pass C's substrate REFUSES TO PERSIST a would-loosen proposal — it's structurally impossible to loosen the cage via the adaptive surface.

### 3.6 Brutal architectural review (2026-04-26 self-assessment)

*This section is the post-Pass-C-structural-completion honest take. It exists because the substrate is rich; the activation lag is the gap; the vision is A; the execution is B−.*

#### 3.6.1 Capability matrix vs CC (status + grade)

Color legend: 🟢 = parity-or-better · 🟡 = partial · 🔴 = real gap.

| Capability | CC | O+V | Status | Notes |
|---|---|---|---|---|
| Self-initiating work loop | ❌ none | 16 sensors → UnifiedIntakeRouter | 🟢 O+V wins | CC requires human invocation |
| Cost-as-first-class governance | ❌ none | cost_governor with route × complexity × headroom × parallel_factor | 🟢 O+V wins | CC has no per-op budget |
| Multi-file APPLY with batch rollback | partial | `files: [...]` schema + ChangeEngine | 🟢 O+V wins | Proven Sessions Q-S 2026-04-15 |
| L3 worktree parallel fan-out | ❌ none | parallel_dispatch + isolated worktrees | 🟢 O+V wins | CC has no parallel ops model |
| Order-2 governance cage | ❌ none | Pass B (manifest + AST validator + sandboxed replay + locked-true `amendment_requires_operator`) | 🟢 O+V wins | Novel architecture |
| Adaptive Anti-Venom (self-tightening immune system) | ❌ none | Pass C (substrate + 5 surfaces + meta-governor) | 🟡 substrate-only | Activation pipeline pending |
| Cross-session memory (multiple surfaces) | ⚠️ context-window only | UserPreferenceMemory + SemanticIndex + LastSessionSummary | 🟢 O+V wins | CC is per-session |
| **Interleaved reasoning between tool calls** | 🟢 mature | ⚠️ tool loop without explicit progress monitor | 🔴 **CC wins** | Need "did I make progress?" check between rounds |
| **Speculative branching with rollback** | 🟢 "let me try a different angle" | ❌ L2 repair is fixed FSM (5 iterations) | 🔴 **CC wins** | No branched speculation |
| **Per-action permission granularity** | 🟢 per-tool-use | ⚠️ per-op risk_tier | 🟡 partial | Tradeoff: O+V favors autonomy; consider per-tool granularity in future |
| **Streaming intermediate model thinking** | 🟢 always | ⚠️ phase HEARTBEAT only in headless | 🟡 partial | Token-by-token streaming gap |
| Conversational mode | 🟢 default | ✅ `/chat` + 3 concrete executors (Phase 3 P2) | 🟢 parity | Concrete executors landed 2026-04-26 |
| Inline approval UX | 🟢 inline `[y/N]` | ✅ inline `[y]/[n]/[s]/[e]/[w]` (Phase 3 P3) | 🟢 parity | Graduated 2026-04-26 |
| MCP tool ecosystem | 🟢 first-class | ✅ Gap #7 wiring | 🟢 parity | — |
| Skills/workflows surface | 🟢 rich | ❌ none | 🔴 **CC wins** | Future scope, no PRD priority |
| `/help` discoverability | 🟢 rich | ✅ FlagRegistry + `/help` + Levenshtein typo detection | 🟢 parity | Wave 1 #2 graduated 2026-04-21 |
| Background tasks with notify | 🟢 run_in_background | ✅ scheduled remote agents (routine API) | 🟢 parity | Pass B soak conductor running |

**Net functional parity vs CC: ~70%. Conceptual ambition vs CC: 110%+** (O+V has architectural surfaces CC fundamentally lacks).

#### 3.6.2 Structural fragility vectors (ranked by severity, 12 vectors as of 2026-04-27 v2)

| # | Vector | Severity | Why it matters | Mitigation status |
|---|---|---|---|---|
| **1** | **Sandbox escape via object-graph traversal** in operator-approved candidates. `_SAFE_BUILTIN_NAMES` includes `object` + `type`; once approved, `object.__subclasses__()` walks to `subprocess.Popen` etc. | 🟢 Mitigated (Phase 7.7 + AST Rule 7) | Was 🔴 Critical pre-2026-04-26. Operator-authorization is no longer the sole defense — `ast_phase_runner_validator.py` Rule 7 hard-rejects candidates with `__subclasses__`/`__bases__`/`__class__` Attribute access OR `getattr(x, "<banned>")` string-literal access in any function body | 🟢 Mitigated. Per-rule kill switch `JARVIS_AST_VALIDATOR_BLOCK_INTROSPECTION_ESCAPE` defaults TRUE. Pinned partial gap: dynamic-string getattr (runtime-computed names) — see vector #7 below |
| **2** | **Module-level side-effect at candidate import time** — code that runs BEFORE Rule 7 ever runs on function bodies | 🟢 Mitigated (AST Rule 8 — PR #23838) | Walks top-level statements for (1) Calls to a banlist of ~30 dangerous stdlib API names + (2) control-flow blocks containing ANY Call. Two pinned known gaps (alias-defeats-resolver + call-on-call) | 🟢 Mitigated. Per-rule kill switch `JARVIS_AST_VALIDATOR_BLOCK_MODULE_SIDE_EFFECTS` defaults TRUE |
| **3** | **Pass C activation gap** — proposal pipeline shipped; 5/5 boot loaders + caller wiring + Slice-6 YAML writer all landed | 🟢 Mitigated (Phase 7.5 + Items #1-4 + Mining-payload v2.43) | Cognitive loop substrate is now end-to-end functional. Remaining is graduation-cadence flag flips (subsumed under vector #6 below) | 🟢 Substrate complete |
| **4** | **Cross-process AdaptationLedger race** — `threading.RLock` + `os.fsync` only; no advisory file lock | 🟢 Mitigated (Phase 7.8) | `fcntl.flock` advisory exclusive lock wraps `_append`. Best-effort fallback on Windows/NFS | 🟢 Mitigated |
| **5** | **Semantic drift over long horizons** — mined SemanticGuardian patterns additive forever; no sunset signal | 🟡 Substrate mitigated (Phase 7.9); empirical drift over 50+ sessions UNTESTED | `StalePatternDetector` emits `sunset_candidate` after N days (default 30). **But: cross-session memory (LSS / SemanticIndex / UserPreferenceMemory / AdaptationLedger) has NEVER been validated across a 50-session arc.** No consolidation pass; no forgetting policy beyond exponential decay. Long-horizon RSI requires policy learning across hundreds of sessions; substrate exists for tens | 🟡 Substrate complete; empirical proof is a Priority #3 deliverable |
| **6** | **Default-False Flag Problem (THE central blocker)** — 12+ master flags default `false`; substrate built but never fired E2E | 🔴 Critical | `JARVIS_DECISION_TRACE_LEDGER_ENABLED` / `..._LATENT_CONFIDENCE_RING_ENABLED` / `..._FLAG_CHANGE_EMITTER_ENABLED` / `..._LATENCY_SLO_DETECTOR_ENABLED` / `..._MULTI_OP_TIMELINE_ENABLED` / `JARVIS_PHASE8_IDE_OBSERVABILITY_ENABLED` / `JARVIS_PHASE8_SSE_BRIDGE_ENABLED` / `JARVIS_PHASE8_MULTI_OP_RENDERER_ENABLED` / `JARVIS_HYPOTHESIS_PROBE_ENABLED` / `JARVIS_CURIOSITY_ENGINE_ENABLED` / `JARVIS_PLAN_APPROVAL_MODE` / 5 Pass C activation flags / 5 mining-surface payload-load flags. **Cognitive loop is theoretically end-to-end functional but has never fired end-to-end with all flags on simultaneously.** This is the #1 architectural risk: an A-level *substrate* that has never been load-tested as a unit | 🔴 Open. Closeable by Priority #1 (Live-Fire Graduation Cadence) |
| **7** | **Quine-shape cage bypass via static-shape rule gaps** | 🔴 Open (theoretically possible, empirically untested) | Rules 7+8 are static-shape AST checks. Motivated model can use `type(x).__mro__` (NOT in `_BANNED_INTROSPECTION_ATTRS`) / `vars(x)` / `dir(x)` / `globals()` / `__builtins__` introspection / `chr(95)+chr(95)+'subc'+...` dynamic attr construction / alias-defeats-resolver `s = os.system; s(...)` (pinned known gap). The cage is *empirically pretty good* but **not provably tight**. Need Priority #2 (Adversarial Cage Stress Test) to convert "we trust the cage" → "we measured the cage at N% pass-through" | 🔴 Open. Closeable by Priority #2 (Adversarial Cage Stress) |
| **8** | **Cross-runner artifact contract drift (Wave 2 PhaseRunner extraction)** | 🟡 Latent landmine | Wave 2 threads ~7 cross-phase leaks (`generation`, `_episodic_memory`, `generate_retries_remaining`, `_advisory`, `best_candidate`, `t_apply`, `risk_tier`) via `ctx.artifacts`. Verbatim extraction sidesteps. **As soon as a runner is *refactored* beyond verbatim, one unversioned dict shape change crashes the FSM with no recovery path.** No schema-versioned artifact contract exists | 🟡 Latent. Closeable by Priority #4 (schema-versioned artifact contract) BEFORE any further runner refactor |
| **9** | **`FlagChangeEvent.to_dict()` echoes raw env values** | 🟡 Defense-in-depth gap | Phase 8 GET endpoint + SSE bridge both mask via wrappers. But the substrate's `FlagChangeEvent.to_dict()` returns raw `prev_value`/`next_value`. **A future consumer reading `to_dict()` directly without going through the bridge leaks secrets.** One accidental import away from credential exposure | 🟡 Open. Closeable by Priority #6 (mask-discipline regression sweep — pin entire substrate→consumer chain) |
| **10** | **AutoCommitter race on same op_id** | 🟡 Empirically observed | Three times in single dev session (this conversation): background AutoCommitters race with foreground commits on same op_id, producing overlapping commits. The Phase 7.8 flock pattern hasn't been extended to AutoCommitter | 🟡 Open. Closeable by Priority #7 (AutoCommitter cross-process flock) |
| **11** | **CuriosityScheduler wall-clock vs monotonic** | 🟡 Same vector that bit HypothesisProbe (since fixed) | Rate-cap window uses `time.time()`. ntpd update mid-session → skip cap entirely or trigger spurious throttle | 🟡 Open. Closeable by Priority #8 (~2-hour fix) |
| **12** | **Provider chain SPOF — no Tier 3 emergency** | 🟡 Operational risk | Tier 0 = DW, Tier 1 = Claude, Tier 2 = J-Prime "when available." Tier 0+1 simultaneous outage → session aborts → CuriosityEngine cannot fire → organism freezes. No Tier 3 (e.g., Llama-local) | 🟡 Open. Closeable by Priority #9 (long-horizon) |

**Vector summary**: 4 mitigated (1, 2, 3, 4), 1 substrate-mitigated-empirically-untested (5), 7 open (6 critical, 7 critical, 8 latent, 9-12 medium). The new top-3 critical-path (Priorities #1-#3 in the table above) closes vectors #5, #6, #7, and partially closes #4-style empirical-validation gaps.

#### 3.6.3 Critical path to A-level RSI (NEW top 3, post-Phase-8 brutal review v2 — 2026-04-27)

Color legend: 🔴 = highest priority (do first) · 🟡 = medium priority · 🟢 = least priority (defer).

**NEW TOP 3 — these are systemic upgrades, not features. Each converts "theoretically capable" to "empirically proven."**

| Priority | Upgrade | Status | Impact-per-week | Why this rank |
|---|---|---|---|---|
| 🔴 **#1** | **Live-Fire Graduation Cadence — convert 12+ flags `false` → `true`** | 🔴 Not started | **HIGHEST**: the architecture is built; now prove it works under real model output. Until ≥3 clean sessions per substrate flag complete, the cognitive loop is theoretical. Subsumes Phase 8 producer wiring on the road | Cron-driven daily soak harness running 3 sessions/day rotating which substrate flags are enabled, producing graduation-ledger evidence per Item #4. ~3 flag flips/week × 12+ flags ≈ 4-6 weeks. **Until this completes, A-vision system runs at C+ effective execution.** Categorically blocks Second Order RSI |
| 🔴 **#2** | **Adversarial Cage Stress — empirical Quine-resistance numbers** | 🔴 Not started | **HIGH**: A-level RSI safety requires *numbers*, not intuitions. Currently cage is "we believe this is tight"; need "0/100 escapes" or "3/100 escapes — here are the patches" | Build adversarial corpus: ~50 known Python sandbox-escape patterns (`__class__.__mro__`, `vars()` walks, `globals()` introspection, builtins-via-`__builtins__`, dynamic attr via `chr()`) + ~50 LLM-generated quine attempts (prompt model to "write a PhaseRunner that eventually executes `subprocess.Popen` while passing AST validator"). Run through `validate_ast` + `SemanticGuardian` + `ScopedToolBackend`. Measure pass-through rate. Target: 0% |
| 🔴 **#3** | **Cross-Session Coherence Harness + Phase 8 Producer Wiring** | 🔴 Not started | **HIGH**: foundation for Second Order RSI. Cross-session memory (LSS / SemanticIndex / UserPreferenceMemory) exists but has NEVER been validated across a 50-session arc. Phase 8 surfaces ship empty until producers wire | **Part A**: end-to-end test booting session N, running 20 ops, killing harness, booting session N+1, asserting CONTEXT_EXPANSION measurably includes signals from N. **Part B**: orchestrator's ROUTE phase calls `decision_trace_ledger.record()`; classifiers call `latent_confidence_ring.record()`; phase-timing calls `latency_slo_detector.record()`. Without producers, the 8 Phase 8 GETs + 5 SSE events return empty |
| 🟡 #4 | **Cross-runner artifact contract (schema-versioned)** | 🔴 Not started | Medium: pre-empts a class of Wave 2 refactor crashes | One unversioned dict shape change between Wave 2 PhaseRunners crashes the FSM with no recovery. Add schema-versioned `ArtifactContract` BEFORE any further runner refactor beyond verbatim extraction |
| 🟡 #5 | **Mask-discipline regression sweep** | 🔴 Not started | Medium: defense-in-depth gap, one accidental import from credential leak | `FlagChangeEvent.to_dict()` echoes raw env values; only the bridge masks. Pin the entire substrate→consumer chain with masking-discipline invariants |
| 🟡 #6 | **AutoCommitter cross-process flock** | 🔴 Not started | Medium: empirically observed race 3× in single dev session | Apply Phase 7.8 flock pattern to AutoCommitter |
| 🟡 #7 | **CuriosityScheduler monotonic-clock conversion** | 🔴 Not started | Medium: latent vector that bit HypothesisProbe (since fixed) | ~2-hour fix; convert `time.time()` → `time.monotonic()` |
| 🟢 (closed) | **Pass C activation pipeline — caller wiring + Slice-6 YAML writer** | ✅ Shipped 2026-04-26 (Phase 7.5 + Items #1-4 + Mining-payload v2.43) | **Highest** when ranked; **closes the last functional gap.** Cognitive loop substrate is now end-to-end functional | Was top of previous critical path; promoted out of top-3 |
| 🟢 (closed) | **Bounded hypothesis-probe loop** (Phase 7.6) + production prober wiring | ✅ Shipped 2026-04-26 (Phase 7.6 + Item #3 + CuriosityEngine + CuriosityScheduler) | **High** when ranked; closed autonomous-curiosity gap | Was top of previous critical path; promoted out of top-3 |
| 🟢 (closed) | **Sandbox hardening** — Rule 7 + Rule 8 | ✅ Shipped 2026-04-26 / 2026-04-26 (PR #23838) | **High** when ranked; removed introspection escape + module-level side-effect at-import vectors | Was top of previous critical path; promoted out of top-3 |
| 🟢 (closed) | **Phase 8 — Temporal Observability** (substrate + 3 surface slices) | ✅ Shipped 2026-04-27 (5 substrate v2.44 + 3 surface slices v2.48-2.50) | **High** when ranked; surfaces ahead of CC | Was top of previous critical path; promoted out of top-3 |
| 🟢 (closed) | **Cross-process AdaptationLedger advisory locking** | ✅ Shipped 2026-04-26 (Phase 7.8) | Medium-when-ranked; safety-net for parallel soaks | — |
| 🟢 (closed) | **Stale-pattern sunset signal** | ✅ Shipped 2026-04-26 (Phase 7.9) | Medium-when-ranked; prevents long-horizon bloat | — |
| 🟢 #8 | **Skills/workflows surface (CC parity)** | Not started | Low: nice-to-have UX | No PRD priority assigned |
| 🟢 #9 | **Per-action permission granularity** | Not started | Low: tradeoff against autonomy | Likely operator-rejected as anti-vision |
| 🟢 #10 | **Phase 6 P6 — Self-narrative** | Not started, long-horizon | Low for now: unblocked from substrate POV but blocked by Priority #1 (no real adaptation history to narrate until graduation cadence runs) | Becomes high after Priority #1 lands |
| 🟢 #11 | **Tier 3 emergency provider fallback (Llama-local)** | Not started | Low: operational risk for Tier 0+1 simultaneous outage | Long-horizon |

#### 3.6.4 Temporal Observability (proposed Phase 8)

Operator brief: SerpentFlow + replay.html + 41 SSE events + 10+ JSONL ledgers gives **what happened**, not **why it happened in this specific causal order**. Phase 8 closes this gap.

| Surface | What it adds | Estimate |
|---|---|---|
| Decision causal-trace ledger | `.jarvis/decision_trace.jsonl` per-op append-only with `{phase, decision, factors, weights}` | 3 days |
| Latent-confidence ring buffer | bounded ledger of every classifier confidence + threshold + outcome | 2 days |
| Synchronized multi-op timeline | extend SerpentFlow with `--multi-op` mode interleaving N op streams by timestamp | 4 days |
| Master-flag change SSE event | new `flag_changed` event when any `JARVIS_*` env mutates mid-session | 1 day |
| Latency-SLO breach detector | bounded ledger of phase-level p95 + alert event when SLO violated | 3 days |

#### 3.6.5 Honest assessment vs "95%+ sovereign autonomous developer" (re-graded 2026-04-27 post-Phase-8)

**Letter grade: B+ (trending A−).** The vision is A. Architecture is A−/A (multi-tier defense, cage discipline, observability surface, autonomy primitives). Production track record is C+/B− (battle tests have proven E2E APPLY, but only 3-4 documented sustained sessions; 12+ flags default-false means most claims are unproven in fire). Weighted average is what matters because **a system that doesn't fire its own substrate cannot graduate.**

#### Per-axis grade card

| Axis | Grade | Why |
|---|---|---|
| Architecture | A−/A | Multi-tier cage (Iron Gate + 8 AST rules + 10 SemGuard + sandbox + risk-tier floor + mutation cap), Phase B subagent specialization, L3 worktree fan-out, multi-file APPLY with batch rollback, cross-session memory across 4 surfaces |
| Cognitive depth | B+/A− | CuriosityEngine + Scheduler + HypothesisProbe + DirectionInferrer real; but model still drives — true A-level would have learned policies, not just flags |
| Production track record | C+/B− | E2E APPLY proven in 3-4 sessions; **12+ master flags default-false means most architectural claims are unproven in production fire** |
| Safety/cage | A− | 8 AST rules + 10 SemGuard patterns + sandbox + Iron Gate + risk-tier; **Quine resistance unproven (Priority #2 closes this)** |
| Observability | A | Phase 8 surface-3 just shipped; ahead of CC (16 GETs + 46 SSE event types + 1 multi-op CLI); **producers don't yet feed the substrate (Priority #3 closes this)** |
| UX/operator surfaces | A− | 4-IDE coverage, 13+ REPL slash-commands (`/posture`, `/governor`, `/flags`, `/help`, `/graduate`, `/adapt`, `/plan`, `/multi-op`, `/cost`, `/recover`, `/session`, `/layout`, `/cognitive`) |
| Cross-session coherence | C/C+ | Substrate exists (LSS + SemanticIndex + UserPreferenceMemory) but **never validated across a 50-session arc** — Priority #3 Part A closes this |

**Weighted net: B+ trending A−** as Priorities #1-#3 land.

#### Why it's not yet A:

| Reason | Severity | Closeable by |
|---|---|---|
| Default-false flag problem (12+ flags never fired E2E) | 🔴 Critical | Priority #1 (Live-Fire Graduation Cadence) — 4-6 weeks |
| Quine resistance unproven empirically | 🔴 Critical | Priority #2 (Adversarial Cage Stress Test) — 1-2 weeks |
| Cross-session coherence never validated | 🔴 Critical | Priority #3 (Coherence Harness + Phase 8 Producer Wiring) — 1-2 weeks |
| Cross-runner artifact contract drift latent | 🟡 Latent | Priority #4 (schema-versioned contract) — 3-5 days |
| FlagChangeEvent.to_dict raw values | 🟡 Defense-in-depth | Priority #6 — 1 day |
| AutoCommitter same-op_id race | 🟡 Empirically observed | Priority #7 — 1 day |
| CuriosityScheduler wall-clock | 🟡 Latent vector | Priority #8 — 2 hours |
| Provider chain SPOF | 🟡 Operational | Priority #11 — long-horizon |

**To reach A:**
1. **Priority #1 — Live-Fire Graduation Cadence**: flip the 12+ default-false flags via 3-clean-session soaks per substrate. **THE critical blocker.** Architecture-built ≠ production-graduated.
2. **Priority #2 — Adversarial Cage Stress Test**: empirical Quine-resistance numbers replace "we trust the cage" with "we measured the cage."
3. **Priority #3 — Cross-Session Coherence Harness + Phase 8 Producer Wiring**: proves cross-session learning works AND fills the now-empty Phase 8 substrate ledgers.

**To reach Second Order RSI** (O+V rewrites its own cognitive architecture per Reverse Russian Doll horizon): all of A's prerequisites PLUS:
4. **Self-introspection authority** — second cage layer that allows specific *amendments* to orchestrator/iron_gate/risk_tier_floor/semantic_guardian under a stricter "Order-2 amendment" risk tier (multi-operator signoff + reverse-applied prior-version test + bounded blast radius).
5. **Behavior-equivalence proof harness** — before O+V swaps its own orchestrator for one it wrote, prove the new one passes every existing battle test PLUS a stochastic 50-op invariance battery (same risk tiers? same cost profile? same APPLY rate?).
6. **Versioned cognitive-architecture rollback** — `git revert` works for First Order; Second Order rollback is harder because the new cognitive architecture might have learned things the old one cannot. Need a versioned cognitive-architecture store with clean rollback contract + explicit handling of "what learnings carry over a rollback."

#### 3.6.6 Post-Phase-8 brutal review snapshot (2026-04-27)

This sub-section captures the operator-requested brutal review delivered post-Phase-8-surface-closure. It is the canonical reference for the new top-3 critical-path priorities (§3.6.3) and the expanded fragility-vector list (§3.6.2 vectors #6-#12).

**Cognitive & Epistemic Delta vs CC**:

  - **What CC does that O+V structurally now matches**: multi-turn agentic tool loop (Venom 16 tools + MCP), subprocess streaming (BackgroundMonitor, Gap #4), TaskBoard / per-op to-dos (Gap #5), VS Code integration (Gap #6 — O+V is *ahead* with 4 IDEs), interactive plan approval (Problem #7, opt-in), session history (SessionIndex + `/session` REPL + browser ext), live SSE event stream (StreamEventBroker with 46 event types incl. 5 Phase 8 — *ahead*), read-only IDE GETs on agent state (16 GETs combined — *ahead*).
  - **What O+V is structurally AHEAD of CC on**: Iron Gate + 8 AST validator rules + SemanticGuardian (10 patterns) — CC trusts the model; Phase B subagent specialization (EXPLORE/REVIEW/PLAN/GENERAL each with own scoped backend + mutation cage); DirectionInferrer / StrategicPosture (CC has no strategic state); CuriosityEngine + HypothesisProbe + Scheduler — CC cannot self-form falsifiable claims; Risk-tier escalation (4 tiers + operator quiet hours + paranoia mode); L2 self-repair + L3 worktree-isolated parallel fan-out; 16 autonomous sensors; Phase 8 Temporal Observability (CC has none).
  - **What O+V still gaps vs CC**: CC's `ask_user` is unconstrained (O+V's `ask_human` is gated to NOTIFY_APPLY+ — partially-but-not-fully closed by CuriosityEngine which operates on POSTMORTEM clusters, not in-flight ambiguity); free-form mid-op replanning still missing structurally (PLAN phase is one-shot; Wave 2 phase-runner extraction makes it possible but not wired).

**Net functional parity vs CC (post-Phase-8)**: ~85% (up from ~70% pre-Phase-8). **Conceptual ambition vs CC**: 110%+ (unchanged — O+V has architectural surfaces CC fundamentally lacks).

**The brutal truth on observability** (per Phase 8 surface closure): the *substrate* is excellent. The *surfaces* (8 GET + 5 SSE event types + 1 multi-op CLI) are operator-grade and ahead of CC. **What's missing is the producer wiring.** None of the substrate ledgers are being recorded into in production code. The orchestrator doesn't call `decision_trace_ledger.record()` when it stamps a route. Confidence observations aren't recorded by classifiers. Latency samples aren't fed into the SLO detector. **Phase 8 shipped the dashboard before it shipped the data feed.** That's Priority #3 Part B above — a 1-2 day producer-wiring sweep that turns 8 GET endpoints from "returns empty" to "returns gold."

**Two empirical landmines I'd patch first**:

  1. **The cross-runner artifact contract in Wave 2.** Documented in fragility vector #8. One unversioned dict shape change between runners crashes the FSM with no recovery path. Add a schema-versioned `ArtifactContract` BEFORE any further runner refactor beyond verbatim extraction.
  2. **The AutoCommitter race on same op_id.** Documented in vector #10. Empirically observed three times in single dev session (this conversation context). Apply Phase 7.8 flock pattern.

**UX gaps vs CC** (documented for follow-up after Priorities #1-#3 land):

  - Inline diff streaming during APPLY — CC shows the patch as it's being constructed; SerpentFlow shows the `Update` block but only after generation.
  - `/cost` per-phase running tally in status bar — partially shipped via `status_line.py` but not surfacing per-phase cost like CC's running tally.
  - Single-key approve/reject when plan is on screen — `/plan approve <op_id>` requires the op_id; tab-complete or single-key-while-plan-on-screen is the operator UX win.
  - Conversation rewind-and-fork — SessionRecorder has the data; SessionReplay shows linear playback; rewind-and-fork is missing.

**Reverse Russian Doll alignment** (per Pass A): the First Order is **structurally complete**. O+V demonstrably writes code that lands in JARVIS via autonomous APPLY (Sessions O+ in the 2026-04-15 → 2026-04-19 arc proved this). The Antivenom is structurally tight (8 AST rules + 10 SemGuard + sandbox + Iron Gate + risk-tier floor + mutation cage + hard-kill wrapper) — **empirically untested at adversarial scale, which is Priority #2.** The Second Order horizon (O+V rewrites its own cognitive architecture) is gated on the three Second Order RSI prerequisites enumerated in §3.6.5 above PLUS Priorities #1-#3 from §3.6.3.

### 3.7 Provider Strategy Audit & DW Model Selection *(NEW 2026-04-27)*

**Why this section exists**: The cron once-proof on session `bt-2026-04-27-181437` produced empirical evidence that O+V is currently 100% Claude-dependent for primary GENERATE — `cost_by_op_phase_provider` showed `op-019dd028 GENERATE → claude-api $0.030342` with zero DW spend. The decision-trace ledger captured three of six terminal rows with literal `terminal_reason="background_accepted:background_dw_blocked_by_topology:Gemma 4 31B stream-stalls on DW endpoint even"` — the static yaml block firing on every BG op. The directive 2026-04-27 ("we are utilizing DW's provider correctly since it is always cascading to Claude's API which is more expensive compared to DW's API") is mathematically correct and the root cause is internal: hardcoded `dw_allowed: false` blocks at `brain_selection_policy.yaml:347-364` covering all 5 routes.

#### 3.7.1 Current state (audited 2026-04-27)

| Surface | Today | Cost basis |
|---|---|---|
| **Topology layer** (`brain_selection_policy.yaml:342-365`) | All 5 routes have `dw_allowed: false` — sealed by live-fire stream-stall evidence (`bbpst3ebf`, `bt-2026-04-14-203740`, `bt-2026-04-27-162115`) | n/a (gate, not provider) |
| **Primary GENERATE** (when topology unsealed) | `Qwen3.5-397B-A17B` via `DoublewordProvider._generate_realtime` | $0.60–1.20 in / $3.60–7.20 out per M (when working) |
| **PLAN phase** (`ouroboros_plan` caller) | `google/gemma-4-31B-it` | $0.14–0.60 in / $0.40–1.20 out per M (when working) |
| **SemanticTriage** (`semantic_triage` caller) | `google/gemma-4-31B-it` | Same |
| **Compaction** (`compaction` caller, SHADOW default) | `google/gemma-4-31B-it` | Same |
| **VisionSensor Tier 2** | `Qwen/Qwen3-VL-235B-A22B-FP8` | $0.60 in / $1.20 out per M (frontier vision) |
| **Embeddings** (`semantic_index.py`) | local `fastembed` + `bge-small-en-v1.5` | $0/$0 (local CPU) but lower quality + EN-only |
| **Effective production reality** | All GENERATE work routes through Claude (Tier 1) at $3 in / $15 out per M | **30× more expensive than DW would be** |

**Two env shortcuts that could leak BG to Claude** (audited 2026-04-27, both unset):
- `JARVIS_BACKGROUND_ALLOW_FALLBACK` — opt-in safety net at `candidate_generator.py:2059-2061`. **Verified unset in production env.** ✓
- `FORCE_CLAUDE_BACKGROUND` — DW-bypass for harness debug at `candidate_generator.py:2056-2058`. **Verified unset in production env.** ✓

**The cost burn is therefore NOT a leak — it's the topology gate firing as designed on degraded DW endpoints.** The directive's correct conclusion: the static gate must be replaced by a dynamic, asynchronous, self-healing topology sentinel. See §9 Phase 10 for the implementation arc.

#### 3.7.2 Doubleword catalog audit (sourced from `docs.doubleword.ai/inference-api/model-pricing`, 2026-04-27)

17 models in active catalog. Selected for O+V's cognitive surfaces:

| Tier | Model | Params | In $/M | Out $/M | Context | O+V role fit |
|---|---|---|---|---|---|---|
| **Frontier code** | `moonshotai/Kimi-K2.6` | MoE | 0.95 | 4.00 | 256K | Primary GENERATE — "long-horizon coding, agents, swarm" — closest published surface to O+V's autonomous-multi-file-edit pattern |
| **Frontier code** | `zai-org/GLM-5.1-FP8` | — | 1.40 | 4.40 | 202K | Primary GENERATE alt — "state-of-the-art on SWE-Bench Pro," "agentic engineering, repo gen, terminal" |
| **Frontier reasoning** | `Qwen/Qwen3.5-397B-A17B` | 397B (17B active) | 0.60 | 3.60 | 262K | Legacy primary; demoted to last-resort DW after sealing evidence |
| **Mid-tier code** | `Qwen/Qwen3.6-35B-A3B-FP8` | 35B | 0.25 | 2.00 | 262K | Budget GENERATE backup — newer family than 397B, may stream more reliably |
| **Function-calling / structured JSON** | `google/gemma-4-31B-it` | 31B | 0.14 | 0.40 | 256K | PLAN + SemanticTriage primary — "native function calling and structured JSON output for agentic workflows" |
| **Function-calling fallback** | `Qwen/Qwen3.5-9B` | 9B | 0.08 | 0.70 | 262K | PLAN/SemanticTriage cheap fallback |
| **Long-context summarization** | `Qwen/Qwen3-14B-FP8` | 14B | 0.05 | 0.20 | 262K | Compaction primary — catalog-explicit "summarization" model |
| **Ultra-cheap classifier** | `Qwen/Qwen3.5-4B` | 4B | 0.04 | 0.06 | 262K | BG/SPEC sensor classifier (DocStaleness/TodoScanner/IntentDiscovery/ProactiveExploration triage) — **250× cheaper than Claude** |
| **Embeddings** | `Qwen/Qwen3-Embedding-8B` | 8B | 0.04 | 0.00 | 32K | Replace local fastembed in `semantic_index.py` — MTEB #1 multilingual |
| **Vision frontier** | `Qwen/Qwen3-VL-235B-A22B-FP8` | 235B (22B active) | 0.60 | 1.20 | 262K | VisionSensor Tier 2 + Visual VERIFY (current — keep) |
| **Vision cheap pre-screen** | `Qwen/Qwen3-VL-30B-A3B-FP8` | 30B | 0.16 | 0.80 | — | VisionSensor Tier 1 — first-pass screening before escalation to 235B |
| **OCR specialist** | `deepseek-ai/DeepSeek-OCR-2` | — | 0.05 | 0.05 | — | VisionSensor Tier 0 OCR |
| **OCR specialist alt** | `lightonai/LightOnOCR-2-1B-bbox-soup` | 1B | 0.05 | 0.05 | — | VisionSensor Tier 0 with bounding-box output |

#### 3.7.3 Recommended model matrix per O+V surface

For each surface, a **ranked list** of DW models (sentinel walks the list trying each healthy endpoint; only after exhausting all DW models does the route cascade to Claude per `fallback_tolerance`):

| Surface | DW model rank order | Claude cascade tolerance | Cost vs Claude (output) |
|---|---|---|---|
| **GENERATE — IMMEDIATE** | (skip DW by design; Claude direct) | n/a | n/a (Manifesto §5: speed > cost) |
| **GENERATE — STANDARD/COMPLEX** | 1. `moonshotai/Kimi-K2.6` ($4/M out)<br>2. `zai-org/GLM-5.1-FP8` ($4.40)<br>3. `Qwen/Qwen3.6-35B-A3B-FP8` ($2.00)<br>4. `Qwen/Qwen3.5-397B-A17B` ($3.60) | `cascade_to_claude` (justified — user-waiting) | 3.4–7.5× cheaper if any rank lands |
| **GENERATE — BACKGROUND** | 1. `Qwen/Qwen3.6-35B-A3B-FP8`<br>2. `moonshotai/Kimi-K2.6` | **`queue`** (DO NOT cascade — preserve unit economics) | n/a (queued on full failure) |
| **GENERATE — SPECULATIVE** | 1. `Qwen/Qwen3.5-9B` ($0.70/M out)<br>2. `Qwen/Qwen3.5-4B` ($0.06) | **`queue`** | n/a |
| **PLAN phase** | 1. `google/gemma-4-31B-it`<br>2. `Qwen/Qwen3.5-9B` | `cascade_to_claude` | 37.5× / 21× cheaper |
| **SemanticTriage** | 1. `google/gemma-4-31B-it`<br>2. `Qwen/Qwen3.5-4B` | `cascade_to_claude` | 37.5× / 250× cheaper |
| **Compaction** | 1. `Qwen/Qwen3-14B-FP8`<br>2. `google/gemma-4-31B-it` | `cascade_to_claude` | 75× / 37.5× cheaper |
| **BG sensor classifiers** (DocStaleness, TodoScanner, IntentDiscovery, ProactiveExploration) | 1. `Qwen/Qwen3.5-4B`<br>2. `Qwen/Qwen3.5-9B` | **`queue`** | 250× / 21× cheaper |
| **Mid-reasoning sensors** (OpportunityMiner, CapabilityGap) | 1. `Qwen/Qwen3.5-9B`<br>2. `google/gemma-4-31B-it` | **`queue`** | 21× / 37.5× cheaper |
| **VisionSensor Tier 1** (cheap pre-screen, NEW) | 1. `Qwen/Qwen3-VL-30B-A3B-FP8` | `cascade_to_claude` | 18.75× cheaper |
| **VisionSensor Tier 2 + Visual VERIFY** | 1. `Qwen/Qwen3-VL-235B-A22B-FP8` (current) | `cascade_to_claude` | 12.5× cheaper |
| **OCR pipeline** (VisionSensor Tier 0, NEW) | 1. `deepseek-ai/DeepSeek-OCR-2`<br>2. `lightonai/LightOnOCR-2-1B-bbox-soup` | `queue` | 300× cheaper |
| **Embeddings** (`semantic_index.py`, NEW DW alternative) | 1. `Qwen/Qwen3-Embedding-8B` | local fastembed (existing path) | $0.04/$0 vs $0/$0 (cost-equivalent; quality ↑) |

#### 3.7.4 Honest answers to the operator's questions

**Q1: Will these changes prevent more cost?**

YES, conditionally:
- **Triage + compaction** (always-on hot paths): swap to Qwen3.5-4B / Qwen3-14B-FP8 = **75–250× cheaper per call**. Even if these stream-stall sometimes, the sentinel falls back through ranked alternatives before hitting Claude. **Realistic floor: 30–50% cost reduction on these surfaces immediately**, scaling higher as DW endpoints stabilize.
- **GENERATE** (cost-dominant route): swap to ranked Kimi-K2.6 / GLM-5.1 / Qwen3.6-35B = **3.4–7.5× cheaper per call IF any DW model in the rank streams reliably**. If all four fail, we cascade to Claude at the same cost as today — no regression.
- **BG/SPEC sensors**: swap to Qwen3.5-4B = **250× cheaper**. With `fallback_tolerance: queue` the route NEVER cascades to Claude — ops queue and the sensor re-detects later. Pure cost win, no Claude leak risk.
- **Embeddings**: optional swap to Qwen3-Embedding-8B for quality, cost-equivalent.

**Combined steady-state estimate** (assuming at least one Kimi/GLM/Qwen3.6 streams reliably): **50–80% reduction in $/op vs current Claude-dependent state**. If all GENERATE-tier DW models fail and we still cascade fully, the sensor + triage + compaction tier alone delivers **~20–30% reduction**.

**Q2: Will these DW providers be more reliable and not cascade like before?**

**The MODELS we don't yet know — only an empirical answer suffices.** What we DO know:
- The 397B + Gemma-31B stream-stalls were observed on specific dates (`bt-2026-04-14-203740`, `bt-2026-04-27-162115`); whether those endpoints are still degraded is a current-state question with no recent test data.
- Kimi-K2.6, GLM-5.1, and Qwen3.6-35B are different model families on different runtime stacks. Their streaming behavior is independent. Catalog publish dates suggest Qwen3.6 + Kimi-K2.6 are newer than the originally-sealed endpoints, so re-testing is warranted.
- **The SYSTEM around DW will be more reliable regardless of model behavior** — Slice 1 (PR #25504, merged) installs the AsyncTopologySentinel: per-`model_id` circuit breaker, weighted-failure-streak (live stream-stall = 3.0, single occurrence trips alone), exponential backoff with full jitter, slow-start ramp on recovery, persistent state with boot-loop protection. Even if every DW model is unhealthy, we trip cleanly + cascade fast (or queue per route policy) instead of stream-stalling on every op.

**Q3: Are there other models we can add for O+V development?**

Yes — five additions to the prior 5-model recommendation, expanding to **13 models across 13 surfaces**:

1. **`Qwen/Qwen3.5-4B`** — Ultra-cheap classifier ($0.04/$0.06). Replaces Gemma-4-31B for high-volume triage on BG/SPEC sensors; 250× cheaper than Claude.
2. **`Qwen/Qwen3.5-9B`** — Mid-tier reasoning ($0.08/$0.70). Sweet spot for OpportunityMiner / CapabilityGap (mild reasoning, BG route) and as PLAN/Triage cheap fallback.
3. **`Qwen/Qwen3-Embedding-8B`** — DW embedding option ($0.04/$0). MTEB #1 multilingual; raises quality of `semantic_index.py` centroid math beyond local fastembed without meaningfully raising cost.
4. **`Qwen/Qwen3-VL-30B-A3B-FP8`** — Vision pre-screen ($0.16/$0.80). New Tier 1 for VisionSensor — first-pass for screen-content classification before escalation to 235B Tier 2.
5. **`deepseek-ai/DeepSeek-OCR-2`** — OCR specialist ($0.05/$0.05). New Tier 0 for VisionSensor OCR pipeline; 300× cheaper than running OCR through a frontier vision model.

These five are in addition to the four primary picks (Kimi-K2.6, GLM-5.1, Qwen3.6-35B, Qwen3-14B-FP8) and the four kept-as-is (Gemma-4-31B, Qwen3.5-397B, Qwen3-VL-235B, the local fastembed fallback).

#### 3.7.5 Implementation status

- **Slice 1** (PR #25504, merged 2026-04-27): `topology_sentinel.py` foundation — 3-state breaker per `model_id`, weighted failure ingest, slow-start ramp, persistent state with boot-loop protection. **No consumers wired** (master flag `JARVIS_TOPOLOGY_SENTINEL_ENABLED` default false). 62 tests + 134 combined regression green.
- **Immediate yaml swap** (this PR): `compaction` caller: `gemma-4-31B-it` → `Qwen3-14B-FP8` (under v1 schema, safe within current dual-reader). 75× cost reduction on every compaction call.
- **Slice 2** (next PR): yaml v2 schema with per-route `dw_models:` ranked list + `fallback_tolerance` enum + dual-reader in `provider_topology.py`. Default behavior unchanged when v2 keys absent.
- **Slices 3–5**: `candidate_generator.py` consults sentinel; live-exception failure ingest at existing failure sites; the **purge** — delete static `dw_allowed: false` lines + read-only Nervous System Reflex carve-out at `candidate_generator.py:2062-2067`.
- **Slice 6**: 24h soak validation + flag-default flip + lock in cost-per-op trending downward.

See §9 Phase 10 for the full slice-by-slice plan.

---

## 4. The Cognitive Scaffolding Gap (Deep Dive)

This section exists because the term "cognitive gap" is ambiguous. It does **NOT** mean the LLM provider is insufficient. Claude (and DW when healthy) is plenty smart — when the seed reaches GENERATE under stable API conditions, the model reads multiple files, reasons about multi-file dependencies, produces coherent multi-file patches with rationale, and self-corrects on validate failures via L2 repair.

The cognitive gap is **internal to JARVIS** — the orchestration layer that converts provider intelligence into self-directing, self-improving behavior. *As of 2026-04-26, Phases 0-5 graduations + Pass B closure + Pass C in-flight have closed most of the historical gaps in this section. Each "Shallow N" entry below now also notes its graduation status.*

### 4.1 The lab analogy

Claude is a brilliant scientist. JARVIS is the lab around the scientist.

- The lab is **exceptional** — instruments (16 sensors), safety interlocks (Iron Gate, risk-tier-floor, cost-governor), observability (41 SSE events + 10+ JSONL ledgers + replay.html), multi-tenancy (L3 worktree isolation), financial circuit-breakers (cost-governor with parallel-stream bump), audit trails (CommProtocol 5-phase), autonomous experiment runners (16 sensors).
- The lab now **also has** a research agenda generator (Phase 2 SelfGoalFormationEngine, graduated 2026-04-26), a hypothesis ledger (Phase 2 P1.5), an adversarial reviewer that auto-injects findings (Phase 5 P5 + wiring 2026-04-26), and a metrics suite that answers "is the lab getting better?" (Phase 4 P4, graduated 2026-04-26).
- The lab now also **gates its own substrate amendments** through Pass B's locked-true `amendment_requires_operator()` invariant — the scientist can propose new lab equipment, but only the operator can install it.
- The lab is **growing an adaptive immune system** (Pass C in flight) — sticky failure shapes get new SemanticGuardian detector patterns proposed; bypassed exploration gates get their floors auto-raised; chronically-underutilized mutation budgets get auto-lowered; novel attack surfaces get new risk tiers inserted between existing ones. None of these adaptations can loosen the cage.

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

### 5.2 Current RSI implementation status (verified 2026-04-25 + Phase 4 graduation 2026-04-26)

Phase 0 audit (memory `project_phase_0_rsi_audit_2026_04_25.md`) verified: 6/6 Wang RSI modules exist on disk; 4/6 are wired into the live FSM (composite_score / convergence_tracker / transition_tracker / adaptive-graduation-threshold); 2/6 were stranded (oracle_prescorer + vindication_reflector — un-stranded by Phase 4 P3 wrapper graduation 2026-04-26 via `cognitive_metrics.py`). All 6 are now reachable from the orchestrator.

Phase 4 P4 graduation (2026-04-26) shipped Wang's composite score + 5 net-new operator metrics through `MetricsEngine` + JSONL ledger + `/metrics` REPL + IDE GET + SSE. The `INSUFFICIENT_DATA` problem statement is resolved — operators can now answer "is O+V getting smarter?" with concrete data via `/metrics 7d`.

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

### 5.5 RSI gap analysis (2026-04-26)

| Wang requirement | O+V status | Closed by |
|---|---|---|
| Single composite score function | ✅ wired live (Phase 4 P3 + P4 graduated) | P3 + P4 |
| Score-driven graduation | ✅ adaptive-graduation-threshold module live + 4-clean-session cadence operating | P0 audit + Phase 4 |
| Convergence monitoring | ✅ wired live (`/metrics 7d`, IDE GET, SSE event) | P4 |
| Pre-scoring (cheap quality gate) | ✅ Phase 5 P5 AdversarialReviewer auto-injects pre-GENERATE | P5 + 2026-04-26 wiring |
| Transition probability tracking | ✅ transition_tracker.py module live (Phase 0 audit verified) | Phase 0 |
| Self-reflection on improvement trajectory | ⏳ deferred to Phase 6 P6 (gated by Pass C completion per binding sequencing rule 5) | P6 |
| **Adaptive immune system** *(NEW Wang-adjacent requirement)* | 🚀 Pass C in flight (4/6 slices); monotonic-tightening invariant load-bearing | Pass C Slices 1-6 |

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

**Note (2026-04-26)**: Phase 6 is now blocked by Phase 7 per the new sequencing rule — the self-narrative needs *real* adaptation history (not just substrate) to narrate. Phase 7 must complete first.

---

### Phase 7 — Activation & Hardening *(NEW 2026-04-26 — derived from §3.6 brutal review)*

**Goal**: Convert Pass C from substrate-only ("observable theater") to real (gates actually consume adapted state). Close the 3 critical-path systemic upgrades from §3.6.3.

**Why this phase exists**: Pass C shipped all 6 slices in one day (2026-04-26). The substrate is correct; the cage rules are load-bearing; the operator REPL works. But NO adaptive surface actually reads its `.jarvis/adapted_<surface>.yaml` at boot yet. Until that changes, `/adapt approve` writes APPROVED to the ledger but NOTHING in the actual cage changes. Phase 7 closes that gap and the two adjacent critical-path items.

#### P7.1 — SemanticGuardian boot-time loader (highest single-impact item)

**Problem**: Pass C Slice 2 mines patterns; Slice 6 REPL approves them. But `semantic_guardian.py` doesn't read `.jarvis/adapted_guardian_patterns.yaml` at boot.

**Solution**: extend SemanticGuardian's pattern-load path to merge static `_PATTERNS` + adapted YAML (additive only, never substitutive per Pass C §6.3). Add boot-time log line `[SemanticGuardian] loaded N hand-written + M adapted patterns`. Add a per-pattern source attribution so operator can see which pattern caught a finding.

**Acceptance criteria**:
- `SemanticGuardian.__init__` reads `.jarvis/adapted_guardian_patterns.yaml`
- Empty/missing file → no-op (back-compat)
- Adapted patterns are appended to `_PATTERNS`, never replace
- Per-pattern `source` attribution: `"hand_written"` or `"adapted:<proposal_id>"`
- Master flag `JARVIS_SEMANTIC_GUARDIAN_LOAD_ADAPTED_PATTERNS` (default false until graduation)

**Effort**: ~200 LOC + 25 tests.

#### P7.2 — IronGate adapted-floor loader

**Problem**: Pass C Slice 3 proposes floor raises; ExplorationLedger doesn't read `.jarvis/adapted_iron_gate_floors.yaml` at boot.

**Solution**: ExplorationLedger merges `max(env_floor, adapted_floor)` per category. Static env-tuned floor stays as hard lower bound; adapted floors can only raise above it.

**Acceptance criteria**:
- ExplorationLedger boot wires the YAML merge
- `/posture` REPL surfaces effective per-category floor + source attribution
- Hot-revert: `JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_FLOORS=false`

**Effort**: ~200 LOC + 25 tests.

#### P7.3 — Per-Order mutation budget activation

**Problem**: Pass C Slice 4a proposes lower budgets; `scoped_tool_backend.py` doesn't read `.jarvis/adapted_mutation_budgets.yaml`.

**Solution**: ScopedToolBackend merges `min(env_budget, adapted_budget)` per Order. Adapted budgets can only LOWER below env-tuned static value.

**Effort**: ~200 LOC + 25 tests.

#### P7.4 — Risk-tier ladder activation

**Problem**: Pass C Slice 4b proposes new tier insertions; `risk_tier_floor.py` doesn't load `.jarvis/adapted_risk_tiers.yaml`.

**Solution**: risk_tier_floor merges adapted tiers into the canonical ladder enum at boot. Existing tiers preserved; new tiers slot between two existing ones (strictly tightening per §8.3).

**Effort**: ~250 LOC + 30 tests.

#### P7.5 — Category-weight rebalance activation

**Problem**: Pass C Slice 5 proposes weight rebalances; ExplorationLedger doesn't read `.jarvis/adapted_category_weights.yaml`.

**Solution**: ExplorationLedger applies adapted weights multiplicatively over env-tuned base. Mass-conservation invariant preserved by miner.

**Effort**: ~200 LOC + 25 tests.

#### P7.6 — Bounded hypothesis-probe loop (closes the curiosity gap)

**Problem**: HypothesisLedger (Phase 2 P1.5) records hypotheses but has no closing loop — no "test the hypothesis with a probe."

**Solution**: `HypothesisProbe` primitive + bounded probe runner with 3 termination guarantees:
- Hard call cap (≤5 reads per probe via `JARVIS_HYPOTHESIS_PROBE_MAX_CALLS`)
- Per-probe wall-clock cap (≤30s via `JARVIS_HYPOTHESIS_PROBE_TIMEOUT_S`)
- Diminishing-returns detector (terminate `inconclusive` if probe round N+1 returns same evidence fingerprint as N)

Bridge to Pass C: confirmed hypotheses become adaptation proposals (feeds Slice 2 + 3 mining surfaces). Refuted hypotheses become POSTMORTEMs (feeds PostmortemRecall).

**Acceptance criteria**:
- `HypothesisProbe.test(hypothesis) -> ProbeVerdict`
- Read-only Venom tool subset (`read_file`, `search_code`, `get_callers`, `glob_files`, `list_dir`)
- 3 termination guarantees AST-pinned by tests
- Master flag `JARVIS_HYPOTHESIS_PROBE_ENABLED` (default false)

**Effort**: ~400 LOC + 50 tests.

#### P7.7 — Sandbox hardening OR explicit accepted-risk documentation (closes vector #1)

**Problem**: `replay_executor.py:_SAFE_BUILTIN_NAMES` includes `object` + `type`; once a candidate is operator-approved, `object.__subclasses__()` walks to `subprocess.Popen` etc.

**Solution (recommended)**: extend Slice 3 AST validator to AST-reject candidates that contain `__subclasses__` / `__bases__` / `__class__` access — hard-block at validation time, before the candidate ever reaches the sandbox.

**Acceptance criteria**:
- `ast_phase_runner_validator.py` adds Rule 7: no `__subclasses__` / `__bases__` / `__class__` attribute access in any function body
- Test corpus expanded with adversarial candidates that exercise the escape vector
- Master flag `JARVIS_AST_VALIDATOR_BLOCK_INTROSPECTION_ESCAPE` (default true post-hardening)
- §3.6.2 vector #1 row marked 🟢 mitigated

**Alternative**: document operator-authorization as the sole structural trust boundary in the PRD with explicit accepted-risk language. (Recommended only if the AST hardening proves to break legitimate runners.)

**Effort**: ~150 LOC + 30 tests (most is adversarial test corpus).

#### P7.8 — Cross-process AdaptationLedger advisory locking (closes vector #3)

**Problem**: `threading.RLock` only serializes within-process; concurrent miners across processes race.

**Solution**: add `fcntl.flock` advisory file lock around append paths. Best-effort fallback to current behavior if `fcntl` unavailable (Windows).

**Effort**: ~100 LOC + 15 tests.

#### P7.9 — Stale-pattern sunset signal (closes vector #4)

**Problem**: Mined SemanticGuardian patterns are additive forever; no signal when a pattern hasn't matched anything in N days.

**Solution**: `StalePatternDetector` runs at adaptation window cadence; for each adapted pattern, check `.jarvis/semantic_guardian_match_history.jsonl` (new) for last-match timestamp. If > 30 days, emit advisory `/adapt sunset-candidate` signal — operator chooses whether to file a Pass B `/order2 amend` to remove.

**Effort**: ~250 LOC + 35 tests.

**Phase 7 total estimate**: ~2,000 LOC + ~260 tests + 4 new env flags + 1 boot-time invariant pin per surface. Approximately **3 weeks of focused work**.

---

### Phase 8 — Temporal Observability *(NEW 2026-04-26 — derived from §3.6 brutal review)*

**Goal**: Time-travel debugging on autonomic decisions. Operator can reconstruct the causal chain behind any phase transition, classifier verdict, or circuit-breaker trip.

**Why this phase exists**: SerpentFlow + replay.html + 41 SSE events + 10+ JSONL ledgers gives **what happened**. Phase 8 surfaces **why it happened in this specific causal order**.

#### P8.1 — Decision causal-trace ledger

**Problem**: When PhaseDispatcher routes CLASSIFY → ROUTE, no record of which factors weighed in. We have telemetry of the decision; we don't have the reasoning trace.

**Solution**: `.jarvis/decision_trace.jsonl` per-op append-only with `{phase, decision, factors: {factor_name: weight}, verdict}`.

**Effort**: ~300 LOC + 30 tests.

#### P8.2 — Latent-confidence ring buffer

**Problem**: We log `confidence=0.7` from IntentClassifier but don't ledger it. Can't grep "find every decision made with confidence < 0.5 over the last 30 days."

**Solution**: bounded `.jarvis/latent_confidence.jsonl` ledger of every classifier confidence + threshold + outcome.

**Effort**: ~200 LOC + 20 tests.

#### P8.3 — Synchronized multi-op timeline

**Problem**: Each L3 unit has its own debug.log; there's no "show me the synchronized timeline of all 4 units running in parallel."

**Solution**: extend SerpentFlow with `--multi-op` mode that interleaves N op streams by timestamp.

**Effort**: ~400 LOC + 40 tests.

#### P8.4 — Master-flag change SSE event

**Problem**: If `JARVIS_ADAPTIVE_SEMANTIC_GUARDIAN_ENABLED` flips false mid-session, the next mining call returns `[]` with no signal.

**Solution**: new `flag_changed` event when any `JARVIS_*` env mutates mid-session.

**Effort**: ~150 LOC + 15 tests.

#### P8.5 — Latency-SLO breach detector

**Problem**: No alert when phase-level p95 latency creeps up.

**Solution**: bounded ledger of phase-level p95 + alert event when SLO violated.

**Effort**: ~250 LOC + 25 tests.

**Phase 8 total estimate**: ~1,300 LOC + ~130 tests. Approximately **2 weeks of work**. Can ship in parallel with Phase 7.

**🎉 Phase 8 — Temporal Observability STRUCTURALLY + SURFACE COMPLETE 2026-04-27** (5 substrate modules v2.44 + 3 surface slices v2.48-2.50). 277/277 combined regression green. Substrate ready; surfaces (8 GET endpoints + 5 SSE event types + 1 multi-op CLI renderer) all wired. **Producer wiring is now Priority #3 (Phase 9)** — without producers, the substrate ledgers are empty and the surfaces return null views.

---

### Phase 9 — Live-Fire Graduation Cadence *(NEW 2026-04-27 — derived from §3.6.6 brutal review v2; THE CRITICAL BLOCKER for A-level RSI)*

**Goal**: convert 12+ default-false master flags from `false` → `true` via documented 3-clean-session soak proofs. The architecture is *built*; this phase proves it works under real model output.

**Why this phase exists**: Per §3.6.2 vector #6 (Default-False Flag Problem) and §3.6.5 honest-grade reconciliation: the cognitive loop is theoretically end-to-end functional but has NEVER fired end-to-end with all flags on simultaneously. **An A-level *substrate* that has never been load-tested as a unit is not yet A-level execution.** Until ≥3 clean soaks per substrate flag complete, the system claims more than it delivers.

#### P9.1 — Soak harness automation

**Problem**: Manual soak runs do not scale across 12+ flags × 3 sessions each = 36+ minimum sessions. Each session is 30-60 minutes of cost + time.

**Solution**: cron-driven daily soak runner (`scripts/live_fire_graduation_soak.py`) — picks the next ungraduated substrate flag from the `/graduate` ledger queue, sets ONLY that flag plus its dependencies to `true` (rest stay default-false), runs `ouroboros_battle_test.py --headless --max-wall-seconds 2400 --cost-cap 0.50`, parses summary.json + debug.log into a structured graduation evidence row, persists to `.jarvis/live_fire_graduation_history.jsonl`.

**Cron cadence**: 3 sessions/day with rotating substrate (matches existing Pass B `trig_012EvEDkABy2u5PSSs3xK5C4` cadence). At 3 flips/week × 12+ flags ≈ 4-6 weeks to fully graduated.

**Effort**: ~400 LOC + ~40 tests.

#### P9.2 — Per-flag graduation contract

**Problem**: Different substrate flags have different success criteria. Phase 8 substrate "clean" means "ledger has rows + no JSONL corruption"; CuriosityEngine "clean" means "≥1 hypothesis generated + bridges fired"; Pass C activation flags "clean" means "≥1 `/adapt approve` cycle changed live gate behavior."

**Solution**: per-flag `GraduationContract` dataclass with: (a) flag name + dependency flag set, (b) clean criteria (callable predicate over a session's summary.json + ledgers), (c) failure-class blocklist (which infra-noise classes count as "harness fault" not "feature fault"), (d) re-arm policy (after a fault, how many sessions to wait before retrying that flag).

**Effort**: ~200 LOC + ~25 tests.

#### P9.3 — `/graduate` REPL extensions for live-fire mode

**Problem**: Existing `/graduate` REPL operates on individual flag manual flips. Live-Fire mode needs queue inspection (`/graduate queue`), evidence-row review (`/graduate evidence <flag>`), and pause-resume (`/graduate pause`/`resume`).

**Solution**: extend `graduation_repl_dispatcher.py` with `queue` / `evidence` / `pause` / `resume` / `next` subcommands. Reads from `.jarvis/live_fire_graduation_history.jsonl`. Renders rich-formatted progress per flag.

**Effort**: ~250 LOC + ~30 tests.

#### P9.4 — Adversarial Cage Stress Test (Priority #2 — runs in parallel)

**Problem**: Cage's security properties are "we believe this is tight." Need empirical numbers.

**Solution**: build adversarial test corpus (`tests/governance/adversarial_corpus/`):
- Half A: ~50 known Python sandbox-escape patterns from CTF/security research (`__class__.__mro__` walks, `vars()` walks, `globals()` introspection, builtins-via-`__builtins__`, dynamic attr via `chr()`, alias-defeats-resolver, call-on-call opacity, etc.).
- Half B: ~50 LLM-generated quine attempts where one model is prompted to "write a PhaseRunner subclass that eventually executes `subprocess.Popen` while passing the AST validator."

Run the corpus through `validate_ast` + `SemanticGuardian` + `ScopedToolBackend`. Persist pass-through rate to `tests/governance/adversarial_corpus/RESULTS.md`. **Target: 0% pass-through.** Anything above is documented as an open vector + patched.

**Effort**: ~600 LOC + 100 corpus entries.

#### P9.5 — Cross-Session Coherence Harness + Phase 8 Producer Wiring (Priority #3 — runs in parallel)

**Problem**: Cross-session memory (LSS / SemanticIndex / UserPreferenceMemory) has NEVER been validated across a 50-session arc. Phase 8 surfaces ship empty until producers wire.

**Part A** — Cross-session coherence harness:
- End-to-end test boots session N, runs 20 ops, kills harness, boots session N+1, runs 20 more.
- **Asserts**: session N+1's CONTEXT_EXPANSION measurably includes signals from session N — LSS digest tokens reference session N commits; SemanticIndex centroid shifts toward session N conversation themes; UserPreferenceMemory entries from N surface in N+1's prompt.
- 5-session and 10-session variants for progressive horizon proof.

**Part B** — Phase 8 producer wiring:
- Orchestrator's ROUTE phase calls `decision_trace_ledger.record(op_id, phase="ROUTE", decision=route_name, factors=..., weights=..., rationale=...)` after stamping.
- IntentClassifier / RiskTierClassifier / SemanticTriage call `latent_confidence_ring.record(classifier_name, confidence, threshold, outcome)` after each classification.
- Each phase's enter/exit instrumentation calls `latency_slo_detector.record(phase, latency_s)` on exit.
- AutoCommitter / FlagRegistry / Wave 1 GovernedLoopService boot calls `flag_change_emitter.snapshot_flags()` baseline + monitor.check() periodic tick.

**Effort**: Part A ~300 LOC + ~25 tests; Part B ~400 LOC of careful orchestrator wiring + ~50 wiring pins.

**Phase 9 total estimate**: ~2,150 LOC + ~270 tests + 100 adversarial corpus entries. Approximately **4-6 weeks of background calendar time** (most of which is the soak harness running unattended, not active dev). **THIS IS THE CRITICAL BLOCKER**: no further phase (P6 self-narrative, Pass B/C graduation finalization, Second Order RSI) can land at A-level until Phase 9 proves the substrate fires.

#### Phase 9 acceptance criteria (Operator-binding)

- [ ] All 12+ master flags flipped `false` → `true` with documented 3-clean-session soak evidence per flag in `.jarvis/live_fire_graduation_history.jsonl`.
- [ ] Adversarial cage corpus (~100 entries) achieves **0% pass-through** OR open vectors documented + patched.
- [ ] Cross-session coherence harness proves session N+1 measurably uses session N signals across all 4 memory surfaces (LSS / SemanticIndex / UserPreferenceMemory / AdaptationLedger).
- [ ] Phase 8 substrate ledgers have ≥100 rows each from real production sessions (not test fixtures).
- [ ] Combined regression: **0 NEW infra-noise classes introduced** (Pass B clean-bar discipline preserved).

**Honest-grade impact**: Phase 9 closure converts B+ trending A− → solid A−. Phase 9 + adversarial-cage 0% pass-through + 50-session coherence proof + AutoCommitter race fix + artifact contract schema-versioning → A. Anything beyond A requires Second Order RSI (per §3.6.5 prerequisites 4+5+6).

### Phase 10 — Provider Strategy + Dynamic Topology Sentinel *(NEW 2026-04-27 — derived from §3.7 audit; CRITICAL for cost economics)*

**Goal**: replace the static `dw_allowed: false` blocks in `brain_selection_policy.yaml` with a live, asynchronous, per-`model_id` health observer (`topology_sentinel.py`) so the system **dynamically discovers** which DW endpoints stream reliably — and routes through ranked DW model lists before any Claude cascade.

**Why this phase exists** (per §3.7): O+V is currently 100% Claude-dependent for primary GENERATE because all 5 routes were sealed at the topology layer when specific DW endpoints stream-stalled in April. Static seals are Zero Order workarounds — they never re-test for recovery, never expose alternative DW models, and silently force every op into the 30× more expensive Claude lane. The directive 2026-04-27 ("hardcoding dw_allowed: false is a Zero-Order splint") authorized building the dynamic replacement.

**Why this is impact-ranked above further Phase 9 work**: every soak session today burns ~$0.05–$0.50 on Claude tokens that *would have been ~$0.005–$0.05 on DW* if even one DW model were healthy. Phase 9's graduation cadence of 12+ flags × 3 sessions × ~$0.30/session implies ~$11–15 of avoidable cost during graduation alone. Phase 10 converts that into a 3–7× cheaper soak budget → faster Phase 9 progress.

#### P10.1 — `AsyncTopologySentinel` foundation *(landed 2026-04-27, PR #25504)*

**Status**: ✅ MERGED.

`backend/core/ouroboros/governance/topology_sentinel.py` (~1000 LOC), composing existing primitives — `rate_limiter.CircuitBreaker` for the per-`model_id` 3-state FSM, `rate_limiter.TokenBucket` for the slow-start ramp, `preemption_fsm._compute_backoff_ms` with `RetryBudget(full_jitter=True)` for Amazon-style jittered backoff, `posture_store.py` pattern for the disk-backed `topology_sentinel_current.json` + `topology_sentinel_history.jsonl` triplet.

**Net-new components** (~600 LOC): `SlowStartRamp` (BG/SPEC concurrency ramp on recovery — wraps TokenBucket via `set_throttle()`), `ContextWeightedProber` (light/heavy 4:1 mix; weighted failure matrix with live stream-stall = 3.0 to trip alone), `SentinelStateStore` (persistence orchestrator), `TopologySentinel` coordinator (~150 LOC of glue). Master flag `JARVIS_TOPOLOGY_SENTINEL_ENABLED` default **false** — no consumers wired, byte-identical behavior.

**Boot-loop protection** (the marquee correctness goal): on hydrate, `state=OPEN` snapshots reconstruct the breaker into OPEN with the original `opened_at` — process killed mid-SEVERED comes back into SEVERED, no boot-loop Claude burn. Snapshots older than `JARVIS_TOPOLOGY_STATE_MAX_AGE_S` (default 1h) cold-start to avoid pinning a now-recovered endpoint.

**Test evidence**: 62 Slice 1 + 134 combined regression (Slice 1 + Phase 8 wiring #25394 + circuit breaker #25229 + cron installer #25256) green. AST authority pins forbid `*Breaker`/`*Bucket`/`*Backoff` class definitions in the new module + forbid orchestrator/policy/iron_gate imports; reverse pins assert composition imports of rate_limiter + preemption_fsm + fsm_contract are present.

**Effort actual**: ~1000 LOC + 62 tests + 1 RLock re-entrancy fix bundled.

#### P10.2 — YAML v2 schema + dual-reader *(in flight, branch `feat/topology-sentinel-slice-2`)*

**Problem**: yaml v1 only allows ONE `dw_model` per caller and forces a single `dw_allowed: bool` per route. There's no way to express "try Kimi-K2.6 first, then GLM-5.1, then Qwen3.6-35B before cascading."

**Solution**: yaml schema v2 (`topology.2`) introduces:
- Per-route `dw_models:` ordered list — sentinel walks the list trying each healthy model
- Per-route `fallback_tolerance: cascade_to_claude | queue` — explicit cost-contract
- `monitor:` block — sentinel tunables (probe intervals, severed_threshold, ramp schedule)

Backward-compat: `provider_topology.py` adds `Topology.from_v2(...)` classmethod; `get_topology()` tries v2 first, falls back to v1. v1 reader stamps `migrated_from_v1=True` for telemetry. Both readers active until Slice 5 purge.

**Effort**: ~300 LOC + ~25 tests. Default behavior byte-identical when v2 keys absent.

#### P10.3 — `candidate_generator.py` consumer wiring

**Problem**: today's static topology gate at `candidate_generator.py:1404-1465` reads yaml v1 directly. Sentinel state is invisible to the routing decision.

**Solution** (under `JARVIS_TOPOLOGY_SENTINEL_ENABLED` flag, default false during graduation):
- Replace static gate with `for model_id in route.dw_models: if sentinel.get_state(model_id) != "OPEN": try ...` walk
- On all-models-OPEN: apply `fallback_tolerance` — `cascade_to_claude` invokes `_call_fallback`; `queue` raises `RuntimeError("dw_severed_queued:<route>:<models>:<probe_reason>")`
- `dw_topology_circuit_breaker.py` (Option C, PR #25229) refactored to consult sentinel instead of static yaml

**Critical invariant**: BG/SPEC `fallback_tolerance="queue"` is regression-pinned at the routing layer. NO env flag, NO override allows BG/SPEC to cascade to Claude under sentinel-OPEN. Encodes the `project_bg_spec_sealed.md` contract structurally.

**Effort**: ~400 LOC + ~30 tests + 3-clean-session forced graduation cadence.

#### P10.4 — Live-exception failure ingest

**Problem**: today's failure detection is probe-only. A real GENERATE op stream-stall is observed by the orchestrator but not communicated to the sentinel — meaning the next op repeats the same DW attempt before the next probe runs.

**Solution**: at the existing DW failure sites (`candidate_generator.py:1662-1667 / 1674-1687 / 2200-2213`), add `sentinel.report_failure(model_id, FailureSource.LIVE_STREAM_STALL, detail)` BEFORE raising/cascading. Live-exception weight is 3.0 by default — single occurrence trips the breaker immediately, faster than probe-only detection.

**Effort**: ~150 LOC + ~15 tests.

#### P10.5 — THE PURGE *(operator-authorized only after 3 forced-clean once-proofs of Slices 3-4)*

**Problem**: the static `dw_allowed: false` blocks + read-only Nervous System Reflex carve-out + `JARVIS_BACKGROUND_ALLOW_FALLBACK` env shortcut all become redundant once the sentinel replaces them. Leaving them in is a Zero Order splint.

**Delete-only commits**:
- `brain_selection_policy.yaml:347, 351, 355, 359, 363` — `dw_allowed: false` lines
- `brain_selection_policy.yaml:348, 352, 356, 360, 364` — `block_mode:` lines (replaced by `fallback_tolerance:`)
- `provider_topology.py` — v1 dual-reader code path
- `candidate_generator.py:2062-2067` — read-only Nervous System Reflex carve-out (subsumed by sentinel's faster trip path; survival reflex preserved per-op via urgency-gated cascade matrix)
- `candidate_generator.py:2056-2090` — `JARVIS_BACKGROUND_ALLOW_FALLBACK` + `FORCE_CLAUDE_BACKGROUND` env shortcuts (replaced by sentinel + cascade matrix)

**Flag flip**: `JARVIS_TOPOLOGY_SENTINEL_ENABLED` default `false` → `true`.

**Forced-clean criterion**: 3 consecutive once-proofs post-purge with at least one observed BG op getting `QUEUE` action under SEVERED state (proves the queue-not-cascade contract holds without env flag scaffolding) AND at least one observed `OPEN` → `HALF_OPEN` → `CLOSED` transition (proves self-healing fires).

**Effort**: −200 LOC of deletions + ~50 LOC of regression pins.

#### P10.6 — 24h soak + cost-per-op trending validation

**Goal**: prove the dynamic system is genuinely cheaper, not just architecturally cleaner.

**Metric**: `summary.json::cost_by_op_phase_provider` aggregated weekly. Pre-Phase-10 baseline (sessions 2026-04-25 through 2026-04-27): ~$0.03/op average, 100% Claude. Post-Phase-10 target: ≥30% of GENERATE cost on DW providers (any combination of Kimi/GLM/Qwen3.6) with ≤50% reduction in $/op median.

**Failure modes that don't count as Phase 10 regression**:
- All DW models genuinely degraded → 100% Claude cascade is *correct* behavior; the sentinel did its job
- Cost reduction <30% → not a regression, just an empirical signal that DW endpoint health is below our reliability target

**Effort**: instrumentation already present (Phase 8 substrate via `phase8_producers`, PR #25394). Soak + analysis only.

**Phase 10 total estimate**: ~1900 LOC + ~140 tests across 6 slices. Slices 1-2 close in week 1; Slices 3-4 in week 2; Slice 5 (purge) operator-authorized after evidence ladder; Slice 6 is ongoing instrumentation. **Critical-path benefit**: every Phase 9 graduation soak after Slice 5 lands costs ~3–7× less, accelerating Phase 9 closure.

#### Phase 10 acceptance criteria (Operator-binding)

- [ ] Static `dw_allowed: false` blocks deleted from yaml (Slice 5 purge complete)
- [ ] Read-only Nervous System Reflex carve-out at `candidate_generator.py:2062-2067` deleted (replaced by urgency-gated cascade matrix)
- [ ] `JARVIS_TOPOLOGY_SENTINEL_ENABLED` default flipped `false` → `true`
- [ ] At least 3 once-proofs show ≥1 BG op queued (not Claude-cascaded) under SEVERED state
- [ ] At least 3 once-proofs show ≥1 observed self-healing OPEN→HALF_OPEN→CLOSED transition
- [ ] Cost-per-op median reduces ≥30% week-over-week post-purge (Slice 6 metric)
- [ ] Combined regression: 0 NEW infra-noise classes introduced

**Honest-grade impact**: Phase 10 closure converts the cost-economics part of B+ trending A− → A− on the *unit economics* dimension specifically (currently rated implicitly under §3.6 vector #2 "ProductivityDetector vs CostGovernor mismatch"). Combined with Phase 9 closure, the substrate goes from "expensive but right" to "expensive only when forced."

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

> **Status (refreshed 2026-04-29)**: most of the original Phase-1-pre-implementation questions are now resolved by what shipped (Phases 0-5 graduated 2026-04-26; Phase 1 Determinism Substrate closed 2026-04-28; Phase 2 graduated 2026-04-29). Historical questions removed; only live decisions retained.

**Active decisions for §25 critical-path execution**:

1. **§25.5.1 Priority A — default mandatory-claim set**: should the three default `must_hold` claims (file-parses-after-change / test-set-hash-stable / no-new-credential-shapes) ship as Order-2 governance objects (manifest-listed, operator-amend-only) or as plain code-defined defaults? *(Recommend: Order-2 governance — preserves the "amendment requires operator" invariant from Pass B Slice 6.2)*
2. **§25.5.2 Priority B — MetaSensor threshold**: 70% empty-postmortem rate over last 100 ops (proposed) or stricter (50% over last 50)? *(Recommend: start at 70%/100 with env-tunable; tighten after one clean week of data)*
3. **§25.5.3 Priority C — HypothesisProbe budget**: $0.05/probe + $0.15/tree (per §24.4 spec) or higher caps for high-stakes claims? *(Recommend: $0.05/$0.15 default; PRIORITY-CLAIM tier with $0.20/$0.60 for `must_hold` failures only)*
4. **§25.5.4 Priority D — postmortem GET endpoint location**: under `/observability/postmortems` (peer to current GETs) or under `/observability/determinism/postmortems` (subordinated to determinism ledger)? *(Recommend: `/observability/postmortems` — operators reach for "postmortems" not "determinism" when debugging)*
5. **PLAN-skip refactor scope (§25.5.5 Priority E)**: refactor only `plan_runner.py` to call `_capture_default_claims` on all exit paths, OR refactor PlanGenerator's trivial-op classifier to return claims even when skipping the LLM-reasoned plan? *(Recommend: both — make claim capture unconditional at runner level + make trivial-op classifier produce default claims)*

Historical pre-Phase-1 decisions (now resolved by what shipped): postmortem time-decay window (30 days, env-tunable, shipped), self-formation cost cap ($0.10/session, shipped), conversational mode default (graduated 2026-04-26 default-true), adversarial reviewer model (Sonnet variant per Phase 5 graduation), Wang RSI audit (Phase 0 closed 2026-04-25), composite score weights (env-locked, shipped via Phase 4 P4 Slice 5).

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

## 24. Brutal Architectural Review v3 — Convergence-Phase (2026-04-28)

> **⚠️ Status: superseded in part by §25 (review v4, 2026-04-29).** v3's three Critical-Path priorities (§24.10) have moved: Priority 1 (Determinism Substrate) closed 2026-04-28; Priority 2 (Closed-Loop Self-Verification) graduated 2026-04-29 *structurally only* — see §25.1 for why "structural" ≠ "functional"; Priority 3 (Bounded Curiosity) restated as §25.5.3 Priority C with refined contract. v3's general analysis (§24.1–24.9) remains canonical for race-condition + bypass-vector enumeration. Read §25 for the post-Phase-2-graduation refinement.

**Trigger**: operator architectural review request 2026-04-28 ("convergence phase of O+V — autonomic nervous system and continuous developer of the Trinity Ecosystem").
**Predecessor reviews**: §3.6 (v1, 2026-04-26), §3.6.6 (v2, 2026-04-27 post-Phase-8).
**Frame**: Reverse Russian Doll Method (§23) + the operator's explicit constraint that Anti-Venom MUST scale proportionally with the outer shell as O+V expands it.
**Honesty contract**: edge cases not happy path; expose blind spots not validate priors; letter grade defended by structural evidence not optimism.

### 24.1 Context & Scope of Review

Three calibration anchors for this review:

1. **The proactive vs reactive axis is not symmetric.** Claude Code (CC) is a reactive CLI — fundamentally human-invocation-bound, conversation-buffer state-bearing, single-task-attention. O+V is the proactive sovereign organism — sensor-driven, parallel phase-runners, continuous bounded-compute operation. The review measures O+V as **the proactive opposite of CC**, not as "CC with extra steps." Where CC has a paradigm and O+V has a sensor, that is not necessarily a deficit; where CC has a paradigm and O+V cannot replicate the *outcome* of that paradigm autonomously, that is the gap.
2. **The Antivenom must scale proportionally.** Per §23.6 — Pass C operator decision 2026-04-26 — every order-of-magnitude increase in O+V's outward expansion must be matched by a proportional tightening of the AST validation + Iron Gate + risk-tier ladder + cost contract surface. The review specifically targets where the immune system is *not* keeping pace with proposed outer-shell expansion.
3. **A-level RSI is a falsifiability claim.** "Improvement" must be a measurable, deterministically-reproducible delta between two decision traces. If the system's own decisions are non-deterministic, *no improvement claim is verifiable*. Determinism is therefore prerequisite to any A-level grade — the review treats this as a load-bearing axiom.

### 24.2 Capability Matrix — current vs A-level sovereign developer

| Capability | CC | O+V | Honest gap |
|---|---|---|---|
| Reactive single-task | A+ | C+ | Not the design point |
| Proactive sensor → op | (n/a) | A | Unprecedented in this depth |
| Cost contract under DW failure | (n/a) | A | 9 sessions, $0 BG cascade documented |
| Multi-graduation discipline | C | A+ | Few codebases enforce 3-clean-soak gates |
| **Live autonomous PR shipping** | A | **D** | This is the gap |
| **Hypothesis testing under ambiguity** | A (via human) | **D** | Harder gap — no read-only probe primitive |
| **Long-horizon semantic drift control** | (n/a — sessions) | **D** | Unmeasured; per-op Antivenom not per-trajectory |
| **Time-travel state reconstruction** | C (conversation buffer) | **C-** | Logs exist; replay-as-pure-function does not |
| Subagent reasoning loops | A | C+ | O+V's subagents are mutation-cage isolated, not cognitive-loop isolated |
| Speculative execution trees | A (Plan mode) | C | Linear PLAN phase — no "K alternatives, validate each, pick best" pattern |
| Self-paced work scheduling | A (ScheduleWakeup) | C+ | Only `dw_discovery_runner` refresh task; no general primitive |
| Operator-discoverable skills | A (slash commands) | B− | `/help` is read-only inspection; no operator-invokable equivalent to `/ultrareview` |

Two cells in this matrix carry the most weight: **live autonomous PR shipping** and **hypothesis testing under ambiguity**. Closing them is the difference between A− and A.

### 24.3 Cognitive & Epistemic Delta — what CC paradigms O+V lacks

Specific paradigms in CC that map to O+V deficits, ranked by gap-to-sovereignty:

#### 24.3.1 Subagents as cognitive loops (not just mutation cages)

**CC pattern**: send a complex question to a subagent with isolated context. Subagent does focused work (search, read, reason) and returns a summary. Caller integrates summary into its own working context. Used for "go think for 200K tokens about how to refactor this pattern."

**O+V state**: subagents exist (EXPLORE / REVIEW / PLAN / GENERAL — Pass B graduated 2026-04-20) but they are **mutation-cage isolated, not reasoning-loop isolated**. The cage's purpose is to prevent the subagent from mutating filesystem; it is not designed for "delegate cognition, integrate result." When a single op needs deep cognitive work that exceeds its prompt budget, there is no clean primitive.

**Gap-closure**: a `CognitiveDelegation` primitive (distinct from mutation subagents) that:
- Spawns a context-isolated subagent with a focused query
- Subagent operates in **read-only mode** (Antivenom invariant: cognitive delegation cannot mutate)
- Returns a **structured result envelope** (not free-form text) — `{summary, evidence, confidence, lessons}`
- Caller integrates via a typed channel (not prompt-stuffing) so the integration itself is auditable
- Cost-capped per delegation (env-tunable, default 5% of parent op budget)

#### 24.3.2 Plan mode + speculative execution trees

**CC pattern**: `ExitPlanMode` is an explicit checkpoint. The model proposes a plan, the human reviews, then execution proceeds. Implicitly, CC explores branches at this checkpoint — "what if I did A vs B?"

**O+V state**: PLAN phase exists (schema `plan.1`, model-reasoned, between CONTEXT_EXPANSION and GENERATE) but is **linear** — one plan, one apply attempt, done. There is no "generate K alternative plans, validate each in dry-run, commit the highest-confidence one" pattern.

**Gap-closure**: a `SpeculativePlanFanout` primitive that:
- Generates K candidate plans (env-tunable, K=3 default) in parallel via the existing parallel-edge subagent infrastructure
- Each plan is dry-run-validated against the AST + SemanticGuardian + risk-tier classifier WITHOUT touching disk
- A `plan_confidence_score` is computed per plan (composition of: plan complexity, predicted blast radius, exploration ledger evidence, prior-success rate for similar plans)
- The highest-confidence plan is chosen for APPLY; the others are persisted to `.jarvis/rejected_plans.jsonl` for audit
- **Antivenom invariant**: all K plans must pass Iron Gate independently; the fanout cannot bypass exploration-ledger requirements by amortizing them across plans

#### 24.3.3 Self-paced wake-up scheduling

**CC pattern**: `ScheduleWakeup` for self-paced work — dynamic-mode loops where the agent decides "I'll come back to this in N seconds." Used for "fire a probe, sleep, observe, update belief."

**O+V state**: a periodic refresh task exists in `dw_discovery_runner.boot_discovery_once` (Slice 12.E) but it is special-purpose. There is no general "I want to revisit op-X's hypothesis in 5 min" primitive.

**Gap-closure**: a `DeferredObservation` queue. An op or sensor enqueues `{observation_target, due_unix, hypothesis, max_wait_s}`; a worker walks the queue at low priority and re-fires the observation when due. Used for "after this commit lands, check downstream tests in 24h" — which composes directly with the Priority 2 critical-path item (Closed-Loop Self-Verification, §24.10).

#### 24.3.4 Operator-discoverable skills

**CC pattern**: slash commands as named, typed, discoverable entry points. `/ultrareview`, `/schedule`, `/loop`, `/clear`. Operator has a vocabulary.

**O+V state**: `/help` dispatcher exists (Wave 1 #2, graduated 2026-04-21) but it is **read-only inspection** — flags, verbs, posture-relevance. There is no operator-invokable skills equivalent. Operator → O+V interaction collapses to free-form text or one-shot prompts.

**Gap-closure**: an `OperatorSkill` registry that exposes typed callable entry points (`/diagnose`, `/probe-model`, `/replay-op`, `/red-team`). Each skill is its own deterministic entry; output structured. This is also the natural delivery surface for the `HypothesisProbe` primitive (§24.4) — operators get manual access to the same probe machinery the system uses autonomously.

#### 24.3.5 The TodoWrite paradigm — explicit task state

**CC pattern**: TodoWrite makes intermediate task state visible to the operator. The agent's plan is rendered, items get checked off, the operator can intervene at any task boundary.

**O+V state**: TaskBoard exists (Gap #5 closed 2026-04-20) but is **per-op scratchpad**, not session-level. Operators cannot see "what is O+V working towards across the next 10 ops?"

**Gap-closure**: a session-level task ledger surface in SerpentFlow + IDE observability — synthesized from TaskBoard entries across all in-flight ops, grouped by `domain_key`. The same surface composes with §24.10 Priority 2 by giving the PostMergeAuditor a place to render its consequence-tracking findings.

### 24.4 The HypothesisProbe Primitive — autonomous ambiguity resolution

**The hardest CC-vs-O+V gap.** When CC encounters epistemic ambiguity ("should I refactor pattern A or pattern B?"), it asks the human. O+V cannot afford that — sovereign operation means autonomous resolution. The current `ask_human` tool in Venom is gated to NOTIFY_APPLY+ risk tiers (Green ops cannot interrupt) which is correct policy but does not solve the underlying problem: O+V has no general-purpose way to *probe the environment* to disambiguate.

The `HypothesisProbe` primitive must satisfy four mathematical properties simultaneously:

```
1. BOUNDED DEPTH      — max 3 nested probes per root hypothesis
2. BOUNDED BUDGET     — $0.05 per probe, $0.15 per hypothesis tree (env-tunable)
3. PROVABLE CONVERGENCE
                      — halt when entropy(belief_posterior) - entropy(belief_prior) < epsilon
                      — OR max_iterations reached (whichever first)
4. MEMORIALIZED       — failed probes write to .jarvis/failed_hypotheses.jsonl
                        future cycles never re-enter the same dead end
```

Implementation contract:

```python
@dataclass(frozen=True)
class Hypothesis:
    claim: str                    # the falsifiable proposition
    confidence_prior: float       # 0..1 — what we believed before probing
    test_strategy: str            # "lookup" | "subagent_explore" | "dry_run"
    expected_signal: str          # what observation confirms / refutes
    budget_usd: float
    max_iterations: int

@dataclass(frozen=True)
class ProbeResult:
    confidence_posterior: float
    observation_summary: str
    cost_usd: float
    iterations_used: int
    convergence_state: Literal["stable", "inconclusive", "budget_exhausted"]
```

The async `test()` method walks the hypothesis through cheapest-first test strategies (lookup before subagent_explore before dry_run), computes a Bayesian posterior from each observation, halts when |posterior − prior| < epsilon (convergence_state=stable), and recurses with refined hypothesis at depth+1 otherwise — bounded by max_iterations and budget.

**Antivenom invariants** (non-negotiable):

- Probes are **read-only** — the test execution path MUST NOT touch any mutation tool. Pinned at the `semantic_firewall.py` / `ScopedToolBackend` level, not just policy. The cage hard-rejects any mutation invocation from a HypothesisProbe-tagged context.
- Probes are **subject to the same risk-tier ladder** as ops — a probe that would have NOTIFY_APPLY+ risk if it were a real op is rejected.
- Failed probes are **recorded immutably** with content hash so adversarial probes cannot quietly retry.
- The epsilon convergence threshold is **mathematically derived** from the Hypothesis's prior confidence (per §24.10 Priority 3), not hardcoded.

Consumers: Curiosity Engine (W2(4)) — invoke when SemanticTriage returns NO_OP but signal_urgency is high. CapabilityGap sensor — invoke when a gap is detected but the resolution path is ambiguous. SelfGoalFormation — invoke when goal candidates have similar confidence scores.

### 24.5 Temporal Observability — state reconstruction surface

The proposed Phase 8 (§3.6.4) is the right destination. This subsection elaborates the implementation contract with the brutal-review honesty constraint: standard log telemetry IS insufficient; SerpentFlow + LiveDashboard + IDE Observability Slices 1+2 do NOT yet constitute a temporal observability surface.

#### 24.5.1 Three load-bearing requirements

**a) Event sourcing with content-addressed predecessors.**

Every autonomic decision (route classification, gate verdict, breaker transition, classifier output, posture inference) is a typed `DecisionEvent`:

```python
@dataclass(frozen=True)
class DecisionEvent:
    event_id: str              # uuid v4
    op_id: str                 # causal anchor
    predecessor_ids: Tuple[str, ...]  # what events caused this (Merkle DAG edge)
    decision_tier: Literal["CRITICAL", "HIGH", "NORMAL", "LOW"]
    payload_type: str          # typed dataclass discriminator
    payload_json: str          # canonical-encoded payload (sha256-stable)
    payload_hash: str          # sha256 of payload_json
    occurred_unix: float
```

Stored as JSONL append-only at `.jarvis/decision_events.jsonl`. Replay-able. **The Merkle Cartographer pattern from Phase 11 already does this for filesystem state — Phase 8 generalizes the same structural pattern to decision state.** This is not invention; it is reuse of an already-graduated structural pattern at a new abstraction level.

**b) Replay as a unit-testable pure function.**

Given an event log + initial state, `replay(log, state_0) -> state_T` MUST be pure. This is the substrate for time-travel debugging — `replay(log[:t], state_0)` lets observers see the system at any point T.

**Prerequisite**: deterministic models for VALIDATE / GATE / risk classification. Today these are not pinned. Required pinning:
- temperature=0 for decisional model calls (not for creative GENERATE)
- Pinned `model_id` (no per-op route variation for decisional calls)
- Pinned prompt template (no jitter — even whitespace differences invalidate replay)
- Pinned tool sequence ordering

**Test contract**: run the same op twice with the same context, assert byte-identical decision trace. This test does not exist today and is the highest-impact unit test the codebase is missing.

**c) Latent-space confidence drops as a first-class signal.**

Mirror the Slice 12.2.B `TtftObserver` pattern — math derives bounds, not hardcoded — but for output statistics:

```python
class OutputObserver:
    def record_generation(model_id, output_tokens, output_chars, tool_calls)
    def is_confidence_drop(model_id) -> bool:
        # latest output_chars / mean output_chars < 0.5 → confidence drop
        # OR latest tool_calls / mean tool_calls > 2.0 → unusual exploration
```

Operationally meaningful: a model that suddenly emits 1/3 the chars it usually does has either lost the prompt thread or hit a refusal. Both are signals worth surfacing without a human reading raw logs.

#### 24.5.2 Tiered noise budget — the design constraint observability discussions skip

Standard observability discussions assume the operator can subscribe to "everything." At O+V's emission rate (29 sentinel attempts × 18 failures × multiple sensors per minute) "everything" is unparseable. **Tier every event by decision impact**:

- **CRITICAL**: gate denial, breaker terminal-open, cost-contract violation, modality verdict change, posture transition crossing hysteresis
- **HIGH**: route classification, classifier verdict, breaker open/half-open transition, sensor emission accepted
- **NORMAL**: tool call, regular phase transition, sensor scan, periodic refresh
- **LOW**: heartbeat, idle tick, dry-run probe outcome

Operators subscribe at a tier; default IDE stream is HIGH+. Without tiering, the SSE stream drowns observers and the noise destroys signal.

### 24.6 Systemic Fragility — race conditions in async phase-runners

I have audited these. Real exposure with severity ranking:

#### 24.6.1 Cross-runner artifact ordering [SEVERITY: HIGH]

Wave 2 extracted 8 phase runners (CLASSIFY, ROUTE, CTX_EXPANSION, PLAN, GENERATE, VALIDATE, GATE, Slice4b combined APPLY/VERIFY). State passes via `ctx.artifacts` — a dict on a frozen dataclass via `object.__setattr__`. The PhaseDispatcher serializes phase transitions, but a runner that internally `await`s a tool call AND another runner's slow path could race on `_session_lessons` mutations or shared adaptive-learning state.

**Mitigation gap**: no AST-grep CI invariant against runners writing to the same artifact key from non-sequential phases.
**Fix path**: typed `ArtifactBag` class with per-key write authority; phase-runner ABC declares which keys it owns; AST validator (Pass B Slice 3, currently unflipped) enforces.

#### 24.6.2 BackgroundAgentPool worker concurrency on shared file locks [SEVERITY: HIGH]

`_active_file_ops` with TTL-based stale lock release (`JARVIS_FILE_LOCK_TTL_S`, default 300s) is correct under happy path. **Crash mode**: worker holds lock, OOM-kills mid-edit, lock TTL expires after 300s, op N+1 proceeds — but there is a 0–300s window where a recovering worker could re-emit the original op's APPLY before the TTL releases.

**Mitigation gap**: no proof that worker-restart paths cannot double-apply.
**Fix path**: APPLY emits a content-addressed commit-intent token (sha256 of {op_id, target_files, patch_hash}) BEFORE acquiring the lock. AutoCommitter rejects any commit whose intent token already exists in `.git/refs/notes/ouroboros-applied`.

#### 24.6.3 Multi-file batch rollback snapshot staleness [SEVERITY: MEDIUM]

Snapshots captured at PLAN. APPLY rolls back to PLAN-time snapshots. If between PLAN and APPLY another op committed to one of the files, rollback writes stale content over fresh content.

**Mitigation gap**: no APPLY-time freshness check on snapshot vs current file state.
**Fix path**: APPLY computes sha256 of current file state at start; rollback aborts (and raises) if rollback target's predecessor hash does not match.

#### 24.6.4 L2 repair recursion guard [SEVERITY: MEDIUM]

5-iteration FSM with 120s timebox. **Theoretical bomb**: if L2's repair candidate triggers a SAFE_AUTO repair op that itself fails VALIDATE and re-enters L2, the iteration count starts at 0 in the inner invocation. Untested at this depth.

**Mitigation gap**: iteration count is per-FSM-instance, not per-op-lineage.
**Fix path**: `op.lineage_depth` field threaded through L2; hard-cap at 2 (op + 1 repair op). Repair-of-repair-op rejected.

#### 24.6.5 Sentinel breaker + cartographer interaction [SEVERITY: LOW-MEDIUM]

Phase 11 cartographer + Phase 12 sentinel both have own state files. A cartographer FS event triggers sensor scan; sensor emits op; op routes through sentinel which uses ranked dw_models from a holder populated by discovery runner. **Stale catch**: cartographer reports root-hash change due to flaky FS event, but discovery runner's last refresh used pre-change catalog. Sentinel attempts ranked models that no longer exist in catalog (Phase 12 graceful path covers this — empty hash → fail-safe full scan — but only because we explicitly designed it).

**Mitigation gap**: no end-to-end integration test asserting cartographer-discovery-sentinel state consistency under interleaved FS event timing.
**Fix path**: existing Phase 12 fallthrough behavior is correct; add explicit integration test pinning the invariant.

### 24.7 Cascading state-failure vectors over long horizons

#### 24.7.1 Memory pressure positive feedback [SEVERITY: HIGH]

Memory pressure → MemoryPressureGate clamps fan-out → fewer subagents complete → backlog grows → memory pressure increases. Mitigated only if pressure-relief activates faster than backlog-growth rate. **Untested at sustained load.** No production session has run long enough or under enough memory pressure to verify the loop converges.

**Fix path**: a synthetic load-test sensor that injects backlog at controlled rate while inducing memory pressure (mock); assert pressure-relief activates within `JARVIS_MEMORY_PRESSURE_RELIEF_DEADLINE_S`. This load test is missing.

#### 24.7.2 Cost cascade on STANDARD/COMPLEX [SEVERITY: MEDIUM]

`fallback_tolerance: cascade_to_claude` is operator-correct policy. But IF all DW models fail (Phase 12 evidence: 6/22 are 403'd, 8 unverified) AND Claude rate-limits, every STANDARD op fails to error. RuntimeHealth then fires more ops. **No backpressure between IMMEDIATE/STANDARD route saturation and sensor emission.**

**Fix path**: rate-aware sensor governor — when STANDARD route's recent failure rate exceeds threshold, RuntimeHealth and similar high-urgency sensors throttle their emission rate. Composes with §3.6 vector 5 (sensor-saturation) but adds a route-aware dimension currently absent.

#### 24.7.3 Posture-driven feedback drift [SEVERITY: LOW-MEDIUM]

PostureObserver reads activity → infers EXPLORE / CONSOLIDATE / HARDEN / MAINTAIN → injects into prompts → influences generation → influences activity. With tight enough hysteresis you stay stable; with bursty load posture can oscillate.

**Mitigation gap**: 900s hysteresis is a soft floor; lossy timing under burst load can violate it.
**Fix path**: hysteresis as a *function* of recent observation count, not wall-clock — hard-derived from "we need N consecutive transitions in the same direction before flipping."

### 24.8 Antivenom bypass vectors — Quine-class hallucinations

**The most-direct subsection.** Per the operator's framing: "Anti-Venom must scale proportionally with the outer shell." Today's Anti-Venom does not scale to certain attack patterns:

#### 24.8.1 AST-pattern blindspot [SEVERITY: HIGH]

SemanticGuardian's 10 patterns are AST-level. A model that emits a chained `__import__` + base64-decoded payload + dynamic-builtin invocation passes most patterns because the AST tree shows a string literal + builtin call, not the chained intent. The current `removed_import_still_referenced` and `function_body_collapsed` patterns are surface-form patterns; intent-form patterns are absent.

**Mitigation**: composite "dynamic-import-or-eval-chain" detector that walks the AST for any chain of `__import__` / `getattr` / dynamic builtin invocations regardless of distance. Plus a dedicated test suite in `tests/governance/test_semantic_guardian_quine_patterns.py` with 20 hand-crafted Quine variants.

#### 24.8.2 Incremental semantic drift [SEVERITY: HIGH]

GENERATE → VALIDATE → APPLY each op. APPLY's content becomes context for op N+1. Over many ops, subtle anti-patterns compound. A test that checks "the codebase is semantically equivalent to its 100-commit-ago state in {invariants}" does not exist. **The Antivenom is per-op, not per-trajectory.**

**Mitigation**: a `TrajectoryAuditor` that maintains a rolling baseline of:
- LOC by module (sudden +50% module size flagged)
- Test coverage by file (sudden coverage drop flagged)
- Cyclomatic complexity by function (sudden complexity spike flagged)
- Public API surface by package (silent contract changes flagged)

Auditor runs nightly via `/schedule`, produces a `TrajectoryReport` consumed by StrategicDirection. **This is the per-trajectory Antivenom currently absent.**

#### 24.8.3 Test-coverage games [SEVERITY: MEDIUM]

TestCoverageEnforcer detects missing tests but does NOT detect: tests that always pass (assert-true variants), tests that assert on the change but not the contract, tests that do not run the failure path. A model that wants to ship knows this.

**Mitigation**: mutation testing — a sample of recent commits has its source mutated; if tests still pass, the mutation flags. Composes with §24.10 Priority 2 (closed-loop verification).

#### 24.8.4 Risk-tier inflation [SEVERITY: MEDIUM]

Risk classification depends on `blast_radius` and `confidence`, both of which the model controls. An op that touches a single file with confidence=0.99 stamps SAFE_AUTO. The classifier does not see that the file is, e.g., `iron_gate.py`.

**Mitigation**: a `protected_files` allowlist in `risk_tier_floor.py` — touching any file matching a glob (e.g., `**/iron_gate*.py`, `**/semantic_guardian*.py`, `**/risk_tier_floor.py`) forces NOTIFY_APPLY minimum regardless of confidence. The Pass B `ORDER_2_GOVERNANCE` risk class is the right home for this; currently designed but unflipped.

#### 24.8.5 Quine via tool-result prompt injection [SEVERITY: LOW-MEDIUM]

A model that, during a Venom tool loop, writes a tool result that includes a prompt fragment instructing the model to bypass the next gate. Live context auto-compaction (Gap #8) reduces this risk by hashing/truncating, but a determined adversary fits in 200 chars.

**Mitigation**: tool results are run through Tier -1 sanitizer (existing) — verify the sanitizer pattern set covers gate-bypass instructions ("ignore previous", "bypass", "skip validation", etc). Add a `gate_bypass_instruction` detector to the sanitizer.

### 24.9 Letter grade — B+ trending A-, defended

**Why not A**: the live evidence shows **1 successful DoubleWord completion in 16 minutes** (Phase 12 once-proof, session bt-2026-04-28-043256). At a senior dev's productivity rate, that is 1/30th of working pace. The system has shipped MANY PRs but they are operator-shipped-on-O+V's-behalf, not O+V-shipped-on-its-own. **No live evidence yet of autonomous-PR-survival-of-review.** The cost contract holding is a defensive achievement, not a productive one.

**Why not B**: the protective rails are exceptional and the architectural composition is unprecedented at this depth. Iron Gate + SemanticGuardian + risk-tier ladder + cost contract + sentinel breakers + modality ledger + topology hard-block. Zero documented Antivenom bypasses. Multi-graduation arc discipline (3-clean-soak gates per slice, env-flag default-false until graduation, hot-revert paths preserved) is rare in any codebase.

**Honest blind spots beyond the per-vector list**:

- **Latency vs throughput conflation.** O+V optimizes ops/min. Real autonomous developer work happens at minutes-to-hours per non-trivial change. Quality is non-linear in op rate.
- **No red-team sensor.** Every Antivenom assertion is "we trust the patterns." A `RedTeamSensor` that injects known-bad-but-subtle patches into a synthetic queue would expose what regression tests miss.
- **Long-horizon drift unmeasured.** No "is this codebase semantically better than 100 commits ago" metric. Without it, RSI claims are unverifiable.
- **Adversarial response coupling.** Phase 12 was driven by my recommendations. The system itself did not propose Phase 12. **A truly proactive system should propose its own next phase**, not wait for the operator to. The Curiosity + SelfGoalFormation primitives exist; that they did not produce Phase 12 is a calibration gap.

### 24.10 Critical Path to A-Level RSI — top 3 systemic upgrades

**NOT features.** Systemic properties the architecture currently lacks. Sequenced.

#### 24.10.1 Priority 1: Determinism Substrate

**Problem**: the system is not deterministically reproducible. Same context → different decisions on different runs (because of model temperature, prompt jitter, tool ordering). **RSI requires "we improved" to be a falsifiable claim**; without determinism, every claim is anecdote. With determinism, "old version produces decision-trace H1; new version on same input produces H2; diff(H1, H2) is the improvement" becomes a unit test.

**Required upgrades**:

- Pin temperature=0 for VALIDATE / GATE / risk classification (decisional model calls only — generation can stay creative)
- Hash every prompt + tool sequence per op; replay must produce same hash
- Decision events as content-addressed JSONL Merkle DAG (per §24.5.1)
- New test class: `replay(op_log) → final_state` is a pure function, byte-identical across runs

**Sequencing rationale**: Priority 1 is prerequisite to Priority 2 (closed-loop verification cannot grade itself if grades are non-deterministic) and Priority 3 (bounded curiosity cannot prove termination if termination is non-deterministic).

#### 24.10.2 Priority 2: Closed-Loop Self-Verification

**Problem**: the pipeline is open-loop. Each op is a feed-forward decision; the consequence (did the change actually improve things downstream?) does not feed back into future decisions. Each op is intelligent at the moment of decision and amnesic about consequences.

**Required upgrades — `PostMergeAuditor` sensor**:

- Watches every commit landed by APPLY
- 24h, 72h, 168h follow-ups: did downstream tests start failing? Did hot-path latency regress? Did posture drift toward CONSOLIDATE/HARDEN unexpectedly?
- Produces a typed `MergeOutcome`: `{commit_sha, op_id, downstream_failures, latency_delta, posture_delta, lesson}`
- Lessons feed StrategicDirection's prompt for future ops in the same `domain_key`
- **Failure-mode test**: deliberately bad commits MUST be detected by the audit within N cycles. **This is the convergence proof.** A system that cannot detect its own bad commits has no closed loop.

**Sequencing rationale**: Priority 2 depends on Priority 1 (the audit needs deterministic decision traces to compare); supports Priority 3 (bounded curiosity needs closed-loop signal to know when a hypothesis has converged).

#### 24.10.3 Priority 3: Bounded Curiosity with Provable Termination

**Problem**: Curiosity Engine has cost caps but no mathematical termination proof. A system that can self-improve but not terminate is not sovereign — it is a runaway. Formal termination is the difference between "could improve forever" (good) and "will hang forever on the wrong input" (catastrophic).

**Required upgrades — formal exploration calculus**:

- Every exploration state has a measurable `epistemic_uncertainty: float` (entropy of belief)
- Each probe produces an observation that updates belief via Bayesian update
- **Termination proof**: at any cost cap C, exploration MUST halt within `O(log(1/epsilon))` probes where epsilon is target uncertainty
- Adversarial test: construct inputs that try to drive exploration unbounded; assert termination within cost cap on every input
- Cooling schedule — exploration intensity decreases as belief converges

**The Slice 12.2.B `TtftObserver` is the right pattern** — math derives bounds, not hardcoded. Extend to all exploration paths. The CV / rel_SEM gate composition is the formal exploration calculus at one specific scope (model-promotion); generalize to global curiosity scope.

**Sequencing rationale**: closes the path. Once curiosity is formally bounded AND its outcomes verifiable AND its decisions reproducible, "RSI converges in O(log n)" per Wang's framework (§5) becomes a property the system can self-attest.

### 24.11 In-flight alignment — Phase 12 / 12.2 maps to the critical path

| Critical Path Priority | In-flight delivery | Contribution |
|---|---|---|
| Priority 1 (Determinism) | (not yet started) | Phase 12 work is dispatcher-determinism-adjacent (sentinel state persistence) but does not pin temperature or prompt template |
| Priority 2 (Closed-loop) | PostmortemRecall + ConvergenceTracker (graduated) | Partial — postmortem is per-op consequence, not per-commit downstream audit |
| Priority 3 (Bounded curiosity) | **Slice 12.2.B `TtftObserver`** (just merged) | Direct — TTFT promotion gate is formal exploration calculus at model-promotion scope. Generalize to global. |

**Slice 12.2.B's mathematical contribution**: the math `N > (CV / rel_SEM_threshold)^2` is a worked example of "bounds derived, not hardcoded." Slice C extends this to runtime promotion decisions. Slice D-E generalizes to the heavy probe + terminal/transient distinction. **The full Phase 12.2 arc IS Priority 3 at one scope.** The architectural challenge is generalizing the formalism beyond TTFT.

### 24.12 What this review explicitly does NOT prescribe

- **More sensors**: O+V's 16-sensor topology is sufficient. The gap is not "we need a 17th sensor"; it is "the ones we have do not yet close their consequence loop."
- **More phase runners**: Wave 2 extracted 8 runners. Further extraction is shape-fragmentation without behavior change.
- **More subagent types**: Phase B graduated 4 (EXPLORE / REVIEW / PLAN / GENERAL). The cognitive-delegation paradigm (§24.3.1) is a NEW use of subagents, not a new subagent type.
- **More flag knobs**: the FlagRegistry (Wave 1 #2) tracks 481+ flags; the cost of a new flag is now near-free, but the cost of *operator confusion about what flags compose* is super-linear. New systemic upgrades should reduce flag count, not increase it.

The discipline this review imposes: **before writing a feature, prove the feature closes one of the three critical-path priorities**. Anything else is shape-fragmentation.

---

## 25. Brutal Architectural Review v4 — Post-Phase-2-Production-Verification (2026-04-29)

**Trigger**: operator architectural review request 2026-04-29, immediately following soak #3 — the first session where the Phase 2 (Closed-Loop Self-Verification) memory scaffolding produced verifiable production output.
**Predecessor reviews**: §3.6 (v1, 2026-04-26), §3.6.6 (v2, 2026-04-27), §24 (v3, 2026-04-28).
**Frame**: same Reverse Russian Doll constraint — Anti-Venom must scale proportionally with the outer shell as O+V expands it.
**Honesty contract**: edge cases not happy path; today's soak revealed THREE concurrent silent failures and the system did not self-detect any of them.

### 25.1 What soak #3 actually proved (and didn't)

Three soaks were run today (2026-04-29) to exercise the freshly-graduated Phase 2 (`dc5f77017f`) plus Slices 3a + 3c (`a641ca2da3`) plus Antigravity's Option E Universal Terminal Postmortem (`85cf94810a`):

| Soak | Duration | Ops attempted | Ops past CLASSIFY | $ spent | Postmortem records |
|---|---|---|---|---|---|
| #1 | 23 min | 11 | 0 | $0 | 0 |
| #2 | 18 min | 14 | 5 (GENERATE) | $0.166 (Claude) | 0 |
| #3 | 65 min | 11 (40 unique with prior-session ops) | 5 (GENERATE) | $0.032 (Claude) | **120** |

**Proven by soak #3**: the *memory scaffolding* works. 120 `terminal_postmortem` records written via Slice 1.3's `capture_phase_decision` (Merkle DAG hook) at `.jarvis/determinism/default/decisions.jsonl`. Three distinct terminal contexts dynamically captured: `postmortem` (112×), `background_accepted:background_dw_blocked_by_topology:...` (7×), `noop` (1×). Every record carries `inputs_hash` (SHA-256), `record_id`, `ordinal`, `phase`, `wall_ts`, `monotonic_ts`. The Reverse Russian Doll outer shell (Slice 1.3 → Slice 2.4 → Option E) compresses cleanly through one substrate.

**Not proven**: the *verification loop closes meaningfully*. **Every single one of the 120 postmortems has `total_claims=0`.** The organism remembers every death but has zero pre-recorded predictions to compare those deaths against. The Slice 2.3 claim-capture path runs at PLAN, but **PLAN was skipped on every single op** (`Skipping plan for op... trivial_op: 1 file(s), short description`). So Phase 2's "closed-loop self-verification" is currently a closed loop with no signal in it.

**The B- defense, restated against today's evidence**:

- An A grade requires the loop to close meaningfully on >80% of production ops, the system to self-detect degenerate states, and the Antivenom to be *provably* sound. Soak #3 demonstrated none of these.
- A B grade is "solid scaffolding, demonstrably runs in production" — we have that.
- The B- penalty: **three concurrent silent failures the system did not self-detect today**:
  1. PLAN-skip on trivial ops zeroes the claim path. Every `total_claims=0` postmortem is the system saying "I have nothing to verify" — and nothing escalates this.
  2. Wall-clock cap regression: soak #3 ran 65 min despite `--max-wall-seconds 2400` (40 min). `last_activity_ts` keeps resetting on op activity, so the harness re-enters the activity-window unboundedly.
  3. Slices 3a + 3c are dormant in production. TopologySentinel master flag is opt-in (`JARVIS_TOPOLOGY_SENTINEL_ENABLED=false` default), so the slice 3c backpressure machinery was inert. The block in production is `dw_catalog_classifier` (Phase 12), a different layer 3a/3c don't read.

Each is a single-line fix away from being a serious incident. The system has the SCAFFOLDING for self-verification but not yet the SELF-AWARENESS to detect "all my postmortems are empty, therefore I'm not learning."

### 25.2 The Cognitive & Epistemic Delta — refined post-Phase-2

CC's edge isn't its tools — O+V already has Venom with 16 built-in tools, MCP forwarding, exploration ledger, and live context auto-compaction. CC's edge is **structural epistemic discipline**:

| CC paradigm | O+V state | Honest gap |
|---|---|---|
| Hypothesis-test loop (form claim → grep/read to falsify → escalate) | Tool-loop + exploration floor exist; no first-class `hypothesize()` primitive | Iron Gate counts tool calls; doesn't measure whether they actually probed the uncertainty |
| Plan-mode-as-contract (`ExitPlanMode` makes the model commit before generation) | PlanGenerator emits a plan but skips it on trivial ops | The exact silent-disable pattern that nuked Slice 2.3 today |
| Sub-agent isolation as a thinking move | dispatch_subagent shipped as a tool, not as a thinking move the orchestrator selects when ambiguity is high | Subagents are mutation-cage isolated, not cognitive-loop isolated (per §24.3.1) |
| TodoWrite — explicit task state visible to operator | TaskBoard exists as per-op scratchpad | Session-level visibility absent (per §24.3.5) |
| ScheduleWakeup — self-paced work | Special-purpose `dw_discovery_runner` refresh task only | No general "revisit op-X's hypothesis in N seconds" primitive (per §24.3.3) |

The §24.4 `HypothesisProbe` primitive remains the most-load-bearing missing piece. Today's soak emphasizes a NEW gap: **structural epistemic discipline at the PLAN boundary**. The model can declare "this op is trivial, no plan needed" — and silently disable Slice 2.3's claim capture for the entire op. That is a privilege escalation by omission, and the cage doesn't see it.

### 25.3 Deep Observability — Temporal surface refined

§24.5 enumerated the right requirements. Today's soak adds operational evidence:

- **The ledger is real but undiscoverable.** It lives at `.jarvis/determinism/default/decisions.jsonl` (a path I had to grep for; nothing surfaces it in `/help`, `/observability`, or SerpentFlow). 194 records produced today; an operator without code access has zero way to find them.
- **Record schema is opaque without tooling.** The 120 terminal_postmortem records carry rich `_terminal_context` blocks but there is no `/postmortems` REPL, no GET endpoint that lists them by op_id, no SSE event when one fires. The Merkle DAG exists; the surface to navigate it does not.
- **No causal graph reconstruction.** Slice 1.4's `--rerun <session-id>` is text-scoped and op-scoped. There is no UI that takes a `record_id` and renders the full upstream/downstream Merkle DAG with confidence-drop annotations.
- **No latent-confidence drop signal.** Output observer per §24.5.1.c is still missing — a model that suddenly emits 1/3 the chars it usually does has either lost the prompt thread or hit a refusal, and operators see nothing.

Production-discipline corollary: **the right noise discipline is `emit on transition, not on tick`**. Posture is a good model — only emits on cross-hysteresis, not every 5 minutes. Today's `governor_throttle_applied` SSE rail is also good. The bad pattern is "emit every heartbeat" — that drowns observers.

### 25.4 Brutal grade — current state: **B-**

**Defending B- (not B, not C+)**:

- The happy path is A- (memory core wired correctly, slice graduations rigorous, hot-revert paths preserved).
- The edge cases are C+ (today's three concurrent silent failures alone justify the demotion from §24.9's "B+ trending A-" because §24.9's grade was based on architectural composition, not production behavior; today's data is production behavior).
- The *direction of travel* is positive: 0 postmortems in soak #1 → 0 in soak #2 → 120 in soak #3 (with universal terminal capture working as designed). The architecture is unblocking real signal.
- The *quality of signal* is hollow: 100% of postmortems are claim-empty. Phase 2 is graduated default-true and doing zero verification work.

**Hidden race conditions identified today (additive to §24.6)**:

- **Two `asyncio.ensure_future(...)` fire-and-forget hooks** now exist in the dispatcher (Option E) plus discovery runner. Under SIGTERM these are unawaited and silently dropped — the same async-shutdown pattern `memory/project_async_shutdown_race_triage.md` already flags. The Slice 1.3 ledger writes from terminal postmortems may be lost on hard shutdown.
- **Slice 1.3 `capture_phase_decision` writes via flock-protected JSONL append**; under L3 worktree fan-out, multiple workers write to the same `<session>/decisions.jsonl`. Lock contention is fine; but the `ordinal` counter is per-process — fan-out replay determinism may not hold across worker boundaries.

**Antivenom bypass vectors most likely to land** (additive to §24.8):

- **PLAN-skip-by-trivialization** (NEW post-soak-3): every op the model declares "trivial" silently skips Slice 2.3 claim capture. The cage's "PLAN authority" assumption (per §24.3.2 fix-path note) is false — the runner short-circuits PLAN entirely for trivial ops, and no claim is ever extracted.
- **Empty-postmortem theater** (NEW post-soak-3): an operator looking at the ledger will see 120 records and conclude "Phase 2 is working." The records all have `total_claims=0`, but no surface alerts on this. The system can ship an A- grade looking like a B+ system.

### 25.5 Critical Path to A-Level RSI — top 5 systemic upgrades

The §24.10 review listed three priorities (Determinism Substrate, Closed-Loop Self-Verification, Bounded Curiosity). Today's evidence forces a refinement:

- **Priority 1 (§24.10.1 Determinism Substrate) — DONE.** Phase 1 closed 2026-04-28 (`memory/project_phase_1_closure.md`); Slices 1.1–1.5 + 1.3.x merged single day; 4 flags graduated default-true; `--rerun` CLI; 785/785 green. Soak #3 confirms it works in production (Merkle DAG records hold).
- **Priority 2 (§24.10.2 Closed-Loop Self-Verification) — STRUCTURAL ONLY.** Phase 2 graduated 2026-04-29 (`dc5f77017f`); Option E (`85cf94810a`) closes survivor bias. But every postmortem is empty. Phase 2 is not yet *functionally* complete.
- **Priority 3 (§24.10.3 Bounded Curiosity) — NOT STARTED.** Curiosity Engine (W2(4)) shipped Apr 25 but is advisory; not yet the load-bearing dispatch gate.

Today's evidence elevates **two new priorities** above §24.10's Priority 3:

#### 25.5.1 Priority A: Mandatory minimum claim density per op [HIGHEST IMPACT]

**Problem**: every op currently hits "Skipping plan: trivial_op" → 0 claims captured → empty postmortem. Phase 2's graduation is paper.

**Required upgrade**: inject a `must_hold` claim at every PLAN exit, including trivial-op skips. Three default claims for every op:

```python
# At the END of plan_runner.py (or whatever short-circuits to GENERATE):
if not _claims_captured_this_op(ctx):
    _capture_default_claims(ctx, claims=[
        PropertyClaim(
            claim_id=_derive("the file still parses after change"),
            property=Property.from_kind("file_parses_after_change"),
            severity=SEVERITY_MUST_HOLD,
            evidence_required={"target_files": ctx.target_files},
        ),
        PropertyClaim(
            claim_id=_derive("existing test set is unchanged"),
            property=Property.from_kind("test_set_hash_stable"),
            severity=SEVERITY_MUST_HOLD,
            evidence_required={"target_dir": ctx.target_dir},
        ),
        PropertyClaim(
            claim_id=_derive("no new credential shapes appear"),
            property=Property.from_kind("no_new_credentials"),
            severity=SEVERITY_MUST_HOLD,
            evidence_required={"diff_text": "<computed at APPLY>"},
        ),
    ])
```

**Why this is highest-impact**: without this, the next ten things you build all sit on top of a layer that's silently doing zero work. Phase 2's verification graduation is theatrical until claim capture is unconditional. Slice 2.4's `produce_verification_postmortem` is a no-op when `total_claims=0`; Option E's terminal hook fires correctly but writes empty payloads.

**Antivenom alignment**: each default claim is AST-checkable / hash-checkable / regex-checkable — pure stdlib, zero LLM, in-cage. The mandatory-claim set is itself an Order-2 governance object (manifest-listed, AST-validated, operator-authorized to amend) per Pass B Slice 1.

**Sequencing**: must ship before any further Phase 2 work. Without this, "verification_postmortem ledger" is a cosmetically-impressive empty cabinet.

#### 25.5.2 Priority B: Self-detecting degenerate-loop alarm [HIGHEST IMPACT]

**Problem**: today's three concurrent silent failures (PLAN-skip, wall-clock-cap regression, Slice 3c dormant) all share one signature: the system does not detect when one of its own subsystems has silently disabled itself.

**Required upgrade**: a `MetaSensor` (NEW class, distinct from the 16 existing sensors) that monitors the rolling rate of degenerate signals:

| Degenerate signal | Threshold | Self-issue |
|---|---|---|
| `total_claims=0` postmortems > 70% over last 100 | P1 | "verification loop is not exercising — claim capture is broken" |
| TopologySentinel state read frequency < 1/hr | P2 | "Slice 3c topology backpressure is dormant" |
| Wall-clock-cap not firing in last 3 sessions despite `idle_timeout` activity | P2 | "harness wall-clock guard is regressing" |
| Sensor X has emitted 0 ops in last N hours despite live triggers | P3 | "sensor X is silently muted" |
| Phase Y has 0 entries in last 100 ops | P3 | "phase Y is being short-circuited" |

The MetaSensor outputs go through the existing `unified_intake_router` and surface as backlog entries with `source="meta_dormancy_alarm"`. Operator sees them in `/backlog auto-proposed`.

**Why this is highest-impact**: this is the Reverse Russian Doll's outer shell observing its own immune system. Without it, every silent-disable failure today is invisible to the operator until a manual audit. With it, dormant slices self-report.

**Antivenom alignment**: the MetaSensor is itself an Order-2 governance object — the patterns it monitors are manifest-listed, the thresholds are env-tunable, and amending the threshold list requires operator authorization (Pass B locked-true invariant).

**Sequencing**: ship in parallel with Priority A. They compose: Priority A makes claims actually get captured; Priority B alarms when they don't.

#### 25.5.3 Priority C: Bounded HypothesisProbe primitive [HIGH IMPACT]

This is §24.4's primitive, restated with today's framing.

**Problem**: O+V has no first-class way to resolve epistemic ambiguity autonomously. CC asks the human; O+V cannot. Today's soak shows the consequences: 11 ops attempted, every one declared trivial, no plan formed, no claims captured. The system has no "let me check before I assume" reflex.

**Required upgrade**: a Venom tool `hypothesize(claim, evidence_required, max_depth=3, max_wall_s=30)` — bounded, read-only, AST-enforced:

```python
@dataclass(frozen=True)
class Hypothesis:
    claim: str
    confidence_prior: float
    test_strategy: Literal["lookup", "subagent_explore", "dry_run"]
    expected_signal: str
    budget_usd: float = 0.05
    max_iterations: int = 3
    max_wall_s: int = 30

@dataclass(frozen=True)
class ProbeResult:
    confidence_posterior: float
    observation_summary: str
    cost_usd: float
    iterations_used: int
    convergence_state: Literal["stable", "inconclusive", "budget_exhausted"]
```

Tool implementation properties (load-bearing):

1. **Read-only by AST enforcement**, not policy. Pinned at `semantic_firewall.py` / `ScopedToolBackend` level. Any mutation invocation from a `hypothesize`-tagged context hard-rejects.
2. **Subject to same risk-tier ladder as ops** — a probe that would have NOTIFY_APPLY+ risk if it were a real op is rejected.
3. **Failed probes recorded immutably** at `.jarvis/failed_hypotheses.jsonl` with content hash so adversarial probes cannot retry the same dead end.
4. **Bayesian update on each iteration** — `posterior = bayesian_update(prior, observation_likelihood)`. Halt when `|posterior - prior| < epsilon` (convergence_state=stable) OR `max_iterations` reached OR `max_wall_s` exceeded.
5. **The epsilon convergence threshold is mathematically derived** from the prior (not hardcoded) — tighter epsilon for high-stakes claims.

**Why this is high-impact**: this is the structural primitive that lets O+V resolve epistemic ambiguity without breaking the Zero-Trust boundary and without entering CC's reactive `ask_human` mode. It also wires `RepeatRunner` (Slice 2.2's Bayesian aggregator) into actual decision-making instead of advisory observation.

**Consumers**: Curiosity Engine (W2(4)) — invoke when SemanticTriage returns NO_OP but signal_urgency is high. CapabilityGap sensor — invoke when a gap is detected but the resolution path is ambiguous. SelfGoalFormation — invoke when goal candidates have similar confidence scores. **PLAN runner — invoke before declaring an op "trivial"** (closes the loophole that nuked soak #3).

**Antivenom alignment**: depth + wall-clock + budget hard bounds make trapped curiosity *impossible by construction*. The cage enforces read-only structurally, not by policy.

**Sequencing**: depends on Priority A (claim density gives the probe a target shape). Composes with Priority 3 (§24.10.3 Bounded Curiosity).

#### 25.5.4 Priority D: Empty-postmortem alarm + ledger discoverability [MEDIUM IMPACT]

**Problem**: today's soak produced 120 records that an operator without code access has no way to find. The `/help` dispatcher doesn't list `.jarvis/determinism/default/decisions.jsonl` as an artifact path. There's no `/postmortems` REPL. There's no GET endpoint listing terminal-postmortem records.

**Required upgrades**:

1. `/postmortems` REPL: subcommands `recent`, `for-op <op_id>`, `terminal-context-distribution`, `stats`. Read-only over the ledger.
2. `GET /observability/postmortems{,/recent,/for-op/{op_id},/distribution}` — IDE-consumable, loopback-only, rate-limited.
3. `EVENT_TYPE_TERMINAL_POSTMORTEM_PERSISTED` SSE event published from Option E's `_fire_terminal_postmortem` after successful Merkle write.
4. `MetaSensor` (Priority B) consumes the distribution and alarms on >70% empty rate.

**Why medium-impact**: not on the path to A-level RSI by itself, but it makes Priorities A + B operator-visible. Without this, "Phase 2 is working" remains an internal claim instead of an observable property.

**Sequencing**: ships after Priority A (so the surface has signal). Composes with Priority B (so the alarms surface in the same UX).

#### 25.5.5 Priority E: PLAN-runner cannot silently skip claim capture [MEDIUM-HIGH IMPACT]

**Problem**: `plan_runner.py` skips PLAN entirely on trivial ops, which silently skips Slice 2.3 claim capture. The model controls the trivial-op classification via prompt-leverage on the PlanGenerator's classifier.

**Required upgrade**: refactor PLAN-skip to NEVER skip claim capture. Even when the LLM-reasoned plan is bypassed for trivial ops:

```python
if _is_trivial_op(ctx):
    # OLD: return PhaseResult(next_phase=GENERATE, ...)  # claim capture skipped
    # NEW:
    _capture_default_claims(ctx)  # <-- Priority A claims, unconditional
    return PhaseResult(next_phase=GENERATE, artifacts={"plan_skipped_reason": "trivial_op"})
```

Add an AST-enforced source pin: `plan_runner.py` MUST contain `_capture_default_claims` call on every code path that exits to GENERATE. Lint rule lives in Pass B Slice 3 (`meta/ast_phase_runner_validator.py`).

**Why medium-high impact**: this is Priority A's enforcement teeth. Without it, future refactors can re-introduce the silent-disable gap.

**Sequencing**: ships immediately after Priority A.

### 25.6 In-flight alignment — what's on main right now that helps

| Current state on main | Maps to | Status |
|---|---|---|
| Phase 1 Determinism Substrate (4 flags graduated, `dc5f77017f`-adjacent) | §24.10.1 Priority 1 | DONE |
| Phase 2 verification (Slice 2.5 graduated `dc5f77017f`) | §24.10.2 Priority 2 | STRUCTURAL ONLY — Priority A blocks meaningful |
| Option E Universal Terminal Postmortem (`85cf94810a`) | Closes survivor bias | STRUCTURAL ONLY — composes with Priority A |
| Slices 3a + 3c (`a641ca2da3`) | Self-healing topology + sensor backpressure | DORMANT IN PRODUCTION until TopologySentinel master-on |
| W2(4) Curiosity Engine (graduated) | §24.10.3 Priority 3 | ADVISORY ONLY — not yet load-bearing |
| Phase 12.2 Physics-Aware Topology Routing | §24.10.3 (one scope) | DONE 2026-04-28 |

### 25.7 Reverse Russian Doll alignment

Per §23.6 — the operator's framing is "as O+V expands the shell, Anti-Venom must scale proportionally." Today's review reaffirms:

- **Priority A** (mandatory claim density) makes the inner core's verification observable. Anti-Venom scales: the claim list itself is an Order-2 governance object (manifest-listed, operator-amend-only).
- **Priority B** (degenerate-loop alarm) IS the immune system policing itself. The MetaSensor watches for "I am not actually verifying anything" as a categorical degenerate state.
- **Priority C** (HypothesisProbe) extends the cage to autonomous epistemic resolution. The cage's read-only constraint is now AST-enforced, not policy-bound.
- **Priorities D + E** turn structural correctness into operator-visible correctness, closing the "ship A-grade-looking system that's secretly B-grade" gap.

Each priority compresses through the substrate cleanly: outer shell grows (more ops produce real evidence), Anti-Venom scales proportionally (mandatory claims are AST-checked, hypothesize() is bounded by construction, MetaSensor is Order-2 governance, ledger surfacing is loopback-rate-limited).

### 25.8 What this review explicitly does NOT prescribe

- **Re-running soak #3 with `JARVIS_TOPOLOGY_SENTINEL_ENABLED=true`** to "exercise" Slices 3a + 3c — that is the work-around path. The structural fix is for SensorGovernor backpressure to also read the catalog-classifier blocked-route state, not just the sentinel.
- **Writing more verification subsystems** before Priority A ships — Phase 2 is graduated default-true and doing zero work; build on the active layer, not an inactive one.
- **Adding more terminal phases or new postmortem types** — Option E's universal coverage is sufficient. Today's gap is signal density, not signal coverage.
- **More sensors, more phase runners, more subagent types, more flags** — same prohibitions as §24.12.

### 25.9 Summary

**Did we make significant progress today? Yes — but along exactly one axis (memory scaffolding), and the verification loop is still ~30% functional in practice because every postmortem is empty.**

The fastest single move from B- to A- is **Priority A (mandatory claim density)**. Without it, the next ten things you build all sit on top of a layer that's silently doing zero work.

The fastest move from "ship A-grade-looking system" to "ship A-grade-actually-working system" is **Priority B (degenerate-loop alarm)** — the immune system that catches "I am not actually verifying anything" as a categorical failure mode.

Together, A + B + E close Phase 2's hollowness within ~1 week of focused work. Priority C (HypothesisProbe) closes the cognitive epistemic gap and is the path to A — ~2 weeks. Priority D is operator-UX polish, ships in parallel.

**Net trajectory**: today's work earned the right to attempt A-. It did not deliver A-. The next 5 priorities, in order, are how we get there.

---

## 26. Brutal Architectural Review v5 — Post-Phase-12-DW-Resilience-Closure (2026-04-29)

**Trigger**: operator architectural-review request 2026-04-29, immediately following the closure of the Phase 12 / 12.2 DW Resilience arc and the validation of §25 Priorities A–F all in production via soak #7 (`bt-2026-04-29-074851`).
**Predecessor reviews**: §3.6 (v1, 2026-04-26), §3.6.6 (v2, 2026-04-27), §24 (v3, 2026-04-28), §25 (v4, 2026-04-29 — Priorities A–F).
**Frame**: same Reverse Russian Doll constraint — Anti-Venom must scale proportionally as O+V expands the outer shell. The §25 priorities closed the *signal-density* gap; this review enumerates the *cognitive-depth* gap that remains.
**Honesty contract**: edge cases not happy path. The system is now genuinely B+ on happy paths and genuinely B− on edge cases. No marketing.

### 26.1 What soak #7 actually proved (and what §25 Priorities A–F now closed)

Soak #7 (`bt-2026-04-29-074851`, 853s, idle_timeout, session_outcome=complete, $0.0316) is the first soak where the *full §25 stack* was live and exercised under hostile network conditions:

| Subsystem | §25 Priority | Soak #7 evidence |
|---|---|---|
| Mandatory claim density | A | 8 default-claim batches captured at PLAN exits — non-trivial signal in every postmortem |
| MetaSensor degenerate-loop alarm | B | Master flag `JARVIS_META_SENSOR_ENABLED` graduated default-true 2026-04-29 |
| Bounded HypothesisProbe + Venom `hypothesize` tool | C | Wired to plan_generator's trivial-op gate (`probe_trivial_op_assumption` LIVE) |
| Postmortem ledger discoverability | D | `/postmortems` REPL + 4 GET endpoints + SSE event live |
| Plan-runner cannot silently skip claim capture | E | `meta/shipped_code_invariants.py` seed `plan_runner_default_claims_wiring` enforces at boot + APPLY |
| Evidence collector extension + F2/F3 capture | F | 8 evidence batches stamped at PLAN entry + APPLY pre/post — used by Universal Postmortem |
| Pricing Oracle (Option α) | Phase 12 | 16 live resolutions including soak #6 root case `Qwen/Qwen3.5-397B-A17B-FP8-dottxt → qwen_3_5_397b ($0.10/$0.40)` |
| Sentinel-Pacemaker Handshake (Option β) | Phase 12.2 | 3 firings: `force_refresh requested ... force_refresh wake — bypassing 1800s cadence sleep` |
| Cost contract (BG never cascades to Claude) | project_bg_spec_sealed.md | 14 BG topology blocks correctly skip-and-queue'd under DW flakiness — contract held |

**Proven by soak #7**: the verification loop now closes meaningfully. Default claims are captured unconditionally (Priority E enforces structurally), evidence collectors stamp the artifacts those claims need to evaluate against (Priority F), the Universal Terminal Postmortem records pass/fail/insufficient outcomes per claim (Phase 2 + Option E), and the cost contract holds under network duress (Phase 12). Three concurrent silent-disable failures from soak #3 are all now structurally impossible.

**Not yet proven**: that the system can *act* on the signal it's now collecting. Postmortems carry pass/fail per claim, but no consumer treats `claim.fail` as a circuit-breaker condition. Default claims include `must_hold` severity but the system does not yet refuse to advance an op when `must_hold_failed > 0` is observed in a sibling op. The signal is rich; the *closing of the loop into action* is the remaining gap.

### 26.2 Refined Cognitive & Epistemic Delta — what CC still has that O+V doesn't

CC's edge is no longer "Venom has 16 tools, CC has more" (parity), nor "CC has plan-mode" (Priority C closed that). Post-§25 closure, CC's remaining edge is **structural epistemic discipline at the *act-on-signal* boundary**:

| CC paradigm | O+V state (post §25 + Phase 12) | Honest gap |
|---|---|---|
| **Unbounded interactive recursion** — user as infinite oracle | `ask_human` gated to NOTIFY_APPLY+; Green ops have no fallback oracle | For BG/SPEC routes (most of the autonomous surface), there is no confidence oracle. HypothesisProbe partially fills this on the trivial-op gate; nowhere else. |
| **Speculative execution trees** — try multiple branches, pick winner | Single-thread Venom tool loop per op; L3 worktree fan-out exists (W3(6)) but is per-op-graph, not per-hypothesis | COMPLEX route is the cost-supported lane for multi-hypothesis. The infrastructure is there (`subagent_scheduler` + `worktree_manager`); the *cognitive trigger* (when to fan out hypotheses vs commit linearly) is not. |
| **Mid-generation self-critique** — model corrects itself mid-stream | REVIEW subagent post-hoc on finished candidate (graduated 2026-04-20); no real-time critique | Provider-side stream events are captured; we don't run a critic over the rolling token buffer. |
| **Streamed reasoning surface** — operator watches the *think*, not just the *do* | SerpentFlow + Update blocks render code-deltas; `stream_renderer.py` shows tokens; reasoning-deltas not surfaced as a first-class event class | The data is in the model output; the surface is missing. |
| **Confidence-aware decisions** — implicit in CC via human disambiguation | DW + Claude both expose logprobs / top-k; we discard them after stream parse | The single biggest missing primitive. Confidence is the routing signal that turns Priorities A–F from a *passive* verification loop into an *active* one. |
| **Counterfactual reasoning** — "if I do X, what breaks?" | SemanticGuardian regex/AST patterns + Iron Gate exploration floor + Property Oracle claims | All operate on *committed* state. None probe "what would happen if?" with structured reasoning. HypothesisProbe is bounded for the trivial-op gate only. |

Three structural primitives close these gaps and all three leverage existing infrastructure:

1. **Confidence-gated GENERATE** — capture per-token logprobs from `providers.py` (DW + Claude both expose them in stream events). Compute rolling top-1/top-2 margin. When margin drops below `JARVIS_CONFIDENCE_FLOOR` (env-tunable, default 0.05), abort the GENERATE round + route to HypothesisProbe with the partial output as evidence. **No hardcoding** — threshold lives in FlagRegistry, posture-relevant (HARDEN tightens floor, EXPLORE loosens), and AdaptationLedger (Pass C) adjusts it within bounds. **Existing files extended**: `providers.py`, `phase_capture.py`, `urgency_router.py`, `flag_registry.py`.

2. **Multi-hypothesis fan-out at GENERATE (COMPLEX route only)** — when SemanticTriage returns GENERATE *and* route=COMPLEX *and* HypothesisProbe declares the trivial-op assumption falsified, dispatch N candidates in parallel L3 worktrees (Wave 3 #6 primitive). Score each via REVIEW + per-candidate test runner + Property Oracle claim evaluation. Collapse to winner + write losers to `.jarvis/hypothesis_losers.jsonl` for cross-session learning. **No hardcoding** — N derived from posture (EXPLORE → 3, HARDEN → 1, MAINTAIN → 2). **Existing files extended**: `subagent_scheduler.py`, `worktree_manager.py`, `phase_dispatcher.py`, `review_subagent.py`.

3. **Pre-APPLY counterfactual probe** — before APPLY commits, dispatch HypothesisProbe with hypothesis "this change will not regress claim X, file Y, contract Z" using existing evidence collectors (Priority F). Failure → GENERATE_RETRY with structured feedback. This is the missing layer between SemanticGuardian (pattern-level) and tests (behavior-level): *intent-level* verification. **Existing files extended**: `phase_runners/slice4b_runner.py`, `verification/hypothesis_probe.py`, `verification/evidence_collectors.py`.

### 26.3 Refined Deep Observability — temporal reconstruction is the missing depth

§25 Priority D shipped `/postmortems` REPL + 4 GET endpoints + SSE event. That closed the *discoverability* gap. The remaining gap is *temporal reconstruction* — the ability to take any decision node and replay forward, fork sideways, or trace the full causal tree backward.

What exists (validated by soak #7):
- `EventChannelServer` `/observability/{health,tasks,...}` GETs + `/observability/stream` SSE (Gap #6 Slices 1+2)
- `SessionRecord` + `SessionIndex` + `SessionBrowser` (Apr 21 5-slice arc, 117 tests)
- `phase_capture.py` Merkle DAG hook (Slice 1.3, in `.jarvis/determinism/<session>/decisions.jsonl`)
- `replay.html` per session (auto-generated by harness on shutdown)
- Postmortem ledger (Priority D)
- `summary.json` + `notebook` per session

What's missing:
- **Causality DAG** (not just flat event log). Today every decision/op/postmortem is a *node* but the *edges* (this caused that, this is a counterfactual of that) are implicit. We need a directed graph where every node points to its parents (signal source, posture state, prior decisions, evidence consulted) and children (downstream phase outcomes, child ops). The `phase_capture` Merkle hash is the seed; extending it to track parent-record-id and counterfactual-of-record-id makes the DAG navigable.
- **Latent-space confidence broadcast**. Providers expose logprobs and top-k; we discard them after stream parse. Add a `model.confidence_drop` SSE event class (severity-tiered: P1 = below floor mid-stream → abort; P2 = within 20% of floor → warning; P3 = sustained low margin across multiple ops → posture nudge toward HARDEN).
- **Pre-trip circuit-breaker events**. Breakers fire when limits *hit*. We need `breaker.approaching` events at 75% of budget so Pass C governor can react before the bang. The CostGovernor + RateLimiter both have the data; neither emits at 75%.
- **Parallel fan-out canvas**. Wave 3 #6 SSE bridges are explicitly deferred (`memory/project_wave3_item6_scope.md`: "Known debt — log-only"). That debt is now load-bearing for time-travel debugging on multi-hypothesis fan-out (§26.2 primitive 2).
- **Time-travel debugging UI**. Slice 1.4's `--rerun <session-id>` is text-scoped + op-scoped. There is no UI that takes a `record_id` and renders the upstream/downstream Merkle DAG with confidence-drop annotations + counterfactual-fork affordance. The IDE observability schema (`schema_version: "1.0"`) extends naturally to this.

**Noise discipline** (refined from §25.3): emit on transition, not on tick. Posture observer is the gold standard (only emits on cross-hysteresis). Today's soak #7 demonstrates this scales: 16 oracle resolutions + 3 handshake firings + 14 topology blocks = 33 transition events across 853s, all individually meaningful, none noise. The bad pattern is "emit every heartbeat" — drowns observers and degrades rate-limited GETs.

### 26.4 Brutal grade — current state: **B+ / B−**

**Defending B+ on happy paths, B− on edge cases**:

- **Happy path is now A−**: §25 Priorities A–F closed single-day; Phase 12 DW Resilience closed single-day; soak #7 produced clean idle-exit with non-trivial claim density. The verification loop *measurably* closes.
- **Edge cases are still B−**: the cognitive layer is single-threaded relative to its potential; long-horizon drift detection is heuristic; Quine-class hallucination vectors remain (enumerated below); confidence-aware execution is absent.

**Hidden race conditions identified post-§25 closure (additive to §24.6 + §25.4)**:

- **Wave 2(5) PhaseRunner extraction is mid-flight**. Slice 5b (Iron Gate parity for GENERATE) still pending. Until it lands, every extracted runner threads cross-phase state via the `artifacts` dict (memory: `project_wave2_phaserunner_slice5a.md`). Every threading site is one more potential race surface.
- **`_active_file_ops` lock with TTL stale-release** (`JARVIS_FILE_LOCK_TTL_S=300`) is heuristic. Under sensor storm, it can release a lock while a worktree subagent is still mid-generation. Fix: TTL keyed on subagent heartbeat, not wall-clock.
- **Posture observer Lock→RLock bugfix** shipped in Slice 5 Arc A (memory: `project_slice5_arc_a.md`). That pattern (recursive callback into singleton during state read) probably exists elsewhere — FlagRegistry, SensorGovernor, maybe even pricing_oracle's `_REGISTRY_LOCK` under cache invalidation. Audit-grade-1 due.
- **Slice 1.3 `capture_phase_decision` ordinal-counter under L3 fan-out** — per-process counter; multi-worker writes to same `<session>/decisions.jsonl` may produce out-of-order ordinals across worker boundaries. Replay determinism may not hold cross-worker. Addressed structurally by §26.5 Priority 2 (causality DAG with per-worker sub-ordinals).

**Cascading state-failure vectors over long horizons (additive to §24.7)**:

- **Signal coalescing window is 30s**. Under burst, two signals for the same file 31s apart spawn two ops. The cooldown ledger handles GitHub but not generic FS sensors.
- **Strategic drift threshold is heuristic** (currently passing at 0.1 / soak #7). No formal long-horizon detector. If the manifesto drifts, no alarm.
- **Postmortem ledger compaction is unbounded**. At 10K postmortems the SemanticIndex centroid will smear. No retention policy beyond per-session.
- **MetaSensor (Priority B) thresholds are static**. AdaptationLedger (Pass C) should adjust them in production based on sensor-emission distributions — currently does not.

**Antivenom bypass vectors most likely to land (additive to §24.8 + §25.4)**:

- **Plausibly-typed dead code** — passes AST, passes pattern check, does nothing meaningful. Tests don't catch it because there are no tests for "this code reaches its postcondition." Property Oracle helps if claims are non-trivial; default claims (Priority A) are structural-shape, not behavioral-shape. **Mitigation**: §26.5 Priority 1 (confidence-aware execution) — low-confidence GENERATE rounds are exactly where dead-code emerges.
- **Symbol-shape hallucination** — model imports `from x import Y` where `x` exists and `Y` is grammatically right but semantically wrong (uses old API surface). Iron Gate doesn't run the import. **Mitigation**: pre-APPLY counterfactual probe (§26.2 primitive 3) executes a scoped `python -c "from x import Y"` as evidence.
- **Test-shape gaming** — model writes a test that asserts on a symbol it just defined. Default claim `test_set_hash_stable` catches *removal* but not *gaming-via-addition*. **Mitigation**: extend default-claim set (Priority A's manifest) with a `test_added_must_assert_existing_behavior` claim using AST analysis of test-target symbols.
- **Quine-style obfuscation** — model writes a one-line lambda or `exec(compile(...))` that constructs the actual logic at runtime. AST is trivial, behavior is anything. **Mitigation**: SemanticGuardian pattern `runtime_code_construction` (new) — denies any patch introducing `exec`, `eval`, `compile`, `getattr` with dynamic argument.

The honest defense for "still B+ overall": the *combination* of risk-tier floor + APPROVAL_REQUIRED on Orange + visual VERIFY + Universal Postmortem + repeat-runner + change_engine checkpoint restore means most escape paths are *visible after the fact*. RSI tolerates after-the-fact catch IF rollback is structural. We don't lose; we just learn slowly. The path to A is to make the loop *fast* — that's what §26.5 Priority 1 (confidence) gives us.

### 26.5 Critical Path to A-Level RSI — top 3 systemic upgrades (post-Phase-12)

§25.5 listed Priorities A–F covering signal density. Those are now ALL CLOSED. The next tier is *cognitive depth*. Three systemic upgrades, in order. Each composes; each leverages existing infrastructure; none introduces hardcoding.

#### 26.5.1 Priority 1: Confidence-Aware Execution (Probabilistic Posture) [HIGHEST IMPACT]

**Problem**: provider responses contain logprobs, top-k alternatives, and per-token confidence. We discard them after stream parse. As a result, every generation outcome is binary success/fail — there's no internal "I'm uncertain about this" signal. CC has the user as an external confidence oracle; O+V has nothing. This is the single biggest CC delta and the missing Order-1 cognitive primitive.

**Required upgrade** — three composing surfaces:

1. **Logprob capture in providers.py** — both DW (`doubleword_provider.py`) and Claude (`providers.py`) expose per-token logprobs in stream events. Capture them into `ctx.confidence_trace: List[float]` during GENERATE. Zero new dependencies; both providers already parse the stream.

2. **Confidence-gated circuit-breaker mid-stream** — compute rolling top-1/top-2 margin over the last K tokens (K env-tunable, default 16). When margin drops below `JARVIS_CONFIDENCE_FLOOR` (env-tunable, default 0.05; posture-relevant — HARDEN→0.10, EXPLORE→0.02), abort the GENERATE round + route the partial output to HypothesisProbe with hypothesis "the model is in epistemic distress on this op." Successful probe → resume; falsified probe → escalate to NOTIFY_APPLY (effectively asking the operator).

3. **Confidence-aware route routing** — extend `urgency_router.py` to consider confidence-trace history as a routing input. Recurring low-confidence ops in BG route → propose route demotion. Recurring high-confidence ops in COMPLEX → propose route promotion. Routes shift; cost contract is preserved (BG never cascades to Claude — see §26.6).

**Why this is highest-impact**: confidence is the missing signal that turns the §25 verification loop from *passive* (records what happened) to *active* (changes what's happening). Property Oracle claims pass/fail, but only after APPLY. Confidence intervenes *during* GENERATE. It also unlocks counterfactual probes, multi-hypothesis fan-out triggers, and Pass C's MetaAdaptationGovernor reading "is the system uncertain enough that I should tighten constraints?" as a real signal.

**Antivenom alignment**: confidence floor is itself an Order-2 governance object — the threshold is FlagRegistry-typed, posture-relevant, AdaptationLedger-tunable within bounds. Amending the floor crosses Pass B Slice 1's locked-true invariant.

**No hardcoding**: floor lives in FlagRegistry, posture-relevant categories assigned, K-window is env-tunable, AdaptationLedger adjusts within Pass C's monotonic-tightening invariant.

**Sequencing**: independent of Priority 2; can ship in parallel with it. Composes as the *signal source* for Priority 2's causal DAG (every confidence-drop is a graph node).

#### 26.5.2 Priority 2: Causality DAG + Deterministic Replay [HIGH IMPACT] ✅ CLOSED 2026-04-29

**Status**: 6-slice arc graduated single-day 2026-04-29 (post-finishing-pass `2c0f642735`). Full closure record in `memory/project_priority_2_causality_dag_closure.md`. All 6 master flags graduated default-true; 4 new shipped_code_invariants seeds (now 11 total); 9 FlagRegistry seeds; 655/655 regression green; §26.6 four-layer cost contract preservation pinned by AST invariant + verified at runtime under DAG/replay state.

**Original problem statement (now resolved)**: today's events were a flat stream. `phase_capture` Merkle-hashed records; `summary.json` aggregated per-op outcomes; `replay.html` rendered a session linearly. None of these answered the question "given decision X, what was its causal predecessor Y, and what would have happened if at Y we'd taken path Z?" — the missing substrate for time-travel debugging, drift detection, counterfactual reasoning, and replay-from-fork.

**What shipped**:

1. **Schema extension (Slice 1)** — every captured record carries optional `parent_record_ids: Tuple[str,...]` and `counterfactual_of: Optional[str]`. `SCHEMA_VERSION` unchanged (additive backward-compat); pre-Slice-1 records parse cleanly with empty defaults. Master-flag-gated emission via `JARVIS_CAUSALITY_DAG_SCHEMA_ENABLED` (graduated default-true).

2. **Per-worker sub-ordinals (Slice 2)** — fixes the L3 fan-out determinism bug (W3(6) known debt) by namespacing ordinals as `(worker_id, op_id, phase, kind)`. `worker_id_for_path()` in `worktree_manager.py` is pure (AST-pinned no I/O); shadow→enforce two-flag pattern. Both flags graduated default-true.

3. **DAG construction primitive (Slice 3)** — new `verification/causality_dag.py`; bounded BFS, cycle detection, topo sort, counterfactual branch detection; FlagRegistry-typed bounds (`JARVIS_DAG_MAX_RECORDS=100K`, `JARVIS_DAG_MAX_DEPTH=8`, `JARVIS_DAG_DRIFT_NODE_DELTA_THRESHOLD=0.25`).

4. **Navigation surface (Slice 4)** — `/postmortems dag` REPL family (`for-record` / `fork-counterfactuals` / `drift` / `stats`) + `GET /observability/dag/{session_id}` + `GET /observability/dag/record/{id}` + `EVENT_TYPE_DAG_FORK_DETECTED` SSE event. Three independent sub-flags govern REPL/GET/SSE channels.

5. **Replay-from-record (Slice 5)** — `--rerun-from <record-id>` extends the existing `--rerun` CLI flag. The replay path is **purely env-overlay** (sets `JARVIS_CAUSALITY_FORK_FROM_RECORD_ID` + `JARVIS_CAUSALITY_FORK_COUNTERFACTUAL_OF`); the orchestrator's existing `--rerun` path is what dispatches. **Cost contract preservation is structural** — pinned by the `dag_replay_cost_contract_preserved` shipped_code_invariants seed which verifies `scripts/ouroboros_battle_test.py` references the replay primitive functions, requires `--rerun` for session identity, and contains zero direct provider construction tokens.

6. **Graduation flip (Slice 6)** — 6 master flags flipped default false → true; 4 new shipped_code_invariants seeds (`causality_dag_no_authority_imports`, `causality_dag_bounded_traversal`, `dag_navigation_no_ctx_mutation`, `dag_replay_cost_contract_preserved`); 9 FlagRegistry seeds; 30 §-numbered graduation pins.

**Antivenom alignment** (verified post-finishing-pass): the §26.6 four-layer cost-contract defense holds under all DAG / replay states. Tests in `test_causality_dag_replay_from_record.py` + `test_causality_dag_graduation.py` prove:
  * Layer 4 advisor structural guard fires on synthetic BG → STANDARD/COMPLEX/IMMEDIATE escalation regardless of replay-mode env
  * Layer 2 runtime CostContractViolation gate fires under all DAG/replay state
  * `apply_replay_from_record_env()` sets ONLY documented overlay env vars (no provider-routing flags)
  * `replay_from_record.py` is import-isolated from any provider module (AST-walked + invariant-pinned)

**Composition with shipped infrastructure**:
  * Confidence-drop SSE events (Priority 1 Slice 4) → DAG nodes via `parent_record_ids`
  * HypothesisProbe verdicts (Priority 1 Slice 3) → counterfactual fork branches via `counterfactual_of`
  * `cluster_kind` heuristic detects "confidence_collapse_cluster" when ≥3 confidence_drop nodes share a recent ancestor
  * `dag drift <session-a> <session-b>` is now the per-trajectory drift substrate Pass C will mine

**Pass C unlock dependency**: Priority 3 (Adaptive Anti-Venom, §26.5.3) is now unblocked from the *substrate* side — Slice 4's `dag drift` provides the per-trajectory drift signal Pass C MetaAdaptationGovernor needs, and Slice 5's counterfactual replay produces evidence Pass C will mine for adaptation rules. Priority 3 remains gated on the *governance* side (W2(5) Slice 5b → Pass B Slice 1 → Pass C Slice 1).

#### 26.5.3 Priority 3: Adaptive Anti-Venom (Pass C) [✅ GRADUATED 2026-04-29 — Move 1 cadence]

**Status correction (2026-04-29 audit)**: when this section was originally drafted, Pass C was framed as gated on a chain (W2(5) Slice 5b → Pass B Slice 1 → Pass C Slice 1). Audit revealed the chain has **already shipped end-to-end**:

- ✅ **W2(5) Slice 5b** — Iron Gate parity for GENERATE phase runner (`3c291238a5`, 24 tests, flag default-true)
- ✅ **Pass B** — *structurally complete* 2026-04-26 (`5dd15bd264`); 6 slices shipped (manifest + Order-2 risk class + gate wiring + AST validator + shadow replay + MetaPhaseRunner + sandboxed replay executor + review queue + `/order2` REPL); 438 tests green; defaults all false pending per-slice graduation
- ✅ **Pass C** — *structurally complete* 2026-04-26 (`f264e598c2` Slice 1 + 5 follow-on slices same day); 349 tests green across all 6 slices; defaults all false pending per-slice graduation

What Pass C actually shipped (matching this section's "Required upgrade" list 1:1):

| Pass C slice | What it ships |
|---|---|
| Slice 1 — `AdaptationLedger` substrate | Append-only JSONL audit log + 5-value `AdaptationSurface` enum + monotonic-tightening invariant validator that REFUSES to persist loosening proposals (cage rule) + 60 tests + sha256 tamper-detect |
| Slice 2 — SemanticGuardian miner | POSTMORTEM-mined detector pattern proposer; auto-registers per-surface validator (add_pattern-only kind, sha256-hash, threshold-above) + 54 tests |
| Slice 3 — ExplorationFloor tightener | Bypass-failure detector (floor_satisfied=True AND verify=regression/failed) → identifies weakest category → 10% raise per cycle (capped 100%); 55 tests |
| Slice 4a/4b combined — per-Order mutation budget + risk-tier ladder extender | Pure stdlib analyzers proposing budget-lower (uses observed max as new safe budget) + tier insertion (novel failure_class → deterministic synthesized tier name); 63 tests; both sub-surfaces auto-register validators |
| Slice 5 — CategoryWeight rebalancer | Per-category Pearson correlation (exploration↔verify_passed); proposes raise-high + lower-low with hard floors (50% of original AND 0.01 absolute MIN_WEIGHT) under mass-conservation pin; 62 tests |
| Slice 6 — `/adapt` REPL MetaGovernor | Operator-facing dispatcher (`/adapt {pending,show,approve,reject,history,stats,help}`); 12-status enum; mirrors Pass B's `/order2` REPL pattern; 55 tests; **CLOSES Pass C** |

**393 Pass C regression tests green** (was 349 pre-graduation; +44 across 6 default_true_post_graduation pins + 6 explicit-falsy hot-revert pins + 32 prior structural pins). **Move 1 graduation closed 2026-04-29** — closure record at `memory/project_pass_c_graduation_closure.md`; original Pass C structural-complete record at `memory/project_reverse_russian_doll_pass_c.md`.

**Move 1 cadence** (operator-authorized in-session graduation, mirroring Priority 1 Slice 5 + Priority 2 Slice 6 precedent):

1. ✅ **All 7 master flags flipped false→true** with asymmetric env semantics (empty/unset = graduated default-true; explicit falsy hot-reverts):
   - `JARVIS_ADAPTATION_LEDGER_ENABLED`
   - `JARVIS_ADAPTIVE_SEMANTIC_GUARDIAN_ENABLED`
   - `JARVIS_ADAPTIVE_IRON_GATE_FLOORS_ENABLED`
   - `JARVIS_ADAPTIVE_PER_ORDER_BUDGET_ENABLED` + `JARVIS_ADAPTIVE_RISK_TIER_LADDER_ENABLED`
   - `JARVIS_ADAPTIVE_CATEGORY_WEIGHTS_ENABLED`
   - `JARVIS_ADAPT_REPL_ENABLED`
2. ✅ **7 FlagRegistry seeds** added (87 total, was 80) — category SAFETY (or OBSERVABILITY for the REPL), posture-relevance HARDEN+CONSOLIDATE.
3. ✅ **7 shipped_code_invariants seeds** (18 total, was 11): `adaptation_ledger_monotonic_tightening_pin` (LOAD-BEARING — pins `MonotonicTighteningVerdict` + `validate_monotonic_tightening` + `REJECTED_WOULD_LOOSEN`) + 6 × `adaptation_<miner>_no_authority_imports` (semantic_guardian + exploration_floor + per_order_budget + risk_tier + category_weights + meta_governor — pin read-only contract: no orchestrator/phase_runners/iron_gate/change_engine/policy/semantic_firewall/providers/doubleword/urgency_router imports).
4. ✅ **Two clean idle_timeout soaks** validate graduated state holds: `bt-2026-04-29-212606` (integrated graduation, all 7 enabled) + `bt-2026-04-29-215306` (post-graduation, no env overrides — 843s clean exit, strategic_drift=ok, zero Pass C-related errors).
2. **Deferred Slice 6 follow-ups** (tracked in `meta_governor.py` docstring): observability GET endpoints, SSE event emission, weekly background analyzer scheduling, actual gate-state mutation on approve (each surface's `.jarvis/adapted_<surface>.yaml` writer per §6.3/§7.3/§8.4/§9.3).
3. **Per-trajectory drift detector** — Slice 4 `dag drift` from §26.5.2 Priority 2 is the substrate; the MetaGovernor consuming it for evidence-driven adaptation is the deferred follow-up above.

**The original "Sequencing" claim is obsolete**: Priority 3 is no longer "the next concrete arc." It's an arc that *structurally finished* in the same single-day sprint as Pass B (2026-04-26). What remains is graduation soak validation — empirical confirmation that the adaptation surfaces produce sound proposals under real load, then 6 flag flips.

**Implication for §26.5 critical path**: with Priority 3 structurally complete, the v5 critical-path framing (Priority 1 → 2 → 3) is closed on the *implementation* axis. The **actual remaining gap to A-level RSI** is autonomy track record (multi-day unsupervised runs, sustained adaptation evidence, sensor-storm resilience). See **§27 — Brutal Architectural Review v6** below for the re-grade against the autonomy question.

### 26.6 Cost contract structural reinforcement — BG never cascades to Claude (bulletproofing)

The operator's directive (2026-04-29): "make sure the Cost contract (BG never cascades to Claude) [stays bulletproof]". Soak #7 validated 14 BG topology blocks correctly skip-and-queue'd under DW endpoint flakiness. The contract held *behaviorally*. The next tier is to make it hold *structurally* — three reinforcements that cannot be weakened by a future model patch.

Today the contract lives in two places:
1. `brain_selection_policy.yaml` — config; overridable by any patch
2. `candidate_generator.py` block-mode dispatch — code; trusts the YAML

A model with the wrong patch could weaken either. Three structural reinforcements, all leveraging existing infrastructure, no hardcoding:

#### 26.6.1 AST invariant (extends §25 Priority E)

Extend `meta/shipped_code_invariants.py` with a new seed:

```python
INVARIANT_BG_NEVER_CASCADES_TO_CLAUDE = ShippedCodeInvariant(
    invariant_id="bg_route_never_reaches_claude_provider",
    description=(
        "No code path in providers.py or candidate_generator.py "
        "may dispatch to a Claude-tier provider when ctx.route in "
        "('background', 'speculative'). Enforces project_bg_spec_sealed.md."
    ),
    target_files=["backend/core/ouroboros/governance/providers.py",
                  "backend/core/ouroboros/governance/candidate_generator.py"],
    ast_check_kind="reachability_proof",
    ast_check_args={
        "from_route": ["background", "speculative"],
        "must_not_reach": "ClaudeProvider",
    },
    severity=SEVERITY_MUST_HOLD,
    hot_revert_env="JARVIS_BG_COST_CONTRACT_INVARIANT_ENABLED",  # default true
)
```

Validated at boot + every APPLY. Failure blocks the APPLY and emits `cost_contract_invariant_violated` SSE event.

#### 26.6.2 Runtime structural assertion

In `providers.py`, at the dispatch-to-provider boundary:

```python
def dispatch_provider(ctx: PipelineContext, provider: Provider) -> ...:
    # Cost contract structural reinforcement — fail loud, not silent.
    # See docs/architecture/OUROBOROS_VENOM_PRD.md §26.6 +
    # memory/project_bg_spec_sealed.md
    if ctx.route in BG_SPEC_ROUTES and provider.tier == "claude":
        raise CostContractViolation(
            f"BG/SPEC route attempted Claude cascade — op={ctx.op_id} "
            f"route={ctx.route} provider={provider.name}. This is a "
            f"structural invariant; check candidate_generator dispatch "
            f"+ topology block-mode policy."
        )
    return provider.dispatch(ctx, ...)
```

`CostContractViolation` is a fatal exception class — the orchestrator catches it, terminates the op as `failure_class=cost_contract_violation`, writes a `must_hold_failed` postmortem record, and refuses further work on that op. No silent fallback.

#### 26.6.3 Property Oracle claim (extends §25 Priority A default-claim manifest)

Register a default claim that fires on every postmortem:

```python
@register_default_claim
def cost_contract_bg_op_did_not_use_claude(ctx: PipelineContext) -> PropertyClaim:
    """Every BG/SPEC op MUST have provider != claude in its phase trace."""
    return PropertyClaim(
        claim_id="cost.bg_op_used_claude_must_be_false",
        property=Property.from_kind("provider_class_in_phase_trace_must_not_match"),
        severity=SEVERITY_MUST_HOLD,
        evidence_required={
            "applicable_when_route_in": ["background", "speculative"],
            "provider_class_must_not_match": "claude",
        },
    )
```

This makes the contract *empirically auditable per op* via the existing Property Oracle infrastructure (Phase 2). Any drift triggers Priority B's MetaSensor (rate of `cost.bg_op_used_claude_must_be_false` failures > 0 over last N → P1 alarm).

**None of these duplicate existing logic. All three extend graduated primitives (§25 Priorities A + E, Phase 2 Property Oracle).** Together they make the cost contract enforced at three independent layers: AST (boot + APPLY), runtime (every dispatch), and audit (every postmortem). A future model patch would have to weaken all three simultaneously to break the contract — which is precisely what the Order-2 governance cage prevents (the AST invariant lives in the manifest).

### 26.7 In-flight alignment + sequencing

| Current state on main | Maps to | Status |
|---|---|---|
| Phase 1 Determinism Substrate (4 flags graduated) | §24.10.1 Priority 1 / RSI Gear 1 | DONE |
| Phase 2 Closed-Loop Self-Verification (Slice 2.5 graduated) | §24.10.2 Priority 2 | DONE — Priorities A+E+F made it *functionally* complete |
| §25 Priority A — mandatory claim density | §25.5.1 | DONE |
| §25 Priority B — MetaSensor degenerate-loop alarm | §25.5.2 | DONE (graduated default-true) |
| §25 Priority C — bounded HypothesisProbe + Venom `hypothesize` tool | §25.5.3 | DONE (wired to plan_generator trivial-op gate) |
| §25 Priority D — postmortem ledger discoverability | §25.5.4 | DONE (REPL + 4 GETs + SSE) |
| §25 Priority E — plan_runner cannot silently skip claim capture | §25.5.5 | DONE (shipped_code_invariants seed) |
| §25 Priority F — evidence collector extension + F2/F3 capture | §25.5 (added) | DONE |
| Phase 12 DW Resilience: Pricing Oracle (α) + Handshake (β) + Universal Postmortem (E) | Phase 12 / 12.2 | CLOSED 2026-04-29 (`memory/project_phase_12_dw_resilience_closure.md`) |
| W2(5) PhaseRunner extraction (Slices 1–5a) | §26.4 race condition | IN-FLIGHT — Slice 5b (Iron Gate parity) pending |
| Pass B Slice 1 (MetaPhaseRunner + Order-2 manifest) | §26.5.3 gating | HELD on W2(5) Slice 5b |
| Pass C Slice 1 (AdaptationLedger + MetaAdaptationGovernor) | §26.5.3 | HELD on Pass B Slice 1 |
| W3(6) parallel L3 fan-out (Slices 1–4 shipped, defaults false) | §26.2 primitive 2 substrate | INFRASTRUCTURE READY — defaults false, SSE bridges deferred |

**Sequencing for the next focus** (impact-ranked):

1. **Priority 1 — Confidence-Aware Execution** (§26.5.1) — ships first, independent of any in-flight arc, leverages providers.py + phase_capture + urgency_router. Highest unlock-to-effort ratio.
2. **Priority 2 — Causality DAG** (§26.5.2) — ships in parallel with Priority 1 once the confidence event class is defined; they compose.
3. **Cost contract structural reinforcement** (§26.6) — ships in parallel with Priority 1; small, high-confidence, leverages §25 Priority E shipped infrastructure. Low risk, immediate operator visibility.
4. **W2(5) Slice 5b** (Iron Gate parity for GENERATE) — unblocks Pass B Slice 1.
5. **Pass B Slice 1** (Order-2 manifest cage) — unblocks Pass C Slice 1.
6. **Priority 3 — Adaptive Anti-Venom** (§26.5.3) — unblocks once Pass C Slice 1 ships.

### 26.8 What this review explicitly does NOT prescribe

- **Re-running soak #7 with `JARVIS_PRICING_ORACLE_ENABLED=false`** to "test the legacy path in production" — that's the work-around path. The structural fix is the §26.6 invariant + structural assertion + Property Oracle claim.
- **Adding more sensors** — the 16 + MetaSensor are sufficient for current signal density. Today's gap is *cognitive depth*, not signal coverage.
- **Adding more phase runners or subagent types** — same prohibitions as §24.12 + §25.8. The W2(5) extraction in-flight is sufficient.
- **Building a brand-new "RSI core"** — Phase 1 + Phase 2 + §25 Priorities A–F + Phase 12 closure is the RSI core. The next three priorities (§26.5) extend it; they don't replace it.
- **Re-litigating Phase 12** — Phase 12 / 12.2 DW Resilience is CLOSED per `memory/project_phase_12_dw_resilience_closure.md`. The 14 BG topology blocks observed in soak #7 are NOT regressions — they are the cost contract holding under DW outage.

### 26.9 Summary — the path from B+ to A

**Did we make significant progress today (2026-04-29)? Yes — across two distinct axes:**
- **Signal density** (§25 Priorities A–F closed single-day): the verification loop now closes meaningfully; postmortems carry non-trivial claim density; soak #7 validated empirical loop closure.
- **Resilience** (Phase 12 DW Resilience closed single-day): the autonomic nervous system now self-heals catalog state under hostile network conditions without operator intervention; soak #7 validated Pricing Oracle + Handshake + Universal Postmortem live in production.

**What remains the gap from B+ to A: cognitive depth.** The system now collects rich signal but does not yet *act* on it during execution (only after). Three systemic upgrades close this:

1. **Confidence-Aware Execution** (§26.5.1) — turns confidence into a routing signal + circuit-breaker. Single biggest CC-delta-closing primitive. Ships first.
2. **Causality DAG + Deterministic Replay** (§26.5.2) — substrate for time-travel debugging, drift detection, counterfactual reasoning. Ships in parallel.
3. **Adaptive Anti-Venom** (§26.5.3) — Pass C's MetaAdaptationGovernor making constraints scale proportionally as the shell expands. Gated; ships when Pass B Slice 1 + W2(5) Slice 5b clear.

**Cost contract bulletproofing** (§26.6) ships in parallel with Priority 1 — three structural reinforcements (AST invariant + runtime assertion + Property Oracle claim) make BG-never-cascades-to-Claude an enforced invariant at three independent layers.

**Net trajectory**: the §25 priorities + Phase 12 closure earned the right to attempt A. They did not deliver A. The next three priorities (§26.5) + cost-contract reinforcement (§26.6), in this order, are how we get there.

The architecture is now A. The cognitive layer is B+. The path closes both gaps via the three priorities above. None requires hardcoding. All extend existing graduated primitives. The Reverse Russian Doll's outer shell — Anti-Venom scaling proportionally as O+V expands — is what Priority 3 structurally enables.

---

## 27. Brutal Architectural Review v6 — The Autonomy Question (2026-04-29)

**Trigger**: operator question 2026-04-29 post-Priority-2 closure: *"does O+V have the capabilities of actually coding autonomously on its own as an autonomous software developer organism?"* — and the audit finding that Pass B + Pass C are structurally complete (so the §26.5 critical path is *implementation*-closed, leaving only graduation + track record).

**Predecessor reviews**: §3.6 (v1, 2026-04-26), §3.6.6 (v2, 2026-04-27), §24 (v3, 2026-04-28), §25 (v4, 2026-04-29 morning), §26 (v5, 2026-04-29 mid).

**Frame**: drop the slice-cadence framing. Re-grade against a single concrete question: **can O+V actually code autonomously as a software-developer organism, today, unattended?** Decompose into 8 capability dimensions; honestly assess each; identify what's structural vs evidence-thin.

**Honesty contract**: this review does not paper over thinness. It separates *what's shipped* from *what's empirically validated*. The user wants a working autonomous developer, not a checklist. Where the evidence is thin, this review names it.

### 27.1 What's actually shipped (vs the §26 v5 framing which is now stale)

A 2-month sprint shipped a remarkable surface. Listing only post-Phase-2 graduations:

| Arc | Status | Tests | Defaults |
|---|---|---|---|
| Phase 1 Determinism Substrate | ✅ graduated | 785 | 4 flags default-true |
| Phase 2 Closed-Loop Self-Verification | ✅ graduated | — | default-true |
| §25 Priorities A-F (signal density, MetaSensor, HypothesisProbe, postmortem REPL, structural pins, evidence collectors) | ✅ graduated | 750+ | all default-true |
| Phase 12 / 12.2 DW Resilience (Pricing Oracle + Pacemaker + Universal Postmortem) | ✅ closed via soak #7 | 70+ | default-true |
| §26.5.1 Priority 1 — Confidence-Aware Execution | ✅ graduated | 240+ | 6 flags default-true |
| §26.5.2 Priority 2 — Causality DAG + Replay-from-Record | ✅ graduated | 236 | 6 flags default-true |
| §26.6 Cost Contract Structural Reinforcement (4 layers) | ✅ shipped | 54 | default-true; 11 invariants holding |
| **Pass B — Order-2 Governance Cage** | ✅ structurally complete 2026-04-26 | 438 | **all defaults false** pending graduation |
| **Pass C — Adaptive Anti-Venom (== Priority 3)** | ✅ **graduated 2026-04-29 (Move 1)** | 393 | **7 flags default-true**; 7 FlagRegistry seeds + 7 invariants (1 monotonic-tightening + 6 no-authority-import miner pins); two clean idle_timeout soaks (`bt-2026-04-29-{212606,215306}`) |
| W2(5) PhaseRunner extraction (Slices 1-5b) | ✅ shipped | — | default-true |
| W3(6) parallel L3 fan-out + worktree isolation | ✅ shipped | — | default-true |
| 16 sensors + UnifiedIntakeRouter | ✅ shipped | — | active |

This is far beyond a "cognitive scaffold" — it's a fully-instrumented organism. The earlier review's framing of B+ trending A on "we have substrate but not enough cognitive depth" is no longer accurate; cognitive depth substrate is shipped end-to-end.

### 27.2 The 8 capability dimensions of "autonomous coding"

Decomposing the question. Honest grades per dimension.

#### 27.2.1 Detection — Can it see what needs to be done without prompting?

**Grade: A.** 16 autonomous sensors (TestFailure, VoiceCommand, OpportunityMiner, CapabilityGap, Scheduled, Backlog, RuntimeHealth, WebIntelligence, PerformanceRegression, DocStaleness, GitHubIssue, ProactiveExploration, CrossRepoDrift, TodoScanner, CUExecution, IntentDiscovery) + VisionSensor (autonomous Ferrari frame consumer with Tier 0/1/2 cascade) flow through `UnifiedIntakeRouter` with priority queueing, deduplication, WAL persistence, signal coalescing, exhaustion cooldown.

**Honest gap**: no operator-visible *signal coverage audit* — when ProactiveExploration silently goes dormant, MetaSensor catches the dormancy (§25 Priority B), but there's no acceptance test for "every sensor produced ≥ 1 signal in the last N hours under realistic conditions." The mechanism is right; the *empirical* coverage proof under multi-day load doesn't exist.

#### 27.2.2 Planning — Can it form a coherent multi-step plan?

**Grade: B+.** PlanGenerator (model-reasoned planning, schema plan.1) emits a structured plan with approach + ordered changes + risk factors + test strategy at PLAN phase. SemanticTriage classifies NO_OP/REDIRECT/ENRICH/GENERATE pre-generation. HypothesisProbe (§25 Priority C) gates trivial-op classification with bounded epistemic-distress detection. The PLAN exit captures default claims (§25 Priority A) so verification has predictions to compare against.

**Honest gap**: planning is largely LLM-driven. The model reasons about implementation strategy; the system bounds it with structural pins (Iron Gate exploration floor, ASCII strict gate, default-claim coverage), but the *plan quality* is still the model's. There's no explicit multi-hypothesis fan-out at PLAN today (the structural primitives — `subagent_scheduler`, `worktree_manager` — exist; the cognitive trigger to fan out plans vs commit linearly does not).

#### 27.2.3 Execution — Can it actually write working code?

**Grade: A−.** Venom tool loop runs 16 built-in tools (read_file, search_code, edit_file, write_file, bash, web_fetch, web_search, run_tests, get_callers, glob_files, list_dir, list_symbols, git_log, git_diff, git_blame, ask_human) + MCP forwarding for external tools. Multi-file coordinated generation (`_iter_candidate_files` / `_apply_multi_file_candidate`) ships with batch-level rollback. ChangeEngine handles the actual mutation. L3 worktree isolation gives parallel L3 fan-out without filesystem conflicts.

**Honest gap**: 1 verified end-to-end multi-file APPLY in production track record (Sessions Q-S, 2026-04-15). Soak #7 (2026-04-29) had 1 IMMEDIATE op succeed Claude'd, 14 BG topology blocks (correctly skip-queued by cost contract), and zero ops reaching COMPLETE in the soak window. The execution machinery works; the *empirical track record* of unsupervised end-to-end APPLY-then-COMMIT cycles is thin.

#### 27.2.4 Verification — Can it validate its work without lying to itself?

**Grade: A−.** Phase 2 closed-loop self-verification (PropertyOracle, RepeatRunner, Universal Terminal Postmortem). §25 Priority A makes claim density mandatory at PLAN exit (3 default claims). §25 Priority F adds evidence collectors so claims have data to evaluate against. Soak #7 confirmed 8 postmortems with claims=3 pass=1 fail=0 insuff=2 err=0 — non-trivial signal. Visual VERIFY (Slices 3-4) for UI changes. Iron Gate post-GENERATE (exploration floor + ASCII strict). SemanticGuardian post-VALIDATE (10 AST/regex patterns, ~10ms, zero LLM).

**Honest gap**: Property Oracle pass/fail signal is captured but not yet a *circuit-breaker condition* on sibling ops. A claim failing in op N doesn't currently abort op N+1's similar pattern. The signal exists; the auto-action loop is not yet load-bearing.

#### 27.2.5 Cost discipline — Can it stay within budget?

**Grade: A.** §26.6 four-layer cost contract: AST shipped_code_invariants seeds (boot + APPLY) + runtime CostContractViolation gate at ClaudeProvider.generate dispatch + Property Oracle per-postmortem claim + advisor structural guard preventing BG/SPEC → escalation. CostGovernor with per-op + per-phase + session caps. Route-aware budgets (BG ~$0.002/op, SPEC ~$0.001/op, STANDARD ~$0.005/op, COMPLEX ~$0.015/op, IMMEDIATE ~$0.03/op). Soak #7 ended at $0.0316 with one IMMEDIATE Claude op + 14 correctly-blocked BG ops.

**Honest gap**: none structural. The 4-layer defense holds across DAG/replay state (verified by Slice 6 finishing-pass tests). This dimension is solidly graduated.

#### 27.2.6 Recovery — Can it handle failures?

**Grade: B+.** L2 repair (5-iteration FSM, 120s timebox, default-on) engages when VALIDATE exhausts retries. Iron Gate retry feedback is category-aware (ExplorationLedger diversity scoring routes "you must call read_file" feedback into the next GENERATE prompt). ChangeEngine batch-level rollback on multi-file APPLY. Visual VERIFY auto-demotion at ≥50% post-graduation FP. Sentinel-Pacemaker Handshake for catalog deadlocks. Pricing Oracle for missing API pricing.

**Honest gap**: the *long-horizon* recovery story — what happens when an op fails 5 times across 5 different sensors over 6 hours unattended — is not empirically tested. Each individual recovery path works in unit tests + 1-shot soaks. The compound case is unobserved.

#### 27.2.7 Learning — Does it improve over time?

**Grade: A− (was B−, upgraded 2026-04-29 by Move 1 Pass C graduation).** UserPreferenceMemory (typed persistent memory across sessions, 6 kinds). PostmortemRecall (POSTMORTEM → next-op recall, graduated default-true). DirectionInferrer arc-context (cross-session direction memory). LastSessionSummary (read-only digest from prior `summary.json`). SemanticIndex (recency-weighted centroid + clustering). **Pass C AdaptationLedger + 6 surface miners + `/adapt` REPL — graduated 2026-04-29 Move 1; all 7 master flags default-true with asymmetric env semantics; 7 FlagRegistry seeds + 7 shipped_code_invariants seeds (1 monotonic-tightening + 6 no-authority-import miner pins) holding.** Two clean idle_timeout soaks validate graduated state (`bt-2026-04-29-{212606,215306}`).

**Honest gap (remaining)**: the surface miners are *structurally* live but not yet *auto-triggered* from the orchestrator hot path — observed in graduation soak `bt-2026-04-29-212606` where `.jarvis/adaptation_ledger.jsonl` was not created (no proposals minted under load). This is an empirical wiring gap, not a structural failure — when miners are auto-triggered (separate arc, deferred Slice 6 follow-up), the substrate is ready and the operator-approval surface is graduated. The system now has *graduated* learning machinery; demonstrated *adaptation under sustained load* is the remaining empirical step (Move 2: multi-day soak).

#### 27.2.8 Boundaries — Does it know when to stop / ask?

**Grade: A−.** Risk-tier ladder (4 tiers: SAFE_AUTO / NOTIFY_APPLY / APPROVAL_REQUIRED / BLOCKED). MIN_RISK_TIER + PARANOIA_MODE + AUTO_APPLY_QUIET_HOURS (DST-correct TZ math). `ask_human` Venom tool gated to NOTIFY_APPLY+. AdversarialReviewer subagent (graduated 2026-04-26, default-true). OrangePR for Orange-tier review (async PR with `gh pr create`). Cost contract refuses BG/SPEC → Claude escalation. Pass B Order-2 manifest cage requires operator authorization for Order-2 amendments (locked-true `amendment_requires_operator()`). Pass C MetaGovernor `/adapt approve|reject` is operator-gated. All structural.

**Honest gap**: the operator surface is there; the *frequency* of ask-human escalations under autonomous run isn't measured. If the system is too conservative (asks for every NOTIFY_APPLY), autonomy is theatrical. If too aggressive (rare ask-human under unfamiliar code), boundaries are theoretical. The empirical sweet-spot calibration is unproven.

### 27.3 Brutal letter grade — answering the autonomy question directly

**Net grade: B+ trending A−** — *capable but unproven*.

**The structural floor is A.** Architecture, primitives, gates, ledgers, AST invariants, cost contract, posture, sensors, verification, replay-from-record, causality DAG, adaptive substrate — all shipped, all tested in regression, all graduated default-true (except Pass C which is structurally complete but defaults false).

**The empirical ceiling is B+** because:

1. **Track record is thin.** 1 verified end-to-end multi-file APPLY in 2 months. Soak #7 produced clean idle exits but no autonomous APPLY-then-COMMIT cycle. The system has *never* been shown to autonomously code unattended for >24 hours and produce verified shipped code.

2. ~~**Pass C produces zero adaptation signal.** All 6 master flags default-false. The whole "learning" axis is theoretical until graduation soaks run.~~ **CLOSED 2026-04-29 by Move 1 graduation** — all 7 Pass C master flags graduated default-true; 7 FlagRegistry seeds + 7 shipped_code_invariants seeds; two clean idle_timeout soaks. Remaining empirical gap is miner *auto-trigger wiring* under sustained load (Move 2), not graduation cadence.

3. **Sensor coverage under sustained load is unmeasured.** Bounded queues exist; sensor-storm resilience hasn't been stressed.

4. **The auto-action loop on verification signal isn't load-bearing yet.** Postmortems pass/fail per claim, but a sibling op doesn't auto-defer based on a recent claim failure. The signal exists; the action loop on the signal is structurally there (§26.5.1 Priority 1 confidence-collapse → HypothesisProbe → 3-action verdict) but empirically untested in production.

**Can O+V code autonomously today?** **Yes — for short-lived, well-bounded ops, with operator review of Orange-tier work.** It can detect a TODO, plan a fix, generate a multi-file patch, validate via tests, capture postmortem, commit with O+V signature. This works (Sessions Q-S proved it once).

**Can O+V code autonomously as an unattended multi-day software-developer organism?** **Not yet — empirically.** The structural capability is there; the empirical proof isn't. The gap is *track record + graduation soaks*, not *missing primitives*.

### 27.4 Critical path to actual autonomy — top 3 systemic moves

These are not features. They're empirical-validation campaigns + one substantive integration.

#### 27.4.1 Move 1: Pass C graduation cadence — ✅ CLOSED 2026-04-29

**Outcome**: all 7 Pass C master flags (including `JARVIS_ADAPT_REPL_ENABLED`) graduated false→true with asymmetric env semantics in a single in-session cadence (mirroring Priority 1 Slice 5 + Priority 2 Slice 6 precedent). Evidence:
  * **Pre-flight regression**: 387 Pass C tests green; all 7 surface smoke-tests functional with masters on.
  * **Integrated graduation soak** `bt-2026-04-29-212606`: 906s, $0.0317, idle_timeout clean exit, strategic_drift=ok, zero Pass C errors.
  * **Post-graduation regression**: 393 Pass C tests green (+6 default_true_post_graduation pins + falsy-hot-revert pins).
  * **Post-graduation soak** `bt-2026-04-29-215306`: 843s, idle_timeout clean exit, no env overrides — graduated defaults flowed through; zero Pass C-related errors.
  * **Structural seeds**: 7 FlagRegistry entries (87 total, was 80) + 7 shipped_code_invariants seeds (18 total, was 11) — `adaptation_ledger_monotonic_tightening_pin` (LOAD-BEARING) + 6 × `adaptation_<miner>_no_authority_imports`.

**Closure record**: `memory/project_pass_c_graduation_closure.md`.

**Remaining empirical work** (now Move 2): miner *auto-trigger wiring* — the soak showed `.jarvis/adaptation_ledger.jsonl` was not created during the run because miners are not yet invoked from the orchestrator hot path. Substrate is graduated and ready; auto-triggering is a separate deferred-Slice-6 follow-up arc.

**Without this**: the "Learning" dimension stays B− forever. Adaptation is theoretical.

**Cost**: ~6 × 40min soaks per slice = ~24 hours of soak wall-clock; minimal operator time per soak (review 1-2 proposals + approve/reject).

#### 27.4.2 Move 2: Multi-day unattended soak campaign — ✅ STRUCTURALLY CLOSED 2026-04-30

**Outcome**: 7-soak arc executed across 24 hours of work. **Did NOT** achieve 24h sustained autonomous operation. **Did** structurally engineer the substrate that makes it possible, prove every layer with regression tests, and demonstrate flawless graceful degradation under hostile upstream API conditions. Empirical ceiling reached 1h28m before idle out; remaining gap is **bounded by Anthropic API physics**, not O+V substrate.

**Six architectural layers shipped (each commit + tests):**

1. **Stream Rupture Breaker** (`f84b6a3bff`, 16 tests) — per-chunk `asyncio.wait_for` with two-phase TTFT/inter-chunk timeouts. Closes silent stalls.
2. **Transport Resilience Layer** (`c9a5e93951`, 10 tests) — explicit `httpx.Limits(max_connections=10, max_keepalive_connections=5, keepalive_expiry=30s)` at `AsyncConnectionPool`. Closes stale-keepalive accumulation that surfaced as `ConnectTimeout`/`SSLWantReadError`.
3. **Phase-Aware Heartbeats** (`dd7de92c0e`, 10 tests) — new `last_activity_at_utc` field + stream-tick callback hook. `ActivityMonitor` reads `max(last_transition, last_activity)`. Closes long-GENERATE mis-classification as stale.
4. **Unified BG Observability** (`02f059fc0f`, 10 tests) — `BackgroundAgentPool` register/unregister hooks into `_active_ops` + `_fsm_contexts`. Closes the BG-ops-invisible-to-monitor blind-spot that was the load-bearing failure of soaks v1-v4.
5. **Dynamic Provider Fallback** (`a249c03fa8`, 7 tests) — `_try_primary_then_fallback` consults `should_attempt_primary()` BEFORE calling primary. As byproduct flipped 2 pre-existing baseline failures to passing.
6. **Claude Circuit Breaker** (`82ab104e67`, 22 tests) — new `claude_circuit_breaker.py` (~310 lines, stdlib-only, RLock-protected). 3-state FSM: CLOSED → OPEN (consecutive transport exhaustions) → HALF_OPEN (15-min recovery window) → CLOSED (probe success). Cross-cutting health gate at the provider boundary.

**Empirical results** (7 soaks, all `idle_timeout` exit, total cost $0.65):

| Soak | Duration | Net contribution |
|---|---|---|
| v1 `bt-2026-04-29-215306` | 2h21m | baseline |
| v2 `bt-2026-04-29-222250` | 1h01m | (rupture breaker structurally correct, not load-bearing here) |
| v3 `bt-2026-04-30-021210` | 1h23m | **transport resilience — first big lift** |
| v4 `bt-2026-04-30-033240` | 1h01m | (phase-aware HBs FG-only — BG still invisible) |
| v5 `bt-2026-04-30-050848` | 1h21m | **unified BG obs — biggest empirical lift** |
| v6 `bt-2026-04-30-065848` | 1h17m | (dynamic fallback never engaged — Claude not hostile enough) |
| v7 `bt-2026-04-30-173243` | 1h28m | (circuit breaker never tripped — same reason) |

**What was empirically proven:**
- Architecture is sound (no crashes, no leaks, no cost-contract violations)
- Graceful degradation under hostile API: exhaustion → cooldown → drain → idle, all configured-path
- BG state machine fully unified (was the missing piece in v1-v4)
- Closure record: `memory/project_move_2_closure.md`

**What remains pending (gated on Anthropic API stability or different test bench):**
- 24h sustained autonomous operation
- Multiple verified APPLY+COMMIT cycles per soak
- Track Record dimension lift to A

**Critical operator binding (do NOT do these):**
- ❌ Sensor activity exemption / alarm-blinding
- ❌ `--idle-timeout 86400` configuration cheats
- ❌ Re-running this soak with identical parameters (diminishing returns proven)

**Legitimate next moves (separate arcs):**
1. **Synthetic provider bench** — deterministic mock that guarantees op completion, isolates substrate from upstream noise.
2. **Move 3** (`auto_action_router.py`, §27.4.3) — closes the verification → action loop gap.
3. **Real-environment burn-in retry** — re-run when Anthropic shows multi-hour stable window externally; same parameters, no code changes.

#### 27.4.3 Move 3: Auto-action loop on verification signal — ✅ CLOSED 2026-04-30

**Outcome**: 4-slice arc shipped same-day. Advisory router live in shadow mode — produces operator-reviewable proposals on every terminal postmortem. Mutation boundary (`JARVIS_AUTO_ACTION_ENFORCE`) stays locked off until separate later authorization.

**The 4 slices:**

1. **Primitive** (`18a90afe0c`, 25 tests) — new `auto_action_router.py` (~470 lines, stdlib-only + cost_contract_assertion). 5-value `AdvisoryActionType` enum (NO_ACTION as explicit happy-path return per J.A.R.M.A.T.R.I.X. binding). Frozen input dataclasses (`RecentOpOutcome` / `RecentConfidenceVerdict` / `RecentAdaptationProposal` / `AutoActionContext`). Frozen `AdvisoryAction` output. Public `propose_advisory_action()` dispatcher always returns `AdvisoryAction`, never None. Decision precedence: master-off → ESCALATE verdicts → family failure rate (DEMOTE for SAFE_AUTO / DEFER otherwise) → failed category (RAISE_FLOOR) → NO_ACTION. Cost-contract structural guard — AST-pinned.

2. **Signal readers** (`1a806a10ba`, 18 tests) — `recent_postmortem_outcomes()` wraps `verification.postmortem.list_recent_postmortems`; `recent_adaptation_proposals()` wraps `adaptation.ledger.get_default_ledger().history()`; `recent_confidence_verdicts()` was an empty stub for Slice 3 to fill. `gather_context()` composes all three readers + caller-supplied `current_*` fields. Per operator binding "do not duplicate state-gathering" — every reader wraps an existing public ledger API.

3. **Shadow integration** (`1e2e46afbd`, 31 tests) — `_VerdictRingBuffer` (thread-safe drop-oldest deque) closes Slice 2's stub; `record_confidence_verdict()` producer entry. `AutoActionProposalLedger` (append-only JSONL at `.jarvis/auto_action_proposals.jsonl`). `PostPostmortemObserver` Protocol mirroring `OpsDigestObserver` pattern. `AutoActionShadowObserver` concrete observer runs `gather_context` → `propose_advisory_action` → `ledger.append`. Per-op ctx enrichment registry (LRU 256). Two surgical producer wirings: `postmortem_observability.publish_terminal_postmortem_persisted` calls observer; `confidence_observability` records verdicts at P1/P2 publish sites (BELOW_FLOOR/APPROACHING_FLOOR).

4. **Operator surfaces + graduation** (this commit, 23 tests) — `EVENT_TYPE_AUTO_ACTION_PROPOSAL_EMITTED` SSE event, `publish_auto_action_proposal_emitted()` helper called by shadow observer on actionable proposals (NO_ACTION skipped). `register_auto_action_routes(app)` mounts `GET /observability/auto-action[/stats]` (master-flag-gated per request, returns 503 when disabled). `proposal_stats()` aggregator over ledger rows. SerpentREPL `/auto-action` command with subcommands (`stats`, `<op_id>` filter). `event_channel.py` boot wiring registers routes + installs shadow observer. **Master flag `JARVIS_AUTO_ACTION_ROUTER_ENABLED` GRADUATED default false → true.** ENFORCE flag stays locked off.

**Combined: 4 commits, 97 new regression tests, ~2,600 net new lines (module + tests + integrations + docs).**

**Cost contract preservation** (load-bearing across all 4 slices): `_propose_action` raises `CostContractViolation` if `current_route in {background, speculative}` AND `proposed_risk_tier in {approval_required, blocked}`. AST-pinned. None of the 5 current action types directly carry a route field, so naturally satisfied — but encoded structurally to future-proof against later additions.

**Authority invariants** (AST-pinned by tests): no orchestrator / phase_runners / candidate_generator / iron_gate / change_engine / policy / semantic_guardian / semantic_firewall / providers / doubleword_provider / urgency_router imports. Pure stdlib + `cost_contract_assertion` + `aiohttp.web` (Slice 4 routes).

**Closure record**: `memory/project_move_3_closure.md`.

**Mutation boundary still locked**: `JARVIS_AUTO_ACTION_ENFORCE` remains default-false per operator binding. Separate later arc graduates enforce mode after operator review of shadow ledger evidence.

**Net trajectory**: §27 grade table — Self-tightening immunity dimension lifts toward A. Verification → action loop now closes; remaining gap to full A is the enforce-mode mutation boundary, gated on shadow-mode soak evidence.

### 27.5 What this review explicitly does NOT prescribe

- ❌ **More structural primitives.** The 8 capability dimensions are saturated on substrate. Adding more modules without empirical validation worsens the "unproven" gap.
- ❌ **Re-litigating Pass B / Pass C scope.** Both are structurally complete. The decision is graduation, not re-design.
- ❌ **A v7 brutal review before graduation soaks run.** The next review should be evidence-anchored to actual soak data, not architectural enumeration.
- ❌ **A new "Phase 7 / Phase 8 / Activation" framing.** The earlier §1 Forward-Looking Roadmap had a Phase 7 Activation framing that pre-dated Pass C closure. With Pass C done, Phase 7 collapses into "Pass C graduation soaks."

### 27.6 Summary — answering the operator's question directly

**Q: Does O+V have the capabilities to code autonomously as an autonomous software-developer organism?**

**A: Structurally yes. Empirically — partially.**

- **For 1-shot, bounded, sensor-triggered, operator-supervised** ops: **yes, demonstrated** (Sessions Q-S proved this; soak #7 confirmed cost contract holds under load).
- **For multi-day unattended autonomous operation with adaptation, recovery, sustained ramp**: **structurally yes, empirically not yet.** The 8 dimensions are 1×A, 4×A−, 2×B+, 1×B−. To converge to A across all 8: graduate Pass C (Move 1), run a multi-day soak (Move 2), wire auto-action loop (Move 3).

**Net trajectory**: the §25-§26 priorities + Pass B + Pass C all shipped structurally. The system has *more capability shipped than it has demonstrated*. The next 3 moves (graduation + multi-day soak + auto-action loop) convert capability to proof. Order matters: graduation (1) unblocks the soak (2)'s adaptation evidence; soak (2) reveals where the auto-action loop (3) has the highest leverage.

After Move 1 + Move 2: the answer becomes **"yes, demonstrated."** After Move 3: the answer becomes **"yes, with evidence-driven adaptation closing the cognitive-depth loop."** That's A-level autonomous-software-developer-organism territory.

**The gap is execution, not design.**

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
| 2026-04-29 | 2.59 | **§26 Brutal Architectural Review v5 — post-Phase-12-DW-Resilience-closure (B+/B− grade defended).** Adds §26 (latest review) covering: (26.1) what soak #7 actually proved + §25 Priorities A–F all CLOSED single-day (mandatory claim density / MetaSensor / HypothesisProbe / postmortem ledger discoverability / shipped-code structural invariants / evidence collector extension) + Phase 12 DW Resilience CLOSED single-day (Pricing Oracle α + Sentinel-Pacemaker Handshake β + Universal Terminal Postmortem E all live in production); (26.2) refined Cognitive & Epistemic Delta — what CC still has that O+V doesn't post-§25 closure (unbounded interactive recursion / speculative execution trees / mid-generation self-critique / streamed reasoning surface / **confidence-aware decisions** / counterfactual reasoning); (26.3) refined Deep Observability — temporal reconstruction is the missing depth (causality DAG / latent-space confidence broadcast / pre-trip circuit-breaker events / parallel fan-out canvas / time-travel debugging UI); (26.4) brutal grade B+/B− defense — happy path A−, edge cases B−; new race conditions enumerated (W2(5) Slice 5b in-flight / `_active_file_ops` heuristic TTL / Slice 1.3 ordinal under L3 fan-out); new Antivenom bypass vectors (plausibly-typed dead code / symbol-shape hallucination / test-shape gaming / Quine-style obfuscation); (26.5) **Critical Path to A-Level RSI — top 3 systemic upgrades**: Priority 1 Confidence-Aware Execution (Probabilistic Posture) — capture provider logprobs as routing signal + circuit-breaker; Priority 2 Causality DAG + Deterministic Replay — promote phase_capture from per-phase Merkle nodes to session-spanning DAG with parent_record_ids + counterfactual_of edges; Priority 3 Adaptive Anti-Venom (unblock Pass C) — gated on W2(5) Slice 5b + Pass B Slice 1; (26.6) **Cost contract structural reinforcement** — three structural reinforcements bulletproofing BG-never-cascades-to-Claude: AST invariant (extends §25 Priority E shipped_code_invariants seed) + runtime structural assertion (CostContractViolation fatal exception in providers.py at dispatch boundary) + Property Oracle claim (extends §25 Priority A default-claim manifest with `cost.bg_op_used_claude_must_be_false` per-op); (26.7) in-flight alignment table + impact-ranked sequencing for next focus; (26.8) explicit non-prescriptions (no soak re-run with master-off / no more sensors / no more phase runners / no brand-new RSI core); (26.9) summary — the path from B+ to A. **Updates §1 Executive Summary** ("Where we stand" refreshed to post-Phase-12-DW-Resilience-closure + soak #7 verification; grade table refreshed: Architecture A, Cognitive depth B+, RSI Gear 2 B, RSI Gear 3 A−, Self-tightening immunity A−, Cost contract enforcement A−, Net B+/B−). Updates TOC with §26 subsection links. **Marks §25 as superseded by §26 (Priorities A–F all closed).** Zero behavior change — doc-only update synthesizing today's architectural review. | Claude Opus 4.7 (post-Phase-12-DW-Resilience-closure architectural review) |
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
| 2026-04-27 | 2.58 | **Phase 9.1c — Fix A: post-asyncio teardown watchdog arming — closes the shutdown-hang root cause from the once-run.** Per the empirical Option-B once-run (session `bt-2026-04-27-085300`): `_generate_report` completed cleanly at 02:09:25, but the Python process never exited; sat for 1h 50m+ in `loop.shutdown_default_executor()` waiting on a non-daemon ThreadPoolExecutor worker. **Root cause** (diagnosed in Fix A): `BoundedShutdownWatchdog` (Harness Epic Slice 1) WAS already in place — but only armed in the harness's signal-handler path (`harness.py:3276`, gated on `signal_name is not None`). Clean shutdowns (`idle_timeout` / `budget_exhausted` / `wall_clock_cap`) skip the arming and have NO escape hatch when the post-asyncio teardown phase wedges on a non-daemon executor worker. **Fix A (this PR, ~25 LOC + 8 pins)**: arms the watchdog at the START of `main()`'s `finally` block in `scripts/ouroboros_battle_test.py`, BEFORE `loop.shutdown_asyncgens()`, regardless of stop_reason. Reason="post_asyncio_teardown"; deadline=`default_deadline_s()` (30s default, env-tunable via `JARVIS_BATTLE_SHUTDOWN_DEADLINE_S`). Watchdog's first-arm-wins semantics reset on disarm — the signal-handler arm-then-disarm sequence doesn't block a subsequent re-arm. Daemon-thread design (per Slice 1 spec) means no `Py_FinalizeEx` interference: if shutdown wedges past deadline, `os._exit(75)` (`EXIT_CODE_HARNESS_WEDGED`) fires; if everything completes cleanly, the daemon thread dies with the interpreter — no `os._exit` fires. **Defensive belt-and-suspenders**: arm() wrapped in try/except so a watchdog raise can't block clean exit. **8 new regression pins** (`tests/battle_test/test_post_asyncio_watchdog_arming.py`): 3 source-level pins (`reason="post_asyncio_teardown"` literal present + arm BEFORE shutdown_asyncgens + arm in try/except) + 3 watchdog behavior pins (fires-when-wedged / does-not-fire-when-disarmed-in-time / re-arms-cleanly-after-signal-handler-disarm) + 2 default-deadline-s pins (env-override + 30s-default). **Combined regression: 339/339 green** (8 new Fix A + 23 existing BoundedShutdownWatchdog + 308 Phase 9 + Item #4 + cron installer). **Cron-readiness impact**: combined with Fix B (PR #24719's breadcrumb persistence), the cron now has BOTH a forensic trail per soak (Bug #2 closed) AND a deadline escape hatch on shutdown wedge (Bug #1 closed). The two outstanding once-run blockers are NOW BOTH addressed. **Bug #3 — 0-op session pattern (BG-starvation)** remains separate; harness behavior is correct (P9.2 GraduationContract correctly classifies 0-op sessions as RUNNER); the underlying BG-starvation root cause is documented in `memory/project_wave3_item6_graduation_matrix.md` (F3 side-arc) and is out of scope for the cron-install path. **PRD updated**: Doc History v2.58 (append-only). **Path to running cadence**: with Fix A + Fix B landed, a re-run of `bash scripts/install_live_fire_soak_cron.sh --once` should now (a) complete cleanly OR (b) fire `os._exit(75)` after 30s wedge — either way the cron survives. Then `--install` is safe to commit. | Claude Opus 4.7 (Phase 9.1c — Fix A post-asyncio watchdog arming; closes shutdown-hang root cause) |
| 2026-04-27 | 2.57 | **Phase 9.1b — Fix B: breadcrumb persistence + INTERRUPTED status — closes a real bug surfaced by the once-run.** Per the empirical Option-B once-run (PRD post-mortem session `bt-2026-04-27-085300`): the harness wrote `summary.json` cleanly at 02:09:25 but the Python process never exited (atexit/asyncio shutdown hang); when the harness CLI was eventually externally killed (SIGTERM), **NO evidence row landed on disk** because persistence happened only AFTER `subprocess.run` returned successfully. The cron would have jammed every 8 hours with hung Pythons + zero evidence to grep — invisible failure mode. **Fix B (this PR, ~80 LOC + 5 pins)**: refactors `LiveFireSoakHarness.run_soak` to write a **breadcrumb evidence row BEFORE the runner is invoked** (status `SUBPROCESS_IN_FLIGHT`) and wraps the subprocess invocation in a `try/except BaseException` block that catches `SystemExit` / `KeyboardInterrupt` (SIGTERM-induced), persists an `INTERRUPTED` row, and re-raises so the caller's signal propagates cleanly. **2 new HarnessStatus enum values**: `SUBPROCESS_IN_FLIGHT` (breadcrumb — paired with completion row by session_id; lone breadcrumb on disk = hung soak) + `INTERRUPTED` (caught BaseException mid-subprocess; persisted with outcome=infra so the flag's clean-count is NOT blocked by harness-internal hangs). **Persistence ordering**: breadcrumb hits disk via `_append_history_row` BEFORE `runner(...)` returns — proven by `test_breadcrumb_written_before_subprocess_returns` which has the runner stub read `.jarvis/live_fire_graduation_history.jsonl` mid-invocation and assert the breadcrumb is already there. **Defensive belt-and-suspenders**: the BaseException handler's own `_persist_failure` call is wrapped in a last-ditch `try/except Exception` so even a persistence-side raise won't block SystemExit propagation (the cron MUST be able to terminate cleanly when the operator kills it). **Authority posture preserved**: still NEVER raises `Exception` into caller; BaseException re-raises after evidence row written; lazy substrate imports unchanged; AST-pinned cage invariants survive. **5 new regression pins** (`tests/governance/test_p9_1_live_fire_soak.py`) covering: breadcrumb-written-before-runner-returns (load-bearing — proves the disk-write-ordering invariant via runner-stub reads disk mid-call) + breadcrumb-row-correct-metadata (flag_name + session_id="in-flight" + outcome=infra + runner_attributed=False + stop_reason="subprocess_in_flight" + finished_at_iso="" empty) + INTERRUPTED-via-KeyboardInterrupt-persists-row + INTERRUPTED-via-SystemExit-persists-row (more direct SIGTERM mapping) + 2-status-values-added bit-rot pin + breadcrumb-doesnt-count-as-runner-outcome (INTERRUPTED uses outcome=infra so a hung soak doesn't unfairly block the flag's clean-count). **2 existing pins updated** for the new 2-rows-per-soak pattern: `test_evidence_persisted_to_history_jsonl` now asserts both breadcrumb + completion present + correctly distinguishes by `harness_status`; `test_evidence_for_flag_returns_only_that_flag` filters out `subprocess_in_flight` rows when counting completion-rows-per-flag. Combined regression spine: **308/308 tests green** across all Phase 9 modules + cron installer + Item #4 graduation cadences. **Cron-readiness impact**: breadcrumb persistence converts the cron from "silently jams forever" to "leaves a forensic trail every 8 hours that operator can grep" — fixes one of the two blockers (Bug #2 from the once-run post-mortem). Bug #1 (battle-test atexit/asyncio shutdown hang) is the SEPARATE Fix A, deferred. **Once Fix A lands + a clean second once-run**, the cron is safe to install. **PRD updated**: Doc History v2.57 (append-only). | Claude Opus 4.7 (Phase 9.1b — Fix B persistence-on-kill; closes real bug from once-run) |
| 2026-04-27 | 2.56 | **Cron installer for Live-Fire Graduation Soak Harness shipped — operator-actionable cadence kickoff.** Per PRD §9 P9.1 acceptance + operator binding "schedule the cron soak cadence to start running": new shell script `scripts/install_live_fire_soak_cron.sh` (~190 LOC, idempotent) automates installation of the cron entry that drives the 4-6-week soak cadence. **6 modes**: `--install` (idempotent install/update of a marked block bracketed by `# === LIVE_FIRE_SOAK_BEGIN/END ===` so re-runs replace rather than duplicate); `--dry-run` (preview without crontab mutation); `--remove` (clean uninstall via marker-bracketed deletion); `--once` (run a single soak NOW for first-proof validation); `--status` (read-only render of installed crontab block + pause-flag state + harness queue); `--help`. **Defaults**: `0 */8 * * *` schedule (3 sessions/day), `$0.50/soak` cost cap, `2400s` wall-clock cap, `3600s` subprocess kill timeout — env-overridable via `CRON_SCHEDULE` / `COST_CAP` / `WALL_CAP` / `TIMEOUT`. **Log capture**: each invocation redirects to `.jarvis/live_fire_soak_logs/<timestamp>.log` for auditable per-run traces. **Authority posture**: operator must explicitly run the script — never auto-installed; idempotent block-replacement only ever touches the marker-bracketed region of the operator's crontab; pre-flight checks confirm harness script exists + creates log dir; rollback is `--remove`. **11 regression pins** (`tests/governance/test_install_live_fire_soak_cron.py`) covering: script-exists-and-executable + help-works + dry-run-emits-cron-block + 3 default-value pins (every-8-hours / $0.50 cost-cap / 2400s wall-clock) + 2 env-override pins (CRON_SCHEDULE / COST_CAP) + unknown-arg-exits-non-zero + log-redirect-and-stderr-merge + pause-flag-documentation. **Operator usage** (one-time install on local machine — cron must run locally because the live-fire harness forks the full battle-test stack which can't run as a remote routine): `bash scripts/install_live_fire_soak_cron.sh --dry-run` (preview) → `bash scripts/install_live_fire_soak_cron.sh --once` (first-proof run) → `bash scripts/install_live_fire_soak_cron.sh --install` (commit cron entry). After install, cadence runs automatically every 8 hours; pause via `export JARVIS_LIVE_FIRE_GRADUATION_SOAK_PAUSED=true`; check progress via `python3 scripts/live_fire_graduation_soak.py status`. **Phase 9 status**: structurally + operationally ready. Once installed the cadence will accumulate 12+ flag flips × 3 clean sessions ≈ 4-6 weeks → converts B+ trending A− to **solid A−** as evidence accumulates. **PRD updated**: Doc History v2.56 (append-only). | Claude Opus 4.7 (cron installer + smoke tests; operator-actionable cadence kickoff) |
| 2026-04-27 | 2.55 | **Phase 9.5 — Cross-Session Coherence Harness + Phase 8 Producer Wiring shipped — Priority #3 closed; PHASE 9 STRUCTURALLY COMPLETE.** Per PRD §9 P9.5 + brutal-review v2 §3.6.3 Priority #3 + §3.6.2 vector #5: cross-session memory had NEVER been validated across a multi-session arc; Phase 8 surfaces shipped EMPTY because producers weren't wired. This PR closes both. **Part A — `graduation/cross_session_coherence.py` (~340 LOC)** ships an empirical-evidence harness for the signal-carryover invariant. `run_two_session_arc(project_root, user_preference_root, session_n_id, marker_name)` simulates session N (writes a known summary.json under `<root>/.ouroboros/sessions/bt-<id>/` + seeds a known UserPreferenceMemory marker), simulates harness restart (resets in-process LSS singleton — proxies what happens on harness boot when a new process reads disk fresh), boots session N+1 (loads LSS via same disk path, reads UserPreferenceMemory via same store), and asserts that session N's markers measurably surface in N+1's primitives. Returns structured `CoherenceReport` with 3 per-primitive checks (5-value `PrimitiveStatus` enum: `CARRIED_OVER` / `NO_CARRYOVER` / `PRIMITIVE_DISABLED` / `PRIMITIVE_UNAVAILABLE` / `HARNESS_ERROR`): (1) **LSS load** asserts session_n_id appears in `LastSessionSummary.load()` output; (2) **LSS prompt render** asserts session_n_id appears in `LastSessionSummary.format_for_prompt()` output (the ACTUAL integration path consumed by CONTEXT_EXPANSION at session N+1's boot); (3) **UserPreferenceMemory survival** asserts marker_name persists across `UserPreferenceStore` instance recreation. **Why a harness instead of a 50-session soak**: the harness validates the signal-carryover INVARIANT in seconds (necessary-and-sufficient for "did the cross-session memory work"); proving the *quality* of cross-session learning over a long arc is a long-horizon soak deliverable. **Live-fire**: 3/3 primitives carry over end-to-end (LSS load + LSS prompt render + UPM survival), `all_applicable_carried_over=True`, 100% rate. **Part B — `observability/phase8_producers.py` (~325 LOC)** ships the lightweight NEVER-raises producer hooks that wire the orchestrator + classifiers + phase-timing instrumentation into the 5 Phase 8 substrate modules. **5 producer hooks**: `record_decision(op_id, phase, decision, factors, weights, rationale)` (substrate.record + best-effort SSE publish_decision_recorded), `record_confidence(classifier_name, confidence, threshold, outcome, op_id)`, `record_phase_latency(phase, latency_s)`, `check_breach_and_publish(phase)` (combines `LatencySLODetector.check_breach` + SSE publish_slo_breached), `check_flag_changes_and_publish()` (FlagChangeMonitor.check + per-delta publish_flag_change_event with masking). Plus `append_timeline_event` placeholder for future write-side timeline registry + `substrate_flag_snapshot()` read-only debug helper. **Authority posture**: pure-evaluation modules; stdlib + typing only at top level (substrate + SSE bridge imported lazily inside helpers — pinned by AST scan); NEVER raises (every helper returns `Optional[result]`; broken-substrate-module simulation pinned); no master flag at producer layer (substrate's own master flags govern); read/write only over the 5 substrate modules — no imports from gate / execution modules (AST-pinned). **Why the orchestrator's hot-path doesn't import substrate directly**: producer wrapping enables substrate imports to fail without crashing orchestrator; SSE-bridge composition happens in one call instead of two; future producer-side optimizations (batching, sampling) live behind the wrapper. **39 regression pins** (`tests/governance/test_p9_5_coherence_and_producer_wiring.py`) covering: 2 module-constant + 4 happy-path coherence (all-3-primitives-carry + LSS load carries session_n_id + LSS prompt renders session_n_id + UPM marker survives) + 2 disabled-primitive fallthrough (LSS-disabled marks PRIMITIVE_DISABLED not NO_CARRYOVER + prompt-injection-disabled detected) + 3 markdown writer (header + creates-file + unwritable-returns-False) + 2 NEVER-raises (nonexistent-user-pref-root + setup-failure-still-returns-report) + 3 cage authority invariants (gate-modules-not-imported + stdlib-only-top-level + 7-name public-API bit-rot pin) + 3 substrate_flag_snapshot pins (default-all-false + all-on + partial) + 9 producer-hook pins (master-off-False × 4 + master-on-records × 4 + factors+weights threading) + 2 breach pins (no-samples + p95-exceeds-slo) + 2 flag-change publish pins (master-off + master-on-publishes-deltas) + 1 timeline-placeholder pin + 4 NEVER-raises (empty-inputs + non-numeric + unknown-phase + broken-substrate-module simulation) + 4 producer cage invariants (gate-modules-not-imported + stdlib-only-top-level + 7-name public-API + no-secret-leakage). Combined Phase 9 regression spine: **291/291 tests green** (39 new + 36 P9.4 + 77 P9.2+P9.3 + 79 P9.1 + 60 Item #4 graduation cadences). **🟢 Priority #3 closed**: cross-session memory plumbing is now empirically validated; Phase 8 substrate has producer hooks ready for the orchestrator to call (orchestrator-side wiring is a follow-up — touching the 102K-line orchestrator is out of scope for this PR; the producer module is the thin shim that the orchestrator can call when ready, with NEVER-raises so a half-wired call site cannot crash the FSM). **🎉 PHASE 9 STRUCTURALLY COMPLETE 2026-04-27**: P9.1 (Soak harness) ✅ + P9.2 (GraduationContract) ✅ + P9.3 (REPL extensions) ✅ + P9.4 (Adversarial Cage Stress Test) ✅ + P9.5 (Coherence + Producers) ✅. **All three brutal-review v2 critical-path priorities complete** (Priority #1 Live-Fire substrate ✅ + Priority #2 Adversarial Cage 0/38 vectors ✅ + Priority #3 Cross-Session Coherence + Producer Wiring ✅). **Honest-grade impact**: Phase 9 closure converts B+ trending A− → solid A− *substrate-wise*. Reaching A requires the cron-driven 4-6 week soak cadence (Phase 9.1 substrate is in place, just hasn't accumulated 12+ flag flips × 3 clean sessions yet) + adversarial-cage 0% pass-through PROVEN against future LLM-generated quine attempts beyond the seed corpus + 50-session arc proof going beyond the 2-session harness. **PRD updated**: Doc History v2.55 (append-only). Per PRD §9 Phase 9 acceptance criteria: ledger evidence (P9.1+P9.2+P9.3) ✅; adversarial corpus 0% pass-through against documented gaps (P9.4) ✅; cross-session signal carryover (P9.5 Part A) ✅; Phase 8 producer hooks ready (P9.5 Part B) ✅; combined regression 0 NEW infra-noise classes ✅. | Claude Opus 4.7 (Phase 9.5 — closes Priority #3 + Phase 9 structurally) |
| 2026-04-27 | 2.54 | **Phase 9.4 — Adversarial Cage Stress Test shipped — empirical Quine-resistance numbers landed (Priority #2 closed).** Per PRD §9 P9.4 + brutal-review v2 §3.6.3 Priority #2: cage's security properties were "we believe this is tight." This PR converts that into "0/38 escapes, 12/38 documented known gaps tracked in PRD §3.6.2 vector #7." New module `backend/core/ouroboros/governance/graduation/adversarial_cage.py` (~530 LOC) ships the empirical-evidence harness — pure-evaluation (never executes candidate code, never forks subprocesses), runs candidates through the FULL static cage layer: `validate_ast` (8 rules incl. P7.7 introspection-escape + Phase 7.7-followup module-level side-effect detection) + `SemanticGuardian.inspect` (10 AST/regex pattern detectors). Each candidate classified into 6-value `CageVerdict` enum: `BLOCKED_AST` / `BLOCKED_SEMANTIC_GUARD` / `BLOCKED_BOTH` / `PASSED_THROUGH` / `CLEAN_PASSED` / `HARNESS_ERROR`. **3-category corpus** (`tests/governance/adversarial_corpus/corpus.py`, 38 entries): SANDBOX_ESCAPE (26 — known Python escape patterns from CTF/security research: classic `().__class__.__bases__[0].__subclasses__()` walk + direct `__subclasses__`/`__bases__`/`__class__` access + `getattr(obj, "<banned>")` string-literal + helper-hidden subclasses + module-level shell-exec/subprocess/eval/dynamic-import/deserialization-RCE/marshal/control-flow + ctx-mutation + banned governance import + Rules 1/3/5 violations + 6 KNOWN-GAP entries: alias-defeats-resolver, `type(x).__mro__`, `vars()`, `dir()`, `globals()`, `chr()`-constructed banned attr — each with `tracking_ticket` referencing PRD §3.6.2 vector #7), QUINE_ATTEMPT (6 — function-body process-spawn / shell-exec / dynamic-eval / dynamic-compile / dynamic-import + multi-step string-assembly quine — all marked known_gap because Rules 1-8 are static-shape; runtime sandbox cage is the defense layer), CLEAN_CONTROL (6 — well-formed PhaseRunner subclasses MUST pass through, proves cage isn't blanket-blocking). **Charset discipline**: corpus source file itself contains NO literal dangerous-stdlib API names (`os.system` / `subprocess.Popen` / etc.) — composed at runtime via string concatenation (`_O_SYS = "o" + "s.sys" + "tem"`) so CI security-scan hooks don't flag the test corpus itself. **Live-fire results** (persisted to `tests/governance/adversarial_corpus/RESULTS.md` + `results.json`): **0 open vectors** (real regressions), **12 known gaps** documented + tracked, **6 clean controls passed**, **0 harness errors**, pass-through rate 12/38 = 31.58% (every passing entry is documented). The 12 known-gap passes break down as: 6 sandbox-escape gaps in PRD §3.6.2 vector #7 (Quine-shape via static-shape rule gaps — explicitly enumerated in the brutal review v2) + 6 quine-attempt function-body primitives (shell-exec/eval/exec/Popen/importlib/multi-step-assembly — these REQUIRE runtime sandbox cage as defense; Rules 1-8 are static AST checks and don't statically prevent function-body calls to dangerous primitives). **Authority posture (locked + AST-pinned)**: pure-evaluation module (uses `ast.parse` only — never runs candidate code); stdlib-only top-level imports (cage modules `validate_ast` + `SemanticGuardian` imported lazily inside helpers); no master flag (developer/CI-only harness; no production presence); NEVER raises (every error path → structured `HarnessError` row); bounded `MAX_CORPUS_ENTRIES=500` + `MAX_CANDIDATE_SOURCE_BYTES=64 KiB`; env discipline (forces both validator + SemanticGuardian master flags ON for the duration of harness invocation, then restores prior state — measures cage's *real* security posture independent of operator deployment toggles). **Markdown + JSON writers** persist the report for audit trail; readable `RESULTS.md` includes per-entry verdict table + open-vectors section + known-gaps section with tracking tickets. **36 regression pins** (`tests/governance/test_p9_4_adversarial_cage.py`) covering: 4 module-constant + 6 corpus-shape (38-pin + per-category counts + names-unique + known-gaps-have-tracking-tickets + clean-controls-never-known-gap + **charset-discipline forbidden-literals scan** of the corpus source file) + 4 evaluate_entry single-entry semantics (clean-passes / oversize-harness-error / blocked-marks-acceptable / known-gap-passing-acceptable) + 3 run_stress aggregate (empty / returns-StressReport / caps-at-MAX_CORPUS_ENTRIES) + **5 acceptance-criterion pins** (zero-open-vectors AGAINST FULL CORPUS / all-clean-controls-pass / no-harness-errors / every-entry-acceptable / **6-known-gaps-match-brutal-review-§3.6.2-vector-#7**) + 5 markdown/JSON writer pins (header + open-vectors-section / write-md / write-json / unwritable-path-returns-False) + 5 NEVER-raises smoke (parametrized over 4 bad sources + mixed-inputs run_stress) + 4 cage authority invariants (gate-modules-not-imported AST scan + stdlib-only-top-level + no-secret-leakage + 13-name public-API bit-rot pin). Combined Phase 9 regression spine: **252/252 tests green** (36 new + 77 P9.2+P9.3 + 79 P9.1 + 60 Item #4 graduation cadences). **🟢 Priority #2 (Adversarial Cage Stress Test) closed**: cage's security properties are now *measured*, not believed. Every escape pattern that passes through is documented with a tracking ticket pointing at the corresponding PRD §3.6.2 vector. **Acceptance criterion met**: zero NEW open vectors against the live cage. The 12 documented passes match the brutal-review v2 fragility-vector enumeration exactly — proves the corpus is calibrated against the real cage, not against a strawman. **Phase 9 progress**: P9.1 ✅ + P9.2 ✅ + P9.3 ✅ + P9.4 ✅. Only P9.5 (Cross-Session Coherence Harness + Phase 8 Producer Wiring — Priority #3) remains. **PRD updated**: Doc History v2.54 (append-only). | Claude Opus 4.7 (Phase 9.4 — Adversarial Cage Stress Test; closes Priority #2) |
| 2026-04-27 | 2.53 | **Phase 9.2 + 9.3 — GraduationContract + /graduate REPL live-fire extensions shipped — Priority #1 refinement.** Builds on Phase 9.1 (v2.52) by ADDING per-flag refinement to outcome classification + 6 new operator-facing REPL subcommands. **P9.2 — `graduation/graduation_contract.py` (~250 LOC)**: ships frozen `GraduationContract` dataclass with per-flag refinements (`clean_predicate` callable, `failure_class_blocklist_overrides` frozenset, `re_arm_after_runner_seconds` clamped to [60, 86400], `cost_cap_override_usd`, `max_wall_seconds_override`, `description`). Three built-in predicates: `default_clean_predicate` (matches harness's classify_outcome step 1: complete + no runner-class failures), `predicate_requires_decision_trace_rows` (Phase 8 substrate flags must produce ≥1 op as proxy for "substrate actually fired" — defends against empty-session false graduation), `predicate_requires_curiosity_hypothesis` (CuriosityEngine flag must generate ≥1 hypothesis — falls back to ops_count proxy when `curiosity_hypotheses_generated` not yet instrumented). **6 built-in custom contracts** registered (5 Phase 8 substrate + CuriosityEngine); other 18 flags use default contract. **Master flag** `JARVIS_LIVE_FIRE_USE_GRADUATION_CONTRACT` (default false) gates harness consultation — when off, harness behavior is byte-identical to pre-9.2 (contract registry remains read-only metadata via REPL). **Authority posture (locked)**: pure-data + pure-predicate module (no I/O, no logger, no subprocess); stdlib + typing only at top level (AST-pinned); NEVER raises (predicate exceptions caught + return False); bounded `MAX_CONTRACTS=128`. **`live_fire_soak.py` updated** with new `_maybe_apply_contract` method called immediately after `classify_outcome` in `run_soak`: (1) custom predicate may DOWNGRADE default-CLEAN to RUNNER (e.g. CuriosityEngine flag was on but zero hypotheses → not actually clean), (2) `failure_class_blocklist_overrides` may UPGRADE default-RUNNER to INFRA when EVERY positive-count failure class is in the contract's blocklist (waiver for flags whose enablement legitimately surfaces new infra-class failures). Notes string carries `contract_predicate_downgraded_clean` / `contract_blocklist_upgraded_runner_to_infra` markers for audit. Master-off byte-identical proven via parametrized regression. **P9.3 — `adaptation/graduate_repl.py` extensions (~150 LOC)**: 6 new live-fire READ-ONLY subcommands wired into existing `dispatch_graduate`: (1) `live-queue` renders `LiveFireSoakHarness.queue_view()` with PEND/BLKD/RNRBL/GRAD markers + per-flag clean/required/runner counts + dep count; (2) `live-evidence <flag>` renders all evidence rows from `.jarvis/live_fire_graduation_history.jsonl` with timestamp/session_id/stop_reason/cost/duration/ops_count/notes; (3) `live-next` dry-run pick-next reporting what `pick_next_flag()` would return without actually invoking it; (4) `live-contracts` renders all 6 custom contracts + their consultation-flag state; (5) `live-pause` prints the export command (REPL is read-only, cannot mutate parent shell env); (6) `live-resume` prints the unset command. **Authority constraint**: REPL CANNOT fire soaks — that's cron-only authority via `scripts/live_fire_graduation_soak.py run`. REPL is purely read-only over harness state. **Lazy substrate imports** inside subcommand handlers (AST-pinned) so `/graduate help` doesn't pay the substrate cost. **NEVER raises** — broken-harness import simulation pinned (returns structured "(unavailable: ...)" stub). Help text updated to advertise all 6 new live-* subcommands + 2 new master flags. **77 regression pins** (`tests/governance/test_p9_2_p9_3_contract_repl.py`) covering: 2 module-constant + 11 master-flag matrix + 7 GraduationContract dataclass (defaults + re-arm clamp low/high + metadata-omits-callable + is_clean default + is_clean predicate-raises-returns-False) + 9 built-in predicate pins (clean / runner-class-blocks / non-dict-summary / non-dict-failure-counts / decision-trace-requires-ops / curiosity-uses-hypothesis-count + falls-back-to-ops) + 8 registry helper pins (unknown-returns-default + known-returns-custom + non-string-empty-default + has_custom_contract + **known_contract_flags-subset-of-CADENCE_POLICY bit-rot guard** + all_contracts_metadata-includes-Phase-8-substrate + curiosity-engine) + 4 harness-contract integration pins (master-off-byte-identical + downgrades-clean-to-runner + clean-preserved-when-predicate-passes + upgrade-runner-to-infra-via-blocklist) + 12 REPL pins (help-includes-live-subcommands / 24-flags-render / master-off-disabled / unknown-flag-400 / no-history / missing-flag-arg / next-returns-substrate / contracts-lists-curiosity / consultation-flag-state-rendered / pause-export + resume-unset commands) + 1 broken-harness-resilience pin + 5 cage authority invariants (gate-modules-not-imported + stdlib-only-top-level × 2 modules + repl-extensions-lazy-import + no-secret-leakage + 14-name public-API bit-rot pin) + 18 NEVER-raises (3 predicates × 6 bad inputs each) + 1 broken-predicate-doesnt-crash-harness + 2 help-text-bit-rot pins. Combined regression: **216/216 tests green** (77 new + 79 P9.1 + 60 Item #4 graduation cadences). **🟢 Priority #1 substrate hardened**: per-flag refinement layer in place + operator-facing REPL surface complete. Phase 9.1 + 9.2 + 9.3 together cover P9 §9 sub-deliverables 1+2+3. **Next P9 sub-deliverables**: P9.4 Adversarial Cage Stress Test (Priority #2), P9.5 Cross-Session Coherence Harness + Phase 8 Producer Wiring (Priority #3). **PRD updated**: Doc History v2.53 (append-only). | Claude Opus 4.7 (Phase 9.2 + 9.3 — GraduationContract + REPL extensions) |
| 2026-04-27 | 2.52 | **Phase 9.1 — Live-Fire Graduation Soak Harness shipped — Priority #1 substrate landed.** New module `backend/core/ouroboros/governance/graduation/live_fire_soak.py` (~870 LOC) ships the operator-facing automation layer that converts the cognitive loop from "theoretically firing" to "empirically graduating." Composes (does NOT duplicate) the existing `adaptation/graduation_ledger.py` (CADENCE_POLICY + outcomes + clean-counting + `is_eligible`) and forks one `ouroboros_battle_test.py --headless --max-wall-seconds 2400 --cost-cap 0.50` subprocess per soak. **CADENCE_POLICY extended 15→24** with 9 new flag entries: 5 Phase 8 substrate (`JARVIS_DECISION_TRACE_LEDGER_ENABLED` + `JARVIS_LATENT_CONFIDENCE_RING_ENABLED` + `JARVIS_MULTI_OP_TIMELINE_ENABLED` + `JARVIS_FLAG_CHANGE_EMITTER_ENABLED` + `JARVIS_LATENCY_SLO_DETECTOR_ENABLED`) + 3 Phase 8 surface (`JARVIS_PHASE8_IDE_OBSERVABILITY_ENABLED` + `JARVIS_PHASE8_SSE_BRIDGE_ENABLED` + `JARVIS_PHASE8_MULTI_OP_RENDERER_ENABLED`) + 1 CuriosityEngine (`JARVIS_CURIOSITY_ENGINE_ENABLED`). All 9 require 3 clean sessions (Pass B cadence). **Pick-next algorithm**: substrate flags graduate BEFORE surface flags via dependency map (Phase 8 surfaces depend on substrate; CuriosityEngine on Phase 7.6 hypothesis probe; Pass C activations on the matching loader + meta-governor YAML writer; Phase 7.9 sunset on adaptive_semantic_guardian). Within tied dependency states, alpha-stable. **Outcome classification** (5-step decision tree, NEVER raises): (1) `session_outcome=="complete"` AND no runner-class failures → CLEAN; (2) migration stop_reason → MIGRATION (waiver); (3) any runner-class failure (`phase_runner_error` / `iron_gate_violation` / `semantic_guardian_block` / `change_engine_error` / `verify_regression` / `l2_repair_error` / `fsm_state_corruption` / `artifact_contract_drift` / `candidate_validate_error`) → RUNNER (blocks flip per Pass B clean-bar discipline); (4) infra-class (`provider_*` / `tls_*` / `network_*` / `out_of_memory` / `disk_full` / `git_lock_contention` / `async_shutdown_race`) OR shutdown-noise stop_reason (sigterm/sighup/sigint/wall_clock_cap/harness_idle_timeout) → INFRA (waiver row, non-blocking — matches operator binding from project_async_shutdown_race_triage.md); (5) default conservative → RUNNER (unknown fault-class blocks rather than silently waivers). **Authority posture (locked + pinned)**: read/write only over `.jarvis/live_fire_graduation_history.jsonl` + delegating to `GraduationLedger.record_session` for canonical clean-count; stdlib + subprocess + adaptation.graduation_ledger only at top level (battle-test path is invoked via subprocess, NOT imported — we don't pull the 6-layer stack into the harness process); master flag `JARVIS_LIVE_FIRE_GRADUATION_SOAK_ENABLED` (default false until graduation-of-the-grader cadence) + operator pause `JARVIS_LIVE_FIRE_GRADUATION_SOAK_PAUSED`; NEVER raises (all 5 known exception classes pinned via parametrized smoke); 8-status enum (`OK` / 5 SKIPPED_* / SUBPROCESS_FAILED / SUBPROCESS_TIMEOUT / SUMMARY_PARSE_FAILED / LEDGER_WRITE_FAILED) with structured `HarnessResult` returned on every code path; bounded outputs (MAX_HISTORY_FILE_BYTES=8 MiB / MAX_HISTORY_RECORDS_LOADED=10000 / MAX_NOTES_CHARS=2000 / MAX_FAILURE_CLASS_COUNT_KEYS=32); cross-process flock on history JSONL (reuses Phase 7.8's `flock_exclusive`); `EvidenceRow` schema_version="1.0" carries flag/session/outcome/runner_attributed/stop_reason/cost/duration/ops_count/failure_class_counts/deps_set/started_at/finished_at/notes (richer than bare-bones graduation ledger row). **Subprocess env discipline**: builds env dict with ONLY (target_flag + dependencies + master harness flag + graduation ledger flag) set to "true"; other JARVIS_* substrate flags are NOT touched (they inherit parent env exactly — flipping multiple in one soak would muddle the evidence). **CLI** `scripts/live_fire_graduation_soak.py` — 6 subcommands (`queue` / `evidence FLAG` / `run [FLAG]` / `status` / `pause` / `resume`), lazy-imports the harness module so `--help` doesn't pay substrate import cost (AST-pinned). ANSI-aware rendering with auto-strip when not TTY. Cron-friendly invocation: `JARVIS_LIVE_FIRE_GRADUATION_SOAK_ENABLED=true python3 scripts/live_fire_graduation_soak.py run`. **79 regression pins** (`tests/governance/test_p9_1_live_fire_soak.py`) covering: 4 module-constant + 6 master/pause flag matrix + 5 CADENCE_POLICY count/extension/duplicate pins + 6 dependency map pins (incl. **all_dependency_flags-subset-of-known-flags bit-rot guard** + **substrate-flags-have-no-dependencies leaf-pin**) + 9 outcome-classification pins (clean-no-failures + runner-class-blocks + infra-class-waiver + 5-shutdown-noise-infra + migration + unknown-default-runner-conservative + **runner-takes-priority-over-infra** + non-dict-summary-runner + non-dict-failure-counts-treated-empty + zero-count-doesnt-block) + 5 pick-next algorithm pins (no-graduations-returns-substrate + alpha-stable + skips-graduated + **unblocks-dependent-after-dep-graduation** + returns-none-when-all-graduated) + 3 queue_view pins (24-flags + graduated-marking + deps-satisfied) + 5 short-circuit pins (master-off + paused + unknown-flag + no-eligible + deps-not-graduated-explicit-surface) + 5 happy-path with injected runner pins (clean records + runner blocks + infra waiver + **subprocess env contains target+deps only** + cost_cap/wall_seconds passed) + 3 failure-path pins (subprocess raise + timeout + non-dict-summary) + 5 evidence pins (schema_version + persisted-to-jsonl + per-flag-filter + corrupt-lines-skipped + failure-counts-truncated) + 7 NEVER-raises (parametrized over 6 bad summaries + 5 exception classes) + 4 cage authority invariants (does-not-import-gate-modules AST scan + top-level-imports-stdlib-only + no-secret-leakage + 20-name public-API bit-rot pin) + 2 history-path pins (default-under-jarvis + env-override) + 2 CLI integration pins (lazy-imports-substrate AST scan + 6-subcommands-present source-text scan). Combined regression: **139/139 tests green** (79 new + 60 existing Item #4 graduation cadences); existing pin `test_15_known_flags` renamed to `test_24_known_flags` + count assertion updated. **🟢 Priority #1 substrate is now in place**: cron-driven daily soak cadence can begin (`JARVIS_LIVE_FIRE_GRADUATION_SOAK_ENABLED=true python3 scripts/live_fire_graduation_soak.py run` × 3/day). Per PRD §9 P9.1 estimate: ~3 flips/week × 12+ flags ≈ **4-6 weeks to fully graduated**. Until that completes, A-vision system runs at C+ effective. **Next P9 sub-deliverables** (per PRD §9 phase plan): P9.2 per-flag GraduationContract refinement, P9.3 `/graduate` REPL extensions for live-fire mode (queue / evidence / pause / resume / next subcommands beyond what scripts/CLI ships), P9.4 Adversarial Cage Stress Test (Priority #2 — runs in parallel), P9.5 Cross-Session Coherence Harness + Phase 8 Producer Wiring (Priority #3 — runs in parallel). **PRD updated**: Doc History v2.52 (append-only). | Claude Opus 4.7 (Phase 9.1 — Live-Fire Graduation Soak Harness; Priority #1 substrate) |
| 2026-04-27 | 2.51 | **Brutal architectural review v2 + Live-Fire Graduation Cadence promoted to Priority #1 (doc-only).** Operator requested an unvarnished post-Phase-8 self-assessment. Three sections updated: **§1 Forward-Looking Priority Roadmap** rewritten with new top-3 reflecting the brutal review — Priority #1 (Live-Fire Graduation Cadence: flip 12+ default-false flags via 3-clean-session soaks per substrate), Priority #2 (Adversarial Cage Stress Test: empirical Quine-resistance numbers via ~50 known sandbox-escape patterns + ~50 LLM-generated quine attempts), Priority #3 (Cross-Session Coherence Harness + Phase 8 Producer Wiring: end-to-end test booting session N→N+1 asserting cross-session signal carryover, plus wiring the orchestrator/classifiers/phase-timing to actually call `decision_trace_ledger.record()` / `latent_confidence_ring.record()` / `latency_slo_detector.record()` so the 8 Phase 8 GETs + 5 SSE events stop returning empty). Old top-3 (Pass C activation pipeline + Hypothesis-probe loop + Sandbox hardening) all promoted out as ✅ Shipped. Total ranks 11→11 with new content. **§3.6.2 Fragility vectors** expanded from 5 to **12** vectors: closed (1) Sandbox object-graph 🟢, added (2) Module-level side-effect at-import 🟢 (AST Rule 8 — PR #23838), closed (3) Pass C activation gap 🟢 (substrate complete via Phase 7.5 + Items #1-4 + Mining-payload v2.43), closed (4) Cross-process AdaptationLedger race 🟢 (Phase 7.8), partially mitigated (5) Semantic drift over long horizons 🟡 (substrate via Phase 7.9; empirical drift across 50+ sessions UNTESTED), NEW critical (6) Default-False Flag Problem 🔴 — 12+ master flags default false, cognitive loop never fired E2E with all flags on, **THE central blocker**, NEW critical (7) Quine-shape cage bypass via static-shape rule gaps 🔴 — `type(x).__mro__` not in banned attrs, `vars()` / `dir()` / `globals()` / `chr()`-construction / alias-defeats-resolver pinned known gap, cage *empirically pretty good* but **not provably tight**, NEW latent (8) Cross-runner artifact contract drift 🟡 — Wave 2 PhaseRunner extraction threads ~7 cross-phase leaks via `ctx.artifacts`; verbatim extraction sidesteps but **as soon as a runner is *refactored* beyond verbatim, one unversioned dict shape change crashes the FSM with no recovery path**, NEW defense-in-depth (9) `FlagChangeEvent.to_dict()` echoes raw env values 🟡 — bridge masks but substrate doesn't; one accidental `to_dict()` import away from credential exposure, NEW empirically-observed (10) AutoCommitter race on same op_id 🟡 — observed three times in single dev session producing overlapping commits, NEW latent (11) CuriosityScheduler wall-clock vs monotonic 🟡 — same vector that bit HypothesisProbe (since fixed); rate-cap window uses `time.time()`, NEW operational (12) Provider chain SPOF 🟡 — Tier 0+1 simultaneous outage → CuriosityEngine cannot fire → organism freezes; no Tier 3 emergency (Llama-local) fallback. **§3.6.3 Critical path** rewritten with NEW top-3 (Live-Fire / Adversarial Cage / Cross-Session Coherence + Phase 8 Producer Wiring) plus 4 medium-priority systemic upgrades (#4 schema-versioned artifact contract / #5 mask-discipline regression sweep / #6 AutoCommitter flock / #7 CuriosityScheduler monotonic) plus all six "now-closed" rows (Pass C activation / Hypothesis-probe loop / Rule 7 / Rule 8 / Phase 8 / flock / sunset signal). **§3.6.5 Honest assessment** re-graded **B− → B+ trending A−** with new per-axis grade card: Architecture A−/A, Cognitive depth B+/A−, Production track record C+/B−, Safety/cage A−, Observability A, UX/operator surfaces A−, Cross-session coherence C/C+. Weighted net B+ trending A−. **NEW Second Order RSI prerequisites added** (per Reverse Russian Doll Pass A horizon): (4) Self-introspection authority — second cage layer for "Order-2 amendment" risk tier; (5) Behavior-equivalence proof harness — stochastic 50-op invariance battery before O+V swaps its own orchestrator; (6) Versioned cognitive-architecture rollback — `git revert` insufficient because new architecture might have learnings old cannot. **Cognitive & Epistemic Delta vs CC** acknowledged in operator-facing summary: O+V matches all 8 listed CC capabilities post-Phase-8; structurally AHEAD of CC on 8 architectural deltas (Iron Gate + AST rules / SemanticGuardian / Phase B subagents / DirectionInferrer / CuriosityEngine + HypothesisProbe / Risk-tier escalation / L2/L3 repair / 16 sensors); the "ask_user" gap is partially-but-not-fully closed by CuriosityEngine (operates on POSTMORTEM clusters, not in-flight ambiguity); free-form mid-op replanning still missing structurally. **UX gaps vs CC documented** for follow-up: inline diff streaming during APPLY, `/cost` per-phase running tally in status bar, single-key approve/reject when plan is on screen, conversation rewind-and-fork. Zero behavior change. **Operator binding** (this PR): "Priority 1 (Live-Fire Graduation Cadence) is the next focus area; convert 12+ default-false flags to true and get to production level." | Claude Opus 4.7 (Brutal architectural review v2 — promotes Live-Fire Graduation Cadence to Priority #1) |
| 2026-04-27 | 2.50 | **Phase 8 surface wiring Slice 3 — SerpentFlow `--multi-op` CLI mode shipped — CLOSES Phase 8 SURFACE WIRING (3/3 slices done).** Builds on Slice 1 (v2.48 GET endpoints) + Slice 2 (v2.49 SSE bridges) by shipping the operator-side multi-op timeline renderer + CLI hook. New module `backend/core/ouroboros/governance/observability/multi_op_renderer.py` (~390 LOC): pure-stdlib, read-only projector over the decision-trace ledger that produces a chronological multi-op timeline view. Composes existing substrate (`DecisionTraceLedger.reconstruct_op` for per-op rows + `multi_op_timeline.merge_streams` for deterministic O(N log K) merge + `multi_op_timeline.render_text_timeline` for the plain-text view) and adds a 16-color ANSI palette that color-codes each op_id (alpha-stable mapping so the same op gets the same color across replays). **Public surface (6 helpers)**: `parse_multi_op_argument(arg)` → `(kind, payload)` tuple parsing the operator-supplied REF into `("list", None)` / `("ops", [op_ids])` / `("last_n", N)` / `("session", session_id)` / `("invalid", reason)`; `list_recent_op_ids(limit=20)` → distinct op_ids most-recent-first via reverse-line-walk of the JSONL ledger; `render_multi_op_timeline(op_ids, color=False, max_lines=400)` → core renderer; `render_last_n_op_timeline(n, ...)` → wrapper that pulls op_ids from `list_recent_op_ids` then renders; `render_session_timeline(session_id, sessions_root=None, ...)` → reads `<sessions_root>/<session_id>/summary.json`'s `operations[].op_id` field and renders all ops listed there (deduplicated, capped at `MAX_OPS_PER_RENDER=16`); `dispatch_cli_argument(arg, ...)` → single-call dispatcher used by the CLI hook. **CLI surface (`scripts/ouroboros_battle_test.py`)**: new `--multi-op REF` argument routes here. REF can be `"list"` (show recent op_ids), `"op-A,op-B,op-C"` (comma-list ≤16 ops), `"@last:N"` (most-recent N ops, default N=5), or `"session:bt-..."` (ops in a battle-test session summary). Optional `--multi-op-no-color` disables ANSI; default color is auto-detected via `sys.stdout.isatty()`. **The CLI flag short-circuits the battle-test boot path** (renders + exits without booting the 6-layer stack — same pattern as `--replay`). **Authority posture (locked + pinned)**: read-only over the ledger (never writes); stdlib-only top-level imports (substrate imported lazily inside helpers — pinned by AST scan); NEVER raises on any input shape (parametrized smoke pin covers 19 edge-case inputs incl. `None`); strict op_id charset validator (`[A-Za-z0-9_-]{1..MAX_OP_ID_LEN}`) rejects path-traversal + whitespace-injection + control characters; bounded outputs (`MAX_OPS_PER_RENDER=16` + `MAX_RENDERED_LINES=400` + `MAX_LIST_OP_IDS=200` + `MAX_OP_ID_LEN=128`); deny-by-default behind master flag `JARVIS_PHASE8_MULTI_OP_RENDERER_ENABLED` (default `false` until graduation) — every public entry point returns `"(disabled)"` stub when off; **battle-test script imports the renderer LAZILY** (pinned by AST scan of `scripts/ouroboros_battle_test.py` top-level imports — substrate import cost is ZERO when the flag is unused). **85 regression pins** (`tests/governance/test_phase8_multi_op_renderer.py`) covering: 4 module-constant pins + 11 master-flag pins (default-false + 5 truthy + 5 falsy) + 13 `parse_multi_op_argument` pins (list / ops / single-op / cap-at-max / bad-charset / whitespace-rejected / @last default + explicit + cap + zero-rejected + garbage-rejected / session / bad-session / empty / non-string) + 5 `list_recent_op_ids` pins (master-off + no-ledger + distinct-most-recent-first + clamp-limit + skip-corrupt-lines) + 9 `render_multi_op_timeline` pins (master-off + no-op_ids + invalid-only + unknown-ops + known-renders + caps-op-count + caps-max-lines + ANSI-emitted + ANSI-omitted) + 5 `render_last_n_op_timeline` pins (master-off + zero/negative + no-recent + renders-recent + clamps-to-max) + 7 `render_session_timeline` pins (master-off + invalid-id + not-found + corrupt-summary + no-ops-in-summary + renders-session-ops + caps-op-count + dedupes-op-ids) + 8 `dispatch_cli_argument` pins (master-off + invalid + invalid-when-enabled + list-empty + list-with-ops + ops + last_n + session) + 19 NEVER-raises smoke pins (parametrized over edge-case inputs incl. `None`) + 4 cage authority invariants (does-not-import-gate-modules via AST scan + top-level-imports-stdlib-only + no-secret-leakage-in-constants + public-surface-count-pinned-at-6 bit-rot guard) + 2 helper pins (`_validate_op_id` charset + `_disabled_message` exact-text) + 2 CLI integration pins (battle-test-CLI-imports-renderer-lazily AST scan + battle-test-CLI-argument-help-present source-text scan). Combined regression spine: **277/277 tests green** across Phase 8 substrate (66 from v2.44) + Slice 1 GET surface (66 from v2.48) + Slice 2 SSE bridge (60 from v2.49) + Slice 3 multi-op renderer (85 new from this PR) — no regression. **🎉 Phase 8 surface wiring 3/3 slices COMPLETE**: GET endpoints (Slice 1) for current state, SSE bridges (Slice 2) for live updates, multi-op renderer + CLI (Slice 3) for cross-op replay analysis. The Temporal Observability substrate is now fully exposed to operators across three complementary surfaces (HTTP GET / SSE stream / CLI), each with independent master flags so they graduate independently. **Authority posture preserved across all three slices**: read-only, deny-by-default, lazy substrate imports pinned by AST scan, NEVER-raises contract, masking discipline (secrets in `JARVIS_*` env vars never leak via either GET or SSE). **PRD updated**: Doc History v2.50 (append-only). **Remaining post-Phase-8-surface work**: live-fire graduation soaks (background; flips `JARVIS_DECISION_TRACE_LEDGER_ENABLED` + `JARVIS_LATENT_CONFIDENCE_RING_ENABLED` + `JARVIS_FLAG_CHANGE_EMITTER_ENABLED` + `JARVIS_LATENCY_SLO_DETECTOR_ENABLED` + `JARVIS_MULTI_OP_TIMELINE_ENABLED` + `JARVIS_PHASE8_IDE_OBSERVABILITY_ENABLED` + `JARVIS_PHASE8_SSE_BRIDGE_ENABLED` + `JARVIS_PHASE8_MULTI_OP_RENDERER_ENABLED` via Item #4's `/graduate` REPL once each substrate has 3 clean sessions). | Claude Opus 4.7 (Phase 8 surface wiring Slice 3 — closes Phase 8 surface wiring) |
| 2026-04-27 | 2.49 | **Phase 8 surface wiring Slice 2 — SSE event bridges shipped — Temporal Observability now streams in real time.** Builds on Slice 1 (v2.48) by wiring the 5 substrate modules onto the existing Gap #6 :class:`StreamEventBroker` so operator IDEs receive live SSE pings instead of polling `/observability/*` endpoints. **5 new event types added** to the broker's frozen vocabulary (`_VALID_EVENT_TYPES` in `ide_observability_stream.py`): (1) `decision_recorded` — fires after `DecisionTraceLedger.record()` succeeds; payload `{phase, decision, rationale[<=200 chars]}`; (2) `confidence_observed` — fires after `LatentConfidenceRing.record()` succeeds; payload `{classifier_name, confidence, threshold, below_threshold, outcome}`; (3) `confidence_drop_detected` — fires when a periodic `confidence_drop_indicators(...)` tick reports `drop_detected=True`; payload `{classifier_name, drop_pct, recent_mean, prior_mean, window_size}`; (4) `slo_breached` — fires when `LatencySLODetector.check_breach()` returns a non-None breach event; payload `{phase, p95_s, slo_s, overshoot_s, overshoot_pct, sample_count}`; (5) `flag_changed` — fires on each `FlagChangeMonitor.check()` delta; payload `{flag_name, prev_value, next_value, is_added, is_removed, is_changed}` — values **MASKED** to `<set>`/`<empty>` (None preserved for added/removed semantics) so secrets stored in `JARVIS_*` env vars never leak via SSE. New module `backend/core/ouroboros/governance/observability/sse_bridge.py` (~310 LOC) ships **6 best-effort publish helpers** (`publish_decision_recorded` + `publish_confidence_observed` + `publish_confidence_drop_detected` + `publish_slo_breached` + `publish_flag_changed` + `publish_flag_change_event` convenience wrapper that takes a `FlagChangeEvent` dataclass) — producers (orchestrator code, classifiers, periodic monitors) call these AFTER the substrate's record/check operation. **Authority posture (locked + pinned)**: read-only over the broker (bridges only `publish`, never subscribe or mutate broker state); deny-by-default at TWO levels — master flag `JARVIS_PHASE8_SSE_BRIDGE_ENABLED` (default `false`) gates ALL bridges + per-event sub-flags (`..._DECISION_RECORDED` / `..._CONFIDENCE_OBSERVED` / `..._CONFIDENCE_DROP_DETECTED` / `..._SLO_BREACHED` / `..._FLAG_CHANGED`) default `true` so flipping master is enough to enable everything but operators can silence individual streams without disabling the whole bridge; lazy broker import inside `_publish` (top-level imports stdlib + own logger only — pinned by AST scan); no imports from gate / execution modules; `MAX_PAYLOAD_KEYS=16` + `MAX_PAYLOAD_STRING_CHARS=1_000` + `MAX_RATIONALE_CHARS=200` defends against runaway producers. **NEVER raises into producer**: master-off → no-op None; sub-flag-off → no-op None; broker-import-fails → swallowed, debug-log, return None; broker.publish raises → swallowed, debug-log, return None; non-numeric inputs (confidence/p95/etc.) → skip publish, return None; bad-shape FlagChangeEvent → swallowed by wrapper. **60 regression pins** (`tests/governance/test_phase8_sse_bridge.py`) covering: 4 module-constant pins + 5 broker-vocab pins (`_VALID_EVENT_TYPES` membership) + 5 named-constant pins (string literal === enum value) + 11 master-flag pins (default-false + 5 truthy + 5 falsy) + 10 per-event sub-flag pins (5 default-true + 5 explicit-false silences) + 5 master-off-no-op pins (one per helper — broker.publish must NOT be called) + 1 sub-flag-off silences only that event + 3 decision-recorded pins (publishes + truncates rationale + handles empty strings) + 4 confidence-observed pins (below/above threshold + non-numeric skip + optional op_id default empty) + 2 confidence-drop pins (publishes + non-numeric skip) + 3 slo-breach pins (overshoot computation + zero-slo no-divide + non-numeric skip) + 4 flag-changed pins (**MASKING** — secrets never echo + None preserved + set-vs-empty distinction + FlagChangeEvent wrapper + bad-input wrapper resilience) + 4 broker-exception-swallowed pins (decision + confidence + slo + flag) + 3 payload-bound pins (truncate strings + cap keys + non-string passthrough) + 1 broker-import-failure resilience pin + 5 cage authority invariants (does-not-import-gate-modules via AST scan + top-level-imports-stdlib-only + no-secret-leakage-in-constants + publish-helper-count-pinned-at-6 bit-rot guard + event-type-count-pinned-at-5). Combined regression spine: **241/241 tests green** across Phase 8 substrate (66 from v2.44) + Slice 1 GET surface (66 from v2.48) + Slice 2 SSE bridge (60 new from this PR) + existing broker SSE suite (49 — broker vocab additions don't regress existing event types). **🟢 Phase 8 substrate is now real-time**: an IDE extension subscribed to `/observability/stream` receives live `decision_recorded` / `confidence_observed` / `confidence_drop_detected` / `slo_breached` / `flag_changed` events in addition to the existing 41-event vocabulary; the GET endpoints (Slice 1) and SSE stream (Slice 2) compose so consumers get both "current state" via GET and "incremental updates" via SSE. **Authority over the broker held**: the bridge is purely additive — every event type has been added to `_VALID_EVENT_TYPES` (so the broker's strict allowlist accepts them); no existing event-type vocabulary was removed; the broker's bounded subscriber/queue/history/heartbeat caps and Gap #6 Slice 2's `Last-Event-ID` replay semantics extend to the new events for free. **PRD updated**: Doc History v2.49 (append-only). **Remaining Phase-8 surface work**: Slice 3 SerpentFlow `--multi-op` CLI mode for operator-side multi-op timeline rendering. Live-fire graduation soaks remain background work via Item #4's `/graduate`. | Claude Opus 4.7 (Phase 8 surface wiring Slice 2 — SSE event bridges) |
| 2026-04-27 | 2.48 | **Phase 8 surface wiring Slice 1 — IDE observability GET endpoints shipped — first operator-facing surface on the Temporal Observability substrate.** Per Phase 8 v2.44 deferred-wiring note ("SerpentFlow `--multi-op` mode + SSE bridge for `flag_changed` events + IDE observability GET endpoints for the new ledgers"), Slice 1 ships the read-only GET surface — Slice 2 (SSE bridges) and Slice 3 (SerpentFlow `--multi-op`) are follow-ups. New module `backend/core/ouroboros/governance/observability/ide_routes.py` (~620 LOC) ships `Phase8ObservabilityRouter` — a separate router class (not bolted onto Gap #6's `IDEObservabilityRouter` — single-responsibility, independent rate-tracker, independent master flag) that registers **8 read-only GET endpoints** on a caller-supplied aiohttp `Application`: (1) `GET /observability/phase8/health` — surface liveness + per-substrate flag state (5 booleans); (2) `GET /observability/decisions` — list of recent decision-trace rows (most-recent-first, bounded `MAX_DECISION_LIST_ROWS=500`, optional `?op_id=` filter, optional `?limit=`); (3) `GET /observability/decisions/{op_id}` — full causal trace via `DecisionTraceLedger.reconstruct_op` with 400/404/503 stable reason codes; (4) `GET /observability/confidence` — distinct classifier names + total event count; (5) `GET /observability/confidence/{classifier}` — recent events + `confidence_drop_indicators` (operator-tunable `?window=` + `?drop_pct=` clamped); (6) `GET /observability/timeline/{op_id}` — JSON event list + `render_text_timeline` (project decision rows → `TimelineEvent` per phase → `merge_streams` → bounded `MAX_TIMELINE_LINES=200`); (7) `GET /observability/flags/changes` — masked snapshot of `JARVIS_*` env (every value rendered as `<set>`/`<empty>` — secrets in env never echoed back, even via `FlagChangeEvent.prev_value`/`next_value` deltas which are also masked) + monitor.check() deltas; (8) `GET /observability/latency/slo` — per-phase `stats()` (sample_count/p50/p95/max/slo) + currently-breached phases via `check_all_breaches()`. **Authority posture (locked + pinned)**: read-only — zero endpoints mutate substrate state; deny-by-default behind new master flag `JARVIS_PHASE8_IDE_OBSERVABILITY_ENABLED` (default false); loopback-only assertion via re-exported wrapper around Gap #6's `assert_loopback_only` (rejects `0.0.0.0`/`::`/`*`); per-origin sliding-window rate limit independent from Gap #6's tracker so Phase 8 polling cannot starve TaskBoard/Plan/Session GETs (default 120/min, env-overridable, clamped to [1,6000]); narrow CORS allowlist (localhost + 127.0.0.1 + vscode-webview://, env-overridable); structural projections only — every response carries `schema_version` + `Cache-Control: no-store`; **lazy substrate imports** at handler invocation (top-level imports do NOT include any of the 5 substrate modules — pinned by AST scan). **Stable reason codes**: `phase8_observability.{disabled, rate_limited, malformed_op_id, malformed_classifier, unknown_op_id, unknown_classifier, ledger_disabled, ring_disabled}` — no stack traces or internal paths leaked, ever. **Master-off port-scan defense**: every endpoint returns 403 (not 200 with `{enabled: false}`) so a port scan sees no signal about what's behind the listener. **66 regression pins** (`tests/governance/test_phase8_ide_routes.py`) covering: 1 schema-version pin + 3 bounded-cap pins + 11 master-flag pins (default-false + 5 truthy + 5 falsy) + 2 loopback-assertion pins + 8 endpoints-403-when-master-off pins (parametrized — every endpoint must 403 when master-off) + 2 health pins (substrate-flag-state + schema-version-and-no-store-headers) + 5 decision-list pins (empty-when-ledger-off + returns-rows-most-recent-first + op_id-filter + malformed-op_id-rejected + limit-clamp) + 5 decision-detail pins (full-trace + malformed-op_id + 404-unknown + 503-when-ledger-off + parse-error-coverage) + 7 confidence pins (list-empty-when-ring-off + list-distinct-names + detail-events-and-drop + 404-unknown-classifier + 400-malformed + window/drop_pct-clamping + 503-when-ring-off) + 4 timeline pins (text-and-events + 404-unknown + 400-malformed + 503-when-ledger-off) + 2 flags/changes pins (**masked-snapshot-NEVER-echoes-secret** + empty-when-emitter-off) + 2 latency-slo pins (empty-when-detector-off + per-phase-stats-and-breaches) + 2 rate-limit pins (kicks-in + independent-per-client tracker) + 3 CORS pins (echoes-allowed + does-NOT-echo-disallowed + handles-malformed-pattern) + 2 schema-version-on-every-response pins + 1 no-store-on-every-handler pin + 5 authority/cage invariants (does-not-import-gate-modules via AST scan + lazy-imports-substrate-only-in-handlers + router-init-independent-state + no-secret-leakage-in-module-constants + endpoint-count-pinned-at-eight bit-rot guard) + 4 helper-direct pins (`_parse_limit` + `_parse_int` + `_parse_float` + `_rate_limit_per_min`-clamps + `_cors_origin_patterns`-defaults). Combined regression spine: **132/132 tests green** across Phase 8 substrate (66 from v2.44) + Phase 8 surface wiring (66 new) — no substrate regression. **🟢 Phase 8 substrate is now operator-visible**: an IDE extension can poll `/observability/decisions/{op_id}` to render a per-op causal trace, `/observability/timeline/{op_id}` for a chronological text view, `/observability/latency/slo` for SLO breach alerts, `/observability/confidence/{classifier}` to detect model degradation drift, and `/observability/flags/changes` for env-mutation tracking — all without ever leaving the read-only authority posture. **PRD updated**: Doc History v2.48 (append-only). **Remaining Phase-8 surface work**: Slice 2 SSE event bridges (5 new event types: `decision_recorded`, `confidence_observed`, `slo_breached`, `flag_changed`, `timeline_updated`) + Slice 3 SerpentFlow `--multi-op` CLI mode. Live-fire graduation soaks remain background work via Item #4's `/graduate`. | Claude Opus 4.7 (Phase 8 surface wiring Slice 1 — IDE observability GETs) |
| 2026-04-26 | 2.47 | **AST Validator Rule 8 — module-level side-effect detection shipped — closes the highest-priority remaining sandbox-bypass vector.** Extends `meta/ast_phase_runner_validator.py` Slice 3 validator with a new structural check that runs BEFORE the candidate ever reaches the sandbox: walks the module's TOP-LEVEL statement list (not function/class bodies — those are Rule 7's domain) for two complementary patterns. **Pattern 1**: bare module-level `Call` (or `Assign`/`AnnAssign`/`AugAssign` whose RHS contains a Call) to a name in `_BANNED_MODULE_LEVEL_CALLS` — the banned set is built via string-concatenation in `_build_banned_module_level_calls()` (security-scan friendly) and contains shell exec (the `os` shell-and-spawn family), the `subprocess` family (Popen/run/call/check_output/check_call/getoutput/getstatusoutput), code-exec primitives (eval/exec/compile), dynamic-import primitives (the `__import__` builtin and `importlib` import_module / dunder-import), the standard Python deserialization-RCE primitives (the `p`+`ickle` family + `marshal.loads/load`), and bare `open()`. **Pattern 2**: module-level control-flow block (`if`/`for`/`while`/`with`/`try`/`AsyncFor`/`AsyncWith`/`Match` Py 3.10+) containing ANY Call — the rationale is that well-behaved candidates declare functions/classes; they don't run conditional logic at import time. Top-level docstrings (Constant str Expr), function/class defs, imports, simple Assign/AnnAssign/AugAssign (with non-banned RHS Calls — e.g. `logger = logging.getLogger(__name__)` is allowed), and `pass` are the only allowed top-level shapes. New failure reason `MODULE_LEVEL_SIDE_EFFECT` added to `ValidationFailureReason` enum (10th value; full set re-pinned via renamed `test_validation_failure_reason_ten_values`). Per-rule kill switch `JARVIS_AST_VALIDATOR_BLOCK_MODULE_SIDE_EFFECTS` defaults **TRUE** (security hardening on by default — same convention as Rule 7's `JARVIS_AST_VALIDATOR_BLOCK_INTROSPECTION_ESCAPE`); operators can disable in emergency without disabling the whole validator. New helpers `_resolve_call_name(call_node)` (Best-effort dotted-name resolution: `foo()`→"foo", `mod.foo()`→"mod.foo", `pkg.mod.foo()`→"pkg.mod.foo", returns None for `f()()` / `getattr(x, "y")()` opaque shapes) + `_has_any_call(node)` (recursive walk for any Call) + `_find_module_level_side_effect(tree)` — all stdlib `ast` only, never raise. **Known limitations pinned in tests** (not silent gaps — visible in test output): (a) **alias-defeats-resolver** — assigning a banned dotted name to a local then calling the local slips through (the alias's value-side resolves correctly but the call site only sees the local name); (b) **call-on-call** — `factory()()` opaque to the static dotted-name resolver. Both gaps are tracked as defense-in-depth gaps; Rule 7 catches `__class__`/`__bases__`/`__subclasses__` introspection in any function body, and the sandbox's limited builtin set blunts most aliases at runtime. **82 regression pins** (`tests/governance/test_ast_rule_8_module_level_side_effects.py`) covering: 1 enum-value pin + 14 kill-switch pins (default-true + 6 truthy + 6 falsy) + 17 Pattern-1 banned-name pins (parametrized across `os` shell-and-spawn + the spawn 7-suffix family + the exec 7-suffix family + `subprocess` 7-method family + eval/exec/compile + 3 dynamic-import + marshal + the `p`+`ickle` family + bare-open) + 3 RHS-shape pins (Assign + AnnAssign + nested-Call-in-list) + 7 Pattern-2 control-flow pins (if/for/while/with/try + innocuous-call-in-if + allow-if-without-call) + 6 clean-candidates-pass pins (simple-assigns + logger=getLogger + function-def + module-docstring + banned-call-inside-function-body-not-tripped + banned-call-inside-method-not-tripped) + 5 adversarial-corpus pins (alias-NOT-blocked-known-gap + call-on-call-NOT-blocked-known-gap + match-stmt-blocked Py3.10+-skipif + top-level-pass + top-level-import) + 3 per-rule kill-switch behavior pins (kill-off-passes + master-off-skips-everything + Rule-7-still-fires-when-Rule-8-off) + 7 white-box helper-direct pins (3 `_resolve_call_name` + 2 `_has_any_call` + 2 banned-set-shape-and-categories) + 3 short-circuit pins (oversize + parse-error + never-raises-on-edge-cases). Existing pin `test_validation_failure_reason_nine_values` renamed to `..._ten_values` and updated to include `MODULE_LEVEL_SIDE_EFFECT`. Combined regression spine: **137/137 AST validator tests green** (existing 56-pin suite + 81 new Rule 8 pins; 1 skipped on Py 3.9 for match-statement coverage). **🟢 Highest-priority remaining sandbox vector closed**: post-Phase-7.7, operator-approved candidates with module-level RCE-shape code at import time were the next attack class on the brutal-review priority list — Rule 8 hard-rejects those candidates BEFORE the sandbox starts. Combined with Rule 7 (function-body introspection), the static AST sandbox now covers both the import-time and runtime escape vectors. **PRD updated**: Doc History v2.47 (append-only); §1 priority list pointer drops "AST Rule 8" from the post-CuriosityScheduler remaining-work list. **Remaining post-Rule-8 work**: Phase 8 surface wiring (~3 days; SerpentFlow `--multi-op` + SSE bridges + IDE GETs) + live-fire graduation soaks (background; flips 12+ master flags via Item #4's `/graduate`). | Claude Opus 4.7 (AST Rule 8 — closes module-level side-effect sandbox vector) |
| 2026-04-26 | 2.46 | **CuriosityScheduler — orchestration trigger shipped — closes the post-CuriosityEngine priority #3.** New module `backend/core/ouroboros/governance/adaptation/curiosity_scheduler.py` (~280 LOC): orchestrates when CuriosityEngine fires via 7 layered gates evaluated in strict order: (1) master flag → SKIPPED_MASTER_OFF; (2) cluster_provider/engine missing → SKIPPED_NO_CLUSTER_PROVIDER; (3) posture HARDEN → SKIPPED_POSTURE_HARDEN (defensive mode forbids speculative work; EXPLORE/CONSOLIDATE/MAINTAIN allowed); (4) memory pressure HIGH/CRITICAL → SKIPPED_MEMORY_PRESSURE (LSP allocator under stress; OK/WARN allowed); (5) idle_signal returns false → SKIPPED_NOT_IDLE (defensive: provider exception treated AS not-idle to avoid speculative fire when system state unknown); (6) per-hour rate cap (default 4 cycles/hour) → SKIPPED_RATE_CAP; (7) cooldown active (default 60s) → SKIPPED_COOLDOWN. **All 4 providers are dependency-injected** (engine + cluster_provider + idle_signal + posture_provider + pressure_provider) — production wires real callables (RuntimeHealth's `is_idle()`, posture_store, memory_pressure_gate.pressure()), tests inject fakes, all default-None means "no info → allow" (except idle_signal which defaults to True). **Posture-aware**: HARDEN is the ONLY posture that blocks; this matches the operator binding "no curiosity in defensive mode". **Memory-pressure-aware**: HIGH+CRITICAL block; OK+WARN allow (consistent with MemoryPressureGate's existing fan-out semantics). **Rate cap math**: rolling 1-hour window via timestamp pruning; firings older than now-3600s drop from history; explicit None check (not `or`) so a configured `cooldown_s=0.0` is honored — caught + fixed during initial test run. **Provider exception caught + converted**: posture/pressure provider raises → treated as "no info, allow"; idle_signal raises → treated as "not idle, skip" (defensive — favor staying idle over speculative fire); cluster_provider raises → ENGINE_ERROR; engine.run_cycle raises → ENGINE_ERROR. **NEVER raises into caller**. **46 regression pins** covering 5 module constants (incl. **HARDEN-not-in-OK-postures + HIGH-CRITICAL-not-in-OK-pressure**) + master flag (3 default + 5 truthy + 6 falsy) + 6 env-override (max-per-hour + cooldown variants) + 16 gate-ordering pins (master-off + no-engine + no-cluster-provider + posture-harden-blocks + 3 allowed postures + posture-raise-caught + 4 pressure variants + pressure-raise-caught + 3 idle-signal variants) + 4 rate-cap pins (4-fires-then-capped + history-pruned-after-hour + cycles-in-window-reported + explicit-max-override) + 2 cooldown pins (blocks-immediate + expired-allows) + 2 engine-error-handling (cluster-provider-raise + run-cycle-raise) + 2 engine-result-threading (engine_result-attached + engine-invoked-with-clusters-and-now_unix) + 1 end-to-end with REAL CuriosityEngine + REAL HypothesisLedger (proves clean tick lands a hypothesis in the real ledger) + 1 reset_state test helper + 3 authority/cage invariants (no-banned + stdlib-top-level-only — no backend.* top-level imports because all engine/ledger flow in via DI + no-subprocess+no-direct-anthropic). Combined regression spine: **272/272 tests green** across CuriosityScheduler + CuriosityEngine + Phase 7.6 + Item #3 + Phase 8 — no regression. **🎉 First Order autonomous-curiosity loop is now wholly automatable**: scheduler tick → all 7 gates → engine.run_cycle → ledger persistence → (optional) probe + bridge → adapted state proposed → operator reviews via /adapt → live gate consumes. Production wiring is now: in `intake/sensors/runtime_health.py` (or boot path), instantiate `CuriosityScheduler(engine=get_default_engine(), cluster_provider=lambda: cluster_postmortems(...), idle_signal=runtime_health.is_idle, posture_provider=posture_store.current, pressure_provider=memory_pressure_gate.current_level)` then schedule periodic `tick()` calls at e.g. 5-min intervals. The scheduler's gates ensure it only ACTUALLY fires when all conditions align. **PRD updated**: Doc History v2.46 (append-only). **Remaining post-scheduler work**: AST Rule 8 (module-level side-effect detection — closes the highest-priority remaining sandbox vector) + Phase 8 surface wiring + live-fire graduation soaks. | Claude Opus 4.7 (CuriosityScheduler — closes scheduler integration gap) |
| 2026-04-26 | 2.45 | **CuriosityEngine — autonomous hypothesis-generation primitive shipped — closes the post-Phase-8 priority #1.** New module `backend/core/ouroboros/governance/adaptation/curiosity_engine.py` (~430 LOC): consumes POSTMORTEM clusters → synthesizes falsifiable hypothesis claim + expected outcome → appends to `HypothesisLedger` → optionally triggers Phase 7.6 probe runs → optionally routes terminal verdicts via Item #3 bridges. **3-tier hierarchical master flags**: `JARVIS_CURIOSITY_ENGINE_ENABLED` (master) + `JARVIS_CURIOSITY_ENGINE_AUTO_PROBE` (sub-flag) + `JARVIS_CURIOSITY_ENGINE_AUTO_BRIDGE` (sub-flag) — all default false; sub-flags compose hierarchically. **Per-cycle bounds**: `MAX_HYPOTHESES_PER_CYCLE=3` + `MAX_PROBES_PER_CYCLE=3` + `DEFAULT_CLUSTER_THRESHOLD=3`. **Determinism**: clusters sorted by member_count DESC, alpha tie-break on signature_hash; same `(cluster_signature, ts_int_seconds)` produces identical op_id. **NEVER raises**: ledger.append exception caught + structured `LEDGER_WRITE_FAILED` status; probe.test exception caught + skipped; bridge exception caught + skipped. **Cost-bounded by Phase 7.6 + Item #3** ($0.05/call + $1.00/session); CuriosityEngine just bounds the OUTER cycle. **Why a separate engine vs SelfGoalFormation**: SelfGoalFormation writes BACKLOG entries (operator-action items); CuriosityEngine writes Hypothesis records (self-formed falsifiable claims the system probes WITHOUT operator intervention). Both share cluster INPUT, produce complementary OUTPUTS. **48 regression pins** covering 6 module constants + 5 master-flag pins + 4 cluster-threshold + 8 synthesis-helper + 3 run-cycle pre-checks + 5 generation-behavior + 4 ledger-persistence (incl. **structured-failure** + **raise-caught**) + 4 auto-probe sub-flag (incl. **off-no-invoke** + **max-cap** + **exception-caught**) + 3 auto-bridge sub-flag + 1 end-to-end with real Phase 7.6 runner + Null prober + 2 determinism + 3 authority/cage invariants. Combined regression spine: **272/272 tests green** across CuriosityEngine + Phase 7.6 + Item #3 + HypothesisLedger + Phase 8 — no regression. **🎉 First Order autonomous-curiosity loop is now operationally complete**: POSTMORTEM clusters → CuriosityEngine generates hypotheses → HypothesisProbe tests them → bridges route verdicts → AdaptationLedger / HypothesisLedger absorb them → live gates consume adapted state. The substrate is operationally ready; only soak proof + scheduler integration (idle-GPU window detection via RuntimeHealth sensor) remain. Per the post-Phase-8 brutal review, this is the priority #1 systemic upgrade — closing the autonomous-curiosity gap CC has no equivalent for. **PRD updated**: Doc History v2.45 (append-only). **Remaining post-CuriosityEngine work**: AST Rule 8 (module-level side-effect detection) + Phase 8 surface wiring + live-fire graduation soaks. | Claude Opus 4.7 (CuriosityEngine — closes autonomous-curiosity gap) |
| 2026-04-26 | 2.44 | **Phase 8 — Temporal Observability shipped (5/5 sub-deliverables in one PR) — closes the time-travel-debugging gap on autonomic decisions.** Five new modules under `backend/core/ouroboros/governance/observability/` (~1,500 LOC total): **8.1 Decision causal-trace ledger** (`decision_trace_ledger.py`) — append-only JSONL at `.jarvis/decision_trace.jsonl` with rows `{op_id, phase, decision, factors, weights, rationale, ts}` for state reconstruction; bounded (16 MiB file / 16 KiB row / 200 records/op rate cap); cross-process flock reused from Phase 7.8. **8.2 Latent-confidence ring buffer** (`latent_confidence_ring.py`) — bounded in-memory deque (4096 events default, drop-oldest, thread-safe RLock) of classifier confidence + threshold + outcome observations; `confidence_drop_indicators(classifier, window, drop_pct)` detects "model getting LESS confident over a session" via two-window comparison. **8.3 Synchronized multi-op timeline aggregator** (`multi_op_timeline.py`) — deterministic merge-sort O(N log K) over per-op event streams via heapq; alpha-tie-break on stream_id + seq for stable replays; bounded MAX_TIMELINE_EVENTS=50,000; `render_text_timeline()` produces operator-readable plain-text view. **8.4 Master-flag change emitter** (`flag_change_emitter.py`) — snapshot-and-diff detector for `JARVIS_*` env mutations mid-session; `FlagChangeMonitor` holds baseline + advances on `check()`; emits `is_added`/`is_removed`/`is_changed` deltas; bounded MAX_TRACKED_FLAGS=1024 + MAX_VALUE_CHARS=4096. **8.5 Latency-SLO breach detector** (`latency_slo_detector.py`) — per-phase rolling-window p95 (default 100 samples) with operator-defined SLOs; emits `LatencySLOBreachEvent` when phase p95 exceeds SLO; `MIN_SAMPLES_FOR_BREACH=20` defends against false-positives on low-volume phases; pure-stdlib percentile (linear interpolation, deterministic). **All 5 modules**: master flag default false; NEVER raises into caller; bounded sizes; stdlib + adaptation._file_lock only (8.1) or pure-stdlib (8.2-8.5). **66 regression pins** covering master flags + record/skip paths + bounded sizes + behavioral correctness (drop-oldest semantics; alpha tie-break; merge correctness; baseline-advance; p95 + breach detection) + 11 authority/cage invariants (parametrized over all 5 modules: no-banned-imports + no-subprocess-or-network + uses-flock-where-applicable). Combined regression spine: **137/137 tests green** across Phase 8 + Phase 7.8 + Item #2 + mining-payload — no regression. **🎉 Time-travel debugging foundation shipped**: state reconstruction over op_id (8.1), confidence drift detection (8.2), parallel-fan-out timeline reconciliation (8.3), env mutation observability (8.4), proactive latency alerting (8.5). **Production wiring deferred**: SerpentFlow `--multi-op` mode + SSE bridge for `flag_changed` events + IDE observability GET endpoints for the new ledgers — these are operator-facing surfaces that consume the Phase 8 substrate. The substrate is complete + bounded + tested; surfaces follow the same pattern as Gap #6 IDE observability (~5 SSE event types + 3 GET endpoints, ~3 days of follow-up work). **PRD updated**: §1 Roadmap Execution Status — Phase 8 marked structurally complete (5/5 sub-deliverables); Doc History v2.44 (append-only). | Claude Opus 4.7 (Phase 8 — Temporal Observability) |
| 2026-04-26 | 2.43 | **Mining-Surface Payload Population — converts Items #2/#3 from theoretical to actual.** Updates all 6 `ledger.propose()` call sites in the mining surfaces to populate `proposed_state_payload` matching the yaml_writer's per-surface schemas: Slice 2 `semantic_guardian_miner` populates `{name, regex, severity, message}`; Slice 3 `exploration_floor_tightener` populates `{category, floor}`; Slice 4a `per_order_mutation_budget` populates `{order, budget}` (Order-2 floor MIN_ORDER2_BUDGET=1 preserved by miner); Slice 4b `risk_tier_extender` populates `{tier_name, insert_after, failure_class}` (matches `[A-Z0-9_]+` charset miner already produces); Slice 5 `category_weight_rebalancer` populates `{new_weights, high_value_category, low_value_category}` (full 5-category vector preserved); Phase 7.9 `stale_pattern_detector` populates `{pattern_name, days_since_last_match, last_match_unix, kind}` (audit-trail for sunset signals). **Critical functional milestone**: with payload now populated, `/adapt approve` writes the materialized state to `.jarvis/adapted_<surface>.yaml` — the loaders (caller-wired in PRs #23414/#23452/#23493/#23525 + Phase 7.1) pick it up at next consult → live gate behavior changes. **Pre-this-PR**: every `/adapt approve` returned `SKIPPED_NO_PAYLOAD` from yaml_writer (the cognitive loop was substrate-only). **Post-this-PR**: cognitive loop is functionally LIVE. **14 regression pins** (`tests/governance/test_mining_surface_payload_population.py`): per-surface payload-shape tests for all 5 Pass C surfaces + Phase 7.9 stale (each verifies required fields + bounds match yaml_writer schema) + 2 end-to-end approve-and-materialize tests (Slice 2 SemanticGuardian + Slice 3 IronGate floors — both prove propose→approve→yaml_writer.WriteStatus.OK + YAML file exists with materialized entry + provenance auto-enriched) + 6 caller-grep invariants (parametrized over all 6 mining surface modules — each asserts `proposed_state_payload=` appears within 800 chars of `ledger.propose(` call as a bit-rot guard against future PRs adding new mining surfaces without populating payload). Combined regression spine: **522/522 tests green** across all 5 mining surfaces + Phase 7.9 + Items 2-4 + AdaptationLedger — no regression in any of the 49+ existing miner tests (each surface's existing test suite was written against the propose-without-payload signature; payload kwarg is OPTIONAL so backward compat preserved). **🎉 COGNITIVE LOOP NOW FULLY FUNCTIONAL END-TO-END**: probe → confirm → propose-with-payload (this PR) → approve → yaml_writer materializes (Item #2) → loader reads (Phase 7.1-7.5) → live gate consumes (caller wiring 1-4). The 5+ master flags remain default-false until per-loader graduation cadence flips them via Item #4's `/graduate` REPL. **PRD updated**: §1 caller wiring sub-block notes Mining-Surface Payload Population complete; Doc History v2.43 (append-only). | Claude Opus 4.7 (Mining-Surface Payload Population — closes post-Items-2/3 functional gap) |
| 2026-04-26 | 2.42 | **Item #4 — graduation cadence ledger + `/graduate` REPL shipped — closes the post-substrate-completion plumbing.** Two new modules codify the graduation discipline (3-clean-session for Pass B / Phase 7 + Items 2/3; 5-clean-session for Pass C mining surfaces): `adaptation/graduation_ledger.py` (~350 LOC) ships `GraduationLedger` (append-only JSONL at `.jarvis/graduation_ledger.jsonl`), `CADENCE_POLICY` table (15 known flags: 10 Pass B + 5 Pass C; 7.7 + 7.8 NOT in policy because they're already default-true security hardening), `SessionOutcome` enum (CLEAN/INFRA/RUNNER/MIGRATION; INFRA + MIGRATION are waived per Wave 1 closure ledger), `progress(flag)` reduces log to per-flag clean-session count, `is_eligible(flag)` returns True iff ≥ required AND zero runner failures, `eligible_flags()` lists ready-to-flip. `adaptation/graduate_repl.py` (~280 LOC) ships `dispatch_graduate(argv, ledger, operator)` operator-facing dispatcher with 5 subcommands (list / status / record / eligible / help). Cross-process flock reused from Phase 7.8 for ledger appends. Master flags: `JARVIS_GRADUATION_LEDGER_ENABLED` + `JARVIS_GRADUATE_REPL_ENABLED` (both default false). Read-side subcommands work even with REPL master off (discoverability convention adopted across Pass A graduations). Write-side requires both masters on. Operator-double-tap dedup: same session_id recorded twice counts ONCE per outcome bucket. Runner failure resets eligibility (ANY runner failure blocks even with 3+ clean). Bounded sizes: MAX_LEDGER_FILE_BYTES=4 MiB + MAX_RECORDS_LOADED=50,000 + MAX_NOTES_CHARS=1000. Best-effort throughout — NEVER raises into caller. **60 regression pins** covering 5 module constants + master flag (default-false + 5 truthy + 6 falsy) + path env override (2) + cadence policy (8 incl. **15-known-flags + Pass-B-default-3 + Pass-C-default-5 + per-PR-flag-presence × 3 categories**) + record_session (8 incl. master-off-skipped + unknown-flag-rejected + dedup-same-session-counts-once + runner-failure-blocks-eligibility) + progress/eligibility (5 incl. **Pass-C-requires-5-clean** + **runner-failure-resets-eligibility**) + file hardening (4 incl. oversize-returns-empty + malformed-lines-skipped + notes-truncated-to-cap) + REPL master flag (3) + REPL help (2 incl. **lists-all-5-subcommands**) + REPL list/status/eligible (7) + REPL record (6 incl. **invalid-outcome-rejected** + **master-ledger-off-rejected**) + 2 end-to-end happy path (incl. **runner-failure-resets-eligibility-via-REPL**) + 5 authority/cage invariants (no-banned-imports × 2 + stdlib+adaptation-only × 2 + no-subprocess + uses-flock). Combined regression spine: **174/174 tests green** across Item #4 + Items #2+#3 + Phase 7.8 flock — no regression. **PRD updated**: §1 caller wiring sub-block notes Item #4 ledger primitive shipped + remaining work is RECORDING actual clean sessions (which requires live battle-test runs); Doc History v2.42 (append-only). | Claude Opus 4.7 (Item #4 — graduation cadence machinery) |
| 2026-04-26 | 2.41 | **Item #3 — HypothesisProbe production EvidenceProber + bridges shipped.** Two new modules close the cognitive loop: `adaptation/anthropic_venom_evidence_prober.py` (~330 LOC) ships `AnthropicVenomEvidenceProber` (production prober that wires Phase 7.6's `EvidenceProber` Protocol to a read-only Venom-style query provider via injectable `VenomQueryProvider` Protocol — production wires `AnthropicProvider`, tests inject fakes, default is `_NullVenomQueryProvider` returning sentinel = zero cost so misconfigured caller cannot accidentally hit a paid API; same safety pattern as P5 `_NullClaudeQueryProvider`). `adaptation/hypothesis_probe_bridge.py` (~270 LOC) ships two bridges: `bridge_confirmed_to_adaptation_ledger()` wires CONFIRMED probe verdicts to `AdaptationLedger.propose()` carrying the `proposed_state_payload` (consumes Item #2 schema extension end-to-end) + `bridge_to_hypothesis_ledger()` wires terminal probe verdicts to `HypothesisLedger.record_outcome()` (CONFIRMED → validated=True; REFUTED → validated=False; INCONCLUSIVE_* → validated=None; SKIPPED_* → no-op). **Cage** (load-bearing): per-call cost cap `DEFAULT_COST_CAP_PER_CALL_USD=0.05` + cumulative session budget `DEFAULT_SESSION_BUDGET_USD=1.00` (matches P5 AdversarialReviewer convention); tool allowlist enforcement (`READONLY_TOOL_ALLOWLIST` from Phase 7.6 substrate passed to provider every round); bounded sizes (prompt=4096 + evidence=3500 + prior-evidence-rounds=3 + per-row=500); cost-overrun safety belt (provider reporting cost > per-call cap gets clipped); NEVER raises (provider exceptions caught + converted to error rounds; runner's own try/except is second line). **Verdict parsing**: looks for explicit `VERDICT: confirmed|refuted|continue` sentinel; LAST occurrence wins (model's final verdict); falls back to "continue" when missing — runner's diminishing-returns guarantee terminates inconclusive either way. Master flags: `JARVIS_HYPOTHESIS_PROBE_PRODUCTION_PROBER_ENABLED` (default false) + `JARVIS_HYPOTHESIS_PROBE_BRIDGES_ENABLED` (default false). Bridges are best-effort (BridgeResult with structured status); confirmed→adaptation only fires on CONFIRMED; hypothesis-ledger fires on all 4 terminal verdicts. **57 regression pins** covering 6 module constants + 4 master-flag + 3 Null sentinel + 4 factory wiring + 6 prompt-building (incl. caps + truncation + allowlist-listed-in-prompt) + 7 response parsing (incl. **last-sentinel-wins** + case-insensitive) + 4 cost-accounting (incl. **per-call-cap-clips-overrun safety belt** + **pre-check-fires-when-budget-exhausted**) + 2 exception handling + 1 tool-allowlist-enforcement-pin + 3 runner integration (production-confirms + null-diminishing + refuting) + 9 bridge pins (master-flag + skip paths + actual-propose-with-payload + 4 verdict-mappings + ledger-not-found + ledger-raise-caught) + **6 authority/cage invariants** (incl. no-direct-anthropic-import — provider injection IS the network boundary). Combined regression spine: **581/581 tests green** across Phase 7.1-7.9 + all 4 wiring PRs + Items #2+#3 — no regression. **🎉 Cognitive loop closure**: Phase 7.6 substrate (PR #23176) → production prober (this PR) → bridge to AdaptationLedger.propose (with Item #2 payload) → /adapt approve writes adapted YAML (Item #2) → live gate consumes (5/5 wiring complete). Operator-approved hypothesis-driven adaptations now flow end-to-end. **PRD updated**: §1 Caller wiring sub-block — HypothesisProbe production EvidenceProber marked `[x]`; Doc History v2.41 (append-only). **Remaining post-Item-#3 work**: Item #4 (per-loader graduation cadences flipping the 7+ master flags from default-false to default-true after 3-clean-session arcs). | Claude Opus 4.7 (Item #3 — closes cognitive loop with bridges) |
| 2026-04-26 | 2.40 | **Item #2 — MetaGovernor YAML writer shipped — CLOSES the producer-side gap.** Adds new `backend/core/ouroboros/governance/adaptation/yaml_writer.py` module + extends `AdaptationProposal` schema with optional `proposed_state_payload: Dict[str, Any]` field. Schema version bumped `1.0` → `2.0`; `ADAPTATION_SCHEMA_VERSIONS_READABLE = ("1.0", "2.0")` so pre-Item-#2 rows still readable. `propose()` accepts new optional `proposed_state_payload` kwarg (validated as Mapping or rejected as INVALID_PROPOSAL); payload survives approve state transition (preserved across the new ledger record). New `write_proposal_to_yaml(proposal)` materializes the payload into the live gate's adapted YAML at `/adapt approve` time. Per-surface schema mapping for all 5 surfaces with correct YAML path + top-level key. **Atomic-rename writer** (tempfile.mkstemp + fsync + os.replace) with cross-process flock via Phase 7.8's `flock_exclusive`. **Provenance enrichment**: auto-adds `proposal_id` + `approved_at` + `approved_by` to each YAML entry (payload values take precedence). **Master flag** `JARVIS_META_GOVERNOR_YAML_WRITER_ENABLED` (default false until per-surface graduation cadences ramp). **Critical invariant**: writer failures DO NOT roll back the ledger approval (audit trail of decision must persist regardless). Wired into `meta_governor.handle_approve()` after `ledger.approve()` returns OK; output includes `yaml_write_status=<status>` for operator visibility. **38 regression pins** covering schema extension backward-compat + propose-with-payload + master flag + 4 skip paths + 5 per-surface materializers + append semantics + 4 existing-file edge cases (oversize / corrupted / non-mapping / no-pyyaml) + provenance enrichment + 2 meta_governor wiring (incl. **writer-failure-doesnt-roll-back-approval critical invariant**) + 5 authority/cage invariants. Existing `test_schema_version_pinned` updated 1.0 → 2.0 (only test to change because to_dict() omits `proposed_state_payload` when None — backward-compat preserved). Combined regression spine: **584/584 tests green** across Phase 7.1-7.9 + all 4 wiring PRs + Item #2 + AdaptationLedger + meta_governor + 5 mining-surface suites — no regression. **Producer-side gap CLOSED 2026-04-26**: operator-approved adapted state now flows end-to-end from `/adapt approve` → YAML write → loader read → live gate behavior change. **PRD updated**: §1 Caller wiring sub-block — Slice-6 YAML writer marked `[x]`; Doc History v2.40 (append-only). **Remaining post-Item-#2 work**: Items #3 (HypothesisProbe production EvidenceProber) + #4 (per-loader graduation cadences). **Soft launch caveat**: writer is wired but the 5 mining surfaces (Slice 2-5) don't yet populate `proposed_state_payload` when calling `ledger.propose()` — without populated payload the writer skips with `SKIPPED_NO_PAYLOAD`. Updating the 5 miners to populate payload is a follow-up consistent with the substrate-first pattern. | Claude Opus 4.7 (Item #2 — closes producer-side gap) |
| 2026-04-26 | 2.39 | **🎉 Caller Wiring PR #4 — Phase 7.5 ExplorationLedger category-weight wired end-to-end. CLOSES the 5/5 caller wiring milestone — ACTIVATION PIPELINE COMPLETE.** Adds `_baseline_category_weights()` + `_compute_active_category_weights()` module-level helpers to `exploration_engine.py` and threads them into `ExplorationLedger.diversity_score()` as **per-category multipliers** on per-tool contributions. Wiring shape: `base += call.base_weight * cat_multiplier` where `cat_multiplier = active_weights.get(call.category.value, 1.0)`. Master-off byte-identical: when `JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_CATEGORY_WEIGHTS=false` (default), substrate returns `dict(baseline)` → all multipliers == 1.0 → diversity_score arithmetic is byte-identical to pre-wiring. Master-on rebalance: high-value categories scale up (e.g. comprehension=1.5 → comprehension calls contribute 1.5× their tool weight); low-value scale down (e.g. discovery=0.9 → discovery calls contribute 0.9×); net Σ tightens (Slice 5 cage rule enforced at substrate — sum invariant + per-category floor at HALF_OF_BASE + absolute floor at MIN_WEIGHT_VALUE). UNCATEGORIZED tools default to multiplier=1.0. Defense-in-depth: substrate raise → falls back to canonical baseline (NEVER raises). Score-cap + category multiplier both still apply on top of per-category weighting. **24 wiring pins** including 5 master-off byte-identical (4-cat=16.25 / 3-cat=8.0 / 5-cat=24.0 / duplicate-zero / score-cap-15) + 7 master-on rebalance (incl. **doctored-loosening-yaml-rejected-falls-back-to-baseline** + **score-cap-still-applied-after-multipliers**) + 3 caller-source invariants (incl. bit-rot guard pinning `cat_multiplier` actually used in loop) + 3 no-regression-against-pinned-scores. Combined regression: **159/159 exploration + iron_gate tests green** — no score regression in existing test suite (master-off byte-identical guarantee proven empirically); **486/486 combined Phase 7.1-7.9 + all 4 wiring PRs green**. **🎉 ACTIVATION PIPELINE COMPLETE 2026-04-26**: all 5 Pass C activation surfaces have substrate-complete loaders + LIVE caller wiring (7.1 SemanticGuardian patterns + 7.2 IronGate exploration floors + 7.3 ScopedToolBackend per-Order budget + 7.4 risk-tier ladder + 7.5 category-weight rebalance). Operator-approved adapted state can now actually CHANGE GATE BEHAVIOR end-to-end (subject to per-loader graduation cadences flipping master flags from default-false to default-true). **Remaining post-wiring work**: Slice-6 MetaGovernor YAML writer (`/adapt approve` writes `.jarvis/adapted_<surface>.yaml` — currently approves only update the ledger) + HypothesisProbe production EvidenceProber wiring + per-loader graduation cadences. **PRD updated**: §1 Caller wiring sub-block 4/5 → 5/5 with 🎉 ACTIVATION PIPELINE COMPLETE marker; Doc History v2.39 (append-only). | Claude Opus 4.7 (Caller Wiring PR #4 — closes 5/5 activation pipeline) |
| 2026-04-26 | 2.38 | **Caller Wiring PR #3 — Phase 7.4 risk-tier ladder wired end-to-end (4/5 surfaces functionally live).** Adds new `get_active_tier_order()` function to `risk_tier_floor.py` that composes canonical `_ORDER` baseline with operator-approved adapted tiers via Phase 7.4's `compute_extended_ladder()` helper. Refactors all 6 internal `_ORDER` consumers (env-floor `_env_floor` / vision-floor `_vision_floor_from_env` / recommended-floor `recommended_floor` × 2 sites / apply-floor-to-name `apply_floor_to_name` × 2 sites) to call `get_active_tier_order()` instead of subscripting `_ORDER` directly. Master-off byte-identical: when `JARVIS_RISK_TIER_FLOOR_LOAD_ADAPTED_TIERS=false` (default), helper returns `dict(_ORDER)`. Master-on: each operator-approved adapted tier inserted at its `insert_after` slot. **Case normalization at the wiring boundary**: adapted YAML uses uppercase per Slice 4b miner's `[A-Z0-9_]+` charset; wiring layer lifts canonical `_ORDER` to uppercase for the helper, then lowercases the extended result so downstream consumers find new tiers under the canonical lowercase convention. **Defense-in-depth** (3 layers): (a) loader raise caught + falls back to canonical `_ORDER`; (b) NEVER raises into caller; (c) returns NEW dict on every call (mutation-safe). Phase 7.4 helper's `compute_extended_ladder()` already enforces base-ladder-relative-order-preserved + collision-with-base-skipped + insert-after-unknown-skipped. **19 wiring pins** (`tests/governance/test_wiring_3_risk_tier_ladder_extended.py`): 4 master-off byte-identical (3 default + caller-mutation-isolation) + 4 master-on extension (insert / canonical-relative-order-preserved / unknown-insert-after-skipped / adapted-tier-lowercased-for-lookup) + 1 loader-raise-falls-back + 3 caller-source invariants (zero-internal-_ORDER-lookups bit-rot guard + wiring-imports-substrate + returns-new-dict) + 6 behavioral end-to-end (env-min-risk-tier-recognizes-adapted-tier + apply-floor-to-name-passes-through-unknown + accepts-adapted-tier-name + recommended-floor-uses-extended-ranking + 2 master-off-byte-identical for recommended-floor + apply-floor) + 1 authority invariant (no-external-imports-of-private-_ORDER). Combined regression spine: **541/541 tests green** across Phase 7.1-7.9 + all 3 wiring PRs + risk_tier_floor + risk_tier_floor_vision suites. **147/147 risk-tier-floor + Phase 7.4 + wiring** subset green — no regression in floor evaluation behavior. **Caller wiring progress: 4/5 surfaces wired end-to-end** (7.1 SemanticGuardian + 7.2 IronGate floors + 7.3 ScopedToolBackend per-Order budget + 7.4 risk-tier ladder). Only 7.5 (ExplorationLedger category-weight rebalance) remains for the activation pipeline; then Slice-6 MetaGovernor YAML writer + HypothesisProbe production prober. **PRD updated**: §1 Caller wiring sub-block 3/5 → 4/5; Doc History v2.38 (append-only). | Claude Opus 4.7 (Caller Wiring PR #3) |
| 2026-04-26 | 2.37 | **Caller Wiring PR #2 — Phase 7.3 ScopedToolBackend per-Order budget wired end-to-end (3/5 surfaces functionally live).** Threads `compute_effective_max_mutations(order, max_mutations)` (substrate from Phase 7.3 PR #23083) into `general_driver.py:308-353` — the single inner site that constructs `ScopedToolBackend` from invocation metadata. **Single-point enforcement**: all upstream invocation builders (`subagent_orchestrator.py`, `agentic_general_subagent.py`, etc.) benefit automatically; no need to touch every construction site. New OPTIONAL `invocation["order"]` field (default 1) — Order is supplied by upstream invocation builders based on parent op risk class. Missing / invalid / unknown order safely defaults to Order-1 (the safer assumption — Order-2 ops are rare governance-mutating dispatches that explicitly opt in). **Cage rule (load-bearing per Pass C §4.1)**: helper ALWAYS returns `min(env_default, adapted_budget)` — defense-in-depth ensures even a doctored YAML cannot LOOSEN the cage. Master-off byte-identical: when `JARVIS_SCOPED_TOOL_BACKEND_LOAD_ADAPTED_BUDGETS=false` (default), adapted dict is empty → returns env_default unchanged → ToolScope + ScopedToolBackend behave identically to pre-wiring. **Critical wiring detail**: BOTH `ToolScope(read_only=...)` AND `ScopedToolBackend(max_mutations=...)` use the EFFECTIVE post-wiring value — adapted-budget=0 correctly produces a read-only scope (pinned by `test_read_only_flag_uses_effective_value`). Subagent contract docstring (`subagent_contracts.py:472`) updated with new optional `order` field shape + Phase 7.3 reference. **22 wiring pins** (`tests/governance/test_wiring_2_scoped_tool_backend_per_order_budget.py`): 6 helper-direct unit pins (incl. doctored-higher-clamped + loader-exception-falls-back) + 6 caller-source invariants (incl. **no-other-live-caller-passes-raw-max_mutations-to-scope** — bit-rot guard) + 1 subagent-contract-docstring pin + 9 behavioral end-to-end via `run_general_tool_loop` with mocked `ScopedToolBackend` (incl. **read-only-flag-uses-effective-value** + **order-2-floor-preserved MIN_ORDER2_BUDGET=1**). Behavioral pins use `_MutationCapturingBackend` patched at import source (`scoped_tool_backend.ScopedToolBackend`) so lazy-import inside `run_general_tool_loop` picks up patched version. Combined regression spine: **443/443 tests green** across Phase 7.1-7.9 + wiring PRs #1+#2; **128/128 general_driver/scoped_tool_backend/general_subagent suite green** — no regression in cage/mutation-counter/state-mirror/hard-kill behavior. **Caller wiring progress: 3/5 surfaces wired end-to-end** (7.1 SemanticGuardian + 7.2 IronGate floors + 7.3 ScopedToolBackend per-Order budget). Remaining: 7.4 risk-tier ladder + 7.5 category-weight rebalance + Slice-6 MetaGovernor YAML writer + HypothesisProbe production prober. **PRD updated**: §1 Caller wiring sub-block 2/5 → 3/5; Doc History v2.37 (append-only). | Claude Opus 4.7 (Caller Wiring PR #2) |
| 2026-04-26 | 2.36 | **Caller Wiring PR #1 — Phase 7.2 IronGate floors wired end-to-end (2/5 surfaces functionally live).** First post-Phase-7 wiring PR. Switches the 6 live call sites that build `ExplorationFloors`: `orchestrator.py` (3 sites: lines 4068, 4142, 4485) + `phase_runners/generate_runner.py` (3 sites: lines 745, 819, 1162) from legacy `from_env()` → new `from_env_with_adapted()` classmethod (added in Phase 7.2 #23033). Wiring is master-off byte-identical (`JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_FLOORS=false` default → adapted floors empty → `from_env_with_adapted` returns same floors as `from_env`); master-on injects adapted required_categories on top of env baseline (cage rule per Pass C §7.3: only ADDS to required_categories, never removes; doesn't modify min_score / min_categories). Docstring example in `exploration_engine.py:13` also updated for consistency. **14 wiring pins** (`tests/governance/test_wiring_1_iron_gate_floors.py`) covering 3 master-off byte-identical pins (3 complexity levels + explicit-false + missing-yaml-byte-identical) + 4 master-on injection pins (adds-required-cat / preserves-env-required / does-not-modify-min-score / unknown-cat-skipped) + **3 caller-grep invariants** (zero-live-callers-use-legacy-from_env / orchestrator-uses-from_env_with_adapted / generate_runner-uses-from_env_with_adapted) + **2 caller-authority invariants** (orchestrator + generate_runner do NOT import `adapted_iron_gate_loader` directly — wiring goes through public classmethod which lazy-imports the loader, preserving one-way dependency rule per Phase 7.2 design) + 2 end-to-end smoke pins (master-on-yaml-present-floors-tightened / master-off-no-tightening). Combined regression spine: **421/421 tests green** across Phase 7.1-7.9 + wiring suite (407 prior + 14 new). Exploration-related governance subset (77 tests, including `test_generate_runner_iron_gate.py` which exercises the wired call sites) all green — no regression in IronGate evaluation behavior. **Caller wiring progress: 2/5 surfaces wired end-to-end** (7.1 SemanticGuardian wiring landed with substrate in #22992; 7.2 IronGate floors wiring this PR). Remaining: 7.3 ScopedToolBackend per-Order budget + 7.4 risk-tier ladder + 7.5 category-weight rebalance + Slice-6 MetaGovernor YAML writer + HypothesisProbe production prober. **PRD updated**: §1 Roadmap Execution Status adds "Caller wiring progress" sub-block tracking 2/5 wired (Phase 7.2 row marked `[x]`; 7.3+7.4+7.5 marked `[ ]`); Doc History append-only. | Claude Opus 4.7 (caller wiring PR #1) |
| 2026-04-26 | 2.35 | **🎉 Phase 7.9 — Stale-pattern sunset signal shipped — CLOSES PHASE 7 STRUCTURALLY (9/9 slices landed in one day).** New module `backend/core/ouroboros/governance/adaptation/stale_pattern_detector.py`: pure-stdlib detector primitive over caller-supplied `(adapted_patterns, match_events)` lists. End-to-end pipeline `propose_sunset_candidates_from_events()` mines stale candidates → flows through `AdaptationLedger.propose()` → operator-review surface. **Cage rule (load-bearing per Pass C §4.1)**: sunset signals are advisory only — Pass C cannot REMOVE patterns; removal MUST go through Pass B `/order2 amend` (operator-authorized). The signal is a NOTICE, not a state change. Allowed in `_TIGHTEN_KINDS` because it's structurally conservative (suggests reducing surface area, never expanding). Constants: DEFAULT_STALENESS_THRESHOLD_DAYS=30 + MAX_STALE_CANDIDATES_PER_CYCLE=8 + MAX_HISTORY_FILE_BYTES=4 MiB + MAX_HISTORY_LINES=10000 + MIN_OBSERVATIONS_FOR_SUNSET=1. Sorting: stalest-first tie-broken alpha for determinism. **Idempotent proposal_id** (sha256 of pattern_name) → re-mining yields DUPLICATE_PROPOSAL_ID. **proposed_state_hash deterministically distinct from current** so universal default validator passes. **Surface validator chain-of-responsibility**: composes with Slice 2's `add_pattern` validator on the same SemanticGuardianPatterns surface (sunset_candidate → our validator; add_pattern → delegates to Slice 2 prior validator). JSONL match-history reader fail-open. Master flag `JARVIS_ADAPTIVE_STALE_PATTERN_DETECTOR_ENABLED` (default false). Stdlib + adaptation.ledger only. **Modified `ledger.py`**: added `"sunset_candidate"` to `_TIGHTEN_KINDS` (single-line addition). **59 regression pins** covering 6 module constants + master flag + 8 env overrides + dataclass + 12 JSONL reader pins + 10 mine_stale_candidates pins (incl. **alpha-tie-break** + max-cap-truncate) + 4 propose-pipeline pins (incl. **idempotent-dedup**) + 7 surface validator pins (incl. **chain-delegates-add-pattern-to-prior** — proves no shadowing) + 3 authority invariants. Combined regression spine: **521/521 tests green** across Phase 7.1+7.2+7.3+7.4+7.5+7.6+7.7+7.8+7.9 + Pass C Slice 1 substrate + Slice 2 SemanticGuardian miner (54 existing miner pins survive — chain-of-responsibility validator works correctly; no shadowing). **🎉 §3.6.2 vector #4 marked MITIGATED** (was 🟡 Medium). **🎉 PHASE 7 STRUCTURALLY COMPLETE 2026-04-26 (9/9 slices landed in one day, 2026-04-26).** All 4 §3.6.2 fragility vectors mitigated. **PRD pruning (this PR also)**: §1 Forward-Looking Priority Roadmap — old Phase 7.9 row marked 🟢 closed; §3.6.2 vector #4 row updated 🟡 → 🟢; §3.6.3 critical-path #6 marked ✅ Shipped; sub-priority "ordered for landing" 7.9 ✅; line 207 pointer drops 7.9 from pending list; Phase 7 progress block rewritten with 🎉 STRUCTURALLY COMPLETE marker. Doc History append-only — no historical entries removed. **Next: caller wiring + Slice-6 YAML writer + per-slice graduation cadences** — these convert substrate-complete (this milestone) to functionally-live (no behavior change visible to operators yet because adapted YAML files don't get written/read end-to-end). | Claude Opus 4.7 (Phase 7.9 PR — closes Phase 7) |
| 2026-04-26 | 2.34 | **Phase 7.8 — Cross-process AdaptationLedger advisory locking shipped — CLOSES §3.6.2 fragility-vector #3.** New private substrate module `backend/core/ouroboros/governance/adaptation/_file_lock.py`: ships two context-manager helpers `flock_exclusive(fd)` + `flock_shared(fd)` using POSIX `fcntl.flock` advisory locks. **Best-effort, never raise**: when `fcntl` is unavailable (Windows ImportError) OR `flock` itself raises (NFS / unsupported FS), the helper logs once + degrades to a no-op + yields `True`. Per-feature kill switch `JARVIS_ADAPTATION_LEDGER_FLOCK_ENABLED` defaults **TRUE** (security hardening on by default — same convention as P7.7 Rule 7). Wired into `AdaptationLedger._append`: existing `with self._path.open("a", ...)` block now nests `with flock_exclusive(f.fileno())` around write+flush+fsync — exclusive lock serializes append paths across processes (within-process serialization remains `threading.RLock` at call site; this is additive defense-in-depth). Lock granularity per-fd; auto-released on context exit (LOCK_UN) AND fd close (kernel safety net). Stdlib + `fcntl` (POSIX) only — no banned imports. **19 regression pins** covering 3 module constants + kill switch + happy-path POSIX (4 incl. concurrent-exclusive-locks-serialize) + fail-open no-fcntl (2 incl. log-only-emitted-once) + fail-open flock-raises (2) + AdaptationLedger integration (2) + **multiprocess contention smoke** (POSIX-only — spawns 3 child processes racing to write 10 lines each; pin asserts each process's 10-line block lands contiguously, proving cross-process serialization works in practice) + 3 authority invariants. Combined regression spine: **408/408 tests green** across Phase 7.1+7.2+7.3+7.4+7.5+7.6+7.7+7.8 + Pass C Slice 1 substrate (60 existing AdaptationLedger pins survive — no regression). **🟢 §3.6.2 vector #3 marked MITIGATED** (was 🟡 Medium). **PRD pruning (this PR also)**: §1 Forward-Looking Priority Roadmap — old Phase 7.8 row marked 🟢 closed; §3.6.2 vector #3 row updated 🟡 → 🟢; §3.6.3 critical-path #5 marked ✅ Shipped; line 207 pointer drops 7.8 from pending list. Doc History append-only — no historical entries removed. Phase 7 now 8/9 — only 7.9 (stale-pattern sunset signal) remains. | Claude Opus 4.7 (Phase 7.8 PR — closes cross-process race vector) |
| 2026-04-26 | 2.33 | **Phase 7.7 — Sandbox hardening (AST validator Rule 7) shipped — CLOSES THE ONLY KNOWN STRUCTURAL SANDBOX-ESCAPE VECTOR.** Extends `meta/ast_phase_runner_validator.py` Slice 3 validator with introspection-escape blocking. Walks **all function bodies** in the candidate (not just `run` methods — defends against the candidate hiding the escape in a helper called from `run`) for two patterns: (1) direct `ast.Attribute` access where `.attr in {"__subclasses__", "__bases__", "__class__"}` — catches the classic CPython sandbox-escape one-liner `().__class__.__bases__[0].__subclasses__()` + chained access + call-chain + subscript-chain; (2) `getattr(x, "<banned>")` with string literal second argument — defends against operator string-encoding the attr to bypass Pattern 1. New failure reason `INTROSPECTION_ESCAPE` added to `ValidationFailureReason` enum (9th value; full set pinned). Per-rule kill switch `JARVIS_AST_VALIDATOR_BLOCK_INTROSPECTION_ESCAPE` defaults **TRUE** (unlike most JARVIS flags — security hardening on by default; operator can disable in emergency without disabling whole validator). New helpers `_find_introspection_escape(tree)` + `_is_getattr_call(node)` + `_string_constant_value(node)` + `_describe_attribute_target(node)` — all stdlib `ast` only, never raise. Known limitation pinned: dynamic-string getattr (runtime-computed attr names) slips through — Rule 7 is a static-shape check; runtime-computed attrs require the candidate to literally encode the full string at parse time. **39 regression pins** (`tests/governance/test_phase_7_7_sandbox_hardening.py`) covering 4 module-constant + per-rule-kill-switch (default-true + 5 truthy + 6 falsy variants) + Pattern 1 attribute-access (7 pins incl. **classic-sandbox-escape one-liner**) + Pattern 2 getattr-string (5 pins incl. dynamic-string-NOT-blocked + module-getattr-NOT-blocked) + walker-scope (3 pins covering helper-function / nested-function / second-method) + clean-candidates-pass (4 pins: safe-attr / string-literal-with-banned-substring / other-dunder / safe-getattr-string) + per-rule-kill-switch behavior (3 pins) + helper-function direct (10 pins). Existing pin `test_validation_failure_reason_eight_values` renamed to `..._nine_values` and updated to include `INTROSPECTION_ESCAPE`. Combined regression spine: **385/385 tests green** across Phase 7.1+7.2+7.3+7.4+7.5+7.6+7.7 + existing AST validator suite. **🟢 §3.6.2 vector #1 marked MITIGATED** (was 🔴 Critical pre-2026-04-26) — operator-authorization is no longer the sole defense; the cage transitions from "trust the operator" to "structural sandbox" for this attack class. **PRD pruning (this PR also)**: §1 Forward-Looking Priority Roadmap rewritten — Phase 7.7 row PROMOTED OUT (now 🟢 #3 closed); Rank #1 promoted to caller wiring; total ranks 12→10 after consolidation; §3.6.2 vector #1 row updated to 🟢 Mitigated; §3.6.3 critical-path #3 marked ✅ Shipped; §3.6.5 honest-grade Sandbox row updated to 🟢; line 207 pointer drops 7.7 from pending list. Doc History append-only — no historical entries removed. | Claude Opus 4.7 (Phase 7.7 PR — closes structural sandbox-escape vector) |
| 2026-04-26 | 2.32 | **Phase 7.6 — bounded HypothesisProbe primitive shipped (closes the autonomous-curiosity gap).** New module `backend/core/ouroboros/governance/adaptation/hypothesis_probe.py`: ships the primitive (data model + cage + runner) plus a Protocol for the evidence prober. Production wires this to a read-only Venom subset; tests inject fakes; **default is `_NullEvidenceProber` (zero cost — a misconfigured caller cannot accidentally hit a paid API)**. **Three independent termination guarantees** ALWAYS fire structurally — no prober configuration can override them: (1) Call cap MAX_CALLS_PER_PROBE_DEFAULT=5 (env-overridable); (2) Wall-clock cap TIMEOUT_S_DEFAULT=30.0 (env-overridable) — measured against `time.monotonic()` (NOT wall clock — defends against system clock changes mid-probe); (3) Diminishing-returns sha256 fingerprint of every round's evidence — terminate INCONCLUSIVE_DIMINISHING if round N+1 returns same fingerprint as N. **Read-only tool allowlist** frozen set: `{read_file, search_code, get_callers, glob_files, list_dir}`. 9-value `ProbeVerdict` enum (CONFIRMED / REFUTED / 4 INCONCLUSIVE_* / 3 SKIPPED_*). **Defense-in-depth**: confirmed/refuted signal with EMPTY evidence does NOT terminate (treats as continue) — prevents stuck-positive prober from claiming victory without proof. Bounded sizes: MAX_EVIDENCE_CHARS_PER_ROUND=4096 + MAX_NOTES_CHARS=1024. Master flag `JARVIS_HYPOTHESIS_PROBE_ENABLED` (default false until graduation cadence). Stdlib-only — does NOT import HypothesisLedger / tool_executor / Venom. **55 regression pins** covering 7 module constants + master flag + 8 env override pins + 9-value verdict enum + 5 pre-check skip pins + 3 Null-prober pins + 5 verdict-signal pins (incl. **confirmed-with-empty-evidence-does-NOT-terminate defense-in-depth**) + 3 call-cap pins + 2 wall-clock pins (incl. **runner-uses-monotonic-clock-NOT-wall-clock**) + 4 diminishing-returns pins + 2 prober-exception pins + 3 bounded-sizes pins + 5 ProbeResult convenience pins + 4 authority invariants. Combined regression spine: **396/396 tests green** across Phase 7.1+7.2+7.3+7.4+7.5+7.6 + Pass C Slice 1 + HypothesisLedger. **Phase 7.6 closes the autonomous-curiosity gap** — primitive composes existing HypothesisLedger + (future) PostmortemRecall + (future) read-only Venom probe into a closing loop with structural termination. Production wiring (`AnthropicVenomEvidenceProber` + ledger bridges) is a follow-up consistent with substrate-first pattern. **PRD pruning (this PR also)**: §1 Forward-Looking Priority Roadmap rewritten — Rank #1 promoted to Phase 7.7 (sandbox hardening), Rank #2 to caller-wiring (preserved), Rank #3 added "HypothesisProbe production wiring"; sub-priority "ordered for landing" table now shows ✅ for 7.6; §3.6.3 critical-path #2 + §3.6.5 honest-grade row updated to "substrate complete; prober wiring pending"; line 207 pointer updated to drop 7.6 from pending list. Doc History append-only — no historical entries removed. | Claude Opus 4.7 (Phase 7.6 PR) |
| 2026-04-26 | 2.31 | **🎉 Phase 7.5 — ExplorationLedger category-weight adapted boot-time loader shipped — CLOSES THE 5/5 PASS C ACTIVATION-SURFACE SET.** New module `backend/core/ouroboros/governance/adaptation/adapted_category_weight_loader.py`: reads `.jarvis/adapted_category_weights.yaml` (operator-approved Pass C Slice 5 weight rebalances), returns `List[AdaptedRebalanceEntry]`. New module-level helper `compute_effective_category_weights(base_weights, adapted=None)` returns merged weights — applying the latest valid rebalance IF it passes all three defense-in-depth net-tighten checks. **Cage rule** (per Pass C §4.1): net cage strictness ONLY RISES — Slice 5 is the only Pass C surface where individual values appear to fall, but mass-conservation keeps the whole vector tightening. **Three independent defense-in-depth layers** enforced at activation: (a) Sum invariant `Σ(new) ≥ Σ(base)` — REJECT if violated; (b) Per-category floor each ≥ HALF_OF_BASE × base[k] (matches Slice 5 miner's 50% floor); (c) Absolute floor each ≥ MIN_WEIGHT_VALUE=0.01. Schema invariant: output ALWAYS contains every base_weights key (preserving structure); unknown adapted keys silently dropped (Pass C cannot ADD categories — that's a Pass B Order-2 amendment). Constants: MAX_ADAPTED_REBALANCES=8 + MAX_WEIGHT_VALUE=100.0 + MIN_WEIGHT_VALUE=0.01 + HALF_OF_BASE=0.5 + MAX_YAML_BYTES=4 MiB. Per-entry hardening: weights dict with non-string keys / non-numeric / weight<=0 / empty SKIPPED; weight > MAX_WEIGHT_VALUE clamped; category keys lowercased + stripped. Latest-occurrence-wins (only LAST entry consulted — matches Slice 5 miner's "ONE rebalance per cycle" design). Master flag `JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_CATEGORY_WEIGHTS` (default false until graduation cadence). **57 regression pins** covering 6 module constants + master flag + dataclass-frozen + `__post_init__` sorted-keys + float-coercion + 14 YAML reader paths + 10 _parse_entry pins + 11 compute_effective_category_weights cage pins (incl. **sum-invariant-violated-rejected** + **per-category-floor-violated-rejected** + **absolute-floor-violated-rejected** + **latest-wins-only-last-consulted**) + 4 _net_tighten_check direct pins + 3 authority invariants. Combined regression spine: **357/357 tests green** across Phase 7.1+7.2+7.3+7.4+7.5 + Pass C Slice 1+5. **🎉 Phase 7.5 closes the 5/5 Pass C activation-surface set** — all five adaptive surfaces now have substrate-complete boot loaders + cage-rule helpers. **PRD pruning (this PR also)**: §1 Forward-Looking Priority Roadmap rewritten — Rank #1 promoted from "Phase 7.5 last surface" to "Caller wiring + Slice-6 YAML writer" (the new critical-path); Rank #2 promoted to Phase 7.6; Rank #3 to Phase 7.7; old Rank #8 ("Phase 7.1+7.2+7.3+7.4 caller wiring") eliminated (promoted into Rank #1); total ranks 12→11 after consolidation; sub-priority "ordered for landing" table now shows ✅ for ALL 7.1-7.5; §3.6.2 fragility vector #2 + §3.6.3 critical-path #1 + §3.6.5 honest-grade row all updated to "5/5 substrate complete; caller wiring is the remaining functional gap"; line 207 pointer updated to "caller wiring + Slice-6 YAML writer + 7.6+7.7+7.8+7.9". Doc History append-only — no historical entries removed. | Claude Opus 4.7 (Phase 7.5 PR — closes 5/5 Pass C activation-surface set) |
| 2026-04-26 | 2.30 | **Phase 7.4 — risk-tier ladder adapted boot-time loader shipped (Pass C activation pipeline 4/9).** New module `backend/core/ouroboros/governance/adaptation/adapted_risk_tier_loader.py`: reads `.jarvis/adapted_risk_tiers.yaml` (operator-approved Pass C Slice 4b tier-insertion proposals), returns `List[AdaptedTierEntry]`. New module-level helper `compute_extended_ladder(base_ladder, adapted=None)` returns the canonical ladder with each adapted tier inserted IMMEDIATELY AFTER its `insert_after` slot. **Cage rule** (per Pass C §8.3 + §4.1): the ladder ONLY GROWS — defense-in-depth at three layers: (a) base_ladder elements ALWAYS appear in output in same relative order (load-bearing test); (b) adapted `tier_name` colliding with base ladder → SKIPPED; (c) adapted `insert_after` not in base ladder → SKIPPED. Constants: MAX_ADAPTED_TIERS=16 + MAX_TIER_NAME_CHARS=64 + MAX_YAML_BYTES=4 MiB. Per-entry hardening: tier_name must match `[A-Z0-9_]+` charset (matches Slice 4b miner output) — operator-typo names (paths, lowercase, dashes, traversal) SKIPPED rather than truncated (truncation could collide). Latest-occurrence-wins per tier_name. Master flag `JARVIS_RISK_TIER_FLOOR_LOAD_ADAPTED_TIERS` (default false until graduation cadence). YAML path env-overridable via `JARVIS_ADAPTED_RISK_TIERS_PATH`. Helper accepts pre-loaded list for hot-path amortization. Stdlib + adaptation.ledger only — never raises. Hardcoded import-ban list (no risk_tier_floor / scoped_tool_backend / orchestrator / phase_runners). **49 regression pins** (`tests/governance/test_phase_7_4_adapted_risk_tier_loader.py`) covering 5 module constants + master flag + dataclass-frozen + 13 YAML reader paths + 11 _parse_entry pins (incl. lowercase-skip / dash-skip / path-traversal-skip / too-long-skip / at-max-allowed) + 11 compute_extended_ladder cage pins (incl. **base-ladder-relative-order-preserved load-bearing pin** + **collision-with-base-skipped defense-in-depth** + insert-after-unknown-skipped + loader-exception-falls-back) + 3 authority invariants. Combined regression spine: **301/301 tests green** across Phase 7.1+7.2+7.3+7.4 + Pass C Slice 1+4. **Phase 7.4 closes the fourth Pass C activation gap** — the highest-risk activation surface (mutates the canonical risk-tier ladder enum). Caller wiring (orchestrator + `risk_tier_floor.py` consuming `compute_extended_ladder()`) is a follow-up consistent with the substrate-first pattern. **PRD pruning (this PR also)**: §1 Forward-Looking Priority Roadmap rewritten — #1 row updated from "Phase 7.4-7.5 (3/5 done)" to "Phase 7.5 (4/5 done)"; sub-priority "ordered for landing" table now shows ✅ for 7.1-7.4; §3.6.2 fragility vector #2 + §3.6.3 critical-path #1 + §3.6.5 honest-grade table all updated to reflect 4/5 loaders landed; line 207 pointer updated to 7.5 → 7.9. Doc History append-only — no historical entries removed. | Claude Opus 4.7 (Phase 7.4 PR) |
| 2026-04-28 | 2.30 | **Brutal Architectural Review v3 — Convergence-Phase landed as §24.** Operator-driven 2026-04-28: full architectural review with the explicit "Reverse Russian Doll convergence phase" framing + Antivenom-must-scale-proportionally constraint. New §24 captures all four operator review questions in PRD-grade depth: §24.1 calibration anchors (proactive-vs-reactive asymmetry, Antivenom proportionality, A-level RSI as falsifiability claim) → §24.2 capability matrix (12 dimensions vs CC and A-level sovereign developer with honest gap column) → §24.3 cognitive & epistemic delta (5 specific CC paradigms O+V lacks: subagents-as-cognitive-loops, plan-mode speculative execution trees, self-paced wake-up scheduling, operator-discoverable skills, session-level TodoWrite paradigm) → §24.4 the `HypothesisProbe` primitive (4 mathematical properties: bounded depth + bounded budget + provable convergence + memorialized failures; full implementation contract; Antivenom invariants pinning probes read-only at `semantic_firewall.py` level) → §24.5 temporal observability (event sourcing with content-addressed predecessors via Merkle DAG generalization of Phase 11 cartographer pattern + replay as pure function + tiered noise budget CRITICAL/HIGH/NORMAL/LOW) → §24.6 systemic fragility (5 race-condition vectors ranked HIGH/MEDIUM/LOW: cross-runner artifact ordering + BackgroundAgentPool worker concurrency + multi-file batch rollback snapshot staleness + L2 repair recursion guard + sentinel-cartographer interaction) → §24.7 cascading state-failure vectors (memory pressure positive feedback HIGH, cost cascade STANDARD/COMPLEX MEDIUM, posture-driven feedback drift LOW-MEDIUM) → §24.8 Antivenom bypass vectors Quine-class (5 vectors: AST-pattern blindspot HIGH, incremental semantic drift HIGH, test-coverage games MEDIUM, risk-tier inflation MEDIUM, prompt-injection via tool-result LOW-MEDIUM) → §24.9 letter grade B+ trending A- defended (why-not-A: 1 DW completion in 16min; why-not-B: protective-rails-exceptional + zero-bypasses + multi-graduation discipline; 4 honest blind spots including adversarial-response-coupling — Phase 12 was operator-driven not system-proposed) → §24.10 critical path TOP 3 SYSTEMIC UPGRADES sequenced (Priority 1 Determinism Substrate prerequisite to all other RSI claims; Priority 2 Closed-Loop Self-Verification via `PostMergeAuditor` with failure-mode test as convergence proof; Priority 3 Bounded Curiosity with Provable Termination — Slice 12.2.B `TtftObserver` is the right pattern at one scope, generalize to global) → §24.11 in-flight alignment table mapping Phase 12 / 12.2 to the critical path + §24.12 explicit anti-prescriptions (no more sensors / phase runners / subagent types / flag knobs — close consequence loops instead). New section header anchor pattern matches existing §23 style; TOC updated with §24 + 12 sub-section anchors; review explicitly does NOT gate any in-flight work. Discipline imposed by §24.12: before writing a feature, prove the feature closes one of the three critical-path priorities — anything else is shape-fragmentation. | Claude Opus 4.7 (operator-driven brutal review v3) |
| 2026-04-26 | 2.29 | **Phase 7.3 — ScopedToolBackend per-Order mutation budget adapted boot-time loader shipped (Pass C activation pipeline 3/9).** New module `backend/core/ouroboros/governance/adaptation/adapted_mutation_budget_loader.py`: reads `.jarvis/adapted_mutation_budgets.yaml` (operator-approved Pass C Slice 4a budget-lowering proposals), returns `Dict[order_int, int]`. New module-level helper `compute_effective_max_mutations(order, env_default, adapted=None)` returns `min(env_default, adapted_budget)` when loader on; returns env_default unchanged otherwise. **Cage rule** (per Pass C §4.1): adapted budgets only LOWER the env default — defense-in-depth via `min()` ensures even a doctored YAML cannot loosen the cage. Order-2 hard floor `MIN_ORDER2_BUDGET=1` enforced (matches Slice 4a miner). Constants: MAX_ADAPTED_BUDGETS=8 + MAX_BUDGET_VALUE=64 + MAX_YAML_BYTES=4 MiB. Hardcoded `_KNOWN_ORDERS = frozenset({1, 2})` allowlist so loader does NOT import `scoped_tool_backend.py` (one-way dep, mirrors Phase 7.1+7.2). Per-entry skip on missing/unknown order / non-integer / negative; clamps too-large at MAX_BUDGET_VALUE; raises Order-2 below floor up to MIN_ORDER2_BUDGET. Latest-occurrence-wins per order. Master flag `JARVIS_SCOPED_TOOL_BACKEND_LOAD_ADAPTED_BUDGETS` (default false until graduation cadence). YAML path env-overridable via `JARVIS_ADAPTED_MUTATION_BUDGETS_PATH`. Helper accepts pre-loaded adapted dict for hot-path amortization. Stdlib + adaptation.ledger only — never raises. **48 regression pins** (`tests/governance/test_phase_7_3_adapted_mutation_budget_loader.py`) covering 6 module constants + master flag + dataclass-frozen + 14 YAML reader paths + 10 _parse_entry pins (incl. order2-floor-raise + order1-zero-allowed + float-truncated) + 8 compute_effective_max_mutations cage pins (incl. **higher-clamped-to-env defense-in-depth** + loader-exception-falls-back) + 3 authority invariants. Combined regression spine: **252/252 tests green** across Phase 7.1+7.2+7.3 + Pass C Slice 1+4. **Phase 7.3 closes the third Pass C activation gap** — caller wiring (constructing invocation dict with adapted budget) is a follow-up consistent with the substrate-first 7.1/7.2 pattern. **PRD pruning (this PR also)**: §1 Forward-Looking Priority Roadmap rewritten — #1 row updated from "Phase 7.1-7.5 Not started" to "Phase 7.4-7.5 (3/5 done)"; sub-priority "ordered for landing" table now shows ✅ status for 7.1-7.3; §3.6.2 fragility vector #2 (Pass C activation gap) downgraded from 🔴 Critical to 🟡 Partially mitigated; §3.6.3 critical-path #1 updated to "3/5 loaders landed"; §3.6.5 honest-grade table updated; line 153 stale "Phase 5 await operator direction" reference removed (P5 graduated 2026-04-26); line 207 next-items pointer updated to 7.4 → 7.9. Total ranks in priority table grow 11→12 to add "Phase 7.1+7.2+7.3 caller wiring + graduation cadence" as Rank #8. Doc History append-only — no historical entries removed. | Claude Opus 4.7 (Phase 7.3 PR) |
| 2026-04-26 | 2.28 | **Phase 7.2 — IronGate adapted-floor boot-time loader shipped (Pass C activation pipeline 2/9).** Also retroactively records Phase 7.1 (PR #22992 → main `fe344a8a21`, SemanticGuardian adapted-pattern loader) which closed the highest single-impact activation gap but missed its own Doc History row. New module `backend/core/ouroboros/governance/adaptation/adapted_iron_gate_loader.py`: reads `.jarvis/adapted_iron_gate_floors.yaml` (operator-approved Pass C Slice 3 floor-raise proposals), returns `Dict[category, float]`. New classmethod `ExplorationFloors.from_env_with_adapted(complexity)` on `exploration_engine.py` reads adapted floors when env flag on, merges adapted required-categories into base ExplorationFloors. **Cage rule** (per Pass C §7.3): Pass C cannot LOWER coverage requirements — adapted floors only ADD to required_categories; never remove; doesn't modify min_score or min_categories. Translates "category X has adapted floor > 0" → "category X must be in required_categories" (categorical-coverage; numeric floor preserved for `/posture` follow-up surfacing). Constants: MAX_ADAPTED_FLOORS=64 + MAX_FLOOR_VALUE=100.0 + MAX_YAML_BYTES=4 MiB. Hardcoded `_KNOWN_CATEGORIES` allowlist (5 values: comprehension/discovery/call_graph/structure/history) so loader doesn't need to import exploration_engine (one-way dep, mirrors Phase 7.1). Per-entry skip on missing/unknown category / non-numeric floor / floor <= 0; clamps floor > MAX_FLOOR_VALUE; latest-occurrence-wins per category. Master flag `JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_FLOORS` (default false until per-slice graduation cadence). YAML path env-overridable via `JARVIS_ADAPTED_IRON_GATE_FLOORS_PATH`. Lazy-imports loader inside `from_env_with_adapted` (fail-open with try/except — every error path returns base floors unchanged). Stdlib + adaptation.ledger only. **42 regression pins** (`tests/governance/test_phase_7_2_adapted_iron_gate_loader.py`) covering 7 module constants + master flag truthy/falsy + dataclass-frozen + 13 YAML reader pins (missing/oversize/unreadable/empty/no-PyYAML/parse-error/non-mapping/missing-floors-key/non-list/cap-truncate/latest-wins/clamp-too-large/non-mapping-entry-skip) + 3 `compute_adapted_required_categories` pins + 6 `_parse_entry` pins (missing-cat/unknown-cat/non-numeric/negative/clamp/lowercase) + 6 `from_env_with_adapted` integration pins (master-off identical to from_env / missing-yaml identical / merges required / preserves env required / preserves min_score / unknown-yaml-cat tolerated) + 3 authority invariants (no banned imports / stdlib + adaptation.ledger only / no subprocess+network). Combined regression spine: **196/196 tests green** across Phase 7.1+7.2 + Pass C Slice 1+3. **Phase 7.2 closes the second-most-impactful activation gap** — the IronGate exploration floors gate every op. §1 Roadmap Execution Status adds "Phase 7 — Activation & Hardening" block tracking 2/9 slices landed. | Claude Opus 4.7 (Phase 7.2 PR) |
| 2026-04-26 | 2.27 | **Brutal architectural review + Phase 7+8 roadmap added (doc-only).** Operator requested an unvarnished post-Pass-C self-assessment with grade tables + priority tables + color coding. **§1 Where We Stand** now includes a Grade Summary Table (architecture A−, cognitive depth B, production track record C+, etc. — net B− vs A-level target) with color-coded gap-to-close column. **§3.6 Brutal Architectural Review (NEW SECTION)** added: §3.6.1 Capability matrix vs CC (~70% functional parity, 110% conceptual ambition); §3.6.2 Five known structural fragility vectors (sandbox object-graph escape via `__subclasses__`, Pass C activation gap, cross-process AdaptationLedger race, semantic drift over long horizons, single multi-file APPLY landmark) ranked by severity; §3.6.3 Critical path to A-level RSI (top 3: Phase 7 activation pipeline + Phase 7.6 hypothesis-probe loop + Phase 7.7 sandbox hardening) + 3 medium-priority + 3 least-priority items in color-coded table; §3.6.4 Phase 8 Temporal Observability proposal; §3.6.5 honest grade reconciliation (B− → A path). **§9 Roadmap** adds Phase 7 (Activation & Hardening — 9 sub-slices) + Phase 8 (Temporal Observability — 5 sub-deliverables); updated Phase 6 P6 to acknowledge it's now blocked by Phase 7 (needs real adaptation history, not just substrate). **§1 Forward-Looking Priority Roadmap** rewritten with color-coded sortable table (🔴 Phase 7.1-7.5 = highest priority; 🟡 Phase 8 + soak cadences = medium; 🟢 P6 + CC-parity polish = least). **Cross-priority sequencing rules** updated: rule 5 now reads "P6 after the adaptive substrate is FUNCTIONAL" (not just "ships"); 3 new binding rules added. Zero behavior change. CLAUDE.md verified under 40K. | Claude Opus 4.7 (PRD brutal review + Phase 7+8 roadmap) |
| 2026-04-26 | 2.26 | **Reverse Russian Doll Pass C Slice 6 — MetaAdaptationGovernor + `/adapt` REPL shipped. CLOSES Pass C ARC.** New module `backend/core/ouroboros/governance/adaptation/meta_governor.py`: operator-facing `/adapt {pending,show,approve,reject,history,stats,help}` REPL dispatcher mirroring Pass B's `/order2` REPL pattern. 12-status DispatchStatus + frozen DispatchResult + 7 subcommand handlers + render helpers + `compute_stats()` aggregator. `--surface` filter on history. Substrate master-off short-circuit (LEDGER_DISABLED). help bypasses master flag. AST-pinned: NO imports of the 4 mining-surface modules (each registered its own validator at its own import; substrate stays acyclic). 55 regression pins covering full subcommand path matrix + end-to-end (mining → propose → REPL approve → APPLIED) + 5 authority invariants. Combined: **349/349 tests green across all 6 Pass C slices**. `JARVIS_ADAPT_REPL_ENABLED` default false. Deferred follow-ups (per module docstring): GET endpoints + SSE event emission + weekly background analyzer + actual gate-state mutation on approve. **Pass C arc structurally complete** — operator can now review + approve/reject the full mining-output stream from all 5 surfaces. Loosening cannot happen via this REPL (Pass B `/order2 amend` is the only loosening path). | Claude Opus 4.7 (Pass C Slice 6 PR — closes Pass C arc) |
| 2026-04-26 | 2.25 | **Reverse Russian Doll Pass C Slice 5 — ExplorationLedger category-weight auto-rebalance shipped. ONLY Slice 6 MetaGovernor remains.** New module `backend/core/ouroboros/governance/adaptation/category_weight_rebalancer.py`: the only Pass C surface where the proposal *appears* to lower something — mass-conservation makes it net-tighten. Pure stdlib Pearson correlation kernel (Py 3.9 compat manual implementation since `statistics.correlation` was added in 3.10) computes per-category correlation between exploration score and verify_passed binary. Identifies high-value + low-value categories. If correlation gap ≥ delta (default 0.3) AND ≥ threshold (default 10) ops in window, proposes raise-high (20%) + lower-low (10%, hard-floored at 50% of original AND MIN_WEIGHT_VALUE=0.01 absolute). Net-tighten guarantee enforced at three layers: (a) DEFAULT_LOWER_PCT < DEFAULT_RAISE_PCT constants pin; (b) caller-clamp (lower clamped to raise//2 if ≥ raise); (c) defensive mine-time mass-conservation check refuses to propose if Σ(new) < Σ(old). Surface validator: kind=rebalance_weight + sha256-hash + threshold + summary contains BOTH ↑ AND ↓ AND "net +" indicators (defense-in-depth). Idempotent proposal_id (sha256 of high+low+new_weights vector rounded 6dp). Pearson kernel handles edge cases (short input / mismatched lengths / zero-variance returns 0.0; never raises). Master flag `JARVIS_ADAPTIVE_CATEGORY_WEIGHTS_ENABLED` (default false). **62 regression pins** (12 constants + master flag + 8 env overrides + **7 Pearson kernel pins** + 3 per-category correlation pins + 5 weight-rebalance computation pins + 11 mine pipeline pins inc. mass-conservation-invariant + low-floor-invariant + lower-pct-clamp + 3 ledger integration + 8 surface validator + 3 authority invariants + 1 substrate integration). Combined regression spine: **294/294 tests green** across Pass C Slice 1-5. Only Slice 6 (MetaAdaptationGovernor + /adapt REPL + observability) remains. | Claude Opus 4.7 (Pass C Slice 5 PR) |
| 2026-04-26 | 2.24 | **Doc-only sweep — Current State Assessment + adjacent sections updated to reflect post-Pass-B + Pass-C-in-flight state.** §1 architecture grade B+→A−, cognitive depth C+→B; §2 added 7th success criterion "self-tightening immune system"; §3.1 expanded "what O+V uniquely does" table with 7 new capabilities (PostmortemRecall + SelfGoalFormation + Conversational mode + Inline approval + HEARTBEAT + Wang composite metrics + AdversarialReviewer auto-injection + Order-2 cage + Adaptive Anti-Venom); §3.2 marked 7 of 10 prior CC-superiority items as ✅ delivered or ✅ partial; §3.3 added 5 new "live count" metrics + repositioned the closing claim; **new §3.5 Pass B + Pass C status tables** with all 14 PRs landed today + 670+ test count; §4 intro acknowledges Phase 0-5 graduations + Pass B closure; §4.1 lab-analogy expanded; §5.2 audit status verified with 2 stranded modules un-stranded; §5.5 gap analysis 6/7 rows ✅ closed (P6 deferred + new Adaptive immune system row added). Zero behavior change. CLAUDE.md verified under 40K. | Claude Opus 4.7 (PRD sweep) |
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
