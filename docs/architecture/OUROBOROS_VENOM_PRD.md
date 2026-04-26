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
  - [x] Graduation pin tests (`tests/governance/test_postmortem_recall_graduation_pins.py`, 16/16 PASS)
  - [ ] Master flag `JARVIS_POSTMORTEM_RECALL_ENABLED` flip false→true (gate: 3 clean live sessions per §11 Layer 4)
- P0.5 — POSTMORTEM root-cause taxonomy expansion: [ ] not started
- P1 — Cross-session pattern detector: [ ] not started
- P1.5 — Self-RAG over own commit history: [ ] not started

**Phase 2 — Self-Direction**: [ ] not started
**Phase 3 — Operator Symbiosis**: [ ] not started (CC-parity items P2/P3 in §3.2 deferred here)
**Phase 4 — Cognitive Metrics**: [ ] not started (oracle_prescorer + vindication_reflector wiring lives here)
**Phase 5 — Adversarial Depth**: [ ] not started
**Phase 6 — Self-Modeling**: [ ] not started

Update discipline: each closing slice updates this section in the same PR. Status is the source of truth for "what's next" — when in doubt, the lowest-numbered `[ ]` row in the lowest-numbered active phase is the next slice.

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
| Conversational mode — natural dialog | ✅ first-class | ❌ intent-driven only | **P2** |
| Real-time token streaming with model thinking visible | ✅ always | ⚠️ phases only in headless | P3 |
| Lightweight approval UX | ✅ inline `[y/N]` | ⚠️ heavy PR flow only | **P3** |
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

#### P3 — Lightweight approval UX

**Problem**: Yellow/Orange-tier approval today = create a PR + review. That's heavy for fast iterations.

**Solution**: SerpentFlow inline approval mode for development:
- Show full diff in terminal with hunks
- Prompt: `[y]es / [n]o / [s]how stack / [e]dit / [w]ait` with 30s default timeout
- On `y`: apply (same path as auto-apply for SAFE_AUTO)
- On `e`: open in $EDITOR, then re-prompt
- Keep existing PR path for production work (operator setting decides)

**Acceptance criteria**:
- New env: `JARVIS_APPROVAL_UX_INLINE_ENABLED` (default false → opt-in)
- When enabled, NOTIFY_APPLY/APPROVAL_REQUIRED show inline prompt instead of (or in addition to) PR
- 30s timeout → auto-defer (not auto-approve — safety)
- Audit trail unchanged (every decision still in cancel ledger)

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
- All 7 metrics computed at session end
- Persisted to `.jarvis/metrics_history.jsonl` (cross-session)
- `/metrics 7d` REPL shows trends
- IDE GET `/observability/metrics`
- `composite_score.py` exists (per RSI_CONVERGENCE_FRAMEWORK.md Improvement 1) — verify before reimplementing

**Effort**: ~800 LOC + 35 tests (larger than original PRD estimate due to composite score depth).

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
- Reviewer hallucinations — findings must reference specific files / patterns; ungrounded findings filtered
- Reviewer disagreement with PLAN — use as warning, not gate (PLAN still authoritative; findings inform GENERATE)
- Cost budget exceeded — reviewer skipped silently with INFO log

**Effort**: ~1000 LOC + 40 tests.

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
