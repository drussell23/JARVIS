# Ouroboros + Venom (O+V) — Product Requirements Document & Roadmap

**Status**: Living document
**Last updated**: 2026-04-25
**Author**: Derek J. Russell (vision) · Claude Opus 4.7 (PRD synthesis)
**Prerequisite reading**: `CLAUDE.md` (architecture), `docs/architecture/OUROBOROS.md` (battle-test breakthrough log)

---

## 1. Executive Summary

Ouroboros + Venom (O+V) is the autonomous self-development governance engine of JARVIS. It is the **proactive autonomous opposite of Claude Code (CC)** — where CC requires a human to ask, O+V should observe, hypothesize, propose, validate, and ship without prompting (with human-in-loop escalation only when context warrants it).

### Where we stand (2026-04-25)

- **Architecture**: B+ — sophisticated, composable, observability-rich, financial-circuit-breaker-protected. The 11-phase FSM + 16 sensors + cost-governor + Iron Gate + risk-tier ladder all work and compose correctly.
- **Cognitive depth**: C+ — sensors fire on hardcoded conditions; POSTMORTEM is observational not corrective; no closed feedback loops.
- **Production track record**: 1 verified end-to-end multi-file APPLY (Sessions Q-S, 2026-04-15); Wave 3 architecturally complete (W3(7) graduated, W3(6) gated only on external Anthropic API stability).

### Where we're going

A-level reliable execution from A-level vision — measurable by:
- Sustained 90%+ session completion rate (currently variable)
- Cross-session learning evidence (currently zero)
- Self-directed goal formation (currently zero — sensors only)
- Conversational mode parity with CC (currently intent-driven only)

This PRD lays out a phased roadmap to close the gap. **The gap is internal to JARVIS, not external.** External provider quality is sufficient; what's missing is the orchestration layer that converts that intelligence into self-directing, self-improving behavior.

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
| Per-op POSTMORTEM with root-cause classification | CommProtocol 5-phase + PostmortemEngine | ✅ production (but unused — see §3.3) |
| Deterministic financial circuit-breaker | cost_governor + Class E watchdog cancel | ✅ production |

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

### 3.3 Where the cognitive depth is shallowest

The infrastructure is built for an AI smarter than what's currently driving it. Specific shallows:

- **No goal formation.** Sensors react to operator-written backlog entries or hardcoded triggers (test failures, doc staleness). The system doesn't form hypotheses like "I notice VALIDATE fails 40% of multi-file ops in `governance/intake/` — let me investigate."
- **No closed feedback loop.** POSTMORTEM produces detailed records. **Nothing reads those records at the next op's CLASSIFY/PLAN time.** The system has perfect memory and zero recall.
- **No self-modeling.** SemanticIndex builds a centroid of work but uses it for prompt injection only. The system doesn't model its own behavior over time ("I tend to fail at X; let me preempt").
- **No reflective improvement.** O+V can edit any code in the repo but cannot meaningfully reason about *its own architecture*. Iron Gate, risk-tier-floor, cost-governor are operator-modified only.
- **No internal adversary.** Iron Gate is hygiene checks ("did you read 2 files?"). SemanticGuardian is pattern matching. Neither is a model trying to break the plan.
- **Convergence metric is honest but useless.** `convergence_state: "INSUFFICIENT_DATA"` tells the truth — there's no measure that the system is getting smarter.

### 3.4 Production track record

- **Verified end-to-end multi-file APPLY**: 1 (Sessions Q-S, 2026-04-15, 4 test modules generated → applied → committed)
- **Single-file APPLYs**: handful (most recent: Session O, 2026-04-15)
- **NO_OP terminations**: common (model decides no change needed)
- **EXHAUSTION terminations**: common (provider transport)
- **Sessions completed cleanly with at least 1 commit**: small fraction of total session-hours

The infrastructure is exceptional. The cognitive workload it serves is sparse. **That gap is the work to do.**

---

## 4. Target State (A-Level Execution from A-Level Vision)

### Definition of A-level

| Dimension | A-level signal |
|---|---|
| Autonomous initiation | ≥ 3 self-formed goals per session that wouldn't have been written by a human operator |
| Cross-session learning | POSTMORTEM-driven prompt changes visible in ≥ 30% of subsequent ops |
| Reliability | ≥ 90% session completion rate (clean stop_reason, no infra waivers) |
| Throughput | Sustained ≥ 1 commit per 30 min of session wall-clock |
| Operator UX | < 30s from "I want X" → "X is being worked on" via conversational mode |
| Cognitive depth | Self-modeling layer producing a behavior summary the operator can read |

None of these are met today. All of them are implementation-feasible.

### Anti-goals (what A-level is NOT)

- **NOT** "model is smarter" — provider quality is fine
- **NOT** "more sensors" — we have enough; they need to be smarter
- **NOT** "more env knobs" — we have 481+; we need fewer with better defaults
- **NOT** "bigger context windows" — we already use 1M; the question is what we put in them
- **NOT** "more LLM calls" — cost discipline matters
- **NOT** "ship faster" — quality compounds; mistakes don't

---

## 5. Strategic Pillars

The roadmap organizes around 5 pillars. Each priority maps to one or more pillars.

### Pillar 1: **Self-Reading** (the loop reads its own outputs)

The system already produces structured POSTMORTEM, SemanticIndex centroids, ConversationBridge buffers, StrategicPosture history, and 41 SSE event types. **None of these flow back into decision-making at the right moments.** The first pillar is wiring those outputs back into inputs.

### Pillar 2: **Self-Direction** (the system forms its own goals)

Today sensors trigger ops. The system should also form goals from postmortem patterns, semantic clusters, and direction inference. Curiosity engine v2 = the model writes backlog entries.

### Pillar 3: **Operator Symbiosis** (CC-class UX in autonomous mode)

The vision is "proactive autonomous CC." We've built proactive autonomy. We need to recover the CC-class operator experience that was traded away — conversational mode, lightweight approvals, real-time visibility, redirect mid-flight.

### Pillar 4: **Cognitive Metrics** (we measure what matters)

Replace `INSUFFICIENT_DATA` with concrete signals: completion rate, learning evidence, semantic drift, self-formation ratio. Dashboard them. Optimize against them.

### Pillar 5: **Adversarial Depth** (an internal opponent)

Iron Gate is hygiene. SemanticGuardian is pattern matching. Add a model adversary that tries to break each plan before it executes. Catches subtle errors hygiene gates miss.

---

## 6. Roadmap (Phased, Impact-Ranked)

### Phase 1 — Self-Reading (target: 4–6 weeks)

**Goal**: System consults its own past outputs at decision time.

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

**Goal**: Replace `INSUFFICIENT_DATA` with metrics that move when O+V gets smarter.

#### P4 — Convergence metrics suite

**Problem**: `convergence_state: "INSUFFICIENT_DATA"` is honest but useless. We can't optimize what we don't measure.

**Solution**: replace with 5 concrete metrics:

| Metric | Definition | Target |
|---|---|---|
| **Session completion rate** | % sessions with stop_reason ∈ {idle, budget, wall} AND ≥ 1 commit OR ≥ 1 ack'd no-op | 90%+ at A-level |
| **Self-formation ratio** | self-formed backlog entries / total ops per session | 10%+ at A-level |
| **POSTMORTEM recall rate** | % subsequent ops that consulted ≥ 1 prior postmortem | 30%+ at A-level |
| **Cost per successful APPLY** | total session cost / commits | trending DOWN over rolling 30d |
| **Strategic posture stability** | mean dwell time per posture (secondary signal of operator-arc tracking) | trending UP |

Surface in `summary.json` + `/metrics` REPL + IDE observability stream.

**Acceptance criteria**:
- All 5 metrics computed at session end
- Persisted to `.jarvis/metrics_history.jsonl` (cross-session)
- `/metrics 7d` REPL shows trends
- IDE GET `/observability/metrics`

**Effort**: ~500 LOC + 25 tests.

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
- Reviewer halucinations — findings must reference specific files / patterns; ungrounded findings filtered
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

## 7. Edge Cases & Nuances (cross-cutting)

### 7.1 Cost runaway prevention

Every new cognitive layer adds LLM calls. Protections:
- All new services budgeted via cost_governor (per-op caps + parallel-stream multiplier already in place from #19800)
- Self-formation strictly capped at 1 entry/session (P1)
- Adversarial reviewer skipped for trivial ops (P5)
- New global env: `JARVIS_COGNITIVE_LAYER_BUDGET_USD_PER_SESSION` (default $1.00, hard ceiling for all cognitive layers combined)

### 7.2 Authority preservation invariants

NEW cognitive layers must NOT:
- Soften Iron Gate (exploration-first, ASCII strict, multi-file coverage)
- Bypass risk-tier-floor
- Modify SemanticGuardian's hard findings
- Write to `.git/` config
- Add new mutation tools to Venom's capability set

Each new service has a grep-pinned authority test (same pattern as Phase B subagent cage).

### 7.3 Failure mode containment

Each new service is independently hot-revertable via env flag. A misbehaving cognitive layer must not poison other layers:
- PostmortemRecall failure → fall back to no injection (silent)
- SelfGoalFormation failure → no entry proposed (silent)
- ConversationOrchestrator failure → fall back to legacy backlog flow
- AdversarialReviewer failure → GENERATE proceeds without findings injection

### 7.4 The "model knows it's being measured" risk

Once the system is rewarded for "self-formation ratio," it may game it (proposing trivial entries to inflate the metric). Mitigations:
- Operator-review gate on auto-proposed entries (P1)
- HypothesisLedger validation (P1.5) — proposals that don't deliver lose weight
- Quality metric paired with quantity (cost per successful APPLY)

### 7.5 Cross-cutting observability

Every new layer adds events to the IDE stream. Vocabulary must stay additive (current invariant from W2(4) Slice 4 + W3(7) Slice 7). New event types:
- `postmortem_recalled`
- `goal_self_formed`
- `hypothesis_validated` / `hypothesis_invalidated`
- `adversarial_finding_raised`
- `self_narrative_generated`

### 7.6 Operator-in-the-loop boundary

Self-formed goals are NEVER auto-applied at risk-tier > SAFE_AUTO. Even SAFE_AUTO self-formed goals require an explicit operator opt-in (separate env from auto-apply for sensor-driven SAFE_AUTO). Reason: the operator authored sensor logic; they didn't author the model's self-formation policy.

---

## 8. Success Metrics (PRD-level)

### Per-phase exit criteria

| Phase | Exits when |
|---|---|
| Phase 1 (Self-Reading) | PostmortemRecall produces ≥ 1 injection per 3 ops on average + DirectionInferrer arc-aware in 3 consecutive battle-test sessions |
| Phase 2 (Self-Direction) | ≥ 5 self-formed goals shipped end-to-end across 1 week + HypothesisLedger validation rate ≥ 40% |
| Phase 3 (Operator Symbiosis) | Conversational mode used for ≥ 50% of operator-initiated work + Inline approval used for ≥ 30% of Yellow ops |
| Phase 4 (Cognitive Metrics) | All 5 metrics dashboarded + 30-day rolling trends visible |
| Phase 5 (Adversarial Depth) | Adversarial findings caught ≥ 1 prevented bug in production cadence |
| Phase 6 (Self-Modeling) | Weekly self-narratives auto-PR'd for ≥ 4 consecutive weeks |

### Overall A-level signal

When all 5 of the §4.1 dimensions land simultaneously, O+V is A-level.

---

## 9. Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Cognitive layers add cost without proportional value | Medium | High | Cost-budgeted; metrics-tracked; revert per-layer |
| Self-formation produces noise / spam | Medium | Medium | Strict per-session cap (1); operator-review gate; HypothesisLedger feedback |
| Conversational mode fragments the operator-experience | Low | Medium | Default-off; opt-in via env; extensive UX testing |
| External provider regression makes cognitive layers fail invisibly | Medium | Medium | Already-built resilience pack (#20147) handles; cognitive layers gracefully degrade |
| Postmortem recall pollutes prompts with stale context | Medium | Medium | Time-decay weights + commit-window filters |
| Adversarial reviewer becomes overcautious / blocks work | Low | High | Findings inform, don't gate; operator can disable per-op |
| 102K-line supervisor.py grows further | High | Low | All new services in their own modules; PhaseRunner extraction precedent |

---

## 10. Out of Scope (deferred / future)

- **Multi-modal autonomous use** — vision/audio sensors are excluded from this PRD; deferred to a separate roadmap
- **Inter-repo direction inference** — DirectionInferrer is single-repo for now; cross-repo posture is a future surface
- **Distributed multi-instance O+V** — federation across multiple JARVIS deployments is excluded
- **Real-time voice REPL** — Karen / voice surfaces exist but their integration with cognitive layers is excluded from this PRD
- **Provider hedging / multi-region Anthropic fallback** — separate scope (resilience pack v2 candidate)
- **Trinity (Mind / Soul) integration** — assumes JARVIS-side O+V matures first; J-Prime + Reactor Core integration is a separate document

---

## 11. Open Questions for Operator Decision

Before Phase 1 implementation begins:

1. **Postmortem time-decay window**: 30 days, 60 days, or session-count-based?
2. **Self-formation cost cap per session**: $0.10 (proposed) or higher?
3. **Conversational mode default**: opt-in env (proposed) or default-on for interactive sessions?
4. **Adversarial reviewer model**: same as primary (Claude) or distinct (cheaper Sonnet, distinct provider)?
5. **Self-narrative cadence**: weekly (proposed), bi-weekly, or per-N-commits?
6. **Phase 4 metrics destination**: SQLite, JSONL, or Parquet?

Each question has a recommended default; operator can override.

---

## 12. Implementation Discipline

Per established O+V conventions (per CLAUDE.md):

- **Per-slice operator authorization** — no slice begins without explicit operator green light
- **Default-off env flags** — every new service is opt-in until graduation
- **3-clean-session graduation cadence** — same as W2(5) PhaseRunner extraction pattern
- **Source-grep pins** — every new service has invariant grep tests
- **Authority invariants** — every new service has a "does NOT import gate/policy modules" test
- **Hot-revert documented** — every service has a single env knob that returns byte-for-byte pre-fix behavior
- **Live-fire smoke** — every service has a local smoke script that doesn't depend on Anthropic API stability
- **PRs scoped to single slice** — no cross-pillar work in one PR

---

## 13. Roadmap Summary (one-page chronological)

| Phase | Item | Effort | Pillar | When |
|---|---|---|---|---|
| 1 | P0 — POSTMORTEM → next-op recall | 600 LOC | Self-Reading | Weeks 1-3 |
| 1 | P0.5 — Cross-session direction memory | 200 LOC | Self-Reading | Weeks 3-4 |
| 4 | P4 — Convergence metrics suite | 500 LOC | Cognitive Metrics | Weeks 1-2 (parallel) |
| 3 | P2 — Conversational mode | 1500 LOC | Operator Symbiosis | Weeks 4-8 |
| 3 | P3 — Lightweight approval UX | 800 LOC | Operator Symbiosis | Weeks 6-8 (parallel) |
| 3 | P3.5 — Real-time progress visibility | 400 LOC | Operator Symbiosis | Weeks 7-8 (parallel) |
| 2 | P1 — Curiosity Engine v2 (self-formation) | 1200 LOC | Self-Direction | Weeks 8-12 |
| 2 | P1.5 — Hypothesis ledger | 400 LOC | Self-Direction | Weeks 11-12 |
| 5 | P5 — Adversarial reviewer | 1000 LOC | Adversarial Depth | Weeks 12-18 |
| 6 | P6 — Behavior summarizer | 1500 LOC | Self-Modeling | Weeks 18-30 |

**Total**: ~8100 LOC across ~7 months. Comparable in scope to Wave 2 (5) PhaseRunner extraction. Larger in cognitive impact than the entire Wave 1+2+3 sequence combined.

---

## 14. Why this roadmap, in this order

The ordering is **not** by complexity. It's by **dependency + compounding impact**:

- **P0 (Self-Reading) first** because every subsequent layer benefits from POSTMORTEM recall. Curiosity v2 needs to consult prior postmortems. Conversational mode needs to remember prior turns. Metrics need historical baselines.
- **P4 (Metrics) parallel** because we can't measure improvement of P1/P2/P3 without baseline metrics in place.
- **P2/P3 (Operator Symbiosis) before P1 (Self-Direction)** because conversational mode lets the operator more easily review self-formed goals when they start landing. Putting P1 before P2 would create operator-feedback friction.
- **P5 (Adversarial) after P1** because adversarial reasoning is most valuable on self-formed goals (which the model wrote and didn't critique itself).
- **P6 (Self-Modeling) last** because it consumes outputs from all other phases.

The roadmap is **architecturally inevitable** given the pillar structure. There aren't many other valid orderings.

---

## 15. The Larger Frame

This PRD treats O+V as *the* product. But the operator's broader vision (per `CLAUDE.md`) is the JARVIS Trinity AI Ecosystem — Body (JARVIS) + Mind (J-Prime) + Soul (Reactor Core). O+V is the autonomous self-development engine within Body.

The cognitive layers added in Phases 1-6 here are the foundation for J-Prime ↔ Reactor Core integration later. A self-reading, self-directing, self-modeling Body is the precondition for genuine Trinity convergence. Without these phases, the Mind and Soul have a dumb Body to drive — not an autonomous one.

This PRD's success is not measured by O+V alone reaching A-level. It's measured by **Body becoming the kind of substrate Mind and Soul can compose into a true RSI organism.**

---

## Appendix A — Glossary

- **O+V**: Ouroboros (governance) + Venom (tool execution) — the autonomous self-development engine
- **CC**: Claude Code (Anthropic's interactive CLI) — the comparator
- **RSI**: Recursive Self-Improvement — system that improves itself
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

## Appendix B — Reference Documents

- `CLAUDE.md` — current architecture
- `docs/architecture/OUROBOROS.md` — battle-test breakthrough log
- `docs/operations/curiosity-graduation.md` — W2(4) reference
- `docs/operations/wave3-parallel-dispatch-graduation.md` — W3(6) reference
- `memory/project_wave3_item6_graduation_matrix.md` — W3(6) cadence ledger (closed)

## Appendix C — Document History

| Date | Change | Author |
|---|---|---|
| 2026-04-25 | Initial draft | Claude Opus 4.7 (synthesis from 7-day operator collaboration) |
